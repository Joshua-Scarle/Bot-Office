"""Standing-rule tripwires: any git push asks, starting a print asks, phone calls are refused,
outbound messages stay locked behind the send class."""
import json
from contextlib import closing

import pytest
from guard_repo import clone_workspace

from botoffice import approval_classes, safety_tripwires
from botoffice.rules import Rules
from botoffice.store import JobStore

WORK = clone_workspace()


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": WORK}


def tool(name, **arguments):
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": arguments}


def running(store, bot, source="chat"):
    job = store.create("work", "claude", bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store.get(job.id)


def setup(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    return store, store.get_bot(bot.id), Rules(store)


def key(event):
    from botoffice.rules import parse_event
    action = parse_event(event)
    trip = safety_tripwires.judge(action.tool, action.command)
    return trip.key if trip else None


PUSHES = [
    "git push origin feature/x",
    "git push",
    "git -C repo push -u origin HEAD",
    "cd repo && git push origin topic",
    "bash -c 'git push origin topic'",
    "echo $(git push origin topic)",
    "gh repo sync owner/repo",
    "gh api -X PUT repos/o/r/contents/a.txt -f message=m -f content=Zg==",
    "gh api repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc",
    "curl -X POST https://api.github.com/repos/o/r/git/refs -d '{}'",
]
NOT_PUSHES = [
    "git status", "git commit -m 'fix git push docs'", "git log origin/main", "git pull origin main",
    "echo git push", "gh api repos/o/r/contents/a.txt", "gh api repos/o/r/git/refs",
    "curl https://api.github.com/repos/o/r/contents/a.txt", "grep push README.md",
]


@pytest.mark.parametrize("command", PUSHES)
def test_any_push_is_tripped(command):
    assert key(bash(command)) == "push"


@pytest.mark.parametrize("command", NOT_PUSHES)
def test_reads_and_mentions_are_not_pushes(command):
    assert key(bash(command)) is None


@pytest.mark.parametrize("name", ["mcp__github__push_files", "mcp__github__create_or_update_file",
                                  "mcp__github__create_branch", "mcp__gitlab__commit_files",
                                  "mcp__git__push"])
def test_connector_pushes_are_tripped(name):
    assert key(tool(name, owner="o", repo="r")) == "push"


@pytest.mark.parametrize("name", ["mcp__github__get_file_contents", "mcp__github__list_branches",
                                  "mcp__fs__create_file", "mcp__github__get_commit"])
def test_connector_reads_and_local_files_are_not_pushes(name):
    assert key(tool(name, owner="o", repo="r")) is None


def test_push_asks_in_chat_and_in_routines_and_a_card_is_made(tmp_path):
    store, bot, rules = setup(tmp_path)
    for source in ("chat", "routine"):
        decision = rules.decide(running(store, bot, source), bash("git push origin feature"))
        assert decision.decision == "deny" and decision.effect == "ask" and decision.approval_id
        assert decision.preset == "push"
        assert "Do not run it another way" in decision.reason
    card = rules.get(decision.approval_id)
    assert card["preset"] == "push" and card["status"] == "pending"


def test_push_approval_covers_only_that_exact_command(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    first = rules.decide(job, bash("git push origin feature"))
    assert rules.approve(first.approval_id)["status"] == "approved"
    assert rules.decide(job, bash("git push origin other")).effect == "ask"


def test_push_card_is_not_bypassed_by_the_share_class_being_off_or_on(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    assert approval_classes.settings(store)  # share defaults off
    assert rules.decide(job, bash("git push origin feature")).effect == "ask"
    approval_classes.update(store, enabled={"share": True})
    assert rules.decide(job, bash("git push origin feature")).effect == "ask"
    # ordinary PR comments are not carded by this tripwire
    approval_classes.update(store, enabled={"share": False})
    assert rules.decide(job, bash("gh pr comment 3 --body hi")).decision == "allow"


def test_force_push_and_main_still_use_the_git_guard(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    assert rules.decide(job, bash("git push origin main")).preset == "git-guard"
    assert rules.decide(job, bash("git push --force origin feature")).preset == "git-guard"
    assert rules.decide(job, bash("gh pr merge 4")).preset == "git-guard"


def test_a_narrow_owner_allow_rule_can_open_pushes_but_never_prints_or_calls(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    rules.add_rule(bot_id=bot.id, effect="allow", kind="shell", pattern=r"\bgit\s+push\s+origin\s+bot/\S+$")
    assert rules.decide(job, bash("git push origin bot/topic")).decision == "allow"
    assert rules.decide(job, bash("git push origin other")).effect == "ask"
    rules.add_rule(bot_id=None, effect="allow", kind="shell", pattern=r".*")
    assert rules.decide(job, bash("prusa-link print start job.gcode")).effect == "ask"
    assert rules.decide(job, bash("dial +15555550100")).effect == "never"


def test_push_pre_push_check_is_not_asked_a_second_time(tmp_path):
    assert safety_tripwires.pushing("GitPush", "") is None


# --- print -------------------------------------------------------------------------------------------

PRINTS = [
    "prusa-link print start part.gcode",
    "octoprint start",
    "octoprint-cli job resume",
    "lp -d Prusa part.pdf",
    "lpr report.txt",
    "curl -X POST http://printer.local/api/job -d '{\"command\":\"start\"}'",
    "curl -X POST 'http://printer/printer/print/start?filename=a.gcode'",
    "curl -X PUT http://printer.local/api/v1/files/usb/a.gcode -H 'Print-After-Upload: ?1' -T a.gcode",
    "curl -X DELETE http://printer.local/api/v1/job/5",
    "cd /tmp && lpr x",
    "bash -c 'octoprint start'",
]
NOT_PRINTS = [
    "octoprint status", "prusa-link status", "curl http://printer.local/api/v1/job", "lpstat -p",
    "echo lp", "grep print notes.md", "python3 -c 'print(1)'", "desk click 10 10", "ls prints/",
]


@pytest.mark.parametrize("command", PRINTS)
def test_print_commands_are_tripped(command):
    assert key(bash(command)) == "print"


@pytest.mark.parametrize("command", NOT_PRINTS)
def test_non_print_commands_are_not_tripped(command):
    assert key(bash(command)) is None


@pytest.mark.parametrize("name", ["mcp__printer__start_print", "mcp__octoprint__print_file",
                                  "mcp__prusa__resume_print", "mcp__octoprint__cancel_job"])
def test_print_connector_tools_are_tripped(name):
    assert key(tool(name, file="a.gcode")) == "print"


@pytest.mark.parametrize("name", ["mcp__printer__get_print_status", "mcp__printer__list_printers",
                                  "mcp__octoprint__job_status"])
def test_print_connector_reads_are_not_tripped(name):
    assert key(tool(name)) is None


def test_print_always_asks_with_devices_class_off_and_no_allow_opens_it(tmp_path):
    store, bot, rules = setup(tmp_path)
    for source in ("chat", "routine"):
        job = running(store, bot, source)
        decision = rules.decide(job, bash("prusa-link print start part.gcode"))
        assert decision.effect == "ask" and decision.preset == "print" and decision.approval_id
    rules.add_rule(bot_id=bot.id, effect="allow", kind="shell", pattern=r"prusa-link")
    assert rules.decide(running(store, bot), bash("prusa-link print start part.gcode")).effect == "ask"
    # desk click / type are still the devices class's business, and it stays off
    assert rules.decide(running(store, bot), bash("desk click 5 5")).decision == "allow"


# --- phone -------------------------------------------------------------------------------------------

CALLS = [
    "dial +15555550100", "call-joshua", "twilio api:core:calls:create --to +1 --from +2 --url x",
    "curl -X POST https://api.twilio.com/2010-04-01/Accounts/AC1/Calls.json -d To=+1",
    "adb shell am start -a android.intent.action.CALL -d tel:+15555550100",
    "xdg-open tel:+15555550100", "termux-telephony-call +15555550100", "bash -c 'dial 123'",
    "linphonec -c x", "asterisk -rx 'originate SIP/x extension 100@default'",
]
NOT_CALLS = [
    "echo call Joshua", "grep -rn 'phone call' docs/", "git commit -m 'no phone calls'", "ls calls/",
    "python3 -m pytest -k call", "curl https://api.twilio.com/2010-04-01/Accounts/AC1/Calls.json",
]


@pytest.mark.parametrize("command", CALLS)
def test_phone_calls_are_tripped(command):
    assert key(bash(command)) == "phone"


@pytest.mark.parametrize("command", NOT_CALLS)
def test_talking_about_calls_is_not_tripped(command):
    assert key(bash(command)) is None


@pytest.mark.parametrize("name", ["mcp__twilio__make_call", "mcp__voice__place_call", "mcp__phone__call",
                                  "mcp__vapi__create_call", "mcp__telephony__dial_number"])
def test_phone_connector_tools_are_tripped(name):
    assert key(tool(name, to="+15555550100")) == "phone"


@pytest.mark.parametrize("name", ["mcp__twilio__list_calls", "mcp__x__call_tool", "mcp__x__function_call",
                                  "mcp__gmail__search_threads"])
def test_other_tools_are_not_phone_calls(name):
    assert key(tool(name)) is None


def test_phone_calls_are_refused_in_every_run_and_no_allow_or_class_opens_them(tmp_path):
    store, bot, rules = setup(tmp_path)
    rules.add_rule(bot_id=None, effect="allow", kind="shell", pattern=r".*")
    for source in ("chat", "routine", "trigger", "wakeup"):
        decision = rules.decide(running(store, bot, source), bash("dial +15555550100"))
        assert decision.decision == "deny" and decision.effect == "never" and decision.preset == "phone"
        assert "Do not retry this or work around it" in decision.reason
    assert rules.decide(running(store, bot), tool("mcp__twilio__make_call", to="+1")).effect == "never"


# --- send stays locked ----------------------------------------------------------------------------

SENDS = [
    tool("mcp__gmail__send_message", to="a@b.c"), tool("mcp__slack__post_message", text="hi"),
    tool("mcp__twilio__send_sms", to="+1", body="hi"), tool("mcp__x__create_tweet", text="hi"),
    tool("mcp__discord__send_dm", user="1", text="hi"), tool("mcp__linkedin__create_post", text="hi"),
    bash("echo hi | sendmail joe@example.com"), bash("mail -s hi joe@example.com < m.txt"),
    bash("curl -X POST https://api.twitter.com/2/tweets -d '{}'"),
    bash("curl -X POST https://hooks.slack.com/services/T/B/x -d '{}'"),
]


@pytest.mark.parametrize("event", SENDS)
def test_outbound_messages_always_ask_even_with_a_blanket_allow_rule(tmp_path, event):
    store, bot, rules = setup(tmp_path)
    rules.add_rule(bot_id=None, effect="allow", kind="shell", pattern=r".*")
    rules.add_rule(bot_id=None, effect="allow", kind="tool", pattern=r".*")
    decision = rules.decide(running(store, bot), event)
    assert decision.effect == "ask" and decision.preset == "send"


def test_send_class_cannot_be_switched_off_or_removed(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, enabled={"send": False})
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, remove=["send"])
    sending = next(c for c in approval_classes.settings(store)["classes"] if c["key"] == "send")
    assert sending["enabled"] and sending["locked"]


def test_a_click_on_post_is_not_pre_approved_by_any_rule(tmp_path):
    store, bot, rules = setup(tmp_path)
    rules.add_rule(bot_id=None, effect="allow", kind="shell", pattern=r".*")
    # a desk click is the devices class's business (off by default); a real send is the send class
    assert rules.decide(running(store, bot), tool("mcp__x__post_tweet", text="hi")).preset == "send"


# --- secrets never land in cards, chat, logs or the bot's refusal text ---------------------------

FAKE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
FAKE_BEARER = "Zx9" + "Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm"


def every_stored_text(store):
    with closing(store._connect()) as db:
        rows = [dict(r) for r in db.execute("SELECT summary FROM approvals")]
        rows += [dict(r) for r in db.execute("SELECT summary FROM gate_log")]
        rows += [dict(r) for r in db.execute("SELECT text FROM messages")]
    return json.dumps(rows)


@pytest.mark.parametrize("command", [
    f"git push https://joshua:{FAKE}@github.com/o/r.git feature",
    f"GITHUB_TOKEN={FAKE} git push origin feature",
    f"git -c http.extraheader='Authorization: Bearer {FAKE_BEARER}' push origin feature",
    f"git push origin feature --push-option=token={FAKE_BEARER}",
])
def test_a_secret_in_a_command_is_not_stored_or_shown_in_a_card(tmp_path, command):
    store, bot, rules = setup(tmp_path)
    decision = rules.decide(running(store, bot), bash(command))
    assert decision.effect == "ask" and decision.approval_id
    stored = every_stored_text(store) + decision.reason
    assert FAKE not in stored and FAKE_BEARER not in stored
    assert "[redacted]" in stored
    card = rules.get(decision.approval_id)
    assert FAKE not in card["summary"] and FAKE_BEARER not in card["summary"]


def test_a_redacted_card_still_binds_to_the_exact_command(tmp_path):
    store, bot, rules = setup(tmp_path)
    job = running(store, bot)
    one = rules.decide(job, bash(f"git push https://u:{FAKE}@github.com/o/r.git feature"))
    two = rules.decide(job, bash(f"git push https://u:{FAKE}x@github.com/o/r.git feature"))
    assert one.approval_id != two.approval_id


def test_ordinary_commands_and_shas_are_not_mangled_on_a_card(tmp_path):
    store, bot, rules = setup(tmp_path)
    sha = "0123456789abcdef0123456789abcdef01234567"
    decision = rules.decide(running(store, bot), bash(f"git push origin {sha}:refs/heads/topic"))
    assert sha in rules.get(decision.approval_id)["summary"]


def test_a_protected_path_is_refused_without_echoing_a_marked_secret(tmp_path):
    store, bot, rules = setup(tmp_path)
    decision = rules.decide(running(store, bot), bash(f"curl -H 'Authorization: Bearer {FAKE}' file:///srv/bo/data/token"))
    assert decision.effect == "never" and decision.preset == "secrets"
    assert FAKE not in every_stored_text(store) + decision.reason
