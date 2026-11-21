from __future__ import annotations

import copy
import json
import shlex
from pathlib import Path

from ..compaction import COMPACT_POLICY, compact_token_limit
from .cli import CliAdapter

# Session-only settings (never saved): PreCompact stdout is appended to the compaction
# instructions; SessionStart(compact) prints DURABLE-STATE.md (memory.md capped, the open tasks, WORKING.md; written by
# Bot-Office into the job directory) after a compaction. A fixed read-only command, so it is safe in every access mode.
CLAUDE_SETTINGS = {
    "hooks": {
        "PreCompact": [{"hooks": [{"type": "command", "command": "printf '%s\\n' " + shlex.quote(COMPACT_POLICY)}]}],
        "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": "cat DURABLE-STATE.md 2>/dev/null || true"}]}],
    }
}


# tools an unattended (dontAsk) run may use without a prompt; MCP and anything else is refused
UNATTENDED_TOOLS = ("Bash", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
                    "WebFetch", "WebSearch", "TodoWrite", "Task", "Skill", "BashOutput", "KillShell")
# MCP tools are not listed: strict-profile runs (triggers; scheduled turns of a "gated" bot) refuse
# them; default ("full") scheduled runs keep them, as in 0.10.x (README, gen 9)


def claude_settings(job, full: bool) -> dict:
    """Per-invocation --settings: compaction hooks (every access mode), and the 0.11.0 action gate:
    a PreToolUse hook to bin/bot-office-gate plus deny rules for the protected paths (Claude
    documents deny rules as applying even under bypassPermissions)."""
    settings = copy.deepcopy(CLAUDE_SETTINGS)
    if job is None or not job.gate_command:
        return settings
    settings.setdefault("hooks", {})["PreToolUse"] = [
        {"matcher": "*", "hooks": [{"type": "command", "command": job.gate_command, "timeout": 30}]}
    ]
    deny = []
    for path in job.secret_paths:
        for target in (path, f"{path}/**"):
            deny += [f"Read(/{target})", f"Edit(/{target})"]
    permissions: dict = {"deny": deny}
    if job.unattended and full:
        permissions["allow"] = list(UNATTENDED_TOOLS)
    settings["permissions"] = permissions
    return settings


class ClaudeAdapter(CliAdapter):
    name = "claude"
    label = "Claude"
    executables = ("claude",)
    status_args = ("auth", "status")
    confine_home = False

    def _status_from_output(self, output: str, returncode: int) -> str:
        compact = output.replace(" ", "")
        if '"loggedin":false' in compact:
            return "needs_login"
        if returncode == 0 and '"loggedin":true' in compact:
            return "ok"
        return super()._status_from_output(output, returncode)

    supports_resume = True

    def steering(self, job) -> str:
        return "live"  # stream-json stdin stays open for the whole turn (CliAdapter._exec feed loop)

    def _exec_args(self, workspace: Path, job=None, session: str | None = None) -> list[str]:
        full = self._full_access(job)
        # unattended full-access runs drop bypass: dontAsk refuses anything not allowed above
        mode = ("dontAsk" if job is not None and job.unattended else "bypassPermissions") if full else "acceptEdits"
        settings = claude_settings(job, full)
        args = [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--replay-user-messages",
            "--permission-mode",
            mode,
            "--disallowedTools",
            "AskUserQuestion",
            "--permission-prompts",
            "none",
        ]
        if full:
            args += ["--autocompact", str(compact_token_limit())]
        if settings:
            args += ["--settings", json.dumps(settings)]
        if job is not None and job.bot_id is not None:
            args += ["--add-dir", str(self._bot_dir(workspace, job) / "attachments")]
            args += ["--add-dir", str(self._bot_dir(workspace, job) / "uploads")]  # files of any type the owner sent
        if session:
            args += ["--resume", session]
        model = self._job_model(job)
        if model:
            args += ["--model", model]
        effort = self._job_effort(job)
        if effort:
            args += ["--effort", effort]
        return args
