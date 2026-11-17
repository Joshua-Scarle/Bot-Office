"""One continuous provider session per bot, small prompts on resume, bounded WORKING.md."""
import asyncio
import json
from datetime import datetime
from uuid import uuid4

import pytest

from botoffice.compaction import WORKING_LIMIT, cap_working
from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import CursorAdapter
from botoffice.providers.streams import session_id_from

SID = "01a1dac2-d19d-7170-89ee-9ee7e0b1454f"


def _job(**extra):
    base = dict(id=uuid4(), prompt="FULL PROMPT", resume_prompt="SHORT PROMPT", status=JobStatus.RUNNING,
                created_at=datetime.now(), slot=Slot.DESKTOP_1, bot_id=uuid4())
    base.update(extra)
    return Job(**base)


def _workspace(tmp_path):
    ws = tmp_path / "data" / "workspaces" / "job"
    ws.mkdir(parents=True)
    return ws


def test_session_ids_from_each_stream():
    assert session_id_from({"type": "thread.started", "thread_id": SID}) == SID
    assert session_id_from({"type": "system", "session_id": SID}) == SID
    assert session_id_from({"event": "result", "result": {"conversation_id": SID}}) == SID
    assert session_id_from({"event": "result", "result": {"conversation_id": ""}}) is None
    assert session_id_from({"session_id": "../../etc"}) is None


def test_resume_argv(tmp_path):
    ws = _workspace(tmp_path)
    job = _job(attachment_paths=["/x/a.png"])
    codex = CodexAdapter(executable="codex")._exec_args(ws, job, SID)
    assert codex[:2] == ["exec", "resume"] and codex[-2:] == [SID, "-"]
    assert "--dangerously-bypass-approvals-and-sandbox" in codex and "--image=/x/a.png" in codex
    assert "-m" in codex and "model_reasoning_effort=high" in codex
    assert any(a.startswith("compact_prompt=") for a in codex)
    assert any(a.startswith("model_auto_compact_token_limit=") for a in codex)
    claude = ClaudeAdapter(executable="claude")._exec_args(ws, job, SID)
    assert claude[claude.index("--resume") + 1] == SID
    settings = json.loads(claude[claude.index("--settings") + 1])
    assert "PreCompact" in settings["hooks"] and "--autocompact" in claude
    agy = AntigravityAdapter(executable="agy")._exec_args(ws, job, SID)
    assert agy[agy.index("--conversation") + 1] == SID
    cursor = CursorAdapter(executable="cursor-agent")._exec_args(ws, job, SID)
    assert cursor[cursor.index("--resume") + 1] == SID
    assert cursor[cursor.index("--workspace") + 1].endswith("/cursor-workspace")
    assert "prompt-resume.txt" in cursor[-1]


def test_restricted_codex_and_cursor_never_resume(tmp_path):
    ws = _workspace(tmp_path)
    job = _job(cli_access="restricted")
    for adapter in (CodexAdapter(executable="codex"), CursorAdapter(executable="cursor-agent")):
        adapter._save_session(ws, job, SID)
        assert adapter._saved_session(ws, job) is None


class Proc:
    def __init__(self, lines, code):
        self.lines, self.returncode, self.pid = list(lines), code, 0
        self.stdin = None
        self.stdout = self
        self.stderr = Err()

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""

    async def wait(self):
        return self.returncode


class Err:
    async def read(self, n):
        return b""


def _fake(monkeypatch, plan, calls):
    async def fake_exec(*args, **kwargs):
        if args[1:3] == ("login", "status"):
            p = Proc([], 0)
            async def communicate():
                return b"Logged in with ChatGPT", b""
            p.communicate = communicate
            return p
        calls.append(args)
        lines, code = plan.pop(0)
        return Proc(lines, code)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_codex_saves_then_resumes_same_session_with_short_prompt(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    adapter = CodexAdapter(executable="codex")
    bot = uuid4()
    started = json.dumps({"type": "thread.started", "thread_id": SID}).encode() + b"\n"
    calls = []
    _fake(monkeypatch, [([started], 0), ([started], 0)], calls)
    first = _job(bot_id=bot)
    assert asyncio.run(adapter.run(first, ws, tmp_path / "1.log", 5)).status == "succeeded"
    assert (tmp_path / "data" / "bots" / str(bot) / "codex-session").read_text() == SID
    assert adapter._stdin(first) == b"FULL PROMPT" and "resume" not in calls[0]
    second = _job(bot_id=bot)
    assert asyncio.run(adapter.run(second, ws, tmp_path / "2.log", 5)).status == "succeeded"
    assert calls[1][1:3] == ("exec", "resume") and SID in calls[1]


def test_failed_resume_falls_back_to_fresh_session(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    adapter = CodexAdapter(executable="codex")
    job = _job()
    adapter._save_session(ws, job, SID)
    new = "11111111-2222-3333-4444-555555555555"
    calls = []
    _fake(monkeypatch, [([], 1), ([json.dumps({"type": "thread.started", "thread_id": new}).encode() + b"\n"], 0)], calls)
    assert asyncio.run(adapter.run(job, ws, tmp_path / "j.log", 5)).status == "succeeded"
    assert calls[0][1:3] == ("exec", "resume") and "resume" not in calls[1]
    assert adapter._saved_session(ws, job) == new
    assert "starting a fresh session" in (tmp_path / "j.log").read_text()
    # 0.9.0: the failed pointer is archived for audit, not deleted
    bot_dir = ws.parent.parent / "bots" / str(job.bot_id)
    assert (bot_dir / "codex-session.resume-failed").read_text() == SID


def _turn_failed(message):
    return [json.dumps({"type": "turn.failed", "error": {"message": message}}).encode() + b"\n"]


def test_resume_hitting_a_usage_limit_keeps_the_session(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    adapter = CodexAdapter(executable="codex")
    job = _job()
    adapter._save_session(ws, job, SID)
    calls = []
    _fake(monkeypatch, [(_turn_failed("You've hit your usage limit. Try again in 2 hours."), 1)], calls)
    result = asyncio.run(adapter.run(job, ws, tmp_path / "j.log", 5))
    assert result.status == "failed" and result.failure_kind == "usage_limit" and len(calls) == 1
    assert adapter._saved_session(ws, job) == SID


def test_transient_resume_failure_retries_the_same_session_once(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    adapter = CodexAdapter(executable="codex")
    adapter.resume_retry_delay = 0
    job = _job()
    adapter._save_session(ws, job, SID)
    calls = []
    ok = [json.dumps({"type": "thread.started", "thread_id": SID}).encode() + b"\n"]
    _fake(monkeypatch, [(_turn_failed("stream disconnected: connection reset"), 1), (ok, 0)], calls)
    assert asyncio.run(adapter.run(job, ws, tmp_path / "j.log", 5)).status == "succeeded"
    assert [call[1:3] for call in calls] == [("exec", "resume"), ("exec", "resume")]
    assert adapter._saved_session(ws, job) == SID
    assert "retrying it once" in (tmp_path / "j.log").read_text()


def test_reset_session_archives_pointer(tmp_path):
    ws = _workspace(tmp_path)
    adapter = ClaudeAdapter(executable="claude")
    job = _job()
    adapter._save_session(ws, job, SID)
    bot_dir = ws.parent.parent / "bots" / str(job.bot_id)
    assert adapter.reset_session(bot_dir) is True
    assert adapter._saved_session(ws, job) is None and adapter.reset_session(bot_dir) is False


def test_working_md_is_capped():
    assert cap_working("x" * 10) == "x" * 10
    capped = cap_working("y" * (WORKING_LIMIT * 2))
    assert len(capped) == WORKING_LIMIT and "truncated" in capped


def test_prompts_reply_rule_and_resume_prompt_is_small():
    from botoffice.workspace import build_prompt, build_resume_prompt
    from types import SimpleNamespace
    bot = SimpleNamespace(name="B")
    codex = _job(provider="codex", prompt="hi", source="chat")
    agy = _job(provider="antigravity", prompt="hi", source="chat")
    assert "Do not write reply.txt" in build_prompt(bot, codex, "W", "R")
    assert "Do not write reply.txt" in build_prompt(bot, agy)  # 0.10.5: Antigravity replies with its final message too
    short = build_resume_prompt(codex)
    assert len(short) < 700 and "history.md" not in short and short.rstrip().endswith("hi")


def test_repeated_transient_resume_failure_keeps_the_session_and_fails_as_transient(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    adapter = CodexAdapter(executable="codex")
    adapter.resume_retry_delay = 0
    job = _job()
    adapter._save_session(ws, job, SID)
    calls = []
    outage = "stream disconnected: connection reset"
    _fake(monkeypatch, [(_turn_failed(outage), 1), (_turn_failed(outage), 1)], calls)
    result = asyncio.run(adapter.run(job, ws, tmp_path / "j.log", 5))
    assert result.status == "failed" and result.failure_kind == "provider_error" and len(calls) == 2
    assert adapter._saved_session(ws, job) == SID  # not archived; the scheduler retries later
    bot_dir = ws.parent.parent / "bots" / str(job.bot_id)
    assert not (bot_dir / "codex-session.resume-failed").exists()
    assert "keeping the session" in (tmp_path / "j.log").read_text()
