"""0.11.13 sidebar and chat UI logic (Needs-you row, bot search, resizable sidebar, file chips), run with node
on the real page code. Real-browser behaviour is covered by scripts/e2e_gui.py."""

import json
import shutil
import subprocess

import pytest

from botoffice.api import CHAT_PAGE

PAGE = CHAT_PAGE.read_text()


def _slice(start: str, end: str) -> str:
    first = PAGE.index(start)
    return PAGE[first:PAGE.index(end, first)]


def _run(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout


STORE = r'''
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
const assert = require("assert");
'''


def test_markup_has_the_accessible_controls():
    assert 'id="needs-toggle" class="needs-toggle" type="button" aria-expanded="false" aria-controls="needs-list"' in PAGE
    assert 'id="notify-enable"' in PAGE and PAGE.index('id="notify-enable"') < PAGE.index('id="needs-list"')  # outside the list
    assert 'id="bot-search"' in PAGE and 'type="search"' in PAGE and 'aria-label="Search bots' in PAGE
    assert 'id="sidebar-resize"' in PAGE and 'role="separator"' in PAGE and 'aria-orientation="vertical"' in PAGE
    assert "cursor: col-resize" in PAGE and "var(--sidebar-w, 272px)" in PAGE
    assert '<input id="image-file" type="file" multiple hidden>' in PAGE  # any file type


def test_needs_you_starts_collapsed_and_remembers_its_state():
    code = _slice("const NEEDS_OPEN_KEY", '$("notify-enable").onclick')
    _run(STORE + r'''
const cls = new Set(); const attrs = {};
const nodes = { "needs-you": { classList: { toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)), contains: (c) => cls.has(c) } },
                "needs-toggle": { setAttribute: (k, v) => { attrs[k] = v; }, onclick: null } };
const $ = (id) => nodes[id];
''' + code + r'''
assert.strictEqual(cls.has("open"), false); assert.strictEqual(attrs["aria-expanded"], "false");   // collapsed on a fresh profile
nodes["needs-toggle"].onclick();
assert.strictEqual(cls.has("open"), true); assert.strictEqual(attrs["aria-expanded"], "true"); assert.strictEqual(store[NEEDS_OPEN_KEY], "1");
nodes["needs-toggle"].onclick();
assert.strictEqual(cls.has("open"), false); assert.strictEqual(store[NEEDS_OPEN_KEY], "0");
store[NEEDS_OPEN_KEY] = "1"; setNeedsOpen(localStorage.getItem(NEEDS_OPEN_KEY) === "1", false);    // a reload with it open
assert.strictEqual(cls.has("open"), true);
''')


def test_needs_you_count_badge_hides_at_zero():
    block = _slice('$("needs-you").hidden = !items.length;', "const stored = sessionStorage")
    assert '$("needs-count").hidden = !items.length' in block and "String(items.length)" in block


def test_bot_search_matches_name_description_and_model():
    _run(STORE + _slice("function botMatches(", "function renderBots(") + r'''
const bots = [
  { name: "Printer Helper", description: "Slices STL files for the Prusa", model: "gpt-6.1-terra", engine: "codex" },
  { name: "Writer", description: "Drafts newsletters", model: "claude-opus-5-5", engine: "claude" },
  { name: "Plain", description: "", model: null, engine: "opencode" },
];
const label = (b) => b.engine + " " + (b.model || "");
const find = (q) => bots.filter((b) => botMatches(b, q, label(b))).map((b) => b.name);
assert.deepStrictEqual(find(""), ["Printer Helper", "Writer", "Plain"]);
assert.deepStrictEqual(find("   "), ["Printer Helper", "Writer", "Plain"]);
assert.deepStrictEqual(find("printer"), ["Printer Helper"]);        // name, any case
assert.deepStrictEqual(find("NEWSLETTER"), ["Writer"]);             // description
assert.deepStrictEqual(find("opus"), ["Writer"]);                   // model
assert.deepStrictEqual(find("terra"), ["Printer Helper"]);
assert.deepStrictEqual(find("slices prusa"), ["Printer Helper"]);   // every word, anywhere
assert.deepStrictEqual(find("slices newsletters"), []);
assert.deepStrictEqual(find("zzz"), []);
assert.deepStrictEqual(find("opencode"), ["Plain"]);                // engine
''')


def test_bot_search_has_an_empty_state_and_escape_clears():
    render = _slice("function renderBots()", "async function loadBots()")
    assert "No bots match" in render and 'setAttribute("role", "status")' in render
    assert 'event.key === "Escape"' in PAGE and 'searchBox.value = ""; renderBots(); searchBox.blur();' in PAGE
    assert '"/"' in PAGE and "event.ctrlKey || event.metaKey" in PAGE
    assert PAGE.index('searchBox.addEventListener("input", renderBots)') > 0


def test_sidebar_width_is_clamped_remembered_and_resettable():
    code = _slice("const SIDEBAR_KEY", "{\n  const handle")
    _run(STORE + r'''
const style = {}; const attrs = {};
const root = { style: { setProperty: (k, v) => { style[k] = v; }, removeProperty: (k) => { delete style[k]; } } };
const handle = { setAttribute: (k, v) => { attrs[k] = v; } };
const $ = (id) => (id === "app" ? root : handle);
const window = { innerWidth: 1400 };
''' + code + r'''
assert.strictEqual(clampSidebarWidth(100, 1400), 200);
assert.strictEqual(clampSidebarWidth(9999, 1400), 520);
assert.strictEqual(clampSidebarWidth(300, 1400), 300);
assert.strictEqual(clampSidebarWidth("junk", 1400), 272);
assert.strictEqual(clampSidebarWidth(500, 600), 280 < 200 ? 200 : 280);   // a 600 px viewport leaves 280 px
assert.strictEqual(clampSidebarWidth(500, 400), 200);                      // never below the minimum
assert.strictEqual(setSidebarWidth(340, true), 340);
assert.strictEqual(style["--sidebar-w"], "340px"); assert.strictEqual(store[SIDEBAR_KEY], "340"); assert.strictEqual(attrs["aria-valuenow"], "340");
assert.strictEqual(setSidebarWidth(10, true), 200); assert.strictEqual(store[SIDEBAR_KEY], "200");
resetSidebarWidth();
assert.ok(!("--sidebar-w" in style)); assert.ok(!(SIDEBAR_KEY in store)); assert.strictEqual(attrs["aria-valuenow"], "272");
''')


def test_resize_handle_uses_pointer_events_keys_and_double_click():
    block = _slice("const handle = $(\"sidebar-resize\")", "async function loadBots()")
    for needle in ('"pointerdown"', '"pointermove"', '"pointerup"', "setPointerCapture", '"dblclick"', "ArrowLeft", "ArrowRight"):
        assert needle in block


def test_file_chips_and_sizes():
    _run(_slice("function formatBytes(", "function updateImages()") + r'''
const assert = require("assert");
assert.strictEqual(formatBytes(0), "0 B"); assert.strictEqual(formatBytes(1536), "1.5 KB");
assert.strictEqual(formatBytes(52428800), "50 MB"); assert.strictEqual(formatBytes(3 * 1024 * 1024), "3.0 MB");
const id = "2026-11-19-" + "a".repeat(32) + "-part.stl";
assert.strictEqual(FILE_ID.exec(id)[1], "part.stl");
assert.strictEqual(FILE_ID.test("2026-11-19-" + "a".repeat(32) + ".png"), false);   // images keep their own path
''')


def test_upload_accepts_any_type_and_non_images_become_chips():
    assert "wrap.append(chatFileChip(botId, id)); return; }" in PAGE
    assert '"?name=" + encodeURIComponent' in PAGE and "isImage" in PAGE
    assert "Choose a PNG, JPEG, GIF or WebP image" not in PAGE
    assert 'Blob([blob], {type: "application/octet-stream"})' in PAGE and "link.download = name" in PAGE  # a download, never inline
    assert "addImages(event.dataTransfer.files)" in PAGE  # drop
    assert 'if (!pasteImageTypes.includes(type) && hasText) return;' in PAGE  # paste
