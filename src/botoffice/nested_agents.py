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

import os
import posixpath
import re
from dataclasses import dataclass, field
from itertools import islice

from . import git_guard

PRESET = "nested-agent"
LABEL = "Nested agent with permissions bypassed"
MAX_DEPTH = 6
LONG_COMMAND = 32 * 1024  # above this a command is not parsed: see _coarse
SCRIPT_BYTES = 256 * 1024  # a script file read at check time (text only)
SCRIPT_DEPTH = 2  # script -> script -> script is followed this far

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
    "nice": {"-n", "--adjustment"}, "chrt": set(), "taskset": {"-c", "--cpu-list"},
    "flock": {"-w", "--timeout", "-E", "--conflict-exit-code"}, "watch": {"-n", "--interval"},
    "parallel": {"-j", "--jobs", "-S", "--sshlogin", "-a", "--arg-file"}, "script": {"-c", "--command", "-t", "-I", "-O", "-B", "-T"}, "ionice": {"-c", "--class", "-n", "--classdata", "-p", "--pid"},
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
_POSITIONAL = {"timeout": 1, "chrt": 1, "taskset": 1, "flock": 1}
# option words whose value is itself a shell command
_COMMAND_OPTS = {"env": {"-S", "--split-string"}, "npx": {"-c", "--call"}, "flock": {"-c", "--command"},
                 "script": {"-c", "--command"}}
# programs whose words are data: a name and a flag after them are not a launch
DATA = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "man", "cat", "less", "more", "head",
        "tail", "wc", "ls", "stat", "file", "diff", "cmp", "sort", "uniq", "cut", "tr", "tee", "touch",
        "mkdir", "rm", "cp", "mv", "ln", "chmod", "chown", "which", "type", "whereis", "git", "gh", "test", "[",
        "[[", "read", "pwd", "cd", "sed", "jq", "yq", "base64", "sha256sum", "md5sum", "curl", "wget", "true",
        "false", "sleep", "date", "kill", "pkill", "pgrep", "apropos", "info", "help", "export", "unset",
        "set", "apt", "apt-get", "dnf", "yum", "apk", "brew", "pip", "pip3", "ssh-agent", "ssh-add"}
_STRIP_VARS = re.compile(r"^(?:PATH|BOT_OFFICE_\w*|GIT_CONFIG\w*)$")

_SYNTAX = {"{", "(", "function", "then", "do", "else", "elif", "if", "while", "until", "!"}
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


@dataclass
class _Ctx:
    cwd: str
    scripts: int = 0  # script files being followed (SCRIPT_DEPTH)
    seen: set = field(default_factory=set)
    written: dict = field(default_factory=dict)  # path -> text written to it earlier in this command
    stripped: str = ""  # an enclosing env -i / env -u: a nested plain launch is stripped too


# the strong bypass tokens, anywhere in a text (a command too long to parse)
_BYPASS_TEXT = re.compile(
    r"--dangerously-skip-permissions|--allow-dangerously-skip-permissions|bypassPermissions|"
    r"--dangerously-bypass-approvals-and-sandbox|--yolo\b|--full-auto|danger-full-access|"
    r"approval_policy\s*=\s*[\"']?never|(?:-a|--ask-for-approval)[\s=]+[\"']?never|"
    r"--approval-mode[\s=]+[\"']?yolo|--yes-always|(?<![\w-])(?:--yes|--force|-y)(?![\w-])", re.I)
_NAME_TEXT = re.compile(r"(?<![\w.@/-])(?:" + "|".join(re.escape(n) for n in AGENTS)
                        + r"|@anthropic-ai/claude-code|@openai/codex|@google/gemini-cli|aider-chat)(?![\w-])", re.I)
# interpreter code that clears or rewrites its environment, and code that starts a program
_ENV_CLEAR = re.compile(
    r"environ\s*\.\s*(?:clear|pop|update|setdefault)\b|environ\s*=|del\s+os\.environ|\bputenv\b|\bclearenv\b|"
    r"\benv\s*[:=]\s*(?:\{|dict\(|None|\[)|process\.env\s*=|delete\s+process\.env|\bENV\s*=|undef\s+%ENV|"
    r"%ENV\s*=|\bENV\.(?:clear|delete|replace)|exec(?:ve|le|vpe|lpe)\b|\bos\.exec\w*|\benv\s+-i\b|env_clear|"
    r"clearenv|\benv\s*=\s*\{\}", re.I)
_SPAWN = re.compile(r"exec\w*|spawn\w*|system|popen|subprocess|\bPopen\b|check_output|check_call|\.run\b|\bcall\b|"
                    r"\bsh\b|\bshell\b|execFile|`", re.I)


def bypass_launch(command: str, cwd: str | None = None) -> str | None:
    """A short description of the first nested agent CLI *command* starts with its permission checks
    switched off (``claude --permission-mode bypassPermissions``) or with the guard environment
    stripped (``env -i claude``), else None. *cwd* resolves the script files it runs."""
    if not command:
        return None
    if len(command) > LONG_COMMAND:
        return _coarse(command)
    ctx = _Ctx(cwd or os.getcwd())
    try:
        return _scan(command[:git_guard.SCAN_LIMIT], 0, ctx)
    except (RecursionError, ValueError, IndexError):
        return _fallback(command)


def _coarse(text: str) -> str | None:
    """Too long to parse: it fails closed. An agent CLI name and a bypass flag anywhere in the text
    (padding, comments and all) count as a launch; a card asks the owner, who can read the command."""
    flag = _BYPASS_TEXT.search(text)
    name = _NAME_TEXT.search(text)
    if flag and name:
        return f"{name.group(0)} {flag.group(0)} (command too long to parse)"
    return None


def _fallback(text: str) -> str | None:
    """Text too deep or odd to parse: an agent name and a bypass flag as words anywhere in it."""
    return _tokens(re.findall(r"[^\s'\"`;&|()]+", text[:git_guard.SCAN_LIMIT]))


def _tokens(words: list[str]) -> str | None:
    """An agent CLI name followed, later in *words*, by one of its bypass flags."""
    for index, word in enumerate(words):
        family = _program(word)
        if family:
            flag = bypass_flag(family, words[index + 1:])
            if flag:
                return f"{posixpath.basename(word)} {flag}"
    return None


_ALIAS = re.compile(r"""\balias\s+(?:-\w+\s+)*[\w.:-]+=(?:'([^']*)'|"([^"]*)"|(\S+))""")
_FUNCTION = re.compile(r"(?<![\w.:-])(?:function\s+)?[\w.:-]{1,64}\s*\(\s*\)\s*\{(.{0,4000}?)\}", re.S)
_WRITE = re.compile(r"""(?<![\w.-])(?:echo|printf)\s+(?:-\w+\s+)*(['"])((?:(?!\1).){0,2000})\1\s*>>?\s*([^\s;&|<>]+)""", re.S)
_MATCHES = 50  # of each kind looked at in one text (bounded work)
_REDIRECT = re.compile(r">>?\s*([^\s;&|<>]+)")


def _scan(text: str, depth: int, ctx: _Ctx) -> str | None:
    if depth > MAX_DEPTH:
        return _fallback(text)
    for alias in islice(_ALIAS.finditer(text), _MATCHES):
        found = _scan(next(group for group in alias.groups() if group is not None), depth + 1, ctx)
        if found:  # an alias whose body launches the agent
            return found
    for function in islice(_FUNCTION.finditer(text), _MATCHES):
        found = _scan(function.group(1), depth + 1, ctx)
        if found:
            return found
    for written in islice(_WRITE.finditer(text), _MATCHES) if ">" in text else ():
        ctx.written[written.group(3)] = written.group(2).replace("\\n", "\n")
    if "<<" in text:
        text, documents = git_guard._heredocs(text)
        for header, body in documents:
            target = _REDIRECT.search(header)
            if target:
                ctx.written[target.group(1)] = body
            # a here-document is commands only when it feeds a shell, code when an interpreter
            words = [posixpath.basename(w).lower() for c in git_guard.split_words(header)[0] for w in c]
            if any(word in git_guard.SHELLS for word in words):
                found = _scan(body, depth + 1, ctx)
            elif any(git_guard.INTERPRETERS.match(word) for word in words):
                found = _code(body, depth + 1)
            else:
                found = None
            if found:
                return found
    commands, nested = git_guard.split_words(text)
    for snippet in nested:
        found = _scan(snippet, depth + 1, ctx)
        if found:
            return found
    for words in commands:
        found = _command(words, depth, ctx)
        if found:
            return found
    return None


def _code(code: str, depth: int, ctx: _Ctx | None = None) -> str | None:
    """Inline interpreter code: shell commands in its string literals, or the agent and the flag
    together as literal words (``subprocess.run(["claude", "--yolo"])``); and code that clears or
    rewrites its environment and starts an agent CLI (the child loses the guard environment)."""
    ctx = ctx or _Ctx(os.getcwd())
    text = code[:git_guard.SCAN_LIMIT]
    for literal in re.findall(r"""(['"])((?:\\.|(?!\1)[^\\\n]){1,2000})\1""", text):
        found = _scan(literal[1], depth + 1, ctx)
        if found:
            return found
    words = re.findall(r"""['"]([^'"\n]{1,300})['"]""", text)
    found = _tokens(words)
    if found:
        return found
    if _ENV_CLEAR.search(text) and _SPAWN.search(text):
        for word in re.findall(r"[^\s'\"`;&|()\[\],{}=]+", text):
            family = _program(word)
            if family and posixpath.basename(word).lower() != "agent":
                return f"{posixpath.basename(word)} (code rewrites its environment and starts an agent CLI)"
    return None


def _script(path: str, ctx: _Ctx, depth: int, hint: str = "") -> str | None:
    """The nested-agent check of the script file *path* (a text file up to SCRIPT_BYTES), or None.
    *hint* is the interpreter's name; without one the shebang says whether it is shell or code."""
    if not path or path.startswith("-") or "$" in path or "`" in path or len(path) > 4096:
        return None
    full = os.path.normpath(os.path.join(ctx.cwd, os.path.expanduser(path)))
    body = ctx.written.get(path)
    if body is None:
        if ctx.scripts >= SCRIPT_DEPTH or full in ctx.seen:
            return None
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > SCRIPT_BYTES:
                return None
            with open(full, "rb") as handle:
                raw = handle.read(SCRIPT_BYTES + 1)
        except OSError:
            return None
        if len(raw) > SCRIPT_BYTES or b"\0" in raw[:8192]:
            return None  # too big, or a binary: not followed
        body = raw.decode("utf-8", "replace")
        ctx.seen.add(full)
    ctx.scripts += 1
    try:
        first = body.split("\n", 1)[0] if body.startswith("#!") else ""
        code = git_guard.INTERPRETERS.match(posixpath.basename(hint)) if hint else re.search(
            r"python|node|perl|ruby|php|lua|deno|bun", first)
        found = _code(body, depth + 1, ctx) if code else _scan(body, depth + 1, ctx)
        return f"{path}: {found}" if found else None
    finally:
        ctx.scripts -= 1


def _nested(text: str, depth: int, ctx: _Ctx, stripped: str) -> str | None:
    """*text* is a command a shell runs under the environment change *stripped* describes."""
    before, ctx.stripped = ctx.stripped, stripped
    try:
        return _scan(text, depth + 1, ctx)
    finally:
        ctx.stripped = before


def _command(words: list[str], depth: int, ctx: _Ctx) -> str | None:
    start = index = 0
    stripped = ctx.stripped  # why the child will not see the guard environment (env -i, env -u PATH ...)
    while index < len(words):
        word = words[index]
        if _ASSIGN.match(word):
            name, _, value = word.partition("=")
            if _STRIP_VARS.match(name) and not (name == "PATH" and "PATH" in value):
                stripped = stripped or f"{name} rewritten"
            index += 1
            continue
        base = posixpath.basename(word).lower()
        if word.endswith("()") or word in _SYNTAX:
            index += 1  # a function header or a shell keyword: the words after it are the command
            continue
        family = _program(word)
        if family:
            flag = bypass_flag(family, words[index + 1:])
            name = base if base in AGENTS else family
            if flag:
                return f"{name} {flag}"
            return f"{name} ({stripped})" if stripped else None
        rest = words[index + 1:]
        if base == "busybox" and rest[:1] in (["sh"], ["ash"]):
            index += 1
            continue
        if base in git_guard.SHELLS or base == "eval" or base in (".", "source"):
            if base == "eval":
                return _nested(" ".join(rest), depth, ctx, stripped)
            if base in (".", "source"):
                return _script(rest[0], ctx, depth) if rest else None
            for position, item in enumerate(rest):
                if item.startswith("-") and not item.startswith("--") and "c" in item[1:]:
                    return _nested(rest[position + 1], depth, ctx, stripped) if position + 1 < len(rest) else None
                if not item.startswith("-"):
                    return _script(item, ctx, depth)
            return None
        if git_guard.INTERPRETERS.match(base):
            for code in git_guard.inline_code(base, rest):
                found = _code(code, depth + 1, ctx)
                if found:
                    return found
            for item in [item for item in rest if not item.startswith("-")][:3]:
                found = _script(item, ctx, depth, base)
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
            if base in DATA:
                return None  # grep, echo, man, cat ...: its words are data
            if "/" in word:
                found = _script(word, ctx, depth)  # ./run.sh, /tmp/a: a file the shell runs
                if found:
                    return found
            # an unknown program or wrapper: an agent CLI and a bypass flag later in its words
            found = _tokens(words[start:])
            if found:
                return found
            for item in words[index:]:
                if any(ch.isspace() for ch in item):
                    found = _scan(item, depth + 1, ctx)
                    if found:
                        return found
            return None
        index += 1
        values, positional = WRAPPERS[base], _POSITIONAL.get(base, 0)
        while index < len(words):
            item = words[index]
            if item == "--":
                index += 1
                break
            if base == "env" and _ASSIGN.match(item):
                name = item.split("=", 1)[0]
                if _STRIP_VARS.match(name) and not (name == "PATH" and "PATH" in item):
                    stripped = stripped or f"env {name}= rewrites the guard environment"
                index += 1
                continue
            if base == "env" and item == "-":
                stripped = stripped or "env - clears the guard environment"
                index += 1
                continue
            if base == "env" and (item in ("--ignore-environment") or (
                    item.startswith("-") and not item.startswith("--") and "i" in item[1:].split("=")[0]
                    and "S" not in item)):
                stripped = stripped or "env -i clears the guard environment"
            if base == "env" and item in ("-u", "--unset") or (base == "env" and item.startswith("--unset=")):
                target = (words[index + 1] if index + 1 < len(words) else "") if item in ("-u", "--unset") \
                    else item.split("=", 1)[1]
                if _STRIP_VARS.match(target):
                    stripped = stripped or f"env -u {target} drops the guard environment"
            if base == "exec" and item == "-c":
                stripped = stripped or "exec -c clears the guard environment"
            if item.startswith("-") and len(item) > 1:
                name = item.split("=", 1)[0]
                cluster = base == "script" and not item.startswith("--") and "c" in item[1:]
                if name in _COMMAND_OPTS.get(base, ()) or cluster:
                    value = item.split("=", 1)[1] if "=" in item else (
                        words[index + 1] if index + 1 < len(words) else "")
                    found = _scan(value, depth + 1, ctx)
                    if found:
                        return found
                    index += 1 if "=" in item else 2
                    continue
                if base == "taskset" and name in ("-c", "--cpu-list"):
                    positional = 0
                index += 2 if item in values else 1
                continue
            if positional:
                positional -= 1
                index += 1
                continue
            break
    return None
