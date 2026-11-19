"""0.11.10: one continuation system for the chat auto-continue and the task loop (docs/CONTINUATION.md)."""
import asyncio
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_auto_continue import _queued as chat_queued
from test_auto_continue import _run_all as chat_run_all
from test_auto_continue import _run_one as chat_run_one
from test_auto_continue import _setup as chat_setup
from test_autocontinue import REAL_LOOP

from botoffice import continuation
from botoffice.api import create_app
from botoffice.assignments import AssignmentError
from botoffice.config import Settings
from botoffice.domain import JobStatus
from botoffice.needs_you import needs_you
from botoffice.providers.base import RunResult
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore, now
from botoffice.workspace import build_self

pytestmark = pytest.mark.continuation_defaults
OWNER = {"Authorization": "Bearer test-token"}
CHAT_LOOP = [reply.replace("BOT-OFFICE-STATUS: continue", "BOT-OFFICE-STATUS: not_done") for reply in REAL_LOOP]


def fresh(n: int, status: str = "continue") -> list[str]:
    return [f"Completed unit {i}: wrote module{i} covering topic{i}\nBOT-OFFICE-STATUS: {status} — next" for i in range(n)]


class Script:
    """Replies in order (the last repeats); a callable gets the job and returns the reply."""

    name = "codex"
    enabled = True

    def __init__(self, *replies):
        self.replies, self.jobs = list(replies), []

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        self.jobs.append(job)
        log_path.write_text("log")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(reply):
            reply = reply(job)
        return RunResult("succeeded", exit_code=0, final_text=reply)


def task_setup(settings, *replies, bot_name="Ada", chat_only=False, **fields):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = Script(*replies)
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot(bot_name, "", "codex", None)
    if chat_only:
        bot = store.update_bot(bot.id, chat_only=1)
    item = scheduler.assignments.create(bot.id, {"title": "Report", "goal": "write it", "budget_turns": 40,
                                                 **fields, "start": True})
    return store, scheduler, adapter, bot, item


def run_next(store, scheduler):
    job = store.claim(["desktop-1"], {"codex"})
    if job is None:
        return None
    asyncio.run(scheduler._execute(0, job))
    return store.get(job.id)


def run_all(store, scheduler, limit=60):
    jobs = []
    while len(jobs) < limit and (job := run_next(store, scheduler)) is not None:
        jobs.append(job)
    return jobs


def system_lines(store, bot):
    return [m.text for m in store.list_messages(bot.id) if m.role == "system"]


def task_queued(store, item):
    return [j for j in store.list() if j.assignment_id == item.id and j.status == JobStatus.QUEUED]


def save(store, **changes):
    return continuation.update(store, changes)


# --- one decide function for both paths ---

def test_both_paths_ask_the_one_decide_function(settings, tmp_path, monkeypatch):
    seen = []
    real = continuation.decide

    def spy(settings_, turn):
        seen.append(turn.path)
        return real(settings_, turn)

    monkeypatch.setattr(continuation, "decide", spy)
    store, scheduler, _, _ = chat_setup(settings, fresh(3, "not_done"))
    chat_run_one(store, scheduler)
    store2, scheduler2, *_ = task_setup(Settings(data_dir=tmp_path / "t", token="test-token", job_timeout=2), *fresh(3))
    run_next(store2, scheduler2)
    assert seen == ["chat", "task"]


@pytest.mark.parametrize("path", ["chat", "task"])
def test_one_consecutive_setting_governs_both_paths(settings, path):
    """Behaviour, not wiring: the same setting (2 automatic turns) stops either path after the owner's turn + 2."""
    if path == "chat":
        store, scheduler, _, bot = chat_setup(settings, fresh(20, "not_done"))
        save(store, consecutive=2)
        jobs = chat_run_all(store, scheduler)
    else:
        store, scheduler, _, bot, item = task_setup(settings, *fresh(20))
        save(store, consecutive=2)
        jobs = run_all(store, scheduler)
        after = scheduler.assignments.get(item.id)
        assert after.status == "waiting_on_user" and after.status_note.startswith("paused, not blocked: 2 automatic")
    assert len(jobs) == 3
    assert any("2 automatic turns in a row without a reply from you" in line for line in system_lines(store, bot))


def test_the_real_loop_replayed_stops_in_the_chat_path_too(settings):
    store, scheduler, _, bot = chat_setup(settings, CHAT_LOOP)
    jobs = chat_run_all(store, scheduler)
    assert len(jobs) == 3 and chat_queued(store, bot) == []  # was: on to the chain limit (10)
    assert system_lines(store, bot)[-1] == ("Stopped continuing automatically: 3 turns in a row with no progress. "
                                            "Reply to keep it going.")


def test_a_blocked_reply_stops_the_chat_path(settings):
    store, scheduler, _, bot = chat_setup(settings, [
        "The combined layout remains blocked by the placement check.\nBOT-OFFICE-STATUS: not_done — keep checking"])
    assert len(chat_run_all(store, scheduler)) == 1
    assert 'blocked or needs you ("remains blocked")' in system_lines(store, bot)[-1]


def test_a_blocked_reply_still_stops_the_task_loop(settings):
    store, scheduler, _, _bot, item = task_setup(settings, REAL_LOOP[0], "Still blocked on the photo.\n"
                                                "BOT-OFFICE-STATUS: continue — wait")
    assert len(run_all(store, scheduler)) == 2
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and "blocked or needs you" in after.status_note
    [item_] = [i for i in needs_you(store) if i["kind"] == "assignment"]
    assert "paused, not blocked" in item_["ask"]


# --- caps (G3) ---

def test_per_bot_daily_cap_defers_the_turn_with_a_line_and_needs_you(settings):
    store, scheduler, _, bot, item = task_setup(settings, *fresh(20))
    save(store, per_bot_day=2, per_hour=None)
    jobs = run_all(store, scheduler)
    assert len(jobs) == 3  # the owner's Start + 2 automatic turns; the third waits
    [held] = task_queued(store, item)
    with closing(store._connect()) as db:
        row = db.execute("SELECT auto_kind, failure_kind, not_before FROM jobs WHERE id=?", (str(held.id),)).fetchone()
        first_auto = db.execute("SELECT MIN(started_at) FROM jobs WHERE auto_kind IS NOT NULL AND started_at IS NOT NULL"
                                ).fetchone()[0]
    assert row["auto_kind"] == "loop" and row["failure_kind"] == continuation.HELD
    assert row["not_before"] > first_auto  # deferred to when the 24 h window slides, not dropped
    line = system_lines(store, bot)[-1]
    assert "per-bot cap of 2 automatic turns a day" in line and "raise the cap in Settings" in line
    [needs] = [i for i in needs_you(store) if i["kind"] == "auto_cap"]
    assert "per-bot cap of 2" in needs["ask"] and "Settings" in needs["ask"] and needs["actions"] == ["settings"]
    assert scheduler.assignments.get(item.id).status == "active"  # nothing lost; it waits
    # raising the cap releases the held turn at once, with no second line for it
    save(store, per_bot_day=3)
    assert run_next(store, scheduler).id == held.id
    assert run_next(store, scheduler) is None  # the next one is over the new cap
    assert sum("per-bot cap" in text for text in system_lines(store, bot)) == 2  # held again at the new cap only


def test_all_bots_hourly_cap_counts_every_bot(settings):
    store, scheduler, _, ada = chat_setup(settings, fresh(20, "not_done"))
    save(store, per_hour=1)
    bea = store.create_bot("Bea", "", "codex", None)
    chat_run_one(store, scheduler)  # Ada's own turn
    chat_run_one(store, scheduler)  # Ada's automatic turn 1 (the hour's only one)
    scheduler.turns.post_message(bea, "go")
    assert chat_run_one(store, scheduler).bot_id == bea.id  # owner turns are never capped
    assert chat_run_one(store, scheduler) is None  # both continuations wait for the hour
    assert any("all-bots cap of 1 automatic turns an hour" in t for t in system_lines(store, bea) + system_lines(store, ada))
    assert {i["bot_name"] for i in needs_you(store) if i["kind"] == "auto_cap"} == {"Ada", "Bea"}


def test_owner_message_supersedes_a_held_chat_continuation(settings):
    store, scheduler, _, bot = chat_setup(settings, fresh(20, "not_done"))
    save(store, per_bot_day=1)
    chat_run_all(store, scheduler)
    [held] = chat_queued(store, bot)
    assert held["failure_kind"] == continuation.HELD
    _, mine = scheduler.turns.post_message(bot, "here is what you needed")
    assert [q["id"] for q in chat_queued(store, bot)] == [str(mine.id)]


def test_unlimited_caps_through_the_settings_api(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        got = client.get("/v1/settings", headers=OWNER).json()["auto_continue"]
        assert got["per_bot_day"] == 20 and got["per_hour"] == 20 and got["evidence"] is True
        assert got["no_progress"] == 3 and got["consecutive"] == 10
        put = client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"per_bot_day": "unlimited",
                                                                                "per_hour": None}}).json()
        assert put["auto_continue"]["per_bot_day"] is None and put["auto_continue"]["per_hour"] is None
        assert put["auto_continue"]["max"] == 10  # untouched keys keep their value
        assert client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"per_hour": 0}}).status_code == 422
        assert client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"no_progress": 0}}).status_code == 422
        assert client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"per_hour": 40}}).json()[
            "auto_continue"]["per_hour"] == 40


def test_unlimited_caps_let_the_loop_run_past_20(settings):
    store, scheduler, _, _, item = task_setup(settings, *fresh(40))
    save(store, per_bot_day=None, per_hour=None, consecutive=30)
    assert len(run_all(store, scheduler)) == 31
    assert "30 automatic turns" in scheduler.assignments.get(item.id).status_note


def test_env_defaults_for_the_caps(monkeypatch):
    monkeypatch.setenv("BOT_OFFICE_AUTO_TURNS_PER_BOT_DAY", "40")
    monkeypatch.setenv("BOT_OFFICE_AUTO_TURNS_PER_HOUR", "unlimited")
    monkeypatch.setenv("BOT_OFFICE_AUTO_NO_PROGRESS", "bogus")
    got = continuation.load(None)
    assert got["per_bot_day"] == 40 and got["per_hour"] is None and got["no_progress"] == 3
    assert continuation.load('{"per_bot_day": 5}')["per_bot_day"] == 5  # a saved value wins


# --- chat-only bots (G1) ---

def test_chat_only_bot_never_continues_in_chat(settings):
    store, scheduler, _, bot = chat_setup(settings, fresh(3, "not_done"))
    store.update_bot(bot.id, chat_only=1)
    assert len(chat_run_all(store, scheduler)) == 1 and chat_queued(store, bot) == []
    assert "chat-only" in system_lines(store, bot)[-1]


def test_chat_only_bot_gets_no_loop_wakeup_or_own_start(settings):
    store, scheduler, _, bot, item = task_setup(settings, *fresh(5), chat_only=True)
    assert len(run_all(store, scheduler)) == 1  # the owner's Start runs one turn
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and "chat-only" in after.status_note
    book = scheduler.assignments
    _, job = scheduler.turns.post_message(bot, "how is it going?")  # a chat turn, not the task's own
    with pytest.raises(AssignmentError) as refused:
        book.report(item.id, bot.id, job.id, "continue")  # out of its turn: no self-start
    assert refused.value.status == 409
    own = book.agent_create(bot.id, {"title": "mine", "start": True})
    assert own.status == "open" and not task_queued(store, own)
    wake = book.add_wakeup(bot.id, None, due_at=now() + timedelta(hours=1), note="later")
    with closing(store._connect()) as db:
        db.execute("UPDATE wakeups SET due_at=? WHERE id=?", ((now() - timedelta(minutes=1)).isoformat(), str(wake.id)))
    assert book.fire_due() == 0
    assert "chat-only" in system_lines(store, bot)[-1]
    self_md = build_self(store.get_bot(bot.id), job)
    assert "Automatic continuation: off: you are a chat-only bot" in self_md


def test_chat_only_on_the_bot_api(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        made = client.post("/v1/bots", headers=OWNER, json={"name": "Quiet", "chat_only": True}).json()
        assert made["chat_only"] is True
        other = client.post("/v1/bots", headers=OWNER, json={"name": "Busy"}).json()
        assert other["chat_only"] is False
        patched = client.patch(f"/v1/bots/{other['id']}", headers=OWNER, json={"chat_only": True}).json()
        assert patched["chat_only"] is True


# --- evidence before done (G4) ---

def test_out_of_turn_start_asks_the_one_decide_function(settings, monkeypatch):
    """bot-task start / status continue from another turn (chat, routine, wake-up) is a continuation
    too: it goes through continuation.decide and a stop refuses it (409), the task as it was."""
    store, scheduler, _, bot, item = task_setup(settings, *fresh(20))
    save(store, consecutive=2)
    run_all(store, scheduler)
    paused = scheduler.assignments.get(item.id)
    assert paused.status == "waiting_on_user" and "paused" in paused.status_note
    chat = store.create("routine: check in", "codex", bot_id=bot.id, source="routine")  # not the owner speaking
    seen = []
    real = continuation.decide
    monkeypatch.setattr(continuation, "decide", lambda s, turn: seen.append(turn) or real(s, turn))
    with pytest.raises(AssignmentError) as refused:
        scheduler.assignments.report(item.id, bot.id, chat.id, "continue")
    assert refused.value.status == 409 and "automatic turns in a row" in refused.value.detail
    assert seen and seen[-1].path == "task" and seen[-1].kind == "continue"
    assert scheduler.assignments.get(item.id).status == "waiting_on_user" and not task_queued(store, item)
    # the decision is not stamped on the asking job (a chat turn's own continuation is decided separately)
    with closing(store._connect()) as db:
        assert db.execute("SELECT auto_decision FROM jobs WHERE id=?", (str(chat.id),)).fetchone()[0] is None
    # auto-continue off: an open task is not started by the bot out of its turn either
    other = scheduler.assignments.create(bot.id, {"title": "later", "start": False})
    save(store, enabled=False)
    with pytest.raises(AssignmentError):
        scheduler.assignments.report(other.id, bot.id, chat.id, "continue")
    save(store, enabled=True)
    started = scheduler.assignments.report(other.id, bot.id, chat.id, "continue")
    assert started.status == "active" and task_queued(store, other)


def test_done_without_evidence_continues_once_then_waits(settings):
    store, scheduler, adapter, _bot, item = task_setup(settings, "Finished.\nBOT-OFFICE-STATUS: done")
    jobs = run_all(store, scheduler)
    assert len(jobs) == 2  # the done, then one evidence turn
    assert "bot-task done --evidence" in adapter.jobs[1].prompt
    with closing(store._connect()) as db:
        assert db.execute("SELECT auto_kind FROM jobs WHERE id=?", (str(jobs[1].id),)).fetchone()[0] == "evidence"
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and after.status_note.startswith("says it is done; no evidence")
    # the owner completes it without any evidence
    scheduler.assignments.update(item.id, {"status": "done"})
    assert scheduler.assignments.get(item.id).status == "done"


def test_done_with_evidence_closes(settings):
    holder = {}

    def done_with_evidence(job):
        holder["scheduler"].assignments.report(job.assignment_id, job.bot_id, job.id, "done",
                                               evidence="pytest -q: 12 passed")
        return "Finished.\nBOT-OFFICE-STATUS: done"

    store, scheduler, _, _, item = task_setup(settings, done_with_evidence)
    holder["scheduler"] = scheduler
    assert len(run_all(store, scheduler)) == 1
    assert scheduler.assignments.get(item.id).status == "done"
    assert any(e["kind"] == "evidence" and "12 passed" in e["note"] for e in scheduler.assignments.events(item.id))


def test_evidence_setting_off_closes_on_done(settings):
    store, scheduler, _, _, item = task_setup(settings, "Finished.\nBOT-OFFICE-STATUS: done")
    save(store, evidence=False)
    assert len(run_all(store, scheduler)) == 1 and scheduler.assignments.get(item.id).status == "done"


def test_out_of_turn_done_needs_evidence(settings):
    store, scheduler, _, bot, item = task_setup(settings, "step\nBOT-OFFICE-STATUS: waiting_on_user — ok?")
    run_all(store, scheduler)
    _, chat = scheduler.turns.post_message(bot, "is it done?")
    with pytest.raises(AssignmentError) as refused:
        scheduler.assignments.report(item.id, bot.id, chat.id, "done")
    assert refused.value.status == 409 and "--evidence" in refused.value.detail
    scheduler.assignments.report(item.id, bot.id, chat.id, "done", evidence="opened the file; 3 pages")
    assert scheduler.assignments.get(item.id).status == "done"


def test_plain_chat_done_is_unaffected(settings):
    store, scheduler, _, bot = chat_setup(settings, ["All done.\nBOT-OFFICE-STATUS: done"])
    assert len(chat_run_all(store, scheduler)) == 1 and chat_queued(store, bot) == []
    assert not [t for t in system_lines(store, bot) if "evidence" in t]


# --- fires once ---

def test_chat_continuation_fires_once_per_job(settings):
    store, scheduler, _, bot = chat_setup(settings, fresh(3, "not_done"))
    first = chat_run_one(store, scheduler)
    assert len(chat_queued(store, bot)) == 1
    assert scheduler._auto_continue(store.get(first.id)) == "already decided"
    assert len(chat_queued(store, bot)) == 1


def test_task_settle_twice_queues_one_turn(settings):
    store, scheduler, _, _, item = task_setup(settings, *fresh(3))
    job = run_next(store, scheduler)
    assert scheduler.assignments.settle(job.id) is None  # already settled by the scheduler
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET assignment_settled=0 WHERE id=?", (str(job.id),))
    # even a replay that got past the settle flag would find the queued turn and add none
    scheduler.assignments.settle(job.id)
    assert len(task_queued(store, item)) == 1


# --- the owner speaking resets the chain (G2), and claim order ---

def test_owner_steer_into_a_task_turn_resets_the_count(settings):
    holder = {"n": 0}

    def steer_on_second_auto(job):
        holder["n"] += 1
        if holder["n"] == 3:  # the 2nd automatic turn: the owner's message joins it
            with closing(holder["store"]._connect()) as db:
                db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments,steer_job_id,"
                           "steer_status) VALUES (?,'user','chat','also add docs',?,'[]',?,'delivered')",
                           (str(job.bot_id), now().isoformat(), str(job.id)))
        return f"Completed unit {holder['n']}: module{holder['n']}\nBOT-OFFICE-STATUS: continue — next"

    store, scheduler, _, _, item = task_setup(settings, steer_on_second_auto)
    holder["store"] = store
    save(store, consecutive=2)
    jobs = run_all(store, scheduler)
    # Start, auto 1, auto 2 (steered: a fresh run starts there), auto 1, auto 2, then the pause
    assert len(jobs) == 5
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"


def test_owner_chat_between_task_turns_resets_the_count(settings):
    store, scheduler, _, bot, _item = task_setup(settings, *fresh(20))
    save(store, consecutive=2)
    run_next(store, scheduler)
    run_next(store, scheduler)  # auto 1
    scheduler.turns.post_message(bot, "keep going")  # runs first (claim order), and resets the count
    jobs = run_all(store, scheduler)
    assert jobs[0].source == "chat" and len([j for j in jobs if j.assignment_id]) == 2


def test_claim_order_owner_chat_then_urgent_task_then_normal(settings):
    store, scheduler, _, bot, normal = task_setup(settings, *fresh(20))
    urgent = scheduler.assignments.create(bot.id, {"title": "Hot", "priority": "urgent", "start": True})
    run_next(store, scheduler)
    run_next(store, scheduler)  # both owner Starts ran; each queued its loop turn
    _, chat = scheduler.turns.post_message(bot, "a question")
    assert store.claim(["desktop-1"], {"codex"}).id == chat.id
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded', finished_at=? WHERE id=?", (now().isoformat(), str(chat.id)))
    hot = store.claim(["desktop-1"], {"codex"})
    assert hot.assignment_id == urgent.id
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded', finished_at=? WHERE id=?", (now().isoformat(), str(hot.id)))
    assert store.claim(["desktop-1"], {"codex"}).assignment_id == normal.id


def test_bot_task_done_sends_the_evidence(tmp_path):
    import json
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"id": "a1b2c3d4e5", "status": "done", "title": "T", "turns_used": 1,
                                         "budget_turns": 20, "minutes_used": 2, "budget_minutes": 120}).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    helper = Path(__file__).resolve().parents[1] / "bin" / "bot-task"
    env = {"BOT_OFFICE_API_URL": f"http://127.0.0.1:{server.server_port}", "BOT_OFFICE_AGENT_TOKEN": "scoped"}
    try:
        subprocess.run([sys.executable, "-I", str(helper), "done", "--evidence", "pytest -q: 14 passed"],
                       env=env, capture_output=True, text=True, timeout=5, check=True)
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert received == [("/v1/agent/tasks/current/status",
                         {"state": "done", "note": "", "evidence": "pytest -q: 14 passed"})]
    missing = subprocess.run([sys.executable, "-I", str(helper), "done"], env=env, capture_output=True, text=True, timeout=5, check=False)
    assert missing.returncode != 0 and "--evidence" in missing.stderr



def test_evidence_must_be_fresh_and_long_enough_and_is_shown(settings):
    """G4 gate: evidence given before the task last became active (rewound, reopened, resumed) does
    not count, nor one shorter than evidence_min; the accepted evidence is in the done note, and
    Needs-you shows the last evidence given."""
    store, scheduler, _, bot, item = task_setup(settings, "step\nBOT-OFFICE-STATUS: waiting_on_user — ok?")
    run_all(store, scheduler)
    book = scheduler.assignments
    chat = store.create("routine: check in", "codex", bot_id=bot.id, source="routine")
    with pytest.raises(AssignmentError) as short:
        book.report(item.id, bot.id, chat.id, "done", evidence="ok")
    assert short.value.status == 400 and "at least 12" in short.value.detail
    book.report(item.id, bot.id, chat.id, "done", evidence="pytest -q: 14 passed")
    done = book.get(item.id)
    assert done.status == "done" and "evidence: pytest -q: 14 passed" in done.status_note
    assert any("evidence: pytest -q: 14 passed" in line for line in system_lines(store, bot))
    # the owner reopens it: the old evidence no longer counts
    book.update(item.id, {"status": "active"})
    with pytest.raises(AssignmentError) as stale:
        book.report(item.id, bot.id, chat.id, "done")
    assert stale.value.status == 409 and "--evidence" in stale.value.detail
    # waiting on the owner: Needs-you shows the last evidence given
    book.report(item.id, bot.id, chat.id, "waiting_on_user", note="check please")
    item2 = book.get(item.id)
    assert item2.status == "waiting_on_user"
    [entry] = [i for i in needs_you(store) if i.get("assignment_id") == str(item.id)]
    assert entry["evidence"] == "pytest -q: 14 passed" and 'last evidence given: "pytest -q: 14 passed"' in entry["ask"]
    # the minimum is a setting
    save(store, evidence_min=2)
    book.update(item.id, {"status": "active"})
    book.report(item.id, bot.id, chat.id, "done", evidence="ok")
    assert book.get(item.id).status == "done"


def test_a_stale_evidence_does_not_close_the_loop(settings):
    holder = {"n": 0}

    def reply(job):
        holder["n"] += 1
        if holder["n"] == 1:
            holder["s"].assignments.report(job.assignment_id, job.bot_id, job.id, "continue",
                                           evidence="ran the checker: all 3 files pass")
            return "Checked.\nBOT-OFFICE-STATUS: waiting_on_user — look?"
        return "Finished.\nBOT-OFFICE-STATUS: done"

    store, scheduler, _, _, item = task_setup(settings, reply)
    holder["s"] = scheduler
    run_all(store, scheduler)
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"
    scheduler.assignments.reply(item.id, "go on")  # resumes: active again, old evidence is history
    run_all(store, scheduler)
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and after.status_note.startswith("says it is done; no evidence")
