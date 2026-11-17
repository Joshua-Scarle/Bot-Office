# Real-time steering plan

- Add nullable steer job, status and delivery timestamp columns to messages; retain the original message and its attachments when converting an undelivered steer to a queued job.
- Accept `mode` on message creation. Atomically attach a steer to a running job; otherwise queue normally. Reject edits of steer messages and mark them in history.
- Scheduler hands durable pending messages to the active adapter, records delivery and emits stream events. On completion, cancellation or restart recovery, convert any pending messages to queued turns.
- Claude keeps stream-json stdin open, sends NDJSON user messages and acknowledges echoed messages; close after result and a short grace. Other resumable CLIs interrupt on a completed step (or timeout), then resume the same session with batched steers. Do not interrupt if resume is unavailable; keep the whole job timeout and append invocation logs.
- Show a steer/queue choice while running and render delivery status live, preserving existing composer and chat features.
- Exercise injection, fallback, queue/recovery races, migration, edit restrictions and UI with fake providers; run the full suite and bump the release to 0.9.1.
