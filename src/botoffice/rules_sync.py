"""0.12.39 rules sync: rule files in a drop folder become dated notes in the bots' memory.md, without duplicates.

Drop folder: ``BOT_OFFICE_RULES_SYNC_DIR`` (default ``~/bo-sync/rules/``). One Markdown file per bot. The bot is
named by a ``bot: <name>`` line among the first lines of the file, else by the file name (the bot's name as a
slug, e.g. ``engineering-lead-bo.md``). A rule is one line::

    - [2026-11-22 07:12 ET | the owner, via grok handoff engineering-lead.md | id=demo-item-limit] Remove DEMO_ITEM_LIMIT ...

``[date[ time ET] | source | id=key]`` is required (other lines are ignored, so a file can carry headings and
notes); ``id=`` is optional. In memory.md the rule becomes a note under the heading ``SECTION``::

    - RULE (2026-11-22 07:12 ET; source: the owner, via grok handoff engineering-lead.md) [rule:demo-item-limit] Remove ...

and the note-age ledger (freshness, 0.12.27) dates it to the rule's own date with the writer "rules sync", so the
bot sees ``⟦Nov 22 · 4d · rules sync⟧``. A rule is already present when a note carries its ``[rule:key]`` with the
same text, or (no id) when a note has the same text; a rule whose id is present with other text is changed in
place (and re-dated). Nothing is ever removed: a rule dropped from the file stays in memory.

Runs: once a day from the scheduler (first pass after midnight ET, or at start when today's pass is missing),
only for files new or changed since their last import (sha256), and on demand (``POST /v1/rules-sync/run``).
A file naming no bot, or a bot that does not exist, is reported (``data/logs/rules-sync.log`` and the run's
result), never guessed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import JobStore

log = logging.getLogger(__name__)
META_KEY = "rules_sync"
SECTION = "## Rules and decisions (synced; each with its date and source)"
WHO = "rules sync"
ET = ZoneInfo("America/New_York")
MAX_FILE_BYTES = 200_000
MAX_RULES_PER_FILE = 200
_LOCK = threading.Lock()  # one run at a time (the daily pass and POST /v1/rules-sync/run)
_RULE = re.compile(
    r"^\s*[-*]\s*\[\s*(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{1,2}:\d{2})(?:\s*ET)?)?\s*\|\s*(?P<source>[^|\]]{1,200}?)"
    r"\s*(?:\|\s*id\s*=\s*(?P<id>[A-Za-z0-9][\w.-]{0,63})\s*)?\]\s*(?P<text>\S.*?)\s*$")
_BOT = re.compile(r"^\s*bot\s*:\s*(?P<name>.+?)\s*$", re.IGNORECASE)
# the id of a note this sync wrote: the first "[rule:key]" right after its "RULE (...)" metadata (a source never
# holds "]", and the rule text comes after the mark, so a "[rule:x]" inside the text is never taken for its id)
_SYNCED_ID = re.compile(r"^- RULE \(.*?\) \[rule:([A-Za-z0-9][\w.-]{0,63})\] ")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def norm(text: str) -> str:
    """Whitespace folded only: a changed letter case or comma is a changed rule."""
    return " ".join(text.split())


# a memory line's own text: without the bullet and a leading "RULE (...)" / "[rule:key]" prefix
_PREFIX = re.compile(r"^\s*(?:[-*+]\s+)?(?:RULE\s*\([^)]{0,200}\)\s*(?:\[rule:[\w.-]{1,64}\])?\s*:?\s*)?")


_SYNCED_TEXT = re.compile(r"^- RULE \(.*?\) \[rule:(?:-|[A-Za-z0-9][\w.-]{0,63})\] (?P<text>.*)$")
_NOTE_LINE = re.compile(r"^\s*[-*+]\s+\S")  # a bullet note: the only kind of line a rule is compared with


def _text_of(line: str) -> str:
    """The note's own words: a synced note's text after its mark (its source may hold parentheses),
    else the line without the bullet and a hand-written "RULE (...):" prefix; "" for a non-note line."""
    synced = _SYNCED_TEXT.match(line)
    if synced:
        return norm(synced["text"])
    if not _NOTE_LINE.match(line):
        return ""
    return norm(_PREFIX.sub("", line, count=1))


def parse(text: str) -> tuple[str | None, list[dict]]:
    """(bot name from a ``bot:`` line or None, rules)."""
    name, rules = None, []
    for number, line in enumerate(text.splitlines()):
        if name is None and number < 10 and (found := _BOT.match(line)):
            name = found["name"]
            continue
        found = _RULE.match(line)
        if not found:
            continue
        moment = datetime.fromisoformat(found["date"] + "T" + (found["time"] or "12:00").zfill(5)).replace(tzinfo=ET)
        when = found["date"] + (f" {found['time']} ET" if found["time"] else "")
        rules.append({"id": found["id"], "when": when, "moment": moment.astimezone(UTC),
                      "source": " ".join(found["source"].split()), "text": " ".join(found["text"].split())})
    return name, rules


def note(rule: dict) -> str:
    # every synced note carries a mark right after its metadata ("[rule:-]" without an id), so rule text that
    # itself starts with "[rule:x]" can never be read as the note's id
    mark = f" [rule:{rule['id']}]" if rule["id"] else " [rule:-]"
    return f"- RULE ({rule['when']}; source: {rule['source']}){mark} {rule['text']}"


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _live_lines(lines: list[str]) -> list[int]:
    """Indexes of the lines outside fenced code blocks (CommonMark: a fence of 3+ backticks or tildes is
    closed only by the same character, at least as many, and nothing else on the line)."""
    out, fence = [], None
    for index, line in enumerate(lines):
        found = _FENCE.match(line)
        if fence is None:
            if found:
                fence = found.group(1)
                continue
            out.append(index)
        elif found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence) \
                and not line.strip()[len(found.group(1)):].strip():
            fence = None
    return out


def merge(memory: str, rule: dict) -> tuple[str, str]:
    """(new memory text, what happened: "present" | "changed" | "added")."""
    lines = memory.split("\n") if memory else []
    line = note(rule)
    live = _live_lines(lines)  # never a line inside a fenced code block
    if rule["id"]:
        for index in live:
            existing = lines[index]
            # only a note this sync wrote ("- RULE (" ... "[rule:key]") is ever replaced
            found = _SYNCED_ID.match(existing)
            if found and found.group(1) == rule["id"]:
                if norm(existing) == norm(line):
                    return memory, "present"
                lines[index] = line
                return "\n".join(lines), "changed"
    wanted, whole = norm(rule["text"]), norm(line)
    for index in live:
        existing = lines[index]
        if norm(existing) == whole:
            return memory, "present"
        if wanted and wanted == _text_of(existing):
            if not rule["id"]:
                return memory, "present"
            if _SYNCED_ID.match(existing):
                continue  # another id's note with the same words: this id still gets its own note
            # the same text written by hand: adopted as this rule's note (same words, now tracked by id),
            # so a later change of the rule replaces it instead of leaving the old text behind
            lines[index] = line
            return "\n".join(lines), "changed"
    heading = next((index for index in live if lines[index] == SECTION), None)
    if heading is None:  # (a copy of the heading inside a code block does not count)
        body = "\n".join(lines).rstrip("\n")
        return (body + ("\n\n" if body else "") + SECTION + "\n" + line + "\n"), "added"
    at = heading + 1
    while at < len(lines) and lines[at].startswith("- RULE ("):
        at += 1
    lines.insert(at, line)
    return "\n".join(lines), "added"


def _state(store: JobStore) -> dict:
    try:
        saved = json.loads(store.get_meta(META_KEY) or "{}")
    except ValueError:
        saved = {}
    return saved if isinstance(saved, dict) else {}


def _find_bot(store: JobStore, name: str | None, stem: str):
    """The bot a file names: its ``bot:`` line, else the one bot whose name slug is the file name (or the
    file name + "-bo"); two candidates is an error, never a guess."""
    if name:
        return store.find_bot(name)
    exact = [bot for bot in store.list_bots() if slug(bot.name) == slug(stem)]
    twins = [bot for bot in store.list_bots() if slug(bot.name) == slug(stem) + "-bo"]
    found = exact or twins
    if len(found) > 1 or (exact and twins):
        raise ValueError(f"the file name {stem!r} matches several bots; add a 'bot: <name>' line")
    return found[0] if found else None


def run(store: JobStore, memory, folder: Path | None, *, only_changed: bool = True, log_dir: Path | None = None) -> dict:
    """Import the drop folder. Returns {"files": [{file, bot, added, changed, present, error}], "at": iso}."""
    with _LOCK:
        return _run(store, memory, folder, only_changed=only_changed, log_dir=log_dir)


def _run(store: JobStore, memory, folder: Path | None, *, only_changed: bool, log_dir: Path | None) -> dict:
    result: dict = {"at": datetime.now(UTC).isoformat(), "files": []}
    if folder is None or not folder.is_dir():
        result["error"] = f"no drop folder: {folder}"
        return result
    state = _state(store)
    seen = state.get("files") if isinstance(state.get("files"), dict) else {}
    for path in sorted(folder.glob("*.md")):
        entry: dict = {"file": path.name}
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("not a regular file under 200 kB")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if only_changed and seen.get(path.name) == digest:
                continue
            name, rules = parse(raw.decode("utf-8", errors="replace"))
            if len(rules) > MAX_RULES_PER_FILE:
                raise ValueError(f"{len(rules)} rules; at most {MAX_RULES_PER_FILE} per file")
            bot = _find_bot(store, name, path.stem)
            if bot is None:
                raise ValueError(f"no bot named {name!r}" if name else f"no bot matches the file name {path.stem!r}")
            entry["bot"] = bot.name
            counts = {"added": 0, "changed": 0, "present": 0}
            for rule in rules:
                outcome = {}

                def change(text: str, rule=rule, outcome=outcome) -> str:
                    after, outcome["what"] = merge(text, rule)
                    return after

                memory.edit_bot(bot.id, change, WHO, rule["moment"])
                counts[outcome["what"]] += 1
            entry.update(counts)
            seen[path.name] = digest
        except Exception as exc:  # one bad file never stops the others; it is reported
            entry["error"] = str(exc)[:300]
        result["files"].append(entry)
    state.update({"files": seen, "last_run": result["at"], "last_day": datetime.now(ET).date().isoformat()})
    store.set_meta(META_KEY, json.dumps(state, sort_keys=True))
    if log_dir is not None:
        try:
            _append(log_dir / "rules-sync.log", json.dumps(result, ensure_ascii=False) + "\n")
        except Exception:
            log.exception("rules sync log could not be written")
    return result


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def busy() -> bool:
    return _LOCK.locked()


def due(store: JobStore, moment: datetime | None = None) -> bool:
    """Today's (ET) daily pass has not run yet."""
    today = (moment or datetime.now(UTC)).astimezone(ET).date().isoformat()
    return _state(store).get("last_day") != today
