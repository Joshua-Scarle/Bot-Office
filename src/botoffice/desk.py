from __future__ import annotations

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
    "usage: desk [--slot SLOT] screenshot [outfile] | click X Y | type TEXT | key KEY"
    " | open URL | message BOT TEXT | attach FILE\n"
    "       desk grok send --to NAME --kind question|result|blocker|fyi --text TEXT"
    " [--thread ID] [--from NAME]\n"
    "       desk grok install-config\n"
    "       desk inbox post --to BOT --from NAME --text TEXT [--thread ID] [--hop N]"
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
        if command in {"grok", "inbox"}:
            if command == "inbox" and os.environ.get("BOT_OFFICE_JOB_ID"):
                raise DeskError("use bot-msg or desk message for agent messages")
            from . import grok_bridge
            data_raw = first_env("BOT_OFFICE_DATA_DIR", "AGENTDESK_DATA_DIR")
            data_dir = Path(data_raw) if data_raw else None
            if command == "grok":
                return grok_bridge.desk_grok(rest, data_dir=data_dir)
            return grok_bridge.desk_inbox(rest, data_dir=data_dir)
        slot = resolve_slot(slot_arg)
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
        else:
            for argv_ in build_commands(slot, command, rest):
                _run(argv_)
    except (DeskError, subprocess.SubprocessError, OSError) as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
