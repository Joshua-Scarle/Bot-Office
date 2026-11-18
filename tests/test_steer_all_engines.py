"""0.11.7: a message sent while ANY engine works steers the running turn (same job), never the next one.

Claude: live stdin injection (tests/test_steering.py). Codex, Cursor and Antigravity with full access:
interrupt promptly and resume the same session (test_steering.py, plus the timing tests here). Restricted
access and OpenCode (which cannot resume): interrupt promptly and run the same job again with the same
flags, the job's prompt, a note of what the interrupted run had done and the message ("restart").
Fake CLIs only: no real engine was run."""
import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient
from test_steering import fake_script, wait_for

from botoffice.api import create_app
from botoffice.domain import JobStatus
from botoffice.providers import cli as cli_module
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.cli import RESTART_PROMPT_LIMIT, _restart_suffix
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import PROMPT_POINTER, CursorAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.providers.streams import parser_for

H = {"Authorization": "Bearer test-token"}

ADAPTERS = {"codex": CodexAdapter, "cursor": CursorAdapter, "antigravity": AntigravityAdapter,
            "opencode": OpenCodeAdapter}

# One fake for every engine. Invocation 1..HANG-1 print a few events, say they are ready and wait to be
# interrupted (SIGINT leaves a marker); the last invocation answers. Everything it was started with is
# written next to it: argv<N>.json and (Codex) stdin<N>.
FAKE = '''
import json, os, signal, sys, time
from pathlib import Path
ENGINE, HANG = {engine!r}, {hang}
argv = sys.argv[1:]
if (ENGINE == "codex" and argv[:2] == ["login", "status"]) or (ENGINE == "cursor" and argv[:1] == ["status"]) \\
        or (ENGINE == "antigravity" and argv[:1] == ["models"]):
    print("Logged in"); sys.exit(0)
base = Path(os.environ.get("BOT_OFFICE_WORKSPACE", "."))
runs = base / "runs"
n = int(runs.read_text()) + 1 if runs.exists() else 1
runs.write_text(str(n))
(base / f"argv{{n}}.json").write_text(json.dumps(argv))
if ENGINE == "codex":
    (base / f"stdin{{n}}").write_text(sys.stdin.read())
sid = f"session_{{n}}23456"
def out(obj):
    print(json.dumps(obj), flush=True)
if n >= HANG:
    if ENGINE == "codex":
        out({{"type": "item.completed", "item": {{"type": "agent_message", "text": "restarted reply"}}}})
        out({{"type": "turn.completed"}})
    elif ENGINE == "cursor":
        out({{"type": "result", "session_id": sid, "result": "restarted reply"}})
    elif ENGINE == "antigravity":
        out({{"event": "result", "result": {{"status": "SUCCESS", "conversation_id": sid, "response": "restarted reply"}}}})
    else:
        out({{"type": "text", "sessionID": sid, "part": {{"text": "restarted reply"}}}})
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *a: ((base / "interrupted").touch(), sys.exit(130)))
if ENGINE == "codex":
    out({{"type": "thread.started", "thread_id": sid}})
    out({{"type": "item.started", "item": {{"id": "i1", "type": "command_execution", "command": f"make step{{n}}"}}}})
    out({{"type": "item.completed", "item": {{"id": "i1", "type": "command_execution"}}}})
elif ENGINE == "cursor":
    out({{"type": "system", "session_id": sid}})
    out({{"type": "tool_call", "subtype": "started", "call_id": "c1", "tool_call": {{"shellToolCall": {{"args": {{"command": f"make step{{n}}"}}}}}}}})
    out({{"type": "tool_call", "subtype": "completed", "call_id": "c1"}})
elif ENGINE == "antigravity":
    out({{"event": "init", "conversation_id": sid, "init": {{"model": "m", "cwd": str(Path.cwd())}}}})
    step = {{"step_type": "tool", "tool_name": "run_command", "step_id": "s1", "tool_info": {{"command": f"make step{{n}}"}}}}
    out({{"event": "step_update", "step_update": dict(step, state="RUNNING")}})
    out({{"event": "step_update", "step_update": dict(step, state="DONE")}})
else:
    out({{"type": "step_start", "sessionID": sid}})
    out({{"type": "text", "sessionID": sid, "part": {{"text": f"looking at part {{n}}"}}}})
    out({{"type": "tool_use", "sessionID": sid, "part": {{"tool": "bash", "state": {{"input": {{"command": f"make step{{n}}"}}}}}}}})
(base / f"ready{{n}}").touch()
time.sleep(30)
'''


def start(settings, tmp_path, engine, *, hang=2, timeout=12):
    settings = dataclasses.replace(settings, job_timeout=timeout)
    exe = fake_script(tmp_path, "fake_" + engine, FAKE.format(engine=engine, hang=hang))
    app = create_app(settings, {engine: ADAPTERS[engine](executable=exe)})
    return app, settings


def new_bot(client, engine, restricted):
    body = {"name": engine.title(), "default_provider": engine}
    if restricted:
        body["cli_access"] = "restricted"
    return client.post("/v1/bots", headers=H, json=body).json()


def workspace_of(settings, job):
    # Antigravity runs in a stable directory and Cursor in its own; BOT_OFFICE_WORKSPACE is the job dir
    return settings.data_dir / "workspaces" / job["id"]


def files(ws, n):
    return json.loads((ws / f"argv{n}.json").read_text()), (ws / f"stdin{n}")


@pytest.mark.parametrize("engine,restricted", [
    ("opencode", False), ("opencode", True), ("codex", True), ("cursor", True), ("antigravity", True)])
def test_message_restarts_the_running_turn_with_what_it_had_done(settings, tmp_path, engine, restricted):
    """The message reaches the same job within a couple of seconds: nothing is queued as a next turn."""
    app, settings = start(settings, tmp_path, engine)
    with TestClient(app) as client:
        bot = new_bot(client, engine, restricted)
        assert bot["steering"] == "restart"
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = workspace_of(settings, job)
        wait_for(lambda: (ws / "ready1").exists())
        sent_at = time.monotonic()
        posted = client.post(url, headers=H, json={"text": "change direction"}).json()
        assert posted["delivery"] == "steer" and posted["delivery_note"] is None
        assert posted["job"]["id"] == job["id"] and posted["message"]["steer_status"] == "pending"
        mid = posted["message"]["id"]
        wait_for(lambda: (ws / "interrupted").exists(), 5)
        assert time.monotonic() - sent_at < 3.5, "the interrupt must not wait for a step boundary of 15 s"
        wait_for(lambda: app.state.store.get(job["id"]).status == JobStatus.SUCCEEDED)
        assert app.state.store.get_message(mid).steer_status == "delivered"
        assert len(app.state.store.list()) == 1  # delivered into this turn, no second job
        assert app.state.store.get(job["id"]).reply_text == "restarted reply"
        argv2, stdin2 = files(ws, 2)
        # a restart, never a session resume (restricted access and OpenCode cannot)
        assert not {"resume", "--resume", "--conversation", "-s", "--session", "--continue"} & set(argv2)
        text = " ".join(argv2) + (stdin2.read_text() if engine == "codex" else "")
        assert "change direction" in text  # the message
        assert "make step1" in text  # what it had done
        assert "stopped part-way" in text
        if engine == "cursor":  # the prompt itself is the file in the workspace
            assert argv2[-1].startswith(PROMPT_POINTER) and (ws / "prompt.txt").exists()
        else:
            assert (ws / "prompt.txt").read_text() in text
        argv1, _ = files(ws, 1)
        # the same access flags as the interrupted invocation: restricted stays restricted
        full = {"codex": "--dangerously-bypass-approvals-and-sandbox", "opencode": "--auto", "cursor": "--force",
                "antigravity": "--dangerously-skip-permissions"}[engine]
        assert (full in argv1) == (full in argv2) == (not restricted)
        assert ("--approve-for-me" in argv2) == (engine == "codex" and restricted)
        assert any(e["kind"] == "steer" and e["status"] == "delivered" for e in app.state.store.list_events(job["id"]))


def test_second_steer_restarts_again_and_keeps_the_whole_story(settings, tmp_path):
    app, settings = start(settings, tmp_path, "opencode", hang=3)
    with TestClient(app) as client:
        bot = new_bot(client, "opencode", False)
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = workspace_of(settings, job)
        wait_for(lambda: (ws / "ready1").exists())
        a = client.post(url, headers=H, json={"text": "first steer"}).json()["message"]["id"]
        wait_for(lambda: (ws / "ready2").exists())
        b = client.post(url, headers=H, json={"text": "second steer"}).json()["message"]["id"]
        wait_for(lambda: app.state.store.get(job["id"]).status == JobStatus.SUCCEEDED)
        assert len(app.state.store.list()) == 1
        assert [app.state.store.get_message(i).steer_status for i in (a, b)] == ["delivered", "delivered"]
        third = " ".join(files(ws, 3)[0])
        for part in ("first steer", "second steer", "make step1", "make step2", "looking at part 1"):
            assert part in third
        assert third.index("make step1") < third.index("first steer") < third.index("make step2") < third.index("second steer")


def test_steers_sent_together_are_delivered_with_one_interrupt(settings, tmp_path):
    app, settings = start(settings, tmp_path, "opencode")
    with TestClient(app) as client:
        bot = new_bot(client, "opencode", False)
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = workspace_of(settings, job)
        wait_for(lambda: (ws / "ready1").exists())
        ids = [client.post(url, headers=H, json={"text": t}).json()["message"]["id"] for t in ("one", "two")]
        wait_for(lambda: app.state.store.get(job["id"]).status == JobStatus.SUCCEEDED)
        assert (ws / "runs").read_text() == "2"
        assert all(app.state.store.get_message(i).steer_status == "delivered" for i in ids)
        text = " ".join(files(ws, 2)[0])
        assert "one" in text and "two" in text


CODEX_IDLE = '''
import json, signal, sys, time
from pathlib import Path
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in"); sys.exit(0)
if sys.argv[1:3] == ["exec", "resume"]:
    Path("resumed").write_text(json.dumps(sys.argv))
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"resumed reply"}}), flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *a: sys.exit(1))
sys.stdin.read()
print(json.dumps({"type":"thread.started", "thread_id":"thread_123456"}), flush=True)
Path("ready").touch()
time.sleep(30)
'''


def test_codex_is_interrupted_at_once_when_no_command_is_running(settings, tmp_path):
    """0.11.6 waited up to 15 s for a step boundary; now an idle moment (thinking) is interrupted after 0.3 s."""
    settings = dataclasses.replace(settings, job_timeout=12)
    exe = fake_script(tmp_path, "fake_codex_idle", CODEX_IDLE)
    app = create_app(settings, {"codex": CodexAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = new_bot(client, "codex", False)
        assert bot["steering"] == "interrupt"
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = workspace_of(settings, job)
        wait_for(lambda: (ws / "ready").exists())
        sent_at = time.monotonic()
        mid = client.post(url, headers=H, json={"text": "now this"}).json()["message"]["id"]
        wait_for(lambda: (ws / "resumed").exists(), 5)
        assert time.monotonic() - sent_at < 3
        wait_for(lambda: app.state.store.get(job["id"]).status == JobStatus.SUCCEEDED)
        assert app.state.store.get_message(mid).steer_status == "delivered" and len(app.state.store.list()) == 1
        assert "thread_123456" in json.loads((ws / "resumed").read_text())


def test_a_running_command_is_given_time_to_finish_then_interrupted(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "STEER_BOUNDARY_TIMEOUT", 1.2)
    source = CODEX_IDLE.replace('Path("ready").touch()', '''print(json.dumps({"type":"item.started", "item":{"id":"i1", "type":"command_execution", "command":"sleep 99"}}), flush=True)
Path("ready").touch()''')
    settings = dataclasses.replace(settings, job_timeout=12)
    exe = fake_script(tmp_path, "fake_codex_busy", source)
    app = create_app(settings, {"codex": CodexAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = new_bot(client, "codex", False)
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = workspace_of(settings, job)
        wait_for(lambda: (ws / "ready").exists())
        sent_at = time.monotonic()
        client.post(url, headers=H, json={"text": "now this"})
        time.sleep(.7)
        assert not (ws / "resumed").exists()  # the command still runs
        wait_for(lambda: (ws / "resumed").exists(), 5)
        assert 1.0 < time.monotonic() - sent_at < 4
        wait_for(lambda: app.state.store.get(job["id"]).status == JobStatus.SUCCEEDED)


def test_engine_that_opts_out_still_queues_with_the_reason(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(OpenCodeAdapter, "restart_steering", False)
    app, settings = start(settings, tmp_path, "opencode")
    with TestClient(app) as client:
        bot = new_bot(client, "opencode", False)
        assert bot["steering"] == "queue"
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={"text": "start"}).json()["job"]
        ws = workspace_of(settings, job)
        wait_for(lambda: (ws / "ready1").exists())
        posted = client.post(url, headers=H, json={"text": "steer"}).json()
        assert posted["delivery"] == "queue" and posted["message"]["steer_status"] == "queued"
        assert "OpenCode" in posted["delivery_note"] and posted["job"]["id"] != job["id"]
        assert not (ws / "interrupted").exists()


def test_restart_note_is_bounded_and_keeps_the_newest_steps(tmp_path):
    history = [f"ran bash: step number {i} " + "x" * 200 for i in range(300)]
    base = "p" * (RESTART_PROMPT_LIMIT - 5000)
    suffix = _restart_suffix(history, [], tmp_path / "workspaces" / "job", base)
    assert len((base + suffix).encode()) < RESTART_PROMPT_LIMIT
    assert "step number 299" in suffix and "step number 0 " not in suffix and "earlier steps left out" in suffix
    assert "no longer pending" in suffix  # no message left (archived by a rewind): just carry on


def test_parsers_record_what_the_run_did():
    parser = parser_for("opencode")
    for obj in ({"type": "text", "sessionID": "s", "part": {"text": "checking the tests"}},
                {"type": "tool_use", "sessionID": "s", "part": {"tool": "bash", "state": {"input": {"command": "npm test"}}}},
                {"type": "text", "sessionID": "s", "part": {"text": "two fail"}}):
        for kind, data in parser.feed(obj):
            parser.record(kind, data)
    parser.flush_said()
    assert parser.progress == ["said: checking the tests", "ran bash: npm test", "said: two fail"]
