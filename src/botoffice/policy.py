"""0.12.42 the approval policy (Grok-Bot style): one place that says what waits for the owner's card.

The owner, Nov 26 2026: "I only ask for approval for things like send emails or irreversible outbound
actions that are risky ... or other costly or highly risky actions ... commit should never be
blocked". Bots keep full CLI access; this module judges what they do, by the commands that would run
(never the prose around them: here-document bodies written to files, echo/printf text, grep
patterns, commit messages, report text and bot-to-bot messages are data).

Order in rules.Rules.evaluate (strictest first): the secrets tripwire (never) > never rules > the git
guard (git_guard.py: default-branch pushes, merges, force-push, deletes outside the bot's scope, repo
settings, and since 0.12.42 any push to a public or unverified GitHub repo) > the send class (email,
texts, DMs) > THIS POLICY (``judge``) > the owner's own ask rules > the sudo auto-review > allow rules
> approval classes > the strict profile (watcher runs).

ASK (``ASK_CLASSES``): GitHub posts people read (issue comments and issues anywhere; PR comments,
reviews and edits on public or other people's repos), messages to people through Grok (`desk grok send`
other than a result/FYI report), HTTP writes to other hosts, remote shells (ssh/scp/rsync to a host),
publishing packages/images/releases/gists, deploys and service control (unless a saved allow rule
covers that exact command), desktop and printer control, connector (MCP) writes, deleting or moving
credentials, writes outside the bot's scope (the Bot-Office live checkout, other bots' work dirs,
shell profiles, ~/.ssh, systemd, /etc), git state changes in the live checkout, system administration
(shutdown, user accounts; sudo goes to the sudo review), and nested agent CLIs run outside the work
roots.

ALLOW: everything else, notably reads of any kind (gh list/view/api GET, git fetch/pull/clone, curl
GET), local edits, git commit/merge/rebase/cherry-pick in worktrees, feature pushes to private repos,
PR workflow writes on the owner's own private repos, nested claude -p / codex exec runs in work dirs,
build-artefact cleanup and bot-to-bot messages. When one of those would have asked under the old E8
rules (``legacy_asks``), rules.py writes it to the append-only ``policy_audit`` table (GET
/v1/policy/audit), so the owner can see what Bot-Office now does on its own.

A tripwire, not a sandbox (a bot with full CLI access, as the same Unix user, can get around it).
"""
from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field

from . import git_guard

VERSION = "0.12.42"

# key -> (label, what it covers); every key is also the card's preset
ASK_CLASSES: dict[str, tuple[str, str]] = {
    "post": ("Posting on GitHub", "issue comments and new issues on any repo; PR comments, reviews and edits on "
                                  "public or other people's repos; releases, gists, labels, new repos"),
    "message": ("Messaging people", "desk grok send other than a result/FYI report (questions and blockers to the owner)"),
    "http-write": ("Web writes", "curl/wget/httpie POST, PUT, PATCH, DELETE or uploads to a host other than this one"),
    "remote": ("Other computers", "ssh, scp, sftp and rsync to another machine"),
    "publish": ("Publishing", "npm/pypi/cargo/gem publish, docker/podman push, GitHub releases and gists"),
    "deploy": ("Deploys and services", "deploy scripts, railway up, kubectl apply, terraform apply, gh workflow run, "
                                       "systemctl start/stop/restart, systemd-run, crontab edits (a saved allow rule "
                                       "for the exact command lets a routine deploy run)"),
    "devices": ("Desktop and printers", "desk click/type/key and 3D printer control"),
    "connector": ("Connector writes", "an MCP tool that changes something (reads never ask)"),
    "credentials": ("Credentials", "deleting, moving or overwriting a credential or key file"),
    "scope": ("Outside the bot's scope", "writes to the Bot-Office live checkout, another bot's work dir, ~/.ssh, "
                                         "shell profiles, systemd units or /etc"),
    "live-checkout": ("Live checkout", "git commands that change the Bot-Office live checkout"),
    "system": ("System administration", "shutdown, reboot, user accounts, disks"),
    "nested-agent": ("Nested agent outside work dirs", "claude -p / codex exec with permission checks off, run "
                                                       "outside the bot's workspace and the work roots"),
}
# the owner's own allow rules (master token only; a bot cannot write one) may let these run: the owner's routine
# deploy rules, a review run he allowed. Never the Bot-Office safeguards: credentials, scope, the live checkout.
LOCKED_CLASSES = frozenset({"credentials", "scope", "live-checkout"})
OVERRIDABLE = frozenset(ASK_CLASSES) - LOCKED_CLASSES
# a sudo command in these classes goes to the sudo auto-review (sudo_review.py) instead of a policy card
SUDO_ROUTED = frozenset({"deploy", "system"})

# the GitHub owners whose private repos count as the owner's own (PR workflow writes need no card)
# the GitHub owners whose private repos count as the owner's own: the controller token's login (owner_lookup,
# set by the API at start-up) plus BOT_OFFICE_OWN_GITHUB_OWNERS="a,b"
OWN_OWNERS_DEFAULT: tuple[str, ...] = ()
owner_lookup = None  # () -> the GitHub login of the controller's token, or None


def own_owners() -> tuple[str, ...]:
    named = [item.strip() for item in os.environ.get("BOT_OFFICE_OWN_GITHUB_OWNERS", "").split(",") if item.strip()]
    try:
        login = owner_lookup() if owner_lookup is not None else None
    except Exception:  # noqa: BLE001
        login = None
    return tuple(dict.fromkeys([*OWN_OWNERS_DEFAULT, *named, *([login] if isinstance(login, str) and login else [])]))
# shared work roots: a bot may write and clean up there (other bots' named dirs excepted)
WORK_ROOTS_DEFAULT = ("~/bo-work", "~/bot-office-work", "~/bot-office-reports", "/tmp")
# per-bot dirs inside these roots: one bot's dir is out of another bot's scope
BOT_DIR_ROOTS = ("~/bot-office-work", "~/bot-office-reports")
_GUARDED_HOME = (".ssh", ".gnupg", ".aws", ".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile",
                 ".bash_logout", ".gitconfig", ".config/systemd", ".config/bot-office", ".config/gh",
                 ".config/git", ".local/bin", ".grok/bin")
_GUARDED_SYSTEM = ("/etc", "/usr", "/boot", "/opt", "/var/lib", "/var/spool", "/root", "/srv")

LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0")
HTTP_PROGRAMS = git_guard.HTTP_PROGRAMS | {"httpie"}
AGENT_CLIS = frozenset({"claude", "codex", "opencode", "gemini", "cursor-agent", "aider", "grok", "qwen", "goose"})
RM_LIKE = git_guard.RM_PROGRAMS | {"truncate"}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_VAR = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_CRED_NAME = re.compile(r"(?i)(?:^|[._-])(?:creds?|credentials?|secrets?|tokens?|passw(?:or)?d|apikey|api[_-]key)"
                        r"(?:$|[._-])|\.(?:pem|key|p12|pfx|cred|kdbx)$|^id_(?:rsa|dsa|ecdsa|ed25519)|^\.netrc$|^\.pgpass$"
                        r"|^auth\.json$|^\.git-credentials$|^hosts\.yml$")
_CRED_DIRS = ("/.ssh/", "/.gnupg/", "/.aws/", "/.config/gh/", "/.docker/", "/.kube/")
_CACHE_ARTEFACTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
                              ".coverage", "htmlcov", "node_modules", "dist", "build", ".tox", ".nox", ".eggs",
                              ".cache", ".parcel-cache", ".next", ".turbo"})


# --- context ----------------------------------------------------------------------------------------

@dataclass
class Scope:
    """Who acts and where: the job's workspace, the CLI's directory, the bot, and the roots it may
    write in. ``own_dir(path)`` says whether a data/workspaces/<job> directory is this bot's (rules.py
    asks the DB); ``bot_dirs`` maps another bot's dir names in BOT_DIR_ROOTS (they stay out of scope)."""

    workspace: str = ""
    cwd: str | None = None
    bot_id: str | None = None
    bot_name: str = ""
    live_root: str = ""
    other_bot_names: tuple[str, ...] = ()
    work_roots: tuple[str, ...] = WORK_ROOTS_DEFAULT
    own_owners: tuple[str, ...] = OWN_OWNERS_DEFAULT
    own_job_dir: object = None  # callable(job_id: str) -> bool, or None (only the current workspace is own)
    home: str = field(default_factory=lambda: os.path.expanduser("~"))

    def expand(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            return self.home + path[1:]
        return path

    @property
    def roots(self) -> tuple[str, ...]:
        """Directories the bot may write in (the job workspace first)."""
        roots = [self.workspace] if self.workspace else []
        roots += [posixpath.normpath(self.expand(root)) for root in self.work_roots]
        return tuple(root for root in roots if root)

    def own_slugs(self) -> set[str]:
        return slugs(self.bot_name)

    def excluded(self) -> tuple[str, ...]:
        """Other bots' named dirs under BOT_DIR_ROOTS (a dir both could claim stays the bot's own)."""
        mine = self.own_slugs()
        theirs = set().union(*(slugs(name) for name in self.other_bot_names)) - mine if self.other_bot_names else set()
        out = []
        for root in BOT_DIR_ROOTS:
            base = self.expand(root)
            for name in sorted(theirs):
                out.append(posixpath.join(base, name))
        return tuple(out)


def slugs(name: str) -> set[str]:
    """Dir names a bot's work and report dirs go by: "MPG Data Pipeline Engineer (BO)" ->
    mpg-data-pipeline-engineer-bo, mpg-data-pipeline-engineer, mpg-dpe."""
    if not name:
        return set()
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return set()
    out = {"-".join(words)}
    core = words[:-1] if words[-1] == "bo" and len(words) > 1 else words
    out.add("-".join(core))
    if len(core) > 2:
        out.add(core[0] + "-" + "".join(word[0] for word in core[1:]))
    return out


# --- reading a command as the commands that would run --------------------------------------------

@dataclass
class Cmd:
    words: list[str]
    cwd: str | None  # where it runs (None: not known)


@dataclass
class Parsed:
    commands: list[Cmd]
    code: list[str]  # inline interpreter code (python -c, a here-document fed to python ...)
    unreadable: bool = False
    # `> file` targets (outside quotes): (target, the directory its command runs in, known?)
    redirects: list[tuple[str, str | None, bool]] = field(default_factory=list)
    cwds: list[str | None] = field(default_factory=list)  # every directory a part runs in
    sudo: bool = False  # a part runs through sudo / doas / pkexec


_RUNNER_VALUE_OPTS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"}, "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"}, "xargs": {"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"},
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-U", "-D"}, "flock": {"-w", "-E"}, "exec": {"-a"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S"}, "setsid": set(), "nohup": set(), "time": set(),
    "command": set(), "chronic": set(), "caffeinate": set(), "doas": {"-u"}, "runuser": {"-u", "-g"},
}
def redirect_targets(text: str) -> tuple[list[tuple[int, str]], int]:
    """The operands of `>`, `>>`, `>|`, `&>` and `N>` outside quotes, dequoted (`> "$HOME/x"` is
    $HOME/x), each with the number of the simple command it belongs to (commands split where
    git_guard.split_words splits them: ; & | newline ( ), not inside $( )), and how many non-empty
    commands there are. Not `>&N` (a descriptor), not `>(...)` (process substitution)."""
    out: list[tuple[int, str]] = []
    i, n, quote, depth = 0, len(text), "", 0
    segment, content = 0, False
    while i < n:
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "'\"":
            quote, content = c, True
            i += 1
            continue
        if c == "\\":
            content = True
            i += 2
            continue
        if text.startswith("$(", i):
            depth, content = depth + 1, True
            i += 2
            continue
        if c == ")" and depth:
            depth -= 1
            i += 1
            continue
        if depth:
            i += 1
            continue
        if c in ";&|\n()":
            if content:
                segment += 1
            content = False
            i += 1
            continue
        if c != ">":
            if c not in " \t\r":
                content = True
            i += 1
            continue
        content = True
        i += 1
        if i < n and text[i] in ">|":
            i += 1
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] in "&(":
            i += 1
            continue
        word: list[str] = []
        while i < n and text[i] not in " \t\n;&|<>()":
            c = text[i]
            if c in "'\"":
                stop = text.find(c, i + 1)
                stop = n if stop < 0 else stop
                word.append(text[i + 1:stop])
                i = stop + 1
            elif c == "\\" and i + 1 < n:
                word.append(text[i + 1])
                i += 2
            else:
                word.append(c)
                i += 1
        if word:
            out.append((segment, "".join(word)))
    return out, segment + (1 if content else 0)


def _expand(word: str, variables: dict[str, str], scope: Scope) -> str:
    found = _VAR.match(word)
    if found:
        name = found.group(1) or found.group(2)
        if name in variables:
            word = variables[name] + word[found.end():]
        elif name in ("HOME",):
            word = scope.home + word[found.end():]
        elif name in ("PWD",) and variables.get("\0cwd"):
            word = variables["\0cwd"] + word[found.end():]
    return scope.expand(word)


def parse(command: str, scope: Scope, depth: int = 0) -> Parsed:
    """The simple commands *command* runs, with runners (timeout, nohup, env, xargs, sudo ...) and
    `sh -c '...'` unwrapped, literal variables (`J=/x; rm $J/y`) and `~` expanded, and `cd` followed.
    Here-document bodies are data unless fed to a shell (then they are commands) or an interpreter
    (then they are code)."""
    out = Parsed([], [])
    if depth > 4 or len(command) > git_guard.SCAN_LIMIT:
        out.unreadable = True
        return out
    try:
        text, documents = git_guard._heredocs(command) if "<<" in command else (command, [])
        for header, body in documents:
            bases = [git_guard._base(w) for part in git_guard.split_words(header)[0] for w in part]
            if any(b in git_guard.SHELLS for b in bases):
                inner = parse(body, scope, depth + 1)
                _merge(out, inner)
            elif any(git_guard.INTERPRETERS.match(b) for b in bases):
                out.code.append(body)
        found_redirects, segments = redirect_targets(text)
        commands, nested = git_guard.split_words(text)
    except (RecursionError, ValueError, IndexError):
        out.unreadable = True
        return out
    for snippet in nested:
        _merge(out, parse(snippet, scope, depth + 1))
    variables: dict[str, str] = {}
    cwd = scope.cwd
    out.cwds.append(cwd)
    matched = segments == len(commands)  # else a redirect is judged against every directory seen
    if not matched:
        out.redirects += [(target, None, False) for _seg, target in found_redirects]
    for index, words in enumerate(commands):
        variables["\0cwd"] = cwd or ""
        if matched:
            out.redirects += [(_expand(t, variables, scope) if "$" in t or t.startswith("~") else t, cwd, True)
                              for seg, t in found_redirects if seg == index]
        # a line of only assignments sets shell variables (a prefix assignment does not change $X in its own line)
        if words and all(_ASSIGN.match(w) for w in words):
            for word in words:
                name, value = _ASSIGN.match(word).groups()
                if "$" in value or "`" in value:
                    variables.pop(name, None)
                    expanded = _expand(value, variables, scope)
                    if "$" not in expanded and "`" not in expanded:
                        variables[name] = expanded
                else:
                    variables[name] = scope.expand(value)
            continue
        words = [_expand(w, variables, scope) if "$" in w or w.startswith("~") else w for w in words]
        cwd = _unwrap(words, cwd, scope, out, depth)
        out.cwds.append(cwd)
    return out


def _merge(out: Parsed, inner: Parsed) -> None:
    out.commands += inner.commands
    out.code += inner.code
    out.redirects += inner.redirects
    out.cwds += inner.cwds
    out.unreadable |= inner.unreadable
    out.sudo |= inner.sudo


def _unwrap(words: list[str], cwd: str | None, scope: Scope, out: Parsed, depth: int) -> str | None:
    i = 0
    while i < len(words) and _ASSIGN.match(words[i]):
        i += 1
    words = words[i:]
    while words:
        program = git_guard._base(words[0])
        if program in _RUNNER_VALUE_OPTS:
            if program in ("sudo", "doas"):
                out.sudo = True
            values = _RUNNER_VALUE_OPTS[program]
            j = 1
            while j < len(words) and (words[j].startswith("-") or (program == "env" and _ASSIGN.match(words[j]))):
                if program == "env" and words[j] in ("-C", "--chdir") and j + 1 < len(words):
                    cwd = _join(cwd, words[j + 1])
                split = None
                if program == "env" and words[j] in ("-S", "--split-string") and j + 1 < len(words):
                    split, skip = words[j + 1], 2
                elif program == "env" and words[j].startswith("--split-string="):
                    split, skip = words[j].split("=", 1)[1], 1
                elif program == "env" and words[j].startswith("-S") and len(words[j]) > 2:
                    split, skip = words[j][2:], 1
                if split is not None:  # env -S "cmd args": the string is the command (env splits it)
                    try:
                        inner = git_guard.split_words(split)[0]
                    except (RecursionError, ValueError, IndexError):
                        out.unreadable = True
                        return cwd
                    words = [*(inner[0] if inner else []), *words[j + skip:]]
                    break
                j += 2 if words[j] in values else 1
            else:
                if program == "timeout" and j < len(words):
                    j += 1  # the duration
                if program == "flock" and j < len(words):
                    j += 1  # the lock file
                words = words[j:]
            continue
        break
    if not words:
        return cwd
    program = git_guard._base(words[0])
    if program in ("pkexec", "su", "run0"):
        out.sudo = True
    if program in ("cd", "pushd", "chdir"):
        target = next((w for w in words[1:] if not w.startswith("-")), "")
        if not target:
            return scope.home
        if "$" in target or "`" in target:
            return None
        return _join(cwd, target)
    if program in git_guard.SHELLS and len(words) > 1:
        for position, word in enumerate(words[1:], 1):
            if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                if position + 1 < len(words):
                    _merge(out, parse(words[position + 1], Scope(**{**scope.__dict__, "cwd": cwd}), depth + 1))
                return cwd
            if not word.startswith("-"):
                break
    if program == "eval":
        _merge(out, parse(" ".join(words[1:]), Scope(**{**scope.__dict__, "cwd": cwd}), depth + 1))
        return cwd
    if git_guard.INTERPRETERS.match(program):
        out.code += git_guard.inline_code(program, words[1:])
    out.commands.append(Cmd(words, cwd))
    return cwd


def _join(cwd: str | None, target: str) -> str | None:
    if "$" in target or "`" in target:
        return None
    if posixpath.isabs(target):
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(cwd, target)) if cwd else None


def _path(cmd_cwd: str | None, path: str, scope: Scope) -> str | None:
    """*path* as an absolute, normalised path, or None when it is not known (a variable, a substitution)."""
    path = scope.expand(path)
    if not path or "$" in path or "`" in path:
        return None
    if not posixpath.isabs(path):
        if not cmd_cwd:
            return None
        path = posixpath.join(cmd_cwd, path)
    return posixpath.normpath(path)


def _within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/")


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


# --- verdicts ---------------------------------------------------------------------------------------

@dataclass
class Verdict:
    effect: str  # "ask" | "allow"
    key: str  # an ASK_CLASSES key, or the allow reason's key
    reason: str
    keys: frozenset = frozenset()  # every class a part of the action hit (ask)

    @property
    def all_keys(self) -> frozenset:
        return self.keys or frozenset({self.key})

    @property
    def overridable(self) -> bool:
        """An allow rule may open it only when every class it hit may be opened."""
        return self.effect == "ask" and self.all_keys <= OVERRIDABLE


def _ask(key: str, reason: str) -> Verdict:
    return Verdict("ask", key, reason)


def judge(tool: str, command: str, paths: list[str], data: dict, scope: Scope, first_party: bool = False) -> Verdict:
    """The policy's verdict for one tool call (rules.py has already applied secrets, never rules, the
    git guard and the send class)."""
    lowered = tool.lower()
    if lowered.startswith("mcp") and not first_party:
        found = _connector(tool)
        if found:
            return found
    if lowered in WRITE_TOOLS:
        for path in paths:
            found = _write_target(scope.cwd, path, scope, f"{tool} {path}")
            if found:
                return found
    if not command:
        return Verdict("allow", "no-command", "")
    parsed = parse(command, scope)
    if parsed.unreadable:
        return _ask("scope", "a command too long or too nested to check")
    allow_notes: list[str] = []
    findings: list[Verdict] = []
    for target, where, known in dict.fromkeys(parsed.redirects):
        for place in ([where] if known else dict.fromkeys(parsed.cwds or [scope.cwd])):
            found = _write_target(place, target, scope, f"writing {target}")
            if found:
                findings.append(found)
    for cmd in parsed.commands:
        found = _command(cmd, scope, allow_notes)
        if found:
            findings.append(found)
    for code in parsed.code:
        found = _code(code, scope)
        if found:
            findings.append(found)
    bypass = _nested_agent(command, parsed, scope)
    if bypass:
        findings.append(bypass)
    if findings:
        # every part is judged; the strictest finding names the card: a locked class (no allow rule opens
        # it) before one no rule may open, before the rest, so a harmless-looking first part cannot mask it
        findings.sort(key=lambda v: (v.key not in LOCKED_CLASSES, v.key in OVERRIDABLE))
        first = findings[0]
        return Verdict("ask", first.key, first.reason, keys=frozenset(v.key for v in findings))
    return Verdict("allow", allow_notes[0] if allow_notes else "ordinary", "; ".join(dict.fromkeys(allow_notes)))


WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit", "str_replace_editor", "apply_patch",
                         "create_file", "write_file", "edit_file", "replace"})


def _command(cmd: Cmd, scope: Scope, notes: list[str]) -> Verdict | None:
    words = cmd.words
    program = git_guard._base(words[0])
    args = words[1:]
    if program in ("echo", "printf", "true", ":", "test", "["):
        return None
    if program in ("bot-msg", "bot-task", "bot-office-say", "bot-office-attach"):
        notes.append("bot-to-bot")
        return None
    if program == "desk" or (git_guard.INTERPRETERS.match(program) and args and git_guard._base(args[0]) == "desk"):
        return _desk(args[1:] if program != "desk" else args, notes)
    if program == "git":
        return _git(args, cmd, scope, notes)
    if program == "gh":
        return _gh(args, cmd, scope, notes)
    if program in HTTP_PROGRAMS:
        for target in _http_outputs(program, args):
            found = _write_target(cmd.cwd, target, scope, f"{program} writing {target}")
            if found:
                return found
        return _http(program, args, notes)
    if program in ("ssh", "scp", "sftp", "mosh", "sshpass", "telnet"):
        return _ask("remote", f"{program} to another machine")
    if program in ("nc", "ncat", "netcat", "socat"):
        hosts = [a for a in args if not a.startswith("-") and not a.isdigit()]
        if any(h.split(":")[0].lower() not in LOOPBACK and not h.startswith(("STDIO", "STDIN", "-")) for h in hosts):
            return _ask("remote", f"{program} to another machine")
    if program == "rsync":
        if any(re.match(r"^[^/\s]*[^/\s:]:", a) or a.startswith("rsync://") for a in args if not a.startswith("-")):
            return _ask("remote", "rsync to another machine")
    found = _publish(program, args) or _deploy(program, args) or _system(program, args)
    if found:
        return found
    if program in ("prusa-link", "prusalink", "octoprint", "octoprint-cli", "octocmd"):
        return _ask("devices", f"{program} (3D printer control)")
    if program in RM_LIKE or program in ("mv", "cp", "install", "ln", "tee", "touch", "sed", "chmod", "chown",
                                          "dd", "mkdir", "rsync", "perl", "chgrp"):
        return _file_op(program, args, cmd, scope, notes)
    return None


def _desk(args: list[str], notes: list[str]) -> Verdict | None:
    positional = [a for a in args if not a.startswith("-")]
    sub = positional[0] if positional else ""
    if sub in ("click", "type", "key", "drag", "scroll", "paste"):
        return _ask("devices", f"desk {sub} (controls a desktop)")
    if sub == "grok" and len(positional) > 1 and positional[1] == "send":
        kind = _option(args, "--kind")
        if kind is None:
            notes.append("desk grok send without --kind (refused by desk)")
            return None
        if kind.lower() in ("result", "fyi", "ack") and _option(args, "--to") and _option(args, "--text") is not None \
                and _option(args, "--from") is None:
            notes.append("grok report (result/fyi)")
            return None
        return _ask("message", f"desk grok send --kind {kind or '(none)'} (a message for the owner)")
    notes.append("bot-to-bot" if sub in ("message", "say") else "desk read")
    return None


def _option(args: list[str], name: str) -> str | None:
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


# git subcommands that change a checkout's files, index or branch
_GIT_STATE = frozenset({"checkout", "switch", "reset", "merge", "pull", "rebase", "cherry-pick", "revert", "stash",
                        "clean", "restore", "am", "apply", "commit", "rm", "mv", "add"})


def _git(args: list[str], cmd: Cmd, scope: Scope, notes: list[str]) -> Verdict | None:
    directory = cmd.cwd
    explicit: list[str | None] = []  # --git-dir / --work-tree: the repo git really changes
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            directory = _join(directory, scope.expand(args[i + 1]))
            i += 2
            continue
        name, eq, value = args[i].partition("=")
        if name in ("--git-dir", "--work-tree"):
            if not eq and i + 1 < len(args):
                value, i = args[i + 1], i + 1
            explicit.append(_join(directory, scope.expand(value)) if value else None)
        i += 2 if args[i] in ("-c", "--namespace") else 1
    sub = args[i] if i < len(args) else ""
    if sub in ("send-email", "imap-send", "request-pull"):
        return _ask("message", f"git {sub} (email to people)")
    if sub in _GIT_STATE and scope.live_root:
        live = _real(scope.live_root)
        for place in explicit:
            if place is None or _within(_real(place), live):
                return _ask("live-checkout", f"git {sub} with --git-dir/--work-tree in or near the live checkout")
        top = git_guard._git_out(directory, "rev-parse", "--show-toplevel") if directory else None
        if top and _real(top) == live:
            return _ask("live-checkout", f"git {sub} in the Bot-Office live checkout ({scope.live_root})")
    if sub == "push":
        notes.append("git push (the git guard judged the branch and the repo)")
    elif sub in ("commit", "merge", "rebase", "cherry-pick", "revert", "am"):
        notes.append(f"local git {sub}")
    elif sub in ("fetch", "pull", "clone", "ls-remote", "remote"):
        notes.append("network read (git)")
    return None


# gh issue/pr subcommands that post or change what people see
_GH_ISSUE_WRITES = frozenset({"comment", "create", "edit", "close", "reopen", "lock", "unlock", "delete", "transfer",
                              "pin", "unpin", "develop"})
_GH_PR_FLOW = frozenset({"create", "edit", "ready", "review", "comment"})
_GH_PR_WRITES = _GH_PR_FLOW | {"close", "reopen", "lock", "unlock"}
_GH_REPO_FLAGS = ("-R", "--repo")


def _gh_repo(args: list[str], cmd: Cmd) -> tuple[str, str] | None:
    named = _option(args, "-R") or _option(args, "--repo")
    if named:
        text = named.strip()
        if "://" not in text and text.count("/") == 1:
            text = "github.com/" + text
        hub = git_guard.github_repo(git_guard.normalise_remote(text if "://" in text else "https://" + text))
        return hub
    if not cmd.cwd:
        return None
    url = git_guard.remote_url(cmd.cwd, "origin")
    return git_guard.github_repo(git_guard.normalise_remote(url)) if url else None


def _own_private(repo: tuple[str, str] | None, scope: Scope) -> bool:
    if repo is None or repo[0].lower() not in {owner.lower() for owner in scope.own_owners}:
        return False
    return git_guard.repo_visibility(*repo) == "private"


def _gh(args: list[str], cmd: Cmd, scope: Scope, notes: list[str]) -> Verdict | None:
    positional = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg in _GH_REPO_FLAGS or arg in ("--jq", "-q", "--json", "-t", "--template", "-H", "--header", "-f", "-F",
                                            "--field", "--raw-field", "--input", "-X", "--method", "-b", "--body",
                                            "--body-file", "--title", "-l", "--label", "--limit", "-L", "--state",
                                            "-s", "--search", "--base", "-B", "--head", "--author", "-a", "-A"):
            skip = True
            continue
        if not arg.startswith("-"):
            positional.append(arg)
    low = [p.lower() for p in positional]
    group = low[0] if low else ""
    action = low[1] if len(low) > 1 else ""
    repo = _gh_repo(args, cmd)
    named = f"{repo[0]}/{repo[1]}" if repo else "a repo not named"
    if group == "issue" and action in _GH_ISSUE_WRITES:
        return _ask("post", f"gh issue {action} on {named} (people read issues)")
    if group == "pr" and action in _GH_PR_WRITES:
        if action in _GH_PR_FLOW and _own_private(repo, scope):
            notes.append(f"gh pr {action} on the owner's private repo {named}")
            return None
        return _ask("post", f"gh pr {action} on {named} ({'public or not verified private' if repo else 'repo unknown'})")
    if group == "release" and action in ("create", "upload", "edit", "delete", "delete-asset"):
        return _ask("publish", f"gh release {action}")
    if group == "gist" and action in ("create", "edit", "delete", "clone", "rename"):
        return _ask("publish", f"gh gist {action}") if action != "clone" else None
    if group == "repo" and action in ("create", "fork", "edit", "delete", "rename", "archive", "unarchive",
                                       "set-default", "deploy-key", "autolink", "sync"):
        return _ask("post", f"gh repo {action}")
    if group in ("label", "ruleset", "secret", "variable", "ssh-key", "gpg-key") and action in (
            "create", "edit", "delete", "clone", "set", "add", "remove"):
        return _ask("post", f"gh {group} {action} (repo or account settings)")
    if group == "workflow" and action in ("run", "enable", "disable"):
        return _ask("deploy", f"gh workflow {action}")
    if group == "api":
        return _gh_api(args, positional[1:], cmd, scope, notes, repo)
    if group and group not in git_guard.GH_GROUPS:
        value = git_guard._gh_alias(group)  # a gh alias: judged as what it expands to
        if value:
            rest = args[args.index(positional[0]) + 1:]
            if value.startswith("!"):
                for inner in parse(value[1:] + " " + " ".join(rest), scope, depth=3).commands:
                    found = _command(inner, scope, notes)
                    if found:
                        return found
                return None
            return _gh([*value.split(), *rest], cmd, scope, notes)
    notes.append("network read (gh)" if group in ("pr", "issue", "run", "repo", "release", "search", "status",
                                                   "workflow", "label", "browse", "cache") else f"gh {group}")
    return None


_API_PR_FLOW = re.compile(r"^/?repos/([^/]+)/([^/]+)/(?:pulls(?:/\d+(?:/(?:reviews(?:/\d+/events)?|comments(?:/\d+/replies)?"
                          r"|requested_reviewers))?)?|issues/\d+/(?:labels|assignees)|statuses/[0-9a-f]+|check-runs(?:/\d+)?)$")


def _gh_api(args: list[str], positional: list[str], cmd: Cmd, scope: Scope, notes: list[str],
            repo: tuple[str, str] | None) -> Verdict | None:
    method = (_option(args, "-X") or _option(args, "--method") or next(
        (a[2:].lstrip("=") for a in args if a.startswith("-X") and len(a) > 2), "")).upper()
    fields = any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=",
                 "--input=")) or (a[:2] in ("-f", "-F") and len(a) > 2) for a in args)
    endpoint = positional[0] if positional else ""
    if endpoint == "graphql":
        text = " ".join(args)
        if re.search(r"\bmutation\b", text):
            return _ask("post", "gh api graphql mutation")
        notes.append("network read (gh api graphql)")
        return None
    if not method:
        method = "POST" if fields else "GET"
    if method in ("GET", "HEAD"):
        notes.append("network read (gh api GET)")
        return None
    path = endpoint.split("?", 1)[0]
    if repo and "{owner}" in path:
        path = path.replace("{owner}", repo[0]).replace("{repo}", repo[1])
    found = _API_PR_FLOW.match(path)
    if found and method in ("POST", "PATCH", "PUT") and _own_private((found.group(1), found.group(2)), scope):
        notes.append(f"gh api {method} {path} (PR workflow on the owner's private repo)")
        return None
    return _ask("post", f"gh api {method} {endpoint or '(no endpoint)'}")


_HTTP_BODY = ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json", "-F",
              "--form", "--form-string", "-T", "--upload-file", "--post-data", "--post-file", "--body-data",
              "--body-file")


_HTTP_VALUE_OPTS = frozenset({"-H", "--header", "-A", "--user-agent", "-o", "--output", "-u", "--user", "-b", "--cookie",
                              "-c", "--cookie-jar", "-e", "--referer", "-m", "--max-time", "--connect-timeout", "-w",
                              "--write-out", "-x", "--proxy", "-r", "--range", "--retry", "--retry-delay", "-K",
                              "--config", "-E", "--cert", "--key", "--cacert", "--resolve", "--limit-rate", "-U",
                              "--output-document", "-P", "--directory-prefix", "-t", "--tries", "-T", "--timeout",
                              "--password", "--http-user", "--http-password", "-a", "--auth", "--unix-socket"})


_CURL_VALUE_LETTERS = frozenset("AbcCdDeEFHKmoPQrTuUwxXyYz")
_WGET_VALUE_SHORT = frozenset({"-O", "-o", "-a", "-P", "-t", "-T", "-U", "-e", "-i", "-B", "-w", "-Q", "-l", "-A", "-R",
                               "-D", "-I", "-X"})
_HTTPIE = ("http", "https", "xh", "xhs", "httpie")


def _http(program: str, args: list[str], notes: list[str]) -> Verdict | None:
    """curl/wget/httpie: a write (a method other than GET/HEAD, a body, an upload) to a host other than
    this machine asks. Each `--next` transfer of curl is judged on its own."""
    transfers, current = [], []
    for arg in args:
        if program not in ("wget", *_HTTPIE) and arg in ("--next", "-:"):
            transfers.append(current)
            current = []
        else:
            current.append(arg)
    transfers.append(current)
    for transfer in transfers:
        found = _http_transfer(program, transfer)
        if found:
            return found
    notes.append(f"network read or local call ({program})")
    return None


def _http_transfer(program: str, args: list[str]) -> Verdict | None:
    method, body, urls = "", False, []
    i = 0
    while i < len(args):
        arg = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in ("--request", "--method"):
                method = (value if eq else nxt).upper()
                i += 1 if eq else 2
                continue
            if name in _HTTP_BODY or name in ("--upload-file", "--post-data", "--post-file", "--body-data",
                                              "--body-file", "--form", "--form-string", "--json"):
                body = True
                i += 1 if eq else 2
                continue
            if name in _HTTP_VALUE_OPTS or name in ("--url",):
                if name == "--url":
                    urls.append(value if eq else nxt)
                i += 1 if eq else 2
                continue
            i += 1
            continue
        if arg.startswith("-") and len(arg) > 1 and program == "wget":
            i += 2 if arg in _WGET_VALUE_SHORT else 1
            continue
        if arg.startswith("-") and len(arg) > 1 and program not in _HTTPIE:
            # curl short options, fused or not: a value letter takes the rest of the word or the next word
            letters, k, took = arg[1:], 0, False
            while k < len(letters):
                letter = letters[k]
                if letter in _CURL_VALUE_LETTERS:
                    value = letters[k + 1:] or nxt
                    took = not letters[k + 1:]
                    if letter == "X":
                        method = value.upper()
                    elif letter in "dF":
                        body = True
                    elif letter == "T":
                        body, method = True, method or "PUT"
                    break
                k += 1
            i += 2 if took else 1
            continue
        if arg.startswith("-") and len(arg) > 1:  # httpie / xh options
            if arg in ("-f", "--form", "--multipart", "--raw"):
                body = True
            i += 2 if arg in ("-a", "--auth", "-A", "--auth-type", "-o", "--output", "--session", "--verify",
                              "--cert", "--cert-key", "--proxy", "--timeout") else 1
            continue
        if program in _HTTPIE and re.fullmatch(r"GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS", arg, re.IGNORECASE) and not urls:
            method = arg.upper()
        else:
            urls.append(arg)
        i += 1
    if program in _HTTPIE:
        if not method and any(re.match(r"^[\w.-]+(?:=(?!=)|:=|@)", u) for u in urls[1:]):
            method = "POST"
        urls = urls[:1]
    writes = method not in ("", "GET", "HEAD", "OPTIONS") or (body and method not in ("GET", "HEAD"))
    if not writes:
        return None
    hosts = [_host(u) for u in urls]
    if not hosts or any(host is None or host not in LOOPBACK for host in hosts):
        named = ", ".join(h or "an unknown host" for h in hosts) or "an unknown host"
        return _ask("http-write", f"{program} {method or 'POST'} to {named}")
    return None


def _http_outputs(program: str, args: list[str]) -> list[str]:
    """Files and directories a download writes (-o/-O FILE, --output, --output-document, -P DIR ...)."""
    out = []
    long_opts = ("--output", "--output-document", "--directory-prefix", "--output-dir", "--dump-header", "--cookie-jar",
                 "--trace", "--trace-ascii", "--stderr", "--output-file", "--append-output")
    short = {"wget": ("-O", "-o", "-a", "-P"), "curl": ("-o", "-D", "-c")}.get(program, ("-o",))
    for index, arg in enumerate(args):
        nxt = args[index + 1] if index + 1 < len(args) else ""
        name, eq, value = arg.partition("=")
        if name in long_opts:
            out.append(value if eq else nxt)
        elif arg in short:
            out.append(nxt)
        elif program == "curl" and len(arg) > 2 and arg[0] == "-" and arg[1] != "-":
            letters = arg[1:]
            for k, letter in enumerate(letters):
                if letter in _CURL_VALUE_LETTERS:
                    if letter in "oDc":
                        out.append(letters[k + 1:] or nxt)
                    break
        elif program == "wget" and len(arg) > 2 and arg[:2] in short:
            out.append(arg[2:])
    return [o for o in out if o and o != "-"]


def _host(url: str) -> str | None:
    if "$" in url or "`" in url:
        return None
    found = re.match(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]*@)?(\[[^\]]*\]|[^:/\s?#]+)", url, re.IGNORECASE)
    if not found:
        return None
    return found.group(1).lower()


def _publish(program: str, args: list[str]) -> Verdict | None:
    # the verb anywhere among the words (global options such as --registry URL come before it)
    verbs = {a.lower() for a in args if not a.startswith("-")}
    first = next((v for v in ("publish", "upload", "push", "copy") if v in verbs), args[0].lower() if args else "")
    if program in ("npm", "pnpm", "yarn", "bun") and first == "publish":
        return _ask("publish", f"{program} publish")
    if program == "twine" and first == "upload" or program in ("cargo", "poetry", "uv", "flit", "hatch") and first == "publish":
        return _ask("publish", f"{program} {first}")
    if program == "gem" and first == "push" or program in ("docker", "podman", "buildah", "skopeo") and first in ("push", "copy"):
        return _ask("publish", f"{program} {first}")
    if program == "vsce" and first == "publish":
        return _ask("publish", "vsce publish")
    return None


_SYSTEMCTL_READS = frozenset({"status", "show", "cat", "is-active", "is-enabled", "is-failed", "list-units",
                              "list-unit-files", "list-timers", "list-sockets", "list-dependencies", "help", "--version",
                              "list-jobs", "get-default", "show-environment"})
_DEPLOY_SCRIPT = re.compile(r"^(?:deploy|go-live|release)(?:[._-][\w.-]*)?$", re.IGNORECASE)


_DEPLOY_VERBS = {
    "railway": ("up", "redeploy", "down", "delete", "deploy"),
    "kubectl": ("apply", "rollout", "delete", "scale", "edit", "patch", "replace", "create", "set", "drain", "cordon"),
    "oc": ("apply", "rollout", "delete", "scale", "edit", "patch", "replace", "create", "set"),
    "terraform": ("apply", "destroy", "import"), "tofu": ("apply", "destroy", "import"), "pulumi": ("up", "destroy"),
    "helm": ("install", "upgrade", "uninstall", "rollback", "delete"),
    "netlify": ("deploy",), "fly": ("deploy", "destroy", "scale"), "flyctl": ("deploy", "destroy", "scale"),
    "wrangler": ("deploy", "publish"), "firebase": ("deploy",), "heroku": ("releases:rollback", "ps:scale", "destroy"),
}
_SERVICE_WRITES = frozenset({"start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart",
                             "enable", "disable", "reenable", "mask", "unmask", "kill", "isolate", "daemon-reload",
                             "daemon-reexec", "edit", "set-property", "reset-failed", "link", "revert", "preset",
                             "set-default", "reboot", "poweroff", "halt", "suspend", "hibernate", "kexec", "switch-root",
                             "terminate-session", "kill-session", "kill-user", "terminate-user", "enable-linger",
                             "disable-linger", "set-environment", "unset-environment", "import-environment", "freeze",
                             "thaw", "clean", "bind", "mount-image"})


def _deploy(program: str, args: list[str]) -> Verdict | None:
    positional = [a for a in args if not a.startswith("-")]
    first = positional[0].lower() if positional else ""
    lowered = {p.lower() for p in positional}
    if program in _DEPLOY_VERBS:
        verb = next((v for v in _DEPLOY_VERBS[program] if v in lowered), None)
        if verb:
            return _ask("deploy", f"{program} {verb}")
    if _DEPLOY_SCRIPT.match(program):
        return _ask("deploy", f"{program} (a deploy script)")
    if program in git_guard.SHELLS and positional and _DEPLOY_SCRIPT.match(git_guard._base(positional[0])):
        return _ask("deploy", f"{program} {positional[0]} (a deploy script)")
    if program in ("make", "just", "task") and any(re.match(r"^deploy", p) for p in positional):
        return _ask("deploy", f"{program} deploy")
    if program in ("npm", "pnpm", "yarn") and first == "run" and len(positional) > 1 and positional[1].startswith("deploy"):
        return _ask("deploy", f"{program} run {positional[1]}")
    if program == "railway" and first in ("up", "redeploy", "down", "delete", "deploy", "variables", "variable") and not (
            first in ("variables", "variable") and not any(a in ("--set", "set", "delete") for a in args)):
        return _ask("deploy", f"railway {first}")
    if program in ("kubectl", "oc") and first in ("apply", "rollout", "delete", "scale", "edit", "patch", "replace",
                                                  "create", "set", "drain", "cordon"):
        return _ask("deploy", f"{program} {first}")
    if program in ("terraform", "tofu", "pulumi") and first in ("apply", "destroy", "up", "import"):
        return _ask("deploy", f"{program} {first}")
    if program == "helm" and first in ("install", "upgrade", "uninstall", "rollback", "delete"):
        return _ask("deploy", f"helm {first}")
    if program == "vercel" and ("--prod" in args or first in ("deploy", "promote", "rollback", "remove")):
        return _ask("deploy", "vercel deploy")
    if program in ("netlify", "fly", "flyctl", "wrangler", "firebase", "heroku") and first in (
            "deploy", "publish", "releases:rollback", "scale", "destroy"):
        return _ask("deploy", f"{program} {first}")
    if program == "ansible-playbook":
        return _ask("deploy", "ansible-playbook")
    if program in ("systemctl", "loginctl") and ((first and first not in _SYSTEMCTL_READS) or lowered & _SERVICE_WRITES):
        verb = next(iter(sorted(lowered & _SERVICE_WRITES)), first)
        return _ask("deploy", f"{program} {verb} (service control)")
    if program == "systemd-run":
        return _ask("deploy", "systemd-run (starts a service or a timer)")
    if program == "crontab" and not ({"-l"} >= set(args) and args):
        return _ask("deploy", "crontab (changes scheduled jobs)")
    if program == "at" or program == "batch":
        return _ask("deploy", f"{program} (schedules a command)")
    return None


def _system(program: str, args: list[str]) -> Verdict | None:
    if program in ("shutdown", "reboot", "poweroff", "halt", "visudo", "useradd", "userdel", "usermod", "groupadd",
                   "groupdel", "passwd", "chsh", "chpasswd", "fdisk", "parted", "wipefs") \
            or program.startswith("mkfs"):
        return _ask("system", f"{program} (system administration)")
    return None


def _file_op(program: str, args: list[str], cmd: Cmd, scope: Scope, notes: list[str]) -> Verdict | None:
    explicit: list[str] = []
    positional = []
    only = False
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if only or not arg.startswith("-") or arg == "-":
            positional.append(arg)
        elif arg == "--":
            only = True
        elif program in ("cp", "mv", "install", "ln") and arg in ("-t", "--target-directory"):
            if index + 1 < len(args):
                explicit.append(args[index + 1])
            skip = True
        elif program in ("cp", "mv", "install", "ln") and arg.startswith("--target-directory="):
            explicit.append(arg.split("=", 1)[1])
        elif program in ("cp", "mv", "install", "ln") and arg.startswith("-t") and len(arg) > 2:
            explicit.append(arg[2:])
        elif program == "sed" and arg in ("-e", "-f", "--expression", "--file"):
            skip = True
    if program == "sed":
        if not any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
            return None
        if not any(a in ("-e", "-f", "--expression", "--file") or a.startswith(("--expression=", "-e")) for a in args):
            positional = positional[1:]  # the script
    if program == "dd":
        positional = [a[3:] for a in args if a.startswith("of=")]
    if program == "perl":
        if not any(a.startswith("-i") or (a.startswith("-") and "i" in a[1:] and not a.startswith("--")) for a in args):
            return None
        positional = [a for a in positional[1:]]
    if program in ("chmod", "chown", "chgrp"):
        positional = positional[1:]
    targets = positional
    if program in ("cp", "install", "ln", "rsync") and len(positional) > 1:
        targets = positional[-1:]
    if explicit:  # -t DIR: every positional is a source, DIR the destination (wherever the option is)
        targets = [*explicit, *(positional if program == "mv" else [])]
    if program == "mkdir":
        targets = positional
    for target in targets:
        if program in RM_LIKE or program == "mv":
            full = _path(cmd.cwd, target, scope)
            if full and _CRED_NAME.search(posixpath.basename(full.rstrip("/"))) or full and any(
                    d in full + "/" for d in _CRED_DIRS):
                return _ask("credentials", f"{program} {target} (a credential or key file)")
            if program in RM_LIKE and full and posixpath.basename(full.rstrip("/")) in _CACHE_ARTEFACTS:
                notes.append("build-artefact cleanup")
        found = _write_target(cmd.cwd, target, scope, f"{program} {target}")
        if found:
            return found
    if program == "mv":  # moving a file away from a guarded place changes it too
        for source in positional[:-1]:
            found = _write_target(cmd.cwd, source, scope, f"mv {source}")
            if found:
                return found
    return None


def _write_target(cwd: str | None, target: str, scope: Scope, what: str) -> Verdict | None:
    """A write to *target*: ask when it is outside the bot's scope (see the module doc)."""
    if not target or target in ("/dev/null", "/dev/stdout", "/dev/stderr", "-") or target.startswith("/dev/fd/"):
        return None
    full = _path(cwd, target, scope)
    if full is None:
        return None  # not known here; deletes of unknown paths are the git guard's (it asks)
    for candidate in dict.fromkeys((full, _real(full))):
        reason = _guarded(candidate, scope)
        if reason:
            return _ask("scope", f"{what}: {reason}")
    return None


def _guarded(path: str, scope: Scope) -> str | None:
    if scope.workspace and _within(path, scope.workspace):
        return None
    home = scope.home.rstrip("/")
    for name in _GUARDED_HOME:
        if _within(path, f"{home}/{name}"):
            return f"~/{name} is outside the bot's scope"
    if scope.live_root:
        live = posixpath.normpath(scope.live_root)
        if _within(path, live):
            workspaces = posixpath.join(live, "data", "workspaces")
            if _within(path, workspaces):
                rest = path[len(workspaces) + 1:].split("/", 1)[0]
                if rest and callable(scope.own_job_dir) and scope.own_job_dir(rest):
                    return None
                return "another job's workspace (data/workspaces) is outside the bot's scope"
            return "the Bot-Office live checkout is outside the bot's scope (use a worktree in ~/bo-work)"
    for excluded in scope.excluded():
        if _within(path, excluded):
            return f"{excluded.replace(home, '~', 1)} is another bot's dir"
    for name in _GUARDED_SYSTEM:
        if _within(path, name):
            return f"{name} is outside the bot's scope"
    return None


_CODE_HTTP_WRITE = re.compile(r"\brequests\.(?:post|put|patch|delete)\s*\(|\bhttpx\.(?:post|put|patch|delete)\s*\(|"
                              r"method\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]|\bfetch\s*\([^)]*method\s*:\s*['\"]"
                              r"(?:POST|PUT|PATCH|DELETE)|\.(?:post|put|patch|delete)\s*\(\s*['\"]https?://", re.I)
_CODE_URL = re.compile(r"https?://([^/\s'\"`:]+)", re.I)


def _code(code: str, scope: Scope) -> Verdict | None:
    """Inline interpreter code: an HTTP write to another host asks, and so do the shell commands it
    spells out as literals (subprocess.run(["gh", "issue", "comment", ...]))."""
    text = code[:git_guard.SCAN_LIMIT]
    if _CODE_HTTP_WRITE.search(text):
        hosts = [h.lower() for h in _CODE_URL.findall(text)]
        if not hosts or any(h not in LOOPBACK for h in hosts):
            return _ask("http-write", "inline code that sends an HTTP write")
    for literal in git_guard._literals(text):
        parsed = parse(literal, scope, depth=3)
        for cmd in parsed.commands:
            found = _command(cmd, scope, [])
            if found and found.key in ("post", "message", "http-write", "remote", "publish", "deploy", "credentials",
                                       "devices"):
                return found
    return None


def _nested_agent(command: str, parsed: Parsed, scope: Scope) -> Verdict | None:
    """An agent CLI with its permission checks off: allowed in the workspace and the work roots (where
    its own work stays), asked anywhere else (the live checkout, a directory not known here)."""
    from . import nested_agents
    found = nested_agents.bypass_launch(command, scope.cwd)
    if not found:
        return None
    places: list[str | None] = []
    for cmd in parsed.commands:
        program = git_guard._base(cmd.words[0])
        family = program in AGENT_CLIS or git_guard.SHELLS.__contains__(program) or program.endswith(".sh")
        if not family:
            continue
        places.append(cmd.cwd)
        for flag in ("-C", "--cd", "--cwd", "--add-dir", "--workdir"):
            value = _option(cmd.words, flag)
            if value is not None:
                places.append(_path(cmd.cwd, value, scope))
        if program.endswith(".sh") or program in git_guard.SHELLS:
            script = next((w for w in cmd.words[1:] if not w.startswith("-")), None) if program in git_guard.SHELLS else cmd.words[0]
            if script:
                places.append(_path(cmd.cwd, script, scope))
    if not places:
        places = [scope.cwd]
    for place in places:
        if place is None:
            return _ask("nested-agent", f"{found}, in a directory not known here")
        if _guarded(place, scope) or not any(_within(place, root) for root in scope.roots):
            return _ask("nested-agent", f"{found}, run in {place} (outside the workspace and the work roots)")
    return None


# --- word-level checks for the approval classes (approval_classes.py) -----------------------------

def programs(command: str) -> list[list[str]]:
    """The simple commands *command* runs (runners and `sh -c` unwrapped, here-document text set aside),
    for checks that must not match prose. A command that cannot be read: [["\0unreadable"]]."""
    parsed = parse(command, Scope(work_roots=()))
    if parsed.unreadable:
        return [["\0unreadable"]]
    return [cmd.words for cmd in parsed.commands]


def runs_sudo(command: str) -> bool:
    """A part of *command* runs through sudo, doas, pkexec or su (not the word in prose); a command
    that cannot be read counts."""
    parsed = parse(command, Scope(work_roots=()))
    return parsed.unreadable or parsed.sudo


def system_command(command: str) -> bool:
    """sudo, service control, scheduled jobs, accounts, shutdown, disks: as commands that run."""
    if runs_sudo(command):
        return True
    for words in programs(command):
        program = git_guard._base(words[0])
        if program == "\0unreadable":
            return True
        if _deploy(program, words[1:]) is not None and program in ("systemctl", "loginctl", "systemd-run", "crontab",
                                                                    "at", "batch"):
            return True
        if _system(program, words[1:]) is not None or program in ("pkexec", "doas"):
            return True
        if program == "su" and "-" in words[1:2]:
            return True
        if program == "podman" and (words[1:3] == ["system", "prune"] or words[1:3] == ["volume", "rm"]):
            return True
    return False


def release_command(command: str) -> bool:
    """Merging a PR, publishing a release or package, force or default-branch pushes: as commands."""
    for words in programs(command):
        program = git_guard._base(words[0])
        positional = [w for w in words[1:] if not w.startswith("-")]
        if program == "\0unreadable":
            return True
        if program == "gh" and positional[:2] in (["pr", "merge"], ["release", "create"], ["release", "upload"],
                                                   ["release", "edit"], ["release", "delete"]):
            return True
        if _publish(program, words[1:]) is not None:
            return True
        if program == "git" and "push" in positional[:1] + positional[1:2]:
            rest = words[words.index("push") + 1:] if "push" in words else []
            if any(w in ("-f", "--force", "--force-with-lease") or w.startswith("--force") or w.startswith("+")
                   or w.split(":")[-1] in ("main", "master") for w in rest):
                return True
    return False


def delete_outside(command: str) -> bool:
    """The "delete" approval class, as commands (never prose): deleting, moving away or overwriting
    (rm, shred, mv, dd of=, a `>` redirect) system files, shell profiles, ~/.ssh and the like, or a
    credential file. Deletes elsewhere outside the bot's scope are the git guard's (it knows the scope)."""
    scope = Scope(work_roots=())
    parsed = parse(command, scope)
    if parsed.unreadable:
        return True
    targets = [target for target, _where, _known in parsed.redirects]
    for cmd in parsed.commands:
        program = git_guard._base(cmd.words[0])
        if program == "dd":
            targets += [w[3:] for w in cmd.words[1:] if w.startswith("of=")]
        elif program in RM_LIKE or program in ("mv", "tee", "truncate"):
            targets += [w for w in cmd.words[1:] if not w.startswith("-")]
    for target in targets:
        full = _path(None, target, scope)
        if not full:
            continue
        if _CRED_NAME.search(posixpath.basename(full.rstrip("/"))) or any(d in full + "/" for d in _CRED_DIRS):
            return True
        home = scope.home.rstrip("/")
        if any(_within(full, f"{home}/{name}") for name in _GUARDED_HOME) or any(
                _within(full, name) for name in _GUARDED_SYSTEM):
            return True
    return False




# --- connectors -------------------------------------------------------------------------------------

_READ_WORDS = frozenset({"get", "list", "read", "search", "fetch", "query", "find", "view", "show", "download",
                         "describe", "lookup", "whoami", "status", "metrics", "logs", "inspect", "check", "guide",
                         "docs", "count", "export", "preview", "diff", "stat", "info", "resolve"})
_WRITE_WORDS = frozenset({"create", "update", "delete", "set", "send", "post", "put", "patch", "write", "upload",
                          "deploy", "apply", "remove", "trash", "untrash", "move", "copy", "share", "label", "unlabel",
                          "mark", "add", "edit", "rename", "run", "execute", "trigger", "restart", "redeploy", "accept",
                          "cancel", "request", "reply", "forward", "archive", "publish", "merge", "close", "batch",
                          "modify", "insert", "replace", "approve", "invite", "pay", "purchase", "order", "book"})


def _connector(tool: str) -> Verdict | None:
    action = tool.split("__")[-1] if "__" in tool else re.sub(r"^mcp[_\-.:/]*", "", tool, flags=re.IGNORECASE)
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", action)) if w]
    if set(words) & {"variable", "variables", "secret", "secrets", "token", "tokens", "credential", "credentials",
                     "password", "passwords", "env"}:
        return _ask("connector", f"connector tool {tool} reads or changes secrets")
    if set(words) & {"draft", "drafts"} and set(words) & {"create", "update", "save", "new"} and not set(words) & {
            "send", "delete", "trash"}:
        return None  # a draft is not sent: nothing leaves, and it can be thrown away
    if words and not set(words) & _WRITE_WORDS and (words[0] in _READ_WORDS or set(words) & {"status", "metrics",
                                                                                              "logs", "whoami"}):
        return None
    return _ask("connector", f"connector tool {tool} may change something")


# --- what the old E8 rules asked for (the audit and the replay) -------------------------------------

LEGACY_E8_SHELL = (
    ("E8 http-write", r"(?i)\b(curl|wget|http|https|httpie|xh)\b.*(-X\s*['\"]?(POST|PUT|PATCH|DELETE)|--data|--json|"
                      r"--post|--upload|--form|\s-[dFT]\s)"),
    ("E8 mail/send", r"(?i)\b(mail|mailx|sendmail|msmtp|mutt|swaks)\b|\b(slack|telegram|twilio)\b.*\b(send|post|message)\b"),
    ("E8 git push", r"\bgit\s+(-\S+\s+)*push\b"),
    ("E8 merge", r"\bgh\s+pr\s+merge\b|\bgit\s+(-\S+\s+)*merge\b|\bgh\s+api\b.*\bmerge"),
    ("E8 deploy word", r"(?i)\bdeploy(?!(?<=[a-z] deploy)(?<!run deploy)(?<!make deploy)(?<!just deploy)(?<!task deploy)"
                       r"(?:ments?|ed|ing) [^\s-])|\brailway\s+up\b|\bkubectl\s+(apply|rollout|delete)\b|\bterraform\s+apply\b"
                       r"|\bvercel\b.*--prod|\bnetlify\s+deploy|\bfly(ctl)?\s+deploy|\bsystemctl\s+(restart|reload|start)\b"
                       r"|\bansible-playbook\b"),
    ("system class word", r"\bsudo\b|\bpkexec\b|\bsu\s+-|\b(?:systemctl|loginctl|crontab|shutdown|reboot|mkfs\S*|visudo|"
                          r"useradd|userdel|usermod)\b"),
)
_LEGACY_RE = tuple((name, re.compile(pattern)) for name, pattern in LEGACY_E8_SHELL)
E8_NOTE_PREFIXES = ("E8 safety gate", "Nov 26 interim")


def legacy_asks(command: str, hits: list[str]) -> list[str]:
    """Why the pre-0.12.42 standard rules (E8 per-bot ask rules, the outbound preset ask, the regex
    approval classes) would have carded this action."""
    found = [f"preset {hit}" for hit in hits if hit in ("outbound", "nested-agent")]
    if command:
        text = command[:32 * 1024]
        found += [name for name, pattern in _LEGACY_RE if pattern.search(text)]
    return found


def describe() -> dict:
    """The policy as data (GET /v1/policy)."""
    return {"version": VERSION, "ask": {key: {"label": label, "covers": covers, "saved_rule_can_allow": key in OVERRIDABLE}
                                        for key, (label, covers) in ASK_CLASSES.items()},
            "always_ask_before_policy": ["secrets (never)", "never rules", "git guard: default-branch push, merge, "
                                         "force-push, branch/tag/repo deletes, deletes outside the bot's scope, repo "
                                         "settings, pushes to public or unverified GitHub repos",
                                         "send class: email, texts, DMs, chat to people"],
            "allow": ["reads of any kind (gh list/view/status/checks, gh api GET, git fetch/pull/clone/ls-remote, curl GET)",
                      "local edits in the workspace and work roots", "here-document text, echo text, grep patterns, "
                      "commit messages", "git commit, local merge/rebase/cherry-pick in worktrees",
                      "feature-branch pushes to private repos", "PR create/edit/review/comment on the owner's private repos",
                      "PR merges that pass the auto-merge check", "nested claude -p / codex exec in work roots",
                      "build-artefact cleanup (__pycache__, .pytest_cache, node_modules, dist ...)",
                      "bot-to-bot messages (bot-msg, desk message, desk grok send --kind result|fyi)",
                      "connector reads"],
            "work_roots": list(WORK_ROOTS_DEFAULT), "own_github_owners": list(own_owners())}
