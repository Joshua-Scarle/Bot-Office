"""0.12.4 git guard: pushes to the default branch, merges, force-pushes, deletes and repo settings
always wait for the owner, in every run kind, whatever the rules, classes or bot access say."""
import json
import os
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from botoffice import approval_classes, git_guard
from botoffice import rules as rules_mod
from botoffice.api import create_app
from botoffice.domain import Job, JobStatus
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.rules import Rules
from botoffice.store import JobStore

BIN = Path(__file__).resolve().parents[1] / "bin"
WORK = "/srv/bots/ada/work"  # a workspace that need not exist: nothing here reads the disk


def bash(command, cwd=None):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    if cwd:
        event["cwd"] = cwd
    return event


def guarded(command, cwd=WORK):
    return git_guard.classify("Bash", command, None, cwd)


PUSH_TO_DEFAULT = [
    "git push origin main", "git push origin master", "git push origin HEAD:main", "git push origin HEAD:refs/heads/main",
    "git push origin feature:main", "git push origin +feature:main", "git push origin refs/heads/main",
    "git push -u origin main", "git push --set-upstream origin main", "git push origin main:main",
    "git push origin :main", "git push origin trunk", "git push origin MAIN", "git push upstream main",
    "git push main", "git push origin 'main'", 'git push origin "ma"in', r"git push origin ma\in",
    "git push origin refs/for/main", "git push origin 'HEAD:main'", "git push ssh://x/y.git main",
    "git push --all", "git push --all origin", "git push --mirror", "git push --mirror origin",
    "git push --prune origin 'refs/heads/*:refs/heads/*'", "git push origin 'refs/heads/*:refs/heads/*'",
    "git push origin feature $BRANCH", "git push origin $BRANCH", "git push origin $(git branch --show-current)",
    "git push origin `git rev-parse --abbrev-ref HEAD`", "git push origin {main,x}",
    # force, delete
    "git push --force origin feature", "git push -f origin feature", "git push --force-with-lease origin feature",
    "git push --force-with-lease=feature:abc origin feature", "git push --force-if-includes origin feature",
    "git push origin +feature", "git push origin +HEAD", "git push -uf origin feature", "git push --forc origin feature",
    "git push origin --delete feature", "git push origin -d feature", "git push origin :feature",
    "git push --del origin feature", "git push origin feature --force",
    "git tag -d v1.0", "git tag --delete v1.0 && git push origin :refs/tags/v1.0", "git push origin :refs/tags/v1.0",
    "git push --delete origin v1.0",
    "git branch -D main", "git branch -d master", "git branch -D origin/main", "git update-ref -d refs/heads/main",
    "git send-pack origin main", "git http-push http://x/y.git main",
    # wrappers and prefixes
    "GIT_TERMINAL_PROMPT=0 git push origin main", "FOO=1 BAR=2 git push origin main", "env GIT_X=1 git push origin main",
    "git -C /srv/repo push origin main", "git -C repo push origin main", "git -c user.name=x push origin main",
    "git --no-pager push origin main", "git -c core.sshCommand=ssh -C . push origin main", "git --git-dir=.git push origin main",
    "/usr/bin/git push origin main", "command git push origin main", "sudo git push origin main", "nohup git push origin main &",
    "time git push origin main", "timeout 20 git push origin main", "xargs -I{} git push origin {}",
    "sh -c 'git push origin main'", "bash -c \"git push origin main\"", "bash -lc 'cd x && git push origin main'",
    "sh -c \"sh -c 'git push origin main'\"", "eval 'git push origin main'", "eval git push origin main",
    "ssh host 'git push origin main'", "docker exec c sh -c 'git push origin main'",
    "echo hi; git push origin main", "true && git push origin main", "false || git push origin main",
    "ls | git push origin main", "git status; git push --force", "echo $(git push origin main)", "echo `git push origin main`",
    "echo \"$(git push origin main)\"", "(git push origin main)", "if true; then git push origin main; fi",
    "for b in a b; do git push origin main; done", "{ git push origin main; }",
    "git push origin feature\ngit push origin main", "git push \\\n origin \\\n main",
    "cd /tmp/x && git push origin main", "git commit -am x && git push origin master",
    "python3 -c \"import subprocess; subprocess.run(['git','push','origin','main'])\"",
    "python - <<'EOF'\nimport os\nos.system('git push origin main')\nEOF",
    "node -e \"require('child_process').execSync('git push origin main')\"",
    "perl -e 'system(\"git\", \"push\", \"origin\", \"main\")'",
    # implicit pushes where the branch is not known
    "git push", "git push origin", "git push -u origin HEAD", "git push origin HEAD", "git push origin @",
    "git push --follow-tags", "git push --tags",
]

GH_AND_API = [
    "gh pr merge 5", "gh pr merge --squash --auto 5", "gh pr merge", "gh -R o/r pr merge 5", "gh pr merge 5 -R o/r",
    "GH_TOKEN=x gh pr merge 5", "gh pr close 5 --delete-branch", "gh pr close 5 -d",
    "gh repo delete o/r --yes", "gh repo edit --default-branch dev", "gh repo edit --visibility public --accept-visibility-change-consequences",
    "gh repo rename new", "gh repo archive o/r", "gh repo unarchive o/r", "gh repo transfer", "gh repo sync",
    "gh repo deploy-key add key.pub", "gh repo create x --push --source .",
    "gh release delete v1 --yes", "gh release delete-asset v1 a.zip",
    "gh secret set X", "gh variable delete X", "gh workflow disable ci.yml",
    "gh api -X DELETE repos/o/r", "gh api --method DELETE repos/o/r/git/refs/heads/x", "gh api --method=DELETE repos/o/r",
    "gh api -XDELETE repos/o/r", "gh api -X=DELETE repos/o/r", "gh api -X delete repos/o/r",
    "gh api repos/o/r -X PATCH -f default_branch=dev", "gh api -X PATCH repos/o/r -f archived=true",
    "gh api -X PUT repos/o/r/pulls/5/merge", "gh api -X PUT repos/o/r/branches/main/protection --input p.json",
    "gh api -X PUT /repos/o/r/contents/a.txt -f message=m -f content=Zg==", "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
    "gh api -X DELETE repos/o/r/git/refs/tags/v1", "gh api -X POST repos/o/r/merges -f base=main -f head=x",
    "gh api repos/o/r/merges -f base=main -f head=x", "gh api repos/o/r/rulesets -f name=x", "gh api -X POST repos/o/r/hooks -f name=web",
    "gh api repos/o/r/merge-upstream -f branch=main", "gh api -X PATCH orgs/o -f x=1", "gh api -X DELETE repositories/123",
    "gh api -X PUT repos/o/r/actions/secrets/X -f encrypted_value=x", "gh api -X $M repos/o/r", "gh api -X DELETE $EP",
    "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"x\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation{deleteRef(input:{refId:\"x\"}){clientMutationId}}'",
    "gh api graphql -f query='mutation { updateRepository(input:{repositoryId:\"x\", hasWikiEnabled:false}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { createCommitOnBranch(input:{}) { commit { oid } } }'",
    "gh alias set m 'pr merge'", "sh -c 'gh pr merge 5'", "gh api -X DELETE repos/o/r | cat",
    "curl -X DELETE https://api.github.com/repos/o/r", "curl -XDELETE https://api.github.com/repos/o/r/git/refs/heads/x",
    "curl --request DELETE https://api.github.com/repos/o/r", "curl --request=PATCH https://api.github.com/repos/o/r -d '{}'",
    "curl -sSX DELETE https://api.github.com/repos/o/r", "curl -H 'Authorization: token x' -X PUT https://api.github.com/repos/o/r/pulls/5/merge",
    "curl -X PATCH -d '{\"private\":false}' https://api.github.com/repos/o/r",
    "curl -X POST https://api.github.com/repos/o/r/merges -d '{}'", "curl -d '{\"base\":\"main\"}' https://api.github.com/repos/o/r/merges",
    "curl -T f https://uploads.github.com/repos/o/r/contents/x", "curl -X POST https://api.github.com/graphql -d '{\"query\":\"mutation{mergePullRequest(input:{})}\"}'",
    "curl -X DELETE \"$API/repos/o/r\"", "curl -X DELETE $URL", "curl -X DELETE https://${GITHUB_API_URL}/repos/o/r",
    "wget --method=DELETE https://api.github.com/repos/o/r", "wget --method DELETE https://api.github.com/repos/o/r",
    "wget --method=PATCH --body-data='{}' https://api.github.com/repos/o/r", "wget -qO- --post-data='x' https://api.github.com/repos/o/r/merges",
    "http DELETE https://api.github.com/repos/o/r", "xh patch https://api.github.com/repos/o/r private:=false",
    "python3 -c \"import requests; requests.delete('https://api.github.com/repos/o/r')\"",
    "python3 -c \"import subprocess; subprocess.run(['gh','pr','merge','5'])\"",
    "node -e \"fetch('https://api.github.com/repos/o/r', {method: 'PATCH'})\"",
    "ruby -e 'system(\"gh repo delete o/r --yes\")'",
]

DELETES = [
    "rm /etc/hosts", "rm -rf /srv/shared", "rm -rf ~/project", "rm -rf ~", "rm -rf $HOME/x", "rm -rf ../other", "rm -r ../../x",
    "rm -rf /tmp/x", "rm /srv/bots/ada/other/file", "rm -rf /", "rm -rf -- /var/x", "rm -f /root/.bashrc", "rm -rf \"$TMPDIR/x\"",
    "rm -rf $(dirname $0)/..", "rm $(find / -name x)", "unlink /etc/x", "rmdir /srv/x", "shred -u /home/x/secret", "trash-put /home/u/x",
    "gio trash /home/u/x", "find /srv -name '*.log' -delete", "find / -type f -exec rm {} \\;", "find .. -delete", "find ~ -name x -delete",
    "cd /etc && rm passwd", "cd .. && rm -rf other", "cd ~ && rm x", "cd \"$OLD\" && rm x", "pushd /srv && rm x",
    "rsync -a --delete src/ host:/srv/x/", "rsync -a --delete ./a/ /srv/b/", "rsync --remove-source-files /srv/a ./b",
    "mv /srv/shared/data /dev/null", "sudo rm -rf /srv/x", "sh -c 'rm -rf /srv/x'", "echo y | rm -r /var/x",
    "xargs rm < list.txt && rm /srv/x", "python3 -c \"import shutil; shutil.rmtree('/srv/x')\"",
    "python3 -c \"import os; os.remove('/etc/x')\"", "python3 -c \"import os; os.unlink('../x')\"",
    "apply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: /srv/x/a.txt\n*** End Patch\nEOF",
]

SAFE = [
    "git push origin feature", "git push origin feature/new-login", "git push -u origin feature/x", "git push origin fix-main-menu",
    "git push origin maintenance", "git push origin main-menu", "git push origin feature/main", "git push origin bugfix/master-key",
    "git push origin HEAD:feature", "git push origin HEAD:refs/heads/feature", "git push origin feature:feature2",
    "git push origin main:feature", "git push --set-upstream origin my-branch", "git push origin tag-v1", "git push -u origin ada/work-1",
    "git push origin refs/heads/feature", "git push --dry-run origin feature", "git push -n origin feature", "git push origin feature -v",
    "git push --no-verify origin feature", "git push --porcelain origin feature", "git push -o ci.skip origin feature",
    "git -C repo push origin feature", "git -c http.extraheader=x push origin feature", "FOO=1 git push origin feature",
    "git push origin feature && git status", "git status", "git log --oneline main", "git log --grep=push main", "git diff main",
    "git checkout main", "git checkout -b feature", "git merge main", "git pull origin main", "git pull", "git fetch origin main",
    "git rebase main", "git branch feature", "git branch -d feature", "git branch -D old-work", "git branch -a", "git branch --list 'd*'",
    "git tag v1.0", "git tag -a v1 -m 'release notes: push main'", "git tag -l", "git commit -m 'git push origin main'",
    "git commit -m \"fix: gh pr merge flow\"", "git commit -am 'do not rm -rf /'", "git stash", "git show main:README.md",
    "git clone https://github.com/o/r.git", "git remote -v", "git diff origin/main...HEAD", "git reset --hard", "git clean -fd",
    "echo git push origin main", "echo 'git push origin main'", "grep -rn 'push origin main' docs", "cat README.md",
    "gh pr create --fill", "gh pr create --title 'merge main into feature' --body 'pr merge'", "gh pr view 5", "gh pr list",
    "gh pr comment 5 --body 'ok to merge'", "gh pr checkout 5", "gh pr diff 5", "gh pr review 5 --approve", "gh pr close 5",
    "gh issue create --title x", "gh issue comment 5 --body hi", "gh issue list", "gh repo view", "gh repo clone o/r", "gh repo fork",
    "gh repo list", "gh repo create new-repo --private", "gh release list", "gh release view v1", "gh release create v1", "gh run list",
    "gh api repos/o/r", "gh api repos/o/r/pulls/5", "gh api repos/o/r/issues/5/comments", "gh api -X GET repos/o/r/branches",
    "gh api repos/o/r/issues -f title=x", "gh api -X POST repos/o/r/issues -f title=x", "gh api -X PATCH repos/o/r/issues/5 -f state=closed",
    "gh api -X POST repos/o/r/issues/5/comments -f body=x", "gh api user", "gh api graphql -f query='{ viewer { login } }'",
    "gh api graphql -f query='query { repository(owner:\"o\", name:\"r\") { id } }'", "gh auth status", "gh secret list", "gh variable list",
    "curl https://api.github.com/repos/o/r", "curl -s https://api.github.com/repos/o/r/pulls", "curl -X GET https://api.github.com/repos/o/r",
    "curl -X POST https://api.github.com/repos/o/r/issues -d '{}'", "curl -X POST https://example.com/x -d y", "curl -X DELETE https://example.com/x",
    "curl -X PUT https://storage.example.com/bucket/x -T f", "curl http://127.0.0.1:8787/v1/status", "wget https://github.com/o/r/archive/main.zip",
    "wget -O x https://example.com", "curl -L https://github.com/o/r/raw/main/README.md",
    "rm -rf build", "rm file.txt", "rm -rf ./node_modules", "rm -rf dist/ .cache", "rm -f outbox/draft.txt", "rm -rf sub/../other",
    "rm -rf $BUILD_DIR/x", "rm \"$f\"", "rm -rf /dev/null", "find . -name '*.pyc' -delete", "find build -type f -delete", "find /srv -name x",
    "ls /srv/x", "cat /etc/hosts", "cp a b", "mv a b", "mkdir -p /tmp/x", "docker rm c1", "git rm old.py", "npm rm lodash", "cd sub && rm x",
    "cd .. && ls", "rsync -a a/ b/", "rsync -a --delete ./a/ ./b/", "echo rm -rf", "python3 -c \"print('hello')\"", "python3 build.py",
    "pytest -q", "ls | grep push", "sudo -n true", "make deploy", "npm test",
]


@pytest.mark.parametrize("command", PUSH_TO_DEFAULT)
def test_pushes_to_the_default_branch_and_force_and_delete_are_guarded(command):
    assert guarded(command), command


@pytest.mark.parametrize("command", GH_AND_API)
def test_merges_repo_changes_and_github_api_writes_are_guarded(command):
    assert guarded(command), command


@pytest.mark.parametrize("command", DELETES)
def test_deleting_outside_the_workspace_is_guarded(command):
    assert guarded(command), command


@pytest.mark.parametrize("command", [c for c in SAFE if c not in ("git push -u origin ada/work-1",)])
def test_ordinary_work_is_not_asked(command):
    assert guarded(command) is None, (command, guarded(command))


def test_implicit_pushes_are_judged_from_the_repo(tmp_path):
    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})

    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=work)
    git("remote", "add", "origin", str(bare), cwd=work)
    root = str(work)
    # on main: every implicit form goes to the default branch
    for command in ("git push", "git push origin", "git push -u origin HEAD", "git push origin HEAD", "git push origin @"):
        assert guarded(command, root), command
    git("checkout", "-q", "-b", "feature/x", cwd=work)
    for command in ("git push", "git push origin", "git push -u origin HEAD", "git push origin HEAD", "git push origin @",
                    "git push -u origin feature/x"):
        assert guarded(command, root) is None, command
    assert guarded("cd . && git push", root) is None
    assert guarded(f"git -C {root} push", "/somewhere/else") is None
    assert guarded("git push", "/somewhere/else"), "a directory that is not a repo cannot be judged: ask"
    assert guarded("cd \"$X\" && git push", root), "a directory not known until run time: ask"
    # push.default matching pushes main too; an upstream on main; a remote with its own refspec
    assert guarded("git -c push.default=matching push", root)
    git("config", "push.default", "upstream", cwd=work)
    assert guarded("git push", root)
    git("config", "--unset", "push.default", cwd=work)
    git("config", "branch.feature/x.remote", "origin", cwd=work)
    git("config", "branch.feature/x.merge", "refs/heads/main", cwd=work)
    assert guarded("git push", root)
    git("config", "--unset", "branch.feature/x.merge", cwd=work)
    git("config", "--unset", "branch.feature/x.remote", cwd=work)
    assert guarded("git push", root) is None
    git("config", "remote.origin.push", "refs/heads/*:refs/heads/*", cwd=work)
    assert guarded("git push", root)
    git("config", "--unset", "remote.origin.push", cwd=work)
    # a repo whose default branch is trunk
    git("checkout", "-q", "-b", "develop", cwd=work)
    git("update-ref", "refs/remotes/origin/develop", "HEAD", cwd=work)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop", cwd=work)
    assert guarded("git push", root), "origin's HEAD names develop: it is the default branch"
    # aliases: a push alias is a push; an unknown name in an unreadable place is asked about
    git("checkout", "-q", "-b", "feature/y", cwd=work)
    git("config", "alias.pm", "push origin main", cwd=work)
    git("config", "alias.sh", "!git push origin main", cwd=work)
    git("config", "alias.ok", "status", cwd=work)
    assert guarded("git pm", root) and guarded("git sh", root) and guarded("git ok", root) is None
    assert guarded("git frobnicate", root) is None and guarded("git frobnicate", None)
    assert guarded("git -c alias.x='push origin main' x", root)


def test_scripts_the_command_runs_are_read(tmp_path):
    (tmp_path / "release.sh").write_text("#!/bin/sh\nset -e\ngit push origin main\n")
    (tmp_path / "py.py").write_text("import subprocess\nsubprocess.run(['git', 'push', 'origin', 'main'])\n")
    (tmp_path / "ok.sh").write_text("#!/bin/sh\ngit status\nls\n")
    root = str(tmp_path)
    for command in ("bash release.sh", "sh ./release.sh", "bash -e release.sh", f"bash {tmp_path}/release.sh",
                    "source release.sh", ". ./release.sh", "python3 py.py", "python py.py --x"):
        assert guarded(command, root), command
    assert guarded("bash ok.sh", root) is None and guarded("bash missing.sh", root) is None


@pytest.mark.parametrize("tool,data", [
    ("mcp__github__merge_pull_request", {"pullNumber": 5}),
    ("mcp__github__delete_branch", {"branch": "x"}), ("mcp__github__delete_ref", {"ref": "heads/x"}),
    ("mcp__github__delete_tag", {}), ("mcp__github__delete_repository", {}), ("mcp__github__delete_release", {}),
    ("mcp__github__delete_file", {"path": "a", "branch": "feature"}),
    ("mcp__github__update_repository", {"private": False}), ("mcp__github__update_repo_settings", {}),
    ("mcp__github__update_ref", {"ref": "heads/main"}), ("mcp__github__update_branch_protection", {}),
    ("mcp__github__create_ruleset", {}), ("mcp__github__create_webhook", {}), ("mcp__github__archive_repository", {}),
    ("mcp__github__transfer_repository", {}), ("mcp__github__rename_repository", {}),
    ("mcp__github__set_default_branch", {"branch": "dev"}), ("mcp__github__force_push", {}),
    ("mcp__github__push_files", {"branch": "main", "files": []}), ("mcp__github__push_files", {"files": []}),
    ("mcp__github__create_or_update_file", {"branch": "master", "path": "a"}), ("mcp__github__create_or_update_file", {"path": "a"}),
    ("mcp__gitlab__merge_merge_request", {}), ("mcp__gitlab__accept_merge_request", {"merge": True}),
    ("mcp__github__enable_auto_merge", {}), ("MergePullRequest", {}),
    ("mcp__fs__delete_file", {"path": "/etc/hosts"}), ("mcp__fs__remove_directory", {"path": "../x"}),
    ("mcp__fs__delete_file", {}), ("mcp__github__github_delete_branch", {}), ("mcp__x__deleteBranch", {}),
    ("mcp__x__updateRepository", {}),
])
def test_connector_tools_that_merge_delete_or_change_settings_are_guarded(tool, data):
    if not tool.lower().startswith("mcp"):
        pytest.skip("not a connector tool")
    assert git_guard.classify(tool, "", data, WORK), tool


@pytest.mark.parametrize("tool,data", [
    ("mcp__github__create_pull_request", {"title": "x", "head": "feature"}), ("mcp__github__get_pull_request", {}),
    ("mcp__github__list_branches", {}), ("mcp__github__create_branch", {"branch": "feature"}),
    ("mcp__github__create_issue", {}), ("mcp__github__add_issue_comment", {}), ("mcp__github__update_pull_request", {}),
    ("mcp__github__update_issue", {}), ("mcp__github__push_files", {"branch": "feature/x", "files": []}),
    ("mcp__github__create_or_update_file", {"branch": "feature", "path": "a"}), ("mcp__github__get_file_contents", {}),
    ("mcp__github__create_repository", {"name": "x"}), ("mcp__github__search_code", {}), ("mcp__github__fork_repository", {}),
    ("mcp__gmail__delete_message", {}), ("mcp__gmail__send_message", {}), ("mcp__fs__delete_file", {"path": "tmp/x.txt"}),
    ("mcp__fs__read_file", {"path": "/etc/hosts"}), ("Read", {"file_path": "/etc/hosts"}), ("Bash", {}), ("Write", {"file_path": "a"}),
])
def test_ordinary_connector_tools_are_not_asked(tool, data):
    assert git_guard.classify(tool, "", data, WORK) is None, tool


def test_a_command_it_cannot_read_is_a_hit_never_a_crash(monkeypatch):
    assert git_guard.classify("Bash", "x" * (git_guard.SCAN_LIMIT + 1)) == "a command too long to check"
    assert git_guard.classify("Bash", "(" * 100 + "git push origin main" + ")" * 100)
    assert git_guard.classify("Bash", "$(" * 20 + "git push origin main" + ")" * 20)
    assert git_guard.classify("Bash", "echo '" + "a" * 5000) is None  # an unterminated quote is just text
    monkeypatch.setattr(git_guard, "_scan_command", lambda *a: 1 / 0)
    assert git_guard.classify("Bash", "ls") == "a command that could not be read"


# --- the decision: above every rule, class, setting and run kind ---

SOURCES = ("chat", "routine", "wakeup", "assignment", "inbox", "trigger", "api")
COMMANDS = ["git push origin main", "git push --force origin feature", "gh pr merge 7", "gh repo delete o/r --yes",
            "curl -X DELETE https://api.github.com/repos/o/r", "git push origin --delete feature", "rm -rf /srv/shared"]


def make_store(tmp_path, scheduled_runs="full", cli_access="full", provider="claude"):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", provider, None)
    store.update_bot(bot.id, scheduled_runs=scheduled_runs, cli_access=cli_access)
    return store, store.get_bot(bot.id)


def running(store, bot, source="chat", origin=None):
    job = store.create("work", bot.default_provider, bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running', origin_job_id=? WHERE id=?",
                   (str(origin) if origin else None, str(job.id)))
    return store.get(job.id)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("scheduled_runs,cli_access", [("full", "full"), ("gated", "full"), ("full", "restricted"),
                                                       ("gated", "restricted")])
def test_every_run_kind_raises_a_card_for_every_guarded_command(tmp_path, source, scheduled_runs, cli_access):
    store, bot = make_store(tmp_path, scheduled_runs, cli_access)
    rules = Rules(store)
    job = running(store, bot, source)
    for command in COMMANDS:
        decision = rules.decide(job, bash(command, WORK))
        assert (decision.decision, decision.effect, decision.preset) == ("deny", "ask", "git-guard"), command
        assert decision.approval_id and "waiting_on_user" in decision.reason
        card = rules.get(decision.approval_id)
        assert card["status"] == "pending" and command in card["summary"]
    # one card per distinct command, each with its Approve / Deny chat message
    with closing(store._connect()) as db:
        cards = db.execute("SELECT COUNT(*) FROM messages WHERE approval_id IS NOT NULL").fetchone()[0]
    assert cards == len(COMMANDS)
    # the same job asking again reuses the card; ordinary commands are untouched
    assert rules.decide(job, bash(COMMANDS[0], WORK)).approval_id == rules.get(1)["id"]
    assert rules.decide(job, bash("git status", WORK)).decision == "allow"
    assert rules.decide(job, bash("rm -rf build", WORK)).decision == "allow"
    if not rules.strict(job):  # the strict unattended profile asks for every push, as before
        assert rules.decide(job, bash("git push origin feature", WORK)).decision == "allow"


def test_nothing_the_owner_can_set_opens_the_guard(tmp_path):
    store, bot = make_store(tmp_path)
    rules = Rules(store)
    # every approval class off, the timeout off, an allow rule for each kind
    approval_classes.update(store, enabled={item.key: False for item in approval_classes.BUILTIN}, timeout_min=0)
    for kind, pattern in (("preset", "outbound"), ("preset", "destructive"), ("shell", r".*"), ("shell", r"\bgit\b"),
                          ("path", r".*"), ("tool", r".*")):
        rules.add_rule(bot_id=None, effect="allow", kind=kind, pattern=pattern, scope="all")
        rules.add_rule(bot_id=bot.id, effect="allow", kind=kind, pattern=pattern, scope="all")
    for source in SOURCES:
        job = running(store, bot, source)
        for command in COMMANDS:
            decision = rules.evaluate(job, rules_mod.parse_event(bash(command, WORK)))
            assert decision[:2] == ("ask", "git-guard"), (source, command)
    # a rule that forbids is stricter and still wins
    rules.add_rule(bot_id=None, effect="never", kind="shell", pattern=r"gh pr merge")
    assert rules.evaluate(running(store, bot), rules_mod.parse_event(bash("gh pr merge 1", WORK)))[0] == "never"
    # the settings page names the guard as locked, and no setting can address it
    assert approval_classes.settings(store)["locked"]["enabled"] is True
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, enabled={"git-guard": False})
    with pytest.raises(approval_classes.ApprovalClassError):
        approval_classes.update(store, remove=["git-guard"])


def test_an_approved_card_runs_exactly_that_command_once_and_in_the_resume_turn_only(tmp_path):
    store, bot = make_store(tmp_path)
    rules = Rules(store)
    for source in ("chat", "routine", "wakeup", "trigger"):
        origin = running(store, bot, source)
        asked = rules.decide(origin, bash("git push origin main", WORK))
        assert asked.decision == "deny"
        # approving makes a grant for the resume turn only: not the origin, not another command, not twice
        assert rules.decide(origin, bash("git push origin main", WORK)).decision == "deny"
        store.finish(origin.id, JobStatus.INCOMPLETE)
        resume_id = rules.approve(asked.approval_id)["resume_job_id"]
        with closing(store._connect()) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (resume_id,))
        resume = store.get(resume_id)
        assert rules.decide(resume, bash("git push origin master", WORK)).decision == "deny"
        assert rules.decide(resume, bash("git push --force origin main", WORK)).decision == "deny"
        assert rules.decide(resume, bash("git push origin main", WORK)).decision == "allow"
        assert rules.decide(resume, bash("git push origin main", WORK)).decision == "deny"


def test_a_guarded_command_over_the_card_limit_is_refused_not_run(tmp_path):
    store, bot = make_store(tmp_path)
    decision = Rules(store).decide(running(store, bot), bash("git push origin main # " + "x" * 5000, WORK))
    assert decision.decision == "deny" and decision.effect == "never" and decision.approval_id is None


def test_ask_cards_wait_in_needs_you_with_the_guard_label(tmp_path):
    from botoffice.needs_you import needs_you
    store, bot = make_store(tmp_path)
    Rules(store).decide(running(store, bot, "routine"), bash("gh pr merge 3", WORK))
    items = [item for item in needs_you(store) if item["kind"] == "approval"]
    assert items and items[0]["class"] == git_guard.LABEL and "gh pr merge 3" in items[0]["ask"]


def test_codex_unattended_and_attended_both_wire_the_hook_and_restricted_codex_gets_the_shims(tmp_path):
    base = {"id": "00000000-0000-4000-8000-000000000001", "prompt": "go", "status": JobStatus.RUNNING,
            "created_at": __import__("datetime").datetime.now(),
            "bot_id": "00000000-0000-4000-8000-000000000002",
            "gate_command": "/repo/bin/bot-office-gate codex --mode attended", "secret_paths": []}
    adapter = CodexAdapter(executable="codex")
    full = adapter._exec_args(tmp_path, Job(**base))
    assert "--dangerously-bypass-approvals-and-sandbox" in full  # full access is still the default
    assert any(arg.startswith("hooks.PreToolUse=") and "bot-office-gate" in arg for arg in full)
    resumed = adapter._exec_args(tmp_path, Job(**base), "0199a213-81c0-7800-8aa1-bbab2a035a53")
    assert any(arg.startswith("hooks.PreToolUse=") for arg in resumed)
    assert "BOT_OFFICE_GUARD_SHIM" not in adapter._env(tmp_path, Job(**base))
    restricted = Job(**{**base, "cli_access": "restricted"})
    assert adapter._env(tmp_path, restricted)["BOT_OFFICE_GUARD_SHIM"] == "1"
    assert OpenCodeAdapter(executable="opencode")._env(tmp_path, Job(**base))["BOT_OFFICE_GUARD_SHIM"] == "1"


# --- the scripts: the hook's local fallback and the PATH shims, against a real controller ---

def run_script(script, args, event=None, env=None, cwd=None):
    result = subprocess.run([sys.executable, "-I", str(BIN / script), *args],
                            input=json.dumps(event).encode() if event is not None else None,
                            env={"PATH": "/usr/bin:/bin", **(env or {})}, cwd=cwd, capture_output=True, timeout=60,
                            check=False)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


def test_the_hook_refuses_a_guarded_command_when_the_controller_cannot_be_asked():
    dead = {"BOT_OFFICE_AGENT_TOKEN": "t", "BOT_OFFICE_API_URL": "http://127.0.0.1:9"}
    for provider in ("claude", "codex", "cursor", "antigravity"):
        code, _, err = run_script("bot-office-gate", [provider, "--mode", "attended"], bash("git push origin main", WORK), dead)
        assert code == 2, provider
    code, _, err = run_script("bot-office-gate", ["claude", "--mode", "attended"], bash("gh pr merge 5", WORK), dead)
    assert code == 2 and "needs the owner's approval" in err
    # with no token at all in an attended run: same
    assert run_script("bot-office-gate", ["claude", "--mode", "attended"], bash("git push --force", WORK), {})[0] == 2
    # everything else still fails open when attended
    assert run_script("bot-office-gate", ["claude", "--mode", "attended"], bash("git push origin feature", WORK), dead)[0] == 0
    assert run_script("bot-office-gate", ["claude", "--mode", "attended"], bash("ls", WORK), dead)[0] == 0
    # Joshua's own CLI (no token, no mode): untouched
    assert run_script("bot-office-gate", ["claude"], bash("git push origin main", WORK), {})[0] == 0


@pytest.fixture
def live(settings):
    import uvicorn

    app = create_app(settings, {"claude": FakeAdapter(enabled=False)})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    yield app, f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=10)


def fake_tools(tmp_path):
    """A directory with a `git` and `gh` that only leave a mark when they really run."""
    directory = tmp_path / "real"
    directory.mkdir()
    for name in ("git", "gh", "curl"):
        program = directory / name
        program.write_text(f"#!/bin/sh\necho \"{name} $@\" >> {tmp_path}/ran.log\n")
        program.chmod(0o755)
    return directory


@pytest.mark.parametrize("source", ["chat", "routine", "wakeup", "assignment", "inbox", "trigger"])
def test_the_shim_stops_a_guarded_command_until_the_card_is_approved(live, tmp_path, source):
    app, url = live
    store = app.state.store
    bot = store.create_bot("Ada", "", "claude", None)
    store.update_bot(bot.id, cli_access="restricted")
    bot = store.get_bot(bot.id)
    origin = running(store, bot, source)
    real = fake_tools(tmp_path)
    ran = tmp_path / "ran.log"

    def env_for(job):
        return {"PATH": f"{BIN}:{real}:/usr/bin:/bin", "BOT_OFFICE_GUARD_SHIM": "1", "BOT_OFFICE_API_URL": url,
                "BOT_OFFICE_AGENT_TOKEN": store.issue_agent_token(job)}

    def shim(name, args, job):
        return subprocess.run([str(BIN / name), *args], env=env_for(job), cwd=tmp_path, capture_output=True, timeout=60,
                              text=True)

    # an ordinary push and an ordinary gh call run at once, through the real program
    assert shim("git", ["push", "origin", "feature"], origin).returncode == 0
    assert shim("gh", ["pr", "view", "5"], origin).returncode == 0
    assert ran.read_text().splitlines() == ["git push origin feature", "gh pr view 5"]
    ran.write_text("")
    # the guarded ones stop on a card and the program never runs
    for name, args in (("git", ["push", "origin", "main"]), ("gh", ["pr", "merge", "7"]),
                       ("git", ["push", "--force", "origin", "feature"]),
                       ("curl", ["-X", "DELETE", "https://api.github.com/repos/o/r"])):
        stopped = shim(name, args, origin)
        assert stopped.returncode == 126 and "Approval #" in stopped.stderr, (name, args, stopped.stderr)
    assert ran.read_text() == "", "a guarded command ran before approval"
    pending = [item for item in rules_for(store).list("pending")]
    assert len(pending) == 4 and all(item["preset"] == "git-guard" for item in pending)
    # approve the push: the resume turn can run exactly it, once
    push = next(item for item in pending if item["summary"].endswith("git push origin main"))
    store.finish(origin.id, JobStatus.INCOMPLETE)
    resume_id = rules_for(store).approve(push["id"])["resume_job_id"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (resume_id,))
    resume = store.get(resume_id)
    assert shim("gh", ["pr", "merge", "7"], resume).returncode == 126  # not what was approved
    assert shim("git", ["push", "origin", "main"], resume).returncode == 0
    assert ran.read_text().splitlines() == ["git push origin main"]
    assert shim("git", ["push", "origin", "main"], resume).returncode == 126  # once


def rules_for(store):
    return Rules(store)


def test_the_shim_is_a_plain_pass_through_without_the_marker_and_fails_closed_without_a_controller(tmp_path):
    real = fake_tools(tmp_path)
    env = {"PATH": f"{BIN}:{real}:/usr/bin:/bin"}
    for args in (["push", "origin", "main"], ["push", "--force"]):
        assert subprocess.run([str(BIN / "git"), *args], env=env, cwd=tmp_path, capture_output=True).returncode == 0
    marked = {**env, "BOT_OFFICE_GUARD_SHIM": "1"}
    done = subprocess.run([str(BIN / "git"), "push", "origin", "main"], env=marked, cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 126 and "refused" in done.stderr
    done = subprocess.run([str(BIN / "git"), "push", "origin", "main"], env={**marked, "BOT_OFFICE_AGENT_TOKEN": "t",
                          "BOT_OFFICE_API_URL": "http://127.0.0.1:9"}, cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 126 and "unreachable" in done.stderr
    lines = (tmp_path / "ran.log").read_text().splitlines()
    assert lines == ["git push origin main", "git push --force"]  # only the two unmarked runs


def test_the_hook_script_end_to_end_denies_before_the_command_and_a_grant_lets_the_resume_turn_through(live, tmp_path):
    app, url = live
    store = app.state.store
    bot = store.create_bot("Ada", "", "claude", None)
    origin = running(store, bot, "chat")
    gate = ["claude", "--mode", "attended"]
    env = {"BOT_OFFICE_AGENT_TOKEN": store.issue_agent_token(origin), "BOT_OFFICE_API_URL": url}
    code, _, err = run_script("bot-office-gate", gate, bash("git push origin main", WORK), env)
    assert code == 2 and "Approval #" in err
    assert run_script("bot-office-gate", gate, bash("git push origin feature", WORK), env)[0] == 0
    pending = Rules(store).list("pending")
    store.finish(origin.id, JobStatus.INCOMPLETE)
    resume_id = Rules(store).approve(pending[0]["id"])["resume_job_id"]
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (resume_id,))
    env = {**env, "BOT_OFFICE_AGENT_TOKEN": store.issue_agent_token(store.get(resume_id))}
    assert run_script("bot-office-gate", gate, bash("git push origin main", WORK), env)[0] == 0
    assert run_script("bot-office-gate", gate, bash("git push origin main", WORK), env)[0] == 2


def test_deleting_the_workspace_itself_is_guarded():
    # from inside the workspace, `cd ..` then naming the workspace directory takes the whole workspace
    assert guarded("cd .. && rm -rf work", WORK)
    assert guarded("rm -rf .", WORK)
    assert guarded("rm -rf build", WORK) is None
    assert guarded("echo git push origin main", WORK) is None
