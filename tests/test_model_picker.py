"""The searchable model picker and the create-form model load, run with node on the real page code.

Regression (0.11.11): opening "New bot" before /v1/model-choices answered left an empty menu, and the
late answer (start() and the dialog each loaded the list) re-rendered the form and put the user's pick
back on the first engine, Codex. Real-browser behaviour (focus, popup placement) is not covered here."""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from botoffice.api import CHAT_PAGE, create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}


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


LOGIC = _slice("function modelMatches(", "function createModelPicker(")

OPTIONS = r'''
const O = (engine, id, display, group) => ({ value: engine + "|" + id, engine, id, display, group });
const options = [
  O("codex", "gpt-6.1-terra", "Codex · gpt-6.1-terra", "Codex"),
  O("claude", "claude-sonnet-5-5", "Claude · Sonnet 5.5", "Claude"),
  O("claude", "claude-opus-5-5", "Claude · Opus 5.5", "Claude"),
  O("opencode", "openai/gpt-5", "OpenCode · openai/gpt-5", "OpenCode"),
];
const values = (r) => r.items.map((o) => o.value);
'''


def test_filter_is_case_insensitive_substring_on_name_id_and_engine():
    _run(LOGIC + OPTIONS + r'''
const assert = require('assert');
assert.deepStrictEqual(values(modelListItems(options, "SONNET", "", null)), ["claude|claude-sonnet-5-5"]);   // display
assert.deepStrictEqual(values(modelListItems(options, "5-5", "", null)), ["claude|claude-sonnet-5-5", "claude|claude-opus-5-5"]);  // id
assert.deepStrictEqual(values(modelListItems(options, "opencode", "", null)), ["opencode|openai/gpt-5"]);     // engine
assert.deepStrictEqual(values(modelListItems(options, "claude", "", null)), ["claude|claude-sonnet-5-5", "claude|claude-opus-5-5"]);  // provider
assert.strictEqual(modelListItems(options, "  ", "", null).items.length, 4);
const none = modelListItems(options, "zzz", "", null);
assert.strictEqual(none.noMatches, true); assert.strictEqual(none.first, -1);
''')


def test_selection_stays_visible_and_marked_when_filtered_out():
    _run(LOGIC + OPTIONS + r'''
const assert = require('assert');
const r = modelListItems(options, "opus", "codex|gpt-6.1-terra", null);
assert.deepStrictEqual(values(r), ["codex|gpt-6.1-terra", "claude|claude-opus-5-5"]);   // pinned on top
assert.strictEqual(r.first, 1);                                                         // Enter picks the real match
assert.strictEqual(r.noMatches, false);
const empty = modelListItems(options, "zzz", "codex|gpt-6.1-terra", null);
assert.strictEqual(empty.noMatches, true); assert.strictEqual(empty.first, -1);          // pinned row is not a match
''')


def test_custom_model_only_where_allowed_and_validated():
    _run(LOGIC + OPTIONS + r'''
const assert = require('assert');
const ok = modelListItems(options, "myprov/my-model", "", "opencode");
assert.deepStrictEqual(values(ok), ["opencode|myprov/my-model"]);
assert.strictEqual(ok.items[0].custom, true); assert.strictEqual(ok.noMatches, false);
assert.strictEqual(modelListItems(options, "myprov/my-model", "", null).noMatches, true);   // not allowed without OpenCode
for (const bad of ["no-slash", "a/", "/b", "-a/b", "a b/c", "a/b c", "a|b/c", ""]) {
  assert.strictEqual(validCustomModel(bad), false, bad);
  assert.ok(!modelListItems(options, bad, "", "opencode").items.some((o) => o.custom), bad);
}
assert.strictEqual(validCustomModel("openrouter/anthropic/claude-x"), true);
// an exact listed id is not offered a second time as custom
assert.ok(!modelListItems(options, "openai/gpt-5", "", "opencode").items.some((o) => o.custom));
''')


def test_keyboard_navigation_wraps():
    _run(LOGIC + r'''
const assert = require('assert');
assert.strictEqual(nextActiveIndex(-1, 3, "ArrowDown"), 0);
assert.strictEqual(nextActiveIndex(2, 3, "ArrowDown"), 0);
assert.strictEqual(nextActiveIndex(0, 3, "ArrowUp"), 2);
assert.strictEqual(nextActiveIndex(-1, 3, "ArrowUp"), 2);
assert.strictEqual(nextActiveIndex(1, 3, "End"), 2);
assert.strictEqual(nextActiveIndex(1, 3, "Home"), 0);
assert.strictEqual(nextActiveIndex(0, 0, "ArrowDown"), -1);
''')


def test_picker_component_keyboard_aria_and_late_reload():
    code = LOGIC + _slice("function createModelPicker(", "/* ---- settings dialog ---- */")
    _run(code + OPTIONS + r'''
const assert = require('assert');
function node() {
  return { children: [], attrs: {}, listeners: {}, hidden: false, className: "", textContent: "", value: "",
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(t, f) { this.listeners[t] = f; }, select() {},
    replaceChildren(...c) { this.children = c; }, append(c) { this.children.push(c); } };
}
function el(tag, cls, text) { const n = node(); n.className = cls || ""; n.textContent = text || ""; return n; }
const ENGINE_LABELS = { opencode: "OpenCode" };
const input = node(), list = node(); list.id = "pick";
const changes = [];
const p = createModelPicker(input, list, { customEngine: "opencode", onChange: (v) => changes.push(v) });
p.customEngine = "opencode";
p.setOptions(options, "codex|gpt-6.1-terra");
assert.strictEqual(input.value, "Codex · gpt-6.1-terra");
const key = (k) => { let prevented = false; input.listeners.keydown({ key: k, preventDefault() { prevented = true; }, stopPropagation() {} }); return prevented; };
const type = (t) => { input.value = t; input.listeners.input(); };
const texts = () => list.children.map((c) => c.textContent);

assert.strictEqual(key("ArrowDown"), true);                      // opens
assert.strictEqual(input.attrs["aria-expanded"], "true"); assert.strictEqual(list.hidden, false);
type("opus");
assert.deepStrictEqual(texts(), ["Codex · gpt-6.1-terra", "Claude · Opus 5.5"]);
assert.strictEqual(list.children[0].attrs["aria-selected"], "true");      // current selection marked
assert.strictEqual(input.attrs["aria-activedescendant"], "pick-1");       // first real match is active
assert.strictEqual(list.children[1].attrs.role, "option");
assert.strictEqual(key("Enter"), true);                                   // selects, never submits the form
assert.strictEqual(p.value, "claude|claude-opus-5-5"); assert.deepStrictEqual(changes, ["claude|claude-opus-5-5"]);
assert.strictEqual(input.attrs["aria-expanded"], "false"); assert.strictEqual(input.value, "Claude · Opus 5.5");

type("zzz");
assert.deepStrictEqual(texts(), ["Claude · Opus 5.5", "No matches"]);
assert.strictEqual(key("Enter"), true); assert.strictEqual(p.value, "claude|claude-opus-5-5");   // nothing to pick
key("Escape");                                                            // closes and clears the query
assert.strictEqual(list.hidden, true); assert.strictEqual(input.value, "Claude · Opus 5.5");
assert.strictEqual(key("Enter"), false);                                  // closed: Enter is left to the form

type("my/typed"); key("Enter");                                           // typed custom provider/model
assert.strictEqual(p.value, "opencode|my/typed"); assert.strictEqual(input.value, "OpenCode · my/typed");
type("badtyped"); key("Enter"); assert.strictEqual(p.value, "opencode|my/typed");

// a late reload of the choices keeps the user's pick; a pristine form takes the preferred one
p.setOptions(options, "codex|gpt-6.1-terra");
assert.strictEqual(p.value, "opencode|my/typed");
p.reset(); p.setOptions(options, "codex|gpt-6.1-terra");
assert.strictEqual(p.value, "codex|gpt-6.1-terra");
''')


def test_choices_load_is_shared_and_form_has_one_picker():
    page = CHAT_PAGE.read_text()
    assert '<select id="f-model"' not in page and "f-custom-list" not in page
    _run(_slice("let choicesLoading", "/* ---- icons ---- */") + r'''
const assert = require('assert');
let calls = 0, release;
const state = { choices: null };
const $ = () => ({ textContent: "", open: false });
class AuthError extends Error {}
function renderBots() {} function renderHeader() {} function fillModelSelect() {} function handleError() {}
async function api() { calls++; await new Promise((r) => { release = r; }); return [{ engine: "claude", models: [] }]; }
(async () => {
  const a = loadChoices(), b = loadChoices();   // start() and openDialog() both ask
  assert.strictEqual(a, b); assert.strictEqual(calls, 1);
  await new Promise((r) => setImmediate(r)); release(); await a;
  assert.strictEqual(state.choices.length, 1);
  const c = loadChoices(); assert.notStrictEqual(c, a); assert.strictEqual(calls, 2);   // a finished load can be repeated
  await new Promise((r) => setImmediate(r)); release(); await c;
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_create_bot_saves_the_chosen_engine_and_model(settings):
    adapters = {"codex": FakeAdapter(), "claude": FakeAdapter()}
    with TestClient(create_app(settings, adapters)) as client:
        choices = client.get("/v1/model-choices", headers=H).json()
        # the shape the form builds its options from
        assert [c["engine"] for c in choices] == ["codex", "claude"]
        assert all(c["models"] and {"id", "display"} <= set(c["models"][0]) for c in choices)
        sonnet = next(m["id"] for m in choices[1]["models"] if "sonnet" in m["id"])
        bot = client.post("/v1/bots", headers=H, json={"name": "Picked", "engine": "claude", "model": sonnet,
                                                       "reason": "needs review depth"})
        assert bot.status_code == 201
        assert (bot.json()["engine"], bot.json()["model"]) == ("claude", sonnet)


def test_list_mousedown_is_prevented_so_scrollbar_drag_keeps_focus():
    page = CHAT_PAGE.read_text()
    assert 'list.addEventListener("mousedown", (event) => event.preventDefault())' in page
