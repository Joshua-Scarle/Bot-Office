"""0.12.9: first_started_at survives retries and restarts; a cancel records who stopped the job."""
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from botoffice.domain import JobStatus, Slot
from botoffice.store import JobStore


def _retry(store, job):
    assert store.requeue(job.id, datetime.now(UTC), "provider_error", error="boom", reply_text=None, max_per_hour=20)


def test_first_started_at_is_kept_over_a_retry(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    job = store.create("hello")
    first = store.claim(Slot.DESKTOP_1, {"codex"})
    assert first.first_started_at == first.started_at
    _retry(store, job)
    again = store.claim(Slot.DESKTOP_1, {"codex"})
    assert again.started_at > first.started_at
    assert again.first_started_at == first.first_started_at


def test_migration_backfills_first_started_at_from_attempts(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = JobStore(path)
    job = store.create("hello")
    store.claim(Slot.DESKTOP_1, {"codex"})
    _retry(store, job)
    store.claim(Slot.DESKTOP_1, {"codex"})
    with closing(sqlite3.connect(path)) as db:
        earliest = db.execute("SELECT MIN(started_at) FROM job_attempts WHERE job_id=?", (str(job.id),)).fetchone()[0]
        db.execute("ALTER TABLE jobs DROP COLUMN first_started_at")
        db.commit()
    assert JobStore(path).get(job.id).first_started_at.isoformat() == earliest


def test_cancel_records_who_stopped_a_queued_job(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    job = store.create("hello")
    assert store.cancel(job.id).error == "cancelled"
    other = store.create("again")
    done = store.cancel(other.id, reason="cancelled by you")
    assert done.status == JobStatus.CANCELLED and done.error == "cancelled by you"


def test_cancelling_a_running_job_stores_the_given_reason(settings):
    import asyncio

    from botoffice.providers.base import RunResult
    from botoffice.scheduler import Scheduler

    class Hangs:
        name = "codex"
        enabled = True

        async def login_status(self):
            return "ok"

        async def run(self, job, workspace, log_path, timeout):
            log_path.write_text("log")
            await asyncio.sleep(30)
            return RunResult("succeeded", exit_code=0)

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": Hangs()})
    job = store.create("hello")
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})

    async def go():
        task = asyncio.ensure_future(scheduler._execute(Slot.DESKTOP_1, claimed))
        scheduler._tasks[0], scheduler._job_ids[0] = task, str(job.id)
        await asyncio.sleep(0.2)
        scheduler.cancel(store.get(job.id), "cancelled by you")
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    done = store.get(job.id)
    assert done.status == JobStatus.CANCELLED and done.error == "cancelled by you"
    assert store.list_events(job.id)[-1]["error"] == "cancelled by you"


def test_backfill_converges_when_the_column_exists_but_is_null(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = JobStore(path)
    job = store.create("hello")
    first = store.claim(Slot.DESKTOP_1, {"codex"})
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE jobs SET first_started_at=NULL")  # as left by older code after a rollback
        db.commit()
    assert JobStore(path).get(job.id).first_started_at == first.started_at
