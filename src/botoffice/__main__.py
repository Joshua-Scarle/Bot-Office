import os
import secrets
import sys

import uvicorn

from .config import Settings

USAGE = """usage: python -m botoffice                      run the server
       python -m botoffice probe-gate PROVIDER     test the action gate of one CLI (2 small model calls)
       python -m botoffice gate-status             show enforced / advisory / none per provider
       python -m botoffice rotate-token [--show]   replace the master token (printed only with --show)
       python -m botoffice isolation-status        show whether the isolation layout is installed"""


def rotate_token(settings: Settings, show: bool = False) -> int:
    """Write a fresh master token (0600, atomically) to BOT_OFFICE_TOKEN_FILE. The token is printed
    only with --show. The controller reads the file on every request, so the old token stops
    working at once. Run as root, the new file keeps the old file's owner (the controller must
    still read it)."""
    target = settings.token_file
    if target is None:
        print("BOT_OFFICE_TOKEN_FILE is not set; nothing to rotate", file=sys.stderr)
        return 2
    token = secrets.token_urlsafe(32)
    try:
        previous = os.stat(target, follow_symlinks=False)
    except OSError:
        previous = None
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        if previous is not None and os.geteuid() == 0:
            os.chown(tmp, previous.st_uid, previous.st_gid, follow_symlinks=False)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print(f"new master token written to {target}; it is in effect now (the old one no longer works)")
    if show:
        print(token)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    settings = Settings.from_env()
    if not argv:
        uvicorn.run("botoffice.api:app", host=settings.host, port=settings.port)
        return 0
    command = argv[0]
    if command == "rotate-token" and argv[1:] in ([], ["--show"]):
        return rotate_token(settings, show=argv[1:] == ["--show"])
    if command == "isolation-status" and len(argv) == 1:
        from . import isolation

        isolation.configure(settings)
        for key, value in isolation.status(settings).items():
            print(f"{key}: {value}")
        return 0
    if command == "probe-gate" and len(argv) == 2:
        from . import isolation
        from .api import _default_providers
        from .probe import probe

        isolation.configure(settings)  # probe the CLI the way the server runs it
        if isolation.layout_error():
            print(f"isolation layout incomplete: {isolation.layout_error()}", file=sys.stderr)
            return 2
        adapter = _default_providers(settings).get(argv[1])
        if adapter is None or not getattr(adapter, "executable", None):
            print(f"unknown or missing provider CLI: {argv[1]}", file=sys.stderr)
            return 2
        state, detail = probe(adapter, settings.control_dir)
        print(f"{argv[1]}: {state} ({detail})")
        return 0 if state == "enforced" else 1
    if command == "gate-status" and len(argv) == 1:
        from .rules import WIRED, gate_state

        for name in WIRED:
            print(f"{name}: {gate_state(settings.control_dir, name)}")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
