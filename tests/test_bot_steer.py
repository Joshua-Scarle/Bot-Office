"""0.11.10: a bot's message steers the receiving bot's running turn by default; "queue" opts out."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.domain import Slot
from botoffice.providers.cli import _steer_prompt
from botoffice.providers.fake import FakeAdapter
from botoffice.store import InboxLimit
from botoffice.workspace import steer_note

H = {"Authorization": "Bearer test-token"}


def _store(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    return app, store


def _running(store, bot, source="chat", slot=Slot.DESKTOP_1):
    store.create("own task", "codex", bot_id=bot.id, source=source)
    job = store.claim(slot, {"codex"})
    assert job and job.bot_id == bot.id and job.source == source
    return job


def _send(store, sender, target, text, origin=None, **kwargs):
    """No pair-rate window, so one test can send several messages in a row."""
    since = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    return store.enqueue_agent_message(sender, target, text, str(origin.id) if origin else None, since,
                                       (20, 60), **kwargs)


def test_steer_reaches_running_turn_after_owner_steers(settings):
    _, store = _store(settings)
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    job = _running(store, b)
    owner, _ = store.post_steer(b.id, "owner first", [])
    message, steered, item = _send(store, a, b, "can you check X?")
    assert item["delivery"] == "steer" and item["mode"] == "steer" and item["status"] == "sent"
    assert str(steered.id) == str(job.id) and item["steered_into"] == str(job.id)
    assert item["job_id"].startswith("steer:") and item["reply"] is None
    assert message.steer_job_id == job.id and message.steer_status == "pending"
    assert message.source == "inbox" and message.sender == "bot A"
    # no turn of its own; owner messages are delivered first
    assert [j for j in store.list() if j.source == "inbox"] == []
    assert [m.text for m in store.pending_steers(job.id)] == ["owner first", "can you check X?"]
    assert owner.steer_job_id == job.id
    # authors in both chats: an inter-bot row, not an owner message
    received = next(m for m in store.list_messages(b.id) if m.text == "can you check X?")
    sent = next(m for m in store.list_messages(a.id) if m.text == "can you check X?")
    assert (received.author, received.peer) == ("bot_in", "A")
    assert (sent.author, sent.peer) == ("bot_out", "B")
    store.deliver_steer(message.id)
    assert store.get_agent_message(item["id"])["status"] == "delivered"
    assert store.delivered_steers(job.id) == 0  # a bot's steer never restarts the auto-continue count
    store.deliver_steer(owner.id)
    assert store.delivered_steers(job.id) == 1


def test_queue_mode_idle_and_switch_off(settings):
    _, store = _store(settings)
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    _, idle_job, idle = _send(store, a, b, "idle")
    assert idle["delivery"] == "start" and idle_job.source == "inbox" and idle["mode"] == "queue"
    store.claim(Slot.DESKTOP_1, {"codex"})  # B now works on that inbox turn
    _, _, busy = _send(store, a, b, "bot turn running")
    assert busy["delivery"] == "queue"  # never steered into a turn that answers a bot
    c = store.create_bot("C", "", "codex", None)
    _running(store, c, slot=Slot.DESKTOP_2)
    _, queued_job, queued = _send(store, a, c, "later", mode="queue")
    assert queued["delivery"] == "queue" and queued_job.source == "inbox"
    store.update_bot(c.id, bot_steer=0)
    assert store.get_bot(c.id).bot_steer is False
    _, _, off = _send(store, a, store.get_bot(c.id), "switched off")
    assert off["delivery"] == "queue"
    store.update_bot(c.id, bot_steer=1)
    _, _, cannot = _send(store, a, store.get_bot(c.id), "engine cannot", can_steer=lambda job: False)
    assert cannot["delivery"] == "queue"


def test_unread_steer_becomes_inbox_turn_with_reply_back(settings):
    _, store = _store(settings)
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    job = _running(store, b)
    message, _, item = _send(store, a, b, "question")
    store.queue_steers(job.id)
    agent = store.get_agent_message(item["id"])
    turn = store.get(agent["job_id"])
    assert turn.source == "inbox" and turn.prompt == "(from bot A) question" and agent["steered_into"] is None
    assert agent["status"] == "queued" and agent["mode"] == "queue"
    assert store.agent_message_for_job(turn.id)["from_bot"] == str(a.id)
    sent = next(m for m in store.list_messages(a.id) if m.text == "question")
    assert str(sent.job_id) == str(turn.id)  # the reply copy groups with the ask
    assert store.get_message(message.id).steer_status == "queued"


def test_answer_hops_allow_three_rounds(settings):
    _, store = _store(settings)
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    ja, jb = _running(store, a), _running(store, b, slot=Slot.DESKTOP_2)
    hops = []
    for _ in range(3):
        _, _, ask = _send(store, a, b, "ask", ja)
        _, _, answer = _send(store, b, a, "answer", jb)
        assert answer["reply_to"] == ask["id"] and answer["delivery"] == "steer"
        hops.append((ask["hop"], answer["hop"]))
    assert hops == [(1, 1), (2, 2), (3, 3)]
    with pytest.raises(ValueError, match="hop"):
        _send(store, a, b, "fourth ask", ja)
    # a third bot is not part of that exchange
    c = store.create_bot("C", "", "codex", None)
    assert _send(store, a, c, "other", ja)[2]["hop"] == 1


def test_steers_count_toward_inbox_caps(settings):
    _, store = _store(settings)
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    _running(store, b)
    since = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    store.enqueue_agent_message(a, b, "one", None, since, (5, 1))
    with pytest.raises(InboxLimit):
        store.enqueue_agent_message(a, b, "two", None, since, (5, 1))


def test_bot_steer_prompt_is_marked_as_a_bot(settings, tmp_path):
    _, store = _store(settings)
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    _running(store, b)
    message, _, _ = _send(store, a, b, "please check X")
    text = _steer_prompt(message, Path(tmp_path))
    assert text.startswith("[Message from the bot A") and "not from the user" in text and "bot-msg" in text
    assert "please check X" in text and "Steering message from the user" not in text
    note = steer_note([message])
    assert "other bots" in note and "(bot A)" in note


def test_api_mode_flag_and_bot_setting(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    store = app.state.store
    store.initialize()
    a, b = store.create_bot("A", "", "codex", None), store.create_bot("B", "", "codex", None)
    with TestClient(app) as client:
        assert client.patch(f"/v1/bots/{b.id}", headers=H, json={"bot_steer": False}).json()["bot_steer"] is False
        assert client.get(f"/v1/bots/{b.id}", headers=H).json()["bot_steer"] is False
        bad = client.post(f"/v1/bots/{a.id}/message-agent", headers=H, json={"to": "B", "body": "x", "mode": "later"})
        assert bad.status_code == 422
