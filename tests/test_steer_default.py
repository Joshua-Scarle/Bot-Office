"""0.10.4: timeout retries name the blocked step; owner chat posts steer by default (also into queued turns)."""

from test_retries import OK, Scripted, make, make_due, run_next

from botoffice import failures
from botoffice.domain import JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.streaming import emit_event

TIMEOUT = RunResult("failed", exit_code=124, error="job timed out", failure_kind=failures.TIMEOUT)


class Prompts(Scripted):
    def __init__(self, *results, tool=None):
        super().__init__(*results)
        self.prompts, self.resumes, self.tool = [], [], tool

    async def run(self, job, workspace, log_path, timeout):
        self.prompts.append(job.prompt)
        self.resumes.append(job.resume_prompt)
        if self.tool and self.runs == 0:
            emit_event("tool", name="command", summary=self.tool)
        return await super().run(job, workspace, log_path, timeout)


def test_timeout_retry_prompt_names_blocked_command(settings):
    adapter = Prompts(TIMEOUT, OK, tool="agy --prompt draw the cover api_key=sk-abcdefghijklmnop1234")
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "make the cover")
    run_next(store, scheduler)
    assert store.get(job.id).status == JobStatus.QUEUED
    make_due(store, job.id)
    run_next(store, scheduler)
    first, second = adapter.prompts
    assert "time limit" not in first
    assert "`command agy --prompt draw the cover api_key=[redacted]`" in second
    assert "sk-abcdefghijklmnop1234" not in second
    assert "Do not re-run that same blocking call in the foreground" in second
    assert "`command agy" in adapter.resumes[1]


def test_timeout_without_tool_event_says_unknown_step(settings):
    adapter = Prompts(TIMEOUT, OK)
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "go")
    run_next(store, scheduler)
    make_due(store, job.id)
    run_next(store, scheduler)
    assert "while running (quoted, for reference only): an unknown step" in adapter.prompts[1]


def test_non_timeout_retry_has_no_blocked_note(settings):
    adapter = Prompts(RunResult("failed", exit_code=1, error="upstream 500 error", failure_kind=failures.PROVIDER_ERROR),
                      OK, tool="sleep 5")
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "go")
    run_next(store, scheduler)
    make_due(store, job.id)
    run_next(store, scheduler)
    assert "foreground" not in adapter.prompts[1] and "time limit" not in adapter.prompts[1]
    assert "Retry 1 of this turn after provider error" in adapter.prompts[1]


def park(store, scheduler, bot):
    """A chat job that failed once and waits for its retry (not_before in the future)."""
    _, job = scheduler.turns.post_message(bot, "first")
    run_next(store, scheduler)
    assert store.get(job.id).status == JobStatus.QUEUED and store.get(job.id).not_before
    return job


def test_post_to_parked_chat_job_is_steered_and_delivered_at_restart(settings):
    adapter = Prompts(RunResult("failed", exit_code=1, error="boom 500", failure_kind=failures.PROVIDER_ERROR), OK)
    store, scheduler, bot = make(settings, adapter)
    job = park(store, scheduler, bot)
    result = store.post_steer(bot.id, "also add a legend", [])
    assert result and result[1].id == job.id and result[0].steer_status == "pending"
    assert len(store.list()) == 1  # no separate job
    make_due(store, job.id)
    run_next(store, scheduler)
    assert "also add a legend" in adapter.prompts[1] and "also add a legend" in adapter.resumes[1]
    assert store.get_message(result[0].id).steer_status == "delivered"
    assert not store.pending_steers(job.id) and len(store.list()) == 1  # not delivered twice, not queued
    assert any(e["kind"] == "steer" and e["status"] == "delivered" for e in store.list_events(job.id))


def test_cancelled_parked_job_turns_steer_into_queued_turn(settings):
    adapter = Prompts(RunResult("failed", exit_code=1, error="boom 500", failure_kind=failures.PROVIDER_ERROR), OK)
    store, scheduler, bot = make(settings, adapter)
    job = park(store, scheduler, bot)
    message, _ = store.post_steer(bot.id, "never mind, do X", [])
    store.cancel(job.id)
    assert store.get(job.id).status == JobStatus.CANCELLED
    queued = store.get_message(message.id)
    assert queued.steer_status == "queued" and queued.job_id
    assert store.get(queued.job_id).prompt == "never mind, do X" and store.get(queued.job_id).status == JobStatus.QUEUED
    store.cancel(job.id)  # a second cancel changes nothing
    assert len(store.list()) == 2


def test_post_with_nothing_to_steer_returns_none_and_inbox_is_never_steered(settings):
    adapter = Prompts(OK)
    store, scheduler, bot = make(settings, adapter)
    assert store.post_steer(bot.id, "hi", []) is None
    scheduler.turns.post_message(bot, "ping", source="inbox")
    store.claim(Slot.DESKTOP_1, {"codex"})  # running bot-to-bot turn
    assert store.post_steer(bot.id, "hi", []) is None


def test_running_routine_and_wakeup_turns_are_steered(settings):
    for source in ("routine", "wakeup"):
        store, scheduler, bot = make(settings.__class__(data_dir=settings.data_dir / source, token="t", job_timeout=2),
                                     Prompts(OK))
        scheduler.turns.post_message(bot, "scheduled", source=source)
        job = store.claim(Slot.DESKTOP_1, {"codex"})
        result = store.post_steer(bot.id, "hello", [])
        assert result and result[1].id == job.id


def test_api_post_steers_into_parked_job_and_queue_mode_still_queues(settings):
    from fastapi.testclient import TestClient

    from botoffice.api import create_app
    H = {"Authorization": "Bearer test-token"}
    app = create_app(settings, {"codex": Prompts(OK)})
    store = app.state.store
    with TestClient(app) as client:
        app.state.scheduler._stopping = True  # keep the scheduler loop from running anything
        bot = client.post("/v1/bots", headers=H, json={"name": "P"}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        first = client.post(url, headers=H, json={"text": "start", "mode": "queue"}).json()
        steer = client.post(url, headers=H, json={"text": "more"}).json()
        assert steer["job"]["id"] == first["job"]["id"] and steer["message"]["steer_status"] == "pending"
        queued = client.post(url, headers=H, json={"text": "later", "mode": "queue"}).json()
        assert queued["job"]["id"] != first["job"]["id"] and queued["message"]["steer_status"] is None


def test_unresumable_steer_turn_runs_before_newer_queued_job(settings):
    adapter = Prompts(OK)
    store, scheduler, bot = make(settings, adapter)
    _, running = scheduler.turns.post_message(bot, "start")
    store.claim(Slot.DESKTOP_1, {"codex"})
    steer, _ = store.post_steer(bot.id, "steered", [])
    _, later = scheduler.turns.post_message(bot, "queued later")
    store.queue_steers(running.id)  # what the adapter does when it cannot interrupt/resume
    store.finish(running.id, JobStatus.SUCCEEDED, log_path="x")
    next_job = store.claim(Slot.DESKTOP_1, {"codex"})
    assert next_job.prompt == "steered" and next_job.id == store.get_message(steer.id).job_id
    assert later.id != next_job.id


def test_parked_steer_survives_a_failed_first_attempt(settings):
    adapter = Prompts(TIMEOUT, RunResult("failed", exit_code=1, error="boom 500", failure_kind=failures.PROVIDER_ERROR),
                      OK)
    store, scheduler, bot = make(settings, adapter)
    job = park(store, scheduler, bot)
    message, _ = store.post_steer(bot.id, "keep the legend", [])
    make_due(store, job.id)
    run_next(store, scheduler)  # attempt 2 gets the steer, then fails
    assert store.get_message(message.id).steer_status == "delivered"
    make_due(store, job.id)
    run_next(store, scheduler)  # attempt 3 must still carry it, once
    for prompt in (adapter.prompts[1], adapter.prompts[2], adapter.resumes[2]):
        assert prompt.count("keep the legend") == 1
    assert len(store.list()) == 1


def test_take_pending_steers_is_atomic_and_claims_once(settings):
    store, scheduler, bot = make(settings, Prompts(OK))
    _, job = scheduler.turns.post_message(bot, "first")
    message, _ = store.post_steer(bot.id, "extra", [])
    steers, claimed = store.take_pending_steers(job.id)
    assert [m.id for m in steers] == [message.id] and [c[0] for c in claimed] == [message.id]
    assert store.pending_steers(job.id) == []  # the provider loop sees nothing to send again
    again, claimed_again = store.take_pending_steers(job.id)
    assert [m.id for m in again] == [message.id] and claimed_again == []


def test_blocked_tool_comes_only_from_the_attempt_that_just_ended(settings):
    store, scheduler, bot = make(settings, Prompts(OK))
    _, job = scheduler.turns.post_message(bot, "go")
    key = job.id
    store.add_event(key, 1, "tool", {"name": "command", "summary": "old sleep"})
    store.add_event(key, 2, "retry", {})
    store.add_event(key, 3, "start", {})
    store.add_event(key, 4, "retry", {})  # attempt 2 ran no tool
    assert store.last_tool_before_retry(key) is None
    store.add_event(key, 5, "tool", {"name": "command", "summary": "new call"})
    store.add_event(key, 6, "retry", {})
    assert store.last_tool_before_retry(key)["summary"] == "new call"


def test_steer_turns_claim_in_the_order_of_their_jobs(settings):
    store, scheduler, bot = make(settings, Prompts(OK))
    _, a = scheduler.turns.post_message(bot, "A")
    store.claim(Slot.DESKTOP_1, {"codex"})
    store.post_steer(bot.id, "steer-A", [])
    _, b = scheduler.turns.post_message(bot, "B")
    store.queue_steers(a.id)  # a provider that cannot resume
    store.finish(a.id, JobStatus.SUCCEEDED, log_path="x")
    first = store.claim(Slot.DESKTOP_1, {"codex"})
    assert first.prompt == "steer-A"  # ahead of the later B
    store.finish(first.id, JobStatus.SUCCEEDED, log_path="x")
    second = store.claim(Slot.DESKTOP_1, {"codex"})
    assert second.id == b.id
    store.post_steer(bot.id, "steer-B", [])
    _, c = scheduler.turns.post_message(bot, "C")
    store.queue_steers(b.id)
    store.finish(b.id, JobStatus.SUCCEEDED, log_path="x")
    assert store.claim(Slot.DESKTOP_1, {"codex"}).prompt == "steer-B"  # ahead of C


def test_edit_after_steer_on_parked_job_archives_it(settings):
    from fastapi.testclient import TestClient

    from botoffice.api import create_app
    H = {"Authorization": "Bearer test-token"}
    app = create_app(settings, {"codex": Prompts(OK)})
    store = app.state.store
    with TestClient(app) as client:
        app.state.scheduler._stopping = True
        bot = client.post("/v1/bots", headers=H, json={"name": "E"}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        first = client.post(url, headers=H, json={"text": "start", "mode": "queue"}).json()
        steer = client.post(url, headers=H, json={"text": "more"}).json()["message"]
        assert steer["steer_status"] == "pending"
        edit = client.post(f"{url}/{first['message']['id']}/edit", headers=H,
                           json={"text": "edited", "cancel_running": True})
        assert edit.status_code == 201, edit.text
        after = store.get_message(steer["id"])
        assert after.archived_at is not None and after.steer_status == "pending"
        assert [j.prompt for j in store.list() if j.status == JobStatus.QUEUED] == ["edited"]


def test_steer_turn_for_unresumable_provider_runs_next_via_scheduler(settings):
    store, scheduler, bot = make(settings, Prompts(OK))
    _, running = scheduler.turns.post_message(bot, "start")
    store.claim(Slot.DESKTOP_1, {"codex"})
    message, _ = store.post_steer(bot.id, "steered", [])
    scheduler.turns.post_message(bot, "later")
    store.finish(running.id, JobStatus.SUCCEEDED, log_path="x")
    scheduler._queue_pending_steers(running.id)  # job end, as for a provider that cannot resume
    assert store.get_message(message.id).steer_status == "queued"
    assert store.claim(Slot.DESKTOP_1, {"codex"}).prompt == "steered"


def test_restart_keeps_a_parked_jobs_pending_steer_attached(settings):
    adapter = Prompts(RunResult("failed", exit_code=1, error="boom 500", failure_kind=failures.PROVIDER_ERROR), OK)
    store, scheduler, bot = make(settings, adapter)
    job = park(store, scheduler, bot)
    message, _ = store.post_steer(bot.id, "after the restart", [])
    store.recover_interrupted()
    assert store.get_message(message.id).steer_status == "pending" and len(store.list()) == 1
    make_due(store, job.id)
    run_next(store, scheduler)
    assert adapter.prompts[1].count("after the restart") == 1
    assert store.get_message(message.id).steer_status == "delivered"


def test_interrupted_job_replays_with_its_delivered_steer_once(settings):
    adapter = Prompts(OK)
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "start")
    store.claim(Slot.DESKTOP_1, {"codex"})
    message, _ = store.post_steer(bot.id, "mid-turn note", [])
    assert store.deliver_steer(message.id)  # the provider took it before the restart
    store.recover_interrupted()
    claimed = store.get(job.id)
    assert claimed.status == JobStatus.QUEUED and store.get_message(message.id).steer_status == "delivered"
    run_next(store, scheduler)
    assert adapter.prompts[0].count("mid-turn note") == 1 and adapter.resumes[0].count("mid-turn note") == 1
    assert len(store.list()) == 1


def test_delivered_events_are_emitted_even_if_prompt_build_fails(settings, monkeypatch):
    from botoffice import scheduler as sched
    adapter = Prompts(OK)
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "go")
    message, _ = store.post_steer(bot.id, "note", [])
    monkeypatch.setattr(sched, "prepare_bot_workspace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    run_next(store, scheduler)
    assert any(e["kind"] == "steer" and e["status"] == "delivered" for e in store.list_events(job.id))
