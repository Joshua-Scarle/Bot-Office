"""0.11.6 chat UI: the bot's turn shows a live working indicator (status line, elapsed time), steering
messages are ordinary user messages with a delivery mark, and jobs are not cards in the chat. Run with
node on the real page code (stubbed DOM and API)."""
import shutil
import subprocess

import pytest

from botoffice.api import CHAT_PAGE


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    return node


def _page():
    return CHAT_PAGE.read_text()


def _slice(start, end):
    page = _page()
    first = page.index(start)
    return page[first:page.index(end, first)]


def _run(script):
    result = subprocess.run([_node(), "-e", script], capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout


PRELUDE = r'''
const assert = require('assert');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.textContent = ''; this.dataset = {};
    this.hidden = false; this.attrs = {}; this.title = ''; this.disabled = false;
    this.classList = { toggle: (c, on) => { const s = new Set(this.className.split(' ').filter(Boolean));
      (on === undefined ? !s.has(c) : on) ? s.add(c) : s.delete(c); this.className = [...s].join(' '); },
      contains: (c) => this.className.split(' ').includes(c) }; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(k, v) { this.attrs[k] = v; }
}
const document = { createElement: (tag) => new Node(tag), querySelectorAll: () => [] };
const elements = { messages: { scrollTop: 0, clientHeight: 0, scrollHeight: 0 } };
const $ = (id) => elements[id];
const state = { msgSig: '', live: new Map(), drawerOpen: false, pendingJobs: [] };
const setInterval = () => 0; const setTimeout = () => 0;
let reloads = 0;
async function loadMessages() { reloads++; }
async function api() { return {}; }
function setStatus() {} function handleError(e) { throw e; } function modelLabel(e, m) { return e + ' ' + m; }
function scheduleDrawer() {} function openDrawer() {}
'''


def _live_code():
    return (_slice("function el(", "function modelOptions(") if False else "") + _slice(
        "/* ---- the working indicator", "async function runStream(")


def _el_code():
    return _slice("function el(tag, cls, text)", "\n\n", ) if False else _slice("function el(tag, cls, text) {", "\n}\n") + "\n}\n"


def test_status_line_follows_the_stream_events_and_elapsed_time_counts_up():
    script = PRELUDE + _el_code() + "function clipX() {}\n" + "function clip(text, n) { return text.length > n ? text.slice(0, n - 1) + '…' : text; }\n" \
        + _live_code() + r'''
const view = newView({ job_id: 'j1', job_status: 'running', text: '', created_at: new Date().toISOString() });
refreshView(view);
assert.strictEqual(view.statusText.textContent, 'Starting up');
assert(view.body.hidden, 'no empty reply bubble before there is text');
const ev = (kind, data = {}) => applyEvent(view, { kind, attempt: 0, ...data });
ev('start');
ev('thinking', { text: 'hmm' });
assert.strictEqual(view.statusText.textContent, 'Thinking');
ev('tool', { name: 'Bash', summary: 'npm test -- --watch=false' });
assert.strictEqual(view.statusText.textContent, 'Running npm test -- --watch=false');
ev('tool', { name: 'Read', summary: 'src/app.py' });
assert.strictEqual(view.statusText.textContent, 'Reading src/app.py');
ev('tool', { name: 'mcp.search', summary: '' });
assert.strictEqual(view.statusText.textContent, 'Using mcp.search');
ev('steer', { status: 'delivered', message_id: 4 });
assert.strictEqual(view.statusText.textContent, 'Got your message, adjusting');
assert(reloads >= 1, 'the chat reloads so the steering message shows as delivered');
ev('text', { text: 'Working on it' });
assert.strictEqual(view.statusText.textContent, 'Working');
// 0.12.24: streamed narration is a step in Activity, never text in the chat bubble
assert.strictEqual(view.body.textContent, ''); assert(view.body.hidden);
assert.strictEqual(view.text, 'Working on it');
assert.strictEqual(view.steps[view.steps.length - 1].kind, 'text'); assert.strictEqual(view.steps[view.steps.length - 1].text, 'Working on it');
ev('text', { text: ' now' });
assert.strictEqual(view.steps[view.steps.length - 1].text, 'Working on it now');  // deltas extend one step
assert.strictEqual(view.tools, 3); assert.strictEqual(view.steps.length, 5);  // steps are data for the drawer, not chat nodes
assert.strictEqual(view.wrap.children.length, 3);  // who, indicator, reply: no step list inline
// queued / retry wait
view.status = 'queued'; view.notBefore = null; refreshView(view);
assert.strictEqual(view.statusText.textContent, 'Waiting to start'); assert(view.wrap.classList.contains('is-waiting'));
view.queuedAfter = 2; refreshView(view);
assert.strictEqual(view.more.textContent, '· 2 more queued'); assert(!view.more.hidden);
// a retry starts clean
ev('retry', { not_before: new Date(Date.now() + 3600e3).toISOString() });
assert.strictEqual(view.text, ''); assert.deepStrictEqual(view.steps, []);
assert(/^Waiting to retry at /.test(view.statusText.textContent));
assert.strictEqual(formatElapsed(5000), '5 s'); assert.strictEqual(formatElapsed(65000), '1 min 05 s');
assert.strictEqual(formatElapsed(3720000), '1 h 02 min');
assert(Number(view.elapsed.dataset.since) > 0);
'''
    _run(script)


def test_composer_and_delivery_marks_say_how_a_message_reaches_the_bot():
    script = PRELUDE + _slice("function composerPlaceholder(", "$(\"queue-next\").onchange") .replace(
        '$("queue-next").onchange', '//') + _slice("function steerMark(", "async function loadMessages()") + r'''
const bot = (steering, activity = 'running') => ({ name: 'Ada', steering, activity });
assert(/right away/.test(steerHint(bot('live'), false)));
assert(/same conversation and carries on/.test(steerHint(bot('interrupt'), false)));
assert(/same task starts again with your message/.test(steerHint(bot('restart'), false)));
assert(!/15 s/.test(steerHint(bot('interrupt'), false)));
assert(/cannot take a message while it works/.test(steerHint(bot('queue'), true)));
assert(/joins the waiting turn/.test(steerHint(bot('queue', 'queued'), false)));
assert(/waits until/.test(steerHint(bot('live'), true)));
assert.strictEqual(composerPlaceholder(bot('live'), false, false), 'Message (Ctrl+V or drop files to attach)');
assert(/^Steer Ada while it works/.test(composerPlaceholder(bot('live'), true, false)));
assert(/after the current turn/.test(composerPlaceholder(bot('queue'), true, true)));
const done = steerMark({ steer_status: 'delivered', steer_delivered_at: '2026-11-17T10:00:00Z' }, bot('live'));
assert.strictEqual(done.cls, 'delivered'); assert(/^✓ Delivered to Ada/.test(done.text));
assert.strictEqual(steerMark({ steer_status: 'queued' }, bot('queue')).cls, 'queued');
assert.strictEqual(steerMark({ steer_status: 'pending' }, bot('interrupt')).text, 'Delivering in a moment');
assert.strictEqual(steerMark({ steer_status: 'pending' }, bot('restart')).text, 'Delivering in a moment');
assert.strictEqual(steerMark({ steer_status: 'pending' }, bot('live')).text, 'Delivering');
'''
    _run(script)


def test_chat_shows_turns_not_jobs_and_keeps_the_composer_usable():
    page = _page()
    for gone in ("Cancel job", '"Queued…"', '"Working…"', '"typing"', "Steer · "):
        assert gone not in page, gone
    # job details live in a drawer reached from the indicator / header
    assert 'id="job-drawer"' in page and 'id="activity-open"' in page and "function openDrawer(" in page
    assert 'createElement("details")' in page
    # the composer is never disabled or hidden while the bot works: the next message steers
    assert '$("text").disabled' not in page and '$("send").disabled = true' not in page
    assert 'body.mode = $("queue-next").checked ? "queue" : "steer"' in page
    assert "if (m !== current) return null;" in page  # queued turns are a count, not extra bubbles
    assert "prefers-reduced-motion" in page
    assert 'style="' not in page and "http://" not in page and "https://" not in page
