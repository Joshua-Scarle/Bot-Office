"""0.9.0 honest errors: provider failure text, classification and reset times (A1)."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from botoffice import failures
from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

from fakes import StreamProc, jsonl

CLAUDE_429 = "You've hit your 5-hour limit · resets 6:10pm (Europe/Berlin)"
# 16:00 in Berlin, 10:00 in New York
NOW = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


def test_claude_limit_text_is_a_usage_limit_with_berlin_reset_shown_in_et():
    assert failures.classify(CLAUDE_429, 1, None) == failures.USAGE_LIMIT
    reset = failures.parse_reset(CLAUDE_429, NOW)
    assert reset == datetime(2026, 11, 14, 17, 10, tzinfo=UTC)
    assert failures.show_time(reset, NOW) == "12:10 PM ET"
    line = failures.failure_line("Claude", failures.USAGE_LIMIT, CLAUDE_429, reset, reset, NOW)
    assert line == f"Claude usage limit reached; resets 12:10 PM ET. Will retry then. Claude said: \u201c{CLAUDE_429}\u201d"


def test_reset_time_forms():
    assert failures.parse_reset("try again in 3 hours", NOW) == NOW + timedelta(hours=3)
    assert failures.parse_reset("Retry-After: 90", NOW) == NOW + timedelta(seconds=90)
    # a time of day already past rolls to the next day (default zone America/New_York)
    assert failures.parse_reset("limit reached, try again at 9:00 AM", NOW) == datetime(2026, 11, 15, 14, 0, tzinfo=UTC)
    assert failures.parse_reset("usage limit; resets Nov 20 at 3pm", NOW) == datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert failures.parse_reset("rate limited", NOW) is None
    assert failures.parse_reset("error 500 at 12", NOW) is None


def test_codex_date_with_at_before_it_and_ordinal_day():
    now = datetime(2026, 11, 14, 22, 40, tzinfo=UTC)
    msg = "You've hit your usage limit. Please try again at Nov 15th, 2026 2:23 AM."
    want = datetime(2026, 11, 15, 2, 23, tzinfo=ZoneInfo("America/New_York"))
    assert failures.parse_reset(msg, now) == want
    for text in ("try again at Nov 15, 2026 2:23 AM", "try again at November 15th 2026 at 2:23 AM",
                 "try again on Nov 15th, 2026 2:23 AM", "try again at Nov 1st, 2026 2:23 AM"):
        assert failures.parse_reset(text, now) is not None
    assert failures.parse_reset("try again at Nov 1st, 2026 2:23 AM", now).day == 1
    # a time only is still a time of day (next 2:23 AM ET)
    assert failures.parse_reset("try again at 2:23 AM", now) == datetime(2026, 11, 15, 7, 23, tzinfo=UTC)


def test_classification():
    assert failures.classify("Not logged in · Please run /login", 1) == failures.AUTH
    assert failures.classify("anything", 1, 401) == failures.AUTH
    assert failures.classify("overloaded_error", 1, 529) == failures.PROVIDER_ERROR
    assert failures.classify("job timed out", 124) == failures.TIMEOUT
    assert failures.classify("Codex launch failed: no such file", None) == failures.LAUNCH
    assert failures.classify("429 Too Many Requests", 1) == failures.USAGE_LIMIT


def _job(**extra):
    base = dict(id=uuid4(), prompt="p", resume_prompt="p", status=JobStatus.RUNNING,
                created_at=datetime.now(UTC), bot_id=uuid4())
    base.update(extra)
    return Job(**base)


def _fake_cli(monkeypatch, stdout: bytes, code: int, status_out: bytes):
    async def fake_exec(*args, **kwargs):
        if args[1:3] in (("auth", "status"), ("login", "status")):
            return StreamProc(status_out, b"", 0)
        return StreamProc(stdout, b"some stderr\n", code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_claude_stream_429_keeps_provider_text_kind_and_reset(monkeypatch, tmp_path):
    ws = tmp_path / "data" / "workspaces" / "j"
    ws.mkdir(parents=True)
    stream = jsonl(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Partial work so far"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
         "result": CLAUDE_429, "usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": 0.01},
    )
    _fake_cli(monkeypatch, stream, 1, b'{"loggedIn": true}')
    result = asyncio.run(ClaudeAdapter(executable="claude").run(_job(), ws, tmp_path / "j.log", 5))
    assert result.status == "failed" and result.failure_kind == failures.USAGE_LIMIT
    assert result.error == CLAUDE_429 and result.retry_at is not None
    assert result.usage == {"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.01}


def test_codex_turn_failed_usage_limit(monkeypatch, tmp_path):
    ws = tmp_path / "data" / "workspaces" / "j"
    ws.mkdir(parents=True)
    stream = jsonl({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again in 2 hours."}})
    _fake_cli(monkeypatch, stream, 1, b"Logged in using ChatGPT")
    result = asyncio.run(CodexAdapter(executable="codex").run(_job(), ws, tmp_path / "j.log", 5))
    assert result.failure_kind == failures.USAGE_LIMIT
    assert "usage limit" in result.error
    assert timedelta(hours=1, minutes=59) < result.retry_at - datetime.now(UTC) <= timedelta(hours=2)


def test_failure_without_stream_error_uses_stderr_tail(monkeypatch, tmp_path):
    ws = tmp_path / "data" / "workspaces" / "j"
    ws.mkdir(parents=True)
    _fake_cli(monkeypatch, b"", 3, b'{"loggedIn": true}')
    result = asyncio.run(ClaudeAdapter(executable="claude").run(_job(), ws, tmp_path / "j.log", 5))
    assert result.error == "some stderr" and result.failure_kind == failures.PROVIDER_ERROR


class ScriptedAdapter:
    name = "claude"
    enabled = True

    def __init__(self, *results):
        self.results = list(results)
        self.runs = 0

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        self.runs += 1
        log_path.write_text("log")
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return result


def run_one(settings, adapter, text="hello", clock=None):
    """Post a chat message and run its job once (no scheduler loop)."""
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"claude": adapter}, **({"clock": clock} if clock else {}))
    settings.ensure_dirs()
    bot = store.create_bot("Ada", "", "claude", "testing")
    _, job = scheduler.turns.post_message(bot, text)

    async def go():
        claimed = store.claim(Slot.DESKTOP_1, {"claude"})
        await scheduler._execute(Slot.DESKTOP_1, claimed)

    asyncio.run(go())
    return store, scheduler, bot, job


def test_failed_turn_posts_engine_line_and_partial_reply(settings):
    reset = failures.parse_reset(CLAUDE_429, NOW)
    adapter = ScriptedAdapter(RunResult("failed", exit_code=1, error=CLAUDE_429, final_text="Half done: step 1 ok",
                                        failure_kind=failures.USAGE_LIMIT, retry_at=reset))
    store, _, bot, job = run_one(settings, adapter, clock=lambda: NOW)
    system = [m for m in store.list_messages(bot.id) if m.role == "system"]
    assert len(system) == 1
    assert system[0].text.startswith("Claude usage limit reached; resets 12:10 PM ET.")
    assert "---\nHalf done: step 1 ok" in system[0].text
    saved = store.get(job.id)
    assert saved.failure_kind == failures.USAGE_LIMIT and saved.error == CLAUDE_429
    assert json.loads(saved.model_dump_json())["failure_kind"] == "usage_limit"


# --- review round 1 ---

def test_redact_hides_obvious_secrets_but_keeps_paths_and_words():
    text = ("Authorization: Bearer abcDEF123456789 api_key=supersecret99 key sk-proj-ABCDEF1234567890 "
            "hex 0123456789abcdef0123456789abcdef tok QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTA= "
            "at /home/box/printpilot-agentdesk/data/workspaces/run-0123/logs ok")
    out = failures.redact(text)
    for secret in ("abcDEF123456789", "supersecret99", "sk-proj-ABCDEF1234567890",
                   "0123456789abcdef0123456789abcdef", "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTA="):
        assert secret not in out
    assert "/home/box/printpilot-agentdesk/data/workspaces/run-0123/logs" in out
    assert failures.redact("You've hit your usage limit. Try again in 2 hours.") == \
        "You've hit your usage limit. Try again in 2 hours."
    assert "sk-live" not in failures.clip("boom: sk-live_0123456789abcdef")


def test_usage_limit_line_keeps_the_provider_text():
    line = failures.failure_line("Codex", failures.USAGE_LIMIT, "Quota exceeded for org", None, None, NOW)
    assert line == "Codex usage limit reached. Codex said: “Quota exceeded for org”"


def test_codex_turn_failed_after_a_message_is_a_failure_that_keeps_the_text(monkeypatch, tmp_path):
    ws = tmp_path / "data" / "workspaces" / "j"
    ws.mkdir(parents=True)
    stream = jsonl(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Half the work"}},
        {"type": "turn.failed", "error": {"message": "stream error: upstream 500"}},
    )
    _fake_cli(monkeypatch, stream, 0, b"Logged in using ChatGPT")
    result = asyncio.run(CodexAdapter(executable="codex").run(_job(), ws, tmp_path / "j.log", 5))
    assert result.status == "failed" and result.failure_kind == failures.PROVIDER_ERROR
    assert result.final_text == "Half the work" and "upstream 500" in result.error


def test_codex_recovered_stream_error_still_succeeds(monkeypatch, tmp_path):
    ws = tmp_path / "data" / "workspaces" / "j"
    ws.mkdir(parents=True)
    stream = jsonl(
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )
    _fake_cli(monkeypatch, stream, 0, b"Logged in using ChatGPT")
    result = asyncio.run(CodexAdapter(executable="codex").run(_job(), ws, tmp_path / "j.log", 5))
    assert result.status == "succeeded" and result.final_text == "Done"


def test_kill_process_group_only_signals_an_unreaped_leader(monkeypatch):
    from botoffice.providers.cli import CliAdapter
    sent = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: sent.append((pid, sig)))

    class Proc:
        pid = 4242
        returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

    leader = Proc()
    asyncio.run(CliAdapter._kill_process_group(leader))
    assert len(sent) == 1  # SIGTERM while unreaped; nothing once wait() reaped it
    sent.clear()
    asyncio.run(CliAdapter._kill_process_group(leader))
    assert sent == []
