"""0.11.0 #10: a job parked on a usage limit gets one notice; the owner may retry it on another
provider (POST /v1/jobs/{id}/switch-provider). Never automatic."""
import asyncio
import re
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_retries import OK, H, Scripted

from botoffice import failures, needs_you
from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

SRC = Path(__file__).resolve().parents[1] / "src" / "botoffice"


class Engine(Scripted):
    def __init__(self, name, *results):
        super().__init__(*results)
        self.name, self.prompts = name, []

    async def run(self, job, workspace, log_path, timeout):
        self.prompts.append(job.prompt)
        return await super().run(job, workspace, log_path, timeout)


def limit(minutes):
    return RunResult("failed", exit_code=1, error="You've hit your usage limit", failure_kind=failures.USAGE_LIMIT,
                     retry_at=datetime.now(UTC) + timedelta(minutes=minutes))


def rig(settings, minutes=60, claude_results=(OK,)):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    codex, claude = Engine("codex", limit(minutes), OK), Engine("claude", *claude_results)
    scheduler = Scheduler(store, settings, {"codex": codex, "claude": claude})
    bot = store.create_bot("Ada", "", "codex", None)
    return store, scheduler, bot, codex, claude


def park(store, scheduler, bot, text="write the report"):
    """Run the bot's turn once on codex: it hits the usage limit and is parked (queued, not_before)."""
    _, job = scheduler.turns.post_message(bot, text)
    claimed = store.claim(Slot.DESKTOP_1, {"codex", "claude"})
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, claimed))
    saved = store.get(job.id)
    assert saved.status == JobStatus.QUEUED and saved.failure_kind == "usage_limit" and saved.not_before
    return saved


def notices(store, bot):
    return [m for m in store.list_messages(bot.id) if m.notice]


def items(store):
    return [item for item in needs_you.needs_you(store) if item["kind"] == "usage_limit"]


def test_parked_over_ten_minutes_gets_exactly_one_notice(settings):
    store, scheduler, bot, *_ = rig(settings, minutes=9)  # reset + 2 min grace = 11 min
    job = park(store, scheduler, bot)
    assert needs_you.post_usage_notices(store) == 1
    assert needs_you.post_usage_notices(store) == 0  # second tick: no duplicate
    scheduler.watchdog()
    [notice] = notices(store, bot)
    assert notice.role == "system" and notice.notice == {"kind": "usage_limit", "job_id": str(job.id),
                                                         "provider": "codex", "open": True}
    assert re.fullmatch(r"Ada is waiting for Codex's usage limit to reset \(back about \d\d:\d\d, 1[01] min\)\.",
                        notice.text)
    [item] = items(store)
    assert item["id"] == f"u:{job.id}:{job.attempts}" and item["actions"] == ["switch", "keep"]


def test_parked_under_ten_minutes_gets_no_notice(settings):
    store, scheduler, bot, *_ = rig(settings, minutes=7)  # 9 min parked
    park(store, scheduler, bot)
    assert needs_you.post_usage_notices(store) == 0 and not notices(store, bot) and not items(store)


def test_notice_clears_when_the_job_starts_or_is_cancelled(settings):
    store, scheduler, bot, *_ = rig(settings)
    job = park(store, scheduler, bot)
    needs_you.post_usage_notices(store)
    assert notices(store, bot) and items(store)
    with closing(store._connect()) as db:  # the reset time came: the job starts
        db.execute("UPDATE jobs SET not_before=? WHERE id=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                                                               str(job.id)))
    claimed = store.claim(Slot.DESKTOP_1, {"codex"})
    assert claimed.id == job.id and not notices(store, bot) and not items(store)
    # a second job, cancelled while parked
    store2, scheduler2, bot2, *_ = rig(settings.__class__(data_dir=settings.data_dir.parent / "two", token="t"))
    other = park(store2, scheduler2, bot2)
    needs_you.post_usage_notices(store2)
    assert notices(store2, bot2)
    store2.cancel(other.id)
    assert not notices(store2, bot2) and not items(store2)


def test_keep_waiting_closes_the_buttons_but_keeps_the_wait(settings):
    store, scheduler, bot, *_ = rig(settings)
    job = park(store, scheduler, bot)
    needs_you.post_usage_notices(store)
    assert store.keep_waiting(job.id) is True
    [notice] = notices(store, bot)
    assert notice.notice["open"] is False and not items(store)
    assert store.get(job.id).not_before and needs_you.post_usage_notices(store) == 0


def test_switch_runs_that_job_on_the_other_provider_with_the_handoff(settings):
    store, scheduler, bot, codex, claude = rig(settings)
    codex.results = [OK, limit(60), OK]  # one earlier turn on codex, so there is a session to hand off
    _, first = scheduler.turns.post_message(bot, "earlier turn")
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, store.claim(Slot.DESKTOP_1, {"codex"})))
    assert store.get(first.id).status == JobStatus.SUCCEEDED
    job = park(store, scheduler, bot)
    message, _ = store.post_steer(bot.id, "also add a legend", [])  # 0.10.5: a steer into the parked job
    assert message.steer_status == "pending"
    needs_you.post_usage_notices(store)
    switched = store.switch_provider(job.id, "claude", "claude-opus-5-5", "high", "codex is out of quota")
    assert switched.provider == "claude" and switched.model == "claude-opus-5-5" and switched.not_before is None
    assert switched.reason == "codex is out of quota" and switched.failure_kind == "usage_limit"
    assert not notices(store, bot) and not items(store)
    event = [e for e in store.list_events(job.id) if e["kind"] == "retry"][-1]
    assert event["switched_from"] == "codex" and event["switched_to"] == "claude"
    unchanged = store.get_bot(bot.id)
    assert unchanged.default_provider == "codex" and unchanged.model == bot.model
    claimed = store.claim(Slot.DESKTOP_1, {"codex", "claude"})
    assert claimed.id == job.id and claimed.provider == "claude"
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, claimed))
    assert store.get(job.id).status == JobStatus.SUCCEEDED and len(codex.prompts) == 2
    # the switched run gets the model-switch handoff and carries the parked steer
    assert "fresh session" in claude.prompts[0] and "also add a legend" in claude.prompts[0]
    assert store.get_message(message.id).steer_status == "delivered"


def test_cancel_after_switch_still_queues_pending_steers(settings):
    store, scheduler, bot, *_ = rig(settings)
    job = park(store, scheduler, bot)
    message, _ = store.post_steer(bot.id, "do X instead", [])
    store.switch_provider(job.id, "claude", "claude-opus-5-5", "high", "quota")
    store.cancel(job.id)
    queued = store.get_message(message.id)
    assert queued.steer_status == "queued" and store.get(queued.job_id).status == JobStatus.QUEUED


def test_scheduler_alone_never_switches_provider(settings):
    store, scheduler, bot, _codex, claude = rig(settings)
    job = park(store, scheduler, bot)
    for _ in range(3):
        scheduler.tick()
        scheduler.watchdog()
    saved = store.get(job.id)
    assert saved.provider == "codex" and saved.not_before and not claude.prompts
    assert not [e for e in store.list_events(job.id) if e.get("switched_to")]


def test_only_the_endpoint_writes_jobs_provider_after_creation():
    """Grep: no scheduler/retry/failure/notice path writes jobs.provider or calls switch_provider."""
    writes = re.compile(r"UPDATE\s+jobs\s+SET[^\"']*\bprovider\s*=", re.IGNORECASE)
    for name in ("scheduler.py", "needs_you.py", "failures.py", "turns.py", "assignments.py", "watchers.py",
                 "usage.py", "rules.py", "workspace.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert not writes.search(text) and "switch_provider(" not in text, name
    store_text = (SRC / "store.py").read_text(encoding="utf-8")
    assert len(writes.findall(store_text)) == 1
    body = store_text.split("    def switch_provider(", 1)[1].split("\n    def ", 1)[0]
    assert writes.search(body)
    callers = [path.name for path in SRC.rglob("*.py")
               if re.search(r"\.switch_provider\b", path.read_text(encoding="utf-8"))]
    assert callers == ["api.py"]


def test_switch_provider_endpoint(settings):
    codex, claude = Engine("codex", limit(60), OK), Engine("claude", OK)
    cursor = Engine("cursor", OK)

    async def signed_out():
        return "needs_login"

    cursor.login_status = signed_out
    app = create_app(settings, {"codex": codex, "claude": claude, "cursor": cursor})
    with TestClient(app) as client:
        store = app.state.store
        scheduler = app.state.scheduler
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        done = store.create("not parked", "codex", bot_id=bot["id"])
        # park a job by hand: claim, then the usage-limit requeue (the live loop is not racing it)
        scheduler._stopping = True
        parked = store.create("parked", "codex", bot_id=bot["id"])
        claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        while claimed and str(claimed.id) != str(parked.id):
            store.finish(claimed.id, JobStatus.SUCCEEDED)
            claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        store.requeue(parked.id, datetime.now(UTC) + timedelta(hours=1), failures.USAGE_LIMIT, error="limit",
                      reply_text=None, max_per_hour=10)
        url = f"/v1/jobs/{parked.id}/switch-provider"
        assert client.post(f"/v1/jobs/{done.id}/switch-provider", json={"provider": "claude", "reason": "r"},
                           headers=H).status_code == 409  # wrong state
        assert client.post(url, json={"provider": "codex"}, headers=H).status_code == 400  # same provider
        assert client.post(url, json={"provider": "cursor"}, headers=H).status_code == 400  # signed out
        assert client.post(url, json={"provider": "opencode"}, headers=H).status_code == 400  # disabled
        assert client.post(url, json={"provider": "claude", "reason": "r"}).status_code == 401  # owner only
        options = client.get(f"/v1/jobs/{parked.id}/switch-options", headers=H).json()
        assert options == [{"provider": "claude", "label": "Claude"}]
        needs_you.post_usage_notices(store)
        [item] = [i for i in client.get("/v1/needs-you", headers=H).json() if i["kind"] == "usage_limit"]
        assert item["options"] == options
        ok = client.post(url, json={"provider": "claude", "reason": "codex quota"}, headers=H)
        assert ok.status_code == 200 and ok.json()["provider"] == "claude" and ok.json()["not_before"] is None
        assert store.get(done.id).provider == "codex"  # only that job
        assert client.get(f"/v1/bots/{bot['id']}", headers=H).json()["default_provider"] == "codex"
        assert client.post(url, json={"provider": "codex"}, headers=H).status_code == 409  # no longer parked
        log = (settings.data_dir / "claude-use-log.tsv").read_text()
        assert str(parked.id) in log and "codex quota" in log


def test_needs_you_polling_does_not_spawn_login_checks_each_time(settings, monkeypatch):
    """Review C2 #2: Needs-you (polled every 16 s per tab) reuses the switch options for 60 s;
    each login check spawns a CLI. The switch itself checks fresh."""
    from botoffice import api as api_module

    codex, claude = Engine("codex", OK), Engine("claude", OK)
    calls = []

    async def status():
        calls.append(1)
        return "ok"

    claude.login_status = status
    app = create_app(settings, {"codex": codex, "claude": claude})
    with TestClient(app) as client:
        store, scheduler = app.state.store, app.state.scheduler
        bot = client.post("/v1/bots", json={"name": "Ada"}, headers=H).json()
        scheduler._stopping = True
        parked = store.create("parked", "codex", bot_id=bot["id"])
        claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        while claimed and str(claimed.id) != str(parked.id):
            store.finish(claimed.id, JobStatus.SUCCEEDED)
            claimed = store.claim(Slot.DESKTOP_1, {"codex"})
        store.requeue(parked.id, datetime.now(UTC) + timedelta(hours=1), failures.USAGE_LIMIT, error="limit",
                      reply_text=None, max_per_hour=10)
        needs_you.post_usage_notices(store)
        for _ in range(200):  # the startup warm probe also runs the login check; let it finish first
            if app.state.provider_status._value is not None:
                break
            time.sleep(0.05)
        calls.clear()
        for _ in range(5):
            [item] = [i for i in client.get("/v1/needs-you", headers=H).json() if i["kind"] == "usage_limit"]
            assert item["options"] == [{"provider": "claude", "label": "Claude"}]
            client.get(f"/v1/jobs/{parked.id}/switch-options", headers=H)
        assert len(calls) == 1
        monkeypatch.setattr(api_module, "SWITCH_OPTIONS_TTL", 0.0)
        client.get("/v1/needs-you", headers=H)
        assert len(calls) == 2  # expired: checked again
        monkeypatch.setattr(api_module, "SWITCH_OPTIONS_TTL", 60.0)
        assert client.post(f"/v1/jobs/{parked.id}/switch-provider", json={"provider": "claude", "reason": "r"},
                           headers=H).status_code == 200
        assert len(calls) == 3  # the switch never trusts the cache


def test_a_hidden_notice_does_not_stay_unread(settings):
    """Review C2 #3: once the job stops being parked the notice is hidden from the chat, so it
    must not keep the unread badge (or the notification count) up forever."""
    store, scheduler, bot, *_ = rig(settings)
    job = park(store, scheduler, bot)
    watermark = max(m.id for m in store.list_messages(bot.id))
    store.mark_read(bot.id, watermark)
    assert store.bot_unread(bot.id).unread_count == 0
    needs_you.post_usage_notices(store)
    assert store.bot_unread(bot.id).unread_count == 1  # shown, so unread until read
    store.cancel(job.id)
    assert not notices(store, bot)
    visible = [m for m in store.list_messages(bot.id) if m.id > watermark and m.role in ("assistant", "system")]
    assert store.bot_unread(bot.id).unread_count == len(visible)
    assert next(b for b in store.list_bots() if b.id == bot.id).unread_count == len(visible)
