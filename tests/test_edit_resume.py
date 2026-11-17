"""Edit/rewind on top of 0.7.3 session resume: an edit resets each engine's saved session,
the next turn starts a fresh one seeded with the retained chat, archived turns never leak."""
import asyncio
from datetime import datetime
from uuid import uuid4

import pytest

from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import CursorAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

ADAPTERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "antigravity": AntigravityAdapter,
    "opencode": OpenCodeAdapter,
}
OLD = {name: f"ses_old{name}0001" for name in ADAPTERS}
NEW = {name: f"ses_new{name}0002" for name in ADAPTERS}


def _resumes(name, args, session):
    if session not in args:
        return False
    flag = {"claude": "--resume", "cursor": "--resume", "antigravity": "--conversation", "opencode": "-s"}.get(name)
    if flag is None:  # codex: `exec resume ... <id> -`
        return args[:2] == ["exec", "resume"]
    return args[args.index(flag) + 1] == session


class Run:
    def __init__(self, args, prompt, history, working):
        self.args, self.prompt, self.history, self.working = args, prompt, history, working


def _wire(adapter, name, runs):
    """Replace only the process launch: argv building, session save/resume and prompts are real."""
    async def login_status():
        return "ok"

    async def fake_exec(args, workspace, timeout, job, parser=None):
        runs.append(Run(list(args), job.prompt, (workspace / "history.md").read_text(),
                        (workspace / "WORKING.md").read_text()))
        # the CLI announces its session: OLD until the edit, NEW for the fresh one after it
        parser.session_id = OLD[name] if len(runs) <= 2 else NEW[name]
        parser.final_text = "ok"
        (workspace / "reply.txt").write_text(f"reply {len(runs)}")
        (workspace / "WORKING.md").write_text(f"W{len(runs)}" + (" ARCHIVED-WORK" if len(runs) == 2 else ""))
        return 0, b"", b""

    adapter.login_status = login_status
    adapter._exec = fake_exec


async def _turn(store, scheduler, name):
    claimed = store.claim(Slot.DESKTOP_1, {name})
    assert claimed is not None
    await scheduler._execute(Slot.DESKTOP_1, claimed)


# OpenCode (0.10.5) never resumes, so there is no continuous session for an edit to reset; its
# fresh-session-every-turn behaviour is covered in test_model_switch.py.
@pytest.mark.parametrize("name", sorted(set(ADAPTERS) - {"opencode"}))
def test_edit_resets_the_resumed_session_and_seeds_a_fresh_one(settings, name):
    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        adapter = ADAPTERS[name](executable="/bin/true")
        runs: list[Run] = []
        _wire(adapter, name, runs)
        scheduler = Scheduler(store, settings, {name: adapter})
        bot = store.create_bot("Ada", "", name, "testing edits")
        bot_dir = settings.data_dir / "bots" / str(bot.id)
        turns = scheduler.turns

        turns.post_message(bot, "first question KEEP-1")
        await _turn(store, scheduler, name)
        assert not _resumes(name, runs[0].args, OLD[name])
        assert (bot_dir / f"{name}-session").read_text() == OLD[name]
        second, _ = turns.post_message(bot, "second question SECRET-ARCHIVED")
        await _turn(store, scheduler, name)
        assert _resumes(name, runs[1].args, OLD[name])  # 0.7.3: one continuous session
        assert "ARCHIVED-WORK" in scheduler.memory.read_working(bot.id)

        edited, _ = turns.edit_message(bot, second.id, "second question, fixed")
        await _turn(store, scheduler, name)
        seeded = runs[2]
        # the stored session was reset (kept for audit), so no resume flag and the full prompt
        assert (bot_dir / f"{name}-session.edit-{edited.id}").read_text() == OLD[name]
        assert OLD[name] not in seeded.args and not _resumes(name, seeded.args, OLD[name])
        if name == "codex":
            assert seeded.args[:2] != ["exec", "resume"]
        assert "rewound" in seeded.prompt
        assert "first question KEEP-1" in seeded.prompt and "second question, fixed" in seeded.prompt
        assert "reply 1" in seeded.prompt  # the retained assistant reply is part of the seed
        for text in (seeded.prompt, seeded.history, seeded.working):
            assert "SECRET-ARCHIVED" not in text and "ARCHIVED-WORK" not in text and "reply 2" not in text
        assert seeded.working == "W1"  # WORKING.md rolled back to before the archived turn
        assert store.pending_rewind_seed(bot.id) is None
        assert (bot_dir / f"{name}-session").read_text() == NEW[name]

        turns.post_message(bot, "third question")
        await _turn(store, scheduler, name)
        follow = runs[3]
        assert _resumes(name, follow.args, NEW[name])  # the fresh session is resumed again
        assert "SECRET-ARCHIVED" not in follow.history and "ARCHIVED-WORK" not in follow.working
        assert "rewound" not in follow.prompt
        assert "ARCHIVED-WORK" not in scheduler.memory.read_working(bot.id)

    asyncio.run(scenario())


def test_archived_turn_finishing_late_does_not_write_working_md(settings, tmp_path):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = CodexAdapter(executable="/bin/true")
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.memory.write_working(bot.id, "before")
    message, job = scheduler.turns.post_message(bot, "slow turn")
    store.claim(Slot.DESKTOP_1, {"codex"})
    store.finish(job.id, JobStatus.SUCCEEDED)
    scheduler.turns.edit_message(bot, message.id, "slow turn, fixed")
    workspace = tmp_path / "late"
    workspace.mkdir()
    (workspace / "WORKING.md").write_text("LATE ARCHIVED STATE")
    log = tmp_path / "late.log"
    log.write_text("")
    scheduler._record_outcome(job, workspace, JobStatus.SUCCEEDED, log, None, "late reply")
    assert scheduler.memory.read_working(bot.id) == "before"
    assert store.snapshot_working(bot.id, job.id, "before") is False
    assert all("late reply" not in m.text for m in store.list_messages(bot.id, include_archived=True))


def test_pending_seed_never_resumes_a_session_saved_meanwhile(settings, tmp_path):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": CodexAdapter(executable="/bin/true")})
    bot = store.create_bot("Ada", "", "codex", None)
    message, job = scheduler.turns.post_message(bot, "hi")
    store.claim(Slot.DESKTOP_1, {"codex"})
    store.finish(job.id, JobStatus.SUCCEEDED)
    edited, _ = scheduler.turns.edit_message(bot, message.id, "hi!")
    scheduler.reconcile_session(bot.id)
    bot_dir = settings.data_dir / "bots" / str(bot.id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "claude-session").write_text("ses_stray00001")  # saved while the seed was still owed
    scheduler.reconcile_session(bot.id, seeding=True)
    assert not (bot_dir / "claude-session").exists()
    assert (bot_dir / f"claude-session.edit-{edited.id}").read_text() == "ses_stray00001"
    job = Job(id=uuid4(), bot_id=bot.id, prompt="p", status=JobStatus.RUNNING, created_at=datetime.now())
    workspace = settings.data_dir / "workspaces" / "next"
    workspace.mkdir(parents=True)
    assert ClaudeAdapter(executable="/bin/true")._saved_session(workspace, job) is None


def test_long_seed_stays_in_history_md(settings, monkeypatch):
    monkeypatch.setattr("botoffice.workspace.SEED_INLINE_BYTES", 10)
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": CodexAdapter(executable="/bin/true")})
    bot = store.create_bot("Ada", "", "codex", None)
    message, job = scheduler.turns.post_message(bot, "a long retained question")
    store.claim(Slot.DESKTOP_1, {"codex"})
    store.finish(job.id, JobStatus.SUCCEEDED)
    _, edited_job = scheduler.turns.edit_message(bot, message.id, "a long retained question, fixed")
    from botoffice.workspace import prepare_bot_workspace

    workspace = settings.data_dir / "workspaces" / "seed"
    workspace.mkdir(parents=True)
    prompt, _, _ = prepare_bot_workspace(store, scheduler.memory, bot, edited_job, workspace)
    assert "read history.md" in prompt and "rewound" in prompt
    assert "a long retained question, fixed" in (workspace / "history.md").read_text()
