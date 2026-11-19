# Bot-Office

**Display name:** Bot-Office.
**Slug / package / CLI identifier:** `bot-office` (use `botoffice` where a hyphen is not allowed, including the Python package and `python -m botoffice`).
**Status of this tree:** Version 0.11.10. What changed is recorded below: [0.11.10](#what-changed-in-01110), [0.11.9](#what-changed-in-0119), [0.11.8](#what-changed-in-0118), [0.11.7](#what-changed-in-0117), [0.11.6](#what-changed-in-0116), [0.11.5](#what-changed-in-0115-deployed), [0.11.4](#what-changed-in-0114-deployed), [0.11.3](#what-changed-in-0113-deployed), [0.11.2](#what-changed-in-0112-deployed), [0.11.1](#what-changed-in-0111), [0.11.0](#what-changed-in-0110), [0.10.0](#what-changed-in-0100), [0.9.3](#what-changed-in-093) and [0.9.0](#what-changed-in-090).

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

## What changed in 0.11.10

**Bots keep working until the task is done.** A chat, API, routine or wake-up turn that ends with `BOT-OFFICE-STATUS: not_done` is now continued automatically in the same session. The chat shows no extra prompts: the bot's bubble stays "Working · continuing, 3 of 10". It stops when the bot reports done, blocked or waiting_on_user, at the owner's limit (default 10 in a row, up to 50), when you press Stop, or when you have a message queued for it. A message you steer into a continuing turn starts a fresh count. It does not run while a question or approval is open, or for assignment turns (they keep their own loop and budget), bot-to-bot replies or watcher runs. A continuation keeps the source of the turn it continues, so unattended turns stay under the unattended rules. Rule 7 of the prompt now says to keep working and to use not_done only for a real pause.

**Two pages every bot gets, both editable by the owner:**

- *Rules for all bots* (`src/botoffice/prompts/rules-for-all-bots.md`, about 420 words): finish what you start (keep going until the goal is met, you're stuck and need the user, or a rule says stop), work on assigned tasks rather than a lifelong purpose, decide reversible things, rarely ask (the owner's approval classes ask for them), verify, report honestly. Edit it first in Settings. API: `GET`/`PUT`/`DELETE /v1/rules-for-all-bots`.
- *System prompt* (`src/botoffice/prompts/system.md`, about 750 words): how Bot-Office works (turns and status, replies, memory files, other bots, delegation, follow-ups, trust). It is prebuilt and kept under Settings → Advanced. API: `/v1/system-prompt`, and `/v1/baseline` as before. A 0.11.9 baseline override carries over as the system prompt override.

Both are inlined in every fresh prompt (system prompt first) and written as `SYSTEM-PROMPT.md` and `RULES-FOR-ALL-BOTS.md` in the job directory (no more `BASELINE.md`). Resumed sessions get a one-line nudge when either changes. The owner's standing rules and the bot's `instructions.md` override both; the numbered rules always apply.

**The chat shows who wrote what.** Only messages you type use your bubble. Messages between bots (bot-msg, inbox drops, reply copies, one bot handing another an assignment), sent or received, show in the chat only as a time and a row like "2 messages with Bot-Office Lead", with that bot's icon; back-to-back messages with the same bot share one row. Click it to read that conversation in an overlay (both bots' icons and names, formatted text, Close Chat or Esc). A bot's own messages to other bots count toward Unread. Bot-Office's machinery (automatic continuations, assignment "Continue" and "Check" steps, wake-up prompts and "Not done (continuing)" lines) stays out of the chat and the Unread count; Show archived and the Details drawer still show it. Routine prompts, watcher runs and assignment starts appear as small notes, never as your messages. Messages carry `author` (owner, bot, bot_out, bot_in, bot_reply, office, system) and `peer` in the API.

**A real Settings page.** The sidebar's ⚙ Settings opens settings for all bots: Rules for all bots, Bot behavior (auto-continue on/off and its limit; `GET`/`PUT /v1/settings`), Approvals, Engines (sign-in status), Voice, and Advanced (system prompt). Each bot's own settings stay in its chat.

**Rarely ask; approvals by class.** Bots no longer ask before every risky step. The owner picks *approval classes*, kinds of action they want done but not on the bot's own, in Settings → Approvals. On by default: sending email or messages, purchases and payments, merging / releasing / force-pushing or pushing to main, admin and system changes (sudo, systemctl, crontab...), and deleting outside the bot's workspace (including deleting through a connector). Off by default: pushing code and posting on GitHub, logging in to other computers, printers and the desktop, and any connector action. You can add your own class (a command pattern or a tool-name pattern), remove it, and set how long an approval waits (60 minutes by default; 0 waits until you answer). Classes apply in every run, your chat included; an owner `allow` action rule still wins for that action. When a bot tries one, the CLI's gate hook stops that one tool call and the bot's chat shows an Approve / Deny card with the whole action. Approve runs exactly that action once in a resume turn (the resume prompt stays out of the chat). Deny, or no answer in time, leaves the task stopped with a one-line note; an expired card can still be approved later. Rule 4 of the rules for all bots now says this: go ahead, Bot-Office asks for you, ask questions only when the goal is unclear or the choice is truly the user's. Which engines can pause, and how, is in [docs/APPROVALS.md](docs/APPROVALS.md): Claude Code, Codex with full access, Cursor and Antigravity through their hooks; opencode and Codex with restricted access cannot be paused; such bots are named in a warning in Settings → Approvals, and their SELF.md tells them never to do those actions themselves but to ask in their reply. API: `approvals` in `GET`/`PUT /v1/settings`; messages carry an `approval` card.

**Bot messages steer too.** A message from one bot to another now reaches the other bot's running turn the way your own messages do, instead of always waiting for its next turn. The sender decides: `bot-msg <bot> "..."` steers by default; `bot-msg --queue <bot> "..."` (or `desk message --queue`, or `"mode": "queue"` on `POST /v1/bots/{sender}/message-agent`) waits for a turn of its own, for anything that can wait. A steered message is marked as from that bot (information, not the user's instructions), arrives after any message of yours waiting for the same turn, and is not answered by the turn's own reply, since that reply belongs to the receiver's task: the receiver answers with a message of its own, and the prompt tells it to. If the receiver is idle the message starts a turn as before; it is never steered into a turn that is itself answering a bot, or into an engine that can't take a message mid-run; and if the turn ends before reading it, it becomes an ordinary queued bot turn whose reply goes back to the sender. Guards: a bot can't message itself; one message per pair per 10 seconds; steered messages count toward the hourly inbox caps; and an exchange between two bots allows three asks with their answers (the answer to a steered ask takes the ask's hop, as a queued turn's reply always has). Each bot has a switch in its settings, "Other bots' messages reach it while it works" (`bot_steer`, on by default); off, bot messages to it queue. The API returns `delivery` (`steer`, `start` or `queue`) and `mode`; a steered message's status is `sent`, then `delivered`.

**Task lists.** Each bot has a task list (the sidebar label stays "Assignments"). Owner and bots can add, edit, reorder, start and drop tasks. Every write carries a version, and a stale write gets a 409 listing what changed. Each prompt shows the bot its open tasks and the changes others made since its last turn. Bots use `bot-task` (`add`, `edit`, `start`, `drop`, `status`, `changes`, ...). Further rules:

- Tasks have a priority (urgent, high, normal, low). Lists, prompts and the queue put higher priority first. Set it with `bot-task add/edit --priority` or the badge and selector in the panel.
- A task added from the form starts on its own; untick "Start now" to leave it open. Start appears only on open tasks that never started.
- A task one bot adds to another bot's list lands open and waits for that bot.
- On its own list, a bot may edit the title and goal of a task you created, never its done-when. Such edits are marked "Edited by a bot", and prompts show them as untrusted text until you rewrite them.
- Dropping a task (by you or a bot) cancels its queued and running turns, and they do not come back.
- A bot's task writes are capped per hour.

Settings (environment, read on every call; unknown values fall back to the default):

- `BOT_OFFICE_CROSS_BOT_SUBTASK_START` = `wait` (default) or `allow`: whether a bot's subtask on another bot's list may start at once. A task without a parent always lands open.
- `BOT_OFFICE_BOT_DROP_OWNER_TASKS` = `with_note` (default) or `owner_only`: whether a bot may drop a task you created on its list, with a note shown in Needs you.
- `BOT_OFFICE_DROP_CANCELS_TURNS` = `on` (default) or `off`.
- `BOT_OFFICE_BOT_EDIT_OWNER_TASKS` = `title_goal` (default) or `off`.
- `BOT_OFFICE_FORM_TASKS_START` = `auto` (default) or `manual`.
- `BOT_OFFICE_TASK_DEFAULT_PRIORITY` = `normal` (default), `urgent`, `high` or `low`.
- `BOT_OFFICE_TASK_WRITES_PER_HOUR` = `200` (default).
- `BOT_OFFICE_TASKS_LABEL` = `Assignments` (default).

`GET /v1/tasks/settings` returns the current values.

Rollback: the database changes are additive (new columns, an index and a marker row), and 0.11.9 ignores them. Tasks created on 0.11.10 still run on 0.11.9, without priority or bot-edit marks.

**Live end-to-end test.** `scripts/e2e_gui.py` drives a real Chromium (or Chrome with `--channel chrome`) through the UI against a throwaway local Bot-Office with a scripted engine and two bots labelled "E2E test bot" (Enter to send, Settings and approvals, the Approve / Deny card, the bot-to-bot row and panel), or read-only against a running install with `--url`/`--token` (`--send-to` sends one real message with Enter, only to a bot whose name starts with "E2E test"). It ends with a plain PASS/FAIL list. Needs `pip install playwright` and `python -m playwright install chromium`.

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

Bots see their peers, schedule, open assignments and recent workspace context in SELF.md, and their open tasks in fresh and resumed prompts. The worklog keeps the last reply paragraph. Progress notes remain separate from replies. Run `python scripts/autonomy_metrics.py --db data/agentdesk.sqlite3 --days 7` for read-only baseline metrics; use `--since YYYY-MM-DD` or `--json` as needed. Roll back by deploying 0.11.2 (tag `backup/pre-autonomy-p0p1-0.11.3-20261115`, DB copy `~/backups/agentdesk.sqlite3.pre-autonomy-p0p1-0.11.3-20261115`); this release makes no data changes. This is phases P0 and P1 of the [bot autonomy plan](docs/plans/bot-autonomy-plan.md); the plan's open questions G1–G10 are still undecided and nothing here depends on them.

## What changed in 0.11.2 (deployed)

Bots can post lasting progress notes during a turn with `desk say "<text>"` or `bot-office-say "<text>"` (`-` reads stdin). `POST /v1/agent/say` (job-scoped agent token, running job only) takes `{"text": "..."}`: 1-500 characters, one note per 10 seconds and 10 per job (429 otherwise). A note is a separate chat row (`kind: "say"`, shown as a muted "progress" line); it never ends the turn or replaces the final reply and status line, is not read aloud, and is left out of the history given to models. It is archived with its turn by an edit. Adds the additive column `messages.kind` (default `message`); 0.11.1 ignores it and would show saved notes as ordinary bot messages.

## What changed in 0.11.1

Small fixes on top of 0.11.0.

- **OpenCode replies.** The reply is only the last text part of the run, so text OpenCode writes before a tool call (for example "I’ll read my context files first.") is no longer glued onto the answer. The earlier parts still stream as progress.
- **Model lists.** Concurrent model-list requests for one engine run the CLI once (later callers reuse the result or the failure of the call ahead of them); engines without a list command skip that lock.

## What changed in 0.11.0

Deployed 14 November 2026 (about 9:32 PM ET) in the existing single-user layout; the isolation installer was not run. Action rules with approvals, watchers that start turns, usage and daily budgets, and tooling to run the controller and the bots as different OS users. Full command-line access stays the default: for your own chats and, by default, for routines, wake-ups, assignments and bot-to-bot turns, the provider bypass flags are unchanged from 0.10.x; Bot-Office only adds its gate hook configuration. Only strict-profile runs (below) drop or tighten the bypass flags. No new model calls per turn; everything stays on loopback.

- **Action rules and approvals.** Each CLI calls `bin/bot-office-gate` through its own hook (Claude `--settings` PreToolUse, Codex `-c` overrides, Cursor `.cursor/hooks.json`, Antigravity `.agents/hooks.json`) before a tool runs; the gate asks `POST /v1/agent/gate`. Decisions: touching the controller's secrets (master token, database, Grok webhook config, other bots' dirs) is always refused and logged as a tripwire in Needs-you; your rules (`GET/POST /v1/rules`, `DELETE /v1/rules/{id}`) are never > ask > allow; the presets `outbound` (push, publish, deploy, send) and `destructive` ask in strict-profile runs and are allowed everywhere else, including your own chat. "Ask" refuses the action with a reason and adds an approval to Needs-you (`GET /v1/approvals`, `POST /v1/approvals/{id}/approve|deny`). Approving stores a one-shot grant for that bot, that exact action and the job that asked, and queues a resume turn; only that turn can use the grant, once, within 24 hours. Unattended requests nobody decides expire after `BOT_OFFICE_APPROVAL_TTL_MIN` (30) minutes. If the gate cannot be reached, the hook refuses strict-profile runs (fail closed); other runs fail open except for the protected secret paths, which the hook still refuses locally. Rules are tripwires, not a sandbox: shell indirection or relative paths can get around a pattern.
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

Bots can show images inline in chat by writing `![description](path)` in their final reply or running `desk attach <file>` or `bot-office-attach <file>` from the workspace. PNG, JPEG, GIF and WebP images are supported, up to six per reply and 10 MiB each. Images are kept in the existing per-bot attachment store.

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
- `GET /v1/bots/{id}/messages?pending=true` adds one placeholder assistant message (`pending: true`, negative `id`, live draft in `text`, `job_id`) per queued or running job (`job_status` says which), so a page reload can replay `/events` from 0 and keep going. The page shows the running one as the live bubble and counts the others ("N more queued"; details in the drawer). The default listing is unchanged.
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
