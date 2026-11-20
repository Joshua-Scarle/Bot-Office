#!/usr/bin/env python3
"""Heartbeat pusher: GET the local /healthz and POST it to the external watchdog.

Stdlib only. Config (environment):
  BOT_OFFICE_HEALTHZ_URL       default http://127.0.0.1:8787/healthz
  WATCHDOG_URL                 required: the Worker's /heartbeat URL
  WATCHDOG_SECRET_FILE         default ~/.config/bot-office/watchdog-secret (must be mode 0600)
The secret is sent as a bearer token and is never printed. Exit 0 = pushed, 1 = push failed,
2 = configuration problem. A controller that is down is pushed too (controller_reachable=false).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_HEALTHZ = "http://127.0.0.1:8787/healthz"
DEFAULT_SECRET_FILE = "~/.config/bot-office/watchdog-secret"
TIMEOUT = 8
MAX_BODY = 64 * 1024
USER_AGENT = "bot-office-heartbeat/1"  # Cloudflare rejects the default Python-urllib agent


class ConfigError(Exception):
    pass


def read_secret(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise ConfigError(f"secret file unreadable: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ConfigError("secret file must be a regular file with mode 0600")
    secret = path.read_text().strip()
    if not secret:
        raise ConfigError("secret file is empty")
    return secret


def fetch_healthz(url: str, timeout: float = TIMEOUT) -> dict | None:
    """The /healthz body, or None when the controller cannot be reached or answers nonsense."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read(MAX_BODY))
    except urllib.error.HTTPError as exc:  # a 5xx may still carry a body; use it if it parses
        try:
            data = json.loads(exc.read(MAX_BODY))
        except (OSError, ValueError):
            return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_payload(healthz: dict | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    return {
        "sent_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "controller_reachable": healthz is not None,
        "healthz": healthz,
    }


def post(url: str, secret: str, payload: dict, timeout: float = TIMEOUT) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status


def main() -> int:
    url = os.environ.get("WATCHDOG_URL", "").strip()
    if not url.startswith("https://"):
        print("WATCHDOG_URL must be set to the Worker's https:// heartbeat URL", file=sys.stderr)
        return 2
    secret_file = Path(os.environ.get("WATCHDOG_SECRET_FILE") or DEFAULT_SECRET_FILE).expanduser()
    try:
        secret = read_secret(secret_file)
    except ConfigError as exc:
        print(f"healthz_push: {exc}", file=sys.stderr)
        return 2
    payload = build_payload(fetch_healthz(os.environ.get("BOT_OFFICE_HEALTHZ_URL") or DEFAULT_HEALTHZ))
    try:
        status = post(url, secret, payload)
    except urllib.error.HTTPError as exc:
        print(f"healthz_push: watchdog answered HTTP {exc.code}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"healthz_push: push failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"healthz_push: pushed (controller_reachable={payload['controller_reachable']}, HTTP {status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
