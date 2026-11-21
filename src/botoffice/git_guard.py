"""0.12.4 git guard: actions that always stop on a card for the owner, in every run.

Pushing to the default branch, merging a PR, force-pushing, deleting a branch, tag or repo,
deleting files outside the bot's workspace and changing repo settings are classified here, by
command, and rules.py turns a hit into an approval card before any user rule, approval class,
bot setting, run kind or CLI access level can say "allow". Only a "never" rule is stricter.
Everything else a bot does (full CLI access) is untouched.

``classify`` reads the shell command as words (quotes removed, ``sh -c`` / ``eval`` / ``$( )`` /
backticks / env and runner prefixes unwrapped, scripts and interpreter code scanned) and answers
with a short reason, or None. It leans to asking: a false positive is a card, a false negative is
a push. Still a tripwire, not a sandbox: shell indirection it cannot see (a binary that pushes
by itself) is out of reach, which is why the PATH shims in bin/ cover the engines that have no
hook. Stdlib only: bin/bot-office-gate and the shims import it too.
"""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
from dataclasses import dataclass

DEFAULT_BRANCHES = ("main", "master", "trunk")
PRESET = "git-guard"
LABEL = "Protected action: default branch, merge, force-push, delete or repo settings"
SCAN_LIMIT = 256 * 1024
MAX_DEPTH = 6
MAX_COMMANDS = 3000
SCRIPT_LIMIT = 64 * 1024
GIT_TIMEOUT = 3

_BRANCH = "(?:" + "|".join(DEFAULT_BRANCHES) + ")"
# a ref that names the default branch: main, refs/heads/main, heads/main, refs/remotes/origin/main
_DEFAULT_REF = re.compile(rf"^(?:refs/(?:heads|remotes/[^/]+)/|heads/)?{_BRANCH}$", re.IGNORECASE)
# the same, or "<remote>/main" (a branch to delete, an upstream)
_DEFAULT_REMOTE_REF = re.compile(rf"^(?:refs/(?:heads|remotes/[^/]+)/|heads/|[^/:]+/)?{_BRANCH}$", re.IGNORECASE)
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_VARS = ("PWD", "WORKSPACE", "BOT_OFFICE_WORKSPACE")

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh", "busybox"}
INTERPRETERS = re.compile(r"^(?:python[\d.]*|pypy\d*|node(?:js)?|deno|bun|perl\d*|ruby|php\d*|lua[\d.]*|tclsh|"
                          r"awk|gawk|mawk|pwsh|powershell|osascript)$")
# programs that run the words after them, sometimes as one quoted string (ssh host 'git push ...')
RUNNERS = {"ssh", "sudo", "su", "doas", "runuser", "sg", "docker", "podman", "kubectl", "nsenter", "chroot",
           "script", "watch", "timeout", "nohup", "setsid", "flock", "parallel", "at", "batch", "systemd-run",
           "tmux", "screen", "xterm", "bwrap", "firejail", "unshare", "nice", "ionice", "time", "stdbuf", "env",
           "command", "builtin", "exec", "xargs", "find", "strace", "ltrace", "sshpass", "proot", "chronic"}
RM_PROGRAMS = {"rm", "unlink", "rmdir", "shred", "srm", "trash", "trash-put", "wipe"}
GIT_BUILTINS = frozenset("""add am archive bisect blame branch bundle cat-file checkout cherry cherry-pick citool
clean clone commit commit-graph config count-objects describe diff diff-files diff-index diff-tree difftool
fast-export fast-import fetch filter-branch fmt-merge-msg for-each-ref format-patch fsck gc gitk grep gui
hash-object help init interpret-trailers log ls-files ls-remote ls-tree maintenance merge merge-base mergetool mv
name-rev notes pack-objects prune pull push range-diff read-tree rebase reflog remote repack replace
request-pull reset restore rev-list rev-parse revert rm shortlog show show-branch show-ref sparse-checkout stash
status submodule subtree switch symbolic-ref tag update-index update-ref var verify-commit verify-tag version
whatchanged worktree write-tree lfs flow""".split())
GH_GROUPS = frozenset("""actions agent-task alias api attestation auth browse cache codespace completion config
copilot extension gist gpg-key help issue label licenses org pr preview project release repo ruleset run search
secret ssh-key status variable workflow version""".split())
# `git -C dir` style options with a separate value
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
_PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
_PUSH_RISKY_LONG = ("delete", "force", "force-with-lease", "force-if-includes", "mirror", "all", "prune")
_REPO_ADMIN = {"delete", "edit", "rename", "archive", "unarchive", "transfer", "sync", "deploy-key", "autolink"}
_GRAPHQL_MUTATION = re.compile(
    r"mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest|deleteRef|updateRefs?\b|deleteRepository|"
    r"deleteBranch|deleteTag|deleteRelease|deleteEnvironment|updateRepository|archiveRepository|"
    r"unarchiveRepository|transferRepository|createCommitOnBranch|"
    r"(?:create|update|delete)(?:BranchProtectionRule|RepositoryRuleset|RepositoryWebhook|DeployKey|Environment)",
    re.IGNORECASE)
_API_SAFE_WRITE = re.compile(r"^(?:repos/[^/]+/[^/]+/issues(?:/|$)|notifications|user/starred/|gists(?:/|$)|"
                             r"user/following/|user/subscriptions/)")
_API_SENSITIVE_POST = re.compile(
    r"^repos/[^/]+/[^/]+/(?:merges$|merge-upstream$|transfer$|rename$|rulesets|hooks|keys|pages|environments|"
    r"actions/|dependabot/|collaborators|invitations|branches/.+/(?:rename|protection)|git/refs/)")
_DYNAMIC = re.compile(r"[$`]")
_METHOD = re.compile(r"^(?:GET|HEAD|OPTIONS|POST|PUT|PATCH|DELETE)$", re.IGNORECASE)
_GITHUB_URL = re.compile(r"api\.github\.com|uploads\.github\.com|/api/v3(?:/|$)|GITHUB_API|GITHUB_GRAPHQL|"
                         r"(?:^|//|@)github\.com/(?:api|repos)/", re.IGNORECASE)
_PATCH_DELETE_FILE = re.compile(r"^\*\*\* Delete File:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class Ctx:
    """Where the command runs: *root* is the bot's workspace (the CLI's directory, if the hook says),
    *cwd* follows `cd` along the command; None once it is not known."""
    root: str | None = None
    cwd: str | None = None
    moved_out: bool = False  # without a root: a `cd` left for somewhere not known to be inside
    commands: int = 0


class _Hit(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _hit(reason: str):
    raise _Hit(reason)


# --- reading a shell command as words -------------------------------------------------------------

def _balanced(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the *closer* matching the *opener* before *start* (or the end of text)."""
    depth, i, quote = 1, start, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def split_words(text: str) -> tuple[list[list[str]], list[str]]:
    """(simple commands as word lists with quotes removed, nested snippets from $( ) and backticks).
    Separators: ; & | newline ( ) and redirects. A word that holds an expansion keeps its `$`."""
    commands: list[list[str]] = []
    nested: list[str] = []
    words: list[str] = []
    cur: list[str] = []
    have = False
    i, n = 0, len(text)

    def end_word():
        nonlocal cur, have
        if have:
            words.append("".join(cur))
        cur, have = [], False

    def end_command():
        nonlocal words
        end_word()
        if words:
            commands.append(words)
        words = []

    def substitution(i: int, quoted: bool) -> int:
        """At "$(": record the inner text, keep the raw text in the word; return the next index."""
        nonlocal have
        stop = _balanced(text, i + 2, "(", ")")
        nested.append(text[i + 2:stop - 1])
        cur.append(text[i:stop])
        have = True
        return stop

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                cur.append(text[i + 1])
                have = True
            i += 2
        elif c == "'":
            stop = text.find("'", i + 1)
            stop = n if stop < 0 else stop
            cur.append(text[i + 1:stop])
            have = True
            i = stop + 1
        elif c == '"':
            have = True
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    cur.append(text[i + 1])
                    i += 2
                elif text.startswith("$(", i):
                    i = substitution(i, True)
                elif text[i] == "`":
                    stop = text.find("`", i + 1)
                    stop = n if stop < 0 else stop
                    nested.append(text[i + 1:stop])
                    cur.append(text[i:stop + 1])
                    i = stop + 1
                else:
                    cur.append(text[i])
                    i += 1
            i += 1
        elif text.startswith("$(", i):
            i = substitution(i, False)
        elif c == "`":
            stop = text.find("`", i + 1)
            stop = n if stop < 0 else stop
            nested.append(text[i + 1:stop])
            cur.append(text[i:stop + 1])
            have = True
            i = stop + 1
        elif c == "$" and text.startswith("${", i):
            stop = _balanced(text, i + 2, "{", "}")
            cur.append(text[i:stop])
            have = True
            i = stop
        elif c in ";&|\n()":
            end_command()
            i += 1
        elif c in "<>":
            end_word()
            i += 1
        elif c in " \t\r":
            end_word()
            i += 1
        else:
            cur.append(c)
            have = True
            i += 1
    end_command()
    return commands, nested


def _base(word: str) -> str:
    return posixpath.basename(word).lower()


def _dynamic(word: str) -> bool:
    return bool(_DYNAMIC.search(word))


# --- the checks -----------------------------------------------------------------------------------

def scan_text(text: str, ctx: Ctx, depth: int = 0) -> None:
    """Raise _Hit if any command in shell *text* is guarded."""
    if depth > MAX_DEPTH:
        _hit("a command nested too deeply to check")
    commands, nested = split_words(text)
    for snippet in nested:
        scan_text(snippet, ctx, depth + 1)
    for words in commands:
        ctx.commands += 1
        if ctx.commands > MAX_COMMANDS:
            _hit("a command with too many parts to check")
        _scan_command(words, ctx, depth)
    if "<<" in text and _interpreter_in(commands):
        _raw_code(text)


def _interpreter_in(commands: list[list[str]]) -> bool:
    return any(INTERPRETERS.match(_base(word)) or _base(word) in SHELLS for words in commands for word in words)


def _scan_command(words: list[str], ctx: Ctx, depth: int) -> None:
    cursor = 0
    while cursor < len(words):
        word = words[cursor]
        if _ASSIGN.match(word) and "/" not in word.split("=", 1)[0]:
            cursor += 1
            continue
        break
    words = words[cursor:]
    for index, word in enumerate(words):
        program = _base(word)
        rest = words[index + 1:]
        if program in ("echo", "printf"):
            return  # prints its words; a substitution in them was scanned as a nested command
        if program == "git" or program.startswith("git-") and program[4:] in ("push", "send-pack", "http-push"):
            _git(rest if program == "git" else [program[4:], *rest], ctx, depth)
            return
        if program == "gh":
            _gh(rest, ctx, depth)
            return
        if program in ("curl", "wget", "http", "https", "xh", "xhs", "httpie", "wcurl"):
            _http(program, rest, ctx)
            return
        if program in RM_PROGRAMS or program == "gio" or program in ("find", "rsync", "mv", "apply_patch"):
            _delete(program, rest, ctx)
            if program != "find":
                return
        if program in ("cd", "pushd", "chdir") and rest:
            _cd(rest, ctx)
            return
        if program in SHELLS or program in ("eval", "source", "."):
            _shell(program, rest, ctx, depth)
            return
        if INTERPRETERS.match(program):
            _interpreter(rest, ctx)
            return
        if program in RUNNERS:
            for item in rest:
                if any(ch.isspace() for ch in item):
                    scan_text(item, ctx, depth + 1)
    return


def _cd(rest: list[str], ctx: Ctx) -> None:
    target = next((w for w in rest if not w.startswith("-") or w == "-"), "")
    if not target or target == "-" or _dynamic(target) or target.startswith("~"):
        ctx.cwd, ctx.moved_out = None, True
    elif ctx.root is None:
        if _outside(target, ctx):
            ctx.moved_out = True
    elif ctx.cwd is not None:
        ctx.cwd = posixpath.normpath(target if posixpath.isabs(target) else posixpath.join(ctx.cwd, target))


def _shell(program: str, rest: list[str], ctx: Ctx, depth: int) -> None:
    if program == "busybox" and rest:
        program, rest = _base(rest[0]), rest[1:]
        if program not in SHELLS:
            return
    if program == "eval":
        scan_text(" ".join(rest), ctx, depth + 1)
        return
    if program in ("source", "."):
        _script_file(rest[0] if rest else "", ctx, depth)
        return
    for position, word in enumerate(rest):
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            tail = rest[position + 1:]
            if tail:
                scan_text(tail[0], ctx, depth + 1)
            return
        if not word.startswith("-"):
            _script_file(word, ctx, depth)
            return


def _script_file(path: str, ctx: Ctx, depth: int) -> None:
    text = _read(path, ctx)
    if text:
        scan_text(text, ctx, depth + 1)
        _raw_code(text)


def _read(path: str, ctx: Ctx) -> str:
    if not path or _dynamic(path):
        return ""
    full = path if os.path.isabs(path) else os.path.join(ctx.cwd or "", path)
    try:
        if os.path.isfile(full) and os.path.getsize(full) <= SCRIPT_LIMIT:
            with open(full, encoding="utf-8", errors="replace") as handle:
                return handle.read(SCRIPT_LIMIT)
    except OSError:
        return ""
    return ""


def _interpreter(rest: list[str], ctx: Ctx) -> None:
    """python -c / node -e / perl -e ...: judge the code text; a script file is read when it can be."""
    _raw_code(" ".join(rest))
    for word in rest:
        if not word.startswith("-") and not _dynamic(word):
            text = _read(word, ctx)
            if text:
                _raw_code(text)
            break


_RAW_WORDS = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)


def _raw_code(text: str) -> None:
    """Code we cannot parse as shell (python, node, a heredoc): the guarded words together."""
    low = text.lower()
    words = set(_RAW_WORDS.findall(low))
    if "git" in words and "push" in words and (words & set(DEFAULT_BRANCHES) or "+refs" in low or re.search(
            r"--(?:force|delete|mirror|all)|[\s'\",]-f\b", low)):
        _hit("git push to the default branch or a force/delete push, inside code")
    if "gh" in words and ((words & {"pr", "merge"}) == {"pr", "merge"} or (
            "repo" in words and words & _REPO_ADMIN) or ("release" in words and "delete" in words)):
        _hit("a GitHub merge or repo change, inside code")
    if _GITHUB_URL.search(text) and (words & {"delete", "patch", "put"} or _GRAPHQL_MUTATION.search(text)
                                     or "/merges" in low or "/rulesets" in low):
        _hit("a GitHub API write, inside code")
    if words & {"rmtree", "unlink", "removedirs", "rmdir", "remove"} and re.search(r"""["'](?:/|~|\.\./)""", text):
        _hit("deleting files outside the workspace, inside code")
    if "api.github.com" in low and "mergepullrequest" in low:
        _hit("a GitHub merge, inside code")


# --- file deletion ----------------------------------------------------------------------------------

def _outside(path: str, ctx: Ctx, root_counts: bool = False) -> bool:
    """Whether *path* may lie outside the bot's workspace (the CLI's working directory).
    With *root_counts*, the workspace directory itself is outside too (deleting it takes the workspace)."""
    if not path:
        return False
    if path.startswith("~") or "$(" in path or "`" in path:
        return True
    if path.startswith("$"):
        name = re.match(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", path)
        if name and name.group(1) not in _SAFE_VARS:
            return name.group(1).upper() in ("HOME", "TMPDIR", "TMP", "TEMP", "OLDPWD", "USER", "LOGNAME", "PREFIX",
                                            "DESTDIR", "ROOT", "PATH", "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                            "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")
        return False
    if path == "/dev/null":
        return False
    if ctx.root is None:
        return ctx.moved_out or os.path.isabs(path) or ".." in path.split("/")
    if ctx.cwd is None:
        return True
    full = posixpath.normpath(path if os.path.isabs(path) else posixpath.join(ctx.cwd, path))
    root = posixpath.normpath(ctx.root)
    if root_counts and full == root:
        return True
    return not (full == root or full.startswith(root.rstrip("/") + "/"))


def _positionals(args: list[str]) -> list[str]:
    out, only = [], False
    for arg in args:
        if only or not arg.startswith("-") or arg == "-":
            out.append(arg)
        elif arg == "--":
            only = True
    return out


def _delete(program: str, args: list[str], ctx: Ctx) -> None:
    if program == "apply_patch":
        return
    if program == "gio":
        if not args or args[0] not in ("trash", "remove"):
            return
        args = args[1:]
    if program == "find":
        if not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
            return
        if any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args) and "-delete" not in args:
            if not any(_base(a) in RM_PROGRAMS | {"mv"} for a in args):
                return
        roots = []
        for arg in args:
            if arg.startswith("-") or arg in ("(", "!", ")"):
                break
            roots.append(arg)
        if not roots:
            roots = ["."]
        for root in roots:
            if _outside(root, ctx):
                _hit("deleting files outside the workspace (find)")
        return
    if program == "rsync":
        if not any(a.startswith(("--delete", "--remove-source-files", "--del")) for a in args):
            return
    if program == "mv":
        if not any(a == "/dev/null" for a in args):
            return
        for target in _positionals(args):
            if target != "/dev/null" and _outside(target, ctx):
                _hit("moving files outside the workspace to /dev/null")
        return
    for target in _positionals(args):
        if _outside(target, ctx, root_counts=True) or (program == "rsync" and re.match(r"^[^/]*:", target)):
            _hit(f"deleting files outside the workspace ({program})")


def _patch_deletes(text: str, ctx: Ctx) -> None:
    for path in _PATCH_DELETE_FILE.findall(text):
        if _outside(path.strip(), ctx):
            _hit("deleting a file outside the workspace (patch)")


# --- git --------------------------------------------------------------------------------------------

def _git_out(directory: str | None, *args: str) -> str | None:
    if not directory or not os.path.isdir(directory):
        return None
    try:
        done = subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL, check=False,
                              env={**{k: v for k, v in os.environ.items() if k != "BOT_OFFICE_GUARD_SHIM"},
                                    "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})  # our own query
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _implicit_push_risky(directory: str | None, remote: str | None, via_head: bool, overrides: dict) -> bool:
    """A push with no (or only HEAD) refspec: where does it go from this repo? Unknown = risky."""
    if not directory or not os.path.isdir(directory) or overrides.get("opaque"):
        return True
    branch = _git_out(directory, "symbolic-ref", "-q", "--short", "HEAD")
    if not branch:
        return True  # detached HEAD, or not a repo we can read
    if _DEFAULT_REF.match(branch):
        return True
    default = _git_out(directory, "symbolic-ref", "-q", "--short", f"refs/remotes/{remote or 'origin'}/HEAD")
    if default and branch == default.split("/", 1)[-1]:
        return True
    mode = overrides.get("push.default") or _git_out(directory, "config", "--get", "push.default") or "simple"
    if mode in ("matching", "upstream", "tracking"):
        return True
    if via_head:
        return False
    upstream = _git_out(directory, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or ""
    merge = _git_out(directory, "config", "--get", f"branch.{branch}.merge") or ""
    if any(ref and _DEFAULT_REMOTE_REF.match(ref) for ref in (upstream, merge)):
        return True
    configured = _git_out(directory, "config", "--get-all", f"remote.{remote or 'origin'}.push")
    return bool(configured)


def _git(args: list[str], ctx: Ctx, depth: int) -> None:
    if depth > MAX_DEPTH:
        _hit("a git alias nested too deeply to check")
    directory = ctx.cwd
    overrides: dict = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            break
        if arg == "-C" and i + 1 < len(args):
            if _dynamic(args[i + 1]):
                directory = None
            elif os.path.isabs(args[i + 1]):
                directory = args[i + 1]
            else:
                directory = os.path.join(directory, args[i + 1]) if directory else None
            i += 2
            continue
        if arg == "-c" and i + 1 < len(args):
            key, _, value = args[i + 1].partition("=")
            key = key.lower()
            if key.startswith("alias.") or (key.startswith("remote.") and key.endswith((".push", ".url", ".pushurl"))) \
                    or key.startswith("url."):
                _hit("git -c that rewrites an alias, remote or url")
            if key == "push.default":
                overrides["push.default"] = value.lower()
            i += 2
            continue
        if arg.split("=")[0] in ("--git-dir", "--work-tree"):
            overrides["opaque"] = True
        if arg in _GIT_VALUE_OPTS:
            i += 2
            continue
        i += 1
    if i >= len(args):
        return
    sub, rest = args[i].lower(), args[i + 1:]
    if _dynamic(sub):
        _hit("a git command that is not known until it runs")
    if sub == "push":
        _git_push(rest, directory, overrides)
    elif sub in ("send-pack", "http-push"):
        _hit(f"git {sub} (pushes refs)")
    elif sub == "tag":
        if any(a in ("--delete", "-d") or (a.startswith("-") and not a.startswith("--") and "d" in a[1:])
               for a in rest if a != "--"):
            _hit("deleting a git tag")
    elif sub == "branch":
        deleting = any(a in ("--delete", "-d", "-D") or (a.startswith("-") and not a.startswith("--")
                                                         and ("d" in a[1:] or "D" in a[1:])) for a in rest)
        if deleting and any(_DEFAULT_REMOTE_REF.match(p) or _dynamic(p) for p in _positionals(rest)):
            _hit("deleting the default branch")
    elif sub == "update-ref":
        if any(a == "-d" for a in rest):
            _hit("deleting a git ref")
    elif sub in ("submodule", "rebase", "bisect", "filter-branch"):
        for item in rest:
            if any(ch.isspace() for ch in item):
                scan_text(item, ctx, depth + 1)
    elif sub not in GIT_BUILTINS:
        _git_alias(sub, rest, directory, ctx, depth)


def _git_alias(sub: str, rest: list[str], directory: str | None, ctx: Ctx, depth: int) -> None:
    value = _git_out(directory, "config", "--get", f"alias.{sub}")
    if value is None:
        if directory is None or not os.path.isdir(directory):
            _hit(f"git {sub}: not a known git command, so it may be an alias for a push")
        return
    value = value.strip()
    if value.startswith("!"):
        scan_text(value[1:] + " " + " ".join(rest), ctx, depth + 1)
    else:
        words, _ = split_words(value)
        _git([*(words[0] if words else []), *rest], Ctx(root=directory, cwd=directory), depth + 1)


def _git_push(rest: list[str], directory: str | None, overrides: dict) -> None:
    positional: list[str] = []
    only = False
    skip = False
    for position, arg in enumerate(rest):
        if skip:
            skip = False
            continue
        if only or not arg.startswith("-") or arg == "-":
            positional.append(arg)
            continue
        if arg == "--":
            only = True
            continue
        if arg.startswith("--"):
            name, _, _value = arg[2:].partition("=")
            if any(target.startswith(name) and len(name) >= 1 for target in _PUSH_RISKY_LONG):
                _hit("git push with --" + name + " (force, delete, all, mirror or prune)")
            if f"--{name}" in _PUSH_VALUE_OPTS and "=" not in arg:
                skip = True
            continue
        cluster = arg[1:]
        if "f" in cluster or "d" in cluster:
            _hit("git push with -f or -d (force or delete)")
        if cluster.startswith("o") and len(cluster) == 1:
            skip = True
    remote = positional[0] if positional else None
    refspecs = positional[1:]
    if remote and (_DEFAULT_REF.match(remote) or _dynamic(remote) and not refspecs):
        _hit("git push naming the default branch")
    for refspec in refspecs:
        if _dynamic(refspec) or any(ch in refspec for ch in "*?[{"):
            _hit("git push with a refspec that is not known until it runs")
        if refspec.startswith("+"):
            _hit("git push --force through a +refspec")
        if refspec.startswith(":") or refspec.endswith(":"):
            _hit("git push deleting a remote ref")
        destination = refspec.split(":", 1)[-1]
        destination = re.sub(r"^refs/for/", "", destination)
        if _DEFAULT_REF.match(destination):
            _hit("git push to the default branch")
    only_head = bool(refspecs) and all(re.fullmatch(r"(?:HEAD|@)(?::.*)?", r) and ":" not in r for r in refspecs)
    if not refspecs or only_head:
        if _implicit_push_risky(directory, remote, only_head, overrides):
            _hit("git push of the current branch, which may be the default branch")


# --- gh ---------------------------------------------------------------------------------------------

def _gh_alias(name: str) -> str | None:
    base = os.environ.get("GH_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "gh")
    try:
        with open(os.path.join(base, "config.yml"), encoding="utf-8") as handle:
            text = handle.read(SCRIPT_LIMIT)
    except OSError:
        return None
    found = re.search(rf"^\s+{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    return found.group(1).strip().strip("'\"") if found else None


def _gh(args: list[str], ctx: Ctx, depth: int) -> None:
    positional = [a for a in args if not a.startswith("-")]
    low = [p.lower() for p in positional]
    pairs = list(zip(low, low[1:]))
    flags = set(args)
    if not low:
        return
    if _dynamic(low[0]):
        _hit("a gh command that is not known until it runs")
    for group, action in pairs:
        if group == "pr" and action == "merge":
            _hit("merging a pull request")
        if group == "pr" and action == "close" and flags & {"-d", "--delete-branch"}:
            _hit("closing a pull request and deleting its branch")
        if group == "repo" and action in _REPO_ADMIN:
            _hit(f"gh repo {action} (repo settings, rename, delete or default-branch change)")
        if group == "repo" and action == "create" and "--push" in flags:
            _hit("gh repo create --push (pushes to the new default branch)")
        if group == "release" and action in ("delete", "delete-asset"):
            _hit("deleting a release")
        if group in ("secret", "variable") and action in ("set", "delete", "remove"):
            _hit(f"changing repo {group}s (settings)")
        if group == "workflow" and action in ("enable", "disable"):
            _hit("changing which workflows run (repo settings)")
    if low[0] == "repo" and len(low) > 1 and low[1] in _REPO_ADMIN:
        _hit(f"gh repo {low[1]}")
    if low[0] == "alias" and len(low) > 1 and low[1] in ("set", "import"):
        for value in positional[3:] or positional[2:]:
            _gh([w for w in value.lstrip("!").split()], ctx, depth + 1)
            scan_text(value.lstrip("!"), ctx, depth + 1)
    if low[0] == "api":
        _gh_api(args[args.index(positional[0]) + 1:], ctx)
    elif low[0] not in GH_GROUPS:
        value = _gh_alias(low[0])
        if value:
            if value.startswith("!"):
                scan_text(value[1:], ctx, depth + 1)
            else:
                _gh([*value.split(), *args[args.index(positional[0]) + 1:]], ctx, depth + 1)


_API_VALUE_OPTS = {"-H", "--header", "-f", "-F", "--field", "--raw-field", "--input", "-q", "--jq", "-t",
                   "--template", "--hostname", "-p", "--preview", "--cache"}


def _gh_api(args: list[str], ctx: Ctx) -> None:
    method, endpoint, fields = "", "", False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1].lstrip("=")
            i += 2
            continue
        if arg.startswith("--method="):
            method = arg.split("=", 1)[1]
        elif arg.startswith("-X"):
            method = arg[2:].lstrip("=")
        elif arg in _API_VALUE_OPTS and i + 1 < len(args):
            fields = fields or arg in ("-f", "-F", "--field", "--raw-field", "--input")
            i += 2
            continue
        elif arg.startswith(("-f", "-F")) and len(arg) > 2 and not arg.startswith("--"):
            fields = True
        elif arg.startswith(("--field=", "--raw-field=", "--input=")):
            fields = True
        elif not arg.startswith("-") and not endpoint:
            endpoint = arg
        i += 1
    _github_write(method, endpoint, fields, args, "gh api")


def _github_write(method: str, endpoint: str, has_body: bool, words: list[str], what: str) -> None:
    method = method.upper()
    path = re.sub(r"^(?:https?://)?[^/\s]*?(?:github\.com|GITHUB_API\w*|\$\{?\w+\}?)(?::\d+)?/(?:api/v3/)?", "", endpoint)
    path = path.lstrip("/").split("?", 1)[0]
    if method and not _METHOD.match(method):
        _hit(f"{what} with a method that is not known until it runs")
    writes = method in ("PUT", "PATCH", "DELETE")
    posts = method == "POST" or (not method and has_body)
    if path.startswith("graphql") or "graphql" in endpoint:
        if _GRAPHQL_MUTATION.search(" ".join(words)):
            _hit(f"{what}: a GraphQL mutation that merges, deletes or changes repo settings")
    if _dynamic(endpoint):
        if writes or posts:
            _hit(f"{what} write to an endpoint that is not known until it runs")
        return
    if writes and not (method != "DELETE" and _API_SAFE_WRITE.match(path)):
        _hit(f"{what} {method} (merge, delete or repo settings change)")
    if posts and _API_SENSITIVE_POST.match(path):
        _hit(f"{what} POST that merges, adds a webhook or key, or changes repo settings")


# --- curl / wget / httpie -----------------------------------------------------------------------------

def _http(program: str, args: list[str], ctx: Ctx) -> None:
    method, body = "", False
    urls: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        value = None
        if arg in ("--request", "--method") and i + 1 < len(args):
            value = args[i + 1]
            i += 1
        elif arg.startswith(("--request=", "--method=")):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-") and not arg.startswith("--") and "X" in arg[1:]:
            tail = arg[arg.index("X") + 1:]
            value = tail.lstrip("=") if tail else (args[i + 1] if i + 1 < len(args) else "")
            i += 0 if tail else 1
        elif arg in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json",
                     "-F", "--form", "--post-data", "--post-file", "--body-data", "--body-file") or arg.startswith(
                ("--data", "--json=", "--form=", "--post-data=", "--post-file=", "--body-")):
            body = True
        elif arg in ("-T", "--upload-file") or arg.startswith("--upload-file="):
            method = method or "PUT"
        elif program in ("http", "https", "xh", "xhs", "httpie") and _METHOD.match(arg) and not urls:
            method = method or arg
        elif not arg.startswith("-"):
            urls.append(arg)
        if value is not None:
            method = value
        i += 1
    everything = " ".join(args)
    github = any(_GITHUB_URL.search(u) for u in urls) or bool(_GITHUB_URL.search(everything))
    unknown_host = any(_dynamic(u) for u in urls)
    if program in ("http", "https", "xh", "xhs", "httpie") and not method:
        if any(re.match(r"^[\w.-]+(?:=|:=|==|@)", u) for u in urls[1:]):
            method = "POST"
    if not (github or (unknown_host and method.upper() in ("PUT", "PATCH", "DELETE"))):
        return
    target = next((u for u in urls if _GITHUB_URL.search(u)), urls[0] if urls else "")
    _github_write(method or ("POST" if body else ""), target, body, args, program)


# --- MCP / connector tools ------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tool_words(tool: str) -> set[str]:
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub("_", tool)) if w}


def _input_strings(value, out: list[str], depth: int = 0) -> None:
    if depth > 5 or len(out) > 200:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _input_strings(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _input_strings(item, out, depth + 1)


def _mcp(tool: str, data: dict | None, ctx: Ctx) -> None:
    words = _tool_words(tool)
    if not words:
        return
    repoish = words & {"repo", "repos", "repository", "repositories", "branch", "branches", "ref", "refs", "tag",
                       "tags", "release", "releases", "ruleset", "rulesets", "protection", "webhook", "hook",
                       "hooks", "deploy", "key", "keys", "collaborator", "secret", "variable", "environment"}
    code_host = words & {"github", "gh", "git", "gitlab", "bitbucket", "gitea", "codeberg", "azure", "devops"}
    if "merge" in words and (words & {"pull", "pr", "request", "mr", "branch", "branches", "auto", "queue"} or len(words) <= 3):
        _hit("a connector tool that merges")
    if words & {"delete", "remove", "destroy", "drop", "trash"} and (repoish or (code_host and words & {"file", "files"})):
        _hit("a connector tool that deletes a branch, tag, repo, release or file")
    if words & {"update", "edit", "set", "patch", "modify", "change", "configure", "enable", "disable"} and (
            words & {"repo", "repository", "settings", "protection", "ruleset", "rulesets", "webhook", "hook",
                     "default", "visibility", "ref", "refs"}):
        _hit("a connector tool that changes repo settings or moves a ref")
    if "create" in words and words & {"ruleset", "rulesets", "protection", "webhook", "hook", "deploy", "key"}:
        _hit("a connector tool that adds repo settings")
    if words & {"archive", "unarchive", "transfer"} or ("rename" in words and repoish) or (
            "default" in words and "branch" in words) or ("force" in words and words & {"push", "ref", "refs", "update"}):
        _hit("a connector tool that archives, transfers or renames a repo or sets the default branch")
    if code_host and (words & {"push", "commit", "upload"} or {"create", "or", "update", "file"} <= words
                      or ("update" in words and words & {"file", "files", "contents"})
                      or ("create" in words and words & {"file", "files"})):
        strings: list[str] = []
        _input_strings(data or {}, strings)
        branches = [data.get(k) for k in ("branch", "ref", "head", "base") if isinstance((data or {}).get(k), str)]
        if not branches or any(_DEFAULT_REF.match(b.strip()) or b.strip() in ("HEAD", "@") or _dynamic(b)
                               for b in branches):
            _hit("a connector tool that commits or pushes to the default branch")
    if words & {"delete", "remove", "trash"} and words & {"file", "files", "directory", "folder", "path"}:
        strings = []
        _input_strings(data or {}, strings)
        paths = [s for s in strings if "/" in s or s.startswith("~") or ".." in s]
        if not strings or any(_outside(p, ctx) for p in paths):
            _hit("a connector tool that deletes files outside the workspace")


# --- entry point ----------------------------------------------------------------------------------

def classify(tool: str, command: str = "", tool_input: dict | None = None, cwd: str | None = None) -> str | None:
    """Why this tool call must wait for the owner (a short phrase), or None. Never raises: a call
    it cannot read is a hit."""
    ctx = Ctx(root=cwd or None, cwd=cwd or None)
    try:
        if tool and tool.lower().startswith("mcp") or tool == "MCP":
            _mcp(tool, tool_input, ctx)
        if command:
            if len(command) > SCAN_LIMIT:
                return "a command too long to check"
            scan_text(command, ctx)
            _patch_deletes(command, ctx)
        if tool.lower() in ("apply_patch", "applypatch"):
            strings: list[str] = []
            _input_strings(tool_input or {}, strings)
            _patch_deletes("\n".join(strings), ctx)
    except _Hit as hit:
        return hit.reason
    except RecursionError:
        return "a command nested too deeply to check"
    except Exception:  # noqa: BLE001 — fail closed on anything this reader did not expect
        return "a command that could not be read"
    return None
