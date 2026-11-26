from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import isolation


def first_env(*names: str, default: str | None = None) -> str | None:
    """Return the first defined environment variable in *names* (new names first)."""
    for name in names:
        if name in os.environ:
            return os.environ[name]
    return default



VOICE_MODES = ("auto", "off")
STT_MODELS = ("tiny.en", "base.en", "small.en")
TTS_MODELS = ("fp32", "int8")
DESKTOP_NAME = re.compile(r"desktop-[1-9][0-9]?")
DESKTOP_MODES = ("per-bot", "pool")
DESKTOP_MEMORY = re.compile(r"[1-9][0-9]{0,5}[mg]")
DESKTOP_GEOMETRY = re.compile(r"([1-9][0-9]{2,3})x([1-9][0-9]{2,3})")
DESKTOP_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,200}(?::[A-Za-z0-9._-]{1,128})?")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = Path.home() / "printpilot-agentdesk" / "data"  # live install path; ~/bot-office may symlink here
    token: str | None = None
    token_file: Path | None = None
    job_timeout: int = 600
    enabled_providers: frozenset[str] = field(default_factory=lambda: frozenset({"codex", "claude", "cursor", "antigravity", "opencode"}))
    desktop_timeout: int = 60
    # optional local voice (scripts/install_voice.sh); "off" hides it entirely
    voice_mode: str = "auto"
    stt_model: str = "small.en"
    tts_model: str = "fp32"
    voice_idle_seconds: int = 600
    # interpreter of the separate voice venv; default <data_dir>/voice/venv/bin/python
    voice_python: Path | None = None
    # global cap on automatic retries (rolling hour, counted in the DB)
    max_retries_per_hour: int = 20
    # inbox and bot-to-bot turns per rolling hour: per sender, and for all senders together
    inbox_per_sender_hourly: int = 20
    inbox_global_hourly: int = 60
    # how far back missed routine runs are looked for (startup and stalled ticks)
    catchup_hours: int = 24
    # jobs that may run at once (one per bot); desktops are leased to them while free
    run_lanes: int = 3
    desktops: tuple[str, ...] = ("desktop-1", "desktop-2")
    # remote-desktop: each bot's own desktop (docs/REMOTE-DESKTOPS.md); "pool" = the old lease only
    desktop_mode: str = "per-bot"
    desktop_image: str = "localhost/light-desktop:v1"
    desktop_max_running: int = 4
    desktop_idle_minutes: int = 20
    desktop_memory: str = "1536m"
    desktop_cpus: float = 1.5
    desktop_geometry: str = "1280x800"
    # 0.10.0: an active assignment without progress this long is flagged stalled (no model call)
    stall_hours: float = 6
    # 0.12.39 stall sweeper (stall_sweep.py): N hours without progress wakes the owner, N more its lead
    # (0 = off), and at most this many such wakes an hour
    stall_sweep_hours: float = 3
    stall_sweep_per_hour: int = 6
    # 0.12.39 rules sync (rules_sync.py): drop folder of rule files re-imported into bots' memory daily
    rules_sync_dir: Path | None = None
    # upper bound of a bot's own turn timeout (bots.job_timeout, 60 s .. this)
    max_turn_timeout: int = 3600
    # 0.11.0: unattended approval requests expire after this many minutes
    approval_ttl_min: int = 1440
    # all watcher-triggered runs per rolling hour
    trigger_runs_per_hour: int = 60
    # hour (ET) of the model-free daily digest
    digest_hour: int = 8
    # private controller dir (token, DB, bridge config, gate capabilities); None = data_dir (old layout)
    private_dir: Path | None = None
    # privileged launcher (deploy/bot-office-run via sudo -u <bots_user>); None = run CLIs directly
    launcher: Path | None = None
    bots_user: str = "ubuntu"

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("Bot-Office API must bind to loopback only")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.job_timeout <= 0 or self.desktop_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if self.voice_mode not in VOICE_MODES:
            raise ValueError("BOT_OFFICE_VOICE must be auto or off")
        if self.stt_model not in STT_MODELS:
            raise ValueError(f"BOT_OFFICE_STT_MODEL must be one of {', '.join(STT_MODELS)}")
        if self.tts_model not in TTS_MODELS:
            raise ValueError("BOT_OFFICE_TTS_MODEL must be fp32 or int8")
        if not 1 <= self.catchup_hours <= 24 * 31:
            raise ValueError("BOT_OFFICE_CATCHUP_HOURS must be between 1 and 744")
        if not 1 <= self.run_lanes <= 16:
            raise ValueError("BOT_OFFICE_RUN_LANES must be between 1 and 16")
        if not self.desktops or len(set(self.desktops)) != len(self.desktops) or not all(
            DESKTOP_NAME.fullmatch(name) for name in self.desktops
        ):
            raise ValueError("BOT_OFFICE_DESKTOPS must be distinct names like desktop-1,desktop-2")
        if self.desktop_mode not in DESKTOP_MODES:
            raise ValueError("BOT_OFFICE_DESKTOP_MODE must be per-bot or pool")
        if not DESKTOP_IMAGE.fullmatch(self.desktop_image):
            raise ValueError("BOT_OFFICE_DESKTOP_IMAGE must be an image reference like localhost/light-desktop:v1")
        if not 1 <= self.desktop_max_running <= 32:
            raise ValueError("BOT_OFFICE_DESKTOP_MAX_RUNNING must be between 1 and 32")
        if not 0 <= self.desktop_idle_minutes <= 7 * 24 * 60:
            raise ValueError("BOT_OFFICE_DESKTOP_IDLE_MINUTES must be between 0 and 10080")
        if not DESKTOP_MEMORY.fullmatch(self.desktop_memory):
            raise ValueError("BOT_OFFICE_DESKTOP_MEMORY must look like 1536m or 2g")
        if not 0.1 <= self.desktop_cpus <= 64:
            raise ValueError("BOT_OFFICE_DESKTOP_CPUS must be between 0.1 and 64")
        geometry = DESKTOP_GEOMETRY.fullmatch(self.desktop_geometry)
        if not geometry or not (320 <= int(geometry[1]) <= 3840 and 240 <= int(geometry[2]) <= 2160):
            raise ValueError("BOT_OFFICE_DESKTOP_GEOMETRY must look like 1280x800")
        if self.max_retries_per_hour < 0:
            raise ValueError("BOT_OFFICE_MAX_RETRIES_PER_HOUR must not be negative")
        if self.inbox_per_sender_hourly < 1 or self.inbox_global_hourly < 1:
            raise ValueError("BOT_OFFICE_INBOX_PER_SENDER_HOURLY and BOT_OFFICE_INBOX_HOURLY must be at least 1")
        if not 0 <= self.stall_sweep_hours <= 24 * 30:
            raise ValueError("BOT_OFFICE_STALL_SWEEP_HOURS must be between 0 and 720")
        if not 1 <= self.stall_sweep_per_hour <= 100:
            raise ValueError("BOT_OFFICE_STALL_SWEEP_PER_HOUR must be between 1 and 100")
        if not 0 < self.stall_hours <= 24 * 30:
            raise ValueError("BOT_OFFICE_STALL_HOURS must be between 0 and 720")
        if not 60 <= self.max_turn_timeout <= 24 * 3600:
            raise ValueError("BOT_OFFICE_MAX_TURN_TIMEOUT must be between 60 and 86400")
        if not 1 <= self.approval_ttl_min <= 24 * 60:
            raise ValueError("BOT_OFFICE_APPROVAL_TTL_MIN must be between 1 and 1440")
        if not 0 <= self.trigger_runs_per_hour <= 1000:
            raise ValueError("BOT_OFFICE_TRIGGER_RUNS_PER_HOUR must be between 0 and 1000")
        if not 0 <= self.digest_hour <= 23:
            raise ValueError("BOT_OFFICE_DIGEST_HOUR must be between 0 and 23")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.bots_user):
            raise ValueError("BOT_OFFICE_BOTS_USER must be a unix user name")
        if self.voice_idle_seconds < 30:
            raise ValueError("BOT_OFFICE_VOICE_IDLE_SECONDS must be at least 30")
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser())
        if self.token_file is not None:
            object.__setattr__(self, "token_file", Path(self.token_file).expanduser())
        if self.voice_python is not None:
            object.__setattr__(self, "voice_python", Path(self.voice_python).expanduser())
        for name in ("private_dir", "launcher"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, Path(getattr(self, name)).expanduser())

    @classmethod
    def from_env(cls) -> Settings:
        providers = first_env(
            "BOT_OFFICE_PROVIDERS", "AGENTDESK_PROVIDERS",
            default="codex,claude,cursor,antigravity,opencode",
        ) or ""
        token_file = first_env("BOT_OFFICE_TOKEN_FILE", "AGENTDESK_TOKEN_FILE")
        return cls(
            host=first_env("BOT_OFFICE_HOST", "AGENTDESK_HOST", default="127.0.0.1") or "127.0.0.1",
            port=int(first_env("BOT_OFFICE_PORT", "AGENTDESK_PORT", default="8787") or "8787"),
            data_dir=Path(
                first_env(
                    "BOT_OFFICE_DATA_DIR", "AGENTDESK_DATA_DIR",
                    default="~/printpilot-agentdesk/data",
                ) or "~/printpilot-agentdesk/data"
            ),
            token=first_env("BOT_OFFICE_TOKEN", "AGENTDESK_TOKEN") or None,
            token_file=Path(token_file) if token_file else None,
            job_timeout=int(
                first_env("BOT_OFFICE_JOB_TIMEOUT", "AGENTDESK_JOB_TIMEOUT", default="600") or "600"
            ),
            enabled_providers=frozenset(
                name.strip().lower() for name in providers.split(",") if name.strip()
            ),
            voice_mode=(os.environ.get("BOT_OFFICE_VOICE") or "auto").strip().lower(),
            stt_model=(os.environ.get("BOT_OFFICE_STT_MODEL") or "small.en").strip(),
            tts_model=(os.environ.get("BOT_OFFICE_TTS_MODEL") or "fp32").strip().lower(),
            voice_idle_seconds=int(os.environ.get("BOT_OFFICE_VOICE_IDLE_SECONDS") or "600"),
            voice_python=Path(voice_python) if (voice_python := os.environ.get("BOT_OFFICE_VOICE_PYTHON")) else None,
            max_retries_per_hour=int(os.environ.get("BOT_OFFICE_MAX_RETRIES_PER_HOUR") or "20"),
            inbox_per_sender_hourly=int(os.environ.get("BOT_OFFICE_INBOX_PER_SENDER_HOURLY") or "20"),
            inbox_global_hourly=int(os.environ.get("BOT_OFFICE_INBOX_HOURLY") or "60"),
            catchup_hours=int(os.environ.get("BOT_OFFICE_CATCHUP_HOURS") or "24"),
            run_lanes=int(os.environ.get("BOT_OFFICE_RUN_LANES") or "3"),
            stall_hours=float(os.environ.get("BOT_OFFICE_STALL_HOURS") or "6"),
            stall_sweep_hours=float(os.environ.get("BOT_OFFICE_STALL_SWEEP_HOURS") or "3"),
            stall_sweep_per_hour=int(os.environ.get("BOT_OFFICE_STALL_SWEEP_PER_HOUR") or "6"),
            rules_sync_dir=Path(rules_dir).expanduser() if (rules_dir := os.environ.get("BOT_OFFICE_RULES_SYNC_DIR"))
            else Path("~/bo-sync/rules").expanduser(),
            max_turn_timeout=int(os.environ.get("BOT_OFFICE_MAX_TURN_TIMEOUT") or "3600"),
            approval_ttl_min=int(os.environ.get("BOT_OFFICE_APPROVAL_TTL_MIN") or "1440"),  # 0.12.42: a day
            trigger_runs_per_hour=int(os.environ.get("BOT_OFFICE_TRIGGER_RUNS_PER_HOUR") or "60"),
            digest_hour=int(os.environ.get("BOT_OFFICE_DIGEST_HOUR") or "8"),
            private_dir=Path(private) if (private := os.environ.get("BOT_OFFICE_PRIVATE_DIR")) else None,
            launcher=Path(launcher) if (launcher := os.environ.get("BOT_OFFICE_LAUNCHER")) else None,
            bots_user=(os.environ.get("BOT_OFFICE_BOTS_USER") or "ubuntu").strip(),
            desktops=tuple(
                name.strip() for name in (os.environ.get("BOT_OFFICE_DESKTOPS") or "desktop-1,desktop-2").split(",")
                if name.strip()
            ),
            desktop_mode=(os.environ.get("BOT_OFFICE_DESKTOP_MODE") or "per-bot").strip().lower(),
            desktop_image=(os.environ.get("BOT_OFFICE_DESKTOP_IMAGE") or "localhost/light-desktop:v1").strip(),
            desktop_max_running=int(os.environ.get("BOT_OFFICE_DESKTOP_MAX_RUNNING") or "4"),
            desktop_idle_minutes=int(os.environ.get("BOT_OFFICE_DESKTOP_IDLE_MINUTES") or "20"),
            desktop_memory=(os.environ.get("BOT_OFFICE_DESKTOP_MEMORY") or "1536m").strip().lower(),
            desktop_cpus=float(os.environ.get("BOT_OFFICE_DESKTOP_CPUS") or "1.5"),
            desktop_geometry=(os.environ.get("BOT_OFFICE_DESKTOP_GEOMETRY") or "1280x800").strip().lower(),
        )

    @property
    def control_dir(self) -> Path:
        """Where controller-only state lives (master token, DB, Grok webhook config): the private dir
        when BOT_OFFICE_PRIVATE_DIR is set (never the shared tree, even while it is missing: the
        controller then fails closed, Sec 6), else data_dir (the old single-user layout)."""
        return self.private_dir if self.private_dir else self.data_dir

    @property
    def db_path(self) -> Path:
        return self.control_dir / "agentdesk.sqlite3"

    @property
    def voice_dir(self) -> Path:
        return self.data_dir / "voice"

    @property
    def voice_interpreter(self) -> Path:
        return self.voice_python or self.voice_dir / "venv" / "bin" / "python"

    def ensure_dirs(self) -> None:
        # isolated layout: data_dir is the shared tree (group bo-share, setgid); the bots may write
        # only its subdirs (data_dir itself holds the voice venv the controller runs); logs stay private
        for path, mode in ((self.data_dir, isolation.locked_dir_mode()), (self.data_dir / "logs", 0o700),
                           *((self.data_dir / name, isolation.shared_dir_mode())
                             for name in ("workspaces", "bots", "shared", "inboxes"))):
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            try:
                path.chmod(mode)
            except PermissionError:
                if mode == 0o700:
                    raise

    def read_token(self) -> str | None:
        if self.token:
            return self.token
        if not self.token_file or not self.token_file.is_file():
            return None
        mode = self.token_file.stat().st_mode & 0o777
        if mode != 0o600:
            raise RuntimeError("BOT_OFFICE_TOKEN_FILE (or AGENTDESK_TOKEN_FILE) must have mode 0600")
        return self.token_file.read_text(encoding="utf-8").strip() or None
