import asyncio
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from botoffice import attachments
from botoffice.api import create_app
from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import SEED_MAX_CHARS, EditError, JobStore
from botoffice.workspace import prepare_bot_workspace

H = {"Authorization": "Bearer test-token"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class RecordingAdapter(FakeAdapter):
    """Writes reply.txt and memory.md like a real bot and records each history.md."""

    def __init__(self, delay: float = 0):
        super().__init__(delay=delay)
        self.histories: list[str] = []
        self.prompts: list[str] = []

    async def run(self, job: Job, workspace: Path, log_path: Path, timeout: int) -> RunResult:
        self.histories.append((workspace / "history.md").read_text())
        self.prompts.append(job.prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        (workspace / "reply.txt").write_text(f"reply to {job.id}")
        (workspace / "memory.md").write_text(f"memory from {job.id}")
        log_path.write_text("ok")
        return RunResult("succeeded", exit_code=0)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _idle(store, bot_id):
    return lambda: not store.pending_jobs(bot_id)


def _texts(client, bot_id, archived=False):
    url = f"/v1/bots/{bot_id}/messages" + ("?archived=true" if archived else "")
    return [(m["role"], m["text"], bool(m["archived_at"])) for m in client.get(url, headers=H).json()]


def test_edit_archives_tail_and_reruns(settings):
    adapter = RecordingAdapter()
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        first = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "one"}, headers=H).json()["message"]
        assert _wait(_idle(store, bot["id"]))
        second = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "two"}, headers=H).json()["message"]
        assert _wait(_idle(store, bot["id"]))
        response = client.post(f"/v1/bots/{bot['id']}/messages/{second['id']}/edit", json={"text": "two, fixed"}, headers=H)
        assert response.status_code == 201, response.text
        edited = response.json()["message"]
        assert edited["edit_of"] == second["id"] and edited["text"] == "two, fixed"
        assert _wait(_idle(store, bot["id"]))
        visible = _texts(client, bot["id"])
        assert [t for _, t, _ in visible][:3] == ["one", f"reply to {first['job_id']}", "two, fixed"]
        assert all(not archived for *_, archived in visible)
        everything = _texts(client, bot["id"], archived=True)
        assert ("user", "two", True) in everything
        assert ("assistant", f"reply to {second['job_id']}", True) in everything
        # the re-run saw the remaining chat only, and the new text as the prompt
        history = adapter.histories[-1]
        assert "one" in history and "two, fixed" in history
        assert "reply to " + second["job_id"] not in history and "user: two\n" not in history + "\n"
        assert "Current message:\ntwo, fixed\n" in adapter.prompts[-1]
        assert "Your tasks (Bot-Office keeps these" in adapter.prompts[-1]
        # archived rows never count as unread
        unread = store.bot_unread(bot["id"]).unread_count
        assert unread == 2  # first reply and the replacement's reply
        # the archive is soft: rows are still in the table
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute("SELECT COUNT(*) FROM messages WHERE archived_at IS NOT NULL").fetchone()[0] == 2


def test_edit_rejects_foreign_bot_assistant_routine_inbox_archived(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        store = app.state.store
        a = client.post("/v1/bots", json={"name": "A"}, headers=H).json()
        b = client.post("/v1/bots", json={"name": "B"}, headers=H).json()
        mine = client.post(f"/v1/bots/{a['id']}/messages", json={"text": "hi"}, headers=H).json()["message"]
        assert _wait(_idle(store, a["id"]))
        reply = store.add_message(a["id"], "assistant", "hello")
        routine = store.add_message(a["id"], "user", "routine", source="routine")
        inbox = store.add_message(a["id"], "user", "from bot", source="inbox", sender="bot B")
        url = "/v1/bots/{}/messages/{}/edit"
        assert client.post(url.format(a["id"], mine["id"]), json={"text": "x"}).status_code == 401
        assert client.post(url.format(b["id"], mine["id"]), json={"text": "x"}, headers=H).status_code == 404
        assert client.post(url.format(a["id"], 999999), json={"text": "x"}, headers=H).status_code == 404
        for message in (reply, routine, inbox):
            assert client.post(url.format(a["id"], message.id), json={"text": "x"}, headers=H).status_code == 422
        assert client.post(url.format(a["id"], mine["id"]), json={"text": ""}, headers=H).status_code == 422
        assert client.post(url.format(a["id"], mine["id"]), json={"text": "x"}, headers=H).status_code == 201
        assert _wait(_idle(store, a["id"]))
        # now archived
        assert client.post(url.format(a["id"], mine["id"]), json={"text": "y"}, headers=H).status_code == 404


def test_job_agent_token_cannot_edit(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        store = app.state.store
        bot = store.create_bot("Ada", "", "codex", None)
        message, job = app.state.scheduler.turns.post_message(bot, "hi")
        assert _wait(_idle(store, bot.id))
        token = store.issue_agent_token(job)
        response = client.post(f"/v1/bots/{bot.id}/messages/{message.id}/edit", json={"text": "x"},
                               headers={"Authorization": "Bearer " + token})
        assert response.status_code == 401


def test_edit_conflicts_with_queued_and_running_then_cancels(settings):
    adapter = RecordingAdapter(delay=30)
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        first = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "one"}, headers=H).json()
        assert _wait(lambda: store.get(first["job"]["id"]).status == JobStatus.RUNNING)
        second = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "two", "mode": "queue"}, headers=H).json()
        url = f"/v1/bots/{bot['id']}/messages/{first['message']['id']}/edit"
        response = client.post(url, json={"text": "one!"}, headers=H)
        assert response.status_code == 409
        assert set(response.json()["jobs"]) == {first["job"]["id"], second["job"]["id"]}
        assert len(_texts(client, bot["id"])) == 2  # unchanged
        adapter.delay = 0
        response = client.post(url, json={"text": "one!", "cancel_running": True}, headers=H)
        assert response.status_code == 201, response.text
        assert store.get(first["job"]["id"]).status == JobStatus.CANCELLED
        assert store.get(second["job"]["id"]).status == JobStatus.CANCELLED
        assert _wait(_idle(store, bot["id"]))
        assert next(t for _, t, _ in _texts(client, bot["id"])) == "one!"


def test_invalid_edit_does_not_cancel_anything(settings, monkeypatch):
    adapter = RecordingAdapter(delay=30)
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "one"}, headers=H).json()
        assert _wait(lambda: store.get(posted["job"]["id"]).status == JobStatus.RUNNING)
        url = f"/v1/bots/{bot['id']}/messages/{posted['message']['id']}/edit"
        # seed too large: rejected before cancelling
        too_long = "x" * 100_000
        monkeypatch.setattr("botoffice.store.SEED_MAX_CHARS", 1000)
        response = client.post(url, json={"text": too_long, "cancel_running": True}, headers=H)
        assert response.status_code == 413
        monkeypatch.setattr("botoffice.store.SEED_MAX_CHARS", SEED_MAX_CHARS)
        # missing attachment: rejected before cancelling
        response = client.post(url, json={"text": "x", "attachments": ["2026-01-01-" + "0" * 32 + ".png"],
                                          "cancel_running": True}, headers=H)
        assert response.status_code == 400
        assert store.get(posted["job"]["id"]).status == JobStatus.RUNNING
        client.post(f"/v1/jobs/{posted['job']['id']}/cancel", headers=H)


def test_failure_after_cancellation_is_reported(settings, monkeypatch):
    adapter = RecordingAdapter(delay=30)
    app = create_app(settings, {"codex": adapter})
    with TestClient(app) as client:
        store = app.state.store
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "one"}, headers=H).json()
        assert _wait(lambda: store.get(posted["job"]["id"]).status == JobStatus.RUNNING)
        real = store.edit_message

        def racing_edit(*args, **kwargs):
            # a routine fires between the cancellation and the commit
            app.state.scheduler.turns.post_message(store.get_bot(bot["id"]), "routine", source="routine")
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "edit_message", racing_edit)
        url = f"/v1/bots/{bot['id']}/messages/{posted['message']['id']}/edit"
        response = client.post(url, json={"text": "x", "cancel_running": True}, headers=H)
        assert response.status_code == 409
        body = response.json()
        assert body["cancelled"] == [posted["job"]["id"]] and "was cancelled" in body["detail"]
        assert "one" in [t for _, t, _ in _texts(client, bot["id"])]  # chat unchanged


def test_late_outcome_after_edit_adds_no_message_or_memory(settings):
    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        scheduler = Scheduler(store, settings, {"codex": RecordingAdapter()})
        bot = store.create_bot("Ada", "", "codex", None)
        message, job = scheduler.turns.post_message(bot, "hi")
        claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        workspace = settings.data_dir / "workspaces" / str(job.id)
        workspace.mkdir(parents=True)
        (workspace / "reply.txt").write_text("late reply")
        (workspace / "memory.md").write_text("late memory")
        store.finish(job.id, JobStatus.SUCCEEDED)  # the job is terminal, the outcome not yet published
        scheduler.turns.edit_message(bot, message.id, "hi again")
        scheduler._snapshots[str(job.id)] = ("", "")
        scheduler._record_outcome(claimed, workspace, JobStatus.SUCCEEDED, workspace / "log", None, "late reply")
        texts = [m.text for m in store.list_messages(bot.id, include_archived=True)]
        assert "late reply" not in texts
        assert scheduler.memory.read_bot(bot.id) == ""
        # a failure notice of an archived turn is dropped as well
        scheduler._record_outcome(claimed, workspace, JobStatus.FAILED, workspace / "log", "boom", "")
        assert not any("boom" in m.text for m in store.list_messages(bot.id, include_archived=True))

    asyncio.run(scenario())


def test_late_cross_bot_reply_to_archived_ask_is_dropped(settings):
    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        scheduler = Scheduler(store, settings, {"codex": RecordingAdapter()})
        sender = store.create_bot("Sender", "", "codex", None)
        target = store.create_bot("Target", "", "codex", None)
        ask, origin = scheduler.turns.post_message(sender, "ask target")
        store.claim(Slot.DESKTOP_1, {"codex"})
        _, agent_job, _ = scheduler.turns.post_agent_message(sender, target, "question", str(origin.id))
        store.finish(origin.id, JobStatus.SUCCEEDED)
        scheduler.turns.edit_message(sender, ask.id, "never mind")
        # 0.9.0: the edited chat turn (priority 0) is claimed before the bot-to-bot job (2)
        assert store.claim(Slot.DESKTOP_2, {"codex"}).prompt == "never mind"
        claimed = store.claim(Slot.DESKTOP_2, {"codex"})
        assert claimed.id == agent_job.id
        workspace = settings.data_dir / "workspaces" / str(agent_job.id)
        workspace.mkdir(parents=True)
        (workspace / "reply.txt").write_text("answer")
        scheduler._finish(claimed, workspace, JobStatus.SUCCEEDED, log_path=workspace / "log")
        # the target's own chat still gets its reply; the sender's archived ask gets nothing
        assert "answer" in [m.text for m in store.list_messages(target.id)]
        assert "answer" not in [m.text for m in store.list_messages(sender.id, include_archived=True)]
        assert store.get(agent_job.id).reply_text == "answer"

    asyncio.run(scenario())


def test_rewind_seed_is_complete_and_includes_retained_images(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    from botoffice.memory import Memory
    from botoffice.turns import Turns

    turns = Turns(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    image = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    turns.post_message(bot, "look at this", attachments=[image])
    for index in range(40):  # longer than the normal 30-message history
        store.add_message(bot.id, "assistant", f"line {index}")
    target, _ = turns.post_message(bot, "edit me")
    store.add_message(bot.id, "assistant", "after the edit point")
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='succeeded'")
        db.commit()
    _, job = turns.edit_message(bot, target.id, "edited")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    prepare_bot_workspace(store, Memory(settings.data_dir), bot, job, workspace)
    history = (workspace / "history.md").read_text()
    assert "look at this" in history and "line 0" in history and "line 39" in history
    assert "edited" in history and "after the edit point" not in history and "edit me" not in history
    stable = settings.data_dir / "bots" / str(bot.id) / "attachments" / image
    assert stable.read_bytes() == PNG and str(stable.resolve()) in history
    # once a provider turn has run with the seed, a normal job keeps the recent-history limit
    store.complete_rewind_seed(bot.id, store.pending_rewind_seed(bot.id))
    _, normal = turns.post_message(bot, "next")
    other = settings.data_dir / "workspaces" / str(normal.id)
    other.mkdir(parents=True)
    prepare_bot_workspace(store, Memory(settings.data_dir), bot, normal, other)
    assert "look at this" not in (other / "history.md").read_text()


def _seeded_edit(settings, adapter):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    image = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    scheduler.turns.post_message(bot, "look at this", attachments=[image])
    for index in range(40):
        store.add_message(bot.id, "assistant", f"line {index}")
    target, _ = scheduler.turns.post_message(bot, "edit me")
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='succeeded'")
        db.commit()
    edited, job = scheduler.turns.edit_message(bot, target.id, "edited")
    return store, scheduler, bot, edited, job


async def _run_next(store, scheduler, bot, text):
    scheduler.turns.post_message(bot, text)
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    await scheduler._execute(Slot.DESKTOP_1, claimed)


def test_seed_survives_a_replacement_cancelled_before_launch(settings):
    async def scenario():
        adapter = RecordingAdapter()
        store, scheduler, bot, edited, job = _seeded_edit(settings, adapter)
        assert store.pending_rewind_seed(bot.id) == edited.id
        store.cancel(job.id)  # cancelled while still queued: no provider ever saw the seed
        assert store.pending_rewind_seed(bot.id) == edited.id
        await _run_next(store, scheduler, bot, "follow-up")
        history = adapter.histories[-1]
        assert "look at this" in history and "line 0" in history and "follow-up" in history
        assert "edit me" not in history
        assert f"/bots/{bot.id}/attachments/" in history
        assert store.pending_rewind_seed(bot.id) is None  # that turn ran with the seed
        await _run_next(store, scheduler, bot, "later")
        assert "look at this" not in adapter.histories[-1]

    asyncio.run(scenario())


def test_seed_survives_failure_before_launch_and_mid_run_cancellation(settings):
    class NotLaunched(RecordingAdapter):
        async def run(self, job, workspace, log_path, timeout):
            self.histories.append((workspace / "history.md").read_text())
            log_path.write_text("login required")
            return RunResult("needs_login", error="login required")  # no process, no exit code

    async def scenario():
        adapter = NotLaunched()
        store, scheduler, bot, edited, _ = _seeded_edit(settings, adapter)
        claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        await scheduler._execute(Slot.DESKTOP_1, claimed)
        assert "look at this" in adapter.histories[-1]
        assert store.pending_rewind_seed(bot.id) == edited.id
        # a provider turn cancelled mid-run keeps the obligation too
        slow = RecordingAdapter(delay=5)
        scheduler.providers["codex"] = slow
        scheduler.turns.post_message(bot, "second")
        claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        task = asyncio.create_task(scheduler._execute(Slot.DESKTOP_1, claimed))
        while not slow.histories:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "look at this" in slow.histories[-1]
        assert store.pending_rewind_seed(bot.id) == edited.id
        # a failed provider process did run with the seed: the obligation is met
        failing = FakeAdapter(result="failed")
        scheduler.providers["codex"] = failing
        await _run_next(store, scheduler, bot, "third")
        assert store.pending_rewind_seed(bot.id) is None

    asyncio.run(scenario())


def test_seed_limits_and_missing_images_fail_without_change(settings, monkeypatch):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    from botoffice.turns import Turns

    turns = Turns(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    image = attachments.save(settings.data_dir, bot.id, PNG, "image/png")
    turns.post_message(bot, "pic", attachments=[image])
    target, _ = turns.post_message(bot, "edit me")
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='succeeded'")
        db.commit()
    monkeypatch.setattr("botoffice.store.SEED_MAX_IMAGES", 0)
    with pytest.raises(EditError) as error:
        turns.edit_message(bot, target.id, "x")
    assert error.value.status == 413
    monkeypatch.setattr("botoffice.store.SEED_MAX_IMAGES", 24)
    attachments.delete(settings.data_dir, bot.id, image)
    with pytest.raises(EditError):
        turns.edit_message(bot, target.id, "x")
    assert all(m.archived_at is None for m in store.list_messages(bot.id, include_archived=True))
    assert store.pending_session_reset(bot.id) is None


def test_opencode_pointer_is_reset_and_never_resumed(settings, tmp_path):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    message, _ = scheduler.turns.post_message(bot, "hi")
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='succeeded'")
        db.commit()
    bot_dir = settings.data_dir / "bots" / str(bot.id)
    bot_dir.mkdir(parents=True)
    (bot_dir / "opencode-session").write_text("ses_old")
    edited, _ = scheduler.turns.edit_message(bot, message.id, "hi!")
    # crash before the filesystem step: the marker survives in SQLite
    assert store.pending_session_reset(bot.id) == edited.id
    restarted = Scheduler(JobStore(store.path), settings, {"codex": FakeAdapter()})
    restarted.reconcile_session(bot.id)
    assert not (bot_dir / "opencode-session").exists()
    assert (bot_dir / f"opencode-session.edit-{edited.id}").read_text() == "ses_old"
    assert store.pending_session_reset(bot.id) is None
    restarted.reconcile_session(bot.id)  # idempotent
    # the next OpenCode run for this bot has no -s
    workspace = settings.data_dir / "workspaces" / "next"
    workspace.mkdir(parents=True)
    job = Job(id=uuid4(), bot_id=bot.id, prompt="p", status=JobStatus.RUNNING, created_at=datetime.now(),
              provider="opencode", model="openai/m")
    args = OpenCodeAdapter(executable="/bin/true")._exec_args(workspace, job)
    assert "-s" not in args


def test_reset_runs_before_the_next_job_even_after_engine_switch(settings):
    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        seen = []

        class Peek(FakeAdapter):
            async def run(self, job, workspace, log_path, timeout):
                seen.append((settings.data_dir / "bots" / str(job.bot_id) / "opencode-session").exists())
                return await super().run(job, workspace, log_path, timeout)

        scheduler = Scheduler(store, settings, {"codex": Peek()})
        bot = store.create_bot("Ada", "", "codex", None)
        message, _ = scheduler.turns.post_message(bot, "hi")
        store.claim(Slot.DESKTOP_1, {"codex"})
        store.finish(store.pending_jobs(bot.id)[0].id, JobStatus.SUCCEEDED)
        bot_dir = settings.data_dir / "bots" / str(bot.id)
        bot_dir.mkdir(parents=True)
        (bot_dir / "opencode-session").write_text("ses_old")  # from an earlier OpenCode period
        _ = scheduler.turns.edit_message(bot, message.id, "hi!")
        claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        await scheduler._execute(Slot.DESKTOP_1, claimed)
        assert seen == [False]
        assert store.pending_session_reset(bot.id) is None

    asyncio.run(scenario())


def test_repeated_edits_keep_chain(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    from botoffice.turns import Turns

    turns = Turns(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    original, _ = turns.post_message(bot, "v1")

    def settle():
        with closing(sqlite3.connect(store.path)) as db:
            db.execute("UPDATE jobs SET status='succeeded'")
            db.commit()

    settle()
    second, _ = turns.edit_message(bot, original.id, "v2")
    settle()
    third, _ = turns.edit_message(bot, second.id, "v3")
    assert third.edit_of == second.id and second.edit_of == original.id
    assert [m.text for m in store.list_messages(bot.id)] == ["v3"]
    assert [m.text for m in store.list_messages(bot.id, include_archived=True)] == ["v1", "v2", "v3"]
    assert store.pending_session_reset(bot.id) == third.id


def test_claude_edit_requires_reason_and_logs(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    from botoffice.turns import TurnError, Turns

    turns = Turns(store, settings, {"claude": FakeAdapter()})
    bot = store.create_bot("Claude bot", "", "claude", None)
    message, _ = turns.post_message(bot, "hi", reason="r")
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='succeeded'")
        db.commit()
    with pytest.raises(TurnError):
        turns.edit_message(bot, message.id, "edited")
    assert store.list_messages(bot.id)[0].archived_at is None
    _, job = turns.edit_message(bot, message.id, "edited", reason="fix typo")
    assert str(job.id) in (settings.data_dir / "claude-use-log.tsv").read_text()


def test_migration_keeps_old_rows_visible_and_archived_attachments_referenced(settings):
    path = settings.data_dir / "agentdesk.sqlite3"
    settings.ensure_dirs()
    with closing(sqlite3.connect(path)) as db:  # a 0.7-style messages table
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL,"
                   " role TEXT NOT NULL, sender TEXT, source TEXT NOT NULL, text TEXT NOT NULL,"
                   " job_id TEXT, created_at TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]')")
        db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments) VALUES"
                   " ('00000000-0000-0000-0000-00000000000b','user','chat','old','2026-01-01T00:00:00+00:00','[\"a.png\"]')")
        db.commit()
    store = JobStore(path)
    assert [m.text for m in store.list_messages("00000000-0000-0000-0000-00000000000b")] == ["old"]
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE messages SET archived_at='2026-01-02T00:00:00+00:00'")
        db.commit()
    assert store.list_messages("00000000-0000-0000-0000-00000000000b") == []
    assert store.attachment_references() == {"00000000-0000-0000-0000-00000000000b": {"a.png"}}


def test_lock_protocol_interleavings_do_not_deadlock(settings):
    """Opposing bot-to-bot sends, edits on both bots and outcome publication from
    several threads at once: everything finishes and no archived turn gets a reply."""
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    from botoffice.turns import TurnError, Turns

    turns = Turns(store, settings, {"codex": FakeAdapter()})
    a = store.create_bot("A", "", "codex", None)
    b = store.create_bot("B", "", "codex", None)
    errors: list[BaseException] = []
    asks = {}
    for bot in (a, b):
        message, job = turns.post_message(bot, "start")
        asks[bot.id] = (message, job)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='running'")
        db.commit()

    def send(sender, target):
        try:
            for _ in range(10):
                try:
                    turns.post_agent_message(sender, target, "ping", str(asks[sender.id][1].id))
                except TurnError:
                    pass
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def edit(bot):
        try:
            for _ in range(10):
                try:
                    turns.edit_message(bot, asks[bot.id][0].id, "edited")
                except EditError:
                    pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def publish(bot):
        try:
            for _ in range(10):
                with store.chat_lock:
                    store.publish_outcome(bot.id, asks[bot.id][1].id, "assistant", "outcome")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=fn, args=args) for fn, args in (
        (send, (a, b)), (send, (b, a)), (edit, (a,)), (edit, (b,)), (publish, (a,)), (publish, (b,)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "deadlock"
    assert not errors
    # edits were blocked by the pending jobs, so nothing was archived and nothing lost
    for bot in (a, b):
        assert all(m.archived_at is None for m in store.list_messages(bot.id, include_archived=True))
    # now let the jobs finish and edit: concurrent publishes after the edit are dropped
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE jobs SET status='succeeded'")
        db.commit()
    barrier = threading.Barrier(3)

    def edit_once(bot):
        barrier.wait()
        try:
            turns.edit_message(bot, asks[bot.id][0].id, "edited")
        except EditError as exc:
            errors.append(exc)

    def publish_once():
        barrier.wait()
        for _ in range(20):
            with store.chat_lock:
                store.publish_outcome(a.id, asks[a.id][1].id, "assistant", "racing outcome")

    threads = [threading.Thread(target=edit_once, args=(a,)), threading.Thread(target=edit_once, args=(b,)),
               threading.Thread(target=publish_once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    rows = store.list_messages(a.id, include_archived=True)
    edited = next(m for m in rows if m.edit_of == asks[a.id][0].id)
    # every outcome published for the archived job is itself archived or was dropped
    assert all(m.archived_at for m in rows if m.text == "racing outcome")
    assert edited.archived_at is None
    assert json.loads(json.dumps([m.text for m in store.list_messages(a.id)]))[-1] == "edited"
