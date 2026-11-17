"""0.11.10: a turn that ends not_done is continued automatically, within the owner's limit."""
import asyncio
from contextlib import closing

import pytest

from botoffice import baseline
from botoffice.domain import JobStatus
from botoffice.providers.base import RunResult
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import AUTO_CONTINUE_LINE, Scheduler
from botoffice.store import JobStore


class ReplyAdapter(FakeAdapter):
    """Replies with the next scripted text (the last one repeats); *during* runs inside a turn."""

    def __init__(self, replies, during=None):
        super().__init__()
        self.replies, self.during, self.jobs = list(replies), during, []

    async def run(self, job, workspace, log_path, timeout):
        self.jobs.append(job)
        if self.during:
            self.during(job)
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        (workspace / "reply.txt").write_text(text)
        log_path.write_text("ok\n")
        return RunResult("succeeded", exit_code=0)


NOT_DONE = "Halfway there.\nBOT-OFFICE-STATUS: not_done — next: write the tests"


def _setup(settings, replies, during=None, source="chat"):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = ReplyAdapter(replies, during)
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.turns.post_message(bot, "build the thing", source=source)
    return store, scheduler, adapter, bot


def _run_one(store, scheduler):
    job = store.claim(("desktop-1",), {"codex"})
    if job is None:
        return None
    asyncio.run(scheduler._execute(0, job))
    return store.get(job.id)


def _run_all(store, scheduler, limit=60):
    done = []
    while (job := _run_one(store, scheduler)) is not None and len(done) < limit:
        done.append(job)
    return done


def _queued(store, bot):
    with closing(store._connect()) as db:
        return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE bot_id=? AND status='queued'", (str(bot.id),))]


def test_not_done_queues_a_continuation_in_the_same_source(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE, "All done.\nBOT-OFFICE-STATUS: done"])
    first = _run_one(store, scheduler)
    assert first.status == JobStatus.INCOMPLETE
    [queued] = _queued(store, bot)
    assert queued["source"] == "chat" and queued["continue_count"] == 1 and queued["continued_from"] == str(first.id)
    assert "Automatic continuation 1 of 10" in queued["prompt"]
    # the continuation is machinery: kept (and shown with ?archived=true) but not in the chat
    assert not [m for m in store.list_messages(bot.id) if m.sender == "Bot-Office"]
    assert not [m for m in store.list_messages(bot.id) if m.role == "system" and m.text.startswith("Not done")]
    lines = [m for m in store.list_messages(bot.id, include_hidden=True) if m.sender == "Bot-Office"]
    assert lines[-1].text == AUTO_CONTINUE_LINE.format(n=1, max=10) and lines[-1].hidden
    assert lines[-1].author == "office"
    second = _run_one(store, scheduler)
    assert second.status == JobStatus.SUCCEEDED and second.continue_count == 1
    assert "an automatic continuation of your unfinished task" in (
        settings.data_dir / "workspaces" / str(second.id) / "prompt.txt").read_text()
    assert _queued(store, bot) == []  # done: the chain ends


@pytest.mark.parametrize("ending", [
    "Need the API key.\nBOT-OFFICE-STATUS: waiting_on_user — which key?",
    "Can't reach the server.\nBOT-OFFICE-STATUS: blocked — host down",
    "Finished.\nBOT-OFFICE-STATUS: done",
    "Finished, no status line.",
])
def test_only_not_done_continues(settings, ending):
    store, scheduler, _, bot = _setup(settings, [ending])
    _run_one(store, scheduler)
    assert _queued(store, bot) == []


def test_chain_stops_at_the_owner_limit(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    baseline.set_auto_continue(store, maximum=3)
    runs = _run_all(store, scheduler)
    assert [job.continue_count for job in runs] == [0, 1, 2, 3]
    assert _queued(store, bot) == []
    assert any("Stopped continuing automatically after 3" in m.text for m in store.list_messages(bot.id)
               if m.role == "system")


def test_switched_off_by_the_owner(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    baseline.set_auto_continue(store, enabled=False)
    _run_one(store, scheduler)
    assert _queued(store, bot) == []


def test_stop_ends_the_chain(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    _run_one(store, scheduler)
    [queued] = _queued(store, bot)
    store.cancel(queued["id"])  # the user pressed Stop on the queued continuation
    assert _run_all(store, scheduler) == [] and _queued(store, bot) == []


def test_a_queued_user_message_wins(settings):
    holder = {}

    def user_writes(job):
        if "job" not in holder:
            holder["job"] = holder["scheduler"].turns.post_message(holder["bot"], "actually, do X instead")[1]

    store, scheduler, _, bot = _setup(settings, [NOT_DONE], during=user_writes)
    holder.update(scheduler=scheduler, bot=bot)
    _run_one(store, scheduler)
    queued = _queued(store, bot)
    assert [q["id"] for q in queued] == [str(holder["job"].id)] and queued[0]["continue_count"] == 0


def test_a_steered_message_starts_a_fresh_count(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    first = _run_one(store, scheduler)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET continue_count=7, continued_from=? WHERE id=?", (str(first.id), str(first.id)))
        db.execute("UPDATE jobs SET status='cancelled' WHERE bot_id=? AND status='queued'", (str(bot.id),))
        db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,attachments,steer_job_id,steer_status) "
                   "VALUES (?,'user','chat','also add docs','2026-11-17T00:00:00+00:00','[]',?,'delivered')",
                   (str(bot.id), str(first.id)))
    assert scheduler._auto_continue(store.get(first.id)) == "continued"
    [queued] = _queued(store, bot)
    assert queued["continue_count"] == 1 and queued["continued_from"] == str(first.id)


@pytest.mark.parametrize("blocker", ["has_open_question", "pending_approval"])
def test_open_question_or_approval_holds_it(settings, monkeypatch, blocker):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    monkeypatch.setattr(store, blocker, lambda job_id: True)
    _run_one(store, scheduler)
    assert _queued(store, bot) == []


@pytest.mark.parametrize("source", ["inbox", "trigger"])
def test_not_for_bot_to_bot_or_watcher_turns(settings, source):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    job = store.get(_queued(store, bot)[0]["id"]).model_copy(update={"source": source})
    assert scheduler._auto_continue(job) == "not eligible"


def test_routine_continuation_stays_unattended(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE], source="routine")
    _run_one(store, scheduler)
    [queued] = _queued(store, bot)
    assert queued["source"] == "routine" and queued["continue_count"] == 1


def test_assignment_turns_use_their_own_loop(settings):
    store, scheduler, _, bot = _setup(settings, [NOT_DONE])
    job = store.get(_queued(store, bot)[0]["id"]).model_copy(update={"assignment_id": bot.id})
    assert scheduler._auto_continue(job) == "not eligible"


def test_rule_seven_asks_to_keep_working():
    from botoffice.workspace import RULES
    seven = next(line for line in RULES.splitlines() if line.startswith("7."))
    assert "Keep working until the task is done" in seven and "not_done means you will carry on" in seven
