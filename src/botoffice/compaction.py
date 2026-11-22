"""Keep/drop policy for provider-native context compaction and the bot's WORKING.md."""
from __future__ import annotations

import os

WORKING_FILE = "WORKING.md"
WORKING_LIMIT = 6000  # characters; hard cap enforced by Bot-Office after every turn
WORKING_TRUNCATED = "\n\n[Bot-Office truncated this file: keep WORKING.md under 6000 characters and prune finished items]\n"
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
- Date every state you keep ("as of <date time ET>"): the summary is a snapshot that will age.
Drop finished tasks and their details, directory listings, file contents, long command or test \
output, superseded plans, greetings and small talk. Do not write a lifetime history or a diary; \
a finished item only stays if a later task depends on it. Keep exact commands, ids, paths and \
error strings only where they are still needed. The bot also keeps WORKING.md (bounded working \
state) in its job directory; stay consistent with it."""


def cap_working(text: str) -> str:
    if len(text) <= WORKING_LIMIT:
        return text
    return text[: WORKING_LIMIT - len(WORKING_TRUNCATED)] + WORKING_TRUNCATED
