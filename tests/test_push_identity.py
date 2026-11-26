"""sec-fixes: every commit pushed must carry noreply author and committer emails (check_denylist.sh
--identities, run by the pre-push hook and sync_github.sh); a personal address is refused, never shown."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_denylist.sh"
PERSONAL = "pat.example.person@example.test"  # fake, not on the noreply allowlist; only ever in a temp repo


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


def commit(repo, name, author, committer=None, env=None):
    (repo / name).write_text(name + "\n")
    git(repo, "add", name)
    ident = {"GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_NAME": "C",
             "GIT_COMMITTER_EMAIL": committer or author}
    git(repo, "commit", "-qm", name, env={**os.environ, **(env or {}), **ident})
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "core.hooksPath", "/dev/null")
    commit(r, "base", "bot-office@users.noreply.github.com")
    return r


def check(repo, *revs, extra=None):
    env = {**os.environ, "BO_PUSH_IDENTITY_ALLOWLIST": str(extra or repo / "none.txt"),
           "BO_PUSH_DENYLIST": str(repo / "absent-denylist.txt")}  # --identities needs no denylist
    return subprocess.run([str(SCRIPT), "--identities", *revs], cwd=repo, capture_output=True, text=True, env=env)


@pytest.mark.parametrize("email", ["bot-office@users.noreply.github.com", "12345+some-login@users.noreply.github.com",
                                   "41898282+github-actions[bot]@users.noreply.github.com", "noreply@github.com",
                                   "noreply@anthropic.com", "Bot-Office@Users.Noreply.GitHub.com",
                                   "bot-office-engineer-8@local", "bot-office-lead@local", "e7@botoffice.local"])
def test_noreply_and_existing_bot_identities_pass(repo, email):
    commit(repo, "x", email)
    assert check(repo, "HEAD").returncode == 0


@pytest.mark.parametrize("author,committer,field", [
    (PERSONAL, "noreply@github.com", "author"), ("noreply@anthropic.com", PERSONAL, "committer"),
    ("someone@example.com", "someone@example.com", "author"), ("evil@users.noreply.github.com.example.com", None, "author"),
    ("bot-office@local.example.com", None, "author"), ("x@anthropic.com", None, "author"),
])
def test_any_other_email_is_refused_and_never_printed(repo, author, committer, field):
    sha = commit(repo, "x", author, committer)
    done = check(repo, "HEAD")
    assert done.returncode == 1
    assert sha[:10] in done.stderr and f"the {field} email" in done.stderr
    assert author not in done.stdout + done.stderr and PERSONAL not in done.stdout + done.stderr


def test_only_the_given_range_is_checked(repo):
    commit(repo, "old", PERSONAL)
    old = git(repo, "rev-parse", "HEAD").stdout.strip()
    commit(repo, "new", "noreply@anthropic.com")
    assert check(repo, "HEAD", "--not", old).returncode == 0
    assert check(repo, "HEAD").returncode == 1


def test_an_extra_exact_address_can_be_allowed_outside_the_repo(repo, tmp_path):
    commit(repo, "x", "ci-robot@example.org")
    extra = tmp_path / "allow.txt"
    extra.write_text("# extra\n  CI-Robot@example.org  \n")
    assert check(repo, "HEAD", extra=extra).returncode == 0
    extra.write_text("robot@example.org\n")  # exact match only
    assert check(repo, "HEAD", extra=extra).returncode == 1
    extra.write_text("ci- robot@example.org\n")  # inner whitespace is not removed
    assert check(repo, "HEAD", extra=extra).returncode == 1


def test_an_annotated_tag_with_a_personal_tagger_is_refused(repo):
    env = {**os.environ, "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": PERSONAL}
    git(repo, "tag", "-a", "v1", "-m", "v1", env=env)
    done = check(repo, "v1")
    assert done.returncode == 1 and "tagger" in done.stderr and PERSONAL not in done.stderr
    git(repo, "tag", "-a", "v2", "-m", "v2", env={**os.environ, "GIT_COMMITTER_NAME": "T",
                                                  "GIT_COMMITTER_EMAIL": "noreply@anthropic.com"})
    assert check(repo, "v2").returncode == 0


def test_a_bad_range_fails_closed(repo):
    assert check(repo, "no-such-ref").returncode == 3


def test_the_repo_names_no_personal_address():
    for path in ("scripts/check_denylist.sh", "scripts/git-hooks/pre-push", "scripts/sync_github.sh",
                 "src/botoffice/auto_merge.py"):
        text = (ROOT / path).read_text(encoding="utf-8").lower()
        assert "gmail" not in text and "@googlemail" not in text, path


def _hooked_clone(tmp_path):
    """A scratch repo whose pre-push is the real hook, an origin that is a local bare repo, a fake gitleaks."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(remote))
    hooks = work / ".git" / "hooks"
    shutil.copy(ROOT / "scripts" / "git-hooks" / "pre-push", hooks / "pre-push")
    shutil.copy(SCRIPT, hooks / "bo-check-denylist")
    for name in ("pre-push", "bo-check-denylist"):
        (hooks / name).chmod(0o755)
    gitleaks = tmp_path / "gitleaks"
    gitleaks.write_text("#!/bin/sh\nexit 0\n")
    gitleaks.chmod(0o755)
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("Pat Example-Person\n")
    env = {"BO_ALLOW_PUSH": "1", "GITLEAKS": str(gitleaks), "BO_PUSH_DENYLIST": str(denylist),
           "BO_PUSH_IDENTITY_ALLOWLIST": str(tmp_path / "none.txt")}
    commit(work, "base", "bot-office@users.noreply.github.com")
    return work, remote, env


def push(work, env):
    return subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main:refs/heads/main"],
                          capture_output=True, text=True, env={**os.environ, **env})


def test_the_pre_push_hook_rejects_a_personal_email_and_lets_noreply_through(tmp_path):
    work, remote, env = _hooked_clone(tmp_path)
    assert push(work, env).returncode == 0
    good = git(work, "rev-parse", "HEAD").stdout.strip()
    commit(work, "leak", PERSONAL, "noreply@github.com")
    done = push(work, env)
    assert done.returncode != 0 and "not a noreply address" in done.stderr
    assert PERSONAL not in done.stdout + done.stderr
    assert git(remote, "rev-parse", "main").stdout.strip() == good  # nothing was published
    git(work, "reset", "-q", "--hard", good)
    commit(work, "fine", "noreply@anthropic.com", "bot-office@users.noreply.github.com")
    assert push(work, env).returncode == 0


def test_a_tagger_line_in_the_tag_message_does_not_hide_the_real_tagger(repo):
    env = {**os.environ, "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": PERSONAL}
    git(repo, "tag", "-a", "v3", "-m", "x\n\ntagger Fake <noreply@github.com> 1 +0000", env=env)
    assert check(repo, "v3").returncode == 1


def test_an_ambiguous_tagger_line_is_refused(repo):
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    raw = (f"object {head}\ntype commit\ntag v9\n"
           f"tagger Name <{PERSONAL}> <noreply@github.com> 1 +0000\n\nmsg\n")
    obj = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "tag", "-w", "--literally", "--stdin"],
                         input=raw, capture_output=True, text=True, check=True).stdout.strip()
    done = check(repo, obj)
    assert done.returncode == 1 and "malformed or ambiguous" in done.stderr and PERSONAL not in done.stderr


def test_replace_refs_cannot_disguise_a_personal_commit(repo):
    bad = commit(repo, "leak", PERSONAL)
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    good = commit(repo, "clean", "noreply@anthropic.com")
    git(repo, "replace", bad, good)  # git log of bad would now show the clean identity
    assert check(repo, bad).returncode == 1


def test_every_tag_in_a_chain_is_checked(repo):
    personal = {**os.environ, "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": PERSONAL}
    good = {**os.environ, "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "noreply@anthropic.com"}
    git(repo, "tag", "-a", "inner", "-m", "inner", env=personal)
    git(repo, "tag", "-a", "outer", "-m", "outer", "inner", env=good)
    done = check(repo, "outer")
    assert done.returncode == 1 and PERSONAL not in done.stderr


def test_a_stale_origin_tracking_ref_does_not_hide_a_personal_commit(tmp_path):
    work, remote, env = _hooked_clone(tmp_path)
    assert push(work, env).returncode == 0
    good = git(work, "rev-parse", "HEAD").stdout.strip()
    leak = commit(work, "leak", PERSONAL)
    git(work, "update-ref", "refs/remotes/origin/old-branch", leak)  # a cached ref the remote no longer has
    done = push(work, env)
    assert done.returncode != 0 and "not a noreply address" in done.stderr
    assert git(remote, "rev-parse", "main").stdout.strip() == good


def test_the_pre_push_hook_still_refuses_without_the_push_switch(tmp_path):
    work, _remote, env = _hooked_clone(tmp_path)
    assert push(work, {**env, "BO_ALLOW_PUSH": "0"}).returncode != 0
