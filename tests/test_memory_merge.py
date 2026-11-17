"""0.9.0: shared/bot memory edits merge; SELF.md, settings-change note, worklog (A7)."""
import asyncio
import threading

from botoffice.domain import Slot
from botoffice.memory import WORKLOG_LINES, Memory, merge3
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore
from botoffice.workspace import prepare_bot_workspace


def test_merge3_keeps_both_sides_and_reports_real_conflicts():
    base = "a\nb\nc\n"
    assert merge3(base, "a\nB\nc\n", "a\nb\nC\n") == ("a\nB\nC\n", [])
    assert merge3(base, base + "one\n", base + "two\n") == (base + "one\ntwo\n", [])
    assert merge3(base, "a\nB\nc\n", "a\nX\nc\n") == ("a\nB\nc\n", ["X"])


def test_conflicting_edit_is_appended_under_a_marker(tmp_path):
    memory = Memory(tmp_path)
    memory.write_shared("fact: old\n")
    memory.write_shared("fact: from the GUI\n")
    merged, conflict = memory.merge_write(memory.shared_path, "fact: old\n", "fact: from the bot\n", "job-1")
    assert conflict is None and merged == "fact: from the GUI\n\n<!-- merged from job job-1 -->\nfact: from the bot\n"
    assert memory.read_shared() == merged


def test_concurrent_shared_memory_writes_all_survive(tmp_path):
    memory = Memory(tmp_path)
    memory.write_shared("# Shared\n")
    base = memory.read_shared()  # every job started from the same snapshot

    def job(number):
        memory.merge_write(memory.shared_path, base, base + f"fact {number}\n", number)

    threads = [threading.Thread(target=job, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    text = memory.read_shared()
    assert all(f"fact {n}\n" in text for n in range(20)) and text.startswith("# Shared\n")


def test_two_bots_finishing_after_each_other_keep_both_shared_facts(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    scheduler.memory.write_shared("shared\n")
    workspaces = []
    for name in ("A", "B"):
        bot = store.create_bot(name, "", "codex", None)
        _, job = scheduler.turns.post_message(bot, "hi")
        workspace = settings.data_dir / "workspaces" / str(job.id)
        workspace.mkdir(parents=True)
        scheduler._snapshots[str(job.id)] = ("", "shared\n")
        (workspace / "shared-memory.md").write_text(f"shared\n{name} learned something\n")
        workspaces.append((job, workspace))
    for job, workspace in workspaces:
        scheduler._write_memory(job, workspace)
    assert scheduler.memory.read_shared() == "shared\nA learned something\nB learned something\n"


def test_self_md_settings_change_note_and_worklog(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "first")
    claimed = store.claim(("desktop-1",), {"codex"})
    asyncio.run(scheduler._execute(0, claimed))
    workspace = settings.data_dir / "workspaces" / str(job.id)
    self_md = (workspace / "SELF.md").read_text()
    assert "Name: Ada" in self_md and "CLI access: full" in self_md and "`desktop-1`" in self_md
    assert "Your settings changed" not in (workspace / "prompt-resume.txt").read_text()
    worklog = scheduler.memory.read_worklog(bot.id).splitlines()
    assert len(worklog) == 1 and " · succeeded · chat · first → " in worklog[0]

    bot = store.update_bot(bot.id, model="gpt-5.5", instructions="Be brief.")
    _, second = scheduler.turns.post_message(bot, "second")
    second_ws = settings.data_dir / "workspaces" / str(second.id)
    second_ws.mkdir(parents=True)
    prepare_bot_workspace(store, scheduler.memory, bot, store.get(second.id), second_ws)
    resume = (second_ws / "prompt-resume.txt").read_text()
    assert "Your settings changed since your last turn: model, instructions" in resume
    assert "first" in (second_ws / "worklog.md").read_text()
    assert "Read SELF.md" in (second_ws / "prompt.txt").read_text()


def test_worklog_keeps_the_last_lines(tmp_path):
    memory = Memory(tmp_path)
    for number in range(WORKLOG_LINES + 5):
        memory.append_worklog("bot", f"line {number}\nwith a newline")
    lines = memory.read_worklog("bot").splitlines()
    assert len(lines) == WORKLOG_LINES and lines[-1] == f"line {WORKLOG_LINES + 4} with a newline"
    assert lines[0] == "line 5 with a newline"


# --- review round 1 ---

def test_concurrent_inserts_keep_repeated_lines_in_order():
    base = "x\n"
    assert merge3(base, base + "A\n", base + "A\nB\nA\n") == ("x\nA\nB\nA\n", [])
    assert merge3(base, base + "A\nB\n", base + "B\n") == ("x\nA\nB\n", [])


def _unmerged(memory, path):
    return sorted(path.parent.glob(f"{path.stem}.conflict-*.md"))


def test_oversized_merge_skips_the_diff_and_keeps_the_job_version_aside(tmp_path, monkeypatch):
    import time

    from botoffice import memory as memory_module

    def boom(*args, **kwargs):
        raise AssertionError("difflib must not run above the cap")

    monkeypatch.setattr(memory_module.difflib, "SequenceMatcher", boom)
    base = "".join(f"line {n}\n" for n in range(memory_module.MERGE_MAX_LINES + 10))
    current = base + "from the GUI\n"
    # reordered and repeated lines: a set-based compare would call these "already present"
    ours = "line 5\nline 1\nline 1\n" + base.replace("line 3\n", "") + "from the bot\n"
    started = time.monotonic()
    assert merge3(base, current, ours) is None
    memory = Memory(tmp_path)
    memory.write_shared(current)
    merged, conflict = memory.merge_write(memory.shared_path, base, ours, "j1", who="Ada")
    assert time.monotonic() - started < 1
    assert merged.startswith(current) and "## Unmerged edit from Ada (" in merged and conflict.name in merged
    assert _unmerged(memory, memory.shared_path) == [conflict]
    assert conflict.read_text() == ours  # the job's whole version, nothing lost
    assert memory.read_shared() == merged


def test_merge_write_reuses_a_prepared_merge_and_remerges_if_the_file_moved(tmp_path, monkeypatch):
    from botoffice import memory as memory_module

    memory = Memory(tmp_path)
    memory.write_shared("a\n")
    prepared = memory.prepare_merge(memory.shared_path, "a\n", "a\nbot\n")
    real = memory_module.merge3
    monkeypatch.setattr(memory_module, "merge3", lambda *a: (_ for _ in ()).throw(AssertionError("no diff")))
    assert memory.merge_write(memory.shared_path, "a\n", "a\nbot\n", "j1", prepared) == ("a\nbot\n", None)
    monkeypatch.setattr(memory_module, "merge3", real)
    memory.write_shared("a\nbot\ngui\n")  # changed after the merge was prepared: merged again, bounded
    stale = ("a\n", ("a\nbot2\n", []))
    assert memory.merge_write(memory.shared_path, "a\n", "a\nbot2\n", "j2", stale) == ("a\nbot\ngui\nbot2\n", None)


def test_merge_over_the_time_budget_keeps_the_file_and_the_job_version(tmp_path, monkeypatch):
    import time

    from botoffice import memory as memory_module

    def slow(*args):
        time.sleep(1)
        return "lost\n", []

    monkeypatch.setattr(memory_module, "merge3", slow)
    monkeypatch.setattr(memory_module, "MERGE_SYNC_SECONDS", 0.05)
    memory = Memory(tmp_path)
    memory.write_shared("a\ngui\n")
    started = time.monotonic()
    # nothing prepared and a stale preparation both take the bounded synchronous path
    for prepared in (None, ("a\n", ("a\nbot\n", []))):
        merged, conflict = memory.merge_write(memory.shared_path, "a\n", "b\na\nbot\n", "j", prepared,
                                              who="Ada")
        assert conflict is not None and conflict.read_text() == "b\na\nbot\n"
        assert merged.startswith("a\ngui\n\n## Unmerged edit from Ada")
    assert time.monotonic() - started < 0.9
    assert memory_module.merge_bounded("a\n", "b\n", "c\n", 0.05) is None  # prepare path too


def test_unmerged_memory_edit_is_noted_in_chat(settings, monkeypatch):
    from botoffice import memory as memory_module

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.memory.write_bot(bot.id, "old\n")
    _, job = scheduler.turns.post_message(bot, "hi")
    monkeypatch.setattr(memory_module, "too_large", lambda *texts: True)

    class Writes(FakeAdapter):
        async def run(self, job, ws, log_path, timeout):
            scheduler.memory.write_bot(bot.id, "old\nfrom the GUI\n")
            (ws / "memory.md").write_text("old\nfrom the bot\n")
            return await super().run(job, ws, log_path, timeout)

    scheduler.providers["codex"] = Writes()
    asyncio.run(scheduler._execute(0, store.claim(("desktop-1",), {"codex"})))
    saved = scheduler.memory.read_bot(bot.id)
    assert saved.startswith("old\nfrom the GUI\n\n## Unmerged edit from Ada (")
    [conflict] = _unmerged(scheduler.memory, scheduler.memory.bot_path(bot.id))
    assert conflict.read_text() == "old\nfrom the bot\n"
    notes = [m.text for m in store.list_messages(bot.id) if m.role == "system"]
    assert any("memory edit could not be merged" in text and conflict.name in text for text in notes)


def test_memory_merge_runs_off_the_event_loop(settings, monkeypatch):
    import threading as threads

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "hi")
    seen = []
    original = scheduler.memory.prepare_merge

    def spy(*args):
        seen.append(threads.current_thread() is threads.main_thread())
        return original(*args)

    monkeypatch.setattr(scheduler.memory, "prepare_merge", spy)

    async def run():
        claimed = store.claim(("desktop-1",), {"codex"})
        workspace = settings.data_dir / "workspaces" / str(job.id)

        class Writes(FakeAdapter):
            async def run(self, job, ws, log_path, timeout):
                (ws / "memory.md").write_text("remember this\n")
                return await super().run(job, ws, log_path, timeout)

        scheduler.providers["codex"] = Writes()
        await scheduler._execute(0, claimed)
        return workspace

    asyncio.run(run())
    assert seen == [False]
    assert scheduler.memory.read_bot(bot.id) == "remember this\n"


def test_symlinked_memory_files_are_ignored(settings, tmp_path):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    scheduler.memory.write_shared("shared\n")
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "hi")
    workspace = settings.data_dir / "workspaces" / str(job.id)
    workspace.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("private key material\n")
    (workspace / "shared-memory.md").symlink_to(secret)
    (workspace / "memory.md").symlink_to(secret)
    scheduler._snapshots[str(job.id)] = ("", "shared\n")
    scheduler._write_memory(job, workspace)
    assert scheduler.memory.read_shared() == "shared\n"
    assert scheduler.memory.read_bot(bot.id) == ""


def test_settings_notice_survives_a_turn_that_failed_before_the_bot_read_it(settings):
    from botoffice.providers.base import RunResult

    class Outcomes(FakeAdapter):
        def __init__(self, results):
            super().__init__()
            self.results = results
            self.prompts = []

        async def run(self, job, workspace, log_path, timeout):
            self.prompts.append((workspace / "prompt-resume.txt").read_text())
            log_path.write_text("log")
            return self.results.pop(0)

    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    ok = RunResult("succeeded", exit_code=0, final_text="ok")
    launch = RunResult("failed", error="could not launch", failure_kind="launch")
    adapter = Outcomes([ok, launch, ok])
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    for text, change in (("one", None), ("two", {"model": "gpt-5.5"}), ("three", None)):
        if change:
            bot = store.update_bot(bot.id, **change)
        scheduler.turns.post_message(bot, text)
        claimed = store.claim(("desktop-1",), {"codex"})
        asyncio.run(scheduler._execute(0, claimed))
    assert "Your settings changed" not in adapter.prompts[0]
    assert "Your settings changed since your last turn: model" in adapter.prompts[1]
    assert "Your settings changed since your last turn: model" in adapter.prompts[2]  # not consumed by the failure
