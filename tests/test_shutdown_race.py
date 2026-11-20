"""0.12.6: a CLI that dies with the service cgroup on SIGTERM is an interrupted turn, not a failed one.

Live drill (Codex, 2026-11-20): systemd signals the controller and the CLI together; Codex exited
first, the runner recorded a provider error and parked the job for a delayed retry with the wrong
notice. Claude lost the race the other way and was re-queued at once with the restart notice."""
import asyncio
import signal
import time

from botoffice import failures
from botoffice.domain import JobStatus
from botoffice.providers.base import RunResult
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler, on_terminate
from botoffice.store import JobStore


def _wait_sync(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class KilledAdapter(FakeAdapter):
    """Runs until released, then reports what a CLI killed by the service's SIGTERM reports."""

    def __init__(self, result="killed"):
        super().__init__()
        self.release = None
        self.mode = result

    async def run(self, job, workspace, log_path, timeout):
        self.started += 1
        await self.release.wait()
        log_path.write_text("cli terminated\n", encoding="utf-8")
        if self.mode == "ok":
            return RunResult("succeeded", exit_code=0)
        return RunResult("failed", exit_code=-15, error="terminated by signal 15",
                         failure_kind=failures.PROVIDER_ERROR)


def _drill(settings, mode, signalled, late=False):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    adapter = KilledAdapter(mode)

    async def go():
        adapter.release = asyncio.Event()
        scheduler = Scheduler(store, settings, {"codex": adapter})
        await scheduler.start()
        _, job = scheduler.turns.post_message(bot, "long task")
        while store.get(job.id).status != JobStatus.RUNNING:
            await asyncio.sleep(0.02)
        if signalled and not late:
            scheduler.begin_shutdown()  # SIGTERM reached the controller
        adapter.release.set()  # the CLI dies / finishes right after
        if signalled and late:
            await asyncio.sleep(0.1)  # the CLI result is in before the signal handler ran
            scheduler.begin_shutdown()
        await asyncio.sleep(1.2)  # past the signal grace window
        await scheduler.stop()
        return job

    job = asyncio.run(go())
    return store, bot, job


def test_cli_killed_with_the_service_leaves_the_job_running_for_recovery(settings):
    store, bot, job = _drill(settings, "killed", signalled=True)
    row = store.get(job.id)
    assert row.status == JobStatus.RUNNING and row.attempts == 0 and row.restart_attempts == 0
    assert not [m for m in store.list_messages(bot.id) if m.role == "system"]
    assert not any(e["kind"] == "retry" for e in store.list_events(job.id))


def test_cli_killed_with_the_service_is_requeued_with_the_restart_notice(settings, monkeypatch):
    from botoffice import procs
    monkeypatch.setattr(procs, "cgroup_clean", lambda *a, **kw: True)
    store, bot, job = _drill(settings, "killed", signalled=True)

    async def again():
        scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
        await scheduler.start()
        await asyncio.sleep(0.5)
        await scheduler.stop()

    asyncio.run(again())
    row = store.get(job.id)
    assert row.status == JobStatus.SUCCEEDED and row.restart_attempts == 1 and row.attempts == 0
    notices = [m.text for m in store.list_messages(bot.id) if m.role == "system"]
    assert notices == ["Bot-Office restarted during this turn; retrying from the last confirmed session; "
                       "prior actions may have occurred."]


def test_a_signal_that_lands_just_after_the_cli_died_still_counts_as_shutdown(settings):
    store, bot, job = _drill(settings, "killed", signalled=True, late=True)
    row = store.get(job.id)
    assert row.status == JobStatus.RUNNING and row.attempts == 0
    assert not any(e["kind"] == "retry" for e in store.list_events(job.id))


def test_on_terminate_restores_the_default_action_when_nothing_was_installed(monkeypatch):
    import os
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, num: killed.append(num))
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        on_terminate(lambda: None, (signal.SIGTERM,))
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert killed == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_a_cli_failure_without_a_shutdown_is_still_a_provider_error(settings):
    store, bot, job = _drill(settings, "killed", signalled=False)
    row = store.get(job.id)
    assert row.status != JobStatus.RUNNING and row.attempts == 1


def test_a_turn_that_finished_during_the_shutdown_keeps_its_result(settings):
    store, bot, job = _drill(settings, "ok", signalled=True)
    assert store.get(job.id).status == JobStatus.SUCCEEDED


def test_on_terminate_chains_the_previous_handler_and_runs_the_callback():
    calls = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: calls.append("previous"))
    try:
        on_terminate(lambda: calls.append("callback"), (signal.SIGTERM,))
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert calls == ["callback", "previous"]
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_on_terminate_is_a_no_op_off_the_main_thread():
    import threading
    out = []
    t = threading.Thread(target=lambda: out.append(on_terminate(lambda: None)))
    t.start(); t.join()
    assert out == [False]
