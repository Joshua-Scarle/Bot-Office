"""0.12.20: evidence must name a check that did not fail, "could not do it" is not a done, and the
status-line rule reaches every engine (Claude included) on fresh and resumed sessions."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from botoffice import failures
from botoffice.api import create_app
from botoffice.domain import JobStatus
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from test_baseline import RecordingAdapter
from test_incomplete import H, Replying, chat, run_turn

B1_REPLY = """I can't tell you. This trial copy has no view of what the leads or DPEs have asked for, so I can't answer from real data.

- **No live state:** I have no GitHub, CI or Railway state to check.

For a real answer, a live Engineering Lead run could check the leads' boards.

BOT-OFFICE-STATUS: done — ran bot-task list, nothing open"""


@pytest.mark.parametrize("reason", ["", "ok", "all done now", "banana purple monkey", "finished the thing nicely"])
def test_words_that_name_no_check_stay_unverified(reason):
    assert failures.judge_evidence("done", reason)[0] == 0


@pytest.mark.parametrize("reason", [
    "pytest: 3 failed, 9 passed", "ran pytest tests/test_x.py, 2 failed", "ran the build, exit code 1",
    "ran curl against staging, got HTTP 500", "checked the test run: traceback in test_x", "tests failed, see the log",
    "ran lint, 4 errors", "ran pytest, tests failed; lint passed", "ran pytest, error during collection",
    "nothing new; ran pytest, 3 failed", "ran pytest, 1 failed; no fix yet, 8 passed"])
def test_failed_check_evidence_is_unverified_but_kept(reason):
    assert failures.judge_evidence("done", reason) == (0, reason)


@pytest.mark.parametrize("reason", [
    "ran pytest tests/test_x.py, 12 passed", "ran pytest, 0 failed", "ran the build: no errors, exit 0",
    "the failing test now passes, ran pytest tests/test_x.py", "checked the output file, matches", "nothing new",
    "ran pytest, 1 failed; fixed it and reran pytest, all passed"])
def test_real_checks_are_verified(reason):
    assert failures.judge_evidence("done", reason) == (1, reason)


def test_done_with_failed_check_is_recorded_done_unverified(settings):
    store, bot, job = run_turn(settings, "Fixed it.\nBOT-OFFICE-STATUS: done — ran pytest, 3 failed, 9 passed")
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "done" and job.verified == 0
    assert job.evidence == "ran pytest, 3 failed, 9 passed"  # kept, so the page can say what was cited
    message = [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1]
    assert message.unverified is True and message.text == "Fixed it."


def test_nonsense_done_is_unverified(settings):
    _, _, job = run_turn(settings, "Fixed it.\nBOT-OFFICE-STATUS: done — banana purple monkey")
    assert job.outcome == "done" and job.verified == 0


def test_couldnt_do_it_is_not_recorded_as_done(settings):
    store, bot, job = run_turn(settings, B1_REPLY)
    assert job.status == JobStatus.INCOMPLETE and job.outcome.startswith("blocked: marked done, but the reply says")
    assert job.verified is None
    notice = [text for role, text in chat(store, bot) if role == "system"][-1]
    assert notice.startswith("Not done (blocked): marked done, but the reply says the work could not be completed")


def test_couldnt_do_it_asking_the_owner_is_waiting_on_user(settings):
    _, _, job = run_turn(settings, "I could not log in to the site. Send me the key and I will go on.\n"
                                   "BOT-OFFICE-STATUS: done — ran the login, got an error")
    assert job.status == JobStatus.INCOMPLETE and job.outcome.startswith("waiting_on_user: marked done")


def test_status_reason_alone_is_enough(settings):
    _, _, job = run_turn(settings, "Here is what I have.\nBOT-OFFICE-STATUS: done — unable to reach the server")
    assert job.status == JobStatus.INCOMPLETE and job.outcome.startswith("blocked:")


@pytest.mark.parametrize("reply", [
    "Reviewed it. Couldn't find any problems in the logs.\n\nAll good.\nBOT-OFFICE-STATUS: done — read the logs end to end, no errors",
    "Fixed the bug; I could not reproduce the failure after the change.\nBOT-OFFICE-STATUS: done — ran pytest, 12 passed",
    "Done.\nBOT-OFFICE-STATUS: done — ran pytest tests/test_x.py, 12 passed, no data loss"])
def test_a_real_done_is_not_downgraded(settings, reply):
    _, _, job = run_turn(settings, reply)
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "done" and job.verified == 1


@pytest.mark.parametrize("reply", [
    "I couldn't access the old log, so I used the new log and completed the review.\nBOT-OFFICE-STATUS: done — checked the new log thoroughly",
    "No data was lost; the migration is complete.\nBOT-OFFICE-STATUS: done — checked the migrated records"])
def test_resolved_obstacles_and_negative_findings_stay_done(settings, reply):
    _, _, job = run_turn(settings, reply)
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "done" and job.verified == 1


def test_inability_in_the_middle_of_a_long_report_is_left_alone(settings):
    reply = "Report.\n\nStep 1 worked.\n\nI couldn't get the old log, so I used the new one.\n\nAll steps finished.\nBOT-OFFICE-STATUS: done — ran the script, output matches"
    _, _, job = run_turn(settings, reply)
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "done"


def test_missing_status_line_is_unverified_and_unchanged(settings):
    store, bot, job = run_turn(settings, "I couldn't do it.")
    assert job.outcome == "unverified" and job.status == JobStatus.SUCCEEDED
    assert [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1].unverified is True


def test_page_says_the_cited_evidence_failed():
    html = (__import__("pathlib").Path(__file__).parents[1] / "src/botoffice/static/bot-office.html").read_text()
    assert "does not show a passing check" in html


def _claude_prompts(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = RecordingAdapter()
    scheduler = Scheduler(store, settings, {"claude": adapter})
    bot = store.create_bot("Ada", "", "claude", None)
    scheduler.turns.post_message(bot, "hello")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"claude"})))
    return adapter.jobs[-1]


def test_claude_job_prompts_carry_the_status_line_rule(settings):
    """Claude has no separate system prompt in this desk: the whole prompt is its first user message, and a
    resumed session gets the short resume prompt. Both must carry the done-with-evidence line."""
    job = _claude_prompts(settings)
    for text in (job.prompt, job.resume_prompt):
        assert "BOT-OFFICE-STATUS: done — <what you checked, and how>" in text
        assert "Not verified" in text or "never done" in text


def test_numbered_rules_name_the_done_line_even_when_the_owner_empties_the_layers(settings):
    from botoffice.workspace import RULES
    assert "BOT-OFFICE-STATUS: done — <what you checked, and how>" in RULES
