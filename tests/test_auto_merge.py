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


READY = "status:ready-to-deploy"
T_COMMIT, T_CHECK, T_LABEL, T_NOTE = ("2026-11-26T10:00:00Z", "2026-11-26T10:05:00Z", "2026-11-26T10:10:00Z",
                                       "2026-11-26T10:12:00Z")
IDS = frozenset({"writer-bot", "review-bot", "owner-acct"})


def ready_pr(**changes):
    pr = {"state": "open", "draft": False, "base": {"ref": "main"},
          "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "acme/widgets"}},
          "mergeable": True, "mergeable_state": "clean", "labels": [{"name": READY}],
          "user": {"login": "writer-bot"}, "body": ""}
    pr.update(changes)
    return pr


def note(login, body, created=T_NOTE, edited=None, created_edit=False):
    """A PR comment as GitHub's GraphQL gives it (lastEditedAt / includesCreatedEdit are the edit flags)."""
    return {"author": {"login": login} if login else None, "createdAt": created, "lastEditedAt": edited,
            "includesCreatedEdit": created_edit, "body": body}


def ready_comments(sha=HEAD[:12]):
    return [note("writer-bot", f"Written-by: claude @ {sha}\nTested: pytest 120 passed on {sha}"),
            note("review-bot", f"- Reviewed-by: codex ({sha})")]


def run(conclusion="success", status="completed", sha=HEAD, name="ci", done=T_CHECK):
    return {"name": name, "head_sha": sha, "status": status, "conclusion": conclusion, "completed_at": done}


def labeled(login="owner-acct", at=T_LABEL, event="labeled", name=READY):
    return {"event": event, "label": {"name": name}, "actor": {"login": login}, "created_at": at}


def commit(sha=HEAD, author="writer-bot", committer="web-flow", date=T_COMMIT):
    return {"sha": sha, "author": {"login": author}, "committer": {"login": committer},
            "commit": {"committer": {"date": date}}}


NO_STATUS = {"total_count": 0, "state": "pending", "statuses": []}
T_PUSH = "2026-11-26T10:01:00Z"


def pushed(sha=HEAD, at=T_PUSH, ref="refs/heads/feature"):
    return {"after": sha, "before": "c" * 40, "ref": ref, "timestamp": at, "activity_type": "push"}


def fake_github(pr, runs=None, status=None, comments=None, events=None, commits=None, private=True, broken=(),
                activity=None):
    runs = runs if runs is not None else [run()]
    comments = comments if comments is not None else ready_comments()
    events = events if events is not None else [labeled()]
    commits = commits if commits is not None else [commit()]
    activity = activity if activity is not None else [pushed()]
    seen = []

    def page(items, path):
        number = int(path.rsplit("page=", 1)[1]) if "&page=" in path else 1
        return items[(number - 1) * 100:number * 100]

    def fetch(path, _token):
        seen.append(path)
        if any(part in path for part in broken):
            raise OSError(path)
        if path.endswith(("/widgets", "/bot-office", "/repo")):
            return {"private": private} if private is not None else {}
        if path.endswith("/pulls/7"):
            return pr
        if "/activity?" in path:
            return activity
        if "/check-runs" in path:
            return {"total_count": len(runs), "check_runs": runs}
        if "/status" in path:
            return status if status is not None else NO_STATUS
        if path == "/graphql":
            variables = json.loads(_token)["variables"]
            start = int(variables["after"] or 0)
            nodes = comments[start:start + 100]
            more = start + 100 < len(comments)
            return {"data": {"repository": {"pullRequest": {"comments": {
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + 100) if more else None}, "nodes": nodes}}}}}
        if "/timeline" in path:
            return page(events, path)
        if "/pulls/7/commits" in path:
            return page(commits, path)
        raise OSError(path)
    fetch.seen = seen
    return fetch


def checker(rules, fetch):
    rules.auto_merge._fetch = fetch
    rules.auto_merge._identities = lambda: IDS
    return rules


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
    rules = checker(Rules(store), fake_github(ready_pr()))
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets --squash {PIN}"))
    assert decision.decision == "allow" and "auto-merge" in decision.reason
    entry = json.loads((tmp_path / "auto-merge.jsonl").read_text().splitlines()[-1])
    assert entry["outcome"] == "allowed" and entry["repo"] == "acme/widgets" and entry["pr"] == 7


def _card(tmp_path, fetch, why, ids=IDS):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fetch)
    rules.auto_merge._identities = lambda: ids
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}"))
    assert (decision.decision, decision.effect) == ("deny", "ask") and decision.approval_id
    assert why in rules.get(decision.approval_id)["summary"] + decision.reason + \
        (tmp_path / "auto-merge.jsonl").read_text()


SHA12 = HEAD[:12]


@pytest.mark.parametrize("pr,runs,why", [
    (ready_pr(labels=[]), None, "not labeled"),
    (ready_pr(labels=[{"name": READY}, {"name": "type:launch"}]), None, "needs the owner"),
    (ready_pr(draft=True), None, "draft"),
    (ready_pr(mergeable_state="blocked"), None, "cleanly mergeable"),
    (ready_pr(base={"ref": "release"}), None, "release"),
    (ready_pr(), [run(status="in_progress", conclusion=None)], "has not passed"),
    (ready_pr(), ["junk"], "could not"),
    (ready_pr(base="main"), None, "no base"),
    (ready_pr(head={"sha": "b" * 40, "ref": "feature", "repo": {"full_name": "acme/widgets"}}), None, "head commit"),
    (ready_pr(head={"sha": HEAD}), None, "no head repository or branch"),
])
def test_anything_not_ready_keeps_the_card(tmp_path, pr, runs, why):
    _card(tmp_path, fake_github(pr, runs), why)


# --- checks: at least one real, completed, successful check run or status on the head commit
@pytest.mark.parametrize("runs,status,why", [
    ([], NO_STATUS, "none at all is not a pass"),
    ([run("neutral")], NO_STATUS, "none at all is not a pass"),
    ([run("skipped"), run("neutral", name="lint")], NO_STATUS, "none at all is not a pass"),
    ([run("failure")], NO_STATUS, "has not passed"),
    ([run("cancelled")], NO_STATUS, "has not passed"),
    ([run("timed_out")], NO_STATUS, "has not passed"),
    ([run("action_required")], NO_STATUS, "has not passed"),
    ([run("stale")], NO_STATUS, "has not passed"),
    ([run(status="queued", conclusion=None)], NO_STATUS, "has not passed"),
    ([run(), run("failure", name="e2e")], NO_STATUS, "has not passed"),
    ([run(sha="b" * 40)], NO_STATUS, "not on the head commit"),
    ([run()], {"total_count": 1, "state": "failure",
               "statuses": [{"context": "ext", "state": "failure", "updated_at": T_CHECK}]}, "status has not passed"),
    ([run()], {"total_count": 1, "state": "pending",
               "statuses": [{"context": "ext", "state": "pending", "updated_at": T_CHECK}]}, "status has not passed"),
    ([], {"total_count": 1, "state": "success", "statuses": []}, "could not be read"),
])
def test_checks_must_really_pass(tmp_path, runs, status, why):
    _card(tmp_path, fake_github(ready_pr(), runs, status), why)


def test_a_successful_status_alone_counts_and_skipped_runs_are_tolerated(tmp_path):
    store, job = make(tmp_path)
    status = {"total_count": 1, "state": "success",
              "statuses": [{"context": "ci/ext", "state": "success", "updated_at": T_CHECK}]}
    rules = checker(Rules(store), fake_github(ready_pr(), [run("skipped")], status))
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).decision == "allow"


# --- markers from authorized identities, bound to the head commit, no self-approval
@pytest.mark.parametrize("comments,why", [
    ([note("writer-bot", "Written-by: claude\nTested: ok"), note("review-bot", "Reviewed-by: codex")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", "Reviewed-by: codex")], "Reviewed-by"),
    ([note("writer-bot", f"Written-by: claude {'b' * 12}\nTested: ok {'b' * 12}"),
      note("review-bot", f"Reviewed-by: codex {'b' * 12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {HEAD[:7]}\nTested: ok {HEAD[:7]}"),
      note("review-bot", f"Reviewed-by: codex {HEAD[:7]}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12} and {'b' * 12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("stranger", f"Reviewed-by: codex {SHA12}")], "Reviewed-by"),
    ([note("stranger", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}", edited="2026-11-26T10:12:00Z")], "Reviewed-by"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}", created_edit=True)], "Reviewed-by"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note(None, f"Reviewed-by: codex {SHA12}")], "Reviewed-by"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("writer-bot", f"Reviewed-by: codex {SHA12}")], "no self-approval"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: failed {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: codex did not review {SHA12}")], "Reviewed-by"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: claude {SHA12}")], "different CLI"),
])
def test_markers_must_be_authorized_bound_and_independent(tmp_path, comments, why):
    _card(tmp_path, fake_github(ready_pr(), comments=comments), why)


def test_markers_in_the_description_do_not_count(tmp_path):
    body = f"Written-by: claude {SHA12}\nReviewed-by: codex {SHA12}\nTested: ok {SHA12}"
    _card(tmp_path, fake_github(ready_pr(body=body), comments=[]), "Tested")


@pytest.mark.parametrize("pr_author,commit_author", [("review-bot", "writer-bot"), ("writer-bot", "review-bot")])
def test_the_pr_author_or_a_commit_author_cannot_review(tmp_path, pr_author, commit_author):
    _card(tmp_path, fake_github(ready_pr(user={"login": pr_author}), commits=[commit(author=commit_author)]),
          "no self-approval")


def test_no_authorized_identities_keeps_the_card(tmp_path):
    _card(tmp_path, fake_github(ready_pr()), "no authorized identities", ids=frozenset())


def test_identities_come_from_the_meta_key_and_the_environment(tmp_path, monkeypatch):
    store, _job = make(tmp_path)
    monkeypatch.setenv(auto_merge.IDENTITY_ENV, "Review-Bot, bad/login")
    store.set_meta(auto_merge.IDENTITY_META_KEY, "owner-acct writer-bot")
    assert auto_merge.authorized_identities(store) == {"review-bot", "owner-acct", "writer-bot"}
    monkeypatch.delenv(auto_merge.IDENTITY_ENV)
    assert auto_merge.authorized_identities() == frozenset()


# --- the ready label, checked against the issue events
@pytest.mark.parametrize("events,why", [
    ([], "no event shows"),
    ([labeled(login="stranger")], "not applied by an authorized identity"),
    ([labeled(at="2026-11-26T10:03:00Z")], "before the head commit's checks finished"),
    ([labeled(at="2026-11-26T09:00:00Z")], "before the head commit's checks finished"),
    ([labeled(), labeled(event="unlabeled", at="2026-11-26T10:11:00Z")], "no event shows"),
    ([labeled(), labeled(login="stranger", at="2026-11-26T10:11:00Z")], "not applied by an authorized"),
    ([{"event": "labeled", "label": {"name": READY}, "actor": {"login": "owner-acct"}}], "could not be read"),
])
def test_the_ready_label_must_be_applied_by_an_authorized_identity_after_the_checks(tmp_path, events, why):
    _card(tmp_path, fake_github(ready_pr(), events=events), why)


def test_a_backdated_head_commit_does_not_revive_an_old_label(tmp_path):
    # the commit claims 09:00 but its checks only finished at 10:05: a 10:04 label is stale
    _card(tmp_path, fake_github(ready_pr(), commits=[commit(date="2026-11-26T09:00:00Z")],
                                events=[labeled(at="2026-11-26T10:04:00Z")]), "re-apply")


def test_later_pages_of_comments_and_events_are_read(tmp_path):
    filler = [note("someone", "lgtm")] * 100
    store, job = make(tmp_path)
    fetch = fake_github(ready_pr(), comments=filler + ready_comments(),
                        events=[labeled(login="stranger")] * 100 + [labeled()])
    rules = checker(Rules(store), fetch)
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).decision == "allow"
    assert fetch.seen.count("/graphql") == 2


def test_a_force_push_after_the_label_or_the_markers_makes_them_stale(tmp_path):
    pushed = {"event": "head_ref_force_pushed", "actor": {"login": "writer-bot"}, "created_at": "2026-11-26T10:11:00Z"}
    _card(tmp_path / "label", fake_github(ready_pr(), events=[labeled(), pushed]), "force-push")
    relabeled = [labeled(), pushed, labeled(at="2026-11-26T10:11:30Z")]  # label re-applied, markers older
    _card(tmp_path / "markers", fake_github(ready_pr(), events=relabeled,
                                            comments=ready_comments() and [
                                                note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}",
                                                     created="2026-11-26T10:05:30Z"),
                                                note("review-bot", f"Reviewed-by: codex {SHA12}",
                                                     created="2026-11-26T10:05:30Z")]), "Tested")


@pytest.mark.parametrize("activity,why", [
    ([], "activity log does not show"),
    ([pushed(sha="b" * 40)], "activity log does not show"),
    ([pushed(ref="refs/heads/other")], "activity log does not show"),
    ([pushed(at="2026-11-26T10:20:00Z")], "re-apply"),  # a plain push of an already-checked commit after the label
])
def test_the_label_must_follow_the_push_that_made_the_head(tmp_path, activity, why):
    _card(tmp_path, fake_github(ready_pr(), activity=activity), why)


def test_the_activity_log_of_the_head_repository_and_branch_is_read(tmp_path):
    store, job = make(tmp_path)
    fetch = fake_github(ready_pr())
    rules = checker(Rules(store), fetch)
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).decision == "allow"
    assert any(path.startswith("/repos/acme/widgets/activity?ref=refs%2Fheads%2Ffeature") for path in fetch.seen)


def test_a_commit_without_a_github_account_keeps_the_card(tmp_path):
    unlinked = commit()
    unlinked["author"] = None
    _card(tmp_path, fake_github(ready_pr(), commits=[unlinked]), "not a GitHub account")


def test_graphql_errors_keep_the_card(tmp_path):
    store, job = make(tmp_path)
    base = fake_github(ready_pr())

    def fetch(path, body):
        return {"errors": [{"message": "x"}]} if path == "/graphql" else base(path, body)
    rules = checker(Rules(store), fetch)
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_too_many_pages_keep_the_card(tmp_path):
    _card(tmp_path, fake_github(ready_pr(), events=[labeled(login="stranger")] * 1000 + [labeled()]),
          "could not be read")


@pytest.mark.parametrize("part", ["/activity", "/timeline", "/graphql", "/pulls/7/commits", "/status", "/check-runs"])
def test_any_unreadable_part_keeps_the_card(tmp_path, part):
    _card(tmp_path, fake_github(ready_pr(), broken=(part,)), "could not be read")


# --- noreply-merge safeguard: never a GitHub-made merge commit in a public repository
def test_a_public_repository_is_refused_not_carded(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr(), private=False))
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets --rebase {PIN}"))
    assert (decision.decision, decision.effect) == ("deny", "never") and not decision.approval_id
    assert "public repository" in decision.reason and "noreply" in decision.reason


def test_the_checker_itself_also_refuses_a_public_repository():
    checker_ = auto_merge.Checker(None, fetch=fake_github(ready_pr(), private=False), identities=lambda: IDS)
    assert checker_.check("acme", "widgets", 7, HEAD) == auto_merge.PUBLIC_REPO_REASON


@pytest.mark.parametrize("command", [
    f"{MERGE} 7 --repo acme/widgets", f"{MERGE} 7 -R acme/widgets --squash --admin",
    f"{MERGE} https://github.com/acme/widgets/pull/7 --auto",
    "gh api -X PUT repos/acme/widgets/pulls/7/merge", "gh -R acme/widgets pr merge 7",
])
def test_every_github_merge_into_a_public_repository_is_refused_not_carded(tmp_path, command):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr(), private=False))
    decision = rules.decide(job, bash(command))
    assert (decision.decision, decision.effect) == ("deny", "never") and not decision.approval_id, command
    assert "public repository" in decision.reason


@pytest.mark.parametrize("command", [f"{MERGE} 7", f"{MERGE} 7 --squash", "gh api graphql -f query='mutation{mergePullRequest}'"])
def test_a_github_merge_that_names_no_repository_is_refused(tmp_path, command):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    decision = rules.decide(job, bash(command))
    assert decision.effect == "never" and "names its one repository" in decision.reason


@pytest.mark.parametrize("command", [
    f"cd /srv && {MERGE} 7 --repo acme/widgets",
    f"{MERGE} 7 # --repo acme/widgets",
    f"{MERGE} 7 --repo acme/widgets; {MERGE} 8",
    f"{MERGE} 7 --repo acme/widgets --repo other/thing",
    f"R=acme/widgets; {MERGE} 7 --repo $R",
    "g'h' pr m'erge' 7", "g\\h pr merge 7", 'gh pr "merge" 7',
    "g${EMPTY}h pr merge 123 --repo acme/widgets", "$G pr merge 1 -R acme/widgets",
    f"{MERGE} 123 --body '--repo acme/widgets'", f"{MERGE} 7 -R acme/widgets --subject x",
    "GH_REPO=acme/widgets gh pr merge 7", "gh api repos/acme/widgets/merges -f base=main",
    "gh api graphql -f query='mutation{mergePullRequest}' -R acme/widgets",
    "curl -X PUT https://api.github.com/repos/acme/widgets/pulls/7/merge",
])
def test_a_github_merge_must_be_one_plain_command_naming_one_repository(tmp_path, command):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))  # private: the shape alone refuses it
    decision = rules.decide(job, bash(command))
    assert (decision.decision, decision.effect) == ("deny", "never"), command
    assert "one plain command" in decision.reason, command


@pytest.mark.parametrize("command,targets", [
    (f"{MERGE} 7 -R acme/widgets", {("acme", "widgets")}),
    (f"{MERGE} https://github.com/acme/widgets/pull/7 --squash", {("acme", "widgets")}),
    ("gh api -X PUT repos/acme/widgets/pulls/7/merge", {("acme", "widgets")}),
    (f"{MERGE} 123 --body '--repo private/repo'", None), (f"{MERGE} feature -R a/b", None),
    (f"{MERGE} 7 -R a/b -R c/d", None), (f"{MERGE} https://github.com/a/b/pull/7 -R c/d", None),
    ("GH_REPO=a/b gh pr merge 7", None), ("gh api graphql -f query=x", None),
])
def test_merge_targets_come_only_from_the_repo_flag_url_or_endpoint(command, targets):
    assert auto_merge.merge_targets(command) == targets


def test_local_merges_with_variables_are_not_github_merges():
    assert not auto_merge.github_merge("git merge $BRANCH") and not auto_merge.github_merge("echo merge")
    assert auto_merge.github_merge("g${EMPTY}h pr merge 1") and auto_merge.github_merge("$G pr merge 1")


def test_quoted_merge_words_are_seen_as_a_merge():
    assert auto_merge.github_merge("g'h' pr m'erge' 7 -R public/repo")
    assert auto_merge.merge_targets("gh pr merge 7 -R 'Acme/Widgets'") == {("acme", "widgets")}


@pytest.mark.parametrize("node", [{"includesCreatedEdit": False}, {"lastEditedAt": None},
                                  {"lastEditedAt": None, "includesCreatedEdit": None}])
def test_a_comment_missing_an_edit_flag_counts_as_edited(node):
    assert auto_merge.comment_from_graphql({"author": {"login": "x"}, "createdAt": T_NOTE, "body": "", **node})["edited"]


def test_unknown_visibility_refuses_any_github_merge(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr(), private=None))
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets"))
    assert decision.effect == "never" and "did not confirm is private" in decision.reason


def test_a_private_repository_merge_outside_auto_merge_still_gets_its_card(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets --admin"))
    assert (decision.decision, decision.effect) == ("deny", "ask") and decision.approval_id


def test_a_connector_merge_tool_is_checked_too(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr(), private=False))
    event = {"hook_event_name": "PreToolUse", "tool_name": "mcp__github__merge_pull_request",
             "tool_input": {"owner": "acme", "repo": "widgets", "pullNumber": 7}}
    assert rules.decide(job, event).effect == "never"
    event["tool_input"] = {"pullNumber": 7}
    assert rules.decide(job, event).effect == "never"


def test_local_git_merges_are_not_affected():
    assert not auto_merge.github_merge("git merge --ff-only feature")
    assert auto_merge.merge_targets(f"{MERGE} 7 --repo Acme/Widgets.git") == {("acme", "widgets")}


def test_bot_office_itself_public_is_refused(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr(), private=False))
    assert rules.decide(job, bash(f"{MERGE} 7 --repo joshua-scarle/bot-office {PIN}")).effect == "never"


def test_github_errors_refuse_the_merge(tmp_path):
    store, job = make(tmp_path)
    rules = Rules(store)

    def broken(path, _token):
        raise OSError("down")
    rules.auto_merge._fetch = broken  # visibility unknown: never a GitHub merge
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "never"


def test_bot_office_trigger_runs_and_the_off_switch_keep_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    assert rules.decide(job, bash(f"{MERGE} 7 --repo joshua-scarle/bot-office {PIN}")).effect == "ask"
    store.set_meta("auto_merge", "off")
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"
    store2, trigger = make(tmp_path / "t", source="trigger")
    strict = checker(Rules(store2), fake_github(ready_pr()))
    assert strict.decide(trigger, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_an_unwritable_log_keeps_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    rules.auto_merge.log_path = tmp_path / "missing" / "auto-merge.jsonl"
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_no_credential_refuses_the_merge(tmp_path, monkeypatch):
    from botoffice import git_guard
    monkeypatch.setattr(auto_merge, "token_source", None)
    monkeypatch.setattr(git_guard, "visibility_lookup", None)  # nor the host's gh login
    store, job = make(tmp_path)
    assert Rules(store).decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "never"


@pytest.mark.parametrize("status", [{"state": "success"}, {"total_count": "1", "state": "success"},
                                    {"total_count": 1, "state": "pending"}, ["x"]])
def test_missing_or_odd_statuses_keep_the_card(tmp_path, status):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr(), status=status))
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).effect == "ask"


def test_judge_never_raises_on_odd_data():
    assert auto_merge.judge({"state": "open", "base": {"ref": "main"}, "head": {"sha": HEAD}, "labels": "x"},
                            [], {"total_count": 0}, [], "acme", "widgets", HEAD)
    assert auto_merge.judge(ready_pr(), [run(done="garbage")], NO_STATUS, ready_comments(), "acme", "widgets",
                            HEAD, events=[labeled()], commits=[commit()], identities=IDS, activity=[pushed()])
    assert auto_merge.judge(ready_pr(), [run()], NO_STATUS, ["x"], "acme", "widgets",
                            HEAD, events=[labeled()], commits=[commit()], identities=IDS, activity=[pushed()])


def test_judge_passes_the_ready_data():
    comments = [auto_merge.comment_from_graphql(node) for node in ready_comments()]
    assert auto_merge.judge(ready_pr(), [run()], NO_STATUS, comments, "acme", "widgets", HEAD,
                            events=[labeled()], commits=[commit()], identities=IDS, activity=[pushed()]) is None


def test_owner_ask_rule_keeps_the_card(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    rules.add_rule(bot_id=None, effect="ask", kind="shell", pattern="gh pr merge*")
    decision = rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}"))
    assert (decision.decision, decision.effect) == ("deny", "ask")


def test_the_shared_visibility_lookup_answers_first(monkeypatch):
    from botoffice import git_guard
    checker_ = auto_merge.Checker(None)
    monkeypatch.setattr(git_guard, "visibility_lookup", lambda owner, repo: "public")
    assert checker_.private("acme", "widgets") is False
    monkeypatch.setattr(git_guard, "visibility_lookup", lambda owner, repo: "internal")
    assert checker_.private("acme", "widgets") is True
    monkeypatch.setattr(git_guard, "visibility_lookup", lambda owner, repo: None)
    monkeypatch.setattr(auto_merge, "token_source", None)
    assert checker_.private("acme", "widgets") is None  # nobody knows: a GitHub merge is refused



@pytest.mark.parametrize("command", [
    "curl -X POST https://api.github.com/graphql -d '{\"query\":\"mutation{merge" + chr(92) + "u0050ullRequest(input:{})}\"}'",
    "curl -X POST https://api.github.com/graphql --data @/srv/q.json",
    "gh api -X PUT repos/acme/widgets/pulls/7/m%65rge",
])
def test_encoded_or_hidden_github_merges_are_refused(tmp_path, command):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    decision = rules.decide(job, bash(command))
    assert (decision.decision, decision.effect) == ("deny", "never"), command


def test_a_local_merge_is_not_a_github_merge(tmp_path):
    store, job = make(tmp_path)
    rules = checker(Rules(store), fake_github(ready_pr()))
    assert rules._github_merge_refused(rules_parse(bash("git merge --ff-only feature"))) is None
    assert rules._github_merge_refused(rules_parse(bash("gh api graphql -f query='query { viewer { login } }'"))) is None


def rules_parse(event):
    from botoffice.rules import parse_event
    return parse_event(event)


@pytest.mark.parametrize("line", ["Tested: untested", "Tested: unverified", "Tested: not yet", "Tested: will run later",
                                  "Tested: wasn't run", "Tested: CI red"])
def test_negative_test_claims_do_not_count(tmp_path, line):
    comments = [note("writer-bot", f"Written-by: claude {SHA12}\n{line} {SHA12}"),
                note("review-bot", f"Reviewed-by: codex {SHA12}")]
    _card(tmp_path, fake_github(ready_pr(), comments=comments), "Tested")


@pytest.mark.parametrize("base", ["release_2026", "production_hotfix", "release-1.2", "prod"])
def test_release_like_bases_keep_the_card(tmp_path, base):
    _card(tmp_path, fake_github(ready_pr(base={"ref": base})), "it merges into")



@pytest.mark.parametrize("comments,why", [
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: integration tests awaiting execution {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: pytest {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),  # says nothing passed
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: tests pass after the fix is implemented {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok if CI agrees {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: should pass {SHA12}"),
      note("review-bot", f"Reviewed-by: codex {SHA12}")], "Tested"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: codex review requested {SHA12}")], "Reviewed-by"),
    ([note("writer-bot", f"Written-by: claude {SHA12}\nTested: ok {SHA12}"),
      note("review-bot", f"Reviewed-by: codex partly, mostly fine {SHA12}")], "Reviewed-by"),
])
def test_markers_must_be_affirmative_and_fixed_in_shape(tmp_path, comments, why):
    _card(tmp_path, fake_github(ready_pr(), comments=comments), why)


def test_affirmative_marker_shapes_pass(tmp_path):
    store, job = make(tmp_path)
    comments = [note("writer-bot", f"Written-by: Claude Code and grok @ {SHA12}\nTested: 120 passed, e2e green {SHA12}"),
                note("review-bot", f"Reviewed-by: codex, approved ({SHA12})")]
    rules = checker(Rules(store), fake_github(ready_pr(), comments=comments))
    assert rules.decide(job, bash(f"{MERGE} 7 --repo acme/widgets {PIN}")).decision == "allow"
