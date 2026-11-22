"""context_recall (E7-1 S5): full-text search over a bot's own retained chat, and over teammates it may read.

One caller path for the HTTP route (``/v1/agent/recall``, job token only) and the MCP tool. The query is never
passed to FTS5 as typed: it is cut to words (``history.tokens``) and every word is quoted, so no operator
(NEAR, column filters, ``*``, quotes) can be written. Rows are only the reader's own and, for ``team``, those of
bots ``team.can_read`` allows: never archived (rewound) or hidden messages, never a private bot. Results are
untrusted data (``untrusted: true``; the client fences the text), at most 20 hits and 8000 characters.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import urlencode

from . import failures, history, team

SCOPES = ("self", "team")
MAX_HITS = 20
MAX_CHARS = 8_000  # the rendered hits: text, bot name, time and line framing
HIT_CHARS = 600
NAME_CHARS = 60  # a hit's bot name, so one hit is always far below MAX_CHARS
SLOTS = threading.BoundedSemaphore(2)  # searches running at once; held by the worker thread itself
SLOT_WAIT_S = 5.0
ADMIT = threading.BoundedSemaphore(4)  # searches admitted (running or waiting for a slot); more is refused before any thread is used
QUERY_CHARS = 200
SECONDS = 2.0  # one search is cut off after this long (a progress handler), whatever the index holds
SNIPPET_TOKENS = 40


class RecallError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _fts_or(words: list[str]) -> str:
    quoted = ['"' + w.replace('"', '""') + '"' for w in words]
    if len(words[-1]) > 1:
        quoted[-1] += "*"
    return " OR ".join(quoted)


def _fts_and(words: list[str]) -> str:
    return _fts_or(words).replace(" OR ", " ")


def readable_bots(db: sqlite3.Connection, reader_id: str, scope: str) -> dict[str, str]:
    """bot id -> name of the bots a recall may search: the reader, and with scope team the readable others."""
    reader = db.execute("SELECT * FROM bots WHERE id=?", (str(reader_id),)).fetchone()
    if reader is None:
        raise RecallError(404, "bot not found")
    allowed = {reader["id"]: reader["name"]}
    if scope == "team":
        sections = team.section_map(db)
        for row in db.execute("SELECT * FROM bots WHERE id<>?", (reader["id"],)):
            if team.can_read(dict(reader), dict(row), sections):
                allowed[row["id"]] = row["name"]
    return allowed


def search(db: sqlite3.Connection, bots: dict[str, str], query: str, limit: int = 10,
           exclude_job: str | None = None) -> list[dict]:
    words = history.tokens((query or "")[:QUERY_CHARS])
    if not words or not bots:
        return []
    limit = max(1, min(int(limit), MAX_HITS))
    marks = ",".join("?" * len(bots))
    where = (f"m.bot_id IN ({marks}) AND m.archived_at IS NULL AND m.hidden=0 AND m.role IN ('user','assistant')"
             + (" AND (m.job_id IS NULL OR m.job_id<>?)" if exclude_job else ""))
    args = [*bots, *([exclude_job] if exclude_job else [])]
    deadline = time.monotonic() + SECONDS
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        rows: list = []
        if history.has_fts(db):
            sql = (f"SELECT m.id, m.bot_id, m.role, m.created_at, snippet({history.TABLE}, -1, '', '', '…', {SNIPPET_TOKENS}) "
                   f"AS snippet FROM {history.TABLE} JOIN messages m ON m.id={history.TABLE}.rowid "
                   f"WHERE {history.TABLE} MATCH ? AND {where} ORDER BY bm25({history.TABLE}), m.id DESC LIMIT ?")
            for expression in ((_fts_and(words), _fts_or(words)) if len(words) > 1 else (_fts_and(words),)):
                rows = db.execute(sql, [expression, *args, limit]).fetchall()
                if rows:
                    break
        else:
            esc = lambda w: "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"  # noqa: E731
            like = " AND ".join("m.text LIKE ? ESCAPE '\\'" for _ in words)
            rows = db.execute(f"SELECT m.id, m.bot_id, m.role, m.created_at, substr(m.text, 1, {HIT_CHARS}) AS snippet "
                              f"FROM messages m WHERE {like} AND {where} ORDER BY m.id DESC LIMIT ?",
                              [*[esc(w) for w in words], *args, limit]).fetchall()
    except sqlite3.OperationalError as exc:
        raise RecallError(503 if "interrupt" in str(exc).lower() else 400, "search failed or took too long") from None
    finally:
        db.set_progress_handler(None, 0)
    hits, used = [], 0
    for row in rows:
        text = failures.redact(" ".join((row["snippet"] or "").split()))  # credential-shaped text is not handed on
        text = text if len(text) <= HIT_CHARS else text[: HIT_CHARS - 1] + "…"
        name = failures.redact(bots[row["bot_id"]])[:NAME_CHARS]
        size = len(text) + len(str(row["created_at"] or "")[:40]) + len(name) + 40  # the rendered line, not only the text
        if used + size > MAX_CHARS:
            break
        used += size
        hits.append({"id": row["id"], "bot_id": row["bot_id"], "bot": name, "role": row["role"], "at": str(row["created_at"] or "")[:40], "text": text})
    return hits


def run(store, job, query: str, scope: str = "self", limit: int = 10) -> dict:
    """The recall for a running *job*: its bot's own chat, plus readable teammates for scope team. Audited."""
    if scope not in SCOPES:
        raise RecallError(400, "scope must be self or team")
    if not SLOTS.acquire(timeout=SLOT_WAIT_S):  # released here, in the worker, whatever happens to the request
        raise RecallError(503, "recall is busy, try again")
    try:
        return _run(store, job, query, scope, limit)
    finally:
        SLOTS.release()


class _Ticket:
    """One admission ticket: returned exactly once, by the worker, or by the caller if the work never started."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "queued"

    def start(self) -> bool:
        with self._lock:
            if self._state != "queued":
                return False
            self._state = "running"
            return True

    def _give_back(self, only_if: str | None) -> None:
        with self._lock:
            if self._state == "released" or (only_if and self._state != only_if):
                return
            self._state = "released"
        ADMIT.release()

    def release(self) -> None:
        self._give_back(None)

    def abandon(self) -> None:
        self._give_back("queued")


def _worker(ticket: _Ticket, store, job, query: str, scope: str, limit: int) -> dict:
    if not ticket.start():  # the caller gave up before this ran
        raise RecallError(503, "recall was cancelled")
    try:
        return run(store, job, query, scope, limit)
    finally:
        ticket.release()


async def run_async(store, job, query: str, scope: str = "self", limit: int = 10) -> dict:
    """The route body: at most ``ADMIT`` searches in flight (refused before any thread is used), run off the event loop.

    The ticket goes back when the worker ends, or at once when the work never started (cancelled while queued in
    the executor, or the submission failed), so it cannot leak.
    """
    if not ADMIT.acquire(blocking=False):
        raise RecallError(503, "recall is busy, try again")
    ticket = _Ticket()
    try:
        future = asyncio.get_running_loop().run_in_executor(None, _worker, ticket, store, job, query, scope, limit)
    except BaseException:
        ticket.abandon()
        raise
    future.add_done_callback(lambda _f: ticket.abandon())
    return await future


def _run(store, job, query: str, scope: str, limit: int) -> dict:
    with closing(store._connect()) as db:
        bots = readable_bots(db, str(job.bot_id), scope)
        hits = search(db, bots, query, limit, exclude_job=str(job.id))
    for bot_id in sorted({h["bot_id"] for h in hits}) or [str(job.bot_id)]:
        team.log_read(store, job, "context_recall", bot_id, sum(1 for h in hits if h["bot_id"] == bot_id))
    return {"untrusted": True, "scope": scope, "count": len(hits), "hits": [{k: v for k, v in h.items() if k != "bot_id"} for h in hits]}


# ---- client side (the MCP server) -----------------------------------------------------------------------------

def fetch(client, args: dict) -> dict:
    from .team_client import TeamClientError
    query, scope, limit = args.get("query"), args.get("scope", "self"), args.get("limit", 10)
    if not isinstance(query, str) or not query.strip():
        raise TeamClientError("query is required")
    if scope not in SCOPES or isinstance(limit, bool) or not isinstance(limit, int):
        raise TeamClientError("scope must be self or team and limit an integer")
    return client.call("GET", "/v1/agent/recall?" + urlencode({"q": query[:QUERY_CHARS], "scope": scope,
                                                              "limit": max(1, min(limit, MAX_HITS))}))


def render(data: dict) -> str:
    from .team_client import wrap_untrusted
    lines = [f'#{h["id"]} {h["at"]} {h["bot"]} ({h["role"]}): {h["text"]}' for h in data.get("hits", [])]
    return wrap_untrusted(f'context_recall:{data.get("scope", "self")}', "\n".join(lines) or "(no matching retained messages)")
