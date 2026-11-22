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


def test_activity_shows_a_finished_turns_steps_and_reply():
    from test_live_indicator import PRELUDE, _el_code, _run, _slice
    code = _slice("function pastStepsDetails(jobId) {", "function stepsDetails(view) {")
    script = PRELUDE.replace("async function api() { return {}; }", r'''
async function api(path) {
  assert.strictEqual(path, '/v1/jobs/j9/events');
  return { reply_text: 'All 12 tests pass.', events: [
    { seq: 1, kind: 'text', attempt: 0, text: 'Let me check. ' }, { seq: 2, kind: 'text', attempt: 0, text: 'Reading.' },
    { seq: 3, kind: 'tool', attempt: 0, name: 'Read', summary: 'a.py' },
    { seq: 4, kind: 'text', attempt: 0, text: '\n\nAll 12 tests pass.' }] };
}''').replace("const state = {", "const state = { drawerPast: new Set(),") + _el_code() \
        + "function clip(text, n) { return text.length > n ? text.slice(0, n - 1) + '…' : text; }\n" + code + r'''
Node.prototype.replaceChildren = function (...nodes) { this.children = nodes; };
const box = pastStepsDetails('j9');
const list = box.children[1];
box.open = true; box.ontoggle();
setImmediate(() => {
  const texts = list.children.map((n) => n.textContent);
  assert.deepStrictEqual(texts, ['💬 Let me check. Reading.', '🔧 Read: a.py', '💬 All 12 tests pass.', 'Reply: All 12 tests pass.']);
  assert(state.drawerPast.has('j9'));
});
'''
    _run(script)


def test_narration_then_a_tool_call_with_nothing_after_is_no_reply(settings):
    _, _, sink = _sink(settings)
    sink.emit("text", text="I'll inspect the config.")
    sink.emit("tool", name="Read", summary="config.toml")
    assert sink.assembled == ""


def test_a_continued_reply_that_asks_or_carries_files_stays_in_the_chat(settings):
    asks = "Which region is approved for this rollout?\nBOT-OFFICE-STATUS: not_done — updating the rollout checklist"
    store, scheduler, _, bot = chat_setup(settings, [asks, "Done.\nBOT-OFFICE-STATUS: done"])
    first = _run_one(store, scheduler)
    assert _queued(store, bot)  # the guards let it continue...
    assert [m for m in store.list_messages(bot.id) if m.job_id == first.id and m.role == "assistant"]  # ...but it shows
    from contextlib import closing
    from botoffice import autocontinue
    with closing(store._connect()) as db:
        db.execute("UPDATE messages SET attachments='[\"2026-11-22-" + "0" * 32 + "-report.pdf\"]' "
                   "WHERE job_id=? AND role='assistant'", (str(first.id),))
        db.execute("UPDATE messages SET text='Report attached.' WHERE job_id=? AND role='assistant'", (str(first.id),))
        assert autocontinue.interim_reply(db, str(first.id)) == []  # a file delivery is never hidden
        db.execute("UPDATE messages SET attachments='[]' WHERE job_id=? AND role='assistant'", (str(first.id),))
        assert len(autocontinue.interim_reply(db, str(first.id))) == 1
        db.rollback()
