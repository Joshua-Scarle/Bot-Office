"""Provider failure classification, reset-time parsing and the retry policy (pure functions).

A failed turn is classified into one *kind*; the kind decides the chat line and whether
the scheduler retries the same job row later (see ``retry_at``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USAGE_LIMIT = "usage_limit"
AUTH = "auth"
TIMEOUT = "timeout"
PROVIDER_ERROR = "provider_error"
TOOL_DENIED = "tool_denied"
INTERRUPTED = "interrupted"
LAUNCH = "launch"
INCOMPLETE = "incomplete"
# 0.11.0: an unattended run refused before launch (no proven action gate, or over the daily budget)
GATE = "gate"
BUDGET = "budget"
KINDS = (USAGE_LIMIT, AUTH, TIMEOUT, PROVIDER_ERROR, TOOL_DENIED, INTERRUPTED, LAUNCH, GATE, BUDGET)

DISPLAY_TZ = ZoneInfo("America/New_York")
ERROR_TEXT_LIMIT = 300

USAGE_TERMS = (
    "usage limit", "hit your limit", "hit your 5-hour limit", "5-hour limit", "weekly limit",
    "rate limit", "rate_limit", "too many requests", "quota", "out of credits", "limit reached",
    "limit exceeded", "resource_exhausted", "insufficient credits",
)
AUTH_TERMS = (
    "not logged in", "signed out", "unauthorized", "not authenticated", "authentication",
    "invalid api key", "invalid_api_key", "api key", "please log in", "please login", "login required",
    "oauth token", "credentials",
)
DENIED_TERMS = ("permission denied by", "tool use was denied", "denied by policy", "not permitted to use")
TRANSIENT_TERMS = (
    "overloaded", "network", "connection", "econnreset", "etimedout", "timed out", "temporarily",
    "503", "502", "500", "504", "529", "internal server error", "bad gateway", "service unavailable",
    "socket", "fetch failed",
)

# Retry policy: kind -> delays before each automatic retry (len = max retries).
PROVIDER_DELAYS = (timedelta(minutes=2), timedelta(minutes=10))
TIMEOUT_DELAYS = (timedelta(seconds=0),)
USAGE_MAX_RETRIES = 2
USAGE_FALLBACK = timedelta(minutes=30)
USAGE_GRACE = timedelta(minutes=2)
USAGE_MAX_WAIT = timedelta(days=7)
NEVER_RETRIED = (AUTH, "needs_login", TOOL_DENIED, LAUNCH, INCOMPLETE, "cancelled", INTERRUPTED, GATE, BUDGET)


STATUS_LINE = re.compile(r"\s*BOT-OFFICE-STATUS:\s*(done|not_done|continue|blocked|waiting_on_user|waiting_on_bot)\b\s*(?:[-\u2013\u2014:]+\s*)?(.*)",
                         re.IGNORECASE)
NOT_DONE_LABELS = {"not_done": "Not done", "blocked": "Not done (blocked)", "waiting_on_user": "Not done (waiting on you)",
                   # 0.10.0 (assignments): another step follows / another bot has to answer first
                   "continue": "Not done (continuing)", "waiting_on_bot": "Not done (waiting on another bot)"}


def split_status(reply: str) -> tuple[str, str, str]:
    """Strip a final ``BOT-OFFICE-STATUS: <state> — <reason>`` line from *reply*.

    Returns (reply without the line, state, reason); state is "unverified" without a line."""
    lines = reply.rstrip().split("\n")
    match = STATUS_LINE.fullmatch(lines[-1].strip().strip("*`")) if lines else None
    if not match:
        return reply, "unverified", ""
    return "\n".join(lines[:-1]).rstrip(), match.group(1).lower(), match.group(2).strip().strip("*`").strip()


# 0.12.16: a "done" counts as verified only when its status line says what was checked and how.
# 0.12.20: and Bot-Office itself must find what it cites (evidence.check: a file in the bot's workspace, a
# commit, a link that answers, or test output in this job's own command output), and no cited check failed.
# 0.12.20: the whole reason, so "nothing new" claims no work; "nothing new, fixed the build" is checked like any other
NO_OP_DONE = re.compile(r"(?:nothing|no)\s+(?:new|to report|changed|to do|found)[.!]?", re.IGNORECASE)
# 0.12.20: a short final note that the reply was cut by the engine's output limit (the draft is the real reply)
LIMIT_NOTE = re.compile(
    r"\b(?:output|token|length|response|reply|message|character)s?[\s-]+(?:token[\s-]+)?(?:limit|maximum|cap)\b"
    r"|\bmax(?:imum)?[_\s-]?(?:output[_\s-])?tokens?\b|\b(?:was|got|been|is)\s+(?:cut\s+off|truncated)\b", re.IGNORECASE)
# a check that did not succeed: "3 failed", "tests failed", a traceback, a non-zero exit, an HTTP 4xx/5xx ...
_NEGATED = re.compile(r"\b(?:0|no|zero|without|not\s+any)\s+(?:test\s+|new\s+)?(?:failures?|failed|failing|fails?|errors?)\b", re.IGNORECASE)
FAILED_COUNT = re.compile(r"\b[1-9]\d*\s+(?:tests?\s+|checks?\s+)?(?:failed|failing|failures?|errors?)\b", re.IGNORECASE)
FAILED_WORD = re.compile(
    r"\b(?:fail(?:ed|s|ing|ure|ures)?|errors?|errored|exceptions?|traceback|timed\s+out|unsuccessful|did\s+not\s+pass|didn['\u2019]t\s+pass|"
    r"not\s+passing|exit(?:ed)?(?:\s+(?:code|status))?\s*[1-9]\d*|(?:http|status|returned)\s+[45]\d\d)\b", re.IGNORECASE)
REPAIRED = re.compile(r"\b(?:re-?ran|rerun|re-?tested|fixed|now|again|after\s+(?:the\s+)?fix)\b[^;\n]*?\b(?:passed|passes|passing|green|succeeded|"
                      r"success(?:ful(?:ly)?)?|exit(?:ed)?\s+0|returned\s+(?:200|ok))\b", re.IGNORECASE)


def failed_check(text: str) -> bool:
    """Does *text* cite a check that did not succeed? "0 failed" and "no errors" do not count. A failure
    stays a failure unless a rerun or fix after it passed ("the failing test now passes", "fixed it and
    reran pytest, all passed"); an unrelated pass ("tests failed; lint passed") does not repair it."""
    text = _NEGATED.sub(" ", text)
    last = None
    for last in [*FAILED_COUNT.finditer(text), *FAILED_WORD.finditer(text)]:
        pass
    if last is None:
        return False
    start = min(m.start() for m in [*FAILED_COUNT.finditer(text), *FAILED_WORD.finditer(text)])
    return not REPAIRED.search(text, start)


def judge_evidence(state: str, reason: str, checked: str | None = None) -> tuple[int | None, str]:
    """(verified, evidence) for a finished turn that ended with *state*. verified is None for states
    that claim nothing (not_done, blocked, no status line: that one is "unverified" already, and a done
    whose whole reason is "nothing new": it claims no work, so there is nothing to verify and nothing to
    flag); 1 for a done whose reason cites something Bot-Office checked (*checked*: what evidence.check
    found); 0 for a bare done, a reason whose files, commits, links or test results could not
    be checked ("red lunch widget"), or a cited check that failed. evidence is the text shown with it: the
    cited text (kept when it is not verified, so the owner can see what was cited), plus what was checked."""
    if state != "done":
        return None, ""
    reason = reason.strip()
    if failed_check(reason):
        return 0, reason
    if NO_OP_DONE.fullmatch(reason):
        return None, ""  # claims no work: nothing to verify, so neither verified nor "Not verified"
    if not checked:
        return 0, reason
    return 1, f"{reason} (Bot-Office checked: {checked})"


# 0.12.20: a reply that says the work could not be done is not a done. Checked on the status reason and the
# opening and closing paragraphs only, so a long report that mentions a failed attempt in the middle stays done.
_CANT = (r"(?:could\s?n['\u2019]?t|could\s+not|can['\u2019]?t|cannot|can\s+not|unable\s+to|(?:was|am|is)\s*(?:n['\u2019]?t|not)\s+able\s+to|"
         r"not\s+able\s+to|failed\s+to|did\s*(?:n['\u2019]?t|not)\s+manage\s+to|never\s+(?:got|managed))")
INABILITY = re.compile(
    rf"\b{_CANT}\s+(?:\w+\s+){{0,3}}?(?:tell|answer|do|complete|finish|get|reach|access|see|check|verify|find|produce|"
    r"determine|run|send|open|read|load|connect|fetch|retrieve|proceed|continue|build|deploy|write|create|"
    r"obtain|poll|sweep|list|set|make|log|authenticate|log\s+in|install|start)\b"
    r"|\bno\s+(?:live\s+|usable\s+|real\s+|current\s+)?(?:data|state|access|view)\b(?!\s+(?:loss|issues?|problems?|changes?|errors?|leaks?|(?:was|were|is|has|had|got)\b))"
    r"|\b(?:not\s+complete[d]?|incomplete|blocked\s+(?:by|on)|was\s+blocked|is\s+blocked)\b"
    r"|\bwork\s+(?:is\s+)?(?:not|n['\u2019]t)\s+(?:done|finished|complete)", re.IGNORECASE)
# "couldn't find any problem" is a result, not an inability
BENIGN = re.compile(
    rf"\b{_CANT}\s+(?:find|see|reproduce|detect|spot|trigger)\s+(?:any|a|an|the|one)?\s*(?:\w+\s+)?"
    r"(?:problems?|issues?|bugs?|errors?|regressions?|leaks?|differences?|mismatch(?:es)?|failures?|duplicates?|conflicts?)\b",
    re.IGNORECASE)
RESOLVED = re.compile(r"\b(?:instead|fell\s+back|worked\s+around|workaround|so\s+I\s+(?:used|read|checked|took|went|ran)|"
                      r"but\s+(?:I\s+)?(?:was\s+able|managed)|and\s+(?:then\s+)?completed)\b", re.IGNORECASE)
WAITING_ON_YOU = re.compile(
    r"\b(?:paste|send\s+me|share|let\s+me\s+know|tell\s+me\s+(?:which|what|where)|need\s+(?:you|your)|your\s+(?:decision|answer|input|go-ahead)|"
    r"if\s+you\s+(?:give|send|share|paste|can)|waiting\s+for\s+you)\b", re.IGNORECASE)
_PARAGRAPHS = re.compile(r"\n\s*\n")
ADMISSION_NOTE = "marked done, but the reply says the work could not be completed"


def admitted_incomplete(reply: str, reason: str) -> str | None:
    """The sentence that says the work could not be completed, from the status *reason* or the first or
    last paragraph of *reply*; None when nothing does."""
    paragraphs = [p.strip() for p in _PARAGRAPHS.split(reply.strip()) if p.strip()]
    pieces = [reason] + ([paragraphs[0], paragraphs[-1]] if paragraphs else [])
    for piece in pieces:
        for sentence in re.split(r"(?<=[.!?])\s+|\n", piece):
            if INABILITY.search(sentence) and not BENIGN.search(sentence) and not RESOLVED.search(sentence):
                return sentence.strip()
    return None


def judge_done(reply: str, state: str, reason: str) -> tuple[str, str]:
    """(state, reason) after a done that admits it could not finish is recorded as what it was: waiting_on_user
    when the reply asks the owner for something, else blocked. Other states and a plain done are unchanged."""
    if state != "done":
        return state, reason
    said = admitted_incomplete(reply, reason)
    if said is None:
        return state, reason
    kind = "waiting_on_user" if WAITING_ON_YOU.search(reply[-600:] + " " + reason) else "blocked"
    return kind, f"{ADMISSION_NOTE}: {clip(said, 160)}"


REDACTED = "[redacted]"
_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s\"',;]{6,}")
_KEY_VALUE = re.compile(r"(?i)\b((?:x-)?api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|"
                        r"authorization|client[_-]?secret)(\s*[:=]\s*[\"']?)([^\s\"',;]{6,})")
_PREFIXED = re.compile(r"\b(?:sk|pk|rk|xai|ghp|gho|ghs|github_pat|glpat|hf)[-_][A-Za-z0-9_-]{8,}|\bAKIA[0-9A-Z]{16}\b")
_LONG = re.compile(r"[A-Za-z0-9+/=_.-]{32,}")


def _long_token(match: re.Match[str]) -> str:
    word = match.group(0)
    if all(len(part) < 32 for part in word.split("/")):
        return word  # a path, not a token
    if any(c.isdigit() for c in word) and any(c.isalpha() for c in word):
        return REDACTED
    return word


def redact(text: str | None) -> str:
    """*text* with obvious secrets (bearer tokens, key=value credentials, sk-/ghp_-style keys,
    long hex or base64 runs) replaced by ``[redacted]``. Failure text reaches chat and the DB."""
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text or "")
    text = _KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _PREFIXED.sub(REDACTED, text)
    return _LONG.sub(_long_token, text)


def clip(text: str | None, limit: int = ERROR_TEXT_LIMIT) -> str:
    """One line of at most *limit* characters with secrets redacted."""
    text = redact(" ".join((text or "").split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def classify(text: str | None, exit_code: int | None, api_status: int | None = None) -> str:
    """The failure kind of a run that did not succeed."""
    lowered = (text or "").lower()
    if api_status == 429 or any(term in lowered for term in USAGE_TERMS):
        return USAGE_LIMIT
    if api_status in (401, 403) or any(term in lowered for term in AUTH_TERMS):
        return AUTH
    if exit_code == 124 or "job timed out" in lowered:
        return TIMEOUT
    if any(term in lowered for term in DENIED_TERMS):
        return TOOL_DENIED
    if exit_code is None and ("launch failed" in lowered or "executable not found" in lowered):
        return LAUNCH
    return PROVIDER_ERROR


def is_transient(text: str | None, exit_code: int | None, api_status: int | None = None) -> bool:
    """A provider error worth repeating as-is (network, 5xx, overloaded, killed by a signal)."""
    if exit_code is not None and exit_code < 0:
        return True
    if api_status is not None and api_status >= 500:
        return True
    lowered = (text or "").lower()
    return any(term in lowered for term in TRANSIENT_TERMS)


_ZONE = r"(?:\s*\(([A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*)\))?"
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
_MONTHS = {name[:3]: number for number, name in enumerate(
    ("january february march april may june july august september october november december").split(), 1)}
_DATE = r"(?:(\d{4})-(\d{2})-(\d{2})|([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?)"
RE_IN = re.compile(r"(?:try again|retry|resets?)\s+in\s+(\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr|day)s?", re.I)
RE_AFTER = re.compile(r"retry[-_ ]after[\"']?\s*[:=]\s*(\d+)", re.I)
RE_DATE_AT = re.compile(r"(?:resets?|try again|available)\s+(?:on\s+|at\s+)?" + _DATE + r"(?:,|\s+at)?\s+" + _TIME + _ZONE, re.I)
RE_AT = re.compile(r"(?:resets?|try again)\s+(?:at\s+)?" + _TIME + _ZONE, re.I)


def _zone(name: str | None) -> ZoneInfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return DISPLAY_TZ


def _hour(hour: str, minute: str | None, meridiem: str | None) -> tuple[int, int] | None:
    h, m = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if meridiem.lower().startswith("p") else 0)
    elif minute is None:
        return None  # a bare number is not a time
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def parse_reset(text: str | None, now: datetime) -> datetime | None:
    """When a usage limit lifts, from the provider's own message; None if not stated.

    Times are read in the zone the message names, else America/New_York; a time of day
    already past rolls over to the next day.
    """
    if not text:
        return None
    if match := RE_AFTER.search(text):
        return now + timedelta(seconds=int(match.group(1)))
    if match := RE_IN.search(text):
        amount, unit = float(match.group(1)), match.group(2).lower()
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit[0]]
        return now + timedelta(seconds=amount * seconds)
    if match := RE_DATE_AT.search(text):
        y, mo, d, month_name, day, year2, hour, minute, meridiem, zone_name = match.groups()
        clock = _hour(hour, minute, meridiem)
        zone = _zone(zone_name)
        local_now = now.astimezone(zone)
        if clock:
            try:
                if y:
                    date = datetime(int(y), int(mo), int(d), *clock, tzinfo=zone)
                else:
                    month = _MONTHS.get(month_name[:3].lower())
                    if month is None:
                        raise ValueError
                    date = datetime(int(year2 or local_now.year), month, int(day), *clock, tzinfo=zone)
                    if not year2 and date < local_now - timedelta(days=1):
                        date = date.replace(year=date.year + 1)
                return date
            except ValueError:
                pass
    if match := RE_AT.search(text):
        hour, minute, meridiem, zone_name = match.groups()
        clock = _hour(hour, minute, meridiem)
        if clock:
            zone = _zone(zone_name)
            local_now = now.astimezone(zone)
            moment = local_now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if moment <= local_now:
                moment += timedelta(days=1)
            return moment
    return None


def show_time(moment: datetime, now: datetime | None = None) -> str:
    """``6:10 PM ET`` (with the weekday/date when it is not today)."""
    local = moment.astimezone(DISPLAY_TZ)
    text = local.strftime("%I:%M %p").lstrip("0") + " ET"
    if now is not None and local.date() != now.astimezone(DISPLAY_TZ).date():
        text = local.strftime("%a %b ") + str(local.day) + ", " + text
    return text


def retry_at(kind: str, retries_done: int, reset: datetime | None, now: datetime) -> datetime | None:
    """When to retry automatically after a failure of *kind*; None means do not retry."""
    if kind == USAGE_LIMIT:
        if retries_done >= USAGE_MAX_RETRIES:
            return None
        if reset is None:
            return now + USAGE_FALLBACK
        if reset - now > USAGE_MAX_WAIT:
            return None
        return max(reset, now) + USAGE_GRACE
    if kind == PROVIDER_ERROR and retries_done < len(PROVIDER_DELAYS):
        return now + PROVIDER_DELAYS[retries_done]
    if kind == TIMEOUT and retries_done < len(TIMEOUT_DELAYS):
        return now + TIMEOUT_DELAYS[retries_done]
    return None


KIND_LABELS = {
    USAGE_LIMIT: "usage limit",
    AUTH: "sign-in problem",
    TIMEOUT: "timed out",
    PROVIDER_ERROR: "provider error",
    TOOL_DENIED: "tool denied",
    INTERRUPTED: "interrupted",
    LAUNCH: "could not start",
    GATE: "action gate not proven",
    BUDGET: "daily budget used",
}


def failure_line(engine: str, kind: str | None, error: str | None, reset: datetime | None,
                 next_try: datetime | None, now: datetime) -> str:
    """The one system line a failed attempt posts in the bot's chat."""
    if kind == USAGE_LIMIT:
        line = f"{engine} usage limit reached"
        if reset is not None:
            line += f"; resets {show_time(reset, now)}"
        line += "."
    else:
        detail = clip(error) or "no details reported"
        line = f"Job failed ({KIND_LABELS.get(kind or '', kind or 'error')}): {detail}"
    if next_try is not None:
        line += (" Will retry then." if kind == USAGE_LIMIT and reset is not None
                 else f" Will retry at {show_time(next_try, now)}.")
    if kind == USAGE_LIMIT and (said := clip(error)):
        line += f" {engine} said: \u201c{said}\u201d"  # the provider's own words, kept
    return line
