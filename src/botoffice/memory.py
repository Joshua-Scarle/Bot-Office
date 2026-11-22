from __future__ import annotations

import difflib
import shutil
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from . import freshness, isolation, safefs

MAX_MEMORY_BYTES = 1_000_000
WORKLOG_LINES = 300
# difflib's matcher is quadratic: above these sizes the line diff is skipped (merge_fallback)
MERGE_MAX_BYTES = 256_000
MERGE_MAX_LINES = 4000
# time budget of one merge: off the event loop (prepare_merge), and under the lock on the
# synchronous path; past it the merge is abandoned and the job's version kept aside
MERGE_SECONDS = 2.0
MERGE_SYNC_SECONDS = 0.2
_merge_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-merge")

Merge = tuple[str, list[str]] | None  # None: no merge could be established (see merge_write)


def _hunks(base: list[str], other: list[str]) -> list[tuple[int, int, list[str]]]:
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _apply(base: list[str], start: int, end: int, hunks: list[tuple[int, int, list[str]]]) -> list[str]:
    out, at = [], start
    for i1, i2, lines in sorted(hunks, key=lambda hunk: (hunk[0], hunk[1])):
        out += base[at:i1] + lines
        at = i2
    return out + base[at:end]


def too_large(*texts: str) -> bool:
    return any(len(text) > MERGE_MAX_BYTES or text.count("\n") > MERGE_MAX_LINES for text in texts)


def merge3(base: str, current: str, ours: str) -> Merge:
    """Line-based three-way merge of *ours* (a job's edit of *base*) into *current* (the file
    now). Non-overlapping changes from both sides are kept; where both changed the same lines,
    *current* wins and our version of those lines is returned as a conflict. None above the
    size cap (no diff is run)."""
    if current == base or current == ours:
        return ours, []
    if ours == base:
        return current, []
    if too_large(base, current, ours):
        return None
    b, c, o = (text.splitlines(keepends=True) for text in (base, current, ours))
    for lines in (b, c, o):
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    tagged = sorted([(*hunk, "c") for hunk in _hunks(b, c)] + [(*hunk, "o") for hunk in _hunks(b, o)],
                    key=lambda hunk: (hunk[0], hunk[1]))
    merged: list[str] = []
    conflicts: list[str] = []
    at, index = 0, 0
    while index < len(tagged):
        group = [tagged[index]]
        start, end = tagged[index][0], tagged[index][1]
        index += 1
        # hunks touching the same base lines (or inserting at the same spot) form one group
        while index < len(tagged) and (tagged[index][0] < end or tagged[index][0] == start
                                       or tagged[index][0] == end and tagged[index][1] == end == start):
            end = max(end, tagged[index][1])
            group.append(tagged[index])
            index += 1
        merged += b[at:start]
        theirs = [(i1, i2, lines) for i1, i2, lines, side in group if side == "c"]
        mine = [(i1, i2, lines) for i1, i2, lines, side in group if side == "o"]
        current_part = _apply(b, start, end, theirs)
        ours_part = _apply(b, start, end, mine)
        if theirs and mine and all(i1 == i2 for i1, i2, *_ in group):
            # both only added lines at the same spot (e.g. two notes appended): keep both; a line
            # of ours is dropped only as often as current already has it (repeats survive)
            unused = Counter(current_part)
            extra = []
            for line in ours_part:
                if unused[line]:
                    unused[line] -= 1
                else:
                    extra.append(line)
            merged += current_part + extra
        else:
            merged += current_part if theirs else ours_part
        if theirs and mine and ours_part != current_part and not all(i1 == i2 for i1, i2, *_ in group):
            conflicts.append("".join(ours_part).strip())
        at = end
    merged += b[at:]
    return "".join(merged), [text for text in conflicts if text]


def merge_bounded(base: str, current: str, ours: str, seconds: float = MERGE_SECONDS) -> Merge:
    """merge3 within a time budget; None if it did not finish in time."""
    if current in (base, ours) or ours == base:
        return merge3(base, current, ours)
    future = _merge_pool.submit(merge3, base, current, ours)
    try:
        return future.result(timeout=seconds)
    except FutureTimeout:
        future.cancel()  # still queued: never runs; running: bounded by the size cap
        return None


def _dir_mode(path: Path) -> int:
    # bots/<id> and the shared memory dir: sticky when isolated (see isolation.shared_dir_mode)
    return isolation.shared_dir_mode() if path.parent.name == "bots" or path.name in ("bots", "shared") \
        else isolation.dir_mode()


def _private_dir(path: Path) -> None:
    """Create/chmod a dir in the shared tree without following a planted link (Sec 1)."""
    safefs.mkdir(path, _dir_mode(path))


def _atomic_write(path: Path, text: str) -> None:
    """Replace *path* atomically; a link at *path* or above it (below the data dir) is refused or
    replaced, never written through."""
    _private_dir(path.parent)
    safefs.write_text(path, text, isolation.file_mode())


class Memory:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        safefs.add_root(data_dir)

    def bot_dir(self, bot_id: UUID | str) -> Path:
        return self.data_dir / "bots" / str(bot_id)

    def bot_path(self, bot_id: UUID | str) -> Path:
        return self.bot_dir(bot_id) / "memory.md"

    def delete_bot_dir(self, bot_id: UUID) -> None:
        """Remove ``data/bots/<id>/`` only. Shared memory and other bots are untouched."""
        root = (self.data_dir / "bots").resolve()
        target = self.data_dir / "bots" / str(UUID(str(bot_id)))
        if target.is_symlink():
            target.unlink()
            return
        if not target.exists():
            return
        if target.resolve().parent != root:
            raise ValueError("refusing to delete outside the bots directory")
        shutil.rmtree(target)

    @property
    def shared_path(self) -> Path:
        return self.data_dir / "shared" / "memory.md"

    @staticmethod
    def _read(path: Path) -> str:
        """The file's text; "" when missing, or when it is a link or not a regular file (a bot
        could otherwise point memory.md at the token and get it into its prompt)."""
        return safefs.read_text(path)

    def read_bot(self, bot_id: UUID | str) -> str:
        return self._read(self.bot_path(bot_id))

    def _write_dated(self, path: Path, text: str, who: str) -> None:
        """Replace *path* (caller holds its lock) and date the notes that changed (freshness)."""
        before, mtime = self._read(path), freshness._mtime(path)
        _atomic_write(path, text)
        freshness.record(path, before, text, who, mtime)

    def write_bot(self, bot_id: UUID | str, text: str, who: str = freshness.OWNER) -> None:
        _private_dir(self.data_dir / "bots")
        with self._lock(self.bot_dir(bot_id)):
            self._write_dated(self.bot_path(bot_id), text, who)

    def prepare_merge(self, path: Path, base: str, ours: str) -> tuple[str, Merge]:
        """The merge against the file as it is now, computed without the lock (run it off the
        event loop); merge_write reuses it if the file has not changed since."""
        current = self._read(path)
        return current, merge_bounded(base, current, ours)

    def merge_write(self, path: Path, base: str, ours: str, job_id: UUID | str,
                    prepared: tuple[str, Merge] | None = None, who: str = "a job") -> tuple[str, Path | None]:
        """Write a job's edit of *path* (made from *base*) without losing edits that other jobs
        or the GUI saved meanwhile: a three-way merge under the file's lock. Lines both sides
        changed keep the newer file's text; the job's version is appended under a marker.
        If the file changed after *prepared* was computed (or nothing was prepared), the merge
        runs here within MERGE_SYNC_SECONDS. When no merge can be established (too large, over
        the time budget), the file is kept as it is and the job's whole version is saved next
        to it as ``<name>.conflict-<time>.md``, referenced under a dated heading. Returns the
        file's text and that conflict file, if any."""
        with self._lock(path.parent):
            current = self._read(path)
            if prepared is not None and prepared[0] == current:
                merge = prepared[1]
            else:
                merge = merge_bounded(base, current, ours, MERGE_SYNC_SECONDS)
            conflict = None
            if merge is None:
                moment = datetime.now(UTC)
                conflict = path.with_name(f"{path.stem}.conflict-{moment:%Y%m%dT%H%M%S%fZ}.md")
                _atomic_write(conflict, ours)
                merged = (current.rstrip("\n") + f"\n\n## Unmerged edit from {who} ({moment:%Y-%m-%d %H:%M} UTC)\n"
                          f"This version could not be merged safely; it is kept in `{conflict.name}` "
                          "next to this file.\n").lstrip("\n")
            else:
                merged, conflicts = merge
                for text in conflicts:
                    merged = merged.rstrip("\n") + f"\n\n<!-- merged from job {job_id} -->\n{text}\n"
            if merged != current:
                mtime = freshness._mtime(path)
                _atomic_write(path, merged)
                freshness.record(path, current, merged, who, mtime)
            return merged, conflict

    def worklog_path(self, bot_id: UUID | str) -> Path:
        return self.bot_dir(bot_id) / "worklog.md"

    def read_worklog(self, bot_id: UUID | str) -> str:
        return self._read(self.worklog_path(bot_id))

    def append_worklog(self, bot_id: UUID | str, line: str) -> None:
        """One line per finished job; only the last WORKLOG_LINES are kept."""
        path = self.worklog_path(bot_id)
        with self._lock(path.parent):
            lines = self._read(path).splitlines()[-(WORKLOG_LINES - 1):]
            _atomic_write(path, "\n".join([*lines, " ".join(line.split())]) + "\n")

    def working_path(self, bot_id: UUID | str) -> Path:
        return self.bot_dir(bot_id) / "WORKING.md"

    def read_working(self, bot_id: UUID | str) -> str:
        return self._read(self.working_path(bot_id))

    def write_working(self, bot_id: UUID | str, text: str, who: str = "Bot-Office") -> None:
        _private_dir(self.data_dir / "bots")
        # no lock (as before 0.12.24): its callers hold the chat lock, and a lock failure must not drop a reply
        self._write_dated(self.working_path(bot_id), text, who)

    def ensure_shared(self) -> None:
        if not self.shared_path.exists():
            self.write_shared("")

    def read_shared(self) -> str:
        return self._read(self.shared_path)

    @staticmethod
    @contextmanager
    def _lock(directory: Path) -> Iterator[None]:
        _private_dir(directory)
        with safefs.lock(directory, _dir_mode(directory)):
            yield

    def write_shared(self, text: str, who: str = freshness.OWNER) -> None:
        with self._lock(self.shared_path.parent):
            self._write_dated(self.shared_path, text, who)
