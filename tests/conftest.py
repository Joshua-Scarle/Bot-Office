import os
from pathlib import Path

import pytest

from botoffice.config import Settings


@pytest.fixture(autouse=True)
def _allow_fake_voice(monkeypatch):
    # the worker's fake voice engine refuses to run unless the tests opt in
    monkeypatch.setenv("BOT_OFFICE_VOICE_FAKE", "1")


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
