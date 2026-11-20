"""GET /healthz payload: cheap, non-sensitive status for the external watchdog (ops/watchdog).

Only booleans, numbers, short enums and the version string leave here: no tokens, no paths,
no message content, and no backup error text (status.json's "error" is read for nothing).
"""
from __future__ import annotations

import json
import os
import shutil
import time
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


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def backup_status(path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """state: ok | failed | not_configured | unknown (file missing or unreadable);
    stale: not ok, or the last finish is older than BACKUP_STALE_HOURS (or has no valid time)."""
    path = path or Path(os.environ.get(BACKUP_STATUS_ENV) or DEFAULT_BACKUP_STATUS).expanduser()
    now = now or datetime.now(UTC)
    state, finished, size = "unknown", None, None
    try:
        with path.open("rb") as handle:
            data = json.loads(handle.read(_STATUS_MAX_BYTES))
        if isinstance(data, dict):
            raw_state = data.get("state")
            state = raw_state if raw_state in _BACKUP_STATES else "unknown"
            finished = _parse_time(data.get("finished"))
            raw_bytes = data.get("bytes")
            size = raw_bytes if isinstance(raw_bytes, int) and not isinstance(raw_bytes, bool) else None
    except (OSError, ValueError):
        pass
    age = round((now - finished).total_seconds() / 3600, 2) if finished else None
    if age is not None and age < 0:
        age = 0.0
    stale = state != "ok" or age is None or age > BACKUP_STALE_HOURS
    return {"state": state, "age_hours": age, "bytes": size, "stale": stale}


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
