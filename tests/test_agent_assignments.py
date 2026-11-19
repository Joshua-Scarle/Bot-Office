"""0.10.0 agent-scoped assignment endpoints and bin/bot-task (B-1)."""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.domain import Job
from botoffice.providers.fake import FakeAdapter
from botoffice.workspace import assignment_brief

H = {"Authorization": "Bearer test-token"}


def running_token(store, bot, assignment_id=None):
    """A running job of *bot* (optionally a turn of *assignment_id*) and its agent token."""
    if assignment_id:
        with store._connect() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE bot_id=? AND status='queued'", (str(bot.id),))
        Assignments(store).start(assignment_id)
    else:
        store.create("work", "codex", bot_id=bot.id, source="chat")
    job = store.claim(["desktop-1"], {"codex"})
    assert job and job.bot_id == bot.id
    return job, {"Authorization": "Bearer " + store.issue_agent_token(job)}


def setup(settings):
    client = TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))
    client.__enter__()
    store = client.app.state.store
    bots = [store.create_bot(name, "", "codex", None) for name in ("Ada", "Bea", "Cy")]
    return client, store, bots


def test_master_token_and_ended_jobs_are_refused(settings):
    client, store, (ada, *_) = setup(settings)
    assert client.get("/v1/agent/assignments", headers=H).status_code == 401
    job, token = running_token(store, ada)
    assert client.get("/v1/agent/assignments", headers=token).status_code == 200
    store.revoke_agent_token(job.id)
    assert client.get("/v1/agent/assignments", headers=token).status_code == 401
    client.__exit__(None, None, None)


def test_bot_creates_its_own_and_child_assignments_within_limits(settings):
    client, store, (ada, bea, cy) = setup(settings)
    _, token = running_token(store, ada)
    own = client.post("/v1/agent/assignments", headers=token, json={"title": "mine", "start": False}).json()
    assert own["bot_id"] == str(ada.id) and own["created_by"] == f"bot:{ada.id}"
    # 0.11.4: any bot's list without a parent too, but it lands open (nothing starts it); a parent must be its own
    other = client.post("/v1/agent/assignments", headers=token, json={"title": "x", "for_bot": "Bea"})
    assert other.status_code == 201 and other.json()["status"] == "open" and other.json()["bot_id"] == str(bea.id)
    theirs = Assignments(store).create(bea.id, {"title": "Bea's"})
    response = client.post("/v1/agent/assignments", headers=token,
                           json={"title": "x", "for_bot": "Cy", "parent_id": str(theirs.id)})
    assert response.status_code == 403
    children = [client.post("/v1/agent/assignments", headers=token,
                            json={"title": f"c{i}", "for_bot": "Bea", "parent_id": own["id"], "start": False,
                                  "budget_turns": 200}) for i in range(4)]
    assert [r.status_code for r in children] == [201, 201, 201, 429]
    assert children[0].json()["budget_turns"] == 20  # never above the defaults
    # depth: root -> child -> grandchild is fine, one more level is not
    child = children[0].json()
    _, bea_token = running_token(store, bea)
    grandchild = client.post("/v1/agent/assignments", headers=bea_token,
                             json={"title": "g", "for_bot": "Cy", "parent_id": child["id"], "start": False})
    assert grandchild.status_code == 201
    _, cy_token = running_token(store, cy)
    too_deep = client.post("/v1/agent/assignments", headers=cy_token,
                           json={"title": "d", "parent_id": grandchild.json()["id"], "start": False})
    assert too_deep.status_code == 409
    client.__exit__(None, None, None)


def test_only_the_assignee_changes_status_or_checkpoint(settings):
    client, store, (ada, bea, cy) = setup(settings)
    book = Assignments(store)
    parent = book.create(ada.id, {"title": "parent"})
    child = book.create(bea.id, {"title": "child"}, created_by=f"bot:{ada.id}", parent_id=parent.id)
    _, ada_token = running_token(store, ada)
    # the parent's bot reads the child and leaves a note, but cannot change it
    assert client.get(f"/v1/agent/assignments/{child.id}", headers=ada_token).json()["title"] == "child"
    assert client.post(f"/v1/agent/assignments/{child.id}/note", headers=ada_token, json={"text": "hurry"}).status_code == 204
    assert client.post(f"/v1/agent/assignments/{child.id}/status", headers=ada_token,
                       json={"state": "done"}).status_code == 403
    assert client.post(f"/v1/agent/assignments/{child.id}/checkpoint", headers=ada_token,
                       json={"text": "x"}).status_code == 403
    # 0.11.4: an unrelated bot reads it and adds notes too, but cannot change it
    _, cy_token = running_token(store, cy)
    assert client.get(f"/v1/agent/assignments/{child.id}", headers=cy_token).status_code == 200
    assert client.post(f"/v1/agent/assignments/{child.id}/note", headers=cy_token, json={"text": "x"}).status_code == 204
    assert client.post(f"/v1/agent/assignments/{child.id}/status", headers=cy_token,
                       json={"state": "done"}).status_code == 403
    # the assignee may
    _, bea_token = running_token(store, bea)
    assert client.post(f"/v1/agent/assignments/{child.id}/status", headers=bea_token,
                       json={"state": "waiting_on_bot", "note": "need Ada"}).json()["status"] == "waiting_on_bot"
    assert client.post(f"/v1/agent/assignments/{child.id}/checkpoint", headers=bea_token,
                       json={"text": "x" * 5000}).status_code == 413
    saved = client.post(f"/v1/agent/assignments/{child.id}/checkpoint", headers=bea_token, json={"text": "step 2"}).json()
    assert saved["checkpoint"] == "step 2"
    assert "Ada: hurry" in [e["note"] for e in book.events(child.id)]
    client.__exit__(None, None, None)


def test_report_during_its_own_turn_waits_for_the_loop(settings):
    client, store, (ada, *_) = setup(settings)
    book = Assignments(store)
    item = book.create(ada.id, {"title": "t"})
    job, token = running_token(store, ada, item.id)
    assert job.assignment_id == item.id
    body = client.post("/v1/agent/assignments/current/status", headers=token, json={"state": "blocked", "note": "no key"}).json()
    # kept for the loop to apply when the turn ends; the assignment stays active meanwhile
    assert body["status"] == "active"
    with store._connect() as db:
        row = db.execute("SELECT reported_status, reported_job FROM assignments WHERE id=?", (str(item.id),)).fetchone()
    assert tuple(row) == ("blocked", str(job.id))
    assert client.post("/v1/agent/assignments/current/status", headers=token, json={"state": "bogus"}).status_code == 400
    client.__exit__(None, None, None)


def test_turn_prompt_carries_the_assignment_brief(settings):
    client, store, (ada, *_) = setup(settings)
    book = Assignments(store)
    item = book.create(ada.id, {"title": "Report", "goal": "Write the Q3 report", "done_when": "report.md exists",
                                "budget_turns": 4, "escalate": "never"})
    job, _ = running_token(store, ada, item.id)
    book.save_checkpoint(item.id, ada.id, job.id, "outline done")
    brief = assignment_brief(store, job)
    for part in ("Write the Q3 report", "report.md exists", "outline done", "Budget left after this turn: 3 turns",
                 "Do not ask the user", "BOT-OFFICE-STATUS: done|continue", f"id {item.id}"):
        assert part in brief
    assert assignment_brief(store, Job(id=job.id, prompt="x", status="queued", created_at=job.created_at)) == ""
    client.__exit__(None, None, None)


def test_assignment_id_reaches_the_turn_environment(tmp_path):
    from botoffice.providers.codex import CodexAdapter

    job = Job(id="00000000-0000-0000-0000-000000000001", prompt="x", status="running", created_at="2026-11-14T00:00:00Z",
              bot_id="00000000-0000-0000-0000-000000000002", assignment_id="00000000-0000-0000-0000-000000000003")
    env = CodexAdapter()._env(tmp_path / "workspaces" / "j", job)
    assert env["BOT_OFFICE_ASSIGNMENT_ID"] == "00000000-0000-0000-0000-000000000003"


def test_bot_task_cli_uses_only_the_job_token(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers["Authorization"],
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"id": "a1", "status": "blocked", "title": "T", "turns_used": 1,
                                         "budget_turns": 20, "minutes_used": 2, "budget_minutes": 120}).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    helper = Path(__file__).resolve().parents[1] / "bin" / "bot-task"
    env = {"BOT_OFFICE_API_URL": f"http://127.0.0.1:{server.server_port}", "BOT_OFFICE_AGENT_TOKEN": "scoped",
           "BOT_OFFICE_TOKEN": "master-must-not-be-used"}
    try:
        result = subprocess.run([sys.executable, "-I", str(helper), "status", "current", "blocked", "--note", "no key"],
                                env=env, capture_output=True, text=True, timeout=5, check=True)
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert received == [("/v1/agent/assignments/current/status", "Bearer scoped", {"state": "blocked", "note": "no key"})]
    assert "[blocked]" in result.stdout
    usage = subprocess.run([sys.executable, "-I", str(helper), "help"], capture_output=True, text=True, check=True)
    assert "bot-task status" in usage.stdout


def test_task_cli_against_task_api(settings):
    client, store, (ada, bea, _) = setup(settings)
    _, token = running_token(store, ada)
    class Handler(BaseHTTPRequestHandler):
        def forward(self):
            assert self.path.startswith('/v1/agent/')
            size = int(self.headers.get('Content-Length', '0'))
            response = client.request(self.command, self.path, content=self.rfile.read(size),
                                      headers={'Authorization': self.headers['Authorization'], 'Content-Type': 'application/json'})
            self.send_response(response.status_code)
            self.end_headers()
            self.wfile.write(response.content)
        do_GET = do_POST = do_PATCH = forward
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    helper = Path(__file__).resolve().parents[1] / 'bin' / 'bot-task'
    env = {'BOT_OFFICE_API_URL': f'http://127.0.0.1:{server.server_port}',
           'BOT_OFFICE_AGENT_TOKEN': token['Authorization'].split(' ', 1)[1]}
    def run(*args):
        return subprocess.run([sys.executable, '-I', str(helper), *args], env=env, capture_output=True, text=True, timeout=10)
    try:
        assert run('add', 'First').returncode == 0
        tasks = client.get('/v1/agent/tasks', headers=token).json()
        task = tasks[0]
        ref = task['id'][:8]
        assert 'First' in run('tasks').stdout
        assert run('edit', ref, '--version', '1', '--title', 'Revised').returncode == 0
        conflict = run('edit', ref, '--version', '1', '--title', 'Stale')
        assert conflict.returncode == 3 and 'Revised' in conflict.stderr and 'edited' in conflict.stderr
        other = Assignments(store).create(bea.id, {'title': 'Other'})
        assert run('edit', str(other.id)[:8], '--version', '1', '--title', 'No').returncode == 4
        # out of the task's own turn a bot does not start it: the owner does (409, said on stderr)
        refused = run('start', ref)
        assert refused.returncode != 0 and "task's own turn" in refused.stderr
        assert run('drop', ref, '--note', 'obsolete').returncode == 0
        changes = run('changes', '--since', '0')
        assert changes.returncode == 0 and 'Last event:' in changes.stdout
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        client.__exit__(None, None, None)
