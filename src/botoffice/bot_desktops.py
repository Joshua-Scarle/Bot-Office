"""Per-bot desktops (docs/REMOTE-DESKTOPS.md).

Each bot gets its own ``desktop-N`` container (N from FIRST to LAST, so port 5900+N and the existing
ticketed viewer work unchanged), created on demand from the light desktop image, with its home
directory on a named volume. Desktops start lazily, stop when idle, and at most ``max_running`` run
at once. Numbers 1-9 and any container we did not create are never touched.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import isolation
from .config import Settings

log = logging.getLogger("botoffice.desktops")

FIRST = 10
LAST = 99
LABEL_BOT = "bot-office.bot-id"
LABEL_KIND = "bot-office.kind"
KIND = "per-bot"
REGISTRY_NAME = "desktops.json"
READY_TIMEOUT = 20.0
REAP_INTERVAL = 60.0
RESERVE_SECONDS = 120.0
SESSION_GRACE = 5 * 60.0  # a desktop over its session limit by this much is stopped even during a turn   # a desktop just started for someone is not evicted before they can use it

# PID 1 is podman's --init; the shell starts the display stack and waits. Xvfb never listens on TCP;
# x11vnc listens inside the container's own network namespace, published to the host on 127.0.0.1 only.
START_SCRIPT = r"""
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
Xvfb :1 -screen 0 "${BO_DESKTOP_GEOMETRY}x24" -nolisten tcp +extension GLX +render -noreset &
i=0; while [ ! -e /tmp/.X11-unix/X1 ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
DISPLAY=:1 openbox >/tmp/openbox.log 2>&1 &
x11vnc -display :1 -forever -shared -rfbport 5900 -nopw -listen 0.0.0.0 -xkb -quiet >/tmp/x11vnc.log 2>&1 &
wait
"""

Runner = Callable[[list[str], float], Awaitable[tuple[int, bytes, bytes]]]


class DesktopError(Exception):
    """A desktop could not be provided. The message is shown to the bot or the owner."""


_scope_ok: bool | None = None   # can this controller create transient scopes? (probed once)
SCOPE_FAILED = (b"Failed to start transient scope", b"Failed to connect to bus", b"Failed to create bus connection")


async def _reap(proc) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _finish(coro) -> None:
    """Run cleanup to the end even if the caller is cancelled (any number of times) meanwhile; a pending
    cancellation is re-raised only after the cleanup is done."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if not task.cancelled() and task.exception() is not None:
        log.warning("desktop cleanup failed: %s", type(task.exception()).__name__)
    if cancelled:
        raise asyncio.CancelledError


async def _scopes_available(environ: dict[str, str] | None = None) -> bool:
    """Probed once with ``systemd-run --user --scope -- true``. Without scopes, desktops still work but stop
    when Bot-Office restarts (their state is kept): said in the log. Availability is preferred over restart
    survival on purpose."""
    global _scope_ok
    if _scope_ok is None:
        env = os.environ if environ is None else environ
        tool = shutil.which("systemd-run")
        ok = False
        if tool and env.get("XDG_RUNTIME_DIR") and not isolation.active():
            try:
                probe = await asyncio.create_subprocess_exec(
                    tool, "--user", "--scope", "--quiet", "--collect", "--", "true",
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError:
                probe = None
            if probe is not None:
                try:
                    ok = await asyncio.wait_for(asyncio.shield(probe.wait()), 15) == 0
                except TimeoutError:
                    ok = False
                finally:
                    if probe.returncode is None:
                        await _finish(_reap(probe))
        if not ok and not isolation.active():
            log.warning("desktops start without their own systemd scope: a Bot-Office restart stops them "
                        "(state is kept; the next use starts them again)")
        _scope_ok = ok
    return _scope_ok


def scope_prefix(argv: list[str], unit: str, available: bool) -> list[str]:
    """``podman run``/``start`` from inside bot-office.service would leave the container's conmon in the
    service's cgroup, so every service restart would kill the desktop. In the single-user layout they run
    in a transient systemd scope of their own instead (found in the live test). ``systemd-run --scope``
    execs podman in its own process, so ending that process ends podman. The isolated layout runs podman
    as the bots user through the launcher, outside the controller's user manager: unchanged."""
    if argv[:1] not in (["run"], ["start"]) or isolation.active() or not available:
        return []
    tool = shutil.which("systemd-run")
    return [tool, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "--"] if tool else []


async def _stop_if_ours(target: str) -> None:
    """After an aborted run (target = name) or start (target = container id): stop that container by id, and
    only if it is labelled as a per-bot desktop (never one someone else created under the same name)."""
    by_id = number_of(target) is None and target.isalnum()
    code, out, _ = await podman(["ps", "-a", "--filter", f"id={target}" if by_id else f"name=^{target}$",
                                 "--format", "json"], 15)
    try:
        listed = parse_ps(out).values() if not code else ()
    except ValueError:
        listed = ()
    found = next((c for c in listed if (c.id.startswith(target) if by_id else c.name == target)), None)
    if found is None or not found.bot_id or not found.id:
        return
    if found.running:
        code, _, _ = await podman(["stop", "--time", "5", found.id], 30)
        if code:
            log.warning("could not stop %s after an aborted command; it counts toward the cap until stopped",
                        found.name)


def _target(argv: list[str]) -> str | None:
    """The container a run/start acts on (its --name, or start's last argument)."""
    if argv[:1] == ["run"] and "--name" in argv:
        return argv[argv.index("--name") + 1]
    if argv[:1] == ["start"] and len(argv) > 1:
        return argv[-1]
    return None


async def _podman_once(argv: list[str], timeout: float, scoped: bool) -> tuple[int, bytes, bytes]:
    options, header = isolation.exec_options()
    unit = f"bo-desktop-{argv[0]}-{uuid.uuid4().hex[:12]}"
    scope = scope_prefix(argv, unit, scoped)
    try:
        proc = await asyncio.create_subprocess_exec(
            *([*scope, shutil.which("podman") or "podman", *argv] if scope else
              isolation.wrap(["podman", *argv], env_stdin=True, runtime_dir=True)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"stdin": asyncio.subprocess.DEVNULL} | options),
        )
    except OSError:
        if not scope:
            raise
        return 127, b"", SCOPE_FAILED[0] + b" (could not run systemd-run)"

    async def abort() -> None:
        # end podman itself (the process systemd-run exec'd into, or the launcher), then stop whatever it
        # had already started, so nothing keeps acting after the caller has moved on
        if isolation.active():
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            await proc.wait()
        else:
            await _reap(proc)
        target = _target(argv)
        if target:
            await _stop_if_ours(target)

    try:
        out, err = await asyncio.wait_for(proc.communicate(header) if header else proc.communicate(), timeout)
    except TimeoutError:
        await _finish(abort())
        return 124, b"", b"timed out"
    except asyncio.CancelledError:
        await _finish(abort())   # however often we are cancelled, the cleanup finishes first
        raise
    return proc.returncode or 0, out, err


async def podman(argv: list[str], timeout: float = 60.0) -> tuple[int, bytes, bytes]:
    """Run one podman command as the bots user (isolated layout) or directly; run/start in their own scope."""
    global _scope_ok
    scoped = argv[:1] in (["run"], ["start"]) and await _scopes_available()
    code, out, err = await _podman_once(argv, timeout, scoped)
    if scoped and code and any(marker in err for marker in SCOPE_FAILED):
        # the scope itself could not be made (podman never ran): fall back, and stop trying scopes
        log.warning("systemd scope unavailable now; desktops start without one until the next restart")
        _scope_ok = False
        code, out, err = await _podman_once(argv, timeout, False)
    return code, out, err


def desktop_name(number: int) -> str:
    return f"desktop-{number}"


def number_of(name: str) -> int | None:
    prefix, _, digits = name.partition("-")
    if prefix != "desktop" or not digits.isdigit() or digits != str(int(digits)):
        return None
    number = int(digits)
    return number if FIRST <= number <= LAST else None


def volume_name(bot_id: str) -> str:
    return f"bo-desktop-home-{uuid.UUID(str(bot_id))}"


def run_argv(name: str, bot_id: str, settings: Settings) -> list[str]:
    """``podman run`` for a new per-bot desktop: locked down, limited, loopback-only publish."""
    number = number_of(name)
    if number is None:
        raise ValueError("not a per-bot desktop name")
    bot = str(uuid.UUID(str(bot_id)))
    return [
        "run", "-d", "--name", name, "--hostname", name,
        "--init", "--restart", "no",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", settings.desktop_memory, "--cpus", f"{settings.desktop_cpus:g}",
        "--pids-limit", "1024", "--shm-size", "256m",
        "--publish", f"127.0.0.1:{5900 + number}:5900",
        "--volume", f"{volume_name(bot)}:/root",
        "--label", f"{LABEL_BOT}={bot}", "--label", f"{LABEL_KIND}={KIND}",
        "--env", f"BO_DESKTOP_GEOMETRY={settings.desktop_geometry}",
        settings.desktop_image, "sh", "-c", START_SCRIPT,
    ]


@dataclass
class Container:
    name: str
    running: bool
    bot_id: str | None      # set only for containers labelled as our per-bot desktop
    publish_ok: bool        # every published port is on 127.0.0.1
    id: str = ""            # podman's container id: stop/start act on it, never on a name that may be reused


def parse_ps(raw: bytes) -> dict[str, Container]:
    """``podman ps -a --format json`` -> containers named desktop-N (any N)."""
    found: dict[str, Container] = {}
    for item in json.loads(raw or b"[]") or []:
        names = item.get("Names") or []
        names = [names] if isinstance(names, str) else names
        labels = item.get("Labels") or {}
        ours = labels.get(LABEL_KIND) == KIND and isinstance(labels.get(LABEL_BOT), str)
        ports = item.get("Ports") or []
        publish_ok = all(str(p.get("host_ip", "")) == "127.0.0.1" for p in ports if isinstance(p, dict))
        for name in names:
            if isinstance(name, str) and name.startswith("desktop-"):
                cid = item.get("Id")
                found[name] = Container(name, str(item.get("State", "")).lower() == "running",
                                        labels[LABEL_BOT] if ours else None, publish_ok,
                                        cid if isinstance(cid, str) and cid.isalnum() else "")
    return found


class Registry:
    """bot id -> desktop number, in the controller's private dir (bots cannot edit it)."""

    def __init__(self, path: Path, excluded: Iterable[str] = ()):
        self.path = path
        # configured lease-pool names are never a per-bot desktop, even inside 10..99
        self.excluded = {n for name in excluded if (n := number_of(name))}
        self._map: dict[str, int] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            # keep the damaged file for inspection; the mapping is rebuilt from container labels (reconcile)
            log.warning("desktop registry unreadable (%s); rebuilding it from container labels", type(exc).__name__)
            with contextlib.suppress(OSError):
                os.replace(self.path, self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}"))
            raw = {}
        bots = raw.get("bots", {}) if isinstance(raw, dict) else {}
        self._map = {}
        for bot, number in bots.items() if isinstance(bots, dict) else ():
            with contextlib.suppress(ValueError, TypeError):
                if (isinstance(number, int) and FIRST <= number <= LAST and number not in self._map.values()
                        and number not in self.excluded):
                    self._map[str(uuid.UUID(bot))] = number

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"bots": self._map}, stream, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def number(self, bot_id: str) -> int | None:
        return self._map.get(str(uuid.UUID(str(bot_id))))

    def bot(self, name: str) -> str | None:
        number = number_of(name)
        return next((bot for bot, n in self._map.items() if n == number), None) if number else None

    def items(self) -> list[tuple[str, int]]:
        return sorted(self._map.items(), key=lambda item: item[1])

    def reconcile(self, containers: dict[str, Container]) -> None:
        """Container labels win over the file (registry lost, damaged or stale): every bot whose labelled
        per-bot container exists is mapped to it (the lowest number when it has several), and a mapping that
        points at another bot's labelled container is dropped. Nothing is ever created or removed here."""
        labelled: dict[str, int] = {}
        owner_of: dict[int, str] = {}
        for container in containers.values():
            number = number_of(container.name)
            if not number or not container.bot_id or number in self.excluded:
                continue
            try:
                bot = str(uuid.UUID(container.bot_id))
            except ValueError:
                continue
            owner_of[number] = bot
            if bot not in labelled or number < labelled[bot]:
                labelled[bot] = number
        existing = {number_of(c.name) for c in containers.values()} - {None}
        # keep a mapping only if its number has no container yet, or a container labelled for that same bot
        fixed = {bot: n for bot, n in self._map.items() if n not in existing or owner_of.get(n) == bot}
        for bot, number in labelled.items():
            fixed = {b: n for b, n in fixed.items() if n != number or b == bot}
            fixed[bot] = number
        if fixed != self._map:
            log.warning("desktop registry corrected from container labels")
            self._map = fixed
            self.save()

    def allocate(self, bot_id: str, containers: dict[str, Container]) -> int:
        """The bot's number, or the lowest free one. A number is free when no other bot has it and no
        container of that name exists that is not this bot's (a hand-made one is never adopted)."""
        bot = str(uuid.UUID(str(bot_id)))
        if bot in self._map:
            return self._map[bot]
        taken = set(self._map.values())
        for number in range(FIRST, LAST + 1):
            existing = containers.get(desktop_name(number))
            if number in taken or number in self.excluded or (existing is not None and existing.bot_id != bot):
                continue
            self._map[bot] = number
            self.save()
            return number
        raise DesktopError("no free desktop number is left (desktop-10 .. desktop-99 are all taken)")


def idle_to_stop(running: Iterable[str], last_used: dict[str, float], viewers: dict[str, int],
                 busy_bots: set[str], bot_of: Callable[[str], str | None], now: float,
                 idle_seconds: float) -> list[str]:
    """Per-bot desktops to stop: idle long enough, no viewer, and their bot has no running turn."""
    if idle_seconds <= 0:
        return []
    return sorted(name for name in running
                  if not viewers.get(name)
                  and bot_of(name) not in busy_bots
                  and now - last_used.get(name, now) >= idle_seconds)


def evict_candidate(running: Iterable[str], keep: str, last_used: dict[str, float], viewers: dict[str, int],
                    busy_bots: set[str], bot_of: Callable[[str], str | None]) -> str | None:
    """At the cap: the least recently used running desktop that nobody is using (or None)."""
    free = [name for name in running
            if name != keep and not viewers.get(name) and bot_of(name) not in busy_bots]
    return min(free, key=lambda name: (last_used.get(name, 0.0), name)) if free else None


async def _rfb_ready(port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
    except (OSError, TimeoutError):
        return False
    try:
        greeting = await asyncio.wait_for(reader.readexactly(12), 2)
        return greeting.startswith(b"RFB ")
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        return False
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


# --- 0.12.47: desktop sessions with explicit, capped extensions and a loop / no-progress stop ---

LOOP_ACTIONS = 8        # the same desk action (command and input) this many times in a row: a loop
LOOP_KEYS = 20          # ... for `desk key` (paging down a long page repeats one key)
LOOP_SHOTS = 8          # this many identical screenshots in a row, over at least SHOTS_SPAN: no progress
SHOTS_SPAN = 120.0
EXTEND_WINDOW = 15 * 60  # an extension is accepted only in the last 15 minutes of the session (no stacking)
MIN_REASON = 10


@dataclass
class Session:
    started: float
    last_activity: float
    budget: float               # seconds from started
    extensions: list = field(default_factory=list)   # (at, reason)
    actions: list = field(default_factory=list)      # recent action fingerprints
    shots: list = field(default_factory=list)        # recent (digest, at) of screenshots
    stopped: str = ""           # "loop" / "no progress": refused until a gap with no desk use


class SessionLimits:
    """Per-desktop session clock. A session is continuous desk use; idle_seconds without any desk command
    (refused ones count as use) starts a new one. It lasts base seconds; `extend` adds step seconds, only
    while the bot's turn runs, with a stated reason, in the session's last EXTEND_WINDOW, up to cap seconds
    in total. LOOP_ACTIONS identical actions (LOOP_KEYS for a key) with no change of screen in between, or
    LOOP_SHOTS identical screenshots in a row, stop it."""

    def __init__(self, base: float, step: float, cap: float, idle: float, clock: Callable[[], float]):
        self.base, self.step, self.cap, self.idle, self.clock = base, step, cap, idle, clock
        self.sessions: dict[str, Session] = {}

    def _current(self, name: str) -> Session:
        now = self.clock()
        session = self.sessions.get(name)
        gap = self.idle if self.idle > 0 else self.base
        if session is None or now - session.last_activity >= gap:
            session = self.sessions[name] = Session(started=now, last_activity=now, budget=self.base)
        return session

    def state(self, name: str) -> dict:
        session = self.sessions.get(name)
        if session is None:
            return {"active": False}
        now = self.clock()
        return {"active": True, "minutes_used": round((now - session.started) / 60, 1),
                "minutes_left": round(max(0.0, session.started + session.budget - now) / 60, 1),
                "extensions": len(session.extensions), "cap_minutes": round(self.cap / 60),
                "stopped": session.stopped or None}

    def check(self, name: str, command: str | None, fingerprint: str | None) -> str | None:
        """Record a desk command; the refusal text, or None when it may run."""
        session = self._current(name)
        now = self.clock()
        session.last_activity = now
        if session.stopped:
            return (f"your desktop session was stopped ({session.stopped}): the same thing kept repeating with no "
                    f"change. Try a different approach without the desktop; desk works again after "
                    f"{round(self.idle / 60)} minutes with no desk use.")
        if now - session.started >= session.budget:
            used = round((now - session.started) / 60)
            if session.budget >= self.cap:
                return (f"your desktop session reached its hard limit ({round(self.cap / 60)} minutes in total). "
                        "Finish without the desktop or report what is left; desk works again after "
                        f"{round(self.idle / 60)} minutes with no desk use.")
            return (f"your desktop session reached its time limit ({used} minutes). If the task genuinely needs "
                    f"more, run `desk extend --reason \"<why, and what is left>\"` (+{round(self.step / 60)} min, "
                    f"up to {round(self.cap / 60)} min in total); otherwise finish without the desktop.")
        if command and command != "screenshot":
            limit = LOOP_KEYS if command == "key" else LOOP_ACTIONS
            session.actions = (session.actions + [f"{command}:{fingerprint or ''}"])[-LOOP_KEYS:]
            recent = session.actions[-limit:]
            if len(recent) == limit and len(set(recent)) == 1:
                session.stopped = "loop: the same action repeated"
                return "loop"
        return None

    def screenshot(self, name: str, digest: str) -> str | None:
        session = self._current(name)
        session.last_activity = self.clock()
        now = self.clock()
        if session.shots and session.shots[-1][0] != digest:
            session.actions = []   # the screen changed: repeating an action (a Next button) is progress
        session.shots = (session.shots + [(digest, now)])[-LOOP_SHOTS:]
        if (len(session.shots) == LOOP_SHOTS and len({d for d, _ in session.shots}) == 1 and not session.stopped
                and now - session.shots[0][1] >= SHOTS_SPAN):
            session.stopped = "no progress: the screen did not change"
            return "no progress"
        return None

    def extend(self, name: str, reason: str, turn_running: bool) -> tuple[bool, str]:
        reason = " ".join((reason or "").split())
        if not turn_running:
            return False, "an extension needs a running turn of the bot"
        if len(reason) < MIN_REASON:
            return False, f"give a reason of at least {MIN_REASON} characters (why, and what is left)"
        session = self._current(name)
        now = self.clock()
        session.last_activity = now
        if session.stopped:
            return False, f"the session was stopped ({session.stopped}); an extension cannot restart it"
        left = session.started + session.budget - now
        if left > EXTEND_WINDOW:
            return False, (f"{round(left / 60)} minutes are still left; extend in the last "
                           f"{EXTEND_WINDOW // 60} minutes of the session")
        if session.budget + self.step > self.cap + 1e-6:
            return False, f"the hard limit is {round(self.cap / 60)} minutes in total; no more extensions"
        session.budget += self.step
        session.extensions.append((now, reason[:300]))
        return True, (f"extended by {round(self.step / 60)} minutes (extension {len(session.extensions)}; "
                      f"{round((session.started + session.budget - now) / 60)} minutes left, hard limit "
                      f"{round(self.cap / 60)} minutes in total)")

    def over(self, name: str, grace: float) -> str | None:
        """Why the reaper should stop this desktop now (stopped by the loop check, over its budget by *grace*,
        or no desk use for the idle time, also during a turn), else None."""
        session = self.sessions.get(name)
        if session is None:
            return None
        if session.stopped:
            return session.stopped
        now = self.clock()
        if now - session.started >= session.budget + grace:
            return "session time limit"
        if self.idle > 0 and now - session.last_activity >= self.idle:
            return "no desk activity"
        return None


class DesktopManager:
    def __init__(self, settings: Settings, *, busy_bots: Callable[[], set[str]] = set,
                 runner: Runner = podman, clock: Callable[[], float] = time.monotonic,
                 ready: Callable[[int], Awaitable[bool]] = _rfb_ready, registry_path: Path | None = None):
        self.settings = settings
        self.busy_bots = busy_bots
        self.runner = runner
        self.clock = clock
        self.ready = ready
        self.registry = Registry(registry_path or settings.control_dir / REGISTRY_NAME, settings.desktops)
        self.last_used: dict[str, float] = {}
        self.viewers: dict[str, int] = {}
        self.controllers: dict[str, int] = {}
        self.reserved: dict[str, float] = {}   # name -> until (clock): just started, not evictable yet
        self.stopping: set[str] = set()         # being stopped: no new viewer is admitted
        self._ids: dict[str, str] = {}          # name -> id of OUR container, from the latest listing
        self._lock = asyncio.Lock()
        self.limits = SessionLimits(settings.desktop_session_minutes * 60, settings.desktop_extend_minutes * 60,
                                    settings.desktop_max_session_minutes * 60, settings.desktop_idle_minutes * 60,
                                    clock)
        self.audit: Callable[[str, str, str], None] = lambda name, tool, text: None   # set by the app

    @property
    def enabled(self) -> bool:
        return self.settings.desktop_mode == "per-bot"

    def bot_of(self, name: str) -> str | None:
        return self.registry.bot(name)

    def name_of(self, bot_id: str) -> str | None:
        number = self.registry.number(bot_id)
        return desktop_name(number) if number else None

    def touch(self, name: str) -> None:
        self.last_used[name] = self.clock()

    def owner_controlling(self, name: str) -> bool:
        return self.controllers.get(name, 0) > 0

    def _protected(self, name: str) -> bool:
        return self.reserved.get(name, 0.0) > self.clock()

    @contextlib.contextmanager
    def viewing(self, name: str, mode: str):
        """Count a viewer (and a controller) for the lifetime of a websocket. Refused while the desktop is
        being stopped (the stop decision and this check never interleave: both run without awaiting)."""
        if name in self.stopping:
            raise DesktopError(f"{name} is stopping; press Connect again")
        self.viewers[name] = self.viewers.get(name, 0) + 1
        if mode == "control":
            self.controllers[name] = self.controllers.get(name, 0) + 1
        self.touch(name)
        try:
            yield
        finally:
            self.viewers[name] -= 1
            if mode == "control":
                self.controllers[name] -= 1
            self.touch(name)

    async def containers(self) -> dict[str, Container]:
        code, out, err = await self.runner(["ps", "-a", "--format", "json"], 15)
        if code:
            raise DesktopError("cannot list desktop containers")
        try:
            found = parse_ps(out)
        except ValueError as exc:
            raise DesktopError("cannot read the desktop container list") from exc
        self.registry.reconcile(found)
        # only containers labelled per-bot for the bot the registry maps to that name are ever acted on
        self._ids = {c.name: c.id for c in found.values()
                     if c.id and c.bot_id and number_of(c.name) and self.registry.bot(c.name) == c.bot_id}
        return found

    def _running_ours(self, containers: dict[str, Container]) -> list[str]:
        """Running per-bot desktops we manage (registry and label agree): the ones we may stop."""
        return [c.name for c in containers.values()
                if c.running and c.bot_id and number_of(c.name) and self.registry.bot(c.name) == c.bot_id]

    @staticmethod
    def _running_labelled(containers: dict[str, Container]) -> int:
        """Every running per-bot-labelled desktop counts toward the cap, managed or not."""
        return sum(1 for c in containers.values() if c.running and c.bot_id and number_of(c.name))

    async def ensure(self, bot_id: str) -> str:
        """Start (creating if needed) the bot's own desktop and wait until its VNC answers."""
        if not self.enabled:
            raise DesktopError("per-bot desktops are off (BOT_OFFICE_DESKTOP_MODE=pool)")
        bot = str(uuid.UUID(str(bot_id)))
        async with self._lock:
            containers = await self.containers()
            name = desktop_name(self.registry.allocate(bot, containers))
            current = containers.get(name)
            if current is not None and current.bot_id != bot:
                raise DesktopError(f"{name} exists but is not this bot's desktop")
            if current is not None and not current.publish_ok:
                raise DesktopError(f"{name} publishes a port beyond 127.0.0.1; refusing to use it")
            if current is None or not current.running:
                await self._make_room(name, containers)
                if current is None:
                    code, out, err = await self.runner(run_argv(name, bot, self.settings), 120)
                    action = "create"
                    cid = out.decode(errors="replace").strip()
                    if not code and cid.isalnum():
                        self._ids[name] = cid
                else:
                    cid = self._ids.get(name)
                    if not cid:
                        raise DesktopError(f"{name} is not a desktop Bot-Office manages")
                    code, _, err = await self.runner(["start", cid], 60)
                    action = "start"
                if code:
                    log.warning("desktop %s %s failed: %s", name, action, err.decode(errors="replace")[-300:])
                    raise DesktopError(f"could not {action} {name}")
                log.info("desktop %s %sed for bot %s", name, action.rstrip("e"), bot)
            self.touch(name)
            self.reserved[name] = self.clock() + RESERVE_SECONDS
        port = 5900 + (number_of(name) or 0)
        deadline = self.clock() + READY_TIMEOUT
        while not await self.ready(port):
            if self.clock() >= deadline:
                raise DesktopError(f"{name} started but its display did not come up in time")
            await asyncio.sleep(0.5)
        return name

    async def refresh(self) -> bool:
        """Reconcile the registry with container labels (listing and Start right after a lost registry).
        False when the live listing failed (callers that verify ownership must then refuse)."""
        async with self._lock:
            try:
                await self.containers()   # reconciles the registry as a side effect
            except DesktopError:
                return False
            return True

    async def start(self, name: str) -> str:
        """Owner "Connect" on a stopped per-bot desktop."""
        bot = self.bot_of(name)
        if bot is None:
            raise DesktopError("not a per-bot desktop")
        return await self.ensure(bot)

    async def _make_room(self, name: str, containers: dict[str, Container]) -> None:
        """Called with the lock held. Stops LRU idle desktops until one more fits; fails closed."""
        running = self._running_ours(containers)
        count = self._running_labelled(containers)
        while count >= self.settings.desktop_max_running:
            # recomputed before every stop: a reservation, viewer or turn may have appeared during the last one
            candidates = [n for n in running if not self._protected(n)]
            victim = evict_candidate(candidates, name, self.last_used, self.viewers, self.busy_bots(), self.bot_of)
            if victim is None:
                raise DesktopError(f"all {self.settings.desktop_max_running} desktops are in use; try again later")
            if not await self.stop(victim, "making room"):
                raise DesktopError(f"could not stop {victim} to make room; try again later")
            running.remove(victim)
            count -= 1

    async def stop(self, name: str, why: str) -> bool:
        """Stop one desktop. No viewer is admitted from the moment it is chosen until the stop finished."""
        cid = self._ids.get(name)
        if number_of(name) is None or not cid:
            return False
        self.stopping.add(name)
        try:
            # by id: if the container was replaced since the listing, the old id no longer exists and nothing stops
            code, _, _ = await self.runner(["stop", "--time", "10", cid], 60)
        finally:
            self.stopping.discard(name)
        if not code:
            log.info("desktop %s stopped (%s)", name, why)
            self.last_used.pop(name, None)
            self.reserved.pop(name, None)
        return not code

    async def stop_now(self, name: str, why: str) -> bool:
        """Stop one per-bot desktop at once (the loop / no-progress stop), from a fresh listing, under the lock;
        only if it is still the same stopped session after the listing (a new session meanwhile keeps it)."""
        session = self.limits.sessions.get(name)
        async with self._lock:
            try:
                await self.containers()
            except DesktopError:
                return False
            current = self.limits.sessions.get(name)
            if (name not in self._ids or self.owner_controlling(name) or current is not session
                    or session is None or not session.stopped):
                return False
            return await self.stop(name, why)

    async def reap_idle(self) -> list[str]:
        idle_seconds = self.settings.desktop_idle_minutes * 60
        if not self.enabled:   # pool mode (rollback) leaves per-bot desktops alone
            return []
        async with self._lock:
            try:
                containers = await self.containers()
            except DesktopError:
                return []
            running = [n for n in self._running_ours(containers) if not self._protected(n)]
            now = self.clock()
            for name in running:
                self.last_used.setdefault(name, now)   # unknown (after a restart): grace from now
            stopped = []
            for name in running:
                # 0.12.47: session limit, loop stop, or no desk use for the idle time (also during a turn); not
                # while the owner watches or controls it. Re-checked right before the stop.
                why = self.limits.over(name, SESSION_GRACE)
                if why and not self.viewers.get(name) and not self.owner_controlling(name) \
                        and self.limits.over(name, SESSION_GRACE) == why and await self.stop(name, why):
                    self.audit(name, "desk session stop", f"{name} stopped: {why}")
                    stopped.append(name)
            running = [n for n in running if n not in stopped]
            if idle_seconds <= 0:
                return stopped
            for name in idle_to_stop(running, self.last_used, self.viewers, self.busy_bots(), self.bot_of,
                                     now, idle_seconds):
                # re-checked right before each stop: a viewer, a turn or a use may have come during an earlier stop
                if (self.viewers.get(name) or self._protected(name) or self.bot_of(name) in self.busy_bots()
                        or self.clock() - self.last_used.get(name, self.clock()) < idle_seconds):
                    continue
                if await self.stop(name, "idle"):
                    stopped.append(name)
            return stopped

    async def run_reaper(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            try:
                await self.reap_idle()
            except Exception as exc:  # noqa: BLE001  the reaper must keep running
                log.warning("desktop idle check failed: %s", type(exc).__name__)
