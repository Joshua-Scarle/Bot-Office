from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from .catalog import capability_tier, valid_model_id
from .cron import parse_cron
from .usage import budget_of

# 0.10.0: "assignment" (a turn of the run-until-done loop) and "wakeup" (a wake-up the bot scheduled)
# 0.11.0: "trigger" (a watcher fired; always unattended)
Source = Literal["api", "chat", "routine", "inbox", "assignment", "wakeup", "trigger"]
BotProvider = Literal["codex", "claude", "cursor", "antigravity", "opencode"]
# full: the official CLI runs unattended with shell, file and network access as the service user;
# restricted: the older edit-only / sandboxed CLI permission flags.
CliAccess = Literal["full", "restricted"]
# runs a routine missed while Bot-Office was down: one catch-up run, or none (a notice either way)
CatchUp = Literal["once", "skip"]
# 0.10.0 assignments: a goal a bot works on across turns until done (see assignments.py)
AssignmentStatus = Literal["open", "active", "waiting_on_user", "waiting_on_bot", "blocked", "done", "dropped"]
CLOSED_STATUSES = ("done", "dropped")
POSITION_LIMIT = 1e12  # task list positions: finite and far from where +1 stops changing a float
# when an assignment shows up in Needs-you: never, when blocked or waiting on the user, or always
Escalate = Literal["never", "on_block", "always"]


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_LOGIN = "needs_login"
    # 0.9.0: the provider finished, but the bot reported the task as not done (BOT-OFFICE-STATUS)
    INCOMPLETE = "incomplete"


class Slot(StrEnum):
    """The two stock desktops (kept for compatibility; the list is Settings.desktops)."""

    DESKTOP_1 = "desktop-1"
    DESKTOP_2 = "desktop-2"


class Job(BaseModel):
    self_extra: str | None = Field(default=None, exclude=True)
    # 0.11.10: the owner-side layers this turn's prompt carried, by layer key (snapshots are saved once
    # the turn succeeds)
    layer_texts: dict[str, str] | None = Field(default=None, exclude=True)
    # 0.11.4: the last task event the prompt's "changes by others" line covered (store.issue_agent_token)
    tasks_seen_event: int | None = Field(default=None, exclude=True)
    id: UUID
    prompt: str
    status: JobStatus
    # leased desktop name (desktop-N), or None when the job runs without a desktop
    slot: str | None = None
    provider: str = "codex"
    created_at: datetime
    started_at: datetime | None = None
    # first start ever: started_at moves on with each retry, so use this for created -> started waits
    first_started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    log_path: str | None = None
    error: str | None = None
    bot_id: UUID | None = None
    reason: str | None = None
    source: Source = "api"
    reply_path: str | None = None
    routine_id: UUID | None = None
    model: str | None = None
    effort: str | None = None
    # audit copy of the full assembled assistant reply, and the rolling live draft
    reply_text: str | None = None
    draft_text: str | None = None
    failure_kind: str | None = None
    # done | unverified (no status line) | not_done: … | blocked: … | waiting_on_user: …
    outcome: str | None = None
    # 0.12.16: a done with the evidence it gave (verified 1), a bare done (0), or neither (None)
    verified: int | None = None
    evidence: str | None = None
    # provider attempts so far; a re-queued retry waits until not_before
    attempts: int = 0
    not_before: datetime | None = None
    restart_attempts: int = 0
    # 0.10.0: the assignment this turn advances, and its turn number there
    assignment_id: UUID | None = None
    assignment_turn: int | None = None
    # 0.11.0: the job whose approval this turn resumes
    origin_job_id: UUID | None = None
    # 0.11.0: the watcher that started this turn
    watcher_id: str | None = None
    # 0.11.10: automatic continuation number (0 = not one) and the turn its chain started from
    continue_count: int = 0
    continued_from: UUID | None = None
    # 0.11.10: the kind of automatic turn (continuation.AUTO_KINDS), None for owner/routine/bot-message turns
    auto_kind: str | None = None
    # 0.11.14: a slash command turn, JSON {"name","args","mode"} (slash.encode_slash); None for every other turn
    slash: str | None = None
    bot_name: str | None = Field(default=None, exclude=True)
    api_url: str | None = Field(default=None, exclude=True)
    agent_token: str | None = Field(default=None, exclude=True)
    cli_access: CliAccess = Field(default="full", exclude=True)
    # short prompt used when the provider resumes the bot's ongoing session
    resume_prompt: str | None = Field(default=None, exclude=True)
    attachment_paths: list[str] = Field(default_factory=list, exclude=True)
    # 0.11.0 action gate, decided by the controller before launch: unattended source, the gate
    # command (bin/bot-office-gate), protected paths, sudo and network for the run
    unattended: bool = Field(default=False, exclude=True)
    gate_command: str | None = Field(default=None, exclude=True)
    secret_paths: list[str] = Field(default_factory=list, exclude=True)
    allow_network: bool = Field(default=True, exclude=True)
    allow_sudo: bool = Field(default=False, exclude=True)
    # 0.12.4 fix2: the controller holds a GitHub token, so the run's git asks for it (bin/bot-office-credential)
    github_credential: bool = Field(default=False, exclude=True)

    @property
    def execution(self) -> int:
        """Execution number of the current run (retries and restart replays each start a new one)."""
        return self.attempts + self.restart_attempts


def _check_model(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not valid_model_id(value):
        raise ValueError("invalid model id")
    return value


def _check_icon(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if any(ord(ch) < 32 or ch in "<>&\"'" for ch in value):
        raise ValueError("icon must be an emoji or short symbol")
    return value


class JobCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    provider: str = Field(default="codex", min_length=1, max_length=32)
    model: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)
    bot_id: UUID | None = None
    effort: str | None = Field(default=None, max_length=16)

    _model = field_validator("model")(_check_model)


class DesktopCommand(BaseModel):
    command: list[str] = Field(min_length=1, max_length=64)


class ProviderInfo(BaseModel):
    name: str
    enabled: bool
    login_status: Literal["ok", "needs_login", "disabled", "error"]


class Bot(BaseModel):
    id: UUID
    name: str
    description: str = ""
    icon: str = ""
    icon_image: str | None = None
    # 0.12.0: seed of the generated avatar; an uploaded image wins, the emoji is the fallback
    avatar_seed: str | None = None
    instructions: str = ""
    default_provider: BotProvider = "codex"
    model: str | None = None
    effort: str | None = None
    cli_access: CliAccess = "full"
    reason: str | None = None
    created_at: datetime
    updated_at: datetime
    activity: Literal["queued", "running"] | None = None
    # waiting / blocked come from the bot's assignments (0.10.0)
    status: Literal["idle", "working", "queued", "error", "offline", "waiting", "blocked"] = "idle"
    # per-bot turn timeout in seconds (None: the service default); browser notifications on/off
    job_timeout: int | None = None
    notify: bool = True
    # 0.11.0: this bot's CLI may use sudo (no no_new_privs); it can then read controller secrets
    allow_sudo: bool = False
    # 0.11.0: "full" (default): routine, wake-up, assignment and inbox turns run as in 0.10.x;
    # "gated": they get the strict unattended profile too (trigger turns always do). Joshua only
    scheduled_runs: Literal["full", "gated"] = "full"
    # 0.11.0: daily unattended budget (stored JSON; see daily_budget) and the daily digest opt-in
    daily_budget_json: str | None = Field(default=None, exclude=True)
    digest: bool = False
    # 0.11.10: other bots' messages may steer this bot's running turn (off: they wait for a turn of their own)
    bot_steer: bool = True
    # 0.11.10: never continues by itself (no auto-continue, loop, verify or wake-up turns)
    chat_only: bool = False
    # 0.12.18: which bots' status and chat this bot may read through the teammate tools
    # (section: its own section and unsectioned bots; all; none), and whether others may read it at all
    team_read: Literal["section", "all", "none"] = "section"
    team_private: bool = False
    # unread state for the owner (filled by GET /v1/bots and the read/unread endpoints)
    unread_count: int = 0
    unread_since: datetime | None = None
    marked_unread: bool = False
    read_revision: int = 0
    # How a message sent while this bot works reaches it: "live" (written into the running CLI),
    # "interrupt" (the CLI is interrupted promptly and its session resumed with it), "restart" (0.11.7:
    # interrupted promptly and run again as the same turn with a note of what it had done, for engines or
    # access modes that cannot resume), "queue" (an engine that cannot take input mid-run: it becomes the
    # next turn). Filled by the bot endpoints.
    steering: Literal["live", "interrupt", "restart", "queue"] | None = None

    @computed_field
    @property
    def avatar(self) -> Literal["upload", "character", "generated", "emoji"]:
        if self.icon_image:
            return "upload"
        if self.avatar_seed:
            return "character" if self.avatar_seed.startswith("char|") else "generated"
        return "emoji"

    @computed_field
    @property
    def engine(self) -> str:
        return self.default_provider

    @computed_field
    @property
    def daily_budget(self) -> dict:
        """unattended_turns / unattended_minutes / tokens per New York day (None: no cap)."""
        return budget_of(self.daily_budget_json)

    @computed_field
    @property
    def capability_tier(self) -> Literal["small", "mid", "frontier"]:
        """Display only; always derived from the current model id."""
        return capability_tier(self.model)


class _BotFields(BaseModel):
    """Shared validation. ``engine`` is the preferred name for ``default_provider``."""

    engine: BotProvider | None = Field(default=None, exclude=True)
    model: str | None = Field(default=None, max_length=100)
    effort: str | None = Field(default=None, max_length=16)
    cli_access: CliAccess | None = None
    icon: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=1000)

    _model = field_validator("model")(_check_model)
    _icon = field_validator("icon")(_check_icon)

    @model_validator(mode="after")
    def _merge_engine(self):
        if self.engine is not None:
            current = getattr(self, "default_provider", None)
            if "default_provider" in self.model_fields_set and current != self.engine:
                raise ValueError("engine and default_provider disagree")
            self.default_provider = self.engine
            self.model_fields_set.add("default_provider")
        return self


class BotCreate(_BotFields):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=50_000)
    default_provider: BotProvider = "codex"
    chat_only: bool = False


class BotUpdate(_BotFields):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    instructions: str | None = Field(default=None, max_length=50_000)
    default_provider: BotProvider | None = None
    # null clears the override (service default); bounds are checked against the service maximum
    job_timeout: int | None = Field(default=None, ge=60, le=3600)
    notify: bool | None = None
    allow_sudo: bool | None = None
    scheduled_runs: Literal["full", "gated"] | None = None
    daily_budget: dict | None = None
    digest: bool | None = None
    bot_steer: bool | None = None
    chat_only: bool | None = None
    team_read: Literal["section", "all", "none"] | None = None
    team_private: bool | None = None


class ModelOption(BaseModel):
    id: str
    label: str
    display: str
    capability_tier: Literal["small", "mid", "frontier"] = "small"


class EngineChoice(BaseModel):
    engine: str
    label: str
    default_model: str | None
    models: list[ModelOption]
    # thinking-effort levels this engine accepts; empty means no effort control
    efforts: list[str] = []
    default_effort: str | None = None


class TextBody(BaseModel):
    text: str = Field(max_length=200_000)


class MessageCreate(BaseModel):
    attachments: list[str] = Field(default_factory=list, max_length=6)
    text: str = Field(min_length=1, max_length=100_000)
    reason: str | None = Field(default=None, max_length=1000)
    mode: Literal["steer", "queue"] | None = None


class MessageEdit(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    # None keeps the original message's images
    attachments: list[str] | None = Field(default=None, max_length=6)
    reason: str | None = Field(default=None, max_length=1000)
    # cancel this bot's queued/running jobs first instead of answering 409
    cancel_running: bool = False


class ReadMark(BaseModel):
    # newest message id the client actually showed; omitted = everything
    up_to: int | None = Field(default=None, ge=0)
    # read_revision the client last saw; a mismatch means a newer "mark as unread" happened
    revision: int | None = Field(default=None, ge=0)
    # true only when the owner explicitly opened the bot (clears a manual unread)
    explicit: bool = False


class InboxCreate(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    from_bot_id: UUID | None = None


class Message(BaseModel):
    question: dict | None = None
    # 0.11.0 (#10): the "parked on a usage limit" notice ({kind, job_id, provider, open}); hidden once
    # the job starts, switches provider or is cancelled
    notice: dict | None = None
    attachments: list[str] = Field(default_factory=list)
    id: int
    bot_id: UUID
    role: Literal["user", "assistant", "system"]
    sender: str | None = None
    source: Source = "chat"
    text: str
    kind: Literal["message", "say"] = "message"
    job_id: UUID | None = None
    steer_job_id: UUID | None = None
    steer_status: Literal["pending", "delivered", "queued"] | None = None
    steer_delivered_at: datetime | None = None
    # 0.11.6: why a steer became a queued turn instead of reaching the running one
    steer_note: str | None = None
    created_at: datetime
    # True for the in-flight placeholder of a queued or running job; text is the
    # live draft and the stream is at /v1/jobs/{job_id}/events
    pending: bool = False
    job_status: str | None = None
    # placeholder of a re-queued job waiting for its retry time
    not_before: datetime | None = None
    # message edit / rewind: archived rows are hidden unless ?archived=true
    archived_at: datetime | None = None
    edit_of: int | None = None
    # the reply's job succeeded but the bot gave no BOT-OFFICE-STATUS line (shown as a subtle label)
    unverified: bool = False
    # 0.12.16: what a done reply said it was checked with (None: none given, or not a done)
    evidence: str | None = None
    # 0.12.16: characters of the reply not shown here; the full text is job.reply_text (GET /v1/jobs/{job_id})
    cut_chars: int = 0
    # 0.11.10: who wrote it, so only the owner's own typed messages look like the owner's.
    # owner: typed by the owner; bot: this bot's reply; bot_out: this bot's message to another bot;
    # bot_in: another bot's (or a bridge sender's) message to this bot; bot_reply: the other bot's
    # reply copied back; office: written by Bot-Office (routine, wake-up, watcher, assignment step,
    # automatic continuation); system: a notice. peer names the other bot where there is one.
    author: Literal["owner", "bot", "bot_out", "bot_in", "bot_reply", "office", "system"] = "owner"
    peer: str | None = None
    # 0.11.10: Bot-Office machinery, listed only with ?archived=true (debugging)
    hidden: bool = False
    # 0.11.10: on the live placeholder of an automatic continuation, e.g. "3 of 10"
    continuing: str | None = None
    # 0.11.10: an approval request's card ({id, status, label, summary, tool, scope, expires_at,
    # decided_at, resumed}); the chat shows Approve / Deny while status is pending
    approval_id: int | None = None
    approval: dict | None = None


class MessagePage(BaseModel):
    """A contiguous page of a chat, oldest first."""
    messages: list[Message]
    has_older: bool
    has_newer: bool


class SearchHit(BaseModel):
    id: int
    bot_id: UUID
    bot_name: str
    role: Literal["user", "assistant", "system"]
    sender: str | None = None
    created_at: datetime
    hidden: bool = False  # a Bot Office status line (wake-up, continuation): not shown in the chat
    snippet: str


class MessageSearch(BaseModel):
    hits: list[SearchHit]
    has_more: bool


class MessagePosted(BaseModel):
    message: Message
    job: Job
    # 0.11.6: "start" (idle bot, a new turn), "steer" (attached to the working turn, delivered live or at
    # its next step) or "queue" (a new turn behind the working one; delivery_note says why)
    delivery: Literal["start", "steer", "queue"] = "start"
    delivery_note: str | None = None


def _check_cron(value: str) -> str:
    parse_cron(value)
    return value


class Routine(BaseModel):
    id: UUID
    bot_id: UUID
    name: str | None = None
    cron: str
    prompt: str
    enabled: bool = True
    last_run_at: datetime | None = None
    created_at: datetime
    catch_up: CatchUp = "once"
    # "bot": made by the bot itself through its job token (0.10.0, limited per bot)
    created_by: Literal["user", "bot"] = "user"
    scan_cursor: datetime | None = Field(default=None, exclude=True)
    version: int = Field(default=0, exclude=True)
    pending_run: datetime | None = Field(default=None, exclude=True)


class RoutineCreate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    cron: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=100_000)
    enabled: bool = True
    catch_up: CatchUp = "once"

    _cron = field_validator("cron")(_check_cron)


class RoutineUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    cron: str | None = Field(default=None, min_length=1, max_length=100)
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    enabled: bool | None = None
    bot_id: UUID | None = None
    catch_up: CatchUp | None = None

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return None if value is None else _check_cron(value)


# every task has one; higher first wherever tasks are listed or picked
TaskPriority = Literal["urgent", "high", "normal", "low"]


class Assignment(BaseModel):
    id: UUID
    bot_id: UUID
    title: str
    goal: str = ""
    done_when: str = ""
    status: AssignmentStatus = "open"
    due_at: datetime | None = None
    budget_turns: int = 20
    budget_minutes: int = 120
    turns_used: int = 0
    minutes_used: float = 0
    verify: bool = False
    escalate: Escalate = "on_block"
    created_by: str = "user"
    parent_id: UUID | None = None
    checkpoint: str = ""
    status_note: str = ""
    last_progress_at: datetime | None = None
    stalled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    priority: TaskPriority = "normal"


# 0.11.4 task lists: the shown stage of an assignment (computed, never stored)
TaskStage = Literal["not_started", "in_progress", "completed", "dropped"]
TaskOrigin = Literal["form", "chat", "auto", "bot", "routine"]


class Task(Assignment):
    """An assignment as the task-list API shows it: its version (send it back to edit), list
    position, origin, last actor and the computed stage with its flags."""
    version: int = 1
    position: float | None = Field(default=None, allow_inf_nan=False)
    origin: str = "form"
    source_message_id: int | None = None
    source_job_id: str | None = None
    updated_by: str = "user"
    reviewer: str | None = None
    # text fields a bot edited (title, goal, done_when): shown as bot-written, never as the user's
    bot_edited: list[str] = Field(default_factory=list)
    stage: TaskStage = "not_started"
    waiting: bool = False  # waiting on the user or on a bot
    blocked: bool = False  # blocked, or flagged stalled by the watchdog
    stalled: bool = False
    # working (turn running) | next_up (turn queued) | parked (queued, held until parked_until) | None
    activity: str | None = None
    parked_until: datetime | None = None


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=20_000)
    done_when: str = Field(default="", max_length=4000)
    due_at: datetime | None = None
    budget_turns: int = Field(default=20, ge=1, le=200)
    budget_minutes: int = Field(default=120, ge=1, le=24 * 60)
    verify: bool = False
    escalate: Escalate = "on_block"
    # queue the first turn right away
    start: bool = False
    # None: BOT_OFFICE_TASK_DEFAULT_PRIORITY (normal)
    priority: TaskPriority | None = None


class AssignmentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=20_000)
    done_when: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    budget_turns: int | None = Field(default=None, ge=1, le=200)
    budget_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    verify: bool | None = None
    escalate: Escalate | None = None
    # the user may close (done / dropped) or reopen an assignment; "active" goes through /start
    status: Literal["open", "done", "dropped"] | None = None
    note: str | None = Field(default=None, max_length=1000)
    priority: TaskPriority | None = None


class TaskCreate(AssignmentCreate):
    # list order (lower first); default: after the bot's last task
    position: float | None = Field(default=None, allow_inf_nan=False, ge=-POSITION_LIMIT, le=POSITION_LIMIT)
    # None follows BOT_OFFICE_FORM_TASKS_START (auto: starts on its own); false = don't start yet
    start: bool | None = None


class TaskUpdate(AssignmentUpdate):
    # the version the client last saw (required): a stale one is answered 409 with the current task
    version: int = Field(ge=1)
    position: float | None = Field(default=None, allow_inf_nan=False, ge=-POSITION_LIMIT, le=POSITION_LIMIT)
    reviewer: str | None = Field(default=None, max_length=200)


class TaskReorder(BaseModel):
    bot_id: str = Field(min_length=1, max_length=200)
    order: list[UUID] = Field(min_length=1, max_length=500)
    # optional: the version the client saw of each listed task; a stale one is a 409
    versions: dict[str, int] | None = None


class TaskNote(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AssignmentStart(BaseModel):
    # extra budget for an assignment that used its own up
    add_turns: int = Field(default=0, ge=0, le=200)
    add_minutes: int = Field(default=0, ge=0, le=24 * 60)


class AssignmentReply(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class Wakeup(BaseModel):
    id: UUID
    bot_id: UUID
    assignment_id: UUID | None = None
    due_at: datetime | None = None
    on_assignment: UUID | None = None
    note: str = ""
    created_by_job: UUID | None = None
    status: Literal["pending", "fired", "cancelled"] = "pending"
    job_id: UUID | None = None
    created_at: datetime
    fired_at: datetime | None = None
