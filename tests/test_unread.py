import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}


def _store(settings) -> JobStore:
    return JobStore(settings.data_dir / "agentdesk.sqlite3")


def _bot(store, name="Ada"):
    return store.create_bot(name, "", "codex", None)


def test_seed_migration_marks_existing_history_read(settings):
    path = settings.data_dir / "agentdesk.sqlite3"
    store = _store(settings)
    bot = _bot(store)
    store.add_message(bot.id, "assistant", "old reply")
    # simulate a 0.7 database: no read table, no seed marker
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE bot_reads")
        db.execute("DELETE FROM meta WHERE key='reads-seeded-v1'")
        db.commit()
    upgraded = _store(settings)
    assert upgraded.bot_unread(bot.id).unread_count == 0
    upgraded.add_message(bot.id, "assistant", "new reply")
    assert upgraded.bot_unread(bot.id).unread_count == 1
    # the seed runs once: a later restart does not mark the new reply read
    assert _store(settings).bot_unread(bot.id).unread_count == 1


def test_counts_only_bot_messages_and_reports_age(settings):
    store = _store(settings)
    bot = _bot(store)
    other = _bot(store, "Bob")
    store.add_message(bot.id, "user", "hi")
    store.add_message(bot.id, "user", "routine prompt", source="routine")
    store.add_message(bot.id, "user", "from another bot", sender="bot Bob", source="inbox")
    assert store.bot_unread(bot.id).unread_count == 0
    first = store.add_message(bot.id, "assistant", "hello")
    store.add_message(bot.id, "system", "Job failed: x")
    # a reply to this bot's message to another bot lands here as assistant
    store.add_message(bot.id, "assistant", "reply", sender="from Bob", source="inbox")
    state = store.bot_unread(bot.id)
    assert state.unread_count == 3
    assert state.unread_since == first.created_at
    assert not state.marked_unread
    assert store.bot_unread(other.id).unread_count == 0


def test_read_is_monotonic_and_capped(settings):
    store = _store(settings)
    bot = _bot(store)
    a = store.add_message(bot.id, "assistant", "a")
    b = store.add_message(bot.id, "assistant", "b")
    assert store.mark_read(bot.id, up_to=a.id)
    assert store.bot_unread(bot.id).unread_count == 1
    assert store.mark_read(bot.id, up_to=0)  # older position: no-op, never backwards
    assert store.bot_unread(bot.id).unread_count == 1
    assert store.mark_read(bot.id, up_to=10**9)  # capped at the newest message
    assert store.bot_unread(bot.id).unread_count == 0
    c = store.add_message(bot.id, "assistant", "c")
    assert store.bot_unread(bot.id).unread_count == 1
    assert c.id > b.id


def test_mark_unread_rewinds_and_manual_flag(settings):
    store = _store(settings)
    bot = _bot(store)
    empty = _bot(store, "Empty")
    store.add_message(bot.id, "assistant", "a")
    latest = store.add_message(bot.id, "assistant", "b")
    store.add_message(bot.id, "user", "later prompt")
    store.mark_read(bot.id)
    store.mark_unread(bot.id)
    state = store.bot_unread(bot.id)
    assert state.unread_count == 1 and state.marked_unread
    assert state.unread_since == latest.created_at
    # mark-as-unread never reduces an existing unread count
    store.mark_unread(bot.id)
    assert store.bot_unread(bot.id).unread_count == 1
    store.mark_unread(empty.id)
    state = store.bot_unread(empty.id)
    assert state.unread_count == 0 and state.marked_unread and state.unread_since


def test_stale_read_cannot_undo_newer_unread(settings):
    store = _store(settings)
    bot = _bot(store)
    store.add_message(bot.id, "assistant", "a")
    b = store.add_message(bot.id, "assistant", "b")
    seen = store.bot_unread(bot.id).read_revision  # tab A loads at this revision
    store.mark_read(bot.id, revision=seen)
    store.mark_unread(bot.id)  # tab B
    # tab A's delayed reads (poll or explicit) carry the old revision: rejected as a whole
    assert not store.mark_read(bot.id, up_to=b.id, revision=seen)
    assert not store.mark_read(bot.id, up_to=b.id, revision=seen, explicit=True)
    state = store.bot_unread(bot.id)
    assert state.unread_count == 1 and state.marked_unread
    # background read with the current revision (poll, visibility change) keeps the flag
    assert not store.mark_read(bot.id, up_to=b.id, revision=state.read_revision)
    # an omitted revision is never enough to clear a manual unread
    assert not store.mark_read(bot.id, explicit=True)
    assert store.bot_unread(bot.id).unread_count == 1
    # explicit open with the current revision clears it
    assert store.mark_read(bot.id, up_to=b.id, revision=state.read_revision, explicit=True)
    state = store.bot_unread(bot.id)
    assert state.unread_count == 0 and not state.marked_unread


def test_read_never_covers_messages_not_loaded(settings):
    store = _store(settings)
    bot = _bot(store)
    shown = store.add_message(bot.id, "assistant", "shown")
    store.add_message(bot.id, "assistant", "arrived during load")
    store.mark_read(bot.id, up_to=shown.id, revision=0, explicit=True)
    assert store.bot_unread(bot.id).unread_count == 1


def test_delete_bot_drops_read_state(settings):
    store = _store(settings)
    bot = _bot(store)
    store.mark_unread(bot.id)
    store.delete_bot(bot.id)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM bot_reads").fetchone()[0] == 0


def test_api_read_unread_and_bot_fields(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        assert client.post("/v1/bots/00000000-0000-0000-0000-000000000000/read").status_code == 401
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        store = app.state.store
        msg = store.add_message(bot["id"], "assistant", "hi")
        listed = client.get("/v1/bots", headers=H).json()[0]
        assert listed["unread_count"] == 1 and listed["unread_since"]
        assert listed["marked_unread"] is False and listed["read_revision"] == 0
        assert client.post(f"/v1/bots/{bot['id']}/read", json={"up_to": -1}, headers=H).status_code == 422
        state = client.post(f"/v1/bots/{bot['id']}/read", json={"up_to": msg.id, "revision": 0}, headers=H).json()
        assert state["unread_count"] == 0
        state = client.post(f"/v1/bots/{bot['id']}/unread", headers=H).json()
        assert state["unread_count"] == 1 and state["marked_unread"] and state["read_revision"] == 1
        # a stale poll is ignored and returns the current state to resync from
        state = client.post(f"/v1/bots/{bot['id']}/read", json={"revision": 0}, headers=H).json()
        assert state["unread_count"] == 1 and state["read_revision"] == 1
        state = client.post(f"/v1/bots/{bot['id']}/read", json={"revision": 1, "explicit": True}, headers=H).json()
        assert state["unread_count"] == 0 and not state["marked_unread"]
        missing = "00000000-0000-0000-0000-000000000000"
        assert client.post(f"/v1/bots/{missing}/unread", headers=H).status_code == 404


def test_get_bot_reports_unread_state_for_a_click_time_resync(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "Ada", "engine": "codex"}).json()
        store = _store(settings)
        store.add_message(bot["id"], "assistant", "reply")
        fresh = client.get("/v1/bots/" + bot["id"], headers=H).json()
        assert fresh["unread_count"] == 1 and fresh["read_revision"] == 0 and fresh["marked_unread"] is False
        client.post("/v1/bots/" + bot["id"] + "/unread", headers=H)
        fresh = client.get("/v1/bots/" + bot["id"], headers=H).json()
        assert fresh["marked_unread"] is True and fresh["read_revision"] == 1
        # a click carrying the older revision is ignored; the resynced one clears it
        stale = client.post("/v1/bots/" + bot["id"] + "/read", headers=H, json={"revision": 0, "explicit": True}).json()
        assert stale["marked_unread"] is True
        done = client.post("/v1/bots/" + bot["id"] + "/read", headers=H, json={"revision": 1, "explicit": True}).json()
        assert done["marked_unread"] is False and done["unread_count"] == 0


def test_delete_bot_drops_its_rules_approvals_gate_log_and_usage_notices(settings):
    """Review C2 #4: same transaction as the rest of delete_bot; other bots and global rules stay."""
    store = _store(settings)
    gone, kept = _bot(store, "Ada"), _bot(store, "Bea")
    stamp = "2026-11-14T00:00:00+00:00"
    with closing(sqlite3.connect(store.path)) as db:
        for bot_id in (str(gone.id), str(kept.id), None):
            db.execute("INSERT INTO action_rules (bot_id,effect,kind,pattern,created_at) VALUES (?,?,?,?,?)",
                       (bot_id, "ask", "command", "rm *", stamp))
        for bot_id in (str(gone.id), str(kept.id)):
            db.execute("INSERT INTO approvals (bot_id,job_id,tool,summary,input_hash,scope,source,created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)", (bot_id, "j", "Bash", "s", "h", "once", "trigger", stamp))
            db.execute("INSERT INTO gate_log (bot_id,decision,effect,summary,created_at) VALUES (?,?,?,?,?)",
                       (bot_id, "deny", "deny", "s", stamp))
            db.execute("INSERT INTO usage_notices (job_id,attempt,bot_id,provider,back_at,created_at)"
                       " VALUES (?,?,?,?,?,?)", (f"job-{bot_id}", 1, bot_id, "codex", stamp, stamp))
        db.commit()
    store.delete_bot(gone.id)
    with closing(sqlite3.connect(store.path)) as db:
        for table in ("action_rules", "approvals", "gate_log", "usage_notices"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE bot_id=?", (str(gone.id),)).fetchone()[0] == 0
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE bot_id=?", (str(kept.id),)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM action_rules WHERE bot_id IS NULL").fetchone()[0] == 1
