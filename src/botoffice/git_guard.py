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
_HOOK_ENV = re.compile(r"^(?:GIT_CONFIG\w*|GIT_EXEC_PATH|GIT_DIR|GIT_SSH\w*|GIT_ASKPASS|GH_TOKEN|GITHUB_TOKEN|"
                       r"GH_ENTERPRISE_TOKEN|GH_CONFIG_DIR|GH_HOST|BOT_OFFICE_\w+)$")
# git config keys that switch off or redirect the push check and the credential path
_HOOK_KEYS = re.compile(r"^(?:core\.hookspath|credential\..*|include\..*|includeif\..*|core\.sshcommand|"
                        r"protocol\..*\.allow|uploadpack\..*|receive\..*)$", re.IGNORECASE)
ZERO = "0" * 40
# GraphQL mutations a bot may run without asking (comments, issues, PRs it opens, reviews, reactions, labels)
_SAFE_MUTATIONS = frozenset({
    "addcomment", "updateissuecomment", "addreaction", "removereaction", "createissue", "updateissue",
    "closeissue", "reopenissue", "createpullrequest", "addpullrequestreview", "submitpullrequestreview",
    "addpullrequestreviewcomment", "addpullrequestreviewthread", "addlabelstolabelable",
    "removelabelsfromlabelable", "markpullrequestreadyforreview", "convertpullrequesttodraft",
    "requestreviews", "resolvereviewthread", "unresolvereviewthread", "minimizecomment"})
# REST writes under repos/<owner>/<repo>/ a bot may make without asking (issues, comments, opening a PR, reviews,
# statuses and check runs); every other non-GET to repos/... asks
_REPO_SAFE_WRITES = (
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/(?:issues|pulls|statuses/[^/]+|check-runs|check-suites)$")),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/(?:comments|labels|assignees|reactions)$")),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/(?:issues|pulls)/comments/\d+/reactions$")),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/(?:comments|reviews|requested_reviewers)$")),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/comments/\d+/replies$")),
    ("PATCH", re.compile(r"^repos/[^/]+/[^/]+/issues/(?:\d+|comments/\d+)$")),
    ("PATCH", re.compile(r"^repos/[^/]+/[^/]+/pulls/comments/\d+$")),
    ("PATCH", re.compile(r"^repos/[^/]+/[^/]+/check-runs/\d+$")),
)
# owner-level endpoints whose writes change settings, membership or delete repos
_ADMIN_PREFIX = re.compile(r"^(?:repos|orgs|enterprises|teams|organizations|app|installation|user/installations)/")


@dataclass
class Ctx:
    """Where the command runs: *root* is the bot's workspace (the CLI's directory, if the hook says),
    *cwd* follows `cd` along the command; None once it is not known."""
    root: str | None = None
    cwd: str | None = None
    moved_out: bool = False  # without a root: a `cd` left for somewhere not known to be inside
    commands: int = 0
    sends: list = None  # fix3 #4: why a part of the command sends something out (the "send" class)

    def __post_init__(self):
        if self.sends is None:
            self.sends = []


# fix3 #5: the controller sets this to its cached GitHub API lookup (owner, repo) -> default branch or
# None; the hooks and shims (no token) leave it None, so an unknown default asks there
default_lookup = None


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
        _raw_code(text, ctx, depth)


def _interpreter_in(commands: list[list[str]]) -> bool:
    return any(INTERPRETERS.match(_base(word)) or _base(word) in SHELLS for words in commands for word in words)


def _scan_command(words: list[str], ctx: Ctx, depth: int) -> None:
    cursor = 0
    while cursor < len(words):
        word = words[cursor]
        if _ASSIGN.match(word) and "/" not in word.split("=", 1)[0]:
            _hook_env(word.split("=", 1)[0])
            cursor += 1
            continue
        break
    words = words[cursor:]
    for index, word in enumerate(words):
        program = _base(word)
        rest = words[index + 1:]
        if program in ("echo", "printf"):
            return  # prints its words; a substitution in them was scanned as a nested command
        if program in ("export", "unset", "declare", "typeset", "local", "readonly", "env", "setenv", "unsetenv"):
            for item in rest:
                _hook_env(item.split("=", 1)[0].lstrip("-"))
            if program == "env" and any(a in ("-i", "--ignore-environment", "-") or a.startswith("-u") or
                                        a.startswith("--unset") for a in rest):
                _hit("env that clears the run's environment (the push check and the GitHub credential path)")
        if program in ("bot-office-credential", "bot-office-git-hook") or program.startswith("git-credential"):
            _hit("calling Bot-Office's GitHub credential helper or push check directly")
        if program == "git" or program.startswith("git-") and program[4:] in ("push", "send-pack", "http-push"):
            _git(rest if program == "git" else [program[4:], *rest], ctx, depth)
            return
        if program == "gh":
            _gh(rest, ctx, depth)
            return
        if program in HTTP_PROGRAMS:
            _http(program, rest, ctx)
            return
        if program in MAIL_PROGRAMS and (index == 0 or any(_base(w) in RUNNERS for w in words[:index])):
            ctx.sends.append(f"sending email ({program})")
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
            _interpreter(rest, ctx, depth)
            return
        if program in RUNNERS:
            for item in rest:
                if any(ch.isspace() for ch in item):
                    scan_text(item, ctx, depth + 1)
    return


def _hook_env(name: str) -> None:
    """Changing the variables that carry the run's git config (the pre-push check, the credential
    helper) or the job's gate settings switches the side-effect check off: always ask."""
    if _HOOK_ENV.match(name):
        _hit(f"changing {name} (switches off the push check or the GitHub credential path)")


def _cd(rest: list[str], ctx: Ctx) -> None:
    target = next((w for w in rest if not w.startswith("-") or w == "-"), "")
    if not target or target == "-" or _dynamic(target) or target.startswith("~"):
        ctx.cwd, ctx.moved_out = None, True
    elif ctx.root is None:
        if _outside(target, ctx):
            ctx.moved_out = True
    elif ctx.cwd is not None:
        moved = posixpath.normpath(target if posixpath.isabs(target) else posixpath.join(ctx.cwd, target))
        real = os.path.realpath(moved)
        # a link into the workspace from outside, or out of it from inside: follow where it really goes
        ctx.cwd = real if _within(real, os.path.realpath(ctx.root)) != _within(moved, ctx.root) else moved


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
        _raw_code(text, ctx, depth)


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


def _interpreter(rest: list[str], ctx: Ctx, depth: int = 0) -> None:
    """python -c / node -e / perl -e ...: judge the code text; a script file is read when it can be."""
    _raw_code(" ".join(rest), ctx, depth)
    for word in rest:
        if not word.startswith("-") and not _dynamic(word):
            text = _read(word, ctx)
            if text:
                _raw_code(text, ctx, depth)
            break


_RAW_WORDS = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)
# fix3 #1: string literals in code (one level of quotes, no nesting) and lists of them
_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1)[^\\\n]){1,2000})\1""")
_LITERAL_LIST = re.compile(r"""[\[(]\s*(?:(?:'[^'\n]{0,300}'|"[^"\n]{0,300}")\s*,\s*){1,40}"""
                           r"""(?:'[^'\n]{0,300}'|"[^"\n]{0,300}")\s*,?\s*[\])]""")
# programs worth reading a literal as a shell command for (os.system("git push ..."), exec('rm -rf ...'))
_LITERAL_PROGRAMS = frozenset({"git", "gh", "rm", "rmdir", "unlink", "shred", "find", "rsync", "mv", "sh", "bash",
                               "zsh", "env", "sudo", "xargs"}) | frozenset(("curl", "wget", "http", "https", "xh",
                                                                           "xhs", "sendmail", "mail", "mailx",
                                                                           "msmtp", "swaks", "mutt"))
# fix3 #4: code that sends over HTTP(S) with a method other than GET, or sends email
_CODE_SEND = re.compile(
    r"\b(?:requests|httpx|session|aiohttp|axios|got|ky|superagent|needle)\s*\.\s*"
    r"(?:post|put|patch|delete)\s*\(|"
    r"\brequests\s*\.\s*request\s*\(\s*['\"](?:post|put|patch|delete)|"
    r"\bmethod\s*[:=]\s*['\"](?:post|put|patch|delete)['\"]|"
    r"\burlopen\s*\([^)]*\bdata\s*=|\bRequest\s*\([^)]*\bdata\s*=|"
    r"\.request\s*\(\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]|"
    r"\bcurl_setopt\s*\([^)]*CURLOPT_POST|\bNet::HTTP::(?:Post|Put|Patch|Delete)\b|->\s*(?:post|put)\s*\(",
    re.IGNORECASE)
_CODE_MAIL = re.compile(r"\bsmtplib\b|\bnodemailer\b|\bNet::SMTP\b|\bsendmail\b|\bPHPMailer\b|\bmail\s*\(\s*\$",
                        re.IGNORECASE)
_CODE_URL = re.compile(r"\b(?:https?|wss?)://([^/\s'\"`:?#]+)", re.IGNORECASE)


def _literals(text: str) -> list[str]:
    """Shell-like commands inside code: a string literal, or a list of literals joined as words."""
    found = []
    for match in _LITERAL_LIST.finditer(text[:SCAN_LIMIT]):
        words = [item[1:-1] for item in re.findall(r"""'[^'\n]*'|"[^"\n]*\"""", match.group(0))]
        if words and _base(words[0]) in _LITERAL_PROGRAMS:
            found.append(" ".join(_shell_quote(word) for word in words))
    for match in _LITERAL.finditer(text[:SCAN_LIMIT]):
        value = match.group(2)
        first = value.split(None, 1)[0] if value.split() else ""
        if " " in value and _base(first) in _LITERAL_PROGRAMS:
            found.append(value)
        if len(found) > 50:
            break
    return found


def _shell_quote(word: str) -> str:
    return word if re.fullmatch(r"[\w@%+=:,./-]+", word) else "'" + word.replace("'", "'\"'\"'") + "'"


def _raw_code(text: str, ctx: Ctx | None = None, depth: int = 0) -> None:
    """Code we cannot parse as shell (python, node, a heredoc): the guarded words together, the shell
    commands in its string literals, and HTTP or mail sends."""
    if ctx is not None:
        if depth < MAX_DEPTH:
            for literal in _literals(text):
                scan_text(literal, ctx, depth + 1)
        if _CODE_MAIL.search(text):
            ctx.sends.append("sending email from code")
        elif _CODE_SEND.search(text):
            hosts = [host.lower() for host in _CODE_URL.findall(text)]
            outside = [host for host in hosts if not allowed_host(host)]
            if outside or not hosts:
                ctx.sends.append(f"a request that sends data to {outside[0] if outside else 'a host not known here'}"
                                 " (from code)")
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

def _outside(path: str, ctx: Ctx, root_counts: bool = False, follow: bool = False) -> bool:
    """Whether *path* may lie outside the bot's workspace (the CLI's working directory).
    With *root_counts*, the workspace directory itself is outside too (deleting it takes the workspace).
    A path that is not a literal (an unknown variable, a substitution, `~`, a glob that can match `..`)
    may be anywhere: outside. Links are resolved (realpath) as well as the words: *follow* resolves the
    last part too (a trailing `/`, or a program that follows links)."""
    if not path:
        return False
    if path == "/dev/null":
        return False
    if path.startswith("~") or "$(" in path or "`" in path:
        return True
    if "$" in path:
        here = {"PWD": ctx.cwd, "WORKSPACE": ctx.root, "BOT_OFFICE_WORKSPACE": ctx.root}
        known = re.fullmatch(r"\$\{?(PWD|WORKSPACE|BOT_OFFICE_WORKSPACE)\}?((?:/[^$]*)?)", path)
        if not known or not here[known.group(1)]:
            return True  # a variable we cannot know here (X=/outside; rm -rf "$X")
        path = here[known.group(1)] + known.group(2)
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if base.startswith(".") and any(ch in base for ch in "*?["):
        return True  # `.*` can match `..`
    if ctx.root is None:
        return ctx.moved_out or os.path.isabs(path) or ".." in path.split("/")
    if ctx.cwd is None:
        return True
    full = posixpath.normpath(path if os.path.isabs(path) else posixpath.join(ctx.cwd, path))
    root = posixpath.normpath(ctx.root)
    real_root = os.path.realpath(root)
    if root_counts and full in (root, real_root):
        return True
    if not _within(full, root):
        return True
    # through links: the parent directory always, the last part when it is followed
    parent, name = posixpath.split(full)
    real = os.path.realpath(full) if follow or path.endswith("/") else posixpath.join(os.path.realpath(parent), name)
    if root_counts and real == real_root:
        return True
    return not _within(real, real_root)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


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
            if _outside(root, ctx, follow=True):
                _hit("deleting files outside the workspace (find)")
        return
    if program == "rsync":
        if not any(a.startswith(("--delete", "--remove-source-files", "--del")) for a in args):
            return
    if program == "mv":
        # moving a file away from outside the workspace deletes it there; moving onto a path outside
        # overwrites what is there
        for target in _positionals(args):
            if target != "/dev/null" and _outside(target, ctx, root_counts=True):
                _hit("moving or overwriting files outside the workspace (mv)")
        for position, arg in enumerate(args):
            value = arg.split("=", 1)[1] if arg.startswith("--target-directory=") else (
                args[position + 1] if arg in ("-t", "--target-directory") and position + 1 < len(args) else None)
            if value is not None and _outside(value, ctx):
                _hit("moving files into a directory outside the workspace (mv)")
        return
    follow = program in ("rsync", "shred", "srm", "wipe")
    for target in _positionals(args):
        if _outside(target, ctx, root_counts=True, follow=follow) or (program == "rsync" and re.match(r"^[^/]*:", target)):
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


# remote default branches the pre-push check learned from the remote itself (ls-remote --symref), by
# (git common dir, remote): used when the repo has no refs/remotes/<remote>/HEAD
_LEARNED: dict[tuple[str, str], str] = {}


def _git_dirs(directory: str | None) -> tuple[str, str] | None:
    """(git dir, common dir) of the repo around *directory*, read from the files (no git process: the
    controller may be another user, and a repo's config must never run anything as the controller)."""
    if not directory:
        return None
    current = os.path.realpath(directory)
    for _ in range(64):
        dot = os.path.join(current, ".git")
        gitdir = None
        if os.path.isdir(dot):
            gitdir = dot
        elif os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8") as handle:
                    line = handle.read(4096).strip()
            except OSError:
                return None
            if line.startswith("gitdir:"):
                value = line[7:].strip()
                gitdir = os.path.normpath(value if os.path.isabs(value) else os.path.join(current, value))
        elif os.path.isfile(os.path.join(current, "HEAD")) and os.path.isdir(os.path.join(current, "refs")):
            gitdir = current  # a bare repo
        if gitdir:
            common = gitdir
            try:
                with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as handle:
                    value = handle.read(4096).strip()
                common = os.path.normpath(value if os.path.isabs(value) else os.path.join(gitdir, value))
            except OSError:
                pass
            return gitdir, common
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def remote_default(directory: str | None, remote: str | None) -> str | None:
    """The default branch of *remote* (a remote name) as the repo around *directory* knows it:
    refs/remotes/<remote>/HEAD (set by clone or `git remote set-head`), else what the pre-push check
    learned from the remote. For a github.com remote the controller asks GitHub first (fix3 #5).
    None when unknown (a URL, a path, no such ref)."""
    url = remote_url(directory, remote) if remote else None
    hub = github_repo(normalise_remote(url)) if url else None
    if hub and repo_default(*hub):
        return repo_default(*hub)
    if not remote or not re.fullmatch(r"[A-Za-z0-9][\w.-]*", remote):
        return None
    dirs = _git_dirs(directory)
    if not dirs:
        return None
    try:
        with open(os.path.join(dirs[1], "refs", "remotes", remote, "HEAD"), encoding="utf-8") as handle:
            line = handle.read(4096).strip()
        prefix = f"ref: refs/remotes/{remote}/"
        if line.startswith(prefix) and line[len(prefix):]:
            return line[len(prefix):]
    except OSError:
        pass
    return _LEARNED.get((os.path.realpath(dirs[1]), remote))


def learn_default(directory: str | None, remote: str | None, branch: str | None) -> None:
    dirs = _git_dirs(directory)
    if dirs and remote and branch and len(_LEARNED) < 10000:
        _LEARNED[(os.path.realpath(dirs[1]), remote)] = branch


def _implicit_push_risky(directory: str | None, remote: str | None, via_head: bool, overrides: dict) -> bool:
    """A push with no (or only HEAD) refspec: where does it go from this repo? Unknown = risky."""
    if not directory or not os.path.isdir(directory) or overrides.get("opaque"):
        return True
    branch = _git_out(directory, "symbolic-ref", "-q", "--short", "HEAD")
    if not branch:
        return True  # detached HEAD, or not a repo we can read
    if _DEFAULT_REF.match(branch):
        return True
    default = remote_default(directory, remote or "origin")
    if default is None or branch == default:
        return True  # the remote's default branch is not known here: ask
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
        if arg in ("-c", "--config-env") and i + 1 < len(args) or arg.startswith("--config-env="):
            setting = args[i + 1] if "=" not in arg else arg.split("=", 1)[1]
            key, _, value = setting.partition("=")
            key = key.lower()
            if key.startswith("alias.") or (key.startswith("remote.") and key.endswith((".push", ".url", ".pushurl"))) \
                    or key.startswith("url."):
                _hit("git -c that rewrites an alias, remote or url")
            if _HOOK_KEYS.match(key) or arg.startswith("--config-env"):
                _hit("git -c that switches off the push check or changes the credential helper")
            if "=" in arg:
                i += 1
                continue
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
    elif sub == "credential" or sub.startswith("credential-"):
        _hit("reading git credentials (git credential)")
    elif sub == "config" and any(_HOOK_KEYS.match(a) for a in rest if not a.startswith("-")) and not any(
            a in ("--get", "--get-all", "--list", "-l", "get", "list") for a in rest):
        _hit("git config that switches off the push check or changes the credential helper")
    elif sub in ("send-pack", "http-push"):
        _hit(f"git {sub} (pushes refs)")
    elif sub == "tag":
        if any(a in ("--delete", "-d") or (a.startswith("-") and not a.startswith("--") and "d" in a[1:])
               for a in rest if a != "--"):
            _hit("deleting a git tag")
    elif sub == "branch":
        if any(a in ("--delete", "-d", "-D") or (a.startswith("-") and not a.startswith("--")
                                                  and ("d" in a[1:] or "D" in a[1:])) for a in rest):
            _hit("deleting a git branch")
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
            if name in ("no-verify", "no-veri", "no-verif") or name.startswith("no-v"):
                _hit("git push --no-verify (skips the push check)")
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
    default = remote_default(directory, remote or "origin")
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
        if refspec in ("tag",) or destination.startswith("refs/") and not destination.startswith("refs/heads/") \
                or refspec in ("HEAD", "@"):
            continue  # a tag or another ref namespace (HEAD alone is judged below)
        branch = re.sub(r"^(?:refs/)?heads/", "", destination)
        if branch in ("HEAD", "@"):
            _hit("git push to the remote's HEAD (its default branch)")
        if default is None:
            _hit("git push to a branch of a remote whose default branch is not known here")
        if branch == default:
            _hit(f"git push to the default branch ({default})")
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
        if group == "auth" and (action in ("token", "login", "logout", "setup-git", "refresh", "switch")
                                or "--show-token" in flags or "-t" in flags):
            _hit(f"gh auth {action} (reads or changes the GitHub credential)")
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


def _body_file(value: str, ctx: Ctx) -> str | None:
    """The text a request body names (`@file`, a file path for --input), or None when it cannot be
    known here (stdin, a variable, a missing or unreadable file)."""
    if value in ("-", "@-", "") or _dynamic(value):
        return None
    path = value.removeprefix("@")
    if path in ("-", "/dev/stdin") or path.startswith(("/dev/fd/", "/proc/")):
        return None
    full = path if os.path.isabs(path) else os.path.join(ctx.cwd or "", path)
    if not ctx.cwd and not os.path.isabs(path):
        return None
    try:
        with open(full, encoding="utf-8", errors="replace") as handle:
            return handle.read(SCAN_LIMIT)
    except OSError:
        return None


def _gh_api(args: list[str], ctx: Ctx) -> None:
    method, endpoint, fields = "", "", False
    bodies: list[str | None] = []  # request text: inline fields and file contents (None: not knowable)
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
            value = args[i + 1]
            if arg in ("-f", "-F", "--field", "--raw-field", "--input"):
                fields = True
                bodies.append(_field_body(arg, value, ctx))
            i += 2
            continue
        elif arg.startswith(("-f", "-F")) and len(arg) > 2 and not arg.startswith("--"):
            fields = True
            bodies.append(_field_body(arg[:2], arg[2:], ctx))
        elif arg.startswith(("--field=", "--raw-field=", "--input=")):
            fields = True
            name, value = arg.split("=", 1)
            bodies.append(_field_body(name, value, ctx))
        elif not arg.startswith("-") and not endpoint:
            endpoint = arg
        i += 1
    _github_write(method, endpoint, fields, [*args, *(b for b in bodies if b)], "gh api",
                  unknown_body=any(b is None for b in bodies))


def _field_body(option: str, value: str, ctx: Ctx) -> str | None:
    if option == "--input":
        return _body_file(value, ctx)
    key, _, item = value.partition("=")
    if _dynamic(item):
        return None
    if option in ("-F", "--field") and item.startswith("@"):
        text = _body_file(item, ctx)
        return None if text is None else key + "=" + text
    return value


def _mutations(text: str) -> set[str] | None:
    """The fields called in a GraphQL mutation in *text* (every `name(`, so nested ones count too and
    an unknown one asks), or None when *text* holds no mutation."""
    low = text.lower()
    at = low.find("mutation")
    if at < 0:
        return None
    return set(re.findall(r"([a-z_][a-z0-9_]*)\s*\(", low[at:])) - {"mutation"} or {"unknown"}


def _github_write(method: str, endpoint: str, has_body: bool, words: list[str], what: str,
                  unknown_body: bool = False) -> None:
    method = method.upper()
    path = re.sub(r"^(?:https?://)?[^/\s]*?(?:github\.com|GITHUB_API\w*|\$\{?\w+\}?)(?::\d+)?/(?:api/v3/)?", "", endpoint)
    path = path.lstrip("/").split("?", 1)[0]
    if method and not _METHOD.match(method):
        _hit(f"{what} with a method that is not known until it runs")
    writes = method in ("PUT", "PATCH", "DELETE")
    posts = method == "POST" or (not method and has_body)
    if path.startswith("graphql") or "graphql" in endpoint:
        if unknown_body:
            _hit(f"{what}: a GraphQL request whose body is not known until it runs (stdin, a variable or a file "
                 "that cannot be read)")
        text = " ".join(words)
        if _GRAPHQL_MUTATION.search(text):
            _hit(f"{what}: a GraphQL mutation that merges, deletes or changes repo settings")
        names = _mutations(text)
        if names is not None and not names <= _SAFE_MUTATIONS:
            _hit(f"{what}: a GraphQL mutation not known to be safe ({', '.join(sorted(names))[:80]})")
        return
    if _dynamic(endpoint):
        if writes or posts:
            _hit(f"{what} write to an endpoint that is not known until it runs")
        return
    if writes or posts:
        verb = method or "POST"
        if _ADMIN_PREFIX.match(path) and not any(verb == safe and pattern.match(path)
                                                 for safe, pattern in _REPO_SAFE_WRITES):
            _hit(f"{what} {verb} {path[:80]} (merge, delete, ref or repo settings change)")
    if writes and not (method != "DELETE" and _API_SAFE_WRITE.match(path)):
        _hit(f"{what} {method} (merge, delete or repo settings change)")
    if posts and _API_SENSITIVE_POST.match(path):
        _hit(f"{what} POST that merges, adds a webhook or key, or changes repo settings")


# --- curl / wget / httpie -----------------------------------------------------------------------------

def _http(program: str, args: list[str], ctx: Ctx) -> None:
    method, body = "", False
    bodies: list[str | None] = []
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
            if "=" in arg and arg.startswith("--"):
                name, item = arg.split("=", 1)
            elif i + 1 < len(args):
                name, item = arg, args[i + 1]
                i += 1
            else:
                name, item = arg, ""
            if name in ("--post-file", "--body-file") or (item.startswith("@") and name not in ("--data-raw",)):
                bodies.append(_body_file(item, ctx))
            elif name in ("-F", "--form") and ("=@" in item or "=<" in item):
                bodies.append(_body_file(item.split("=", 1)[1].lstrip("<"), ctx))
            else:
                bodies.append(item)
        elif arg.startswith("-d") and len(arg) > 2 and not arg.startswith("--"):
            body = True
            item = arg[2:]
            bodies.append(_body_file(item, ctx) if item.startswith("@") else item)
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
        if any(re.match(r"^[\w.-]+(?:=(?!=)|:=|@)", u) for u in urls[1:]):
            method = "POST"
    _http_send(program, method, body, args, urls, ctx)
    if not (github or (unknown_host and method.upper() in ("PUT", "PATCH", "DELETE"))):
        return
    target = next((u for u in urls if _GITHUB_URL.search(u)), urls[0] if urls else "")
    _github_write(method or ("POST" if body else ""), target, body, [*args, *(b for b in bodies if b)], program,
                  unknown_body=any(b is None for b in bodies))


HTTP_PROGRAMS = frozenset({"curl", "wget", "http", "https", "xh", "xhs", "httpie", "wcurl"})
MAIL_PROGRAMS = frozenset({"sendmail", "mail", "mailx", "msmtp", "swaks", "mutt", "neomutt", "s-nail"})
# fix3 #4: the hosts a bot may send to (a non-GET request) without a card: this machine and the package
# registries installs talk to (docs/APPROVALS.md "Sending"); BOT_OFFICE_SEND_ALLOW_HOSTS adds more
SEND_ALLOWED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com", "crates.io",
    "static.crates.io", "index.crates.io", "proxy.golang.org", "sum.golang.org", "rubygems.org",
    "repo.maven.apache.org", "repo1.maven.org"})
_URLISH = re.compile(r"^(?:[a-z][a-z0-9+.-]*://\S+|(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?|"
                     r"localhost(?::\d+)?(?:[/?#]\S*)?|\d+\.\d+\.\d+\.\d+(?::\d+)?(?:[/?#]\S*)?|:\d*(?:/\S*)?)$",
                     re.IGNORECASE)
_HOST = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]*@)?(\[[^\]]*\]|[^:/\s?#]*)", re.IGNORECASE)


def allowed_host(host: str) -> bool:
    """A host on the send allowlist (this machine, the controller, package registries, the owner's list)."""
    host = host.strip("[]").lower().rstrip(".")
    extra = {h.strip().lower() for h in os.environ.get("BOT_OFFICE_SEND_ALLOW_HOSTS", "").split(",") if h.strip()}
    controller = re.match(r"^https?://(\[[^\]]*\]|[^:/]+)", os.environ.get("BOT_OFFICE_API_URL", ""))
    if controller:
        extra.add(controller.group(1).strip("[]").lower())
    return host in SEND_ALLOWED_HOSTS or host in extra


def _http_send(program: str, method: str, body: bool, args: list[str], urls: list[str], ctx: Ctx) -> None:
    """Any request but GET/HEAD/OPTIONS to a host off the allowlist, or to a host not known here, sends."""
    verb = (method or ("POST" if body else "GET")).upper()
    if any(a in ("-G", "--get") for a in args) and not method:
        verb = "GET"
    if verb in ("GET", "HEAD", "OPTIONS"):
        return
    label = f"{verb} request" if _METHOD.match(verb) else "request whose method is not known until it runs"
    targets = [u for u in urls if _URLISH.match(u) or _dynamic(u)]
    if program in ("http", "https", "xh", "xhs", "httpie"):
        targets = targets[:1]
    hosts = []
    for target in targets:
        if _dynamic(target) and not re.match(r"^[a-z]+://[\w.-]+(?:[:/]|$)", target, re.IGNORECASE):
            hosts.append(None)
            continue
        found = _HOST.match(target)
        name = found.group(1) if found else ""
        hosts.append(name or ("localhost" if target.startswith(":") else None))
    outside = [h for h in hosts if h and not allowed_host(h)]
    if outside or not hosts or None in hosts:
        where = outside[0] if outside else "a host not known until it runs"
        ctx.sends.append(f"a {label} to {where} ({program})")


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


def mcp_repo(data: dict) -> tuple[str, str] | None:
    """(owner, repo) a connector call names: owner + repo, or "owner/repo" in repo / repository /
    full_name, or a github.com URL."""
    owner, repo = data.get("owner"), data.get("repo") or data.get("repository") or data.get("full_name")
    if isinstance(owner, str) and isinstance(repo, str) and owner and repo and "/" not in repo:
        return owner, repo
    for value in (repo, data.get("url"), data.get("repo_url")):
        if isinstance(value, str):
            found = re.match(r"^(?:https?://(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", value.strip())
            if found:
                return found.group(1), found.group(2)
    return None


def repo_default(owner: str, repo: str) -> str | None:
    """The default branch of github.com/<owner>/<repo> from the controller's cached lookup, or None
    (no lookup here, no token, not found)."""
    if default_lookup is None or not owner or not repo or _dynamic(owner + repo):
        return None
    try:
        value = default_lookup(owner, repo)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, str) and value else None


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
        branches = [data.get(k) for k in ("branch", "ref", "head", "base") if isinstance((data or {}).get(k), str)]
        branches = [re.sub(r"^(?:refs/)?heads/", "", b.strip()) for b in branches]
        if not branches or any(_DEFAULT_REF.match(b) or b in ("HEAD", "@") or _dynamic(b) for b in branches):
            _hit("a connector tool that commits or pushes to the default branch")
        # fix3 #5: the target repo's real default branch (the controller asks GitHub, cached); unknown asks
        target = mcp_repo(data or {})
        default = repo_default(*target) if target else None
        if default is None:
            _hit("a connector tool that commits or pushes to a repo whose default branch is not known")
        if default in branches:
            _hit(f"a connector tool that commits or pushes to the default branch ({default})")
    if words & {"delete", "remove", "trash"} and words & {"file", "files", "directory", "folder", "path"}:
        strings = []
        _input_strings(data or {}, strings)
        paths = [s for s in strings if "/" in s or s.startswith("~") or ".." in s]
        if not strings or any(_outside(p, ctx) for p in paths):
            _hit("a connector tool that deletes files outside the workspace")


# --- sending to people (the "send" class, locked on like the git guard) ---------------------------

# email, texts, DMs and chat messages to people; not `desk grok send` or bot-msg (they message bots)
SEND_SHELL = re.compile(r"\b(?:sendmail|mutt|msmtp|swaks|mailx)\b|(?:^|[;&|]\s*)mail\s")
SEND_TOOLS = re.compile(r"(?:^|[_\-.:/])(?:send(?:_\w+)?|reply|reply_all|forward|tweet|dm|sms|"
                        r"post_(?:message|comment|reply|tweet|status))(?=[_\-.:/]|$)", re.IGNORECASE)
SEND_KEY = "send"
SEND_LABEL = "Sending email or messages"


# fix3 #4: connector tools that send, by the words of their name (the part after mcp__<server>__)
_SEND_VERBS = frozenset({"send", "post", "reply", "publish", "tweet", "retweet", "forward", "broadcast", "dm",
                         "sms", "email", "mail", "message", "notify", "announce", "comment", "invite", "share",
                         "toot", "skeet", "upvote", "react", "respond"})
_SEND_OBJECTS = frozenset({"message", "messages", "email", "emails", "mail", "comment", "comments", "issue",
                           "issues", "discussion", "tweet", "post", "posts", "status", "reply", "chat", "dm", "sms",
                           "notification", "invite", "invitation", "review"})
_CREATE_VERBS = frozenset({"create", "add", "new", "write", "submit", "make", "open", "start", "schedule", "put"})
_READ_VERBS = frozenset({"get", "list", "read", "search", "fetch", "query", "find", "view", "show", "download",
                         "describe", "lookup", "check", "count", "watch", "export", "preview", "draft", "drafts",
                         "compose", "save", "validate", "render", "parse"})


def _send_tool(tool: str) -> bool:
    action = tool.split("__")[-1] if "__" in tool else re.sub(r"^mcp[_\-.:/]*", "", tool, flags=re.IGNORECASE)
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub("_", action)) if w]
    if not words or words[0] in _READ_VERBS:
        return False
    return bool(set(words) & _SEND_VERBS) or bool(set(words) & _CREATE_VERBS and set(words) & _SEND_OBJECTS)


def send_reason(tool: str, command: str = "", tool_input: dict | None = None, cwd: str | None = None) -> str | None:
    """Why this call sends something out of the machine (email, a message, a post, any non-GET request
    to a host off the allowlist): always asks, in every run. None when it does not."""
    if command and SEND_SHELL.search(command):
        return "sending email or a message"
    if tool and (tool.lower().startswith("mcp") or tool == "MCP") and (SEND_TOOLS.search(tool) or _send_tool(tool)):
        return "sending through a connector"
    if command:
        ctx = Ctx(root=cwd or None, cwd=cwd or None)
        try:
            if len(command) > SCAN_LIMIT:
                return "a command too long to check"
            scan_text(command, ctx)
        except _Hit:
            pass  # the git guard asks for it anyway; what it found so far still counts
        except Exception:  # noqa: BLE001
            return "a command that could not be read"
        if ctx.sends:
            return ctx.sends[0]
    return None


def judge(tool: str, command: str = "", tool_input: dict | None = None, cwd: str | None = None) -> str | None:
    """The git guard, then the send class: why this call needs the owner's card, or None."""
    return classify(tool, command, tool_input, cwd) or send_reason(tool, command, tool_input, cwd)


# --- the pre-push check (bin/bot-office-git-hook reports the real refs) ----------------------------

PUSH_TOOL = "GitPush"
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def classify_push(report: dict | None) -> str | None:
    """Judge the refs a push really updates, as git's pre-push hook sees them: deleting a ref, a
    non-fast-forward (force) update, overwriting a tag, anything outside branches and tags, and the
    remote's default branch (or any branch while that is unknown) ask."""
    if not isinstance(report, dict) or not isinstance(report.get("updates"), list):
        return "a push the check could not read"
    default = report.get("default_branch") if isinstance(report.get("default_branch"), str) else None
    hub = github_repo(normalise_remote(str(report.get("url") or "")))
    if hub and repo_default(*hub):
        default = repo_default(*hub)  # fix3 #5: GitHub's own answer (the controller's cached lookup)
    if len(report["updates"]) > 500:
        return "a push of too many refs to check"
    for update in report["updates"]:
        if not isinstance(update, dict):
            return "a push the check could not read"
        ref, local, remote = (str(update.get(k) or "") for k in ("remote_ref", "local_sha", "remote_sha"))
        if not ref or not _SHA.match(local) or not _SHA.match(remote):
            return "a push the check could not read"
        if set(local) == {"0"}:
            return f"deleting {ref} on the remote"
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/"):]
            if _DEFAULT_REF.match(branch) or branch == default:
                return f"pushing to the default branch ({branch})"
            if not default:
                return f"pushing {branch} to a remote whose default branch is not known"
        elif ref.startswith("refs/tags/"):
            if set(remote) != {"0"}:
                return f"overwriting the tag {ref[len('refs/tags/'):]}"
            continue
        else:
            return f"pushing {ref} (not a branch or a tag)"
        if set(remote) != {"0"} and update.get("fast_forward") is not True:
            return f"a force-push to {branch} (not a fast-forward)"
    return None


def push_commands(command: str) -> list[tuple[str | None, set[str] | None]]:
    """The `git push` commands in *command*: (remote or None, destination branch/tag names or None
    for "not known from the words")."""
    found: list[tuple[str | None, set[str] | None]] = []
    try:
        commands, nested = split_words(command)
        for snippet in nested:
            commands += split_words(snippet)[0]
    except Exception:  # noqa: BLE001
        return [(None, None)] if "push" in command else []
    for words in commands:
        bases = [_base(word) for word in words]
        if "git" not in bases:
            continue
        args = words[bases.index("git") + 1:]
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in _GIT_VALUE_OPTS else 1
        if i >= len(args) or args[i] != "push":
            continue
        positional, skip = [], False
        for arg in args[i + 1:]:
            if skip:
                skip = False
            elif arg.startswith("-"):
                skip = arg in _PUSH_VALUE_OPTS
            else:
                positional.append(arg)
        refs: set[str] | None = set()
        for refspec in positional[1:]:
            name = re.sub(r"^refs/(?:heads|tags)/", "", refspec.lstrip("+").split(":")[-1])
            if not name or name in ("HEAD", "@", "tag") or _dynamic(name) or refs is None:
                refs = None if name != "tag" else refs
                continue
            refs.add(name)
        found.append((positional[0] if positional else None, refs or None))
    return found


# --- fix3 #3: the exact update an approval covers -----------------------------------------------------

def normalise_remote(url: str | None) -> str:
    """One name per repository: host/path in lower case without user, port, scheme or a trailing .git
    (https://u@github.com/O/R.git, git@github.com:O/R, ssh://git@github.com/O/R -> github.com/o/r); a
    local path is file:<realpath>. "" for nothing."""
    url = (url or "").strip()
    if not url:
        return ""
    found = re.match(r"^([a-z][a-z0-9+.-]*)://(?:[^@/]*@)?(\[[^\]]*\]|[^/:]*)(?::\d*)?(/.*)?$", url, re.IGNORECASE)
    if found and found.group(1).lower() == "file":
        return "file:" + os.path.realpath(found.group(3) or "/")
    if found:
        host, path = found.group(2), found.group(3) or ""
    else:
        scp = re.match(r"^(?:[^@/:]+@)?([^/:]{2,}):(?!//)(.*)$", url)
        if not scp:
            return "file:" + os.path.realpath(url)
        host, path = scp.group(1), scp.group(2)
    path = re.sub(r"\.git$", "", path.strip("/")).strip("/")
    return f"{host.lower()}/{path.lower()}"


def github_repo(normalised: str) -> tuple[str, str] | None:
    found = re.fullmatch(r"(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", normalised or "")
    return (found.group(1), found.group(2)) if found else None


def _config(common: str) -> dict[tuple[str, str], dict[str, str]]:
    """The repo's config file as {(section, subsection): {key: last value}} (enough for remotes and
    branches; no includes). Read from the file: no git process."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    try:
        with open(os.path.join(common, "config"), encoding="utf-8", errors="replace") as handle:
            text = handle.read(SCRIPT_LIMIT)
    except OSError:
        return out
    section = ("", "")
    for line in text.splitlines():
        line = line.strip()
        head = re.match(r'^\[\s*([\w.-]+)(?:\s+"((?:\\.|[^"\\])*)")?\s*\]', line)
        if head:
            name, sub = head.group(1).lower(), head.group(2)
            if sub is None and "." in name:
                name, sub = name.split(".", 1)
            section = (name, (sub or "").replace('\\"', '"').replace("\\\\", "\\"))
            continue
        pair = re.match(r"^([A-Za-z][\w-]*)\s*(?:=\s*(.*))?$", line)
        if pair:
            value = re.sub(r"\s+[;#].*$", "", pair.group(2) or "true").strip()
            if len(value) > 1 and value[0] == value[-1] == '"':
                value = value[1:-1]
            out.setdefault(section, {})[pair.group(1).lower()] = value
    return out


def _is_url(remote: str) -> bool:
    return "://" in remote or remote.startswith(("/", "./", "../", "~")) or bool(re.match(r"^[^/:]+@?[^/:]*:", remote))


def remote_url(directory: str | None, remote: str | None) -> str | None:
    """Where `git push <remote>` goes from the repo around *directory*: the remote's pushurl or url
    (read from the config file), or *remote* itself when it is a URL or a path."""
    if not remote or _dynamic(remote):
        return None
    if _is_url(remote):
        return os.path.join(directory or "", remote) if remote.startswith(("./", "../")) else remote
    dirs = _git_dirs(directory)
    if not dirs:
        return None
    entry = _config(dirs[1]).get(("remote", remote), {})
    return entry.get("pushurl") or entry.get("url")


def _read_ref(gitdir: str, common: str, ref: str, depth: int = 0) -> tuple[str | None, str | None]:
    """(full ref name, sha) of *ref* from the files (loose ref, then packed-refs); symbolic refs once."""
    if depth > 3 or ".." in ref.split("/"):
        return None, None
    for base in ((gitdir, common) if ref == "HEAD" or not ref.startswith("refs/") else (common, gitdir)):
        try:
            with open(os.path.join(base, ref), encoding="utf-8") as handle:
                line = handle.read(4096).strip()
        except OSError:
            continue
        if line.startswith("ref: "):
            return _read_ref(gitdir, common, line[5:].strip(), depth + 1)
        if _SHA.match(line):
            return ref, line
    try:
        with open(os.path.join(common, "packed-refs"), encoding="utf-8") as handle:
            for line in handle.read(4 * 1024 * 1024).splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _SHA.match(parts[0]):
                    return ref, parts[0]
    except OSError:
        pass
    return None, None


def resolve_ref(directory: str | None, name: str) -> tuple[str | None, str | None]:
    """(full ref, sha) a push source names, git's lookup order; (None, sha) for a plain sha."""
    dirs = _git_dirs(directory)
    if not dirs or not name or _dynamic(name):
        return None, None
    if _SHA.match(name):
        return None, name
    if name in ("HEAD", "@"):
        return _read_ref(dirs[0], dirs[1], "HEAD")
    for candidate in (name, f"refs/{name}", f"refs/tags/{name}", f"refs/heads/{name}", f"refs/remotes/{name}"):
        if candidate != "HEAD" and (candidate.startswith("refs/") or candidate in ("FETCH_HEAD", "ORIG_HEAD")):
            found = _read_ref(dirs[0], dirs[1], candidate)
            if found[1]:
                return found
    return None, None


def _push_words(command: str) -> list[tuple[str | None, list[str]]]:
    """(-C directory or None, the words after `push`) of every `git push` in *command*."""
    found = []
    commands, nested = split_words(command)
    for snippet in nested:
        commands += split_words(snippet)[0]
    for words in commands:
        bases = [_base(word) for word in words]
        if "git" not in bases:
            continue
        args = words[bases.index("git") + 1:]
        i, directory = 0, None
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C" and i + 1 < len(args):
                directory = args[i + 1] if directory is None else os.path.join(directory, args[i + 1])
            i += 2 if args[i] in _GIT_VALUE_OPTS else 1
        if i < len(args) and args[i] == "push":
            found.append((directory, args[i + 1:]))
    return found


def push_plan(command: str, cwd: str | None) -> list[dict] | None:
    """The exact updates the `git push` commands in *command* would make, read from the repo's files
    now: [{url (normalised), ref (full name), op (update|force|delete), old, new}]. old is the
    remote-tracking ref (zero when there is none: a new branch). None when any part is not known
    here (a pattern, --all/--mirror/--tags, an unresolvable source, an unknown remote): the approval
    then covers no push and the pre-push check asks with the exact update."""
    try:
        pushes = _push_words(command)
    except Exception:  # noqa: BLE001
        return None
    plan: list[dict] = []
    for directory, rest in pushes:
        if directory is not None:
            if _dynamic(directory):
                return None
            directory = directory if os.path.isabs(directory) else os.path.join(cwd or "", directory)
        else:
            directory = cwd
        dirs = _git_dirs(directory)
        if not dirs:
            return None
        force = delete = False
        positional: list[str] = []
        skip = False
        for arg in rest:
            if skip:
                skip = False
            elif arg.startswith("--"):
                name = arg[2:].split("=", 1)[0]
                if name.startswith("force"):
                    force = True
                elif name == "delete":
                    delete = True
                elif name in ("all", "mirror", "tags", "prune", "branches") or name.startswith("follow"):
                    return None
                skip = f"--{name}" in _PUSH_VALUE_OPTS and "=" not in arg
            elif arg.startswith("-") and arg != "-":
                force = force or "f" in arg[1:]
                delete = delete or "d" in arg[1:]
                skip = arg[1:] == "o"
            else:
                positional.append(arg)
        config = _config(dirs[1])
        current = None
        try:
            with open(os.path.join(dirs[0], "HEAD"), encoding="utf-8") as handle:
                line = handle.read(4096).strip()
            current = line[len("ref: refs/heads/"):] if line.startswith("ref: refs/heads/") else None
        except OSError:
            pass
        branch_cfg = config.get(("branch", current or ""), {})
        remote = positional[0] if positional else (branch_cfg.get("pushremote") or
                                                     config.get(("remote", ""), {}).get("pushdefault") or
                                                     branch_cfg.get("remote") or "origin")
        url = remote_url(directory, remote)
        if not url:
            return None
        normalised = normalise_remote(url)
        specs = positional[1:] or ([current] if current and not delete else [])
        if not specs:
            return None
        for spec in specs:
            one_force = force or spec.startswith("+")
            spec = spec.lstrip("+")
            if _dynamic(spec) or any(ch in spec for ch in "*?[{"):
                return None
            if delete:
                src, dst = "", spec
            elif ":" in spec:
                src, dst = spec.split(":", 1)
            else:
                src, dst = spec, spec
            full_src, new = resolve_ref(directory, src) if src else (None, ZERO)
            if src and not new:
                return None
            if not dst or (dst in ("HEAD", "@") and src in ("HEAD", "@")):
                dst = full_src or ""
            if dst.startswith("refs/"):
                full_dst = dst
            elif full_src and full_src.startswith("refs/") and dst == src:
                full_dst = full_src if full_src.startswith(("refs/heads/", "refs/tags/")) else ""
            elif full_src and full_src.startswith("refs/tags/"):
                full_dst = f"refs/tags/{dst}"
            else:
                full_dst = f"refs/heads/{re.sub(r'^heads/', '', dst)}"
            if not full_dst or full_dst in ("refs/heads/HEAD", "refs/heads/@"):
                return None
            old = ZERO
            if full_dst.startswith("refs/heads/") and not _is_url(remote):
                old = _read_ref(dirs[0], dirs[1], f"refs/remotes/{remote}/{full_dst[len('refs/heads/'):]}")[1] or ZERO
            op = "delete" if not src else "force" if one_force else "update"
            plan.append({"url": normalised, "ref": full_dst, "op": op, "old": old, "new": new})
    return plan or None


def git_targets(argv: list[str], cwd: str | None) -> tuple[str | None, list[str]]:
    """(operation, normalised repository URLs) a git or gh call needs the GitHub credential for:
    ("push" | "fetch" | "gh", [...]); (None, []) for a call that does not reach a remote."""
    if not argv:
        return None, []
    program = _base(argv[0])
    args = argv[1:]
    if program == "gh":
        for i, arg in enumerate(args):
            value = args[i + 1] if arg in ("-R", "--repo") and i + 1 < len(args) else (
                arg.split("=", 1)[1] if arg.startswith("--repo=") else None)
            if value:
                found = re.match(r"^(?:https?://)?(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", value)
                return "gh", [f"github.com/{found.group(1).lower()}/{found.group(2).lower()}"] if found else []
        url = remote_url(cwd, "origin")
        return "gh", [normalise_remote(url)] if url else []
    if program != "git":
        return None, []
    i, directory = 0, cwd
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            directory = os.path.join(directory or "", args[i + 1])
        i += 2 if args[i] in _GIT_VALUE_OPTS else 1
    if i >= len(args):
        return None, []
    sub, rest = args[i], args[i + 1:]
    positional = [a for a in rest if not a.startswith("-")]
    if sub == "push":
        op = "push"
    elif sub in ("fetch", "pull", "ls-remote", "clone", "remote", "archive"):
        op = "fetch"
    else:
        return None, []
    if sub == "clone":
        return op, [normalise_remote(positional[0])] if positional else []
    if sub == "remote" and positional[:1] != ["update"]:
        return None, []
    dirs = _git_dirs(directory)
    config = _config(dirs[1]) if dirs else {}
    if sub == "remote" or "--all" in rest or "--multiple" in rest:
        names = positional[1:] if sub == "remote" and len(positional) > 1 else [
            sub_name for (section, sub_name) in config if section == "remote" and sub_name]
        if "--multiple" in rest:
            names = positional
    else:
        current = None
        if dirs:
            try:
                with open(os.path.join(dirs[0], "HEAD"), encoding="utf-8") as handle:
                    line = handle.read(4096).strip()
                current = line[len("ref: refs/heads/"):] if line.startswith("ref: refs/heads/") else None
            except OSError:
                pass
        branch = config.get(("branch", current or ""), {})
        fallback = (branch.get("pushremote") or config.get(("remote", ""), {}).get("pushdefault")
                    if op == "push" else None) or branch.get("remote") or "origin"
        names = [positional[0] if positional else fallback]
    urls = [remote_url(directory, name) for name in names[:20]]
    return op, sorted({normalise_remote(url) for url in urls if url})


def only_pushes(command: str, cwd: str | None) -> bool:
    """*command* is plain `git push` commands and calls the guard has no objection to (no nested
    shell, no options that change git's config or skip its hooks): the pre-push check, which knows
    the exact refs, can judge it."""
    try:
        commands, nested = split_words(command)
    except Exception:  # noqa: BLE001
        return False
    if nested or not commands:
        return False
    pushes = 0
    for words in commands:
        if not words:
            continue
        if _base(words[0]) == "git" and "push" in words:
            before, j = words[1:words.index("push")], 0
            while j < len(before):  # only -C <dir> and --no-pager before `push`
                if before[j] == "-C" and j + 1 < len(before):
                    j += 2
                elif before[j] in ("--no-pager", "-P"):
                    j += 1
                else:
                    return False
            if any(w.startswith(("--no-v", "--exec", "--receive-pack")) or _dynamic(w) for w in words):
                return False
            pushes += 1
            continue
        if judge("Bash", shlex_join(words), None, cwd) is not None:
            return False
    return pushes > 0


def shlex_join(words: list[str]) -> str:
    return " ".join(_shell_quote(word) for word in words)


_PLAN_OPS = {"update":("create", "fast-forward"), "force": ("create", "fast-forward", "force"), "delete": ("delete",)}


def update_op(update: dict) -> str:
    """What one reported update does: delete, create, fast-forward or force."""
    local, remote = str(update.get("local_sha") or ""), str(update.get("remote_sha") or "")
    if set(local) == {"0"}:
        return "delete"
    if set(remote) == {"0"}:
        return "create"
    return "fast-forward" if update.get("fast_forward") is True else "force"


def plan_covers(plan: list[dict] | None, report: dict | None) -> bool:
    """Every update the pre-push check reports is one of *plan*'s, exactly: the same repository
    (normalised URL), full ref name, old and new sha, and an operation the plan allows."""
    if not plan or not isinstance(report, dict) or not isinstance(report.get("updates"), list) or not report["updates"]:
        return False
    url = normalise_remote(str(report.get("url") or ""))
    left = list(plan)
    for update in report["updates"]:
        if not isinstance(update, dict):
            return False
        op = update_op(update)
        match = next((item for item in left if item.get("url") == url and item.get("ref") == update.get("remote_ref")
                      and item.get("old") == update.get("remote_sha") and item.get("new") == update.get("local_sha")
                      and op in _PLAN_OPS.get(str(item.get("op")), ())), None)
        if match is None:
            return False
        left.remove(match)
    return True


def describe_plan(plan: list[dict]) -> str:
    return "; ".join(f"{item['url']} {item['ref']}: {item['old'][:10]} -> {item['new'][:10]} ({item['op']})"
                     for item in plan)


def push_identity(report: dict | None) -> list:
    """What a pre-push approval is bound to: the repository and each update's ref, old and new sha."""
    report = report if isinstance(report, dict) else {}
    updates = report.get("updates") if isinstance(report.get("updates"), list) else []
    return [normalise_remote(str(report.get("url") or "")), sorted(
        [str(u.get("remote_ref")), str(u.get("remote_sha")), str(u.get("local_sha"))] for u in updates
        if isinstance(u, dict))]


# --- fix3 #4: a job with no bot records what it was refused, for the owner's Needs-you ------------

REFUSED_FILE = ".bot-office-refused.jsonl"


def record_refusal(what: str, why: str, kind: str = PRESET) -> None:
    """Append one line to <workspace>/.bot-office-refused.jsonl (the gate file's directory); the
    controller turns it into a Needs-you notice when the job ends. Never raises."""
    import json
    import time
    gate = os.environ.get("BOT_OFFICE_GATE_FILE")
    if not gate:
        return
    path = os.path.join(os.path.dirname(gate), REFUSED_FILE)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 64 * 1024:
            return
        line = json.dumps({"what": str(what)[:600], "why": str(why)[:300], "kind": kind,
                           "at": int(time.time())}) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def readable_event(event) -> bool:
    """A hook event the gate can judge: a dict naming a tool, a command, a file or a known hook
    event. Anything else (garbage, a truncated body, an unknown shape) is denied (fail closed)."""
    if not isinstance(event, dict) or "unparsed" in event:
        return False
    if any(isinstance(event.get(key), str) and event.get(key) for key in ("tool_name", "tool", "toolName")):
        return True
    data = event.get("tool_input") or event.get("input") or event.get("toolInput")
    if isinstance(event.get("command"), (str, list)) or (isinstance(data, dict) and "command" in data):
        return True
    return str(event.get("hook_event_name") or event.get("hookEventName") or "") in (
        "beforeShellExecution", "beforeReadFile", "beforeMCPExecution") and bool(event.get("file_path"))


# --- entry point ----------------------------------------------------------------------------------

def classify(tool: str, command: str = "", tool_input: dict | None = None, cwd: str | None = None) -> str | None:
    """Why this tool call must wait for the owner (a short phrase), or None. Never raises: a call
    it cannot read is a hit."""
    ctx = Ctx(root=cwd or None, cwd=cwd or None)
    if tool == PUSH_TOOL:
        try:
            return classify_push(tool_input)
        except Exception:  # noqa: BLE001
            return "a push the check could not read"
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
