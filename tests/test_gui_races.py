"""Browser-side races, run with node on the real page code (with stubbed DOM and API):
cross-tab read/unread interleavings, read-aloud switched off while a request is in
flight, and a microphone permission granted after the context changed."""

import shutil
import subprocess

import pytest

from botoffice.api import CHAT_PAGE


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    return node


def _slice(start: str, end: str) -> str:
    page = CHAT_PAGE.read_text()
    first = page.index(start)
    return page[first:page.index(end, first)]


def _run(script: str) -> None:
    result = subprocess.run([_node(), "-e", script], capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout


def test_click_read_uses_click_time_revision_and_retries_once():
    code = _slice("function shownBot(", "async function markUnread(")
    script = r'''
const assert = require('assert');
class AuthError extends Error {}
function handleError(e) { throw e; }
const document = {visibilityState: 'visible'};
const state = {bots: [], selected: 'b', shown: {bot: 'b', counted: 0, max: 10}, readSent: new Map()};
function applyUnread(fresh) { Object.assign(state.bots[0], fresh); }
// the server: a revision bumped by every "mark as unread"; a stale read is ignored
const server = {revision: 1, marked: true, unread: 0};
const posts = [];
let onPost = null;
async function api(path, options) {
  const body = JSON.parse(options.body);
  posts.push(body.revision);
  if (onPost) { onPost(); onPost = null; }
  if (body.revision === server.revision && body.explicit) { server.marked = false; server.unread = 0; }
  return {id: 'b', read_revision: server.revision, marked_unread: server.marked, unread_count: server.unread};
}
''' + code + r'''
function reset(local, srv) {
  state.bots = [{id: 'b', ...local}]; state.readSent = new Map(); posts.length = 0;
  Object.assign(server, srv);
}
(async () => {
  // 1. another tab marked unread before the click, after this tab's last poll: the
  //    click's cached revision is stale, so it is retried once with the click-time copy
  reset({read_revision: 1, marked_unread: true, unread_count: 0}, {revision: 2, marked: true, unread: 0});
  await markRead('b', true, {revision: 1, synced: Promise.resolve({read_revision: 2, marked_unread: true, unread_count: 0})});
  assert.deepStrictEqual(posts, [1, 2]); assert.strictEqual(server.marked, false);

  // 2. another tab marks unread after the click (while messages load): never cleared
  reset({read_revision: 1, marked_unread: true, unread_count: 0}, {revision: 1, marked: true, unread: 0});
  const synced = Promise.resolve({read_revision: 1, marked_unread: true, unread_count: 0});
  server.revision = 2;  // the later "mark as unread", before the read arrives
  state.bots[0].read_revision = 2;  // and a bot poll already brought the new revision in
  await markRead('b', true, {revision: 1, synced});
  assert.deepStrictEqual(posts, [1]); assert.strictEqual(server.marked, true);
  assert.strictEqual(state.bots[0].marked_unread, true);

  // 3. this tab thought the bot was read; the click-time copy shows an unread: cleared
  reset({read_revision: 1, marked_unread: false, unread_count: 0}, {revision: 2, marked: true, unread: 0});
  state.readSent.set('b', 0);
  await markRead('b', true, {revision: 1, synced: Promise.resolve({read_revision: 2, marked_unread: true, unread_count: 0})});
  assert.deepStrictEqual(posts, [2]); assert.strictEqual(server.marked, false);

  // 4. nothing unread anywhere: no request
  reset({read_revision: 1, marked_unread: false, unread_count: 0}, {revision: 1, marked: false, unread: 0});
  await markRead('b', true, {revision: 1, synced: Promise.resolve({read_revision: 1, marked_unread: false, unread_count: 0})});
  assert.deepStrictEqual(posts, []);

  // 5. the retry itself is never repeated, even if yet another unread lands meanwhile
  reset({read_revision: 1, marked_unread: true, unread_count: 0}, {revision: 2, marked: true, unread: 0});
  let calls = 0;
  const realApi = api;
  api = async (path, options) => { calls++; if (calls === 2) server.revision = 3; return realApi(path, options); };
  await markRead('b', true, {revision: 1, synced: Promise.resolve({read_revision: 2, marked_unread: true, unread_count: 0})});
  assert.strictEqual(calls, 2); assert.strictEqual(server.marked, true);
})().catch(err => { console.error(err); process.exit(1); });
'''
    _run(script)


def test_read_aloud_off_wins_over_in_flight_requests_and_other_tabs():
    code = _slice("const player = {", "function fillVoicePanel(")
    script = r'''
const assert = require('assert');
class AuthError extends Error {}
let errors = [];
function handleError(e) { errors.push(e); }
const nodes = {};
const $ = (id) => nodes[id] || (nodes[id] = {setAttribute() {}, checked: false, hidden: false, textContent: ''});
function el() { return {setAttribute() {}}; }
function updateMic() {}
function fillVoicePanel() {}
const channels = [];
class BroadcastChannel { constructor() { channels.push(this); } postMessage(data) { this.sent = (this.sent || []).concat([data]); } }
const window = {BroadcastChannel};
const voice = {status: {tts: {available: true}}, prefs: {auto_read: true, voice: 'bm_george', speed: 1}, prefsSeq: 0};
let serverPrefs = {auto_read: true, voice: 'bm_george', speed: 1};
let gate = null, plays = 0;
async function api(path, options = {}) {
  if (path === '/v1/voice/prefs' && !options.method) { if (gate) await gate; return {...serverPrefs}; }
  if (path === '/v1/voice/prefs') { const update = JSON.parse(options.body); if (gate) await gate; serverPrefs = {...serverPrefs, ...update}; return {...serverPrefs}; }
  if (path === '/v1/voice/speakable') { plays++; return {chunks: []}; }
  throw new Error('unexpected ' + path);
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
''' + code + r'''
const reply = (id) => ({id, role: 'assistant', pending: false, text: 'reply ' + id});
(async () => {
  autoRead('b', [reply(1)]);                 // opening the bot: history is never read
  autoRead('b', [reply(1), reply(2)]); await tick();
  assert.strictEqual(plays, 1);

  // switched off here while the PUT is slow: a reply arriving meanwhile is not played
  let open; gate = new Promise((resolve) => { open = resolve; });
  nodes['read-aloud'].onclick();
  assert.strictEqual(voice.prefs.auto_read, false);   // local at once
  assert.deepStrictEqual(channels[0].sent, [{auto_read: false}]);
  autoRead('b', [reply(1), reply(2), reply(3)]); await tick();
  open(); gate = null; await tick();
  assert.strictEqual(plays, 1); assert.strictEqual(serverPrefs.auto_read, false);

  // switched off in another tab without a broadcast: the fresh server check wins
  voice.prefs.auto_read = true; serverPrefs.auto_read = false;
  autoRead('b', [reply(4)]); await tick();
  assert.strictEqual(plays, 1); assert.strictEqual(voice.prefs.auto_read, false);

  // switched off locally while the pre-play check is in flight (server still says on)
  voice.prefs.auto_read = true; serverPrefs.auto_read = true;
  gate = new Promise((resolve) => { open = resolve; });
  autoRead('b', [reply(5)]); await tick();
  setAutoRead(false);
  open(); gate = null; await tick();
  assert.strictEqual(plays, 1); assert.strictEqual(voice.prefs.auto_read, false);

  // another tab announces "off": this tab stops at once
  voice.prefs.auto_read = true; serverPrefs.auto_read = true;
  let stopped = 0;
  player.msg = 99; player.stop = () => { stopped++; };
  channels[0].onmessage({data: {auto_read: false}});
  assert.strictEqual(stopped, 1); assert.strictEqual(player.msg, null); assert.strictEqual(voice.prefs.auto_read, false);

  // and when it is on, auto-read still works
  channels[0].onmessage({data: {auto_read: true}});
  autoRead('b', [reply(6)]); await tick();
  assert.strictEqual(plays, 2);
  assert.deepStrictEqual(errors, []);
})().catch(err => { console.error(err); process.exit(1); });
'''
    _run(script)


def test_microphone_granted_after_a_context_change_is_released():
    code = _slice("function updateMic(", "async function transcribe(") + _slice("function stopDictation(", "$(\"mic\").onclick")
    script = r'''
const assert = require('assert');
const nodes = {};
const $ = (id) => nodes[id] || (nodes[id] = {setAttribute() {}, classList: {toggle() {}}, hidden: false});
let errors = [];
function handleError(e) { errors.push(e); }
function setStatus() {}
function serverStt() { return true; }
function useBrowserStt() { return false; }
function transcribe() {}
const MIC_MAX_MS = 120000;
const voice = {status: null, prefs: null, rec: null, stream: null, chunks: [], timer: null, busy: false, speech: null, micGen: 0, asking: false, prefsSeq: 0};
const state = {selected: 'a'};
let resolveMic, rejectMic, recorders = 0;
const window = {isSecureContext: true, MediaRecorder: true};
const navigator = {mediaDevices: {getUserMedia: () => new Promise((resolve, reject) => { resolveMic = resolve; rejectMic = reject; })}};
class MediaRecorder { constructor() { recorders++; this.state = 'inactive'; } start() { this.state = 'recording'; } stop() { this.state = 'inactive'; if (this.onstop) this.onstop(); } static isTypeSupported() { return true; } }
function stream() { const tracks = [{stopped: false, stop() { this.stopped = true; }}]; return {tracks, getTracks: () => tracks}; }
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
''' + code + r'''
(async () => {
  // switching bots (selectBot calls stopDictation) while the permission prompt is open
  let pending = startDictation(); await tick();
  assert.strictEqual(voice.asking, true);
  stopDictation();
  let granted = stream(); resolveMic(granted); await pending;
  assert(granted.tracks[0].stopped); assert.strictEqual(voice.rec, null); assert.strictEqual(voice.stream, null);
  assert.strictEqual(recorders, 0); assert.strictEqual(voice.asking, false);

  // the selected bot changed by any other route: also released
  pending = startDictation(); await tick();
  state.selected = 'b';
  granted = stream(); resolveMic(granted); await pending;
  assert(granted.tracks[0].stopped); assert.strictEqual(recorders, 0);

  // pressing the mic again while asking cancels the request
  pending = startDictation(); await tick();
  toggleDictation();
  granted = stream(); resolveMic(granted); await pending;
  assert(granted.tracks[0].stopped); assert.strictEqual(recorders, 0);

  // a denial after a context change shows no stale error
  pending = startDictation(); await tick();
  stopDictation();
  rejectMic(Object.assign(new Error('denied'), {name: 'NotAllowedError'})); await pending;
  assert.deepStrictEqual(errors, []);

  // the normal case still records
  pending = startDictation(); await tick();
  granted = stream(); resolveMic(granted); await pending;
  assert.strictEqual(recorders, 1); assert(voice.rec && !granted.tracks[0].stopped);
  clearTimeout(voice.timer);
})().catch(err => { console.error(err); process.exit(1); });
'''
    _run(script)


def test_header_controls_stay_on_screen_between_720_and_1100px():
    sync_api = pytest.importorskip("playwright.sync_api")
    page_html = CHAT_PAGE.read_text()
    from pathlib import Path

    def launch(pw):
        # the bundled build, else any Chromium Playwright downloaded earlier
        candidates = [None, *sorted(Path.home().glob(".cache/ms-playwright/chromium*/*/chrome*"), reverse=True)]
        for executable in candidates:
            if executable is not None and (not executable.is_file() or executable.suffix):
                continue
            try:
                return pw.chromium.launch(executable_path=str(executable) if executable else None)
            except Exception:  # noqa: BLE001, S112 - try the next one
                continue
        pytest.skip("chromium is unavailable")

    with sync_api.sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=page_html)
                   if route.request.url.endswith("/") else route.fulfill(status=401, body="{}"))
        page.goto("http://office.test/")
        page.evaluate("""() => {
            document.getElementById('lock').hidden = true;
            document.getElementById('app').hidden = false;
            document.getElementById('chat-head').hidden = false;
            for (const id of ['read-aloud', 'head-effort']) document.getElementById(id).hidden = false;
            document.getElementById('head-name').textContent = 'A bot with a fairly long name';
            document.getElementById('head-desc').textContent = 'Description '.repeat(10);
            document.getElementById('head-model').textContent = 'claude-opus-5-5 with a long model label';
            document.getElementById('head-effort').textContent = 'effort: medium';
            document.getElementById('head-tier').textContent = 'frontier';
        }""")
        for width in (721, 760, 820, 900, 1000, 1100, 1280):
            page.set_viewport_size({"width": width, "height": 800})
            offscreen = page.evaluate("""() => {
                const head = document.getElementById('chat-head');
                const out = [];
                if (head.scrollWidth > head.clientWidth + 1) out.push('header scrolls');
                for (const node of head.querySelectorAll('button, .model-pill')) {
                    if (node.hidden || getComputedStyle(node).display === 'none') continue;
                    const box = node.getBoundingClientRect();
                    if (box.right > window.innerWidth + 0.5 || box.left < 0) out.push(node.id || node.textContent);
                }
                return out;
            }""")
            assert offscreen == [], (width, offscreen)
        browser.close()


def test_header_keeps_six_buttons_and_the_rest_sit_in_the_more_menu():
    # 0.12.30, the owner: "Keep unread, search, activity, desktop, routines and assignments, with everything
    # else in a menu button"
    sync_api = pytest.importorskip("playwright.sync_api")
    page_html = CHAT_PAGE.read_text()
    from pathlib import Path

    with sync_api.sync_playwright() as pw:
        browser = None
        for executable in [None, *sorted(Path.home().glob(".cache/ms-playwright/chromium*/*/chrome*"), reverse=True)]:
            if executable is not None and (not executable.is_file() or executable.suffix):
                continue
            try:
                browser = pw.chromium.launch(executable_path=str(executable) if executable else None)
                break
            except Exception:  # noqa: BLE001, S112 - try the next one
                continue
        if browser is None:
            pytest.skip("chromium is unavailable")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=page_html)
                   if route.request.url.endswith("/") else route.fulfill(status=401, body="{}"))
        page.goto("http://office.test/")
        page.evaluate("""() => {
            document.getElementById('lock').hidden = true;
            document.getElementById('app').hidden = false;
            document.getElementById('chat-head').hidden = false;
            document.getElementById('read-aloud').hidden = false;
        }""")
        shown = page.evaluate("""() => [...document.querySelectorAll('#chat-head button')]
            .filter((b) => b.getClientRects().length).map((b) => b.textContent.trim())""")
        assert shown == ["Unread", "Search", "Activity", "Desktop", "Routines", "Assignments", "⋯"]
        page.click("#head-more")
        items = page.evaluate("""() => [...document.querySelectorAll('.sidebar-menu [role=menuitem]')]
            .map((b) => b.textContent)""")
        assert items == ["🔈 Read replies aloud: off", "Voice", "Show archived messages", "Agent messages",
                         "Memory", "Watchers", "Settings"]
        page.click("#head-more")  # a second press closes it
        assert page.evaluate("() => document.querySelectorAll('.sidebar-menu').length") == 0
        page.click("#head-more")
        page.click(".sidebar-menu >> text=Show archived messages")  # runs the hidden button's own handler
        assert page.get_attribute("#show-archived", "aria-pressed") == "true"
        page.click("#head-more")
        assert "Hide archived messages" in page.inner_text(".sidebar-menu")
        browser.close()
