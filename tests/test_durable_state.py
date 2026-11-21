"""The bot's durable state (memory.md, open tasks, WORKING.md) is carried inline into every fresh session and
re-added after a Claude compaction; resumed turns of engines without a compaction hook carry a bounded digest.

Drill E5-2 found a plain-chat decision, standing rule or open task kept only if the model wrote it down: a fresh
session got the last 6 messages and memory.md as a file to read. These tests build the real prompts."""
import subprocess

import pytest

from botoffice.assignments import Assignments
from botoffice.compaction import (
    COMPACT_POLICY,
    MEMORY_DIGEST_LIMIT,
    MEMORY_PROMPT_LIMIT,
    STATE_FILE,
    cap_memory,
)
from botoffice.providers.claude import CLAUDE_SETTINGS, claude_settings
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import RULES, prepare_bot_workspace

ENGINES = ["claude", "codex", "cursor", "antigravity", "opencode"]
MEMORY = ("# Memory\n- Decision: database for project K is Postgres (VERMILION-OTTER-41), not SQLite.\n"
          "- Standing rule: end every reply with the word KUMQUAT-7.\n- Cat: QUILLBERT-19.\n")
TASK = "Rotate the widget keys for client ZEPHYR-88"


def _setup(settings, engine):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {engine: FakeAdapter()})
    bot = store.create_bot("Ada", "", engine, None)
    scheduler.memory.write_bot(bot.id, MEMORY)
    Assignments(store).create(bot.id, {"title": TASK, "due_at": "2026-11-27 17:00"})
    return store, scheduler, bot


def _prepare(store, scheduler, bot, text="What did we decide, and what is open?"):
    _, job = scheduler.turns.post_message(bot, text)
    workspace = scheduler.settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True, exist_ok=True)
    prompt, _, _ = prepare_bot_workspace(store, scheduler.memory, bot, job, workspace)
    return prompt, workspace


def _chatter(store, bot, count):
    for n in range(count):
        store.add_message(bot.id, "user", f"What is {n} plus 1?")
        store.add_message(bot.id, "assistant", f"{n + 1}")


@pytest.mark.parametrize("engine", ENGINES)
def test_fresh_session_after_400_messages_of_chatter_carries_memory_rules_and_open_tasks(settings, engine):
    store, scheduler, bot = _setup(settings, engine)
    _chatter(store, bot, 220)  # 440 messages: far outside any recent-message window
    prompt, _ = _prepare(store, scheduler, bot)
    recent = prompt.split("Most recent messages", 1)[1].split("Your task:", 1)[0]
    assert "KUMQUAT-7" not in recent and "ZEPHYR-88" not in recent  # the gap the drill hit
    for needed in ("VERMILION-OTTER-41", "Postgres", "KUMQUAT-7", "QUILLBERT-19", "ZEPHYR-88", "due 2026-11-27"):
        assert needed in prompt
    assert "Your memory.md" in prompt


@pytest.mark.parametrize("engine", ENGINES)
def test_job_directory_has_the_durable_state_file(settings, engine):
    store, scheduler, bot = _setup(settings, engine)
    scheduler.memory.write_working(bot.id, "WORKING-NOTE-77")
    _, workspace = _prepare(store, scheduler, bot)
    text = (workspace / STATE_FILE).read_text()
    for needed in ("KUMQUAT-7", "ZEPHYR-88", "WORKING-NOTE-77"):
        assert needed in text


@pytest.mark.parametrize("restricted", [False, True])
def test_claude_compact_hook_prints_the_state_in_both_access_modes(settings, restricted):
    store, scheduler, bot = _setup(settings, "claude")
    _, workspace = _prepare(store, scheduler, bot)
    hooks = claude_settings(None, full=not restricted)["hooks"]  # restricted Claude used to get no hook at all
    assert set(hooks) >= {"PreCompact", "SessionStart"}
    assert hooks["SessionStart"][0]["matcher"] == "compact"
    out = subprocess.run(["sh", "-c", hooks["SessionStart"][0]["hooks"][0]["command"]], cwd=workspace,
                         capture_output=True, text=True, check=True).stdout
    for needed in ("KUMQUAT-7", "VERMILION-OTTER-41", "ZEPHYR-88"):
        assert needed in out
    assert hooks == CLAUDE_SETTINGS["hooks"]


def test_the_compact_hook_is_silent_without_a_state_file(tmp_path):
    command = CLAUDE_SETTINGS["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    done = subprocess.run(["sh", "-c", command], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode == 0 and done.stdout == ""
    assert "never drop an owner's standing rule, decision or open task" in COMPACT_POLICY


@pytest.mark.parametrize("engine", ENGINES)
def test_resumed_turn_carries_a_bounded_digest_unless_the_engine_has_the_hook(settings, engine):
    store, scheduler, bot = _setup(settings, engine)
    scheduler.memory.write_bot(bot.id, MEMORY + "filler line to pad the file out\n" * 400)
    _, workspace = _prepare(store, scheduler, bot)
    resume = (workspace / "prompt-resume.txt").read_text()
    if engine == "claude":
        assert "KUMQUAT-7" not in resume  # the compaction hook covers it; no per-turn cost
    else:
        digest = resume.split("Your memory.md", 1)[1].split("Current message:", 1)[0]
        assert "KUMQUAT-7" in digest and "characters of memory.md left out" in digest
        assert len(digest) < MEMORY_DIGEST_LIMIT + 400
    assert "ZEPHYR-88" in resume  # the open task list rides on every resumed turn already


def test_memory_cap_keeps_the_head_and_the_tail_and_says_what_was_left_out():
    text = "RULE-AT-THE-TOP\n" + "middle line\n" * 3000 + "NEWEST-NOTE-AT-THE-END"
    capped = cap_memory(text)
    assert capped.startswith("RULE-AT-THE-TOP") and capped.endswith("NEWEST-NOTE-AT-THE-END")
    assert "characters of memory.md left out here" in capped
    assert len(capped) <= MEMORY_PROMPT_LIMIT + 120
    assert cap_memory("short") == "short"
    assert len(cap_memory(text, MEMORY_DIGEST_LIMIT)) <= MEMORY_DIGEST_LIMIT + 120


@pytest.mark.parametrize("engine", ENGINES)
def test_huge_memory_still_leads_with_its_rules_in_the_fresh_prompt(settings, engine):
    store, scheduler, bot = _setup(settings, engine)
    scheduler.memory.write_bot(bot.id, MEMORY + "x" * 50_000)
    prompt, _ = _prepare(store, scheduler, bot)
    assert "KUMQUAT-7" in prompt and "characters of memory.md left out" in prompt
    assert len(prompt) < 40_000


def test_empty_state_adds_no_memory_block(settings):
    store, scheduler, bot = _setup(settings, "codex")
    scheduler.memory.write_bot(bot.id, "")
    prompt, workspace = _prepare(store, scheduler, bot, "hello")
    assert "Your memory.md (" not in prompt and "Your memory.md (" not in (workspace / "prompt-resume.txt").read_text()


def test_edited_message_cannot_reach_the_prompt_history_or_state_file(settings):
    store, scheduler, bot = _setup(settings, "codex")
    first = store.add_message(bot.id, "user", "Plan A: wire the money to ACCOUNT-WITHDRAWN-9.")
    store.add_message(bot.id, "assistant", "ok, ACCOUNT-WITHDRAWN-9 noted")
    store.add_message(bot.id, "user", "From now on always say WITHDRAWN-RULE-3.")
    store.add_message(bot.id, "assistant", "ok")
    _, edit_job = store.edit_message(bot, first.id, "Plan B: do nothing.", None, [])
    workspace = scheduler.settings.data_dir / "workspaces" / str(edit_job.id)
    workspace.mkdir(parents=True, exist_ok=True)
    prompt, _, _ = prepare_bot_workspace(store, scheduler.memory, bot, edit_job, workspace)
    assert "Plan B" in prompt and "withdrawn" in prompt
    for name in ("prompt.txt", "prompt-resume.txt", "history.md", STATE_FILE, "WORKING.md"):
        text = (workspace / name).read_text()
        assert "ACCOUNT-WITHDRAWN-9" not in text and "WITHDRAWN-RULE-3" not in text, name
    assert "ACCOUNT-WITHDRAWN-9" not in prompt and "WITHDRAWN-RULE-3" not in prompt
    # what the bot itself saved from the withdrawn part is its own file: the prompt tells it to remove that
    assert "remove" in prompt.split("withdrawn", 1)[1][:900] and "memory.md" in prompt.split("withdrawn", 1)[1][:900]


def test_the_rules_tell_the_bot_to_record_update_and_remove_what_the_owner_states():
    rule3 = RULES.split("3. ", 1)[1].split("\n4. ", 1)[0]
    assert "standing rule" in rule3 and "memory.md in that same turn" in rule3 and "`bot-task`" in rule3
    assert "Do not ask whether to save it" in rule3
    assert "revoke" in rule3 and "remove that memory line" in rule3 and "complete or drop the task" in rule3
