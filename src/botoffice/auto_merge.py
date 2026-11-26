"""0.12.31: merges that go ahead without the owner's card (the owner, Nov 24 2026).

The owner's rule: routine or minor work that passed tests/QA, was reviewed by a different CLI and is
labeled ``status:ready-to-deploy`` merges automatically; new predictive models, launches, new tool
versions, big redesigns or UI, tutorials, BT accuracy levers and spending still ask him.

Only one shape of command qualifies: a single, plain

    gh pr merge <number|url> --repo <owner>/<repo> --match-head-commit <full 40-character sha>
                             [--squash|--merge|--rebase] [--delete-branch]

(the repository named explicitly, or a PR URL; the head commit always named in full, so a later push
cannot ride on an earlier check; no other flags, no pipes, no second command). The
controller then reads the pull request from GitHub with its own credential and lets the merge
through only when every check below holds; anything else, including any error reading GitHub,
keeps the owner's card (fail safe):

- the repository is not on EXCLUDED_REPOS (Bot-Office itself: a merge there changes the gate and
  needs a restart card anyway);
- the PR is open, not a draft, its base is not a release branch, GitHub says it is mergeable and
  ``clean`` (required checks green, no requested changes blocking it);
- it carries READY_LABEL and none of the BLOCKER_LABELS (also matched after a ``prefix:``);
- (sec-fixes) at least one real check run or commit status on the head commit finished with
  success; every check run is completed and either success or neutral/skipped (those two never count
  as the success), every commit status is success; zero checks and zero statuses keep the card (no
  vacuous pass); malformed, incomplete or more than one page of check data keeps the card;
- markers: ``Written-by: <cli>``, ``Reviewed-by: <cli>`` (a different CLI) and ``Tested: <what
  passed>`` lines count only in a PR *comment* (never the description, which any collaborator can
  edit unseen) that is unedited, written by an authorized identity (``authorized_identities``) and
  whose marker line names the head commit (at least 12 hex characters of it, so any later push makes
  every marker stale). Lines must be affirmative and fixed in shape: Written-by / Reviewed-by hold only
  CLI names (plus "approved" or "lgtm"), Tested must end saying it passed ("<what ran> passed", green,
  ok ...) with no conditional (if, after, when, until, will ...); a line that
  says no, not, failed, skipped, pending, awaiting, requested or the like counts as no line at all. A Reviewed-by only counts from a login that is not the PR author, not an author or
  committer of the PR's commits and not the login that posted the Written-by (no self-approval);
- READY_LABEL counts only when the issue timeline shows it was last applied by an authorized identity
  after the head commit, after every check run and status on it finished and after the last
  force-push, and after the head commit was pushed to the head branch (GitHub's repository activity
  log; a plain push leaves no PR event; unknown push time keeps the card) (re-apply it after the
  last push's checks); an "unlabeled" after it, a commit whose
  author or committer is no GitHub account, or events that cannot all be read, keep the card;
- (sec-fixes, noreply-merge safeguard) the repository is private: a GitHub merge into a public
  repository is refused outright (``PUBLIC_REPO_REASON``), because GitHub writes the merge (or
  squash) commit with the token account's own email. Merge locally with a noreply identity and push
  fast-forward instead (the pre-push identity check then covers every commit). A rebase merge is not
  proven safe either (GitHub rewrites the commits itself), so no merge method is exempt. Rules
  applies the same refusal to *every* GitHub merge a bot runs, card or not (``github_merge``,
  ``merge_targets``): one that names no repository, or one GitHub does not confirm private, is
  refused too.
- Known limit: GitHub is read just before the command runs; the head pin binds the code, but a
  label or check that changes in the seconds between the read and the merge is not seen (GitHub
  branch protection is the place for checks that must hold at the merge itself).

Authorized identities are GitHub logins, from the meta key ``auto_merge_identities`` and the
environment ``BOT_OFFICE_AUTO_MERGE_IDENTITIES`` (comma or space separated, case-insensitive). The
default is none, which keeps every card: with one shared GitHub account for all bots no review can
be independent, so an owner must name at least a separate reviewer account first.

The owner switch is the meta key ``auto_merge`` ("off" turns it off; on by default). Every
automatic merge decision is appended to ``auto-merge.jsonl`` next to the DB.
Stdlib only; the credential is never logged or returned.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

PRESET = "auto-merge"
META_KEY = "auto_merge"
READY_LABEL = "status:ready-to-deploy"
BLOCKER_LABELS = frozenset({
    "needs-joshua", "needs joshua", "needs-owner", "do-not-merge", "do not merge", "wip", "hold", "blocked",
    "launch", "predictive-model", "new-model", "model", "new-version", "version", "release", "redesign", "ui",
    "ux", "design", "tutorial", "bt-accuracy", "accuracy", "spending", "cost", "visual-review", "needs-visual-review",
    "security",
})
EXCLUDED_REPOS = frozenset({("joshua-scarle", "bot-office")})
CLIS = ("claude", "codex", "cursor", "agy", "antigravity", "opencode", "grok", "gemini")
API_TIMEOUT = 5
LOG_LIMIT = 2 * 1024 * 1024
_MERGE_FLAGS = {"-s", "--squash", "-m", "--merge", "-r", "--rebase", "-d", "--delete-branch"}
_VALUE_FLAGS = {"-R", "--repo", "--match-head-commit"}
_PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d{1,7})/?")
_REPO = re.compile(r"([\w.-]+)/([\w.-]+)")
_SHA = re.compile(r"[0-9a-f]{40}")
_HEX = re.compile(r"(?<![0-9a-z])[0-9a-f]{7,40}(?![0-9a-z])")
SHA_MIN = 12  # hex characters of the head commit a marker line must name
IDENTITY_META_KEY = "auto_merge_identities"
IDENTITY_ENV = "BOT_OFFICE_AUTO_MERGE_IDENTITIES"
PAGE_SIZE = 100
MAX_PAGES = 10
PUBLIC_REPO_REASON = ("it is a public repository: a GitHub merge would write a commit with the account's own email "
                      "into public history; merge locally with a noreply identity and push fast-forward instead")
# any GitHub-side merge, in any shape the git guard knows (gh pr merge, REST merge endpoints, GraphQL)
_GITHUB_MERGE = re.compile(r"\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b|/pulls/\d+/merge\b|/merges\b|mergePullRequest"
                           r"|enablePullRequestAutoMerge|enqueuePullRequest|merge-upstream", re.IGNORECASE)
_TARGET_REPO = re.compile(r"(?:--repo[=\s]+|-R\s+|github\.com/|repos/)([\w.-]+)/([\w.-]+)", re.IGNORECASE)
COMMENTS_QUERY = ("query($owner: String!, $name: String!, $number: Int!, $after: String) { repository(owner: $owner, "
                  "name: $name) { pullRequest(number: $number) { comments(first: 100, after: $after) { pageInfo { "
                  "hasNextPage endCursor } nodes { author { login } createdAt lastEditedAt includesCreatedEdit body } } } } }")
_NEGATIVE = re.compile(r"\b(no|not|none|n/?a|never|todo|tbd|fail\w*|skip\w*|pending|didn'?t|won'?t|unknown|"
                       r"partial\w*|broken|errors?|un(?:tested|reviewed|verified|checked|run|approved)|without|"
                       r"missing|incomplete|wip|draft|later|soon|disabled|flaky|nope|\w+n't|blocked|red|awaiting|await|"
                       r"waiting|requested|request|queued|scheduled|running|planned|expected|should|will|once|"
                       r"eventually|progress|ongoing|soon|next|if|when|after|before|until|unless|would|could|might|"
                       r"assum\w*|hopefully|implement\w*|fix(?:ed)?\s+is)\b|\?")
# round 5: a marker is an affirmative statement in a fixed shape, not "anything without a negative word"
# fixed shape: "<what ran> passed" - the line ends with the affirmative word (a trailing on/at/for before the
# SHA is dropped), so "tests pass after the fix is implemented" is no claim
_AFFIRMATIVE = re.compile(r"\b(?:passed|passes|pass|green|ok|succeeded|success|successful)[.!]?$")
_MARKER_WORDS = frozenset(CLIS) | {"code", "cli", "and", "approved", "lgtm"}

# set by the API at start-up (like git_guard.default_lookup): () -> the controller's GitHub token
token_source: Callable[[], str | None] | None = None


def parse(command: str) -> tuple[str, str, int, str | None] | None:
    """(owner, repo, number, head sha or None) when *command* is exactly one plain qualifying
    `gh pr merge`, else None."""
    if not isinstance(command, str) or len(command) > 400 or re.search(r"[;&|<>`$()\n\\{}*?!#~]", command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 4 or words[:3] != ["gh", "pr", "merge"]:
        return None
    target = None
    repo = None
    sha = None
    methods = 0
    rest = words[3:]
    index = 0
    while index < len(rest):
        word = rest[index]
        if "=" in word and word.startswith("--"):
            word, value = word.split("=", 1)
            rest[index + 1:index + 1] = [value]
        if word in _VALUE_FLAGS:
            if index + 1 >= len(rest):
                return None
            value = rest[index + 1]
            if word == "--match-head-commit":
                if sha is not None or not _SHA.fullmatch(value):
                    return None
                sha = value
            else:
                if repo is not None or not _REPO.fullmatch(value):
                    return None
                repo = value
            index += 2
            continue
        if word in _MERGE_FLAGS:
            if word not in ("-d", "--delete-branch"):
                methods += 1
            index += 1
            continue
        if word.startswith("-") or target is not None:
            return None  # --admin, --auto, --body ... or a second target: the card
        target = word
        index += 1
    if target is None or methods > 1 or sha is None:
        return None  # no full head sha: the merge would not be bound to the commit that was checked
    url = _PR_URL.fullmatch(target)
    if url:
        owner, name, number = url.group(1), url.group(2), int(url.group(3))
        if repo is not None and repo.lower() != f"{owner}/{name}".lower():
            return None
    elif target.isdigit() and 0 < len(target) <= 7 and repo is not None:
        owner, name = repo.split("/", 1)
        number = int(target)
    else:
        return None  # a branch name, or no explicit repository: gh would guess; the card
    if not number:
        return None
    return owner, name.removesuffix(".git"), number, sha


def _bare(command: str) -> str:
    """The command as the shell will see its words: quotes and backslashes removed (g'h' pr m"erge")."""
    return re.sub(r"[\"'\\]", "", command or "")


def _unexpanded(command: str) -> str:
    """*command* with every shell expansion removed, as if each were empty (g${EMPTY}h -> gh)."""
    text = _bare(command)
    for _ in range(3):
        text = re.sub(r"\$\{[^{}]*\}|\$\([^()]*\)|`[^`]*`|\$\w+|\$.", "", text)
    return text


def _decoded(command: str) -> str:
    """*command* with JSON/Python \\uXXXX / \\xXX escapes and URL %XX escapes decoded (merge\\u0050ullRequest)."""
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), command or "")
    text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    return urllib.parse.unquote(text)


def github_merge(command: str) -> bool:
    """True when *command* (or a tool name) may ask GitHub itself to merge a pull request: as written,
    with quoting removed, or with any shell expansion taken as empty."""
    if not isinstance(command, str):
        return False
    texts = (command, _bare(command), _unexpanded(command), _decoded(command), _decoded(_bare(command)))
    if any(_GITHUB_MERGE.search(text) for text in texts):
        return True
    # an expansion could supply the missing word: "$G pr merge", "gh p$X merge"
    return bool(re.search(r"[$`]", command)) and bool(re.search(r"merge", _unexpanded(command), re.IGNORECASE)) \
        and bool(re.search(r"\b(?:pr|api|graphql)\b|pulls|github", _unexpanded(command), re.IGNORECASE))


_SHELL_META = re.compile(r"[;&|<>`#\n\r]|\$")
_PR_MERGE_SWITCHES = {"-s", "--squash", "-m", "--merge", "-r", "--rebase", "-d", "--delete-branch", "--admin", "--auto",
                      "--disable-auto"}
_API_VALUE_FLAGS = {"-X", "--method"}
_API_SWITCHES = {"-i", "--include", "--silent"}
_ENDPOINT = re.compile(r"/?repos/([\w.-]+)/([\w.-]+)/(?:pulls/\d+/merge|merges|merge-upstream)/?")


def merge_targets(command: str) -> set[tuple[str, str]] | None:
    """The one (owner, repo) a GitHub merge command really targets, lowercase, as a set; None unless it
    is one plain `gh pr merge` (repository from -R/--repo or the PR URL only, no flag that carries free
    text such as --body) or one plain `gh api` call to a REST merge endpoint (no fields or input), so no
    repository name in a comment, a body or another command can stand in for the real target."""
    if not isinstance(command, str) or _SHELL_META.search(command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
        if words[0].split("=", 1)[0] in ("GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN"):
            return None  # the environment picks the repository or host
        words.pop(0)
    if len(words) < 3 or words[0] != "gh":
        return None
    rest, repos = words[1:], set()
    if "pr" in rest and rest.index("pr") + 1 < len(rest) and rest[rest.index("pr") + 1] == "merge":
        cut = rest.index("pr")
        flags, after = rest[:cut], rest[cut + 2:]
        positional = []
        items = flags + after
        index = 0
        while index < len(items):
            word = items[index]
            value = None
            if word.startswith("--") and "=" in word:
                word, value = word.split("=", 1)
            if word in ("-R", "--repo"):
                if value is None:
                    if index + 1 >= len(items):
                        return None
                    value = items[index + 1]
                    index += 1
                if not _REPO.fullmatch(value):
                    return None
                repos.add(tuple(value.lower().removesuffix(".git").split("/", 1)))
            elif word == "--match-head-commit":
                if value is None:
                    index += 1
            elif word in _PR_MERGE_SWITCHES and value is None:
                pass
            elif word.startswith("-"):
                return None  # --body, --subject, --body-file, --author-email ...: free text; refused
            elif index >= len(flags):
                positional.append(word)
            else:
                return None
            index += 1
        if len(positional) > 1:
            return None
        if positional:
            url = _PR_URL.fullmatch(positional[0])
            if url:
                repos.add((url.group(1).lower(), url.group(2).lower().removesuffix(".git")))
            elif not positional[0].isdigit():
                return None  # a branch name: gh picks the PR from the working directory
        return repos if len(repos) == 1 else None
    if rest[0] == "api":
        endpoint = None
        index = 1
        while index < len(rest):
            word = rest[index]
            if word in _API_VALUE_FLAGS:
                index += 2
                continue
            if word in _API_SWITCHES:
                index += 1
                continue
            if word.startswith("-") or endpoint is not None:
                return None  # fields, input, headers, a host: refused
            endpoint = word
            index += 1
        found = _ENDPOINT.fullmatch(endpoint or "")
        if not found:
            return None  # GraphQL (node ids) or anything else: no repository can be bound
        return {(found.group(1).lower(), found.group(2).lower().removesuffix(".git"))}
    return None


def enabled(store) -> bool:
    try:
        return (store.get_meta(META_KEY) or "on").strip().lower() not in ("off", "0", "false", "no")
    except Exception:  # noqa: BLE001
        return False


def _label_blocks(name: str) -> bool:
    low = name.strip().lower()
    parts = {low, low.split(":", 1)[-1].strip(), low.split("/", 1)[-1].strip()}
    return bool(parts & BLOCKER_LABELS) or low.startswith(("needs:joshua", "needs-joshua", "needs joshua"))


def authorized_identities(store=None) -> frozenset[str]:
    """The GitHub logins whose markers and ready label count (lowercase); empty when none are set."""
    raw = os.environ.get(IDENTITY_ENV, "")
    if store is not None:
        try:
            raw += "," + (store.get_meta(IDENTITY_META_KEY) or "")
        except Exception:  # noqa: BLE001 - an unreadable setting adds nobody
            pass
    return frozenset(word.lower() for word in re.split(r"[\s,]+", raw) if re.fullmatch(r"[A-Za-z0-9-]{1,39}(\[bot\])?", word))


def _login(user) -> str:
    return str(user.get("login") or "").lower() if isinstance(user, dict) else ""


def _time(value) -> float:
    """Seconds since the epoch of a GitHub ISO time; ValueError for anything else (fail closed)."""
    from datetime import datetime
    if not isinstance(value, str) or not value:
        raise ValueError("missing time")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _names_head(line: str, head: str) -> bool:
    """True when every commit-like hex word in *line* is a >= SHA_MIN prefix of *head* and there is one."""
    words = [word for word in _HEX.findall(line.lower()) if not word.isdigit()]
    return bool(words) and all(len(word) >= SHA_MIN and head.startswith(word) for word in words)


def _markers(comments: list, head: str, identities: frozenset[str], after: float = 0.0) -> tuple[dict, list, bool]:
    """({cli: {logins that wrote it}}, [(cli, reviewer login)], tested) from authorized comments that
    GitHub says were never edited (``edited`` is False), posted after *after* (the last force-push),
    whose marker lines name *head*."""
    written: dict[str, set[str]] = {}
    reviewed: list[tuple[str, str]] = []
    tested = False
    for comment in comments:
        if not isinstance(comment, dict):
            raise TypeError("comment")
        login = _login(comment.get("user"))
        if not login or login not in identities:
            continue
        if comment.get("edited") is not False or _time(comment.get("created_at")) <= after:
            continue  # edited (perhaps by someone else), unknown, or older than the last force-push
        for line in str(comment.get("body") or "").splitlines():
            found = re.match(r"^\s*(?:[-*]\s*)?(written-by|reviewed-by|tested)\s*:\s*(\S.*)$", line, re.IGNORECASE)
            if not found or not _names_head(found.group(2), head):
                continue
            key = found.group(1).lower()
            value = _HEX.sub(" ", found.group(2).lower()).strip()
            if _NEGATIVE.search(value):
                continue  # 'Tested: failed', 'Reviewed-by: codex did not review': no claim at all
            if key == "tested":
                claim = re.sub(r"[\s(@:,-]*(?:\b(?:on|at|for|of)\b)?[\s()@:,-]*$", "", value)
                tested = tested or bool(_AFFIRMATIVE.search(claim))  # it must end saying it passed
                continue
            if any(word not in _MARKER_WORDS for word in re.findall(r"[a-z][a-z0-9-]*", value)):
                continue  # Written-by / Reviewed-by hold only CLI names (and approved / lgtm), nothing else
            clis = {cli for cli in CLIS if re.search(rf"\b{cli}\b", value)}
            if "antigravity" in clis:
                clis = (clis - {"antigravity"}) | {"agy"}
            for cli in clis:
                if key == "written-by":
                    written.setdefault(cli, set()).add(login)
                else:
                    reviewed.append((cli, login))
    return written, reviewed, tested


def comment_from_graphql(node: dict) -> dict:
    """A GraphQL IssueComment as _markers reads it; edited unless GitHub says plainly it never was."""
    edited = not ("lastEditedAt" in node and node["lastEditedAt"] is None and node.get("includesCreatedEdit") is False)
    return {"user": node.get("author"), "created_at": node.get("createdAt"), "edited": bool(edited),
            "body": node.get("body")}


def _checks_pass(checks: list, statuses: dict, head: str) -> tuple[str | None, float]:
    """(why not, latest finish time) for the head commit's check runs and commit statuses."""
    if not isinstance(checks, list) or not all(isinstance(run, dict) for run in checks):
        return "the checks could not be read", 0.0
    passed = 0
    latest = 0.0
    for run in checks:
        name = str(run.get("name") or "?")[:60]
        if str(run.get("head_sha") or "") != head:
            return f"check {name} is not on the head commit", 0.0
        if run.get("status") != "completed" or run.get("conclusion") not in ("success", "neutral", "skipped"):
            return f"check {name} has not passed", 0.0
        latest = max(latest, _time(run.get("completed_at")))
        passed += run.get("conclusion") == "success"
    if not isinstance(statuses, dict):
        return "the commit statuses could not be read", 0.0
    count, items = statuses.get("total_count"), statuses.get("statuses")
    if type(count) is not int or count < 0 or not isinstance(items, list) or len(items) != count \
            or not all(isinstance(item, dict) for item in items):
        return "the commit statuses could not be read", 0.0
    if count and statuses.get("state") != "success":
        return "a commit status has not passed", 0.0
    for item in items:
        if item.get("state") != "success":
            return f"commit status {str(item.get('context') or '?')[:60]} has not passed", 0.0
        latest = max(latest, _time(item.get("updated_at") or item.get("created_at")))
        passed += 1
    if not passed:
        return "no check run or commit status passed on the head commit (none at all is not a pass)", 0.0
    return None, latest


def _label_applied(events: list, identities: frozenset[str], after: float) -> str | None:
    """Why the ready label does not count, or None: its last 'labeled' event (no 'unlabeled' after
    it) is by an authorized identity, strictly after *after*."""
    last = None
    for event in events:
        if not isinstance(event, dict):
            raise TypeError("event")
        if event.get("event") in ("head_ref_force_pushed", "head_ref_restored", "reopened"):
            after = max(after, _time(event.get("created_at")))  # a rewrite of the branch: old evidence is stale
            continue
        if event.get("event") not in ("labeled", "unlabeled"):
            continue
        label = event.get("label")
        if not isinstance(label, dict) or str(label.get("name") or "").strip().lower() != READY_LABEL:
            continue
        last = event if event["event"] == "labeled" else None
    if last is None:
        return f"no event shows who applied {READY_LABEL}"
    if _login(last.get("actor")) not in identities:
        return f"{READY_LABEL} was not applied by an authorized identity"
    if _time(last.get("created_at")) <= after:
        return f"{READY_LABEL} was applied before the head commit's checks finished or a later force-push (re-apply it)"
    return None


def judge(pr: dict, checks: list[dict], statuses: dict, comments: list, owner: str, repo: str,
          sha: str | None, *, events: list | None = None, commits: list | None = None,
          identities: frozenset[str] = frozenset(), activity: list | None = None) -> str | None:
    """None when the PR may merge without the card, else why not (plain words). Data of an
    unexpected shape is a why-not, never an exception. *activity* is the head repository's push log
    for the head branch (GET /repos/{o}/{r}/activity), newest first."""
    try:
        return _judge(pr, checks, statuses, comments, owner, repo, sha, events, commits, identities, activity)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError) as exc:
        return f"GitHub sent data that could not be read ({type(exc).__name__})"


def _push_time(activity, head: str, ref: str) -> float | None:
    """When *head* last became the tip of *ref* (server time), from the activity log; None if unknown."""
    if not isinstance(activity, list):
        return None
    times = []
    for item in activity:
        if not isinstance(item, dict):
            raise TypeError("activity")
        if item.get("after") == head and item.get("ref") == f"refs/heads/{ref}":
            times.append(_time(item.get("timestamp")))
    return max(times) if times else None


def _judge(pr: dict, checks: list, statuses: dict, comments: list, owner: str, repo: str,
           sha: str | None, events: list | None, commits: list | None, identities: frozenset[str],
           activity: list | None = None) -> str | None:
    if (owner.lower(), repo.lower()) in EXCLUDED_REPOS:
        return "Bot-Office's own repository always asks"
    if not isinstance(pr, dict) or pr.get("state") != "open":
        return "the pull request is not open"
    if pr.get("draft"):
        return "the pull request is a draft"
    if not isinstance(pr.get("base"), dict) or not isinstance(pr.get("head"), dict):
        return "GitHub gave no base or head branch"
    base = str(pr["base"].get("ref") or "")
    if not base or re.match(r"^(release|releases|prod|production)(?![a-z0-9])", base.lower()):
        return f"it merges into {base or 'an unknown branch'}"
    head = str(pr["head"].get("sha") or "")
    if not _SHA.fullmatch(head) or not sha or head != sha.lower():
        return "the head commit is not the one named"
    if pr.get("mergeable") is not True or pr.get("mergeable_state") not in ("clean", "has_hooks"):
        return f"GitHub does not report it cleanly mergeable ({pr.get('mergeable_state') or 'unknown'})"
    raw_labels = pr.get("labels") or []
    if not isinstance(raw_labels, list) or not all(isinstance(item, dict) for item in raw_labels):
        return "GitHub gave labels that could not be read"
    labels = [str(item.get("name") or "") for item in raw_labels]
    if READY_LABEL not in [label.strip().lower() for label in labels]:
        return f"it is not labeled {READY_LABEL}"
    blocking = [label for label in labels if _label_blocks(label)]
    if blocking:
        return f"it is labeled {', '.join(blocking[:3])}, which needs the owner"
    why, finished = _checks_pass(checks, statuses, head)
    if why:
        return why
    if not identities:
        return f"no authorized identities are set for auto-merge ({IDENTITY_META_KEY})"
    if not isinstance(events, list) or not isinstance(commits, list) or not isinstance(comments, list):
        return "the PR's events, commits or comments could not be read"
    head_commit = None
    code_logins = {_login(pr.get("user"))}
    for item in commits:
        if not isinstance(item, dict) or not isinstance(item.get("commit"), dict):
            raise TypeError("commit")
        people = {_login(item.get("author")), _login(item.get("committer"))}
        if "" in people:  # an email linked to no account: who wrote it cannot be told apart from a reviewer
            return "a commit's author or committer is not a GitHub account, so an independent review cannot be shown"
        code_logins |= people
        if item.get("sha") == head:
            head_commit = item
    code_logins -= {"", "web-flow"}
    if head_commit is None:
        return "the head commit is not among the PR's commits"
    committed = (head_commit["commit"].get("committer") or {}).get("date")
    pushed = _push_time(activity, head, str(pr["head"].get("ref") or ""))
    if pushed is None:  # a plain push leaves no PR event: GitHub's activity log is the push time
        return "GitHub's activity log does not show when the head commit was pushed"
    why = _label_applied(events, identities, max(finished, _time(committed), pushed))
    if why:
        return why
    pushed = max([_time(event.get("created_at")) for event in events if isinstance(event, dict)
                  and event.get("event") in ("head_ref_force_pushed", "head_ref_restored")] or [0.0])
    written, reviewed, tested = _markers(comments, head, identities, pushed)
    if not tested:
        return f"no authorized 'Tested:' line names the head commit ({head[:SHA_MIN]}) and says what passed"
    if not written or not reviewed:
        return f"no authorized 'Written-by:' and 'Reviewed-by:' lines name the CLIs and the head commit ({head[:SHA_MIN]})"
    if not any(cli not in written for cli, _ in reviewed):
        return "it was not reviewed by a different CLI"
    writers = set().union(*written.values())
    if not any(cli not in written and login not in code_logins and login not in writers for cli, login in reviewed):
        return "the only review is from the PR's author, a commit's author or the writer (no self-approval)"
    return None


def _get(api: str, path: str, token: str):
    request = urllib.request.Request(f"{api.rstrip('/')}{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "bot-office"})
    with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
        return json.loads(response.read(4 * 1024 * 1024).decode())


class Checker:
    """Reads a PR from GitHub and judges it; ``fetch`` is swappable for tests. Writes the log."""

    def __init__(self, log_path: Path | None = None, fetch: Callable[[str, str], object] | None = None,
                 api: str | None = None, identities: Callable[[], frozenset[str]] | None = None):
        self.log_path = log_path
        self._fetch = fetch
        self._api = api or os.environ.get("BOT_OFFICE_GITHUB_API", "https://api.github.com")
        self._identities = identities or authorized_identities
        self._lock = threading.Lock()

    def _read(self, path: str):
        if self._fetch is not None:
            return self._fetch(path, "")
        token = token_source() if token_source else None
        if not token:
            raise OSError("no GitHub credential")
        return _get(self._api, path, token)

    def _graphql(self, query: str, variables: dict):
        body = json.dumps({"query": query, "variables": variables})
        if self._fetch is not None:
            return self._fetch("/graphql", body)
        token = token_source() if token_source else None
        if not token:
            raise OSError("no GitHub credential")
        request = urllib.request.Request(f"{self._api.rstrip('/')}/graphql", data=body.encode(), method="POST", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "bot-office",
            "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return json.loads(response.read(4 * 1024 * 1024).decode())

    def _comments(self, owner: str, repo: str, number: int) -> list:
        """Every PR comment with GitHub's own edit flags (REST cannot tell an edit within the same second)."""
        out: list = []
        after = None
        for _page in range(MAX_PAGES):
            answer = self._graphql(COMMENTS_QUERY, {"owner": owner, "name": repo, "number": number, "after": after})
            if not isinstance(answer, dict) or answer.get("errors"):
                raise ValueError("GraphQL error")
            block = answer["data"]["repository"]["pullRequest"]["comments"]
            for node in block["nodes"]:
                if not isinstance(node, dict):
                    raise TypeError("comment")
                out.append(comment_from_graphql(node))
            info = block["pageInfo"]
            if not info.get("hasNextPage"):
                return out
            after = info.get("endCursor")
            if not isinstance(after, str) or not after:
                raise ValueError("no cursor")
        raise ValueError("too many pages")

    def private(self, owner: str, repo: str) -> bool | None:
        """True only when GitHub says the repository is private (or internal); False public; None unknown.
        The controller's shared, cached lookup (git_guard.repo_visibility: the token, else the host's gh
        login, and BOT_OFFICE_PUBLIC_REPOS) answers first; a swapped-in ``fetch`` (tests) is asked itself."""
        if self._fetch is None:
            from . import git_guard
            known = git_guard.repo_visibility(owner, repo)
            if known in ("private", "internal"):
                return True
            if known == "public":
                return False
        try:
            info = self._read(f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}")
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError):
            return None
        if not isinstance(info, dict) or not isinstance(info.get("private"), bool):
            return None
        return info["private"]

    def _read_all(self, path: str) -> list:
        """Every page of a list endpoint (at most MAX_PAGES); more, or a non-list, is a ValueError."""
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            chunk = self._read(f"{path}{'&' if '?' in path else '?'}per_page={PAGE_SIZE}&page={page}")
            if not isinstance(chunk, list):
                raise ValueError("not a list")
            items.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                return items
        raise ValueError("too many pages")

    def check(self, owner: str, repo: str, number: int, sha: str | None) -> str | None:
        """None when the merge may go ahead, else why not; any error reading GitHub is a why-not.
        PUBLIC_REPO_REASON (compare with ==) means the merge must be refused, not carded."""
        base = f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        try:
            info = self._read(base)
            if not isinstance(info, dict) or not isinstance(info.get("private"), bool):
                return "GitHub did not say whether the repository is public"
            if info["private"] is False:
                return PUBLIC_REPO_REASON
            if (owner.lower(), repo.lower()) in EXCLUDED_REPOS:
                return "Bot-Office's own repository always asks"
            pr = self._read(f"{base}/pulls/{number}")
            head = str(((pr or {}).get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                return "GitHub gave no head commit"
            runs = self._read(f"{base}/commits/{head}/check-runs?per_page=100")
            statuses = self._read(f"{base}/commits/{head}/status?per_page=100")
            comments = self._comments(owner, repo, number)
            events = self._read_all(f"{base}/issues/{number}/timeline")  # labels and force-pushes
            commits = self._read_all(f"{base}/pulls/{number}/commits")
            head_repo = str(((pr.get("head") or {}).get("repo") or {}).get("full_name") or "")
            head_ref = str((pr.get("head") or {}).get("ref") or "")
            if not _REPO.fullmatch(head_repo) or not head_ref:
                return "GitHub gave no head repository or branch"
            hr_owner, hr_name = head_repo.split("/", 1)
            activity = self._read(f"/repos/{urllib.parse.quote(hr_owner)}/{urllib.parse.quote(hr_name)}/activity"
                                  f"?ref={urllib.parse.quote('refs/heads/' + head_ref, safe='')}&per_page=100")
            if not isinstance(activity, list):
                return "GitHub's activity log could not be read"
            identities = frozenset(self._identities())
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            return f"GitHub could not be read ({type(exc).__name__})"
        checks = runs.get("check_runs") if isinstance(runs, dict) else None
        total = runs.get("total_count") if isinstance(runs, dict) else None
        if not isinstance(checks, list) or type(total) is not int or total != len(checks):
            return "the checks could not all be read"
        if not all(isinstance(item, dict) for item in comments):
            return "the comments could not be read"
        return judge(pr, checks, statuses, comments, owner, repo, sha, events=events, commits=commits,
                     identities=identities, activity=activity)

    def log(self, entry: dict) -> bool:
        if self.log_path is None:
            return True
        try:
            line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
        except (TypeError, ValueError):
            return False
        with self._lock:
            try:
                if self.log_path.exists() and self.log_path.stat().st_size > LOG_LIMIT:
                    os.replace(self.log_path, self.log_path.with_suffix(".jsonl.1"))
                fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                             0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except (OSError, ValueError):
                return False
        return True
