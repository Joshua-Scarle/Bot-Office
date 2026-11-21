"""Device companion, server side: pairing, device tokens, grants and the approval binding."""
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


def test_revoke_kills_tokens_grants_and_pending_requests(tmp_path):
    store, bot = make_store(tmp_path)
    reply = paired(store)
    machine = machines.get_machine(store, reply["machine_id"])
    job = running(store, bot)
    item = machines.record_request(store, job, machine, "ls", {"path": "/srv/work", "depth": 1})
    machines.approve_always(store, item["approval_id"], "/srv/work/**", confirm_broad=True)
    other = machines.record_request(store, job, machine, "read",
                                    {"path": "/etc/hosts", "offset": 0, "length": 10, "b64": False, "file_size": 10})
    machines.revoke(store, reply["machine_id"])
    assert machines.authenticate(store, reply["token"])[0] is None
    assert machines.list_grants(store) == []
    assert machines.get_request(store, other["id"])["status"] == "cancelled"
    assert machines.approval_dict(store, other["approval_id"])["status"] == "denied"
    with pytest.raises(machines.MachineError):
        machines.rotate(store, reply["machine_id"])


# --- grants ---

@pytest.mark.parametrize("pattern,path,hit", [
    ("/srv/work/**", "/srv/work", True),
    ("/srv/work/**", "/srv/work/a/b/c.txt", True),
    ("/srv/work/**", "/srv/workshop/x", False),
    ("/srv/work/*.txt", "/srv/work/a.txt", True),
    ("/srv/work/*.txt", "/srv/work/sub/a.txt", False),
    ("/srv/work/a?.log", "/srv/work/ab.log", True),
    ("/srv/work/a?.log", "/srv/work/a/.log", False),
    ("/srv/work/a.txt", "/srv/work/a.txt", True),
    ("/srv/work/a.txt", "/srv/work/aXtxt", False),
    ("/srv/work/[ab].txt", "/srv/work/a.txt", False),  # brackets are literal
])
def test_glob_grants(pattern, path, hit):
    assert machines.grant_matches("read", pattern, {"path": path}) is hit


def test_grant_patterns_are_checked():
    for bad in ("relative/*", "/*", "**", "/srv/../etc/**", "/srv//x", "/srv/x/", 5, "/a\nb"):
        with pytest.raises(machines.MachineError):
            machines.check_pattern(bad)
    with pytest.raises(machines.MachineError) as err:
        machines.check_pattern("/home/**")
    assert err.value.status == 428
    assert machines.check_pattern("/srv/**", confirm_broad=True) == "/srv/**"
    assert machines.check_pattern("/srv/work/proj/**") == "/srv/work/proj/**"


def test_push_grant_is_a_destination_folder_and_never_overwrites():
    args = {"path": "/srv/work/out/a.bin", "overwrite": False}
    assert machines.grant_matches("push", "/srv/work/out/**", args)
    assert not machines.grant_matches("push", "/srv/work/out/**", {**args, "overwrite": True})
    assert not machines.grant_matches("push", "/srv/work/other/**", args)


def test_run_grant_is_the_exact_argv_and_cwd():
    args = {"argv": ["make", "test"], "sh": None, "cwd": "/srv/work"}
    pattern = machines.run_pattern(args)
    assert machines.grant_matches("run", pattern, args)
    assert not machines.grant_matches("run", pattern, {**args, "argv": ["make", "test", "-j8"]})
    assert not machines.grant_matches("run", pattern, {**args, "cwd": "/srv"})
    assert not machines.grant_matches("run", pattern, {"argv": None, "sh": "make test", "cwd": "/srv/work"})
    for unsafe in (["bash", "-c", "x"], ["/usr/bin/env", "rm"], ["sudo", "ls"], ["python3", "-c", "1"],
                   ["make", "a;b"], ["ls", "$(id)"], ["ls", "*"]):
        with pytest.raises(machines.MachineError):
            machines.run_pattern({"argv": unsafe, "cwd": "/srv"})
    with pytest.raises(machines.MachineError):
        machines.run_pattern({"argv": None, "sh": "ls", "cwd": "/srv"})


def test_grant_skips_the_card_then_expires_and_revokes(tmp_path):
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    job = running(store, bot)
    first = machines.record_request(store, job, machine, "ls", {"path": "/srv/work/a", "depth": 1})
    assert first["status"] == "pending" and first["approval_id"]
    machines.approve_always(store, first["approval_id"], "/srv/work/**", confirm_broad=True)
    second = machines.record_request(store, job, machine, "ls", {"path": "/srv/work/b", "depth": 1})
    assert second["status"] == "approved" and second["grant_id"] and second["approval_id"] is None
    outside = machines.record_request(store, job, machine, "ls", {"path": "/srv/other", "depth": 1})
    assert outside["status"] == "pending"
    # another bot does not inherit the grant
    other_bot = store.create_bot("Bo", "", "codex", None)
    assert machines.record_request(store, running(store, other_bot), machine, "ls",
                                   {"path": "/srv/work/b", "depth": 1})["status"] == "pending"
    grant = machines.list_grants(store)[0]
    assert grant["expires_at"] > (now() + timedelta(days=29)).isoformat()
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_grants SET expires_at=?", ((now() - timedelta(seconds=1)).isoformat(),))
    assert machines.record_request(store, job, machine, "ls", {"path": "/srv/work/c", "depth": 1})["status"] == "pending"
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, second["id"], job)  # its grant is gone now
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_grants SET expires_at=?", ((now() + timedelta(days=1)).isoformat(),))
    machines.revoke_grant(store, grant["id"])
    assert machines.record_request(store, job, machine, "ls", {"path": "/srv/work/d", "depth": 1})["status"] == "pending"


# --- binding ---

def test_approval_is_single_use_and_bound_to_the_arguments(tmp_path):
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    job = running(store, bot)
    args = {"path": "/srv/work/a.txt", "offset": 0, "length": 100, "b64": False, "file_size": 100}
    item = machines.record_request(store, job, machine, "read", args)
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, item["id"], job)  # not approved yet
    machines.approve(store, item["approval_id"])
    # the stored arguments changed after the approval (a tampered row): refused
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_requests SET args_json=? WHERE id=?",
                   (json.dumps({**args, "path": "/srv/work/b.txt"}, sort_keys=True), item["id"]))
    with pytest.raises(machines.MachineError) as err:
        machines.begin_execute(store, item["id"], job)
    assert err.value.status == 403
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_requests SET args_json=? WHERE id=?", (json.dumps(args, sort_keys=True), item["id"]))
    claimed = machines.begin_execute(store, item["id"], job)
    assert claimed["args"]["path"] == "/srv/work/a.txt"
    assert machines.approval_dict(store, item["approval_id"])["status"] == "consumed"
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, item["id"], job)  # single use
    # another job of the same bot cannot use it either
    other = machines.record_request(store, job, machine, "read", args)
    machines.approve(store, other["approval_id"])
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, other["id"], running(store, bot))


def test_push_binding_includes_the_sha256(tmp_path):
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    job = running(store, bot)
    args = {"path": "/srv/work/a.bin", "source": "a.bin", "size": 3, "sha256": "a" * 64, "overwrite": False,
            "requested": "~/a.bin", "exists": False}
    item = machines.record_request(store, job, machine, "push", args)
    base = machines.binding_hash(machine["id"], str(bot.id), str(job.id), "push", args)
    assert item["binding_hash"] == base
    assert machines.binding_hash(machine["id"], str(bot.id), str(job.id), "push", {**args, "sha256": "b" * 64}) != base
    machines.approve(store, item["approval_id"])
    machines.fail_unexecuted(store, item["id"], job, "the file differs from the approved sha256")
    assert machines.get_request(store, item["id"])["status"] == "failed"
    assert machines.approval_dict(store, item["approval_id"])["status"] == "consumed"
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, item["id"], job)


def test_deny_timeout_and_ended_job_are_denials(tmp_path):
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    job = running(store, bot)
    args = {"path": "/srv/work", "depth": 1}
    denied = machines.record_request(store, job, machine, "ls", args)
    machines.deny(store, denied["approval_id"])
    with pytest.raises(machines.MachineError):
        machines.begin_execute(store, denied["id"], job)
    late = machines.record_request(store, job, machine, "ls", args)
    with closing(store._connect()) as db:
        db.execute("UPDATE machine_requests SET expires_at=? WHERE id=?", ((now() - timedelta(seconds=1)).isoformat(), late["id"]))
    assert machines.expire(store) == 1
    assert machines.get_request(store, late["id"])["status"] == "expired"
    assert machines.approval_dict(store, late["approval_id"])["status"] == "expired"
    with pytest.raises(machines.MachineError):
        machines.approve(store, late["approval_id"])
    # an expired machine approval offers no Re-run in Needs-you
    assert all(late["approval_id"] not in (item.get("approval_id"), item.get("id")) for item in needs_you.needs_you(store))
    cancelled = machines.record_request(store, job, machine, "ls", args)
    machines.cancel(store, cancelled["id"], job)
    with pytest.raises(machines.MachineError):
        machines.approve(store, cancelled["approval_id"])
    ended = machines.record_request(store, job, machine, "ls", args)
    store.finish(job.id, JobStatus.SUCCEEDED)
    with pytest.raises(machines.MachineError):
        machines.approve(store, ended["approval_id"])


def test_class_off_refuses_everything(tmp_path):
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    job = running(store, bot)
    assert machines.class_enabled(store)  # "Controlling a computer" is on by default
    item = machines.record_request(store, job, machine, "ls", {"path": "/srv/work", "depth": 1})
    machines.approve(store, item["approval_id"])
    approval_classes.update(store, enabled={machines.CLASS_KEY: False})
    assert not machines.class_enabled(store)
    with pytest.raises(machines.MachineError) as err:
        machines.begin_execute(store, item["id"], job)
    assert err.value.status == 403
    # the gate never matches the computer class: it only covers bin/machine requests
    assert approval_classes.match(store, "Bash", "machine ls /") is None


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
        assert "token" not in json.dumps(listed).replace("token_issued_at", "")
        assert listed["defaults"] == {"sandbox": False, "confirm_local": False}
        # a device token is not a master token
        assert client.get("/v1/machines", headers={"Authorization": "Bearer " + reply["token"]}).status_code == 401
        assert client.post(f"/v1/machines/{reply['machine_id']}/activate", headers=H,
                           json={"fingerprint": "nope"}).status_code == 409
        assert client.post(f"/v1/machines/{reply['machine_id']}/activate", headers=H,
                           json={"fingerprint": reply["fingerprint"]}).status_code == 200
        assert client.patch(f"/v1/machines/{reply['machine_id']}", headers=H,
                            json={"sandbox": True}).json()["flags"]["sandbox"] is True
        bot = store.create_bot("Ada", "", "codex", None)
        job = running(store, bot)
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        assert client.get("/v1/agent/machines", headers=agent).json()[0]["name"] == "laptop"
        assert client.get("/v1/machines", headers=agent).status_code == 401
        # offline: refused before any approval is asked
        answer = client.post("/v1/agent/machines/requests", headers=agent,
                             json={"tool": "ls", "args": {"path": "/srv"}})
        assert answer.status_code == 409 and sql(store, "SELECT COUNT(*) FROM approvals")[0][0] == 0
        assert client.post("/v1/agent/machines/requests", headers=agent,
                           json={"tool": "nope", "args": {}}).status_code == 400
        audit = client.get(f"/v1/machines/{reply['machine_id']}/audit", headers=H).json()
        assert {"paired", "activated"} <= {item["event"] for item in audit}
        assert client.post(f"/v1/machines/{reply['machine_id']}/revoke", headers=H).json()["status"] == "revoked"
        assert client.get("/v1/agent/machines", headers=agent).json() == []


def test_machine_approval_card_and_approve_route(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        machine = machines.get_machine(store, paired(store)["machine_id"])
        bot = store.create_bot("Ada", "", "codex", None)
        job = running(store, bot)
        item = machines.record_request(store, job, machine, "run",
                                       {"argv": ["make", "test"], "sh": None, "cwd": "/srv/work", "timeout": 30})
        card = approval_card(store, machines.approval_dict(store, item["approval_id"]))
        assert card["machine"]["tool"] == "run" and card["machine"]["args"]["argv"] == ["make", "test"]
        assert card["machine"]["full_access"] and card["machine"]["can_always"]
        assert client.post(f"/v1/approvals/{item['approval_id']}/approve", headers=H).status_code == 200
        assert machines.get_request(store, item["id"])["status"] == "approved"
        # approving twice is refused, and no resume turn was queued for a machine approval
        assert client.post(f"/v1/approvals/{item['approval_id']}/approve", headers=H).status_code == 409
        broad = machines.record_request(store, job, machine, "read",
                                        {"path": "/srv/a.txt", "offset": 0, "length": 1, "b64": False, "file_size": 1})
        answer = client.post(f"/v1/machines/approvals/{broad['approval_id']}/always", headers=H,
                             json={"pattern": "/srv/**"})
        assert answer.status_code == 428
        assert client.post(f"/v1/machines/approvals/{broad['approval_id']}/always", headers=H,
                           json={"pattern": "/srv/**", "confirm_broad": True}).status_code == 200
        assert client.get("/v1/machine-grants", headers=H).json()[0]["pattern"] == "/srv/**"


def test_card_sizes_and_no_screenshot_grants(tmp_path):
    assert machines._size(500) == "500 bytes"
    assert machines._size(2048) == "2.0 KiB"
    assert machines._size(100 * 1024 * 1024) == "100.0 MiB"
    store, bot = make_store(tmp_path)
    machine = machines.get_machine(store, paired(store)["machine_id"])
    job = running(store, bot)
    item = machines.record_request(store, job, machine, "shot", {"dest": "shot.png"})
    assert approval_card(store, machines.approval_dict(store, item["approval_id"]))["machine"]["can_always"] is False
    with pytest.raises(machines.MachineError):
        machines.approve_always(store, item["approval_id"])
    assert not machines.grant_matches("shot", "screen", {"dest": "x.png"})


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
