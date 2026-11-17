from datetime import datetime
from pathlib import Path

import pytest

from botoffice import desk
from botoffice.cron import cron_matches, parse_cron


def test_cron_matching():
    moment = datetime(2026, 11, 12, 9, 30)  # Thursday
    assert cron_matches("* * * * *", moment)
    assert cron_matches("30 9 * * *", moment)
    assert cron_matches("*/15 8-10 * * 4", moment)
    assert cron_matches("0,30 9 12 11 *", moment)
    assert not cron_matches("31 9 * * *", moment)
    assert not cron_matches("*/7 * * * *", moment)
    assert cron_matches("30 9 1 * 4", moment)  # dom OR dow when both restricted
    assert cron_matches("0 0 * * 7", datetime(2026, 11, 15, 0, 0))  # 7 == Sunday


@pytest.mark.parametrize("expr", ["", "* * * *", "60 * * * *", "*/0 * * * *", "a * * * *", "5-2 * * * *"])
def test_cron_rejects_invalid(expr):
    with pytest.raises(ValueError):
        parse_cron(expr)


def test_desk_argv():
    assert desk.build_commands("desktop-1", "click", ["10", "20"]) == [
        ["podman", "exec", "desktop-1", "env", "DISPLAY=:1", "xdotool", "mousemove", "10", "20", "click", "1"]
    ]
    assert desk.build_commands("desktop-2", "type", ["-rf", "hi"])[0][-4:] == ["xdotool", "type", "--", "-rf hi"][-4:]
    assert desk.build_commands("desktop-1", "key", ["ctrl+l"])[0][-3:] == ["xdotool", "key", "ctrl+l"]
    assert desk.build_commands("desktop-1", "open", ["https://example.com"]) == [
        ["podman", "exec", "-d", "desktop-1", "env", "DISPLAY=:1", "epiphany", "https://example.com"]
    ]
    shot = desk.screenshot_commands("desktop-1", Path("/w/s.png"), "x.png")
    assert shot[0] == ["podman", "exec", "desktop-1", "env", "DISPLAY=:1", "import", "-window", "root", "/tmp/x.png"]
    assert shot[1] == ["podman", "cp", "desktop-1:/tmp/x.png", "/w/s.png"]


def test_desk_rejections(tmp_path):
    with pytest.raises(desk.DeskError):
        desk.resolve_slot("desktop-3")
    with pytest.raises(desk.DeskError):
        desk.resolve_slot(None, {})
    assert desk.resolve_slot(None, {"AGENTDESK_SLOT": "desktop-2"}) == "desktop-2"
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.org", "https://", "-x"):
        with pytest.raises(desk.DeskError):
            desk.build_commands("desktop-1", "open", [url])
    for command, args in (("type", [""]), ("type", []), ("key", [""]), ("click", ["a", "1"]), ("click", ["1"])):
        with pytest.raises(desk.DeskError):
            desk.build_commands("desktop-1", command, args)
    with pytest.raises(desk.DeskError):
        desk.screenshot_dest("../../etc/x.png", tmp_path)
    assert desk.screenshot_dest(None, tmp_path) == tmp_path.resolve() / "screenshot.png"
    assert desk.main(["--slot", "bogus", "key", "a"]) == 1


def test_desk_message_writes_private_inbox_file(tmp_path):
    directory, payload = desk.message_target(
        ["helper", "hello", "there"], {"AGENTDESK_BOT_ID": "abc"}, tmp_path
    )
    assert directory == tmp_path / "_by-name"
    assert payload == {"to": "helper", "text": "hello there", "from_bot_id": "abc", "job_id": ""}
    path = desk.write_inbox(directory, payload)
    assert path.stat().st_mode & 0o777 == 0o600 and directory.stat().st_mode & 0o777 == 0o700
    uid = "7b1f2c0e-1111-4222-8333-444455556666"
    assert desk.message_target([uid, "x"], {}, tmp_path)[0] == tmp_path / uid
    with pytest.raises(desk.DeskError):
        desk.message_target(["bot"], {}, tmp_path)
    with pytest.raises(desk.DeskError):
        desk.message_target(["bot", " "], {}, tmp_path)


@pytest.mark.parametrize("failure", ["write", "close", "rename"])
def test_desk_removes_private_temp_file_on_failure(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError(f"{failure} failed")

    if failure == "write":
        monkeypatch.setattr(desk.json, "dump", fail)
    elif failure == "close":
        fdopen = desk.os.fdopen

        class FailingClose:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream

            def __exit__(self, *args):
                self.stream.close()
                fail()

        monkeypatch.setattr(desk.os, "fdopen", lambda *args, **kwargs: FailingClose(fdopen(*args, **kwargs)))
    else:
        def fail_rename(source, destination):
            assert source.stat().st_mode & 0o777 == 0o600
            fail()

        monkeypatch.setattr(desk.os, "replace", fail_rename)

    with pytest.raises(OSError, match=f"{failure} failed"):
        desk.write_inbox(tmp_path, {"agent_token": "secret"})
    assert list(tmp_path.iterdir()) == []
