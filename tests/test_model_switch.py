"""0.9.3: the first turn after a model/provider switch already has the conversation's context."""
import asyncio
import json
from itertools import count

import pytest

from botoffice import attachments
from botoffice.domain import Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import CursorAdapter
from botoffice.providers.opencode import OpenCodeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
CLASSES = {"claude": ClaudeAdapter, "codex": CodexAdapter, "cursor": CursorAdapter, "antigravity": AntigravityAdapter,
           "opencode": OpenCodeAdapter}
RESUME_FLAG = {"claude": "--resume", "cursor": "--resume", "antigravity": "--conversation", "opencode": "-s"}


class Rig:
    def __init__(self, settings):
        settings.ensure_dirs()
        self.settings = settings
        self.store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        self.runs: list[dict] = []
        self.ids = count(1)
        adapters = {}
        for name, cls in CLASSES.items():
            adapters[name] = adapter = cls(executable="/bin/true")
            self._wire(adapter, name)
        self.scheduler = Scheduler(self.store, settings, adapters)
        self.bot = self.store.create_bot("Ada", "", "claude", "testing", model="claude-sonnet-5-5")
        self.dir = settings.data_dir / "bots" / str(self.bot.id)

    def _wire(self, adapter, name):
        async def login_status():
            return "ok"

        async def fake_exec(args, workspace, timeout, job, parser=None):
            session = next((args[args.index(RESUME_FLAG[name]) + 1] for _ in [0]
                            if name in RESUME_FLAG and RESUME_FLAG[name] in args), None)
            if name == "codex" and args[:2] == ["exec", "resume"]:
                session = args[-2]
            self.runs.append({"engine": name, "args": list(args), "prompt": job.prompt, "resumed": session,
                              "working": (workspace / "WORKING.md").read_text()})
            parser.session_id = session or f"ses_{name}{next(self.ids):04d}"
            parser.final_text = "ok"
            (workspace / "WORKING.md").write_text("OPEN-TASK: ship the report")
            return 0, b"", b""

        adapter.login_status = login_status
        adapter._exec = fake_exec

    def switch(self, engine=None, model=None, **fields):
        self.bot = self.store.update_bot(self.bot.id, default_provider=engine or self.bot.default_provider,
                                         model=model or self.bot.model, **fields)

    def turn(self, text, **kw):
        async def go():
            self.scheduler.turns.post_message(self.bot, text, **kw)
            claimed = self.store.claim(Slot.DESKTOP_1, set(CLASSES))
            await self.scheduler._execute(Slot.DESKTOP_1, claimed)
        asyncio.run(go())
        return self.runs[-1]

    def pointer(self, engine):
        path = self.dir / f"{engine}-session"
        return path.read_text() if path.exists() else None

    def archived(self, engine):
        return sorted(p.name for p in self.dir.glob(f"{engine}-session.handoff-*"))


@pytest.fixture
def rig(settings):
    r = Rig(settings)
    r.turn("first question ALPHA-CONTEXT")
    return r


def test_same_model_still_resumes_short(rig):
    pointer = rig.pointer("claude")
    run = rig.turn("second")
    assert run["resumed"] == pointer and "New turn" in run["prompt"] and "Most recent messages" not in run["prompt"]
    assert rig.pointer("claude") == pointer and not rig.archived("claude")


def test_same_provider_model_change_resumes_in_session(rig):
    pointer = rig.pointer("claude")
    rig.switch(model="claude-opus-5-5")
    run = rig.turn("second")
    assert run["resumed"] == pointer and run["args"][run["args"].index("--model") + 1] == "claude-opus-5-5"
    assert "Most recent messages" not in run["prompt"] and not rig.archived("claude")
    assert rig.pointer("claude") == pointer
    # the settings note (model changed) rides on the resume prompt
    assert "model" in run["prompt"] and "re-read SELF.md" in run["prompt"]


def test_same_provider_model_change_without_in_session_support_hands_off(rig):
    rig.switch(engine="cursor", model="auto")
    rig.turn("on cursor CURSOR-ONE")
    old = rig.pointer("cursor")
    rig.switch(model="gpt-5")
    run = rig.turn("after the model switch")
    assert run["resumed"] is None and old not in run["args"]
    assert len(rig.archived("cursor")) == 1 and rig.pointer("cursor") != old
    assert "fresh session" in run["prompt"] and "OPEN-TASK: ship the report" in run["prompt"]
    assert "ALPHA-CONTEXT" in run["prompt"] and "CURSOR-ONE" in run["prompt"]
    assert "gpt-5" in run["prompt"] and "auto" in run["prompt"]
    later = rig.turn("and again")
    assert later["resumed"] == rig.pointer("cursor") and "New turn" in later["prompt"]


def test_cross_provider_no_prior_session_gets_handoff_then_resumes(rig):
    rig.switch(engine="codex", model="gpt-5.5")
    run = rig.turn("now on codex")
    assert run["resumed"] is None and run["args"][:2] == ["exec", "-m"]
    assert "ALPHA-CONTEXT" in run["prompt"] and "OPEN-TASK: ship the report" in run["prompt"]
    assert "was Claude" in run["prompt"] and "gpt-5.5" in run["prompt"]
    assert "memory.md" in run["prompt"] and "Most recent messages" in run["prompt"]
    later = rig.turn("follow-up")
    assert later["resumed"] == rig.pointer("codex") and later["args"][:2] == ["exec", "resume"]
    assert "New turn" in later["prompt"] and "fresh session" not in later["prompt"]


def test_cross_provider_stale_session_is_archived_not_resumed(rig):
    rig.switch(engine="codex", model="gpt-5.5")
    rig.turn("old codex era CODEX-OLD")
    stale = rig.pointer("codex")
    rig.switch(engine="claude", model="claude-sonnet-5-5")
    rig.turn("claude era CLAUDE-LATER")
    rig.switch(engine="codex", model="gpt-5.5")
    run = rig.turn("back on codex")
    assert run["resumed"] is None and stale not in run["args"]
    assert (rig.dir / "codex-session").read_text() != stale
    assert len(rig.archived("codex")) == 1
    assert "CLAUDE-LATER" in run["prompt"] and "ALPHA-CONTEXT" in run["prompt"] and "fresh session" in run["prompt"]


def test_switch_back_and_forth_without_a_job_keeps_the_session(rig):
    pointer = rig.pointer("claude")
    rig.switch(engine="codex", model="gpt-5.5")
    rig.switch(engine="claude", model="claude-sonnet-5-5")
    run = rig.turn("still on claude")
    assert run["resumed"] == pointer and not rig.archived("claude") and "New turn" in run["prompt"]


def test_instructions_only_change_resumes_with_settings_note(rig):
    pointer = rig.pointer("claude")
    rig.switch(instructions="be brief")
    run = rig.turn("next")
    assert run["resumed"] == pointer and "instructions" in run["prompt"] and "fresh session" not in run["prompt"]
    assert not rig.archived("claude")


def test_first_prompt_after_switch_keeps_image_paths(settings):
    r = Rig(settings)
    name = attachments.save(settings.data_dir, r.bot.id, PNG, "image/png")
    r.turn("look at this photo", attachments=[name])
    r.switch(engine="codex", model="gpt-5.5")
    run = r.turn("what was in the photo?")
    stable = (settings.data_dir / "bots" / str(r.bot.id) / "attachments" / name).resolve()
    assert str(stable) in run["prompt"] and "INDEX.md" in run["prompt"] and "fresh session" in run["prompt"]


def test_edit_after_a_switch_still_resets_and_seeds(rig):
    rig.switch(engine="codex", model="gpt-5.5")
    rig.turn("codex turn")
    message, _ = rig.scheduler.turns.post_message(rig.bot, "to be edited")
    claimed = rig.store.claim(Slot.DESKTOP_1, set(CLASSES))
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, claimed))
    rig.scheduler.turns.edit_message(rig.bot, message.id, "edited text")
    claimed = rig.store.claim(Slot.DESKTOP_1, set(CLASSES))
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, claimed))
    run = rig.runs[-1]
    assert run["resumed"] is None and "rewound" in run["prompt"] and "edited text" in run["prompt"]


@pytest.mark.parametrize("engine,first_model,next_model", [
    ("codex", "gpt-5.5", "gpt-5.6"), ("cursor", "auto", "gpt-5"),
])
def test_restricted_model_change_hands_off(rig, engine, first_model, next_model):
    rig.switch(engine=engine, model=first_model)
    rig.turn("prior provider turn")
    old = rig.pointer(engine)
    rig.switch(model=next_model, cli_access="restricted")
    run = rig.turn("restricted model turn")
    assert run["resumed"] is None and old not in run["args"]
    assert len(rig.archived(engine)) == 1 and rig.pointer(engine) is None
    assert "fresh session" in run["prompt"] and "prior provider turn" in run["prompt"]
    assert "engine/model changed" in run["prompt"]


def test_restricted_turn_is_included_when_full_access_returns(rig):
    rig.switch(engine="codex", model="gpt-5.5")
    rig.turn("full access")
    old = rig.pointer("codex")
    rig.switch(cli_access="restricted")
    restricted = rig.turn("RESTRICTED-CONTEXT")
    assert restricted["resumed"] is None and "fresh session" in restricted["prompt"]
    assert "engine/model changed" not in restricted["prompt"]
    assert len(rig.archived("codex")) == 1 and rig.pointer("codex") is None
    rig.switch(cli_access="full")
    run = rig.turn("back to full")
    assert run["resumed"] is None and old not in run["args"]
    assert "RESTRICTED-CONTEXT" in run["prompt"] and "fresh session" in run["prompt"]
    assert "engine/model changed" not in run["prompt"]
    later = rig.turn("continue")
    assert later["resumed"] == rig.pointer("codex")


def test_archive_failure_does_not_launch_provider(rig, monkeypatch):
    rig.switch(engine="codex", model="gpt-5.5")
    rig.turn("first codex")
    rig.switch(engine="claude", model="claude-sonnet-5-5")
    rig.turn("back on claude")
    rig.switch(engine="codex", model="gpt-5.5")
    def fail_archive(*args, **kwargs):
        raise OSError("rename refused")
    monkeypatch.setattr("botoffice.scheduler.archive_session", fail_archive)
    rig.scheduler.turns.post_message(rig.bot, "must fail")
    claimed = rig.store.claim(Slot.DESKTOP_1, set(CLASSES))
    count_before = len(rig.runs)
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, claimed))
    result = rig.store.get(claimed.id)
    assert result.status == "failed" and "Could not archive" in result.error
    assert len(rig.runs) == count_before and rig.pointer("codex") is not None


def test_legacy_snapshot_does_not_resume_even_when_hashes_match(rig):
    path = rig.dir / "self.sha256"
    snapshot = json.loads(path.read_text())
    del snapshot["last"]
    path.write_text(json.dumps(snapshot))
    old = rig.pointer("claude")
    run = rig.turn("after upgrade")
    assert run["resumed"] is None and old not in run["args"]
    assert "fresh session" in run["prompt"] and "ALPHA-CONTEXT" in run["prompt"]
    assert "engine/model changed" not in run["prompt"]
    assert "last" in json.loads(path.read_text())
    assert rig.turn("next")["resumed"] == rig.pointer("claude")


def test_failed_snapshot_keeps_handoff_pending_when_switching_back(rig, monkeypatch):
    import botoffice.scheduler as scheduler_module

    old = rig.pointer("claude")
    save = scheduler_module.save_settings_snapshot
    def fail_snapshot(*args, **kwargs):
        raise OSError("snapshot write failed")
    monkeypatch.setattr(scheduler_module, "save_settings_snapshot", fail_snapshot)
    rig.switch(engine="codex", model="gpt-5.5")
    rig.turn("INTERVENING-CODEX-TURN")
    assert (rig.dir / ".handoff-pending").exists()
    monkeypatch.setattr(scheduler_module, "save_settings_snapshot", save)
    rig.switch(engine="claude", model="claude-sonnet-5-5")
    run = rig.turn("back after snapshot failure")
    assert run["resumed"] is None and old not in run["args"]
    assert "INTERVENING-CODEX-TURN" in run["prompt"] and "fresh session" in run["prompt"]
    assert not (rig.dir / ".handoff-pending").exists()


@pytest.mark.parametrize("content", [None, "{broken", "[]", '{"last": {}}'])
def test_missing_or_corrupt_snapshot_does_not_resume_pointer(rig, content):
    path = rig.dir / "self.sha256"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    old = rig.pointer("claude")
    run = rig.turn("after losing snapshot")
    assert run["resumed"] is None and old not in run["args"]
    assert "ALPHA-CONTEXT" in run["prompt"] and "fresh session" in run["prompt"]
    assert len(rig.archived("claude")) == 1


def test_model_change_without_pointer_uses_handoff_tail(rig):
    for i in range(4):
        rig.turn(f"earlier turn {i} LONG-TAIL-{i}")
    (rig.dir / "claude-session").unlink()
    rig.switch(model="claude-opus-5-5")
    run = rig.turn("new model without pointer")
    assert run["resumed"] is None and "fresh session" in run["prompt"]
    assert "LONG-TAIL-0" in run["prompt"] and "was Claude" in run["prompt"]


def test_model_change_failed_resume_falls_back_to_handoff(rig):
    adapter = rig.scheduler.providers["claude"]
    original = adapter._exec
    calls = []
    async def fail_resume_once(args, workspace, timeout, job, parser=None):
        calls.append(job.prompt)
        if len(calls) == 1:
            return 1, b"", b"session not found"
        return await original(args, workspace, timeout, job, parser)
    adapter._exec = fail_resume_once
    rig.switch(model="claude-opus-5-5")
    run = rig.turn("fallback after model switch")
    assert len(calls) == 2 and "New turn" in calls[0]
    assert run["resumed"] is None and "fresh session" in calls[1]
    assert "ALPHA-CONTEXT" in calls[1] and "was Claude" in calls[1]


def test_edit_queued_before_a_switch_seeds_and_names_the_switch(rig):
    message, _ = rig.scheduler.turns.post_message(rig.bot, "to be edited")
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, rig.store.claim(Slot.DESKTOP_1, set(CLASSES))))
    rig.scheduler.turns.edit_message(rig.bot, message.id, "edited text")
    rig.switch(engine="codex", model="gpt-5.5")
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, rig.store.claim(Slot.DESKTOP_1, set(CLASSES))))
    run = rig.runs[-1]
    assert run["engine"] == "claude" and run["resumed"] is None
    assert "rewound" in run["prompt"] and "edited text" in run["prompt"] and "also changed" not in run["prompt"]
    later = rig.turn("now on codex")
    assert later["engine"] == "codex" and later["resumed"] is None and "was Claude" in later["prompt"]


def test_outgrown_pending_seed_after_a_switch_falls_back_to_the_handoff(rig, monkeypatch):
    from botoffice.store import EditError
    done = []
    monkeypatch.setattr(rig.store, "pending_rewind_seed", lambda bot_id: None if done else 7)
    monkeypatch.setattr(rig.store, "complete_rewind_seed", lambda bot_id, generation: done.append(generation))

    def outgrown(bot_id, message_id):
        raise EditError(413, "too long")
    monkeypatch.setattr(rig.store, "rewind_seed", outgrown)
    rig.switch(engine="codex", model="gpt-5.5")
    run = rig.turn("after the switch")
    assert run["engine"] == "codex" and run["resumed"] is None and done == [7]
    assert "was Claude" in run["prompt"] and "ALPHA-CONTEXT" in run["prompt"] and "Most recent messages" in run["prompt"]


def test_outgrown_seed_with_missing_snapshot_still_gets_the_handoff_tail(rig, monkeypatch):
    # the seeded turn archives the old pointer before model_switch looks for it
    from botoffice.store import EditError
    for i in range(4):
        rig.turn(f"earlier turn {i} LONG-TAIL-{i}")
    done = []
    monkeypatch.setattr(rig.store, "pending_rewind_seed", lambda bot_id: None if done else 7)
    monkeypatch.setattr(rig.store, "complete_rewind_seed", lambda bot_id, generation: done.append(generation))

    def outgrown(bot_id, message_id):
        raise EditError(413, "too long")
    monkeypatch.setattr(rig.store, "rewind_seed", outgrown)
    (rig.dir / "self.sha256").unlink()
    assert rig.pointer("claude") and not (rig.dir / ".handoff-pending").exists()
    rig.switch(model="claude-opus-5-5")
    run = rig.turn("after the switch")
    assert run["resumed"] is None and done == [7] and "fresh session" in run["prompt"]
    assert "LONG-TAIL-0" in run["prompt"]



def test_edit_after_a_switch_seeds_and_names_the_switch(rig):
    message, _ = rig.scheduler.turns.post_message(rig.bot, "to be edited")
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, rig.store.claim(Slot.DESKTOP_1, set(CLASSES))))
    rig.switch(engine="codex", model="gpt-5.5")
    rig.scheduler.turns.edit_message(rig.bot, message.id, "edited text")
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, rig.store.claim(Slot.DESKTOP_1, set(CLASSES))))
    run = rig.runs[-1]
    assert run["engine"] == "codex" and run["resumed"] is None
    assert "rewound" in run["prompt"] and "edited text" in run["prompt"] and "ALPHA-CONTEXT" in run["prompt"]
    assert "also changed (was Claude" in run["prompt"]
    later = rig.turn("follow-up")
    assert later["resumed"] == rig.pointer("codex") and "fresh session" not in later["prompt"]


def test_assignment_turn_after_a_switch_gets_the_handoff(rig):
    rig.switch(engine="codex", model="gpt-5.5")
    from botoffice.rules import record_capability  # 0.11.0: unattended turns need a proven gate
    record_capability(rig.scheduler.settings.control_dir, "codex", "enforced", helpers=True)
    item = rig.scheduler.assignments.create(rig.bot.id, {"title": "Report", "goal": "write it", "start": True})
    claimed = rig.store.claim(Slot.DESKTOP_1, set(CLASSES))
    assert claimed.assignment_id == item.id
    asyncio.run(rig.scheduler._execute(Slot.DESKTOP_1, claimed))
    run = rig.runs[-1]
    assert run["engine"] == "codex" and run["resumed"] is None
    assert "was Claude" in run["prompt"] and "ALPHA-CONTEXT" in run["prompt"] and "Report" in run["prompt"]


def test_opencode_never_resumes_every_turn_carries_the_handoff_context(rig):
    """OpenCode 1.19.5 `run -s` hangs: each turn is a fresh session with the compact handoff."""
    rig.switch(engine="opencode", model="openai/gpt-5")
    (rig.dir / "opencode-session").write_text("ses_legacy0001")  # a pointer from before this change
    first = rig.turn("on opencode OC-ONE")
    assert "-s" not in first["args"] and "ALPHA-CONTEXT" in first["prompt"]
    assert not (rig.dir / "opencode-session").exists() and len(rig.archived("opencode")) == 1
    second = rig.turn("again OC-TWO")
    assert second["resumed"] is None and "-s" not in second["args"]
    assert "fresh session" in second["prompt"] and "OPEN-TASK: ship the report" in second["prompt"]
    assert "OC-ONE" in second["prompt"] and "OC-TWO" in second["prompt"]
    assert not (rig.dir / "opencode-session").exists() and len(rig.archived("opencode")) == 1
