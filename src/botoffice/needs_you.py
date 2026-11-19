"""0.10.0 Needs-you: what is waiting on Joshua, computed from the database (no model calls).

Items: assignments that ask for him (waiting on him, blocked, stalled, past due), failed
unattended turns and bots that failed the same way three times in a row. Each item id names its
state, so a resolved item disappears and a new state (or a new failure) comes back as a new id.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from . import continuation
from .assignments import utc_time
from .catalog import ENGINE_LABELS
from .cron import to_cron_time
from .store import PARKED_SQL, JobStore, approval_card, now

FAILED_WINDOW = timedelta(hours=72)
REPEAT_FAILURES = 3
# failures Joshua must act on wherever they happen; other failures only of turns he did not start
ACTION_KINDS = ("auth", "usage_limit", "gate", "budget")
UNATTENDED = ("routine", "wakeup", "inbox", "api", "trigger")
ASSIGNMENT_ACTIONS = ["reply", "start", "done", "dismiss"]
# #10: a job parked on a usage limit for longer than this gets one notice per attempt
PARK_NOTICE_AFTER = timedelta(minutes=10)
NOTICE_TZ = "America/New_York"
# an assignment waiting on the owner for something to hand over (photos, files) rather than a decision
MATERIALS = re.compile(r"\b(photos?|pictures?|images?|screenshots?|scans?|files?|uploads?|attach\w*)\b", re.I)


def _assignment_state(row: sqlite3.Row, stamp: datetime) -> str | None:
    if row["stalled_at"]:
        return "stalled"
    status, escalate = row["status"], row["escalate"]
    if status == "waiting_on_user":
        return status
    if status == "blocked" and escalate != "never":
        return status
    if status == "waiting_on_bot" and escalate == "always":
        return status
    if row["due_at"] and utc_time(row["due_at"]) < stamp:
        return "overdue"
    return None


def _assignment_ask(row: sqlite3.Row, state: str, bot: str) -> str:
    note = row["status_note"] or ""
    title = row["title"]
    return {
        "waiting_on_user": f'{bot} needs you on "{title}": {note or "see the chat"}',
        "blocked": f'{bot} is blocked on "{title}": {note or "see the chat"}',
        "waiting_on_bot": f'{bot} is waiting on another bot for "{title}"' + (f": {note}" if note else ""),
        "stalled": f'No progress on "{title}" for a while; nudge, reply or stop it',
        "overdue": f'"{title}" is past its due time',
    }[state]


def needs_you(store: JobStore) -> list[dict]:
    stamp = now()
    items: list[dict] = []
    with closing(store._connect()) as db:
        bots = {row["id"]: row for row in db.execute("SELECT id, name, notify FROM bots")}
        for row in db.execute("SELECT * FROM questions WHERE status='open'"):
            bot = bots.get(row["bot_id"])
            if bot:
                prompt = json.loads(row["payload"])["steps"][0]["prompt"]
                items.append({"id": f"q:{row['id']}:open", "kind": "question", "state": "open",
                              "bot_id": row["bot_id"], "bot_name": bot["name"],
                              "notify": bool(bot["notify"]), "question_id": row["id"],
                              "assignment_id": row["assignment_id"],
                              "title": "Question", "ask": f"{bot['name']} asks: {prompt[:120]}",
                              "at": row["created_at"], "actions": ["answer", "skip"]})
        for row in db.execute("SELECT * FROM assignments WHERE status NOT IN ('done','dropped') "
                              "AND dismissed_at IS NULL ORDER BY updated_at DESC"):
            bot = bots.get(row["bot_id"])
            state = _assignment_state(row, stamp)
            if bot is None or state is None:
                continue
            # the newest evidence the bot gave (stale or too short ones too), so the owner can judge it
            given = db.execute("SELECT note FROM assignment_events WHERE assignment_id=? AND kind='evidence' "
                               "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            ask = _assignment_ask(row, state, bot["name"])
            if given and state == "waiting_on_user":
                ask += f' (last evidence given: "{given["note"][:200]}")'
            items.append({
                "id": f"a:{row['id']}:{state}:{row['stalled_at'] or row['updated_at']}",
                "kind": "assignment", "state": state, "bot_id": row["bot_id"], "bot_name": bot["name"],
                "notify": bool(bot["notify"]), "assignment_id": row["id"], "title": row["title"],
                "ask": ask, "at": row["stalled_at"] or row["updated_at"],
                "evidence": given["note"] if given else None, "actions": ASSIGNMENT_ACTIONS,
            })
        # 0.11.4: a task the owner created that its bot dropped, until the owner dismisses it
        for row in db.execute("SELECT * FROM assignments WHERE status='dropped' AND created_by='user' "
                              "AND updated_by LIKE 'bot:%' AND dismissed_at IS NULL AND closed_at>=? "
                              "ORDER BY closed_at DESC LIMIT 50", ((stamp - FAILED_WINDOW).isoformat(),)):
            bot = bots.get(row["bot_id"])
            if bot is None:
                continue
            items.append({
                "id": f"a:{row['id']}:dropped_by_bot:{row['closed_at']}", "kind": "assignment",
                "state": "dropped_by_bot", "bot_id": row["bot_id"], "bot_name": bot["name"],
                "notify": bool(bot["notify"]), "assignment_id": row["id"], "title": row["title"],
                "ask": f'{bot["name"]} dropped your task "{row["title"]}": {row["status_note"] or "no note"}',
                "at": row["closed_at"], "actions": ["dismiss"],
            })
        since = (stamp - FAILED_WINDOW).isoformat()
        # only actionable failures, filtered before the limit: login/usage failures of any turn
        # (assignment turns too, whatever their escalation), others of unattended non-assignment turns
        kinds, sources = ",".join("?" * len(ACTION_KINDS)), ",".join("?" * len(UNATTENDED))
        failed = db.execute(
            "SELECT j.* FROM jobs j WHERE j.status IN ('failed','needs_login') AND j.finished_at>=? "
            "AND j.dismissed_at IS NULL AND j.bot_id IS NOT NULL "
            f"AND (j.status='needs_login' OR j.failure_kind IN ({kinds}) "
            f"OR (j.assignment_id IS NULL AND j.source IN ({sources}))) "
            "AND NOT EXISTS (SELECT 1 FROM jobs k WHERE k.bot_id=j.bot_id AND k.status='succeeded' "
            "AND k.finished_at>j.finished_at) ORDER BY j.finished_at DESC LIMIT 50",
            (since, *ACTION_KINDS, *UNATTENDED)).fetchall()
        for job in failed:
            bot = bots.get(job["bot_id"])
            if bot is None:
                continue
            kind = "needs_login" if job["status"] == "needs_login" else job["failure_kind"]
            reason = (job["error"] or kind or "failed").splitlines()[0][:200]
            items.append({
                "id": f"j:{job['id']}:{job['attempts']}", "kind": "job", "state": "failed", "bot_id": job["bot_id"],
                "bot_name": bot["name"], "notify": bool(bot["notify"]), "job_id": job["id"],
                "assignment_id": job["assignment_id"], "title": f"{job['source'] or 'turn'} failed",
                "ask": f"{bot['name']}'s {job['source'] or ''} turn failed: {reason}", "at": job["finished_at"],
                "actions": ["retry", "dismiss"],
            })
        for bot_id, bot in bots.items():
            last = db.execute(
                "SELECT id, status, failure_kind, dismissed_at FROM jobs WHERE bot_id=? AND status NOT IN "
                "('queued','running','cancelled') ORDER BY finished_at DESC LIMIT ?",
                (bot_id, REPEAT_FAILURES)).fetchall()
            kinds = {row["failure_kind"] for row in last if row["status"] == "failed"}
            if len(last) == REPEAT_FAILURES and len(kinds) == 1 and None not in kinds \
                    and all(row["status"] == "failed" for row in last) and not last[0]["dismissed_at"]:
                kind = kinds.pop()
                items.append({
                    "id": f"r:{bot_id}:{kind}:{last[0]['id']}", "kind": "repeat", "state": "failing",
                    "bot_id": bot_id, "bot_name": bot["name"], "notify": bool(bot["notify"]),
                    "job_id": last[0]["id"], "title": f"{kind} three times",
                    "ask": f"{bot['name']} failed {REPEAT_FAILURES} times in a row ({kind})", "at": None,
                    "actions": ["dismiss"],
                })
        _approvals(db, bots, items, store)
        for row in db.execute(
                f"SELECT n.*, j.not_before, j.assignment_id FROM usage_notices n JOIN jobs j ON j.id=n.job_id "
                f"WHERE {PARKED_SQL} AND n.attempt=j.attempts AND n.dismissed_at IS NULL ORDER BY n.created_at DESC"):
            bot = bots.get(row["bot_id"])
            if bot is None:
                continue
            items.append({
                "id": f"u:{row['job_id']}:{row['attempt']}", "kind": "usage_limit", "state": "parked",
                "bot_id": row["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]),
                "job_id": row["job_id"], "assignment_id": row["assignment_id"], "provider": row["provider"],
                "title": "Waiting on a usage limit",
                "ask": _usage_text(bot["name"], row["provider"], row["not_before"], stamp),
                # "switch" becomes one "Retry on <provider>" button per provider in "options" (the API
                # fills it with the enabled, signed-in, different providers)
                "at": row["created_at"], "actions": ["switch", "keep"], "options": [],
            })
        for row in db.execute("SELECT bot_id, COUNT(*) AS held, MIN(error) AS why, MAX(created_at) AS at FROM jobs "
                              "WHERE status='queued' AND failure_kind='budget' GROUP BY bot_id"):
            bot = bots.get(row["bot_id"])
            if bot is None:
                continue
            items.append({
                "id": f"b:{row['bot_id']}:{row['held']}", "kind": "budget", "state": "held",
                "bot_id": row["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]),
                "title": "Daily budget used",
                "ask": f"{bot['name']} used today's unattended budget; {row['held']} turn(s) wait until tomorrow "
                       f"({(row['why'] or '').removeprefix('held: ')})",
                "at": row["at"], "actions": ["allow"],
            })
        # 0.11.10: automatic turns a cap holds until its window slides; gone once they run or are cancelled
        for row in db.execute("SELECT bot_id, COUNT(*) AS held, MIN(error) AS why, MIN(not_before) AS until, "
                              "MIN(created_at) AS at FROM jobs WHERE status='queued' AND failure_kind=? GROUP BY bot_id",
                              (continuation.HELD,)):
            bot = bots.get(row["bot_id"])
            if bot is None:
                continue
            until = datetime.fromisoformat(row["until"]) if row["until"] else stamp
            items.append({
                "id": f"c:{row['bot_id']}:{row['at']}", "kind": "auto_cap", "state": "held",
                "bot_id": row["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]),
                "title": "Automatic-turn cap reached",
                "ask": f"{bot['name']} stopped automatic work: {(row['why'] or '').removeprefix('held: ')}; "
                       f"{row['held']} turn(s) wait until {to_cron_time(until).strftime('%-I:%M %p')} ET. "
                       "You can raise the cap in Settings (Auto-continue), or reply to it.",
                "at": row["at"], "actions": ["settings"],
            })
        for row in db.execute("SELECT * FROM watchers WHERE enabled=0 AND paused_reason IS NOT NULL"):
            bot = bots.get(row["bot_id"])
            if bot is None:
                continue
            items.append({
                "id": f"w:{row['id']}:{row['last_fired_at']}", "kind": "watcher", "state": "paused",
                "bot_id": row["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]),
                "watcher_id": row["id"], "title": f"Watcher {row['name']} paused",
                "ask": f"{bot['name']}'s watcher \"{row['name']}\" was paused: {row['paused_reason']}",
                "at": row["last_fired_at"], "actions": ["resume"],
            })
    return _annotate(items)


def _annotate(items: list[dict]) -> list[dict]:
    """Give every item a *category* and a *group* (additive keys; ids and actions are unchanged).

    group: one per assignment (its own item plus any question, failed turn or usage notice of the
    same assignment), else one per item, so a client counts an assignment once however many
    things it is waiting on. category: ``materials`` for an assignment waiting on the owner to hand
    something over (photos, files: not a decision, and it stays until they do), else ``action``.
    Incomplete turns are not items at all: they are history with a Retry button in the chat."""
    for item in items:
        item["group"] = f"as:{item['assignment_id']}" if item.get("assignment_id") else item["id"]
        waits = item["kind"] == "assignment" and item["state"] == "waiting_on_user"
        item["category"] = "materials" if waits and MATERIALS.search(item["ask"]) else "action"
    return items


def count_items(items: list[dict]) -> dict[str, int]:
    """What the Needs-you badge shows: distinct groups, and how many of them are only waiting on materials."""
    groups: dict[str, str] = {}
    for item in items:
        group = item.get("group") or item["id"]
        if groups.get(group) != "action":
            groups[group] = item.get("category", "action")
    return {"total": len(groups), "materials": sum(1 for c in groups.values() if c == "materials")}


def _usage_text(bot: str, provider: str, not_before: str, stamp: datetime) -> str:
    from zoneinfo import ZoneInfo

    back = utc_time(not_before)
    minutes = max(0, round((back - stamp).total_seconds() / 60))
    local = back.astimezone(ZoneInfo(NOTICE_TZ))
    clock = local.strftime("%H:%M %Z")  # always with the zone (ET): a bare clock time was ambiguous
    if local.date() != stamp.astimezone(ZoneInfo(NOTICE_TZ)).date():
        clock = f"{local:%a %b} {local.day}, {clock}"
    return (f"{bot} is waiting for {ENGINE_LABELS.get(provider, provider)}'s usage limit to reset "
            f"(back about {clock}, {minutes} min).")


def parked_on_usage(store: JobStore) -> list[sqlite3.Row]:
    """Read helper: bot jobs parked on a usage limit, with *parked_at* (their last attempt's end)."""
    with closing(store._connect()) as db:
        return db.execute(
            "SELECT j.id, j.bot_id, j.provider, j.attempts, j.not_before, "
            "(SELECT MAX(a.finished_at) FROM job_attempts a WHERE a.job_id=j.id) AS parked_at "
            f"FROM jobs j WHERE {PARKED_SQL} AND j.bot_id IS NOT NULL").fetchall()


def post_usage_notices(store: JobStore, stamp: datetime | None = None) -> int:
    """#10 watchdog check (scheduler, once a minute, model-free): ONE notice per job and attempt
    for a job parked on a usage limit for more than PARK_NOTICE_AFTER: a system line in the bot's
    chat plus the Needs-you item. Both disappear when the job starts, switches or is cancelled
    (they are shown only while the job is still parked at that attempt). Never switches anything."""
    stamp = stamp or now()
    posted = 0
    for row in parked_on_usage(store):
        if not row["parked_at"]:
            continue
        back, parked = utc_time(row["not_before"]), utc_time(row["parked_at"])
        if back - parked <= PARK_NOTICE_AFTER or back <= stamp:
            continue
        bot = store.get_bot(row["bot_id"])
        if bot is None:
            continue
        with closing(store._connect()) as db:
            fresh = db.execute(
                "INSERT OR IGNORE INTO usage_notices (job_id,attempt,bot_id,provider,back_at,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (row["id"], row["attempts"], row["bot_id"], row["provider"], row["not_before"],
                 stamp.isoformat())).rowcount
        if not fresh:
            continue  # this attempt already has its notice
        message = store.add_message(bot.id, "system", _usage_text(bot.name, row["provider"], row["not_before"], stamp),
                                    source="api")
        with closing(store._connect()) as db:
            db.execute("UPDATE usage_notices SET message_id=? WHERE job_id=? AND attempt=?",
                       (message.id, row["id"], row["attempts"]))
        posted += 1
    return posted


def _preview(text: str, limit: int = 160) -> str:
    """A one-line preview for the card title and notifications; the card shows the whole text."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit] + "…"


def _approvals(db, bots: dict, items: list[dict], store: JobStore | None = None) -> None:
    """0.11.0: approval cards (Approve once / Deny; Re-run once expired) and tripwire alerts."""
    since = (now() - FAILED_WINDOW).isoformat()
    for row in db.execute("SELECT * FROM approvals WHERE status='pending' OR (status='expired' AND "
                          "resume_job_id IS NULL AND created_at>=?) ORDER BY id DESC LIMIT 50", (since,)):
        bot = bots.get(row["bot_id"])
        if bot is None:
            continue
        expired = row["status"] == "expired"
        items.append({
            "id": f"p:{row['id']}:{row['status']}", "kind": "approval", "state": row["status"],
            "bot_id": row["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]),
            "approval_id": row["id"], "job_id": row["job_id"], "title": f"Approval #{row['id']}",
            # the task this approval's turn belongs to: it is grouped (and counted) with that task's other items
            "assignment_id": row["assignment_id"],
            "class": (approval_card(store, dict(row))["label"] if store is not None else row["preset"]),
            "ask": (f"{bot['name']} asks to run ({row['source']}, {row['preset'] or 'rule'}): {_preview(row['summary'])} "
                    f"({len(row['summary'])} characters, fingerprint {row['input_hash'][:12]}; the whole command is below)"
                    + (" — expired; Re-run approves it now" if expired else "")),
            # the whole action as approved (Sec 5): the grant covers exactly this text
            "command": row["summary"], "fingerprint": row["input_hash"], "length": len(row["summary"]),
            "at": row["created_at"], "actions": ["rerun", "deny"] if expired else ["approve", "deny"],
        })
    for row in db.execute("SELECT * FROM gate_log WHERE preset='secrets' AND effect='never' AND dismissed_at IS NULL "
                          "AND created_at>=? ORDER BY id DESC LIMIT 20", (since,)):
        bot = bots.get(row["bot_id"])
        if bot is None:
            continue
        items.append({
            "id": f"g:{row['id']}", "kind": "alert", "state": "tripwire", "bot_id": row["bot_id"],
            "bot_name": bot["name"], "notify": bool(bot["notify"]), "alert_id": row["id"], "job_id": row["job_id"],
            "title": "Protected file", "ask": f"{bot['name']} tried to touch a protected file (blocked): {row['summary']}",
            "at": row["created_at"], "actions": ["dismiss"],
        })


def dismiss_job(store: JobStore, job_id: str) -> bool:
    with closing(store._connect()) as db:
        return db.execute("UPDATE jobs SET dismissed_at=? WHERE id=? AND status NOT IN ('queued','running')",
                          (now().isoformat(), job_id)).rowcount > 0


def last_desk_job(store: JobStore, desktop: str) -> str | None:
    """The latest bot turn that ran on *desktop* (for "Hand back")."""
    with closing(store._connect()) as db:
        row = db.execute("SELECT id FROM jobs WHERE slot=? AND bot_id IS NOT NULL "
                         "ORDER BY COALESCE(started_at, created_at) DESC LIMIT 1", (desktop,)).fetchone()
    return row["id"] if row else None
