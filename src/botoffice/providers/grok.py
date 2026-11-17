from __future__ import annotations

from pathlib import Path

from ..domain import Job
from .base import RunResult


class GrokAdapter:
    name = "grok"
    enabled = False

    async def login_status(self) -> str:
        return "disabled"

    async def run(self, job: Job, workspace: Path, log_path: Path, timeout: int) -> RunResult:
        raise NotImplementedError("Grok provider is disabled")
