import asyncio

from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import STANDING_RULES_NOTICE, prepare_bot_workspace, save_rules_snapshot


def test_rules_notice_tracks_shared_memory_and_fresh_prompts(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "hello")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    scheduler.memory.write_shared("first rules\n")

    def prepare():
        prepare_bot_workspace(store, scheduler.memory, bot, job, workspace)
        return (workspace / "prompt.txt").read_text(), (workspace / "prompt-resume.txt").read_text()

    fresh, resume = prepare()
    assert fresh.count(STANDING_RULES_NOTICE) == resume.count(STANDING_RULES_NOTICE) == 1
    save_rules_snapshot(scheduler.memory.read_shared(), scheduler.memory.bot_dir(bot.id))
    fresh, resume = prepare()
    assert fresh.count(STANDING_RULES_NOTICE) == 1
    assert STANDING_RULES_NOTICE not in resume
    scheduler.memory.write_shared("changed rules\n")
    assert prepare()[1].count(STANDING_RULES_NOTICE) == 1


def test_failed_turn_does_not_consume_rules_notice(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = FakeAdapter(result="failed")
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.memory.write_shared("rules\n")
    _, first = scheduler.turns.post_message(bot, "first")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    assert not (scheduler.memory.bot_dir(bot.id) / "rules.sha256").exists()
    _, second = scheduler.turns.post_message(bot, "second")
    workspace = settings.data_dir / "workspaces" / str(second.id)
    workspace.mkdir(parents=True)
    prepare_bot_workspace(store, scheduler.memory, bot, second, workspace)
    assert STANDING_RULES_NOTICE in (workspace / "prompt-resume.txt").read_text()
    assert STANDING_RULES_NOTICE in (settings.data_dir / "workspaces" / str(first.id) / "prompt-resume.txt").read_text()
    adapter.result = "succeeded"
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    prepare_bot_workspace(store, scheduler.memory, bot, second, workspace)
    assert STANDING_RULES_NOTICE not in (workspace / "prompt-resume.txt").read_text()


def test_rules_snapshot_write_failure_does_not_fail_turn_or_mark_handoff(settings, monkeypatch, caplog):
    import botoffice.scheduler as scheduler_module

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "hello")

    def fail(*args):
        raise OSError("snapshot failed")

    monkeypatch.setattr(scheduler_module, "save_rules_snapshot", fail)
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    assert store.get(job.id).status == "succeeded"
    assert not (scheduler.memory.bot_dir(bot.id) / ".handoff-pending").exists()
    assert "Could not save rules snapshot" in caplog.text
