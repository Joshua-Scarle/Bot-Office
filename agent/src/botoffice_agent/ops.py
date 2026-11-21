"""What the agent does for one approved call. Every path is re-checked here, at execution."""
from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import os
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .denycmd import Checker
from .paths import DIR_FLAGS, LocalDenied, PathChanged, check_denied, open_parent, open_verified, resolve, under

LS_MAX_DEPTH = 3
LS_MAX_ENTRIES = 2000
READ_CHUNK_MAX = 1024 * 1024
READ_FILE_CAP = 16 * 1024 * 1024
RUN_TIMEOUT_DEFAULT = 30
RUN_TIMEOUT_MAX = 300
STREAM_CAP = 64 * 1024
TRANSFER_MAX = 100 * 1024 * 1024
SHOT_MAX = 5 * 1024 * 1024
CHUNK = 256 * 1024
KILL_GRACE = 2.0
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"


class Declined(Exception):
    """The person at this computer said no (or did not answer) to the confirm pop-up."""


@dataclass
class Context:
    home: str
    roots: list[str]
    sandbox: bool = False
    commands: Checker | None = None

    def check_command(self, argv, sh, cwd: str) -> None:
        """The command deny-list; it wins over any grant."""
        (self.commands or Checker(self.home, self.roots)).check(argv, sh, cwd)


def _arg_path(args: dict, key: str = "path") -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{key} must be an absolute canonical path")
    return value


def op_resolve(args: dict, ctx: Context) -> dict:
    kind = args.get("kind", "any")
    if kind not in ("dir", "file", "any", "parent"):
        raise ValueError("bad kind")
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path required")
    found = resolve(path, kind, ctx.home, ctx.roots)
    if "argv" in args or "sh" in args:  # run: the command deny-list too, before anyone is asked
        argv, sh = args.get("argv"), args.get("sh")
        if argv is not None and (not isinstance(argv, list) or not all(isinstance(item, str) for item in argv)):
            raise ValueError("argv must be a list of strings")
        ctx.check_command(argv, sh if argv is None else None, found["canonical"])
    return found


def op_ls(args: dict, ctx: Context) -> dict:
    root = _arg_path(args)
    depth = args.get("depth", 1)
    if not isinstance(depth, int) or not 1 <= depth <= LS_MAX_DEPTH:
        raise ValueError("depth is 1 to 3")
    entries: list[dict] = []
    truncated = False
    top = open_verified(root, ctx.roots, directory=True)

    def walk(fd: int, prefix: str, level: int) -> None:
        nonlocal truncated
        room = LS_MAX_ENTRIES - len(entries) + 1  # one more than fits tells us it was cut
        items = []
        with os.scandir(fd) as found:
            for entry in found:
                items.append(entry)
                if len(items) >= room:
                    break
        items.sort(key=lambda entry: entry.name)
        for entry in items:
            if len(entries) >= LS_MAX_ENTRIES:
                truncated = True
                return
            rel = f"{prefix}{entry.name}"
            full = os.path.join(root, rel)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(entry.name, dir_fd=fd)
                except OSError:
                    target = "?"
                entries.append({"path": rel, "type": "link", "size": 0, "target": target})
                continue
            kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
            entries.append({"path": rel + ("/" if kind == "dir" else ""), "type": kind, "size": info.st_size,
                            "mtime": int(info.st_mtime)})
            if kind == "dir" and level < depth and not any(under(full, denied) for denied in ctx.roots):
                try:
                    child = os.open(entry.name, DIR_FLAGS, dir_fd=fd)
                except OSError:
                    continue
                try:
                    walk(child, rel + "/", level + 1)
                finally:
                    os.close(child)

    try:
        walk(top, "", 1)
    finally:
        os.close(top)
    return {"entries": entries, "truncated": truncated}


def op_read(args: dict, ctx: Context) -> dict:
    path = _arg_path(args)
    offset, length = args.get("offset", 0), args.get("length", READ_CHUNK_MAX)
    if not isinstance(offset, int) or offset < 0 or not isinstance(length, int) or not 1 <= length <= READ_CHUNK_MAX:
        raise ValueError("bad offset or length")
    fd = open_verified(path, ctx.roots)
    try:
        size = os.fstat(fd).st_size
        if size > READ_FILE_CAP:
            raise ValueError("the file is over 16 MiB; use pull")
        data = os.pread(fd, length, offset)
    finally:
        os.close(fd)
    end = offset + len(data)
    if args.get("b64"):
        text, encoding = base64.b64encode(data).decode(), "base64"
    else:
        text, encoding = data.decode("utf-8", errors="replace"), "utf-8"
    return {"data": text, "encoding": encoding, "size": size, "eof": end >= size, "next_offset": end,
            "sha256": hashlib.sha256(data).hexdigest()}


def read_chunks(args: dict, ctx: Context):
    """pull: (fd, size) of the approved file; the caller streams it."""
    path = _arg_path(args)
    fd = open_verified(path, ctx.roots)
    size = os.fstat(fd).st_size
    if size > TRANSFER_MAX:
        os.close(fd)
        raise ValueError("the file is over 100 MiB")
    if isinstance(args.get("size"), int) and args["size"] != size:
        os.close(fd)
        raise PathChanged("the file changed size since the approval")
    return fd, size


class PushWriter:
    """push: a temp file next to the destination (dir fd, O_EXCL, O_NOFOLLOW), then an atomic
    rename (overwrite) or link (never overwrite)."""

    def __init__(self, args: dict, ctx: Context):
        self.path = _arg_path(args)
        self.size, self.sha256 = args.get("size"), args.get("sha256")
        self.overwrite = bool(args.get("overwrite"))
        if not isinstance(self.size, int) or not 0 <= self.size <= TRANSFER_MAX or not isinstance(self.sha256, str):
            raise ValueError("bad size or sha256")
        self.dir_fd, self.name = open_parent(self.path, ctx.roots)
        self.tmp = f".{self.name}.botoffice-{secrets.token_hex(6)}.tmp"
        try:
            self._check_target()
            self.fd = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                              dir_fd=self.dir_fd)
        except BaseException:
            os.close(self.dir_fd)
            raise
        self.hash = hashlib.sha256()
        self.written = 0

    def _check_target(self) -> None:
        try:
            info = os.stat(self.name, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PathChanged(f"{self.path} is not a regular file now")
        if not self.overwrite:
            raise FileExistsError(f"{self.path} exists (no overwrite)")

    def write(self, data: bytes) -> None:
        self.written += len(data)
        if self.written > self.size:
            raise ValueError("more data than approved")
        self.hash.update(data)
        view = memoryview(data)
        while view:
            done = os.write(self.fd, view)
            if done <= 0:
                raise OSError("short write")
            view = view[done:]

    def commit(self) -> dict:
        if self.written != self.size or self.hash.hexdigest() != self.sha256:
            raise ValueError("the content is not what was approved (sha256 or size)")
        os.fsync(self.fd)
        if os.fstat(self.fd).st_size != self.size:
            raise OSError("the written file is not the approved size")
        os.close(self.fd)
        self.fd = -1
        self._check_target()
        if self.overwrite:
            os.replace(self.tmp, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        else:
            os.link(self.tmp, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd, follow_symlinks=False)
            os.unlink(self.tmp, dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)
        return {"path": self.path, "size": self.written, "sha256": self.sha256}

    def close(self) -> None:
        if getattr(self, "fd", -1) >= 0:
            os.close(self.fd)
            self.fd = -1
        try:
            os.unlink(self.tmp, dir_fd=self.dir_fd)
        except OSError:
            pass
        if self.dir_fd >= 0:
            os.close(self.dir_fd)
            self.dir_fd = -1


# --- run ---

def minimal_env(home: str) -> dict:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return {"PATH": SAFE_PATH, "HOME": home, "USER": user, "LOGNAME": user, "LANG": "C.UTF-8"}


def bwrap_prefix(cwd: str, roots: list[str], bwrap: str, cwd_fd: int) -> list[str]:
    """Read-only view of the whole computer, a private /tmp, no network, deny-listed paths hidden,
    killed with the agent. Nothing is writable except /tmp (in memory, gone after the command).
    The working folder is the verified fd bound over its canonical path, so a swap of the path
    after the check cannot move the command elsewhere."""
    argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--unshare-net", "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session",
            "--ro-bind-fd", str(cwd_fd), cwd]
    for root in roots:
        try:
            info = os.lstat(root)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            argv += ["--tmpfs", root]
        elif stat.S_ISREG(info.st_mode):
            argv += ["--ro-bind", "/dev/null", root]
    return argv + ["--chdir", cwd, "--"]


@functools.cache
def bwrap_works(bwrap: str) -> bool:
    """Once per agent run: can bwrap build the sandbox on this computer at all?"""
    fd = os.open("/tmp", DIR_FLAGS)
    try:
        done = subprocess.run(bwrap_prefix("/tmp", [], bwrap, fd) + ["true"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=20, pass_fds=(fd,))
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.close(fd)
    return done.returncode == 0


def _drain(stream, cap: int, out: dict, key: str) -> None:
    kept = bytearray()
    truncated = False
    while block := stream.read(65536):
        room = cap - len(kept)
        if room > 0:
            kept += block[:room]
        if len(block) > room:
            truncated = True
    out[key] = bytes(kept)
    out[key + "_truncated"] = truncated


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill(process: subprocess.Popen) -> None:
    """TERM, then KILL, to the command's whole process group (start_new_session made it its own)."""
    pgid = process.pid
    for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if process.poll() is not None and not _group_alive(pgid):
                break
            time.sleep(0.05)
        else:
            continue
        break
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(5)


def op_run(args: dict, ctx: Context, cancelled: threading.Event | None = None) -> dict:
    cwd = _arg_path(args, "cwd")
    argv, sh = args.get("argv"), args.get("sh")
    timeout = args.get("timeout", RUN_TIMEOUT_DEFAULT)
    if not isinstance(timeout, int) or not 1 <= timeout <= RUN_TIMEOUT_MAX:
        raise ValueError("timeout is 1 to 300 seconds")
    if argv is not None:
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise ValueError("argv must be a list of strings")
        command = list(argv)
    elif isinstance(sh, str) and sh:
        command = ["/bin/sh", "-c", sh]
    else:
        raise ValueError("argv or sh required")
    ctx.check_command(argv if argv is not None else None, sh if argv is None else None, cwd)  # again, at execution
    cwd_fd = open_verified(cwd, ctx.roots, directory=True)
    try:
        if ctx.sandbox:
            bwrap = shutil.which("bwrap")
            if not bwrap:
                raise LocalDenied("sandboxing is on but bwrap is not installed; refused")
            if not bwrap_works(bwrap):
                raise LocalDenied("sandboxing is on but bwrap cannot create a sandbox here (are unprivileged "
                                  "user namespaces restricted?); refused")
            command = bwrap_prefix(cwd, ctx.roots, bwrap, cwd_fd) + command
            where = {"cwd": "/", "pass_fds": (cwd_fd,)}
        else:
            # the verified fd, not the path: a swap after the check cannot move the command elsewhere
            where = {"cwd": f"/proc/self/fd/{cwd_fd}", "pass_fds": (cwd_fd,)}
        if cancelled is not None and cancelled.is_set():
            raise Declined("cancelled before it started")
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=minimal_env(ctx.home), start_new_session=True, close_fds=True, **where)
        except FileNotFoundError as exc:
            return {"exit": 127, "timed_out": False, "stdout": "", "stderr": f"not found: {exc.filename}",
                    "stdout_truncated": False, "stderr_truncated": False, "sandboxed": ctx.sandbox}
    finally:
        os.close(cwd_fd)
    out: dict = {}
    readers = [threading.Thread(target=_drain, args=(process.stdout, STREAM_CAP, out, "stdout"), daemon=True),
               threading.Thread(target=_drain, args=(process.stderr, STREAM_CAP, out, "stderr"), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = stopped = False
    try:
        while process.poll() is None:
            if time.monotonic() - started > timeout:
                timed_out = True
                break
            if cancelled is not None and cancelled.is_set():
                stopped = True
                break
            time.sleep(0.05)
    finally:
        _kill(process)  # the whole process group, also children left behind by a command that exited
    for reader in readers:
        reader.join(5)
    stdout, stderr = out.get("stdout", b""), out.get("stderr", b"")
    return {"exit": None if (timed_out or stopped) else process.returncode, "timed_out": timed_out,
            "cancelled": stopped, "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"), "stdout_truncated": out.get("stdout_truncated", False),
            "stderr_truncated": out.get("stderr_truncated", False), "sandboxed": ctx.sandbox,
            "sha256": hashlib.sha256(stdout + b"\0" + stderr).hexdigest()}


def take_shot() -> bytes:
    """A PNG of the screen with grim, gnome-screenshot or ImageMagick import, if one is installed."""
    with tempfile.TemporaryDirectory() as folder:
        target = os.path.join(folder, "shot.png")
        for command in (["grim", target], ["gnome-screenshot", "-f", target], ["import", "-window", "root", target]):
            if shutil.which(command[0]) is None:
                continue
            try:
                subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
                with open(target, "rb") as handle:
                    data = handle.read(SHOT_MAX + 1)
            except (OSError, subprocess.SubprocessError):
                continue
            if len(data) > SHOT_MAX:
                raise ValueError("the screenshot is over 5 MiB")
            return data
    raise FileNotFoundError("no screenshot tool (grim, gnome-screenshot or import) worked")


def confirm_with(command: list[str], timeout: int) -> Callable[[str], bool]:
    """A yes/no pop-up: exit 0 is yes; anything else, a missing program or no answer is no."""
    def ask(text: str) -> bool:
        argv = [item.replace("{text}", text) for item in command]
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0
    return ask


__all__ = ["Context", "Declined", "LocalDenied", "PathChanged", "PushWriter", "check_denied", "confirm_with",
           "op_ls", "op_read", "op_resolve", "op_run", "read_chunks", "take_shot"]
