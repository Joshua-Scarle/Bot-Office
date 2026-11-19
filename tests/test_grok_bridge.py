import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from botoffice import desk
from botoffice.desk import DeskError
from botoffice.domain import JobStatus
from botoffice.grok_bridge import (
    MAX_HOPS,
    auth_header,
    desk_grok,
    desk_inbox,
    install_config,
    post_inbox,
    send_to_grok,
)
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore


def _config(tmp_path: Path, **overrides) -> Path:
    payload = {"url": "https://example.test/hook", "key": "test-key", **overrides}
    return install_config(json.dumps(payload), tmp_path)


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def getcode(self):
        return self.status


def test_install_config_from_json_and_lines(tmp_path):
    path = install_config('{"url":"https://example.test/hook","key":"secret-key"}', tmp_path)
    assert path == tmp_path / "grok_webhook.json"
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    assert data["url"] == "https://example.test/hook" and data["key"] == "secret-key"
    assert data["header"] == "Authorization"
    path = install_config("https://example.test/other\nother-key\nX-Api-Key\n", tmp_path)
    data = json.loads(path.read_text())
    assert data["header"] == "X-Api-Key" and data["key"] == "other-key"
    out = io.StringIO()
    import sys
    old = sys.stdout
    sys.stdout = out
    try:
        assert desk_grok(["install-config"], stdin=io.StringIO('{"url":"https://example.test/hook","key":"k"}'), data_dir=tmp_path) == 0
    finally:
        sys.stdout = old
    assert "secret-key" not in out.getvalue() and "0600" in out.getvalue()


def test_auth_header_defaults_to_bearer_and_is_configurable():
    assert auth_header({"url": "https://x", "key": "abc", "header": "Authorization", "scheme": "Bearer"}) == (
        "Authorization",
        "Bearer abc",
    )
    assert auth_header({"url": "https://x", "key": "abc", "header": "X-Api-Key", "scheme": ""}) == ("X-Api-Key", "abc")


def test_send_posts_json_with_bearer_header(tmp_path):
    _config(tmp_path)
    calls = []

    def opener(request, timeout=15):
        calls.append((request, timeout))
        return FakeResponse(200)

    assert (
        send_to_grok(
            to="Chief Of Staff",
            kind="question",
            text="Need a review",
            thread_id="t1",
            from_bot="Cursor Bot",
            data_dir=tmp_path,
            opener=opener,
            now=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
        )
        == "sent"
    )
    request, _ = calls[0]
    assert request.full_url == "https://example.test/hook"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert request.get_header("Content-type") == "application/json"
    payload = json.loads(request.data.decode())
    assert payload == {
        "from_bot": "Cursor Bot",
        "to": "Chief Of Staff",
        "kind": "question",
        "text": "Need a review",
        "thread_id": "t1",
        "ts": "2026-11-13T12:00:00+00:00",
        "hop": 1,
    }


def test_send_drops_ack_and_rate_limits(tmp_path, monkeypatch):
    _config(tmp_path)
    monkeypatch.setenv("BOT_OFFICE_GROK_PER_BOT_DAILY", "30")  # the per-bot share is tested separately
    calls = []

    def opener(request, timeout=15):
        calls.append(request)
        return FakeResponse(200)

    assert send_to_grok(to="CoS", kind="ack", text="ok", from_bot="A", data_dir=tmp_path, opener=opener) == "dropped: kind=ack is not sent"
    assert calls == []
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    assert send_to_grok(to="CoS", kind="fyi", text="one", from_bot="A", data_dir=tmp_path, opener=opener, now=now) == "sent"
    second = send_to_grok(to="CoS", kind="fyi", text="two", from_bot="A", data_dir=tmp_path, opener=opener, now=now + timedelta(seconds=30))
    assert second.startswith("dropped: rate limit")
    assert send_to_grok(to="CoS", kind="fyi", text="other", from_bot="B", data_dir=tmp_path, opener=opener, now=now + timedelta(seconds=30)) == "sent"
    later = now + timedelta(seconds=61)
    assert send_to_grok(to="CoS", kind="fyi", text="retry", from_bot="A", data_dir=tmp_path, opener=opener, now=later) == "sent"
    # fill the daily cap from a third bot
    for index in range(27):
        assert (
            send_to_grok(
                to="CoS",
                kind="fyi",
                text=f"n{index}",
                from_bot="C",
                data_dir=tmp_path,
                opener=opener,
                now=later + timedelta(minutes=index + 1),
            )
            == "sent"
        )
    daily = send_to_grok(
        to="CoS", kind="fyi", text="overflow", from_bot="D", data_dir=tmp_path, opener=opener, now=later + timedelta(hours=2)
    )
    assert "daily limit" in daily
    assert len(calls) == 30


def test_custom_header_and_cli_drop_ack(tmp_path, capsys):
    _config(tmp_path, header="X-Webhook-Key", scheme="")
    calls = []

    def opener(request, timeout=15):
        calls.append(request)
        return FakeResponse(200)

    assert send_to_grok(to="CoS", kind="result", text="done", from_bot="A", data_dir=tmp_path, opener=opener) == "sent"
    assert calls[0].get_header("X-webhook-key") == "test-key"


def test_inbox_post_starts_job_and_enforces_hops(tmp_path):
    store = JobStore(tmp_path / "agentdesk.sqlite3")
    bot = store.create_bot("Cursor Bot", "help", "codex", None)
    providers = {"codex": FakeAdapter()}
    status, job_id = post_inbox(
        to="Cursor Bot",
        text="Please take this",
        sender="Chief Of Staff",
        thread_id="thread-1",
        hop=1,
        data_dir=tmp_path,
        providers=providers,
    )
    assert status == "queued"
    job = store.get(job_id)
    assert job is not None and job.status == JobStatus.QUEUED and job.source == "inbox" and job.bot_id == bot.id
    messages = store.list_messages(bot.id)
    assert messages[0].sender == "Chief Of Staff"
    assert "thread=thread-1" in messages[0].text and "Please take this" in messages[0].text
    dropped, detail = post_inbox(
        to="Cursor Bot",
        text="too far",
        sender="Chief Of Staff",
        thread_id="thread-1",
        hop=MAX_HOPS + 1,
        data_dir=tmp_path,
        providers=providers,
    )
    assert dropped == "dropped" and "hop limit" in detail
    assert len(store.list()) == 1
    # hop 6 is still allowed
    status, _ = post_inbox(
        to=str(bot.id),
        text="last hop",
        sender="Chief Of Staff",
        thread_id="thread-1",
        hop=MAX_HOPS,
        data_dir=tmp_path,
        providers=providers,
    )
    assert status == "queued"
    # sending back would be hop 7 and must drop
    _config(tmp_path)

    def opener(request, timeout=15):
        raise AssertionError("must not post after hop limit")

    result = send_to_grok(
        to="Chief Of Staff",
        kind="result",
        text="done",
        thread_id="thread-1",
        from_bot="Cursor Bot",
        data_dir=tmp_path,
        opener=opener,
    )
    assert "hop limit" in result


def test_missing_config_does_not_mention_key_value(tmp_path):
    with pytest.raises(DeskError, match="webhook config not found"):
        send_to_grok(to="CoS", kind="fyi", text="x", data_dir=tmp_path)


@pytest.fixture
def controller(settings, monkeypatch):
    """The real API app; desk's HTTP transport is routed into it, the webhook is recorded."""
    from fastapi.testclient import TestClient

    from botoffice import grok_bridge
    from botoffice.api import create_app

    _config(settings.data_dir)
    sent = []
    monkeypatch.setattr(grok_bridge, "post_webhook", lambda config, payload, opener=None: sent.append(payload))
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        def transport(url, body, token):
            response = client.post("/v1/" + url.split("/v1/", 1)[1], json=body, headers={"Authorization": f"Bearer {token}"})
            return response.status_code, response.json()

        monkeypatch.setattr(grok_bridge, "_transport", transport)
        (settings.data_dir / "token").write_text("test-token\n")
        for name in ("BOT_OFFICE_AGENT_TOKEN", "BOT_OFFICE_JOB_ID", "BOT_OFFICE_BOT_ID", "BOT_OFFICE_TOKEN_FILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("BOT_OFFICE_DATA_DIR", str(settings.data_dir))
        yield SimpleNamespace(app=app, client=client, store=app.state.store, sent=sent)


def _agent_env(monkeypatch, store, bot_name):
    bot = store.find_bot(bot_name) or store.create_bot(bot_name, "", "codex", None)
    job = store.create("turn", "codex", bot_id=bot.id)
    store.claim(("desktop-1",), {"codex"})
    monkeypatch.setenv("BOT_OFFICE_AGENT_TOKEN", store.issue_agent_token(store.get(job.id)))
    monkeypatch.setenv("BOT_OFFICE_JOB_ID", str(job.id))
    monkeypatch.setenv("BOT_OFFICE_BOT_ID", str(bot.id))
    return bot, job


def test_job_sends_grok_through_the_controller_as_its_own_bot(controller, monkeypatch, capsys):
    _agent_env(monkeypatch, controller.store, "Overflow")
    assert desk.main(["grok", "send", "--to", "Chief Of Staff", "--kind", "result", "--text", "hello"]) == 0
    assert controller.sent[0]["text"] == "hello" and controller.sent[0]["from_bot"] == "Overflow"
    # a job cannot pose as another bot
    assert desk.main(["grok", "send", "--to", "CoS", "--kind", "fyi", "--text", "x", "--from", "Someone Else"]) == 1
    assert "only allowed with the master token" in capsys.readouterr().err
    # inside a job, desk inbox stays refused
    assert desk.main(["inbox", "post", "--to", "Other", "--from", "Overflow", "--text", "x"]) == 1


def test_operator_shell_uses_the_master_token_file(controller, capsys):
    controller.store.create_bot("Helper", "help", "codex", None)
    assert desk_grok(["send", "--to", "CoS", "--kind", "ack", "--text", "noop", "--from", "A"]) == 2
    assert "kind=ack" in capsys.readouterr().out
    assert desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "hi", "--from", "Ops"]) == 0
    assert controller.sent[-1]["from_bot"] == "Ops"
    assert desk_inbox(["post", "--to", "Helper", "--from", "Chief Of Staff", "--thread", "abc", "--text", "hello"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("queued ")
    assert any(job.source == "inbox" for job in controller.store.list())
    with pytest.raises(DeskError, match="bot not found"):
        desk_inbox(["post", "--to", "Nobody", "--from", "CoS", "--text", "hello"])


def test_bridge_inbox_requires_the_master_token(controller, monkeypatch):
    bot, job = _agent_env(monkeypatch, controller.store, "Helper")
    response = controller.client.post("/v1/bridge/inbox", json={"to": "Helper", "from": "x", "text": "y"},
                                      headers={"Authorization": "Bearer " + controller.store.issue_agent_token(job)})
    assert response.status_code == 401


def test_inbound_hop_cannot_be_reset_by_the_sender(tmp_path):
    store = JobStore(tmp_path / "agentdesk.sqlite3")
    store.create_bot("Scout", "", "codex", None)
    for _ in range(3):
        post_inbox(to="Scout", text="again", sender="CoS", thread_id="t", hop=1, data_dir=tmp_path,
                   providers={"codex": FakeAdapter()})
    # each inbound message advanced the stored hop although the sender kept saying hop=1
    assert store.bridge_inbox_hop("t", 1, MAX_HOPS) == (4, True)


def test_webhook_url_must_be_https_and_redirects_are_errors(tmp_path, monkeypatch):
    from botoffice import grok_bridge

    with pytest.raises(DeskError, match="must be https"):
        install_config('{"url":"http://example.test/hook","key":"k"}', tmp_path)
    assert install_config('{"url":"http://127.0.0.1:9/hook","key":"k"}', tmp_path)
    with pytest.raises(DeskError, match="must be https"):
        grok_bridge.post_webhook({"url": "http://example.test/x", "key": "k", "header": "Authorization"}, {})

    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setenv("BOT_OFFICE_GROK_HOSTS", "127.0.0.1")  # loopback only when allowlisted
    config = {"url": f"http://127.0.0.1:{server.server_port}/hook", "key": "k", "header": "Authorization", "scheme": "Bearer"}
    try:
        with pytest.raises(DeskError, match="webhook HTTP 302"):
            grok_bridge.post_webhook(config, {"x": 1})
    finally:
        server.server_close()


def test_concurrent_sends_respect_the_limits_and_failed_sends_release(tmp_path):
    import threading

    from botoffice import grok_bridge

    _config(tmp_path)
    store = JobStore(tmp_path / "agentdesk.sqlite3")
    calls = []

    def opener(request, timeout=15):
        calls.append(json.loads(request.data))
        return FakeResponse(200)

    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    results = []

    def send(number):
        results.append(send_to_grok(to="CoS", kind="fyi", text=str(number), from_bot=f"bot{number % 2}",
                                    thread_id="shared", data_dir=tmp_path, opener=opener, now=now))

    threads = [threading.Thread(target=send, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # one per bot per minute: exactly two of 20 concurrent sends went out, with distinct hops
    assert results.count("sent") == 2 and len(calls) == 2
    assert sorted(call["hop"] for call in calls) == [1, 2]

    def broken(request, timeout=15):
        raise grok_bridge.WebhookNotSent("webhook request failed")

    later = now + timedelta(minutes=5)
    with pytest.raises(DeskError):
        send_to_grok(to="CoS", kind="fyi", text="x", from_bot="bot0", thread_id="shared", data_dir=tmp_path,
                     opener=broken, now=later)
    # the failed send released its quota and its hop step
    assert send_to_grok(to="CoS", kind="fyi", text="y", from_bot="bot0", thread_id="shared", data_dir=tmp_path,
                        opener=opener, now=later) == "sent"
    assert calls[-1]["hop"] == 3
    # a reservation whose process died before settling still counts (fails safe)
    store.bridge_reserve("bot9", None, later, per_bot_seconds=60, daily_limit=30, max_hops=MAX_HOPS)
    assert send_to_grok(to="CoS", kind="fyi", text="z", from_bot="bot9", data_dir=tmp_path, opener=opener,
                        now=later).startswith("dropped: rate limit")


def test_old_json_state_is_imported_once(tmp_path):
    now = datetime.now(UTC)
    (tmp_path / "grok_bridge_rate.json").write_text(json.dumps({"sends": [{"bot": "A", "ts": now.isoformat()}]}))
    (tmp_path / "grok_bridge_threads.json").write_text(json.dumps({"t1": 5}))
    store = JobStore(tmp_path / "agentdesk.sqlite3")
    store.initialize()
    assert (tmp_path / "grok_bridge_rate.json.imported").is_file() and not (tmp_path / "grok_bridge_rate.json").exists()
    assert store.bridge_reserve("A", None, now, per_bot_seconds=60, daily_limit=30, max_hops=6)[2].startswith("rate limit")
    assert store.bridge_reserve("B", "t1", now, per_bot_seconds=60, daily_limit=30, max_hops=6)[1:] == (6, None)
    # a second start imports nothing again
    (tmp_path / "grok_bridge_threads.json").write_text(json.dumps({"t1": 1}))
    JobStore(tmp_path / "agentdesk.sqlite3").initialize()
    assert store.bridge_reserve("C", "t1", now, per_bot_seconds=60, daily_limit=30, max_hops=6)[2] == "hop limit of 6 reached"


def test_bot_inbox_file_window_is_60_seconds(settings):
    from contextlib import closing

    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Sender", "", "codex", None)
    job = store.create("turn", "codex", bot_id=bot.id)
    store.claim(("desktop-1",), {"codex"})
    token = store.issue_agent_token(store.get(job.id))
    store.finish(job.id, JobStatus.SUCCEEDED)
    assert store.agent_token_job(token) is None  # the live endpoints stop at job end
    assert store.valid_desk_agent_token(str(job.id), bot.id, token)  # the inbox file path: 60 s more
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?",
                   ((datetime.now(UTC) - timedelta(seconds=61)).isoformat(), str(job.id)))
    assert not store.valid_desk_agent_token(str(job.id), bot.id, token)


def test_desk_message_inside_a_job_uses_the_agent_api(controller, monkeypatch, capsys):
    controller.store.create_bot("Target", "", "codex", None)
    _agent_env(monkeypatch, controller.store, "Sender")
    monkeypatch.setenv("BOT_OFFICE_API_URL", "http://127.0.0.1:8787")
    assert desk.main(["message", "Target", "hello", "there"]) == 0
    assert capsys.readouterr().out.startswith("Message ")
    messages = controller.store.list_agent_messages()
    assert len(messages) == 1 and messages[0]["body"] == "hello there"


def test_install_config_refuses_to_run_inside_a_bot_turn(tmp_path):
    raw = io.StringIO('{"url":"https://evil.test/hook","key":"k"}')
    with pytest.raises(DeskError, match="operator"):
        desk_grok(["install-config"], stdin=raw, data_dir=tmp_path,
                  environ={"BOT_OFFICE_JOB_ID": "j", "BOT_OFFICE_AGENT_TOKEN": "t"})
    assert not (tmp_path / "grok_webhook.json").exists()
    raw = io.StringIO('{"url":"https://example.test/hook","key":"k"}')
    assert desk_grok(["install-config"], stdin=raw, data_dir=tmp_path, environ={}) == 0


def test_ambiguous_webhook_errors_stay_counted(tmp_path):
    from botoffice import grok_bridge

    _config(tmp_path)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

    def timeout(request, timeout=15):
        raise TimeoutError("read timed out")

    with pytest.raises(grok_bridge.WebhookUnsure, match="may have been delivered"):
        send_to_grok(to="CoS", kind="fyi", text="x", from_bot="A", data_dir=tmp_path, opener=timeout, now=now)
    # the send may have arrived: it keeps its quota (the per-bot minute is still used up)
    second = send_to_grok(to="CoS", kind="fyi", text="y", from_bot="A", data_dir=tmp_path,
                          opener=lambda request, timeout=15: FakeResponse(200), now=now + timedelta(seconds=5))
    assert second.startswith("dropped: rate limit")

    def refused(request, timeout=15):
        raise ConnectionRefusedError()

    with pytest.raises(grok_bridge.WebhookNotSent):
        send_to_grok(to="CoS", kind="fyi", text="z", from_bot="B", data_dir=tmp_path, opener=refused, now=now)
    # certainly not sent: released
    assert send_to_grok(to="CoS", kind="fyi", text="z", from_bot="B", data_dir=tmp_path,
                        opener=lambda request, timeout=15: FakeResponse(200), now=now) == "sent"


def test_each_bot_gets_a_share_of_the_daily_limit(tmp_path, monkeypatch):
    _config(tmp_path)
    monkeypatch.setenv("BOT_OFFICE_GROK_PER_BOT_DAILY", "3")
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    ok = lambda request, timeout=15: FakeResponse(200)  # noqa: E731
    for index in range(3):
        assert send_to_grok(to="CoS", kind="fyi", text=str(index), from_bot="A", data_dir=tmp_path,
                            opener=ok, now=now + timedelta(minutes=2 * index)) == "sent"
    fourth = send_to_grok(to="CoS", kind="fyi", text="4", from_bot="A", data_dir=tmp_path, opener=ok,
                          now=now + timedelta(minutes=10))
    assert fourth == "dropped: daily limit of 3 sends per bot reached"
    # other bots still have their share
    assert send_to_grok(to="CoS", kind="fyi", text="b", from_bot="B", data_dir=tmp_path, opener=ok,
                        now=now + timedelta(minutes=10)) == "sent"


def test_webhook_refuses_private_addresses_unless_allowlisted(monkeypatch):
    import socket

    from botoffice import grok_bridge

    def resolve(address):
        return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.delenv("BOT_OFFICE_GROK_HOSTS", raising=False)
    for private in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.2"):
        monkeypatch.setattr(grok_bridge.socket, "getaddrinfo", resolve(private))
        with pytest.raises(grok_bridge.WebhookNotSent, match="private or local"):
            grok_bridge.vetted_address("hooks.example.com", 443)
    monkeypatch.setattr(grok_bridge.socket, "getaddrinfo", resolve("93.184.216.34"))
    assert grok_bridge.vetted_address("hooks.example.com", 443) == "93.184.216.34"
    # allowlisted: a private address is fine, and any other host is refused before connecting
    monkeypatch.setenv("BOT_OFFICE_GROK_HOSTS", "hooks.internal")
    monkeypatch.setattr(grok_bridge.socket, "getaddrinfo", resolve("10.0.0.5"))
    assert grok_bridge.vetted_address("hooks.internal", 443) == "10.0.0.5"
    with pytest.raises(grok_bridge.WebhookNotSent, match="BOT_OFFICE_GROK_HOSTS"):
        grok_bridge.check_url("https://hooks.example.com/x")
    grok_bridge.check_url("https://hooks.internal/x")


def test_inbound_hop_is_stored_only_with_the_turn(tmp_path, monkeypatch):
    from botoffice.store import BridgeHopConflict
    from botoffice.turns import TurnError, Turns

    store = JobStore(tmp_path / "agentdesk.sqlite3")
    bot = store.create_bot("Scout", "", "codex", None)
    providers = {"codex": FakeAdapter()}
    assert store.bridge_inbox_hop("t", 1, MAX_HOPS) == (1, True)
    assert store.bridge_inbox_hop("t", 1, MAX_HOPS) == (1, True)  # reading stores nothing

    def failing(self, *args, **kwargs):
        raise TurnError("bot is busy")

    with monkeypatch.context() as patch:
        patch.setattr(Turns, "post_message", failing)
        with pytest.raises(DeskError, match="busy"):
            post_inbox(to="Scout", text="hi", sender="CoS", thread_id="t", hop=1, data_dir=tmp_path, providers=providers)
    # the failed enqueue consumed no hop
    assert store.bridge_inbox_hop("t", 1, MAX_HOPS) == (1, True)
    assert post_inbox(to="Scout", text="hi", sender="CoS", thread_id="t", hop=1, data_dir=tmp_path,
                      providers=providers)[0] == "queued"
    assert store.bridge_inbox_hop("t", 1, MAX_HOPS) == (2, True)
    # a hop that another message stored meanwhile conflicts instead of being reused
    with pytest.raises(BridgeHopConflict):
        store.enqueue_message(bot, "late", "late", None, "inbox", "CoS", None, None, bridge_hop=("t", 1))


def _rows(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "agentdesk.sqlite3")
    return db.execute("SELECT bot, state, target, error FROM bridge_sends ORDER BY id").fetchall()


def test_failed_and_refused_sends_are_recorded_but_not_counted(tmp_path, monkeypatch):
    from botoffice import grok_bridge

    monkeypatch.setenv("BOT_OFFICE_GROK_PER_BOT_DAILY", "30")
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    ok = lambda request, timeout=15: FakeResponse(200)  # noqa: E731
    # no config yet: refused before any quota is used
    with pytest.raises(DeskError):
        send_to_grok(to="CoS", kind="fyi", text="x", from_bot="A", data_dir=tmp_path, opener=ok, now=now)
    with pytest.raises(DeskError, match="kind must be"):
        send_to_grok(to="CoS", kind="bogus", text="x", from_bot="A", data_dir=tmp_path, opener=ok, now=now)
    _config(tmp_path)

    def refused(request, timeout=15):
        raise ConnectionRefusedError()

    with pytest.raises(grok_bridge.WebhookNotSent):
        send_to_grok(to="CoS", kind="fyi", text="x", from_bot="A", data_dir=tmp_path, opener=refused, now=now)
    with pytest.raises(grok_bridge.WebhookUnsure):
        send_to_grok(to="CoS", kind="fyi", text="x", from_bot="B", data_dir=tmp_path,
                     opener=lambda request, timeout=15: (_ for _ in ()).throw(TimeoutError("t")), now=now)
    assert send_to_grok(to="CoS", kind="fyi", text="x", from_bot="A", data_dir=tmp_path, opener=ok, now=now) == "sent"
    assert send_to_grok(to="CoS", kind="fyi", text="y", from_bot="A", data_dir=tmp_path, opener=ok,
                        now=now + timedelta(seconds=5)).startswith("dropped: rate limit")
    rows = _rows(tmp_path)
    assert [(r[0], r[1]) for r in rows] == [("A", "failed")] * 3 + [("B", "sent"), ("A", "sent"), ("A", "failed")]
    assert all(r[2] == "CoS" for r in rows)
    assert rows[0][3]
    assert rows[1][3].startswith("kind must be")
    assert rows[3][3].startswith("unsure:")  # delivery unknown: counted, with the note
    assert rows[5][3].startswith("dropped: rate limit")
    # the caps count only sent/reserved rows: three failed rows left bot A's quota untouched
    store = JobStore(tmp_path / "agentdesk.sqlite3")
    res, _hop, why = store.bridge_reserve("A", None, now + timedelta(seconds=90), per_bot_seconds=60,
                                          daily_limit=3, max_hops=6)
    assert res is not None and why is None  # 2 counted rows (B unsure-sent, A sent), not 6
    assert store.bridge_reserve("Z", None, now + timedelta(seconds=95), per_bot_seconds=60, daily_limit=3,
                                max_hops=6)[2] == "daily limit of 3 sends reached"


def test_old_bridge_sends_table_gains_columns(tmp_path):
    import sqlite3
    path = tmp_path / "agentdesk.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE bridge_sends (id INTEGER PRIMARY KEY, bot TEXT NOT NULL, ts TEXT NOT NULL, "
               "state TEXT NOT NULL DEFAULT 'reserved')")
    db.execute("INSERT INTO bridge_sends (bot, ts, state) VALUES ('A', '2026-11-13T12:00:00+00:00', 'sent')")
    db.commit()
    db.close()
    JobStore(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(bridge_sends)")}
    assert {"target", "error"} <= cols
    assert sqlite3.connect(path).execute("SELECT state, error FROM bridge_sends").fetchall() == [("sent", None)]


def test_desk_grok_logs_a_send_that_could_not_reach_the_controller(tmp_path):
    JobStore(tmp_path / "agentdesk.sqlite3")  # an existing database (a missing one is never created)
    env = {"BOT_OFFICE_API_URL": "http://127.0.0.1:9", "BOT_OFFICE_TOKEN_FILE": str(tmp_path / "token"),
           "BOT_OFFICE_BOT_ID": ""}
    (tmp_path / "token").write_text("t")
    with pytest.raises(DeskError, match="unreachable"):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "hi", "--from", "Sec"],
                  data_dir=tmp_path, environ=env)
    rows = _rows(tmp_path)
    assert len(rows) == 1 and rows[0][:3] == ("Sec", "failed", "CoS") and "unreachable" in rows[0][3]
    empty = tmp_path / "other"
    empty.mkdir()
    (empty / "token").write_text("t")
    with pytest.raises(DeskError):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "hi", "--from", "Sec"], data_dir=empty,
                  environ={**env, "BOT_OFFICE_TOKEN_FILE": str(empty / "token")})
    assert not (empty / "agentdesk.sqlite3").exists()


# --- r2 review: every refused or dropped send is audited; uncertain controller outcomes are not "failed" ---


def test_ack_and_malformed_config_are_audited_not_counted(tmp_path):
    ok = lambda request, timeout=15: FakeResponse(200)  # noqa: E731
    _config(tmp_path)
    assert send_to_grok(to="CoS", kind="ack", text="ok", from_bot="A", data_dir=tmp_path, opener=ok).startswith("dropped")
    from botoffice.grok_bridge import config_path
    for raw in ('{"url": 5, "key": "k"}', '["x"]', '{"url": "https://[bad", "key": "k"}'):
        config_path(tmp_path).write_text(raw)
        with pytest.raises(DeskError):
            send_to_grok(to="CoS", kind="fyi", text="x", from_bot="A", data_dir=tmp_path, opener=ok)
    rows = _rows(tmp_path)
    assert [(r[0], r[1]) for r in rows] == [("A", "dropped"), ("A", "failed"), ("A", "failed"), ("A", "failed")]
    assert "malformed" in rows[1][3] and "malformed" in rows[2][3] and "malformed" in rows[3][3]


def test_api_refusals_reach_the_audit(controller, monkeypatch):
    # an actually empty --text, an oversized text and a bad --from: all logged, none counted
    with pytest.raises(DeskError):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "", "--from", "Ops"])
    with pytest.raises(DeskError):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "x" * 10001, "--from", "Ops"])
    _agent_env(monkeypatch, controller.store, "Overflow")
    with pytest.raises(DeskError):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "x", "--from", "Someone Else"])
    rows = sqlite3.connect(controller.store.path).execute(
        "SELECT bot, state, error FROM bridge_sends ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("Ops", "failed")] * 2 + [("Overflow", "failed")]
    assert "non-empty" in rows[0][2] and "too long" in rows[1][2] and "master token" in rows[2][2]
    assert controller.sent == []


def _cli_env(tmp_path):
    JobStore(tmp_path / "agentdesk.sqlite3")
    (tmp_path / "token").write_text("t")
    return {"BOT_OFFICE_API_URL": "http://127.0.0.1:9", "BOT_OFFICE_TOKEN_FILE": str(tmp_path / "token"),
            "BOT_OFFICE_BOT_ID": ""}


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), ValueError("not json")])
def test_uncertain_controller_outcome_is_unsure_not_failed(tmp_path, monkeypatch, failure):
    from botoffice import grok_bridge
    import urllib.error

    def boom(request, timeout=30):
        raise urllib.error.URLError(failure) if isinstance(failure, TimeoutError) else failure

    monkeypatch.setattr(grok_bridge._NO_REDIRECTS, "open", boom)
    with pytest.raises(DeskError, match="may have been sent"):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "hi", "--from", "Sec"],
                  data_dir=tmp_path, environ=_cli_env(tmp_path))
    rows = _rows(tmp_path)
    assert [(r[0], r[1]) for r in rows] == [("Sec", "unsure")]  # never a definite failed send


def test_connection_refused_stays_a_definite_failed_send(tmp_path):
    with pytest.raises(DeskError, match="unreachable"):
        desk_grok(["send", "--to", "CoS", "--kind", "fyi", "--text", "hi", "--from", "Sec"],
                  data_dir=tmp_path, environ=_cli_env(tmp_path))
    assert [(r[0], r[1]) for r in _rows(tmp_path)] == [("Sec", "failed")]


def test_concurrent_initialisers_of_an_old_database_both_succeed(tmp_path):
    import threading
    path = tmp_path / "agentdesk.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE bridge_sends (id INTEGER PRIMARY KEY, bot TEXT NOT NULL, ts TEXT NOT NULL, "
               "state TEXT NOT NULL DEFAULT 'reserved')")
    db.commit()
    db.close()
    errors = []
    barrier = threading.Barrier(4)

    def init():
        barrier.wait()
        try:
            JobStore(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=init) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(bridge_sends)")} >= {"target", "error"}
