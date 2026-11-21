from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from . import attachments as attachment_files
from . import avatars, continuation, failures, sections
from . import history as chat_history
from . import usage as budgets
from .catalog import DEFAULT_ICON, DEFAULT_ICONS, default_effort, default_model
from .domain import Bot, Job, JobStatus, Message, Routine

BOT_DATES = ("created_at", "updated_at")
# 0.11.6: shown on a message that could not steer the running turn and became the next one
BUSY_WITH_BOT_NOTE = ("The bot is answering another bot, and a message to that turn would go to the other bot, "
                      "so yours is queued as the next turn.")
SLASH_BUSY_NOTE = ("The bot is running a slash command, which cannot take a message mid-run, "
                   "so yours is queued as the next turn.")
STEER_ENDED_NOTE = "It could not be delivered before the turn ended, so it runs as the next turn."
BOT_FIELDS = {
    "name",
    "instructions",
    "default_provider",
    "reason",
    "model",
    "icon",
    "icon_image",
    "avatar_seed",
    "description",
    "effort",
    "cli_access",
    "job_timeout",
    "notify",
    "allow_sudo",
    "scheduled_runs",
    "daily_budget_json",
    "digest",
    "bot_steer",
    "chat_only",
}
IDENTITY_MIGRATION = "crewdeck-identity-v1"
EFFORT_MIGRATION = "crewdeck-effort-v1"
READS_MIGRATION = "reads-seeded-v1"
AVATARS_MIGRATION = "avatars.default_assigned"
AVATAR_CHARS_MIGRATION = "avatars.characters_default"      # 0.12.12: generated patterns -> derived characters
AVATAR_GHOST_BACKUP = "avatars.ghost_backup"              # JSON {bot_id: [old "char|ghost|<color>" seeds, oldest first]}, to undo the droplet re-key
AVATAR_PATTERN_BACKUP = "avatars.pattern_backup"            # JSON {bot_id: old pattern seed}, to undo that migration
# 0.9.0: Grok bridge rate/hop state moved from JSON files into SQLite (imported once)
BRIDGE_IMPORT = "bridge_json_import"
TASKS_SEEN_INIT = "tasks_seen_init"  # meta key: the "changes by others" watermark was set (task lists)
# an assignment's priority as a sort key (urgent 0 .. low 3; anything else counts as normal)
PRIORITY_RANK = "(CASE {0}priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'low' THEN 3 ELSE 2 END)"
BRIDGE_JSON = ("grok_bridge_rate.json", "grok_bridge_threads.json")
# roles that count as unread: what the bot (or the system) said, never the prompts it received
# (0.11.10: plus the bot's own messages to other bots, see _UNREAD_WHERE)
UNREAD_ROLES = ("assistant", "system")
LEGACY_RENAMES = {("Cursor Bot", "cursor"): "Scout"}
# A rewound turn is seeded with the complete remaining chat. These bound that seed
# (text kept well below every provider's context window; images copied into the job).
SEED_MAX_CHARS = 400_000
MAX_MANUAL_RETRIES = 10  # per job (Retry button / API); a new message starts a new job
SEED_MAX_IMAGES = 24
SEED_MAX_IMAGE_BYTES = 64 * 1024 * 1024
EDITABLE_SOURCES = ("chat", "api", "assignment")  # assignment: the user's own replies (sender NULL)
SAY_MAX_CHARS = 500
SAY_INTERVAL_SECONDS = 10
SAY_MAX_PER_JOB = 10
BUDGET_USED = "its assignment's budget is used up"


class EditError(Exception):
    """An edit was rejected; ``status`` is the HTTP status, ``jobs`` blocking job ids."""

    def __init__(self, status: int, detail: str, jobs: list[str] | None = None):
        super().__init__(detail)
        self.status, self.detail, self.jobs = status, detail, jobs or []


@dataclass
class RewindSeed:
    messages: list[Message]
    images: list[tuple[int, str]] = dataclass_field(default_factory=list)  # (message id, attachment id)


def now() -> datetime:
    return datetime.now(UTC)


# job status -> agent message status (bot-msg / message-agent)
AGENT_STATUS = {"succeeded": "done", "failed": "failed", "cancelled": "failed", "needs_login": "failed",
                "incomplete": "not_done"}


# lower runs first; chat, edit and API turns (Joshua) are 0
STORED_ERROR_LIMIT = 2000
SOURCE_PRIORITY = {"inbox": 2, "assignment": 2, "wakeup": 2, "routine": 3, "trigger": 3}


class InboxLimit(ValueError):
    """An inbox or bot-to-bot turn over its hourly cap (per sender or for everyone)."""


def _inbox_limit(db: sqlite3.Connection, caps: tuple[int, int], *, sender: str | None = None,
                 from_bot: str | None = None) -> None:
    """Raise InboxLimit if another inbox turn would exceed *caps* = (per sender, all senders)
    in the last hour. A bot sender is counted by id; any other by its sender name (unnamed
    inbox files share one count)."""
    per_sender, overall = caps
    since = (now() - timedelta(hours=1)).isoformat()
    # 0.11.10: bot messages steered into a running turn count like inbox turns (until one becomes a turn)
    turns = db.execute("SELECT (SELECT count(*) FROM jobs WHERE source='inbox' AND created_at>=?) + "
                       "(SELECT count(*) FROM agent_messages WHERE job_id LIKE 'steer:%' AND created_at>=?)",
                       (since, since)).fetchone()[0]
    if turns >= overall:
        raise InboxLimit(f"inbox limit of {overall} turns per hour reached")
    if from_bot is not None:
        count = db.execute("SELECT count(*) FROM agent_messages WHERE from_bot=? AND created_at>=?",
                           (from_bot, since)).fetchone()[0]
    else:
        count = db.execute(
            "SELECT count(*) FROM messages m JOIN jobs j ON j.id=m.job_id WHERE m.role='user' "
            "AND m.source='inbox' AND j.source='inbox' AND COALESCE(m.sender,'')=? AND j.created_at>=? "
            "AND j.id NOT IN (SELECT job_id FROM agent_messages)", (sender or "", since)).fetchone()[0]
    if count >= per_sender:
        raise InboxLimit(f"inbox limit of {per_sender} turns per hour from this sender reached")


def _init_assignment_tables(db: sqlite3.Connection) -> None:
    """0.10.0: assignments, their event trail and self-scheduled wake-ups (all additive)."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS assignments (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
            done_when TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', due_at TEXT,
            budget_turns INTEGER NOT NULL DEFAULT 20, budget_minutes INTEGER NOT NULL DEFAULT 120,
            turns_used INTEGER NOT NULL DEFAULT 0, minutes_used REAL NOT NULL DEFAULT 0,
            verify INTEGER NOT NULL DEFAULT 0, escalate TEXT NOT NULL DEFAULT 'on_block',
            created_by TEXT NOT NULL DEFAULT 'user', parent_id TEXT, checkpoint TEXT NOT NULL DEFAULT '',
            status_note TEXT NOT NULL DEFAULT '', last_progress_at TEXT, stalled_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
            next_turn INTEGER NOT NULL DEFAULT 0, missing_status INTEGER NOT NULL DEFAULT 0,
            verifying INTEGER NOT NULL DEFAULT 0, dismissed_at TEXT,
            reported_status TEXT, reported_note TEXT, reported_job TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS assignments_bot ON assignments(bot_id, status)")
    db.execute("CREATE INDEX IF NOT EXISTS assignments_parent ON assignments(parent_id)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS assignment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL, kind TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '', job_id TEXT, created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS assignment_events_a ON assignment_events(assignment_id, id)")
    # 0.11.4 task lists: optimistic version, list order, where a task came from, who changed it last
    # (user | bot:<id> | system), a reserved reviewer (peer review of done is not built); every
    # event records its actor and the task's version. Additive: 0.11.3 ignores these columns.
    add_columns(db, "assignments", (
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("position", "REAL"),
        ("origin", "TEXT NOT NULL DEFAULT 'form'"),
        ("source_message_id", "INTEGER"),
        ("source_job_id", "TEXT"),
        ("updated_by", "TEXT NOT NULL DEFAULT 'user'"),
        ("reviewer", "TEXT"),
        # the text fields a bot edited (comma list of title, goal, done_when);
        # prompts delimit those as untrusted even on a task the user created
        ("bot_edited", "TEXT NOT NULL DEFAULT ''"),
        # urgent | high | normal | low; lists, prompts and the claim order put higher first
        ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
    ))
    db.execute("CREATE INDEX IF NOT EXISTS assignments_pick ON assignments(bot_id, status, position, created_at)")
    # the event actor and the "changes by others" watermark land in one transaction with a
    # durable marker, so a crash in between cannot replay old history as new changes
    db.execute("BEGIN IMMEDIATE")
    try:
        add_columns(db, "assignment_events", (("actor", "TEXT NOT NULL DEFAULT 'system'"), ("version", "INTEGER")))
        if not db.execute("SELECT 1 FROM meta WHERE key=?", (TASKS_SEEN_INIT,)).fetchone():
            # first start with task lists: older events are history, not "changes since your last turn"
            db.execute("UPDATE bots SET tasks_seen_event=(SELECT COALESCE(MAX(id), 0) FROM assignment_events)")
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (TASKS_SEEN_INIT, now().isoformat()))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    # the per-actor write limit (BOT_OFFICE_TASK_WRITES_PER_HOUR) counts these
    db.execute("CREATE INDEX IF NOT EXISTS assignment_events_actor ON assignment_events(actor, created_at)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS wakeups (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, assignment_id TEXT, due_at TEXT, on_assignment TEXT,
            note TEXT NOT NULL DEFAULT '', created_by_job TEXT, status TEXT NOT NULL DEFAULT 'pending',
            job_id TEXT, created_at TEXT NOT NULL, fired_at TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS wakeups_pending ON wakeups(status, due_at)")
    # one job per (assignment, turn number): a replayed enqueue can never add a second turn
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_assignment_turn ON jobs(assignment_id, assignment_turn) "
               "WHERE assignment_id IS NOT NULL")


def _init_control_tables(db: sqlite3.Connection) -> None:
    """0.11.0: action rules, approvals and the gate's log of refused actions (all additive)."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS action_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, effect TEXT NOT NULL, kind TEXT NOT NULL,
            pattern TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'all', note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, job_id TEXT NOT NULL,
            assignment_id TEXT, tool TEXT NOT NULL, summary TEXT NOT NULL, input_hash TEXT NOT NULL,
            scope TEXT NOT NULL, source TEXT NOT NULL, preset TEXT, status TEXT NOT NULL DEFAULT 'pending',
            expires_at TEXT, decided_at TEXT, grant_expires_at TEXT, resume_job_id TEXT,
            consumed_by TEXT, consumed_at TEXT, created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS approvals_grant ON approvals(bot_id, input_hash, status)")
    # 0.12.4 fix3: the exact updates (repo, ref, operation, old/new sha) a push approval covers
    add_columns(db, "approvals", (("push_plan", "TEXT"),))
    db.execute(
        """CREATE TABLE IF NOT EXISTS gate_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, job_id TEXT, provider TEXT, decision TEXT NOT NULL,
            effect TEXT NOT NULL, preset TEXT, tool TEXT, summary TEXT NOT NULL, approval_id INTEGER,
            created_at TEXT NOT NULL, dismissed_at TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS gate_log_time ON gate_log(created_at)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS watchers (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}', secret_hash TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, debounce_s INTEGER NOT NULL DEFAULT 60,
            max_per_hour INTEGER NOT NULL DEFAULT 6, assignment_id TEXT, last_state TEXT,
            last_checked_at TEXT, last_fired_at TEXT, paused_reason TEXT, created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS watcher_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, watcher_id TEXT NOT NULL, outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', job_id TEXT, created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS watcher_runs_w ON watcher_runs(watcher_id, id)")
    # 0.11.0 (#10): one "parked on a usage limit" notice per job and attempt (needs_you.post_usage_notices)
    db.execute(
        """CREATE TABLE IF NOT EXISTS usage_notices (
            job_id TEXT NOT NULL, attempt INTEGER NOT NULL, bot_id TEXT NOT NULL, message_id INTEGER,
            provider TEXT NOT NULL, back_at TEXT, created_at TEXT NOT NULL, dismissed_at TEXT,
            PRIMARY KEY (job_id, attempt)
        )"""
    )


def _init_notify_triggers(db: sqlite3.Connection) -> None:
    """Needs-you transitions are logged in notify_events by the write that causes them, so a state that
    appears and clears between two pushes is still on record. Keys are raw and opaque (they may hold any
    character); notify.py hashes them before anything leaves. Overdue assignments are time-driven (no
    write happens) and are logged by notify.record_needs_you instead."""
    stamp = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    insert = "INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at)"
    # the sequence number this very insert will get: never reused (AUTOINCREMENT), so two incidents of the
    # same row can never share a key, whatever the clock says
    nxt = "(SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='notify_events'),0)+1)"
    # CREATE ... IF NOT EXISTS keeps an older body, so the ones whose keys changed are replaced
    for name in ("notify_asg_dropped", "notify_watcher_paused", "notify_held_new", "notify_held_kind"):
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    state = ("CASE WHEN NEW.stalled_at IS NOT NULL THEN 'stalled' WHEN NEW.status='waiting_on_user' THEN 'waiting_on_user' "
             "WHEN NEW.status='blocked' AND NEW.escalate IS NOT 'never' THEN 'blocked' "
             "WHEN NEW.status='waiting_on_bot' AND NEW.escalate IS 'always' THEN 'waiting_on_bot' END")
    asg = (f"{insert} SELECT 'a:' || NEW.id || ':' || {state} || ':' || COALESCE(NEW.stalled_at, NEW.updated_at), "
           f"'needs_you', NEW.bot_id, NULL, 'assignment_' || {state}, {stamp} "
           f"WHERE NEW.status NOT IN ('done','dropped') AND NEW.dismissed_at IS NULL AND {state} IS NOT NULL;")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS notify_asg_new AFTER INSERT ON assignments BEGIN {asg} END")
    db.execute("CREATE TRIGGER IF NOT EXISTS notify_asg_state AFTER UPDATE OF status, stalled_at, escalate ON assignments "
               "WHEN OLD.status IS NOT NEW.status OR OLD.stalled_at IS NOT NEW.stalled_at "
               f"OR OLD.escalate IS NOT NEW.escalate BEGIN {asg} END")
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_asg_dropped AFTER UPDATE OF status, updated_by ON assignments
            WHEN NEW.status='dropped' AND NEW.created_by='user' AND NEW.updated_by LIKE 'bot:%'
                 AND (OLD.status IS NOT NEW.status OR OLD.updated_by IS NOT NEW.updated_by)
            BEGIN {insert} VALUES ('a:' || NEW.id || ':dropped_by_bot:' || {nxt}, 'needs_you', NEW.bot_id, NULL,
                                   'assignment_dropped_by_bot', {stamp}); END""")
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_approval_new AFTER INSERT ON approvals WHEN NEW.status='pending'
            BEGIN {insert} VALUES ('p:' || NEW.id || ':pending', 'needs_you', NEW.bot_id, NEW.job_id,
                                   'approval_pending', {stamp}); END""")
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_approval_expired AFTER UPDATE OF status ON approvals
            WHEN NEW.status='expired' AND OLD.status IS NOT 'expired' AND NEW.resume_job_id IS NULL
            BEGIN {insert} VALUES ('p:' || NEW.id || ':expired', 'needs_you', NEW.bot_id, NEW.job_id,
                                   'approval_expired', {stamp}); END""")
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_tripwire AFTER INSERT ON gate_log
            WHEN NEW.preset='secrets' AND NEW.effect='never' AND NEW.bot_id IS NOT NULL
            BEGIN {insert} VALUES ('g:' || NEW.id, 'needs_you', NEW.bot_id, NEW.job_id, 'alert_tripwire', {stamp}); END""")
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_usage_parked AFTER INSERT ON usage_notices
            BEGIN {insert} VALUES ('u:' || NEW.job_id || ':' || NEW.attempt, 'needs_you', NEW.bot_id, NEW.job_id,
                                   'usage_limit_parked', {stamp}); END""")
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_watcher_paused AFTER UPDATE OF enabled, paused_reason ON watchers
            WHEN NEW.enabled=0 AND NEW.paused_reason IS NOT NULL
                 AND (OLD.enabled IS NOT 0 OR OLD.paused_reason IS NOT NEW.paused_reason)
            BEGIN {insert} VALUES ('w:' || NEW.id || ':' || {nxt}, 'needs_you',
                                   NEW.bot_id, NULL, 'watcher_paused', {stamp}); END""")
    # a hold is a transition into queued+budget/auto_cap (a new job, or a job whose hold kind or status changed);
    # each one is its own incident (job id and time in the key), a rewrite with the same values is not
    held = ("NEW.status='queued' AND NEW.failure_kind IN ('budget','auto_cap') AND NEW.bot_id IS NOT NULL")
    body = (f"BEGIN {insert} VALUES ('h:' || NEW.id || ':' || NEW.failure_kind || ':' || {nxt}, "
            f"'needs_you', NEW.bot_id, NULL, NEW.failure_kind || '_held', {stamp}); END")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS notify_held_new AFTER INSERT ON jobs WHEN {held} {body}")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS notify_held_kind AFTER UPDATE OF failure_kind, status ON jobs "
               f"WHEN {held} AND (OLD.status IS NOT 'queued' OR OLD.failure_kind IS NOT NEW.failure_kind) {body}")
    # an assignment that became overdue and closes before any poll saw it: the close is the last chance to log it
    db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS notify_asg_overdue_close AFTER UPDATE OF status ON assignments
            WHEN NEW.status IN ('done','dropped') AND OLD.status NOT IN ('done','dropped') AND NEW.due_at IS NOT NULL
                 AND NEW.dismissed_at IS NULL AND julianday(NEW.due_at) < julianday('now')
            BEGIN {insert} VALUES ('a:' || NEW.id || ':overdue:' || NEW.due_at, 'needs_you', NEW.bot_id, NULL,
                                   'assignment_overdue', {stamp}); END""")
    _backfill_notify(db)


def _backfill_notify(db: sqlite3.Connection) -> None:
    """Needs-you states already open are logged once per type (marker notify-backfill-<type>), so a database
    initialised by an earlier revision still runs the types it lacks. Same keys as the triggers, so a repeat
    adds nothing; the counter-keyed types skip a row that already has any event of its own."""
    stamp = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    insert = "INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at)"
    state = ("CASE WHEN stalled_at IS NOT NULL THEN 'stalled' WHEN status='waiting_on_user' THEN 'waiting_on_user' "
             "WHEN status='blocked' AND escalate IS NOT 'never' THEN 'blocked' "
             "WHEN status='waiting_on_bot' AND escalate IS 'always' THEN 'waiting_on_bot' END")
    # the same windows and filters as needs_you.py (FAILED_WINDOW, actionable failures, no later success)
    # (store cannot import needs_you; keep these in step with needs_you.FAILED_WINDOW / ACTION_KINDS / UNATTENDED)
    since = "strftime('%Y-%m-%dT%H:%M:%S','now','-72 hours')"
    kinds = "'auth','usage_limit','gate','budget'"
    sources = "'routine','wakeup','inbox','api','trigger'"

    def unlogged(prefix: str) -> str:  # no event yet for this row (counter-keyed triggers)
        return f"AND NOT EXISTS (SELECT 1 FROM notify_events e WHERE e.key LIKE {prefix} || '%')"

    steps = {
        "assignments": f"{insert} SELECT 'a:' || id || ':' || {state} || ':' || COALESCE(stalled_at, updated_at), 'needs_you', "
                       f"bot_id, NULL, 'assignment_' || {state}, {stamp} FROM assignments "
                       f"WHERE status NOT IN ('done','dropped') AND dismissed_at IS NULL AND {state} IS NOT NULL",
        "approvals": f"{insert} SELECT 'p:' || id || ':pending', 'needs_you', bot_id, job_id, 'approval_pending', {stamp} "
                     "FROM approvals WHERE status='pending'",
        "questions": f"{insert} SELECT 'q:' || id || ':open', 'needs_you', bot_id, NULL, 'question_open', {stamp} "
                     "FROM questions WHERE status='open'",
        "drops": f"{insert} SELECT 'a:' || id || ':dropped_by_bot:open', 'needs_you', bot_id, NULL, 'assignment_dropped_by_bot', {stamp} "
                 "FROM assignments WHERE status='dropped' AND created_by='user' AND updated_by LIKE 'bot:%' AND dismissed_at IS NULL "
                 + unlogged("'a:' || assignments.id || ':dropped_by_bot:'"),
        "holds": f"{insert} SELECT 'h:' || id || ':' || failure_kind || ':open', 'needs_you', bot_id, NULL, failure_kind || '_held', {stamp} "
                 "FROM jobs WHERE status='queued' AND failure_kind IN ('budget','auto_cap') AND bot_id IS NOT NULL "
                 + unlogged("'h:' || jobs.id || ':' || jobs.failure_kind || ':'"),
        "watchers": f"{insert} SELECT 'w:' || id || ':open', 'needs_you', bot_id, NULL, 'watcher_paused', {stamp} "
                    "FROM watchers WHERE enabled=0 AND paused_reason IS NOT NULL " + unlogged("'w:' || watchers.id || ':'"),
        "usage": f"{insert} SELECT 'u:' || job_id || ':' || attempt, 'needs_you', bot_id, job_id, 'usage_limit_parked', {stamp} "
                 "FROM usage_notices WHERE dismissed_at IS NULL",
        "failed": f"{insert} SELECT 'j:' || j.id || ':' || j.attempts, 'failed', j.bot_id, j.id, COALESCE(j.failure_kind, j.status), {stamp} "
                  f"FROM jobs j WHERE j.status IN ('failed','needs_login') AND j.finished_at>={since} "
                  "AND j.dismissed_at IS NULL AND j.bot_id IS NOT NULL "
                  f"AND (j.status='needs_login' OR j.failure_kind IN ({kinds}) OR (j.assignment_id IS NULL AND j.source IN ({sources}))) "
                  "AND NOT EXISTS (SELECT 1 FROM jobs k WHERE k.bot_id=j.bot_id AND k.status='succeeded' AND k.finished_at>j.finished_at)",
        "expired": f"{insert} SELECT 'p:' || id || ':expired', 'needs_you', bot_id, job_id, 'approval_expired', {stamp} "
                   f"FROM approvals WHERE status='expired' AND resume_job_id IS NULL AND created_at>={since}",
        "tripwires": f"{insert} SELECT 'g:' || id, 'needs_you', bot_id, job_id, 'alert_tripwire', {stamp} "
                     f"FROM gate_log WHERE preset='secrets' AND effect='never' AND dismissed_at IS NULL AND bot_id IS NOT NULL AND created_at>={since}",
    }
    for name, sql in steps.items():
        marker = f"notify-backfill-{name}"
        if db.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
            continue
        db.execute(sql)
        db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, '1')", (marker,))


class BridgeHopConflict(Exception):
    """An inbound bridge thread's hop moved between reading it and queueing the turn."""


class RoutineChanged(Exception):
    """The routine was edited, disabled, deleted or scanned after it was read; the scan is dropped."""


def _priority(source: str | None) -> int:
    return SOURCE_PRIORITY.get(source or "", 0)


# a job parked on a usage limit (#10): queued, waiting for its retry time; *j* is the jobs row
PARKED_SQL = "j.status='queued' AND j.failure_kind='usage_limit' AND j.not_before IS NOT NULL"


def clip_error(error: str | None) -> str | None:
    """Failure text as stored (jobs.error, job_attempts): secrets redacted, bounded."""
    return failures.clip(error, STORED_ERROR_LIMIT) if error else error


def execution(row: Mapping[str, Any]) -> int:
    """Execution number of a job row's current (or next) run: every retry and every restart
    replay is a new execution, so events and attempt history never mix two runs."""
    return int(row["attempts"] or 0) + int(row["restart_attempts"] or 0)


def _minutes(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)


def _run_minutes(db: sqlite3.Connection, job: Mapping[str, Any]) -> tuple[float, bool]:
    """Minutes of a job's finished runs, and whether its latest run happened."""
    runs = db.execute("SELECT started_at, finished_at FROM job_attempts WHERE job_id=? AND started_at IS NOT NULL "
                      "ORDER BY id", (job["id"],)).fetchall()
    total = sum(_minutes(a["started_at"], a["finished_at"]) for a in runs)
    # a cancelled queued job never ran its latest run (its started_at belongs to a recorded attempt)
    last_started = runs[-1]["started_at"] if runs else None
    ran = (job["status"] not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value) and bool(job["started_at"])
           and job["started_at"] != last_started)
    if ran:
        total += _minutes(job["started_at"], job["finished_at"] or now().isoformat())
    return total, ran


def charge_assignment_minutes(db: sqlite3.Connection, job: Mapping[str, Any]) -> tuple[float, bool]:
    """Charge the minutes of an assignment job's finished runs that were not charged yet.

    Every run counts once: earlier runs (retries, restart replays) are in job_attempts, the
    latest one counts when the job has finished. Returns (minutes charged now, whether the
    latest run happened). Runs inside the caller's transaction.
    """
    total, ran = _run_minutes(db, job)
    delta = total - (job["assignment_minutes"] or 0)
    if job["assignment_id"] and delta > 0:
        db.execute("UPDATE assignments SET minutes_used=minutes_used+? WHERE id=?", (delta, job["assignment_id"]))
        db.execute("UPDATE jobs SET assignment_minutes=? WHERE id=?", (total, job["id"]))
    return max(delta, 0.0), ran


def assignment_event(db: sqlite3.Connection, assignment_id: str, kind: str, note: str = "",
                     job_id: str | None = None, *, actor: str = "system") -> None:
    """An assignment_events row with its actor (user | bot:<id> | system) and the task's
    version at that moment (after the change, when the caller bumped it)."""
    db.execute("INSERT INTO assignment_events (assignment_id,kind,note,job_id,created_at,actor,version) "
               "VALUES (?,?,?,?,?,?,(SELECT version FROM assignments WHERE id=?))",
               (assignment_id, kind, note[:2000], job_id, now().isoformat(), actor, assignment_id))


def _drop_stale_hold(db: sqlite3.Connection, job_id: str, why: str) -> None:
    """A queued automatic turn that should no longer run (continuation.stale_hold) is cancelled with a
    chat line; an active task it belonged to waits for the owner, so it does not stall silently."""
    stamp = now().isoformat()
    row = db.execute("SELECT bot_id, source, assignment_id, failure_kind FROM jobs WHERE id=?", (job_id,)).fetchone()
    held = "A held automatic turn" if row["failure_kind"] == continuation.HELD else "An automatic turn"
    db.execute("UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
               (JobStatus.CANCELLED.value, stamp, f"not run: {why}", job_id))
    if row["bot_id"] and db.execute("SELECT 1 FROM bots WHERE id=?", (row["bot_id"],)).fetchone():
        db.execute("INSERT INTO messages (bot_id,role,source,text,job_id,created_at,attachments) VALUES (?,?,?,?,?,?,'[]')",
                   (row["bot_id"], "system", row["source"],
                    f"{held} was not run: {why}. Reply to keep it going.", job_id, stamp))
    if row["assignment_id"] and not db.execute("SELECT 1 FROM jobs WHERE assignment_id=? AND status IN "
                                               "('queued','running') LIMIT 1", (row["assignment_id"],)).fetchone():
        note = f"paused, not blocked: {why}. Reply to resume."
        if db.execute("UPDATE assignments SET status='waiting_on_user', status_note=?, updated_at=?, dismissed_at=NULL, "
                      "version=version+1, updated_by='system' WHERE id=? AND status='active'",
                      (note, stamp, row["assignment_id"])).rowcount:
            assignment_event(db, row["assignment_id"], "status", f"waiting_on_user: {note}", job_id)


def _assignment_gate(db: sqlite3.Connection, job_id: str) -> str | None:
    """Claim-time check of an assignment turn (every run: continuation, verify, retry, reply,
    wake-up, restart replay): its assignment must be active with budget left. Reserves one turn
    of the budget in the claim's transaction; returns why the run may not start, else None."""
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job["assignment_id"]:
        return None
    charge_assignment_minutes(db, job)  # earlier runs of a retried job count before the check
    row = db.execute("SELECT * FROM assignments WHERE id=?", (job["assignment_id"],)).fetchone()
    if row is None:
        return "its assignment is gone"
    if row["status"] != "active":
        return "its assignment was stopped" if row["status"] == "open" else f"its assignment is {row['status']}"
    if row["turns_used"] >= row["budget_turns"] or row["minutes_used"] >= row["budget_minutes"]:
        return BUDGET_USED
    db.execute("UPDATE assignments SET turns_used=turns_used+1, updated_at=? WHERE id=?", (now().isoformat(), row["id"]))
    return None


def _hold_for_budget(db: sqlite3.Connection, job_id: str, why: str) -> None:
    """Over the bot's daily budget: the unattended job waits until 00:00 New York time (a
    Needs-you item offers "Allow +N today"), with one chat line per held job."""
    row = db.execute("SELECT bot_id, source, failure_kind FROM jobs WHERE id=?", (job_id,)).fetchone()
    db.execute("UPDATE jobs SET not_before=?, failure_kind=?, error=? WHERE id=?",
               (budgets.next_day(now()).isoformat(), failures.BUDGET, f"held: daily budget used ({why})", job_id))
    if row["failure_kind"] != failures.BUDGET and db.execute("SELECT 1 FROM bots WHERE id=?", (row["bot_id"],)).fetchone():
        db.execute("INSERT INTO messages (bot_id,role,source,text,job_id,created_at,attachments) VALUES (?,?,?,?,?,?,'[]')",
                   (row["bot_id"], "system", row["source"],
                    f"A {row['source']} turn waits until tomorrow: daily budget used ({why}).", job_id, now().isoformat()))


def _run_row(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """A job as one timeline entry, with its earlier attempts and their usage."""
    attempts = [{"attempt": item["attempt"], "kind": item["kind"], "started_at": item["started_at"],
                 "finished_at": item["finished_at"], "usage": json.loads(item["usage_json"]) if item["usage_json"] else None}
                for item in db.execute("SELECT * FROM job_attempts WHERE job_id=? ORDER BY id", (row["id"],))]
    return {"job_id": row["id"], "source": row["source"], "status": row["status"], "outcome": row["outcome"],
            "failure_kind": row["failure_kind"], "created_at": row["created_at"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
            "error": row["error"], "attempts": attempts}


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _minute(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


def add_columns(db: sqlite3.Connection, table: str, specs: Sequence[tuple[str, str]]) -> set[str]:
    """Add the columns of *specs* (name, DDL) that *table* lacks, checked and added under one write lock
    (the caller's transaction, else BEGIN IMMEDIATE), so two processes starting at once cannot both add
    one; a duplicate column is skipped all the same. Returns the columns this call added."""
    own = not db.in_transaction
    if own:
        db.execute("BEGIN IMMEDIATE")
    added: set[str] = set()
    try:
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, ddl in specs:
            if column in have:
                continue
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
                continue
            added.add(column)
        if own:
            db.commit()
    except BaseException:
        if own:
            db.rollback()
        raise
    return added


LINK_COLUMNS = ("steered_into", "reply_to", "sender_message_id", "target_message_id")


def _unwiden_agent_messages(db: sqlite3.Connection) -> None:
    """A database first opened by a 0.11.10 pre-release has the link columns on agent_messages
    itself: move them to agent_message_links and drop them, so 0.11.9's positional INSERT works."""
    db.execute("BEGIN IMMEDIATE")  # checked under the write lock: another process may have just done it
    try:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_messages)")}
        present = [column for column in LINK_COLUMNS if column in columns]
        if not present:
            db.rollback()
            return
        picks = ", ".join(column if column in present else "NULL" for column in LINK_COLUMNS)
        db.execute(f"INSERT OR IGNORE INTO agent_message_links (message_id, {', '.join(LINK_COLUMNS)}) "
                   f"SELECT id, {picks} FROM agent_messages")
        for index in ("agent_messages_steered", "agent_messages_sent", "agent_messages_received"):
            db.execute(f"DROP INDEX IF EXISTS {index}")
        db.execute("DROP VIEW IF EXISTS agent_messages_x")
        for column in present:
            db.execute(f"ALTER TABLE agent_messages DROP COLUMN {column}")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def message_author(values: dict) -> tuple[str, str | None]:
    """(author, peer) of a messages row; see Message.author. ``agent_from`` is the sending bot of the
    row's job when that job is a bot-to-bot message (agent_messages.from_bot), else None."""
    role, sender, source = values.get("role"), values.get("sender"), values.get("source")
    if role == "system":
        return "system", None
    if role == "assistant":
        if source == "inbox" and sender and sender.startswith("from "):
            return "bot_reply", sender[5:]
        return "bot", None
    if sender is None and source in EDITABLE_SOURCES:
        return "owner", None
    if source == "assignment" and sender and sender.startswith(("to ", "bot ")):
        # a hand-off between bots (assignments.Assignments.create): only code sets these senders
        return ("bot_out", sender[3:]) if sender.startswith("to ") else ("bot_in", sender[4:])
    if source == "inbox" and sender:
        if values.get("agent_from") and values["agent_from"] == str(values.get("bot_id")):
            return "bot_out", sender[3:] if sender.startswith("to ") else sender
        return "bot_in", sender[4:] if sender.startswith("bot ") else sender
    return "office", None


PRESET_LABELS = {"outbound": "Outbound action in an unattended run", "destructive":
                 "Destructive action in an unattended run", None: "An action rule you set",
                 "git-guard": "Protected action: default branch, merge, force-push, delete or repo settings"}


def approval_card(store: JobStore, item: dict) -> dict:
    """0.11.10: what the chat's Approve / Deny card shows for one approval."""
    from . import approval_classes
    label = approval_classes.label(store, item["preset"]) or PRESET_LABELS.get(item["preset"]) or item["preset"]
    return {"id": item["id"], "status": item["status"], "label": label, "summary": item["summary"],
            "tool": item["tool"], "scope": item["scope"], "expires_at": item["expires_at"],
            "decided_at": item["decided_at"], "resumed": bool(item["resume_job_id"])}


def _model(cls, row: sqlite3.Row | None, dates: tuple[str, ...]):
    if row is None:
        return None
    values = dict(row)
    if cls is Message:
        values["author"], values["peer"] = message_author(values)
    for field in dates:
        if values[field]:
            values[field] = datetime.fromisoformat(values[field])
    if cls is Message and "attachments" in values:
        values["attachments"] = json.loads(values["attachments"])
    return cls.model_validate(values)


class JobStore:
    def __init__(self, path: Path, initialize: bool = True):
        self.path = Path(path)
        self._initialized = False
        self.sections = sections.Sections(self._connect)
        # The one lock for chat mutations that must not interleave with an edit: the edit
        # commit and outcome publication (chat message + memory write). Every other
        # enqueue/claim path is a single BEGIN IMMEDIATE transaction, i.e. serialized by
        # SQLite's database-wide write lock. There is no per-bot lock, so there is no lock
        # order to get wrong; neither lock is ever held while waiting for a job to stop.
        self.chat_lock = threading.RLock()
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._initialized:
            self._initialized = True
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if not self._initialized:
            self.initialize()
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _init_db(self) -> None:
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
                    slot TEXT, provider TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, exit_code INTEGER,
                    log_path TEXT, error TEXT
                )"""
            )
            added_jobs = add_columns(db, "jobs", (
                ("bot_id", "TEXT"),
                ("reason", "TEXT"),
                ("source", "TEXT NOT NULL DEFAULT 'api'"),
                ("reply_path", "TEXT"),
                ("routine_id", "TEXT"),
                ("model", "TEXT"),
                ("effort", "TEXT"),
                ("reply_text", "TEXT"),
                ("draft_text", "TEXT"),
                ("agent_token_hash", "TEXT"),
                # 0.9.0: failures.classify kind of the last failed attempt; reported token usage
                ("failure_kind", "TEXT"),
                ("usage_json", "TEXT"),
                # provider attempts so far (same row is re-queued; job_id never changes) and
                # the earliest time a re-queued attempt may start
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("not_before", "TEXT"),
                # controller restarts that re-queued this job (at most 1), separate from attempts
                ("restart_attempts", "INTEGER NOT NULL DEFAULT 0"),
                # what the bot reported (BOT-OFFICE-STATUS line): done | unverified | not_done: reason …
                ("outcome", "TEXT"),
                # 0.12.16: a done turn with evidence of how it was checked: 1 (text in evidence), 0 (a bare
                # done), NULL (not a done, or recorded before 0.12.16)
                ("verified", "INTEGER"),
                ("evidence", "TEXT"),
                # claim order: Joshua's chat/API turns first, then bot-to-bot, then routines
                ("priority", "INTEGER NOT NULL DEFAULT 0"),
                # 0.10.0: the assignment this turn advances, its turn number there, whether the
                # loop has settled it (budget charged, next step decided), and Needs-you dismissal
                ("assignment_id", "TEXT"),
                ("assignment_turn", "INTEGER"),
                ("assignment_settled", "INTEGER NOT NULL DEFAULT 0"),
                # minutes of this job's runs already charged to its assignment (each run once)
                ("assignment_minutes", "REAL NOT NULL DEFAULT 0"),
                ("dismissed_at", "TEXT"),
                # 0.11.0: the job whose approval this turn resumes (one-shot grants bind to it)
                ("origin_job_id", "TEXT"),
                # 0.11.0: the watcher that started this turn (source trigger)
                ("watcher_id", "TEXT"),
                # 0.11.10: automatic continuation number (0 = not one) and the turn the chain started from
                ("continue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("continued_from", "TEXT"),
                # 0.11.10 one continuation system: which kind of automatic turn this job is (chat,
                # loop, verify, evidence, wakeup; NULL = not automatic, counted by the caps) and what
                # continuation.decide said after it finished (set once)
                ("auto_kind", "TEXT"),
                ("auto_decision", "TEXT"),
                # when a cap first held this automatic turn (continuation.stale_hold)
                ("held_at", "TEXT"),
                # the failure_kind the job had before a cap held it (restored when it runs)
                ("held_kind", "TEXT"),
                # 0.11.14: slash command turn (JSON name/args/mode), see slash.py
                ("slash", "TEXT"),
                # 0.12.9: when the job first started; started_at is rewritten by every retry or restart
                # re-run, so created_at -> started_at also counted the retry backoff
                ("first_started_at", "TEXT"),
            ))
            if "priority" in added_jobs:
                # jobs queued before 0.9.0 keep their place behind Joshua's turns
                db.execute("UPDATE jobs SET priority=CASE source WHEN 'inbox' THEN ? WHEN 'routine' THEN ? ELSE 0 END",
                           (SOURCE_PRIORITY["inbox"], SOURCE_PRIORITY["routine"]))
            db.execute(
                """CREATE TABLE IF NOT EXISTS bots (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, instructions TEXT NOT NULL,
                    default_provider TEXT NOT NULL, reason TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            add_columns(db, "bots", (
                ("model", "TEXT"),
                ("icon", "TEXT NOT NULL DEFAULT ''"),
                ("icon_image", "TEXT"),
                # 0.12.0: seed of the generated avatar (NULL: none, the emoji shows); an uploaded image wins
                ("avatar_seed", "TEXT"),
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("effort", "TEXT"),
                ("cli_access", "TEXT NOT NULL DEFAULT 'full'"),
                # id of the replacement message of an edit whose provider-session reset
                # (saved <engine>-session pointers) is not yet reconciled; NULL when nothing is pending
                ("session_reset", "INTEGER"),
                # WORKING.md to restore with that reset (its state before the first archived
                # turn that changed it); NULL when it needs no change
                ("working_restore", "TEXT"),
                # id of the replacement message of an edit whose full-chat seed has not yet
                # reached a provider turn that actually ran; NULL when nothing is pending
                ("rewind_seed", "INTEGER"),
                # 0.10.0: per-bot turn timeout in seconds (NULL: service default); browser notifications
                ("job_timeout", "INTEGER"),
                ("notify", "INTEGER NOT NULL DEFAULT 1"),
                # 0.11.0: run this bot's CLI without no_new_privs (sudo works; Joshua only)
                ("allow_sudo", "INTEGER NOT NULL DEFAULT 0"),
                # 0.11.0: full (0.10.x behaviour) | gated (strict profile for scheduled turns)
                ("scheduled_runs", "TEXT NOT NULL DEFAULT 'full'"),
                # 0.11.0: daily unattended budget (NULL: defaults), today's "Allow +N", digest opt-in
                ("daily_budget_json", "TEXT"),
                ("budget_extra_json", "TEXT"),
                ("digest", "INTEGER NOT NULL DEFAULT 0"),
                # 0.11.10: messages from other bots may steer this bot's running turn
                ("bot_steer", "INTEGER NOT NULL DEFAULT 1"),
                # 0.11.4: the last assignment_events id this bot's prompt showed ("changes by others")
                ("tasks_seen_event", "INTEGER NOT NULL DEFAULT 0"),
                # 0.11.10: a chat-only bot never continues by itself (no loop, verify or wake-up turns)
                ("chat_only", "INTEGER NOT NULL DEFAULT 0"),
            ))
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            # email-notification outbox (notify.py): appended by these triggers and by notify's detector,
            # acknowledged by the Worker through a cursor; holds codes and ids only, never text
            db.execute(
                """CREATE TABLE IF NOT EXISTS notify_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, cls TEXT NOT NULL,
                    bot_id TEXT, ref TEXT, kind TEXT NOT NULL, created_at TEXT NOT NULL)""")
            for name, status, cls, prefix in (("failed", "'failed','needs_login'", "failed", "j"),
                                              ("done", "'succeeded'", "done", "d")):
                db.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS notify_job_{name} AFTER UPDATE OF status ON jobs
                        WHEN NEW.status IN ({status}) AND OLD.status IS NOT NEW.status AND NEW.bot_id IS NOT NULL
                        BEGIN INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at)
                        VALUES ('{prefix}:' || NEW.id || ':' || NEW.attempts, '{cls}', NEW.bot_id, NEW.id,
                                COALESCE(NEW.failure_kind, NEW.status), strftime('%Y-%m-%dT%H:%M:%SZ','now')); END""")
            # WORKING.md as it was before each turn that changed it, so an edit can roll it back
            db.execute(
                """CREATE TABLE IF NOT EXISTS working_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    before TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS working_snapshots_bot ON working_snapshots(bot_id, job_id)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS bot_aliases (alias TEXT PRIMARY KEY, bot_id TEXT NOT NULL)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL,
                    role TEXT NOT NULL, sender TEXT, source TEXT NOT NULL, text TEXT NOT NULL,
                    job_id TEXT, created_at TEXT NOT NULL
                )"""
            )
            add_columns(db, "messages", (
                ("attachments", "TEXT NOT NULL DEFAULT '[]'"),
                ("kind", "TEXT NOT NULL DEFAULT 'message'"),
                # message edit / rewind: soft archive, never deleted
                ("archived_at", "TEXT"),
                ("archived_by", "INTEGER"),
                ("edit_of", "INTEGER"),
                ("steer_job_id", "TEXT"),
                ("steer_status", "TEXT"),
                ("steer_delivered_at", "TEXT"),
                ("steer_reason", "TEXT"),  # the API reason, used if the steer becomes a queued turn
                ("steer_note", "TEXT"),  # 0.11.6: why a steer became a queued turn (shown in the chat)
                # 0.11.10: Bot-Office machinery (automatic continuations, assignment "Continue" steps,
                # wake-up prompts, "Not done (continuing)" lines): kept, but not shown in the chat
                ("hidden", "INTEGER NOT NULL DEFAULT 0"),
                # 0.12.16: characters of the reply left out of this message (the job keeps the full text)
                ("cut_chars", "INTEGER NOT NULL DEFAULT 0"),
                # 0.11.10: the chat line that carries an approval's Approve / Deny card
                ("approval_id", "INTEGER"),
            ))
            db.execute("CREATE INDEX IF NOT EXISTS messages_bot ON messages (bot_id, id)")
            db.execute(
                """CREATE TABLE IF NOT EXISTS routines (
                    id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, cron TEXT NOT NULL,
                    prompt TEXT NOT NULL, enabled INTEGER NOT NULL, last_run_at TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            add_columns(db, "routines", (
                ("name", "TEXT"),
                # 0.9.0: what to do with runs missed while the controller was down (once | skip)
                ("catch_up", "TEXT NOT NULL DEFAULT 'once'"),
                # last minute (UTC) already scanned for due runs; NULL = start scanning now
                ("scan_cursor", "TEXT"),
                # bumped by every edit, so a scan never acts on a routine changed meanwhile
                ("version", "INTEGER NOT NULL DEFAULT 0"),
                # latest run that came due while the previous run was still going (catch-up owed)
                ("pending_run", "TEXT"),
                # 0.10.0: "bot" when the bot made it through its job token (limited per bot)
                ("created_by", "TEXT NOT NULL DEFAULT 'user'"),
            ))
            # Audit trail of every stream event, kept after a bot is deleted.
            db.execute(
                """CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, seq)
                )"""
            )

            db.execute("""CREATE TABLE IF NOT EXISTS agent_messages (
                id TEXT PRIMARY KEY, from_bot TEXT NOT NULL, to_bot TEXT NOT NULL,
                body TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL,
                hop INTEGER NOT NULL, created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS agent_messages_pair ON agent_messages (from_bot, to_bot, created_at)")
            # 0.11.10: a message steered into the receiver's running job (job_id is then "steer:<id>"
            # until it becomes a turn of its own), the steer it answers, and its two chat rows. A side
            # table, not new columns: 0.11.9 inserts into agent_messages positionally (8 values), so the
            # rollback build must still find exactly 8 columns there.
            db.execute("""CREATE TABLE IF NOT EXISTS agent_message_links (
                message_id TEXT PRIMARY KEY, steered_into TEXT, reply_to TEXT,
                sender_message_id INTEGER, target_message_id INTEGER
            )""")
            _unwiden_agent_messages(db)
            # 0.11.12: the turn a bot-to-bot message was sent from (None-origin messages are not here) and
            # whether its reply already woke that bot ("woke_at" is the claim: a reply wakes once)
            db.execute("""CREATE TABLE IF NOT EXISTS agent_message_origin (
                message_id TEXT PRIMARY KEY, origin_job TEXT NOT NULL, woke_at TEXT, wake_decision TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS agent_message_origin_job ON agent_message_origin (origin_job)")
            db.execute("CREATE INDEX IF NOT EXISTS agent_message_links_steered ON agent_message_links (steered_into)")
            db.execute("CREATE INDEX IF NOT EXISTS agent_message_links_reply ON agent_message_links (reply_to)")
            db.execute("CREATE INDEX IF NOT EXISTS agent_message_links_sent ON agent_message_links (sender_message_id)")
            db.execute("CREATE INDEX IF NOT EXISTS agent_message_links_received ON agent_message_links (target_message_id)")
            # what the code reads: an agent message with its links (seq orders equal timestamps)
            # (rebuilt only when missing or different: stores opened at once must not race on it)
            view = ("CREATE VIEW agent_messages_x AS SELECT m.*, m.rowid AS seq, l.steered_into, l.reply_to, "
                    "l.sender_message_id, l.target_message_id FROM agent_messages m "
                    "LEFT JOIN agent_message_links l ON l.message_id=m.id")
            found = db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='agent_messages_x'").fetchone()
            if found is None or found[0] != view:
                if found is not None:
                    db.execute("DROP VIEW IF EXISTS agent_messages_x")
                try:
                    db.execute(view)
                except sqlite3.OperationalError as exc:
                    if "already exists" not in str(exc):
                        raise

            # one row per finished attempt that was re-queued (automatic or manual retry)
            db.execute(
                """CREATE TABLE IF NOT EXISTS job_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    kind TEXT, error TEXT, reply_text TEXT, started_at TEXT, finished_at TEXT NOT NULL,
                    automatic INTEGER NOT NULL DEFAULT 1, not_before TEXT
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS job_attempts_time ON job_attempts(automatic, finished_at)")
            # 0.11.0: what each retried run reported (tokens, cost), so budgets count every run
            add_columns(db, "job_attempts", (("usage_json", "TEXT"),))
            # every start-up, so an interrupted migration or a rollback to older code (which does not set
            # the column) still converges; a no-op once every started job has it
            db.execute("UPDATE jobs SET first_started_at=COALESCE((SELECT MIN(a.started_at) FROM job_attempts a "
                       "WHERE a.job_id=jobs.id AND a.started_at IS NOT NULL), started_at) "
                       "WHERE first_started_at IS NULL AND started_at IS NOT NULL")
            if "assignment_minutes" in added_jobs and "assignment_id" not in added_jobs:
                # pre-release 0.10.0 data charged turn and minutes at settlement: settled jobs are
                # fully charged, finished unsettled ones still owe the turn claim now charges
                for job in db.execute("SELECT * FROM jobs WHERE assignment_id IS NOT NULL").fetchall():
                    if job["assignment_settled"]:
                        db.execute("UPDATE jobs SET assignment_minutes=? WHERE id=?",
                                   (_run_minutes(db, job)[0], job["id"]))
                    elif job["status"] not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
                        db.execute("UPDATE assignments SET turns_used=turns_used+1 WHERE id=?", (job["assignment_id"],))
            db.execute("CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, priority, created_at)")
            # 0.12.1: most per-bot job lookups (running/queued turn, last failure, caps) filter on bot_id
            db.execute("CREATE INDEX IF NOT EXISTS jobs_bot ON jobs(bot_id, status, created_at)")
            db.execute("CREATE TABLE IF NOT EXISTS agent_tokens (token_hash TEXT PRIMARY KEY, job_id TEXT NOT NULL, bot_id TEXT NOT NULL)")

            # Unread state per bot. revision changes on every "mark as unread" so a stale
            # read from another tab or an older poll can be rejected as a whole.
            db.execute(
                """CREATE TABLE IF NOT EXISTS bot_reads (
                    bot_id TEXT PRIMARY KEY, last_read_message_id INTEGER NOT NULL DEFAULT 0,
                    read_at TEXT, manual_unread_at TEXT, revision INTEGER NOT NULL DEFAULT 0
                )"""
            )

            self._migrate_identity(db)
            self._migrate_effort(db)
            self._migrate_reads(db)
            sections.ensure_schema(db)
            self._migrate_avatars(db)
            self._migrate_avatar_characters(db)
            self._migrate_retired_shapes(db)
            chat_history.ensure_fts(db, lambda d, key: d.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?,?)", (key, now().isoformat())))

            # Grok bridge state: one row per send (reserved before the webhook call, then sent
            # or failed) and the last hop per bridge thread.
            db.execute(
                "CREATE TABLE IF NOT EXISTS bridge_sends (id INTEGER PRIMARY KEY, bot TEXT NOT NULL, "
                "ts TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'reserved')"
            )
            db.execute("CREATE INDEX IF NOT EXISTS bridge_sends_ts ON bridge_sends(ts)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS bridge_threads (thread_id TEXT PRIMARY KEY, hop INTEGER NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            self._import_bridge_json(db)
            _init_assignment_tables(db)
            db.execute("CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, "
                       "job_id TEXT, assignment_id TEXT, message_id INTEGER NOT NULL, payload TEXT NOT NULL, "
                       "status TEXT NOT NULL, answer TEXT, blocking INTEGER NOT NULL, created_at TEXT NOT NULL, "
                       "answered_at TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS questions_bot ON questions(bot_id, status)")
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS notify_question_open AFTER INSERT ON questions WHEN NEW.status='open'
                    BEGIN INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at)
                    VALUES ('q:' || NEW.id || ':open', 'needs_you', NEW.bot_id, NULL, 'question_open',
                            strftime('%Y-%m-%dT%H:%M:%SZ','now')); END""")
            _init_control_tables(db)
            _init_notify_triggers(db)

    def _import_bridge_json(self, db: sqlite3.Connection) -> None:
        """One-time: import the pre-0.9.0 bridge JSON files, then rename them ``*.imported``."""
        directory = self.path.parent
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (BRIDGE_IMPORT,)).fetchone():
                db.rollback()
            else:
                rate, threads = (_read_json(directory / name) for name in BRIDGE_JSON)
                for item in (rate.get("sends") or []) if isinstance(rate, dict) else []:
                    with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError):
                        stamp = datetime.fromisoformat(item["ts"])
                        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
                        db.execute("INSERT INTO bridge_sends (bot, ts, state) VALUES (?,?,'sent')",
                                   (str(item.get("bot") or ""), stamp.astimezone(UTC).isoformat()))
                for thread_id, hop in (threads.items() if isinstance(threads, dict) else ()):
                    with contextlib.suppress(TypeError, ValueError):
                        db.execute(
                            "INSERT INTO bridge_threads VALUES (?,?,?) ON CONFLICT(thread_id) DO UPDATE "
                            "SET hop=max(hop, excluded.hop)", (str(thread_id), int(hop), now().isoformat()))
                db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (BRIDGE_IMPORT, now().isoformat()))
                db.commit()
        except BaseException:
            db.rollback()
            raise
        for name in BRIDGE_JSON:
            with contextlib.suppress(OSError):
                if (directory / name).is_file():
                    (directory / name).replace(directory / f"{name}.imported")

    # --- Grok bridge state (rate limits and thread hops; network I/O happens outside) ---

    def bridge_reserve(self, bot: str, thread_id: str | None, moment: datetime, *, per_bot_seconds: int,
                       daily_limit: int, max_hops: int,
                       per_bot_daily: int | None = None) -> tuple[int | None, int, str | None]:
        """Check the limits, then reserve a send and advance the thread hop in one short write
        transaction, committed before the webhook call. Returns (reservation id, hop, None),
        or (None, hop, reason) when the send must be dropped. A reservation whose process died
        before settling keeps counting against the limits (fails safe)."""
        moment = moment.astimezone(UTC)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT hop FROM bridge_threads WHERE thread_id=?", (thread_id,)).fetchone() if thread_id else None
                hop = (row["hop"] if row else 0) + 1
                reason = None
                if hop > max_hops:
                    reason = f"hop limit of {max_hops} reached"
                elif db.execute("SELECT count(*) FROM bridge_sends WHERE ts>=? AND state!='failed'",
                                ((moment - timedelta(hours=24)).isoformat(),)).fetchone()[0] >= daily_limit:
                    reason = f"daily limit of {daily_limit} sends reached"
                elif per_bot_daily is not None and db.execute(
                        "SELECT count(*) FROM bridge_sends WHERE bot=? AND ts>=? AND state!='failed'",
                        (bot, (moment - timedelta(hours=24)).isoformat())).fetchone()[0] >= per_bot_daily:
                    reason = f"daily limit of {per_bot_daily} sends per bot reached"
                elif db.execute("SELECT 1 FROM bridge_sends WHERE bot=? AND ts>? AND state!='failed' LIMIT 1",
                                (bot, (moment - timedelta(seconds=per_bot_seconds)).isoformat())).fetchone():
                    reason = f"rate limit: at most one send per {per_bot_seconds}s per bot"
                if reason:
                    db.rollback()
                    return None, hop, reason
                cursor = db.execute("INSERT INTO bridge_sends (bot, ts) VALUES (?,?)", (bot, moment.isoformat()))
                if thread_id:
                    db.execute("INSERT INTO bridge_threads VALUES (?,?,?) ON CONFLICT(thread_id) DO UPDATE "
                               "SET hop=excluded.hop, updated_at=excluded.updated_at", (thread_id, hop, now().isoformat()))
                db.commit()
                return cursor.lastrowid, hop, None
            except BaseException:
                db.rollback()
                raise

    def bridge_settle(self, reservation: int, sent: bool, thread_id: str | None = None, hop: int = 0) -> None:
        """A sent reservation stays counted; a failed one is released and its hop step undone
        (unless the thread moved on meanwhile)."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE bridge_sends SET state=? WHERE id=?", ("sent" if sent else "failed", reservation))
            if not sent and thread_id:
                db.execute("UPDATE bridge_threads SET hop=hop-1 WHERE thread_id=? AND hop=?", (thread_id, hop))
            db.commit()

    def bridge_inbox_hop(self, thread_id: str | None, supplied: int, max_hops: int) -> tuple[int, bool]:
        """Hop of an inbound bridge message: max(stored + 1, supplied), so a sender cannot
        reset a thread's count. Returns (hop, accepted). Read only: the hop is stored with the
        turn itself (enqueue_message *bridge_hop*), so a failed enqueue consumes no hop."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT hop FROM bridge_threads WHERE thread_id=?", (thread_id,)).fetchone() if thread_id else None
        hop = max((row["hop"] + 1) if row else 1, supplied)
        return hop, hop <= max_hops

    @staticmethod
    def _migrate_effort(db: sqlite3.Connection) -> None:
        """One-time: give existing bots their engine's default effort (Codex high, Claude medium).

        Bots on an engine with no effort flag stay unset. Past jobs are not rewritten.
        """
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (EFFORT_MIGRATION,)).fetchone():
                db.rollback()
                return
            stamp = now().isoformat()
            for row in db.execute(
                "SELECT id, default_provider FROM bots WHERE effort IS NULL"
            ).fetchall():
                level = default_effort(row["default_provider"])
                if level:
                    db.execute("UPDATE bots SET effort=? WHERE id=?", (level, row["id"]))
            db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (EFFORT_MIGRATION, stamp))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _migrate_reads(db: sqlite3.Connection) -> None:
        """One-time: treat everything already in existing chats as read, so an upgrade does
        not flood the sidebar with old history. Bots created later start with nothing read."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (READS_MIGRATION,)).fetchone():
                db.rollback()
                return
            stamp = now().isoformat()
            db.execute(
                "INSERT OR IGNORE INTO bot_reads (bot_id, last_read_message_id, read_at) "
                "SELECT bots.id, COALESCE((SELECT MAX(id) FROM messages WHERE messages.bot_id=bots.id), 0), ? "
                "FROM bots",
                (stamp,),
            )
            db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (READS_MIGRATION, stamp))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _migrate_avatars(db: sqlite3.Connection) -> None:
        """One-time (0.12.0): a bot with no uploaded image and no seed gets its default generated avatar.
        Its emoji is kept and stays the fallback. Nothing else about the bot changes."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (AVATARS_MIGRATION,)).fetchone():
                db.rollback()
                return
            used = [r[0] for r in db.execute("SELECT avatar_seed FROM bots WHERE avatar_seed IS NOT NULL")]
            for row in db.execute("SELECT id FROM bots WHERE icon_image IS NULL AND avatar_seed IS NULL ORDER BY created_at, id").fetchall():
                seed = avatars.default_seed(row["id"], used)
                used.append(seed)
                db.execute("UPDATE bots SET avatar_seed=? WHERE id=?", (seed, row["id"]))
            db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (AVATARS_MIGRATION, now().isoformat()))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _migrate_avatar_characters(db: sqlite3.Connection) -> None:
        """One-time (0.12.12): a bot still on a generated pattern gets the character its id hashes to. Uploads, emoji-only
        bots and bots that already have a character are untouched. The old seeds are kept as JSON in ``meta``
        (``avatars.pattern_backup``) so the change can be undone; 0.12.1 and older show the emoji for a character."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (AVATAR_CHARS_MIGRATION,)).fetchone():
                db.rollback()
                return
            old = {}
            rows = db.execute("SELECT id, avatar_seed FROM bots WHERE icon_image IS NULL AND avatar_seed IS NOT NULL "
                              "AND avatar_seed NOT LIKE 'char|%'").fetchall()
            for row in rows:
                old[row["id"]] = row["avatar_seed"]
                db.execute("UPDATE bots SET avatar_seed=? WHERE id=?", (avatars.derived_character(row["id"]), row["id"]))
            if old:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (AVATAR_PATTERN_BACKUP, json.dumps(old)))
            db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (AVATAR_CHARS_MIGRATION, now().isoformat()))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _migrate_retired_shapes(db: sqlite3.Connection) -> None:
        """Every startup, idempotent: a stored ``char|ghost|<color>`` becomes ``char|droplet|<color>`` (same color). Only
        ``avatar_seed`` of those bots changes. The old values are merged into ``meta`` (``avatars.ghost_backup``)
        first. With nothing to change (the normal case) it writes nothing."""
        db.execute("BEGIN IMMEDIATE")
        try:
            changes = {}
            for row in db.execute("SELECT id, avatar_seed FROM bots WHERE avatar_seed LIKE 'char|%'").fetchall():
                new = avatars.migrate_character_seed(row["avatar_seed"])
                if new is not None:
                    changes[row["id"]] = (row["avatar_seed"], new)
            if changes:
                found = db.execute("SELECT value FROM meta WHERE key=?", (AVATAR_GHOST_BACKUP,)).fetchone()
                backup = json.loads(found[0]) if found else {}
                for bot_id, (old, new) in changes.items():
                    backup.setdefault(bot_id, []).append(old)       # oldest first: a rollback and re-upgrade adds to it
                    db.execute("UPDATE bots SET avatar_seed=? WHERE id=?", (new, bot_id))
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (AVATAR_GHOST_BACKUP, json.dumps(backup)))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _migrate_identity(db: sqlite3.Connection) -> None:
        """One-time: give existing bots a model and icon, and rename CLI-named bots.

        Ids, memory, history and routines are untouched. A renamed bot keeps its
        old name as an alias so bridge senders addressing it still reach it.
        """
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (IDENTITY_MIGRATION,)).fetchone():
                db.rollback()
                return
            stamp = now().isoformat()
            rows = db.execute("SELECT id, name, default_provider, model, icon FROM bots").fetchall()
            names = {row["name"] for row in rows}
            for row in rows:
                name = row["name"]
                updates: dict[str, str] = {}
                new_name = LEGACY_RENAMES.get((name, row["default_provider"]))
                if new_name and new_name not in names:
                    updates["name"] = new_name
                    names.add(new_name)
                    db.execute(
                        "INSERT OR IGNORE INTO bot_aliases (alias, bot_id) VALUES (?,?)",
                        (name, row["id"]),
                    )
                if not row["model"]:
                    model = default_model(row["default_provider"])
                    if model:
                        updates["model"] = model
                if not row["icon"]:
                    updates["icon"] = DEFAULT_ICONS.get(updates.get("name", name), DEFAULT_ICON)
                if updates:
                    updates["updated_at"] = stamp
                    db.execute(
                        f"UPDATE bots SET {','.join(f'{k}=?' for k in updates)} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )
            db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (IDENTITY_MIGRATION, stamp))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _job(row: sqlite3.Row | None) -> Job | None:
        return _model(Job, row, ("created_at", "started_at", "first_started_at", "finished_at", "not_before"))

    def create(
        self,
        prompt: str,
        provider: str = "codex",
        *,
        bot_id: UUID | None = None,
        reason: str | None = None,
        source: str = "api",
        routine_id: UUID | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> Job:
        job = Job(
            id=uuid4(),
            prompt=prompt,
            provider=provider,
            status=JobStatus.QUEUED,
            created_at=now(),
            bot_id=bot_id,
            reason=reason,
            source=source,
            routine_id=routine_id,
            model=model,
            effort=effort,
        )
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO jobs (id,prompt,status,provider,created_at,bot_id,reason,source,"
                "routine_id,model,effort,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(job.id),
                    job.prompt,
                    job.status.value,
                    job.provider,
                    job.created_at.isoformat(),
                    str(bot_id) if bot_id else None,
                    reason,
                    source,
                    str(routine_id) if routine_id else None,
                    model,
                    effort,
                    _priority(source),
                ),
            )
        return job

    def get(self, job_id: UUID | str) -> Job | None:
        with closing(self._connect()) as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (str(job_id),)).fetchone())

    def list(self) -> list[Job]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at, id").fetchall()
        return [self._job(row) for row in rows if row is not None]

    def running_job_ids(self) -> list[str]:
        with closing(self._connect()) as db:
            return [row["id"] for row in db.execute("SELECT id FROM jobs WHERE status=?", (JobStatus.RUNNING.value,))]

    def post_steer(self, bot_id: UUID | str, text: str, attachments: list[str],
                   reason: str | None = None, job_id: UUID | str | None = None) -> tuple[Message, Job] | None:
        """See :meth:`post_steer_explained`; this returns only the result (None: nothing to steer)."""
        return self.post_steer_explained(bot_id, text, attachments, reason, job_id)[0]

    def post_steer_explained(
        self, bot_id: UUID | str, text: str, attachments: list[str], reason: str | None = None,
        job_id: UUID | str | None = None, *, unsteerable: Callable[[Job], str | None] | None = None,
    ) -> tuple[tuple[Message, Job] | None, str | None]:
        """Attach an owner message to the bot's current turn atomically with checking its state.

        The turn is, in order: the bot's running job (a chat/API one first), else its oldest queued
        chat/API job (not started yet, or parked between attempts; the steer is delivered when it starts),
        never an automatic one (``auto_kind`` set, or held by a cap: it would wait with the hold).
        Assignment turns are steerable too (0.11.6): the message is part of that turn, is not a turn of its
        own and costs no budget; if it is still undelivered when the turn ends it runs as a chat turn.
        Never steered into: bot-to-bot turns (``inbox``, anything with an agent message row), whose reply
        goes back to the sending bot and must not carry the owner's chat text.

        Returns ``(result, note)``. *result* is None when there is nothing to steer, and then *note*
        says why when the bot was busy (it answers another bot) so the caller can tell the owner; None
        for an idle bot. *unsteerable* (job -> reason or None) is asked for a RUNNING job: when the
        engine cannot take a message mid-run the steer is attached and turned into a queued turn in the
        same transaction, so it is never a pending message that quietly waits (the note is then returned
        with the result and stored on the message as ``steer_note``).
        """
        note = None
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if job_id:
                row = db.execute("SELECT * FROM jobs WHERE bot_id=? AND id=? AND status='running' AND slash IS NULL",
                                 (str(bot_id), str(job_id))).fetchone()
            else:
                # a slash command turn (0.11.14) takes no steer: its prompt is the bare command line
                row = db.execute("SELECT * FROM jobs WHERE bot_id=? AND status='running' AND source!='inbox' AND slash IS NULL "
                                 "AND NOT EXISTS (SELECT 1 FROM agent_messages a WHERE a.job_id=jobs.id) "
                                 "ORDER BY source IN ('chat','api') DESC, started_at LIMIT 1",
                                 (str(bot_id),)).fetchone()
                if not row:
                    # never an automatic turn (a queued or cap-held continuation): the owner's message
                    # is a turn of its own, which supersedes a queued chat continuation (enqueue_message)
                    row = db.execute("SELECT * FROM jobs WHERE bot_id=? AND status='queued' "
                                     "AND source IN ('chat','api') AND auto_kind IS NULL AND slash IS NULL "
                                     "AND COALESCE(failure_kind,'')!=? ORDER BY created_at, id LIMIT 1",
                                     (str(bot_id), continuation.HELD)).fetchone()
                if not row:
                    busy = db.execute("SELECT slash FROM jobs WHERE bot_id=? AND status='running' "
                                      "ORDER BY slash IS NULL LIMIT 1", (str(bot_id),)).fetchone()
                    queued_slash = db.execute("SELECT 1 FROM jobs WHERE bot_id=? AND status='queued' AND slash IS NOT NULL "
                                              "LIMIT 1", (str(bot_id),)).fetchone()
                    db.rollback()
                    if (busy and busy["slash"]) or (not busy and queued_slash):
                        return None, SLASH_BUSY_NOTE
                    return None, (BUSY_WITH_BOT_NOTE if busy else None)
            if not row:
                db.rollback()
                return None, None
            cur = db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments,steer_job_id,"
                             "steer_status,steer_reason) VALUES (?,'user','chat',?,?,?,?,'pending',?)",
                             (str(bot_id), text, now().isoformat(), json.dumps(attachments), row['id'], reason))
            if unsteerable is not None and row['status'] == JobStatus.RUNNING.value:
                note = unsteerable(self._job(row))
                if note:
                    self._queue_steers(db, row['id'], note)
            message = db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
            queued_job = (db.execute("SELECT * FROM jobs WHERE id=?", (message['job_id'],)).fetchone()
                          if note and message['job_id'] else None)
            db.commit()
            result_job = self._job(queued_job) if queued_job is not None else self._job(row)
            return (_model(Message, message, ("created_at",)), result_job), note

    def pending_steers(self, job_id: UUID | str) -> list[Message]:
        with closing(self._connect()) as db:
            # the owner's messages first, then other bots' (0.11.10), each in the order sent
            rows = db.execute("SELECT * FROM messages WHERE steer_job_id=? AND steer_status='pending' "
                              "AND archived_at IS NULL ORDER BY source='inbox', id", (str(job_id),)).fetchall()
        return [_model(Message, row, ("created_at",)) for row in rows]

    def delivered_steers(self, job_id: UUID | str) -> int:
        """How many owner messages were steered into *job_id* while it ran."""
        with closing(self._connect()) as db:
            # another bot's steer (source inbox) never restarts the owner's auto-continue count
            return db.execute("SELECT COUNT(*) FROM messages WHERE steer_job_id=? AND steer_status='delivered' "
                              "AND source!='inbox'", (str(job_id),)).fetchone()[0]

    def user_turn_waiting(self, bot_id: UUID | str) -> bool:
        """A chat or API turn of the owner is queued for this bot (it runs next and decides)."""
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM jobs WHERE bot_id=? AND status='queued' AND source IN ('chat','api') "
                              "AND auto_kind IS NULL LIMIT 1", (str(bot_id),)).fetchone() is not None

    def claim_auto_decision(self, job_id: UUID | str) -> bool:
        """0.11.10: mark *job_id* as being decided (continue or not); False if it already was, so a
        finished turn is continued at most once whoever calls."""
        with closing(self._connect()) as db:
            return db.execute("UPDATE jobs SET auto_decision='deciding' WHERE id=? AND auto_decision IS NULL",
                              (str(job_id),)).rowcount == 1

    def fail_stale_decisions(self, line: str) -> int:
        """At start: a job left ``deciding`` (the process died, or ``decide`` raised, before the
        decision was stored) gets ``error`` and one chat line *line*, so the chain never stops silently."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT j.id, j.bot_id, j.source FROM jobs j JOIN bots b ON b.id=j.bot_id "
                              "WHERE j.auto_decision='deciding'").fetchall()
            for row in rows:
                db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments) "
                           "VALUES (?,'system',?,?,?,'[]')", (row["bot_id"], row["source"], line, now().isoformat()))
            db.execute("UPDATE jobs SET auto_decision='error' WHERE auto_decision='deciding'")
            db.commit()
        return len(rows)

    def record_stop_decision(self, job_id: UUID | str, code: str, bot_id: UUID | str, line: str, *,
                             source: str) -> None:
        """A decision not to continue *job_id*: its code and the owner's chat line in one transaction."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET auto_decision=? WHERE id=?", (code, str(job_id)))
            db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments) "
                       "VALUES (?,'system',?,?,?,'[]')", (str(bot_id), source, line, now().isoformat()))
            db.commit()

    def set_auto_decision(self, job_id: UUID | str, code: str) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE jobs SET auto_decision=? WHERE id=?", (code, str(job_id)))

    def chain_turns(self, root: UUID | str, until: datetime) -> list[tuple[str, str]]:
        """(reply, status reason) of a chat continuation chain's finished turns up to *until*, oldest first."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT reply_text, outcome FROM jobs WHERE (id=? OR continued_from=?) AND created_at<=? "
                              "AND status NOT IN ('queued','running') ORDER BY created_at, continue_count",
                              (str(root), str(root), until.isoformat())).fetchall()
        return [(row["reply_text"] or "", (row["outcome"] or "").partition(": ")[2]) for row in rows]

    def pending_approval(self, job_id: UUID | str) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM approvals WHERE job_id=? AND status='pending' LIMIT 1",
                              (str(job_id),)).fetchone() is not None

    def has_open_question(self, job_id: UUID | str) -> bool:
        """The job is waiting on an unanswered blocking ``desk ask`` question."""
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM questions WHERE job_id=? AND status='open' AND blocking=1",
                              (str(job_id),)).fetchone() is not None

    def take_pending_steers(self, job_id: UUID | str) -> tuple[list[Message], list[tuple[int, str]]]:
        """Claim the job's pending steers for an attempt's prompt: select and mark delivered in one
        transaction, so the provider's mid-turn steer loop can never also send them.

        Returns (every non-archived steer of this job still attached to it, pending or delivered earlier,
        so a retry keeps the ones an earlier attempt did not get to use; (id, stamp) of those claimed now)."""
        stamp = now().isoformat()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            claimed = [(row["id"], stamp) for row in db.execute(
                "SELECT id FROM messages WHERE steer_job_id=? AND steer_status='pending' AND archived_at IS NULL "
                "ORDER BY source='inbox', id", (str(job_id),)).fetchall()]
            db.executemany("UPDATE messages SET steer_status='delivered', steer_delivered_at=? WHERE id=?",
                           [(stamp, mid) for mid, _ in claimed])
            rows = db.execute("SELECT * FROM messages WHERE steer_job_id=? AND steer_status IN ('pending','delivered') "
                              "AND archived_at IS NULL ORDER BY source='inbox', id", (str(job_id),)).fetchall()
            db.commit()
        return [_model(Message, row, ("created_at",)) for row in rows], claimed

    def deliver_steer(self, message_id: int) -> str | None:
        stamp = now().isoformat()
        with closing(self._connect()) as db:
            changed = db.execute("UPDATE messages SET steer_status='delivered', steer_delivered_at=? "
                                 "WHERE id=? AND steer_status='pending' AND archived_at IS NULL",
                                 (stamp, message_id)).rowcount
        return stamp if changed else None

    @staticmethod
    def _queue_steers(db: sqlite3.Connection, job_id: str, note: str | None = None) -> list[int]:
        # the steered job's settings; the bot's if that job row is gone
        rows = db.execute("SELECT m.*, j.id AS steered, COALESCE(j.provider, b.default_provider) AS provider, "
                          "COALESCE(m.steer_reason, j.reason, b.reason) AS turn_reason, "
                          "j.created_at AS steered_at, CASE WHEN j.id IS NULL THEN b.model ELSE j.model END AS turn_model, "
                          "CASE WHEN j.id IS NULL THEN b.effort ELSE j.effort END AS turn_effort "
                          "FROM messages m JOIN bots b ON b.id=m.bot_id LEFT JOIN jobs j ON j.id=m.steer_job_id "
                          "WHERE m.steer_job_id=? AND m.steer_status='pending' AND m.archived_at IS NULL "
                          "ORDER BY m.id", (job_id,)).fetchall()
        for index, row in enumerate(rows, 1):
            key = str(uuid4())
            # claim orders by (priority, created_at): stamping the turn just after the job it was steered
            # into (+1 ms per steer, message order) puts it ahead of turns queued later, and keeps two
            # steered jobs' turns in the order of their jobs. Not "now", which would sort behind them.
            created = (datetime.fromisoformat(row['steered_at']) + timedelta(milliseconds=index)
                       if row['steered_at'] else now())
            effort = row['turn_effort'] or (None if row['steered'] else default_effort(row['provider']))
            source, prompt = 'chat', row['text']
            if row['source'] == 'inbox':
                # 0.11.10: another bot's steer becomes an inbox turn of its own: its reply goes back to
                # that bot, and its text stays marked as a bot's (never an owner's chat turn)
                source, prompt = 'inbox', f"(from {row['sender']}) {row['text']}"
                db.execute("UPDATE agent_messages SET job_id=? WHERE id=(SELECT message_id FROM agent_message_links "
                           "WHERE target_message_id=?)", (key, row['id']))
                db.execute("UPDATE agent_message_links SET steered_into=NULL WHERE target_message_id=?", (row['id'],))
                db.execute("UPDATE messages SET job_id=? WHERE id=(SELECT sender_message_id FROM agent_message_links "
                           "WHERE target_message_id=?)", (key, row['id']))
            db.execute("INSERT INTO jobs (id,prompt,status,provider,created_at,bot_id,reason,source,model,effort,priority) "
                       "VALUES (?,?,'queued',?,?,?,?,?,?,?,?)",
                       (key, prompt, row['provider'], created.isoformat(), row['bot_id'], row['turn_reason'],
                        source, row['turn_model'], effort, _priority(source)))
            db.execute("UPDATE messages SET job_id=?, steer_status='queued', steer_note=? WHERE id=?",
                       (key, note or STEER_ENDED_NOTE, row['id']))
        return [row['id'] for row in rows]

    def steered_reply_id(self, message: Message) -> int | None:
        """For a steer turned into a queued turn: the newest message of the job it was sent into."""
        if not message.steer_job_id:
            return None
        with closing(self._connect()) as db:
            row = db.execute("SELECT MAX(id) FROM messages WHERE bot_id=? AND job_id=? AND kind!='say' AND archived_at IS NULL",
                             (str(message.bot_id), str(message.steer_job_id))).fetchone()
        return row[0] if row else None

    def queue_steers(self, job_id: UUID | str, note: str | None = None) -> list[int]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            ids = self._queue_steers(db, str(job_id), note)
            db.commit()
        return ids

    def recover_interrupted(
        self, held: set[str] | frozenset[str] = frozenset(), queued_steers: list[int] | None = None
    ) -> list[dict]:
        """After a controller restart: re-queue each RUNNING job once, else fail it.

        A job whose leftover provider processes could not be stopped (*held*) is never
        replayed. Each transition and its chat notice commit together, so a repeated
        startup neither loses nor duplicates them. Replay is at-least-once: actions the
        interrupted attempt already took may have happened. Returns the affected rows. Message ids of
        pending steers turned into queued turns are appended to *queued_steers* (the caller logs them).
        """
        stamp = now().isoformat()
        affected = []
        made: list[int] = []
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute("SELECT * FROM jobs WHERE status=?", (JobStatus.RUNNING.value,)).fetchall()
                for row in rows:
                    key = row["id"]
                    made.extend(self._queue_steers(db, key, "Bot-Office restarted during the turn, so it runs as the next turn."))
                    stopped = row["assignment_id"] and not db.execute(
                        "SELECT 1 FROM assignments WHERE id=? AND status='active'", (row["assignment_id"],)).fetchone()
                    if stopped:
                        # a stop (or close) is durable: its turn is never replayed
                        db.execute(
                            "UPDATE jobs SET status=?, finished_at=?, error=?, failure_kind='interrupted', slot=NULL,"
                            " agent_token_hash=NULL WHERE id=?",
                            (JobStatus.CANCELLED.value, stamp, "assignment stopped; not replayed after restart", key),
                        )
                        notice = "Bot-Office restarted during this turn; not replayed (its assignment was stopped)."
                        outcome = "cancelled"
                    elif key not in held and not row["restart_attempts"]:
                        # the interrupted execution goes into the attempt history; the replay runs
                        # as a new execution (restart_attempts+1) without using an automatic retry
                        db.execute(
                            "INSERT INTO job_attempts (job_id,attempt,kind,error,reply_text,started_at,finished_at,"
                            "automatic) VALUES (?,?,'interrupted',?,?,?,?,0)",
                            (key, execution(row), "interrupted by controller restart", row["draft_text"],
                             row["started_at"], stamp),
                        )
                        db.execute(
                            "UPDATE jobs SET status=?, restart_attempts=restart_attempts+1, failure_kind='interrupted',"
                            " slot=NULL, not_before=NULL, draft_text=NULL, agent_token_hash=NULL WHERE id=?",
                            (JobStatus.QUEUED.value, key),
                        )
                        seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM job_events WHERE job_id=?", (key,)).fetchone()[0]
                        db.execute(
                            "INSERT INTO job_events (job_id,seq,kind,data,created_at) VALUES (?,?,?,?,?)",
                            (key, seq + 1, "retry", json.dumps({"attempt": execution(row) + 1, "restart": True}), stamp),
                        )
                        notice = ("Bot-Office restarted during this turn; retrying from the last confirmed session; "
                                  "prior actions may have occurred.")
                        outcome = "requeued"
                    else:
                        why = ("its earlier processes could not be proven stopped" if key in held
                               else "it was already retried once after a restart")
                        db.execute(
                            "UPDATE jobs SET status=?, finished_at=?, error=?, failure_kind='interrupted' WHERE id=?",
                            (JobStatus.FAILED.value, stamp, f"interrupted by controller restart; not retried: {why}", key),
                        )
                        notice = f"Bot-Office restarted during this turn; not retried again ({why})."
                        outcome = "failed"
                    if row["bot_id"] and db.execute("SELECT 1 FROM bots WHERE id=?", (row["bot_id"],)).fetchone() and not db.execute(
                        "SELECT 1 FROM messages WHERE bot_id=? AND job_id=? AND role='user' AND archived_at IS NOT NULL",
                        (row["bot_id"], key),
                    ).fetchone():
                        db.execute(
                            "INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at,attachments)"
                            " VALUES (?,?,?,?,?,?,?,'[]')",
                            (row["bot_id"], "system", None, row["source"], notice, key, stamp),
                        )
                    affected.append({"id": key, "bot_id": row["bot_id"], "outcome": outcome})
                # Also repair a crash between a terminal transition and its pending-steer cleanup. A queued
                # (parked) job is not terminal: its pending steers stay attached for its next attempt.
                for row in db.execute("SELECT DISTINCT m.steer_job_id FROM messages m LEFT JOIN jobs j "
                                      "ON j.id=m.steer_job_id WHERE m.steer_status='pending' "
                                      "AND (j.id IS NULL OR j.status NOT IN ('queued','running'))").fetchall():
                    made.extend(self._queue_steers(db, row[0]))
                db.execute("DELETE FROM agent_tokens WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id=agent_tokens.job_id AND j.bot_id=agent_tokens.bot_id AND j.status='running')")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        if queued_steers is not None:
            queued_steers.extend(made)  # only after the commit: a rolled-back recovery logs nothing
        return affected

    def cancel(self, job_id: UUID | str, *, queue_steers: bool = True, reason: str = "cancelled") -> Job | None:
        """Cancel a queued job. Steers still pending on it become queued turns (unless the caller,
        an edit/rewind, archives or queues them itself)."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (str(job_id),)).fetchone()
            job = self._job(row)
            if job and job.status == JobStatus.QUEUED:
                db.execute(
                    "UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
                    (JobStatus.CANCELLED.value, now().isoformat(), reason, str(job_id)),
                )
                if queue_steers:
                    self._queue_steers(db, str(job_id))
            db.commit()
        return self.get(job_id)

    def claim(self, desktops: str | Sequence[str], enabled_providers: set[str] | frozenset[str]) -> Job | None:
        """Start the next due queued job (lowest priority value, then oldest) and lease it the
        first free desktop of *desktops*; with none free it runs without a desktop (slot NULL).
        How many jobs run at once is the scheduler's lane count, not the desktop count."""
        if not enabled_providers:
            return None
        desktops = [desktops] if isinstance(desktops, str) else list(desktops)
        placeholders = ",".join("?" for _ in enabled_providers)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            busy = {row["slot"] for row in db.execute(
                "SELECT slot FROM jobs WHERE status=? AND slot IS NOT NULL", (JobStatus.RUNNING.value,))}
            slot = next((name for name in desktops if name not in busy), None)
            while True:
                row = db.execute(
                    f"SELECT id FROM jobs AS queued WHERE status=? AND provider IN ({placeholders}) "
                    "AND (bot_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs AS active "
                    "WHERE active.bot_id=queued.bot_id AND active.status='running')) "
                    "AND (not_before IS NULL OR not_before <= ?) "
                    # among equal job priorities, the turn of the more urgent task goes first
                    "ORDER BY priority, COALESCE((SELECT " + PRIORITY_RANK.format("a.") + " FROM assignments AS a "
                    "WHERE a.id=queued.assignment_id), 2), created_at, id LIMIT 1",
                    (JobStatus.QUEUED.value, *sorted(enabled_providers), now().isoformat()),
                ).fetchone()
                if not row:
                    db.commit()  # keeps the cancellations of turns the gate refused
                    return None
                job_id = row["id"]
                # 0.11.10: an automatic turn runs only while auto-continue is on (a held one only if
                # still current), and over a cap it waits for its window, before it reserves a task turn
                stale = continuation.stale_hold(db, job_id, now())
                if stale is not None:
                    _drop_stale_hold(db, job_id, stale)
                    continue
                capped = continuation.cap_gate(db, job_id, now())
                if capped is not None:
                    continuation.hold(db, job_id, *capped, now())
                    continue
                refused = _assignment_gate(db, job_id)
                if refused is None:
                    held = budgets.budget_gate(db, job_id, now())
                    if held is None:
                        continuation.unhold(db, job_id)
                        break
                    _hold_for_budget(db, job_id, held)
                    continue
                # cancelled with a short note; the assignment loop settles it (budget -> blocked)
                db.execute("UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
                           (JobStatus.CANCELLED.value, now().isoformat(), f"not run: {refused}", job_id))
                bot_id = db.execute("SELECT bot_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
                if bot_id and db.execute("SELECT 1 FROM bots WHERE id=?", (bot_id,)).fetchone():
                    db.execute("INSERT INTO messages (bot_id,role,source,text,job_id,created_at,attachments) "
                               "VALUES (?,?,?,?,?,?,'[]')", (bot_id, "system", "assignment",
                                                             f"Assignment turn not run: {refused}.", job_id,
                                                             now().isoformat()))
            started = now().isoformat()
            db.execute(
                "UPDATE jobs SET status=?, slot=?, started_at=?, first_started_at=COALESCE(first_started_at, ?) "
                "WHERE id=? AND status=?",
                (JobStatus.RUNNING.value, slot, started, started, job_id, JobStatus.QUEUED.value),
            )
            db.commit()
        return self.get(job_id)

    def finish(
        self,
        job_id: UUID | str,
        status: JobStatus,
        *,
        exit_code: int | None = None,
        log_path: str | None = None,
        error: str | None = None,
        reply_path: str | None = None,
        reply_text: str | None = None,
        failure_kind: str | None = None,
        usage: dict | None = None,
        outcome: str | None = None,
        verified: int | None = None,
        evidence: str | None = None,
    ) -> Job | None:
        if status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise ValueError("finish requires a terminal status")
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE jobs SET status=?, finished_at=?, exit_code=?, log_path=?, error=?, reply_path=?, "
                "reply_text=?, failure_kind=?, usage_json=?, outcome=?, verified=?, evidence=? WHERE id=? AND status=?",
                (
                    status.value,
                    now().isoformat(),
                    exit_code,
                    log_path,
                    clip_error(error),
                    reply_path,
                    reply_text,
                    failure_kind,
                    json.dumps(usage) if usage else None,
                    outcome,
                    verified,
                    clip_error(evidence) if evidence else None,
                    str(job_id),
                    JobStatus.RUNNING.value,
                ),
            )
        return self.get(job_id)

    # --- retries (same job row; job_id, chat linkage and rewind checks stay intact) ---

    def requeue(
        self, job_id: UUID | str, not_before: datetime, kind: str, *, error: str | None,
        reply_text: str | None, max_per_hour: int, event_error: str | None = None,
        usage: dict | None = None,
    ) -> bool:
        """Move a RUNNING job back to QUEUED for an automatic retry at *not_before*.

        The rolling-hour cap is checked, the attempt recorded and the ``retry`` event written
        in the same transaction, so a crash never leaves a re-queued job without its event.
        Returns False (nothing changed) when the cap is reached or the job is not running.
        """
        stamp = now()
        error = clip_error(error)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT attempts, restart_attempts, started_at FROM jobs WHERE id=? AND status=?",
                                 (str(job_id), JobStatus.RUNNING.value)).fetchone()
                used = db.execute("SELECT COUNT(*) FROM job_attempts WHERE automatic=1 AND finished_at>?",
                                  ((stamp - timedelta(hours=1)).isoformat(),)).fetchone()[0]
                if not row or used >= max_per_hour:
                    db.rollback()
                    return False
                db.execute(
                    "INSERT INTO job_attempts (job_id,attempt,kind,error,reply_text,started_at,finished_at,"
                    "automatic,not_before,usage_json) VALUES (?,?,?,?,?,?,?,1,?,?)",
                    (str(job_id), execution(row), kind, error, reply_text, row["started_at"],
                     stamp.isoformat(), not_before.isoformat(), json.dumps(usage) if usage else None),
                )
                db.execute(
                    "UPDATE jobs SET status=?, attempts=attempts+1, not_before=?, failure_kind=?, error=?, "
                    "slot=NULL, draft_text=NULL, agent_token_hash=NULL WHERE id=?",
                    (JobStatus.QUEUED.value, not_before.isoformat(), kind, error, str(job_id)),
                )
                db.execute("DELETE FROM agent_tokens WHERE job_id=?", (str(job_id),))
                seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM job_events WHERE job_id=?",
                                 (str(job_id),)).fetchone()[0]
                db.execute(
                    "INSERT INTO job_events (job_id,seq,kind,data,created_at) VALUES (?,?,?,?,?)",
                    (str(job_id), seq + 1, "retry", json.dumps({
                        "attempt": execution(row) + 1, "not_before": not_before.isoformat(),
                        "failure_kind": kind, "error": event_error if event_error is not None else error}),
                     stamp.isoformat()),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return True

    def retry_now(self, job_id: UUID | str) -> Job:
        """Manual retry of a failed (or incomplete) job: re-queue the same row once, now.

        Raises ValueError if the job is not in a retryable state or its turn was archived
        by an edit. Writes a ``retry`` event so open event streams switch attempts. An
        assignment turn is retried through its assignment (reactivated, settled again).
        """
        key = str(job_id)
        with closing(self._connect()) as db:
            row = db.execute("SELECT assignment_id FROM jobs WHERE id=?", (key,)).fetchone()
        if row and row["assignment_id"]:
            from .assignments import Assignments

            return Assignments(self).retry(key)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._retry(db, key)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.get(key)

    def _retry(self, db: sqlite3.Connection, key: str) -> None:
        stamp = now().isoformat()
        row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if not row:
            raise LookupError("job not found")
        if row["status"] not in (JobStatus.FAILED.value, JobStatus.INCOMPLETE.value):
            raise ValueError(f"job is {row['status']}; only failed or incomplete jobs can be retried")
        if row["bot_id"] and db.execute(
            "SELECT 1 FROM messages WHERE bot_id=? AND job_id=? AND role='user' AND archived_at IS NOT NULL",
            (row["bot_id"], key),
        ).fetchone():
            raise ValueError("this turn was rewound by an edit")
        manual = db.execute(
            "SELECT COUNT(*) FROM job_attempts WHERE job_id=? AND automatic=0 AND kind!='interrupted'",
            (key,),
        ).fetchone()[0]
        if manual >= MAX_MANUAL_RETRIES:
            raise ValueError(f"this job was already retried {manual} times; send a new message instead")
        db.execute(
            "INSERT INTO job_attempts (job_id,attempt,kind,error,reply_text,started_at,finished_at,"
            "automatic) VALUES (?,?,?,?,?,?,?,0)",
            (key, execution(row), row["failure_kind"] or row["status"], row["error"],
             row["reply_text"], row["started_at"], row["finished_at"] or stamp),
        )
        # assignment_settled=0: the new run is settled (charged, decided) like any other
        db.execute(
            "UPDATE jobs SET status=?, attempts=attempts+1, not_before=NULL, finished_at=NULL, slot=NULL,"
            " draft_text=NULL, agent_token_hash=NULL, assignment_settled=0 WHERE id=?",
            (JobStatus.QUEUED.value, key),
        )
        seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM job_events WHERE job_id=?", (key,)).fetchone()[0]
        db.execute(
            "INSERT INTO job_events (job_id,seq,kind,data,created_at) VALUES (?,?,?,?,?)",
            (key, seq + 1, "retry", json.dumps({"attempt": execution(row) + 1, "manual": True}), stamp),
        )

    def switch_provider(self, job_id: UUID | str, provider: str, model: str | None, effort: str | None,
                        reason: str | None) -> Job:
        """#10: the owner's "Retry on <provider>" for a job parked on a usage limit. One transaction:
        this job's provider, model and effort (the bot's defaults are untouched), no wait, and a
        ``retry`` event {switched_from, switched_to}. Never called by the scheduler (only the
        endpoint). The next claim takes the model-switch handoff (workspace.model_switch keys off
        the job's provider). Raises LookupError (no job) or ValueError (not parked)."""
        key = str(job_id)
        stamp = now().isoformat()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(f"SELECT j.*, ({PARKED_SQL}) AS parked FROM jobs j WHERE j.id=?", (key,)).fetchone()
                if not row:
                    raise LookupError("job not found")
                if not row["parked"]:
                    raise ValueError(f"job is {row['status']}; only a job waiting on a usage limit can switch provider")
                if row["provider"] == provider:
                    raise ValueError(f"the job already runs on {provider}")
                db.execute("UPDATE jobs SET provider=?, model=?, effort=?, reason=?, not_before=NULL WHERE id=?",
                           (provider, model, effort, reason if reason is not None else row["reason"], key))
                seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM job_events WHERE job_id=?", (key,)).fetchone()[0]
                db.execute(
                    "INSERT INTO job_events (job_id,seq,kind,data,created_at) VALUES (?,?,?,?,?)",
                    (key, seq + 1, "retry", json.dumps({"attempt": execution(row), "not_before": None,
                                                        "switched_from": row["provider"], "switched_to": provider}),
                     stamp),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        job = self.get(key)
        assert job is not None
        return job

    def keep_waiting(self, job_id: UUID | str) -> bool:
        """#10 [Keep waiting]: close the open notice of a parked job (the job keeps its retry time)."""
        with closing(self._connect()) as db:
            return db.execute("UPDATE usage_notices SET dismissed_at=? WHERE job_id=? AND dismissed_at IS NULL",
                              (now().isoformat(), str(job_id))).rowcount > 0

    def list_attempts(self, job_id: UUID | str) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt, id", (str(job_id),))
            return [dict(row) for row in rows]

    # --- stream events (audit trail + live replay) ---

    def set_draft(self, job_id: UUID | str, text: str) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE jobs SET draft_text=? WHERE id=?", (text, str(job_id)))

    def add_event(self, job_id: UUID | str, seq: int, kind: str, data: dict) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "INSERT OR REPLACE INTO job_events (job_id,seq,kind,data,created_at) VALUES (?,?,?,?,?)",
                (str(job_id), seq, kind, json.dumps(data, ensure_ascii=False), now().isoformat()),
            )

    def last_event_seq(self, job_id: UUID | str) -> int:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT MAX(seq) AS seq FROM job_events WHERE job_id=?", (str(job_id),)
            ).fetchone()
        return int(row["seq"] or 0)

    def last_tool_before_retry(self, job_id: UUID | str) -> dict | None:
        """The last ``tool`` event (data dict) of the attempt that ended in the newest ``retry`` event,
        i.e. between the previous ``retry`` event (or the start) and the newest one."""
        key = str(job_id)
        with closing(self._connect()) as db:
            seqs = [r[0] for r in db.execute(
                "SELECT seq FROM job_events WHERE job_id=? AND kind='retry' ORDER BY seq DESC LIMIT 2", (key,))]
            if not seqs:
                return None
            row = db.execute(
                "SELECT data FROM job_events WHERE job_id=? AND kind='tool' AND seq<? AND seq>? "
                "ORDER BY seq DESC LIMIT 1", (key, seqs[0], seqs[1] if len(seqs) > 1 else 0)).fetchone()
        try:
            return json.loads(row["data"]) if row else None
        except ValueError:
            return None

    def list_events(self, job_id: UUID | str, after: int = 0, limit: int = 2000) -> list[dict]:
        """Events with ``seq > after`` in order, each as ``{seq, kind, created_at, **data}``."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT seq, kind, data, created_at FROM job_events WHERE job_id=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (str(job_id), after, limit),
            ).fetchall()
        events = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except ValueError:
                data = {}
            events.append(
                {**data, "seq": row["seq"], "kind": row["kind"], "created_at": row["created_at"]}
            )
        return events

    def pending_jobs(self, bot_id: UUID | str) -> list[Job]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE bot_id=? AND status IN (?,?) ORDER BY created_at, id",
                (str(bot_id), JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchall()
        return [self._job(row) for row in rows]

    def routine_active(self, routine_id: UUID | str) -> bool:
        with closing(self._connect()) as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM jobs WHERE routine_id=? AND status IN (?,?) LIMIT 1",
                    (str(routine_id), JobStatus.QUEUED.value, JobStatus.RUNNING.value),
                ).fetchone()
            )

    def routine_last_start(self, routine_id: UUID | str) -> datetime | None:
        """When the routine's latest run actually started (or was queued, if it has not started)."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT MAX(COALESCE(started_at, created_at)) AS at FROM jobs WHERE routine_id=?",
                             (str(routine_id),)).fetchone()
        return datetime.fromisoformat(row["at"]) if row and row["at"] else None

    def create_bot(
        self,
        name: str,
        instructions: str,
        default_provider: str,
        reason: str | None,
        *,
        model: str | None = None,
        icon: str = "",
        description: str = "",
        effort: str | None = None,
        cli_access: str | None = None,
    ) -> Bot:
        stamp = now()
        bot_id = uuid4()
        with closing(self._connect()) as db:
            used = [r[0] for r in db.execute("SELECT avatar_seed FROM bots WHERE avatar_seed IS NOT NULL")]
        bot = Bot(
            id=bot_id,
            name=name,
            instructions=instructions,
            default_provider=default_provider,
            model=model or default_model(default_provider),
            effort=effort or default_effort(default_provider),
            cli_access=cli_access or "full",
            icon=icon or DEFAULT_ICON,
            description=description,
            avatar_seed=avatars.default_character(bot_id, used),
            reason=reason,
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            with closing(self._connect()) as db:
                db.execute(
                    "INSERT INTO bots (id,name,instructions,default_provider,reason,created_at,"
                    "updated_at,model,icon,description,effort,cli_access,avatar_seed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(bot.id),
                        name,
                        instructions,
                        default_provider,
                        reason,
                        stamp.isoformat(),
                        stamp.isoformat(),
                        bot.model,
                        bot.icon,
                        description,
                        bot.effort,
                        bot.cli_access,
                        bot.avatar_seed,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("bot name already exists") from exc
        return bot

    def get_bot(self, bot_id: UUID | str) -> Bot | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM bots WHERE id=?", (str(bot_id),)).fetchone()
        return _model(Bot, row, BOT_DATES)

    def find_bot(self, ref: str) -> Bot | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM bots WHERE id=? OR name=?", (ref, ref)).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT bots.* FROM bot_aliases JOIN bots ON bots.id = bot_aliases.bot_id"
                    " WHERE bot_aliases.alias=?",
                    (ref,),
                ).fetchone()
        return _model(Bot, row, BOT_DATES)

    def list_bots(self) -> list[Bot]:
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT bots.*, {self._ACTIVITY_SELECT}, {self._UNREAD_SELECT}"
                " FROM bots LEFT JOIN bot_reads r ON r.bot_id = bots.id"
                " ORDER BY name COLLATE NOCASE, name"
            ).fetchall()
        return [self._bot_with_unread(row) for row in rows]

    def self_peers(self, bot_id, limit: int = 20):
        """Bounded peer details for SELF.md, without unread-message subqueries."""
        with closing(self._connect()) as db:
            count = db.execute("SELECT COUNT(*) FROM bots WHERE id != ?", (str(bot_id),)).fetchone()[0]
            rows = db.execute(
                f"SELECT id, name, model, default_provider, description, {self._ACTIVITY_SELECT} "
                "FROM bots WHERE id != ? ORDER BY name COLLATE NOCASE, name LIMIT ?",
                (str(bot_id), limit),
            ).fetchall()
        return rows, count

    def job_assignment_events(self, job_id, limit: int = 5):
        with closing(self._connect()) as db:
            count = db.execute("SELECT COUNT(*) FROM assignment_events WHERE job_id=?", (str(job_id),)).fetchone()[0]
            rows = db.execute("SELECT assignment_id, kind FROM assignment_events WHERE job_id=? ORDER BY id LIMIT ?",
                              (str(job_id), limit)).fetchall()
        return rows, count

    _ACTIVITY_SELECT = (
        "(SELECT CASE WHEN SUM(jobs.status='running') > 0 THEN 'running'"
        " WHEN COUNT(*) > 0 THEN 'queued' END FROM jobs WHERE jobs.bot_id = bots.id"
        " AND jobs.status IN ('queued','running')) AS activity"
    )

    # unread = bot-originated, not archived, newer than the read watermark; a usage-limit notice
    # whose job is no longer parked is hidden from the chat (list_messages), so it never counts
    _UNREAD_WHERE = (
        "m.bot_id = bots.id AND (m.role IN ('assistant','system') OR (m.role='user' AND m.source='inbox'"
        " AND EXISTS (SELECT 1 FROM agent_messages_x a WHERE (a.job_id=m.job_id OR a.sender_message_id=m.id)"
        " AND a.from_bot=m.bot_id))"
        " OR (m.role='user' AND m.source='assignment' AND m.sender LIKE 'to %'))"
        " AND m.archived_at IS NULL AND m.hidden=0"
        " AND m.id > COALESCE(r.last_read_message_id, 0)"
        " AND NOT EXISTS (SELECT 1 FROM usage_notices n JOIN jobs j ON j.id=n.job_id WHERE n.message_id=m.id"
        f" AND NOT ({PARKED_SQL} AND n.attempt=j.attempts))"
    )
    _UNREAD_SELECT = (
        f"(SELECT COUNT(*) FROM messages m WHERE {_UNREAD_WHERE}) AS unread_count,"
        f" (SELECT MIN(m.created_at) FROM messages m WHERE {_UNREAD_WHERE}) AS unread_first,"
        " r.manual_unread_at AS manual_unread_at, COALESCE(r.revision, 0) AS read_revision"
    )

    @staticmethod
    def _bot_with_unread(row: sqlite3.Row) -> Bot:
        values = dict(row)
        first, manual = values.pop("unread_first"), values.pop("manual_unread_at")
        values["marked_unread"] = bool(manual)
        since = first or manual
        if first and manual:
            since = min(first, manual)
        values["unread_since"] = since
        return _model(Bot, values, (*BOT_DATES, "unread_since"))

    def bot_unread(self, bot_id: UUID | str) -> Bot | None:
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT bots.*, {self._ACTIVITY_SELECT}, {self._UNREAD_SELECT} FROM bots"
                " LEFT JOIN bot_reads r ON r.bot_id = bots.id WHERE bots.id=?",
                (str(bot_id),),
            ).fetchone()
        return self._bot_with_unread(row) if row else None

    def mark_read(
        self, bot_id: UUID | str, up_to: int | None = None, revision: int | None = None,
        explicit: bool = False,
    ) -> bool:
        """Advance the read watermark. Returns False if the request was stale and ignored.

        The whole mutation (watermark and manual flag) is rejected when ``revision`` does
        not match the current one, so a read computed before a "mark as unread" (another
        tab, an older poll) can neither clear the flag nor move the watermark past the
        rewound position. A manual unread is only cleared by an explicit open that saw
        the current revision. The watermark never moves backwards and never past the
        newest message of the bot.
        """
        key = str(bot_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM bot_reads WHERE bot_id=?", (key,)).fetchone()
                current = row["revision"] if row else 0
                manual = row["manual_unread_at"] if row else None
                if revision is not None and revision != current:
                    db.rollback()
                    return False
                if manual and not (explicit and revision == current):
                    db.rollback()
                    return False
                newest = db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages WHERE bot_id=?", (key,)
                ).fetchone()[0]
                target = newest if up_to is None else min(up_to, newest)
                watermark = max(row["last_read_message_id"] if row else 0, target)
                db.execute(
                    "INSERT INTO bot_reads (bot_id, last_read_message_id, read_at, manual_unread_at, revision)"
                    " VALUES (?,?,?,NULL,?) ON CONFLICT(bot_id) DO UPDATE SET"
                    " last_read_message_id=excluded.last_read_message_id, read_at=excluded.read_at,"
                    " manual_unread_at=NULL",
                    (key, watermark, now().isoformat(), current),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return True

    def mark_unread(self, bot_id: UUID | str) -> None:
        """Rewind the watermark to just before the latest bot message (never forward)
        and set the manual flag, so bots without any bot messages still show as unread."""
        key = str(bot_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM bot_reads WHERE bot_id=?", (key,)).fetchone()
                latest = db.execute(
                    "SELECT MAX(id) FROM messages WHERE bot_id=? AND role IN ('assistant','system')"
                    " AND archived_at IS NULL",
                    (key,),
                ).fetchone()[0]
                existing = row["last_read_message_id"] if row else 0
                watermark = min(existing, latest - 1) if latest else existing
                db.execute(
                    "INSERT INTO bot_reads (bot_id, last_read_message_id, manual_unread_at, revision)"
                    " VALUES (?,?,?,1) ON CONFLICT(bot_id) DO UPDATE SET"
                    " last_read_message_id=excluded.last_read_message_id,"
                    " manual_unread_at=excluded.manual_unread_at, revision=bot_reads.revision+1",
                    (key, max(watermark, 0), now().isoformat()),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def update_bot(self, bot_id: UUID | str, **fields: str | None) -> Bot | None:
        if not fields.keys() <= BOT_FIELDS:
            raise ValueError("unknown bot field")
        fields["updated_at"] = now().isoformat()
        assignments = ",".join(f"{key}=?" for key in fields)
        try:
            with closing(self._connect()) as db:
                db.execute(
                    f"UPDATE bots SET {assignments} WHERE id=?", (*fields.values(), str(bot_id))
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("bot name already exists") from exc
        return self.get_bot(bot_id)

    def delete_bot(self, bot_id: UUID | str) -> list[Job]:
        """Delete a bot, its messages, routines and aliases; cancel its queued jobs.

        Returns jobs of this bot that were running so the caller can stop them.
        Job records themselves are kept as history.
        """
        key = str(bot_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=? WHERE bot_id=? AND status=?",
                (JobStatus.CANCELLED.value, now().isoformat(), "bot deleted", key, JobStatus.QUEUED.value),
            )
            running = db.execute(
                "SELECT * FROM jobs WHERE bot_id=? AND status=?", (key, JobStatus.RUNNING.value)
            ).fetchall()
            db.execute("DELETE FROM messages WHERE bot_id=?", (key,))
            db.execute("DELETE FROM routines WHERE bot_id=?", (key,))
            db.execute("DELETE FROM watcher_runs WHERE watcher_id IN (SELECT id FROM watchers WHERE bot_id=?)", (key,))
            db.execute("DELETE FROM watchers WHERE bot_id=?", (key,))
            db.execute("DELETE FROM bot_aliases WHERE bot_id=?", (key,))
            db.execute("DELETE FROM bot_reads WHERE bot_id=?", (key,))
            self.sections.forget_bot(db, key)
            db.execute("DELETE FROM working_snapshots WHERE bot_id=?", (key,))
            # 0.11.0: its usage notices, own action rules (global rules have bot_id NULL), approvals and gate log
            for table in ("usage_notices", "action_rules", "approvals", "gate_log"):
                db.execute(f"DELETE FROM {table} WHERE bot_id=?", (key,))
            # 0.10.0: its open assignments are dropped (kept as history) and its wake-ups cancelled
            dropped = [row["id"] for row in db.execute(
                "SELECT id FROM assignments WHERE bot_id=? AND status NOT IN ('done','dropped')", (key,))]
            db.execute("UPDATE assignments SET status='dropped', status_note='bot deleted', closed_at=?, updated_at=?, "
                       "version=version+1, updated_by='user' "
                       "WHERE bot_id=? AND status NOT IN ('done','dropped')", (now().isoformat(), now().isoformat(), key))
            for assignment_id in dropped:
                assignment_event(db, assignment_id, "status", "dropped: bot deleted", actor="user")
            db.execute("UPDATE wakeups SET status='cancelled' WHERE bot_id=? AND status='pending'", (key,))
            db.execute("DELETE FROM bots WHERE id=?", (key,))
            db.commit()
        return [self._job(row) for row in running]

    def issue_agent_token(self, job: Job) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET agent_token_hash=? WHERE id=? AND bot_id=?", (digest, str(job.id), str(job.bot_id)))
            db.execute("INSERT INTO agent_tokens VALUES (?,?,?)", (digest, str(job.id), str(job.bot_id)))
            if job.tasks_seen_event is not None:
                # 0.11.4: the prompt built for this run showed task changes up to this event
                db.execute("UPDATE bots SET tasks_seen_event=MAX(tasks_seen_event, ?) WHERE id=?",
                           (job.tasks_seen_event, str(job.bot_id)))
            db.commit()
        return token

    def valid_desk_agent_token(self, job_id: str, bot_id: UUID, token: str) -> bool:
        """Accept a running job or a job finished within the inbox delivery window."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        current = now()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT agent_token_hash, status, finished_at FROM jobs WHERE id=? AND bot_id=?",
                (job_id, str(bot_id)),
            ).fetchone()
        if not row or not row["agent_token_hash"] or not secrets.compare_digest(row["agent_token_hash"], digest):
            return False
        if row["status"] == JobStatus.RUNNING.value:
            return True
        if row["finished_at"] and row["status"] in {status.value for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.NEEDS_LOGIN, JobStatus.INCOMPLETE)}:
            finished = datetime.fromisoformat(row["finished_at"])
            # an inbox file from a bot is accepted for 60 s after its job ended (the live
            # agent-token endpoints stop accepting the token at job end)
            return current - timedelta(seconds=60) < finished <= current
        return False

    def agent_token_job(self, token: str) -> Job | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self._connect()) as db:
            row = db.execute("SELECT j.* FROM agent_tokens t JOIN jobs j ON j.id=t.job_id AND j.bot_id=t.bot_id WHERE t.token_hash=? AND j.status='running'", (digest,)).fetchone()
        return self._job(row)

    def revoke_agent_token(self, job_id: UUID) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM agent_tokens WHERE job_id=?", (str(job_id),))

    def enqueue_agent_message(self, sender: Bot, target: Bot, text: str, origin_id: str | None, since: str,
                              inbox_caps: tuple[int, int] | None = None, *, mode: str = "steer",
                              can_steer: Callable[[Job], bool] | None = None) -> tuple[Message, Job, dict]:
        """Send *text* from *sender* to *target* (bot-msg, desk message, the message-agent API).

        *mode* "steer" (the default, 0.11.10): when *target* is running a turn its engine can take a
        message in (*can_steer*), and *target* lets bots steer it (``bot_steer``), the message joins that
        turn the way an owner's steer does (delivered in order after the owner's own). The agent message
        then has no turn of its own: its job_id is ``steer:<id>`` and ``steered_into`` names the turn; its
        reply is not copied back (the turn belongs to the receiver's own task), so the receiver answers
        with a message of its own. If the turn ends before the message reaches it, it becomes a queued
        inbox turn like any other (JobStore._queue_steers). Never steered into: a turn that is itself
        answering a bot (inbox), so a third bot's text never lands in that reply. Otherwise, and for
        *mode* "queue", the message is a new queued inbox turn whose reply is copied back to *sender*.

        Hops: a message sent from a job continues the exchange that reached that job as a queued inbox turn,
        or, to the bot that steered into that job, that pair's exchange; at most 3 deep. The first message back to the bot whose steer
        reached the job is that steer's answer and takes the steer's hop (a queued turn's reply is free in
        the same way), so ask, answer, follow-up, answer... allows three asks. The item returned carries
        ``delivery``: "steer", "start" (the receiver was idle) or "queue" (behind its other work)."""
        job_id, message_id, stamp = str(uuid4()), str(uuid4()), now().isoformat()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows: list[sqlite3.Row] = []
                if origin_id:
                    origin = db.execute("SELECT bot_id FROM jobs WHERE id=?", (origin_id,)).fetchone()
                    if not origin or origin["bot_id"] != str(sender.id):
                        raise ValueError("invalid originating job")
                    rows = db.execute("SELECT * FROM agent_messages_x WHERE job_id=? OR steered_into=? "
                                      "ORDER BY created_at DESC, seq DESC", (origin_id, origin_id)).fetchall()
                answered = next((r for r in rows if r["steered_into"] == origin_id and r["from_bot"] == str(target.id)
                                 and r["reply_to"] is None and not db.execute(
                                     "SELECT 1 FROM agent_message_links WHERE reply_to=? LIMIT 1", (r["id"],)).fetchone()),
                                None)
                if answered is not None:
                    hop, thread, reply_to = answered["hop"], answered["thread_id"], answered["id"]
                else:
                    # a steer into the origin job belongs to its own task, so only that pair's exchange
                    # continues from it; a queued inbox turn's chain continues to any bot (as before)
                    parent = max((r for r in rows if r["job_id"] == origin_id or r["from_bot"] == str(target.id)),
                                 key=lambda r: r["hop"], default=None)
                    hop = parent["hop"] + 1 if parent else 1
                    thread, reply_to = (parent["thread_id"] if parent else None), None
                if hop > 3:
                    raise ValueError("maximum message hop depth reached: three messages back and forth "
                                     "in one exchange; carry on with what you have")
                if db.execute("SELECT 1 FROM agent_messages WHERE from_bot=? AND to_bot=? AND created_at>?", (str(sender.id), str(target.id), since)).fetchone():
                    raise ValueError("message rate limit reached")
                if inbox_caps:
                    _inbox_limit(db, inbox_caps, from_bot=str(sender.id))
                running = None
                if mode == "steer" and getattr(target, "bot_steer", True):
                    row = db.execute(
                        "SELECT * FROM jobs WHERE bot_id=? AND status='running' AND source!='inbox' "
                        "AND NOT EXISTS (SELECT 1 FROM agent_messages a WHERE a.job_id=jobs.id) "
                        "ORDER BY source IN ('chat','api') DESC, started_at LIMIT 1", (str(target.id),)).fetchone()
                    if row is not None and (can_steer is None or can_steer(self._job(row))):
                        running = row
                if running is not None:
                    cursor = db.execute(
                        "INSERT INTO messages (bot_id,role,sender,source,text,created_at,steer_job_id,steer_status) "
                        "VALUES (?,'user',?,'inbox',?,?,?,'pending')",
                        (str(target.id), f'bot {sender.name}', text, stamp, running["id"]))
                    target_row = cursor.lastrowid
                    sent = db.execute("INSERT INTO messages (bot_id,role,sender,source,text,created_at) "
                                      "VALUES (?,'user',?,'inbox',?,?)",
                                      (str(sender.id), f'to {target.name}', text, stamp)).lastrowid
                    db.execute("INSERT INTO agent_messages (id,from_bot,to_bot,body,job_id,thread_id,hop,created_at) "
                               "VALUES (?,?,?,?,?,?,?,?)", (message_id, str(sender.id), str(target.id), text,
                                                            f"steer:{message_id}", thread or message_id, hop, stamp))
                    db.execute("INSERT INTO agent_message_links (message_id,steered_into,reply_to,sender_message_id,"
                               "target_message_id) VALUES (?,?,?,?,?)", (message_id, running["id"], reply_to, sent, target_row))
                    delivery, job = "steer", running
                else:
                    busy = db.execute("SELECT 1 FROM jobs WHERE bot_id=? AND status IN ('queued','running') LIMIT 1",
                                      (str(target.id),)).fetchone()
                    prompt = f"(from bot {sender.name}) {text}"
                    db.execute("INSERT INTO jobs (id,prompt,status,provider,created_at,bot_id,reason,source,model,effort,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (job_id, prompt, 'queued', target.default_provider, stamp, str(target.id), target.reason, 'inbox', target.model, target.effort or default_effort(target.default_provider), _priority('inbox')))
                    target_row = db.execute("INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at) VALUES (?,?,?,?,?,?,?)", (str(target.id), 'user', f'bot {sender.name}', 'inbox', text, job_id, stamp)).lastrowid
                    sent = db.execute("INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at) VALUES (?,?,?,?,?,?,?)", (str(sender.id), 'user', f'to {target.name}', 'inbox', text, job_id, stamp)).lastrowid
                    db.execute("INSERT INTO agent_messages (id,from_bot,to_bot,body,job_id,thread_id,hop,created_at) "
                               "VALUES (?,?,?,?,?,?,?,?)", (message_id, str(sender.id), str(target.id), text, job_id,
                                                            thread or job_id, hop, stamp))
                    db.execute("INSERT INTO agent_message_links (message_id,reply_to,sender_message_id,target_message_id) "
                               "VALUES (?,?,?,?)", (message_id, reply_to, sent, target_row))
                    delivery, job = ("queue" if busy else "start"), db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if origin_id:
                    db.execute("INSERT INTO agent_message_origin (message_id, origin_job) VALUES (?,?)",
                               (message_id, origin_id))
                row = db.execute("SELECT * FROM messages WHERE id=?", (target_row,)).fetchone()
                db.commit()
            except BaseException:
                db.rollback()
                raise
        item = self.get_agent_message(message_id)
        item["delivery"] = delivery
        return _model(Message, row, ("created_at",)), self._job(job), item

    def create_agent_message(self, from_bot: UUID, to_bot: UUID, body: str, job_id: UUID,
                             thread_id: str, hop: int) -> dict:
        message_id = str(uuid4())
        with closing(self._connect()) as db:
            db.execute("INSERT INTO agent_messages (id,from_bot,to_bot,body,job_id,thread_id,hop,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (message_id, str(from_bot), str(to_bot), body, str(job_id),
                        thread_id, hop, now().isoformat()))
        return self.get_agent_message(message_id)

    def outstanding_bot_replies(self, job_id: UUID | str) -> list[str]:
        """Names of the bots whose reply to a message sent from turn *job_id* has not arrived or has not
        woken that turn's bot yet (0.11.12). A message that became a turn of its own is outstanding until
        its reply is copied back and has woken the bot; a steered one until the other bot answers with a
        message of its own."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT m.job_id, m.id, o.woke_at, b.name FROM agent_message_origin o "
                "JOIN agent_messages_x m ON m.id=o.message_id LEFT JOIN bots b ON b.id=m.to_bot "
                "WHERE o.origin_job=? ORDER BY m.created_at", (str(job_id),)).fetchall()
            names = []
            for row in rows:
                if row["job_id"].startswith("steer:"):
                    answered = db.execute("SELECT 1 FROM agent_message_links WHERE reply_to=? LIMIT 1",
                                          (row["id"],)).fetchone()
                    if answered is None:
                        names.append(row["name"] or "another bot")
                elif row["woke_at"] is None:
                    names.append(row["name"] or "another bot")
        return names

    def unwoken_replies(self, bot_id: UUID | str, since: str) -> list[dict]:
        """Replies (turns of their own, finished) to messages *bot_id* sent from a turn after *since*
        that have not woken it: one dict each (message id, origin job, the other bot's name, how the
        reply ended, its text)."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT m.id, o.origin_job, b.name AS target, j.status, j.error, j.failure_kind, j.reply_text, j.id AS reply_job "
                "FROM agent_message_origin o JOIN agent_messages m ON m.id=o.message_id "
                "JOIN jobs j ON j.id=m.job_id LEFT JOIN bots b ON b.id=m.to_bot "
                "WHERE m.from_bot=? AND o.woke_at IS NULL AND m.created_at>? "
                "AND j.status NOT IN ('queued','running') ORDER BY m.created_at", (str(bot_id), since)).fetchall()
        return [dict(row) for row in rows]

    def claim_reply_wake(self, message_id: str, decision: str | None = None) -> bool:
        """Mark a reply as having woken its asker; False if it already had (a reply wakes once)."""
        with closing(self._connect()) as db:
            return db.execute("UPDATE agent_message_origin SET woke_at=?, wake_decision=? "
                              "WHERE message_id=? AND woke_at IS NULL",
                              (now().isoformat(), decision, message_id)).rowcount > 0

    def set_reply_wake_decision(self, message_id: str, decision: str) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE agent_message_origin SET wake_decision=? WHERE message_id=?", (decision, message_id))

    # a message steered into a running turn has no job of its own: "sent" until that turn reads it,
    # then "delivered"; its reply is never the receiver's turn's reply (that turn is the receiver's task)
    _AGENT_SELECT = ("SELECT m.*, j.status AS job_status, CASE WHEN j.id IS NULL THEN NULL ELSE j.reply_text END AS reply, "
                     "j.finished_at, (SELECT t.steer_status FROM messages t WHERE t.id=m.target_message_id) AS steer_status "
                     "FROM agent_messages_x m LEFT JOIN jobs j ON j.id=m.job_id")

    @staticmethod
    def _agent_item(row: sqlite3.Row) -> dict:
        item = dict(row)
        status, steer = item.pop("job_status"), item.pop("steer_status")
        item.pop("seq", None)
        if status is None and item["job_id"].startswith("steer:"):
            item["status"] = "delivered" if steer == "delivered" else "sent"
        else:
            item["status"] = AGENT_STATUS.get(status, status)
        item["mode"] = "steer" if item.get("steered_into") or item["job_id"].startswith("steer:") else "queue"
        return item

    def list_agent_messages(self, bot: str | None = None) -> list[dict]:
        query = self._AGENT_SELECT
        args = ()
        if bot:
            query += " WHERE m.from_bot=? OR m.to_bot=?"
            args = (bot, bot)
        query += " ORDER BY m.created_at DESC LIMIT 500"
        with closing(self._connect()) as db:
            rows = db.execute(query, args).fetchall()
        return [self._agent_item(row) for row in rows]

    def get_agent_message(self, message_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute(self._AGENT_SELECT + " WHERE m.id=?", (message_id,)).fetchone()
        return None if row is None else self._agent_item(row)

    def agent_message_for_job(self, job_id: UUID | str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM agent_messages_x WHERE job_id=?", (str(job_id),)).fetchone()
        return dict(row) if row else None

    def recent_pair_message(self, from_bot: UUID, to_bot: UUID, since: str) -> bool:
        with closing(self._connect()) as db:
            return bool(db.execute("SELECT 1 FROM agent_messages WHERE from_bot=? AND to_bot=? AND created_at>? LIMIT 1",
                                   (str(from_bot), str(to_bot), since)).fetchone())

    def attachment_references(self) -> dict[str, set[str]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT bot_id, attachments FROM messages").fetchall()
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(row["bot_id"], set()).update(json.loads(row["attachments"]))
        return result

    def enqueue_message(self, bot: Bot, prompt: str, text: str, reason: str | None,
                        source: str, sender: str | None, routine_id: UUID | None,
                        attachment_ids: list[str] | None, *,
                        routine_scan: tuple[Routine, datetime, datetime] | None = None,
                        notice: str | None = None,
                        bridge_hop: tuple[str, int] | None = None,
                        inbox_caps: tuple[int, int] | None = None,
                        priority: int | None = None,
                        hook: Callable[[sqlite3.Connection, Job], None] | None = None) -> tuple[Message, Job]:
        """Queue a turn. *routine_scan* = (routine as scanned, new scan cursor, fired minute)
        advances the routine and clears its owed catch-up in the same transaction (RoutineChanged
        if the routine changed since the scan), and *notice* adds a system line before the turn.
        *bridge_hop* = (thread id, hop) stores an inbound bridge hop with the turn
        (BridgeHopConflict if the thread's stored hop reached it meanwhile). *inbox_caps* applies
        the hourly inbox caps (InboxLimit). *hook* runs in the same transaction after the inserts
        (watchers: quota, dedupe and state); an exception from it rolls everything back."""
        job = Job(id=uuid4(), prompt=prompt, provider=bot.default_provider,
                  status=JobStatus.QUEUED, created_at=now(), bot_id=bot.id,
                  reason=reason, source=source, routine_id=routine_id, model=bot.model,
                  effort=bot.effort or default_effort(bot.default_provider))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if routine_scan and routine_id:
                    scanned, cursor, fired = routine_scan
                    if not self._routine_unchanged(db, scanned):
                        raise RoutineChanged(str(routine_id))
                    db.execute("UPDATE routines SET scan_cursor=?, last_run_at=?, pending_run=NULL WHERE id=?",
                               (cursor.isoformat(), fired.isoformat(), str(routine_id)))
                if inbox_caps:
                    _inbox_limit(db, inbox_caps, sender=sender)
                if bridge_hop:
                    thread_id, hop = bridge_hop
                    row = db.execute("SELECT hop FROM bridge_threads WHERE thread_id=?", (thread_id,)).fetchone()
                    if row and row["hop"] >= hop:
                        raise BridgeHopConflict(thread_id)
                    db.execute("INSERT INTO bridge_threads VALUES (?,?,?) ON CONFLICT(thread_id) DO UPDATE "
                               "SET hop=excluded.hop, updated_at=excluded.updated_at", (thread_id, hop, now().isoformat()))
                if notice:
                    db.execute("INSERT INTO messages (bot_id,role,source,text,created_at) VALUES (?,?,?,?,?)",
                               (str(bot.id), "system", source, notice, now().isoformat()))
                db.execute("INSERT INTO jobs (id,prompt,status,provider,created_at,bot_id,reason,source,routine_id,model,effort,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (str(job.id), prompt, job.status.value, job.provider, job.created_at.isoformat(), str(bot.id), reason, source, str(routine_id) if routine_id else None, bot.model, job.effort, _priority(source) if priority is None else priority))
                cursor = db.execute("INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at,attachments) VALUES (?,?,?,?,?,?,?,?)",
                                    (str(bot.id), "user", sender, source, text, str(job.id), now().isoformat(), json.dumps(attachment_ids or [])))
                if sender is None and source in ("chat", "api"):
                    # 0.11.10: the owner's message supersedes a queued (or cap-held) chat continuation
                    db.execute("UPDATE jobs SET status='cancelled', finished_at=?, error='superseded by your message' "
                               "WHERE bot_id=? AND status='queued' AND auto_kind='chat'", (now().isoformat(), str(bot.id)))
                row = db.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
                if hook:
                    hook(db, job)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return _model(Message, row, ("created_at",)), job

    def add_message(
        self,
        bot_id: UUID | str,
        role: str,
        text: str,
        *,
        sender: str | None = None,
        source: str = "chat",
        job_id: UUID | str | None = None,
        attachments: list[str] | None = None,
    ) -> Message:
        stamp = now().isoformat()
        with closing(self._connect()) as db:
            cursor = db.execute(
                "INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at,attachments)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (str(bot_id), role, sender, source, text, str(job_id) if job_id else None, stamp, json.dumps(attachments or [])),
            )
            row = db.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _model(Message, row, ("created_at",))

    @staticmethod
    def _message_rows(db: sqlite3.Connection, where: str, args: tuple, limit: int) -> list[sqlite3.Row]:
        """The newest *limit* (-1: all) rows matching *where*, oldest first, with the reply-verified and
        agent-sender columns."""
        # a succeeded reply without a BOT-OFFICE-STATUS line, or a done without evidence of how it was checked
        # (0.12.16; a task's `bot-task done --evidence` counts), is "unverified"; a done with evidence shows it
        given = "EXISTS(SELECT 1 FROM assignment_events e WHERE e.job_id=j.id AND e.kind='evidence')"
        return db.execute(
            "SELECT m.*, COALESCE(m.role='assistant' AND m.kind!='say' AND j.status='succeeded' AND (j.outcome='unverified'"
            f" OR (j.outcome='done' AND j.verified=0 AND NOT {given})), 0) AS unverified,"
            " CASE WHEN m.role='assistant' AND m.kind!='say' AND j.status='succeeded' AND j.outcome='done'"
            " THEN COALESCE(j.evidence, (SELECT e.note FROM assignment_events e WHERE e.job_id=j.id AND e.kind='evidence'"
            " ORDER BY e.id DESC LIMIT 1)) END AS evidence,"
            " COALESCE((SELECT a.from_bot FROM agent_messages a WHERE a.job_id=m.job_id LIMIT 1),"
            " (SELECT a.from_bot FROM agent_messages_x a WHERE a.sender_message_id=m.id OR a.target_message_id=m.id"
            " LIMIT 1)) AS agent_from"
            f" FROM (SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?) AS m"
            " LEFT JOIN jobs AS j ON j.id=m.job_id ORDER BY m.id",
            (*args, limit),
        ).fetchall()

    def _hydrate(self, db: sqlite3.Connection, bot_id: UUID | str, rows: list[sqlite3.Row]) -> list[Message]:
        """Rows to Messages with their question, usage notice and approval cards."""
        from .questions import unpack
        cards = {row["message_id"]: unpack(row) for row in db.execute(
            "SELECT * FROM questions WHERE bot_id=?", (str(bot_id),))}
        notices = {row["message_id"]: row for row in db.execute(
            f"SELECT n.*, ({PARKED_SQL} AND n.attempt=j.attempts) AS parked FROM usage_notices n "
            "JOIN jobs j ON j.id=n.job_id "
            "WHERE n.bot_id=? AND n.message_id IS NOT NULL", (str(bot_id),))}
        approval_ids = [row["approval_id"] for row in rows if row["approval_id"]]
        approvals = {row["id"]: dict(row) for row in db.execute(
            f"SELECT * FROM approvals WHERE id IN ({','.join('?' * len(approval_ids))})",
            approval_ids)} if approval_ids else {}
        out = []
        for row in rows:
            approval = approvals.get(row["approval_id"]) if row["approval_id"] else None
            card = approval_card(self, approval) if approval else None
            if card is not None:
                out.append(_model(Message, row, ("created_at", "archived_at")).model_copy(update={"approval": card}))
                continue
            notice = notices.get(row["id"])
            if notice is not None and not notice["parked"]:
                continue  # the job started, was switched or cancelled: the notice is gone
            card = {"kind": "usage_limit", "job_id": notice["job_id"], "provider": notice["provider"],
                    "open": not notice["dismissed_at"]} if notice is not None else None
            out.append(_model(Message, row, ("created_at", "archived_at")).model_copy(
                update={"question": cards.get(row["id"]), "notice": card}))
        return out

    def list_messages(
        self, bot_id: UUID | str, limit: int | None = None, *, include_archived: bool = False,
        up_to: int | None = None, include_progress: bool = True, include_hidden: bool | None = None,
    ) -> list[Message]:
        """*include_hidden* (default: same as *include_archived*) also returns Bot-Office machinery rows."""
        where = "bot_id=?" + ("" if include_archived else " AND archived_at IS NULL")
        if not (include_archived if include_hidden is None else include_hidden):
            where += " AND hidden=0"
        if not include_progress:
            where += " AND kind!='say'"
        args: tuple = (str(bot_id),)
        if up_to is not None:
            where, args = where + " AND id<=?", (*args, up_to)
        with closing(self._connect()) as db:
            rows = self._message_rows(db, where, args, limit if limit is not None else -1)
            return self._hydrate(db, bot_id, rows)

    def history_page(
        self, bot_id: UUID | str, limit: int = 50, *, before: int | None = None, after: int | None = None,
        around: int | None = None, include_archived: bool = False,
    ) -> tuple[list[Message], bool, bool]:
        """One contiguous page of a chat (oldest first) by message id: the newest *limit*, the *limit* just
        older than *before*, just newer than *after*, or centred on *around*. Returns (messages, has_older,
        has_newer). Keyset by id, so messages arriving meanwhile can neither duplicate nor skip a row."""
        limit = max(1, min(int(limit), 500))
        where = "bot_id=?" + ("" if include_archived else " AND archived_at IS NULL AND hidden=0")
        base: tuple = (str(bot_id),)
        with closing(self._connect()) as db:
            def ids(extra: str, arg: tuple, order: str, count: int) -> list[int]:
                return [r[0] for r in db.execute(
                    f"SELECT id FROM messages WHERE {where}{extra} ORDER BY id {order} LIMIT ?", (*base, *arg, count))]
            if around is not None:
                half = limit // 2
                picked = ids(" AND id<=?", (around,), "DESC", half + 1) + ids(" AND id>?", (around,), "ASC", limit - half - 1)
            elif before is not None:
                picked = ids(" AND id<?", (before,), "DESC", limit)
            elif after is not None:
                picked = ids(" AND id>?", (after,), "ASC", limit)
            else:
                picked = ids("", (), "DESC", limit)
            if not picked:
                return [], False, False
            low, high = min(picked), max(picked)
            rows = self._message_rows(db, where + " AND id BETWEEN ? AND ?", (*base, low, high), -1)
            older = bool(ids(" AND id<?", (low,), "DESC", 1))
            newer = bool(ids(" AND id>?", (high,), "ASC", 1))
            return self._hydrate(db, bot_id, rows), older, newer

    def search_messages(self, query: str, *, bot_id: UUID | str | None = None, limit: int = 30,
                        before: int | None = None, offset: int = 0) -> tuple[list[dict], bool]:
        """Best-match-first search of chat messages, attachment names and Bot Office status lines, one bot or all
        (FTS5 bm25, LIKE where it is missing)."""
        with closing(self._connect()) as db:
            return chat_history.search(db, query, bot_id=str(bot_id) if bot_id else None,
                                       limit=max(1, min(int(limit), 100)), before=before,
                                       offset=max(0, int(offset)))

    def get_message(self, message_id: int) -> Message | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return _model(Message, row, ("created_at", "archived_at"))

    def job_message(self, bot_id: UUID | str, job_id: UUID | str) -> Message | None:
        """The user message that started *job_id* in this bot's chat (archived or not)."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM messages WHERE bot_id=? AND job_id=? AND role='user' ORDER BY id LIMIT 1",
                (str(bot_id), str(job_id)),
            ).fetchone()
        return _model(Message, row, ("created_at", "archived_at"))

    # --- message edit / rewind ---

    def _seed(self, db: sqlite3.Connection, bot_id: str, before_id: int, text: str) -> RewindSeed:
        """The non-archived chat before *before_id*, excluding progress notes, checked against the seed limits.

        Raises EditError(413) when it cannot be seeded in full; nothing is ever dropped.
        """
        rows = db.execute(
            "SELECT * FROM messages WHERE bot_id=? AND archived_at IS NULL AND kind!='say' AND id<? ORDER BY id",
            (bot_id, before_id),
        ).fetchall()
        messages = [_model(Message, row, ("created_at", "archived_at")) for row in rows]
        chars = len(text) + sum(len(m.text) + 64 for m in messages)
        if chars > SEED_MAX_CHARS:
            raise EditError(413, f"the remaining chat is too long to seed a fresh session ({chars} > {SEED_MAX_CHARS} characters)")
        images = [(m.id, name) for m in messages for name in m.attachments]
        # only real images count against the image caps; an uploaded file is just named in the seed (checked to exist)
        pictures = [item for item in images if not attachment_files.is_file_id(item[1])]
        if len(pictures) > SEED_MAX_IMAGES:
            raise EditError(413, f"the remaining chat has too many images to seed ({len(pictures)} > {SEED_MAX_IMAGES})")
        total = 0
        for _, name in images:
            try:
                length = attachment_files.size(self.path.parent, bot_id, name)
                if not attachment_files.is_file_id(name):
                    total += length
            except (OSError, ValueError) as exc:
                raise EditError(409, f"{'file' if attachment_files.is_file_id(name) else 'image'} {name} of the remaining chat is missing") from exc
        if total > SEED_MAX_IMAGE_BYTES:
            raise EditError(413, "the remaining chat's images are too large to seed")
        return RewindSeed(messages, images)

    def rewind_seed(self, bot_id: UUID | str, before_id: int, text: str = "") -> RewindSeed:
        with closing(self._connect()) as db:
            return self._seed(db, str(bot_id), before_id, text)

    def _editable(self, db: sqlite3.Connection, bot_id: str, message_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row or row["bot_id"] != bot_id or row["archived_at"]:
            raise EditError(404, "message not found")
        if row["steer_job_id"]:
            raise EditError(409, "steering messages cannot be edited")
        if row["role"] != "user" or row["sender"] is not None or row["source"] not in EDITABLE_SOURCES:
            raise EditError(422, "only your own messages can be edited")
        return row

    def check_edit(self, bot_id: UUID | str, message_id: int, text: str) -> RewindSeed:
        """Everything that can be validated without changing state (used before cancelling)."""
        with closing(self._connect()) as db:
            self._editable(db, str(bot_id), message_id)
            return self._seed(db, str(bot_id), message_id, text)

    def edit_message(
        self, bot: Bot, message_id: int, text: str, reason: str | None,
        attachment_ids: list[str],
    ) -> tuple[Message, Job]:
        """Archive *message_id* and everything after it, queue the replacement, and record
        that the bot's provider session must be reset - all in one transaction.

        Rechecks ownership, archive state, the seed and pending jobs under the write lock;
        a job that appeared since the caller's checks makes this fail with 409.
        """
        key = str(bot.id)
        job = Job(id=uuid4(), prompt=text, provider=bot.default_provider,
                  status=JobStatus.QUEUED, created_at=now(), bot_id=bot.id,
                  reason=reason, source="chat", model=bot.model,
                  effort=bot.effort or default_effort(bot.default_provider))
        with self.chat_lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                original = self._editable(db, key, message_id)
                pending = [row["id"] for row in db.execute(
                    "SELECT id FROM jobs WHERE bot_id=? AND status IN ('queued','running') ORDER BY created_at, id",
                    (key,),
                )]
                if pending:
                    raise EditError(409, "a reply is queued or running for this bot", pending)
                self._seed(db, key, message_id, text)
                stamp = now().isoformat()
                # an edited assignment reply re-runs as a turn of the same assignment
                # (a closed one too: the re-run reopens it, see _rewind_assignments)
                linked = db.execute(
                    "SELECT a.* FROM jobs j JOIN assignments a ON a.id=j.assignment_id WHERE j.id=?",
                    (original["job_id"],)).fetchone() if original["source"] == "assignment" else None
                if original["source"] == "assignment" and linked is None:
                    raise EditError(409, "the assignment of this reply is gone")
                turn = None
                if linked is not None:
                    turn = linked["next_turn"] + 1
                    db.execute("UPDATE assignments SET next_turn=? WHERE id=?", (turn, linked["id"]))
                    job = job.model_copy(update={"source": "assignment", "assignment_id": UUID(linked["id"]),
                                                 "assignment_turn": turn})
                db.execute(
                    "INSERT INTO jobs (id,prompt,status,provider,created_at,bot_id,reason,source,model,effort,"
                    "assignment_id,assignment_turn,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(job.id), text, job.status.value, job.provider, job.created_at.isoformat(), key,
                     reason, job.source, bot.model, job.effort, linked["id"] if linked else None, turn,
                     1 if linked else 0),
                )
                cursor = db.execute(
                    "INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at,attachments,edit_of)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, "user", None, original["source"], text, str(job.id), stamp,
                     json.dumps(attachment_ids), message_id),
                )
                new_id = cursor.lastrowid
                # WORKING.md before the first turn being archived that changed it
                restore = db.execute(
                    "SELECT before FROM working_snapshots WHERE bot_id=? AND job_id IN"
                    " (SELECT job_id FROM messages WHERE bot_id=? AND id>=? AND id<? AND archived_at IS NULL"
                    " AND job_id IS NOT NULL) ORDER BY id LIMIT 1",
                    (key, key, message_id, new_id),
                ).fetchone()
                self._rewind_assignments(db, key, message_id, new_id, linked["id"] if linked else None, stamp)
                db.execute(
                    "UPDATE messages SET archived_at=?, archived_by=? WHERE bot_id=? AND id>=? AND id<?"
                    " AND archived_at IS NULL",
                    (stamp, new_id, key, message_id, new_id),
                )
                db.execute(
                    "UPDATE questions SET status='skipped', answer=?, answered_at=? WHERE bot_id=?"
                    " AND status='open' AND message_id IN (SELECT id FROM messages WHERE bot_id=?"
                    " AND archived_by=?)",
                    (json.dumps({"skip": True, "steps": []}), stamp, key, key, new_id),
                )
                # an older pending restore (not yet reconciled) stays unless this edit reaches further back
                db.execute(
                    "UPDATE bots SET session_reset=?, rewind_seed=?, working_restore=COALESCE(?, working_restore)"
                    " WHERE id=?",
                    (new_id, new_id, restore["before"] if restore else None, key),
                )
                row = db.execute("SELECT * FROM messages WHERE id=?", (new_id,)).fetchone()
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return _model(Message, row, ("created_at", "archived_at")), job

    @staticmethod
    def _rewind_assignments(db: sqlite3.Connection, bot_id: str, first: int, new_id: int, rerun: str | None,
                            stamp: str) -> None:
        """Reconcile assignments whose turns an edit archives. Their checkpoints have no snapshots,
        so each one is marked "rewound" (paused, with a note; the re-run's own assignment stays
        active), including one the archived turns completed or that was closed since: it reopens,
        and wake-ups the archived turns scheduled are cancelled. No turn of this bot is
        queued here (edit_message refuses while one is), so no continuation survives the edit."""
        jobs = [r["job_id"] for r in db.execute(
            "SELECT job_id FROM messages WHERE bot_id=? AND id>=? AND id<? AND archived_at IS NULL "
            "AND job_id IS NOT NULL", (bot_id, first, new_id))]
        if not jobs:
            return
        marks = ",".join("?" * len(jobs))
        db.execute(f"UPDATE wakeups SET status='cancelled' WHERE status='pending' AND created_by_job IN ({marks})", jobs)
        touched = {r["assignment_id"] for r in db.execute(
            f"SELECT DISTINCT assignment_id FROM jobs WHERE id IN ({marks}) AND assignment_id IS NOT NULL", jobs)}
        for assignment_id in sorted(touched | ({rerun} if rerun else set())):
            row = db.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                continue
            if assignment_id == rerun:
                status, note = "active", "rewound: re-running the edited reply"
            else:
                status, note = "open", "rewound by an edit; its checkpoint may describe removed turns"
            # the owner's edit rewinds it: a versioned change by the user
            db.execute("UPDATE assignments SET status=?, status_note=?, updated_at=?, closed_at=NULL, stalled_at=NULL, "
                       "dismissed_at=NULL, missing_status=0, verifying=0, reported_status=NULL, reported_note=NULL, "
                       "reported_job=NULL, version=version+1, updated_by='user' WHERE id=?",
                       (status, note, stamp, assignment_id))
            assignment_event(db, assignment_id, "rewound", note, actor="user")

    def pending_session_reset(self, bot_id: UUID | str) -> int | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT session_reset FROM bots WHERE id=?", (str(bot_id),)).fetchone()
        return row["session_reset"] if row else None

    def bots_with_session_reset(self) -> list[str]:
        with closing(self._connect()) as db:
            return [row["id"] for row in db.execute("SELECT id FROM bots WHERE session_reset IS NOT NULL")]

    def pending_working_restore(self, bot_id: UUID | str) -> str | None:
        """WORKING.md content to restore with the pending session reset, if it needs one."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT working_restore FROM bots WHERE id=?", (str(bot_id),)).fetchone()
        return row["working_restore"] if row else None

    def complete_session_reset(self, bot_id: UUID | str, generation: int) -> None:
        """Clear the reset marker, unless a newer edit has replaced it meanwhile."""
        with closing(self._connect()) as db:
            db.execute("UPDATE bots SET session_reset=NULL, working_restore=NULL WHERE id=? AND session_reset=?",
                       (str(bot_id), generation))

    def snapshot_working(self, bot_id: UUID | str, job_id: UUID | str, before: str) -> bool:
        """Record WORKING.md as it was before *job_id* changed it. Returns False (nothing
        recorded) if that turn has been archived by an edit; the caller then must not save
        the turn's WORKING.md. Callers hold chat_lock so an edit cannot commit in between."""
        key, job_key = str(bot_id), str(job_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                archived = db.execute(
                    "SELECT 1 FROM messages WHERE bot_id=? AND job_id=? AND role='user'"
                    " AND archived_at IS NOT NULL LIMIT 1",
                    (key, job_key),
                ).fetchone()
                if archived or not db.execute("SELECT 1 FROM bots WHERE id=?", (key,)).fetchone():
                    db.rollback()
                    return False
                db.execute(
                    "INSERT INTO working_snapshots (bot_id,job_id,before,created_at) VALUES (?,?,?,?)",
                    (key, job_key, before, now().isoformat()),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return True

    def pending_rewind_seed(self, bot_id: UUID | str) -> int | None:
        """The edit whose seed the bot's next provider turn must still carry, if any."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT rewind_seed FROM bots WHERE id=?", (str(bot_id),)).fetchone()
        return row["rewind_seed"] if row else None

    def complete_rewind_seed(self, bot_id: UUID | str, generation: int) -> None:
        """Clear the seed obligation, unless a newer edit has replaced it meanwhile."""
        with closing(self._connect()) as db:
            db.execute("UPDATE bots SET rewind_seed=NULL WHERE id=? AND rewind_seed=?", (str(bot_id), generation))

    def say(self, bot_id: UUID | str, job_id: UUID | str, text: str) -> Message:
        """Post a progress note atomically with the running and rate-limit checks."""
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= SAY_MAX_CHARS:
            raise EditError(400, f"text must be 1..{SAY_MAX_CHARS} characters after stripping")
        text = text.strip()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                job = db.execute("SELECT * FROM jobs WHERE id=? AND bot_id=?", (str(job_id), str(bot_id))).fetchone()
                archived = db.execute("SELECT 1 FROM messages WHERE bot_id=? AND job_id=? AND role='user' AND archived_at IS NOT NULL", (str(bot_id), str(job_id))).fetchone()
                if not job or job["status"] != "running" or archived:
                    raise EditError(409, "job is not running or its turn is archived")
                count, last = db.execute("SELECT COUNT(*), MAX(created_at) FROM messages WHERE job_id=? AND kind='say'", (str(job_id),)).fetchone()
                if count >= SAY_MAX_PER_JOB:
                    raise EditError(429, "progress note limit reached; try again in a new turn")
                stamp = now()
                if last:
                    next_at = datetime.fromisoformat(last) + timedelta(seconds=SAY_INTERVAL_SECONDS)
                    if stamp < next_at:
                        raise EditError(429, f"progress notes are rate limited; try again at {next_at.isoformat()}")
                cursor = db.execute("INSERT INTO messages (bot_id,role,source,text,job_id,created_at,kind) VALUES (?,?,?,?,?,?,'say')", (str(bot_id), 'assistant', job['source'], text, str(job_id), stamp.isoformat()))
                row = db.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return _model(Message, row, ("created_at",))

    def publish_outcome(
        self, bot_id: UUID | str, job_id: UUID | str, role: str, text: str, *,
        sender: str | None = None, source: str = "chat", attachments: list[str] | None = None,
        hidden: bool = False, cut_chars: int = 0,
    ) -> Message | None:
        """Add a job's result to *bot_id*'s chat unless the turn that asked for it has been
        archived by an edit (checked and inserted in one transaction). Returns None if
        dropped; the job record keeps the reply either way."""
        ids = attachments or []
        if len(ids) > 6 or any(not attachment_files.is_valid_id(name) for name in ids):
            raise ValueError("invalid attachment ids")
        key, job_key = str(bot_id), str(job_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                archived = db.execute(
                    "SELECT 1 FROM messages WHERE bot_id=? AND job_id=? AND role='user'"
                    " AND archived_at IS NOT NULL LIMIT 1",
                    (key, job_key),
                ).fetchone()
                if archived or not db.execute("SELECT 1 FROM bots WHERE id=?", (key,)).fetchone():
                    db.rollback()
                    return None
                cursor = db.execute(
                    "INSERT INTO messages (bot_id,role,sender,source,text,job_id,created_at,attachments,hidden,cut_chars)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (key, role, sender, source, text, job_key, now().isoformat(), json.dumps(ids), int(hidden),
                     max(0, cut_chars)),
                )
                row = db.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return _model(Message, row, ("created_at", "archived_at"))

    def get_meta(self, key: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as db:
            db.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def delete_meta(self, key: str) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM meta WHERE key=?", (key,))

    @staticmethod
    def _routine(row: sqlite3.Row | None) -> Routine | None:
        if row is not None:
            row = dict(row) | {"enabled": bool(row["enabled"])}
        return _model(Routine, row, ("created_at", "last_run_at", "scan_cursor", "pending_run"))

    def create_routine(
        self,
        bot_id: UUID | str,
        cron: str,
        prompt: str,
        enabled: bool,
        name: str | None = None,
        catch_up: str = "once",
        created_by: str = "user",
        max_bot_routines: int | None = None,
    ) -> Routine:
        """*max_bot_routines* (bot-made routines) is checked in the insert's transaction."""
        routine_id = str(uuid4())
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if max_bot_routines is not None and db.execute(
                    "SELECT COUNT(*) FROM routines WHERE bot_id=? AND created_by='bot'", (str(bot_id),)
                ).fetchone()[0] >= max_bot_routines:
                    raise OverflowError(f"limit of {max_bot_routines} bot-made routines reached")
                db.execute(
                    "INSERT INTO routines (id,bot_id,cron,prompt,enabled,created_at,name,catch_up,scan_cursor,created_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (routine_id, str(bot_id), cron, prompt, int(enabled), now().isoformat(), name, catch_up,
                     _minute(now()).isoformat(), created_by),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.get_routine(routine_id)

    def get_routine(self, routine_id: UUID | str) -> Routine | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM routines WHERE id=?", (str(routine_id),)).fetchone()
        return self._routine(row)

    def list_routines(self, bot_id: UUID | str | None = None) -> list[Routine]:
        query, args = "SELECT * FROM routines", ()
        if bot_id is not None:
            query, args = query + " WHERE bot_id=?", (str(bot_id),)
        with closing(self._connect()) as db:
            rows = db.execute(query + " ORDER BY created_at, id", args).fetchall()
        return [self._routine(row) for row in rows]

    def update_routine(self, routine_id: UUID | str, **fields: object) -> Routine | None:
        if not fields:
            return self.get_routine(routine_id)
        if not fields.keys() <= {"cron", "prompt", "enabled", "last_run_at", "name", "bot_id", "catch_up", "scan_cursor"}:
            raise ValueError("unknown routine field")
        if ("cron" in fields or fields.get("enabled")) and "scan_cursor" not in fields:
            # a new schedule or a re-enabled routine starts now; time spent disabled is not "missed"
            fields["scan_cursor"] = _minute(now())
        if "bot_id" in fields:
            bot_id = fields["bot_id"]
            if bot_id is None or not self.get_bot(bot_id):
                raise ValueError("routine bot not found")
            fields["bot_id"] = str(bot_id if isinstance(bot_id, UUID) else UUID(str(bot_id)))
        values = [
            int(v)
            if k == "enabled"
            else v.isoformat()
            if isinstance(v, datetime)
            else v
            for k, v in fields.items()
        ]
        with closing(self._connect()) as db:
            db.execute(
                f"UPDATE routines SET {','.join(f'{k}=?' for k in fields)}, version=version+1 WHERE id=?",
                (*values, str(routine_id)),
            )
        return self.get_routine(routine_id)

    @staticmethod
    def _routine_unchanged(db: sqlite3.Connection, scanned: Routine) -> bool:
        """The routine still exists, is enabled and was neither edited nor scanned since *scanned*."""
        row = db.execute("SELECT enabled, version, scan_cursor FROM routines WHERE id=?", (str(scanned.id),)).fetchone()
        if not row or not row["enabled"] or row["version"] != scanned.version:
            return False
        cursor = datetime.fromisoformat(row["scan_cursor"]) if row["scan_cursor"] else None
        return cursor == scanned.scan_cursor

    def advance_routine(self, routine: Routine, cursor: datetime, bot_id: UUID | str | None = None,
                        notice: str | None = None, *, pending: datetime | None = None) -> bool:
        """Move a routine's scan cursor without starting a turn (optionally with a chat notice),
        and set (or clear) its owed catch-up run. Nothing changes, and False is returned, if the
        routine was edited, disabled, deleted or scanned since *routine* was read."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if not self._routine_unchanged(db, routine):
                    db.rollback()
                    return False
                db.execute("UPDATE routines SET scan_cursor=?, pending_run=? WHERE id=?",
                           (cursor.isoformat(), pending.isoformat() if pending else None, str(routine.id)))
                if notice and bot_id and db.execute("SELECT 1 FROM bots WHERE id=?", (str(bot_id),)).fetchone():
                    db.execute("INSERT INTO messages (bot_id,role,source,text,created_at) VALUES (?,?,?,?,?)",
                               (str(bot_id), "system", "routine", notice, now().isoformat()))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return True

    # --- 0.11.0 usage, budgets and timelines ---

    def allow_budget(self, bot_id: UUID | str, turns: int, minutes: int, tokens: int) -> int:
        """"Allow +N today": raise today's unattended budget and release the bot's held jobs
        (returns how many). The extra lapses at 00:00 New York time."""
        stamp = now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT budget_extra_json FROM bots WHERE id=?", (str(bot_id),)).fetchone()
                if row is None:
                    db.rollback()
                    raise LookupError("bot not found")
                extra = budgets.extra_today(row["budget_extra_json"], stamp)
                extra = {"day": budgets.day_key(stamp), "turns": extra["turns"] + turns,
                         "minutes": extra["minutes"] + minutes, "tokens": extra["tokens"] + tokens}
                db.execute("UPDATE bots SET budget_extra_json=? WHERE id=?", (json.dumps(extra), str(bot_id)))
                released = db.execute("UPDATE jobs SET not_before=NULL, failure_kind=NULL, error=NULL WHERE bot_id=? "
                                      "AND status='queued' AND failure_kind=?", (str(bot_id), failures.BUDGET)).rowcount
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return released

    def usage_by_day(self, days: int = 7) -> list[dict]:
        """Per bot and New York day: all runs and unattended runs (turns, minutes, tokens, cost)."""
        stamp = now()
        out = []
        with closing(self._connect()) as db:
            bots = db.execute("SELECT id, name, daily_budget_json, budget_extra_json, scheduled_runs FROM bots").fetchall()
            for back in range(days):
                since = budgets.day_start(stamp, back)
                until = budgets.next_day(since) if back else stamp
                items = budgets.runs(db, since, until)
                for bot in bots:
                    mine = [item for item in items if item["bot_id"] == bot["id"]]
                    if not mine and back:
                        continue
                    unattended = [item for item in mine if item["source"] in budgets.UNATTENDED]
                    # what the daily budget counts: strict-profile runs, without gate refusals
                    held = budgets.budget_sources(bot["scheduled_runs"])
                    budgeted = [item for item in unattended if item["source"] in held and item["kind"] != "gate"]
                    out.append({"bot_id": bot["id"], "bot_name": bot["name"], "day": budgets.day_key(since),
                                "all": budgets.totals(mine), "unattended": budgets.totals(unattended),
                                "budgeted": budgets.totals(budgeted),
                                **({"budget": budgets.budget_of(bot["daily_budget_json"]),
                                    "extra": budgets.extra_today(bot["budget_extra_json"], stamp)} if not back else {})})
        return out

    def routine_runs(self, routine_id: UUID | str, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as db:
            return [_run_row(db, row) for row in db.execute(
                "SELECT * FROM jobs WHERE routine_id=? ORDER BY created_at DESC LIMIT ?", (str(routine_id), limit))]

    def assignment_timeline(self, assignment_id: UUID | str) -> list[dict]:
        """Turns, assignment events and approvals of one assignment, oldest first."""
        key = str(assignment_id)
        with closing(self._connect()) as db:
            items = [{"type": "job", "at": row["created_at"], **_run_row(db, row)} for row in db.execute(
                "SELECT * FROM jobs WHERE assignment_id=?", (key,))]
            items += [{"type": "event", "at": row["created_at"], "kind": row["kind"], "note": row["note"],
                       "job_id": row["job_id"]} for row in db.execute(
                "SELECT * FROM assignment_events WHERE assignment_id=?", (key,))]
            items += [{"type": "approval", "at": row["created_at"], "approval_id": row["id"], "status": row["status"],
                       "summary": row["summary"], "job_id": row["job_id"]} for row in db.execute(
                "SELECT * FROM approvals WHERE assignment_id=? OR job_id IN (SELECT id FROM jobs WHERE assignment_id=?)",
                (key, key))]
        return sorted(items, key=lambda item: item["at"] or "")

    def delete_routine(self, routine_id: UUID | str) -> bool:
        with closing(self._connect()) as db:
            return db.execute("DELETE FROM routines WHERE id=?", (str(routine_id),)).rowcount > 0
