"""0.11.9: the baseline charter every bot gets, owner control over it, and the resume nudge."""
import asyncio
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from botoffice import baseline
from botoffice.api import create_app
from botoffice.catalog import ENGINES
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import RULES, STANDING_RULES_NOTICE, prepare_bot_workspace

OWNER = {"Authorization": "Bearer test-token"}


class RecordingAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.jobs = []

    async def run(self, job, workspace, log_path, timeout):
        self.jobs.append(job)
        return await super().run(job, workspace, log_path, timeout)


def _setup(settings, engine="codex"):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = RecordingAdapter()
    scheduler = Scheduler(store, settings, {engine: adapter})
    bot = store.create_bot("Ada", "", engine, None)  # 0.11.8: Claude needs no reason
    return store, scheduler, adapter, bot


def _prepare(store, scheduler, bot, job):
    workspace = scheduler.settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True, exist_ok=True)
    prepare_bot_workspace(store, scheduler.memory, bot, job, workspace)
    return workspace


def test_default_baseline_is_short_and_covers_the_essentials():
    text = baseline.default_text()
    assert len(text.split()) < 1200
    for needed in ("waiting_on_user", "bot-msg", "bot-task create --for", "bot-task wake", "memory.md",
                   "WORKING.md", "shared-memory.md", "never", "Verify", "AGENTS.md"):
        assert needed in text
    assert "state the reason" not in text


@pytest.mark.parametrize("engine", ENGINES)
def test_fresh_prompt_carries_the_baseline_on_every_engine(settings, engine):
    store, scheduler, adapter, bot = _setup(settings, engine)
    scheduler.turns.post_message(bot, "hello")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {engine})))
    job = adapter.jobs[-1]
    assert baseline.default_text().strip() in job.prompt
    # layered before the owner's standing rules and the non-overridable Rules
    assert job.prompt.index("--- BASELINE.md ---") < job.prompt.index(STANDING_RULES_NOTICE) < job.prompt.index("Rules:\n1.")
    workspace = scheduler.settings.data_dir / "workspaces" / str(job.id)
    assert baseline.default_text() in (workspace / "BASELINE.md").read_text()
    assert "BASELINE.md" in (workspace / "SELF.md").read_text()
    # the turn succeeded, so the next resumed turn is not nudged again
    _, second = scheduler.turns.post_message(bot, "again")
    ws = _prepare(store, scheduler, bot, second)
    assert baseline.CHANGED_NOTICE not in (ws / "prompt-resume.txt").read_text()


def test_owner_override_is_used_and_reset_restores_default(settings):
    store, scheduler, _, bot = _setup(settings)
    baseline.set_override(store, "Custom charter: always answer in haiku.\n")
    _, job = scheduler.turns.post_message(bot, "hello")
    ws = _prepare(store, scheduler, bot, job)
    prompt = (ws / "prompt.txt").read_text()
    assert "Custom charter: always answer in haiku." in prompt
    assert baseline.default_text().strip() not in prompt
    assert "Custom charter" in (ws / "BASELINE.md").read_text()
    baseline.reset(store)
    ws = _prepare(store, scheduler, bot, job)
    assert baseline.default_text().strip() in (ws / "prompt.txt").read_text()
    # an emptied baseline leaves no block in the prompt but keeps the safety rules
    baseline.set_override(store, "")
    ws = _prepare(store, scheduler, bot, job)
    prompt = (ws / "prompt.txt").read_text()
    assert "--- BASELINE.md ---" not in prompt and RULES.splitlines()[0] in prompt
    with pytest.raises(ValueError):
        baseline.set_override(store, "x" * (baseline.MAX_CHARS + 1))


def test_resume_nudge_only_when_the_baseline_changed(settings):
    store, scheduler, _, bot = _setup(settings)
    _, job = scheduler.turns.post_message(bot, "hello")
    # a session that started before baselines existed is told once
    assert baseline.CHANGED_NOTICE in (_prepare(store, scheduler, bot, job) / "prompt-resume.txt").read_text()
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    _, second = scheduler.turns.post_message(bot, "again")
    assert baseline.CHANGED_NOTICE not in (_prepare(store, scheduler, bot, second) / "prompt-resume.txt").read_text()
    baseline.set_override(store, "New charter\n")
    resume = (_prepare(store, scheduler, bot, second) / "prompt-resume.txt").read_text()
    assert resume.count(baseline.CHANGED_NOTICE) == 1
    assert "New charter" not in resume  # a one-line nudge, not the text


def test_failed_turn_keeps_the_nudge(settings):
    store, scheduler, adapter, bot = _setup(settings)
    adapter.result = "failed"
    scheduler.turns.post_message(bot, "hello")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    assert not (scheduler.memory.bot_dir(bot.id) / baseline.SNAPSHOT).exists()


def test_api_is_owner_only(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        store = app.state.store
        state = client.get("/v1/baseline", headers=OWNER).json()
        assert state["customized"] is False and state["text"] == state["default"] == baseline.default_text()
        saved = client.put("/v1/baseline", headers=OWNER, json={"text": "Owner charter"}).json()
        assert saved["customized"] is True and saved["text"] == "Owner charter"
        assert client.get("/v1/baseline", headers=OWNER).json()["text"] == "Owner charter"
        too_long = client.put("/v1/baseline", headers=OWNER, json={"text": "x" * (baseline.MAX_CHARS + 1)})
        assert too_long.status_code == 422

        bot = store.create_bot("Ada", "", "codex", None)
        job = store.create("work", "codex", bot_id=bot.id, source="chat")
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        assert client.get("/v1/baseline", headers=agent).status_code == 401
        assert client.put("/v1/baseline", headers=agent, json={"text": "bot charter"}).status_code == 401
        assert client.delete("/v1/baseline", headers=agent).status_code == 401
        assert client.put("/v1/baseline", json={"text": "anon"}).status_code == 401
        assert baseline.effective(store) == "Owner charter"

        reset = client.delete("/v1/baseline", headers=OWNER).json()
        assert reset["customized"] is False and reset["text"] == baseline.default_text()


def test_settings_ui_has_baseline_section():
    from importlib.resources import files
    html = files("botoffice").joinpath("static/bot-office.html").read_text()
    for needed in ('id="office-settings"', 'id="baseline-panel"', "Baseline instructions", 'id="baseline-reset"',
                   "Reset to default", '"/v1/baseline"'):
        assert needed in html
