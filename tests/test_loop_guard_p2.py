"""0.11.13: the P2s of the loop-guard review: the consecutive ceiling, history counting, non-English
replies and which open questions block a task."""
from contextlib import closing

import pytest
from test_continuation import fresh, run_all, task_setup

from botoffice import autocontinue as ac
from botoffice.store import now


def turn_of(store, scheduler, item, job):
    """The Turn the guard sees after *job* (its history from the jobs table)."""
    book = scheduler.assignments
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM assignments WHERE id=?", (str(item.id),)).fetchone()
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (str(job.id),)).fetchone()
        return book._turn(db, row, job_row, "continue")


def edit_job(store, job, sql, *args):
    with closing(store._connect()) as db, db:
        db.execute(f"UPDATE jobs SET {sql} WHERE id=?", (*args, str(job.id)))


# --- P2-4: productive work is not paused at ten turns ---

def test_productive_work_runs_past_ten_turns_by_default(settings):
    store, scheduler, _, _, item = task_setup(settings, *fresh(14), "All finished.\nBOT-OFFICE-STATUS: done")
    jobs = run_all(store, scheduler)
    assert len(jobs) == 15 and scheduler.assignments.get(item.id).status == "done"


def test_the_dead_stop_reason_helper_is_gone():
    assert not hasattr(ac, "stop_reason")


# --- P2-5: history counting ---

def test_a_failed_turn_in_the_history_starts_the_run_after_it(settings):
    store, scheduler, _, _, item = task_setup(settings, *fresh(4))
    jobs = run_all(store, scheduler, limit=4)
    edit_job(store, jobs[1], "status='failed', outcome=NULL")  # turn 2 failed; 3 and 4 followed
    turn = turn_of(store, scheduler, item, jobs[3])
    assert turn.automatic == 2 and len(turn.run) == 2


def test_a_turn_that_never_ran_is_skipped_not_counted(settings):
    store, scheduler, _, _, item = task_setup(settings, *fresh(4))
    jobs = run_all(store, scheduler, limit=4)
    edit_job(store, jobs[1], "status='cancelled', outcome=NULL, started_at=NULL")
    turn = turn_of(store, scheduler, item, jobs[3])
    assert turn.automatic == 2 and len(turn.run) == 3  # turns 1 (the owner's, ends the run), 3 and 4


def test_a_cancelled_turn_that_ran_stops_the_run(settings):
    store, scheduler, _, _, item = task_setup(settings, *fresh(4))
    jobs = run_all(store, scheduler, limit=4)
    edit_job(store, jobs[1], "status='cancelled', outcome=NULL")
    turn = turn_of(store, scheduler, item, jobs[3])
    assert turn.automatic == 2


def test_the_settled_state_decides_for_the_finished_turn(settings):
    """A structured 'continue' report leaves the stored outcome empty; the run still counts it."""
    store, scheduler, _, _, item = task_setup(settings, *fresh(3))
    jobs = run_all(store, scheduler, limit=3)
    edit_job(store, jobs[2], "outcome=NULL")
    book = scheduler.assignments
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM assignments WHERE id=?", (str(item.id),)).fetchone()
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (str(jobs[2].id),)).fetchone()
        turn = book._turn(db, row, job_row, "continue", ("continue", "photo needed"))
    assert turn.run[-1][1] == "photo needed" and turn.automatic == 2


# --- P2-6: non-English replies ---

def test_distinct_cjk_replies_are_not_repeats():
    a, b = "今日はテストを書きました。設定を更新しました。", "別のモジュールを修正して、ログを確認しました。"
    assert ac.similarity(a, b) < ac.REPEAT_SIMILARITY
    assert ac.similarity(a, a) == 1.0
    assert ac.made_progress(b, [a]) is True and ac.made_progress(a, [a]) is False


def test_non_latin_words_are_tokens():
    assert ac.similarity("Проверил конфигурацию сервера", "Обновил документацию проекта") < 0.6
    assert ac.similarity("Überprüft Größe Straße", "überprüft größe straße") == 1.0


@pytest.mark.parametrize("a,b", [("", ""), ("!!!", "???"), ("ok", "so")])
def test_no_tokens_is_not_a_repeat(a, b):
    assert ac.similarity(a, b) == 0.0


# --- P2-7: only the task's own open question blocks it ---

def ask(store, bot, item, *, assignment, blocking=1):
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO questions VALUES (?,?,NULL,?,1,'{}','open',NULL,?,?,NULL)",
                   (f"q{assignment}{blocking}", str(bot.id), assignment, blocking, now().isoformat()))


def test_a_question_of_another_chat_or_task_does_not_block_the_task(settings):
    store, scheduler, _, bot, item = task_setup(settings, *fresh(2))
    jobs = run_all(store, scheduler, limit=2)
    ask(store, bot, item, assignment=None)
    ask(store, bot, item, assignment="some-other-task")
    assert turn_of(store, scheduler, item, jobs[1]).question_open is False


def test_the_tasks_own_open_question_still_blocks_it(settings):
    store, scheduler, _, bot, item = task_setup(settings, *fresh(2))
    jobs = run_all(store, scheduler, limit=2)
    ask(store, bot, item, assignment=str(item.id))
    assert turn_of(store, scheduler, item, jobs[1]).question_open is True
