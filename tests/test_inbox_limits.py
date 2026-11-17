import json
from dataclasses import replace
from datetime import timedelta

import pytest

from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore, now
from botoffice.turns import InboxLimitError, TurnError, Turns


def _setup(settings, per_sender=2, overall=3):
    settings = replace(settings, inbox_per_sender_hourly=per_sender, inbox_global_hourly=overall)
    store = JobStore(settings.data_dir / "jobs.sqlite3")
    return settings, store, Turns(store, settings, {"codex": FakeAdapter()})


def test_inbox_turns_are_capped_per_sender_and_overall(settings):
    settings, store, turns = _setup(settings)
    bot = store.create_bot("Scout", "", "codex", None)
    for _ in range(2):
        turns.post_message(bot, "hi", source="inbox", sender="CoS")
    with pytest.raises(InboxLimitError, match="from this sender"):
        turns.post_message(bot, "hi", source="inbox", sender="CoS")
    # another sender has its own count, until the overall cap
    turns.post_message(bot, "hi", source="inbox", sender="Other")
    with pytest.raises(InboxLimitError, match="3 turns per hour"):
        turns.post_message(bot, "hi", source="inbox", sender="Third")
    # chat turns are not inbox turns
    turns.post_message(bot, "hi")
    # the window rolls
    with store._connect() as db:
        db.execute("UPDATE jobs SET created_at=? WHERE source='inbox'", ((now() - timedelta(hours=2)).isoformat(),))
    turns.post_message(bot, "hi", source="inbox", sender="CoS")


def test_bot_to_bot_messages_share_the_caps(settings):
    settings, store, turns = _setup(settings, per_sender=1, overall=10)
    a = store.create_bot("A", "", "codex", None)
    b = store.create_bot("B", "", "codex", None)
    c = store.create_bot("C", "", "codex", None)
    turns.post_agent_message(a, b, "one")
    # another target: not the 10 s pair limit but A's hourly cap
    with pytest.raises(InboxLimitError, match="from this sender"):
        turns.post_agent_message(a, c, "two")
    assert isinstance(InboxLimitError("x"), TurnError)
    turns.post_agent_message(b, c, "three")


def test_unattributed_inbox_files_are_capped_without_flooding_the_chat(settings):
    settings, store, turns = _setup(settings, per_sender=2, overall=50)
    bot = store.create_bot("Scout", "", "codex", None)
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    directory = settings.data_dir / "inboxes" / "_by-name"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        (directory / f"{index}.json").write_text(json.dumps({"to": "Scout", "text": f"file {index}"}))
    scheduler._read_inboxes()
    assert not list(directory.glob("*.json"))
    assert len([job for job in store.list() if job.source == "inbox"]) == 2
    # the dropped files left no chat lines
    assert [m.role for m in store.list_messages(bot.id)] == ["user", "user"]


def test_inbox_caps_come_from_the_environment(monkeypatch):
    from botoffice.config import Settings

    monkeypatch.setenv("BOT_OFFICE_INBOX_PER_SENDER_HOURLY", "7")
    monkeypatch.setenv("BOT_OFFICE_INBOX_HOURLY", "70")
    settings = Settings.from_env()
    assert (settings.inbox_per_sender_hourly, settings.inbox_global_hourly) == (7, 70)
    monkeypatch.delenv("BOT_OFFICE_INBOX_PER_SENDER_HOURLY")
    monkeypatch.delenv("BOT_OFFICE_INBOX_HOURLY")
    assert (Settings.from_env().inbox_per_sender_hourly, Settings.from_env().inbox_global_hourly) == (20, 60)
