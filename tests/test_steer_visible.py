"""0.11.6: a message to a working bot steers it wherever the engine can take it, and when it cannot
(since 0.11.7 no built-in engine is in that case, see test_steer_all_engines) the fallback is stated at once (``delivery`` / ``delivery_note``, ``steer_note``), never a silent queue."""
import dataclasses

from fastapi.testclient import TestClient
from test_loop import setup as loop_setup
from test_steering import fake_script, wait_for

from botoffice.api import create_app
from botoffice.domain import JobStatus
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.opencode import OpenCodeAdapter

H = {"Authorization": "Bearer test-token"}


# --- the root cause: a running assignment turn refused every steer and queued a separate chat job ---


def test_message_to_a_bot_in_an_assignment_turn_steers_that_turn(settings):
    store, scheduler, _, bot, item = loop_setup(settings)
    turn = store.claim(["desktop-1"], {"codex"})
    assert turn.assignment_id == item.id
    used = scheduler.assignments.get(item.id).turns_used
    posted = store.post_steer(bot.id, "also check the totals", [])
    assert posted is not None, "a bot working on an assignment must still be steerable"
    message, job = posted
    assert str(job.id) == str(turn.id) and message.steer_status == "pending"
    assert len([j for j in store.list() if j.source == "chat"]) == 0  # no separate chat job
    assert scheduler.assignments.get(item.id).turns_used == used  # a steer is not a turn


def test_undelivered_steer_of_an_assignment_turn_still_runs_next_as_a_chat_turn(settings):
    store, scheduler, _, bot, _item = loop_setup(settings)
    turn = store.claim(["desktop-1"], {"codex"})
    message, _ = store.post_steer(bot.id, "also check the totals", [])
    store.finish(turn.id, JobStatus.SUCCEEDED)
    scheduler._queue_pending_steers(turn.id)
    converted = store.get(store.get_message(message.id).job_id)
    assert converted.status == JobStatus.QUEUED and converted.assignment_id is None and converted.source == "chat"
    assert store.get_message(message.id).steer_note  # says why it is a queued turn


# --- explicit delivery in the API response ---


def test_api_says_how_a_message_was_delivered(settings):
    app = create_app(settings, {"codex": FakeAdapter(delay=1.0)})
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "A"}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        first = client.post(url, headers=H, json={"text": "start"}).json()
        assert first["delivery"] == "start" and first["delivery_note"] is None
        wait_for(lambda: app.state.store.get(first["job"]["id"]).status == JobStatus.RUNNING)
        steered = client.post(url, headers=H, json={"text": "adjust"}).json()
        assert steered["delivery"] == "steer" and steered["message"]["steer_status"] == "pending"
        later = client.post(url, headers=H, json={"text": "after", "mode": "queue"}).json()
        assert later["delivery"] == "queue" and "next turn" in later["delivery_note"]


def test_message_to_a_bot_answering_another_bot_says_it_was_queued(settings):
    app = create_app(settings, {"codex": FakeAdapter(delay=1.0)})
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "A"}).json()
        inbox = client.post(f"/v1/bots/{bot['id']}/inbox", headers=H, json={"text": "ping"}).json()
        wait_for(lambda: app.state.store.get(inbox["job"]["id"]).status == JobStatus.RUNNING)
        posted = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={"text": "hello"}).json()
        assert posted["delivery"] == "queue"
        assert "another bot" in posted["delivery_note"]
        assert posted["job"]["id"] != inbox["job"]["id"]


def test_bots_report_how_they_can_be_steered(settings):
    apps = {"codex": CodexAdapter(executable="codex"), "claude": ClaudeAdapter(executable="claude"),
            "opencode": OpenCodeAdapter(executable="opencode")}
    app = create_app(settings, apps)
    with TestClient(app) as client:
        got = {}
        for engine in apps:
            body = {"name": engine, "default_provider": engine}
            if engine == "claude":
                body["reason"] = "tests"
            got[engine] = client.post("/v1/bots", headers=H, json=body).json()["steering"]
        assert got == {"codex": "interrupt", "claude": "live", "opencode": "restart"}
        restricted = client.post("/v1/bots", headers=H, json={"name": "r", "cli_access": "restricted"}).json()
        assert restricted["steering"] == "restart"  # 0.11.7: restricted access is steered by a restart, not queued


def test_claude_live_steer_response_is_a_steer(settings, tmp_path):
    settings = dataclasses.replace(settings, job_timeout=8)
    exe = fake_script(tmp_path, "fake_claude_live", '''
import json, sys, time
from pathlib import Path
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn":true}'); sys.exit(0)
json.loads(sys.stdin.readline())
print(json.dumps({"type":"system", "session_id":"session_123456"}), flush=True)
Path("ready").touch()
steer = json.loads(sys.stdin.readline())
print(json.dumps(steer), flush=True)
print(json.dumps({"type":"result", "result":"adjusted", "session_id":"session_123456"}), flush=True)
sys.stdin.read()
''')
    app = create_app(settings, {"claude": ClaudeAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "C", "default_provider": "claude", "reason": "t"}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = settings.data_dir / "workspaces" / job["id"]
        wait_for(lambda: (ws / "ready").exists())
        posted = client.post(url, headers=H, json={"text": "steer"}).json()
        assert posted["delivery"] == "steer" and posted["job"]["id"] == job["id"]
        mid = posted["message"]["id"]
        wait_for(lambda: app.state.store.get_message(mid).steer_status == "delivered")
        assert len(app.state.store.list()) == 1  # delivered into the running turn, no second job
