"""Remote access through Cloudflare Tunnel + Cloudflare Access (0.12.23).

Bot-Office keeps listening on loopback only. ``cloudflared`` on the host connects outbound and forwards
to 127.0.0.1:8787; Cloudflare Access sits in front and puts a signed JWT (RS256) in the
``Cf-Access-Jwt-Assertion`` header. This module checks that JWT again inside Bot-Office, fails closed,
and maps the identity to a role:

* ``owner``         a person whose email is in ``owners`` (a browser, full GUI and API).
* ``remote-client`` a service token listed in ``clients`` (least privilege: a fixed route allowlist).

Stopgap without Cloudflare Access (0.12.23): ``shared_keys`` lets a caller that sends a long random key
in the ``X-Bot-Office-Key`` header in through the tunnel. Only the key's SHA-256 is stored, every key
has an ``expires_at``, and a shared key can only ever have the ``remote-client`` role (never owner).

Local requests (loopback, no Cloudflare headers) pass through completely unchanged.

Pure standard library. ``httpx`` is imported lazily, only inside the default JWKS fetcher, so this
module and its tests import without fastapi or httpx. No hostnames, emails or tokens live in code.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("botoffice.remote")

CONFIG_ENV = "BOT_OFFICE_REMOTE_ACCESS_FILE"
CONFIG_NAME = "remote_access.json"
CONFIG_RECHECK_SECONDS = 5.0
KEY_CACHE_SECONDS = 3600.0
KEY_REFETCH_MIN_SECONDS = 60.0
CLOCK_SKEW_SECONDS = 60
MIN_RSA_BITS = 2048
FETCH_TIMEOUT_SECONDS = 5.0

ROLE_OWNER = "owner"
ROLE_CLIENT = "remote-client"
ROLES = {ROLE_OWNER, ROLE_CLIENT}

# Headers that only Cloudflare (or someone forging Cloudflare) adds. Any of them marks a request REMOTE.
CLOUDFLARE_HEADERS = (
    b"cf-ray", b"cf-connecting-ip", b"cf-access-jwt-assertion",
    b"cf-access-client-id", b"cf-access-client-secret", b"cf-visitor",
)
JWT_HEADER = b"cf-access-jwt-assertion"
KEY_HEADER = b"x-bot-office-key"
# Remote credentials: never passed on to the app (0.12.35 review).
CREDENTIAL_HEADERS = (JWT_HEADER, KEY_HEADER, b"cf-access-client-id", b"cf-access-client-secret")
ACCESS_COOKIES = (b"CF_Authorization",)   # the Access session cookie: a replayable owner credential
KEY_MIN_CHARS = 43          # 256 bits of base64url, the shortest key we accept
KEY_MAX_CHARS = 256
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# The identity of the request being served, set by the middleware only after the JWT, the host, the
# role and the route were all checked. auth.require_token trusts it; nothing else may set it.
current_remote_identity: contextvars.ContextVar[RemoteIdentity | None] = contextvars.ContextVar(
    "botoffice_remote_identity", default=None)

# ---------------------------------------------------------------------------------------------
# Route allowlist for role "remote-client" (a Claude cloud session holding a service token).
# Every entry is (method, fully anchored path regex). Anything not listed is refused with 403.
# Deliberately absent: slot exec, settings, bot create/patch/delete/retire, session reset, slash
# commands, memory and shared-memory writes, baseline/rules layers, watchers, budgets, attachments,
# icon upload/generate, voice, desktops/VNC, bridge/inbox, /v1/agent/*, message edit, inbox post.
# ---------------------------------------------------------------------------------------------
_ID = r"[0-9a-fA-F-]{1,64}"
REMOTE_CLIENT_ALLOWLIST: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern)) for method, pattern in (
        ("GET", r"^/healthz$"),
        ("GET", r"^/v1/remote/whoami$"),
        ("GET", r"^/v1/providers$"),
        ("GET", r"^/v1/sections$"),
        ("GET", r"^/v1/bots$"),
        ("GET", rf"^/v1/bots/{_ID}$"),
        ("GET", rf"^/v1/bots/{_ID}/messages$"),
        ("GET", rf"^/v1/bots/{_ID}/history$"),
        ("GET", rf"^/v1/bots/{_ID}/icon$"),
        ("POST", rf"^/v1/bots/{_ID}/messages$"),          # an owner chat message to a bot
        ("POST", rf"^/v1/bots/{_ID}/read$"),
        ("POST", rf"^/v1/bots/{_ID}/unread$"),
        ("POST", rf"^/v1/bots/{_ID}/questions/{_ID}/answer$"),  # answer a bot's question card
        ("GET", rf"^/v1/bots/{_ID}/assignments$"),
        ("POST", rf"^/v1/bots/{_ID}/assignments$"),
        ("GET", rf"^/v1/assignments/{_ID}$"),
        ("GET", rf"^/v1/assignments/{_ID}/timeline$"),
        ("POST", rf"^/v1/assignments/{_ID}/(?:reply|start|stop)$"),
        ("GET", r"^/v1/jobs$"),
        ("GET", rf"^/v1/jobs/{_ID}$"),
        ("GET", rf"^/v1/jobs/{_ID}/events$"),
        ("POST", rf"^/v1/jobs/{_ID}/cancel$"),
        ("GET", r"^/v1/needs-you$"),
        ("POST", r"^/v1/needs-you/dismiss$"),
    )
)

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_TEAM_DOMAIN = re.compile(r"^[a-z0-9-]+\.cloudflareaccess\.com$")
_DNS_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
# DER DigestInfo prefix for SHA-256 (RFC 8017 section 9.2)
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class AccessDenied(Exception):
    """A request failed verification. The message is for the log only, never for the client."""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RemoteIdentity:
    kind: str          # "user" (a person through the browser) or "service" (a service token)
    identity: str      # lowercased email, or the service token's common name
    display_name: str
    role: str          # "owner" or "remote-client"
    expires_at: float | None = field(default=None, compare=False)   # the JWT's exp (owner websockets close then)


@dataclass(frozen=True)
class RemoteConfig:
    team_domain: str | None               # None when only shared keys are set up (no Cloudflare Access yet)
    audience: tuple[str, ...]
    hostnames: tuple[str, ...]
    owners: tuple[str, ...]
    clients: dict[str, tuple[str, str]]   # common_name -> (name, role)
    shared_keys: dict[str, tuple[str, float]] = field(default_factory=dict)  # sha256 hex -> (name, expires_at epoch seconds)


@dataclass(frozen=True)
class ConfigResult:
    state: str                              # "disabled" | "ok" | "broken"
    config: RemoteConfig | None = None


# ---------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------
def parse_config(raw: Any) -> RemoteConfig | None:
    """Validate strictly. Returns None when the file says ``enabled: false``; raises ConfigError otherwise."""
    if not isinstance(raw, dict):
        raise ConfigError("top level must be an object")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be true or false")
    if not enabled:
        return None
    shared_keys = _parse_shared_keys(raw.get("shared_keys", {}))
    team = raw.get("team_domain")
    audience = raw.get("audience")
    if team is None and audience is None:
        if not shared_keys:
            raise ConfigError("set team_domain and audience (Cloudflare Access), or at least one shared key")
        audience = []
    else:
        if not isinstance(team, str) or not _TEAM_DOMAIN.match(team):
            raise ConfigError("team_domain must look like <team>.cloudflareaccess.com")
        if (not isinstance(audience, list) or not audience
                or not all(isinstance(a, str) and a.strip() for a in audience)):
            raise ConfigError("audience must be a non-empty list of non-empty strings")
    hostnames = raw.get("hostnames")
    if (not isinstance(hostnames, list) or not hostnames
            or not all(isinstance(h, str) and _DNS_NAME.match(h) for h in hostnames)):
        raise ConfigError("hostnames must be a non-empty list of lowercase DNS names (no port, no scheme)")
    owners = raw.get("owners", [])
    if not isinstance(owners, list) or not all(isinstance(o, str) and _EMAIL.match(o.strip()) for o in owners):
        raise ConfigError("owners must be a list of email addresses")
    clients_raw = raw.get("clients", {})
    if not isinstance(clients_raw, dict):
        raise ConfigError("clients must be an object")
    clients: dict[str, tuple[str, str]] = {}
    for common_name, entry in clients_raw.items():
        if not isinstance(common_name, str) or not common_name.strip():
            raise ConfigError("client keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise ConfigError("each client must be an object with name and role")
        name, role = entry.get("name"), entry.get("role")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("each client needs a name")
        # 0.12.35 review: a service token is always least privilege. "owner" is a person (an email
        # in owners) only; a stolen owner-role token would otherwise be full control with no Origin check.
        if not isinstance(role, str) or role != ROLE_CLIENT:
            raise ConfigError("client role must be 'remote-client' (owner is only for emails in owners)")
        clients[common_name] = (name.strip(), role)
    return RemoteConfig(
        team_domain=team,
        audience=tuple(a.strip() for a in audience),
        hostnames=tuple(hostnames),
        owners=tuple(sorted({o.strip().lower() for o in owners})),
        clients=clients,
        shared_keys=shared_keys,
    )


def _parse_expiry(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("each shared key needs expires_at, an ISO 8601 time with a time zone")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError("expires_at must be an ISO 8601 time, e.g. 2026-12-01T00:00:00Z") from exc
    if moment.tzinfo is None:
        raise ConfigError("expires_at must include a time zone (end it with Z for UTC)")
    return moment.timestamp()


def _parse_shared_keys(raw: Any) -> dict[str, tuple[str, float]]:
    if not isinstance(raw, dict):
        raise ConfigError("shared_keys must be an object")
    keys: dict[str, tuple[str, float]] = {}
    for digest, entry in raw.items():
        if not isinstance(digest, str) or not _SHA256_HEX.match(digest):
            raise ConfigError("shared_keys keys must be the key's SHA-256, 64 lowercase hex characters")
        if not isinstance(entry, dict):
            raise ConfigError("each shared key must be an object with name and expires_at")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("each shared key needs a name")
        if entry.get("role", ROLE_CLIENT) != ROLE_CLIENT:
            raise ConfigError("a shared key can only have the 'remote-client' role")
        keys[digest] = (name.strip(), _parse_expiry(entry.get("expires_at")))
    return keys


def check_shared_key(value: bytes, config: RemoteConfig, now: float) -> RemoteIdentity:
    """Constant-time match of a presented key against the stored SHA-256 digests."""
    keys = config.shared_keys or {}
    if not keys:
        raise AccessDenied("no shared keys set up")
    try:
        text = value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise AccessDenied("key is not ascii") from exc
    if not KEY_MIN_CHARS <= len(text) <= KEY_MAX_CHARS:
        raise AccessDenied("key has the wrong length")
    presented = hashlib.sha256(text.encode("ascii")).hexdigest()
    match: tuple[str, float] | None = None
    for digest, entry in keys.items():          # compare against every entry, no early exit
        if hmac.compare_digest(presented, digest):
            match = entry
    if match is None:
        raise AccessDenied("unknown key")
    name, expires_at = match
    if now >= expires_at:
        raise AccessDenied(f"key {name} has expired")
    return RemoteIdentity("service", name, name, ROLE_CLIENT)


class ConfigLoader:
    """Loads the JSON file, and reloads it when its mtime changes (checked at most once per 5 s)."""

    def __init__(self, path_fn: Callable[[], Path], clock: Callable[[], float] = time.time):
        self._path_fn = path_fn
        self._clock = clock
        self._checked_at: float | None = None
        self._signature: tuple[int, int] | None | str = "unset"
        self._result = ConfigResult("disabled")
        self._lock = threading.Lock()

    def get(self) -> ConfigResult:
        with self._lock:
            now = self._clock()
            if self._checked_at is not None and now - self._checked_at < CONFIG_RECHECK_SECONDS:
                return self._result
            self._checked_at = now
            path = self._path_fn()
            try:
                st = path.stat()
                signature: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                signature = None
            except OSError as exc:
                signature = None
                self._signature = "unset"
                log.error("remote access config unreadable (%s)", type(exc).__name__)
                self._result = ConfigResult("broken")
                return self._result
            if signature == self._signature:
                return self._result
            self._signature = signature
            if signature is None:
                self._result = ConfigResult("disabled")
                return self._result
            try:
                cfg = parse_config(json.loads(path.read_text(encoding="utf-8")))
            except (ConfigError, ValueError, OSError) as exc:
                # logged once per file version (the signature gate above), never the file's contents
                log.error("remote access config invalid, failing closed: %s", exc)
                self._result = ConfigResult("broken")
            except Exception as exc:   # noqa: BLE001  any other parse error also fails closed (never keeps the old config)
                log.error("remote access config invalid, failing closed: %s", type(exc).__name__)
                self._result = ConfigResult("broken")
            else:
                self._result = ConfigResult("ok", cfg) if cfg else ConfigResult("disabled")
            return self._result


def config_path(settings: Any) -> Path:
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else Path(settings.control_dir) / CONFIG_NAME


# ---------------------------------------------------------------------------------------------
# JWT verification (RS256 only)
# ---------------------------------------------------------------------------------------------
def _b64url_decode(segment: str) -> bytes:
    if not segment or not _B64URL.match(segment):
        raise AccessDenied("bad base64url segment")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise AccessDenied("bad base64url segment") from exc


def default_fetcher(url: str) -> dict:
    import httpx  # lazy: the module and its tests import without httpx
    response = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False)
    response.raise_for_status()
    return response.json()


def _parse_jwks(doc: Any) -> dict[str, tuple[int, int]]:
    keys: dict[str, tuple[int, int]] = {}
    items = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("no keys")
    for item in items:
        try:
            if item.get("kty") != "RSA" or not isinstance(item.get("kid"), str):
                continue
            n = int.from_bytes(_b64url_decode(item["n"]), "big")
            e = int.from_bytes(_b64url_decode(item["e"]), "big")
        except (AttributeError, KeyError, TypeError, AccessDenied):
            continue
        if n.bit_length() < MIN_RSA_BITS or e < 3 or e % 2 == 0 or e >= n:
            continue   # reject small moduli and nonsense exponents
        keys[item["kid"]] = (n, e)
    return keys


class KeyCache:
    """JWKS keys by kid: cached 1 hour; an unknown kid refetches at most once per 60 s; on a fetch
    failure the cached keys stay in use."""

    def __init__(self, team_domain: str, fetcher: Callable[[str], dict], clock: Callable[[], float]):
        self.url = f"https://{team_domain}/cdn-cgi/access/certs"
        self._fetcher = fetcher
        self._clock = clock
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched_at: float | None = None
        self._attempt_at: float | None = None
        self._lock = threading.Lock()

    def get(self, kid: str) -> tuple[int, int] | None:
        # 0.12.35 review: the fetch runs OUTSIDE the lock, by one caller at a time; everyone else gets the
        # cached keys at once instead of queueing behind a slow network call.
        with self._lock:
            now = self._clock()
            fresh = self._fetched_at is not None and now - self._fetched_at < KEY_CACHE_SECONDS
            if kid in self._keys and fresh:
                return self._keys[kid]
            if self._attempt_at is not None and now - self._attempt_at < KEY_REFETCH_MIN_SECONDS:
                return self._keys.get(kid)
            self._attempt_at = now
        try:
            keys = _parse_jwks(self._fetcher(self.url))
        except Exception as exc:   # noqa: BLE001  network, JSON or shape errors: keep what we have
            log.warning("could not fetch the Access signing keys (%s)", type(exc).__name__)
            with self._lock:
                return self._keys.get(kid)
        with self._lock:
            self._keys = keys
            self._fetched_at = now
            return self._keys.get(kid)


def rsa_pkcs1_sha256_verify(n: int, e: int, signing_input: bytes, signature: bytes) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - 3 - len(t)) + b"\x00" + t
    return hmac.compare_digest(em, expected)


class AccessVerifier:
    def __init__(self, fetcher: Callable[[str], dict] | None = None,
                 clock: Callable[[], float] = time.time):
        self._fetcher = fetcher or default_fetcher
        self._clock = clock
        self._caches: dict[str, KeyCache] = {}
        self._lock = threading.Lock()

    def _cache(self, team_domain: str) -> KeyCache:
        with self._lock:
            if team_domain not in self._caches:
                self._caches[team_domain] = KeyCache(team_domain, self._fetcher, self._clock)
            return self._caches[team_domain]

    def verify(self, token: str, config: RemoteConfig) -> RemoteIdentity:
        parts = token.strip().split(".")
        if len(parts) != 3:
            raise AccessDenied("not a three part token")
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
        except ValueError as exc:
            raise AccessDenied("undecodable token") from exc
        signature = _b64url_decode(parts[2])
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AccessDenied("token parts are not objects")
        if header.get("alg") != "RS256":
            raise AccessDenied("algorithm is not RS256")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AccessDenied("no kid")
        key = self._cache(config.team_domain).get(kid)
        if key is None:
            raise AccessDenied("unknown signing key")
        if not rsa_pkcs1_sha256_verify(key[0], key[1], f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
            raise AccessDenied("bad signature")
        self._check_claims(claims, config)
        return self._identity(claims, config)

    def _check_claims(self, claims: dict, config: RemoteConfig) -> None:
        now = self._clock()
        if claims.get("iss") != f"https://{config.team_domain}":
            raise AccessDenied("wrong issuer")
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if not any(isinstance(a, str) and a in config.audience for a in auds):
            raise AccessDenied("wrong audience")
        exp = claims.get("exp")
        if not _is_number(exp) or exp <= now - CLOCK_SKEW_SECONDS:
            raise AccessDenied("expired or no exp")
        for name in ("nbf", "iat"):
            if name in claims and (not _is_number(claims[name]) or claims[name] > now + CLOCK_SKEW_SECONDS):
                raise AccessDenied(f"{name} is in the future")

    @staticmethod
    def _identity(claims: dict, config: RemoteConfig) -> RemoteIdentity:
        email = claims.get("email")
        if email not in (None, ""):
            if not isinstance(email, str):
                raise AccessDenied("bad email claim")
            who = email.strip().lower()
            if who in config.owners:
                return RemoteIdentity("user", who, who, ROLE_OWNER, float(claims["exp"]))
            raise AccessDenied("email is not an owner")
        common = claims.get("common_name")
        if isinstance(common, str) and common:
            entry = config.clients.get(common)
            if entry:
                return RemoteIdentity("service", common, entry[0], entry[1])
            raise AccessDenied("unknown service token")
        raise AccessDenied("no identity claim")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---------------------------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------------------------
def _host_of(raw: bytes | None) -> str:
    host = (raw or b"").decode("latin-1").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    return host.partition(":")[0].rstrip(".")


def _strip_access_cookie(value: bytes) -> bytes:
    parts = [p for p in value.split(b";") if p.strip() and p.strip().split(b"=", 1)[0].strip() not in ACCESS_COOKIES]
    return b";".join(parts).strip()


def _without_credentials(scope: dict) -> dict:
    scope = dict(scope)
    out = []
    for n, v in scope.get("headers", []):
        name = n.lower()
        if name in CREDENTIAL_HEADERS:
            continue
        if name == b"cookie":
            v = _strip_access_cookie(v)
            if not v:
                continue
        out.append((n, v))
    scope["headers"] = out
    return scope


def _log_path(path: str) -> str:
    """A bounded, escaped path for log lines (no forged lines from encoded control characters)."""
    return path[:200].encode("unicode_escape").decode("ascii")


def route_allowed(method: str, path: str) -> bool:
    return any(m == method and pattern.fullmatch(path) for m, pattern in REMOTE_CLIENT_ALLOWLIST)


class RemoteAccessMiddleware:
    def __init__(self, app, settings_or_loader, fetcher: Callable[[str], dict] | None = None,
                 clock: Callable[[], float] = time.time):
        self.app = app
        if isinstance(settings_or_loader, ConfigLoader):
            self.loader = settings_or_loader
        else:
            settings = settings_or_loader
            self.loader = ConfigLoader(lambda: config_path(settings), clock)
        self.verifier = AccessVerifier(fetcher, clock)
        self.clock = clock

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        result = self.loader.get()
        headers: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers", []):
            headers.setdefault(name.lower(), []).append(value)
        host = _host_of((headers.get(b"host") or [b""])[0])
        hostnames = result.config.hostnames if result.config else ()
        remote = host in hostnames or any(h in headers for h in CLOUDFLARE_HEADERS)
        if not remote:
            # local requests keep the bearer-token policy; a stray key header is dropped, not passed on
            await self.app(_without_credentials(scope) if KEY_HEADER in headers else scope, receive, send)
            return
        method = scope.get("method", "GET").upper() if kind == "http" else "GET"
        path = _log_path(scope.get("path", ""))
        if result.state == "broken":
            log.error("remote request refused: configuration is invalid (%s %s)", method, path)
            await self._deny(kind, send, 503, "remote access misconfigured")
            return
        if result.state != "ok" or result.config is None:
            await self._deny(kind, send, 403, "remote access is not enabled")
            return
        config = result.config
        if host not in config.hostnames:
            log.warning("remote request refused: host not allowed (%s %s)", method, path)
            await self._deny(kind, send, 403, "remote access denied")
            return
        tokens = headers.get(JWT_HEADER, [])
        keys = headers.get(KEY_HEADER, [])
        if len(tokens) > 1 or len(keys) > 1 or (tokens and keys) or not (tokens or keys):
            log.warning("remote request refused: missing or repeated credentials (%s %s)", method, path)
            await self._deny(kind, send, 403, "remote access denied")
            return
        try:
            if tokens:
                if config.team_domain is None:
                    raise AccessDenied("Cloudflare Access is not set up")
                identity = await asyncio.to_thread(
                    self.verifier.verify, tokens[0].decode("latin-1"), config)
            else:
                identity = check_shared_key(keys[0], config, self.clock())
        except AccessDenied as exc:
            log.warning("remote request refused: %s (%s %s)", exc, method, path)
            await self._deny(kind, send, 403, "remote access denied")
            return
        except Exception as exc:   # noqa: BLE001  anything unexpected fails closed too
            log.warning("remote request refused: verifier error %s (%s %s)", type(exc).__name__, method, path)
            await self._deny(kind, send, 403, "remote access denied")
            return

        origin = (headers.get(b"origin") or [b""])[0].decode("latin-1")
        expected_origin = "https://" + (headers.get(b"host") or [b""])[0].decode("latin-1").strip().lower()
        if identity.role == ROLE_CLIENT:
            if kind == "websocket":
                log.warning("remote client websocket refused (%s)", identity.display_name)
                await self._deny(kind, send, 403, "not allowed for this client")
                return
            if not route_allowed(method, scope.get("path", "")):
                log.warning("remote client route refused: %s %s %s", identity.display_name, method, path)
                await self._deny(kind, send, 403, "not allowed for this client")
                return
        elif identity.role == ROLE_OWNER:
            if (kind == "websocket" or method not in SAFE_METHODS) and origin != expected_origin:
                log.warning("cross-site request refused: %s %s %s", identity.identity, method, path)
                await self._deny(kind, send, 403, "cross-site request refused")
                return

        log.info("remote %s (%s) %s %s", identity.display_name, identity.role, method, path)
        scope = _without_credentials(scope)   # the app never sees the key, the Access JWT or a token secret
        if kind == "websocket":
            receive, send = self._guard_socket(identity, receive, send)
        token = current_remote_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            current_remote_identity.reset(token)

    def _still_allowed(self, identity: RemoteIdentity) -> bool:
        if identity.expires_at is not None and self.clock() > identity.expires_at + CLOCK_SKEW_SECONDS:
            return False
        result = self.loader.get()
        if result.state != "ok" or result.config is None:
            return False
        if identity.kind == "user":
            return identity.identity in result.config.owners
        return identity.identity in result.config.clients

    def _guard_socket(self, identity: RemoteIdentity, receive, send):
        """0.12.35 review: an owner websocket is re-checked on every message; it is closed (1008) once the
        JWT expired, the owner was removed or remote access was turned off or broken."""
        closed = False

        async def close() -> None:
            nonlocal closed
            if not closed:
                closed = True
                log.warning("remote websocket closed: access no longer valid (%s)", identity.display_name)
                await send({"type": "websocket.close", "code": 1008})

        async def guarded_receive():
            if closed:
                return {"type": "websocket.disconnect", "code": 1008}
            message = await receive()
            if message.get("type") == "websocket.receive" and not self._still_allowed(identity):
                await close()
                return {"type": "websocket.disconnect", "code": 1008}
            return message

        async def guarded_send(message):
            if closed:
                return
            if message.get("type") == "websocket.send" and not self._still_allowed(identity):
                await close()
                return
            await send(message)

        return guarded_receive, guarded_send

    @staticmethod
    async def _deny(kind: str, send, status: int, detail: str) -> None:
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
