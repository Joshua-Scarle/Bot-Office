"""Question cards: authentication, validation, delivery and safe rendering."""
import io
import json
import runpy
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import ask, desk, needs_you, questions
from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.domain import JobStatus
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}
PAYLOAD = {"prompt": "Which?", "options": [{"label": "One", "value": "1"}, {"label": "Two"}]}


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


def create(client, auth, payload=PAYLOAD, **extra):
    return client.post("/v1/agent/ask", headers=auth, json={**payload, **extra})


def respond(client, bot, item, body):
    return client.post(f"/v1/bots/{bot.id}/questions/{item['id']}/answer", headers=H, json=body)


def test_auth_validation_replacement_and_unread(setup):
    client, store, bot, _job, agent = setup
    assert create(client, H).status_code == 401
    assert client.post("/v1/agent/ask", headers=agent, content="{").status_code == 400
    for bad in ({"prompt": "", "options": PAYLOAD["options"]},
                {"steps": [PAYLOAD] * 5}, {"prompt": "x", "options": [{"label": "x"}] * 9}):
        assert create(client, agent, bad).status_code == 400
    first = create(client, agent).json()
    second = create(client, agent).json()
    assert questions.get(store, first["id"])["status"] == "skipped"
    assert questions.get(store, second["id"])["status"] == "open"
    messages = store.list_messages(bot.id)
    assert [m.question["status"] for m in messages if m.question] == ["skipped", "open"]
    assert store.bot_unread(bot.id).unread_count == 2
    response = client.get(f"/v1/agent/questions/{second['id']}", headers=agent)
    assert response.json()["status"] == "open"


def test_answer_validation_and_running_steer(setup):
    client, store, bot, job, agent = setup
    item = create(client, agent, blocking=False).json()
    assert respond(client, bot, item, {"selected": ["unknown"]}).status_code == 400
    assert respond(client, bot, item, {"selected": ["1", "Two"]}).status_code == 400
    assert respond(client, bot, item, {"selected": ["One"]}).status_code == 200
    assert respond(client, bot, item, {"skip": True}).status_code == 409
    message = store.list_messages(bot.id)[-1]
    assert message.steer_job_id == job.id and message.steer_status == "pending"
    assert "1" in message.text


def test_multi_custom_skip_and_finished_job(setup):
    client, store, bot, job, agent = setup
    steps = [{**PAYLOAD, "multiSelect": True}, {**PAYLOAD, "allowCustom": True}]
    item = create(client, agent, {"steps": steps}, blocking=False).json()
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (str(job.id),))
    response = respond(client, bot, item, {"steps": [{"selected": ["1", "Two"]},
                                                      {"custom": "my answer"}]})
    assert response.status_code == 200
    message = next(m for m in reversed(store.list_messages(bot.id))
                   if m.role == "user" and "my answer" in m.text)
    assert message.steer_status is None
    assert store.get(message.job_id).status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED}
    skipped = questions.create(store, bot.id, None, PAYLOAD, blocking=True)
    assert respond(client, bot, skipped, {"skip": True}).json()["status"] == "skipped"


def test_blocking_no_steer_and_assignment_guard(setup):
    client, store, bot, job, agent = setup
    book = Assignments(store)
    assignment = book.create(bot.id, {"title": "Report"})
    with closing(store._connect()) as db:
        db.execute("UPDATE assignments SET status='active' WHERE id=?", (str(assignment.id),))
        db.execute("UPDATE jobs SET assignment_id=? WHERE id=?", (str(assignment.id), str(job.id)))
    item = create(client, agent).json()
    assert book.get(assignment.id).status == "waiting_on_user"
    assert any(x["kind"] == "question" for x in needs_you.needs_you(store))
    assert respond(client, bot, item, {"custom": "something"}).status_code == 200
    assert book.get(assignment.id).status == "active"
    assert not any(m.steer_status for m in store.list_messages(bot.id))
    again = create(client, agent).json()
    with closing(store._connect()) as db:
        db.execute("UPDATE assignments SET status='blocked',status_note='changed' WHERE id=?",
                   (str(assignment.id),))
    respond(client, bot, again, {"skip": True})
    assert book.get(assignment.id).status == "blocked"
    assert not any(x["kind"] == "question" for x in needs_you.needs_you(store))


def test_idle_waiting_and_question_fence(setup, settings, tmp_path):
    client, store, bot, job, agent = setup
    item = create(client, agent).json()
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (str(job.id),))
    bots = client.get("/v1/bots", headers=H).json()
    assert next(b for b in bots if b["id"] == str(bot.id))["status"] == "waiting"
    assert respond(client, bot, item, {"skip": True}).status_code == 200
    bots = client.get("/v1/bots", headers=H).json()
    assert next(b for b in bots if b["id"] == str(bot.id))["status"] != "waiting"
    text = "Hello\n```ask\n" + json.dumps(PAYLOAD) + "\n```"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = workspace / "job.log"
    log.write_text("")
    client.app.state.scheduler._record_outcome(
        job, workspace, JobStatus.SUCCEEDED, log, None, text
    )
    messages = store.list_messages(bot.id)
    assert any(m.text == "Hello" for m in messages)
    assert messages[-1].question["payload"]["steps"][0]["prompt"] == "Which?"
    assert all("```ask" not in m.text for m in messages)


def test_helper_no_wait_and_wait(setup, monkeypatch, capsys):
    client, store, bot, _job, agent = setup
    monkeypatch.setenv("BOT_OFFICE_AGENT_TOKEN", agent["Authorization"].split()[1])
    monkeypatch.setenv("BOT_OFFICE_API_URL", "http://testserver")
    monkeypatch.setenv("BOT_OFFICE_BOT_ID", str(bot.id))
    class Response(io.BytesIO):
        pass
    def urlopen(request, timeout):
        response = client.request(request.get_method(), request.full_url,
                                  headers=dict(request.header_items()),
                                  content=request.data)
        assert response.status_code in (200, 201), response.text
        return Response(response.content)
    monkeypatch.setattr(ask.urllib.request, "urlopen", urlopen)
    assert desk.main(["ask", "--no-wait", json.dumps(PAYLOAD)]) == 0
    assert json.loads(capsys.readouterr().out)["id"]
    def reply():
        for _ in range(100):
            cards = [m.question for m in store.list_messages(bot.id)
                     if m.question and m.question["blocking"]]
            if cards:
                respond(client, bot, cards[-1], {"selected": ["1"]})
                return
            time.sleep(.01)
        pytest.fail("helper did not create a blocking card")
    thread = threading.Thread(target=reply)
    thread.start()
    assert ask.main([json.dumps(PAYLOAD)]) == 0
    thread.join()
    result = json.loads(capsys.readouterr().out)
    assert result["answer"]["steps"][0]["selected"] == ["1"]
    assert not result["skipped"]
    assert not any(m.steer_status for m in store.list_messages(bot.id))
    monkeypatch.setattr(sys, "argv", ["bot-office-ask", "--no-wait", json.dumps(PAYLOAD)])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path("bin/bot-office-ask", run_name="__main__")
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out)["id"]

    def skip():
        for _ in range(100):
            cards = [m.question for m in store.list_messages(bot.id)
                     if m.question and m.question["status"] == "open" and m.question["blocking"]]
            if cards:
                respond(client, bot, cards[-1], {"skip": True})
                return
            time.sleep(.01)
        pytest.fail("helper did not create a blocking card")

    thread = threading.Thread(target=skip)
    thread.start()
    assert ask.main([json.dumps(PAYLOAD)]) == 0
    thread.join()
    assert json.loads(capsys.readouterr().out)["skipped"] is True


def test_gui_safe_and_claude_flag(tmp_path):
    html = Path("src/botoffice/static/bot-office.html").read_text()
    renderer = html.split("function renderQuestion(", 1)[1]
    renderer = renderer.split("async function loadMessages", 1)[0]
    assert "innerHTML" not in renderer and "insertAdjacentHTML" not in renderer
    assert 'el("div", "question-prompt", step.prompt)' in renderer
    assert "isComposing" in renderer
    assert "keyCode !== 229" in renderer
    args = ClaudeAdapter()._exec_args(tmp_path)
    assert args[args.index("--disallowedTools") + 1] == "AskUserQuestion"


def test_finish_reads_reply_file_and_strips_fence(setup, tmp_path):
    client, store, bot, job, _ = setup
    workspace = tmp_path / "reply-workspace"
    workspace.mkdir()
    (workspace / "reply.txt").write_text("```ask\n" + json.dumps(PAYLOAD) + "\n```")
    log = workspace / "job.log"
    log.write_text("")
    client.app.state.scheduler._finish(job, workspace, JobStatus.SUCCEEDED, log_path=log)
    messages = store.list_messages(bot.id)
    assert any(m.question and m.question["status"] == "open" for m in messages)
    assert any(m.text == "Asked a question." for m in messages)
    assert all("```ask" not in m.text for m in messages)


def test_custom_disabled_and_helper_missing_token(setup, monkeypatch, capsys):
    client, _, bot, _, agent = setup
    item = create(client, agent, {**PAYLOAD, "allowCustom": False}).json()
    assert respond(client, bot, item, {"custom": "no"}).status_code == 400
    assert respond(client, bot, item, {}).status_code == 400
    assert respond(client, bot, item, {"skip": True}).status_code == 200
    monkeypatch.delenv("BOT_OFFICE_AGENT_TOKEN", raising=False)
    assert desk.main(["ask", json.dumps(PAYLOAD)]) == 1
    assert "BOT_OFFICE_AGENT_TOKEN" in capsys.readouterr().err


def test_blocking_answer_after_job_finished_becomes_user_message(setup):
    client, store, bot, job, agent = setup
    item = create(client, agent).json()
    # the asking message does not carry the running job's id
    assert next(m for m in store.list_messages(bot.id) if m.question).job_id is None
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (str(job.id),))
    assert respond(client, bot, item, {"custom": "late answer"}).status_code == 200
    message = store.list_messages(bot.id)[-1]
    assert message.role == "user" and "late answer" in message.text


def test_edit_skips_open_question(setup):
    client, store, bot, _job, _agent = setup
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE bot_id=?", (str(bot.id),))
    user = client.post(f"/v1/bots/{bot.id}/messages", headers=H, json={"text": "one"}).json()["message"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE bot_id=?", (str(bot.id),))
    item = questions.create(store, bot.id, None, PAYLOAD)
    assert needs_you.needs_you(store)
    store.edit_message(bot, user["id"], "changed", None, [])
    assert questions.get(store, item["id"])["status"] == "skipped"
    assert not any(x["kind"] == "question" for x in needs_you.needs_you(store))


def test_only_last_ask_fence_makes_a_card(setup, tmp_path):
    client, store, bot, job, _agent = setup
    other = {**PAYLOAD, "prompt": "Second?"}
    text = "\n".join("```ask\n" + json.dumps(p) + "\n```" for p in (PAYLOAD, other))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = workspace / "job.log"
    log.write_text("")
    client.app.state.scheduler._record_outcome(job, workspace, JobStatus.SUCCEEDED, log, None, text)
    cards = [m.question for m in store.list_messages(bot.id) if m.question]
    assert len(cards) == 1 and cards[0]["status"] == "open"
    assert cards[0]["payload"]["steps"][0]["prompt"] == "Second?"
