"""Cached engine sign-in status for GET /v1/providers (0.12.8).

Every call used to probe each engine's CLI (median 2.0 s, up to 5.7 s), and Settings waits for it.
The result is now kept for ``TTL`` seconds and served stale-while-revalidate: an old answer is returned
at once and a single background probe renews it. One probe runs at a time, so concurrent callers share
it. A caller that needs the truth now (``refresh=True``, or after ``invalidate()``) waits for a probe
that started after its own request (or in the last second) and fails with ProviderProbeError if that probe\nraises. A background renewal that raises keeps the previous answer; an answer with an
``error`` engine is only trusted for ``ERROR_TTL`` so a transient failure is retried soon."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

TTL = 60.0
ERROR_TTL = 10.0
JOIN_WINDOW = 1.0   # a refresh may share a probe that began this recently (a double click, two tabs)


class ProviderProbeError(RuntimeError):
    """A probe the caller waited for failed (the cache has nothing it may trust for this request)."""


class ProviderStatusCache:
    def __init__(self, probe: Callable[[], Awaitable[list]], *, ttl: float = TTL, error_ttl: float = ERROR_TTL,
                 clock: Callable[[], float] = time.monotonic):
        self._probe = probe
        self._ttl = ttl
        self._error_ttl = error_ttl
        self._clock = clock
        self._value: list | None = None
        self._expires = 0.0          # fresh until this time
        self._task: asyncio.Task | None = None
        self._task_started = 0.0
        self._task_epoch = 0
        self._must_probe = False
        self._epoch = 0              # bumped by invalidate(): an older probe may not be stored

    def _fresh(self) -> bool:
        return self._value is not None and self._clock() < self._expires

    def invalidate(self) -> None:
        """The next get() waits for a new probe; an answer in flight from before this is not stored."""
        self._epoch += 1
        self._must_probe = True

    def _start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task_started = self._clock()
            self._task_epoch = self._epoch
            self._task = asyncio.create_task(self._run(self._epoch), name="botoffice-provider-probe")
        return self._task

    async def _run(self, epoch: int) -> tuple[int, list | None]:
        try:
            value = await self._probe()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Provider status probe failed; keeping the last answer", exc_info=True)
            return epoch, None
        if epoch == self._epoch:
            bad = any(getattr(item, "login_status", None) == "error" for item in value)
            self._value = value
            self._must_probe = False
            self._expires = self._clock() + (self._error_ttl if bad else self._ttl)
        return epoch, value

    def _joinable(self) -> bool:
        return self._task_epoch == self._epoch and (
            self._must_probe or self._clock() - self._task_started <= JOIN_WINDOW)

    async def get(self, refresh: bool = False) -> list:
        arrived = self._clock()
        if self._value is not None and not refresh and not self._must_probe:
            if not self._fresh():
                self._start()  # stale: answer now, renew behind the caller
            return list(self._value)
        need_new = refresh or self._must_probe
        for _ in range(3):
            task = self._task
            if need_new and task is not None and not task.done() and not self._joinable():
                # a probe that began before this request may hold the old sign-in state: let it end, then ask again
                with contextlib.suppress(Exception):
                    await asyncio.shield(task)
            epoch, value = await asyncio.shield(self._start())
            if value is None:
                raise ProviderProbeError("the engine status probe failed")
            if epoch == self._epoch:
                return list(value)
            # invalidated while it ran: that answer is out of date, look again
        return list(value)

    async def warm(self) -> None:
        with contextlib.suppress(Exception):
            await self.get()

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
