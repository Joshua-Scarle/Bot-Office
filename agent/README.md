# botoffice-agent

The device companion for Bot Office. It lets your bots list, read, copy files and run commands on
this computer, **one approved call at a time**. It is MIT licensed and uses the Python standard library plus `websockets`.

## How it is safe

- **No open port.** The agent never listens. It connects out to `ws://127.0.0.1:18787`, which is
  the local end of an SSH tunnel to the Bot Office server (`botoffice-tunnel.service`). Before it
  sends its token there, it checks that the tunnel port belongs to your own user, so another
  account on this computer cannot catch it. Run the agent as your own user: as root it refuses.
- **Sandbox.** With the sandbox switch on, commands run in bubblewrap. On timeout or cancel the sandbox ends
  with every process inside it. Without the sandbox, a command that starts a new session of its
  own (a daemon) can outlive the call.
- **Pairing, then Activate.** `botoffice-agent pair CODE` turns a one-time code from Settings >
  Computers into a device token. Before the computer can do anything, you check that the
  fingerprint the agent prints matches the one in Bot Office and press Activate.
- **Every call is approved in Bot Office**, unless you made a narrow "always" grant for it. The
  approval card shows the canonical path or the exact command. The approval is bound to exactly
  that: a different argument, or a different file for a push (sha256), needs a new approval.
- **This computer checks again** at execution:
  - Paths are walked from `/` without following symlinks, so a symlink planted after the approval fails.
  - The deny-list always wins. Built in: `~/.ssh`, `~/.gnupg`, browser profiles, keyrings, `~/.password-store`, `/etc/shadow` and this agent's own files. Add your own in `deny.toml`.
- **Commands** run with a minimal environment and a 30 s default timeout (300 s at most). Each output stream is capped at 64 KiB. On timeout the whole process group is killed.
- **Audit log** at `~/.local/state/botoffice-agent/audit.jsonl`. It records what ran, for which bot, and the outcome. It never stores file contents or command output.
- **Revoke** in Settings > Computers cuts the connection at once. The agent then exits.

## Install

```sh
pip install --user ./agent            # or: pipx install ./agent
ssh-keygen -t ed25519 -f ~/.ssh/bo_tunnel -N ''   # then allow this key on the server (port forwarding only)
ssh -i ~/.ssh/bo_tunnel user@server true          # once, to accept the server's host key
mkdir -p ~/.config/botoffice-agent ~/.config/systemd/user
echo 'BOTOFFICE_SSH=user@server' > ~/.config/botoffice-agent/tunnel.env
cp agent/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now botoffice-tunnel
botoffice-agent pair ABCD-EFGH        # the code from Settings > Computers > Add a computer
# check the fingerprint in Bot Office, press Activate, then:
systemctl --user enable --now botoffice-agent
```

A good `authorized_keys` line for the tunnel key on the server:
`restrict,port-forwarding,permitopen="127.0.0.1:8787" ssh-ed25519 AAAA... bo_tunnel`

## Configuration

`~/.config/botoffice-agent/config.toml` (every key is optional):

```toml
server = "ws://127.0.0.1:18787"
# "on" or "off" here always win; "server" (the default) follows the switch in Settings > Computers
sandbox = "server"        # run commands in bubblewrap: read-only view, no network, private /tmp
confirm_local = "server"  # also ask on this screen before each call
confirm_command = ["zenity", "--question", "--title", "Bot Office", "--text", "{text}"]
confirm_timeout = 120     # no answer is a no; so is a missing confirm program
token_store = "auto"      # auto (keyring via secret-tool, else a 0600 file), keyring, file
```

`~/.config/botoffice-agent/deny.toml`:

```toml
paths = ["~/Private", "~/.aws"]
```

With the sandbox on, a command sees the whole computer **read-only**. Deny-listed folders appear
empty and deny-listed files read as empty. It has no network, and only a private `/tmp` is
writable. If `bwrap` is not installed, sandboxed commands are refused rather than run without the sandbox.

## Commands

- `botoffice-agent pair CODE [--server URL]`: pair this computer. Plain `ws://` and `http://` URLs are accepted only for 127.0.0.1.
- `botoffice-agent run`: the service loop (`botoffice-agent.service`). It reconnects with backoff.
- `botoffice-agent status`: shows the pairing, the fingerprint and the local flags.
- `botoffice-agent unpair`: forgets the token here. Also press Revoke in Bot Office.

The token is rotated automatically every 30 days over the live connection. The old token keeps
working for 5 minutes after a rotation.
