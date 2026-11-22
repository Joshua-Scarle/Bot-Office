import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.base import RunResult
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.streams import ClaudeParser, CursorParser
from botoffice.streaming import emit_event


def assistant(text, *, partial=False):
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if partial:
        event["timestamp_ms"] = 1
    return event


def parse(lines):
    parser = CursorParser()
    events = [event for line in lines for event in parser.feed(line)]
    return parser, "".join(data["text"] for kind, data in events if kind == "text"), events


def test_sanitized_cursor_trace():
    fixture = Path(__file__).parent / "fixtures" / "cursor-segments.jsonl"
    lines = [json.loads(line) for line in fixture.read_text().splitlines()]
    parser, draft, events = parse(lines)
    first = "First synthetic sentence for a preliminary check."
    second = "Second synthetic sentence describes another routine check."
    last = ("Final synthetic sentence reports the outcome of the checks. "
            + "All checks passed safely and the example is complete. " * 3)
    assert draft == first + "\n\n" + second + "\n\n" + last
    assert parser.final_text == last.strip()
    assert events[-1] == ("final", {"text": last.strip()})
    assert parser.usage == lines[-1]["usage"]


def test_cursor_without_partial_output():
    parser, draft, _ = parse([assistant("First."), {"type": "tool_call", "subtype": "completed"},
                              assistant(" Last. "), {"type": "result", "result": "First. Last."}])
    assert draft == "First.\n\n Last. "
    assert parser.final_text == "Last."


def test_cursor_single_segment_and_result_fallback():
    parser, draft, _ = parse([assistant("Hi", partial=True), assistant("Hi", partial=True),
                              assistant("HiHi"), {"type": "result", "result": "wrong"}])
    assert draft == parser.final_text == "HiHi"
    parser, _, events = parse([{"type": "result", "result": "fallback", "usage": {"tokens": 3}}])
    assert parser.final_text == "fallback" and parser.usage == {"tokens": 3}
    assert events[-1] == ("final", {"text": "fallback"})


def test_cursor_all_segments_snapshot_and_error():
    parser, draft, events = parse([assistant("One.", partial=True), {"type": "thinking"},
                                  assistant("Two.", partial=True), assistant("One.Two."),
                                  {"type": "result", "is_error": True, "result": "failed",
                                   "usage": {"tokens": 2}}])
    assert draft == "One.\n\nTwo."
    assert parser.final_text is None and parser.error_text == "failed"
    assert parser.usage == {"tokens": 2} and events[-1] == ("error", {"message": "failed"})


def test_cursor_empty_last_segment_is_an_empty_reply_like_claude():
    tool = {"type": "tool_call", "subtype": "started", "tool_call": {"shellToolCall": {"args": {"command": "ls"}}}}
    parser, draft, events = parse([assistant("Let me check.", partial=True), tool, assistant(""),
                                  {"type": "result", "result": ""}])
    assert draft == "Let me check."
    assert parser.final_text == "" and events[-1] == ("final", {"text": ""})
    claude = ClaudeParser()
    for line in ({"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me check."}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
                 {"type": "result", "result": ""}):
        claude.feed(line)
    assert claude.final_text == parser.final_text


# ---------- through the scheduler: the stored reply ----------

H = {"Authorization": "Bearer test-token"}
TOOL = {"type": "tool_call", "subtype": "started", "tool_call": {"readToolCall": {"args": {"path": "a.md"}}}}


class CursorReplay(FakeAdapter):
    """Feeds Cursor stream lines through the real parser into the job's event sink."""

    def __init__(self, lines, reply=None):
        super().__init__()
        self.lines, self.reply = lines, reply

    async def run(self, job, workspace, log_path, timeout):
        parser = CursorParser()
        for line in self.lines:
            for kind, data in parser.feed(line):
                emit_event(kind, **data)
        if self.reply is not None:
            (workspace / "reply.txt").write_text(self.reply, encoding="utf-8")
        if parser.final_text is None:  # error, cancel or timeout: no final event
            return RunResult("failed", exit_code=1, error="stopped")
        return RunResult("succeeded", exit_code=0, final_text=parser.final_text)


def stored_reply(settings, lines, reply=None, bot=False):
    app = create_app(settings, {"codex": CursorReplay(lines, reply)})
    with TestClient(app) as client:
        if bot:
            bot_id = client.post("/v1/bots", json={"name": "helper", "instructions": "x"}, headers=H).json()["id"]
            job_id = client.post(f"/v1/bots/{bot_id}/messages", json={"text": "hi"}, headers=H).json()["job"]["id"]
        else:
            job_id = client.post("/v1/jobs", json={"prompt": "p"}, headers=H).json()["id"]
        deadline = time.monotonic() + 10
        while (job := client.get(f"/v1/jobs/{job_id}", headers=H).json()["job"])["status"] in ("queued", "running"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return job


def test_partial_reply_without_final_separates_segments_once(settings):
    lines = [assistant("One.", partial=True), assistant("One."), TOOL, assistant("Two", partial=True),
             assistant(".", partial=True), assistant("Two."), {"type": "thinking", "text": "hm"}]
    job = stored_reply(settings, [TOOL, *lines])
    # 0.12.24: with no final reply, the kept reply is the last segment: "One." came before a tool call
    assert job["status"] == "failed" and job["reply_text"] == "Two."


def test_final_event_and_reply_file_win_over_the_draft(settings):
    lines = [assistant("One.", partial=True), TOOL, assistant("Two.", partial=True), assistant("Two."),
             {"type": "result", "result": "One.Two."}]
    assert stored_reply(settings, lines)["reply_text"] == "Two."
    assert stored_reply(settings, lines, reply="From the file.", bot=True)["reply_text"] == "From the file."


def test_cursor_timestamped_segment_snapshots_are_not_repeated():
    fixture = Path(__file__).parent / "fixtures" / "cursor-timestamped-snapshots.jsonl"
    lines = [json.loads(line) for line in fixture.read_text().splitlines()]
    parser, draft, events = parse(lines)
    segments = ["I'll read the job prompt and follow it from the job directory.",
                ("I'll read the session files and standing rules first, then run the date and hostname check "
                 "the message asks for."),
                "I'll check the date.", "I'll check the hostname next.",
                "The date is Mon Jan 5 09:00:00 UTC 2026 and the hostname is example-host."]
    assert draft == "\n\n".join(segments)
    assert parser.final_text == segments[-1] and events[-1] == ("final", {"text": segments[-1]})


def test_cursor_short_repeated_delta_is_kept():
    parser, draft, _ = parse([assistant("ha", partial=True), assistant("ha", partial=True),
                              {"type": "result", "result": "haha"}])
    assert draft == parser.final_text == "haha"
