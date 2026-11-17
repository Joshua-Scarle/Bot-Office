"""Find and stop provider processes left over from an interrupted job (restart recovery).

An interrupted job is replayed only with proof that nothing of its earlier execution still
runs (``recovery_proof``); without proof it fails closed (chat notice and a Retry button).

1. The proof, the service cgroup: under systemd (``KillMode=control-group``) every
   process the old controller started was killed before this one started, so the
   controller's own cgroup holds only this controller and its descendants. Without a
   readable service cgroup (a shell, an SSH session scope) nothing is replayed.
2. The job marker, for stopping leftovers: every provider process (and everything it
   starts) inherits ``BOT_OFFICE_JOB_ID=<job id>``; marked processes are stopped at startup
   in every case, and a marked survivor still blocks its job. Each process is re-verified
   (marker and start time, via a pidfd) right before it is signalled, so a recycled pid is
   never hit. A marker scan is not proof: a child may have wiped its own environment.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import isolation

PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class Scan:
    """Marked processes as (pid, start time); *complete* is False if any process of this
    user could not be read, so the absence of a marker proves nothing."""
    found: tuple[tuple[int, int], ...]
    complete: bool


def _starttime(pid: int, proc: Path = PROC) -> int | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # field 22, counted after the ")" that closes the command name (which may contain spaces)
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _has_marker(pid: int, marker: bytes, proc: Path = PROC) -> bool | None:
    """True/False, or None if the environment cannot be read."""
    try:
        environ = (proc / str(pid) / "environ").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return marker in environ.split(b"\0")


def _zombie(pid: int, proc: Path = PROC) -> bool:
    """Exited but not yet reaped (runs nothing), or already gone."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return True
    return stat[stat.rfind(")") + 2:][:1] in ("Z", "X")


def _same_user(entry: Path) -> bool:
    with contextlib.suppress(OSError):
        return entry.stat().st_uid == os.getuid()
    return False


def scan(job_id: str, proc: Path = PROC) -> Scan:
    marker = f"BOT_OFFICE_JOB_ID={job_id}".encode()
    found: list[tuple[int, int]] = []
    complete = True
    try:
        entries = list(proc.iterdir())
    except OSError:
        return Scan((), False)
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        has = _has_marker(pid, marker, proc)
        if has is None:
            if _same_user(entry) and not _zombie(pid, proc):
                complete = False  # ours but unreadable (e.g. made non-dumpable): cannot rule it out
            continue
        if has:
            start = _starttime(pid, proc)
            if start is not None:
                found.append((pid, start))
    return Scan(tuple(found), complete)


def job_processes(job_id: str, proc: Path = PROC) -> list[int]:
    return [pid for pid, _ in scan(job_id, proc).found]


def _signal_verified(pid: int, start: int, marker: bytes, sig: int, proc: Path = PROC) -> bool:
    """Signal *pid* only if it is still the process that was scanned (same start time, same
    job marker), checked through a pidfd so the pid cannot be recycled in between.
    Returns False on a permission error."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (OSError, AttributeError):
        fd = None
    try:
        if _starttime(pid, proc) != start or not _has_marker(pid, marker, proc):
            return True  # gone, or a different process now
        if fd is not None:
            signal.pidfd_send_signal(fd, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    finally:
        if fd is not None:
            os.close(fd)
    return True


def stop_job_processes(job_id: str, grace: float = 3.0, proc: Path = PROC) -> bool:
    """SIGTERM then SIGKILL every marked process of *job_id*. True once a complete scan
    finds none (a scan that could not read every process of this user is not proof)."""
    launcher = isolation.active()
    if launcher is not None and proc == PROC:
        # isolated layout: the job's processes belong to the bots user; the launcher stops them
        return launcher.stop_job(job_id)
    marker = f"BOT_OFFICE_JOB_ID={job_id}".encode()
    current = scan(job_id, proc)
    if not current.found:
        return current.complete
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        for pid, start in current.found:
            if not _signal_verified(pid, start, marker, sig, proc):
                return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            current = scan(job_id, proc)
            if not current.found:
                return current.complete
            time.sleep(0.05)
    current = scan(job_id, proc)
    return not current.found and current.complete


def own_cgroup(proc: Path = PROC, root: Path = CGROUP_ROOT) -> Path | None:
    """This process's cgroup v2 directory, if it is a systemd service cgroup."""
    try:
        lines = (proc / "self" / "cgroup").read_text().splitlines()
    except OSError:
        return None
    path = next((line[3:] for line in lines if line.startswith("0::")), None)
    if not path or not path.rstrip("/").endswith(".service"):
        return None  # not under a service unit (a shell, a scope, a test run): no proof here
    directory = root / path.lstrip("/")
    return directory if (directory / "cgroup.procs").is_file() else None


def _ppid(pid: int, proc: Path) -> int | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    try:
        return int(stat[stat.rfind(")") + 2:].split()[1])
    except (IndexError, ValueError):
        return None


def _descends_from_self(pid: int, proc: Path) -> bool:
    me = os.getpid()
    seen = set()
    while pid not in seen and pid > 1:
        if pid == me:
            return True
        seen.add(pid)
        parent = _ppid(pid, proc)
        if parent is None:
            return False
        pid = parent
    return False


def _cgroup_pids(directory: Path) -> Iterable[int] | None:
    pids: list[int] = []
    try:
        for cgroup_procs in [directory / "cgroup.procs", *directory.rglob("*/cgroup.procs")]:
            pids += [int(line) for line in cgroup_procs.read_text().split()]
    except (OSError, ValueError):
        return None
    return pids


def cgroup_clean(proc: Path = PROC, root: Path = CGROUP_ROOT) -> bool | None:
    """True if this controller's service cgroup holds nothing but the controller and its own
    descendants (leftovers of an earlier run would be orphans), False if it holds anything
    else, None if there is no service cgroup to check."""
    directory = own_cgroup(proc, root)
    if directory is None:
        return None
    pids = _cgroup_pids(directory)
    if pids is None:
        return None
    return all(_descends_from_self(pid, proc) for pid in pids)


def recovery_proof(job_ids: Iterable[str], proc: Path = PROC, root: Path = CGROUP_ROOT) -> set[str]:
    """The interrupted jobs that may be replayed: none of their earlier processes can still run.

    Marked leftovers are stopped first. Only the systemd service cgroup can prove the rest:
    when it holds nothing but this controller and its own descendants, every job is proven.
    Without that proof (no service cgroup, unreadable, anything else left in it) none is: a
    marker scan alone cannot see a child that dropped its environment."""
    job_ids = list(job_ids)
    stopped = {job_id for job_id in job_ids if stop_job_processes(job_id, proc=proc)}
    if cgroup_clean(proc, root) is not True:
        return set()
    # a marked process that survived elsewhere (moved out of the cgroup) still blocks its job
    if isolation.active() is not None and proc == PROC:
        return stopped  # other-user processes are invisible to scan(); the launcher's answer counts
    return {job_id for job_id in job_ids if not scan(job_id, proc).found}
