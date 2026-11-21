import asyncio
import builtins
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.cursor import PROMPT_POINTER, CursorAdapter

from fakes import StreamProc

CASES = [
    (ClaudeAdapter, "claude", ("auth", "status"), ".claude"),
    (CursorAdapter, "cursor-agent", ("status",), ".cursor"),
]


def _job(prompt="hello"):
    return Job(
        id=uuid4(), prompt=prompt, status=JobStatus.RUNNING, created_at=datetime.now(), slot=Slot.DESKTOP_2,
        cli_access="restricted",  # legacy limits; full access is covered in test_full_cli_access.py
    )


class FakeProcess:
    def __init__(self, result, returncode, inputs):
        self.result, self.returncode, self.inputs = result, returncode, inputs

    async def communicate(self, input=None):
        self.inputs.append(input)
        return self.result


@pytest.mark.parametrize("cls,exe,status_args,cred", CASES)
def test_needs_login_does_not_spawn_exec_or_read_credentials(monkeypatch, tmp_path, cls, exe, status_args, cred):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess((b"Not logged in", b""), 1, [])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    original_open = builtins.open
    original_path_open = Path.open

    def guard(path):
        if cred in str(path):
            raise AssertionError("adapter must not read credential files")

    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (guard(f), original_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (guard(self), original_path_open(self, *a, **k))[1])

    async def scenario():
        result = await cls(executable=exe).run(_job(), tmp_path, tmp_path / "job.log", 1)
        assert result.status == "needs_login"
        assert calls == [(exe, *status_args)]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "output,code,expected",
    [(b"Logged in as me", 0, "ok"), (b"Signed out", 1, "needs_login"), (b"???", 0, "error")],
)
def test_login_status_parsing(monkeypatch, output, code, expected):
    async def fake_exec(*args, **kwargs):
        return FakeProcess((output, b""), code, [])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(ClaudeAdapter(executable="claude").login_status()) == expected


def test_disabled_adapters_report_disabled():
    assert asyncio.run(ClaudeAdapter(enabled=False).login_status()) == "disabled"
    assert asyncio.run(CursorAdapter(enabled=False).login_status()) == "disabled"


def _run_exec(monkeypatch, tmp_path, adapter, exe):
    calls, inputs = [], []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        if args[1] in ("auth", "status", "models"):
            out = b"Logged in" if args[1] != "models" else b"gemini-3-flash\n"
            return StreamProc(out, b"", 0, inputs)
        return StreamProc(b"done\n", b"", 0, inputs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(adapter.run(_job("write report"), tmp_path, tmp_path / "job.log", 1))
    return result, calls, inputs


def _assert_env(env, tmp_path):
    git = {key for key in env if key.startswith("GIT_CONFIG_")}  # core.hooksPath for the pre-push check
    assert set(env) - git <= {
        "PATH", "LANG", "TERM", "HOME",
        "BOT_OFFICE_SLOT", "AGENTDESK_SLOT", "BOT_OFFICE_JOB_ID",
        "BOT_OFFICE_INBOX_DIR", "AGENTDESK_INBOX_DIR",
    }
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath" and env["GIT_CONFIG_VALUE_0"].endswith("/bin/git-hooks")
    env = {key: value for key, value in env.items() if key not in git}
    assert env["BOT_OFFICE_SLOT"] == env["AGENTDESK_SLOT"] == "desktop-2"
    assert env["BOT_OFFICE_INBOX_DIR"] == env["AGENTDESK_INBOX_DIR"]
    assert env["PATH"].split(":")[0].endswith("/bin") and "bin" in env["PATH"].split(":")[0]
    assert not any("KEY" in key or "TOKEN" in key for key in env)


def test_claude_exec_argv_stdin_and_env(monkeypatch, tmp_path):
    result, calls, inputs = _run_exec(monkeypatch, tmp_path, ClaudeAdapter(executable="claude"), "claude")
    assert result.status == "succeeded"
    args, kwargs = calls[1]
    assert args == (
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--replay-user-messages",
        "--permission-mode", "acceptEdits", "--disallowedTools", "AskUserQuestion", "--permission-prompts", "none", "--mcp-config", "{\"mcpServers\": {}}", "--strict-mcp-config", "--effort", "medium",
    )
    assert "--dangerously-skip-permissions" not in args and "--bare" not in args
    assert kwargs["cwd"] == tmp_path
    import json
    assert inputs[0] is None
    assert json.loads(inputs[1])["message"]["content"][0]["text"] == "write report"
    _assert_env(kwargs["env"], tmp_path)
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]


def test_cursor_exec_argv_and_env(monkeypatch, tmp_path):
    result, calls, inputs = _run_exec(monkeypatch, tmp_path, CursorAdapter(executable="cursor-agent"), "cursor-agent")
    assert result.status == "succeeded"
    args, kwargs = calls[1]
    assert args == (
        "cursor-agent", "-p", "--output-format", "stream-json", "--stream-partial-output", "--trust",
        "--workspace", str(tmp_path),
        PROMPT_POINTER,
    )
    assert not {"--api-key", "--force", "--yolo"} & set(args)
    assert inputs == [None, None]
    _assert_env(kwargs["env"], tmp_path)
    assert "CURSOR_API_KEY" not in kwargs["env"]


def test_cursor_prefers_cursor_agent_then_agent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: {"agent": "/x/agent"}.get(name))
    assert CursorAdapter().executable == "/x/agent"
    monkeypatch.setattr("shutil.which", lambda name: f"/x/{name}")
    assert CursorAdapter().executable == "/x/cursor-agent"


def test_claude_login_status_parses_json(monkeypatch):
    cases = [
        (b'{ "loggedIn": true, "authMethod": "claude.ai" }', 0, "ok"),
        (b'{"loggedIn": false}', 0, "needs_login"),
        (b'{"loggedIn": true}', 1, "error"),
    ]

    async def scenario(output, code, expected):
        async def fake_exec(*args, **kwargs):
            return FakeProcess((output, b""), code, [])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert await ClaudeAdapter(executable="claude").login_status() == expected

    for output, code, expected in cases:
        asyncio.run(scenario(output, code, expected))

from botoffice.providers.antigravity import AntigravityAdapter


def test_antigravity_needs_login_from_models_and_skips_exec(monkeypatch, tmp_path):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess((b"Error: Please sign in to view available models. Launch the CLI without arguments to sign in.\n", b""), 1, [])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    original_open = builtins.open
    original_path_open = Path.open

    def guard(path):
        if ".gemini" in str(path):
            raise AssertionError("adapter must not read credential files")

    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (guard(f), original_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (guard(self), original_path_open(self, *a, **k))[1])

    result = asyncio.run(AntigravityAdapter(executable="agy").run(_job(), tmp_path, tmp_path / "job.log", 1))
    assert result.status == "needs_login"
    assert calls == [("agy", "models")]


def test_antigravity_login_status_ok_from_models(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProcess((b"gemini-3-pro\ngemini-3-flash\n", b""), 0, [])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(AntigravityAdapter(executable="agy").login_status()) == "ok"


def test_antigravity_exec_argv_and_env(monkeypatch, tmp_path):
    result, calls, inputs = _run_exec(
        monkeypatch, tmp_path, AntigravityAdapter(executable="agy"), "agy"
    )
    assert result.status == "succeeded"
    args, kwargs = calls[1]
    assert args == (
        "agy", "-p", "write report", "--output-format", "stream-json", "--mode", "accept-edits",
        "--effort", "medium",
    )
    assert "--dangerously-skip-permissions" not in args
    assert inputs == [None, None]
    _assert_env(kwargs["env"], tmp_path)
    assert "GEMINI_API_KEY" not in kwargs["env"]
    assert kwargs["cwd"] == tmp_path
