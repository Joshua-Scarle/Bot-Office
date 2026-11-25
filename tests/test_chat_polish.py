"""0.12.1: instant sent-message echo, friendly errors, coalesced live redraws, gzip, jobs(bot_id) index."""
import shutil
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from botoffice.api import CHAT_PAGE, create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is unavailable")


def run_node(script: str) -> None:
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-1500:]


def function_source(page: str, name: str) -> str:
    start = page.index(f"function {name}(")
    depth, i = 0, page.index("{", start)
    for j in range(i, len(page)):
        depth += {"{": 1, "}": -1}.get(page[j], 0)
        if depth == 0:
            return page[start:j + 1]
    raise AssertionError(name)


def test_big_responses_are_gzipped_and_the_live_stream_is_not(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        page = client.get("/", headers={"Accept-Encoding": "gzip"})
        assert page.headers["content-encoding"] == "gzip"
        assert page.text == CHAT_PAGE.read_text()  # the client decodes it: same bytes as the file
        assert int(page.headers["content-length"]) < CHAT_PAGE.stat().st_size / 3
        plain = client.get("/", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers
        bot = client.post("/v1/bots", headers=H, json={"name": "Z"}).json()
        job = client.post(f"/v1/bots/{bot['id']}/messages", headers=H, json={"text": "hi"}).json()["job"]["id"]
        with client.stream("GET", f"/v1/jobs/{job}/events", headers={**H, "Accept": "text/event-stream", "Accept-Encoding": "gzip"}) as stream:
            assert "content-encoding" not in stream.headers
            assert stream.headers["content-type"].startswith("text/event-stream")
            stream.read()


def test_attachment_downloads_and_icons_are_not_recompressed(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "Z"}).json()
        data = b"A" * 20000  # compressible on purpose: only the path rule keeps it raw
        file_id = client.post(f"/v1/bots/{bot['id']}/attachments?name=notes.txt", headers={**H, "Content-Type": "text/plain"}, content=data)
        assert file_id.status_code in (200, 201), file_id.text
        got = client.get(f"/v1/bots/{bot['id']}/attachments/{file_id.json()['id']}", headers={**H, "Accept-Encoding": "gzip"})
        assert got.status_code == 200 and "content-encoding" not in got.headers and got.content == data
        listing = client.get("/v1/bots", headers={**H, "Accept-Encoding": "gzip"})
        assert listing.status_code == 200


def test_jobs_have_a_bot_index_and_queries_use_it(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app):
        db = sqlite3.connect(settings.data_dir / "agentdesk.sqlite3")
        names = {row[1] for row in db.execute("PRAGMA index_list(jobs)")}
        assert "jobs_bot" in names and "jobs_claim" in names
        plan = " ".join(str(row) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE bot_id=? AND status='running' ORDER BY created_at LIMIT 1", ("x",)))
        assert "jobs_bot" in plan


@needs_node
def test_errors_are_plain_and_say_what_to_do():
    page = CHAT_PAGE.read_text()
    run_node(f"""
const assert = require('assert');
class AuthError extends Error {{}}
{function_source(page, "friendlyError")}
assert.match(friendlyError(new TypeError('Failed to fetch')), /Can't reach Bot-Office.*SSH tunnel.*still in the box/);
assert.strictEqual(friendlyError(new TypeError('m.text is undefined')), 'm.text is undefined', 'a code bug is not blamed on the network');
assert.match(friendlyError(new Error('Load failed')), /Can't reach Bot-Office/);
assert.match(friendlyError(new Error('stream HTTP 502')), /live view lost its connection \\(HTTP 502\\).*reconnects by itself/);
assert.match(friendlyError(Object.assign(new Error('x'), {{status: 413}})), /too large/);
assert.match(friendlyError(Object.assign(new Error('x'), {{status: 429}})), /Too many requests/);
assert.match(friendlyError(Object.assign(new Error('database is locked'), {{status: 500}})), /Bot-Office had a problem \\(database is locked\\)/);
const timeout = Object.assign(new Error('No answer from the server after 30 s. Check the connection and try again.'), {{timeout: true}});
assert.strictEqual(friendlyError(timeout), timeout.message);
assert.strictEqual(friendlyError(Object.assign(new Error('Name already taken'), {{status: 409}})), 'Name already taken');
""")


@needs_node
def test_live_redraws_are_coalesced_to_one_per_frame():
    page = CHAT_PAGE.read_text()
    run_node(f"""
const assert = require('assert');
let frames = [], draws = 0, timers = [];
global.requestAnimationFrame = (fn) => frames.push(fn);
global.cancelAnimationFrame = () => {{}};
global.setTimeout = (fn) => timers.push(fn); global.clearTimeout = () => {{}};
const box = {{scrollTop: 0, scrollHeight: 500}}; const $ = () => box;
function refreshView() {{ draws += 1; }}
{function_source(page, "queueRefresh")}
const view = {{}};
for (let i = 0; i < 50; i++) queueRefresh(view, i === 49);
assert.strictEqual(draws, 0, 'nothing is drawn per event');
assert.strictEqual(frames.length, 1);
frames[0]();
assert.strictEqual(draws, 1);
assert.strictEqual(box.scrollTop, 500, 'sticks to the bottom when it was at the bottom');
timers[0]();  // the fallback timer firing afterwards does nothing
assert.strictEqual(draws, 1);
queueRefresh(view, false); assert.strictEqual(frames.length, 2, 'the next event schedules a new frame');
box.scrollTop = 0; view.stopped = true; frames[1]();
assert.strictEqual(draws, 1, 'a stopped bubble is not drawn'); assert.strictEqual(box.scrollTop, 0, 'and does not move the scroll');
const live = {{}}; queueRefresh(live, true); queueRefresh(live, false); frames[2]();
assert.strictEqual(box.scrollTop, 0, 'the latest event said the reader scrolled up');
""")


@needs_node
def test_message_is_echoed_before_the_server_answers_and_a_failed_send_restores_the_draft():
    page = CHAT_PAGE.read_text()
    start = page.index('$("composer").onsubmit = async (event) => {')
    end = page.index("\nfunction autoGrow()", start)
    run_node(r"""
const assert = require('assert');
const elements = { composer: {}, text: {value: 'hello there'}, 'queue-next': {checked: false}, messages: {} };
const $ = (id) => elements[id];
let bot = {id: 'one', name: 'Ada'}, state = {selected: 'one', echo: null}, draftRevision = 0, sendingImages = false;
const pendingImages = [];
let reject, resolve, echoes = [], cleared = 0;
function currentBot() { return bot; }
function api() { return new Promise((res, rej) => { resolve = res; reject = rej; }); }
function showEcho(b, text, n) { echoes.push([b.id, text, n, elements.text.value]); state.echo = {bot: b.id, nodes: []}; return state.echo; }
function clearEcho(echo) { cleared += 1; if (state.echo === echo) state.echo = null; }
function removeImage() {} function updateImages() {} function autoGrow() {} function saveDraft() {} function loadDraft() { return ""; } function setStatus() {} function renderBots() {}
async function loadMessages() {} function renderHeader() {} function handleError() {} function deleteUpload() {}
""" + page[start:end] + r"""
(async () => {
  const sending = elements.composer.onsubmit({preventDefault() {}});
  assert.deepStrictEqual(echoes, [['one', 'hello there', 0, 'hello there']], 'echoed before the request resolves');
  assert.strictEqual(elements.text.value, '', 'the box is empty at once');
  reject(Object.assign(new Error('Failed to fetch'), {}));
  await sending;
  assert.strictEqual(elements.text.value, 'hello there', 'a failed send gives the draft back');
  assert.ok(cleared >= 1 && state.echo === null, 'the local copy is removed');
  // a draft typed while it was sending is not overwritten
  elements.text.value = 'hello there';
  const second = elements.composer.onsubmit({preventDefault() {}});
  elements.text.value = 'something new';
  reject(new Error('boom')); await second;
  assert.strictEqual(elements.text.value, 'something new');
  // success keeps the echo until the stored copy is drawn, and marks it posted
  elements.text.value = 'again';
  const third = elements.composer.onsubmit({preventDefault() {}});
  resolve({message: {}, delivery: 'start'}); await third;
  assert.strictEqual(elements.text.value, '');
})().catch((err) => { console.error(err); process.exit(1); });
""")


@needs_node
def test_echo_lifecycle_two_quick_sends_and_stored_copy_handoff():
    page = CHAT_PAGE.read_text()
    run_node(f"""
const assert = require('assert');
class Node {{ constructor(cls) {{ this.className = cls || ''; this.children = []; this.parent = null; this.textContent = ''; this.scrollTop = 0; this.scrollHeight = 900; }}
  append(...kids) {{ for (const k of kids) {{ if (k.parent) k.parent.children = k.parent.children.filter((c) => c !== k); k.parent = this; this.children.push(k); }} }}
  remove() {{ if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }}
  querySelector(sel) {{ return this.children.find((c) => c.className.split(' ').includes(sel.slice(1))) || null; }} }}
function el(tag, cls, text) {{ const n = new Node(cls); if (text !== undefined) n.textContent = text; return n; }}
function workingRow() {{ const row = el('div', 'working'), status = el('span', 'status-text'); row.append(status); return {{ row, status, elapsed: el('span'), more: el('span') }}; }}
function isOwnerMessage(m) {{ return m.author === 'owner' && m.role === 'user'; }}
const thread = new Node('thread'), messages = new Node('messages');
const $ = (id) => (id === 'thread' ? thread : messages);
const state = {{ echo: null, shown: {{ bot: 'one', max: 10 }} }};
{function_source(page, "showEcho")}
{function_source(page, "clearEcho")}
{function_source(page, "echoStillNeeded")}
thread.append(el('div', 'empty'));
const idle = {{ id: 'one', name: 'Ada', activity: null }};
const first = showEcho(idle, 'one', 0);
assert.strictEqual(thread.children.length, 2, 'bubble and the starting row; the greeting is gone');
assert.strictEqual(first.baseId, 10);
assert.match(first.nodes[1].children[0].children[0].textContent, /Sending to Ada/);
// a second send before the first is drawn replaces the first echo's nodes and owns state.echo
const second = showEcho({{ ...idle, activity: 'queued' }}, 'two', 1);
assert.strictEqual(thread.children.length, 1, 'first nodes removed, no starting row while busy');
assert.ok(thread.children[0].children[1].textContent.includes('📎 1 attachment'));
clearEcho(first);  // the first send's refresh finishing must not remove the second echo
assert.strictEqual(state.echo, second); assert.strictEqual(thread.children.length, 1);
// stored-copy handoff: needed until a newer owner message is listed
assert.strictEqual(second.need, 2); assert.strictEqual(second.baseId, 10, 'the second echo keeps the first one\\'s base');
assert.ok(echoStillNeeded(second, [{{ id: 10, author: 'owner', role: 'user' }}], 'one'));
assert.ok(echoStillNeeded(second, [{{ id: 11, author: 'owner', role: 'user' }}], 'one'), 'only the first stored copy is listed: the second is still being sent');
assert.ok(echoStillNeeded(second, [{{ id: 11, author: 'bot_in', role: 'user' }}], 'one'), 'other authors do not count');
assert.ok(!echoStillNeeded(second, [{{ id: 11, author: 'owner', role: 'user' }}, {{ id: 12, author: 'owner', role: 'user' }}], 'one'));
assert.ok(!echoStillNeeded(second, [], 'two'), 'another bot is open');
assert.ok(!echoStillNeeded(null, [], 'one'));
clearEcho(second);
assert.strictEqual(state.echo, null); assert.strictEqual(thread.children.length, 0);
""")
