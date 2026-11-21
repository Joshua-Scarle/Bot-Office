"""Email notifications, controller side: a durable event log pushed to the external watchdog Worker
(ops/watchdog), which batches and mails it.

What leaves here: fixed reason codes, ids, counts and a bot label, never free text. Failure kinds and
backup stages are enums; a bot name goes out only when it matches a strict pattern (one short token),
otherwise as "bot-<6 hex of a hash of its id>". No prompt, reply, title, error text or secret is ever read
into the payload. The recipient is not known here at all (the Worker owns it).

Delivery: events are APPENDED to the notify_events table when they happen (SQL triggers on job
status changes and new questions; `record_needs_you` for the other needs-you states, which are
derived), so a short-lived or old item is never lost to polling. They are pushed in order, in batches,
and removed from the queue only when the Worker acknowledges a sequence number (a durable cursor in
`meta`). A push outage, however long, only delays; the Worker takes at most what it can hold and
acknowledges just that, the rest stays queued here. Classes: needs_you, failed, outage, backup (default
on), done (default off); outage and backup are raised by the Worker from the heartbeat.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import stat
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import failures, health, needs_you
from .store import JobStore, now

log = logging.getLogger(__name__)

META_KEY = "notify-email"
CURSOR_KEY = "notify-cursor"
EPOCH_KEY = "notify-epoch"
DEFAULT_PREFS = {"needs_you": True, "failed": True, "outage": True, "backup": True, "done": False}
EVENT_CLASSES = ("needs_you", "failed", "done")
URL_ENV = "WATCHDOG_URL"
SECRET_FILE_ENV = "WATCHDOG_SECRET_FILE"
DEFAULT_SECRET_FILE = "~/.config/bot-office/watchdog-secret"
INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 8
BATCH = 100  # events per push; the rest stays queued and goes in the next push
MAX_PUSHES_PER_CYCLE = 20
KEEP_ACKED = timedelta(days=14)
KEEP_UNACKED = timedelta(days=60)  # an outage longer than this loses the oldest events (counted in the README)
KEEP_UNCONFIGURED = timedelta(days=1)  # nothing is pushed: no backlog of old events is built up for later
_MAX_SECRET_BYTES = 4096

JOB_KINDS = frozenset(failures.KINDS) | {"needs_login", "succeeded", "failed", "cancelled", "incomplete"}
_CODE = re.compile(r"^[a-z][a-z_]{0,31}$")
_BOT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_KEY = re.compile(r"^[A-Za-z0-9:_.-]{1,80}$")


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


def kind_code(value: object, cls: str) -> str:
    """A fixed code: a known failure kind for job events, a short lowercase token for needs-you kinds."""
    text = value if isinstance(value, str) else ""
    if cls == "needs_you":
        return text if _CODE.match(text) else "other"
    return text if text in JOB_KINDS else "other"


def bot_label(bot_id: object, name: object) -> str:
    """The bot's name only if it is one short plain token; anything else is a hash of its id."""
    if isinstance(name, str) and _BOT.match(name):
        return name
    return "bot-" + hashlib.sha256(str(bot_id).encode()).hexdigest()[:6]


def record_needs_you(store: JobStore) -> int:
    """Append every currently derived needs-you item (other than failed turns, which are the "failed"
    class) not logged yet. Idempotent by item id. Returns how many were new."""
    if not get_prefs(store)["needs_you"]:
        return 0
    items = [item for item in needs_you.needs_you(store) if item["kind"] != "job"]
    stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    new = 0
    with closing(store._connect()) as db:
        for item in items:
            key = item["id"]
            if not isinstance(key, str) or not _KEY.match(key):
                continue
            kind = kind_code(f"{item['kind']}_{item['state']}", "needs_you")
            cur = db.execute("INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at) "
                             "VALUES (?,?,?,?,?,?)", (key, "needs_you", item["bot_id"], None, kind, stamp))
            new += cur.rowcount
    return new


def _epoch(store: JobStore) -> str:
    epoch = store.get_meta(EPOCH_KEY)
    if not epoch:
        epoch = secrets.token_hex(8)
        store.set_meta(EPOCH_KEY, epoch)
    return epoch


def _cursor(store: JobStore) -> int:
    try:
        return max(0, int(store.get_meta(CURSOR_KEY) or 0))
    except ValueError:
        return 0


def build_batch(store: JobStore, stamp=None) -> tuple[dict, int, bool]:
    """(payload, upto, more): the next events after the cursor. Events of a class that is switched off
    are skipped (covered by `upto`, never sent). Payload fields are codes, ids and counts only."""
    stamp = stamp or now()
    prefs = get_prefs(store)
    cursor = _cursor(store)
    with closing(store._connect()) as db:
        rows = db.execute("SELECT e.seq, e.key, e.cls, e.bot_id, e.ref, e.kind, b.name FROM notify_events e "
                          "LEFT JOIN bots b ON b.id=e.bot_id WHERE e.seq>? ORDER BY e.seq LIMIT ?",
                          (cursor, BATCH + 1)).fetchall()
    more = len(rows) > BATCH
    rows = rows[:BATCH]
    events = [{"seq": row["seq"], "id": row["key"], "cls": row["cls"], "bot": bot_label(row["bot_id"], row["name"]),
               "job": row["ref"] if isinstance(row["ref"], str) and _REF.match(row["ref"]) else None,
               "kind": kind_code(row["kind"], row["cls"])}
              for row in rows if row["cls"] in EVENT_CLASSES and prefs[row["cls"]] and _KEY.match(row["key"])]
    upto = rows[-1]["seq"] if rows else cursor
    payload = {"sent_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "epoch": _epoch(store), "prefs": prefs,
               "backup_reason": health.backup_reason() if prefs["backup"] else None,
               "upto": upto, "events": events}
    return payload, upto, more


def prune(store: JobStore, keep: timedelta) -> None:
    with closing(store._connect()) as db:
        db.execute("DELETE FROM notify_events WHERE created_at<?", ((now() - keep).strftime("%Y-%m-%dT%H:%M:%SZ"),))


def _ack(store: JobStore, ack: object, upto: int) -> None:
    cursor = _cursor(store)
    if isinstance(ack, int) and not isinstance(ack, bool) and cursor < ack <= upto:
        store.set_meta(CURSOR_KEY, str(ack))
        with closing(store._connect()) as db:
            db.execute("DELETE FROM notify_events WHERE seq<=? AND created_at<?",
                       (ack, (now() - KEEP_ACKED).strftime("%Y-%m-%dT%H:%M:%SZ")))


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
    """Detects, then pushes the queue every INTERVAL_SECONDS (and again at once while more is queued).
    Unconfigured (no WATCHDOG_URL) it does nothing; events still accumulate in the table for later."""

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

    def push_once(self) -> tuple[bool, bool]:
        """One detection pass and one push (blocking: call in a thread). (ok, more queued). A failed or
        unacknowledged push changes nothing: the same events go again next time."""
        more = False
        try:
            secret = read_secret(self.secret_file)
            record_needs_you(self.store)
            prune(self.store, KEEP_UNACKED)
            body, upto, more = build_batch(self.store)
            # a redirect is never followed: the bearer token goes to the configured URL only
            resp = httpx.post(events_url(self.url), json=body, timeout=TIMEOUT_SECONDS, follow_redirects=False,
                              headers={"Authorization": f"Bearer {secret}", "User-Agent": "bot-office-events/1"})
            error = None if resp.status_code == 200 else (
                "redirect refused" if 300 <= resp.status_code < 400 else f"HTTP {resp.status_code}")
            if error is None:
                try:
                    ack = resp.json().get("ack")
                except (ValueError, AttributeError):
                    ack = None
                before = _cursor(self.store)
                _ack(self.store, ack, upto)
                after = _cursor(self.store)
                # push again at once only while the Worker took something and more is queued
                more = (more or after < upto) and after > before
            else:
                more = False
        except ConfigError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the class name only; a message could carry a URL or path
            error = type(exc).__name__
        if error != self._last_error:
            (log.warning if error else log.info)("watchdog events push: %s", error or "ok")
            self._last_error = error
        return error is None, error is None and more

    async def run(self) -> None:
        if not self.configured:
            log.info("watchdog events push off (%s is not set to an https URL)", URL_ENV)
            while True:  # the triggers still log events; keep only the last day so enabling it later is not a flood
                await asyncio.to_thread(prune, self.store, KEEP_UNCONFIGURED)
                await asyncio.sleep(3600)
        while True:
            for _ in range(MAX_PUSHES_PER_CYCLE):
                ok, more = await asyncio.to_thread(self.push_once)
                if not (ok and more):
                    break
            await asyncio.sleep(self.interval)
