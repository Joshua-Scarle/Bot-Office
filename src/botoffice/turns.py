from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .catalog import default_effort, effective_effort, valid_effort
from .config import Settings
from .domain import Bot, Job, Message, Routine
from .providers.base import ProviderAdapter
from .store import InboxLimit, JobStore, now


class TurnError(Exception):
    pass


class InboxLimitError(TurnError):
    """An inbox or bot-to-bot turn over its hourly cap (Settings.inbox_*_hourly)."""


class Turns:
    def __init__(self, store: JobStore, settings: Settings, providers: dict[str, ProviderAdapter]):
        self.store = store
        self.settings = settings
        self.providers = providers

    def _inbox_caps(self) -> tuple[int, int]:
        return self.settings.inbox_per_sender_hourly, self.settings.inbox_global_hourly

    def _validate(
        self, provider: str, reason: str | None, bot: Bot | None
    ) -> tuple[str, str | None]:
        provider = provider.strip().lower()
        adapter = self.providers.get(provider)
        if not adapter or not adapter.enabled or provider not in self.settings.enabled_providers:
            raise TurnError("provider is disabled or unavailable")
        reason = (reason or "").strip() or (bot.reason if bot else None)
        reason = (reason or "").strip() or None
        return provider, reason

    def _log_claude_use(self, job: Job) -> None:
        reason = " ".join((job.reason or "").split())
        line = f"{now().isoformat()}\t{job.id}\t{reason}\n"
        path = self.settings.data_dir / "claude-use-log.tsv"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)

    def create_job(
        self,
        prompt: str,
        provider: str,
        *,
        bot: Bot | None = None,
        reason: str | None = None,
        source: str = "api",
        routine_id: UUID | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> Job:
        provider, reason = self._validate(provider, reason, bot)
        if model is None and bot is not None and provider == bot.default_provider:
            model = bot.model
        # the job records the effort it was queued with; later bot edits never rewrite it
        if effort is not None and not valid_effort(provider, effort):
            raise TurnError(f"effort {effort!r} is not supported by {provider}")
        if effort is None and bot is not None and provider == bot.default_provider:
            effort = bot.effort
        if effort is not None and not valid_effort(provider, effort):
            effort = None
        effort = effective_effort(provider, model, effort or default_effort(provider))
        job = self.store.create(
            prompt,
            provider,
            bot_id=bot.id if bot else None,
            reason=reason,
            source=source,
            routine_id=routine_id,
            model=model,
            effort=effort,
        )
        if provider == "claude":
            self._log_claude_use(job)
        return job

    def post_agent_message(self, sender: Bot, target: Bot, text: str, origin_id: str | None = None, *,
                           mode: str = "steer", can_steer: Callable[[Job], bool] | None = None,
                           ) -> tuple[Message, Job, dict]:
        """See JobStore.enqueue_agent_message: *mode* "steer" (default) or "queue"."""
        if mode not in ("steer", "queue"):
            raise TurnError("mode must be steer or queue")
        if sender.id == target.id:
            raise TurnError("cannot message the same bot")
        if not text.strip() or len(text) > 10000:
            raise TurnError("message body must contain 1 to 10000 characters")
        self._validate(target.default_provider, None, target)
        since = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        try:
            result = self.store.enqueue_agent_message(sender, target, text, origin_id, since, self._inbox_caps(),
                                                      mode=mode, can_steer=can_steer)
        except InboxLimit as exc:
            raise InboxLimitError(str(exc)) from exc
        except ValueError as exc:
            raise TurnError(str(exc)) from exc
        if target.default_provider == "claude" and result[2].get("delivery") != "steer":
            self._log_claude_use(result[1])  # a steer is part of a turn already logged
        return result

    def post_message(
        self,
        bot: Bot,
        text: str,
        *,
        reason: str | None = None,
        source: str = "chat",
        sender: str | None = None,
        routine_id: UUID | None = None,
        attachments: list[str] | None = None,
        routine_scan: tuple[Routine, datetime, datetime] | None = None,
        notice: str | None = None,
        bridge_hop: tuple[str, int] | None = None,
        prompt: str | None = None,
        hook: Callable[[sqlite3.Connection, Job], None] | None = None,
    ) -> tuple[Message, Job]:
        """*prompt* (default: *text*) is what the bot gets when it differs from the chat line;
        *hook* runs inside the enqueue transaction (JobStore.enqueue_message)."""
        provider, reason = self._validate(bot.default_provider, reason, bot)
        prompt = prompt or (f"(from {sender}) {text}" if sender else text)
        try:
            message, job = self.store.enqueue_message(
                bot, prompt, text, reason, source, sender, routine_id, attachments,
                routine_scan=routine_scan, notice=notice, bridge_hop=bridge_hop, hook=hook,
                inbox_caps=self._inbox_caps() if source == "inbox" else None)
        except InboxLimit as exc:
            raise InboxLimitError(str(exc)) from exc
        if provider == "claude":
            self._log_claude_use(job)
        return message, job

    def edit_message(
        self, bot: Bot, message_id: int, text: str, *, reason: str | None = None,
        attachments: list[str] | None = None,
    ) -> tuple[Message, Job]:
        """Rewind the chat to *message_id* and queue *text* in its place (see JobStore.edit_message)."""
        provider, reason = self._validate(bot.default_provider, reason, bot)
        message, job = self.store.edit_message(bot, message_id, text, reason, attachments or [])
        if provider == "claude":
            self._log_claude_use(job)
        return message, job
