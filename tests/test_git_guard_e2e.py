"""0.12.4 fix2 #7: end to end through the scheduler, in every run kind. A fake engine runs a real
`git push` to a local bare repo, the way a CLI does: the gate hook first, then the command, with the
environment the adapter builds (core.hooksPath, so git's own pre-push check runs too). The remote's
default branch does not move before the owner approves, moves once after, and a push the gate never
saw (as if the classifier missed it) is still stopped by the pre-push check."""
import asyncio
import dataclasses
import json
import os
import shlex
import socket
import subprocess
import threading
from contextlib import closing

import pytest

from botoffice import rules as rules_mod
from botoffice import slash as slash_mod
from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.providers.base import RunResult
from botoffice.providers.cli import CliAdapter
from botoffice.rules import Rules
from botoffice.scheduler import Scheduler

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
PUSH = "git push origin HEAD:main"


def git(cwd, *args, env=None):
    done = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **IDENTITY, **(env or {})},
                          capture_output=True, text=True, timeout=60, check=False)
    return done


def repos(tmp_path):
    """A bare remote whose default branch is main, and a clone of it (origin/HEAD set by the clone)."""
    remote, seed, clone = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "clone"
    assert git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote)).returncode == 0
    assert git(tmp_path, "init", "-q", "-b", "main", str(seed)).returncode == 0
    assert git(seed, "commit", "-q", "--allow-empty", "-m", "start").returncode == 0
    assert git(seed, "push", "-q", str(remote), "main").returncode == 0
    assert git(tmp_path, "clone", "-q", str(remote), str(clone)).returncode == 0
    return remote, clone


def head(remote):
    return git(remote, "rev-parse", "refs/heads/main").stdout.strip()


class Pusher(CliAdapter):
    """Each turn runs the next list of steps: ("hooked", cmd) asks the gate hook and runs cmd only on
    an allow, as Claude/Codex do; ("direct", cmd) runs cmd without asking (a command the classifier
    missed); ("commit", msg) makes a throwaway commit."""

    name = "codex"

    def __init__(self, clone):
        super().__init__(executable="/bin/true")
        self.clone, self.turns, self.seen = clone, [], []

    async def run(self, job, workspace, log_path, timeout):
        env = {**self._env(workspace, job), **IDENTITY}
        results = []
        for kind, text in (self.turns.pop(0) if self.turns else []):
            if kind == "commit":
                results.append(git(self.clone, "commit", "-q", "--allow-empty", "-m", text).returncode)
                continue
            if kind == "hooked":
                event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": text},
                         "cwd": str(self.clone)}
                gate = subprocess.run(shlex.split(job.gate_command), input=json.dumps(event).encode(),  # noqa: ASYNC221
                                      env=env, capture_output=True, timeout=60, check=False)
                if gate.returncode != 0:
                    results.append(("gate", gate.returncode))
                    continue
            done = subprocess.run(text, shell=True, cwd=self.clone, env=env,  # noqa: ASYNC221
                                  capture_output=True, timeout=60, check=False)
            results.append(("ran", done.returncode))
        self.seen.append((job.id, results))
        return RunResult("succeeded", exit_code=0)


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def world(settings, tmp_path):
    import uvicorn

    settings = dataclasses.replace(settings, port=free_port())
    settings.ensure_dirs()
    remote, clone = repos(tmp_path)
    adapter = Pusher(clone)
    # the API answers the hooks; this test drives the scheduler itself (lifespan off: no background loop)
    app = create_app(settings, {"codex": adapter})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="error",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    store = app.state.store
    scheduler = Scheduler(store, settings, {"codex": adapter})
    yield store, scheduler, adapter, remote, clone
    server.should_exit = True
    thread.join(timeout=10)


def run_next(store, scheduler):
    claimed = store.claim(["desktop-1"], {"codex"})
    assert claimed is not None
    asyncio.run(scheduler._execute(0, claimed))
    return claimed


def pending(store):
    with closing(store._connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id")]


def start(store, settings, bot, kind):
    if kind == "assignment":
        made = Assignments(store).create(bot.id, {"title": "Ship the fix", "start": True})
        with closing(store._connect()) as db:
            row = db.execute("SELECT id FROM jobs WHERE assignment_id=?", (str(made.id),)).fetchone()
        assert row is not None, "starting the assignment queues its first turn"
        return row[0]
    if kind == "trigger":
        rules_mod.record_capability(settings.control_dir, "codex", "enforced", "test", helpers=True)
    job = store.create("work", "codex", bot_id=bot.id, source="chat" if kind == "slash" else kind)
    if kind == "slash":
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET slash=? WHERE id=?", (slash_mod.encode_slash("review", "src", "command"),
                                                              str(job.id)))
    return str(job.id)


@pytest.mark.parametrize("kind", ["chat", "routine", "wakeup", "assignment", "slash", "inbox", "trigger"])
def test_a_push_to_main_waits_for_approve_then_runs_once(world, kind):
    store, scheduler, adapter, remote, clone = world
    bot = store.create_bot("Ada", "", "codex", None)
    store.update_bot(bot.id, cli_access="full")
    bot = store.get_bot(bot.id)
    before = head(remote)
    first = start(store, scheduler.settings, bot, kind)
    adapter.turns = [[("commit", "throwaway 1"), ("hooked", PUSH), ("direct", PUSH)]]
    assert str(run_next(store, scheduler).id) == first
    # the hook refused the command; git's pre-push check refused the push the hook never saw
    assert adapter.seen[-1][1][1:] == [("gate", 2), ("ran", 1)], adapter.seen
    assert head(remote) == before, "pushed before approval"
    cards = pending(store)
    assert [card["job_id"] for card in cards] == [first, first]
    command_card = next(card for card in cards if PUSH in card["summary"])
    assert any("GitPush" in card["summary"] or "default branch" in card["summary"] for card in cards
               if card is not command_card)
    # Approve: the resume turn runs the approved command once; the pre-push check lets that push through
    throwaway = git(clone, "rev-parse", "HEAD").stdout.strip()
    resume = Rules(store).approve(command_card["id"], assignments=Assignments(store))["resume_job_id"]
    adapter.turns = [[("hooked", PUSH), ("commit", "throwaway 2"), ("hooked", PUSH), ("direct", PUSH)]]
    assert str(run_next(store, scheduler).id) == str(resume)
    assert adapter.seen[-1][1] == [("ran", 0), 0, ("gate", 2), ("ran", 1)], adapter.seen
    assert head(remote) == throwaway != before, "the approved push did not land exactly once"


def test_a_job_with_no_bot_is_refused_and_never_pushes(world):
    store, scheduler, adapter, remote, _clone = world
    before = head(remote)
    store.create("check", "codex", source="api")
    adapter.turns = [[("commit", "throwaway"), ("hooked", PUSH), ("direct", PUSH),
                      ("hooked", "git push origin HEAD:feature/x")]]
    run_next(store, scheduler)
    assert adapter.seen[-1][1] == [0, ("gate", 2), ("ran", 1), ("ran", 0)], adapter.seen
    assert head(remote) == before and pending(store) == []
    assert git(remote, "rev-parse", "refs/heads/feature/x").returncode == 0  # ordinary pushes still work
