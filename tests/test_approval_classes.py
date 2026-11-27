"""0.11.10 approval classes: the owner picks kinds of action that wait for their yes; bots rarely ask."""
import json
import shutil
import subprocess
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("github_private")  # named GitHub merges reach their card
from fastapi.testclient import TestClient
from guard_repo import clone_workspace

from botoffice import approval_classes
from botoffice.api import create_app
from botoffice.domain import JobStatus
from botoffice.needs_you import needs_you
from botoffice.providers.fake import FakeAdapter
from botoffice.rules import Rules
from botoffice.store import JobStore, now

H = {"Authorization": "Bearer test-token"}


WORK = clone_workspace()


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": WORK}


def tool(name, **arguments):
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": arguments}


def setup(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    return store, store.get_bot(bot.id), Rules(store)


def running(store, bot, source="chat"):
    job = store.create("work", "claude", bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store.get(job.id)


@pytest.mark.parametrize("event,expected", [
    (tool("mcp__gmail__send_message", to="a@b.c"), "send"),
    (tool("mcp__gmail__reply", body="hi"), "send"),
    (tool("mcp__slack__post_message", text="hi"), "send"),
    (tool("mcp__gmail__create_draft", body="hi"), None),
    (tool("mcp__gmail__search_threads", query="x"), None),
    (bash("echo hi | sendmail joe@example.com"), "send"),
    (bash("desk grok send --to G --text hi"), None),  # a message to a bot, not a person
    (bash("bot-msg Bea 'status?'"), None),
    (tool("mcp__shop__checkout", cart="1"), "purchase"),
    (tool("mcp__stripe__create_payment", amount=5), "purchase"),
    (tool("mcp__stripe__get_payment_details"), None),
    (bash("gh pr merge 12 --squash -R o/r"), "git-guard"),  # 0.12.4: the guard asks first
    (bash("npm publish"), "release"),
    (bash("git push origin main"), "git-guard"),
    (bash("git push --force origin feature"), "git-guard"),
    (bash("git push origin feature"), None),
    (bash("git push origin maintenance"), None),
    (bash("sudo apt-get install x"), "system"),
    (bash("crontab -l"), None),  # 0.12.45: a read (classes judge the commands that run)
    (bash("crontab -r"), "system"),
    (bash("rm -rf /srv/shared/project"), "git-guard"),
    (bash("rm -rf build"), None),
    (tool("mcp__drive__trash_file", id="1"), "delete"),
    (tool("mcp__gmail__delete_draft", id="1"), "delete"),
    (tool("delete_file", path="a.txt"), None),  # Cursor's own tool, inside the workspace
    (bash("ssh host uptime"), "remote"),  # 0.12.45: the policy's "remote" class (the Settings class is off)
    (bash("pytest -q"), None),
    (tool("Edit", file_path="a.py", old_string="x", new_string="sudo rm -rf /"), None),
])
def test_default_classes(tmp_path, event, expected):
    store, bot, rules = setup(tmp_path)
    decision = rules.decide(running(store, bot), event)
    if expected is None:
        assert decision.decision == "allow", decision
    else:
        assert (decision.decision, decision.effect, decision.preset) == ("deny", "ask", expected)
        assert "waiting_on_user" in decision.reason and "Never repackage the refused action" in decision.reason


def test_settings_lists_defaults_and_switches_classes(tmp_path):
    store, bot, rules = setup(tmp_path)
    current = approval_classes.settings(store)
    on = {item["key"] for item in current["classes"] if item["enabled"]}
    assert on == {"send", "purchase", "release", "system", "delete"}
    assert current["timeout_min"] == approval_classes.TIMEOUT_DEFAULT
    approval_classes.update(store, enabled={"release": False, "remote": True})
    job = running(store, bot)
    # the release class is off, but the git guard is not a class: it cannot be switched off
    assert rules.decide(job, bash("git push origin main")).preset == "git-guard"
    # 0.12.45: switching a Settings class off does not open the policy (publishing still asks)
    assert rules.decide(job, bash("npm publish")).preset == "publish"
    assert rules.decide(job, bash("ssh host uptime")).preset == "remote"
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, remove=["send"])
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, enabled={"nope": True})


def test_custom_classes(tmp_path):
    store, bot, rules = setup(tmp_path)
    after = approval_classes.update(store, add=[{"label": "Deploys", "kind": "shell", "pattern": r"\bmake\s+deploy\b"},
                                               {"label": "Calendar invites", "kind": "tool",
                                                "pattern": r"create_event"}])
    keys = [item["key"] for item in after["classes"] if not item["builtin"]]
    assert keys == ["custom-1", "custom-2"]
    job = running(store, bot)
    assert rules.decide(job, bash("make deploy")).preset == "custom-1"
    assert rules.decide(job, tool("mcp__gcal__create_event", title="x")).preset == "custom-2"
    approval_classes.update(store, enabled={"custom-2": False}, remove=["custom-1"])
    assert rules.decide(job, bash("make deploy")).preset == "deploy"  # 0.12.45: the policy still asks
    # 0.12.45: a connector write (an invite goes to people) still asks under the policy
    assert rules.decide(job, tool("mcp__gcal__create_event", title="y")).preset == "connector"
    for bad in ({"label": "", "kind": "shell", "pattern": "x"}, {"label": "x", "kind": "web", "pattern": "x"},
                {"label": "x", "kind": "shell", "pattern": "("}):
        with pytest.raises(approval_classes.ApprovalClassError):
            approval_classes.update(store, add=[bad])
    # a number is never reused while the old class still exists
    again = approval_classes.update(store, add=[{"label": "Again", "kind": "shell", "pattern": "x"}])
    assert [item["key"] for item in again["classes"] if not item["builtin"]] == ["custom-2", "custom-3"]


def test_an_owner_allow_rule_beats_a_class(tmp_path):
    store, bot, rules = setup(tmp_path)
    rules.add_rule(bot_id=bot.id, effect="allow", kind="shell", pattern=r"\bnpm\s+publish\b")
    rules.add_rule(bot_id=bot.id, effect="allow", kind="shell", pattern=r"\bgh\s+pr\s+merge\b")
    assert rules.decide(running(store, bot), bash("npm publish")).decision == "allow"
    # 0.12.4: but no allow rule opens the git guard
    assert rules.decide(running(store, bot), bash("gh pr merge 3 -R o/r")).effect == "ask"


def test_chat_card_approve_hides_the_resume_prompt(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    approval = rules.decide(job, tool("mcp__gmail__send_message", to="joe@example.com")).approval_id
    rules.decide(job, tool("mcp__gmail__send_message", to="joe@example.com"))  # asked again: one card
    cards = [m for m in store.list_messages(bot.id) if m.approval]
    assert len(cards) == 1
    card = cards[0].approval
    assert card["id"] == approval and card["status"] == "pending" and card["label"] == "Sending email or messages"
    assert "joe@example.com" in card["summary"] and cards[0].author == "system"
    store.finish(job.id, JobStatus.INCOMPLETE)
    item = rules.approve(approval)
    visible = store.list_messages(bot.id)
    assert [m.approval["status"] for m in visible if m.approval] == ["approved"]
    assert not any(m.job_id and str(m.job_id) == item["resume_job_id"] for m in visible)
    hidden = [m for m in store.list_messages(bot.id, include_hidden=True)
              if m.job_id and str(m.job_id) == item["resume_job_id"]]
    assert hidden and hidden[0].hidden and hidden[0].text.startswith(f"Approval #{approval} granted")


def test_deny_and_timeout_leave_a_short_note(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    long = "x" * 1000
    denied = rules.decide(job, bash(f"sudo echo {long}")).approval_id
    rules.deny(denied)
    note = store.list_messages(bot.id)[-1]
    assert note.role == "system" and note.text.startswith(f"Approval #{denied} denied: ")
    assert len(note.text) < 300
    timed = rules.decide(job, bash("sudo reboot")).approval_id
    with closing(store._connect()) as db:
        db.execute("UPDATE approvals SET expires_at=? WHERE id=?", ((now() - timedelta(seconds=1)).isoformat(), timed))
    assert rules.expire() == 1
    assert rules.expire() == 0  # one note only
    texts = [m.text for m in store.list_messages(bot.id) if m.role == "system" and not m.approval]
    assert sum("timed out" in text for text in texts) == 1
    cards = {m.approval["id"]: m.approval["status"] for m in store.list_messages(bot.id) if m.approval}
    assert cards == {denied: "denied", timed: "expired"}
    # still approvable from the card ("Approve and run")
    store.finish(job.id, JobStatus.INCOMPLETE)
    assert rules.approve(timed)["status"] == "approved"


def test_needs_you_names_the_class(tmp_path):
    store, bot, rules = setup(tmp_path)
    rules.decide(running(store, bot), tool("mcp__shop__checkout"))
    items = [item for item in needs_you(store) if item["kind"] == "approval"]
    assert items and items[0]["class"] == "Purchases and payments"


def test_settings_api(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        got = client.get("/v1/settings", headers=H).json()["approvals"]
        assert {item["key"] for item in got["classes"]} >= set(approval_classes.BUILTIN_KEYS)
        assert set(got["gates"]) >= {"claude", "codex", "opencode"} and got["gates"]["opencode"] == "none"
        # round 2 (A16): Sending is locked on; switching it off is refused
        assert client.put("/v1/settings", headers=H, json={"approvals": {"enabled": {"send": False}}}).status_code == 422
        changed = client.put("/v1/settings", headers=H, json={"approvals": {
            "enabled": {"release": False}, "timeout_min": 15,
            "add": [{"label": "Deploys", "kind": "shell", "pattern": "deploy"}]}}).json()["approvals"]
        by_key = {item["key"]: item for item in changed["classes"]}
        assert by_key["send"]["enabled"] and by_key["send"]["locked"] and not by_key["release"]["enabled"]
        assert by_key["custom-1"]["enabled"] and changed["timeout_min"] == 15
        assert client.put("/v1/settings", headers=H, json={"approvals": {"remove": ["send"]}}).status_code == 422
        assert client.put("/v1/settings", headers=H, json={"approvals": {"timeout_min": -1}}).status_code == 422
        assert client.put("/v1/settings", json={"approvals": {"enabled": {"send": True}}}).status_code == 401
        store = client.app.state.store
        bot = store.create_bot("Ada", "", "codex", None)
        job = running(store, store.get_bot(bot.id))
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        assert client.put("/v1/settings", headers=agent,
                          json={"approvals": {"enabled": {"send": False}}}).status_code == 401
        # the chat's messages carry the card
        Rules(store).decide(job, bash("sudo reboot"))
        listed = client.get(f"/v1/bots/{bot.id}/messages", headers=H).json()
        cards = [m["approval"] for m in (listed if isinstance(listed, list) else listed["messages"]) if m["approval"]]
        assert cards and cards[0]["label"] == "Admin and system changes" and cards[0]["status"] == "pending"


class Gated:
    """An engine whose turn tries to send an email through Bot-Office's gate, as a CLI hook would."""

    name = "codex"
    enabled = True

    def __init__(self):
        self.scheduler = None

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        from botoffice.providers.base import RunResult
        log_path.write_text("log")
        decision = self.scheduler.rules.decide(job, tool("mcp__gmail__send_message", to="joe@example.com"))
        if decision.decision == "allow":
            return RunResult("succeeded", exit_code=0, final_text="Sent.\n\nBOT-OFFICE-STATUS: done")
        return RunResult("succeeded", exit_code=0, final_text=(
            f"Drafted.\n\nBOT-OFFICE-STATUS: waiting_on_user — approval #{decision.approval_id}"))


def test_a_real_turn_waits_on_the_card_and_approve_finishes_it(settings):
    from test_loop import run_all

    from botoffice.scheduler import Scheduler
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    engine = Gated()
    scheduler = Scheduler(store, settings, {"codex": engine})
    engine.scheduler = scheduler
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.turns.post_message(store.get_bot(bot.id), "email Joe the report", source="chat")
    jobs = run_all(store, scheduler)
    assert len(jobs) == 1  # auto-continue does not run while the approval waits
    visible = store.list_messages(bot.id)
    assert [m.approval["status"] for m in visible if m.approval] == ["pending"]
    # the "Not done (waiting on you)" line would only repeat the card
    assert not any(m.role == "system" and m.text.startswith("Not done") for m in visible)
    approval = next(m.approval["id"] for m in visible if m.approval)
    scheduler.rules.approve(approval)
    jobs = run_all(store, scheduler)
    assert len(jobs) == 1 and jobs[0].status == JobStatus.SUCCEEDED
    visible = store.list_messages(bot.id)
    assert [m.approval["status"] for m in visible if m.approval] == ["consumed"]
    assert visible[-1].text == "Sent." and [m.text for m in visible if m.author == "owner"] == ["email Joe the report"]


def test_engines_that_cannot_pause_are_named_and_told_not_to_act(tmp_path):
    from botoffice.workspace import self_extra
    store = JobStore(tmp_path / "db.sqlite3")
    claude = store.create_bot("Cleo", "", "claude", None)
    loose = store.create_bot("Opal", "", "opencode", None)
    store.update_bot(loose.id, cli_access="full")
    names = {item["name"] for item in approval_classes.unpausable_bots(store)}
    assert "Opal" in names and "Cleo" not in names
    assert approval_classes.can_pause("codex", "full") and not approval_classes.can_pause("codex", "restricted")
    for bot, expect in ((claude, "Bot-Office pauses that one step"), (loose, "never do these yourself")):
        bot = store.get_bot(bot.id)
        job = store.create("work", bot.default_provider, bot_id=bot.id, source="chat")
        extra, _, stable = self_extra(store, bot, job, "", now())
        assert expect in extra and "Sending email or messages" in extra and "Sending email" in stable
    for item in approval_classes.BUILTIN:
        if item.key not in approval_classes.LOCKED:
            approval_classes.update(store, enabled={item.key: False})
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, enabled={"send": False})
    note = approval_classes.self_note(store, "opencode", "full")  # the git guard and Sending are always on
    assert "Protected action" in note and "Sending email" in note


def test_settings_show_unpausable_bots_by_each_class_and_docs_say_checkpoint():
    """Settings names the bots a class can only advise next to every class that is on; the docs
    describe classes as a checkpoint / tripwire, not enforcement."""
    from pathlib import Path
    root = Path(__file__).parents[1]
    html = (root / "src" / "botoffice" / "static" / "bot-office.html").read_text()
    start = html.index("function showApprovals(data)")
    body = html[start:html.index("\n}\n", start)]
    assert "c.enabled && loose.length" in body and "Advice only, can't pause: " in body
    assert "This is defense in depth, not an inescapable boundary." in html and "pauses that one step" not in html
    docs = (root / "docs" / "APPROVALS.md").read_text()
    assert "A checkpoint, not enforcement." in docs and "fails closed" in docs and "s=sudo; $s" in docs
    readme = (root / "README.md").read_text()
    assert "defense in depth, not an inescapable boundary" in readme and "every run" not in readme[readme.index("**Rarely ask"):][:4000]


# --- custom patterns: the bot controls the input ---

def test_a_custom_pattern_that_backtracks_is_refused_on_save(tmp_path):
    store, bot, rules = setup(tmp_path)
    for slow in (r"(a+)+$", r"^(\s*)*$", r"(x+x+)+y"):
        with pytest.raises(approval_classes.ApprovalClassError, match="too slow"):
            approval_classes.update(store, add=[{"label": "Slow", "kind": "shell", "pattern": slow}])
    assert not [item for item in approval_classes.settings(store)["classes"] if not item["builtin"]]
    approval_classes.update(store, add=[{"label": "Deploys", "kind": "shell", "pattern": r"\bmake\s+deploy\b"}])
    job = running(store, bot)
    assert rules.decide(job, bash("make deploy")).preset == "custom-1"


def test_a_custom_pattern_sees_a_bounded_command_and_a_longer_one_asks(tmp_path):
    store, bot, rules = setup(tmp_path)
    approval_classes.update(store, add=[{"label": "Deploys", "kind": "shell", "pattern": r"\bmake\s+deploy\b"}])
    [custom] = [item for item in approval_classes.active(store) if item.key == "custom-1"]
    assert not custom.matches("Bash", "ls " + "a" * 4000)
    # padding past the bound cannot hide the matching part: a longer command is in the class
    padded = "echo " + " " * approval_classes.CUSTOM_MATCH_CHARS + "; make deploy"
    assert custom.matches("Bash", padded)
    assert rules.decide(running(store, bot), bash(padded)).preset == "custom-1"
    # built-in classes are not affected by the bound
    assert not [item for item in approval_classes.active(store) if item.custom is False
                and item.matches("Bash", "ls " + "a" * 5000)]


def test_the_approval_card_collapses_whitespace_and_says_what_is_clipped():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src" / "botoffice" / "static" / "bot-office.html").read_text()
    start = html.index("const APPROVAL_SHOWN_CHARS")
    preview = html[start:html.index("function renderApproval", start)]
    card = html[html.index("function renderApproval"):html.index("function renderQuestion")]
    assert "more characters" in preview and "more lines" in preview and "in all" in preview
    assert "blank lines⟩" in preview and "spaces⟩" in preview
    assert "approvalPreview(a.summary" in card and '"Show all"' in card and "a.summary" in card.split("all.onclick")[1]
    assert "innerHTML" not in card
    node = shutil.which("node")
    if node is None:
        return
    script = preview + """
const out = {};
out.pad = approvalPreview("ls" + " ".repeat(300) + "&& rm -rf ~");
out.lines = approvalPreview("echo hi" + "\\n".repeat(60) + "rm -rf ~");
out.long = approvalPreview("x".repeat(2000));
out.short = approvalPreview("git status");
console.log(JSON.stringify(out));
"""
    result = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert result["pad"]["shown"] == "ls ⟨300 spaces⟩ && rm -rf ~" and result["pad"]["changed"]
    assert result["lines"]["shown"] == "echo hi\n⟨59 blank lines⟩\nrm -rf ~"
    assert result["long"]["more"] == "… 1400 more characters not shown (2000 in all)"
    assert result["short"] == {"shown": "git status", "more": "", "changed": False}
