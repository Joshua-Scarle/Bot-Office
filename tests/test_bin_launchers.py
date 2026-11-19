"""bin/ scripts that import botoffice re-exec under the app venv, whichever python started them."""
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
VENV_PYTHON = BIN.parent / ".venv" / "bin" / "python"


@pytest.mark.skipif(not VENV_PYTHON.exists() or not Path("/usr/bin/python3").exists(), reason="needs .venv and system python")
@pytest.mark.parametrize("script", ["desk", "bot-office-attach"])
def test_runs_from_system_python_and_via_symlink(script, tmp_path):
    link = tmp_path / script
    link.symlink_to(BIN / script)
    args = ["--help"] if script == "desk" else []
    done = subprocess.run(["/usr/bin/python3", str(link), *args], capture_output=True, text=True, timeout=60)
    assert "ModuleNotFoundError" not in done.stderr
    assert "No module named" not in done.stderr


def test_scripts_carry_the_reexec_guard():
    for name in ("desk", "bot-office-ask", "bot-office-attach", "bot-office-say"):
        assert "BOT_OFFICE_VENV_REEXEC" in (BIN / name).read_text()


def _guard_source(name):
    text = (BIN / name).read_text()
    return text[: text.index("sys.path.insert(0")]


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="needs a system python")
@pytest.mark.parametrize("name", ["desk", "bot-office-ask", "bot-office-attach", "bot-office-say"])
def test_nested_launch_still_reexecs(name, tmp_path):
    """The loop guard is dropped once in the venv, so a child started under a non-venv python re-execs too."""
    import os
    import venv

    root = tmp_path / "app"
    (root / "bin").mkdir(parents=True)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root / ".venv")
    probe = root / "bin" / "probe"
    probe.write_text(
        "#!/usr/bin/env python3\n" + _guard_source(name)
        + "import subprocess\n"
        "print('prefix', sys.prefix, 'guard', os.environ.get('BOT_OFFICE_VENV_REEXEC'), flush=True)\n"
        "if len(sys.argv) < 2:\n"
        "    subprocess.run(['/usr/bin/python3', __file__, 'nested'], check=True)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "BOT_OFFICE_VENV_REEXEC"}
    done = subprocess.run(["/usr/bin/python3", str(probe)], capture_output=True, text=True, timeout=60, env=env)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 2, done.stdout
    for line in lines:  # outer and nested: both ran inside the venv, guard cleared
        assert line == f"prefix {(root / '.venv').resolve()} guard None" or line == f"prefix {root / '.venv'} guard None", line
