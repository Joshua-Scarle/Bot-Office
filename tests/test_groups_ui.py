"""0.12.0 sidebar sections and avatar UI: markup and logic run with node on the real page code.
Real-browser behaviour (drag, keyboard, dialogs, upload) is covered by scripts/e2e_gui.py."""

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


def test_markup_has_the_section_and_avatar_controls():
    assert 'id="new-section"' in PAGE and 'id="sidebar-live"' in PAGE and 'aria-live="polite"' in PAGE
    assert '<dialog id="section-dialog"' in PAGE and 'id="section-delete-cancel" autofocus' in PAGE
    assert 'id="f-presets"' not in PAGE and 'data-kind="pattern"' not in PAGE          # 0.12.11: the Pattern tab is gone
    assert 'id="f-hidden"' in PAGE and '"Hide"' in PAGE and '"Unhide"' in PAGE
    assert 'accept="image/png,image/jpeg,image/webp"' in PAGE and "image/svg" not in PAGE.split('id="f-image"')[1][:200]
    for aria in ('aria-expanded', 'aria-haspopup", "menu"', 'role", "menuitem"'):
        assert aria in PAGE
    assert '"Move to"' in PAGE and "Delete…" in PAGE and 'event.altKey' in PAGE


def test_layout_orders_bots_by_section_and_never_loses_one():
    code = _slice("function sidebarLayout()", "function renderBots()")
    _run('''
const assert = require("assert");
const state = { bots: [{id: "a"}, {id: "b"}, {id: "c"}, {id: "d"}], sections: null };
''' + code + r'''
let lay = sidebarLayout();                                     // no layout: one flat list
assert.deepStrictEqual(lay.sections, []); assert.deepStrictEqual(lay.ungrouped.bots.map((b) => b.id), ["a", "b", "c", "d"]);
state.sections = { sections: [{id: "s1", name: "One", collapsed: false, bot_ids: ["c", "a", "gone"]}], ungrouped: { collapsed: true, bot_ids: ["b"] } };
lay = sidebarLayout();
assert.deepStrictEqual(lay.sections[0].bots.map((b) => b.id), ["c", "a"]);                    // layout order, unknown ids dropped
assert.deepStrictEqual(lay.ungrouped.bots.map((b) => b.id), ["b", "d"]);                      // a bot newer than the layout lands in Ungrouped
assert.strictEqual(lay.ungrouped.collapsed, true);
assert.strictEqual(sectionOfBot(lay, "a").id, "s1"); assert.strictEqual(sectionOfBot(lay, "d").id, "ungrouped");
''')


def test_section_order_is_computed_before_the_request():
    code = _slice("async function placeSection(", "function moveSectionStep(")
    _run('''
const assert = require("assert");
let sent = null;
const state = {}, announce = () => {}, renderBots = () => {}, focusSidebar = () => {}, sidebarFail = () => {};
const api = async (path, opts) => { sent = JSON.parse(opts.body).ids; return { sections: [{id: "x", name: "X"}] }; };
''' + code + r'''
const lay = { sections: [{id: "a"}, {id: "b"}, {id: "c"}] };
(async () => {
  await placeSection(lay, "c", "a", false); assert.deepStrictEqual(sent, ["c", "a", "b"]);   // before a
  await placeSection(lay, "a", "c", true);  assert.deepStrictEqual(sent, ["b", "c", "a"]);   // after c
  await placeSection(lay, "a", "ungrouped", true); assert.deepStrictEqual(sent, ["b", "c", "a"]); // onto Ungrouped: the end
  await placeSection(lay, "b", "c", false); assert.deepStrictEqual(sent, ["a", "b", "c"]);
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_avatar_choice_prefers_upload_then_generated_then_emoji():
    code = _slice("function hasImage(bot)", "async function iconUrl(")
    _run('''
const assert = require("assert");
''' + code + r'''
assert.strictEqual(hasImage({icon_image: "image/png", avatar_seed: null}), true);
assert.strictEqual(hasImage({icon_image: null, avatar_seed: "preset-1"}), true);
assert.strictEqual(hasImage({icon_image: null, avatar_seed: null, icon: "🤖"}), false);
''')


def test_hidden_group_is_last_builtin_searchable_and_cannot_be_dragged_or_edited():
    code = _slice("function sidebarLayout()", "function renderBots()")
    _run('''
const assert = require("assert");
const state = { bots: [{id: "a"}, {id: "b"}, {id: "c"}], sections: null };
''' + code + r'''
let lay = sidebarLayout();                                      // no layout yet: Hidden exists, empty and collapsed
assert.deepStrictEqual(lay.hidden.bots, []); assert.strictEqual(lay.hidden.collapsed, true); assert.strictEqual(lay.hidden.builtin, true);
state.sections = { sections: [], ungrouped: { collapsed: false, bot_ids: ["a"] }, hidden: { id: "hidden", collapsed: false, bot_ids: ["c", "b", "gone"] } };
lay = sidebarLayout();
assert.deepStrictEqual(lay.hidden.bots.map((b) => b.id), ["c", "b"]); assert.strictEqual(lay.hidden.collapsed, false);
assert.strictEqual(sectionOfBot(lay, "b").id, "hidden"); assert.strictEqual(sectionOfBot(lay, "a").id, "ungrouped");
assert.ok(!lay.sections.some((s) => s.id === "hidden"));        // never in the reorderable list
''')
    page_ok = "const hidShown = lay.hidden.bots.filter(match)" in PAGE and "if (!filtering || hidShown.length)" in PAGE
    assert page_ok                                               # a search shows hidden matches, the header stays otherwise
    head = _slice("function sectionHead(", "function renameInput(")
    assert "fixed || !event.altKey" in head and "head.draggable = !filtering" in head and "if (!fixed) {" in head   # no menu, no drag for built-ins
    assert "!sec.builtin" in head


def test_bot_menu_offers_hide_and_unhide_and_settings_toggle_moves_the_bot():
    assert 'sec.id === "hidden" ? { text: "Unhide"' in PAGE and 'moveBot(botId, "hidden", 1e6)' in PAGE
    save = _slice("$(\"dialog-save\").disabled = true;", "$(\"delete-start\").onclick")
    assert 'section_id: $("f-hidden").checked ? "hidden" : null' in save
