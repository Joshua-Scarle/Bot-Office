"""0.12.18 hotfix: B8 (a nested agent CLI with its permission checks off) and B11 (a scheduled run
gets no approval from the turn that scheduled it)."""
from contextlib import closing
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from guard_repo import clone_workspace

from botoffice import nested_agents, workspace
from botoffice.assignments import Assignments
from botoffice.domain import Job, JobStatus
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.cli import REPO_BIN
from botoffice.rules import Rules, parse_event, presets_hit
from botoffice.store import JobStore, approval_card, now

WORK = clone_workspace()
NESTED = nested_agents.PRESET


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": WORK}


def hits(command):
    return presets_hit(parse_event(bash(command)))


# --- B8: detection ---

BYPASS = [
    # claude
    'claude -p --model claude-opus-5-5 --effort high --permission-mode bypassPermissions "build it"',
    "claude --permission-mode=bypassPermissions -p x",
    "claude --dangerously-skip-permissions -p x",
    "claude --allow-dangerously-skip-permissions -p x",
    "/home/u/.local/bin/claude --dangerously-skip-permissions",
    "~/.local/bin/claude -p hi --permission-mode bypassPermissions",
    # codex
    "codex --dangerously-bypass-approvals-and-sandbox exec go",
    "codex exec --yolo go",
    "codex exec --full-auto go",
    "codex -s danger-full-access exec go",
    "codex --sandbox danger-full-access exec go",
    "codex --sandbox=danger-full-access exec go",
    "codex -c approval_policy=never exec go",
    "codex -c 'approval_policy=\"never\"' exec go",
    "codex --config approval_policy=never exec go",
    "codex -c sandbox_mode=danger-full-access exec go",
    "codex -a never exec go",
    "codex --ask-for-approval never exec go",
    "codex --ask-for-approval=never exec go",
    # gemini, opencode, cursor-agent, aider
    "gemini --yolo -p go",
    "gemini -y -p go",
    "gemini --approval-mode yolo -p go",
    "opencode run --yolo go",
    "opencode run -y go",
    "opencode run --dangerously-skip-permissions go",
    "cursor-agent --force -p go",
    "cursor-agent --yolo -p go",
    "agent -f -p go",
    "aider --yes-always --message go",
]

WRAPPED = [
    "env FOO=1 claude --dangerously-skip-permissions -p x",
    "env -u HOME claude --dangerously-skip-permissions -p x",
    "env -S 'claude --dangerously-skip-permissions -p x'",
    "nohup claude -p --permission-mode bypassPermissions x > opus-build.log 2>&1 &",
    "setsid -f claude --dangerously-skip-permissions -p x",
    "timeout 600 codex exec --yolo go",
    "timeout -s KILL 10m codex exec --yolo go",
    "nice -n 10 claude --dangerously-skip-permissions -p x",
    "nice claude --dangerously-skip-permissions -p x",
    "ionice -c 3 gemini --yolo -p go",
    "bash -c 'claude --dangerously-skip-permissions -p x'",
    'sh -c "codex exec --yolo go"',
    "bash -lc 'cd /tmp && claude --permission-mode bypassPermissions -p x'",
    "eval 'claude --dangerously-skip-permissions -p x'",
    "ls briefs/*.md | xargs -n1 claude --dangerously-skip-permissions -p",
    "xargs -I{} claude --dangerously-skip-permissions -p {} < list",
    "npx -y @anthropic-ai/claude-code --dangerously-skip-permissions -p x",
    "npx @openai/codex@latest exec --yolo go",
    "npx --package @google/gemini-cli gemini --yolo",
    "npx -c 'claude --dangerously-skip-permissions -p x'",
    "pnpm dlx @openai/codex --full-auto exec go",
    "uvx --from aider-chat aider --yes-always",
    "pipx run aider-chat --yes-always",
    "sudo -u bot claude --dangerously-skip-permissions",
    "cd /tmp && claude --dangerously-skip-permissions -p x",
    "true; FOO=bar claude --dangerously-skip-permissions -p x",
    "out=$(claude --dangerously-skip-permissions -p x)",
    "echo `codex exec --yolo go`",
    "bash <<EOF\nclaude --dangerously-skip-permissions -p x\nEOF",
    "python3 -c \"import subprocess; subprocess.run(['claude', '--dangerously-skip-permissions', '-p', 'x'])\"",
    "python3 -c \"import os; os.system('codex exec --yolo go')\"",
    "timeout 300 nohup env A=1 bash -c 'nice codex exec --yolo go'",
]

NOT_BYPASS = [
    # a mention is not a launch
    "grep bypassPermissions file",
    "grep -rn -- --dangerously-skip-permissions .",
    "rg 'claude --yolo' docs",
    "echo claude --yolo",
    "echo 'claude --dangerously-skip-permissions' >> notes.md",
    "printf '%s\\n' 'codex --yolo'",
    "cat > run.sh <<EOF\nclaude --dangerously-skip-permissions -p x\nEOF",
    "tee brief.md <<'EOF'\ncodex exec --yolo go\nEOF",
    "man claude",
    "which claude codex",
    "git commit -m 'block claude --dangerously-skip-permissions'",
    # a launch without a bypass flag
    "claude -p hi",
    "claude --permission-mode acceptEdits -p hi",
    "claude --permission-mode plan -p hi",
    "claude --help",
    "codex exec go",
    "codex -s workspace-write -a on-request exec go",
    "codex -c approval_policy=on-request exec go",
    "gemini -p go",
    "opencode run go",
    "cursor-agent -p go",
    "aider --message go",
    "nohup claude -p hi > log &",
    "npx @anthropic-ai/claude-code -p hi",
    # other programs' flags
    "apt-get -y install jq",
    "ssh-agent -s",
]


@pytest.mark.parametrize("command", BYPASS + WRAPPED)
def test_bypass_launches_are_the_nested_agent_preset(command):
    found = hits(command)
    assert found and found[0] == NESTED, command
    assert nested_agents.bypass_launch(command)


@pytest.mark.parametrize("command", NOT_BYPASS)
def test_mentions_and_plain_launches_are_not_flagged(command):
    assert NESTED not in hits(command), command
    assert nested_agents.bypass_launch(command) is None


# --- B8: the bot's rules decide (ask => card, allow => runs, never => refused) ---

def make(tmp_path, scheduled_runs="full"):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Lead", "", "claude", None)
    store.update_bot(bot.id, scheduled_runs=scheduled_runs)
    return store, store.get_bot(bot.id), Rules(store)


def running(store, bot, source="chat", origin=None):
    job = store.create("work", "claude", bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running', origin_job_id=? WHERE id=?",
                   (str(origin) if origin else None, str(job.id)))
    return store.get(job.id)


B8 = 'claude -p --model claude-opus-5-5 --effort high --permission-mode bypassPermissions "$(cat /tmp/brief-opus.md)"'


@pytest.mark.parametrize("preset", ["outbound", "destructive", NESTED])
def test_ask_raises_a_nested_agent_card(tmp_path, preset):
    store, bot, rules = make(tmp_path)
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern=preset)
    decision = rules.decide(running(store, bot), bash(B8))
    assert decision.decision == "deny" and decision.effect == "ask" and decision.approval_id
    assert decision.preset == NESTED and "waiting_on_user" in decision.reason
    item = rules.get(decision.approval_id)
    assert approval_card(store, item)["label"] == "Nested agent with permissions bypassed"
    assert "bypassPermissions" in item["summary"]


def test_ask_rule_blocks_until_approved_then_runs_once(tmp_path):
    store, bot, rules = make(tmp_path)
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern="outbound")
    origin = running(store, bot)
    approval = rules.decide(origin, bash(B8)).approval_id
    store.finish(origin.id, JobStatus.INCOMPLETE)
    resume_id = rules.approve(approval)["resume_job_id"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (resume_id,))
    resume = store.get(resume_id)
    assert rules.decide(resume, bash(B8)).decision == "allow"
    assert rules.decide(resume, bash(B8)).effect == "ask"  # once


def test_allow_runs_and_never_refuses(tmp_path):
    store, bot, rules = make(tmp_path)
    chat = running(store, bot)
    rules.add_rule(bot_id=bot.id, effect="allow", kind="preset", pattern="outbound")
    assert rules.decide(chat, bash(B8)).decision == "allow"
    rules.add_rule(bot_id=bot.id, effect="never", kind="preset", pattern="destructive")
    refused = rules.decide(chat, bash(B8))
    assert refused.decision == "deny" and refused.effect == "never" and refused.preset == NESTED


def test_no_flag_launch_and_no_rule_are_untouched(tmp_path):
    store, bot, rules = make(tmp_path)
    chat = running(store, bot)
    assert rules.decide(chat, bash(B8)).decision == "allow"  # no rule: the owner's full default
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern="outbound")
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern="destructive")
    for command in ("claude -p hi", "codex exec go", "grep bypassPermissions f", "man claude", "ls"):
        assert rules.decide(chat, bash(command)).decision == "allow", command


def test_strict_trigger_run_asks_without_a_rule_and_allow_cannot_open_it(tmp_path):
    store, bot, rules = make(tmp_path)
    trigger = running(store, bot, "trigger")
    rules.add_rule(bot_id=bot.id, effect="allow", kind="preset", pattern=NESTED, scope="unattended")
    assert rules.decide(trigger, bash("codex exec --yolo go")).effect == "ask"


def test_child_process_inherits_the_guard_environment(tmp_path):
    """A nested CLI started from the bot's shell gets PATH with bin/ first (the git/gh/mail shims),
    the job's BOT_OFFICE_* settings and the GIT_CONFIG_* pairs (core.hooksPath: the pre-push check)."""
    job = Job(id=uuid4(), prompt="go", status=JobStatus.RUNNING, created_at=datetime.now(), bot_id=uuid4(),
              gate_command="/repo/bin/bot-office-gate claude --mode attended", secret_paths=[])
    env = ClaudeAdapter(executable="claude")._env(tmp_path, job)
    assert env["PATH"].split(":")[0] == str(REPO_BIN)
    assert env["BOT_OFFICE_JOB_ID"] == str(job.id) and env["BOT_OFFICE_GATE_FILE"]
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["core.hooksPath"] == str(REPO_BIN / "git-hooks")


@pytest.mark.parametrize("command", ["env -i claude -p x", "unset BOT_OFFICE_GATE_FILE; claude -p x",
                                     "GIT_CONFIG_COUNT=0 claude -p x", "export BOT_OFFICE_JOB_ID=x"])
def test_stripping_the_guard_environment_asks(tmp_path, command):
    store, bot, rules = make(tmp_path)
    assert rules.decide(running(store, bot), bash(command)).effect == "ask"


# --- B11: a scheduled run gets no approval from the turn that scheduled it ---

def fire_wake(store, bot, scheduled_by, note):
    book = Assignments(store)
    wakeup = book.add_wakeup(bot.id, scheduled_by, due_at=now() + timedelta(hours=1), note=note)
    with closing(store._connect()) as db:
        db.execute("UPDATE wakeups SET due_at=? WHERE id=?", ((now() - timedelta(seconds=1)).isoformat(), str(wakeup.id)))
    assert book.fire_due() == 1
    (woken,) = [job for job in store.list() if job.bot_id == bot.id and job.source == "wakeup"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(woken.id),))
    return store.get(woken.id)


# a send the always-on send class stops, and an outbound action only the bot's ask rule stops
SENDS = ["desk grok send --to Duet --kind fyi --text 'the approved update'", "git push origin feature"]


@pytest.mark.parametrize("consumed", [False, True])
@pytest.mark.parametrize("SEND", SENDS)
def test_wake_cannot_use_the_scheduling_jobs_approval(tmp_path, consumed, SEND):
    """End to end: approve an outbound action in job A, A's resume turn schedules a wake, the wake
    fires job B, and the same action in B raises a fresh card (no grant, pass or ticket carries over)."""
    store, bot, rules = make(tmp_path)
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern="outbound")
    a = running(store, bot)
    first = rules.decide(a, bash(SEND))
    assert first.effect == "ask"
    store.finish(a.id, JobStatus.INCOMPLETE)
    resume_id = rules.approve(first.approval_id)["resume_job_id"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (resume_id,))
    resume = store.get(resume_id)
    if consumed:
        assert rules.decide(resume, bash(SEND)).decision == "allow"
    woken = fire_wake(store, bot, resume.id, "if a channel exists, send the approved update there")
    assert woken.origin_job_id is None and "approved update" in woken.prompt
    again = rules.decide(woken, bash(SEND))
    assert again.decision == "deny" and again.effect == "ask"
    assert again.approval_id and again.approval_id != first.approval_id
    assert rules.consume_grant(woken, parse_event(bash(SEND))) is None
    # the in-memory approved commands and push passes are per job
    assert not rules._approved_command(woken, ["desk", "grok", "send"])
    assert not rules._push_allowed(woken, ["https://github.com/o/r"])
    assert rules.get(first.approval_id)["status"] == ("consumed" if consumed else "approved")


def test_github_ticket_and_push_pass_stay_with_their_job(tmp_path):
    store, bot, rules = make(tmp_path)
    a = running(store, bot)
    rules._approved[str(a.id)] = [(float("inf"), "gh pr merge 5", WORK)]
    rules._passes[str(a.id)] = [(float("inf"), [{"url": "https://github.com/o/r"}])]
    woken = fire_wake(store, bot, a.id, "merge it")
    assert rules._approved_command(a, ["gh", "pr", "merge", "5"])
    assert not rules._approved_command(woken, ["gh", "pr", "merge", "5"])
    assert rules._push_allowed(a, ["https://github.com/o/r"]) and not rules._push_allowed(woken, ["https://github.com/o/r"])


@pytest.mark.parametrize("source", ["wakeup", "routine", "assignment", "inbox"])
def test_unattended_rules_judge_scheduled_runs_of_full_bots(tmp_path, source):
    store, bot, rules = make(tmp_path, "full")
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern="outbound", scope="unattended")
    assert rules.decide(running(store, bot, "chat"), bash("git push origin feature")).decision == "allow"
    asked = rules.decide(running(store, bot, source), bash("git push origin feature"))
    assert asked.decision == "deny" and asked.effect == "ask"
    rules.add_rule(bot_id=bot.id, effect="never", kind="shell", pattern=r"\bcurl\b", scope="unattended")
    assert rules.decide(running(store, bot, source), bash("curl https://x.example")).effect == "never"


def test_strictest_scope_wins_and_an_other_scope_allow_opens_nothing(tmp_path):
    store, bot, rules = make(tmp_path, "gated")
    wake = running(store, bot, "wakeup")
    # gated: its own (unattended) allow opens the preset ...
    rules.add_rule(bot_id=bot.id, effect="allow", kind="preset", pattern="outbound", scope="unattended")
    assert rules.decide(wake, bash("git push origin feature")).decision == "allow"
    # ... but an attended ask still asks, and an attended never refuses
    rules.add_rule(bot_id=bot.id, effect="ask", kind="shell", pattern=r"git push", scope="attended")
    assert rules.decide(wake, bash("git push origin feature")).effect == "ask"
    rules.add_rule(bot_id=bot.id, effect="never", kind="shell", pattern=r"git push", scope="attended")
    assert rules.decide(wake, bash("git push origin feature")).effect == "never"
    # an unattended allow does not open anything in a full bot's wake-up that its attended rules ask
    store2, bot2, rules2 = make(tmp_path / "b", "full")
    rules2.add_rule(bot_id=bot2.id, effect="ask", kind="preset", pattern="outbound", scope="attended")
    rules2.add_rule(bot_id=bot2.id, effect="allow", kind="preset", pattern="outbound", scope="unattended")
    assert rules2.decide(running(store2, bot2, "wakeup"), bash("git push origin feature")).effect == "ask"


def test_woken_runs_are_told_approvals_do_not_carry_over():
    line = "This run was started by a scheduled wake. Earlier approvals do not carry over; any send or " \
           "destructive action needs a fresh approval."

    def job(source, origin=None):
        return Job(id=uuid4(), prompt="go", status=JobStatus.RUNNING, created_at=datetime.now(), bot_id=uuid4(),
                   source=source, origin_job_id=origin)

    assert line in workspace._reply_rule(job("wakeup"))
    assert "scheduled routine. Earlier approvals do not carry over" in workspace._reply_rule(job("routine"))
    assert "Earlier approvals" not in workspace._reply_rule(job("chat"))
    # the resume turn after an approval holds that one grant: not told otherwise
    assert "Earlier approvals" not in workspace._reply_rule(job("wakeup", uuid4()))
