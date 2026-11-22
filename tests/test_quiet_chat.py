"""0.12.24: Bot-Office chat works like Grok Bot: the CLI's step-by-step stream stays in the job log
(Activity); only deliberate messages reach the chat (desk say notes, questions, the final reply)."""
from botoffice import workspace
from botoffice.baseline import SYSTEM
from botoffice.domain import JobStatus
from botoffice.streaming import EventSink
from botoffice.store import JobStore

from test_auto_continue import NOT_DONE, _queued, _run_one
from test_auto_continue import _setup as chat_setup
from test_loop import run_all
from test_loop import setup as loop_setup


def _sink(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    job = store.create("p", "codex")
    return store, job, EventSink(store, job.id)


def test_narration_before_a_tool_call_is_never_the_reply(settings):
    store, job, sink = _sink(settings)
    sink.emit("text", text="Let me look at the config. ")
    sink.emit("tool", name="Read", summary="config.toml")
    sink.emit("text", text="Now running the tests...")
    sink.emit("thinking", text="hmm")
    sink.emit("text", text="All 12 tests pass.")
    sink.flush()
    assert sink.assembled == "All 12 tests pass."  # no final event: the last segment only
    assert "Let me look" in sink.draft and "Now running" in sink.draft  # the log keeps every step
    assert "Now running" in (store.get(job.id).draft_text or "")
    sink.emit("final", text="Tests: 12 passed.")
    assert sink.assembled == "Tests: 12 passed."  # a reported final reply wins


def test_a_whole_message_replace_is_its_own_segment(settings):
    _, _, sink = _sink(settings)
    sink.emit("text", text="Checking the logs.", replace=True)
    sink.emit("tool", name="shell", summary="grep")
    sink.emit("text", text="Found it: a typo in the cron line.", replace=True)
    assert sink.assembled == "Found it: a typo in the cron line."


def test_a_continued_chat_turn_keeps_its_reply_out_of_the_chat(settings):
    store, scheduler, _, bot = chat_setup(settings, [NOT_DONE, "Built it; tests pass.\nBOT-OFFICE-STATUS: done"])
    first = _run_one(store, scheduler)
    assert first.status == JobStatus.INCOMPLETE and _queued(store, bot)
    shown = [m for m in store.list_messages(bot.id) if m.role == "assistant"]
    assert not [m for m in shown if m.job_id == first.id]  # an interim step, not a message
    kept = [m for m in store.list_messages(bot.id, include_hidden=True) if m.job_id == first.id and m.role == "assistant"]
    assert kept and kept[0].hidden and "Halfway there." in kept[0].text  # kept for Activity
    assert "Halfway there." in (store.get(first.id).reply_text or "")
    assert "your final reply reports the whole task" in _queued(store, bot)[0]["prompt"]
    second = _run_one(store, scheduler)
    assert second.status == JobStatus.SUCCEEDED
    assert [m.text for m in store.list_messages(bot.id) if m.role == "assistant"] == ["Built it; tests pass."]


def test_a_not_done_turn_that_stops_stays_in_the_chat(settings):
    reply = "Need the API key from you.\nBOT-OFFICE-STATUS: not_done — please send the key"
    store, scheduler, _, bot = chat_setup(settings, [reply])
    first = _run_one(store, scheduler)
    assert not _queued(store, bot)  # needs the owner: no continuation
    assert [m.text for m in store.list_messages(bot.id) if m.role == "assistant" and m.job_id == first.id]


def test_say_notes_of_a_continued_turn_stay_in_the_chat(settings):
    holder = {}

    def during(job):
        if not holder:
            holder["say"] = store_ref[0].say(job.bot_id, job.id, "On it: building the thing, about 10 min.")

    store_ref = []
    store, scheduler, adapter, bot = chat_setup(settings, [NOT_DONE, "Done.\nBOT-OFFICE-STATUS: done"])
    store_ref.append(store)
    adapter.during = during
    _run_one(store, scheduler)
    notes = [m for m in store.list_messages(bot.id) if m.kind == "say"]
    assert [m.text for m in notes] == ["On it: building the thing, about 10 min."]


def test_assignment_steps_that_carry_on_are_not_chat_messages(settings):
    store, scheduler, _, bot, item = loop_setup(
        settings, "Step 1 done.\nBOT-OFFICE-STATUS: continue", "Step 2 done.\nBOT-OFFICE-STATUS: continue",
        "Step 3 done.\nBOT-OFFICE-STATUS: continue", budget_turns=3)
    jobs = run_all(store, scheduler)
    assert len(jobs) == 3
    shown = [m.text for m in store.list_messages(bot.id) if m.role == "assistant"]
    # steps 1 and 2 queued the next step (hidden); step 3 hit the budget, so the owner reads it
    assert shown == ["Step 3 done."]
    hidden = [m.text for m in store.list_messages(bot.id, include_hidden=True) if m.role == "assistant" and m.hidden]
    assert hidden == ["Step 1 done.", "Step 2 done."]


def test_prompts_ask_for_deliberate_messages():
    system = SYSTEM.default_text()
    assert "never narrate steps" in system and "acknowledgement first" in system
    assert "Only your final message reaches the chat" in system
    assert "acknowledgement first" in workspace.RULES and "never for routine steps" in workspace.RULES
    assert "streams live" not in workspace.RULES


def test_reply_rules_say_the_stream_is_not_shown():
    from types import SimpleNamespace
    for provider in ("codex", "claude", "antigravity"):
        rule = workspace._reply_rule(SimpleNamespace(provider=provider, source="chat", origin_job_id=None))
        assert "do not narrate your steps" in rule and "streams live" not in rule
