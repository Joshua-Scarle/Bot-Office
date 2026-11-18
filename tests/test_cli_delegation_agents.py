from botoffice.memory import Memory
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import (
    CLI_DELEGATION_END,
    CLI_DELEGATION_SECTION,
    CLI_DELEGATION_START,
    prepare_bot_workspace,
    upsert_delimited_section,
)


def setup_turn(settings, provider="codex"):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {provider: FakeAdapter()})
    bot = store.create_bot("Ada", "", provider, None)
    _, job = scheduler.turns.post_message(bot, "hello")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    memory = Memory(settings.data_dir)
    return store, memory, bot, job, workspace


def test_generated_files_and_persistent_user_content(settings):
    store, memory, bot, job, workspace = setup_turn(settings)
    bot_dir = memory.bot_dir(bot.id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "AGENTS.md").write_text("User instructions\n")
    (bot_dir / "CLAUDE.md").write_text("Claude instructions\n")
    (bot_dir / "GEMINI.md").write_text("Gemini instructions\n")
    for _ in range(2):
        prepare_bot_workspace(store, memory, bot, job, workspace)
    for name, user_text in [("AGENTS.md", "User instructions"), ("CLAUDE.md", "Claude instructions"),
                            ("GEMINI.md", "Gemini instructions")]:
        text = (workspace / name).read_text()
        assert user_text in text
        assert text == (bot_dir / name).read_text()
        assert text.count(CLI_DELEGATION_START) == 1
        assert CLI_DELEGATION_SECTION in text
        for expected in ("codex exec -m <model>", "claude -p --model <model>",
                         "cursor-agent -p --force --sandbox disabled", "agy -p --dangerously-skip-permissions",
                         "opencode run --auto", "Cursor's built-in subagents", "background agents",
                         "model switching", "If the owner's standing rules"):
            assert expected in text
        assert "state the reason" not in text and "gpt-6.1-terra" not in text
    assert CLI_DELEGATION_SECTION not in (workspace / "prompt.txt").read_text()
    assert CLI_DELEGATION_SECTION not in (workspace / "prompt-resume.txt").read_text()


def test_missing_provider_files_use_agents_content(settings):
    store, memory, bot, job, workspace = setup_turn(settings)
    prepare_bot_workspace(store, memory, bot, job, workspace)
    agents = (workspace / "AGENTS.md").read_text()
    assert agents == (workspace / "CLAUDE.md").read_text() == (workspace / "GEMINI.md").read_text()
    assert not (memory.bot_dir(bot.id) / "CLAUDE.md").exists()
    assert not (memory.bot_dir(bot.id) / "GEMINI.md").exists()


def test_upsert_replaces_only_marked_block():
    original = f"before\n{CLI_DELEGATION_START}\nold\n{CLI_DELEGATION_END}\nafter"
    updated = upsert_delimited_section(original, CLI_DELEGATION_START, CLI_DELEGATION_END, CLI_DELEGATION_SECTION)
    assert updated == f"before\n{CLI_DELEGATION_SECTION}\nafter"
    assert upsert_delimited_section(updated, CLI_DELEGATION_START, CLI_DELEGATION_END, CLI_DELEGATION_SECTION) == updated


def test_cursor_workspace_preserves_its_own_content(settings):
    store, memory, bot, job, workspace = setup_turn(settings, "cursor")
    cursor_dir = memory.bot_dir(bot.id) / "cursor-workspace"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "AGENTS.md").write_text("Cursor user instructions\n")
    prepare_bot_workspace(store, memory, bot, job, workspace)
    text = (cursor_dir / "AGENTS.md").read_text()
    assert text.startswith("Cursor user instructions\n")
    assert CLI_DELEGATION_SECTION in text
    prepare_bot_workspace(store, memory, bot, job, workspace)
    assert (cursor_dir / "AGENTS.md").read_text() == text
