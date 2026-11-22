"""Live streaming, event replay, effort, tiers, New York cron and the job audit trail."""

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fakes import StreamProc, jsonl
from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.catalog import capability_tier, effort_levels
from botoffice.cron import cron_matches
from botoffice.domain import Job, JobStatus
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import CursorAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.streams import ClaudeParser, CodexParser, CursorParser
from botoffice.store import JobStore
from botoffice.streaming import EventSink

H = {"Authorization": "Bearer test-token"}
SSE = {**H, "Accept": "text/event-stream"}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def make_bot(client, **overrides):
    response = client.post("/v1/bots", json={"name": "helper", "instructions": "x"} | overrides, headers=H)
    assert response.status_code == 201, response.text
    return response.json()


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        events.append({"id": fields.get("id"), "event": fields["event"], "data": json.loads(fields["data"])})
    return events


class StreamingFake(FakeAdapter):
    """Emits stream events like a real adapter, optionally held open until released."""

    def __init__(self, hold: asyncio.Event | None = None, reply_file: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.hold = hold
        self.reply_file = reply_file
        self.jobs = []

    async def run(self, job, workspace, log_path, timeout):
        from botoffice.streaming import emit_event

        self.jobs.append(job)
        emit_event("thinking", text="considering")
        emit_event("text", text="Hel")
        emit_event("tool", name="Bash", summary="ls")
        emit_event("text", text="lo")
        if self.hold is not None:
            await self.hold.wait()
        if self.reply_file:
            (workspace / "reply.txt").write_text("Hello there (reply.txt)", encoding="utf-8")
        return await super().run(job, workspace, log_path, timeout)


# ---------- parsers ----------


def test_claude_parser_partials_tools_and_final_without_duplicates():
    parser = ClaudeParser()
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hm"},
            {"type": "text", "text": "Hi"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ]}},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done"}}},
        {"type": "assistant", "parent_tool_use_id": "sub", "message": {"content": [{"type": "text", "text": "ignored"}]}},
        {"type": "mystery"},
        {"type": "result", "subtype": "success", "result": "Done", "is_error": False},
    ]
    events = [e for line in lines for e in parser.feed(line)]
    assert events == [
        ("thinking", {"text": "hm"}),
        ("text", {"text": "Hi"}),
        ("tool", {"name": "Bash", "summary": "ls -la"}),
        ("text", {"text": "\n\nDone"}),
        ("final", {"text": "Done"}),
    ]
    assert parser.final_text == "Done"


def test_claude_parser_without_partials_uses_assistant_blocks():
    parser = ClaudeParser()
    events = parser.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "whole"}]}})
    assert events == [("text", {"text": "whole"})]
    assert parser.feed({"type": "result", "is_error": True, "result": "boom"})[0][0] == "error"


def test_codex_parser_items():
    parser = CodexParser()
    lines = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "plan"}},
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed"},
        {"type": "future.event"},
    ]
    events = [e for line in lines for e in parser.feed(line)]
    assert [k for k, _ in events] == ["thinking", "tool", "text", "text"]
    assert events[2][1] == {"text": "first", "replace": True}
    assert parser.finish() == [("final", {"text": "answer"})]


def test_cursor_parser_drops_consolidated_duplicate_and_uses_result():
    parser = CursorParser()
    chunk = lambda t: {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}
    events = [e for line in (chunk("Hel"), chunk("lo"), chunk("Hello"), {"type": "tool_call", "subtype": "started",
              "tool_call": {"readToolCall": {"args": {"path": "a.md"}}}},
              {"type": "result", "result": "Hello"}) for e in parser.feed(line)]
    assert events == [
        ("text", {"text": "Hel"}),
        ("text", {"text": "lo"}),
        ("tool", {"name": "read", "summary": "a.md"}),
        ("final", {"text": "Hello"}),
    ]


# ---------- adapters stream live ----------


def test_adapter_streams_events_to_sink_and_reports_final(monkeypatch, tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    job = store.create("p", "codex")
    stdout = jsonl(
        {"type": "item.completed", "item": {"type": "reasoning", "text": "think"}},
        {"type": "item.started", "item": {"type": "command_execution", "command": "echo hi"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "all done"}},
        {"type": "turn.completed"},
    )

    async def fake_exec(*args, **kwargs):
        if args[1:3] == ("login", "status"):
            return StreamProc(b"Logged in with ChatGPT", b"", 0)
        return StreamProc(stdout, b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        sink = EventSink(store, job.id, tmp_path / "events.jsonl")
        token = sink.install()
        try:
            run_job = Job(id=job.id, prompt="p", status=JobStatus.RUNNING, created_at=datetime.now(), effort="xhigh")
            result = await CodexAdapter(executable="codex").run(run_job, tmp_path, tmp_path / "job.log", 5)
        finally:
            sink.close()
            sink.uninstall(token)
        return result, sink

    result, sink = asyncio.run(scenario())
    assert result.status == "succeeded" and result.final_text == "all done"
    kinds = [e["kind"] for e in store.list_events(job.id)]
    assert kinds == ["thinking", "tool", "text", "final"]
    assert sink.assembled == "all done"
    assert store.get(job.id).draft_text == "all done"
    assert oct((tmp_path / "events.jsonl").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "job.log").stat().st_mode & 0o777) == "0o600"


def test_adapter_args_use_job_effort_and_stream_flags(tmp_path):
    def job(effort):
        return Job(id=uuid4(), prompt="p", status=JobStatus.RUNNING, created_at=datetime.now(), effort=effort)

    assert "model_reasoning_effort=xhigh" in CodexAdapter(executable="c")._exec_args(tmp_path, job("xhigh"))
    assert "--json" in CodexAdapter(executable="c")._exec_args(tmp_path, job("low"))
    claude = ClaudeAdapter(executable="c")._exec_args(tmp_path, job("max"))
    assert claude[claude.index("--effort") + 1] == "max" and "stream-json" in claude
    cursor = CursorAdapter(executable="c")._exec_args(tmp_path, job("high"))
    assert "--effort" not in cursor and "--stream-partial-output" in cursor
    with pytest.raises(ValueError):
        ClaudeAdapter(executable="c")._exec_args(tmp_path, job("--evil"))
    with pytest.raises(ValueError):
        CodexAdapter(executable="c")._exec_args(tmp_path, job("max"))  # codex has no "max"


# ---------- events API: store, replay, resume ----------


def test_events_stored_replayed_after_reload_and_final_recorded(settings):
    fake = StreamingFake()
    app = create_app(settings, {"codex": fake})
    with TestClient(app) as client:
        bot = make_bot(client)
        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).json()
        job_id = posted["job"]["id"]
        wait_for(lambda: client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"]["status"] == "succeeded")

        assert client.get(f"/v1/jobs/{job_id}/events").status_code == 401
        snap = client.get(f"/v1/jobs/{job_id}/events", headers=H).json()
        assert snap["done"] and snap["status"] == "succeeded"
        kinds = [e["kind"] for e in snap["events"]]
        assert kinds[0] == "start" and kinds[-1] == "done" and "final" in kinds
        assert {"thinking", "tool", "text"} <= set(kinds)
        assert [e["seq"] for e in snap["events"]] == list(range(1, len(kinds) + 1))
        text = "".join(e["text"] for e in snap["events"] if e["kind"] == "text")
        assert text == "Hello"  # coalesced deltas still add up

        # "reload": resume from the middle with ?after= and with Last-Event-ID
        middle = snap["events"][2]["seq"]
        resumed = client.get(f"/v1/jobs/{job_id}/events?after={middle}", headers=H).json()
        assert [e["seq"] for e in resumed["events"]] == [e["seq"] for e in snap["events"] if e["seq"] > middle]
        sse = client.get(f"/v1/jobs/{job_id}/events", headers={**SSE, "Last-Event-ID": str(middle)})
        assert sse.headers["content-type"].startswith("text/event-stream")
        frames = parse_sse(sse.text)
        assert [f["data"]["seq"] for f in frames] == [e["seq"] for e in resumed["events"]]
        assert frames[0]["id"] == str(middle + 1) and frames[-1]["event"] == "done"
        full = parse_sse(client.get(f"/v1/jobs/{job_id}/events", headers=SSE).text)
        assert [f["event"] for f in full] == kinds
        assert "test-token" not in sse.text

        # final message recorded on the job, in chat, and in the audit copy
        job = client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"]
        assert job["reply_text"] == "Hello there (reply.txt)"
        history = client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json()
        assert history[-1]["text"] == "Hello there (reply.txt)" and not history[-1]["pending"]
        assert oct((settings.data_dir / "logs" / f"{job_id}.events.jsonl").stat().st_mode & 0o777) == "0o600"
        assert client.get(f"/v1/jobs/{uuid4()}/events", headers=H).status_code == 404


def test_pending_placeholder_and_live_sse_while_running(settings):
    gate = asyncio.Event()
    fake = StreamingFake(hold=gate)
    app = create_app(settings, {"codex": fake})
    with TestClient(app) as client:
        bot = make_bot(client)
        job_id = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).json()["job"]["id"]

        def pending():
            msgs = client.get(f"/v1/bots/{bot['id']}/messages?pending=true", headers=H).json()
            return [m for m in msgs if m["pending"]]

        placeholder = wait_for(lambda: (p := pending()) and p[0]["text"] == "Hello" and p)[0]
        assert placeholder["job_id"] == job_id and placeholder["role"] == "assistant" and placeholder["id"] < 0
        # default listing stays free of placeholders
        assert not any(m["pending"] for m in client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json())
        mid = client.get(f"/v1/jobs/{job_id}/events", headers=H).json()
        assert not mid["done"] and mid["draft"] == "Hello" and mid["reply_text"] is None

        app.state.scheduler._loop_task.get_loop().call_soon_threadsafe(gate.set)
        frames = parse_sse(client.get(f"/v1/jobs/{job_id}/events", headers=SSE).text)  # replays then ends
        assert frames[-1]["event"] == "done" and frames[-1]["data"]["status"] == "succeeded"
        assert not pending()


def test_cancel_and_legacy_jobs_end_the_stream(settings):
    gate = asyncio.Event()
    app = create_app(settings, {"codex": StreamingFake(hold=gate)})
    with TestClient(app) as client:
        bot = make_bot(client)
        job_id = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hi"}, headers=H).json()["job"]["id"]
        wait_for(lambda: client.get(f"/v1/jobs/{job_id}/events", headers=H).json()["events"], timeout=30)
        client.post(f"/v1/jobs/{job_id}/cancel", headers=H)
        # cancel is asynchronous (the task is cancelled, its handler writes the terminal event later)
        wait_for(lambda: client.get(f"/v1/jobs/{job_id}/events", headers=H).json()["done"], timeout=30)
        frames = parse_sse(client.get(f"/v1/jobs/{job_id}/events", headers=SSE).text)
        assert frames[-1]["event"] == "done"
        # a finished job from before 0.4 has no events at all: closes at once with a synthetic done.
        # Its provider is not enabled, so the live scheduler (polling every 50 ms) can never claim and
        # run it between create and cancel; with "codex" that window made the test flaky under load.
        old = app.state.store.create("old", "retired-provider")
        app.state.store.cancel(old.id)
        frames = parse_sse(client.get(f"/v1/jobs/{old.id}/events", headers=SSE).text)
        assert [f["event"] for f in frames] == ["done"]


def test_stream_covers_inbox_routine_and_api_jobs(settings):
    fake = StreamingFake()
    app = create_app(settings, {"codex": fake})
    with TestClient(app) as client:
        bot = make_bot(client)
        inbox = client.post(f"/v1/bots/{bot['id']}/inbox", json={"text": "ping"}, headers=H).json()["job"]
        api_job = client.post("/v1/jobs", json={"prompt": "plain"}, headers=H).json()
        routine = client.post(
            f"/v1/bots/{bot['id']}/routines", json={"cron": "0 9 * * 1", "prompt": "weekly"}, headers=H
        ).json()
        app.state.scheduler.clock = lambda: datetime(2026, 11, 9, 14, 0, tzinfo=UTC)  # Mon 9:00 EST
        app.state.scheduler.tick()
        routine_job = wait_for(
            lambda: next((j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "routine"), None)
        )
        assert routine["id"] == routine_job["routine_id"]
        for job_id in (inbox["id"], api_job["id"], routine_job["id"]):
            snap = wait_for(
                lambda job_id=job_id: (s := client.get(f"/v1/jobs/{job_id}/events", headers=H).json())["done"] and s
            )
            assert {"start", "text", "final", "done"} <= {e["kind"] for e in snap["events"]}
        api_final = client.get(f"/v1/jobs/{api_job['id']}", headers=H).json()["job"]
        assert api_final["reply_text"] == "lo"  # no bot, no reply.txt: the last stream segment (0.12.24: "Hel" came before a tool call)


# ---------- audit ----------


def test_audit_keeps_full_reply_failures_and_survives_bot_delete(settings):
    big = "x" * 30_000

    class BigReply(StreamingFake):
        async def run(self, job, workspace, log_path, timeout):
            await super().run(job, workspace, log_path, timeout)
            (workspace / "reply.txt").write_text(big, encoding="utf-8")
            return await FakeAdapter.run(self, job, workspace, log_path, timeout)

    app = create_app(settings, {"codex": BigReply(reply_file=False)})
    with TestClient(app) as client:
        bot = make_bot(client)
        job_id = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "audit me"}, headers=H).json()["job"]["id"]
        wait_for(lambda: client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"]["status"] == "succeeded")
        chat = client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json()
        assert len(chat[-1]["text"]) == 20_000  # chat display is truncated ...
        job = client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"]
        assert job["reply_text"] == big  # ... the audit copy is not
        for key in ("prompt", "provider", "model", "effort", "source", "bot_id", "created_at", "started_at",
                    "finished_at", "exit_code"):
            assert job[key] is not None, key
        assert job["source"] == "chat" and job["effort"] == "high"
        listed = client.get("/v1/jobs", headers=H).json()
        assert "reply_text" not in listed[0]  # list stays light

        assert client.delete(f"/v1/bots/{bot['id']}", headers=H).status_code == 204
        kept = client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"]
        assert kept["reply_text"] == big and kept["prompt"] == "audit me"
        assert client.get(f"/v1/jobs/{job_id}/events", headers=H).json()["events"]


def test_failed_job_keeps_partial_output_and_error(settings):
    class Failing(StreamingFake):
        async def run(self, job, workspace, log_path, timeout):
            from botoffice.providers.base import RunResult
            from botoffice.streaming import emit_event

            emit_event("text", text="partial answer")
            log_path.write_text("log\n")
            return RunResult("failed", exit_code=3, error="exploded")

    app = create_app(settings, {"codex": Failing()})
    with TestClient(app) as client:
        job_id = client.post("/v1/jobs", json={"prompt": "p"}, headers=H).json()["id"]
        job = wait_for(lambda: (j := client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"])["status"] == "failed" and j)
        assert job["exit_code"] == 3 and job["error"] == "exploded" and job["reply_text"] == "partial answer"
        done = client.get(f"/v1/jobs/{job_id}/events", headers=H).json()["events"][-1]
        assert done["kind"] == "done" and done["status"] == "failed"
        assert oct((settings.data_dir / "logs" / f"{job_id}.log").stat().st_mode & 0o777) == "0o600"


# ---------- effort ----------


def test_effort_defaults_validation_and_change_keeps_identity(settings):
    fake = StreamingFake()
    app = create_app(settings, {"codex": fake, "claude": FakeAdapter(), "cursor": FakeAdapter()})
    with TestClient(app) as client:
        codex = make_bot(client, name="c1")
        assert codex["effort"] == "high"
        claude = make_bot(client, name="c2", engine="claude", model="claude-haiku-5-5", reason="why")
        assert claude["effort"] == "medium" and claude["capability_tier"] == "small"
        cursor = make_bot(client, name="c3", engine="cursor")
        assert cursor["effort"] is None
        bad = client.post("/v1/bots", json={"name": "b", "effort": "max"}, headers=H)  # codex has no max
        assert bad.status_code == 400 and "low, medium, high, xhigh" in bad.json()["detail"]
        assert client.post("/v1/bots", json={"name": "b", "engine": "cursor", "effort": "high"}, headers=H).status_code == 400
        assert client.patch(f"/v1/bots/{codex['id']}", json={"effort": "bogus"}, headers=H).status_code == 400
        assert client.patch(f"/v1/bots/{cursor['id']}", json={"effort": "low"}, headers=H).status_code == 400
        assert client.patch(f"/v1/bots/{claude['id']}", json={"effort": "max"}, headers=H).json()["effort"] == "max"

        # a job records the effort it was queued with; later changes do not rewrite it
        first = client.post(f"/v1/bots/{codex['id']}/messages", json={"text": "one"}, headers=H).json()["job"]
        assert first["effort"] == "high"
        wait_for(lambda: client.get(f"/v1/jobs/{first['id']}", headers=H).json()["job"]["status"] == "succeeded")
        before = client.get(f"/v1/bots/{codex['id']}", headers=H).json()
        after = client.patch(f"/v1/bots/{codex['id']}", json={"effort": "low"}, headers=H).json()
        assert after["effort"] == "low"
        for key in ("id", "name", "model", "instructions", "engine", "created_at"):
            assert after[key] == before[key]
        second = client.post(f"/v1/bots/{codex['id']}/messages", json={"text": "two"}, headers=H).json()["job"]
        assert second["effort"] == "low"
        assert client.get(f"/v1/jobs/{first['id']}", headers=H).json()["job"]["effort"] == "high"
        wait_for(lambda: any(j.effort == "low" for j in fake.jobs))

        # changing engine keeps a level the new engine accepts, else its default
        moved = client.patch(f"/v1/bots/{claude['id']}", json={"engine": "codex"}, headers=H).json()
        assert moved["effort"] == "high" and moved["model"] == "gpt-6.1-terra"  # "max" is not a codex level
        to_cursor = client.patch(f"/v1/bots/{codex['id']}", json={"engine": "cursor"}, headers=H).json()
        assert to_cursor["effort"] is None
        # direct jobs: explicit effort is validated too
        assert client.post("/v1/jobs", json={"prompt": "p", "effort": "max"}, headers=H).status_code == 400
        assert client.post("/v1/jobs", json={"prompt": "p", "effort": "medium"}, headers=H).json()["effort"] == "medium"


def test_model_choices_report_effort_levels_and_tiers(settings):
    providers = {"codex": FakeAdapter(), "claude": FakeAdapter(), "cursor": FakeAdapter(), "antigravity": FakeAdapter()}
    with TestClient(create_app(settings, providers)) as client:
        by_engine = {c["engine"]: c for c in client.get("/v1/model-choices", headers=H).json()}
    assert by_engine["codex"]["efforts"] == ["low", "medium", "high", "xhigh"]
    assert by_engine["codex"]["default_effort"] == "high"
    assert by_engine["claude"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert by_engine["claude"]["default_effort"] == "medium"
    assert by_engine["antigravity"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert by_engine["cursor"]["efforts"] == [] and by_engine["cursor"]["default_effort"] is None
    tiers = {m["id"]: m["capability_tier"] for m in by_engine["claude"]["models"]}
    assert tiers == {
        "claude-opus-5-5": "frontier", "claude-sonnet-5-5": "mid",
        "claude-haiku-5-5": "small", "claude-fable-5-1": "mid",
    }
    assert effort_levels("cursor") == ()


def test_effort_migration_is_additive_and_defaults_existing_bots(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = JobStore(path)
    codex = store.create_bot("Test Bot", "", "codex", None)
    claude = store.create_bot("Overflow work bot", "", "claude", "r", model="claude-haiku-5-5")
    cursor = store.create_bot("Scout", "", "cursor", None)
    job = store.create("old job", "codex", bot_id=codex.id)
    db = sqlite3.connect(path)  # simulate a 0.3.0 database: no effort values, no migration marker
    db.execute("UPDATE bots SET effort=NULL")
    db.execute("DELETE FROM meta WHERE key='crewdeck-effort-v1'")
    db.commit()
    db.close()
    again = JobStore(path)
    assert again.get_bot(codex.id).effort == "high"
    assert again.get_bot(claude.id).effort == "medium" and again.get_bot(claude.id).model == "claude-haiku-5-5"
    assert again.get_bot(cursor.id).effort is None
    assert again.get(job.id).effort is None  # past jobs are not rewritten
    again.update_bot(claude.id, effort="low")
    assert JobStore(path).get_bot(claude.id).effort == "low"  # runs once only


# ---------- capability tier ----------


@pytest.mark.parametrize(
    "model,tier",
    [
        ("claude-haiku-5-5", "small"), ("gpt-5.5-mini", "small"), ("gpt-6.1-nano", "small"),
        ("gemini-3.8-flash-lite", "small"), ("some-unknown-model", "small"), (None, "small"), ("", "small"),
        ("claude-sonnet-5-5", "mid"), ("claude-fable-5-1", "mid"), ("composer-2.5", "mid"),
        ("gemini-3.8-flash-high", "mid"), ("gpt-5.3-codex", "mid"), ("auto", "mid"),
        ("claude-opus-5-5", "frontier"), ("gpt-6.1-terra", "frontier"), ("o3", "frontier"),
        ("o4", "frontier"), ("claude-opus-5-thinking-high", "frontier"),
        ("o4-mini", "small"), ("gpt-6.1-mini", "small"),  # the lower tier wins
    ],
)
def test_capability_tier(model, tier):
    assert capability_tier(model) == tier


def test_tier_follows_model_and_is_not_editable(settings):
    app = create_app(settings, {"claude": FakeAdapter()})
    with TestClient(app) as client:
        bot = make_bot(client, engine="claude", model="claude-haiku-5-5", reason="r")
        assert bot["capability_tier"] == "small"
        up = client.patch(f"/v1/bots/{bot['id']}", json={"model": "claude-opus-5-5", "capability_tier": "small"}, headers=H)
        assert up.json()["capability_tier"] == "frontier"
        assert client.get(f"/v1/bots/{bot['id']}", headers=H).json()["capability_tier"] == "frontier"


# ---------- New York cron ----------


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_cron_is_new_york_time_across_dst():
    monday_nine = "0 9 * * 1"
    assert cron_matches(monday_nine, utc(2026, 3, 2, 14, 0))  # Mon 9:00 EST (UTC-5)
    assert not cron_matches(monday_nine, utc(2026, 3, 2, 13, 0))  # 8:00 EST
    assert not cron_matches(monday_nine, utc(2026, 3, 2, 9, 0))  # 9:00 UTC is not 9:00 ET
    # DST began Sunday 2026-03-08: the next Monday 9:00 is EDT (UTC-4)
    assert cron_matches(monday_nine, utc(2026, 3, 9, 13, 0))
    assert not cron_matches(monday_nine, utc(2026, 3, 9, 14, 0))
    # DST ended Sunday 2026-11-01: back to EST
    assert cron_matches(monday_nine, utc(2026, 11, 2, 14, 0))
    assert not cron_matches(monday_nine, utc(2026, 11, 2, 13, 0))
    # the weekday and hour are New York's: 02:00 UTC Tuesday is still Monday evening in New York
    assert cron_matches("0 21 * * 1", utc(2026, 11, 3, 2, 0))
    assert not cron_matches("0 21 * * 2", utc(2026, 11, 3, 2, 0))
    # naive datetimes are taken as New York wall-clock time
    assert cron_matches(monday_nine, datetime(2026, 11, 2, 9, 0))
    # other zones convert: Berlin 15:00 (CET) on Mon 2026-11-02 is 9:00 ET
    from zoneinfo import ZoneInfo

    assert cron_matches(monday_nine, datetime(2026, 11, 2, 15, 0, tzinfo=ZoneInfo("Europe/Berlin")))


def test_scheduler_fires_routine_at_ny_nine_not_utc_nine(settings):
    fake = FakeAdapter()
    app = create_app(settings, {"codex": fake})
    with TestClient(app) as client:
        bot = make_bot(client)
        r = client.post(
            f"/v1/bots/{bot['id']}/routines",
            json={"cron": "0 9 * * 1", "prompt": "weekly", "name": "site-check"},
            headers=H,
        ).json()
        assert r["name"] == "site-check"
        scheduler = app.state.scheduler

        def routine_jobs():
            return [j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "routine"]

        scheduler.clock = lambda: utc(2026, 11, 2, 9, 0)  # Mon 09:00 UTC = 04:00 ET
        scheduler.tick()
        assert not routine_jobs()
        scheduler.clock = lambda: utc(2026, 11, 2, 14, 0, 20)  # Mon 09:00 ET
        scheduler.tick()
        assert len(routine_jobs()) == 1
        scheduler.tick()  # same minute: not twice
        assert len(routine_jobs()) == 1


def test_scheduler_skips_repeated_hour_when_dst_ends(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        bot = make_bot(client)
        client.post(f"/v1/bots/{bot['id']}/routines", json={"cron": "30 1 * * *", "prompt": "nightly"}, headers=H)
        scheduler = app.state.scheduler
        count = lambda: len([j for j in client.get("/v1/jobs", headers=H).json() if j["source"] == "routine"])
        scheduler.clock = lambda: utc(2026, 11, 1, 5, 30)  # 1:30 EDT, first pass
        scheduler.tick()
        wait_for(lambda: count() == 1 and all(
            j["status"] != "running" for j in client.get("/v1/jobs", headers=H).json()))
        scheduler.clock = lambda: utc(2026, 11, 1, 6, 30)  # 1:30 EST, repeated wall-clock time
        scheduler.tick()
        assert count() == 1


def test_routine_name_optional_and_updatable(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        bot = make_bot(client)
        plain = client.post(f"/v1/bots/{bot['id']}/routines", json={"cron": "0 9 * * 1", "prompt": "p"}, headers=H).json()
        assert plain["name"] is None
        named = client.patch(f"/v1/routines/{plain['id']}", json={"name": "competitor-pulse", "enabled": False}, headers=H).json()
        assert named["name"] == "competitor-pulse" and named["enabled"] is False
        assert client.get(f"/v1/bots/{bot['id']}/routines", headers=H).json()[0]["name"] == "competitor-pulse"


def test_page_has_live_stream_effort_and_tier_hooks_without_token_in_urls():
    from botoffice.api import CHAT_PAGE

    html = CHAT_PAGE.read_text(encoding="utf-8")
    for needle in ("getReader()", "text/event-stream", 'id="f-effort"', 'id="f-tier"', 'createElement("details")'):
        assert needle in html
    assert "EventSource" not in html
    assert "?token" not in html and "access_token" not in html
    assert os.path.exists(CHAT_PAGE)


def test_claude_compact_shows_a_status_line_not_the_raw_result():
    """0.11.15: a finished /compact has an empty `result`; the chat said nothing useful (it fell back to the raw stream)."""
    boundary = {"type": "system", "subtype": "compact_boundary", "session_id": "aa7f34af-aabd-49e6-ae60-1a69189e14d8",
                "compact_metadata": {"trigger": "manual", "pre_tokens": 39951, "post_tokens": 10491, "duration_ms": 4686}}
    done = {"type": "result", "subtype": "success", "is_error": False, "result": "", "local_command": "compact", "num_turns": 0}
    parser = ClaudeParser()
    assert parser.feed(boundary) == []
    assert parser.feed(done) == [("final", {"text": "Conversation compacted (40k → 10.5k tokens, 4.7s)"})]
    assert parser.final_text == "Conversation compacted (40k → 10.5k tokens, 4.7s)"
    bare = ClaudeParser()  # no boundary line seen: still a short status
    assert bare.feed(done) == [("final", {"text": "Conversation compacted"})]
    # the CLI's own message (nothing to compact, not logged in) is kept as it is
    failed = ClaudeParser()
    assert failed.feed({**done, "result": "Not enough messages to compact."}) == [("final", {"text": "Not enough messages to compact."})]
    # an ordinary empty result is not turned into a compact status
    plain = ClaudeParser()
    assert plain.feed({"type": "result", "is_error": False, "result": ""}) == [("final", {"text": ""})]
