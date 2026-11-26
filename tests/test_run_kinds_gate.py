"""A16: the approval card is raised in every run kind, for the git guard and for outbound sends,
and the action does not run before the owner approves."""
import asyncio
import json
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("github_private")  # named GitHub merges reach their card

from botoffice import slash as slash_mod
from botoffice.domain import JobStatus
from botoffice.providers.base import RunResult
from botoffice.providers.cli import CliAdapter
from botoffice.rules import Rules
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

GATE = Path(__file__).resolve().parents[1] / "bin" / "bot-office-gate"
WORK = "/srv/bots/ada/work"

# chat, routine, wake-up / auto-continue, assignment, slash, plus the other sources that start a turn
KINDS = ["chat", "routine", "wakeup", "assignment", "slash", "inbox", "trigger"]
GIT_GUARD = ("gh pr merge 3 -R o/r", "git push origin main")
OUTBOUND = (("Bash", {"command": "sendmail -t < outbox/note.eml"}),
            ("mcp__gmail__send_message", {"to": "a@example.com", "body": "hi"}),
            ("Bash", {"command": "mail -s hi someone@example.com < outbox/note.txt"}))


class Recording(CliAdapter):
    name = "codex"

    def __init__(self):
        super().__init__(executable="/bin/true")
        self.jobs = []

    async def run(self, job, workspace, log_path, timeout):
        self.jobs.append(job)
        return RunResult("succeeded", exit_code=0)


def event(tool, data):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": data, "cwd": WORK}


def make_job(store, bot, kind):
    source = "chat" if kind == "slash" else kind
    job = store.create("work", "codex", bot_id=bot.id, source=source)
    if kind == "slash":
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET slash=? WHERE id=?",
                       (slash_mod.encode_slash("review", "src", "command"), str(job.id)))
    return store.get(job.id)


@pytest.fixture
def world(settings):
    store = JobStore(settings.data_dir / "db.sqlite3")
    adapter = Recording()
    scheduler = Scheduler(store, settings, {"codex": adapter})
    settings.ensure_dirs()
    bot = store.create_bot("Ada", "", "codex", None)
    return store, scheduler, adapter, bot


def pending(store):
    with closing(store._connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM approvals WHERE status='pending'")]


@pytest.mark.parametrize("kind", KINDS)
def test_every_run_kind_is_wired_to_the_gate(world, kind):
    """The run reaches the CLI with the gate hook command and the mode file, whatever started it."""
    store, scheduler, adapter, bot = world
    made = make_job(store, bot, kind)
    claimed = store.claim(["desktop-1"], {"codex"})
    if kind == "trigger":  # a trigger is held until the host proves the gate: it never runs ungated
        asyncio.run(scheduler._execute(0, claimed))
        assert store.get(made.id).failure_kind == "gate" and adapter.jobs == []
        return
    asyncio.run(scheduler._execute(0, claimed))
    assert store.get(made.id).status == JobStatus.SUCCEEDED
    ran = adapter.jobs[-1]
    assert "bot-office-gate" in ran.gate_command and ran.agent_token
    mode = json.loads((scheduler.settings.data_dir / "workspaces" / str(made.id) / ".bot-office-gate.json").read_text())
    assert mode["job_id"] == str(made.id)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("command", GIT_GUARD)
def test_git_guard_card_in_every_run_kind_and_nothing_runs_before_approval(world, kind, command):
    store, _scheduler, _adapter, bot = world
    rules = Rules(store)
    job = make_job(store, bot, kind)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    job = store.get(job.id)
    first = rules.decide(job, event("Bash", {"command": command}))
    assert first.decision == "deny" and first.effect == "ask" and first.approval_id
    assert "waiting_on_user" in first.reason
    cards = [item for item in pending(store) if item["id"] == first.approval_id]
    assert cards and cards[0]["job_id"] == str(job.id)
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE approval_id=?", (first.approval_id,)).fetchone()[0] == 1
    # trying again before an answer is still refused (same card), never allowed
    again = rules.decide(job, event("Bash", {"command": command}))
    assert again.decision == "deny" and again.approval_id == first.approval_id


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("tool,data", OUTBOUND)
def test_outbound_card_in_every_run_kind_and_nothing_runs_before_approval(world, kind, tool, data):
    store, _scheduler, _adapter, bot = world
    rules = Rules(store)
    job = make_job(store, bot, kind)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    job = store.get(job.id)
    first = rules.decide(job, event(tool, data))
    assert first.decision == "deny" and first.effect == "ask" and first.approval_id, (kind, tool)
    assert [item for item in pending(store) if item["id"] == first.approval_id]
    assert rules.decide(job, event(tool, data)).decision == "deny"


@pytest.mark.parametrize("kind", ["chat", "routine", "wakeup", "assignment", "slash"])
def test_allow_rules_and_switched_off_classes_do_not_open_either_in_any_kind(world, kind):
    store, _scheduler, _adapter, bot = world
    rules = Rules(store)
    rules.add_rule(bot_id=bot.id, effect="allow", kind="shell", pattern=r"git|sendmail|gh")
    rules.add_rule(bot_id=bot.id, effect="allow", kind="preset", pattern="outbound")
    job = make_job(store, bot, kind)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    job = store.get(job.id)
    assert rules.decide(job, event("Bash", {"command": "git push origin main"})).decision == "deny"


def test_a_job_with_no_bot_still_runs_the_hook_and_its_fallback_refuses_guarded_actions(world, settings):
    """POST /v1/jobs without a bot has no token and no chat for a card: the hook is still wired and,
    with nobody to ask, refuses what the git guard guards."""
    import subprocess
    import sys
    store, scheduler, adapter, _bot = world
    made = store.create("check", "codex", source="api")
    asyncio.run(scheduler._execute(0, store.claim(["desktop-1"], {"codex"})))
    assert store.get(made.id).status == JobStatus.SUCCEEDED
    ran = adapter.jobs[-1]
    assert "bot-office-gate" in ran.gate_command and "--mode local" in ran.gate_command
    gate_file = settings.data_dir / "workspaces" / str(made.id) / ".bot-office-gate.json"
    assert json.loads(gate_file.read_text())["mode"] == "local"

    def gate(command):
        return subprocess.run([sys.executable, "-I", str(GATE), "codex", "--mode", "local"],
                              input=json.dumps(event("Bash", {"command": command})).encode(),
                              env={"PATH": "/usr/bin:/bin", "BOT_OFFICE_GATE_FILE": str(gate_file)},
                              capture_output=True, timeout=60, check=False)
    assert gate("git push origin main").returncode == 2
    assert gate("gh pr merge 3").returncode == 2
    assert gate("git status").returncode == 0
