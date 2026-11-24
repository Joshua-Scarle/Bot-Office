import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from botoffice import duet_bridge  # noqa: E402
from botoffice.api import create_app  # noqa: E402
from botoffice.providers.fake import FakeAdapter  # noqa: E402

H = {"Authorization": "Bearer test-token"}


def _app(settings):
    token = settings.control_dir / "github-token"
    settings.ensure_dirs()
    token.write_text("ghp_" + "a" * 30)
    token.chmod(0o600)
    path = duet_bridge.config_path(settings.control_dir)
    path.write_text(json.dumps({"repo": "owner/mailbox-repo", "pr": 7, "default_bot": "Alpha"}))
    path.chmod(0o600)
    return create_app(settings, {"codex": FakeAdapter()})


def test_send_and_status_routes(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.post("/v1/bots", headers=H, json={"name": "Alpha"})
        assert client.post("/v1/bridge/duet/send", json={"text": "x"}).status_code == 401
        assert client.get("/v1/bridge/duet/status").status_code == 401
        status = client.get("/v1/bridge/duet/status", headers=H)
        assert status.status_code == 200 and status.json()["repo"] == "owner/mailbox-repo"
        assert client.post("/v1/bridge/duet/send", headers=H,
                           json={"text": "ghp_" + "b" * 30}).status_code == 400
