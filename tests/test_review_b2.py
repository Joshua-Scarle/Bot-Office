"""Stage B verification round 2: rewind of closed assignments, the bot routine gap by actual
start times, and the pre-release budget migration."""

from contextlib import closing
from datetime import timedelta

from test_loop import run_all, run_next
from test_loop import setup as loop_setup

from botoffice.domain import JobStatus
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore, now

WAIT = "Asked.\nBOT-OFFICE-STATUS: waiting_on_user — which one?"
DONE = "Done.\nBOT-OFFICE-STATUS: done"


def sql(store, query, *args):
    with closing(store._connect()) as db:
        db.execute(query, args)
        db.commit()


def test_rewind_reopens_assignments_the_archived_turns_completed(settings):
    store, scheduler, adapter, bot, item = loop_setup(settings, WAIT, DONE, DONE, DONE)
    book = scheduler.assignments
    run_all(store, scheduler)
    reply_job = book.reply(item.id, "answer one")
    run_all(store, scheduler)
    other = book.create(bot.id, {"title": "Other", "start": True})
    run_all(store, scheduler)
    assert book.get(item.id).status == "done" and book.get(other.id).status == "done"
    message = next(m for m in store.list_messages(bot.id) if m.job_id and str(m.job_id) == reply_job
                   and m.role == "user")
    _, job = scheduler.turns.edit_message(bot, message.id, "answer two")  # the reply of a closed assignment
    rerun = book.get(item.id)
    assert rerun.status == "active" and rerun.closed_at is None and "rewound" in rerun.status_note
    reopened = book.get(other.id)
    assert reopened.status == "open" and reopened.closed_at is None and "rewound" in reopened.status_note
    assert store.get(job.id).assignment_id == item.id
    assert run_next(store, scheduler).id == job.id and "answer two" in adapter.prompts[-1]


def test_bot_routine_gap_counts_from_the_actual_start_of_a_late_run(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter(enabled=True)})
    bot = store.create_bot("Ada", "", "codex", None)
    routine = store.create_routine(bot.id, "*/15 * * * *", "tick", True, created_by="bot")
    stamp = now()
    current = stamp.replace(minute=stamp.minute - stamp.minute % 15, second=0, microsecond=0)
    sql(store, "UPDATE routines SET last_run_at=?, scan_cursor=? WHERE id=?",
        (current - timedelta(minutes=30)).isoformat(), (current - timedelta(minutes=1)).isoformat(), str(routine.id))
    late = store.create("tick", "codex", bot_id=bot.id, source="routine", routine_id=routine.id)
    sql(store, "UPDATE jobs SET status='succeeded', started_at=?, finished_at=? WHERE id=?",
        (current - timedelta(minutes=1)).isoformat(), current.isoformat(), str(late.id))
    scheduler._fire_routine(store.get_routine(routine.id), current)
    assert [j.id for j in store.list() if j.source == "routine"] == [late.id]  # one minute after: deferred
    assert store.get_routine(routine.id).pending_run is not None
    sql(store, "UPDATE jobs SET started_at=? WHERE id=?", (current - timedelta(minutes=16)).isoformat(), str(late.id))
    scheduler._fire_routine(store.get_routine(routine.id), current)
    assert len([j for j in store.list() if j.source == "routine"]) == 2  # the owed run, once the gap passed


def test_budget_migration_neither_double_charges_nor_loses_turn_charges(settings):
    store, _, _, _, item = loop_setup(settings)
    start, end = (now() - timedelta(minutes=3)).isoformat(), now().isoformat()
    jobs = [store.create("x", "codex", bot_id=item.bot_id, source="assignment") for _ in range(2)]
    sql(store, "DELETE FROM jobs WHERE assignment_id=?", str(item.id))  # the queued first turn
    for job, settled in zip(jobs, (1, 0), strict=True):
        sql(store, "UPDATE jobs SET status='succeeded', started_at=?, finished_at=?, assignment_id=?, "
            "assignment_turn=?, assignment_settled=? WHERE id=?", start, end, str(item.id), 2 - settled, settled,
            str(job.id))
    # pre-release 0.10.0: the settled job's turn and minutes were charged at settlement
    sql(store, "UPDATE assignments SET turns_used=1, minutes_used=3, next_turn=2 WHERE id=?", str(item.id))
    sql(store, "ALTER TABLE jobs DROP COLUMN assignment_minutes")
    upgraded = JobStore(settings.data_dir / "agentdesk.sqlite3")
    with closing(upgraded._connect()) as db:
        charged = db.execute("SELECT assignment_minutes FROM jobs WHERE id=?", (str(jobs[0].id),)).fetchone()[0]
        row = db.execute("SELECT turns_used FROM assignments WHERE id=?", (str(item.id),)).fetchone()
    assert 2.9 < charged < 3.1 and row[0] == 2  # settled: fully charged; unsettled: its turn now
    Scheduler(upgraded, settings, {}).assignments.settle_pending()
    after = Scheduler(upgraded, settings, {}).assignments.get(item.id)
    assert after.turns_used == 2 and 5.9 < after.minutes_used < 6.1
    assert upgraded.get(jobs[1].id).status == JobStatus.SUCCEEDED
