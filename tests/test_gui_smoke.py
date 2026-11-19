from html.parser import HTMLParser
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "link"):
            reference = attrs.get("src") if tag == "script" else attrs.get("href")
            if reference:
                self.references.append(reference)


def test_html_and_local_assets_and_status(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        bot = client.post("/v1/bots", headers=headers, json={"name": "Idle"}).json()
        listed = client.get("/v1/bots", headers=headers).json()
        assert next(item for item in listed if item["id"] == bot["id"])["status"] == "idle"
        page = client.get("/")
        assert page.status_code == 200
        assets = Assets()
        assets.feed(page.text)
        assert all(not urlsplit(ref).netloc and urlsplit(ref).scheme == "" for ref in assets.references)
        assert "/static/vendor/novnc/core/rfb.js" in page.text
        assert client.get("/static/vendor/novnc/core/rfb.js").status_code == 200


def test_opencode_effort_can_be_unset(settings):
    from botoffice.providers.fake import FakeAdapter

    with TestClient(create_app(settings, {"opencode": FakeAdapter()})) as client:
        headers = {"Authorization": "Bearer test-token"}
        bot = client.post("/v1/bots", headers=headers, json={"name": "Code", "engine": "opencode", "effort": "high"}).json()
        response = client.patch("/v1/bots/" + bot["id"], headers=headers, json={"effort": None})
        assert response.status_code == 200 and response.json()["effort"] is None
        page = client.get("/").text
        assert 'default (unset)' in page
        assert '$("f-custom").value = ""; fillEffortSelect()' in page


def test_image_composer_smoke():
    from botoffice.api import CHAT_PAGE
    page = CHAT_PAGE.read_text()
    assert 'document.addEventListener("paste"' in page
    assert 'function clipboardImageFiles(' in page
    assert 'function clipboardHtmlImages(' in page
    assert 'function normalizePastedImage(' in page
    assert 'addEventListener("drop"' in page
    assert 'id="image-file"' in page
    assert 'URL.createObjectURL' in page and 'attachments' in page


def test_sending_preserves_changed_draft_and_new_images():
    import shutil
    import subprocess
    import pytest
    from botoffice.api import CHAT_PAGE

    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    page = CHAT_PAGE.read_text()
    start = page.index('$("composer").onsubmit = async (event) => {')
    end = page.index('\nfunction autoGrow()', start)
    handler = page[start:end]
    script = r'''
const assert = require('assert');
const elements = { composer: {}, text: {value: 'first'}, 'queue-next': {checked: false}, messages: {} };
const $ = (id) => elements[id];
let bot = {id: 'one'}, state = {selected: 'one'}, draftRevision = 0, sendingImages = false;
const first = {id: 'a', bot: 'one'}, pendingImages = [first];
let resolveRequest;
function currentBot() { return bot; }
function api() { return new Promise(resolve => { resolveRequest = resolve; }); }
function removeImage(item) { pendingImages.splice(pendingImages.indexOf(item), 1); item.removed = true; }
function updateImages() {} function autoGrow() {} function setStatus() {} function renderBots() {}
async function loadMessages() {} function renderHeader() {} function handleError(e) { throw e; } function deleteUpload() {}
''' + handler + r'''
(async () => {
  const sending = elements.composer.onsubmit({preventDefault() {}});
  elements.text.value = 'new draft'; draftRevision++;
  const second = {id: 'b', bot: 'one'}; pendingImages.push(second);
  resolveRequest(); await sending;
  assert.strictEqual(elements.text.value, 'new draft');
  assert.deepStrictEqual(pendingImages, [second]);
  const sending2 = elements.composer.onsubmit({preventDefault() {}});
  state.selected = 'two'; bot = {id: 'two'}; draftRevision++;
  elements.text.value = 'another bot draft';
  resolveRequest(); await sending2;
  assert.strictEqual(elements.text.value, 'another bot draft');
})().catch(err => { console.error(err); process.exit(1); });
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_evicted_attachment_is_refetched_on_click():
    import shutil
    import subprocess
    import pytest
    from botoffice.api import CHAT_PAGE

    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    page = CHAT_PAGE.read_text()
    code = page[page.index('async function imageUrl('):page.index('\nasync function loadMessages()', page.index('async function imageUrl('))]
    script = r'''
const assert = require('assert');
const imageCache = new Map(), state = {selected: 'bot'}, revoked = new Set();
let count = 0, fetches = 0;
const URL = {createObjectURL() { return 'blob:' + ++count; }, revokeObjectURL(url) { revoked.add(url); }};
async function api() { fetches++; return {}; }
const elements = {'image-full': {}, 'image-view': {showModal() { this.opened = true; }}};
const $ = id => elements[id];
const document = {createElement() { return {}; }};
''' + code + r'''
(async () => {
  const wrap = {append(img) { this.img = img; }};
  showAttachment(wrap, 'bot', 'first');
  await new Promise(resolve => setImmediate(resolve));
  const original = wrap.img.src;
  for (let i = 0; i < 100; i++) await imageUrl('bot', 'other' + i);
  assert(revoked.has(original));
  wrap.img.onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(fetches, 102);
  assert.notStrictEqual(elements['image-full'].src, original);
  assert(!revoked.has(elements['image-full'].src));
  assert(elements['image-view'].opened);
})().catch(err => { console.error(err); process.exit(1); });
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_clipboard_image_paste_with_node():
    import shutil
    import subprocess
    import pytest
    from botoffice.api import CHAT_PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    page = CHAT_PAGE.read_text()
    start = page.index('// Clipboard access and cancellation')
    end = page.index('for (const zone of', start)
    script = r'''
const assert = require('assert');
const {File} = require('buffer');
const elements = {text: {value: '', selectionStart: 0, selectionEnd: 0, events: 0, setRangeText(s, a, b) { this.value = this.value.slice(0, a) + s + this.value.slice(b); }, dispatchEvent() { this.events++; }}, composer: {contains: x => x === elements.text}, messages: {contains: () => false}};
const $ = id => elements[id];
const body = {}, settings = {matches: () => true, isContentEditable: false};
const document = {body, activeElement: body, addEventListener: (name, fn) => { assert.equal(name, 'paste'); global.handler = fn; }};
const location = {href: 'https://office.test/chat', origin: 'https://office.test'};
class DOMParser { parseFromString(html) { return {querySelectorAll: () => [...html.matchAll(/<img src="([^"]+)">/g)].map(m => ({getAttribute: () => m[1]}))}; } }
let status = '', added = [];
function currentBot() { return {id: 'bot'}; }
function setStatus(s) { status = s; }
function addImages(files) { added.push(...files); return files.length; }
function getToken() { return 'test-token'; }
let fetched = [], mode = 'ok', cancelled = 0, reads = 0;
async function fetch(url, options) {
  fetched.push([options.headers ? 'auth' : 'plain', url]);
  if (mode === 'fail') throw Error('network');
  const chunks = mode === 'stream-large' ? [new Uint8Array(10 * 1024 * 1024), new Uint8Array(1)] : [new Uint8Array(await png.arrayBuffer())];
  return {ok: true, headers: {get: name => name === 'content-length' && mode === 'header-large' ? String(10 * 1024 * 1024 + 1) : name === 'content-type' ? 'image/png' : null},
    body: {cancel: async () => { cancelled++; }, getReader: () => ({read: async () => { reads++; return chunks.length ? {value: chunks.shift(), done: false} : {done: true}; }, cancel: async () => { cancelled++; }})}};
}
''' + page[start:end] + r'''
const png = new File([new Uint8Array([137,80,78,71,13,10,26,10])], 'image.png', {type: 'image/png', lastModified: 1});
async function paste({files = [], items = [], data = {}} = {}) {
  added = []; status = ''; let prevented = false;
  handler({clipboardData: {files, items, getData: type => data[type] || ''}, preventDefault: () => { prevented = true; }});
  await new Promise(resolve => setTimeout(resolve, 10));
  return {prevented, added, status};
}
(async () => {
  let r = await paste({files: [png]}); assert(r.prevented && r.added.length === 1);
  const item = {kind: 'file', type: 'image/png', getAsFile: () => png};
  r = await paste({items: [item]}); assert(r.prevented && r.added.length === 1);
  r = await paste({files: [png], items: [item], data: {'text/plain': 'image.png'}}); assert(r.prevented && r.added.length === 1);
  const unknown = new File([png], 'unknown', {type: ''});
  r = await paste({files: [unknown]}); assert(r.prevented && r.added[0].type === 'image/png');
  r = await paste({data: {'text/html': '<img src="data:image/png;base64,iVBORw0KGgo=">'}}); assert(r.prevented && r.added.length === 1);
  r = await paste({data: {'text/html': '<img src="/v1/bots/bot/attachments/abc">'}}); assert(r.prevented && r.added.length === 1 && fetched[0][0] === 'auth');
  r = await paste({data: {'text/html': '<img src="/images/test.png">'}}); assert(r.prevented && r.added.length === 1 && fetched[1][0] === 'plain');
  r = await paste({data: {'text/html': '<img src="https://other.test/image.png">', 'text/plain': 'hello'}}); assert(!r.prevented && !r.added.length);
  r = await paste({data: {'text/uri-list': 'file:///home/me/image.png', 'text/plain': '/home/me/image.png'}}); assert(r.prevented && !r.added.length && r.status.includes("browser can't read files"));
  const fromItems = new File([png], 'image.png', {type: 'image/png', lastModified: 2});
  r = await paste({files: [png], items: [{kind: 'file', type: 'image/png', getAsFile: () => fromItems}]}); assert(r.prevented && r.added.length === 1);
  const unsupported = new File([png], 'x.svg', {type: 'image/svg+xml'});
  const octet = new File([png], 'x', {type: 'application/octet-stream'});
  for (const file of [unsupported, octet, unknown]) {
    r = await paste({files: [file], data: {'text/plain': 'hello'}}); assert(!r.prevented && !r.added.length);
  }
  const before = fetched.length;
  r = await paste({data: {'text/html': '<img src="/images/test.png">', 'text/plain': 'hello'}});
  assert(!r.prevented && !r.added.length && fetched.length === before);
  mode = 'fail';
  r = await paste({data: {'text/html': '<img src="/images/test.png">'}});
  assert(r.prevented && !r.added.length && r.status.includes('Could not read'));
  elements.text.value = 'ab'; elements.text.selectionStart = elements.text.selectionEnd = 1;
  await resolvePastedImages([new URL('/images/test.png', location.href)], 'bot', 'hello');
  assert.equal(elements.text.value, 'ahellob'); assert.equal(elements.text.events, 1);
  r = await paste({data: {'text/html': '<img src="data:image/png;base64,%%%">', 'text/plain': 'hello'}});
  assert(!r.prevented);
  elements.text.value = 'ab'; elements.text.selectionStart = elements.text.selectionEnd = 1;
  r = await paste({data: {'text/html': '<img src="data:image/png;base64,A">', 'text/plain': 'hello'}});
  assert(r.prevented && !r.added.length && elements.text.value === 'ahellob' && elements.text.events === 2);
  r = await paste({data: {'text/html': '<img src="data:image/png;base64,AAA=">', 'text/plain': 'hello'}});
  assert(r.prevented && r.added.length === 1);
  const originalAtob = global.atob; let decodes = 0;
  global.atob = s => { decodes++; return originalAtob(s); };
  r = await paste({data: {'text/html': '<img src="data:image/png;base64,' + 'A'.repeat(4 * (Math.floor(10 * 1024 * 1024 / 3) + 1)) + '">'}});
  assert(r.prevented && !r.added.length && decodes === 0 && r.status.includes('10 MiB'));
  mode = 'header-large'; reads = cancelled = 0;
  r = await paste({data: {'text/html': '<img src="/large.png">'}});
  assert(r.prevented && !r.added.length && reads === 0 && cancelled === 1 && r.status.includes('10 MiB'));
  mode = 'stream-large'; reads = cancelled = 0;
  r = await paste({data: {'text/html': '<img src="/large.png">'}});
  assert(r.prevented && !r.added.length && reads === 2 && cancelled === 1 && r.status.includes('10 MiB'));
  document.activeElement = settings;
  r = await paste({files: [png]}); assert(!r.prevented && !r.added.length);
})().catch(err => {console.error(err); process.exit(1);});
'''
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_unread_gui_smoke():
    from botoffice.api import CHAT_PAGE

    page = CHAT_PAGE.read_text()
    assert '"/read"' in page and '"/unread"' in page
    assert 'id="mark-unread"' in page and "Mark as unread (Alt+U)" in page
    assert "oncontextmenu" in page and "document.title" in page
    assert '"visibilitychange"' in page
    # background reads never clear a manual unread; the revision travels with every read
    assert "if (!explicit && bot.marked_unread) return;" in page
    assert "let revision = click ? click.revision : bot.read_revision || 0;" in page
    assert "localStorage" not in page


def test_edit_gui_smoke():
    from botoffice.api import CHAT_PAGE

    page = CHAT_PAGE.read_text()
    assert '"/edit"' in page and "cancel_running" in page and "Show archived" in page
    assert "are NOT undone" in page
    assert 'setAttribute("aria-label", "archived")' in page
    # only the owner's own, completed, visible messages are editable
    assert 'isOwnerMessage(m) && (m.source === "chat" || m.source === "api" || m.source === "assignment") && !m.pending' in page
    assert 'function isOwnerMessage(m) { return m.author === "owner" && m.role === "user"; }' in page


def test_dictation_gui_smoke():
    from botoffice.api import CHAT_PAGE

    page = CHAT_PAGE.read_text()
    assert 'id="mic"' in page and 'aria-pressed="false"' in page
    assert "getUserMedia" in page and "decodeAudioData" in page and "OfflineAudioContext" in page
    assert '"/v1/voice/transcribe"' in page and 'type: "audio/wav"' in page
    assert "MIC_MAX_MS = 120000" in page and "stopTracks()" in page
    assert "NotAllowedError" in page and "isSecureContext" in page
    # the transcript only goes into the composer; the browser engine is opt-in
    transcribe = page[page.index("async function transcribe("):page.index("async function toWav16k(")]
    assert "insertAtCaret" in transcribe and "requestSubmit" not in transcribe
    assert "voice.prefs.browser_stt" in page


def test_read_aloud_gui_smoke():
    from botoffice.api import CHAT_PAGE

    page = CHAT_PAGE.read_text()
    assert 'id="read-aloud"' in page and 'id="voice-panel"' in page and 'id="voice-select"' in page
    assert '"/v1/voice/speak"' in page and '"/v1/voice/speakable"' in page
    assert "URL.revokeObjectURL(url)" in page and "new Audio(url)" in page
    # read-aloud only queues replies completed after the bot was opened, never the history
    auto = page[page.index("function autoRead("):page.index("function updateReadAloud(")]
    assert "player.seen.has(botId)" in auto and "voice.prefs.auto_read" in auto
    assert "The browser blocked audio playback" in page
    assert 'role === "assistant" && m.kind !== "say" && !m.pending' in page


def test_task_list_gui_contract(settings, monkeypatch):
    monkeypatch.setenv('BOT_OFFICE_TASKS_LABEL', 'Work queue')
    with TestClient(create_app(settings, {'codex': FakeAdapter()})) as client:
        assert client.get('/v1/tasks/settings', headers={'Authorization': 'Bearer test-token'}).json()['label'] == 'Work queue'
        page = client.get('/').text
    assert "settings = await api(\"/v1/tasks/settings\")" in page and "tasksLabel = settings.label" in page
    assert 'showToolPanel("assignments-panel", tasksLabel)' in page
    assert '$("board-heading").textContent = tasksLabel' in page
    assert all(label in page for label in ('Not started', 'In progress', 'Completed', 'id="board-panel"'))
    assert page.count('/v1/tasks?counts=1') == 1
    assert 'Promise.all([api("/v1/needs-you"), loadTaskCounts()])' in page
