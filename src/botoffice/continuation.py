"""0.11.10: the one continuation system (docs/CONTINUATION.md).

Two places continue a bot without the owner: the chat auto-continue (a turn ended ``not_done``;
``scheduler._auto_continue``) and the task loop (an assignment turn ended ``continue``, or a done
turn gets its check or evidence turn; ``assignments._settle``). Both describe the finished turn as a
``Turn`` and act on ``decide``; neither keeps stop rules or counters of its own. The settings live in
the 0.11.10 ``auto_continue`` meta row. The caps on automatic turns are applied when a job is claimed
(``cap_gate``, in the claim transaction), so a held turn waits for its window instead of being dropped.

Text heuristics (blocked / needs-you phrases, no progress) are ``autocontinue``'s.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import autocontinue
from .cron import to_cron_time

KEY = "auto_continue"
CHAIN_MAX_LIMIT = 50
# jobs.auto_kind: the automatic turns the caps count (owner chat, Start/Reply, routines and bot
# messages are not automatic)
AUTO_KINDS = ("chat", "loop", "verify", "evidence", "wakeup")
HELD = "auto_cap"  # jobs.failure_kind of a turn a cap holds until its window slides
CONTINUE_STATES = ("continue", "not_done")
DEFAULTS: dict[str, object] = {
    "enabled": True,
    "max": 10,  # chat: continuations in a row (the 0.11.10 setting)
    "no_progress": autocontinue.NO_PROGRESS_LIMIT,
    "consecutive": autocontinue.MAX_CONSECUTIVE_CONTINUES,
    "per_bot_day": 20,  # G3 probation; to be raised to 40. None = unlimited
    "per_hour": 20,  # all bots together. None = unlimited
    "evidence": True,  # G4: a task counts as done only with evidence of how it was verified
    "evidence_min": 12,  # the shortest evidence text (characters) that counts
    "hold_hours": 12,  # an automatic turn a cap held longer than this is dropped with a line, not run late
}
ENV = {"no_progress": "BOT_OFFICE_AUTO_NO_PROGRESS", "consecutive": "BOT_OFFICE_AUTO_CONSECUTIVE",
       "per_bot_day": "BOT_OFFICE_AUTO_TURNS_PER_BOT_DAY", "per_hour": "BOT_OFFICE_AUTO_TURNS_PER_HOUR",
       "evidence": "BOT_OFFICE_DONE_NEEDS_EVIDENCE", "evidence_min": "BOT_OFFICE_DONE_EVIDENCE_MIN_CHARS",
       "hold_hours": "BOT_OFFICE_AUTO_HOLD_HOURS"}
BOUNDS = {"max": (1, CHAIN_MAX_LIMIT), "no_progress": (1, 100), "consecutive": (1, 1000),
          "per_bot_day": (1, 100_000), "per_hour": (1, 100_000), "evidence_min": (1, 1000), "hold_hours": (1, 720)}
CAPS = ("per_bot_day", "per_hour")
WINDOWS = {"per_bot_day": timedelta(hours=24), "per_hour": timedelta(hours=1)}
UNLIMITED = "unlimited"


def _check(key: str, value: object) -> object:
    """The stored form of *value* for *key*; ValueError if it is not one."""
    if key in ("enabled", "evidence"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if key in CAPS and (value is None or value == UNLIMITED):
        return None
    low, high = BOUNDS[key]
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        extra = ' or "unlimited"' if key in CAPS else ""
        raise ValueError(f"{key} must be a whole number from {low} to {high}{extra}")
    return value


def defaults() -> dict[str, object]:
    """DEFAULTS with the env defaults applied (read per call; a bad value keeps the default)."""
    out = dict(DEFAULTS)
    for key, name in ENV.items():
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            continue
        value: object = raw
        if key == "evidence":
            value = {"on": True, "off": False}.get(raw, raw)
        elif raw != UNLIMITED:
            try:
                value = int(raw)
            except ValueError:
                continue
        try:
            out[key] = _check(key, value)
        except ValueError:
            continue
    return out


def load(raw: str | None) -> dict[str, object]:
    """Settings from the meta JSON: a saved key wins over the default; a bad one is ignored."""
    settings = defaults()
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key in DEFAULTS:
                try:
                    settings[key] = _check(key, value)
                except ValueError:
                    pass
    return settings


def read(db: sqlite3.Connection) -> dict[str, object]:
    row = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    return load(row[0] if row else None)


def get(store) -> dict[str, object]:
    return load(store.get_meta(KEY))


def chat_limit(settings: dict[str, object]) -> int:
    """The continuations in a row a chat really gets: ``max``, or ``consecutive`` if that is lower
    (an env default can make it so after ``max`` was saved)."""
    return min(int(settings["max"]), int(settings["consecutive"]))


def update(store, changes: dict[str, object]) -> dict[str, object]:
    """Save the given keys (others keep their saved value or default); ValueError names a bad one.
    Held turns are released for a re-check, so a raised cap applies at once."""
    checked = {key: _check(key, value) for key, value in changes.items() if key in DEFAULTS}
    try:
        saved = json.loads(store.get_meta(KEY) or "{}")
    except ValueError:
        saved = {}
    saved = {key: value for key, value in (saved if isinstance(saved, dict) else {}).items() if key in DEFAULTS}
    saved.update(checked)
    after = load(json.dumps(saved))
    if "max" in checked and int(after["max"]) > int(after["consecutive"]):
        # the chat chain also stops at "consecutive": a larger max would never be reached (lowering
        # consecutive alone is fine: GET /v1/settings shows the effective chat limit)
        raise ValueError(f"max ({after['max']}) cannot be more than consecutive ({after['consecutive']}): "
                         "the chat stops at whichever comes first; raise consecutive too")
    store.set_meta(KEY, json.dumps(saved))
    if any(key in CAPS for key in checked):
        release_held(store)
    return get(store)


# --- the decision ---

@dataclass
class Turn:
    """A finished turn, as far as continuing it matters."""
    path: str  # "chat" | "task"
    state: str = "continue"  # what the turn reported (continue, not_done, done, ...)
    kind: str = "continue"  # what would run next: continue | verify | evidence
    # (reply, status reason) of the turns in a row since the owner last spoke that reported
    # continue / not_done, oldest first, the finished turn last (the owner's own turn included)
    run: list[tuple[str, str]] = field(default_factory=list)
    automatic: int = 0  # automatic turns since the owner last spoke; the next one is automatic + 1
    chat_only: bool = False
    question_open: bool = False
    approval_pending: bool = False
    owner_waiting: bool = False
    budget_used: str | None = None  # task: why its own turn/minute budget is used up


@dataclass(frozen=True)
class Decision:
    go: bool
    code: str  # continue | chat_only | off | status | budget | question | approval | owner | needs_user |
    #            no_progress | chain_max | consecutive
    why: str = ""


def decide(settings: dict[str, object], turn: Turn) -> Decision:
    """Whether Bot-Office queues the next automatic turn, and if not, why (shown to the owner)."""
    def stop(code: str, why: str) -> Decision:
        return Decision(False, code, why)

    if turn.chat_only:
        return stop("chat_only", "this bot is chat-only, so it does not continue by itself")
    if not settings["enabled"]:
        return stop("off", "auto-continue is off (Settings, Auto-continue)")
    if turn.kind == "continue" and turn.state not in CONTINUE_STATES:
        return stop("status", f"it reported {turn.state.replace('_', ' ')}")
    if turn.budget_used:
        return stop("budget", turn.budget_used)
    if turn.question_open:
        return stop("question", "a question to you is still open")
    if turn.approval_pending:
        return stop("approval", "an approval is waiting for you")
    # the task loop yields instead: its next turn queues behind the owner's (claim order, G2)
    if turn.owner_waiting and turn.path == "chat":
        return stop("owner", "your new message comes first")
    if turn.kind == "continue" and turn.run:
        reply, reason = turn.run[-1]
        if phrase := autocontinue.needs_user_phrase(reply, reason):
            return stop("needs_user", f'the reply says it is blocked or needs you ("{phrase}")')
        stalled = autocontinue.no_progress_run(turn.run)
        if stalled >= int(settings["no_progress"]):
            return stop("no_progress", f"{stalled} turns in a row with no progress")
    number = turn.automatic + 1
    if turn.path == "chat" and number > int(settings["max"]):
        return stop("chain_max", f"{settings['max']} continuations in a row (the chat limit)")
    if number > int(settings["consecutive"]):
        return stop("consecutive", f"{turn.automatic} automatic turns in a row without a reply from you")
    return Decision(True, "continue")


def chat_stop_line(decision: Decision) -> str:
    if decision.code == "owner":
        return "Not continuing automatically: your new message comes first."
    return f"Stopped continuing automatically: {decision.why}. Reply to keep it going."


def task_pause_note(decision: Decision) -> str:
    return f"paused, not blocked: {decision.why}. Reply to resume."


# --- caps on automatic turns (G3), checked at claim ---

def cap_gate(db: sqlite3.Connection, job_id: str, stamp: datetime) -> tuple[str, datetime] | None:
    """(why, until) when the automatic job *job_id* is over a cap, else None. Counts the automatic
    turns that started in the rolling window; *until* is when the window slides past the oldest."""
    job = db.execute("SELECT bot_id, auto_kind FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None or not job["auto_kind"]:
        return None
    settings = read(db)
    for key in CAPS:
        cap = settings[key]
        if cap is None:
            continue
        window = WINDOWS[key]
        scope, args = ("AND bot_id=?", (job["bot_id"],)) if key == "per_bot_day" else ("", ())
        rows = db.execute(f"SELECT started_at FROM jobs WHERE auto_kind IS NOT NULL AND started_at>=? AND id!=? {scope} "
                          "ORDER BY started_at DESC LIMIT ?",
                          ((stamp - window).isoformat(), job_id, *args, int(cap))).fetchall()
        if len(rows) >= int(cap):
            until = datetime.fromisoformat(rows[-1]["started_at"]) + window + timedelta(seconds=1)
            why = (f"the per-bot cap of {cap} automatic turns a day is reached" if key == "per_bot_day"
                   else f"the all-bots cap of {cap} automatic turns an hour is reached")
            return why, until
    return None


def cap_line(why: str, until: datetime) -> str:
    return (f"Automatic work paused: {why}. The next automatic turn waits until "
            f"{to_cron_time(until).strftime('%-d %b %-I:%M %p')} ET. You can raise the cap in Settings "
            "(Auto-continue), or reply to give it a turn now.")


def hold(db: sqlite3.Connection, job_id: str, why: str, until: datetime, stamp: datetime) -> None:
    """Defer the job to *until* (claim skips it till then) with one chat line per held job."""
    row = db.execute("SELECT bot_id, source, failure_kind FROM jobs WHERE id=?", (job_id,)).fetchone()
    db.execute("UPDATE jobs SET not_before=?, failure_kind=?, error=?, held_at=COALESCE(held_at, ?) WHERE id=?",
               (until.isoformat(), HELD, f"held: {why}", stamp.isoformat(), job_id))
    if row["failure_kind"] != HELD and db.execute("SELECT 1 FROM bots WHERE id=?", (row["bot_id"],)).fetchone():
        db.execute("INSERT INTO messages (bot_id,role,source,text,job_id,created_at,attachments) VALUES (?,?,?,?,?,?,'[]')",
                   (row["bot_id"], "system", row["source"], cap_line(why, until), job_id, stamp.isoformat()))


def stale_hold(db: sqlite3.Connection, job_id: str, stamp: datetime) -> str | None:
    """Why a turn a cap held should not run now that its window slid, else None. A held turn was
    decided before it waited: what changed meanwhile is checked again (auto-continue switched off,
    the bot made chat-only, its task no longer active) and a turn held over ``hold_hours`` is not run
    late."""
    job = db.execute("SELECT j.held_at, j.assignment_id, b.chat_only FROM jobs j LEFT JOIN bots b ON b.id=j.bot_id "
                     "WHERE j.id=? AND j.failure_kind=?", (job_id, HELD)).fetchone()
    if job is None:
        return None
    settings = read(db)
    if not settings["enabled"]:
        return "auto-continue is now off"
    if job["chat_only"]:
        return "the bot is now chat-only"
    if job["assignment_id"]:
        task = db.execute("SELECT status FROM assignments WHERE id=?", (job["assignment_id"],)).fetchone()
        if task is None or task["status"] != "active":
            return "its task is no longer active"
    if job["held_at"] and datetime.fromisoformat(job["held_at"]) + timedelta(hours=int(settings["hold_hours"])) < stamp:
        return f"it waited more than {settings['hold_hours']} hours for a cap"
    return None


def release_held(store) -> int:
    """Let every held turn be checked again at the next claim (after the owner changed a cap)."""
    with closing(store._connect()) as db:
        return db.execute("UPDATE jobs SET not_before=NULL WHERE status='queued' AND failure_kind=?", (HELD,)).rowcount
