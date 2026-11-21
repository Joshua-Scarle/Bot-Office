"""GET /healthz payload: cheap, non-sensitive status for the external watchdog (ops/watchdog).

Only booleans, numbers, short enums and the version string leave here: no tokens, no paths,
no message content, and no backup error text (status.json's "error" is never read).

Nothing here can block the controller or its shutdown: the status file is read by a daemon thread the
caller waits on for a bound (BackupReader), and build() itself runs on a daemon thread (run_daemon).
A backup that could not be read in time is "unjudged": the watchdog neither alerts nor recovers on it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__

BACKUP_STATUS_ENV = "BOT_OFFICE_BACKUP_STATUS"
DEFAULT_BACKUP_STATUS = "~/contabo-backup/status.json"
BACKUP_STALE_HOURS = 30.0  # the daily backup is overdue past this
SCHEDULER_STALE_SECONDS = 30.0  # the loop ticks every second; this long silent = wedged
DISK_LOW_PCT = 5.0
_BACKUP_STATES = {"ok", "failed", "not_configured"}
_STATUS_MAX_BYTES = 64 * 1024

_started = time.monotonic()
FUTURE_TOLERANCE_HOURS = 0.1  # a finish time further ahead than this is a clock error, not a fresh backup
HEALTH_TIMEOUT_SECONDS = 3.0  # an answer slower than this is itself the finding
BACKUP_READ_WAIT_SECONDS = 1.0  # how long a caller waits for the status file before calling it unjudged
BACKUP_CACHE_SECONDS = 600.0  # a last good read is reused this long while the file system is stuck
BACKUP_STAGES = ("dump", "archive", "encrypt", "upload", "verify", "prune", "restore_test")
_inflight: asyncio.Future | None = None


def run_daemon(fn, *args) -> asyncio.Future:
    """fn(*args) on a daemon thread, as an awaitable: a call that never returns cannot hold up
    interpreter shutdown (a ThreadPoolExecutor thread would be joined at exit)."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(setter, value) -> None:
        if not future.done():
            setter(value)

    def work() -> None:
        try:
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to the awaiting caller
                loop.call_soon_threadsafe(deliver, future.set_exception, exc)
            else:
                loop.call_soon_threadsafe(deliver, future.set_result, result)
        except RuntimeError:
            pass  # the loop is gone (shutdown) before a late result: nobody is waiting

    threading.Thread(target=work, name="healthz", daemon=True).start()
    return future


class BackupReader:
    """Reads the backup status file on a daemon thread, one read at a time. read() waits at most `wait`
    seconds: a stuck mount returns the last good read (up to BACKUP_CACHE_SECONDS old) or (None, False)
    = unjudged. The caller never blocks longer than the bound, and shutdown never waits for the thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._path: Path | None = None
        self._data: dict | None = None
        self._at: float | None = None

    def _work(self, path: Path) -> None:
        data = read_status_file(path)
        with self._lock:
            self._path, self._data, self._at = path, data, time.monotonic()

    def read(self, path: Path, wait: float | None = None) -> tuple[dict | None, bool]:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                thread = self._thread = threading.Thread(target=self._work, args=(path,), name="backup-status",
                                                         daemon=True)
                thread.start()
        thread.join(BACKUP_READ_WAIT_SECONDS if wait is None else wait)
        with self._lock:
            fresh = not thread.is_alive()
            if self._path == path and self._at is not None and (
                    fresh or time.monotonic() - self._at < BACKUP_CACHE_SECONDS):
                return self._data, True
        return None, False


backup_reader = BackupReader()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_status_file(path: Path) -> dict | None:
    """The status file's JSON object, or None. Opened without blocking (a FIFO or a dead mount must not
    hang us), checked on the same descriptor to be a regular file, and read with a size cap."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = json.loads(handle.read(_STATUS_MAX_BYTES))
    except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def backup_status(path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """state: ok | failed | not_configured | unknown (file missing, unreadable, not a regular file, malformed,
    or finished in the future) | unjudged (the file system did not answer in time); stale: not ok, or the last finish is older than BACKUP_STALE_HOURS or unknown."""
    path = path or Path(os.environ.get(BACKUP_STATUS_ENV) or DEFAULT_BACKUP_STATUS).expanduser()
    now = now or datetime.now(UTC)
    state, finished, size = "unknown", None, None
    data, judged = backup_reader.read(path)
    if not judged:
        return {"state": "unjudged", "age_hours": None, "bytes": None, "stale": False}
    if data is not None:
        raw_state = data.get("state")
        state = raw_state if isinstance(raw_state, str) and raw_state in _BACKUP_STATES else "unknown"
        finished = _parse_time(data.get("finished"))
        raw_bytes = data.get("bytes")
        size = raw_bytes if isinstance(raw_bytes, int) and not isinstance(raw_bytes, bool) else None
    age = round((now - finished).total_seconds() / 3600, 2) if finished else None
    if age is not None and age < -FUTURE_TOLERANCE_HOURS:
        state, age = "unknown", None  # a date in the future cannot vouch for a backup
    elif age is not None and age < 0:
        age = 0.0
    stale = state != "ok" or age is None or age > BACKUP_STALE_HOURS
    return {"state": state, "age_hours": age, "bytes": size, "stale": stale}


def backup_reason(path: Path | None = None) -> str | None:
    """A fixed reason code for the alert mail: "stage_<stage>" for a stage in BACKUP_STAGES, "failed" when the
    status says failed, else None. The status file's free-text "error" is never read or sent."""
    path = path or Path(os.environ.get(BACKUP_STATUS_ENV) or DEFAULT_BACKUP_STATUS).expanduser()
    data, judged = backup_reader.read(path)
    if not judged or not data:
        return None
    stage = data.get("stage")
    if isinstance(stage, str) and stage in BACKUP_STAGES:
        return f"stage_{stage}"
    return "failed" if data.get("state") == "failed" else None


def disk_free_pct(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.anchor or "/")
    except OSError:
        return None
    return round(usage.free * 100 / usage.total, 1) if usage.total else None


def build(app_state: Any, backup_path: Path | None = None) -> dict[str, Any]:
    settings, store, scheduler = app_state.settings, app_state.store, app_state.scheduler
    live = scheduler.liveness()
    scheduler_ok = bool(live["alive"]) and live["last_tick_age_seconds"] is not None \
        and live["last_tick_age_seconds"] <= SCHEDULER_STALE_SECONDS
    db_ok, running, queued = True, None, None
    try:
        running = len(store.running_job_ids())
        from . import notify  # late: notify imports this module

        queued = notify.backlog(store)
    except Exception:  # noqa: BLE001 - any DB failure is the answer here, never a 500
        db_ok = False
    free = disk_free_pct(settings.data_dir)
    disk_ok = free is None or free >= DISK_LOW_PCT
    return {
        "ok": scheduler_ok and db_ok and disk_ok,
        "version": __version__,
        "uptime_seconds": int(time.monotonic() - _started),
        "scheduler": {"alive": scheduler_ok, "last_tick_age_seconds": live["last_tick_age_seconds"]},
        "db_reachable": db_ok,
        "running_jobs": running,
        "paused": scheduler.paused(),
        "notify_backlog": queued,
        "backup": backup_status(backup_path),
        "disk_free_pct": free,
    }


async def snapshot(app_state: Any, backup_path: Path | None = None,
                   timeout: float | None = None) -> tuple[dict[str, Any], int]:
    """build() off the event loop, bounded, and shared: concurrent callers wait on one run on one
    daemon thread, so a slow disk or a locked database can neither stall the controller nor
    pile up threads, and a stuck one cannot block shutdown. Returns (body, http status); a timeout is ok=false, 503."""
    global _inflight
    loop = asyncio.get_running_loop()
    if _inflight is None or _inflight.done() or _inflight.get_loop() is not loop:  # (another loop: a dead run)
        _inflight = run_daemon(build, app_state, backup_path)
    try:
        return await asyncio.wait_for(asyncio.shield(_inflight), HEALTH_TIMEOUT_SECONDS if timeout is None else timeout), 200
    except TimeoutError:
        return {"ok": False, "version": __version__, "timed_out": True}, 503
