"""0.12.39 stall sweeper: stalled work wakes its owner, then its section lead. Model-free.

A started task (active, waiting on a bot, waiting on the owner or blocked) with no progress for
``BOT_OFFICE_STALL_SWEEP_HOURS`` (N, default 3), or a bot whose latest turn ended incomplete or failed N
hours ago with nothing run since, is a stall. Each stall moves through two strikes, at most once each:

1. after N hours without progress: the owning bot gets one automatic turn (a chat wake-up, so a paused
   task stays paused) that says what is stuck and asks it to move it, retry an expired card's step once,
   ask the owner with bot-office-ask, or say in one line what it needs;
2. after N more hours still without progress: the owner's section lead gets one automatic turn about it,
   and the owner's chat says so.

Progress (a task's ``last_progress_at`` moving, or any newer turn of the bot) starts a new stall: the
strikes count again. Not swept: tasks that are only open (never started, or stopped by the owner),
bots that are working (a turn queued or running), have an approval card pending or a wake-up pending,
bots in the Retired or Hidden section, and bots on the sweep's exclude list. Every wake goes through
``Assignments._fire``, so chat-only bots, auto-continue off and the wake-up and automatic-turn caps still
apply, and a wake-up that does not run says why in the chat. At most ``BOT_OFFICE_STALL_SWEEP_PER_HOUR``
wakes (default 6) are queued an hour, across all bots.

The lead of a bot: the sweep config's ``leads`` map (bot id -> lead bot id, ``"*"`` = the default lead),
else the bot named "<first word of its name> Lead (BO)" (MS Data Pipeline Engineer (BO) -> MS Lead (BO)),
else the default lead, else the app-admin bot; never the bot itself. The config is the meta key
``stall_sweep`` (``GET``/``PUT /v1/stall-sweep``, master token).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from uuid import uuid4

from .cron import to_cron_time
from .domain import CLOSED_STATUSES
from .rules import untrusted
from .store import JobStore, assignment_event, now

META_KEY = "stall_sweep"
SWEPT_TASK_STATUSES = ("active", "waiting_on_bot", "waiting_on_user", "blocked")
SWEPT_JOB_STATUSES = ("incomplete", "failed")
SKIPPED_SECTIONS = ("Retired", "Hidden")
LOOKBACK_HOURS = 72  # an older stalled turn is history, not something to wake a bot for


class StallSweepError(ValueError):
    pass


def _table(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS stall_sweeps (subject TEXT PRIMARY KEY, bot_id TEXT NOT NULL, "
               "marker TEXT NOT NULL, strike INTEGER NOT NULL, last_at TEXT NOT NULL, lead_id TEXT)")


def config(store: JobStore) -> dict:
    try:
        saved = json.loads(store.get_meta(META_KEY) or "{}")
    except ValueError:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    leads = saved.get("leads") if isinstance(saved.get("leads"), dict) else {}
    exclude = saved.get("exclude") if isinstance(saved.get("exclude"), list) else []
    return {"leads": {str(k): str(v) for k, v in leads.items()}, "exclude": [str(x) for x in exclude]}


def update(store: JobStore, *, leads: dict[str, str] | None = None, exclude: list[str] | None = None) -> dict:
    """Replace the leads map and/or the exclude list. Names or ids; saved as ids (StallSweepError if one
    is not a bot, or a bot is its own lead)."""
    current = config(store)

    def resolve(ref: str) -> str:
        bot = store.find_bot(str(ref))
        if bot is None:
            raise StallSweepError(f"bot not found: {ref}")
        return str(bot.id)

    if leads is not None:
        mapped: dict[str, str] = {}
        for key, value in leads.items():
            source = "*" if key == "*" else resolve(key)
            target = resolve(value)
            if source == target:
                raise StallSweepError(f"a bot cannot be its own lead: {key}")
            mapped[source] = target
        current["leads"] = mapped
    if exclude is not None:
        current["exclude"] = sorted({resolve(ref) for ref in exclude})
    store.set_meta(META_KEY, json.dumps(current, sort_keys=True))
    return current


def lead_of(db: sqlite3.Connection, bot: sqlite3.Row, cfg: dict) -> sqlite3.Row | None:
    def get(bot_id: str | None) -> sqlite3.Row | None:
        if not bot_id or bot_id == bot["id"]:
            return None
        return db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()

    found = get(cfg["leads"].get(bot["id"]))
    if found is None:
        prefix = (bot["name"].split() or [""])[0]
        if prefix and not bot["name"].startswith(f"{prefix} Lead"):
            row = db.execute("SELECT id FROM bots WHERE name=?", (f"{prefix} Lead (BO)",)).fetchone()
            found = get(row["id"] if row else None)
    if found is None:
        found = get(cfg["leads"].get("*"))
    if found is None:
        row = db.execute("SELECT id FROM bots WHERE app_admin=1 AND id!=? ORDER BY created_at LIMIT 1",
                         (bot["id"],)).fetchone()
        found = get(row["id"] if row else None)
    return found


def _skipped_bots(db: sqlite3.Connection, cfg: dict) -> set[str]:
    marks = ",".join("?" for _ in SKIPPED_SECTIONS)
    rows = db.execute(f"SELECT m.bot_id FROM bot_section_members m JOIN bot_sections s ON s.id=m.section_id "
                      f"WHERE s.name IN ({marks})", SKIPPED_SECTIONS).fetchall()
    return {row["bot_id"] for row in rows} | set(cfg["exclude"])


def _busy(db: sqlite3.Connection, bot_id: str) -> bool:
    """Working or already waiting on something that will wake it: a turn queued or running, an
    approval card pending, or a wake-up pending."""
    return bool(
        db.execute("SELECT 1 FROM jobs WHERE bot_id=? AND status IN ('queued','running') LIMIT 1", (bot_id,)).fetchone()
        or db.execute("SELECT 1 FROM approvals WHERE bot_id=? AND status='pending' LIMIT 1", (bot_id,)).fetchone()
        or db.execute("SELECT 1 FROM wakeups WHERE bot_id=? AND status='pending' LIMIT 1", (bot_id,)).fetchone())


def _when(stamp: str) -> str:
    return to_cron_time(datetime.fromisoformat(stamp)).strftime("%-d %b %-I:%M %p ET")


def _stalls(db: sqlite3.Connection, cutoff: datetime, stamp: datetime) -> list[dict]:
    """Every current stall: subject, bot row, marker (what progress would change), since, description."""
    from .assignments import _quoted  # the user's title quoted, a bot's delimited
    out: list[dict] = []
    marks = ",".join("?" for _ in SWEPT_TASK_STATUSES)
    for row in db.execute(f"SELECT * FROM assignments WHERE status IN ({marks}) "
                          "AND COALESCE(last_progress_at, created_at)<?", (*SWEPT_TASK_STATUSES, cutoff.isoformat())):
        since = row["last_progress_at"] or row["created_at"]
        state = row["status"].replace("_", " ")
        note = f" ({row['status_note'][:200]})" if row["status_note"] else ""
        out.append({"subject": f"task:{row['id']}", "bot_id": row["bot_id"], "marker": since, "since": since,
                    "assignment": row["id"],
                    "what": f"your task {_quoted(row)} (id {row['id'][:8]})",
                    "lead_what": f"its task {_quoted(row)} (id {row['id'][:8]})",
                    "state": f"It is {state}{note}" if not note else
                             f"It is {state}, with this status note: {untrusted('task status note', row['status_note'][:200])}",
                    "check": f"bot-task show {row['id'][:8]}; bot-task approvals"})
    oldest = (stamp - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    for bot in db.execute("SELECT id FROM bots").fetchall():
        job = db.execute("SELECT * FROM jobs WHERE bot_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                         (bot["id"],)).fetchone()
        if job is None or job["status"] not in SWEPT_JOB_STATUSES:
            continue
        ended = job["finished_at"] or job["created_at"]
        if not ended or ended >= cutoff.isoformat() or ended < oldest:
            continue
        if job["assignment_id"]:
            task = db.execute("SELECT status FROM assignments WHERE id=?", (job["assignment_id"],)).fetchone()
            if task is not None and task["status"] not in CLOSED_STATUSES:
                continue  # the task's own stall covers it
        said = (job["outcome"] or job["error"] or "").strip()
        state = (f"It ended {job['status']}" + (f"; its status line: {untrusted('last turn status', said[:300])}"
                                                if said else ""))
        out.append({"subject": f"job:{job['id']}", "bot_id": bot["id"], "marker": job["id"], "since": ended,
                    "assignment": None,
                    "what": f"your last turn (job {job['id'][:8]}, {_when(ended)})",
                    "lead_what": f"its last turn (job {job['id'][:8]}, {_when(ended)})",
                    "state": state, "check": "bot-task approvals; bot-task list"})
    return out


def _owner_prompt(item: dict, hours: float, age: float) -> tuple[str, str]:
    prompt = (
        f"Stall check from Bot-Office (an automatic, model-free sweep; not a message from the owner). "
        f"{item['what'][0].upper()}{item['what'][1:]} has had no progress since {_when(item['since'])} "
        f"({age:.0f} h). {item['state']}.\n"
        f"First look at its live state ({item['check']}). Then do the one thing that moves it: take the next "
        "step if you can; if an approval card it needed expired, retry that step once so a fresh card is "
        "raised; if it needs the owner's decision, ask with bot-office-ask so it reaches Needs you (not only "
        "in chat); if it waits on another bot, message that bot. If nothing can move it now, say in one line "
        f"what it needs. If there is still no progress in {hours:g} h, Bot-Office tells your section lead. "
        "End with BOT-OFFICE-STATUS.")
    return prompt, f"Stall check: no progress for {age:.0f} h"


def _lead_prompt(item: dict, owner: str, hours: float, age: float) -> tuple[str, str]:
    prompt = (
        f"Stall escalation from Bot-Office (an automatic, model-free sweep; not a message from the owner). "
        f"{owner} has made no progress on {item['lead_what']} for {age:.0f} h. {item['state']}. "
        f"Bot-Office woke {owner} about it {hours:g} h ago, and nothing has moved since.\n"
        f"As its section lead: look at {owner}'s status and chat, unblock it if you can (answer it or message "
        "it), or raise what it needs to the owner with bot-office-ask. Don't redo its work yourself. Say in "
        "one or two lines what you did, and end with BOT-OFFICE-STATUS.")
    return prompt, f"Stall escalation: {owner}, no progress for {age:.0f} h"


def _line(db: sqlite3.Connection, bot_id: str, text: str) -> None:
    db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments) VALUES (?,'system','wakeup',?,?,'[]')",
               (bot_id, text, now().isoformat()))


def _wake(assignments, db: sqlite3.Connection, bot_id: str, note: str, custom: tuple[str, str]) -> int:
    wakeup_id = str(uuid4())
    db.execute("INSERT INTO wakeups (id,bot_id,assignment_id,note,status,created_at) VALUES (?,?,NULL,?,'pending',?)",
               (wakeup_id, bot_id, note[:200], now().isoformat()))
    row = db.execute("SELECT * FROM wakeups WHERE id=?", (wakeup_id,)).fetchone()
    return assignments._fire(db, row, custom=custom)


def sweep(store: JobStore, assignments, hours: float, *, per_hour: int = 6, stamp: datetime | None = None) -> dict:
    """One pass. Returns {"woken": n, "escalated": n, "skipped": n}. Never raises for one bad item."""
    counts = {"woken": 0, "escalated": 0, "skipped": 0}
    if hours <= 0:
        return counts
    stamp = stamp or now()
    cutoff = stamp - timedelta(hours=hours)
    cfg = config(store)
    with closing(store._connect()) as db:
        _table(db)
        db.commit()
        skipped = _skipped_bots(db, cfg)
        items = _stalls(db, cutoff, stamp)
    for item in sorted(items, key=lambda item: item["since"]):
        with closing(assignments._tx()) as db:
            try:
                done = _step(assignments, db, item, cfg, skipped, hours, per_hour, stamp, cutoff)
                db.commit()
            except Exception:
                db.rollback()
                import logging
                logging.getLogger(__name__).exception("stall sweep failed for %s", item["subject"])
                done = "skipped"
        if done == "cap":
            break
        counts[done] += 1
    return counts


def _step(assignments, db: sqlite3.Connection, item: dict, cfg: dict, skipped: set[str], hours: float,
          per_hour: int, stamp: datetime, cutoff: datetime) -> str:
    bot = db.execute("SELECT * FROM bots WHERE id=?", (item["bot_id"],)).fetchone()
    if bot is None or bot["id"] in skipped or _busy(db, bot["id"]):
        return "skipped"
    record = db.execute("SELECT * FROM stall_sweeps WHERE subject=?", (item["subject"],)).fetchone()
    fresh = record is None or record["marker"] != item["marker"]
    if not fresh and (record["strike"] >= 2 or record["last_at"] >= cutoff.isoformat()):
        return "skipped"  # this stall had both strikes, or its first is less than N hours old
    if db.execute("SELECT COUNT(*) FROM stall_sweeps WHERE last_at>=?",
                  ((stamp - timedelta(hours=1)).isoformat(),)).fetchone()[0] >= per_hour:
        return "cap"
    age = (stamp - datetime.fromisoformat(item["since"])).total_seconds() / 3600
    if fresh:
        if item["assignment"]:
            db.execute("UPDATE assignments SET stalled_at=COALESCE(stalled_at, ?), dismissed_at=NULL WHERE id=?",
                       (stamp.isoformat(), item["assignment"]))
            assignment_event(db, item["assignment"], "stalled", f"no progress for {age:.0f} h; owner woken")
        _line(db, bot["id"], f"Stall check: {item['what'].replace('your ', '', 1)} has had no progress for "
                             f"{age:.0f} h, so Bot-Office woke {bot['name']} to look at it.")
        _wake(assignments, db, bot["id"], "stall check", _owner_prompt(item, hours, age))
        db.execute("INSERT INTO stall_sweeps (subject,bot_id,marker,strike,last_at) VALUES (?,?,?,1,?) "
                   "ON CONFLICT(subject) DO UPDATE SET marker=excluded.marker, strike=1, last_at=excluded.last_at, "
                   "lead_id=NULL", (item["subject"], bot["id"], item["marker"], stamp.isoformat()))
        return "woken"
    lead = lead_of(db, bot, cfg)
    if lead is None:
        _line(db, bot["id"], f"Stall check: still no progress on {item['what'].replace('your ', '', 1)} after "
                             f"{age:.0f} h, and no section lead is set to tell (Settings: stall sweep leads).")
    else:
        _line(db, bot["id"], f"Stall check: still no progress after {age:.0f} h, so Bot-Office told {lead['name']}.")
        _wake(assignments, db, lead["id"], f"stall escalation: {bot['name']}",
              _lead_prompt(item, bot["name"], hours, age))
    db.execute("UPDATE stall_sweeps SET strike=2, last_at=?, lead_id=? WHERE subject=?",
               (stamp.isoformat(), lead["id"] if lead else None, item["subject"]))
    return "escalated"
