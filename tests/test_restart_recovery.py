"""0.9.0 restart recovery: re-queue once with a chat notice; never replay over live leftovers (A3)."""
import asyncio
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from botoffice import procs
from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def cgroup_proof(monkeypatch):
    """Replay needs the service-cgroup proof; the host running the tests may have none."""
    monkeypatch.setattr(procs, "cgroup_clean", lambda *a, **kw: True)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _notices(store, bot_id):
    return [m.text for m in store.list_messages(bot_id) if m.role == "system"]


def _running_job(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    _, job = scheduler.turns.post_message(bot, "long task")
    assert store.claim(Slot.DESKTOP_1, {"codex"}).id == job.id
    return store, bot, job


def _start_and_stop(store, settings, adapter=None):
    async def go():
        scheduler = Scheduler(store, settings, {"codex": adapter or FakeAdapter(delay=30)})
        await scheduler.start()
        await scheduler.stop()

    asyncio.run(go())


def test_graceful_stop_requeues_once_with_notice_and_reruns(settings, cgroup_proof):
    adapter = FakeAdapter(delay=30)
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        job_id = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).json()["job"]["id"]
        assert _wait(lambda: store.get(job_id).status == JobStatus.RUNNING)
    # shutdown is not a user cancel: the row stays running for the next start
    assert store.get(job_id).status == JobStatus.RUNNING
    adapter.delay = 0
    with TestClient(create_app(settings, {"codex": adapter})):
        assert _wait(lambda: store.get(job_id).status == JobStatus.SUCCEEDED)
    job = store.get(job_id)
    assert job.restart_attempts == 1 and job.attempts == 0
    notices = _notices(store, bot["id"])
    assert notices == ["Bot-Office restarted during this turn; retrying from the last confirmed session; "
                       "prior actions may have occurred."]
    assert any(e["kind"] == "retry" and e.get("restart") for e in store.list_events(job_id))


def test_second_restart_fails_with_notice_and_repeated_startup_is_idempotent(settings):
    store, bot, job = _running_job(settings)
    assert store.recover_interrupted() == [{"id": str(job.id), "bot_id": str(bot.id), "outcome": "requeued"}]
    store.claim(Slot.DESKTOP_1, {"codex"})  # re-run starts, then the controller dies again
    store.recover_interrupted()
    store.recover_interrupted()  # a third startup changes nothing
    assert store.get(job.id).status == JobStatus.FAILED
    notices = _notices(store, bot.id)
    assert len(notices) == 2
    assert notices[1] == ("Bot-Office restarted during this turn; not retried again "
                          "(it was already retried once after a restart).")


def _spawn(job_id, script):
    env = {**os.environ, "BOT_OFFICE_JOB_ID": str(job_id)}
    return subprocess.Popen([sys.executable, "-c", script], env=env, start_new_session=True)


def _gone(pid):
    try:
        with open(f"/proc/{pid}/status") as status:
            return "State:\tZ" in status.read()
    except FileNotFoundError:
        return True


def test_sigkilled_controller_leftovers_and_orphan_children_are_stopped_before_replay(settings, cgroup_proof):
    store, bot, job = _running_job(settings)
    # the "provider" ignores SIGTERM and started a child in its own session (an orphan)
    leader = _spawn(job.id, "import signal, subprocess, sys, time\n"
                            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
                            "time.sleep(60)")
    try:
        assert _wait(lambda: len(procs.job_processes(str(job.id))) == 2)
        pids = procs.job_processes(str(job.id))
        _start_and_stop(store, settings)
        assert all(_gone(pid) for pid in pids)
        saved = store.get(job.id)
        assert saved.restart_attempts == 1 and saved.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    finally:
        leader.kill()
        leader.wait()


def test_job_whose_processes_survive_is_not_replayed(settings, monkeypatch):
    store, bot, job = _running_job(settings)
    monkeypatch.setattr(procs, "stop_job_processes", lambda job_id, grace=3.0, **kw: False)
    _start_and_stop(store, settings)
    saved = store.get(job.id)
    assert saved.status == JobStatus.FAILED and "could not be proven stopped" in saved.error
    assert "could not be proven stopped" in _notices(store, bot.id)[0]


def test_retry_prompt_mentions_the_restart(settings):
    from botoffice.workspace import retry_note

    store, bot, job = _running_job(settings)
    store.recover_interrupted()
    assert "restarted" in retry_note(store.get(job.id))


# --- review round 1: fail-closed proof, re-verified signals, execution identity ---

def _fake_proc(tmp_path, cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/bot-office.service"):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text(cgroup + "\n")
    return proc


def _fake_pid(proc, pid, ppid, state="S", environ=b"PATH=/bin\0"):
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "stat").write_text(f"{pid} (x y) {state} {ppid} 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 12345 0 0\n")
    if environ is None:
        (entry / "environ").mkdir()  # unreadable
    else:
        (entry / "environ").write_bytes(environ)


def _fake_cgroup(tmp_path, pids):
    root = tmp_path / "cg"
    unit = root / "user.slice/user-1000.slice/user@1000.service/app.slice/bot-office.service"
    unit.mkdir(parents=True)
    (unit / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids))
    return root


def test_cgroup_with_only_the_controller_and_its_children_proves_recovery(tmp_path):
    me = os.getpid()
    proc = _fake_proc(tmp_path)
    _fake_pid(proc, 900001, me)  # a child this controller started (e.g. voice worker)
    root = _fake_cgroup(tmp_path, [me, 900001])
    assert procs.cgroup_clean(proc, root) is True
    # an unreadable process elsewhere does not matter when the cgroup proves it
    _fake_pid(proc, 900002, 1, environ=None)
    assert procs.recovery_proof(["job-a"], proc, root) == {"job-a"}


def test_leftover_in_the_cgroup_fails_closed_even_without_a_marker(tmp_path):
    me = os.getpid()
    proc = _fake_proc(tmp_path)
    _fake_pid(proc, 900003, 1)  # orphan that wiped its environment
    root = _fake_cgroup(tmp_path, [me, 900003])
    assert procs.cgroup_clean(proc, root) is False
    assert procs.recovery_proof(["job-a"], proc, root) == set()


def test_without_a_service_cgroup_nothing_is_proven(tmp_path):
    # a marker scan alone cannot see a child that dropped its environment: no replay
    proc = _fake_proc(tmp_path, cgroup="0::/user.slice/session-3.scope")
    root = tmp_path / "cg"
    assert procs.cgroup_clean(proc, root) is None
    assert procs.recovery_proof(["job-a"], proc, root) == set()
    _fake_pid(proc, 900004, 1)
    assert procs.recovery_proof(["job-a"], proc, root) == set()


def test_unreadable_service_cgroup_proves_nothing(tmp_path):
    proc = _fake_proc(tmp_path)
    root = _fake_cgroup(tmp_path, [os.getpid()])
    unit = next(root.rglob("bot-office.service"))
    (unit / "cgroup.procs").write_text("not a pid\n")
    assert procs.cgroup_clean(proc, root) is None
    assert procs.recovery_proof(["job-a"], proc, root) == set()
    (unit / "cgroup.procs").unlink()
    assert procs.own_cgroup(proc, root) is None
    assert procs.recovery_proof(["job-a"], proc, root) == set()


def test_marked_survivor_outside_a_clean_cgroup_blocks_its_job(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path)
    root = _fake_cgroup(tmp_path, [os.getpid()])
    _fake_pid(proc, 900007, 1, environ=b"BOT_OFFICE_JOB_ID=job-a\0")
    monkeypatch.setattr(procs, "stop_job_processes", lambda job_id, **kw: False)
    assert procs.recovery_proof(["job-a", "job-b"], proc, root) == {"job-b"}


def test_without_cgroup_proof_leftovers_are_killed_and_the_job_fails_retryable(settings, monkeypatch):
    # e.g. a shell or SSH session scope: the marker scan finds and stops the leftovers,
    # but the job is not replayed; it fails with the notice and the Retry button works
    monkeypatch.setattr(procs, "cgroup_clean", lambda *a, **kw: None)
    store, bot, job = _running_job(settings)
    leader = _spawn(job.id, "import time; time.sleep(60)")
    try:
        assert _wait(lambda: procs.job_processes(str(job.id)) == [leader.pid])
        _start_and_stop(store, settings)
        assert leader.wait(timeout=5) is not None
        saved = store.get(job.id)
        assert saved.status == JobStatus.FAILED and saved.restart_attempts == 0
        assert "could not be proven stopped" in saved.error
        assert _notices(store, bot.id)[0].startswith("Bot-Office restarted during this turn; not retried")
        assert store.retry_now(job.id).status == JobStatus.QUEUED
    finally:
        leader.kill()
        leader.wait()


def test_unreadable_proc_is_no_proof(tmp_path):
    assert procs.scan("job-a", tmp_path / "missing") == procs.Scan((), False)
    assert procs.stop_job_processes("job-a", proc=tmp_path / "missing") is False


def test_signal_is_re_verified_against_start_time_and_marker():
    sleeper = _spawn("job-x", "import time; time.sleep(60)")
    try:
        assert _wait(lambda: procs.job_processes("job-x") == [sleeper.pid])
        start = procs._starttime(sleeper.pid)
        marker = b"BOT_OFFICE_JOB_ID=job-x"
        import signal as signals
        # a recycled pid (different start time) is never signalled
        assert procs._signal_verified(sleeper.pid, start + 1, marker, signals.SIGKILL)
        assert sleeper.poll() is None
        assert procs._signal_verified(sleeper.pid, start, b"BOT_OFFICE_JOB_ID=other", signals.SIGKILL)
        assert sleeper.poll() is None
        assert procs._signal_verified(sleeper.pid, start, marker, signals.SIGKILL)
        assert sleeper.wait(timeout=5) == -signals.SIGKILL
    finally:
        sleeper.kill()
        sleeper.wait()


def test_no_proof_at_startup_fails_the_job_with_a_retryable_notice(settings, monkeypatch):
    store, bot, job = _running_job(settings)
    monkeypatch.setattr(procs, "cgroup_clean", lambda *a, **kw: False)
    _start_and_stop(store, settings)
    saved = store.get(job.id)
    assert saved.status == JobStatus.FAILED and "could not be proven stopped" in saved.error
    assert _notices(store, bot.id)[0].startswith("Bot-Office restarted during this turn; not retried")
    assert store.retry_now(job.id).status == JobStatus.QUEUED  # the Retry button works


def test_restart_replay_is_a_new_execution_with_history(settings):
    store, bot, job = _running_job(settings)
    store.recover_interrupted()
    saved = store.get(job.id)
    assert saved.attempts == 0 and saved.restart_attempts == 1 and saved.execution == 1
    history = store.list_attempts(job.id)
    assert [(a["attempt"], a["kind"], a["automatic"]) for a in history] == [(0, "interrupted", 0)]
    retry = [e for e in store.list_events(job.id) if e["kind"] == "retry"]
    assert retry == [dict(retry[0], attempt=1, restart=True)]
    # the replay streams as execution 1, so its events never mix with execution 0's
    adapter = FakeAdapter()
    scheduler = Scheduler(store, settings, {"codex": adapter})
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, claimed))
    events = store.list_events(job.id)
    assert {e["attempt"] for e in events if e["kind"] in ("start", "done")} == {1}
