"""0.10.0 run-until-done loop: status-driven continuation, budgets, verify, stop, idempotency (B-2)."""
import asyncio
import threading

from botoffice import failures
from botoffice.assignments import Assignments
from botoffice.domain import JobStatus
from botoffice.providers.base import RunResult
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore


class Scripted:
    """Replies from a list, one per run; a RunResult in the list is returned as is."""

    name = "codex"
    enabled = True

    def __init__(self, *replies):
        self.replies = list(replies)
        self.timeouts = []
        self.prompts = []

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        self.timeouts.append(timeout)
        self.prompts.append(job.prompt)
        log_path.write_text("log")
        reply = self.replies.pop(0) if self.replies else "BOT-OFFICE-STATUS: continue"
        if isinstance(reply, RunResult):
            (workspace / "WORKING.md").write_text("half way")
            return reply
        return RunResult("succeeded", exit_code=0, final_text=reply)


def setup(settings, *replies, **fields):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = Scripted(*replies)
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    item = scheduler.assignments.create(bot.id, {"title": "Report", "goal": "write it", **fields, "start": True})
    return store, scheduler, adapter, bot, item


def run_next(store, scheduler):
    job = store.claim(["desktop-1"], {"codex"})
    if job is None:
        return None
    asyncio.run(scheduler._execute(0, job))
    return store.get(job.id)


def run_all(store, scheduler, limit=50):
    jobs = []
    while (job := run_next(store, scheduler)) is not None and len(jobs) < limit:
        jobs.append(job)
    return jobs


def queued(store, item):
    return [j for j in store.list() if j.assignment_id == item.id and j.status == JobStatus.QUEUED]


def test_continue_requeues_until_the_turn_budget_then_blocks(settings):
    store, scheduler, _, bot, item = setup(settings, budget_turns=3)
    jobs = run_all(store, scheduler)
    assert len(jobs) == 3 and [j.assignment_turn for j in jobs] == [1, 2, 3]
    after = scheduler.assignments.get(item.id)
    assert after.status == "blocked" and after.turns_used == 3 and "budget used" in after.status_note
    assert not queued(store, item)
    assert any("budget used" in m.text for m in store.list_messages(bot.id) if m.role == "system")


def test_minutes_budget_also_stops_the_loop(settings):
    store, scheduler, _, _, item = setup(settings)
    queued_id = queued(store, item)[0].id
    with store._connect() as db:
        db.execute("UPDATE assignments SET minutes_used=budget_minutes WHERE id=?", (str(item.id),))
    assert run_all(store, scheduler) == []  # refused at claim: the turn never runs
    assert store.get(queued_id).status == JobStatus.CANCELLED and "budget" in store.get(queued_id).error
    scheduler.assignments.settle_pending()  # the watchdog settles the refused turn
    after = scheduler.assignments.get(item.id)
    assert after.status == "blocked" and "budget used" in after.status_note


def test_done_closes_and_verify_runs_exactly_one_extra_turn(settings):
    store, scheduler, _, _, item = setup(settings, "Finished.\nBOT-OFFICE-STATUS: done")
    assert len(run_all(store, scheduler)) == 1
    assert scheduler.assignments.get(item.id).status == "done"

    store, scheduler, adapter, _, item = setup(
        settings.__class__(data_dir=settings.data_dir / "v", token="t", job_timeout=2),
        "Finished.\nBOT-OFFICE-STATUS: done", "Checked.\nBOT-OFFICE-STATUS: done", verify=True)
    jobs = run_all(store, scheduler)
    assert len(jobs) == 2 and "done when" in adapter.prompts[1]
    assert scheduler.assignments.get(item.id).status == "done"


def test_missing_status_continues_once_then_blocks(settings):
    store, scheduler, _, _, item = setup(settings, "did a bit", "did more")
    assert len(run_all(store, scheduler)) == 2
    after = scheduler.assignments.get(item.id)
    assert after.status == "blocked" and "two turns" in after.status_note


def test_blocked_and_waiting_stop_the_loop(settings):
    for state in ("blocked", "waiting_on_user", "waiting_on_bot"):
        data = settings.__class__(data_dir=settings.data_dir / state, token="t", job_timeout=2)
        store, scheduler, _, _, item = setup(data, f"Stuck.\nBOT-OFFICE-STATUS: {state} — need a key")
        assert len(run_all(store, scheduler)) == 1
        after = scheduler.assignments.get(item.id)
        assert (after.status, after.status_note) == (state, "need a key")


def test_reported_status_counts_when_the_reply_has_no_line(settings):
    store, scheduler, _, bot, item = setup(settings)
    job = store.claim(["desktop-1"], {"codex"})
    scheduler.assignments.report(item.id, bot.id, job.id, "waiting_on_user", "which branch?")
    scheduler.providers["codex"].replies = ["Asked."]
    asyncio.run(scheduler._execute(0, job))
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"


def test_a_failed_turn_blocks_with_its_reason(settings):
    store, scheduler, _, _, item = setup(settings, RunResult("failed", exit_code=1, error="boom",
                                                             failure_kind=failures.TOOL_DENIED))
    assert len(run_all(store, scheduler)) == 1
    after = scheduler.assignments.get(item.id)
    assert after.status == "blocked" and "tool_denied" in after.status_note


def test_timeout_keeps_working_state_and_retries_counting_toward_the_budget(settings):
    timeout = RunResult("failed", exit_code=124, error="timed out", failure_kind=failures.TIMEOUT)
    store, scheduler, _, bot, item = setup(settings, timeout, "Resumed.\nBOT-OFFICE-STATUS: continue")
    first = run_next(store, scheduler)
    assert first.status == JobStatus.QUEUED and first.attempts == 1  # same row, Stage A retry
    assert scheduler.memory.read_working(bot.id) == "half way"
    assert scheduler.assignments.get(item.id).turns_used == 1  # each run is charged when it is claimed
    second = run_next(store, scheduler)
    assert second.id == first.id and second.status == JobStatus.INCOMPLETE
    after = scheduler.assignments.get(item.id)
    assert after.turns_used == 2 and after.status == "active" and len(queued(store, item)) == 1


def test_per_bot_turn_timeout(settings):
    store, scheduler, adapter, bot, _ = setup(settings, "x\nBOT-OFFICE-STATUS: done")
    store.update_bot(bot.id, job_timeout=120)
    run_next(store, scheduler)
    assert adapter.timeouts == [120]
    store.update_bot(bot.id, job_timeout=10**6)
    assert scheduler.turn_timeout(store.get_bot(bot.id)) == settings.max_turn_timeout


def test_user_chat_is_claimed_before_the_next_continuation(settings):
    store, scheduler, _, bot, item = setup(settings, "step\nBOT-OFFICE-STATUS: continue")
    run_next(store, scheduler)
    assert len(queued(store, item)) == 1
    _, chat = scheduler.turns.post_message(bot, "quick question")
    assert store.claim(["desktop-1"], {"codex"}).id == chat.id


def test_stop_cancels_running_and_queued_turns_and_children(settings):
    store, scheduler, _, bot, item = setup(settings)
    other = store.create_bot("Bea", "", "codex", None)
    child = scheduler.assignments.create(other.id, {"title": "child", "start": True}, created_by=f"bot:{bot.id}",
                                         parent_id=item.id)
    running = store.claim(["desktop-1"], {"codex"})
    assert running.assignment_id == item.id
    assert scheduler.assignments.stop(item.id) == [str(running.id)]
    assert scheduler.assignments.get(item.id).status == "open"
    assert scheduler.assignments.get(child.id).status == "open"
    assert not queued(store, child)
    # the running turn is cancelled by the caller; settling it charges but decides nothing
    store.finish(running.id, JobStatus.CANCELLED, error="cancelled")
    assert scheduler.assignments.settle(running.id) == "charged"
    assert scheduler.assignments.get(item.id).status == "open" and not queued(store, item)


def test_crash_between_finish_and_settle_is_settled_once(settings, monkeypatch):
    store, scheduler, _, _, item = setup(settings)
    job = store.claim(["desktop-1"], {"codex"})
    # the job finished, then the controller died before the loop step
    store.finish(job.id, JobStatus.INCOMPLETE, outcome="continue")
    restarted = Scheduler(store, settings, scheduler.providers)

    async def idle():
        return None

    monkeypatch.setattr(restarted, "_run_loop", idle)  # keep the new turn queued for the check
    asyncio.run(restarted.start())
    asyncio.run(restarted.stop())
    assert len(queued(store, item)) == 1 and scheduler.assignments.get(item.id).turns_used == 1
    # replays change nothing
    assert scheduler.assignments.settle(job.id) is None
    assert scheduler.assignments.settle_pending() == 0
    assert len(queued(store, item)) == 1 and scheduler.assignments.get(item.id).turns_used == 1


def test_concurrent_stop_and_settle_never_leave_a_runnable_turn(settings):
    store, scheduler, _, _, item = setup(settings)
    book = scheduler.assignments
    for _ in range(10):
        job = store.claim(["desktop-1"], {"codex"})
        store.finish(job.id, JobStatus.INCOMPLETE, outcome="continue")
        barrier = threading.Barrier(2)

        def settle(job_id=job.id, barrier=barrier):
            barrier.wait()
            Assignments(store).settle(job_id)

        def stop(barrier=barrier):
            barrier.wait()
            Assignments(store).stop(item.id)

        threads = [threading.Thread(target=settle), threading.Thread(target=stop)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert book.get(item.id).status == "open" and not queued(store, item)
        book.start(item.id)
    assert book.get(item.id).turns_used == 10


def test_bot_cancel_of_a_running_turn_pauses_the_assignment(settings):
    store, scheduler, _, _, item = setup(settings)
    job = store.claim(["desktop-1"], {"codex"})
    store.finish(job.id, JobStatus.CANCELLED, error="cancelled")
    assert scheduler.assignments.settle(job.id) == "open"
    assert not queued(store, item)
