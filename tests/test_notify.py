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


def _rows(store):
    with closing(store._connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM notify_events ORDER BY seq")]


def _question(store, bot, qid="q1", prompt="SECRET QUESTION TEXT"):
    with closing(store._connect()) as db:
        db.execute("INSERT INTO questions (id, bot_id, message_id, status, payload, blocking, created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (qid, str(bot.id), 1, "open", json.dumps({"steps": [{"prompt": prompt}]}), 0, now().isoformat()))


def test_events_are_appended_when_they_happen_and_carry_codes_never_text(settings, tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"state": "failed", "finished": now().isoformat(),
                                                      "error": "password: abc " + SECRET_TEXT, "stage": "upload"}))
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Ada", "PRIVATE INSTRUCTIONS", "codex", None)
        failed = _job(store, bot, "failed", error="provider said no " + SECRET_TEXT, kind="provider_error")
        _job(store, bot, "succeeded", source="chat")
        _question(store, bot)
        payload, upto, more = notify.build_batch(store)
    text = json.dumps(payload)
    for leak in ("PRIVATE", "abcdefghijklmnop1234", "zzzzzzzz9999", "password", "provider said no", "SECRET QUESTION"):
        assert leak not in text
    by = {e["cls"]: e for e in payload["events"]}
    assert set(by) == {"failed", "needs_you"}  # done is off by default
    assert by["failed"]["bot"] == "Ada" and by["failed"]["job"] == str(failed.id) and by["failed"]["kind"] == "provider_error"
    assert by["failed"]["id"] == f"j:{failed.id}:{store.get(failed.id).attempts}"
    assert by["needs_you"]["kind"] == "question_open" and by["needs_you"]["id"] == "q:q1:open"
    assert payload["backup_reason"] == "stage_upload"
    assert payload["prefs"]["needs_you"] is True and more is False and upto == 3
    assert not any("reason" in e for e in payload["events"])


def test_free_text_never_leaves_even_in_names_kinds_and_backup_errors(settings, tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"state": "failed", "stage": "hunter2 and a sentence",
                                                      "error": "password: hunter2 for alice@example.com"}))
    with _client(settings) as client:
        store = client.app.state.store
        sentence = store.create_bot("my password is hunter2 please call 555-0100", "", "codex", None)
        pw = store.create_bot("password: abc", "", "codex", None)
        plain = store.create_bot("Ada-2", "", "codex", None)
        _job(store, sentence, "failed", error="password: abc", kind="password: abc")
        _job(store, pw, "failed", error="a sentence with private content.", kind="a sentence")
        _job(store, plain, "failed", error="x", kind="auth")
        payload, _, _ = notify.build_batch(store)
    text = json.dumps(payload)
    for leak in ("hunter2", "555", "password", "sentence", "alice", "private content"):
        assert leak not in text
    bots = {e["bot"] for e in payload["events"]}
    assert "Ada-2" in bots and sum(b.startswith("bot-") for b in bots) == 2
    assert {e["kind"] for e in payload["events"]} == {"other", "auth"}
    assert payload["backup_reason"] == "failed"
    assert notify.bot_label("id-1", "x" * 40).startswith("bot-") and notify.bot_label("id-1", "Ann") == "Ann"


def test_a_job_that_failed_and_was_dismissed_within_a_minute_is_still_logged(settings):
    """The trigger records the transition itself; no poll has to catch the state."""
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Bea", "", "codex", None)
        job = _job(store, bot, "failed", error="boom", kind="auth")
        _set(store, "jobs", job.id, status="succeeded", dismissed_at=now().isoformat())  # gone from every list
        rows = _rows(store)
    assert [r["cls"] for r in rows] == ["failed", "done"]


def test_detector_logs_derived_needs_you_once_and_survives_resolution(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Cy", "", "codex", None)
        _question(store, bot, "q9")
        assert notify.record_needs_you(store) == 0  # the question trigger already logged it
        assert [r["key"] for r in _rows(store)] == ["q:q9:open"]
        with closing(store._connect()) as db:
            db.execute("UPDATE questions SET status='answered' WHERE id='q9'")
        assert [r["key"] for r in _rows(store)] == ["q:q9:open"]  # resolved items stay in the log until acked
        notify.set_prefs(store, {"needs_you": False})
        assert notify.record_needs_you(store) == 0


def test_batches_hold_the_cursor_until_the_worker_acks(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Di", "", "codex", None)
        for _ in range(notify.BATCH + 30):
            _job(store, bot, "failed", error="x", kind="auth")
        first, upto, more = notify.build_batch(store)
        assert len(first["events"]) == notify.BATCH and more is True and upto == notify.BATCH
        again, _, _ = notify.build_batch(store)
        assert [e["seq"] for e in again["events"]] == [e["seq"] for e in first["events"]]  # nothing moved: no ack yet
        notify._ack(store, 40, upto)  # a partial ack (the Worker was full)
        nxt, upto2, more2 = notify.build_batch(store)
        assert nxt["events"][0]["seq"] == 41 and len(nxt["events"]) == 90 and more2 is False
        notify._ack(store, 10**9, upto2)  # a bogus ack beyond what was sent is ignored
        assert notify._cursor(store) == 40
        notify._ack(store, upto2, upto2)
        last, _, more3 = notify.build_batch(store)
        assert last["events"] == [] and more3 is False


def test_disabled_classes_are_skipped_not_sent(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Ed", "", "codex", None)
        _job(store, bot, "failed", error="x", kind="auth")
        _job(store, bot, "succeeded", source="chat")
        notify.set_prefs(store, {"failed": False, "backup": False, "done": True})
        payload, upto, _ = notify.build_batch(store)
    assert [e["cls"] for e in payload["events"]] == ["done"]
    assert payload["backup_reason"] is None and upto == 2  # the failed event is covered by the cursor, not sent


def test_events_survive_a_long_push_outage_and_are_delivered_in_order(settings, tmp_path, monkeypatch):
    """500 events while every push fails; then the Worker takes 100 at a time (and only 60 once) and acks."""
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Fay", "", "codex", None)
        for _ in range(500):
            _job(store, bot, "failed", error="x", kind="auth")
        notifier = notify.Notifier(store, url="https://w.example/heartbeat", secret_file=_secret(tmp_path))
        for _ in range(3):
            assert notifier.push_once() == (False, False)
        total = len(_rows(store))  # 500 failures, plus the "failed three times in a row" needs-you item
        assert total == 501 and notify._cursor(store) == 0
        received, caps = [], iter([60] + [100] * 20)

        def worker(url, json=None, **kw):
            take = json["events"][: next(caps)]
            received.extend(e["seq"] for e in take)
            ack = take[-1]["seq"] if take else json["upto"]
            return httpx.Response(200, json={"ack": ack})

        monkeypatch.setattr(notify.httpx, "post", worker)
        for _ in range(20):
            ok, more = notifier.push_once()
            assert ok
            if not more:
                break
    assert received == list(range(1, total + 1)) and notify._cursor(store) == total


def test_old_events_are_pruned_and_an_unconfigured_notifier_keeps_one_day(settings):
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Hal", "", "codex", None)
        old = _job(store, bot, "failed", error="x", kind="auth")
        _job(store, bot, "failed", error="x", kind="auth")
        with closing(store._connect()) as db:
            db.execute("UPDATE notify_events SET created_at=? WHERE ref=?", ("2020-01-01T00:00:00Z", str(old.id)))
        notify.prune(store, notify.KEEP_UNCONFIGURED)
        assert [r["ref"] for r in _rows(store)] != [str(old.id)] and len(_rows(store)) == 1


def test_a_worker_that_acks_nothing_does_not_spin_the_loop(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: httpx.Response(200, json={"ack": 0}))
    with _client(settings) as client:
        store = client.app.state.store
        bot = store.create_bot("Gus", "", "codex", None)
        for _ in range(notify.BATCH + 5):
            _job(store, bot, "failed", error="x", kind="auth")
        assert notify.Notifier(store, url="https://w.example/heartbeat", secret_file=_secret(tmp_path)).push_once() == (True, False)


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
        assert notifier.push_once()[0] is True
    assert seen["url"] == "https://w.example/events"
    assert seen["follow_redirects"] is False and seen["headers"]["Authorization"] == "Bearer s3cret-value"
    assert set(seen["json"]) == {"sent_at", "epoch", "prefs", "backup_reason", "upto", "events"}


def test_push_failures_are_quiet_sanitised_and_never_print_the_secret(settings, tmp_path, monkeypatch, caplog):
    with _client(settings) as client:
        store = client.app.state.store
        url = "https://w.example/heartbeat"
        for status in (302, 401, 500):
            monkeypatch.setattr(notify.httpx, "post", lambda *a, _s=status, **k: httpx.Response(_s))
            assert notify.Notifier(store, url=url, secret_file=_secret(tmp_path)).push_once()[0] is False

        def boom(*a, **k):
            raise httpx.ConnectError("could not reach https://w.example/events with s3cret-value")

        monkeypatch.setattr(notify.httpx, "post", boom)
        assert notify.Notifier(store, url=url, secret_file=_secret(tmp_path)).push_once()[0] is False
        # a missing file or a world-readable one: refused with a message that names no path, nothing sent
        sent = []
        monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: sent.append(1) or httpx.Response(200))
        assert notify.Notifier(store, url=url, secret_file=tmp_path / "nope").push_once()[0] is False
        assert notify.Notifier(store, url=url, secret_file=_secret(tmp_path, mode=0o644)).push_once()[0] is False
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
            assert notifier.push_once()[0] is False
    finally:
        sink.shutdown()
        origin.shutdown()
    assert hits == []


def test_app_pushes_in_the_background_only_when_configured(settings, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify.Notifier, "push_once", lambda self: calls.append(1) or (True, False))
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
