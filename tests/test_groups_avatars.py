"""0.12.0: sidebar sections (groups) and bot avatars: store, API, generator and upload validation."""

import io
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from botoffice import avatars
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as c:
        yield c


def mk(client, name):
    r = client.post("/v1/bots", headers=H, json={"name": name, "group": "Ungrouped"})   # 0.12.20: new bots are auto-grouped; these tests start ungrouped
    assert r.status_code == 201, r.text
    return r.json()


def mk_pattern(client, name):
    """A bot with a generated pattern avatar (0.12.2: new bots start with a character)."""
    bot = mk(client, name)
    r = client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "preset-3"})
    assert r.status_code == 200 and r.json()["avatar"] == "generated"
    return r.json()


def layout(client):
    r = client.get("/v1/sections", headers=H)
    assert r.status_code == 200
    return r.json()


def image_bytes(fmt="PNG", size=(300, 200), mode="RGB", color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format=fmt)
    return out.getvalue()


# ---------------------------------------------------------------- sections
def test_fresh_install_has_no_sections_and_all_bots_ungrouped(client):
    a, b = mk(client, "Alpha"), mk(client, "Beta")
    lay = layout(client)
    assert lay["sections"] == []
    assert lay["ungrouped"] == {"collapsed": False, "bot_ids": [a["id"], b["id"]]}


def test_section_crud_names_and_limits(client):
    r = client.post("/v1/sections", headers=H, json={"name": "  Work   stuff "})
    assert r.status_code == 201 and r.json()["name"] == "Work stuff" and r.json()["collapsed"] is False
    sid = r.json()["id"]
    assert client.post("/v1/sections", headers=H, json={"name": "work STUFF"}).status_code == 409
    assert client.post("/v1/sections", headers=H, json={"name": "Ungrouped"}).status_code == 409
    for bad in ("", "   ", "x" * 61, "a\x01b"):
        assert client.post("/v1/sections", headers=H, json={"name": bad}).status_code == 400
    assert client.patch(f"/v1/sections/{sid}", headers=H, json={"name": "Play"}).json()["name"] == "Play"
    other = client.post("/v1/sections", headers=H, json={"name": "Other"}).json()["id"]
    assert client.patch(f"/v1/sections/{other}", headers=H, json={"name": "play"}).status_code == 409
    assert client.patch("/v1/sections/nope", headers=H, json={"name": "x"}).status_code == 404
    assert client.patch(f"/v1/sections/{sid}", headers=H, json={"junk": 1}).status_code == 422


def test_collapsed_state_persists_including_ungrouped(client):
    sid = client.post("/v1/sections", headers=H, json={"name": "A"}).json()["id"]
    assert client.patch(f"/v1/sections/{sid}", headers=H, json={"collapsed": True}).json()["collapsed"] is True
    assert client.patch("/v1/sections/ungrouped", headers=H, json={"collapsed": True}).json()["collapsed"] is True
    lay = layout(client)
    assert lay["sections"][0]["collapsed"] is True and lay["ungrouped"]["collapsed"] is True
    assert client.patch("/v1/sections/ungrouped", headers=H, json={"name": "x"}).status_code == 400
    client.patch(f"/v1/sections/{sid}", headers=H, json={"collapsed": False})
    assert layout(client)["sections"][0]["collapsed"] is False


def test_move_bots_between_sections_and_reorder(client):
    a, b, c = (mk(client, n)["id"] for n in ("A", "B", "C"))
    s1 = client.post("/v1/sections", headers=H, json={"name": "One"}).json()["id"]
    s2 = client.post("/v1/sections", headers=H, json={"name": "Two"}).json()["id"]
    move = lambda bot, sec, i: client.put(f"/v1/bots/{bot}/section", headers=H, json={"section_id": sec, "index": i})
    assert move(a, s1, 0).status_code == 200
    assert move(b, s1, 1).status_code == 200
    assert move(c, s1, 0).status_code == 200          # insert at the top
    lay = layout(client)
    assert lay["sections"][0]["bot_ids"] == [c, a, b] and lay["ungrouped"]["bot_ids"] == []
    move(a, s2, 99)                                    # index clamps
    lay = layout(client)
    assert lay["sections"][0]["bot_ids"] == [c, b] and lay["sections"][1]["bot_ids"] == [a]
    move(b, s1, 0)                                     # reorder within a section
    assert layout(client)["sections"][0]["bot_ids"] == [b, c]
    move(c, None, 0)                                   # back to Ungrouped
    lay = layout(client)
    assert lay["ungrouped"]["bot_ids"] == [c] and lay["sections"][0]["bot_ids"] == [b]
    assert move(c, "nope", 0).status_code == 404
    assert move("00000000-0000-0000-0000-000000000000", s1, 0).status_code == 404


def test_ungrouped_reorder_and_new_bots_append(client):
    a, b, c = (mk(client, n)["id"] for n in ("A", "B", "C"))
    client.put(f"/v1/bots/{c}/section", headers=H, json={"section_id": None, "index": 0})
    assert layout(client)["ungrouped"]["bot_ids"] == [c, a, b]
    d = mk(client, "D")["id"]
    assert layout(client)["ungrouped"]["bot_ids"] == [c, a, b, d]


def test_reorder_sections_needs_a_permutation(client):
    ids = [client.post("/v1/sections", headers=H, json={"name": n}).json()["id"] for n in "ABC"]
    r = client.put("/v1/sections/order", headers=H, json={"ids": [ids[2], ids[0], ids[1]]})
    assert [s["name"] for s in r.json()["sections"]] == ["C", "A", "B"]
    assert [s["position"] for s in r.json()["sections"]] == [0, 1, 2]
    assert client.put("/v1/sections/order", headers=H, json={"ids": ids[:2]}).status_code == 409
    assert client.put("/v1/sections/order", headers=H, json={"ids": [ids[0]] * 3}).status_code == 409
    new = client.post("/v1/sections", headers=H, json={"name": "D"}).json()
    assert new["position"] == 3


def test_delete_section_moves_bots_to_top_of_ungrouped_and_keeps_them(client):
    a, b, c = (mk(client, n)["id"] for n in ("A", "B", "C"))
    s1 = client.post("/v1/sections", headers=H, json={"name": "One"}).json()["id"]
    s2 = client.post("/v1/sections", headers=H, json={"name": "Two"}).json()["id"]
    for bot, i in ((b, 0), (c, 1)):
        client.put(f"/v1/bots/{bot}/section", headers=H, json={"section_id": s1, "index": i})
    r = client.delete(f"/v1/sections/{s1}", headers=H)
    assert r.status_code == 200 and r.json()["moved_bot_ids"] == [b, c]
    lay = layout(client)
    assert [s["id"] for s in lay["sections"]] == [s2] and lay["sections"][0]["position"] == 0
    assert lay["ungrouped"]["bot_ids"] == [b, c, a]
    assert {x["id"] for x in client.get("/v1/bots", headers=H).json()} == {a, b, c}
    assert client.delete(f"/v1/sections/{s1}", headers=H).status_code == 404


def test_deleting_a_bot_cleans_its_membership(client, settings):
    a, b = mk(client, "A")["id"], mk(client, "B")["id"]
    s = client.post("/v1/sections", headers=H, json={"name": "One"}).json()["id"]
    client.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": s, "index": 0})
    assert client.delete(f"/v1/bots/{a}", headers=H).status_code in (200, 204)
    assert layout(client)["sections"][0]["bot_ids"] == []
    db = sqlite3.connect(settings.data_dir / "agentdesk.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM bot_section_members WHERE bot_id=?", (a,)).fetchone()[0] == 0
    assert b in layout(client)["ungrouped"]["bot_ids"]


def test_sections_need_the_master_token(client):
    assert client.get("/v1/sections").status_code == 401
    assert client.post("/v1/sections", json={"name": "x"}).status_code == 401
    assert client.get("/v1/sections", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_schema_is_additive_and_idempotent(settings):
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Old", "i", "codex", None)
    sid = store.sections.create("Keep")["id"]
    store.sections.move_bot(str(bot.id), sid, 0)
    again = JobStore(settings.data_dir / "agentdesk.sqlite3")      # a restart re-runs every migration
    assert again.sections.layout()["sections"][0]["bot_ids"] == [str(bot.id)]
    assert again.get_bot(bot.id).avatar_seed == bot.avatar_seed


# ---------------------------------------------------------------- avatars
def test_generator_is_deterministic_valid_png_and_presets_differ():
    one = avatars.render_png("preset-3")
    assert one == avatars.render_png("preset-3") and one != avatars.render_png("preset-4")
    img = Image.open(io.BytesIO(one))
    assert img.format == "PNG" and img.size == (256, 256)
    assert len({avatars.render_png(s) for s in avatars.PRESET_SEEDS}) == avatars.PRESET_COUNT
    assert avatars.render_png("my-own-seed") != avatars.render_png("my-own-seed2")
    with pytest.raises(ValueError):
        avatars.render_png("../../etc")


def test_new_bots_get_a_default_character_and_keep_their_emoji(client):
    bot = client.post("/v1/bots", headers=H, json={"name": "Pic", "icon": "🦊"}).json()
    assert bot["avatar"] == "character" and avatars.is_character(bot["avatar_seed"]) and bot["icon"] == "🦊"
    shape, color = bot["avatar_seed"].split("|")[1:]
    assert shape in avatars.CHAR_SHAPES and color in avatars.CHAR_COLORS
    assert client.get(f"/v1/bots/{bot['id']}/icon", headers=H).status_code == 404   # drawn in the browser, not fetched


def test_generated_pattern_still_serves_a_png(client):
    bot = mk_pattern(client, "Pat")
    r = client.get(f"/v1/bots/{bot['id']}/icon", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (256, 256)


def test_icon_caching_etag_and_versioned_urls(client):
    bot = mk_pattern(client, "Cache")
    r = client.get(f"/v1/bots/{bot['id']}/icon", headers=H)
    assert r.headers["cache-control"] == "private, no-cache" and r.headers["etag"]
    again = client.get(f"/v1/bots/{bot['id']}/icon", headers={**H, "If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and not again.content
    v = client.get(f"/v1/bots/{bot['id']}/icon?v=1", headers=H)
    assert "max-age=86400" in v.headers["cache-control"]
    assert client.get(f"/v1/bots/{bot['id']}/icon").status_code == 401


def test_migration_assigns_default_avatars_once_and_never_touches_uploads(settings):
    path = settings.data_dir / "agentdesk.sqlite3"
    store = JobStore(path)
    plain, uploaded = store.create_bot("Plain", "i", "codex", None), store.create_bot("Up", "i", "codex", None)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE bots SET avatar_seed=NULL")
        db.execute("UPDATE bots SET icon_image='image/png' WHERE id=?", (str(uploaded.id),))
        db.execute("DELETE FROM meta WHERE key='avatars.default_assigned'")
    store = JobStore(path)
    assert store.get_bot(plain.id).avatar_seed in avatars.PRESET_SEEDS and store.get_bot(plain.id).icon == plain.icon
    assert store.get_bot(uploaded.id).avatar_seed is None
    store.update_bot(plain.id, avatar_seed=None)                    # chose the emoji: it stays chosen
    assert JobStore(path).get_bot(plain.id).avatar_seed is None


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGB"), ("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGB"), ("GIF", "P")])
def test_upload_is_resized_cropped_and_stored_as_png(client, fmt, mode):
    bot = mk(client, "Up" + fmt + mode)
    r = client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes(fmt, (900, 300), mode, 128 if mode == "P" else (9, 99, 199)))
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == "upload" and r.json()["icon_image"] == "image/png"
    got = client.get(f"/v1/bots/{bot['id']}/icon", headers=H)
    img = Image.open(io.BytesIO(got.content))
    assert img.format == "PNG" and img.size == (256, 256) and got.headers["content-type"] == "image/png"


@pytest.mark.parametrize("payload,status", [
    (b"", 400),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 415),
    (b"%PDF-1.4 hello", 415),
    (b"just text", 415),
    (image_bytes("PNG")[:60], 415),                                  # truncated
    (image_bytes("PNG") + b"<script>", 200),                         # trailing junk is dropped on re-encode
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, 415),                      # magic only
    (b"x" * (2 * 1024 * 1024 + 1), 413),
])
def test_upload_validation(client, payload, status):
    bot = mk_pattern(client, "V")
    r = client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=payload)
    assert r.status_code == status, r.text
    if status != 200:
        assert client.get(f"/v1/bots/{bot['id']}", headers=H).json()["avatar"] == "generated"   # unchanged


def test_upload_pixel_bomb_is_refused(client, monkeypatch):
    monkeypatch.setattr(avatars, "MAX_UPLOAD_PIXELS", 1000)
    bot = mk(client, "Bomb")
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes("PNG", (100, 100))).status_code == 413


def test_upload_output_drops_metadata_and_overwrites_cleanly(client, settings):
    bot = mk(client, "Meta")
    img = Image.new("RGB", (64, 64), (1, 2, 3))
    out = io.BytesIO()
    exif = Image.Exif()
    exif[0x010E] = "SECRET-DESCRIPTION"
    img.save(out, format="JPEG", exif=exif)
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=out.getvalue()).status_code == 200
    stored = client.get(f"/v1/bots/{bot['id']}/icon", headers=H).content
    assert b"SECRET-DESCRIPTION" not in stored
    # a second upload replaces the first; the file is mode 600
    client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes("PNG", (32, 32)))
    path = settings.data_dir / "bots" / bot["id"] / "icon"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_generate_replaces_upload_and_remove_falls_back_to_emoji(client, settings):
    bot = mk(client, "Gen")
    client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes())
    path = settings.data_dir / "bots" / bot["id"] / "icon"
    assert path.exists()
    r = client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "preset-7"})
    assert r.status_code == 200 and r.json()["avatar"] == "generated" and r.json()["avatar_seed"] == "preset-7"
    assert r.json()["icon_image"] is None and not path.exists()
    r = client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H)         # no body: a random seed
    assert r.json()["avatar_seed"] not in (None, "preset-7")
    assert client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "../x"}).status_code == 400
    r = client.delete(f"/v1/bots/{bot['id']}/icon", headers=H)
    assert r.json()["avatar"] == "emoji" and r.json()["icon"]
    assert client.get(f"/v1/bots/{bot['id']}/icon", headers=H).status_code == 404


def test_presets_and_preview(client):
    presets = client.get("/v1/avatar-presets", headers=H).json()["presets"]
    assert [p["seed"] for p in presets] == list(avatars.PRESET_SEEDS)
    r = client.get("/v1/avatar-preview?seed=preset-2", headers=H)
    assert r.status_code == 200 and r.content == avatars.render_png("preset-2")
    assert client.get("/v1/avatar-preview?seed=a/b", headers=H).status_code == 400
    assert client.get("/v1/avatar-preview?seed=preset-2").status_code == 401


def test_icon_routes_reject_a_bots_own_agent_token(client):
    # job-scoped agent tokens are not the master token
    assert client.put("/v1/bots/00000000-0000-0000-0000-000000000000/icon", headers={"Authorization": "Bearer agent"}, content=b"x").status_code == 401


def test_upload_without_pillow_is_a_clear_503_and_generation_still_works(client, monkeypatch):
    import sys
    payload = image_bytes()
    monkeypatch.setitem(sys.modules, "PIL", None)
    bot = mk_pattern(client, "NoPil")
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=payload).status_code == 503
    assert client.get(f"/v1/bots/{bot['id']}/icon", headers=H).status_code == 200


def test_default_avatars_spread_over_the_presets():
    used = []
    for i in range(avatars.PRESET_COUNT):
        used.append(avatars.default_seed(f"bot-{i}", used))
    assert sorted(used) == sorted(avatars.PRESET_SEEDS)          # twelve bots, twelve different avatars
    assert avatars.default_seed("x", used) == avatars.default_seed("x", used)


def test_seed_with_a_trailing_newline_is_refused(client):
    assert not avatars.valid_seed("abc\n")
    bot = mk(client, "NL")
    assert client.post(f"/v1/bots/{bot['id']}/icon/generate", headers=H, json={"seed": "abc\n"}).status_code == 400


def test_presets_are_kept_and_etag_names_the_generator():
    avatars.render_png("preset-1")
    assert "preset-1" in avatars._PRESET_PNGS
    assert avatars.etag(b"x") == avatars.etag(b"x") and avatars.etag(b"x") != avatars.etag(b"y")


def test_jpeg_upload_uses_reduced_decode_and_still_yields_256(client):
    bot = mk(client, "Big")
    r = client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes("JPEG", (3000, 2000)))
    assert r.status_code == 200
    assert Image.open(io.BytesIO(client.get(f"/v1/bots/{bot['id']}/icon", headers=H).content)).size == (256, 256)


def test_phone_sized_jpeg_is_accepted_but_a_huge_png_is_not(client, monkeypatch):
    bot = mk(client, "Phone")
    monkeypatch.setattr(avatars, "MAX_UPLOAD_PIXELS", 2_000_000)   # the full-size limit; JPEG has its own, larger one
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes("JPEG", (3000, 2000))).status_code == 200
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes("PNG", (3000, 2000))).status_code == 413


def test_very_wide_jpeg_is_refused_after_the_reduced_decode(client, monkeypatch):
    bot = mk(client, "Wide")
    monkeypatch.setattr(avatars, "MAX_UPLOAD_PIXELS", 100_000)
    assert client.put(f"/v1/bots/{bot['id']}/icon", headers=H, content=image_bytes("JPEG", (6000, 300))).status_code == 413


# ---------------------------------------------------------------- Hidden (0.12.11)
def hidden(client):
    return layout(client)["hidden"]


def test_hidden_section_always_exists_collapsed_and_is_not_a_regular_section(client):
    lay = layout(client)
    assert lay["hidden"] == {"id": "hidden", "name": "Hidden", "builtin": True, "collapsed": True, "bot_ids": []}
    assert lay["sections"] == []                                    # not in the reorderable list
    assert client.patch("/v1/sections/hidden", headers=H, json={"collapsed": False}).json()["collapsed"] is False
    assert hidden(client)["collapsed"] is False


def test_hidden_cannot_be_renamed_deleted_or_reordered_and_its_name_is_reserved(client):
    assert client.patch("/v1/sections/hidden", headers=H, json={"name": "Nope"}).status_code == 409
    assert client.delete("/v1/sections/hidden", headers=H).status_code == 409
    for name in ("Hidden", "hidden", "HIDDEN", " hidden ", "\uff28idden", "Hid\u200bden", "Hidden\u034f", "Hidden\ufe0f", "Ungrouped\u034f", "Hi\u0301dden", "H\u0130DDEN", "Hidden\u0345", "Hidden\u093e", "Hidden\u3164", "Hidden\u2800", "\u202eHidden\u202c", "Hidden\u00a0", "\uff28\uff29DDEN"):
        assert client.post("/v1/sections", headers=H, json={"name": name}).status_code == 409, name
    other = client.post("/v1/sections", headers=H, json={"name": "Work"}).json()["id"]
    assert client.patch(f"/v1/sections/{other}", headers=H, json={"name": "HiDdEn"}).status_code == 409
    assert client.put("/v1/sections/order", headers=H, json={"ids": ["hidden", other]}).status_code == 200   # ignored, not an error
    assert [s["id"] for s in layout(client)["sections"]] == [other] and hidden(client)["name"] == "Hidden"
    assert client.post("/v1/sections", headers=H, json={"name": "Hidden stuff"}).status_code == 201


def test_hide_and_unhide_a_bot_through_the_move_route_and_survive_a_restart(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as c:
        a, b = mk(c, "A")["id"], mk(c, "B")["id"]
        r = c.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": "hidden", "index": 0})
        assert r.status_code == 200 and r.json()["hidden"]["bot_ids"] == [a]
        assert a not in r.json()["ungrouped"]["bot_ids"] and b in r.json()["ungrouped"]["bot_ids"]
        c.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": "hidden", "index": 0})   # twice: no duplicate
        assert hidden(c)["bot_ids"] == [a]
        c.patch("/v1/sections/hidden", headers=H, json={"collapsed": False})
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as c:                    # restart
        lay = layout(c)
        assert lay["hidden"]["bot_ids"] == [a] and lay["hidden"]["collapsed"] is False and lay["sections"] == []
        assert c.get("/v1/bots", headers=H).json() and {x["id"] for x in c.get("/v1/bots", headers=H).json()} >= {a, b}
        r = c.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": None, "index": 0})
        assert r.json()["hidden"]["bot_ids"] == [] and a in r.json()["ungrouped"]["bot_ids"]


def test_hidden_membership_moves_between_sections_and_hidden_and_a_deleted_section_keeps_hidden_bots(client):
    a, b = mk(client, "A")["id"], mk(client, "B")["id"]
    s = client.post("/v1/sections", headers=H, json={"name": "Work"}).json()["id"]
    client.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": s, "index": 0})
    client.put(f"/v1/bots/{b}/section", headers=H, json={"section_id": s, "index": 1})
    client.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": "hidden", "index": 0})
    lay = layout(client)
    assert lay["sections"][0]["bot_ids"] == [b] and lay["hidden"]["bot_ids"] == [a]
    assert client.delete(f"/v1/sections/{s}", headers=H).status_code == 200
    lay = layout(client)
    assert lay["hidden"]["bot_ids"] == [a] and lay["ungrouped"]["bot_ids"] == [b]


def test_seed_hidden_in_one_call_replaces_the_set_and_validates(client):
    a, b, c = (mk(client, n)["id"] for n in "ABC")
    r = client.put("/v1/sections/hidden/members", headers=H, json={"bot_ids": [a, b, a]})
    assert r.status_code == 200 and r.json()["hidden"]["bot_ids"] == [a, b]
    assert c in r.json()["ungrouped"]["bot_ids"] and a not in r.json()["ungrouped"]["bot_ids"]
    r = client.put("/v1/sections/hidden/members", headers=H, json={"bot_ids": [b]})
    assert r.json()["hidden"]["bot_ids"] == [b] and a in r.json()["ungrouped"]["bot_ids"]
    bad = client.put("/v1/sections/hidden/members", headers=H, json={"bot_ids": [b, "00000000-0000-0000-0000-000000000000"]})
    assert bad.status_code == 404 and hidden(client)["bot_ids"] == [b]                       # all or nothing
    assert client.put("/v1/sections/hidden/members", headers=H, json={"bot_ids": ["nope"]}).status_code == 422
    assert client.put("/v1/sections/hidden/members", json={"bot_ids": []}).status_code == 401
    assert client.put("/v1/sections/hidden/members", headers=H, json={"bot_ids": []}).json()["hidden"]["bot_ids"] == []


def test_deleting_a_hidden_bot_cleans_its_membership(client):
    a = mk(client, "A")["id"]
    client.put(f"/v1/bots/{a}/section", headers=H, json={"section_id": "hidden", "index": 0})
    assert client.delete(f"/v1/bots/{a}", headers=H).status_code == 204
    assert hidden(client)["bot_ids"] == []


def test_hidden_startup_is_idempotent_repairs_the_name_and_moves_a_clashing_section(settings):
    path = settings.data_dir / "agentdesk.sqlite3"
    store = JobStore(path)
    with sqlite3.connect(path) as db:                               # older code could rename or delete it, a user could have made "Hidden"
        db.execute("DELETE FROM bot_sections WHERE id='hidden'")
        db.execute("INSERT INTO bot_sections (id, name, position, collapsed, created_at) VALUES ('mine','Hidden',0,0,'x')")
    for _ in range(3):
        again = JobStore(path)
    lay = again.sections.layout()
    assert lay["hidden"]["name"] == "Hidden" and lay["hidden"]["collapsed"] is True
    assert [s["name"] for s in lay["sections"]] == ["Hidden (old)"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE bot_sections SET name='Renamed', collapsed=0 WHERE id='hidden'")
    assert JobStore(path).sections.layout()["hidden"]["name"] == "Hidden"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM bot_sections WHERE id='hidden'").fetchone()[0] == 1
    del store


def test_a_hidden_bot_still_runs_its_jobs_and_is_listed_everywhere(settings):
    """Hiding is only sidebar placement: the scheduler's claim, the bot list and the bot's own routes ignore it."""
    from botoffice.scheduler import Scheduler
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Quiet", "", "codex", None)
    store.sections.move_bot(str(bot.id), "hidden", 0)
    assert store.sections.layout()["hidden"]["bot_ids"] == [str(bot.id)]
    scheduler.turns.post_message(bot, "routine work", source="routine")
    job = store.claim(["desktop-1"], {"codex"})
    assert job is not None and str(job.bot_id) == str(bot.id)
    assert str(bot.id) in {str(b.id) for b in store.list_bots()}
    import inspect
    from botoffice import store as store_mod
    src = inspect.getsource(store_mod)
    assert src.count("bot_section_members") <= 2 and "sections." not in inspect.getsource(Scheduler)   # nothing schedules by section


# ---------------------------------------------------------------- characters replace patterns (0.12.11)
def test_derived_character_is_stable_valid_and_spreads():
    ids = [f"00000000-0000-4000-8000-{n:012d}" for n in range(200)]
    seeds = [avatars.derived_character(i) for i in ids]
    assert seeds == [avatars.derived_character(i) for i in ids]
    assert all(avatars.character_seed(*s[len(avatars.CHAR_PREFIX):].split("|")) == s for s in seeds)
    assert len(set(seeds)) > 60 and {s.split("|")[1] for s in seeds} == set(avatars.CHAR_SHAPES)


def test_migration_turns_patterns_into_derived_characters_once_and_keeps_a_backup(settings):
    import json
    path = settings.data_dir / "agentdesk.sqlite3"
    store = JobStore(path)
    pat, up, emoji, char = (store.create_bot(n, "i", "codex", None) for n in ("Pat", "Up", "Emoji", "Char"))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE bots SET avatar_seed='preset-4' WHERE id=?", (str(pat.id),))
        db.execute("UPDATE bots SET avatar_seed='preset-5', icon_image='image/png' WHERE id=?", (str(up.id),))
        db.execute("UPDATE bots SET avatar_seed=NULL WHERE id=?", (str(emoji.id),))
        db.execute("UPDATE bots SET avatar_seed='char|star|red' WHERE id=?", (str(char.id),))
        db.execute("DELETE FROM meta WHERE key='avatars.characters_default'")
    store = JobStore(path)
    assert store.get_bot(pat.id).avatar_seed == avatars.derived_character(str(pat.id))
    assert store.get_bot(pat.id).avatar == "character" and store.get_bot(pat.id).icon == pat.icon
    assert store.get_bot(up.id).avatar_seed == "preset-5" and store.get_bot(emoji.id).avatar_seed is None
    assert store.get_bot(char.id).avatar_seed == "char|star|red"
    with sqlite3.connect(path) as db:
        backup = json.loads(db.execute("SELECT value FROM meta WHERE key='avatars.pattern_backup'").fetchone()[0])
    assert backup == {str(pat.id): "preset-4"}
    store.update_bot(pat.id, avatar_seed="preset-9")                # an explicit later choice is not overwritten again
    assert JobStore(path).get_bot(pat.id).avatar_seed == "preset-9"


def test_character_seed_is_not_a_usable_generator_seed_so_old_code_falls_back_to_the_emoji():
    assert not avatars.valid_seed(avatars.derived_character("x"))


def test_reserved_name_comparison_does_not_fold_lookalike_letters_by_design():
    """Documented limit: only case, width, invisible characters and combining marks are folded. A lookalike is a plain
    user section; the built-in Hidden section is addressed by its fixed id, so it cannot be taken over or shadowed."""
    from botoffice import sections
    assert sections.reserved_key("H\u026aDDEN") != "hidden" and sections.reserved_key("\u041didden") != "hidden"
    assert sections.reserved_key("\uff28\u1d35dden \u200f") == "hidden"
