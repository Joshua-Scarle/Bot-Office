from __future__ import annotations

import contextlib
from pathlib import Path

from .. import isolation, safefs
from ..domain import Job
from .cli import NEEDS_LOGIN_TERMS, CliAdapter, archive_session


class AntigravityAdapter(CliAdapter):
    name = "antigravity"
    label = "Antigravity"
    executables = ("agy",)
    status_args = ("models",)
    list_models_args = ("models",)
    confine_home = False

    def _status_from_output(self, output: str, returncode: int) -> str:
        if any(term in output for term in NEEDS_LOGIN_TERMS) or "please sign in" in output:
            return "needs_login"
        if returncode == 0 and output.strip():
            return "ok"
        return "error"

    supports_resume = True
    STABLE_MARKER = ".stable"

    def _can_resume(self, job) -> bool:
        # a conversation is bound to the directory it was created in, so resuming needs the stable
        # per-bot working directory; restricted bots keep the job workspace and start fresh each turn
        return super()._can_resume(job) and self._full_access(job)

    def _stable_dir(self, workspace: Path, job: Job | None) -> Path | None:
        if not self._can_resume(job):
            return None
        directory = self._bot_dir(workspace, job) / "antigravity-workspace"
        # shared tree: created and marked without following a planted link; isolated, it is sticky
        # like a job workspace, so the bot (the CLI's user) works in it but cannot swap .agents/hooks.json
        safefs.mkdir(directory, isolation.workspace_mode())
        marker = directory / self.STABLE_MARKER
        if not safefs.is_file(marker):
            safefs.unlink(marker)  # a planted link is removed, never followed
            safefs.touch(marker)
        return directory

    def _cwd(self, workspace: Path, job: Job | None = None) -> Path:
        return self._stable_dir(workspace, job) or workspace

    def _saved_session(self, workspace: Path, job: Job) -> str | None:
        session = super()._saved_session(workspace, job)
        if session and not safefs.is_file(self._bot_dir(workspace, job) / "antigravity-workspace" / self.STABLE_MARKER):
            # saved before the stable directory existed: that conversation is bound to an old job
            # directory, so keep the pointer for audit and start a fresh conversation
            with contextlib.suppress(OSError):
                archive_session(self._bot_dir(workspace, job), self.name, "cwd-migrate")
            self._stable_dir(workspace, job)
            return None
        return session

    def _exec_args(self, workspace: Path, job: Job | None = None, session: str | None = None) -> list[str]:
        if job is not None and job.gate_command:
            # 0.11.0 action gate: PreToolUse hook in the job workspace (the CLI's cwd)
            # in the CLI's working directory (the stable per-bot dir for resumable bots, 0.10.6)
            self._write_json(self._cwd(workspace, job) / ".agents" / "hooks.json", {"hooks": {"PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": job.gate_command, "timeout": 30}]}]}})
        prompt = job.prompt if job is not None else ""
        if self._stable_dir(workspace, job):
            prompt = (f"This turn's job directory is {workspace}. Read "
                      f"{workspace / ('prompt-resume.txt' if session else 'prompt.txt')} and follow it; reply.txt, "
                      "history.md and the other files it names are in that job directory. Where it says the "
                      f"current directory, use {workspace}: the shell starts in a different folder, so cd there "
                      "or use absolute paths.")
        args = [
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            *(["--dangerously-skip-permissions"] if self._full_access(job) else ["--mode", "accept-edits"]),
        ]
        if session:
            args += ["--conversation", session]
        model = self._job_model(job)
        if model:
            args += ["--model", model]
        effort = self._job_effort(job)
        if effort:
            args += ["--effort", effort]
        return args

    def _stdin(self, job: Job) -> bytes | None:
        return None

    def _steer_exec_args(self, workspace: Path, job: Job, session: str | None) -> list[str]:
        args = self._exec_args(workspace, job, session)
        args[args.index("-p") + 1] = job.prompt  # the steering text itself, not the pointer
        return args
