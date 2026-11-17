"""Codex reports the thread's running total in turn.completed; a job's usage is the delta."""
import asyncio
import json

from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter

from test_session_resume import SID, _fake, _job, _workspace


def _lines(thread, total=None, final="ok"):
    out = [{"type": "thread.started", "thread_id": thread},
           {"type": "item.completed", "item": {"type": "agent_message", "text": final}}]
    if total is not None:
        out.append({"type": "turn.completed", "usage": total})
    return [json.dumps(o).encode() + b"\n" for o in out]


def _run(adapter, job, ws, tmp_path):
    return asyncio.run(adapter.run(job, ws, tmp_path / f"{job.id}.log", 5))


def test_cumulative_totals_become_per_job_deltas(monkeypatch, tmp_path):
    ws, adapter, bot = _workspace(tmp_path), CodexAdapter(executable="codex"), _job().bot_id
    plan = [(_lines(SID, {"input_tokens": 1000, "output_tokens": 10}), 0),
            (_lines(SID, {"input_tokens": 1900, "output_tokens": 25}), 0),
            (_lines(SID, {"input_tokens": 50, "output_tokens": 5}), 0),  # total went down: a reset
            (_lines(SID, None), 0)]  # reported nothing
    _fake(monkeypatch, plan, [])
    usage = [_run(adapter, _job(bot_id=bot), ws, tmp_path).usage for _ in range(4)]
    assert usage == [{"input_tokens": 1000, "output_tokens": 10},  # new thread: baseline 0
                     {"input_tokens": 900, "output_tokens": 15},
                     {"input_tokens": 50, "output_tokens": 5},
                     None]


def test_resumed_thread_without_a_baseline_is_not_charged_its_history(monkeypatch, tmp_path):
    ws, adapter, bot = _workspace(tmp_path), CodexAdapter(executable="codex"), _job().bot_id
    adapter._save_session(ws, _job(bot_id=bot), SID)
    _fake(monkeypatch, [(_lines(SID, {"input_tokens": 15_100_000}), 0),
                        (_lines(SID, {"input_tokens": 17_100_000}), 0)], [])
    assert _run(adapter, _job(bot_id=bot), ws, tmp_path).usage is None
    assert _run(adapter, _job(bot_id=bot), ws, tmp_path).usage == {"input_tokens": 2_000_000}


def test_interrupted_and_resumed_segments_are_both_counted(tmp_path):
    ws, adapter, job = _workspace(tmp_path), CodexAdapter(executable="codex"), _job()
    adapter._save_session(ws, job, SID)
    adapter._baseline_file(ws, job).write_text(json.dumps({"thread": SID, "total": {"input_tokens": 1000}}))
    # first segment interrupted after reporting a total; the resume's total includes it
    segments = [(SID, SID, {"input_tokens": 1400}), (SID, SID, {"input_tokens": 1900})]
    assert adapter._job_usage(ws, job, segments) == {"input_tokens": 900}
    # an interrupted segment that printed no turn.completed is still inside the resumed total
    adapter._baseline_file(ws, job).write_text(json.dumps({"thread": SID, "total": {"input_tokens": 1000}}))
    assert adapter._job_usage(ws, job, [(SID, SID, None), (SID, SID, {"input_tokens": 1700})]) == {"input_tokens": 700}
    # the baseline moved on: the next job starts from 1700
    assert adapter._job_usage(ws, job, [(SID, SID, {"input_tokens": 1750})]) == {"input_tokens": 50}


def test_failed_resume_with_fresh_thread_counts_both_threads(tmp_path):
    ws, adapter, job = _workspace(tmp_path), CodexAdapter(executable="codex"), _job()
    adapter._save_session(ws, job, SID)
    adapter._baseline_file(ws, job).write_text(json.dumps({"thread": SID, "total": {"input_tokens": 100}}))
    new = "11111111-2222-3333-4444-555555555555"
    segments = [(SID, SID, {"input_tokens": 130}), (None, new, {"input_tokens": 40})]
    assert adapter._job_usage(ws, job, segments) == {"input_tokens": 70}


def test_other_engines_sum_their_interrupted_and_resumed_segments(tmp_path):
    ws, adapter, job = _workspace(tmp_path), ClaudeAdapter(executable="claude"), _job()
    segments = [("s", "s", {"input_tokens": 10, "output_tokens": 2}), ("s", "s", {"input_tokens": 5, "cost_usd": 0.5}),
                ("s", "s", None)]
    assert adapter._job_usage(ws, job, segments) == {"input_tokens": 15, "output_tokens": 2, "cost_usd": 0.5}
    assert adapter._job_usage(ws, job, [("s", "s", None)]) is None


def test_production_sequence_of_one_resumed_thread_gives_per_job_deltas(monkeypatch, tmp_path):
    """Stored usage_json input_tokens of consecutive jobs of one resumed thread (14 Nov 2026, 18:41-18:56 UTC)."""
    ws, adapter, bot = _workspace(tmp_path), CodexAdapter(executable="codex"), _job().bot_id
    adapter._save_session(ws, _job(bot_id=bot), SID)
    adapter._baseline_file(ws, _job(bot_id=bot)).parent.mkdir(parents=True, exist_ok=True)
    adapter._baseline_file(ws, _job(bot_id=bot)).write_text(json.dumps({"thread": SID, "total": {"input_tokens": 15_144_947}}))
    totals = [16_512_928, 16_867_165, 17_096_748]
    _fake(monkeypatch, [(_lines(SID, {"input_tokens": t}), 0) for t in totals], [])
    usage = [_run(adapter, _job(bot_id=bot), ws, tmp_path).usage for _ in totals]
    assert usage == [{"input_tokens": 1_367_981}, {"input_tokens": 354_237}, {"input_tokens": 229_583}]
    path = adapter._baseline_file(ws, _job(bot_id=bot))
    assert json.loads(path.read_text())["total"] == {"input_tokens": 17_096_748}
    assert oct(path.stat().st_mode & 0o777) == "0o600" and not path.with_name(path.name + ".tmp").exists()


def test_job_without_a_bot_reads_and_writes_no_baseline(tmp_path):
    ws, adapter = _workspace(tmp_path), CodexAdapter(executable="codex")
    job = _job(bot_id=None)
    assert adapter._job_usage(ws, job, [(SID, SID, {"input_tokens": 5})]) is None
    assert adapter._job_usage(ws, job, [(None, SID, {"input_tokens": 5})]) == {"input_tokens": 5}
    assert not list(tmp_path.rglob("codex-usage-baseline.json*"))
