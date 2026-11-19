"""Slash command popup logic (0.11.14), run with node on the real page code.

Covers filtering, keyboard movement and the line split. The popup itself (focus, placement, Enter picking
instead of sending) is driven in a real browser by scripts/e2e_gui.py."""

import shutil
import subprocess

import pytest

from botoffice.api import CHAT_PAGE


def _slice(start: str, end: str) -> str:
    page = CHAT_PAGE.read_text()
    first = page.index(start)
    return page[first:page.index(end, first)]


def _run(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout


LOGIC = _slice("/* ---- slash commands: logic", "/* ---- slash commands: the popup")
COMMANDS = r'''
const C = (name, description, extra) => Object.assign({ name, description, action: "run", source: "cli", kind: "builtin" }, extra || {});
const commands = [
  C("model", "Switch model", { action: "map", target: "model" }),
  C("compact", "Summarize the conversation to free context"),
  C("context", "Show context window usage"),
  C("code-review", "Review the current diff", { kind: "command", source: "custom" }),
  C("doctor", "Diagnose", { action: "grey", reason: "terminal only" }),
  C("recap", "Recap; mentions compact in passing"),
];
const names = (list) => list.map((c) => c.name);
const assert = require('assert');
'''


def test_query_is_the_first_token_only():
    _run(LOGIC + r'''
const assert = require('assert');
assert.strictEqual(slashQuery("/", 1), "");
assert.strictEqual(slashQuery("/com", 4), "com");
assert.strictEqual(slashQuery("/com", 2), "c");            // the caret decides how much was typed
assert.strictEqual(slashQuery("/compact now", 12), null);   // after a space the command is done
assert.strictEqual(slashQuery("/compact now", 4), "com");   // caret back inside the name
assert.strictEqual(slashQuery("hello /x", 8), null);        // not at the start
assert.strictEqual(slashQuery("/a\nb", 4), null);
assert.strictEqual(slashQuery("", 0), null);
''')


def test_filter_is_case_insensitive_name_prefix_first_then_name_then_description():
    _run(LOGIC + COMMANDS + r'''
assert.deepStrictEqual(names(slashFilter(commands, "")), names(commands));
assert.deepStrictEqual(names(slashFilter(commands, "CO")), ["compact", "context", "code-review", "recap"]);  // prefix, then description ("compact in passing" matches recap)
assert.deepStrictEqual(names(slashFilter(commands, "view")), ["code-review"]);                              // substring of the name
assert.deepStrictEqual(names(slashFilter(commands, "free context")), ["compact"]);                         // description
assert.deepStrictEqual(names(slashFilter(commands, "zzz")), []);
assert.deepStrictEqual(names(slashFilter(commands, "doc")), ["doctor"]);                                     // greyed commands stay in the list
''')


def test_keyboard_movement_wraps():
    _run(LOGIC + r'''
const assert = require('assert');
assert.strictEqual(slashMove(-1, "ArrowDown", 3), 0);
assert.strictEqual(slashMove(0, "ArrowDown", 3), 1);
assert.strictEqual(slashMove(2, "ArrowDown", 3), 0);
assert.strictEqual(slashMove(0, "ArrowUp", 3), 2);
assert.strictEqual(slashMove(2, "ArrowUp", 3), 1);
assert.strictEqual(slashMove(1, "End", 3), 2);
assert.strictEqual(slashMove(1, "Home", 3), 0);
assert.strictEqual(slashMove(1, "x", 3), 1);
assert.strictEqual(slashMove(0, "ArrowDown", 0), -1);
''')


def test_split_and_lookup():
    _run(LOGIC + COMMANDS + r'''
assert.deepStrictEqual(slashSplit("/compact"), { name: "compact", args: "" });
assert.deepStrictEqual(slashSplit("  /review  main  branch "), { name: "review", args: "main  branch" });
assert.deepStrictEqual(slashSplit("/ship line one\nline two"), { name: "ship", args: "line one\nline two" });
assert.deepStrictEqual(slashSplit("/code-review:x y"), { name: "code-review:x", args: "y" });
assert.strictEqual(slashSplit("compact"), null);
assert.strictEqual(slashSplit("/"), null);
assert.strictEqual(slashSplit("/ spaced"), null);
assert.strictEqual(slashFind(commands, "compact").name, "compact");
assert.strictEqual(slashFind(commands, "nope"), null);
// a path-like message is not a command of the bot, so the page sends it as a normal message
assert.strictEqual(slashFind(commands, slashSplit("/etc/hosts please").name), null);
''')


def test_row_labels():
    _run(LOGIC + COMMANDS + r'''
assert.strictEqual(slashDescription(commands[4]), "terminal only");
assert.strictEqual(slashDescription(commands[1]), "Summarize the conversation to free context");
assert.strictEqual(slashTag(commands[0]), "Bot Office");
assert.strictEqual(slashTag(commands[3]), "custom");
assert.strictEqual(slashTag(commands[1]), "");
''')


def test_page_wires_the_popup_and_a_confirm_for_the_destructive_command():
    page = CHAT_PAGE.read_text()
    assert 'id="slash-list" class="slash-list" role="listbox"' in page
    assert 'role="combobox" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="false" aria-controls="slash-list"' in page
    assert 'id="slash-confirm"' in page and 'askConfirm("Start a fresh session?"' in page
    assert "confirm: true" in page  # the server gate is only passed after the dialog's OK
    assert page.index("trySlash(bot, text)") < page.index('api("/v1/bots/" + bot.id + "/messages"')


def test_a_late_blur_does_not_close_a_list_opened_since():
    assert 'document.activeElement !== $("text")) closeSlash()' in CHAT_PAGE.read_text()
