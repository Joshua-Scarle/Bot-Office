"""0.11.13: chat uploads of any file type (stored in uploads/, never executed, served as downloads)."""
import os

import pytest
from fastapi.testclient import TestClient

from botoffice import attachments
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}
PNG = b"\x89PNG\r\n\x1a\nimage"


@pytest.fixture
def client_bot(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "files"}).json()["id"]
        yield client, bot, settings


def up(client, bot, name, data, **kw):
    return client.post(f"/v1/bots/{bot}/attachments", params={"name": name}, headers=H, content=data, **kw)


@pytest.mark.parametrize("name,data", [
    ("report.pdf", b"%PDF-1.7 body"), ("data.csv", b"a,b\n1,2\n"), ("part.stl", b"solid x\nendsolid"),
    ("part.step", b"ISO-10303-21;"), ("print.gcode", b"M104 S200\n; sliced"), ("bundle.zip", b"PK\x03\x04zip"),
    ("run.sh", b"#!/bin/sh\nrm -rf /\n"), ("x.svg", b"<svg onload=alert(1)>"),
])
def test_any_file_is_stored_safely_and_served_as_a_download(client_bot, name, data):
    client, bot, settings = client_bot
    response = up(client, bot, name, data)
    assert response.status_code == 201
    info = response.json()
    assert info["kind"] == "file" and info["size"] == len(data) and info["name"] == name
    path = settings.data_dir / "bots" / bot / "uploads" / info["id"]
    assert path.read_bytes() == data
    assert path.stat().st_mode & 0o777 == 0o400          # no exec bit, no write
    assert path.parent.stat().st_mode & 0o777 == 0o700
    got = client.get(info["url"], headers=H)
    assert got.content == data
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]
    message = client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "see file", "attachments": [info["id"]]})
    assert message.status_code == 201
    assert client.get(f"/v1/bots/{bot}/messages", headers=H).json()[0]["attachments"] == [info["id"]]


@pytest.mark.parametrize("raw", ["../../etc/passwd", "..\\..\\evil.sh", "/abs/path/x.txt", "a/b/c.txt", "..", ".", "",
                                 ".hidden", "nul\x00.txt", "sp ace & $(rm).txt", "é漢字.txt", "x" * 400 + ".pdf"])
def test_names_are_sanitized_and_cannot_traverse(client_bot, raw):
    client, bot, settings = client_bot
    response = up(client, bot, raw or "x", b"data") if raw else client.post(
        f"/v1/bots/{bot}/attachments", params={"name": ""}, headers=H, content=b"data")
    if response.status_code == 415:  # an empty name is the image-only form
        return
    assert response.status_code == 201
    stored = response.json()["id"]
    assert attachments.FILE_ID_RE.fullmatch(stored) and "/" not in stored and ".." not in stored.replace("...", "")
    folder = settings.data_dir / "bots" / bot / "uploads"
    assert [p.name for p in folder.iterdir()] == [stored]
    assert not (settings.data_dir / "etc").exists()


def test_same_name_twice_never_overwrites(client_bot):
    client, bot, settings = client_bot
    a = up(client, bot, "same.txt", b"one").json()["id"]
    b = up(client, bot, "same.txt", b"two").json()["id"]
    assert a != b
    folder = settings.data_dir / "bots" / bot / "uploads"
    assert (folder / a).read_bytes() == b"one" and (folder / b).read_bytes() == b"two"


def test_oversize_is_refused_with_a_clear_error(client_bot, monkeypatch):
    client, bot, settings = client_bot
    monkeypatch.setenv("BOT_OFFICE_MAX_UPLOAD_MB", "1")
    big = b"x" * (1024 * 1024 + 1)
    response = up(client, bot, "big.bin", big)
    assert response.status_code == 413 and "1 MB" in response.json()["detail"]
    declared = client.post(f"/v1/bots/{bot}/attachments", params={"name": "big.bin"},
                           headers={**H, "Content-Length": str(len(big))}, content=b"")
    assert declared.status_code == 413
    assert not (settings.data_dir / "bots" / bot / "uploads").exists() or not list((settings.data_dir / "bots" / bot / "uploads").iterdir())
    assert up(client, bot, "ok.bin", b"x" * 1024 * 1024).status_code == 201


def test_default_limit_is_50_mb_and_images_keep_their_own_10_mib(monkeypatch):
    monkeypatch.delenv("BOT_OFFICE_MAX_UPLOAD_MB", raising=False)
    assert attachments.max_file_bytes() == 50 * 1024 * 1024 and attachments.MAX_BYTES == 10 * 1024 * 1024


def test_images_still_work_with_and_without_a_name(client_bot):
    client, bot, settings = client_bot
    plain = client.post(f"/v1/bots/{bot}/attachments", headers=H, content=PNG).json()
    named = up(client, bot, "photo.png", PNG).json()
    for info in (plain, named):
        assert info["mime"] == "image/png" and info["id"].endswith(".png")
        assert (settings.data_dir / "bots" / bot / "attachments" / info["id"]).exists()
        assert client.get(info["url"], headers=H).headers["content-disposition"] == "inline"
    assert client.post(f"/v1/bots/{bot}/attachments", headers=H, content=b"%PDF-1.0").status_code == 415  # no name: images only
    assert client.post(f"/v1/bots/{bot}/attachments", headers=H, content=b"").status_code == 400
    assert up(client, bot, "empty.txt", b"").status_code == 400


def test_file_ids_cannot_be_forged_or_crossed_between_bots(client_bot):
    client, bot, settings = client_bot
    other = client.post("/v1/bots", headers=H, json={"name": "other"}).json()["id"]
    info = up(client, bot, "a.txt", b"secret").json()
    assert client.post(f"/v1/bots/{other}/messages", headers=H, json={"text": "x", "attachments": [info["id"]]}).status_code == 400
    assert client.get(f"/v1/bots/{other}/attachments/{info['id']}", headers=H).status_code == 404
    for bad in ("2026-01-01-" + "0" * 32 + "-..%2F..%2Fx", "2026-01-01-" + "0" * 32 + "-.sh", "../uploads"):
        assert client.get(f"/v1/bots/{bot}/attachments/{bad}", headers=H).status_code in (404, 422)
    assert client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "x", "attachments": ["../../etc/passwd"]}).status_code == 400


def test_unsent_upload_can_be_deleted_but_a_sent_one_cannot(client_bot):
    client, bot, settings = client_bot
    a = up(client, bot, "a.txt", b"1").json()["id"]
    b = up(client, bot, "b.txt", b"2").json()["id"]
    client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "x", "attachments": [b]})
    assert client.delete(f"/v1/bots/{bot}/attachments/{a}", headers=H).status_code == 204
    assert client.delete(f"/v1/bots/{bot}/attachments/{b}", headers=H).status_code == 409


def test_prompt_names_the_file_path_but_gives_it_to_no_image_input(client_bot):
    from botoffice import workspace
    client, bot, settings = client_bot
    info = up(client, bot, "part.stl", b"solid").json()
    path = str((settings.data_dir / "bots" / bot / "uploads" / info["id"]).resolve())
    line = workspace._history_line(type("M", (), {"text": "hi", "role": "user", "sender": None, "steer_job_id": None,
                                                  "created_at": __import__("datetime").datetime(2026, 1, 1)})(), [path])
    assert "attached files" in line and path in line and "attached images" not in line


def test_uploads_are_never_made_executable_or_extracted(client_bot):
    client, bot, settings = client_bot
    info = up(client, bot, "tool.sh", b"#!/bin/sh\n").json()
    path = settings.data_dir / "bots" / bot / "uploads" / info["id"]
    assert not os.access(path, os.X_OK)


def test_steer_prompt_names_files_in_uploads_and_never_as_images(client_bot):
    from types import SimpleNamespace
    from botoffice.providers.cli import _steer_prompt
    client, bot, settings = client_bot
    img = client.post(f"/v1/bots/{bot}/attachments", headers=H, content=PNG).json()["id"]
    doc = up(client, bot, "report.pdf", b"%PDF").json()["id"]
    message = SimpleNamespace(text="look", bot_id=bot, attachments=[img, doc], sender=None, source="chat")
    prompt = _steer_prompt(message, settings.data_dir / "bots" / bot / "workspace")
    assert f"/bots/{bot}/attachments/{img}" in prompt
    assert f"/bots/{bot}/uploads/{doc}" in prompt and f"/attachments/{doc}" not in prompt
    assert "never to run" in prompt


def test_files_do_not_count_against_the_seed_image_caps(client_bot):
    """Many files (and big ones) never make rewind_seed refuse; only images count against its caps."""
    from botoffice import store as store_mod
    client, bot, settings = client_bot
    ids = [up(client, bot, f"f{i}.bin", b"x" * 1024).json()["id"] for i in range(store_mod.SEED_MAX_IMAGES + 6)]
    for start in range(0, len(ids), 6):
        client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "files", "attachments": ids[start:start + 6]})
    store = client.app.state.store
    last = store.list_messages(bot)[-1].id
    seed = store.rewind_seed(bot, last + 1)
    assert len(seed.images) == len(ids)  # named in the seed, none refused


def test_lowering_the_limit_does_not_hide_files_already_sent(client_bot, monkeypatch):
    client, bot, settings = client_bot
    monkeypatch.setenv("BOT_OFFICE_MAX_UPLOAD_MB", "2")
    info = up(client, bot, "keep.bin", b"x" * (1024 * 1024 + 10)).json()
    monkeypatch.setenv("BOT_OFFICE_MAX_UPLOAD_MB", "1")
    assert client.get(info["url"], headers=H).status_code == 200
    assert client.get(info["url"] + "/info", headers=H).json()["size"] == 1024 * 1024 + 10


def test_unsent_uploads_are_pruned_even_for_a_bot_with_no_images(client_bot):
    client, bot, settings = client_bot
    old = up(client, bot, "old.bin", b"1").json()["id"]
    sent = up(client, bot, "sent.bin", b"2").json()["id"]
    client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "x", "attachments": [sent]})
    folder = settings.data_dir / "bots" / bot / "uploads"
    assert not (settings.data_dir / "bots" / bot / "attachments").exists()  # no image folder at all
    for name in (old, sent):
        os.utime(folder / name, (1, 1))
    attachments.prune(settings.data_dir, client.app.state.store.attachment_references())
    assert not (folder / old).exists() and (folder / sent).exists()


def test_claude_is_granted_the_uploads_folder():
    import inspect
    from botoffice.providers import claude
    assert '"uploads"' in inspect.getsource(claude) and '"--add-dir"' in inspect.getsource(claude)


class ReadsUploads(FakeAdapter):
    """Stands in for a CLI that opens the file path the prompt names, as the real ones do (0.11.15 live check)."""

    def __init__(self):
        super().__init__()
        self.paths: list[str] = []

    async def run(self, job, workspace, log_path, timeout):
        import re
        from botoffice.providers.base import RunResult
        found = re.findall(r"^- (/\S+/uploads/\S+)$", (workspace / "prompt.txt").read_text(), re.M)
        self.paths += found
        text = " ".join(open(p).read().strip() for p in found) if found else "no file named"
        log_path.write_text("ran\n", encoding="utf-8")
        return RunResult("succeeded", exit_code=0, final_text=f"The file says: {text}")


def test_a_turn_can_read_an_uploaded_file_and_quotes_its_token(settings):
    """Upload through the API, send, and the CLI-side reply quotes the unique token from the named path."""
    import time
    token = "UPLOADTOKEN-8f3a91c2"
    reader = ReadsUploads()
    with TestClient(create_app(settings, {"codex": reader})) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "reader"}).json()["id"]
        info = up(client, bot, "note.txt", f"The secret code is {token}.\n".encode()).json()
        sent = client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "quote the code in the file", "attachments": [info["id"]]})
        assert sent.status_code == 201
        end = time.monotonic() + 10
        reply = ""
        while time.monotonic() < end and token not in reply:
            time.sleep(0.05)
            reply = " ".join(m["text"] for m in client.get(f"/v1/bots/{bot}/messages", headers=H).json() if m["role"] != "user")
        assert token in reply, reply
        assert reader.paths and reader.paths[0].endswith(info["id"])
