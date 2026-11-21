"""Sidebar sections (0.12.0): named groups of bots, their order, and each section's collapsed state.

A bot belongs to at most one section. "Ungrouped" is virtual and always last: bots with
``section_id = ''`` (an explicit order) come first, then bots with no row at all in the default
name order. Every write is one ``BEGIN IMMEDIATE`` transaction that renumbers positions densely.

0.12.12: "Hidden" is a built-in section (fixed id ``hidden``, a real ``bot_sections`` row created idempotently at
startup, collapsed by default). It cannot be renamed, deleted or reordered, its name is reserved, and the sidebar
draws it after Ungrouped. The layout reports it under its own ``hidden`` key, not in ``sections``. Hiding is
only where the sidebar lists a bot: nothing else in the controller reads these tables.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

UNGROUPED = ""
UNGROUPED_ALIAS = "ungrouped"
HIDDEN = "hidden"
HIDDEN_NAME = "Hidden"
HIDDEN_POSITION = 1_000_000          # old code (rollback) lists it as an ordinary section, always last
RESERVED = (UNGROUPED_ALIAS, HIDDEN)
MAX_NAME = 60
MAX_SECTIONS = 100
UNGROUPED_COLLAPSED_KEY = "sections.ungrouped_collapsed"


class SectionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS bot_sections (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL,
            collapsed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS bot_section_members (
            bot_id TEXT PRIMARY KEY, section_id TEXT NOT NULL, position INTEGER NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS bot_section_members_section ON bot_section_members (section_id, position)")
    ensure_hidden(db)


_BLANKS = "\u115f\u1160\u3164\uffa0\u2800"   # invisible fillers that are not format characters (Hangul fillers, braille blank)


def _strip_marks(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc", "Mn", "Mc", "Me") and ch not in _BLANKS)


def reserved_key(name: str) -> str:
    """Case-, width-, invisible-character- and combining-mark-insensitive form used to compare against the reserved names.
    Marks go before AND after casefolding: casefold turns U+0345 (a mark) into a letter and can add marks of its own.
    Deliberately NOT done: folding visually confusable letters (Cyrillic or Greek capitals, Latin small capitals such as U+026A).
    The list is open-ended and a lookalike section is only a section: the built-in one is found by its fixed id, so a
    lookalike name gains nothing (no membership, no collapse state, no deletion rights); it can only look similar."""
    text = _strip_marks(unicodedata.normalize("NFKD", name))
    return _strip_marks(unicodedata.normalize("NFKD", text.casefold())).strip()


def ensure_hidden(db: sqlite3.Connection) -> None:
    """Create the built-in Hidden section if it is missing (collapsed), put its name back if older code renamed it,
    and move a user section that already took the name out of the way. Safe to run on every start."""
    db.execute("BEGIN IMMEDIATE")
    try:
        taken = [r["name"] for r in db.execute("SELECT name FROM bot_sections WHERE id<>?", (HIDDEN,))]
        for row in db.execute("SELECT id, name FROM bot_sections WHERE id<>?", (HIDDEN,)).fetchall():
            if reserved_key(row["name"]) == HIDDEN:
                new = f"{row['name']} (old)"
                while any(new.casefold() == t.casefold() for t in taken):
                    new += "'"
                db.execute("UPDATE bot_sections SET name=? WHERE id=?", (new, row["id"]))
                taken.append(new)
        db.execute(
            "INSERT OR IGNORE INTO bot_sections (id, name, position, collapsed, created_at) VALUES (?,?,?,1,?)",
            (HIDDEN, HIDDEN_NAME, HIDDEN_POSITION, datetime.now(UTC).isoformat()))
        db.execute("UPDATE bot_sections SET name=?, position=? WHERE id=? AND (name<>? OR position<>?)",
                   (HIDDEN_NAME, HIDDEN_POSITION, HIDDEN, HIDDEN_NAME, HIDDEN_POSITION))
        db.commit()
    except BaseException:
        db.rollback()
        raise

def clean_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise SectionError(400, "section name is required")
    if len(name) > MAX_NAME:
        raise SectionError(400, f"section name is too long (max {MAX_NAME} characters)")
    if any(ord(ch) < 32 for ch in name):
        raise SectionError(400, "section name has control characters")
    return name


class Sections:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect

    # ---- reads -----------------------------------------------------------------------------
    @staticmethod
    def _layout(db: sqlite3.Connection) -> dict:
        every = [dict(r) for r in db.execute("SELECT * FROM bot_sections ORDER BY position, id")]
        sections = [s for s in every if s["id"] != HIDDEN]
        hidden = next((s for s in every if s["id"] == HIDDEN), {"collapsed": 1})
        known = {s["id"] for s in every}
        bots = [r["id"] for r in db.execute("SELECT id FROM bots ORDER BY name COLLATE NOCASE, name")]
        alive = set(bots)
        lists: dict[str, list[str]] = {s["id"]: [] for s in every}
        lists[UNGROUPED] = []
        placed: set[str] = set()
        for row in db.execute("SELECT bot_id, section_id FROM bot_section_members ORDER BY position, bot_id"):
            if row["bot_id"] not in alive:
                continue
            target = row["section_id"] if row["section_id"] in known else UNGROUPED
            lists[target].append(row["bot_id"])
            placed.add(row["bot_id"])
        lists[UNGROUPED].extend(b for b in bots if b not in placed)
        flag = db.execute("SELECT value FROM meta WHERE key=?", (UNGROUPED_COLLAPSED_KEY,)).fetchone()
        return {
            "sections": [
                {"id": s["id"], "name": s["name"], "position": i, "collapsed": bool(s["collapsed"]),
                 "bot_ids": lists[s["id"]]}
                for i, s in enumerate(sections)
            ],
            "ungrouped": {"collapsed": bool(flag and flag["value"] == "1"), "bot_ids": lists[UNGROUPED]},
            "hidden": {"id": HIDDEN, "name": HIDDEN_NAME, "builtin": True, "collapsed": bool(hidden["collapsed"]),
                       "bot_ids": lists.get(HIDDEN, [])},
        }

    def layout(self) -> dict:
        with closing(self._connect()) as db:
            return self._layout(db)

    # ---- writes ----------------------------------------------------------------------------
    def _write(self, fn):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = fn(db)
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _section(db: sqlite3.Connection, section_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM bot_sections WHERE id=?", (section_id,)).fetchone()
        if row is None:
            raise SectionError(404, "section not found")
        return row

    @staticmethod
    def _unique(db: sqlite3.Connection, name: str, except_id: str | None = None) -> None:
        key = reserved_key(name)
        if key == UNGROUPED_ALIAS:
            raise SectionError(409, "\"Ungrouped\" is reserved for bots without a section")
        if key == HIDDEN:
            raise SectionError(409, "\"Hidden\" is reserved for the built-in Hidden section")
        for row in db.execute("SELECT id, name FROM bot_sections"):
            if row["id"] != except_id and row["name"].casefold() == name.casefold():
                raise SectionError(409, "a section with that name already exists")

    @staticmethod
    def _put_list(db: sqlite3.Connection, section_id: str, bot_ids: list[str]) -> None:
        for position, bot_id in enumerate(bot_ids):
            db.execute(
                "INSERT INTO bot_section_members (bot_id, section_id, position) VALUES (?,?,?) "
                "ON CONFLICT(bot_id) DO UPDATE SET section_id=excluded.section_id, position=excluded.position",
                (bot_id, section_id, position),
            )

    def create(self, name: object) -> dict:
        name = clean_name(name)

        def run(db):
            self._unique(db, name)
            count, top = db.execute("SELECT COUNT(*), COALESCE(MAX(position), -1) FROM bot_sections WHERE id<>?", (HIDDEN,)).fetchone()
            if count >= MAX_SECTIONS:
                raise SectionError(409, f"at most {MAX_SECTIONS} sections")
            section_id = uuid4().hex[:12]
            db.execute("INSERT INTO bot_sections (id, name, position, collapsed, created_at) VALUES (?,?,?,0,?)",
                       (section_id, name, top + 1, datetime.now(UTC).isoformat()))
            return section_id

        section_id = self._write(run)
        return self._found(section_id)

    def update(self, section_id: str, name: object = None, collapsed: bool | None = None) -> dict:
        def run(db):
            if section_id == UNGROUPED_ALIAS:
                if name is not None:
                    raise SectionError(400, "Ungrouped cannot be renamed")
                if collapsed is not None:
                    db.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (UNGROUPED_COLLAPSED_KEY, "1" if collapsed else "0"))
                return
            self._section(db, section_id)
            if section_id == HIDDEN and name is not None:
                raise SectionError(409, "Hidden is built in and cannot be renamed")
            if name is not None:
                clean = clean_name(name)
                self._unique(db, clean, section_id)
                db.execute("UPDATE bot_sections SET name=? WHERE id=?", (clean, section_id))
            if collapsed is not None:
                db.execute("UPDATE bot_sections SET collapsed=? WHERE id=?", (1 if collapsed else 0, section_id))

        self._write(run)
        if section_id == UNGROUPED_ALIAS:
            return {"id": UNGROUPED_ALIAS, "name": "Ungrouped", **self.layout()["ungrouped"]}
        if section_id == HIDDEN:
            return self.layout()["hidden"]
        return self._found(section_id)

    def _found(self, section_id: str) -> dict:
        found = next((s for s in self.layout()["sections"] if s["id"] == section_id), None)
        if found is None:   # removed by another request between the write and this read
            raise SectionError(404, "section not found")
        return found

    def delete(self, section_id: str) -> list[str]:
        """Remove a section; its bots go to the top of Ungrouped, in their order. Returns those bot ids."""
        def run(db):
            if section_id == HIDDEN:
                raise SectionError(409, "Hidden is built in and cannot be deleted")
            self._section(db, section_id)
            layout = self._layout(db)
            moved = next(s for s in layout["sections"] if s["id"] == section_id)["bot_ids"]
            self._put_list(db, UNGROUPED, moved + layout["ungrouped"]["bot_ids"])
            db.execute("DELETE FROM bot_sections WHERE id=?", (section_id,))
            db.execute("DELETE FROM bot_section_members WHERE section_id=?", (section_id,))
            self._renumber_sections(db)
            return moved

        return self._write(run)

    @staticmethod
    def _renumber_sections(db: sqlite3.Connection) -> None:
        for position, row in enumerate(db.execute("SELECT id FROM bot_sections WHERE id<>? ORDER BY position, id", (HIDDEN,)).fetchall()):
            db.execute("UPDATE bot_sections SET position=? WHERE id=?", (position, row["id"]))

    def reorder(self, ids: list[str]) -> dict:
        ids = [i for i in ids if i != HIDDEN]   # Hidden has no place in the order: it is always last
        def run(db):
            current = [r["id"] for r in db.execute("SELECT id FROM bot_sections WHERE id<>? ORDER BY position, id", (HIDDEN,))]
            if sorted(ids) != sorted(current) or len(set(ids)) != len(ids):
                raise SectionError(409, "the section list changed; reload and try again")
            for position, section_id in enumerate(ids):
                db.execute("UPDATE bot_sections SET position=? WHERE id=?", (position, section_id))

        self._write(run)
        return self.layout()

    def move_bot(self, bot_id: str, section_id: str | None, index: int) -> dict:
        """Put a bot into a section (None: Ungrouped) at an index of that section's visible list."""
        target = UNGROUPED if section_id in (None, "", UNGROUPED_ALIAS) else section_id

        def run(db):
            if db.execute("SELECT 1 FROM bots WHERE id=?", (bot_id,)).fetchone() is None:
                raise SectionError(404, "bot not found")
            if target != UNGROUPED:
                self._section(db, target)
            layout = self._layout(db)
            lists = {s["id"]: s["bot_ids"] for s in layout["sections"]}
            lists[UNGROUPED] = layout["ungrouped"]["bot_ids"]
            lists[HIDDEN] = layout["hidden"]["bot_ids"]
            source = next((key for key, ids in lists.items() if bot_id in ids), UNGROUPED)
            was_explicit = source != UNGROUPED or db.execute(
                "SELECT 1 FROM bot_section_members WHERE bot_id=?", (bot_id,)).fetchone() is not None
            dest = [b for b in lists[target] if b != bot_id]
            dest.insert(max(0, min(int(index), len(dest))), bot_id)
            if source != target and was_explicit:
                self._put_list(db, source, [b for b in lists[source] if b != bot_id])
            self._put_list(db, target, dest)

        self._write(run)
        return self.layout()

    def set_hidden(self, bot_ids: list[str]) -> dict:
        """Make exactly these bots the Hidden section's members (in the given order); bots that were hidden and are
        not listed go back to the top of Ungrouped. Unknown ids are a 404 and change nothing; repeats are ignored."""
        wanted = list(dict.fromkeys(bot_ids))

        def run(db):
            alive = {r["id"] for r in db.execute("SELECT id FROM bots")}
            missing = [b for b in wanted if b not in alive]
            if missing:
                raise SectionError(404, "bot not found: " + ", ".join(missing[:5]))
            layout = self._layout(db)
            released = [b for b in layout["hidden"]["bot_ids"] if b not in wanted]
            ungrouped = [b for b in layout["ungrouped"]["bot_ids"] if b not in wanted]
            for bot_id in wanted:   # leave any other section's list dense
                for sec in layout["sections"]:
                    if bot_id in sec["bot_ids"]:
                        self._put_list(db, sec["id"], [b for b in sec["bot_ids"] if b not in wanted])
            self._put_list(db, HIDDEN, wanted)
            if released:
                self._put_list(db, UNGROUPED, released + ungrouped)

        self._write(run)
        return self.layout()

    def forget_bot(self, db: sqlite3.Connection, bot_id: str) -> None:
        """Called inside delete_bot's transaction."""
        db.execute("DELETE FROM bot_section_members WHERE bot_id=?", (bot_id,))
