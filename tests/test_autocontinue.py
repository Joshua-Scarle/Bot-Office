"""Run-until-done loop guards: no auto-continue on a blocked / needs-user reply, and a cap on
continues that make no progress (a real loop seen in testing)."""
import pytest
from test_loop import queued, run_all, setup

from botoffice import autocontinue as ac
from botoffice import continuation

CONT = "\nBOT-OFFICE-STATUS: continue — "

# Sanitized replay of the real loop: turns 8-17 of the stuck assignment (paraphrased, same shape).
# Every reply hedges about the same unresolved check; none asks for anything; every status is "continue".
REAL_LOOP = [
    "The answers fit the estimated columns, but the placement check failed for several lines. Nothing was printed." + CONT + "resolve the failed check",
    "Part of the placement failure comes from the check detecting the paper edge. One answer needs a closer check." + CONT + "verify clearances",
    "The closer check found one answer crosses printed text. The revised layout has not passed all checks yet." + CONT + "verify revised spacing",
    "One section passed the existing checks. It still needs a travel and clearance review." + CONT + "review the candidate",
    "A candidate layout exists, but the remaining sections still need reliable placement checks." + CONT + "resolve placement",
    "Possible placements found. They still need physical alignment checks; two answers remain unresolved." + CONT + "verify candidates",
    "The revised placement fits its column but is close to a clamp; the photo does not establish clearance." + CONT + "resolve layout",
    "Spacing found, but two lines are too close to printed text and the placement checks still fail." + CONT + "investigate safer placement",
    "A possible position found, but the wording needs more detail and clearance still needs checking." + CONT + "refine wording",
    "A specific answer fits and passes the checks, but the alignment still needs verification." + CONT + "verify layout",
]
FIRST_STEPS = [f"Step {n} done: produced {name}.{CONT}next" for n, name in
               enumerate(["the page map", "a draft layout", "a preview", "a file", "a summary"], 1)]


@pytest.mark.parametrize("text", [
    "I can’t verify the second pass from the current photo.",
    "The combined layout remains blocked by the placement check.",
    "Please send a straight overhead photo of the bed.",
    "I still need a photo of the holder before going on.",
    "Which size would you prefer?",
    "Waiting for your go-ahead.",
    "Done with step one. Do you want the long or short version?",
])
def test_needs_user_phrases_are_caught(text):
    assert ac.needs_user_phrase(text)


@pytest.mark.parametrize("text", [
    "Wrote the parser and its tests; all 14 pass.",
    "Step 3 of 5 finished. Next I will wire the settings page.",
    "Unblocked the CI job by pinning the dependency.",
    "The preview shows the first four answers fitting under their questions.",
    "I need to refactor the helper first, then the tests.",
])
def test_ordinary_progress_is_not_a_needs_user_signal(text):
    assert ac.needs_user_phrase(text) is None


def test_blocked_reply_with_continue_status_stops_the_loop(settings):
    store, scheduler, _, bot, item = setup(
        settings, "The second pass remains blocked by the placement check." + CONT + "keep trying")
    assert len(run_all(store, scheduler)) == 1
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and "blocked or needs you" in after.status_note
    assert not queued(store, item)
    assert any("waiting on user" in m.text for m in store.list_messages(bot.id) if m.role == "system")


def test_needs_input_in_the_status_reason_also_stops_it(settings):
    store, scheduler, _, _, item = setup(settings, "Looked at it." + CONT + "need a photo of the holder")
    assert len(run_all(store, scheduler)) == 1
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"


def test_open_blocking_question_stops_the_loop(settings):
    store, scheduler, _, bot, item = setup(settings, "Step one." + CONT + "next")
    with store._connect() as db:
        db.execute("INSERT INTO questions VALUES ('q1',?,NULL,?,1,'{}','open',NULL,1,'2026-11-18T00:00:00+00:00',NULL)",
                   (str(bot.id), str(item.id)))
    assert len(run_all(store, scheduler)) == 1
    assert scheduler.assignments.get(item.id).status == "waiting_on_user"
    assert not queued(store, item)


def test_replay_of_the_real_loop_pauses_after_three_turns_with_no_progress(settings):
    store, scheduler, _, _, item = setup(settings, *REAL_LOOP, budget_turns=20)
    jobs = run_all(store, scheduler)
    assert len(jobs) == 3  # it ran 11 turns in a row before the fix
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and "no progress" in after.status_note
    assert not queued(store, item)
    assert after.turns_used == 3 and after.status != "blocked"  # surfaced as needs-you, not a dead end


def test_real_loop_with_its_blocked_reply_stops_even_sooner(settings):
    replies = REAL_LOOP[:1] + ["The combined layout remains blocked by the placement check." + CONT + "resolve"]
    store, scheduler, *_ = setup(settings, *replies, *REAL_LOOP)
    assert len(run_all(store, scheduler)) == 2


def test_productive_continues_are_not_capped_early(settings):
    store, scheduler, _, _, item = setup(settings, *FIRST_STEPS, "All finished.\nBOT-OFFICE-STATUS: done")
    jobs = run_all(store, scheduler)
    assert len(jobs) == 6 and scheduler.assignments.get(item.id).status == "done"


def test_consecutive_continue_ceiling_even_when_every_reply_is_fresh(settings):
    fresh = [f"Completed unit {n}: wrote module{n} covering topic{n}{CONT}next" for n in range(40)]
    store, scheduler, _, _, item = setup(settings, *fresh, budget_turns=40)
    continuation.update(store, {"per_bot_day": None, "per_hour": None})  # the ceiling, not the 20/hour cap
    jobs = run_all(store, scheduler)
    # 0.11.10 one continuation system: the owner's Start plus 30 automatic turns (the limit counts automatic ones)
    assert len(jobs) == ac.MAX_CONSECUTIVE_CONTINUES + 1
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and "in a row without a reply" in after.status_note


def test_a_user_reply_resets_the_count_and_the_loop_resumes(settings):
    store, scheduler, _, _, item = setup(settings, *REAL_LOOP)
    assert len(run_all(store, scheduler)) == 3
    scheduler.assignments.reply(item.id, "Here are the photos.")
    assert scheduler.assignments.get(item.id).status == "active"
    assert len(run_all(store, scheduler)) == 3  # a fresh run: the user's turn plus two more, then paused again
    after = scheduler.assignments.get(item.id)
    assert after.status == "waiting_on_user" and after.turns_used == 6


def test_waiting_on_user_status_still_works_as_before(settings):
    store, scheduler, _, _, item = setup(settings, "Stuck.\nBOT-OFFICE-STATUS: waiting_on_user — need a key")
    assert len(run_all(store, scheduler)) == 1
    assert scheduler.assignments.get(item.id).status_note == "need a key"  # not rewritten by the guard


def test_repeating_the_same_reply_is_no_progress():
    same = "Reviewed the module layout and renamed three helpers in the config loader."
    assert ac.made_progress(same, []) is True
    assert ac.made_progress(same.upper() + "!!", [same]) is False
    assert ac.no_progress_run([(same, "")] * 4) == 3
