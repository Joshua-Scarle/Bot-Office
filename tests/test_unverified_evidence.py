"""0.12.16: unverified results are obvious, a done needs evidence, a cut reply says so and keeps its full text."""
from fastapi.testclient import TestClient

from botoffice import failures
from botoffice.api import create_app
from botoffice.scheduler import REPLY_LIMIT
from test_incomplete import H, Replying, run_turn

HTML = (__import__("pathlib").Path(__file__).parents[1] / "src/botoffice/static/bot-office.html").read_text()


def reply(store, bot):
    return [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1]


def test_judge_evidence():
    assert failures.judge_evidence("done", "") == (0, "")
    assert failures.judge_evidence("done", "ok") == (0, "")
    assert failures.judge_evidence("done", "all done now") == (1, "all done now")  # words, not proof: see below
    assert failures.judge_evidence("done", "nothing new") == (1, "nothing new")
    assert failures.judge_evidence("done", "ran pytest tests/test_x.py: 12 passed") == (1, "ran pytest tests/test_x.py: 12 passed")
    assert failures.judge_evidence("blocked", "need key") == (None, "")


def test_done_without_evidence_is_marked_unverified(settings):
    store, bot, job = run_turn(settings, "Fixed it.\n\nBOT-OFFICE-STATUS: done")
    assert job.outcome == "done" and job.verified == 0 and not job.evidence
    message = reply(store, bot)
    assert message.unverified is True and message.evidence is None and message.text == "Fixed it."


def test_done_with_evidence_shows_it(settings):
    store, bot, job = run_turn(settings, "Fixed it.\n\nBOT-OFFICE-STATUS: done — ran pytest tests/test_x.py, 12 passed")
    assert job.outcome == "done" and job.verified == 1 and job.evidence == "ran pytest tests/test_x.py, 12 passed"
    message = reply(store, bot)
    assert message.unverified is False and message.evidence == "ran pytest tests/test_x.py, 12 passed"


def test_missing_status_line_stays_unverified(settings):
    store, bot, job = run_turn(settings, "Fixed it.")
    assert job.outcome == "unverified" and job.verified is None
    assert reply(store, bot).unverified is True


def test_long_reply_is_cut_with_a_notice_and_the_full_text_is_reachable(settings):
    full = "x" * (REPLY_LIMIT + 1234)
    store, bot, job = run_turn(settings, full + "\nBOT-OFFICE-STATUS: done — checked by reading it back, 3 steps")
    message = reply(store, bot)
    assert len(message.text) == REPLY_LIMIT and message.cut_chars == 1234
    assert job.reply_text == full
    with TestClient(create_app(settings, {"codex": Replying("")})) as client:
        body = client.get(f"/v1/jobs/{job.id}", headers=H).json()["job"]
        assert body["reply_text"] == full
        shown = [m for m in client.get(f"/v1/bots/{bot.id}/messages", headers=H).json() if m["role"] == "assistant"][-1]
        assert shown["cut_chars"] == 1234 and shown["job_id"] == str(job.id)


def test_short_reply_has_no_notice(settings):
    store, bot, job = run_turn(settings, "Short.\nBOT-OFFICE-STATUS: done — read the file back, matches")
    assert reply(store, bot).cut_chars == 0


def test_page_has_the_badge_the_evidence_line_and_the_cut_notice():
    assert '"Not verified"' in HTML and "Verified by: " in HTML
    assert "This reply was cut at 20,000 characters; " in HTML and "Show full reply" in HTML


def test_prompts_ask_for_evidence_on_done():
    from botoffice.workspace import RULES
    from pathlib import Path
    prompts = Path(failures.__file__).parent / "prompts"
    assert "done — <evidence>" in (prompts / "system.md").read_text()
    assert "done — <evidence>" in (prompts / "rules-for-all-bots.md").read_text()


def test_full_reply_button_keeps_a_failure_line_and_updates_the_notice():
    assert 'm.role === "system"' in HTML and "Showing the full reply (" in HTML
