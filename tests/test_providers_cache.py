"""GET /v1/providers is served from a cache (0.12.8): hit, TTL expiry, single-flight, invalidation,
startup warm, and a failed probe never poisons it. Unit tests drive ProviderStatusCache with a fake clock;
the API tests use FakeAdapter engines that count their probes."""

import asyncio
import time

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.domain import ProviderInfo
import pytest

from botoffice.provider_status import ProviderProbeError, ProviderStatusCache
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Probe:
    def __init__(self, login="ok", delay=0.0):
        self.calls = 0
        self.login = login
        self.delay = delay
        self.fail = False

    async def __call__(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("probe blew up")
        return [ProviderInfo(name="codex", enabled=True, login_status=self.login)]


def make(probe=None, **kw):
    clock = Clock()
    probe = probe or Probe()
    return ProviderStatusCache(probe, clock=clock, **kw), probe, clock


async def settle(cache):
    task = cache._task
    if task is not None:
        await task


def test_hit_serves_cache_without_probing():
    async def go():
        cache, probe, _ = make()
        first = await cache.get()
        second = await cache.get()
        assert first == second and probe.calls == 1
    asyncio.run(go())


def test_ttl_expiry_serves_stale_at_once_and_renews_in_background():
    async def go():
        cache, probe, clock = make(ttl=60)
        await cache.get()
        probe.login = "needs_login"
        clock.now += 61
        stale = await cache.get()
        assert stale[0].login_status == "ok"        # answered without waiting for the probe
        await settle(cache)
        assert probe.calls == 2
        assert (await cache.get())[0].login_status == "needs_login"
        assert probe.calls == 2
    asyncio.run(go())


def test_single_flight_for_concurrent_callers_and_stale_renewals():
    async def go():
        cache, probe, clock = make(Probe(delay=0.05))
        results = await asyncio.gather(*(cache.get() for _ in range(10)))
        assert probe.calls == 1 and all(r == results[0] for r in results)
        clock.now += 100
        await asyncio.gather(*(cache.get() for _ in range(10)))
        await settle(cache)
        assert probe.calls == 2
    asyncio.run(go())


def test_refresh_probes_now_and_concurrent_refreshes_share_one_probe():
    async def go():
        cache, probe, clock = make(Probe(delay=0.05))
        await cache.get()
        probe.login = "needs_login"
        clock.now += 1
        results = await asyncio.gather(*(cache.get(refresh=True) for _ in range(5)))
        assert all(r[0].login_status == "needs_login" for r in results)
        assert probe.calls == 2
    asyncio.run(go())


def test_refresh_does_not_return_a_probe_that_started_before_it():
    async def go():
        cache, probe, clock = make(Probe(delay=0.05))
        first = asyncio.create_task(cache.get())
        await asyncio.sleep(0.01)
        probe.login = "needs_login"      # the user signed out while the first probe ran
        clock.now += 5
        fresh = await cache.get(refresh=True)
        await first
        assert fresh[0].login_status == "needs_login" and probe.calls == 2
    asyncio.run(go())


def test_invalidate_makes_the_next_call_wait_for_a_new_probe():
    async def go():
        cache, probe, clock = make()
        await cache.get()
        probe.login = "needs_login"
        cache.invalidate()
        clock.now += 1
        assert (await cache.get())[0].login_status == "needs_login"
        assert probe.calls == 2
        await cache.get()
        assert probe.calls == 2
    asyncio.run(go())


def test_invalidate_during_a_probe_discards_its_answer():
    async def go():
        cache, probe, clock = make(Probe(delay=0.05))
        task = asyncio.create_task(cache.get())
        await asyncio.sleep(0.01)
        cache.invalidate()
        await task
        clock.now += 1
        await cache.get()
        assert probe.calls == 2
    asyncio.run(go())


def test_background_probe_failure_keeps_the_last_answer():
    async def go():
        cache, probe, clock = make(ttl=60)
        await cache.get()
        probe.fail = True
        clock.now += 61
        assert (await cache.get())[0].login_status == "ok"     # stale answer, renewal fails behind it
        await settle(cache)
        assert (await cache.get())[0].login_status == "ok"     # still served, and it retries next time
        probe.fail = False
        probe.login = "needs_login"
        clock.now += 1
        await cache.get()
        await settle(cache)
        assert (await cache.get())[0].login_status == "needs_login"
    asyncio.run(go())


def test_a_probe_the_caller_waits_for_that_fails_raises_not_stale():
    async def go():
        cache, probe, clock = make()
        await cache.get()
        probe.fail = True
        clock.now += 5
        with pytest.raises(ProviderProbeError):
            await cache.get(refresh=True)
        cache.invalidate()
        with pytest.raises(ProviderProbeError):
            await cache.get()
        probe.fail = False
        assert (await cache.get())[0].login_status == "ok"     # nothing was poisoned
    asyncio.run(go())


def test_first_probe_failure_raises_and_is_not_cached():
    async def go():
        cache, probe, _ = make()
        probe.fail = True
        with pytest.raises(ProviderProbeError):
            await cache.get()
        probe.fail = False
        assert (await cache.get())[0].login_status == "ok"
    asyncio.run(go())


def test_invalidate_during_a_probe_gives_waiters_the_new_answer():
    async def go():
        cache, probe, clock = make(Probe(delay=0.05))
        waiting = asyncio.create_task(cache.get())
        await asyncio.sleep(0.01)
        probe.login = "needs_login"
        cache.invalidate()
        result = await waiting
        assert result[0].login_status == "needs_login" and probe.calls == 2
    asyncio.run(go())


def test_refresh_burst_with_a_running_clock_shares_a_probe():
    async def go():
        cache, probe, clock = make(Probe(delay=0.05))
        await cache.get()
        clock.now += 5
        first = asyncio.create_task(cache.get(refresh=True))
        await asyncio.sleep(0.01)
        clock.now += 0.2                 # the second caller arrives a moment later
        await asyncio.gather(first, cache.get(refresh=True))
        assert probe.calls == 2
    asyncio.run(go())


def test_error_answer_is_trusted_only_briefly():
    async def go():
        cache, probe, clock = make(Probe(login="error"), ttl=60, error_ttl=10)
        await cache.get()
        probe.login = "ok"
        clock.now += 11
        await cache.get()            # stale after 11 s: renewed behind the caller
        await settle(cache)
        assert probe.calls == 2
        assert (await cache.get())[0].login_status == "ok"
        clock.now += 11
        await cache.get()
        assert probe.calls == 2      # an ok answer lives the full TTL
    asyncio.run(go())


def _app(settings, fake):
    return create_app(settings, providers={"codex": fake, "claude": FakeAdapter(), "cursor": FakeAdapter(),
                                           "antigravity": FakeAdapter(), "opencode": FakeAdapter(),
                                           "grok": FakeAdapter(enabled=False)})


def _counting(fake):
    fake.probes = 0
    original = fake.login_status

    async def counted():
        fake.probes += 1
        return await original()
    fake.login_status = counted
    return fake


def test_api_hit_shape_and_refresh(settings):
    fake = _counting(FakeAdapter())
    with TestClient(_app(settings, fake)) as client:
        first = client.get("/v1/providers", headers=H)
        assert first.status_code == 200
        body = first.json()
        assert [i["name"] for i in body] == ["codex", "claude", "cursor", "antigravity", "opencode", "grok"]
        assert all(set(item) == {"name", "enabled", "login_status"} for item in body)
        probes = fake.probes
        for _ in range(3):
            assert client.get("/v1/providers", headers=H).json() == body
        assert fake.probes == probes               # served from the cache
        fake.login = "needs_login"
        assert client.get("/v1/providers", headers=H).json() == body
        fresh = client.get("/v1/providers?refresh=1", headers=H).json()
        assert next(i for i in fresh if i["name"] == "codex")["login_status"] == "needs_login"
        assert fake.probes == probes + 1


def test_api_startup_warms_the_cache(settings):
    fake = _counting(FakeAdapter())
    with TestClient(_app(settings, fake)) as client:
        for _ in range(100):
            if client.app.state.provider_status._value is not None:
                break
            time.sleep(0.02)
        assert fake.probes == 1
        client.get("/v1/providers", headers=H)
        assert fake.probes == 1


def test_api_needs_login_job_invalidates_the_cache(settings):
    fake = _counting(FakeAdapter())
    with TestClient(_app(settings, fake)) as client:
        client.get("/v1/providers", headers=H)
        probes = fake.probes
        fake.login = "needs_login"
        client.app.state.scheduler.on_needs_login()
        body = client.get("/v1/providers", headers=H).json()
        assert next(i for i in body if i["name"] == "codex")["login_status"] == "needs_login"
        assert fake.probes == probes + 1


def test_api_refresh_requires_auth(settings):
    with TestClient(_app(settings, FakeAdapter())) as client:
        assert client.get("/v1/providers?refresh=1").status_code in (401, 403)


def test_api_probe_failure_is_a_503_not_stale_data(settings):
    fake = FakeAdapter()
    with TestClient(_app(settings, fake)) as client:
        client.get("/v1/providers", headers=H)
        cache = client.app.state.provider_status

        async def broken():
            raise RuntimeError("boom")
        cache._probe = broken
        assert client.get("/v1/providers?refresh=1", headers=H).status_code == 503
