"""Slice 1 connectors (Engineer 8): official hosted MCP servers (Railway, Cloudflare) for Claude jobs.

Bot-Office is the OAuth client (dynamic client registration + PKCE S256, MCP authorization spec). The
provider redirects Joshua's *browser* to http://127.0.0.1:<port>/v1/connectors/oauth/callback; the SSH
tunnel to the loopback UI (README, "SSH tunnel") carries it to the controller. Tokens live in a 0700
directory outside the repo (0600 files), are refreshed here, and reach a job only as an environment
variable that the --mcp-config header names as ${VAR}: never in argv, the database, events or logs.

The database holds ids and codes only (connector_state, connector_bots). Writes are off by default;
a tool that is not on a connector's read list counts as a write (unknown = write). Write tools are
only offered on engines that can raise an approval card (approval_classes.can_pause), and every call
of one still goes through the "connector_writes" approval class (approval_classes.py).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

KILL_ENV = "BOT_OFFICE_CONNECTORS"  # "0" switches injection and tool allowance off
DIR_ENV = "BOT_OFFICE_CONNECTORS_DIR"
CALLBACK_PATH = "/v1/connectors/oauth/callback"
CLIENT_NAME = "Bot Office"
PENDING_TTL = 600
PENDING_MAX = 20
REFRESH_SKEW = 90
HTTP_TIMEOUT = 15.0
MCP_VERSION = "2025-06-18"
STATUSES = ("not_connected", "unchecked", "ok", "needs_login", "error")
BAD = ("needs_login", "error")
ERROR_CLASSES = ("http_401", "http_403", "http_4xx", "http_5xx", "network", "timeout", "bad_response",
                 "refresh_failed", "no_refresh_token", "tools_list")

Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class ConnectorError(ValueError):
    pass


@dataclass(frozen=True)
class Connector:
    id: str
    label: str
    endpoint: str
    server: str  # key in --mcp-config; tools are mcp__<server>__<tool>
    env_var: str
    auth_kinds: tuple[str, ...]
    scopes: tuple[str, ...]  # asked for on Connect: read-level only
    prm_url: str  # RFC 9728 protected-resource metadata
    read_tools: frozenset[str]
    write_tools: frozenset[str]  # official names known to change things; anything unlisted is a write too
    docs: str

    def tool(self, name: str) -> str:
        return f"mcp__{self.server}__{name}"

    def is_read(self, name: str) -> bool:
        return name in self.read_tools


REGISTRY: dict[str, Connector] = {c.id: c for c in (
    Connector(
        "railway", "Railway", "https://mcp.railway.com", "bo_railway", "BOT_OFFICE_CONNECTOR_RAILWAY_TOKEN",
        ("oauth", "token"), ("workspace:member",), "https://mcp.railway.com/.well-known/oauth-protected-resource",
        frozenset({"whoami", "list-projects", "list-services", "list-feature-flags", "get-feature-flag",
                   "check-cloud-agent-task", "list-cloud-agent-tasks", "get-cloud-agent-transcript",
                   "list-cloud-agent-models", "list-automations", "get-automation", "list-automation-triggers",
                   "list-monitors", "list-automation-runs", "get-automation-run"}),
        frozenset({"create-project", "set-feature-flag", "delete-feature-flag", "redeploy", "accept-deploy",
                   "railway-agent", "run-cloud-agent-task", "respond-cloud-agent-task", "cancel-cloud-agent-task",
                   "create-automation", "update-automation", "delete-automation", "create-automation-trigger",
                   "update-automation-trigger", "create-automation-event-trigger",
                   "update-automation-event-trigger", "delete-automation-trigger", "run-automation"}),
        "https://docs.railway.com/ai/mcp-server"),
    Connector(
        "cloudflare", "Cloudflare", "https://mcp.cloudflare.com/mcp", "bo_cloudflare",
        "BOT_OFFICE_CONNECTOR_CLOUDFLARE_TOKEN", ("oauth", "token"), ("user:read", "account:read"),
        "https://mcp.cloudflare.com/.well-known/oauth-protected-resource/mcp",
        # Code Mode: search() reads the API spec, docs() the docs; execute() runs API calls: a write
        frozenset({"search", "docs"}), frozenset({"execute"}),
        "https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/"),
)}
SERVERS = {c.server: c for c in REGISTRY.values()}


def enabled() -> bool:
    return os.environ.get(KILL_ENV, "1").strip() != "0"


def write_tool_regex() -> str:
    """Tool names of our servers that are not known reads (approval class connector_writes)."""
    parts = []
    for c in REGISTRY.values():
        reads = "|".join(re.escape(name) for name in sorted(c.read_tools))
        parts.append(f"{re.escape(f'mcp__{c.server}__')}(?!(?:{reads})$)")
    return "^(?:" + "|".join(parts) + ")"


def get(connector_id: str) -> Connector:
    try:
        return REGISTRY[connector_id]
    except KeyError:
        raise ConnectorError(f"no connector {connector_id!r}") from None


# ---- secret store -------------------------------------------------------------------------------

def store_dir() -> Path:
    override = os.environ.get(DIR_ENV)
    return Path(override).expanduser() if override else Path.home() / ".bot-office" / "connectors"


def _forbidden_roots() -> list[Path]:
    roots = [Path(__file__).resolve().parents[2], Path.home() / "printpilot-agentdesk"]
    return [root.resolve() for root in roots]


def _secure_dir() -> Path:
    path = store_dir().resolve()
    for root in _forbidden_roots():
        if path == root or root in path.parents:
            raise ConnectorError("the connector secret directory must be outside the repository and the "
                                 "Bot-Office install tree")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (path.stat().st_mode & 0o777) != 0o700:
        path.chmod(0o700)
    return path


_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


def _write_secret(name: str, data: dict) -> None:
    if not _NAME.match(name):
        raise ConnectorError("bad secret name")
    directory = _secure_dir()
    tmp = directory / f".{name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, directory / name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_secret(name: str) -> dict | None:
    try:
        data = json.loads((_secure_dir() / name).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _delete_secret(name: str) -> None:
    (_secure_dir() / name).unlink(missing_ok=True)


def credential(connector_id: str) -> dict | None:
    get(connector_id)
    return _read_secret(f"{connector_id}.json")


# ---- database (ids and codes only) --------------------------------------------------------------

def init_tables(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS connector_state (
        connector_id TEXT PRIMARY KEY, status TEXT NOT NULL, last_health_at TEXT, last_error_class TEXT,
        since TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS connector_bots (
        bot_id TEXT NOT NULL, connector_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        write_enabled INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bot_id, connector_id))""")


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def state(store, connector_id: str) -> dict:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM connector_state WHERE connector_id=?", (connector_id,)).fetchone()
    if row is None:
        return {"status": "unchecked" if credential(connector_id) else "not_connected",
                "last_health_at": None, "last_error_class": None, "since": None}
    return {key: row[key] for key in ("status", "last_health_at", "last_error_class", "since")}


def _set_state(store, connector_id: str, status: str, error_class: str | None = None, probed: bool = False) -> None:
    assert status in STATUSES and (error_class is None or error_class in ERROR_CLASSES)
    now = _stamp()
    with closing(store._connect()) as db:
        row = db.execute("SELECT status, since, last_health_at FROM connector_state WHERE connector_id=?",
                         (connector_id,)).fetchone()
        since = row["since"] if row and row["status"] == status else now
        health = now if probed else (row["last_health_at"] if row else None)
        db.execute("INSERT INTO connector_state (connector_id,status,last_health_at,last_error_class,since) "
                   "VALUES (?,?,?,?,?) ON CONFLICT(connector_id) DO UPDATE SET status=excluded.status, "
                   "last_health_at=excluded.last_health_at, last_error_class=excluded.last_error_class, "
                   "since=excluded.since", (connector_id, status, health, error_class, since))
        if status in BAD:
            # the existing notify path (notify.py): codes and ids only; one event per episode
            db.execute("INSERT OR IGNORE INTO notify_events (key, cls, bot_id, ref, kind, created_at) "
                       "VALUES (?,?,?,?,?,?)", (f"conn:{connector_id}:{status}:{since}", "needs_you", None, None,
                                                "connector_login" if status == "needs_login" else "connector_error",
                                                now))


def needs_you_items(store) -> list[dict]:
    items = []
    with closing(store._connect()) as db:
        rows = db.execute("SELECT * FROM connector_state WHERE status IN ('needs_login','error')").fetchall()
    for row in rows:
        c = REGISTRY.get(row["connector_id"])
        if c is None:
            continue
        login = row["status"] == "needs_login"
        ask = (f"{c.label} needs you to sign in again (Settings, Connectors, Connect)." if login else
               f"{c.label} did not answer the health check ({row['last_error_class'] or 'error'}); see Settings, Connectors.")
        items.append({"id": f"c:{c.id}:{row['status']}:{row['since']}", "kind": "connector", "state": row["status"],
                      "bot_id": None, "bot_name": "Bot Office", "notify": True, "title": f"{c.label} connector",
                      "ask": ask, "at": row["since"], "actions": ["settings"]})
    return items


def set_bot(store, connector_id: str, bot_id: str, enabled: bool | None = None, write_enabled: bool | None = None) -> dict:
    get(connector_id)
    with closing(store._connect()) as db:
        db.execute("INSERT OR IGNORE INTO connector_bots (bot_id, connector_id) VALUES (?,?)", (bot_id, connector_id))
        if enabled is not None:
            db.execute("UPDATE connector_bots SET enabled=? WHERE bot_id=? AND connector_id=?",
                       (int(bool(enabled)), bot_id, connector_id))
        if write_enabled is not None:
            db.execute("UPDATE connector_bots SET write_enabled=? WHERE bot_id=? AND connector_id=?",
                       (int(bool(write_enabled)), bot_id, connector_id))
        row = db.execute("SELECT enabled, write_enabled FROM connector_bots WHERE bot_id=? AND connector_id=?",
                         (bot_id, connector_id)).fetchone()
    return {"enabled": bool(row["enabled"]), "write_enabled": bool(row["write_enabled"])}


def bot_flags(store, connector_id: str) -> dict[str, dict]:
    with closing(store._connect()) as db:
        rows = db.execute("SELECT bot_id, enabled, write_enabled FROM connector_bots WHERE connector_id=?",
                          (connector_id,)).fetchall()
    return {row["bot_id"]: {"enabled": bool(row["enabled"]), "write_enabled": bool(row["write_enabled"])} for row in rows}


# ---- HTTP ---------------------------------------------------------------------------------------

def _http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    response = httpx.request(method, url, headers=headers, content=body, timeout=HTTP_TIMEOUT,
                             follow_redirects=False)
    return response.status_code, {k.lower(): v for k, v in response.headers.items()}, response.content[:1_000_000]


def _json(status_body: tuple[int, dict, bytes]) -> dict:
    status, _headers, body = status_body
    if status != 200:
        raise ConnectorError(f"unexpected HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ConnectorError("bad JSON from the server") from exc
    if not isinstance(data, dict):
        raise ConnectorError("bad JSON from the server")
    return data


def _https(url: object) -> str:
    if not isinstance(url, str) or urlparse(url).scheme != "https":
        raise ConnectorError("the server metadata names a non-https endpoint")
    return url


# ---- OAuth (client side) ------------------------------------------------------------------------

def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def discover(c: Connector, transport: Transport | None = None) -> dict:
    """Protected-resource metadata, then the authorization server's RFC 8414 document. Requires PKCE S256."""
    transport = transport or _http
    try:
        prm = _json(transport("GET", c.prm_url, {"accept": "application/json"}, None))
        issuer = _https((prm.get("authorization_servers") or [None])[0]).rstrip("/")
        meta = _json(transport("GET", issuer + "/.well-known/oauth-authorization-server",
                               {"accept": "application/json"}, None))
    except (httpx.HTTPError, ConnectorError) as exc:
        raise ConnectorError(f"{c.label} OAuth discovery failed") from exc
    if "S256" not in (meta.get("code_challenge_methods_supported") or []):
        raise ConnectorError(f"{c.label} does not offer PKCE S256")
    return {"issuer": str(meta.get("issuer") or issuer), "authorization_endpoint": _https(meta.get("authorization_endpoint")),
            "token_endpoint": _https(meta.get("token_endpoint")),
            "registration_endpoint": meta.get("registration_endpoint"),
            "resource": prm.get("resource") or c.endpoint,
            "scopes_supported": meta.get("scopes_supported") or []}


def _client(c: Connector, meta: dict, redirect: str, transport: Transport) -> dict:
    cached = _read_secret(f"{c.id}.client.json")
    if cached and cached.get("redirect_uri") == redirect and cached.get("issuer") == meta["issuer"]:
        return cached
    if not meta.get("registration_endpoint"):
        raise ConnectorError(f"{c.label} does not allow dynamic client registration")
    body = json.dumps({"client_name": CLIENT_NAME, "redirect_uris": [redirect], "response_types": ["code"],
                       "grant_types": ["authorization_code", "refresh_token"],
                       "token_endpoint_auth_method": "none"}).encode()
    try:
        data = _json_created(transport("POST", _https(meta["registration_endpoint"]),
                                       {"content-type": "application/json", "accept": "application/json"}, body))
    except (httpx.HTTPError, ConnectorError) as exc:
        raise ConnectorError(f"{c.label} refused dynamic client registration") from exc
    if not data.get("client_id"):
        raise ConnectorError(f"{c.label} registration returned no client id")
    client = {"client_id": data["client_id"], "redirect_uri": redirect, "issuer": meta["issuer"]}
    if data.get("client_secret"):
        client["client_secret"] = data["client_secret"]
    _write_secret(f"{c.id}.client.json", client)
    return client


def _json_created(result: tuple[int, dict, bytes]) -> dict:
    status, headers, body = result
    return _json((200, headers, body) if status in (200, 201) else (status, headers, body))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pending_name(state_value: str) -> str:
    return "pending-" + hashlib.sha256(state_value.encode()).hexdigest()[:40]


def _purge_pending() -> None:
    directory = _secure_dir()
    files = sorted(directory.glob("pending-*"), key=lambda p: p.stat().st_mtime)
    for path in files:
        if time.time() - path.stat().st_mtime > PENDING_TTL:
            path.unlink(missing_ok=True)
    for path in sorted(directory.glob("pending-*"), key=lambda p: p.stat().st_mtime)[:-PENDING_MAX]:
        path.unlink(missing_ok=True)


def start_oauth(connector_id: str, port: int, transport: Transport | None = None) -> str:
    """The authorize URL for Joshua's browser (state, PKCE S256, resource indicator)."""
    transport = transport or _http
    c = get(connector_id)
    meta = discover(c, transport)
    redirect = redirect_uri(port)
    client = _client(c, meta, redirect, transport)
    state_value, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    scopes = list(c.scopes) + (["offline_access"] if "offline_access" in meta["scopes_supported"] else [])
    _purge_pending()
    _write_secret(_pending_name(state_value), {
        "connector": c.id, "verifier": verifier, "redirect_uri": redirect, "client": client, "meta": {
            "issuer": meta["issuer"], "token_endpoint": meta["token_endpoint"], "resource": meta["resource"]},
        "created": time.time()})
    query = urlencode({"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                       "state": state_value, "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
                       "code_challenge_method": "S256", "scope": " ".join(scopes), "resource": meta["resource"]})
    return f"{meta['authorization_endpoint']}?{query}"


def _token_request(token_endpoint: str, form: dict, client: dict, transport: Transport) -> dict:
    form = {**form, "client_id": client["client_id"]}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    return _json(transport("POST", token_endpoint, {"content-type": "application/x-www-form-urlencoded",
                                                   "accept": "application/json"}, urlencode(form).encode()))


def _credential_from(data: dict, previous: dict | None, meta: dict, client: dict) -> dict:
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise ConnectorError("the token response had no access token")
    lifetime = data.get("expires_in")
    return {"kind": "oauth", "access_token": token,
            "refresh_token": data.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": time.time() + (lifetime if isinstance(lifetime, (int, float)) and lifetime > 0 else 3600),
            "token_endpoint": meta["token_endpoint"], "resource": meta["resource"], "client": client}


def complete_oauth(store, state_value: str, code: str, iss: str | None = None, transport: Transport | None = None) -> str:
    """Callback: validate the one-time state, exchange the code, store the tokens. Returns the connector id."""
    transport = transport or _http
    name = _pending_name(state_value or "")
    pending = _read_secret(name)
    if pending is None:
        raise ConnectorError("unknown or expired sign-in (start Connect again)")
    _delete_secret(name)  # single use
    if time.time() - float(pending.get("created", 0)) > PENDING_TTL:
        raise ConnectorError("the sign-in expired (start Connect again)")
    if iss and iss.rstrip("/") != pending["meta"]["issuer"].rstrip("/"):
        raise ConnectorError("the sign-in came from an unexpected issuer")
    if not code:
        raise ConnectorError("no authorization code")
    c = get(pending["connector"])
    try:
        data = _token_request(pending["meta"]["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"], "resource": pending["meta"]["resource"]},
            pending["client"], transport)
        _write_secret(f"{c.id}.json", _credential_from(data, None, pending["meta"], pending["client"]))
    except (httpx.HTTPError, ConnectorError) as exc:
        raise ConnectorError(f"{c.label} did not accept the sign-in") from exc
    _set_state(store, c.id, "unchecked")
    return c.id


def save_token(store, connector_id: str, token: str) -> None:
    c = get(connector_id)
    token = (token or "").strip()
    if not 8 <= len(token) <= 4096 or re.search(r"\s", token):
        raise ConnectorError("that does not look like a token")
    _write_secret(f"{c.id}.json", {"kind": "token", "access_token": token})
    _set_state(store, c.id, "unchecked")


def revoke(store, connector_id: str) -> None:
    c = get(connector_id)
    _delete_secret(f"{c.id}.json")
    with closing(store._connect()) as db:
        db.execute("DELETE FROM connector_state WHERE connector_id=?", (c.id,))


def refresh(store, connector_id: str, transport: Transport | None = None) -> bool:
    """Refresh an OAuth credential controller-side. False (and needs_login or error, with a Needs-you
    item and a notification) when it cannot be."""
    transport = transport or _http
    cred = credential(connector_id)
    if not cred or cred.get("kind") != "oauth":
        return False
    if not cred.get("refresh_token"):
        _set_state(store, connector_id, "needs_login", "no_refresh_token")
        return False
    try:
        data = _token_request(cred["token_endpoint"], {"grant_type": "refresh_token",
                                                       "refresh_token": cred["refresh_token"],
                                                       "resource": cred.get("resource", get(connector_id).endpoint)},
                              cred["client"], transport)
        new = _credential_from(data, cred, {"token_endpoint": cred["token_endpoint"],
                                            "resource": cred.get("resource", "")}, cred["client"])
    except (httpx.TimeoutException, httpx.TransportError):
        _set_state(store, connector_id, "error", "network")  # may pass: the credential is kept
        return False
    except (httpx.HTTPError, ConnectorError):
        _set_state(store, connector_id, "needs_login", "refresh_failed")
        return False
    _write_secret(f"{connector_id}.json", new)
    return True


def access_token(store, connector_id: str, transport: Transport | None = None) -> str | None:
    """A usable token, refreshed when near expiry; None when there is none (state already recorded)."""
    transport = transport or _http
    cred = credential(connector_id)
    if not cred:
        return None
    if cred.get("kind") == "oauth" and float(cred.get("expires_at", 0)) - REFRESH_SKEW <= time.time():
        if not refresh(store, connector_id, transport):
            return None
        cred = credential(connector_id) or {}
    token = cred.get("access_token")
    return token if isinstance(token, str) and token else None


# ---- health probe -------------------------------------------------------------------------------

def _mcp_post(c: Connector, token: str, payload: dict, transport: Transport, session: str | None = None):
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json",
               "accept": "application/json, text/event-stream", "mcp-protocol-version": MCP_VERSION}
    if session:
        headers["mcp-session-id"] = session
    return transport("POST", c.endpoint, headers, json.dumps(payload).encode())


def _rpc_result(body: bytes) -> dict | None:
    text = body.decode("utf-8", "replace")
    candidates = [text] + [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    for item in candidates:
        try:
            data = json.loads(item)
        except ValueError:
            continue
        if isinstance(data, dict) and ("result" in data or "error" in data):
            return data
    return None


def _classify(status: int) -> str:
    return {401: "http_401", 403: "http_403"}.get(status) or ("http_5xx" if status >= 500 else "http_4xx")


def _probe_once(c: Connector, token: str, transport: Transport) -> tuple[str, str | None]:
    try:
        status, headers, body = _mcp_post(c, token, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_VERSION, "capabilities": {},
                       "clientInfo": {"name": "bot-office-health", "version": "1"}}}, transport)
        if status in (401, 403):
            return "needs_login", _classify(status)
        if status != 200:
            return "error", _classify(status)
        reply = _rpc_result(body)
        if not reply or "result" not in reply:
            return "error", "bad_response"
        session = headers.get("mcp-session-id")
        _mcp_post(c, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, transport, session)
        status, _h, body = _mcp_post(c, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, transport, session)
        if status in (401, 403):
            return "needs_login", _classify(status)
        listing = _rpc_result(body) if status == 200 else None
        if not listing or "result" not in listing:
            return "error", "tools_list"
        return "ok", None
    except httpx.TimeoutException:
        return "error", "timeout"
    except (httpx.HTTPError, OSError):
        return "error", "network"


def probe(store, connector_id: str, transport: Transport | None = None) -> dict:
    """Read-only initialize + tools/list with the stored credential. 401/403 means needs_login (after one
    controller-side refresh for OAuth); anything else that fails is an error. Always recorded."""
    transport = transport or _http
    c = get(connector_id)
    token = access_token(store, connector_id, transport)
    if token is None:
        if credential(connector_id) is None:
            _set_state(store, connector_id, "not_connected")
        return state(store, connector_id)  # a failed refresh already recorded needs_login / error
    status, error = _probe_once(c, token, transport)
    if status == "needs_login" and (credential(connector_id) or {}).get("kind") == "oauth":
        if refresh(store, connector_id, transport):
            status, error = _probe_once(c, access_token(store, connector_id, transport) or "", transport)
        else:
            return state(store, connector_id)
    _set_state(store, connector_id, status, error, probed=True)
    return state(store, connector_id)


# ---- job plan (Claude) --------------------------------------------------------------------------

def plan_for_job(store, bot_id: str | None, *, unattended: bool, can_pause: bool,
                 transport: Transport | None = None) -> tuple[dict | None, dict[str, str]]:
    """(plan, env) for one job; plan is {"mcp": {...}, "allowed": [...], "disallowed": [...]} with no
    secret in it, env maps the ${VAR} names to token values for the spawn environment only.
    Nothing for unattended runs (the strict profile refuses third-party MCP) or a bot without a connector."""
    transport = transport or _http
    if not enabled() or bot_id is None or unattended:
        return None, {}
    servers: dict = {}
    allowed: list[str] = []
    disallowed: list[str] = []
    env: dict[str, str] = {}
    for c in REGISTRY.values():
        flags = bot_flags(store, c.id).get(str(bot_id))
        if not flags or not flags["enabled"] or state(store, c.id)["status"] == "needs_login":
            continue
        token = access_token(store, c.id, transport)
        if token is None:
            continue
        env[c.env_var] = token
        servers[c.server] = {"type": "http", "url": c.endpoint,
                             "headers": {"Authorization": f"Bearer ${{{c.env_var}}}"}}
        allowed += [c.tool(name) for name in sorted(c.read_tools)]
        if flags["write_enabled"] and can_pause:
            allowed += [c.tool(name) for name in sorted(c.write_tools)]  # each call still asks (connector_writes)
        else:
            disallowed += [c.tool(name) for name in sorted(c.write_tools)]
    if not servers:
        return None, {}
    return {"mcp": {"mcpServers": servers}, "allowed": allowed, "disallowed": disallowed}, env


def isolate_mcp() -> bool:
    """Claude jobs load only the --mcp-config below (--strict-mcp-config): not ~/.claude.json or any other user MCP."""
    return enabled()


def extend_mcp_config(job, base: dict | None = None) -> dict | None:
    """The --mcp-config document for this job: *base* (e.g. a team tool's) plus the enabled connectors."""
    plan = getattr(job, "connector_plan", None) if job is not None else None
    if not plan or not plan.get("mcp"):
        return base
    return {"mcpServers": {**((base or {}).get("mcpServers") or {}), **plan["mcp"]["mcpServers"]}}


def allowed_tools(job) -> list[str]:
    plan = getattr(job, "connector_plan", None) if job is not None else None
    return list(plan["allowed"]) if plan else []


def disallowed_tools(job) -> list[str]:
    plan = getattr(job, "connector_plan", None) if job is not None else None
    return list(plan["disallowed"]) if plan else []


def job_env(job) -> dict[str, str]:
    return dict(getattr(job, "connector_env", None) or {}) if job is not None else {}


# ---- what Settings shows ------------------------------------------------------------------------

def listing(store, bots: list[dict]) -> list[dict]:
    """Every connector with status and per-bot flags. Never a token, never a token's length or tail."""
    result = []
    for c in REGISTRY.values():
        cred = credential(c.id)
        flags = bot_flags(store, c.id)
        result.append({
            "id": c.id, "label": c.label, "endpoint": c.endpoint, "docs": c.docs, "auth_kinds": list(c.auth_kinds),
            "connected_with": cred.get("kind") if cred else None, **state(store, c.id),
            "read_tools": sorted(c.read_tools), "write_tools": sorted(c.write_tools),
            "bots": [{"bot_id": b["id"], "name": b["name"], **flags.get(b["id"], {"enabled": False, "write_enabled": False})}
                     for b in bots]})
    return result
