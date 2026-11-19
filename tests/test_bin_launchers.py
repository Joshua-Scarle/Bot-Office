"""bin/ scripts that import botoffice re-exec under the app venv, whichever python started them."""
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
LAUNCHERS = {"desk": "desk", "bot-office-ask": "ask", "bot-office-attach": "attach", "bot-office-say": "say"}
SYSTEM_PYTHON = "/usr/bin/python3"
MARKER = "BOT_OFFICE_VENV_REEXEC"

needs_system_python = pytest.mark.skipif(not Path(SYSTEM_PYTHON).exists(), reason="needs a system python")


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != MARKER and not k.startswith("PYTHON")}
    env.update(extra)
    return env


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """A throwaway app root: the real launcher files, a real venv, and stub botoffice modules.

    Each stub main() reports the interpreter and marker it ran with, and starts the nested
    launcher named in NEST under the system python.
    """
    root = tmp_path_factory.mktemp("app")
    (root / "bin").mkdir()
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root / ".venv")
    pkg = root / "src" / "botoffice"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    stub = (
        "import os, subprocess, sys\n"
        "def main(argv=None):\n"
        "    print('RAN', sys.prefix, repr(os.environ.get('BOT_OFFICE_VENV_REEXEC')), flush=True)\n"
        "    nest = os.environ.get('NEST')\n"
        "    if nest:\n"
        "        env = dict(os.environ); env.pop('NEST')\n"
        f"        subprocess.run(['{SYSTEM_PYTHON}', nest, 'x'], check=True, env=env)\n"
        "    return 0\n"
    )
    (pkg / "outgoing_images.py").write_text(stub.replace("def main(argv=None):", "def attach_file(path):") + "    return path\n")
    for name, module in LAUNCHERS.items():
        (pkg / f"{module}.py").write_text(stub)
        (root / "bin" / name).write_text((BIN / name).read_text())
    return root


def _ran(done, app):
    assert done.returncode == 0, done.stderr
    prefix = str((app / ".venv").resolve())
    return [line.split(" ", 2)[1:] for line in done.stdout.splitlines() if line.startswith("RAN ")], prefix


@needs_system_python
@pytest.mark.parametrize("name", LAUNCHERS)
def test_runs_under_the_venv_interpreter_from_system_python(name, app):
    done = subprocess.run([SYSTEM_PYTHON, str(app / "bin" / name), "x"], capture_output=True, text=True, timeout=60, env=_clean_env())
    runs, prefix = _ran(done, app)
    assert runs == [[prefix, "None"]]  # one run, in the venv, marker cleared


@needs_system_python
@pytest.mark.parametrize("name", LAUNCHERS)
@pytest.mark.parametrize("inherited", ["1", "99999999"])
def test_inherited_marker_does_not_block_reexec(name, inherited, app):
    done = subprocess.run([SYSTEM_PYTHON, str(app / "bin" / name), "x"], capture_output=True, text=True, timeout=60, env=_clean_env(**{MARKER: inherited}))
    runs, prefix = _ran(done, app)
    assert runs == [[prefix, "None"]]


@needs_system_python
@pytest.mark.parametrize("name", LAUNCHERS)
def test_nested_launcher_under_system_python_reexecs(name, app):
    done = subprocess.run([SYSTEM_PYTHON, str(app / "bin" / name), "x"], capture_output=True, text=True, timeout=60,
                          env=_clean_env(NEST=str(app / "bin" / name)))
    runs, prefix = _ran(done, app)
    assert runs == [[prefix, "None"], [prefix, "None"]]  # outer and nested both in the venv


@needs_system_python
@pytest.mark.parametrize("name", LAUNCHERS)
def test_a_launcher_starting_another_nested(name, app):
    other = "bot-office-say" if name != "bot-office-say" else "desk"
    done = subprocess.run([SYSTEM_PYTHON, str(app / "bin" / name), "x"], capture_output=True, text=True, timeout=60,
                          env=_clean_env(NEST=str(app / "bin" / other)))
    runs, prefix = _ran(done, app)
    assert runs == [[prefix, "None"], [prefix, "None"]]


@needs_system_python
def test_broken_venv_python_falls_through_without_leaving_the_marker(tmp_path, app):
    root = tmp_path / "app"
    (root / "bin").mkdir(parents=True)
    shutil_copy = (app / "bin" / "desk").read_text()
    (root / "bin" / "desk").write_text(shutil_copy)
    (root / "src").mkdir()
    (root / "src" / "botoffice").mkdir()
    (root / "src" / "botoffice" / "__init__.py").write_text("")
    (root / "src" / "botoffice" / "desk.py").write_text(
        "import os\ndef main():\n    print('RAN', repr(os.environ.get('BOT_OFFICE_VENV_REEXEC')))\n    return 0\n")
    (root / ".venv" / "bin").mkdir(parents=True)
    bad = root / ".venv" / "bin" / "python"
    bad.write_text("not an executable\n")
    bad.chmod(0o755)  # exists, but exec fails (ENOEXEC)
    done = subprocess.run([SYSTEM_PYTHON, str(root / "bin" / "desk"), "x"], capture_output=True, text=True, timeout=60, env=_clean_env())
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "RAN None"


@pytest.mark.skipif(not (BIN.parent / ".venv" / "bin" / "python").exists() or not Path(SYSTEM_PYTHON).exists(),
                    reason="needs .venv and system python")
@pytest.mark.parametrize("name,expected", [
    ("desk", "desk: no desktop selected"),
    ("bot-office-ask", "are required"),
    ("bot-office-say", "are required"),
    ("bot-office-attach", "usage: bot-office-attach"),
])
def test_real_launchers_reach_botoffice_from_system_python(name, expected, tmp_path):
    """The repo's own launchers import botoffice (so the venv was used) and print their own message."""
    link = tmp_path / name
    link.symlink_to(BIN / name)
    args = [] if name != "desk" else ["--help"]
    env = {k: v for k, v in _clean_env().items() if not k.startswith("BOT_OFFICE")}
    done = subprocess.run([SYSTEM_PYTHON, str(link), *args], capture_output=True, text=True, timeout=60, env=env)
    assert "ModuleNotFoundError" not in done.stderr and "No module named" not in done.stderr, done.stderr
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert expected in done.stderr, done.stderr
