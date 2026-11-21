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
    outside = workspace.parent / "outside.txt"  # inside the data dir, but not this job's workspace: protected
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
    token.write_text("t")
    (workspace / SIDECAR).write_text("\n".join([
        str(workspace / "link.txt"), str(outside), "../outside.txt", str(workspace / "empty.txt"),
        str(workspace / "pipe.txt"), str(bot_dir / "memory.md"), str(token), str(workspace / "sub"),
        "/etc/shadow", str(workspace / "sub" / "ok.txt")]) + "\n")
    message = finish(turn, "done")
    assert [attachments.display_name(a) for a in message.attachments] == ["ok.txt"]
    # the symlink, outside, ../outside.txt, memory.md, token and /etc/shadow are protected; the rest (empty, fifo, directory) are not files
    assert "6 files were not attached: a protected location." in message.text
    assert "3 files could not be attached." in message.text


def test_limits_are_unchanged():
    assert attachments.MAX_OUTGOING == 6 and attachments.DEFAULT_MAX_FILE_MB == 50
    assert attachments.max_file_bytes() == 50 * 1024 * 1024


def test_a_file_outside_the_workspace_is_attached(turn, tmp_path):
    settings, store, bot, _, _, workspace = turn
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "report.csv").write_text("a,b\n1,2\n")
    (elsewhere / "real.html").write_text("<p>hi</p>")
    (workspace / "viahtml.html").symlink_to(elsewhere / "real.html")  # a symlink out of the workspace
    (workspace / SIDECAR).write_text(f"{elsewhere / 'report.csv'}\n../../../elsewhere/report.csv\n{workspace / 'viahtml.html'}\n")
    message = finish(turn, "done")
    assert sorted(attachments.display_name(a) for a in message.attachments) == ["real.html", "report.csv"]
    assert "not attached" not in message.text and "could not" not in message.text


@pytest.fixture
def lair(turn, tmp_path, monkeypatch):
    """A fake home with the protected places in it, and a plain directory next to it."""
    home = tmp_path / "home"
    for rel in (".ssh", ".config/rclone", ".claude/projects", ".codex/sessions"):
        (home / rel).mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("KEY")
    (home / ".config" / "rclone" / "rclone.conf").write_text("[x]")
    (home / ".claude" / "projects" / "s.jsonl").write_text("{}")
    (home / ".codex" / "auth.json").write_text("{}")
    (home / ".netrc").write_text("machine")
    (home / "work").mkdir()
    (home / "work" / ".env").write_text("A=1")
    (home / "work" / "prod.env").write_text("A=1")
    (home / "work" / ".env.local").write_text("A=1")
    (home / "work" / "fine.txt").write_text("fine")
    monkeypatch.setenv("HOME", str(home))
    return turn, home


def test_denylist_refuses_every_protected_place(lair, monkeypatch):
    (settings, store, bot, _, _, workspace), home = lair
    db_copy = settings.data_dir / "agentdesk.sqlite3"  # the store's own database
    assert db_copy.exists()
    token = settings.data_dir / "token"
    token.write_text("t")
    session = settings.data_dir / "bots" / str(bot.id) / "sessions" / "s.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}")
    secure = home / "secure"  # stands in for /workspace/secure
    secure.mkdir()
    (secure / "k.txt").write_text("x")
    monkeypatch.setattr(attachments, "DENY_SYSTEM", (str(secure),))
    protected = [home / ".ssh" / "id_ed25519", home / ".config" / "rclone" / "rclone.conf", home / ".claude" / "projects" / "s.jsonl",
                 home / ".codex" / "auth.json", home / ".netrc", home / "work" / ".env", home / "work" / "prod.env",
                 home / "work" / ".env.local", db_copy, token, session, secure / "k.txt"]
    (workspace / SIDECAR).write_text("".join(f"{p}\n" for p in protected) + f"{home / 'work' / 'fine.txt'}\n")
    message = finish(lair[0], "done")
    assert [attachments.display_name(a) for a in message.attachments] == ["fine.txt"]
    assert f"{len(protected)} files were not attached: a protected location." in message.text


def test_denylist_follows_symlinks_dotdot_and_hard_links(lair):
    (settings, store, bot, _, _, workspace), home = lair
    key = home / ".ssh" / "id_ed25519"
    (home / "work" / "innocent.txt").symlink_to(key)  # a name that looks harmless, pointing at a key
    (home / "work" / "dir").symlink_to(home / ".ssh", target_is_directory=True)  # a directory link into ~/.ssh
    os.link(key, home / "work" / "hard.txt")  # same file, other name: not a protected path, so the link count is the check
    token = settings.data_dir / "token"
    token.write_text("t")
    os.link(token, home / "work" / "tok.txt")
    (workspace / SIDECAR).write_text("\n".join([
        str(home / "work" / "innocent.txt"),
        str(home / "work" / "dir" / "id_ed25519"),
        str(home / "work" / "dir" / ".." / ".ssh" / "id_ed25519"),
        f"../../../{home.name}/.ssh/id_ed25519",  # ../ out of the workspace, relative
        f"../../../{home.name}/work/../.ssh/id_ed25519",
        str(workspace / ".." / ".." / "token"),  # ../ into the data dir
        "~/.ssh/id_ed25519",
        str(home / "work" / "tok.txt"), str(home / "work" / "hard.txt"),
        str(home / "work" / "fine.txt")]) + "\n")
    message = finish(lair[0], "done")
    assert [attachments.display_name(a) for a in message.attachments] == ["fine.txt"]
    assert "9 files were not attached: a protected location." in message.text


def test_denylist_covers_the_private_controller_dir(turn, tmp_path):
    settings, store, bot, _, _, workspace = turn
    private = tmp_path / "private"
    private.mkdir()
    (private / "agentdesk.sqlite3").write_text("db")
    (private / "token").write_text("t")
    (workspace / "a.txt").write_text("a")
    (workspace / SIDECAR).write_text(f"{private / 'agentdesk.sqlite3'}\n{private / 'token'}\n{workspace / 'a.txt'}\n")
    text, ids = attachments.collect_outgoing(settings.data_dir, bot.id, workspace, "", "x", {}, private_dir=private)
    assert [attachments.display_name(i) for i in ids] == ["a.txt"] and "2 files were not attached" in text


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
    ("a.html", "html", "application/octet-stream"), ("a.HTM", "html", "application/octet-stream"), ("a.svg", "svg", "application/octet-stream"),
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
    assert disposition.startswith("inline;" if preview in ("pdf", "text") else "attachment;") and f'filename="{name}"' in disposition
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


PAGE = b"<!doctype html><title>t</title><style>p{color:red}</style><p>hello</p><script>alert(1)</script>"


def view_url(client, bot, name, data):
    info = up(client, bot, name, data)
    got = client.post(info["url"] + "/view-link", headers=H)
    assert got.status_code == 200, got.text
    return info, got.json()["url"]


@pytest.mark.parametrize("name,ctype", [("report.html", "text/html; charset=utf-8"), ("pic.svg", "image/svg+xml")])
def test_html_and_svg_are_served_sandboxed_through_a_signed_link(client_bot, name, ctype):
    client, bot = client_bot
    info, url = view_url(client, bot, name, PAGE)
    got = client.get(url)  # no bearer header: an iframe cannot send one, the link carries the signature
    assert got.status_code == 200 and got.content == PAGE and got.headers["content-type"] == ctype
    csp = got.headers["content-security-policy"]
    assert csp.startswith("sandbox;") and "allow-same-origin" not in csp and "allow-scripts" not in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert got.headers["x-content-type-options"] == "nosniff" and "no-store" in got.headers["cache-control"]
    # the bearer route still never renders them
    plain = client.get(info["url"], headers=H)
    assert plain.headers["content-type"] == "application/octet-stream" and plain.headers["content-disposition"].startswith("attachment;")
    assert client.get(info["url"]).status_code == 401


def test_view_link_is_bound_to_one_file_and_expires(client_bot, monkeypatch):
    client, bot = client_bot
    info, url = view_url(client, bot, "a.html", PAGE)
    other = up(client, bot, "b.html", PAGE)
    path, query = url.split("?")
    assert client.get(path.replace(info["id"], other["id"]) + "?" + query).status_code == 403  # another file
    assert client.get(path + "?" + query.replace("sig=", "sig=x")).status_code == 403
    assert client.get(path + "?exp=1&sig=abc").status_code == 403
    import time as _time
    real = _time.time
    monkeypatch.setattr("botoffice.api.time.time", lambda: real() + 600)
    assert client.get(url).status_code == 403  # expired
    monkeypatch.undo()
    pdf = up(client, bot, "x.pdf", b"%PDF-1.4")
    assert client.post(pdf["url"] + "/view-link", headers=H).status_code == 400  # only html and svg
    assert client.post(info["url"] + "/view-link").status_code == 401
    other_bot = client.post("/v1/bots", headers=H, json={"name": "o"}).json()["id"]
    assert client.get(url.replace(bot, other_bot)).status_code == 403


def test_page_csp_allows_only_its_own_frames_and_the_viewer_never_gets_same_origin():
    from botoffice import api
    csp = api._page_with_csp()[1]
    assert "frame-src 'self'" in csp and "default-src 'none'" in csp
    html = api.CHAT_PAGE.read_text()
    assert 'setAttribute("sandbox", scripts ? "allow-scripts" : "")' in html and "allow-same-origin" not in html


def test_env_and_key_files_are_refused_even_inside_the_workspace(turn):
    workspace = turn[-1]
    for name in (".env", "prod.env", ".env.local", "id_rsa", "server.pem", "rclone.conf"):
        (workspace / name).write_text("x")
    (workspace / "ok.txt").write_text("ok")
    (workspace / SIDECAR).write_text("".join(f"{workspace / n}\n" for n in (".env", "prod.env", ".env.local", "id_rsa", "server.pem", "rclone.conf", "ok.txt")))
    message = finish(turn, "done")
    assert [attachments.display_name(a) for a in message.attachments] == ["ok.txt"]
    assert "6 files were not attached: a protected location." in message.text


def _csp_parts(got):
    return [part.strip() for part in got.headers["content-security-policy"].split(";")]


def test_run_scripts_is_off_by_default_and_the_link_says_so(client_bot):
    client, bot = client_bot
    info = up(client, bot, "app.html", PAGE)
    plain = client.post(info["url"] + "/view-link", headers=H).json()
    assert plain["scripts"] is False and "scripts=" not in plain["url"]
    off = client.post(info["url"] + "/view-link", headers=H, params={"scripts": "false"}).json()
    assert off["scripts"] is False and "scripts=" not in off["url"]
    got = client.get(plain["url"])
    assert _csp_parts(got)[0] == "sandbox"  # no tokens at all: nothing is allowed
    assert "script-src" not in got.headers["content-security-policy"]


def test_run_scripts_link_adds_allow_scripts_only_never_same_origin(client_bot):
    client, bot = client_bot
    info = up(client, bot, "app.html", PAGE)
    link = client.post(info["url"] + "/view-link", headers=H, params={"scripts": "true"}).json()
    assert link["scripts"] is True and "scripts=1" in link["url"]
    got = client.get(link["url"])
    assert got.status_code == 200 and got.content == PAGE
    parts = _csp_parts(got)
    assert parts[0].split() == ["sandbox", "allow-scripts"]
    for banned in ("allow-same-origin", "allow-top-navigation", "allow-popups", "allow-forms", "allow-modals"):
        assert banned not in got.headers["content-security-policy"]
    assert "connect-src 'none'" in parts and "default-src 'none'" in parts and "form-action 'none'" in parts
    assert "frame-ancestors 'self'" in parts and "script-src 'unsafe-inline'" in parts
    assert got.headers["x-content-type-options"] == "nosniff" and "no-store" in got.headers["cache-control"]
    assert H["Authorization"].split()[-1] not in link["url"]  # the master token is never in the URL


def test_scripts_flag_is_signed_and_cannot_be_added_to_a_plain_link(client_bot):
    client, bot = client_bot
    info = up(client, bot, "app.html", PAGE)
    plain = client.post(info["url"] + "/view-link", headers=H).json()["url"]
    assert client.get(plain + "&scripts=1").status_code == 403
    on = client.post(info["url"] + "/view-link", headers=H, params={"scripts": "true"}).json()["url"]
    assert client.get(on.replace("&scripts=1", "")).status_code == 403  # nor taken off, which would only confuse
    assert client.get(on.replace("scripts=1", "scripts=2")).status_code == 403
    assert client.get(on).status_code == 200


def test_svg_run_scripts_uses_the_same_policy(client_bot):
    client, bot = client_bot
    info = up(client, bot, "pic.svg", PAGE)
    got = client.get(client.post(info["url"] + "/view-link", headers=H, params={"scripts": "true"}).json()["url"])
    assert _csp_parts(got)[0].split() == ["sandbox", "allow-scripts"] and got.headers["content-type"] == "image/svg+xml"


def test_page_frames_scripts_only_behind_an_unchecked_switch_and_never_with_same_origin():
    import re
    from botoffice import api
    html = api.CHAT_PAGE.read_text()
    assert 'id="file-preview-run"' in html and 'id="file-preview-run" checked' not in html
    assert "allow-same-origin" not in html and "allow-top-navigation" not in html and "allow-popups" not in html and "allow-forms" not in html
    # the only sandbox values the page can set: the empty one, or allow-scripts when the switch is on
    assert re.findall(r'setAttribute\("sandbox", ([^)]*)\)', html) == ['scripts ? "allow-scripts" : ""']
    # off at every open and never remembered
    assert "run.checked = false" in html
    start = html.index("async function previewFile")
    body = html[start:html.index("async function downloadFile")]
    assert "localStorage" not in body and "sessionStorage" not in body
    assert 'scripts ? "?scripts=true" : ""' in body and "Scripts are running in this page" in body
