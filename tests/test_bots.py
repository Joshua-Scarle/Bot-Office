import json
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}
NY = ZoneInfo("America/New_York")


class ReplyingFake(FakeAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.workspaces = []
        self.prompts = []

    async def run(self, job, workspace, log_path, timeout):
        self.workspaces.append(workspace)
        self.prompts.append(job.prompt)
        (workspace / "reply.txt").write_text("hi from bot", encoding="utf-8")
        (workspace / "memory.md").write_text("learned a thing", encoding="utf-8")
        (workspace / "shared-memory.md").write_text("shared fact", encoding="utf-8")
        return await super().run(job, workspace, log_path, timeout)


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def make_bot(client, **overrides):
    body = {"name": "helper", "instructions": "be helpful"} | overrides
    response = client.post("/v1/bots", json=body, headers=H)
    assert response.status_code == 201, response.text
    return response.json()


def test_chat_turn_prepares_workspace_and_records_reply(settings):
    fake = ReplyingFake()
    with TestClient(create_app(settings, {"codex": fake})) as client:
        bot = make_bot(client)
        assert oct((settings.data_dir / "bots" / bot["id"]).stat().st_mode & 0o777) == "0o700"
        client.put(f"/v1/bots/{bot['id']}/memory", json={"text": "old memory"}, headers=H)
        response = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hello"}, headers=H)
        assert response.status_code == 201
        assert response.json()["job"]["source"] == "chat"
        assert response.json()["job"]["bot_id"] == bot["id"]
        messages = wait_for(
            lambda: (m := client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json())
            and len(m) == 2
            and m
        )
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["text"] == "hi from bot"
        assert messages[1]["job_id"] == response.json()["job"]["id"]
        ws = fake.workspaces[0]
        for name in ("instructions.md", "memory.md", "shared-memory.md", "history.md", "DESKTOP.md", "prompt.txt"):
            assert (ws / name).exists()
        assert (ws / "instructions.md").read_text() == "be helpful"
        assert "hello" in fake.prompts[0] and "reply.txt" in fake.prompts[0]
        assert client.get(f"/v1/bots/{bot['id']}/memory", headers=H).json()["text"] == "learned a thing"
        assert client.get("/v1/shared-memory", headers=H).json()["text"] == "shared fact"


def test_failed_job_posts_short_error(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(result="needs_login")})) as client:
        bot = make_bot(client)
        client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hello"}, headers=H)
        messages = wait_for(
            lambda: len(m := client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json()) == 2 and m
        )
        assert messages[1]["role"] == "system" and "needs_login" in messages[1]["text"]


def test_claude_requires_reason_and_is_logged(settings):
    providers = {"codex": FakeAdapter(), "claude": FakeAdapter()}
    with TestClient(create_app(settings, providers)) as client:
        assert client.post("/v1/jobs", json={"prompt": "x", "provider": "claude"}, headers=H).status_code == 400
        assert client.post("/v1/jobs", json={"prompt": "x", "provider": "claude", "reason": " "}, headers=H).status_code == 400
        assert client.post("/v1/bots", json={"name": "c", "default_provider": "claude"}, headers=H).status_code == 400
        bot = make_bot(client, name="c", default_provider="claude", reason="needs deep review")
        assert client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).status_code == 201
        bare = make_bot(client, name="d")
        assert client.patch(f"/v1/bots/{bare['id']}", json={"default_provider": "claude"}, headers=H).status_code == 400
        job = client.post("/v1/jobs", json={"prompt": "SECRET PROMPT", "provider": "claude", "reason": "audit"}, headers=H)
        assert job.status_code == 201 and job.json()["reason"] == "audit"
        log = settings.data_dir / "claude-use-log.tsv"
        assert log.stat().st_mode & 0o777 == 0o600
        lines = log.read_text().splitlines()
        assert [line.split("\t")[2] for line in lines] == ["needs deep review", "audit"]
        assert "SECRET" not in log.read_text() and "test-token" not in log.read_text()


def test_cursor_job_and_default_codex(settings):
    providers = {"codex": FakeAdapter(), "cursor": FakeAdapter(), "antigravity": FakeAdapter()}
    with TestClient(create_app(settings, providers)) as client:
        assert client.post("/v1/jobs", json={"prompt": "x", "provider": "cursor"}, headers=H).json()["provider"] == "cursor"
        assert client.post("/v1/jobs", json={"prompt": "x", "provider": "antigravity"}, headers=H).json()["provider"] == "antigravity"
        assert client.post("/v1/jobs", json={"prompt": "x"}, headers=H).json()["provider"] == "codex"
        assert client.post("/v1/jobs", json={"prompt": "x", "provider": "grok"}, headers=H).status_code == 400


def test_all_v1_routes_require_auth_and_root_is_open(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = make_bot(client)
        uid = bot["id"]
        for method, path in [
            ("get", "/v1/jobs"), ("post", "/v1/jobs"), ("get", "/v1/slots"), ("get", "/v1/providers"),
            ("get", "/v1/bots"), ("post", "/v1/bots"), ("get", f"/v1/bots/{uid}"), ("patch", f"/v1/bots/{uid}"),
            ("get", f"/v1/bots/{uid}/memory"), ("put", f"/v1/bots/{uid}/memory"),
            ("get", "/v1/shared-memory"), ("put", "/v1/shared-memory"),
            ("get", f"/v1/bots/{uid}/messages"), ("post", f"/v1/bots/{uid}/messages"),
            ("post", f"/v1/bots/{uid}/inbox"), ("get", f"/v1/bots/{uid}/routines"),
            ("post", f"/v1/bots/{uid}/routines"), ("patch", f"/v1/routines/{uid}"),
            ("delete", f"/v1/routines/{uid}"), ("post", "/v1/slots/desktop-1/exec"),
        ]:
            assert getattr(client, method)(path).status_code == 401, (method, path)
        page = client.get("/")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert "test-token" not in page.text and "http://" not in page.text and "https://" not in page.text
        assert client.get("/openapi.json").status_code == 404


def test_bot_crud_validation(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = make_bot(client)
        assert client.post("/v1/bots", json={"name": "helper"}, headers=H).status_code == 409
        assert client.post("/v1/bots", json={"name": ""}, headers=H).status_code == 422
        assert client.post("/v1/bots", json={"name": "x" * 81}, headers=H).status_code == 422
        assert client.post("/v1/bots", json={"name": "y", "default_provider": "grok"}, headers=H).status_code == 422
        patched = client.patch(f"/v1/bots/{bot['id']}", json={"name": "renamed"}, headers=H).json()
        assert patched["name"] == "renamed" and patched["instructions"] == "be helpful"
        assert [b["name"] for b in client.get("/v1/bots", headers=H).json()] == ["renamed"]
        assert client.get(f"/v1/bots/{os.urandom(16).hex()}", headers=H).status_code == 404
        assert (settings.data_dir / "shared" / "memory.md").exists()


def test_routine_due_creates_job_without_overlap(settings):
    fake = FakeAdapter(delay=0.3)
    app = create_app(settings, {"codex": fake})
    with TestClient(app) as client:
        bot = make_bot(client)
        assert client.post(f"/v1/bots/{bot['id']}/routines", json={"cron": "bad", "prompt": "p"}, headers=H).status_code == 422
        routine = client.post(
            f"/v1/bots/{bot['id']}/routines", json={"cron": "*/5 * * * *", "prompt": "daily check"}, headers=H
        ).json()
        scheduler = app.state.scheduler
        scheduler.clock = lambda: datetime(2026, 11, 12, 9, 5, 30, tzinfo=NY)
        wait_for(lambda: [j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "routine"])
        jobs = [j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "routine"]
        assert len(jobs) == 1 and jobs[0]["prompt"] == "daily check"
        stored = client.get(f"/v1/bots/{bot['id']}/routines", headers=H).json()[0]
        assert stored["last_run_at"].startswith("2026-11-12T09:05:00")
        scheduler.clock = lambda: datetime(2026, 11, 12, 9, 10, 0, tzinfo=NY)
        scheduler.tick()
        if jobs[0]["status"] != "succeeded":
            assert len([j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "routine"]) == 1
        client.patch(f"/v1/routines/{routine['id']}", json={"enabled": False}, headers=H)
        assert client.delete(f"/v1/routines/{routine['id']}", headers=H).status_code == 204
        assert client.delete(f"/v1/routines/{routine['id']}", headers=H).status_code == 404


def test_inbox_post_and_dropped_file_trigger_one_turn_each(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        sender = make_bot(client, name="alice")
        target = make_bot(client, name="bob")
        posted = client.post(
            f"/v1/bots/{target['id']}/inbox", json={"text": "ping", "from_bot_id": sender["id"]}, headers=H
        )
        assert posted.status_code == 400

        other = make_bot(client, name="charlie")
        inbox = settings.data_dir / "inboxes" / "_by-name"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "good.json").write_text(json.dumps({"to": "bob", "text": "dropped", "from_bot_id": other["id"]}))
        (inbox / "anonymous.json").write_text(json.dumps({"to": "bob", "text": "anonymous"}))
        (inbox / "bad.json").write_text("{not json")
        (inbox / "odd.json").write_text(json.dumps({"to": 5, "text": "x"}))
        (inbox / "ghost.json").write_text(json.dumps({"to": "nobody", "text": "x"}))
        app.state.scheduler.tick()
        app.state.scheduler.tick()
        inbox_jobs = [j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "inbox"]
        assert len(inbox_jobs) == 1
        assert [p.name for p in inbox.glob("*.json")] == []
        texts = [m["text"] for m in client.get(f"/v1/bots/{target['id']}/messages", headers=H).json()]
        assert texts.count("dropped") == 0
        assert texts.count("anonymous") == 1


def test_store_migrates_existing_jobs_table(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE jobs (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
        slot TEXT, provider TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
        finished_at TEXT, exit_code INTEGER, log_path TEXT, error TEXT)"""
    )
    db.execute("INSERT INTO jobs (id,prompt,status,provider,created_at) VALUES ('7b1f2c0e-1111-4222-8333-444455556666','p','queued','codex','2026-01-01T00:00:00+00:00')")
    db.commit()
    db.close()
    store = JobStore(path)
    [job] = store.list()
    assert job.source == "api" and job.bot_id is None

def test_routine_can_be_moved_to_another_bot_and_enabled(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        source = make_bot(client, name="Source")
        dest = make_bot(client, name="Dest")
        routine = client.post(
            f"/v1/bots/{source['id']}/routines",
            json={"cron": "25 11 * * 3", "prompt": "pulse", "enabled": False, "name": "competitor-pulse"},
            headers=H,
        ).json()
        assert routine["enabled"] is False and routine["bot_id"] == source["id"]
        moved = client.patch(
            f"/v1/routines/{routine['id']}",
            json={"bot_id": dest["id"], "enabled": True},
            headers=H,
        )
        assert moved.status_code == 200, moved.text
        body = moved.json()
        assert body["bot_id"] == dest["id"] and body["enabled"] is True and body["name"] == "competitor-pulse"
        assert client.get(f"/v1/bots/{source['id']}/routines", headers=H).json() == []
        listed = client.get(f"/v1/bots/{dest['id']}/routines", headers=H).json()
        assert len(listed) == 1 and listed[0]["id"] == routine["id"]
        missing = client.patch(
            f"/v1/routines/{routine['id']}",
            json={"bot_id": "00000000-0000-0000-0000-000000000000"},
            headers=H,
        )
        assert missing.status_code == 404



def test_get_and_patch_bot_report_the_live_status_like_the_list(settings):
    fake = FakeAdapter(delay=1.0)
    with TestClient(create_app(settings, {"codex": fake})) as client:
        bot = make_bot(client)
        assert client.get(f"/v1/bots/{bot['id']}", headers=H).json()["status"] == "idle"
        client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "work"}, headers=H)
        wait_for(lambda: client.get("/v1/bots", headers=H).json()[0]["status"] == "working")
        got = client.get(f"/v1/bots/{bot['id']}", headers=H).json()
        assert got["status"] == "working" and got["activity"] == "running"
        patched = client.patch(f"/v1/bots/{bot['id']}", json={"description": "d"}, headers=H).json()
        assert patched["status"] == "working" and patched["activity"] == "running"


def test_opencode_models_must_be_provider_slash_model(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        for model in ("claude-sonnet-5-5", "x/", "a//b", "a/b/"):
            assert client.post("/v1/bots", json={"name": "bad", "default_provider": "opencode", "model": model},
                               headers=H).status_code == 400
        # ids the generic model-id pattern already refuses (leading / or -, whitespace) never get that far
        for model in ("/x", "-a/b", "a/ b", "a /b", "a/b\tc"):
            assert client.post("/v1/bots", json={"name": "bad", "default_provider": "opencode", "model": model},
                               headers=H).status_code in (400, 422)
        oc = client.post("/v1/bots", json={"name": "oc", "default_provider": "opencode", "model": "openai/gpt-5"}, headers=H)
        assert oc.status_code == 201
        url = f"/v1/bots/{oc.json()['id']}"
        assert client.patch(url, json={"model": "claude-sonnet-5-5"}, headers=H).status_code == 400
        assert client.get(url, headers=H).json()["model"] == "openai/gpt-5"
        assert client.patch(url, json={"model": "anthropic/claude-sonnet-4-5"}, headers=H).status_code == 200
        # nested ids (a gateway provider in front of the vendor/model) are fine
        nested = client.patch(url, json={"model": "openrouter/anthropic/claude-x"}, headers=H)
        assert nested.status_code == 200 and nested.json()["model"] == "openrouter/anthropic/claude-x"
        assert client.post("/v1/bots", json={"name": "nested", "default_provider": "opencode",
                                             "model": "openrouter/anthropic/claude-x"}, headers=H).status_code == 201
        # switching to opencode with a plain model, or a codex bot taking a plain model, are judged per engine
        bot = make_bot(client, name="plain")
        assert client.patch(f"/v1/bots/{bot['id']}", json={"model": "gpt-5"}, headers=H).status_code == 200
        assert client.patch(f"/v1/bots/{bot['id']}", json={"default_provider": "opencode", "model": "gpt-5"},
                            headers=H).status_code == 400
        assert client.patch(f"/v1/bots/{bot['id']}", json={"default_provider": "opencode"}, headers=H).status_code == 200


def test_model_error_opencode_shape():
    from botoffice.catalog import model_error
    for ok in ("a/b", "openrouter/anthropic/claude-x", "opencode/big-pickle"):
        assert model_error("opencode", ok) is None
    for bad in ("abc", "x/", "/x", "a//b", "a/b/", "-a/b", "a/ b", "a\tb/c"):
        assert model_error("opencode", bad)
    assert model_error("codex", "gpt-5") is None and model_error("opencode", None) is None
