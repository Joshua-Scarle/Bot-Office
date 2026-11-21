"""0.12.14: the "ghost" character shape was replaced by the "droplet"; stored ghost values keep working."""
import json
import re
import sqlite3

from botoffice import avatars
from botoffice.store import JobStore
from test_characters import H, PAGE, _node, _slice, client, mk  # noqa: F401


def test_ghost_is_not_a_current_shape_and_the_hash_never_yields_it():
    assert "ghost" not in avatars.CHAR_SHAPES and "droplet" in avatars.CHAR_SHAPES and len(avatars.CHAR_SHAPES) == 12
    shapes = {avatars.derived_character(f"id-{n}").split("|")[1] for n in range(3000)}
    assert shapes == set(avatars.CHAR_SHAPES) and "ghost" not in shapes


def test_derived_shapes_are_unchanged_except_where_ghost_was():
    old = ("square", "circle", "blob", "hexagon", "cloud", "pill", "star", "diamond", "squircle", "ghost", "shield", "triangle")
    import hashlib
    for n in range(500):
        key = f"k{n}"
        digest = hashlib.sha256(("botoffice-char:" + key).encode()).digest()
        was = old[int.from_bytes(digest[:4], "big") % 12]
        now = avatars.derived_character(key).split("|")[1]
        assert now == ("droplet" if was == "ghost" else was)


def test_server_maps_the_retired_name_and_rejects_unknown():
    assert avatars.character_seed("ghost", "teal") == "char|droplet|teal"
    assert avatars.character_seed("droplet", "teal") == "char|droplet|teal"
    assert avatars.character_seed("Ghost", "teal") is None and avatars.character_seed("ghost", "mauve") is None
    assert not avatars.valid_seed("char|droplet|teal")        # rollback-safe: 0.12.1 reads it as no image and shows the emoji
    assert avatars.migrate_character_seed("char|ghost|orange") == "char|droplet|orange"
    for same in ("char|droplet|orange", "char|star|red", "char|ghost|mauve", "char|ghost|", "char|ghost|red|x", "preset-3", None, "char|ghost"):
        assert avatars.migrate_character_seed(same) is None


def test_put_character_ghost_stores_droplet_and_catalog_lists_alias(client):
    bot = mk(client)
    r = client.put(f"/v1/bots/{bot['id']}/avatar/character", headers=H, json={"shape": "ghost", "color": "pink"}).json()
    assert r["avatar_seed"] == "char|droplet|pink" and r["avatar"] == "character"
    cat = client.get("/v1/avatar-characters", headers=H).json()
    assert "ghost" not in cat["shapes"] and cat["aliases"] == {"ghost": "droplet"}


def test_startup_migration_rekeys_ghost_keeps_color_is_idempotent_and_touches_nothing_else(settings):
    path = settings.data_dir / "agentdesk.sqlite3"
    store = JobStore(path)
    a, b, c, d = (store.create_bot(n, "i", "codex", None) for n in ("A", "B", "C", "D"))
    seeds = [(a, "char|ghost|yellow"), (b, "char|ghost|slate"), (c, "char|star|red"), (d, "char|ghost|mauve")]
    with sqlite3.connect(path) as db:
        for bot, seed in seeds:
            db.execute("UPDATE bots SET avatar_seed=? WHERE id=?", (seed, str(bot.id)))
        before = {str(x.id): x.model_dump(mode="json") for x in store.list_bots()}
    for _ in range(3):                                            # repeated restarts
        store = JobStore(path)
    assert store.get_bot(a.id).avatar_seed == "char|droplet|yellow" and store.get_bot(b.id).avatar_seed == "char|droplet|slate"
    assert store.get_bot(c.id).avatar_seed == "char|star|red" and store.get_bot(d.id).avatar_seed == "char|ghost|mauve"
    after = {str(x.id): x.model_dump(mode="json") for x in store.list_bots()}
    for key in before:
        changed = {k for k in before[key] if before[key][k] != after[key][k]}
        assert changed <= {"avatar_seed"}, changed
    with sqlite3.connect(path) as db:
        backup = json.loads(db.execute("SELECT value FROM meta WHERE key='avatars.ghost_backup'").fetchone()[0])
        assert backup == {str(a.id): "char|ghost|yellow", str(b.id): "char|ghost|slate"}
    JobStore(path)                                                # a no-op start leaves the backup as it was
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='avatars.ghost_backup'").fetchone()[0]) == backup
    # rollback to 0.12.12 and back: old code could store a ghost again; the next start re-keys it, keeping the first backup
    with sqlite3.connect(path) as db:
        db.execute("UPDATE bots SET avatar_seed='char|ghost|cyan' WHERE id=?", (str(a.id),))
    assert JobStore(path).get_bot(a.id).avatar_seed == "char|droplet|cyan"
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='avatars.ghost_backup'").fetchone()[0])[str(a.id)] == "char|ghost|yellow"


def test_page_draws_droplet_reads_ghost_as_droplet_and_has_no_ghost_body():
    shapes = json.loads(re.search(r"const CHAR_SHAPES = (\[[^\]]*\])", PAGE).group(1))
    assert shapes == list(avatars.CHAR_SHAPES)
    assert "GHOST_PATH" not in PAGE and 'shape === "ghost"' not in PAGE
    assert re.search(r"const CHAR_ALIASES = \{ghost: \"droplet\"\}", PAGE)
    code = _slice("const CHAR_SHAPES", "// the badge for emoji")
    out = _node('''
const assert = require("assert");
const state = {};
''' + code + '''
assert.deepEqual(parseChar("char|ghost|orange"), {shape: "droplet", color: "orange"});
assert.deepEqual(parseChar("ghost:teal"), {shape: "droplet", color: "teal"});
assert.deepEqual(parseChar("char|droplet|teal"), {shape: "droplet", color: "teal"});
assert.equal(parseChar("char|ghost|mauve"), null);
assert.equal(parseChar("char|constructor|red"), null);
assert.deepEqual(charOf({avatar_seed: "char|ghost|red"}), {shape: "droplet", color: "red"});
console.log("ok");
''')
    assert out.strip() == "ok"


def test_droplet_face_fits_inside_the_body_for_every_expression():
    """The eyes and the mouth stay inside the belly circle (centre 24,30 r 14) and the body stays inside the 48 box."""
    code = _slice("const CHAR_SHAPES", "// the badge for emoji")
    out = _node('''
const assert = require("assert");
const state = {};
''' + code + '''
const nums = DROPLET_PATH.match(/-?[\\d.]+/g).map(Number);
assert.equal(DROPLET_PATH.match(/C/g).length, 2);
const [cy, gap] = CHAR_FACE.droplet;
assert.ok(cy - 6 > 14 && cy + 8.6 < 42 && Math.hypot(gap + 3.5, 8.6 + cy - 30) < 14, "face");
assert.ok(Math.min(10, 24 - gap - 3.2) >= 10 && 24 + gap + 3.2 <= 38);
console.log("ok");
''')
    assert out.strip() == "ok"
