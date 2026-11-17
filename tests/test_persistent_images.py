"""Sent images stay available to their bot for every later turn."""
import pytest

from botoffice import attachments
from botoffice.memory import Memory
from botoffice.store import JobStore
from botoffice.workspace import prepare_bot_workspace

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def _setup(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Pics", "", "codex", None)
    return store, bot


def test_earlier_image_is_reachable_in_a_later_turn(settings):
    from botoffice.scheduler import Scheduler
    store, bot = _setup(settings)
    from botoffice.providers.fake import FakeAdapter
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    name = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    msg, first = scheduler.turns.post_message(bot, "here is the worksheet photo", attachments=[name])
    ws1 = settings.data_dir / "workspaces" / str(first.id); ws1.mkdir(parents=True)
    prepare_bot_workspace(store, Memory(settings.data_dir), bot, first, ws1)
    store.finish(first.id, __import__("botoffice.domain", fromlist=["JobStatus"]).JobStatus.SUCCEEDED)
    import shutil; shutil.rmtree(ws1)  # the old job directory is gone
    _, second = scheduler.turns.post_message(bot, "describe the photo from earlier")
    ws2 = settings.data_dir / "workspaces" / str(second.id); ws2.mkdir(parents=True)
    prompt, _, _ = prepare_bot_workspace(store, Memory(settings.data_dir), bot, second, ws2)
    stable = (settings.data_dir / "bots" / str(bot.id) / "attachments" / name).resolve()
    assert stable.read_bytes() == PNG
    for text in (prompt, (ws2 / "prompt-resume.txt").read_text(), (ws2 / "history.md").read_text()):
        assert "INDEX.md" in text or str(stable) in text
    assert str(stable) in (ws2 / "history.md").read_text()
    index = (stable.parent / "INDEX.md").read_text()
    assert f"message {msg.id}" in index and str(stable) in index and "worksheet photo" in index


def test_sent_images_are_never_pruned(settings, monkeypatch):
    store, bot = _setup(settings)
    sent = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    unsent = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    monkeypatch.setattr(attachments, "UNREFERENCED_TTL", -1)
    attachments.prune(settings.data_dir, {str(bot.id): {sent}})
    folder = settings.data_dir / "bots" / str(bot.id) / "attachments"
    assert (folder / sent).exists() and not (folder / unsent).exists()


def test_quota_refuses_new_uploads_instead_of_deleting(settings, monkeypatch):
    store, bot = _setup(settings)
    sent = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    attachments.write_index(settings.data_dir, bot.id, [("2026-11-14 12:00", 1, sent, "x")])
    monkeypatch.setattr(attachments, "MAX_COUNT", 1)
    with pytest.raises(OverflowError, match="never deleted automatically"):
        attachments.save(settings.data_dir, bot.id, PNG, "image/png", {str(bot.id): {sent}})
    assert (settings.data_dir / "bots" / str(bot.id) / "attachments" / sent).exists()
    monkeypatch.setattr(attachments, "MAX_COUNT", 2)  # INDEX.md does not count as an image
    attachments.save(settings.data_dir, bot.id, PNG, "image/png", {str(bot.id): {sent}})


def test_index_is_not_served_through_the_api(settings):
    store, bot = _setup(settings)
    attachments.write_index(settings.data_dir, bot.id, [])
    with pytest.raises(ValueError):
        attachments.read(settings.data_dir, bot.id, "INDEX.md")
