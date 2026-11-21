"""Keep/drop policy for provider-native context compaction and the bot's WORKING.md."""
from __future__ import annotations

import os

WORKING_FILE = "WORKING.md"
WORKING_LIMIT = 6000  # characters; hard cap enforced by Bot-Office after every turn
WORKING_TRUNCATED = "\n\n[Bot-Office truncated this file: keep WORKING.md under 6000 characters and prune finished items]\n"
RECENT_MESSAGES = 6  # injected into the prompt only when a fresh session starts
# The bot's durable state travels inline (not as a request to read a file): memory.md, the open tasks and WORKING.md.
STATE_FILE = "DURABLE-STATE.md"  # written into every job directory; the Claude compact hook prints it
MEMORY_PROMPT_LIMIT = 6000  # characters of memory.md inlined in a fresh session (about 1.5k tokens, once per session)
MEMORY_DIGEST_LIMIT = 1500  # the same for a resumed turn of an engine without a compaction hook (about 400 tokens/turn)


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
error strings only where they are still needed. The bot also keeps memory.md (durable facts, the owner's \
standing rules and decisions) and WORKING.md in its job directory, and Bot-Office re-adds both and the task list \
after this compaction; stay consistent with them, and never drop an owner's standing rule, decision or open task."""


def cap_working(text: str) -> str:
    if len(text) <= WORKING_LIMIT:
        return text
    return text[: WORKING_LIMIT - len(WORKING_TRUNCATED)] + WORKING_TRUNCATED


def cap_memory(text: str, limit: int = MEMORY_PROMPT_LIMIT) -> str:
    """memory.md for a prompt: whole when it fits, else its head (where the standing rules usually are) and its
    tail (the newest notes) around a marker that says how much was left out and where the rest is."""
    text = text.strip()
    if len(text) <= limit:
        return text
    tail = limit // 3
    head = limit - tail
    gap = len(text) - head - tail
    return (text[:head].rstrip() + f"\n[... {gap} characters of memory.md left out here: read memory.md for them ...]\n"
            + text[-tail:].lstrip())


def memory_block(memory: str, limit: int = MEMORY_PROMPT_LIMIT) -> str:
    """The inline memory.md section of a fresh-session prompt or digest (empty when the file is empty)."""
    if not memory.strip():
        return ""
    return ("\nYour memory.md (durable facts, the owner's standing rules and decisions; inlined by Bot-Office, "
            f"the file is in the current directory):\n{cap_memory(memory, limit)}\n")


def state_file(memory: str, tasks_block: str, working: str) -> str:
    """DURABLE-STATE.md: everything that must outlive a context compaction, for the Claude SessionStart(compact)
    hook to print: memory.md (capped), the open tasks and WORKING.md."""
    return ("Bot-Office re-adds your durable state after a context compaction. Anything the owner stated earlier "
            "(a decision, a standing rule, an open task) is in memory.md or the task list below; both win over "
            "your summary.\n" + (memory_block(memory) or "\nYour memory.md is empty.\n") + (tasks_block or "")
            + f"\nYour WORKING.md:\n{working.strip() or '(empty)'}\n")
