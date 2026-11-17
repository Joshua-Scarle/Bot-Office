"""0.11.0 review Sec 1: no controller read or write follows a link a bot planted in the shared tree."""
import asyncio
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from botoffice import isolation, safefs
from botoffice.domain import Job, JobStatus
from botoffice.memory import Memory
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

SECRET = "MASTER-TOKEN-0123456789abcdef"


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "private" / "token"
    path.parent.mkdir()
    path.write_text(SECRET)
    path.chmod(0o600)
    return path


def untouched(path):
    return path.read_text() == SECRET


def test_safefs_refuses_links_at_the_name_and_above_it(tmp_path, secret):
    root = tmp_path / "data"
    (root / "bots" / "a").mkdir(parents=True)
    safefs.add_root(root)
    link = root / "bots" / "a" / "memory.md"
    link.symlink_to(secret)
    assert safefs.read_text(link) == "" and not safefs.is_file(link) and safefs.exists(link)
    with pytest.raises(safefs.UnsafePath):
        safefs.read_bytes(link)
    with pytest.raises(safefs.UnsafePath):
        safefs.chmod(link, 0o666)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    safefs.write_text(link, "new")  # replaces the link itself
    assert untouched(secret) and not link.is_symlink() and link.read_text() == "new"
    # a link in place of a directory below the root: nothing is read or written through it
    (root / "bots" / "b").symlink_to(secret.parent)
    assert safefs.read_text(root / "bots" / "b" / "token") == ""
    with pytest.raises(safefs.UnsafePath):
        safefs.write_text(root / "bots" / "b" / "token", "x")
    with pytest.raises(safefs.UnsafePath):
        safefs.mkdir(root / "bots" / "b" / "sub")
    assert untouched(secret) and not (secret.parent / "sub").exists()
    assert safefs.names(root / "bots" / "b") == []
    os.mkfifo(root / "bots" / "a" / "fifo")
    assert safefs.read_text(root / "bots" / "a" / "fifo") == ""  # never blocks on a FIFO
    safefs.unlink(root / "bots" / "b")
    assert secret.parent.is_dir() and untouched(secret)


def test_memory_files_never_follow_links(tmp_path, secret):
    memory = Memory(tmp_path / "data")
    bot = uuid4()
    memory.write_bot(bot, "fact\n")
    for name in ("memory.md", "WORKING.md", "worklog.md"):
        path = memory.bot_dir(bot) / name
        path.unlink(missing_ok=True)
        path.symlink_to(secret)
    assert memory.read_bot(bot) == "" and memory.read_working(bot) == "" and memory.read_worklog(bot) == ""
    memory.write_bot(bot, "mine\n")
    memory.write_working(bot, "w\n")
    memory.append_worklog(bot, "line")
    assert untouched(secret) and memory.read_bot(bot) == "mine\n"
    # the lock file too
    lock = memory.bot_dir(bot) / ".lock"
    lock.unlink()
    lock.symlink_to(secret)
    with pytest.raises(OSError):
        memory.write_bot(bot, "x")
    assert untouched(secret)
    # the whole bot dir swapped for a link to the private dir
    other = uuid4()
    memory.bot_dir(other).symlink_to(secret.parent)
    assert memory.read_bot(other) == ""
    with pytest.raises(OSError):
        memory.write_bot(other, "x")
    assert sorted(os.listdir(secret.parent)) == ["token"]


def job_for(bot_id, **extra):
    return Job(**{"id": uuid4(), "prompt": "go", "status": JobStatus.RUNNING, "created_at": datetime.now(),
                  "bot_id": bot_id, "provider": "codex", **extra})


def test_session_pointer_links_are_not_read_or_written_through(tmp_path, secret):
    data = tmp_path / "data"
    safefs.add_root(data)
    bot = uuid4()
    workspace = data / "workspaces" / "j"
    workspace.mkdir(parents=True)
    bot_dir = data / "bots" / str(bot)
    bot_dir.mkdir(parents=True)
    # the token looks like a session id; a planted pointer link must not put it in argv
    secret.write_text("abcdefgh12345678")
    pointer = bot_dir / "codex-session"
    pointer.symlink_to(secret)
    adapter = CodexAdapter(executable="codex")
    job = job_for(bot, cli_access="full")
    assert adapter._saved_session(workspace, job) is None
    adapter._save_session(workspace, job, "0199a213-81c0-7800-8aa1-bbab2a035a53")
    assert secret.read_text() == "abcdefgh12345678" and not pointer.is_symlink()
    assert "abcdefgh12345678" not in adapter._exec_args(workspace, job_for(bot))
    # 0.10.6: OpenCode never resumes, so it neither reads nor writes a pointer (a planted one stays inert)
    (bot_dir / "opencode-session").symlink_to(secret)
    opencode = OpenCodeAdapter(executable="opencode")
    job = job_for(bot, provider="opencode")
    assert opencode._saved_session(workspace, job) is None
    opencode._save_session(workspace, job, "ses_abc")
    assert secret.read_text() == "abcdefgh12345678"


def test_codex_usage_baseline_is_not_read_or_written_through_a_link(tmp_path, secret):
    """0.10.5's per-job usage baseline is controller state in the shared tree (Sec 1)."""
    data = tmp_path / "data"
    safefs.add_root(data)
    bot = uuid4()
    workspace = data / "workspaces" / "j"
    workspace.mkdir(parents=True)
    (data / "bots" / str(bot)).mkdir(parents=True)
    adapter = CodexAdapter(executable="codex")
    job = job_for(bot, cli_access="full")
    secret.write_text(json.dumps({"thread": "t", "total": {"input_tokens": 1}}))
    baseline = adapter._baseline_file(workspace, job)
    baseline.symlink_to(secret)
    # the linked "baseline" is ignored: a resumed thread with no baseline reports no usage
    assert adapter._job_usage(workspace, job, [("t", "t", {"input_tokens": 50})]) is None
    assert json.loads(secret.read_text())["total"] == {"input_tokens": 1} and not baseline.is_symlink()
    assert json.loads(baseline.read_text()) == {"thread": "t", "total": {"input_tokens": 50}}


def test_antigravity_stable_dir_refuses_links_and_carries_the_gate_hook(tmp_path, secret):
    """0.10.6's stable per-bot dir: marker never touched through a link; the gate hook goes in the
    CLI's working directory (that dir), not only the job workspace."""
    from botoffice.providers.antigravity import AntigravityAdapter
    data = tmp_path / "data"
    safefs.add_root(data)
    bot = uuid4()
    workspace = data / "workspaces" / "j"
    workspace.mkdir(parents=True)
    bot_dir = data / "bots" / str(bot)
    bot_dir.mkdir(parents=True)
    adapter = AntigravityAdapter(executable="agy")
    job = job_for(bot, provider="antigravity", cli_access="full", gate_command="/x/bot-office-gate --mode attended")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (bot_dir / "antigravity-workspace").symlink_to(elsewhere)
    with pytest.raises(OSError):
        adapter._cwd(workspace, job)
    assert list(elsewhere.iterdir()) == []
    (bot_dir / "antigravity-workspace").unlink()
    stable = adapter._cwd(workspace, job)
    (stable / ".stable").unlink()
    (stable / ".stable").symlink_to(secret)
    before = secret.read_text()
    assert adapter._saved_session(workspace, job) is None and secret.read_text() == before
    adapter._exec_args(workspace, job)
    hook = json.loads((stable / ".agents" / "hooks.json").read_text())
    assert hook["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == job.gate_command


def test_isolated_state_lives_in_the_private_dir(settings, tmp_path):
    import dataclasses

    settings = dataclasses.replace(settings, private_dir=tmp_path / "private-state")
    settings.private_dir.mkdir(mode=0o700)
    isolation.configure(settings, isolation.Launcher(Path("/nonexistent"), "bots"))
    try:
        bot = uuid4()
        bot_dir = settings.data_dir / "bots" / str(bot)
        assert isolation.state_dir(bot_dir) == settings.private_dir / "bot-state" / str(bot)
        assert isolation.state_dir(isolation.state_dir(bot_dir)) == isolation.state_dir(bot_dir)
        workspace = settings.data_dir / "workspaces" / "j"
        workspace.mkdir(parents=True)
        CodexAdapter(executable="codex")._save_session(workspace, job_for(bot), "0199a213-81c0-7800-8aa1-bbab2a035a53")
        assert (settings.private_dir / "bot-state" / str(bot) / "codex-session").is_file()
        assert not (bot_dir / "codex-session").exists()
        # a pointer the bot plants in its own dir is ignored
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "claude-session").write_text("0199a213-81c0-7800-8aa1-000000000000")
        from botoffice.providers.claude import ClaudeAdapter
        assert ClaudeAdapter(executable="claude")._saved_session(workspace, job_for(bot, provider="claude")) is None
    finally:
        isolation.configure(settings, None)


def test_scheduler_ignores_planted_links_in_workspace_and_inbox(settings, secret):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")

    class Plants(FakeAdapter):
        async def run(self, job, ws, log_path, timeout):
            for name in ("reply.txt", "WORKING.md", "memory.md"):
                (ws / name).unlink(missing_ok=True)
                (ws / name).symlink_to(secret)
            return await super().run(job, ws, log_path, timeout)

    scheduler = Scheduler(store, settings, {"codex": Plants()})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "hi")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    texts = [message.text for message in store.list_messages(bot.id)]
    assert not any(SECRET in text for text in texts)
    assert SECRET not in scheduler.memory.read_working(bot.id) and SECRET not in scheduler.memory.read_bot(bot.id)
    assert untouched(secret)
    # a retried job's workspace: the controller's own files replace planted links
    workspace = settings.data_dir / "workspaces" / str(job.id)
    for name in (".bot-office-gate.json", "prompt.txt", "AGENTS.md", "SELF.md"):
        (workspace / name).unlink(missing_ok=True)
        (workspace / name).symlink_to(secret)
    scheduler.gate_fields(store.get(job.id), bot, workspace)
    from botoffice.workspace import prepare_bot_workspace
    prepare_bot_workspace(store, scheduler.memory, bot, store.get(job.id), workspace, 60, None)
    assert untouched(secret) and not (workspace / "SELF.md").is_symlink()
    # an inbox file that is a link to the token is dropped unread
    inbox = settings.data_dir / "inboxes" / str(bot.id)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "x.json").symlink_to(secret)
    scheduler._read_inboxes()
    assert untouched(secret) and not (inbox / "x.json").exists()
    assert not any(SECRET in message.text for message in store.list_messages(bot.id))


def test_cli_delegation_files_do_not_follow_links(settings, secret):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    bot_dir = scheduler.memory.bot_dir(bot.id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "CLAUDE.md").symlink_to(secret)
    _, job = scheduler.turns.post_message(bot, "hi")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    from botoffice.workspace import prepare_bot_workspace
    prepare_bot_workspace(store, scheduler.memory, bot, store.get(job.id), workspace, 60, None)
    assert untouched(secret) and SECRET not in (workspace / "CLAUDE.md").read_text()
    assert "bot-office:cli-delegation" in (workspace / "AGENTS.md").read_text()


def test_handoff_marker_and_snapshots_refuse_links(settings, secret):
    from botoffice.scheduler import _mark_handoff_pending
    from botoffice.workspace import SELF_SNAPSHOT, model_switch, save_settings_snapshot

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    memory = Memory(settings.data_dir)
    bot = store.create_bot("Ada", "", "codex", None)
    bot_dir = memory.bot_dir(bot.id)
    bot_dir.mkdir(parents=True)
    (bot_dir / ".handoff-pending").symlink_to(secret)
    with pytest.raises(OSError):
        _mark_handoff_pending(bot_dir)
    (bot_dir / SELF_SNAPSHOT).symlink_to(secret)
    save_settings_snapshot(bot, bot_dir)
    assert untouched(secret) and json.loads((bot_dir / SELF_SNAPSHOT).read_text())["last"]
    # a snapshot link is never followed (unreadable: treated like a lost snapshot, so a handoff)
    (bot_dir / SELF_SNAPSHOT).unlink()
    (bot_dir / SELF_SNAPSHOT).symlink_to(secret)
    assert model_switch(job_for(bot.id), bot_dir) == (("", ""), False)
