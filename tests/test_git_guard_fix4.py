"""Fix round 4 (Codex round-3 review): the guard is defence in depth and must not slow normal work.

#1 the remote's default branch is verified with `git ls-remote --symref` (the bot's own credentials);
   while it is not known, main, master, trunk, production and the clone's own default are guarded and
   other branches pass. #2 only inline code is judged, never a script file or an import. #3 only
   requests to known sending endpoints (and BOT_OFFICE_SEND_HOSTS) are sending.
"""
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from test_git_guard_e2e import git

from botoffice import git_guard

BIN = Path(__file__).resolve().parents[1] / "bin"
ZERO = "0" * 40


@pytest.fixture(autouse=True)
def _no_github_lookup(monkeypatch):
    monkeypatch.setattr(git_guard, "default_lookup", None)
    monkeypatch.setattr(git_guard, "_LEARNED", {})
    monkeypatch.delenv("BOT_OFFICE_SEND_HOSTS", raising=False)


def remote_and_clone(tmp_path, remote_default="production", clone_default="main"):
    """A bare remote whose HEAD is *remote_default* and a clone whose origin/HEAD still says
    *clone_default* (the remote's default changed after the clone)."""
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    assert git(tmp_path, "init", "-q", "--bare", "-b", clone_default, str(bare)).returncode == 0
    assert git(tmp_path, "init", "-q", "-b", "feature", str(work)).returncode == 0
    assert git(work, "commit", "-q", "--allow-empty", "-m", "one").returncode == 0
    assert git(work, "remote", "add", "origin", str(bare)).returncode == 0
    for branch in {clone_default, remote_default, "feature"}:
        assert git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}").returncode == 0
    assert git(work, "fetch", "-q", "origin").returncode == 0
    assert git(work, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{clone_default}").returncode == 0
    assert git(bare, "symbolic-ref", "HEAD", f"refs/heads/{remote_default}").returncode == 0
    return bare, work


def hook_default(work, monkeypatch, remote="origin", url=""):
    hook = runpy.run_path(str(BIN / "bot-office-git-hook"))
    monkeypatch.chdir(work)
    for key in ("GIT_CONFIG_COUNT", "GIT_DIR"):
        monkeypatch.delenv(key, raising=False)
    return hook["default_branch"](remote, url)


def report(branch, found, sha="a" * 40):
    default, source, local = found
    return {"remote": "origin", "url": "/x", "default_branch": default, "default_source": source,
            "local_default": local, "updates": [{"local_ref": "refs/heads/x", "local_sha": sha,
                                                 "remote_ref": f"refs/heads/{branch}", "remote_sha": ZERO}]}


# --- #1 the default branch ---------------------------------------------------------------------------

def test_a_stale_origin_head_is_corrected_by_the_remote(tmp_path, monkeypatch):
    bare, work = remote_and_clone(tmp_path)
    found = hook_default(work, monkeypatch, url=str(bare))
    assert found == ("production", "remote", "main")
    assert git_guard.classify_push(report("production", found))
    assert git_guard.classify_push(report("main", found))  # the usual names always ask
    assert git_guard.classify_push(report("feature", found)) is None


def test_a_real_default_outside_the_usual_names_is_found(tmp_path, monkeypatch):
    bare, work = remote_and_clone(tmp_path, remote_default="develop")
    found = hook_default(work, monkeypatch, url=str(bare))
    assert found == ("develop", "remote", "main")
    assert git_guard.classify_push(report("develop", found))
    assert git_guard.classify_push(report("feature", found)) is None


def test_a_missing_origin_head_is_found_from_the_remote(tmp_path, monkeypatch):
    bare, work = remote_and_clone(tmp_path, remote_default="develop")
    assert git(work, "symbolic-ref", "-d", "refs/remotes/origin/HEAD").returncode == 0
    found = hook_default(work, monkeypatch, url=str(bare))
    assert found == ("develop", "remote", None)
    assert git_guard.classify_push(report("develop", found))
    assert git_guard.classify_push(report("feature", found)) is None


def test_when_ls_remote_fails_the_usual_names_and_the_clones_default_are_guarded(tmp_path, monkeypatch):
    _bare, work = remote_and_clone(tmp_path, remote_default="develop", clone_default="develop")
    assert git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git")).returncode == 0
    found = hook_default(work, monkeypatch, url=str(tmp_path / "gone.git"))
    assert found == (None, None, "develop")
    for branch in ("main", "master", "trunk", "production", "develop"):
        assert git_guard.classify_push(report(branch, found)), branch
    assert git_guard.classify_push(report("feature", found)) is None
    assert git_guard.classify_push(report("ada/work-1", (None, None, None))) is None


def test_the_remotes_answer_is_cached(tmp_path, monkeypatch):
    bare, work = remote_and_clone(tmp_path)
    assert hook_default(work, monkeypatch, url=str(bare))[0] == "production"
    cache = json.loads((work / ".git" / "bot-office-default-branch.json").read_text())
    assert cache[str(bare)][1] == "production"
    bare.rename(tmp_path / "moved.git")  # the remote cannot be asked now: the cached answer holds
    assert hook_default(work, monkeypatch, url=str(bare))[0] == "production"
    # a failed lookup is cached for a short time only
    hook = runpy.run_path(str(BIN / "bot-office-git-hook"))
    assert hook["CACHE_UNKNOWN_TTL"] <= 300 < hook["CACHE_TTL"] and hook["LS_REMOTE_TIMEOUT"] <= 10


def test_the_pre_push_check_in_a_job_without_a_bot(tmp_path):
    bare, work = remote_and_clone(tmp_path, remote_default="develop")
    gate_file = tmp_path / "gate.json"
    gate_file.write_text(json.dumps({"mode": "local"}))
    sha = git(work, "rev-parse", "HEAD").stdout.strip()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "BOT_OFFICE_JOB_ID": "1",
           "BOT_OFFICE_GATE_FILE": str(gate_file)}

    def push(branch):
        line = f"refs/heads/feature {sha} refs/heads/{branch} {ZERO}\n"
        return subprocess.run([sys.executable, "-I", str(BIN / "bot-office-git-hook"), "origin", str(bare)],
                              input=line.encode(), env=env, cwd=work, capture_output=True, timeout=60, check=False)

    assert push("develop").returncode == 1  # the remote's real default, though the clone says main
    assert push("new-feature").returncode == 0


def test_the_command_guard_uses_the_learned_default_and_guards_the_fallback(tmp_path):
    _bare, work = remote_and_clone(tmp_path, remote_default="develop", clone_default="stable")
    root = str(work)
    # not verified yet: the clone's own default and the usual names ask, other branches pass
    assert git_guard.classify("Bash", "git push origin HEAD:stable", None, root)
    assert git_guard.classify("Bash", "git push origin HEAD:production", None, root)
    assert git_guard.classify("Bash", "git push origin HEAD:develop", None, root) is None
    assert git_guard.classify("Bash", "git push -u origin feature", None, root) is None
    # the pre-push check learned the remote's answer (ls-remote): that is the default now
    git_guard.learn_default(root, "origin", "develop")
    assert git_guard.classify("Bash", "git push origin HEAD:develop", None, root)
    assert git_guard.classify("Bash", "git push origin HEAD:stable", None, root) is None
    assert git_guard.classify("Bash", "git push origin HEAD:main", None, root)


def test_connector_pushes_with_no_known_default(monkeypatch):
    push = "mcp__github__push_files"
    assert git_guard.classify(push, "", {"owner": "o", "repo": "r", "branch": "feature"}, "/") is None
    for branch in ("main", "master", "production"):
        assert git_guard.classify(push, "", {"owner": "o", "repo": "r", "branch": branch}, "/"), branch
    monkeypatch.setattr(git_guard, "default_lookup", lambda owner, repo: "develop")
    assert git_guard.classify(push, "", {"owner": "o", "repo": "r", "branch": "develop"}, "/")


# --- #2 only inline code -------------------------------------------------------------------------------

def test_script_files_and_imports_do_not_ask(tmp_path):
    (tmp_path / "test_push.py").write_text(
        "import smtplib, subprocess\nFIXTURE = 'git push --force origin main'\n"
        "def dormant():\n    smtplib.SMTP('mail.example.org').sendmail('a', 'b', 'c')\n"
        "    subprocess.run(['git', 'push', '-f', 'origin', 'main'])\n")
    (tmp_path / "send.js").write_text("fetch('https://hooks.slack.com/services/x', {method: 'POST'})\n")
    root = str(tmp_path)
    for command in ("python test_push.py", "python3 -m pytest test_push.py", "pytest -q test_push.py",
                    "python -u test_push.py --flag 'git push origin main'", "node send.js", "bash test_push.py",
                    "python3 -c 'import smtplib; print(\"ok\")'", "python3 -c 'import smtplib, ssl'",
                    "cat > t.py <<'EOF'\nimport smtplib\nsmtplib.SMTP('x').sendmail('a', 'b', 'c')\nEOF\npython t.py",
                    "cat > notes.md <<EOF\ngit push --force origin main\nEOF"):
        assert git_guard.judge("Bash", command, None, root) is None, command


@pytest.mark.parametrize("command", [
    "python3 -c \"import subprocess; subprocess.run(['git', 'push', 'origin', 'main'])\"",
    "python3 -Ic \"import os; os.system('git push --force')\"",
    "node -e \"require('child_process').execSync('git push origin main')\"",
    "node --eval=\"require('child_process').execSync('git push origin main')\"",
    "perl -ne 'system(\"git push origin main\")'", "ruby -e 'system(\"git push -f\")'",
    "php -r 'exec(\"git push origin main\");'", "awk 'BEGIN { system(\"git push origin main\") }'",
    "python3 - <<'EOF'\nimport os\nos.system('git push origin main')\nEOF",
    "cat <<'EOF' | python3\nimport os\nos.system('git push origin main')\nEOF",
    "bash <<'EOF'\ngit push origin main\nEOF",
    "python3 -c \"import smtplib; smtplib.SMTP('mail.example.org').sendmail('a', 'b', 'c')\"",
])
def test_inline_code_is_still_judged(command):
    assert git_guard.judge("Bash", command, None, "/tmp"), command


def test_inline_code_finds_only_the_code():
    assert git_guard.inline_code("python3", ["-c", "x", "script.py"]) == ["x"]
    assert git_guard.inline_code("python3", ["script.py", "-c", "x"]) == []
    assert git_guard.inline_code("python3", ["-m", "pytest", "-c", "x"]) == []
    assert git_guard.inline_code("perl", ["-n", "-e", "a", "-E", "b", "file"]) == ["a", "b"]
    assert git_guard.inline_code("gawk", ["-F", ":", "{print}", "file"]) == ["{print}"]
    assert git_guard.inline_code("awk", ["-f", "prog.awk", "file"]) == []
    assert git_guard.inline_code("deno", ["eval", "x"]) == ["x"]
    assert git_guard.inline_code("deno", ["run", "x.ts"]) == []


# --- #3 known sending endpoints ----------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "curl -X POST https://api.github.com/graphql -d '{\"query\": \"{ viewer { login } }\"}'",
    "gh api graphql -f query='{ viewer { login } }'",
    "curl https://api.openai.com/v1/embeddings -H 'Content-Type: application/json' -d '{\"input\": \"x\"}'",
    "curl -X POST https://api.anthropic.com/v1/messages -d @req.json",
    "http POST api.example.com/search q=x", "curl -X PUT https://storage.example.org/x -T f",
    "curl -X POST $API_URL -d x", "curl https://slack.com/api/conversations.history",
    "curl -X POST https://slack.com/api/conversations.list",
    "python3 -c \"import requests; requests.post('https://api.openai.com/v1/embeddings', json={})\"",
    "node -e \"fetch('https://api.example.com/graphql', {method: 'POST', body: '{}'})\"",
])
def test_reading_apis_with_post_do_not_ask(command):
    assert git_guard.judge("Bash", command, None, "/tmp") is None, command


@pytest.mark.parametrize("command", [
    "curl -X POST -d '{\"text\": \"hi\"}' https://hooks.slack.com/services/T/B/X",
    "curl -X POST https://slack.com/api/chat.postMessage -d 'channel=C&text=hi'",
    "curl 'https://api.telegram.org/bot123:abc/sendMessage?chat_id=1&text=hi'",  # GET sends too
    "curl -X POST https://discord.com/api/webhooks/1/abc -d '{}'",
    "curl -X POST https://graph.microsoft.com/v1.0/me/sendMail -d @mail.json",
    "curl -X POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send -d @m.json",
    "curl -X POST https://api.sendgrid.com/v3/mail/send -d @m.json",
    "curl -s --user api:k https://api.mailgun.net/v3/x/messages -F to=a@b.c",
    "curl -X POST https://api.postmarkapp.com/email -d @m.json", "curl -X POST https://api.resend.com/emails -d x",
    "curl -X POST https://api.twilio.com/2010-04-01/Accounts/A/Messages.json -d x",
    "curl -X POST https://api.x.com/2/tweets -d x", "curl -X POST https://api.twitter.com/2/tweets -d x",
    "curl -X POST https://graph.facebook.com/v19.0/me/feed -d x",
    "curl -X POST https://api.linkedin.com/v2/ugcPosts -d x", "curl -X POST https://x.webhook.office.com/a -d x",
    "curl -X POST $SLACK_WEBHOOK_URL -d x", "http POST hooks.slack.com/services/x text=hi",
    "python3 -c \"import requests; requests.post('https://hooks.slack.com/services/x', json={})\"",
    "sendmail owner@example.com < note.txt",
])
def test_known_sending_endpoints_ask(command):
    assert git_guard.judge("Bash", command, None, "/tmp"), command


def test_the_reason_names_the_host_not_the_secret_path():
    reason = git_guard.judge("Bash", "curl -X POST https://discord.com/api/webhooks/1/secret-part -d x", None, "/tmp")
    assert "discord.com" in reason and "secret-part" not in reason


def test_more_sending_endpoints_from_the_environment(monkeypatch):
    assert git_guard.judge("Bash", "curl -X POST https://notify.example.org/v1/push -d x", None, "/tmp") is None
    monkeypatch.setenv("BOT_OFFICE_SEND_HOSTS", "notify.example.org/v1/push, *.chat.example.com")
    assert git_guard.judge("Bash", "curl -X POST https://notify.example.org/v1/push -d x", None, "/tmp")
    assert git_guard.judge("Bash", "curl -X POST https://notify.example.org/v1/status -d x", None, "/tmp") is None
    assert git_guard.judge("Bash", "curl -X POST https://eu.chat.example.com/m -d x", None, "/tmp")
    assert git_guard.judge("Bash", "curl https://eu.chat.example.com/m", None, "/tmp") is None  # a GET
