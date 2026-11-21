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

from . import continuation, git_guard
from .assignments import utc_time
from .catalog import ENGINE_LABELS
from .cron import to_cron_time
from .store import PARKED_SQL, JobStore, approval_card, now

FAILED_WINDOW = timedelta(hours=72)
REPEAT_FAILURES = 3
# failures Joshua must act on wherever they happen; other failures only of turns he did not start
ACTION_KINDS = ("auth", "usage_limit", "gate", "budget")
UNATTENDED = ("routine", "wakeup", "inbox", "api", "trigger")
# 0.12.15: turns nobody watches that end badly (blocked, waiting on the owner, failed, no status line) stay
# listed for a week, one item per bot, routine/assignment/watcher and reason, until a later run of the same
# one succeeds or the owner dismisses it. Assignment turns that end blocked or waiting already have their
# assignment item; routine, wake-up and watcher turns must end with a status line, bot replies need not.
UNATTENDED_WINDOW = timedelta(days=7)
BAD_END_SOURCES = ("routine", "wakeup", "inbox", "trigger")
NO_STATUS_SOURCES = BAD_END_SOURCES
NO_STATUS_REASON = "the turn ended without a BOT-OFFICE-STATUS line"
NO_EVIDENCE_REASON = "it said done without saying what it checked or how"
END_WORDS = {"blocked": "ended blocked", "waiting_on_user": "is waiting on you", "failed": "failed",
             "no_status": "ended without a status line", "no_evidence": "finished with nothing to show it was checked"}
ASSIGNMENT_ACTIONS = ["reply", "start", "done", "dismiss"]
# #10: a job parked on a usage limit for longer than this gets one notice per attempt
PARK_NOTICE_AFTER = timedelta(minutes=10)
NOTICE_TZ = "America/New_York"


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
        _unattended(db, bots, items, stamp)
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
        _refused_without_bot(db, items)
        for row in db.execute(
                f"SELECT n.*, j.not_before FROM usage_notices n JOIN jobs j ON j.id=n.job_id "
                f"WHERE {PARKED_SQL} AND n.attempt=j.attempts AND n.dismissed_at IS NULL ORDER BY n.created_at DESC"):
            bot = bots.get(row["bot_id"])
            if bot is None:
                continue
            items.append({
                "id": f"u:{row['job_id']}:{row['attempt']}", "kind": "usage_limit", "state": "parked",
                "bot_id": row["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]),
                "job_id": row["job_id"], "provider": row["provider"], "title": "Waiting on a usage limit",
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
    return items


def _usage_text(bot: str, provider: str, not_before: str, stamp: datetime) -> str:
    from zoneinfo import ZoneInfo

    back = utc_time(not_before)
    minutes = max(0, round((back - stamp).total_seconds() / 60))
    clock = back.astimezone(ZoneInfo(NOTICE_TZ)).strftime("%H:%M")
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


def _push_update(row) -> str:
    """fix3 #3: the exact update a push card covers (approving passes only that one)."""
    pushes = row["tool"] == git_guard.PUSH_TOOL or git_guard.push_commands(row["summary"].partition(": ")[2])
    if row["preset"] != git_guard.PRESET or not pushes:
        return ""
    try:
        plan = json.loads(row["push_plan"] or "null")
    except ValueError:
        plan = None
    if not plan:
        return " — the exact update is not known yet; git's push check asks again with it"
    return f" — exact update: {git_guard.describe_plan(plan)}"


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
            "class": (approval_card(store, dict(row))["label"] if store is not None else row["preset"]),
            "ask": (f"{bot['name']} asks to run ({row['source']}, {row['preset'] or 'rule'}): {_preview(row['summary'])} "
                    f"({len(row['summary'])} characters, fingerprint {row['input_hash'][:12]}; the whole command is below)"
                    + _push_update(row) + (" — expired; Re-run approves it now" if expired else "")),
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


def _refused_without_bot(db, items: list[dict]) -> None:
    """fix3 #4: a job with no bot cannot wait for an approval card and continue (it has no bot
    session to resume, and running it again would repeat its earlier steps), so a protected or
    sending action is refused there; this notice tells the owner what and why (Dismiss)."""
    since = (now() - FAILED_WINDOW).isoformat()
    for row in db.execute("SELECT * FROM gate_log WHERE bot_id IS NULL AND effect='never' AND preset IN (?, ?) "
                          "AND dismissed_at IS NULL AND created_at>=? ORDER BY id DESC LIMIT 20",
                          ("git-guard", "send", since)):
        what = row["summary"].removeprefix("refused: ")
        items.append({
            "id": f"g:{row['id']}", "kind": "alert", "state": "refused", "bot_id": None,
            "bot_name": "Job without a bot", "notify": True, "alert_id": row["id"], "job_id": row["job_id"],
            "title": "Refused: needs your approval",
            "ask": (f"A job with no bot (job {str(row['job_id'])[:8]}) tried: {what}. It was refused. A job "
                    "without a bot cannot wait for an Approve card and then continue: it has no bot session "
                    "to resume, and running it again would repeat its earlier steps. To do this with an "
                    "Approve card, give the task to a bot."),
            "at": row["created_at"], "actions": ["dismiss"],
        })


def dismiss_job(store: JobStore, job_id: str) -> bool:
    """Dismiss a finished job and every other open bad end in the same grouped Needs-you item."""
    stamp = now()
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND status NOT IN ('queued','running')", (job_id,)).fetchone()
        if row is None:
            return False
        ids = [job_id]
        end = _bad_end(row)
        if end is not None and row["bot_id"] is not None and row["dismissed_at"] is None:  # a repeated click is a no-op
            key = (row["bot_id"], _stream(row), _reason_key(*end))
            ids += [other["id"] for other, category, reason in _bad_ends(db, stamp)
                    if (other["bot_id"], _stream(other), _reason_key(category, reason)) == key]
        db.execute(f"UPDATE jobs SET dismissed_at=? WHERE id IN ({','.join('?' * len(ids))})", (stamp.isoformat(), *ids))
    return True


def last_desk_job(store: JobStore, desktop: str) -> str | None:
    """The latest bot turn that ran on *desktop* (for "Hand back")."""
    with closing(store._connect()) as db:
        row = db.execute("SELECT id FROM jobs WHERE slot=? AND bot_id IS NOT NULL "
                         "ORDER BY COALESCE(started_at, created_at) DESC LIMIT 1", (desktop,)).fetchone()
    return row["id"] if row else None


def _stream(job: sqlite3.Row) -> tuple[str, str]:
    """The run series a job belongs to: a later success of the same series clears an earlier bad end."""
    for column in ("routine_id", "assignment_id", "watcher_id"):
        if job[column]:
            return column, job[column]
    return "source", job["source"]


def _bad_end(job: sqlite3.Row) -> tuple[str, str] | None:
    """(category, reason) when *job* ended in a way the owner should hear about, else None."""
    status, assignment = job["status"], job["assignment_id"]
    if status in ("failed", "needs_login"):
        if status == "failed" and not (assignment or job["failure_kind"] in ACTION_KINDS or job["source"] in UNATTENDED):
            return None
        kind = "needs_login" if status == "needs_login" else job["failure_kind"]
        return "failed", (job["error"] or kind or "failed").splitlines()[0][:200]
    if assignment or job["source"] not in BAD_END_SOURCES:
        return None
    state, _, reason = (job["outcome"] or "").partition(":")
    if status == "incomplete" and state in ("blocked", "waiting_on_user"):
        return state, reason.strip()[:200] or "no reason given"
    if status == "succeeded" and job["outcome"] == "unverified" and job["source"] in NO_STATUS_SOURCES:
        return "no_status", NO_STATUS_REASON
    if status == "succeeded" and job["outcome"] == "done" and job["verified"] == 0:
        return "no_evidence", NO_EVIDENCE_REASON  # 0.12.16: a done is not a check
    return None


def _reason_key(category: str, reason: str) -> str:
    return category + ":" + re.sub(r"\d{4,}", "#", " ".join(reason.lower().split()))


def _name(job: sqlite3.Row) -> str:
    if job["routine_id"]:
        label = job["routine_name"] or (job["routine_prompt"] or "").strip()[:40]
        return f'routine "{label}"' if label else "a deleted routine"
    if job["assignment_id"]:
        return f'assignment "{job["assignment_title"]}"' if job["assignment_title"] else "an assignment"
    if job["watcher_id"]:
        return f'watcher "{job["watcher_name"]}"' if job["watcher_name"] else "a watcher"
    return {"inbox": "a bot message", "wakeup": "a wake-up", "api": "an API message",
            "trigger": "a watcher event"}.get(job["source"], f"a {job['source']} turn")


def _bad_ends(db: sqlite3.Connection, stamp: datetime) -> list[tuple[sqlite3.Row, str, str]]:
    """Unattended bad ends still open (no later success of their series, not dismissed), oldest first."""
    since = (stamp - UNATTENDED_WINDOW).isoformat()
    kinds, sources = ",".join("?" * len(ACTION_KINDS)), ",".join("?" * len(UNATTENDED))
    rows = db.execute(
        "SELECT j.*, r.name AS routine_name, r.prompt AS routine_prompt, a.title AS assignment_title, "
        "a.status AS assignment_status, w.name AS watcher_name FROM jobs j "
        "LEFT JOIN routines r ON r.id=j.routine_id LEFT JOIN assignments a ON a.id=j.assignment_id "
        "LEFT JOIN watchers w ON w.id=j.watcher_id "
        "WHERE j.bot_id IS NOT NULL AND j.finished_at>=? AND j.dismissed_at IS NULL "
        "AND j.status IN ('failed','needs_login','incomplete','succeeded') "
        "AND (j.status='needs_login' OR j.assignment_id IS NOT NULL "
        f"OR (j.status='failed' AND (j.failure_kind IN ({kinds}) OR j.source IN ({sources}))) "
        "OR (j.status IN ('incomplete','succeeded') AND j.source IN ('routine','wakeup','inbox','trigger'))) "
        "ORDER BY j.finished_at, j.rowid", (since, *ACTION_KINDS, *UNATTENDED)).fetchall()
    bad = [(row, *end) for row in rows if (end := _bad_end(row)) is not None
           and row["assignment_status"] not in ("done", "dropped")]
    if not bad:
        return []
    wins = db.execute(
        "SELECT bot_id, source, routine_id, assignment_id, watcher_id, outcome, verified, finished_at FROM jobs "
        "WHERE status='succeeded' AND finished_at>=? AND bot_id IS NOT NULL ORDER BY finished_at",
        (min(row["finished_at"] for row, _, _ in bad),)).fetchall()
    # newest success per bot, per series and per series with a status line (one pass, no pairwise compare)
    by_bot: dict[str, str] = {}
    by_series: dict[tuple, str] = {}
    by_series_stated: dict[tuple, str] = {}
    by_series_checked: dict[tuple, str] = {}
    for win in wins:
        by_bot[win["bot_id"]] = win["finished_at"]
        by_series[(win["bot_id"], _stream(win))] = win["finished_at"]
        if win["outcome"] != "unverified":
            by_series_stated[(win["bot_id"], _stream(win))] = win["finished_at"]
            if win["verified"] != 0:
                by_series_checked[(win["bot_id"], _stream(win))] = win["finished_at"]
    open_ends = []
    for row, category, reason in bad:
        key = (row["bot_id"], _stream(row))
        # a failure of no particular routine, assignment or watcher (key[1][0] == "source") is cleared by any
        # later success of the bot, as before 0.12.15
        login = row["status"] == "needs_login" or (category == "failed" and (
            row["failure_kind"] in ACTION_KINDS or key[1][0] == "source"))
        # a login or usage problem is fixed for the whole bot; the rest only by the same series running
        # well again (a missing status line only by a run that has one)
        newest = by_bot if login else {"no_status": by_series_stated, "no_evidence": by_series_checked}.get(category, by_series)
        if newest.get(row["bot_id"] if login else key, "") <= row["finished_at"]:
            open_ends.append((row, category, reason))
    return open_ends


def _unattended(db: sqlite3.Connection, bots: dict, items: list[dict], stamp: datetime) -> None:
    """Needs-you items for unattended turns that ended badly, grouped by bot, series and reason."""
    groups: dict[tuple, list[tuple[sqlite3.Row, str, str]]] = {}
    for row, category, reason in _bad_ends(db, stamp):
        groups.setdefault((row["bot_id"], _stream(row), _reason_key(category, reason)), []).append((row, category, reason))
    for group in sorted(groups.values(), key=lambda g: g[-1][0]["finished_at"], reverse=True)[:50]:
        bot = bots.get(group[0][0]["bot_id"])
        if bot is None:
            continue
        (first, category, _), (job, _, reason) = group[0], group[-1]
        count = len(group)
        what = _name(job)
        repeat = f" ({count} times since {to_cron_time(datetime.fromisoformat(first['finished_at'])).strftime('%b %-d, %-I:%M %p')} ET)" if count > 1 else ""
        item = {"bot_id": job["bot_id"], "bot_name": bot["name"], "notify": bool(bot["notify"]), "job_id": job["id"],
                "job_ids": [row["id"] for row, _, _ in group], "count": count, "reason": reason,
                "name": what, "first_at": first["finished_at"], "at": job["finished_at"]}
        if category == "failed":
            kind = "needs_login" if job["status"] == "needs_login" else job["failure_kind"]
            items.append({**item, "id": f"j:{first['id']}:{first['attempts']}", "kind": "job", "state": "failed",
                          "title": f"{job['source'] or 'turn'} failed",
                          "ask": f"{bot['name']}'s {what} failed: {reason or kind}{repeat}",
                          "actions": ["retry", "dismiss"]})
        else:
            # the id names the group's first run, so a growing repeat count never notifies again (failed groups too)
            items.append({**item, "id": f"x:{first['id']}:{category}", "kind": "unattended", "state": category,
                          "title": f"{what} {END_WORDS[category]}",
                          "ask": f"{bot['name']}'s {what} {END_WORDS[category]}: {reason}{repeat}",
                          "actions": ["dismiss"]})
