"""Email notifications, controller side: a durable event log pushed to the external watchdog Worker
(ops/watchdog), which batches and mails it.

What leaves here: fixed reason codes, hashed ids, counts and a pseudonymous bot label, never free text.
Failure kinds and backup stages are enums; a bot is always "bot-<6 hex of sha256(bot id)>", its name is
never read; an event id is a hash of the internal key (which may hold any character). No prompt, reply,
title, name, error text or secret is read into the payload. The recipient is not known here at all (the
Worker owns it).

Delivery: events are APPENDED to the notify_events table when they happen (SQL triggers on job
status changes, questions, assignments, approvals, tripwires, usage parking, watchers and budget holds;
`record_needs_you` only for overdue tasks, which no write announces), so a short-lived item is never
lost to polling. They are pushed in order, in batches,
and removed from the queue only when the Worker acknowledges a sequence number (a durable cursor in
`meta`). A push outage, however long, only delays; the Worker takes at most what it can hold and
acknowledges just that, the rest stays queued here. An unacknowledged event is NEVER deleted by age: past
BACKLOG_LIMIT the queue is kept and surfaced (a Needs-you item, `notify_backlog` in /healthz, a log line). Classes: needs_you, failed, outage, backup (default
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
FREEZE_KEY = "notify-freeze"
OFF_FILE = "NOTIFY_OFF"  # in the data dir: no new detection, nothing past the sequence seen first is pushed
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
BACKLOG_LIMIT = 1000  # unacknowledged events past this are kept and reported, never dropped
_MAX_SECRET_BYTES = 4096

JOB_KINDS = frozenset(failures.KINDS) | {"needs_login", "succeeded", "failed", "cancelled", "incomplete"}
_CODE = re.compile(r"^[a-z][a-z_]{0,31}$")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


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


def bot_label(bot_id: object) -> str:
    """The only thing that names a bot outside the controller: a hash of its id, never its name."""
    return "bot-" + hashlib.sha256(str(bot_id).encode()).hexdigest()[:6]


def event_id(key: str) -> str:
    """Opaque id of an event: a hash of its internal key, so any character in the key is safe."""
    return "e" + hashlib.sha256(key.encode()).hexdigest()[:16]


def record_needs_you(store: JobStore) -> int:
    """Append overdue assignments not logged yet (idempotent by item id). Every other needs-you state is
    logged by a trigger at the write that causes it (store._init_notify_triggers). Returns how many were new."""
    if not get_prefs(store)["needs_you"]:
        return 0
    items = [item for item in needs_you.needs_you(store) if item["kind"] == "assignment" and item["state"] == "overdue"]
    stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    new = 0
    with closing(store._connect()) as db:
        for item in items:
            # one per assignment per overdue episode (a new due time is a new episode)
            due = db.execute("SELECT due_at FROM assignments WHERE id=?", (item["assignment_id"],)).fetchone()
            cur = db.execute("INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at) "
                             "VALUES (?,?,?,?,?,?)", (f"a:{item['assignment_id']}:overdue:{due['due_at'] if due else ''}", "needs_you", item["bot_id"], None,
                                                      "assignment_overdue", stamp))
            new += cur.rowcount
    return new


def backlog(store: JobStore) -> int:
    """Events not acknowledged by the Worker yet (a count only)."""
    with closing(store._connect()) as db:
        return db.execute("SELECT COUNT(*) FROM notify_events WHERE seq>?", (_cursor(store),)).fetchone()[0]


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


def build_batch(store: JobStore, stamp=None, limit: int | None = None) -> tuple[dict, int, bool]:
    """(payload, upto, more): the next events after the cursor. Events of a class that is switched off
    are skipped (covered by `upto`, never sent). Payload fields are codes, ids and counts only."""
    stamp = stamp or now()
    prefs = get_prefs(store)
    cursor = _cursor(store)
    with closing(store._connect()) as db:
        rows = db.execute("SELECT seq, key, cls, bot_id, ref, kind FROM notify_events WHERE seq>? AND seq<=? "
                          "ORDER BY seq LIMIT ?", (cursor, limit if limit is not None else 1 << 62, BATCH + 1)).fetchall()
    more = len(rows) > BATCH
    rows = rows[:BATCH]
    events = [{"seq": row["seq"], "id": event_id(row["key"]), "cls": row["cls"], "bot": bot_label(row["bot_id"]),
               "job": row["ref"] if isinstance(row["ref"], str) and _REF.match(row["ref"]) else None,
               "kind": kind_code(row["kind"], row["cls"])}
              for row in rows if row["cls"] in EVENT_CLASSES and prefs[row["cls"]]]
    upto = rows[-1]["seq"] if rows else cursor
    payload = {"sent_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "epoch": _epoch(store), "prefs": prefs,
               "backup_reason": health.backup_reason() if prefs["backup"] else None,
               "upto": upto, "events": events}
    return payload, upto, more


def prune(store: JobStore, keep: timedelta = KEEP_ACKED) -> None:
    """Delete ACKNOWLEDGED events older than *keep*. Unacknowledged ones are never deleted."""
    with closing(store._connect()) as db:
        db.execute("DELETE FROM notify_events WHERE seq<=? AND created_at<?",
                   (_cursor(store), (now() - keep).strftime("%Y-%m-%dT%H:%M:%SZ")))


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
        self._backlogged = False

    @property
    def configured(self) -> bool:
        return self.url.startswith("https://")

    def push_once(self) -> tuple[bool, bool]:
        """One detection pass and one push (blocking: call in a thread). (ok, more queued). A failed or
        unacknowledged push changes nothing: the same events go again next time."""
        more = False
        try:
            secret = read_secret(self.secret_file)
            limit = self._freeze()
            if limit is None:
                record_needs_you(self.store)
            prune(self.store)
            self._watch_backlog()
            body, upto, more = build_batch(self.store, limit=limit)
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

    def _freeze(self) -> int | None:
        """With NOTIFY_OFF present (rollback): the highest sequence logged when it was first seen; later events
        stay in the table, unsent. None when the flag is absent (any earlier freeze is cleared)."""
        if not (self.store.path.parent / OFF_FILE).exists():
            if self.store.get_meta(FREEZE_KEY):
                self.store.delete_meta(FREEZE_KEY)
            return None
        frozen = self.store.get_meta(FREEZE_KEY)
        if not frozen:
            with closing(self.store._connect()) as db:
                frozen = str(db.execute("SELECT COALESCE(MAX(seq),0) FROM notify_events").fetchone()[0])
            self.store.set_meta(FREEZE_KEY, frozen)
        return int(frozen)

    def _watch_backlog(self) -> None:
        over = backlog(self.store) > BACKLOG_LIMIT
        if over != self._backlogged:
            (log.error if over else log.info)(
                "notify backlog %s: %d events unacknowledged, all kept", "over the limit" if over else "back under the limit",
                backlog(self.store))
            self._backlogged = over

    async def run(self) -> None:
        if not self.configured:
            log.info("watchdog events push off (%s is not set to an https URL); events stay queued", URL_ENV)
            return
        while True:
            for _ in range(MAX_PUSHES_PER_CYCLE):
                ok, more = await asyncio.to_thread(self.push_once)
                if not (ok and more):
                    break
            await asyncio.sleep(self.interval)
