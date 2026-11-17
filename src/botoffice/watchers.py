"""0.11.0 #7 event triggers ("watchers"): wake a bot when something outside it changes.

Kinds:
- webhook: ``POST /v1/hooks/{id}`` with ``Authorization: Bearer <per-watcher secret>`` (stored as a
  sha256, shown once); the body (64 kB max) reaches the bot as untrusted data;
- path: a directory listing (one level, a glob) checked every 10 s; new or changed files fire;
- poll: a fixed, read-only ``gh`` command run with the user's own signed-in gh every >= 5 min; the
  first result is the baseline, and the bot is woken (with a short diff) only when the normalised
  output changes, so an unchanged poll costs no model call;
- assignment: another bot's assignment changed state.

Every run is a ``trigger`` turn (unattended, priority 3, subject to the action gate). Limits: one
run in flight per watcher, a debounce (60 s), a per-watcher hourly cap (6; over it the watcher is
paused and shown in Needs-you) and a global hourly cap (BOT_OFFICE_TRIGGER_RUNS_PER_HOUR). Quota,
dedupe, state and the enqueue are one SQLite transaction.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import heapq
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from . import isolation
from .config import Settings
from .domain import Bot, Job
from .rules import UNTRUSTED_RULE, Rules, parse_event, untrusted
from .store import JobStore, now
from .turns import TurnError, Turns

log = logging.getLogger(__name__)

KINDS = ("webhook", "path", "poll", "assignment")
BODY_LIMIT = 64 * 1024
PATH_EVERY = 10.0
POLL_MIN_INTERVAL = 300
POLL_TIMEOUT = 30
POLL_OUTPUT_LIMIT = 512 * 1024
STATE_LIMIT = 64 * 1024
DIFF_LIMIT = 4000
PATH_ENTRIES = 1000
_UNSET = object()

# read-only gh operations a poll watcher may run (master-configured; bots cannot reach this API)
GH_READ = {("pr", "list"), ("pr", "view"), ("pr", "checks"), ("issue", "list"), ("issue", "view"),
           ("run", "list"), ("run", "view"), ("release", "list"), ("release", "view"), ("repo", "view")}
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_FLAGS = {
    "-R": _REPO, "--repo": _REPO,
    "--json": re.compile(r"^[A-Za-z,]{1,400}$"),
    "-s": re.compile(r"^(open|closed|merged|all)$"), "--state": re.compile(r"^(open|closed|merged|all)$"),
    "-L": re.compile(r"^([1-9][0-9]?|100)$"), "--limit": re.compile(r"^([1-9][0-9]?|100)$"),
    "-l": re.compile(r"^[\w .:/-]{1,60}$"), "--label": re.compile(r"^[\w .:/-]{1,60}$"),
    "-A": re.compile(r"^[A-Za-z0-9-]{1,40}$"), "--author": re.compile(r"^[A-Za-z0-9-]{1,40}$"),
    "-b": re.compile(r"^[\w./-]{1,100}$"), "--branch": re.compile(r"^[\w./-]{1,100}$"),
}
_SWITCHES = {"--comments"}
_API_PATH = re.compile(r"^(repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){0,6}|notifications)"
                       r"(\?[A-Za-z0-9_=&.,:-]{0,200})?$")
_REDACT = (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})"),
           re.compile(r'(?i)("[^"]*(?:token|secret|password|authorization)[^"]*"\s*:\s*)"[^"]*"'))


class WatcherError(ValueError):
    pass


class _Skip(Exception):
    def __init__(self, outcome: str, detail: str = "", pause: bool = False):
        super().__init__(outcome)
        self.outcome, self.detail, self.pause = outcome, detail, pause


def check_poll_argv(argv: object) -> list[str]:
    """The argv if it is an allowlisted read-only gh command, else WatcherError. No shell, no other
    executable, no write flags (-X, -f, --input, ...), no values a bot could substitute."""
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv) or len(argv) > 16:
        raise WatcherError("poll command must be a list of strings")
    if len(argv) < 3 or argv[0] != "gh":
        raise WatcherError("poll command must be a read-only gh command, e.g. [\"gh\",\"pr\",\"list\",\"-R\",\"o/r\"]")
    if argv[1] == "api":
        if len(argv) != 3 or not _API_PATH.match(argv[2]) or ".." in argv[2]:
            raise WatcherError("gh api: only a GET of repos/<owner>/<repo>/... or notifications, with no flags")
        return list(argv)
    if (argv[1], argv[2]) not in GH_READ:
        raise WatcherError(f"gh {argv[1]} {argv[2]} is not on the read-only list")
    index = 3
    while index < len(argv):
        arg = argv[index]
        if arg in _SWITCHES:
            index += 1
        elif arg in _FLAGS:
            if index + 1 >= len(argv) or not _FLAGS[arg].match(argv[index + 1]):
                raise WatcherError(f"bad value for {arg}")
            index += 2
        elif re.fullmatch(r"[0-9]{1,9}", arg):
            index += 1
        else:
            raise WatcherError(f"argument {arg[:40]!r} is not allowed in a poll command")
    return list(argv)


def check_config(kind: str, config: dict, settings: Settings) -> dict:
    if kind not in KINDS:
        raise WatcherError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(config, dict):
        raise WatcherError("config must be an object")
    if kind == "webhook":
        return {}
    if kind == "path":
        raw = str(config.get("dir") or "")
        if not raw.startswith("/"):
            # no ~: it would expand against the controller's HOME, not the bots user's
            raise WatcherError("path watcher: dir must be an absolute path (no ~)")
        folder = Path(raw).resolve()
        for protected in {settings.data_dir.resolve(), settings.control_dir.resolve()}:
            if folder == protected or protected in folder.parents or folder in protected.parents:
                raise WatcherError("path watcher: Bot-Office's own data directory cannot be watched")
        pattern = str(config.get("glob") or "*")
        if "/" in pattern or len(pattern) > 100:
            raise WatcherError("path watcher: glob matches names in one directory (no /)")
        return {"dir": str(folder), "glob": pattern}
    if kind == "poll":
        interval = int(config.get("interval_s") or POLL_MIN_INTERVAL)
        if interval < POLL_MIN_INTERVAL:
            raise WatcherError(f"poll watcher: interval_s must be at least {POLL_MIN_INTERVAL}")
        return {"argv": check_poll_argv(config.get("argv")), "interval_s": interval}
    try:
        return {"assignment_id": str(UUID(str(config.get("assignment_id"))))}
    except ValueError as exc:
        raise WatcherError("assignment watcher: assignment_id is required") from exc


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def redact(text: str) -> str:
    text = _REDACT[0].sub("[redacted]", text)
    return _REDACT[1].sub(r'\1"[redacted]"', text)


def normalise(output: str) -> str:
    """Stable text for hashing and diffing: JSON re-serialised with sorted keys, else stripped text."""
    try:
        return json.dumps(json.loads(output), sort_keys=True, indent=1, ensure_ascii=False)
    except ValueError:
        return output.strip()


def short_diff(old: str, new: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "now", n=1, lineterm=""))
    text = "\n".join(lines[2:])
    return text if len(text) <= DIFF_LIMIT else text[:DIFF_LIMIT] + "\n[... diff clipped]"


def _row(row) -> dict:
    item = dict(row)
    item["config"] = json.loads(item.pop("config_json") or "{}")
    item["enabled"] = bool(item["enabled"])
    item["has_secret"] = bool(item.pop("secret_hash", None))
    item.pop("last_state", None)
    return item


class Watchers:
    def __init__(self, store: JobStore, settings: Settings, turns: Turns, rules: Rules):
        self.store = store
        self.settings = settings
        self.turns = turns
        self.rules = rules
        self._checked: dict[str, float] = {}
        self._polling: set[str] = set()

    # --- CRUD (master token only) ---

    def create(self, bot: Bot, kind: str, name: str, prompt: str, config: dict, *,
               debounce_s: int = 60, max_per_hour: int = 6, enabled: bool = True) -> tuple[dict, str | None]:
        """(watcher, secret); the secret of a webhook watcher is returned only here."""
        config = check_config(kind, config, self.settings)
        name, prompt = name.strip()[:80], prompt.strip()
        if not name or not prompt or len(prompt) > 4000:
            raise WatcherError("name and a prompt (up to 4000 characters) are required")
        if not 0 <= debounce_s <= 86400 or not 1 <= max_per_hour <= 60:
            raise WatcherError("debounce_s must be 0..86400 and max_per_hour 1..60")
        if kind == "webhook" and self._outbound_effect(bot) not in ("ask", "never"):
            raise WatcherError("a webhook watcher needs the bot's unattended outbound preset to be ask or never")
        if kind == "assignment":
            with closing(self.store._connect()) as db:
                row = db.execute("SELECT bot_id FROM assignments WHERE id=?", (config["assignment_id"],)).fetchone()
            if not row:
                raise WatcherError("assignment not found")
            if row["bot_id"] == str(bot.id):
                raise WatcherError("an assignment watcher watches another bot's assignment")
        secret = secrets.token_urlsafe(32) if kind == "webhook" else None
        watcher_id = str(uuid4())
        with closing(self.store._connect()) as db:
            db.execute("INSERT INTO watchers (id,bot_id,kind,name,prompt,config_json,secret_hash,enabled,debounce_s,"
                       "max_per_hour,assignment_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (watcher_id, str(bot.id), kind, name, prompt, json.dumps(config),
                        secret_hash(secret) if secret else None, int(enabled), debounce_s, max_per_hour,
                        config.get("assignment_id"), now().isoformat()))
        return self.get(watcher_id), secret  # type: ignore[return-value]

    def _outbound_effect(self, bot: Bot) -> str:
        """What the gate would do with an outbound action in one of this bot's trigger runs."""
        probe = Job(id=uuid4(), prompt="", status="running", created_at=now(), bot_id=bot.id, source="trigger")
        event = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
        return self.rules.evaluate(probe, parse_event(event))[0]

    def get(self, watcher_id: str) -> dict | None:
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT * FROM watchers WHERE id=?", (str(watcher_id),)).fetchone()
        return _row(row) if row else None

    def list(self, bot_id: UUID | str | None = None) -> list[dict]:
        with closing(self.store._connect()) as db:
            if bot_id is None:
                rows = db.execute("SELECT * FROM watchers ORDER BY created_at").fetchall()
            else:
                rows = db.execute("SELECT * FROM watchers WHERE bot_id=? ORDER BY created_at", (str(bot_id),)).fetchall()
        return [_row(row) for row in rows]

    def update(self, watcher_id: str, fields: dict) -> dict:
        current = self.get(watcher_id)
        if not current:
            raise LookupError("watcher not found")
        sets: dict[str, object] = {}
        if "name" in fields or "prompt" in fields:
            name = str(fields.get("name", current["name"])).strip()[:80]
            prompt = str(fields.get("prompt", current["prompt"])).strip()
            if not name or not prompt or len(prompt) > 4000:
                raise WatcherError("name and a prompt (up to 4000 characters) are required")
            sets.update(name=name, prompt=prompt)
        if "config" in fields:
            config = check_config(current["kind"], fields["config"], self.settings)
            sets["config_json"] = json.dumps(config)
            if config != current["config"]:
                sets["last_state"] = None  # a new target takes a new baseline
        if "debounce_s" in fields:
            if not 0 <= int(fields["debounce_s"]) <= 86400:
                raise WatcherError("debounce_s must be 0..86400")
            sets["debounce_s"] = int(fields["debounce_s"])
        if "max_per_hour" in fields:
            if not 1 <= int(fields["max_per_hour"]) <= 60:
                raise WatcherError("max_per_hour must be 1..60")
            sets["max_per_hour"] = int(fields["max_per_hour"])
        if "enabled" in fields:
            sets["enabled"] = int(bool(fields["enabled"]))
            if fields["enabled"]:
                sets["paused_reason"] = None
        if sets:
            with closing(self.store._connect()) as db:
                db.execute(f"UPDATE watchers SET {', '.join(f'{key}=?' for key in sets)} WHERE id=?",
                           (*sets.values(), str(watcher_id)))
        return self.get(watcher_id)  # type: ignore[return-value]

    def rotate_secret(self, watcher_id: str) -> str:
        current = self.get(watcher_id)
        if not current or current["kind"] != "webhook":
            raise LookupError("webhook watcher not found")
        secret = secrets.token_urlsafe(32)
        with closing(self.store._connect()) as db:
            db.execute("UPDATE watchers SET secret_hash=? WHERE id=?", (secret_hash(secret), str(watcher_id)))
        return secret

    def delete(self, watcher_id: str) -> bool:
        with closing(self.store._connect()) as db:
            db.execute("DELETE FROM watcher_runs WHERE watcher_id=?", (str(watcher_id),))
            return db.execute("DELETE FROM watchers WHERE id=?", (str(watcher_id),)).rowcount > 0

    def runs(self, watcher_id: str, limit: int = 100) -> list[dict]:
        with closing(self.store._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT r.*, j.status AS job_status, j.outcome AS job_outcome FROM watcher_runs r "
                "LEFT JOIN jobs j ON j.id=r.job_id WHERE r.watcher_id=? ORDER BY r.id DESC LIMIT ?",
                (str(watcher_id), limit))]

    # --- firing ---

    def _record(self, watcher_id: str, outcome: str, detail: str = "", job_id: str | None = None,
                quiet: bool = False) -> None:
        """History row; *quiet* skips it when the last row has the same outcome (a path or poll
        watcher retrying every check while busy would otherwise fill the history)."""
        with closing(self.store._connect()) as db:
            if quiet:
                last = db.execute("SELECT outcome FROM watcher_runs WHERE watcher_id=? ORDER BY id DESC LIMIT 1",
                                  (watcher_id,)).fetchone()
                if last and last["outcome"] == outcome:
                    return
            db.execute("INSERT INTO watcher_runs (watcher_id,outcome,detail,job_id,created_at) VALUES (?,?,?,?,?)",
                       (watcher_id, outcome, detail[:500], job_id, now().isoformat()))

    def fire(self, watcher: dict, headline: str, payload: str, *, new_state: object = _UNSET,
             expected_state: object = _UNSET, force: bool = False) -> tuple[str, str | None]:
        """Queue one trigger turn if the limits allow it: (outcome, job id). The state change, the
        quota and the enqueue commit together; a skipped run leaves the state as it was."""
        watcher_id = watcher["id"]
        bot = self.store.get_bot(watcher["bot_id"])
        if bot is None:
            return "error", None
        stamp = now()

        def hook(db, job) -> None:
            row = db.execute("SELECT * FROM watchers WHERE id=?", (watcher_id,)).fetchone()
            if row is None or not row["enabled"]:
                raise _Skip("paused", "the watcher is paused or deleted")
            if expected_state is not _UNSET and row["last_state"] != expected_state:
                raise _Skip("stale", "already handled")
            if not force and row["last_fired_at"] and \
                    stamp.isoformat() < _plus(row["last_fired_at"], row["debounce_s"]):
                raise _Skip("debounced", f"within {row['debounce_s']} s of the last run")
            if db.execute("SELECT 1 FROM jobs WHERE watcher_id=? AND status IN ('queued','running')",
                          (watcher_id,)).fetchone():
                raise _Skip("busy", "the previous run has not finished")
            hour = (stamp - timedelta(hours=1)).isoformat()
            fired = db.execute("SELECT COUNT(*) FROM watcher_runs WHERE watcher_id=? AND outcome='fired' "
                               "AND created_at>=?", (watcher_id, hour)).fetchone()[0]
            if fired >= row["max_per_hour"]:
                raise _Skip("capped", f"{fired} runs in the last hour (limit {row['max_per_hour']}); paused", True)
            total = db.execute("SELECT COUNT(*) FROM jobs WHERE source='trigger' AND created_at>=?",
                               (hour,)).fetchone()[0]
            if total > self.settings.trigger_runs_per_hour:  # this job is already inserted
                raise _Skip("global_cap", f"{self.settings.trigger_runs_per_hour} trigger runs/hour reached")
            db.execute("UPDATE jobs SET watcher_id=? WHERE id=?", (watcher_id, str(job.id)))
            state_sql = ", last_state=?" if new_state is not _UNSET else ""
            db.execute(f"UPDATE watchers SET last_fired_at=?{state_sql} WHERE id=?",
                       (stamp.isoformat(), *([new_state] if new_state is not _UNSET else []), watcher_id))
            db.execute("INSERT INTO watcher_runs (watcher_id,outcome,detail,job_id,created_at) VALUES (?,?,?,?,?)",
                       (watcher_id, "fired", headline[:500], str(job.id), stamp.isoformat()))

        source = f"{watcher['kind']}:{watcher['name']}"
        prompt = (f'Watcher "{watcher["name"]}" ({watcher["kind"]}) fired: {headline}\n{UNTRUSTED_RULE}\n'
                  f"{untrusted(source, payload)}\n\nWhen this watcher fires: {watcher['prompt']}")
        try:
            _message, job = self.turns.post_message(bot, f'Watcher "{watcher["name"]}" fired: {headline}',
                                                    source="trigger", prompt=prompt, hook=hook)
        except _Skip as skip:
            quiet = watcher["kind"] != "webhook" and skip.outcome in ("busy", "debounced", "global_cap", "stale")
            if skip.outcome != "stale":
                self._record(watcher_id, skip.outcome, skip.detail, quiet=quiet)
            if skip.pause:
                with closing(self.store._connect()) as db:
                    db.execute("UPDATE watchers SET enabled=0, paused_reason=? WHERE id=?", (skip.detail, watcher_id))
            return skip.outcome, None
        except TurnError as exc:
            self._record(watcher_id, "error", str(exc), quiet=True)
            return "error", None
        return "fired", str(job.id)

    def _baseline(self, watcher: dict, state: str) -> bool:
        """Store the first state without firing; False if another check stored one first."""
        with closing(self.store._connect()) as db:
            took = db.execute("UPDATE watchers SET last_state=? WHERE id=? AND last_state IS NULL",
                              (state, watcher["id"])).rowcount > 0
        if took:
            self._record(watcher["id"], "baseline", "first check; nothing fired")
        return took

    def _state(self, watcher_id: str) -> str | None:
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT last_state FROM watchers WHERE id=?", (watcher_id,)).fetchone()
        return row["last_state"] if row else None

    def webhook(self, watcher_id: str, secret: str, body: bytes, content_type: str = "") -> str:
        """Outcome of a webhook call; LookupError for an unknown id or a wrong secret (both 401)."""
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT * FROM watchers WHERE id=? AND kind='webhook'", (watcher_id,)).fetchone()
        expected = row["secret_hash"] if row and row["secret_hash"] else "0" * 64
        if not hmac.compare_digest(secret_hash(secret), expected) or row is None:
            raise LookupError("unknown watcher or wrong secret")
        text = body.decode("utf-8", "replace")
        if "json" in content_type or text.lstrip().startswith(("{", "[")):
            text = normalise(text)
        return self.fire(_row(row), f"webhook call ({len(body)} bytes)", text)[0]

    def test(self, watcher_id: str) -> tuple[str, str | None]:
        """The GUI's Test button: one run now (debounce skipped; caps and busy still apply)."""
        watcher = self.get(watcher_id)
        if not watcher:
            raise LookupError("watcher not found")
        return self.fire(watcher, "test run from the Watchers panel", "(no payload: this is a test run)", force=True)

    # --- checks (scheduler tick) ---

    def tick(self, spawn: Callable[[Callable[[], None]], None] | None = None) -> None:
        """Path and assignment watchers are checked inline (cheap); a due poll runs via *spawn*
        (a worker thread in the scheduler) so the loop never waits on gh."""
        stamp = time.monotonic()
        for watcher in self.list():
            if not watcher["enabled"] or watcher["kind"] == "webhook":
                continue
            every = watcher["config"].get("interval_s", POLL_MIN_INTERVAL) if watcher["kind"] == "poll" else PATH_EVERY
            if stamp - self._checked.get(watcher["id"], -1e9) < every:
                continue
            self._checked[watcher["id"]] = stamp
            try:
                if watcher["kind"] == "path":
                    self.check_path(watcher)
                elif watcher["kind"] == "assignment":
                    self.check_assignment(watcher)
                elif watcher["id"] not in self._polling:
                    self._polling.add(watcher["id"])
                    job = lambda w=watcher: self._poll_task(w)
                    (spawn or (lambda fn: fn()))(job)
            except Exception:
                log.exception("watcher %s check failed", watcher["id"])

    def _poll_task(self, watcher: dict) -> None:
        try:
            self.check_poll(watcher)
        except Exception:
            log.exception("poll watcher %s failed", watcher["id"])
        finally:
            self._polling.discard(watcher["id"])

    def check_path(self, watcher: dict) -> str:
        folder, pattern = Path(watcher["config"]["dir"]), watcher["config"].get("glob", "*")
        listing: dict[str, list[int]] = {}
        try:
            with os.scandir(folder) as entries:
                # over PATH_ENTRIES matches: always the same ones (smallest names), never scandir's
                # arbitrary order, which made the state flap and fire on every check
                matches = heapq.nsmallest(PATH_ENTRIES, (
                    entry for entry in entries
                    if fnmatch.fnmatch(entry.name, pattern) and entry.is_file(follow_symlinks=False)),
                    key=lambda entry: entry.name)
                for entry in matches:
                    info = entry.stat(follow_symlinks=False)
                    listing[entry.name] = [info.st_mtime_ns, info.st_size]
        except OSError as exc:
            self._record(watcher["id"], "error", f"cannot list {folder}: {type(exc).__name__}", quiet=True)
            return "error"
        state = json.dumps(listing, sort_keys=True)
        previous = self._state(watcher["id"])
        if previous is None:
            self._baseline(watcher, state)
            return "baseline"
        if previous == state:
            return "unchanged"
        try:
            old = json.loads(previous)
        except ValueError:
            old = {}
        changed = sorted(name for name, meta in listing.items() if old.get(name) != meta)
        if not changed:  # only removals: remember them, no run
            self._save_state(watcher["id"], previous, state)
            return "unchanged"
        lines = [("new: " if name not in old else "changed: ") + name for name in changed[:50]]
        return self.fire(watcher, f"{len(changed)} new or changed file(s) in {folder}",
                         "\n".join(lines), new_state=state, expected_state=previous)[0]

    def _save_state(self, watcher_id: str, previous: str | None, state: str) -> None:
        with closing(self.store._connect()) as db:
            db.execute("UPDATE watchers SET last_state=? WHERE id=? AND last_state IS ?", (state, watcher_id, previous))

    def check_assignment(self, watcher: dict) -> str:
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT a.title, a.status, a.status_note, b.name FROM assignments a "
                             "LEFT JOIN bots b ON b.id=a.bot_id WHERE a.id=?",
                             (watcher["config"]["assignment_id"],)).fetchone()
        if row is None:
            self._record(watcher["id"], "error", "the assignment was deleted", quiet=True)
            return "error"
        previous = self._state(watcher["id"])
        if previous is None:
            self._baseline(watcher, row["status"])
            return "baseline"
        if previous == row["status"]:
            return "unchanged"
        # the title, the bot name and the note are bot-written: they stay inside <untrusted-data> (Sec 10)
        headline = f"an assignment went {previous} -> {row['status']}"
        payload = (f"bot: {row['name'] or 'a bot'}\nassignment: {row['title'][:200]}\n"
                   f"status note: {row['status_note'] or '(none)'}")
        return self.fire(watcher, headline, payload,
                         new_state=row["status"], expected_state=previous)[0]

    def run_poll(self, argv: list[str]) -> str:
        """Run the allowlisted gh command (re-checked here) with a scrubbed environment."""
        argv = check_poll_argv(argv)
        # isolated: gh lives with the bots user (its login); never resolved on the controller's PATH
        launcher = isolation.active()
        executable = launcher.which("gh") if launcher is not None else shutil.which("gh")
        if not executable:
            raise WatcherError("gh is not installed")
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "XDG_CONFIG_HOME") if key in os.environ}
        env.update(GH_PROMPT_DISABLED="1", NO_COLOR="1", GH_NO_UPDATE_NOTIFIER="1", GH_PAGER="cat")
        # isolated layout: gh runs as the bots user (its gh login) through the launcher
        command = isolation.wrap([executable, *argv[1:]], env_stdin=True)
        env, header = isolation.run_env(env)
        try:
            out = subprocess.run(command, capture_output=True, timeout=POLL_TIMEOUT, env=env,
                                 input=header, check=False)
        except subprocess.TimeoutExpired as exc:
            raise WatcherError(f"gh timed out after {POLL_TIMEOUT} s") from exc
        if out.returncode != 0:
            raise WatcherError(f"gh exited {out.returncode}: {redact(out.stderr.decode('utf-8', 'replace'))[:200]}")
        return redact(out.stdout[:POLL_OUTPUT_LIMIT].decode("utf-8", "replace"))

    def check_poll(self, watcher: dict, runner: Callable[[list[str]], str] | None = None) -> str:
        try:
            output = (runner or self.run_poll)(watcher["config"]["argv"])
        except (WatcherError, OSError) as exc:
            self._record(watcher["id"], "error", str(exc), quiet=True)
            return "error"
        with closing(self.store._connect()) as db:
            db.execute("UPDATE watchers SET last_checked_at=? WHERE id=?", (now().isoformat(), watcher["id"]))
        text = normalise(output)[:STATE_LIMIT]
        state = json.dumps({"hash": hashlib.sha256(text.encode()).hexdigest(), "text": text})
        previous = self._state(watcher["id"])
        if previous is None:
            self._baseline(watcher, state)
            return "baseline"
        try:
            old = json.loads(previous)
        except ValueError:
            old = {"hash": "", "text": ""}
        if old.get("hash") == json.loads(state)["hash"]:
            return "unchanged"  # no model call
        diff = short_diff(old.get("text", ""), text)
        return self.fire(watcher, f"the output of `{' '.join(watcher['config']['argv'])}` changed", diff,
                         new_state=state, expected_state=previous)[0]


def _plus(stamp: str, seconds: int) -> str:
    return (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat()
