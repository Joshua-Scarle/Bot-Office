# Computers (device companion)

Bots can work on your own computers: list folders, read files, copy files in and out of their job
workspace, run commands and take a screenshot. Each call is **one approval you answer**, unless
you saved a narrow "Always" grant for it. The computer runs `botoffice-agent` (see `agent/`). The
agent connects out through an SSH tunnel and never opens a port.

## Setting up

1. On the computer, install the agent and the tunnel (`agent/README.md`).
2. In Settings > Computers, press **Add a computer** to get a pairing code. The code works once and
   lasts 10 minutes. On the computer, run `botoffice-agent pair ABCD-EFGH`.
3. Compare the fingerprint the agent printed with the one in Settings > Computers, then press
   **Activate**. Until you activate it, the device token is useless.

## What a bot can do

`bin/machine` is the bot's helper. It blocks like `desk ask` until you decide. No answer in time
means no. Everything that comes back from the computer is wrapped as untrusted data.

| Command | Limits |
| --- | --- |
| `machine list` | no approval; names and online state only |
| `machine ls PATH [--depth N]` | depth up to 3, 2000 entries; symlinks shown, never followed |
| `machine read PATH` | up to 1 MiB per call, files up to 16 MiB |
| `machine run -- PROG ARG...` / `--sh "..."` | 30 s default timeout, 300 s at most; 64 KiB per output stream |
| `machine pull REMOTE DEST` | up to 100 MiB, into the job workspace only |
| `machine push SRC REMOTE` | up to 100 MiB, from the job workspace only; bound to the file's sha256 |
| `machine shot DEST` | a PNG up to 5 MiB, into the job workspace only |

At most 30 requests per bot per minute, and at most 20 approvals per bot waiting at once.

## The approval

The card shows the computer, the canonical path (after `~`, `..` and symlinks were resolved on the
computer) or the exact command. The approval is bound to exactly that:

- **Single use.** It is bound to this bot, this job, this computer, this tool and these
  arguments, and for a push to the content's sha256. Anything different needs a new approval.
- **Checked again at execution.** Bot Office checks the binding, the job and the computer. The
  computer walks the path again without following symlinks, so a swap made after you approved
  fails. Its deny-list always wins.
- **Turned off with the class.** The class "Controlling a computer" in Settings > Approvals is on
  by default. Switching it off refuses every computer call. It never means "allow without asking".

**Always for this bot** saves a grant for 30 days that you can revoke in Settings > Computers:

- ls, read and pull: a path glob. `*` and `?` stay inside one folder, `**` spans folders.
- push: the destination folder. Replacing an existing file always asks again.
- run: the exact argv and folder. A grant is never allowed for shell strings, shell characters
  or interpreters like `bash`, `python` or `env`.

Patterns that cover a lot of the computer (fewer than three fixed folders) need an extra confirm.

## Per-computer switches

Settings > Computers has two switches per computer. Both are off by default, and the defaults are
set in `machines.DEVICE_FLAG_DEFAULTS`.

- **Sandbox commands**: `run` happens in bubblewrap. The command gets a read-only view of the
  computer, no network and a private `/tmp`, and the deny-listed paths are hidden.
- **Confirm on the computer**: a pop-up on the computer asks before each call as well.

The computer's own `config.toml` can set each switch to `on`, `off` or `server` (the default).
`on` and `off` there always win over Settings.

## Revoking

**Revoke** in Settings > Computers ends the device token, closes the connection, ends every grant
for that computer and denies the requests still waiting. The agent stops and needs a new pairing.
Tokens rotate on their own every 30 days, and the old token keeps working for 5 minutes after a
rotation.

## Records

- Bot Office keeps `machine_audit`: pairing, activation, every request, decision and result.
  Settings > Computers shows it.
- The computer keeps `~/.local/state/botoffice-agent/audit.jsonl`.
- Neither record stores file contents or command output.
