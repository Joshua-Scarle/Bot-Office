"""scripts/check_denylist.sh: blocks denylisted values and forbidden paths, never prints the value, fails closed."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_denylist.sh"
SECRET = "Pat Example-Person"  # fake value; only ever lives in the temp denylist


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "test@example.test")
    git(r, "config", "core.hooksPath", "/dev/null")
    (r / "a.txt").write_text("hello\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-qm", "base")
    dl = tmp_path / "denylist.txt"
    dl.write_text("# comment line\n\n" + SECRET + "\n")
    return r, dl


def run(repo, dl, *args):
    env = {**os.environ, "BO_PUSH_DENYLIST": str(dl)}
    return subprocess.run([str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, env=env)


def test_clean_staged_and_commits_pass(repo):
    r, dl = repo
    (r / "b.txt").write_text("nothing personal\n")
    git(r, "add", "b.txt")
    assert run(r, dl, "--staged").returncode == 0
    assert run(r, dl, "--commits", "HEAD").returncode == 0


def test_staged_value_is_blocked_case_insensitively_and_not_echoed(repo):
    r, dl = repo
    (r / "b.txt").write_text("contact " + SECRET.upper() + "\n")
    git(r, "add", "b.txt")
    p = run(r, dl, "--staged")
    assert p.returncode == 1
    assert "b.txt" in p.stderr
    assert SECRET.lower() not in (p.stdout + p.stderr).lower()


@pytest.mark.parametrize("path", ["policy-pack/RULES.md", "tests/fixtures/installed-RULES-2030-01-01.md", "data/shared/x.md"])
def test_forbidden_paths_are_blocked_whatever_their_content(repo, path):
    r, dl = repo
    f = r / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("harmless\n")
    git(r, "add", "-f", path)
    assert run(r, dl, "--staged").returncode == 1
    git(r, "commit", "-qm", "add")
    p = run(r, dl, "--commits", "HEAD~1..HEAD")
    assert p.returncode == 1 and "forbidden path" in p.stderr


def test_commit_message_and_identity_are_checked(repo):
    r, dl = repo
    git(r, "commit", "-q", "--allow-empty", "-m", "thanks " + SECRET)
    p = run(r, dl, "--commits", "HEAD~1..HEAD")
    assert p.returncode == 1 and "message" in p.stderr and SECRET not in p.stderr
    git(r, "commit", "-q", "--allow-empty", "-m", "fine", "--author", SECRET + " <x@example.test>")
    p = run(r, dl, "--commits", "HEAD~1..HEAD")
    assert p.returncode == 1 and "identity" in p.stderr
    msg = r / "msg.txt"
    msg.write_text("hello " + SECRET + "\n")
    assert run(r, dl, "--message", str(msg)).returncode == 1
    msg.write_text("hello\n# " + SECRET + " in a git comment line is ignored\n")
    assert run(r, dl, "--message", str(msg)).returncode == 0


def test_added_lines_in_history_are_found_but_removals_are_not(repo):
    r, dl = repo
    (r / "a.txt").write_text("hello\n" + SECRET + "\n")
    git(r, "commit", "-qam", "add")
    (r / "a.txt").write_text("hello\n")
    git(r, "commit", "-qam", "remove")
    assert run(r, dl, "--commits", "HEAD~1..HEAD").returncode == 0  # the removal publishes nothing new
    p = run(r, dl, "--commits", "HEAD~2..HEAD")
    assert p.returncode == 1 and "a.txt adds" in p.stderr


def test_missing_or_empty_denylist_fails_closed(repo, tmp_path):
    r, _ = repo
    assert run(r, tmp_path / "nope.txt", "--commits", "HEAD").returncode == 3
    empty = tmp_path / "empty.txt"
    empty.write_text("# only comments\n\n")
    assert run(r, empty, "--staged").returncode == 3
