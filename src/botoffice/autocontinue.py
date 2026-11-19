"""Guards for the run-until-done loop: when a "continue" turn must not queue another one.

A bot that ends a turn with ``BOT-OFFICE-STATUS: continue`` gets its next step queued automatically.
That is wrong when the reply itself says the bot is blocked or needs the user, and it is wasteful when
turn after turn reports the same unresolved problem. Both guards are provider-agnostic: they read only
the reply text and the status reason. Structured signals (an open question card, a non-"continue"
status) are handled by the caller and win over these text heuristics.

The text lists are deliberately short and conservative; a hit pauses the assignment as
``waiting_on_user`` (it shows in Needs-you and a reply resumes it), so a false positive costs one reply.
"""
import re

# Consecutive "continue" turns with no user message in between, whatever they say.
MAX_CONSECUTIVE_CONTINUES = 10
# Consecutive "continue" turns that made no progress (see ``made_progress``).
NO_PROGRESS_LIMIT = 3

_NEEDS_INPUT = [
    r"\b(?:i am|i'm|is|are|remains?|remained|still|currently) blocked\b",
    r"\bblocked (?:on|by|until|waiting)\b",
    r"\bcan(?:not|'t) (?:verify|confirm|proceed|continue|tell|determine|check)\b",
    r"\b(?:unable|not able) to (?:verify|confirm|proceed|continue|determine)\b",
    (r"\bneeds? (?:you|your|a|an|the|some|more|another)?\s*(?:\w+\s){0,3}?"
     r"(?:photos?|pictures?|images?|screenshots?|measurements?|decision|confirmation|approval|input|permission|credentials?)\b"),
    r"\bneed you to\b",
    (r"\bplease (?:send|provide|upload|confirm|share|tell|let me know|reply|answer|approve|choose|attach|take|check|"
     r"advise|clarify|decide)\b"),
    r"\b(?:waiting|awaiting|wait) (?:for|on) (?:you|your)\b",
    r"\b(?:would|do|should) you (?:like|prefer|want|rather)\b",
    r"\bwhich (?:one )?(?:do|would|should) you\b",
]
_NEEDS_INPUT_RE = [re.compile(p, re.IGNORECASE) for p in _NEEDS_INPUT]

# Weak signals that the work is stuck on something unresolved: not a stop on their own, but a turn that
# carries one and brings nothing new is not progress.
_UNRESOLVED = re.compile(
    r"\b(?:unresolved|unverified|not (?:yet )?(?:ready|verified|passed|confirmed|resolved|safe)|"
    r"(?:has|have|did|does|do) not (?:pass|passed|fit|work)|fail(?:s|ed|ing|ure|ures)?|"
    r"(?:still|remains?|remained) (?:\w+\s){0,3}?(?:need\w*|require\w*|unresolved|unclear|missing|open|overlap\w*|"
    r"fail\w*|blocked)|nothing was (?:saved|printed|sent|changed))\b", re.IGNORECASE)
_STOP = frozenset("a an the and or of to in on is are was were be been it its this that these those with for as at by "
                  "from not no but if then so than too very can will have has had do does did into out up down over "
                  "under again still now just also you your".split())
REPEAT_SIMILARITY = 0.6


def _clean(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'")


# Text that is not the bot's own words about its own state: fenced and inline code (test logs, source),
# quoted lines, and short double-quoted strings. Blanked (same length, newlines kept) before the phrase
# checks, so "FAILED tests/..." in a log or "please send a photo" in an error message it just wrote is ignored.
_QUOTED = [re.compile(p, flags) for p, flags in (
    (r"```.*?```", re.DOTALL), (r"~~~.*?~~~", re.DOTALL), (r"```.*\Z", re.DOTALL), (r"~~~.*\Z", re.DOTALL),
    (r"`[^`\n]+`", 0), (r"^[ \t]*>.*$", re.MULTILINE), (r'"[^"\n]{1,300}"', 0), (r"“[^”\n]{1,300}”", 0))]


def prose(text: str) -> str:
    """*text* with code, quoted lines and quoted strings blanked out, so only the bot's own prose is left."""
    text = text or ""
    for pattern in _QUOTED:
        text = pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return text


# A sentence that says the problem is fixed ("Fixed the failed test", "The earlier failure is resolved")
# and does not hedge ("but", "still", "not yet") does not report anything unresolved.
_FIXED = re.compile(r"\b(?:fix(?:ed|es)?|resolv(?:ed|es)|repair(?:ed|s)?|no longer|now (?:pass(?:es)?|work(?:s|ing)?)|"
                    r"(?:all )?(?:tests? )?(?:pass(?:es|ed)?|green)|passing)\b", re.IGNORECASE)
_HEDGED = re.compile(r"\b(?:but|however|yet|still|remain(?:s|ed|ing)?|unresolved|not|never)\b|\bfail(?:s|ed|ing)? to\b|n't", re.IGNORECASE)


def _unresolved_in(text: str) -> bool:
    """True when *text* (prose only) has a sentence that reports something unresolved."""
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", _clean(text)):
        if _UNRESOLVED.search(sentence) and not (_FIXED.search(sentence) and not _HEDGED.search(sentence)):
            return True
    return False


def needs_user_phrase(reply: str, reason: str = "") -> str | None:
    """The phrase that says the bot is blocked or needs the user, else None (reply body and status reason).
    Code, quotes and quoted strings in the reply do not count: they are not the bot asking."""
    text = _clean(f"{prose(reply)}\n{reason}")
    for pattern in _NEEDS_INPUT_RE:
        if match := pattern.search(text):
            return match.group(0).strip()
    last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    if last.endswith("?") and re.search(r"\b(?:you|your)\b", last, re.IGNORECASE):
        return last[-80:]  # the reply ends by asking the user something
    return None


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _clean(text).lower()) if len(w) > 2 and w not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta or tb else 1.0


def made_progress(reply: str, earlier: list[str]) -> bool:
    """False when *reply* reports something still unresolved or repeats one of the *earlier* replies
    (same words after dropping case, punctuation and filler). Progress is a heuristic on the text: a
    turn that moved things forward says so without hedging, and a stuck one keeps saying "still ...".
    """
    if _unresolved_in(prose(reply)):
        return False
    return not any(similarity(reply, old) >= REPEAT_SIMILARITY for old in earlier)


def no_progress_run(replies: list[tuple[str, str]]) -> int:
    """How many of the newest *replies* in a row made no progress (``made_progress``)."""
    stalled = 0
    for i in range(len(replies) - 1, -1, -1):
        if made_progress(replies[i][0], [r for r, _ in replies[max(0, i - 3):i]]):
            break
        stalled += 1
    return stalled


def stop_reason(replies: list[tuple[str, str]]) -> str | None:
    """Why the loop should pause, from the run of "continue" turns since the user last spoke.

    *replies* is [(reply text, status reason)] oldest first, the turn that just ended last."""
    if not replies:
        return None
    reply, reason = replies[-1]
    if phrase := needs_user_phrase(reply, reason):
        return f"the reply says it is blocked or needs you (\"{phrase}\")"
    if len(replies) >= MAX_CONSECUTIVE_CONTINUES:
        return f"{len(replies)} continues in a row without a reply from you"
    stalled = no_progress_run(replies)
    if stalled >= NO_PROGRESS_LIMIT:
        return f"{stalled} continues in a row with no progress"
    return None
