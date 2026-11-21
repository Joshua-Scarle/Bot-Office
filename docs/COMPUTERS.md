# Computers (device companion)

Bots can work on your own computers: list folders, read files, copy files in and out of their job
workspace, run commands and take a screenshot. A bot asks **once per task** with one card. After
you allow it, its calls in that scope run without more cards until the task ends. The computer
runs `botoffice-agent` (see `agent/`). The agent connects out through an SSH tunnel and never
opens a port. The computer's own deny-list always wins, whatever you allowed.

## Setting up

1. On the computer, install the agent and the tunnel (`agent/README.md`).
2. In Settings > Computers, press **Add computer** to get a pairing code. The code works once and
   lasts 10 minutes. On the computer, run `botoffice-agent pair ABCD-EFGH`.
3. Compare the fingerprint the agent printed with the one in Settings > Computers, then press
   **Activate**. Until you activate it, the device token is useless.
   Bot Office serves only agents 0.2 or newer (their hello says they keep their own deny-list and
   obey End grant). An older agent is turned away, and the audit shows `outdated_agent`.

## What a bot can do

`bin/machine` is the bot's helper. Everything that comes back from the computer is wrapped as
untrusted data.

| Command | Scope | Limits |
| --- | --- | --- |
| `machine list` | none | names and online state only |
| `machine request --scope read\|files\|shell` | the one asked | asks for the task's access up front |
| `machine ls PATH [--depth N]` | read | depth up to 3, 2000 entries; symlinks shown, never followed |
| `machine read PATH` | read | up to 1 MiB per call, files up to 16 MiB |
| `machine pull REMOTE DEST` | read | up to 100 MiB, into the job workspace only |
| `machine shot DEST` | read | a PNG up to 5 MiB, into the job workspace only |
| `machine push SRC REMOTE` | files | up to 100 MiB, from the job workspace only; checked against its sha256 |
| `machine run -- PROG ARG...` / `--sh "..."` | shell | 30 s default timeout, 300 s at most; 64 KiB per output stream |

The scopes nest: read is inside files, and files is inside shell. At most 30 requests per bot per
minute, and at most 20 cards per bot waiting at once.

## The card: one per task

A task is one job: an assignment run, or one chat turn. A new chat turn is a new task, so with the
default setting it asks again.

- **The first call of a task** (or `machine request --scope X`) raises one card. The card shows the
  bot, the computer, the task, the scope it needs (from the tool, or the one it asked for), how long
  it can last and the first calls waiting. **Allow for this task** or **Deny**. The card waits as
  long as other approvals do (Settings > Approvals). No answer in time is a deny.
- **Inside the grant there are no more cards.** Each call is still checked when it runs: the grant
  is live, its scope covers the tool, the job is still running, the computer is active and
  "Controlling a computer" is on. The computer checks its deny-list again.
- **A wider call** (a `push` under read, or a `run` under files) raises **one** widen card. Calls
  inside the current scope keep working while it waits. Allowing it widens the same grant. Its end
  time does not move. If a still wider call comes while a card waits, that card is withdrawn and a
  new one asks for the wider scope, so you never allow more than the card you saw. When the grant
  ends, its waiting widen card is denied with it.
- **The grant is bound** to this computer, this bot, this job and this scope. It ends when the job
  ends, when you end it, or at the time cap (**4 hours** by default; Settings > Computers, Grant
  cap, 1 minute to 24 hours), whichever comes first.

### Prompt setting (per computer)

| Setting | What happens |
| --- | --- |
| **Always ask** (default) | one card per task |
| **Remember for this bot** | after you allow one card, this bot's next tasks on this computer start without a card, up to the remembered scope, for 30 days. Settings > Computers lists it with **Forget**. Switching away from this setting forgets them all. |
| **Always allow** | no cards on this computer. Every call is still a task grant with the cap, is still audited, and still meets the computer's deny-list. |

The defaults are constants in `machines.py`: `PROMPT_MODE_DEFAULT = "ask"`,
`REMEMBER_TTL = 30 days`, `GRANT_CAP_DEFAULT_MIN = 240`. After you press **End grant** on a task,
that task asks again with a card under any setting.

## What the computer always refuses

The agent checks every call before Bot Office raises a card, and again right before it runs. A
refusal is sent back as "blocked" and goes in both audit logs. Only the computer's own files change
these lists. Nothing Bot Office sends can.

- **Paths**: `~/.ssh`, `~/.gnupg`, browser profiles (`~/.config/google-chrome`,
  `~/.config/chromium`, `~/.mozilla`), keyrings, `~/.password-store`, `/etc/shadow`, `/etc/gshadow`
  and the agent's own config and state. Add more in `deny.toml` (`paths`). Paths are resolved
  (`~`, `..`, symlinks) and walked again with `O_NOFOLLOW` at execution.
- **Commands** (as an argv and inside `sh -c` strings, through `sudo`, `env`, `timeout`, `xargs`,
  `bash -c`, `eval` and so on):
  - a recursive `rm` of `/`, your home folder or a folder above it, including `rm -fr /`,
    `rm -rf --no-preserve-root /`, `rm -rf ~/`, `$HOME` and `/*`;
  - `mkfs*`, `mke2fs`, `wipefs`;
  - `dd of=` a disk, and any write or redirect to `/dev/sd*`, `/dev/nvme*` and other disks;
  - `shutdown`, `reboot`, `poweroff`, `halt`, `init 0|6`, `systemctl poweroff|reboot|halt`;
  - a fork bomb;
  - a recursive `chmod`, `chown` or `chgrp` of `/` or your home folder;
  - a recursive `rm`, `chmod` or `chown` of a path with a shell variable in it (`rm -rf "$DIR/"`
    is `rm -rf /` when `DIR` is empty), so a bot writes such paths out;
  - `find / -delete`, and decoded text piped into a shell;
  - any word that names a deny-listed path;
  - your own regular expressions in `deny.toml` (`commands`).

  When the checker cannot read a shell string with confidence (unbalanced quotes, `$(...)`,
  backticks, `${...}`, `eval`, or a variable used as the command) and the string contains a
  catastrophic word anywhere, it refuses. This is a tripwire, not a sandbox: a command could write
  a script and run it. For commands, the sandbox switch is the real boundary.

## Stopping it

- **End grant**: on the card, in the sidebar banner that shows every live grant, or in Settings >
  Computers. Calls in flight fail at once. Running commands are killed on the computer (TERM, then
  KILL, to their whole process group). The rest of that task asks again.
- **Revoke** a computer: its device token dies, the connection closes, its grants end, waiting
  cards are denied and the agent exits. It needs a new pairing.
- **Stop all computer access** (Settings > Computers): switches the class "Controlling a computer"
  off, ends every grant, kills running commands and denies every waiting card. Bots cannot use
  any computer until you press **Resume computer access** (or switch the class on again in
  Settings > Approvals).
- **On the computer**: `systemctl --user stop botoffice-agent` (or stop the tunnel).

Tokens rotate on their own every 30 days. The old token keeps working for 5 minutes after a
rotation.

## Per-computer switches

Settings > Computers has two switches per computer. Both are off by default
(`machines.DEVICE_FLAG_DEFAULTS`).

- **Sandbox commands**: `run` happens in bubblewrap. The command gets a read-only view of the
  computer, no network and a private `/tmp`, and the deny-listed paths are hidden.
- **Confirm on the computer too**: a pop-up on the computer asks **once per grant**, and once more
  if the grant is widened. A no (or no answer) holds for the rest of that grant.

The computer's own `config.toml` can set each switch to `on`, `off` or `server` (the default).
`on` and `off` there always win over Settings.

## Records

- Bot Office keeps `machine_audit` with every event: pairing, activation, each card and decision,
  each grant (started, widened, ended and why), and **every call**: allowed, waiting, done,
  failed or blocked. Calls record the tool, the canonical path or the command, exit code and
  sizes. Settings > Computers > Audit shows it, filterable by computer, bot and session (job id).
- The computer keeps `~/.local/state/botoffice-agent/audit.jsonl` (`botoffice-agent audit`). It
  records each call with its bot, grant and outcome, plus every End grant.
- Neither record stores file contents or command output. Residual, by design: the card, the request record and both audits keep the command *text* (argv / `sh`, paths, sizes, hashes) so the owner can see what was asked and what ran; they do not store file *contents* or command *output*. A secret a bot puts on a command line (for example `--sh 'printf %s SECRET > file'`) will appear there, so do not pass secrets as arguments if you do not want them in the log. This is by design, like the documented `rm -r "$f"` refusal.
