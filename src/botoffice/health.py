"""GET /healthz payload: cheap, non-sensitive status for the external watchdog (ops/watchdog).

Only booleans, numbers, short enums and the version string leave here: no tokens, no paths,
no message content, and no backup error text (status.json's "error" is read for nothing).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, failures

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
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="healthz")
_inflight: asyncio.Future | None = None


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
    or finished in the future); stale: not ok, or the last finish is older than BACKUP_STALE_HOURS or unknown."""
    path = path or Path(os.environ.get(BACKUP_STATUS_ENV) or DEFAULT_BACKUP_STATUS).expanduser()
    now = now or datetime.now(UTC)
    state, finished, size = "unknown", None, None
    data = read_status_file(path)
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
    """A short, redacted one-line reason from the status file's "error" (for the alert mail only;
    never served by /healthz)."""
    path = path or Path(os.environ.get(BACKUP_STATUS_ENV) or DEFAULT_BACKUP_STATUS).expanduser()
    data = read_status_file(path)
    raw = data.get("error") if data else None
    return failures.clip(raw, 120) if isinstance(raw, str) and raw.strip() else None


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
    db_ok, running = True, None
    try:
        running = len(store.running_job_ids())
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
        "backup": backup_status(backup_path),
        "disk_free_pct": free,
    }


async def snapshot(app_state: Any, backup_path: Path | None = None,
                   timeout: float | None = None) -> tuple[dict[str, Any], int]:
    """build() off the event loop, bounded, and shared: concurrent callers wait on one run in a
    dedicated single thread, so a slow disk or a locked database can neither stall the controller nor
    pile up threads. Returns (body, http status); a timeout is ok=false, 503."""
    global _inflight
    loop = asyncio.get_running_loop()
    if _inflight is None or _inflight.done():
        _inflight = loop.run_in_executor(_executor, build, app_state, backup_path)
    try:
        return await asyncio.wait_for(asyncio.shield(_inflight), HEALTH_TIMEOUT_SECONDS if timeout is None else timeout), 200
    except TimeoutError:
        return {"ok": False, "version": __version__, "timed_out": True}, 503
