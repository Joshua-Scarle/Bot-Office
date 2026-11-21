"""Session reset (0.12.20): what "a clean session" means, beyond forgetting the CLI's session id.

``POST /v1/bots/{id}/session/reset`` records a context floor in SQLite (store.reset_context: the newest
message and job, atomically with the no-queued-or-running-job check). From then on the prompt assembler
(workspace.py) shows nothing at or before the floor: no recent messages, no history.md, no seed, no
earlier jobs in SELF.md. The files that carry the earlier work are archived here, never deleted, into
``bots/<id>/reset-archive/<generation>-<time>/``: every engine's session pointer, worklog.md, WORKING.md,
the stable working directories of the engines that keep one (Cursor, Antigravity) and the image index.
memory.md, instructions.md, the task list and the owner's chat stay as they are.

The SQLite row commits first and the files follow here, before every job of the bot (scheduler) and right
after the reset (api), all under one lock: idempotent and crash-safe, the same pattern as
scheduler.reconcile_session. A failure raises and leaves the cleanup owed, so the bot's next job fails
instead of running on the old state.
"""
from __future__ import annotations

import contextlib
import os
import stat
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from . import attachments, isolation, safefs
from .providers.cli import SESSION_ENGINES, archive_session

# per-bot files and directories (name in data/bots/<id>/) that hold an earlier session's work
ARCHIVED = ("worklog.md", "WORKING.md", "cursor-workspace", "antigravity-workspace")

_lock = threading.Lock()


def _move(source_dir: int, name: str, target_dir: int) -> None:
    """Move *name* between two directories, both held open as descriptors that were opened without following
    a link: nothing is resolved by path again, so swapping a path component meanwhile changes nothing here.
    A link itself is moved, never what it points at; the target directory is new, so nothing is replaced."""
    try:
        os.stat(name, dir_fd=source_dir, follow_symlinks=False)
    except FileNotFoundError:
        return
    os.rename(name, name, src_dir_fd=source_dir, dst_dir_fd=target_dir)


def _open(name: str, parent: int, *, create: bool = False) -> int:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, isolation.dir_mode(), dir_fd=parent)
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def reconcile_context(store, memory, bot_id, engines: Iterable[str] = ()) -> list[str]:
    """Finish an owed reset cleanup (idempotent). Returns the engines whose saved session it archived.
    Raises OSError, leaving the cleanup owed, if something could not be moved."""
    if store.context_cleanup_pending(bot_id) is None:
        return []
    bot_dir = memory.bot_dir(bot_id)
    archived: list[str] = []
    with _lock, memory._lock(bot_dir):  # this process, and (file lock) any other one
        # a generation another caller finished while we waited is skipped; a newer one that arrived meanwhile is done too
        while (gen := store.context_cleanup_pending(bot_id)) is not None:
            archived += _clean(store, memory, bot_id, bot_dir, gen, engines)
    return archived


def _clean(store, memory, bot_id, bot_dir: Path, gen: int, engines: Iterable[str]) -> list[str]:
    state = isolation.state_dir(bot_dir)
    archived = []
    # every engine's saved session (a bot can switch engines); a link is removed, never followed
    for engine in sorted({*SESSION_ENGINES, *engines}):
        pointer = state / f"{engine}-session"
        info = safefs.lstat(pointer)
        if info is not None and stat.S_ISLNK(info.st_mode):
            safefs.unlink(pointer)
        elif archive_session(bot_dir, engine, f"reset-{gen}-{time.time_ns()}"):
            archived.append(engine)
    fds: list[int] = []
    try:
        bot_fd = safefs.open_dir(bot_dir)
        fds.append(bot_fd)
        root_fd = _open("reset-archive", bot_fd, create=True)
        fds.append(root_fd)
        name = f"{gen}-{time.time_ns()}"
        os.mkdir(name, isolation.dir_mode(), dir_fd=root_fd)  # exclusive: a name that exists is an error
        archive_fd = _open(name, root_fd)
        fds.append(archive_fd)
        if os.listdir(archive_fd):  # the directory just made was swapped for another one: never move into it
            raise OSError(f"reset archive {name} was replaced during the cleanup")
        for item in ARCHIVED:
            _move(bot_fd, item, archive_fd)
        # INDEX.md lists the earlier messages' text and images
        try:
            attachments_fd = safefs.open_dir(attachments.directory(store.path.parent, bot_id))
        except FileNotFoundError:
            attachments_fd = None
        except ValueError as exc:  # attachments.directory refused the path (a link in it): owed, not diverted
            raise OSError(f"attachment directory refused: {exc}") from exc
        if attachments_fd is not None:
            fds.append(attachments_fd)
            _move(attachments_fd, attachments.INDEX_FILE, archive_fd)
        if state != bot_dir:  # isolated: the session pointers live in a private directory
            fds.append(safefs.open_dir(state))
        for fd in fds:
            os.fsync(fd)  # the moves are on disk before the cleanup is recorded as done
    finally:
        for fd in fds:
            os.close(fd)
    store.complete_context_cleanup(bot_id, gen)
    return archived
