from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .config import DESKTOP_NAME, first_env
from .outgoing_images import job_cwd

SLOTS = ("desktop-1", "desktop-2")
DISPLAY = "DISPLAY=:1"
TIMEOUT = 30
USAGE = (
    "usage: desk [--slot SLOT] screenshot [outfile] | click X Y [--sends WHAT] | type TEXT | key KEY [--sends WHAT]"
    "   (in a turn: your own desktop)"
    " | open URL | message BOT TEXT | attach FILE | extend --reason TEXT\n"
    "       desk grok send --to NAME --kind question|result|blocker|fyi --text TEXT"
    " [--thread ID] [--from NAME]\n"
    "       desk team list | status BOT | read BOT [--limit N] [--since X] | message [--queue] BOT TEXT | ask BOT TEXT [--timeout S]\n"
    "       desk grok install-config\n"
    "       desk duet send --text TEXT [--to main] [--kind K] [--thread ID] [--reply-to ID] | status | install-config\n"
    "       desk inbox post --to BOT --from NAME --text TEXT [--thread ID] [--hop N] [--priority]"
)


class DeskError(Exception):
    pass


def _require(value: str, what: str) -> str:
    if not value or not value.strip() or "\x00" in value:
        raise DeskError(f"{what} must not be empty")
    return value


def resolve_slot(explicit: str | None, environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    leased = env.get("BOT_OFFICE_SLOT") or env.get("AGENTDESK_SLOT") or ""
    if env.get("BOT_OFFICE_JOB_ID"):
        # inside a job only the desktop it leased: another job may be using the others
        if not leased:
            # a job that got no desktop (all leased to other jobs) has no BOT_OFFICE_SLOT
            raise DeskError("No desktop is free this turn; desk is unavailable")
        if explicit and explicit != leased:
            raise DeskError(f"this turn leased {leased}; --slot {explicit} is in use by another job or not leased")
    slot = explicit or leased
    if not slot:
        raise DeskError(f"no desktop selected; use --slot one of {', '.join(SLOTS)}")
    configured = env.get("BOT_OFFICE_DESKTOPS")
    allowed = [name.strip() for name in configured.split(",")] if configured else [*SLOTS, leased]
    if slot not in allowed or not DESKTOP_NAME.fullmatch(slot):
        raise DeskError(f"unknown slot {slot!r}; expected one of {', '.join(name for name in allowed if name)}")
    return slot


OWNER_CONTROLLING = ("the owner is controlling this desktop right now; wait for the hand-back "
                     "(desk screenshot and desk open still work)")
OPEN_WAIT = 30.0


def _agent_desktop(environ: dict[str, str], body: dict, transport=None) -> tuple[int, dict]:
    from . import grok_bridge

    send = transport or grok_bridge._transport
    status, data = send(grok_bridge._api_url(environ) + "/v1/agent/desktop", body, environ["BOT_OFFICE_AGENT_TOKEN"])
    return status, data if isinstance(data, dict) else {}


def own_desktop(environ: dict[str, str], transport=None, command: str | None = None,
                args: list[str] | None = None) -> tuple[str, bool] | None:
    """Inside a turn: this bot's own desktop (started on demand by the controller) and whether the owner is
    controlling it. None when per-bot desktops are off (then the leased pool desktop is used). 0.12.47: the
    command and a digest of its input go along (session limit, loop stop); the text itself never does."""
    body: dict = {}
    if command:
        body["command"] = command
        body["input"] = hashlib.sha256("\0".join(args or []).encode()).hexdigest()[:32]
    status, data = _agent_desktop(environ, body, transport)
    detail = data.get("detail")
    if status == 409 and detail == "per-bot desktops are off":
        return None
    if status != 200:
        raise DeskError(detail if isinstance(detail, str) else f"no desktop (Bot-Office API HTTP {status})")
    name = data.get("name")
    if not isinstance(name, str) or not DESKTOP_NAME.fullmatch(name):
        raise DeskError("the controller answered with an invalid desktop name")
    return name, bool(data.get("owner_controlling"))


def leased_controlling(environ: dict[str, str], slot: str, transport=None) -> bool:
    """Whether the owner is controlling the pool desktop this turn leased (asked, never assumed)."""
    status, data = _agent_desktop(environ, {"slot": slot}, transport)
    if status != 200:
        detail = data.get("detail")
        raise DeskError(detail if isinstance(detail, str) else f"desktop state unavailable (HTTP {status})")
    return bool(data.get("owner_controlling"))


def pick_desktop(explicit: str | None, environ: dict[str, str], transport=None, command: str | None = None,
                 args: list[str] | None = None) -> tuple[str, bool]:
    """(desktop, owner_controlling). In a turn without --slot (or with --slot naming the bot's own desktop)
    the bot's own desktop; --slot <leased pool desktop> and operator shells keep the old rules."""
    in_turn = bool(environ.get("BOT_OFFICE_JOB_ID") and environ.get("BOT_OFFICE_AGENT_TOKEN"))
    leased = environ.get("BOT_OFFICE_SLOT") or environ.get("AGENTDESK_SLOT") or ""
    if not in_turn:
        return resolve_slot(explicit, environ), False
    if not (explicit and explicit == leased):
        own = own_desktop(environ, transport, command, args)
        if own is not None:
            if explicit and explicit != own[0]:
                raise DeskError(f"your own desktop is {own[0]}; --slot {explicit} is not yours"
                                + (f" (this turn also leased {leased})" if leased else ""))
            return own
    slot = resolve_slot(explicit, environ)   # inside a turn: only the leased pool desktop
    return slot, leased_controlling(environ, slot, transport)


def _exec(slot: str, *command: str, detach: bool = False) -> list[str]:
    return ["podman", "exec", *(["-d"] if detach else []), slot, "env", DISPLAY, *command]


def build_commands(slot: str, command: str, args: list[str]) -> list[list[str]]:
    if command == "click":
        if len(args) != 2 or not all(a.isdigit() for a in args):
            raise DeskError("click needs integer X and Y")
        return [_exec(slot, "xdotool", "mousemove", args[0], args[1], "click", "1")]
    if command == "type":
        text = _require(" ".join(args), "text")
        return [_exec(slot, "xdotool", "type", "--", text)]
    if command == "key":
        if len(args) != 1:
            raise DeskError("key needs exactly one KEY")
        return [_exec(slot, "xdotool", "key", _require(args[0], "key"))]
    if command == "open":
        if len(args) != 1:
            raise DeskError("open needs exactly one URL")
        parsed = urlparse(_require(args[0], "url"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise DeskError("only http and https URLs are allowed")
        return [_exec(slot, "epiphany", args[0], detach=True)]
    raise DeskError(f"unknown command {command!r}")


BLANK_TITLES = {"", "Blank page", "Web", "Epiphany"}


def browser_title(slot: str, run=subprocess.run) -> str | None:
    """The visible browser window's title (None: no window yet)."""
    probe = _exec(slot, "xdotool", "search", "--onlyvisible", "--class", "epiphany", "getwindowname", "%@")
    try:
        done = run(probe, capture_output=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out = done.stdout.decode(errors="replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
    titles = [line.strip() for line in out.splitlines() if line.strip()]
    return titles[-1] if titles else ""


def wait_for_browser(slot: str, before: str | None = None, timeout: float = OPEN_WAIT, run=subprocess.run,
                     sleep=None) -> bool:
    """After ``desk open``: wait until the browser shows a page title that is not blank and differs from the
    one before the open (the new page loaded), so a following screenshot shows it."""
    import time

    sleep = sleep or time.sleep
    deadline = time.monotonic() + timeout
    while True:
        title = browser_title(slot, run)
        if title is not None and title not in BLANK_TITLES and title != before:
            sleep(1.0)   # the title arrives with the document; give the first paint a moment
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)


def screenshot_commands(slot: str, dest: Path, tmp_name: str) -> list[list[str]]:
    remote = f"/tmp/{tmp_name}"
    return [
        _exec(slot, "import", "-window", "root", remote),
        ["podman", "cp", f"{slot}:{remote}", str(dest)],
        ["podman", "exec", slot, "rm", "-f", remote],
    ]


def screenshot_dest(arg: str | None, cwd: Path) -> Path:
    dest = (cwd / (arg or "screenshot.png")).resolve()
    if cwd.resolve() not in dest.parents:
        raise DeskError("outfile must be inside the workspace")
    return dest


def message_target(args: list[str], environ: dict[str, str], inbox_root: Path) -> tuple[Path, dict[str, str]]:
    if len(args) < 2:
        raise DeskError("message needs BOT and TEXT")
    target = _require(args[0], "bot")
    text = _require(" ".join(args[1:]), "text")
    try:
        directory = str(uuid.UUID(target))
    except ValueError:
        directory = "_by-name"
    payload = {"to": target, "text": text}
    bot_id = environ.get("BOT_OFFICE_BOT_ID") or environ.get("AGENTDESK_BOT_ID")
    if bot_id:
        payload["from_bot_id"] = bot_id
        payload["job_id"] = environ.get("BOT_OFFICE_JOB_ID", "")
        if environ.get("BOT_OFFICE_AGENT_TOKEN"):
            payload["agent_token"] = environ["BOT_OFFICE_AGENT_TOKEN"]
    return inbox_root / directory, payload


def write_inbox(directory: Path, payload: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = directory / f"{uuid.uuid4().hex}.json"
    partial = directory / f".{final.name}.tmp"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            json.dump(payload, stream)
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return final


def _run(argv: list[str]) -> None:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if result.returncode != 0:
        raise DeskError((result.stderr or result.stdout).strip() or f"{argv[0]} failed")


def _in_turn() -> bool:
    return bool(os.environ.get("BOT_OFFICE_JOB_ID") and os.environ.get("BOT_OFFICE_AGENT_TOKEN"))


def _report_shot(dest: Path, slot_arg: str | None) -> None:
    """The screenshot's digest to the controller (the no-progress stop of the bot's own desktop); best
    effort, never the image. Not for the leased pool desktop (--slot)."""
    leased = os.environ.get("BOT_OFFICE_SLOT") or os.environ.get("AGENTDESK_SLOT") or ""
    if not _in_turn() or (slot_arg and slot_arg == leased):
        return
    try:
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:32]
        _agent_desktop(dict(os.environ), {"shot": digest})
    except Exception:  # noqa: BLE001 — a lost report never fails the screenshot
        return


def _extend(rest: list[str]) -> int:
    """desk extend --reason TEXT: one more step for this bot's desktop session (asked, never automatic)."""
    if len(rest) < 2 or rest[0] != "--reason":
        raise DeskError('extend needs --reason "<why, and what is left>"')
    if not _in_turn():
        raise DeskError("desk extend works only inside a bot's turn")
    from . import grok_bridge

    status, data = grok_bridge._transport(grok_bridge._api_url(dict(os.environ)) + "/v1/agent/desktop/extend",
                                          {"reason": " ".join(rest[1:])[:300]}, os.environ["BOT_OFFICE_AGENT_TOKEN"])
    detail = data.get("detail") if isinstance(data, dict) else None
    if status != 200:
        raise DeskError(detail if isinstance(detail, str) else f"extend failed (Bot-Office API HTTP {status})")
    print(f"Desktop {data.get('name')}: {detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        slot_arg = None
        if args[:1] == ["--slot"]:
            if len(args) < 2:
                raise DeskError(USAGE)
            slot_arg, args = args[1], args[2:]
        if not args:
            raise DeskError(USAGE)
        command, rest = args[0], args[1:]
        if command == "say":
            from .say import main as say_main

            return say_main(rest)
        if command == "ask":
            from .ask import main as ask_main

            return ask_main(rest)
        if command == "team":
            from .team_client import main as team_main

            return team_main(rest)
        if command == "attach":
            if len(rest) != 1:
                raise DeskError("attach needs exactly one file")
            from .outgoing_images import attach_file

            try:
                path = attach_file(rest[0])
            except ValueError as exc:
                raise DeskError(str(exc)) from exc
            print(f"File queued for reply: {path}")
            return 0
        if command == "message":
            if os.environ.get("BOT_OFFICE_AGENT_TOKEN") and os.environ.get("BOT_OFFICE_BOT_ID"):
                # inside a job: the agent API, like bot-msg (the sender is the token's bot)
                from urllib.parse import quote

                from . import grok_bridge
                mode = "steer"
                if rest and rest[0] in ("--queue", "--steer"):
                    mode, rest = rest[0][2:], rest[1:]
                if len(rest) < 2:
                    raise DeskError("message needs BOT and TEXT")
                body = {"to": _require(rest[0], "bot"), "body": _require(" ".join(rest[1:]), "text"), "mode": mode}
                path = f"/v1/bots/{quote(os.environ['BOT_OFFICE_BOT_ID'], safe='')}/message-agent"
                result = grok_bridge.call_api(path, body)
                note = f" {result['note']}" if result.get("note") else ""
                print(f"Message {result.get('id')}: {result.get('status')}.{note}")
                return 0
            inbox = first_env("BOT_OFFICE_INBOX_DIR", "AGENTDESK_INBOX_DIR")
            if not inbox:
                raise DeskError("BOT_OFFICE_INBOX_DIR (or AGENTDESK_INBOX_DIR) is not set")
            directory, payload = message_target(rest, dict(os.environ), Path(inbox))
            write_inbox(directory, payload)
            return 0
        if command == "duet":
            from . import duet_bridge
            return duet_bridge.desk_duet(rest)
        if command in {"grok", "inbox"}:
            if command == "inbox" and os.environ.get("BOT_OFFICE_JOB_ID"):
                raise DeskError("use bot-msg or desk message for agent messages")
            from . import grok_bridge
            data_raw = first_env("BOT_OFFICE_DATA_DIR", "AGENTDESK_DATA_DIR")
            data_dir = Path(data_raw) if data_raw else None
            if command == "grok":
                return grok_bridge.desk_grok(rest, data_dir=data_dir)
            return grok_bridge.desk_inbox(rest, data_dir=data_dir)
        if command == "extend":
            return _extend(rest)
        if command in ("click", "key") and "--sends" in rest:
            # 0.12.47: the bot marked this click or key as sending or posting on a website; the gate asked the
            # owner (policy.py) and only then is it run; the flag and its text are not part of the input
            index = rest.index("--sends")
            rest = rest[:index] + rest[index + 2:]
        if command not in ("screenshot", "click", "type", "key", "open"):
            raise DeskError(f"unknown command {command!r}")
        slot, controlling = pick_desktop(slot_arg, dict(os.environ), command=command, args=rest)
        if controlling and command in ("click", "type", "key"):
            raise DeskError(OWNER_CONTROLLING)
        if command == "screenshot":
            if len(rest) > 1:
                raise DeskError("screenshot takes at most one outfile")
            dest = screenshot_dest(rest[0] if rest else None, job_cwd())
            commands = screenshot_commands(slot, dest, f"botoffice-{uuid.uuid4().hex}.png")
            try:
                _run(commands[0])
                _run(commands[1])
            finally:
                subprocess.run(commands[2], capture_output=True, timeout=TIMEOUT, check=False)
            _report_shot(dest, slot_arg)
            print(f"Screenshot of {slot} saved to {dest}")
        else:
            before = browser_title(slot) if command == "open" else None
            for argv_ in build_commands(slot, command, rest):
                _run(argv_)
            if command == "open":
                print(f"Opened on {slot}: the page has loaded." if wait_for_browser(slot, before) else
                      f"Asked {slot} to open the page; it is still loading (take a screenshot in a few seconds).")
    except (DeskError, subprocess.SubprocessError, OSError) as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
