"""0.12.32: each bot keeps its own unsent draft; switching chats no longer carries text over."""

import shutil
import subprocess

import pytest

from botoffice.api import CHAT_PAGE


def _run(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout


def _slice(start: str, end: str) -> str:
    page = CHAT_PAGE.read_text()
    first = page.index(start)
    return page[first:page.index(end, first)]


def test_drafts_are_kept_per_bot_and_survive_a_reload():
    code = _slice("const DRAFTS_KEY", "\n", ) + "\n" + _slice("const chatDrafts", "\nfunction loadDraft") \
        + "\n" + _slice("function loadDraft", "\n")
    script = r'''
const assert = require('assert');
const store = {};
const localStorage = {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }};
''' + code + r'''
saveDraft('a', 'hello A'); saveDraft('b', 'note for B');
assert.strictEqual(loadDraft('a'), 'hello A'); assert.strictEqual(loadDraft('b'), 'note for B');
assert.strictEqual(loadDraft('c'), '');
saveDraft('a', '   '); assert.strictEqual(loadDraft('a'), '');
assert.deepStrictEqual(JSON.parse(store['bo-chat-drafts']), {b: 'note for B'});
saveDraft(null, 'x'); assert.deepStrictEqual(JSON.parse(store['bo-chat-drafts']), {b: 'note for B'});
'''
    _run(script)
    # a stored copy is read back on load; a broken or blocked store is ignored
    load = _slice("const chatDrafts", "\nfunction saveDraft")
    _run("const assert=require('assert');const DRAFTS_KEY='k';"
         "const localStorage={getItem:()=>JSON.stringify({b:'kept'})};" + load +
         ";assert.strictEqual(chatDrafts.b,'kept');")
    _run("const assert=require('assert');const DRAFTS_KEY='k';"
         "const localStorage={getItem:()=>{throw new Error('blocked')}};" + load +
         ";assert.deepStrictEqual(chatDrafts,{});")
    _run("const assert=require('assert');const DRAFTS_KEY='k';"
         "const localStorage={getItem:()=>'[1,2]'};" + load + ";assert.deepStrictEqual(chatDrafts,{});")


def test_switching_bots_saves_the_old_draft_and_loads_the_new_one():
    page = CHAT_PAGE.read_text()
    line = page[page.index("async function selectBot("):]
    line = line[:line.index("state.selected = id")]
    assert 'saveDraft(state.selected, $("text").value); $("text").value = loadDraft(id);' in line
    assert 'saveDraft(state.selected, $("text").value)' in page[page.index('$("text").addEventListener("input"'):][:200]
