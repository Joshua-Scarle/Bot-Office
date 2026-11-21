"""Device companion, server side: pairing, device tokens, session grants, prompt modes, audit."""
import asyncio
import json
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from botoffice import approval_classes, machines, needs_you
from botoffice.api import create_app
from botoffice.domain import JobStatus
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore, approval_card, now

H = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    machines._request_times.clear()


def make_store(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    return store, bot


def running(store, bot):
    job = store.create("work", "codex", bot_id=bot.id, source="chat")
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store.get(job.id)


def paired(store, active=True):
    code = machines.create_code(store, "laptop")["code"]
    reply = machines.redeem(store, code, "lap", "Linux 6", "0.1.0")
    if active:
        machines.activate(store, reply["machine_id"], reply["fingerprint"])
    return reply


def sql(store, query, *args):
    with closing(store._connect()) as db:
        return db.execute(query, args).fetchall()


# --- pairing ---

def test_pairing_code_good_once_then_used(tmp_path):
    store, _ = make_store(tmp_path)
    code = machines.create_code(store, "laptop")["code"]
    assert len(code) == 9 and code[4] == "-"
    reply = machines.redeem(store, code.lower().replace("-", " "), "lap", "Linux", "0.1.0")
    assert reply["status"] == "pending" and reply["token"].startswith("bom_" + reply["machine_id"])
    assert reply["name"] == "laptop"
    # the token is stored hashed only
    row = machines.get_machine(store, reply["machine_id"])
    assert row["token_hash"] == machines.token_hash(reply["token"]) and reply["token"] not in json.dumps(row)
    with pytest.raises(machines.MachineError) as err:
        machines.redeem(store, code, "lap", "Linux", "0.1.0")
    assert err.value.status == 403


def test_pairing_code_expires(tmp_path):
    store, _ = make_store(tmp_path)
    code = machines.create_code(store)["code"]
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_pairing_codes SET expires_at=?", ((now() - timedelta(seconds=1)).isoformat(),))
    with pytest.raises(machines.MachineError):
        machines.redeem(store, code, "lap", "Linux", "0.1.0")


def test_wrong_codes_lock_every_live_code(tmp_path):
    store, _ = make_store(tmp_path)
    code = machines.create_code(store)["code"]
    for _ in range(machines.CODE_MAX_FAILURES):
        with pytest.raises(machines.MachineError):
            machines.redeem(store, "ZZZZ-ZZZZ", "x", "x", "x")
    with pytest.raises(machines.MachineError):
        machines.redeem(store, code, "lap", "Linux", "0.1.0")  # locked out now, even the right one
    for junk in (None, 5, "", "ABC", "ABCD-EFGU", "x" * 5000):
        assert machines.normalize_code(junk) is None


def test_live_code_limit_and_pair_rate_limit(tmp_path):
    store, _ = make_store(tmp_path)
    for _ in range(machines.LIVE_CODES_MAX):
        machines.create_code(store)
    with pytest.raises(machines.MachineError):
        machines.create_code(store)
    limit = machines.RateLimit(3, 60.0)
    assert [limit.allow() for _ in range(4)] == [True, True, True, False]


# --- tokens, activation, rotation, revoke ---

def test_token_is_useless_until_activated_with_the_right_fingerprint(tmp_path):
    store, _ = make_store(tmp_path)
    reply = paired(store, active=False)
    assert machines.authenticate(store, reply["token"]) == (None, "not_activated")
    with pytest.raises(machines.MachineError):
        machines.activate(store, reply["machine_id"], "0000-0000-0000-0000")
    machines.activate(store, reply["machine_id"], reply["fingerprint"])
    machine, reason = machines.authenticate(store, reply["token"])
    assert reason == "ok" and machine["id"] == reply["machine_id"]
    # the agent computes the same fingerprint from its token
    assert machines.fingerprint(machines.token_hash(reply["token"])) == reply["fingerprint"]
    for bad in (None, "", "test-token", reply["token"] + "x", reply["token"][:-1] + ("A" if reply["token"][-1] != "A" else "B"), "bom_000000000000_" + "a" * 43):
        assert machines.authenticate(store, bad)[0] is None


def test_rotation_overlap_then_old_token_dies(tmp_path):
    store, _ = make_store(tmp_path)
    reply = paired(store)
    new = machines.rotate(store, reply["machine_id"])
    assert machines.authenticate(store, new)[1] == "ok"
    assert machines.authenticate(store, reply["token"])[1] == "ok"  # the 5-minute overlap
    with pytest.raises(machines.MachineError):
        machines.rotate(store, reply["machine_id"])  # not twice inside the overlap
    with closing(store._connect()) as db:
        db.execute("UPDATE machines SET prev_expires_at=?", ((now() - timedelta(seconds=1)).isoformat(),))
    assert machines.authenticate(store, reply["token"])[0] is None
    assert machines.authenticate(store, new)[1] == "ok"
    machine = machines.get_machine(store, reply["machine_id"])
    assert not machines.needs_rotation(machine)
    assert machines.needs_rotation({**machine, "token_issued_at": (now() - timedelta(days=31)).isoformat()})



LS = {"path": "/srv/work", "depth": 1}
READ = {"path": "/srv/work/a.txt", "offset": 0, "length": 10, "b64": False, "file_size": 10}
RUN = {"argv": ["make", "test"], "sh": None, "cwd": "/srv/work", "timeout": 30}
PUSH = {"path": "/srv/work/a.bin", "source": "a.bin", "size": 3, "sha256": "a" * 64, "overwrite": False,
        "requested": "~/a.bin", "exists": False}


def setup(tmp_path, mode=None):
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    if mode:
        machines.update_machine(store, machine["id"], mode=mode)
        machine = machines.get_machine(store, machine["id"])
    return store, bot, machine, running(store, bot)


def cards(store):
    return sql(store, "SELECT id, status FROM approvals WHERE source='machine' ORDER BY id")


def test_revoke_kills_tokens_grants_and_pending_requests(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    item = machines.record_request(store, job, machine, "ls", LS)
    machines.approve(store, item["approval_id"])
    assert len(machines.list_grants(store)) == 1
    other_job = running(store, bot)
    other = machines.record_request(store, other_job, machine, "read", READ)
    machines.revoke(store, machine["id"])
    assert machines.get_machine(store, machine["id"])["status"] == "revoked"
    assert machines.list_grants(store) == []
    assert machines.get_request(store, item["id"])["status"] == "cancelled"
    assert machines.get_request(store, other["id"])["status"] == "cancelled"
    assert machines.approval_dict(store, other["approval_id"])["status"] == "denied"
    with pytest.raises(machines.MachineError):
        machines.rotate(store, machine["id"])


# --- session grants (one card per task) ---

def test_one_card_per_task_then_calls_run_without_cards(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    first = machines.record_request(store, job, machine, "ls", LS)
    assert first["status"] == "pending" and first["approval_id"]
    # more calls while the card waits join the same card
    second = machines.record_request(store, job, machine, "read", READ)
    assert second["approval_id"] == first["approval_id"] and len(cards(store)) == 1
    card = approval_card(store, machines.approval_dict(store, first["approval_id"]))["machine"]
    assert card["scope"] == "read" and card["task"] == "chat turn: work" and card["cap_minutes"] == 240
    assert len(card["calls"]) == 2 and card["grant"] is None and not card["full_access"]
    machines.approve(store, first["approval_id"])
    assert {machines.get_request(store, r["id"])["status"] for r in (first, second)} == {"approved"}
    grant = machines.list_grants(store)[0]
    assert (grant["kind"], grant["scope"], grant["job_id"], grant["task"]) == ("task", "read", str(job.id),
                                                                             "chat turn: work")
    # inside the grant: no card, at once
    for _ in range(5):
        item = machines.record_request(store, job, machine, "ls", LS)
        assert item["status"] == "approved" and item["approval_id"] is None and item["grant_id"] == grant["id"]
    assert len(cards(store)) == 1
    claimed = machines.begin_execute(store, item["id"], job)
    assert claimed["grant"]["id"] == grant["id"] and claimed["args"] == LS
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, item["id"], job)  # each call row is used once
    # the grant is this job's: a new job (a new chat turn or task) asks again, another bot too
    again = machines.record_request(store, running(store, bot), machine, "ls", LS)
    assert again["status"] == "pending" and again["approval_id"] != first["approval_id"]
    other_bot = store.create_bot("Bo", "", "codex", None)
    assert machines.record_request(store, running(store, other_bot), machine, "ls", LS)["status"] == "pending"
    # and another job cannot run this job's call
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, machines.record_request(store, job, machine, "ls", LS)["id"], running(store, bot))


def test_a_wider_call_raises_one_widen_card(tmp_path):
    store, _, machine, job = setup(tmp_path)
    first = machines.record_request(store, job, machine, "read", READ)
    machines.approve(store, first["approval_id"])
    push = machines.record_request(store, job, machine, "push", PUSH)
    assert push["status"] == "pending"
    run = machines.record_request(store, job, machine, "run", RUN)
    # wider than the card on screen: that card is withdrawn and one new card asks for shell, so an
    # Allow on the "files" card can never grant shell
    assert run["approval_id"] != push["approval_id"]
    assert machines.approval_dict(store, push["approval_id"])["status"] == "expired"
    with pytest.raises(machines.MachineError):
        machines.approve(store, push["approval_id"])
    assert machines.get_request(store, push["id"])["approval_id"] == run["approval_id"]  # it waits on the new one
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM approvals WHERE source='machine' AND status='pending'").fetchone()[0] == 1
    card = approval_card(store, machines.approval_dict(store, run["approval_id"]))["machine"]
    assert card["scope"] == "shell" and card["widen_from"] == "read" and card["full_access"]
    assert "widen its access from read to shell" in machines.approval_dict(store, run["approval_id"])["summary"]
    # the read grant keeps working while the widen card waits; a call it covers keeps the card
    assert machines.record_request(store, job, machine, "ls", LS)["status"] == "approved"
    assert machines.record_request(store, job, machine, "push", PUSH)["approval_id"] == run["approval_id"]
    machines.approve(store, run["approval_id"])
    grants = machines.list_grants(store)
    assert len(grants) == 1 and grants[0]["scope"] == "shell"  # the same grant, widened; its end did not move
    assert machines.record_request(store, job, machine, "run", RUN)["status"] == "approved"
    assert machines.get_request(store, push["id"])["status"] == "approved"


def test_ending_a_grant_denies_its_waiting_widen_card(tmp_path):
    store, _, machine, job = setup(tmp_path)
    first = machines.record_request(store, job, machine, "read", READ)
    machines.approve(store, first["approval_id"])
    grant = machines.list_grants(store)[0]
    widen = machines.record_request(store, job, machine, "run", RUN)
    machines.end_grant(store, grant["id"])
    assert machines.approval_dict(store, widen["approval_id"])["status"] == "denied"
    assert machines.get_request(store, widen["id"])["status"] == "denied"
    with pytest.raises(machines.MachineError):  # a late Allow opens nothing
        machines.approve(store, widen["approval_id"])
    assert machines.list_grants(store) == []


def test_a_widen_card_only_widens_the_grant_it_was_raised_for(tmp_path):
    store, _, machine, job = setup(tmp_path)
    first = machines.record_request(store, job, machine, "read", READ)
    machines.approve(store, first["approval_id"])
    widen = machines.record_request(store, job, machine, "run", RUN)
    with closing(store._connect()) as db:  # the grant runs out without the sweep seeing it yet
        db.execute("UPDATE machine_grants SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(machines.MachineError):
        machines.approve(store, widen["approval_id"])
    assert machines.list_grants(store) == []


def test_request_scope_up_front(tmp_path):
    store, _, machine, job = setup(tmp_path)
    ask = machines.record_request(store, job, machine, "grant", {"scope": "files"}, "files")
    assert ask["status"] == "pending"
    machines.approve(store, ask["approval_id"])
    assert machines.get_request(store, ask["id"])["status"] == "done"
    assert machines.record_request(store, job, machine, "push", PUSH)["status"] == "approved"
    assert machines.record_request(store, job, machine, "grant", {"scope": "read"}, "read")["status"] == "done"
    assert machines.record_request(store, job, machine, "run", RUN)["status"] == "pending"
    assert machines.check_scope("shell") == "shell"
    with pytest.raises(machines.MachineError):
        machines.check_scope("root")


def test_grant_ends_with_the_job_at_the_cap_and_by_the_owner(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    machines.set_grant_cap(store, 30)
    first = machines.record_request(store, job, machine, "ls", LS)
    machines.approve(store, first["approval_id"])
    grant = machines.list_grants(store)[0]
    assert grant["expires_at"] < (now() + timedelta(minutes=31)).isoformat()
    assert machines.grant_live(store, grant["id"], str(job.id))
    queued = machines.record_request(store, job, machine, "ls", LS)
    # the cap
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_grants SET expires_at=?", ((now() - timedelta(seconds=1)).isoformat(),))
    assert not machines.grant_live(store, grant["id"], str(job.id))
    with pytest.raises(machines.MachineError) as err:
        machines.begin_execute(store, queued["id"], job)
    assert err.value.status == 403
    assert machines.sweep(store) == [grant["id"]]
    assert machines.get_grant(store, grant["id"])["end_reason"] == "expired"
    assert machines.get_request(store, queued["id"])["status"] == "cancelled"
    assert machines.record_request(store, job, machine, "ls", LS)["status"] == "pending"  # asks again
    # the owner's End grant
    job2 = running(store, bot)
    item = machines.record_request(store, job2, machine, "ls", LS)
    machines.approve(store, item["approval_id"])
    live = [g for g in machines.list_grants(store) if g["job_id"] == str(job2.id)][0]
    machines.end_grant(store, live["id"])
    assert not machines.grant_live(store, live["id"], str(job2.id))
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, item["id"], job2)
    assert machines.record_request(store, job2, machine, "ls", LS)["status"] == "pending"
    # the job's end
    job3 = running(store, bot)
    item = machines.record_request(store, job3, machine, "ls", LS)
    machines.approve(store, item["approval_id"])
    store.finish(job3.id, JobStatus.SUCCEEDED)
    ended = machines.sweep(store)
    assert len(ended) == 1 and machines.get_grant(store, ended[0])["end_reason"] == "task ended"
    with pytest.raises(machines.MachineError):
        machines.set_grant_cap(store, 0)
    with pytest.raises(machines.MachineError):
        machines.set_grant_cap(store, 24 * 60 + 1)
    assert machines.grant_cap_minutes(store) == 30


def test_prompt_modes(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    assert machines.PROMPT_MODE_DEFAULT == "ask" and machine["prompt_mode"] in (None, "ask")
    assert machines.REMEMBER_TTL == timedelta(days=30) and machines.GRANT_CAP_DEFAULT_MIN == 240
    # remember for this bot: one card, then the next tasks of this bot start without one
    machines.update_machine(store, machine["id"], mode="remember")
    machine = machines.get_machine(store, machine["id"])
    first = machines.record_request(store, job, machine, "read", READ)
    machines.approve(store, first["approval_id"])
    remembered = [g for g in machines.list_grants(store) if g["kind"] == "remember"]
    assert len(remembered) == 1 and remembered[0]["scope"] == "read"
    assert remembered[0]["expires_at"] > (now() + timedelta(days=29)).isoformat()
    job2 = running(store, bot)
    assert machines.record_request(store, job2, machine, "ls", LS)["status"] == "approved"
    assert machines.record_request(store, job2, machine, "run", RUN)["status"] == "pending"  # wider: a card
    other_bot = store.create_bot("Bo", "", "codex", None)
    assert machines.record_request(store, running(store, other_bot), machine, "ls", LS)["status"] == "pending"
    # back to ask: remembered grants end
    machines.update_machine(store, machine["id"], mode="ask")
    machine = machines.get_machine(store, machine["id"])
    assert not [g for g in machines.list_grants(store) if g["kind"] == "remember"]
    assert machines.record_request(store, running(store, bot), machine, "ls", LS)["status"] == "pending"
    # always allow: no card at all, still a task grant with the cap, still in the audit
    machines.update_machine(store, machine["id"], mode="allow")
    machine = machines.get_machine(store, machine["id"])
    job4 = running(store, bot)
    item = machines.record_request(store, job4, machine, "run", RUN)
    assert item["status"] == "approved"
    grant = machines.get_grant(store, item["grant_id"])
    assert grant["kind"] == "task" and grant["source"] == "always allow" and grant["scope"] == "shell"
    # End grant by the owner wins over always allow for the rest of that task
    machines.end_grant(store, grant["id"])
    assert machines.record_request(store, job4, machine, "ls", LS)["status"] == "pending"
    assert {"grant_started", "allowed"} <= {row["event"] for row in machines.audit_log(store, job_id=str(job4.id))}
    with pytest.raises(machines.MachineError):
        machines.update_machine(store, machine["id"], mode="sometimes")


def test_deny_timeout_and_ended_job_are_denials(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    denied = machines.record_request(store, job, machine, "ls", LS)
    machines.deny(store, denied["approval_id"])
    assert machines.get_request(store, denied["id"])["status"] == "denied"
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, denied["id"], job)
    late = machines.record_request(store, job, machine, "ls", LS)
    with closing(store._connect()) as db:
        db.execute("UPDATE approvals SET expires_at=? WHERE id=?", ((now() - timedelta(seconds=1)).isoformat(),
                                                                   late["approval_id"]))
    assert machines.expire(store) == 1
    assert machines.get_request(store, late["id"])["status"] == "expired"
    assert machines.approval_dict(store, late["approval_id"])["status"] == "expired"
    with pytest.raises(machines.MachineError):
        machines.approve(store, late["approval_id"])
    # an expired machine approval offers no Re-run in Needs-you
    assert all(late["approval_id"] not in (item.get("approval_id"), item.get("id")) for item in needs_you.needs_you(store))
    ended = machines.record_request(store, job, machine, "ls", LS)
    store.finish(job.id, JobStatus.SUCCEEDED)
    with pytest.raises(machines.MachineError):
        machines.approve(store, ended["approval_id"])
    with pytest.raises(machines.MachineError):
        machines.record_request(store, job, machine, "ls", LS)  # the job is not running


def test_stop_all_ends_grants_denies_cards_and_refuses(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    item = machines.record_request(store, job, machine, "ls", LS)
    machines.approve(store, item["approval_id"])
    waiting = machines.record_request(store, running(store, bot), machine, "ls", LS)
    grant_id = machines.list_grants(store)[0]["id"]
    assert machines.stop_all(store) == [grant_id]
    assert not machines.class_enabled(store)
    assert machines.get_grant(store, grant_id)["end_reason"] == "computer access stopped"
    assert machines.approval_dict(store, waiting["approval_id"])["status"] == "denied"
    assert machines.get_request(store, item["id"])["status"] == "cancelled"
    with pytest.raises(machines.MachineError) as err:
        machines.begin_execute(store, item["id"], job)
    assert err.value.status == 403
    # the gate never matches the computer class: it only covers bin/machine requests
    assert approval_classes.match(store, "Bash", "machine ls /") is None
    machines.resume_all(store)
    assert machines.class_enabled(store)
    assert machines.record_request(store, job, machine, "ls", LS)["status"] == "pending"


def test_audit_has_every_call_and_no_contents(tmp_path):
    store, bot, machine, job = setup(tmp_path)
    item = machines.record_request(store, job, machine, "ls", LS)
    machines.approve(store, item["approval_id"])
    run = machines.record_request(store, job, machine, "grant", {"scope": "shell"}, "shell")
    machines.approve(store, run["approval_id"])
    call = machines.record_request(store, job, machine, "run", RUN)
    machines.begin_execute(store, call["id"], job)
    machines.finish(store, call["id"], ok=True, result={"exit": 0, "stdout": "SECRET-OUTPUT", "stderr": "",
                                                         "sha256": "f" * 64})
    blocked = machines.record_request(store, job, machine, "run", {**RUN, "argv": ["rm", "-rf", "/"]})
    machines.begin_execute(store, blocked["id"], job)
    machines.finish(store, blocked["id"], ok=False, error="refused by this computer's command deny-list", blocked=True)
    assert machines.get_request(store, blocked["id"])["status"] == "blocked"
    machines.refused(store, job, machine, "read", {**READ, "path": "~/.ssh/id_ed25519"}, blocked=True,
                     why="on this computer's deny-list")
    log = machines.audit_log(store, machine_id=machine["id"], bot_id=str(bot.id), job_id=str(job.id))
    events = [row["event"] for row in log]
    assert {"card", "grant_allowed", "grant_widened", "allowed", "done", "blocked"} <= set(events)
    done = next(row for row in log if row["event"] == "done")
    detail = json.loads(done["detail"])
    assert detail["argv"] == ["make", "test"] and detail["exit"] == 0 and detail["stdout_bytes"] == 13
    assert done["grant_id"] and done["bot_name"] == "Ada" and done["machine_name"] == "laptop"
    assert "SECRET-OUTPUT" not in json.dumps(log) and "SECRET-OUTPUT" not in json.dumps(
        sql(store, "SELECT result_json FROM machine_requests")[0][0])
    assert sum(1 for row in log if row["event"] == "blocked") == 2
    assert machines.audit_log(store, job_id="nope") == []


def test_argument_validation_and_limits():
    with pytest.raises(machines.MachineError):
        machines.validate_args("rm", {})
    assert machines.validate_args("run", {"argv": ["ls"]}) == {"argv": ["ls"], "sh": None, "cwd": "~", "timeout": 30}
    for tool, bad in (("ls", {"path": "/", "depth": 4}), ("read", {"path": "/x", "length": 2 * 1024 * 1024}),
                      ("run", {"argv": ["ls"], "sh": "ls"}), ("run", {"argv": ["ls"], "timeout": 301}),
                      ("run", {"argv": []}), ("run", {"sh": "x" * 5000}), ("ls", {"path": "a\nb"}),
                      ("push", {"path": "/x", "source": "a", "size": 1, "sha256": "XYZ"}),
                      ("push", {"path": "/x", "source": "a", "size": 101 * 1024 * 1024, "sha256": "a" * 64}),
                      ("read", {"path": "/x", "b64": "yes"})):
        with pytest.raises(machines.MachineError):
            machines.validate_args(tool, bad)
    with pytest.raises(machines.MachineError):
        machines.canonical_args("read", {"path": "~/big", "offset": 0, "length": 1, "b64": False},
                                {"canonical": "/srv/big", "size": 17 * 1024 * 1024})
    with pytest.raises(machines.MachineError) as err:
        machines.canonical_args("push", {"path": "~/a", "overwrite": False}, {"canonical": "/srv/a", "exists": True})
    assert err.value.status == 409
    with pytest.raises(machines.MachineError):
        machines.canonical_args("ls", {"path": "x", "depth": 1}, {"canonical": "relative"})
    assert [machines.TOOL_SCOPE[t] for t in ("ls", "read", "pull", "shot", "push", "run")] == \
        ["read", "read", "read", "read", "files", "shell"]
    assert machines.scope_covers("shell", "read") and not machines.scope_covers("read", "files")
    assert not machines.scope_covers(None, "read") and machines.wider("files", "read") == "files"


def test_no_pattern_grants_left():
    for gone in ("approve_always", "grant_matches", "check_pattern", "run_pattern", "binding_hash", "revoke_grant"):
        assert not hasattr(machines, gone)


def test_device_flags_default_off(tmp_path):
    store, _ = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    assert machines.DEVICE_FLAG_DEFAULTS == {"sandbox": False, "confirm_local": False}
    assert machines.device_flags(machine) == {"sandbox": False, "confirm_local": False}
    machines.update_machine(store, machine["id"], sandbox=True)
    assert machines.device_flags(machines.get_machine(store, machine["id"]))["sandbox"] is True


# --- API ---

def test_owner_api_and_agent_token_scope(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        assert client.get("/v1/machines").status_code == 401
        code = client.post("/v1/machines/pairing-codes", headers=H, json={"name": "laptop"}).json()["code"]
        reply = client.post("/v1/machines/pair", json={"code": code, "hostname": "lap", "os": "Linux", "version": "1"}).json()
        listed = client.get("/v1/machines", headers=H).json()
        shown = listed["machines"][0]
        assert shown["status"] == "pending" and shown["fingerprint"] == reply["fingerprint"]
        assert shown["prompt_mode"] == "ask"
        assert "token" not in json.dumps(listed).replace("token_issued_at", "")
        assert listed["defaults"]["sandbox"] is False and listed["defaults"]["confirm_local"] is False
        assert listed["defaults"]["prompt_mode"] == "ask" and listed["grant_cap_min"] == 240
        assert listed["computer_access"] is True
        # a device token is not a master token
        assert client.get("/v1/machines", headers={"Authorization": "Bearer " + reply["token"]}).status_code == 401
        assert client.post(f"/v1/machines/{reply['machine_id']}/activate", headers=H,
                           json={"fingerprint": "nope"}).status_code == 409
        assert client.post(f"/v1/machines/{reply['machine_id']}/activate", headers=H,
                           json={"fingerprint": reply["fingerprint"]}).status_code == 200
        assert client.patch(f"/v1/machines/{reply['machine_id']}", headers=H,
                            json={"sandbox": True}).json()["flags"]["sandbox"] is True
        assert client.patch(f"/v1/machines/{reply['machine_id']}", headers=H,
                            json={"prompt_mode": "remember"}).json()["prompt_mode"] == "remember"
        assert client.patch(f"/v1/machines/{reply['machine_id']}", headers=H,
                            json={"prompt_mode": "yolo"}).status_code in (400, 409, 422)
        assert client.patch("/v1/machine-settings", headers=H, json={"grant_cap_min": 60}).status_code == 200
        assert client.patch("/v1/machine-settings", headers=H, json={"grant_cap_min": 0}).status_code in (400, 422)
        bot = store.create_bot("Ada", "", "codex", None)
        job = running(store, bot)
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        assert client.get("/v1/agent/machines", headers=agent).json()[0]["name"] == "laptop"
        assert client.get("/v1/machines", headers=agent).status_code == 401
        assert client.get("/v1/machines/audit", headers=agent).status_code == 401
        assert client.post("/v1/machines/stop-all", headers=agent).status_code == 401
        # offline: refused before any approval is asked
        answer = client.post("/v1/agent/machines/requests", headers=agent,
                             json={"tool": "ls", "args": {"path": "/srv"}})
        assert answer.status_code == 409 and sql(store, "SELECT COUNT(*) FROM approvals")[0][0] == 0
        assert client.post("/v1/agent/machines/requests", headers=agent,
                           json={"tool": "grant", "scope": "shell"}).status_code == 409
        assert client.post("/v1/agent/machines/requests", headers=agent,
                           json={"tool": "grant", "scope": "root"}).status_code == 400
        assert client.post("/v1/agent/machines/requests", headers=agent,
                           json={"tool": "nope", "args": {}}).status_code == 400
        audit = client.get(f"/v1/machines/{reply['machine_id']}/audit", headers=H).json()
        assert {"paired", "activated"} <= {item["event"] for item in audit}
        everything = client.get("/v1/machines/audit", headers=H, params={"machine_id": reply["machine_id"]}).json()
        assert {"paired", "activated"} <= {item["event"] for item in everything}
        assert client.post(f"/v1/machines/{reply['machine_id']}/revoke", headers=H).json()["status"] == "revoked"
        assert client.get("/v1/agent/machines", headers=agent).json() == []


def test_machine_card_end_grant_and_stop_all_routes(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        machine = machines.get_machine(store, paired(store)["machine_id"])
        bot = store.create_bot("Ada", "", "codex", None)
        job = running(store, bot)
        item = machines.record_request(store, job, machine, "run", RUN)
        card = approval_card(store, machines.approval_dict(store, item["approval_id"]))
        assert card["machine"]["scope"] == "shell" and card["machine"]["full_access"]
        assert card["machine"]["calls"] == [item["summary"]]
        assert "can_always" not in card["machine"]
        assert client.post(f"/v1/approvals/{item['approval_id']}/approve", headers=H).status_code == 200
        assert machines.get_request(store, item["id"])["status"] == "approved"
        # approving twice is refused, and no resume turn was queued for a machine approval
        assert client.post(f"/v1/approvals/{item['approval_id']}/approve", headers=H).status_code == 409
        card = approval_card(store, machines.approval_dict(store, item["approval_id"]))
        assert card["machine"]["grant"]["live"] is True
        grants = client.get("/v1/machine-grants", headers=H).json()
        assert grants[0]["scope"] == "shell" and grants[0]["task"] == "chat turn: work"
        assert client.post(f"/v1/machine-grants/{grants[0]['id']}/end", headers=H).status_code == 200
        assert client.get("/v1/machine-grants", headers=H).json() == []
        assert client.post("/v1/machine-grants/99999/end", headers=H).status_code == 404
        assert client.post(f"/v1/machines/approvals/{item['approval_id']}/always", headers=H,
                           json={"pattern": "/srv/**"}).status_code in (404, 405)
        assert client.post("/v1/machines/stop-all", headers=H).status_code == 200
        assert client.get("/v1/machines", headers=H).json()["computer_access"] is False
        assert client.post("/v1/machines/resume-all", headers=H).status_code == 200
        assert client.get("/v1/machines", headers=H).json()["computer_access"] is True

def test_hub_sends_a_push_only_as_far_as_the_agent_granted():
    import asyncio
    import io
    from botoffice import machine_hub

    class Conn:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message["type"])

    async def scenario():
        loop = asyncio.get_running_loop()
        call, conn = machine_hub._Call(loop.create_future()), Conn()
        sender = loop.create_task(machine_hub.Hub._send_upload(
            conn, "c1", call, io.BytesIO(b"a" * (machine_hub.CHUNK * 3)), loop.time() + 5))
        await asyncio.sleep(0.1)
        assert conn.sent == []  # nothing before the first credit
        call.credits, _ = 2, call.wake.set()
        await asyncio.sleep(0.1)
        assert conn.sent == ["chunk", "chunk"]
        call.credits, _ = 5, call.wake.set()
        await asyncio.wait_for(sender, 1)
        assert conn.sent == ["chunk", "chunk", "chunk", "chunk_end"]
        stalled = machine_hub._Call(loop.create_future())
        with pytest.raises(asyncio.TimeoutError):
            await machine_hub.Hub._send_upload(conn, "c2", stalled, io.BytesIO(b"x"), loop.time() + 0.2)

    asyncio.run(scenario())


def test_hub_push_deadline_covers_a_stuck_socket():
    """A send that never completes (no room on the socket) still ends at the deadline with a cancel."""
    import asyncio
    import io
    from botoffice import machine_hub

    class Stuck:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(json.loads(text)["type"])
            if self.sent[-1] == "chunk":
                await asyncio.Event().wait()

    async def scenario():
        hub = machine_hub.Hub.__new__(machine_hub.Hub)
        conn = machine_hub._Connection(Stuck(), "m1")
        hub.connections = {"m1": conn}

        async def grant():
            while not conn.calls:
                await asyncio.sleep(0.01)
            call = next(iter(conn.calls.values()))
            call.credits, _ = 4, call.wake.set()

        asyncio.get_running_loop().create_task(grant())
        started = asyncio.get_running_loop().time()
        with pytest.raises(machine_hub.HubError) as caught:
            await hub.call("m1", "push", {}, timeout=0.5, upload=io.BytesIO(b"a" * 10))
        assert caught.value.status == 504 and asyncio.get_running_loop().time() - started < 2
        assert conn.ws.sent == ["call", "chunk", "cancel"] and conn.calls == {}

    asyncio.run(scenario())


def test_hub_refuses_an_agent_without_session_grants(tmp_path):
    from botoffice.machine_hub import CLOSE_OUTDATED, Hub
    store, _, machine, _ = setup(tmp_path)

    class FakeWS:
        headers = {}
        closed_with = None
        sent = []

        def __init__(self, hello):
            self.hello = hello

        async def accept(self):
            pass

        async def receive_text(self):
            return json.dumps(self.hello)

        async def close(self, code=1000, reason=""):
            self.closed_with = code

        async def send_text(self, text):
            self.sent.append(text)

    token = machines.rotate(store, machine["id"])
    hub = Hub(store)
    old = FakeWS({"type": "hello", "version": "0.1.0"})
    old.headers = {"authorization": f"Bearer {token}"}
    asyncio.run(hub.serve(old))
    assert old.closed_with == CLOSE_OUTDATED and not hub.is_online(machine["id"])
    events = [item["event"] for item in machines.audit_log(store, machine_id=machine["id"])]
    assert "outdated_agent" in events and "connected" not in events


def test_hub_sends_nothing_under_a_dead_grant():
    from botoffice.machine_hub import Hub, HubError, _Connection

    class WS:
        sent = []

        async def send_text(self, text):
            self.sent.append(text)

    hub = Hub(None)
    ws = WS()
    hub.connections["m"] = _Connection(ws, "m")
    with pytest.raises(HubError) as caught:
        asyncio.run(hub.call("m", "read", {}, timeout=5, grant_id=1, alive=lambda: False))
    assert caught.value.status == 403 and ws.sent == []
