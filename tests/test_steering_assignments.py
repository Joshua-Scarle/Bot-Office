"""0.9.1 steering x 0.10.0 assignments. Until 0.11.6 a message sent during an assignment turn never
steered it: it became a separate chat turn behind the running one, which looked like queuing. Steers now
enter assignment turns and still cost no budget."""

from test_loop import queued
from test_loop import setup as loop_setup

from botoffice.domain import JobStatus


def test_steer_during_an_assignment_turn_attaches_to_it_and_is_not_charged(settings):
    store, scheduler, _, bot, item = loop_setup(settings)
    turn = store.claim(["desktop-1"], {"codex"})
    assert turn.assignment_id == item.id
    used = scheduler.assignments.get(item.id).turns_used
    message, job = store.post_steer(bot.id, "also check the totals", [])
    assert str(job.id) == str(turn.id) and message.steer_job_id == turn.id
    assert scheduler.assignments.get(item.id).turns_used == used
    store.finish(turn.id, JobStatus.SUCCEEDED)
    scheduler._queue_pending_steers(turn.id)  # never taken by the CLI: runs next, as a plain chat turn
    converted = store.get(store.get_message(message.id).job_id)
    assert converted.source == "chat" and converted.assignment_id is None and converted.status == JobStatus.QUEUED
    assert scheduler.assignments.get(item.id).turns_used == used  # still not charged to the assignment


def test_queue_mode_message_during_an_assignment_turn_runs_before_the_next_continuation(settings):
    store, scheduler, _, bot, item = loop_setup(settings)
    turn = store.claim(["desktop-1"], {"codex"})
    _, job = scheduler.turns.post_message(bot, "also check the totals")  # mode "queue"
    assert job.assignment_id is None and job.source == "chat"
    used = scheduler.assignments.get(item.id).turns_used
    store.finish(turn.id, JobStatus.SUCCEEDED)
    claimed = store.claim(["desktop-1"], {"codex"})
    assert str(claimed.id) == str(job.id)
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
