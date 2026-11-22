"""0.12.24: memory is a snapshot. Notes carry their age and source, superseded/expired notes are flagged,
the tags never reach the stored files, bots are told the time and how long since their last turn, and
they can check approval cards live."""
import asyncio
import json
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from botoffice import freshness
from botoffice.api import create_app
from botoffice.baseline import SYSTEM
from botoffice.compaction import COMPACT_POLICY
from botoffice.memory import Memory
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import prepare_bot_workspace

NOW = datetime(2026, 11, 22, 21, 0, tzinfo=UTC)
MEMORY = """# Memory

- DEMO_ITEM_LIMIT is 1500
  (from the box handoff)
- Bot-Office runs 0.11.5

```
code stays untagged
```
"""


def test_notes_split_on_bullets_and_skip_headings_and_fences():
    found = [(first, taggable) for first, _, taggable in freshness.notes(MEMORY)]
    assert found == [(0, False), (2, True), (4, True), (6, False)]


def test_annotate_strip_round_trip_and_ages(tmp_path):
    memory = Memory(tmp_path)
    memory.write_bot("b1", MEMORY, who="Ada")
    path = memory.bot_path("b1")
    shown = freshness.annotate(MEMORY, path, NOW + timedelta(days=20), "Ada")
    assert shown.startswith(freshness.HEADER_START)
    assert "- DEMO_ITEM_LIMIT is 1500 ⟦" in shown and "· you⟧" in shown and "OLD" in shown
    assert "code stays untagged\n" in shown and "# Memory\n" in shown
    assert freshness.strip(shown) == MEMORY
    # another writer's note is attributed by name, a fresh one is not OLD
    memory.write_bot("b1", MEMORY + "- new fact\n", who="owner")
    shown = freshness.annotate(memory.read_bot("b1"), path, datetime.now(UTC), "Ada")
    assert "- new fact ⟦" in shown and "0d · owner⟧" in shown


def test_unchanged_notes_keep_their_date_and_edits_redate(tmp_path):
    memory = Memory(tmp_path)
    path = memory.bot_path("b1")
    memory.write_bot("b1", "- a\n- b\n", who="Ada")
    ledger = freshness.load(path)
    for entry in ledger.values():
        entry["at"] = (NOW - timedelta(days=30)).isoformat()
    (freshness.ledger_path(path)).write_text(json.dumps({"v": 1, "notes": ledger}))
    memory.write_bot("b1", "- a\n- b verified 2026-11-22\n", who="Ada")
    shown = freshness.annotate(memory.read_bot("b1"), path, NOW, "Ada").split("\n")
    assert "30d OLD" in shown[1] and "OLD" not in shown[2]


def test_untracked_notes_are_dated_on_or_before_the_file_time(tmp_path):
    path = tmp_path / "bots" / "b1" / "memory.md"
    path.parent.mkdir(parents=True)
    path.write_text("- legacy fact\n")
    old = (NOW - timedelta(days=6)).timestamp()
    os.utime(path, (old, old))
    shown = freshness.annotate("- legacy fact\n", path, NOW, "Ada")
    assert "⟦≤Nov 16 · ≥6d · source unknown⟧" in shown
    # the first tracked write keeps the old note's upper bound, not "now"
    Memory(tmp_path).write_bot("b1", "- legacy fact\n- new\n", who="Ada")
    entry = freshness.load(path)[freshness.keys("- legacy fact\n")[0]]
    assert entry["upper"] is True and entry["at"].startswith("2026-11-16")


def test_superseded_and_expired_notes_are_flagged(tmp_path):
    text = "- SUPERSEDED 2026-11-20: limit was 1193\n- ~~old owner: Ada~~\n- freeze (expires 2026-11-01)\n- live (expires 2027-01-01)\n"
    shown = freshness.annotate(text, tmp_path / "memory.md", NOW, "Ada").split("\n")[1:]
    assert "SUPERSEDED⟧" in shown[0] and "SUPERSEDED⟧" in shown[1]
    assert "EXPIRED⟧" in shown[2] and "EXPIRED" not in shown[3]


def _setup(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    return store, scheduler, bot


def test_workspace_shows_tagged_files_and_unchanged_files_are_not_rewritten(settings):
    store, scheduler, bot = _setup(settings)
    scheduler.memory.write_bot(bot.id, "- version 0.11.5\n", who="Ada")
    scheduler.memory.write_shared("- product status as of Oct 25\n")
    scheduler.memory.write_working(bot.id, "- next: merge\n", who="Ada")
    _, job = scheduler.turns.post_message(bot, "what version runs?")
    seen = {}

    class Reads(FakeAdapter):
        async def run(self, job, ws, log_path, timeout):
            for name in ("memory.md", "shared-memory.md", "WORKING.md", "prompt.txt", "prompt-resume.txt", "SELF.md"):
                seen[name] = (ws / name).read_text()
            # the bot edits one note, keeping the tags of the others
            (ws / "memory.md").write_text(seen["memory.md"].replace("0.11.5", "0.12.24"))
            return await super().run(job, ws, log_path, timeout)

    scheduler.providers["codex"] = Reads()
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    assert "- version 0.11.5 ⟦" in seen["memory.md"] and "· you⟧" in seen["memory.md"]
    assert "- product status as of Oct 25 ⟦" in seen["shared-memory.md"] and "· owner⟧" in seen["shared-memory.md"]
    assert "- next: merge ⟦" in seen["WORKING.md"] and "- next: merge ⟦" in seen["prompt.txt"]
    assert "last saved" in seen["prompt.txt"] and "\nNow: " in seen["prompt.txt"] and "\nNow: " in seen["prompt-resume.txt"]
    assert "Your approval cards pending now (live" in seen["SELF.md"]
    # stored files never carry the tags; the edit was merged and re-dated
    assert scheduler.memory.read_bot(bot.id) == "- version 0.12.24\n"
    assert scheduler.memory.read_shared() == "- product status as of Oct 25\n"
    assert scheduler.memory.read_working(bot.id) == "- next: merge\n"


def test_resume_prompt_warns_after_a_long_gap(settings):
    store, scheduler, bot = _setup(settings)
    _, first = scheduler.turns.post_message(bot, "first")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    with closing(store._connect()) as db:
        old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        db.execute("UPDATE jobs SET started_at=?, finished_at=? WHERE id=?", (old, old, str(first.id)))
        db.commit()
    _, second = scheduler.turns.post_message(bot, "status?")
    ws = settings.data_dir / "workspaces" / str(second.id)
    ws.mkdir(parents=True)
    prepare_bot_workspace(store, scheduler.memory, bot, store.get(second.id), ws)
    resume = (ws / "prompt-resume.txt").read_text()
    assert "Your previous turn ended 3d ago" in resume and "may be out of date" in resume


def test_prompts_say_memory_is_a_snapshot():
    text = SYSTEM.default_text()
    assert "dated snapshots" in text and "check the live source" in text and "SUPERSEDED" in text
    assert "`bot-task tasks|approvals`" in text
    assert "as of <date time ET>" in COMPACT_POLICY


def test_agent_can_check_approval_cards_live(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    app = create_app(settings, store=store, providers={"codex": FakeAdapter()})
    with TestClient(app) as client:
        ada = store.create_bot("Ada", "", "codex", None)
        bob = store.create_bot("Bob", "", "codex", None)
        store.create("work", "codex", bot_id=ada.id, source="chat")
        job = store.claim(["desktop-1"], {"codex"})
        stamp = datetime.now(UTC).isoformat()
        with closing(store._connect()) as db:
            for bot, status in ((ada, "pending"), (bob, "approved")):
                db.execute("INSERT INTO approvals (bot_id, job_id, tool, summary, input_hash, scope, source, status, "
                           "created_at, decided_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (str(bot.id), str(job.id), "Bash", f"{bot.name} secret-ish command", "h", "once", "gate",
                            status, stamp, stamp if status != "pending" else None,
                            (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
            db.commit()
            ids = [row[0] for row in db.execute("SELECT id FROM approvals ORDER BY id")]
        token = store.issue_agent_token(job)
        headers = {"Authorization": f"Bearer {token}"}
        mine = client.get("/v1/agent/approvals", headers=headers).json()
        assert [(item["id"], item["status"], item.get("mine")) for item in mine] == [(ids[0], "pending", True)]
        assert "Ada" in mine[0]["summary"]
        both = client.get("/v1/agent/approvals", params={"ids": f"#{ids[1]} 999"}, headers=headers).json()
        assert both[0] == {"id": ids[1], "status": "approved"}  # another bot's card: status only
        assert both[1] == {"id": 999, "status": "not_found"}
        with closing(store._connect()) as db:
            db.execute("UPDATE bots SET team_private=1 WHERE id=?", (str(bob.id),))
            db.commit()
        hidden = client.get("/v1/agent/approvals", params={"ids": str(ids[1])}, headers=headers).json()
        assert hidden == [{"id": ids[1], "status": "not_found"}]  # a bot it may not read: no such card
        assert client.get("/v1/agent/approvals", params={"ids": str(2**64)}, headers=headers).status_code == 400
        assert client.get("/v1/agent/approvals", params={"ids": "²"}, headers=headers).status_code == 400


def test_backfill_dates_notes_from_edit_events_then_stated_dates(tmp_path):
    from botoffice import safefs

    safefs.add_root(tmp_path)
    path = tmp_path / "memory.md"
    path.write_text("### Status (as of 25 October 2026)\n\n- Demo is in public beta\n- limit is 1500 now\n- plain fact\n")
    edit = (datetime(2026, 11, 21, 23, 0, tzinfo=UTC), "Lead Bot", "@@ -1 +1 @@\n+- limit is 1500 now\n")
    assert freshness.backfill(path, [edit], now=NOW) == 4
    shown = freshness.annotate(path.read_text(), path, NOW, "Other Lead").split("\n")
    assert "⟦~Oct 25 · ~28d OLD · date in note⟧" in shown[3]
    assert "⟦Nov 21 · 0d · Lead Bot⟧" in shown[4]
    assert "~Oct 25" in shown[5]  # inherits its heading's date
    assert freshness.backfill(path, [], now=NOW) == 0  # a ledger exists: left alone


def test_strip_never_removes_text_the_file_really_has(tmp_path):
    # the file's own ⟦…⟧ (code, quotes): shown untagged and returned exactly as written
    own = "- a note\n```\nx ⟦Nov 21 · 1d · you⟧\n```\n"
    assert freshness.annotate(own, tmp_path / "memory.md", NOW) == own
    assert freshness.strip(own, own) == own
    # a tag-like text in the middle of a line is not one of ours
    text = "- see ⟦Nov 21 · 1d · you⟧ mid-line\n"
    assert freshness.strip(text) == text


def test_removed_note_keeps_its_date_when_restored(tmp_path):
    memory = Memory(tmp_path)
    path = memory.working_path("b1")
    memory.write_working("b1", "- plan A\n", who="Ada")
    ledger = freshness.load(path)
    for entry in ledger.values():
        entry["at"] = (NOW - timedelta(days=20)).isoformat()
    freshness._save(path, ledger)
    memory.write_working("b1", "- plan B\n", who="Ada")
    memory.write_working("b1", "- plan A\n", who="")  # a rewind restores the old text
    assert "20d OLD · you" in freshness.annotate("- plan A\n", path, NOW, "Ada")


def test_large_memory_read_back_is_not_truncated_by_the_tags(settings):
    store, scheduler, bot = _setup(settings)
    from botoffice.memory import MAX_MEMORY_BYTES
    big = "".join(f"- note {n} " + "x" * 60 + "\n" for n in range(MAX_MEMORY_BYTES // 80))
    scheduler.memory.write_bot(bot.id, big, who="Ada")
    _, job = scheduler.turns.post_message(bot, "hi")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    assert scheduler.memory.read_bot(bot.id) == big


def test_strip_handles_trailing_spaces_and_leaves_fences_alone():
    assert freshness.strip("- a ⟦Nov 21 · 1d · you⟧  \n") == "- a  \n"
    fenced = "```\nexample ⟦Nov 21 · 1d · you⟧\n```\n"
    assert freshness.strip(fenced) == fenced


def test_too_large_to_tag_is_shown_as_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, "MAX_TAGGED", 50)
    text = "- one\n- two\n- three\n- four\n"
    assert freshness.annotate(text, tmp_path / "memory.md", NOW) == text
