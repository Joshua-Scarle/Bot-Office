"""Persistence groundwork for steering messages."""
import asyncio

import json
import sqlite3
from uuid import uuid4

import pytest

from botoffice.store import EditError, JobStore
from botoffice.workspace import _history_line


def test_old_message_table_migrates_and_steers_cannot_be_edited(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, "
                   "role TEXT NOT NULL, sender TEXT, source TEXT NOT NULL, text TEXT NOT NULL, "
                   "job_id TEXT, created_at TEXT NOT NULL)")
    store = JobStore(path)
    store.initialize()
    bot_id, job_id = uuid4(), uuid4()
    msg = store.add_message(bot_id, "user", "change course")
    with store._connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        assert {"steer_job_id", "steer_status", "steer_delivered_at"} <= columns
        db.execute("UPDATE messages SET steer_job_id=?, steer_status='pending' WHERE id=?", (str(job_id), msg.id))
    msg = store.get_message(msg.id)
    assert msg.steer_job_id == job_id and msg.steer_status == "pending"
    assert "user (steer): change course" in _history_line(msg)
    with pytest.raises(EditError) as error:
        store.check_edit(bot_id, msg.id, "replacement")
    assert error.value.status == 409

import time
from fastapi.testclient import TestClient
from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler

H = {"Authorization": "Bearer test-token"}


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    assert False, "timed out waiting for fake provider"


def fake_script(tmp_path, name, source):
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + source)
    path.chmod(0o755)
    return str(path)


def test_claude_injects_and_acknowledges_before_closing_stdin(settings, tmp_path):
    settings = __import__("dataclasses").replace(settings, job_timeout=8)
    exe = fake_script(tmp_path, "fake_claude", '''
import json, sys
from pathlib import Path
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn":true}'); sys.exit(0)
first = json.loads(sys.stdin.readline())
Path("initial.json").write_text(json.dumps(first))
print(json.dumps({"type":"system", "session_id":"session_123456"}), flush=True)
Path("ready").touch()
steer = json.loads(sys.stdin.readline())
Path("steer.json").write_text(json.dumps(steer))
print(json.dumps(steer), flush=True)
print(json.dumps({"type":"result", "result":"adjusted", "session_id":"session_123456"}), flush=True)
sys.stdin.read()
Path("stdin-closed").touch()
''')
    app = create_app(settings, {"claude": ClaudeAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=H, json={"name":"A", "engine":"claude", "reason":"test"}).json()
        posted = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={"text":"start"}).json()
        jid = posted['job']['id']
        ws = settings.data_dir / "workspaces" / jid
        wait_for(lambda: (ws / "ready").exists())
        from botoffice import attachments
        name = attachments.save(settings.data_dir, bot['id'], b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "image/png")
        steer = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={"text":"change course", "attachments":[name]}).json()
        assert steer['job']['id'] == jid and steer['message']['steer_status'] == 'pending'
        mid = steer['message']['id']
        wait_for(lambda: app.state.store.get_message(mid).steer_status == 'delivered')
        wait_for(lambda: app.state.store.get(jid).status == JobStatus.SUCCEEDED)
        assert (ws / "stdin-closed").exists()
        import json
        sent = json.loads((ws / 'steer.json').read_text())
        assert sent['type'] == 'user' and 'change course' in sent['message']['content'][0]['text']
        assert str((settings.data_dir / 'bots' / bot['id'] / 'attachments' / name).resolve()) in sent['message']['content'][0]['text']
        assert app.state.store.get_message(mid).steer_delivered_at
        assert any(e['kind'] == 'steer' and e['status'] == 'delivered' for e in app.state.store.list_events(jid))
        assert len(app.state.store.list()) == 1


@pytest.mark.parametrize('boundary', [True, False])
def test_codex_interrupts_at_boundary_and_resumes_same_session(settings, tmp_path, monkeypatch, boundary):
    settings = __import__("dataclasses").replace(settings, job_timeout=10)
    if not boundary:
        monkeypatch.setattr('botoffice.providers.cli.STEER_BOUNDARY_TIMEOUT', 1.0)
    exe = fake_script(tmp_path, "fake_codex", '''
import json, signal, sys, time
from pathlib import Path
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in"); sys.exit(0)
if sys.argv[1:3] == ["exec", "resume"]:
    Path("resume-args.json").write_text(json.dumps(sys.argv))
    Path("resume-input").write_text(sys.stdin.read())
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"final response"}}), flush=True)
    print(json.dumps({"type":"turn.completed"}), flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *a: sys.exit(1))
sys.stdin.read()
print(json.dumps({"type":"thread.started", "thread_id":"thread_123456"}), flush=True)
print(json.dumps({"type":"item.started", "item":{"id":"i1", "type":"command_execution", "command":"make"}}), flush=True)
Path("reply.txt").write_text("outdated reply")
Path("ready").touch()
while not Path("boundary").exists(): time.sleep(.01)
print(json.dumps({"type":"item.completed", "item":{"id":"i1", "type":"command_execution"}}), flush=True)
while True: time.sleep(.02)
''')
    app = create_app(settings, {"codex": CodexAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'B'}).json()
        initial = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={'text':'start'}).json()
        jid = initial['job']['id']; ws = settings.data_dir / 'workspaces' / jid
        wait_for(lambda: (ws / 'ready').exists())
        a = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={'text':'first steer'}).json()
        b = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={'text':'second steer'}).json()
        time.sleep(.6)  # a command is running: the interrupt waits for its end (or the timeout)
        assert not (ws / 'resume-input').exists()
        if boundary:
            (ws / 'boundary').touch()
        wait_for(lambda: app.state.store.get(jid).status == JobStatus.SUCCEEDED)
        import json
        assert 'thread_123456' in json.loads((ws / 'resume-args.json').read_text())
        prompt = (ws / 'resume-input').read_text()
        assert 'interrupted' in prompt and 'first steer' in prompt and 'second steer' in prompt
        assert app.state.store.get_message(a['message']['id']).steer_status == 'delivered'
        assert app.state.store.get_message(b['message']['id']).steer_status == 'delivered'
        assert len(app.state.store.list()) == 1
        assert app.state.store.get(jid).reply_text == 'final response'
        assert 'starting a fresh session' not in (settings.data_dir / 'logs' / f'{jid}.log').read_text()


def test_queue_api_and_end_pending_fallback(settings):
    adapter = FakeAdapter(delay=.8)
    app = create_app(settings, {'codex':adapter})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'C'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        first = client.post(url, headers=H, json={'text':'start', 'mode':'steer'}).json()
        assert first['message']['steer_status'] is None
        wait_for(lambda: app.state.store.get(first['job']['id']).status == JobStatus.RUNNING)
        queued = client.post(url, headers=H, json={'text':'later', 'mode':'queue'}).json()
        assert queued['job']['id'] != first['job']['id'] and queued['message']['steer_status'] is None
        steer = client.post(url, headers=H, json={'text':'pending'}).json()
        mid = steer['message']['id']
        assert steer['job']['id'] == first['job']['id']
        edit = client.post(url + f'/{mid}/edit', headers=H, json={'text':'edit'})
        assert edit.status_code == 409 and 'cannot be edited' in edit.text
        wait_for(lambda: app.state.store.get_message(mid).steer_status == 'queued')
        converted = app.state.store.get_message(mid)
        assert str(converted.job_id) != first['job']['id']
        wait_for(lambda: any(e['kind'] == 'steer' and e['status'] == 'queued' for e in app.state.store.list_events(first['job']['id'])))
        events = wait_for(lambda: (ev := app.state.store.list_events(first['job']['id'])) and any(e['kind'] == 'done' for e in ev) and ev)
        queued_at = next(e['seq'] for e in events if e['kind'] == 'steer' and e['status'] == 'queued')
        assert queued_at < next(e['seq'] for e in events if e['kind'] == 'done')  # seen before the stream ends


def test_restart_converts_pending_steers(settings):
    store = JobStore(settings.data_dir / 'jobs.sqlite3')
    bot = store.create_bot('D', '', 'codex', None)
    scheduler = Scheduler(store, settings, {'codex': FakeAdapter()})
    _, job = scheduler.turns.post_message(bot, 'start')
    store.claim(Slot.DESKTOP_1, {'codex'})
    msg, _ = store.post_steer(bot.id, 'after restart', [])
    store.recover_interrupted()
    assert store.get_message(msg.id).steer_status == 'queued'
    assert store.get(store.get_message(msg.id).job_id).prompt == 'after restart'
    store.recover_interrupted()
    assert len(store.list()) == 2

@pytest.mark.parametrize('restricted', [False])
def test_no_session_converts_without_interrupt(settings, tmp_path, restricted):
    from dataclasses import replace
    settings = replace(settings, job_timeout=8)
    exe = fake_script(tmp_path, 'fake_codex_no_session', '''
import json, signal, sys, time
from pathlib import Path
if sys.argv[1:3] == ['login', 'status']:
    print('Logged in'); sys.exit(0)
signal.signal(signal.SIGINT, lambda *a: (Path('interrupted').touch(), sys.exit(1)))
sys.stdin.read()
Path('ready').touch()
time.sleep(.8)
print(json.dumps({'type':'item.completed', 'item':{'type':'command_execution'}}), flush=True)
time.sleep(.3)
print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message','text':'finished'}}), flush=True)
''')
    app = create_app(settings, {'codex': CodexAdapter(executable=exe)})
    with TestClient(app) as client:
        body = {'name':'NoSession'}
        if restricted: body['cli_access'] = 'restricted'
        bot = client.post('/v1/bots', headers=H, json=body).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'start'}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'ready').exists())
        mid = client.post(url, headers=H, json={'text':'steer'}).json()['message']['id']
        wait_for(lambda: app.state.store.get_message(mid).steer_status == 'queued')
        assert not (ws / 'interrupted').exists()
        assert app.state.store.get_message(mid).job_id != job['id']


def test_claude_unacknowledged_message_is_queued(settings, tmp_path):
    from dataclasses import replace
    settings = replace(settings, job_timeout=6)
    exe = fake_script(tmp_path, 'fake_claude_no_echo', '''
import json, sys
from pathlib import Path
if sys.argv[1:3] == ['auth', 'status']:
    print('{"loggedIn":true}'); sys.exit(0)
sys.stdin.readline()
Path('ready').touch()
sys.stdin.readline()
print(json.dumps({'type':'result', 'result':'done', 'session_id':'session_123456'}), flush=True)
sys.stdin.read()
''')
    app = create_app(settings, {'claude': ClaudeAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'NoEcho', 'engine':'claude', 'reason':'test'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'start'}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'ready').exists())
        mid = client.post(url, headers=H, json={'text':'unacknowledged'}).json()['message']['id']
        wait_for(lambda: app.state.store.get_message(mid).steer_status == 'queued')
        message = app.state.store.get_message(mid)
        assert message.steer_delivered_at is None
        assert app.state.store.get(message.job_id).prompt == 'unacknowledged'


@pytest.mark.parametrize('engine,flag,session', [
    ('cursor', '--resume', 'cursor_session_123'),
    ('antigravity', '--conversation', 'agy_session_123'),
])
def test_other_fallback_providers_resume_their_session(settings, tmp_path, engine, flag, session):
    from dataclasses import replace
    from botoffice.providers.cursor import CursorAdapter
    from botoffice.providers.antigravity import AntigravityAdapter
    from botoffice.providers.opencode import OpenCodeAdapter
    settings = replace(settings, job_timeout=8)
    source = '''
import json, os, signal, sys, time
from pathlib import Path
base = Path(os.environ.get('BOT_OFFICE_WORKSPACE', '.'))  # Antigravity runs in a stable directory
engine, flag, sid = ENGINE, FLAG, SID
if sys.argv[1:2] in (['status'], ['models']):
    print('Logged in'); sys.exit(0)
if flag in sys.argv:
    (base / 'resumed.json').write_text(json.dumps(sys.argv))
    if engine == 'opencode':
        print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':'resumed reply'}}), flush=True)
    elif engine == 'antigravity':
        print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':sid,'response':'resumed reply'}}), flush=True)
    else:
        print(json.dumps({'type':'result','session_id':sid,'result':'resumed reply'}), flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *a: sys.exit(1))
if engine == 'opencode': event = {'type':'step_start','sessionID':sid}
elif engine == 'antigravity': event = {'event':'init','conversation_id':sid,'init':{'model':'m','cwd':str(Path.cwd())}}
else: event = {'type':'system','session_id':sid}
print(json.dumps(event), flush=True)
(base / 'ready').touch()
while not (base / 'boundary').exists(): time.sleep(.01)
if engine == 'cursor': event = {'type':'tool_call','subtype':'completed'}
elif engine == 'antigravity': event = {'event':'step_update','step_update':{'step_type':'tool','state':'DONE','tool_name':'run_command'}}
else: event = {'type':'step_finish','sessionID':sid}
print(json.dumps(event), flush=True)
while True: time.sleep(.02)
'''.replace('ENGINE', repr(engine)).replace('FLAG', repr(flag)).replace('SID', repr(session))
    exe = fake_script(tmp_path, 'fake_' + engine, source)
    cls = {'cursor':CursorAdapter, 'antigravity':AntigravityAdapter, 'opencode':OpenCodeAdapter}[engine]
    app = create_app(settings, {engine:cls(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Other', 'engine':engine}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'start'}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'ready').exists())
        mid = client.post(url, headers=H, json={'text':'change direction'}).json()['message']['id']
        (ws / 'boundary').touch()
        wait_for(lambda: app.state.store.get(job['id']).status == JobStatus.SUCCEEDED)
        import json
        args = json.loads((ws / 'resumed.json').read_text())
        assert args[args.index(flag) + 1] == session
        assert any('change direction' in arg for arg in args)
        assert app.state.store.get_message(mid).steer_status == 'delivered'
        assert app.state.store.get(job['id']).reply_text == 'resumed reply'
        assert len(app.state.store.list()) == 1


def test_rewind_before_pending_steer_archives_it(settings):
    adapter = FakeAdapter(delay=30)
    app = create_app(settings, {'codex': adapter})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Rewind'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        first = client.post(url, headers=H, json={'text':'start'}).json()
        wait_for(lambda: app.state.store.get(first['job']['id']).status == JobStatus.RUNNING)
        steer = client.post(url, headers=H, json={'text':'change'}).json()['message']
        adapter.delay = 0
        response = client.post(url + f"/{first['message']['id']}/edit", headers=H,
                               json={'text':'replacement', 'cancel_running':True})
        assert response.status_code == 201, response.text
        assert app.state.store.get_message(steer['id']).archived_at
        assert all(job.prompt != 'change' for job in app.state.store.pending_jobs(bot['id']))


# --- review fixes ---------------------------------------------------------------------------

CLAUDE_ECHO = '''
import json, sys
from pathlib import Path
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn":true}'); sys.exit(0)
first = json.loads(sys.stdin.readline())
text = first["message"]["content"][0]["text"].strip()
print(json.dumps({"type":"system", "session_id":"session_123456"}), flush=True)
print(json.dumps({"type":"user","message":{"role":"user","content":[{"type":"text","text":text}]},"isReplay":True}), flush=True)
Path("ready").touch()
steer = json.loads(sys.stdin.readline())["message"]["content"][0]["text"]
echo = MODE
print(json.dumps({"type":"user","message":{"role":"user","content":[{"type":"text","text":echo}]},"isReplay":True}), flush=True)
print(json.dumps({"type":"result", "result":"adjusted", "session_id":"session_123456"}), flush=True)
sys.stdin.read()
'''


@pytest.mark.parametrize('mode', ['"  " + steer + "\\n"', 'steer.strip()', '"reformatted: " + steer[:20]'])
def test_claude_echo_is_matched_tolerantly(settings, tmp_path, mode):
    """Stripped text matches; an unmatched replay echo still delivers the oldest steer (never twice)."""
    from dataclasses import replace
    settings = replace(settings, job_timeout=8)
    exe = fake_script(tmp_path, 'fake_claude_echo', CLAUDE_ECHO.replace('MODE', mode))
    app = create_app(settings, {'claude': ClaudeAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Echo', 'engine':'claude', 'reason':'test'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'  start  '}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'ready').exists())
        time.sleep(.2)  # the initial prompt's echo must not count as a steer delivery
        mid = client.post(url, headers=H, json={'text':'turn left'}).json()['message']['id']
        wait_for(lambda: app.state.store.get(job['id']).status == JobStatus.SUCCEEDED)
        assert app.state.store.get_message(mid).steer_status == 'delivered'
        assert len(app.state.store.list()) == 1


@pytest.mark.parametrize('ending', ['unparsed', 'eof'])
def test_claude_stdin_closes_without_a_parsed_result(settings, tmp_path, monkeypatch, ending):
    from dataclasses import replace
    settings = replace(settings, job_timeout=8)
    monkeypatch.setattr('botoffice.providers.cli.CLAUDE_UNPARSED_GRACE', .3)
    exe = fake_script(tmp_path, 'fake_claude_' + ending, '''
import os, sys
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn":true}'); sys.exit(0)
sys.stdin.readline()
if ENDING == "eof":
    sys.stdout.flush(); os.close(1)
else:
    print('{"type":"result", "result": broken json', flush=True)
sys.stdin.read()
'''.replace('ENDING', repr(ending)))
    app = create_app(settings, {'claude': ClaudeAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Close', 'engine':'claude', 'reason':'test'}).json()
        job = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={'text':'start'}).json()['job']
        done = wait_for(lambda: (j := app.state.store.get(job['id'])).status not in (JobStatus.QUEUED, JobStatus.RUNNING) and j, 6)
        assert done.error != 'job timed out'


CODEX_INTERRUPTIBLE = '''
import json, signal, sys, time
from pathlib import Path
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in"); sys.exit(0)
if sys.argv[1:3] == ["exec", "resume"]:
    Path("resume-input").write_text(sys.stdin.read())
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"final response"}}), flush=True)
    sys.exit(0)
def on_int(*a):
    Path("interrupted").touch()
    if IGNORE: return
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"after interrupt"}}), flush=True)
    sys.exit(1)
signal.signal(signal.SIGINT, on_int)
sys.stdin.read()
print(json.dumps({"type":"thread.started", "thread_id":"thread_123456"}), flush=True)
Path("ready").touch()
while not Path("boundary").exists(): time.sleep(.01)
print(json.dumps({"type":"item.completed", "item":{"type":"command_execution"}}), flush=True)
while True: time.sleep(.02)
'''


def test_interrupt_with_nothing_pending_continues_instead_of_failing(settings, tmp_path, monkeypatch):
    from dataclasses import replace
    settings = replace(settings, job_timeout=10)
    exe = fake_script(tmp_path, 'fake_codex_gone', CODEX_INTERRUPTIBLE.replace('IGNORE', 'False'))
    app = create_app(settings, {'codex': CodexAdapter(executable=exe)})
    store = app.state.store
    real = store.pending_steers
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Gone'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'start'}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'ready').exists())
        # the steer disappears between the SIGINT and the resume (e.g. archived by a rewind)
        monkeypatch.setattr(store, 'pending_steers', lambda jid: [] if (ws / 'interrupted').exists() else real(jid))
        mid = client.post(url, headers=H, json={'text':'steer'}).json()['message']['id']
        (ws / 'boundary').touch()
        finished = wait_for(lambda: (j := store.get(job['id'])).status not in (JobStatus.QUEUED, JobStatus.RUNNING) and j)
        assert finished.status == JobStatus.SUCCEEDED and finished.attempts <= 1
        assert 'Continue the current task' in (ws / 'resume-input').read_text()
        assert finished.reply_text == 'final response'
        # output after the SIGINT is parsed, not discarded
        assert any('after interrupt' in json.dumps(e) for e in store.list_events(job['id']))
        assert store.get_message(mid).steer_status == 'queued'  # not lost


def test_ignored_sigint_is_escalated(settings, tmp_path, monkeypatch):
    from dataclasses import replace
    settings = replace(settings, job_timeout=10)
    monkeypatch.setattr('botoffice.providers.cli.STEER_INTERRUPT_GRACE', .3)
    exe = fake_script(tmp_path, 'fake_codex_stubborn', CODEX_INTERRUPTIBLE.replace('IGNORE', 'True'))
    app = create_app(settings, {'codex': CodexAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Stubborn'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'start'}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'ready').exists())
        mid = client.post(url, headers=H, json={'text':'steer now'}).json()['message']['id']
        (ws / 'boundary').touch()
        wait_for(lambda: app.state.store.get(job['id']).status == JobStatus.SUCCEEDED, 6)
        assert (ws / 'interrupted').exists() and 'steer now' in (ws / 'resume-input').read_text()
        assert app.state.store.get_message(mid).steer_status == 'delivered'


def test_database_error_in_stream_loop_kills_the_cli(settings, tmp_path, monkeypatch):
    import os, sqlite3
    from dataclasses import replace
    settings = replace(settings, job_timeout=10)
    exe = fake_script(tmp_path, 'fake_claude_dberror', '''
import json, os, sys, time
from pathlib import Path
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn":true}'); sys.exit(0)
sys.stdin.readline()
Path("pid").write_text(str(os.getpid()))
steer = json.loads(sys.stdin.readline())
print(json.dumps(steer), flush=True)
while True: time.sleep(.05)
''')
    app = create_app(settings, {'claude': ClaudeAdapter(executable=exe)})
    store = app.state.store

    def locked(_):
        raise sqlite3.OperationalError('database is locked')
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Locked', 'engine':'claude', 'reason':'test'}).json()
        url = f"/v1/bots/{bot['id']}/messages"
        job = client.post(url, headers=H, json={'text':'start'}).json()['job']
        ws = settings.data_dir / 'workspaces' / job['id']
        wait_for(lambda: (ws / 'pid').exists())
        monkeypatch.setattr(store, 'deliver_steer', locked)
        client.post(url, headers=H, json={'text':'steer'})
        wait_for(lambda: store.get(job['id']).status == JobStatus.FAILED, 6)
        pid = int((ws / 'pid').read_text())

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        wait_for(gone, 3)


def _running(settings, source='chat', provider='codex'):
    store = JobStore(settings.data_dir / 'jobs.sqlite3')
    bot = store.create_bot('S', '', provider, 'bot reason')
    scheduler = Scheduler(store, settings, {provider: FakeAdapter()})
    kwargs = {'sender': 'Other'} if source == 'inbox' else {}
    _, job = scheduler.turns.post_message(bot, 'start', source=source, **kwargs)
    store.claim(Slot.DESKTOP_1, {provider})
    return store, scheduler, bot, store.get(job.id)


def test_edit_deferral_leaves_steers_of_a_running_job_alone(settings):
    store, scheduler, bot, job = _running(settings)
    msg, _ = store.post_steer(bot.id, 'steer', [])
    with scheduler.defer_steers_for_edit([job]):
        pass
    assert store.get_message(msg.id).steer_status == 'pending'
    with scheduler.defer_steers_for_edit([job]):
        store.finish(job.id, JobStatus.CANCELLED)
    assert store.get_message(msg.id).steer_status == 'queued'


def test_queued_steer_sees_the_steered_reply_and_keeps_its_reason(settings):
    from botoffice.memory import Memory
    from botoffice.workspace import prepare_bot_workspace
    store, scheduler, bot, job = _running(settings, provider='claude')
    msg, _ = store.post_steer(bot.id, 'steer text', [], 'steer reason')
    store.add_message(bot.id, 'assistant', 'reply of the steered turn', job_id=job.id)
    store.finish(job.id, JobStatus.SUCCEEDED)
    scheduler._queue_pending_steers(job.id)
    converted = store.get(store.get_message(msg.id).job_id)
    assert converted.reason == 'steer reason'
    assert str(converted.id) in (settings.data_dir / 'claude-use-log.tsv').read_text()
    ws = settings.data_dir / 'workspaces' / str(converted.id); ws.mkdir(parents=True)
    prepare_bot_workspace(store, Memory(settings.data_dir), bot, converted, ws)
    history = (ws / 'history.md').read_text()
    assert history.index('steer text') < history.index('reply of the steered turn')


def test_pending_steer_of_a_missing_job_is_repaired(settings):
    store = JobStore(settings.data_dir / 'jobs.sqlite3')
    store.initialize()
    bot = store.create_bot('M', '', 'codex', None)
    msg = store.add_message(bot.id, 'user', 'orphan')
    with store._connect() as db:
        db.execute("UPDATE messages SET steer_job_id=?, steer_status='pending' WHERE id=?", (str(uuid4()), msg.id))
    store.recover_interrupted()
    msg = store.get_message(msg.id)
    assert msg.steer_status == 'queued'
    assert store.get(msg.job_id).provider == 'codex' and store.get(msg.job_id).prompt == 'orphan'


@pytest.mark.parametrize('source,steers', [('chat', True), ('api', True), ('inbox', False)])
def test_steers_only_attach_to_chat_or_api_turns(settings, source, steers):
    store, _, bot, _ = _running(settings, source=source)
    assert (store.post_steer(bot.id, 'steer', []) is not None) == steers


def test_opencode_legacy_session_pointer_is_archived_and_never_resumed(settings, tmp_path):
    from dataclasses import replace
    from botoffice.workspace import save_settings_snapshot
    from botoffice.providers.opencode import OpenCodeAdapter
    settings = replace(settings, job_timeout=8)
    exe = fake_script(tmp_path, 'fake_opencode_resume', '''
import json, sys
if "-s" in sys.argv:
    print("Error: resume hangs on 1.19.5", file=sys.stderr); sys.exit(1)
print(json.dumps({"type":"text","sessionID":"ses_fresh_1","part":{"type":"text","text":"fresh reply"}}), flush=True)
''')
    app = create_app(settings, {'opencode': OpenCodeAdapter(executable=exe)})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name':'Oc', 'engine':'opencode'}).json()
        pointer = settings.data_dir / 'bots' / bot['id'] / 'opencode-session'
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text('ses_old_1')
        save_settings_snapshot(app.state.store.get_bot(bot['id']), pointer.parent)
        job = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={'text':'start'}).json()['job']
        wait_for(lambda: app.state.store.get(job['id']).status == JobStatus.SUCCEEDED)
        assert not pointer.exists() and list(pointer.parent.glob('opencode-session.handoff-*'))
        assert app.state.store.get(job['id']).reply_text == 'fresh reply'


def test_restart_recovery_logs_a_steer_turned_claude_turn_once(settings):
    store, scheduler, bot, job = _running(settings, provider='claude')
    msg, _ = store.post_steer(bot.id, 'after restart', [], 'steer reason')

    async def boot():
        await scheduler.start()
        await scheduler.stop()
        await scheduler.start()  # a second startup changes nothing and logs nothing
        await scheduler.stop()

    asyncio.run(boot())
    converted = store.get(store.get_message(msg.id).job_id)
    assert converted.provider == 'claude' and converted.reason == 'steer reason'
    lines = [l.split('\t') for l in (settings.data_dir / 'claude-use-log.tsv').read_text().splitlines()]
    assert [(l[1], l[2]) for l in lines if l[1] == str(converted.id)] == [(str(converted.id), 'steer reason')]
