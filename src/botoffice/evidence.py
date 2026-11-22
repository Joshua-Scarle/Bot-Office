"""0.12.20: a done's evidence is checked by Bot-Office itself, not taken on the bot's word.

Evidence counts only when it cites something that exists and Bot-Office can check:

- a file inside the bot's own workspace (this turn's job directory, the stable Cursor/Antigravity work
  directory, or the job directory of another of its turns), not one Bot-Office writes into every job
  directory (prompt.txt, reply.txt, SELF.md, memory.md ...): walked from the root one component at a time without following
  links, so ``..`` or a symlink cannot reach outside; it must be a regular, non-empty file. Its content
  is never read.
- a git commit (7 to 40 hex characters, with a digit) in a repository inside those directories:
  ``git cat-file -e <sha>^{commit}`` against that repository's own ``.git`` directory, with no global or
  system config, hooks or fsmonitor.
- a link (http or https, no user name or password in it) that answers 2xx within a few seconds, after at
  most ``MAX_REDIRECTS`` redirects. Every address the host resolves to must be public (no private,
  loopback, link-local, metadata, multicast or reserved address), and the connection goes to the
  address that was checked, so a second DNS answer cannot redirect it. No proxy, cookie or credential is
  used.
- test output: a cited "N passed" (or "N passing") that appears in this job's own captured tool output
  (the command results in the CLI's log, not the bot's reply text), on a line that shows no failure.

:func:`check` returns a short description of the first thing that checked out, or None.
"""
from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import failures

MAX_CANDIDATES = 8  # of each kind, per evidence text
URL_TIMEOUT = 4.0  # seconds per connection
URL_BUDGET = 8.0  # seconds for every link of one evidence text
MAX_REDIRECTS = 3
GIT_TIMEOUT = 5.0
REPO_DEPTH = 3
OBJECT_ENTRIES = 50_000  # entries of a repository's objects/ that are checked for links
MAX_REPOS = 10
LOG_LIMIT = 64 * 1024 * 1024  # bytes of a job's log that are searched for test output

URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`]+", re.IGNORECASE)
SHA_RE = re.compile(r"(?<![0-9A-Za-z/._-])[0-9a-f]{7,40}(?![0-9A-Za-z_-])", re.IGNORECASE)
PASSED_RE = re.compile(r"\b(\d+)\s+(passed|passing)\b", re.IGNORECASE)
# a failure count anywhere in a tool result ("3 failed", "3 tests failed", "1 test case failing", "failures=2")
FAILED_ON_LINE = re.compile(r"\b[1-9]\d*\s+(?:[a-z]+\s+){0,2}?(?:failed|failing|failures?|errors?)\b"
                            r"|\b(?:failed|failures?|errors?)\s*[:=]\s*[1-9]", re.IGNORECASE)
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}$")
_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_EDGE = "\"'`()[]{}<>,;:!?*"
# what Bot-Office writes into every job directory: present in every turn, so citing one proves nothing
GENERATED = frozenset({"prompt.txt", "prompt-resume.txt", "reply.txt", "SELF.md", "memory.md", "instructions.md",
                       "shared-memory.md", "history.md", "worklog.md", "WORKING.md", "DESKTOP.md", "SYSTEM-PROMPT.md",
                       "RULES-FOR-ALL-BOTS.md"})
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".cache", "reset-archive"}
# tool-output keys in the engines' JSON logs (Claude tool_result / tool_use_result, Codex command items,
# OpenCode tool state, Cursor tool results); assistant text ("text", Claude's "result") is never searched
OUTPUT_KEYS = {"tool_use_result", "aggregated_output", "output", "stdout", "stderr", "formatted_output"}
# what the model wrote (a tool call's input or arguments, an assistant message) is never searched, even
# where it has an "output" key
INPUT_KEYS = {"input", "args", "arguments", "parameters"}
EXIT_KEYS = ("exit_code", "exitCode", "exit_status", "returncode")

MAX_LINK_THREADS = 8  # link checks still running (a stalled DNS lookup cannot be cancelled); more: none start
_link_threads = threading.BoundedSemaphore(MAX_LINK_THREADS)


@dataclass
class Scope:
    """Where the evidence of one job may point. *roots*: directories a cited file or repository may be in
    (a relative path is tried in each, the job's own directory first). *owned*: for an absolute path
    under *workspaces* (data/workspaces/<job id>/...), whether that job directory is one of this bot's.
    *log_path*: the job's captured CLI output; None when it is not written yet."""
    roots: list[Path]
    log_path: Path | None = None
    workspaces: Path | None = None
    owned: Callable[[str], bool] | None = None
    network: bool = True
    resolve: Callable[..., list] = field(default=socket.getaddrinfo, repr=False)
    # Bot-Office's own directory the roots are in (the data dir): opened as is, and every component
    # below it, the roots' own included, is opened without following links (None: the roots' parents)
    base: Path | None = None


def job_scope(data_dir: Path, store: object, job: object, *, network: bool = True) -> Scope:
    """The scope of a bot job (*job*: a Job with ``id`` and ``bot_id``; *store*: the JobStore). Its own job
    directory, the bot's stable Cursor/Antigravity work directories, any other job directory of the same
    bot, and the job's CLI log (written when the CLI exits)."""
    bot_id = str(job.bot_id)
    bot_dir = Path(data_dir) / "bots" / bot_id

    def owned(name: str) -> bool:
        try:
            other = store.get(name)
        except Exception:  # noqa: BLE001 - not a job id: not one of this bot's directories
            return False
        return other is not None and str(other.bot_id) == bot_id

    log = Path(data_dir) / "logs" / f"{job.id}.log"
    return Scope(roots=[Path(data_dir) / "workspaces" / str(job.id), bot_dir / "cursor-workspace",
                        bot_dir / "antigravity-workspace"],
                 log_path=log, workspaces=Path(data_dir) / "workspaces", owned=owned, network=network,
                 base=Path(data_dir))


def check(text: str, scope: Scope) -> str | None:
    """What Bot-Office could check in *text* (a short description), or None when nothing it cites exists."""
    text = (text or "").strip()
    if not text or failures.failed_check(text):  # "tests failed; see report.txt" names a failure, not a check
        return None
    if failures.admitted_incomplete(text, text):  # "I couldn't do it; wrote report.md" is not evidence of a done
        return None
    return (_check_tests(text, scope) or _check_files(text, scope) or _check_commits(text, scope)
            or (_check_urls(text, scope) if scope.network else None))


# --- files ---------------------------------------------------------------------------------------------

def _path_candidates(text: str) -> list[str]:
    stripped = URL_RE.sub(" ", text)
    found = re.findall(r"`([^`\n]{1,512})`", stripped)
    for token in stripped.split():
        token = token.strip(_EDGE).rstrip(".")
        if token and ("/" in token or _EXTENSION.search(token)) and len(token) <= 1024:
            found.append(token)
    out: list[str] = []
    for item in found:
        item = _LINE_SUFFIX.sub("", item.strip().strip(_EDGE))
        if item and item not in out and "\x00" not in item:
            out.append(item)
    return out[:MAX_CANDIDATES * 2]


def _inside(path: str, root: Path) -> tuple[str, ...] | None:
    """Components of *path* below *root* (lexically, after removing ``.``), or None if it is not below it.
    A ``..`` component anywhere is refused rather than resolved."""
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    if path.startswith("/"):
        root_parts = [p for p in str(root).split("/") if p]
        if parts[: len(root_parts)] != root_parts:
            return None
        parts = parts[len(root_parts):]
    return tuple(parts) or None


def open_dir(root: Path, parts: Iterable[str], base: Path | None = None) -> int | None:
    """A descriptor of the directory root/parts..., opened one component at a time from *base* (else the
    root's parent) without following any link: a root, or a directory between *base* and it, swapped for
    a link is refused. None when that fails or *root* is not below *base*."""
    root = Path(root)
    if base is None:
        start, below = root.parent, (root.name,)
    else:
        try:
            start, below = Path(base), root.relative_to(base).parts
        except ValueError:
            return None
    if not below or ".." in below:
        return None
    try:
        fd = os.open(start, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    for part in (*below, *parts):
        try:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except OSError:
            os.close(fd)
            return None
        os.close(fd)
        fd = nxt
    return fd


def regular_file(root: Path, parts: Iterable[str], base: Path | None = None) -> int | None:
    """Size of the regular file root/parts..., walked without following any link (``open_dir``); None
    when it is missing, a link, or not a regular file."""
    parts = tuple(parts)
    fd = open_dir(root, parts[:-1], base)
    if fd is None:
        return None
    try:
        try:
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except OSError:
            return None
        return info.st_size if stat.S_ISREG(info.st_mode) else None
    finally:
        os.close(fd)


def _roots_for(path: str, scope: Scope) -> list[Path]:
    roots = list(scope.roots)
    if path.startswith("/") and scope.workspaces is not None and scope.owned is not None:
        below = _inside(path, scope.workspaces)
        if below and len(below) > 1 and scope.owned(below[0]):
            roots.append(scope.workspaces / below[0])
    return roots


def _check_files(text: str, scope: Scope) -> str | None:
    for candidate in _path_candidates(text):
        for root in _roots_for(candidate, scope):
            parts = _inside(candidate, root)
            if not parts or (len(parts) == 1 and (parts[0] in GENERATED or parts[0].startswith(".bot-office"))):
                continue
            size = regular_file(root, parts, scope.base)
            if size:
                return f"file {'/'.join(parts)} exists ({size:,} bytes)"
    return None


# --- commits -------------------------------------------------------------------------------------------

def _repos(scope: Scope) -> list[tuple[Path, tuple[str, ...]]]:
    """``.git`` directories (not links, not git files) at most REPO_DEPTH levels below the roots, as
    (root, components below it)."""
    found: list[tuple[Path, tuple[str, ...]]] = []
    for root in scope.roots:
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        base = len(Path(root).parts)
        for current, dirs, _files in os.walk(root):  # never follows links
            depth = len(Path(current).parts) - base
            if ".git" in dirs:
                git = (Path(root), (*Path(current).parts[base:], ".git"))
                if not (Path(current) / ".git").is_symlink() and git not in found:
                    found.append(git)
                    if len(found) >= MAX_REPOS:
                        return found
            dirs[:] = [d for d in dirs if d != ".git" and d not in _SKIP_DIRS and depth < REPO_DEPTH]
    return found


def git_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C",
            "GIT_NO_REPLACE_OBJECTS": "1",
            # the repository's own config is the bot's: a partial clone would fetch a missing object through
            # its remote (ext::, uploadpack, sshCommand: a command). No transport is allowed (this overrides
            # any protocol.*.allow in that config), and no lazy fetch (Git 2.45+)
            "GIT_ALLOW_PROTOCOL": "none", "GIT_NO_LAZY_FETCH": "1", "GIT_PROTOCOL_FROM_USER": "0"}


def _no_links(fd: int, limit: int = OBJECT_ENTRIES) -> bool:
    """True when nothing below the directory open at *fd* is a link (walked by descriptor, at most
    *limit* entries: a bigger tree is not checked and does not count)."""
    seen = 0
    try:
        for _path, dirs, files, dir_fd in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
            for name in (*dirs, *files):
                seen += 1
                if seen > limit or stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                    return False
    except OSError:
        return False
    return True


def _own_objects(fd: int) -> bool:
    """True when the repository open at *fd* keeps its objects itself: objects/ is a real directory with
    no link anywhere below it (a linked pack/ or loose-object directory could lend another repository's
    objects), and no alternates or commondir point Git elsewhere."""
    try:
        objects = os.open("objects", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    except OSError:
        return False
    try:
        if not _no_links(objects):
            return False
    finally:
        os.close(objects)
    for name in ("objects/info/alternates", "objects/info/http-alternates", "commondir"):
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


def commit_exists(root: Path, parts: tuple[str, ...], sha: str, base: Path | None = None) -> bool:
    """Whether *sha* names a commit of the repository root/parts (the ``.git`` directory, walked without
    following links and pinned by its descriptor, so it cannot be swapped between the checks and the Git run)."""
    fd = open_dir(root, parts, base)
    if fd is None:
        return False
    try:
        if not _own_objects(fd):
            return False
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", f"--git-dir=/proc/self/fd/{fd}",
             "cat-file", "-e", f"{sha}^{{commit}}"],
            env=git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(fd,), timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.close(fd)
    return done.returncode == 0


def _check_commits(text: str, scope: Scope) -> str | None:
    shas = list(dict.fromkeys(m.group(0).lower() for m in SHA_RE.finditer(URL_RE.sub(" ", text))))[:MAX_CANDIDATES]
    if not shas:
        return None
    for root, parts in _repos(scope):
        for sha in shas:
            if commit_exists(root, parts, sha, scope.base):
                return f"commit {sha} is in {(root.joinpath(*parts[:-1])).name}"
    return None


# --- links ---------------------------------------------------------------------------------------------

class _Pinned(http.client.HTTPSConnection):
    """An HTTP(S) connection to an address that was already checked, with the URL's host name for the
    Host header and TLS (SNI and certificate)."""

    def __init__(self, host: str, address: str, port: int, secure: bool, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address, self._secure = address, secure

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host) if self._secure else sock


def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local
                                 or ip.is_reserved or ip.is_unspecified)


def _fetch(url: str, scope: Scope, deadline: float) -> bool:
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return False
        secure = parts.scheme.lower() == "https"
        try:
            port = parts.port or (443 if secure else 80)
        except ValueError:
            return False
        try:
            answers = scope.resolve(parts.hostname, port, type=socket.SOCK_STREAM)
        except OSError:
            return False
        addresses = [str(a[4][0]) for a in answers]
        if not addresses or not all(public_address(a) for a in addresses):
            return False  # every answer must be public: no private, loopback, link-local or metadata address
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        conn = _Pinned(parts.hostname, addresses[0], port, secure, min(URL_TIMEOUT, left))
        try:
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            conn.request("GET", path, headers={"User-Agent": "Bot-Office evidence check", "Accept": "*/*"})
            response = conn.getresponse()
            status, location = response.status, response.getheader("Location")
        except (OSError, http.client.HTTPException, ValueError):
            return False
        finally:
            conn.close()
        if 200 <= status < 300:
            return True
        if status in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            continue
        return False
    return False


def _check_urls(text: str, scope: Scope) -> str | None:
    urls = list(dict.fromkeys(u.rstrip(".,;:!?)]}'\"") for u in URL_RE.findall(text)))[:MAX_CANDIDATES]
    deadline = time.monotonic() + URL_BUDGET
    for url in urls:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        if _fetch_bounded(url, scope, deadline, left + 0.5):
            return f"link {url[:200]} answered"
    return None


def _fetch_bounded(url: str, scope: Scope, deadline: float, wait: float) -> bool:
    """``_fetch`` in its own daemon thread, waited for at most *wait* seconds. A lookup that stalls past
    that keeps only its own thread (never one a later check needs, never shutdown); while
    MAX_LINK_THREADS are still running, no link is checked (it does not count)."""
    if not _link_threads.acquire(blocking=False):
        return False
    answer: list[bool] = []

    def run() -> None:
        try:
            answer.append(bool(_fetch(url, scope, deadline)))
        except Exception:  # noqa: BLE001 - a link that cannot be checked does not count
            answer.append(False)
        finally:
            _link_threads.release()

    worker = threading.Thread(target=run, name="evidence-url", daemon=True)
    try:
        worker.start()
    except RuntimeError:  # no thread to be had: the link is not checked
        _link_threads.release()
        return False
    worker.join(wait)
    return bool(answer and answer[0])


# --- test output ---------------------------------------------------------------------------------------

def _collect(node: object, out: list[str], inside: bool = False) -> bool:
    """Append the tool-output strings in *node* to *out*; True when *node* reports a failed run
    (is_error, a nonzero exit code, status failed/error). What the model wrote is skipped."""
    failed = False
    if isinstance(node, str):
        if inside:
            out.append(node)
    elif isinstance(node, list):
        for item in node:
            failed |= _collect(item, out, inside)
    elif isinstance(node, dict):
        if node.get("type") == "assistant" or node.get("role") == "assistant":
            return False
        failed = node.get("is_error") is True or str(node.get("status", "")).lower() in ("failed", "error")
        for key in EXIT_KEYS:
            code = node.get(key)
            failed |= isinstance(code, int) and not isinstance(code, bool) and code != 0
        here = inside or node.get("type") == "tool_result"
        for key, value in node.items():
            if key not in INPUT_KEYS:
                failed |= _collect(value, out, here or key in OUTPUT_KEYS)
    return failed


def tool_output(log_text: str) -> list[tuple[str, bool]]:
    """The tool and command results in a job's CLI log (JSON lines; each engine writes one result per
    line), as (output, the run failed); assistant text and tool inputs are left out."""
    results: list[tuple[str, bool]] = []
    for line in log_text.splitlines():
        if "pass" not in line or not line.startswith("{"):
            continue
        out: list[str] = []
        with contextlib.suppress(ValueError):
            failed = _collect(json.loads(line), out)
            if out:
                results.append(("\n".join(out), failed))
    return results


def _check_tests(text: str, scope: Scope) -> str | None:
    cited = list(dict.fromkeys(m.group(1) + " " + m.group(2).lower() for m in PASSED_RE.finditer(text)))
    if not cited or scope.log_path is None:
        return None
    try:
        with open(scope.log_path, "rb") as handle:  # the controller's own log, not a bot file
            data = handle.read(LOG_LIMIT)
    except OSError:
        return None
    results = tool_output(data.decode("utf-8", errors="replace"))
    for claim in cited:
        pattern = re.compile(rf"\b{re.escape(claim)}\b", re.IGNORECASE)
        for output, failed in results:
            # the whole result must pass: no failure count anywhere in it, and no failed exit
            if not failed and pattern.search(output) and not FAILED_ON_LINE.search(output):
                return f'test output "{claim}" is in this job\'s own command output'
    return None
