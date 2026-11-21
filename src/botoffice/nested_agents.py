"""0.12.18 (B8): a nested agent CLI started with its permission checks switched off.

A bot may run another agent CLI from its shell (``claude -p ...``, ``codex exec ...``). Started
plainly, that child keeps its own permission prompts (headless, it cannot run a gated tool) and
inherits the run's environment: PATH with Bot-Office's ``bin/`` first, ``BOT_OFFICE_*`` and the
``GIT_CONFIG_*`` pairs that set ``core.hooksPath`` (the pre-push check). Started with a bypass flag
(``--permission-mode bypassPermissions``, ``--yolo``, ``-a never`` ...) it runs any tool unchecked,
so the bot's own approval rules would no longer see what it does.

``bypass_launch(command)`` finds such a launch in a shell command: the agent's name as the program
(also through a path, ``env``/``nohup``/``setsid``/``timeout``/``nice``/``xargs``/``sudo``
wrappers, ``bash -c``/``sh -c``/``eval``, ``$( )``, ``npx``/``bunx``/``pnpm dlx``/``uvx``/``pipx
run``, a here-document fed to a shell, inline interpreter code). A mention is not a launch:
``grep bypassPermissions f``, ``echo claude --yolo``, ``man claude``, a here-document written to a
file. rules.presets_hit reports it as the ``nested-agent`` preset, which the bot's ``outbound`` and
``destructive`` rules also govern (ask => card, allow => runs, never => refused).

Defense in depth, not a sandbox: a script file the bot wrote and then runs, a renamed copy of the
CLI, or flags built at run time are not read here (as for the git guard, fix4 #2). Stdlib only.
"""
from __future__ import annotations

import posixpath
import re

from . import git_guard

PRESET = "nested-agent"
LABEL = "Nested agent with permissions bypassed"
MAX_DEPTH = 6

# program name -> agent family
AGENTS = {"claude": "claude", "claude-code": "claude", "codex": "codex", "opencode": "opencode",
          "gemini": "gemini", "cursor-agent": "cursor", "agent": "cursor", "aider": "aider"}
# npm / PyPI package names (a version suffix is dropped) -> agent family
PACKAGES = {"@anthropic-ai/claude-code": "claude", "@openai/codex": "codex", "opencode-ai": "opencode",
            "@google/gemini-cli": "gemini", "aider-chat": "aider", "aider-install": "aider"}

# wrappers that run the words after them: option words that take a value (the next word)
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "nohup": set(), "setsid": set(), "command": set(), "builtin": set(), "exec": {"-a"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "--class", "-n", "--classdata", "-p", "--pid"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "time": {"-f", "--format", "-o", "--output"},
    "xargs": {"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L", "--max-lines", "-n", "--max-args",
              "-P", "--max-procs", "-s", "--max-chars"},
    "sudo": {"-u", "--user", "-g", "--group", "-C", "-D", "--chdir", "-h", "--host", "-p", "--prompt",
             "-r", "--role", "-t", "--type", "-U", "--other-user"},
    "doas": {"-u", "-C"}, "chronic": set(), "caffeinate": set(), "unbuffer": set(),
    "npx": {"-p", "--package"}, "bunx": {"-p", "--package"}, "pnpx": set(),
    "uvx": {"--from", "--with", "-p", "--python"},
}
# wrappers that take one positional word before the command (timeout's duration)
_POSITIONAL = {"timeout": 1}
# option words whose value is itself a shell command
_COMMAND_OPTS = {"env": {"-S", "--split-string"}, "npx": {"-c", "--call"}}

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _program(word: str) -> str | None:
    """The agent family *word* starts, as a program or a package name."""
    base = posixpath.basename(word).lower()
    if base in AGENTS:
        return AGENTS[base]
    package = word.lower()
    if package.count("@") > (1 if package.startswith("@") else 0):
        package = package.rsplit("@", 1)[0]  # name@version
    package = package.split("==", 1)[0]
    return PACKAGES.get(package)


def _value(words: list[str], index: int, name: str) -> str | None:
    """The value of option *name* at *index* (``--name=v`` or ``--name v``), else None."""
    word = words[index]
    if word == name:
        return words[index + 1] if index + 1 < len(words) else ""
    if word.startswith(name + "=") and name.startswith("--"):
        return word[len(name) + 1:]
    if not name.startswith("--") and word.startswith(name) and len(word) > len(name):
        return word[len(name):]  # -anever, -sdanger-full-access
    return None


def _unquote(value: str) -> str:
    return value.strip().strip("'\"").strip().lower()


def bypass_flag(family: str, args: list[str]) -> str | None:
    """The flag in *args* (an agent CLI's arguments) that switches its permission checks off."""
    for index, word in enumerate(args):
        if word == "--":
            break
        low = word.lower()
        if family == "claude":
            if low in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"):
                return word
            mode = _value(args, index, "--permission-mode")
            if mode is not None and _unquote(mode) == "bypasspermissions":
                return f"--permission-mode {mode}"
        elif family == "codex":
            if low in ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto"):
                return word
            for name in ("-s", "--sandbox"):
                value = _value(args, index, name)
                if value is not None and _unquote(value) == "danger-full-access":
                    return f"{name} {value}"
            for name in ("-a", "--ask-for-approval"):
                value = _value(args, index, name)
                if value is not None and _unquote(value) == "never":
                    return f"{name} {value}"
            for name in ("-c", "--config"):
                value = _value(args, index, name)
                if value is None or "=" not in value:
                    continue
                key, _, setting = value.partition("=")
                key, setting = key.strip().lower(), _unquote(setting)
                if (key == "approval_policy" and setting == "never") or (
                        key == "sandbox_mode" and setting == "danger-full-access"):
                    return f"{name} {value}"
        elif family in ("gemini", "opencode", "cursor"):
            if low in ("--yolo", "-y", "--dangerously-skip-permissions"):
                return word
            if family == "cursor" and low in ("--force", "-f"):
                return word
            mode = _value(args, index, "--approval-mode")
            if mode is not None and _unquote(mode) == "yolo":
                return f"--approval-mode {mode}"
        elif family == "aider" and low in ("--yes-always", "--yes"):
            return word
    return None


def bypass_launch(command: str) -> str | None:
    """A short description of the first nested agent CLI *command* starts with its permission checks
    switched off (``claude --permission-mode bypassPermissions``), else None."""
    if not command:
        return None
    try:
        return _scan(command[:git_guard.SCAN_LIMIT], 0)
    except (RecursionError, ValueError, IndexError):
        return _fallback(command)


def _fallback(text: str) -> str | None:
    """Text too deep or odd to parse: an agent name and a bypass flag as words anywhere in it."""
    words = re.findall(r"[^\s'\"`;&|()]+", text[:git_guard.SCAN_LIMIT])
    for index, word in enumerate(words):
        family = _program(word)
        if family:
            flag = bypass_flag(family, words[index + 1:])
            if flag:
                return f"{posixpath.basename(word)} {flag}"
    return None


def _scan(text: str, depth: int) -> str | None:
    if depth > MAX_DEPTH:
        return _fallback(text)
    if "<<" in text:
        text, documents = git_guard._heredocs(text)
        for header, body in documents:
            # a here-document is commands only when it feeds a shell, code when an interpreter
            words = [posixpath.basename(w).lower() for c in git_guard.split_words(header)[0] for w in c]
            if any(word in git_guard.SHELLS for word in words):
                found = _scan(body, depth + 1)
            elif any(git_guard.INTERPRETERS.match(word) for word in words):
                found = _code(body, depth + 1)
            else:
                found = None
            if found:
                return found
    commands, nested = git_guard.split_words(text)
    for snippet in nested:
        found = _scan(snippet, depth + 1)
        if found:
            return found
    for words in commands:
        found = _command(words, depth)
        if found:
            return found
    return None


def _code(code: str, depth: int) -> str | None:
    """Inline interpreter code: shell commands in its string literals, or the agent and the flag
    together as literal words (``subprocess.run(["claude", "--yolo"])``)."""
    for literal in re.findall(r"""(['"])((?:\\.|(?!\1)[^\\\n]){1,2000})\1""", code[:git_guard.SCAN_LIMIT]):
        found = _scan(literal[1], depth + 1)
        if found:
            return found
    words = re.findall(r"""['"]([^'"\n]{1,300})['"]""", code[:git_guard.SCAN_LIMIT])
    for index, word in enumerate(words):
        family = _program(word)
        if family:
            flag = bypass_flag(family, words[index + 1:])
            if flag:
                return f"{posixpath.basename(word)} {flag}"
    return None


def _command(words: list[str], depth: int) -> str | None:
    index = 0
    while index < len(words):
        word = words[index]
        if _ASSIGN.match(word):
            index += 1
            continue
        base = posixpath.basename(word).lower()
        family = _program(word)
        if family:
            flag = bypass_flag(family, words[index + 1:])
            return f"{base if base in AGENTS else family} {flag}" if flag else None
        rest = words[index + 1:]
        if base in git_guard.SHELLS or base == "eval":
            if base == "eval":
                return _scan(" ".join(rest), depth + 1)
            for position, item in enumerate(rest):
                if item.startswith("-") and not item.startswith("--") and "c" in item[1:]:
                    return _scan(rest[position + 1], depth + 1) if position + 1 < len(rest) else None
                if not item.startswith("-"):
                    return None  # a script file is not read
            return None
        if git_guard.INTERPRETERS.match(base):
            for code in git_guard.inline_code(base, rest):
                found = _code(code, depth + 1)
                if found:
                    return found
            return None
        if base in ("pnpm", "yarn", "bun") and rest[:1] in (["dlx"], ["x"]):
            index += 2
            continue
        if base == "pipx" and rest[:1] == ["run"]:
            index += 2
            continue
        if base not in WRAPPERS:
            return None  # another program: its words are data (grep, echo, man, cat ...)
        index += 1
        values, positional = WRAPPERS[base], _POSITIONAL.get(base, 0)
        while index < len(words):
            item = words[index]
            if item == "--":
                index += 1
                break
            if base == "env" and _ASSIGN.match(item):
                index += 1
                continue
            if item.startswith("-") and len(item) > 1:
                name = item.split("=", 1)[0]
                if name in _COMMAND_OPTS.get(base, ()):
                    value = item.split("=", 1)[1] if "=" in item else (
                        words[index + 1] if index + 1 < len(words) else "")
                    found = _scan(value, depth + 1)
                    if found:
                        return found
                    index += 1 if "=" in item else 2
                    continue
                index += 2 if item in values else 1
                continue
            if positional:
                positional -= 1
                index += 1
                continue
            break
    return None
