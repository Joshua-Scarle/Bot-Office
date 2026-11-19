"""0.12.2 character avatars: storage in avatar_seed, API validation, precedence, and the page's drawing and
status mapping (node runs the real page code)."""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from botoffice import avatars
from botoffice.api import CHAT_PAGE, create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}
PAGE = CHAT_PAGE.read_text()


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as c:
        yield c


def mk(client, name="C"):
    r = client.post("/v1/bots", headers=H, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def test_set_character_stores_seed_and_replaces_pattern(client):
    bot = mk(client)
    client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "preset-2"})
    r = client.put(f"/v1/bots/{bot['id']}/avatar/character", headers=H, json={"shape": "star", "color": "violet"})
    assert r.status_code == 200
    assert r.json()["avatar"] == "character" and r.json()["avatar_seed"] == "char|star|violet"
    assert client.get(f"/v1/bots/{bot['id']}/icon", headers=H).status_code == 404
    again = client.get(f"/v1/bots/{bot['id']}", headers=H).json()
    assert again["avatar_seed"] == "char|star|violet" and again["icon"] == bot["icon"]


@pytest.mark.parametrize("body", [{"shape": "star", "color": "mauve"}, {"shape": "dragon", "color": "blue"},
                                  {"shape": "../x", "color": "blue"}, {"shape": "star"}, {"shape": "star", "color": "blue", "x": 1}])
def test_set_character_rejects_unknown_names(client, body):
    bot = mk(client)
    before = client.get(f"/v1/bots/{bot['id']}", headers=H).json()["avatar_seed"]
    assert client.put(f"/v1/bots/{bot['id']}/avatar/character", headers=H, json=body).status_code in (400, 422)
    assert client.get(f"/v1/bots/{bot['id']}", headers=H).json()["avatar_seed"] == before


def test_character_needs_auth_and_a_real_bot(client):
    bot = mk(client)
    body = {"shape": "pill", "color": "teal"}
    assert client.put(f"/v1/bots/{bot['id']}/avatar/character", json=body).status_code == 401
    assert client.put("/v1/bots/00000000-0000-0000-0000-000000000000/avatar/character", headers=H, json=body).status_code == 404


def test_generate_and_preview_refuse_the_character_marker(client):
    bot = mk(client)
    assert client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "char|star|blue"}).status_code == 400
    assert client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "char:star:blue"}).status_code == 200   # plain text, a normal seed
    assert client.get("/v1/avatar-preview?seed=char%7Cstar%7Cblue", headers=H).status_code == 400


def test_upload_then_character_and_back_to_emoji(client):
    import io
    from PIL import Image
    bot = mk(client)
    out = io.BytesIO(); Image.new("RGB", (64, 64), (9, 9, 200)).save(out, format="PNG")
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=out.getvalue()).json()["avatar"] == "upload"
    r = client.put(f"/v1/bots/{bot['id']}/avatar/character", headers=H, json={"shape": "cloud", "color": "cyan"}).json()
    assert r["avatar"] == "character" and r["icon_image"] is None
    assert client.delete(f"/v1/bots/{bot['id']}/icon", headers=H).json()["avatar"] == "emoji"


def test_character_catalog_and_page_lists_agree(client):
    cat = client.get("/v1/avatar-characters", headers=H).json()
    assert cat["shapes"] == list(avatars.CHAR_SHAPES) and cat["colors"] == list(avatars.CHAR_COLORS)
    assert cat["expressions"] == list(avatars.CHAR_EXPRESSIONS)
    assert len(cat["shapes"]) * len(cat["colors"]) == 80
    shapes = re.search(r"const CHAR_SHAPES = (\[[^\]]*\])", PAGE).group(1)
    assert json.loads(shapes) == cat["shapes"]
    colors = re.search(r"const CHAR_COLORS = \{(.*?)\};", PAGE, re.S).group(1)
    assert re.findall(r"(\w+): \"#", colors) == cat["colors"]
    exprs = re.search(r"const EXPRESSIONS = (\[[^\]]*\])", PAGE).group(1)
    assert json.loads(exprs) == cat["expressions"]


def test_default_characters_are_distinct_and_stable():
    used = []
    for i in range(20):
        used.append(avatars.default_character(f"bot-{i}", used))
    assert len(set(used)) == 20 and all(avatars.is_character(s) for s in used)
    assert avatars.default_character("x", used) == avatars.default_character("x", used)


def test_rollback_safe_marker_is_not_a_generator_seed(client):
    # 0.12.1 serves /icon only for seeds matching SEED_RE and otherwise 404s, so the page shows the emoji
    assert not avatars.valid_seed("char|star|blue")
    bot = mk(client)
    client.put(f"/v1/bots/{bot['id']}/avatar/character", headers=H, json={"shape": "star", "color": "blue"})
    assert client.get(f"/v1/bots/{bot['id']}/icon", headers=H).status_code == 404


# ---------------------------------------------------------------- page logic (node)
def _slice(start: str, end: str) -> str:
    first = PAGE.index(start)
    return PAGE[first:PAGE.index(end, first)]


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_status_maps_to_expression_and_done_window():
    code = _slice("const CHAR_SHAPES", "// the badge for emoji")
    _node('''
const assert = require("assert");
const state = {};
const renderBots = () => {}, renderHeader = () => {};
''' + code + '''
const bot = (status, id = "b") => ({id, status});
assert.equal(botExpression(bot("idle")), "idle");
assert.equal(botExpression(bot(undefined)), "idle");
for (const s of ["working", "queued"]) assert.equal(botExpression(bot(s)), "working");
for (const s of ["waiting", "blocked"]) assert.equal(botExpression(bot(s)), "waiting");
assert.equal(botExpression(bot("error")), "error");
assert.equal(botExpression(bot("offline")), "sleeping");
// done: only after working/queued -> idle, only for DONE_MS, never over a non-idle status
noteStatuses([bot("idle")], [bot("idle")]);
assert.equal(botExpression(bot("idle")), "idle");
noteStatuses([bot("working")], [bot("idle")]);
assert.equal(botExpression(bot("idle")), "done");
assert.equal(botExpression(bot("error")), "error");
state.doneUntil.set("b", Date.now() - 1);
assert.equal(botExpression(bot("idle")), "idle");
noteStatuses([bot("queued")], [bot("idle")]);
noteStatuses([bot("idle")], [bot("working")]);
assert.equal(botExpression(bot("idle")), "idle");      // working again cleared the done mark
noteStatuses([], [bot("idle", "new")]);
assert.equal(botExpression(bot("idle", "new")), "idle");
clearTimeout(state.doneTimer);
''')


def test_character_parsing_accepts_only_known_names():
    code = _slice("const CHAR_SHAPES", "// the badge for emoji")
    _node('''
const assert = require("assert");
const state = {};
''' + code + '''
assert.deepEqual(parseChar("char|star|violet"), {shape: "star", color: "violet"});
assert.deepEqual(parseChar("pill:teal"), {shape: "pill", color: "teal"});
for (const bad of ["pill:constructor", "char|star|toString", "char|star|mauve", "char|dragon|blue", "preset-3", "", null, undefined, "char|star", "char|a|b|c", "char|Star|blue", "char:star:blue"]) assert.equal(parseChar(bad), null, String(bad));
assert.equal(charOf({avatar_seed: "char|star|blue"}).shape, "star");
assert.equal(charOf({avatar_seed: "char:star:blue"}), null);                              // not the stored form
assert.equal(charSeed({shape: "star", color: "blue"}), "char|star|blue");
assert.equal(charOf({avatar_seed: "char|star|blue", icon_image: "image/png"}), null);   // an upload wins
assert.equal(charOf({avatar_seed: "preset-1"}), null);
assert.equal(charOf({}), null);
''')


def test_every_shape_color_expression_draws_inside_the_viewbox_with_eyes():
    """A tiny DOM stub is enough: every combination must build a body and a face without throwing."""
    code = _slice("const CHAR_SHAPES", "// the badge for emoji")
    out = _node('''
const assert = require("assert");
const state = {};
function Node(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.dataset = {}; }
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Node.prototype.append = function (...k) { this.kids.push(...k); };
const document = { createElementNS: (ns, tag) => new Node(tag) };
''' + code + '''
let n = 0;
const count = (node, pred) => (pred(node) ? 1 : 0) + node.kids.reduce((a, k) => a + count(k, pred), 0);
for (const shape of CHAR_SHAPES) for (const color of Object.keys(CHAR_COLORS)) for (const expr of EXPRESSIONS) {
  const svg = charSvg(shape, color, expr);
  assert.equal(svg.attrs.viewBox, "0 0 48 48");
  assert.ok(svg.attrs.class.includes(expr) && svg.attrs["aria-label"] && svg.attrs.role === "img");
  assert.equal(count(svg, (x) => x.attrs.class === "body"), 1);
  // two eye marks in every expression (rect/circle eyes or two path marks)
  const eyes = count(svg, (x) => x.attrs.class === "eye");
  const marks = count(svg, (x) => x.tag === "path" && x.attrs.class === "mark");
  assert.ok(eyes === 2 || marks >= 2, shape + color + expr);
  n++;
}
console.log(n);
''')
    assert out.strip() == str(8 * 10 * 6)


def test_picker_is_a_keyboard_radio_group():
    assert "ArrowRight" in PAGE and "b.tabIndex = b.dataset.kind === state.kind" in PAGE


def test_markup_and_css_for_characters():
    assert 'id="f-kind"' in PAGE and 'role="radiogroup"' in PAGE
    for kind in ("emoji", "character", "upload", "pattern"):
        assert f'data-kind="{kind}"' in PAGE and f'data-panel="{kind}"' in PAGE
    assert 'id="f-char-shapes"' in PAGE and 'id="f-char-colors"' in PAGE and 'id="head-dot"' in PAGE
    # motion only when the user has not asked for less
    css = PAGE[PAGE.index(".avatar.is-char"):PAGE.index(".avatar.lg {")]
    assert "@media (prefers-reduced-motion: no-preference)" in css
    assert css.count("animation:") == 2 and css.index("@media (prefers-reduced-motion: no-preference)") < css.index("animation:")
    assert "<img" not in css and "innerHTML" not in _slice("function charSvg", "function parseChar")


def test_characters_are_ours_no_third_party_assets():
    block = _slice("/* ---- character avatars", "function fillAvatar")
    assert "http" not in block.replace('"http:" + "//www.w3.org/2000/svg"', "") and "data:" not in block and "base64" not in block
