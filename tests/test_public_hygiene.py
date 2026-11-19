"""0.11.10 public hygiene: no owner name or internal plan references in what this release adds, and
the e2e script takes its live token from a file or the environment rather than the command line."""
import argparse
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "d1d2192"  # 0.11.9
# owner-specific names (the owner, the deploy path, the owner's own bots) and internal plan references
INTERNAL = re.compile(r"Joshua|printpilot-agentdesk|/home/\w|Printer Writing Bot|Bot-Office Lead|"
                      r"Engineer \d|\bG\d{1,2}\b|\bplan (?:\d|G\d|P\d)|\bP\d Q\d|review fix \d|\bfix \d+\b")
# set to 1 only where the source has no git history (a tarball build); CI and a checkout must scan
NO_GIT_OPT_OUT = "BOT_OFFICE_HYGIENE_WITHOUT_GIT"


def _added_lines() -> list[str]:
    try:
        diff = subprocess.run(["git", "diff", "-U0", BASE, "--", "src", "bin", "scripts", "README.md", "docs", "tests",
                               ":(exclude)docs/archive", ":(exclude)docs/plans",
                               ":(exclude)tests/test_public_hygiene.py"],  # this file names what it looks for
                              cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        if os.environ.get(NO_GIT_OPT_OUT) == "1":
            pytest.skip(f"no git history for the 0.11.9 base ({NO_GIT_OPT_OUT}=1)")
        pytest.fail(f"cannot diff against the 0.11.9 base ({exc}); set {NO_GIT_OPT_OUT}=1 only for a build without git")
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


def test_the_untrusted_helper_and_the_rollback_line_name_no_owner():
    source = (ROOT / "src/botoffice/assignments.py").read_text(encoding="utf-8")
    start = source.index("def untrusted(")
    helper = source[start:source.index("\n\n", start)]
    assert "Joshua" not in helper and " his " not in helper and "the owner" in helper
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rollback = next(line for line in readme.splitlines() if "steer_status='delivered'" in line and "sqlite3" in line)
    assert rollback.startswith("sqlite3 data/agentdesk.sqlite3 ")


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
