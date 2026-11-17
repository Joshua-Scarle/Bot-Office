"""0.11.0 review round: probe helper reachability (gen 2), Claude's strict tool list (gen 9) and
restricted Codex exactly as on master (gen 11)."""
import json
import urllib.request
from datetime import datetime
from uuid import uuid4

import pytest

from botoffice import probe as probe_mod
from botoffice.domain import Job, JobStatus
from botoffice.providers.base import RunResult
from botoffice.providers.claude import UNATTENDED_TOOLS
from botoffice.providers.codex import CodexAdapter
from botoffice.rules import gate_state, helpers_reachable


class ProbeCli:
    """Stands in for a CLI under the probe: optionally calls `bot-task list`, then asks the gate."""

    name = "codex"
    supports_resume = True
    executable = None

    def __init__(self, reach_helpers: bool):
        self.reach_helpers = reach_helpers

    async def run(self, job, workspace, log_path, timeout):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if self.reach_helpers:
            with opener.open(job.api_url + probe_mod.HELPER_PATH, timeout=5) as response:
                assert json.loads(response.read()) == []
        event = {"tool_name": "Bash", "tool_input": {"command": f"touch x && echo {probe_mod.CANARY}"}}
        request = urllib.request.Request(job.api_url + "/v1/agent/gate", data=json.dumps({"event": event}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=5) as response:
            assert json.loads(response.read())["decision"] == "deny"
        return RunResult("succeeded", exit_code=0)


@pytest.mark.parametrize("reach", [True, False])
def test_probe_records_whether_the_helpers_reach_the_api(tmp_path, reach):
    state, detail = probe_mod.probe(ProbeCli(reach), tmp_path, timeout=30)
    assert state == "enforced" and gate_state(tmp_path, "codex") == "enforced"
    assert helpers_reachable(tmp_path, "codex") is reach and f"helpers={reach}" in detail
    assert "bot-task list" in probe_mod.PROMPT


def test_claude_strict_tools_include_background_shell_and_skills():
    assert {"Skill", "BashOutput", "KillShell"} <= set(UNATTENDED_TOOLS)
    assert not any(name.lower().startswith("mcp") for name in UNATTENDED_TOOLS)


def codex_job(**extra):
    fields = {"id": uuid4(), "prompt": "go", "status": JobStatus.RUNNING, "created_at": datetime.now(),
              "bot_id": uuid4(), "provider": "codex", "source": "routine", "cli_access": "restricted"}
    return Job(**{**fields, **extra})


@pytest.mark.parametrize("unattended", [False, True])
def test_restricted_codex_matches_master(tmp_path, unattended):
    """gen 11: restricted bots never resume and pass exactly master's flags (no sandbox override),
    whatever the profile."""
    adapter = CodexAdapter(executable="codex")
    job = codex_job(unattended=unattended)
    assert adapter._can_resume(job) is False
    args = adapter._exec_args(tmp_path, job)
    assert "--approve-for-me" in args and "sandbox_mode" not in " ".join(args)
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_full_access_codex_non_strict_args_match_master(tmp_path):
    """gen 11 / invariant: attended and default scheduled full-access runs keep 0.10.x's exact flags."""
    adapter = CodexAdapter(executable="codex")
    for source in ("chat", "routine"):
        job = codex_job(cli_access="full", source=source, unattended=False)
        fresh = adapter._exec_args(tmp_path, job)
        assert "--dangerously-bypass-approvals-and-sandbox" in fresh and "sandbox_mode" not in " ".join(fresh)
        resumed = adapter._exec_args(tmp_path, job, session="0199a213-81c0-7800-8aa1-bbab2a035a53")
        assert resumed[:2] == ["exec", "resume"] and "--dangerously-bypass-approvals-and-sandbox" in resumed
        assert "sandbox_mode" not in " ".join(resumed)


def test_readme_states_the_profile_scope_honestly():
    """gen 2/3/4/5/9: the README says what the strict profile changes and what it does not."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "bypass flags are unchanged from 0.10.x" in text and "Only strict-profile runs" in text
    assert "refuses strict-profile runs (fail closed); other runs fail open except" in text
    assert "MCP tools are refused" in text and "Skill, BashOutput, KillShell" in text
    assert "helpers reach Bot-Office" in text
    assert "this is not implemented" not in text and "implemented by `deploy/install-isolation.sh`" in text
    # review C2 #8/#9/#10
    assert "enforced against honest runs" in text and "restricted Codex bot" in text
    assert "**Smoke check after install.**" in text and "desk screenshot" in text


def test_strict_profile_refuses_restricted_codex_which_has_no_gate_hook(settings):
    """Review C2 #9: an 'enforced' probe (run in full mode) does not cover restricted Codex,
    whose --approve-for-me run wires no hook; attended and default scheduled runs are unaffected."""
    from botoffice.rules import record_capability
    from botoffice.scheduler import Scheduler
    from botoffice.store import JobStore

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "db.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": CodexAdapter(executable="codex")})
    record_capability(settings.control_dir, "codex", "enforced", helpers=True)
    bot = store.create_bot("Ada", "", "codex", None, cli_access="restricted")
    trigger = store.create("event", "codex", bot_id=bot.id, source="trigger")
    assert "full CLI access" in scheduler.gate_refusal(trigger, bot)
    for source in ("chat", "routine"):  # not strict (scheduled_runs: full): runs as before
        assert scheduler.gate_refusal(store.create("x", "codex", bot_id=bot.id, source=source), bot) is None
    full = store.update_bot(bot.id, cli_access="full") or store.get_bot(bot.id)
    assert scheduler.gate_refusal(trigger, full) is None
