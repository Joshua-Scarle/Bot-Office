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
    r = client.post("/v1/bots", headers=H, json={"name": name})
    assert r.status_code == 201, r.text
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
    move = lambda bot, sec, i: client.put(f"/v1/bots/{bot}/section", headers=H, json={"section_id": sec, "index": i})  # noqa: E731
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


def test_new_bots_get_a_default_generated_avatar_and_keep_their_emoji(client):
    bot = client.post("/v1/bots", headers=H, json={"name": "Pic", "icon": "🦊"}).json()
    assert bot["avatar"] == "generated" and bot["avatar_seed"] in avatars.PRESET_SEEDS and bot["icon"] == "🦊"
    r = client.get(f"/v1/bots/{bot['id']}/icon", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (256, 256)


def test_icon_caching_etag_and_versioned_urls(client):
    bot = mk(client, "Cache")
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
    bot = mk(client, "V")
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
    bot = mk(client, "NoPil")
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
