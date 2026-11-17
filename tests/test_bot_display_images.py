"""Bot-produced images use the same private store as user uploads."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import attachments
from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.memory import Memory
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import prepare_bot_workspace

PNG = b"\x89PNG\r\n\x1a\nimage"
ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def turn(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Pictures", "", "codex", None)
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    _, job = scheduler.turns.post_message(bot, "make a picture")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    (workspace / "prompt.txt").write_text("make a picture")
    return settings, store, bot, scheduler, job, workspace


def finish(turn, text):
    _, store, bot, scheduler, job, workspace = turn
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    assert claimed is not None and claimed.id == job.id
    (workspace / "reply.txt").write_text(text)
    log = workspace / "run.log"
    log.write_text("")
    scheduler._finish(claimed, workspace, JobStatus.SUCCEEDED, log_path=log)
    return [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1]


def test_fake_adapter_turn_writes_a_reply_image(settings):
    class ImageAdapter(FakeAdapter):
        async def run(self, job, workspace, log_path, timeout):
            (workspace / "chart.png").write_bytes(PNG)
            (workspace / "reply.txt").write_text("![chart](chart.png)")
            return await super().run(job, workspace, log_path, timeout)

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Pictures", "", "codex", None)
    scheduler = Scheduler(store, settings, {"codex": ImageAdapter()})
    _, job = scheduler.turns.post_message(bot, "make a picture")
    asyncio.run(scheduler._execute(0, job))
    message = [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1]
    assert len(message.attachments) == 1
    assert attachments.read(settings.data_dir, bot.id, message.attachments[0]) == PNG


def test_reply_image_is_persisted_served_and_indexed(turn):
    settings, store, bot, _, job, workspace = turn
    (workspace / "chart.png").write_bytes(PNG)
    message = finish(turn, 'Here is the chart: ![chart](chart.png "Graph")')
    assert len(message.attachments) == 1
    name = message.attachments[0]
    assert attachments.ID_RE.fullmatch(name)
    folder = settings.data_dir / "bots" / str(bot.id) / "attachments"
    assert (folder / name).read_bytes() == PNG
    assert "![chart](chart.png" not in message.text
    assert store.get(job.id).reply_text.startswith("Here is the chart:")
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        url = f"/v1/bots/{bot.id}/attachments/{name}"
        assert client.get(url).status_code == 401
        response = client.get(url, headers=HEADERS)
        assert response.status_code == 200 and response.content == PNG
    _, next_job = turn[3].turns.post_message(bot, "look again")
    next_workspace = workspace.parent / str(next_job.id)
    next_workspace.mkdir()
    prompt, _, _ = prepare_bot_workspace(store, Memory(settings.data_dir), bot, next_job,
                                          next_workspace)
    index = (folder / "INDEX.md").read_text().lower()
    row = next(line for line in index.splitlines() if name in line)
    assert [part.strip() for part in row.split("|")][2] == "from bot"
    for text in (prompt, (next_workspace / "prompt-resume.txt").read_text(),
                 (next_workspace / "SELF.md").read_text()):
        assert "desk attach" in text and "bot-office-attach" in text
    assert str(folder / name) in (next_workspace / "history.md").read_text()
    attachments.prune(settings.data_dir, store.attachment_references())
    assert (folder / name).exists()


@pytest.mark.parametrize("command", [["desk", "attach"], ["bot-office-attach"]])
def test_helpers_attach_without_desktop(turn, command):
    workspace = turn[-1]
    (workspace / "chart.png").write_bytes(PNG)
    result = subprocess.run([sys.executable, str(ROOT / "bin" / command[0]), *command[1:],
                             "chart.png"], check=False, cwd=workspace, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    message = finish(turn, "Done")
    assert len(message.attachments) == 1


@pytest.mark.parametrize("command", [["desk", "attach"], ["bot-office-attach"]])
def test_helpers_attach_from_a_subdirectory(turn, command):
    workspace = turn[-1]
    sub = workspace / "out" / "deep"
    sub.mkdir(parents=True)
    (sub / "chart.png").write_bytes(PNG)
    result = subprocess.run([sys.executable, str(ROOT / "bin" / command[0]), *command[1:],
                             "chart.png"], check=False, cwd=sub, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (workspace / ".bot-office-outgoing-images").is_file()
    assert not (sub / ".bot-office-outgoing-images").exists()
    assert len(finish(turn, "Done").attachments) == 1


def test_helper_fails_outside_a_workspace(tmp_path):
    (tmp_path / "chart.png").write_bytes(PNG)
    result = subprocess.run([sys.executable, str(ROOT / "bin" / "bot-office-attach"), "chart.png"],
                            check=False, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0 and "workspace" in result.stderr
    assert not (tmp_path / ".bot-office-outgoing-images").exists()


def test_helpers_fail_for_missing_files(turn):
    for command in (["desk", "attach"], ["bot-office-attach"]):
        result = subprocess.run([sys.executable, str(ROOT / "bin" / command[0]), *command[1:],
                                 "missing.png"], check=False, cwd=turn[-1], capture_output=True, text=True)
        assert result.returncode != 0
        assert "missing" in (result.stderr + result.stdout).lower()


def test_deduplicates_and_caps_at_six(turn):
    workspace = turn[-1]
    for n in range(8):
        (workspace / f"{n}.png").write_bytes(PNG + bytes([n]))
    (workspace / ".bot-office-outgoing-images").write_text(
        "\n".join(str(workspace / f"{n}.png") for n in range(8)) + "\n"
    )
    message = finish(turn, "![first](0.png)")
    assert len(message.attachments) == 6
    folder = turn[0].data_dir / "bots" / str(turn[2].id) / "attachments"
    assert len({(folder / name).read_bytes() for name in message.attachments}) == 6


def test_unsafe_and_invalid_paths_are_ignored(turn):
    settings, store, _bot, _, _, workspace = turn
    other = store.create_bot("Other", "", "codex", None)
    other_path = settings.data_dir / "bots" / str(other.id) / "secret.png"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_path.write_bytes(PNG)
    token = settings.data_dir / "token"
    token.write_bytes(PNG)
    outside = workspace.parent / "outside.png"
    outside.write_bytes(PNG)
    (workspace / "escape.png").symlink_to(outside)
    (workspace / "escape-dir").symlink_to(workspace.parent, target_is_directory=True)
    (workspace / "INDEX.md").write_bytes(PNG)
    (workspace / "not-image.png").write_bytes(b"not an image")
    (workspace / "empty.png").write_bytes(b"")
    (workspace / "large.png").write_bytes(PNG + b"x" * attachments.MAX_BYTES)
    (workspace / "dir.png").mkdir()
    paths = ["../outside.png", str(other_path), str(token), "escape.png", "escape-dir/outside.png",
             "INDEX.md", "not-image.png", "empty.png", "large.png", "dir.png", "missing.png",
             "https://example.com/x.png", "file://example.com/x.png"]
    message = finish(turn, "\n".join(f"![image]({path})" for path in paths))
    assert message.attachments == []
    assert store.get(turn[4].id).status == JobStatus.SUCCEEDED


def test_quota_refusal_does_not_fail_turn(turn, monkeypatch):
    (turn[-1] / "chart.png").write_bytes(PNG)
    monkeypatch.setattr(attachments, "MAX_COUNT", 0)
    message = finish(turn, "![chart](chart.png)")
    assert message.attachments == []
    assert "quota" in message.text.lower() or "space" in message.text.lower()
    assert turn[1].get(turn[4].id).status == JobStatus.SUCCEEDED


def test_bot_can_reattach_its_own_stored_image(turn):
    settings, _, bot, _, _, _ = turn
    name = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    path = settings.data_dir / "bots" / str(bot.id) / "attachments" / name
    message = finish(turn, f"![previous]({path})")
    assert len(message.attachments) == 1
    assert attachments.read(settings.data_dir, bot.id, message.attachments[0]) == PNG


@pytest.mark.parametrize("data, accepted", [
    (b"\xff\xd8\xffjpeg", True),
    (b"GIF89agif", True),
    (b"RIFF1234WEBPimage", True),
    (b"RIFF1234NOPEimage", False),
])
def test_checks_image_signatures_not_extensions(turn, data, accepted):
    (turn[-1] / "image.png").write_bytes(data)
    message = finish(turn, "![image](image.png)")
    assert bool(message.attachments) is accepted


def test_collection_failure_still_publishes_the_reply(turn, monkeypatch):
    (turn[-1] / "chart.png").write_bytes(PNG)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(attachments, "collect_outgoing", boom)
    message = finish(turn, "Here: ![chart](chart.png)")
    assert message.attachments == []
    assert message.text == "Here: ![chart](chart.png)"


def test_skipped_images_add_a_note(turn):
    workspace = turn[-1]
    (workspace / "good.png").write_bytes(PNG)
    (workspace / "bad.png").write_bytes(b"not an image")
    message = finish(turn, "![a](good.png) ![b](bad.png) ![c](missing.png) ![d](../x.png)")
    assert len(message.attachments) == 1
    assert "3 images could not be attached." in message.text


def test_collection_is_bounded(turn, monkeypatch):
    workspace = turn[-1]
    for n in range(10):
        (workspace / f"{n}.png").write_bytes(PNG + bytes([n]))
    opened = []
    real = attachments._read_local
    monkeypatch.setattr(attachments, "_read_local",
                        lambda path, *a, **k: opened.append(path) or real(path, *a, **k))
    message = finish(turn, "".join(f"![i](missing{n}.png)" for n in range(40)) + "![ok](3.png)")
    assert message.attachments == []  # the good image is past the candidate cap
    assert len(opened) <= attachments.MAX_CANDIDATES + 1
    opened.clear()
    (workspace / ".bot-office-outgoing-images").write_text(
        "".join(f"{workspace / f'{n}.png'}\n" for n in range(10)))
    _, ids = attachments.collect_outgoing(turn[0].data_dir, turn[2].id, workspace, "", "Done", {})
    assert len(ids) == attachments.MAX_OUTGOING
    assert len(opened) == attachments.MAX_OUTGOING + 1  # sidecar + accepted images only


def _inbox_turn(settings, reply_text, files=()):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    sender = store.create_bot("Asker", "", "codex", None)
    target = store.create_bot("Drawer", "", "codex", None)
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    _, job, _ = scheduler.turns.post_agent_message(sender, target, "draw something")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    (workspace / "prompt.txt").write_text("draw something")
    for name, data in files:
        (workspace / name).write_bytes(data)
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    (workspace / "reply.txt").write_text(reply_text)
    log = workspace / "run.log"
    log.write_text("")
    scheduler._finish(claimed, workspace, JobStatus.SUCCEEDED, log_path=log)
    copy = [m for m in store.list_messages(sender.id) if m.role == "assistant"][-1]
    return store, sender, target, workspace, copy


def test_inbox_copy_of_a_reply_carries_its_images_to_the_receiving_bot(settings):
    store, sender, target, workspace, copy = _inbox_turn(
        settings, "Here: ![chart](chart.png) and ![gone](missing.png)", [("chart.png", PNG)])
    assert copy.source == "inbox" and copy.sender == "from Drawer"
    assert len(copy.attachments) == 1
    assert attachments.read(settings.data_dir, sender.id, copy.attachments[0]) == PNG  # in the receiver's store
    assert "![chart](chart.png)" not in copy.text and "![gone](missing.png)" in copy.text
    assert "1 image could not be attached" in copy.text
    own = [m for m in store.list_messages(target.id) if m.role == "assistant"][-1]
    assert len(own.attachments) == 1 and "![chart](chart.png)" not in own.text  # same handling as a normal reply


def test_inbox_copy_honours_the_attachment_cap(settings):
    store, sender, target, workspace, copy = _inbox_turn(
        settings, "\n".join(f"![i](n{n}.png)" for n in range(8)), [(f"n{n}.png", PNG + bytes([n])) for n in range(8)])
    assert len(copy.attachments) == attachments.MAX_OUTGOING
    assert "2 images could not be attached" in copy.text
    folder = settings.data_dir / "bots" / str(sender.id) / "attachments"
    assert all((folder / name).is_file() for name in copy.attachments)
