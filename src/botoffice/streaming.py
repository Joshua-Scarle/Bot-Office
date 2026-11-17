"""Per-job event sink: stores stream events for audit and live replay.

Adapters call :func:`emit_event`; it is a no-op outside a running job. The
scheduler installs a sink for each job through a context variable, so adapter
``run`` signatures stay unchanged.

Every event is written to the ``job_events`` table and appended to
``data/logs/<job>.events.jsonl`` (mode 0600). Streamed text and thinking
deltas are coalesced (about 0.2 s) so a long answer is not one row per token.
Only normalized events are stored, never environment or credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any
from uuid import UUID

from .store import JobStore, now

FLUSH_SECONDS = 0.2
MAX_EVENT_TEXT = 100_000
DELTA_KINDS = ("text", "thinking")

_current: ContextVar[EventSink | None] = ContextVar("agentdesk_event_sink", default=None)


class EventSink:
    def __init__(self, store: JobStore, job_id: UUID | str, events_log: Path | None = None, attempt: int = 0):
        self.store = store
        self.job_id = str(job_id)
        # every event carries the attempt it belongs to (a retry re-runs the same job id)
        self.attempt = attempt
        self.events_log = events_log
        self.seq = store.last_event_seq(self.job_id)
        self.draft = ""
        self.final_text: str | None = None
        self._pending: tuple[str, str] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._fd: int | None = None

    # --- context variable plumbing ---
    def install(self) -> Token:
        return _current.set(self)

    @staticmethod
    def uninstall(token: Token) -> None:
        _current.reset(token)

    @property
    def assembled(self) -> str:
        """Final reply if the stream reported one, else the text streamed so far."""
        return (self.final_text if self.final_text is not None else self.draft).strip()

    # --- emitting ---
    def emit(self, kind: str, **data: Any) -> None:
        if kind in DELTA_KINDS and not data.get("replace"):
            text = str(data.get("text") or "")
            if not text:
                return
            if kind == "text":
                self.draft += text
            if self._pending and self._pending[0] != kind:
                self.flush()
            self._pending = (kind, (self._pending[1] if self._pending else "") + text)
            self._schedule()
            return
        self.flush()
        if kind == "text":
            self.draft = str(data.get("text") or "")
        elif kind == "final":
            self.final_text = str(data.get("text") or "")
        self._write(kind, data)
        if kind == "text":
            self.store.set_draft(self.job_id, self.draft)

    def _schedule(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._timer is None:
            self._timer = loop.call_later(FLUSH_SECONDS, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        with contextlib.suppress(Exception):
            self.flush()

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._pending:
            return
        kind, text = self._pending
        self._pending = None
        self._write(kind, {"text": text[:MAX_EVENT_TEXT]})
        if kind == "text":
            self.store.set_draft(self.job_id, self.draft)

    def _write(self, kind: str, data: dict[str, Any]) -> None:
        self.seq += 1
        data = {"attempt": self.attempt, **data}
        if "text" in data:
            data = {**data, "text": str(data["text"])[:MAX_EVENT_TEXT]}
        self.store.add_event(self.job_id, self.seq, kind, data)
        if self.events_log is None:
            return
        line = json.dumps(
            {**data, "seq": self.seq, "kind": kind, "at": now().isoformat()}, ensure_ascii=False
        )
        with contextlib.suppress(OSError):
            if self._fd is None:
                self._fd = os.open(
                    self.events_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
                )
                os.fchmod(self._fd, 0o600)
            os.write(self._fd, (line + "\n").encode())

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.flush()
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


def current_sink() -> EventSink | None:
    return _current.get()


def emit_event(kind: str, **data: Any) -> None:
    sink = _current.get()
    if sink is not None:
        sink.emit(kind, **data)
