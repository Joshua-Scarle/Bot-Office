"""botoffice-agent pair | run | status | unpair."""
from __future__ import annotations

import argparse
import http.client
import json
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse

from . import __version__, tunnel
from .config import FLAGS, ConfigError, TokenStore, fingerprint, load, read_device, write_device

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def http_url(server: str) -> str:
    """The HTTP base for *server* (ws:// or http://); plain text only to this computer itself."""
    parsed = urllib.parse.urlsplit(server.rstrip("/"))
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(parsed.scheme)
    if scheme is None or not parsed.hostname:
        raise ConfigError(f"bad server URL: {server}")
    if scheme == "http" and parsed.hostname not in LOOPBACK:
        raise ConfigError("plain http/ws is only allowed to 127.0.0.1 (the SSH tunnel); use https/wss otherwise")
    return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def _post_json(base: str, path: str, body: bytes) -> tuple[int, object]:
    """POST without proxies; to the local tunnel only after checking the port is this user's."""
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme == "http":
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
        connection.sock = tunnel.connect(parsed.hostname or "", parsed.port or 80)
    else:
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=30,
                                                 context=ssl.create_default_context())
    try:
        connection.request("POST", parsed.path + path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read(64 * 1024)
    finally:
        connection.close()
    try:
        return response.status, json.loads(payload or b"{}")
    except ValueError:
        return response.status, {}


def cmd_pair(args, cfg) -> int:
    server = args.server or cfg.server
    base = http_url(server)
    if read_device(cfg) and not args.force:
        print("this computer is already paired; run `botoffice-agent unpair` first (or pass --force)", file=sys.stderr)
        return 1
    body = json.dumps({"code": args.code, "hostname": socket.gethostname(),
                       "os": f"{platform.system()} {platform.release()}", "version": __version__}).encode()
    status, reply = _post_json(base, "/v1/machines/pair", body)
    if status != 200:
        detail = reply.get("detail") if isinstance(reply, dict) else None
        print(f"pairing failed: {status} {detail or ''}".rstrip(), file=sys.stderr)
        return 1
    token = reply.get("token")
    if not isinstance(token, str) or fingerprint(token) != reply.get("fingerprint"):
        print("pairing failed: the server's answer does not match its fingerprint", file=sys.stderr)
        return 1
    tokens = TokenStore(cfg, reply["machine_id"])
    where = tokens.save(token)
    write_device(cfg, {"machine_id": reply["machine_id"], "name": reply.get("name"), "server": server,
                       "paired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    print(f"paired as {reply.get('name')!r}; token stored in the {where}")
    print(f"fingerprint: {reply['fingerprint']}")
    print("Next: in Bot Office, Settings > Computers, check this fingerprint matches and press Activate.")
    return 0


def cmd_run(args, cfg) -> int:
    from .client import run
    device = read_device(cfg)
    if device is None:
        print("not paired: run `botoffice-agent pair CODE` first", file=sys.stderr)
        return 3
    return run(cfg, device, TokenStore(cfg, device["machine_id"]))


def cmd_status(args, cfg) -> int:
    device = read_device(cfg)
    print(f"botoffice-agent {__version__}")
    print(f"config: {cfg.config_dir}  audit: {cfg.audit_path}")
    if device is None:
        print("not paired")
        return 1
    print(f"paired: {device.get('name')} ({device['machine_id']}) with {device.get('server')} since {device.get('paired_at')}")
    try:
        token = TokenStore(cfg, device["machine_id"]).load()
        print(f"fingerprint: {fingerprint(token)}")
    except ConfigError as exc:
        print(f"token: {exc}")
    for name in FLAGS:
        value = getattr(cfg, name)
        print(f"{name}: {value}" + (" (follows Bot Office)" if value == "server" else " (this computer decides)"))
    print(f"deny-list: {len(cfg.deny_entries())} entries")
    return 0


def cmd_unpair(args, cfg) -> int:
    device = read_device(cfg)
    if device is None:
        print("not paired")
        return 0
    TokenStore(cfg, device["machine_id"]).clear()
    (cfg.config_dir / "device.json").unlink(missing_ok=True)
    print("unpaired here; also press Revoke for this computer in Bot Office, Settings > Computers")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botoffice-agent", description="Bot Office device companion")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("pair", help="pair with a code from Settings > Computers")
    pair.add_argument("code")
    pair.add_argument("--server", help="default: ws://127.0.0.1:18787 (the SSH tunnel)")
    pair.add_argument("--force", action="store_true")
    sub.add_parser("run", help="connect and serve approved calls (the service)")
    sub.add_parser("status")
    sub.add_parser("unpair")
    args = parser.parse_args(argv)
    try:
        cfg = load()
        return {"pair": cmd_pair, "run": cmd_run, "status": cmd_status, "unpair": cmd_unpair}[args.command](args, cfg)
    except (ConfigError, tunnel.TunnelNotOurs) as exc:
        print(f"botoffice-agent: {exc}", file=sys.stderr)
        return 2
    except (OSError, urllib.error.URLError) as exc:
        print(f"botoffice-agent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
