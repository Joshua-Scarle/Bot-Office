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
- every check run and commit status on its head commit finished with success (not neutral, skipped,
  pending or failed); malformed or incomplete check data keeps the card;
- its description or a comment has ``Written-by: <cli>``, ``Reviewed-by: <cli>`` (a different
  CLI) and ``Tested: <what passed>`` lines; a line that says no, not, failed, skipped, pending or
  the like counts as no line at all.

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
_NEGATIVE = re.compile(r"\b(no|not|none|n/?a|never|todo|tbd|fail\w*|skip\w*|pending|didn'?t|won'?t|unknown|"
                       r"partial\w*|broken|errors?)\b|\?")

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


def enabled(store) -> bool:
    try:
        return (store.get_meta(META_KEY) or "on").strip().lower() not in ("off", "0", "false", "no")
    except Exception:  # noqa: BLE001
        return False


def _label_blocks(name: str) -> bool:
    low = name.strip().lower()
    parts = {low, low.split(":", 1)[-1].strip(), low.split("/", 1)[-1].strip()}
    return bool(parts & BLOCKER_LABELS) or low.startswith(("needs:joshua", "needs-joshua", "needs joshua"))


def _markers(texts: list[str]) -> tuple[set[str], set[str], bool]:
    written: set[str] = set()
    reviewed: set[str] = set()
    tested = False
    for text in texts:
        for line in (text or "").splitlines():
            found = re.match(r"^\s*(?:[-*]\s*)?(written-by|reviewed-by|tested)\s*:\s*(\S.*)$", line, re.IGNORECASE)
            if not found:
                continue
            key, value = found.group(1).lower(), found.group(2).strip().lower()
            if _NEGATIVE.search(value):
                continue  # 'Tested: failed', 'Reviewed-by: codex did not review': no claim at all
            if key == "tested":
                tested = tested or bool(re.sub(r"[\W_]+", "", value))
                continue
            clis = {cli for cli in CLIS if re.search(rf"\b{cli}\b", value)}
            if "antigravity" in clis:
                clis = (clis - {"antigravity"}) | {"agy"}
            (written if key == "written-by" else reviewed).update(clis)
    return written, reviewed, tested


def judge(pr: dict, checks: list[dict], statuses: dict, comments: list[str], owner: str, repo: str,
          sha: str | None) -> str | None:
    """None when the PR may merge without the card, else why not (plain words). Data of an
    unexpected shape is a why-not, never an exception."""
    try:
        return _judge(pr, checks, statuses, comments, owner, repo, sha)
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        return f"GitHub sent data that could not be read ({type(exc).__name__})"


def _judge(pr: dict, checks: list, statuses: dict, comments: list[str], owner: str, repo: str,
           sha: str | None) -> str | None:
    if (owner.lower(), repo.lower()) in EXCLUDED_REPOS:
        return "Bot-Office's own repository always asks"
    if not isinstance(pr, dict) or pr.get("state") != "open":
        return "the pull request is not open"
    if pr.get("draft"):
        return "the pull request is a draft"
    if not isinstance(pr.get("base"), dict) or not isinstance(pr.get("head"), dict):
        return "GitHub gave no base or head branch"
    base = str(pr["base"].get("ref") or "")
    if not base or re.match(r"^(release|releases|prod|production)\b", base.lower()):
        return f"it merges into {base or 'an unknown branch'}"
    head = str(pr["head"].get("sha") or "")
    if not head or not sha or head != sha.lower():
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
    if not isinstance(checks, list) or not all(isinstance(run, dict) for run in checks):
        return "the checks could not be read"
    for run in checks:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            return f"check {str(run.get('name') or '?')[:60]} has not passed"
    count = statuses.get("total_count") if isinstance(statuses, dict) else None
    if type(count) is not int or count < 0:
        return "the commit statuses could not be read"
    if count and statuses.get("state") != "success":
        return "a commit status has not passed"
    written, reviewed, tested = _markers([str(pr.get("body") or ""), *comments])
    if not tested:
        return "no 'Tested:' line says what passed"
    if not written or not reviewed:
        return "no 'Written-by:' and 'Reviewed-by:' lines name the CLIs"
    if not (reviewed - written):
        return "it was not reviewed by a different CLI"
    return None


def _get(api: str, path: str, token: str):
    request = urllib.request.Request(f"{api.rstrip('/')}{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "bot-office"})
    with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
        return json.loads(response.read(4 * 1024 * 1024).decode())


class Checker:
    """Reads a PR from GitHub and judges it; ``fetch`` is swappable for tests. Writes the log."""

    def __init__(self, log_path: Path | None = None, fetch: Callable[[str, str], object] | None = None,
                 api: str | None = None):
        self.log_path = log_path
        self._fetch = fetch
        self._api = api or os.environ.get("BOT_OFFICE_GITHUB_API", "https://api.github.com")
        self._lock = threading.Lock()

    def _read(self, path: str):
        if self._fetch is not None:
            return self._fetch(path, "")
        token = token_source() if token_source else None
        if not token:
            raise OSError("no GitHub credential")
        return _get(self._api, path, token)

    def check(self, owner: str, repo: str, number: int, sha: str | None) -> str | None:
        """None when the merge may go ahead, else why not; any error reading GitHub is a why-not."""
        if (owner.lower(), repo.lower()) in EXCLUDED_REPOS:
            return "Bot-Office's own repository always asks"
        base = f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        try:
            pr = self._read(f"{base}/pulls/{number}")
            head = str(((pr or {}).get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                return "GitHub gave no head commit"
            runs = self._read(f"{base}/commits/{head}/check-runs?per_page=100")
            statuses = self._read(f"{base}/commits/{head}/status")
            comments = self._read(f"{base}/issues/{number}/comments?per_page=100")
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError) as exc:
            return f"GitHub could not be read ({type(exc).__name__})"
        checks = runs.get("check_runs") if isinstance(runs, dict) else None
        total = runs.get("total_count") if isinstance(runs, dict) else None
        if not isinstance(checks, list) or type(total) is not int or total != len(checks):
            return "the checks could not all be read"
        if not isinstance(comments, list) or not all(isinstance(item, dict) for item in comments):
            return "the comments could not be read"
        texts = [str(item.get("body") or "") for item in comments]
        return judge(pr, checks, statuses, texts, owner, repo, sha)

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
