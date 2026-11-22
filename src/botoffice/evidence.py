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
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

MAX_CANDIDATES = 8  # of each kind, per evidence text
URL_TIMEOUT = 4.0  # seconds per connection
URL_BUDGET = 8.0  # seconds for every link of one evidence text
MAX_REDIRECTS = 3
GIT_TIMEOUT = 5.0
REPO_DEPTH = 3
MAX_REPOS = 10
LOG_LIMIT = 64 * 1024 * 1024  # bytes of a job's log that are searched for test output

URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`]+", re.IGNORECASE)
SHA_RE = re.compile(r"(?<![0-9A-Za-z/._-])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![0-9A-Za-z_-])", re.IGNORECASE)
PASSED_RE = re.compile(r"\b(\d+)\s+(passed|passing)\b", re.IGNORECASE)
FAILED_ON_LINE = re.compile(r"\b[1-9]\d*\s+(?:failed|failing|failures?|errors?)\b", re.IGNORECASE)
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

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evidence-url")


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
                 log_path=log, workspaces=Path(data_dir) / "workspaces", owned=owned, network=network)


def check(text: str, scope: Scope) -> str | None:
    """What Bot-Office could check in *text* (a short description), or None when nothing it cites exists."""
    text = (text or "").strip()
    if not text:
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


def regular_file(root: Path, parts: Iterable[str]) -> int | None:
    """Size of the regular file root/parts..., walked without following any link below *root*; None
    when it is missing, a link, or not a regular file."""
    parts = tuple(parts)
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError:
                return None
            os.close(fd)
            fd = nxt
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
            size = regular_file(root, parts)
            if size:
                return f"file {'/'.join(parts)} exists ({size:,} bytes)"
    return None


# --- commits -------------------------------------------------------------------------------------------

def _repos(scope: Scope) -> list[Path]:
    """``.git`` directories (not links, not git files) at most REPO_DEPTH levels below the roots."""
    found: list[Path] = []
    for root in scope.roots:
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        base = len(Path(root).parts)
        for current, dirs, _files in os.walk(root):  # never follows links
            depth = len(Path(current).parts) - base
            if ".git" in dirs:
                git = Path(current) / ".git"
                if not git.is_symlink() and git not in found:
                    found.append(git)
                    if len(found) >= MAX_REPOS:
                        return found
            dirs[:] = [d for d in dirs if d != ".git" and d not in _SKIP_DIRS and depth < REPO_DEPTH]
    return found


def git_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def commit_exists(git_dir: Path, sha: str) -> bool:
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", f"--git-dir={git_dir}",
             "cat-file", "-e", f"{sha}^{{commit}}"],
            env=git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _check_commits(text: str, scope: Scope) -> str | None:
    shas = list(dict.fromkeys(m.group(0).lower() for m in SHA_RE.finditer(URL_RE.sub(" ", text))))[:MAX_CANDIDATES]
    if not shas:
        return None
    for git in _repos(scope):
        for sha in shas:
            if commit_exists(git, sha):
                return f"commit {sha} is in {git.parent.name}"
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
        future = _pool.submit(_fetch, url, scope, deadline)
        try:
            if future.result(timeout=left + 0.5):
                return f"link {url[:200]} answered"
        except (FutureTimeout, Exception):  # noqa: BLE001 - a link that cannot be checked does not count
            continue
    return None


# --- test output ---------------------------------------------------------------------------------------

def _collect(node: object, out: list[str], inside: bool = False) -> None:
    if isinstance(node, str):
        if inside:
            out.append(node)
    elif isinstance(node, list):
        for item in node:
            _collect(item, out, inside)
    elif isinstance(node, dict):
        here = inside or node.get("type") == "tool_result"
        for key, value in node.items():
            _collect(value, out, here or key in OUTPUT_KEYS)


def tool_output(log_text: str) -> str:
    """The tool and command output in a job's CLI log (JSON lines); assistant text is left out."""
    out: list[str] = []
    for line in log_text.splitlines():
        if "pass" not in line or not line.startswith("{"):
            continue
        with contextlib.suppress(ValueError):
            _collect(json.loads(line), out)
    return "\n".join(out)


def _check_tests(text: str, scope: Scope) -> str | None:
    cited = list(dict.fromkeys(m.group(1) + " " + m.group(2).lower() for m in PASSED_RE.finditer(text)))
    if not cited or scope.log_path is None:
        return None
    try:
        with open(scope.log_path, "rb") as handle:  # the controller's own log, not a bot file
            data = handle.read(LOG_LIMIT)
    except OSError:
        return None
    output = tool_output(data.decode("utf-8", errors="replace"))
    for claim in cited:
        pattern = re.compile(rf"\b{re.escape(claim)}\b", re.IGNORECASE)
        for line in output.splitlines():
            if pattern.search(line) and not FAILED_ON_LINE.search(line):
                return f'test output "{claim}" is in this job\'s own command output'
    return None
