"""0.10.0 self-scheduled wake-ups and bot-owned routines with limits (B-3)."""
import json
import subprocess
import sys
import threading
from contextlib import closing
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.cron import to_cron_time
from botoffice.providers.fake import FakeAdapter
from botoffice.store import now

H = {"Authorization": "Bearer test-token"}


def setup(settings):
    client = TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))
    client.__enter__()
    store = client.app.state.store
    bots = [store.create_bot(name, "", "codex", None) for name in ("Ada", "Bea", "Cy")]
    return client, store, bots


def token_for(store, bot):
    store.create("work", "codex", bot_id=bot.id, source="chat")
    job = store.claim(["desktop-1"], {"codex"})
    assert job.bot_id == bot.id
    return job, {"Authorization": "Bearer " + store.issue_agent_token(job)}


def make_due(store, wakeup_id):
    with closing(store._connect()) as db:
        db.execute("UPDATE wakeups SET due_at=? WHERE id=?", ((now() - timedelta(seconds=1)).isoformat(), str(wakeup_id)))


def wake_jobs(store, bot):
    return [job for job in store.list() if job.bot_id == bot.id and job.source == "wakeup"]


def test_wakeup_limits_are_enforced_server_side(settings):
    client, store, (ada, bea, _) = setup(settings)
    _, token = token_for(store, ada)
    post = lambda body: client.post("/v1/agent/wakeups", headers=token, json=body)
    assert post({"in_minutes": 2, "note": "too soon"}).status_code == 400
    assert post({"due_at": (now() + timedelta(days=31)).isoformat()}).status_code == 400
    assert post({"note": "no time"}).status_code == 400
    assert post({"in_minutes": 60, "note": "x"}).status_code == 201
    # a time without a zone is Joshua's (ET): 10 minutes ahead in ET is fine, 2 minutes is not
    in_et = lambda minutes: to_cron_time(now() + timedelta(minutes=minutes)).replace(tzinfo=None).isoformat()
    assert post({"due_at": in_et(10)}).status_code == 201
    assert post({"due_at": in_et(2)}).status_code == 400
    while len(Assignments(store).wakeups(ada.id)) < 5:
        assert post({"in_minutes": 120}).status_code == 201
    assert post({"in_minutes": 120}).status_code == 429
    # never on another bot's assignment, never waiting on one it cannot read
    theirs = Assignments(store).create(bea.id, {"title": "Bea's"})
    with closing(store._connect()) as db:
        db.execute("UPDATE wakeups SET status='cancelled' WHERE bot_id=?", (str(ada.id),))
    assert post({"assignment_id": str(theirs.id), "in_minutes": 60}).status_code == 403
    assert post({"on_assignment": str(theirs.id)}).status_code == 403
    assert client.post("/v1/agent/wakeups", headers=H, json={"in_minutes": 60}).status_code == 401
    client.__exit__(None, None, None)


def test_a_due_wakeup_fires_once_across_restarts(settings):
    client, store, (ada, *_) = setup(settings)
    book = Assignments(store)
    item = book.create(ada.id, {"title": "Report"})
    job, _ = token_for(store, ada)
    wakeup = book.add_wakeup(ada.id, job.id, assignment_id=item.id, due_at=now() + timedelta(hours=1),
                             note="check the CI run")
    assert book.fire_due() == 0  # not due yet
    make_due(store, wakeup.id)
    assert book.fire_due() == 1
    assert Assignments(store).fire_due() == 0  # a restarted controller (or a second tick) fires nothing
    (fired,) = wake_jobs(store, ada)
    assert fired.assignment_id == item.id and "You scheduled this wake-up at" in fired.prompt
    assert "check the CI run" in fired.prompt
    with closing(store._connect()) as db:
        assert db.execute("SELECT priority FROM jobs WHERE id=?", (str(fired.id),)).fetchone()[0] == 2
    assert book.get(item.id).status == "active"
    assert book.wakeups(ada.id, include_done=True)[0].status == "fired"
    client.__exit__(None, None, None)


def test_unlinked_wakeup_and_the_daily_cap(settings):
    client, store, (ada, *_) = setup(settings)
    book = Assignments(store)
    wakeup = book.add_wakeup(ada.id, None, due_at=now() + timedelta(hours=1), note="ping")
    make_due(store, wakeup.id)
    assert book.fire_due() == 1 and wake_jobs(store, ada)[0].assignment_id is None
    stamp = now().isoformat()
    with closing(store._connect()) as db:
        for i in range(23):
            db.execute("INSERT INTO wakeups (id,bot_id,status,created_at,fired_at) VALUES (?,?,'fired',?,?)",
                       (f"old-{i}", str(ada.id), stamp, stamp))
    over = book.add_wakeup(ada.id, None, due_at=now() + timedelta(hours=1), note="one too many")
    make_due(store, over.id)
    assert book.fire_due() == 0
    assert book._wakeup(over.id).status == "cancelled" and len(wake_jobs(store, ada)) == 1
    assert any("24 wake-ups per day" in m.text for m in store.list_messages(ada.id) if m.role == "system")
    client.__exit__(None, None, None)


def test_on_change_wakeup_fires_when_the_child_changes(settings):
    client, store, (ada, bea, _) = setup(settings)
    book = Assignments(store)
    parent = book.create(ada.id, {"title": "parent"})
    child = book.create(bea.id, {"title": "child", "start": True}, created_by=f"bot:{ada.id}", parent_id=parent.id)
    _, ada_token = token_for(store, ada)
    response = client.post("/v1/agent/wakeups", headers=ada_token,
                           json={"assignment_id": str(parent.id), "on_assignment": str(child.id), "note": "merge it"})
    assert response.status_code == 201
    assert not wake_jobs(store, ada)  # no status change yet
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='cancelled' WHERE bot_id=? AND status='queued'", (str(bea.id),))
    _, bea_token = token_for(store, bea)
    client.post(f"/v1/agent/assignments/{child.id}/status", headers=bea_token, json={"state": "done"})
    (fired,) = wake_jobs(store, ada)
    assert "is now done" in fired.prompt and fired.assignment_id == parent.id
    assert "<<<UNTRUSTED from bot:" in fired.prompt and "\nchild\n" in fired.prompt  # a bot wrote the title
    client.__exit__(None, None, None)


def test_stop_cancels_pending_wakeups_and_master_manages_them(settings):
    client, store, (ada, *_) = setup(settings)
    book = Assignments(store)
    item = book.create(ada.id, {"title": "t"})
    first = book.add_wakeup(ada.id, None, assignment_id=item.id, due_at=now() + timedelta(hours=1))
    book.stop(item.id)
    assert book._wakeup(first.id).status == "cancelled"
    second = book.add_wakeup(ada.id, None, due_at=now() + timedelta(hours=1), note="later")
    listed = client.get(f"/v1/bots/{ada.id}/wakeups", headers=H).json()
    assert [w["id"] for w in listed] == [str(second.id)]
    assert client.delete(f"/v1/bots/{ada.id}/wakeups/{second.id}", headers=H).status_code == 204
    make_due(store, second.id)
    assert book.fire_due() == 0 and client.get(f"/v1/bots/{ada.id}/wakeups", headers=H).json() == []
    client.__exit__(None, None, None)


def test_bot_routines_limits_and_ownership(settings):
    client, store, (ada, bea, _) = setup(settings)
    _, token = token_for(store, ada)
    made = client.post("/v1/agent/routines", headers=token, json={"cron": "0 9 * * 1-5", "prompt": "standup"})
    assert made.status_code == 201 and made.json()["created_by"] == "bot"
    assert client.post("/v1/agent/routines", headers=token, json={"cron": "*/10 * * * *", "prompt": "x"}).status_code == 400
    assert client.post("/v1/agent/routines", headers=token, json={"cron": "0,5 9 * * *", "prompt": "x"}).status_code == 400
    assert client.post("/v1/agent/routines", headers=token, json={"cron": "*/15 * * * *", "prompt": "x"}).status_code == 201
    for i in range(8):
        assert client.post("/v1/agent/routines", headers=token, json={"cron": f"{i} 3 * * *", "prompt": "x"}).status_code == 201
    assert client.post("/v1/agent/routines", headers=token, json={"cron": "0 4 * * *", "prompt": "x"}).status_code == 429
    # Joshua's routines and other bots' routines are off limits
    users = store.create_routine(ada.id, "0 8 * * *", "user's", True)
    beas = store.create_routine(bea.id, "0 8 * * *", "bea's", True, created_by="bot")
    for other in (users, beas):
        assert client.patch(f"/v1/agent/routines/{other.id}", headers=token, json={"enabled": False}).status_code == 403
        assert client.delete(f"/v1/agent/routines/{other.id}", headers=token).status_code == 403
    rid = made.json()["id"]
    assert client.patch(f"/v1/agent/routines/{rid}", headers=token, json={"cron": "* * * * *"}).status_code == 400
    assert client.patch(f"/v1/agent/routines/{rid}", headers=token, json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/v1/agent/routines/{rid}", headers=token).status_code == 204
    listed = client.get(f"/v1/bots/{ada.id}/routines", headers=H).json()
    assert {r["created_by"] for r in listed} == {"user", "bot"}
    client.__exit__(None, None, None)


def test_bot_task_wake_and_routine_commands(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.command, self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            reply = {"id": "w1", "status": "pending", "due_at": "2026-11-14T20:00:00Z", "note": "n", "enabled": True,
                     "created_by": "bot", "cron": "0 9 * * *", "name": None, "prompt": "p"}
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: [server.handle_request() for _ in range(2)])
    thread.start()
    helper = Path(__file__).resolve().parents[1] / "bin" / "bot-task"
    env = {"BOT_OFFICE_API_URL": f"http://127.0.0.1:{server.server_port}", "BOT_OFFICE_AGENT_TOKEN": "scoped"}
    try:
        for argv in (["wake", "-", "--in", "2h", "--note", "check CI"],
                     ["routine", "add", "--cron", "0 9 * * *", "--prompt", "p"]):
            subprocess.run([sys.executable, "-I", str(helper), *argv], env=env, capture_output=True, text=True,
                           timeout=5, check=True)
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert received[0] == ("POST", "/v1/agent/wakeups", {"note": "check CI", "in_minutes": 120})
    assert received[1] == ("POST", "/v1/agent/routines", {"cron": "0 9 * * *", "prompt": "p", "name": None})
    bad = subprocess.run([sys.executable, "-I", str(helper), "wake", "-", "--in", "soon"], env=env,
                         capture_output=True, text=True, timeout=5, check=False)
    assert bad.returncode != 0 and "--in takes" in bad.stderr


def test_gui_lists_wakeups_and_marks_bot_routines():
    html = (Path(__file__).resolve().parents[1] / "src/botoffice/static/bot-office.html").read_text()
    assert 'id="wakeups-list"' in html and "/wakeups/" in html
    assert 'item.created_by === "bot"' in html
