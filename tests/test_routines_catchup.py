"""0.9.0 routines: minute-window scan, catch-up policy and notices; tick errors are logged (A4)."""
import logging
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 14, 10, 0, 20, tzinfo=NY)


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


def make(settings, providers=None):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    clock = Clock(NOW)
    scheduler = Scheduler(store, settings, providers or {"codex": FakeAdapter()}, clock=clock)
    bot = store.create_bot("Ada", "", "codex", None)
    return store, scheduler, bot, clock


def routine(store, bot, cron, cursor, catch_up="once", name="Daily"):
    created = store.create_routine(bot.id, cron, "check things", True, name=name, catch_up=catch_up)
    if cursor is None:
        with closing(store._connect()) as db:
            db.execute("UPDATE routines SET scan_cursor=NULL WHERE id=?", (str(created.id),))
    else:
        store.update_routine(created.id, scan_cursor=cursor)
    return created


def routine_jobs(store):
    return [job for job in store.list() if job.source == "routine"]


def notices(store, bot):
    return [m.text for m in store.list_messages(bot.id) if m.role == "system"]


def test_missed_daily_run_catches_up_once_with_one_notice(settings):
    store, scheduler, bot, clock = make(settings)
    routine(store, bot, "0 8 * * *", datetime(2026, 11, 13, 20, 0, tzinfo=NY))
    scheduler.tick()
    assert len(routine_jobs(store)) == 1
    assert notices(store, bot) == ['Missed 1 run of "Daily" (last due 8:00 AM ET) while Bot-Office was not '
                                   "running; running once now."]
    clock.moment += timedelta(minutes=1)
    scheduler.tick()
    scheduler.tick()
    assert len(routine_jobs(store)) == 1 and len(notices(store, bot)) == 1
    saved = store.list_routines(bot.id)[0]
    assert saved.last_run_at.astimezone(NY) == datetime(2026, 11, 14, 8, 0, tzinfo=NY)


def test_skip_policy_posts_notice_only(settings):
    store, scheduler, bot, _ = make(settings)
    routine(store, bot, "0 8 * * *", datetime(2026, 11, 13, 20, 0, tzinfo=NY), catch_up="skip")
    scheduler.tick()
    assert routine_jobs(store) == []
    assert notices(store, bot) == ['Missed 1 run of "Daily" (last due 8:00 AM ET) while Bot-Office was not '
                                   "running; skipped."]
    scheduler.tick()
    assert len(notices(store, bot)) == 1


def test_downtime_beyond_the_window_is_reported_without_a_count(settings):
    store, scheduler, bot, _ = make(settings)
    routine(store, bot, "0 * * * *", NOW - timedelta(days=3), name="Hourly")
    routine(store, bot, "0 8 * * 1", NOW - timedelta(days=3), name="Weekly")  # Monday 8:00, outside 24 h
    scheduler.tick()
    texts = notices(store, bot)
    # the 10:00 run is on time; the 24 hours before it hold 24 misses, and older ones were not counted
    assert texts[0] == ('Missed 24+ runs of "Hourly" (last due 9:00 AM ET) while Bot-Office was not running; '
                        "running once now.")
    assert texts[1] == ('Runs of "Weekly" may have been missed while Bot-Office was not running '
                        "(only the last 24 hours were checked); skipped.")
    assert len(routine_jobs(store)) == 1


def test_a_three_minute_tick_gap_still_fires_without_notice(settings):
    store, scheduler, bot, clock = make(settings)
    clock.moment = datetime(2026, 11, 14, 9, 7, 10, tzinfo=NY)
    routine(store, bot, "*/5 * * * *", datetime(2026, 11, 14, 9, 4, tzinfo=NY))
    scheduler.tick()
    assert len(routine_jobs(store)) == 1 and notices(store, bot) == []


def test_running_routine_is_not_stacked_and_owes_one_catch_up_run(settings):
    store, scheduler, bot, clock = make(settings)
    routine(store, bot, "*/5 * * * *", NOW - timedelta(minutes=1))
    scheduler.tick()  # 10:00 fires
    (first,) = routine_jobs(store)
    clock.moment += timedelta(minutes=10)  # the 10:00 run is still queued; 10:05 and 10:10 come due
    scheduler.tick()
    clock.moment += timedelta(minutes=1)
    scheduler.tick()
    assert len(routine_jobs(store)) == 1
    assert notices(store, bot) == ['Routine "Daily" came due at 10:10 AM ET while its previous run was still '
                                   "going; it will run once when that run finishes."]
    store.cancel(first.id)  # the previous run ends
    scheduler.tick()
    assert len(routine_jobs(store)) == 2 and len(notices(store, bot)) == 1
    clock.moment += timedelta(minutes=1)
    scheduler.tick()
    assert len(routine_jobs(store)) == 2  # owed only once


def test_skip_policy_notes_a_run_that_came_due_while_running(settings):
    store, scheduler, bot, clock = make(settings)
    routine(store, bot, "*/5 * * * *", NOW - timedelta(minutes=1), catch_up="skip")
    scheduler.tick()
    (first,) = routine_jobs(store)
    clock.moment += timedelta(minutes=5)
    scheduler.tick()
    assert notices(store, bot)[-1].endswith("while its previous run was still going; skipped (catch-up is off).")
    store.cancel(first.id)
    clock.moment += timedelta(minutes=1)
    scheduler.tick()
    assert len(routine_jobs(store)) == 1


def test_scan_of_a_routine_edited_meanwhile_does_nothing(settings, monkeypatch):
    store, scheduler, bot, clock = make(settings)
    scanned = routine(store, bot, "*/5 * * * *", NOW - timedelta(minutes=1))
    scanned = store.get_routine(scanned.id)
    # an edit (or disable, or delete) lands between the scan and the enqueue
    store.update_routine(scanned.id, prompt="new prompt")
    scheduler._fire_routine(scanned, NOW.replace(second=0))
    assert routine_jobs(store) == [] and store.get_routine(scanned.id).last_run_at is None
    store.update_routine(scanned.id, enabled=False)
    stale = store.get_routine(scanned.id)
    store.update_routine(scanned.id, enabled=True)
    assert store.advance_routine(stale, NOW) is False
    store.delete_routine(scanned.id)
    scheduler._fire_routine(stale.model_copy(update={"enabled": True}), NOW.replace(second=0))
    assert routine_jobs(store) == []
    # the current version fires as usual
    fresh = routine(store, bot, "*/5 * * * *", NOW - timedelta(minutes=1), name="Fresh")
    scheduler.tick()
    assert [job.prompt for job in routine_jobs(store)] == ["check things"]
    assert store.get_routine(fresh.id).last_run_at is not None


def test_legacy_edited_and_reenabled_routines_invent_no_misses(settings):
    store, scheduler, bot, clock = make(settings)
    clock.moment = datetime.now(NY)
    legacy = routine(store, bot, "0 8 * * *", None)  # pre-0.9.0 row: no cursor
    old = routine(store, bot, "0 8 * * *", clock.moment - timedelta(days=2), name="Old")
    store.update_routine(old.id, enabled=False)
    store.update_routine(old.id, enabled=True)  # time spent disabled is not missed
    edited = routine(store, bot, "0 8 * * *", clock.moment - timedelta(days=2), name="Edited")
    store.update_routine(edited.id, cron="0 9 * * *")
    scheduler.tick()
    assert notices(store, bot) == []
    assert all(r.scan_cursor is not None for r in store.list_routines(bot.id))
    assert legacy.id in {r.id for r in store.list_routines(bot.id)}


def test_failing_routine_is_logged_and_does_not_stop_the_others(settings, caplog):
    store, scheduler, bot, _ = make(settings)
    broken = routine(store, bot, "0 8 * * *", NOW - timedelta(minutes=5), name="Broken")
    with closing(store._connect()) as db:
        db.execute("UPDATE routines SET cron='bad' WHERE id=?", (str(broken.id),))
    routine(store, bot, "*/5 * * * *", NOW - timedelta(minutes=1), name="Fine")
    with caplog.at_level(logging.ERROR, logger="botoffice.scheduler"):
        scheduler.tick()
    assert len(routine_jobs(store)) == 1
    assert any(str(broken.id) in record.getMessage() for record in caplog.records)


def test_failing_inbox_file_is_logged_and_others_still_handled(settings, caplog, monkeypatch):
    store, scheduler, bot, _ = make(settings)
    inbox = settings.data_dir / "inboxes" / "_by-name"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "a.json").write_text('{"to": "Ada", "text": "one"}')
    (inbox / "b.json").write_text('{"to": "Ada", "text": "two"}')
    real = scheduler._handle_inbox_file

    def flaky(path):
        if path.name == "a.json":
            path.unlink()
            raise RuntimeError("boom")
        real(path)

    monkeypatch.setattr(scheduler, "_handle_inbox_file", flaky)
    with caplog.at_level(logging.ERROR, logger="botoffice.scheduler"):
        scheduler.tick()
    assert [job.source for job in store.list()] == ["inbox"]
    assert any("a.json" in record.getMessage() for record in caplog.records)


def test_enqueue_failure_advances_the_cursor_with_a_notice(settings):
    store, scheduler, bot, _ = make(settings, providers={"claude": FakeAdapter()})
    routine(store, bot, "0 8 * * *", datetime(2026, 11, 13, 20, 0, tzinfo=NY))
    scheduler.tick()
    scheduler.tick()
    assert routine_jobs(store) == []
    texts = notices(store, bot)
    assert len(texts) == 1 and texts[0].endswith('Routine "Daily" could not start: provider is disabled or unavailable')
