"""0.11.12: waiting for another bot's reply is not a blocker. A turn whose only outstanding item is that reply
finishes as done (a task turn: waiting_on_bot), never waiting_on_user, and the reply wakes the bot again
through the continuation rules (``continuation.decide`` and the caps)."""
import asyncio
from contextlib import closing
from uuid import uuid4

import pytest
from test_continuation import Script, save, system_lines

from botoffice import continuation
from botoffice.domain import JobStatus
from botoffice.needs_you import needs_you
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore, now

pytestmark = pytest.mark.continuation_defaults
ASYNC = "Asked the other bot.\nBOT-OFFICE-STATUS: waiting_on_user — reply arrives asynchronously"


class Desk:
    """Test Bot and Peer on one scheduler; the replies are functions of the job."""

    def __init__(self, settings, asker, peer=lambda job: "Here is my answer.\nBOT-OFFICE-STATUS: done — checked the answer against the source, it matches"):
        settings.ensure_dirs()
        self.store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        self.asker_bot = self.store.create_bot("Test Bot", "", "codex", None)
        self.peer_bot = self.store.create_bot("Peer", "", "codex", None)
        self.seen: list = []
        self.asker, self.peer = asker, peer
        self.scheduler = Scheduler(self.store, settings, {"codex": Script(self._reply)})
        self.book = self.scheduler.assignments

    def _reply(self, job):
        self.seen.append(job)
        fn = self.asker if str(job.bot_id) == str(self.asker_bot.id) else self.peer
        return fn(job) if callable(fn) else fn

    def ask(self, job, text="what is the number?", mode="queue"):
        """What bot-msg does from inside a turn."""
        return self.scheduler.turns.post_agent_message(self.store.get_bot(job.bot_id), self.peer_bot, text,
                                                       str(job.id), mode=mode)[2]

    def next(self):
        job = self.store.claim(["desktop-1"], {"codex"})
        if job is None:
            return None
        asyncio.run(self.scheduler._execute(0, job))
        return self.store.get(job.id)

    def all(self, limit=30):
        done = []
        while len(done) < limit and (job := self.next()) is not None:
            done.append(job)
        return done

    def start_chat(self, text="ask the peer"):
        self.scheduler.turns.post_message(self.asker_bot, text)

    def jobs_of(self, bot, **where):
        return [j for j in self.store.list() if str(j.bot_id) == str(bot.id)
                and all(getattr(j, k) == v for k, v in where.items())]


@pytest.mark.parametrize("status_text", [
    ASYNC,
    "Asked the other bot.\nBOT-OFFICE-STATUS: waiting_on_bot — waiting for the peer's answer",
    "Asked the other bot.\nBOT-OFFICE-STATUS: not_done — need the peer's reply first",
])
def test_a_turn_waiting_only_for_a_bot_reply_finishes_done(settings, status_text):
    desk = Desk(settings, lambda job: (desk.ask(job), status_text)[1])
    desk.start_chat()
    first = desk.next()
    assert first.status == JobStatus.SUCCEEDED and first.outcome == "done"
    assert not first.error
    assert needs_you(desk.store) == []
    assert not [m for m in desk.store.list_messages(desk.asker_bot.id) if m.role == "system" and "Not done" in m.text]


def test_the_reply_wakes_the_bot_once_and_it_finishes(settings):
    seen_prompts = []

    def asker(job):
        seen_prompts.append(job.prompt)
        if len(seen_prompts) == 1:
            desk.ask(job)
            return ASYNC
        return "Got it: the number is 7.\nBOT-OFFICE-STATUS: done — checked the answer against the source, it matches"

    desk = Desk(settings, asker, peer="The number is 7.\nBOT-OFFICE-STATUS: done — checked the answer against the source, it matches")
    desk.start_chat()
    jobs = desk.all()
    assert [j.bot_id == desk.asker_bot.id for j in jobs] == [True, False, True]  # ask, peer's inbox turn, wake
    wake = jobs[2]
    assert wake.source == "wakeup" and wake.auto_kind == "wakeup" and wake.outcome == "done"
    assert "The number is 7." in seen_prompts[1] and "untrusted-data" in seen_prompts[1]
    assert needs_you(desk.store) == []
    assert desk.book.wake_for_replies(desk.asker_bot.id) == 0 and not desk.all()  # once only


def test_the_reply_wake_goes_through_decide_and_the_caps(settings, monkeypatch):
    seen = []
    real = continuation.decide
    monkeypatch.setattr(continuation, "decide", lambda s, turn: (seen.append(turn), real(s, turn))[1])
    desk = Desk(settings, lambda job: (desk.ask(job), ASYNC)[1])
    desk.start_chat()
    desk.all()
    assert any(t.kind == "wakeup" and t.path == "chat" for t in seen)


def test_the_hourly_cap_holds_the_reply_wake(settings):
    asked = []
    desk = Desk(settings, lambda job: (desk.ask(job), ASYNC)[1] if not asked.append(1) and len(asked) == 1
                else "ok\nBOT-OFFICE-STATUS: done — checked the answer against the source, it matches")
    save(desk.store, per_hour=1)
    with closing(desk.store._connect()) as db:  # one automatic turn already started this hour
        db.execute("INSERT INTO jobs (id,prompt,status,provider,created_at,started_at,bot_id,source,auto_kind) "
                   "VALUES (?,'x','succeeded','codex',?,?,?,'chat','chat')",
                   (str(uuid4()), now().isoformat(), now().isoformat(), str(desk.peer_bot.id)))
    desk.start_chat()
    desk.all()
    [wake] = desk.jobs_of(desk.asker_bot, source="wakeup")
    assert wake.status == JobStatus.QUEUED and wake.failure_kind == continuation.HELD
    assert any(line.startswith("Automatic work paused: the all-bots cap") for line in system_lines(desk.store, desk.asker_bot))
    save(desk.store, per_hour=None)  # raising the cap releases it
    desk.all()
    got = desk.store.get(wake.id)
    assert got.status == JobStatus.SUCCEEDED, (got.status, got.error, got.failure_kind)


def test_auto_continue_off_drops_the_reply_wake_with_a_line(settings):
    desk = Desk(settings, lambda job: (desk.ask(job), ASYNC)[1])
    save(desk.store, enabled=False)
    desk.start_chat()
    desk.all()
    assert not desk.jobs_of(desk.asker_bot, source="wakeup")
    assert any("auto-continue is off" in line for line in system_lines(desk.store, desk.asker_bot))


def test_a_chat_only_bot_is_not_woken(settings):
    desk = Desk(settings, lambda job: (desk.ask(job), ASYNC)[1])
    desk.store.update_bot(desk.asker_bot.id, chat_only=1)
    desk.start_chat()
    desk.all()
    assert not desk.jobs_of(desk.asker_bot, source="wakeup")


def test_the_consecutive_limit_stops_a_reply_wake(settings):
    desk = Desk(settings, lambda job: (desk.ask(job), ASYNC)[1])
    save(desk.store, consecutive=1)
    desk.start_chat()
    with closing(desk.store._connect()) as db:  # one automatic turn since the owner spoke
        db.execute("INSERT INTO jobs (id,prompt,status,provider,created_at,bot_id,source,auto_kind) "
                   "VALUES (?,'x','succeeded','codex',?,?,'chat','chat')", (str(uuid4()), now().isoformat(), str(desk.asker_bot.id)))
    desk.all()
    assert not desk.jobs_of(desk.asker_bot, source="wakeup")
    assert any("automatic turns in a row" in line for line in system_lines(desk.store, desk.asker_bot))


# --- the owner is still asked when the bot really needs them ---

@pytest.mark.parametrize("status_text", [
    "Which size do you want, large or small?\nBOT-OFFICE-STATUS: waiting_on_user — need your decision on the size",
    "Asked the peer too.\nBOT-OFFICE-STATUS: waiting_on_user — need your photo of the holder, and the peer's reply",
    "Asked the peer.\nPlease send a photo of the bed.\nBOT-OFFICE-STATUS: waiting_on_user — see above",
    "Asked the peer.\nBOT-OFFICE-STATUS: waiting_on_user — owner to pick prod or staging; answer needed before the message goes out",
])
def test_a_real_owner_question_still_waits_on_the_owner(settings, status_text):
    desk = Desk(settings, lambda job: (desk.ask(job), status_text)[1])
    desk.start_chat()
    first = desk.next()
    assert first.status == JobStatus.INCOMPLETE and first.outcome.startswith("waiting_on_user")
    assert "waiting on you" in first.error


def test_waiting_on_the_owner_without_a_pending_bot_reply_is_unchanged(settings):
    desk = Desk(settings, ASYNC)  # says "reply arrives asynchronously" but sent nothing
    desk.start_chat()
    first = desk.next()
    assert first.status == JobStatus.INCOMPLETE and first.outcome.startswith("waiting_on_user")


def test_an_open_question_card_keeps_the_owner_wait(settings):
    text = "Asked the peer.\n```ask\nWhich size?\n- large\n- small\n```\nBOT-OFFICE-STATUS: waiting_on_user — reply arrives"
    desk = Desk(settings, lambda job: (desk.ask(job), text)[1])
    desk.start_chat()
    assert desk.next().status == JobStatus.INCOMPLETE


def test_blocked_is_not_rewritten(settings):
    desk = Desk(settings, lambda job: (desk.ask(job), "No access.\nBOT-OFFICE-STATUS: blocked — no credentials; also waiting for a reply")[1])
    desk.start_chat()
    first = desk.next()
    assert first.status == JobStatus.INCOMPLETE and first.outcome.startswith("blocked")
    desk.all()
    assert not desk.jobs_of(desk.asker_bot, source="wakeup")  # a blocked turn is not waiting for the reply


def test_an_owner_wait_turn_is_not_woken_by_the_reply(settings):
    desk = Desk(settings, lambda job: (desk.ask(job), "Which size do you prefer?\nBOT-OFFICE-STATUS: waiting_on_user — need your decision")[1])
    desk.start_chat()
    desk.all()
    assert not desk.jobs_of(desk.asker_bot, source="wakeup")


# --- the reply arrives while the bot is running: woken when that turn ends ---

def test_a_reply_that_arrives_during_a_turn_wakes_at_its_end(settings):
    desk = Desk(settings, lambda job: (desk.ask(job), ASYNC)[1])
    desk.start_chat()
    desk.next()  # the ask
    desk.scheduler.turns.post_message(desk.asker_bot, "and what else?")  # the owner's next message
    busy = desk.store.claim(["desktop-1"], {"codex"})  # ... which is running now (its turn is not over)
    assert busy.bot_id == desk.asker_bot.id
    asyncio.run(desk.scheduler._execute(0, desk.store.claim(["desktop-2"], {"codex"})))  # the peer answers meanwhile
    assert not desk.jobs_of(desk.asker_bot, source="wakeup")  # deferred: the bot is running
    desk.store.finish(busy.id, JobStatus.SUCCEEDED, exit_code=0, log_path="", error=None, reply_path=None,
                      reply_text="fine", failure_kind=None, usage=None, outcome="done")
    assert desk.book.wake_for_replies(desk.asker_bot.id) == 1


# --- a task turn ---

def test_a_task_turn_waiting_for_a_bot_is_waiting_on_bot_and_the_reply_resumes_it(settings):
    state = {"turn": 0}

    def asker(job):
        state["turn"] += 1
        if state["turn"] == 1:
            desk.ask(job)
            return ASYNC
        return "Used the answer; finished.\nBOT-OFFICE-STATUS: done — wrote the report, ran the check: it passes"

    desk = Desk(settings, asker, peer="The number is 7.\nBOT-OFFICE-STATUS: done — checked the answer against the source, it matches")
    save(desk.store, evidence=False)
    item = desk.book.create(desk.asker_bot.id, {"title": "Report", "goal": "write it", "start": True})
    first = desk.next()
    assert first.assignment_id == item.id
    after = desk.book.get(item.id)
    assert after.status == "waiting_on_bot", (after.status, after.status_note)
    assert needs_you(desk.store) == []
    desk.next()  # the peer's turn: its reply is copied back and wakes the task
    woken = [j for j in desk.store.list() if j.assignment_id == item.id and j.source == "wakeup"]
    assert len(woken) == 1 and woken[0].auto_kind == "wakeup"
    assert desk.book.get(item.id).status == "active"
    desk.all()
    assert desk.book.get(item.id).status == "done"


def test_a_task_the_guard_paused_is_not_woken_by_the_reply(settings):
    def asker(job):
        desk.ask(job)
        return "Still blocked on the photo.\nBOT-OFFICE-STATUS: continue — wait"

    desk = Desk(settings, asker)
    item = desk.book.create(desk.asker_bot.id, {"title": "Report", "goal": "write it", "start": True})
    desk.all()
    assert desk.book.get(item.id).status == "waiting_on_user"
    assert not [j for j in desk.store.list() if j.assignment_id == item.id and j.source == "wakeup"]
