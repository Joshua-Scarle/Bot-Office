"""0.11.10 approval classes: kinds of action the owner wants done, but not done on the bot's own.

Bots rarely ask. A bot just goes ahead; when it tries an action in a class the owner switched on,
the CLI's gate hook (bin/bot-office-gate) stops that one tool call, Bot-Office records an
approval, and the chat shows an Approve / Deny card. Approve runs exactly that action once in a
resume turn; Deny, or no answer within the timeout, leaves the task stopped with a short note.

Classes apply in every run, attended (your chat) and unattended (routines, watchers...). The
strict unattended profile in rules.py still asks for every outbound and destructive action on
top of these. Matching is on the tool's name and its shell command: a tripwire, not a sandbox.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from .store import JobStore

KEY = "approval_classes"
CUSTOM_LIMIT = 30
LABEL_LIMIT = 80
PATTERN_LIMIT = 500
TIMEOUT_DEFAULT = 60
TIMEOUT_MAX = 7 * 24 * 60
CUSTOM_KINDS = ("shell", "tool")

_SEGMENT = re.compile(r"[;&|\n]")
_SEP = r"(?:^|[;&|(`]|\$\(|\b(?:then|do|else|xargs|exec|env|nohup|time|sudo)\s)\s*"
_GIT_OPTS = r"(?:-[^\s;&|]+\s+(?:[^\s;&|-][^\s;&|]*\s+)?)*"
_GIT_PUSH = re.compile(r"\bgit\s+" + _GIT_OPTS + r"push\b")
_FORCE_OR_MAIN = re.compile(r"\s(?:-f|--force(?:-with-lease)?(?:=\S*)?)(?=\s|$)|[\s:+](?:main|master)(?=\s|$)")
# a tool name's words: "mcp__gmail__send_message" -> gmail, send, message
_WORD = r"(?:^|[_\-.:/])"
_END = r"(?=[_\-.:/]|$)"


def _tool_re(*verbs: str) -> re.Pattern:
    return re.compile(_WORD + "(?:" + "|".join(verbs) + ")" + _END, re.IGNORECASE)


def _shell(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(pattern) for pattern in patterns)


def _is_mcp(tool: str) -> bool:
    return tool.lower().startswith("mcp")


def _git_push_risky(command: str) -> bool:
    for segment in _SEGMENT.split(command):
        found = _GIT_PUSH.search(segment)
        if found and _FORCE_OR_MAIN.search(segment, found.end()):
            return True
    return False


def _delete_outside(command: str) -> bool:
    from .rules import _PROTECTED_WRITE_DIRS, _RM_RECURSIVE, _WRITE_OPS, _rm_outside
    if _RM_RECURSIVE.search(command) and _rm_outside(command):
        return True
    if any(name in command for name in _PROTECTED_WRITE_DIRS) and _WRITE_OPS.search(command):
        return True
    return bool(re.search(r"\bdd\s[^;&|\n]*\bof=", command))


@dataclass(frozen=True)
class ApprovalClass:
    key: str
    label: str
    description: str
    default: bool
    shell: tuple[re.Pattern, ...] = ()
    tools: re.Pattern | None = None
    mcp_only: bool = False
    check: Callable[[str], bool] | None = None

    def matches(self, tool: str, command: str) -> bool:
        if command and (any(pattern.search(command) for pattern in self.shell)
                        or (self.check is not None and self.check(command))):
            return True
        if self.tools is not None and tool and (_is_mcp(tool) or not self.mcp_only):
            return self.tools.search(tool) is not None
        return False


BUILTIN: tuple[ApprovalClass, ...] = (
    ApprovalClass(
        "send", "Sending email or messages",
        "Email, texts, DMs and chat messages to people, from the command line or a connector. Messages to other bots never ask.", True,
        # not `desk grok send` or bot-msg: those message bots, not people
        shell=_shell(r"\b(?:sendmail|mutt|msmtp|swaks|mailx)\b|(?:^|[;&|]\s*)mail\s"),
        tools=_tool_re(r"send(?:_\w+)?", "reply", "reply_all", "forward", "tweet", "dm", "sms",
                       r"post_(?:message|comment|reply|tweet|status)"),
        mcp_only=True),
    ApprovalClass(
        "purchase", "Purchases and payments",
        "Buying, paying, ordering, refunding or moving money through a connector or CLI.", True,
        shell=_shell(r"\bstripe\s+(?:charges|payment_intents|refunds|transfers|payouts|invoices)\s+(?:create|pay|send)"),
        tools=_tool_re("pay", "purchase", "checkout", "buy", "charge", "refund",
                       r"(?:create|make|submit|confirm|place)_(?:payment|order|purchase|charge|transfer)",
                       r"spend_request_create"),
        mcp_only=True),
    ApprovalClass(
        "release", "Merging, releasing, force-pushing",
        "Merging pull requests, publishing releases or packages, and pushing to main or with --force.", True,
        shell=_shell(r"\bgh\s+pr\s+merge\b",
                     r"\bgh\s+release\s+(?:create|upload|edit|delete)\b",
                     r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\b(?:docker|podman)\s+push\b"),
        tools=_tool_re(r"merge_pull_request", r"create_release", r"publish_\w+"), mcp_only=True,
        check=_git_push_risky),
    ApprovalClass(
        "system", "Admin and system changes",
        "sudo, services (systemctl), scheduled jobs (crontab), user accounts, shutdown and reboot.", True,
        shell=_shell(r"\bsudo\b|\bpkexec\b|\bsu\s+-",
                     r"\b(?:systemctl|loginctl|crontab|shutdown|reboot|mkfs\S*|visudo|useradd|userdel|usermod)\b",
                     r"\bpodman\s+(?:system\s+prune|volume\s+rm)\b")),
    ApprovalClass(
        "delete", "Deleting outside the bot's workspace",
        "Deleting or overwriting files elsewhere on the computer, or deleting email, files and records "
        "through a connector. Work inside the bot's own folder is never asked about.", True,
        tools=_tool_re("delete", r"delete_\w+", "trash", r"trash_\w+", "destroy", "purge",
                       "wipe"),
        mcp_only=True, check=_delete_outside),
    ApprovalClass(
        "share", "Pushing code and posting on GitHub",
        "git push to any branch, opening or commenting on pull requests and issues.", False,
        shell=_shell(_SEP + r"git\s+" + _GIT_OPTS + r"push\b",
                     r"\bgh\s+(?:issue|pr)\s+(?:comment|create|edit|close|review|reopen)\b"),
        tools=_tool_re(r"create_pull_request", r"create_issue", r"add_\w*comment\w*", "push_files"), mcp_only=True),
    ApprovalClass(
        "remote", "Logging in to other computers",
        "ssh, scp, sftp and rsync to another machine.", False,
        shell=_shell(r"\b(?:ssh|scp|sftp)\s", r"\brsync\b[^;&|\n]*\s[^\s:]+:")),
    ApprovalClass(
        "devices", "Controlling printers and the screen",
        "3D printer control (PrusaLink, OctoPrint) and clicking or typing on a desktop (desk click/type/key).",
        False, shell=_shell(r"\b(?:prusa-?link|octoprint(?:-cli)?|octocmd)\b", r"\bdesk\s+(?:click|type|key)\b")),
    ApprovalClass(
        "connectors", "Any connector action",
        "Every tool call through a connector (MCP server), including ones that only read.", False,
        tools=re.compile(r".*"), mcp_only=True),
)
BUILTIN_KEYS = tuple(item.key for item in BUILTIN)


class ApprovalClassError(ValueError):
    pass


def _load(store: JobStore) -> dict:
    raw = store.get_meta(KEY)
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    return saved if isinstance(saved, dict) else {}


def _custom_class(item: dict) -> ApprovalClass | None:
    try:
        pattern = re.compile(item["pattern"], re.IGNORECASE if item["kind"] == "tool" else 0)
    except (KeyError, TypeError, re.error):
        return None
    if item["kind"] == "tool":
        return ApprovalClass(item["key"], item["label"], "", True, tools=pattern)
    return ApprovalClass(item["key"], item["label"], "", True, shell=(pattern,))


def settings(store: JobStore) -> dict:
    """What the Settings page shows: every class with its on/off state, and the timeout."""
    saved = _load(store)
    enabled = saved.get("enabled") if isinstance(saved.get("enabled"), dict) else {}
    classes = [{"key": item.key, "label": item.label, "description": item.description, "builtin": True,
                "default": item.default, "enabled": bool(enabled.get(item.key, item.default))} for item in BUILTIN]
    for item in saved.get("custom") or []:
        if isinstance(item, dict) and _custom_class(item) is not None:
            classes.append({"key": item["key"], "label": item["label"], "description": "", "builtin": False,
                            "kind": item["kind"], "pattern": item["pattern"], "default": True,
                            "enabled": bool(enabled.get(item["key"], True))})
    timeout = saved.get("timeout_min", TIMEOUT_DEFAULT)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 <= timeout <= TIMEOUT_MAX:
        timeout = TIMEOUT_DEFAULT
    return {"classes": classes, "timeout_min": timeout}


def update(store: JobStore, *, enabled: dict[str, bool] | None = None, add: list[dict] | None = None,
           remove: list[str] | None = None, timeout_min: int | None = None) -> dict:
    saved = _load(store)
    current = settings(store)
    known = {item["key"] for item in current["classes"]}
    flags = {item["key"]: item["enabled"] for item in current["classes"]}
    custom = [item for item in saved.get("custom") or [] if isinstance(item, dict) and _custom_class(item)]
    for key in remove or []:
        if key in BUILTIN_KEYS:
            raise ApprovalClassError("built-in classes can be switched off, not removed")
        if key not in known:
            raise ApprovalClassError(f"no approval class {key!r}")
        custom = [item for item in custom if item["key"] != key]
        flags.pop(key, None)
    for item in add or []:
        label = str(item.get("label") or "").strip()
        kind = item.get("kind")
        pattern = str(item.get("pattern") or "").strip()
        if not label or len(label) > LABEL_LIMIT:
            raise ApprovalClassError(f"a class name is 1 to {LABEL_LIMIT} characters")
        if kind not in CUSTOM_KINDS:
            raise ApprovalClassError("kind must be shell (a command pattern) or tool (a tool-name pattern)")
        if not pattern or len(pattern) > PATTERN_LIMIT:
            raise ApprovalClassError(f"a pattern is 1 to {PATTERN_LIMIT} characters")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ApprovalClassError(f"invalid pattern: {exc}") from exc
        if len(custom) >= CUSTOM_LIMIT:
            raise ApprovalClassError(f"at most {CUSTOM_LIMIT} classes of your own")
        number = 1 + max([int(entry["key"].split("-")[1]) for entry in custom
                          if str(entry.get("key", "")).startswith("custom-")
                          and str(entry["key"]).split("-")[1].isdigit()] or [0])
        key = f"custom-{number}"
        custom.append({"key": key, "label": label, "kind": kind, "pattern": pattern})
        known.add(key)
        flags[key] = True
    for key, value in (enabled or {}).items():
        if key not in known:
            raise ApprovalClassError(f"no approval class {key!r}")
        flags[key] = bool(value)
    if timeout_min is not None:
        if not 0 <= timeout_min <= TIMEOUT_MAX:
            raise ApprovalClassError(f"the timeout is 0 (never) to {TIMEOUT_MAX} minutes")
        saved["timeout_min"] = timeout_min
    saved["custom"] = custom
    saved["enabled"] = flags
    store.set_meta(KEY, json.dumps(saved))
    return settings(store)


def active(store: JobStore) -> list[ApprovalClass]:
    saved = _load(store)
    enabled = saved.get("enabled") if isinstance(saved.get("enabled"), dict) else {}
    result = [item for item in BUILTIN if enabled.get(item.key, item.default)]
    for item in saved.get("custom") or []:
        built = _custom_class(item) if isinstance(item, dict) else None
        if built is not None and enabled.get(built.key, True):
            result.append(built)
    return result


def match(store: JobStore, tool: str, command: str) -> ApprovalClass | None:
    """The first switched-on class this tool call falls in, or None."""
    for item in active(store):
        try:
            if item.matches(tool, command):
                return item
        except re.error:
            continue
    return None


def label(store: JobStore, key: str | None) -> str | None:
    if not key:
        return None
    for item in settings(store)["classes"]:
        if item["key"] == key:
            return item["label"]
    return None


def timeout_min(store: JobStore) -> int:
    return settings(store)["timeout_min"]
