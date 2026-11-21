# botoffice-agent

The device companion for Bot Office. It lets your bots list, read and copy files and run commands on
this computer. Bots get access **one grant per task**, and this computer's own deny-list wins over
every grant. It is MIT licensed and uses the Python standard library plus `websockets`.

## How it is safe

- **No open port.** The agent never listens. It connects out to `ws://127.0.0.1:18787`, which is
  the local end of an SSH tunnel to the Bot Office server (`botoffice-tunnel.service`). Before it
  sends its token there, it checks that the tunnel port belongs to your own user, so another
  account on this computer cannot catch it. Run the agent as your own user: as root it refuses.
- **Pairing, then Activate.** `botoffice-agent pair CODE` turns a one-time code from Settings >
  Computers into a device token. Before the computer can do anything, you check that the
  fingerprint the agent prints matches the one in Bot Office and press Activate.
- **One card per task.** A bot's first call in a task raises one card in Bot Office (bot,
  computer, task, scope, how long). After **Allow for this task**, its calls in that scope run
  until the task ends, you press End grant, or the time cap passes (4 h by default). Scopes nest:
  read (ls, read, pull, shot) is inside files (also push), and files is inside shell (also run).
  A wider call raises one widen card. Settings > Computers can switch a computer to
  "remember for this bot" (30 days) or "always allow". The default is "always ask".
- **This computer decides last.** Every call is checked here before Bot Office asks anyone, and
  again right before it runs:
  - Paths are resolved (`~`, `..`, symlinks), then walked from `/` with `O_NOFOLLOW` at execution,
    so a symlink planted later fails.
  - The **path deny-list**: `~/.ssh`, `~/.gnupg`, browser profiles, keyrings, `~/.password-store`,
    `/etc/shadow`, `/etc/gshadow` and this agent's own files, plus `paths` in `deny.toml`.
  - The **command deny-list**, for argv and `sh -c` strings, looking through `sudo`, `env`, `timeout`,
    `xargs`, `bash -c`, `eval` and so on. It refuses `rm -rf` of `/`, `~` or `$HOME` (and
    `rm -fr /`, `--no-preserve-root`, `/*`), `mkfs*`, `dd of=` a disk, writes to `/dev/sd*` and
    `/dev/nvme*`, shutdown, reboot, poweroff and halt, `systemctl poweroff|reboot`, fork bombs, and
    `chmod -R`/`chown -R` of `/` or `~`. It also refuses any word naming a deny-listed path, and
    your own `commands` patterns in `deny.toml`. A shell string it cannot read with confidence is
    refused if it contains a catastrophic word anywhere. It also checks the string as the shell would
    spell it out (`$IFS` as a space, other variables empty, `{a,b}` lists, line continuations) and
    reads abbreviated long options (`--rec` is `--recursive`). This is a tripwire, not a sandbox.
- **Sandbox** (switch, off by default). Commands run in bubblewrap: a read-only view of the
  computer, deny-listed paths hidden, no network, a private `/tmp`. When the sandbox ends, every
  process inside it ends too.
- **Confirm on this computer** (switch, off by default). One pop-up here per grant, and once more
  when the grant is widened. A no (or no answer) holds for the whole grant, widened or not.
- **Kill switches.** End grant in Bot Office stops that grant's calls here at once. Running
  commands get TERM, then KILL, to their whole process group. Revoke makes the agent exit (it is
  not restarted). Stop all computer access does both for every grant. Here:
  `systemctl --user stop botoffice-agent`.
- **Commands** run with a minimal environment and a 30 s default timeout (300 s at most). Each
  output stream is capped at 64 KiB. On timeout the whole process group is killed.
- **Audit log** at `~/.local/state/botoffice-agent/audit.jsonl` (mode 0600). It records each call
  with its bot, grant, job and outcome, refused calls, and every End grant. It never stores file
  contents or command output.

## Install (Linux laptop, from the release files)

You need Python 3.11 or newer, `ssh`, and optionally `bubblewrap` (sandbox) and `zenity`
(confirm pop-up). Copy the two release files to the laptop, for example to `~/Downloads`:
`botoffice_agent-0.2.0.tar.gz` and `botoffice_agent-0.2.0-py3-none-any.whl`. Check them against
`sha256sums.txt`:

```sh
cd ~/Downloads && sha256sum -c sha256sums.txt --ignore-missing
```

1. **A key for the tunnel only** (no passphrase, so the service can start on its own):

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/bo_tunnel -N '' -C "bo_tunnel-$(hostname)"
   cat ~/.ssh/bo_tunnel.pub
   ```

2. **On the server**, append ONE line to `~ubuntu/.ssh/authorized_keys` (back the file up first).
   Use the public key from step 1:

   ```text
   restrict,port-forwarding,permitopen="127.0.0.1:8787",permitlisten="127.0.0.1:1",command="/bin/false" ssh-ed25519 AAAA... bo_tunnel-laptop
   ```

   `restrict` turns off forwarding of agents and X11, PTYs and `~/.ssh/rc`. `port-forwarding` and
   `permitopen` then allow exactly one forward: to Bot Office on the server's loopback.
   `port-forwarding` also turns remote forwards (`ssh -R`) back on. `permitlisten="127.0.0.1:1"`
   turns them off again: port 1 is privileged, so the server never opens it for this user. (OpenSSH
   rejects a key whose line says `permitlisten="none"`, so do not use that.)
   `command="/bin/false"` stops the key from running a shell or commands. The tunnel uses `-N`, so
   it never needs one.

3. **Accept the server's host key once** and check that the key can tunnel but not log in:

   ```sh
   ssh -i ~/.ssh/bo_tunnel -o IdentitiesOnly=yes ubuntu@SERVER true; echo "exit $?"   # prints exit 1: no shell
   ```

4. **Install the agent** in its own virtual environment:

   ```sh
   python3 -m venv ~/.local/share/botoffice-agent/venv
   ~/.local/share/botoffice-agent/venv/bin/pip install ~/Downloads/botoffice_agent-0.2.0-py3-none-any.whl
   mkdir -p ~/.local/bin && ln -sf ~/.local/share/botoffice-agent/venv/bin/botoffice-agent ~/.local/bin/botoffice-agent
   botoffice-agent --version
   ```

   (Or `pip install ~/Downloads/botoffice_agent-0.2.0.tar.gz` to build from the source release.)

5. **The systemd user units** ship in the source release:

   ```sh
   mkdir -p ~/.config/systemd/user ~/.config/botoffice-agent
   tar -xzf ~/Downloads/botoffice_agent-0.2.0.tar.gz -C /tmp botoffice_agent-0.2.0/systemd
   cp /tmp/botoffice_agent-0.2.0/systemd/*.service ~/.config/systemd/user/
   echo 'BOTOFFICE_SSH=ubuntu@SERVER' > ~/.config/botoffice-agent/tunnel.env
   systemctl --user daemon-reload
   systemctl --user enable --now botoffice-tunnel
   ss -ltn | grep 18787        # the tunnel's local end: 127.0.0.1:18787 only
   ```

6. **Pair**: in Bot Office, Settings > Computers > **Add computer** gives a code. Then:

   ```sh
   botoffice-agent pair ABCD-EFGH
   ```

   It prints a fingerprint. Compare it with the one in Settings > Computers, then press
   **Activate** there.

7. **Start the agent**, and keep it running when you are logged out:

   ```sh
   systemctl --user enable --now botoffice-agent
   loginctl enable-linger "$USER"
   botoffice-agent status
   ```

   Settings > Computers now shows the computer online.

## Configuration

`~/.config/botoffice-agent/config.toml` (every key is optional):

```toml
server = "ws://127.0.0.1:18787"
# "on" or "off" here always win; "server" (the default) follows the switch in Settings > Computers
sandbox = "server"        # run commands in bubblewrap: read-only view, no network, private /tmp
confirm_local = "server"  # one pop-up on this screen per grant (and per widening)
confirm_command = ["zenity", "--question", "--title", "Bot Office", "--text", "{text}"]
confirm_timeout = 120     # no answer is a no; so is a missing confirm program
token_store = "auto"      # auto (keyring via secret-tool, else a 0600 file), keyring, file
```

`~/.config/botoffice-agent/deny.toml` adds to the built-in deny-lists. It never removes from them,
and only this file changes them:

```toml
paths = ["~/Private", "~/.aws", "~/.kube"]
commands = ['\bgit\s+push\b', '\bcurl\b.*\|\s*sh']   # regular expressions, searched in the command
```

Restart the agent after editing either file: `systemctl --user restart botoffice-agent`. Test a
command against the deny-list without running it:

```sh
botoffice-agent check --sh 'rm -rf ~/'     # refused: ...
botoffice-agent check -- ls -la            # allowed by the deny-list
```

With the sandbox on, a command sees the whole computer **read-only**. Deny-listed folders appear
empty and deny-listed files read as empty. It has no network, and only a private `/tmp` is
writable. If `bwrap` is not installed or cannot build a sandbox, sandboxed commands are refused
rather than run without it.

## Commands

- `botoffice-agent pair CODE [--server URL]`: pair this computer. Plain `ws://` and `http://` URLs
  are accepted only for 127.0.0.1.
- `botoffice-agent run`: the service loop (`botoffice-agent.service`). It reconnects with backoff.
- `botoffice-agent status`: the pairing, the fingerprint, the local switches and the deny-lists.
- `botoffice-agent audit [-n 20]`: the last entries of the local audit log.
- `botoffice-agent check [--sh STRING | -- ARGV...] [--cwd DIR]`: would the deny-list refuse it?
- `botoffice-agent unpair`: forgets the token here. Also press Revoke in Bot Office.

The token is rotated automatically every 30 days over the live connection. The old token keeps
working for 5 minutes after a rotation.

## Kill switches

| Where | What | Effect |
| --- | --- | --- |
| Bot Office card, sidebar banner, Settings > Computers | **End grant** | that grant's calls stop now; commands killed (process group) |
| Settings > Computers | **Revoke** | token dead, agent exits (exit 3, not restarted) |
| Settings > Computers | **Stop all computer access** | every grant ended, every card denied, no computer use until Resume |
| this computer | `systemctl --user stop botoffice-agent` | no connection at all |
| this computer | `systemctl --user stop botoffice-tunnel` | no route to Bot Office |

## Uninstall

```sh
systemctl --user disable --now botoffice-agent botoffice-tunnel
botoffice-agent unpair                      # then press Revoke in Settings > Computers
rm ~/.config/systemd/user/botoffice-agent.service ~/.config/systemd/user/botoffice-tunnel.service
systemctl --user daemon-reload
rm -rf ~/.local/share/botoffice-agent ~/.local/bin/botoffice-agent ~/.config/botoffice-agent
rm -rf ~/.local/state/botoffice-agent       # the local audit log; keep it if you want the record
rm ~/.ssh/bo_tunnel ~/.ssh/bo_tunnel.pub
```

On the server, delete the `bo_tunnel-...` line from `~ubuntu/.ssh/authorized_keys`.
