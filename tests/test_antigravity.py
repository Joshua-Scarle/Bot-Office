"""0.10.5: Antigravity runs in a stable per-bot directory and its reply comes from the stream."""
import asyncio
import json
import stat
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from botoffice import outgoing_images
from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.streams import AntigravityParser, parser_for, session_id_from
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

CONV = "9f1c2a3e-1111-4222-8333-444455556666"
INIT = {"event": "init", "conversation_id": CONV, "init": {"model": "gemini-3-pro", "cwd": "/somewhere"}}


def delta(text, state="ACTIVE"):
    return {"event": "step_update", "step_update": {"conversation_id": CONV, "step_type": "agent_response",
                                                    "state": state, "text_delta": text}}


def tool(state, name="run_command", info=None):
    return {"event": "step_update", "step_update": {"step_type": "tool", "state": state, "tool_name": name,
                                                    "tool_info": info or {"command": "ls -la"}}}


def result(response="sent\n", **extra):
    return {"event": "result", "result": {"conversation_id": CONV, "status": "SUCCESS", "response": response,
                                          "usage": {"input_tokens": 12, "output_tokens": 3}, **extra}}


def run_parser(*lines):
    parser, events = AntigravityParser(), []
    for line in lines:
        if parser.session_id is None:
            parser.session_id = session_id_from(line)
        events += parser.feed(line)
    return parser, events


def test_parser_streams_text_tools_and_final_reply():
    assert isinstance(parser_for("antigravity"), AntigravityParser)
    done = {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE",
                                                      "usage": {"input_tokens": 5, "output_tokens": 1}}}
    parser, events = run_parser(INIT, delta("phase-"), delta("one"), done, tool("ACTIVE"), tool("ACTIVE"),
                                tool("DONE"), delta("sent"), done, result("phase-one\n\nsent\n"))
    assert parser.session_id == CONV
    assert [e for e in events if e[0] == "text"] == [("text", {"text": "phase-"}), ("text", {"text": "one"}),
                                                     ("text", {"text": "\n\nsent"})]
    assert [e for e in events if e[0] == "tool"] == [("tool", {"name": "run_command", "summary": "ls -la"})]
    assert parser.final_text == "phase-one\n\nsent\n" and events[-1] == ("final", {"text": "phase-one\n\nsent\n"})
    assert parser.usage == {"input_tokens": 12, "output_tokens": 3} and parser.error_text is None


def test_parser_errors_and_interrupt():
    parser, events = run_parser(INIT, {"event": "result", "result": {"status": "ERROR", "response": "",
                                                                       "error": "quota exceeded"}})
    assert parser.error_text == "quota exceeded" and events == [("error", {"message": "quota exceeded"})]
    assert parser.final_text is None
    # a steering interrupt is not shown as an error event; the CLI adapter handles it
    parser, events = run_parser(INIT, {"event": "result", "result": {"status": "ERROR", "response": "",
                                                                       "error": "interrupted",
                                                                       "finished_reason": "interrupted"}})
    assert events == [] and parser.error_text == "interrupted" and parser.final_text is None
    assert run_parser("junk", {"event": "result", "result": "x"})[1] == []


def test_tool_separates_text_when_response_has_no_done_event():
    _, events = run_parser(delta("Before."), tool("ACTIVE"), tool("DONE"), delta("After."))
    assert [data["text"] for kind, data in events if kind == "text"] == ["Before.", "\n\nAfter."]


def make_job(workspace: Path, bot_id=None, **extra) -> Job:
    base = dict(id=uuid4(), prompt="FULL PROMPT", resume_prompt="SHORT PROMPT", status=JobStatus.RUNNING,
                created_at=datetime.now(), slot=Slot.DESKTOP_1, bot_id=bot_id or uuid4(), provider="antigravity")
    return Job(**(base | extra))


def workspace_of(tmp_path) -> Path:
    ws = tmp_path / "data" / "workspaces" / "job"
    ws.mkdir(parents=True)
    return ws


def test_full_access_bot_runs_in_a_stable_directory_with_absolute_paths(tmp_path):
    ws = workspace_of(tmp_path)
    adapter = AntigravityAdapter(executable="agy")
    job = make_job(ws)
    stable = tmp_path / "data" / "bots" / str(job.bot_id) / "antigravity-workspace"
    assert adapter._cwd(ws, job) == stable
    assert stat.S_IMODE(stable.stat().st_mode) == 0o700 and (stable / ".stable").is_file()
    fresh = adapter._exec_args(ws, job, None)
    assert fresh[0] == "-p" and f"job directory is {ws}." in fresh[1] and str(ws / "prompt.txt") in fresh[1]
    assert "--dangerously-skip-permissions" in fresh and "--conversation" not in fresh
    resumed = adapter._exec_args(ws, job, CONV)
    assert str(ws / "prompt-resume.txt") in resumed[1] and resumed[resumed.index("--conversation") + 1] == CONV
    # a steering resume sends the steering text itself
    assert adapter._steer_exec_args(ws, job.model_copy(update={"prompt": "STEER"}), CONV)[1] == "STEER"
    env = adapter._env(ws, job)
    assert env["BOT_OFFICE_WORKSPACE"] == str(ws) and env["BOT_OFFICE_JOB_ID"] == str(job.id)


def test_restricted_and_botless_jobs_keep_the_job_workspace_and_never_resume(tmp_path):
    ws = workspace_of(tmp_path)
    adapter = AntigravityAdapter(executable="agy")
    restricted = make_job(ws, cli_access="restricted")
    assert adapter._cwd(ws, restricted) == ws and "BOT_OFFICE_WORKSPACE" not in adapter._env(ws, restricted)
    assert adapter._exec_args(ws, restricted, None)[1] == "FULL PROMPT"
    adapter._save_session(ws, restricted, CONV)
    assert adapter._saved_session(ws, restricted) is None
    assert adapter._cwd(ws, make_job(ws).model_copy(update={"bot_id": None})) == ws  # not a bot's job
    assert not list((tmp_path / "data").glob("bots/*/antigravity-workspace"))


def test_a_session_from_a_job_directory_is_archived_once_then_the_stable_one_is_resumed(tmp_path):
    ws = workspace_of(tmp_path)
    adapter = AntigravityAdapter(executable="agy")
    job = make_job(ws)
    bot_dir = tmp_path / "data" / "bots" / str(job.bot_id)
    bot_dir.mkdir(parents=True)
    (bot_dir / "antigravity-session").write_text(CONV)  # created in an old job folder
    assert adapter._saved_session(ws, job) is None
    assert not (bot_dir / "antigravity-session").exists()
    assert (bot_dir / "antigravity-session.cwd-migrate").read_text() == CONV
    assert (bot_dir / "antigravity-workspace" / ".stable").is_file()
    adapter._save_session(ws, job, CONV)  # a conversation started in the stable directory
    assert adapter._saved_session(ws, job) == CONV
    assert adapter._saved_session(ws, job) == CONV and not list(bot_dir.glob("*.cwd-migrate-*"))


def test_desk_attach_and_screenshot_resolve_against_the_job_directory(tmp_path, monkeypatch):
    job_dir, stable = tmp_path / "job", tmp_path / "bot" / "antigravity-workspace"
    job_dir.mkdir()
    stable.mkdir(parents=True)
    (job_dir / "prompt.txt").write_text("p")
    (job_dir / "chart.png").write_bytes(b"png")
    monkeypatch.chdir(stable)
    monkeypatch.setenv("BOT_OFFICE_WORKSPACE", str(job_dir))
    assert outgoing_images.job_cwd() == job_dir
    assert outgoing_images.attach_file("chart.png") == (job_dir / "chart.png").resolve()
    assert (job_dir / outgoing_images.SIDECAR).read_text().strip() == str((job_dir / "chart.png").resolve())
    # inside the job directory (any provider) nothing changes
    monkeypatch.chdir(job_dir)
    assert outgoing_images.job_cwd() == Path.cwd()
    monkeypatch.delenv("BOT_OFFICE_WORKSPACE")
    assert outgoing_images.job_cwd() == Path.cwd()


FAKE_AGY = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if sys.argv[1:2] == ["models"]:
    print("gemini-3-pro"); sys.exit(0)
conv = "9f1c2a3e-1111-4222-8333-444455556666"
log = Path(os.environ["BOT_OFFICE_WORKSPACE"]) / "calls.jsonl"
with log.open("a") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[1:]}) + "\\n")
reply = os.environ.get("FAKE_REPLY", "sent")
def emit(o): print(json.dumps(o), flush=True)
emit({"event": "init", "conversation_id": conv, "init": {"model": "m", "cwd": os.getcwd()}})
emit({"event": "step_update", "step_update": {"conversation_id": conv, "step_type": "agent_response", "state": "ACTIVE", "text_delta": reply}})
emit({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": reply + "\\n",
                                    "usage": {"input_tokens": 4, "output_tokens": 2}}})
'''


def run_turns(settings, tmp_path, monkeypatch, *replies):
    script = tmp_path / "agy"
    script.write_text(FAKE_AGY)
    script.chmod(0o755)
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = AntigravityAdapter(executable=str(script))
    scheduler = Scheduler(store, settings, {"antigravity": adapter})
    bot = store.create_bot("Ada", "", "antigravity", None)
    jobs = []
    for reply in replies:
        monkeypatch.setenv("FAKE_REPLY", reply)
        scheduler.turns.post_message(bot, "do the thing")
        claimed = store.claim(Slot.DESKTOP_1, {"antigravity"})
        asyncio.run(scheduler._execute(Slot.DESKTOP_1, claimed))
        jobs.append(store.get(claimed.id))
    chat = [(m.role, m.text) for m in store.list_messages(bot.id) if m.role != "user"]
    return store, bot, jobs, chat


def test_end_to_end_reply_comes_from_the_stream_in_the_stable_directory(settings, tmp_path, monkeypatch):
    store, bot, jobs, chat = run_turns(settings, tmp_path, monkeypatch,
                                       "First answer.\nBOT-OFFICE-STATUS: done", "Second answer.")
    bot_dir = settings.data_dir / "bots" / str(bot.id)
    stable = bot_dir / "antigravity-workspace"
    assert chat == [("assistant", "First answer."), ("assistant", "Second answer.")]
    assert [j.status for j in jobs] == [JobStatus.SUCCEEDED] * 2 and jobs[0].outcome == "done"
    assert jobs[0].reply_text == "First answer."
    with closing(store._connect()) as db:
        usage = db.execute("SELECT usage_json FROM jobs WHERE id=?", (str(jobs[0].id),)).fetchone()[0]
    assert json.loads(usage) == {"input_tokens": 4, "output_tokens": 2}
    assert (bot_dir / "antigravity-session").read_text() == "9f1c2a3e-1111-4222-8333-444455556666"
    # the process ran in the stable directory both times; each turn was pointed at its own job directory
    for job in jobs:
        calls = [json.loads(line) for line in (settings.data_dir / "workspaces" / str(job.id) / "calls.jsonl")
                 .read_text().splitlines()]
        assert [c["cwd"] for c in calls] == [str(stable.resolve())]
        assert str(settings.data_dir / "workspaces" / str(job.id)) in calls[0]["argv"][1]
    second = json.loads((settings.data_dir / "workspaces" / str(jobs[1].id) / "calls.jsonl").read_text())
    assert "--conversation" in second["argv"] and "prompt-resume.txt" in second["argv"][1]
    first = json.loads((settings.data_dir / "workspaces" / str(jobs[0].id) / "calls.jsonl").read_text())
    assert "--conversation" not in first["argv"] and "prompt.txt" in first["argv"][1]


def test_end_to_end_status_only_reply_shows_a_short_line_not_the_stream(settings, tmp_path, monkeypatch):
    _, _, jobs, chat = run_turns(settings, tmp_path, monkeypatch, "BOT-OFFICE-STATUS: done — step2 written")
    assert jobs[0].status == JobStatus.SUCCEEDED and chat == [("assistant", "Done — step2 written")]
    assert not any('"event"' in text for _, text in chat)


def test_a_model_that_names_its_effort_gets_no_effort_flag(tmp_path):
    """0.11.15: `agy --model gemini-3.8-flash-high --effort medium` fails ("model conflicts with --effort")."""
    from botoffice.catalog import default_model, effective_effort, model_carries_effort
    ws = workspace_of(tmp_path)
    adapter = AntigravityAdapter(executable="agy")
    default = default_model("antigravity")
    assert model_carries_effort("antigravity", default) and default.endswith("-high")
    for model, effort in ((default, "medium"), ("gemini-3.8-flash-low", "max"), (default, None)):
        args = adapter._exec_args(ws, make_job(ws).model_copy(update={"model": model, "effort": effort}), None)
        assert "--effort" not in args and args[args.index("--model") + 1] == model
    # a model without an effort suffix still takes the bot's effort (or the engine default)
    args = adapter._exec_args(ws, make_job(ws).model_copy(update={"model": "gemini-3.8-pro", "effort": "high"}), None)
    assert args[args.index("--effort") + 1] == "high"
    assert effective_effort("antigravity", "gemini-3.8-pro", "high") == "high"
    assert effective_effort("antigravity", default, "high") is None
    assert not model_carries_effort("claude", "claude-opus-5-high")  # only Antigravity ids name an effort



def test_a_new_antigravity_bot_and_its_jobs_carry_no_conflicting_effort(settings):
    """The default pair of a fresh bot queues a job without an effort; a plain model keeps the default."""
    from fastapi.testclient import TestClient

    from botoffice.api import create_app
    from botoffice.catalog import default_model
    from botoffice.providers.fake import FakeAdapter
    H = {"Authorization": "Bearer test-token"}
    adapter = FakeAdapter()
    adapter.name = "antigravity"
    app = create_app(settings, {"codex": FakeAdapter(), "antigravity": adapter})
    with TestClient(app) as client:
        bot = client.post("/v1/bots", json={"name": "ag", "instructions": "x", "default_provider": "antigravity"}, headers=H).json()
        assert bot["model"] == default_model("antigravity")
        job = client.post("/v1/jobs", json={"prompt": "hi", "provider": "antigravity", "bot_id": bot["id"]}, headers=H).json()
        assert job["model"] == bot["model"] and job["effort"] is None
        plain = client.post("/v1/jobs", json={"prompt": "hi", "provider": "antigravity", "model": "gemini-3.8-pro"}, headers=H).json()
        assert plain["effort"] == "medium"


def test_the_page_hides_effort_for_the_same_models_the_server_does():
    """bot-office.html mirrors catalog.model_carries_effort: same answers, and the settings dialog uses it."""
    import shutil
    import subprocess

    import pytest

    from botoffice.api import CHAT_PAGE
    from botoffice.catalog import model_carries_effort
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    page = CHAT_PAGE.read_text()
    start = page.index("function modelCarriesEffort")
    func = page[start:page.index("\n", start)]
    cases = [("antigravity", m) for m in ("gemini-3.8-flash-high", "gemini-3.8-flash-LOW", "x-xhigh", "gemini-3.8-pro", "gemini-high-x", "")]
    cases += [("claude", "claude-opus-5-5"), ("codex", "gpt-6.1-high"), ("opencode", "a/b-max")]
    script = func + f"\nconst out = {json.dumps(cases)}.map(([e, m]) => modelCarriesEffort(e, m));\nconsole.log(JSON.stringify(out));"
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=30).stdout)
    assert got == [model_carries_effort(e, m) for e, m in cases]
    assert "const levels = fixed ? [] : effortsFor(engine);" in page and "modelCarriesEffort(bot.engine, bot.model)" in page
