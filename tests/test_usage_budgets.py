"""0.11.0 #10 usage per run, daily budgets, timelines and the model-free digest."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from botoffice import needs_you
from botoffice import usage as budgets
from botoffice.api import create_app
from botoffice.domain import JobStatus
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore, now

H = {"Authorization": "Bearer test-token"}


def ran(store, bot, source, minutes=1.0, usage=None, status="succeeded"):
    """A finished run that started *minutes* long ago today."""
    job = store.create("x", "codex", bot_id=bot.id, source=source)
    stamp = now()
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status=?, started_at=?, finished_at=?, usage_json=? WHERE id=?",
                   (status, (stamp - timedelta(minutes=minutes)).isoformat(), stamp.isoformat(),
                    json.dumps(usage) if usage else None, str(job.id)))
    return job


def budget(store, bot, **values):
    store.update_bot(bot.id, daily_budget_json=json.dumps(budgets.check_budget({**budgets.DEFAULT_BUDGET, **values})))


def test_turn_budget_holds_unattended_jobs_until_midnight_and_allow_releases(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    store.update_bot(bot.id, scheduled_runs="gated")
    budget(store, bot, unattended_turns=2)
    ran(store, bot, "routine")
    ran(store, bot, "trigger")
    ran(store, bot, "chat")  # Joshua's turns do not count
    held = store.create("check", "codex", bot_id=bot.id, source="routine")
    chat = store.create("hi", "codex", bot_id=bot.id, source="chat")
    claimed = store.claim(["desktop-1"], {"codex"})
    assert claimed.id == chat.id  # chat is never held
    store.finish(chat.id, JobStatus.SUCCEEDED)
    assert store.claim(["desktop-1"], {"codex"}) is None
    job = store.get(held.id)
    assert job.status == JobStatus.QUEUED and job.failure_kind == "budget"
    assert job.not_before == budgets.next_day(now()) and job.not_before.astimezone(budgets.DAY_TZ).hour == 0
    assert "2 unattended turns today (budget 2)" in job.error
    lines = [m.text for m in store.list_messages(bot.id) if m.role == "system"]
    assert lines == ["A routine turn waits until tomorrow: daily budget used (2 unattended turns today (budget 2))."]
    item = next(item for item in needs_you.needs_you(store) if item["kind"] == "budget")
    assert item["actions"] == ["allow"] and "1 turn(s)" in item["ask"]
    assert store.allow_budget(bot.id, 1, 0, 0) == 1
    assert store.claim(["desktop-1"], {"codex"}).id == held.id
    assert not [item for item in needs_you.needs_you(store) if item["kind"] == "budget"]


def test_minutes_and_token_caps_count_every_attempt_and_fail_closed_on_unknown(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    store.update_bot(bot.id, scheduled_runs="gated")
    job = store.create("x", "codex", bot_id=bot.id, source="wakeup")
    store.claim(["desktop-1"], {"codex"})
    assert store.requeue(job.id, now(), "timeout", error="slow", reply_text=None, max_per_hour=10,
                         usage={"input_tokens": 700, "output_tokens": 300})
    with closing(store._connect()) as db:
        db.execute("UPDATE job_attempts SET started_at=? WHERE job_id=?",
                   ((now() - timedelta(minutes=50)).isoformat(), str(job.id)))
        used = budgets.totals(budgets.runs(db, budgets.day_start(now()), now(), str(bot.id), unattended=True))
    assert used["turns"] == 1 and used["tokens"] == 1000 and used["minutes"] >= 49  # the queued retry is not a run
    budget(store, bot, unattended_minutes=45)
    with closing(store._connect()) as db:
        assert "unattended minutes" in budgets.over_budget(db, str(bot.id), now())
    budget(store, bot, unattended_minutes=None, tokens=5000)
    with closing(store._connect()) as db:
        assert budgets.over_budget(db, str(bot.id), now()) is None
    ran(store, bot, "inbox", usage=None)  # usage unknown
    with closing(store._connect()) as db:
        assert "did not report token use" in budgets.over_budget(db, str(bot.id), now())
    budget(store, bot, tokens=None)
    with closing(store._connect()) as db:
        assert budgets.over_budget(db, str(bot.id), now()) is None


def test_default_bots_hold_only_trigger_runs_and_gate_refusals_do_not_count(tmp_path):
    """scheduled_runs full (default): routines, wake-ups, assignments and inbox turns are
    reported but never held, as in 0.10.x; a run refused by the release gate never counts."""
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    assert bot.scheduled_runs == "full"
    budget(store, bot, unattended_turns=1)
    for source in ("routine", "wakeup", "assignment", "inbox"):
        ran(store, bot, source)
    routine = store.create("check", "codex", bot_id=bot.id, source="routine")
    assert store.claim(["desktop-1"], {"codex"}).id == routine.id
    store.finish(routine.id, JobStatus.SUCCEEDED)
    refused = ran(store, bot, "trigger", status="failed")
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET failure_kind='gate' WHERE id=?", (str(refused.id),))
        assert budgets.over_budget(db, str(bot.id), now()) is None
    trigger = store.create("event", "codex", bot_id=bot.id, source="trigger")
    assert store.claim(["desktop-1"], {"codex"}).id == trigger.id
    store.finish(trigger.id, JobStatus.SUCCEEDED)
    held = store.create("event", "codex", bot_id=bot.id, source="trigger")
    assert store.claim(["desktop-1"], {"codex"}) is None and store.get(held.id).failure_kind == "budget"
    report = store.usage_by_day(1)[0]
    assert report["budgeted"]["turns"] == 1 and report["unattended"]["turns"] == 7


def test_day_boundaries_are_new_york_days():
    stamp = datetime(2026, 11, 14, 4, 30, tzinfo=UTC)  # 23:30 ET on the 13th
    assert budgets.day_key(stamp) == "2026-11-13"
    assert budgets.next_day(stamp) == datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    assert budgets.day_start(stamp, 1) == datetime(2026, 11, 12, 5, 0, tzinfo=UTC)


class NoModel(FakeAdapter):
    async def run(self, *args, **kwargs):
        raise AssertionError("the digest must not call a model")


def test_digest_is_built_from_the_database_once_a_day(settings):
    store = JobStore(settings.data_dir / "db.sqlite3")
    settings.ensure_dirs()
    clock = {"now": datetime(2026, 11, 14, 12, 0, tzinfo=UTC)}  # 7:00 ET
    scheduler = Scheduler(store, settings, {"codex": NoModel()}, clock=lambda: clock["now"])
    ada = store.create_bot("Ada", "", "codex", None)
    store.update_bot(ada.id, digest=1)
    job = store.create("x", "codex", bot_id=ada.id, source="routine")
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='failed', failure_kind='timeout', started_at=?, finished_at=?, "
                   "usage_json=? WHERE id=?", ("2026-11-13T15:00:00+00:00", "2026-11-13T15:20:00+00:00",
                                               json.dumps({"input_tokens": 10, "output_tokens": 5}), str(job.id)))
    assert scheduler.post_digest() is False  # before 8:00 ET
    clock["now"] = datetime(2026, 11, 14, 13, 1, tzinfo=UTC)
    assert scheduler.post_digest() is True
    assert scheduler.post_digest() is False  # once a day
    text = [m.text for m in store.list_messages(ada.id) if m.role == "system"][-1]
    assert text.startswith("Daily digest for 2026-11-13 (no model call)")
    assert "Ada: 1 runs (routine 1), 20 min, 15 tokens; failures: timeout 1" in text
    assert "Open Needs-you items: 1." in text


def test_endpoints_master_only_and_budget_validation(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        bot = client.post("/v1/bots", headers=H, json={"name": "Ada"}).json()
        assert bot["daily_budget"] == {"unattended_turns": 40, "unattended_minutes": 120, "tokens": None}
        assert bot["digest"] is False
        bad = client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"daily_budget": {"unattended_turns": -1}})
        assert bad.status_code == 422
        saved = client.patch(f"/v1/bots/{bot['id']}", headers=H,
                             json={"daily_budget": {"unattended_turns": 5, "tokens": 100000}, "digest": True}).json()
        assert saved["daily_budget"] == {"unattended_turns": 5, "unattended_minutes": 120, "tokens": 100000}
        assert saved["digest"] is True
        routine = client.post(f"/v1/bots/{bot['id']}/routines", headers=H, json={"cron": "0 9 * * *", "prompt": "p"}).json()
        model = store.get_bot(bot["id"])
        job = store.create("p", "codex", bot_id=model.id, source="routine", routine_id=routine["id"])
        runs = client.get(f"/v1/routines/{routine['id']}/runs", headers=H).json()
        assert [run["job_id"] for run in runs] == [str(job.id)] and runs[0]["attempts"] == []
        day = client.get("/v1/usage", headers=H).json()
        assert day[0]["bot_name"] == "Ada" and day[0]["budget"]["unattended_turns"] == 5
        assert client.post(f"/v1/bots/{bot['id']}/budget/allow", headers=H, json={"turns": 3}).json() == {"released": 0}
        assert client.get("/v1/usage", headers=H).json()[0]["extra"]["turns"] == 3
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
        agent = {"Authorization": "Bearer " + store.issue_agent_token(store.get(job.id))}
        assert client.get("/v1/usage", headers=agent).status_code == 401
        assert client.post(f"/v1/bots/{bot['id']}/budget/allow", headers=agent).status_code == 401
        assert client.patch(f"/v1/bots/{bot['id']}", headers=agent, json={"daily_budget": {}}).status_code == 401


def test_assignment_timeline_has_jobs_events_and_approvals(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        bot = client.post("/v1/bots", headers=H, json={"name": "Ada"}).json()
        client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"scheduled_runs": "gated"})
        made = client.post(f"/v1/bots/{bot['id']}/assignments", headers=H,
                           json={"title": "T", "goal": "g", "done_when": "d"}).json()
        client.post(f"/v1/assignments/{made['id']}/start", headers=H, json={})
        timeline = client.get(f"/v1/assignments/{made['id']}/timeline", headers=H).json()
        assert {item["type"] for item in timeline} >= {"job", "event"}
        job_id = next(item["job_id"] for item in timeline if item["type"] == "job")
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        client.app.state.scheduler.rules.decide(
            store.get(job_id), {"tool_name": "Bash", "tool_input": {"command": "git push"}})
        timeline = client.get(f"/v1/assignments/{made['id']}/timeline", headers=H).json()
        assert any(item["type"] == "approval" and item["summary"] == "Bash: git push" for item in timeline)
        assert client.get("/v1/assignments/00000000-0000-0000-0000-000000000000/timeline", headers=H).status_code == 404


def test_memory_edit_is_a_job_event(settings):
    import asyncio

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.memory.write_bot(bot.id, "old fact\n")
    _, job = scheduler.turns.post_message(bot, "hi")

    class Writes(FakeAdapter):
        async def run(self, job, ws, log_path, timeout):
            (ws / "memory.md").write_text("old fact\nnew fact\n")
            return await super().run(job, ws, log_path, timeout)

    scheduler.providers["codex"] = Writes()
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    [event] = [event for event in store.list_events(job.id) if event["kind"] == "memory"]
    assert event["file"] == "memory" and event["diff"] == "@@ -1,0 +2 @@\n+new fact"
