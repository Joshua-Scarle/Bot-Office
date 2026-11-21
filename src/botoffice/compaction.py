"""Keep/drop policy for provider-native context compaction and the bot's WORKING.md."""
from __future__ import annotations

import os
import re

WORKING_FILE = "WORKING.md"
WORKING_LIMIT = 6000  # characters; hard cap enforced by Bot-Office after every turn
WORKING_TRUNCATED = "\n\n[Bot-Office truncated this file: keep WORKING.md under 6000 characters and prune finished items]\n"
OWNER_NOTES_FILE = "OWNER-NOTES.md"
OWNER_NOTES_LIMIT = 3000  # characters; newest statements win
OWNER_NOTE_LINE = 300  # characters kept of one statement
OWNER_NOTES_SCAN = 400  # newest chat messages looked at
RECENT_MESSAGES = 6  # injected into the prompt only when a fresh session starts


def compact_token_limit() -> int:
    """Token count at which CLIs auto-compact (cost control). Override with BOT_OFFICE_COMPACT_TOKENS."""
    try:
        return max(100_000, int(os.environ.get("BOT_OFFICE_COMPACT_TOKENS", "100000")))
    except ValueError:
        return 100_000


COMPACT_POLICY = """You are compacting the conversation of a persistent Bot-Office bot. The summary replaces the \
history, so keep only what the bot needs for its current work and its long-horizon work:
- The task list is kept by Bot-Office and re-sent every turn; keep the task ids you are working on.
- Active tasks and their exact current state (what is done, what is next, blockers).
- Upcoming or scheduled tasks and precisely how to do them when the time comes (commands, paths, \
people, conditions, safety rules).
- Key decisions with a one-line reason, standing rules and constraints from the user.
- Open questions still waiting on someone.
- The last user request and whether it has been answered.
Drop finished tasks and their details, directory listings, file contents, long command or test \
output, superseded plans, greetings and small talk. Do not write a lifetime history or a diary; \
a finished item only stays if a later task depends on it. Keep exact commands, ids, paths and \
error strings only where they are still needed. The bot also keeps WORKING.md (bounded working \
state) and OWNER-NOTES.md (the owner's own standing rules, decisions and open tasks, verbatim) in its job \
directory; stay consistent with them, and never drop an owner's standing rule, decision or open task."""


def cap_working(text: str) -> str:
    if len(text) <= WORKING_LIMIT:
        return text
    return text[: WORKING_LIMIT - len(WORKING_TRUNCATED)] + WORKING_TRUNCATED


_I = re.IGNORECASE
_DAYS = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|tonight|eod|end of (?:the )?(?:day|week|month)|next week"
# Statements an owner makes in plain chat that a bot must still honour after a compaction or a session reset.
# Matched on sentences (not questions), kept verbatim: no model is asked to remember them.
OWNER_PATTERNS = (
    ("rule", re.compile(r"\bfrom now on\b|\bgoing forward\b|\bstanding (?:rule|instruction)s?\b|\balways\b|\bnever\b"
                        r"|\bevery (?:reply|response|message|time)\b|\bdon'?t ever\b|\bdo not ever\b", _I)),
    ("decision", re.compile(r"\b(?:we|i)(?:'ve| have)? (?:decided|chose|agreed|picked|settled)\b|\bdecision\b"
                            r"|\blet'?s go with\b|\b(?:we|i)(?:'re| are) going with\b|\bwent with\b", _I)),
    ("task", re.compile(r"\bopen task\b|\bto-?do\b|\bnot (?:done|started) yet\b|\bstill (?:need|have) to\b"
                        r"|\bneeds? to be done\b|\bdon'?t forget\b|\bremind me\b|\bdeadline\b"
                        rf"|\bdue (?:on |by )?(?:{_DAYS})|\bby (?:{_DAYS})", _I)),
)
OWNER_PATTERNS += (("fact", re.compile(r"\bfyi\b|\bremember (?:that|this)\b|\bfor (?:later|the record)\b|\bnote that\b", _I)),)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def owner_statements(text: str) -> list[str]:
    """The sentences of one owner message that read as a rule, decision or open task: ``[kind] sentence``."""
    found = []
    for sentence in _SENTENCE.split(text):
        sentence = " ".join(sentence.split())
        if len(sentence) < 8 or sentence.endswith("?"):
            continue
        for kind, pattern in OWNER_PATTERNS:
            if pattern.search(sentence):
                clipped = sentence if len(sentence) <= OWNER_NOTE_LINE else sentence[:OWNER_NOTE_LINE] + " [...]"
                found.append(f"[{kind}] {clipped}")
                break
    return found


def owner_notes(texts: list[str]) -> str:
    """OWNER-NOTES.md: the owner's rules, decisions and open tasks, verbatim, oldest first, newest kept when
    over the cap. *texts* are the owner's own messages, oldest first. Generated every turn from the chat, so
    an edited or withdrawn message drops out by itself."""
    lines: list[str] = []
    for text in texts:
        for line in owner_statements(text):
            if line in lines:
                lines.remove(line)  # restated: keep the latest position
            lines.append(line)
    kept, size = [], 0
    for line in reversed(lines):
        if size + len(line) + 1 > OWNER_NOTES_LIMIT:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(reversed(kept))
