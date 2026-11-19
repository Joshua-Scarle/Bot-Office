"""0.11.12: the three P1s of the loop-guard review (3199254, inside continuation.decide since 0.11.10):
a guard pause stays paused, productive replies are not mistaken for a stall, and a structured
``bot-task status continue`` report reaches the guard."""
from datetime import timedelta

import pytest
from test_continuation import due_now, fresh, run_all, system_lines, task_queued, task_setup

from botoffice import autocontinue as ac
from botoffice.assignments import AssignmentError
from botoffice.store import now

pytestmark = pytest.mark.continuation_defaults
CONT = "\nBOT-OFFICE-STATUS: continue — "
BLOCKED = "Still blocked on the photo of the holder." + CONT + "wait"


# --- P1-1: a guard pause is not revived without the owner ---

def test_a_guard_pause_is_not_revived_by_a_status_change_wakeup(settings):
    """The bot waits on its own task's status changing; the guard's pause is that change."""
    store, scheduler, _, bot, item = task_setup(settings, "Started.\nBOT-OFFICE-STATUS: continue — go", BLOCKED)
    book = scheduler.assignments
    wake = book.add_wakeup(bot.id, None, assignment_id=item.id, on_assignment=item.id, note="when it changes")
    run_all(store, scheduler)
    after = book.get(item.id)
    assert after.status == "waiting_on_user" and after.status_note.startswith("paused, not blocked")
    assert not task_queued(store, item)
    assert book._wakeup(wake.id).status == "cancelled"
    assert not [j for j in store.list() if j.source == "wakeup"]


def test_a_chat_wakeup_on_the_paused_task_does_not_resume_the_task(settings):
    store, scheduler, _, bot, item = task_setup(settings, BLOCKED)
    book = scheduler.assignments
    book.add_wakeup(bot.id, None, on_assignment=item.id, note="tell me")
    run_all(store, scheduler)
    assert book.get(item.id).status == "waiting_on_user"
    assert not [j for j in task_queued(store, item)]


def test_a_scheduled_wakeup_does_not_resume_a_guard_paused_task(settings):
    store, scheduler, _, bot, item = task_setup(settings, BLOCKED)
    book = scheduler.assignments
    run_all(store, scheduler)
    assert book.get(item.id).status == "waiting_on_user"
    with closing_db(store) as db:  # one that slipped in before the pause
        db.execute("INSERT INTO wakeups (id,bot_id,assignment_id,due_at,note,status,created_at) "
                   "VALUES ('w9',?,?,?,'later','pending',?)", (str(bot.id), str(item.id),
                   (now() - timedelta(minutes=1)).isoformat(), now().isoformat()))
    assert book.fire_due() == 0
    assert book.get(item.id).status == "waiting_on_user" and not task_queued(store, item)


def test_an_out_of_turn_bot_continue_does_not_resume_a_guard_paused_task(settings):
    store, scheduler, _, bot, item = task_setup(settings, BLOCKED)
    book = scheduler.assignments
    run_all(store, scheduler)
    with pytest.raises(AssignmentError) as refused:
        book.report(item.id, bot.id, store.list()[0].id, "continue", "keep going")
    assert refused.value.status == 409
    assert book.get(item.id).status == "waiting_on_user" and not task_queued(store, item)
    # the owner's reply still resumes it
    book.reply(item.id, "here is the photo")
    assert book.get(item.id).status == "active" and task_queued(store, item)


def closing_db(store):
    from contextlib import closing
    return closing(store._connect())


# --- P1-2: only the bot's own words about being stuck count ---

@pytest.mark.parametrize("reply", [
    "Fixed the failed test; all 14 pass now.",
    "Fixed the failure in the parser. The suite is green.",
    "The earlier failure is resolved: it was a stale fixture.",
    "Resolved the failing import; the build passes.",
    "Ran the suite:\n```\nFAILED tests/test_a.py::test_x - AssertionError\n1 failed, 13 passed\n```\nThat was the old behaviour; I updated the test and it passes.",
    "Updated the error text. The old message said:\n> failed to connect\nand now says what to check.",
])
def test_resolved_or_quoted_failure_words_are_progress(reply):
    assert ac.made_progress(reply, [])


@pytest.mark.parametrize("reply", [
    "The check failed for three lines.",
    "Two tests still fail after the change.",
    "The placement failure remains unresolved.",
])
def test_a_real_unresolved_failure_is_still_no_progress(reply):
    assert not ac.made_progress(reply, [])


@pytest.mark.parametrize("reply", [
    "Added the form handler. The log shows:\n```\nplease send the file again\n```\nI turned that into a constant.",
    "The error text now reads \"please send a photo\" and the test covers it.",
    "Renamed the prompt. The old one:\n> Please send me the photo\nis in the changelog.",
    "Added a helper `please_send_photo()` and wired it up.",
    "Wrote the tests:\n```python\nassert 'I am blocked' not in text\n```",
])
def test_quoted_or_code_text_is_not_the_bots_own_request(reply):
    assert ac.needs_user_phrase(reply) is None


@pytest.mark.parametrize("reply", [
    "Built the page.\nPlease send a photo of the bed so I can check the clearance.",
    "Built the page. I am blocked on the credentials.",
    "Built the page. Which one do you want?",
])
def test_the_bots_own_request_outside_code_still_counts(reply):
    assert ac.needs_user_phrase(reply)


def test_a_request_after_a_code_block_is_still_seen():
    text = "Output:\n```\nok\n```\nPlease send the photo."
    assert ac.needs_user_phrase(text)


def test_productive_turns_that_mention_a_fixed_failure_keep_going(settings):
    work = ["parser tokens", "router middleware", "cache eviction", "mailer templates", "billing invoices", "export csv"]
    more = ["lexer rewritten", "handlers merged", "ttl added", "html escaped", "rounding corrected", "header quoted"]
    replies = [f"Fixed the failed test for {w}: {m}. Ran it, it passes.{CONT}next" for w, m in zip(work, more)]
    store, scheduler, _, bot, item = task_setup(settings, *replies)
    run_all(store, scheduler, limit=6)
    after = scheduler.assignments.get(item.id)
    assert after.status == "active", (after.status, after.status_note)
    assert task_queued(store, item)


# --- P1-3: the structured report's reason reaches the guard ---

def test_a_structured_continue_report_with_a_needs_you_note_pauses(settings):
    holder = {}

    def reply(job):
        holder["book"].report(job.assignment_id, job.bot_id, job.id, "continue", "need a photo of the holder")
        return "Worked on the layout. Created the draft file."  # no status line

    store, scheduler, _, bot, item = task_setup(settings, reply)
    holder["book"] = scheduler.assignments
    run_all(store, scheduler)
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user", (after.status, after.status_note)
    assert "need a photo" in after.status_note or "blocked or needs you" in after.status_note
    assert not task_queued(store, item)


def test_a_structured_continue_report_without_a_blocker_still_continues(settings):
    holder = {}

    def reply(job):
        holder["book"].report(job.assignment_id, job.bot_id, job.id, "continue", "wire the settings page next")
        return "Wrote the parser and its tests."

    store, scheduler, _, bot, item = task_setup(settings, reply)
    holder["book"] = scheduler.assignments
    run_all(store, scheduler, limit=2)
    assert scheduler.assignments.get(item.id).status == "active" and task_queued(store, item)
