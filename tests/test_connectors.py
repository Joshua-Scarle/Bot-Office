"""E8 Slice 1: official connectors (Railway, Cloudflare) for Claude jobs."""
import base64
import hashlib
import json
import logging
import os
import re
import stat
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from botoffice import approval_classes, connectors
from botoffice.api import create_app
from botoffice.domain import Job, JobStatus, Slot
from botoffice.needs_you import needs_you
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}
SECRET = "sk-test-SECRETVALUE-0123456789"
ACCESS = "access-SECRETVALUE-abcdef"
REFRESH = "refresh-SECRETVALUE-abcdef"
RAILWAY = connectors.get("railway")
CLOUDFLARE = connectors.get("cloudflare")


class Fake:
    """A scripted transport: (method, url) -> (status, headers, body) or a callable or an exception."""

    def __init__(self):
        self.calls, self.routes = [], {}

    def on(self, method, url, reply):
        self.routes[(method, url)] = reply

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        reply = self.routes.get((method, url))
        if reply is None:
            return 404, {}, b"{}"
        if callable(reply):
            reply = reply(headers, body)
        if isinstance(reply, Exception):
            raise reply
        status, payload = reply[0], reply[1]
        return status, (reply[2] if len(reply) > 2 else {}), payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def mcp_ok(c, session="s1"):
    def reply(headers, body):
        method = json.loads(body).get("method")
        if method == "initialize":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}, {"mcp-session-id": session}
        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
        return 202, b""
    return reply


def oauth_routes(fake, c=RAILWAY, token_reply=None):
    issuer = "https://auth.example.test"
    fake.on("GET", c.prm_url, (200, {"resource": c.endpoint, "authorization_servers": [issuer]}))
    fake.on("GET", issuer + "/.well-known/oauth-authorization-server", (200, {
        "issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
        "registration_endpoint": issuer + "/register", "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["offline_access", *c.scopes]}))
    fake.on("POST", issuer + "/register", (201, {"client_id": "client-123"}))
    fake.on("POST", issuer + "/token", token_reply or (200, {
        "access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}))
    fake.on("POST", c.endpoint, mcp_ok(c))
    return issuer


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".bot-office" / "connectors"
    monkeypatch.setenv(connectors.DIR_ENV, str(path))
    monkeypatch.delenv(connectors.KILL_ENV, raising=False)
    return path


@pytest.fixture
def fake(monkeypatch):
    transport = Fake()
    monkeypatch.setattr(connectors, "_http", transport)
    return transport


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "db.sqlite3")


@pytest.fixture
def bot(store):
    return store.get_bot(store.create_bot("Ada", "", "claude", None).id)


def connect(store, bot, cid="railway", token=SECRET, write=False):
    connectors.save_token(store, cid, token)
    connectors.set_bot(store, cid, str(bot.id), enabled=True, write_enabled=write)


def db_bytes(store):
    return Path(store.path).read_bytes() + b"".join(p.read_bytes() for p in Path(store.path).parent.glob("*sqlite3-*"))


def job_for(bot, plan=None, env=None, **kw):
    return Job(id=uuid4(), prompt="hi", status=JobStatus.RUNNING, created_at=datetime.now(), slot=Slot.DESKTOP_2,
               bot_id=bot.id, cli_access="full", connector_plan=plan, connector_env=env or {}, **kw)


# ---- registry ----------------------------------------------------------------------------------

def test_registry_is_railway_and_cloudflare_official_https():
    assert set(connectors.REGISTRY) == {"railway", "cloudflare"}
    assert RAILWAY.endpoint == "https://mcp.railway.com"
    assert CLOUDFLARE.endpoint == "https://mcp.cloudflare.com/mcp"
    for c in connectors.REGISTRY.values():
        assert c.endpoint.startswith("https://") and c.auth_kinds == ("oauth", "token")
        assert not (c.read_tools & c.write_tools)
        assert "$" not in c.env_var


def test_unknown_tool_is_a_write_and_known_reads_are_not():
    pattern = re.compile(connectors.write_tool_regex(), re.IGNORECASE)
    assert pattern.search("mcp__bo_railway__redeploy")
    assert pattern.search("mcp__bo_railway__some-new-tool")  # unknown = write
    assert pattern.search("mcp__bo_cloudflare__execute")
    assert not pattern.search("mcp__bo_railway__list-projects")
    assert not pattern.search("mcp__bo_cloudflare__search")
    assert not pattern.search("mcp__other__redeploy")
    assert not pattern.search("Bash")


# ---- secret store ------------------------------------------------------------------------------

def test_store_modes_location_and_no_plaintext_in_db(secrets_dir, store, bot):
    connect(store, bot)
    connectors.probe(store, "railway", Fake())  # a failing probe still records state
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    files = [p for p in secrets_dir.iterdir()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    assert SECRET.encode() not in db_bytes(store)
    repo = Path(connectors.__file__).resolve().parents[2]
    assert repo not in secrets_dir.resolve().parents
    assert SECRET in (secrets_dir / "railway.json").read_text()


@pytest.mark.parametrize("where", ["repo", "data"])
def test_store_refuses_a_directory_inside_the_repo_or_install_tree(monkeypatch, where):
    repo = Path(connectors.__file__).resolve().parents[2]
    target = repo / "secrets" if where == "repo" else repo / "data" / "connectors"
    monkeypatch.setenv(connectors.DIR_ENV, str(target))
    with pytest.raises(connectors.ConnectorError):
        connectors.save_token(None, "railway", SECRET)
    assert not target.exists()


def test_default_dir_is_outside_every_tree(monkeypatch):
    monkeypatch.delenv(connectors.DIR_ENV, raising=False)
    assert connectors.store_dir() == Path.home() / ".bot-office" / "connectors"


def test_protected_paths_hide_the_store_from_the_cli(secrets_dir, settings, store):
    scheduler = Scheduler(store, settings, {"claude": FakeAdapter()})
    assert str(secrets_dir) in scheduler.protected_paths()


# ---- per-bot enablement, write flag, plan ------------------------------------------------------

def test_enablement_and_write_flag_default_off(secrets_dir, store, bot):
    assert connectors.bot_flags(store, "railway") == {}
    assert connectors.set_bot(store, "railway", str(bot.id)) == {"enabled": False, "write_enabled": False}
    assert connectors.set_bot(store, "railway", str(bot.id), enabled=True) == {"enabled": True, "write_enabled": False}
    other = store.get_bot(store.create_bot("Bo", "", "claude", None).id)
    assert connectors.bot_flags(store, "railway").get(str(other.id)) is None


def test_plan_reads_allowed_writes_omitted_when_flag_off(secrets_dir, store, bot):
    connect(store, bot)
    plan, env = connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True)
    assert plan["mcp"]["mcpServers"]["bo_railway"]["url"] == "https://mcp.railway.com"
    assert "mcp__bo_railway__list-projects" in plan["allowed"]
    assert not any(name.endswith(("redeploy", "railway-agent")) for name in plan["allowed"])
    assert "mcp__bo_railway__redeploy" in plan["disallowed"]
    assert env == {RAILWAY.env_var: SECRET}


def test_write_tools_only_with_flag_and_an_engine_that_can_pause(secrets_dir, store, bot):
    connect(store, bot, write=True)
    plan, _ = connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True)
    assert "mcp__bo_railway__redeploy" in plan["allowed"] and "mcp__bo_railway__redeploy" not in plan["disallowed"]
    for provider, access in (("opencode", "full"), ("codex", "restricted")):
        assert not approval_classes.can_pause(provider, access)
    plan, _ = connectors.plan_for_job(store, str(bot.id), unattended=False,
                                      can_pause=approval_classes.can_pause("opencode", "full"))
    assert "mcp__bo_railway__redeploy" not in plan["allowed"] and "mcp__bo_railway__redeploy" in plan["disallowed"]
    assert "mcp__bo_railway__list-projects" in plan["allowed"]  # reads stay


def test_no_plan_for_unattended_disabled_unconnected_or_killed(secrets_dir, store, bot, monkeypatch):
    assert connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True) == (None, {})
    connect(store, bot)
    assert connectors.plan_for_job(store, str(bot.id), unattended=True, can_pause=True) == (None, {})
    assert connectors.plan_for_job(store, None, unattended=False, can_pause=True) == (None, {})
    connectors.set_bot(store, "railway", str(bot.id), enabled=False)
    assert connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True) == (None, {})
    connectors.set_bot(store, "railway", str(bot.id), enabled=True)
    monkeypatch.setenv(connectors.KILL_ENV, "0")
    assert connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True) == (None, {})
    assert not connectors.isolate_mcp()


def test_needs_login_connector_is_not_injected(secrets_dir, store, bot, fake):
    connect(store, bot)
    fake.on("POST", RAILWAY.endpoint, (401, b""))
    assert connectors.probe(store, "railway")["status"] == "needs_login"
    assert connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True) == (None, {})


# ---- approval classes --------------------------------------------------------------------------

def test_write_tools_raise_a_card_reads_do_not(store):
    names = ["mcp__bo_railway__redeploy", "mcp__bo_railway__brand-new-tool", "mcp__bo_cloudflare__execute"]
    for name in names:
        assert approval_classes.match(store, name, "") is not None, name
    assert approval_classes.match(store, "mcp__bo_railway__list-projects", "") is None
    assert approval_classes.match(store, "mcp__bo_cloudflare__search", "") is None
    assert approval_classes.match(store, "mcp__bo_railway__brand-new-tool", "").key == "connector_writes"
    # "Any connector action" still catches reads when the owner turns it on (no second gate)
    approval_classes.update(store, enabled={"connectors": True})
    assert approval_classes.match(store, "mcp__bo_railway__list-projects", "").key == "connectors"


# ---- Claude launch: --mcp-config, --strict-mcp-config, no secrets in argv ----------------------

def claude_args(job, tmp_path):
    return ClaudeAdapter(executable="claude")._exec_args(tmp_path, job, None)


def test_claude_args_merge_connector_config_strict_and_no_secret(secrets_dir, store, bot, tmp_path, monkeypatch):
    # a user-level wrapper like the host's railway-mcp-readonly must not reach the job
    home = tmp_path / "userhome"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"railway": {"command": "railway-mcp-readonly"}}}))
    monkeypatch.setenv("HOME", str(home))
    connect(store, bot)
    plan, env = connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True)
    args = claude_args(job_for(bot, plan, env), tmp_path)
    assert "--strict-mcp-config" in args
    config = json.loads(args[args.index("--mcp-config") + 1])
    assert set(config["mcpServers"]) == {"bo_railway"}
    assert config["mcpServers"]["bo_railway"]["headers"]["Authorization"] == "Bearer ${BOT_OFFICE_CONNECTOR_RAILWAY_TOKEN}"
    joined = "\0".join(args)
    assert SECRET not in joined and "railway-mcp-readonly" not in joined
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "mcp__bo_railway__list-projects" in allowed and "mcp__bo_railway__redeploy" not in allowed
    assert "mcp__bo_railway__redeploy" in "\0".join(a for i, a in enumerate(args) if args[i - 1] == "--disallowedTools")
    assert args.index("--add-dir") < args.index("--mcp-config")  # same site E7 will use: after --add-dir


def test_claude_args_without_connectors_still_isolate_user_mcp(bot, tmp_path):
    args = claude_args(job_for(bot), tmp_path)
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in args and "--allowedTools" not in args


def test_kill_switch_restores_the_old_launch(bot, tmp_path, monkeypatch):
    monkeypatch.setenv(connectors.KILL_ENV, "0")
    args = claude_args(job_for(bot), tmp_path)
    assert "--mcp-config" not in args and "--strict-mcp-config" not in args


def test_unattended_allow_list_has_no_connector_tools(bot):
    from botoffice.providers.claude import UNATTENDED_TOOLS, claude_settings
    job = job_for(bot, {"mcp": {"mcpServers": {}}, "allowed": ["mcp__bo_railway__whoami"], "disallowed": []},
                  gate_command="gate", unattended=True, secret_paths=["/x"])
    allow = claude_settings(job, True)["permissions"]["allow"]
    assert allow == list(UNATTENDED_TOOLS) and not any(a.startswith("mcp") for a in allow)


def test_token_reaches_the_spawn_environment_only(secrets_dir, store, bot, tmp_path):
    connect(store, bot)
    plan, env = connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True)
    job = job_for(bot, plan, env, gate_command="gate", agent_token="agent-token")
    assert ClaudeAdapter(executable="claude")._env(tmp_path, job)[RAILWAY.env_var] == SECRET
    assert SECRET not in job.model_dump_json() and SECRET not in repr(job)
    assert SECRET not in json.dumps(plan)


def test_extend_mcp_config_unions_with_a_base():
    job = Job(id=uuid4(), prompt="x", status=JobStatus.RUNNING, created_at=datetime.now(), slot=Slot.DESKTOP_2,
              connector_plan={"mcp": {"mcpServers": {"bo_railway": {"url": "u"}}}, "allowed": [], "disallowed": []})
    merged = connectors.extend_mcp_config(job, {"mcpServers": {"team": {"url": "t"}}})
    assert set(merged["mcpServers"]) == {"team", "bo_railway"}
    assert connectors.extend_mcp_config(None, {"mcpServers": {}}) == {"mcpServers": {}}


# ---- OAuth -------------------------------------------------------------------------------------

def test_authorize_url_has_state_pkce_s256_resource_and_loopback_redirect(secrets_dir, fake):
    oauth_routes(fake)
    url = connectors.start_oauth("railway", 8787)
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://auth.example.test/authorize?")
    assert query["redirect_uri"] == "http://127.0.0.1:8787/v1/connectors/oauth/callback"
    assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
    assert query["client_id"] == "client-123" and query["resource"] == "https://mcp.railway.com"
    assert "offline_access" in query["scope"] and len(query["state"]) >= 32
    pending = json.loads((secrets_dir / connectors._pending_name(query["state"])).read_text())
    digest = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == digest and pending["verifier"] not in url
    registration = json.loads(next(c for c in fake.calls if c[1].endswith("/register"))[3])
    assert registration["redirect_uris"] == [query["redirect_uri"]] and "client_secret" not in registration
    connectors.start_oauth("railway", 8787)  # the registered client is reused
    assert sum(c[1].endswith("/register") for c in fake.calls) == 1


def test_oauth_discovery_needs_pkce_s256(secrets_dir, fake):
    issuer = oauth_routes(fake)
    fake.on("GET", issuer + "/.well-known/oauth-authorization-server", (200, {
        "issuer": issuer, "authorization_endpoint": issuer + "/a", "token_endpoint": issuer + "/t",
        "code_challenge_methods_supported": ["plain"]}))
    with pytest.raises(connectors.ConnectorError, match="S256"):
        connectors.start_oauth("railway", 8787)


def start_state(port=8787):
    return parse_qs(urlparse(connectors.start_oauth("railway", port)).query)["state"][0]


def test_callback_exchange_stores_tokens_and_state_is_single_use(secrets_dir, store, fake):
    oauth_routes(fake)
    state = start_state()
    assert connectors.complete_oauth(store, state, "code-1") == "railway"
    exchange = next(c for c in fake.calls if c[1].endswith("/token"))
    form = parse_qs(exchange[3].decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["code-1"]
    assert form["code_verifier"] and form["resource"] == ["https://mcp.railway.com"]
    cred = connectors.credential("railway")
    assert cred["kind"] == "oauth" and cred["access_token"] == ACCESS and cred["refresh_token"] == REFRESH
    assert stat.S_IMODE((secrets_dir / "railway.json").stat().st_mode) == 0o600
    with pytest.raises(connectors.ConnectorError):  # replay
        connectors.complete_oauth(store, state, "code-1")
    with pytest.raises(connectors.ConnectorError):  # forged state
        connectors.complete_oauth(store, "forged", "code-1")
    assert ACCESS.encode() not in db_bytes(store)


def test_callback_rejects_expired_state_and_wrong_issuer(secrets_dir, store, fake, monkeypatch):
    oauth_routes(fake)
    state = start_state()
    with pytest.raises(connectors.ConnectorError, match="issuer"):
        connectors.complete_oauth(store, state, "c", iss="https://evil.example.test")
    state = start_state()
    path = secrets_dir / connectors._pending_name(state)
    data = json.loads(path.read_text())
    data["created"] = time.time() - 3600
    path.write_text(json.dumps(data))
    with pytest.raises(connectors.ConnectorError, match="expired"):
        connectors.complete_oauth(store, state, "c")


def test_refresh_success_rotates_and_keeps_the_old_refresh_token_if_not_rotated(secrets_dir, store, fake):
    oauth_routes(fake, token_reply=(200, {"access_token": "new-access-value", "expires_in": 3600}))
    cred = {"kind": "oauth", "access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() - 5,
            "token_endpoint": "https://auth.example.test/token", "resource": "https://mcp.railway.com",
            "client": {"client_id": "client-123"}}
    connectors._write_secret("railway.json", cred)
    assert connectors.access_token(store, "railway") == "new-access-value"
    assert connectors.credential("railway")["refresh_token"] == REFRESH


def expired_oauth(store, token_reply, fake):
    oauth_routes(fake, token_reply=token_reply)
    connectors._write_secret("railway.json", {
        "kind": "oauth", "access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() - 5,
        "token_endpoint": "https://auth.example.test/token", "resource": "https://mcp.railway.com",
        "client": {"client_id": "client-123"}})


def test_refresh_failure_is_needs_login_needs_you_and_a_notification(secrets_dir, store, bot, fake):
    expired_oauth(store, (400, {"error": "invalid_grant"}), fake)
    connectors.set_bot(store, "railway", str(bot.id), enabled=True)
    assert connectors.access_token(store, "railway") is None
    assert connectors.state(store, "railway")["status"] == "needs_login"
    assert connectors.plan_for_job(store, str(bot.id), unattended=False, can_pause=True) == (None, {})
    item = next(i for i in needs_you(store) if i["kind"] == "connector")
    assert item["state"] == "needs_login" and "Railway" in item["ask"] and "Connect" in item["ask"]
    with closing(store._connect()) as db:
        rows = db.execute("SELECT cls, kind, key FROM notify_events WHERE key LIKE 'conn:%'").fetchall()
    assert [(r["cls"], r["kind"]) for r in rows] == [("needs_you", "connector_login")]
    connectors.access_token(store, "railway")  # same episode: no second notification
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM notify_events WHERE key LIKE 'conn:%'").fetchone()[0] == 1
    assert ACCESS.encode() not in db_bytes(store) and REFRESH.encode() not in db_bytes(store)


def test_refresh_network_failure_is_an_error_not_a_lost_login(secrets_dir, store, fake):
    expired_oauth(store, httpx.ConnectError("boom"), fake)
    assert connectors.access_token(store, "railway") is None
    assert connectors.state(store, "railway")["status"] == "error"
    assert connectors.credential("railway")["refresh_token"] == REFRESH


# ---- health probe ------------------------------------------------------------------------------

def test_probe_ok_records_health_time(secrets_dir, store, bot, fake):
    connect(store, bot)
    fake.on("POST", RAILWAY.endpoint, mcp_ok(RAILWAY))
    result = connectors.probe(store, "railway")
    assert result["status"] == "ok" and result["last_health_at"] and result["last_error_class"] is None
    sent = [c for c in fake.calls if c[1] == RAILWAY.endpoint]
    assert [json.loads(c[3]).get("method") for c in sent] == ["initialize", "notifications/initialized", "tools/list"]
    assert sent[0][2]["authorization"] == f"Bearer {SECRET}" and sent[1][2]["mcp-session-id"] == "s1"


def test_probe_reads_an_sse_reply(secrets_dir, store, bot, fake):
    connect(store, bot)
    sse = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
    fake.on("POST", RAILWAY.endpoint, (200, sse))
    assert connectors.probe(store, "railway")["status"] == "ok"


@pytest.mark.parametrize("status,cls", [(401, "http_401"), (403, "http_403")])
def test_probe_401_403_is_needs_login_with_needs_you(secrets_dir, store, bot, fake, status, cls):
    connect(store, bot)
    fake.on("POST", RAILWAY.endpoint, (status, b""))
    result = connectors.probe(store, "railway")
    assert (result["status"], result["last_error_class"]) == ("needs_login", cls)
    assert any(i["kind"] == "connector" and i["state"] == "needs_login" for i in needs_you(store))


@pytest.mark.parametrize("reply,cls", [((503, b"oops"), "http_5xx"), ((404, b""), "http_4xx"),
                                       ((200, b"not json"), "bad_response"), (httpx.ConnectError("x"), "network"),
                                       (httpx.ReadTimeout("x"), "timeout")])
def test_probe_other_failures_are_errors_never_silent(secrets_dir, store, bot, fake, reply, cls):
    connect(store, bot)
    fake.on("POST", RAILWAY.endpoint, reply)
    result = connectors.probe(store, "railway")
    assert (result["status"], result["last_error_class"]) == ("error", cls)
    item = next(i for i in needs_you(store) if i["kind"] == "connector")
    assert item["state"] == "error" and cls in item["ask"]
    with closing(store._connect()) as db:
        assert db.execute("SELECT kind FROM notify_events WHERE key LIKE 'conn:%'").fetchone()["kind"] == "connector_error"


def test_oauth_401_refreshes_once_and_retries(secrets_dir, store, fake):
    oauth_routes(fake)
    connectors._write_secret("railway.json", {
        "kind": "oauth", "access_token": "stale-access-value", "refresh_token": REFRESH, "expires_at": time.time() + 3600,
        "token_endpoint": "https://auth.example.test/token", "resource": "https://mcp.railway.com",
        "client": {"client_id": "client-123"}})
    fake.on("POST", RAILWAY.endpoint, lambda h, b: (401, b"") if "stale" in h["authorization"] else mcp_ok(RAILWAY)(h, b))
    assert connectors.probe(store, "railway")["status"] == "ok"
    assert connectors.credential("railway")["access_token"] == ACCESS


def test_probe_without_credential_is_not_connected(secrets_dir, store):
    assert connectors.probe(store, "railway")["status"] == "not_connected"
    assert not any(i["kind"] == "connector" for i in needs_you(store))


# ---- API and UI --------------------------------------------------------------------------------

@pytest.fixture
def client(settings, secrets_dir, fake):
    return TestClient(create_app(settings, {"claude": FakeAdapter()}))


def test_api_requires_the_owner_token(client):
    for call in (lambda h: client.get("/v1/connectors", headers=h),
                 lambda h: client.post("/v1/connectors/railway/token", headers=h, json={"token": SECRET}),
                 lambda h: client.post("/v1/connectors/railway/oauth/start", headers=h),
                 lambda h: client.post("/v1/connectors/railway/revoke", headers=h)):
        assert call({}).status_code == 401
        assert call({"Authorization": "Bearer an-agent-job-token"}).status_code == 401


def test_every_connector_route_but_the_browser_callback_uses_the_owner_dependency(client):
    routes = {r.path: r for r in client.app.routes if r.path.startswith("/v1/connectors")}
    assert len(routes) >= 7 and connectors.CALLBACK_PATH in routes
    for path, route in routes.items():  # the owner dependency compares the master token only, never an agent token
        if path != connectors.CALLBACK_PATH:
            assert any("require_token" in d.call.__qualname__ for d in route.dependant.dependencies), path


def test_paste_fallback_uses_the_body_and_never_echoes(client, fake, secrets_dir, caplog):
    fake.on("POST", RAILWAY.endpoint, mcp_ok(RAILWAY))
    with caplog.at_level(logging.DEBUG):
        assert client.post("/v1/connectors/railway/token?token=query-only-value-9", headers=H).status_code == 422  # not read from the URL
        response = client.post("/v1/connectors/railway/token", headers=H, json={"token": SECRET})
        listing = client.get("/v1/connectors", headers=H)
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert SECRET not in response.text and SECRET not in listing.text and SECRET not in caplog.text
    assert listing.json()["connectors"][0]["connected_with"] == "token"
    assert client.post("/v1/connectors/railway/token", headers=H, json={"token": "x y"}).status_code == 422


def test_list_enable_write_flag_revoke_roundtrip(client, secrets_dir):
    bot = client.post("/v1/bots", headers=H, json={"name": "Ada", "default_provider": "claude"}).json()
    rows = client.get("/v1/connectors", headers=H).json()["connectors"]
    railway = next(c for c in rows if c["id"] == "railway")
    assert railway["status"] == "not_connected" and railway["bots"][0]["enabled"] is False
    assert railway["bots"][0]["write_enabled"] is False
    url = f"/v1/connectors/railway/bots/{bot['id']}"
    assert client.put(url, headers=H, json={"enabled": True}).json() == {"enabled": True, "write_enabled": False}
    assert client.put(url, headers=H, json={"write_enabled": True}).json() == {"enabled": True, "write_enabled": True}
    assert client.put(url, headers=H, json={"secret": 1}).status_code == 422
    assert client.put(f"/v1/connectors/railway/bots/{uuid4()}", headers=H, json={"enabled": True}).status_code == 404
    assert client.put(f"/v1/connectors/nope/bots/{bot['id']}", headers=H, json={}).status_code == 404
    client.post("/v1/connectors/railway/token", headers=H, json={"token": SECRET})
    assert client.post("/v1/connectors/railway/revoke", headers=H).json()["status"] == "not_connected"
    assert not (secrets_dir / "railway.json").exists()


def test_oauth_flow_through_the_routes(client, fake, secrets_dir, caplog):
    oauth_routes(fake)
    with caplog.at_level(logging.DEBUG):
        url = client.post("/v1/connectors/railway/oauth/start", headers=H).json()["authorize_url"]
        state = parse_qs(urlparse(url).query)["state"][0]
        assert client.get("/v1/connectors/oauth/callback", params={"code": "c", "state": "forged"}).status_code == 400
        page = client.get("/v1/connectors/oauth/callback", params={"code": "c", "state": state})  # no bearer: a browser
        listing = client.get("/v1/connectors", headers=H)
    assert page.status_code == 200 and "Railway connected" in page.text
    assert listing.json()["connectors"][0]["status"] == "ok"
    assert listing.json()["callback"] == "http://127.0.0.1:8787/v1/connectors/oauth/callback"
    for text in (page.text, listing.text, caplog.text, url):
        assert ACCESS not in text and REFRESH not in text
    assert client.get("/v1/connectors/oauth/callback", params={"code": "c", "state": state}).status_code == 400  # replay


def test_callback_provider_error_clears_the_pending_sign_in(client, fake, secrets_dir):
    oauth_routes(fake, CLOUDFLARE)
    url = client.post("/v1/connectors/cloudflare/oauth/start", headers=H).json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    page = client.get("/v1/connectors/oauth/callback", params={"error": "access_denied", "state": state})
    assert page.status_code == 400 and "access_denied" in page.text
    assert not (secrets_dir / connectors._pending_name(state)).exists()


def test_connect_failure_is_reported_not_swallowed(client, fake, secrets_dir):
    response = client.post("/v1/connectors/railway/oauth/start", headers=H)  # discovery 404s
    assert response.status_code == 502 and "discovery" in response.json()["detail"]


def test_settings_panel_has_connect_masked_paste_and_no_reveal():
    html = (Path(connectors.__file__).parent / "static" / "bot-office.html").read_text()
    section = html[html.index('id="set-connectors"'):]
    section = section[:section.index("</section>")]
    assert "Connectors" in section and "Reads only by default" in section
    script = html[html.index("function showConnectors"):html.index("async function loadSettings")]
    assert '"Connect"' in script and 'input.type = "password"' in script and 'autocomplete = "off"' in script
    assert "reveal" not in script.lower() and "show token" not in script.lower()
    assert 'input.value = ""' in script  # the pasted value leaves the DOM on save
    assert "token}" in script and "?token" not in script  # POST body only
