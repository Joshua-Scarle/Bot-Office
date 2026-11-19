"""0.11.10 public hygiene: no owner name or internal plan references in what this release adds, and
the e2e script takes its live token from a file or the environment rather than the command line."""
import argparse
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "d1d2192"  # 0.11.9
INTERNAL = re.compile(r"Joshua|Engineer \d|\bG\d{1,2}\b|\bplan (?:\d|G\d|P\d)|\bP\d Q\d|review fix \d|\bfix \d+\b")


def _added_lines() -> list[str]:
    try:
        diff = subprocess.run(["git", "diff", "-U0", BASE, "--", "src", "bin", "scripts", "README.md", "docs",
                               ":(exclude)docs/archive", ":(exclude)docs/plans"],
                              cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no git history for the 0.11.9 base")
    lines, name = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[6:]
        elif line.startswith("+"):
            lines.append(f"{name}: {line[1:]}")
    return lines


def test_added_lines_name_no_owner_and_no_internal_plan_references():
    bad = [line for line in _added_lines() if INTERNAL.search(line)]
    assert bad == []


def test_the_bot_prompt_says_the_owner_not_a_name():
    text = (ROOT / "src/botoffice/workspace.py").read_text(encoding="utf-8")
    assert "Ask the owner with `desk ask`" in text and "Ask Joshua" not in text


def _e2e():
    spec = importlib.util.spec_from_file_location("e2e_gui", ROOT / "scripts/e2e_gui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(token=None, token_file=None):
    return argparse.Namespace(token=token, token_file=token_file)


def test_e2e_token_from_a_file_or_the_environment_and_token_flag_deprecated(tmp_path, capsys):
    e2e = _e2e()
    secret = tmp_path / "token"
    secret.write_text("from-file\n", encoding="utf-8")
    other = tmp_path / "env-token"
    other.write_text("from-env-file", encoding="utf-8")
    assert e2e.resolve_token(_args(token="flag", token_file=str(secret)), {"BOT_OFFICE_TOKEN": "env"}) == "from-file"
    assert e2e.resolve_token(_args(), {"BOT_OFFICE_TOKEN_FILE": str(other), "BOT_OFFICE_TOKEN": "env"}) == "from-env-file"
    assert e2e.resolve_token(_args(token="flag"), {"BOT_OFFICE_TOKEN": "env"}) == "env"
    assert capsys.readouterr().err == ""
    assert e2e.resolve_token(_args(token="flag"), {}) == "flag"
    assert "--token is deprecated" in capsys.readouterr().err
    assert e2e.resolve_token(_args(), {}) is None
    doc = e2e.__doc__
    assert "--token-file" in doc and '--token "$(cat' not in doc
