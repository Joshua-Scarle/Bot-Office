"""Automatic sidebar groups (0.12.20): every new bot lands in a section, retiring a bot moves it to "Retired".

One helper for every creation path (UI and API ``POST /v1/bots``, bot-created ``POST /v1/agent/bots``, and any
router or handoff intake that calls :func:`place_new_bot`). Inference order:

1. an explicit group (a section name or id; an unknown name creates that section)
2. the creator bot's own section (a real one: not Hidden, Retired or Unsorted)
3. the name prefix: the bot name's first word (or first two) against the existing section names and ``ALIASES``,
   case-insensitively, so "MS ML Engineer" goes to "MS"
4. "Unsorted" (created if missing)

Placement never fails a creation: a section error is swallowed and the bot stays in Ungrouped.
"""
from __future__ import annotations

import re

from . import sections as sections_mod

UNSORTED = "Unsorted"
RETIRED = "Retired"

# lower-case first word of a bot name -> canonical group name
ALIASES = {
    "ms": "MS", "mpg": "MPG", "web": "Web", "bt": "BT", "devops": "DevOps",
    "bot-office": "Bot-Office", "botoffice": "Bot-Office", "bo": "Bot-Office",
    "engineering": "Engineering", "eng": "Engineering",
}


def _key(name: str) -> str:
    return sections_mod.reserved_key(name)


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[\s_]+", name.strip()) if w]


def prefix_group(name: str, existing: list[str]) -> str | None:
    """The group name this bot name's prefix points at, or None. Existing section names win over aliases."""
    words = _words(name)
    if not words:
        return None
    by_key = {_key(n): n for n in existing}
    two = _key(" ".join(words[:2])) if len(words) > 1 else None
    if two and two in by_key:
        return by_key[two]
    first = _key(words[0].strip("()[]{}:,."))
    if first in by_key and first not in (_key(UNSORTED), _key(RETIRED)):
        return by_key[first]
    alias = ALIASES.get(first)
    if alias:
        return by_key.get(_key(alias), alias)
    return None


def _find(store, name: str) -> str | None:
    key = _key(name)
    for section in store.sections.layout()["sections"]:
        if _key(section["name"]) == key:
            return section["id"]
    return None


def ensure_section(store, name: str) -> str:
    """The id of the section called *name* (case-insensitive), created at the end if missing."""
    found = _find(store, name)
    if found:
        return found
    try:
        return store.sections.create(name)["id"]
    except sections_mod.SectionError:
        found = _find(store, name)   # lost a creation race, or the section list is full
        if found:
            return found
        raise


def _creator_section(store, creator_bot_id) -> str | None:
    if not creator_bot_id:
        return None
    layout = store.sections.layout()
    skip = {_key(UNSORTED), _key(RETIRED)}
    for section in layout["sections"]:
        if str(creator_bot_id) in section["bot_ids"] and _key(section["name"]) not in skip:
            return section["id"]
    return None


def infer_section(store, name: str, group: str | None = None, creator_bot_id=None) -> str | None:
    """The section id for a new bot (``None``: leave it ungrouped, only for an explicit "Ungrouped")."""
    if group is not None and group.strip():
        key = _key(group)
        if key == sections_mod.UNGROUPED_ALIAS:
            return None
        if key == sections_mod.HIDDEN:
            return sections_mod.HIDDEN
        layout = store.sections.layout()
        if any(s["id"] == group.strip() for s in layout["sections"]):
            return group.strip()
        return ensure_section(store, sections_mod.clean_name(group))
    inherited = _creator_section(store, creator_bot_id)
    if inherited:
        return inherited
    existing = [s["name"] for s in store.sections.layout()["sections"]]
    named = prefix_group(name, existing)
    return ensure_section(store, named or UNSORTED)


def place_new_bot(store, bot, group: str | None = None, creator_bot_id=None) -> str | None:
    """Put a freshly created *bot* in its group; returns the section id, or None if it stayed ungrouped."""
    try:
        section_id = infer_section(store, bot.name, group, creator_bot_id)
        if section_id is None:
            return None
        store.sections.move_bot(str(bot.id), section_id, 10**6)
        return section_id
    except sections_mod.SectionError:
        return None


def retire_bot(store, bot_id) -> str:
    """Move the bot to the "Retired" section (created if missing); returns its id."""
    section_id = ensure_section(store, RETIRED)
    store.sections.move_bot(str(bot_id), section_id, 10**6)
    return section_id
