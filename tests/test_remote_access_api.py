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
