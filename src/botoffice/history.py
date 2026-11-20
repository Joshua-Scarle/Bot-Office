"""Message search (0.12.5): an FTS5 index over chat messages, with a LIKE fallback.

The index is an external-content table over ``messages`` kept in sync by triggers, created and backfilled once
(under the write lock) the first time a 0.12.5 store opens the database. Nothing existing is altered, so an older
version keeps working on the same file.
"""
from __future__ import annotations

import re
import sqlite3

FTS_MIGRATION = "messages-fts-v1"
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"  # private highlight markers in a snippet; the client turns them into <mark>
SNIPPET_TOKENS = 24
_WORD = re.compile(r"\w+", re.UNICODE)

_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN "
    "INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN "
    "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN "
    "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); "
    "INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END",
)


def ensure_fts(db: sqlite3.Connection, meta_set) -> bool:
    """Create and backfill the index once. Returns False (and leaves the database as it was) if FTS5 is missing."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone():
        return True
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone():
            db.rollback()  # another process finished first
            return True
        db.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id', "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        for ddl in _TRIGGERS:
            db.execute(ddl)
        db.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        meta_set(db, FTS_MIGRATION)
        db.commit()
        return True
    except sqlite3.OperationalError:
        db.rollback()
        return False
    except BaseException:
        db.rollback()
        raise


def has_fts(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone() is not None


def tokens(query: str) -> list[str]:
    return _WORD.findall(query)[:12]


def fts_query(words: list[str]) -> str:
    """Every word quoted (no FTS operator can be typed in); the last is a prefix match, for search as you type."""
    quoted = ['"' + w.replace('"', '""') + '"' for w in words]
    quoted[-1] += "*"
    return " ".join(quoted)


def _like_snippet(text: str, words: list[str]) -> str:
    low = text.lower()
    at = min((i for i in (low.find(w.lower()) for w in words) if i >= 0), default=0)
    start = max(0, at - 60)
    end = min(len(text), at + 140)
    piece = text[start:end]
    for word in sorted(set(words), key=len, reverse=True):
        piece = re.sub(re.escape(word), lambda m: MARK_OPEN + m.group(0) + MARK_CLOSE, piece, flags=re.IGNORECASE)
    return ("…" if start else "") + piece + ("…" if end < len(text) else "")


def search(db: sqlite3.Connection, query: str, *, bot_id: str | None = None, limit: int = 30,
           before: int | None = None) -> tuple[list[dict], bool]:
    """Newest-first hits among visible messages (not archived, not Bot-Office machinery) of existing bots.

    Returns (hits, has_more). A hit: id, bot_id, bot_name, role, sender, created_at, snippet (markers around matches).
    """
    words = tokens(query)
    if not words:
        return [], False
    where = "m.archived_at IS NULL AND m.hidden=0"
    args: list = []
    if bot_id:
        where += " AND m.bot_id=?"
        args.append(bot_id)
    if before is not None:
        where += " AND m.id<?"
        args.append(before)
    head = "SELECT m.id, m.bot_id, b.name AS bot_name, m.role, m.sender, m.created_at, "
    if has_fts(db):
        sql = (head + f"snippet(messages_fts, 0, char(2), char(3), '…', {SNIPPET_TOKENS}) AS snippet "
               "FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid JOIN bots b ON b.id=m.bot_id "
               f"WHERE messages_fts MATCH ? AND {where} ORDER BY m.id DESC LIMIT ?")
        params = [fts_query(words), *args, limit + 1]
        rows = db.execute(sql, params).fetchall()
    else:
        like = " AND ".join("m.text LIKE ? ESCAPE '\\'" for _ in words)
        sql = (head + "m.text AS snippet FROM messages m JOIN bots b ON b.id=m.bot_id "
               f"WHERE {like} AND {where} ORDER BY m.id DESC LIMIT ?")
        escaped = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
        rows = db.execute(sql, [*escaped, *args, limit + 1]).fetchall()
        rows = [{**dict(r), "snippet": _like_snippet(r["snippet"], words)} for r in rows]
    hits = [dict(r) for r in rows[:limit]]
    return hits, len(rows) > limit
