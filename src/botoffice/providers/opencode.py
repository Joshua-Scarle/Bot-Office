from __future__ import annotations

import os
import time
from pathlib import Path

from .. import failures
from ..catalog import DEFAULT_MODELS, model_error, parse_model_listing
from ..domain import Job
from .base import RunResult
from .cli import EXIT_IDLE, CliAdapter


class OpenCodeAdapter(CliAdapter):
    """Run the official OpenCode CLI; session IDs are not credentials."""

    name = "opencode"
    label = "OpenCode"
    executables = ("opencode",)
    status_args = ("auth", "list")
    # `opencode models` prints one provider/model id per line (no chat run)
    list_models_args = ("models",)
    confine_home = False
    # OpenCode 1.19.5 `run -s <session>` hangs without output (and a session stays bound to its
    # first directory), so every turn is a fresh session carrying the compact handoff context
    # (scheduler: engines that cannot resume). No session id is saved.
    supports_resume = False
    # A message sent while OpenCode works steers it by restart (0.11.7, CliAdapter.steering "restart"):
    # the run is interrupted and started again as the same job with the same flags, the job's prompt,
    # a note of the steps it had taken (text and tool calls from its stream) and the message. Because
    # `run -s` cannot be used, no session is resumed and the model keeps only that note, not its context.
    # a hung run prints nothing at all: stop it instead of waiting for the job timeout
    idle_timeout: float | None = 60.0

    def _remember_models(self, output: str) -> None:
        models = [(mid, label) for mid, label in parse_model_listing(output) if not model_error(self.name, mid)]
        if models:
            self._models_cache = (time.monotonic(), models)

    def _auth_env(self) -> dict[str, str]:
        # Pass provider API-key environment variables directly to the official CLI.
        return {key: value for key, value in os.environ.items() if key.endswith("_API_KEY")}

    async def login_status(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.executable:
            return "error"
        # Auth may also be supplied to the CLI by its provider environment.
        # A model run is the authoritative authentication check.
        return "ok"

    def _exec_args(self, workspace: Path, job: Job | None = None, session: str | None = None) -> list[str]:
        assert job is not None
        args = ["run", "--format", "json", "-m", self._job_model(job) or DEFAULT_MODELS[self.name],
                "--dir", str(workspace)]
        if self._full_access(job):
            args.append("--auto")
        if effort := self._job_effort(job):
            args.extend(["--variant", effort])
        args.extend(f"--file={path}" for path in job.attachment_paths)
        args.extend(["--", job.prompt])
        return args

    def _stdin(self, job: Job) -> None:
        return None

    def _failed(self, code, stderr, parser, error=None):
        if code == EXIT_IDLE:
            return super()._failed(code, stderr, parser, self._idle_message())
        error = error or parser.error or stderr.decode(errors="replace").strip()
        kind = failures.classify(error, code)
        if kind != failures.USAGE_LIMIT and any(term in error.lower() for term in ("auth", "credential", "api key", "login")):
            return RunResult("needs_login", exit_code=code, error="OpenCode authentication failed; run opencode auth login or configure the provider", final_text=parser.final_text, failure_kind=failures.AUTH)
        return super()._failed(code, stderr, parser, error or "OpenCode run failed")

    async def run(self, job: Job, workspace: Path, log_path: Path, timeout: int) -> RunResult:
        if not self.enabled:
            return RunResult("failed", error="OpenCode provider is disabled")
        if not self.executable:
            return RunResult("failed", error="OpenCode executable not found on PATH")
        return await super().run(job, workspace, log_path, timeout)
