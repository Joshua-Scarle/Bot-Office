"""Standing-rule tripwires: any git push, starting a print, and phone calls.

Three Joshua rules the approval classes did not cover on their own (classes `share` and `devices`
default off, and turning them on would also card PR comments or desk clicks):

- push: any git push (and its gh / connector equivalents) waits for the owner's approval. Pushes to
  main, force-pushes and deletes are already asked by git_guard; this adds every other branch.
  A future narrow owner `allow` rule can still open it (rules.py checks it after the allow rules).
- print: starting, resuming or stopping a print always waits for approval. Nothing opens it.
- phone: placing a phone call is refused outright ("never"). Nothing opens it.

Like the rest of the gate this is a tripwire on a tool's name and its command line, not a sandbox.
It reuses git_guard's shell reader so quoting, `cd`, `$( )` and `bash -c` do not hide a command.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import git_guard

PUSH_KEY = "push"
PRINT_KEY = "print"
PHONE_KEY = "phone"
LABELS = {
    PUSH_KEY: "Pushing code to GitHub or another remote",
    PRINT_KEY: "Starting, resuming or stopping a print",
    PHONE_KEY: "Phone calls (never allowed)",
}
MAX_DEPTH = 3
_WRITE_METHOD = re.compile(r"^(?:POST|PUT|PATCH|DELETE)$", re.IGNORECASE)


@dataclass(frozen=True)
class Trip:
    key: str
    why: str


# --- helpers ------------------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_READ = frozenset({"get", "list", "read", "search", "fetch", "query", "find", "view", "show", "download",
                   "describe", "lookup", "check", "count", "watch", "export", "preview", "status", "history"})


def _split_tool(tool: str) -> tuple[str, list[str]]:
    """(server, words of the action) for ``mcp__server__do_thing`` or ``mcp__doThing``."""
    if not tool.lower().startswith("mcp"):
        return "", []
    parts = tool.split("__")
    server = parts[1].lower() if len(parts) > 2 else ""
    action = parts[-1] if len(parts) > 1 else re.sub(r"^mcp[_\-.:/]*", "", tool, flags=re.IGNORECASE)
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub("_", action)) if w]
    return server, words


def _commands(command: str, depth: int = 0) -> list[list[str]]:
    """Every simple command in *command* as words, including ones inside $( ), backticks and the
    string argument of a shell or interpreter (``bash -c 'git push'``)."""
    try:
        commands, nested = git_guard.split_words(command)
    except Exception:  # noqa: BLE001
        return []
    for snippet in nested:
        commands += git_guard.split_words(snippet)[0]
    found = list(commands)
    if depth < MAX_DEPTH:
        for words in commands:
            bases = [git_guard._base(w) for w in words]
            if not any(b in git_guard.SHELLS or git_guard.INTERPRETERS.match(b) or b in git_guard.RUNNERS
                       or b in ("eval", "xargs", "env", "nohup", "time", "timeout") for b in bases):
                continue
            for word in words:
                if any(ch.isspace() for ch in word) and word != command:
                    found += _commands(word, depth + 1)
    return found


_WRAPPERS = frozenset({"sudo", "env", "nohup", "time", "timeout", "xargs", "exec", "command", "nice", "ionice",
                       "setsid", "stdbuf", "then", "do", "else", "if", "!"})


def _program(words: list[str]) -> tuple[str, list[str]]:
    """(program, its arguments) of a simple command: leading ``VAR=x``, wrappers (``sudo``, ``env``,
    ``timeout 5``...) and their options are skipped, so ``echo git push`` is echo, not git."""
    i = 0
    while i < len(words):
        word = words[i]
        base = git_guard._base(word)
        if ("=" in word and "/" not in word.split("=", 1)[0]) or base in _WRAPPERS or word.startswith("-") \
                or (i and git_guard._base(words[i - 1]) in ("timeout", "nice", "ionice") and word[:1].isdigit()):
            i += 1
            continue
        return base, words[i + 1:]
    return "", []


def _git_args(words: list[str]) -> list[str] | None:
    """Arguments after ``git`` (global options skipped), or None when this is not a git command."""
    program, args = _program(words)
    if program != "git":
        return None
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in git_guard._GIT_VALUE_OPTS else 1
    return args[i:]


# --- push -----------------------------------------------------------------------------------------

_GH_WRITES = re.compile(r"/git/(?:refs|commits|trees|blobs|tags)\b|/contents/|/merges\b|/merge-upstream\b|"
                        r"/branches/[^/\s]+/rename\b")
_GH_API_URL = re.compile(r"(?:api\.github\.com|/repos/[^/\s]+/[^/\s]+)")
_HTTP_BODY = re.compile(r"\s(?:-d|--data\S*|-F|--form|-f|--field|--raw-field|--input|-T|--upload-file)(?:\s|=|\S)")
_METHOD = re.compile(r"(?:^|\s)(?:-X|--request|--method)[=\s]*['\"]?([A-Za-z]+)")
_PUSH_URL_WORDS = re.compile(r"push", re.IGNORECASE)


def _push_command(command: str) -> str | None:
    for words in _commands(command):
        args = _git_args(words)
        if args and args[0] == "push":
            return "git push"
        program, rest = _program(words)
        if program == "gh":
            if rest[:2] == ["repo", "sync"]:
                return "gh repo sync (pushes to a remote)"
            if rest[:1] == ["api"]:
                text = " ".join(rest[1:])
                method = _METHOD.search(text)
                writes = (method and _WRITE_METHOD.match(method.group(1))) or (not method and _HTTP_BODY.search(" " + text))
                if writes and _GH_WRITES.search(text):
                    return "a gh api write to a repository's refs, commits or files"
    if re.search(r"\b(?:curl|wget|http|https|xh|xhs)\b", command) and _GH_API_URL.search(command) \
            and _GH_WRITES.search(command):
        method = _METHOD.search(command)
        if (method and _WRITE_METHOD.match(method.group(1))) or (not method and _HTTP_BODY.search(command)):
            return "an API write to a repository's refs, commits or files"
    return None


_GIT_SERVER = re.compile(r"git(?:hub|lab|ea)|bitbucket|codeberg|azure.?devops", re.IGNORECASE)
_WRITE_VERBS = frozenset({"create", "update", "put", "commit", "upload", "write", "add", "set", "apply", "sync"})
_REPO_OBJECTS = frozenset({"file", "files", "ref", "refs", "commit", "commits", "branch", "tag", "blob", "tree",
                           "content", "contents"})


def _push_tool(tool: str) -> str | None:
    server, words = _split_tool(tool)
    if not words or words[0] in _READ:
        return None
    if "push" in words:
        return "pushing through a connector"
    if (_GIT_SERVER.search(server) or "git" in words) and set(words) & _WRITE_VERBS and set(words) & _REPO_OBJECTS:
        return "writing commits, branches or files to a repository through a connector"
    return None


# --- print ----------------------------------------------------------------------------------------

_PRINTER_PROGRAMS = frozenset({"prusa-link", "prusalink", "octoprint", "octoprint-cli", "octocmd", "moonraker",
                               "moonraker-cli", "klipper", "printctl", "prusa-connect", "pc-cli"})
_CUPS_PROGRAMS = frozenset({"lp", "lpr", "lp.cups", "lpr.cups", "cupsdo"})
_PRINT_VERBS = frozenset({"print", "start", "resume", "stop", "cancel", "abort", "pause", "restart", "run",
                          "begin", "send", "upload", "queue", "job", "emergency_stop", "firmware_restart"})
_PRINT_UPLOAD_ON = re.compile(r"print[-_]after[-_]upload[\"']?\s*[:=]\s*[\"']?(?:1|true|\?1)|"
                              r"print[-_]after[-_]upload:\s*\?1|\"print\"\s*:\s*true", re.IGNORECASE)
_JOB_ENDPOINT = re.compile(r"/api/v1/job\b|/api/job\b|/printer/print/(?:start|resume|cancel|pause)\b|"
                           r"/printer/emergency_stop\b", re.IGNORECASE)
_PRINT_WORDS = frozenset({"print", "printing"})
_PRINT_DEVICE = frozenset({"printer", "job"})
_PRINT_ACTS = frozenset({"start", "resume", "stop", "cancel", "abort", "pause", "restart", "begin", "run", "send",
                         "queue", "launch"})


def _print_command(command: str) -> str | None:
    for words in _commands(command):
        base, args = _program(words)
        rest = [w.lower() for w in args]
        if base in _CUPS_PROGRAMS:
            return f"printing a document ({base})"
        if base in _PRINTER_PROGRAMS and (set(rest) & _PRINT_VERBS or any(
                w.startswith(("print", "start", "resume", "stop", "cancel")) for w in rest)):
            return f"a printer command ({base}) that starts, resumes or stops a print"
    if re.search(r"\b(?:curl|wget|http|https|xh|xhs|httpie)\b", command):
        method = _METHOD.search(command)
        write = bool(method and _WRITE_METHOD.match(method.group(1))) or bool(_HTTP_BODY.search(command))
        if _JOB_ENDPOINT.search(command) and write:
            return "an API call that starts, resumes or stops a print job"
        if re.search(r"/api/(?:v1/)?files/|/server/files/upload", command, re.IGNORECASE) and _PRINT_UPLOAD_ON.search(command):
            return "an upload that starts printing"
    return None


def _print_tool(tool: str) -> str | None:
    server, words = _split_tool(tool)
    if not words or words[0] in _READ:
        return None
    if set(words) & _PRINT_WORDS:
        return "a print tool"
    if set(words) & _PRINT_DEVICE and set(words) & _PRINT_ACTS and re.search(
            r"print|octo|prusa|klipper|moonraker|bambu|3d", server):
        return "a printer connector action"
    return None


# --- phone calls ----------------------------------------------------------------------------------

_PHONE_PROGRAMS = frozenset({"dial", "call-joshua", "calljoshua", "phone-call", "phonecall", "make-call",
                             "place-call", "call-phone", "linphonec", "pjsua", "baresip", "sipcmd", "twinkle",
                             "termux-telephony-call", "urgent-phone-escalation", "urgent_phone_escalation",
                             "phone-escalate", "escalate-phone", "callme"})
_TELEPHONY_VENDORS = (r"twilio|vonage|nexmo|telnyx|plivo|signalwire|vapi|retell(?:ai)?|bland(?:ai)?|aircall|"
                      r"bandwidth|dialpad|ringcentral|voip|sinch|messagebird|elevenlabs")
_PHONE_HTTP = re.compile(r"(?:" + _TELEPHONY_VENDORS + r")[^\s'\"]*[^\n]{0,200}?/(?:Calls?|calls?|call|dial|voice)\b|"
                         r"twiml|<Dial>|\bapi\.twilio\.com\S*/Calls\b|outbound[-_]call", re.IGNORECASE)
_PHONE_CLI_ARGS = re.compile(r"(?:^|[:/\-_ ])calls?(?:[:/\-_ ]|$)", re.IGNORECASE)
_PHONE_ACTION = re.compile(r"android\.intent\.action\.(?:CALL|CALL_PRIVILEGED|DIAL)\b|\btel:[+\d(]|"
                           r"\bcallto:|\bsip:", re.IGNORECASE)
_PHONE_VENDOR_WORDS = re.compile(r"\b(?:" + _TELEPHONY_VENDORS + r")\b", re.IGNORECASE)
_PHONE_SERVER = re.compile(_TELEPHONY_VENDORS + r"|phone|telephon|voice|dial|call|sms", re.IGNORECASE)
_CALL_WORDS = frozenset({"call", "calls", "dial", "ring", "phone", "phonecall", "telephony"})
_CALL_VERBS = frozenset({"make", "place", "start", "initiate", "create", "outbound", "originate", "trigger",
                         "escalate", "dial", "call", "ring"})


def _phone_command(command: str) -> str | None:
    for words in _commands(command):
        base, rest = _program(words)
        if base.lower() in _PHONE_PROGRAMS:
            return f"placing a phone call ({base})"
        if base.lower() == "twilio" and any(_PHONE_CLI_ARGS.search(a) for a in rest):
            return "placing a phone call (twilio)"
        if base in ("adb", "am") and _PHONE_ACTION.search(" ".join(rest)):
            return "placing a phone call from an Android device"
        if base in ("xdg-open", "open", "gio", "termux-open", "start") and any(_PHONE_ACTION.search(a) for a in rest):
            return "opening a phone-call link"
        if base == "asterisk" and any("originate" in a.lower() for a in rest):
            return "placing a phone call (asterisk originate)"
        text = " ".join(rest)
        method = _METHOD.search(text)
        if base in ("curl", "wget", "http", "https", "xh", "xhs") and _PHONE_HTTP.search(text) and (
                (method and _WRITE_METHOD.match(method.group(1))) or (not method and _HTTP_BODY.search(" " + text))):
            return "a request to a telephony service that places a call"
    return None


def _phone_tool(tool: str) -> str | None:
    server, words = _split_tool(tool)
    if not words or words[0] in _READ:
        return None
    word_set = set(words)
    if word_set & {"dial", "phonecall", "telephony"}:
        return "a phone-call tool"
    if "phone" in word_set and word_set & _CALL_VERBS:
        return "a phone-call tool"
    if word_set & _CALL_WORDS and (words[0] in _CALL_VERBS or _PHONE_SERVER.search(server)) \
            and not (word_set & {"tool", "tools", "function", "api", "method"}):
        return "a phone-call tool"
    return None


# --- entry point ----------------------------------------------------------------------------------

def phone(tool: str, command: str = "") -> Trip | None:
    why = (_phone_command(command) if command else None) or _phone_tool(tool or "")
    return Trip(PHONE_KEY, why) if why else None


def printing(tool: str, command: str = "") -> Trip | None:
    why = (_print_command(command) if command else None) or _print_tool(tool or "")
    return Trip(PRINT_KEY, why) if why else None


def pushing(tool: str, command: str = "") -> Trip | None:
    if tool == git_guard.PUSH_TOOL:  # the pre-push check of a push already asked (or allowed) as a command
        return None
    why = (_push_command(command) if command else None) or _push_tool(tool or "")
    return Trip(PUSH_KEY, why) if why else None


def judge(tool: str, command: str = "") -> Trip | None:
    """The first tripwire this call trips: phone, then print, then push."""
    return phone(tool, command) or printing(tool, command) or pushing(tool, command)


# --- secrets in what a card, a log line or a refusal shows ------------------------------------------

REDACTED = "[redacted]"
_USERINFO = re.compile(r"(://[^/\s:@'\"]+:)[^@\s/'\"]+(@)")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s\"',;]{6,}")
_SECRET_VAR = re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|credential|auth)[A-Za-z0-9_.-]*"
                         r"\s*[:=]\s*[\"']?)[^\s\"',;&|]{6,}")
_PREFIXED = re.compile(r"\b(?:sk|pk|rk|xai|ghp|gho|ghs|ghu|ghr|github_pat|glpat|hf|xox[abp])[-_][A-Za-z0-9_-]{8,}|"
                       r"\bAKIA[0-9A-Z]{16}\b")


def redact_secrets(text: str) -> str:
    """*text* with credentials replaced by ``[redacted]``: userinfo in a URL, bearer tokens,
    ``TOKEN=value`` style assignments and sk-/ghp_-style keys. For what people and logs read (an
    approval card, a gate log line, the refusal a bot sees); never applied to the fingerprint, so an
    approval still binds to the exact command. Git SHAs and other long hex are left alone."""
    text = _USERINFO.sub(lambda m: m.group(1) + REDACTED + m.group(2), text or "")
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _SECRET_VAR.sub(lambda m: m.group(1) + REDACTED, text)
    return _PREFIXED.sub(REDACTED, text)
