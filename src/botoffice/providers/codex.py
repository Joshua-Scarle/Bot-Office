from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from .. import isolation, safefs
from ..catalog import DEFAULT_MODELS
from ..compaction import COMPACT_POLICY, compact_token_limit
from .cli import CliAdapter, Segment

REJECTED_FLAG_PHRASES = (
    "unexpected argument",
    "unexpected option",
    "unrecognized option",
    "unknown option",
)


def hook_overrides(job) -> list[str]:
    """Only the PreToolUse hook to bin/bot-office-gate (no sandbox or approval change): attended
    full-access Codex keeps its bypass flag and still stops on the git guard (0.12.4)."""
    if not job.gate_command:
        return []
    hook = json.dumps(job.gate_command)
    return ["-c", "features.codex_hooks=true",
            "-c", f'hooks.PreToolUse=[{{matcher="*",hooks=[{{type="command",command={hook},timeout=30}}]}}]']


def gate_overrides(job) -> list[str]:
    """0.11.0 per-invocation profile for an unattended full-access run (never ~/.codex/config.toml):
    Codex's own workspace-write sandbox with no approvals, network only if a rule allows it, and a
    PreToolUse hook to bin/bot-office-gate. Verified per host by scripts/probe_gate.sh codex."""
    hook = json.dumps(job.gate_command or "")
    return [
        "-c", 'sandbox_mode="workspace-write"',
        "-c", 'approval_policy="never"',
        "-c", f"sandbox_workspace_write.network_access={'true' if job.allow_network else 'false'}",
        "-c", "features.codex_hooks=true",
        "-c", f'hooks.PreToolUse=[{{matcher="*",hooks=[{{type="command",command={hook},timeout=30}}]}}]',
    ]


class CodexAdapter(CliAdapter):
    name = "codex"
    label = "Codex"
    executables = ("codex",)
    status_args = ("login", "status")
    ok_terms = ("logged in", "authenticated", "chatgpt")

    def _auth_env(self) -> dict[str, str]:
        return {"CODEX_HOME": os.path.join(isolation.bots_home(), ".codex")}

    supports_resume = True
    tracks_tools = True

    def _can_resume(self, job) -> bool:
        # `codex exec resume` has no --approve-for-me/--sandbox, so restricted bots stay fresh. A message
        # to a restricted bot still steers it (0.11.7): the interrupted run is started again with the
        # same restricted flags (CliAdapter.steering "restart"), never resumed under other ones.
        return super()._can_resume(job) and self._full_access(job)

    def _tool_signal(self, obj):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        kind, itype, key = obj.get("type"), item.get("type"), str(item.get("id") or "")
        if kind == "item.started" and itype in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
            return "start", key
        if kind == "item.completed" and itype not in ("agent_message", "reasoning"):
            return "end", key
        return None

    def _access_args(self, job) -> list[str]:
        """Sandbox/approval flags: full = the bypass flag (as 0.10.x); strict-profile full =
        gate_overrides; restricted = --approve-for-me exactly as on master (gen 11: restricted-mode
        flags are parked under invariant 1; restricted bots never resume, see _can_resume)."""
        if not self._full_access(job):
            return ["--approve-for-me"]
        if job is not None and job.unattended:
            return gate_overrides(job)
        return ["--dangerously-bypass-approvals-and-sandbox", *(hook_overrides(job) if job is not None else [])]

    def _hooked(self, job) -> bool:
        return self._full_access(job)

    def _exec_args(self, workspace: Path, job=None, session: str | None = None) -> list[str]:
        images = [f"--image={path}" for path in (job.attachment_paths if job else [])]
        compaction = [
            "-c", f"model_auto_compact_token_limit={compact_token_limit()}",
            "-c", f"compact_prompt={json.dumps(COMPACT_POLICY)}",
        ]
        if session:
            # official `codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]`; cwd is the job workspace
            return [
                "exec", "resume",
                "-m", self._job_model(job) or DEFAULT_MODELS["codex"],
                "-c", f"model_reasoning_effort={self._job_effort(job)}",
                *compaction,
                *self._access_args(job),
                "--skip-git-repo-check",
                "--json",
                *images,
                session,
                "-",
            ]
        return [
            "exec",
            "-m",
            self._job_model(job) or DEFAULT_MODELS["codex"],
            "-c",
            f"model_reasoning_effort={self._job_effort(job)}",
            *(compaction if self._full_access(job) else []),
            *self._access_args(job),
            "--skip-git-repo-check",
            "--json",
            "-C",
            str(workspace),
            *images,
            "-",
        ]

    def _failure_error(self, code: int, stderr: bytes, parser=None) -> str | None:
        text = stderr.decode(errors="replace").lower()
        if code == 2 or any(phrase in text for phrase in REJECTED_FLAG_PHRASES):
            return "Codex flags are unsupported by this CLI"
        return super()._failure_error(code, stderr, parser)

    # --- token usage ---
    # What `codex exec --json` reports in `turn.completed.usage` is the thread's running total, not
    # the turn's: a long-lived bot session shows e.g. 15.1M, then 17.1M after the next turn. Stored
    # as is, every job would be charged the whole history again. Production evidence (14 Nov 2026,
    # one resumed thread, consecutive jobs): input_tokens 15,144,947 -> 16,512,928 -> 16,867,165 ->
    # 17,096,748, the last two from ~1-minute jobs (a minute cannot use 350k new tokens at those
    # cached ratios), and a bot whose thread was reset showed its value drop. No raw `codex exec`
    # capture was made, so this is inferred from stored values, not from the CLI's docs. The job's usage is therefore the last
    # total of its thread minus the total before the job (kept next to the session pointer in
    # `codex-usage-baseline.json`). That also covers an interrupted segment that never printed its own
    # `turn.completed` (steer interrupt + resume): the resumed segment's total includes it. A thread
    # that starts inside this job has baseline 0; a resumed thread with no recorded baseline (first run
    # after this change) reports no usage for that job rather than the whole history; a key whose total
    # went down (reset) counts in full; nothing is ever negative. A job that never reaches here
    # (cancelled, launch failure) leaves the baseline behind, so the next job's delta includes its tokens:
    # the total stays right, the per-job attribution shifts.
    def _baseline_file(self, workspace: Path, job) -> Path:
        # controller-only state: the private dir when isolated, never read or written through a link
        return isolation.state_dir(self._bot_dir(workspace, job)) / "codex-usage-baseline.json"

    def _job_usage(self, workspace: Path, job, segments: list[Segment]) -> dict | None:
        totals: dict[str, dict] = {}  # thread -> its last reported running total
        fresh: set[str] = set()  # threads that began inside this job
        seen: set[str] = set()
        for started, ended, usage in segments:
            thread = ended or started
            if not thread:
                continue
            if started is None and thread not in seen:
                fresh.add(thread)
            seen.add(thread)
            if usage:
                totals[thread] = usage
        if not totals:
            return None
        base_thread, base = None, {}
        keep = bool(job.bot_id) and self._can_resume(job)  # no bot / not resumable: no baseline file at all
        with contextlib.suppress(OSError, ValueError):
            if not keep:
                raise ValueError("no baseline")
            saved = json.loads(safefs.read_text(self._baseline_file(workspace, job), limit=64 * 1024))
            if isinstance(saved, dict) and isinstance(saved.get("total"), dict):
                base_thread, base = saved.get("thread"), saved["total"]
        result: dict | None = None
        for thread, total in totals.items():
            if thread in fresh:
                before: dict | None = {}
            elif thread == base_thread:
                before = base
            else:
                before = None  # no baseline for a resumed thread: unknown, not "everything"
            if before is None:
                continue
            result = result if result is not None else {}
            for key, value in total.items():
                previous = before.get(key, 0)
                delta = value - previous if value >= previous else value  # smaller total = reset
                result[key] = result.get(key, 0) + max(0, delta)
        last = list(totals)[-1]
        if keep:
            with contextlib.suppress(OSError):
                # atomic (temp + rename inside the directory fd), 0600, no link followed
                safefs.write_text(self._baseline_file(workspace, job), json.dumps({"thread": last, "total": totals[last]}),
                                  0o600, dir_mode=isolation.dir_mode())
        return result
