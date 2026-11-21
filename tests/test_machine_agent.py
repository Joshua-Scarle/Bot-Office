"""Device companion, the computer's side (agent/): paths, the deny-lists, caps, flags, confirm, End grant."""
import asyncio
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "src"))

from botoffice_agent import cli, config, denycmd, ops  # noqa: E402
from botoffice_agent.paths import LocalDenied, PathChanged, deny_roots, open_verified  # noqa: E402


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "h"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh" / "id_ed25519").write_text("private")
    (root / "work" / "sub").mkdir(parents=True)
    (root / "work" / "a.txt").write_text("hello")
    return root


def ctx_for(home, extra=()):
    roots = deny_roots([*config.DEFAULT_DENY, *extra], str(home))
    return ops.Context(home=str(home), roots=roots)


def canonical(path):
    return os.path.realpath(path)


# --- paths ---

def test_dotdot_resolves_to_the_canonical_path_shown_on_the_card(home):
    ctx = ctx_for(home)
    found = ops.op_resolve({"path": "~/work/sub/../a.txt", "kind": "file"}, ctx)
    assert found["canonical"] == canonical(home / "work" / "a.txt") and found["size"] == 5
    assert ops.op_resolve({"path": "work", "kind": "dir"}, ctx)["canonical"] == canonical(home / "work")
    with pytest.raises(LocalDenied):
        ops.op_resolve({"path": "~/work/../.ssh/id_ed25519", "kind": "file"}, ctx)
    with pytest.raises(NotADirectoryError):
        ops.op_resolve({"path": "~/work/a.txt", "kind": "dir"}, ctx)
    with pytest.raises(FileNotFoundError):
        ops.op_resolve({"path": "~/work/missing", "kind": "file"}, ctx)


def test_symlink_into_ssh_is_denied(home):
    ctx = ctx_for(home)
    (home / "work" / "keys").symlink_to(home / ".ssh")
    (home / "work" / "innocent.txt").symlink_to(home / ".ssh" / "id_ed25519")
    for spelled in ("~/work/keys/id_ed25519", "~/work/innocent.txt"):
        with pytest.raises(LocalDenied):
            ops.op_resolve({"path": spelled, "kind": "file"}, ctx)
    with pytest.raises(LocalDenied):
        ops.op_resolve({"path": "~/work/keys", "kind": "dir"}, ctx)
    # at execution the deny-list is checked again, whatever was approved
    with pytest.raises(LocalDenied):
        ops.op_read({"path": canonical(home / ".ssh" / "id_ed25519")}, ctx)
    with pytest.raises(LocalDenied):
        ops.op_run({"argv": ["true"], "cwd": canonical(home / ".ssh")}, ctx)
    # push may not write through a symlink destination either
    with pytest.raises(PathChanged):
        ops.op_resolve({"path": "~/work/innocent.txt", "kind": "parent"}, ctx)


def test_deny_toml_adds_paths(home, tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "deny.toml").write_text('paths = ["~/work/sub"]\n')
    cfg = config.load(home, cfg_dir, tmp_path / "state")
    roots = deny_roots(cfg.deny_entries(), str(home))
    ctx = ops.Context(home=str(home), roots=roots)
    with pytest.raises(LocalDenied):
        ops.op_resolve({"path": "~/work/sub", "kind": "dir"}, ctx)
    # the built-in list is always there, and the agent's own files are on it
    assert canonical(home / ".ssh") in roots and str(cfg_dir) in roots
    (cfg_dir / "deny.toml").write_text('paths = "nope"\n')
    with pytest.raises(config.ConfigError):
        config.load(home, cfg_dir, tmp_path / "state")


def test_swap_after_the_approval_is_caught_at_execution(home):
    """TOCTOU: the card showed a canonical path; a symlink planted afterwards fails the walk."""
    ctx = ctx_for(home)
    target = ops.op_resolve({"path": "~/work/sub", "kind": "dir"}, ctx)["canonical"]
    (home / "work" / "sub" / "f.txt").write_text("approved")
    file_path = canonical(home / "work" / "sub" / "f.txt")
    secret = home / "elsewhere"
    secret.mkdir()
    (secret / "f.txt").write_text("not approved")
    # swap the folder for a symlink to another place
    os.rename(home / "work" / "sub", home / "work" / "sub.old")
    (home / "work" / "sub").symlink_to(secret)
    with pytest.raises(PathChanged):
        ops.op_read({"path": file_path}, ctx)
    with pytest.raises(PathChanged):
        ops.op_ls({"path": target, "depth": 1}, ctx)
    with pytest.raises(PathChanged):
        ops.op_run({"argv": ["true"], "cwd": target}, ctx)
    with pytest.raises(PathChanged):
        ops.PushWriter({"path": target + "/new.bin", "size": 1, "sha256": "0" * 64}, ctx)
    assert not (secret / "new.bin").exists()
    # swap a file for a symlink
    (home / "work" / "sub").unlink()
    os.rename(home / "work" / "sub.old", home / "work" / "sub")
    os.unlink(file_path)
    os.symlink(secret / "f.txt", file_path)
    with pytest.raises(PathChanged):
        ops.op_read({"path": file_path}, ctx)
    with pytest.raises(PathChanged):
        open_verified(file_path, ctx.roots)


def test_ls_skips_denied_folders_and_caps_entries(home, monkeypatch):
    ctx = ctx_for(home)
    (home / "work" / "sub" / "deep.txt").write_text("x")
    (home / "work" / "link").symlink_to(home / ".ssh")
    listed = ops.op_ls({"path": canonical(home), "depth": 3}, ctx)
    paths = {item["path"] for item in listed["entries"]}
    assert "work/sub/deep.txt" in paths and ".ssh/" in paths
    assert not any(item.startswith(".ssh/") and item != ".ssh/" for item in paths)  # not descended
    link = next(item for item in listed["entries"] if item["path"] == "work/link")
    assert link["type"] == "link"  # shown, never followed
    monkeypatch.setattr(ops, "LS_MAX_ENTRIES", 2)
    capped = ops.op_ls({"path": canonical(home / "work"), "depth": 1}, ctx)
    assert len(capped["entries"]) == 2 and capped["truncated"]
    with pytest.raises(ValueError):
        ops.op_ls({"path": canonical(home), "depth": 4}, ctx)


def test_read_limits(home):
    ctx = ctx_for(home)
    big = home / "work" / "big.bin"
    with open(big, "wb") as handle:
        handle.truncate(17 * 1024 * 1024)
    with pytest.raises(ValueError):
        ops.op_read({"path": canonical(big)}, ctx)
    data = ops.op_read({"path": canonical(home / "work" / "a.txt"), "offset": 1, "length": 3}, ctx)
    assert data["data"] == "ell" and not data["eof"] and data["next_offset"] == 4
    assert ops.op_read({"path": canonical(home / "work" / "a.txt"), "b64": True}, ctx)["data"] == "aGVsbG8="
    with pytest.raises(ValueError):
        ops.op_read({"path": canonical(home / "work" / "a.txt"), "length": 2 * 1024 * 1024}, ctx)
    with pytest.raises(ValueError):
        ops.op_read({"path": "relative.txt"}, ctx)


# --- run ---

def test_run_caps_output_minimal_env_and_exit(home, monkeypatch):
    ctx = ctx_for(home)
    monkeypatch.setenv("BOT_OFFICE_SECRET_PROBE", "leak")
    work = canonical(home / "work")
    done = ops.op_run({"argv": ["sh", "-c", "env; head -c 200000 /dev/zero | tr '\\0' x; echo err >&2; exit 7"],
                       "cwd": work}, ctx)
    assert done["exit"] == 7 and done["stdout_truncated"] and len(done["stdout"].encode()) == ops.STREAM_CAP
    assert "BOT_OFFICE_SECRET_PROBE" not in done["stdout"] and f"HOME={home}" in done["stdout"]
    assert done["stderr"] == "err\n" and not done["timed_out"]
    assert ops.op_run({"argv": ["pwd"], "cwd": work}, ctx)["stdout"].strip() == work
    assert ops.op_run({"argv": ["no-such-program-x"], "cwd": work}, ctx)["exit"] == 127
    assert ops.op_run({"sh": "echo $((6*7))", "cwd": work}, ctx)["stdout"] == "42\n"
    with pytest.raises(ValueError):
        ops.op_run({"argv": ["true"], "cwd": work, "timeout": 301}, ctx)


def test_run_timeout_kills_the_whole_process_group(home, tmp_path):
    ctx = ctx_for(home)
    marker = tmp_path / "survivor"
    started = time.monotonic()
    done = ops.op_run({"sh": f"(sleep 3; touch {marker}) & sleep 30", "cwd": canonical(home / "work"),
                       "timeout": 1}, ctx)
    assert done["timed_out"] and done["exit"] is None and time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists()  # the background child died with the group


def test_run_cancel(home):
    import threading
    ctx = ctx_for(home)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    done = ops.op_run({"argv": ["sleep", "30"], "cwd": canonical(home / "work")}, ctx, cancel)
    assert done["cancelled"] and done["exit"] is None


def test_sandbox_refuses_without_bwrap_and_hides_denied_paths(home, monkeypatch):
    ctx = ctx_for(home)
    ctx.sandbox = True
    monkeypatch.setattr(ops.shutil, "which", lambda name: None)
    with pytest.raises(LocalDenied):
        ops.op_run({"argv": ["true"], "cwd": canonical(home / "work")}, ctx)
    monkeypatch.setattr(ops.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(ops, "bwrap_works", lambda path: False)  # e.g. user namespaces restricted
    with pytest.raises(LocalDenied):
        ops.op_run({"argv": ["true"], "cwd": canonical(home / "work")}, ctx)
    prefix = ops.bwrap_prefix(canonical(home / "work"), ctx.roots, "/usr/bin/bwrap", 7)
    assert "--unshare-net" in prefix and "--die-with-parent" in prefix and prefix[-1] == "--"
    assert prefix[prefix.index(canonical(home / ".ssh")) - 1] == "--tmpfs"
    assert prefix[prefix.index("--chdir") + 1] == canonical(home / "work")
    # the working folder is the verified fd, bound over its path, not the path looked up again
    assert prefix[prefix.index("--ro-bind-fd") + 1:prefix.index("--ro-bind-fd") + 3] == ["7", canonical(home / "work")]


# --- push / pull ---

def test_push_checks_sha256_overwrite_and_mode(home):
    ctx = ctx_for(home)
    data = b"payload"
    digest = hashlib.sha256(data).hexdigest()
    dest = canonical(home / "work") + "/new.bin"
    writer = ops.PushWriter({"path": dest, "size": len(data), "sha256": digest}, ctx)
    writer.write(data)
    assert writer.commit()["sha256"] == digest
    writer.close()
    assert Path(dest).read_bytes() == data and os.stat(dest).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        ops.PushWriter({"path": dest, "size": 1, "sha256": digest}, ctx)
    wrong = ops.PushWriter({"path": dest, "size": len(data), "sha256": "0" * 64, "overwrite": True}, ctx)
    wrong.write(data)
    with pytest.raises(ValueError):
        wrong.commit()
    wrong.close()
    long = ops.PushWriter({"path": dest, "size": 1, "sha256": digest, "overwrite": True}, ctx)
    with pytest.raises(ValueError):
        long.write(b"too long")
    long.close()
    replaced = ops.PushWriter({"path": dest, "size": 3, "sha256": hashlib.sha256(b"new").hexdigest(),
                               "overwrite": True}, ctx)
    replaced.write(b"new")
    replaced.commit()
    replaced.close()
    assert Path(dest).read_bytes() == b"new"
    assert sorted(os.listdir(home / "work")) == ["a.txt", "new.bin", "sub"]  # no temp files left behind


def test_pull_refuses_a_file_that_changed_size(home):
    ctx = ctx_for(home)
    path = canonical(home / "work" / "a.txt")
    fd, size = ops.read_chunks({"path": path, "size": 5}, ctx)
    os.close(fd)
    assert size == 5
    with pytest.raises(PathChanged):
        ops.read_chunks({"path": path, "size": 4}, ctx)


# --- flags, config, confirm, token ---

def test_flag_semantics_local_on_off_always_win():
    assert config.effective("on", False) is True
    assert config.effective("off", True) is False
    assert config.effective("server", True) is True
    assert config.effective("server", False) is False
    assert config.effective("server", None) is False  # unknown server value: off


def test_config_values(tmp_path, home):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    assert config.load(home, cfg_dir, tmp_path / "st").local_flags() == {"sandbox": "server", "confirm_local": "server"}
    (cfg_dir / "config.toml").write_text('sandbox = "on"\nconfirm_local = "off"\nconfirm_command = ["true"]\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    assert cfg.sandbox == "on" and cfg.confirm_local == "off" and cfg.confirm_command == ["true"]
    (cfg_dir / "config.toml").write_text('sandbox = "yes"\n')
    with pytest.raises(config.ConfigError):
        config.load(home, cfg_dir, tmp_path / "st")


def test_confirm_command_missing_or_slow_means_no():
    assert ops.confirm_with(["true"], 5)("ok?") is True
    assert ops.confirm_with(["false"], 5)("ok?") is False
    assert ops.confirm_with(["no-such-confirm-program-x"], 5)("ok?") is False
    assert ops.confirm_with(["sleep", "5"], 1)("ok?") is False


def test_confirm_text_is_one_argument(tmp_path):
    out = tmp_path / "asked"
    script = "import sys; open(sys.argv[1], 'w').write(sys.argv[2])"
    assert ops.confirm_with([sys.executable, "-c", script, str(out), "{text}"], 10)("run; rm -rf $HOME")
    assert out.read_text() == "run; rm -rf $HOME"  # never through a shell


def test_token_file_store_is_private(tmp_path, home):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text('token_store = "file"\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    store = config.TokenStore(cfg, "abc")
    assert store.save("bom_abc_secret") == "file"
    assert os.stat(cfg_dir / "token").st_mode & 0o777 == 0o600 and store.load() == "bom_abc_secret"
    os.chmod(cfg_dir / "token", 0o644)
    with pytest.raises(config.ConfigError):
        store.load()
    store.clear()
    assert not (cfg_dir / "token").exists()


def test_token_keyring_store_through_secret_tool(tmp_path, home, monkeypatch):
    cfg = config.load(home, tmp_path / "cfg", tmp_path / "st")
    calls = []

    class Done:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    def runner(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return Done(0, "bom_abc_secret\n") if argv[1] == "lookup" else Done(0)

    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/secret-tool")
    store = config.TokenStore(cfg, "abc", runner=runner)
    assert store.save("bom_abc_secret") == "keyring" and store.load() == "bom_abc_secret"
    stored = calls[0]
    assert stored[0][:2] == ["secret-tool", "store"] and stored[1] == "bom_abc_secret"
    assert "bom_abc_secret" not in " ".join(stored[0])  # never on a command line
    assert not (tmp_path / "cfg" / "token").exists()


def test_fingerprint_matches_the_server():
    from botoffice import machines
    token = "bom_0123456789ab_" + "A" * 43
    assert config.fingerprint(token) == machines.fingerprint(machines.token_hash(token))


def test_pair_url_rules():
    assert cli.http_url("ws://127.0.0.1:18787") == "http://127.0.0.1:18787"
    assert cli.http_url("wss://desk.example.com") == "https://desk.example.com"
    for bad in ("ws://desk.example.com", "http://10.0.0.2:8787", "ftp://x", "nonsense"):
        with pytest.raises(config.ConfigError):
            cli.http_url(bad)


# --- confirm-local in the call path ---

def test_confirm_local_decline_is_reported(home, tmp_path):
    pytest.importorskip("websockets")
    from botoffice_agent import client
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text('token_store = "file"\nconfirm_local = "on"\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    config.TokenStore(cfg, "abc").save("bom_abc_x")
    asked = []

    def confirm(text):
        asked.append(text)
        return False

    async def scenario():
        agent = client.Agent(cfg, {"machine_id": "abc"}, config.TokenStore(cfg, "abc"), confirm=confirm)
        sent = []

        async def send(message):
            sent.append(message)

        agent.send = send
        await agent.call("c1", "read", {"path": canonical(home / "work" / "a.txt"), "meta": {"bot": "Ada"}})
        await agent.call("c2", "resolve", {"path": "~/work", "kind": "dir"})  # resolving never asks
        return sent

    sent = asyncio.run(scenario())
    assert sent[0] == {"type": "result", "id": "c1", "ok": False, "code": "declined_local",
                       "error": "declined on this computer (or no answer)"}
    assert sent[1]["ok"] and len(asked) == 1 and asked[0].startswith("Ada wants to read")
    lines = cfg.audit_path.read_text().splitlines()
    assert '"declined_local"' in lines[0] and "hello" not in lines[0]  # contents never in the log
    assert os.stat(cfg.audit_path).st_mode & 0o777 == 0o600


def test_sandbox_runs_read_only_without_network_where_bwrap_works(home):
    bwrap = ops.shutil.which("bwrap")
    if not bwrap or not ops.bwrap_works(bwrap):
        pytest.skip("bwrap cannot build a sandbox on this host")
    ctx = ctx_for(home)
    ctx.sandbox = True
    ctx.commands = denycmd.Checker(str(home), [])  # past the command tripwire: this tests the sandbox itself
    work = canonical(home / "work")
    done = ops.op_run({"sh": "cat a.txt; echo; ls -A ../.ssh | wc -l; touch new 2>/dev/null || echo ro", "cwd": work}, ctx)
    assert done["sandboxed"] and done["stdout"].split() == ["hello", "0", "ro"]


def test_push_chunks_that_arrive_with_the_call_are_kept(home, tmp_path):
    """The hub sends the call and its chunks back to back; none may be lost before the task starts."""
    import base64
    pytest.importorskip("websockets")
    from botoffice_agent import client
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text('token_store = "file"\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    config.TokenStore(cfg, "abc").save("bom_abc_x")
    data = b"x" * 1000
    dest = canonical(home / "work") + "/pushed.bin"

    async def scenario():
        agent = client.Agent(cfg, {"machine_id": "abc"}, config.TokenStore(cfg, "abc"))
        sent = []

        async def send(message):
            sent.append(message)

        agent.send = send
        await agent.handle({"type": "call", "id": "p1", "op": "push",
                            "args": {"path": dest, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}})
        for start in range(0, len(data), 300):
            await agent.handle({"type": "chunk", "id": "p1", "data": base64.b64encode(data[start:start + 300]).decode()})
        await agent.handle({"type": "chunk_end", "id": "p1"})
        await asyncio.gather(*agent.tasks)
        return sent

    sent = asyncio.run(scenario())
    assert sent[-1]["ok"], sent
    assert sent[0] == {"type": "credit", "id": "p1", "n": client.UPLOAD_QUEUE}  # granted once ready to write
    assert Path(dest).read_bytes() == data


def test_more_chunks_than_granted_cancel_that_push_without_stalling(home, tmp_path):
    """The receive loop never waits on a full upload queue, so a cancel for another call still lands."""
    import base64
    from botoffice_agent import client
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text('token_store = "file"\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    config.TokenStore(cfg, "abc").save("bom_abc_x")
    data = b"y" * (client.UPLOAD_QUEUE + 4)
    dest = canonical(home / "work") + "/flood.bin"

    async def scenario():
        agent = client.Agent(cfg, {"machine_id": "abc"}, config.TokenStore(cfg, "abc"))
        sent = []

        async def send(message):
            sent.append(message)

        agent.send = send
        await agent.handle({"type": "call", "id": "p1", "op": "push",
                            "args": {"path": dest, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}})
        agent.cancels["other"] = threading.Event()
        started = time.monotonic()
        for byte in data:  # a server that ignores the credit
            await agent.handle({"type": "chunk", "id": "p1", "data": base64.b64encode(bytes([byte])).decode()})
        await agent.handle({"type": "cancel", "id": "other"})
        elapsed = time.monotonic() - started
        other = agent.cancels.pop("other").is_set()
        await asyncio.gather(*agent.tasks)
        return sent, elapsed, other

    sent, elapsed, other = asyncio.run(scenario())
    assert elapsed < 1 and other
    assert sent[-1]["ok"] is False and not Path(dest).exists()


def test_children_left_behind_are_killed_when_the_command_exits(home, tmp_path):
    ctx = ctx_for(home)
    marker = tmp_path / "late"
    done = ops.op_run({"sh": f"(sleep 2; touch {marker}) >/dev/null 2>&1 & echo started", "cwd": canonical(home / "work")},
                      ctx)
    assert done["exit"] == 0 and done["stdout"] == "started\n"
    time.sleep(3)
    assert not marker.exists()


def test_cancelled_before_start_never_runs(home, tmp_path):
    import threading
    ctx = ctx_for(home)
    cancel = threading.Event()
    cancel.set()
    marker = tmp_path / "ran"
    with pytest.raises(ops.Declined):
        ops.op_run({"argv": ["touch", str(marker)], "cwd": canonical(home / "work")}, ctx, cancel)
    assert not marker.exists()


def test_a_late_yes_after_cancel_is_void(home, tmp_path):
    """Confirm-local: Bot Office cancels while the question is open; the later yes runs nothing."""
    pytest.importorskip("websockets")
    import threading
    from botoffice_agent import client
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text('token_store = "file"\nconfirm_local = "on"\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    config.TokenStore(cfg, "abc").save("bom_abc_x")
    marker = tmp_path / "ran"
    asking, answer = threading.Event(), threading.Event()

    def confirm(text):
        asking.set()
        answer.wait(10)
        return True

    async def scenario():
        agent = client.Agent(cfg, {"machine_id": "abc"}, config.TokenStore(cfg, "abc"), confirm=confirm)
        sent = []

        async def send(message):
            sent.append(message)

        agent.send = send
        await agent.handle({"type": "call", "id": "r1", "op": "run",
                            "args": {"argv": ["touch", str(marker)], "cwd": canonical(home / "work"), "timeout": 5}})
        await asyncio.to_thread(asking.wait, 10)
        await agent.handle({"type": "cancel", "id": "r1"})
        answer.set()
        await asyncio.gather(*agent.tasks)
        return sent

    sent = asyncio.run(scenario())
    assert sent[-1]["ok"] is False and sent[-1]["code"] == "declined_local"
    assert not marker.exists()


def test_large_folder_listing_stops_at_the_cap(home, monkeypatch):
    ctx = ctx_for(home)
    many = home / "work" / "many"
    many.mkdir()
    for index in range(50):
        (many / f"f{index:03}").write_text("")
    monkeypatch.setattr(ops, "LS_MAX_ENTRIES", 10)
    listed = ops.op_ls({"path": canonical(many), "depth": 1}, ctx)
    assert len(listed["entries"]) == 10 and listed["truncated"]


# --- the local tunnel port must be this user's ---

PROC_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:4963 00000000:0000 0A 00000000:00000000 00:00000000 00000000  {listener}        0 1 1
   1: 0100007F:4963 0100007F:D431 01 00000000:00000000 00:00000000 00000000  {accepted}        0 2 1
   2: 0100007F:D431 0100007F:4963 01 00000000:00000000 00:00000000 00000000  1000        0 3 1
"""


def test_tunnel_port_owner_is_checked():
    from botoffice_agent import tunnel
    table = PROC_TCP.format(listener=1000, accepted=1000)
    assert tunnel._owner([table], 0x4963, 0xD431) == 1000
    squatted = PROC_TCP.format(listener=1001, accepted=1001)
    assert tunnel._owner([squatted], 0x4963, 0xD431) == 1001
    assert tunnel._owner([table], 0x4963, 0x1111) is None


def test_a_port_held_by_another_user_gets_nothing():
    import socket as socketlib
    from botoffice_agent import tunnel
    import threading
    listener = socketlib.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
    try:
        sock = tunnel.connect("127.0.0.1", port)  # our own listener: fine
        own = sock.getsockname()[1]
        with pytest.raises(tunnel.TunnelNotOurs):
            tunnel.check(sock, uid=os.getuid() + 1, wait=0.2)  # as seen by someone else
        # a listener that never accepts shows uid 0 for the waiting connection: refused
        never = PROC_TCP.replace("4963", f"{port:04X}").replace("D431", f"{own:04X}").format(listener=0, accepted=0)
        with pytest.raises(tunnel.TunnelNotOurs):
            tunnel.check(sock, tables=[never], uid=os.getuid() or 1, wait=0.2)
        sock.close()
    finally:
        listener.close()
        for item in accepted:
            item.close()
    with pytest.raises(tunnel.TunnelNotOurs):
        tunnel.connect("desk.example.com", 18787)


def test_root_never_sends_over_the_plain_tunnel():
    """For root, a listener that has not accepted yet looks like its own (uid 0): always refused."""
    import socket as socketlib
    from botoffice_agent import tunnel
    a, b = socketlib.socketpair(socketlib.AF_UNIX)
    with a, b, pytest.raises(tunnel.TunnelNotOurs, match="root"):
        tunnel.check(a, tables=[], uid=0, wait=0)


# --- the command deny-list ---

CATASTROPHIC_ARGV = [
    ["rm", "-rf", "/"], ["rm", "-fr", "/"], ["rm", "-r", "-f", "/"], ["rm", "-rf", "--no-preserve-root", "/"],
    ["rm", "--recursive", "--force", "/"], ["rm", "-rf", "~/"], ["rm", "-rf", "~"], ["rm", "-rf", "$HOME"],
    ["rm", "-rf", "{home}"], ["rm", "-rf", "{home}/"], ["rm", "-Rf", "{home}/.."],
    ["sudo", "rm", "-rf", "/"], ["env", "X=1", "nice", "-n", "5", "rm", "-rf", "/"], ["timeout", "5", "rm", "-rf", "/"],
    ["rm", "-rf", "--", "/"], ["rm", "-rf", "/./"], ["rm", "-rf", "//"], ["/bin/rm", "-rf", "/"],
    ["mkfs.ext4", "/dev/sda1"], ["mkfs", "-t", "ext4", "/dev/sdb"], ["mke2fs", "/dev/sdb"], ["wipefs", "-a", "/dev/sda"],
    ["dd", "if=/dev/zero", "of=/dev/sda"], ["dd", "if=x.img", "of=/dev/nvme0n1", "bs=4M"], ["tee", "/dev/sda"],
    ["shutdown", "-h", "now"], ["reboot"], ["poweroff"], ["halt"], ["/sbin/reboot"], ["systemctl", "poweroff"],
    ["systemctl", "reboot"], ["sudo", "systemctl", "--no-wall", "reboot"], ["init", "0"], ["loginctl", "poweroff"],
    ["chmod", "-R", "777", "/"], ["chown", "-R", "nobody", "~"], ["chmod", "--recursive", "000", "$HOME"],
    ["find", "/", "-delete"], ["find", "~", "-exec", "rm", "-rf", "{}", "+"],
    ["cat", "~/.ssh/id_ed25519"], ["cp", "{home}/.ssh/id_ed25519", "/tmp/x"], ["tar", "czf", "x.tgz", ".ssh"],
    ["ls", "{home}/.config/google-chrome/Default/Cookies"], ["sqlite3", "--", "~/.mozilla/x"],
    ["rsync", "-a", "--include=~/.gnupg/**", "x"], ["cat", "/etc/shadow"], ["bash", "-c", "rm -rf /"],
    ["sh", "-c", "cat ~/.ssh/id_ed25519"], ["xargs", "rm", "-rf", "/"],
]
CATASTROPHIC_SH = [
    "rm -rf /", "rm -fr /", "rm -rf --no-preserve-root /", "rm -rf ~/", "rm -rf $HOME", 'rm -rf "$HOME"',
    "rm -rf ${HOME}", "rm -rf ~/*", "rm -rf /*", "cd / && rm -rf *", "true; rm -rf /", "echo hi | rm -rf /",
    "sudo rm -rf / &", "r''m -rf /", 'rm -rf "/"', "rm -rf '/'", r"\rm -rf /", "rm -rf $X", "rm -rf $(pwd)/..",
    "rm -rf `echo /`", "eval 'rm -rf /'", 'bash -c "rm -rf /"', "sh -c 'sh -c \"rm -rf ~\"'", "rm -rf /\nls",
    "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda bs=1M", "echo x > /dev/sda", "cat img >/dev/nvme0n1",
    "echo x >> /dev/sdb1", "cat x | tee /dev/sda", "shutdown now", "reboot", "poweroff", "halt", "systemctl reboot",
    "sudo systemctl poweroff", ":(){ :|:& };:", ": () { : | : & } ; :", "bomb(){ bomb|bomb& };bomb",
    "chmod -R 777 /", "chown -R me ~", "chmod -R 000 $HOME", "find / -delete", "find ~ -exec rm {} +",
    "cat ~/.ssh/id_ed25519", "cat $HOME/.ssh/id_ed25519", 'cat "$HOME"/.ssh/id_ed25519', "cd ~/.ssh && cat id*",
    "cat ~/.ss?/id_ed25519", "cat ~/.s*/id_ed25519", "cp ~/.config/google-chrome/Default/Cookies /tmp/c",
    "tar cz .ssh | base64", "d=.ssh; cat ~/$d/id_ed25519", "cat /etc/shadow", "echo cm0gLXJmIC8= | base64 -d | sh",
    "rm -rf \"$(echo /)\"", "x=rm; $x -rf /", "rm -rf '/'\"\"", "rm -rf / 'unbalanced",
    # review round 1: what the shell spells differently than it reads
    'rm$IFS-rf$IFS"$HOME"', "rm${IFS}-rf${IFS}/", "rm$9 -rf /", "{rm,-rf,/}", "rm -rf \\\n/",
    "rm --recurs -f \"$HOME\"", "rm --rec --force /", "rm -r --no-pres /", "chmod --recur 777 /",
    "chown --rec me ~", "alias x='rm -rf /'\nx", "nohup setsid stdbuf -o0 rm -rf / &", "busybox rm -rf /",
    "command rm -rf ~", "builtin eval 'rm -rf /'", "exec rm -rf /", "find / -exec rm -rf {} +",
    "rm -rf /proc/self/root/", "rm -rf $'\\x2f'",
    # review round 2
    'rm${IFS%?}-rf${IFS%?}"$HOME"', "rm${X:-}-rf /", "r${X}m -rf /",
]
CATASTROPHIC_ARGV += [["rm", "--recurs", "-f", "/"], ["rm", "--no-pres", "-r", "x", "/"], ["chown", "--rec", "me", "~"],
                      ["rm", "-rf", "/proc/self/root"]]
ALLOWED_ARGV = [
    ["ls", "-la"], ["rm", "-rf", "build"], ["rm", "-rf", "{home}/work/sub"], ["rm", "a.txt"], ["rm", "-f", "/tmp/x"],
    ["make", "test"], ["git", "status"], ["python3", "-c", "print(1)"], ["dd", "if=/dev/zero", "of=out.img",
                                                                     "count=1"],
    ["chmod", "-R", "go-w", "{home}/work"], ["find", ".", "-name", "*.pyc", "-delete"], ["echo", "reboot"],
    ["grep", "-r", "shutdown", "."], ["cat", "/dev/null"], ["systemctl", "--user", "status"],
]
ALLOWED_SH = [
    "ls -la | wc -l", "rm -rf build dist", "make test && echo ok", "for f in *.txt; do wc -l $f; done",
    "echo hi > /dev/null", "find . -name '*.o' -delete", "git log --oneline | head", "cd sub && rm -rf node_modules",
    "dd if=/dev/zero of=/tmp/x bs=1k count=1 2>/dev/null", "python3 -c 'print(42)'", "echo $HOME", "du -sh ~/work",
    "echo {a,b}.txt", "rm --force a.txt", "ls --recursive .", "IFS=, read a b <<< 'x,y'; echo $a",
    "make \\\n  test",
]


def _checker(home):
    roots = deny_roots([*config.DEFAULT_DENY], str(home))
    return denycmd.Checker(str(home), roots, ())


@pytest.mark.parametrize("argv", CATASTROPHIC_ARGV, ids=lambda a: " ".join(a)[:40])
def test_catastrophic_and_credential_argv_is_refused(home, argv):
    with pytest.raises(LocalDenied):
        _checker(home).check([item.replace("{home}", str(home)) for item in argv], None, str(home / "work"))


@pytest.mark.parametrize("text", CATASTROPHIC_SH, ids=lambda t: t[:40])
def test_catastrophic_and_credential_sh_is_refused(home, text):
    with pytest.raises(LocalDenied):
        _checker(home).check(None, text.replace("{home}", str(home)), str(home / "work"))


@pytest.mark.parametrize("argv", ALLOWED_ARGV, ids=lambda a: " ".join(a)[:40])
def test_ordinary_argv_is_allowed(home, argv):
    _checker(home).check([item.replace("{home}", str(home)) for item in argv], None, str(home / "work"))


@pytest.mark.parametrize("text", ALLOWED_SH, ids=lambda t: t[:40])
def test_ordinary_sh_is_allowed(home, text):
    _checker(home).check(None, text, str(home / "work"))


def test_rm_in_the_home_folder_itself_is_catastrophic(home):
    with pytest.raises(LocalDenied):
        _checker(home).check(["rm", "-rf", "."], None, str(home))  # cwd is home
    with pytest.raises(LocalDenied):
        _checker(home).check(None, "rm -rf ..", str(home / "work"))
    _checker(home).check(["rm", "-rf", "."], None, str(home / "work"))


def test_deny_toml_commands_and_paths(home, tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "deny.toml").write_text('paths = ["~/work/secret"]\ncommands = ["\\\\bgit\\\\s+push\\\\b"]\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    roots = deny_roots(cfg.deny_entries(), str(home))
    checker = denycmd.Checker(str(home), roots, cfg.deny_commands)
    for argv, sh in ((["git", "push", "origin"], None), (None, "cd x && git  push"), (["cat", "secret/x"], None),
                     (None, f"cat {cfg_dir}/config.toml")):
        with pytest.raises(LocalDenied):
            checker.check(argv, sh, str(home / "work"))
    checker.check(["git", "pull"], None, str(home / "work"))
    (cfg_dir / "deny.toml").write_text('commands = ["(unclosed"]\n')
    with pytest.raises(config.ConfigError):
        config.load(home, cfg_dir, tmp_path / "st")


def test_resolve_and_run_check_the_command(home, tmp_path):
    ctx = ctx_for(home)
    work = canonical(home / "work")
    with pytest.raises(LocalDenied):
        ops.op_resolve({"path": "~/work", "kind": "dir", "argv": ["rm", "-rf", "/"], "sh": None}, ctx)
    with pytest.raises(LocalDenied):
        ops.op_resolve({"path": "~/work", "kind": "dir", "argv": None, "sh": "cat ~/.ssh/id_ed25519"}, ctx)
    assert ops.op_resolve({"path": "~/work", "kind": "dir", "argv": ["ls"], "sh": None}, ctx)["canonical"] == work
    # and again at execution, whatever was approved
    marker = tmp_path / "never"
    with pytest.raises(LocalDenied):
        ops.op_run({"sh": f"touch {marker}; reboot", "cwd": work}, ctx)
    assert not marker.exists()


# --- session grants on the computer: confirm once per grant, End grant ---

def _agent(home, tmp_path, confirm_local="on", confirm=None):
    from botoffice_agent import client
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.toml").write_text(f'token_store = "file"\nconfirm_local = "{confirm_local}"\n')
    cfg = config.load(home, cfg_dir, tmp_path / "st")
    config.TokenStore(cfg, "abc").save("bom_abc_x")
    agent = client.Agent(cfg, {"machine_id": "abc"}, config.TokenStore(cfg, "abc"), confirm=confirm)
    sent = []

    async def send(message):
        sent.append(message)

    agent.send = send
    return agent, sent, cfg


def test_confirm_local_asks_once_per_grant_and_again_when_widened(home, tmp_path):
    pytest.importorskip("websockets")
    asked = []
    answers = {"7": True, "8": False}

    def confirm(text):
        asked.append(text)
        return answers[text.split("#")[1].split()[0]] if "#" in text else True

    async def scenario():
        agent, sent, _ = _agent(home, tmp_path, confirm=confirm)
        read = {"path": canonical(home / "work" / "a.txt")}

        def meta(grant, scope):
            return {"bot": "Ada", "grant": grant, "scope": scope, "task": f"#{grant} fix it", "until": "17:00"}

        for index in range(3):
            await agent.call(f"a{index}", "read", {**read, "meta": meta(7, "read")})
        await agent.call("w1", "read", {**read, "meta": meta(7, "shell")})  # widened: asks again
        for index in range(2):
            await agent.call(f"b{index}", "read", {**read, "meta": meta(8, "read")})  # no: once, held for the grant
        return sent

    sent = asyncio.run(scenario())
    assert len(asked) == 3
    assert "Ada may look at files" in asked[0] and "for: #7 fix it" in asked[0] and "until 17:00" in asked[0]
    assert [m["ok"] for m in sent] == [True, True, True, True, False, False]
    assert sent[-1]["code"] == "declined_local"


def test_confirm_local_off_never_asks(home, tmp_path):
    pytest.importorskip("websockets")

    def confirm(text):
        raise AssertionError("asked")

    async def scenario():
        agent, sent, _ = _agent(home, tmp_path, confirm_local="off", confirm=confirm)
        agent.server_flags = {"confirm_local": True}  # this computer's off wins over Bot Office
        await agent.call("a", "read", {"path": canonical(home / "work" / "a.txt"), "meta": {"grant": 1, "scope": "read"}})
        return sent

    assert asyncio.run(scenario())[0]["ok"]


def test_end_grant_kills_the_running_process_group(home, tmp_path):
    pytest.importorskip("websockets")
    pids = tmp_path / "pids"

    async def scenario():
        agent, sent, cfg = _agent(home, tmp_path, confirm_local="off")
        meta = {"bot": "Ada", "grant": 5, "scope": "shell", "request": 1}
        await agent.handle({"type": "call", "id": "r1", "op": "run", "args": {
            "sh": f"sleep 120 & echo $! > {pids}; sleep 120", "cwd": canonical(home / "work"), "timeout": 300,
            "meta": meta}})
        await agent.handle({"type": "call", "id": "r2", "op": "run", "args": {
            "argv": ["sleep", "120"], "cwd": canonical(home / "work"), "timeout": 300, "meta": {**meta, "grant": 6}}})
        for _ in range(100):
            if pids.exists() and pids.read_text().strip():
                break
            await asyncio.sleep(0.05)
        started = time.monotonic()
        await agent.handle({"type": "end_grant", "grant": 5})
        done = [task for task in agent.tasks]
        await asyncio.wait(done, timeout=0.1)
        while not any(m.get("id") == "r1" for m in sent) and time.monotonic() - started < 15:
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        still_other = any(not t.done() for t in agent.tasks)
        # a call of the ended grant that arrives late is refused here
        await agent.call("r3", "read", {"path": canonical(home / "work" / "a.txt"), "meta": meta})
        await agent.handle({"type": "end_grant", "grant": 6})
        await asyncio.wait_for(asyncio.gather(*agent.tasks), 15)
        return sent, elapsed, still_other, cfg

    sent, elapsed, still_other, cfg = asyncio.run(scenario())
    first = next(m for m in sent if m["id"] == "r1")
    assert first["ok"] and first["data"]["cancelled"] and first["data"]["exit"] is None and elapsed < 10
    assert still_other  # another grant's call kept running until its own end
    child = int(pids.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the background child of the command's process group is gone too
    assert next(m for m in sent if m["id"] == "r3")["code"] == "declined_local"
    log = cfg.audit_path.read_text()
    assert '"op": "end_grant"' in log and '"grant": 5' in log


def test_a_local_no_holds_when_the_grant_is_widened(home, tmp_path):
    pytest.importorskip("websockets")
    asked = []

    def confirm(text):
        asked.append(text)
        return False

    async def scenario():
        agent, sent, _ = _agent(home, tmp_path, confirm=confirm)
        read = {"path": canonical(home / "work" / "a.txt")}
        await agent.call("a", "read", {**read, "meta": {"bot": "Ada", "grant": 9, "scope": "read"}})
        await agent.call("b", "read", {**read, "meta": {"bot": "Ada", "grant": 9, "scope": "shell"}})
        return sent

    sent = asyncio.run(scenario())
    assert len(asked) == 1  # the widening does not get a second chance
    assert [m["code"] for m in sent] == ["declined_local", "declined_local"]


def test_a_read_cancelled_while_on_disk_sends_nothing_back(home, tmp_path, monkeypatch):
    pytest.importorskip("websockets")
    from botoffice_agent import client

    async def scenario():
        agent, sent, cfg = _agent(home, tmp_path, confirm_local="off")
        cancel = threading.Event()
        real = client.ops.op_read

        def slow(args, ctx):
            data = real(args, ctx)
            cancel.set()  # End grant arrives while the file is read
            return data

        monkeypatch.setattr(client.ops, "op_read", slow)
        agent.cancels["r"] = cancel
        await agent.call("r", "read", {"path": canonical(home / "work" / "a.txt"), "meta": {"grant": 3, "scope": "read"}})
        return sent, cfg

    sent, cfg = asyncio.run(scenario())
    assert sent[0]["ok"] is False and "data" not in sent[0]
    assert "hello" not in (cfg.state_dir / "audit.jsonl").read_text()


def test_the_hello_says_what_this_agent_does():
    from botoffice_agent import client
    assert {"grants", "end_grant", "deny_commands"} <= set(client.CAPS)


def test_a_late_local_yes_after_a_cancel_is_not_kept(home, tmp_path):
    pytest.importorskip("websockets")
    asked = []

    async def scenario():
        agent, sent, _ = _agent(home, tmp_path)
        cancel = threading.Event()

        def confirm(text):
            asked.append(text)
            cancel.set()  # Bot Office gave up while the question was open
            return True

        agent.confirm = confirm
        agent.cancels["a"] = cancel
        read = {"path": canonical(home / "work" / "a.txt")}
        await agent.call("a", "read", {**read, "meta": {"grant": 4, "scope": "read"}})
        agent.confirm = lambda text: asked.append(text) or False
        await agent.call("b", "read", {**read, "meta": {"grant": 4, "scope": "read"}})
        return sent

    sent = asyncio.run(scenario())
    assert len(asked) == 2 and [m["ok"] for m in sent] == [False, False]


def test_a_call_past_its_grant_end_is_refused_here(home, tmp_path):
    pytest.importorskip("websockets")

    async def scenario():
        agent, sent, _ = _agent(home, tmp_path, confirm_local="off")
        read = {"path": canonical(home / "work" / "a.txt")}
        await agent.call("a", "read", {**read, "meta": {"grant": 2, "until": "2000-01-01T00:00:00+00:00"}})
        await agent.call("b", "read", {**read, "meta": {"grant": 2, "until": "2999-01-01T00:00:00+00:00"}})
        return sent

    sent = asyncio.run(scenario())
    assert [m["ok"] for m in sent] == [False, True]


def test_push_cancelled_before_the_rename_leaves_nothing(home):
    cancel = threading.Event()
    target = home / "work" / "new.bin"
    writer = ops.PushWriter({"path": canonical(target), "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                             "overwrite": False}, ops.Context(home=str(home), roots=deny_roots([*config.DEFAULT_DENY],
                                                                                              str(home))))
    writer.write(b"abc")
    cancel.set()
    with pytest.raises(ValueError):
        writer.commit(cancel)
    writer.close()
    assert not target.exists() and [p.name for p in target.parent.iterdir() if p.name.startswith(".")] == []
