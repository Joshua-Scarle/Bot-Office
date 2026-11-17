from botoffice.domain import JobStatus, Slot
from botoffice.store import JobStore


def test_store_create_list_get_cancel_and_idempotent(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    created = store.create("write a short report")
    assert created.status == JobStatus.QUEUED
    assert store.get(created.id) == created
    assert store.list() == [created]

    cancelled = store.cancel(created.id)
    assert cancelled.status == JobStatus.CANCELLED
    assert store.cancel(created.id) == cancelled
    assert store.get("missing") is None


def test_running_cancel_waits_for_scheduler_and_recovery_fails_interrupted_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    queued = store.create("queued")
    assert store.cancel(queued.id).status == JobStatus.CANCELLED

    running = store.create("running")
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    assert claimed.id == running.id
    assert store.cancel(running.id).status == JobStatus.RUNNING
    assert store.claim(Slot.DESKTOP_1, {"codex"}) is None

    # 0.9.0: the first restart re-queues the job once (same row); the next one fails it
    assert store.recover_interrupted() == [{"id": str(running.id), "bot_id": None, "outcome": "requeued"}]
    requeued = store.get(running.id)
    assert requeued.status == JobStatus.QUEUED and requeued.restart_attempts == 1 and requeued.slot is None
    store.claim(Slot.DESKTOP_1, {"codex"})
    store.recover_interrupted()
    recovered = store.get(running.id)
    assert recovered.status == JobStatus.FAILED
    assert recovered.error.startswith("interrupted by controller restart; not retried")
    assert recovered.finished_at is not None


def test_recovery_removes_orphan_tokens_and_runs_at_startup(settings):
    from fastapi.testclient import TestClient
    from botoffice.api import create_app
    from botoffice.providers.fake import FakeAdapter

    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    bot = store.create_bot("Recovery", "", "codex", None)
    job = store.create("running", "codex", bot_id=bot.id)
    running = store.claim(Slot.DESKTOP_1, {"codex"})
    token = store.issue_agent_token(running)
    assert store.agent_token_job(token) is not None
    with store._connect() as db:
        db.execute("INSERT INTO agent_tokens VALUES (?, ?, ?)", ("orphan", "missing", str(bot.id)))
    with TestClient(app):
        assert store.get(job.id).status != JobStatus.RUNNING or store.get(job.id).restart_attempts == 1
        assert store.agent_token_job(token) is None
        with store._connect() as db:
            assert db.execute("SELECT count(*) FROM agent_tokens WHERE token_hash='orphan'").fetchone()[0] == 0
