"""scripts/svc.sh: sets XDG_RUNTIME_DIR itself, matches the listener to MainPID, guards restarts."""
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "svc.sh"


def _fake_bin(tmp_path, main_pid, listener_pid, restarted, service_env=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(f"""#!/bin/sh
[ -n "$XDG_RUNTIME_DIR" ] || {{ echo 'Failed to connect to bus' >&2; exit 1; }}
case "$*" in
  *"show"*MainPID*) echo {main_pid} ;;
  *"show"*Environment*) echo '{service_env}' ;;
  *"show"*) printf 'active\\nrunning\\n' ;;
  *"is-enabled"*) echo enabled ;;
  *"restart"*) echo restarted >> {restarted} ;;
esac
exit 0
""")
    (bin_dir / "ss").write_text(f"""#!/bin/sh
echo 'LISTEN 0 2048 127.0.0.1:8787 0.0.0.0:* users:(("python",pid={listener_pid},fd=6))'
""")
    (bin_dir / "loginctl").write_text("#!/bin/sh\necho yes\n")
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    return bin_dir


def _run(tmp_path, *args, main_pid=111, listener_pid=111, running=0, xdg=True, layout="data", env_vars=None):
    # layout: which dir the SERVICE uses ("data" = BOT_OFFICE_DATA_DIR, "private" = BOT_OFFICE_PRIVATE_DIR,
    # "none" = nothing in its environment); the caller shell's env never decides
    data = tmp_path / ("private" if layout == "private" else "data")
    data.mkdir(exist_ok=True)
    service_env = {"data": f"BOT_OFFICE_DATA_DIR={data}", "private": f"BOT_OFFICE_PRIVATE_DIR={data}", "none": "FOO=1"}[layout]
    db = sqlite3.connect(data / "agentdesk.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT, status TEXT)")
    db.execute("DELETE FROM jobs")
    db.executemany("INSERT INTO jobs VALUES (?, 'running')", [(str(i),) for i in range(running)])
    db.commit()
    db.close()
    restarted = tmp_path / "restarted"
    bin_dir = _fake_bin(tmp_path, main_pid, listener_pid, restarted, service_env) if not (tmp_path / "bin").exists() else tmp_path / "bin"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **(env_vars or {})}
    if xdg:
        env["XDG_RUNTIME_DIR"] = str(tmp_path)
    done = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return done, restarted


def test_status_ok_when_listener_is_mainpid(tmp_path):
    done, _ = _run(tmp_path, "status")
    assert done.returncode == 0, done.stderr
    assert "RESULT: OK" in done.stdout and "MainPID:  111" in done.stdout and "linger:   yes" in done.stdout


def test_status_flags_a_listener_outside_the_unit(tmp_path):
    done, _ = _run(tmp_path, "status", main_pid=111, listener_pid=os.getpid())
    assert done.returncode == 2 and "MISMATCH" in done.stderr


def test_restart_refuses_while_jobs_run_unless_forced(tmp_path):
    done, restarted = _run(tmp_path, "restart", running=2)
    assert done.returncode == 4 and "running jobs: 2" in done.stderr and not restarted.exists()
    done, restarted = _run(tmp_path, "restart", "--force", running=2)
    assert restarted.exists() and "RESULT: OK" in done.stdout


@pytest.mark.skipif(not Path(f"/run/user/{os.getuid()}").is_dir(), reason="needs /run/user/<uid>")
def test_supplies_xdg_runtime_dir_when_the_shell_has_none(tmp_path):
    # a non-login ssh shell: no XDG_RUNTIME_DIR; the fake systemctl fails like the real one without it
    done, _ = _run(tmp_path, "status", xdg=False)
    assert done.returncode == 0 and "RESULT: OK" in done.stdout, done.stderr


def test_restart_reads_the_services_private_dir_not_the_callers_shell(tmp_path):
    # the service runs with BOT_OFFICE_PRIVATE_DIR (jobs running); the caller's shell points at an idle db
    idle = tmp_path / "idle"
    idle.mkdir()
    db = sqlite3.connect(idle / "agentdesk.sqlite3")
    db.execute("CREATE TABLE jobs (id TEXT, status TEXT)")
    db.commit()
    db.close()
    done, restarted = _run(tmp_path, "restart", running=1, layout="private", env_vars={"BOT_OFFICE_DATA_DIR": str(idle)})
    assert done.returncode == 4 and "running jobs: 1" in done.stderr and not restarted.exists()


def test_restart_fails_closed_when_the_services_db_is_unknown(tmp_path):
    done, restarted = _run(tmp_path, "restart", layout="none")
    assert done.returncode == 5 and "cannot identify" in done.stderr and not restarted.exists()

