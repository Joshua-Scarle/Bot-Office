#!/usr/bin/env python3
"""Make, list or revoke a temporary shared key for remote access without Cloudflare Access.

    python3 shared_key.py new --host botoffice.example.com --days 7
    python3 shared_key.py list
    python3 shared_key.py revoke            # removes every shared key

`new` writes the key to a 0600 file next to the config (default ~/bot-office/data/remote_key.txt) and
stores only its SHA-256 and expiry in remote_access.json. The key is never printed. Copy it from the
file into the Claude environment's network secret yourself, then delete the file (`shred -u`).
Bot-Office re-reads remote_access.json within 5 seconds; no restart is needed.
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("BOT_OFFICE_DATA") or os.environ.get("BOT_OFFICE_DATA_DIR")
            or Path.home() / "bot-office" / "data")


def load(path: Path) -> dict:
    if not path.exists():
        return {"enabled": False}
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".remote_access.", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_secret(path: Path, value: str) -> None:
    # a fresh 0600 file in the same directory, then an atomic rename: an old file's permissions or a
    # symlink at the target can never see the new key (0.12.35 review)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".remote_key.", suffix=".txt")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["new", "list", "revoke"])
    ap.add_argument("--config", type=Path, default=DATA / "remote_access.json")
    ap.add_argument("--key-file", type=Path, default=DATA / "remote_key.txt")
    ap.add_argument("--host", help="public hostname, e.g. botoffice.example.com (needed the first time)")
    ap.add_argument("--name", default="claude-cloud")
    ap.add_argument("--days", type=float, default=7.0, help="key lifetime in days (default 7, max 30)")
    args = ap.parse_args()

    cfg = load(args.config)
    keys = cfg.get("shared_keys", {})

    if args.action == "list":
        for digest, entry in keys.items():
            print(f"{entry.get('name')}  sha256:{digest[:12]}...  expires {entry.get('expires_at')}")
        if not keys:
            print("no shared keys")
        return 0

    if args.action == "revoke":
        cfg.pop("shared_keys", None)
        if not cfg.get("team_domain"):
            cfg["enabled"] = False
        save(args.config, cfg)
        if args.key_file.exists():
            args.key_file.unlink()
        print("all shared keys removed; remote access by key is off within 5 seconds")
        return 0

    if not 0 < args.days <= 30:
        print("--days must be between 0 and 30", file=sys.stderr)
        return 2
    hostnames = cfg.get("hostnames") or ([args.host.strip().lower()] if args.host else [])
    if not hostnames:
        print("first run needs --host", file=sys.stderr)
        return 2
    key = secrets.token_urlsafe(32)          # 256 bits, 43 characters
    expires = (datetime.now(timezone.utc) + timedelta(days=args.days)).replace(microsecond=0)
    keys = {d: e for d, e in keys.items() if e.get("name") != args.name}   # one key per name
    keys[hashlib.sha256(key.encode("ascii")).hexdigest()] = {
        "name": args.name, "expires_at": expires.isoformat().replace("+00:00", "Z")}
    cfg.update(enabled=True, hostnames=hostnames, shared_keys=keys)
    write_secret(args.key_file, key)
    save(args.config, cfg)
    print(f"new key for {args.name}, expires {keys[next(reversed(keys))]['expires_at']}")
    print(f"the key is in {args.key_file} (0600); it was not printed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
