import json
from contextlib import closing

import pytest

from botoffice import team

from teamkit import MASTER, Desk, add_message, put_section, run_job, set_bot


def row(**kw):
    return {"id": kw.pop("id"), "team_read": kw.pop("team_read", "section"), "team_private": kw.pop("team_private", 0)}


@pytest.mark.parametrize("mode,mine,theirs,private,expected", [
    ("section", "s1", "s1", 0, True),    # same section
    ("section", "s1", "s2", 0, False),   # different sections
    ("section", None, "s2", 0, True),    # reader has no section
    ("section", "s1", None, 0, True),    # target has no section
    ("section", None, None, 0, True),    # nobody is sectioned
    ("all", "s1", "s2", 0, True),
    ("none", "s1", "s1", 0, False),      # none reads nobody
    ("none", None, None, 0, False),
    ("all", "s1", "s1", 1, False),       # private is never readable, even with all
    ("section", None, None, 1, False),
])
def test_can_read_matrix(mode, mine, theirs, private, expected):
    sections = {k: v for k, v in (("r", mine), ("t", theirs)) if v}
    assert team.can_read(row(id="r", team_read=mode), row(id="t", team_private=private), sections) is expected


def test_can_read_never_oneself():
    assert team.can_read(row(id="r", team_read="all"), row(id="r"), {}) is False


def test_http_acl_alias_deleted_private_and_master(settings):
    with Desk(settings) as d:
        reader, mate, other, loose, secret = (d.bot(n) for n in ("Reader", "Mate", "Other", "Loose", "Secret"))
        put_section(d.store, "A", reader, mate)
        put_section(d.store, "B", other)
        set_bot(d.store, secret, team_private=1)
        _, h = run_job(d.store, reader)
        get = lambda ref, tail="status": d.client.get(f"/v1/agent/team/{ref}/{tail}", headers=h)  # noqa: E731

        assert get("Mate").status_code == 200
        assert get(str(mate.id)).status_code == 200
        assert get("Loose").status_code == 200          # unsectioned bots are readable
        assert get("Other").status_code == 403          # another section
        assert get("Secret").status_code == 403         # private
        assert get("Secret", "transcript").status_code == 403
        assert get("Reader").status_code == 404         # yourself
        assert get("Nobody").status_code == 404
        # aliases resolve to the bot and are checked on that bot, never around the ACL
        with closing(d.store._connect()) as db:
            db.execute("INSERT INTO bot_aliases VALUES ('old-secret', ?)", (str(secret.id),))
            db.execute("INSERT INTO bot_aliases VALUES ('old-other', ?)", (str(other.id),))
            db.execute("INSERT INTO bot_aliases VALUES ('old-mate', ?)", (str(mate.id),))
            db.execute("INSERT INTO bot_aliases VALUES ('ghost', 'deleted-bot-id')")
            db.execute("INSERT INTO bot_aliases VALUES ('Mate2', ?)", (str(other.id),))
        assert get("old-secret").status_code == 403
        assert get("old-other").status_code == 403
        assert get("old-mate").status_code == 200
        assert get("ghost").status_code == 404          # alias of a deleted bot
        # a name wins over an alias of the same text (store.find_bot rules): "Other" stays Other
        assert get("Mate2").status_code == 403
        # a deleted bot is gone
        with closing(d.store._connect()) as db:
            db.execute("DELETE FROM bots WHERE id=?", (str(mate.id),))
        assert get("Mate").status_code == 404
        assert get("old-mate").status_code == 404
        # the master token and no token never reach these routes
        assert d.client.get("/v1/agent/team", headers=MASTER).status_code == 401
        assert d.client.get("/v1/agent/team/Loose/status", headers=MASTER).status_code == 401
        assert d.client.get("/v1/agent/team").status_code == 401
        assert d.client.post("/v1/agent/team/ask", headers=MASTER, json={"to": "Loose", "body": "x"}).status_code == 401


def test_team_read_modes_and_list(settings):
    with Desk(settings) as d:
        reader, mate, other, secret = (d.bot(n) for n in ("Reader", "Mate", "Other", "Secret"))
        put_section(d.store, "A", reader, mate)
        put_section(d.store, "B", other)
        set_bot(d.store, secret, team_private=1)
        _, h = run_job(d.store, reader)
        listing = {b["name"]: b for b in d.client.get("/v1/agent/team", headers=h).json()["bots"]}
        assert "Reader" not in listing and listing["Mate"]["readable"] is True
        assert listing["Mate"]["state"] == "idle" and listing["Mate"]["open_tasks"] == 0
        assert listing["Mate"]["section"] == "A"
        assert listing["Other"]["readable"] is False and "state" not in listing["Other"]
        assert listing["Secret"]["private"] is True and "state" not in listing["Secret"]  # messageable, no status
        set_bot(d.store, reader, team_read="all")
        assert d.client.get("/v1/agent/team/Other/status", headers=h).status_code == 200
        assert d.client.get("/v1/agent/team/Secret/status", headers=h).status_code == 403
        set_bot(d.store, reader, team_read="none")
        assert d.client.get("/v1/agent/team/Mate/status", headers=h).status_code == 403
        assert d.client.get("/v1/agent/team", headers=h).json()["team_read"] == "none"


def test_untrusted_marking_caps_and_retention(settings):
    with Desk(settings) as d:
        reader, mate = d.bot("Reader"), d.bot("Mate")
        _, h = run_job(d.store, reader)
        for i in range(60):
            add_message(d.store, mate, "assistant" if i % 2 else "user", f"m{i} " + "x" * 3000)
        add_message(d.store, mate, "user", "ARCHIVED", archived_at="2026-11-21T00:00:00+00:00")
        add_message(d.store, mate, "user", "MACHINERY", hidden=1)
        add_message(d.store, mate, "user", "WITH-FILE", attachments=json.dumps(["a.png"]))
        out = d.client.get("/v1/agent/team/Mate/transcript?limit=50", headers=h).json()
        assert out["untrusted"] is True
        assert sum(len(m["text"]) for m in out["messages"]) <= team.TRANSCRIPT_CHARS
        assert len(out["messages"]) < 50  # the budget, not the limit, cut it
        texts = " ".join(m["text"] for m in out["messages"])
        assert "ARCHIVED" not in texts and "MACHINERY" not in texts
        assert "attachments" not in out["messages"][0] and out["messages"][-1]["text"].startswith("WITH-FILE")
        assert d.client.get("/v1/agent/team/Mate/transcript?limit=51", headers=h).status_code == 422
        assert d.client.get("/v1/agent/team/Mate/transcript?since=garbage", headers=h).status_code == 400
        newest = out["messages"][-1]["id"]
        assert d.client.get(f"/v1/agent/team/Mate/transcript?since={newest - 1}", headers=h).json()["count"] == 1
        assert d.client.get("/v1/agent/team/Mate/transcript?since=2999-01-01T00:00:00Z", headers=h).json()["count"] == 0


def test_retained_messages_is_a_pure_reusable_reader(settings):
    with Desk(settings) as d:
        mate = d.bot("Mate")
        for i in range(5):
            add_message(d.store, mate, "user", f"q{i}")
        with closing(d.store._connect()) as db:
            got = team.retained_messages(db, mate.id, limit=3)
            assert [m["text"] for m in got] == ["q2", "q3", "q4"]  # newest limit, oldest first
            assert team.retained_messages(db, mate.id, limit=99, max_chars=4)[-1]["text"] == "q4"
            assert len(team.retained_messages(db, mate.id, limit=99, max_chars=4)) == 2


def test_status_content_and_read_audit(settings):
    with Desk(settings) as d:
        reader, mate = d.bot("Reader"), d.bot("Mate")
        job, h = run_job(d.store, reader)
        add_message(d.store, mate, "assistant", "first")
        for i in range(3):
            add_message(d.store, mate, "assistant", f"reply{i} " + "y" * 500)
        add_message(d.store, mate, "assistant", "progress", kind="say")
        with closing(d.store._connect()) as db:
            db.execute("INSERT INTO assignments (id,bot_id,title,status,status_note,created_at,updated_at) VALUES "
                       "('a1',?, 'Review draft','waiting_on_bot','waiting on Scout','2026-11-21','2026-11-21')", (str(mate.id),))
            db.execute("INSERT INTO questions (id,bot_id,message_id,payload,status,blocking,created_at) VALUES "
                       "('q1',?,1,'{}','open',1,'2026-11-21')", (str(mate.id),))
        status = d.client.get("/v1/agent/team/Mate/status", headers=h).json()
        assert status["untrusted"] is True and status["state"] == "idle" and status["open_questions"] == 1
        assert {"title": "Review draft", "status": "waiting_on_bot", "waiting_on": "waiting on Scout"} in status["tasks"]
        assert len(status["last_replies"]) == 3 and all(len(r["text"]) <= 300 for r in status["last_replies"])
        assert not any(r["text"].startswith("progress") for r in status["last_replies"])
        d.client.get("/v1/agent/team/Mate/transcript", headers=h)
        with closing(d.store._connect()) as db:
            reads = [tuple(r) for r in db.execute("SELECT kind, count FROM team_reads WHERE job_id=? ORDER BY id", (str(job.id),))]
            events = [r["kind"] for r in db.execute("SELECT kind FROM job_events WHERE job_id=?", (str(job.id),))]
        assert reads[0] == ("team_status", 1) and reads[1][0] == "team_read"
        assert "team_status" in events and "team_read" in events


def test_patch_exposes_team_settings_and_seed_is_a_data_flag(settings):
    with Desk(settings) as d:
        bot = d.bot("Plain")
        got = d.client.patch(f"/v1/bots/{bot.id}", headers=MASTER, json={"team_read": "all", "team_private": True}).json()
        assert got["team_read"] == "all" and got["team_private"] is True
        assert d.client.patch(f"/v1/bots/{bot.id}", headers=MASTER, json={"team_read": "everyone"}).status_code == 422
        assert d.client.get("/v1/bots", headers=MASTER).json()[0]["team_read"] == "all"
        fresh = d.bot("Fresh")
        assert fresh.team_read == "section" and fresh.team_private is False  # defaults leave other bots alone
        printer = d.bot("Printer Writing Bot")
        with closing(d.store._connect()) as db:
            db.execute("DELETE FROM meta WHERE key='team.private_seeded'")
            before = dict(db.execute("SELECT * FROM bots WHERE id=?", (str(printer.id),)).fetchone())
        from botoffice.store import JobStore
        JobStore(d.store.path).initialize()  # a restart: the migration runs on the existing database
        with closing(d.store._connect()) as db:
            after = dict(db.execute("SELECT * FROM bots WHERE id=?", (str(printer.id),)).fetchone())
            others = [r[0] for r in db.execute("SELECT team_private FROM bots WHERE name<>'Printer Writing Bot' AND name<>'Plain'")]
        assert {k: v for k, v in after.items() if v != before[k]} == {"team_private": 1}  # nothing else changed
        assert after["cli_access"] == before["cli_access"] and others == [0]
        JobStore(d.store.path).initialize()  # idempotent: a later flag the owner cleared stays cleared
        with closing(d.store._connect()) as db:
            db.execute('UPDATE bots SET team_private=0 WHERE id=?', (str(printer.id),))
        JobStore(d.store.path).initialize()
        with closing(d.store._connect()) as db:
            assert db.execute('SELECT team_private FROM bots WHERE id=?', (str(printer.id),)).fetchone()[0] == 0
