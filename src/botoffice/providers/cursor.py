from __future__ import annotations

from pathlib import Path

from .. import isolation, safefs
from ..domain import Job
from .cli import CliAdapter

PROMPT_POINTER = "Read prompt.txt in the current workspace and follow it."


class CursorAdapter(CliAdapter):
    name = "cursor"
    label = "Cursor"
    executables = ("cursor-agent", "agent")
    status_args = ("status",)
    list_models_args = ("models",)
    confine_home = False

    supports_resume = True
    tracks_tools = True

    def _can_resume(self, job) -> bool:
        # resuming needs the stable per-bot --workspace; restricted bots keep the job workspace (a message
        # to one still steers it: the run is started again there with the same flags, see steering())
        return super()._can_resume(job) and self._full_access(job)

    def _tool_signal(self, obj):
        if obj.get("type") != "tool_call":
            return None
        key = str(obj.get("call_id") or "")
        if obj.get("subtype") == "started":
            return "start", key
        if obj.get("subtype") in ("completed", "finished"):
            return "end", key
        return None

    def _restart_exec_args(self, workspace: Path, job: Job, suffix: str) -> list[str]:
        args = self._exec_args(workspace, job, None)
        if args[-1] == PROMPT_POINTER:  # the prompt is the file in the workspace: add the note after it
            args[-1] = PROMPT_POINTER + suffix
        return args

    def _exec_args(self, workspace: Path, job=None, session: str | None = None) -> list[str]:
        stable = self._can_resume(job)
        if stable:
            chat_dir = self._bot_dir(workspace, job) / "cursor-workspace"
            safefs.mkdir(chat_dir, isolation.dir_mode())  # never through a planted link
        self._write_gate_files(chat_dir if stable else workspace, job)
        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--trust",
            *(["--force", "--sandbox", "disabled"] if self._full_access(job) else []),
            "--workspace",
            str(chat_dir if stable else workspace),
        ]
        if session:
            args += ["--resume", session]
        model = self._job_model(job)
        if model:
            args += ["--model", model]
        if stable:
            pointer = (f"This turn's job directory is {workspace} (also the shell's working directory). "
                       f"Read {workspace / ('prompt-resume.txt' if session else 'prompt.txt')} and follow it; reply.txt, history.md and the "
                       "other files it names are in that job directory.")
            return [*args, pointer]
        return [*args, PROMPT_POINTER]

    def _write_gate_files(self, directory: Path, job: Job | None) -> None:
        """0.11.0 action gate in the CLI's own project files: deny rules for protected paths
        (.cursor/cli.json; deny beats --force) and hooks to bin/bot-office-gate (.cursor/hooks.json)."""
        if job is None or not job.gate_command:
            return
        deny = [f"{tool}({target})" for path in job.secret_paths for target in (path, f"{path}/**")
                for tool in ("Read", "Write")]
        self._write_json(directory / ".cursor" / "cli.json", {"permissions": {"allow": [], "deny": deny}})
        hook = [{"command": job.gate_command}]
        self._write_json(directory / ".cursor" / "hooks.json", {"version": 1, "hooks": {
            "beforeShellExecution": hook, "beforeMCPExecution": hook, "beforeReadFile": hook}})

    def _stdin(self, job: Job) -> bytes | None:
        return None

    def _steer_exec_args(self, workspace: Path, job: Job, session: str | None) -> list[str]:
        args = self._exec_args(workspace, job, session)
        args[-1] = job.prompt
        return args
