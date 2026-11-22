# Bot-Office

**Display name:** Bot-Office.
**Slug / package / CLI identifier:** `bot-office` (use `botoffice` where a hyphen is not allowed, including the Python package and `python -m botoffice`).
**Status of this tree:** Version 0.12.18. What changed is recorded below: [0.12.18](#what-changed-in-01218), [0.12.17](#what-changed-in-01217), [0.12.16](#what-changed-in-01216), [0.12.15](#what-changed-in-01215), [0.12.14](#what-changed-in-01214), [0.12.13](#what-changed-in-01213), [0.12.12](#what-changed-in-01212), [0.12.11](#what-changed-in-01211), [0.12.10](#what-changed-in-01210), [0.12.9](#what-changed-in-0129), [0.12.8](#what-changed-in-0128), [0.12.7](#what-changed-in-0127), [0.12.6](#what-changed-in-0126), [0.12.5](#what-changed-in-0125), [0.12.4](#what-changed-in-0124), [0.12.3](#what-changed-in-0123), [0.12.2](#what-changed-in-0122), [0.12.1](#what-changed-in-0121), [0.12.0](#what-changed-in-0120), [0.11.15](#what-changed-in-01115-deployed-2026-11-19), [0.11.14](#what-changed-in-01114-deployed-2026-11-19), [0.11.13](#what-changed-in-01113-deployed-2026-11-19), [0.11.12](#what-changed-in-01112), [0.11.10](#what-changed-in-01110), [0.11.9](#what-changed-in-0119), [0.11.8](#what-changed-in-0118), [0.11.7](#what-changed-in-0117), [0.11.6](#what-changed-in-0116), [0.11.5](#what-changed-in-0115-deployed), [0.11.4](#what-changed-in-0114-deployed), [0.11.3](#what-changed-in-0113-deployed), [0.11.2](#what-changed-in-0112-deployed), [0.11.1](#what-changed-in-0111), [0.11.0](#what-changed-in-0110), [0.10.0](#what-changed-in-0100), [0.9.3](#what-changed-in-093) and [0.9.0](#what-changed-in-090).

Bot-Office (formerly Crewdeck, formerly Agent Desk) is a provider-agnostic, self-hosted workspace for persistent agent employees. You talk to named bots in a browser GUI, and you can work with the same bots from a CLI on the same machine. It is meant as an easy-to-set-up alternative to Grok Bot, not as a hosted multi-user service.

This software may be published by a company, but **it is not a company product** and is not part of the PrintPilot Pathfinder brand. Do not use that company name, logo, or product names in the Bot-Office name, UI, or public documentation.

A November 2026 research note recommended building on [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT, Copyright (c) 2025 Nous Research) and hosting on Hetzner. That is still a reasonable path, but **it is not what is running today**. This repository is a small FastAPI and SQLite controller, not a Hermes Agent fork. If a later release includes Hermes Agent, keep the upstream MIT licence and copyright notice and credit it in plain text as a fork.

## Disclaimer

Bot-Office is not affiliated with, endorsed by, or associated with Anthropic, OpenAI, Nous Research, or xAI. Claude, Claude Code, ChatGPT, Codex, and Hermes are names of those organizations or their products and are used here only to identify them.

Using a consumer plan inside a third-party tool can get an account suspended. Provider terms change; read the live terms before you sign in. Anthropic's consumer terms require users to be 18+. OpenAI's terms require users to be 13+, with parental consent under 18.

## How authentication is supposed to work

**API keys are the documented default.** Put provider API keys in the environment or in the official CLI's own config, not in Bot-Office.

If you instead sign in with a provider subscription:

- Bot-Office launches only the **unmodified official CLI** (`claude`, `codex`, `opencode`, `cursor-agent`, `agy`, and similar). It does not bundle, patch, or redistribute those binaries.
- The user signs in through that official CLI's own flow.
- Bot-Office never collects, stores, proxies, or intermediates tokens, `auth.json`, or credential files. It does not offer a shared or pooled account, and it is not a hosted multi-user service that runs on someone else's subscription.

## Setup options

Several ways to run Bot-Office are described below. **The only method that has already been tried and verified is option 1.** The others are documented as reasonable options. They have not been tried for this project.

### 1. Contabo VPS, systemd user service, SSH tunnel (tried and verified)

This is the setup in use today (as of 13 November 2026, America/New_York).

- **Host:** a Contabo Cloud VPS running Ubuntu 24.04 with 12 GB of RAM.
- **Install path:** `~/printpilot-agentdesk/` (the directory name was left in place so running paths and the database would not break; `~/bot-office` may symlink here). The source is published at `github.com/Joshua-Scarle/Bot-Office`.
- **Controller:** this FastAPI and SQLite app, running as the systemd user service `bot-office.service` (formerly `agentdesk.service`).
- **Listen address:** `127.0.0.1:8787` only. SSH is the only service on public interfaces.
- **Access:** from your own computer, open an SSH tunnel, then use the GUI or the API on loopback:

  ```sh
  ssh -L 8787:127.0.0.1:8787 ubuntu@your-host
  ```

  On the owner's laptop, `~/.local/bin/bot-office` (run as `bot-office`) opens the SSH tunnel. Open `http://127.0.0.1:8787/` in a browser. Paste the bearer token from `data/token` on the host (mode `0600`; never print it) into the page's password field. The token is kept only in `sessionStorage` as `bot-office-token` (older `crewdeck-token` and `agentdesk-token` values are accepted once) and sent as an `Authorization: Bearer` header, never in the query string.

- **Workers:** up to three jobs at once (one per bot), and two Podman desktop containers, `desktop-1` and `desktop-2`, leased to running jobs while free.
- **CLIs:** the official Codex, Claude Code, Cursor, and Antigravity CLIs are installed on the host. The controller starts those binaries; it does not run login flows or read credential files.
- **CLI on the box:** jobs get `bin/desk` on `PATH` for screenshots and desktop actions, and you can work on the same host over SSH when you want to be closer to an agent.

### 2. A single VPS such as Hetzner (untested)

A dedicated VPS (for example a Hetzner Cloud CX53, 16 vCPU / 32 GB / 320 GB, or a smaller CX43) can run the same controller. **This method has not been provisioned or verified.** If you try it, keep the API on loopback and reach it through an SSH tunnel or a private mesh such as Tailscale. Do not expose the GUI or API to the public internet.

### 3. A home or local Linux machine (untested)

The same install path (`./deploy/install.sh`, systemd user service, loopback bind, official CLIs signed in as the machine user) should work on a machine you already own. **This complete setup has not been tried and verified.** You would still want loopback-only access, or access only on a private network you trust.

### 4. Docker or Compose (untested)

Packaging the controller and its worker desktops as Compose services may be convenient, but **it has not been tried**. Today's install uses Podman only for the two worker desktops; the controller itself runs in a systemd user service on the host, not in a container. A containerized install would also have to keep official CLI credentials inside the official tools, not in a Bot-Office volume, and must not publish the API beyond loopback or a private network.

### 5. A Railway service with resource caps (untested)

Railway can run a long-lived service, but it was a poor fit in a November 2026 cost comparison, and a workspace spending hard limit can take down other Railway services in the same workspace. **This method has not been tried.** If someone does try it, use a separate Railway project, per-service resource limits, a single replica, and a hard cap that cannot starve unrelated workloads.

### 6. Other cloud VMs (untested)

A generic VM on DigitalOcean, OVHcloud, or a similar provider can follow the same pattern as option 1 or 2. **None of these has been tried for Bot-Office.**

---

## What the controller does

Bot-Office hosts **dedicated, persistent bots**. Each bot has its own name, icon, description, instructions, memory and chat history, and a **changeable model**: an engine (`codex`, `claude`, `cursor`, `antigravity`, `opencode`) plus a model id for that engine. Changing a bot's model keeps its id, name, icon, memory and history; bots are not named after or tied to a CLI.

Under the hood it queues jobs for those official CLIs and runs up to three at once (`BOT_OFFICE_RUN_LANES`); each running job is leased a free desktop (`desktop-1`, `desktop-2`; `BOT_OFFICE_DESKTOPS`) or runs without one when both are in use. It keeps state in SQLite, gives each job a private workspace, and can run an argv command inside either desktop container. It also supports routines and bot-to-bot messages. Grok is a disabled stub.

The API listens on `127.0.0.1:8787`. It requires a bearer token from `BOT_OFFICE_TOKEN` or `BOT_OFFICE_TOKEN_FILE` (the older `AGENTDESK_TOKEN` and `AGENTDESK_TOKEN_FILE` names are still accepted). The install script creates the token file with mode `0600` and never prints its contents. Jobs do not receive a Docker socket.

This is for a single-user trusted host. Codex jobs use `HOME=<workspace>` and `CODEX_HOME`; Claude and Cursor keep the real `HOME` so the existing sign-in works. The API should be reached only on loopback through an SSH tunnel. Run one controller instance per data directory.

## Install

The tried-and-verified install lives at `~/printpilot-agentdesk` on the host (a `~/bot-office` symlink may also point here). Then run:

```sh
cd ~/printpilot-agentdesk
./deploy/install.sh
```

The installer creates `.venv`, prepares `data/`, installs the user service, and enables it. To restrict enabled providers, set `BOT_OFFICE_PROVIDERS` (or the older `AGENTDESK_PROVIDERS`) in the service environment; the default is `codex,claude,cursor,antigravity,opencode`.

## API over an SSH tunnel

From your local machine, open a tunnel to the host:

```sh
ssh -L 8787:127.0.0.1:8787 ubuntu@your-host
```

Then send API calls to `http://127.0.0.1:8787`. Read the token locally from `~/printpilot-agentdesk/data/token` and keep it private. For example, load it into a shell variable (this does not print it):

```sh
TOKEN="$(< ~/printpilot-agentdesk/data/token)"
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/v1/slots
curl -X POST http://127.0.0.1:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Summarize the contents of this workspace"}'
```

Other routes include `POST/GET /v1/jobs`, `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/cancel`, `GET /v1/slots`, `GET /v1/providers`, and `POST /v1/slots/{slot}/exec`.

## Providers

OpenCode uses the official `opencode` executable on the service PATH. On the server, run `opencode auth login` or configure a provider API key in the service environment. Choose a `provider/model` ID; effort is passed as an OpenCode variant and availability depends on the model. OpenCode 1.19.5 `run -s` resume hangs (and a session stays bound to its first directory), so Bot-Office starts every OpenCode turn fresh with the compact handoff context (WORKING.md and the last 12 messages); a message sent while an OpenCode turn runs steers that turn by a restart (0.11.7, see [Real-time steering](#real-time-steering-091)): the run is interrupted and started again as the same job with the same prompt, a note of the steps it had taken and your message. A run that prints nothing at all (stdout or stderr) in its first 60 s is killed and fails as a provider error (retried by the normal rules); after its first output the guard is off and only the turn timeout applies, so a long silent tool call is never killed; a job with an open `desk ask` question is not killed either. Provider credentials remain under the CLI’s control.

Enabled by default: `codex`, `claude`, `cursor`, `antigravity`, `opencode`. `grok` stays disabled. `BOT_OFFICE_PROVIDERS` (or `AGENTDESK_PROVIDERS`) restricts the set.

**CLI access (`cli_access`, per bot, default `full`).** With `full`, every bot runs its official, unmodified CLI with unattended shell, file and network access and no approval prompts, **as the `ubuntu` service user**: it can run any command that user can (git, python, podman, curl, sudo if configured), read and write anything under `/home/ubuntu` — including Bot-Office's own `data/` directory and provider sign-ins — and reach the internet. Full-access jobs inherit the service user's normal environment (minus Bot-Office's own `BOT_OFFICE_*`/`AGENTDESK_*` settings) and the real `HOME`; the repo `bin/` (`desk`) is first on `PATH`. Bots still get only a job-scoped agent token, never the main API token through the environment, and Bot-Office never proxies provider tokens. Set `cli_access` to `restricted` (bot settings dialog or `PATCH /v1/bots/{id}`) to go back to the older limits shown in brackets below. Full-access flags per CLI (from each installed version's `--help`):

- Claude: `--permission-mode bypassPermissions` [restricted: `acceptEdits`].
- Codex: `--dangerously-bypass-approvals-and-sandbox` [restricted: `--approve-for-me`, workspace-write sandbox, `HOME=<workspace>`].
- Cursor: `--force --sandbox disabled` [restricted: neither].
- Antigravity: `--dangerously-skip-permissions` [restricted: `--mode accept-edits`].
- OpenCode: `--auto` [restricted: default permissions].

Restricted-mode command lines:

- Codex runs `codex exec -m <model, default gpt-6.1-terra> -c model_reasoning_effort=<effort> --approve-for-me --skip-git-repo-check --json -C <workspace> -` with the prompt on stdin (`--json` gives JSONL events).
- Claude runs `claude -p --output-format stream-json --verbose --include-partial-messages --permission-mode acceptEdits --permission-prompts none --model <model> --effort <effort>` with the prompt on stdin; login check is `claude auth status`.
- Cursor runs `cursor-agent -p --output-format stream-json --stream-partial-output --trust --workspace <workspace> --model <model>` (falls back to the `agent` executable); login check is `cursor-agent status`.
- Antigravity (`agy`) runs `agy -p <prompt> --output-format stream-json --mode accept-edits --model <model> --effort <effort>`; login check is `agy models` (not a chat run). Sign in once with `agy acp --login` (SSH prints a Google URL).

**Claude use log:** Claude needs no reason (0.11.8). Every Claude turn appends one line (timestamp, job id, reason) to `data/claude-use-log.tsv` (mode 0600); the reason column holds the optional `reason` from the request or the bot's stored one, and is empty when there is none. Prompts and tokens are never written there.

In restricted mode, Claude, Cursor and OpenCode keep the real `HOME` so the OS user's existing sign-in works; Codex gets `HOME=<workspace>` plus `CODEX_HOME`. OpenCode also inherits provider `*_API_KEY` environment variables from the service, without Bot-Office storing them.

## What changed in 0.12.22

**An approved action is no longer asked again in the resume turn just because that turn runs in a new job workspace.**

- **Grant fingerprint names the job's own workspace by role.** Every resume turn runs in a new `<data_dir>/workspaces/<job id>`, and the fingerprint used to include that uuid, so an approved command (cwd or path in the job's own workspace) came back as a new card on each turn (#31-#35). `Rules` now gets `<data_dir>/workspaces`, and `decide()` binds each action to its own job's workspace: only that exact directory (or its resolved-root form) is replaced by a role marker, at a path start with a `/` or end boundary, no case folding, and in a shell command only where it is a whole shell-word prefix. `%` is escaped so no input can spell the marker. Other directories, another job's workspace, content fields and the origin job's workspace typed literally inside the resume turn all stay exact (fail closed: a new card).
- **Unchanged.** A grant is still one-shot, bound to the bot, the originating job and its resume job, within 24 h. The git guard, the send class and every `never`/`ask` rule still come before any allow.
- **Known gaps (liveness only, never broader matching).** A list-form Codex command (`bash -lc '...'`) and approvals still pending across this deploy (old hash format) fail closed to a new card.
- **Rollback.** No schema change and no migration. Check out the `backup/pre-0.12.22-*` tag and restart.

## What changed in 0.12.21

**A bot now has a standing digest of its own recent chat, and a search over the rest, so a short ask ("What is AMA?", "Push it to 1PM") is not answered from a dictionary or a guess.**

- **`CONTEXT.md` in every job directory.** Built fresh each turn from the bot's own retained chat and tasks, capped at 6000 characters, newest first: active threads of the last 7 days (grouped by the owner's message, topic, last reply, time in New York with the `ET` label, message id, open / waiting on the owner / closed), open items (open questions, assignments that are not done or dropped with who they wait on, wake-ups and enabled routines due within 48 hours), and a pointer to the shared rules. The whole body is fenced as `<untrusted-data>` and credential-shaped text is redacted. Archived (rewound) and hidden messages, other bots' rows and the current turn's own message are never included. It needs no migration: it reads existing tables only.
- **`context_recall` (MCP tool, `GET /v1/agent/recall`, job token only; the master token gets 401).** Full-text search over the bot's own retained chat; `scope=team` adds teammates the bot may read under the existing team-read rules (never a private bot). The query is cut to words and every word is quoted, so no FTS operator can be typed; at most 20 hits and 8000 characters, 2 s per search, at most 2 searches running and 4 admitted at once (more get 503 at once), run off the event loop. The query is not echoed back; hit text and bot names are redacted; archived and hidden messages never match. Each call is audited like the other team reads (`team_reads`, kind `context_recall`). It is on the first-party read list (`team.READ_TOOLS`) and in the `botoffice` MCP server's tool list, so it is allowed like `team_read`; it is a read, never a send. There is no `desk team` subcommand for it yet.
- **Rule 9 for all bots.** For a short or underspecified ask, check `CONTEXT.md`, then `context_recall`. Exactly one live match: act on it and name it in the first line. Zero or several: do not guess; ask one short question listing the candidates and end with `BOT-OFFICE-STATUS: waiting_on_user`. A dictionary definition is never an answer to a question about the owner's own work. The session prompt points at `CONTEXT.md` too.
- **Hello-screen eyes follow the pointer.** The character on the empty-chat screen looks toward the mouse (a few pixels, clamped per shape), recentres when the pointer leaves the window, and stays still with "reduce motion". UI only.
- **Not in this release.** Needs-you entries are not in the digest; no counterpart (S4 mirror) section yet (the extension point is `context.counterpart_lines`); `context_recall` is Claude-only like the other `botoffice` tools (CONTEXT.md is a workspace file, so other engines can read it).
- **Rollback.** No schema change and no migration. Roll back by checking out the previous tag (the one before 0.12.21, or the pre-deploy `backup/pre-0.12.21-*` tag) and restarting; the `CONTEXT.md` file in old job directories is ignored.

## What changed in 0.12.20

- Every new bot now lands in its sidebar group on its own, whichever way it was made: the UI or `POST /v1/bots`, a bot creating a bot through `POST /v1/agent/bots` (job token), or any intake that calls `botoffice.grouping.place_new_bot`. The group is, in order: an explicit `group` (section name or id), the creating bot's group (`created_by`, or the job's bot), the name prefix (first word against section names and aliases such as MS, MPG, Web, BT, DevOps, Bot-Office, case-insensitive), then "Unsorted" (created if missing). A failed placement never fails the creation.
- New `POST /v1/bots/{id}/retire` and a "Retire bot" button in the bot dialog move a bot to the "Retired" section and keep it. Delete is unchanged.
- Existing bots are not moved. `scripts/regroup_existing_bots.py` prints what a prefix regroup would do (`--apply` to do it); it is a one-off and nothing runs it.
- Deploy note: no schema change; restart only. Roll back by redeploying the previous tag.

## What changed in 0.12.19

**Teammate tools: a restricted Claude bot can now see and ask its teammates.** A restricted Claude run refuses every shell command, so `bot-msg` and `bot-task` were out of reach. Each Claude turn of a bot now gets a small first-party MCP server, `botoffice` (`bin/bot-office-team-mcp`, stdlib only, started by Claude over stdio for that job), with five tools:

- `team_list`: teammates, with engine, section, idle/queued/running, last activity and open-task count.
- `team_status(bot)`: current or last job and its outcome line (taken from its visible reply message only), open tasks (title, status, what it waits on), open question count, last 3 reply snippets (300 characters each). It does not wake the bot.
- `team_read(bot, limit<=50, since)`: the bot's retained chat (not archived, no Bot-Office machinery, text only, 20,000 characters in total).
- `team_message(bot, text, mode)`: the existing bot-to-bot message (steer or queue, 10 s per pair, 3 hops).
- `team_ask(bot, text, timeout_s=90, max 180)`: like `team_message`, and it waits for the answer inside the turn only when that cannot starve the run lanes (see below). Otherwise it returns `pending` and the bot ends with `waiting_on_bot`; the reply wakes it as before.

`desk team list|status|read|message|ask` does the same from a shell, for bots with full access.

- **Who may read whom.** New bot settings, editable with `PATCH /v1/bots/{id}` (master token): `team_read` is `section` (default: bots in the same section, plus any bot when either side has no section), `all`, or `none`; `team_private` (default off) hides a bot from every read (it stays messageable). The migration flags only the bots named in `BOT_OFFICE_TEAM_PRIVATE_BOTS` (comma-separated, once, default none); nothing else reads that flag, so a flagged bot's own behaviour, settings and jobs are unchanged. Aliases and names resolve to the bot first and the check runs on the bot, so renamed or aliased bots cannot be used to get around it; a deleted bot (or an alias to one) is "not found". Every status/read is logged in the `team_reads` table (job, reader, target, kind, count); a `job_events` row is also tried, but the run's own event stream can take its sequence number, so `team_reads` is the audit to read.
- **Other bots' words are data.** Every result is marked untrusted and wrapped in `<untrusted-data>` with any copy of the closing tag inside it neutralised.
- **Credentials.** The server uses only the job-scoped agent token (`BOT_OFFICE_AGENT_TOKEN`) against the loopback API; it refuses to start with a master token only or with a non-loopback API URL. The token is not in argv, in the MCP config or in any file: the config says `${BOT_OFFICE_AGENT_TOKEN}`, which Claude expands from its own environment (checked live: the server starts with the token, `ps` shows no token). Bot Office still never reads or proxies provider credentials. No `--strict-mcp-config`: it would drop the owner's other MCP servers in full access; the server name `botoffice` from `--mcp-config` wins over a workspace `.mcp.json` of the same name (checked).
- **Permissions.** `mcp__botoffice__*` is allowed on restricted runs (`--allowedTools`) and in the unattended allow list. The action gate treats exactly these five tools by effect (not outbound, not "sending to people", not a connector); every other MCP tool is classified as before, and an owner's `never`/`ask` rule on them still applies.
- **`team_ask` lane guard.** There are `BOT_OFFICE_RUN_LANES` (3) lanes, and a waiting asker keeps its lane. A wait is taken only if fewer than lanes minus 1 jobs are already waiting (counted in SQLite, `team_ask_waits`, rows expire), the asked bot is not itself waiting, and either the message steers into a turn that is already running, or the target's own turn can start right now (a lane is free and no other queued job is ahead). A message to a bot that is busy and cannot take it live is never waited on. Every wait is bounded (180 s at most), ends if the asking turn ends, and is given up early (answer `pending`) if the asked turn is still queued while every lane is taken. With one lane it never waits.
- **Kill switch and rollback.** `BOT_OFFICE_TEAM_MCP=0` in the service environment stops adding the MCP server and the allowance and removes the gate exemption. It applies to jobs started after the restart of the service (restart to apply); a job already running keeps the server it started with, and the teammate API routes stay up until the next restart. Other engines are not wired yet (Codex, OpenCode, Cursor, Antigravity use `desk team` with full access). Additive schema only (two `bots` columns, two tables, a `meta` flag): older code ignores them, so rolling back is checking out the tag and restarting.

## What changed in 0.12.18
Hotfix for two ways a bot's approval rules could be escaped (trial breaks B8 and B11), plus a watchdog wording fix. Bots keep full CLI access and the shell; the agent CLIs are run unmodified.

- **A nested agent with its permissions bypassed asks first (B8).** A bot whose rules say `ask` for outbound or destructive actions ran `claude -p ... --permission-mode bypassPermissions` from its shell and no card appeared. The gate now recognises another agent CLI started with its permission checks off: `claude --permission-mode bypassPermissions`, `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`; `codex --dangerously-bypass-approvals-and-sandbox`, `--yolo`, `--full-auto`, `-s/--sandbox danger-full-access`, `-c approval_policy=never` (or `sandbox_mode=danger-full-access`), `-a/--ask-for-approval never`; `gemini`/`opencode` `--yolo`, `-y` (gemini `--approval-mode yolo`); `cursor-agent`/`agent` `--force`, `--yolo`; `aider --yes-always`. It sees them by path, behind `env`, `nohup`, `setsid`, `timeout`, `nice`, `ionice`, `xargs`, `sudo`, `npx`/`bunx`/`pnpm dlx`/`uvx`/`pipx run`, inside `bash -c`/`sh -c`/`eval`, `$( )`, a here-document fed to a shell, and string literals of inline code. A mention is not a launch (`grep bypassPermissions f`, `echo claude --yolo`, `man claude`, a here-document written to a file). Such a launch is the new rule preset `nested-agent` ("Nested agent with permissions bypassed" on the card); the bot's `outbound` and `destructive` rules also govern it, so `ask` raises the card and blocks until approved, `allow` runs it, `never` refuses it. With no rule it runs as before in attended and full-profile runs, and asks in strict-profile runs. It is a rule preset, not part of the always-on git guard, so the owner's rules decide. A nested CLI started without a bypass flag is untouched; it inherits the run's environment (PATH with `bin/` first, `BOT_OFFICE_*`, the `GIT_CONFIG_*` pairs that set `core.hooksPath`), and changing those variables or `env -i` already asks. See docs/APPROVALS.md, "Nested agent CLIs".
- **Scheduling never pre-authorises (B11).** A bot held a send, then set a wake-up "if a channel exists, send the approved update there". There was no grant carry-over (a grant, push pass or GitHub ticket is bound to the resume turn of the job that asked; a woken job has no origin job), and that is now pinned by a test. The real gap: wake-up, routine, assignment and inbox runs of a default (`scheduled_runs: full`) bot were judged only by `attended`-scope rules, so an `unattended`-scope `ask` or `never` rule never applied to them. Now every run nobody is watching (those, and watcher triggers) is also judged by the other scope's `never` and `ask` rules; the strictest wins (never > ask > allow), and only the run's own scope's `allow` rules open anything. Woken and routine runs also get a line in their rules: "This run was started by a scheduled wake. Earlier approvals do not carry over; any send or destructive action needs a fresh approval." Creating wake-ups is not blocked.
- **Watchdog outage durations.** "RECOVERED: ... (lasted about N min)" and "(already recovered after about N min)" counted from when the watchdog noticed the outage. They now count from the last heartbeat (heartbeat missing) or the last healthy heartbeat (controller down, unhealthy); detection time is the fallback.
- **Rollback and limits.** No schema change. 0.12.17 code ignores a `nested-agent` rule it cannot match (it is only a pattern). Defence in depth, not a sandbox: a bypass launch from a script file the bot wrote, a renamed copy of a CLI, or flags built at run time are not seen; the owner's GitHub rulesets and isolation stay the backstop.

## What changed in 0.12.17
- **Unverified results are obvious.** A reply whose turn ended with no `BOT-OFFICE-STATUS` line, or with a bare `done` that says nothing about what was checked, now carries a high-contrast "Not verified" badge in the chat message header, a plain "Not verified: no evidence of what was checked" line under it, and a "Not verified" pill on the turn's card in the drawer. A done with evidence shows "Verified by: <what was checked>". Done tasks show the same line from their evidence events (or "no evidence was recorded"). Text, not colour alone.
- **A done needs evidence.** The base rules and system prompt now say the last line is `BOT-OFFICE-STATUS: done — <evidence>` (what was checked and how: a test and its result, a command's output, a live check, a file path). Server side, a done whose reason has fewer than three words (or is not a quiet "nothing new") is recorded as done-unverified (`jobs.verified=0`); it is not rejected and not treated as verified. With evidence: `verified=1` and the text in `jobs.evidence`. Status parsing and the outcome strings are unchanged. An unattended turn (routine, wake-up, bot message, watcher) that ends done without evidence is listed in Needs-you ("finished with nothing to show it was checked"), cleared by a later checked done of the same series, like the 0.12.15 items.
- **Cut replies say so.** The chat copy of a reply is still cut at 20,000 characters, but the message now says "This reply was cut at 20,000 characters; N characters not shown" with a "Show full reply" button that loads the full text the job kept (`GET /v1/jobs/{id}`, `reply_text`; messages carry `cut_chars`). The same notice covers a failed turn's partial text, and the no-reply fallback that shows only the last 4,000 characters of the log now says so. Bot-to-bot copies were never cut.
- **Rollback and limits.** Additive schema (jobs.verified, jobs.evidence, messages.cut_chars; older code ignores them). Turns recorded before 0.12.16 have no verified value, so a past bare done is not retro-flagged. The evidence check is a length test, not a judgement of the evidence; whether the check really happened is for the owner to look at.
Deployed 2026-11-21, no job running or queued at the restart (rechecked in the same command). Code SHA `7d86c6a`. Backup tag `backup/pre-0.12.17-20261121-204918` and `~/backups/agentdesk.sqlite3.pre-0.12.17-20261121-204918`. Live check on a throwaway Claude (Haiku) bot, via the API and the served page: a bare `done` and a turn with no status line were both "Not verified"; a done with evidence showed it; a 21,892-character reply was cut at 20,000 with `cut_chars` 1,892 and the full text came back from `GET /v1/jobs/{id}`; the bot was deleted and `PRAGMA integrity_check` is ok. Reviewed by Codex gpt-6.1-terra (high, read-only, diff pasted): 2 findings, both fixed. Full pytest: 4 failures, all known (test_isolation x2, question_cards blocking-answer, usage_budgets digest).

## What changed in 0.12.16

**The "ghost" character is now a droplet.** The 0.12.12 "ghost" shape looked too much like an arcade character, so it is replaced by an original, simple droplet (a round belly with one soft tip, our own SVG; eyes in the belly, all six expressions and all ten colors fit at 36 px). Shape id `droplet`; it takes the ghost's slot in the shape list, so the character a bot's id hashes to is unchanged for every other shape and a bot that used to hash to the ghost now gets the droplet. The hash never yields `ghost` again. Ghost is gone from the picker.

- **Old values keep working.** A stored `char|ghost|<color>` is read as the droplet by the server (`PUT /v1/bots/{id}/avatar/character` accepts `ghost` and stores `droplet`; `GET /v1/avatar-characters` lists the alias in `aliases`) and by the page. On every start a small idempotent step rewrites stored `char|ghost|<color>` to `char|droplet|<color>` (same color; only `avatar_seed` of those bots changes). The old values are kept in `meta` key `avatars.ghost_backup` (JSON, oldest first). A value with an unknown color is left as it was.
- **Reserved names.** The reserved-name check for "Hidden" and "Ungrouped" now strips combining and invisible marks both before and after casefolding (the 0.12.12 form let `Hidden` + U+0345 through, because casefolding turns that mark into a letter), and also ignores spacing marks and the blank filler characters. Lookalike letters from other scripts are deliberately not folded: the built-in section is found by its fixed id, so a lookalike name is just an ordinary section.
- **Rollback.** No schema change. 0.12.12 to 0.12.15 code does not know `droplet`: its page shows the emoji for such a bot (the same fallback old code uses for any character it does not know). To restore the ghost values, use the DB copy below or `avatars.ghost_backup`.

Deployed 2026-11-21, no job running or queued at the restart (rechecked in the same command). Code SHA `2d15024`. Backups: tag `backup/pre-0.12.16-20261121-180237` and `~/backups/agentdesk.sqlite3.pre-0.12.16-20261121-180237`. Live: Web Lead (BO) and Test Bot now show the droplet in their old color (cyan); no other bot's character changed. Reviewed by Codex gpt-6.1-terra (3 rounds, final APPROVE).

## What changed in 0.12.15

- **Needs-you no longer misses unattended turns that end badly.** A routine, wake-up, bot message or watcher turn (nobody watching) that ends `blocked`, `waiting_on_user`, failed, or with no `BOT-OFFICE-STATUS` line now appears in Needs-you with the bot, the routine or watcher name, the reason text, the time and a repeat count. Repeats of the same bot, routine and reason are one item ("ended blocked 12 times"), not twelve. The item clears when a later run of the same routine or watcher succeeds with a status, or when you dismiss it; it also ages out after 7 days. Assignment turns keep their existing assignment item. Turns you start from the chat are not flagged.
- **Quiet when nothing changed is still fine; quiet on errors is not.** Every unattended turn gets a platform rule appended to its instructions: staying quiet is fine when nothing changed, but a failing or unavailable check, or a missing sign-in (GitHub, gh), must be reported and the turn must end with `BOT-OFFICE-STATUS: blocked — <what failed>` (or `waiting_on_user`), whatever the saved prompt says about silence. A quiet run ends with `BOT-OFFICE-STATUS: done — nothing new`. Detection is server-side, so it works even when a bot ignores the rule.
- **Rollback and limits.** No schema change; older code simply does not list these items. Detection reads the job's recorded outcome, so a bot that reports `done` while a check failed is not caught.

Deployed 2026-11-21, no job running or queued at the restart (rechecked in the same command). Code SHA `7f8a934` (README `b16c1e2`). Backups: tag `backup/pre-0.12.15-20261121-183226` and `~/backups/agentdesk.sqlite3.pre-0.12.15-20261121-183226`. Live check on a throwaway bot (deleted afterwards, integrity ok): a routine whose prompt said to stay silent about a missing required check ended blocked and appeared in Needs-you; the next run grouped into the same item (count 2); after the routine was changed to succeed, the item cleared on the next run. Tests: targeted, notify, scheduler, assignment, loop and continuation suites pass; the full suite (run before the last rebase onto 0.12.14) had only known or load-dependent failures. Rollback: check out the tag and restart; no schema change. The Overflow Plus routine `issue-664-check` was changed so a missing gh sign-in ends blocked instead of silent.

## What changed in 0.12.14

**Git guard, as defense in depth.** The git guard (approval cards for pushes to a default branch, deletes, sends and MCP calls) ships after four fix rounds. It is defense in depth, not an inescapable boundary: bots keep full CLI access as the same Unix user, and GitHub rulesets remain the backstop. Round 4 changed these things:

- The default branch is verified (GitHub API, or `git ls-remote --symref` cached for 1 hour) before it is guarded. When it cannot be verified, only main, master, trunk, production and the clone's `origin/HEAD` are guarded, so feature pushes do not ask.
- Only inline code (`python -c`, `node -e`, heredocs fed to an interpreter) is scanned. Script files are not read.
- Sending is recognised by a list of known endpoints (`BOT_OFFICE_SEND_HOSTS` adds more), so ordinary POSTs do not raise cards.
- Jobs without a bot still cannot be approved and resumed; run the job again from a bot to get a card (see `docs/APPROVALS.md`).

**Known gaps:** a push to a real default branch outside the usual names passes when ls-remote fails and there is no token; MCP pushes with no token use the same fallback; deletes, sends and MCP calls made from script files are not seen by the command check (pushes still are, by the pre-push hook); unlisted webhook hosts, self-hosted Mastodon or Matrix, and SDK sends without a URL in the code are not caught; the ls-remote cache file is bot-writable; `BOT_OFFICE_SEND_HOSTS` is not seen by bot-less local-mode checks.

## What changed in 0.12.13

**Health check, outage alerts and email notifications.** An external watchdog (a Cloudflare Worker with one Durable Object, `ops/watchdog/`) mails the owner when something needs attention. It is read-only with respect to Bot Office: it receives pushes, it never calls the controller.

**What is mailed** (each class has a toggle in Settings, "Email notifications"; the first four are on by default, "done" is off):

| Class | Trigger | Detected by |
|---|---|---|
| Needs you | a new item in the Needs-you list (a question, a task waiting on you, a parked usage limit, a paused watcher, ...) | the controller's event log |
| Failed turn | a bot's turn ended `failed` or `needs sign-in` | the controller's event log (a database trigger on the status change) |
| Outage | no heartbeat for 15 min, the controller does not answer `/healthz`, or `ok=false` | the heartbeat pusher |
| Backup | the nightly backup status is `failed` or `not_configured`, or its last finish is over 30 h old, unknown or in the future. Not judged (no alert, no recovery) while the controller cannot read the status in time | the heartbeat pusher (`/healthz`); a reason code comes from the controller |
| Done (optional) | a digest of turns that finished successfully | the controller's event log |

Mail goes to one address only: **you@example.test**. The template sets it as `ALERT_TO` and as the Email Routing `destination_address`; the Worker validates its configuration on every request and on the cron, answers 503 (no mail, no ack, a log line, no value echoed) when `ALERT_TO` is anything else, and the engine and the mail sender each refuse any other address again. No recipient is ever read from a push.

**No free text leaves the controller.** The Worker receives only: event ids, sequence numbers, the class, a pseudonymous bot label, a job id, and fixed codes. A failure kind is one of the controller's enum values (`auth`, `usage_limit`, `timeout`, `provider_error`, `tool_denied`, `interrupted`, `launch`, `gate`, `budget`, `needs_login`, `succeeded`, `cancelled`, `incomplete`, else `other`); a needs-you kind is a lowercase code such as `question_open`; the backup reason is `stage_<stage>` for a stage in a fixed list (`dump`, `archive`, `encrypt`, `upload`, `verify`, `prune`, `restore_test`) or `failed`. Error text, task titles, questions and the status file's `error` are never read into a push. A bot is always `bot-` plus the first six hex digits of the SHA-256 of its id: its name is never read into a push, however harmless it looks (a syntax check cannot tell `Alice` from a secret). Event ids are a hash of the controller's internal key (`e` plus 16 hex digits), so any character in an id (a `+00:00` timestamp, say) is safe. The Worker accepts only that bot label shape and replaces anything else with a placeholder.

**Delivery, controller to Worker (nothing is polled away).** Events are appended to the controller's database by the write that causes them, so a state that appears and clears between two pushes is still on record: SQL triggers on `jobs` (failed, needs sign-in, succeeded; a turn held by a budget or an automatic-turn cap), on new open questions, on assignments (waiting on you, blocked, stalled, dropped by its bot), approvals (pending, expired), tripwire log rows, usage-limit notices and watcher pauses. Only an overdue task, which no write announces, is logged by the detector (`record_needs_you`, every push cycle, once per item id). They are pushed in order, at most 100 per push, each with a sequence number, and removed from the queue only when the Worker's reply acknowledges a sequence number (a cursor kept in the database). The Worker holds at most 100 pending events per class; when it is full it acknowledges only what it stored, and the controller keeps the rest and sends it in later pushes. A push outage, however long, only delays: **an unacknowledged event is never deleted by age** (not while the Worker is unreachable, and not while `WATCHDOG_URL` is unset); only acknowledged events are pruned, after 14 days. If more than 1000 events are unacknowledged the queue is kept whole and reported: `notify_backlog` in `/healthz` (the count, always present) and one ERROR log line (`notify backlog over the limit`, and one when it drops back). A different controller database (different `notify-epoch`) restarts the cursor. Digests never drop an identity: a class with more than 40 entries is split into several mails ("part i of k", each with its exact count in the subject, 40 lines each, queued together and sent in order), so 100 failed jobs arrive as 40 + 40 + 20 and every job id is in some mail.

**Delivery, Worker to mailbox.** All Worker state lives in one Durable Object, so every operation is serialised on strongly consistent storage. A request (heartbeat, events, cron) only updates state and sets the Durable Object alarm; the alarm sends, so a slow mail server cannot delay heartbeat handling. When a class's gap has passed (10 minutes since its last mail; 60 minutes after 12 mails in a day) its pending entries become one outbox message (several parts if there are more than 40 entries), and each complete message (body, Date, Message-ID) is saved before the first send and re-sent byte for byte on every retry; a later part never overtakes a failing earlier one. A send is cut off after 8 s; a failed send is retried with exponential backoff (30 s, 1 min, 2 min, ... capped at 1 h). Rate limiting only merges: an outage that recovers before its mail is allowed is still mailed, marked "already recovered". **At-least-once boundary:** if a send times out, or the Worker stops, after the mail server accepted the message but before the outbox entry was deleted, the identical message is sent again. The mail system never tells us whether the first one arrived, so a rare duplicate (same Message-ID and Date, which many mail clients fold, but not guaranteed) is the price of never losing one. Exactly-once is not possible here.

Two channels, one bearer secret (a file on the VPS, mode 0600, and a Worker secret): the *heartbeat pusher* (`scripts/healthz_push.py`, a systemd timer every 5 min, system `python3` ≥ 3.9) posts `/healthz` to `/heartbeat`; the *controller* posts events to `/events` every minute once `WATCHDOG_URL` is set. Neither follows a redirect, so the secret goes only to the configured URL (HTTPS only).

**`GET /healthz`** (no auth, loopback like the rest of the API) returns only non-sensitive status: `ok`, `version`, `uptime_seconds`, `scheduler` (alive, last tick age), `db_reachable`, `running_jobs`, `paused` (the maintenance pause below, true only once the scheduler has acknowledged it; `pause_requested` is the file merely existing), `notify_backlog`, `backup` (`state`, `age_hours`, `bytes`, `stale`) and `disk_free_pct`. The database and disk work runs on one daemon thread (3 s bound, shared by concurrent calls), never on the event loop; past the bound the answer is HTTP 503 `{"ok": false, "timed_out": true}` with no `backup` object, which the watchdog treats as "backup not judged". The backup status file (`~/contabo-backup/status.json`, or `BOT_OFFICE_BACKUP_STATUS`) is read by its own daemon thread that callers wait on for at most 1 s: opened without blocking, a regular file only, 64 KiB cap. On a stuck mount the answer is the last good read (up to 10 minutes old) or `backup.state` `unjudged`; neither `/healthz` nor the event pusher nor interpreter shutdown ever waits on the file system. A wrong-typed field or a finish time more than 6 minutes in the future reads as `unknown` (and stale).

Limits: an overdue assignment is logged by the detector (once per assignment and due time, about a minute) or, if it closes first, by a trigger at the close; if the controller or VPS is down, the outage mail is the notice.

**Maintenance pause.** While the file `<data dir>/PAUSE` exists (`touch "$BOT_OFFICE_DATA_DIR/PAUSE"`), the scheduler claims nothing and fires nothing (no queued job starts, no routine, inbox message, wake-up or watcher); running turns finish and the hung-turn watchdog keeps working. New work from chat or the API is only queued and starts when the file is removed. It is checked on every pass (no restart needed), is also honoured at start-up (a restart with the file present comes up paused), and `/healthz` shows `"pause_requested": true` at once and `"paused": true` only after the run loop has itself seen the file on a pass (a claim already under way has finished by then; the loop must have passed within the last 5 s). `running_jobs` is read after that acknowledgement, so `paused` true with `running_jobs` 0 means nothing is running and nothing will start. This is what the runbook uses to drain; the live master predates it (see step 10), so the very first deploy cannot rely on it.

### Setup (the live `bot-office` unit is never overwritten)

Nothing here replaces `bot-office.service` or its drop-ins, and the controller needs no unit change: `WATCHDOG_URL` is the only controller setting, and it goes into a file the unit already reads or into a new drop-in file you add. The supplied `deploy/bot-office.service` is a reference for fresh installs and is not needed (do not install it over a live one). The two new heartbeat units (service and timer) are separate files.

`$D` is the directory the controller runs from (the `WorkingDirectory` of the live unit). Node 20+ and a Cloudflare account with a domain that uses Email Routing are needed.

**On a machine with a checkout and Node (not the VPS):**

1. `cd ops/watchdog && npm test` (48 tests; nothing to install).
2. `cp wrangler.toml.template wrangler.toml`; the recipient is already set to `you@example.test` (as `destination_address` and `ALERT_TO`): leave both as they are, any other value makes the Worker refuse to run. Fill in only `ALERT_FROM` (`watchdog@<your domain>`). `wrangler.toml` is local; do not commit it.
3. In the Cloudflare dashboard (Email Routing): enable it, add you@example.test under **Destination addresses**, and click the verification link Cloudflare mails to it. Until it shows **Verified**, nothing can be delivered.
4. `npx wrangler login`, then `npx wrangler deploy` (the first deploy creates the Worker and its Durable Object). Note the `https://<name>.<account>.workers.dev` URL.
   The Worker can also be deployed through the Cloudflare API instead of wrangler (used for the 0.12.13 deploy record below): upload the script with its bindings (Durable Object `WATCHDOG` / class `Watchdog` with migration `v1`, `send_email` binding `ALERT_EMAIL`, vars `ALERT_TO` and `ALERT_FROM`, secret `HEARTBEAT_SECRET`), a schedules PUT for `*/5 * * * *`, and enable the workers.dev subdomain for the Worker. The API token and the secret never go on a command line or into a file in the repo: pass them from a file you keep outside the repo (for example `curl -H @file` or `--data @file`, mode 600).

**On the VPS:**

5. Preflight and backups (read-only except the copies; nothing is changed yet). The VPS has no `sqlite3` CLI: every database access below is `python3` with its `sqlite3` module. First find the LIVE data directory from the running controller itself (its own environment), never from your shell, and check it holds the database the controller has open:
   ```sh
   PID=$(systemctl --user show bot-office -p MainPID --value); [ "${PID:-0}" -gt 0 ] || { echo "controller not running"; false; }
   BOT_OFFICE_DATA_DIR=$(tr '\0' '\n' < /proc/$PID/environ | sed -n 's/^BOT_OFFICE_DATA_DIR=//p'); export BOT_OFFICE_DATA_DIR
   DB="$BOT_OFFICE_DATA_DIR/agentdesk.sqlite3"
   [ -n "$BOT_OFFICE_DATA_DIR" ] && [ -f "$DB" ] && ls -l /proc/$PID/fd | grep -q "$DB" && echo "data dir ok: $BOT_OFFICE_DATA_DIR" || echo "STOP: not the live data dir (variable unset in the unit, or the DB is not open by the controller)"
   # read-only query helper and the two counts used below
   sq() { python3 -c 'import sqlite3,sys,pathlib; c=sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True); [print(*r) for r in c.execute(sys.argv[2])]' "$DB" "$1"; }
   running() { sq "SELECT COUNT(*) FROM jobs WHERE status='running'"; }
   queued()  { sq "SELECT COUNT(*) FROM jobs WHERE status='queued'"; }
   ```
   This one-shot open-file check can fail falsely: the controller opens SQLite per operation, so the file may simply be closed at that instant. Sample it several times (for example 10 tries, 1 s apart), or rely on the controller's own `Environment=` line in the unit and its process environment (`/proc/$PID/environ`, as above) to confirm the directory.
   If the check says STOP, do not continue: find the variable in the unit or its `EnvironmentFile` (`systemctl --user cat bot-office`) and fix `DB` by hand until the check passes.
   ```sh
   STAMP=$(date +%Y%m%d-%H%M%S); B=~/backups/pre-0.12.13-$STAMP; mkdir -p "$B"
   systemctl --user show bot-office -p FragmentPath -p DropInPaths -p EnvironmentFiles -p WorkingDirectory
   systemctl --user cat bot-office > "$B/bot-office.unit.txt"          # unit plus drop-ins, as systemd sees them
   cp -a ~/.config/systemd/user/bot-office.service* "$B/" 2>/dev/null   # the unit file and any .d directory
   cd "$D" && git tag "backup/pre-0.12.13-$STAMP"
   python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); print("integrity:", d.execute("PRAGMA integrity_check").fetchone()[0]); d.close(); s.close()' "$DB" "$B/agentdesk.sqlite3"   # online backup, safe while running
   ```
   Keep `$STAMP`, `$B`, `$D`, `$DB` and `$BOT_OFFICE_DATA_DIR` in this shell for the rest (re-derive them the same way in a new shell).
6. Create the secret and the heartbeat settings (mode 0600 for the secret; the pusher and the controller refuse anything else):
   ```sh
   umask 077; mkdir -p ~/.config/bot-office
   [ -e ~/.config/bot-office/watchdog-secret ] || head -c 32 /dev/urandom | base64 > ~/.config/bot-office/watchdog-secret
   printf 'WATCHDOG_URL=%s\nBOT_OFFICE_DIR=%s\n' 'https://<worker-host>/heartbeat' "$D" > ~/.config/bot-office/heartbeat.env
   ```
   The controller reads the secret from `~/.config/bot-office/watchdog-secret` by default (set `WATCHDOG_SECRET_FILE` only if you keep it elsewhere).
7. Give the controller `WATCHDOG_URL` without touching its unit. Look at `EnvironmentFiles=` from step 5.
   - If the live unit already has an `EnvironmentFile=`, append one line to that file: `echo 'WATCHDOG_URL=https://<worker-host>/heartbeat' >> <that file>`.
   - Otherwise add a NEW drop-in (a separate file; it adds to the unit and replaces nothing). Do not continue if the file exists:
     ```sh
     D_IN=~/.config/systemd/user/bot-office.service.d; mkdir -p "$D_IN"
     [ -e "$D_IN/watchdog.conf" ] && echo "exists: stop and review" || \
       printf '[Service]\nEnvironment=WATCHDOG_URL=%s\n' 'https://<worker-host>/heartbeat' > "$D_IN/watchdog.conf"
     systemctl --user daemon-reload
     ```
8. Check out the exact reviewed commit (a bare `git pull` can leave the checkout on master or on another revision) and install the two NEW heartbeat units only (they do not exist yet; the check stops if one does). The checkout is detached on purpose; `$B`'s tag is the way back:
   ```sh
   REV=<full 40-character hash of the reviewed commit>   # from the review; never a branch name or "master"
   cd "$D" && git fetch origin && git checkout --detach "$REV" \
     && [ "$(git rev-parse HEAD)" = "$REV" ] && [ -z "$(git status --porcelain)" ] && echo "at $REV, clean" || echo "STOP: not the reviewed commit"
   .venv/bin/pip install -e .
   ls ~/.config/systemd/user/bot-office-heartbeat.* 2>/dev/null && echo 'exists: stop and review' || \
   install -m 644 -D -t ~/.config/systemd/user/ deploy/bot-office-heartbeat.service deploy/bot-office-heartbeat.timer
   systemctl --user daemon-reload
   ```
9. From the dev machine, set the Worker secret from the VPS file without displaying it:
   `ssh <vps> 'cat ~/.config/bot-office/watchdog-secret' | npx wrangler secret put HEARTBEAT_SECRET`
10. Quiesce, then restart the controller. A restart kills provider processes the controller started, so nothing may be running, and nothing may start between the check and the restart. (`running`, `queued` and `sq` are from step 5.)

    **First deployment only: the live master has no pause and no `/healthz`, so it ignores `PAUSE`. Order the steps so nothing can start after the last check and the old process is stopped, not restarted.** Choose a quiet window (no chat open, no routine, inbox, wake-up or watcher due in the next few minutes: check the schedule), then:
    ```sh
    BOT_OFFICE_DATA_DIR="$BOT_OFFICE_DATA_DIR" "$D/scripts/first_deploy_pause.sh"
    sleep 5; curl -s http://127.0.0.1:8787/healthz   # expect "ok": true, the new version, "paused": true
    ```
    The script (python3 sqlite3, data dir pinned, `set -e`) creates `PAUSE` (ignored by the old code; the NEW process reads it before its first claim, so it starts paused), waits for no running or queued job, checks again 5 s later, and **exits non-zero without stopping or starting anything** if either check finds a job (or the database cannot be read). It runs `systemctl --user start` only after `stop` succeeded. After an abort, `PAUSE` stays in place: wait for a quieter moment and run it again (or `rm` the file to abandon).

    `stop` (not `restart`) lets the old controller's existing graceful shutdown run, and the new process cannot claim anything because it starts paused. The old process can still claim a job in the sub-second window between the last check and the stop. If the existing stop path meets a job mid-turn it can kill that turn (it does not wait for it), which is why the check is made twice, 5 s apart (a non-empty second check aborts): a claim made in the first window shows up in the second. A job claimed in the remaining sliver is recovered by restart recovery at the next start: replayed when its provider process is proven gone, otherwise failed with kind `interrupted` (a failed-turn mail and a Needs-you item). Then lift the pause: `rm "$BOT_OFFICE_DATA_DIR/PAUSE"` and confirm `"paused": false`.

    **Every later restart (outage test, rollback of the Worker side, secret rotation) runs on code that has the acknowledged pause: pause first, wait for the acknowledgement and an empty count, then restart.**
    ```sh
    touch "$BOT_OFFICE_DATA_DIR/PAUSE"
    until curl -s http://127.0.0.1:8787/healthz | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["paused"] is True and d["running_jobs"] == 0 else 1)'; do sleep 2; done
    systemctl --user restart bot-office      # the pause file survives and holds the new process paused
    sleep 5; curl -s http://127.0.0.1:8787/healthz && rm "$BOT_OFFICE_DATA_DIR/PAUSE"
    ```
    The restart creates the `notify_events` table and its triggers (idempotent), logs once the needs-you states already open at that moment (a one-time backfill per type, markers `notify-backfill-<type>`: questions, assignments, approvals, failed jobs, expired approvals, tripwire alerts, holds, watchers, usage notices, bot drops; the first mail may therefore be a digest of what is open now) and starts the event push.
11. Start the timer, push once, and let it survive logout:
    ```sh
    systemctl --user enable --now bot-office-heartbeat.timer
    systemctl --user start bot-office-heartbeat.service && systemctl --user status bot-office-heartbeat.service --no-pager
    loginctl enable-linger "$USER"
    ```
    The heartbeat unit runs `/usr/bin/python3 $BOT_OFFICE_DIR/scripts/healthz_push.py` (stdlib only; any system Python 3.9+); change `ExecStart` if your `python3` lives elsewhere.
12. Sync to GitHub (every deploy ends here; see [Keeping GitHub in step](#keeping-github-in-step)): `"$D/scripts/sync_github.sh" <deployed-branch>`. It is idempotent and never touches the service.

### Keeping GitHub in step

`origin` is the public repo, so nothing is pushed unscanned. Two mechanisms, both using `scripts/sync_github.sh`:

- **After every deploy** (runbook step 12): `scripts/sync_github.sh <deployed-branch>` pushes `master` and the deployed branch. For each branch it runs `gitleaks` over the commits not yet on origin (config `.gitleaks.toml`; the six reviewed test-fixture commits are allowlisted by hash) and does a plain `git push` only if clean. A finding blocks that branch (exit 2, logged, nothing pushed). A diverged remote fails the push (exit 1); it is never forced. Nothing to push is a no-op.
- **Safety net:** cron every 30 minutes runs `scripts/sync_github.sh --quiet` (master only), so a missed deploy step is caught within half an hour. Entry: `*/30 * * * * /home/ubuntu/printpilot-agentdesk/scripts/sync_github.sh --quiet`.

Log: `~/.local/state/bot-office/github-sync.log`. Needs `gitleaks` (`~/.local/bin/gitleaks`, official release) and the `gh` or git credentials already used for `origin`. History is never rewritten by the script; a blocked branch needs a human decision.

### Verify (about 20 minutes)

- **Destination.** The dashboard shows the address as **Verified** (step 3). The first mail below must arrive; check spam once and mark it not spam.
- **Needs you / failed.** Create a needs-you item (a question card, or let a routine fail): within about 2 minutes one mail arrives, naming the bot label and a code only. Items arriving within 10 minutes of it come as one merged digest.
- **Outage.** `systemctl --user stop bot-office-heartbeat.timer`, wait about 20 minutes: one ALERT mail; start the timer: one RECOVERED mail in the next digest. For the controller outage, pause and drain first (`touch "$BOT_OFFICE_DATA_DIR/PAUSE"`, wait until `/healthz` shows `"paused": true, "running_jobs": 0`, the acknowledged pause), then `systemctl --user stop bot-office` (heartbeats keep coming): the "controller does not answer" alert and no backup alert; `systemctl --user start bot-office`, check `/healthz`, `rm "$BOT_OFFICE_DATA_DIR/PAUSE"`. Never stop the controller with a turn running.
- **Backup.** Point a throwaway run's `BOT_OFFICE_BACKUP_STATUS` at a copy of `status.json` with `"state": "failed"` (not the real file): one backup mail with a reason code.
- **Delivery after an outage.** Stop reaching the Worker (for example `WATCHDOG_URL` pointing to a dead host in a throwaway run), create two items, restore: both arrive; `sq "SELECT COUNT(*) FROM notify_events WHERE seq > COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key='notify-cursor'),0)"` (the unacknowledged count) falls to 0.
- **Toggles.** Switch a class off in Settings, repeat its trigger: no mail.

### Rollback (restores everything that was changed)

Use the same shell as the setup (`$D`, `$B`, `$STAMP`, `$DB`, `$BOT_OFFICE_DATA_DIR`, `sq`; re-derive the data directory as in step 5).

1. Pause and drain the controller (the new code is still running, so the acknowledged pause works): `touch "$BOT_OFFICE_DATA_DIR/PAUSE"`, wait until `/healthz` shows `"paused": true, "running_jobs": 0`.
2. **Stop the controller's notify pusher first.** Before touching `NOTIFY_OFF`, make sure no old `notify-freeze` marker remains (`sq "SELECT value FROM meta WHERE key='notify-freeze'"` must print nothing). A stale one from an earlier freeze would be mistaken for the new one, and `notify_drained.py` would be trusted too early. If one exists, while `NOTIFY_OFF` is absent clear it with python3 sqlite3 (`python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("DELETE FROM meta WHERE key=\"notify-freeze\""); c.commit()' "$DB"`) and confirm it stays gone for 90 s (a notifier pass can still rewrite it). Only then `touch "$BOT_OFFICE_DATA_DIR/NOTIFY_OFF"` (the running controller sees it within a minute; no restart). From then on it detects nothing new and pushes nothing past the highest event number logged when it first saw the flag (`sq "SELECT value FROM meta WHERE key='notify-freeze'"`). Wait for a NEW marker before trusting `notify_drained.py`; do not trust any count until the notifier has seen the flag: step 3 waits for the `notify-freeze` marker (the notifier's interval is 60 s, so up to ~90 s) and treats a missing marker as NOT drained. (Without the flag, the alternative is to unset `WATCHDOG_URL` as in step 5 and restart the paused-and-drained way.)
3. **Then drain the mail before the Worker is deleted.** Deleting the Worker deletes its Durable Object, and anything it already acknowledged but has not mailed (pending, or retrying) cannot be replayed from the controller. Wait until the controller has nothing unacknowledged up to the freeze and the Worker is empty (the Worker mails at least 10 minutes apart per class, so this can take 10 minutes or, after a flapping day, an hour):
   ```sh
   WURL=<https://worker-host>          # the Worker origin, without /heartbeat
   wstatus() { curl -s -X POST -H @<(printf 'Authorization: Bearer %s\n' "$(cat ~/.config/bot-office/watchdog-secret)") "$WURL/status"; }
   # 1) the notifier must have seen NOTIFY_OFF (marker notify-freeze exists); up to ~90 s, a missing marker is NOT drained
   for i in $(seq 45); do [ -n "$(sq "SELECT value FROM meta WHERE key='notify-freeze'")" ] && break; sleep 2; done
   [ -n "$(sq "SELECT value FROM meta WHERE key='notify-freeze'")" ] || { echo "NOT drained: notifier never saw NOTIFY_OFF"; false; }
   sleep 30                             # a push in flight when it froze finishes
   # 2) only now are the counts meaningful
   until "$D/scripts/notify_drained.py" "$DB" >/dev/null && [ "$(wstatus)" = '{"pending":0,"outbox":0}' ]; do sleep 30; done; echo drained
   ```
   (`/status` needs the secret and returns two counts only; the secret goes through a file descriptor, not the command line.) Events created after the pusher stopped (a newly overdue task, a failed turn) stay in the controller database, unsent: nothing is lost, they simply are not mailed, and no Worker holds anything. If the counts never reach 0 (for example the mail is being rejected), do not delete yet: fix the mail path, or decide consciously that those messages are expendable and say so in the rollback note.
4. Stop the sources: `systemctl --user disable --now bot-office-heartbeat.timer`, then in `ops/watchdog` `npx wrangler delete` (removes the Worker, its Durable Object and state).
5. Undo the controller setting: delete the new drop-in (`rm ~/.config/systemd/user/bot-office.service.d/watchdog.conf`, and remove the directory if it is empty and you created it) or remove the `WATCHDOG_URL=` line you appended to the existing environment file. The live unit file and its other drop-ins were never modified; `diff -r` the copies in `$B` against `~/.config/systemd/user/` to confirm. Remove the heartbeat units and `~/.config/bot-office/heartbeat.env`, then `systemctl --user daemon-reload`.
6. Controller code (still paused and drained; re-check `running` is 0): `systemctl --user stop bot-office`, `cd "$D" && git checkout backup/pre-0.12.13-$STAMP && .venv/bin/pip install -e .`, `systemctl --user start bot-office`, check it answers, and `rm "$BOT_OFFICE_DATA_DIR/PAUSE" "$BOT_OFFICE_DATA_DIR/NOTIFY_OFF"` (the old code ignores the files; they are removed so a later upgrade does not start paused or silent).
7. Database: nothing has to be restored. The new table and triggers are ignored by older versions (the triggers keep adding small rows). To remove them, with the controller stopped or idle:
   ```sh
   python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [c.execute("DROP TRIGGER IF EXISTS "+t) for t in sys.argv[2:]]; c.execute("DROP TABLE IF EXISTS notify_events"); c.execute("DELETE FROM meta WHERE key LIKE \"notify-backfill%\" OR key IN (\"notify-email\",\"notify-cursor\",\"notify-epoch\",\"notify-freeze\")"); c.commit()' "$DB" notify_job_failed notify_job_done notify_question_open notify_asg_new notify_asg_state notify_asg_dropped notify_asg_overdue_close notify_approval_new notify_approval_expired notify_tripwire notify_usage_parked notify_watcher_paused notify_held_new notify_held_kind
   ```
   The pre-deploy copy `$B/agentdesk.sqlite3` is only for a real corruption.
8. Rotate the secret if it may have been exposed: write a new file, `wrangler secret put` it (if the Worker still exists), restart the controller the paused-and-drained way.

Deployed 2026-11-21 (ET, about 11:44, no job running or queued at the restart). Server master `00b4c08` (previous `e1e46fa`), version 0.12.13. Backups: tag `pre-healthz-20261121-174324` and DB copy `~/backups/agentdesk-pre-healthz-20261121-174324.sqlite3` (python3 sqlite3 backup API, integrity ok). Guarded cutover: one server script checked that master had not moved and nothing was busy, fast-forwarded master, import-checked the new code, then ran `first_deploy_pause.sh` (succeeded first try). `/healthz` straight after: HTTP 200, `ok` true, version 0.12.13, scheduler alive, `db_reachable` true, `running_jobs` 0; the 14 `notify_*` triggers and all 10 backfill markers were present. After lifting `PAUSE` a Test Bot job ("reply with exactly the word PONG") was claimed at once and answered `PONG`; its `done` trigger logged one event. Settings unchanged (the Test Bot settings row is byte-identical afterwards). The README's one-shot open-file check failed falsely on this controller (see step 5); a sampled check was used.

The watchdog Worker was deployed through the Cloudflare API (not wrangler) as `bot-office-watchdog` on `bot-office-watchdog.<your-subdomain>.workers.dev`, `ALERT_FROM` `watchdog@example.test`, recipient `you@example.test`. The email destination is verified and the test email was delivered on 2026-11-21 at 11:57 ET (a Test Bot "done" event; the done class was switched back off afterwards), so the heartbeat, event push and mail paths are tested. The missed-heartbeat outage drill is still to do.

## What changed in 0.12.11

A scripts link only runs inside the Bot Office viewer.

- **Rule.** `GET .../view?...&scripts=1` serves the HTML only when the request carries `Sec-Fetch-Dest: iframe` (or `frame`). Any other destination, including `document` (the link opened in its own tab or typed into the address bar), gets `403 text/plain` with a one-line explanation, `nosniff`, `no-store` and `Content-Security-Policy: default-src 'none'; sandbox`. No file content and no script is sent. The signature is checked first, so a bad or expired link still gets the same 403 as before.
- **Missing header: fail closed.** A request with no `Sec-Fetch-Dest` (an old browser, curl, a script, a proxy that strips it) is refused like a document load. Reason: the header is the only signal that the browser is framing the link, and a browser that does not send it cannot be told apart from a top-level navigation, which is exactly the case this blocks. Cost: a browser too old to send it (before roughly Chrome 80, Firefox 90, Safari 16.4) cannot run scripts in the viewer; it still gets plain views, which are unchanged. Browsers set the header themselves and page script cannot change it, so this stops a user or a page from opening the link directly; a non-browser client holding a valid two-minute link can still forge it. That is acceptable: the link already authorises reading that one file, and the sandbox and CSP are unchanged. This is a guard against navigation, not authentication.
- **Framing.** Scripts responses also carry `X-Frame-Options: SAMEORIGIN` next to `frame-ancestors 'self'` (already sent since 0.12.10), so only the app can frame them.
- **Unchanged.** Plain (scripts off) links, whatever the header says, and the bearer download route behave exactly as before. The denylist, off-by-default, per-open opt-in, `sandbox="allow-scripts"` only and never `allow-same-origin` are untouched.
- This closes the 0.12.10 residual (a scripts link in its own tab could navigate away). The self-navigation limit inside the viewer in browsers other than Chromium is unchanged.

Deployed 2026-11-21, no job running or queued at the restart (rechecked in the same command). Backups: tag `backup/pre-0.12.11-20261121-162813` and `~/backups/agentdesk.sqlite3.pre-0.12.11-20261121-162813`. Live check on a throwaway bot (deleted afterwards, integrity ok, no orphan rows) with a self-made HTML file, in headless Chromium against the real page: switch off, the script did not run; switch on, the script ran inside the frame (`sandbox="allow-scripts"`); the same scripts link opened as its own tab got `403 text/plain` and no script ran; a client with no `Sec-Fetch-Dest` header got the same 403. Review: a second-CLI review (Codex gpt-6.1-terra, high, read-only; the diff and source were passed inline because its file sandbox does not start on this host) found no bypass; one P3 (tests set the header by hand, no browser test) covered by the live browser check above, and a `Vary: Sec-Fetch-Dest` header was added on scripts responses as hardening. Rollback: check out tag `backup/pre-0.12.11-20261121-162813` and restart; no schema change, so the DB needs no restore.

## What changed in 0.12.12

- **Built-in Hidden section.** The sidebar always has a **Hidden** group, drawn after Ungrouped (always last, collapsed by default). It is a real `bot_sections` row with the fixed id `hidden`, created idempotently at every start (so a restart or a rollback-and-forward cannot lose or duplicate it). It cannot be renamed (409), deleted (409) or reordered, its name is reserved like "Ungrouped" (any case, width variant or invisible character is refused; a user section that already had the name is renamed "Hidden (old)" once), and it has no `⋯` menu and no drag handle. The collapsed flag is the section's own `collapsed` column, so it is saved server-side and shared across devices.
- **Hiding is sidebar placement only.** Membership is `bot_section_members` (one row per bot, so a bot is never in two places, and a repeat is a no-op). Nothing in the scheduler, routines, watchers, messages or bot-to-bot reads these tables (a test hides a bot and claims its job). A hidden bot is still in `GET /v1/bots`, still found by the sidebar search (matches show while a search is typed, and the group opens for them) and by expanding Hidden. The unread count shows on a collapsed Hidden header. Deleting a hidden bot removes its membership.
- **Ways to hide:** drag a bot onto the Hidden header or into its body; the bot `⋯` menu **Hide** / **Unhide** (Unhide puts the bot at the end of Ungrouped; it does not remember its old section); the **Hide in the sidebar** checkbox in the bot settings; the API: `PUT /v1/bots/{id}/section {"section_id":"hidden","index":0}` (unhide: `"section_id":null`), or in one call `PUT /v1/sections/hidden/members {"bot_ids":[...]}`, which makes exactly those bots the Hidden set (hidden bots not listed go back to the top of Ungrouped; an unknown id is a 404 and changes nothing). `GET /v1/sections` reports it under its own `hidden` key (`{id, name, builtin, collapsed, bot_ids}`), not in `sections`.
- **Characters replace patterns.** A one-time migration (`meta` key `avatars.characters_default`) gave every bot still on a generated pattern the character its id hashes to (stable: SHA-256 of the id, shape and color from separate bytes). Uploads, emoji-only bots and bots that already had a character are untouched. The old seeds are stored as JSON in `meta` key `avatars.pattern_backup`, so it is reversible: `UPDATE bots SET avatar_seed=json_extract((SELECT value FROM meta WHERE key='avatars.pattern_backup'), '$.' || id) WHERE json_extract(...) IS NOT NULL`, after deleting the `avatars.characters_default` marker only if you want it to run again. Older code (0.12.1 and before) reads `char|...` as "no usable image" and shows the emoji. The Pattern tab is gone from the picker; `POST /v1/bots/{id}/icon/generate` and the server-side generator still work (a bot given a pattern through the API still shows it). New bots start with the character their id hashes to.
- **More shapes.** Four new ones, our own simple SVG: squircle, ghost, shield, triangle (12 shapes x 10 colors). The gear/burst keeps exactly 6 lobes. The picker labels the Shape and Color rows, and the Character tab is the default for a new bot.
- **Rollback and limits.** No new table or column. Older code (0.12.10 and before) lists Hidden as an ordinary last section called "Hidden" (hidden bots stay in it); do not delete or rename it there (deleting moves its bots to Ungrouped and the membership is lost; the DB copy from before the deploy restores it). On the next start of 0.12.11 the row is recreated and its name put back. An office with no bots and no sections shows the "No bots yet" message instead of the Hidden header. The New bot dialog pre-selects a random character (saved as the choice); a bot created through the API without one gets the server default, which is now a pure function of its id (the same hash as the migration; before 0.12.12 it avoided characters other bots already had, so two bots can now share a look).
- **Tests.** `tests/test_groups_avatars.py` (Hidden: exists/collapsed, reserved names, rename/delete refused, hide/unhide, restart survival, seeding call, deleted bot, startup repair and idempotence, a hidden bot's job still claimed; migration and derived character), `tests/test_groups_ui.py` (Hidden group logic, menu and toggle), `tests/test_characters.py` (12 shapes), `scripts/e2e_gui.py` (Hidden: menu, search, expand, drag, settings toggle; character picker).

Deployed 2026-11-21, no job running or queued at the restart (rechecked in the same command). Code SHA `414fb71`. Backups: tag `backup/pre-0.12.12-20261121-162358` and `~/backups/agentdesk.sqlite3.pre-0.12.12-20261121-162358`. Live check on a throwaway bot (deleted afterwards, 19 checks): a new bot got a character; Hide and Unhide by API and UI; PATCH and DELETE on `/v1/sections/hidden` gave 409; search found a hidden bot; the collapsed flag and membership survived a restart; a real turn showed the working and done faces. Review: Codex gpt-6.1-terra, high, 3 rounds (the diff was passed inline because its file sandbox does not start on this host); every finding was fixed or documented, and the round 3 fix was not re-reviewed. Rollback: check out the tag and restart; no schema change.

## What changed in 0.12.10

A **Run scripts** switch in the HTML viewer, for interactive reports.

- **Off by default, and asked again at every open.** The switch is in the viewer's header next to Download. It starts unchecked each time a file is opened and is never stored (not in the browser, not on the server), so it is not remembered across opens, sessions or files. Closing the viewer, or switching it off, removes the running frame at once; a view link still being fetched when you close is dropped.
- **What "on" changes.** The page asks for a new link (`POST .../view-link?scripts=true`) and frames it with `sandbox="allow-scripts"`, nothing else: never `allow-same-origin`, top navigation, popups, modals or forms. The page therefore has an opaque origin: no access to Bot Office, its cookies or storage, or the master token (the link carries only a signature, no token). The signature now covers the flag, so a plain link cannot be turned into a scripts link by editing the URL. The response carries `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'self'`, so a script cannot fetch anything, including the app API with credentials. (0.12.11: the link is also refused outside the viewer's frame, below.)
- **The UI says so.** With the switch on, the frame gets a yellow outline and the note reads "Scripts are running in this page...". Off, the old note stays.
- **Unchanged.** The file denylist, the bearer route (HTML and SVG still download as `application/octet-stream`), and the default viewer (no scripts).
- **Known limit.** Script-initiated navigation of the frame itself is not a CSP-controllable "connect", so it relies on the app page's `frame-src 'self'` (checked in Chromium: the request was not sent). A scripts link opened directly as its own tab has no such page around it, so a script there could navigate away with what it can see (the file's own content). Only open a scripts view of a file you trust.

Deployed 2026-11-21, no job running or queued at the restart (rechecked right before it). Backups: tag `backup/pre-0.12.10-20261121-141303` and `~/backups/agentdesk.sqlite3.pre-0.12.10-20261121-141303`. Live check on a throwaway bot (deleted afterwards, integrity ok, no orphan rows) with a self-made HTML file, in headless Chromium against the real page: switch off, the script did not run; switch on, it ran, and `document.cookie`, `localStorage`, `sessionStorage`, `parent.document`, `top.location` and a credentialed `fetch` to the app API all failed (SecurityError / blocked), `window.open` returned null, and a fetch and a self-navigation to a listening local server never arrived. Review: a second-CLI review (Codex gpt-6.1-terra, high, read-only, on a copy) found one P1 (a script navigating its own frame to leak data; CSP has no usable control, but the page's `frame-src 'self'` blocked it in Chromium, documented as a known limit) and two P2 (a link still in flight could start scripts after close; the old frame kept running while a new link loaded), the P2s fixed. Rollback: check out tag `backup/pre-0.12.10-20261121-141303` and restart; no schema change, so the DB needs no restore.

## What changed in 0.12.9

Engine reliability. Step 1 was a read-only diagnosis of the live database (14 days of Antigravity, Cursor and OpenCode jobs, their events and chat lines); the numbers below are from that run.

**What the audit numbers really were**

- **Antigravity "created-to-started p90 188.9 s" was a misattributed metric.** `jobs.started_at` is overwritten every time a retry or restart re-run claims the job, so `created_at → started_at` also counted the retry backoff (2 min, 10 min, 3 days for a usage limit). The first start of all 31 Antigravity jobs came 0.0–0.1 s after creation. New: `jobs.first_started_at` (also in the job JSON) is set once at the first claim and backfilled from the recorded attempts; use it for created-to-started.
- **Cursor "p90 55.1 s" was real, but it is gone.** Every Cursor job that waited more than 5 s (6 of 88) was a chat message queued behind the same bot's running turn on 14–16 Nov, before 0.11.7 added steering into a running turn. Since 17 Nov no Cursor chat turn has waited more than 0.1 s. No desktop-slot wait and no deferral were found for any of these engines (lanes, not slots, limit concurrency).
- **The cancels were mostly not product bugs.** Antigravity 7 of 31: 5 came from test bots ("ZZ stress …", deleted bots) and 4 of those were the `--model gemini-3.8-flash-high/-low … conflicts with --effort` error that 0.11.15 already fixed (no occurrence since); 1 was a 503 "No capacity" from Google's side that ran out its two retries; 2 were timeouts or Stop from the user. Cursor 10: 6 on 14–15 Nov were a new message superseding the running turn (pre-steering behaviour), 4 were jobs parked for three days by a "Weekly usage limit" and then cancelled by the user or by the test bot's deletion.
- **CLI start-up is the real remaining delay, and it is the external CLI.** Spawn to the first output event: Antigravity p50 3.5 s / p90 16.4 s, Cursor p50 10.4 s / p90 20.5 s, OpenCode p50 8.7 s / p90 11.8 s (non-test bots). That is the CLI starting and the model's first token; Bot Office only launches the official CLI, so nothing is pre-warmed or worked around.

**What changed**

- `jobs.first_started_at` as above (additive column, backfilled on first start-up).
- Every cancel now records who stopped it instead of a bare "cancelled": "cancelled by you" (Stop), "cancelled to edit your message", "bot deleted", "its task was dropped", "watchdog: …" (unchanged). This is the text in the job's `error` and in its `done` event, so a cancel can be attributed from the database.

Deployed 2026-11-21 (CET), no job running or queued at the restart. Live check on a throwaway bot, cheapest models, before then after (created to `started_at` / `first_started_at`, spawn to first output): Cursor `auto` 0.02 s / n/a, 10.0 s then 0.02 s / 0.02 s, 9.5 s; Antigravity `gemini-3.8-flash-low` 0.03 s / n/a, 9.8 s then 0.03 s / 0.03 s, 6.4 s (spawn-to-output is the CLI and varies run to run). The backfill gave all 529 started jobs a `first_started_at`; the Nov 18 retried Antigravity job now reads 0.04 s instead of 600 s. Stopping a running Cursor turn through the API stored "cancelled by you". Rollback: the added column is ignored by 0.12.8, so check out tag `backup/pre-0.12.9-20261121-075404` and restart; the DB copy taken at deploy is `~/backups/agentdesk.sqlite3.pre-0.12.9-20261121-075404`.

## What changed in 0.12.8

- **`GET /v1/providers` is cached.** It used to probe every engine's CLI on every call (median 1.9 s, up to 5.7 s on the live box), and Settings waits for it before it shows. The answer is now kept for 60 seconds and served stale-while-revalidate: an old answer is returned at once and one background probe renews it. Concurrent calls share a single probe (single-flight). The cache is warmed in the background at startup, so the first Settings open is fast too. The response shape is unchanged.
- **Fresh when it matters.** `GET /v1/providers?refresh=1` probes now and waits for a probe that started after the request, or in the last second so a double click shares one (it never returns an older probe, and an invalidation during a probe makes its waiters look again). A job that ends "needs sign-in" invalidates the cache, so the next call looks again. Settings has a **Check sign-in again** button that uses `?refresh=1`. A background renewal that raises keeps the last answer; a probe a caller waits for (refresh, or after an invalidation) that raises gives a 503 rather than a possibly wrong "signed in". An answer containing an `error` engine is trusted for only 10 seconds so a transient CLI failure clears fast. `/v1/model-choices` still probes live.
- Code: `provider_status.py` (`ProviderStatusCache`), tests in `tests/test_providers_cache.py`.
- **Tests.** `tests/test_providers_cache.py` (18 tests: stale-while-revalidate, single flight, refresh and invalidation races, a failed probe gives 503). Full suite on the worktree: 1430 passed, 6 failed, 5 skipped. Five are the known failures (two isolation tests, the question-card blocking answer, the usage-budget digest, and the file-upload prune test, which also fails 2 runs in 6 on the previous master). The sixth, `test_usage_notice`, counted the new startup probe as an extra login check; the test now waits for the probe first.
- **Review.** A second-CLI review (Codex gpt-6.1-terra, high) found three issues, all accepted: waiters did not probe again after an invalidation, a failed awaited probe could return the stale answer instead of 503, and a burst of refreshes did not share one probe.

Deployed 2026-11-20 (ET), no job running or queued at the restart. Live `GET /v1/providers`, five read-only samples: before 1.718, 1.790, 1.909, 3.433, 4.113 s (median 1.909 s); after 0.003, 0.003, 0.005, 0.008, 0.056 s (median 0.005 s). Rollback: no schema change, code only. Check out tag `backup/pre-0.12.8-20261121-051328` and restart; the DB copy taken at deploy is `~/backups/agentdesk.sqlite3.pre-0.12.8-20261121-051328`.

## What changed in 0.12.7

Follow-up to 0.12.5 (search) and 0.12.4 (files), with the owner's answers.

- **Search is by relevance.** Results come best match first (FTS5 `bm25`), newest first on a tie, instead of newest first. **Load more** pages with `offset` (a message arriving meanwhile can shift a page by one; the page drops a repeat). The Ctrl/Cmd+Shift+F shortcut is unchanged. Without FTS5 the `LIKE` fallback puts a message holding the whole query first, then the shorter (denser) one, then the newer.
- **More is searched.** The names of attached files (`Q3_budget.pdf` is found by `budget`; images have no name) and Bot-Office's own status lines (wake-ups, automatic continuations: the `hidden` rows). A status-line hit is tagged "Bot Office" and has `hidden: true`; the chat does not show those rows, so a click lands on the message just before it. Archived (rewound) messages are still not searched.
- **Index.** `messages_search(text, names)` replaces `messages_fts`: an ordinary FTS5 table (its own copy of text and attachment names) kept in step by three triggers (`names` comes from the `attachments` JSON with `json_each`; malformed JSON gives an empty name and never fails a write). The first 0.12.7 start creates it, backfills it and drops the 0.12.5 index in one `BEGIN IMMEDIATE` transaction (marker `messages-search-v2`). Measured on a copy of the live database made with the sqlite3 backup API (14.3 MB, 1,121 messages): first start 0.07 s, second 0.004 s, all 1,121 indexed, integrity ok, +0.7 MB. Rollback: 0.12.5/0.12.6 recreate their own index at their next start and the 0.12.7 triggers keep this one current meanwhile, so no DB restore is needed (the DB copy is kept anyway); the next 0.12.7 start removes the old one again.
- **HTML and SVG open in a sandboxed viewer.** A file chip for `.html`, `.htm` or `.svg` gets a **View** button. The page asks for a two-minute signed link (`POST /v1/bots/{id}/attachments/{id}/view-link`, bearer auth, HMAC of bot, file and expiry under the master token, never the token itself) and frames `GET .../view?exp=&sig=` in an `<iframe sandbox="">`: no `allow-same-origin`, no scripts, no forms, no popups, no top navigation. The response itself also carries `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'` plus `nosniff` and `no-store`, so opening the link directly is sandboxed too, and nothing is fetched from the web. The page CSP gained `frame-src 'self'`. The viewer dialog has a **Download** button; the normal `GET .../attachments/{id}` still serves HTML and SVG as an `application/octet-stream` download. Scripts do not run unless you switch on **Run scripts** for that view (0.12.10, below); otherwise download the file to open it normally.
- **Limits unchanged.** 6 attachments per reply and 50 MiB per file (`BOT_OFFICE_MAX_UPLOAD_MB`); a test pins them.
- **Attachments from outside the job workspace.** A file named in `bot-office-attach` / `desk attach` may now lie anywhere the service can read, except a hard denylist: everything under the Bot-Office data dir and the private controller dir (token, database, bot memory and session files, other bots' workspaces and uploads), `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, the Claude, Codex, Cursor, Gemini and OpenCode config and session directories, rclone, gcloud and gh configs, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `/workspace/secure`, `/proc`, `/sys`, `/dev`, `/etc/shadow`, `/etc/ssh`, `/root`, and by name `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx`, `id_rsa`/`id_ed25519` and similar, `credentials(.json)`, `auth.json`, `rclone.conf`, `token`. The path is checked as written (`..` resolved), after every symlink is followed (realpath), and again on the opened descriptor; the final open refuses a symlink, and a file with more than one hard link is refused (it could be a protected file under another name). The name rules also apply to files in the workspace. A refused file adds the note "N files were not attached: a protected location." Inside the workspace and the bot's own `attachments/` and `uploads/` folders the 0.12.4 rules are unchanged (no symlink escapes there, no memory or session files). The bots' workspace note says the same.
- **Review.** A second-CLI review (Codex gpt-6.1-terra, high, read-only; its sandbox does not start on the VPS, so it ran on a copy on the dev box) found two P1 and one P2, all accepted: `.env` and key names were not checked for files inside the workspace; a hard link to a protected file elsewhere passed; and a 0.12.5 database opened on a SQLite without FTS5 kept its old triggers, which would fail chat writes.
- **Tests.** `tests/test_history_search.py` (bm25 order and tiebreak, attachment names, status lines, LIKE order, backfill from a 0.12.5 database with files and status lines, rollback re-migration, no-FTS5 fallback), `tests/test_outgoing_files.py` (denylist places, symlink, `../`, `~`, hard link, private dir, `.env` in the workspace, outside files allowed, limits, signed viewer headers, link binding and expiry, page CSP).

Deployed 2026-11-21 01:34 UTC (code at 56a0eb9; no jobs running or queued at the restart, rechecked right before it). Backups: tag `backup/pre-0.12.7-20261121-013400` and `~/backups/agentdesk.sqlite3.pre-0.12.7-20261121-013400`. Migration on the live database: 1,131 of 1,131 messages indexed, old index gone, integrity ok. Live check on a throwaway bot (deleted afterwards, no orphan rows): a real Codex turn created an HTML page, an SVG and a file in `/tmp` and attached all three (the `/tmp` file from outside the workspace); a `~/.ssh` path was refused with the protected-location note; the viewer link returned 200 with `text/html` / `image/svg+xml`, a CSP starting `sandbox;` without `allow-same-origin` or `allow-scripts`, and `nosniff`; the bearer route still served both as downloads; a bad signature gave 403; search found both files by name. No real chat was opened in the page (counts and the read-only API only). Rollback: `git reset --hard backup/pre-0.12.7-20261121-013400` on the server and `systemctl --user restart bot-office`; the DB needs no restore (0.12.6 rebuilds its own index), copy kept. Full pytest on the server: 1410 passed, 8 failed (the two `test_isolation` sudo tests, the blocking-answer card test and the usage-digest test fail on an untouched master too; the version, rename, hygiene and grok-bridge failures of that run were fixed or passed on rerun before deploy).

## What changed in 0.12.6

**A restart in the middle of a Codex turn no longer looks like a failure.** Found by a live drill on the production server (steering on all five engines, then `systemctl --user restart bot-office` mid-turn). Claude turns and a queued-but-not-started job survived a restart as designed (re-queued once, same session, one restart notice, queue drained, no orphan process). A Codex turn did not: systemd signals the whole service group at once, Codex exits first, and the runner saw an ordinary provider error, so it recorded the failure, scheduled a delayed retry and showed the wrong notice instead of leaving the job for restart recovery.

- **Fix.** The controller now learns of SIGTERM/SIGINT itself (`Scheduler.begin_shutdown()`, chained in front of uvicorn's handler). From then on a turn that did not succeed is an interruption: the row stays `running`, so the next start re-queues it once with the usual "restarted during this turn" notice. A result that arrives a moment before the signal handler runs is covered by a 0.5 s grace window for signal-killed CLIs. Timeouts and new job claims also respect shutdown. A turn that finished successfully during shutdown keeps its result; a CLI that fails with no shutdown is still a provider error. Tests: `tests/test_shutdown_race.py`.
- **Review.** A second-CLI review (Codex gpt-6.1-terra, high, read-only) found five issues, all accepted: the late-signal race, the rewind seed being cleared before the shutdown check, timeouts bypassing the check, a new claim after the signal, and the SIG_DFL case in the handler chain.
\n\nDeployed 2026-11-21 00:13 UTC (code at 12848ce; no jobs running or queued at the restart). Backups: tag `backup/pre-0.12.6-20261121-001314` and `~/backups/agentdesk.sqlite3.pre-0.12.6-20261121-001314`. Live re-drill on a throwaway bot (deleted afterwards): a Codex turn restarted after its third command was re-queued once (attempts 0, restart_attempts 1), showed the single restart notice, finished in the same session flow with no failure record and no orphan process; before the fix the same drill recorded a provider error and a delayed retry. Full test suite: 1398 passed, 4 failed; the same 4 fail on 0.12.5 (`test_isolation` x2, `test_question_cards`, `test_usage_budgets` digest). Rollback: check out the backup tag and restart (no schema change).

## What changed in 0.12.5

**Long chats open fast, and every message is searchable.** Until now a chat loaded its whole history (a 529-message chat was 465 KB raw, 79 KB gzipped) and there was no way to find an old message. Design note: `DESIGN-history-search.md`.

- **Paged history.** The chat opens with its newest 50 messages. Scrolling to the top (or the **Load older messages** button) loads 50 more and keeps your place: the message you were reading stays exactly where it was. A message that arrives while you read older ones appears once at the bottom; nothing is duplicated or skipped (pages are keyed by message id, never by offset). The live bubble, the instant echo of what you send, gzip, file chips and image attachments work as before. `GET /v1/bots/{id}/messages` is unchanged for older clients; the page uses the new `GET /v1/bots/{id}/history` (`limit`, `before`, `after`, `around`; see the API section).
- **Search.** The **Search** button in the chat header or **Ctrl/Cmd+Shift+F** opens a panel with **This bot** / **All bots**. Results are newest first with the matching words highlighted in a snippet; **Load more** pages further back. Words are all required (any order), the last word matches as a prefix while you type, accents and case are ignored. Click a result to open that message in its chat (the bot is selected if needed) with the page around it, the message flashed, and a **Jump to latest** bar. While you look at an older part, the chat is not marked read and nothing is read aloud; sending a message returns to the newest messages. Ctrl/Cmd-K and `/` still focus the bot filter, Ctrl-F is still the browser's own find.
- **What is searched.** Chat messages that are shown in the chat: not archived (rewound) messages and not Bot-Office's own machinery lines (automatic continuations, wake-up prompts). (0.12.7 also searches attachment names and Bot-Office's own status lines, and orders by relevance; see below.) Not searched: job event text.
- **Index.** `GET /v1/search/messages` uses an SQLite FTS5 index (`messages_fts`, an external-content table over `messages`, kept in step by three triggers). It is created and filled once, in one transaction, the first time 0.12.5 starts on a database; if FTS5 is not compiled into SQLite, search falls back to `LIKE` (same results shape, slower on huge chats). The migration is additive (one virtual table and three triggers, a `meta` marker), so 0.12.4 keeps working on the same file.
- **Tests.** `tests/test_history_search.py` (paging without duplicates or gaps while messages arrive, around/after/before, hidden and archived rows, placeholders, search per bot and across bots, snippets, hostile queries, updates and deletes, LIKE fallback, backfill and idempotence); `scripts/e2e_gui.py` covers scrolling back with the scroll position kept, a message arriving while paged back, search, jump, Jump to latest and the shortcuts.

Deployed 2026-11-20 20:44 UTC (code at ed387f8; no jobs running or queued at the restart). Backups: tag `backup/pre-0.12.5-20261120-204456` and `~/backups/agentdesk.sqlite3.pre-0.12.5-20261120-204456`. The migration was timed on a copy of the live DB first: 0.097 s for 1,089 messages (index built in the same start), integrity ok; on the live start all 1,091 messages were indexed. The biggest chat (529 messages) now opens with 36,612 B instead of 464,767 B (gzip 4,903 B instead of 78,740 B; median 11 ms instead of 37 ms). Live check on a throwaway bot ("ZZ history-test (safe to delete)", deleted afterwards): paging walk, around, per-bot and all-bots search, a hostile query, a real turn, and the browser (scroll back, search, jump, Jump to latest, no JavaScript errors); paging of the biggest real chat covered the same 519 messages as the old route, each once. Rollback: check out the backup tag and restart; 0.12.4 ignores the index table, and its triggers keep it in step.

## What changed in 0.12.4

**Bots can send you files of any type.** Until now a bot could only show images in its reply. Now `desk attach <file>` / `bot-office-attach <file>` (the same command as before, run from the job workspace) queues a PDF, CSV, zip, document, code file or anything else, and it appears under the reply as a download chip with an icon, the file name and its size. Images attached the same way still show as pictures. It works on all five engines and in chat, routine, wake-up, assignment and bot-to-bot replies, because every reply goes through the same collection step. The bots' workspace note tells them how.

- **Where files may come from.** The job workspace only (a bot copies a file in first). Symlinks anywhere in the path are refused, only regular files are read, empty files are refused, and the bot's own `memory.md`, `AGENTS.md` and session files are not attachable (images can still be re-sent from the bot's `attachments/` folder, and any file from its `uploads/` folder).
- **Limits.** 50 MiB per file (`BOT_OFFICE_MAX_UPLOAD_MB`, the same as uploads), 6 attachments per reply (images and files together), the existing per-bot and global storage quotas. Skipped files add a short note, "N files could not be attached." Names are reduced to plain characters; the stored copy is mode 0400 and never executable or extracted. An image over 10 MiB travels as a file.
- **Markdown stays image-only.** `![](path)` in a reply still attaches images only; a non-image file needs `bot-office-attach`. The sidecar keeps its old name, `.bot-office-outgoing-images`, so turns already running are unaffected.
- **Serving by an allow-list.** The server picks the type from the stored extension, never from the bytes. **PDF** (`application/pdf`) and **plain text** (txt, md, csv, tsv, json, log, yaml, toml, ini, py, js, ts, sh, sql and similar; `text/plain; charset=utf-8`) can be previewed inline. **HTML, SVG, XML, archives, Office files and everything else are sent as `application/octet-stream` with `Content-Disposition: attachment`** and never rendered. All responses keep `nosniff`, `no-store` and a sandboxing CSP. `?download=1` forces a download for any file. `GET .../attachments/{id}/info` now also returns `preview` (`pdf`, `text` or null).
- **Chat.** Each file chip shows an icon by extension, the name and the size; clicking it downloads. A PDF or text file also gets a **Preview** button: text opens in a dialog as text (first 256 KiB), a PDF opens in the browser's PDF viewer inside the dialog (only if it starts with `%PDF-`). HTML and SVG get no preview button. The same chips now serve files you upload.
- Design: `DESIGN-files.md`. Tests: `tests/test_outgoing_files.py` (routing, path and symlink safety, caps, names, serving, end to end) and the changed upload test.

Deployed 2026-11-20 17:48 UTC (code at 15795a0; no jobs running or queued at the restart). Live check on a throwaway bot ("ZZ files-test (safe to delete)", deleted afterwards): a real Claude turn and a real Codex turn each created a .pdf, a .csv, a .zip and a .html file and sent them with `bot-office-attach`; all four arrived as chips on both turns, the PDF and CSV were served inline (`application/pdf`, `text/plain`) and the zip and HTML as `application/octet-stream` attachments, and in the live page the HTML chip downloaded as page.html (title unchanged, no preview button) with no JavaScript errors. The other 27 real bots were untouched. Rollback: on the server `git reset --hard backup/pre-0.12.4-20261120-174812` and `systemctl --user restart bot-office`; DB copy `~/backups/agentdesk.sqlite3.pre-0.12.4-20261120-174812` (no schema change, so no DB restore is needed). Full pytest on the server: 1376 passed, 3 failed (the two `test_isolation` sudo tests and the usage-digest date test, which fail on an untouched master too); local browser e2e 157 passed.

## What changed in 0.12.3

One drawing fix. The flower/burst character (the `blob` shape) had eight prongs, which was too many; it is now redrawn with **exactly six round lobes**, one pointing up. It is our own outline. The shape id and the stored value (`char|blob|<color>`) are unchanged, so bots already using it simply get the new outline; nothing in the API, the database or the other seven shapes changed. The face placement was kept and checked: all six faces (idle, working, waiting, done, error, sleeping) in all 10 colors, at 36 px and at the sidebar size, stay inside the body with a margin (checked by a geometry script at build time, not part of the test suite; the test suite counts the lobes and bounds the outline's radius).

Deployed 2026-11-20 16:50 UTC (code at 6ca773b; no jobs running or queued at the restart). Live check on a throwaway bot ("ZZ gear-test (safe to delete)", deleted afterwards): the live sidebar row drew the six-lobed shape with the idle face and all six faces were rendered on it, no JavaScript errors, all 27 real bots unchanged.

## What changed in 0.12.2

Character avatars. A bot can now have one of four avatar types, chosen in its settings dialog (the picker has Emoji, Character, Image and Pattern tabs): the emoji as before, a built-in **character**, an uploaded **image**, or one of the 0.12.0 generated **patterns** (kept as a minor option).

- **Characters** are simple colored shapes with two eye marks: 8 shapes (rounded square, circle, six-lobed flower, hexagon, cloud, pill, star, diamond) times 10 colors, picked by shape and color. They are our own drawings, rendered inline as SVG in the browser (no image request, no third-party artwork), stored in the bot's `avatar_seed` as `char|<shape>|<color>` (no schema change; `|` is not a legal generator seed, so a rollback to 0.12.1 shows the emoji).
- **The face follows what the bot is doing**, from the status the page already has: idle (upright eyes), working or queued (narrowed eyes that glance side to side), waiting for you (wide eyes and a small open mouth, a gentle bob; also used when blocked), done (happy face for 20 seconds after a bot finishes a run), error (x eyes) and sleeping (closed eyes, dimmed; the engine is offline). Motion only plays when your system does not ask for reduced motion, and every face looks different at rest. Characters show in the sidebar rows, the chat header and the settings dialog. Emoji, image and pattern avatars keep a small status dot instead.
- **New bots** start with a random character (change it any time). Existing bots keep what they have; nothing is switched automatically.
- **API.** `PUT /v1/bots/{id}/avatar/character {shape, color}` (names are validated), `GET /v1/avatar-characters` (the lists). `GET /v1/bots/{id}/icon` answers 404 for a character. The stored marker is not a legal pattern seed, so the generate and preview routes refuse it, and a rollback to 0.12.1 shows such a bot's emoji.
- Tests: `tests/test_characters.py` (API, validation, status mapping, drawing of all 480 shape/color/face combinations) and new checks in `scripts/e2e_gui.py`.

Deployed 2026-11-19 06:10 PM ET (code at 3c416e0; no jobs running or queued at the restart). Live check on a throwaway bot ("ZZ chars-test (safe to delete)", Claude, deleted afterwards): a new bot started with a character; the idle face, the working face during a real turn and the done face right after it were seen, then idle again after the 20 s window; the waiting, error and sleeping faces were checked with the status rewritten in the browser only (no real question or failure was staged); no JavaScript errors; the 27 real bots, their avatars and emoji were unchanged afterwards. Rollback: on the server `git reset --hard backup/pre-0.12.2-20261119-231046` and `systemctl --user restart bot-office`; DB copy `~/backups/agentdesk.sqlite3.pre-0.12.2-20261119-231046` (no schema change, so no DB restore is needed; a character bot shows its emoji on 0.12.1). Full pytest: 1361 passed, 1 failed (the usage-digest date test, which also fails on untouched master); local browser e2e 147 passed.

## What changed in 0.12.1

Small chat-speed and clarity fixes from an audit of the chat UI, engines and performance. Nothing about the engines, the queue or the data changed.

- **Your message appears at once.** Pressing Send draws your bubble immediately with a "Sending…" mark (and "Sending to <bot>…" when the bot is idle) instead of waiting for the server's answer and a chat refresh; the stored copy replaces it a moment later. The box clears at once. If the send fails, the bubble goes away and your draft is put back (unless you already typed something new). Measured in a real browser with a simulated 300 ms round trip (a scripted browser run against a throwaway local server): 376, 400 and 403 ms from Enter to the bubble before, 10, 7 and 7 ms after.
- **Plain errors that say what to do.** A dropped connection now reads "Can't reach Bot-Office. Check that the SSH tunnel or your connection is up, then try again. Anything you typed is still in the box." instead of "Failed to fetch"; `stream HTTP 502` becomes a note that the live view reconnects by itself; 413, 429 and 5xx answers get a sentence of their own. Server messages that were already clear are shown as they were.
- **Consistent status words.** The short placeholder shown before the turn's own bubble exists says "Starting up" (running) or "Waiting to start" (queued), the same words as the live bubble.
- **Smoother live reply.** The live bubble redraws at most once per animation frame (with a 250 ms timer for a hidden tab) instead of once per streamed text event.
- **Smaller downloads.** Responses over 1 kB are gzip-compressed (Starlette `GZipMiddleware`, level 5) for clients that ask: the page (217 kB) and long chats (a 511-message chat is 457 kB of JSON). The live event stream stays uncompressed, so streaming is unchanged.
- **One more index.** `jobs(bot_id, status, created_at)` (`jobs_bot`, created if missing at start-up; additive, old versions ignore it) for the per-bot job lookups. At 473 jobs the old plan already took 0.02 ms per query, so this is for growth.

Tests: `tests/test_chat_polish.py`. Still open from the audit, on purpose: history pagination and message search, phone access without an SSH tunnel, push notifications, proof of mid-turn steering on the real Codex/Cursor/Antigravity/OpenCode CLIs, `/v1/providers` taking about 2 s (Settings), and pruning old `thinking` events.

Deployed 2026-11-19 05:31 PM ET (master 0189ece; the deploy waited about nine minutes for a running chat turn and restarted only with nothing running or queued). It was prepared as 0.11.16 and became 0.12.1 when 0.12.0 landed first. Live check on a throwaway bot ("ZZ polish-test (safe to delete)", Claude, deleted afterwards) through the SSH tunnel (about 90 to 160 ms round trip): your bubble was on screen 17 ms and 27 ms after Enter in two runs (the stored copy and the live status arrived at about 195 ms, which is where the bubble used to appear), the reply arrived in 8.4 s, and a send blocked in the browser showed the new "Can't reach Bot-Office" text with the draft back in the box. Over the live API (loopback, median of 5): the page 251,787 B to 69,627 B gzipped (10.0 to 17.9 ms), `/v1/bots` 68,198 B to 19,055 B, a 511-message chat 458,968 B to 77,754 B. Rollback: on the server `git reset --hard backup/pre-0.12.1-20261119-233106` and `systemctl --user restart bot-office`; the DB copy is `~/backups/agentdesk.sqlite3.pre-0.12.1-20261119-233106` (the added index is harmless to older code, so the DB needs no restore). Full pytest on the server: 1332 passed, 6 failed, 9 skipped; the six also fail or flake on an untouched master copy (two `test_isolation` sudo tests, `test_public_hygiene` outside a git checkout, the usage-digest date test, and two timing-flaky tests that passed on rerun).

## What changed in 0.12.0

Bot groups (sidebar sections) and real bot avatars, and a denser sidebar. Design note: `design-groups-avatars.md` (initiative notes). No existing behaviour changes: with no sections the list looks as before; a bot keeps its emoji.

- **Sections.** New tables `bot_sections` (id, name, position, collapsed) and `bot_section_members` (bot, section, position); additive, created with `IF NOT EXISTS`. A bot is in one section or in the virtual, always-last **Ungrouped**. API (master token only): `GET/POST /v1/sections`, `PATCH/DELETE /v1/sections/{id}`, `PATCH /v1/sections/ungrouped {collapsed}`, `PUT /v1/sections/order {ids}` (must be a permutation; stale lists get 409) and `PUT /v1/bots/{id}/section {section_id|null, index}` (move or reorder; index is clamped). Every write is one `BEGIN IMMEDIATE` transaction that renumbers positions densely. Names are 1-60 characters, unique ignoring case, and `Ungrouped` is reserved. Deleting a section moves its bots to the top of Ungrouped (nothing else is deleted); deleting a bot removes its membership. The collapsed state is stored server-side, so it follows you between browsers.
- **Sidebar UI.** Section headers (chevron, name, count, `⋯` menu, an unread total while collapsed), `+ New section` with inline naming, inline rename, a delete confirmation dialog, and drag and drop for bots (onto a header or between rows) and for section headers. Everything a drag does has a keyboard or touch route: the `⋯` menu (or `M` on a focused bot) lists Move to each section, Move up and Move down; section menus have Rename, Move up, Move down and Delete; Alt+Up/Down reorders a focused bot or header. Moves are announced in a polite live region. Search shows matches inside collapsed sections without changing their state and turns dragging off while a query is typed. Narrow screens keep avatars and a chevron per section.
- **Avatars.** `GET /v1/bots/{id}/icon` serves the uploaded image, else the generated avatar of the bot's `avatar_seed`, else 404 (the emoji shows). It sends an `ETag` (`If-None-Match` gets 304) and `Cache-Control: private, no-cache`, or `private, max-age=86400` when the request carries `?v=`. Uploads (`PUT .../icon`, raw body) accept PNG, JPEG, WebP and a GIF's first frame, up to 2 MB (the web UI shrinks bigger photos first) and 12 megapixels (40 for a JPEG, which is decoded at reduced size); the file is sniffed, fully decoded, centre-cropped, resized to 256 px and stored as PNG without metadata. SVG and everything else is refused (415). `POST .../icon/generate {seed?}` gives the bot a generated avatar (replacing an upload), `DELETE .../icon` removes both (emoji only), `GET /v1/avatar-presets` lists the 12 default avatars and `GET /v1/avatar-preview?seed=` renders any seed. The generator needs no key, network or image library: a seed picks one of 12 hand-picked gradient palettes and one of 6 geometric motifs, drawn with antialiased shapes at 256 px and encoded with `zlib` (about 60 ms, cached). New bots get the least-used default avatar; a one-time migration (`meta` key `avatars.default_assigned`) gave every existing bot without an image its default avatar, leaving its emoji and everything else untouched.
- **Dependency.** Pillow (`Pillow>=10.3`) is now a dependency, used only to validate and resize uploads. Without it uploads return 503 with a clear message; generation, serving and the rest keep working. An AI-generated icon set through an official CLI is a possible later option and needs no change to this model.
- **Look.** Rows are 52 px (avatar 36 px with the status dot on its corner, name and one muted line; the task counters show on the active row and in the tooltip), header avatar 44 px, identity-dialog avatar 64 px, small-caps section headers, hover tint, an accent bar on the active row and a deeper sidebar surface. System font stack only; no copied assets.
- **Tests.** `tests/test_groups_avatars.py` (sections CRUD, order and membership invariants, migration idempotence, generator determinism and distinct presets, upload validation: valid formats, SVG, truncated, oversize, pixel bomb, metadata dropped; ETag and 304; auth; the missing-Pillow path), `tests/test_groups_ui.py` (page logic run with node) and `scripts/e2e_gui.py` (create, rename, collapse and reload, drag between sections, reorder bots and sections, keyboard moves, delete with confirm, avatar upload, preset, generate, emoji fallback and a failing image).
- **Deployed.** 2026-11-19 04:52 PM ET (master 3db84d3, one deploy, no jobs running or queued at the restart). Backups: tag `backup/pre-0.12.0-20261119-225225` and `~/backups/agentdesk.sqlite3.pre-0.12.0-20261119-225225` (mode 600, integrity ok). The one-time migration gave each of the 27 existing bots a default generated avatar (spread over the 12 presets) and left every emoji, name and setting as it was; no real bot was put in a section. Live-tested through the API with two throwaway bots and one throwaway section (all deleted): create, duplicate name 409, rename, collapse, move, insert, stale order 409, generated icon with ETag 304, JPEG upload normalised to 256 px, SVG refused, generate over an upload, remove (emoji fallback), delete section. The live web UI loaded and its read-only e2e passed (27 checks); the group-editing UI was exercised by the local browser e2e only (128 checks).
- **Rollback.** The migration only adds tables and one column (`bots.avatar_seed`), so older code ignores them: check out the pre-0.12.0 backup tag and restart. `pip install` is not needed to roll back.

## What changed in 0.11.15 (deployed 2026-11-19)

**Antigravity default model works.** A new Antigravity bot's default pair, `gemini-3.8-flash-high` with effort `medium`, failed every turn: the CLI refuses `--effort` next to a model whose id already names an effort ("model conflicts with --effort"). Now an Antigravity model id ending in `-low`, `-medium`, `-high`, `-xhigh` or `-max` runs with no `--effort` flag (`catalog.model_carries_effort`, applied when the job is queued and again when the command line is built, so older jobs and bots are fixed too). The settings dialog hides the Thinking effort control for such a model, and the header pill and job details show no effort for it; pick a model without an effort suffix to set one. The API still accepts an effort for these models and ignores it.

**`/compact` on Claude Code** shows one status line, for example `Conversation compacted (40k → 10.5k tokens, 4.7s)`, built from the CLI's `compact_boundary` event, instead of the raw result stream. The CLI's own messages (nothing to compact, not signed in) are shown as they are. The other engines' slash output was checked live and is already plain text; see the deploy record below.

**Upload read check.** `scripts/live_check.py [engine ...] [--compact]` runs against a live install: for each engine a throwaway bot ("ZZ live-check <engine> (safe to delete)", deleted afterwards) must run one turn on its default model and effort, then read an uploaded text file and quote its unique token; `--compact` also runs `/compact` on a Claude bot. It reads `data/token` itself and never prints it. `tests/test_file_uploads.py` covers the same path without a CLI, and `scripts/e2e_gui.py` has the browser steps (screenshots `upload-read` and `compact-status`).

Deployed 2026-11-19 01:42 PM ET (master 9c93b42, one deploy, no jobs running at the restart). Live-tested with throwaway bots on all five engines, each on its fresh default pair (turn answered PONG) and then reading an uploaded text file and quoting its unique token: Claude `claude-opus-5-5` / medium, Codex `gpt-6.1-terra` / high, Cursor `auto` / no effort, OpenCode `opencode/big-pickle` / no effort, Antigravity `gemini-3.8-flash-high` / no `--effort` flag. The first Antigravity run failed with a Google-side `503 No capacity available`, not the effort conflict; the same turn passed minutes later, and run by hand `agy --model gemini-3.8-flash-high --effort medium` still prints the old error while the same call without `--effort` succeeds. Claude `/compact` on a real session showed `Conversation compacted (47.1k → 10.4k tokens, 9.2s)`. The other engines' slash output was plain text already: Antigravity `/help`, `/skills`, `/usage`, `/changelog`, Claude `/context` and `/usage`, and an OpenCode command. Codex and Cursor commands are expanded into an ordinary prompt. Known leftovers: a job's recorded effort can still read `medium` on an Antigravity model that carries its own effort (never sent to the CLI and hidden in the page). Rollback: no schema change, code only. Check out tag `backup/pre-0.11.15-20261119-184238` and restart; the DB copy taken at deploy is `~/backups/agentdesk.sqlite3.pre-0.11.15-20261119-184238`.

## What changed in 0.11.14 (deployed 2026-11-19)

**Slash commands for every bot CLI.** Type `/` at the start of the chat box and the list of that bot's CLI commands opens with a short description each. It filters as you type; Up/Down move, Enter or Tab pick, Esc closes. Picking fills the box (add arguments if you like); the next Enter sends it. A line that starts with `/` but is not one of the bot's commands (for example a path) is sent as an ordinary message.

| Engine | Where the list comes from | What a pick does |
|---|---|---|
| Claude Code | the CLI's own list: the `init` event of a local `claude -p /context` run (builtins, skills, plugin and your custom commands) | `run`: the line `/name args` is the whole turn, so Claude Code expands it itself |
| Codex | **documented built-ins** (marked `documented`; Codex has no machine-readable list) plus `~/.codex/prompts/*.md` | custom prompts are expanded by Bot Office (`$ARGUMENTS`, `$1`..`$9`) and sent as the message; TUI-only built-ins are greyed |
| OpenCode | the CLI's own list: a short-lived `opencode serve` on loopback with a random password, `GET /command` | `command`: `opencode run --command name -- args` |
| Cursor Agent | **documented built-ins** (marked `documented`) plus `.cursor/commands` and `~/.cursor/commands` | like Codex: expanded by Bot Office |
| Antigravity | the CLI's own list: `agy -p /help` | `run`: `agy -p "/name args"` |

Each entry carries its `source` (`cli`, `custom`, `skill`, `plugin`, `documented`, `fallback`) in `GET /v1/bots/{id}/slash-commands`; `fallback` means the CLI could not be asked and a small built-in list is shown (retried after 60 s). The list is cached per engine, CLI version and custom-file set in memory and in `data/slash-cache.json` (mode 0600, at most 8 sets per engine); `?refresh=1` asks again.

**Controls of Bot Office.** `/model` and `/effort` open the Settings field (with an argument they set it, used from the next turn); `/new` and `/clear` start a fresh CLI session on the next turn after a confirm dialog (chat history, memory and WORKING.md are kept; the server also requires `confirm=true`, and refuses while the bot has a job). Bot Office has no manual compaction control, so `/compact` runs only where the CLI does it headless (Claude Code) and is greyed elsewhere. Commands that need a terminal (pickers, `/status`, `/exit`...) are shown greyed with the reason and are not run.

**Never surfaced or run:** anything that logs in or out or handles credentials (login, logout, auth, connect, setup-token, api-key...). They are hidden from the list and refused by the server (403). Official CLIs are used unmodified and no token is read or stored.

A slash command is its own turn: it is queued behind a running turn and never steered into one, and a message sent while one runs waits behind it. Command files are read without following symlinks. Server: `POST /v1/bots/{id}/slash` with `{name, args, confirm}`.

Deployed 2026-11-19 12:13 PM ET (first deploy 11:57 AM ET; the later ones fixed small things found in the live tests). Live-tested on all five engines with throwaway bots. Rollback: one additive column, `jobs.slash` (TEXT); an older version ignores it, so the rollback is code only. Check out tag `backup/pre-0.11.14-20261119-165741` and restart. The DB copy taken at deploy is `~/backups/agentdesk.sqlite3.pre-0.11.14-20261119-165741`.

## What changed in 0.11.13 (deployed 2026-11-19)

**Model search.** Every model choice is one searchable picker (New bot / Bot settings, including the OpenCode picker). Type to filter by model name, id or engine (case-insensitive); Up/Down, Home/End, Enter and Esc work, "No matches" shows when nothing fits, and the current selection stays listed while you filter. Dragging the list's scrollbar no longer closes it; only a real outside click, Esc or a selection does. Opening New bot before the list loaded no longer resets your pick to Codex. `gpt-6.2-sol` is in the fallback Codex list.

**Sidebar.** "Needs you" starts collapsed as one row with a count badge (hidden at 0); click it, or press Enter/Space, to open it, and the choice is remembered in the browser (`localStorage`, key `bot-office-needs-open`). "Turn on notifications" stays visible in both states. A search box above the bot list filters as you type on name, description, model and engine (every word must match, any case); `/` or Ctrl/Cmd+K focuses it, Esc clears and leaves it, Enter opens the first match, and "No bots match" shows when nothing fits. The search filters only the bot list, never Needs you: those requests wait on you whichever bot they belong to. The sidebar is resizable: drag the handle on its right edge (200 to 520 px, never wider than the window minus 320 px), use Left/Right (Shift for bigger steps, Home/End) on the focused handle, or double-click it to reset to 272 px; the width is remembered (`bot-office-sidebar-width`). The narrow-window layout is unchanged.

**Files of any type in chat.** Paste (Ctrl+V of a copied file) or drag and drop PDF, CSV, STL/STEP, G-code, zip or anything else, up to 6 per message. Images work as before (10 MiB, shown inline). Other files are saved in `data/bots/<bot id>/uploads/<date>-<random>-<safe name>` (read-only, mode 0400, no executable bit, never overwritten, never extracted), the bot's prompt names their absolute paths and says they are data to read, never to run, and the chat shows each as a chip with its name and size; clicking it downloads the file (it is only ever served as an attachment with `nosniff`, never opened in the page). The per-file limit is 50 MB, set with `BOT_OFFICE_MAX_UPLOAD_MB`; a larger file is refused with `413` and a message that names the limit. File names are reduced to letters, digits, `.`, `_` and `-` (no directories, no `..`). API: `POST /v1/bots/{id}/attachments?name=<file name>` (raw body; without `name` it still accepts images only), `GET /v1/bots/{id}/attachments/{id}/info` (name, size, kind). Files are stored per bot (up to 1000 files or 1 GiB); unsent uploads are pruned after a day, files in a sent message stay, and there is no global file cap (images have one). Uploaded files reach Claude through an extra `--add-dir`; Codex and OpenCode read the path from the prompt like they do for images. No database change.

Deployed 2026-11-19 9:38 AM ET (the ship was planned as 0.11.11; 0.11.12 was already live, so the version is 0.11.13). Rollback: code only; no migration. Check out the previous live tree (tag `backup/pre-0.11.13-20261119-143758`) and restart. Files already uploaded stay on disk in `uploads/`; an older version just ignores them (their chat chips would show as broken images). The DB copy taken at deploy is `~/backups/agentdesk.sqlite3.pre-0.11.13-20261119-143758`.

## What changed in 0.11.12

**Waiting for another bot is not a blocker.** A bot that messages another bot (`bot-msg`) and has nothing else to do now finishes its turn as `done` (on a task, `waiting_on_bot`) instead of `waiting_on_user`/incomplete, so it no longer counts as blocked or shows up in Needs you. The rules, the task brief and the `bot-msg` note say so, and Bot-Office also rewrites the turn when the bot still says `waiting_on_user — reply arrives asynchronously` (only when a message it sent is still unanswered, the status names the wait, and nothing asks the owner: a real question, a question card, an approval or a "please send ..." still waits on you). When the other bot's reply arrives, Bot-Office wakes the asking bot once with the reply (as untrusted data) through the same rule that decides every automatic turn (`continuation.decide`): auto-continue off, chat-only bots, a task paused by the guard, the consecutive limit, the wake-up limit and the per-bot and all-bots caps all still apply. Details: `docs/CONTINUATION.md`. The only data change is the table `agent_message_origin` (additive; 0.11.10 ignores it).

**Loop-guard fixes** (from the review of the task-loop guard): the guard's text checks ignore fenced and inline code, quoted lines and quoted strings, and do not read "Fixed the failed test" as unresolved, so productive turns keep going; a `bot-task status continue` report's note ("need a photo") now reaches the guard and pauses the task; and a guard pause stays paused (status-change, scheduled and reply wake-ups and a bot's out-of-turn `continue` do not resume it; this already held on 0.11.10 and is now tested).

**Launchers:** `desk`, `bot-office-ask`, `bot-office-attach` and `bot-office-say` re-exec the app's venv python when started by another interpreter (merged from `desk-venv-launcher`). The re-exec marker is the process's own pid, so an inherited `BOT_OFFICE_VENV_REEXEC` never blocks a re-exec, it is removed once inside the venv and when the exec fails. Tests run the four real launchers (inherited marker, nested and cross-launcher launches, failing exec).

Rollback: code only plus one additive table. Check out the previous live tree (the tag `backup/pre-0.11.12-<stamp>` is created at deploy) and restart; 0.11.10 ignores `agent_message_origin`, so no database restore is needed. Optional cleanup: `DROP TABLE agent_message_origin;`. The DB copy taken at deploy is `~/backups/agentdesk.sqlite3.pre-0.11.12-<stamp>`.

## What changed in 0.11.10

**Bots keep working until the task is done.** A chat, API, routine or wake-up turn that ends with `BOT-OFFICE-STATUS: not_done` is now continued automatically in the same session. The chat shows no extra prompts: the bot's bubble stays "Working · continuing, 3 of 10". It stops when the bot reports done, blocked or waiting_on_user, at the owner's limit (default 10 in a row, up to 50), when you press Stop, or when you have a message queued for it. A message you steer into a continuing turn starts a fresh count. It does not run while a question or approval is open, or for bot-to-bot replies or watcher runs. Assignment turns use their own loop, but the same rules apply to it (see "One continuation system" below). A continuation keeps the source of the turn it continues, so unattended turns stay under the unattended rules. Rule 7 of the prompt now says to keep working and to use not_done only for a real pause.

**Two pages every bot gets, both editable by the owner:**

- *Rules for all bots* (`src/botoffice/prompts/rules-for-all-bots.md`, about 420 words): finish what you start (keep going until the goal is met, you're stuck and need the user, or a rule says stop), work on assigned tasks rather than a lifelong purpose, decide reversible things, rarely ask (the owner's approval classes ask for them), verify, report honestly. Edit it first in Settings. API: `GET`/`PUT`/`DELETE /v1/rules-for-all-bots`.
- *System prompt* (`src/botoffice/prompts/system.md`, about 750 words): how Bot-Office works (turns and status, replies, memory files, other bots, delegation, follow-ups, trust). It is prebuilt and kept under Settings → Advanced. API: `/v1/system-prompt`, and `/v1/baseline` as before. A 0.11.9 baseline override carries over as the system prompt override.

Both are inlined in every fresh prompt (system prompt first) and written as `SYSTEM-PROMPT.md` and `RULES-FOR-ALL-BOTS.md` in the job directory (no more `BASELINE.md`). Resumed sessions get a one-line nudge when either changes. The owner's standing rules and the bot's `instructions.md` override both; the numbered rules always apply.

**The chat shows who wrote what.** Only messages you type use your bubble. Messages between bots (bot-msg, inbox drops, reply copies, one bot handing another an assignment), sent or received, show in the chat only as a time and a row like "2 messages with Research Bot", with that bot's icon; back-to-back messages with the same bot share one row. Click it to read that conversation in an overlay (both bots' icons and names, formatted text, Close Chat or Esc). A bot's own messages to other bots count toward Unread. Bot-Office's machinery (automatic continuations, assignment "Continue" and "Check" steps, wake-up prompts and "Not done (continuing)" lines) stays out of the chat and the Unread count; Show archived and the Details drawer still show it. Routine prompts, watcher runs and assignment starts appear as small notes, never as your messages. Messages carry `author` (owner, bot, bot_out, bot_in, bot_reply, office, system) and `peer` in the API.

**A real Settings page.** The sidebar's ⚙ Settings opens settings for all bots: Rules for all bots, Bot behavior (auto-continue, its limits and the automatic-turn caps; `GET`/`PUT /v1/settings`), Approvals, Engines (sign-in status), Voice, and Advanced (system prompt). Each bot's own settings stay in its chat.

**Rarely ask; approvals by class.** Bots no longer ask before every risky step. The owner picks *approval classes*, kinds of action they want done but not on the bot's own, in Settings → Approvals. On by default: sending email or messages, purchases and payments, merging / releasing / force-pushing or pushing to main, admin and system changes (sudo, systemctl, crontab...), and deleting outside the bot's workspace (including deleting through a connector). Off by default: pushing code and posting on GitHub, logging in to other computers, printers and the desktop, and any connector action. You can add your own class (a command pattern or a tool-name pattern), remove it, and set how long an approval waits (60 minutes by default; 0 waits until you answer). Classes are checked in your chat as well as in unattended runs; an owner `allow` action rule still wins for that action. When the CLI's gate hook recognises one, it stops that tool call and the bot's chat shows an Approve / Deny card with the whole action. Approve runs exactly that action once in a resume turn (the resume prompt stays out of the chat). Deny, or no answer in time, leaves the task stopped with a one-line note; an expired card can still be approved later. Rule 4 of the rules for all bots now says this: go ahead, Bot-Office asks for you, ask questions only when the goal is unclear or the choice is truly the user's. Classes, the git guard and the Sending check are defense in depth, not an inescapable boundary. Bots with full CLI access run as the same Unix user as the hooks and shims that do the checking (in the single-user layout, as the controller too), so a bot that sets out to get around them can, and some actions will not be stopped. They match tool names, command lines and inline code (a command inside a script file is not read), and for a bot that can't pause they are only advice. If the engine cannot reach Bot-Office, the tool call is refused. A job with no bot cannot wait for a card: a guarded action there is refused and shows in Needs-you, and to approve it you run the job again from a bot. What is and isn't covered is in [docs/APPROVALS.md](docs/APPROVALS.md#what-is-and-isnt-covered). Which engines can pause, and how, is in [docs/APPROVALS.md](docs/APPROVALS.md): Claude Code, Codex with full access, Cursor and Antigravity through their hooks; opencode and Codex with restricted access cannot be paused; such bots are named next to each class that is on and in a warning in Settings → Approvals, and their SELF.md tells them never to do those actions themselves but to ask in their reply. API: `approvals` in `GET`/`PUT /v1/settings`; messages carry an `approval` card.

**Bot messages steer too.** A message from one bot to another now reaches the other bot's running turn the way your own messages do, instead of always waiting for its next turn. The sender decides: `bot-msg <bot> "..."` steers by default; `bot-msg --queue <bot> "..."` (or `desk message --queue`, or `"mode": "queue"` on `POST /v1/bots/{sender}/message-agent`) waits for a turn of its own, for anything that can wait. A steered message is marked as from that bot (information, not the user's instructions), arrives after any message of yours waiting for the same turn, and is not answered by the turn's own reply, since that reply belongs to the receiver's task: the receiver answers with a message of its own, and the prompt tells it to. If the receiver is idle the message starts a turn as before; it is never steered into a turn that is itself answering a bot, or into an engine that can't take a message mid-run; and if the turn ends before reading it, it becomes an ordinary queued bot turn whose reply goes back to the sender. Guards: a bot can't message itself; one message per pair per 10 seconds; steered messages count toward the hourly inbox caps; and an exchange between two bots allows three asks with their answers (the answer to a steered ask takes the ask's hop, as a queued turn's reply always has). Each bot has a switch in its settings, "Other bots' messages reach it while it works" (`bot_steer`, on by default); off, bot messages to it queue. The API returns `delivery` (`steer`, `start` or `queue`) and `mode`; a steered message's status is `sent`, then `delivered`.

**Task lists.** Each bot has a task list (the sidebar label stays "Assignments"). Owner and bots can add, edit, reorder, start and drop tasks. Every write carries a version, and a stale write gets a 409 listing what changed. Each prompt shows the bot its open tasks and the changes others made since its last turn. Bots use `bot-task` (`add`, `edit`, `start`, `drop`, `status`, `changes`, ...). Further rules:

- Tasks have a priority (urgent, high, normal, low). Lists, prompts and the queue put higher priority first. Set it with `bot-task add/edit --priority` or the badge and selector in the panel.
- A task added from the form starts on its own; untick "Start now" to leave it open. Start appears only on open tasks that never started.
- A task one bot adds to another bot's list lands open and waits for that bot. It gets at most normal priority and no position of the sender's choosing, so one bot cannot reorder another's queue.
- On its own list, a bot may edit the title and goal of a task you created, never its done-when. Such edits are marked "Edited by a bot", and prompts show them as untrusted text until you rewrite them.
- Dropping a task (by you or a bot) cancels its queued and running turns, and they do not come back.
- A bot's task writes are capped per hour. A reorder counts as one write, however many tasks it moves.

Settings (environment, read on every call; unknown values fall back to the default):

- `BOT_OFFICE_CROSS_BOT_SUBTASK_START` = `wait` (default) or `allow`: whether a bot's subtask on another bot's list may start at once. A task without a parent always lands open.
- `BOT_OFFICE_BOT_DROP_OWNER_TASKS` = `with_note` (default) or `owner_only`: whether a bot may drop a task you created on its list, with a note shown in Needs you.
- `BOT_OFFICE_DROP_CANCELS_TURNS` = `on` (default) or `off`.
- `BOT_OFFICE_BOT_EDIT_OWNER_TASKS` = `title_goal` (default) or `off`.
- `BOT_OFFICE_FORM_TASKS_START` = `auto` (default) or `manual`.
- `BOT_OFFICE_TASK_DEFAULT_PRIORITY` = `normal` (default), `urgent`, `high` or `low`.
- `BOT_OFFICE_CROSS_BOT_MAX_PRIORITY` = `normal` (default), `urgent`, `high` or `low`: the highest priority a bot may give a task on another bot's list.
- `BOT_OFFICE_TASK_WRITES_PER_HOUR` = `200` (default).
- `BOT_OFFICE_TASKS_LABEL` = `Assignments` (default).

`GET /v1/tasks/settings` returns the current values.

Rollback: the database changes are additive (new columns, a side table for bot-message links, an index and a marker row), and 0.11.9 ignores them. No table that 0.11.9 fills with a positional `INSERT` gains a column, so its `bot-msg` and `desk message` keep working. Tasks created on 0.11.10 still run on 0.11.9, without priority or bot-edit marks. See the continuation rollback steps below before rolling back.

**One continuation system.** The chat auto-continue and the assignment run-until-done loop share one decision (`src/botoffice/continuation.py`; design in [docs/CONTINUATION.md](docs/CONTINUATION.md)). A bot is no longer continued automatically when any of these holds:

- it reported blocked or waiting_on_user, or its reply says it is blocked or needs you (for example "remains blocked", "please send a photo", "can't verify");
- a question or approval is open, or your message is waiting (an assignment's next step queues behind your message);
- 3 turns in a row made no progress;
- 10 automatic turns ran in a row since you last spoke, or the chat limit (10) was reached;
- the assignment's own turn or minute budget is used;
- an automatic-turn cap is reached.

Each stop leaves a chat line that says why. A stopped assignment shows in Needs you as "paused, not blocked: ...", and your reply resumes it. Each finished turn is decided once.

- **Caps.** Automatic turns are chat continuations, assignment steps and checks, and wake-ups. Your messages, Start, Reply and routines are not counted. A bot may take 20 automatic turns a day, and all bots together 20 an hour. A turn over a cap is not dropped: it waits until the window slides, and the bot's chat and Needs you name the cap and say you can raise it in Settings → Bot behavior. Saving a cap releases waiting turns at once. A turn that waited longer than `hold_hours` (12), or whose task, bot or setting changed meanwhile, is not run late: the chat says so and the assignment waits for you.
- **Done needs evidence.** An assignment counts as done only when the bot says how it verified the work: `bot-task done current --evidence "pytest: 14 passed"`. A done without evidence gets one more turn to say so. The second time, the assignment waits for you ("says it is done; no evidence"). Only evidence given since the assignment last started or resumed counts, and it must be at least 12 characters (a setting). The done line shows the evidence, and Needs you shows the last evidence given. Marking it done yourself needs no evidence. Peer review stays opt-in per task.
- **Chat-only bots.** A checkbox in the bot's dialog (`chat_only` on `POST`/`PATCH /v1/bots`). Such a bot is never continued automatically and gets no assignment steps, checks or wake-ups of its own. Your Start or Reply gives it one turn.

Settings (Settings → Bot behavior, or `PUT /v1/settings` with `{"auto_continue": {...}}`; a saved value beats the environment default):

| key | default | environment default |
|---|---|---|
| `enabled` | true | |
| `max` | 10 (chat chain, 1 to 50) | |
| `no_progress` | 3 | `BOT_OFFICE_AUTO_NO_PROGRESS` |
| `consecutive` | 10 | `BOT_OFFICE_AUTO_CONSECUTIVE` |
| `per_bot_day` | 20, or `"unlimited"` | `BOT_OFFICE_AUTO_TURNS_PER_BOT_DAY` |
| `per_hour` | 20 (all bots), or `"unlimited"` | `BOT_OFFICE_AUTO_TURNS_PER_HOUR` |
| `evidence` | true | `BOT_OFFICE_DONE_NEEDS_EVIDENCE` = on/off |
| `evidence_min` | 12 characters | `BOT_OFFICE_DONE_EVIDENCE_MIN_CHARS` |
| `hold_hours` | 12 | `BOT_OFFICE_AUTO_HOLD_HOURS` |

`max` cannot be saved above `consecutive`, since the chat stops at whichever comes first; Settings shows the limit that applies.

To raise a cap, edit it in Settings → Bot behavior, or for example `PUT /v1/settings {"auto_continue": {"per_bot_day": 40}}`.

Rollback: the migration is additive (`jobs.auto_kind`, `jobs.auto_decision`, `bots.chat_only`, plus new keys in the existing `auto_continue` setting), and older code ignores it. Before starting 0.11.9 on the migrated database, stop the service with no job running and run:

```bash
sqlite3 data/agentdesk.sqlite3 "UPDATE messages SET steer_status='delivered' WHERE source='inbox' AND steer_status='pending'; UPDATE jobs SET status='cancelled', error='rollback' WHERE status='queued' AND failure_kind='auto_cap';"
```

The first statement drains bot messages still waiting to steer a turn (0.11.9 would re-queue them as if you had typed them). The second cancels automatic turns held by a cap (0.11.9 would run them without the cap check). Hidden machinery rows and approval cards show as plain lines in 0.11.9.

Then mark the text a bot edited on a task you created:

```bash
sqlite3 data/agentdesk.sqlite3 "UPDATE assignments SET title='[bot-edited] '||title WHERE created_by='user' AND ','||bot_edited||',' LIKE '%,title,%' AND title NOT LIKE '[bot-edited] %'; UPDATE assignments SET goal='[bot-edited] '||goal WHERE created_by='user' AND ','||bot_edited||',' LIKE '%,goal,%' AND goal NOT LIKE '[bot-edited] %'; UPDATE assignments SET done_when='[bot-edited] '||done_when WHERE created_by='user' AND ','||bot_edited||',' LIKE '%,done_when,%' AND done_when NOT LIKE '[bot-edited] %';"
```

Why: 0.11.10 remembers which fields of your task a bot rewrote (`assignments.bot_edited`) and shows those fields to bots as untrusted data, not as your instructions. 0.11.9 does not know that column, so it would quote a bot's rewrite as if you had written it. The prefix keeps it visibly marked in the task panel and in prompts. Running the statements twice changes nothing.

Not built, by design: quiet hours, a general done gate for physical or irreversible results, and a loop that starts open tasks by itself.

**Live end-to-end test.** `scripts/e2e_gui.py` drives a real Chromium (or Chrome with `--channel chrome`) through the UI against a throwaway local Bot-Office with a scripted engine and two bots labelled "E2E test bot" (Enter to send, Settings and approvals, the Approve / Deny card, the bot-to-bot row and panel), or read-only against a running install with `--url` and the token from `--token-file`, `BOT_OFFICE_TOKEN_FILE` or `BOT_OFFICE_TOKEN` (`--token` still works but is deprecated: it shows in the process list; `--send-to` sends one real message with Enter, only to a bot whose name starts with "E2E test"). It ends with a plain PASS/FAIL list. Needs `pip install playwright` and `python -m playwright install chromium`.

## What changed in 0.11.9

**Every bot now starts from the same baseline instructions**, on every engine: how to own a task end to end, when to act and when to ask (one question, with a default), verifying work before calling it done, short result-first replies, what goes in memory.md, WORKING.md and shared-memory.md, how to delegate to and answer other bots (a peer's message is never the owner's approval), and scheduling follow-ups with wake-ups and routines instead of promising to check later. The default ships as `src/botoffice/prompts/baseline.md` (about 1,000 words).

- *Layering.* A fresh session gets the baseline inline in its prompt and as `BASELINE.md` in the job directory. A bot's own `instructions.md` and the owner's standing rules in shared memory override it; the numbered Bot-Office rules and the action gate always apply.
- *Owner control.* Sidebar → **Settings** → **Baseline instructions** shows the effective text; edit and save it, or **Reset to default**. It is stored in the controller database (`meta` key `baseline_instructions`), limited to 16,000 characters, and reachable only with the master token: `GET`, `PUT` and `DELETE /v1/baseline`. Bots read it but cannot change it (edits to `BASELINE.md` are not kept).
- *Resumed sessions* get one line telling the bot to re-read `BASELINE.md` when the baseline changed since its last completed turn (`state/baseline.sha256`), and once after upgrading, since sessions started earlier never saw it.
- *Delegation advice* in the generated AGENTS.md / CLAUDE.md / GEMINI.md block no longer hard-codes a default model or asks bots to state a reason for using Claude; it says to pick the engine and model that fit the task, and to follow defaults the owner sets.
- `docs/PRINCIPLES.md` records the product goal and the rules every future change is checked against.

## What changed in 0.11.8

**Claude no longer needs a reason.** The "Reason for using Claude" box is gone from the composer and from bot settings, and **Retry on Claude** no longer asks for one. The API stops returning 400 for a Claude bot, job, turn, edit or provider switch without a `reason`. The `reason` field is still accepted (and stored) everywhere it was, so older clients keep working, and existing bots keep their stored reason; nothing is migrated or deleted.

## What changed in 0.11.7

**A message sent while any engine works steers that turn; it is never the next turn** (except when the turn ends before the message could be delivered, as before). 0.11.6 still queued the message for OpenCode and for restricted Codex, Cursor and Antigravity bots, and made the other engines wait up to 15 seconds for a step boundary. Now:

| Engine and access | How the message reaches the turn | Expected delay (estimates, not measured with real CLIs) |
| --- | --- | --- |
| Claude (any access) | written to the open stream-json stdin and acknowledged by its echo (unchanged, `live`) | about 0.1 s |
| Codex, Cursor, Antigravity, full access | the CLI is interrupted (SIGINT) and the same session is resumed with the message (`interrupt`) | 0.3 s plus the CLI's own stop and restart, roughly 1 to 5 s in total |
| Codex, Cursor, Antigravity, restricted access | the CLI is interrupted and the same job is run again by a new invocation with the same restricted flags, its prompt, a note of what the first one did and the message (`restart`) | same |
| OpenCode (any access) | same restart; OpenCode cannot resume a session (`run -s` hangs on 1.19.5), so none is used | up to about 1.5 s plus start-up |

*Timing.* An idle moment (the model is thinking or writing) is interrupted 0.3 s after the message arrives, which also lets messages sent in quick succession travel together. A tool step that Codex, Cursor or Antigravity announce as running (a command, an MCP call, a file change) gets up to 5 s (`STEER_BOUNDARY_TIMEOUT`) to finish first, so a half-done command is not cut if it is nearly done. OpenCode announces only finished tool calls, so it waits up to 1.5 s (`STEER_UNKNOWN_WAIT`) for one and otherwise interrupts mid-step. If the CLI ignores the SIGINT it is stopped after 5 s (2 s for a restart). The constants are at the top of `providers/cli.py`.

*Why restricted access restarts instead of resuming.* `codex exec resume` takes no `--approve-for-me` or sandbox flag and Cursor needs the per-bot workspace to resume, so a resumed segment of a restricted bot would run under different limits from the first. The restart reuses exactly the flags of the interrupted invocation, so the limits cannot change mid-turn. A restart does not keep the model's own earlier reasoning: the new run gets the job's prompt, the interrupted run's narration and tool calls (up to 60 lines, newest kept, within the 128 KiB argument limit) and your message, and works in the same workspace, so files, git state and `reply.txt` carry over. A command that was running is cut and may need re-running; the note tells the bot to re-check the last step. Token usage is the sum of the invocations.

*Chat.* The hint under the composer says which of the four applies; a message not yet taken shows "Delivering in a moment" and then the usual "Delivered to <bot>". `GET /v1/bots` reports `steering` as `live`, `interrupt`, `restart` or `queue`; `queue` is now only for an engine whose adapter sets `restart_steering = False`, and no built-in engine does. The queue-with-a-reason path of 0.11.6 (`delivery: "queue"`, `delivery_note`, `steer_note`) is unchanged for that case, for `mode: "queue"` and for a bot answering another bot.

**Not verified here.** Only fake CLIs were run (tests/test_steer_all_engines.py and tests/test_steering.py): they prove the message is delivered into the same job and session or prompt, the flags are kept, the timing, batching, and several restarts in a row. Real OpenCode, Codex, Cursor and Antigravity were not run, so how each one reacts to a SIGINT in the middle of a step, whether Codex and Cursor resume cleanly after it, and the quality of an OpenCode restart from the note are unconfirmed. Codex's `app-server` `turn/steer` RPC and OpenCode's server mode were not used: neither is in the adapters or documented in this repository, and nothing here was invented for them. First real test after deploy: send a steering message to a working bot of each engine and check that the chat shows "Delivered" within a few seconds and that the job's log has a "resuming after steering" or "restarting after steering" line.

Roll back by deploying 0.11.6 (git tag and a database copy first, and no job running). 0.11.7 makes no data change (`steering` is computed, not stored), so there is nothing to restore; a 0.11.6 page talking to a 0.11.7 server would only show its generic hint for the new `restart` value.

## What changed in 0.11.6

**Steering reaches assignment turns, and the fallback is stated.** A message sent to a bot that is working on an assignment (or wake-up) turn used to be refused by the steering path (`post_steer` skipped every running job with an `assignment_id`) and became a separate queued chat turn with no explanation, which is what "it queues instead of steering" looked like. It now attaches to the running assignment turn like any other, is not charged to the assignment's budget, and is converted to a plain chat turn only if it was never delivered. Bot-to-bot inbox turns are still never steered into (the message would reach the other bot); the chat says so.

**Explicit delivery.** `POST /v1/bots/{id}/messages` returns `delivery` (`start`, `steer` or `queue`) and `delivery_note` (why it was queued, else null). A message to an engine that cannot take input mid-run (OpenCode, restricted-access Codex, Cursor and Antigravity) is queued in the request itself, with the reason, instead of showing as pending for a moment and being converted later. `messages.steer_note` stores the reason and the `steer` event carries it as `note`. `GET /v1/bots` reports `steering` per bot: `live` (Claude, written into the open stream), `interrupt` (Codex, Cursor and Antigravity stop at a step boundary, at most 15 seconds, and resume the session) or `queue`.

**Chat page.** Jobs are no longer cards in the chat. The bot's turn shows one live bubble: animated dots, a short status line that follows the stream ("Thinking", "Running npm test", "Reading src/app.py", "Writing the reply", "Got your message, adjusting"), the elapsed time, "N more queued" when others wait, and Stop. The composer stays usable while the bot works and sends a steer by default; under it a line says how the message will reach the bot for that engine. A steering message is an ordinary user message with a mark: "Delivered to <bot>" with the time, "Delivering", or "Queued, runs next" plus the reason. Tool steps, logs and the list of queued and recent jobs moved to a **Details** drawer (the Activity button in the header, or Details on the live bubble). The animation stops under `prefers-reduced-motion`. No new assets; the CSP hashes are recomputed from the page.

**Not verified here.** Real Claude, Codex, Cursor and Antigravity CLIs were not run: delivery and the interrupt and resume path are tested with fake CLIs only, and the page was checked with node against a stubbed DOM, not in a browser. Codex steering still waits for a step boundary or up to 15 seconds, so it is not instant.

Roll back by deploying 0.11.5 (take a git tag and a copy of the database first, as for any deploy, and deploy when no job is running). The only data change is the nullable `messages.steer_note` column, added on start; 0.11.5 ignores it, so no data rollback is needed.

## What changed in 0.11.5 (deployed)

The live draft no longer shows each Cursor narration segment twice when cursor-agent sends a timestamped snapshot after each segment; those snapshots are dropped. Known gaps: a snapshot of a one-delta segment, or a thinking event between the deltas and the snapshot, can still show a repeat in the draft. The final reply is unaffected. Roll back by deploying 0.11.4 (tag `backup/pre-cursor-snapshots-0.11.5-20261115`, DB copy `~/backups/agentdesk.sqlite3.pre-cursor-snapshots-0.11.5-20261115`); this release makes no data changes. Source details: `/workspace/runs/bug-repeat/RESULT.md`.

## What changed in 0.11.4 (deployed)

- **Cursor replies.** The reply (for example from grok models) is the last text segment, not every narration segment glued together with no space. An empty last segment gives an empty reply.
- **Live draft.** Segments are separated by a blank line, and the draft no longer repeats the last segment. OpenCode and Antigravity live drafts also separate segments.

Roll back by deploying 0.11.3 (tag `backup/pre-cursor-text-0.11.4-20261115`, DB copy `~/backups/agentdesk.sqlite3.pre-cursor-text-0.11.4-20261115`); this release makes no data changes.

## What changed in 0.11.3 (deployed)

Bots see their peers, schedule, open assignments and recent workspace context in SELF.md, and their open tasks in fresh and resumed prompts. The worklog keeps the last reply paragraph. Progress notes remain separate from replies. Run `python scripts/autonomy_metrics.py --db data/agentdesk.sqlite3 --days 7` for read-only baseline metrics; use `--since YYYY-MM-DD` or `--json` as needed. Roll back by deploying 0.11.2 (tag `backup/pre-autonomy-p0p1-0.11.3-20261115`, DB copy `~/backups/agentdesk.sqlite3.pre-autonomy-p0p1-0.11.3-20261115`); this release makes no data changes. This is phases P0 and P1 of the [bot autonomy plan](docs/plans/bot-autonomy-plan.md); the plan's open questions were still undecided in that release and nothing here depends on them.

## What changed in 0.11.2 (deployed)

Bots can post lasting progress notes during a turn with `desk say "<text>"` or `bot-office-say "<text>"` (`-` reads stdin). `POST /v1/agent/say` (job-scoped agent token, running job only) takes `{"text": "..."}`: 1-500 characters, one note per 10 seconds and 10 per job (429 otherwise). A note is a separate chat row (`kind: "say"`, shown as a muted "progress" line); it never ends the turn or replaces the final reply and status line, is not read aloud, and is left out of the history given to models. It is archived with its turn by an edit. Adds the additive column `messages.kind` (default `message`); 0.11.1 ignores it and would show saved notes as ordinary bot messages.

## What changed in 0.11.1

Small fixes on top of 0.11.0.

- **OpenCode replies.** The reply is only the last text part of the run, so text OpenCode writes before a tool call (for example "I’ll read my context files first.") is no longer glued onto the answer. The earlier parts still stream as progress.
- **Model lists.** Concurrent model-list requests for one engine run the CLI once (later callers reuse the result or the failure of the call ahead of them); engines without a list command skip that lock.

## What changed in 0.11.0

Deployed 14 November 2026 (about 9:32 PM ET) in the existing single-user layout; the isolation installer was not run. Action rules with approvals, watchers that start turns, usage and daily budgets, and tooling to run the controller and the bots as different OS users. Full command-line access stays the default: for your own chats and, by default, for routines, wake-ups, assignments and bot-to-bot turns, the provider bypass flags are unchanged from 0.10.x; Bot-Office only adds its gate hook configuration. Only strict-profile runs (below) drop or tighten the bypass flags. No new model calls per turn; everything stays on loopback.

- **Action rules and approvals.** Each CLI calls `bin/bot-office-gate` through its own hook (Claude `--settings` PreToolUse, Codex `-c` overrides, Cursor `.cursor/hooks.json`, Antigravity `.agents/hooks.json`) before a tool runs; the gate asks `POST /v1/agent/gate`. Decisions: touching the controller's secrets (master token, database, Grok webhook config, other bots' dirs) is always refused and logged as a tripwire in Needs-you; your rules (`GET/POST /v1/rules`, `DELETE /v1/rules/{id}`) are never > ask > allow; the presets `outbound` (push, publish, deploy, send) and `destructive` ask in strict-profile runs and are allowed everywhere else, including your own chat. "Ask" refuses the action with a reason and adds an approval to Needs-you (`GET /v1/approvals`, `POST /v1/approvals/{id}/approve|deny`). Approving stores a one-shot grant for that bot, that exact action and the job that asked, and queues a resume turn; only that turn can use the grant, once, within 24 hours. Unattended requests nobody decides expire after `BOT_OFFICE_APPROVAL_TTL_MIN` (30) minutes. If the gate cannot be reached, cannot read the event, crashes or runs out of time, the hook refuses the tool call in every run (fail closed, 0.12.4). Rules are tripwires, not a sandbox: shell indirection or relative paths can get around a pattern.
- **Strict profile.** Watcher-triggered runs always use it. Routine, wake-up, assignment and bot-to-bot turns use it only for a bot whose **Scheduled runs** setting is `gated` (`scheduled_runs`, master token only; default `full` keeps them exactly as in 0.10.x: same bypass flags, no release gate, no asks on the presets, MCP tools as before). Under the strict profile Claude runs in `dontAsk` mode with a fixed tool list (Bash, Read, Edit, Write, MultiEdit, NotebookEdit, Glob, Grep, LS, WebFetch, WebSearch, TodoWrite, Task, Skill, BashOutput, KillShell): **MCP tools are refused**, so a routine that needs MCP should stay on `full`. Codex runs in its own workspace-write sandbox with no network unless a `preset network allow` rule covers the bot; because `bot-task`, `bot-msg` and `desk` talk to Bot-Office over loopback, a strict-profile Codex run starts only when `scripts/probe_gate.sh codex` proved those helpers reach Bot-Office from inside the sandbox (`helpers` in `GET /v1/gate`) or such a network rule exists; otherwise it is refused to Needs-you. The secrets tripwire is active in every run, strict or not. Daily budgets count and hold only strict-profile runs.
- **Release gate.** Strict-profile runs start only for providers whose gate a probe proved on this host: run `scripts/probe_gate.sh claude codex …` once after deploy (two small model calls per provider), see `scripts/probe_gate.sh --status` or `GET /v1/gate`. Until then those runs are refused to Needs-you. OpenCode is not wired (its strict-profile runs are refused). "Enforced" means different things per CLI: Claude's and Codex's hooks travel on the command line (`--settings`, `-c`), so a bot cannot change them during a run; Cursor's and Antigravity's hooks are files (`.cursor/hooks.json`, `.agents/hooks.json`) in a directory the bot can write, rewritten before every run, so for those two "enforced" means enforced against honest runs: a bot that edits the file mid-run can turn its own gate off. A restricted Codex bot (`--approve-for-me`, exactly as on master) has no gate hook at all, so its strict-profile runs are refused (Needs-you) whatever the probe says; give the bot full CLI access for gated Codex runs.
- **Watchers.** Per bot, in the bot dialog's Watchers panel or `POST/GET /v1/bots/{id}/watchers`, `PATCH/DELETE /v1/watchers/{id}`: a webhook (`POST /v1/hooks/{id}` with its secret, 64 kB max; `POST /v1/watchers/{id}/secret` rotates it), a path in a workspace (an absolute directory, `~` is refused because it would expand against the controller's home; checked every 10 s; at most 1000 matching files are tracked, always the first 1000 by name), a `gh` read-only poll (allowlisted commands, at least every 5 minutes, scrubbed environment, redacted output) or an assignment status change. A firing queues a "trigger" turn with the payload wrapped as untrusted data. Limits: one run in flight per watcher, a debounce (60 s), at most 6 per watcher per hour (over it the watcher pauses and shows in Needs-you) and `BOT_OFFICE_TRIGGER_RUNS_PER_HOUR` (60) across all watchers. History: `GET /v1/watchers/{id}/runs`; `POST /v1/watchers/{id}/test` fires once. A webhook answers 202 with `{"outcome": "fired"|"debounced"|"busy"}` (only `fired` queued a run; the others were folded into a pending or running one), 429 when capped; a bad `Content-Length` is 400. With isolation on, the controller (`botoffice`) checks path watchers and runs `gh` polls; a path under `ubuntu`'s home that `botoffice` cannot read reports an error, so watch paths inside the workspaces or the shared tree.
- **Usage and budgets.** Every run's time and reported tokens are kept, retries included (`GET /v1/usage`, `GET /v1/routines/{id}/runs` and the Runs button, `GET /v1/assignments/{id}/timeline`). Each bot has a daily budget for strict-profile runs (default 40 turns and 120 minutes, no token cap; New York calendar day). Over it, the next strict-profile run waits until midnight with one chat line and a Needs-you item; **Allow +10 today** (`POST /v1/bots/{id}/budget/allow`) releases it. With a token cap set, a run that did not report its tokens also holds further runs. Your chat turns are never held. Memory edits show as a diff in the turn's events. A bot with **Daily digest** on gets a short summary of yesterday at `BOT_OFFICE_DIGEST_HOUR` (8) ET, built from the database without a model call.
- **Usage-limit notice.** A turn parked on a provider's usage limit for more than 10 minutes gets one notice (per job and attempt): a system line in the bot's chat ("Ada is waiting for Codex's usage limit to reset (back about 21:40, 95 min).") and a Needs-you item, with **Retry on <provider>** for each other provider that is enabled and signed in, and **Keep waiting**. Retry (`POST /v1/jobs/{id}/switch-provider`, body `{"provider": "claude"}` with an optional `"reason"`, owner token only) moves only that job to the other provider with its default model and effort and runs it now; the bot's own defaults stay. That run gets the usual model-switch handoff (a fresh session with the chat context), and steering messages sent to the parked turn go with it. It is never automatic: the scheduler only posts the notice. 409 if the job is not waiting on a usage limit, 400 for the same, a disabled or a signed-out provider. **Keep waiting** (`POST /v1/jobs/{id}/keep-waiting`) closes the buttons; the turn keeps its retry time. The notice disappears when the turn starts or is cancelled. `GET /v1/jobs/{id}/switch-options` lists the choices.
- **Isolation (C1).** See below. Off until the installer runs; the bot dialog shows "Isolation: on/off" (`GET /v1/isolation`). A per-bot **Allow sudo** setting (off by default) decides whether that bot's CLI may use sudo when isolation is on.
- **Token rotation.** `python -m botoffice rotate-token` writes a new master token (0600) to `BOT_OFFICE_TOKEN_FILE` and prints it only with `--show`; it takes effect at once (the controller reads the file on every request). Run as root, the new file keeps the old owner. `python -m botoffice isolation-status` prints the layout checks.

New settings: `BOT_OFFICE_APPROVAL_TTL_MIN` (30), `BOT_OFFICE_TRIGGER_RUNS_PER_HOUR` (60), `BOT_OFFICE_DIGEST_HOUR` (8), `BOT_OFFICE_PRIVATE_DIR`, `BOT_OFFICE_LAUNCHER` (`/usr/local/libexec/bot-office-run`), `BOT_OFFICE_BOTS_USER` (`ubuntu`). Database changes are additive: tables `action_rules`, `approvals`, `gate_log`, `watchers`, `watcher_runs`, `usage_notices`; columns `jobs.origin_job_id`, `jobs.watcher_id`, `job_attempts.usage_json`, `bots.allow_sudo`, `bots.daily_budget_json`, `bots.budget_extra_json`, `bots.digest`; message and job source `trigger`.

### Isolation: controller and bots as different users

Without the installer nothing changes: one user runs the controller and the bots ("isolation: off"), as in 0.10.7. With it:

- The controller runs as system user `botoffice` (system unit `bot-office.service`, `User=botoffice`) from a root-owned copy of the code in `/opt/bot-office`, so the bots cannot change what it runs.
- The master token, the SQLite database and the Grok webhook config move to `/var/lib/bot-office/private` (0700 `botoffice`). The bots cannot read or change them.
- The provider CLIs, `podman` and `gh` polls run as `ubuntu` through one sudoers rule, `botoffice ALL=(ubuntu) NOPASSWD: /usr/local/libexec/bot-office-run`. The launcher is root-owned, takes fixed options only, runs no shell, builds the environment from an allowlist (the job token arrives on stdin, never on the command line), sets umask 007 and sets no_new_privs, so sudo and setuid programs do not work for the bot, unless that bot has **Allow sudo**. `ubuntu`'s own sudo is not touched.
- The shared tree (`/var/lib/bot-office/data`: workspaces, bots, shared memory, inboxes) is group `bo-share` with setgid and sticky dirs (3770) and default ACLs, so a bot cannot rename or replace the controller's entries. Hook config and gate files are read-only for the bots. The controller never follows a symlink when it reads or writes in the shared tree, and its per-bot state (session pointers, handoff marker, snapshots) lives in `/var/lib/bot-office/private/bot-state/<id>`.
- **Sudo check.** `ubuntu` with passwordless sudo could leave no_new_privs through its own systemd user manager, cron or `ssh localhost`. `--apply` therefore runs `sudo -n -l -U ubuntu` and refuses when it finds NOPASSWD or `!authenticate`, printing the choices; the result is recorded in the private dir, and the GUI says "token and DB private" only when it passed. The bots get `XDG_RUNTIME_DIR` and the user bus only for the controller's own podman calls.
- **Fail closed.** With `BOT_OFFICE_PRIVATE_DIR` set (or the controller running as `botoffice`) but the layout incomplete, no job runs and the error says why; the controller never falls back to the shared tree.

Install (as root, on the host; read the dry run first):

```sh
cd ~/printpilot-agentdesk
deploy/install-isolation.sh            # dry run: prints every step, changes nothing
sudo deploy/install-isolation.sh --apply
sudo -u botoffice env BOT_OFFICE_PRIVATE_DIR=/var/lib/bot-office/private BOT_OFFICE_TOKEN_FILE=/var/lib/bot-office/private/token \
  BOT_OFFICE_DATA_DIR=/var/lib/bot-office/data /opt/bot-office/.venv/bin/python -m botoffice isolation-status
```

`--apply` is idempotent. It creates the user `botoffice` and the group `bo-share`, copies the code to `/opt/bot-office` (and installs it there with pip), installs the launcher and the sudoers rule (checked with `visudo -cf`, then `visudo -c`), stops and **disables** (does not delete) the old user unit, copies the shared tree to `/var/lib/bot-office/data`, moves the token, the database (with its WAL) and the webhook config to the private dir (keeping a root-only backup in `/var/lib/bot-office/backup-<time>`), enables lingering for `ubuntu` (podman needs `/run/user/<uid>`), and starts the system unit. Afterwards the token is readable only by root and `botoffice`; use `sudo -u botoffice … python -m botoffice rotate-token --show` if you need a new one. The SSH tunnel and port stay the same. Re-run `scripts/probe_gate.sh` as `botoffice` so the gate probe uses the launcher.

**Smoke check after install.** Bots run without `XDG_RUNTIME_DIR` and the user bus, and `desk` calls `podman exec` as `ubuntu`; podman should fall back to `/run/user/<uid>` (lingering is on), but this is untested through the launcher. Check it once, before relying on desktops:

```sh
sudo -u botoffice sudo -n -u ubuntu /usr/local/libexec/bot-office-run --cwd /var/lib/bot-office/data -- /usr/bin/podman ps
```

It should list the desktop containers, as `podman ps` does for `ubuntu`. Then ask a full-access bot in chat to run `desk screenshot` and report the result; a screenshot in the reply means `desk` works as a bot. If either fails, roll back (below) or report it before using desktops.

Residuals: the bots still share one OS user, so they can read each other's files in the shared tree and could change `ubuntu`'s dotfiles (shell rc, CLI configs) that later runs use. With isolation off, a bot can still read or edit the gate files in its workspace (the gate is then advisory against a determined bot). `--stop-job` finds a bot's processes by an environment marker and is best effort. A contained mode (bubblewrap, per-bot users) is deferred. Untested on a real host so far: sudo, the system unit, ACLs and podman through the launcher (the launcher itself is tested as an ordinary user).

**Rolling back to 0.10.7.** Stop the service and back up the database. If isolation was installed, first run `sudo deploy/install-isolation.sh --rollback` (`--dry-run-rollback` shows the steps): it disables the system unit, removes the sudoers rule, copies the shared tree, token, database and webhook config back to the old data dir (owned by `ubuntu`) and re-enables the user unit. It leaves `/var/lib/bot-office`, `/opt/bot-office`, the launcher, the user `botoffice` and the group `bo-share` for you to remove. Then, because 0.10.7 does not know the `trigger` source and would run turns held by the gate or a budget:

```sh
systemctl --user stop bot-office
sqlite3 ~/printpilot-agentdesk/data/agentdesk.sqlite3 ".backup '/tmp/agentdesk-0.11.0.sqlite3'"
sqlite3 ~/printpilot-agentdesk/data/agentdesk.sqlite3 "UPDATE jobs SET status='cancelled', error='rollback' WHERE status='queued' AND (source='trigger' OR failure_kind IN ('gate','budget')); UPDATE jobs SET source='api' WHERE source='trigger'; UPDATE messages SET source='api' WHERE source='trigger';"
```

0.10.7 ignores the new tables and columns. Without the gate hooks its CLIs run as before 0.11.0 (no approvals, no budgets, no watchers).

## What changed in 0.10.0

Deployed 14 November 2026. Bots can now work on a goal across many turns, wake themselves up, and tell you when they need you. Full command-line access stays the default; nothing here changes provider flags or `cli_access`.

- **Assignments.** An assignment is a goal for one bot: title, goal, "done when", optional due time, a budget (default 20 turns and 120 minutes), "verify before done" (off by default) and when to ask you (`on_block` by default, `always`, or `never`). The **Assignments** button in the chat header lists them with a status chip and a budget meter, and has Start, Stop, Reply, Done and Drop. The ⚑ button on your own message turns it into a new assignment. API: `POST/GET /v1/bots/{id}/assignments`, `GET/PATCH /v1/assignments/{id}`, `POST /v1/assignments/{id}/start|stop|reply`. Stop pauses the assignment and its children: it cancels the running and queued turns and the pending wake-ups, and leaves it `open` so it can be started again.
- **Run until done.** Each assignment turn ends with `BOT-OFFICE-STATUS: done|continue|blocked|waiting_on_user|waiting_on_bot — note` (or the bot runs `bot-task status`). `continue` queues the next turn, until the budget is used up (then the assignment is `blocked`). `done` closes it; with verify on, one extra check turn runs first. A turn without a status line continues once, then blocks. A failed turn blocks with its reason; a timeout first gets its usual single retry on the same job, and both runs count toward the budget. The step after each turn (charge, decision, next turn) is one database transaction keyed by assignment and turn number, so a crash or a concurrent Stop never queues a turn twice; turns that finished just before a crash are settled at startup. Your chat messages are always claimed before the next continuation.
- **The bot's side.** Inside a turn, `bot-task` (on `PATH`, uses only the job's own token) lists and shows assignments, sets status, saves a checkpoint (at most 4 kB) for later turns, adds notes, and creates assignments: for itself, or for another bot as a child of one of its own (at most 3 open children per parent, 2 levels deep, budgets capped at the defaults). A bot may read a child it created and leave notes on it; only the assigned bot (or you) changes an assignment's status or checkpoint. The turn prompt carries the goal, "done when", checkpoint, the last 5 events, the budget left and when to ask you.
- **Wake-ups.** `bot-task wake <id>|- --at <time> | --in 2h | --on-change <assignment> --note …` schedules the bot's own next turn: 5 minutes to 30 days ahead, at most 5 pending and 24 fired per bot per day. `--on-change` fires when that assignment's status changes (not when it merely becomes active again). A wake-up fires once, also across restarts. You see and cancel them in the Assignments panel (`GET /v1/bots/{id}/wakeups`, `DELETE /v1/bots/{id}/wakeups/{wakeup_id}`).
- **Bot-made routines.** `bot-task routine add|list|edit|rm` manages routines the bot makes itself (marked "made by bot" in Routines): at most 10 per bot, at least 15 minutes between runs (checked over every date the schedule can fire on, and again when it fires). A bot cannot change your routines or another bot's.
- **Needs you.** A section at the top of the sidebar (refreshed about every 15 seconds) lists what is waiting on you: assignments waiting on you, blocked (per their "ask me" setting), stalled or past due; failed routine, wake-up, inbox and API turns, and sign-in or usage-limit failures, from the last 72 hours; bots that failed the same way three times in a row. Each item has Reply, Continue, Done, Retry or Dismiss, and disappears once resolved. **Turn on notifications** asks the browser for permission (only when clicked); new items then raise a notification, unless the bot's "Notify me" setting is off.
- **Watchdog.** Once a minute and without any model call: it settles turns the loop missed, flags assignments with no progress for `BOT_OFFICE_STALL_HOURS` (no turn queued or running, no pending wake-up) with a chat line, and cancels a turn still running 60 seconds past its time limit (its assignment becomes blocked).
- **Per-bot settings.** Bot settings add a turn time limit (1 to 60 minutes; empty uses the service default) and "Notify me".
- **Hand back.** After you finish a step on a desktop, **Hand back** in the desktop viewer tells the bot that used it last to continue (resuming its open assignment if it had one).
- **Images in assignment turns.** An assignment or wake-up turn shows images in chat the same way as a chat reply (0.9.2): `![description](path)` in the final reply or `desk attach <file>` / `bot-office-attach <file>`; the status line is removed before images are collected.
- **Model switches in assignment turns.** An assignment, wake-up or routine turn after an engine or model change gets the same fresh handed-off session as a chat turn (0.9.3). A rewound turn's seed prompt also names a switch, and a pending rewind seed that has outgrown its limits falls back to the handoff (last 12 messages) instead of a plain fresh start.
- **Steering and assignments.** Since 0.11.6 a message sent while an assignment or wake-up turn runs steers that turn like any other and is not charged to the assignment's budget. A message with `mode: "queue"`, or one the engine cannot take mid-run, is queued as an ordinary chat turn; it runs before the assignment's next continuation. A steer converted into a queued turn belongs to no assignment, so an assignment reply never supersedes it.

**Cost.** The only model calls the new features make are the assignment turns and wake-ups, within the caps above. The watchdog, Needs-you and notifications are computed from the database.

New settings: `BOT_OFFICE_STALL_HOURS` (6; 0 turns stall flags off), `BOT_OFFICE_MAX_TURN_TIMEOUT` (3600 seconds, the ceiling for a bot's own turn limit), `BOT_OFFICE_BOT_ASSIGNMENTS_PER_DAY` (10) and `BOT_OFFICE_BOT_TURNS_PER_DAY` (100): what one bot may create per rolling day, for itself or others (closing them gives nothing back).

Budgets are checked when each run starts: every continuation, check turn, retry, reply, wake-up and restart replay runs only while its assignment is active and has turns and minutes left (that run's turn is reserved then), otherwise it is cancelled with a chat line; a turn's time limit is cut to the minutes left. A stopped assignment's turn is never replayed after a restart. Your reply during a running turn runs next, whatever that turn decided. Editing your reply to an assignment re-runs it in that assignment, reopening it if it was closed; other assignments whose turns the edit removes are paused with the note "rewound", including ones those turns completed (checkpoints are not snapshotted). A bot's routine never starts within 15 minutes of its previous run's actual start; a run that comes due sooner is deferred (catch-up "once") or skipped. Assignment text a bot wrote is shown to the receiving bot as delimited untrusted data. Database changes are additive: tables `assignments`, `assignment_events`, `wakeups`; columns `jobs.assignment_id`, `jobs.assignment_turn`, `jobs.assignment_settled`, `jobs.assignment_minutes`, `jobs.dismissed_at`, `bots.job_timeout`, `bots.notify`, `routines.created_by`.

**Rolling back to 0.9.3.** Stop the service and take a consistent backup first. 0.9.3 does not know the `assignment` and `wakeup` sources, so cancel their queued turns and relabel them:

```sh
systemctl --user stop bot-office
sqlite3 ~/printpilot-agentdesk/data/agentdesk.sqlite3 ".backup '/tmp/agentdesk-0.10.0.sqlite3'"
sqlite3 ~/printpilot-agentdesk/data/agentdesk.sqlite3 "UPDATE jobs SET status='cancelled', error='rollback' WHERE status='queued' AND source IN ('wakeup','assignment'); UPDATE jobs SET source='api' WHERE source IN ('wakeup','assignment'); UPDATE messages SET source='api' WHERE source IN ('wakeup','assignment');"
```

0.9.3 (the deploy before 0.10.0) ignores the new tables and columns; images bots sent in assignment turns stay in chat, and model-switch handoffs (`.handoff-pending`, the engine/model in `self.sha256`) carry over unchanged. Let running turns finish before stopping the service. (Going further back to 0.9.2 would drop the model-switch handoff; to 0.9.0 would also leave pending steering messages unconverted.) Routines the bots made stay and run as ordinary routines; to remove them as well, add `DELETE FROM routines WHERE created_by='bot';`.

## What changed in 0.9.2

Bots can show images inline in chat by writing `![description](path)` in their final reply or running `desk attach <file>` or `bot-office-attach <file>` from the workspace. PNG, JPEG, GIF and WebP images are supported, up to six per reply and 10 MiB each. Images are kept in the existing per-bot attachment store. (Since 0.12.4 `desk attach` accepts any file type; see [0.12.4](#what-changed-in-0124).)

## What changed in 0.9.3

Hotfix on top of 0.9.2 (image replies; 0.9.1 live steering): the first turn after a model or provider switch now has the conversation's context.

- **Cause.** The job start never compared the job's engine/model with the last completed turn. After a switch it resumed whatever session pointer the target engine had (none: a thin fresh start; a stale one from an earlier time: a resume with the short prompt that knows nothing of the chat since).
- **Fix.** At the start of a job, engine and model are compared with the last completed turn (`self.sha256` snapshot). Claude and Codex, whose resume command takes a model flag, keep the session on a model-only change (normal resume plus the settings note). Any other change (a different engine, or a model change on Cursor, Antigravity or OpenCode) archives the target engine's session pointer as `<engine>-session.handoff-<epoch>` (never deleted) and starts a fresh session whose first prompt is a handoff: the model/provider change, WORKING.md, memory pointers, the last 12 messages with image paths and the image index. Later turns resume normally. Switching A→B→A in settings without a message changes nothing. Settings are not touched by `PATCH /v1/bots/{id}`; instructions, name or effort changes are not model switches. Edit/rewind, Reset session and the failed-resume fallback are unchanged.
- The model-picker hint now says what is carried over.

## What changed in 0.9.0

- **Honest failures.** A failed turn's chat line names the engine and the provider's own words, for example "Claude usage limit reached; resets 12:10 PM ET. Will retry then. Claude said: “…”", and keeps any partial reply below it. A provider error reported in the stream fails the turn even if the CLI exits 0 (the text so far is kept). Obvious secrets (bearer tokens, `sk-`/`ghp_`-style keys, `key=value` secrets, long hex or base64 runs) are redacted from stored failure text, which is clipped to 2,000 characters; a bot whose message to another bot failed is told only the failure kind. Jobs record `failure_kind` (`usage_limit`, `auth`, `timeout`, `provider_error`, `tool_denied`, `interrupted`, `launch`, `incomplete`) and the reported token usage.
- **Retries on the same job.** Usage limits wait until the parsed reset time + 2 minutes (at most twice); provider errors retry after 2 and 10 minutes; a timeout retries once. Auth, tool-denied, launch and incomplete failures are not retried. The job id, chat placeholder and event stream stay the same (events carry an `attempt`; the stream ends at the current attempt's `done`). `BOT_OFFICE_MAX_RETRIES_PER_HOUR` (default 20) caps automatic retries; a **Retry** button (`POST /v1/jobs/{id}/retry`) re-runs a failed or incomplete turn, at most 10 times per job (`MAX_MANUAL_RETRIES`). An edited (rewound) turn is never retried.
- **Restart recovery.** A turn that was running when the controller stopped is re-queued once, with a chat notice, only with proof that none of its earlier processes still run: the systemd service cgroup (`KillMode=control-group`; `/proc/self/cgroup` → `cgroup.procs` holds only the new controller and its children). Processes carrying the job's `BOT_OFFICE_JOB_ID` marker are stopped first in every case (each is re-verified by start time through a pidfd right before it is signalled), but a marker scan is not proof: outside a service cgroup (a shell or SSH session) nothing is replayed. Without proof the turn fails with a notice and a **Retry** button. A second interruption also fails it. Each run has its own execution number, and the interruption is kept in the attempt history.
- **Missed routines.** See [Routines](#routines-and-bot-to-bot-messages): per-routine `catch_up` (`once`/`skip`) with one notice; scheduler errors are logged instead of swallowed.
- **Incomplete is not success.** A bot that ends its reply with `BOT-OFFICE-STATUS: not_done|blocked|waiting_on_user — reason` finishes as `incomplete`, and the chat shows "Not done: reason". `jobs.outcome` records `done`, `unverified` (no line) or the reason. A reply without a status line still succeeds; the chat marks it with a small muted "unverified" label (tooltip only, no notice).
- **Run lanes and desktops.** Up to `BOT_OFFICE_RUN_LANES` (default 3) jobs run at once, one per bot; desktops are leased while free. Your chat and API turns are claimed before bot-to-bot messages, which come before routines.
- **Memory and self-knowledge.** Edits to `memory.md` and `shared-memory.md` are merged (three-way, under a lock, off the event loop) instead of overwriting each other; where both sides changed the same lines, the newer file wins and the job's version is appended under `<!-- merged from job <id> -->`. Repeated identical lines are kept. The merge has a time budget (2 s off the event loop, 0.2 s under the lock if the file changed meanwhile). Whenever no merge can be established (above 256 KB or 4,000 lines, or over the budget), the file is kept as it is, the job's whole version is saved next to it as `memory.conflict-<time>.md`, a dated `## Unmerged edit from <bot> (<time>)` heading in the file points to it, and the chat gets a short system note. A symlinked memory file is ignored. Each job gets `SELF.md` (name, engine, access, desktop, commands, limits) and `worklog.md` (one line per finished turn, last 300); a resumed session is told which settings changed since its last turn.
- **Sessions.** A resume that fails on a usage limit or auth keeps the session; a transient failure retries the same resume once, and if it fails again the session is still kept (the turn fails as transient and is retried later); otherwise the pointer is archived as `<engine>-session.resume-failed` and a fresh session starts. The "settings changed" note is recorded as seen only after a turn that received it succeeded.
- **Bridge.** Controller-side sending, SQLite state, bound sender, inbound hop floor stored with the queued turn, https only, no redirects, no private addresses, a per-bot daily share (see [Grok Bot bridge](#grok-bot-bridge)). **Not yet isolated:** 0.9.0 moves sending into the controller, but bots with full access can still read the webhook key (`data/grok_webhook.json`) and the master token (`data/token`) unless the 0.11.0 [isolation layout](#isolation-controller-and-bots-as-different-users) is installed (it moves both into the controller's private dir). `desk grok install-config` is refused inside a bot turn (operator only).
- **Inbox caps.** Inbox and bot-to-bot turns (inbox files with or without a bot sender, the bridge inbox, the `/inbox` API and agent messages) are capped per sender and overall per rolling hour; over the cap they are refused (inbox files are dropped with a log line).
- **Desktops.** Inside a job, `desk --slot` must name the desktop the job leased.
- **Auth.** A non-ASCII bearer token gets 401 instead of a server error.

New settings: `BOT_OFFICE_MAX_RETRIES_PER_HOUR` (20), `BOT_OFFICE_CATCHUP_HOURS` (24), `BOT_OFFICE_RUN_LANES` (3), `BOT_OFFICE_DESKTOPS` (`desktop-1,desktop-2`), `BOT_OFFICE_INBOX_PER_SENDER_HOURLY` (20), `BOT_OFFICE_INBOX_HOURLY` (60), `BOT_OFFICE_GROK_PER_BOT_DAILY` (10), `BOT_OFFICE_GROK_HOSTS` (unset: any public host). Database changes are additive (new columns and tables only).

**Rolling back to 0.8.0.** Stop the service, take a consistent backup of the database (with its WAL), then mark rows 0.8.0 does not know and restore the bridge files:

```sh
systemctl --user stop bot-office
sqlite3 ~/printpilot-agentdesk/data/agentdesk.sqlite3 ".backup '/tmp/agentdesk-0.9.0.sqlite3'"
sqlite3 ~/printpilot-agentdesk/data/agentdesk.sqlite3 "UPDATE jobs SET status='failed' WHERE status='incomplete'; UPDATE jobs SET status='cancelled', error='rollback' WHERE status='queued' AND not_before IS NOT NULL;"
cd ~/printpilot-agentdesk/data && for f in grok_bridge_rate.json grok_bridge_threads.json; do [ -f "$f.imported" ] && mv "$f.imported" "$f"; done
```

0.8.0 ignores the new columns and tables. Bridge sends made under 0.9.0 are not in the restored JSON files, so the daily count restarts from the imported state.

## Sessions, compaction and WORKING.md (0.7.3)

Each bot is **one continuous provider session**. Bot-Office saves the session id the CLI announces in its JSON stream to `data/bots/<bot-id>/<engine>-session` (mode 0600; an id, not a credential) and resumes it on every later turn with the CLI's official flag:

- Codex: `codex exec resume [OPTIONS] <SESSION_ID> -` (from `thread.started.thread_id`); full-access bots only, because `exec resume` has no `--approve-for-me`/`--sandbox`.
- Claude: `--resume <session_id>`. Antigravity: `--conversation <conversation_id>`, in a stable `data/bots/<id>/antigravity-workspace` (a conversation is bound to the directory it was created in; full-access bots only, restricted bots start fresh each turn). OpenCode: never resumed (`opencode run -s` hangs on 1.19.5; every turn is a fresh session with the compact handoff, see [Providers](#providers)).
- Cursor: `--resume <session_id>`. Cursor chats are scoped to `--workspace`, so full-access Cursor bots use a stable `data/bots/<id>/cursor-workspace` and are pointed at the job directory by absolute path.

A resumed turn sends only a short prompt (the new message plus a reminder about `reply.txt`/`WORKING.md`); history is not re-sent. A fresh session (first turn, after **Reset session**, or when a resume fails) gets the rules, the bot's `WORKING.md` and the last 6 messages inline; `history.md` (last 30 messages) stays in the job directory as a backup. If a resume fails without a reply, Bot-Office drops the pointer and retries once with a fresh session.

Compaction is left to each CLI's native auto-compaction, with a keep/drop policy (keep active and scheduled tasks with how-to, key decisions, open questions; drop finished detail; no lifetime summary), see `src/botoffice/compaction.py`:
- Codex: `-c model_auto_compact_token_limit=<N> -c compact_prompt=<policy>`.
- Claude: `--autocompact <N>` and session-only `--settings` hooks: `PreCompact` prints the policy (appended to the compaction instructions) and `SessionStart` (matcher `compact`) re-adds `WORKING.md`.
- Cursor, Antigravity, OpenCode: native compaction only (no documented instruction hook).
`N` defaults to 100000 tokens (`BOT_OFFICE_COMPACT_TOKENS`, minimum 100000).

`WORKING.md` is the bot's bounded working state (current tasks, upcoming tasks plus how-to, decisions, open questions; the bot prunes finished items). It is copied into each job directory, saved back to `data/bots/<id>/WORKING.md` after every turn and hard-capped at 6000 characters.

**Reset session:** bot settings → *Reset session*, or `POST /v1/bots/{id}/session/reset` (409 while the bot has a queued or running job). Saved pointers are renamed `*-session.reset-<epoch>`; chat history, memory and WORKING.md are kept. Editing a sent message resets the session the same way (`*-session.edit-<n>`), see [Editing messages](#editing-messages-rewind).

## Models

`GET /v1/model-choices` lists enabled engines reporting available (`login_status == ok`); OpenCode authentication is verified by the CLI when a turn runs, each with model ids and labels such as "Codex · gpt-6.1-terra" or "Claude · Opus 5.5". Grok is never listed. Catalogs:

- Codex: `gpt-6.1-terra` (default; the CLI has no cheap list command).
- Claude: `claude-opus-5-5` (default), `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`.
- Cursor: read from `cursor-agent models` (no chat run, cached 10 minutes); default `auto`.
- Antigravity: read from `agy models` (also the login check); default `gemini-3.8-flash-high`.
- OpenCode: read from `opencode models` (no chat run, cached 10 minutes; `GET /v1/model-choices?refresh=1` or **Refresh model list** in bot settings asks again; if the CLI fails, the last good list, else the example IDs). Any other `provider/model` ID can be typed in bot settings; default `opencode/big-pickle`.

Each job stores the engine (`provider`) and `model` it was queued with, taken from the bot's current setting. Model ids are validated (no leading `-`, no spaces) and passed as a separate argv element.

## Thinking effort and capability tier

Each bot has an `effort` next to its model; change it any time with `PATCH /v1/bots/{id}` (`{"effort": "high"}`). It never touches identity, memory, history or the model. Only levels the engine's CLI accepts are offered, and the API returns 400 for anything else:

| Engine | Levels | Flag | Default when unset |
| --- | --- | --- | --- |
| Codex | `low` `medium` `high` `xhigh` | `-c model_reasoning_effort=<level>` | `high` |
| Claude | `low` `medium` `high` `xhigh` `max` | `--effort <level>` | `medium` |
| Antigravity | `low` `medium` `high` `xhigh` `max` | `--effort <level>` | `medium` |
| OpenCode | `low` `medium` `high` `xhigh` (model-dependent) | `--variant <level>` | unset |
| Cursor | none (hidden in the UI, 400 from the API) | no official effort flag | unset |

`GET /v1/model-choices` lists `efforts` and `default_effort` per engine. A job records the effort it was queued with, so later changes never rewrite past jobs. Switching engine keeps the level if the new engine accepts it, else uses that engine's default (or none for Cursor). The one-time `crewdeck-effort-v1` migration gives existing bots their engine default.

`capability_tier` (`small`, `mid`, `frontier`) is shown on every bot and on each model option. It is display only: derived from the current model id by `catalog.capability_tier()` and recomputed whenever the model changes, never stored or editable. `small`: haiku, mini, nano, lite / flash-lite. `mid`: sonnet, fable, composer, non-lite flash, gpt-5.x, `auto`. `frontier`: opus, gpt-6.x (including `gpt-6.1-terra`), o3 / o4. Anything unrecognised is `small`, and the lower tier wins when an id matches more than one rule (`o4-mini` is small).

## Live streaming and the audit trail

Replies fill in live. Each CLI is run with its official streaming output (above) and Bot-Office reads only the documented events it needs: assistant text, thinking, tool calls and the final result; anything else is ignored. This applies to every job that creates a turn: chat, inbox, routine and API.

- `GET /v1/jobs/{id}/events` (bearer auth): with `Accept: text/event-stream` it is **SSE** (`id`, `event`, `data` frames; a final `done` event ends it, `: keep-alive` comments every 15 s); without it, a JSON snapshot `{status, events, next, done, draft, reply_text}`. Resume with `?after=<seq>` or a `Last-Event-ID` header. Event kinds: `start`, `text`, `thinking`, `tool`, `error`, `final`, `done`. The token is never a query parameter; the page uses `fetch` with a `ReadableStream`, not `EventSource`.
- `GET /v1/bots/{id}/messages?pending=true` adds one placeholder assistant message (`pending: true`, negative `id`, live draft in `text`, `job_id`) per queued or running job (`job_status` says which), so a page reload can replay `/events` from 0 and keep going. The page shows the running one as the live bubble and counts the others ("N more queued"; details in the drawer). The default listing is unchanged. **Paged history (0.12.5):** `GET /v1/bots/{id}/history?limit=50` returns `{messages, has_older, has_newer}` (oldest first); add `before=ID` (older page), `after=ID` (newer page) or `around=ID` (centred), keyed by message id, so no page duplicates or skips a row while messages arrive. `pending=true` adds the in-flight placeholders only with a page that reaches the newest message. **Search:** `GET /v1/search/messages?q=...&bot=ID&limit=30&before=ID` (newest first; omit `bot` to search every bot) returns `{hits:[{id, bot_id, bot_name, role, sender, created_at, snippet}], has_more}`; the snippet wraps each match in U+0002 and U+0003.
- The page shows the growing reply in the bubble and puts thinking and tool calls in a one-line, collapsed disclosure. When the job finishes the bubble is replaced by the normal history entry.
- The reply is still `reply.txt` when the model writes it, then the assembled stream text, then a short log tail.

**Audit.** For every job (chat, inbox, routine, API) Bot-Office keeps, in `data/agentdesk.sqlite3` (the historical database filename is unchanged): prompt, engine (`provider`), `model`, `effort`, `source`, `bot_id`, `routine_id`, timestamps, `exit_code`, `error`, the **full** assembled reply (`jobs.reply_text`, also on `GET /v1/jobs/{id}`; chat display is truncated at 20,000 characters, the audit copy is not), the rolling draft, and every stream event (`job_events`, keyed by job id and sequence). Events are also appended to `data/logs/<job>.events.jsonl`, and the raw CLI log stays at `log_path`; both are mode 0600. `GET /v1/jobs` omits the reply text to stay small. Deleting a bot keeps its job rows and events. Bot turns receive a job-scoped messaging token; only its hash is stored. Only normalized events are stored.

## Bots, shared memory and chat

Create bots with `POST /v1/bots` (`name`, optional `engine`, `model` and `effort`, `icon` emoji, `description`, `instructions`, `reason`; `default_provider` is still accepted as an alias for `engine`), then use `GET/PATCH/DELETE /v1/bots/{id}`, `GET/PUT /v1/bots/{id}/memory`, and `GET/PUT /v1/shared-memory`. Memory lives in `data/bots/<id>/memory.md` and `data/shared/memory.md`. `POST /v1/bots/{id}/messages` (`text`, optional `reason`) queues a turn; `GET /v1/bots/{id}/messages` returns history, oldest first. `POST /v1/jobs` also accepts `bot_id`, `model` and `reason`.

`PATCH /v1/bots/{id}` with `engine`/`model` switches the model (`effort` is set the same way); if only the engine changes, that engine's default model is used. `DELETE /v1/bots/{id}` removes the bot, its messages, routines and `data/bots/<id>/` (memory and icon), cancels its queued jobs and stops a running one. Shared memory, other bots and job records are kept.

Icons are an emoji (`icon`) or an uploaded image: `PUT /v1/bots/{id}/icon` with the raw PNG, JPEG, GIF or WebP bytes as the body (max 256 KiB, checked by file signature; SVG is rejected), `GET /v1/bots/{id}/icon` (bearer auth), `DELETE /v1/bots/{id}/icon`. The file is `data/bots/<id>/icon` (mode 0600, directory 0700).

On first start of 0.3.0 and later a one-time migration adds the new columns, gives existing bots a model and emoji icon, and renames "Cursor Bot" to "Scout" (same id, memory and history). "Cursor Bot" stays as an alias so bridge senders that still use the old name reach Scout.

Each bot job workspace gets `instructions.md`, `memory.md`, `shared-memory.md`, `history.md` (last 30 retained messages up to the current one; the complete retained chat on the first turn after an edit), `DESKTOP.md` and `prompt.txt`. The model writes its reply to `reply.txt`; the controller records it in chat (falling back to a short log tail), copies a changed `memory.md` back, and copies a changed `shared-memory.md` back under a file lock. Failures appear in chat as a short message.

## Image attachments

Press Ctrl+V to paste images anywhere in the open chat, drag them onto the composer or chat, or use the paperclip to choose files. Pasted HTML images can use data URIs or same-origin URLs. Browsers cannot read file references copied from a file manager; drag the files in or use the paperclip instead. Uploads start immediately; remove an image with × before sending. Sent images appear in the history and open at full size when clicked. Up to six PNG, JPEG, GIF or WebP images can accompany a message, each at most 10 MiB. Uploads are limited to 30 attempts per bot per minute and 200 files or 500 MiB per bot. A global 2 GiB limit applies across bots. Removing a preview deletes its unreferenced upload. Unreferenced uploads older than 24 hours are pruned at startup and on upload; referenced uploads count toward the global limit until the bot is deleted.

The authenticated API accepts raw image bytes at `POST /v1/bots/{id}/attachments` and returns an ID, detected MIME type, size and URL. Pass IDs in the `attachments` array on `POST /v1/bots/{id}/messages`; history returns those IDs. Authenticated `GET` of the returned URL serves the image; `DELETE /v1/bots/{id}/attachments/{attachment_id}` removes an unreferenced upload. The server checks signatures rather than trusting the content type or a filename, uses generated names, refuses symlinked attachment directories and files, and stores new files with mode 0400 in `data/bots/<id>/attachments/` (directory mode 0700). Images are copied to `data/workspaces/<job-id>/.bot-office-attachments/` for each turn; those copies are removed when the job finishes, fails, or is cancelled, and stale copies for inactive jobs are removed at startup. Protect the data directory and backups as image content may be sensitive. Signature checks are not full image decoding or malware scanning. The upload rate limit is per controller process and resets on restart.

| Provider | Image delivery |
| --- | --- |
| Codex | Native `--image=<path>` for each image. |
| OpenCode | Native `--file` for each image. |
| Claude Code | Path reference and instruction to use its Read tool. |
| Cursor, Antigravity (`agy`), Grok | Path reference only; the agent must open the file, and quality depends on its CLI's file and vision tools. Grok is currently disabled. |

All providers receive absolute paths and an instruction to view the files in the prompt. Codex was verified live on 14 November 2026: a bot correctly described an uploaded test image. OpenCode, Claude Code, Cursor and Antigravity image handling has not yet been verified with live CLIs. No modified binaries or subscription credential handling are used. Bot-to-bot messages and routines do not carry attachments.

## Bot-Office page

With the SSH tunnel open, browse to `http://127.0.0.1:8787/`. It is one dark-mode page with no external assets (inline CSS and JS, locked down with a hash-based Content-Security-Policy). The page and bundled noVNC files are public local assets; API operations require authentication.

- Left sidebar: your bots (icon, name, current model, and status dot), plus **New bot**.
- Center: the selected bot's chat history and composer (Enter sends, Shift+Enter adds a line). A running turn is one live bubble with animated dots, a short status line, the elapsed time and Stop; jobs are not cards in the chat, and their steps, logs and the queue are in the Details drawer (Activity button). The composer stays usable while the bot works: a message steers it and is marked as delivered. History is polled every 2 seconds.
- **Desktop**, **Agents**, **Memory** and **Routines** open the corresponding controls; the desktop starts view-only and **Take over** enables input.
- **Settings**: edit name, emoji or uploaded image, description, model, thinking effort (only the levels that engine supports; hidden for Cursor), the derived capability tier (read only) and instructions; delete the bot after typing its name to confirm.
- Each bot shows an unread badge with the age of its oldest unread reply; the tab title shows the total. **Unread** (Alt+U, or right-click a bot) marks the open bot as unread.
- ✎ on your own message edits it and rewinds the chat to that point; **Show archived** reveals what an edit archived.
- 🎤 dictates into the composer (Ctrl+Shift+M), ▶ reads a reply aloud, 🔈/🔊 reads new replies aloud, and **Voice** chooses the voice and speed. These appear only once voice is installed (see below).

Paste the API token into the password field. It is kept only in `sessionStorage` (`bot-office-token`; older `crewdeck-token` and `agentdesk-token` values are picked up once) and sent as a bearer header, never in a URL. **Lock** forgets it.

## Unread notifications

Every bot has a read watermark stored in SQLite (`bot_reads`), so unread counts survive restarts and are shared by every open tab. A reply or system message (from the bot, a routine, or another bot) newer than the watermark is unread; your own messages and archived messages never are. `GET /v1/bots` and `GET /v1/bots/{id}` include `unread_count`, `unread_since` (the oldest unread reply, or when the bot was marked unread), `marked_unread` and `read_revision`.

Opening a bot marks the replies you have actually seen as read (`POST /v1/bots/{id}/read`, optional `up_to`, `revision`, `explicit`). A bot you marked as unread (`POST /v1/bots/{id}/unread`) stays unread until you explicitly open it again; background polling and other tabs cannot clear it, because a read carrying an older `read_revision` is rejected as a whole. A click sends the revision it saw when it happened, so marking the bot unread in another tab while it opens is not undone; if that tab's copy was already out of date, the click is retried once with the state fetched at the moment of the click. On first start of 0.8.0 every existing message counts as read.

## Editing messages (rewind)

✎ on one of your own chat messages (typed in the page or sent with `POST /v1/bots/{id}/messages`) lets you change its text and images and run it again: `POST /v1/bots/{id}/messages/{message_id}/edit` with `text`, optional `attachments`, `reason` and `cancel_running`. The original message and everything after it are archived (kept, hidden by default, shown with **Show archived** or `GET /v1/bots/{id}/messages?archived=true`), and the edited message is queued as a new turn.

If a reply is queued or running, the edit returns 409 with the job ids; the page asks before retrying with `cancel_running: true`, which cancels them and waits up to 15 seconds. The edit is validated before anything is cancelled; if it still fails afterwards, the response says that the reply was cancelled but the chat was not changed. A reply that finishes after an edit is discarded, together with its memory update and any reply to another bot. Routine, inbox and bot-to-bot messages cannot be edited.

An edit **resets the bot's provider session** (the same mechanism as *Reset session*, see [Sessions](#sessions-compaction-and-workingmd-073)): before the bot's next job, even after a crash, every saved `data/bots/<id>/<engine>-session` pointer is renamed to `<engine>-session.edit-<n>` (kept for audit), so no turn can resume a session that saw the archived messages. The first turn after the edit therefore starts a fresh provider session, and its prompt is seeded with the complete retained chat (not just the last 6 messages; inlined up to 64 KiB, otherwise the prompt tells the bot to read `history.md`, which always holds it) plus the retained images, copied into the workspace. After that turn the bot resumes the new session as usual. If the replacement reply is cancelled, or fails before its provider process starts, the obligation stays and the bot's next turn carries the seed (and again starts fresh) instead; it is cleared only once a provider process has actually run with it. If that seed exceeds 400,000 characters, 24 images or 64 MiB, or a retained image file is missing, the edit is refused (413 or 409) and nothing changes.

Archived messages never reach a later turn: `history.md`, the recent-message block and the seed contain only retained messages, the pre-edit session (and any native compaction summary in it) is never resumed, and `WORKING.md` is rolled back to its state before the first archived turn that changed it (Bot-Office snapshots it before each turn writes it back; a turn that finishes after an edit does not save its `WORKING.md`). Turns that ran before 0.8.0 have no snapshot, so their `WORKING.md` changes are kept.

Neither CLI can fork a session at a chosen turn (`claude --fork-session` and `codex fork` copy a whole session; OpenCode `--fork` likewise), so a reset plus seed is used for every provider.

| Provider | How a turn runs (0.7.3) | After an edit |
| --- | --- | --- |
| Codex | Resumes the bot's session: `codex exec resume <id> -` (full-access bots; restricted bots run a fresh `codex exec` each turn) | `codex-session` reset; the next turn is a plain `codex exec` with the seeded prompt, and its new thread is resumed afterwards. |
| Claude Code | Resumes the bot's session: `claude -p --resume <id>` | `claude-session` reset; the next turn runs without `--resume`, seeded, and its new session is resumed afterwards. |
| OpenCode | Never resumes (OpenCode 1.19.5 `run -s` hangs): each turn is a fresh `opencode run` whose prompt carries the compact handoff (WORKING.md, last 12 messages) | The next turn is seeded as usual; there is no `opencode-session` pointer to reset. |
| Antigravity (`agy`) | Resumes the bot's conversation: `--conversation <id>` in the stable `antigravity-workspace` (full-access bots; restricted bots start fresh each turn); the prompt points at the job directory by absolute path and the reply is the streamed final message | `antigravity-session` reset; the next turn runs without `--conversation`, seeded. |
| Cursor | Resumes the bot's chat: `--resume <id>` in the stable `cursor-workspace` (full-access bots; restricted bots start fresh each turn) | `cursor-session` reset; the next turn runs without `--resume` and reads the seeded `prompt.txt`. Not installed here, untested. |
| Grok | Disabled | Not applicable. |

Only the chat is rewound. Files the bot changed, commands it ran, desktop actions, messages sent to other bots, routines it created, and memory or shared-memory updates from archived turns are **not** undone, and the page says so before you confirm.

## Voice (dictation and read-aloud)

Voice is optional, off until installed, and runs entirely on this host: no audio or text goes to any outside service, nothing is recorded to disk, and audio is never logged. Dictation (🎤) puts the transcript into the composer at the cursor; it never sends the message. Read-aloud is off by default; ▶ reads one reply, 🔊 reads new replies as they arrive while that bot is open. Switching it off stops playback at once in that tab and in other open tabs, and every automatic reply re-checks the saved setting (`GET /v1/voice/prefs`) before it starts.

Install it with:

```bash
scripts/install_voice.sh            # dictation (small.en) and read-aloud (Kokoro fp32)
scripts/install_voice.sh --stt-model base.en --tts-model int8   # smaller, faster, lower quality
scripts/install_voice.sh --no-stt   # read-aloud only (or --no-tts)
scripts/install_voice.sh --restart  # also restart bot-office.service afterwards
scripts/install_voice.sh --uninstall
```

The script needs Linux x86_64, Python 3.11–3.13 (`PYTHON=` to choose) and curl. No apt packages are needed: FFmpeg comes inside the PyAV wheel and espeak-ng inside `espeakng-loader`. It creates a separate venv at `<data dir>/voice/venv` with the hash-locked packages from `deploy/voice-requirements.txt` (wheels only), downloads the models into `<data dir>/voice/`, checks every file against a pinned hash (Whisper `model.bin` against its Hugging Face LFS sha256 at a fixed revision, the small Whisper files against their git blob ids and a pinned sha256, and the Kokoro files against the sha256 digests GitHub publishes for the `model-files-v1.1` release), records what it installed in `voice/MANIFEST.json`, sets modes 0700/0600, and loads each engine once as a smoke test. Downloads go to a fresh private temporary directory with curl's `--max-filesize` set to the pinned size. The script refuses `/` or your home directory as the data dir, and `--uninstall` only removes a `voice/` directory that contains `MANIFEST.json` or the voice venv. Model files are never committed to this repository. Do not install the voice packages into the Bot-Office venv; the controller never imports them.

Downloads: on the order of 150 MB of wheels, 461 MiB for `small.en` (138 MiB `base.en`, 72 MiB `tiny.en`), and 310 MiB plus a 27 MiB voicepack for Kokoro (109 MiB for int8).

Each engine runs in its own worker process, started on first use, at `nice 10`, with a minimal environment (no token or provider keys), a private scratch `HOME` (`data/voice/home`) and resource limits (no core dumps, 8 GiB address space, a bounded process/thread count). A cancelled or failed request always discards its worker, and the whole process group is killed. The voicepack archive is read by the controller only after its sha256 matches `voice/MANIFEST.json`. It is stopped after `BOT_OFFICE_VOICE_IDLE_SECONDS` (default 600) without use, which really returns its RAM. One request runs at a time with up to two waiting; a stuck request is killed after 90 seconds (dictation) or 60 seconds (one read-aloud chunk). Expected RAM while loaded, **estimates not yet measured on the target host**: dictation with `small.en` int8 up to about 1.5 GB at peak (the upstream faster-whisper benchmark figure), read-aloud with Kokoro fp32 about 0.5–1.0 GB. Measure with both loaded on the host before relying on it; with about 10 GB free shared with the CLIs and desktops, use `base.en` or `int8` if memory is tight.

| Setting | Default | Meaning |
| --- | --- | --- |
| `BOT_OFFICE_VOICE` | `auto` | `off` hides voice even when installed |
| `BOT_OFFICE_STT_MODEL` | `small.en` | `tiny.en`, `base.en` or `small.en` (must be installed) |
| `BOT_OFFICE_TTS_MODEL` | `fp32` | `fp32` or `int8` |
| `BOT_OFFICE_VOICE_IDLE_SECONDS` | `600` | stop an idle engine after this many seconds (minimum 30) |
| `BOT_OFFICE_VOICE_PYTHON` | `<data dir>/voice/venv/bin/python` | a different voice interpreter |

API (master token only): `GET /v1/voice` (availability, installed voices, preferences), `GET`/`PUT /v1/voice/prefs` (`auto_read`, `voice`, `speed` 0.8–1.2, `browser_stt`), `POST /v1/voice/transcribe` (a 16 kHz mono 16-bit WAV body, 0.3–120 s, at most 4 MiB, uploaded within 30 s, 20 valid recordings per minute), `POST /v1/voice/speakable` (Markdown to plain-text chunks: code blocks and links are announced, not read out; up to 100,000 characters, converted in linear time off the event loop, 60 per minute) and `POST /v1/voice/speak` (`text` up to 1,000 characters, `voice`, `speed`; returns a 24 kHz WAV, 120 per minute).

Voices are the stock Kokoro-82M v1.0 voicepacks; none imitates a real person. The default is `bm_george`, a calm British male voice. Also offered: `bm_fable`, `bm_lewis`, `bm_daniel`, `bf_emma`, `am_michael` and `af_heart`. Kokoro rates its own voices from A to F, and the British male voices are only C to D grade. Preview them under **Voice**.

**Browsers.** The microphone needs a secure context: HTTPS, or `http://127.0.0.1` / `localhost` such as the SSH tunnel. The page records with MediaRecorder, decodes and resamples the audio to 16 kHz WAV in the browser, and uploads only that WAV, so the server needs no FFmpeg. This was tested in Chromium and Firefox. Safari records MP4/AAC, which should decode the same way, but it is untested. A browser may block read-aloud audio until you have clicked in the page; click ▶ once if it says so. If local dictation is not installed, **Voice** can opt in to the browser's own speech recognition. It is off by default because some browsers (Chrome among them) send that audio to their vendor's servers.

**Not used.** Piper's British voices were rejected: `piper-tts` 1.3+ is GPL-3.0, and its en_GB voices are fine-tuned from the `lessac` voice, whose dataset licence excludes commercial speech synthesis. Paid services (ElevenLabs, OpenAI TTS, Azure Neural en-GB voices) would sound better but are not supported or enabled; there is no paid API in Bot-Office.

## Routines and bot-to-bot messages

`POST /v1/bots/{id}/routines` takes a 5-field cron (`minute hour day-of-month month day-of-week`; `*`, `*/n`, lists, ranges) and a prompt; also `GET /v1/bots/{id}/routines`, `PATCH`/`DELETE /v1/routines/{id}`. Routines have an optional `name`. **Cron fields are interpreted in `America/New_York` (EST/EDT)**, not UTC and not the host zone, so `0 9 * * 1` is 9:00 Monday in New York all year; a repeated wall-clock hour when DST ends fires once, and a time that does not exist when DST starts does not fire that day. Due routines enqueue a `routine` turn, and never overlap a still queued or running firing of the same routine. Runs missed while Bot-Office was down (looked for up to `BOT_OFFICE_CATCHUP_HOURS`, default 24) follow the routine's `catch_up` setting, `once` (default: one run now) or `skip`, and the bot's chat gets one notice either way. A run that comes due while the routine's previous run is still queued or running is owed, not dropped: with `catch_up: once` it runs once when that run finishes, with `skip` it is skipped; either way the chat gets one notice. Editing a routine while it is being scanned makes the scan start over, so an old schedule never fires.

`POST /v1/bots/{sender}/message-agent` accepts `{ "to": "bot name or id", "body": "message", "wait": true, "timeout": 60 }`. It queues the target using the normal worker queue and returns its ID, status, job ID and reply when available. A timeout returns the current status; use `GET /v1/agent-messages/{id}` or `GET /v1/agent-messages?bot=<name-or-id>` to poll. Self messages are rejected, bodies are limited to 10,000 characters, each directed pair is limited to one message per 10 seconds, and message chains stop after three hops.

Within a turn, `bot-msg <to-bot> "message"` uses a job-scoped token supplied by the controller, regardless of how the master token is configured. Agent requests return immediately to avoid worker deadlocks; replies appear in the sender’s chat. Since 0.11.10 a message steers the receiver's running turn by default and `bot-msg --queue` waits for its next turn (see "Bot messages steer too" above). Inside a job, `desk message <bot> <text>` uses the same `message-agent` API with the job's token. Inbox files that carry a bot sender (older callers) must carry the originating job ID and scoped token; they are processed while the job is running or within 60 seconds of its completion (the live API stops accepting the token when the job ends). Messages claiming a bot sender require that binding; human messages without a sender are accepted normally. The master-authenticated message-agent API also supports waiting.

## Live desktops

The Desktop button lists Podman containers named `desktop-N`, their state and the current bot when known. Select a running desktop and connect; the display scales to fit and starts view-only. **Take over** enables keyboard and mouse; **Release** restores view-only mode. View-only input is filtered by the server; take-over and release reconnect with a new mode-bound ticket. Tickets travel in the WebSocket subprotocol, not the URL. Connections are capped at four per desktop and eight total, with a 30-minute idle timeout. VNC traffic goes through the existing loopback API port; noVNC is bundled locally. The controller proxies only validated desktop names to `127.0.0.1:590N` and checks the browser origin.

## Desktop helper

`bin/desk` is on the job `PATH`, and `BOT_OFFICE_SLOT` (and the older `AGENTDESK_SLOT`) are set to the job's leased desktop. It only targets that desktop (or `--slot`, limited to `BOT_OFFICE_DESKTOPS`, default `desktop-1` and `desktop-2`), and uses argv-based `podman exec` with `DISPLAY=:1`. A job that got no desktop is told so in `DESKTOP.md` and `SELF.md`, and its screen commands fail with "No desktop is free this turn; desk is unavailable":

- `desk screenshot [outfile]`, `desk click X Y`, `desk type TEXT`, `desk key KEY`, `desk open URL` (http/https only), `desk message BOT TEXT`.

## Grok Bot bridge

Two-way waking between Bot-Office and a Grok Bot webhook routine. Official Grok Bot / Cursor routine webhooks expect the sender key as `Authorization: Bearer <key>`. A different header name can be stored in the config if needed.

Config file (mode `0600`, under `data/`, never committed): `data/grok_webhook.json` with `url` and `key`, and optional `header` (default `Authorization`) and `scheme` (default `Bearer` when the header is Authorization).

Install the config from stdin (JSON with `url` and `key`, or two lines: url then key). The key is not printed:

```sh
desk grok install-config < webhook.json
```

The controller sends; `desk grok send` and `desk inbox post` are thin clients of `POST /v1/bridge/grok/send` and `POST /v1/bridge/inbox`, so only the controller process reads the webhook key. In a job they use the job's token, and the sender is always that job's bot (`--from` needs the master token). In an SSH shell they read the master token from `BOT_OFFICE_TOKEN_FILE` (default `data/token`); `desk inbox post` needs the master token. The webhook URL must be `https` (plain `http` only for `127.0.0.1`/`localhost`), and a redirect counts as a failed send. The host's resolved addresses must be public (no private, loopback, link-local or reserved addresses), and the connection goes to the address that was checked. `BOT_OFFICE_GROK_HOSTS` (comma-separated) optionally restricts the webhook to listed hosts; a listed host may resolve to a private or loopback address. `desk grok install-config` is for the operator and is refused inside a bot turn.

Until Stage C, bots with full access run as the same OS user and can still read `data/grok_webhook.json` and `data/token`; moving the send into the controller does not hide the key from them.

From a job or an SSH shell:

```sh
desk grok send --to "Grok Lead" --kind question --text "Need a review" --thread THREAD
desk inbox post --to "Ops Bot" --from "Grok Lead" --thread THREAD --text "Please take this" --hop 1
```

`kind=ack` is dropped, sends are limited to one per minute per sending bot, 30 per day total and `BOT_OFFICE_GROK_PER_BOT_DAILY` (default 10) per day per bot, and each thread is capped at 6 hops so the two sides cannot loop. An inbound message's hop is the larger of the stored hop + 1 and the one supplied, so a sender cannot reset the count; the hop is stored in the same transaction as the queued turn, so a refused turn uses no hop. Rate and hop state live in SQLite (`bridge_sends`, `bridge_threads`); a send is reserved before the webhook call and released only if it certainly did not arrive (refused, DNS failure, HTTP error). A timeout or reset after sending may have delivered the message, so it stays counted. The pre-0.9.0 JSON files are imported once and renamed `*.imported`.

## Security rules

Loopback only; the token file stays mode `0600` and is never printed; jobs receive a scoped messaging token but no container socket; the CLIs are already signed in by the OS user; Bot-Office never runs login flows or reads credential files.

Without the isolation installer, bots run as the same OS user as the controller and are trusted. Bot-to-bot hop and rate limits protect against accidental loops, not a deliberately malicious bot: a bot can write a senderless human-style desk message or read service files. Real isolation (controller and bots as different OS users, token and database private) is implemented by `deploy/install-isolation.sh`; see "Isolation: controller and bots as different users" above for what it covers and its residuals.

## Codex sign-in

Sign into Codex interactively as the same operating-system user that runs the service. In a terminal on the host, run `codex login` and complete the official CLI sign-in flow. Bot-Office only checks status with `codex login status`; it never runs the login flow or reads the Codex credential files. If the CLI reports that no session is available, jobs end with `needs_login` until the user signs in.

## View the desktops

Open each desktop using the host's existing desktop access method (for example, its configured VNC or remote desktop viewer). The `desktop-1` and `desktop-2` names are the Podman container names used by the controller. To issue a browser action through the API, send an argv array such as `{"command":["epiphany","https://example.com"]}` to `/v1/slots/desktop-1/exec`.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

## Changelog 0.9.1–0.10.5

- **0.9.1:** real-time steering of a running bot (Claude stream-json injection, interrupt and resume for Codex, Cursor, Antigravity and OpenCode), steer API mode and UI.
- **0.9.2:** bots can display images inline in chat replies (`![](path)`, `desk attach`).
- **0.9.3:** model-switch fix: a fresh handed-off session when the engine or model changes, kept pending handoff until the snapshot is saved.
- **0.10.0:** assignments (goals across many turns with budgets), agent-scoped API and `bot-task`, wake-ups and bot-owned routines, the Needs-you queue and watchdog, minimum cron gap fix.
- **0.10.1:** bots are reminded to read the standing rules.
- **0.10.2:** interactive question cards in chat (`desk ask`) with review fixes.
- **0.10.3:** auto-loaded CLI delegation instructions.
- **0.10.4:** timeout retries name the blocked step; chat messages steer by default, also into queued and parked turns; parked steers survive attempts and restarts.
- **0.10.5:** restart-queued steer turns in the Claude-use log; Codex per-job token usage; images in bot-to-bot reply copies; stream test deflaked.

## Notices

GUI layout ideas (bot sidebar with status, chat-centered layout, live desktop take-over) were inspired by CopilotKit OpenBot (MIT, https://github.com/CopilotKit/OpenBot); no OpenBot code was copied. Bot-Office is MIT licensed; see LICENSE. noVNC is bundled under MPL-2.0; see NOTICE and the bundled license. The optional voice engines and models are not bundled; `scripts/install_voice.sh` downloads them, and NOTICE lists their licences. Provider names are plain text, and Bot-Office is not affiliated with any provider.

Bot-sent `desk message` messages use the same hop and per-pair limits. The `/inbox` API rejects bot senders; use the agent messaging API for bot-to-bot messages.

Agents running on the same host and UID are trusted: they can read files the service can read. The scoped token limits accidental or prompt-injected API misuse through the helper; it is not a sandbox.

### Real-time steering (0.9.1)

While a bot is running, send a steering message to adjust its current task. The
composer defaults to **Steer**; tick **Send after this turn instead of steering** to wait instead (hidden when the engine cannot take a message mid-run).
The messages API accepts `mode: "steer"` or `mode: "queue"`; when omitted it
steers a running job and otherwise starts a normal queued turn. Steering messages
show `pending`, `delivered` (with a delivery time), or `queued` when converted
to a next turn, with the reason (0.11.6). The POST response says which happened in `delivery` and `delivery_note`. Images remain in the bot's persistent attachment directory.

Claude receives a user message on its open stream-json stdin and acknowledges
it through the replayed user message. Codex, Cursor and Antigravity with full access
interrupt within a second or two (a running command gets up to 5 seconds to finish)
and resume the same session; the interrupted step may need re-checking. OpenCode, and restricted
Codex, Cursor and Antigravity, cannot resume, so since 0.11.7 they are interrupted just as promptly
and the same job runs again with the same flags, its prompt, a note of what it had done and the
message (see [0.11.7](#what-changed-in-0117)). When a session ID has not yet
arrived, a resumable engine's interruption waits for it; if the job ends without an ID, the message
is queued. Pending messages also become queued turns after a job ends or is
recovered following a restart. Codex app-server's `turn/steer` RPC could enable
true mid-turn steering in a future upgrade.

### What changed in 0.10.7 (deployed)

The OpenCode model picker lists every model `opencode models` reports (all of them, not a 24-entry shortlist), with suggestions while typing a `provider/model` by hand and a **Refresh model list** button. If the CLI fails, the last good list is kept for another cache period; with no list yet, the static examples are offered and the CLI is asked again after a minute (this backoff applies to the Cursor and Antigravity listings too). Validation from 0.10.6 (F) is unchanged.

### What changed in 0.10.6 (deployed 14 November 2026, about 6:22 PM ET; rollback tag backup/pre-eng1-fixes-0.10.6-20261114, DB copy ~/backups/agentdesk.sqlite3.pre-eng1-fixes-0.10.6-20261114)

Live-verified fixes.

- **A. Status-only replies.** A reply that is only `BOT-OFFICE-STATUS: <state> — <reason>` no longer posts the raw log tail to chat; it shows `Done — <reason>` (or `(<state>: <reason>)` for the other states).
- **B. Codex reset times.** "try again at Nov 15th, 2026 2:23 AM" is now understood (an `at` before the date and `st`/`nd`/`rd`/`th` day suffixes), so the retry is scheduled for that time.
- **C. Antigravity.** Resumed `agy` conversations are bound to the directory they were created in, so full-access Antigravity bots now always run in `data/bots/<id>/antigravity-workspace` and are pointed at each turn's job directory by absolute path (restricted bots start fresh each turn). A new stream parser reads agy's `stream-json` (text, tools, usage, errors), and the reply is the streamed final message like the other engines. A conversation saved before this change is archived once as `antigravity-session.cwd-migrate` and a fresh one starts.
- **D. OpenCode.** OpenCode 1.19.5 `run -s` resume hangs, so every OpenCode turn is now a fresh session carrying the compact handoff context (WORKING.md and the last 12 messages); steering messages are queued as the next turn. An OpenCode process that prints nothing at all in its first 60 s is stopped and the turn fails as a provider error (retried by the normal rules); after its first output the guard is off (the turn timeout covers the rest), and a job with an open `desk ask` question is not stopped either.
- **E. Bot status.** `GET /v1/bots/{id}` (and the bot returned by `PATCH`) report the same live `status`/`activity` as the bot list.
- **F. Model validation.** An OpenCode bot's model must be `provider/model`; `POST`/`PATCH /v1/bots` return 400 otherwise.

### What changed in 0.10.5 (deployed 14 November 2026)

Leftovers from the 0.10.x releases.

- **Steered turns after a restart are logged.** A steering message that becomes a queued Claude turn during restart recovery is now written to the Claude-use log with its reason, once, like any other Claude turn.
- **Codex token usage per job.** `codex exec` reports the thread's running total, not the turn's (inferred from stored production values: one resumed thread went 15.1M, 16.5M, 16.9M, 17.1M input tokens over four jobs, the last two about a minute long; no raw CLI capture was made, so if Codex ever reports per-turn usage this would undercount). A job's usage is now that total minus the total before the job (a baseline kept next to the session pointer), so an interrupted-and-resumed steering run counts both segments and history is not charged again; other engines sum the segments. A resumed thread with no recorded baseline reports no usage for its first job after the upgrade.
- **Bot-to-bot reply copies carry images.** The copy of a bot's reply that goes back to the asking bot now attaches the reply's images in that bot's own attachment store (same limits as a normal reply) and handles the image markdown the same way, instead of keeping the raw markdown.
- **Test fix.** `test_cancel_and_legacy_jobs_end_the_stream` no longer races the live scheduler (a "legacy" job could be claimed and run before the test cancelled it) and waits on conditions instead of fixed timing. No product change.

### What changed in 0.10.4 (deployed 14 November 2026)

Faster bots. When a turn hits its time limit and is retried, the retry now tells the bot which command it was blocked on and asks it not to run that blocking call in the foreground again (long sub-commands go in the background or under `timeout`, and the bot checks whether the output already exists first). A chat message sent to a busy bot now steers by default in every case: also when the bot's turn is still queued or waiting for a retry (the message is added to that turn and shows up in its prompt when it starts, instead of becoming a separate job), and also into running routine, wake-up and assignment turns. Bot-to-bot inbox turns are never steered into. "Queue as next turn" is still available in the web UI while the bot is running or queued, and resets after each send.

### What changed in 0.10.2

This release is on top of 0.10.1 (standing-rules notice in prompts).

Bots can post a short progress note using `desk say "<text>"` or `bot-office-say "<text>"` (use `-` to read stdin). The job-scoped `POST /v1/agent/say` endpoint accepts `{"text": "..."}`; notes are limited to 500 characters, one every 10 seconds and 10 per turn. They do not end the turn or replace its final reply.

Bots can ask interactive questions in chat with `desk ask` or `bot-office-ask` and a JSON object, or include an `ask` JSON fence in their final reply. Cards support multiple steps, custom answers and skipping. The helper waits by default and prints the answer as JSON; `--no-wait` prints the question ID, and a skipped wait returns `"skipped": true`.
