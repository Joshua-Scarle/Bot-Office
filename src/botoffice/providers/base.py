from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..domain import Job


@dataclass
class RunResult:
    status: str
    exit_code: int | None = None
    error: str | None = None
    final_text: str | None = None  # final reply from the CLI's stream, if it reported one
    # failures.classify kind of a failed run, the provider's stated reset time (usage
    # limits) and the token usage the CLI reported
    failure_kind: str | None = None
    retry_at: datetime | None = None
    usage: dict | None = None
    # A reply file left by an interrupted invocation is not the resumed invocation's reply.
    reply_file_stale: bool = False


class ProviderAdapter(Protocol):
    name: str
    enabled: bool

    async def login_status(self) -> str: ...

    async def run(self, job: Job, workspace: Path, log_path: Path, timeout: int) -> RunResult: ...
