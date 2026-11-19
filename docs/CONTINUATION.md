# One continuation system (0.11.10)

Bot-Office continues a bot without the owner in two places:

- **chat**: a chat (or API, routine, wake-up) turn ends `BOT-OFFICE-STATUS: not_done`. A hidden "continue" turn is posted in the same session. The working bubble shows "continuing, n of max".
- **task loop**: an assignment turn ends `continue` (or `not_done`). The next turn is queued at loop priority. A `done` on a task with `verify` set queues a check turn.

Both paths ask one function, `continuation.decide(settings, turn)`. `scheduler._auto_continue` and `assignments._settle` build a `Turn` (path, kind, the run of turns since the owner last spoke, open question/approval, owner message waiting, task budget, chat-only) and act on the returned `Decision`. Neither path keeps its own stop rules or counters. A bot's `bot-task start` or `bot-task status ID continue` counts only from the task's own current turn while the task is active (the loop then decides the next turn). From any other turn (a chat, routine or wake-up turn, or an old turn of the task), or on a task that is open, paused, blocked or waiting, it is refused with 409 and the task stays as it is: only the owner starts or resumes a task from outside its turn. A wake-up is an automatic turn too: when it comes due, `assignments._fire` asks `decide` (kind `wakeup`). A bot cannot set a wake-up on its task while the task is `waiting_on_user` or `blocked`, or open because the owner stopped it (409). A wake-up on a chat-only bot, while auto-continue is off, on a task that is `waiting_on_user` or `blocked`, or on a task the owner stopped is cancelled with a `Wake-up ("<note>") skipped: ...` line in the bot's chat instead of reviving the task; the owner's reply or Start resumes it. A task that is active, open and never stopped, or waiting on another bot may still be woken. When the loop pauses or blocks a task, its pending wake-ups are cancelled with one line. The text heuristics (needs-you phrases, no-progress) live in `autocontinue.py`. `continuation.py` uses them.

## Stop rules (in order, both paths)

1. The bot is **chat-only** (`bots.chat_only`).
2. Auto-continue is **off** (Settings).
3. The turn reported something other than continue/not_done (`blocked`, `waiting_on_user`, ...). That status stands as before.
4. The task's own **turn/minute budget** is used. The task is blocked, as in 0.10.
5. A **blocking question card** is open, or an **approval** is pending.
6. An **owner message is waiting**. Chat: the owner's turn is the continuation. Task loop: the next step queues behind the owner's turn (claim order), so the owner always goes first.
7. The reply or its status reason **says it is blocked or needs the owner** (`needs_user_phrase`).
8. **No progress** in `no_progress` turns in a row (default 3, `made_progress`).
9. **Consecutive automatic turns** since the owner last spoke exceed `consecutive` (default 30). The owner speaking means a chat message, an assignment reply/start, or a message steered into the running turn.
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

Either can be `null`, meaning unlimited. While auto-continue is off (or the bot is chat-only), no queued automatic job runs: claim cancels it with the line "An automatic turn was not run: auto-continue is now off. Reply to keep it going.", and its task waits for the owner. A held job was decided before it waited, so when its hold ends it is checked again: if auto-continue was switched off, the bot was made chat-only, the owner has written to the bot since it was held ("your message comes first"), its task is no longer active, or it was held longer than `hold_hours` (default 12), it is cancelled with the chat line "A held automatic turn was not run: <why>. Reply to keep it going." and its task waits for the owner ("paused, not blocked"). A job over a cap is **deferred, not dropped**: `not_before` is set to when the window slides, `failure_kind='auto_cap'`. The job's own `failure_kind` (a retried run's, say) is kept in `held_kind` and put back when it runs; a hold is not a run and counts no attempt or retry. One chat line names the cap and says it can be raised in Settings. A Needs-you item (`kind: auto_cap`) stays while turns are held. Saving the settings releases held turns for a re-check. An owner chat message cancels the bot's queued chat continuations. It is never steered into an automatic turn (queued or held): it becomes a turn of its own.

The chat path stores its decision with what it leads to: the stop line, or the queued continuation, in the same transaction. Until then the finished job is marked `deciding`, and a start after a crash gives such a job `error` and one chat line. An assignment reply cancels its loop turns.

## Done needs evidence

`bot-task done [ID|current] --evidence "<how it was verified>"` (or `POST /v1/agent/tasks/{ref}/status {"state":"done","evidence":...}`) stores an `evidence` task event.

A bot's `done` (status line or call) on a task with no evidence event:
- The first time, the loop continues once with "say how you verified it (bot-task done --evidence ...)".
- The next time, the task goes `waiting_on_user` with "says it is done; no evidence".

Both count since the task last became active: after a reopen, rewind or resume, the first done without evidence gets its ask turn again. The same holds once the owner has written to the bot since the ask.

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
| consecutive | 30 | BOT_OFFICE_AUTO_CONSECUTIVE |
| per_bot_day | 20 (or null = unlimited) | BOT_OFFICE_AUTO_TURNS_PER_BOT_DAY |
| per_hour | 20 (or null = unlimited) | BOT_OFFICE_AUTO_TURNS_PER_HOUR |
| evidence | true | BOT_OFFICE_DONE_NEEDS_EVIDENCE (on/off) |
| evidence_min | 12 (1..1000 characters) | BOT_OFFICE_DONE_EVIDENCE_MIN_CHARS |
| hold_hours | 12 (1..720) | BOT_OFFICE_AUTO_HOLD_HOURS |

`max` cannot be saved above `consecutive` (422): the chat chain stops at whichever comes first. If `consecutive` is later lowered below it (a save or an env default), `GET /v1/settings` returns the limit that applies as `auto_continue_chat_limit`, Settings shows it, and the chat bubble counts against it.

Not built: quiet hours, a physical/irreversible done gate, and a pick-up loop that starts open tasks by itself.

## Migration and rollback

The migration is additive:
- `jobs.auto_kind`, `jobs.auto_decision`, `jobs.held_at`, `jobs.held_kind`, all TEXT NULL. Columns are added under one write lock (`store.add_columns`), so two processes starting at once do not both add one.
- `bots.chat_only` INTEGER DEFAULT 0.
- New keys in the existing `auto_continue` meta JSON.

Older code ignores all of these. To roll back, stop the service with no job running, then before starting the previous build:

- drain pending bot steers: `UPDATE messages SET steer_status='delivered' WHERE source='inbox' AND steer_status='pending'` (0.11.9 would re-queue them as owner chat turns);
- cancel held jobs: `UPDATE jobs SET status='cancelled', error='rollback' WHERE status='queued' AND failure_kind='auto_cap'` (0.11.9 would run them without the cap check);
- mark text a bot edited on an owner's task with a `[bot-edited] ` prefix (the statements are in the README): 0.11.9 does not read `assignments.bot_edited`, so it would quote that text to bots as the owner's own.

Bot-message links (steered into, reply to, chat rows) live in the side table `agent_message_links`, not in `agent_messages`, because 0.11.9 inserts into `agent_messages` positionally.

## Waiting for another bot's reply (0.11.12)

A pending reply to a `bot-msg` is not a blocker. Three parts:

- **Rules.** The rules, the task brief and the `bot-msg` note tell the bot to finish the turn (`done`, on a task `waiting_on_bot`) and that the reply wakes it.
- **Classification** (`Scheduler._waiting_for_bot`). When a turn ends `not_done`, `waiting_on_user` or `waiting_on_bot` while a message it sent in that turn is still unanswered, and the status names the wait (or is `waiting_on_bot`), and nothing asks the owner (no needs-you phrase, no question card, no pending approval), it is a normal finish: `done` for a chat turn, `waiting_on_bot` for a task turn. It is then not incomplete and not in Needs you. `blocked`, a real question to the owner, and a `waiting_on_user` with nothing pending stay as the bot said.
- **The wake** (`Assignments.wake_for_replies`). When the other bot's turn finishes and its reply is copied back, and again when the asking bot's own turn ends (a reply that came while it was running), each reply wakes the asker once, if the asking turn ended done / unverified / waiting on a bot. It is a wake-up (`source=wakeup`, `auto_kind=wakeup`) fired through `_fire`, so `continuation.decide` applies (auto-continue off, chat-only bots, a task paused by the guard, the owner's stop) together with the consecutive limit (counting the automatic turns since the owner last wrote), the daily wake-up limit and, at claim, the caps with their hold and chat line. The reply text is in the turn's prompt as untrusted data. Replies are tracked in `agent_message_origin` (the turn a message was sent from, and the once-only claim). Steered messages are answered by a message of the other bot, which reaches the asker as an inbox turn as before (hop and inbox limits).

## Loop guard fixes (0.11.12)

- A guard pause stays paused: status-change wake-ups, due wake-ups, reply wake-ups and a bot's out-of-turn `continue` all go through `decide` or are refused (409), so none resumes a task waiting on the owner. (Checked on 0.11.10; tests added.)
- The text checks read the bot's own prose only: fenced and inline code, quoted lines and double-quoted strings are ignored, and a sentence that says a failure is fixed ("Fixed the failed test") is not unresolved. A repeat or a "still failing" sentence still counts.
- A `bot-task status continue` report's note reaches the guard: the settled state and reason are passed to `decide`.

