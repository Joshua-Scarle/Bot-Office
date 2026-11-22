"""0.12.24: memory is a snapshot. Every note a bot is shown carries when it was last written and by whom.

memory.md, shared-memory.md and WORKING.md are split into notes (a top-level bullet or paragraph with its
continuation lines; headings, comments and code fences are not tagged on their own). A small ledger next to
each file (``<name>.ages.json``) records, per note, when that exact text was first written and who wrote it
(the owner from the GUI/API, a bot by name). Editing a note re-dates it, so a refreshed note is young again.

In a job's workspace the files are shown with one tag at the end of each note's first line::

    - Demo service: DEMO_ITEM_LIMIT 1500 ⟦Nov 21 · 1d · you⟧
    - Product status (as of 25 October 2026) ... ⟦≤Nov 16 · ≥6d OLD · source unknown⟧

``OLD``: older than ``OLD_DAYS``. ``EXPIRED``: the note says ``(expires YYYY-MM-DD)`` and that day has
passed. ``SUPERSEDED``: the note is marked ``SUPERSEDED`` (or struck through), i.e. history, not current.
Notes from before the ledger existed (or edited outside Bot-Office) are dated "on or before" the file's
modification time, with an unknown source.

The tags and the one-line header are removed again (``strip``) before a job's edits are merged back, so the
stored files never carry them; ``strip(annotate(x)) == x``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

from . import isolation, safefs

OLD_DAYS = 14
LEDGER_SUFFIX = ".ages.json"
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TAG = re.compile(r" ⟦[~≤]?" + _MON + r" \d{1,2} · [~≥]?\d+d(?: OLD)? · [^⟦⟧\n]{1,60}?(?: · EXPIRED)?(?: · SUPERSEDED)?⟧(?=[ \t]*$)")
HEADER_START = "<!-- Bot-Office note ages:"
_HEADER = re.compile(r"\A" + re.escape(HEADER_START) + r"[^\n]*-->\n")
_BULLET = re.compile(r"^(?:[-*+]\s|\d+[.)]\s)")
_EXPIRES = re.compile(r"\(\s*expires:?\s*(\d{4}-\d{2}-\d{2})\s*\)", re.IGNORECASE)
_SUPERSEDED = re.compile(r"\bSUPERSEDED\b|^\s*(?:[-*+]\s+)?~~")
OWNER = "owner"


def ledger_path(path: Path) -> Path:
    """Controller-private when isolated (isolation.state_dir), so a bot cannot forge a note's date or author."""
    return isolation.state_dir(path.parent) / (path.name.removesuffix(".md") + LEDGER_SUFFIX)


KEEP_GONE = 400  # entries of removed notes kept, so a restored or reverted note keeps its original date


def _key(lines: list[str]) -> str:
    text = "\n".join(" ".join(line.split()) for line in lines)
    return hashlib.sha256(text.encode()).hexdigest()[:20]


def notes(text: str) -> list[tuple[int, list[int], bool]]:
    """(first line index, all line indexes, taggable) per note of *text*."""
    lines = text.split("\n")
    out: list[tuple[int, list[int], bool]] = []
    current: list[int] | None = None
    fence = False
    for index, line in enumerate(lines):
        bare = line.strip()
        if bare.startswith(("```", "~~~")):
            fence = not fence
            if current is None:
                current = [index]
                out.append((index, current, False))
            else:
                current.append(index)
            continue
        if fence:
            if current is None:
                current = [index]
                out.append((index, current, False))
            else:
                current.append(index)
            continue
        if not bare:
            current = None
            continue
        if bare.startswith("#") or bare.startswith("<!--"):
            out.append((index, [index], False))
            current = None
            continue
        indented = line[:1].isspace()
        if current is None or (not indented and _BULLET.match(line)):
            current = [index]
            out.append((index, current, True))
        else:
            current.append(index)
    return out


def keys(text: str) -> list[str]:
    lines = text.split("\n")
    return [_key([lines[i] for i in members]) for _, members, _ in notes(text)]


def _valid(entry: object) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("at"), str) and len(entry["at"]) < 40
            and isinstance(entry.get("by", ""), str) and len(entry.get("by", "")) <= 200)


def _load_all(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    try:
        raw = safefs.read_text(ledger_path(path))
        data = json.loads(raw) if raw and len(raw) < 50_000_000 else {}
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    def clean(value: object) -> dict[str, dict]:
        return {k: v for k, v in value.items() if isinstance(k, str) and _valid(v)} if isinstance(value, dict) else {}
    return clean(data.get("notes")), clean(data.get("gone"))


def load(path: Path) -> dict[str, dict]:
    return _load_all(path)[0]


def _mtime(path: Path) -> datetime | None:
    info = safefs.lstat(path)
    return datetime.fromtimestamp(info.st_mtime, UTC) if info is not None else None


def record(path: Path, before: str, after: str, who: str, before_mtime: datetime | None,
           moment: datetime | None = None) -> None:
    """Update the ledger of *path* after *after* replaced *before* (call under the file's lock).
    Notes already in the ledger keep their entry; notes that were in *before* but untracked get
    "on or before" *before_mtime*; new notes get *moment* and *who*. Never raises."""
    with contextlib.suppress(Exception):
        moment = moment or datetime.now(UTC)
        ledger, gone = _load_all(path)
        old = set(keys(before))
        fresh: dict[str, dict] = {}
        for key in keys(after):
            if key in fresh:
                continue
            if key in ledger or key in gone:
                fresh[key] = ledger.get(key) or gone[key]
            elif key in old:
                fresh[key] = {"at": (before_mtime or moment).isoformat(), "by": "", "upper": True}
            else:
                fresh[key] = {"at": moment.isoformat(), "by": who}
        removed = {**gone, **{k: v for k, v in ledger.items() if k not in fresh}}
        removed = {k: v for k, v in removed.items() if k not in fresh}
        removed = dict(list(removed.items())[-KEEP_GONE:])
        _save(path, fresh, removed)


def _save(path: Path, notes_: dict, gone: dict | None = None) -> None:
    safefs.write_text(ledger_path(path), json.dumps({"v": 1, "notes": notes_, "gone": gone or {}}, ensure_ascii=False),
                      isolation.file_mode(), dir_mode=isolation.dir_mode())


def _when(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _who(by: str, self_name: str | None) -> str:
    if not by:
        return "source unknown"
    if self_name and by == self_name:
        return "you"
    by = " ".join(by.replace("⟦", "").replace("⟧", "").split())
    return (by if len(by) <= 40 else by[:39] + "…") or "source unknown"


def _tag(entry: dict | None, fallback: datetime | None, now: datetime, note_text: str,
         self_name: str | None) -> str:
    from .context import ET  # one ET definition

    at = _when(entry["at"]) if entry else None
    upper = bool(entry.get("upper")) if entry else True
    stated = bool(entry.get("stated")) if entry else False  # backfill: the newest date written in the note
    at = at or fallback or now
    days = max(0, (now - at).days)
    local = at.astimezone(ET)
    when = f"{'~' if stated else '≤' if upper else ''}{local:%b} {local.day}"
    age = f"{'~' if stated else '≥' if upper else ''}{days}d" + (" OLD" if days >= OLD_DAYS else "")
    who = "date in note" if stated else _who(entry.get("by", "") if entry else "", self_name)
    parts = [when, age, who]
    today = now.astimezone(ET).date()
    for match in _EXPIRES.finditer(note_text):
        with contextlib.suppress(ValueError):
            if date.fromisoformat(match.group(1)) < today:
                parts.append("EXPIRED")
                break
    if _SUPERSEDED.search(note_text):
        parts.append("SUPERSEDED")
    return " ⟦" + " · ".join(parts) + "⟧"


def header(now: datetime) -> str:
    from .context import et_label

    return (f"{HEADER_START} ⟦last written · age · by⟧ ends each note; now {et_label(now)}. A note is a snapshot "
            f"from that date and may be out of date: check the live source before relying on one that can change. "
            f"OLD = {OLD_DAYS}+ days (age only: a standing rule still applies). Tags and this line are removed when the file is saved. -->\n")


def taggable(text: str) -> bool:
    """False when *text* already has ⟦ ⟧ of its own: it is then shown untagged and never stripped, so
    stripping cannot remove anything the file really contains."""
    return "⟦" not in text and "⟧" not in text and HEADER_START not in text


MAX_TAGGED = 1_000_000  # = memory.MAX_MEMORY_BYTES: a tagged copy always reads back whole


def annotate(text: str, path: Path, now: datetime | None = None, self_name: str | None = None,
             with_header: bool = True) -> str:
    """*text* (the content of *path*) with a ⟦date · age · by⟧ tag per note and a header line.
    Never raises: on any error the text is returned untagged."""
    if not text.strip() or not taggable(text):
        return text
    try:
        now = now or datetime.now(UTC)
        ledger = load(path)
        fallback = _mtime(path)
        lines = text.split("\n")
        for first, members, tagged in notes(text):
            if not tagged:
                continue
            body = [lines[i] for i in members]
            lines[first] = lines[first] + _tag(ledger.get(_key(body)), fallback, now, "\n".join(body), self_name)
        tagged = (header(now) if with_header else "") + "\n".join(lines)
        return tagged if len(tagged) <= MAX_TAGGED else text  # too large to tag: shown as stored
    except Exception:  # noqa: BLE001 - a tagging bug must never fail the turn
        return text


def strip(text: str, base: str = "") -> str:
    """Remove what ``annotate`` added (the header line and the ⟦…⟧ tags at line ends). *base*: the stored
    text the workspace copy was made from; if it was not taggable nothing was added, so nothing is removed."""
    if base and not taggable(base):
        return text
    out, fence = [], False
    for line in _HEADER.sub("", text, count=1).split("\n"):
        if line.strip().startswith(("```", "~~~")):
            fence = not fence
        out.append(line if fence else _TAG.sub("", line))  # never inside a code fence (none are added there)
    return "\n".join(out)


def saved_at(path: Path) -> str:
    """'Nov 21 4:22 PM ET (1d ago)' for the file's last write, or '' if it does not exist."""
    from .context import et_label

    moment = _mtime(path)
    if moment is None:
        return ""
    days = (datetime.now(UTC) - moment).days
    return f"{et_label(moment)} ({days}d ago)" if days else f"{et_label(moment)} (<1d ago)"


_DATES = (
    (re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(\d{1,2}) (January|February|March|April|May|June|July|August|September|October|November|December) "
                r"(20\d\d)\b"), lambda m: (int(m[3]), _MONTHS.index(m[2][:3]) + 1, int(m[1]))),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2})(?:, (20\d\d))?\b"),
     lambda m: (int(m[3] or 0), _MONTHS.index(m[1]) + 1, int(m[2]))),
)
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def stated_date(text: str, year: int, not_after: datetime) -> datetime | None:
    """The newest date written in *text* that is not after *not_after* (a year-less 'Nov 21' takes *year*)."""
    best = None
    for pattern, parts in _DATES:
        for match in pattern.finditer(text):
            with contextlib.suppress(ValueError, IndexError):
                y, m, d = parts(match)
                moment = datetime(y or year, m, d, 12, tzinfo=UTC)
                if moment <= not_after and (best is None or moment > best):
                    best = moment
    return best


def backfill(path: Path, events: list[tuple[datetime, str, str]], now: datetime | None = None) -> int:
    """One-time ledger for a file written before 0.12.24 (no ledger yet). Per note: the latest memory-edit
    event (time, writer, diff) that added one of its lines; else the newest date written in the note; else
    "on or before" the file's time. Returns the number of notes dated."""
    if safefs.lstat(ledger_path(path)) is not None:
        return 0
    text = safefs.read_text(path)
    if not text.strip() or not taggable(text):
        return 0
    now = now or datetime.now(UTC)
    mtime = _mtime(path) or now
    added: dict[str, tuple[datetime, str]] = {}
    for moment, who, diff in sorted(events):
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                bare = " ".join(line[1:].split())
                if len(bare) >= 12:
                    added[bare] = (moment, who)
    lines = text.split("\n")
    out: dict[str, dict] = {}
    heading = ""
    for first, members, tagged in notes(text):
        body = [lines[i] for i in members]
        if not tagged and lines[first].lstrip().startswith("#"):
            heading = lines[first]  # "### Product status (as of 25 October 2026)" dates the notes below it
        key = _key(body)
        hits = [added[b] for b in (" ".join(line.split()) for line in body) if b in added]
        if hits:
            moment, who = max(hits)
            out[key] = {"at": moment.isoformat(), "by": who}
        elif (stated := stated_date("\n".join(body), mtime.year, mtime)
              or stated_date(heading, mtime.year, mtime)) is not None:
            out[key] = {"at": stated.isoformat(), "by": "", "stated": True}
        else:
            out[key] = {"at": mtime.isoformat(), "by": "", "upper": True}
    _save(path, out)
    return len(out)
