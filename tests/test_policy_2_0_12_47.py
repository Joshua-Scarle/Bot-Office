"""0.12.47 policy-2: a refused or waiting step never ends the work; approved cards re-run the step in a
resume turn; denies wake the bot to carry on; own-desktop input without a card; capped desktop sessions."""
from contextlib import closing

import pytest
from teamkit import MASTER, Desk, run_job
from test_desk_own_desktop import fake_manager
from test_policy_0_12_45 import judge, office, visibility  # noqa: F401  (fixtures)

from botoffice import bot_desktops as bd
from botoffice import desk, policy, remote_access, workspace
from botoffice.baseline import _read_default
from botoffice.domain import JobStatus
from botoffice.rules import Rules, ask_reason, deny_prompt, resume_prompt
from botoffice.store import JobStore

WORK = "/tmp"


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": WORK}


def setup(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    return store, store.get_bot(bot.id), Rules(store)


def running(store, bot, source="chat"):
    job = store.create("work", "claude", bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store.get(job.id)


def start(store, job_id):
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job_id),))
    return store.get(job_id)


def audit(store):
    with closing(store._connect()) as db:
        return [dict(r) for r in db.execute("SELECT * FROM policy_audit ORDER BY id").fetchall()]


ASKS = "curl -sS -m 3 -X POST http://192.0.2.1:9/e2e"   # http-write: a card under the policy


# --- item 1: the texts every bot gets ---

def test_refusal_says_keep_going_and_it_reruns_once_approved():
    text = ask_reason(7, "Bash: x")
    assert "runs this exact step for you once it is approved" in text
    assert "Keep working" in text and "Never repackage the refused action" in text
    assert "which command waits, why it needs the owner's approval, and that it runs once approved" in text
    assert text.endswith("BOT-OFFICE-STATUS: waiting_on_user — approval #7")
    assert "end the turn with" not in text.split("Only when")[0]   # never "stop now"


def test_default_prompts_carry_the_rule():
    rules = workspace.RULES
    assert "A refused or waiting step never ends your work" in rules and "Never say you can't" in rules
    assert "do not try it another way" not in rules
    system = _read_default("system.md")
    assert "never ends your work" in system and "don't find another way to do it" not in system
    assert "reruns it once approved" in _read_default("rules-for-all-bots.md")


def test_resume_and_deny_prompts():
    resume = resume_prompt(3, "Bash: make x")
    assert "check it is still needed" in resume and "run exactly that command now, once" in resume
    assert "carry the task forward" in resume
    denied = deny_prompt(3, "Bash: make x", "not today")
    assert "The owner's note: not today" in denied and "Keep working" in denied
    assert "never say you can't while an allowed way remains" in denied


# --- item 1(a): approved after the turn ended wakes the bot, the step runs once ---

def test_card_approved_after_the_turn_ended_resumes_and_runs_once(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    decision = rules.decide(origin, bash(ASKS))
    assert (decision.decision, decision.effect) == ("deny", "ask") and "Keep working" in decision.reason
    store.finish(origin.id, JobStatus.INCOMPLETE, outcome=f"waiting_on_user: approval #{decision.approval_id}")
    item = rules.approve(decision.approval_id)
    resume = store.get(item["resume_job_id"])
    assert resume.status == JobStatus.QUEUED and resume.origin_job_id == origin.id
    assert resume.prompt.startswith(f"Approval #{decision.approval_id} granted by the owner")
    resume = start(store, resume.id)
    first = rules.decide(resume, bash(ASKS))
    assert first.decision == "allow" and first.approval_id == decision.approval_id
    again = rules.decide(resume, bash(ASKS))
    assert again.decision == "deny"           # once: a second run asks again
    tools = [row["tool"] for row in audit(store)]
    assert "approval approve" in tools


def test_approved_while_still_working_is_not_carded_twice(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    rules.approve(approval)                     # the owner answers while the bot keeps working
    retry = rules.decide(origin, bash(ASKS))
    assert retry.decision == "deny" and retry.approval_id == approval and "already granted" in retry.reason
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1


def test_resume_turn_that_skips_the_step_is_reported(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    store.finish(origin.id, JobStatus.INCOMPLETE, outcome="waiting_on_user: x")
    resume = start(store, rules.approve(approval)["resume_job_id"])
    store.finish(resume.id, JobStatus.SUCCEEDED, outcome="done")
    assert rules.resume_ended(store.get(resume.id)) == [approval]
    texts = [m.text for m in store.list_messages(bot.id)]
    assert any(t.startswith(f"Approval #{approval}: the resume turn ended without running") for t in texts)
    rules.expire()
    assert rules.get(approval)["status"] == "expired"
    assert any(row["tool"] == "approval unused" for row in audit(store))


# --- deny: recorded, the bot carries on without the action ---

def test_deny_wakes_the_bot_and_the_same_action_is_not_carded_again(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    store.finish(origin.id, JobStatus.INCOMPLETE, outcome=f"waiting_on_user: approval #{approval}")
    item = rules.deny(approval, note="use the local copy instead")
    assert item["status"] == "denied" and item["owner_note"] == "use the local copy instead"
    follow = store.get(item["followup_job_id"])
    assert follow.status == JobStatus.QUEUED and "The owner's note: use the local copy instead" in follow.prompt
    follow = start(store, follow.id)
    again = rules.decide(follow, bash(ASKS))
    assert (again.decision, again.effect) == ("deny", "never") and "denied this exact command" in again.reason
    other = rules.decide(follow, bash("curl -sS -X POST http://192.0.2.2/other"))
    assert other.effect == "ask"                # a different action still gets its card
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM approvals WHERE input_hash=(SELECT input_hash FROM approvals "
                          "WHERE id=?)", (approval,)).fetchone()[0] == 1
    row = [r for r in audit(store) if r["tool"] == "approval deny"][0]
    assert row["source"] == "owner" and "use the local copy instead" in row["policy"]


def test_deny_of_a_finished_chat_turn_does_not_wake(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    store.finish(origin.id, JobStatus.SUCCEEDED, outcome="done")
    assert rules.deny(approval)["followup_job_id"] is None


def test_deny_in_the_asking_turn_stops_recarding(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    rules.deny(approval, follow_up=False)
    again = rules.decide(origin, bash(ASKS))
    assert again.effect == "never" and again.approval_id == approval


def test_deny_endpoint_takes_a_note(settings):
    with Desk(settings) as d:
        bot = d.bot("A")
        job, _ = run_job(d.store, bot)
        approval = d.app.state.scheduler.rules.decide(job, bash(ASKS)).approval_id
        r = d.client.post(f"/v1/approvals/{approval}/deny", headers=MASTER, json={"note": "no", "follow_up": False})
        assert r.status_code == 200 and r.json()["owner_note"] == "no"
        assert d.client.post(f"/v1/approvals/{approval}/deny", headers=MASTER).status_code == 409


# --- item 4: labels and Reviewed-by lines on private PRs stay card-free ---

@pytest.mark.parametrize("command", [
    "gh pr edit 951 -R acme/app --add-label status:ready-to-deploy",
    "gh pr review 951 -R acme/app --comment -b 'Reviewed-by: Web Lead (BO) at abc1234'",
    "gh pr review 951 -R acme/app --approve -b 'Reviewed-by: Web Lead (BO)'",
    "gh pr comment 951 -R acme/app --body 'Reviewed-by: Web Lead (BO)'",
])
def test_private_pr_labels_and_review_lines_need_no_card(office, command):
    assert judge(office, command)[0] == "allow", command


def test_public_pr_review_line_still_asks(office):
    assert judge(office, "gh pr review 3 -R acme/office --comment -b 'Reviewed-by: X'")[0] == "ask"


# --- item 5: own sandbox desktop input ---

@pytest.mark.parametrize("command,effect", [
    ("desk click 100 200", "allow"),
    ("desk key Return", "allow"),
    ("desk type 'Prusa MK4 nozzle'", "allow"),
    ("desk type https://example.com/a?b=1", "allow"),
    ("desk type hunter2", "ask"),
    ("desk type \"$(cat ~/.pw)\"", "ask"),
    ("desk type $PASSWORD", "ask"),
    ("desk type my password is x", "ask"),
    ("desk type ghp_abcdefghijklmnopqrstu", "ask"),
    ("cat pw | xargs desk type", "ask"),
    ("cat pw | xargs -I{} desk type {}", "ask"),
    ("cat pw | xargs -IX desk type X", "ask"),
    ("find . -name pw -exec desk type {} ;", "ask"),
    ("grep -n 'desk type' notes.md", "allow"),
    ("desk --slot desktop-1 click 1 2", "ask"),
    ("desk --slot=desktop-1 type hi", "ask"),
    ("desk click 10 20 --sends 'the reply to Bob'", "ask"),
    ("desk screenshot", "allow"),
])
def test_desk_input_policy(command, effect, monkeypatch):
    monkeypatch.delenv("BOT_OFFICE_DESKTOP_MODE", raising=False)
    verdict = policy.judge("Bash", command, [], {}, policy.Scope(workspace="/tmp/ws", cwd="/tmp/ws"))
    assert verdict.effect == effect, verdict


def test_pool_mode_keeps_the_card(monkeypatch):
    monkeypatch.setenv("BOT_OFFICE_DESKTOP_MODE", "pool")
    assert policy.judge("Bash", "desk click 1 2", [], {}, policy.Scope(workspace="/w", cwd="/w")).effect == "ask"


def test_desk_sends_flag_is_not_input(monkeypatch):
    seen = []
    monkeypatch.setattr(desk, "pick_desktop", lambda slot, env, **kw: seen.append(kw) or ("desktop-12", False))
    monkeypatch.setattr(desk, "_run", lambda argv: seen.append(argv))
    assert desk.main(["click", "1", "2", "--sends", "reply to Bob"]) == 0
    assert seen[0]["args"] == ["1", "2"] and seen[1][-2:] == ["click", "1"][:0] + seen[1][-2:]
    assert "reply to Bob" not in " ".join(seen[1]) and "mousemove" in seen[1]


# --- item 6: desktop sessions ---

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def limits(clock, base=60, step=60, cap=240, idle=20):
    return bd.SessionLimits(base * 60, step * 60, cap * 60, idle * 60, clock)


def use(lim, clock, minutes, name="desktop-10"):
    """Keep the desktop busy for *minutes* (a desk command every 10 minutes); the last answer."""
    answer = None
    for step in range(minutes // 10):
        clock.t += 10 * 60
        answer = lim.check(name, "click", f"{clock.t}")
    return answer


def test_session_limit_and_capped_extensions():
    clock = Clock()
    lim = limits(clock)
    assert lim.check("desktop-10", "click", "a") is None
    assert use(lim, clock, 30) is None
    ok, text = lim.extend("desktop-10", "long upload still running", True)
    assert not ok and "still left" in text                 # no stacking early
    assert "time limit" in use(lim, clock, 30)
    assert lim.extend("desktop-10", "x", True)[0] is False                 # a reason is required
    assert lim.extend("desktop-10", "long upload still running", False)[0] is False   # an active turn too
    last = None
    for n in range(3):
        ok, text = lim.extend("desktop-10", f"long upload still running {n}", True)
        assert ok, text
        assert lim.check("desktop-10", "click", f"c{n}") is None
        last = use(lim, clock, 60)
        assert last
    assert "hard limit" in last
    ok, text = lim.extend("desktop-10", "please, one more hour", True)
    assert not ok and "hard limit" in text
    assert lim.over("desktop-10", 0) == "session time limit"


def test_refused_use_keeps_the_session_a_gap_resets_it():
    clock = Clock()
    lim = limits(clock, base=60)
    lim.check("desktop-10", "click", "a")
    assert use(lim, clock, 60)
    assert use(lim, clock, 30)                                # still the same session (refusals count as use)
    clock.t += 21 * 60
    assert lim.check("desktop-10", "click", "a") is None      # 20 min with no desk use: a new session


def test_loop_and_no_progress_stop():
    clock = Clock()
    lim = limits(clock)
    for _ in range(bd.LOOP_ACTIONS - 1):
        assert lim.check("desktop-10", "click", "same") is None
    assert lim.check("desktop-10", "click", "same") == "loop"
    assert "stopped" in lim.check("desktop-10", "click", "other")
    assert lim.extend("desktop-10", "I need it anyway", True)[0] is False
    assert lim.over("desktop-10", 0).startswith("loop")
    other = limits(clock)
    for n in range(bd.LOOP_SHOTS - 1):
        other.check("desktop-11", "click", f"x{n}")
        assert other.screenshot("desktop-11", "f" * 32) is None
        clock.t += 30
    assert other.screenshot("desktop-11", "f" * 32) == "no progress"
    quick = limits(clock)                      # identical shots within a few seconds: waiting, not stuck
    for _ in range(3 * bd.LOOP_SHOTS):
        assert quick.screenshot("desktop-14", "e" * 32) is None
    assert "stopped" in other.check("desktop-11", "key", "Return")
    paging = limits(clock)                     # a Next button with the screen changing each time: progress
    for n in range(3 * bd.LOOP_ACTIONS):
        assert paging.check("desktop-12", "click", "next") is None
        paging.screenshot("desktop-12", f"{n:032x}")
    keys = limits(clock)
    for _ in range(bd.LOOP_KEYS - 1):
        assert keys.check("desktop-13", "key", "Page_Down") is None
    assert keys.check("desktop-13", "key", "Page_Down") == "loop"


def test_agent_api_refuses_after_the_limit_and_extends(settings, tmp_path):
    with Desk(settings) as d:
        manager = fake_manager(d, tmp_path)
        clock = Clock()
        manager.limits.clock = clock
        a = d.bot("A")
        _, ha = run_job(d.store, a)
        first = d.client.post("/v1/agent/desktop", headers=ha, json={"command": "click", "input": "ab" * 16})
        assert first.status_code == 200 and first.json()["session"]["active"]
        for _ in range(6):
            clock.t += 10 * 60
            refused = d.client.post("/v1/agent/desktop", headers=ha, json={"command": "click", "input": "cd" * 16})
        assert refused.status_code == 429 and "desk extend" in refused.json()["detail"]
        short = d.client.post("/v1/agent/desktop/extend", headers=ha, json={"reason": "more"})
        assert short.status_code == 409
        ok = d.client.post("/v1/agent/desktop/extend", headers=ha, json={"reason": "the export takes 90 minutes"})
        assert ok.status_code == 200 and "extended by 60 minutes" in ok.json()["detail"]
        again = d.client.post("/v1/agent/desktop", headers=ha, json={"command": "click", "input": "cd" * 16})
        assert again.status_code == 200
        rows = [r for r in audit(d.store) if r["tool"] == "desk extend"]
        assert [r["policy"].split(":")[0] for r in rows] == ["refused", "extended"]


def test_agent_api_loop_stop_is_audited(settings, tmp_path):
    with Desk(settings) as d:
        fake_manager(d, tmp_path)
        a = d.bot("A")
        _, ha = run_job(d.store, a)
        codes = [d.client.post("/v1/agent/desktop", headers=ha, json={"command": "click", "input": "ee" * 16}).status_code
                 for _ in range(bd.LOOP_ACTIONS)]
        assert codes[:-1] == [200] * (bd.LOOP_ACTIONS - 1) and codes[-1] == 429
        assert any(r["tool"] == "desk session stop" for r in audit(d.store))


def test_remote_view_sessions_extend_in_capped_steps():
    sessions = remote_access.ViewSessions(step=3600, cap=4 * 3600)
    key = sessions.open("owner@example.test", "/v1/desktops/desktop-10/vnc", 0.0)
    assert sessions.ends_at(key) == remote_access.MAX_REMOTE_WEBSOCKET_SECONDS
    assert sessions.extend("someone@example.test", "/v1/desktops/desktop-10/vnc", 10.0) == []
    for n in range(3):
        assert sessions.extend("owner@example.test", "/v1/desktops/desktop-10/vnc", 10.0)[0]["extended"]
    assert sessions.ends_at(key) == 4 * 3600
    assert not sessions.extend("owner@example.test", "/v1/desktops/desktop-10/vnc", 10.0)[0]["extended"]
    sessions.close(key)
    assert sessions.ends_at(key) == 0.0


def test_local_view_has_no_limit(settings):
    with Desk(settings) as d:
        r = d.client.post("/v1/desktops/desktop-10/extend-view", headers=MASTER, json={"reason": "long task"})
        assert r.status_code == 200 and r.json()["limited"] is False


# --- Codex round 1 regressions ---

def test_approved_but_not_yet_bound_is_not_recarded(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    rules.mark_approved(approval)               # the window before approve() binds the resume turn
    retry = rules.decide(origin, bash(ASKS))
    assert retry.approval_id == approval and "already granted" in retry.reason


def test_a_grant_for_one_task_does_not_swallow_another_tasks_card(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    store.finish(origin.id, JobStatus.INCOMPLETE, outcome="waiting_on_user: x")
    rules.approve(approval)
    other = running(store, bot)
    decision = rules.decide(other, bash(ASKS))
    assert decision.effect == "ask" and "already granted" not in decision.reason


def test_resume_step_now_allowed_by_policy_uses_the_grant(tmp_path):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    store.finish(origin.id, JobStatus.INCOMPLETE, outcome="waiting_on_user: x")
    resume = start(store, rules.approve(approval)["resume_job_id"])
    with closing(store._connect()) as db:      # the owner opens it with an allow rule meanwhile
        db.execute("INSERT INTO action_rules (bot_id,effect,kind,pattern,scope,note,created_at) VALUES "
                   "(NULL,'allow','shell',?, 'all','test',?)", ("^" + ASKS.replace(".", "\\.").replace("?", "\\?")
                                                               .replace("$", "\\$") + "$", "2026-11-26T00:00:00"))
    decision = rules.decide(resume, bash(ASKS))
    assert (decision.decision, decision.effect) == ("allow", "allow"), decision
    assert rules.get(approval)["status"] == "consumed"
    assert rules.resume_ended(resume) == []


def test_deny_whose_follow_up_cannot_be_queued_still_denies_and_says_so(tmp_path, monkeypatch):
    store, bot, rules = setup(tmp_path)
    origin = running(store, bot)
    approval = rules.decide(origin, bash(ASKS)).approval_id
    store.finish(origin.id, JobStatus.INCOMPLETE, outcome="waiting_on_user: x")

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "enqueue_message", broken)
    item = rules.deny(approval)
    assert item["status"] == "denied" and item["follow_up_error"] == "OSError" and item["followup_job_id"] is None
    assert any("could not be woken" in m.text for m in store.list_messages(bot.id))


def test_stale_loop_stop_spares_a_new_session(settings, tmp_path):
    import asyncio
    with Desk(settings) as d:
        manager = fake_manager(d, tmp_path)
        a = d.bot("A")
        _, ha = run_job(d.store, a)
        assert d.client.post("/v1/agent/desktop", headers=ha, json={"command": "click", "input": "aa" * 16}).status_code == 200
        name = manager.name_of(str(a.id))
        manager.limits.sessions[name].stopped = "loop: test"
        old = manager.limits.sessions[name]
        manager.limits.sessions[name] = bd.Session(started=0, last_activity=0, budget=3600)   # a new session meanwhile
        assert asyncio.run(manager.stop_now(name, "loop")) is False
        manager.limits.sessions[name] = old
        assert asyncio.run(manager.stop_now(name, "loop")) is True


def test_no_desk_activity_stops_even_during_a_turn():
    clock = Clock()
    lim = limits(clock)
    lim.check("desktop-10", "click", "a")
    clock.t += 21 * 60
    assert lim.over("desktop-10", bd.SESSION_GRACE) == "no desk activity"
