"""B9: a session reset gives a truly clean session. Task A on a bot, reset, new memory, unrelated task B:
nothing of A reaches B's prompt, CLI session/resume args or workspace files. A follow-up in the same
session (no reset between) still sees its earlier context."""
import asyncio
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import CursorAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import EditError, JobStore

H = {"Authorization": "Bearer test-token"}
ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter, "cursor": CursorAdapter, "antigravity": AntigravityAdapter}
FLAG = {"claude": "--resume", "cursor": "--resume", "antigravity": "--conversation"}
A_TASK = "Audit the MeltpoolNet pipeline for report ZEBRA-4417"
A_REPLY = "Found issue OKAPI-9902 in the rank-only plan"
A_WORKING = "Open: send the NARWHAL-31 drafts"
A_SCRATCH = "PELICAN-5520 scratch notes"
A_WORDS = ("ZEBRA-4417", "OKAPI-9902", "NARWHAL-31", "PELICAN-5520", "MeltpoolNet")


class Run:
    def __init__(self, args, workspace, bot_dir):
        self.args, self.prompt = list(args), None
        self.workspace_files = {p.name: p.read_text(errors="replace") for p in workspace.iterdir() if p.is_file()}
        self.bot_files = {str(p.relative_to(bot_dir)): p.read_text(errors="replace")
                          for p in bot_dir.rglob("*") if p.is_file() and "reset-archive" not in p.parts}
        self.prompt = self.workspace_files.get("prompt.txt", "")


def _wire(adapter, name, runs, bot_dir_of):
    async def login_status():
        return "ok"

    async def fake_exec(args, workspace, timeout, job, parser=None):
        runs.append(Run(args, workspace, bot_dir_of(job)))
        run = runs[-1]
        resumed = args[:2] == ["exec", "resume"] if name == "codex" else FLAG[name] in args
        parser.session_id = f"ses_{name}_{len(runs):04d}_session"
        parser.final_text = A_REPLY if A_TASK in job.prompt else f"answer {len(runs)}"
        (workspace / "reply.txt").write_text(parser.final_text)
        if A_TASK in job.prompt:
            (workspace / "WORKING.md").write_text(A_WORKING)
            bot_dir = bot_dir_of(job)
            for folder in ("cursor-workspace", "antigravity-workspace"):
                (bot_dir / folder).mkdir(exist_ok=True)
                (bot_dir / folder / "scratch.txt").write_text(A_SCRATCH)
        run.resumed = resumed
        return 0, b"", b""

    adapter.login_status = login_status
    adapter._exec = fake_exec


async def _turn(store, scheduler, name):
    claimed = store.claim(Slot.DESKTOP_1, {name})
    assert claimed is not None
    await scheduler._execute(Slot.DESKTOP_1, claimed)


def _setup(settings, name):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = ADAPTERS[name](executable="/bin/true")
    runs: list[Run] = []
    _wire(adapter, name, runs, lambda job: settings.data_dir / "bots" / str(job.bot_id))
    scheduler = Scheduler(store, settings, {name: adapter})
    bot = store.create_bot("Ada", "", name, "testing reset")
    return store, scheduler, bot, runs, settings.data_dir / "bots" / str(bot.id)


def _everything(run):
    return "\n".join([run.prompt, " ".join(run.args), *run.workspace_files.values(), *run.bot_files.values()])


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_reset_then_unrelated_task_sees_nothing_of_the_earlier_one(settings, name):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, name)
        client = TestClient(create_app(settings, {name: scheduler.providers[name]}, store=store))
        scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, name)
        scheduler.turns.post_message(bot, "second turn of A")
        await _turn(store, scheduler, name)
        assert (bot_dir / f"{name}-session").exists()
        assert A_WORKING in scheduler.memory.read_working(bot.id)

        assert client.post(f"/v1/bots/{bot.id}/session/reset", headers=H).status_code == 200
        owner_memory = "Owner memory: answer briefly. HOUSE-RULE-1"
        assert client.put(f"/v1/bots/{bot.id}/memory", headers=H, json={"text": owner_memory}).status_code == 200

        scheduler.turns.post_message(bot, "What do you know about earlier work?")
        await _turn(store, scheduler, name)
        b = runs[-1]
        assert not b.resumed and not any(re.fullmatch(r"ses_\w+_session", a) for a in b.args)  # no stale CLI session
        everything = _everything(b)
        for word in A_WORDS:
            assert word not in everything, word
        assert b.workspace_files["memory.md"] == owner_memory  # memory exactly as the owner set it
        assert b.workspace_files["WORKING.md"] == ""
        assert b.workspace_files["worklog.md"] == ""
        assert "What do you know about earlier work?" in b.prompt  # its own message is there
        history = b.workspace_files["history.md"]
        assert history.count("] user:") == 1 and "] assistant:" not in history  # no earlier chat, only this message
        # kept for audit, outside what any job sees
        archive = list((bot_dir / "reset-archive").rglob("*"))
        assert any(p.name == "WORKING.md" and p.read_text() == A_WORKING for p in archive)
        # the owner's own chat view still shows the earlier conversation
        shown = client.get(f"/v1/bots/{bot.id}/history?limit=50", headers=H).json()
        assert any(A_TASK in m["text"] for m in shown["messages"])

        # the session after the reset is a new thread that continues normally
        scheduler.turns.post_message(bot, "and a follow-up")
        await _turn(store, scheduler, name)
        follow = runs[-1]
        assert follow.resumed  # the new session is resumed, and it is the one started after the reset
        assert f"ses_{name}_{len(runs) - 1:04d}_session" in follow.args
        for word in A_WORDS:
            assert word not in _everything(follow)

    asyncio.run(scenario())


def test_follow_up_in_the_same_session_still_sees_its_context(settings):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
        scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, "claude")
        scheduler.turns.post_message(bot, "Push it to 1PM")
        await _turn(store, scheduler, "claude")
        follow = runs[-1]
        assert follow.resumed  # the same CLI session carries the conversation
        assert follow.args[follow.args.index("--resume") + 1] == "ses_claude_0001_session"
        assert A_WORKING == follow.workspace_files["WORKING.md"]  # the saved working state is there
        assert A_TASK in follow.workspace_files["history.md"] and A_REPLY in follow.workspace_files["history.md"]
        assert "ZEBRA-4417" in follow.workspace_files["worklog.md"] or "Audit the MeltpoolNet" in follow.workspace_files["worklog.md"]

        # a fresh session without a reset (the CLI lost its session) is seeded with the recent chat
        (bot_dir / "claude-session").unlink()
        scheduler.turns.post_message(bot, "and again")
        await _turn(store, scheduler, "claude")
        fresh = runs[-1]
        assert not fresh.resumed
        assert A_TASK in fresh.prompt and A_REPLY in fresh.prompt and A_WORKING in fresh.prompt

    asyncio.run(scenario())


def test_reset_is_refused_while_a_job_is_queued_and_changes_nothing(settings):
    store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
    client = TestClient(create_app(settings, {"claude": scheduler.providers["claude"]}, store=store))
    scheduler.turns.post_message(bot, A_TASK)
    assert client.post(f"/v1/bots/{bot.id}/session/reset", headers=H).status_code == 409
    assert store.context_floor(bot.id) == 0 and store.context_cleanup_pending(bot.id) is None


def test_a_message_from_before_the_reset_cannot_be_edited_or_seeded_back(settings):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
        client = TestClient(create_app(settings, {"claude": scheduler.providers["claude"]}, store=store))
        old, _ = scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, "claude")
        client.post(f"/v1/bots/{bot.id}/session/reset", headers=H)
        with pytest.raises(EditError) as error:
            store.check_edit(bot.id, old.id, "changed")
        assert error.value.status == 409
        new, _ = scheduler.turns.post_message(bot, "fresh topic")
        await _turn(store, scheduler, "claude")
        # editing a message after the reset rewinds only within the new session
        scheduler.turns.edit_message(bot, new.id, "fresh topic, edited")
        await _turn(store, scheduler, "claude")
        assert all(word not in _everything(runs[-1]) for word in A_WORDS)
        assert "rewound" in runs[-1].prompt

    asyncio.run(scenario())


def test_earlier_job_directories_are_not_listed_after_a_reset(settings):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
        client = TestClient(create_app(settings, {"claude": scheduler.providers["claude"]}, store=store))
        _, first = scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, "claude")
        scheduler.turns.post_message(bot, "second")
        await _turn(store, scheduler, "claude")
        assert str(first.id) in runs[-1].workspace_files["SELF.md"]  # inside one session they are listed
        client.post(f"/v1/bots/{bot.id}/session/reset", headers=H)
        scheduler.turns.post_message(bot, "unrelated")
        await _turn(store, scheduler, "claude")
        assert str(first.id) not in runs[-1].workspace_files["SELF.md"]

    asyncio.run(scenario())


def test_a_failed_cleanup_blocks_the_next_job_until_it_succeeds(settings, monkeypatch):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
        client = TestClient(create_app(settings, {"claude": scheduler.providers["claude"]}, store=store))
        scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, "claude")
        import botoffice.context_reset as cleanup

        real = cleanup._move
        monkeypatch.setattr(cleanup, "_move", lambda *a: (_ for _ in ()).throw(OSError("disk says no")))
        assert client.post(f"/v1/bots/{bot.id}/session/reset", headers=H).status_code == 500
        assert store.context_cleanup_pending(bot.id) is not None  # still owed
        scheduler.turns.post_message(bot, "unrelated")
        count = len(runs)
        await _turn(store, scheduler, "claude")
        assert len(runs) == count  # no provider process ran on the old state
        monkeypatch.setattr(cleanup, "_move", real)
        scheduler.turns.post_message(bot, "unrelated again")
        await _turn(store, scheduler, "claude")
        assert store.context_cleanup_pending(bot.id) is None
        assert all(word not in _everything(runs[-1]) for word in A_WORDS)

    asyncio.run(scenario())


def test_two_resets_are_two_generations(settings):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
        client = TestClient(create_app(settings, {"claude": scheduler.providers["claude"]}, store=store))
        scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, "claude")
        assert client.post(f"/v1/bots/{bot.id}/session/reset", headers=H).json() == {"reset": ["claude"]}
        assert client.post(f"/v1/bots/{bot.id}/session/reset", headers=H).json() == {"reset": []}  # nothing saved
        scheduler.turns.post_message(bot, "topic B")
        await _turn(store, scheduler, "claude")
        scheduler.turns.post_message(bot, "topic C")
        await _turn(store, scheduler, "claude")
        client.post(f"/v1/bots/{bot.id}/session/reset", headers=H)
        scheduler.turns.post_message(bot, "topic D")
        await _turn(store, scheduler, "claude")
        last = runs[-1]
        assert "topic B" not in _everything(last) and "topic C" not in _everything(last)
        assert not last.resumed

    asyncio.run(scenario())


def test_a_reset_clears_an_edit_whose_job_never_reached_a_provider(settings):
    async def scenario():
        store, scheduler, bot, runs, bot_dir = _setup(settings, "claude")
        client = TestClient(create_app(settings, {"claude": scheduler.providers["claude"]}, store=store))
        first, _ = scheduler.turns.post_message(bot, A_TASK)
        await _turn(store, scheduler, "claude")
        _, job = scheduler.turns.edit_message(bot, first.id, A_TASK + " (edited)")
        claimed = store.claim(Slot.DESKTOP_1, {"claude"})
        store.finish(claimed.id, JobStatus.CANCELLED)  # the edited turn never ran: the rewind seed is still owed
        assert store.pending_rewind_seed(bot.id) is not None
        assert client.post(f"/v1/bots/{bot.id}/session/reset", headers=H).status_code == 200
        assert store.pending_rewind_seed(bot.id) is None and store.pending_session_reset(bot.id) is None
        scheduler.turns.post_message(bot, "unrelated")
        await _turn(store, scheduler, "claude")
        assert "rewound" not in runs[-1].prompt
        assert all(word not in _everything(runs[-1]) for word in A_WORDS)

    asyncio.run(scenario())
