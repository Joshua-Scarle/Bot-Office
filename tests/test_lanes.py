"""0.9.0: Joshua-first claim order; run lanes are separate from leased desktops (A6)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from botoffice import desk
from botoffice.api import create_app
from botoffice.config import Settings
from botoffice.domain import JobStatus
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}
DESKTOPS = ("desktop-1", "desktop-2")


def make(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    return store, Scheduler(store, settings, {"codex": FakeAdapter()})


def test_user_turn_is_claimed_before_older_routine_and_bot_to_bot_jobs(settings):
    store, scheduler = make(settings)
    bots = [store.create_bot(name, "", "codex", None) for name in ("A", "B", "C", "D")]
    scheduler.turns.post_message(bots[0], "routine work", source="routine")
    scheduler.turns.post_message(bots[1], "from a bot", source="inbox")
    store.create("api job")
    scheduler.turns.post_message(bots[3], "Joshua asks")
    order = [store.claim(DESKTOPS, {"codex"}).prompt for _ in range(4)]
    assert order == ["api job", "Joshua asks", "from a bot", "routine work"]


def test_claim_leases_free_desktops_then_runs_without_one(settings):
    store, _ = make(settings)
    jobs = [store.create(f"job {n}") for n in range(3)]
    claimed = [store.claim(DESKTOPS, {"codex"}) for _ in jobs]
    assert [job.slot for job in claimed] == ["desktop-1", "desktop-2", None]
    store.finish(claimed[0].id, JobStatus.SUCCEEDED)
    store.create("next")
    assert store.claim(DESKTOPS, {"codex"}).slot == "desktop-1"


def test_job_without_desktop_gets_no_desktop_note_and_no_slot_env(settings):
    settings = Settings(data_dir=settings.data_dir, token="test-token", job_timeout=2, desktops=("desktop-1",))
    store, scheduler = make(settings)
    bots = [store.create_bot(name, "", "codex", None) for name in ("A", "B")]
    for bot in bots:
        scheduler.turns.post_message(bot, "hi")
    first, second = store.claim(settings.desktops, {"codex"}), store.claim(settings.desktops, {"codex"})
    assert first.slot == "desktop-1" and second.slot is None
    asyncio.run(scheduler._execute(0, second))
    note = (settings.data_dir / "workspaces" / str(second.id) / "DESKTOP.md").read_text()
    assert "No desktop is free this turn" in note
    assert store.get(second.id).status == JobStatus.SUCCEEDED


def test_settings_validate_lanes_and_desktops(monkeypatch):
    monkeypatch.setenv("BOT_OFFICE_RUN_LANES", "4")
    monkeypatch.setenv("BOT_OFFICE_DESKTOPS", "desktop-1, desktop-3")
    settings = Settings.from_env()
    assert settings.run_lanes == 4 and settings.desktops == ("desktop-1", "desktop-3")
    for bad in ({"run_lanes": 0}, {"desktops": ("vnc-1",)}, {"desktops": ("desktop-1", "desktop-1")}, {"desktops": ()}):
        with pytest.raises(ValueError):
            Settings(**bad)


def test_desk_without_a_leased_desktop_says_so():
    with pytest.raises(desk.DeskError, match="No desktop is free this turn; desk is unavailable"):
        desk.resolve_slot(None, {"BOT_OFFICE_JOB_ID": "x"})
    assert desk.resolve_slot(None, {"BOT_OFFICE_SLOT": "desktop-3"}) == "desktop-3"  # leased extra desktop
    assert desk.resolve_slot("desktop-3", {"BOT_OFFICE_DESKTOPS": "desktop-1,desktop-3"}) == "desktop-3"
    with pytest.raises(desk.DeskError):
        desk.resolve_slot("desktop-4", {"BOT_OFFICE_DESKTOPS": "desktop-1,desktop-3"})


def test_slots_endpoint_lists_configured_desktops(settings):
    settings = Settings(data_dir=settings.data_dir, token="test-token", job_timeout=2,
                        desktops=("desktop-1", "desktop-2", "desktop-3"))
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        slots = client.get("/v1/slots", headers=H).json()
        assert [slot["slot"] for slot in slots] == ["desktop-1", "desktop-2", "desktop-3"]
        assert client.post("/v1/slots/desktop-9/exec", json={"command": ["true"]}, headers=H).status_code == 404


def test_upgrade_backfills_priority_of_queued_jobs_from_their_source(settings):
    from contextlib import closing

    store, scheduler = make(settings)
    bots = [store.create_bot(name, "", "codex", None) for name in ("A", "B", "C")]
    scheduler.turns.post_message(bots[0], "routine work", source="routine")
    scheduler.turns.post_message(bots[1], "from a bot", source="inbox")
    scheduler.turns.post_message(bots[2], "Joshua asks")
    # a database from before 0.9.0: no priority column
    with closing(store._connect()) as db:
        db.execute("DROP INDEX jobs_claim")
        db.execute("ALTER TABLE jobs DROP COLUMN priority")
        db.commit()
    upgraded = JobStore(settings.data_dir / "agentdesk.sqlite3")
    order = [upgraded.claim(DESKTOPS, {"codex"}).prompt for _ in range(3)]
    assert order == ["Joshua asks", "from a bot", "routine work"]


def test_job_scoped_desk_slot_must_match_the_lease():
    job = {"BOT_OFFICE_JOB_ID": "j", "BOT_OFFICE_SLOT": "desktop-1"}
    assert desk.resolve_slot(None, job) == "desktop-1"
    assert desk.resolve_slot("desktop-1", job) == "desktop-1"
    with pytest.raises(desk.DeskError, match="leased desktop-1"):
        desk.resolve_slot("desktop-2", job)
    with pytest.raises(desk.DeskError, match="No desktop is free"):
        desk.resolve_slot("desktop-2", {"BOT_OFFICE_JOB_ID": "j"})  # no lease: no explicit pick either
    # outside a job (the operator at a shell) --slot still chooses freely
    assert desk.resolve_slot("desktop-2", {}) == "desktop-2"
