"""Path rules: canonical paths for the approval card, and a symlink-proof open at execution.

1. ``resolve`` (before the approval) expands ``~``, resolves every symlink with ``realpath`` and
   checks the deny-list on both the spelling and the canonical result. Bot Office shows and binds
   the canonical path, so ``..`` and symlink tricks are visible on the card.
2. At execution ``open_verified`` walks the canonical path from ``/`` one component at a time with
   ``O_NOFOLLOW`` directory fds. A symlink anywhere (planted after the approval) fails the walk;
   the opened fd is then checked against the canonical path again through ``/proc/self/fd``.
   The deny-list is checked again too. It always wins over an approval.
"""
from __future__ import annotations

import errno
import os
import stat

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class LocalDenied(Exception):
    """Refused by this computer's deny-list."""


class PathChanged(Exception):
    """The path is not what was approved any more (a symlink or a rename since)."""


def expand(path: str, home: str) -> str:
    """``~`` and relative paths are under the home directory; the result is absolute, not resolved."""
    if "\x00" in path:
        raise ValueError("path contains NUL")
    if path == "~":
        path = home
    elif path.startswith("~/"):
        path = os.path.join(home, path[2:])
    elif not path.startswith("/"):
        path = os.path.join(home, path)
    return os.path.normpath(path) if not path.startswith("//") else "/" + path.lstrip("/")


def deny_roots(entries: list[str], home: str) -> list[str]:
    roots: set[str] = set()
    for entry in entries:
        full = os.path.normpath(expand(entry, home))
        roots.add(full)
        roots.add(os.path.realpath(full))
    return sorted(roots)


def under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


def check_denied(path: str, roots: list[str]) -> None:
    for root in roots:
        if under(path, root):
            raise LocalDenied(f"{path} is on this computer's deny-list")


def resolve(path: str, kind: str, home: str, roots: list[str]) -> dict:
    """{canonical, exists, type, size} for *path*; *kind* is dir, file, any or parent (push: the
    folder must exist, the name may not)."""
    spelled = expand(path, home)
    check_denied(spelled, roots)
    if kind == "parent":
        name = os.path.basename(spelled)
        if name in ("", ".", "..") or "/" in name:
            raise ValueError("the destination needs a file name")
        parent = os.path.realpath(os.path.dirname(spelled), strict=True)
        if not os.path.isdir(parent):
            raise NotADirectoryError(errno.ENOTDIR, "not a folder", parent)
        canonical = os.path.join(parent, name)
        check_denied(canonical, roots)
        try:
            info = os.lstat(canonical)
        except FileNotFoundError:
            return {"canonical": canonical, "exists": False, "type": "missing", "size": 0}
        if stat.S_ISLNK(info.st_mode):
            raise PathChanged(f"{canonical} is a symlink; push writes regular files only")
        if not stat.S_ISREG(info.st_mode):
            raise IsADirectoryError(errno.EISDIR, "not a regular file", canonical)
        return {"canonical": canonical, "exists": True, "type": "file", "size": info.st_size}
    canonical = os.path.realpath(spelled, strict=True)
    check_denied(canonical, roots)
    info = os.stat(canonical)
    kind_found = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
    if kind == "dir" and kind_found != "dir":
        raise NotADirectoryError(errno.ENOTDIR, "not a folder", canonical)
    if kind == "file" and kind_found != "file":
        raise IsADirectoryError(errno.EISDIR, "not a regular file", canonical)
    return {"canonical": canonical, "exists": True, "type": kind_found, "size": info.st_size}


def _fd_path(fd: int) -> str | None:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _walk(parts: list[str]) -> int:
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise PathChanged("not a canonical path")
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PathChanged("a symlink or file is in the path now; it changed since the approval") from exc
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_parent(canonical: str, roots: list[str]) -> tuple[int, str]:
    """(fd of the parent folder, final name), walked without following any symlink."""
    if not canonical.startswith("/") or os.path.normpath(canonical) != canonical or canonical == "/":
        raise PathChanged("not a canonical path")
    check_denied(canonical, roots)
    parent, name = os.path.split(canonical)
    fd = _walk([part for part in parent.split("/") if part])
    seen = _fd_path(fd)
    if seen is not None and seen != parent:
        os.close(fd)
        raise PathChanged(f"{parent} moved since the approval")
    return fd, name


def open_verified(canonical: str, roots: list[str], *, directory: bool = False) -> int:
    """An fd on exactly *canonical*, or PathChanged / LocalDenied / OSError."""
    if canonical == "/":
        check_denied(canonical, roots)
        return os.open("/", DIR_FLAGS)
    parent_fd, name = open_parent(canonical, roots)
    try:
        flags = DIR_FLAGS if directory else os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PathChanged(f"{canonical} is a symlink now; it changed since the approval") from exc
            raise
    finally:
        os.close(parent_fd)
    info = os.fstat(fd)
    if not directory and not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise PathChanged(f"{canonical} is not a regular file")
    seen = _fd_path(fd)
    if seen is not None and seen != canonical:
        os.close(fd)
        raise PathChanged(f"{canonical} moved since the approval")
    return fd
