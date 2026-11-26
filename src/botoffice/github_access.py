"""0.12.4 fix3: the controller's GitHub token, used without handing it to a job on request.

- ``Tickets``: a one-time ticket, valid TICKET_SECONDS, bound to one job, one repository
  (normalised URL) and one operation (push, fetch, gh). The controller issues it only for an
  operation it allows (rules.Rules.issue_ticket); bin/bot-office-credential (git) and the gh shim
  exchange it for the token, once. A call with only the job's agent token gets nothing (403).
- ``DefaultBranches``: the default branch of a github.com repo from the GitHub API, cached, for the
  git guard (git_guard.default_lookup): MCP pushes, pushes and the pre-push check. Unknown asks.

Stdlib only. Never logs or returns the token except from ``Tickets.redeem``'s caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

TICKET_SECONDS = 120
TICKETS_PER_JOB = 300  # issued over a job's life (a loop asking for tickets stops here)
OPS = ("push", "fetch", "gh")
DEFAULT_TTL = 3600
UNKNOWN_TTL = 300
API_TIMEOUT = 4


@dataclass
class Ticket:
    job_id: str
    repo: str  # normalised URL (git_guard.normalise_remote), "" for a gh call with no repo
    op: str
    expires: float


class Tickets:
    def __init__(self) -> None:
        self._open: dict[str, Ticket] = {}
        self._issued: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(ticket: str) -> str:
        return hashlib.sha256(ticket.encode()).hexdigest()

    def issue(self, job_id: str, repo: str, op: str) -> str | None:
        """A new ticket, or None when this job has had TICKETS_PER_JOB."""
        if op not in OPS:
            raise ValueError("unknown operation")
        stamp = time.monotonic()
        with self._lock:
            self._open = {k: v for k, v in self._open.items() if v.expires > stamp}
            if self._issued.get(job_id, 0) >= TICKETS_PER_JOB:
                return None
            self._issued[job_id] = self._issued.get(job_id, 0) + 1
            ticket = secrets.token_urlsafe(24)
            self._open[self._key(ticket)] = Ticket(job_id, repo, op, stamp + TICKET_SECONDS)
        return ticket

    def redeem(self, ticket: str, job_id: str, repo: str | None) -> Ticket | None:
        """Use *ticket* once: it must be this job's, unexpired and, when *repo* is given (git says
        which repository it needs the credential for), for that repository."""
        if not isinstance(ticket, str) or not ticket or len(ticket) > 100:
            return None
        with self._lock:
            found = self._open.get(self._key(ticket))
            if found is None or found.job_id != job_id or found.expires < time.monotonic():
                return None
            if repo is not None and found.repo != repo:
                return None
            del self._open[self._key(ticket)]
        return found

    def forget(self, job_id: str) -> None:
        with self._lock:
            self._open = {k: v for k, v in self._open.items() if v.job_id != job_id}
            self._issued.pop(job_id, None)


def _fetch_default(api: str, owner: str, repo: str, token: str) -> str | None:
    url = f"{api.rstrip('/')}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "Accept": "application/vnd.github+json",
                                                   "User-Agent": "bot-office"})
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            data = json.loads(response.read(1024 * 1024).decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    value = data.get("default_branch") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


class DefaultBranches:
    """Cached GitHub default branches: DEFAULT_TTL for an answer, UNKNOWN_TTL for none."""

    def __init__(self, token: Callable[[], str | None], fetch: Callable[[str, str, str, str], str | None] | None = None,
                 api: str | None = None):
        self._token = token
        self._fetch = fetch or _fetch_default
        self._api = api or os.environ.get("BOT_OFFICE_GITHUB_API", "https://api.github.com")
        self._cache: dict[tuple[str, str], tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    def __call__(self, owner: str, repo: str) -> str | None:
        key = (owner.lower(), repo.lower().removesuffix(".git"))
        stamp = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > stamp:
                return cached[1]
        token = self._token()
        if not token:
            return None
        value = self._fetch(self._api, key[0], key[1], token)
        with self._lock:
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[key] = (stamp + (DEFAULT_TTL if value else UNKNOWN_TTL), value)
        return value


def _fetch_visibility(api: str, owner: str, repo: str, token: str) -> str | None:
    """0.12.42: "public", "private" or "internal" from GET /repos/<owner>/<repo>, or None."""
    url = f"{api.rstrip('/')}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "Accept": "application/vnd.github+json",
                                                   "User-Agent": "bot-office"})
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            data = json.loads(response.read(1024 * 1024).decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("visibility")
    if value in ("public", "private", "internal"):
        return value
    private = data.get("private")
    return "private" if private is True else "public" if private is False else None


class Visibility(DefaultBranches):
    """0.12.42: cached repo visibility for git_guard.visibility_lookup (pushes to public repos ask)."""

    def __init__(self, token: Callable[[], str | None], fetch: Callable[[str, str, str, str], str | None] | None = None,
                 api: str | None = None):
        super().__init__(token, fetch or _fetch_visibility, api)


class Login:
    """0.12.42: the GitHub login of the controller's token (GET /user), cached: the owner whose private
    repos count as the owner's own in the approval policy (policy.owner_lookup)."""

    def __init__(self, token: Callable[[], str | None], api: str | None = None):
        self._token = token
        self._api = api or os.environ.get("BOT_OFFICE_GITHUB_API", "https://api.github.com")
        self._cached: tuple[float, str | None] = (0.0, None)
        self._lock = threading.Lock()

    def __call__(self) -> str | None:
        stamp = time.monotonic()
        with self._lock:
            if self._cached[0] > stamp:
                return self._cached[1]
        token = self._token()
        value = None
        if token:
            request = urllib.request.Request(f"{self._api.rstrip('/')}/user", headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "bot-office"})
            try:
                with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
                    data = json.loads(response.read(256 * 1024).decode())
                login = data.get("login") if isinstance(data, dict) else None
                value = login if isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) else None
            except (urllib.error.URLError, OSError, ValueError):
                value = None
        with self._lock:
            self._cached = (stamp + (DEFAULT_TTL if value else UNKNOWN_TTL), value)
        return value
