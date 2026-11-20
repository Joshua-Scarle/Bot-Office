"""Record files (images or any other type) for the controller to attach when the current turn finishes.

The sidecar keeps its 0.12 name so turns and bots already running keep working."""
from __future__ import annotations

import os
from pathlib import Path

SIDECAR = ".bot-office-outgoing-images"


def job_cwd() -> Path:
    """Where relative names resolve: the current directory, or the job directory when the CLI runs
    in a stable directory of its own (BOT_OFFICE_WORKSPACE, e.g. Antigravity)."""
    cwd = Path.cwd()
    job_dir = os.environ.get("BOT_OFFICE_WORKSPACE")
    if job_dir and Path(job_dir).is_dir():
        job = Path(job_dir).resolve()
        if job != cwd.resolve() and job not in cwd.resolve().parents:
            return job
    return cwd


def find_workspace(*starts: Path) -> Path:
    """The job workspace: the nearest directory at or above a start holding prompt.txt."""
    for start in starts:
        for folder in (start, *start.parents):
            if (folder / "prompt.txt").is_file():
                return folder
    raise ValueError("not inside a Bot-Office job workspace (no prompt.txt found)")


def attach_file(filename: str, workspace: Path | None = None) -> Path:
    """Record an existing file as an absolute path in the workspace sidecar."""
    if not filename or "\n" in filename or "\r" in filename or "\x00" in filename:
        raise ValueError("attach needs a valid file path")
    cwd = workspace or job_cwd()
    path = (cwd / filename).resolve(strict=True)
    if "\n" in str(path) or "\r" in str(path):
        raise ValueError("attach needs a file path without newlines")
    if not path.is_file():
        raise ValueError(f"not a file: {filename}")
    workspace_root = find_workspace(cwd.resolve(), path.parent)
    with (workspace_root / SIDECAR).open("a", encoding="utf-8") as stream:
        stream.write(f"{path}\n")
    return path
