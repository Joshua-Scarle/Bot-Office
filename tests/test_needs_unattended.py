"""0.12.15 Needs-you lists unattended turns that end badly (blocked, waiting on the owner, failed, no
status line), grouped; a later success of the same routine or a dismissal clears them; quiet runs that
say done are not flagged; the platform rule reaches every unattended turn."""
import asyncio
from contextlib import closing

from fastapi.testclient import TestClient

from botoffice import needs_you, workspace
from botoffice.api import create_app
from botoffice.domain import Slot
from botoffice.providers.base import RunResult
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}
BLOCKED = "Could not run the check.\n\nBOT-OFFICE-STATUS: blocked — check module unavailable"


class Replying:
    name = "codex"
    enabled = True

    def __init__(self):
        self.reply, self.result = "", "succeeded"

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace_dir, log_path, timeout):
        log_path.write_text("log")
        if self.result == "failed":
            return RunResult("failed", exit_code=1, error="gh exploded", failure_kind="launch")
        return RunResult("succeeded", exit_code=0, final_text=self.reply)


class Desk:
    def __init__(self, settings):
        settings.ensure_dirs()
        self.store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        self.adapter = Replying()
        self.scheduler = Scheduler(self.store, settings, {"codex": self.adapter})
        self.bot = self.store.create_bot("Ada", "", "codex", None)
        self.routine = self.store.create_routine(self.bot.id, "0 8 * * *", "Check the mailbox", True, name="mailbox")

    def run(self, reply, source="routine", routine=True, result="succeeded"):
        self.adapter.reply, self.adapter.result = reply, result
        _, job = self.scheduler.turns.post_message(self.bot, "go", source=source,
                                                   routine_id=self.routine.id if routine else None)
        asyncio.run(self.scheduler._execute(Slot.DESKTOP_1, self.store.claim(Slot.DESKTOP_1, {"codex"})))
        return self.store.get(job.id)

    def items(self):
        return needs_you.needs_you(self.store)


def test_blocked_routine_run_appears_with_bot_name_reason_and_time(settings):
    desk = Desk(settings)
    job = desk.run(BLOCKED)
    (item,) = desk.items()
    assert item["kind"] == "unattended" and item["state"] == "blocked" and item["count"] == 1
    assert item["bot_name"] == "Ada" and item["reason"] == "check module unavailable"
    assert item["name"] == 'routine "mailbox"' and item["at"] == job.finished_at.isoformat()
    assert 'Ada\'s routine "mailbox" ended blocked: check module unavailable' in item["ask"]
    assert item["actions"] == ["dismiss"]


def test_repeats_group_into_one_item_with_a_count(settings):
    desk = Desk(settings)
    jobs = [desk.run(BLOCKED) for _ in range(4)]
    (item,) = desk.items()
    assert item["count"] == 4 and item["job_id"] == str(jobs[-1].id) and "(4 times since" in item["ask"]
    assert item["id"] == f"x:{jobs[0].id}:blocked"  # stable while the repeat count grows: no new notification
    # a different reason is its own item
    desk.run("Nope.\nBOT-OFFICE-STATUS: waiting_on_user — need the new token")
    assert sorted(i["state"] for i in desk.items()) == ["blocked", "waiting_on_user"]


def test_a_later_success_of_the_same_routine_clears_it(settings):
    desk = Desk(settings)
    desk.run(BLOCKED)
    # another routine succeeding does not clear it
    other = desk.store.create_routine(desk.bot.id, "0 9 * * *", "Other", True, name="other")
    desk.adapter.reply = "ok\nBOT-OFFICE-STATUS: done — ran the routine check, all passed"
    _, job = desk.scheduler.turns.post_message(desk.bot, "go", source="routine", routine_id=other.id)
    asyncio.run(desk.scheduler._execute(Slot.DESKTOP_1, desk.store.claim(Slot.DESKTOP_1, {"codex"})))
    assert [i["state"] for i in desk.items()] == ["blocked"]
    desk.run("Fixed.\nBOT-OFFICE-STATUS: done — reran it, passes now")
    assert desk.items() == []


def test_quiet_when_nothing_changed_is_not_flagged(settings):
    desk = Desk(settings)
    desk.run("Nothing new.\nBOT-OFFICE-STATUS: done — nothing new")
    desk.run("BOT-OFFICE-STATUS: done — nothing new")
    assert desk.items() == []


def test_a_routine_without_a_status_line_is_flagged_until_one_has_it(settings):
    desk = Desk(settings)
    desk.run("NO_NEW_COMMENTS")
    desk.run("NO_NEW_COMMENTS")
    (item,) = desk.items()
    assert item["state"] == "no_status" and item["count"] == 2 and "without a status line" in item["ask"]
    desk.run("NO_NEW_COMMENTS")  # still no line: not cleared
    assert desk.items()[0]["count"] == 3
    desk.run("All quiet.\nBOT-OFFICE-STATUS: done — nothing new")
    assert desk.items() == []


def test_failed_routine_runs_group_too(settings):
    desk = Desk(settings)
    for _ in range(3):
        desk.run("", result="failed")
    items = [i for i in desk.items() if i["kind"] == "job"]
    assert len(items) == 1 and items[0]["count"] == 3 and items[0]["actions"] == ["retry", "dismiss"]
    assert "gh exploded" in items[0]["ask"] and 'routine "mailbox"' in items[0]["ask"]


def test_live_chat_turns_and_assignment_ends_are_not_listed_here(settings):
    desk = Desk(settings)
    desk.run(BLOCKED, source="chat", routine=False)  # the owner is watching a chat turn
    assert desk.items() == []
    desk.run(BLOCKED, source="inbox", routine=False)  # a bot's message: nobody watches
    assert [i["state"] for i in desk.items()] == ["blocked"]


def test_dismissing_clears_the_whole_group_and_a_new_failure_returns(settings):
    desk = Desk(settings)
    for _ in range(3):
        desk.run(BLOCKED)
    (item,) = desk.items()
    with TestClient(create_app(settings, {"codex": Replying()})) as client:
        assert client.post(f"/v1/jobs/{item['job_id']}/dismiss", headers=H).status_code == 204
        assert desk.items() == []
        assert client.get("/v1/needs-you", headers=H).json() == []
    desk.run(BLOCKED)
    (again,) = desk.items()
    assert again["count"] == 1 and again["id"] != item["id"]


def test_the_api_lists_it_for_the_ui(settings):
    desk = Desk(settings)
    desk.run(BLOCKED)
    with TestClient(create_app(settings, {"codex": Replying()})) as client:
        (item,) = client.get("/v1/needs-you", headers=H).json()
        assert item["kind"] == "unattended" and item["bot_name"] == "Ada"


def test_every_unattended_turn_gets_the_report_errors_rule(settings):
    desk = Desk(settings)
    for source, expected in (("routine", True), ("wakeup", True), ("trigger", True), ("inbox", True),
                             ("chat", False), ("api", False), ("assignment", False)):
        job = desk.store.create("x", "codex", bot_id=desk.bot.id, source=source)
        for prompt in (workspace.build_prompt(desk.bot, job), workspace.build_resume_prompt(job)):
            assert ("Never stay quiet about a problem" in prompt) is expected, (source, prompt[-200:])
    job = desk.store.create("x", "codex", bot_id=desk.bot.id, source="routine")
    assert "Staying quiet when nothing changed is fine" in workspace.build_resume_prompt(job)


def test_a_bot_message_reply_without_a_status_line_is_flagged_too(settings):
    desk = Desk(settings)
    desk.run("Here you go.", source="inbox", routine=False)
    assert [(i["state"], i["name"]) for i in desk.items()] == [("no_status", "a bot message")]


def test_failed_group_keeps_its_id_as_repeats_arrive_and_reasons_stay_apart(settings):
    desk = Desk(settings)
    desk.run("", result="failed")
    first = [i for i in desk.items() if i["kind"] == "job"][0]["id"]
    desk.run("", result="failed")
    assert [i["id"] for i in desk.items() if i["kind"] == "job"] == [first]
    desk.run("x\nBOT-OFFICE-STATUS: blocked — HTTP 401 from the API")
    desk.run("x\nBOT-OFFICE-STATUS: blocked — HTTP 500 from the API")
    assert sorted(i["reason"] for i in desk.items() if i["state"] == "blocked") == [
        "HTTP 401 from the API", "HTTP 500 from the API"]


def test_a_repeated_dismiss_click_does_not_clear_newer_failures(settings):
    desk = Desk(settings)
    old = desk.run(BLOCKED)
    assert needs_you.dismiss_job(desk.store, str(old.id))
    desk.run(BLOCKED)
    assert needs_you.dismiss_job(desk.store, str(old.id))  # the stale second click
    assert [i["count"] for i in desk.items()] == [1]


def test_an_unattended_done_without_evidence_is_listed_until_a_checked_done_follows(settings):
    desk = Desk(settings)
    desk.run("Did it.\nBOT-OFFICE-STATUS: done")
    items = desk.items()
    assert [i["state"] for i in items] == ["no_evidence"] and "nothing to show it was checked" in items[0]["title"]
    desk.run("Did it.\nBOT-OFFICE-STATUS: done")
    assert [(i["state"], i["count"]) for i in desk.items()] == [("no_evidence", 2)]
    desk.run("Did it.\nBOT-OFFICE-STATUS: done — ran pytest, 12 passed")
    assert desk.items() == []
