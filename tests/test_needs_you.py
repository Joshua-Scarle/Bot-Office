"""0.10.0 Needs-you queue, model-free watchdog, notifications and desktop hand-back (B-4)."""
import asyncio
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.domain import JobStatus
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice import needs_you
from botoffice.store import JobStore, now

H = {"Authorization": "Bearer test-token"}


def client_for(settings):
    return TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))


def set_row(store, table, row_id, **fields):
    marks = ", ".join(f"{key}=?" for key in fields)
    with closing(store._connect()) as db:
        db.execute(f"UPDATE {table} SET {marks} WHERE id=?", (*fields.values(), str(row_id)))


def needs(client):
    return client.get("/v1/needs-you", headers=H).json()


def test_assignment_items_appear_by_state_and_clear_when_resolved(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        book = Assignments(store)
        ada = store.create_bot("Ada", "", "codex", None)
        item = book.create(ada.id, {"title": "Report"})
        assert needs(client) == []
        with closing(book._tx()) as db:
            book._set_status(db, book._row(db, item.id), "waiting_on_user", "which branch?")
            db.commit()
        (entry,) = needs(client)
        assert entry["state"] == "waiting_on_user" and "which branch?" in entry["ask"] and entry["notify"] is True
        assert entry["actions"] == ["reply", "start", "done", "dismiss"]
        # Joshua replies: the assignment is active again and the item is gone
        client.post(f"/v1/assignments/{item.id}/reply", headers=H, json={"text": "main"})
        assert needs(client) == []
        # blocked shows unless escalation is "never"; waiting on a bot only with "always"
        for status, escalate, shown in (("blocked", "on_block", True), ("blocked", "never", False),
                                        ("waiting_on_bot", "on_block", False), ("waiting_on_bot", "always", True)):
            set_row(store, "assignments", item.id, status=status, escalate=escalate)
            assert bool(needs(client)) is shown, (status, escalate)
        # a dismissed item stays hidden until the state changes
        set_row(store, "assignments", item.id, status="blocked", escalate="on_block")
        first = needs(client)[0]["id"]
        assert client.post("/v1/needs-you/dismiss", headers=H, json={"assignment_id": str(item.id)}).status_code == 200
        assert needs(client) == []
        with closing(book._tx()) as db:
            book._set_status(db, book._row(db, item.id), "waiting_on_user", "again")
            db.commit()
        assert needs(client)[0]["id"] != first
        client.patch(f"/v1/assignments/{item.id}", headers=H, json={"status": "done"})
        assert needs(client) == []
        # past due
        late = book.create(ada.id, {"title": "Late", "due_at": now() - timedelta(hours=1)})
        assert [e["state"] for e in needs(client)] == ["overdue"] and needs(client)[0]["assignment_id"] == str(late.id)


def test_failed_unattended_turns_and_repeated_failures(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        bea = store.create_bot("Bea", "", "codex", None)
        store.update_bot(bea.id, notify=0)
        stamp = now()
        ids = []
        for i, source in enumerate(("chat", "routine", "routine")):
            job = store.create("x", "codex", bot_id=bea.id, source=source)
            set_row(store, "jobs", job.id, status="failed", failure_kind="provider_error", error="boom",
                    finished_at=(stamp + timedelta(seconds=i)).isoformat())
            ids.append(str(job.id))
        items = needs(client)
        # Joshua saw his own chat failure; the routine failures and the repeat are listed
        assert {e["job_id"] for e in items if e["kind"] == "job"} == set(ids[1:])
        (repeat,) = [e for e in items if e["kind"] == "repeat"]
        assert repeat["job_id"] == ids[2] and repeat["notify"] is False
        assert client.post(f"/v1/jobs/{ids[2]}/dismiss", headers=H).status_code == 204
        items = needs(client)
        assert [e["job_id"] for e in items] == [ids[1]]
        # a later success clears the rest
        done = store.create("x", "codex", bot_id=bea.id, source="chat")
        set_row(store, "jobs", done.id, status="succeeded", finished_at=(stamp + timedelta(seconds=9)).isoformat())
        assert needs(client) == []
        # auth problems show even for chat turns
        login = store.create("x", "codex", bot_id=bea.id, source="chat")
        set_row(store, "jobs", login.id, status="needs_login", finished_at=(stamp + timedelta(seconds=10)).isoformat())
        assert [e["job_id"] for e in needs(client)] == [str(login.id)]


def test_watchdog_flags_stalls_without_a_model_call(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    adapter = FakeAdapter()
    scheduler = Scheduler(store, settings, {"codex": adapter})
    book = scheduler.assignments
    ada = store.create_bot("Ada", "", "codex", None)
    item = book.create(ada.id, {"title": "Report"})
    old = (now() - timedelta(hours=settings.stall_hours + 1)).isoformat()
    set_row(store, "assignments", item.id, status="active", last_progress_at=old)
    jobs_before = len(store.list())
    # a pending wake-up means it is waiting on purpose
    wakeup = book.add_wakeup(ada.id, None, assignment_id=item.id, due_at=now() + timedelta(hours=1))
    scheduler.watchdog()
    assert book.get(item.id).stalled_at is None
    book.cancel_wakeup(wakeup.id)
    scheduler.watchdog()
    assert book.get(item.id).stalled_at is not None
    scheduler.watchdog()  # flagged once
    assert [e["kind"] for e in book.events(item.id)].count("stalled") == 1
    assert len(store.list()) == jobs_before  # no turn queued, so no model call
    assert any("no progress" in m.text for m in store.list_messages(ada.id) if m.role == "system")
    # progress (a checkpoint, a status change, a turn) clears the flag
    book.start(item.id)
    assert book.get(item.id).stalled_at is None


def test_watchdog_cancels_a_hung_turn_and_blocks_its_assignment(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter(delay=60)})
    ada = store.create_bot("Ada", "", "codex", None)
    item = scheduler.assignments.create(ada.id, {"title": "Report", "start": True})

    async def scenario():
        job = store.claim(["desktop-1"], {"codex"})
        task = asyncio.create_task(scheduler._execute(0, job))
        scheduler._tasks[0], scheduler._job_ids[0] = task, str(job.id)
        await asyncio.sleep(0.2)
        set_row(store, "jobs", job.id, started_at=(now() - timedelta(hours=3)).isoformat())
        scheduler.watchdog()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return store.get(job.id)

    job = asyncio.run(scenario())
    assert job.status == JobStatus.CANCELLED and job.error.startswith("watchdog")
    after = scheduler.assignments.get(item.id)
    assert after.status == "blocked"
    assert not [j for j in store.list() if j.assignment_id == item.id and j.status == JobStatus.QUEUED]


def test_hand_back_continues_the_bot_that_used_the_desktop(settings):
    with client_for(settings) as client:
        store = client.app.state.store
        ada = store.create_bot("Ada", "", "codex", None)
        assert client.post("/v1/desktops/desktop-1/hand-back", headers=H).status_code == 404
        assert client.post("/v1/desktops/nope/hand-back", headers=H).status_code == 404
        item = Assignments(store).create(ada.id, {"title": "Login flow", "start": True})
        job = store.claim(["desktop-1"], {"codex"})
        store.finish(job.id, JobStatus.INCOMPLETE, outcome="waiting_on_user")
        set_row(store, "assignments", item.id, status="waiting_on_user")
        body = client.post("/v1/desktops/desktop-1/hand-back", headers=H).json()
        queued = store.get(body["job_id"])
        assert queued.assignment_id == item.id and "finished the step on desktop-1" in queued.prompt
        assert Assignments(store).get(item.id).status == "active"
        # without an open assignment it is a plain chat message to the bot
        client.patch(f"/v1/assignments/{item.id}", headers=H, json={"status": "done"})
        client.app.state.scheduler.providers["codex"].enabled = True
        plain = store.get(client.post("/v1/desktops/desktop-1/hand-back", headers=H).json()["job_id"])
        assert plain.assignment_id is None and plain.source == "chat"


def test_bot_notify_and_timeout_flow_into_needs_you():
    html = (Path(__file__).resolve().parents[1] / "src/botoffice/static/bot-office.html").read_text()
    for part in ('id="needs-you"', "/v1/needs-you", 'id="notify-enable"', "Notification.requestPermission",
                 'id="f-notify"', 'id="f-timeout"', 'id="desktop-hand-back"', "/hand-back"):
        assert part in html
    # permission is only ever asked from the button's click handler
    assert html.count("requestPermission") == 1
    assert "notificationsAllowed()" in html


def test_incomplete_turns_never_count_and_an_assignment_counts_once(settings):
    """Printer Writing Bot case: dozens of incomplete turns plus assignments waiting for photos."""
    with client_for(settings) as client:
        store = client.app.state.store
        book = Assignments(store)
        ada = store.create_bot("Ada", "", "codex", None)
        photos = book.create(ada.id, {"title": "Worksheet"})
        other = book.create(ada.id, {"title": "Pick a branch"})
        with closing(book._tx()) as db:
            book._set_status(db, book._row(db, photos.id), "waiting_on_user", "need photos to check alignment")
            book._set_status(db, book._row(db, other.id), "waiting_on_user", "which branch should I use?")
            db.commit()
        for index in range(72):
            job = store.create("x", "codex", bot_id=ada.id, source="chat" if index % 2 else "assignment")
            set_row(store, "jobs", job.id, status="incomplete", failure_kind="incomplete", error="Not done: waiting",
                    finished_at=now().isoformat())
        items = needs(client)
        assert sorted(i["kind"] for i in items) == ["assignment", "assignment"]  # 72 incomplete turns: no items
        by_title = {i["title"]: i for i in items}
        assert by_title["Worksheet"]["category"] == "materials"
        assert by_title["Pick a branch"]["category"] == "action"
        assert needs_you.count_items(items) == {"total": 2, "materials": 1}
        # a question on the same assignment is a second item but not a second count
        extra = {"id": "q:1:open", "kind": "question", "state": "open", "ask": "asks: ?", "assignment_id": photos.id}
        again = needs_you._annotate([*items, extra])
        assert needs_you.count_items(again) == {"total": 2, "materials": 0}  # the question is a real ask
