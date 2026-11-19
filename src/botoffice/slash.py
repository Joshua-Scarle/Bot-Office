"""Slash commands for every bot CLI (0.11.14).

The chat box lists the commands of the CLI that runs the bot's turns. Where the CLI can say what its
commands are, the list comes from the installed CLI itself (and is cached per CLI version and per
custom-command files); where it exposes nothing machine-readable, a small documented built-in list is
used. Every entry says where it came from (``source``) and what Bot Office does with it (``action``):

  run      the CLI runs it headless (Claude Code, Antigravity: the raw ``/name args`` is the whole turn)
  command  OpenCode: ``opencode run --command name -- args``
  expand   the CLI's own custom command file (Codex prompts, Cursor commands) is filled in by Bot Office
           and sent as the message, which is what the CLI's TUI does with such a file
  map      a Bot Office control does the job (model, effort, new session)
  grey     cannot work headless (interactive picker, TUI only, ...): shown greyed with the reason

Login, logout and other authentication commands are never listed, mapped or run (``is_auth``): they
would touch subscription credentials, which Bot Office never collects, stores or proxies.

Sources, per engine (the CLI versions checked on the VPS: Claude Code 2.1.319, Codex 0.167.0, OpenCode
1.19.5, Cursor Agent 2026.11.17, Antigravity 1.5.12):
  claude      cli: the ``slash_commands`` / ``terminal_slash_commands`` of the init event of
              ``claude -p --output-format stream-json --verbose /context`` (a local command: no model turn).
              Descriptions: documented table, command and skill files.
  opencode    cli: ``GET /command`` of a short-lived ``opencode serve`` (password-protected, loopback).
  antigravity cli: ``agy -p /help`` (local), one ``/name<TAB>description`` line per command.
  codex       documented built-ins (``codex exec`` has no command list and treats ``/x`` as text) plus
              ``~/.codex/prompts/*.md`` custom prompts.
  cursor      documented built-ins (same reason) plus ``.cursor/commands`` and ``~/.cursor/commands``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import secrets
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .catalog import ENGINE_LABELS, ENGINES

CATALOG_VERSION = 2  # bump to drop every cached list
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}$")
MAX_ARGS = 20_000
PROBE_TIMEOUT = 25.0
VERSION_TTL = 60.0  # a CLI update is noticed within a minute
MAX_KEYS = 8
FALLBACK_TTL = 60.0  # a failed probe is not asked again for this long
DESC_MAX = 160

# ---- never surfaced, mapped or run: anything that logs in or out, or handles credentials ----
AUTH_RE = re.compile(
    r"(?<![a-z])(?:re|de)?(?:auth(?:enticat\w*|oriz\w*)?|log[-_.]?(?:in|out)|sign[-_.]?(?:in|out)|oauth|credentials?|connect|"
    r"api-?key|(?:setup|api|access|auth)-?tokens?|setup-(?:bedrock|vertex)|install-github-app|account)(?![a-z])", re.I)


def is_auth(name: str) -> bool:
    return bool(AUTH_RE.search(name.strip().lstrip("/")))


# ---- what a Bot Office control does for a command that cannot (or should not) run in the CLI ----
MODEL, EFFORT, NEW = "model", "effort", "new"
MAPPED_ALL: dict[str, tuple[str, str]] = {
    "model": (MODEL, "Switch this bot's model (opens the model picker; /model <id> sets it)"),
    "models": (MODEL, "Switch this bot's model (opens the model picker)"),
    "effort": (EFFORT, "Change this bot's effort level (opens Settings; /effort <level> sets it)"),
    "new": (NEW, "Start a fresh session on the next turn (chat history is kept)"),
    "clear": (NEW, "Start a fresh session on the next turn (chat history is kept)"),
    "reset": (NEW, "Start a fresh session on the next turn (chat history is kept)"),
    "new-chat": (NEW, "Start a fresh session on the next turn (chat history is kept)"),
}
TARGET_ENGINES: dict[str, tuple[str, ...]] = {EFFORT: ("claude", "codex", "opencode", "antigravity")}
CONFIRM_TARGETS = {NEW}

# ---- documented built-ins; used where the CLI lists nothing, and for the descriptions the CLI does not give ----
TUI = "interactive terminal command; it has no headless form"
CODEX_BUILTINS: dict[str, tuple[str, str | None]] = {
    "model": ("choose the model and reasoning effort", None),
    "new": ("start a new chat during a conversation", None),
    "clear": ("clear the terminal and start a new chat", None),
    "compact": ("summarize the conversation to free context", "Bot Office has no manual compaction control (auto-compaction is on); codex exec has no /compact"),
    "review": ("review my current changes and find issues", "runs as the separate `codex review` command, not inside a chat turn"),
    "diff": ("show the git diff", TUI),
    "status": ("show the session configuration and token usage", TUI),
    "init": ("create an AGENTS.md file with instructions for Codex", TUI),
    "mention": ("mention a file", TUI),
    "mcp": ("list configured MCP tools", TUI),
    "permissions": ("choose what Codex is allowed to do", "Bot Office sets the access level per bot (CLI access in Settings)"),
    "approvals": ("choose what Codex is allowed to do", "Bot Office sets the access level per bot (CLI access in Settings)"),
    "fork": ("fork the current chat", TUI),
    "resume": ("resume a saved chat", TUI),
    "agents": ("browse and switch sessions", TUI),
    "feedback": ("send logs to the maintainers", TUI),
    "quit": ("exit Codex", TUI),
    "exit": ("exit Codex", TUI),
    "logout": ("sign out", None),
}
CURSOR_BUILTINS: dict[str, tuple[str, str | None]] = {
    "model": ("set or list models", None),
    "new-chat": ("start a new chat", None),
    "compress": ("summarize the conversation to free context", "Bot Office has no manual compaction control; Cursor compresses on its own"),
    "plan": ("switch to plan mode", "Bot Office runs Cursor turns with its own mode"),
    "ask": ("switch to ask mode", "Bot Office runs Cursor turns with its own mode"),
    "resume": ("resume a previous chat", TUI),
    "mcp": ("manage MCP servers", TUI),
    "rules": ("create and edit rules", TUI),
    "commands": ("create and edit custom commands", TUI),
    "usage": ("show usage", TUI),
    "auto-run": ("toggle auto-run for tool calls", "Bot Office sets the access level per bot (CLI access in Settings)"),
    "sandbox": ("configure the sandbox", "Bot Office sets the access level per bot (CLI access in Settings)"),
    "vim": ("toggle Vim keys", TUI),
    "help": ("show help", TUI),
    "about": ("show version and account information", TUI),
    "feedback": ("send feedback", TUI),
    "quit": ("exit Cursor Agent", TUI),
    "logout": ("sign out", None),
}
OPENCODE_BUILTINS: dict[str, tuple[str, str | None]] = {
    "new": ("start a new session", None),
    "models": ("list available models", None),
    "compact": ("compact the session", "Bot Office has no manual compaction control; OpenCode compacts on its own"),
    "sessions": ("list and switch sessions", TUI),
    "undo": ("undo the last message and file changes", TUI),
    "redo": ("redo an undone message", TUI),
    "share": ("share the current session", "sharing a session sends it to a public link; use the CLI directly"),
    "unshare": ("unshare the current session", TUI),
    "export": ("export the conversation to Markdown", TUI),
    "editor": ("open an external editor", TUI),
    "details": ("toggle tool execution details", TUI),
    "thinking": ("toggle thinking blocks", TUI),
    "themes": ("list available themes", TUI),
    "help": ("show the help dialog", TUI),
    "exit": ("exit OpenCode", TUI),
    "connect": ("add a provider", None),
}
# claude: names the CLI lists (init event) that are Claude Code's own; the rest are skills, plugins and custom files
CLAUDE_DESCRIPTIONS = {
    "autocompact": "show or set the auto-compact window", "clear": "start a new conversation", "compact": "summarize the conversation to free context",
    "config": "change settings (key=value)", "context": "show context window usage", "doctor": "diagnose the installation",
    "effort": "set the reasoning effort", "extra-usage": "manage extra usage", "fast": "toggle fast mode", "init": "create a CLAUDE.md for this project",
    "insights": "report on your Claude Code sessions", "mcp": "show MCP server status", "model": "set the model",
    "output-style": "show or set the output style", "recap": "recap the current session", "rename": "rename the session",
    "security-review": "security review of the pending changes", "usage": "show plan usage limits", "usage-credits": "manage usage credits",
    "ultrareview": "multi-agent cloud review (billed)", "reload-skills": "reload skills", "list-agents": "list sessions and agents",
    "goal": "set or show the session goal", "code-review": "review the current diff or a PR", "simplify": "clean up the changed code",
    "loop": "run a prompt on an interval", "schedule": "manage scheduled routines", "debug": "debug an issue", "verify": "verify a change works",
    "batch": "run a change across many files in parallel", "update-config": "change Claude Code settings", "run": "launch the app and see the change",
    "agents": "list agents", "advisor": "ask the advisor model", "import": "import settings from another tool",
    "color": "set the session color", "focus": "focus view", "heapdump": "write a heap dump", "reload-plugins": "reload plugins",
    "skill-doctor": "diagnose skills", "team-onboarding": "onboarding guide for a team", "design-consent": "design tools consent", "design-revoke": "revoke design tools consent",
}
CLAUDE_GREY = {
    "fast": "fast mode is not available when Claude Code runs as an agent",
    "agents": "the agents wizard was removed from Claude Code; ask the bot to create one",
    "config": "settings are changed in Bot Office Settings or the CLI directly",
    "import": TUI, "heapdump": "debugging command for the terminal session", "design-consent": TUI, "design-revoke": TUI,
    "ultrareview": "launches a billed cloud review; the owner starts it from the CLI",
}
CLAUDE_MAPPED = {"model", "effort", "clear", "reset", "new"}
AGY_GREY = {
    "config": "settings are changed in Bot Office Settings or the CLI directly",
    "settings": "settings are changed in Bot Office Settings or the CLI directly",
    "hooks": "Bot Office manages the hooks of its bots (action gate)",
    "permissions": "Bot Office sets the access level per bot (CLI access in Settings)",
    "plugin": TUI, "plugins": TUI, "agents": TUI, "credits": "shows a purchase link; use the CLI directly",
}


def _clip(text: str, limit: int = DESC_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def entry(name: str, description: str = "", *, source: str, kind: str = "builtin", action: str = "run",
          target: str | None = None, reason: str | None = None, confirm: bool = False,
          engine: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "description": _clip(description), "source": source, "kind": kind,
                           "action": action}
    if target:
        out["target"] = target
        out["confirm"] = confirm or target in CONFIRM_TARGETS
    if reason:
        out["reason"] = _clip(reason, 200)
    return out


# ---- custom command and skill files ----
def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(frontmatter keys, body) of a Markdown file with an optional ``---`` header (flat ``key: value`` only)."""
    meta: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip() and not key.startswith((" ", "\t")):
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            body = text[end + 4:]
            return meta, body[1:] if body.startswith("\n") else body
    return meta, text


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _read(path: Path, limit: int = 200_000) -> str:
    """Text of a regular file; a symlink (planted to point at something private) is never followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read(limit).decode(errors="replace")
    except OSError:
        return ""


def scan_commands(root: Path, *, depth: int = 3, cap: int = 400) -> list[tuple[str, Path]]:
    """``name`` (folders become ``a:b``) and path of every ``*.md`` below *root*; symlinks are not followed."""
    found: list[tuple[str, Path]] = []
    if not root.is_dir() or root.is_symlink():
        return found

    def walk(folder: Path, prefix: str, level: int) -> None:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return
        for item in names:
            if len(found) >= cap:
                return
            path = folder / item
            if path.is_symlink():
                continue
            if path.is_dir() and level < depth:
                walk(path, f"{prefix}{item}:", level + 1)
            elif path.is_file() and item.endswith(".md") and not item.startswith("."):
                found.append((prefix + item[:-3], path))

    walk(root, "", 1)
    return found


def scan_skills(root: Path, cap: int = 400) -> list[tuple[str, Path]]:
    """``name`` and ``SKILL.md`` of every skill folder directly below *root*."""
    out: list[tuple[str, Path]] = []
    if not root.is_dir() or root.is_symlink():
        return out
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for item in names[:cap]:
        path = root / item / "SKILL.md"
        if not (root / item).is_symlink() and not path.is_symlink() and path.is_file():
            out.append((item, path))
    return out


def file_signature(paths: list[Path]) -> str:
    """Changes when a command or skill file is added, removed or edited (name, size, mtime)."""
    digest = hashlib.sha256()
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        digest.update(f"{path}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def custom_roots(engine: str, home: Path, project: Path | None) -> dict[str, list[Path]]:
    """Where the engine keeps its custom commands (``commands``) and skills (``skills``)."""
    roots: dict[str, list[Path]] = {"commands": [], "skills": []}
    dirs = [home] + ([project] if project else [])
    for base in dirs:
        if engine == "claude":
            roots["commands"].append(base / ".claude" / "commands")
            roots["skills"].append(base / ".claude" / "skills")
        elif engine == "codex" and base == home:
            roots["commands"].append(base / ".codex" / "prompts")
        elif engine == "cursor":
            roots["commands"].append(base / ".cursor" / "commands")
        elif engine == "opencode":
            roots["commands"] += [base / ".opencode" / "command", base / ".opencode" / "commands"]
            if base == home:
                roots["commands"] += [base / ".config" / "opencode" / "command", base / ".config" / "opencode" / "commands"]
    return roots


def _inside(base: Path, root: Path) -> bool:
    """*root* is reached from *base* without crossing a symlink (a bot can plant ``.cursor -> /elsewhere``)."""
    try:
        parts = root.relative_to(base).parts
    except ValueError:
        return False
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def custom_files(engine: str, home: Path, project: Path | None) -> tuple[list[tuple[str, Path, str]], list[Path]]:
    """([(name, path, "command"|"skill")], every file looked at) for the signature."""
    roots = custom_roots(engine, home, project)
    bases = [b for b in (home, project) if b is not None]
    roots = {kind: [r for r in paths if any(_inside(b, r) for b in bases)] for kind, paths in roots.items()}
    items: list[tuple[str, Path, str]] = []
    seen: list[Path] = []
    for root in roots["commands"]:
        for name, path in scan_commands(root):
            items.append((name, path, "command"))
            seen.append(path)
    for root in roots["skills"]:
        for name, path in scan_skills(root):
            items.append((name, path, "skill"))
            seen.append(path)
    return items, seen


def custom_entries(items: list[tuple[str, Path, str]], *, action: str, expand: bool) -> dict[str, dict[str, Any]]:
    """Entries for custom files. Project files win over home files (they were listed later)."""
    out: dict[str, dict[str, Any]] = {}
    for name, path, kind in items:
        meta, body = _frontmatter(_read(path))
        description = meta.get("description") or _first_line(body)
        hint = meta.get("argument-hint") or meta.get("argument_hint") or ""
        e = entry(name, description + (f"  {hint}" if hint else ""), source="skill" if kind == "skill" else "custom",
                  kind=kind, action=action)
        if expand:
            e["file"] = str(path)
        out[name] = e
    return out


# ---- expanding a custom command file (Codex prompts, Cursor commands) ----
def render_template(body: str, args: str) -> str:
    """What the CLIs do with a command file: ``$ARGUMENTS`` is all the arguments, ``$1``..``$9`` the
    positional ones, ``$$`` a dollar sign; named ``$NAME`` placeholders are left as written."""
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        rest = body[i + 1:]
        if rest.startswith("$"):
            out.append("$")
            i += 2
        elif rest.startswith("ARGUMENTS"):
            out.append(args.strip())
            i += 1 + len("ARGUMENTS")
        elif rest[:1].isdigit() and rest[0] != "0":
            index = int(rest[0])
            out.append(words[index - 1] if index <= len(words) else "")
            i += 2
        else:
            out.append("$")
            i += 1
    text = "".join(out).strip()
    if "$ARGUMENTS" not in body and args.strip() and not re.search(r"\$[1-9]", body):
        text += "\n\n" + args.strip()  # a command that takes no placeholder still gets what the user typed after it
    return text


def expand_file(path: str | Path, args: str) -> str:
    _, body = _frontmatter(_read(Path(path), 100_000))
    return render_template(body, args)


# ---- per-engine builders ----
def _documented(table: dict[str, tuple[str, str | None]], engine: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, (description, grey) in table.items():
        out[name] = _classify_builtin(engine, name, description, "documented", grey)
    return out


def _classify_builtin(engine: str, name: str, description: str, source: str, grey: str | None = None,
                      terminal: bool = False) -> dict[str, Any]:
    """Mapped controls win, then greyed commands; what is left runs in the CLI."""
    mapped = MAPPED_ALL.get(name)
    if mapped and (mapped[0] not in TARGET_ENGINES or engine in TARGET_ENGINES[mapped[0]]):
        return entry(name, mapped[1], source=source, action="map", target=mapped[0])
    if grey:
        return entry(name, description, source=source, action="grey", reason=grey)
    if terminal:
        return entry(name, description, source=source, action="grey", reason="Claude Code lists it as terminal-only")
    return entry(name, description, source=source, action="run")


def parse_claude_init(text: str) -> tuple[list[str], list[str]] | None:
    """(slash_commands, terminal_slash_commands) of the first system/init line of stream-json output."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            commands = [str(c) for c in obj.get("slash_commands") or [] if isinstance(c, str)]
            terminal = [str(c) for c in obj.get("terminal_slash_commands") or [] if isinstance(c, str)]
            return commands, terminal
    return None


def build_claude(names: list[str], terminal: list[str], items: list[tuple[str, Path, str]]) -> list[dict[str, Any]]:
    custom = {name: (path, kind) for name, path, kind in items}
    skill_ids = {name for name, _, kind in items if kind == "skill"}
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        if is_auth(name):
            continue
        if name.startswith("__"):
            continue  # internal
        if name in custom:
            meta, body = _frontmatter(_read(custom[name][0]))
            desc = meta.get("description") or _first_line(body)
            hint = meta.get("argument-hint") or ""
            kind = "skill" if name in skill_ids else "command"
            out[name] = entry(name, desc + (f"  {hint}" if hint else ""), source="custom" if kind == "command" else "skill",
                              kind=kind)
        elif ":" in name:
            plugin, _, rest = name.partition(":")
            out[name] = entry(name, f"{rest.replace('-', ' ')} (plugin {plugin})", source="plugin", kind="plugin")
        elif name in CLAUDE_DESCRIPTIONS or name in CLAUDE_MAPPED or name in terminal:
            out[name] = _classify_builtin("claude", name, CLAUDE_DESCRIPTIONS.get(name, ""), "cli",
                                          CLAUDE_GREY.get(name), terminal=name in terminal)
        else:
            out[name] = entry(name, "", source="cli", kind="skill")  # a bundled skill or an unknown command: Claude runs it
    for mapped in ("model", "effort", "clear"):
        out.setdefault(mapped, _classify_builtin("claude", mapped, "", "cli"))
    return list(out.values())


def parse_agy_help(text: str) -> list[tuple[str, str]]:
    """``/name (alias)<TAB>description`` lines of ``agy -p /help`` to (name, description)."""
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = re.match(r"^/([A-Za-z0-9][\w:.-]*)(?:\s+\(([^)]*)\))?\s*\t+\s*(.+?)\s*$", line)
        if match:
            found.append((match.group(1), match.group(3)))
    return found


def build_agy(parsed: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out = []
    for name, description in parsed:
        if is_auth(name):
            continue
        out.append(_classify_builtin("antigravity", name, description, "cli", AGY_GREY.get(name)))
    for mapped in ("model", "effort"):
        if not any(e["name"] == mapped for e in out):
            out.append(_classify_builtin("antigravity", mapped, "", "cli"))
    return out


def build_opencode(commands: list[dict[str, Any]], items: list[tuple[str, Path, str]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in commands:
        name = str(c.get("name") or "")
        if not name or is_auth(name):
            continue
        kind = str(c.get("source") or "command")
        description = str(c.get("description") or "")
        hints = c.get("hints")
        if isinstance(hints, list) and hints:
            description += "  " + " ".join(str(h) for h in hints[:4])
        out[name] = entry(name, description, source="cli", kind={"command": "command", "skill": "skill", "mcp": "mcp"}.get(kind, kind))
    for name, desc_tuple in OPENCODE_BUILTINS.items():
        if is_auth(name) or name in out:
            continue
        out[name] = _classify_builtin("opencode", name, desc_tuple[0], "documented", desc_tuple[1])
    for mapped in ("model", "effort"):
        if mapped not in out:
            out[mapped] = _classify_builtin("opencode", mapped, "", "documented")
    return list(out.values())


def build_expand(engine: str, table: dict[str, tuple[str, str | None]],
                 items: list[tuple[str, Path, str]]) -> list[dict[str, Any]]:
    out = _documented({n: v for n, v in table.items() if not is_auth(n)}, engine)
    for name, e in custom_entries([i for i in items if i[2] == "command"], action="expand", expand=True).items():
        if is_auth(name):
            continue
        if name in out:
            continue  # a built-in (a control of Bot Office, or one greyed with its reason) keeps its name
        out[name] = e
    for mapped in ("model", "effort"):
        if mapped not in out:
            candidate = _classify_builtin(engine, mapped, "", "documented")
            if candidate["action"] == "map":
                out[mapped] = candidate
    return list(out.values())


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runnable first (mapped controls, then the rest), greyed last; alphabetical within each group."""
    rank = {"map": 0, "run": 1, "command": 1, "expand": 1, "grey": 2}
    return sorted(entries, key=lambda e: (rank.get(e["action"], 3), e["name"].lower()))


def set_actions(engine: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """opencode runs its listed commands with ``--command``: ``run`` entries become ``command``."""
    if engine == "opencode":
        for e in entries:
            if e["action"] == "run":
                e["action"] = "command"
    return entries


# ---- probes (spawn the real CLI; kept apart so tests can replace them) ----
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def fetch_json(url: str, password: str | None, timeout: float = 3.0) -> Any:
    request = urllib.request.Request(url)
    import base64
    if password is not None:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode())
    with _LOOPBACK.open(request, timeout=timeout) as response:  # loopback only: no proxy, no redirects
        return json.loads(response.read(2_000_000))


async def probe_opencode(adapter, cwd: Path) -> list[dict[str, Any]] | None:
    """The command list of a short-lived loopback ``opencode serve`` guarded by a random password."""
    executable = adapter.executable
    if not executable:
        return None
    port = free_port()
    password = secrets.token_urlsafe(24)
    env = adapter._env()
    env["OPENCODE_SERVER_PASSWORD"] = password
    argv, env, header = adapter._spawn([executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)], env, cwd)
    from .providers import cli as cli_mod
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env,
        cwd=str(cwd) if cwd.is_dir() and not header else None, start_new_session=True,
        **({"stdin": asyncio.subprocess.PIPE} if header else {"stdin": asyncio.subprocess.DEVNULL}))
    try:
        if header and proc.stdin is not None:
            proc.stdin.write(header)
            await proc.stdin.drain()
        deadline = time.monotonic() + PROBE_TIMEOUT
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                return None
            url = f"http://127.0.0.1:{port}/command"
            try:  # our server answers 401 without the password; anything else on the port is not ours
                await asyncio.to_thread(fetch_json, url, None)
                return None
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    return None
            except (OSError, urllib.error.URLError, ValueError):
                await asyncio.sleep(0.5)
                continue
            try:
                data = await asyncio.to_thread(fetch_json, url, password)
            except (OSError, urllib.error.URLError, ValueError):
                return None
            return data if isinstance(data, list) else None
        return None
    finally:
        await cli_mod.CliAdapter._kill_process_group(proc)


async def probe_claude(adapter, cwd: Path) -> tuple[list[str], list[str]] | None:
    got = await adapter.capture_bytes(("-p", "--output-format", "stream-json", "--verbose", "/context"),
                                      timeout=PROBE_TIMEOUT, cwd=cwd)
    if got is None:
        return None
    return parse_claude_init(got[1].decode(errors="replace"))


async def probe_agy(adapter, cwd: Path) -> list[tuple[str, str]] | None:
    got = await adapter.capture_bytes(("-p", "/help"), timeout=PROBE_TIMEOUT, cwd=cwd)
    if got is None or got[0] != 0:
        return None
    parsed = parse_agy_help(got[1].decode(errors="replace"))
    return parsed or None


async def cli_version(adapter) -> str:
    got = await adapter.capture_bytes(("--version",), timeout=15)
    if got is None or got[0] != 0:
        return ""
    return " ".join(got[1].decode(errors="replace").split())[:80]


# ---- the catalog service ----
def _probe_ok(engine: str, probe: Any) -> bool:
    need = {"claude": "names", "opencode": "commands", "antigravity": "help"}.get(engine, "documented")
    return isinstance(probe, dict) and need in probe


class SlashCatalog:
    """Lists, caches and resolves slash commands. ``cache_file`` keeps the CLI probes between restarts."""

    def __init__(self, providers: dict[str, Any], data_dir: Path, home: Path | None = None):
        self.providers = providers
        self.data_dir = data_dir
        self.home = home
        self._cache: dict[str, dict[str, Any]] = {}
        self._failed: dict[str, float] = {}
        self._versions: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loaded = False

    # -- persistence of the probes (only what the CLI said, keyed by engine + version + signature) --
    @property
    def cache_file(self) -> Path:
        return self.data_dir / "slash-cache.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self.cache_file.read_text())
            if isinstance(data, dict) and data.get("v") == CATALOG_VERSION and isinstance(data.get("probes"), dict):
                self._cache.update(data["probes"])

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"v": CATALOG_VERSION, "probes": self._cache}, handle)
            os.replace(tmp, self.cache_file)

    def _home(self) -> Path:
        from . import isolation
        return self.home or Path(isolation.bots_home())

    async def _version(self, engine: str, adapter) -> str:
        """The CLI's version, asked at most once a minute (the cache key of its command list)."""
        seen = self._versions.get(engine)
        if seen and time.monotonic() - seen[0] < VERSION_TTL:
            return seen[1]
        version = await cli_version(adapter)
        self._versions[engine] = (time.monotonic(), version)
        return version

    async def commands(self, engine: str, project: Path | None = None, refresh: bool = False) -> dict[str, Any]:
        if engine not in ENGINES:  # an engine without a CLI of its own (nothing to list)
            return {"engine": engine, "label": ENGINE_LABELS.get(engine, engine), "version": "", "cached": False,
                    "source": "none", "note": "This engine has no slash commands.", "commands": []}
        adapter = self.providers.get(engine)
        home = self._home()
        items, seen = custom_files(engine, home, project)
        signature = file_signature(seen)
        usable = adapter is not None and getattr(adapter, "enabled", False) and bool(getattr(adapter, "executable", None))
        version = await self._version(engine, adapter) if usable else ""
        key = f"{engine}|{version}|{signature}"
        self._load()
        lock = self._locks.setdefault(engine, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None and not _probe_ok(engine, hit.get("probe") if isinstance(hit, dict) else None):
                self._cache.pop(key, None)  # damaged or hand-edited cache entry
                hit = None
            cached = hit is not None and not refresh
            if usable and (hit is None or refresh):
                if time.monotonic() - self._failed.get(engine, -1e9) >= FALLBACK_TTL:
                    probe = await self._probe(engine, adapter, project or home, items, usable)
                    if probe is not None:
                        mine = [k for k in self._cache if k.startswith(engine + "|")]
                        for old_key in mine[:max(0, len(mine) - (MAX_KEYS - 1))]:
                            self._cache.pop(old_key, None)
                        self._cache[key] = hit = {"probe": probe}
                        cached = False
                        self._save()
                        self._failed.pop(engine, None)
                    else:
                        self._failed[engine] = time.monotonic()  # a failed refresh keeps the stale hit, if any
            try:
                entries, source_note = self._assemble(engine, hit["probe"] if hit else None, items)
            except (KeyError, TypeError, ValueError, AttributeError):  # a damaged cache entry: forget it, use the fallback
                self._cache.pop(key, None)
                hit = None
                entries, source_note = self._assemble(engine, None, items)
        return {"engine": engine, "label": ENGINE_LABELS.get(engine, engine), "version": version,
                "cached": cached, "source": "cli" if hit else "fallback", "note": source_note,
                "commands": sort_entries(set_actions(engine, entries))}

    async def _probe(self, engine, adapter, cwd: Path, items, usable: bool) -> Any:
        if not usable:
            return None
        if time.monotonic() - self._failed.get(engine, -1e9) < FALLBACK_TTL:
            return None
        cwd = cwd if cwd.is_dir() else self._home()
        try:
            if engine == "claude":
                got = await probe_claude(adapter, cwd)
                return {"names": got[0], "terminal": got[1]} if got else None
            if engine == "opencode":
                got = await probe_opencode(adapter, cwd)
                return {"commands": got} if got else None
            if engine == "antigravity":
                got = await probe_agy(adapter, cwd)
                return {"help": got} if got else None
        except (OSError, asyncio.TimeoutError):
            return None
        return {"documented": True}  # codex / cursor: nothing to ask the CLI; the documented list is the list

    def _assemble(self, engine: str, probe: dict[str, Any] | None, items) -> tuple[list[dict[str, Any]], str]:
        if engine == "claude":
            if probe and "names" in probe:
                return build_claude(probe["names"], probe["terminal"], items), \
                    "From Claude Code's own command list (init event of a local /context run)."
            names = [n for n, _, _ in items] + ["context", "usage", "compact", "model", "effort", "clear"]
            return build_claude(names, [], items), \
                "Claude Code could not be asked for its list; showing a small built-in list and your own command files."
        if engine == "antigravity":
            if probe and "help" in probe:
                return build_agy([(n, d) for n, d in probe["help"]]), "From `agy -p /help`."
            return build_agy([("help", "show available commands and keybindings"), ("usage", "view model quota usage"),
                              ("skills", "list available skills")]), "Antigravity could not be asked for its list; showing a small built-in list."
        if engine == "opencode":
            if probe and "commands" in probe:
                return build_opencode(probe["commands"], items), "From OpenCode's own command list (`opencode serve`, GET /command)."
            return build_opencode([{"name": "init", "description": "guided AGENTS.md setup", "source": "command"},
                                   {"name": "review", "description": "review changes [commit|branch|pr]", "source": "command"}], items), \
                "OpenCode could not be asked for its list; showing a small built-in list."
        if engine == "codex":
            return build_expand("codex", CODEX_BUILTINS, items), \
                "Codex has no command list for headless runs: documented built-ins plus ~/.codex/prompts."
        return build_expand("cursor", CURSOR_BUILTINS, items), \
            "Cursor Agent has no command list for headless runs: documented built-ins plus .cursor/commands."

    async def resolve(self, engine: str, name: str, project: Path | None = None) -> dict[str, Any] | None:
        data = await self.commands(engine, project)
        wanted = name.strip().lstrip("/")
        for item in data["commands"]:
            if item["name"] == wanted:
                return item
        return None


def split_line(text: str) -> tuple[str, str] | None:
    """``/name args`` to (name, args); None when the text is not a slash line."""
    text = text.strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    head, _, args = text[1:].partition(" ")
    head = head.strip()
    if "\n" in head:
        head, _, rest = head.partition("\n")
        args = rest + (" " + args if args else "")
    if not NAME_RE.match(head):
        return None
    return head, args.strip()


def slash_job_text(name: str, args: str) -> str:
    return f"/{name} {args}".strip()


def encode_slash(name: str, args: str, mode: str) -> str:
    return json.dumps({"name": name, "args": args, "mode": mode}, ensure_ascii=False)


def decode_slash(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("name"), str) and data.get("mode") in ("raw", "command", "expand"):
        return {"name": data["name"], "args": str(data.get("args") or ""), "mode": data["mode"]}
    return None


__all__ = ["SlashCatalog", "is_auth", "split_line", "render_template", "expand_file", "encode_slash", "decode_slash"]
