from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings, first_env
from .desk import DeskError
from .providers.antigravity import AntigravityAdapter
from .providers.claude import ClaudeAdapter
from .providers.codex import CodexAdapter
from .providers.cursor import CursorAdapter
from .providers.opencode import OpenCodeAdapter
from .store import BridgeHopConflict, JobStore
from .turns import TurnError, Turns

KINDS = ("question", "result", "blocker", "fyi")
DROPPED_KINDS = {"ack"}
MAX_HOPS = 6
PER_BOT_SECONDS = 60
DAILY_LIMIT = 30
# no single bot may use up the whole daily limit (BOT_OFFICE_GROK_PER_BOT_DAILY)
PER_BOT_DAILY = 10
CONFIG_NAME = "grok_webhook.json"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
DEFAULT_HEADER = "Authorization"
DEFAULT_SCHEME = "Bearer"

USAGE_GROK = (
    "usage: desk grok send --to NAME --kind question|result|blocker|fyi --text TEXT"
    " [--thread ID] [--from NAME]\n"
    "       desk grok install-config   # reads url and key from stdin"
)
USAGE_INBOX = "usage: desk inbox post --to BOT --from NAME --text TEXT [--thread ID] [--hop N]"


def _data_dir() -> Path:
    return Settings.from_env().data_dir


def _private_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.chmod(path, mode)


def config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / CONFIG_NAME


def load_config(data_dir: Path | None = None) -> dict[str, str]:
    path = config_path(data_dir)
    if not path.is_file():
        raise DeskError(f"webhook config not found: {path} (run desk grok install-config)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeskError("webhook config is unreadable") from exc
    url, key = (data.get("url") or "").strip(), (data.get("key") or "").strip()
    if not url or not key:
        raise DeskError("webhook config must include url and key")
    header = (data.get("header") or DEFAULT_HEADER).strip() or DEFAULT_HEADER
    scheme = data.get("scheme")
    if scheme is None:
        scheme = DEFAULT_SCHEME if header.lower() == "authorization" else ""
    return {"url": url, "key": key, "header": header, "scheme": str(scheme).strip()}


def auth_header(config: dict[str, str]) -> tuple[str, str]:
    key = config["key"]
    scheme = config.get("scheme") or ""
    value = f"{scheme} {key}".strip() if scheme else key
    return config["header"], value


def parse_install_config(raw: str) -> dict[str, str]:
    text = raw.strip()
    if not text:
        raise DeskError("expected url and key on stdin")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DeskError("stdin JSON must include url and key") from exc
        url, key = str(data.get("url") or "").strip(), str(data.get("key") or "").strip()
        header = str(data.get("header") or "").strip()
        scheme = data.get("scheme")
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            raise DeskError("stdin must be JSON, or two lines: url then key")
        url, key, header, scheme = lines[0], lines[1], (lines[2] if len(lines) > 2 else ""), None
    if not url or not key:
        raise DeskError("url and key must be non-empty")
    check_url(url)
    out = {"url": url, "key": key, "header": header or DEFAULT_HEADER}
    if scheme is not None and str(scheme).strip() != "":
        out["scheme"] = str(scheme).strip()
    return out


class WebhookNotSent(DeskError):
    """The webhook certainly did not accept the message (refused before or by the server)."""


class WebhookUnsure(DeskError):
    """The message may have been delivered (e.g. a timeout after sending): it stays counted."""


def allowed_hosts() -> set[str]:
    """BOT_OFFICE_GROK_HOSTS: optional comma-separated webhook host allowlist. A listed host
    may also resolve to a private or loopback address."""
    raw = first_env("BOT_OFFICE_GROK_HOSTS") or ""
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def per_bot_daily() -> int:
    raw = first_env("BOT_OFFICE_GROK_PER_BOT_DAILY") or ""
    return int(raw) if raw.strip().isdigit() and int(raw) > 0 else PER_BOT_DAILY


def check_url(url: str) -> None:
    """Webhook URLs must be https; plain http only to this machine. With an allowlist set the
    host must be on it."""
    parsed = urlparse(url)
    if not parsed.netloc or not (
        parsed.scheme == "https" or parsed.scheme == "http" and (parsed.hostname or "") in LOCAL_HOSTS
    ):
        raise WebhookNotSent("url must be https (http only for 127.0.0.1 or localhost)")
    allowed = allowed_hosts()
    if allowed and (parsed.hostname or "").lower() not in allowed:
        raise WebhookNotSent("webhook host is not in BOT_OFFICE_GROK_HOSTS")


def vetted_address(host: str, port: int) -> str:
    """The address to connect to: every address *host* resolves to must be public (not
    private, loopback, link-local or reserved) unless the host is allowlisted."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebhookNotSent("webhook host could not be resolved") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise WebhookNotSent("webhook host could not be resolved")
    if host.lower() not in allowed_hosts():
        for address in addresses:
            if not ipaddress.ip_address(address.split("%")[0]).is_global:
                raise WebhookNotSent("webhook host resolves to a private or local address "
                                     "(add it to BOT_OFFICE_GROK_HOSTS to allow)")
    return addresses[0]


class _VettedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        # connect to the address that was checked, so a second DNS answer cannot differ
        self.sock = socket.create_connection((vetted_address(self.host, self.port), self.port),
                                             self.timeout, self.source_address)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _VettedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        _VettedHTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _VettedHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def http_open(self, req):  # noqa: ANN001
        return self.do_open(_VettedHTTPConnection, req)

    def https_open(self, req):  # noqa: ANN001
        return self.do_open(_VettedHTTPSConnection, req, context=self._context)


def install_config(raw: str, data_dir: Path | None = None) -> Path:
    path = config_path(data_dir)
    _private_write(path, json.dumps(parse_install_config(raw), indent=2) + "\n")
    return path


def build_payload(
    *,
    from_bot: str,
    to: str,
    kind: str,
    text: str,
    thread_id: str | None,
    hop: int,
    ts: datetime | None = None,
) -> dict[str, Any]:
    return {
        "from_bot": from_bot,
        "to": to,
        "kind": kind,
        "text": text,
        "thread_id": thread_id,
        "ts": (ts or datetime.now(UTC)).isoformat(),
        "hop": hop,
    }


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx from the webhook is an error: the key is never re-sent to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECTS = urllib.request.build_opener(_RefuseRedirects())
# the webhook: no redirects, no proxy, and only to vetted addresses
_WEBHOOK = urllib.request.OpenerDirector()
for _handler in (_VettedHandler(), _RefuseRedirects(), urllib.request.HTTPErrorProcessor(),
                 urllib.request.HTTPDefaultErrorHandler()):
    _WEBHOOK.add_handler(_handler)


def post_webhook(
    config: dict[str, str],
    payload: dict[str, Any],
    *,
    opener: Any = None,
    timeout: int = 15,
) -> int:
    body = json.dumps(payload).encode()
    header, value = auth_header(config)
    request = urllib.request.Request(
        config["url"],
        data=body,
        method="POST",
        headers={header: value, "Content-Type": "application/json"},
    )
    check_url(config["url"])
    try:
        if opener is not None:
            response = opener(request, timeout=timeout)
            status = getattr(response, "status", None) or response.getcode()
        else:
            with _WEBHOOK.open(request, timeout=timeout) as response:
                status = response.getcode()
    except DeskError:
        raise
    except urllib.error.HTTPError as exc:
        raise WebhookNotSent(f"webhook HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, WebhookNotSent):
            raise reason from exc
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            raise WebhookNotSent("webhook request failed (not sent)") from exc
        raise WebhookUnsure("webhook did not answer; the message may have been delivered") from exc
    except (ConnectionRefusedError, socket.gaierror) as exc:
        raise WebhookNotSent("webhook request failed (not sent)") from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise WebhookUnsure("webhook did not answer; the message may have been delivered") from exc
    if status != 200:
        raise WebhookNotSent(f"webhook HTTP {status}")
    return int(status)


def resolve_from_bot(explicit: str | None, data_dir: Path, environ: dict[str, str] | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = environ if environ is not None else os.environ
    bot_id = (env.get("BOT_OFFICE_BOT_ID") or env.get("AGENTDESK_BOT_ID") or "").strip()
    if not bot_id:
        return "agentdesk"
    store = JobStore(data_dir / "agentdesk.sqlite3")
    bot = store.find_bot(bot_id)
    return bot.name if bot else bot_id


def send_to_grok(
    *,
    to: str,
    kind: str,
    text: str,
    thread_id: str | None = None,
    from_bot: str | None = None,
    data_dir: Path | None = None,
    environ: dict[str, str] | None = None,
    opener: Any = None,
    now: datetime | None = None,
) -> str:
    kind = kind.strip().lower()
    if kind in DROPPED_KINDS:
        return "dropped: kind=ack is not sent"
    if kind not in KINDS:
        raise DeskError(f"kind must be one of {', '.join(KINDS)}")
    to = to.strip()
    text = text.strip()
    if not to or not text:
        raise DeskError("to and text must be non-empty")
    directory = data_dir or _data_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sender = resolve_from_bot(from_bot, directory, environ)
    config = load_config(directory)
    check_url(config["url"])
    store = JobStore(directory / "agentdesk.sqlite3")
    try:
        reservation, hop, dropped = store.bridge_reserve(
            sender, thread_id, now or datetime.now(UTC),
            per_bot_seconds=PER_BOT_SECONDS, daily_limit=DAILY_LIMIT, max_hops=MAX_HOPS,
            per_bot_daily=per_bot_daily(),
        )
    except sqlite3.OperationalError:
        return "dropped: bridge state is busy; not sent"
    if dropped or reservation is None:
        return f"dropped: {dropped}"
    payload = build_payload(
        from_bot=sender, to=to, kind=kind, text=text, thread_id=thread_id, hop=hop, ts=now
    )
    try:
        post_webhook(config, payload, opener=opener)
    except WebhookNotSent:
        # certainly not delivered: release the quota and undo the hop step
        store.bridge_settle(reservation, False, thread_id, hop)
        raise
    except BaseException:
        # a timeout or anything else may have come after delivery: keep it counted
        store.bridge_settle(reservation, True)
        raise
    store.bridge_settle(reservation, True)
    return "sent"


def _providers(settings: Settings) -> dict:
    return {
        "opencode": OpenCodeAdapter(enabled="opencode" in settings.enabled_providers),
        "codex": CodexAdapter(enabled="codex" in settings.enabled_providers),
        "claude": ClaudeAdapter(enabled="claude" in settings.enabled_providers),
        "cursor": CursorAdapter(enabled="cursor" in settings.enabled_providers),
        "antigravity": AntigravityAdapter(enabled="antigravity" in settings.enabled_providers),
    }


def post_inbox(
    *,
    to: str,
    text: str,
    sender: str,
    thread_id: str | None = None,
    hop: int | None = None,
    data_dir: Path | None = None,
    providers: dict | None = None,
    turns: Turns | None = None,
) -> tuple[str, str]:
    """Queue an inbound bridge message as a turn (the controller passes its own *turns*)."""
    text = text.strip()
    sender = sender.strip()
    to = to.strip()
    if not to or not text or not sender:
        raise DeskError("to, from, and text must be non-empty")
    supplied = 1 if hop is None else hop
    if supplied < 1:
        raise DeskError("hop must be a positive integer")
    if supplied > MAX_HOPS:
        return "dropped", f"dropped: hop limit of {MAX_HOPS} reached"
    if turns is None:
        settings = Settings(data_dir=data_dir or _data_dir(), token="desk-local")
        settings.ensure_dirs()
        turns = Turns(JobStore(settings.data_dir / "agentdesk.sqlite3"), settings, providers or _providers(settings))
    store = turns.store
    bot = store.find_bot(to)
    if not bot:
        raise DeskError(f"bot not found: {to}")
    for _ in range(5):
        incoming_hop, accepted = store.bridge_inbox_hop(thread_id, supplied, MAX_HOPS)
        if not accepted:
            return "dropped", f"dropped: hop limit of {MAX_HOPS} reached"
        prompt = text
        if thread_id:
            prompt = f"[bridge thread={thread_id} hop={incoming_hop}]\n{text}"
        try:
            # the hop is stored in the same transaction as the turn: no hop without delivery
            _message, job = turns.post_message(bot, prompt, source="inbox", sender=sender,
                                               bridge_hop=(thread_id, incoming_hop) if thread_id else None)
        except BridgeHopConflict:
            continue  # another message of this thread moved its hop meanwhile: recompute
        except TurnError as exc:
            raise DeskError(str(exc)) from exc
        return "queued", str(job.id)
    return "dropped", "dropped: bridge thread is busy; try again"


# --- desk-side HTTP clients: the controller does the sending (it alone reads the webhook key) ---


def _api_token(environ: dict[str, str], data_dir: Path | None) -> str:
    """The job's agent token, else (operator shell) the master token file."""
    token = environ.get("BOT_OFFICE_AGENT_TOKEN", "").strip()
    if token:
        return token
    configured = environ.get("BOT_OFFICE_TOKEN_FILE") or environ.get("AGENTDESK_TOKEN_FILE")
    path = Path(configured).expanduser() if configured else (data_dir or _data_dir()) / "token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DeskError(f"no API token: {path} is not readable (set BOT_OFFICE_TOKEN_FILE)") from exc
    if not token:
        raise DeskError(f"no API token: {path} is empty")
    return token


def _api_url(environ: dict[str, str]) -> str:
    url = environ.get("BOT_OFFICE_API_URL", "").strip()
    if url:
        return url.rstrip("/")
    port = first_env("BOT_OFFICE_PORT", "AGENTDESK_PORT", default="8787") or "8787"
    return f"http://127.0.0.1:{int(port)}"


def _transport(url: str, body: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
    """POST JSON to the local controller; returns (HTTP status, JSON body)."""
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with _NO_REDIRECTS.open(request, timeout=30) as response:
            return response.getcode(), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
        return exc.code, data if isinstance(data, dict) else {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeskError(f"Bot-Office API unreachable at {url.split('/v1/')[0]}") from exc


def call_api(path: str, body: dict[str, Any], *, environ: dict[str, str] | None = None,
             data_dir: Path | None = None) -> dict[str, Any]:
    env = dict(os.environ) if environ is None else environ
    status, data = _transport(_api_url(env) + path, body, _api_token(env, data_dir))
    if status != 200:
        detail = data.get("detail")
        raise DeskError(detail if isinstance(detail, str) else f"Bot-Office API HTTP {status}")
    return data


def _flag_parser(prog: str, usage: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, usage=usage, add_help=True)
    return parser


def desk_grok(argv: list[str], *, stdin=None, data_dir: Path | None = None,
              environ: dict[str, str] | None = None) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE_GROK)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "install-config":
        env = environ if environ is not None else os.environ
        if env.get("BOT_OFFICE_AGENT_TOKEN") or env.get("BOT_OFFICE_JOB_ID"):
            # a bot turn must not repoint the webhook (operator only; full isolation is Stage C)
            raise DeskError("install-config is for the operator; it cannot run inside a bot turn")
        raw = (stdin or sys.stdin).read()
        path = install_config(raw, data_dir)
        print(f"wrote {path} (mode 0600)")
        return 0
    if command != "send":
        raise DeskError(USAGE_GROK)
    parser = _flag_parser("desk grok send", USAGE_GROK)
    parser.add_argument("--to", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--thread")
    parser.add_argument("--from", dest="from_bot")
    args = parser.parse_args(rest)
    body = {"to": args.to, "kind": args.kind, "text": args.text, "thread_id": args.thread}
    if args.from_bot:
        body["from"] = args.from_bot
    result = str(call_api("/v1/bridge/grok/send", body, environ=environ, data_dir=data_dir).get("result"))
    print(result)
    return 0 if result == "sent" else 2


def desk_inbox(argv: list[str], *, data_dir: Path | None = None, environ: dict[str, str] | None = None) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE_INBOX)
        return 0
    if argv[0] != "post":
        raise DeskError(USAGE_INBOX)
    parser = _flag_parser("desk inbox post", USAGE_INBOX)
    parser.add_argument("--to", required=True)
    parser.add_argument("--from", dest="sender", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--thread")
    parser.add_argument("--hop", type=int)
    args = parser.parse_args(argv[1:])
    body = {"to": args.to, "from": args.sender, "text": args.text, "thread_id": args.thread, "hop": args.hop}
    data = call_api("/v1/bridge/inbox", body, environ=environ, data_dir=data_dir)
    status, detail = str(data.get("status")), str(data.get("detail"))
    print(detail if status == "dropped" else f"{status} {detail}")
    return 0 if status == "queued" else 2
