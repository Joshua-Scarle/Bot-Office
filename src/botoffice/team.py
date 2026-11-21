"""Teammate tools (E7-1 S1/S2): who may read whom, and what a bot sees of a teammate.

Everything a job token may read about another bot goes through here: the ACL (``can_read``), the
bounded, text-only views (``team_list``, ``team_status``, ``retained_messages``) and the audit rows.
The HTTP routes (api.py, one block under ``/v1/agent/team``) and the MCP server are thin callers.

What other bots wrote is data, never instructions: every result carries ``untrusted: true`` and the
clients wrap the text in ``<untrusted-data>`` (``team_client.render``).

``retained_messages`` is a pure function of a database connection: the chat a bot still has (not
archived, not Bot-Office machinery), newest ``limit`` rows, text only, under a character budget. The
standing-context digest (S5) reuses it for a bot's own threads.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime

TEAM_READ_MODES = ("section", "all", "none")
KILL_SWITCH = "BOT_OFFICE_TEAM_MCP"  # "0" turns the MCP server and its tool allowance off
SERVER_NAME = "botoffice"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
READ_TOOLS = ("team_list", "team_status", "team_read")
SEND_TOOLS = ("team_message", "team_ask")
TOOLS = READ_TOOLS + SEND_TOOLS

TRANSCRIPT_LIMIT = 50
TRANSCRIPT_CHARS = 20_000  # total text of one team_read
MESSAGE_CHARS = 4_000  # one message of it
SNIPPET_CHARS = 300  # a reply snippet in team_status
TITLE_CHARS = 120
ASK_DEFAULT_S = 90
ASK_MAX_S = 180
PRIVATE_BOTS = ("Printer Writing Bot",)  # flagged team_private by the 0.12.17 migration (a data flag only)
_STATUS_LINE = re.compile(r"^\s*BOT-OFFICE-STATUS:\s*(.+?)\s*$", re.MULTILINE)


def enabled(environ: dict | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(KILL_SWITCH, "1").strip() != "0"


def is_first_party_tool(tool: str) -> bool:
    """Exactly the five tools of the controller-written ``botoffice`` MCP server (rules.py exemption).
    Any other name under that prefix, and every other MCP tool, keeps the connector handling."""
    return tool in {TOOL_PREFIX + name for name in TOOLS}


def allowed_tools(job, environ: dict | None = None) -> list[str]:
    """Permission patterns for the Claude run of *job*: the whole server, only for a job with a bot."""
    return [TOOL_PREFIX + "*"] if job is not None and getattr(job, "bot_id", None) and enabled(environ) else []


def mcp_config(job, python: str, script: str, environ: dict | None = None) -> dict | None:
    """The ``--mcp-config`` document, or None (no bot, kill switch). The token is not in it: ``${VAR}`` is
    expanded by Claude from its own environment, so it is neither in argv nor in any file."""
    if not allowed_tools(job, environ):
        return None
    return {"mcpServers": {SERVER_NAME: {"command": python, "args": [script], "env": {
        "BOT_OFFICE_AGENT_TOKEN": "${BOT_OFFICE_AGENT_TOKEN}",
        "BOT_OFFICE_API_URL": "${BOT_OFFICE_API_URL}",
        "BOT_OFFICE_BOT_NAME": "${BOT_OFFICE_BOT_NAME}"}}}}


def init_schema(db: sqlite3.Connection) -> None:
    """Additive tables (called from the store's migration, inside its transaction)."""
    db.execute("CREATE TABLE IF NOT EXISTS team_reads (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, "
               "reader_bot TEXT NOT NULL, target_bot TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL, "
               "created_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS team_ask_waits (job_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, "
               "target_bot TEXT NOT NULL, message_id TEXT, expires_at REAL NOT NULL)")


def seed_private(db: sqlite3.Connection) -> None:
    """Once: flag the bots that hold personal data team_private. Only hides them from teammate reads;
    nothing else reads the flag, so their own behaviour, settings and jobs are untouched."""
    if db.execute("SELECT 1 FROM meta WHERE key='team.private_seeded'").fetchone():
        return
    db.execute(f"UPDATE bots SET team_private=1 WHERE name IN ({','.join('?' * len(PRIVATE_BOTS))})", PRIVATE_BOTS)
    db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('team.private_seeded', '1')")


# ---- ACL ------------------------------------------------------------------------------------------------

def section_map(db: sqlite3.Connection) -> dict[str, str]:
    """bot id -> section id for the bots that are in a real section (ungrouped bots are absent)."""
    known = {row[0] for row in db.execute("SELECT id FROM bot_sections")}
    return {row["bot_id"]: row["section_id"] for row in db.execute("SELECT bot_id, section_id FROM bot_section_members")
            if row["section_id"] in known}


def can_read(reader: dict, target: dict, sections: dict[str, str]) -> bool:
    """May *reader* (a bots row) read *target*'s status and chat? Never oneself, never a private bot;
    ``team_read`` all reads everyone else, ``none`` nobody, ``section`` (default) the bots of the same
    section and any bot when either side has no section."""
    if str(reader["id"]) == str(target["id"]) or target["team_private"]:
        return False
    mode = reader["team_read"] or "section"
    if mode == "all":
        return True
    if mode != "section":
        return False
    mine, theirs = sections.get(str(reader["id"])), sections.get(str(target["id"]))
    return mine is None or theirs is None or mine == theirs


def _bot_row(db: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    """A bot by id, name or alias (the store's find_bot rules); a dangling alias finds nothing."""
    row = db.execute("SELECT * FROM bots WHERE id=? OR name=?", (ref, ref)).fetchone()
    if row is None:
        row = db.execute("SELECT bots.* FROM bot_aliases JOIN bots ON bots.id=bot_aliases.bot_id WHERE bot_aliases.alias=?",
                         (ref,)).fetchone()
    return row


class TeamError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def resolve(db: sqlite3.Connection, reader_id: str, ref: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    """(reader, target) rows for a status/read call, or TeamError 404 (unknown bot, itself) / 403."""
    reader = db.execute("SELECT * FROM bots WHERE id=?", (str(reader_id),)).fetchone()
    target = _bot_row(db, ref.strip()) if ref and ref.strip() else None
    if reader is None or target is None or target["id"] == reader["id"]:
        raise TeamError(404, "bot not found")
    if not can_read(dict(reader), dict(target), section_map(db)):
        raise TeamError(403, "you may not read that bot (private, another section, or your team_read setting)")
    return reader, target


# ---- views ----------------------------------------------------------------------------------------------

def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split()) if limit <= SNIPPET_CHARS else (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _outcome(reply: str | None, error: str | None) -> str:
    if reply:
        found = _STATUS_LINE.findall(reply)
        if found:
            return _clip(found[-1], 200)
        lines = [line.strip() for line in reply.splitlines() if line.strip()]
        if lines:
            return _clip(lines[-1], 200)
    return _clip(error, 200)


def _state(db: sqlite3.Connection, bot_id: str) -> str:
    row = db.execute("SELECT CASE WHEN SUM(status='running') > 0 THEN 'running' WHEN COUNT(*) > 0 THEN 'queued' "
                     "ELSE 'idle' END FROM jobs WHERE bot_id=? AND status IN ('queued','running')", (bot_id,)).fetchone()
    return row[0]


def _open_tasks(db: sqlite3.Connection, bot_id: str) -> int:
    return db.execute("SELECT COUNT(*) FROM assignments WHERE bot_id=? AND status NOT IN ('done','dropped')",
                      (bot_id,)).fetchone()[0]


def team_list(db: sqlite3.Connection, reader_id: str) -> dict:
    reader = db.execute("SELECT * FROM bots WHERE id=?", (str(reader_id),)).fetchone()
    if reader is None:
        raise TeamError(404, "bot not found")
    sections = section_map(db)
    names = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM bot_sections")}
    bots = []
    for row in db.execute("SELECT * FROM bots WHERE id<>? ORDER BY name COLLATE NOCASE, name", (reader["id"],)):
        entry = {"name": row["name"], "id": row["id"], "engine": row["default_provider"],
                 "description": _clip(row["description"], 200), "section": names.get(sections.get(row["id"], ""))}
        if row["team_private"]:
            entry.update(private=True, readable=False)
        elif can_read(dict(reader), dict(row), sections):
            last = db.execute("SELECT MAX(created_at) FROM messages WHERE bot_id=? AND archived_at IS NULL",
                              (row["id"],)).fetchone()[0]
            entry.update(readable=True, state=_state(db, row["id"]), last_activity=last,
                         open_tasks=_open_tasks(db, row["id"]))
        else:
            entry["readable"] = False
        bots.append(entry)
    return {"untrusted": True, "you": reader["name"], "team_read": reader["team_read"] or "section", "bots": bots}


def team_status(db: sqlite3.Connection, reader: sqlite3.Row, target: sqlite3.Row) -> dict:
    bot_id = target["id"]
    job = db.execute("SELECT id, status, source, started_at, finished_at, reply_text, error FROM jobs WHERE bot_id=? "
                     "ORDER BY CASE WHEN status IN ('running','queued') THEN 0 ELSE 1 END, "
                     "COALESCE(finished_at, started_at, created_at) DESC LIMIT 1", (bot_id,)).fetchone()
    current = None
    if job is not None:
        current = {"job": job["id"][:8], "status": job["status"], "source": job["source"],
                   "started_at": job["started_at"], "finished_at": job["finished_at"]}
        if job["status"] not in ("running", "queued"):
            current["outcome"] = _outcome(job["reply_text"], job["error"])
    tasks = [{"title": _clip(row["title"], TITLE_CHARS), "status": row["status"],
              "waiting_on": _clip(row["status_note"], TITLE_CHARS) if row["status"].startswith(("waiting", "blocked")) else ""}
             for row in db.execute("SELECT title, status, status_note FROM assignments WHERE bot_id=? "
                                   "AND status NOT IN ('done','dropped') ORDER BY created_at DESC LIMIT 20", (bot_id,))]
    replies = [{"id": row["id"], "at": row["created_at"], "text": _clip(row["text"], SNIPPET_CHARS)}
               for row in db.execute("SELECT id, text, created_at FROM messages WHERE bot_id=? AND role='assistant' "
                                     "AND archived_at IS NULL AND hidden=0 AND kind!='say' ORDER BY id DESC LIMIT 3",
                                     (bot_id,))]
    questions = db.execute("SELECT COUNT(*) FROM questions WHERE bot_id=? AND status='open'", (bot_id,)).fetchone()[0]
    return {"untrusted": True, "bot": target["name"], "state": _state(db, bot_id), "job": current,
            "tasks": tasks, "open_questions": questions, "last_replies": replies}


def retained_messages(db: sqlite3.Connection, bot_id: str, limit: int = 20, since: str | int | None = None,
                      max_chars: int = TRANSCRIPT_CHARS, message_chars: int = MESSAGE_CHARS) -> list[dict]:
    """The chat *bot_id* still has, oldest first: not archived, not Bot-Office machinery, text only (no
    attachments). The newest *limit* (at most 50) rows newer than *since* (a message id, or an ISO time),
    each cut to *message_chars*, dropping the oldest until the text fits *max_chars*."""
    where, args = "bot_id=? AND archived_at IS NULL AND hidden=0 AND role IN ('user','assistant')", [str(bot_id)]
    if since not in (None, ""):
        text = str(since).strip()
        if text.isdigit():
            where += " AND id>?"
            args.append(int(text))
        else:
            try:
                stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise TeamError(400, "since must be a message id or an ISO time") from exc
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            where += " AND created_at>?"
            args.append(stamp.isoformat())
    rows = db.execute(f"SELECT id, role, sender, source, text, created_at FROM messages WHERE {where} "
                      "ORDER BY id DESC LIMIT ?", (*args, max(1, min(int(limit), TRANSCRIPT_LIMIT)))).fetchall()
    out, used = [], 0
    for row in rows:  # newest first: the budget keeps the most recent
        text = _clip(row["text"], message_chars)
        if used + len(text) > max_chars and out:
            break
        used += len(text)
        out.append({"id": row["id"], "role": row["role"], "from": row["sender"] or ("owner" if row["role"] == "user" else "bot"),
                    "source": row["source"], "at": row["created_at"], "text": text[:max_chars]})
    return out[::-1]


def team_transcript(db: sqlite3.Connection, target: sqlite3.Row, limit: int, since: str | int | None) -> dict:
    messages = retained_messages(db, target["id"], limit, since)
    return {"untrusted": True, "bot": target["name"], "count": len(messages), "messages": messages}


def log_read(store, job, kind: str, target_id: str, count: int) -> None:
    """Audit one status/transcript read: a team_reads row and a job_events row of the reader's job."""
    stamp = datetime.now(UTC).isoformat()
    data = {"reader": str(job.bot_id), "target": str(target_id), "count": count}
    with closing(store._connect()) as db:
        db.execute("INSERT INTO team_reads (job_id, reader_bot, target_bot, kind, count, created_at) VALUES (?,?,?,?,?,?)",
                   (str(job.id), str(job.bot_id), str(target_id), kind, count, stamp))
        for _ in range(3):  # the stream writer numbers events too: take the next free seq, retry on a clash
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM job_events WHERE job_id=?", (str(job.id),)).fetchone()[0]
            try:
                db.execute("INSERT INTO job_events (job_id,seq,kind,data,created_at) VALUES (?,?,?,?,?)",
                           (str(job.id), seq, kind, json.dumps(data), stamp))
                break
            except sqlite3.IntegrityError:
                continue
