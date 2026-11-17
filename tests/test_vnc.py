import socket
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from botoffice import vnc
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}


def test_ticket_expiry_and_single_use(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(vnc.time, "monotonic", lambda: clock[0])
    tickets = vnc.Tickets()
    ticket = tickets.issue("desktop-1")
    assert tickets.consume(ticket, "desktop-1")
    assert not tickets.consume(ticket, "desktop-1")
    ticket = tickets.issue("desktop-1")
    clock[0] += 31
    assert not tickets.consume(ticket, "desktop-1")
    assert not vnc.same_origin("http://evil.example", "localhost:8787")
    for name in ("localhost", "desktop-0", "desktop-999", "desktop-1/../x"):
        with pytest.raises(ValueError):
            vnc.desktop_port(name)


def test_vnc_proxy_and_listing(settings, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    sock.settimeout(5)
    def serve():
        try:
            conn, _ = sock.accept()
            with conn:
                conn.sendall(b"hello")
                assert conn.recv(5) == b"world"
        finally:
            sock.close()
    thread = threading.Thread(target=serve)
    thread.start()
    async def desktops():
        return [{"name": "desktop-1", "port": port, "status": "running"}]
    monkeypatch.setattr(vnc, "desktops", desktops)
    original = vnc.desktop_port
    def mapped_port(name):
        original(name)
        return port
    monkeypatch.setattr(vnc, "desktop_port", mapped_port)
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        assert client.post("/v1/desktops/desktop-1/ticket").status_code == 401
        assert client.get("/v1/desktops", headers=H).json()[0]["name"] == "desktop-1"
        assert client.post("/v1/desktops/evil/ticket", headers=H).status_code == 404
        ticket = client.post("/v1/desktops/desktop-1/ticket?mode=control", headers=H).json()["ticket"]
        with client.websocket_connect("/v1/desktops/desktop-1/vnc", headers={"origin": "http://testserver"}, subprotocols=["binary", "bo-ticket." + ticket]) as ws:
            assert ws.accepted_subprotocol == "binary"
            assert ws.receive_bytes() == b"hello"
            ws.send_bytes(b"world")
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/v1/desktops/desktop-1/vnc", headers={"origin": "http://testserver"}, subprotocols=["binary", "bo-ticket." + ticket]
        ):
            pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_desktop_rejects_bad_names_tokens_and_origins(settings, monkeypatch):
    async def desktops():
        return [{"name": "desktop-1", "port": 5901, "status": "running"}]

    monkeypatch.setattr(vnc, "desktops", desktops)
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        assert client.post("/v1/desktops/desktop-1/ticket", headers={"Authorization": "Bearer wrong"}).status_code == 401
        for name in ("desktop-1;x", "desktop-999", "..%2F"):
            assert client.post(f"/v1/desktops/{name}/ticket", headers=H).status_code == 404
        ticket = client.post("/v1/desktops/desktop-1/ticket", headers=H).json()["ticket"]
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/v1/desktops/desktop-1/vnc", headers={"origin": "http://evil.example"}, subprotocols=["binary", "bo-ticket." + ticket]
        ):
            pass
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/v1/desktops/desktop-1/vnc?ticket=bogus", headers={"origin": "http://testserver"}
        ):
            pass


def test_desktop_listing_invokes_podman(settings, monkeypatch):
    import json

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps([{"Names": ["desktop-1"], "State": "running"},
                               {"Names": ["desktop-2"], "State": "exited"},
                               {"Names": ["desktop-999"], "State": "running"}]).encode(), b""

    async def launch(*args, **kwargs):
        assert args[:3] == ("podman", "ps", "-a")
        return Process()

    monkeypatch.setattr(vnc.asyncio, "create_subprocess_exec", launch)
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        result = client.get("/v1/desktops", headers=H).json()
    assert [(item["name"], item["status"], item["port"]) for item in result] == [
        ("desktop-1", "running", 5901), ("desktop-2", "stopped", 5902)]


def handshake(filt, version=b"RFB 003.008\n"):
    for part in (b"RFB 003.", b"008\n"):
        filt.feed_server(part)
    assert filt.feed(version[:5]) == b""
    assert filt.feed(version[5:]) == version
    filt.feed_server(b"\x02")
    filt.feed_server(b"\x02\x01")
    assert filt.feed(b"\x01") == b"\x01"
    if version.endswith(b"008\n"):
        filt.feed_server(bytes(4))
    assert filt.feed(b"\x01") == b"\x01"


def test_view_filter_fragmented_and_coalesced():
    filt = vnc.ViewFilter()
    handshake(filt)
    update = b"\x03" + bytes(9)
    key = b"\x04" + bytes(7)
    pointer = b"\x05" + bytes(5)
    cut = b"\x06" + bytes(3) + (5).to_bytes(4, "big") + b"hello"
    encodings = b"\x02\x00\x00\x01" + bytes(4)
    continuous = b"\x96" + bytes(9)
    fence = b"\xf8" + bytes(7) + b"\x03abc"
    resize = b"\xfb" + bytes(5) + b"\x01" + bytes(17)
    stream = update + key + pointer + cut + encodings + continuous + fence + resize + pointer + update
    result = b"".join(filt.feed(stream[i:i + 3]) for i in range(0, len(stream), 3))
    assert result == update + encodings + continuous + fence + update
    with pytest.raises(ValueError):
        filt.feed(b"\xff")


def test_view_filter_extended_clipboard_caps_and_limits():
    filt = vnc.ViewFilter()
    handshake(filt)
    # noVNC replies with all five action flags, its text format flag and a size.
    caps = b"\x06\x00\x00\x00" + (-8).to_bytes(4, "big", signed=True) + b"\x1f\x00\x00\x02" + bytes(4)
    update = b"\x03" + bytes(9)
    stream = caps + update
    assert b"".join(filt.feed(stream[i:i + 3]) for i in range(0, len(stream), 3)) == update
    for size in (-1048577, -2147483648):
        fresh = vnc.ViewFilter()
        handshake(fresh)
        with pytest.raises(ValueError):
            fresh.feed(b"\x06" + bytes(3) + size.to_bytes(4, "big", signed=True))


def test_view_filter_rejects_desync_and_unsupported_security():
    filt = vnc.ViewFilter()
    filt.feed_server(b"RFB 003.008\n")
    with pytest.raises(ValueError):
        filt.feed(b"RFB 003.003\n" + b"\x01\x03" + bytes(9) + b"\x05" + bytes(5))
    filt = vnc.ViewFilter()
    filt.feed_server(b"RFB 003.008\n")
    filt.feed(b"RFB 003.008\n")
    with pytest.raises(ValueError):
        filt.feed_server(b"\x01\x02")
    filt = vnc.ViewFilter()
    handshake(filt, b"RFB 003.007\n")
    with pytest.raises(ValueError):
        filt.feed(b"\x06" + bytes(3) + (1024 * 1024 + 1).to_bytes(4, "big"))
    with pytest.raises(ValueError):
        fresh = vnc.ViewFilter()
        handshake(fresh)
        fresh.feed(b"\xf8" + bytes(7) + b"\x41")
