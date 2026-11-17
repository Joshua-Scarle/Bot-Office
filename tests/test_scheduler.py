import asyncio
import os
import time

from botoffice.domain import JobStatus, Slot
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.providers.fake import FakeAdapter


def test_inbox_scan_removes_only_stale_desk_temp_files(settings, monkeypatch):
    from botoffice import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module.time, "time", lambda: 10_000)
    directory = settings.data_dir / "inboxes" / "bot"
    directory.mkdir(parents=True)
    stale = directory / f".{'a' * 32}.json.tmp"
    fresh = directory / f".{'b' * 32}.json.tmp"
    boundary = directory / f".{'c' * 32}.json.tmp"
    unrelated = directory / ".other.json.tmp"
    for path in (stale, fresh, boundary, unrelated):
        path.write_text("data")
    for path, modified in ((stale, 9399), (fresh, 9999), (boundary, 9400), (unrelated, 1)):
        os.utime(path, (modified, modified))

    scheduler = Scheduler(JobStore(settings.data_dir / "jobs.sqlite3"), settings, {})
    scheduler._read_inboxes()

    assert not stale.exists()
    assert all(path.exists() for path in (fresh, boundary, unrelated))


class TrackingFake(FakeAdapter):
    def __init__(self):
        super().__init__(delay=0.15)
        self.active = 0
        self.maximum = 0
        self.seen_slots = set()

    async def run(self, job, workspace, log_path, timeout):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.seen_slots.add(job.slot)
        try:
            return await super().run(job, workspace, log_path, timeout)
        finally:
            self.active -= 1


def test_three_lanes_run_parallel_and_each_desktop_has_one_job(settings):
    # 0.9.0: concurrency is the lane count (3); the two desktops are leased while free
    async def scenario():
        store = JobStore(settings.data_dir / "jobs.sqlite3")
        adapter = TrackingFake()
        scheduler = Scheduler(store, settings, {"codex": adapter})
        jobs = [store.create(f"job {number}") for number in range(4)]
        await scheduler.start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            statuses = [store.get(job.id).status for job in jobs]
            running = [job for job in store.list() if job.status == JobStatus.RUNNING]
            leased = [job.slot for job in running if job.slot]
            assert len(set(leased)) == len(leased) and len(running) <= 3
            if all(status == JobStatus.SUCCEEDED for status in statuses):
                break
            await asyncio.sleep(0.02)
        await scheduler.stop()
        assert all(store.get(job.id).status == JobStatus.SUCCEEDED for job in jobs)
        assert adapter.maximum == 3
        assert adapter.seen_slots == {Slot.DESKTOP_1, Slot.DESKTOP_2, None}

    asyncio.run(scenario())
