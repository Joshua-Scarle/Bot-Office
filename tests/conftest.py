import os
from pathlib import Path

import pytest

from botoffice.config import Settings


@pytest.fixture(autouse=True)
def _allow_fake_voice(monkeypatch):
    # the worker's fake voice engine refuses to run unless the tests opt in
    monkeypatch.setenv("BOT_OFFICE_VOICE_FAKE", "1")


def pytest_configure(config):
    config.addinivalue_line("markers", "continuation_defaults: run with the shipped evidence default (on)")


@pytest.fixture(autouse=True)
def _done_without_evidence(request, monkeypatch):
    """0.11.10 makes a task's done need evidence by default. Tests written before it exercise the loop,
    verify turns and status lines, not the evidence gate, so they run with it off; tests/test_continuation.py
    (marked continuation_defaults) runs with the shipped default."""
    if request.node.get_closest_marker("continuation_defaults") is None:
        monkeypatch.setenv("BOT_OFFICE_DONE_NEEDS_EVIDENCE", "off")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", token="test-token", job_timeout=2)


@pytest.fixture(autouse=True)
def _fake_host_cgroup(monkeypatch, tmp_path_factory):
    """BOT_OFFICE_TEST_HOST_CGROUP=scope|dirty-service makes the host's own cgroup look like an
    SSH session scope or a service cgroup with foreign processes, to prove the suite does not
    depend on where it runs. Tests that pass their own fake /proc are unaffected."""
    mode = os.environ.get("BOT_OFFICE_TEST_HOST_CGROUP")
    if not mode:
        return
    from botoffice import procs

    real = procs.own_cgroup
    fake = None
    if mode == "dirty-service":
        fake = tmp_path_factory.mktemp("cg") / "bot-office.service"
        fake.mkdir()
        (fake / "cgroup.procs").write_text(f"{os.getpid()}\n1\n")

    def own_cgroup(proc=procs.PROC, root=procs.CGROUP_ROOT):
        return fake if proc == procs.PROC else real(proc, root)

    monkeypatch.setattr(procs, "own_cgroup", own_cgroup)


@pytest.fixture(autouse=True)
def _no_real_sudo_review(monkeypatch):
    """0.12.28: tests never start a real `claude -p` reviewer; no answer falls back to the card."""
    from botoffice import sudo_review
    monkeypatch.setattr(sudo_review, "run_claude", lambda prompt, timeout=sudo_review.TIMEOUT: None)


@pytest.fixture(autouse=True)
def _github_repos_are_private(monkeypatch):
    """0.12.45: a push to a public GitHub repo (or one whose visibility is not verified) asks. Tests never
    ask GitHub: every repo reads as private;
    tests about visibility set their own lookup."""
    from botoffice import git_guard, github_access
    monkeypatch.setattr(github_access, "_fetch_visibility", lambda api, owner, repo, token: "private")
    monkeypatch.setattr(git_guard, "visibility_lookup", lambda owner, repo: "private")
    monkeypatch.setattr(github_access.Login, "__call__", lambda self: None)  # no GET /user from tests
    monkeypatch.setattr(github_access, "gh_cli_get", lambda endpoint, jq: None)  # nor the host's gh login
