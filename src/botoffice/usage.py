"""0.11.0 #10 usage, per-bot daily budgets and the model-free daily digest.

Every run counts: the attempts table keeps each retried or interrupted run (with its usage), the
jobs table the latest run. A day is a New York calendar day. Daily budgets cover runs under the
strict unattended profile only: trigger runs, plus routine, wake-up, assignment and bot-to-bot
turns of a bot set to ``scheduled_runs: gated``. By default scheduled turns are reported but never
held (as in 0.10.x), and Joshua's chat turns are never held. A run the release gate refused (it
never started a CLI) does not count.
The budget is checked when a job is claimed, in the claim transaction, so two lanes cannot both
take the last turn. Token caps apply only when every counted run reported its usage; a run with
unknown usage holds further runs (fail closed) while a token cap is set.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAY_TZ = ZoneInfo("America/New_York")
UNATTENDED = ("routine", "wakeup", "assignment", "trigger", "inbox")
STRICT = ("trigger",)
DEFAULT_BUDGET = {"unattended_turns": 40, "unattended_minutes": 120, "tokens": None}
TOKEN_KEYS = ("input_tokens", "output_tokens")


def day_start(stamp: datetime, days_back: int = 0) -> datetime:
    """00:00 New York time of *stamp*'s day (or *days_back* days earlier), in UTC."""
    local = stamp.astimezone(DAY_TZ).date() - timedelta(days=days_back)
    return datetime.combine(local, time(0), DAY_TZ).astimezone(UTC)


def next_day(stamp: datetime) -> datetime:
    local = stamp.astimezone(DAY_TZ).date() + timedelta(days=1)
    return datetime.combine(local, time(0), DAY_TZ).astimezone(UTC)


def day_key(stamp: datetime) -> str:
    return stamp.astimezone(DAY_TZ).date().isoformat()


def budget_of(raw: str | None) -> dict:
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        stored = {}
    return {key: stored.get(key, default) if isinstance(stored, dict) else default
            for key, default in DEFAULT_BUDGET.items()}


def check_budget(budget: dict) -> dict:
    out = {}
    for key, default in DEFAULT_BUDGET.items():
        value = budget.get(key, default)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10**9):
            raise ValueError(f"{key} must be a whole number >= 0 or null (no cap)")
        out[key] = value
    return out


def tokens(usage: dict | None) -> int | None:
    if not usage:
        return None
    found = [usage[key] for key in TOKEN_KEYS if isinstance(usage.get(key), int | float)]
    return int(sum(found)) if found else None


def _usage(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _overlap(start: str | None, end: str | None, since: datetime, until: datetime) -> float:
    if not start:
        return 0.0
    begin = max(datetime.fromisoformat(start), since)
    finish = min(datetime.fromisoformat(end) if end else until, until)
    return max(0.0, (finish - begin).total_seconds() / 60)


def budget_sources(scheduled_runs: str | None) -> tuple[str, ...]:
    """The sources a bot's daily budget counts and holds (see rules.is_unattended)."""
    return UNATTENDED if scheduled_runs == "gated" else STRICT


def runs(db: sqlite3.Connection, since: datetime, until: datetime, bot_id: str | None = None,
         unattended: bool | None = None, sources: tuple[str, ...] | None = None,
         skip_gate: bool = False) -> list[dict]:
    """Every run (attempt) that started in [since, until): bot, source, kind, minutes, usage.
    *sources* narrows to those job sources; *skip_gate* drops runs the release gate refused."""
    where, args = ["started_at>=?", "started_at<?"], [since.isoformat(), until.isoformat()]
    if bot_id:
        where.append("j.bot_id=?")
        args.append(bot_id)
    if unattended is not None:
        where.append(f"j.source {'' if unattended else 'NOT '}IN ({','.join('?' * len(UNATTENDED))})")
        args.extend(UNATTENDED)
    if sources is not None:
        where.append(f"j.source IN ({','.join('?' * len(sources))})")
        args.extend(sources)
    clause = " AND ".join(where)
    gate_a = " AND COALESCE(a.kind, '')!='gate'" if skip_gate else ""
    gate_j = " AND COALESCE(j.failure_kind, '')!='gate'" if skip_gate else ""
    rows = db.execute(
        "SELECT j.bot_id, j.source, a.kind, a.started_at, a.finished_at, a.usage_json, 0 AS current "
        f"FROM job_attempts a JOIN jobs j ON j.id=a.job_id WHERE {clause.replace('started_at', 'a.started_at')}"
        f"{gate_a} "
        "UNION ALL SELECT j.bot_id, j.source, COALESCE(j.failure_kind, j.status), j.started_at, j.finished_at, "
        f"j.usage_json, 1 FROM jobs j WHERE j.status!='queued' AND {clause.replace('started_at', 'j.started_at')}"
        f"{gate_j}",
        args + args).fetchall()
    return [{"bot_id": row["bot_id"], "source": row["source"], "kind": row["kind"], "current": row["current"],
             "minutes": _overlap(row["started_at"], row["finished_at"], since, until),
             "usage": _usage(row["usage_json"])} for row in rows]


def totals(items: list[dict]) -> dict:
    counted = [tokens(item["usage"]) for item in items]
    return {"turns": len(items), "minutes": round(sum(item["minutes"] for item in items), 1),
            "tokens": sum(value for value in counted if value is not None),
            "unknown_usage": sum(1 for value in counted if value is None),
            "cost_usd": round(sum(float(item["usage"].get("cost_usd", 0)) for item in items if item["usage"]), 4)}


def extra_today(raw: str | None, stamp: datetime) -> dict:
    """The "Allow +N today" grant, if it is for today."""
    try:
        extra = json.loads(raw) if raw else {}
    except ValueError:
        extra = {}
    if not isinstance(extra, dict) or extra.get("day") != day_key(stamp):
        return {"turns": 0, "minutes": 0, "tokens": 0}
    return {key: int(extra.get(key) or 0) for key in ("turns", "minutes", "tokens")}


def over_budget(db: sqlite3.Connection, bot_id: str, stamp: datetime) -> str | None:
    """Why this bot's next unattended run must wait for tomorrow, or None."""
    row = db.execute("SELECT daily_budget_json, budget_extra_json, scheduled_runs FROM bots WHERE id=?",
                     (bot_id,)).fetchone()
    if row is None:
        return None
    budget, extra = budget_of(row["daily_budget_json"]), extra_today(row["budget_extra_json"], stamp)
    used = totals(runs(db, day_start(stamp), stamp + timedelta(seconds=1), bot_id,
                       sources=budget_sources(row["scheduled_runs"]), skip_gate=True))
    if budget["unattended_turns"] is not None and used["turns"] >= budget["unattended_turns"] + extra["turns"]:
        return f"{used['turns']} unattended turns today (budget {budget['unattended_turns'] + extra['turns']})"
    if budget["unattended_minutes"] is not None and \
            used["minutes"] >= budget["unattended_minutes"] + extra["minutes"]:
        return f"{used['minutes']:.0f} unattended minutes today (budget {budget['unattended_minutes'] + extra['minutes']})"
    if budget["tokens"] is not None:
        if used["unknown_usage"]:
            return f"{used['unknown_usage']} run(s) today did not report token use and a token cap is set"
        if used["tokens"] >= budget["tokens"] + extra["tokens"]:
            return f"{used['tokens']} tokens today (budget {budget['tokens'] + extra['tokens']})"
    return None


def budget_gate(db: sqlite3.Connection, job_id: str, stamp: datetime) -> str | None:
    """Claim-time check (inside the claim transaction): the reason an unattended job must wait."""
    job = db.execute("SELECT j.bot_id, j.source, j.priority, b.scheduled_runs FROM jobs j JOIN bots b ON b.id=j.bot_id "
                     "WHERE j.id=?", (job_id,)).fetchone()
    if job is None or job["source"] not in budget_sources(job["scheduled_runs"]):
        return None
    if job["priority"] is not None and job["priority"] < 0:
        return None  # 0.12.39: a priority inbox message (store.URGENT_PRIORITY) always wakes the bot
    return over_budget(db, job["bot_id"], stamp)


def digest(db: sqlite3.Connection, stamp: datetime, open_items: list[dict]) -> str:
    """Yesterday (New York) in a few lines, from the database only: no model call."""
    until, since = day_start(stamp), day_start(stamp, 1)
    bots = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM bots")}
    items = runs(db, since, until)
    lines = [f"Daily digest for {day_key(since)} (no model call):"]
    if not items:
        lines.append("No runs.")
    for bot_id, name in sorted(bots.items(), key=lambda pair: pair[1].lower()):
        mine = [item for item in items if item["bot_id"] == bot_id]
        if not mine:
            continue
        total = totals(mine)
        sources: dict[str, int] = {}
        failed: dict[str, int] = {}
        for item in mine:
            sources[item["source"] or "api"] = sources.get(item["source"] or "api", 0) + 1
            if item["kind"] not in (None, "succeeded", "running", "queued", "incomplete", "cancelled"):
                failed[item["kind"]] = failed.get(item["kind"], 0) + 1
        usage = f", {total['tokens']} tokens" + (f" ({total['unknown_usage']} runs unknown)" if total["unknown_usage"] else "")
        lines.append(f"- {name}: {total['turns']} runs ({', '.join(f'{k} {v}' for k, v in sorted(sources.items()))}), "
                     f"{total['minutes']:.0f} min{usage}"
                     + (f"; failures: {', '.join(f'{k} {v}' for k, v in sorted(failed.items()))}" if failed else ""))
    changed = db.execute("SELECT COUNT(DISTINCT assignment_id) FROM assignment_events WHERE created_at>=? AND created_at<?",
                         (since.isoformat(), until.isoformat())).fetchone()[0]
    lines.append(f"Assignments changed: {changed}. Open Needs-you items: {len(open_items)}.")
    return "\n".join(lines)
