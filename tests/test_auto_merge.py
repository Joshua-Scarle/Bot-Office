"""0.12.31: a ready, reviewed, green PR merges without the owner's card; anything else keeps it."""
import json
from contextlib import closing

import pytest

from botoffice import auto_merge
from botoffice.rules import Rules
from botoffice.store import JobStore

MERGE = " ".join(["gh", "pr", "merge"])
HEAD = "a" * 40
PIN = f"--match-head-commit {HEAD}"


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


def make(tmp_path, source="chat"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    job = store.create("work", "claude", bot_id=bot.id, source=source)
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (str(job.id),))
    return store, store.get(job.id)


def ready_pr(**changes):
    pr = {"state": "open", "draft": False, "base": {"ref": "main"}, "head": {"sha": HEAD},
          "mergeable": True, "mergeable_state": "clean", "labels": [{"name": "status:ready-to-deploy"}],
          "body": "Written-by: claude\nReviewed-by: codex\nTested: pytest 120 passed"}
    pr.update(changes)
    return pr


def fake_github(pr, runs=None, status=None, comments=None):
    runs = runs if runs is not None else [{"name": "ci", "status": "completed", "conclusion": "success"}]

    def fetch(path, _token):
        if path.endswith("/pulls/7"):
            return pr
        if "/check-runs" in path:
            return {"total_count": len(runs), "check_runs": runs}
        if path.endswith("/status"):
            return status or {"total_count": 0, "state": "pending"}
        if "/comments" in path:
            return comments or []
        raise OSError(path)
    return fetch


@pytest.mark.parametrize("command,expected", [
    (f"{MERGE} 7 --repo acme/widgets --squash {PIN}", ("acme", "widgets", 7, HEAD)),
    (f"{MERGE} https://github.com/acme/widgets/pull/7 --merge --delete-branch {PIN}", ("acme", "widgets", 7, HEAD)),
    (f"{MERGE} 7 -R acme/widgets --match-head-commit={HEAD}", ("acme", "widgets", 7, HEAD)),
])
def test_plain_merges_parse(command, expected):
    assert auto_merge.parse(command) == expected


@pytest.mark.parametrize("command", [
    f"{MERGE} 7", f"{MERGE} feature-branch --repo acme/widgets", f"{MERGE} 7 --repo acme/widgets --admin",
    f"{MERGE} 7 --repo acme/widgets --auto", f"{MERGE} 7 --repo acme/widgets; rm -rf x",
    f"{MERGE} 7 --repo acme/widgets --squash --rebase", f"{MERGE} 7 8 --repo acme/widgets",
    f"{MERGE} https://github.com/acme/widgets/pull/7 --repo other/repo", "git merge main",
    f"{MERGE} 7 --repo acme/widgets --squash", f"{MERGE} 7 --repo acme/widgets --match-head-commit {HEAD[:12]}",
])
def test_anything_else_does_not_parse(command):
    assert auto_merge.parse(command) is None


def test_a_ready_pr_merges_and_is_logged(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(ready_pr())
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets --squash {PIN}"))
    assert decision.decision == "allow" and "auto-merge" in decision.reason
    entry = json.loads((tmp_path / "auto-merge.jsonl").read_text().splitlines()[-1])
    assert entry["outcome"] == "allowed" and entry["repo"] == "acme/widgets" and entry["pr"] == 7


@pytest.mark.parametrize("pr,runs,why", [
    (ready_pr(labels=[]), None, "not labeled"),
    (ready_pr(labels=[{"name": "status:ready-to-deploy"}, {"name": "type:launch"}]), None, "needs the owner"),
    (ready_pr(draft=True), None, "draft"),
    (ready_pr(mergeable_state="blocked"), None, "cleanly mergeable"),
    (ready_pr(base={"ref": "release"}), None, "release"),
    (ready_pr(body="Written-by: claude\nReviewed-by: claude\nTested: ok"), None, "different CLI"),
    (ready_pr(body="Written-by: claude\nReviewed-by: codex"), None, "Tested"),
    (ready_pr(), [{"name": "ci", "status": "in_progress", "conclusion": None}], "has not passed"),
    (ready_pr(), [{"name": "ci", "status": "completed", "conclusion": "neutral"}], "has not passed"),
    (ready_pr(), [{"name": "ci", "status": "completed", "conclusion": "skipped"}], "has not passed"),
    (ready_pr(), ["junk"], "could not"),
    (ready_pr(body="Written-by: claude\nReviewed-by: codex\nTested: failed"), None, "Tested"),
    (ready_pr(body="Written-by: claude\nReviewed-by: codex did not review\nTested: ok"), None, "Reviewed-by"),
    (ready_pr(base="main"), None, "no base"),
    (ready_pr(head={"sha": "b" * 40}), None, "head commit"),
])
def test_anything_not_ready_keeps_the_card(tmp_path, pr, runs, why):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(pr, runs)
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}"))
    assert (decision.decision, decision.effect) == ("deny", "ask") and decision.approval_id
    assert why in rules.get(decision.approval_id)["summary"] + decision.reason + \
        (tmp_path / "auto-merge.jsonl").read_text()


def test_markers_in_a_comment_count(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(ready_pr(body=""), comments=[
        {"body": "- Written-by: codex\n- Reviewed-by: Claude Code\n- Tested: e2e green"}])
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).decision == "allow"


def test_github_errors_keep_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)

    def broken(path, _token):
        raise OSError("down")
    rules.auto_merge._fetch = broken
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_bot_office_trigger_runs_and_the_off_switch_keep_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(ready_pr())
    assert rules.decide(job, bash(f"{MERGE} 7 --repo joshua-scarle/bot-office {PIN}")).effect == "ask"
    store.set_meta("auto_merge", "off")
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"
    store2, trigger = make(tmp_path / "t", source="trigger")
    strict = Rules(store2)
    strict.auto_merge._fetch = fake_github(ready_pr())
    assert strict.decide(trigger, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_an_unwritable_log_keeps_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(ready_pr())
    rules.auto_merge.log_path = tmp_path / "missing" / "auto-merge.jsonl"
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_no_credential_keeps_the_card(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_merge, "token_source", None)
    store, job = make(tmp_path)
    assert Rules(store).decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


@pytest.mark.parametrize("status", [{"state": "success"}, {"total_count": "1", "state": "success"},
                                    {"total_count": 1, "state": "pending"}, ["x"]])
def test_missing_or_odd_statuses_keep_the_card(tmp_path, status):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(ready_pr(), status=status)
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_judge_never_raises_on_odd_data():
    assert auto_merge.judge({"state": "open", "base": {"ref": "main"}, "head": {"sha": HEAD}, "labels": "x"},
                            [], {"total_count": 0}, [], "acme", "widgets", HEAD)


def test_owner_ask_rule_keeps_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)
    rules.auto_merge._fetch = fake_github(ready_pr())
    rules.add_rule(bot_id=None, effect="ask", kind="shell", pattern="gh pr merge*")
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}"))
    assert (decision.decision, decision.effect) == ("deny", "ask")
