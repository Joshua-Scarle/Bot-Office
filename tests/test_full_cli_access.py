"""Bots default to full, unattended CLI access via each official CLI's documented flag."""
import os
from datetime import datetime
from uuid import uuid4

import pytest

from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import CursorAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.store import JobStore as Store


def _job(**extra):
    return Job(id=uuid4(), prompt="go", status=JobStatus.RUNNING, created_at=datetime.now(),
               slot=Slot.DESKTOP_1, **extra)


def test_job_defaults_to_full_access():
    assert _job().cli_access == "full"


@pytest.mark.parametrize("adapter,full,restricted", [
    (ClaudeAdapter(executable="claude"), ["--permission-mode", "bypassPermissions"], ["--permission-mode", "acceptEdits"]),
    (CodexAdapter(executable="codex"), ["--dangerously-bypass-approvals-and-sandbox"], ["--approve-for-me"]),
    (CursorAdapter(executable="cursor-agent"), ["--force", "--sandbox", "disabled"], []),
    (AntigravityAdapter(executable="agy"), ["--dangerously-skip-permissions"], ["--mode", "accept-edits"]),
    (OpenCodeAdapter(executable="opencode"), ["--auto"], []),
])
def test_full_and_restricted_flags(tmp_path, adapter, full, restricted):
    def has(args, seq):
        return any(args[i:i + len(seq)] == seq for i in range(len(args))) if seq else True
    full_args = adapter._exec_args(tmp_path, _job())
    limited = adapter._exec_args(tmp_path, _job(cli_access="restricted"))
    assert has(full_args, full)
    assert has(limited, restricted)
    assert not has(limited, full)


def test_full_env_inherits_user_env_but_not_bot_office_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("USER", "ubuntu")
    monkeypatch.setenv("BOT_OFFICE_TOKEN_FILE", "/secret/token")
    monkeypatch.setenv("AGENTDESK_TOKEN_FILE", "/secret/token")
    adapter = CodexAdapter(executable="codex")
    env = adapter._env(tmp_path, _job(bot_name="Bot", agent_token="agent-tok", bot_id=uuid4()))
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000" and env["USER"] == "ubuntu"
    assert env["HOME"] == os.path.expanduser("~")  # real home, not the job workspace
    assert "BOT_OFFICE_TOKEN_FILE" not in env and "AGENTDESK_TOKEN_FILE" not in env
    assert env["BOT_OFFICE_AGENT_TOKEN"] == "agent-tok"
    assert env["PATH"].split(":")[0].endswith("/bin")
    restricted = adapter._env(tmp_path, _job(cli_access="restricted"))
    assert "XDG_RUNTIME_DIR" not in restricted and restricted["HOME"] == str(tmp_path)


def test_bot_cli_access_defaults_full_and_can_be_restricted(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    bot = store.create_bot("Full", "", "codex", None)
    assert bot.cli_access == "full" and store.get_bot(bot.id).cli_access == "full"
    assert store.update_bot(bot.id, cli_access="restricted").cli_access == "restricted"
