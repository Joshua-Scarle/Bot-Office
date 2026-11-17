"""Symlink-safe file I/O for the controller in the bot-writable shared tree (0.11.0 review, Sec 1).

With isolation on, the bots user can create, rename and replace entries in ``workspaces/``,
``bots/``, ``shared/`` and ``inboxes/``. A planted symlink there must never make the controller
read a private file (the token into a prompt or argv) or write through it (overwrite the DB).

Every helper here walks from a registered root (the data dir, which the bots user cannot
change) to the target one component at a time with ``O_NOFOLLOW`` and ``dir_fd``, and opens the
final name with ``O_NOFOLLOW``: a symlink anywhere below the root is refused (``UnsafePath``),
never followed. Reads also refuse anything that is not a regular file (a FIFO cannot block the
controller). Writes are atomic: a fresh temp file (``O_EXCL``) renamed over the name inside the
same directory fd, so the rename replaces a planted link instead of writing through it. Paths
outside every root are opened with ``O_NOFOLLOW`` on the final component only.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import fnmatch
import logging
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("botoffice.safefs")

_roots: tuple[Path, ...] = ()
_roots_lock = threading.Lock()
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(OSError):
    """A symlink (or a non-regular file) where the controller expects its own file or dir."""


def add_root(path: Path | str) -> None:
    """Trust *path* itself (an operator-chosen dir the bots cannot replace); below it nothing is
    followed."""
    global _roots
    root = Path(os.path.abspath(path))
    with _roots_lock:
        if root not in _roots:
            # replaced, never mutated: readers iterate a consistent tuple without the lock
            _roots = tuple(sorted((*_roots, root), key=lambda item: len(item.parts), reverse=True))


def _split(path: Path | str) -> tuple[Path, tuple[str, ...]]:
    """(trusted anchor, components below it) for *path*."""
    full = Path(os.path.abspath(path))
    for root in _roots:
        if full == root:
            return root, ()
        if root in full.parents:
            return root, full.relative_to(root).parts
    return full.parent, (full.name,)


def _unsafe(path: Path | str, exc: OSError) -> OSError:
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
        return UnsafePath(exc.errno, f"refusing a symlink or non-directory at {path}")
    return exc


def _walk(anchor: Path, parts: tuple[str, ...], create: bool, mode: int, path: Path | str) -> int:
    """fd of the directory anchor/parts..., each component opened without following links."""
    if create:
        os.makedirs(anchor, mode, exist_ok=True)  # the trusted anchor (a root, or a path outside all roots)
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise UnsafePath(errno.EINVAL, f"refusing path component {part!r} in {path}")
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode, dir_fd=fd)
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise _unsafe(path, exc) from exc
                with contextlib.suppress(OSError):
                    os.fchmod(next_fd, mode)  # mkdir applies the umask; setgid/sticky need fchmod
            except OSError as exc:
                raise _unsafe(path, exc) from exc
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_dir(path: Path | str, *, create: bool = False, mode: int = 0o700) -> int:
    anchor, parts = _split(path)
    return _walk(anchor, parts, create, mode, path)


@contextlib.contextmanager
def _parent(path: Path | str, create: bool = False, mode: int = 0o700) -> Iterator[tuple[int, str]]:
    anchor, parts = _split(path)
    if not parts:  # a root itself: trusted, like its parent
        anchor, parts = anchor.parent, (anchor.name,)
    fd = _walk(anchor, parts[:-1], create, mode, path)
    try:
        yield fd, parts[-1]
    finally:
        os.close(fd)


def mkdir(path: Path | str, mode: int = 0o700) -> None:
    """Create *path* (and parents) if missing and set *mode* on it, never through a link."""
    os.close(open_dir(path, create=True, mode=mode))
    fd = open_dir(path)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def lstat(path: Path | str) -> os.stat_result | None:
    try:
        with _parent(path) as (fd, name):
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except UnsafePath:
        return None


def is_file(path: Path | str) -> bool:
    info = lstat(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def is_dir(path: Path | str) -> bool:
    info = lstat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def exists(path: Path | str) -> bool:
    """Anything (a link included) at *path*."""
    return lstat(path) is not None


def _open_file(path: Path | str, flags: int, mode: int = 0o600) -> int:
    with _parent(path) as (fd, name):
        try:
            return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd)
        except OSError as exc:
            raise _unsafe(path, exc) from exc


def read_bytes(path: Path | str, limit: int | None = None) -> bytes:
    """The bytes of the regular file at *path* (at most *limit*); raises FileNotFoundError or
    UnsafePath."""
    fd = _open_file(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UnsafePath(errno.EINVAL, f"not a regular file: {path}")
        return stream.read() if limit is None else stream.read(limit)


def read_text(path: Path | str, default: str = "", limit: int | None = None) -> str:
    """Text of the regular file at *path*; *default* if it is missing, a link or not a file."""
    try:
        data = read_bytes(path, None if limit is None else limit * 4)
    except (FileNotFoundError, NotADirectoryError):
        return default
    except UnsafePath:
        log.warning("refused to read %s: a symlink or not a regular file", path)
        return default
    text = data.decode("utf-8", errors="replace")
    return text if limit is None else text[:limit]


def write_bytes(path: Path | str, data: bytes, mode: int = 0o600, *, dir_mode: int | None = None,
                fsync: bool = False) -> None:
    """Atomically replace *path* with *data* (mode *mode*); parents are created with *dir_mode*
    (default: none are created). A link at *path* is replaced, never written through."""
    with _parent(path, create=dir_mode is not None, mode=dir_mode or 0o700) as (dfd, name):
        tmp = f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), mode)
                if fsync:
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        if fsync:
            os.fsync(dfd)


def write_text(path: Path | str, text: str, mode: int = 0o600, *, dir_mode: int | None = None,
               fsync: bool = False) -> None:
    write_bytes(path, text.encode("utf-8"), mode, dir_mode=dir_mode, fsync=fsync)


def append_text(path: Path | str, text: str, mode: int = 0o600) -> None:
    fd = _open_file(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UnsafePath(errno.EINVAL, f"not a regular file: {path}")
        stream.write(text)


def touch(path: Path | str, mode: int = 0o600, *, fsync: bool = False) -> None:
    """Create *path* if missing (a regular file; a link there is refused)."""
    fd = _open_file(path, os.O_WRONLY | os.O_CREAT, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePath(errno.EINVAL, f"not a regular file: {path}")
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    if fsync:
        fsync_dir(Path(path).parent)


def chmod(path: Path | str, mode: int) -> None:
    """chmod the file or dir itself; a link is refused (never its target)."""
    try:
        fd = _open_file(path, os.O_RDONLY | os.O_NONBLOCK)
    except IsADirectoryError:
        fd = open_dir(path)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def unlink(path: Path | str, missing_ok: bool = True) -> None:
    """Remove the name (a link is removed itself, never its target)."""
    try:
        with _parent(path) as (fd, name):
            os.unlink(name, dir_fd=fd)
    except FileNotFoundError:
        if not missing_ok:
            raise


def rename(src: Path | str, dst: Path | str) -> None:
    """Rename within one directory (os.replace semantics; links are moved, not followed)."""
    if Path(src).parent != Path(dst).parent:
        raise ValueError("safefs.rename stays within one directory")
    with _parent(src) as (fd, name):
        os.replace(name, Path(dst).name, src_dir_fd=fd, dst_dir_fd=fd)


def names(directory: Path | str, pattern: str = "*", *, regular: bool = True) -> list[str]:
    """Entry names in *directory* matching *pattern* (regular files only unless *regular* is False)."""
    try:
        fd = open_dir(directory)
    except (FileNotFoundError, NotADirectoryError, UnsafePath):
        return []
    try:
        found = []
        for name in os.listdir(fd):
            if not fnmatch.fnmatchcase(name, pattern):
                continue
            if regular:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
            found.append(name)
        return sorted(found)
    finally:
        os.close(fd)


def fsync_dir(path: Path | str) -> None:
    with contextlib.suppress(OSError):
        fd = open_dir(path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def lock(directory: Path | str, mode: int = 0o700) -> Iterator[None]:
    """An exclusive flock on ``<directory>/.lock`` (created without following links)."""
    os.close(open_dir(directory, create=True, mode=mode))
    fd = _open_file(Path(directory) / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
