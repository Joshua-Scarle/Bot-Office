"""0.11.10: guards around the one continuation system (owner messages, holds, wake-ups, the decision
marker, migrations, settings and the rollback statement)."""
import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_auto_continue import _queued as chat_queued
from test_auto_continue import _run_all as chat_run_all
from test_auto_continue import _run_one as chat_run_one
from test_auto_continue import _setup as chat_setup
from test_continuation import OWNER, fresh, run_all, save, system_lines, task_queued, task_setup

from botoffice import approval_classes, continuation
from botoffice import store as store_module
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore, now

pytestmark = pytest.mark.continuation_defaults
ROOT = Path(__file__).resolve().parents[1]


def job_row(store, job_id):
    with closing(store._connect()) as db:
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (str(job_id),)).fetchone())


# --- the owner's message is never attached to an automatic turn ---

@pytest.mark.parametrize("held", [True, False])
def test_an_owner_message_is_its_own_turn_not_a_queued_continuation(settings, held):
    """Through the HTTP owner-message route: a queued chat continuation (held by a cap or not) is not
    steered into; the owner's message becomes its own job and supersedes the continuation."""
    store, scheduler, _, bot = chat_setup(settings, fresh(20, "not_done"))
    if held:
        save(store, per_bot_day=1)
        chat_run_all(store, scheduler)
    else:
        chat_run_one(store, scheduler)
    [waiting] = chat_queued(store, bot)
    assert waiting["auto_kind"] == "chat"
    assert (waiting["failure_kind"] == continuation.HELD) is held
    client = TestClient(create_app(settings, {"codex": FakeAdapter()}, store=store))  # no lifespan: nothing runs
    posted = client.post(f"/v1/bots/{bot.id}/messages", headers=OWNER, json={"text": "here is what you needed"})
    assert posted.status_code == 201
    body = posted.json()
    assert body["delivery"] != "steer" and body["job"]["id"] != waiting["id"]
    assert [q["id"] for q in chat_queued(store, bot)] == [body["job"]["id"]]
    assert job_row(store, waiting["id"])["status"] == "cancelled"
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE steer_job_id=?", (waiting["id"],)).fetchone()[0] == 0


def test_an_owner_message_still_steers_into_the_owners_own_queued_turn(settings):
    store, _scheduler, _, bot = chat_setup(settings, fresh(2, "done"))  # the owner's first turn is queued
    [mine] = chat_queued(store, bot)
    client = TestClient(create_app(settings, {"codex": FakeAdapter()}, store=store))
    body = client.post(f"/v1/bots/{bot.id}/messages", headers=OWNER, json={"text": "and one more thing"}).json()
    assert body["delivery"] == "steer" and body["job"]["id"] == mine["id"]


# --- holds ---

def test_a_cap_hold_keeps_the_jobs_failure_kind_and_counts_no_retry(settings):
    store, scheduler, _, bot = chat_setup(settings, fresh(20, "not_done"))
    save(store, per_bot_day=1)
    chat_run_all(store, scheduler)
    [held] = chat_queued(store, bot)
    # as if the continuation had been parked after a timed-out attempt when the cap held it
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET failure_kind='timeout', attempts=1, not_before=NULL, held_kind=NULL WHERE id=?",
                   (held["id"],))
        attempts_before = db.execute("SELECT COUNT(*) FROM job_attempts WHERE job_id=?", (held["id"],)).fetchone()[0]
        lines_before = db.execute("SELECT COUNT(*) FROM messages WHERE job_id=? AND text LIKE 'Automatic work paused%'",
                                  (held["id"],)).fetchone()[0]
    for _ in range(2):  # held, released by hand, held again
        assert store.claim(("desktop-1",), {"codex"}) is None
        row = job_row(store, held["id"])
        assert (row["failure_kind"], row["held_kind"], row["attempts"]) == (continuation.HELD, "timeout", 1)
        continuation.release_held(store)
    save(store, per_bot_day=None)
    claimed = store.claim(("desktop-1",), {"codex"})
    assert str(claimed.id) == held["id"]
    row = job_row(store, held["id"])
    assert (row["status"], row["failure_kind"], row["held_kind"], row["attempts"]) == ("running", "timeout", None, 1)
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM job_attempts WHERE job_id=?", (held["id"],)).fetchone()[0] == attempts_before
        lines = db.execute("SELECT COUNT(*) FROM messages WHERE job_id=? AND text LIKE 'Automatic work paused%'",
                           (held["id"],)).fetchone()[0]
    assert lines == lines_before + 1  # one line for this hold, however often it is held again


def test_a_held_task_turn_is_dropped_when_the_owner_wrote_since(settings):
    store, scheduler, _, bot, item = task_setup(settings, *fresh(20))
    save(store, per_bot_day=1)
    run_all(store, scheduler)
    [held] = task_queued(store, item)
    assert held.failure_kind == continuation.HELD
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET held_at=? WHERE id=?", ((now() - timedelta(minutes=5)).isoformat(), str(held.id)))
    store.add_message(bot.id, "user", "I changed my mind about the report")  # the owner writes meanwhile
    continuation.release_held(store)
    assert store.claim(("desktop-1",), {"codex"}) is None
    assert job_row(store, held.id)["status"] == "cancelled"
    assert system_lines(store, bot)[-1] == ("A held automatic turn was not run: you wrote since it was held, and "
                                            "your message comes first. Reply to keep it going.")
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"


def test_an_evidence_ask_is_asked_again_after_the_owner_wrote(settings):
    holder = {"turns": 0}

    def reply(job):
        holder["turns"] += 1
        if holder["turns"] == 2:  # the evidence turn: the owner writes while it runs
            holder["store"].add_message(job.bot_id, "user", "the printer is back, check it on that")
        return "Finished.\nBOT-OFFICE-STATUS: done"

    store, scheduler, _adapter, _bot, item = task_setup(settings, reply)
    holder["store"] = store
    jobs = run_all(store, scheduler)
    # done, ask, (owner wrote) done, ask again, done: then it waits
    assert len(jobs) == 3
    assert [job_row(store, job.id)["auto_kind"] for job in jobs] == [None, "evidence", "evidence"]
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"


# --- the decision marker ---

class Died(BaseException):
    """The process dies (not an error the scheduler catches)."""


def test_the_decision_marker_lands_with_the_continuation(settings, monkeypatch):
    store, scheduler, _, bot = chat_setup(settings, fresh(5, "not_done"))
    monkeypatch.setattr(scheduler, "_auto_continue", lambda job: "later")
    first = chat_run_one(store, scheduler)
    monkeypatch.delattr(scheduler, "_auto_continue")

    def died(*args, **kwargs):
        raise Died

    monkeypatch.setattr(scheduler.turns, "post_message", died)
    with pytest.raises(Died):
        scheduler._auto_continue(first)
    # not marked decided while nothing is queued: recovery at start sees it
    assert job_row(store, first.id)["auto_decision"] == "deciding" and chat_queued(store, bot) == []
    assert store.fail_stale_decisions("Could not decide.") == 1
    assert job_row(store, first.id)["auto_decision"] == "error"
    # the normal path: the marker and the queued continuation together
    monkeypatch.delattr(scheduler.turns, "post_message")
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET auto_decision=NULL WHERE id=?", (str(first.id),))
    seen = []
    real = store.enqueue_message

    def spy(*args, **kwargs):
        hook = kwargs.get("hook")

        def checked(db, job):
            hook(db, job)
            seen.append(db.execute("SELECT auto_decision FROM jobs WHERE id=?", (str(first.id),)).fetchone()[0])
        kwargs["hook"] = checked if hook else None
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "enqueue_message", spy)
    assert scheduler._auto_continue(first) == "continued"
    assert seen == ["continue"] and len(chat_queued(store, bot)) == 1


def test_a_stop_decision_and_its_line_are_one_write(settings):
    store, scheduler, _, bot = chat_setup(settings, fresh(3, "not_done"))
    save(store, enabled=False)
    first = chat_run_one(store, scheduler)
    assert job_row(store, first.id)["auto_decision"] == "off"
    assert "auto-continue is off" in system_lines(store, bot)[-1]


# --- wake-ups are never dropped silently ---

def test_a_wakeup_dropped_while_auto_continue_is_off_says_so_in_the_chat(settings):
    store, scheduler, _, bot, _item = task_setup(settings, *fresh(1, "blocked"))
    book = scheduler.assignments
    run_all(store, scheduler)
    watched = book.create(bot.id, {"title": "watched", "start": False})
    on_change = book.add_wakeup(bot.id, None, on_assignment=watched.id, note="watch the other task")
    timed = book.add_wakeup(bot.id, None, due_at=now() + timedelta(hours=1), note="ping")
    save(store, enabled=False)
    book.start(watched.id)  # the status change fires the on-change wake-up
    with closing(store._connect()) as db:
        db.execute("UPDATE wakeups SET due_at=? WHERE id=?", ((now() - timedelta(minutes=1)).isoformat(), str(timed.id)))
    assert book.fire_due() == 0
    visible = [m.text for m in store.list_messages(bot.id) if m.role == "system"]  # what the chat shows
    for wake, note in ((on_change, "watch the other task"), (timed, "ping")):
        assert book._wakeup(wake.id).status == "cancelled"
        assert any(text.startswith(f'Wake-up ("{note}") skipped: auto-continue is off') for text in visible)
    assert not [j for j in store.list() if j.source == "wakeup"]


# --- settings, GUI, migrations, rollback ---

def test_the_pattern_self_test_runs_off_the_event_loop(settings, monkeypatch):
    threads = []

    def update(store, **kwargs):
        try:
            asyncio.get_running_loop()
            threads.append("event loop")
        except RuntimeError:
            threads.append("worker")

    monkeypatch.setattr(approval_classes, "update", update)
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        assert client.put("/v1/settings", headers=OWNER, json={"approvals": {"timeout_min": 30}}).status_code == 200
    assert threads == ["worker"]


def test_the_agent_chat_overlay_survives_a_deleted_peer():
    html = (ROOT / "src/botoffice/static/bot-office.html").read_text(encoding="utf-8")
    start = html.index("function botByName(")
    lookup = html[start:html.index("\n}\n", start) + 2]
    assert "(state.bots || [])" in lookup and '|| "another bot"' in lookup
    opener = html[html.index("function openAgentChat("):]
    assert opener.split("\n")[1].strip().startswith("if (!bot) return;")
    assert "state.lastMessages || []" in opener[:2000]
    node = shutil.which("node")
    if node is None:
        return
    script = (f"let state = {{bots: [{{id: 'b1', name: 'Ada'}}, null]}};\n{lookup}\n"
              "console.log(JSON.stringify([botByName('Ada').id, botByName('Gone').id, botByName(undefined).name, "
              "(state = {}, botByName('X').id)]));")
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == ["b1", "peer:Gone", "another bot", "peer:X"]


def test_adding_columns_is_checked_under_the_write_lock(tmp_path):
    """A second start that waits on another's migration does not fail on the column the other added."""
    path = tmp_path / "agentdesk.sqlite3"
    JobStore(path).initialize()
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE jobs DROP COLUMN held_kind")
    other = sqlite3.connect(path, isolation_level=None, timeout=10)
    other.execute("BEGIN IMMEDIATE")  # another process is migrating
    errors = []

    def start():
        try:
            JobStore(path).initialize()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    time.sleep(0.5)
    other.execute("ALTER TABLE jobs ADD COLUMN held_kind TEXT")
    other.execute("COMMIT")
    other.close()
    thread.join(30)
    assert errors == []
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        assert store_module.add_columns(db, "jobs", (("held_kind", "TEXT"),)) == set()
        assert store_module.add_columns(db, "jobs", (("extra_one", "TEXT"),)) == {"extra_one"}


def test_starts_at_once_on_an_old_database(tmp_path):
    for round_ in range(3):
        path = tmp_path / f"db{round_}.sqlite3"
        JobStore(path).initialize()
        with closing(sqlite3.connect(path)) as db:
            for column in ("held_kind", "held_at", "auto_kind", "auto_decision"):
                db.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        errors = []

        def start(path=path, errors=errors):
            try:
                JobStore(path).initialize()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []


def test_the_readme_rollback_marks_bot_edited_text(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    book = scheduler.assignments
    mine = book.create(bot.id, {"title": "Mine", "goal": "my goal", "start": False})
    edited = book.create(bot.id, {"title": "Edited", "goal": "kept", "done_when": "tests pass", "start": False})
    bots_own = book.agent_create(bot.id, {"title": "Its own", "start": False})
    with closing(store._connect()) as db:
        db.execute("UPDATE assignments SET bot_edited='title,done_when' WHERE id=?", (str(edited.id),))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    line = next(text for text in readme.splitlines() if "[bot-edited] " in text and text.startswith("sqlite3 "))
    sql = re.match(r'sqlite3 data/agentdesk\.sqlite3 "(.*)"$', line).group(1)
    with closing(sqlite3.connect(settings.data_dir / "agentdesk.sqlite3")) as db:
        for _ in range(2):  # twice changes nothing more
            db.executescript(sql)
            db.commit()
    assert (book.get(edited.id).title, book.get(edited.id).goal, book.get(edited.id).done_when) == (
        "[bot-edited] Edited", "kept", "[bot-edited] tests pass")
    assert (book.get(mine.id).title, book.get(mine.id).goal) == ("Mine", "my goal")
    assert book.get(bots_own.id).title == "Its own"  # a bot's own task is untrusted in 0.11.9 already
