import asyncio
import builtins
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from botoffice.domain import Job, JobStatus
from botoffice.providers.codex import CodexAdapter

from fakes import StreamProc


def test_needs_login_does_not_spawn_exec_or_read_codex_credentials(monkeypatch, tmp_path):
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            communicate=lambda: asyncio.sleep(0, result=(b"Not logged in", b"")),
            returncode=1,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    original_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if ".codex" in str(file):
            raise AssertionError("adapter must not read ~/.codex")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    original_path_open = Path.open

    def guarded_path_open(self, *args, **kwargs):
        if ".codex" in str(self):
            raise AssertionError("adapter must not read ~/.codex")
        return original_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)

    async def scenario():
        adapter = CodexAdapter(executable="codex")
        job = Job(id=uuid4(), prompt="hello", status=JobStatus.RUNNING, created_at=__import__("datetime").datetime.now())
        log = tmp_path / "job.log"
        result = await adapter.run(job, tmp_path, log, 1)
        assert result.status == "needs_login"
        assert calls == [("codex", "login", "status")]

    asyncio.run(scenario())


def test_exec_uses_confined_flags_stdin_and_never_falls_back(monkeypatch, tmp_path):
    calls = []
    inputs = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        if args[1:3] == ("login", "status"):
            return StreamProc(b"Logged in with ChatGPT", b"", 0, inputs)
        return StreamProc(b"", b"unrecognized option --approve-for-me", 2, inputs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def scenario():
        adapter = CodexAdapter(executable="codex")
        job = Job(id=uuid4(), prompt="write report", status=JobStatus.RUNNING, created_at=__import__("datetime").datetime.now(), cli_access="restricted")
        log = tmp_path / "job.log"
        result = await adapter.run(job, tmp_path, log, 1)
        assert result.status == "failed"
        assert result.error == "Codex flags are unsupported by this CLI"
        assert len(calls) == 2
        exec_args, exec_kwargs = calls[1]
        assert exec_args == (
            "codex", "exec", "-m", "gpt-6.1-terra", "-c", "model_reasoning_effort=high",
            "--approve-for-me", "--skip-git-repo-check", "--json", "-C", str(tmp_path), "-",
        )
        assert exec_kwargs["start_new_session"] is True
        assert exec_kwargs["env"]["HOME"] == str(tmp_path)
        assert exec_kwargs["env"]["CODEX_HOME"] == os.path.expanduser("~/.codex")
        assert "--sandbox" not in exec_args
        env = exec_kwargs["env"]
        assert {key for key in env if not key.startswith("GIT_CONFIG_")} <= {  # + core.hooksPath (pre-push check)
            "PATH", "LANG", "TERM", "HOME", "CODEX_HOME",
            "BOT_OFFICE_INBOX_DIR", "AGENTDESK_INBOX_DIR",
            "BOT_OFFICE_SLOT", "AGENTDESK_SLOT", "BOT_OFFICE_JOB_ID",
            "BOT_OFFICE_BOT_ID", "AGENTDESK_BOT_ID",
        }
        # This job has no slot or bot_id, so those keys are absent; when present they are paired.
        assert "BOT_OFFICE_INBOX_DIR" in env and env["BOT_OFFICE_INBOX_DIR"] == env["AGENTDESK_INBOX_DIR"]
        if "BOT_OFFICE_SLOT" in env or "AGENTDESK_SLOT" in env:
            assert env["BOT_OFFICE_SLOT"] == env["AGENTDESK_SLOT"]
        if "BOT_OFFICE_BOT_ID" in env or "AGENTDESK_BOT_ID" in env:
            assert env["BOT_OFFICE_BOT_ID"] == env["AGENTDESK_BOT_ID"]
        assert "OPENAI_API_KEY" not in exec_kwargs["env"]
        assert calls[1][0][-1] == "-"
        assert inputs == [None, b"write report"]

    asyncio.run(scenario())
