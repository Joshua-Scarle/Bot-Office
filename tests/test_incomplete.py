"""0.9.0: a finished run is not a finished task; BOT-OFFICE-STATUS lines (A5)."""
import asyncio
import time

from fastapi.testclient import TestClient

from botoffice import failures
from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import RULES, build_resume_prompt

H = {"Authorization": "Bearer test-token"}


class Replying:
    name = "codex"
    enabled = True

    def __init__(self, reply):
        self.reply = reply

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        log_path.write_text("log")
        return RunResult("succeeded", exit_code=0, final_text=self.reply)


def run_turn(settings, reply):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": Replying(reply)})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "do the thing")
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, claimed))
    return store, bot, store.get(job.id)


def chat(store, bot):
    return [(m.role, m.text) for m in store.list_messages(bot.id) if m.role != "user"]


def test_blocked_status_line_makes_the_job_incomplete(settings):
    store, bot, job = run_turn(settings, "Did half of it.\n\nBOT-OFFICE-STATUS: blocked — need the API key")
    assert job.status == JobStatus.INCOMPLETE and job.failure_kind == failures.INCOMPLETE
    assert job.outcome == "blocked: need the API key" and job.reply_text == "Did half of it."
    assert chat(store, bot) == [("assistant", "Did half of it."), ("system", "Not done (blocked): need the API key")]
    # incomplete is never retried automatically, but the Retry button / API may re-run it
    assert job.attempts == 0 and store.retry_now(job.id).status == JobStatus.QUEUED


def test_not_done_and_waiting_on_user(settings):
    store, bot, job = run_turn(settings, "Started.\nBOT-OFFICE-STATUS: not_done - tests still failing")
    assert job.status == JobStatus.INCOMPLETE and chat(store, bot)[-1] == ("system", "Not done: tests still failing")


def test_done_line_is_stripped_and_missing_line_is_unverified(settings):
    store, bot, job = run_turn(settings, "All finished.\nBOT-OFFICE-STATUS: done")
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "done"
    assert chat(store, bot) == [("assistant", "All finished.")]
    store, bot, job = run_turn(settings.__class__(data_dir=settings.data_dir / "b", token="t", job_timeout=2), "Fine.")
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "unverified"


def test_status_line_only_counts_as_the_last_line():
    text, state, _ = failures.split_status("BOT-OFFICE-STATUS: blocked — x\nbut then I fixed it")
    assert state == "unverified" and text.startswith("BOT-OFFICE-STATUS")


def test_prompts_explain_the_status_line():
    assert "BOT-OFFICE-STATUS: not_done|blocked|waiting_on_user" in RULES
    from test_failures import _job
    assert "BOT-OFFICE-STATUS" in build_resume_prompt(_job())


def test_bot_to_bot_message_reports_not_done(settings):
    app = create_app(settings, {"codex": Replying("Partial.\nBOT-OFFICE-STATUS: waiting_on_user — which branch?")})
    with TestClient(app) as client:
        a = client.post("/v1/bots", headers=H, json={"name": "Alpha"}).json()
        client.post("/v1/bots", headers=H, json={"name": "Beta"})
        message = client.post(f'/v1/bots/{a["id"]}/message-agent', headers=H,
                              json={"to": "Beta", "body": "hello", "wait": True, "timeout": 3}).json()
        assert message["status"] == "not_done"
        store = app.state.store
        deadline = time.monotonic() + 3
        while not any("not done" in m.text for m in store.list_messages(a["id"])) and time.monotonic() < deadline:
            time.sleep(0.02)
        reply = [m.text for m in store.list_messages(a["id"]) if m.sender == "from Beta"]
        assert reply == ["Message not done (waiting on you): which branch?\n\nPartial."]


def test_unverified_reply_carries_a_subtle_flag_only(settings):
    store, bot, job = run_turn(settings, "Fine.")
    assert job.status == JobStatus.SUCCEEDED
    messages = [m for m in store.list_messages(bot.id) if m.role != "user"]
    assert [(m.role, m.text, m.unverified) for m in messages] == [("assistant", "Fine.", True)]
    store, bot, job = run_turn(settings.__class__(data_dir=settings.data_dir / "b", token="t", job_timeout=2),
                               "Fine.\nBOT-OFFICE-STATUS: done")
    assert not any(m.unverified for m in store.list_messages(bot.id))


class Failing(Replying):
    async def run(self, job, workspace, log_path, timeout):
        log_path.write_text("log")
        return RunResult("failed", exit_code=1, error="Beta's stderr: /secret/path token=abcdef1234",
                         failure_kind=failures.TOOL_DENIED)


def test_failed_bot_to_bot_message_reports_only_the_failure_kind(settings):
    app = create_app(settings, {"codex": Failing("")})
    with TestClient(app) as client:
        a = client.post("/v1/bots", headers=H, json={"name": "Alpha"}).json()
        client.post("/v1/bots", headers=H, json={"name": "Beta"})
        message = client.post(f'/v1/bots/{a["id"]}/message-agent', headers=H,
                              json={"to": "Beta", "body": "hello", "wait": True, "timeout": 3}).json()
        assert message["status"] == "failed"
        store = app.state.store
        deadline = time.monotonic() + 3
        while not any(m.sender == "from Beta" for m in store.list_messages(a["id"])) and time.monotonic() < deadline:
            time.sleep(0.02)
        reply = [m.text for m in store.list_messages(a["id"]) if m.sender == "from Beta"]
        assert reply == ["Message failed (tool denied)."]


def test_status_line_only_reply_never_falls_back_to_the_raw_log(settings):
    raw = '{"event":"step_update","step_update":{"tool_name":"run_command"}}'

    class LogReplying(Replying):
        async def run(self, job, workspace, log_path, timeout):
            log_path.write_text(raw * 50)
            return RunResult("succeeded", exit_code=0, final_text=self.reply)

    def turn(reply, sub):
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / f"{sub}.sqlite3")
        scheduler = Scheduler(store, settings, {"codex": LogReplying(reply)})
        bot = store.create_bot("Ada", "", "codex", None)
        scheduler.turns.post_message(bot, "do the thing")
        asyncio.run(scheduler._execute(Slot.DESKTOP_1, store.claim(Slot.DESKTOP_1, {"codex"})))
        return chat(store, bot)

    assert turn("BOT-OFFICE-STATUS: done — step2 written", "a") == [("assistant", "Done — step2 written")]
    assert turn("BOT-OFFICE-STATUS: done", "b") == [("assistant", "Done.")]
    assert turn("BOT-OFFICE-STATUS: continue — step1 written", "c") == [
        ("assistant", "(continue: step1 written)"), ("system", "Not done (continuing): step1 written")]
    assert turn("BOT-OFFICE-STATUS: blocked", "d")[0] == ("assistant", "(blocked)")
    # without a status line the old log fallback still applies
    assert turn("", "e") == [("assistant", (raw * 50)[-4000:])]
