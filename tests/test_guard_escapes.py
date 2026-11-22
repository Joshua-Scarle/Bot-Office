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
    "/usr/local/bin/claude --dangerously-skip-permissions",
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
SENDS = ["desk grok send --to Duet --kind question --text 'the approved update'", "git push origin feature"]


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
    ticket = rules.tickets.issue(str(a.id), "https://github.com/o/r", "fetch")
    assert ticket
    rules._approved[str(a.id)] = [(float("inf"), "gh pr merge 5", WORK)]
    rules._passes[str(a.id)] = [(float("inf"), [{"url": "https://github.com/o/r"}])]
    woken = fire_wake(store, bot, a.id, "merge it")
    # job B cannot redeem job A's ticket (and the failed try does not burn it) ...
    assert rules.tickets.redeem(ticket, str(woken.id), "https://github.com/o/r") is None
    assert rules.tickets.redeem(ticket, str(woken.id), None) is None
    # ... which job A still redeems, once
    redeemed = rules.tickets.redeem(ticket, str(a.id), "https://github.com/o/r")
    assert redeemed and redeemed.job_id == str(a.id)
    assert rules.tickets.redeem(ticket, str(a.id), "https://github.com/o/r") is None
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


# --- round 2: scripts, wrappers, long commands, a stripped environment ---

def hits_in(command, cwd):
    return presets_hit(parse_event({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                    "tool_input": {"command": command}, "cwd": str(cwd)}))


def write(path, text, mode=0o755):
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.mark.parametrize("run", ["bash {f}", "sh {f}", "zsh {f}", "dash {f}", "busybox sh {f}", "bash -x {f}",
                                 ". {f}", "source {f}", "{f}", "./{n}", "nohup {f} &", "env A=1 {f}",
                                 "sudo bash {f}", "setsid bash {f}"])
def test_a_script_file_that_launches_a_bypassed_agent_is_read(tmp_path, run):
    f = write(tmp_path / "a.sh", "#!/bin/sh\necho start\nclaude --dangerously-skip-permissions -p go\n")
    assert NESTED in hits_in(run.format(f=f, n=f.name), tmp_path)


def test_script_launch_variants_and_limits(tmp_path):
    py = write(tmp_path / "j.py", "import subprocess\nsubprocess.run(['codex', 'exec', '--yolo', 'go'])\n", 0o644)
    js = write(tmp_path / "j.js", "require('child_process').execSync('gemini --yolo -p go')\n", 0o644)
    assert NESTED in hits_in(f"python3 {py}", tmp_path)
    assert NESTED in hits_in("python3 j.py", tmp_path)
    assert NESTED in hits_in(f"node {js}", tmp_path)
    # a script that runs a script (b.sh -> c.sh) is followed, a chain of three is not, nor a loop
    write(tmp_path / "c.sh", "claude --dangerously-skip-permissions -p x\n")
    write(tmp_path / "b.sh", "bash c.sh\n")
    write(tmp_path / "a2.sh", "bash b.sh\n")
    assert NESTED in hits_in("bash b.sh", tmp_path) and NESTED not in hits_in("bash a2.sh", tmp_path)
    write(tmp_path / "loop.sh", "bash loop.sh\n")
    assert NESTED not in hits_in("bash loop.sh", tmp_path)
    # no bypass flag, a missing file, a binary, a file over 256 KiB
    write(tmp_path / "plain.sh", "claude -p hi\n")
    assert NESTED not in hits_in("bash plain.sh", tmp_path) and NESTED not in hits_in("bash nope.sh", tmp_path)
    (tmp_path / "bin.sh").write_bytes(b"\0\1claude --dangerously-skip-permissions\n")
    assert NESTED not in hits_in("bash bin.sh", tmp_path)
    write(tmp_path / "big.sh", "claude --dangerously-skip-permissions\n" + "# pad\n" * 50000)
    assert NESTED not in hits_in("bash big.sh", tmp_path)


def test_a_script_written_and_run_in_one_command(tmp_path):
    assert NESTED in hits_in("cat > /tmp/x.sh <<'EOF'\nclaude --dangerously-skip-permissions -p go\nEOF\nbash /tmp/x.sh", tmp_path)
    assert NESTED in hits_in("echo 'codex --yolo exec go' > r.sh; sh r.sh", tmp_path)


@pytest.mark.parametrize("command", [
    "busybox sh -c 'claude --dangerously-skip-permissions -p x'", "busybox ash -c 'codex --yolo exec x'",
    "command claude --dangerously-skip-permissions -p x", "exec codex --yolo exec x",
    "doas claude --permission-mode bypassPermissions -p x", "sudo -u bot claude --dangerously-skip-permissions",
    "stdbuf -oL codex --yolo exec x", "chrt -f 10 claude --dangerously-skip-permissions -p x",
    "ionice -c3 claude --dangerously-skip-permissions -p x", "taskset 0x3 claude --dangerously-skip-permissions -p x",
    "taskset -c 0,1 codex --yolo exec x", "script -qc 'claude --dangerously-skip-permissions -p x' /dev/null",
    "script -c 'codex --yolo exec x' out", "flock /tmp/l claude --dangerously-skip-permissions -p x",
    "flock -n /tmp/l -c 'codex --yolo exec x'", "watch -n 5 claude --dangerously-skip-permissions -p x",
    "watch 'codex --yolo exec x'", "parallel claude --dangerously-skip-permissions -p ::: a b",
    "parallel 'codex --yolo exec {}' ::: a b",
    # an unrecognised wrapper: the name and the flag later in its argv
    "mywrap --opt claude --dangerously-skip-permissions -p x", "FOO=1 runner -- codex exec --yolo x",
    "launcher 'claude --dangerously-skip-permissions -p x'",
    # an alias or a function whose body launches it
    "alias c='claude --dangerously-skip-permissions'; c -p x", 'alias c="codex --yolo"',
    "run() { claude --dangerously-skip-permissions -p \"$1\"; }; run x",
    "function go { codex --yolo exec x; }",
])
def test_more_wrappers_aliases_and_functions(tmp_path, command):
    assert NESTED in hits_in(command, tmp_path)


@pytest.mark.parametrize("command", ["time grep claude --dangerously-skip-permissions f", "nice echo claude --dangerously-skip-permissions", "watch -n 5 ls claude",
                                     "script -c 'ls' out", "flock /tmp/l git status",
                                     "alias ll='ls -la'", "run() { echo claude --dangerously-skip-permissions; }",
                                     "git commit -m 'claude --dangerously-skip-permissions'", "apt-get install -y aider-chat"])
def test_more_forms_that_are_not_launches(tmp_path, command):
    assert NESTED not in hits_in(command, tmp_path)


@pytest.mark.parametrize("command", [
    "claude --dangerously-skip-permissions -p x", "codex exec --yolo go",
    "bash -c 'codex --yolo exec x'", "printf x | claude --permission-mode bypassPermissions -p",
])
def test_long_commands_fail_closed(tmp_path, command):
    padded = command + "\n# " + "x" * 40000
    assert len(padded) > nested_agents.LONG_COMMAND and NESTED in hits_in(padded, tmp_path)
    assert NESTED in hits_in("# " + "x" * 40000 + "\n" + command, tmp_path)
    store, bot, rules = make(tmp_path)
    rules.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern=NESTED, scope="attended")
    decision = rules.decide(running(store, bot), bash(padded))
    # not run. Over APPROVAL_LIMIT a card cannot show the whole command, so it is refused outright
    assert decision.decision == "deny" and decision.preset == NESTED


def test_long_commands_without_both_parts_are_untouched(tmp_path):
    assert NESTED not in hits_in("echo hi\n# " + "x" * 40000, tmp_path)
    assert NESTED not in hits_in("claude -p hi\n# " + "x" * 40000, tmp_path)
    assert NESTED not in hits_in("grep -n --yolo file\n# " + "x" * 40000, tmp_path)


@pytest.mark.parametrize("command", [
    "env -i claude -p x", "env -i PATH=/usr/bin codex exec x", "env --ignore-environment claude -p x",
    "env - claude -p x", "env -u PATH claude -p x", "env -u BOT_OFFICE_GATE_FILE codex exec x",
    "env -u GIT_CONFIG_COUNT claude -p x", "env --unset=PATH claude -p x", "env BOT_OFFICE_JOB_ID= claude -p x",
    "env GIT_CONFIG_COUNT=0 claude -p x", "exec -c claude -p x", "env -i bash -c 'claude -p x'",
    "python3 -c 'import os; os.environ.clear(); os.execvp(\"claude\", [\"claude\", \"-p\", \"x\"])'",
    "python3 -c 'import subprocess; subprocess.run([\"codex\", \"exec\", \"x\"], env={})'",
    "node -e 'require(\"child_process\").spawn(\"claude\", [\"-p\"], {env: {}})'",
    "perl -e '%ENV = (); exec \"codex\", \"exec\", \"x\"'",
])
def test_an_agent_launched_without_the_guard_environment_is_a_nested_agent_hit(tmp_path, command):
    assert NESTED in hits_in(command, tmp_path)


@pytest.mark.parametrize("command", [
    "env FOO=1 claude -p x", "env -u HOME claude -p x", "PATH=$PATH:/opt/bin claude -p x", "env -i ls",
    "env -i printenv", "python3 -c 'import os; os.environ.clear(); print(1)'",
    "python3 -c 'import subprocess; subprocess.run([\"ls\"], env={})'",
])
def test_plain_agents_and_other_programs_under_a_cleared_environment(tmp_path, command):
    assert NESTED not in hits_in(command, tmp_path)


def test_interpreter_text_that_only_mentions_a_launch_is_a_known_false_positive(tmp_path):
    """Fail safe: a string literal that reads as a bypass launch raises a card even if it is only printed."""
    assert NESTED in hits_in("python3 -c 'print(\"claude --dangerously-skip-permissions\")'", tmp_path)


@pytest.mark.parametrize("command", ["a()" * 9000, "f(){ " * 5000, "alias " * 5000, "echo '" * 5000 + ">",
                                     "env " * 8000 + "claude", "x " * 15000 + "claude", "bash " * 6000,
                                     "function " * 3000, "a." * 15000])
def test_nested_detection_runs_in_bounded_time(command):
    import time
    started = time.perf_counter()
    hits(command)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("command", [
    "/usr/bin/claude --dangerously-skip-permissions -p x", "codex -anever exec x", "codex -a never exec x",
    "agent -f -p x", "agent --force -p x", "cursor-agent -y", "gemini --yolo -p x", "codex -sdanger-full-access exec",
    "codex -c 'approval_policy=\"never\"' exec", "claude --permission-mode=bypassPermissions -p x",
    "claude --permission-mode bypassPermissions", "aider --yes-always", "x=1 ./claude --dangerously-skip-permissions",
    "foo --opt '/opt/bin/codex' --yolo", "claude -- --dangerously-skip-permissions",
])
def test_long_commands_use_the_same_flags_as_the_parser(tmp_path, command):
    for padded in (command + "\n# " + "x" * 40000, "# " + "x" * 40000 + "\n" + command):
        assert len(padded) > nested_agents.LONG_COMMAND and NESTED in hits_in(padded, tmp_path)


def test_long_command_flag_list_is_the_parsers():
    # one list: every flag the normal parser accepts is also found in a long command
    for family, flag in (("claude", ["--dangerously-skip-permissions"]), ("codex", ["-a", "never"]),
                         ("cursor", ["-f"]), ("gemini", ["-y"]), ("aider", ["--yes"])):
        assert nested_agents.bypass_flag(family, flag, strict=False)


def test_cd_is_followed_to_find_a_script(tmp_path):
    sub = tmp_path / "work"
    sub.mkdir()
    write(sub / "a.sh", "claude --dangerously-skip-permissions -p x\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert NESTED in hits_in(f"cd {sub} && bash a.sh", other)
    assert NESTED in hits_in("cd work; bash a.sh", tmp_path)
    assert NESTED in hits_in(f"pushd {sub} >/dev/null; ./a.sh", other)
    assert NESTED in hits_in(f"cd {sub} && cd .. && cd work && sh a.sh", other)
    # a cd that cannot be resolved: nothing read from disk, no false hit
    assert NESTED not in hits_in("cd $UNKNOWN && bash a.sh", other)
    assert NESTED not in hits_in(f"cd {other} && bash a.sh", tmp_path)


@pytest.mark.parametrize("command", [
    "printf '%s\\n' 'claude --dangerously-skip-permissions' > a.sh; bash a.sh",
    "printf '%s\\n' 'codex --yolo exec x' | tee a.sh; sh ./a.sh",
    "cd $UNKNOWN && echo x > a.sh && bash a.sh # claude --dangerously-skip-permissions",
    "python3 -c \"open('a.sh','w').write('claude --dangerously-skip-permissions')\"; bash a.sh",
    "cp tmpl.sh a.sh; sed -i 's/$/ /' a.sh; bash a.sh  # codex -a never",
    "cat > a.sh <<EOF\nclaude --dangerously-skip-permissions\nEOF\nchmod +x a.sh; ./a.sh",
])
def test_write_and_run_with_agent_and_flag_text_is_a_hit(tmp_path, command):
    assert NESTED in hits_in(command, tmp_path)


@pytest.mark.parametrize("command", [
    "printf '%s\\n' 'claude --dangerously-skip-permissions' > notes.txt",
    "echo hi > a.sh; bash a.sh", "bash a.sh # claude --dangerously-skip-permissions",
    "printf 'claude -p hi' > a.sh; bash a.sh",
])
def test_write_and_run_needs_all_three_parts(tmp_path, command):
    assert NESTED not in hits_in(command, tmp_path)
