import io
import json
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import baseline, desk, say, store as store_module, workspace
from botoffice.api import create_app
from botoffice.providers.base import RunResult
from botoffice.providers.fake import FakeAdapter
from botoffice.store import EditError

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def setup(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        store = app.state.store
        bot = store.create_bot("Ada", "", "codex", None)
        job = store.create("work", "codex", bot_id=bot.id, source="chat")
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
        token = store.issue_agent_token(job)
        yield client, store, bot, job, {"Authorization": "Bearer " + token}


def test_api_notes_limits_and_scope(setup):
    client, store, bot, job, auth = setup
    post = lambda value, headers=auth: client.post("/v1/agent/say", headers=headers, json={"text": value})
    assert post(" ").status_code == 400
    assert post("x" * 501).status_code == 400
    assert post("x", H).status_code == 401
    result = post("  milestone\nnext  ")
    assert result.status_code == 201, result.text
    assert result.json()["kind"] == "say" and result.json()["job_id"] == str(job.id)
    assert result.json()["text"] == "milestone\nnext"
    messages = client.get(f"/v1/bots/{bot.id}/messages?pending=true", headers=H).json()
    assert any(m["kind"] == "say" for m in messages)
    assert any(m["pending"] and m["job_id"] == str(job.id) for m in messages)
    assert "try again" in post("again").json()["detail"]
    assert post("again").status_code == 429
    other = store.create_bot("Other", "", "codex", None)
    other_job = store.create("other work", "codex", bot_id=other.id, source="chat")
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(other_job.id),))
    other_auth = {"Authorization": "Bearer " + store.issue_agent_token(other_job)}
    spoof = client.post("/v1/agent/say", headers=other_auth,
                        json={"text": "other note", "job_id": str(job.id), "bot_id": str(bot.id)})
    assert spoof.status_code == 201
    assert spoof.json()["job_id"] == str(other_job.id) and spoof.json()["bot_id"] == str(other.id)
    with pytest.raises(EditError) as exc:
        store.say(other.id, job.id, "impersonate")
    assert exc.value.status == 409
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (str(job.id),))
    with pytest.raises(EditError) as exc:
        store.say(bot.id, job.id, "late")
    assert exc.value.status == 409
    assert post("late").status_code == 401  # agent_scope rejects expired tokens


def test_count_limit_is_atomic(setup, monkeypatch):
    client, store, bot, job, auth = setup
    monkeypatch.setattr(store_module, "SAY_INTERVAL_SECONDS", 0)
    for i in range(9):
        store.say(bot.id, job.id, str(i))
    barrier = threading.Barrier(2)
    def attempt(_):
        barrier.wait()
        try:
            store.say(bot.id, job.id, "race")
            return 201
        except EditError as exc:
            assert "new turn" in exc.detail
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [201, 429]
    assert len([m for m in store.list_messages(bot.id) if m.kind == "say"]) == 10
    response = client.post("/v1/agent/say", headers=auth, json={"text": "one more"})
    assert response.status_code == 429 and "new turn" in response.json()["detail"]
    assert store.list_messages(bot.id, include_progress=False) == []
    assert store.rewind_seed(bot.id, 999999).messages == []


def test_cli(monkeypatch, capsys):
    for k, v in {"BOT_OFFICE_AGENT_TOKEN": "token", "BOT_OFFICE_API_URL": "http://example", "BOT_OFFICE_BOT_ID": "bot"}.items():
        monkeypatch.setenv(k, v)
    seen = []
    def success(request, timeout):
        seen.append((json.loads(request.data), timeout, request.full_url))
        return io.BytesIO(b'{"id": 42}')
    monkeypatch.setattr(say.urllib.request, "urlopen", success)
    assert desk.main(["say", "hello"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin note\n"))
    assert say.main(["-"]) == 0
    assert [x[0]["text"] for x in seen] == ["hello", "stdin note\n"]
    assert all(x[1] <= 10 and x[2].endswith("/v1/agent/say") for x in seen)
    assert "42" in capsys.readouterr().out
    def error(*args, **kwargs):
        raise urllib.error.HTTPError("http://example", 429, "limited", {}, io.BytesIO(b'{"detail":"try later"}'))
    monkeypatch.setattr(say.urllib.request, "urlopen", error)
    assert say.main(["hello"]) != 0
    assert "try later" in capsys.readouterr().err


class SayingAdapter(FakeAdapter):
    store = None
    async def run(self, job, workspace, log_path, timeout):
        self.store.say(job.bot_id, job.id, "milestone")
        (workspace / "reply.txt").write_text("All done\nBOT-OFFICE-STATUS: not_done — follow up")
        log_path.write_text("ok")
        return RunResult("succeeded", exit_code=0)


def test_scheduler_final_and_rewind(settings):
    adapter = SayingAdapter()
    app = create_app(settings, {"codex": adapter})
    adapter.store = app.state.store
    baseline.set_auto_continue(app.state.store, enabled=False)  # 0.11.10: one turn only here
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "Ada"}).json()
        msg = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={"text": "work"}).json()["message"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not any(m.kind == "message" and m.role == "assistant" for m in app.state.store.list_messages(bot["id"])):
            time.sleep(.02)
        messages = client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json()
        notes = [m for m in messages if m["kind"] == "say"]
        final = [m for m in messages if m["role"] == "assistant" and m["kind"] == "message"]
        assert len(notes) == len(final) == 1
        assert notes[0]["job_id"] == final[0]["job_id"] == msg["job_id"]
        assert final[0]["text"] == "All done"
        assert app.state.store.get(msg["job_id"]).outcome == "not_done: follow up"
        response = client.post(f"/v1/bots/{bot['id']}/messages/{msg['id']}/edit", headers=H, json={"text": "changed"})
        assert response.status_code == 201, response.text
        archived = client.get(f"/v1/bots/{bot['id']}/messages?archived=true", headers=H).json()
        assert next(m for m in archived if m["id"] == notes[0]["id"])["archived_at"]


def test_prompt_instructions(setup):
    _, _, bot, job, _ = setup
    assert 'desk say' in workspace.build_self(bot, job)
    assert '10 per turn' in workspace.RULES
    assert 'do not narrate your steps' in workspace._reply_rule(job)


def test_say_rows_are_never_a_jobs_reply(setup):
    _, store, bot, job, _ = setup
    from botoffice.domain import Message
    note = store.say(bot.id, job.id, "note")
    steer = Message(id=0, bot_id=bot.id, role="user", text="more", steer_job_id=job.id, created_at=note.created_at)
    assert store.steered_reply_id(steer) is None  # only a progress note exists: no reply yet
    final = store.publish_outcome(bot.id, job.id, "assistant", "final", source="chat")
    assert store.steered_reply_id(steer) == final.id
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded', outcome='unverified' WHERE id=?", (str(job.id),))
    flags = {m.kind: m.unverified for m in store.list_messages(bot.id) if m.role == "assistant"}
    assert flags == {"say": False, "message": True}
