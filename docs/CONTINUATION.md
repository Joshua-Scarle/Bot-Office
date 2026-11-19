# One continuation system (0.11.10)

Bot-Office continues a bot without the owner in two places:

- **chat**: a chat (or API, routine, wake-up) turn ends `BOT-OFFICE-STATUS: not_done`. A hidden "continue" turn is posted in the same session. The working bubble shows "continuing, n of max".
- **task loop**: an assignment turn ends `continue` (or `not_done`). The next turn is queued at loop priority. A `done` on a task with `verify` set queues a check turn.

Both paths ask one function, `continuation.decide(settings, turn)`. `scheduler._auto_continue` and `assignments._settle` build a `Turn` (path, kind, the run of turns since the owner last spoke, open question/approval, owner message waiting, task budget, chat-only) and act on the returned `Decision`. Neither path keeps its own stop rules or counters. A bot's own `bot-task start` or `bot-task status ID continue` sent from outside the task's own turn (a chat, routine or wake-up turn) queues a loop turn too, so it asks `decide` as well; a stop refuses it with 409 and leaves the task as it is. A wake-up is an automatic turn too: when it comes due, `assignments._fire` asks `decide` (kind `wakeup`). A wake-up on a chat-only bot, while auto-continue is off, or on a task that is `waiting_on_user` or `blocked` is cancelled with a "Wake-up skipped: ..." line instead of reviving the task; the owner's reply resumes it. When the loop pauses or blocks a task, its pending wake-ups are cancelled with one line. The text heuristics (needs-you phrases, no-progress) live in `autocontinue.py`. `continuation.py` uses them.

## Stop rules (in order, both paths)

1. The bot is **chat-only** (`bots.chat_only`).
2. Auto-continue is **off** (Settings).
3. The turn reported something other than continue/not_done (`blocked`, `waiting_on_user`, ...). That status stands as before.
4. The task's own **turn/minute budget** is used. The task is blocked, as in 0.10.
5. A **blocking question card** is open, or an **approval** is pending.
6. An **owner message is waiting**. Chat: the owner's turn is the continuation. Task loop: the next step queues behind the owner's turn (claim order), so the owner always goes first.
7. The reply or its status reason **says it is blocked or needs the owner** (`needs_user_phrase`).
8. **No progress** in `no_progress` turns in a row (default 3, `made_progress`).
9. **Consecutive automatic turns** since the owner last spoke exceed `consecutive` (default 10). The owner speaking means a chat message, an assignment reply/start, or a message steered into the running turn.
10. Chat only: the chain exceeds `max` (default 10, 1..50; the 0.11.10 setting).

A stop always leaves a visible line:
- Chat: "Stopped continuing automatically: <why>. Reply to keep it going."
- Task: the task goes `waiting_on_user` with "paused, not blocked: <why>. Reply to resume." It shows in Needs you, and the owner's reply resumes it.

Each finished job is decided once. Chat uses `jobs.auto_decision`, set with a compare-and-set. Tasks use the existing `assignment_settled`.

## Caps on automatic turns

Automatic turns are jobs with `jobs.auto_kind` set: `chat` (chat continuation), `loop` (task continue, or a bot's own `continue` report), `verify`, `evidence`, `wakeup`. Owner chat, owner Start/Reply and routines are not automatic.

The caps are checked when a job is claimed (`continuation.cap_gate` in `store.claim`, in the claim transaction):
- `per_bot_day`: 20 per bot in a rolling 24 h.
- `per_hour`: 20 for all bots in a rolling hour.

Either can be `null`, meaning unlimited. While auto-continue is off (or the bot is chat-only), no queued automatic job runs: claim cancels it with the line "An automatic turn was not run: auto-continue is now off. Reply to keep it going.", and its task waits for the owner. A held job was decided before it waited, so when its hold ends it is checked again: if auto-continue was switched off, the bot was made chat-only, its task is no longer active, or it was held longer than `hold_hours` (default 12), it is cancelled with the chat line "A held automatic turn was not run: <why>. Reply to keep it going." and its task waits for the owner ("paused, not blocked"). A job over a cap is **deferred, not dropped**: `not_before` is set to when the window slides, `failure_kind='auto_cap'`. One chat line names the cap and says it can be raised in Settings. A Needs-you item (`kind: auto_cap`) stays while turns are held. Saving the settings releases held turns for a re-check. An owner chat message cancels the bot's queued chat continuations. An assignment reply cancels its loop turns.

## Done needs evidence

`bot-task done [ID|current] --evidence "<how it was verified>"` (or `POST /v1/agent/tasks/{ref}/status {"state":"done","evidence":...}`) stores an `evidence` task event.

A bot's `done` (status line or call) on a task with no evidence event:
- The first time, the loop continues once with "say how you verified it (bot-task done --evidence ...)".
- The next time, the task goes `waiting_on_user` with "says it is done; no evidence".

Both count since the task last became active: after a reopen, rewind or resume, the first done without evidence gets its ask turn again.

Only evidence given since the task last became active counts (a start, a reply that resumes it, a reopen or a rewind starts over), and only text at least `evidence_min` characters long (shorter evidence is refused with 400). The accepted evidence is added to the done note (`...; evidence: <text>`), and a task waiting on the owner shows the last evidence given in Needs-you (`evidence`).

Owner completion needs no evidence. Setting `evidence` (default on) turns the gate off. Peer review stays opt-in per task (`verify`, `reviewer`). Plain chat turns are untouched.

## Chat-only bots

`bots.chat_only` (default 0; create/PATCH `chat_only`; checkbox in the bot dialog). Such a bot never auto-continues and gets no loop, verify or wake-up turns:
- A task it is on pauses as `waiting_on_user`.
- Its own `bot-task start`/`continue` leaves the task as it is, with a 409.
- Its wake-ups are skipped with a line.

Its SELF.md says continuation is off. The owner's Start and Reply still run one turn each.

## Settings

All of these are in `GET/PUT /v1/settings` under `auto_continue`. A value saved there beats the env default.

| key | default | env default |
|---|---|---|
| enabled | true | - |
| max | 10 (1..50) | - |
| no_progress | 3 | BOT_OFFICE_AUTO_NO_PROGRESS |
| consecutive | 10 | BOT_OFFICE_AUTO_CONSECUTIVE |
| per_bot_day | 20 (or null = unlimited) | BOT_OFFICE_AUTO_TURNS_PER_BOT_DAY |
| per_hour | 20 (or null = unlimited) | BOT_OFFICE_AUTO_TURNS_PER_HOUR |
| evidence | true | BOT_OFFICE_DONE_NEEDS_EVIDENCE (on/off) |
| evidence_min | 12 (1..1000 characters) | BOT_OFFICE_DONE_EVIDENCE_MIN_CHARS |
| hold_hours | 12 (1..720) | BOT_OFFICE_AUTO_HOLD_HOURS |

`max` cannot be saved above `consecutive` (422): the chat chain stops at whichever comes first. If `consecutive` is later lowered below it (a save or an env default), `GET /v1/settings` returns the limit that applies as `auto_continue_chat_limit`, Settings shows it, and the chat bubble counts against it.

Not built: quiet hours, a physical/irreversible done gate, and a pick-up loop that starts open tasks by itself.

## Migration and rollback

The migration is additive:
- `jobs.auto_kind`, `jobs.auto_decision`, both TEXT NULL.
- `bots.chat_only` INTEGER DEFAULT 0.
- New keys in the existing `auto_continue` meta JSON.

Older code ignores all of these. To roll back, stop the service with no job running, then before starting the previous build:

- drain pending bot steers: `UPDATE messages SET steer_status='delivered' WHERE source='inbox' AND steer_status='pending'` (0.11.9 would re-queue them as owner chat turns);
- cancel held jobs: `UPDATE jobs SET status='cancelled', error='rollback' WHERE status='queued' AND failure_kind='auto_cap'` (0.11.9 would run them without the cap check).

Bot-message links (steered into, reply to, chat rows) live in the side table `agent_message_links`, not in `agent_messages`, because 0.11.9 inserts into `agent_messages` positionally.
