"""0.9.1 steering x 0.10.0 assignments: steers never enter assignment turns or their budgets."""

from test_loop import queued
from test_loop import setup as loop_setup

from botoffice.domain import JobStatus


def test_steer_during_an_assignment_turn_becomes_a_plain_chat_turn(settings):
    store, scheduler, _, bot, item = loop_setup(settings)
    turn = store.claim(["desktop-1"], {"codex"})
    assert turn.assignment_id == item.id
    # the API's steer mode falls back to a queued chat turn: no stream injection, no interrupt
    assert store.post_steer(bot.id, "also check the totals", []) is None
    message, job = scheduler.turns.post_message(bot, "also check the totals")
    assert message.steer_job_id is None and job.assignment_id is None and job.source == "chat"
    used = scheduler.assignments.get(item.id).turns_used
    store.finish(turn.id, JobStatus.SUCCEEDED)
    claimed = store.claim(["desktop-1"], {"codex"})
    assert str(claimed.id) == str(job.id)  # the user's message runs before the next continuation
    assert scheduler.assignments.get(item.id).turns_used == used


def test_converted_steer_is_not_superseded_by_an_assignment_reply(settings):
    store, scheduler, _, bot, item = loop_setup(settings)
    _, chat = scheduler.turns.post_message(bot, "hello")
    loop_turns = [j.id for j in queued(store, item)]
    for job_id in loop_turns:  # let the chat turn run first
        store.finish(job_id, JobStatus.CANCELLED)
    running = store.claim(["desktop-1"], {"codex"})
    assert str(running.id) == str(chat.id)
    steer, _ = store.post_steer(bot.id, "steer", [])
    store.finish(running.id, JobStatus.SUCCEEDED)
    scheduler._queue_pending_steers(running.id)
    converted = store.get(store.get_message(steer.id).job_id)
    assert converted.status == JobStatus.QUEUED and converted.assignment_id is None
    scheduler.assignments.reply(item.id, "go on")
    assert store.get(converted.id).status == JobStatus.QUEUED
