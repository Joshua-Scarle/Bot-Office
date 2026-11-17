import time

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter


def test_api_auth_and_job_execution(settings):
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        assert client.get("/v1/jobs").status_code == 401
        assert client.get("/v1/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        response = client.post("/v1/jobs", json={"prompt": "make a test"}, headers=headers)
        assert response.status_code == 201
        job_id = response.json()["id"]
        deadline = time.monotonic() + 3
        status = "queued"
        while time.monotonic() < deadline:
            status = client.get(f"/v1/jobs/{job_id}", headers=headers).json()["job"]["status"]
            if status == "succeeded":
                break
            time.sleep(0.02)
        assert status == "succeeded"
        assert client.get("/v1/jobs", headers=headers).json()[0]["id"] == job_id


def test_provider_status_reports_fake_login_and_disabled_stubs(settings):
    app = create_app(settings, {"codex": FakeAdapter(login="needs_login")})
    with TestClient(app) as client:
        response = client.get("/v1/providers", headers={"Authorization": "Bearer test-token"})
        providers = {item["name"]: item for item in response.json()}
    assert providers["codex"] == {"name": "codex", "enabled": True, "login_status": "needs_login"}
    assert providers["claude"] == {"name": "claude", "enabled": False, "login_status": "disabled"}
    assert providers["cursor"]["enabled"] is False
    assert providers["antigravity"]["enabled"] is False
    assert providers["grok"]["enabled"] is False
    assert providers["grok"]["login_status"] == "disabled"
