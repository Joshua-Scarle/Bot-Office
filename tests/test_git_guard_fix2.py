"""0.12.4 fix2: the review's findings. The guard reads the repo's real default branch, follows
symlinks, reads request bodies, binds a grant to the whole action, keeps Sending locked on, refuses
bot-less jobs and fails closed in every hook; git's own pre-push check and the credential helper
are the boundary at the side effect."""
import json
import os
import runpy
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("github_private")  # named GitHub merges reach their card
from fastapi.testclient import TestClient
from guard_repo import clone_workspace

from botoffice import approval_classes, git_guard
from botoffice import rules as rules_mod
from botoffice.api import create_app
from botoffice.providers.cli import git_env
from botoffice.providers.fake import FakeAdapter
from botoffice.rules import Rules
from botoffice.store import JobStore

BIN = Path(__file__).resolve().parents[1] / "bin"
H = {"Authorization": "Bearer test-token"}
SHA_A, SHA_B, ZERO = "a" * 40, "b" * 40, "0" * 40


def bash(command, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def asks(command, cwd):
    return git_guard.classify("Bash", command, None, cwd) is not None


def running(store, bot, source="chat"):
    job = store.create("work", "claude", bot_id=bot.id if bot else None, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store.get(job.id)


# --- #4 the default branch comes from the repo; deletes follow symlinks ---------------------------

def test_the_default_branch_is_read_from_the_clone_not_assumed():
    work = clone_workspace(default="production")
    assert asks("git push origin production", work)
    assert asks("git push origin HEAD:refs/heads/production", work)
    assert asks("git push origin main", work)  # the usual names still ask
    assert not asks("git push origin feature/x", work)
    assert not asks("git push -u origin fix-123", work)


def test_an_unknown_default_branch_guards_the_usual_names_only(tmp_path):
    # fix4 #1: no verified default: main, master, trunk and production ask, a feature branch does not
    (tmp_path / ".git").mkdir()  # a repo with no refs/remotes/origin/HEAD
    assert not asks("git push origin feature/x", str(tmp_path))
    assert not asks("git push origin feature/x", None)
    assert asks("git push origin production", str(tmp_path)) and asks("git push origin master", None)
    assert asks("git push", str(tmp_path))  # not on a branch here: where it goes is not known


@pytest.mark.parametrize("command", ["git branch -d old", "git branch -D old", "git branch --delete old",
                                     "git tag -d v1", "git tag --delete v1", "git push origin :old",
                                     "git push --no-verify origin feature", "git push --no-verify"])
def test_branch_and_tag_deletes_and_no_verify_ask(command):
    assert asks(command, clone_workspace())


def test_deletes_follow_symlinks_and_unknown_paths_ask(tmp_path):
    work = Path(clone_workspace())
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "link").symlink_to(outside)
    for command in ("rm -rf link/data", "rm -rf link/", "find link -delete", "shred -u link/x",
                    "rm -rf $SOMEWHERE/x", 'rm -rf "$DIR"', "rm -rf ${X:-/}", "rm -rf .*",
                    "mv repo /tmp/elsewhere", "mv -t /tmp repo", "rsync -a --delete repo/ link/"):
        assert asks(command, str(work)), command
    for command in ("rm -rf build", "rm -rf $PWD/build", "rm -rf ./repo/tmp", "mv a.txt b.txt", "find . -name x -delete"):
        assert not asks(command, str(work)), command


# --- #5 request bodies ----------------------------------------------------------------------------

def test_gh_api_and_curl_bodies_are_read(tmp_path):
    work = clone_workspace()
    (Path(work) / "merge.json").write_text(json.dumps({"query": "mutation { mergePullRequest(input: {}) { x } }"}))
    (Path(work) / "odd.graphql").write_text("mutation { addStar(input: {starrableId: \"x\"}) { x } }")
    (Path(work) / "read.graphql").write_text("query { viewer { login } }")
    (Path(work) / "comment.graphql").write_text("mutation { addComment(input: {}) { x } }")
    for command in ("gh api graphql --input merge.json", "gh api graphql -F query=@odd.graphql",
                    "gh api graphql --input -", "gh api graphql -F query=@missing.graphql",
                    "gh api graphql -f query=\"$Q\"", "gh api -X POST repos/o/r/dispatches",
                    "gh api repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc", "gh api -X PUT repos/o/r/topics",
                    "gh api -X POST orgs/o/repos -f name=x",
                    "curl -d @merge.json https://api.github.com/graphql",
                    "curl --data-binary @- https://api.github.com/graphql",
                    "curl -X POST https://api.github.com/repos/o/r/hooks -d @merge.json"):
        assert asks(command, work), command
    for command in ("gh api graphql -F query=@read.graphql", "gh api graphql -F query=@comment.graphql",
                    "gh api repos/o/r/issues/3/comments -f body=hi", "gh api repos/o/r/pulls/3",
                    "curl -s https://api.github.com/repos/o/r"):
        assert not asks(command, work), command


# --- the pre-push check's judgement ---------------------------------------------------------------

def update(ref, local=SHA_A, remote=SHA_B, fast_forward=True):
    return {"local_ref": "refs/heads/x", "local_sha": local, "remote_ref": ref, "remote_sha": remote,
            "fast_forward": fast_forward}


@pytest.mark.parametrize("updates,default,asked", [
    ([update("refs/heads/feature")], "main", False),
    ([update("refs/heads/feature", remote=ZERO)], "main", False),  # a new branch
    ([update("refs/tags/v1", remote=ZERO)], "main", False),  # a new tag
    ([update("refs/heads/production")], "production", True),
    ([update("refs/heads/main")], "production", True),  # the usual names always ask
    ([update("refs/heads/feature")], None, False),  # fix4 #1: default not known: the usual names only
    ([update("refs/heads/production")], None, True),
    ([update("refs/heads/trunk")], None, True),
    ([update("refs/heads/feature", local=ZERO)], "main", True),  # delete
    ([update("refs/heads/feature", fast_forward=False)], "main", True),
    ([update("refs/heads/feature", fast_forward=None)], "main", True),  # old commit not here: not proven
    ([update("refs/tags/v1")], "main", True),  # overwriting a tag
    ([update("refs/meta/config")], "main", True),
    ([update("refs/heads/feature", local="nope")], "main", True),
    ("garbage", "main", True),
])
def test_classify_push(updates, default, asked):
    reason = git_guard.classify_push({"remote": "origin", "updates": updates, "default_branch": default,
                                      "default_source": "remote" if default else None})
    assert (reason is not None) == asked, reason


def test_push_commands_name_the_refs():
    assert git_guard.push_commands("git push origin HEAD:main") == [("origin", {"main"})]
    assert git_guard.push_commands("cd x && git push -u origin a b:refs/heads/c") == [("origin", {"a", "c"})]
    assert git_guard.push_commands("git push") == [(None, None)]
    assert git_guard.push_commands("ls") == []


# --- #3 a grant is bound to the whole action ------------------------------------------------------

def test_a_grant_is_bound_to_the_directory_and_the_tool_arguments(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    rules = Rules(store)
    one, two = clone_workspace(), clone_workspace()
    job = running(store, bot)
    card = rules.decide(job, bash("git push origin main", one)).approval_id
    mcp = {"hook_event_name": "PreToolUse", "tool_name": "mcp__github__merge_pull_request",
           "tool_input": {"owner": "o", "repo": "r", "pullNumber": 5}, "cwd": one}
    mcp_card = rules.decide(job, mcp).approval_id
    assert card and mcp_card
    rules.approve(card)
    rules.approve(mcp_card)
    with closing(store._connect()) as db:
        resume_ids = [row[0] for row in db.execute("SELECT resume_job_id FROM approvals WHERE id IN (?, ?)",
                                                   (card, mcp_card))]
        db.execute("UPDATE jobs SET status='running' WHERE id IN (?, ?)", tuple(resume_ids))
    resume = store.get(resume_ids[0])
    # same command, another repo; same tool, another pull request: not what was approved
    assert rules.decide(resume, bash("git push origin main", two)).decision == "deny"
    other = {**mcp, "tool_input": {"owner": "o", "repo": "r", "pullNumber": 6}}
    assert rules.decide(store.get(resume_ids[1]), other).decision == "deny"
    # cosmetic fields (a description, a timeout) do not change the action
    exact = bash("git push origin main", one)
    exact["tool_input"]["description"] = "Push the fix"
    assert rules.decide(resume, exact).decision == "allow"
    assert rules.decide(resume, bash("git push origin main", one)).decision == "deny"  # once
    assert rules.decide(store.get(resume_ids[1]), mcp).decision == "allow"


# --- #2 A16: Sending is locked on; a job with no bot is refused -----------------------------------

@pytest.mark.parametrize("source", ["chat", "routine", "wakeup", "assignment", "inbox", "trigger"])
def test_allow_rules_cannot_open_sending_or_the_guard(tmp_path, source):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    rules = Rules(store)
    approval_classes.update(store, enabled={item.key: False for item in approval_classes.BUILTIN
                                            if item.key not in approval_classes.LOCKED}, timeout_min=0)
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, enabled={"send": False})
    for kind, pattern in (("preset", "outbound"), ("shell", ".*"), ("tool", ".*"), ("path", ".*")):
        for bot_id in (None, bot.id):
            rules.add_rule(bot_id=bot_id, effect="allow", kind=kind, pattern=pattern, scope="all")
    job = running(store, bot, source)
    work = clone_workspace()
    for event in (bash("sendmail -t < outbox/a.eml", work), bash("git push origin main", work),
                  {"hook_event_name": "PreToolUse", "tool_name": "mcp__gmail__send_message",
                   "tool_input": {"to": "a@example.com"}, "cwd": work}):
        decision = rules.decide(job, event)
        assert (decision.decision, decision.effect) == ("deny", "ask"), (source, event)
    assert "send" in {item.key for item in approval_classes.active(store)}
    assert next(c for c in approval_classes.settings(store)["classes"] if c["key"] == "send")["locked"]


def test_a_job_with_no_bot_is_refused_without_a_card(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    rules = Rules(store)
    job = running(store, None, "api")
    work = clone_workspace()
    for command in ("git push origin main", "gh pr merge 3", "sendmail x@example.com < a"):
        decision = rules.decide(job, bash(command, work))
        assert (decision.decision, decision.effect) == ("deny", "never"), command
    assert rules.decide(job, bash("git status", work)).decision == "allow"
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_the_local_gate_refuses_guarded_and_sending_commands():
    work = clone_workspace()
    for command, expected in (("git push origin main", 2), ("sendmail a@example.com < x", 2),
                              ("git push origin feature", 0), ("ls", 0)):
        assert gate(["codex", "--mode", "local"], bash(command, work), {})[0] == expected, command


# --- #6 every hook fails closed ---------------------------------------------------------------------

def gate(args, event, env, raw=None, timeout=60):
    data = raw if raw is not None else json.dumps(event).encode()
    done = subprocess.run([sys.executable, "-I", str(BIN / "bot-office-gate"), *args], input=data,
                          env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, timeout=timeout, check=False)
    return done.returncode, done.stdout.decode(), done.stderr.decode()


def denied(provider, code, out):
    if code != 2:
        return False
    if provider == "cursor":
        return json.loads(out)["permission"] == "deny"
    if provider in ("claude", "codex"):
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    if provider == "antigravity":
        return json.loads(out)["decision"] == "deny"
    return True


@pytest.fixture
def silent_server():
    """A controller that accepts the connection and never answers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()


@pytest.fixture
def odd_server():
    """A controller that answers 200 with a body that is not a decision."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'["allow"]'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("provider", ["claude", "codex", "cursor", "antigravity", "opencode"])
def test_the_gate_fails_closed_for_every_provider(provider, silent_server, odd_server):
    dead = {"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://127.0.0.1:9"}
    work = clone_workspace()
    for mode in ("attended", "unattended", "local"):
        args = [provider, "--mode", mode]
        assert denied(provider, *gate(args, None, dead, raw=b"not json")[:2]), (provider, mode)
        assert denied(provider, *gate(args, None, dead, raw=b'{"tool_name": "Bash", "tool_inp')[:2])
        assert denied(provider, *gate(args, None, dead, raw=b'{"something": "else"}')[:2])
        assert denied(provider, *gate(args, None, dead, raw=b"[1, 2]")[:2])
        assert denied(provider, *gate(args, None, dead, raw=b"x" * (2 * 1024 * 1024))[:2])
        assert denied(provider, *gate(args, bash("ls", work), dead)[:2])  # controller unreachable
    timed = {"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": silent_server, "BOT_OFFICE_GATE_TIMEOUT": "0.5"}
    started = time.monotonic()
    assert denied(provider, *gate([provider, "--mode", "attended"], bash("ls", work), timed)[:2])
    assert time.monotonic() - started < 15
    odd = {"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": odd_server}
    assert denied(provider, *gate([provider, "--mode", "attended"], bash("ls", work), odd)[:2])


def test_the_gate_denies_when_its_input_never_ends():
    """A hook that cannot finish reading its input runs out of time (DEADLINE) and denies."""
    env = {"PATH": "/usr/bin:/bin", "BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://127.0.0.1:9",
           "BOT_OFFICE_GATE_TIMEOUT": "0.5"}
    proc = subprocess.Popen([sys.executable, "-I", str(BIN / "bot-office-gate"), "claude", "--mode", "attended"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    proc.stdin.write(b'{"tool_name": "Bash", ')
    proc.stdin.flush()  # and never closed while the gate runs
    try:
        code = proc.wait(timeout=30)
    finally:
        proc.stdin.close()
    assert code == 2 and b"no answer in time" in proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()


@pytest.mark.parametrize("provider", ["claude", "codex", "cursor", "antigravity"])
def test_a_crash_in_the_gate_denies(provider, monkeypatch, capsys):
    module = runpy.run_path(str(BIN / "bot-office-gate"), run_name="gate")
    main = module["main"]

    def boom(*args):
        raise RuntimeError("bug")

    monkeypatch.setitem(main.__globals__, "_decide", boom)
    assert main(["bot-office-gate", provider]) == 2
    captured = capsys.readouterr()
    assert "check failed (RuntimeError)" in captured.err + captured.out


def test_the_shim_fails_closed_and_stays_on_for_children(tmp_path, monkeypatch, capsys):
    module = runpy.run_path(str(BIN / "bot-office-guard-shim"), run_name="shim")
    main = module["main"]

    def boom(*args):
        raise RuntimeError("bug")

    monkeypatch.setitem(main.__globals__, "_main", boom)
    assert main() == 126
    # children of a shimmed command still see BOT_OFFICE_GUARD_SHIM (a nested `git push` is checked too)
    real = tmp_path / "real"
    real.mkdir()
    (real / "git").write_text(f"#!/bin/sh\nenv > {tmp_path}/env.txt\n")
    (real / "git").chmod(0o755)
    env = {"PATH": f"{BIN}:{real}:/usr/bin:/bin", "BOT_OFFICE_GUARD_SHIM": "1"}
    done = subprocess.run([str(BIN / "git"), "status"], env=env, cwd=clone_workspace(), capture_output=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert "BOT_OFFICE_GUARD_SHIM=1" in (tmp_path / "env.txt").read_text()


def test_the_pre_push_hook_fails_closed(tmp_path):
    hook = BIN / "bot-office-git-hook"
    line = f"refs/heads/feature {SHA_A} refs/heads/feature {ZERO}\n"
    base = {"PATH": "/usr/bin:/bin", "BOT_OFFICE_JOB_ID": "1"}

    def run(env, text=line):
        return subprocess.run([sys.executable, "-I", str(hook), "origin", "https://example.com/r.git"],
                              input=text.encode(), env={**base, **env}, cwd=tmp_path, capture_output=True,
                              timeout=90, check=False)

    assert run({}).returncode == 1  # no token, not local mode
    assert run({"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://127.0.0.1:9"}).returncode == 1
    assert run({"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://example.com:80"}).returncode == 1
    gate_file = tmp_path / "gate.json"
    gate_file.write_text(json.dumps({"mode": "local"}))
    assert run({"BOT_OFFICE_GATE_FILE": str(gate_file)}, "garbage\n").returncode == 1
    # local mode: the default branch is not known here (no remote): a feature push goes on (fix4 #1),
    # a push to production is refused. 0.12.45: only to a remote whose visibility is known here (a local
    # path); a host that is not GitHub, or GitHub without the controller's lookup, is refused
    assert run({"BOT_OFFICE_GATE_FILE": str(gate_file)}).returncode == 1
    local = subprocess.run([sys.executable, "-I", str(hook), "origin", str(tmp_path / "remote.git")], input=line.encode(),
                           env={**base, "BOT_OFFICE_GATE_FILE": str(gate_file)}, cwd=tmp_path, capture_output=True,
                           timeout=90, check=False)
    assert local.returncode == 0, local.stderr
    production = f"refs/heads/production {SHA_A} refs/heads/production {ZERO}\n"
    assert run({"BOT_OFFICE_GATE_FILE": str(gate_file)}, production).returncode == 1
    # not a Bot-Office run: only the repo's own hook (none here)
    assert subprocess.run([sys.executable, "-I", str(hook), "origin", "x"], input=line.encode(),
                          env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path, capture_output=True, timeout=60,
                          check=False).returncode == 0


def test_the_hooks_directory_chains_the_repos_own_hooks(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    own = repo / ".git" / "hooks" / "pre-commit"
    own.write_text(f"#!/bin/sh\ntouch {tmp_path}/own-ran\nexit 1\n")
    own.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           **git_env(SimpleNamespace(github_credential=False))}
    done = subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, env=env,
                          capture_output=True, timeout=60, check=False)
    assert done.returncode == 1 and (tmp_path / "own-ran").exists()
    assert all(os.path.exists(BIN / "git-hooks" / name) for name in ("pre-push", "pre-commit", "commit-msg"))


# --- #1 the credential: only from the controller ----------------------------------------------------

def test_git_env_sets_the_hook_and_the_helper_only_with_a_token():
    plain = git_env(SimpleNamespace(github_credential=False))
    assert plain["GIT_CONFIG_COUNT"] == "1" and plain["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    helped = git_env(SimpleNamespace(github_credential=True))
    assert helped["GIT_CONFIG_COUNT"] == "4" and helped["GIT_CONFIG_VALUE_3"] == "true"
    assert helped["GIT_CONFIG_VALUE_1"] == "" and helped["GIT_CONFIG_VALUE_2"].endswith("bot-office-credential")
    assert not any("token" in value.lower() for value in helped.values())


@pytest.mark.parametrize("command", ["git -c core.hooksPath=/dev/null push origin feature",
                                     "git config core.hooksPath /tmp/h", "GIT_CONFIG_COUNT=0 git push origin feature",
                                     "export GIT_CONFIG_COUNT=0", "unset GIT_CONFIG_COUNT", "env -i git push origin feature",
                                     "env -u GIT_CONFIG_KEY_0 git push", "gh auth token", "git credential fill",
                                     "bot-office-credential get", "GH_TOKEN=x gh pr view 3",
                                     "git -c credential.helper=store push origin feature",
                                     "git config --global credential.helper store"])
def test_changing_the_hook_or_the_credential_asks(command):
    assert asks(command, clone_workspace())


def test_the_token_file_and_the_endpoint(settings, tmp_path, monkeypatch):
    path = tmp_path / "github-token"
    monkeypatch.setenv("BOT_OFFICE_GITHUB_TOKEN_FILE", str(path))
    assert rules_mod.github_token(settings) is None
    path.write_text("github_pat_example\n")
    path.chmod(0o644)
    assert rules_mod.github_token(settings) is None  # readable by others: refused
    path.chmod(0o600)
    assert rules_mod.github_token(settings) == "github_pat_example"
    link = tmp_path / "link"
    link.symlink_to(path)
    monkeypatch.setenv("BOT_OFFICE_GITHUB_TOKEN_FILE", str(link))
    assert rules_mod.github_token(settings) is None  # a symlink: refused
    monkeypatch.setenv("BOT_OFFICE_GITHUB_TOKEN_FILE", str(path))
    with TestClient(create_app(settings, {"claude": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        bot = store.create_bot("Ada", "", "claude", None)
        job = running(store, bot)
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        assert client.post("/v1/agent/github-token", json={"host": "github.com"}).status_code == 401
        assert client.post("/v1/agent/github-token", headers=agent, json={"host": "evil.example"}).status_code == 404
        # fix3: a bare call gets nothing; a ticket for an allowed operation gets the token once
        assert client.post("/v1/agent/github-token", headers=agent, json={"host": "github.com"}).status_code == 403
        ticket = client.post("/v1/agent/github-ticket", headers=agent,
                             json={"argv": ["git", "fetch", "https://github.com/o/r.git"], "cwd": "/"}).json()["tickets"][0]
        got = client.post("/v1/agent/github-token", headers=agent, json={"host": "github.com", "tickets": [ticket]})
        assert got.status_code == 200 and got.json() == {"token": "github_pat_example"}
        with closing(store._connect()) as db:
            logged = " ".join(str(value) for row in db.execute("SELECT * FROM gate_log") for value in row)
        assert "github_pat_example" not in logged and "GitHub credential" in logged
        path.unlink()
        assert client.post("/v1/agent/github-token", headers=agent, json={"host": "github.com"}).status_code == 404


def test_the_credential_helper_prints_nothing_without_the_controller():
    helper = BIN / "bot-office-credential"
    for env in ({}, {"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://127.0.0.1:9"},
                {"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://example.com:80"}):
        done = subprocess.run([sys.executable, "-I", str(helper), "get"], input=b"protocol=https\nhost=github.com\n",
                              env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, timeout=60, check=False)
        assert done.returncode == 0 and done.stdout == b""
    done = subprocess.run([sys.executable, "-I", str(helper), "store"],
                          input=b"protocol=https\nhost=github.com\npassword=x\n", env={"PATH": "/usr/bin:/bin"},
                          capture_output=True, timeout=60, check=False)
    assert done.returncode == 0 and done.stdout == b""


def test_the_launcher_passes_git_config_and_nothing_like_it():
    launcher = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy" / "bot-office-run"),
                              run_name="launcher")
    allowed = launcher["GIT_CONFIG"]
    assert all(allowed.match(name) for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_12"))
    assert not any(allowed.match(name) for name in ("GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                                                     "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_x"))
