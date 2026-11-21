"""Email notifications, controller side: a compact, secret-free event summary pushed to the external
watchdog Worker (ops/watchdog), which dedupes, batches and sends the mail.

What leaves here: counts, bot names, job ids, kinds and a short redacted failure reason. Never a
prompt, a reply, a task title or a secret. The recipient is not known to the controller at all (the
Worker owns it). Classes: needs_you, failed, outage, backup (default on) and done (default off); the
Worker itself raises outage and backup from the heartbeat, so the toggles travel with each push.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import failures, health, needs_you
from .store import JobStore, now

log = logging.getLogger(__name__)

META_KEY = "notify-email"
DEFAULT_PREFS = {"needs_you": True, "failed": True, "outage": True, "backup": True, "done": False}
URL_ENV = "WATCHDOG_URL"
SECRET_FILE_ENV = "WATCHDOG_SECRET_FILE"
DEFAULT_SECRET_FILE = "~/.config/bot-office/watchdog-secret"
INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 8
MAX_ITEMS = 50
FAILED_WINDOW = timedelta(hours=24)
DONE_WINDOW = timedelta(hours=6)
_MAX_SECRET_BYTES = 4096


class ConfigError(Exception):
    pass


def get_prefs(store: JobStore) -> dict[str, bool]:
    try:
        stored = json.loads(store.get_meta(META_KEY) or "{}")
    except ValueError:
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    return {key: stored[key] if isinstance(stored.get(key), bool) else default
            for key, default in DEFAULT_PREFS.items()}


def set_prefs(store: JobStore, changes: dict[str, bool | None]) -> dict[str, bool]:
    prefs = get_prefs(store)
    prefs.update({key: bool(value) for key, value in changes.items() if key in DEFAULT_PREFS and value is not None})
    store.set_meta(META_KEY, json.dumps(prefs))
    return prefs


def _short(text: object, limit: int) -> str:
    return failures.clip(text if isinstance(text, str) else "", limit)


def _job_items(db, query: str, args: tuple) -> list[dict]:
    items = []
    for row in db.execute(query, args):
        items.append({"id": f"j:{row['id']}:{row['attempts']}", "bot": _short(row["name"], 60),
                      "job": str(row["id"])[:40], "kind": _short(row["failure_kind"] or row["status"], 24),
                      "reason": _short(row["error"], 120) if row["status"] != "succeeded" else ""})
    return items


def build_events(store: JobStore, stamp: datetime | None = None) -> dict:
    """The summary the Worker mails from. Only the enabled classes carry items."""
    stamp = stamp or now()
    prefs = get_prefs(store)
    out: dict = {"sent_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "prefs": prefs}
    if prefs["needs_you"]:
        # failed turns are the "failed" class; everything else the owner must act on is needs-you
        items = [item for item in needs_you.needs_you(store) if item["kind"] != "job"]
        out["needs_you"] = {"count": len(items), "items": [
            {"id": _short(item["id"], 80), "bot": _short(item["bot_name"], 60),
             "kind": _short(f"{item['kind']} {item['state']}", 40)} for item in items[:MAX_ITEMS]]}
    cols = "j.id, j.attempts, j.status, j.failure_kind, j.error, b.name"
    with closing(store._connect()) as db:
        if prefs["failed"]:
            where = "j.status IN ('failed','needs_login') AND j.finished_at>=?"
            since = ((stamp - FAILED_WINDOW).isoformat(),)
            count = db.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", since).fetchone()[0]
            out["failed"] = {"count": count, "items": _job_items(
                db, f"SELECT {cols} FROM jobs j LEFT JOIN bots b ON b.id=j.bot_id WHERE {where} "
                    f"ORDER BY j.finished_at DESC LIMIT {MAX_ITEMS}", since)}
        if prefs["done"]:
            where = "j.status='succeeded' AND j.finished_at>=?"
            since = ((stamp - DONE_WINDOW).isoformat(),)
            count = db.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", since).fetchone()[0]
            out["done"] = {"count": count, "items": _job_items(
                db, f"SELECT {cols} FROM jobs j LEFT JOIN bots b ON b.id=j.bot_id WHERE {where} "
                    f"ORDER BY j.finished_at DESC LIMIT {MAX_ITEMS}", since)}
    if prefs["backup"]:
        out["backup_reason"] = health.backup_reason()
    return out


def read_secret(path: Path) -> str:
    """Same rules as scripts/healthz_push.py: one descriptor, regular file, mode 0600, no path in errors."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(f"secret file unreadable: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ConfigError("secret file must be a regular file with mode 0600")
        try:
            secret = os.read(fd, _MAX_SECRET_BYTES + 1).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise ConfigError("secret file unreadable") from None
    finally:
        os.close(fd)
    if not secret or len(secret) > _MAX_SECRET_BYTES:
        raise ConfigError("secret file is empty or too large")
    return secret


def events_url(heartbeat_url: str) -> str:
    """The Worker's /events URL next to its /heartbeat URL."""
    parts = urlsplit(heartbeat_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/events", "", ""))


class Notifier:
    """Pushes the summary every INTERVAL_SECONDS. Unconfigured (no WATCHDOG_URL) it does nothing."""

    def __init__(self, store: JobStore, url: str | None = None, secret_file: Path | None = None,
                 interval: float = INTERVAL_SECONDS) -> None:
        self.store = store
        self.url = (os.environ.get(URL_ENV, "") if url is None else url).strip()
        self.secret_file = secret_file or Path(os.environ.get(SECRET_FILE_ENV) or DEFAULT_SECRET_FILE).expanduser()
        self.interval = interval
        self._last_error: str | None = None

    @property
    def configured(self) -> bool:
        return self.url.startswith("https://")

    def push_once(self) -> bool:
        """One push (blocking: call in a thread). False on any failure; the next push repeats the
        same still-open items, so nothing is lost by a failed one."""
        try:
            secret = read_secret(self.secret_file)
            body = build_events(self.store)
            # a redirect is never followed: the bearer token goes to the configured URL only
            resp = httpx.post(events_url(self.url), json=body, timeout=TIMEOUT_SECONDS, follow_redirects=False,
                              headers={"Authorization": f"Bearer {secret}", "User-Agent": "bot-office-events/1"})
            error = None if resp.status_code == 200 else (
                "redirect refused" if 300 <= resp.status_code < 400 else f"HTTP {resp.status_code}")
        except ConfigError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the class name only; a message could carry a URL or path
            error = type(exc).__name__
        if error != self._last_error:
            (log.warning if error else log.info)("watchdog events push: %s", error or "ok")
            self._last_error = error
        return error is None

    async def run(self) -> None:
        if not self.configured:
            log.info("watchdog events push off (%s is not set to an https URL)", URL_ENV)
            return
        while True:
            await asyncio.to_thread(self.push_once)
            await asyncio.sleep(self.interval)
