"""0.11.0 C1/B14: the privileged-path launcher, the isolation layout and its installer."""
import asyncio
import dataclasses
import json
import os
import runpy
import signal
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import __main__ as cli_main
from botoffice import isolation, procs
from botoffice.api import create_app
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.fake import FakeAdapter

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "deploy" / "bot-office-run"
INSTALL = ROOT / "deploy" / "install-isolation.sh"
H = {"Authorization": "Bearer test-token"}
LAUNCHER = runpy.run_path(str(RUN), run_name="bot_office_run")


@pytest.fixture
def layout(settings, tmp_path, monkeypatch):
    """Force the isolated layout with a fake sudo that just runs the launcher as this user."""
    sudo = tmp_path / "fake-sudo"
    sudo.write_text('#!/bin/sh\n# drop "-n -u USER"\nshift 3\nexec /usr/bin/python3 -I "$@"\n')
    sudo.chmod(0o755)
    monkeypatch.setattr(isolation, "SUDO", str(sudo))
    umask = os.umask(0o022)
    os.umask(umask)
    isolation._which_cache.clear()
    launcher = isolation.configure(settings, isolation.Launcher(RUN, "bots"))
    yield launcher
    isolation.configure(settings, None)
    isolation._which_cache.clear()
    os.umask(umask)


def user(home="/home/bots"):
    return type("pw", (), {"pw_dir": home, "pw_name": "bots", "pw_shell": "/bin/bash", "pw_uid": 4242})()


# --- the launcher ---------------------------------------------------------------------------

def test_launcher_parses_fixed_options_only():
    parse = LAUNCHER["parse"]
    assert parse(["--allow-sudo", "--cwd", "/w", "--env-stdin", "--", "/bin/x", "--cwd", "y"]) == (
        {"allow_sudo": True, "cwd": "/w", "env_stdin": True, "runtime_dir": False}, ["/bin/x", "--cwd", "y"])
    assert parse(["--", "/bin/true"]) == ({"allow_sudo": False, "cwd": None, "env_stdin": False,
                                           "runtime_dir": False}, ["/bin/true"])
    assert parse(["--runtime-dir", "--", "/bin/true"])[0]["runtime_dir"] is True
    for bad in (["/bin/true"], ["--shell", "--", "/bin/true"], ["--"], ["--cwd"], []):
        with pytest.raises(ValueError):
            parse(bad)


def test_launcher_env_is_built_from_an_allowlist():
    sent = {"BOT_OFFICE_AGENT_TOKEN": "agent", "BOT_OFFICE_JOB_ID": "j", "OPENAI_API_KEY": "k",
            "BOT_OFFICE_TOKEN_FILE": "/var/lib/bot-office/private/token", "BOT_OFFICE_PRIVATE_DIR": "/p",
            "LD_PRELOAD": "/tmp/evil.so", "PYTHONPATH": "/tmp", "SUDO_ASKPASS": "/x", "HOME": "/elsewhere",
            "PATH": "/opt/bot-office/bin:relative:/usr/bin", "CODEX_HOME": "/var/lib/bot-office/private",
            "TERM": "xterm", "NUL": "a\x00b"}
    env = LAUNCHER["build_env"](sent, user(), "/w")
    assert env["BOT_OFFICE_AGENT_TOKEN"] == "agent" and env["OPENAI_API_KEY"] == "k" and env["TERM"] == "xterm"
    for dropped in ("BOT_OFFICE_TOKEN_FILE", "BOT_OFFICE_PRIVATE_DIR", "LD_PRELOAD", "PYTHONPATH", "SUDO_ASKPASS", "NUL"):
        assert dropped not in env
    assert env["HOME"] == "/home/bots" and env["USER"] == env["LOGNAME"] == "bots"
    assert env["PATH"] == "/opt/bot-office/bin:/usr/bin"
    assert env["CODEX_HOME"] == "/home/bots/.codex"  # never outside the bots user's home
    confined = LAUNCHER["build_env"]({"HOME": "/w/home"}, user(), "/w")
    assert confined["HOME"] == "/w/home"  # restricted bots: HOME inside the workspace only
    assert LAUNCHER["build_env"]({"HOME": "/wx"}, user(), "/w")["HOME"] == "/home/bots"
    # Sec 8: `..` cannot climb out of the workspace or the bots user's home
    assert LAUNCHER["build_env"]({"HOME": "/w/../etc"}, user(), "/w")["HOME"] == "/home/bots"
    assert LAUNCHER["build_env"]({"CODEX_HOME": "/home/bots/../../var/lib/bot-office/private"}, user(),
                                 "/w")["CODEX_HOME"] == "/home/bots/.codex"
    assert LAUNCHER["build_env"]({"CODEX_HOME": "/home/bots/.codex-b"}, user(), "/w")["CODEX_HOME"] == "/home/bots/.codex-b"


def test_launcher_runtime_dir_only_for_podman(tmp_path, monkeypatch):
    """Sec 2: bot CLIs never get XDG_RUNTIME_DIR or the user-bus address; podman calls do."""
    monkeypatch.setattr(LAUNCHER["os"].path, "isdir", lambda path: True)
    bot = LAUNCHER["build_env"]({"XDG_RUNTIME_DIR": "/run/user/1", "DBUS_SESSION_BUS_ADDRESS": "x"}, user(), "/w")
    assert "XDG_RUNTIME_DIR" not in bot and "DBUS_SESSION_BUS_ADDRESS" not in bot
    podman = LAUNCHER["build_env"]({}, user(), None, True)
    assert podman["XDG_RUNTIME_DIR"].startswith("/run/user/") and "DBUS_SESSION_BUS_ADDRESS" in podman


def test_launcher_end_to_end_scrubs_env_sets_nnp_and_keeps_stdin(tmp_path):
    header = json.dumps({"BOT_OFFICE_AGENT_TOKEN": "agent-secret", "BOT_OFFICE_TOKEN_FILE": "/p/token",
                         "LD_PRELOAD": "x"}) + "\n"
    script = "import os,sys;print(json.dumps({'env':dict(os.environ),'cwd':os.getcwd(),'stdin':sys.stdin.read()," \
             "'nnp':open('/proc/self/status').read().split('NoNewPrivs:')[1].split()[0],'umask':os.umask(0)}))"
    argv = [sys.executable, "-I", str(RUN), "--cwd", str(tmp_path), "--env-stdin", "--",
            sys.executable, "-c", "import json;" + script]
    done = subprocess.run(argv, input=(header + "the prompt").encode(), capture_output=True, check=False,
                          env={"PATH": "/usr/bin:/bin", "SECRET_FROM_CONTROLLER": "s"})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["stdin"] == "the prompt" and out["cwd"] == str(tmp_path)
    assert out["env"]["BOT_OFFICE_AGENT_TOKEN"] == "agent-secret"
    assert not {"BOT_OFFICE_TOKEN_FILE", "LD_PRELOAD", "SECRET_FROM_CONTROLLER"} & set(out["env"])
    assert out["nnp"] == "1" and out["umask"] == 0o007
    if Path("/proc/self/status").read_text().split("NoNewPrivs:")[1].split()[0] == "0":
        argv.insert(3, "--allow-sudo")
        allowed = subprocess.run(argv, input=header.encode(), capture_output=True, check=False)
        assert json.loads(allowed.stdout)["nnp"] == "0"  # a bot Joshua allowed sudo for


def test_launcher_refuses_bad_programs_and_ids(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, "-I", str(RUN), *args], capture_output=True, check=False,
                              stdin=subprocess.DEVNULL)
    assert run("--", "env").returncode == 2  # relative program
    assert run("--", str(tmp_path)).returncode == 2  # not a file
    assert run("--stop-job", "../../etc").returncode == 2
    assert run("--which", "a/b", "/usr/bin").returncode == 2
    assert run("--which", "sh", "/bin:/usr/bin").stdout.decode().strip().startswith("/")
    assert run("--which", "no-such-cli-xyz", "/usr/bin").returncode == 1


def test_launcher_stop_job_kills_only_that_job():
    job_id = str(uuid.uuid4())
    mine = subprocess.Popen(["sleep", "60"], env={**os.environ, "BOT_OFFICE_JOB_ID": job_id})
    other = subprocess.Popen(["sleep", "60"], env={**os.environ, "BOT_OFFICE_JOB_ID": str(uuid.uuid4())})
    try:
        done = subprocess.run([sys.executable, "-I", str(RUN), "--stop-job", job_id], capture_output=True,
                              check=False, timeout=20)
        assert done.returncode == 0
        assert mine.wait(timeout=5) == -signal.SIGTERM
        assert other.poll() is None
    finally:
        for proc in (mine, other):
            proc.kill()
            proc.wait()


# --- the controller side --------------------------------------------------------------------

def test_launcher_prefix_wrap_and_header():
    launcher = isolation.Launcher(Path("/usr/local/libexec/bot-office-run"), "ubuntu")
    assert launcher.prefix() == ["/usr/bin/sudo", "-n", "-u", "ubuntu", "/usr/local/libexec/bot-office-run", "--"]
    assert launcher.prefix(allow_sudo=True, cwd="/w", env_stdin=True)[5:] == [
        "--allow-sudo", "--cwd", "/w", "--env-stdin", "--"]
    # a bare name resolves as the bots user (--which); the controller's PATH is never consulted
    launcher_which = isolation.Launcher.which
    try:
        isolation.Launcher.which = lambda self, name, search_path=None: "/home/ubuntu/.local/bin/" + name
        assert launcher.wrap(["sh", "-c", "x"])[-3:] == ["/home/ubuntu/.local/bin/sh", "-c", "x"]
        isolation.Launcher.which = lambda self, name, search_path=None: None
        assert launcher.wrap(["sh", "-c", "x"])[-3:] == ["sh", "-c", "x"]  # the launcher refuses it
    finally:
        isolation.Launcher.which = launcher_which
    assert json.loads(launcher.header({"A": "1"})) == {"A": "1"} and launcher.header({}).endswith(b"\n")


def test_layout_detection(settings, tmp_path, monkeypatch):
    mine = tmp_path / "run"
    mine.write_text("#!/bin/sh\n")
    mine.chmod(0o755)
    assert not isolation.launcher_usable(mine)  # not root-owned
    assert not isolation.launcher_usable(tmp_path / "missing")
    root_owned = Path("/usr/bin/env")
    if root_owned.stat().st_uid != 0:
        pytest.skip("no root-owned file to stand in for the launcher")
    assert isolation.launcher_usable(root_owned)
    other = dataclasses.replace(settings, launcher=root_owned, bots_user="nobody")
    assert isolation.detect(other) == isolation.Launcher(root_owned, "nobody")
    monkeypatch.setattr(isolation, "_current_user", lambda: "nobody")
    assert isolation.detect(other) is None  # the controller already is the bots user
    assert isolation.detect(dataclasses.replace(other, launcher=mine)) is None


def test_status_and_control_dir_fallback(settings, tmp_path):
    isolation.configure(settings, None)
    report = isolation.status(settings)
    assert report["isolation"] == "off" and "launcher" in report["why"]
    assert report["token_in_private_dir"] is False and report["db_in_private_dir"] is False
    private = tmp_path / "private"
    moved = dataclasses.replace(settings, private_dir=private)
    # Sec 6: a set private dir is never swapped for the shared tree, even while it is missing
    assert moved.control_dir == private and moved.db_path == private / "agentdesk.sqlite3"
    private.mkdir(mode=0o700)
    assert moved.control_dir == private and moved.db_path == private / "agentdesk.sqlite3"
    assert settings.control_dir == settings.data_dir  # the old layout


def test_incomplete_isolated_layout_fails_closed(settings, tmp_path, monkeypatch):
    """Sec 6: with BOT_OFFICE_PRIVATE_DIR set (or running as botoffice) and the layout not whole,
    no job runs and nothing falls back to the shared tree."""
    from botoffice.providers.fake import FakeAdapter
    from botoffice.scheduler import Scheduler
    from botoffice.store import JobStore

    private = tmp_path / "private"
    isolated = dataclasses.replace(settings, private_dir=private, bots_user="nobody-here")
    monkeypatch.setattr(isolation, "_current_user", lambda: "botoffice")
    good = isolation.Launcher(RUN, "nobody-here")
    assert "launcher" in isolation.check_layout(isolated, None)
    assert "missing" in isolation.check_layout(isolated, good)
    private.mkdir(mode=0o755)
    assert "0700" in isolation.check_layout(isolated, good)
    private.chmod(0o700)
    assert isolation.check_layout(isolated, good) is None
    assert "TOKEN_FILE" in isolation.check_layout(dataclasses.replace(isolated, token_file=tmp_path / "t"), good)
    assert "not set" in isolation.check_layout(dataclasses.replace(isolated, private_dir=None), good)
    monkeypatch.setattr(isolation, "_current_user", lambda: "nobody-here")
    assert "bots user" in isolation.check_layout(isolated, good)
    monkeypatch.setattr(isolation, "_current_user", lambda: "someone")
    assert isolation.check_layout(dataclasses.replace(isolated, private_dir=None), None) is None  # old layout
    # detection with no usable launcher: jobs are refused with the reason
    monkeypatch.setattr(isolation, "_current_user", lambda: "botoffice")
    monkeypatch.setattr(isolation, "launcher_usable", lambda path: False)
    try:
        isolation.configure(isolated)
        assert isolation.active() is None and "launcher" in isolation.layout_error()
        assert isolation.status(isolated)["isolation"] == "incomplete: no jobs run"
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        ran = []

        class Records(FakeAdapter):
            async def run(self, job, ws, log_path, timeout):
                ran.append(job.id)
                return await super().run(job, ws, log_path, timeout)

        scheduler = Scheduler(store, settings, {"codex": Records()})
        bot = store.create_bot("Ada", "", "codex", None)
        _, job = scheduler.turns.post_message(bot, "hi")
        asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
        done = store.get(job.id)
        assert not ran and done.status.value == "failed" and "isolation layout is incomplete" in done.error
    finally:
        isolation.configure(settings, None)
    assert isolation.layout_error() is None


def test_rotate_token_keeps_the_owner_and_says_it_is_live(settings, tmp_path, capsys, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("old\n")
    token_file.chmod(0o600)
    chowned = []
    monkeypatch.setattr(cli_main.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli_main.os, "chown", lambda path, uid, gid, **kw: chowned.append((uid, gid)))
    assert cli_main.rotate_token(dataclasses.replace(settings, token_file=token_file)) == 0
    assert chowned == [(os.getuid(), os.getgid())] and "in effect now" in capsys.readouterr().out


def test_isolation_endpoint_is_master_only(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        assert client.get("/v1/isolation", headers=H).json()["isolation"] == "off"
        assert client.get("/v1/isolation").status_code == 401


def test_cli_runs_through_the_launcher_with_env_on_stdin(settings, layout, tmp_path, monkeypatch):
    fake_cli = tmp_path / "fake-cli"
    fake_cli.write_text("#!/usr/bin/python3 -I\nimport json, os, sys\n"
                        "print(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("BOT_OFFICE_TOKEN_FILE", "/var/lib/bot-office/private/token")
    monkeypatch.setenv("CONTROLLER_ONLY", "x")
    adapter = CodexAdapter(executable=str(fake_cli))
    argv, env, header = adapter._spawn([str(fake_cli), "--version"], adapter._env())
    assert argv[:5] == [isolation.SUDO, "-n", "-u", "bots", str(RUN)] and "--env-stdin" in argv
    assert "CONTROLLER_ONLY" not in env and "CONTROLLER_ONLY" in json.loads(header)  # launcher drops it later
    code, output = asyncio.run(adapter._capture(("--version",)))
    seen = json.loads(output.strip().splitlines()[0])
    assert code == 0 and seen["argv"] == ["--version"]
    assert "CONTROLLER_ONLY" not in seen["env"] and "BOT_OFFICE_TOKEN_FILE" not in seen["env"]
    assert seen["env"]["CODEX_HOME"].endswith("/.codex")


def test_job_env_and_allow_sudo_flag(settings, layout):
    from botoffice.domain import Job

    base = {"id": str(uuid.uuid4()), "prompt": "p", "provider": "codex", "status": "running",
            "created_at": "2026-11-14T00:00:00Z"}
    job = Job(**base, agent_token="agent-secret", allow_sudo=True)
    adapter = CodexAdapter(executable="/bin/true")
    argv, env, header = adapter._spawn(["/bin/true"], adapter._env(Path("/w/ws"), job), Path("/w/ws"), job)
    assert "--allow-sudo" in argv and argv[argv.index("--cwd") + 1] == "/w/ws"
    assert "agent-secret" not in " ".join(argv) and "agent-secret" not in json.dumps(env)  # never in argv
    assert json.loads(header)["BOT_OFFICE_AGENT_TOKEN"] == "agent-secret"
    plain = Job(**base)
    assert "--allow-sudo" not in adapter._spawn(["/bin/true"], {}, Path("/w"), plain)[0]


def test_old_layout_runs_directly(settings):
    isolation.configure(settings, None)
    adapter = CodexAdapter(executable="/bin/true")
    assert adapter._spawn(["/bin/true", "x"], {"A": "1"}) == (["/bin/true", "x"], {"A": "1"}, b"")
    assert isolation.wrap(["podman", "ps"]) == ["podman", "ps"]
    assert isolation.exec_options() == ({}, b"")
    assert (isolation.dir_mode(), isolation.file_mode(), isolation.workspace_mode()) == (0o700, 0o600, 0o700)


def test_shared_modes_when_isolated(settings, layout):
    assert (isolation.dir_mode(), isolation.file_mode(), isolation.locked_file_mode(), isolation.workspace_mode()) == (
        0o2770, 0o660, 0o640, 0o3770)
    settings.ensure_dirs()
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o2750
    # sticky (+t) on the bot-writable top dirs: a bot cannot rename or delete the controller's entries
    for name in ("workspaces", "bots", "shared", "inboxes"):
        assert stat.S_IMODE((settings.data_dir / name).stat().st_mode) == 0o3770
    assert stat.S_IMODE((settings.data_dir / "logs").stat().st_mode) == 0o700


def test_stop_and_recovery_go_through_the_launcher(settings, monkeypatch):
    calls = []

    class Recording(isolation.Launcher):
        def stop_job(self, job_id, timeout=15):
            calls.append(job_id)
            return job_id == "a"

    isolation.configure(settings, Recording(RUN, "bots"))
    try:
        monkeypatch.setattr(procs, "cgroup_clean", lambda *a, **k: True)
        assert procs.stop_job_processes("a") is True and procs.stop_job_processes("b") is False
        assert procs.recovery_proof(["a", "b"]) == {"a"}
    finally:
        isolation.configure(settings, None)
    assert calls == ["a", "b", "a", "b"]


def test_podman_and_gh_are_wrapped(settings, layout, monkeypatch):
    from botoffice import desktop, vnc

    seen = []

    class Proc:
        returncode = 0

        async def communicate(self, data=None):
            seen.append(data)
            return b"[]", b""

    async def launch(*args, **kwargs):
        seen.append((args, kwargs.get("env")))
        return Proc()

    monkeypatch.setattr(desktop.asyncio, "create_subprocess_exec", launch)
    asyncio.run(desktop.run_desktop_command("desktop-1", ["ls"]))
    asyncio.run(vnc.desktops())
    for (args, env), header in ((seen[0], seen[1]), (seen[2], seen[3])):
        assert args[0] == isolation.SUDO and "--env-stdin" in args and "podman" in args[args.index("--") + 1]
        assert json.loads(header) and "BOT_OFFICE" not in json.dumps(env)


def test_rotate_token_never_prints_it_without_show(settings, tmp_path, capsys):
    token_file = tmp_path / "token"
    token_file.write_text("old\n")
    token_file.chmod(0o600)
    moved = dataclasses.replace(settings, token_file=token_file)
    assert cli_main.rotate_token(moved) == 0
    first = token_file.read_text().strip()
    out = capsys.readouterr().out
    assert first != "old" and first not in out and len(first) >= 32
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert cli_main.rotate_token(moved, show=True) == 0
    second = token_file.read_text().strip()
    assert second in capsys.readouterr().out and second != first
    assert cli_main.rotate_token(dataclasses.replace(settings, token_file=None)) == 2
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".token")]


# --- the installer --------------------------------------------------------------------------

def installer_env(tmp_path):
    return {**os.environ, "OLD_DATA": str(tmp_path / "data"), "APP_DIR": str(tmp_path / "opt"),
            "STATE_DIR": str(tmp_path / "state"), "LAUNCHER": str(tmp_path / "libexec" / "run"),
            "SUDOERS_FILE": str(tmp_path / "sudoers"), "UNIT_FILE": str(tmp_path / "unit"),
            "BOTS_USER": os.environ.get("USER") or __import__("pwd").getpwuid(os.getuid()).pw_name}


def test_install_script_dry_run_changes_nothing_and_shows_the_plan(tmp_path):
    old = tmp_path / "data"
    old.mkdir()
    (old / "token").write_text("SECRET-TOKEN-VALUE\n")
    (old / "grok_webhook.json").write_text('{"secret": "SECRET-HOOK"}')
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    done = subprocess.run(["bash", str(INSTALL)], env=installer_env(tmp_path), capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert out.startswith("DRY RUN: nothing is changed")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before and (old / "token").read_text() == "SECRET-TOKEN-VALUE\n"
    assert "SECRET-TOKEN-VALUE" not in out + done.stderr and "SECRET-HOOK" not in out + done.stderr
    assert "visudo -c" in out and "!use_pty" in out
    assert f"botoffice ALL=({installer_env(tmp_path)['BOTS_USER']}) NOPASSWD: {tmp_path / 'libexec' / 'run'}" in out
    assert "systemctl --user disable --now bot-office.service" in out  # disabled, not deleted
    assert "enable-linger" in out or "linger already enabled" in out
    assert f"cp -a {old / 'token'}" in out and f"rm -f {old / 'token'}" in out  # moved with a backup
    assert "User=botoffice" in out and "install -d -o botoffice -g botoffice -m 0700" in out
    assert "-m 3770" in out and "d:g:bo-share:rwX" in out and "chmod +t" in out  # sticky (Sec 1)
    assert "gate-capabilities.json" in INSTALL.read_text().split("PRIVATE_FILES=(")[1].split(")")[0]
    assert "would run: sudo -l -U" in out and "sha256 of src/" in out and "go-w,a+rX" in out
    assert "sudoers" not in out.split("== launcher")[0]  # nothing about ubuntu's own sudo
    again = subprocess.run(["bash", str(INSTALL)], env=installer_env(tmp_path), capture_output=True, text=True,
                           timeout=60, check=False)
    assert again.stdout.replace(again.stdout.split("backup-")[1][:15], "") == out.replace(out.split("backup-")[1][:15], "")


def test_install_script_sudo_check_refuses_nopasswd_and_records_the_result(tmp_path):
    """Sec 2: --apply refuses while the bots user has sudo without a password; the result the
    GUI claim rests on is written to the private dir."""
    listing = tmp_path / "listing"
    env = {**installer_env(tmp_path), "BO_TEST_SUDO_LIST": str(listing)}
    listing.write_text("User u may run the following commands on h:\n    (ALL : ALL) NOPASSWD: ALL\n")
    done = subprocess.run(["bash", str(INSTALL)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0 and "REFUSED" in done.stderr and "--apply would REFUSE" in done.stdout
    assert "Decide one of" in done.stderr and "result=nopasswd" in done.stdout
    listing.write_text("User u may run the following commands on h:\n    (ALL : ALL) ALL\n")
    done = subprocess.run(["bash", str(INSTALL)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert "REFUSED" not in done.stderr and "result=passed" in done.stdout
    assert f"{tmp_path / 'state' / 'private' / 'sudo-check'} (0600" in done.stdout
    text = INSTALL.read_text()
    assert 'listing="$(sudo -n -l -U "$BOTS_USER"' in text and "never honoured by --apply" in text


def test_install_script_moves_controller_state_out_of_the_shared_tree(tmp_path):
    """Sec 1: session pointers, the handoff marker and snapshots move to private/bot-state; a
    link planted in their place is never moved or chowned."""
    bot = tmp_path / "state" / "data" / "bots" / "b1"
    bot.mkdir(parents=True)
    for name in ("codex-session", "claude-session.reset-1", ".handoff-pending", "self.sha256", "rules.sha256"):
        (bot / name).write_text("x")
    (bot / "memory.md").write_text("m")
    (bot / "opencode-session").symlink_to("/etc/shadow")
    done = subprocess.run(["bash", str(INSTALL)], env=installer_env(tmp_path), capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    target = tmp_path / "state" / "private" / "bot-state" / "b1"
    for name in ("codex-session", "claude-session.reset-1", ".handoff-pending", "self.sha256", "rules.sha256"):
        assert f"would run: mv -T {bot / name} {target / name}" in done.stdout
    assert "opencode-session" not in done.stdout and "memory.md" not in done.stdout
    assert (bot / "codex-session").exists()  # a dry run


def test_install_script_apply_and_rollback_need_root(tmp_path):
    if os.getuid() == 0:
        pytest.skip("running as root")
    for flag in ("--apply", "--rollback"):
        done = subprocess.run(["bash", str(INSTALL), flag], env=installer_env(tmp_path), capture_output=True,
                              text=True, timeout=30, check=False)
        assert done.returncode == 1 and "needs root" in done.stderr
    dry = subprocess.run(["bash", str(INSTALL), "--dry-run-rollback"], env=installer_env(tmp_path),
                         capture_output=True, text=True, timeout=30, check=False)
    assert dry.returncode == 0 and "would run: rm -f" in dry.stdout and "visudo -c" in dry.stdout
    assert "systemctl --user enable --now bot-office.service" in dry.stdout
    assert subprocess.run(["bash", str(INSTALL), "--bogus"], capture_output=True, check=False).returncode == 2


def test_bots_path_and_gh_resolve_as_the_bots_user(settings, layout, tmp_path, monkeypatch):
    """Sec 3 / gen 7: the controller's PATH holds no bots-writable dir; bot CLIs and gh resolve
    on BOT_OFFICE_BOTS_PATH through the launcher's --which, and the bot env gets that PATH."""
    from botoffice import watchers
    from botoffice.providers.codex import CodexAdapter

    bots_bin = tmp_path / "bots-bin"
    bots_bin.mkdir()
    for name in ("gh", "codex"):
        (bots_bin / name).write_text("#!/bin/sh\necho '[]'\n")
        (bots_bin / name).chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("BOT_OFFICE_BOTS_PATH", f"{bots_bin}:/usr/bin:/bin")
    assert isolation.bots_path() == f"{bots_bin}:/usr/bin:/bin"
    assert CodexAdapter().executable == str(bots_bin / "codex")
    assert layout.wrap(["gh", "api", "x"])[-3] == str(bots_bin / "gh")
    env = CodexAdapter()._env(tmp_path / "data" / "workspaces" / "j", None)
    assert env["PATH"].split(":")[1] == str(bots_bin)
    _, header = isolation.run_env({"PATH": "/usr/bin"})
    assert json.loads(header)["PATH"] == isolation.bots_path()
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, b"[]", b"")

    monkeypatch.setattr(watchers.subprocess, "run", fake_run)
    monkeypatch.setattr(watchers.shutil, "which", lambda name: pytest.fail("gh resolved on the controller's PATH"))
    watchers.Watchers.run_poll(object.__new__(watchers.Watchers), ["gh", "api", "repos/a/b/issues"])
    assert seen and seen[-1][seen[-1].index("--") + 1] == str(bots_bin / "gh")


def test_unit_path_holds_no_bots_dir():
    unit = (ROOT / "deploy" / "bot-office-system.service").read_text()
    path = next(line for line in unit.splitlines() if line.startswith("Environment=PATH="))
    assert "/home/" not in path and "BOT_OFFICE_BOTS_PATH=/home/ubuntu/.local/bin" in unit


def test_launcher_keeps_every_variable_the_cli_adapters_set():
    """Review C2 #1: each `env["KEY"] = ...` in providers/cli.py (and the adapters' _auth_env)
    must survive the launcher. Deliberately not passed through as sent: HOME (the bots user's
    home, or the confined workspace) and PATH (absolute entries only) - checked above."""
    import re as _re
    keys = set()
    for name in ("cli.py", "codex.py"):
        source = (ROOT / "src" / "botoffice" / "providers" / name).read_text()
        keys |= set(_re.findall(r'env\["([A-Z][A-Z0-9_]*)"\]\s*=', source))
        keys |= set(_re.findall(r'return \{"([A-Z][A-Z0-9_]*)":', source))
    rewritten = {"HOME", "PATH"}
    assert "BOT_OFFICE_WORKSPACE" in keys and "CODEX_HOME" in keys
    sent = {key: f"/home/bots/.v-{key.lower()}" for key in keys - rewritten}
    env = LAUNCHER["build_env"](sent, user(), "/w")
    assert {key for key in sent if key not in env} == set()
    assert LAUNCHER["build_env"]({"ANTHROPIC_API_KEY": "k"}, user(), "/w")["ANTHROPIC_API_KEY"] == "k"  # opencode
