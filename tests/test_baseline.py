"""0.11.9/0.11.10: the two owner-side layers every bot gets (System prompt, Rules for all bots), owner
control over them, the resume nudge, and the Settings page."""
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
from botoffice.workspace import STANDING_RULES_NOTICE, prepare_bot_workspace

OWNER = {"Authorization": "Bearer test-token"}
SYSTEM, RULES = baseline.SYSTEM, baseline.RULES


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
    bot = store.create_bot("Ada", "", engine, None)
    return store, scheduler, adapter, bot


def _prepare(store, scheduler, bot, job):
    workspace = scheduler.settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True, exist_ok=True)
    prepare_bot_workspace(store, scheduler.memory, bot, job, workspace)
    return workspace


def test_default_layers_are_short_and_split_cleanly():
    rules, system = RULES.default_text(), SYSTEM.default_text()
    assert len(rules.split()) < 450 and len(rules.split()) + len(system.split()) < 1300  # 0.12.17: +30 words for the done-needs-evidence rule
    # the owner's own words, in substance
    for needed in ("Keep working on your task until", "the goal is met", "stuck and need the user's help",
                   "another rule or instruction tells you to stop", "not an endless goal",
                   "Suggest next tasks instead of starting them", "waiting_on_user", "Verify"):
        assert needed in rules
    for needed in ("bot-msg", "bot-task create --for", "bot-task wake", "memory.md", "WORKING.md",
                   "shared-memory.md", "AGENTS.md", "RULES-FOR-ALL-BOTS.md", "not_done"):
        assert needed in system
    # agency rules live in one place only
    assert "Finish what you start" not in system and "bot-msg" not in rules
    assert "state the reason" not in rules + system


@pytest.mark.parametrize("engine", ENGINES)
def test_fresh_prompt_carries_both_layers_in_order_on_every_engine(settings, engine):
    store, scheduler, adapter, bot = _setup(settings, engine)
    scheduler.turns.post_message(bot, "hello")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {engine})))
    prompt = adapter.jobs[-1].prompt
    assert SYSTEM.default_text().strip() in prompt and RULES.default_text().strip() in prompt
    # system prompt, then rules for all bots, then the owner's standing rules, then the numbered Rules
    assert (prompt.index("--- SYSTEM-PROMPT.md ---") < prompt.index("--- RULES-FOR-ALL-BOTS.md ---")
            < prompt.index(STANDING_RULES_NOTICE) < prompt.index("Rules:\n1."))
    workspace = scheduler.settings.data_dir / "workspaces" / str(adapter.jobs[-1].id)
    assert SYSTEM.default_text() in (workspace / "SYSTEM-PROMPT.md").read_text()
    assert RULES.default_text() in (workspace / "RULES-FOR-ALL-BOTS.md").read_text()
    assert not (workspace / "BASELINE.md").exists()
    assert "RULES-FOR-ALL-BOTS.md" in (workspace / "SELF.md").read_text()
    # the turn succeeded, so the next resumed turn is not nudged
    _, second = scheduler.turns.post_message(bot, "again")
    resume = (_prepare(store, scheduler, bot, second) / "prompt-resume.txt").read_text()
    assert SYSTEM.changed_notice() not in resume and RULES.changed_notice() not in resume


def test_owner_overrides_are_used_and_reset_restores_defaults(settings):
    store, scheduler, _, bot = _setup(settings)
    RULES.set_override(store, "Custom rule: always answer in haiku.\n")
    SYSTEM.set_override(store, "Custom system page.\n")
    _, job = scheduler.turns.post_message(bot, "hello")
    ws = _prepare(store, scheduler, bot, job)
    prompt = (ws / "prompt.txt").read_text()
    assert "Custom rule: always answer in haiku." in prompt and "Custom system page." in prompt
    assert RULES.default_text().strip() not in prompt and SYSTEM.default_text().strip() not in prompt
    RULES.reset(store)
    SYSTEM.reset(store)
    prompt = (_prepare(store, scheduler, bot, job) / "prompt.txt").read_text()
    assert RULES.default_text().strip() in prompt and SYSTEM.default_text().strip() in prompt
    # an emptied layer leaves no block in the prompt; the numbered Rules stay
    RULES.set_override(store, "")
    prompt = (_prepare(store, scheduler, bot, job) / "prompt.txt").read_text()
    assert "--- RULES-FOR-ALL-BOTS.md ---" not in prompt and "--- SYSTEM-PROMPT.md ---" in prompt
    assert "Rules:\n1." in prompt
    with pytest.raises(ValueError):
        RULES.set_override(store, "x" * (RULES.max_chars + 1))


def test_0119_override_carries_over_as_the_system_prompt(settings):
    store, scheduler, _, bot = _setup(settings)
    store.set_meta("baseline_instructions", "Charter saved in 0.11.9")  # what 0.11.9's PUT /v1/baseline stored
    assert SYSTEM.effective(store) == "Charter saved in 0.11.9"
    assert RULES.effective(store) == RULES.default_text()
    _, job = scheduler.turns.post_message(bot, "hello")
    ws = _prepare(store, scheduler, bot, job)
    assert "Charter saved in 0.11.9" in (ws / "SYSTEM-PROMPT.md").read_text()


def test_resume_nudge_per_layer_only_when_it_changed(settings):
    store, scheduler, _, bot = _setup(settings)
    _, job = scheduler.turns.post_message(bot, "hello")
    # a session that started before the layers existed is told once about each
    resume = (_prepare(store, scheduler, bot, job) / "prompt-resume.txt").read_text()
    assert SYSTEM.changed_notice() in resume and RULES.changed_notice() in resume
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    _, second = scheduler.turns.post_message(bot, "again")
    resume = (_prepare(store, scheduler, bot, second) / "prompt-resume.txt").read_text()
    assert SYSTEM.changed_notice() not in resume and RULES.changed_notice() not in resume
    RULES.set_override(store, "New rules\n")
    resume = (_prepare(store, scheduler, bot, second) / "prompt-resume.txt").read_text()
    assert resume.count(RULES.changed_notice()) == 1 and SYSTEM.changed_notice() not in resume
    assert "New rules" not in resume  # a one-line nudge, not the text


def test_failed_turn_keeps_the_nudge(settings):
    store, scheduler, adapter, bot = _setup(settings)
    adapter.result = "failed"
    scheduler.turns.post_message(bot, "hello")
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    for layer in baseline.LAYERS:
        assert not (scheduler.memory.bot_dir(bot.id) / layer.snapshot).exists()


@pytest.mark.parametrize(("path", "layer"), [("/v1/baseline", SYSTEM), ("/v1/system-prompt", SYSTEM),
                                             ("/v1/rules-for-all-bots", RULES)])
def test_layer_api_is_owner_only(settings, path, layer):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        store = app.state.store
        state = client.get(path, headers=OWNER).json()
        assert state["customized"] is False and state["text"] == state["default"] == layer.default_text()
        saved = client.put(path, headers=OWNER, json={"text": "Owner text"}).json()
        assert saved["customized"] is True and saved["text"] == "Owner text"
        assert layer.effective(store) == "Owner text"
        too_long = client.put(path, headers=OWNER, json={"text": "x" * (layer.max_chars + 1)})
        assert too_long.status_code == 422

        bot = store.create_bot("Ada", "", "codex", None)
        job = store.create("work", "codex", bot_id=bot.id, source="chat")
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        assert client.get(path, headers=agent).status_code == 401
        assert client.put(path, headers=agent, json={"text": "bot text"}).status_code == 401
        assert client.delete(path, headers=agent).status_code == 401
        assert client.put(path, json={"text": "anon"}).status_code == 401
        assert layer.effective(store) == "Owner text"

        reset = client.delete(path, headers=OWNER).json()
        assert reset["customized"] is False and reset["text"] == layer.default_text()


def test_settings_api_auto_continue(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        assert client.get("/v1/settings", headers=OWNER).json()["auto_continue"].items() >= {"enabled": True, "max": 10}.items()
        saved = client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"enabled": False, "max": 4}}).json()
        assert saved["auto_continue"].items() >= {"enabled": False, "max": 4}.items()
        assert client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"max": 0}}).status_code == 422
        assert client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"max": 999}}).status_code == 422
        partial = client.put("/v1/settings", headers=OWNER, json={"auto_continue": {"enabled": True}}).json()
        assert partial["auto_continue"].items() >= {"enabled": True, "max": 4}.items()
        assert client.put("/v1/settings", json={"auto_continue": {"enabled": False}}).status_code == 401
        assert client.get("/v1/settings").status_code == 401


def test_settings_page_markup():
    from importlib.resources import files
    html = files("botoffice").joinpath("static/bot-office.html").read_text()
    for needed in ('id="office-settings"', 'id="settings-dialog"', "Rules for all bots", 'id="rules-reset"',
                   'id="auto-continue"', 'id="auto-continue-max"', 'id="engines-list"', 'id="set-advanced"',
                   "<summary>Advanced</summary>", 'id="system-reset"', '"/v1/rules-for-all-bots"',
                   '"/v1/system-prompt"', '"/v1/settings"'):
        assert needed in html
    # the rules come before Advanced, and the system prompt sits inside Advanced
    advanced = html.index('id="set-advanced"')
    assert html.index('id="rules-text"') < advanced < html.index('id="system-text"')
    assert 'id="baseline-panel"' not in html
