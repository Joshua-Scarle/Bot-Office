from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from . import attachments, autocontinue, baseline, continuation, failures, git_guard, isolation, needs_you, procs
from . import freshness, house_style, rules_sync, safefs, stall_sweep
from . import slash as slash_mod
from . import usage as budgets
from .assignments import BOT_ROUTINE_MIN_GAP, Assignments
from .catalog import ENGINE_LABELS
from .compaction import WORKING_FILE, cap_working
from .config import Settings
from .cron import cron_matches, to_cron_time
from .domain import Bot, Job, JobStatus, Routine
from .memory import MAX_MEMORY_BYTES, Memory, Merge
from .providers.base import ProviderAdapter, RunResult
from .providers.cli import REPO_BIN, SESSION_ENGINES, CliAdapter, archive_session
from .rules import Rules, gate_state, github_token, github_token_path, helpers_reachable, is_unattended
from .store import JobStore, RoutineChanged, now
from .streaming import EventSink
from .turns import InboxLimitError, TurnError, Turns
from .watchers import Watchers
from .workspace import (
    JOB_ATTACHMENTS_DIR,
    _image_index,
    model_switch,
    prepare_bot_workspace,
    save_rules_snapshot,
    save_settings_snapshot,
)

SIGNAL_GRACE_SECONDS = 0.5
PAUSE_FILE = "PAUSE"
PAUSE_ACK_FRESH_SECONDS = 5.0  # a loop that has not passed for this long cannot vouch for the pause
TICK_SECONDS = 1.0
WATCHDOG_SECONDS = 60.0
SWEEP_SECONDS = 300.0
# a running turn this long past its timeout did not stop by itself (wait_for) and is cancelled
HUNG_GRACE_SECONDS = 60
MAX_INBOX_BYTES = 200_000
MAX_INBOX_PER_TICK = 20
INBOX_TEMP_MAX_AGE = 10 * 60
INBOX_TEMP_NAME = re.compile(r"\.[0-9a-f]{32}\.json\.tmp")
REPLY_LIMIT = 20_000
LOG_FALLBACK_LIMIT = 4_000
# a due routine run up to this old fires as usual; older ones are "missed" (catch-up policy)
ROUTINE_ON_TIME = timedelta(minutes=2)
log = logging.getLogger("botoffice.scheduler")


def _mark_handoff_pending(bot_dir: Path) -> None:
    """Durable marker in the bot's state dir (private when isolated), never through a link."""
    directory = isolation.state_dir(bot_dir)
    safefs.mkdir(directory, isolation.dir_mode())
    safefs.touch(directory / ".handoff-pending", fsync=True)


# 0.11.10 auto-continue: turns of these sources that end not_done are continued (assignment turns have
# their own loop; bot-to-bot replies go back to the sender; watcher runs have their own quotas). The
# continuation keeps the source, so it runs under the same attended/unattended rules as the turn it continues.
AUTO_CONTINUE_SOURCES = ("chat", "api", "routine", "wakeup")
AUTO_CONTINUE_FAILED = "Could not continue automatically. Reply to keep it going."
AUTO_CONTINUE_LINE = "Continuing automatically ({n} of {max})"
AUTO_CONTINUE_PROMPT = (
    "Automatic continuation {n} of {max}: your previous turn ended not_done, so carry on with the same task "
    "from where you stopped (WORKING.md and your last reply have the state). Keep going until it is done, you "
    "are stuck and need the user, or a rule or instruction tells you to stop. Do not start work beyond that "
    "task. If it is still unfinished when you stop, end with the BOT-OFFICE-STATUS line. The owner did not see "
    "your previous reply: your final reply reports the whole task.")


class Scheduler:
    def __init__(
        self,
        store: JobStore,
        settings: Settings,
        providers: dict[str, ProviderAdapter],
        clock: Callable[[], datetime] = now,
    ):
        self.store = store
        self.settings = settings
        self.providers = providers
        self.clock = clock
        self.on_needs_login: Callable[[], None] | None = None  # 0.12.8: the engine sign-in cache hooks in here
        self.memory = Memory(settings.data_dir)
        self.turns = Turns(store, settings, providers)
        self.assignments = Assignments(store)
        self.assignments.cancel_running = self._cancel_dropped
        self.rules = Rules(store, settings.approval_ttl_min, self.protected_paths(),
                           workspaces=settings.data_dir / "workspaces")
        self.watchers = Watchers(store, settings, self.turns, self.rules)
        self._snapshots: dict[str, tuple[str, str]] = {}
        self._prepared: dict[str, list[tuple[Path, str, str, tuple[str, Merge] | None]]] = {}
        self._sinks: dict[str, EventSink] = {}
        self._steer_deferrals: dict[str, int] = {}
        self._last_tick = 0.0
        self._last_watchdog = 0.0
        self._last_sweep = 0.0  # 0.12.39 stall sweep and the daily rules sync (every SWEEP_SECONDS)
        self._pause_seen = False
        self._pause_seen_at = 0.0
        self._cancel_reasons: dict[str, str] = {}
        # run lanes (Settings.run_lanes) -> running job task / job id; desktops are leased by claim
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._job_ids: dict[int, str] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self.settings.ensure_dirs()
        self.store.initialize()
        # leftovers of jobs that were running when the controller stopped must be proven gone
        # before anything is replayed (procs.recovery_proof); without proof a job is not retried
        running = self.store.running_job_ids()
        proven = await asyncio.to_thread(procs.recovery_proof, running) if running else set()
        steered: list[int] = []
        self.store.recover_interrupted(set(running) - proven, steered)
        self.store.fail_stale_decisions(AUTO_CONTINUE_FAILED)  # left "deciding" by a crash
        for message_id in steered:
            self._log_steer_turn(message_id)  # a steer queued here is a Claude turn like any other
        self._cleanup_stale_attachments()
        for bot_id in self.store.bots_with_session_reset():
            with contextlib.suppress(OSError):
                self.reconcile_session(bot_id)  # retried before the bot's next job otherwise
        self.memory.ensure_shared()
        with contextlib.suppress(Exception):
            self.assignments.settle_pending()  # turns that finished just before a crash
        self._stopping = False
        self._loop_task = asyncio.create_task(self._run_loop(), name="botoffice-scheduler")

    def _cleanup_stale_attachments(self) -> None:
        root = self.settings.data_dir / "workspaces"
        for job in self.store.list():
            if job.status in {JobStatus.RUNNING, JobStatus.QUEUED}:
                continue
            workspace = root / str(job.id)
            if not workspace.is_symlink():
                shutil.rmtree(workspace / JOB_ATTACHMENTS_DIR, ignore_errors=True)

    def begin_shutdown(self) -> None:
        """SIGTERM/SIGINT reached the controller. systemd signals the whole service cgroup at once, so a
        CLI can die before stop() runs; from now on a turn that did not succeed is an interruption (the
        row stays RUNNING for recover_interrupted), not a provider error to retry later."""
        self._stopping = True

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def liveness(self) -> dict:
        """For /healthz: is the run loop alive, and how long since its last tick (None: never)."""
        task = self._loop_task
        age = round(time.monotonic() - self._last_tick, 1) if self._last_tick else None
        return {"alive": bool(task and not task.done()), "last_tick_age_seconds": age}

    async def _run_loop(self) -> None:
        while not self._stopping:
            # the pause is read once per pass, before any claim: what this pass saw is what it acts on, and
            # the acknowledgement (see pause_acknowledged) is only given for a pass that claims nothing
            self._pause_seen = self.paused()
            self._pause_seen_at = time.monotonic()
            self._reap_tasks()
            for lane in range(self.settings.run_lanes):
                task = self._tasks.get(lane)
                if task and not task.done():
                    continue
                if self._stopping or self._pause_seen:
                    break
                job = self.store.claim(self.settings.desktops, self._enabled_providers())
                if not job:
                    break
                task = asyncio.create_task(self._execute(lane, job), name=f"job-{job.id}")
                self._tasks[lane] = task
                self._job_ids[lane] = str(job.id)
            if time.monotonic() - self._last_tick >= TICK_SECONDS:
                self._last_tick = time.monotonic()
                try:
                    self.tick()
                except Exception:
                    log.exception("scheduler tick failed")
            await asyncio.sleep(0.05)

    def paused(self) -> bool:
        """Maintenance pause: while the file <data_dir>/PAUSE exists nothing new starts and nothing fires
        (no claim, routine, inbox, wake-up or watcher). Running turns finish; queued work waits for the
        file to go. Checked on every pass, so it needs no restart: create it, wait for running_jobs 0
        in /healthz, restart, remove it."""
        try:
            return (self.settings.data_dir / PAUSE_FILE).exists()
        except OSError:
            return False

    def pause_acknowledged(self) -> bool:
        """True only once the run loop itself has seen the pause on a recent pass: it is not in the middle of a
        claim and starts nothing more. Only then is the running count final. (PAUSE merely existing is not
        enough: a claim may already be under way.)"""
        return bool(self._pause_seen and time.monotonic() - self._pause_seen_at <= PAUSE_ACK_FRESH_SECONDS)

    def tick(self) -> None:
        if not self.paused():
            self._fire_routines()
            self._read_inboxes()
            self.assignments.fire_due()
            self.watchers.tick(lambda fn: threading.Thread(target=fn, name="botoffice-poll", daemon=True).start())
            if time.monotonic() - self._last_sweep >= SWEEP_SECONDS:
                self._last_sweep = time.monotonic()
                self.sweep()
        if time.monotonic() - self._last_watchdog >= WATCHDOG_SECONDS:
            self._last_watchdog = time.monotonic()
            self.watchdog()

    def sweep(self) -> None:
        """0.12.39, every SWEEP_SECONDS while not paused: the stall sweep (stall_sweep.py) and, once a day,
        the rules sync from the drop folder (rules_sync.py). Each is model-free; a failure is logged only."""
        try:
            stall_sweep.sweep(self.store, self.assignments, self.settings.stall_sweep_hours,
                              per_hour=self.settings.stall_sweep_per_hour, blocking=False, paused=self.paused)
        except Exception:
            log.exception("stall sweep failed")
        try:
            if self.settings.rules_sync_dir is not None and rules_sync.due(self.store) and not rules_sync.busy():
                # off the loop: a large drop folder must not hold up claims or the watchdog
                threading.Thread(target=self._rules_sync, name="botoffice-rules-sync", daemon=True).start()
        except Exception:
            log.exception("rules sync failed")

    def _rules_sync(self) -> None:
        try:
            rules_sync.run(self.store, self.memory, self.settings.rules_sync_dir, log_dir=self.settings.data_dir / "logs")
        except Exception:
            log.exception("rules sync failed")

    def watchdog(self) -> None:
        """Once a minute, model-free: settle turns the loop missed, flag stalled assignments and
        cancel turns that hang past their timeout."""
        self.assignments.settle_pending()
        self.assignments.mark_stalled(self.settings.stall_hours)
        self.rules.expire()
        try:
            # #10: one notice per job parked on a usage limit > 10 min; it never switches provider
            needs_you.post_usage_notices(self.store)
        except Exception:
            log.exception("usage-limit notices failed")
        try:
            self.post_digest()
        except Exception:
            log.exception("daily digest failed")
        stamp = time.time()
        for lane, job_id in list(self._job_ids.items()):
            task, job = self._tasks.get(lane), self.store.get(job_id)
            if not task or task.done() or not job or job.status != JobStatus.RUNNING or not job.started_at:
                continue
            limit = self.turn_timeout(self.store.get_bot(job.bot_id) if job.bot_id else None) + HUNG_GRACE_SECONDS
            if stamp - job.started_at.timestamp() > limit:
                self._cancel_reasons[job_id] = f"watchdog: still running {limit} s after it started; cancelled"
                if job.bot_id:
                    self.store.add_message(job.bot_id, "system", "A turn hung past its time limit and was cancelled.",
                                           source=job.source or "chat")
                task.cancel()

    def post_digest(self) -> bool:
        """Once a day after BOT_OFFICE_DIGEST_HOUR (New York): yesterday's runs, failures, usage,
        assignment changes and open Needs-you items as a system line in each opted-in bot's chat.
        Built from the database only: no model call."""
        stamp = self.clock()
        if stamp.astimezone(budgets.DAY_TZ).hour < self.settings.digest_hour:
            return False
        day = budgets.day_key(stamp)
        if self.store.get_meta("digest:last") == day:
            return False
        self.store.set_meta("digest:last", day)
        chosen = [bot for bot in self.store.list_bots() if bot.digest]
        if not chosen:
            return False
        with contextlib.closing(self.store._connect()) as db:
            text = budgets.digest(db, stamp, needs_you.needs_you(self.store))
        for bot in chosen:
            self.store.add_message(bot.id, "system", text, source="api")
        return True

    def protected_paths(self) -> tuple[str, ...]:
        """Controller-only files the action gate refuses to touch (tripwire, not a boundary)."""
        control = self.settings.control_dir
        paths = [self.settings.data_dir / "token", self.settings.data_dir / "agentdesk.sqlite3",
                 self.settings.data_dir / "grok_webhook.json", control / "token", control / "agentdesk.sqlite3",
                 control / "grok_webhook.json", self.store.path]
        if self.settings.token_file:
            paths.append(self.settings.token_file)
        paths.append(github_token_path(self.settings))
        if self.settings.private_dir:
            paths.append(self.settings.private_dir)
        return tuple(dict.fromkeys(str(path) for path in paths))

    def gate_refusal(self, job: Job, bot: Bot) -> str | None:
        """Why a strict-profile run (trigger; scheduled turns of a "gated" bot) may not start: its
        provider's action gate is not proven on this host (scripts/probe_gate.sh). Fake and test
        adapters run no tools and are exempt."""
        adapter = self.providers.get(job.provider)
        if not job.bot_id or not is_unattended(job, bot.scheduled_runs) or not isinstance(adapter, CliAdapter):
            return None
        if gate_state(self.settings.control_dir, job.provider) != "enforced":
            return (f"not run: unattended {job.source} turns need a proven action gate for {job.provider} "
                    f"(operator: scripts/probe_gate.sh {job.provider})")
        if job.provider == "codex" and bot.cli_access == "restricted":
            # review C2 #9: restricted Codex runs with --approve-for-me exactly as on master, which
            # wires no gate hook; a probe in full mode says nothing about such a run
            return ("not run: unattended Codex turns need full CLI access for this bot so the action gate "
                    "is wired (restricted Codex has no gate hook)")
        if (job.provider == "codex" and not self.rules.network_allowed(bot.id, True)
                and not helpers_reachable(self.settings.control_dir, "codex")):
            # gen 2: Codex's strict sandbox has no network, so bot-task/bot-msg/desk could not reach
            # Bot-Office; refuse (Needs-you) rather than run a turn that cannot report back
            return ("not run: a strict-profile Codex turn has no network, so bot-task and bot-msg cannot reach "
                    "Bot-Office (operator: re-run scripts/probe_gate.sh codex to check the helpers, or add a "
                    "'preset network allow' rule for this bot)")
        return None

    def gate_fields(self, job: Job, bot: Bot, workspace: Path) -> dict:
        """Per-run gate settings, decided here (never from the job's text) and also written to
        the job workspace for the gate script (.bot-office-gate.json)."""
        unattended = is_unattended(job, bot.scheduled_runs)
        fields = {
            "unattended": unattended,
            "gate_command": f"{REPO_BIN / 'bot-office-gate'} {job.provider} --mode "
                            f"{'unattended' if unattended else 'attended'}",
            "secret_paths": list(self.protected_paths()),
            "allow_network": self.rules.network_allowed(bot.id, unattended),
            "allow_sudo": bot.allow_sudo,
            "github_credential": github_token(self.settings) is not None,
        }
        # isolated: readable, not writable, by the bot; a retried job's workspace may hold a link
        # the bot planted, which the atomic write replaces instead of following
        safefs.write_text(workspace / ".bot-office-gate.json",
                          json.dumps({"mode": "unattended" if unattended else "attended",
                                      "job_id": str(job.id), "provider": job.provider}),
                          isolation.locked_file_mode())
        return fields

    def turn_timeout(self, bot: Bot | None) -> int:
        """The bot's own turn timeout (60 s .. BOT_OFFICE_MAX_TURN_TIMEOUT), else the service default."""
        if bot is None or not bot.job_timeout:
            return self.settings.job_timeout
        return max(60, min(bot.job_timeout, self.settings.max_turn_timeout))

    def _settle(self, job: Job) -> None:
        """The run-until-done loop: charge the assignment and queue its next step (atomic)."""
        if job.assignment_id:
            try:
                self.assignments.settle(job.id)
            except Exception:
                log.exception("assignment turn %s could not be settled; the watchdog retries", job.id)

    def _post_turn(self, bot_ref: str | UUID, text: str, source: str, **kwargs: object) -> None:
        bot = self.store.find_bot(str(bot_ref))
        if not bot:
            return
        try:
            self.turns.post_message(bot, text, source=source, **kwargs)
        except InboxLimitError as exc:
            # dropped without a chat line, so a flood of inbox files cannot flood the chat either
            log.warning("inbox file for bot %s dropped: %s", bot.id, exc)
        except TurnError as exc:
            self.store.add_message(bot.id, "system", f"Could not start turn: {exc}", source=source)

    def _fire_routines(self) -> None:
        current = self.clock().astimezone(UTC).replace(second=0, microsecond=0)
        for routine in self.store.list_routines():
            if not routine.enabled:
                continue
            try:
                self._fire_routine(routine, current)
            except Exception:
                log.exception("routine %s failed to fire", routine.id)

    def _fire_routine(self, routine: Routine, current: datetime) -> None:
        """Scan the minutes since the routine's cursor: due runs up to two minutes old fire as
        usual; older ones were missed while Bot-Office was down and follow the catch-up policy
        (one run now, or none), with one chat notice either way."""
        # a routine without a cursor (pre-0.9.0) starts at the current minute: no invented misses
        cursor = routine.scan_cursor or current - timedelta(minutes=1)
        cursor = cursor.astimezone(UTC).replace(second=0, microsecond=0)
        if cursor == current and routine.pending_run is None:
            return
        if cursor > current:
            # the clock went back: look at the current minute only (last_run_at prevents repeats)
            cursor = current - timedelta(minutes=1)
        first = cursor + timedelta(minutes=1)
        oldest = current - timedelta(hours=self.settings.catchup_hours)
        unscanned = first < oldest
        last = to_cron_time(routine.last_run_at) if routine.last_run_at else None
        seen = {last.replace(tzinfo=None)} if last else set()
        due: list[datetime] = []
        minute = max(first, oldest)
        while minute <= current:
            # cron fields are America/New_York wall-clock time, whatever the host zone is
            local = to_cron_time(minute)
            # skip what already ran, and the repeated wall-clock hour when DST ends (1:30 happens twice)
            if (last is None or local > last) and local.replace(tzinfo=None) not in seen and cron_matches(routine.cron, local):
                seen.add(local.replace(tzinfo=None))
                due.append(local)
            minute += timedelta(minutes=1)
        bot = self.store.find_bot(str(routine.bot_id))
        if not bot:
            self.store.advance_routine(routine, current, pending=routine.pending_run)
            return
        if self.store.routine_active(routine.id):
            # runs never stack, and none is dropped silently: the latest due run is owed until
            # the previous run finishes (catch-up "once"), with one notice when it first comes due
            if not due:
                self.store.advance_routine(routine, current, pending=routine.pending_run)
                return
            notice = None
            if routine.pending_run is None:
                later = ("it will run once when that run finishes" if routine.catch_up == "once"
                         else "skipped (catch-up is off)")
                notice = (f"Routine {self._routine_label(routine)} came due at "
                          f"{failures.show_time(due[-1], self.clock())} while its previous run was still going; {later}.")
            self.store.advance_routine(routine, current, bot.id, notice, pending=due[-1])
            return
        owed = routine.pending_run if routine.catch_up == "once" else None
        if routine.created_by == "bot" and (due or owed) and self._too_soon(routine, due, current):
            # a bot's routine never fires more often than every 15 min, whatever its cron says or
            # however late the previous run started: the run is deferred (catch-up "once") or skipped
            later = (due[-1] if due else to_cron_time(owed)) if routine.catch_up == "once" else None
            self.store.advance_routine(routine, current, pending=later)
            return
        if not due and not unscanned and owed is None:
            self.store.advance_routine(routine, current)
            return
        on_time = [moment for moment in due if current - moment <= ROUTINE_ON_TIME]
        missed = [moment for moment in due if moment not in on_time]
        run = on_time[-1] if on_time else missed[-1] if missed and routine.catch_up == "once" else None
        run = run or (to_cron_time(owed) if owed else None)
        notice = self._missed_notice(routine, missed, unscanned, run is not None) if missed or unscanned else None
        if run is None:
            self.store.advance_routine(routine, current, bot.id, notice)
            return
        try:
            self.turns.post_message(bot, routine.prompt, source="routine", routine_id=routine.id,
                                    routine_scan=(routine, current, run), notice=notice)
        except RoutineChanged:
            return  # edited, disabled or deleted meanwhile: the next tick scans the new version
        except TurnError as exc:
            failed = f"Routine {self._routine_label(routine)} could not start: {exc}"
            self.store.advance_routine(routine, current, bot.id, f"{notice}\n{failed}" if notice else failed)

    def _too_soon(self, routine: Routine, due: list[datetime], current: datetime) -> bool:
        """Whether a run now would follow the previous one by less than the bot minimum gap,
        judged by when that run actually started, not when it was scheduled."""
        if due and routine.last_run_at and due[-1] - to_cron_time(routine.last_run_at) < BOT_ROUTINE_MIN_GAP:
            return True
        started = self.store.routine_last_start(routine.id)
        return started is not None and current - started < BOT_ROUTINE_MIN_GAP

    @staticmethod
    def _routine_label(routine: Routine) -> str:
        return f'"{routine.name}"' if routine.name else f"({routine.cron})"

    def _missed_notice(self, routine: Routine, missed: list[datetime], unscanned: bool, running: bool) -> str:
        hours = self.settings.catchup_hours
        if missed:
            count = f"{len(missed)}{'+' if unscanned else ''} run{'s' if len(missed) > 1 or unscanned else ''}"
            head = (f"Missed {count} of {self._routine_label(routine)} "
                    f"(last due {failures.show_time(missed[-1], self.clock())}) while Bot-Office was not running")
        else:
            head = (f"Runs of {self._routine_label(routine)} may have been missed while Bot-Office was not running "
                    f"(only the last {hours} hours were checked)")
        return f"{head}; {'running once now' if running else 'skipped'}."

    def _read_inboxes(self) -> None:
        root = self.settings.data_dir / "inboxes"
        handled = 0
        # real dirs only (a link to elsewhere is skipped); files are read without following links
        directories = [root / name for name in safefs.names(root, regular=False) if safefs.is_dir(root / name)]
        cutoff = time.time() - INBOX_TEMP_MAX_AGE
        for directory in directories:
            for name in safefs.names(directory, ".*.json.tmp", regular=False):
                path = directory / name
                if INBOX_TEMP_NAME.fullmatch(name):
                    info = safefs.lstat(path)
                    if info is not None and info.st_mtime < cutoff:
                        with contextlib.suppress(OSError):
                            safefs.unlink(path)
        for directory in directories:
            for path in (directory / name for name in safefs.names(directory, "*.json", regular=False)):
                if handled >= MAX_INBOX_PER_TICK:
                    return
                handled += 1
                try:
                    self._handle_inbox_file(path)
                except Exception:
                    log.exception("inbox file %s could not be handled", path.name)

    def _handle_inbox_file(self, path: Path) -> None:
        try:
            try:
                raw = safefs.read_bytes(path, MAX_INBOX_BYTES + 1)  # a link or a FIFO is refused
            finally:
                safefs.unlink(path)
            if len(raw) > MAX_INBOX_BYTES:
                raise ValueError("too large")
            data = json.loads(raw.decode("utf-8"))
            to, text = data["to"], data["text"]
            sender_ref = data.get("from_bot_id")
            if not isinstance(to, str) or not isinstance(text, str) or not text.strip():
                raise ValueError("invalid fields")
        except (OSError, ValueError, KeyError, TypeError):
            with contextlib.suppress(OSError):
                safefs.unlink(path)
            return
        if "from_bot_id" not in data:
            self._post_turn(to, text, "inbox")
            return
        sender = self.store.find_bot(sender_ref) if isinstance(sender_ref, str) else None
        target = self.store.find_bot(to)
        if sender and target:
            try:
                job_id, token = data.get("job_id"), data.pop("agent_token", None)
                if not isinstance(job_id, str) or not isinstance(token, str) or not self.store.valid_desk_agent_token(job_id, sender.id, token):
                    raise TurnError("invalid originating job token")
                self.turns.post_agent_message(sender, target, text, data["job_id"])
            except InboxLimitError as exc:
                log.warning("inbox file from bot %s dropped: %s", sender.id, exc)
                # 0.12.39: a refused send is visible to its sender (a flood only fills the sender's own chat)
                self.store.add_message(sender.id, "system", f"Message to {target.name} not sent: {exc}.",
                                       source="inbox")
            except TurnError as exc:
                self.store.add_message(sender.id, "system", f"Could not send message: {exc}", source="inbox")
        # Claimed bot senders require a valid bot and job binding.

    def _enabled_providers(self) -> frozenset[str]:
        return frozenset(
            name
            for name, adapter in self.providers.items()
            if adapter.enabled and name in self.settings.enabled_providers
        )

    def reconcile_session(self, bot_id: UUID | str, *, seeding: bool = False) -> None:
        """Finish a pending provider-session reset after an edit (idempotent, crash-safe).

        The edit commits the reset marker in SQLite; the filesystem side happens here and
        before every job of the bot, so no job can resume the pre-edit session. Every
        engine's saved session pointer (0.7.3 resume, see providers/cli.py) is renamed to
        ``<engine>-session.edit-<n>`` (kept for audit), never deleted; all engines are
        reset because a bot can switch engines. WORKING.md is rolled back to its state
        before the first archived turn that changed it. *seeding* (a turn that carries the
        rewind seed) also archives any pointer saved since, so the seed always starts a
        fresh session.
        """
        generation = self.store.pending_session_reset(bot_id)
        if generation is None and not seeding:
            return
        directory = isolation.state_dir(self.settings.data_dir / "bots" / str(bot_id))
        owed = generation if generation is not None else self.store.pending_rewind_seed(bot_id)
        tag = f"edit-{owed}" if owed is not None else None  # None: archive_session's reset-<epoch>
        changed = False
        for engine in sorted({*SESSION_ENGINES, *self.providers}):
            pointer = directory / f"{engine}-session"
            info = safefs.lstat(pointer)
            if info is not None and stat.S_ISLNK(info.st_mode):
                safefs.unlink(pointer)
                changed = True
            else:
                changed = archive_session(directory, engine, tag) or changed
        if changed:
            safefs.fsync_dir(directory)
        if generation is None:
            return
        restore = self.store.pending_working_restore(bot_id)
        if restore is not None and self.store.get_bot(bot_id):
            self.memory.write_working(bot_id, restore, who="")  # restored: a known note keeps its date
        self.store.complete_session_reset(bot_id, generation)

    def _reap_tasks(self) -> None:
        for lane, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(lane, None)
                self._job_ids.pop(lane, None)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()

    async def _execute(self, lane: int | str, job: Job) -> None:
        log_path = self.settings.data_dir / "logs" / f"{job.id}.log"
        workspace = self.settings.data_dir / "workspaces" / str(job.id)
        sink = EventSink(self.store, job.id, self.settings.data_dir / "logs" / f"{job.id}.events.jsonl",
                         attempt=job.execution)
        self._sinks[str(job.id)] = sink
        token = sink.install()
        seed_generation: int | None = None
        turn_slash = slash_mod.decode_slash(job.slash)
        slash_turn = bool(turn_slash and turn_slash["mode"] in ("raw", "command"))  # 0.11.14: the CLI's own command line
        try:
            sink.emit("start", provider=job.provider, model=job.model, effort=job.effort, slot=job.slot)
            workspace.mkdir(parents=True, exist_ok=True, mode=isolation.workspace_mode())
            workspace.chmod(isolation.workspace_mode())  # isolated: sticky, the gate files stay
            run_job = job
            adapter = self.providers[job.provider]
            bot = self.store.get_bot(job.bot_id) if job.bot_id else None
            timeout = self.turn_timeout(bot)
            if job.assignment_id:
                # an assignment turn never outruns the minutes left in its budget (checked at claim)
                left = self.assignments.seconds_left(job.assignment_id)
                if left is not None:
                    timeout = max(1, min(timeout, math.ceil(left)))
            refused = isolation.layout_error()
            if refused:  # fail closed (Sec 6): never run a bot outside the layout that was installed
                refused = f"not run: the isolation layout is incomplete ({refused})"
                log_path.write_text(f"Bot-Office: {refused}\n", encoding="utf-8")
                self._finish(job, workspace, JobStatus.FAILED, log_path=log_path, error=refused,
                             result=RunResult("failed", error=refused, failure_kind=failures.LAUNCH),
                             retryable=False)
                return
            refused = self.gate_refusal(job, bot) if bot else None
            if refused:
                log_path.write_text(f"Bot-Office: {refused}\n", encoding="utf-8")
                self._finish(job, workspace, JobStatus.FAILED, log_path=log_path, error=refused,
                             result=RunResult("failed", error=refused, failure_kind=failures.GATE), retryable=False)
                return
            if bot:
                bot_dir = self.memory.bot_dir(bot.id)
                state = isolation.state_dir(bot_dir)  # pointers, marker, snapshots
                marker = state / ".handoff-pending"
                effective_job = job.model_copy(update={"cli_access": bot.cli_access})
                can_resume = getattr(adapter, "_can_resume", lambda job: True)(effective_job)
                current = self.store.job_message(bot.id, job.id)
                # the edit whose seed this turn carries (see prepare_bot_workspace)
                seed_generation = self.store.pending_rewind_seed(bot.id) if current else None
                # reconciliation may archive the pointer; model_switch still needs to know it existed
                had_session = bool(safefs.names(state, "*-session"))
                # never start (or resume) a provider session from before an edit; a seeded
                # turn always starts a fresh one
                self.reconcile_session(bot.id, seeding=bool(current and (current.edit_of is not None
                                                                         or seed_generation is not None)))
                # also on a seeded turn: the seed prompt names the switch, and a pending seed that
                # outgrew its limits falls back to the handoff instead of a plain fresh start
                saved_session = getattr(adapter, "_saved_session", lambda workspace, job: None)
                handoff, resume_switch = model_switch(
                    job, bot_dir, can_resume, safefs.exists(marker), bool(saved_session(workspace, effective_job)),
                    had_session,
                )
                # Persist before launching: a crash or a snapshot failure must force the
                # next turn to rebuild context, even after an in-session model switch.
                if handoff or not can_resume or seed_generation is not None or (current and current.edit_of is not None):
                    _mark_handoff_pending(bot_dir)
                if (handoff and not resume_switch) or not can_resume:
                    archive_error = None
                    try:
                        archive_session(bot_dir, job.provider, f"handoff-{int(time.time())}")
                    except OSError as exc:
                        archive_error = exc
                    pointer = state / f"{job.provider}-session"
                    if safefs.exists(pointer):
                        raise OSError(f"Could not archive {job.provider} session pointer: {pointer} still exists"
                                      f"{f' ({archive_error})' if archive_error else ''}") from archive_error
                # messages sent while this turn was queued or parked: claimed atomically (the provider's
                # steer loop never sees them) and part of every attempt's prompt, also a retry's. Known
                # trade-off: a retry in a resumed provider session may show such a steer a second time.
                parked, claimed = self.store.take_pending_steers(job.id)
                for message_id, stamp in claimed:  # right away: a failing prompt build must not hide them
                    sink.emit("steer", message_id=message_id, status="delivered", delivered_at=stamp)
                prompt, bot_memory, shared = prepare_bot_workspace(
                    self.store, self.memory, bot, job, workspace, timeout, handoff, steers=parked, moment=self.clock()
                )
                self._snapshots[str(job.id)] = (bot_memory, shared)
                run_job = job.model_copy(update={"prompt": prompt, "bot_name": bot.name,
                    "api_url": f"http://127.0.0.1:{self.settings.port}",
                    "agent_token": self.store.issue_agent_token(job),
                    "cli_access": bot.cli_access, "resume_prompt": job.resume_prompt,
                    **self.gate_fields(job, bot, workspace)})
                if turn_slash and slash_turn:
                    # a slash command turn is the CLI's own command line, not the wrapped Bot Office prompt
                    line = (slash_mod.slash_job_text(turn_slash["name"], turn_slash["args"]) if turn_slash["mode"] == "raw"
                            else turn_slash["args"])
                    run_job = run_job.model_copy(update={"prompt": line, "resume_prompt": line})
            else:
                safefs.write_text(workspace / "prompt.txt", job.prompt, isolation.file_mode())
                # A16: a job with no bot (POST /v1/jobs) has no agent token and no chat for an approval
                # card, so the hook runs in "local" mode: the git guard and sending to people are refused
                # outright (never run unchecked), and so is a guarded push at the pre-push check
                safefs.write_text(workspace / ".bot-office-gate.json",
                                  json.dumps({"mode": "local", "job_id": str(job.id), "provider": job.provider}),
                                  isolation.locked_file_mode())
                run_job = job.model_copy(update={
                    "gate_command": f"{REPO_BIN / 'bot-office-gate'} {job.provider} --mode local",
                    "unattended": False})
            result: RunResult = await asyncio.wait_for(
                adapter.run(run_job, workspace, log_path, timeout),
                timeout=timeout + 10,
            )
            if result.status != "succeeded" and not self._stopping and (result.exit_code or 0) < 0:
                # killed by a signal: the service's SIGTERM may reach this process a moment after the child
                await asyncio.sleep(SIGNAL_GRACE_SECONDS)
            if self._stopping and result.status != "succeeded":
                raise asyncio.CancelledError  # the CLI died with the service: see begin_shutdown
            if seed_generation is not None and result.exit_code is not None and not slash_turn:
                # the provider process ran with the seed, so the fresh session has it; a
                # cancellation or a failure before launch keeps it for the next turn
                self.store.complete_rewind_seed(bot.id, seed_generation)
            status = JobStatus(result.status)
            if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.NEEDS_LOGIN}:
                status = JobStatus.FAILED
            if result.final_text and sink.final_text is None:
                sink.final_text = result.final_text
            if bot and status == JobStatus.SUCCEEDED:
                # A failed snapshot write must leave the handoff pending for the next turn.
                try:
                    save_settings_snapshot(bot, self.memory.bot_dir(bot.id), job, extra=job.self_extra)
                except OSError:
                    _mark_handoff_pending(bot_dir)
                    log.warning("Could not save settings snapshot for bot %s", bot.id, exc_info=True)
                else:
                    if can_resume and not slash_turn:  # a slash turn did not carry the handoff: the next turn still must
                        safefs.unlink(marker)
                try:
                    save_rules_snapshot(shared, self.memory.bot_dir(bot.id))
                except OSError:
                    log.warning("Could not save rules snapshot for bot %s", bot.id, exc_info=True)
                for layer in baseline.LAYERS:
                    if job.layer_texts and layer.key in job.layer_texts:
                        try:
                            layer.save_snapshot(job.layer_texts[layer.key], self.memory.bot_dir(bot.id))
                        except OSError:
                            log.warning("Could not save %s snapshot for bot %s", layer.key, bot.id, exc_info=True)
                await asyncio.to_thread(self._prepare_memory, job, workspace)
            # only failures the adapter classified itself are retried automatically
            retryable = bool(result.failure_kind)
            if status == JobStatus.NEEDS_LOGIN:
                result.failure_kind = failures.AUTH
                if self.on_needs_login is not None:
                    self.on_needs_login()
            elif status == JobStatus.FAILED and not result.failure_kind:
                result.failure_kind = failures.classify(result.error, result.exit_code)
            self._finish(
                job, workspace, status, exit_code=result.exit_code, log_path=log_path, error=result.error,
                result=result, retryable=retryable,
            )
        except asyncio.CancelledError:
            if self._stopping:
                # controller shutdown, not a user cancel: the row stays RUNNING so the next
                # start re-queues it once (recover_interrupted) with a chat notice
                raise
            reason = self._cancel_reasons.pop(str(job.id), "cancelled")
            with contextlib.suppress(Exception):
                self._queue_pending_steers(job.id, sink)  # before the terminal state a client may poll
                self.store.finish(
                    job.id,
                    JobStatus.CANCELLED,
                    log_path=str(log_path),
                    error=reason,
                    reply_text=sink.assembled or None,
                )
                self._queue_pending_steers(job.id, sink)
                sink.emit("done", status="cancelled", exit_code=None, error=reason)
            self._settle(job)
            raise
        except TimeoutError:
            if self._stopping:
                raise asyncio.CancelledError from None
            self._finish(
                job, workspace, JobStatus.FAILED, exit_code=124, log_path=log_path, error="job timed out",
                result=RunResult("failed", exit_code=124, failure_kind=failures.TIMEOUT),
            )
        except Exception as exc:
            if self._stopping:
                raise asyncio.CancelledError from exc  # failed because the service is going down
            if not log_path.exists():
                log_path.write_text(f"Bot-Office adapter error: {exc}\n", encoding="utf-8")
            self._finish(job, workspace, JobStatus.FAILED, log_path=log_path, error=str(exc),
                         result=RunResult("failed", failure_kind=failures.LAUNCH))
        finally:
            if not self._stopping:
                self._queue_pending_steers(job.id, sink)
            self.store.revoke_agent_token(job.id)
            self.rules.forget_job(str(job.id))
            if not job.bot_id:
                self._record_refusals(job)
            self._snapshots.pop(str(job.id), None)
            self._prepared.pop(str(job.id), None)
            sink.close()
            self._sinks.pop(str(job.id), None)
            sink.uninstall(token)

    def _record_refusals(self, job: Job) -> None:
        """fix3 #4: what the gate, the shims and the pre-push check refused a job with no bot (it
        cannot wait for an approval), from <workspace>/.bot-office-refused.jsonl, as Needs-you notices."""
        path = self.settings.data_dir / "workspaces" / str(job.id) / git_guard.REFUSED_FILE
        text = safefs.read_text(path, default="", limit=64 * 1024)
        for line in text.splitlines()[:20]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                self.rules.log_refusal(job, str(item.get("what") or ""), str(item.get("why") or ""),
                                       str(item.get("kind") or ""))
        if text:
            with contextlib.suppress(OSError):
                path.unlink()

    def _finish(
        self,
        job: Job,
        workspace: Path,
        status: JobStatus,
        *,
        exit_code: int | None = None,
        log_path: Path,
        error: str | None = None,
        result: RunResult | None = None,
        retryable: bool = True,
    ) -> None:
        reply = workspace / "reply.txt"
        kind = result.failure_kind if result and status != JobStatus.SUCCEEDED else None
        reset = result.retry_at if result else None
        sink = self._sinks.get(str(job.id))
        if sink is not None:
            with contextlib.suppress(Exception):
                sink.flush()  # before a re-queue clears the draft
        # full audit copy: the model's reply.txt, else the assembled stream text
        audit = self._read_text(reply, 10**9).strip() if job.bot_id and not (result and result.reply_file_stale) else ""
        if not audit and sink is not None:
            audit = sink.assembled
        with contextlib.suppress(OSError):
            log_path.chmod(0o600)
        outcome = None
        verified, evidence = None, ""  # 0.12.16: did a done say how it was checked (failures.judge_evidence)
        stated = ("", "")  # the status line the bot ended with, if any (state, reason)
        if status == JobStatus.SUCCEEDED:
            # a finished run is not a finished task: the bot says so on its last line
            audit, state, reason = failures.split_status(audit)
            state, reason = self._waiting_for_bot(job, audit, state, reason)
            if state != "unverified":
                stated = (state, reason)
            outcome = f"{state}: {reason}" if reason and state != "done" else state
            verified, evidence = failures.judge_evidence(state, reason)
            if state in failures.NOT_DONE_LABELS:
                status, kind = JobStatus.INCOMPLETE, failures.INCOMPLETE
                error = f"{failures.NOT_DONE_LABELS[state]}: {reason or 'no reason given'}"
        next_try = failures.retry_at(kind, job.attempts, reset, now()) if status == JobStatus.FAILED and kind and retryable else None
        if sink is not None:
            self._queue_pending_steers(job.id, sink)  # before the terminal state a client may poll
        if next_try is not None:
            # the re-queue and its retry event commit together (store.requeue)
            if self.store.requeue(job.id, next_try, kind, error=error, reply_text=audit or None,
                                  max_per_hour=self.settings.max_retries_per_hour,
                                  event_error=failures.clip(error), usage=result.usage if result else None):
                if sink is not None:
                    sink.seq = self.store.last_event_seq(job.id)
                    self._queue_pending_steers(job.id, sink)
                # same row, same job_id: the chat placeholder stays; the stream switches attempts
                if job.bot_id:
                    with contextlib.suppress(Exception):
                        self._record_outcome(job, workspace, status, log_path, error, audit,
                                             kind=kind, reset=reset, next_try=next_try, stated=stated)
                return
            error = f"{error or kind} (not retried: limit of {self.settings.max_retries_per_hour} automatic retries per hour reached)"
        self.store.finish(
            job.id,
            status,
            exit_code=exit_code,
            log_path=str(log_path),
            error=error,
            reply_path=str(reply) if job.bot_id and reply.is_file() else None,
            reply_text=audit or None,
            failure_kind=kind,
            usage=result.usage if result else None,
            outcome=outcome, verified=verified, evidence=evidence,
        )
        if sink is not None:
            self._queue_pending_steers(job.id, sink)
        if job.bot_id and self.store.get_bot(job.bot_id):
            with contextlib.suppress(Exception):
                self._log_work(job, status, audit or error or "")
        agent_message = self.store.agent_message_for_job(job.id)
        if agent_message and agent_message.get("reply_to"):
            # 0.11.10: this turn read another bot's answer to a steer; the answer needs no answer back
            agent_message = None
        if agent_message:
            sender = self.store.get_bot(agent_message["from_bot"])
            target = self.store.get_bot(agent_message["to_bot"])
            if sender and target:
                if status == JobStatus.SUCCEEDED:
                    text = audit
                elif status == JobStatus.INCOMPLETE:
                    text = f"Message {error[0].lower()}{error[1:]}" + (f"\n\n{audit}" if audit else "")
                else:
                    # only the kind: the target's error text stays out of another bot's context
                    text = f"Message failed ({failures.KIND_LABELS.get(kind or '', kind or status.value)})."
                ids: list[str] = []
                if status in (JobStatus.SUCCEEDED, JobStatus.INCOMPLETE) and text:
                    # the reply's images travel with the copy, into the receiving bot's own store
                    # (same limits and validation as a normal reply); attached markup is removed
                    try:
                        text, ids = attachments.collect_outgoing(
                            self.settings.data_dir, sender.id, workspace, audit, text,
                            self.store.attachment_references(), source_bot_id=target.id,
                            private_dir=self.settings.private_dir)
                    except Exception:
                        log.exception("Could not collect images for the inbox copy of job %s", job.id)
                # dropped if the sender's ask ("to <target>" row of this job) was archived by an edit
                with self.store.chat_lock:
                    if self.store.publish_outcome(sender.id, job.id, "assistant", text or "(no reply)",
                                                  sender=f"from {target.name}", source="inbox",
                                                  attachments=ids) is not None and ids:
                        with contextlib.suppress(Exception):
                            _image_index(self.store, sender)
                # the reply wakes the bot that asked (once; not while it is running: its turn end asks again)
                self._wake_for_replies(sender.id)
        if job.bot_id:
            with contextlib.suppress(Exception):
                self._record_outcome(job, workspace, status, log_path, error, audit, kind=kind, reset=reset,
                                     stated=stated)
            if status == JobStatus.INCOMPLETE and stated[0] == "not_done":
                try:
                    self._auto_continue(job)
                except Exception:
                    log.exception("could not decide auto-continue for job %s", job.id)
                    with contextlib.suppress(Exception):
                        self.store.set_auto_decision(job.id, "error")
                        self.store.add_message(job.bot_id, "system", AUTO_CONTINUE_FAILED, source=job.source)
        self._settle(job)
        if job.bot_id:
            self._wake_for_replies(job.bot_id)
        if sink is not None:
            with contextlib.suppress(Exception):
                sink.emit("final", text=audit)
                sink.emit("done", status=status.value, exit_code=exit_code, error=error)

    # a reason that names the wait for another bot's reply (the bot's own words for it)
    _BOT_WAIT = re.compile(r"\b(?:repl(?:y|ies|ied)|asynchronous\w*|bot-msg|another bot|other bot|"
                           r"hear(?:ing)? back|comes? back|arrives?)\b", re.IGNORECASE)
    # a reason that addresses the owner is never rewritten
    _OWNER_WORDS = re.compile(r"\?|\b(?:you|your|owner|user|approv\w*|decid\w*|choos\w*|pick|confirm\w*)\b", re.IGNORECASE)

    def _waiting_for_bot(self, job: Job, reply: str, state: str, reason: str) -> tuple[str, str]:
        """0.11.12: a turn whose only outstanding item is another bot's reply is not blocked and does not
        need the owner. When the bot ends not_done / waiting_on_user / waiting_on_bot with a message to
        another bot still unanswered, a status that names that wait (or waiting_on_bot) and nothing for the
        owner (no phrase asking them, no question card, no approval), it is a normal finish: ``done`` for a
        chat turn, ``waiting_on_bot`` for a task turn. The reply then wakes the bot (``wake_for_replies``).
        Anything that asks the owner, and ``blocked``, are left as the bot said."""
        if state not in ("not_done", "waiting_on_user", "waiting_on_bot") or not job.bot_id:
            return state, reason
        names = self.store.outstanding_bot_replies(job.id)
        if not names or self.store.pending_approval(job.id) or "```ask" in reply \
                or self.store.has_open_question(job.id):
            return state, reason
        if state != "waiting_on_bot" and self._OWNER_WORDS.search(reason):
            return state, reason
        if autocontinue.needs_user_phrase(reply, reason):
            return state, reason
        named = any(name.lower() in reason.lower() for name in names)
        if state != "waiting_on_bot" and not (named or self._BOT_WAIT.search(reason)):
            return state, reason
        if job.assignment_id:
            return "waiting_on_bot", reason
        return "done", reason

    def _wake_for_replies(self, bot_id: UUID | str) -> None:
        try:
            self.assignments.wake_for_replies(bot_id)
        except Exception:
            log.exception("could not wake bot %s for a bot reply", bot_id)

    def _auto_continue(self, job: Job) -> str:
        """0.11.10: a turn that ended ``not_done`` is continued in the same session when
        ``continuation.decide`` says so (the same rules as the task loop). Not for assignment turns (the
        loop decides those), bot-to-bot or watcher turns, nor a turn whose chat line an edit withdrew.
        A message the owner steered into the turn starts a new chain. Decided once per job (a second
        call returns "already decided"). Returns the decision code."""
        assert job.bot_id
        if job.assignment_id or job.source not in AUTO_CONTINUE_SOURCES or self.store.agent_message_for_job(job.id):
            return "not eligible"
        if not self.store.claim_auto_decision(job.id):
            return "already decided"
        bot = self.store.get_bot(job.bot_id)
        line = self.store.job_message(job.bot_id, job.id)
        if bot is None or line is None or line.archived_at is not None:
            self.store.set_auto_decision(job.id, "withdrawn")
            return "withdrawn"
        settings = continuation.get(self.store)
        steered = self.store.delivered_steers(job.id) > 0
        number = 1 if steered else job.continue_count + 1
        root = job.id if steered or not job.continued_from else job.continued_from
        finished = self.store.get(job.id) or job
        # the turns in a row since the owner last spoke: the chain from its root (the owner's turn)
        run = ([(finished.reply_text or "", (finished.outcome or "").partition(": ")[2])] if steered
               else self.store.chain_turns(root, finished.created_at))
        decision = continuation.decide(settings, continuation.Turn(
            path="chat", state="not_done", run=run, automatic=number - 1, chat_only=bot.chat_only,
            question_open=self.store.has_open_question(job.id), approval_pending=self.store.pending_approval(job.id),
            owner_waiting=self.store.user_turn_waiting(bot.id)))
        # the marker stays "deciding" until the stop line or the queued continuation is written, in
        # the same transaction, so a crash in between is recovered at start (fail_stale_decisions)
        if not decision.go:
            self.store.record_stop_decision(job.id, decision.code, bot.id, continuation.chat_stop_line(decision),
                                            source=job.source)
            return decision.code
        limit = continuation.chat_limit(settings)  # what the chain really gets ("n of max")
        text = AUTO_CONTINUE_LINE.format(n=number, max=limit)

        def mark(db, new_job: Job) -> None:
            db.execute("UPDATE jobs SET auto_decision=? WHERE id=?", (decision.code, str(job.id)))
            db.execute("UPDATE jobs SET continue_count=?, continued_from=?, auto_kind='chat' WHERE id=?",
                       (number, str(root), str(new_job.id)))
            # the owner sees the working bubble carry on ("continuing, n of max"), not these lines
            db.execute("UPDATE messages SET hidden=1 WHERE job_id=? AND role='user' AND sender='Bot-Office'",
                       (str(new_job.id),))
            db.execute("UPDATE messages SET hidden=1 WHERE job_id=? AND role='system' AND text LIKE 'Not done%'",
                       (str(job.id),))
            # 0.12.24: the not_done turn's reply is a step of the work, not a message to the owner: it
            # stays on the job (Activity), and the turn that finishes reports the whole task
            autocontinue.interim_reply(db, str(job.id))

        try:
            self.turns.post_message(bot, text, source=job.source, sender="Bot-Office",
                                    prompt=AUTO_CONTINUE_PROMPT.format(n=number, max=limit), hook=mark)
        except TurnError as exc:
            self.store.add_message(bot.id, "system", f"Could not continue automatically: {exc}", source=job.source)
            self.store.set_auto_decision(job.id, "error")
            return "error"
        return "continued"

    def _log_work(self, job: Job, status: JobStatus, result: str) -> None:
        assert job.bot_id
        moment = to_cron_time(self.clock()).strftime("%Y-%m-%d %H:%M ET")
        result, _, _ = failures.split_status(result)
        paragraphs = [p for p in re.split(r"\n\s*\n", result.strip()) if p.strip()]
        asked = " ".join(job.prompt.split())[:100]
        answered = " ".join(paragraphs[-1].split())[:500] if paragraphs else "(no reply)"
        events, count = self.store.job_assignment_events(job.id)
        summary = ", ".join(f"{(row['assignment_id'] or 'unknown')[:8]} {row['kind']}" for row in events)
        if count > len(events):
            summary += f", and {count - len(events)} more"
        self.memory.append_worklog(job.bot_id, f"{moment} · {status.value} · {job.source} · {asked} → {answered}"
                                   + (f" · tasks: {summary}" if summary else ""))

    @staticmethod
    def _read_text(path: Path, limit: int) -> str:
        """reply.txt, WORKING.md, the log: never through a link (a bot could point reply.txt at
        the token and have it posted in chat)."""
        try:
            return safefs.read_text(path, limit=limit)
        except OSError:
            return ""

    def _record_outcome(
        self,
        job: Job,
        workspace: Path,
        status: JobStatus,
        log_path: Path,
        error: str | None,
        audit: str = "",
        *,
        kind: str | None = None,
        reset: datetime | None = None,
        next_try: datetime | None = None,
        stated: tuple[str, str] = ("", ""),
    ) -> None:
        assert job.bot_id
        if not self.store.get_bot(job.bot_id):
            return  # bot was deleted while the job ran; do not recreate its data
        working = workspace / WORKING_FILE
        if safefs.is_file(working):
            # bounded working state survives compaction and session loss, even after a failed
            # turn; dropped if an edit archived the turn, and snapshotted so an edit can roll it back
            # the ⟦date · age · by⟧ tags shown in the workspace are never stored (freshness)
            with self.store.chat_lock:
                before = self.memory.read_working(job.bot_id)
                # read with room for the tags; the limit applies to the stripped text
                text = cap_working(freshness.strip(self._read_text(working, 2 * MAX_MEMORY_BYTES), before))
                if text != before and self.store.snapshot_working(job.bot_id, job.id, before):
                    bot = self.store.get_bot(job.bot_id)
                    self.memory.write_working(job.bot_id, text, who=bot.name if bot else "a bot")
        if status not in (JobStatus.SUCCEEDED, JobStatus.INCOMPLETE):
            if status in (JobStatus.CANCELLED, JobStatus.NEEDS_LOGIN):
                line = f"Job {status.value}: {failures.clip(error) or status.value}"
            else:
                engine = ENGINE_LABELS.get(job.provider, job.provider)
                line = failures.failure_line(engine, kind or failures.PROVIDER_ERROR, error, reset, next_try, self.clock())
            partial = audit[:REPLY_LIMIT].strip()
            if partial:
                # never drop the provider's own (partial or final) words
                line += f"\n\n---\n{partial}"
            self.store.publish_outcome(job.bot_id, job.id, "system", line, source=job.source,
                                       cut_chars=max(0, len(audit) - REPLY_LIMIT))
            return
        # chat shows a truncated copy and says so (cut_chars); the job keeps it all (GET /v1/jobs/{id})
        cut = max(0, len(audit) - REPLY_LIMIT)
        text = audit[:REPLY_LIMIT].strip()
        from_reply = bool(text)  # the bot's own words (not a status-only line or the log tail)
        if not text and stated[0]:
            # a reply that was only a status line: say that, never the raw log tail
            state, reason = stated
            text = (f"Done — {reason}" if reason else "Done.") if state == "done" else (
                f"({state}: {reason})" if reason else f"({state})")
        sink = self._sinks.get(str(job.id))
        if not text and sink is not None and sink.streamed:
            # 0.12.24: a streaming engine's log is its step-by-step output, never a chat message
            text = "(no reply; the steps are in Activity)"
        if not text:
            log_text = self._read_text(log_path, 10**7).split("\n--- stderr ---\n")[0]
            text = log_text.strip()[-LOG_FALLBACK_LIMIT:] or "(no reply)"
            if len(log_text.strip()) > LOG_FALLBACK_LIMIT:
                text = (f"(no reply; only the last {LOG_FALLBACK_LIMIT:,} of {len(log_text.strip()):,} characters of "
                        f"the log are shown)\n{text}")
        # Image file work happens before the lock; if an edit archives the turn meanwhile the
        # new files stay unreferenced and the TTL prune removes them.
        ids: list[str] = []
        try:
            text, ids = attachments.collect_outgoing(
                self.settings.data_dir, job.bot_id, workspace, audit, text,
                self.store.attachment_references(), private_dir=self.settings.private_dir,
            )
        except Exception:
            log.exception("Could not collect outgoing images for job %s", job.id)
        from . import questions
        text, cards = questions.extract(text)
        if from_reply:
            # 0.12.37: the chat copy loses a leading header, bold labels and a filler closing (house_style);
            # the job keeps the reply as written
            text = house_style.tidy_reply(text)
        # The chat message and the memory write are published together under the chat
        # lock, so an edit either archived this turn first (both are skipped; the job
        # keeps the reply) or commits after both.
        with self.store.chat_lock:
            if self.store.publish_outcome(
                job.bot_id, job.id, "assistant", text, source=job.source, attachments=ids, cut_chars=cut,
            ) is None:
                return
            # One card per reply: the last ```ask fence wins; earlier ones are dropped.
            for card in cards[-1:]:
                questions.create(self.store, job.bot_id, job.id, card)
            if ids:
                with contextlib.suppress(Exception):
                    bot = self.store.get_bot(job.bot_id)
                    if bot:
                        _image_index(self.store, bot)
            if status == JobStatus.INCOMPLETE:
                # an assignment step that carries on is machinery: the next turn shows it (0.11.10)
                # waiting on an approval: the chat's Approve / Deny card already says so
                waiting_card = stated[0] == "waiting_on_user" and self.store.pending_approval(job.id)
                self.store.publish_outcome(job.bot_id, job.id, "system", error or "Not done", source=job.source,
                                           hidden=(bool(job.assignment_id) and stated[0] == "continue") or waiting_card)
            self._write_memory(job, workspace)

    @staticmethod
    def _read_own_file(path: Path, limit: int) -> str | None:
        """A regular file the job wrote, never followed through a symlink (a bot could point
        memory.md at any file it can read and have it copied into shared memory)."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            return stream.read(limit * 4).decode("utf-8", errors="replace")[:limit]

    def _memory_edits(self, job: Job, workspace: Path) -> list[tuple[Path, str, str]]:
        """(target, base, the job's version) of each memory file the job changed."""
        assert job.bot_id
        old_memory, old_shared = self._snapshots.get(str(job.id), ("", ""))
        edits = []
        for name, target, base in (("memory.md", self.memory.bot_path(job.bot_id), old_memory),
                                   ("shared-memory.md", self.memory.shared_path, old_shared)):
            # room for the tags (an untouched tagged copy is at most MAX_MEMORY_BYTES); the limit applies
            # to the stripped text, so a bot's append near the limit is not cut short by the tags
            ours = self._read_own_file(workspace / name, 2 * MAX_MEMORY_BYTES)
            ours = freshness.strip(ours, base)[:MAX_MEMORY_BYTES] if ours is not None else None
            if ours is not None and ours != base:
                edits.append((target, base, ours))
        return edits

    def _prepare_memory(self, job: Job, workspace: Path) -> None:
        """Compute the memory merges ahead of _finish (called in a worker thread, so a large
        diff never blocks the event loop or holds the chat lock)."""
        self._prepared[str(job.id)] = [(target, base, ours, self.memory.prepare_merge(target, base, ours))
                                       for target, base, ours in self._memory_edits(job, workspace)]

    def _write_memory(self, job: Job, workspace: Path) -> None:
        assert job.bot_id
        prepared = self._prepared.pop(str(job.id), None)
        if prepared is None:
            prepared = [(*edit, None) for edit in self._memory_edits(job, workspace)]
        bot = self.store.get_bot(job.bot_id)
        who = bot.name if bot else "a bot"
        # merged, not overwritten: other bots (shared) or the GUI may have saved meanwhile
        for target, base, ours, merge in prepared:
            _, conflict = self.memory.merge_write(target, base, ours, job.id, merge, who=who)
            if base != ours:
                # 0.11.0: the edit as a job event, shown in timelines
                name = "shared" if target == self.memory.shared_path else "memory"
                diff = "\n".join(list(difflib.unified_diff(base.splitlines(), ours.splitlines(), "before", "after",
                                                           n=0, lineterm=""))[2:])
                data = {"file": name, "diff": failures.redact(diff)[:2000]}
                sink = self._sinks.get(str(job.id))
                with contextlib.suppress(Exception):
                    if sink is not None:
                        sink.emit("memory", **data)  # the sink numbers the job's events
                    else:
                        self.store.add_event(job.id, self.store.last_event_seq(job.id) + 1, "memory", data)
            if conflict is not None:
                name = "shared memory" if target == self.memory.shared_path else "memory"
                self.store.publish_outcome(
                    job.bot_id, job.id, "system",
                    f"This turn's {name} edit could not be merged; the current file is kept and "
                    f"this turn's version is saved as {conflict.name}.", source=job.source)

    def _log_steer_turn(self, message_id: int) -> None:
        """Claude-use log entry (with the turn's reason) for the queued turn a steer became."""
        message = self.store.get_message(message_id)
        converted = self.store.get(message.job_id) if message and message.job_id else None
        if converted and converted.provider == "claude":
            with contextlib.suppress(OSError):
                self.turns._log_claude_use(converted)

    def _queue_pending_steers(self, job_id: UUID | str, sink: EventSink | None = None) -> None:
        key = str(job_id)
        if self._steer_deferrals.get(key):
            return
        for message_id in self.store.queue_steers(key):
            self._log_steer_turn(message_id)
            active = sink or self._sinks.get(key)
            queued = self.store.get_message(message_id)
            note = queued.steer_note if queued else None
            if active:
                active.emit("steer", message_id=message_id, status="queued", note=note)
            else:
                self.store.add_event(key, self.store.last_event_seq(key) + 1, "steer",
                                     {"message_id": message_id, "status": "queued", "note": note})

    @contextlib.contextmanager
    def defer_steers_for_edit(self, jobs: list[Job]):
        """Let rewind archive later steers before turning the remaining ones into jobs."""
        keys = [str(job.id) for job in jobs]  # running, or queued/parked with steers attached
        for key in keys:
            self._steer_deferrals[key] = self._steer_deferrals.get(key, 0) + 1
        try:
            yield
        finally:
            for key in keys:
                self._steer_deferrals[key] -= 1
                if not self._steer_deferrals[key]:
                    del self._steer_deferrals[key]
                    job = self.store.get(key)
                    if not job or job.status != JobStatus.RUNNING:  # else the adapter / job end handles them
                        self._queue_pending_steers(key)

    def _cancel_dropped(self, job_ids: list[str]) -> None:
        """A dropped task's running turns stop (they settle without queueing another)."""
        for job_id in job_ids:
            job = self.store.get(job_id)
            if job:
                self._cancel_reasons.setdefault(str(job.id), "its task was dropped")
                self.cancel(job)

    def cancel(self, job: Job, reason: str | None = None) -> None:
        """Stop a running job; *reason* (who and why) is what its cancelled row and chat show."""
        if job.status == JobStatus.RUNNING:
            lane = next((lane for lane, job_id in self._job_ids.items() if job_id == str(job.id)), None)
            task = self._tasks.get(lane) if lane is not None else None
            if task:
                if reason:
                    self._cancel_reasons.setdefault(str(job.id), reason)
                task.cancel()

    def slots(self) -> list[dict[str, object]]:
        leased = {job.slot: job for job in self.store.list() if job.status == JobStatus.RUNNING and job.slot}
        result = []
        for name in self.settings.desktops:
            job = leased.get(name)
            result.append(
                {
                    "slot": name,
                    "status": "busy" if job and job.status == JobStatus.RUNNING else "idle",
                    "current_job": job,
                }
            )
        return result


def on_terminate(callback: Callable[[], None], signums: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)) -> bool:
    """Run ``callback`` when one of ``signums`` arrives, then the handler that was installed before
    (uvicorn's, which starts the graceful shutdown). Only the main thread can install handlers; anywhere
    else (the test client's portal thread) nothing is changed and False is returned."""
    if threading.current_thread() is not threading.main_thread():
        return False
    for signum in signums:
        previous = signal.getsignal(signum)

        def handler(number, frame, previous=previous):
            callback()
            if callable(previous):
                previous(number, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(number, signal.SIG_DFL)
                os.kill(os.getpid(), number)

        signal.signal(signum, handler)
    return True
