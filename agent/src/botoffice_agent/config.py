"""Local configuration, device state and the device token.

Files (XDG paths; tests pass their own directories):
- ``~/.config/botoffice-agent/config.toml``: ``server``, ``sandbox`` and ``confirm_local``
  (``"on"``, ``"off"`` or ``"server"``: on/off here always win; "server" follows the per-computer
  setting in Bot Office), ``confirm_command`` (argv list, ``{text}`` is replaced by the question),
  ``confirm_timeout`` (seconds; no answer is a no), ``token_store`` (``auto``, ``keyring``, ``file``).
- ``~/.config/botoffice-agent/deny.toml``: ``paths = [...]`` added to the built-in deny-list and
  ``commands = [...]`` (regular expressions; a ``run`` whose command matches one is refused) added
  to the built-in catastrophic-command list. Only this file changes the deny-list: nothing Bot
  Office sends can.
- ``~/.config/botoffice-agent/device.json``: machine id, server, when paired (no secret).
- the token: the libsecret keyring through ``secret-tool`` when it works, else
  ``~/.config/botoffice-agent/token`` (mode 0600; readable by anything running as you).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP = "botoffice-agent"
DEFAULT_SERVER = "ws://127.0.0.1:18787"
FLAG_VALUES = ("on", "off", "server")
FLAGS = ("sandbox", "confirm_local")
DEFAULT_CONFIRM = ["zenity", "--question", "--title", "Bot Office", "--width", "520", "--text", "{text}"]
DEFAULT_CONFIRM_TIMEOUT = 120
# always refused, whatever Bot Office approved (deny.toml adds to these, never removes)
DEFAULT_DENY = ("~/.ssh", "~/.gnupg", "~/.config/google-chrome", "~/.config/chromium", "~/.mozilla",
                "~/.local/share/keyrings", "~/.password-store", "/etc/shadow", "/etc/gshadow",
                "~/.config/botoffice-agent", "~/.local/state/botoffice-agent")


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP


@dataclass
class Config:
    home: Path
    config_dir: Path
    state_dir: Path
    server: str = DEFAULT_SERVER
    sandbox: str = "server"
    confirm_local: str = "server"
    confirm_command: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIRM))
    confirm_timeout: int = DEFAULT_CONFIRM_TIMEOUT
    token_store: str = "auto"
    deny: list[str] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    def deny_entries(self) -> list[str]:
        return [*DEFAULT_DENY, str(self.config_dir), str(self.state_dir), *self.deny]

    def local_flags(self) -> dict:
        return {"sandbox": self.sandbox, "confirm_local": self.confirm_local}


def effective(local: str, server_value: object) -> bool:
    """The flag that applies: this computer's own on/off always wins; "server" follows Bot Office."""
    if local == "on":
        return True
    if local == "off":
        return False
    return server_value is True


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load(home: Path | None = None, cfg_dir: Path | None = None, st_dir: Path | None = None) -> Config:
    home = Path(home or Path.home())
    cfg = Config(home=home, config_dir=Path(cfg_dir or config_dir()), state_dir=Path(st_dir or state_dir()))
    data = _read_toml(cfg.config_dir / "config.toml")
    if "server" in data:
        cfg.server = str(data["server"])
    for name in FLAGS:
        value = data.get(name, "server")
        if value not in FLAG_VALUES:
            raise ConfigError(f"config.toml: {name} must be one of {', '.join(FLAG_VALUES)}")
        setattr(cfg, name, value)
    if "confirm_command" in data:
        command = data["confirm_command"]
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ConfigError("config.toml: confirm_command must be a list of strings")
        cfg.confirm_command = command
    timeout = data.get("confirm_timeout", DEFAULT_CONFIRM_TIMEOUT)
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ConfigError("config.toml: confirm_timeout must be 1 to 3600 seconds")
    cfg.confirm_timeout = timeout
    store = data.get("token_store", "auto")
    if store not in ("auto", "keyring", "file"):
        raise ConfigError("config.toml: token_store must be auto, keyring or file")
    cfg.token_store = store
    deny_file = _read_toml(cfg.config_dir / "deny.toml")
    deny = deny_file.get("paths", [])
    if not isinstance(deny, list) or not all(isinstance(item, str) and item for item in deny):
        raise ConfigError("deny.toml: paths must be a list of paths")
    cfg.deny = deny
    commands = deny_file.get("commands", [])
    if not isinstance(commands, list) or not all(isinstance(item, str) and item for item in commands):
        raise ConfigError("deny.toml: commands must be a list of regular expressions")
    for item in commands:
        try:
            re.compile(item)
        except re.error as exc:
            raise ConfigError(f"deny.toml: commands: {item!r}: {exc}") from exc
    cfg.deny_commands = commands
    return cfg


# --- device state and token ---

def fingerprint(token: str) -> str:
    """The same value Bot Office shows in Settings > Computers before Activate."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    value = hashlib.sha256(("botoffice-device-fp:" + digest).encode()).hexdigest()[:16]
    return "-".join(value[i:i + 4] for i in range(0, 16, 4))


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_device(cfg: Config) -> dict | None:
    try:
        data = json.loads((cfg.config_dir / "device.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("machine_id") else None


def write_device(cfg: Config, device: dict) -> None:
    _private_write(cfg.config_dir / "device.json", json.dumps(device, indent=2) + "\n")


class TokenStore:
    """The device token in the libsecret keyring (secret-tool) or a 0600 file."""

    def __init__(self, cfg: Config, machine_id: str, runner=subprocess.run):
        self.cfg = cfg
        self.machine_id = machine_id
        self.file = cfg.config_dir / "token"
        self.run = runner

    def _attrs(self) -> list[str]:
        return ["service", APP, "machine", self.machine_id]

    def _keyring_ok(self) -> bool:
        return self.cfg.token_store != "file" and shutil.which("secret-tool") is not None

    def save(self, token: str) -> str:
        """Where it went: "keyring" or "file"."""
        if self._keyring_ok():
            done = self.run(["secret-tool", "store", "--label", "Bot Office device token", *self._attrs()],
                            input=token, text=True, capture_output=True, timeout=30)
            if done.returncode == 0:
                self.file.unlink(missing_ok=True)
                return "keyring"
            if self.cfg.token_store == "keyring":
                raise ConfigError("secret-tool could not store the token (is a keyring unlocked?)")
        elif self.cfg.token_store == "keyring":
            raise ConfigError("token_store is keyring but secret-tool is not installed")
        _private_write(self.file, token + "\n")
        return "file"

    def load(self) -> str:
        if self._keyring_ok():
            done = self.run(["secret-tool", "lookup", *self._attrs()], text=True, capture_output=True, timeout=30)
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout.strip()
        try:
            info = os.stat(self.file, follow_symlinks=False)
            if info.st_mode & 0o077:
                raise ConfigError(f"{self.file} must be mode 0600")
            return self.file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise ConfigError("no device token: run `botoffice-agent pair CODE` first") from None

    def clear(self) -> None:
        if self._keyring_ok():
            self.run(["secret-tool", "clear", *self._attrs()], capture_output=True, timeout=30)
        self.file.unlink(missing_ok=True)
