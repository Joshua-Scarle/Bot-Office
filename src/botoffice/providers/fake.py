from __future__ import annotations

import asyncio
from pathlib import Path

from ..domain import Job
from .base import RunResult


class FakeAdapter:
    name = "codex"

    def __init__(
        self,
        login: str = "ok",
        result: str = "succeeded",
        delay: float = 0,
        enabled: bool = True,
    ):
        self.login = login
        self.result = result
        self.delay = delay
        self.enabled = enabled
        self.started = 0

    async def login_status(self) -> str:
        return self.login if self.enabled else "disabled"

    async def run(self, job: Job, workspace: Path, log_path: Path, timeout: int) -> RunResult:
        self.started += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.result == "needs_login":
            log_path.write_text("Fake provider needs login\n", encoding="utf-8")
            return RunResult("needs_login", error="not logged in")
        if self.result == "failed":
            log_path.write_text("Fake provider failed\n", encoding="utf-8")
            return RunResult("failed", exit_code=1, error="fake failure")
        log_path.write_text(f"Fake run for {job.id}\n", encoding="utf-8")
        return RunResult("succeeded", exit_code=0)
