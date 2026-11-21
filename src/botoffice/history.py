"""Message search (0.12.5, reworked in 0.12.7): an FTS5 index over chat messages, with a LIKE fallback.

0.12.7 indexes the message text, the names of attached files and Bot Office's own status lines (wake-ups,
continuations: the ``hidden`` rows) and orders hits by FTS5 bm25 relevance, newest first on a tie. The index is
``messages_search`` (its own copy of the text plus the attachment names), kept in sync by triggers, created and
backfilled once (under the write lock) the first time a 0.12.7 store opens the database. The 0.12.5 index
``messages_fts`` is dropped then; nothing else is altered, so an older version keeps working on the same file (it
rebuilds its own index at its next start, and the 0.12.7 triggers keep this one current meanwhile).
"""
from __future__ import annotations

import re
import sqlite3

FTS_MIGRATION = "messages-search-v2"
TABLE = "messages_search"
LEGACY_TABLE = "messages_fts"  # 0.12.5 / 0.12.6
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"  # private highlight markers in a snippet; the client turns them into <mark>
SNIPPET_TOKENS = 24
_WORD = re.compile(r"\w+", re.UNICODE)


def names_sql(row: str) -> str:
    """SQL for the display names of the files in ``<row>.attachments`` (a JSON list of stored ids). A stored file id is
    ``<date>-<32 hex>-<name>`` (44 characters before the name); an image id has no name and gives nothing."""
    return (f"CASE WHEN json_valid({row}.attachments) THEN COALESCE((SELECT group_concat(substr(j.value, 45), ' ') "
            f"FROM json_each({row}.attachments) j WHERE j.type='text' AND length(j.value) > 44 "
            "AND substr(j.value, 11, 1)='-' AND substr(j.value, 44, 1)='-'), '') ELSE '' END")


TRIGGER_NAMES = (f"{TABLE}_ai", f"{TABLE}_ad", f"{TABLE}_au")
_TRIGGERS = (
    f"CREATE TRIGGER IF NOT EXISTS {TABLE}_ai AFTER INSERT ON messages BEGIN "
    f"INSERT INTO {TABLE}(rowid, text, names) VALUES (new.id, new.text, {names_sql('new')}); END",
    f"CREATE TRIGGER IF NOT EXISTS {TABLE}_ad AFTER DELETE ON messages BEGIN "
    f"DELETE FROM {TABLE} WHERE rowid=old.id; END",
    f"CREATE TRIGGER IF NOT EXISTS {TABLE}_au AFTER UPDATE OF text, attachments ON messages BEGIN "
    f"DELETE FROM {TABLE} WHERE rowid=old.id; "
    f"INSERT INTO {TABLE}(rowid, text, names) VALUES (new.id, new.text, {names_sql('new')}); END",
)
_BACKFILL = f"INSERT INTO {TABLE}(rowid, text, names) SELECT m.id, m.text, {names_sql('m')} FROM messages m"


def _drop_legacy(db: sqlite3.Connection) -> None:
    """Remove the 0.12.5 index and its triggers (an older version that ran meanwhile recreates them)."""
    for name in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    try:
        db.execute(f"DROP TABLE IF EXISTS {LEGACY_TABLE}")
    except sqlite3.OperationalError:
        pass  # a build without FTS5 cannot drop it; the table is harmless


def ensure_fts(db: sqlite3.Connection, meta_set) -> bool:
    """Create and backfill the index once. Returns False (and leaves the database as it was) if FTS5 is missing."""
    if db.execute(f"SELECT 1 FROM sqlite_master WHERE name='{TABLE}'").fetchone():
        return _sync_triggers(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute(f"SELECT 1 FROM sqlite_master WHERE name='{TABLE}'").fetchone():
            db.rollback()  # another process finished first
            return True
        db.execute(f"CREATE VIRTUAL TABLE {TABLE} USING fts5(text, names, tokenize='unicode61 remove_diacritics 2')")
        for ddl in _TRIGGERS:
            db.execute(ddl)
        db.execute(_BACKFILL)
        _drop_legacy(db)
        meta_set(db, FTS_MIGRATION)
        db.commit()
        return True
    except sqlite3.OperationalError as exc:
        db.rollback()
        if "fts5" not in str(exc).lower():
            raise  # a lock or disk error is not "no FTS5": let the start fail rather than hide it
        return False
    except BaseException:
        db.rollback()
        raise


def _sync_triggers(db: sqlite3.Connection) -> bool:
    """The index exists. If this SQLite cannot read it (a file copied to a build without FTS5) drop the triggers,
    so chat writes keep working and search falls back to LIKE; once it can read it again, bring the index up to
    date and restore the triggers. A 0.12.5 index left by a rollback is removed."""
    usable = has_fts(db)
    have = {r[0] for r in db.execute(f"SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '{TABLE}_%'")}
    legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'messages_fts%'").fetchone() is not None
    if usable and len(have) == len(_TRIGGERS) and not legacy:
        return True
    if not usable and not have:
        return False
    db.execute("BEGIN IMMEDIATE")
    try:
        if usable:
            for ddl in _TRIGGERS:
                db.execute(ddl)
            if len(have) != len(_TRIGGERS):  # triggers were missing: the index may have missed writes
                db.execute(f"DELETE FROM {TABLE}")
                db.execute(_BACKFILL)
            _drop_legacy(db)
        else:
            for name in TRIGGER_NAMES:
                db.execute(f"DROP TRIGGER IF EXISTS {name}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return usable


def has_fts(db: sqlite3.Connection) -> bool:
    """The index exists and this SQLite can read it (a file copied to a build without FTS5 reports False)."""
    if db.execute(f"SELECT 1 FROM sqlite_master WHERE name='{TABLE}'").fetchone() is None:
        return False
    try:
        db.execute(f"SELECT 1 FROM {TABLE} LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


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
           before: int | None = None, offset: int = 0) -> tuple[list[dict], bool]:
    """Best match first among messages of existing bots: chat text, attached file names and Bot Office's own status
    lines (``hidden`` is true on those; they are not shown in the chat). Archived (rewound) messages are not searched.

    FTS5 orders by bm25 (newest first on a tie); the LIKE fallback puts a message holding the whole query first,
    then the shorter message (the denser match), then the newer. ``offset`` pages (``before`` still filters by id).
    Returns (hits, has_more). A hit: id, bot_id, bot_name, role, sender, created_at, hidden, snippet.
    """
    words = tokens(query)
    if not words:
        return [], False
    where = "m.archived_at IS NULL"
    args: list = []
    if bot_id:
        where += " AND m.bot_id=?"
        args.append(bot_id)
    if before is not None:
        where += " AND m.id<?"
        args.append(before)
    head = "SELECT m.id, m.bot_id, b.name AS bot_name, m.role, m.sender, m.created_at, m.hidden, "
    if has_fts(db):
        sql = (head + f"snippet({TABLE}, -1, char(2), char(3), '…', {SNIPPET_TOKENS}) AS snippet "
               f"FROM {TABLE} JOIN messages m ON m.id={TABLE}.rowid JOIN bots b ON b.id=m.bot_id "
               f"WHERE {TABLE} MATCH ? AND {where} ORDER BY bm25({TABLE}), m.id DESC LIMIT ? OFFSET ?")
        rows = db.execute(sql, [fts_query(words), *args, limit + 1, offset]).fetchall()
    else:
        names = names_sql("m")
        like = " AND ".join(f"(m.text LIKE ? ESCAPE '\\' OR {names} LIKE ? ESCAPE '\\')" for _ in words)
        esc = lambda w: "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"  # noqa: E731
        whole = esc(" ".join(words))
        sql = (head + "m.text || CASE WHEN " + names + " != '' THEN ' ' || " + names + " ELSE '' END AS snippet "
               "FROM messages m JOIN bots b ON b.id=m.bot_id "
               f"WHERE {like} AND {where} "
               "ORDER BY (m.text LIKE ? ESCAPE '\\') DESC, length(m.text) ASC, m.id DESC LIMIT ? OFFSET ?")
        params = [p for w in words for p in (esc(w), esc(w))]
        rows = db.execute(sql, [*params, *args, whole, limit + 1, offset]).fetchall()
        rows = [{**dict(r), "snippet": _like_snippet(r["snippet"], words)} for r in rows]
    hits = [{**dict(r), "hidden": bool(r["hidden"])} for r in rows[:limit]]
    return hits, len(rows) > limit
