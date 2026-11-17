"""0.10.0 assignments: tables, master API, start / stop / reply (B-1)."""
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}


def client_for(settings):
    return TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))


def make(client, name="Ada", **body):
    bot = client.post("/v1/bots", headers=H, json={"name": name}).json()
    body = {"title": "Ship the report", "goal": "Write it", "done_when": "report.md exists", **body}
    response = client.post(f"/v1/bots/{bot['id']}/assignments", headers=H, json=body)
    assert response.status_code == 201, response.text
    return bot, response.json()


def jobs_of(store, assignment_id):
    return [job for job in store.list() if job.assignment_id and str(job.assignment_id) == assignment_id]


def test_create_list_get_and_edit(settings):
    with client_for(settings) as client:
        bot, item = make(client)
        assert item["status"] == "open" and item["budget_turns"] == 20 and item["created_by"] == "user"
        listed = client.get(f"/v1/bots/{bot['id']}/assignments", headers=H).json()
        assert [a["id"] for a in listed] == [item["id"]]
        patched = client.patch(f"/v1/assignments/{item['id']}", headers=H,
                               json={"budget_turns": 5, "escalate": "always", "verify": True}).json()
        assert patched["budget_turns"] == 5 and patched["escalate"] == "always" and patched["verify"] is True
        detail = client.get(f"/v1/assignments/{item['id']}", headers=H).json()
        assert [e["kind"] for e in detail["events"]] == ["created", "edited"]
        assert client.get("/v1/assignments/" + "0" * 32, headers=H).status_code == 404
        assert client.post(f"/v1/bots/{bot['id']}/assignments", json={"title": "x"}).status_code == 401


def test_start_queues_one_priority_turn_and_stop_cancels_it(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        _, item = make(client, start=True)
        assert item["status"] == "active"
        (job,) = jobs_of(store, item["id"])
        assert job.source == "assignment" and job.assignment_turn == 1 and job.status == "queued"
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute("SELECT priority FROM jobs WHERE id=?", (str(job.id),)).fetchone()[0] == 1
        # starting again while a turn is queued adds nothing
        client.post(f"/v1/assignments/{item['id']}/start", headers=H)
        assert len(jobs_of(store, item["id"])) == 1
        stopped = client.post(f"/v1/assignments/{item['id']}/stop", headers=H).json()
        assert stopped["status"] == "open"
        assert store.get(job.id).status == "cancelled"


def test_reply_activates_and_queues_a_user_turn(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        _, item = make(client)
        reply = client.post(f"/v1/assignments/{item['id']}/reply", headers=H, json={"text": "use the Q3 numbers"}).json()
        assert reply["assignment"]["status"] == "active"
        job = store.get(reply["job_id"])
        assert "use the Q3 numbers" in job.prompt and str(job.assignment_id) == item["id"]
        message = store.job_message(job.bot_id, job.id)
        assert message.text == "use the Q3 numbers" and message.sender is None
        client.patch(f"/v1/assignments/{item['id']}", headers=H, json={"status": "done"})
        assert store.get(job.id).status == "cancelled"  # closing cancels its queued turns
        assert client.post(f"/v1/assignments/{item['id']}/reply", headers=H, json={"text": "x"}).status_code == 409


def test_bot_status_comes_from_assignments(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        _, item = make(client)
        Assignments(store).update(item["id"], {"status": "dropped"})
        _, second = make(client, name="Bea")
        with closing(store._connect()) as db:
            db.execute("UPDATE assignments SET status='waiting_on_user' WHERE id=?", (second["id"],))
        assert Assignments(store).bot_states() == {second["bot_id"]: "waiting"}
        with closing(store._connect()) as db:
            db.execute("UPDATE assignments SET status='blocked' WHERE id=?", (second["id"],))
        assert Assignments(store).bot_states() == {second["bot_id"]: "blocked"}


def test_bot_status_waiting_shows_in_bot_list(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(delay=60)})) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "Wes"}).json()
        item = client.post(f"/v1/bots/{bot['id']}/assignments", headers=H, json={"title": "t"}).json()
        with closing(client.app.state.store._connect()) as db:
            db.execute("UPDATE assignments SET status='waiting_on_user' WHERE id=?", (item["id"],))
        listed = {b["name"]: b for b in client.get("/v1/bots", headers=H).json()}
        assert listed["Wes"]["status"] == "waiting"


def test_deleting_a_bot_drops_its_assignments(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        bot, item = make(client, start=True)
        assert client.delete(f"/v1/bots/{bot['id']}", headers=H).status_code == 204
        assert Assignments(store).get(item["id"]).status == "dropped"


def test_bot_turn_timeout_and_notify_settings(settings):
    with client_for(settings) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "Tim"}).json()
        assert bot["job_timeout"] is None and bot["notify"] is True
        updated = client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"job_timeout": 900, "notify": False}).json()
        assert updated["job_timeout"] == 900 and updated["notify"] is False
        assert client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"job_timeout": 30}).status_code == 422
        assert client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"job_timeout": None}).json()["job_timeout"] is None


def test_migration_is_additive_on_a_090_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    JobStore(path)  # current schema
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE assignments")
        db.execute("DROP TABLE wakeups")
        db.execute("DROP INDEX jobs_assignment_turn")
    store = JobStore(path)
    with closing(store._connect()) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    assert {"assignments", "assignment_events", "wakeups"} <= tables
    assert {"assignment_id", "assignment_turn", "assignment_settled", "dismissed_at"} <= columns
