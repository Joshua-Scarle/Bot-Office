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
