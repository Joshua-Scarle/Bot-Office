"""0.11.0 C1: the privileged path and the isolation layout.

Isolated layout (deploy/install-isolation.sh --apply): the controller runs as system user
``botoffice`` and keeps the master token, the SQLite DB and the Grok webhook config in a private
dir (0700). Every provider CLI and every podman command runs as the bots user through
``sudo -n -u <bots user> /usr/local/libexec/bot-office-run``, which sets no_new_privs (unless the
bot has allow_sudo), umask 007 and a scrubbed environment. The shared tree (workspaces, bot dirs,
shared memory, inboxes, attachments) is group ``bo-share`` with setgid dirs, so the controller
writes it group-readable; files the bot must not change (hook configs, the gate file) are written
group read-only in dirs it cannot write.

Old single-user layout (no launcher, or the controller already runs as the bots user): commands
run directly as today, modes stay 0700/0600, and the GUI shows "isolation: off".
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LAUNCHER = Path("/usr/local/libexec/bot-office-run")
CONTROLLER_USER = "botoffice"
SHARE_GROUP = "bo-share"
SUDO = "/usr/bin/sudo"


@dataclass(frozen=True)
class Launcher:
    path: Path
    user: str

    def prefix(self, *, allow_sudo: bool = False, cwd: Path | str | None = None, env_stdin: bool = False,
               runtime_dir: bool = False) -> list[str]:
        """argv prefix that runs a program as the bots user through the launcher. *runtime_dir*
        passes the bots user's XDG_RUNTIME_DIR/DBUS address (rootless podman only)."""
        args = [SUDO, "-n", "-u", self.user, str(self.path)]
        if allow_sudo:
            args.append("--allow-sudo")
        if runtime_dir:
            args.append("--runtime-dir")
        if cwd is not None:
            args += ["--cwd", str(cwd)]
        if env_stdin:
            args.append("--env-stdin")
        return [*args, "--"]

    def wrap(self, argv: list[str], **options) -> list[str]:
        # a bare name resolves as the bots user sees it, never on the controller's own PATH
        # (the launcher refuses a name it cannot find as non-absolute)
        program = argv[0] if os.path.isabs(argv[0]) else (self.which(argv[0]) or argv[0])
        return [*self.prefix(**options), program, *argv[1:]]

    @staticmethod
    def header(env: dict[str, str]) -> bytes:
        """The --env-stdin line (the job token travels here, never in argv)."""
        return (json.dumps(env, separators=(",", ":")) + "\n").encode()

    def which(self, name: str, search_path: str | None = None) -> str | None:
        """Absolute path of *name* as the bots user resolves it on *search_path* (default:
        bots_path(); cached for a minute)."""
        search_path = search_path if search_path is not None else bots_path()
        key = (name, search_path)
        cached = _which_cache.get(key)
        if cached and time.monotonic() - cached[0] < WHICH_TTL:
            return cached[1]
        try:
            done = subprocess.run([SUDO, "-n", "-u", self.user, str(self.path), "--which", name, search_path],
                                  capture_output=True, timeout=15, check=False, stdin=subprocess.DEVNULL)
            found = done.stdout.decode(errors="replace").strip() if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            found = None
        found = found if found and os.path.isabs(found) else None
        _which_cache[key] = (time.monotonic(), found)
        return found

    def stop_job(self, job_id: str, timeout: float = 15) -> bool:
        """Stop a job's processes (they belong to the bots user); True when none is left."""
        try:
            done = subprocess.run([*[SUDO, "-n", "-u", self.user, str(self.path)], "--stop-job", job_id],
                                  capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0


WHICH_TTL = 60.0
_which_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_active: Launcher | None = None
_shared = False
_state_root: Path | None = None
_layout_error: str | None = None


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def launcher_usable(path: Path) -> bool:
    """Root-owned, not writable by group or others, executable: the installed launcher."""
    try:
        info = path.stat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and bool(info.st_mode & 0o111))


def detect(settings) -> Launcher | None:
    """The launcher to use, or None for the old layout (run CLIs directly)."""
    path = settings.launcher or DEFAULT_LAUNCHER
    if _current_user() == settings.bots_user:
        return None  # the controller already is the bots user: nothing to switch to
    if not launcher_usable(path):
        return None
    return Launcher(path, settings.bots_user)


def check_layout(settings, launcher: Launcher | None) -> str | None:
    """Why the isolated layout is expected but not whole (Sec 6: fail closed), or None.
    Expected when BOT_OFFICE_PRIVATE_DIR is set or the controller runs as the controller user;
    then the launcher must be usable and the private dir a 0700 dir of the controller's own."""
    user = _current_user()
    private = settings.private_dir
    if not private and user != CONTROLLER_USER:
        return None  # the old single-user layout
    if user == settings.bots_user:
        return (f"BOT_OFFICE_PRIVATE_DIR is set but the controller runs as the bots user {user}: "
                "the bots could read the private dir")
    if launcher is None:
        return f"no usable root-owned launcher at {settings.launcher or DEFAULT_LAUNCHER} (deploy/install-isolation.sh)"
    if not private:
        return f"the controller runs as {user} but BOT_OFFICE_PRIVATE_DIR is not set"
    try:
        info = os.stat(private, follow_symlinks=False)
    except OSError:
        return f"the private dir {private} is missing (deploy/install-isolation.sh --apply)"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        return f"the private dir {private} is not a 0700 dir owned by {user}"
    if settings.token_file and private not in settings.token_file.parents:
        return f"BOT_OFFICE_TOKEN_FILE is outside the private dir {private}"
    return None


def layout_error() -> str | None:
    """Set when the isolated layout is expected but incomplete: no job may run (Sec 6)."""
    return _layout_error


def configure(settings, launcher: Launcher | None | bool = True) -> Launcher | None:
    """Set the process-wide layout (create_app/Scheduler call this once). *launcher* True detects
    (and fails closed: layout_error() is set when the isolated layout is expected but not whole);
    a Launcher or None forces one (tests)."""
    global _active, _shared, _state_root, _layout_error
    _active = detect(settings) if launcher is True else (launcher or None)
    _layout_error = check_layout(settings, _active) if launcher is True else None
    _shared = _active is not None
    # controller-only per-bot state (session pointers, handoff marker, snapshots) leaves the
    # bot-writable tree when isolated
    _state_root = (settings.private_dir / "bot-state") if _shared and settings.private_dir else None
    from . import safefs
    safefs.add_root(settings.data_dir)
    if settings.private_dir:
        safefs.add_root(settings.private_dir)
    if _shared:
        os.umask(0o007)
    return _active


def active() -> Launcher | None:
    return _active


def wrap(argv: list[str], **options) -> list[str]:
    """*argv* through the launcher in the isolated layout, unchanged otherwise."""
    return _active.wrap(argv, **options) if _active else list(argv)


def bots_path() -> str:
    """PATH for programs that run as the bots user. The isolated unit keeps its own PATH free of
    dirs the bots user can write (Sec 3) and names the bots' PATH in BOT_OFFICE_BOTS_PATH; the
    old layout uses the controller's PATH as before."""
    return os.environ.get("BOT_OFFICE_BOTS_PATH") or os.environ.get("PATH", "")


def sudo_env() -> dict[str, str]:
    """The environment sudo itself sees (the launcher builds the program's own from scratch)."""
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": os.environ.get("LANG", "C.UTF-8")}


def run_env(env: dict[str, str] | None = None) -> tuple[dict[str, str] | None, bytes]:
    """(environment for the spawned process, stdin header) for a launcher-wrapped command."""
    if _active is None:
        return env, b""
    if env is None:  # the controller's own environment: its settings never travel
        sent = {key: value for key, value in os.environ.items() if not key.startswith(("BOT_OFFICE_", "AGENTDESK_"))}
    else:
        sent = dict(env)
    sent["PATH"] = bots_path()
    return sudo_env(), _active.header(sent)


def exec_options() -> tuple[dict, bytes]:
    """Extra create_subprocess_exec options and the stdin header for a launcher-wrapped command
    ({} and b"" in the old layout, so those calls stay exactly as before)."""
    if _active is None:
        return {}, b""
    env, header = run_env()
    return {"env": env, "stdin": subprocess.PIPE}, header


def state_dir(bot_dir: Path) -> Path:
    """Where the controller keeps *bot_dir*'s controller-only state: ``<private>/bot-state/<id>``
    when isolated (the bot cannot plant or swap files there), else *bot_dir* itself (the old
    layout, unchanged). Idempotent: state_dir(state_dir(x)) == state_dir(x)."""
    return _state_root / bot_dir.name if _state_root is not None else bot_dir


def state_root() -> Path | None:
    return _state_root


def bots_home() -> str:
    """HOME of the user the CLIs run as (the bots user when isolated)."""
    if _active:
        try:
            return pwd.getpwnam(_active.user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def share(path: Path) -> None:
    """A controller-made dir outside the setgid shared tree (e.g. the probe's temp dir): group
    bo-share, mode 2770 when isolated; 0700 otherwise."""
    if _shared:
        try:
            os.chown(path, -1, grp.getgrnam(SHARE_GROUP).gr_gid)
        except (KeyError, OSError):
            pass
    path.chmod(dir_mode())


# file modes: private in the old layout, group bo-share in the shared tree when isolated
def dir_mode() -> int:
    return 0o2770 if _shared else 0o700


def file_mode() -> int:
    return 0o660 if _shared else 0o600


def locked_dir_mode() -> int:
    """Dirs of controller-written config the bot may read but not change (.cursor, .agents)."""
    return 0o2750 if _shared else 0o700


def locked_file_mode() -> int:
    return 0o640 if _shared else 0o600


def shared_dir_mode() -> int:
    """bots/, shared/, inboxes/, workspaces/ and bots/<id>: sticky when isolated, so a bot cannot
    rename or delete the controller's entries (it could swap them for links otherwise)."""
    return 0o3770 if _shared else 0o700


def workspace_mode() -> int:
    """Job workspaces: sticky, so the bot cannot delete the controller's gate files."""
    return 0o3770 if _shared else 0o700


def sudo_check(private: Path) -> str:
    """The installer's recorded `sudo -l -U <bots user>` result: passed, nopasswd or unchecked."""
    try:
        text = (private / "sudo-check").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "unchecked"
    for line in text.splitlines():
        if line.startswith("result="):
            value = line.split("=", 1)[1].strip()
            return value if value in ("passed", "nopasswd") else "unchecked"
    return "unchecked"


def status(settings) -> dict:
    """What isolation-status and GET /v1/isolation report (no secrets, no file contents)."""
    private = settings.private_dir
    private_ok = bool(private and private.is_dir())
    mode = None
    if private_ok:
        mode = oct(private.stat().st_mode & 0o7777)
    launcher = settings.launcher or DEFAULT_LAUNCHER
    try:
        group = grp.getgrnam(SHARE_GROUP).gr_name
    except KeyError:
        group = None
    checks = {
        "controller_user": _current_user(),
        "bots_user": settings.bots_user,
        "launcher": str(launcher),
        "launcher_installed": launcher_usable(launcher),
        "private_dir": str(private) if private else None,
        "private_dir_mode": mode,
        "token_in_private_dir": bool(private_ok and settings.token_file and private in settings.token_file.parents),
        "db_in_private_dir": bool(private_ok and (settings.control_dir / "agentdesk.sqlite3").exists()
                                  and settings.control_dir == private),
        "share_group": group,
    }
    on = (_active is not None and private_ok and mode == "0o700" and checks["token_in_private_dir"]
          and checks["db_in_private_dir"])
    checks["isolation"] = "on" if on else "off"
    checks["sudo_check"] = sudo_check(private) if private_ok else "unchecked"
    # the token and DB are private from the bots only while the bots user's sudo needs a
    # password (the installer checks and records it; Sec 2)
    checks["secrets_private"] = on and checks["sudo_check"] == "passed"
    if _active is None:
        checks["why"] = ("the controller runs as the bots user" if checks["controller_user"] == settings.bots_user
                         else "no root-owned launcher at " + str(launcher))
    elif not on:
        checks["why"] = "the private dir, token or DB is not in the isolated layout"
    if _layout_error:
        checks["isolation"] = "incomplete: no jobs run"
        checks["why"] = _layout_error
    return checks
