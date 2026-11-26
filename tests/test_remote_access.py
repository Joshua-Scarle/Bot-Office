"""Pure tests for botoffice.remote_access: no fastapi, no httpx, no network.

The RSA key below is a throwaway TEST FIXTURE, not a secret. It is generated at import time from a fixed
seed with a small Miller-Rabin search, so no key material is committed to the repository.
"""
import asyncio
import base64
import hashlib
import json
import os
import random

import pytest

from botoffice import remote_access as ra

# ---------------------------------------------------------------- deterministic test keys
_SMALL_PRIMES = [p for p in range(3, 200) if all(p % q for q in range(2, int(p ** 0.5) + 1))]


def _is_prime(n, rng, rounds=24):
    if n < 2 or n % 2 == 0:
        return n == 2
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _make_key(bits, seed):
    rng = random.Random(seed)

    def prime():
        while True:
            c = rng.getrandbits(bits // 2) | (1 << (bits // 2 - 1)) | (1 << (bits // 2 - 2)) | 1
            if _is_prime(c, rng):
                return c
    while True:
        p, q = prime(), prime()
        e = 65537
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


N, E, D = _make_key(2048, 20261125)
SMALL_N, SMALL_E, SMALL_D = _make_key(1024, 7)
OTHER_N, OTHER_E, OTHER_D = _make_key(2048, 99)

TEAM = "yourteam.cloudflareaccess.com"
AUD = "aud-tag-1"
HOST = "office.example.test"
NOW = 1_800_000_000.0


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(kid, n=N, e=E):
    size = lambda x: (x.bit_length() + 7) // 8
    return {"kty": "RSA", "kid": kid, "n": b64(n.to_bytes(size(n), "big")), "e": b64(e.to_bytes(size(e), "big"))}


def sign(claims, kid="k1", alg="RS256", n=N, d=D, header=None):
    head = header if header is not None else {"alg": alg, "kid": kid, "typ": "JWT"}
    signing = f"{b64(json.dumps(head).encode())}.{b64(json.dumps(claims).encode())}"
    if alg == "RS256":
        k = (n.bit_length() + 7) // 8
        t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing.encode()).digest()
        em = b"\x00\x01" + b"\xff" * (k - 3 - len(t)) + b"\x00" + t
        sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    else:
        sig = b""
    return f"{signing}.{b64(sig)}"


def user_claims(email="you@example.test", **over):
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "email": email, "exp": NOW + 600, "iat": NOW - 5, "nbf": NOW - 5}
    claims.update(over)
    return claims


def service_claims(common="abc123.access", **over):
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "common_name": common, "exp": NOW + 600, "iat": NOW}
    claims.update(over)
    return claims


CONFIG = {
    "enabled": True, "team_domain": TEAM, "audience": [AUD], "hostnames": [HOST],
    "owners": ["You@Example.Test"],
    "clients": {"abc123.access": {"name": "claude-cloud", "role": "remote-client"}},
}


# ---------------------------------------------------------------- harness
class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Fetcher:
    def __init__(self, keys=None, fail=False):
        self.keys = keys if keys is not None else [jwk("k1")]
        self.fail = fail
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        assert url == f"https://{TEAM}/cdn-cgi/access/certs"
        if self.fail:
            raise OSError("down")
        return {"keys": self.keys}


class Probe:
    """The downstream ASGI app: records what the middleware let through."""
    def __init__(self):
        self.seen = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            self.seen.append(("lifespan", None))
            return
        self.seen.append((scope["path"], ra.current_remote_identity.get()))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


def write_config(tmp_path, cfg=CONFIG):
    path = tmp_path / "remote_access.json"
    path.write_text(json.dumps(cfg) if not isinstance(cfg, str) else cfg)
    return path


def build(tmp_path, cfg=CONFIG, fetcher=None, clock=None):
    path = write_config(tmp_path, cfg) if cfg is not None else tmp_path / "missing.json"
    clock = clock or Clock()
    probe = Probe()
    loader = ra.ConfigLoader(lambda: path, clock)
    mw = ra.RemoteAccessMiddleware(probe, loader, fetcher=fetcher or Fetcher(), clock=clock)
    return mw, probe, path, clock


def run(mw, method="GET", path="/v1/bots", headers=None, kind="http"):
    scope = {"type": kind, "method": method, "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


def status(sent):
    return sent[0]["status"] if sent and sent[0]["type"] == "http.response.start" else None


def detail(sent):
    return json.loads(sent[1]["body"])["detail"]


def remote(token, **extra):
    headers = {"host": HOST, "cf-access-jwt-assertion": token}
    headers.update(extra)
    return headers


# ---------------------------------------------------------------- tests
def test_owner_get_passes_and_sets_identity(tmp_path):
    mw, probe, *_ = build(tmp_path)
    sent = run(mw, headers=remote(sign(user_claims())))
    assert status(sent) == 200
    path, who = probe.seen[0]
    assert who == ra.RemoteIdentity("user", "you@example.test", "you@example.test", "owner")
    assert ra.current_remote_identity.get() is None   # reset afterwards


def test_local_request_passes_untouched(tmp_path):
    mw, probe, *_ = build(tmp_path)
    sent = run(mw, headers={"host": "127.0.0.1:8787", "authorization": "Bearer x"})
    assert status(sent) == 200 and probe.seen == [("/v1/bots", None)]


def test_local_request_with_no_config_file(tmp_path):
    mw, probe, *_ = build(tmp_path, cfg=None)
    assert status(run(mw, headers={"host": "127.0.0.1:8787"})) == 200


def test_lifespan_passes_through(tmp_path):
    mw, probe, *_ = build(tmp_path)
    run(mw, kind="lifespan", path="")
    assert probe.seen == [("lifespan", None)]


def test_remote_without_jwt_is_403(tmp_path):
    mw, probe, *_ = build(tmp_path)
    sent = run(mw, headers={"host": HOST})
    assert status(sent) == 403 and detail(sent) == "remote access denied" and not probe.seen


def test_forged_cf_ray_from_loopback_without_jwt_is_403(tmp_path):
    mw, probe, *_ = build(tmp_path)
    for header in ("cf-ray", "cf-connecting-ip", "cf-visitor", "cf-access-client-id", "cf-access-client-secret"):
        sent = run(mw, headers={"host": "127.0.0.1:8787", header: "1"})
        assert status(sent) == 403, header
    assert not probe.seen


def test_remote_with_missing_config_is_403(tmp_path):
    mw, probe, *_ = build(tmp_path, cfg=None)
    sent = run(mw, headers={"host": "127.0.0.1:8787", "cf-ray": "abc"})
    assert status(sent) == 403 and detail(sent) == "remote access is not enabled"


def test_remote_with_disabled_config_is_403(tmp_path):
    mw, probe, *_ = build(tmp_path, cfg={"enabled": False})
    sent = run(mw, headers={"host": HOST, "cf-ray": "abc"})
    assert status(sent) == 403 and not probe.seen


@pytest.mark.parametrize("cfg", [
    "{not json", "[]", {"enabled": True}, {**CONFIG, "team_domain": "evil.example.test"},
    {**CONFIG, "audience": []}, {**CONFIG, "audience": [""]}, {**CONFIG, "hostnames": ["Office.Example.Test"]},
    {**CONFIG, "hostnames": ["office.example.test:8787"]}, {**CONFIG, "hostnames": ["https://office.example.test"]},
    {**CONFIG, "owners": ["not-an-email"]}, {**CONFIG, "clients": {"a": {"name": "x", "role": "admin"}}},
    {**CONFIG, "enabled": "yes"},
])
def test_broken_config_is_503(tmp_path, cfg, caplog):
    mw, probe, *_ = build(tmp_path, cfg=cfg)
    sent = run(mw, headers=remote(sign(user_claims()), **{"cf-ray": "1"}))
    assert status(sent) == 503 and detail(sent) == "remote access misconfigured" and not probe.seen
    # a local request still works
    assert status(run(mw, headers={"host": "127.0.0.1:8787"})) == 200


def test_broken_config_logged_once(tmp_path, caplog):
    mw, probe, path, clock = build(tmp_path, cfg="{bad")
    with caplog.at_level("ERROR", logger="botoffice.remote"):
        for _ in range(3):
            clock.t += 10
            run(mw, headers={"host": HOST, "cf-ray": "1"})
    assert len([r for r in caplog.records if "config invalid" in r.getMessage()]) == 1


def test_bad_signature(tmp_path):
    mw, probe, *_ = build(tmp_path)
    token = sign(user_claims(), n=OTHER_N, d=OTHER_D)   # signed by a different key under the same kid
    assert status(run(mw, headers=remote(token))) == 403 and not probe.seen


def test_tampered_payload(tmp_path):
    mw, probe, *_ = build(tmp_path)
    head, _, sig = sign(user_claims()).split(".")
    forged = b64(json.dumps(user_claims("evil@example.test")).encode())
    assert status(run(mw, headers=remote(f"{head}.{forged}.{sig}"))) == 403


@pytest.mark.parametrize("claims", [
    user_claims(aud=["other"]), user_claims(aud="other"), user_claims(iss="https://evil.cloudflareaccess.com"),
    user_claims(exp=NOW - 61), user_claims(exp="soon"), user_claims(nbf=NOW + 120), user_claims(iat=NOW + 120),
    {k: v for k, v in user_claims().items() if k != "exp"},
])
def test_bad_claims(tmp_path, claims):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(claims)))) == 403 and not probe.seen


def test_claims_within_skew_pass(tmp_path):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims(exp=NOW - 30, nbf=NOW + 30))))) == 200


def test_aud_string_and_list_match(tmp_path):
    mw, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims(aud=AUD))))) == 200
    assert status(run(mw, headers=remote(sign(user_claims(aud=["x", AUD]))))) == 200


def test_alg_none_and_hs256_rejected(tmp_path):
    mw, probe, *_ = build(tmp_path)
    for alg in ("none", "HS256", "rs256"):
        assert status(run(mw, headers=remote(sign(user_claims(), alg=alg)))) == 403, alg
    assert status(run(mw, headers=remote(sign(user_claims(), header={"alg": "RS256"})))) == 403   # no kid
    assert not probe.seen


def test_garbage_tokens(tmp_path):
    mw, *_ = build(tmp_path)
    for token in ("", "a.b", "a.b.c.d", "!!!.???.***", "e30.e30."):
        assert status(run(mw, headers=remote(token))) == 403, token


def test_duplicate_jwt_headers_rejected(tmp_path):
    mw, probe, *_ = build(tmp_path)
    token = sign(user_claims()).encode()
    scope = {"type": "http", "method": "GET", "path": "/v1/bots", "headers": [
        (b"host", HOST.encode()), (b"cf-access-jwt-assertion", token), (b"cf-access-jwt-assertion", token)]}
    sent = []

    async def send(m):
        sent.append(m)

    asyncio.run(mw(scope, None, send))
    assert status(sent) == 403 and not probe.seen


def test_unknown_kid_refetches_once_then_succeeds(tmp_path):
    fetcher = Fetcher(keys=[jwk("old")])
    mw, probe, path, clock = build(tmp_path, fetcher=fetcher)
    assert status(run(mw, headers=remote(sign(user_claims(), kid="old")))) == 200
    assert fetcher.calls == 1
    fetcher.keys = [jwk("old"), jwk("new")]
    clock.t += 61
    assert status(run(mw, headers=remote(sign(user_claims(exp=clock.t + 600), kid="new")))) == 200
    assert fetcher.calls == 2


def test_refetch_is_rate_limited(tmp_path):
    fetcher = Fetcher(keys=[jwk("k1")])
    mw, probe, path, clock = build(tmp_path, fetcher=fetcher)
    run(mw, headers=remote(sign(user_claims())))
    assert fetcher.calls == 1
    for _ in range(5):
        assert status(run(mw, headers=remote(sign(user_claims(), kid="ghost")))) == 403
    assert fetcher.calls == 1                      # within 60 s: no refetch
    clock.t += 61
    run(mw, headers=remote(sign(user_claims(exp=clock.t + 600), kid="ghost")))
    assert fetcher.calls == 2


def test_keys_cached_for_an_hour_and_stale_keys_survive_fetch_failure(tmp_path):
    fetcher = Fetcher()
    mw, probe, path, clock = build(tmp_path, fetcher=fetcher)
    run(mw, headers=remote(sign(user_claims())))
    clock.t += 1000
    run(mw, headers=remote(sign(user_claims(exp=clock.t + 600))))
    assert fetcher.calls == 1
    clock.t += 3000                                # past the hour: refetch, and it fails
    fetcher.fail = True
    sent = run(mw, headers=remote(sign(user_claims(exp=clock.t + 600))))
    assert fetcher.calls == 2 and status(sent) == 200   # cached keys still verify


def test_fetch_failure_without_cache_rejects(tmp_path):
    mw, probe, *_ = build(tmp_path, fetcher=Fetcher(fail=True))
    assert status(run(mw, headers=remote(sign(user_claims())))) == 403 and not probe.seen


def test_key_under_2048_bits_rejected(tmp_path):
    fetcher = Fetcher(keys=[jwk("k1", SMALL_N, SMALL_E)])
    mw, probe, *_ = build(tmp_path, fetcher=fetcher)
    token = sign(user_claims(), n=SMALL_N, d=SMALL_D)
    assert status(run(mw, headers=remote(token))) == 403 and not probe.seen


def test_unknown_email_403(tmp_path):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims("stranger@example.test"))))) == 403
    assert not probe.seen


def test_email_match_is_case_insensitive(tmp_path):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims("YOU@example.TEST"))))) == 200


def test_host_not_in_hostnames_403(tmp_path):
    mw, probe, *_ = build(tmp_path)
    sent = run(mw, headers=remote(sign(user_claims()), host="evil.example.test"))
    assert status(sent) == 403 and not probe.seen


def test_host_with_port_still_matches(tmp_path):
    mw, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims()), host=HOST.upper() + ":443"))) == 200


def test_owner_post_requires_exact_origin(tmp_path):
    mw, probe, *_ = build(tmp_path)
    token = sign(user_claims())
    no_origin = run(mw, "POST", "/v1/bots/abc/messages", remote(token))
    assert status(no_origin) == 403 and detail(no_origin) == "cross-site request refused"
    wrong = run(mw, "POST", "/v1/bots/abc/messages", remote(token, origin="https://evil.example.test"))
    assert status(wrong) == 403
    http_origin = run(mw, "POST", "/v1/bots/abc/messages", remote(token, origin=f"http://{HOST}"))
    assert status(http_origin) == 403
    assert not probe.seen
    ok = run(mw, "POST", "/v1/bots/abc/messages", remote(token, origin=f"https://{HOST}"))
    assert status(ok) == 200


class WsApp:
    """A downstream websocket app: accepts, then echoes frames until the client goes away."""
    def __init__(self):
        self.seen = []
        self.events = []
        self.cancelled = False

    async def __call__(self, scope, receive, send):
        self.seen.append((scope["path"], ra.current_remote_identity.get(),
                          [n for n, _ in scope["headers"]]))
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept", "subprotocol": "binary"})
        try:
            while True:
                message = await receive()
                self.events.append(message["type"])
                if message["type"] == "websocket.disconnect":
                    return
                await send({"type": "websocket.send", "bytes": message.get("bytes", b"")})
        except asyncio.CancelledError:
            self.cancelled = True
            raise


VNC_PATH = "/v1/desktops/desktop-12/vnc"


def build_ws(tmp_path, cfg=CONFIG, clock=None):
    path = write_config(tmp_path, cfg)
    clock = clock or Clock()
    app = WsApp()
    mw = ra.RemoteAccessMiddleware(app, ra.ConfigLoader(lambda: path, clock), fetcher=Fetcher(), clock=clock)
    mw.ws_recheck = 0.01
    return mw, app, path, clock


def run_ws(mw, headers, frames=(), path=VNC_PATH, on_frame=None, hold=0.0):
    """Drive one websocket: connect, then *frames* (on_frame(i) runs before frame i), then wait *hold*
    seconds for the middleware to act, then disconnect. Returns what was sent to the client."""
    scope = {"type": "websocket", "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    sent = []
    queue = [{"type": "websocket.connect"}] + [{"type": "websocket.receive", "bytes": f} for f in frames]

    async def receive():
        if queue:
            message = queue.pop(0)
            if message["type"] == "websocket.receive" and on_frame:
                on_frame(len(frames) - len(queue) - 1)
            return message
        await asyncio.sleep(hold)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    asyncio.run(asyncio.wait_for(mw(scope, receive, send), 10))
    return sent


def owner_ws(token, **extra):
    return remote(token, origin=f"https://{HOST}", **extra)


def test_owner_desktop_websocket_allowed_over_remote(tmp_path):
    mw, app, *_ = build_ws(tmp_path)
    sent = run_ws(mw, owner_ws(sign(user_claims())), frames=[b"hi"])
    assert sent[0]["type"] == "websocket.accept"
    assert {"type": "websocket.send", "bytes": b"hi"} in sent
    path, who, header_names = app.seen[0]
    assert path == VNC_PATH and who.role == "owner" and who.kind == "user" and who.host == HOST
    assert b"cf-access-jwt-assertion" not in header_names   # the JWT never reaches the app
    assert ra.current_remote_identity.get() is None


def test_owner_websocket_needs_exact_origin(tmp_path):
    mw, app, *_ = build_ws(tmp_path)
    token = sign(user_claims())
    for headers in (remote(token), remote(token, origin="https://evil.example.test"),
                    remote(token, origin=f"http://{HOST}")):
        assert run_ws(mw, headers) == [{"type": "websocket.close", "code": 1008}]
    assert not app.seen


@pytest.mark.parametrize("path", ["/v1/ws", "/v1/desktops/desktop-1/vnc/x", "/v1/desktops/desktop-0/vnc",
                                  "/v1/desktops/desktop-100/vnc", "/v1/desktops/evil/vnc", "/v1/jobs/1/events"])
def test_other_remote_websockets_refused_for_owner(tmp_path, path):
    mw, app, *_ = build_ws(tmp_path)
    assert run_ws(mw, owner_ws(sign(user_claims())), path=path) == [{"type": "websocket.close", "code": 1008}]
    assert not app.seen


def test_remote_client_and_shared_key_websockets_still_refused(tmp_path):
    mw, app, *_ = build_ws(tmp_path)
    sent = run_ws(mw, owner_ws(sign(service_claims())))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    cfg = dict(CONFIG, shared_keys=KEY_CONFIG["shared_keys"])
    mw, app2, *_ = build_ws(tmp_path, cfg)
    sent = run_ws(mw, {"host": HOST, "x-bot-office-key": KEY, "origin": f"https://{HOST}"})
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert not app.seen and not app2.seen


def test_expired_or_unsigned_jwt_websocket_refused(tmp_path):
    mw, app, *_ = build_ws(tmp_path)
    expired = sign(user_claims(exp=NOW - 3600))
    assert run_ws(mw, owner_ws(expired)) == [{"type": "websocket.close", "code": 1008}]
    assert run_ws(mw, owner_ws(sign(user_claims(), d=D + 2))) == [{"type": "websocket.close", "code": 1008}]
    assert run_ws(mw, owner_ws(sign(user_claims(email="other@example.test")))) == [
        {"type": "websocket.close", "code": 1008}]
    assert not app.seen


def test_owner_websocket_closed_when_jwt_expires(tmp_path):
    clock = Clock()
    mw, app, *_ = build_ws(tmp_path, clock=clock)
    token = sign(user_claims(exp=NOW + 100))

    def tick(i):
        if i == 1:
            clock.t = NOW + 101   # the JWT expired between two frames
    sent = run_ws(mw, owner_ws(token), frames=[b"a", b"b", b"c"], on_frame=tick)
    assert sent[0]["type"] == "websocket.accept"
    assert {"type": "websocket.send", "bytes": b"a"} in sent
    assert {"type": "websocket.send", "bytes": b"b"} not in sent and {"type": "websocket.send", "bytes": b"c"} not in sent
    assert sent[-1] == {"type": "websocket.close", "code": 1008}


def test_idle_owner_websocket_closed_at_expiry_by_the_watchdog(tmp_path):
    clock = Clock()
    mw, app, *_ = build_ws(tmp_path, clock=clock)
    token = sign(user_claims(exp=NOW + 100))

    def tick(i):
        clock.t = NOW + 200    # time passes while no frame flows; the periodic re-check must cut it

    async def drive():
        scope = {"type": "websocket", "path": VNC_PATH,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in owner_ws(token).items()]}
        out = []
        first = [True]

        async def receive():
            if first[0]:
                first[0] = False
                return {"type": "websocket.connect"}
            tick(0)
            await asyncio.sleep(30)    # a silent viewer: only the watchdog can end this
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            out.append(message)
        await asyncio.wait_for(mw(scope, receive, send), 5)
        return out
    sent = asyncio.run(drive())
    assert sent[0]["type"] == "websocket.accept" and sent[-1] == {"type": "websocket.close", "code": 1008}
    assert app.cancelled


def test_owner_websocket_hard_maximum_age(tmp_path):
    clock = Clock()
    mw, app, *_ = build_ws(tmp_path, clock=clock)
    token = sign(user_claims(exp=NOW + 10 * 3600))

    def tick(i):
        if i == 1:
            clock.t = NOW + ra.MAX_REMOTE_WEBSOCKET_SECONDS + 1
    sent = run_ws(mw, owner_ws(token), frames=[b"a", b"b"], on_frame=tick)
    assert {"type": "websocket.send", "bytes": b"b"} not in sent and sent[-1] == {"type": "websocket.close", "code": 1008}


def test_owner_websocket_closed_when_owner_removed(tmp_path):
    clock = Clock()
    mw, app, path, _ = build_ws(tmp_path, clock=clock)
    token = sign(user_claims())

    def revoke(i):
        if i == 1:
            write_config(tmp_path, dict(CONFIG, owners=["someone@example.test"]))
            clock.t += ra.CONFIG_RECHECK_SECONDS + 1
    sent = run_ws(mw, owner_ws(token), frames=[b"a", b"b"], on_frame=revoke)
    assert {"type": "websocket.send", "bytes": b"a"} in sent
    assert {"type": "websocket.send", "bytes": b"b"} not in sent and sent[-1] == {"type": "websocket.close", "code": 1008}


def test_local_websocket_untouched(tmp_path):
    mw, app, *_ = build_ws(tmp_path)
    sent = run_ws(mw, {"host": "127.0.0.1:8787"}, frames=[b"x"])
    assert sent[0]["type"] == "websocket.accept" and app.seen[0][1] is None


def test_remote_client_allowlist(tmp_path):
    mw, probe, *_ = build(tmp_path)
    token = sign(service_claims())
    assert status(run(mw, "GET", "/v1/bots", remote(token))) == 200
    assert probe.seen[-1][1].role == "remote-client" and probe.seen[-1][1].display_name == "claude-cloud"
    uid = "0123abcd-0123-4abc-8def-0123456789ab"
    for method, path in [("GET", f"/v1/bots/{uid}/messages"), ("POST", f"/v1/bots/{uid}/messages"),
                         ("POST", f"/v1/bots/{uid}/questions/{uid}/answer"), ("GET", f"/v1/jobs/{uid}/events"),
                         ("POST", f"/v1/jobs/{uid}/cancel"), ("GET", "/v1/needs-you"),
                         ("POST", "/v1/needs-you/dismiss"), ("POST", f"/v1/assignments/{uid}/reply"),
                         ("GET", "/v1/remote/whoami"), ("GET", "/healthz")]:
        assert status(run(mw, method, path, remote(token))) == 200, (method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/slots/desktop-1/exec"), ("GET", "/v1/settings"), ("PUT", "/v1/settings"),
    ("POST", "/v1/bots"), ("PATCH", "/v1/bots/abc"), ("DELETE", "/v1/bots/abc"),
    ("POST", "/v1/bots/abc/retire"), ("POST", "/v1/bots/abc/session/reset"), ("POST", "/v1/bots/abc/slash"),
    ("PUT", "/v1/bots/abc/memory"), ("PUT", "/v1/shared-memory"), ("GET", "/v1/agent/tasks"),
    ("POST", "/v1/bridge/inbox"), ("POST", "/v1/bots/abc/attachments"), ("POST", "/v1/bots/abc/watchers"),
    ("GET", "/v1/bots/abc/messages/"), ("GET", "/v1/bots/abc/messages\n"), ("GET", "/v1/bots/abc/../settings"),
    ("GET", "//v1/bots"), ("GET", "/v1/bots/abc/extra"), ("HEAD", "/v1/bots"), ("POST", "/v1/jobs"),
    ("GET", "/"), ("POST", "/v1/bots/abc/messages/1/edit"),
])
def test_remote_client_denied(tmp_path, method, path):
    mw, probe, *_ = build(tmp_path)
    sent = run(mw, method, path, remote(sign(service_claims())))
    assert status(sent) == 403 and detail(sent) == "not allowed for this client" and not probe.seen


def test_remote_client_websocket_closed(tmp_path):
    mw, probe, *_ = build(tmp_path)
    sent = run(mw, path="/v1/desktops/desktop-1/vnc", headers=remote(sign(service_claims())), kind="websocket")
    assert sent == [{"type": "websocket.close", "code": 1008}] and not probe.seen


def test_unknown_service_token_403(tmp_path):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(service_claims("nope.access"))))) == 403


def test_service_token_can_never_be_owner(tmp_path):
    # 0.12.35 review: an owner-role client entry makes the whole config broken (fail closed)
    cfg = {**CONFIG, "clients": {"own456.access": {"name": "owner-token", "role": "owner"}}}
    mw, probe, *_ = build(tmp_path, cfg)
    sent = run(mw, "POST", "/v1/settings", remote(sign(service_claims("own456.access"))))
    assert status(sent) == 503 and not probe.seen


@pytest.mark.parametrize("role", [[], {}, ["remote-client"], None, 1])
def test_unhashable_client_role_reload_fails_closed(tmp_path, role):
    mw, probe, path, clock = build(tmp_path)
    token = sign(service_claims())
    assert status(run(mw, headers=remote(token))) == 200
    path.write_text(json.dumps({**CONFIG, "clients": {"abc123.access": {"name": "x", "role": role}}}))
    os.utime(path, ns=(1, 1_700_000_000_000_000_000))
    clock.t += 6
    assert status(run(mw, headers=remote(token))) == 503
    clock.t += 6   # the next check still refuses: the old config is never reused
    assert status(run(mw, headers=remote(token))) == 503


def test_credentials_never_reach_the_app(tmp_path):
    seen = []
    async def app(scope, receive, send):
        seen.append({n for n, _ in scope["headers"]})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    loader = ra.ConfigLoader(lambda: write_config(tmp_path), Clock())
    mw = ra.RemoteAccessMiddleware(app, loader, fetcher=Fetcher(), clock=Clock())
    assert status(run(mw, headers=remote(sign(user_claims()), **{"cf-access-client-secret": "s"}))) == 200
    assert status(run(mw, headers={"host": "127.0.0.1:8787", "x-bot-office-key": "anything"})) == 200
    for names in seen:
        assert not names & {b"cf-access-jwt-assertion", b"x-bot-office-key", b"cf-access-client-secret"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_exp_refused(tmp_path, bad):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims(exp=bad))))) == 403


def test_logged_path_is_escaped():
    assert ra._log_path("/v1/x\nFAKE line") == "/v1/x\\nFAKE line"
    assert len(ra._log_path("/" + "a" * 500)) == 200


def test_config_reload_revokes_a_client(tmp_path):
    mw, probe, path, clock = build(tmp_path)
    token = sign(service_claims())
    assert status(run(mw, headers=remote(token))) == 200
    revoked = {**CONFIG, "clients": {}}
    path.write_text(json.dumps(revoked))
    os.utime(path, ns=(1, 1_700_000_000_000_000_000))   # force a new mtime
    assert status(run(mw, headers=remote(token))) == 200   # inside 5 s: not rechecked yet
    clock.t += 6
    assert status(run(mw, headers=remote(token))) == 403


def test_config_reload_to_broken_then_fixed(tmp_path):
    mw, probe, path, clock = build(tmp_path)
    token = sign(user_claims(exp=NOW + 6000))
    assert status(run(mw, headers=remote(token))) == 200
    path.write_text("{oops")
    os.utime(path, ns=(1, 1_700_000_000_000_000_000))
    clock.t += 6
    assert status(run(mw, headers=remote(token))) == 503
    write_config(tmp_path)
    os.utime(path, ns=(1, 1_700_000_100_000_000_000))
    clock.t += 6
    assert status(run(mw, headers=remote(token))) == 200


def test_config_path_uses_env_then_control_dir(tmp_path, monkeypatch):
    class S:
        control_dir = tmp_path

    monkeypatch.delenv(ra.CONFIG_ENV, raising=False)
    assert ra.config_path(S()) == tmp_path / "remote_access.json"
    monkeypatch.setenv(ra.CONFIG_ENV, str(tmp_path / "x.json"))
    assert ra.config_path(S()) == tmp_path / "x.json"


def test_middleware_accepts_settings_object(tmp_path, monkeypatch):
    class S:
        control_dir = tmp_path

    monkeypatch.delenv(ra.CONFIG_ENV, raising=False)
    write_config(tmp_path)
    probe = Probe()
    mw = ra.RemoteAccessMiddleware(probe, S(), fetcher=Fetcher(), clock=Clock())
    assert status(run(mw, headers=remote(sign(user_claims())))) == 200


def test_info_log_has_no_query_or_token(tmp_path, caplog):
    mw, *_ = build(tmp_path)
    token = sign(user_claims())
    with caplog.at_level("INFO", logger="botoffice.remote"):
        run(mw, headers=remote(token))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "you@example.test" in text and "owner" in text and "GET /v1/bots" in text and token not in text


# ---------------------------------------------------------------- shared keys (stopgap without Access)
# A made-up test key, not a secret: 43+ base64url characters, like `secrets.token_urlsafe(32)`.
KEY = "test-key-not-a-secret-" + "x" * 30
KEY_DIGEST = hashlib.sha256(KEY.encode()).hexdigest()
KEY_CONFIG = {
    "enabled": True, "hostnames": [HOST],
    "shared_keys": {KEY_DIGEST: {"name": "claude-cloud", "expires_at": "2099-01-01T00:00:00Z"}},
}


def keyed(key=KEY, **extra):
    headers = {"host": HOST, "cf-ray": "abc-IAD", "x-bot-office-key": key}
    headers.update(extra)
    return headers


def test_shared_key_allows_remote_client_routes(tmp_path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, headers=keyed())
    assert status(sent) == 200
    assert probe.seen[0][1] == ra.RemoteIdentity("service", "claude-cloud", "claude-cloud", "remote-client")
    assert status(run(mw, "POST", "/v1/bots/abc/messages", headers=keyed())) == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/v1/settings"), ("POST", "/v1/bots"), ("DELETE", "/v1/bots/abc"),
    ("POST", "/v1/slots/1/exec"), ("GET", "/v1/agent/whoami"),
])
def test_shared_key_is_least_privilege(tmp_path, method, path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, method, path, headers=keyed())
    assert status(sent) == 403 and detail(sent) == "not allowed for this client"
    assert probe.seen == []


def test_shared_key_websocket_closed(tmp_path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, path="/v1/ws", headers=keyed(), kind="websocket")
    assert sent == [{"type": "websocket.close", "code": 1008}] and probe.seen == []


@pytest.mark.parametrize("key", [
    "", "short", KEY + "y", KEY[:-1] + "y", "z" * 300, KEY.upper(),
])
def test_wrong_shared_key_403(tmp_path, key):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, headers=keyed(key))
    assert status(sent) == 403 and detail(sent) == "remote access denied"
    assert probe.seen == []


def test_no_credentials_through_tunnel_403(tmp_path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, headers={"host": HOST, "cf-ray": "abc-IAD"})
    assert status(sent) == 403 and probe.seen == []


def test_repeated_or_mixed_credentials_403(tmp_path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    scope_headers = [(b"host", HOST.encode()), (b"x-bot-office-key", KEY.encode()),
                     (b"x-bot-office-key", KEY.encode())]
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)
    asyncio.run(mw({"type": "http", "method": "GET", "path": "/v1/bots", "headers": scope_headers},
                   receive, send))
    assert status(sent) == 403
    both = keyed(**{"cf-access-jwt-assertion": sign(service_claims())})
    assert status(run(mw, headers=both)) == 403
    assert probe.seen == []


def test_jwt_refused_when_access_not_set_up(tmp_path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, headers=remote(sign(user_claims())))
    assert status(sent) == 403 and probe.seen == []


def test_shared_key_expires(tmp_path):
    cfg = json.loads(json.dumps(KEY_CONFIG))
    cfg["shared_keys"][KEY_DIGEST]["expires_at"] = "2026-11-30T00:00:00Z"
    clock = Clock(1795996799.0)   # 2026-11-29T23:59:59Z
    mw, probe, *_ = build(tmp_path, cfg, clock=clock)
    assert status(run(mw, headers=keyed())) == 200
    clock.t = 1795996800.0        # 2026-11-30T00:00:00Z
    assert status(run(mw, headers=keyed())) == 403


def test_shared_key_header_is_stripped_before_the_app(tmp_path):
    seen_headers = []

    async def app(scope, receive, send):
        seen_headers.extend(name for name, _ in scope["headers"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    path = write_config(tmp_path, KEY_CONFIG)
    clock = Clock()
    mw = ra.RemoteAccessMiddleware(app, ra.ConfigLoader(lambda: path, clock), fetcher=Fetcher(), clock=clock)
    assert status(run(mw, headers=keyed())) == 200
    assert b"x-bot-office-key" not in seen_headers and b"host" in seen_headers


def test_shared_key_revoked_by_removing_it(tmp_path):
    mw, probe, path, clock = build(tmp_path, KEY_CONFIG)
    assert status(run(mw, headers=keyed())) == 200
    path.write_text(json.dumps({"enabled": False}))
    clock.t += 10
    assert status(run(mw, headers=keyed())) == 403


def test_shared_keys_and_access_together(tmp_path):
    cfg = dict(CONFIG)
    cfg["shared_keys"] = KEY_CONFIG["shared_keys"]
    mw, probe, *_ = build(tmp_path, cfg)
    assert status(run(mw, headers=keyed())) == 200
    assert status(run(mw, headers=remote(sign(user_claims())))) == 200


def test_local_request_ignores_key_header(tmp_path):
    mw, probe, *_ = build(tmp_path, KEY_CONFIG)
    sent = run(mw, headers={"host": "127.0.0.1:8787", "x-bot-office-key": "anything"})
    assert status(sent) == 200 and probe.seen[0][1] is None


@pytest.mark.parametrize("bad", [
    {"enabled": True, "hostnames": [HOST]},                                         # nothing to check
    {"enabled": True, "hostnames": [HOST], "shared_keys": {}},
    {"enabled": True, "hostnames": [HOST], "shared_keys": {"ABC": {"name": "x", "expires_at": "2099-01-01T00:00:00Z"}}},
    {"enabled": True, "hostnames": [HOST], "shared_keys": {KEY_DIGEST: {"name": "x"}}},
    {"enabled": True, "hostnames": [HOST], "shared_keys": {KEY_DIGEST: {"name": "x", "expires_at": "2099-01-01T00:00:00"}}},
    {"enabled": True, "hostnames": [HOST], "shared_keys": {KEY_DIGEST: {"name": "x", "expires_at": "soon"}}},
    {"enabled": True, "hostnames": [HOST], "shared_keys": {KEY_DIGEST: {"expires_at": "2099-01-01T00:00:00Z"}}},
    {"enabled": True, "hostnames": [HOST], "shared_keys": {KEY_DIGEST: {"name": "x", "role": "owner",
                                                                        "expires_at": "2099-01-01T00:00:00Z"}}},
    {"enabled": True, "hostnames": [HOST], "team_domain": TEAM,
     "shared_keys": {KEY_DIGEST: {"name": "x", "expires_at": "2099-01-01T00:00:00Z"}}},  # half of Access
])
def test_bad_shared_key_config_is_broken(tmp_path, bad):
    mw, probe, *_ = build(tmp_path, bad)
    assert status(run(mw, headers=keyed())) == 503


# ---------------------------------------------------------------- 0.12.35 review round 2
def test_access_cookie_is_stripped_others_kept():
    scope = {"headers": [(b"cookie", b"a=1; CF_Authorization=secret; b=2"), (b"host", b"h")]}
    out = dict(ra._without_credentials(scope)["headers"])
    assert out[b"cookie"] == b"a=1; b=2" and out[b"host"] == b"h"
    only = {"headers": [(b"cookie", b"CF_Authorization=secret")]}
    assert ra._without_credentials(only)["headers"] == []


def test_jwks_fetch_runs_outside_the_lock(tmp_path):
    import threading as _t
    seen = []
    cache = None
    def fetcher(url):
        seen.append(cache._lock.acquire(blocking=False))   # the lock must be free during the fetch
        if seen[-1]:
            cache._lock.release()
        return {"keys": [jwk("k1")]}
    cache = ra.KeyCache(TEAM, fetcher, Clock())
    assert cache.get("k1") is not None and seen == [True]


def test_jwks_cache_miss_waits_for_the_refresh_in_flight():
    import threading as _t
    started, release = _t.Event(), _t.Event()
    calls = []
    def fetcher(url):
        calls.append(1)
        started.set()
        release.wait(5)
        return {"keys": [jwk("k1")]}
    cache = ra.KeyCache(TEAM, fetcher, Clock())
    out = {}
    leader = _t.Thread(target=lambda: out.setdefault("a", cache.get("k1")))
    leader.start(); started.wait(5)
    follower = _t.Thread(target=lambda: out.setdefault("b", cache.get("k1")))
    follower.start()
    release.set(); leader.join(5); follower.join(5)
    assert out["a"] is not None and out["b"] is not None and len(calls) == 1


# ---------------------------------------------------------------- 0.12.35 review round 5
def test_cached_keys_expire_when_refresh_keeps_failing():
    clock = Clock()
    fetcher = Fetcher()
    cache = ra.KeyCache(TEAM, fetcher, clock)
    assert cache.get("k1") is not None
    fetcher.fail = True
    clock.t += ra.KEY_CACHE_SECONDS + 1
    assert cache.get("k1") is not None            # stale but inside the maximum: still usable
    clock.t += ra.KEY_MAX_STALE_SECONDS
    assert cache.get("k1") is None                 # past the maximum stale life: fail closed


def _stream(mw, headers, chunks_then):
    """The app streams 3 chunks; between chunks ``chunks_then`` may change the world."""
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(3):
            await send({"type": "http.response.body", "body": f"c{i}".encode(), "more_body": True})
            chunks_then(i)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw.app = app
    scope = {"type": "http", "method": "GET", "path": "/v1/jobs/abc/events",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return [m.get("body") for m in sent if m["type"] == "http.response.body"]


def test_stream_ends_when_jwt_expires(tmp_path):
    mw, probe, path, clock = build(tmp_path)
    def tick(i):
        if i == 0:
            clock.t = NOW + 600 + ra.CLOCK_SKEW_SECONDS + 1
    bodies = _stream(mw, remote(sign(service_claims(exp=NOW + 600))), tick)
    assert bodies == [b"c0", b""]   # the final frame is checked too


def test_stream_ends_when_client_revoked(tmp_path):
    mw, probe, path, clock = build(tmp_path)
    def tick(i):
        if i == 1:
            path.write_text(json.dumps({**CONFIG, "clients": {}}))
            os.utime(path, ns=(1, 1_700_000_000_000_000_000))
            clock.t += 6
    bodies = _stream(mw, remote(sign(service_claims(exp=NOW + 6000))), tick)
    assert bodies == [b"c0", b"c1", b""]


def test_stream_unbounded_while_still_valid(tmp_path):
    mw, probe, path, clock = build(tmp_path)
    bodies = _stream(mw, remote(sign(service_claims(exp=NOW + 6000))), lambda i: None)
    assert bodies == [b"c0", b"c1", b"c2", b""]


def test_too_many_pending_jwt_checks_get_503(tmp_path):
    mw, probe, *_ = build(tmp_path)
    mw._pending_verify = ra.MAX_PENDING_VERIFY
    sent = run(mw, headers=remote(sign(user_claims())))
    assert status(sent) == 503 and detail(sent) == "remote access busy"


# ---------------------------------------------------------------- 0.12.35 review round 6
def test_stream_ends_when_shared_key_with_same_name_replaced(tmp_path):
    other = "b" * 64
    cfg = {**KEY_CONFIG, "shared_keys": {**KEY_CONFIG["shared_keys"]}}
    mw, probe, path, clock = build(tmp_path, cfg)
    def tick(i):
        if i == 0:   # the key is replaced by another key with the SAME name
            path.write_text(json.dumps({**KEY_CONFIG, "shared_keys": {other: {"name": "claude-cloud",
                                                                             "expires_at": "2099-01-01T00:00:00Z"}}}))
            os.utime(path, ns=(1, 1_700_000_000_000_000_000))
            clock.t += 6
    assert _stream(mw, keyed(), tick) == [b"c0", b""]


def test_stream_ends_when_audience_or_host_changes(tmp_path):
    for change in ({"audience": ["other-aud"]}, {"hostnames": ["other.example.test"]}):
        mw, probe, path, clock = build(tmp_path)
        def tick(i, change=change):
            if i == 0:
                path.write_text(json.dumps({**CONFIG, **change}))
                os.utime(path, ns=(1, 1_700_000_000_000_000_000 + len(str(change))))
                clock.t += 6
        assert _stream(mw, remote(sign(service_claims(exp=NOW + 6000))), tick) == [b"c0", b""]


def test_remote_request_has_a_hard_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "MAX_REMOTE_REQUEST_SECONDS", 0.05)
    mw, probe, *_ = build(tmp_path)
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"c0", "more_body": True})
        await asyncio.sleep(5)

    mw.app = app
    scope = {"type": "http", "method": "GET", "path": "/v1/jobs/abc/events",
             "headers": [(k.lower().encode(), v.encode()) for k, v in remote(sign(service_claims(exp=NOW + 6000))).items()]}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    assert [m.get("body") for m in sent if m["type"] == "http.response.body"] == [b"c0", b""]


def test_verify_slot_released_after_worker_finishes(tmp_path):
    mw, probe, *_ = build(tmp_path)
    assert status(run(mw, headers=remote(sign(user_claims())))) == 200
    assert mw._pending_verify == 0


# ---------------------------------------------------------------- 0.12.35 review round 7
def _call(mw, app, headers):
    sent = []
    mw.app = app
    scope = {"type": "http", "method": "GET", "path": "/v1/jobs/abc/events",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


def test_app_timeout_error_propagates(tmp_path):
    mw, probe, *_ = build(tmp_path)

    async def app(scope, receive, send):
        raise TimeoutError("backend slow")

    with pytest.raises(TimeoutError):
        _call(mw, app, remote(sign(service_claims(exp=NOW + 6000))))


def test_deadline_does_not_wait_for_stubborn_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "MAX_REMOTE_REQUEST_SECONDS", 0.05)
    mw, probe, *_ = build(tmp_path)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"c0", "more_body": True})
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await send({"type": "http.response.body", "body": b"late", "more_body": True})
            await asyncio.sleep(0.01)

    sent = _call(mw, app, remote(sign(service_claims(exp=NOW + 6000))))
    assert [m.get("body") for m in sent if m["type"] == "http.response.body"] == [b"c0", b""]


def test_sized_response_is_aborted_not_padded(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "MAX_REMOTE_REQUEST_SECONDS", 0.05)
    mw, probe, *_ = build(tmp_path)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"10")]})
        await send({"type": "http.response.body", "body": b"12345", "more_body": True})
        await asyncio.sleep(5)

    sent = _call(mw, app, remote(sign(service_claims(exp=NOW + 6000))))
    assert [m.get("body") for m in sent if m["type"] == "http.response.body"] == [b"12345"]


def test_headers_not_sent_once_revoked(tmp_path):
    mw, probe, path, clock = build(tmp_path)

    async def app(scope, receive, send):
        clock.t = NOW + 600 + ra.CLOCK_SKEW_SECONDS + 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"secret"})

    sent = _call(mw, app, remote(sign(service_claims(exp=NOW + 600))))
    assert sent[0]["status"] == 403 and b"secret" not in b"".join(m.get("body", b"") for m in sent)


def test_server_cancel_cancels_the_app_and_closes_the_gate(tmp_path):
    mw, probe, *_ = build(tmp_path)
    sent, state = [], {}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            state["cancelled"] = True
            await send({"type": "http.response.body", "body": b"late", "more_body": True})
            raise

    mw.app = app
    scope = {"type": "http", "method": "GET", "path": "/v1/jobs/abc/events",
             "headers": [(k.lower().encode(), v.encode()) for k, v in remote(sign(service_claims(exp=NOW + 6000))).items()]}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    async def main():
        outer = asyncio.ensure_future(mw(scope, receive, send))
        await asyncio.sleep(0.05)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert state.get("cancelled") and not any(m.get("body") == b"late" for m in sent)


def test_cut_does_not_hang_when_the_close_frame_stalls(tmp_path):
    clock = Clock()
    mw, app, *_ = build_ws(tmp_path, clock=clock)
    token = sign(user_claims(exp=NOW + 100))

    async def drive():
        scope = {"type": "websocket", "path": VNC_PATH,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in owner_ws(token).items()]}
        first = [True]

        async def receive():
            if first[0]:
                first[0] = False
                return {"type": "websocket.connect"}
            clock.t = NOW + 200
            await asyncio.sleep(30)
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            if message["type"] == "websocket.close":
                await asyncio.sleep(30)          # a client that never takes the close frame
        await asyncio.wait_for(mw(scope, receive, send), 6)
    asyncio.run(drive())
    assert app.cancelled


def test_a_frame_stalled_at_the_cut_is_never_delivered(tmp_path):
    clock = Clock()
    path = write_config(tmp_path)
    delivered, state = [], {}

    async def app(scope, receive, send):
        await receive()
        await send({"type": "websocket.accept"})
        state["sending"] = True
        await send({"type": "websocket.send", "bytes": b"late"})   # stalls on a slow client
        await asyncio.sleep(30)

    mw = ra.RemoteAccessMiddleware(app, ra.ConfigLoader(lambda: path, clock), fetcher=Fetcher(), clock=clock)
    mw.ws_recheck = 0.01
    token = sign(user_claims(exp=NOW + 100))

    async def drive():
        scope = {"type": "websocket", "path": VNC_PATH,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in owner_ws(token).items()]}

        async def receive():
            if "connected" not in state:
                state["connected"] = True
                return {"type": "websocket.connect"}
            await asyncio.sleep(30)

        async def send(message):
            if message["type"] == "websocket.send":
                clock.t = NOW + 200          # the JWT expires while this frame waits on the client
                await asyncio.sleep(0.5)
            delivered.append(message)
        await asyncio.wait_for(mw(scope, receive, send), 6)
    asyncio.run(drive())
    assert state.get("sending")
    assert not any(m.get("bytes") == b"late" for m in delivered)
    assert delivered[-1] == {"type": "websocket.close", "code": 1008}


def test_cut_from_inside_the_app_still_sends_the_close_frame(tmp_path):
    clock = Clock()
    path = write_config(tmp_path)
    sent = []

    async def app(scope, receive, send):
        await receive()
        await send({"type": "websocket.accept"})

        async def reader():
            while (await receive())["type"] != "websocket.disconnect":
                pass
        child = asyncio.ensure_future(reader())   # the cut is detected in this child, like the VNC proxy
        try:
            await asyncio.sleep(30)
        finally:
            child.cancel()

    mw = ra.RemoteAccessMiddleware(app, ra.ConfigLoader(lambda: path, clock), fetcher=Fetcher(), clock=clock)
    mw.ws_recheck = 60.0                           # the watchdog is not what notices it
    token = sign(user_claims(exp=NOW + 100))

    async def drive():
        scope = {"type": "websocket", "path": VNC_PATH,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in owner_ws(token).items()]}
        steps = iter([{"type": "websocket.connect"}, {"type": "websocket.receive", "bytes": b"x"}])

        async def receive():
            message = next(steps, None)
            if message is None:
                await asyncio.sleep(30)
            if message["type"] == "websocket.receive":
                clock.t = NOW + 200
            return message

        async def send(message):
            sent.append(message)
        await asyncio.wait_for(mw(scope, receive, send), 6)
    asyncio.run(drive())
    assert sent[-1] == {"type": "websocket.close", "code": 1008}
