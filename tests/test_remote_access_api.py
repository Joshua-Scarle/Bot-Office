"""Integration tests through the real app. Skipped where fastapi is not installed."""
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from test_remote_access import (
    CONFIG,
    HOST,
    KEY,
    KEY_CONFIG,
    Fetcher,
    jwk,
    service_claims,
    sign,
    user_claims,
)

from botoffice import remote_access as ra
from botoffice.api import create_app


@pytest.fixture
def client(settings, tmp_path, monkeypatch):
    monkeypatch.setenv(ra.CONFIG_ENV, str(tmp_path / "remote_access.json"))
    import json
    cfg = dict(CONFIG, shared_keys=KEY_CONFIG["shared_keys"])
    (tmp_path / "remote_access.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(ra, "default_fetcher", Fetcher(keys=[jwk("k1")]))
    app = create_app(settings=settings)
    with TestClient(app) as c:
        yield c


def _u():
    t = time.time()
    return user_claims(exp=t + 600, iat=t - 5, nbf=t - 5)


def _s():
    t = time.time()
    return service_claims(exp=t + 600, iat=t - 5)


def _remote(token, **extra):
    return {"host": HOST, "cf-access-jwt-assertion": token, **extra}


def test_whoami_local(client):
    assert client.get("/v1/remote/whoami").json() == {"remote": False}


def test_whoami_remote_owner(client):
    r = client.get("/v1/remote/whoami", headers=_remote(sign(_u())))
    assert r.json() == {"remote": True, "kind": "user", "role": "owner", "name": "you@example.test"}


def test_remote_owner_needs_no_bearer(client):
    assert client.get("/v1/bots", headers=_remote(sign(_u()))).status_code == 200


def test_remote_without_jwt_blocked(client):
    assert client.get("/v1/bots", headers={"host": HOST}).status_code == 403
    assert client.get("/v1/bots", headers={"cf-ray": "1"}).status_code == 403


def test_remote_client_cannot_reach_settings(client):
    headers = _remote(sign(_s()))
    assert client.get("/v1/bots", headers=headers).status_code == 200
    assert client.get("/v1/settings", headers=headers).status_code == 403
    assert client.post("/v1/slots/desktop-1/exec", headers=headers, json={}).status_code == 403


def test_agent_scope_refuses_remote_identity(client):
    r = client.get("/v1/agent/tasks", headers=_remote(sign(_u())))
    assert r.status_code == 401


def test_local_bearer_still_works(client):
    assert client.get("/v1/bots").status_code == 401
    assert client.get("/v1/bots", headers={"Authorization": "Bearer test-token"}).status_code == 200
    assert client.get("/v1/bots", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_shared_key_reaches_bots_but_not_settings(client):
    headers = {"host": HOST, "cf-ray": "1", "x-bot-office-key": KEY}
    assert client.get("/v1/bots", headers=headers).status_code == 200
    assert client.get("/v1/remote/whoami", headers=headers).json() == {
        "remote": True, "kind": "service", "role": "remote-client", "name": "claude-cloud"}
    assert client.get("/v1/settings", headers=headers).status_code == 403
    wrong = dict(headers, **{"x-bot-office-key": KEY + "z"})
    assert client.get("/v1/bots", headers=wrong).status_code == 403


# ---------------------------------------------------------------- remote desktop viewer (remote-desktop)
import socket  # noqa: E402
import threading  # noqa: E402

from starlette.websockets import WebSocketDisconnect  # noqa: E402

from botoffice import vnc  # noqa: E402


@pytest.fixture
def fake_desktop(monkeypatch):
    """A loopback 'VNC server' that sends an RFB greeting to every connection, mapped to desktop-12."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            with conn:
                conn.sendall(b"RFB 003.008\n")
                conn.settimeout(2)
                try:
                    conn.recv(64)
                except OSError:
                    pass
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    async def desktops():
        return [{"name": "desktop-12", "port": port, "status": "running"}]
    monkeypatch.setattr(vnc, "desktops", desktops)
    original = vnc.desktop_port
    monkeypatch.setattr(vnc, "desktop_port", lambda name: (original(name), port)[1])
    yield port
    stop.set()
    thread.join(2)
    sock.close()


ORIGIN = f"https://{HOST}"


def _greeting(client, ticket, headers):
    with client.websocket_connect("/v1/desktops/desktop-12/vnc", subprotocols=["binary", "bo-ticket." + ticket],
                                  headers=headers) as ws:
        return ws.receive_bytes()


def test_remote_owner_opens_the_desktop_viewer(client, fake_desktop):
    headers = _remote(sign(_u()), origin=ORIGIN)
    grant = client.post("/v1/desktops/desktop-12/ticket", headers=headers)
    assert grant.status_code == 200
    assert _greeting(client, grant.json()["ticket"], headers) == b"RFB 003.008\n"


def test_ticket_is_bound_to_who_asked(client, fake_desktop):
    remote_headers = _remote(sign(_u()), origin=ORIGIN)
    local_headers = {"Authorization": "Bearer test-token", "origin": "http://testserver"}
    # a remote owner's ticket does not open a local socket
    ticket = client.post("/v1/desktops/desktop-12/ticket", headers=remote_headers).json()["ticket"]
    with pytest.raises(WebSocketDisconnect):
        _greeting(client, ticket, {"origin": "http://testserver"})
    # and a local ticket does not open a remote socket
    ticket = client.post("/v1/desktops/desktop-12/ticket", headers=local_headers).json()["ticket"]
    with pytest.raises(WebSocketDisconnect):
        _greeting(client, ticket, remote_headers)
    # the local one still works locally (when not burnt by a refused attempt)
    ticket = client.post("/v1/desktops/desktop-12/ticket", headers=local_headers).json()["ticket"]
    assert _greeting(client, ticket, {"origin": "http://testserver"}) == b"RFB 003.008\n"


def test_remote_client_gets_no_ticket_and_no_socket(client, fake_desktop):
    service = _remote(sign(_s()), origin=ORIGIN)
    assert client.post("/v1/desktops/desktop-12/ticket", headers=service).status_code == 403
    assert client.get("/v1/desktops", headers=service).status_code == 403
    ticket = client.post("/v1/desktops/desktop-12/ticket", headers=_remote(sign(_u()), origin=ORIGIN)).json()["ticket"]
    with pytest.raises(WebSocketDisconnect):
        _greeting(client, ticket, service)
    keyed = {"host": HOST, "x-bot-office-key": KEY, "origin": ORIGIN}
    assert client.post("/v1/desktops/desktop-12/ticket", headers=keyed).status_code == 403


def test_remote_owner_ticket_post_needs_origin(client, fake_desktop):
    assert client.post("/v1/desktops/desktop-12/ticket", headers=_remote(sign(_u()))).status_code == 403
