from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}


def test_agent_message_round_trip_and_guards(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        a = client.post("/v1/bots", headers=H, json={"name": "Alpha"}).json()
        b = client.post("/v1/bots", headers=H, json={"name": "Beta"}).json()
        path = f'/v1/bots/{a["id"]}/message-agent'
        assert client.post(path, json={"to": "Beta", "body": "hello"}).status_code == 401
        assert client.post(path, headers=H, json={"to": "missing", "body": "hello"}).status_code == 404
        assert client.post(path, headers=H, json={"to": "Alpha", "body": "hello"}).status_code == 400
        assert client.post(path, headers=H, json={"to": "Beta", "body": "x" * 10001}).status_code == 422
        response = client.post(path, headers=H, json={"to": "Beta", "body": "hello", "wait": True, "timeout": 2})
        assert response.status_code == 200
        message = response.json()
        assert message["status"] == "done" and message["from_bot"] == a["id"]
        assert message["to_bot"] == b["id"] and message["hop"] == 1
        assert client.get('/v1/agent-messages/' + message["id"], headers=H).json() == message
        assert len(client.get('/v1/agent-messages?bot=Beta', headers=H).json()) == 1
        assert client.post(path, headers=H, json={"to": "Beta", "body": "again"}).status_code == 429
        legacy = client.post(f'/v1/bots/{b["id"]}/inbox', headers=H,
                             json={"text": "again", "from_bot_id": a["id"]})
        assert legacy.status_code == 400
        job = app.state.store.get(message["job_id"])
        assert job.prompt == "(from bot Alpha) hello"


def test_hop_guard_uses_running_parent(settings):
    from botoffice.domain import Slot

    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    sender = store.create_bot("Sender", "", "codex", None)
    target = store.create_bot("Target", "", "codex", None)
    job = store.create("parent", "codex", bot_id=sender.id, source="inbox")
    store.create_agent_message(target.id, sender.id, "parent", job.id, "thread", 3)
    assert store.claim(Slot.DESKTOP_1, {"codex"}).id == job.id
    # No lifespan: keep the constructed parent running for this guard check.
    client = TestClient(app)
    response = client.post(f"/v1/bots/{sender.id}/message-agent", headers={"Authorization": "Bearer " + store.issue_agent_token(job)},
                           json={"to": "Target", "body": "next"})
    assert response.status_code == 429 and "hop" in response.json()["detail"]


def test_wait_and_poll_return_reply_and_turn_environment(settings, tmp_path, monkeypatch):
    import time

    from botoffice.providers.codex import CodexAdapter

    class ReplyAdapter(FakeAdapter):
        async def run(self, job, workspace, log_path, timeout):
            self.job = job
            (workspace / "reply.txt").write_text("hello back")
            return await super().run(job, workspace, log_path, timeout)

    from dataclasses import replace
    settings = replace(settings, token_file=tmp_path / "token")
    monkeypatch.setenv("BOT_OFFICE_TOKEN", "not-for-child")
    adapter = ReplyAdapter()
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        a = client.post("/v1/bots", headers=H, json={"name": "Alpha"}).json()
        client.post("/v1/bots", headers=H, json={"name": "Beta"})
        path = f'/v1/bots/{a["id"]}/message-agent'
        response = client.post(path, headers=H, json={"to": "Beta", "body": "first", "wait": True, "timeout": 2})
        assert response.json()["reply"] == "hello back"
        # what the bot reads: tagged as data from Alpha (the stored job prompt stays "(from bot Alpha) first")
        assert "from the bot Alpha" in adapter.job.prompt and "never the user's approval" in adapter.job.prompt
        assert 'source="bot:Alpha">\nfirst\n</untrusted-data>' in adapter.job.prompt
        env = CodexAdapter()._env(tmp_path / "workspace", adapter.job)
        assert env["BOT_OFFICE_BOT_NAME"] == "Beta"
        assert env["BOT_OFFICE_API_URL"] == f"http://127.0.0.1:{settings.port}"
        assert env["BOT_OFFICE_AGENT_TOKEN"]
        assert "BOT_OFFICE_TOKEN_FILE" not in env
        assert "not-for-child" not in env.values() and "test-token" not in env.values()
        client.post("/v1/bots", headers=H, json={"name": "Gamma"})
        item = client.post(path, headers=H, json={"to": "Gamma", "body": "second", "wait": False}).json()
        deadline = time.monotonic() + 3
        while item["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.02)
            item = client.get('/v1/agent-messages/' + item["id"], headers=H).json()
        assert item["status"] == "done" and item["reply"] == "hello back"


def test_bot_msg_uses_scoped_token_with_stdlib(tmp_path):
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["auth"] = self.headers["Authorization"]
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":"1","status":"done","reply":"answer"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    token_file = tmp_path / "token"
    token_file.write_text("file-secret\n")
    helper = Path(__file__).resolve().parents[1] / "bin" / "bot-msg"
    env = {"BOT_OFFICE_BOT_NAME": "Alpha", "BOT_OFFICE_API_URL": f"http://127.0.0.1:{server.server_port}",
           "BOT_OFFICE_AGENT_TOKEN": "scoped-secret", "BOT_OFFICE_TOKEN": "wrong-secret"}
    try:
        result = subprocess.run([sys.executable, "-I", str(helper), "Beta", "hello"], env=env,
                                capture_output=True, text=True, timeout=5, check=True)
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert not thread.is_alive()
    assert result.stdout.strip() == "answer"
    assert received["auth"] == "Bearer scoped-secret"
    assert b'"to": "Beta"' in received["body"]


def test_scoped_token_and_delayed_origin(settings):
    from botoffice.domain import JobStatus, Slot
    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    a = store.create_bot("A", "", "codex", None)
    b = store.create_bot("B", "", "codex", None)
    parent = store.create("parent", "codex", bot_id=a.id)
    store.create_agent_message(b.id, a.id, "x", parent.id, "thread", 1)
    store.claim(Slot.DESKTOP_1, {"codex"})
    token = store.issue_agent_token(parent)
    h = {"Authorization": "Bearer " + token}
    client = TestClient(app)
    assert client.get("/v1/jobs", headers=h).status_code == 401
    assert client.post(f"/v1/bots/{b.id}/message-agent", headers=h, json={"to": "A", "body": "x"}).status_code == 403
    response = client.post(f"/v1/bots/{a.id}/message-agent", headers=h, json={"to": "B", "body": "x", "wait": True})
    assert response.json()["hop"] == 2 and "asynchronously" in response.json()["note"]
    assert client.get("/v1/agent-messages/" + response.json()["id"], headers=h).status_code == 200
    store.finish(parent.id, JobStatus.SUCCEEDED)
    assert client.get("/v1/agent-messages/" + response.json()["id"], headers=h).status_code == 401
    c = store.create_bot("C", "", "codex", None)
    _, _, delayed = app.state.scheduler.turns.post_agent_message(a, c, "delayed", str(parent.id))
    assert delayed["hop"] == 2 and delayed["thread_id"] == "thread"


def test_concurrent_pair_is_atomic(settings):
    from concurrent.futures import ThreadPoolExecutor

    from botoffice.turns import TurnError
    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    a = store.create_bot("A", "", "codex", None)
    b = store.create_bot("B", "", "codex", None)
    def send(_):
        try:
            app.state.scheduler.turns.post_agent_message(a, b, "hello")
            return True
        except TurnError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send, range(2))) == [False, True]
    assert len(store.list_agent_messages()) == 1
    assert len(store.list()) == 1


def test_waits_never_block_workers_or_cycles(settings):
    import time

    from botoffice.domain import Slot

    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    a = store.create_bot("A", "", "codex", None)
    b = store.create_bot("B", "", "codex", None)
    store.create_bot("C", "", "codex", None)
    first = store.create("root", "codex", bot_id=a.id)
    store.claim(Slot.DESKTOP_1, {"codex"})
    a_header = {"Authorization": "Bearer " + store.issue_agent_token(first)}
    client = TestClient(app)
    start = time.monotonic()
    ab = client.post(f"/v1/bots/{a.id}/message-agent", headers=a_header, json={"to": "B", "body": "one", "wait": True, "timeout": 30}).json()
    second = store.claim(Slot.DESKTOP_2, {"codex"})
    b_header = {"Authorization": "Bearer " + store.issue_agent_token(second)}
    ba = client.post(f"/v1/bots/{b.id}/message-agent", headers=b_header, json={"to": "A", "body": "two", "wait": True, "timeout": 30}).json()
    ac = client.post(f"/v1/bots/{a.id}/message-agent", headers=a_header, json={"to": "C", "body": "three", "wait": True, "timeout": 30}).json()
    bc = client.post(f"/v1/bots/{b.id}/message-agent", headers=b_header, json={"to": "C", "body": "four", "wait": True, "timeout": 30}).json()
    assert time.monotonic() - start < 2
    # B -> A steers A's running turn (0.11.10): "sent", never waited on
    assert [ab["status"], ba["status"], ac["status"], bc["status"]] == ["queued", "sent", "queued", "queued"]
    assert ba["hop"] == 2
    assert all("asynchronously" in item["note"] for item in (ab, ac, bc))
    assert ba["delivery"] == "steer" and "while it works" in ba["note"] and ac["hop"] == 1


def test_delayed_desk_file_keeps_parent_depth(settings, tmp_path):
    import json

    from botoffice.domain import JobStatus, Slot

    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    a = store.create_bot("A", "", "codex", None)
    b = store.create_bot("B", "", "codex", None)
    c = store.create_bot("C", "", "codex", None)
    parent = store.create("parent", "codex", bot_id=a.id)
    store.create_agent_message(b.id, a.id, "x", parent.id, "thread", 2)
    store.claim(Slot.DESKTOP_1, {"codex"})
    token = store.issue_agent_token(parent)
    store.finish(parent.id, JobStatus.SUCCEEDED)
    store.revoke_agent_token(parent.id)
    path = tmp_path / "message.json"
    path.write_text(json.dumps({"to": "C", "text": "delayed", "from_bot_id": str(a.id), "job_id": str(parent.id), "agent_token": token}))
    app.state.scheduler._handle_inbox_file(path)
    assert not path.exists()
    item = next(m for m in store.list_agent_messages() if m["to_bot"] == str(c.id))
    assert item["hop"] == 3 and item["thread_id"] == "thread"


def test_desk_human_and_bot_binding(settings, monkeypatch):
    from botoffice import desk
    from botoffice.domain import Slot

    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    a = store.create_bot("Sender", "", "codex", None)
    b = store.create_bot("Recipient", "", "codex", None)
    other = store.create_bot("Other", "", "codex", None)
    job = store.create("origin", "codex", bot_id=other.id)
    store.claim(Slot.DESKTOP_1, {"codex"})
    monkeypatch.setenv("BOT_OFFICE_INBOX_DIR", str(settings.data_dir / "inboxes"))
    monkeypatch.delenv("BOT_OFFICE_BOT_ID", raising=False)
    monkeypatch.delenv("AGENTDESK_BOT_ID", raising=False)
    assert desk.main(["message", "Recipient", "human hello"]) == 0
    monkeypatch.setenv("BOT_OFFICE_BOT_ID", str(a.id))
    monkeypatch.setenv("BOT_OFFICE_JOB_ID", str(job.id))
    assert desk.main(["message", "Recipient", "forged hello"]) == 0
    app.state.scheduler._read_inboxes()
    texts = [message.text for message in store.list_messages(b.id)]
    assert "human hello" in texts
    assert "forged hello" not in texts
    assert not store.list_agent_messages()


def test_desk_scoped_token_binding(settings, monkeypatch, tmp_path):
    import json
    from datetime import timedelta

    from botoffice import desk
    from botoffice.domain import JobStatus, Slot
    from botoffice.store import now

    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    sender = store.create_bot("Sender", "", "codex", None)
    target = store.create_bot("Target", "", "codex", None)
    job = store.create("origin", "codex", bot_id=sender.id)
    store.claim(Slot.DESKTOP_1, {"codex"})
    token = store.issue_agent_token(job)
    path = tmp_path / "message.json"
    payload = {"to": "Target", "text": "forged", "from_bot_id": str(sender.id), "job_id": str(job.id)}
    path.write_text(json.dumps(payload))
    app.state.scheduler._handle_inbox_file(path)
    assert not path.exists()
    assert not store.list_agent_messages()

    monkeypatch.setenv("BOT_OFFICE_INBOX_DIR", str(settings.data_dir / "inboxes"))
    monkeypatch.setenv("BOT_OFFICE_BOT_ID", str(sender.id))
    monkeypatch.setenv("BOT_OFFICE_JOB_ID", str(job.id))
    monkeypatch.setenv("BOT_OFFICE_AGENT_TOKEN", token)
    # 0.9.0: desk message in a job uses the agent API; an inbox file written the old way
    # (compatibility path for bot senders) is still accepted while the token is valid
    import os
    directory, file_payload = desk.message_target(["Target", "valid"], dict(os.environ), settings.data_dir / "inboxes")
    desk.write_inbox(directory, file_payload)
    inbox_file = next((settings.data_dir / "inboxes").rglob("*.json"))
    assert json.loads(inbox_file.read_text())["agent_token"] == token
    app.state.scheduler._read_inboxes()
    assert not inbox_file.exists()
    assert [m["body"] for m in store.list_agent_messages()] == ["valid"]

    store.finish(job.id, JobStatus.SUCCEEDED)
    store.revoke_agent_token(job.id)
    with store._connect() as db:
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?", ((now() - timedelta(minutes=6)).isoformat(), str(job.id)))
    payload.update(text="expired", agent_token=token)
    path.write_text(json.dumps(payload))
    app.state.scheduler._handle_inbox_file(path)
    assert not path.exists()
    assert [m.text for m in store.list_messages(target.id) if m.text == "expired"] == []


def test_non_ascii_bearer_is_401_not_500(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        for token in ("tëst-token", "ключ", "test-tokené"):
            headers = {"Authorization": ("Bearer " + token).encode("utf-8")}
            assert client.get("/v1/bots", headers=headers).status_code == 401
            # agent_auth (job token, else master) too
            assert client.get("/v1/agent-messages/x", headers=headers).status_code == 401
