"""0.12.4 fix3: the round-2 review. A push card passes only the exact update it names; the GitHub
token goes out only for a one-time ticket the controller issued for an allowed call; the default
branch of a GitHub repo comes from GitHub; sending (non-GET requests off the allowlist, mail,
send-ish connector tools) asks; a job with no bot that is refused leaves a Needs-you notice; hooks
inherited from the user's or system config still run; code in one-liners is read for commands."""
import json
import os
import runpy
import subprocess
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
import test_git_guard_e2e as e2e
from fastapi.testclient import TestClient
from guard_repo import clone_workspace
from test_git_guard_e2e import IDENTITY, git, head, pending, run_next

from botoffice import git_guard, github_access
from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.needs_you import needs_you
from botoffice.providers.cli import git_env
from botoffice.providers.fake import FakeAdapter
from botoffice.rules import Rules

BIN = Path(__file__).resolve().parents[1] / "bin"
H = {"Authorization": "Bearer test-token"}
TOKEN = "github_pat_fix3_example"
world = e2e.world  # the end-to-end fixture: scheduler, API server, a bare remote and a clone


def running(store, bot, source="chat"):
    job = store.create("work", "claude", bot_id=bot.id if bot else None, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store.get(job.id)


def bash(command, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def pre_push(url, updates):
    return {"hook_event_name": "PrePush", "tool_name": "GitPush", "cwd": "/",
            "tool_input": {"remote": "origin", "url": url, "updates": updates}}


def update(ref, old, new, fast_forward=True):
    return {"local_ref": "refs/heads/x", "local_sha": new, "remote_ref": ref, "remote_sha": old,
            "fast_forward": fast_forward}


def gate(client, header, event):
    return client.post("/v1/agent/gate", headers=header, json={"provider": "codex", "event": event}).json()


def github_clone(tmp_path, default="main"):
    """A real repo whose origin is https://github.com/o/r.git, with origin/<default> at the first commit."""
    repo = tmp_path / "gh"
    assert git(tmp_path, "init", "-q", "-b", "feature", str(repo)).returncode == 0
    assert git(repo, "commit", "-q", "--allow-empty", "-m", "one").returncode == 0
    first = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "remote", "add", "origin", "https://github.com/o/r.git").returncode == 0
    assert git(repo, "update-ref", f"refs/remotes/origin/{default}", first).returncode == 0
    assert git(repo, "update-ref", "refs/remotes/origin/feature", first).returncode == 0
    assert git(repo, "commit", "-q", "--allow-empty", "-m", "two").returncode == 0
    return repo, first, git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def github(settings, tmp_path, monkeypatch):
    """The controller holds a token; GitHub says the default branch of o/r is main (no network)."""
    path = tmp_path / "github-token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    monkeypatch.setenv("BOT_OFFICE_GITHUB_TOKEN_FILE", str(path))
    monkeypatch.setattr(github_access, "_fetch_default", lambda api, owner, repo, token: "main")
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as client:
        store = client.app.state.store
        bot = store.create_bot("Ada", "", "codex", None)
        job = running(store, bot)
        yield client, store, bot, job, {"Authorization": "Bearer " + store.issue_agent_token(job)}


# --- #3 a push card passes the exact update it names, nothing else ---------------------------------

def test_the_push_plan_names_repo_ref_operation_and_both_shas(tmp_path):
    repo, first, second = github_clone(tmp_path)
    plan = git_guard.push_plan("git push origin HEAD:main", str(repo))
    assert plan == [{"url": "github.com/o/r", "ref": "refs/heads/main", "op": "update", "old": first, "new": second}]
    report = {"url": "git@github.com:O/R.git", "updates": [update("refs/heads/main", first, second)]}
    assert git_guard.plan_covers(plan, report)
    for changed in ({"updates": [update("refs/heads/main", first, "c" * 40)]},       # another commit
                    {"updates": [update("refs/heads/release", first, second)]},       # another branch
                    {"url": "https://github.com/o/other.git"},                         # another repo
                    {"updates": [update("refs/heads/main", first, second, False)]},   # now a force push
                    {"updates": [update("refs/heads/main", first, second), update("refs/tags/v1", "0" * 40, second)]}):
        assert not git_guard.plan_covers(plan, {**report, **changed}), changed


@pytest.mark.parametrize("command", ["git push --all origin", "git push --mirror origin", "git push origin $BRANCH",
                                     "git push origin 'refs/heads/*:refs/heads/*'", "git push nowhere HEAD:main"])
def test_an_update_that_cannot_be_worked_out_has_no_plan(tmp_path, command):
    repo, _first, _second = github_clone(tmp_path)
    assert git_guard.push_plan(command, str(repo)) is None


def test_an_approved_push_passes_once_and_only_as_approved(github, tmp_path):
    client, store, _bot, _job, header = github
    repo, first, second = github_clone(tmp_path)
    command = "git push origin HEAD:main"
    assert gate(client, header, bash(command, str(repo)))["effect"] == "ask"
    card = next(item for item in client.get("/v1/needs-you", headers=H).json() if item["kind"] == "approval")
    assert f"exact update: github.com/o/r refs/heads/main: {first[:10]} -> {second[:10]}" in card["ask"]
    assert card["command"].startswith(f"Bash: {command}") and "exact update" not in card["command"]
    resume = Rules(store).approve(pending(store)[0]["id"], assignments=Assignments(store))["resume_job_id"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(resume),))
    resumed = {"Authorization": "Bearer " + store.issue_agent_token(store.get(resume))}
    url = "https://github.com/o/r.git"
    assert gate(client, resumed, bash(command, str(repo)))["decision"] == "allow"
    # git's pre-push check: another commit, guarded branch, operation or repo is not what was approved
    assert gate(client, resumed, pre_push(url, [update("refs/heads/main", first, "c" * 40)]))["effect"] == "ask"
    assert gate(client, resumed, pre_push(url, [update("refs/heads/master", first, second)]))["effect"] == "ask"
    assert gate(client, resumed, pre_push(url, [update("refs/heads/main", first, second, False)]))["effect"] == "ask"
    assert gate(client, resumed, pre_push("https://github.com/o/x.git",
                                          [update("refs/heads/main", first, second)]))["effect"] == "ask"
    assert gate(client, resumed, pre_push(url, [update("refs/heads/main", first, second)]))["decision"] == "allow"
    assert gate(client, resumed, pre_push(url, [update("refs/heads/main", first, second)]))["effect"] == "ask"


def test_a_push_card_from_the_pre_push_check_shows_the_update(github):
    client, _store, _bot, _job, header = github
    answer = gate(client, header, pre_push("https://github.com/o/r.git", [update("refs/heads/main", "a" * 40, "b" * 40)]))
    assert answer["effect"] == "ask"
    card = next(item for item in client.get("/v1/needs-you", headers=H).json() if item["kind"] == "approval")
    assert "exact update: github.com/o/r refs/heads/main: aaaaaaaaaa -> bbbbbbbbbb (update)" in card["ask"]


# --- #2 the token goes out only for a ticket the controller issued for an allowed call --------------

def ticket(client, header, argv, cwd="/"):
    return client.post("/v1/agent/github-ticket", headers=header, json={"argv": argv, "cwd": cwd})


def redeem(client, header, tickets, path="o/r.git"):
    return client.post("/v1/agent/github-token", headers=header,
                       json={"host": "github.com", "path": path, "tickets": tickets, "for": "CALLER-CHOSEN-TEXT"})


def test_a_bare_request_for_the_token_is_refused(github):
    client, _store, _bot, _job, header = github
    for body in ({"host": "github.com"}, {"host": "github.com", "for": "git"}, {"host": "github.com", "ticket": "made-up"}):
        assert client.post("/v1/agent/github-token", headers=header, json=body).status_code == 403


def test_a_ticket_is_one_time_and_bound_to_job_and_repo(github, monkeypatch):
    client, store, bot, _job, header = github
    argv = ["git", "fetch", "https://github.com/o/r.git"]
    first = ticket(client, header, argv).json()["tickets"]
    assert redeem(client, header, first, path="o/other.git").status_code == 403  # another repo
    other_job = running(store, bot)
    other = {"Authorization": "Bearer " + store.issue_agent_token(other_job)}
    assert redeem(client, other, first).status_code == 403  # another job
    got = redeem(client, header, first)
    assert got.status_code == 200 and got.json() == {"token": TOKEN}
    assert redeem(client, header, first).status_code == 403  # used once
    monkeypatch.setattr(github_access, "TICKET_SECONDS", -1)
    assert redeem(client, header, ticket(client, header, argv).json()["tickets"]).status_code == 403  # expired


def test_a_guarded_push_gets_no_ticket_until_approved_and_a_feature_push_does(github, tmp_path):
    client, _store, _bot, _job, header = github
    repo, _first, _second = github_clone(tmp_path)
    refused = ticket(client, header, ["git", "push", "origin", "HEAD:main"], str(repo))
    assert refused.status_code == 403 and "approval" in refused.json()["detail"]
    assert ticket(client, header, ["gh", "pr", "merge", "3", "-R", "o/r"]).status_code == 403
    assert ticket(client, header, ["git", "push", "origin", "HEAD:feature"], str(repo)).status_code == 200
    assert ticket(client, header, ["gh", "pr", "view", "3", "-R", "o/r"]).status_code == 200
    assert ticket(client, header, ["git", "status"], str(repo)).status_code == 403  # not a GitHub operation


def test_hand_outs_are_logged_without_the_token_or_the_callers_words(github):
    client, store, _bot, _job, header = github
    assert redeem(client, header, ticket(client, header, ["git", "fetch", "https://github.com/o/r.git"])
                  .json()["tickets"]).status_code == 200
    with closing(store._connect()) as db:
        logged = " ".join(str(value) for row in db.execute("SELECT * FROM gate_log") for value in row)
    assert "GitHub credential for fetch github.com/o/r" in logged
    assert TOKEN not in logged and "CALLER-CHOSEN-TEXT" not in logged


class _Controller(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(body)
        reply = json.dumps({"token": "secret-for-git"} if body.get("tickets") == ["t1"] else {}).encode()
        self.send_response(200 if body.get("tickets") == ["t1"] else 403)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def test_the_credential_helper_spends_the_calls_ticket_and_nothing_else():
    server = HTTPServer(("127.0.0.1", 0), _Controller)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = {"PATH": "/usr/bin:/bin", "BOT_OFFICE_AGENT_TOKEN": "agent",
                "BOT_OFFICE_API_URL": f"http://127.0.0.1:{server.server_port}"}
        ask = "protocol=https\nhost=github.com\npath=o/r.git\n"

        def helper(env):
            return subprocess.run([sys.executable, "-I", str(BIN / "bot-office-credential"), "get"], input=ask.encode(),
                                  env={**base, **env}, capture_output=True, timeout=30, check=False).stdout.decode()

        assert helper({}) == "" and _Controller.seen == []  # no ticket: it does not even ask
        assert helper({"BOT_OFFICE_GITHUB_TICKETS": "t2"}) == ""
        assert helper({"BOT_OFFICE_GITHUB_TICKETS": "t1"}) == "username=x-access-token\npassword=secret-for-git\n"
        assert _Controller.seen[-1] == {"host": "github.com", "path": "o/r.git", "tickets": ["t1"]}
    finally:
        server.shutdown()


def test_git_names_the_repository_to_the_helper():
    env = git_env(SimpleNamespace(github_credential=True))
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["credential.https://github.com.useHttpPath"] == "true"


# --- #5 the target repo's real default branch, from GitHub ------------------------------------------

def test_connector_pushes_use_the_repos_real_default_branch(monkeypatch):
    monkeypatch.setattr(git_guard, "default_lookup", lambda owner, repo: "production" if repo == "r" else None)
    push = "mcp__github__push_files"
    assert git_guard.classify(push, "", {"owner": "o", "repo": "r", "branch": "production"}, "/")
    assert git_guard.classify(push, "", {"repository": "o/r", "branch": "production"}, "/")
    assert git_guard.classify(push, "", {"owner": "o", "repo": "r", "branch": "feature"}, "/") is None
    # fix4 #1: an unknown default guards main, master, trunk and production only
    assert git_guard.classify(push, "", {"owner": "o", "repo": "unknown", "branch": "feature"}, "/") is None
    assert git_guard.classify(push, "", {"owner": "o", "repo": "unknown", "branch": "production"}, "/")
    assert git_guard.classify(push, "", {"branch": "feature"}, "/") is None
    assert git_guard.classify(push, "", {"branch": "production"}, "/")
    assert git_guard.classify("mcp__github__merge_pull_request", "", {"owner": "o", "repo": "r"}, "/")


def test_the_pre_push_check_and_the_command_guard_use_githubs_default(monkeypatch, tmp_path):
    monkeypatch.setattr(git_guard, "default_lookup", lambda owner, repo: "production")
    report = {"url": "https://github.com/o/r.git", "default_branch": None,
              "updates": [update("refs/heads/production", "a" * 40, "b" * 40)]}
    assert git_guard.classify_push(report)
    report["updates"] = [update("refs/heads/feature", "a" * 40, "b" * 40)]
    assert git_guard.classify_push(report) is None
    # no token on the controller (GitHub not asked): the remote's own answer (ls-remote) when the hook
    # has one; else (fix4 #1) the usual names and the clone's own default are guarded, nothing else
    monkeypatch.setattr(git_guard, "default_lookup", lambda owner, repo: None)
    assert git_guard.classify_push({**report, "default_branch": "feature", "default_source": "remote"})
    assert git_guard.classify_push({**report, "default_branch": "main", "default_source": "remote"}) is None
    assert git_guard.classify_push({**report, "local_default": "feature"})
    assert git_guard.classify_push(report) is None
    monkeypatch.setattr(git_guard, "default_lookup", lambda owner, repo: "production")
    repo, _first, _second = github_clone(tmp_path, default="main")  # the clone's origin/HEAD is stale
    assert git_guard.classify("Bash", "git push origin HEAD:production", None, str(repo))


def test_with_the_controllers_token_the_hook_does_not_ask_github(tmp_path, monkeypatch):
    repo, _first, _second = github_clone(tmp_path, default="trunk")
    assert git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk").returncode == 0
    hook = runpy.run_path(str(BIN / "bot-office-git-hook"))
    monkeypatch.chdir(repo)
    for key, value in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                       "GIT_CONFIG_VALUE_0": str(BIN / "bot-office-credential")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(hook["default_branch"].__globals__, "remote_head", lambda *a: pytest.fail("asked GitHub"))
    assert hook["default_branch"]("origin", "https://github.com/o/r.git") == (None, None, "trunk")


def test_default_branches_are_cached_and_need_the_token():
    calls = []

    def fetch(api, owner, repo, token):
        calls.append((owner, repo, token))
        return "trunk"

    lookup = github_access.DefaultBranches(lambda: "t", fetch=fetch, api="https://api.invalid")
    assert lookup("O", "R.git") == "trunk" and lookup("o", "r") == "trunk" and len(calls) == 1
    assert github_access.DefaultBranches(lambda: None, fetch=fetch)("o", "r") is None and len(calls) == 1


# --- #4 sending is a class: it asks in every run ----------------------------------------------------

@pytest.mark.parametrize("command", [
    "curl -X POST https://hooks.slack.com/services/x -d '{}'", "curl -d @report.json https://api.sendgrid.com/v3/mail/send",
    "curl --json '{}' https://discord.com/api/webhooks/1/x", "wget --post-data=a=b https://api.twilio.com/2010-04-01/x",
    "http POST api.mailgun.net/v3/x/messages a=b", "xh hooks.slack.com/services/x a=b",
    "sendmail owner@example.com < note.txt",
    "python3 -c \"import requests; requests.post('https://hooks.slack.com/services/x', json={})\"",
    "node -e \"fetch('https://graph.microsoft.com/v1.0/me/sendMail', {method: 'POST', body: 'x'})\"",
    "python3 -c \"import smtplib; smtplib.SMTP('mail.example.org').sendmail('a', 'b', 'c')\""])
def test_a_request_that_sends_asks(command):
    assert git_guard.judge("Bash", command, None, clone_workspace()), command


@pytest.mark.parametrize("command", [
    "curl https://example.org/data.json", "curl -X POST http://127.0.0.1:8080/v1/x -d a", "wget https://example.org/f",
    "curl -d a http://localhost:9000/x", "http example.org/search q==x", "pip install requests",
    "python3 -c \"import requests; print(requests.get('https://example.org').text)\"", "ls mail",
    # fix4 #3: a POST that is not to a known sending endpoint does not ask
    "curl -d @report.json https://api.example.com/v1", "wget --post-data=a=b https://example.org/form",
    "http POST example.org/x a=b", "curl -X POST https://ci.internal.example/hook"])
def test_reading_and_local_requests_do_not_ask(command):
    assert git_guard.judge("Bash", command, None, clone_workspace()) is None, command


def test_the_sending_endpoints_can_be_extended(monkeypatch):
    command = "curl -X POST https://ci.internal.example/hook"
    assert git_guard.judge("Bash", command, None, clone_workspace()) is None
    monkeypatch.setenv("BOT_OFFICE_SEND_HOSTS", "ci.internal.example")
    assert git_guard.judge("Bash", command, None, clone_workspace())


@pytest.mark.parametrize("tool,asked", [
    ("mcp__slack__post_message", True), ("mcp__slack__chat_postMessage", True), ("mcp__github__create_issue", True),
    ("mcp__github__add_issue_comment", True), ("mcp__x__publish_post", True), ("mcp__gmail__send_email", True),
    ("mcp__slack__get_message", False), ("mcp__github__list_issues", False), ("mcp__notion__create_page", False),
    ("mcp__github__get_issue_comments", False)])
def test_connector_tools_that_send_ask(tool, asked):
    assert (git_guard.send_reason(tool, "", {}, "/") is not None) is asked


def test_a_job_with_no_bot_is_refused_with_a_notice(world):
    store, scheduler, adapter, remote, _clone = world
    before = head(remote)
    store.create("check", "codex", source="api")
    adapter.turns = [[("commit", "throwaway"), ("hooked", "git push origin HEAD:main"), ("direct", "git push origin HEAD:main"),
                      ("hooked", "curl -X POST https://email.example.invalid.amazonaws.com/x -d a")]]
    run_next(store, scheduler)
    assert adapter.seen[-1][1] == [0, ("gate", 2), ("ran", 1), ("gate", 2)], adapter.seen
    assert head(remote) == before and pending(store) == []
    notices = [item for item in needs_you(store) if item.get("state") == "refused"]
    texts = " ".join(item["ask"] for item in notices)
    assert len(notices) >= 3 and "git push" in texts and "amazonaws.com" in texts
    assert all(item["bot_name"] == "Job without a bot" and item["actions"] == ["dismiss"] for item in notices)


# --- #6 hooks inherited from the user's or the system's config still run ---------------------------

def test_a_global_hooks_path_is_chained(tmp_path):
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "pre-push"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {tmp_path}/{name}-ran\nexit 1\n")
        (hooks / name).chmod(0o755)
    (tmp_path / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n")
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **IDENTITY,
           **git_env(SimpleNamespace(github_credential=False))}

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, timeout=60, check=False)

    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=env)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    assert run("commit", "-q", "--allow-empty", "-m", "x").returncode == 1
    assert (tmp_path / "pre-commit-ran").exists()
    assert run("commit", "-q", "--no-verify", "--allow-empty", "-m", "x").returncode == 0
    assert run("push", "-q", str(remote), "HEAD:feature").returncode == 1  # not a Bot-Office run: own hook only
    assert (tmp_path / "pre-push-ran").exists()


# --- #1 code in one-liners is read for the commands it runs; more programs go through the shim ------

@pytest.mark.parametrize("command", [
    "python3 -c \"import subprocess; subprocess.run(['git', 'push', '--force', 'origin', 'x'])\"",
    "python3 -c \"import os; os.system('git push origin main')\"",
    "node -e \"require('child_process').execSync('gh pr merge 3')\"",
    "perl -e 'system(\"git\", \"push\", \"origin\", \":old\")'"])
def test_one_liners_that_run_guarded_commands_ask(command):
    assert git_guard.classify("Bash", command, None, clone_workspace()), command


def test_send_programs_go_through_the_shim(tmp_path):
    for name in ("http", "https", "xh", "xhs", "sendmail", "mail", "mailx", "msmtp", "swaks", "mutt", "curl", "wget"):
        stub = BIN / name
        assert os.access(stub, os.X_OK) and f"--as {name}" in stub.read_text(), name
    gate_file = tmp_path / "gate.json"
    gate_file.write_text(json.dumps({"mode": "local"}))
    env = {"PATH": f"{BIN}:/usr/bin:/bin", "BOT_OFFICE_GUARD_SHIM": "1", "BOT_OFFICE_JOB_ID": "1",
           "BOT_OFFICE_GATE_FILE": str(gate_file), "BOT_OFFICE_SEND_HOSTS": "hooks.example.invalid"}
    done = subprocess.run([str(BIN / "curl"), "-s", "-X", "POST", "https://hooks.example.invalid/x"], cwd=tmp_path,
                          env=env, capture_output=True, timeout=60, check=False)
    assert done.returncode != 0 and b"notice" in done.stderr
    refused = [json.loads(line) for line in (tmp_path / git_guard.REFUSED_FILE).read_text().splitlines()]
    assert refused[-1]["kind"] == "send" and "hooks.example.invalid" in refused[-1]["what"]
