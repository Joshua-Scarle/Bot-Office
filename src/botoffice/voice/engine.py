"""Controller side of a voice worker process: lazy start, one request at a time, a
bounded queue, hard timeouts that kill the process, and idle shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import resource
import signal
import struct
import time
from collections.abc import Callable, Sequence
from pathlib import Path

MAX_FRAME = 16 * 1024 * 1024
# only these reach the worker; never the controller's token or provider keys
WORKER_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")
# Worker resource limits. Address space is generous (a loaded model plus onnxruntime /
# CTranslate2 thread arenas stay well below it); NPROC counts every task of this uid, so
# it is the current count plus headroom for the engine's own threads.
WORKER_AS_BYTES = 8 * 1024**3
WORKER_NPROC_HEADROOM = 512


class VoiceError(Exception):
    status = 503


class VoiceBusy(VoiceError):
    status = 429


class VoiceTimeout(VoiceError):
    status = 504


class VoiceFailed(VoiceError):
    status = 502


def worker_env(home: Path | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in WORKER_ENV if key in os.environ}
    env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", PYTHONDONTWRITEBYTECODE="1",
               OMP_NUM_THREADS="4", PYTHONUNBUFFERED="1", MALLOC_ARENA_MAX="4")
    if home is not None:
        env["HOME"] = str(home)  # a private scratch dir, not the controller's home
    env.update(extra or {})
    return env


def _user_tasks() -> int:
    """Processes and threads currently owned by this uid (what RLIMIT_NPROC counts)."""
    uid, count = str(os.getuid()), 0
    with contextlib.suppress(OSError):
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/status", "rb") as status:
                    fields = dict(line.split(b":", 1) for line in status.read().splitlines() if b":" in line)
            except OSError:
                continue
            if fields.get(b"Uid", b"").split()[:1] == [uid.encode()]:
                count += int(fields.get(b"Threads", b"1").strip() or 1)
    return count


def worker_limits() -> list[tuple[int, int]]:
    """(resource, value) pairs applied as both soft and hard limit in the worker."""
    limits = [(resource.RLIMIT_CORE, 0)]
    for kind, wanted in ((resource.RLIMIT_AS, WORKER_AS_BYTES),
                         (resource.RLIMIT_NPROC, _user_tasks() + WORKER_NPROC_HEADROOM)):
        _, hard = resource.getrlimit(kind)
        limits.append((kind, wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)))
    return limits


def _limiter(limits: list[tuple[int, int]]) -> Callable[[], None]:
    def apply() -> None:  # runs in the forked child before exec: setrlimit calls only
        for kind, value in limits:
            resource.setrlimit(kind, (value, value))
    return apply


class WorkerEngine:
    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        timeout: float,
        load_timeout: float = 180.0,
        max_waiters: int = 2,
        log_path: Path | None = None,
        home: Path | None = None,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.argv = list(argv)
        self.timeout = timeout
        self.load_timeout = load_timeout
        self.max_waiters = max_waiters
        self.log_path = log_path
        self.home = home
        self.env = env
        self.clock = clock
        self.proc: asyncio.subprocess.Process | None = None
        self.last_used = 0.0
        self.load_ms: int | None = None
        self.starts = 0
        self._lock = asyncio.Lock()
        self._inflight = 0

    @property
    def loaded(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def request(self, header: dict, payload: bytes = b"") -> tuple[dict, bytes]:
        """One active request plus up to *max_waiters* queued; more raise VoiceBusy."""
        if self._inflight > self.max_waiters:
            raise VoiceBusy(f"{self.name} is busy, try again shortly")
        self._inflight += 1
        try:
            async with self._lock:
                done = False
                try:
                    proc = await self._ensure()
                    reply, data = await asyncio.wait_for(exchange(proc, header, payload), self.timeout)
                    done = True
                except TimeoutError as exc:
                    raise VoiceTimeout(f"{self.name} timed out") from exc
                except (OSError, EOFError, ValueError, asyncio.IncompleteReadError) as exc:
                    raise VoiceFailed(f"{self.name} worker stopped unexpectedly") from exc
                finally:
                    self.last_used = self.clock()
                    if not done:
                        # Any failure, including cancellation mid-request or mid-load, may
                        # leave an unread reply (or ready frame) in the pipe: never reuse
                        # that worker. stop() detaches it at once; shielded so a second
                        # cancellation cannot leave the process running.
                        await asyncio.shield(self.stop())
                if not reply.get("ok"):
                    raise VoiceFailed(f"{self.name} failed: {reply.get('error', 'unknown error')}")
                return reply, data
        finally:
            self._inflight -= 1

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self.loaded:
            assert self.proc is not None
            return self.proc
        await self.stop()
        log = None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            log = os.fdopen(fd, "ab")
        try:
            if self.home is not None:
                self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=log or asyncio.subprocess.DEVNULL, env=worker_env(self.home, self.env), cwd="/",
                start_new_session=True, limit=MAX_FRAME, preexec_fn=_limiter(worker_limits()),
            )
        except OSError as exc:
            raise VoiceError(f"{self.name} worker could not start") from exc
        finally:
            if log:
                log.close()
        self.starts += 1
        try:
            ready, _ = await asyncio.wait_for(read_frame(self.proc), self.load_timeout)
        except TimeoutError as exc:
            raise VoiceTimeout(f"{self.name} model load timed out") from exc
        except (OSError, EOFError, ValueError, asyncio.IncompleteReadError) as exc:
            raise VoiceFailed(f"{self.name} worker failed to start") from exc
        if not ready.get("ready"):
            raise VoiceFailed(f"{self.name} worker failed to start: {ready.get('error', '')}"[:300])
        self.load_ms = ready.get("load_ms")
        return self.proc

    async def stop(self) -> None:
        """Kill and reap the worker without blocking the event loop (frees its RAM)."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # its own session: anything it forked too
            except OSError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)

    async def reap_if_idle(self, idle_seconds: float) -> bool:
        """Stop the worker after *idle_seconds* without use, never during or with queued work."""
        if self.proc is None or self._inflight or self._lock.locked():
            return False
        if self.clock() - self.last_used < idle_seconds:
            return False
        await self.stop()
        return True


async def exchange(proc: asyncio.subprocess.Process, header: dict, payload: bytes) -> tuple[dict, bytes]:
    assert proc.stdin is not None
    raw = json.dumps(header).encode()
    proc.stdin.write(struct.pack(">I", len(raw)) + raw + struct.pack(">I", len(payload)) + payload)
    await proc.stdin.drain()
    return await read_frame(proc)


async def read_frame(proc: asyncio.subprocess.Process) -> tuple[dict, bytes]:
    assert proc.stdout is not None
    parts = []
    for _ in range(2):
        (size,) = struct.unpack(">I", await proc.stdout.readexactly(4))
        if size > MAX_FRAME:
            raise ValueError("frame too large")
        parts.append(await proc.stdout.readexactly(size))
    return json.loads(parts[0]), parts[1]
