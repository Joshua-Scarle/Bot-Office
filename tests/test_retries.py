"""0.9.0 retries: same-row re-queue with backoff, caps, attempt-aware events (A2)."""
import asyncio
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from botoffice import failures
from botoffice.api import create_app
from botoffice.config import Settings
from botoffice.domain import JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.streaming import emit_event

H = {"Authorization": "Bearer test-token"}
SSE = {**H, "Accept": "text/event-stream"}


class Scripted:
    """Returns the scripted results in order (the last one repeats)."""

    name = "codex"
    enabled = True

    def __init__(self, *results, during=None):
        self.results = list(results)
        self.runs = 0
        self.during = during

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        self.runs += 1
        log_path.write_text("log")
        emit_event("text", text=f"draft {self.runs}")
        if self.during:
            self.during(job)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def provider_error(text="upstream 500 error"):
    return RunResult("failed", exit_code=1, error=text, failure_kind=failures.PROVIDER_ERROR)


OK = RunResult("succeeded", exit_code=0, final_text="all done")


def make(settings, adapter):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    return store, scheduler, bot


def make_due(store, job_id):
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET not_before=? WHERE id=?",
                   ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), str(job_id)))


def run_next(store, scheduler):
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    assert claimed is not None
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, claimed))
    return claimed


def test_provider_error_retries_twice_with_growing_delays_then_fails(settings):
    adapter = Scripted(provider_error())
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "do it")
    run_next(store, scheduler)
    first = store.get(job.id)
    assert first.status == JobStatus.QUEUED and first.attempts == 1 and first.failure_kind == "provider_error"
    assert timedelta(minutes=1, seconds=50) < first.not_before - datetime.now(UTC) <= timedelta(minutes=2)
    assert store.claim(Slot.DESKTOP_1, {"codex"}) is None  # not due yet
    make_due(store, job.id)
    run_next(store, scheduler)
    second = store.get(job.id)
    assert second.status == JobStatus.QUEUED and second.attempts == 2
    assert timedelta(minutes=9) < second.not_before - datetime.now(UTC) <= timedelta(minutes=10)
    make_due(store, job.id)
    run_next(store, scheduler)
    final = store.get(job.id)
    assert final.status == JobStatus.FAILED and final.id == job.id and adapter.runs == 3
    lines = [m.text for m in store.list_messages(bot.id) if m.role == "system"]
    assert len(lines) == 3 and all("upstream 500 error" in line for line in lines)
    assert "Will retry at" in lines[0] and "Will retry at" in lines[1] and "Will retry" not in lines[2]
    attempts = store.list_attempts(job.id)
    assert [a["attempt"] for a in attempts] == [0, 1] and all(a["error"] == "upstream 500 error" for a in attempts)
    # events are attempt-aware; only the last attempt has a done
    events = store.list_events(job.id)
    assert [e["attempt"] for e in events if e["kind"] == "retry"] == [1, 2]
    assert [e["attempt"] for e in events if e["kind"] == "done"] == [2]
    assert {e["attempt"] for e in events if e["kind"] == "start"} == {0, 1, 2}


def test_auth_failure_never_retries(settings):
    adapter = Scripted(RunResult("failed", exit_code=1, error="Not logged in", failure_kind=failures.AUTH))
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "x")
    run_next(store, scheduler)
    assert store.get(job.id).status == JobStatus.FAILED and store.get(job.id).attempts == 0


def test_usage_limit_waits_for_reset_plus_two_minutes(settings):
    reset = datetime.now(UTC) + timedelta(hours=3)
    adapter = Scripted(RunResult("failed", exit_code=1, error="You've hit your 5-hour limit",
                                 failure_kind=failures.USAGE_LIMIT, retry_at=reset))
    adapter.name = "codex"
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "x")
    run_next(store, scheduler)
    saved = store.get(job.id)
    assert saved.status == JobStatus.QUEUED and saved.failure_kind == "usage_limit"
    assert abs((saved.not_before - (reset + timedelta(minutes=2))).total_seconds()) < 1
    line = next(m.text for m in store.list_messages(bot.id) if m.role == "system")
    first_line = line.split("\n")[0]
    assert first_line.startswith("Codex usage limit reached; resets ") and "Will retry then." in first_line
    assert first_line.endswith("Codex said: \u201cYou've hit your 5-hour limit\u201d")


def test_global_hourly_cap_stops_retries(settings):
    settings = Settings(data_dir=settings.data_dir, token="t", job_timeout=2, max_retries_per_hour=1)
    adapter = Scripted(provider_error())
    store, scheduler, bot = make(settings, adapter)
    _, one = scheduler.turns.post_message(bot, "one")
    run_next(store, scheduler)
    assert store.get(one.id).status == JobStatus.QUEUED
    store.cancel(one.id)
    _, two = scheduler.turns.post_message(bot, "two")
    run_next(store, scheduler)
    failed = store.get(two.id)
    assert failed.status == JobStatus.FAILED and "automatic retries per hour" in failed.error


def test_retried_job_keeps_id_and_rewind_drops_the_retried_reply(settings):
    def archive_turn(job):
        if job.attempts == 1:  # the edit lands while the retry is running
            with closing(store._connect()) as db:
                db.execute("UPDATE messages SET archived_at=? WHERE job_id=?", (datetime.now(UTC).isoformat(), str(job.id)))

    adapter = Scripted(provider_error(), OK, during=archive_turn)
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "hi")
    run_next(store, scheduler)
    make_due(store, job.id)
    second = run_next(store, scheduler)
    assert second.id == job.id and store.get(job.id).status == JobStatus.SUCCEEDED
    assert store.get(job.id).reply_text == "all done"  # the job keeps it ...
    texts = [m.text for m in store.list_messages(bot.id, include_archived=True)]
    assert "all done" not in texts  # ... the rewound chat does not


def test_manual_retry_api_sse_spans_attempts_and_edit_blocks_retry(settings):
    adapter = Scripted(RunResult("failed", exit_code=1, error="Not logged in", failure_kind=failures.AUTH), OK)
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).json()
        job_id = posted["job"]["id"]
        deadline = time.monotonic() + 5
        while store.get(job_id).status != JobStatus.FAILED and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.post(f"/v1/jobs/{job_id}/retry", headers=H).status_code == 200
        assert client.post(f"/v1/jobs/{job_id}/retry", headers=H).status_code == 409  # already queued
        body = client.get(f"/v1/jobs/{job_id}/events", headers=SSE).text  # ends at the CURRENT attempt's done
        frames = [line for line in body.splitlines() if line.startswith("event: ")]
        assert frames.count("event: done") == 2 and "event: retry" in frames
        assert store.get(job_id).status == JobStatus.SUCCEEDED and store.get(job_id).attempts == 1
        texts = [m["text"] for m in client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json()]
        assert "all done" in texts
        # a queued or running (or succeeded) job cannot be retried
        assert client.post(f"/v1/jobs/{job_id}/retry", headers=H).status_code == 409
        # a failed turn that was rewound by an edit cannot be retried either
        adapter.results = [RunResult("failed", exit_code=1, error="Not logged in", failure_kind=failures.AUTH)]
        again = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "two"}, headers=H).json()
        while store.get(again["job"]["id"]).status != JobStatus.FAILED and time.monotonic() < deadline + 5:
            time.sleep(0.02)
        adapter.results = [OK]
        edit = client.post(f"/v1/bots/{bot['id']}/messages/{again['message']['id']}/edit", json={"text": "2"}, headers=H)
        assert edit.status_code == 201, edit.text
        assert client.post(f"/v1/jobs/{again['job']['id']}/retry", headers=H).status_code == 409


def test_sse_reconnect_while_waiting_for_retry_stays_open(settings, monkeypatch):
    monkeypatch.setattr(failures, "PROVIDER_DELAYS", (timedelta(seconds=0.6),))
    adapter = Scripted(provider_error(), OK)
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        job_id = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).json()["job"]["id"]
        deadline = time.monotonic() + 5
        while store.get(job_id).attempts != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = client.get(f"/v1/jobs/{job_id}/events", headers=H).json()
        assert snapshot["done"] is False and snapshot["draft"] == ""  # old draft cleared
        pending = client.get(f"/v1/bots/{bot['id']}/messages?pending=true", headers=H).json()
        assert any(m["pending"] and m["job_id"] == job_id for m in pending)
        body = client.get(f"/v1/jobs/{job_id}/events", headers=SSE).text  # reconnect during the wait
        assert body.count("event: done") == 1 and '"attempt": 1' in body.split("event: done")[1]
        assert store.get(job_id).status == JobStatus.SUCCEEDED


# --- review round 1 ---

def test_requeue_and_its_retry_event_commit_together(settings):
    store, scheduler, bot = make(settings, Scripted(provider_error()))
    _, job = scheduler.turns.post_message(bot, "x")
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    store.set_draft(job.id, "old draft")
    assert store.requeue(job.id, datetime.now(UTC), "provider_error", error="boom token=abcdef123456",
                         reply_text=None, max_per_hour=20)
    saved = store.get(job.id)
    assert saved.status == JobStatus.QUEUED and saved.draft_text is None
    assert "abcdef123456" not in saved.error
    events = store.list_events(job.id)
    assert events[-1]["kind"] == "retry" and events[-1]["attempt"] == claimed.execution + 1
    # the scheduler path writes exactly one retry event per re-queue (no second one from the sink)
    store2, scheduler2, bot2 = make(settings.__class__(data_dir=settings.data_dir / "b", token="t", job_timeout=2),
                                    Scripted(provider_error()))
    _, job2 = scheduler2.turns.post_message(bot2, "x")
    run_next(store2, scheduler2)
    assert [e["kind"] for e in store2.list_events(job2.id)].count("retry") == 1


def test_stored_error_is_redacted_and_clipped(settings):
    secret = "sk-ant-" + "A1" * 20
    adapter = Scripted(RunResult("failed", exit_code=1, error=f"auth header {secret} " + "x" * 5000,
                                 failure_kind=failures.TOOL_DENIED))
    store, scheduler, bot = make(settings, adapter)
    _, job = scheduler.turns.post_message(bot, "x")
    run_next(store, scheduler)
    saved = store.get(job.id)
    assert secret not in saved.error and len(saved.error) <= 2000
    assert all(secret not in m.text for m in store.list_messages(bot.id))


def test_manual_retries_are_capped_per_job(settings):
    import pytest

    from botoffice.store import MAX_MANUAL_RETRIES

    store, scheduler, bot = make(settings, Scripted(provider_error()))
    _, job = scheduler.turns.post_message(bot, "x")
    for _ in range(MAX_MANUAL_RETRIES):
        store.claim(Slot.DESKTOP_1, {"codex"})
        store.finish(job.id, JobStatus.FAILED, error="boom")
        assert store.retry_now(job.id).status == JobStatus.QUEUED
    store.claim(Slot.DESKTOP_1, {"codex"})
    store.finish(job.id, JobStatus.FAILED, error="boom")
    with pytest.raises(ValueError, match="already retried 10 times"):
        store.retry_now(job.id)
    assert store.get(job.id).status == JobStatus.FAILED
    with TestClient(create_app(settings, {"codex": Scripted(provider_error())})) as client:
        response = client.post(f"/v1/jobs/{job.id}/retry", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 409
