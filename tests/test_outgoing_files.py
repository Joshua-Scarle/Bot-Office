"""0.12.4 (A21): a bot sends the owner a file of any type; stored safely, served by an allow-list, previewed only when safe."""
import os

import pytest
from fastapi.testclient import TestClient

from botoffice import attachments
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from test_bot_display_images import PNG, finish, turn  # noqa: F401  (the turn fixture)

H = {"Authorization": "Bearer test-token"}
SIDECAR = ".bot-office-outgoing-images"
FILES = {"report.pdf": b"%PDF-1.4 x", "data.csv": b"a,b\n1,2\n", "bundle.zip": b"PK\x03\x04zip", "page.html": b"<script>alert(1)</script>"}


def queue(workspace, *names):
    (workspace / SIDECAR).write_text("".join(f"{workspace / n}\n" for n in names))


def test_any_file_in_the_sidecar_is_stored_in_uploads_and_listed(turn):
    settings, store, bot, _, _, workspace = turn
    for name, data in FILES.items():
        (workspace / name).write_bytes(data)
    (workspace / "chart.png").write_bytes(PNG)
    queue(workspace, *FILES, "chart.png")
    message = finish(turn, "Here you go")
    assert len(message.attachments) == 5
    files = [a for a in message.attachments if attachments.is_file_id(a)]
    assert sorted(attachments.display_name(a) for a in files) == sorted(FILES)
    uploads = settings.data_dir / "bots" / str(bot.id) / "uploads"
    for name in files:
        path = uploads / name
        assert path.read_bytes() == FILES[attachments.display_name(name)]
        assert path.stat().st_mode & 0o777 == 0o400  # never executable
    assert any(attachments.ID_RE.fullmatch(a) for a in message.attachments)  # the image went to the image store
    assert "could not be attached" not in message.text


def test_markdown_image_syntax_stays_image_only(turn):
    workspace = turn[-1]
    (workspace / "data.csv").write_bytes(FILES["data.csv"])
    message = finish(turn, "![x](data.csv)")
    assert message.attachments == []
    assert "1 image could not be attached" in message.text


def test_unsafe_file_paths_are_refused(turn):
    settings, store, bot, _, _, workspace = turn
    outside = workspace.parent / "outside.txt"
    outside.write_text("secret")
    (workspace / "link.txt").symlink_to(outside)
    (workspace / "empty.txt").write_bytes(b"")
    os.mkfifo(workspace / "pipe.txt")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "ok.txt").write_text("fine")
    bot_dir = settings.data_dir / "bots" / str(bot.id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "memory.md").write_text("private notes")
    token = settings.data_dir / "token"
    (workspace / SIDECAR).write_text("\n".join([
        str(workspace / "link.txt"), str(outside), "../outside.txt", str(workspace / "empty.txt"),
        str(workspace / "pipe.txt"), str(bot_dir / "memory.md"), str(token), str(workspace / "sub"),
        "/etc/passwd", str(workspace / "sub" / "ok.txt")]) + "\n")
    message = finish(turn, "done")
    assert [attachments.display_name(a) for a in message.attachments] == ["ok.txt"]
    assert "9 files could not be attached." in message.text


def test_symlinked_directory_in_the_path_is_refused(turn):
    workspace = turn[-1]
    target = workspace.parent / "elsewhere"
    target.mkdir()
    (target / "a.txt").write_text("x")
    (workspace / "dir").symlink_to(target, target_is_directory=True)
    (workspace / SIDECAR).write_text(f"{workspace / 'dir' / 'a.txt'}\n")
    assert finish(turn, "done").attachments == []


def test_oversized_file_is_refused_and_the_cap_is_six(turn, monkeypatch):
    workspace = turn[-1]
    monkeypatch.setenv("BOT_OFFICE_MAX_UPLOAD_MB", "0.001")  # ~1 KiB
    (workspace / "big.bin").write_bytes(b"x" * 5000)
    for n in range(8):
        (workspace / f"s{n}.txt").write_text("s")
    queue(workspace, "big.bin", *[f"s{n}.txt" for n in range(8)])
    message = finish(turn, "done")
    assert len(message.attachments) == attachments.MAX_OUTGOING
    assert all("big" not in attachments.display_name(a) for a in message.attachments)
    assert "3 files could not be attached." in message.text  # the big one, plus two over the cap


def test_filename_is_sanitised(turn):
    workspace = turn[-1]
    (workspace / "Q3 report (final) é.pdf").write_bytes(FILES["report.pdf"])
    (workspace / "..hidden").write_text("x")
    (workspace / SIDECAR).write_text(f"{workspace / 'Q3 report (final) é.pdf'}\n{workspace / '..hidden'}\n")
    names = [attachments.display_name(a) for a in finish(turn, "done").attachments]
    assert names == ["Q3_report_final_.pdf", "hidden"]


def test_image_over_ten_mib_travels_as_a_file(turn):
    workspace = turn[-1]
    (workspace / "big.png").write_bytes(PNG + b"\0" * (attachments.MAX_BYTES + 1))
    queue(workspace, "big.png")
    message = finish(turn, "done")
    assert [attachments.is_file_id(a) for a in message.attachments] == [True]


@pytest.fixture
def client_bot(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "files"}).json()["id"]
        yield client, bot


def up(client, bot, name, data):
    return client.post(f"/v1/bots/{bot}/attachments", params={"name": name}, headers=H, content=data).json()


@pytest.mark.parametrize("name,preview,ctype", [
    ("a.pdf", "pdf", "application/pdf"), ("a.csv", "text", "text/plain; charset=utf-8"), ("a.md", "text", "text/plain; charset=utf-8"),
    ("a.html", None, "application/octet-stream"), ("a.HTM", None, "application/octet-stream"), ("a.svg", None, "application/octet-stream"),
    ("a.xml", None, "application/octet-stream"), ("a.zip", None, "application/octet-stream"), ("noext", None, "application/octet-stream"),
])
def test_serving_is_by_extension_allow_list(client_bot, name, preview, ctype):
    client, bot = client_bot
    info = up(client, bot, name, b"<html><script>alert(1)</script>")
    meta = client.get(info["url"] + "/info", headers=H).json()
    assert meta["preview"] == preview and meta["name"] == name
    got = client.get(info["url"], headers=H)
    assert got.headers["content-type"] == ctype
    disposition = got.headers["content-disposition"]
    assert disposition.startswith("inline;" if preview else "attachment;") and f'filename="{name}"' in disposition
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"] and "no-store" in got.headers["cache-control"]
    forced = client.get(info["url"], headers=H, params={"download": 1})
    assert forced.headers["content-disposition"].startswith("attachment;")
    assert forced.headers["content-type"] == "application/octet-stream"


def test_end_to_end_reply_file_is_downloadable(settings):
    class FileAdapter(FakeAdapter):
        async def run(self, job, workspace, log_path, timeout):
            (workspace / "report.pdf").write_bytes(FILES["report.pdf"])
            (workspace / SIDECAR).write_text(f"{workspace / 'report.pdf'}\n")
            (workspace / "reply.txt").write_text("Report attached.")
            return await super().run(job, workspace, log_path, timeout)

    with TestClient(create_app(settings, {"codex": FileAdapter()})) as client:
        bot = client.post("/v1/bots", headers=H, json={"name": "sender"}).json()["id"]
        client.post(f"/v1/bots/{bot}/messages", headers=H, json={"text": "send the report"})
        import time
        for _ in range(100):
            replies = [m for m in client.get(f"/v1/bots/{bot}/messages", headers=H).json() if m["role"] == "assistant"]
            if replies and replies[-1]["attachments"]:
                break
            time.sleep(0.1)
        name = replies[-1]["attachments"][0]
        got = client.get(f"/v1/bots/{bot}/attachments/{name}", headers=H)
        assert got.status_code == 200 and got.content == FILES["report.pdf"] and got.headers["content-type"] == "application/pdf"
