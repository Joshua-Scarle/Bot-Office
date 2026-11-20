import importlib.util
import json
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
                     error="boom /home/ubuntu/.secret-path token=abc")
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
