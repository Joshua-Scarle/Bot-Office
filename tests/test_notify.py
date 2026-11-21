"""Email notifications (controller side): the secret-free event summary, the per-class toggles and the
authenticated push to the watchdog Worker."""
import http.server
import json
import threading
from contextlib import closing
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from botoffice import health, notify
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.store import now

H = {"Authorization": "Bearer test-token"}
SECRET_TEXT = "Bearer abcdefghijklmnop1234 and token=zzzzzzzz9999"


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch, tmp_path):
    monkeypatch.delenv(notify.URL_ENV, raising=False)
    monkeypatch.setenv(health.BACKUP_STATUS_ENV, str(tmp_path / "status.json"))


def _client(settings):
    return TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))


def _set(store, table, row_id, **fields):
    marks = ", ".join(f"{key}=?" for key in fields)
    with closing(store._connect()) as db:
        db.execute(f"UPDATE {table} SET {marks} WHERE id=?", (*fields.values(), str(row_id)))


def _job(store, bot, status, error=None, kind=None, source="routine", prompt="PRIVATE PROMPT BODY"):
    job = store.create(prompt, "codex", bot_id=bot.id, source=source)
    _set(store, "jobs", job.id, status=status, failure_kind=kind, error=error, finished_at=now().isoformat())
    return job


def test_prefs_default_on_for_the_four_alert_classes_and_off_for_done(settings):
    with _client(settings) as client:
        store = client.app.state.store
        assert notify.get_prefs(store) == {"needs_you": True, "failed": True, "outage": True, "backup": True,
                                           "done": False}
        got = client.get("/v1/settings", headers=H).json()["notifications"]
        assert got["email"]["needs_you"] is True and got["email"]["done"] is False and got["configured"] is False
        put = client.put("/v1/settings", headers=H, json={"notifications": {"failed": False, "done": True}})
        assert put.status_code == 200
        assert put.json()["notifications"]["email"] == {"needs_you": True, "failed": False, "outage": True,
                                                         "backup": True, "done": True}
        # persisted, and a partial update leaves the rest alone
        client.put("/v1/settings", headers=H, json={"notifications": {"outage": False}})
        assert notify.get_prefs(store) == {"needs_you": True, "failed": False, "outage": False, "backup": True,
                                           "done": True}
        assert client.put("/v1/settings", headers=H, json={"notifications": {"bogus": True}}).status_code == 422
        assert client.put("/v1/settings", json={"notifications": {"done": False}}).status_code in (401, 403)


def test_corrupt_stored_prefs_fall_back_to_defaults(settings):
    with _client(settings) as client:
        store = client.app.state.store
        store.set_meta(notify.META_KEY, "[1, 2")
        assert notify.get_prefs(store)["needs_you"] is True
        store.set_meta(notify.META_KEY, json.dumps({"needs_you": "no", "failed": False}))
        assert notify.get_prefs(store)["needs_you"] is True and notify.get_prefs(store)["failed"] is False


def test_events_hold_counts_names_ids_and_a_redacted_reason_never_bodies(settings, tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"state": "failed", "finished": now().isoformat(),
                                                      "error": "upload failed " + SECRET_TEXT}))
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Ada", "PRIVATE INSTRUCTIONS", "codex", None)
        failed = _job(store, bot, "failed", error="provider said no " + SECRET_TEXT, kind="provider_error")
        ok = _job(store, bot, "succeeded", source="chat")
        store.create("a question", "codex", bot_id=bot.id, source="chat")
        events = notify.build_events(store)
    text = json.dumps(events)
    assert "PRIVATE" not in text and "abcdefghijklmnop1234" not in text and "zzzzzzzz9999" not in text
    assert "a question" not in text
    assert events["failed"]["count"] == 1
    (item,) = events["failed"]["items"]
    assert item["bot"] == "Ada" and item["job"] == str(failed.id) and item["kind"] == "provider_error"
    assert item["id"] == f"j:{failed.id}:{store.get(failed.id).attempts}" and len(item["reason"]) <= 120
    assert "provider said no" in item["reason"] and "[redacted]" in item["reason"]
    assert "done" not in events and str(ok.id) not in text  # done is off by default
    assert events["backup_reason"].startswith("upload failed") and "zzzzzzzz9999" not in events["backup_reason"]
    assert events["prefs"]["needs_you"] is True and events["needs_you"]["count"] == 0


def test_needs_you_items_carry_kind_and_bot_but_not_the_question_text(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Bea", "", "codex", None)
        job = _job(store, bot, "failed", error="boom", kind="provider_error")  # a job failure is the "failed" class
        # a pending question card
        with closing(store._connect()) as db:
            db.execute("INSERT INTO questions (id, bot_id, message_id, status, payload, blocking, created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       ("q1", str(bot.id), 1, "open", json.dumps({"steps": [{"prompt": "SECRET QUESTION TEXT"}]}),
                        0, now().isoformat()))
        events = notify.build_events(store)
    assert events["needs_you"]["count"] >= 1
    kinds = {i["kind"] for i in events["needs_you"]["items"]}
    assert "question open" in kinds
    assert "SECRET QUESTION TEXT" not in json.dumps(events)
    assert all(not i["id"].startswith("j:") for i in events["needs_you"]["items"])
    assert str(job.id) in json.dumps(events["failed"])


def test_disabled_classes_are_not_built(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Cy", "", "codex", None)
        _job(store, bot, "failed", error="x", kind="auth")
        notify.set_prefs(store, {"failed": False, "needs_you": False, "backup": False, "done": True})
        _job(store, bot, "succeeded", source="chat")
        events = notify.build_events(store)
    assert "failed" not in events and "needs_you" not in events and "backup_reason" not in events
    assert events["done"]["count"] == 1 and events["prefs"]["failed"] is False


def test_old_failures_leave_the_window_and_items_are_capped(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Di", "", "codex", None)
        old = _job(store, bot, "failed", error="old", kind="auth")
        _set(store, "jobs", old.id, finished_at=(now() - timedelta(hours=30)).isoformat())
        for _ in range(notify.MAX_ITEMS + 5):
            _job(store, bot, "failed", error="new", kind="auth")
        events = notify.build_events(store)
    assert events["failed"]["count"] == notify.MAX_ITEMS + 5
    assert len(events["failed"]["items"]) == notify.MAX_ITEMS
    assert all(item["job"] != str(old.id) for item in events["failed"]["items"])


def test_events_url_and_unconfigured_notifier(settings):
    assert notify.events_url("https://w.example/heartbeat") == "https://w.example/events"
    assert notify.events_url("https://w.example/x/heartbeat?a=1") == "https://w.example/x/events"
    with _client(settings) as client:
        notifier = notify.Notifier(client.app.state.store, url="")
        assert notifier.configured is False
        assert notify.Notifier(client.app.state.store, url="http://insecure.example/heartbeat").configured is False


def _secret(tmp_path, text="s3cret-value", mode=0o600):
    path = tmp_path / "secret"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_push_posts_the_summary_with_a_bearer_and_never_follows_redirects(settings, tmp_path, monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return httpx.Response(200)

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    with _client(settings) as client:
        notifier = notify.Notifier(client.app.state.store, url="https://w.example/heartbeat",
                                   secret_file=_secret(tmp_path))
        assert notifier.push_once() is True
    assert seen["url"] == "https://w.example/events"
    assert seen["follow_redirects"] is False and seen["headers"]["Authorization"] == "Bearer s3cret-value"
    assert set(seen["json"]) >= {"sent_at", "prefs", "needs_you", "failed", "backup_reason"}


def test_push_failures_are_quiet_sanitised_and_never_print_the_secret(settings, tmp_path, monkeypatch, caplog):
    with _client(settings) as client:
        store = client.app.state.store
        url = "https://w.example/heartbeat"
        for status in (302, 401, 500):
            monkeypatch.setattr(notify.httpx, "post", lambda *a, _s=status, **k: httpx.Response(_s))
            assert notify.Notifier(store, url=url, secret_file=_secret(tmp_path)).push_once() is False

        def boom(*a, **k):
            raise httpx.ConnectError("could not reach https://w.example/events with s3cret-value")

        monkeypatch.setattr(notify.httpx, "post", boom)
        assert notify.Notifier(store, url=url, secret_file=_secret(tmp_path)).push_once() is False
        # a missing file or a world-readable one: refused with a message that names no path, nothing sent
        sent = []
        monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: sent.append(1) or httpx.Response(200))
        assert notify.Notifier(store, url=url, secret_file=tmp_path / "nope").push_once() is False
        assert notify.Notifier(store, url=url, secret_file=_secret(tmp_path, mode=0o644)).push_once() is False
        assert sent == []
    log = caplog.text
    assert "s3cret-value" not in log and str(tmp_path) not in log
    assert "redirect refused" in log and "ConnectError" in log


def test_push_does_not_follow_a_real_redirect(settings, tmp_path):
    hits = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    class Origin(Sink):
        def do_POST(self):  # noqa: N802
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/steal")
            self.end_headers()

    sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    for server in (sink, origin):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with _client(settings) as client:
            notifier = notify.Notifier(client.app.state.store, url=f"http://127.0.0.1:{origin.server_port}/heartbeat",
                                       secret_file=_secret(tmp_path))
            assert notifier.push_once() is False
    finally:
        sink.shutdown()
        origin.shutdown()
    assert hits == []


def test_app_pushes_in_the_background_only_when_configured(settings, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify.Notifier, "push_once", lambda self: calls.append(1) or True)
    with _client(settings):
        pass
    assert calls == []  # no WATCHDOG_URL
    monkeypatch.setenv(notify.URL_ENV, "https://w.example/heartbeat")
    monkeypatch.setattr(notify, "INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(notify.Notifier, "__init__", (lambda orig: lambda self, store, **kw: orig(self, store, interval=0.05))(
        notify.Notifier.__init__))
    import time
    with _client(settings):
        time.sleep(0.4)
    assert len(calls) >= 2
