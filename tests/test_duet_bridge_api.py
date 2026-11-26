import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from botoffice import duet_bridge  # noqa: E402
from botoffice.api import create_app  # noqa: E402
from botoffice.providers.fake import FakeAdapter  # noqa: E402

from test_duet_bridge import Fake  # noqa: E402

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    deny = tmp_path / "denylist.txt"
    deny.write_text("")
    monkeypatch.setenv("BO_PUSH_DENYLIST", str(deny))
    github = Fake()
    # the app builds its own bridge: swap the module's transport so nothing reaches the network
    monkeypatch.setattr(duet_bridge, "default_transport", github)
    return github


def _app(settings, **config):
    token = settings.control_dir / "github-token"
    settings.ensure_dirs()
    token.write_text("ghp_" + "a" * 30)
    token.chmod(0o600)
    path = duet_bridge.config_path(settings.control_dir)
    path.write_text(json.dumps({"repo": "owner/mailbox-repo", "pr": 7, "default_bot": "Alpha", **config}))
    path.chmod(0o600)
    return create_app(settings, {"codex": FakeAdapter(enabled=False)})


def running_token(store, bot):
    store.create("work", "codex", bot_id=bot.id, source="chat")
    job = store.claim(["desktop-1"], {"codex"})
    assert job and job.bot_id == bot.id
    return {"Authorization": "Bearer " + store.issue_agent_token(job)}


def test_send_and_status_routes(settings, fake):
    app = _app(settings)
    with TestClient(app) as client:
        client.post("/v1/bots", headers=H, json={"name": "Alpha"})
        assert client.post("/v1/bridge/duet/send", json={"text": "x"}).status_code == 401
        assert client.get("/v1/bridge/duet/status").status_code == 401
        status = client.get("/v1/bridge/duet/status", headers=H)
        assert status.status_code == 200 and status.json()["repo"] == "owner/mailbox-repo"
        assert {"worker_running", "mailbox_ready", "waiting_for"} <= set(status.json())
        assert client.post("/v1/bridge/duet/send", headers=H,
                           json={"text": "ghp_" + "b" * 30}).status_code == 400
        sent = client.post("/v1/bridge/duet/send", headers=H, json={"text": "hello", "from": "Alpha"})
        assert sent.status_code == 200, sent.text
        assert client.post("/v1/bridge/duet/send", headers=H,
                           json={"text": "hello", "from": "Nobody"}).status_code == 404
    assert all(call[1].startswith("https://api.github.com/") for call in fake.calls)


def test_job_token_sends_only_as_an_allowed_bot(settings, fake):
    app = _app(settings, senders=["Beta"])
    with TestClient(app) as client:
        store = client.app.state.store
        alpha, beta, gamma = (store.create_bot(name, "", "codex", None) for name in ("Alpha", "Beta", "Gamma"))
        refused = client.post("/v1/bridge/duet/send", headers=running_token(store, gamma), json={"text": "x"})
        assert refused.status_code == 403
        ok = client.post("/v1/bridge/duet/send", headers=running_token(store, beta), json={"text": "from beta"})
        assert ok.status_code == 200, ok.text
        assert f"from={beta.id} from_name=Beta" in fake.posted[-1]["body"]


def test_validation_errors_do_not_echo_the_text(settings, fake):
    app = _app(settings)
    with TestClient(app) as client:
        secret = "ghp_" + "e" * 30
        answer = client.post("/v1/bridge/duet/send", headers=H, json={"text": secret * 3000})
        assert answer.status_code == 422 and secret not in answer.text
