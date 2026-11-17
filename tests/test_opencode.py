import asyncio
import json
import time
from datetime import datetime
from uuid import uuid4

from botoffice import failures
from botoffice.domain import Job, JobStatus
from botoffice.providers.cli import CliAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.providers.streams import OpenCodeParser
from botoffice.store import JobStore
from botoffice.streaming import EventSink


def test_opencode_run_never_resumes(tmp_path):
    script = tmp_path / "opencode"
    script.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
Path("argv.json").write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"type":"step_start","sessionID":"ses_123"}))
print(json.dumps({"type":"text","sessionID":"ses_123","part":{"type":"text","text":"hello"}}))
print(json.dumps({"type":"step_finish","sessionID":"ses_123","part":{"reason":"stop"}}))
''')
    script.chmod(0o755)
    workspace = tmp_path / "workspaces" / "job"
    workspace.mkdir(parents=True)
    job = Job(id=uuid4(), bot_id=uuid4(), prompt="hi", status=JobStatus.RUNNING,
              created_at=datetime.now(), provider="opencode", model="openai/model", effort="high")
    adapter = OpenCodeAdapter(executable=str(script))
    result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 5))
    assert result.status == "succeeded" and result.final_text == "hello"
    args = json.loads((workspace / "argv.json").read_text())
    assert args == ["run", "--format", "json", "-m", "openai/model", "--dir", str(workspace), "--auto",
                    "--variant", "high", "--", "hi"]
    # OpenCode 1.19.5 `run -s` hangs: no session is saved, a legacy pointer is ignored, `-s` is never passed
    bot_dir = tmp_path / "bots" / str(job.bot_id)
    assert not (bot_dir / "opencode-session").exists()
    bot_dir.mkdir(parents=True)
    (bot_dir / "opencode-session").write_text("ses_legacy")
    job.resume_prompt = "short resume prompt"
    result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 5))
    assert result.status == "succeeded"
    again = json.loads((workspace / "argv.json").read_text())
    assert "-s" not in again and again[-2:] == ["--", "hi"]
    assert adapter._saved_session(workspace, job) is None


def test_opencode_error_and_missing_binary(tmp_path, monkeypatch):
    script = tmp_path / "opencode"
    script.write_text('''#!/usr/bin/env python3
import json
print(json.dumps({"type":"error","error":{"data":{"message":"API key missing"}}}))
''')
    script.chmod(0o755)
    job = Job(id=uuid4(), prompt="hi", status=JobStatus.RUNNING, created_at=datetime.now())
    result = asyncio.run(OpenCodeAdapter(executable=str(script)).run(job, tmp_path, tmp_path / "log", 5))
    assert result.status == "needs_login" and "opencode auth login" in result.error
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = asyncio.run(OpenCodeAdapter().run(job, tmp_path, tmp_path / "log", 5))
    assert result.status == "failed" and "not found" in result.error


def test_opencode_parser_ignores_unknown_and_reports_error():
    parser = OpenCodeParser()
    assert parser.feed({"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "stop"}}) == []
    assert parser.session_id == "ses_1"
    assert parser.feed({"type": "error", "error": {"data": {"message": "bad model"}}}) == [
        ("error", {"message": "bad model"})
    ]


SILENT = '''#!/usr/bin/env python3
import time
time.sleep(30)
'''


def _silent_job(tmp_path, script_text=SILENT):
    script = tmp_path / "opencode"
    script.write_text(script_text)
    script.chmod(0o755)
    workspace = tmp_path / "workspaces" / "job"
    workspace.mkdir(parents=True, exist_ok=True)
    job = Job(id=uuid4(), bot_id=uuid4(), prompt="hi", status=JobStatus.RUNNING,
              created_at=datetime.now(), provider="opencode", model="openai/model")
    return OpenCodeAdapter(executable=str(script)), job, workspace


def test_opencode_no_output_guard_kills_a_hung_run(tmp_path, monkeypatch):
    adapter, job, workspace = _silent_job(tmp_path)
    assert OpenCodeAdapter.idle_timeout == 60 and CliAdapter.idle_timeout is None
    monkeypatch.setattr(OpenCodeAdapter, "idle_timeout", 0.4)
    started = time.monotonic()
    result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 20))
    assert time.monotonic() - started < 10
    assert result.status == "failed" and result.error == "OpenCode produced no output for 0.4 s; stopped"
    assert result.failure_kind == failures.PROVIDER_ERROR  # retried by the normal rules


def test_opencode_guard_ends_at_the_first_output(tmp_path, monkeypatch):
    # prints once, then stays silent for longer than the limit (a long tool call): never killed
    once = '''#!/usr/bin/env python3
import json, time
print(json.dumps({"type": "text", "sessionID": "ses_1", "part": {"text": "x"}}), flush=True)
time.sleep(2.0)
'''
    adapter, job, workspace = _silent_job(tmp_path, once)
    monkeypatch.setattr(OpenCodeAdapter, "idle_timeout", 0.5)
    result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 20))
    assert result.status == "succeeded" and result.exit_code == 0

    # stderr alone also counts as output
    noisy = '''#!/usr/bin/env python3
import sys, time
print("starting", file=sys.stderr, flush=True)
time.sleep(2.0)
'''
    adapter, job, workspace = _silent_job(tmp_path, noisy)
    assert asyncio.run(adapter.run(job, workspace, tmp_path / "log", 20)).exit_code == 0

    # silent from the start: killed at about the limit, not at the job timeout
    adapter, job, workspace = _silent_job(tmp_path)
    started = time.monotonic()
    result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 20))
    assert 0.4 <= time.monotonic() - started < 5
    assert result.status == "failed" and "no output" in result.error


def test_opencode_guard_waits_for_an_open_question(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenCodeAdapter, "idle_timeout", 0.5)

    # a silent run with an open blocking question is left alone until the question is answered
    quiet = '''#!/usr/bin/env python3
import time
time.sleep(1.5)
print("{}")
'''
    adapter, job, workspace = _silent_job(tmp_path, quiet)
    store = JobStore(tmp_path / "db.sqlite3")
    open_question = {"v": True}
    store.has_open_question = lambda job_id: open_question["v"]
    sink = EventSink(store, job.id)
    token = sink.install()
    try:
        result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 20))
    finally:
        sink.uninstall(token)
    assert result.status == "succeeded"
    open_question["v"] = False
    adapter, job, workspace = _silent_job(tmp_path, quiet)
    sink = EventSink(store, job.id)
    token = sink.install()
    try:
        result = asyncio.run(adapter.run(job, workspace, tmp_path / "log", 20))
    finally:
        sink.uninstall(token)
    assert result.status == "failed" and "no output" in result.error


def test_store_has_open_question(tmp_path):
    from contextlib import closing
    store = JobStore(tmp_path / "db.sqlite3")
    job_id = uuid4()

    def add(status, blocking):
        with closing(store._connect()) as db:
            db.execute("INSERT INTO questions (id, bot_id, job_id, message_id, payload, status, blocking, created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (str(uuid4()), str(uuid4()), str(job_id), 1, "{}", status, blocking, "2026-11-14T00:00:00"))

    assert not store.has_open_question(job_id)
    add("answered", 1)
    add("open", 0)  # a non-blocking card does not hold the job
    assert not store.has_open_question(job_id)
    add("open", 1)
    assert store.has_open_question(job_id)


def _text(value):
    return {"type": "text", "part": {"type": "text", "text": value}}


def test_opencode_reply_is_only_the_last_text_part():
    parser = OpenCodeParser()
    events = [parser.feed(_text("I will read my context files first.")),
              parser.feed({"type": "tool_use", "part": {"tool": "read", "state": {"input": {"filePath": "a"}}}}),
              parser.feed(_text("ELM-21"))]
    assert parser.final_text == "ELM-21"
    assert events[0] == [("text", {"text": "I will read my context files first."})]  # progress still streams
    assert events[2] == [("text", {"text": "\n\nELM-21"})]


def test_opencode_single_text_part_and_several_keep_the_last():
    parser = OpenCodeParser()
    parser.feed(_text("hello"))
    assert parser.final_text == "hello"
    for value in ("one", "two", "three"):
        parser.feed(_text(value))
    assert parser.final_text == "three"


def test_opencode_error_after_text_still_reported():
    parser = OpenCodeParser()
    parser.feed(_text("partial"))
    parser.feed({"type": "error", "error": {"name": "Boom"}})
    assert parser.error == "Boom" and parser.error_text == "Boom"
