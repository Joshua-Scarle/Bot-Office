from __future__ import annotations

import asyncio
import base64
import contextlib
from contextlib import closing
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import approval_classes, attachments, auto_merge, avatars, baseline, continuation, duet_bridge, git_guard, github_access, grok_bridge, health
from . import bot_desktops, failures, grouping, isolation, needs_you, notify, recall, safefs, team, vnc
from . import policy as policy_mod
from . import sections as sections_mod
from . import slash as slash_mod
from . import rules as action_rules
from . import usage as budgets
from . import remote_access
from . import rules_sync, stall_sweep
from .auth import auth_dependency, bearer
from .catalog import (
    ENGINE_LABELS,
    ENGINES,
    STATIC_MODELS,
    capability_tier,
    choice_label,
    default_effort,
    default_model,
    effort_levels,
    model_error,
    shortlist_models,
    valid_effort,
)
from .config import Settings
from .cron import min_gap
from .desktop import run_desktop_command
from .assignments import (
    BOT_EDIT_OWNER_TASKS,
    BOT_ROUTINE_MIN_GAP,
    CROSS_BOT_MAX_PRIORITY,
    CROSS_BOT_SUBTASK_START,
    DROP_CANCELS_TURNS,
    FORM_TASKS_START,
    MAX_BOT_ROUTINES,
    PRIORITIES,
    TASK_DEFAULT_PRIORITY,
    TASK_WRITES_PER_HOUR,
    AssignmentError,
    _limit,
    choice,
    drop_rule,
    tasks_label,
)
from .domain import (
    MessagePage,
    MessageSearch,
    _check_cron,
    POSITION_LIMIT,
    Assignment,
    AssignmentCreate,
    AssignmentReply,
    AssignmentStart,
    AssignmentUpdate,
    Bot,
    BotCreate,
    BotUpdate,
    DesktopCommand,
    EngineChoice,
    InboxCreate,
    Job,
    JobCreate,
    JobStatus,
    Message,
    MessageCreate,
    MessageEdit,
    MessagePosted,
    ModelOption,
    ProviderInfo,
    ReadMark,
    Routine,
    RoutineCreate,
    RoutineUpdate,
    Task,
    TaskCreate,
    TaskNote,
    TaskReorder,
    TaskUpdate,
    TextBody,
    Wakeup,
)
from .provider_status import ProviderProbeError, ProviderStatusCache
from .providers.antigravity import AntigravityAdapter
from .providers.base import ProviderAdapter
from .providers.claude import ClaudeAdapter
from .providers.codex import CodexAdapter
from .providers.cursor import CursorAdapter
from .providers.grok import GrokAdapter
from .providers.opencode import OpenCodeAdapter
from .scheduler import Scheduler, on_terminate
from .store import EditError, JobStore
from .turns import TurnError
from .watchers import BODY_LIMIT as WATCHER_BODY_LIMIT
from .watchers import WatcherError
from .voice import SpeakableRequest, SpeakRequest, VoiceError, VoicePrefs, VoiceService
from .voice import VoicePrefsUpdate as VoicePrefsBody
from .voice.text import chunks as speech_chunks
from .voice.wav import WavError, parse_pcm16_mono


class AvatarCharacter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shape: str = Field(max_length=16)
    color: str = Field(max_length=16)


class AvatarGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: str | None = Field(default=None, max_length=64)


class SectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)


class SectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    collapsed: bool | None = None


class SectionOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(max_length=200)


class HiddenMembers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_ids: list[UUID] = Field(max_length=500)


class BotSectionMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str | None = None
    index: int = Field(default=0, ge=0, le=100000)


class AutoContinueBody(BaseModel):
    """0.11.10: one section for chat auto-continue and the task loop (continuation.py)."""
    enabled: bool | None = None
    max: int | None = None
    no_progress: int | None = None
    consecutive: int | None = None
    # a whole number, or null / "unlimited" for no cap
    per_bot_day: int | Literal["unlimited"] | None = None
    per_hour: int | Literal["unlimited"] | None = None
    evidence: bool | None = None
    evidence_min: int | None = None
    hold_hours: int | None = None


class ApprovalClassCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["shell", "tool"]
    pattern: str = Field(min_length=1, max_length=500)


class ApprovalsBody(BaseModel):
    # class key -> on/off
    enabled: dict[str, bool] | None = Field(default=None, max_length=100)
    add: list[ApprovalClassCreate] | None = Field(default=None, max_length=10)
    remove: list[str] | None = Field(default=None, max_length=100)
    # minutes an approval in your own chat waits for an answer; 0 = until answered
    timeout_min: int | None = Field(default=None, ge=0)


class NotificationsBody(BaseModel):
    """Email classes (0.12.13): each toggle is optional; missing keys keep their value."""
    model_config = ConfigDict(extra="forbid")
    needs_you: bool | None = None
    failed: bool | None = None
    outage: bool | None = None
    backup: bool | None = None
    done: bool | None = None


class SettingsBody(BaseModel):
    auto_continue: AutoContinueBody | None = None
    approvals: ApprovalsBody | None = None
    notifications: NotificationsBody | None = None


class AgentMessageCreate(BaseModel):
    to: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    # 0.11.10: "steer" reaches the receiver's running turn now (else starts or queues a turn);
    # "queue" waits for its next turn and its reply comes back to the sender
    mode: Literal["steer", "queue"] = "steer"
    wait: bool = False
    timeout: float = Field(default=30, gt=0, le=120)


class TeamAsk(AgentMessageCreate):
    timeout_s: int = Field(default=team.ASK_DEFAULT_S, ge=5, le=team.ASK_MAX_S)


class AgentAssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=20_000)
    done_when: str = Field(default="", max_length=4000)
    for_bot: str | None = Field(default=None, max_length=200)
    parent_id: str | None = Field(default=None, max_length=64)
    budget_turns: int = Field(default=20, ge=1, le=200)
    budget_minutes: int = Field(default=120, ge=1, le=24 * 60)
    start: bool = True
    # None = BOT_OFFICE_TASK_DEFAULT_PRIORITY (normal)
    priority: Literal["urgent", "high", "normal", "low"] | None = None


TASK_STAGES = {"not_started", "in_progress", "completed", "dropped"}


class AgentAdminBotUpdate(BaseModel):
    """0.12.26: what an app admin may change on a bot. Anything else (access, sudo, scheduled-run
    profile, budgets, team visibility, admin) is refused by the model (extra fields are an error)."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=100)
    effort: str | None = Field(default=None, max_length=16)
    job_timeout: int | None = Field(default=None, ge=60, le=3600)
    notify: bool | None = None
    digest: bool | None = None
    bot_steer: bool | None = None
    chat_only: bool | None = None


class AgentTaskCreate(AgentAssignmentCreate):
    # 0.11.4: lands open unless the bot asks to start one on its own list (or a child it delegates)
    start: bool = False
    position: float | None = Field(default=None, allow_inf_nan=False, ge=-POSITION_LIMIT, le=POSITION_LIMIT)


class AgentTaskEdit(BaseModel):
    # other fields are kept so Assignments.agent_edit refuses them (400 unknown, 403 not
    # allowed) instead of a partial success
    model_config = ConfigDict(extra="allow")
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=20_000)
    done_when: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    position: float | None = Field(default=None, allow_inf_nan=False, ge=-POSITION_LIMIT, le=POSITION_LIMIT)
    priority: Literal["urgent", "high", "normal", "low"] | None = None


class AgentTaskDrop(BaseModel):
    note: str = Field(default="", max_length=1000)


class AgentTaskReorder(BaseModel):
    order: list[str] = Field(min_length=1, max_length=500)


class AgentStatus(BaseModel):
    state: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=1000)
    # how the work was verified (tests, a check, a command's result, a file)
    evidence: str = Field(default="", max_length=1000)


class AgentText(BaseModel):
    text: str = Field(max_length=20_000)


class GateRequest(BaseModel):
    provider: str = Field(default="", max_length=32)
    event: dict = Field(default_factory=dict)


# a hook event is at most what bin/bot-office-gate forwards (1 MB of stdin) plus its envelope;
# larger bodies are refused before parsing (0.11.0 review Sec 4)
GATE_BODY_LIMIT = 1024 * 1024 + 4096
SWITCH_OPTIONS_TTL = 60.0  # #10: seconds a parked job's [Retry on <provider>] choices are reused


async def _read_capped(request: Request, limit: int, what: str) -> bytes:
    """The request body, refused with 413 above *limit* bytes (400 for a bad Content-Length)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad Content-Length") from exc
    if declared > limit:
        raise HTTPException(status_code=413, detail=f"{what} over {limit // 1024} kB")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail=f"{what} over {limit // 1024} kB")
    return bytes(body)


class RuleCreate(BaseModel):
    bot_id: UUID | None = None
    effect: str = Field(max_length=10)
    kind: str = Field(max_length=10)
    pattern: str = Field(min_length=1, max_length=500)
    scope: str = Field(default="all", max_length=12)
    note: str = Field(default="", max_length=500)


class WatcherCreate(BaseModel):
    kind: str = Field(max_length=12)
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    config: dict = Field(default_factory=dict)
    debounce_s: int = 60
    max_per_hour: int = 6
    enabled: bool = True


class WatcherUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    prompt: str | None = Field(default=None, max_length=4000)
    config: dict | None = None
    debounce_s: int | None = None
    max_per_hour: int | None = None
    enabled: bool | None = None


class BudgetAllow(BaseModel):
    turns: int = Field(default=10, ge=0, le=1000)
    minutes: int = Field(default=30, ge=0, le=1440)
    tokens: int = Field(default=0, ge=0, le=10**9)


class NeedsYouDismiss(BaseModel):
    assignment_id: UUID


class NeedsYouClear(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class SwitchProvider(BaseModel):
    """#10 "Retry on <provider>" for a job parked on a usage limit (owner only)."""
    provider: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=1000)


class AgentWakeup(BaseModel):
    assignment_id: str | None = Field(default=None, max_length=64)
    due_at: datetime | None = None
    in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 31)
    on_assignment: str | None = Field(default=None, max_length=64)
    note: str = Field(default="", max_length=2000)


class AgentRoutine(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    for_bot: str | None = Field(default=None, max_length=200)  # 0.12.26: app admin only
    cron: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20_000)
    enabled: bool = True

    _cron = field_validator("cron")(_check_cron)


class AgentRoutineUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    cron: str | None = Field(default=None, min_length=1, max_length=100)
    prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    enabled: bool | None = None

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return None if value is None else _check_cron(value)


class SlashRun(BaseModel):
    """A picked slash command (0.11.14). *confirm* is required for the destructive ones (new session)."""
    name: str = Field(min_length=1, max_length=80)
    args: str = Field(default="", max_length=slash_mod.MAX_ARGS)
    confirm: bool = False


class BridgeSend(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=1, max_length=10000)
    thread_id: str | None = Field(default=None, max_length=200)
    from_bot: str | None = Field(default=None, alias="from", max_length=200)


class DuetSend(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(min_length=1, max_length=60000)
    to: str = Field(default="main", max_length=200)
    kind: str = Field(default="fyi", max_length=20)
    thread: str | None = Field(default=None, max_length=200)
    reply_to: str | None = Field(default=None, max_length=200)
    from_bot: str | None = Field(default=None, alias="from", max_length=200)
    msg_id: str | None = Field(default=None, alias="id", max_length=64)


class BridgeInbox(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to: str = Field(min_length=1, max_length=200)
    sender: str = Field(alias="from", min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=10000)
    thread_id: str | None = Field(default=None, max_length=200)
    hop: int | None = None
    # 0.12.39: a priority message always wakes the recipient (claimed first, no hourly inbox cap or budget hold)
    priority: bool = False


class StallSweepConfig(BaseModel):
    """0.12.39 stall sweep: bot (name or id, or "*" = default) -> its section lead; bots never swept."""
    leads: dict[str, str] | None = None
    exclude: list[str] | None = None


CHAT_PAGE = Path(__file__).parent / "static" / "bot-office.html"
MAX_ICON_BYTES = avatars.MAX_UPLOAD_BYTES
TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.NEEDS_LOGIN, JobStatus.INCOMPLETE}


class TextGZipMiddleware(GZipMiddleware):
    """GZip for the page and JSON; attachment bodies and icons are images or files, mostly already compressed."""

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and ("/attachments/" in path or path.endswith("/icon")):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


EVENT_POLL_SECONDS = 0.2
EVENT_KEEPALIVE_SECONDS = 15.0
EVENT_GRACE_SECONDS = 3.0
SNAPSHOT_LIMIT = 2000
EDIT_CANCEL_WAIT_SECONDS = 15.0
EDIT_CANCEL_REASON = "cancelled to edit your message"
STT_MAX_BYTES = 4 * 1024 * 1024  # ~125 s of 16 kHz 16-bit mono
STT_MIN_SECONDS, STT_MAX_SECONDS = 0.3, 120.0
STT_PER_MINUTE = 20
TTS_PER_MINUTE = 120
SPEAKABLE_PER_MINUTE = 60
STT_READ_SECONDS = 30.0  # a slow upload never holds a connection open longer
# synthesized audio is served as an opaque download, never rendered as a document
AUDIO_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Content-Disposition": 'inline; filename="speech.wav"',
}
VOICE_REAP_SECONDS = 30.0
ICON_FILE = "icon"
_sniff_image = attachments.sniff_image


def _page_with_csp() -> tuple[str, str]:
    """Return the page and a CSP that allows only its own inline script and style."""
    html = CHAT_PAGE.read_text(encoding="utf-8")

    def hashes(tag: str) -> str:
        blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", html, flags=re.DOTALL)
        return " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(b.encode()).digest()).decode() + "'"
            for b in blocks
        )

    csp = (
        "default-src 'none'; "
        f"script-src 'self' {hashes('script')}; style-src {hashes('style')}; "
        "img-src blob: data:; media-src blob:; frame-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    return html, csp


def _default_providers(settings: Settings) -> dict[str, ProviderAdapter]:
    return {
        "opencode": OpenCodeAdapter(enabled="opencode" in settings.enabled_providers),
        "codex": CodexAdapter(enabled="codex" in settings.enabled_providers),
        "claude": ClaudeAdapter(enabled="claude" in settings.enabled_providers),
        "cursor": CursorAdapter(enabled="cursor" in settings.enabled_providers),
        "antigravity": AntigravityAdapter(enabled="antigravity" in settings.enabled_providers),
        "grok": GrokAdapter(),
    }


def _tail(path: str | None, limit: int = 8192) -> str:
    if not path:
        return ""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return stream.read().decode(errors="replace")
    except OSError:
        return ""



class AgentDesktopRequest(BaseModel):
    """POST /v1/agent/desktop: empty for the bot's own desktop, or the turn's leased pool slot."""
    slot: str | None = Field(default=None, max_length=16)

def create_app(
    settings: Settings | None = None,
    providers: dict[str, ProviderAdapter] | None = None,
    store: JobStore | None = None,
    voice: VoiceService | None = None,
) -> FastAPI:
    if settings is None:
        # the real server: pick the layout (launcher + private dir, or the old single-user one)
        settings = Settings.from_env()
        isolation.configure(settings)
        if settings.private_dir and not settings.private_dir.is_dir():
            # never fall back to the shared tree for the DB and the token (Sec 6)
            raise RuntimeError(f"BOT_OFFICE_PRIVATE_DIR {settings.private_dir} is missing; "
                               "run deploy/install-isolation.sh --apply or unset it")
    store = store or JobStore(settings.db_path, initialize=False)
    providers = providers or _default_providers(settings)
    scheduler = Scheduler(store, settings, providers)
    slash_catalog = slash_mod.SlashCatalog(providers, settings.data_dir)
    voice = voice or VoiceService(settings)

    async def reap_voice() -> None:
        while True:
            await asyncio.sleep(VOICE_REAP_SECONDS)
            with contextlib.suppress(Exception):
                await voice.reap_idle()

    notifier = notify.Notifier(store)

    def _duet_steer(bot: Bot, text: str, hook) -> bool:  # noqa: ANN001
        # the chat endpoint's rule for a message that reaches a busy bot (see _post_owner_message_ex);
        # *hook* writes the bridge's receipt in the steer's own transaction
        _, steer_reason = scheduler.turns._validate(bot.default_provider, None, bot)
        result, _note = store.post_steer_explained(bot.id, text, [], steer_reason, unsteerable=_steer_blocked(bot),
                                                   hook=hook)
        return bool(result)  # attached to the turn, or (engine cannot take input mid-run) queued as its own turn

    duet = duet_bridge.DuetBridge(store, settings, turns=scheduler.turns, steer=_duet_steer)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        await asyncio.to_thread(attachments.prune, settings.data_dir, store.attachment_references())
        await scheduler.start()
        pusher = asyncio.create_task(notifier.run(), name="botoffice-notify")
        warm = asyncio.create_task(provider_status.warm(), name="botoffice-provider-warm")
        on_terminate(scheduler.begin_shutdown)
        on_terminate(duet.signal_stop)  # no new GitHub request or delivery once shutdown starts
        duet.start()
        reaper = asyncio.create_task(reap_voice(), name="botoffice-voice-reaper")
        desk_reaper = asyncio.create_task(bot_desks.run_reaper(), name="botoffice-desktop-reaper")
        try:
            yield
        finally:
            # before the scheduler goes: the join covers one in-flight HTTP request (its timeout)
            if not await asyncio.to_thread(duet.stop):
                # still inside one request past its deadline; the stop flag keeps it from delivering anything
                duet_bridge.log.warning("duet bridge worker did not exit in time")
        pusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pusher
        warm.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warm
        await provider_status.close()
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        desk_reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await desk_reaper
        await voice.close()
        await scheduler.stop()

    app = FastAPI(
        title="Bot-Office",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # 0.12.1: the page (217 kB) and long chats (hundreds of kB) shrink a lot over a tunnel;
    # Starlette leaves text/event-stream (the live reply) uncompressed
    app.add_middleware(TextGZipMiddleware, minimum_size=1024, compresslevel=5)
    # 0.12.23: added last, so it is the OUTERMOST middleware (it runs before gzip). It checks the Cloudflare
    # Access JWT on requests that came through the tunnel and leaves loopback requests untouched.
    app.add_middleware(remote_access.RemoteAccessMiddleware, settings_or_loader=settings)
    app.state.settings = settings
    app.state.store = store
    app.state.providers = providers
    app.state.scheduler = scheduler
    app.state.voice = voice
    app.mount("/static/vendor/novnc", StaticFiles(directory=CHAT_PAGE.parent / "vendor" / "novnc"), name="novnc")
    tickets = vnc.Tickets()
    # remote-desktop: each bot's own desktop, started on demand and stopped when idle
    bot_desks = bot_desktops.DesktopManager(settings, busy_bots=store.running_bot_ids)
    app.state.bot_desktops = bot_desks
    proxy_counts: dict[str, int] = {}
    attachment_uploads: dict[UUID, list[float]] = {}
    attachment_locks: dict[UUID, asyncio.Lock] = {}
    attachment_global_lock = asyncio.Lock()
    upload_slots = asyncio.Semaphore(4)

    async def attachment_io(fn, *args):
        # Keep the lock held until a worker finishes even if its caller is cancelled.
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def attachment_lock(bot_id: UUID) -> asyncio.Lock:
        return attachment_locks.setdefault(bot_id, asyncio.Lock())
    auth = auth_dependency(settings)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # no auth on purpose (the loopback watchdog pusher calls it); the body holds only booleans,
        # numbers and short enums. The blocking work (database, disk, status file) runs in a bounded
        # worker thread, never on the event loop.
        body, code = await health.snapshot(app.state)
        return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})

    memory = scheduler.memory
    assignments = scheduler.assignments

    def _assignment_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AssignmentError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    def _cancel_jobs(job_ids: list[str]) -> None:
        for job_id in job_ids:
            job = store.get(job_id)
            if job:
                scheduler.cancel(job, "its task was stopped or dropped")

    def _steer_blocked(bot: Bot):
        """For store.post_steer_explained: why this engine cannot take a message while *job* runs."""
        def note(job: Job) -> str | None:
            if job.slash:
                return "A slash command turn cannot take a message while it runs, so yours runs as the next turn."
            adapter = providers.get(job.provider)
            check = getattr(adapter, "steer_unavailable_note", None)
            return check(job.model_copy(update={"cli_access": bot.cli_access})) if check else None
        return note

    def _post_owner_message_ex(bot: Bot, text: str, *, reason: str | None = None,
                               attachments: list[str] | None = None,
                               queue: bool = False) -> tuple[Message, Job, str, str | None]:
        """An owner chat post: steered into the bot's running or queued turn (see JobStore.post_steer)
        unless the caller asks for a separate queued turn (mode "queue"). Returns (message, job, delivery,
        note): delivery "steer" (attached to the working turn), "queue" (a new turn behind other work, the
        note says why: the owner asked, the engine cannot take input mid-run, the bot is answering another
        bot) or "start" (idle bot). Bot-to-bot inbox messages, routines, wakeups and assignment steps are
        not owner posts and never come through here."""
        note = None
        if not queue:
            _, steer_reason = scheduler.turns._validate(bot.default_provider, reason, bot)
            result, note = store.post_steer_explained(bot.id, text, attachments or [], steer_reason,
                                                      unsteerable=_steer_blocked(bot))
            if result:
                message, job = result
                if message.steer_status == "queued":  # the engine cannot take it mid-run: said at once
                    return message, job, "queue", message.steer_note
                return message, job, "steer", None
        message, job = scheduler.turns.post_message(bot, text, reason=reason, attachments=attachments)
        others = [item for item in store.pending_jobs(bot.id) if str(item.id) != str(job.id)]
        if not others:
            return message, job, "start", None
        if queue:
            note = "Queued as the next turn, as you chose."
        return message, job, "queue", note or "Queued behind the bot's other turns; it runs when they finish."

    def _post_owner_message(bot: Bot, text: str, *, reason: str | None = None,
                            attachments: list[str] | None = None, queue: bool = False) -> tuple[Message, Job]:
        message, job, _, _ = _post_owner_message_ex(bot, text, reason=reason, attachments=attachments, queue=queue)
        return message, job

    def _bot(bot_id: UUID | str) -> Bot:
        bot = store.get_bot(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="bot not found")
        return bot

    @app.get("/v1/remote/whoami")
    async def remote_whoami() -> dict:
        # no auth dependency: the page asks this before it has any credential. The middleware already
        # refused every remote request without a verified identity, so this only reports it.
        who = remote_access.current_remote_identity.get()
        if who is None:
            return {"remote": False}
        return {"remote": True, "kind": who.kind, "role": who.role, "name": who.display_name}

    @app.get("/", response_class=HTMLResponse)
    async def chat_page() -> HTMLResponse:
        html, csp = _page_with_csp()
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": csp,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
            },
        )

    @app.post("/v1/jobs", response_model=Job, status_code=201, dependencies=[Depends(auth)])
    async def create_job(body: JobCreate) -> Job:
        bot = _bot(body.bot_id) if body.bot_id else None
        provider = body.provider
        if bot and "provider" not in body.model_fields_set:
            provider = bot.default_provider
        try:
            return scheduler.turns.create_job(
                body.prompt,
                provider,
                bot=bot,
                reason=body.reason,
                source="api",
                model=body.model,
                effort=body.effort,
            )
        except TurnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(
        "/v1/jobs",
        response_model=list[Job],
        response_model_exclude={"__all__": {"reply_text", "draft_text"}},
        dependencies=[Depends(auth)],
    )
    async def list_jobs() -> list[Job]:
        """All jobs. The full audit reply is on ``GET /v1/jobs/{id}`` (``reply_text``)."""
        return store.list()

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
    async def get_job(job_id: UUID) -> dict[str, Any]:
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job": job, "log_tail": _tail(job.log_path)}

    def _frame(event: dict[str, Any]) -> str:
        data = json.dumps(event, ensure_ascii=False)
        return f"id: {event['seq']}\nevent: {event['kind']}\ndata: {data}\n\n"

    @app.get("/v1/jobs/{job_id}/events", dependencies=[Depends(auth)])
    async def job_events(job_id: UUID, request: Request, after: int = Query(default=0, ge=0)):
        """Stream events of one job (chat, inbox, routine or API).

        ``Accept: text/event-stream`` gives SSE (``id``/``event``/``data`` frames; use
        ``fetch`` with the bearer header, never the query string). Otherwise a JSON
        snapshot. Resume after a reload with ``?after=<seq>`` or ``Last-Event-ID``.
        """
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        last_id = (request.headers.get("last-event-id") or "").strip()
        if last_id.isdigit():
            after = int(last_id)
        if "text/event-stream" not in request.headers.get("accept", ""):
            events = store.list_events(job_id, after, SNAPSHOT_LIMIT)
            current = store.get(job_id) or job
            finished = current.status in TERMINAL and len(events) < SNAPSHOT_LIMIT
            return {
                "job_id": str(job_id),
                "status": current.status,
                "events": events,
                "next": events[-1]["seq"] if events else after,
                "done": finished,
                "draft": current.draft_text or "",
                "reply_text": current.reply_text if finished else None,
            }

        async def stream():
            cursor = after
            idle = 0.0
            terminal_wait = 0.0
            while True:
                events = store.list_events(job_id, cursor, 500)
                for event in events:
                    cursor = event["seq"]
                    yield _frame(event)
                    if event["kind"] == "done":
                        # a retried job re-runs under the same id: only the current attempt's
                        # done ends the stream (events before 0.9.0 carry no attempt)
                        current = store.get(job_id)
                        if current is None or event.get("attempt", current.execution) == current.execution:
                            return
                if events:
                    idle = terminal_wait = 0.0
                    continue
                current = store.get(job_id)
                if current is None:
                    return
                if current.status in TERMINAL:
                    # the scheduler writes final+done just after marking the job finished;
                    # a job that never streamed (older job) closes at once
                    if store.last_event_seq(job_id) == 0 or terminal_wait >= EVENT_GRACE_SECONDS:
                        tail = {"seq": cursor, "kind": "done", "status": current.status.value}
                        yield f"event: done\ndata: {json.dumps(tail)}\n\n"
                        return
                    terminal_wait += EVENT_POLL_SECONDS
                if await request.is_disconnected():
                    return
                idle += EVENT_POLL_SECONDS
                if idle >= EVENT_KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": keep-alive\n\n"
                await asyncio.sleep(EVENT_POLL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/jobs/{job_id}/cancel", response_model=Job, dependencies=[Depends(auth)])
    async def cancel_job(job_id: UUID) -> Job:
        before = store.get(job_id)
        if not before:
            raise HTTPException(status_code=404, detail="job not found")
        if before.status == JobStatus.RUNNING:
            scheduler.cancel(before, "cancelled by you")
            job = store.get(job_id)
        else:
            job = store.cancel(job_id, reason="cancelled by you")
        assert job is not None
        return job

    @app.post("/v1/jobs/{job_id}/retry", response_model=Job, dependencies=[Depends(auth)])
    async def retry_job(job_id: UUID) -> Job:
        """Re-queue a failed or incomplete job now, as the same job (same chat turn)."""
        try:
            return store.retry_now(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/slots", dependencies=[Depends(auth)])
    async def get_slots() -> list[dict[str, object]]:
        return scheduler.slots()

    async def _provider_info(name: str) -> ProviderInfo:
        adapter = providers.get(name)
        enabled = bool(adapter and adapter.enabled and name in settings.enabled_providers)
        if not enabled:
            login = "disabled"
        else:
            try:
                login = await adapter.login_status()
                if login not in {"ok", "needs_login", "disabled", "error"}:
                    login = "error"
            except Exception:
                login = "error"
        return ProviderInfo(name=name, enabled=enabled, login_status=login)

    async def _probe_providers() -> list[ProviderInfo]:
        return list(await asyncio.gather(*(_provider_info(name) for name in (*ENGINES, "grok"))))

    provider_status = ProviderStatusCache(_probe_providers)
    app.state.provider_status = provider_status
    # a job that ends "needs sign-in" means the cached "signed in" is wrong: look again at the next request
    scheduler.on_needs_login = provider_status.invalidate

    @app.get("/v1/providers", response_model=list[ProviderInfo], dependencies=[Depends(auth)])
    async def get_providers(refresh: bool = False) -> list[ProviderInfo]:
        """Engine sign-in status, from a cache (60 s, renewed in the background); ``?refresh=1`` probes now."""
        try:
            return await provider_status.get(refresh=refresh)
        except ProviderProbeError as exc:
            raise HTTPException(status_code=503, detail="could not check the engines just now; try again") from exc

    @app.get("/v1/model-choices", response_model=list[EngineChoice], dependencies=[Depends(auth)])
    async def get_model_choices(refresh: bool = False) -> list[EngineChoice]:
        """Engines that are enabled and signed in, each with selectable model ids."""

        async def _choice(info: ProviderInfo) -> EngineChoice | None:
            if not info.enabled or info.login_status != "ok":
                return None
            adapter = providers[info.name]
            models: list[tuple[str, str]] = []
            lister = getattr(adapter, "list_models", None)
            if lister is not None:
                try:
                    models = list(await (lister(refresh=True) if refresh else lister()))
                except Exception:
                    models = []
            if not models:
                models = list(STATIC_MODELS.get(info.name, ()))
            models = shortlist_models(info.name, models)
            return EngineChoice(
                engine=info.name,
                label=ENGINE_LABELS[info.name],
                default_model=default_model(info.name),
                models=[
                    ModelOption(
                        id=mid,
                        label=label,
                        display=choice_label(info.name, mid, label),
                        capability_tier=capability_tier(mid),
                    )
                    for mid, label in models
                ],
                efforts=list(effort_levels(info.name)),
                default_effort=default_effort(info.name),
            )

        infos = await asyncio.gather(*(_provider_info(name) for name in ENGINES))
        listed = await asyncio.gather(*(_choice(info) for info in infos))
        return [choice for choice in listed if choice is not None]

    @app.post("/v1/slots/{slot_name}/exec", dependencies=[Depends(auth)])
    async def exec_in_desktop(slot_name: str, body: DesktopCommand) -> dict[str, str | int]:
        if slot_name not in settings.desktops:
            raise HTTPException(status_code=404, detail="unknown slot")
        slot = slot_name
        if any(not arg or "\x00" in arg for arg in body.command):
            raise HTTPException(status_code=422, detail="command arguments must be non-empty")
        try:
            return await run_desktop_command(slot, body.command, settings.desktop_timeout)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="podman executable not found") from exc

    def _check_effort(engine: str, effort: str | None) -> None:
        if effort is None or valid_effort(engine, effort):
            return
        levels = effort_levels(engine)
        detail = (
            f"effort must be one of: {', '.join(levels)}"
            if levels
            else f"engine {engine} has no effort setting"
        )
        raise HTTPException(status_code=400, detail=detail)

    def _check_model(engine: str, model: str | None) -> None:
        if error := model_error(engine, model):
            raise HTTPException(status_code=400, detail=error)

    def _make_bot(body: BotCreate, creator_bot_id=None) -> Bot:
        _check_model(body.default_provider, body.model)
        _check_effort(body.default_provider, body.effort)
        try:
            bot = store.create_bot(
                body.name.strip(),
                body.instructions,
                body.default_provider,
                body.reason,
                model=body.model,
                icon=body.icon or "",
                description=(body.description or "").strip(),
                effort=body.effort,
                cli_access=body.cli_access,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if body.chat_only:
            bot = store.update_bot(bot.id, chat_only=1)
        memory.write_bot(bot.id, "")
        if creator_bot_id is None and body.created_by:
            creator = store.find_bot(body.created_by)
            creator_bot_id = creator.id if creator else None
        grouping.place_new_bot(store, bot, body.group, creator_bot_id)
        return _with_steering([bot])[0]

    @app.post("/v1/bots", response_model=Bot, status_code=201, dependencies=[Depends(auth)])
    async def create_bot(body: BotCreate) -> Bot:
        return _make_bot(body)

    @app.post("/v1/bots/{bot_id}/retire", dependencies=[Depends(auth)])
    async def retire_bot(bot_id: UUID) -> dict:
        """0.12.20: move the bot to the "Retired" section (nothing is deleted)."""
        _bot(bot_id)
        try:
            return {"section_id": grouping.retire_bot(store, bot_id)}
        except sections_mod.SectionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    def _with_steering(bots: list[Bot]) -> list[Bot]:
        """Set each bot's ``steering``: how a message sent while it works reaches the CLI."""
        for bot in bots:
            check = getattr(providers.get(bot.default_provider), "steering", None)
            if check:
                bot.steering = check(SimpleNamespace(bot_id=bot.id, cli_access=bot.cli_access))
        return bots

    def _with_status(bots: list[Bot]) -> list[Bot]:
        """Set each bot's ``status`` (offline/working/queued/blocked/waiting/error, else idle)."""
        with closing(store._connect()) as db:
            question_bots = {row[0] for row in db.execute("SELECT DISTINCT bot_id FROM questions WHERE status='open'")}
            if len(bots) == 1:  # one bot (GET/PATCH): read only its latest job, not every job
                row = db.execute("SELECT * FROM jobs WHERE bot_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                                 (str(bots[0].id),)).fetchone()
                latest = {bots[0].id: store._job(row)} if row is not None else {}
            else:
                latest = {job.bot_id: job for job in store.list() if job.bot_id}
        waiting = assignments.bot_states()
        _with_steering(bots)
        for bot in bots:
            adapter = providers.get(bot.default_provider)
            if not adapter or not adapter.enabled or bot.default_provider not in settings.enabled_providers:
                bot.status = "offline"
            elif bot.activity:
                bot.status = "working" if bot.activity == "running" else "queued"
            elif state := waiting.get(str(bot.id)):
                bot.status = state  # from its assignments: blocked or waiting
            elif str(bot.id) in question_bots:
                bot.status = "waiting"
            elif (job := latest.get(bot.id)) and job.status in {JobStatus.FAILED, JobStatus.NEEDS_LOGIN}:
                bot.status = "error"
        return bots

    @app.get("/v1/bots", response_model=list[Bot], dependencies=[Depends(auth)])
    async def list_bots() -> list[Bot]:
        return _with_status(store.list_bots())

    @app.post("/v1/bots/{bot_id}/read", response_model=Bot, dependencies=[Depends(auth)])
    async def mark_read(bot_id: UUID, body: ReadMark | None = None) -> Bot:
        """Mark the bot's messages read up to ``up_to``; a stale request is ignored and the
        current state is returned so the client can resync its revision."""
        _bot(bot_id)
        body = body or ReadMark()
        store.mark_read(bot_id, body.up_to, body.revision, body.explicit)
        return store.bot_unread(bot_id)

    @app.post("/v1/bots/{bot_id}/unread", response_model=Bot, dependencies=[Depends(auth)])
    async def mark_unread(bot_id: UUID) -> Bot:
        _bot(bot_id)
        store.mark_unread(bot_id)
        return store.bot_unread(bot_id)

    @app.get("/v1/bots/{bot_id}", response_model=Bot, dependencies=[Depends(auth)])
    async def get_bot(bot_id: UUID) -> Bot:
        _bot(bot_id)
        return _with_status([store.bot_unread(bot_id)])[0]  # unread fields so a client can resync, and the live status

    @app.patch("/v1/bots/{bot_id}", response_model=Bot, dependencies=[Depends(auth)])
    async def update_bot(bot_id: UUID, body: BotUpdate) -> Bot:
        """Edit identity (name, icon, description, instructions), model or effort.

        Changing the engine/model/effort never touches id, memory, history, routines or
        past jobs (a job keeps the effort it was queued with).
        """
        bot = _bot(bot_id)
        fields = body.model_dump(exclude_unset=True)
        if fields.get("name"):
            fields["name"] = fields["name"].strip()
        if "description" in fields:
            fields["description"] = (fields["description"] or "").strip()
        for key in ("name", "instructions", "default_provider", "model", "icon", "cli_access", "notify", "allow_sudo",
                    "scheduled_runs", "daily_budget", "digest", "bot_steer", "chat_only", "team_read", "team_private",
                    "app_admin", "sudo_review"):
            if key in fields and fields[key] is None:
                del fields[key]
        if "daily_budget" in fields:
            try:
                fields["daily_budget_json"] = json.dumps(budgets.check_budget(fields.pop("daily_budget")))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if "digest" in fields:
            fields["digest"] = int(fields["digest"])
        if "bot_steer" in fields:
            fields["bot_steer"] = int(fields["bot_steer"])
        if "chat_only" in fields:
            fields["chat_only"] = int(fields["chat_only"])
        if "team_private" in fields:
            fields["team_private"] = int(fields["team_private"])
        if "app_admin" in fields:
            fields["app_admin"] = int(fields["app_admin"])
        if "sudo_review" in fields:
            fields["sudo_review"] = int(fields["sudo_review"])
        if fields.get("job_timeout") is not None and fields["job_timeout"] > settings.max_turn_timeout:
            raise HTTPException(status_code=422, detail=f"job_timeout must be at most {settings.max_turn_timeout} seconds")
        if "notify" in fields:
            fields["notify"] = int(fields["notify"])
        if "allow_sudo" in fields:
            # Joshua only (master token): this bot's CLI then runs without no_new_privs and can
            # read the controller's secrets through sudo
            fields["allow_sudo"] = int(fields["allow_sudo"])
        engine = fields.get("default_provider", bot.default_provider)
        if engine != bot.default_provider and "model" not in fields:
            # a model id belongs to one engine; pick the new engine's default
            fields["model"] = default_model(engine)
        if "model" in fields or engine != bot.default_provider:
            _check_model(engine, fields.get("model", bot.model))
        if fields.get("effort") is None and engine != "opencode":
            fields.pop("effort", None)
        _check_effort(engine, fields.get("effort"))
        if engine != bot.default_provider and "effort" not in fields:
            # keep the level if the new engine accepts it, else that engine's default (or none)
            keep = bot.effort if bot.effort and valid_effort(engine, bot.effort) else None
            fields["effort"] = keep or default_effort(engine)
        try:
            updated = store.update_bot(bot_id, **fields)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        assert updated is not None
        return _with_status([store.bot_unread(bot_id) or updated])[0]

    def _reset_session(bot: Bot) -> dict[str, list[str]]:
        if any(j.bot_id == bot.id and j.status in {JobStatus.QUEUED, JobStatus.RUNNING} for j in store.list()):
            raise HTTPException(status_code=409, detail="bot is busy; reset after its current job")
        bot_dir = memory.bot_dir(bot.id)
        reset = [name for name, adapter in providers.items()
                 if hasattr(adapter, "reset_session") and adapter.reset_session(bot_dir)]
        return {"reset": reset}

    @app.post("/v1/bots/{bot_id}/session/reset", dependencies=[Depends(auth)])
    async def reset_bot_session(bot_id: UUID) -> dict[str, list[str]]:
        """Forget the bot's saved provider sessions; its next turn starts one fresh session.

        Chat history, memory.md and WORKING.md are kept (the fresh session is seeded with
        WORKING.md and the last few messages). Refused while the bot has a queued or running job.
        """
        return _reset_session(_bot(bot_id))

    @app.get("/v1/bots/{bot_id}/slash-commands", dependencies=[Depends(auth)])
    async def list_slash_commands(bot_id: UUID, refresh: bool = False) -> dict[str, Any]:
        """The slash commands of this bot's engine (0.11.14), with what each does here (see slash.py)."""
        bot = _bot(bot_id)
        data = await slash_catalog.commands(bot.default_provider, memory.bot_dir(bot.id), refresh=refresh)
        data["commands"] = [{k: v for k, v in item.items() if k != "file"} for item in data["commands"]]
        return data

    @app.post("/v1/bots/{bot_id}/slash", dependencies=[Depends(auth)])
    async def run_slash_command(bot_id: UUID, body: SlashRun) -> dict[str, Any]:
        """Run a picked slash command: through the CLI (a queued turn), as a Bot Office control, or refuse it."""
        bot = _bot(bot_id)
        name = body.name.strip().lstrip("/")
        if slash_mod.is_auth(name):
            raise HTTPException(status_code=403, detail="login and authentication commands are never run by Bot Office")
        if not slash_mod.NAME_RE.match(name):
            raise HTTPException(status_code=422, detail="invalid command name")
        item = await slash_catalog.resolve(bot.default_provider, name, memory.bot_dir(bot.id))
        if item is None:
            raise HTTPException(status_code=404, detail=f"/{name} is not a command of {bot.default_provider}")
        args = body.args.strip()
        action = item["action"]
        if action == "grey":
            raise HTTPException(status_code=422, detail=item.get("reason") or "cannot run headless")
        if action == "map":
            target = item["target"]
            if target == slash_mod.NEW:
                if not body.confirm:
                    raise HTTPException(status_code=409, detail={
                        "confirm_required": True,
                        "message": f"Start a fresh {bot.default_provider} session for {bot.name}? "
                                   "Chat history, memory and WORKING.md are kept."})
                return {"action": "map", "target": target, **_reset_session(bot)}
            return {"action": "map", "target": target, "args": args}
        mode = {"run": "raw", "command": "command", "expand": "expand"}[action]
        text = slash_mod.slash_job_text(name, args)
        prompt = text
        if mode == "expand":
            prompt = await asyncio.to_thread(slash_mod.expand_file, item["file"], args)
            if not prompt.strip():
                raise HTTPException(status_code=422, detail=f"/{name} is empty")
        encoded = slash_mod.encode_slash(name, args, mode)

        def mark(db, job: Job) -> None:
            db.execute("UPDATE jobs SET slash=? WHERE id=?", (encoded, str(job.id)))
            job.slash = encoded

        try:
            message, job = scheduler.turns.post_message(bot, text, prompt=prompt, hook=mark)
        except TurnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        queued = any(str(j.id) != str(job.id) for j in store.pending_jobs(bot.id))
        return {"action": action, "message": jsonable_encoder(message), "job": jsonable_encoder(job),
                "delivery": "queue" if queued else "start",
                "delivery_note": "Queued as its own turn behind the bot's other work." if queued else None}

    @app.delete("/v1/bots/{bot_id}", status_code=204, dependencies=[Depends(auth)])
    async def delete_bot(bot_id: UUID) -> None:
        """Remove the bot, its messages, routines and data/bots/<id>/. Shared memory stays."""
        async with attachment_global_lock, attachment_lock(bot_id):
            _bot(bot_id)
            for job in store.delete_bot(bot_id):
                scheduler.cancel(job, "bot deleted")
            try:
                await attachment_io(memory.delete_bot_dir, bot_id)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=500, detail="bot removed but its data dir was not") from exc

    def _icon_path(bot_id: UUID) -> Path:
        return memory.bot_dir(bot_id) / ICON_FILE

    @app.post("/v1/bots/{bot_id}/attachments", status_code=201, dependencies=[Depends(auth)])
    async def upload_attachment(bot_id: UUID, request: Request, name: str | None = None) -> dict:
        """Raw body upload. Without ``name`` the body must be an image (10 MiB). With ``name`` (0.11.13) an image is
        still stored as an image; anything else is stored as a file of that name (BOT_OFFICE_MAX_UPLOAD_MB, 50)."""
        _bot(bot_id)
        limit = attachments.max_file_bytes() if name else attachments.MAX_BYTES
        too_big = f"file too large (the limit is {limit // (1024 * 1024)} MB)" if name else "image too large"
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > limit:
                    raise HTTPException(status_code=413, detail=too_big)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
        if upload_slots.locked():
            raise HTTPException(status_code=429, detail="too many concurrent uploads")
        async with upload_slots:
            cutoff = time.monotonic() - 60
            recent = [stamp for stamp in attachment_uploads.get(bot_id, []) if stamp > cutoff]
            if len(recent) >= 30:
                raise HTTPException(status_code=429, detail="too many attachment uploads")
            recent.append(time.monotonic())
            attachment_uploads[bot_id] = recent
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(status_code=413, detail=too_big)
                data.extend(chunk)
            if not data:
                raise HTTPException(status_code=400, detail="file is empty" if name else "image is empty")
            mime = _sniff_image(data)
            if not mime and not name:
                raise HTTPException(status_code=415, detail="unsupported image")
            if mime and len(data) > attachments.MAX_BYTES:
                raise HTTPException(status_code=413, detail="image too large")
            try:
                async with attachment_global_lock, attachment_lock(bot_id):
                    _bot(bot_id)
                    refs = await asyncio.to_thread(store.attachment_references)
                    if mime:
                        stored = await attachment_io(attachments.save, settings.data_dir, bot_id, data, mime, refs)
                    else:
                        stored = await attachment_io(attachments.save_file, settings.data_dir, bot_id, data, name or "file", refs)
            except OverflowError as exc:
                raise HTTPException(status_code=507, detail=str(exc)) from exc
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail="invalid attachment directory") from exc
            return {"id": stored, "mime": mime or "application/octet-stream", "size": len(data), "kind": "image" if mime else "file",
                    "name": None if mime else attachments.display_name(stored),
                    "url": f"/v1/bots/{bot_id}/attachments/{stored}"}

    @app.delete("/v1/bots/{bot_id}/attachments/{attachment_id}", status_code=204, dependencies=[Depends(auth)])
    async def delete_attachment(bot_id: UUID, attachment_id: str) -> None:
        async with attachment_global_lock, attachment_lock(bot_id):
            _bot(bot_id)
            refs = await asyncio.to_thread(store.attachment_references)
            if attachment_id in refs.get(str(bot_id), set()):
                raise HTTPException(status_code=409, detail="attachment is referenced by a message")
            try:
                await attachment_io(attachments.delete, settings.data_dir, bot_id, attachment_id)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=404, detail="attachment not found") from exc

    @app.get("/v1/bots/{bot_id}/attachments/{attachment_id}/info", dependencies=[Depends(auth)])
    async def attachment_info(bot_id: UUID, attachment_id: str) -> dict:
        """Name, size and kind of a stored image or file (for the chat's file chips)."""
        _bot(bot_id)
        try:
            size = await asyncio.to_thread(attachments.size, settings.data_dir, bot_id, attachment_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404, detail="attachment not found") from exc
        is_file = attachments.is_file_id(attachment_id)
        return {"id": attachment_id, "kind": "file" if is_file else "image", "size": size,
                "name": attachments.display_name(attachment_id),
                "preview": attachments.serve_type(attachment_id)[1] if is_file else None}

    @app.get("/v1/bots/{bot_id}/attachments/{attachment_id}", dependencies=[Depends(auth)])
    async def get_attachment(bot_id: UUID, attachment_id: str, download: bool = False) -> Response:
        _bot(bot_id)
        try:
            data = await asyncio.to_thread(attachments.read, settings.data_dir, bot_id, attachment_id)
            mime = _sniff_image(data)
            if attachments.is_file_id(attachment_id):
                mime = None  # a file is data: its type comes from an allow-list by extension, never from its bytes
            elif not mime or attachments.EXTENSIONS[mime] != attachment_id.rsplit(".", 1)[-1]:
                raise ValueError("invalid image")
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404, detail="attachment not found") from exc
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
                   "Content-Security-Policy": "default-src 'none'; sandbox"}
        if mime is None:
            label = attachments.display_name(attachment_id)
            served, preview = attachments.serve_type(attachment_id)
            inline = preview in ("pdf", "text") and not download  # html and svg are framed only through /view
            headers["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{label}"'
            return Response(data, media_type=served if inline else "application/octet-stream", headers=headers)
        headers["Content-Disposition"] = "inline"
        return Response(data, media_type=mime, headers=headers)

    VIEW_SECONDS = 120

    def _view_signature(bot_id: UUID, attachment_id: str, expires: int, scripts: bool = False) -> str:
        key = (settings.read_token() or "").encode()
        if not key:
            raise HTTPException(status_code=503, detail="API token is not configured")
        mac = hmac.new(key, f"view\n{bot_id}\n{attachment_id}\n{expires}\n{int(scripts)}".encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")

    @app.post("/v1/bots/{bot_id}/attachments/{attachment_id}/view-link", dependencies=[Depends(auth)])
    async def attachment_view_link(bot_id: UUID, attachment_id: str, scripts: bool = False) -> dict:
        """A two-minute URL for the sandboxed viewer of one HTML or SVG file: an iframe cannot send the bearer
        token, so the link itself carries a signature for exactly this bot and file. ``scripts`` (default off, asked
        for again at every open) signs a link whose response lets inline script run in the still-opaque sandbox."""
        _bot(bot_id)
        if not attachments.is_file_id(attachment_id) or attachments.serve_type(attachment_id)[1] not in ("html", "svg"):
            raise HTTPException(status_code=400, detail="only an html or svg file can be viewed")
        try:
            await asyncio.to_thread(attachments.size, settings.data_dir, bot_id, attachment_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404, detail="attachment not found") from exc
        expires = int(time.time()) + VIEW_SECONDS
        sig = _view_signature(bot_id, attachment_id, expires, scripts)
        flag = "&scripts=1" if scripts else ""
        return {"url": f"/v1/bots/{bot_id}/attachments/{attachment_id}/view?exp={expires}{flag}&sig={sig}",
                "expires_in": VIEW_SECONDS, "scripts": scripts}

    @app.get("/v1/bots/{bot_id}/attachments/{attachment_id}/view")
    async def view_attachment(request: Request, bot_id: UUID, attachment_id: str, exp: int = 0, sig: str = "", scripts: int = 0) -> Response:
        """An HTML or SVG file for the page's sandboxed iframe. Authorised by the signed link, not the bearer.
        The response carries ``Content-Security-Policy: sandbox`` (no allow-same-origin, no scripts, no network), and the
        page frames it with an empty ``sandbox`` attribute as well. A link signed with ``scripts=1`` (the signature
        covers the flag, so it cannot be added to a plain link) swaps in ``allow-scripts`` only, still without network.
        0.12.11: a scripts link is served only when the browser says it loads it into a frame (``Sec-Fetch-Dest:
        iframe`` or ``frame``). Opened as its own page (``document``, any other destination) it gets a plain-text 403
        and nothing runs. A missing header (old browser, curl, other client) is refused too: fail closed, since only
        a browser that sends the header can show the link is framed. Plain links are not affected."""
        run = scripts == 1
        if exp < int(time.time()) or not hmac.compare_digest(sig.encode(), _view_signature(bot_id, attachment_id, exp, run).encode()):
            raise HTTPException(status_code=403, detail="link expired or invalid")
        if run and request.headers.get("sec-fetch-dest", "").strip().lower() not in ("iframe", "frame"):
            return Response("Scripts run only inside the Bot Office viewer. Open the file there and switch on Run scripts.\n",
                            status_code=403, media_type="text/plain", headers={
                                "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
                                "Referrer-Policy": "no-referrer", "Vary": "Sec-Fetch-Dest",
                                "Content-Security-Policy": "default-src 'none'; sandbox"})
        served, preview = attachments.serve_type(attachment_id)
        if not attachments.is_file_id(attachment_id) or preview not in ("html", "svg"):
            raise HTTPException(status_code=404, detail="attachment not found")
        try:
            data = await asyncio.to_thread(attachments.read, settings.data_dir, bot_id, attachment_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404, detail="attachment not found") from exc
        headers = {
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (attachments.VIEW_CSP_SCRIPTS if run else attachments.VIEW_CSP) + "; frame-ancestors 'self'",
            "Content-Disposition": f'inline; filename="{attachments.display_name(attachment_id)}"'}
        if run:
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Vary"] = "Sec-Fetch-Dest"
        return Response(data, media_type=served, headers=headers)

    def _png_response(request: Request, data: bytes, mime: str, versioned: bool) -> Response:
        tag = avatars.etag(data)
        headers = {
            "ETag": tag,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400" if versioned else "private, no-cache",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type=mime, headers=headers)

    @app.get("/v1/bots/{bot_id}/icon", dependencies=[Depends(auth)])
    async def get_bot_icon(bot_id: UUID, request: Request, v: str | None = None) -> Response:
        """The uploaded image, else the generated avatar of the bot's seed, else 404 (the emoji shows).
        Cached by ETag; a request carrying ``v`` (the client passes the bot's updated_at) may be cached for good."""
        bot = _bot(bot_id)
        if bot.icon_image:
            try:
                data = await attachment_io(safefs.read_bytes, _icon_path(bot_id), MAX_ICON_BYTES)
            except OSError:
                data = None
            if data is not None:
                return _png_response(request, data, bot.icon_image, v is not None)
        if bot.avatar_seed and avatars.valid_seed(bot.avatar_seed):
            data = await avatars.run(avatars.render_png, bot.avatar_seed)
            return _png_response(request, data, "image/png", v is not None)
        raise HTTPException(status_code=404, detail="no icon image")

    @app.put("/v1/bots/{bot_id}/icon", response_model=Bot, dependencies=[Depends(auth)])
    async def put_bot_icon(bot_id: UUID, request: Request) -> Bot:
        """Upload a PNG, JPEG or WebP (GIF first frame) as the raw request body (max 2 MB). It is checked, cropped
        to a square, resized to 256 px and stored as PNG; SVG and anything else is refused."""
        _bot(bot_id)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_ICON_BYTES:
            raise HTTPException(status_code=413, detail="icon image is too large (limit 2 MB)")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_ICON_BYTES:
                raise HTTPException(status_code=413, detail="icon image is too large (limit 2 MB)")
        try:
            png = await avatars.run(avatars.normalise_upload, bytes(data))
        except avatars.AvatarError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        # bots/<id> is bot-writable when isolated: create, write and replace without following links
        safefs.mkdir(memory.bot_dir(bot_id), isolation.shared_dir_mode())
        safefs.write_bytes(_icon_path(bot_id), png, 0o600)
        updated = store.update_bot(bot_id, icon_image="image/png")
        if updated is None:   # deleted while the image was being processed
            safefs.unlink(_icon_path(bot_id))
            raise HTTPException(status_code=404, detail="bot not found")
        return updated

    @app.delete("/v1/bots/{bot_id}/icon", response_model=Bot, dependencies=[Depends(auth)])
    async def delete_bot_icon(bot_id: UUID) -> Bot:
        """Remove the uploaded image and the generated avatar: the bot shows its emoji."""
        _bot(bot_id)
        safefs.unlink(_icon_path(bot_id))
        updated = store.update_bot(bot_id, icon_image=None, avatar_seed=None)
        if updated is None:
            raise HTTPException(status_code=404, detail="bot not found")
        return updated

    @app.post("/v1/bots/{bot_id}/icon/generate", response_model=Bot, dependencies=[Depends(auth)])
    async def generate_bot_icon(bot_id: UUID, body: AvatarGenerate | None = None) -> Bot:
        """Give the bot a generated avatar (a given seed, e.g. a preset, or a fresh random one). Replaces an upload."""
        _bot(bot_id)
        seed = (body.seed if body else None) or secrets.token_hex(6)
        if not avatars.valid_seed(seed):
            raise HTTPException(status_code=400, detail="seed must be 1-64 letters, digits or . _ : -")
        await avatars.run(avatars.render_png, seed)
        safefs.unlink(_icon_path(bot_id))
        updated = store.update_bot(bot_id, icon_image=None, avatar_seed=seed)
        if updated is None:
            raise HTTPException(status_code=404, detail="bot not found")
        return updated

    @app.put("/v1/bots/{bot_id}/avatar/character", response_model=Bot, dependencies=[Depends(auth)])
    async def set_bot_character(bot_id: UUID, body: AvatarCharacter) -> Bot:
        """0.12.2: give the bot a built-in character (shape + color; drawn in the browser, its face follows the
        bot's status). Replaces an upload or a generated pattern."""
        _bot(bot_id)
        seed = avatars.character_seed(body.shape, body.color)
        if seed is None:
            raise HTTPException(status_code=400, detail="unknown character shape or color")
        safefs.unlink(_icon_path(bot_id))
        updated = store.update_bot(bot_id, icon_image=None, avatar_seed=seed)
        if updated is None:
            raise HTTPException(status_code=404, detail="bot not found")
        return updated

    @app.get("/v1/avatar-characters", dependencies=[Depends(auth)])
    async def avatar_characters() -> dict:
        return {"shapes": list(avatars.CHAR_SHAPES), "colors": list(avatars.CHAR_COLORS),
                "expressions": list(avatars.CHAR_EXPRESSIONS), "aliases": dict(avatars.CHAR_SHAPE_ALIASES)}

    @app.get("/v1/avatar-presets", dependencies=[Depends(auth)])
    async def avatar_presets() -> dict:
        return {"presets": [{"seed": seed} for seed in avatars.PRESET_SEEDS]}

    @app.get("/v1/avatar-preview", dependencies=[Depends(auth)])
    async def avatar_preview(request: Request, seed: str) -> Response:
        if not avatars.valid_seed(seed):
            raise HTTPException(status_code=400, detail="seed must be 1-64 letters, digits or . _ : -")
        data = await avatars.run(avatars.render_png, seed)
        return _png_response(request, data, "image/png", True)

    # ---- sidebar sections (0.12.0) ----
    def _sections(call, *args):
        try:
            return call(*args)
        except sections_mod.SectionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/v1/sections", dependencies=[Depends(auth)])
    async def get_sections() -> dict:
        return store.sections.layout()

    @app.post("/v1/sections", status_code=201, dependencies=[Depends(auth)])
    async def create_section(body: SectionCreate) -> dict:
        return _sections(store.sections.create, body.name)

    @app.put("/v1/sections/order", dependencies=[Depends(auth)])
    async def order_sections(body: SectionOrder) -> dict:
        return _sections(store.sections.reorder, body.ids)

    @app.put("/v1/sections/hidden/members", dependencies=[Depends(auth)])
    async def set_hidden_members(body: HiddenMembers) -> dict:
        """0.12.12: make exactly these bots the Hidden section's members (others there return to Ungrouped)."""
        return _sections(store.sections.set_hidden, [str(b) for b in body.bot_ids])

    @app.patch("/v1/sections/{section_id}", dependencies=[Depends(auth)])
    async def patch_section(section_id: str, body: SectionPatch) -> dict:
        return _sections(store.sections.update, section_id, body.name, body.collapsed)

    @app.delete("/v1/sections/{section_id}", dependencies=[Depends(auth)])
    async def delete_section(section_id: str) -> dict:
        """Its bots move to the top of Ungrouped; no bot is deleted."""
        moved = _sections(store.sections.delete, section_id)
        return {"deleted": section_id, "moved_bot_ids": moved}

    @app.put("/v1/bots/{bot_id}/section", dependencies=[Depends(auth)])
    async def move_bot_section(bot_id: UUID, body: BotSectionMove) -> dict:
        return _sections(store.sections.move_bot, str(bot_id), body.section_id, body.index)

    @app.get("/v1/bots/{bot_id}/memory", dependencies=[Depends(auth)])
    async def get_bot_memory(bot_id: UUID) -> dict[str, str]:
        _bot(bot_id)
        return {"text": memory.read_bot(bot_id)}

    @app.put("/v1/bots/{bot_id}/memory", dependencies=[Depends(auth)])
    async def put_bot_memory(bot_id: UUID, body: TextBody) -> dict[str, str]:
        _bot(bot_id)
        memory.write_bot(bot_id, body.text)
        return {"text": body.text}

    @app.get("/v1/shared-memory", dependencies=[Depends(auth)])
    async def get_shared_memory() -> dict[str, str]:
        return {"text": memory.read_shared()}

    @app.put("/v1/shared-memory", dependencies=[Depends(auth)])
    async def put_shared_memory(body: TextBody) -> dict[str, str]:
        memory.write_shared(body.text)
        return {"text": body.text}

    # 0.11.9/0.11.10: the owner-side layers every bot gets (Rules for all bots; the System prompt, which
    # /v1/baseline has edited since 0.11.9). Owner only (master token): bots read them as files in their
    # job directory but cannot change them.
    def _layer_routes(layer: baseline.Layer, *paths: str) -> None:
        async def get_layer() -> dict[str, object]:
            return layer.state(store)

        async def put_layer(body: TextBody) -> dict[str, object]:
            try:
                layer.set_override(store, body.text)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return layer.state(store)

        async def reset_layer() -> dict[str, object]:
            layer.reset(store)
            return layer.state(store)

        for path in paths:
            app.get(path, dependencies=[Depends(auth)])(get_layer)
            app.put(path, dependencies=[Depends(auth)])(put_layer)
            app.delete(path, dependencies=[Depends(auth)])(reset_layer)

    _layer_routes(baseline.SYSTEM, "/v1/baseline", "/v1/system-prompt")
    _layer_routes(baseline.RULES, "/v1/rules-for-all-bots")

    @app.get("/v1/settings", dependencies=[Depends(auth)])
    async def get_settings() -> dict[str, object]:
        """Settings for all bots (0.11.10). The two layers have their own routes (large texts)."""
        gates = {name: action_rules.gate_state(settings.control_dir, name) for name in action_rules.WIRED}
        auto = baseline.auto_continue(store)
        return {"auto_continue": auto,
                "auto_continue_max_limit": baseline.AUTO_CONTINUE_MAX,
                # what a chat chain really gets (max, or consecutive if lower)
                "auto_continue_chat_limit": continuation.chat_limit(auto),
                "auto_continue_defaults": continuation.defaults(),
                "approvals": {**approval_classes.settings(store), "gates": gates,
                              "unpausable_bots": approval_classes.unpausable_bots(store)},
                "notifications": {"email": notify.get_prefs(store), "configured": notifier.configured}}

    @app.put("/v1/settings", dependencies=[Depends(auth)])
    async def put_settings(body: SettingsBody) -> dict[str, object]:
        if body.auto_continue is not None:
            given = body.auto_continue.model_dump(exclude_unset=True)
            # null means "not given", except for a cap, where it means unlimited
            changes = {key: value for key, value in given.items() if value is not None or key in continuation.CAPS}
            try:
                baseline.set_auto_continue(store, maximum=changes.pop("max", None), **changes)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.notifications is not None:
            notify.set_prefs(store, body.notifications.model_dump())
        if body.approvals is not None:
            change = body.approvals
            try:
                # a new custom pattern is self-tested in a child process (up to 2 s): off the event loop
                await asyncio.to_thread(approval_classes.update, store, enabled=change.enabled,
                                        add=[item.model_dump() for item in change.add or []],
                                        remove=change.remove, timeout_min=change.timeout_min)
            except approval_classes.ApprovalClassError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await get_settings()

    @app.get("/v1/desktops", dependencies=[Depends(auth)])
    async def list_desktops() -> list[dict]:
        await bot_desks.refresh()
        result = await vnc.desktops()
        slots = {item["slot"]: item for item in scheduler.slots()}
        for desktop in result:
            name = desktop["name"]
            owner_bot = bot_desks.bot_of(name)
            if owner_bot:
                bot = store.get_bot(owner_bot)
                desktop["kind"] = "per-bot"
                desktop["bot"] = bot.name if bot else None
                desktop["bot_id"] = owner_bot
                desktop["job_id"] = None
            else:
                slot = slots.get(name, {})
                job = slot.get("current_job")
                bot = store.get_bot(job.bot_id) if job and job.bot_id else None
                desktop["kind"] = "pool" if name in settings.desktops else "other"
                desktop["bot"] = bot.name if bot else None
                desktop["bot_id"] = str(bot.id) if bot else None
                desktop["job_id"] = str(job.id) if job else None
            desktop["viewers"] = bot_desks.viewers.get(name, 0)
            desktop["owner_controlling"] = bot_desks.owner_controlling(name)
        return result

    def _desktop_error(exc: bot_desktops.DesktopError) -> HTTPException:
        return HTTPException(status_code=409, detail=str(exc))

    @app.post("/v1/desktops/{name}/start", dependencies=[Depends(auth)])
    async def start_desktop(name: str) -> dict:
        """Start a stopped per-bot desktop (the owner pressed Connect)."""
        if bot_desks.bot_of(name) is None:
            await bot_desks.refresh()
        if bot_desks.bot_of(name) is None:
            raise HTTPException(status_code=404, detail="not a per-bot desktop")
        try:
            return {"name": await bot_desks.start(name)}
        except bot_desktops.DesktopError as exc:
            raise _desktop_error(exc) from exc

    @app.post("/v1/bots/{bot_id}/desktop", dependencies=[Depends(auth)])
    async def open_bot_desktop(bot_id: UUID) -> dict:
        """The owner opens a bot's own desktop (created on first use)."""
        _bot(bot_id)
        try:
            return {"name": await bot_desks.ensure(str(bot_id))}
        except bot_desktops.DesktopError as exc:
            raise _desktop_error(exc) from exc

    @app.post("/v1/desktops/{name}/ticket", dependencies=[Depends(auth)])
    async def desktop_ticket(name: str, mode: str = Query(default="view", pattern="^(view|control)$"),
                             bot: UUID | None = None) -> dict:
        try:
            vnc.desktop_port(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="desktop not found") from exc
        # with ?bot=, the viewer only connects if this desktop is still that bot's own (it may have been
        # remapped between the page's list and this request)
        if bot is not None and bot_desks.bot_of(name) != str(bot):
            raise HTTPException(status_code=409, detail="this desktop is no longer that bot's; reload the list")
        if not any(item["name"] == name and item["status"] == "running" for item in await vnc.desktops()):
            raise HTTPException(status_code=404, detail="desktop is not running")
        if bot is not None and not await bot_desks.refresh():   # live listing: a replaced container is dropped
            raise HTTPException(status_code=503, detail="cannot verify the desktop right now; try again")
        if bot is not None and bot_desks.bot_of(name) != str(bot):   # again, after the awaits
            raise HTTPException(status_code=409, detail="this desktop is no longer that bot's; reload the list")
        # bound to who asked: a remote owner's ticket only opens a socket from that same verified person
        binding = vnc.binding_for(remote_access.current_remote_identity.get())
        return {"ticket": tickets.issue(name, mode, binding, str(bot) if bot else None), "expires_in": vnc.TICKET_TTL}

    @app.websocket("/v1/desktops/{name}/vnc")
    async def desktop_vnc(ws: WebSocket, name: str) -> None:
        try:
            port = vnc.desktop_port(name)
        except ValueError:
            await ws.close(code=1008)
            return
        protocols = ws.scope.get("subprotocols", [])
        ticket = next((p[10:] for p in protocols if p.startswith("bo-ticket.")), "")
        binding = vnc.binding_for(remote_access.current_remote_identity.get())
        if tickets.bot_bound(ticket) and not await bot_desks.refresh():   # fail closed without a live listing
            tickets.consume(ticket, name, binding)
            await ws.close(code=1013)
            return
        mode = (tickets.consume(ticket, name, binding, bot_desks.bot_of)
                if vnc.same_origin(ws.headers.get("origin"), ws.headers.get("host", "")) else None)
        if not mode or "binary" not in protocols or proxy_counts.get(name, 0) >= 4 or sum(proxy_counts.values()) >= 8:
            await ws.close(code=1008)
            return
        # admitted as a viewer before any await: a desktop chosen for stopping refuses, one being viewed is kept
        viewer = contextlib.ExitStack()
        try:
            viewer.enter_context(bot_desks.viewing(name, mode))
        except bot_desktops.DesktopError:
            await ws.close(code=1013)
            return
        proxy_counts[name] = proxy_counts.get(name, 0) + 1
        writer = None
        try:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await ws.close(code=1011)
                return
            await ws.accept(subprotocol="binary")
            last_traffic = time.monotonic()
            view_filter = vnc.ViewFilter() if mode == "view" else None

            async def to_desktop() -> None:
                nonlocal last_traffic
                while True:
                    data = await ws.receive_bytes()
                    last_traffic = time.monotonic()
                    data = view_filter.feed(data) if view_filter else data
                    if data:
                        writer.write(data)
                        await writer.drain()

            async def to_browser() -> None:
                nonlocal last_traffic
                while data := await reader.read(65536):
                    last_traffic = time.monotonic()
                    if view_filter:
                        view_filter.feed_server(data)
                    await ws.send_bytes(data)

            async def idle_watch() -> None:
                while True:
                    await asyncio.sleep(30)
                    if time.monotonic() - last_traffic >= 1800:
                        return

            tasks = [asyncio.create_task(to_desktop()), asyncio.create_task(to_browser()),
                     asyncio.create_task(idle_watch())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            viewer.close()
            proxy_counts[name] -= 1
            if writer:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()

    async def agent_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Job | None:  # noqa: B008
        if credentials:
            job = store.agent_token_job(credentials.credentials)
            if job:
                return job
        await auth(credentials)
        return None

    async def agent_scope(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Job:  # noqa: B008
        """The job-scoped agent token only (never the master token): its bot, while its job runs."""
        job = store.agent_token_job(credentials.credentials) if credentials else None
        if not job or not job.bot_id:
            raise HTTPException(status_code=401, detail="a running job's agent token is required")
        return job

    @app.post("/v1/agent/desktop")
    async def agent_desktop(body: AgentDesktopRequest | None = None,
                            job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        """remote-desktop: the calling bot's own desktop (from the token, never an argument), started
        on demand. ``desk`` uses it when it runs inside a turn without --slot. With ``slot`` (only the
        pool desktop this turn leased) it just reports whether the owner is controlling that one."""
        if body is not None and body.slot is not None:
            if not job.slot or body.slot != job.slot:
                raise HTTPException(status_code=403, detail="that desktop is not leased to this turn")
            return {"name": job.slot, "owner_controlling": bot_desks.owner_controlling(job.slot)}
        if not bot_desks.enabled:
            raise HTTPException(status_code=409, detail="per-bot desktops are off")
        try:
            name = await bot_desks.ensure(str(job.bot_id))
        except bot_desktops.DesktopError as exc:
            raise _desktop_error(exc) from exc
        return {"name": name, "owner_controlling": bot_desks.owner_controlling(name)}

    def _target_bot(ref: str) -> Bot:
        """Another bot by name, id or alias; the 404 says that only registered bots can hold tasks
        (inbox senders such as CLI engineer runs are not bots)."""
        found = store.find_bot(ref.strip())
        if not found:
            raise HTTPException(status_code=404, detail=(
                f"bot not found: no registered bot is named {ref.strip()[:80]!r}. `desk team list` shows the "
                "bots; an inbox sender (for example a CLI engineer run) is not a bot and cannot be given tasks"))
        return found

    def _admin(job: Job, target: Bot | None = None) -> Bot:
        """The caller's bot if it is an app admin, else 403; with *target*, also 403 unless the admin may
        reach that bot: never a private bot, another admin or a bot with owner-granted sudo."""
        bot = store.get_bot(job.bot_id)
        if bot is None or not bot.app_admin:
            raise HTTPException(status_code=403, detail="only an app admin bot may do this (the owner sets app_admin)")
        if target is not None and target.id != bot.id:
            if target.team_private or target.app_admin or target.allow_sudo:
                raise HTTPException(status_code=403, detail="a private bot, another admin or a bot with sudo is "
                                                            "changed only by the owner")
        return bot

    @app.patch("/v1/agent/bots/{bot_ref}", response_model=Bot)
    async def agent_admin_update_bot(bot_ref: str, body: AgentAdminBotUpdate,
                                     job: Job = Depends(agent_scope)) -> Bot:  # noqa: B008
        """0.12.26: an app admin edits a bot's profile and work settings. Never its engine, instructions
        (the owner's voice), access or safety settings (cli_access, allow_sudo, scheduled_runs, budgets,
        team visibility, app_admin), and never a private bot, another admin or a bot with sudo; those stay
        with the owner (master token)."""
        _admin(job)
        target = _target_bot(bot_ref)
        _admin(job, target)
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(status_code=400, detail="nothing to change")
        try:
            update = BotUpdate(**fields)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=jsonable_encoder(exc.errors(include_url=False))) from exc
        return await update_bot(target.id, update)

    @app.post("/v1/agent/bots", response_model=Bot, status_code=201)
    async def agent_create_bot(body: BotCreate, job: Job = Depends(agent_scope)) -> Bot:  # noqa: B008
        """0.12.20: a running bot creates a bot; it lands in the creator's group unless a group is given."""
        return _make_bot(body, creator_bot_id=job.bot_id)

    @app.post("/v1/agent/ask", status_code=201)
    async def agent_ask(request: Request, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        from . import questions
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("blocking", True), bool):
                raise ValueError("invalid question or blocking flag")
            return questions.create(store, job.bot_id, job.id, body, body.get("blocking", True))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/agent/say", status_code=201, response_model=Message)
    async def agent_say(request: Request, job: Job = Depends(agent_scope)) -> Message:  # noqa: B008
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected an object with text")
            return store.say(job.bot_id, job.id, body.get("text"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except EditError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.get("/v1/agent/questions/{question_id}")
    async def agent_question(question_id: str, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        from . import questions
        item = questions.get(store, question_id)
        if not item or item["job_id"] != str(job.id):
            raise HTTPException(status_code=404, detail="question not found")
        return item

    @app.post("/v1/bots/{bot_id}/questions/{question_id}/answer", dependencies=[Depends(auth)])
    async def answer_question(bot_id: UUID, question_id: str, request: Request) -> dict:
        from . import questions
        bot = _bot(bot_id)
        try:
            body = await request.json()
            item = questions.answer(store, bot_id, question_id, body)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        asker = store.get(item["job_id"]) if item["job_id"] else None
        running = asker is not None and asker.status == JobStatus.RUNNING
        # A running job's waiting helper returns a blocking answer itself; in every other
        # case (non-blocking, or the helper is gone) it reaches the bot as a user message.
        if not (item["blocking"] and running):
            answer = item["answer"]
            if answer["skip"]:
                text = "[The user skipped your question]"
            else:
                parts = []
                for step, choice in zip(item["payload"]["steps"], answer["steps"], strict=True):
                    parts.append(step["prompt"] + ": " + ", ".join(
                        choice["selected"] + ([choice["custom"]] if choice["custom"] else [])))
                text = "[Answer to your question] " + "\n".join(parts)
            steered = running and store.post_steer(bot.id, text, [], job_id=item["job_id"])
            if not steered:
                try:
                    _post_owner_message(bot, text)
                except TurnError:
                    # Keep the answer queued even if the provider was disabled meanwhile.
                    store.enqueue_message(bot, text, text, bot.reason, "chat", None, None, None)
        return item

    def _marked(events: list[dict]) -> list[dict]:
        # whose text an event note is: anything not the owner's is data, never the owner's instructions
        return [{**event, "bot_written": event.get("actor") != "user"} for event in events]

    def _agent_assignment_id(job: Job, ref: str) -> str:
        if ref in ("current", "-"):
            if not job.assignment_id:
                raise HTTPException(status_code=400, detail="this turn has no current assignment; pass its id")
            return str(job.assignment_id)
        try:
            return str(UUID(ref))
        except ValueError:
            pass
        # 0.12.40: the 8-character ids bot-task prints work too (a unique prefix of 8+ hex characters)
        try:
            return assignments.find(ref)
        except AssignmentError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.get("/v1/agent/approvals")
    async def agent_approvals(ids: str = "", job: Job = Depends(agent_scope)) -> list[dict]:  # noqa: B008
        """0.12.24 live source: the bot's own approval cards (no ids: the latest 20), or the current
        status of the cards named in *ids* (another bot's card: its status only). Read-only: a pending
        card past its expiry reads as expired (the watchdog persists that)."""
        moment = datetime.now(UTC).isoformat()
        def shape(item: dict) -> dict:
            status = item.get("status")
            if status == "pending" and item.get("expires_at") and str(item["expires_at"]) <= moment:
                status = "expired"
            elif status == "approved" and item.get("grant_expires_at") and str(item["grant_expires_at"]) <= moment:
                status = "approved, grant expired"
            if str(item.get("bot_id")) != str(job.bot_id):
                return {"id": item["id"], "status": status}
            out = {key: item.get(key) for key in ("id", "created_at", "decided_at", "expires_at")}
            return {**out, "status": status, "mine": True, "summary": failures.clip(item.get("summary") or "", 200)}
        with closing(store._connect()) as db:
            if ids.strip():
                wanted = []
                for part in ids.replace(",", " ").split()[:20]:
                    number = part.lstrip("#")
                    if not re.fullmatch(r"[0-9]{1,18}", number) or int(number) == 0:
                        raise HTTPException(status_code=400, detail=f"not an approval number: {part[:20]}")
                    wanted.append(int(number))
                found = {row["id"]: dict(row) for n in wanted
                         for row in db.execute("SELECT * FROM approvals WHERE id=?", (n,))}
                # another bot's card only if this bot may read that bot (team ACL); else it does not exist
                reader = db.execute("SELECT * FROM bots WHERE id=?", (str(job.bot_id),)).fetchone()
                sections = team.section_map(db)
                def visible(item: dict) -> bool:
                    if str(item.get("bot_id")) == str(job.bot_id):
                        return True
                    target = db.execute("SELECT * FROM bots WHERE id=?", (str(item.get("bot_id")),)).fetchone()
                    return bool(reader and target and team.can_read(dict(reader), dict(target), sections))
                return [shape(found[n]) if n in found and visible(found[n]) else {"id": n, "status": "not_found"}
                        for n in wanted]
            rows = db.execute("SELECT * FROM approvals WHERE bot_id=? ORDER BY id DESC LIMIT 20",
                              (str(job.bot_id),)).fetchall()
        return [shape(dict(row)) for row in rows]

    @app.get("/v1/agent/assignments")
    async def agent_list_assignments(job: Job = Depends(agent_scope)) -> list[Assignment]:  # noqa: B008
        return assignments.visible_to(job.bot_id)

    @app.post("/v1/agent/assignments", response_model=Assignment, status_code=201)
    async def agent_create_assignment(body: AgentAssignmentCreate, job: Job = Depends(agent_scope)) -> Assignment:  # noqa: B008
        target = None
        if body.for_bot:
            target = _target_bot(body.for_bot).id
        parent = _agent_assignment_id(job, body.parent_id) if body.parent_id else None
        fields = body.model_dump(exclude={"for_bot", "parent_id"})
        return _assignment_call(assignments.agent_create, job.bot_id, fields, for_bot=target, parent_id=parent)

    @app.get("/v1/agent/assignments/{ref}")
    async def agent_get_assignment(ref: str, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        item = _assignment_call(assignments.readable_by, _agent_assignment_id(job, ref), job.bot_id)
        return {**item.model_dump(mode="json"), "events": _marked(assignments.events(item.id, 10))}

    @app.post("/v1/agent/assignments/{ref}/status", response_model=Assignment)
    async def agent_assignment_status(ref: str, body: AgentStatus, job: Job = Depends(agent_scope)) -> Assignment:  # noqa: B008
        return _assignment_call(assignments.report, _agent_assignment_id(job, ref), job.bot_id, job.id,
                                body.state.strip().lower(), body.note.strip(), body.evidence.strip())

    @app.post("/v1/agent/assignments/{ref}/checkpoint", response_model=Assignment)
    async def agent_assignment_checkpoint(ref: str, body: AgentText, job: Job = Depends(agent_scope)) -> Assignment:  # noqa: B008
        return _assignment_call(assignments.save_checkpoint, _agent_assignment_id(job, ref), job.bot_id, job.id, body.text)

    @app.post("/v1/agent/assignments/{ref}/note", status_code=204)
    async def agent_assignment_note(ref: str, body: AgentText, job: Job = Depends(agent_scope)) -> None:  # noqa: B008
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="note is empty")
        _assignment_call(assignments.add_note, _agent_assignment_id(job, ref), job.bot_id, job.id, body.text.strip()[:2000])

    # --- 0.11.4 task lists (job token only): every list readable, cross-bot add/edit ---
    # AssignmentError raised here is answered by the handler below (409 conflicts carry the
    # current task and the changes since the caller's version)

    def _agent_task_id(job: Job, ref: str) -> str:
        if ref in ("current", "-"):
            return _agent_assignment_id(job, ref)
        return assignments.find(ref)

    @app.get("/v1/agent/tasks", response_model=list[Task])
    async def agent_list_tasks(bot: str | None = None, closed: bool = False,
                               job: Job = Depends(agent_scope)) -> list[Task]:  # noqa: B008
        """Your list, another bot's (``bot=<name or id>``) or every bot's (``bot=all``)."""
        target = job.bot_id
        if bot == "all":
            target = None
        elif bot:
            found = store.find_bot(bot)
            if not found:
                raise HTTPException(status_code=404, detail="bot not found")
            target = found.id
        return assignments.tasks(target, include_closed=closed)

    @app.get("/v1/agent/tasks/changes")
    async def agent_task_changes(since: int | None = Query(default=None, ge=0),
                                 job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        """What others changed on your tasks (and on tasks you created) since your last prompt
        (or since event *since*). Reading does not mark them seen."""
        changes, last, total = assignments.changes(job.bot_id, since)
        # last_event: pass it as *since* for the next page while more is true
        if len(changes) < total:
            last = changes[-1]["id"]
        return {"changes": changes, "last_event": last, "more": len(changes) < total}

    @app.get("/v1/agent/tasks/{ref}")
    async def agent_get_task(ref: str, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        item = assignments.task(_agent_task_id(job, ref))
        return {**item.model_dump(mode="json"), "events": _marked(assignments.events(item.id, 20)),
                "children": [child.model_dump(mode="json") for child in assignments.children(item.id)]}

    @app.post("/v1/agent/tasks", response_model=Task, status_code=201)
    async def agent_create_task(body: AgentTaskCreate, job: Job = Depends(agent_scope)) -> Task:  # noqa: B008
        target = None
        if body.for_bot:
            target = _target_bot(body.for_bot).id
        parent = _agent_task_id(job, body.parent_id) if body.parent_id else None
        fields = body.model_dump(exclude={"for_bot", "parent_id"})
        item = assignments.agent_create(job.bot_id, fields, for_bot=target, parent_id=parent, job_id=job.id)
        return assignments.task(item.id)

    @app.patch("/v1/agent/tasks/{ref}", response_model=Task)
    async def agent_edit_task(ref: str, body: AgentTaskEdit, job: Job = Depends(agent_scope)) -> Task:  # noqa: B008
        fields = body.model_dump(exclude_unset=True, exclude={"version"})
        for key in ("title", "goal", "done_when", "priority"):
            if key in fields and fields[key] is None and key not in (body.model_extra or {}):
                del fields[key]
        return assignments.agent_edit(_agent_task_id(job, ref), job.bot_id, fields, body.version)

    @app.post("/v1/agent/tasks/reorder", response_model=list[Task])
    async def agent_reorder_tasks(body: AgentTaskReorder, job: Job = Depends(agent_scope)) -> list[Task]:  # noqa: B008
        ids = [_agent_task_id(job, ref) for ref in body.order]
        return assignments.reorder(job.bot_id, ids, actor=f"bot:{job.bot_id}")

    @app.post("/v1/agent/tasks/{ref}/status", response_model=Task)
    async def agent_task_status(ref: str, body: AgentStatus, job: Job = Depends(agent_scope)) -> Task:  # noqa: B008
        item = assignments.report(_agent_task_id(job, ref), job.bot_id, job.id, body.state.strip().lower(),
                                  body.note.strip(), body.evidence.strip())
        return assignments.task(item.id)

    @app.post("/v1/agent/tasks/{ref}/drop", response_model=Task)
    async def agent_drop_task(ref: str, body: AgentTaskDrop, job: Job = Depends(agent_scope)) -> Task:  # noqa: B008
        return assignments.agent_drop(_agent_task_id(job, ref), job.bot_id, job.id, body.note)

    @app.post("/v1/agent/tasks/{ref}/note", status_code=204)
    async def agent_task_note(ref: str, body: AgentText, job: Job = Depends(agent_scope)) -> None:  # noqa: B008
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="note is empty")
        assignments.add_note(_agent_task_id(job, ref), job.bot_id, job.id, body.text.strip()[:2000])

    @app.post("/v1/agent/tasks/{ref}/checkpoint", response_model=Task)
    async def agent_task_checkpoint(ref: str, body: AgentText, job: Job = Depends(agent_scope)) -> Task:  # noqa: B008
        item = assignments.save_checkpoint(_agent_task_id(job, ref), job.bot_id, job.id, body.text)
        return assignments.task(item.id)

    @app.get("/v1/agent/wakeups")
    async def agent_list_wakeups(job: Job = Depends(agent_scope)) -> list[Wakeup]:  # noqa: B008
        return assignments.wakeups(job.bot_id)

    @app.post("/v1/agent/wakeups", response_model=Wakeup, status_code=201)
    async def agent_add_wakeup(body: AgentWakeup, job: Job = Depends(agent_scope)) -> Wakeup:  # noqa: B008
        due = body.due_at
        if body.in_minutes is not None:
            due = datetime.now(UTC) + timedelta(minutes=body.in_minutes)
        linked = _agent_assignment_id(job, body.assignment_id) if body.assignment_id else None
        waits = _agent_assignment_id(job, body.on_assignment) if body.on_assignment else None
        return _assignment_call(assignments.add_wakeup, job.bot_id, job.id, assignment_id=linked, due_at=due,
                                on_assignment=waits, note=body.note.strip())

    @app.delete("/v1/agent/wakeups/{wakeup_id}", status_code=204)
    async def agent_cancel_wakeup(wakeup_id: UUID, job: Job = Depends(agent_scope)) -> None:  # noqa: B008
        _assignment_call(assignments.cancel_wakeup, wakeup_id, job.bot_id)

    def _bot_routine(job: Job, routine_id: UUID) -> Routine:
        routine = store.get_routine(routine_id)
        if routine is None:
            raise HTTPException(status_code=404, detail="routine not found")
        if routine.created_by != "bot":
            raise HTTPException(status_code=403, detail="the owner's routines are changed only by the owner")
        if routine.bot_id != job.bot_id:
            owner = store.get_bot(routine.bot_id)
            caller = store.get_bot(job.bot_id)
            if owner is None or caller is None or not caller.app_admin:
                raise HTTPException(status_code=403, detail="a bot changes only the routines it made itself")
            _admin(job, owner)
        return routine

    def _routine_gap(cron: str) -> None:
        gap = min_gap(cron, datetime.now(UTC))
        if gap is not None and gap < BOT_ROUTINE_MIN_GAP:
            raise HTTPException(status_code=400, detail="a bot's routine must fire at most every 15 minutes")

    @app.get("/v1/agent/routines")
    async def agent_list_routines(bot: str | None = None, job: Job = Depends(agent_scope)) -> list[Routine]:  # noqa: B008
        """Your routines; an app admin (0.12.26) may list another bot's with ``bot=<name or id>``."""
        if bot:
            target = _target_bot(bot)
            if target.id != job.bot_id:
                _admin(job, target)
                return [r for r in store.list_routines(target.id) if r.created_by == "bot"]
        return store.list_routines(job.bot_id)

    @app.post("/v1/agent/routines", response_model=Routine, status_code=201)
    async def agent_create_routine(body: AgentRoutine, job: Job = Depends(agent_scope)) -> Routine:  # noqa: B008
        _routine_gap(body.cron)
        owner = job.bot_id
        if body.for_bot:
            target = _target_bot(body.for_bot)
            if target.id != job.bot_id:
                _admin(job, target)  # 0.12.26: an app admin makes a routine for another bot (still "made by bot")
                owner = target.id
        try:
            return store.create_routine(owner, body.cron, body.prompt, body.enabled, name=body.name,
                                        created_by="bot", max_bot_routines=MAX_BOT_ROUTINES)
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    @app.patch("/v1/agent/routines/{routine_id}", response_model=Routine)
    async def agent_update_routine(routine_id: UUID, body: AgentRoutineUpdate, job: Job = Depends(agent_scope)) -> Routine:  # noqa: B008
        _bot_routine(job, routine_id)
        fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        if "cron" in fields:
            _routine_gap(fields["cron"])
        return store.update_routine(routine_id, **fields)

    @app.delete("/v1/agent/routines/{routine_id}", status_code=204)
    async def agent_delete_routine(routine_id: UUID, job: Job = Depends(agent_scope)) -> None:  # noqa: B008
        _bot_routine(job, routine_id)
        store.delete_routine(routine_id)

    @app.post("/v1/bots/{bot_ref}/message-agent")
    async def message_agent(bot_ref: str, body: AgentMessageCreate, binding: Job | None = Depends(agent_auth)) -> dict:  # noqa: B008
        sender = store.find_bot(bot_ref)
        target = store.find_bot(body.to)
        if not sender or not target:
            raise HTTPException(status_code=404, detail="bot not found")
        if binding and binding.bot_id != sender.id:
            raise HTTPException(status_code=403, detail="token is bound to another bot")
        blocked = _steer_blocked(target)
        try:
            _, _, item = scheduler.turns.post_agent_message(
                sender, target, body.body, str(binding.id) if binding else None, mode=body.mode,
                can_steer=lambda job: blocked(job) is None)
        except TurnError as exc:
            status = 429 if "limit" in str(exc) or "hop" in str(exc) else 400
            # 0.12.39: a refused send stays visible in the sender's chat after its turn ends
            with contextlib.suppress(Exception):
                store.add_message(sender.id, "system", f"Message to {target.name} not sent: {str(exc)[:200]}.",
                                  source="inbox")
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        if item.get("delivery") == "steer":
            item["note"] = (f"Delivered to {target.name} while it works; it reads it within its current task and "
                            "answers with a message to you if you asked something (it appears in your chat).")
            return item
        if body.mode == "steer" and not target.bot_steer:
            item["note"] = f"{target.name} takes messages from bots as turns of their own, so yours was queued."
        if body.wait and binding:
            prefix = (item["note"] + " ") if item.get("note") else ""
            item["note"] = prefix + ("Queued asynchronously: agent turns cannot wait for another worker; "
                                     "the reply will appear in chat and wake you. Waiting for it is not a blocker: finish "
                                     "your turn (BOT-OFFICE-STATUS: done, or waiting_on_bot on a task).")
            return item
        if body.wait:
            deadline = asyncio.get_running_loop().time() + body.timeout
            while item["status"] in ("queued", "running") and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(min(0.1, max(0, deadline - asyncio.get_running_loop().time())))
                item = store.get_agent_message(item["id"])
        return item

    @app.post("/v1/bridge/grok/send")
    async def bridge_grok_send(body: BridgeSend, binding: Job | None = Depends(agent_auth)) -> dict:  # noqa: B008
        """Send to a Grok Bot over the webhook. The controller holds the webhook key; a job's
        token always sends as its own bot (``from`` is for the master token only)."""
        if binding:
            bot = store.get_bot(binding.bot_id) if binding.bot_id else None
            if not bot:
                raise HTTPException(status_code=403, detail="token is not bound to a bot")
            if body.from_bot and body.from_bot.strip() not in (bot.name, str(bot.id)):
                raise HTTPException(status_code=403, detail="--from is only allowed with the master token")
            sender = bot.name
        else:
            sender = (body.from_bot or "").strip() or None
        def not_sent(why: str) -> None:
            # 0.12.39: a send that did not go out stays visible in the sending bot's chat, not only in
            # the tool output of a turn that may already be over
            if binding and binding.bot_id:
                with contextlib.suppress(Exception):
                    store.add_message(binding.bot_id, "system",
                                      f"Grok send to {body.to.strip()[:80]} ({body.kind}) was not delivered: {why}.",
                                      source="inbox")

        try:
            result = await asyncio.to_thread(
                grok_bridge.send_to_grok, to=body.to, kind=body.kind, text=body.text, thread_id=body.thread_id,
                from_bot=sender, data_dir=settings.control_dir, environ={},
            )
        except grok_bridge.DeskError as exc:
            status = 400 if str(exc).startswith(("kind must", "to and text")) else 502
            code = re.search(r"\bHTTP (\d{3})\b", str(exc))
            not_sent(f"the webhook answered HTTP {code.group(1)}" if code else
                     str(exc)[:120] if status == 400 else "the webhook refused it or could not be reached")
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except Exception:
            not_sent("the webhook could not be reached")
            raise
        if str(result).startswith("dropped") and body.kind.strip().lower() not in grok_bridge.DROPPED_KINDS:
            not_sent(str(result)[:160])  # an ack is never sent, by design: no line for it
        return {"result": result}

    @app.post("/v1/bridge/duet/send")
    async def bridge_duet_send(body: DuetSend, binding: Job | None = Depends(agent_auth),  # noqa: B008
                               credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:  # noqa: B008
        """Post a message to Claude Duet (a review on the mailbox pull request). A job's token always sends as
        its own bot, and only when the mailbox config allows that bot (default_bot, senders); ``from`` (any bot)
        is for the master token only."""
        if binding:
            bot = store.get_bot(binding.bot_id) if binding.bot_id else None
            if not bot:
                raise HTTPException(status_code=403, detail="token is not bound to a bot")
            if body.from_bot and body.from_bot.strip() not in (bot.name, str(bot.id)):
                raise HTTPException(status_code=403, detail="--from is only allowed with the master token")
        elif body.from_bot and body.from_bot.strip():
            bot = store.find_bot(body.from_bot.strip())
            if not bot:
                raise HTTPException(status_code=404, detail="--from names no registered bot")
        else:
            bot = None
        job_alive = None
        if binding is not None and credentials is not None:
            token, job_id = credentials.credentials, str(binding.id)

            def job_alive() -> bool:  # asked again under the send lock and right before the POST
                current = store.agent_token_job(token)
                return current is not None and str(current.id) == job_id
        try:
            return await asyncio.to_thread(duet.send, text=body.text, bot=bot, via_job=binding is not None,
                                           to=body.to, kind=body.kind, thread=body.thread, reply_to=body.reply_to,
                                           msg_id=body.msg_id, job_alive=job_alive)
        except duet_bridge.DuetError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=502, detail="GitHub is unreachable") from exc

    @app.get("/v1/bridge/duet/status", dependencies=[Depends(auth)])
    async def bridge_duet_status() -> dict:
        return await asyncio.to_thread(duet.report)

    @app.post("/v1/bridge/inbox", dependencies=[Depends(auth)])
    async def bridge_inbox(body: BridgeInbox) -> dict:
        """An inbound bridge message (``desk inbox post``, master token only)."""
        try:
            status, detail = grok_bridge.post_inbox(to=body.to, text=body.text, sender=body.sender,
                                                    thread_id=body.thread_id, hop=body.hop, turns=scheduler.turns,
                                                    priority=body.priority)
        except grok_bridge.DeskError as exc:
            raise HTTPException(status_code=404 if str(exc).startswith("bot not found") else 400,
                                detail=str(exc)) from exc
        return {"status": status, "detail": detail}

    @app.get("/v1/agent-messages", dependencies=[Depends(auth)])
    async def list_agent_messages(bot: str | None = None) -> list[dict]:
        if bot:
            found = store.find_bot(bot)
            if not found:
                raise HTTPException(status_code=404, detail="bot not found")
            bot = str(found.id)
        return store.list_agent_messages(bot)

    @app.get("/v1/agent-messages/{message_id}")
    async def get_agent_message(message_id: str, binding: Job | None = Depends(agent_auth)) -> dict:  # noqa: B008
        item = store.get_agent_message(message_id)
        if not item:
            raise HTTPException(status_code=404, detail="message not found")
        if binding and item["from_bot"] != str(binding.bot_id):
            raise HTTPException(status_code=403, detail="message belongs to another bot")
        return item

    # ---- teammate tools (E7-1): job token only (agent_scope, never the master token); see team.py ----------
    def _team_db(job: Job, fn):
        try:
            with closing(store._connect()) as db:
                return fn(db)
        except team.TeamError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.get("/v1/agent/team")
    async def agent_team_list(job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        return _team_db(job, lambda db: team.team_list(db, str(job.bot_id)))

    @app.get("/v1/agent/team/{bot}/status")
    async def agent_team_status(bot: str, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        def run(db):
            reader, target = team.resolve(db, str(job.bot_id), bot)
            return team.team_status(db, reader, target), target["id"]
        result, target_id = _team_db(job, run)
        team.log_read(store, job, "team_status", target_id, 1)
        return result

    @app.get("/v1/agent/team/{bot}/transcript")
    async def agent_team_transcript(bot: str, limit: int = Query(default=20, ge=1, le=team.TRANSCRIPT_LIMIT),
                                    since: str | None = Query(default=None, max_length=64),
                                    job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        def run(db):
            _, target = team.resolve(db, str(job.bot_id), bot)
            return team.team_transcript(db, target, limit, since), target["id"]
        result, target_id = _team_db(job, run)
        team.log_read(store, job, "team_read", target_id, result["count"])
        return result

    async def _team_send(job: Job, body: AgentMessageCreate) -> dict:
        item = await message_agent(str(job.bot_id), AgentMessageCreate(to=body.to, body=body.body, mode=body.mode), job)
        return {key: item[key] for key in ("id", "status", "mode", "delivery", "note", "hop") if key in item}

    @app.post("/v1/agent/team/message")
    async def agent_team_message(body: AgentMessageCreate, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        return await _team_send(job, body)

    @app.post("/v1/agent/team/ask")
    async def agent_team_ask(body: TeamAsk, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        """Ask and wait for the answer, bounded and lane-safe (team.py explains the guard); otherwise the
        message is sent as usual and ``pending`` says to end the turn: the reply wakes the asker."""
        target = store.find_bot(body.to)
        if not target:
            raise HTTPException(status_code=404, detail="bot not found")
        lanes = settings.run_lanes
        reserved = team.reserve_wait(store, job, target.id, lanes, body.timeout_s)
        try:
            item = await _team_send(job, body)
            delivery = item.get("delivery")
            waiting = reserved and (delivery == "steer" or (
                delivery == "start" and team.lane_free(store, lanes, str(store.get_agent_message(item["id"])["job_id"]))))
            found = None
            if waiting:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + body.timeout_s
                while True:
                    found = team.find_reply(store, item["id"])
                    current = store.get(job.id)
                    if found or loop.time() >= deadline or current is None or str(current.status) != "running":
                        break
                    if team.target_stalled(store, lanes, item["id"]):  # still queued and no lane free: give up, pending
                        waiting = False
                        break
                    await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))
            if found:
                store.claim_reply_wake(item["id"], "answered in turn")
                return {"id": item["id"], "status": found["status"], "delivery": delivery, "reply": found["reply"],
                        "untrusted": True}
            why = ("it did not answer within the time limit" if waiting else
                   "waiting here could starve the run lanes or the bot is not free" if not reserved or delivery != "steer"
                   else "no answer yet")
            return {"id": item["id"], "status": "pending", "delivery": delivery, "note":
                    f"No answer in this turn ({why}). Do not wait or poll: end your turn with "
                    "BOT-OFFICE-STATUS: waiting_on_bot; the reply will wake you."}
        finally:
            if reserved:
                team.release_wait(store, job)

    @app.get("/v1/agent/recall")
    async def agent_recall(q: str = Query(min_length=1, max_length=recall.QUERY_CHARS), scope: str = Query(default="self"),
                           limit: int = Query(default=10, ge=1, le=recall.MAX_HITS),
                           job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        try:
            return await recall.run_async(store, job, q, scope, limit)  # off the event loop, admission-bounded
        except recall.RecallError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.post(
        "/v1/bots/{bot_id}/messages",
        response_model=MessagePosted,
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def post_message(bot_id: UUID, body: MessageCreate) -> MessagePosted:
        bot = _bot(bot_id)
        try:
            async with attachment_global_lock, attachment_lock(bot_id):
                _bot(bot_id)
                ids = list(dict.fromkeys(body.attachments))
                await _check_attachments(bot_id, ids)
                message, job, delivery, note = _post_owner_message_ex(
                    bot, body.text, reason=body.reason, attachments=ids, queue=body.mode == "queue")
        except TurnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return MessagePosted(message=message, job=job, delivery=delivery, delivery_note=note)

    async def _check_attachments(bot_id: UUID, ids: list[str]) -> None:
        for attachment_id in ids:
            try:
                if attachments.is_file_id(attachment_id):
                    await asyncio.to_thread(attachments.size, settings.data_dir, bot_id, attachment_id)  # lstat only
                else:
                    data = await asyncio.to_thread(attachments.read, settings.data_dir, bot_id, attachment_id)
                    mime = _sniff_image(data)
                    if not mime or attachments.EXTENSIONS[mime] != attachment_id.rsplit(".", 1)[-1]:
                        raise ValueError("invalid image")
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail="attachment not found") from exc

    def _edit_failed(exc: EditError, cancelled: list[str]) -> JSONResponse:
        detail = exc.detail
        if cancelled:
            detail = f"The queued/running reply was cancelled, but the chat was not changed: {detail}"
        return JSONResponse({"detail": detail, "jobs": exc.jobs, "cancelled": cancelled}, status_code=exc.status)

    @app.post(
        "/v1/bots/{bot_id}/messages/{message_id}/edit",
        response_model=MessagePosted,
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def edit_message(bot_id: UUID, message_id: int, body: MessageEdit):
        """Edit one of the owner's messages and rewind: that message and everything after
        it are archived (never deleted), the new text is queued, and the bot's next turn
        starts a fresh provider session seeded with the remaining chat.

        Everything that can fail without side effects (ownership, editability, provider and
        reason, images, seed size) is validated before any job is cancelled. Cancellation
        waits happen without holding any lock; the final commit revalidates under the
        write lock and reports, if it still fails, that jobs were cancelled.
        """
        bot = _bot(bot_id)
        try:
            original = store.get_message(message_id)
            store.check_edit(bot_id, message_id, body.text)
            scheduler.turns._validate(bot.default_provider, body.reason, bot)
        except EditError as exc:
            return _edit_failed(exc, [])
        except TurnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        assert original is not None
        ids = list(dict.fromkeys(original.attachments if body.attachments is None else body.attachments))
        await _check_attachments(bot_id, ids)
        cancelled: list[str] = []
        pending = store.pending_jobs(bot_id)
        if pending and not body.cancel_running:
            return JSONResponse(
                {"detail": "A reply is queued or running for this bot. Cancel it and rewind?",
                 "jobs": [str(job.id) for job in pending]},
                status_code=409,
            )
        with scheduler.defer_steers_for_edit(pending):
            if pending:
                signalled: set[str] = set()
                for job in pending:
                    cancelled.append(str(job.id))
                    if job.status == JobStatus.RUNNING:
                        scheduler.cancel(job, EDIT_CANCEL_REASON)
                        signalled.add(str(job.id))
                    else:
                        store.cancel(job.id, queue_steers=False, reason=EDIT_CANCEL_REASON)  # defer_steers_for_edit handles them
                deadline = asyncio.get_running_loop().time() + EDIT_CANCEL_WAIT_SECONDS
                while waiting := [job for job in store.pending_jobs(bot_id) if str(job.id) in cancelled]:
                    if asyncio.get_running_loop().time() >= deadline:
                        still = [str(job.id) for job in waiting]
                        return _edit_failed(EditError(409, "a job did not stop in time; try again", still), cancelled)
                    for job in waiting:  # a queued job may have been claimed meanwhile
                        if job.status == JobStatus.RUNNING and str(job.id) not in signalled:
                            scheduler.cancel(job, EDIT_CANCEL_REASON)
                            signalled.add(str(job.id))
                    await asyncio.sleep(0.05)
            try:
                async with attachment_global_lock, attachment_lock(bot_id):
                    bot = _bot(bot_id)
                    await _check_attachments(bot_id, ids)
                    message, job = scheduler.turns.edit_message(bot, message_id, body.text, reason=body.reason, attachments=ids)
            except EditError as exc:
                return _edit_failed(exc, cancelled)
            except TurnError as exc:
                return _edit_failed(EditError(400, str(exc)), cancelled)
            except HTTPException as exc:
                if not cancelled:
                    raise
                return _edit_failed(EditError(exc.status_code, str(exc.detail)), cancelled)
            with contextlib.suppress(OSError):
                scheduler.reconcile_session(bot_id)  # otherwise done before the bot's next job
            return MessagePosted(message=message, job=job)

    @app.get("/v1/bots/{bot_id}/messages", response_model=list[Message], dependencies=[Depends(auth)])
    async def list_messages(bot_id: UUID, pending: bool = False, archived: bool = False) -> list[Message]:
        """Chat history. With ``?pending=true`` also one in-flight placeholder per queued/running job.

        A placeholder has ``pending=true``, a negative ``id`` and the live draft as
        ``text``; attach to ``/v1/jobs/{job_id}/events`` to replay and continue it.
        The default list is unchanged so older clients never see placeholders.
        """
        _bot(bot_id)
        messages = store.list_messages(bot_id, include_archived=archived)
        messages.extend(_placeholders(bot_id, pending))
        return messages

    def _placeholders(bot_id: UUID, pending: bool) -> list[Message]:
        out: list[Message] = []
        for index, job in enumerate(store.pending_jobs(bot_id) if pending else (), start=1):
            out.append(
                Message(
                    id=-index,
                    bot_id=bot_id,
                    role="assistant",
                    source=job.source,
                    text=job.draft_text or "",
                    job_id=job.id,
                    created_at=job.started_at or job.created_at,
                    pending=True,
                    job_status=job.status.value,
                    not_before=job.not_before,
                    author="bot",
                    continuing=(f"{job.continue_count} of {baseline.auto_continue(store)['max']}"
                                if job.continue_count else None),
                )
            )
        return out

    @app.get("/v1/bots/{bot_id}/history", response_model=MessagePage, dependencies=[Depends(auth)])
    async def history(
        bot_id: UUID, limit: int = Query(50, ge=1, le=500), before: int | None = Query(None, ge=1),
        after: int | None = Query(None, ge=0), around: int | None = Query(None, ge=1),
        pending: bool = False, archived: bool = False,
    ) -> MessagePage:
        """One page of the chat, oldest first, by message id (keyset: nothing duplicates or skips while new
        messages arrive). No cursor: the newest *limit*. ``before``: just older than that id. ``after``: just
        newer. ``around``: centred on it. In-flight placeholders (``pending=true``) come only with a page that
        reaches the newest message. The unpaged ``/messages`` route is unchanged."""
        _bot(bot_id)
        if sum(x is not None for x in (before, after, around)) > 1:
            raise HTTPException(422, "use only one of before, after, around")
        messages, has_older, has_newer = store.history_page(
            bot_id, limit, before=before, after=after, around=around, include_archived=archived)
        if not has_newer and before is None:
            messages.extend(_placeholders(bot_id, pending))
        return MessagePage(messages=messages, has_older=has_older, has_newer=has_newer)

    @app.get("/v1/search/messages", response_model=MessageSearch, dependencies=[Depends(auth)])
    async def search_messages(
        q: str = Query(..., min_length=1, max_length=200), bot: UUID | None = None,
        limit: int = Query(30, ge=1, le=100), before: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0, le=100000),
    ) -> MessageSearch:
        """Search chat messages, best match first (FTS5 bm25, newest first on a tie): one bot (``bot=``) or all.
        Also searched: names of attached files and Bot Office's own status lines (``hidden`` is true on those; they
        are not shown in the chat). ``offset`` pages. ``snippet`` wraps each match in U+0002 ... U+0003.
        Archived (rewound) messages are not searched."""
        if bot is not None:
            _bot(bot)
        hits, more = store.search_messages(q, bot_id=bot, limit=limit, before=before, offset=offset)
        return MessageSearch(hits=hits, has_more=more)

    @app.post(
        "/v1/bots/{bot_id}/inbox",
        response_model=MessagePosted,
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def post_inbox(bot_id: UUID, body: InboxCreate) -> MessagePosted:
        bot = _bot(bot_id)
        sender = _bot(body.from_bot_id) if body.from_bot_id else None
        try:
            if sender:
                raise TurnError("originating job required; use message-agent")
            else:
                message, job = scheduler.turns.post_message(bot, body.text, source="inbox")
        except TurnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return MessagePosted(message=message, job=job)

    # --- voice (optional; see voice/__init__.py) ---

    stt_requests: list[float] = []
    tts_requests: list[float] = []
    speakable_requests: list[float] = []

    def _voice_prefs() -> VoicePrefs:
        raw = store.get_meta("voice_prefs")
        try:
            return VoicePrefs.model_validate_json(raw) if raw else VoicePrefs()
        except ValueError:
            return VoicePrefs()

    def _voice_error(exc: VoiceError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    def _rate_limit(log: list[float], limit: int) -> None:
        cutoff = time.monotonic() - 60
        log[:] = [stamp for stamp in log if stamp > cutoff]
        if len(log) >= limit:
            raise HTTPException(status_code=429, detail="too many voice requests, wait a minute")
        log.append(time.monotonic())

    @app.get("/v1/voice", dependencies=[Depends(auth)])
    async def voice_status() -> dict:
        return {**await voice.status(), "prefs": _voice_prefs().model_dump()}

    @app.get("/v1/voice/prefs", dependencies=[Depends(auth)])
    async def get_voice_prefs() -> dict:
        """Just the preferences (cheap; the GUI re-checks auto_read before auto-playing)."""
        return _voice_prefs().model_dump()

    @app.put("/v1/voice/prefs", dependencies=[Depends(auth)])
    async def put_voice_prefs(body: VoicePrefsBody) -> dict:
        prefs = _voice_prefs().model_copy(update=body.model_dump(exclude_none=True))
        store.set_meta("voice_prefs", prefs.model_dump_json())
        return prefs.model_dump()

    @app.post("/v1/voice/transcribe", dependencies=[Depends(auth)])
    async def transcribe(request: Request) -> dict:
        """16 kHz mono 16-bit PCM WAV in, text out. Audio stays in memory and is never
        stored or logged; the browser does all decoding of compressed audio."""
        if not (await voice.stt_status())["available"]:
            raise HTTPException(status_code=503, detail="speech-to-text is not installed")
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in ("audio/wav", "audio/x-wav", "audio/wave"):
            raise HTTPException(status_code=415, detail="send audio/wav (16 kHz mono 16-bit PCM)")
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > STT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="recording too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
        data = bytearray()
        try:
            async with asyncio.timeout(STT_READ_SECONDS):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > STT_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="recording too large")
                    data.extend(chunk)
        except TimeoutError as exc:
            raise HTTPException(status_code=408, detail="the upload took too long") from exc
        try:
            pcm = parse_pcm16_mono(bytes(data))
        except WavError as exc:
            raise HTTPException(status_code=415, detail=f"invalid WAV: {exc}") from exc
        seconds = len(pcm) / 2 / 16_000
        if not STT_MIN_SECONDS <= seconds <= STT_MAX_SECONDS:
            raise HTTPException(status_code=422, detail="recording must be 0.3 to 120 seconds long")
        _rate_limit(stt_requests, STT_PER_MINUTE)  # only valid recordings use up the budget
        try:
            return await voice.transcribe(pcm)
        except VoiceError as exc:
            raise _voice_error(exc) from exc

    @app.post("/v1/voice/speakable", dependencies=[Depends(auth)])
    async def speakable(body: SpeakableRequest) -> dict:
        """A message's Markdown as plain-text chunks (<= 400 chars) for /v1/voice/speak."""
        _rate_limit(speakable_requests, SPEAKABLE_PER_MINUTE)
        return {"chunks": await asyncio.to_thread(speech_chunks, body.text)}

    @app.post("/v1/voice/speak", dependencies=[Depends(auth)])
    async def speak(body: SpeakRequest) -> Response:
        """One chunk of plain text in, 24 kHz mono WAV out; nothing is stored on disk."""
        if not (await voice.tts_status())["available"]:
            raise HTTPException(status_code=503, detail="text-to-speech is not installed")
        _rate_limit(tts_requests, TTS_PER_MINUTE)
        try:
            wav = await voice.speak(body.text, body.voice, body.speed)
        except VoiceError as exc:
            raise _voice_error(exc) from exc
        return Response(wav, media_type="audio/wav", headers=AUDIO_HEADERS)

    @app.post(
        "/v1/bots/{bot_id}/routines",
        response_model=Routine,
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def create_routine(bot_id: UUID, body: RoutineCreate) -> Routine:
        _bot(bot_id)
        return store.create_routine(bot_id, body.cron, body.prompt, body.enabled, name=body.name,
                                    catch_up=body.catch_up)

    @app.get("/v1/bots/{bot_id}/routines", response_model=list[Routine], dependencies=[Depends(auth)])
    async def list_routines(bot_id: UUID) -> list[Routine]:
        _bot(bot_id)
        return store.list_routines(bot_id)

    @app.patch("/v1/routines/{routine_id}", response_model=Routine, dependencies=[Depends(auth)])
    async def update_routine(routine_id: UUID, body: RoutineUpdate) -> Routine:
        if not store.get_routine(routine_id):
            raise HTTPException(status_code=404, detail="routine not found")
        fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        try:
            routine = store.update_routine(routine_id, **fields)
        except ValueError as exc:
            status = 404 if "not found" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        assert routine is not None
        return routine

    @app.delete("/v1/routines/{routine_id}", status_code=204, dependencies=[Depends(auth)])
    async def delete_routine(routine_id: UUID) -> None:
        if not store.delete_routine(routine_id):
            raise HTTPException(status_code=404, detail="routine not found")

    # --- 0.10.0 assignments (master token) ---

    @app.post("/v1/bots/{bot_id}/assignments", response_model=Assignment, status_code=201,
              dependencies=[Depends(auth)])
    async def create_assignment(bot_id: UUID, body: AssignmentCreate) -> Assignment:
        _bot(bot_id)
        return _assignment_call(assignments.create, bot_id, body.model_dump())

    @app.get("/v1/bots/{bot_id}/assignments", response_model=list[Assignment], dependencies=[Depends(auth)])
    async def list_assignments(bot_id: UUID, include_closed: bool = True) -> list[Assignment]:
        _bot(bot_id)
        return assignments.list(bot_id, include_closed=include_closed)

    @app.get("/v1/assignments/{assignment_id}", dependencies=[Depends(auth)])
    async def get_assignment(assignment_id: UUID) -> dict:
        item = assignments.get(assignment_id)
        if not item:
            raise HTTPException(status_code=404, detail="assignment not found")
        return {**item.model_dump(mode="json"), "events": assignments.events(assignment_id)}

    @app.patch("/v1/assignments/{assignment_id}", response_model=Assignment, dependencies=[Depends(auth)])
    async def update_assignment(assignment_id: UUID, body: AssignmentUpdate) -> Assignment:
        fields = body.model_dump(exclude_unset=True)
        for key in ("title", "goal", "done_when", "budget_turns", "budget_minutes", "verify", "escalate"):
            if key in fields and fields[key] is None:
                del fields[key]
        return _assignment_call(assignments.update, assignment_id, fields)

    @app.post("/v1/assignments/{assignment_id}/start", response_model=Assignment, dependencies=[Depends(auth)])
    async def start_assignment(assignment_id: UUID, body: AssignmentStart | None = None) -> Assignment:
        body = body or AssignmentStart()
        return _assignment_call(assignments.start, assignment_id, body.add_turns, body.add_minutes)

    # --- 0.11.4 task lists (master token): assignments with version, stage and actor ---

    @app.exception_handler(AssignmentError)
    async def assignment_error(request: Request, exc: AssignmentError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail, **exc.extra}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI's 422, except that a non-finite number in the input (``Infinity``/``NaN``, which
        Python's json accepts) is echoed as text: JSON cannot carry it and the default answer was a 500."""
        def finite(value):
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            if isinstance(value, dict):
                return {key: finite(item) for key, item in value.items()}
            if isinstance(value, list | tuple):
                return [finite(item) for item in value]
            return value
        if request.url.path.startswith("/v1/bridge/duet/"):
            # outbound Duet text is checked for secrets only after validation: never echo it
            errors = [{key: value for key, value in error.items() if key not in ("input", "ctx")}
                      for error in exc.errors()]
            return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)
        errors = [{**error, "input": finite(error.get("input"))} for error in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    def _stages(value: str | None) -> list[str] | None:
        if not value:
            return None
        stages = [part.strip() for part in value.split(",") if part.strip()]
        unknown = set(stages) - TASK_STAGES
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown stage: {', '.join(sorted(unknown))}")
        return stages

    @app.get("/v1/tasks/settings", dependencies=[Depends(auth)])
    async def task_settings() -> dict:
        """Read-only task-list settings for the GUI: its label, the owner's task-list
        settings as they are set now (environment), the stages and priorities."""
        return {"label": tasks_label(), "bot_drop_owner_tasks": drop_rule(),
                "cross_bot_subtask_start": choice(CROSS_BOT_SUBTASK_START),
                "drop_cancels_turns": choice(DROP_CANCELS_TURNS),
                "bot_edit_owner_tasks": choice(BOT_EDIT_OWNER_TASKS),
                "form_tasks_start": choice(FORM_TASKS_START),
                "default_priority": choice(TASK_DEFAULT_PRIORITY),
                "cross_bot_max_priority": choice(CROSS_BOT_MAX_PRIORITY),
                "task_writes_per_hour": _limit(TASK_WRITES_PER_HOUR),
                "priorities": list(PRIORITIES),
                "stages": ["not_started", "in_progress", "completed", "dropped"]}

    @app.get("/v1/tasks", dependencies=[Depends(auth)])
    async def list_tasks(bot: str | None = None, stage: str | None = None, include_dropped: bool = False,
                         limit: int = Query(default=200, ge=1, le=1000), counts: bool = False) -> Any:
        """The board (every bot's tasks) or one bot's (``bot``), filtered by stage; ``counts=1``
        returns only the counts per bot (the sidebar chip, one request for all bots)."""
        if counts:
            return {"bots": assignments.counts()}
        target = None
        if bot:
            found = store.find_bot(bot)
            if not found:
                raise HTTPException(status_code=404, detail="bot not found")
            target = found.id
        stages = _stages(stage)
        return assignments.tasks(target, stages=stages, include_dropped=include_dropped or bool(
            stages and "dropped" in stages), limit=limit)

    @app.post("/v1/tasks/reorder", response_model=list[Task], dependencies=[Depends(auth)])
    async def reorder_tasks(body: TaskReorder) -> list[Task]:
        bot = store.find_bot(body.bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="bot not found")
        return assignments.reorder(bot.id, body.order, actor="user", versions=body.versions)

    @app.get("/v1/bots/{bot_ref}/tasks", response_model=list[Task], dependencies=[Depends(auth)])
    async def list_bot_tasks(bot_ref: str, include_closed: bool = True) -> list[Task]:
        bot = store.find_bot(bot_ref)
        if not bot:
            raise HTTPException(status_code=404, detail="bot not found")
        return assignments.tasks(bot.id, include_closed=include_closed)

    @app.post("/v1/bots/{bot_ref}/tasks", response_model=Task, status_code=201, dependencies=[Depends(auth)])
    async def create_task(bot_ref: str, body: TaskCreate) -> Task:
        bot = store.find_bot(bot_ref)
        if not bot:
            raise HTTPException(status_code=404, detail="bot not found")
        fields = body.model_dump()
        if fields["start"] is None:
            # a task from the form starts on its own like a chat task, unless the owner unticks it
            fields["start"] = choice(FORM_TASKS_START) == "auto"
        return assignments.task(assignments.create(bot.id, fields, origin="form").id)

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(auth)])
    async def get_task(task_id: UUID) -> dict:
        item = assignments.task(task_id)
        return {**item.model_dump(mode="json"), "events": assignments.events(task_id),
                "children": [child.model_dump(mode="json") for child in assignments.children(task_id)]}

    @app.get("/v1/tasks/{task_id}/events", dependencies=[Depends(auth)])
    async def task_events(task_id: UUID, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
        assignments.task(task_id)
        return assignments.events(task_id, limit)

    @app.patch("/v1/tasks/{task_id}", response_model=Task, dependencies=[Depends(auth)])
    async def update_task(task_id: UUID, body: TaskUpdate) -> Task:
        fields = body.model_dump(exclude_unset=True, exclude={"version"})
        for key in ("title", "goal", "done_when", "budget_turns", "budget_minutes", "verify", "escalate"):
            if key in fields and fields[key] is None:
                del fields[key]
        assignments.update(task_id, fields, expected_version=body.version)
        return assignments.task(task_id)

    @app.delete("/v1/tasks/{task_id}", status_code=204, dependencies=[Depends(auth)])
    async def delete_task(task_id: UUID) -> None:
        assignments.delete(task_id)

    @app.post("/v1/tasks/{task_id}/start", response_model=Task, dependencies=[Depends(auth)])
    async def start_task(task_id: UUID, body: AssignmentStart | None = None) -> Task:
        body = body or AssignmentStart()
        assignments.start(task_id, body.add_turns, body.add_minutes)
        return assignments.task(task_id)

    @app.post("/v1/tasks/{task_id}/stop", response_model=Task, dependencies=[Depends(auth)])
    async def stop_task(task_id: UUID) -> Task:
        _cancel_jobs(assignments.stop(task_id))
        return assignments.task(task_id)

    @app.post("/v1/tasks/{task_id}/reply", dependencies=[Depends(auth)])
    async def reply_task(task_id: UUID, body: AssignmentReply) -> dict:
        job_id = assignments.reply(task_id, body.text)
        return {"job_id": job_id, "task": assignments.task(task_id).model_dump(mode="json")}

    @app.post("/v1/tasks/{task_id}/note", status_code=204, dependencies=[Depends(auth)])
    async def note_task(task_id: UUID, body: TaskNote) -> None:
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="note is empty")
        assignments.add_note(task_id, None, None, body.text.strip())

    # --- 0.10.0 Needs-you (computed, model-free) ---

    switch_cache: dict[str, tuple[float, list[dict]]] = {}

    async def _switch_options(current: str, fresh: bool = False) -> list[dict]:
        """Providers a parked job may switch to: enabled, signed in and different from *current*.
        Cached for SWITCH_OPTIONS_TTL seconds: Needs-you is polled every few seconds per tab and each
        login check spawns a CLI (through sudo when isolated). The switch itself checks fresh."""
        cached = switch_cache.get(current)
        if not fresh and cached and time.monotonic() - cached[0] < SWITCH_OPTIONS_TTL:
            return [dict(item) for item in cached[1]]
        infos = await asyncio.gather(*(_provider_info(name) for name in ENGINES if name != current))
        options = [{"provider": info.name, "label": ENGINE_LABELS.get(info.name, info.name)}
                   for info in infos if info.enabled and info.login_status == "ok"]
        switch_cache[current] = (time.monotonic(), options)
        return [dict(item) for item in options]

    @app.get("/v1/needs-you", dependencies=[Depends(auth)])
    async def get_needs_you() -> list[dict]:
        items = needs_you.needs_you(store)
        options: dict[str, list[dict]] = {}
        for item in items:
            if item["kind"] == "usage_limit":
                if item["provider"] not in options:
                    options[item["provider"]] = await _switch_options(item["provider"])
                item["options"] = options[item["provider"]]
        return items

    @app.get("/v1/jobs/{job_id}/switch-options", dependencies=[Depends(auth)])
    async def get_switch_options(job_id: UUID) -> list[dict]:
        """#10: the [Retry on <provider>] choices for a parked job's notice."""
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return await _switch_options(job.provider)

    @app.post("/v1/jobs/{job_id}/switch-provider", response_model=Job, dependencies=[Depends(auth)])
    async def switch_provider(job_id: UUID, body: SwitchProvider) -> Job:
        """#10: run a job parked on a usage limit on another provider, now. Owner only (master
        token); never automatic. Only that job changes (provider, the target's default model and
        effort); the bot's defaults stay. The next run gets the model-switch handoff."""
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not (job.status == JobStatus.QUEUED and job.failure_kind == "usage_limit" and job.not_before):
            raise HTTPException(status_code=409, detail="only a job waiting on a usage limit can switch provider")
        target = body.provider
        if target == job.provider:
            raise HTTPException(status_code=400, detail=f"the job already runs on {target}")
        if target not in ENGINES or target not in {item["provider"] for item in await _switch_options(job.provider, fresh=True)}:
            raise HTTPException(status_code=400, detail=f"{target} is not enabled and signed in")
        reason = (body.reason or "").strip() or None
        try:
            switched = await asyncio.to_thread(store.switch_provider, job.id, target, default_model(target),
                                               default_effort(target), reason)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if target == "claude":
            scheduler.turns._log_claude_use(switched)
        return switched

    @app.post("/v1/jobs/{job_id}/keep-waiting", dependencies=[Depends(auth)])
    async def keep_waiting(job_id: UUID) -> dict:
        """#10 [Keep waiting]: close the notice; the job keeps its retry time."""
        if not store.get(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        return {"closed": await asyncio.to_thread(store.keep_waiting, job_id)}

    # --- 0.11.0 action rules and approvals ---

    rules = scheduler.rules

    @app.post("/v1/agent/gate")
    async def agent_gate(request: Request, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        """bin/bot-office-gate asks before a tool runs; the job's source and its bot's scheduled_runs
        setting (never its text) decide attended vs the strict unattended profile. The agent token
        is valid only while its job runs. The body is capped before it is parsed (Sec 4)."""
        raw = await _read_capped(request, GATE_BODY_LIMIT, "gate event")
        try:
            body = GateRequest.model_validate_json(raw or b"{}")
        except (ValueError, RecursionError) as exc:
            raise HTTPException(status_code=422, detail="invalid gate event") from exc
        return (await asyncio.to_thread(rules.decide, job, body.event, body.provider[:32])).as_dict()

    # 0.12.4 fix3 #5: the git guard asks GitHub (cached) for a repo's real default branch
    git_guard.default_lookup = github_access.DefaultBranches(lambda: action_rules.github_token(settings))
    git_guard.visibility_lookup = github_access.Visibility(lambda: action_rules.github_token(settings))
    auto_merge.token_source = lambda: action_rules.github_token(settings)  # 0.12.31

    @app.post("/v1/agent/github-ticket")
    async def agent_github_ticket(request: Request, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        """0.12.4 fix3 #2: one-time tickets for one git or gh call (the shim sends its words and
        directory). The controller works out the operation and the GitHub repositories itself and
        issues a ticket per repository only when it allows the call: a push or a gh call the guard
        stops needs the owner's approval first. 403 otherwise; 404 when it holds no token."""
        raw = await _read_capped(request, 64 * 1024, "ticket request")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("argv"), list):
            raise HTTPException(status_code=422, detail="expected argv and cwd")
        if action_rules.github_token(settings) is None:
            raise HTTPException(status_code=404, detail="no GitHub token configured")
        cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else None
        tickets, why = await asyncio.to_thread(rules.issue_ticket, job, body["argv"], cwd)
        if not tickets:
            raise HTTPException(status_code=403, detail=why)
        return {"tickets": tickets, "expires_in": github_access.TICKET_SECONDS}

    @app.post("/v1/agent/github-token")
    async def agent_github_token(request: Request, job: Job = Depends(agent_scope)) -> dict:  # noqa: B008
        """0.12.4 fix3 #2: the GitHub token for exactly one ticketed operation (bin/bot-office-credential
        for git, the gh shim for gh). The agent token alone gets 403: the ticket must be this job's,
        unused, unexpired and, for git, for the repository git names. Each hand-out is logged with the
        operation and repository the ticket was issued for, never the token."""
        raw = await _read_capped(request, 8192, "credential request")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        host = str(body.get("host") or "github.com").lower()
        if host not in action_rules.GITHUB_HOSTS:
            raise HTTPException(status_code=404, detail="no credential for this host")
        token = action_rules.github_token(settings)
        if token is None:
            raise HTTPException(status_code=404, detail="no GitHub token configured")
        path = body.get("path")
        repo = git_guard.normalise_remote(f"https://{host}/{path}") if isinstance(path, str) and path else None
        offered = body.get("tickets") if isinstance(body.get("tickets"), list) else [body.get("ticket")]
        for ticket in offered[:8]:
            used = rules.tickets.redeem(ticket, str(job.id), repo) if isinstance(ticket, str) else None
            if used is not None:
                await asyncio.to_thread(rules.log_credential, job, used)
                return {"token": token}
        raise HTTPException(status_code=403, detail="a ticket for this operation is required")

    @app.get("/v1/rules", dependencies=[Depends(auth)])
    async def list_rules(bot_id: UUID | None = None) -> dict:
        return {"rules": rules.list_rules(bot_id), "presets": {
            "outbound": "git push, gh writes, mail, curl/wget writes to other hosts, desk grok send, desk click/type/key, "
                        "ssh/scp/rsync to hosts, publish/push of packages and images, MCP tools",
            "destructive": "sudo, systemctl, crontab, podman rm/stop/kill, git reset --hard / clean, rm -r outside the "
                           "workspace, writes to ~/.ssh, ~/.config/systemd, shell profiles, /etc, the Bot-Office repo",
            "nested-agent": "another agent CLI (claude, codex, opencode, gemini, cursor-agent, aider) started with its "
                            "permission checks off (--permission-mode bypassPermissions, --dangerously-skip-permissions, "
                            "--yolo, --full-auto, -a never, -s danger-full-access...); your outbound and destructive "
                            "rules also govern it",
            "secrets": "the controller token, database, bridge config, private dir and other bots' dirs (always never)",
            "network": "strict-profile Codex runs get network only with a 'network' allow rule; without one "
                       "they start only if scripts/probe_gate.sh codex proved bot-task reaches Bot-Office "
                       "(else they are refused to Needs-you: bot-task/bot-msg/desk would fail)"},
            "defaults": "strict profile (watcher triggers always; routine, wake-up, assignment and bot-to-bot "
                        "turns only for bots with scheduled_runs 'gated'): outbound and destructive ask; "
                        "everything else, incl. your own chat: allow; bot-msg: allow; secrets: never everywhere. "
                        "0.11.10: the approval classes switched on in Settings ask in every run, after your own "
                        "rules (GET /v1/settings, approvals). 0.12.4: pushes to the default branch, merges, "
                        "force-pushes, deletes (branch, tag, repo, files outside the workspace) and repo "
                        "settings changes ask in every run before any rule: an allow rule cannot open them"}

    @app.post("/v1/rules", status_code=201, dependencies=[Depends(auth)])
    async def add_rule(body: RuleCreate) -> dict:
        if body.bot_id is not None:
            _bot(body.bot_id)
        try:
            return rules.add_rule(bot_id=body.bot_id, effect=body.effect, kind=body.kind, pattern=body.pattern,
                                  scope=body.scope, note=body.note)
        except action_rules.RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/v1/rules/{rule_id}", status_code=204, dependencies=[Depends(auth)])
    async def delete_rule(rule_id: int) -> None:
        if not rules.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail="rule not found")

    # ---- 0.12.39: stall sweep config and runs, rules sync (master token only) ----
    @app.get("/v1/stall-sweep", dependencies=[Depends(auth)])
    async def get_stall_sweep() -> dict:
        return {**stall_sweep.config(store), "hours": settings.stall_sweep_hours,
                "per_hour": settings.stall_sweep_per_hour}

    @app.put("/v1/stall-sweep", dependencies=[Depends(auth)])
    async def put_stall_sweep(body: StallSweepConfig) -> dict:
        try:
            return stall_sweep.update(store, leads=body.leads, exclude=body.exclude)
        except stall_sweep.StallSweepError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/stall-sweep/run", dependencies=[Depends(auth)])
    async def run_stall_sweep() -> dict:
        if scheduler.paused():
            raise HTTPException(status_code=409, detail="paused (data/PAUSE): nothing is woken")
        return await asyncio.to_thread(stall_sweep.sweep, store, scheduler.assignments, settings.stall_sweep_hours,
                                       per_hour=settings.stall_sweep_per_hour, paused=scheduler.paused)

    @app.post("/v1/rules-sync/run", dependencies=[Depends(auth)])
    async def run_rules_sync(all_files: bool = False) -> dict:
        """Import the drop folder now (only new or changed files unless all_files; dedup applies either way)."""
        return await asyncio.to_thread(rules_sync.run, store, scheduler.memory, settings.rules_sync_dir,
                                       only_changed=not all_files, log_dir=settings.data_dir / "logs")

    # ---- 0.12.42: the approval policy and its append-only audit (master token only) ----
    @app.get("/v1/policy", dependencies=[Depends(auth)])
    async def get_policy() -> dict:
        return {**policy_mod.describe(), "card_timeout_min": {
            "attended": approval_classes.timeout_min(store), "unattended": settings.approval_ttl_min},
            "reuse_hours": action_rules.REUSE_HOURS}

    @app.get("/v1/policy/audit", dependencies=[Depends(auth)])
    async def get_policy_audit(limit: int = 200, bot_id: UUID | None = None, since: str | None = None) -> dict:
        return {"audit": await asyncio.to_thread(rules.audit, limit, str(bot_id) if bot_id else None, since)}

    @app.get("/v1/approvals", dependencies=[Depends(auth)])
    async def list_approvals(status: str | None = None) -> list[dict]:
        rules.expire()
        return rules.list(status)

    def _approval_call(fn, *args):
        try:
            return fn(*args)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (action_rules.RuleError, AssignmentError) as exc:
            raise HTTPException(status_code=409, detail=getattr(exc, "detail", str(exc))) from exc

    @app.post("/v1/approvals/{approval_id}/approve", dependencies=[Depends(auth)])
    async def approve(approval_id: int) -> dict:
        """Approve once (also Re-run of an expired request): one-shot grant + resume turn."""
        rules.expire()
        return _approval_call(rules.approve, approval_id, assignments)

    @app.post("/v1/approvals/{approval_id}/deny", dependencies=[Depends(auth)])
    async def deny(approval_id: int) -> dict:
        return _approval_call(rules.deny, approval_id)

    @app.post("/v1/gate-alerts/{alert_id}/dismiss", status_code=204, dependencies=[Depends(auth)])
    async def dismiss_alert(alert_id: int) -> None:
        if not rules.dismiss_alert(alert_id):
            raise HTTPException(status_code=404, detail="alert not found")

    # --- 0.11.0 watchers (master token; webhooks use their own per-watcher secret) ---
    watchers = scheduler.watchers

    def _watcher_call(fn, *args):
        try:
            return fn(*args)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WatcherError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/bots/{bot_id}/watchers", status_code=201, dependencies=[Depends(auth)])
    async def create_watcher(bot_id: UUID, body: WatcherCreate) -> dict:
        """The secret of a webhook watcher is in this response only (stored as a sha256)."""
        bot = _bot(bot_id)
        watcher, secret = _watcher_call(
            lambda: watchers.create(bot, body.kind, body.name, body.prompt, body.config, debounce_s=body.debounce_s,
                                    max_per_hour=body.max_per_hour, enabled=body.enabled))
        return {**watcher, **({"secret": secret, "url": f"/v1/hooks/{watcher['id']}"} if secret else {})}

    @app.get("/v1/bots/{bot_id}/watchers", dependencies=[Depends(auth)])
    async def list_watchers(bot_id: UUID) -> list[dict]:
        _bot(bot_id)
        return watchers.list(bot_id)

    @app.patch("/v1/watchers/{watcher_id}", dependencies=[Depends(auth)])
    async def update_watcher(watcher_id: UUID, body: WatcherUpdate) -> dict:
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items() if value is not None}
        return _watcher_call(watchers.update, str(watcher_id), fields)

    @app.delete("/v1/watchers/{watcher_id}", status_code=204, dependencies=[Depends(auth)])
    async def delete_watcher(watcher_id: UUID) -> None:
        if not watchers.delete(str(watcher_id)):
            raise HTTPException(status_code=404, detail="watcher not found")

    @app.post("/v1/watchers/{watcher_id}/test", dependencies=[Depends(auth)])
    async def test_watcher(watcher_id: UUID) -> dict:
        outcome, job_id = _watcher_call(watchers.test, str(watcher_id))
        return {"outcome": outcome, "job_id": job_id}

    @app.post("/v1/watchers/{watcher_id}/secret", dependencies=[Depends(auth)])
    async def rotate_watcher_secret(watcher_id: UUID) -> dict:
        return {"secret": _watcher_call(watchers.rotate_secret, str(watcher_id))}

    @app.get("/v1/watchers/{watcher_id}/runs", dependencies=[Depends(auth)])
    async def watcher_runs(watcher_id: UUID) -> list[dict]:
        if not watchers.get(str(watcher_id)):
            raise HTTPException(status_code=404, detail="watcher not found")
        return watchers.runs(str(watcher_id))

    @app.post("/v1/hooks/{watcher_id}", status_code=202)
    async def webhook(watcher_id: str, request: Request) -> JSONResponse:
        """A webhook watcher: Bearer <its secret>, body up to 64 kB (untrusted data for the bot).
        202 means accepted: the outcome says whether a run was queued ("fired") or folded into
        one already pending or running ("debounced", "busy"); it never means the work is done."""
        header = request.headers.get("authorization", "")
        secret = header[7:].strip() if header.lower().startswith("bearer ") else ""
        body = await _read_capped(request, WATCHER_BODY_LIMIT, "body")
        try:
            outcome = await asyncio.to_thread(watchers.webhook, watcher_id, secret, body,
                                              request.headers.get("content-type", ""))
        except LookupError as exc:
            raise HTTPException(status_code=401, detail="unknown watcher or wrong secret") from exc
        status = {"fired": 202, "debounced": 202, "busy": 202, "capped": 429, "global_cap": 429}.get(outcome, 409)
        return JSONResponse({"outcome": outcome}, status_code=status)

    # --- 0.11.0 usage, budgets and timelines (master token) ---

    @app.get("/v1/usage", dependencies=[Depends(auth)])
    async def get_usage(days: int = Query(default=7, ge=1, le=60)) -> list[dict]:
        """Per bot and New York day: runs, minutes, tokens and cost (unknown usage is counted, not zero)."""
        return await asyncio.to_thread(store.usage_by_day, days)

    @app.post("/v1/bots/{bot_id}/budget/allow", dependencies=[Depends(auth)])
    async def allow_budget(bot_id: UUID, body: BudgetAllow | None = None) -> dict:
        """"Allow +N today": more unattended budget until 00:00 New York time; held jobs run."""
        _bot(bot_id)
        body = body or BudgetAllow()
        return {"released": store.allow_budget(bot_id, body.turns, body.minutes, body.tokens)}

    @app.get("/v1/routines/{routine_id}/runs", dependencies=[Depends(auth)])
    async def routine_runs(routine_id: UUID) -> list[dict]:
        if not store.get_routine(routine_id):
            raise HTTPException(status_code=404, detail="routine not found")
        return store.routine_runs(routine_id)

    @app.get("/v1/assignments/{assignment_id}/timeline", dependencies=[Depends(auth)])
    async def assignment_timeline(assignment_id: UUID) -> list[dict]:
        if not assignments.get(assignment_id):
            raise HTTPException(status_code=404, detail="assignment not found")
        return store.assignment_timeline(assignment_id)

    @app.get("/v1/gate", dependencies=[Depends(auth)])
    async def gate_status() -> dict:
        """Per provider: enforced (probe passed on this host), advisory (wired, not proven) or none.
        Unattended runs need enforced."""
        return {name: {"state": action_rules.gate_state(settings.control_dir, name),
                       **action_rules.read_capabilities(settings.control_dir).get(name, {})}
                for name in action_rules.WIRED}

    @app.get("/v1/isolation", dependencies=[Depends(auth)])
    async def isolation_status() -> dict:
        """C1: whether the controller/bots split is installed (paths and modes only, no secrets)."""
        return isolation.status(settings)

    @app.post("/v1/needs-you/clear", dependencies=[Depends(auth)])
    async def clear_needs_you(body: NeedsYouClear) -> dict:
        """0.12.34: hide these items until they change (pending cards and open questions stay)."""
        return {"cleared": needs_you.clear(store, body.ids)}

    @app.post("/v1/needs-you/dismiss", response_model=Assignment, dependencies=[Depends(auth)])
    async def dismiss_needs_you(body: NeedsYouDismiss) -> Assignment:
        return _assignment_call(assignments.dismiss, body.assignment_id)

    @app.post("/v1/jobs/{job_id}/dismiss", status_code=204, dependencies=[Depends(auth)])
    async def dismiss_job(job_id: UUID) -> None:
        if not needs_you.dismiss_job(store, str(job_id)):
            raise HTTPException(status_code=404, detail="no finished job with that id")

    @app.post("/v1/desktops/{name}/hand-back", dependencies=[Depends(auth)])
    async def desktop_hand_back(name: str, bot: UUID | None = None) -> dict:
        """Joshua finished a step on a desktop: the bot that last used it continues."""
        # checked and posted with no await in between: the bot is never told to continue while the owner
        # still holds a control connection to this desktop
        if bot_desks.owner_controlling(name):
            raise HTTPException(status_code=409, detail="release control of this desktop first")
        own = bot_desks.bot_of(name)
        if bot is not None and own != str(bot):
            raise HTTPException(status_code=409, detail="this desktop is no longer that bot's; reload the list")
        if own is not None:
            bot = store.get_bot(own)
            if bot is None:
                raise HTTPException(status_code=404, detail="the bot of this desktop no longer exists")
            try:
                _, posted = _post_owner_message(bot, f"The owner finished the step on your desktop ({name}); continue.")
            except TurnError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"bot_id": str(bot.id), "job_id": str(posted.id)}
        if name not in settings.desktops:
            raise HTTPException(status_code=404, detail="desktop not found")
        job_id = needs_you.last_desk_job(store, name)
        job = store.get(job_id) if job_id else None
        bot = store.get_bot(job.bot_id) if job and job.bot_id else None
        if bot is None:
            raise HTTPException(status_code=404, detail="no bot has used this desktop")
        text = f"Joshua finished the step on {name}; continue."
        item = assignments.get(job.assignment_id) if job.assignment_id else None
        if item and item.status not in ("done", "dropped"):
            queued = _assignment_call(assignments.reply, item.id, text)
        else:
            try:
                _, posted = _post_owner_message(bot, text)
            except TurnError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            queued = str(posted.id)
        return {"bot_id": str(bot.id), "job_id": queued}

    @app.post("/v1/assignments/{assignment_id}/stop", response_model=Assignment, dependencies=[Depends(auth)])
    async def stop_assignment(assignment_id: UUID) -> Assignment:
        """Pause the assignment and its children: queued turns and pending wake-ups are
        cancelled, running turns are stopped."""
        _cancel_jobs(_assignment_call(assignments.stop, assignment_id))
        return assignments.get(assignment_id)

    @app.post("/v1/assignments/{assignment_id}/reply", dependencies=[Depends(auth)])
    async def reply_assignment(assignment_id: UUID, body: AssignmentReply) -> dict:
        job_id = _assignment_call(assignments.reply, assignment_id, body.text)
        return {"job_id": job_id, "assignment": assignments.get(assignment_id).model_dump(mode="json")}

    @app.get("/v1/bots/{bot_id}/wakeups", response_model=list[Wakeup], dependencies=[Depends(auth)])
    async def list_wakeups(bot_id: UUID, include_done: bool = False) -> list[Wakeup]:
        _bot(bot_id)
        return assignments.wakeups(bot_id, include_done=include_done)

    @app.delete("/v1/bots/{bot_id}/wakeups/{wakeup_id}", status_code=204, dependencies=[Depends(auth)])
    async def cancel_wakeup(bot_id: UUID, wakeup_id: UUID) -> None:
        _assignment_call(assignments.cancel_wakeup, wakeup_id, bot_id)

    return app


app = create_app()
