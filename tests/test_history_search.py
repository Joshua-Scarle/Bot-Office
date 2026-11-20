"""0.12.5: paged chat history (keyset by message id) and message search (FTS5 with a LIKE fallback)."""
import sqlite3
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from botoffice import history
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def desk(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        yield client, app.state.store


def new_bot(client, name="Z"):
    return client.post("/v1/bots", headers=H, json={"name": name}).json()["id"]


def fill(store, bot, n, prefix="msg"):
    return [store.add_message(bot, "user" if i % 2 else "assistant", f"{prefix} {i}").id for i in range(n)]


def page(client, bot, **params):
    r = client.get(f"/v1/bots/{bot}/history", headers=H, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_newest_page_then_walk_back_without_duplicates_or_gaps(desk):
    client, store = desk
    bot = new_bot(client)
    ids = fill(store, bot, 123)
    got = page(client, bot, limit=50)
    assert [m["id"] for m in got["messages"]] == ids[-50:] and got["has_older"] and not got["has_newer"]
    seen = [m["id"] for m in got["messages"]]
    while got["has_older"]:
        got = page(client, bot, limit=50, before=got["messages"][0]["id"])
        assert got["has_newer"]
        seen = [m["id"] for m in got["messages"]] + seen
    assert seen == ids


def test_new_messages_while_paging_neither_duplicate_nor_skip(desk):
    client, store = desk
    bot = new_bot(client)
    ids = fill(store, bot, 60)
    first = page(client, bot, limit=20)
    ids += fill(store, bot, 7, "late")  # arrives between two page requests
    older = page(client, bot, limit=20, before=first["messages"][0]["id"])
    assert older["messages"][-1]["id"] == first["messages"][0]["id"] - 1
    newer = page(client, bot, limit=20, after=first["messages"][-1]["id"])
    assert [m["id"] for m in newer["messages"]] == ids[-7:] and not newer["has_newer"]


def test_after_and_around(desk):
    client, store = desk
    bot = new_bot(client)
    ids = fill(store, bot, 100)
    mid = page(client, bot, limit=20, around=ids[50])
    got = [m["id"] for m in mid["messages"]]
    assert ids[50] in got and got == ids[ids.index(got[0]):ids.index(got[0]) + len(got)]
    assert mid["has_older"] and mid["has_newer"] and 19 <= len(got) <= 21
    tail = page(client, bot, limit=20, around=ids[-1])
    assert tail["messages"][-1]["id"] == ids[-1] and not tail["has_newer"]
    up = page(client, bot, limit=30, after=ids[10])
    assert [m["id"] for m in up["messages"]] == ids[11:41] and up["has_older"] and up["has_newer"]
    assert page(client, bot, limit=5, before=ids[0]) == {"messages": [], "has_older": False, "has_newer": False}


def test_around_edges_and_other_bots_interleaving(desk):
    client, store = desk
    a, b = new_bot(client, "A"), new_bot(client, "B")
    ids = []
    for i in range(30):
        ids.append(store.add_message(a, "user", f"a {i}").id)
        store.add_message(b, "user", f"b {i}")  # ids of the other bot interleave: gaps in a's ids
    got = page(client, a, limit=1, around=ids[10])
    assert [m["id"] for m in got["messages"]] == [ids[10]] and got["has_older"] and got["has_newer"]
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE messages SET archived_at='2026-01-01T00:00:00+00:00' WHERE id=?", (ids[10],))
        db.commit()
    got = page(client, a, limit=6, around=ids[10])  # the target itself is not shown, its neighbours are
    assert ids[10] not in [m["id"] for m in got["messages"]] and ids[9] in [m["id"] for m in got["messages"]]
    assert page(client, a, limit=10, around=10**9)["messages"][-1]["id"] == ids[-1]  # past the end: the tail
    assert page(client, a, limit=10, around=1)["has_older"] is False


def test_cursors_are_exclusive_and_limits_validated(desk):
    client, _ = desk
    bot = new_bot(client)
    assert client.get(f"/v1/bots/{bot}/history", headers=H, params={"before": 3, "after": 2}).status_code == 422
    assert client.get(f"/v1/bots/{bot}/history", headers=H, params={"limit": 0}).status_code == 422
    assert client.get(f"/v1/bots/{bot}/history", headers=H, params={"limit": 501}).status_code == 422
    assert client.get(f"/v1/bots/{bot}/history").status_code in (401, 403)


def test_hidden_and_archived_rows_are_left_out_like_the_full_route(desk):
    client, store = desk
    bot = new_bot(client)
    ids = fill(store, bot, 6)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE messages SET hidden=1 WHERE id=?", (ids[2],))
        db.execute("UPDATE messages SET archived_at='2026-01-01T00:00:00+00:00' WHERE id=?", (ids[4],))
        db.commit()
    full = [m["id"] for m in client.get(f"/v1/bots/{bot}/messages", headers=H).json()]
    assert [m["id"] for m in page(client, bot)["messages"]] == full == [ids[0], ids[1], ids[3], ids[5]]
    assert ids[4] in [m["id"] for m in page(client, bot, archived="true")["messages"]]


def test_pending_placeholder_only_with_the_newest_page(desk, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4
    client, store = desk
    bot = new_bot(client)
    fill(store, bot, 8)
    stamp = datetime.now(UTC)
    job = SimpleNamespace(id=uuid4(), source="chat", draft_text="typing…", started_at=stamp, created_at=stamp,
                          status=SimpleNamespace(value="running"), not_before=None, continue_count=0)
    monkeypatch.setattr(store, "pending_jobs", lambda _bot: [job])
    newest = page(client, bot, limit=3, pending="true")
    assert [m["pending"] for m in newest["messages"]] == [False, False, False, True] and newest["messages"][-1]["id"] < 0
    older = page(client, bot, limit=3, before=newest["messages"][0]["id"], pending="true")
    assert not any(m["pending"] for m in older["messages"])
    mid = page(client, bot, limit=3, around=newest["messages"][0]["id"], pending="true")
    assert not any(m["pending"] for m in mid["messages"])  # a window that is not the end of the chat has none
    assert not any(m["pending"] for m in page(client, bot, limit=3)["messages"])  # not asked for


def test_unpaged_messages_route_is_unchanged(desk):
    client, store = desk
    bot = new_bot(client)
    fill(store, bot, 80)
    assert len(client.get(f"/v1/bots/{bot}/messages", headers=H).json()) == 80


# ---- search ----

def search(client, q, **params):
    r = client.get("/v1/search/messages", headers=H, params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_search_one_bot_and_all_bots_with_snippet_markers(desk):
    client, store = desk
    a, b = new_bot(client, "Alpha"), new_bot(client, "Beta")
    m1 = store.add_message(a, "user", "please review the quarterly invoice totals today")
    m2 = store.add_message(b, "assistant", "The invoice was sent; invoices are filed in /srv/Invoices")
    store.add_message(a, "user", "nothing relevant here")
    both = search(client, "invoice")
    assert [h["id"] for h in both["hits"]] == [m2.id, m1.id] and not both["has_more"]  # newest first
    assert both["hits"][0]["bot_name"] == "Beta"
    assert "\x02invoice\x03" in both["hits"][1]["snippet"]
    only_a = search(client, "invoice", bot=a)
    assert [h["id"] for h in only_a["hits"]] == [m1.id]
    assert search(client, "invoi")["hits"]  # prefix match on the last word, as you type
    assert search(client, "quarterly totals")["hits"][0]["id"] == m1.id  # words AND, any order


def test_search_skips_archived_hidden_and_deleted(desk):
    client, store = desk
    bot = new_bot(client)
    keep = store.add_message(bot, "user", "zebra stripes").id
    gone = store.add_message(bot, "user", "zebra archived").id
    hid = store.add_message(bot, "user", "zebra hidden").id
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE messages SET archived_at='2026-01-01T00:00:00+00:00' WHERE id=?", (gone,))
        db.execute("UPDATE messages SET hidden=1 WHERE id=?", (hid,))
        db.commit()
    assert [h["id"] for h in search(client, "zebra")["hits"]] == [keep]
    assert client.delete(f"/v1/bots/{bot}", headers=H).status_code in (200, 204)
    assert search(client, "zebra")["hits"] == []


def test_search_follows_text_updates_and_diacritics(desk):
    client, store = desk
    bot = new_bot(client)
    mid = store.add_message(bot, "assistant", "a café menu").id
    assert search(client, "cafe")["hits"][0]["id"] == mid
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE messages SET text='a tea menu' WHERE id=?", (mid,))
        db.commit()
    assert search(client, "cafe")["hits"] == [] and search(client, "tea")["hits"][0]["id"] == mid


def test_search_paging_by_before_and_hostile_queries(desk):
    client, store = desk
    bot = new_bot(client)
    ids = [store.add_message(bot, "user", f"needle number {i}").id for i in range(35)]
    first = search(client, "needle", limit=30)
    assert len(first["hits"]) == 30 and first["has_more"]
    rest = search(client, "needle", limit=30, before=first["hits"][-1]["id"])
    assert [h["id"] for h in first["hits"] + rest["hits"]] == ids[::-1] and not rest["has_more"]
    for q in ('"', "needle AND", "needle OR NOT (", "*", "a:b", "NEAR(", "\\", "%", "' OR 1=1 --", "!!!"):
        assert client.get("/v1/search/messages", headers=H, params={"q": q}).status_code == 200
    assert search(client, "!!!")["hits"] == []
    assert client.get("/v1/search/messages", headers=H, params={"q": ""}).status_code == 422
    assert client.get("/v1/search/messages", headers=H, params={"q": "x", "bot": "00000000-0000-0000-0000-000000000000"}).status_code == 404


def test_like_fallback_has_the_same_shape(desk):
    client, store = desk
    bot = new_bot(client)
    mid = store.add_message(bot, "user", "100% sure the Invoice_total is wrong " + "x" * 300).id
    with closing(sqlite3.connect(store.path)) as db:
        for t in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
            db.execute(f"DROP TRIGGER {t}")
        db.execute("DROP TABLE messages_fts")
        db.commit()
    hit = search(client, "invoice_total")["hits"][0]
    assert hit["id"] == mid and "\x02Invoice_total\x03" in hit["snippet"] and len(hit["snippet"]) < 260
    assert search(client, "100%")["hits"][0]["id"] == mid
    assert search(client, "zzz")["hits"] == []
    store.add_message(bot, "user", "still inserts fine without the index")


def test_migration_backfills_existing_messages_once_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = JobStore(path)
    # the schema exists; seed an "old" database by dropping the index
    with closing(sqlite3.connect(path)) as db:
        for t in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
            db.execute(f"DROP TRIGGER {t}")
        db.execute("DROP TABLE messages_fts")
        db.execute("DELETE FROM meta WHERE key=?", (history.FTS_MIGRATION,))
        db.executemany("INSERT INTO messages (bot_id,role,source,text,created_at) VALUES ('b','user','chat',?,'2026-01-01T00:00:00+00:00')",
                       [(f"history row {i} walrus",) for i in range(2000)])
        db.commit()
    started = time.perf_counter()
    JobStore(path).initialize()
    assert time.perf_counter() - started < 5
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'walrus'").fetchone()[0] == 2000
        assert db.execute("SELECT 1 FROM meta WHERE key=?", (history.FTS_MIGRATION,)).fetchone()
    JobStore(path).initialize()  # second start: nothing to do
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'walrus'").fetchone()[0] == 2000
        db.execute("INSERT INTO messages (bot_id,role,source,text,created_at) VALUES ('b','user','chat','new walrus','2026-01-01T00:00:00+00:00')")
        assert db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'walrus'").fetchone()[0] == 2001
