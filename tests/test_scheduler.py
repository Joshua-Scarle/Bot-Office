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


def test_the_pause_file_stops_claims_and_firing_and_lifts_without_a_restart(settings):
    """Maintenance pause (deploy runbook): with <data_dir>/PAUSE nothing new starts and no tick fires,
    a running turn finishes, queued work waits and runs once the file is removed."""
    pause = settings.data_dir / "PAUSE"

    async def scenario():
        store = JobStore(settings.data_dir / "jobs.sqlite3")
        scheduler = Scheduler(store, settings, {"codex": FakeAdapter(delay=0.05)})
        running = store.create("already running")
        await scheduler.start()
        deadline = time.monotonic() + 4
        while store.get(running.id).status != JobStatus.SUCCEEDED and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        pause.write_text("")
        assert scheduler.paused() is True
        waiting = store.create("queued while paused")
        fired = []
        scheduler._fire_routines = lambda: fired.append(1)
        await asyncio.sleep(0.6)  # many loop passes and at least one tick
        assert store.get(waiting.id).status == JobStatus.QUEUED and fired == []
        scheduler.tick()
        assert fired == []
        pause.unlink()
        assert scheduler.paused() is False
        deadline = time.monotonic() + 4
        while store.get(waiting.id).status != JobStatus.SUCCEEDED and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        while not fired and time.monotonic() < deadline + 2:
            await asyncio.sleep(0.05)
        await scheduler.stop()
        assert store.get(waiting.id).status == JobStatus.SUCCEEDED and fired

    asyncio.run(scenario())


def test_a_pause_is_acknowledged_only_after_the_loop_has_seen_it(settings):
    """/healthz may call the scheduler paused (and its running count final) only after the run loop itself
    observed PAUSE on a pass: the file merely existing is not enough, a claim may be under way."""
    pause = settings.data_dir / "PAUSE"

    async def scenario():
        store = JobStore(settings.data_dir / "jobs.sqlite3")
        scheduler = Scheduler(store, settings, {"codex": FakeAdapter(delay=0.05)})
        await scheduler.start()
        await asyncio.sleep(0.2)
        assert scheduler.pause_acknowledged() is False
        pause.write_text("")
        assert scheduler.paused() is True and scheduler.pause_acknowledged() is False  # no pass has run since
        await asyncio.sleep(0.3)
        assert scheduler.pause_acknowledged() is True
        job = store.create("queued during the pause")
        await asyncio.sleep(0.3)
        assert store.get(job.id).status == JobStatus.QUEUED and scheduler.pause_acknowledged() is True
        pause.unlink()
        assert scheduler.pause_acknowledged() is True  # still the last observation until the next pass...
        await asyncio.sleep(0.3)
        assert scheduler.pause_acknowledged() is False  # ...which withdraws it
        await scheduler.stop()

    asyncio.run(scenario())


def test_a_loop_that_stopped_passing_cannot_vouch_for_the_pause(settings):
    async def scenario():
        store = JobStore(settings.data_dir / "jobs.sqlite3")
        scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
        scheduler._pause_seen, scheduler._pause_seen_at = True, time.monotonic() - 60
        assert scheduler.pause_acknowledged() is False

    asyncio.run(scenario())
