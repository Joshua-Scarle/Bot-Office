import ast
import asyncio
import http.server
import importlib.util
import json
import os
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import health
from botoffice.api import create_app

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _status(tmp_path, **fields):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(fields))
    return path


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_healthz_shape_no_auth_and_no_secrets(settings, tmp_path, monkeypatch):
    status = _status(tmp_path, state="ok", started=_iso(3), finished=_iso(2), bytes=123,
                     error="boom /srv/x/.secret-path token=abc")
    monkeypatch.setenv(health.BACKUP_STATUS_ENV, str(status))
    with TestClient(create_app(settings)) as client:
        resp = client.get("/healthz")  # no Authorization header
        assert resp.status_code == 200
        body = resp.json()
        text = resp.text
    assert set(body) == {"ok", "version", "uptime_seconds", "scheduler", "db_reachable",
                         "running_jobs", "backup", "disk_free_pct"}
    assert body["db_reachable"] is True and body["running_jobs"] == 0
    assert set(body["backup"]) == {"state", "age_hours", "bytes", "stale"}
    assert body["backup"]["state"] == "ok"
    assert isinstance(body["disk_free_pct"], float)
    assert "test-token" not in text
    assert str(settings.data_dir) not in text and str(tmp_path) not in text
    assert "boom" not in text and "secret-path" not in text


def test_healthz_scheduler_alive_after_first_tick(settings, tmp_path, monkeypatch):
    monkeypatch.setenv(health.BACKUP_STATUS_ENV, str(tmp_path / "missing.json"))
    with TestClient(create_app(settings)) as client:
        body = client.get("/healthz").json()
    assert body["scheduler"]["alive"] is True
    assert body["scheduler"]["last_tick_age_seconds"] < 5
    assert body["ok"] is True
    assert body["backup"]["state"] == "unknown" and body["backup"]["stale"] is True


def test_healthz_db_failure_is_reported_not_raised(settings, tmp_path, monkeypatch):
    monkeypatch.setenv(health.BACKUP_STATUS_ENV, str(tmp_path / "missing.json"))
    app = create_app(settings)
    with TestClient(app) as client:
        def broken():
            raise RuntimeError("db gone")
        monkeypatch.setattr(app.state.store, "running_job_ids", broken)
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["db_reachable"] is False and resp.json()["ok"] is False


def test_backup_state_and_staleness(tmp_path):
    ok = health.backup_status(_status(tmp_path, state="ok", finished=_iso(5), bytes=9), NOW)
    assert (ok["state"], ok["age_hours"], ok["stale"], ok["bytes"]) == ("ok", 5.0, False, 9)
    edge = health.backup_status(_status(tmp_path, state="ok", finished=_iso(30)), NOW)
    assert edge["stale"] is False
    old = health.backup_status(_status(tmp_path, state="ok", finished=_iso(30.5)), NOW)
    assert old["stale"] is True
    failed = health.backup_status(_status(tmp_path, state="failed", finished=_iso(1), error="x"), NOW)
    assert failed["state"] == "failed" and failed["stale"] is True
    nc = health.backup_status(_status(tmp_path, state="not_configured"), NOW)
    assert nc["state"] == "not_configured" and nc["age_hours"] is None and nc["stale"] is True
    assert health.backup_status(_status(tmp_path, state="weird", finished=_iso(1)), NOW)["state"] == "unknown"
    no_time = health.backup_status(_status(tmp_path, state="ok", finished="garbage"), NOW)
    assert no_time["stale"] is True


def test_backup_status_missing_or_corrupt_file(tmp_path):
    assert health.backup_status(tmp_path / "nope.json", NOW)["state"] == "unknown"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert health.backup_status(bad, NOW)["state"] == "unknown"
    bad.write_text("[1,2]")
    assert health.backup_status(bad, NOW)["state"] == "unknown"


# ---- heartbeat pusher ----

def _load_pusher():
    path = Path(__file__).resolve().parents[1] / "scripts" / "healthz_push.py"
    spec = importlib.util.spec_from_file_location("healthz_push", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pusher_payload_wraps_healthz_and_marks_down_controller():
    push = _load_pusher()
    body = {"ok": True, "version": "1"}
    payload = push.build_payload(body, NOW)
    assert payload == {"sent_at": "2026-11-20T12:00:00Z", "controller_reachable": True, "healthz": body}
    down = push.build_payload(None, NOW)
    assert down["controller_reachable"] is False and down["healthz"] is None


def test_pusher_reads_secret_only_from_a_0600_file(tmp_path):
    push = _load_pusher()
    secret = tmp_path / "secret"
    secret.write_text("s3cret-value\n")
    secret.chmod(0o644)
    with pytest.raises(push.ConfigError) as err:
        push.read_secret(secret)
    assert "s3cret-value" not in str(err.value)
    secret.chmod(0o600)
    assert push.read_secret(secret) == "s3cret-value"
    secret.write_text("\n")
    with pytest.raises(push.ConfigError):
        push.read_secret(secret)
    with pytest.raises(push.ConfigError):
        push.read_secret(tmp_path / "absent")


def test_pusher_main_posts_bearer_and_never_prints_secret(tmp_path, monkeypatch, capsys):
    push = _load_pusher()
    secret = tmp_path / "secret"
    secret.write_text("s3cret-value")
    secret.chmod(0o600)
    monkeypatch.setenv("WATCHDOG_URL", "https://watchdog.example/heartbeat")
    monkeypatch.setenv("WATCHDOG_SECRET_FILE", str(secret))
    monkeypatch.setattr(push, "fetch_healthz", lambda url: {"ok": True})
    seen = {}

    def fake_post(url, token, payload, timeout=0):
        seen.update(url=url, token=token, payload=payload)
        return 200

    monkeypatch.setattr(push, "post", fake_post)
    assert push.main() == 0
    assert seen["token"] == "s3cret-value" and seen["payload"]["healthz"] == {"ok": True}
    out = capsys.readouterr()
    assert "s3cret-value" not in out.out + out.err

    monkeypatch.setattr(push, "post", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 401, "no", {}, None)))
    assert push.main() == 1
    out = capsys.readouterr()
    assert "s3cret-value" not in out.out + out.err


def test_pusher_config_errors(tmp_path, monkeypatch):
    push = _load_pusher()
    monkeypatch.delenv("WATCHDOG_URL", raising=False)
    assert push.main() == 2
    monkeypatch.setenv("WATCHDOG_URL", "http://insecure.example/")
    assert push.main() == 2
    monkeypatch.setenv("WATCHDOG_URL", "https://w.example/heartbeat")
    monkeypatch.setenv("WATCHDOG_SECRET_FILE", str(tmp_path / "absent"))
    assert push.main() == 2


def test_pusher_fetch_healthz_unreachable_is_none():
    push = _load_pusher()
    assert push.fetch_healthz("http://127.0.0.1:9/healthz", timeout=1) is None


# ---- review fixes: malformed / future status, blocking work, redirects, secret errors ----

def test_backup_status_malformed_field_types_are_unknown_not_a_crash(tmp_path):
    for state in ([1], {"a": 1}, 5, None, True):
        body = health.backup_status(_status(tmp_path, state=state, finished=_iso(1)), NOW)
        assert body["state"] == "unknown" and body["stale"] is True
    for finished in ([1], {"a": 1}, 5, None):
        body = health.backup_status(_status(tmp_path, state="ok", finished=finished), NOW)
        assert body["state"] == "ok" and body["age_hours"] is None and body["stale"] is True
    body = health.backup_status(_status(tmp_path, state="ok", finished=_iso(1), bytes=[1]), NOW)
    assert body["bytes"] is None and body["stale"] is False


def test_backup_status_future_dated_is_unknown_and_stale_not_fresh(tmp_path):
    far = health.backup_status(_status(tmp_path, state="ok", finished=_iso(-48)), NOW)
    assert far["state"] == "unknown" and far["age_hours"] is None and far["stale"] is True
    skew = health.backup_status(_status(tmp_path, state="ok", finished=_iso(-0.05)), NOW)  # 3 min: clock skew
    assert skew["age_hours"] == 0.0 and skew["stale"] is False


def test_backup_status_file_must_be_a_regular_file_and_never_blocks(tmp_path):
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)  # a plain open() would hang forever with no writer
    started = time.monotonic()
    assert health.backup_status(fifo, NOW)["state"] == "unknown"
    assert health.backup_status(tmp_path, NOW)["state"] == "unknown"  # a directory
    assert time.monotonic() - started < 2
    assert health.backup_reason(fifo) is None


def test_backup_reason_is_redacted_and_short(tmp_path):
    reason = health.backup_reason(_status(tmp_path, state="failed",
                                          error="rclone failed token=abcdef123456 " + "x" * 500))
    assert "abcdef123456" not in reason and len(reason) <= 120
    assert health.backup_reason(_status(tmp_path, state="failed", error=["x"])) is None


def test_healthz_is_bounded_and_off_the_event_loop(settings, tmp_path, monkeypatch):
    monkeypatch.setenv(health.BACKUP_STATUS_ENV, str(tmp_path / "missing.json"))
    monkeypatch.setattr(health, "HEALTH_TIMEOUT_SECONDS", 0.3)
    release, calls = threading.Event(), []
    app = create_app(settings)
    with TestClient(app) as client:
        def stuck():
            calls.append(1)
            release.wait(10)
            return []
        monkeypatch.setattr(app.state.store, "running_job_ids", stuck)
        started = time.monotonic()
        first = client.get("/healthz")
        second = client.get("/healthz")  # joins the run still in flight; no second thread piles up
        elapsed = time.monotonic() - started
        release.set()
    assert first.status_code == second.status_code == 503
    assert first.json()["ok"] is False and first.json()["timed_out"] is True
    assert elapsed < 3 and len(calls) == 1


def test_healthz_does_not_stall_the_event_loop(settings, tmp_path, monkeypatch):
    """While the database call is stuck, the loop keeps serving other requests."""
    monkeypatch.setenv(health.BACKUP_STATUS_ENV, str(tmp_path / "missing.json"))
    monkeypatch.setattr(health, "HEALTH_TIMEOUT_SECONDS", 1.5)
    release = threading.Event()
    app = create_app(settings)

    async def scenario():
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            app.state.store.running_job_ids = lambda: release.wait(10) or []
            slow = asyncio.create_task(http.get("/healthz"))
            await asyncio.sleep(0.2)
            started = time.monotonic()
            other = await http.get("/v1/settings", headers={"Authorization": "Bearer test-token"})
            quick = time.monotonic() - started
            release.set()
            await slow
            return other.status_code, quick

    with TestClient(app):  # starts the app's lifespan; the async client then talks to the same app
        status, quick = asyncio.run(scenario())
    assert status == 200 and quick < 1.0


class _Redirector(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Redirector.seen.append(("origin", self.headers.get("Authorization")))
        self.send_response(307)
        self.send_header("Location", self.server.target)
        self.end_headers()

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args):
        pass


class _Sink(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        _Sink.seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args):
        pass


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_pusher_never_follows_a_redirect_with_the_bearer_token():
    push = _load_pusher()
    sink, origin = _serve(_Sink), _serve(_Redirector)
    try:
        # another origin (other port), and the same address downgraded to another scheme: both refused
        for target in (f"http://127.0.0.1:{sink.server_port}/steal", "http://example.invalid/steal"):
            _Sink.seen.clear()
            origin.target = target
            with pytest.raises(urllib.error.HTTPError) as err:
                push.post(f"http://127.0.0.1:{origin.server_port}/heartbeat", "s3cret-value", {"a": 1})
            assert err.value.code == 307
            assert _Sink.seen == []
        # the healthz GET does not follow one either
        origin.target = f"http://127.0.0.1:{sink.server_port}/steal"
        assert push.fetch_healthz(f"http://127.0.0.1:{origin.server_port}/healthz") is None
        assert _Sink.seen == []
    finally:
        sink.shutdown()
        origin.shutdown()


def test_pusher_main_reports_a_refused_redirect(tmp_path, monkeypatch, capsys):
    push = _load_pusher()
    secret = tmp_path / "secret"
    secret.write_text("s3cret-value")
    secret.chmod(0o600)
    origin = _serve(_Redirector)
    origin.target = "https://elsewhere.example/x"
    monkeypatch.setenv("WATCHDOG_URL", "https://watchdog.example/heartbeat")
    monkeypatch.setenv("WATCHDOG_SECRET_FILE", str(secret))
    monkeypatch.setattr(push, "fetch_healthz", lambda url: {"ok": True})
    real_post = push.post
    monkeypatch.setattr(push, "post", lambda url, token, payload, timeout=8: real_post(
        f"http://127.0.0.1:{origin.server_port}/heartbeat", token, payload, timeout))
    try:
        assert push.main() == 1
    finally:
        origin.shutdown()
    out = capsys.readouterr()
    assert "redirect refused" in out.err and "s3cret-value" not in out.out + out.err


def test_pusher_secret_errors_name_no_path_and_have_no_traceback(tmp_path, monkeypatch, capsys):
    push = _load_pusher()
    secret = tmp_path / "private-dir-name" / "watchdog-secret"
    secret.parent.mkdir()
    secret.write_text("s3cret-value")
    secret.chmod(0o600)
    monkeypatch.setenv("WATCHDOG_URL", "https://watchdog.example/heartbeat")
    monkeypatch.setenv("WATCHDOG_SECRET_FILE", str(secret))

    def boom(fd, n):
        raise PermissionError(13, "denied", str(secret))

    monkeypatch.setattr(push.os, "read", boom)  # fails after the open and the checks
    assert push.main() == 2
    out = capsys.readouterr()
    assert "private-dir-name" not in out.out + out.err and "Traceback" not in out.err
    monkeypatch.undo()
    monkeypatch.setenv("WATCHDOG_URL", "https://watchdog.example/heartbeat")
    secret.write_bytes(b"\xff\xfe\x00bad")
    secret.chmod(0o600)
    with pytest.raises(push.ConfigError) as err:
        push.read_secret(secret)
    assert "private-dir-name" not in str(err.value)
    secret.unlink()
    secret.mkdir()
    secret.chmod(0o700)
    with pytest.raises(push.ConfigError):
        push.read_secret(secret)  # a directory, checked on the opened descriptor
    link = tmp_path / "link"
    (tmp_path / "real").write_text("s3cret-value")
    (tmp_path / "real").chmod(0o600)
    link.symlink_to(tmp_path / "real")
    assert push.read_secret(link) == "s3cret-value"  # a symlink to a good file is fine


def test_pusher_runs_on_an_older_system_python():
    """The unit uses the system python3, which may predate datetime.UTC (3.11)."""
    source = (Path(__file__).resolve().parents[1] / "scripts" / "healthz_push.py").read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                and node.module == "datetime" for alias in node.names}
    assert "UTC" not in imported
