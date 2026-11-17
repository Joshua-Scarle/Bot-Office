"""OpenCode's model picker is filled from `opencode models` (cached, refreshable, safe fallback)."""
import asyncio

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.catalog import STATIC_MODELS, shortlist_models
from botoffice.providers import cli
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.opencode import OpenCodeAdapter

H = {"Authorization": "Bearer test-token"}
LISTING = (b"\x1b[0m\nopencode/big-pickle\nopencode/glm-5.3-flash-free\n"
           b"openrouter/anthropic/claude-x\nnot-a-provider-model\nopencode/big-pickle\n")


class Proc:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode

    async def communicate(self, input=None):
        return self.stdout, b""


def fake_cli(monkeypatch, outputs):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        out = outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return Proc(*out) if isinstance(out, tuple) else Proc(out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_lists_provider_model_ids_from_the_cli_and_caches(monkeypatch):
    calls = fake_cli(monkeypatch, [LISTING])
    adapter = OpenCodeAdapter(executable="opencode")
    expected = [("opencode/big-pickle", "opencode/big-pickle"),
                ("opencode/glm-5.3-flash-free", "opencode/glm-5.3-flash-free"),
                ("openrouter/anthropic/claude-x", "openrouter/anthropic/claude-x")]
    assert asyncio.run(adapter.list_models()) == expected
    assert asyncio.run(adapter.list_models()) == expected
    assert calls == [("opencode", "models")]  # cached, and never a chat run


def test_refresh_asks_the_cli_again(monkeypatch):
    calls = fake_cli(monkeypatch, [LISTING, b"opencode/big-pickle\nanthropic/claude-new\n"])
    adapter = OpenCodeAdapter(executable="opencode")
    asyncio.run(adapter.list_models())
    assert [m for m, _ in asyncio.run(adapter.list_models(refresh=True))] == ["opencode/big-pickle", "anthropic/claude-new"]
    assert len(calls) == 2


def test_failed_listing_keeps_the_last_good_one_and_backs_off(monkeypatch):
    calls = fake_cli(monkeypatch, [LISTING, (b"boom", 1)])
    adapter = OpenCodeAdapter(executable="opencode")
    good = asyncio.run(adapter.list_models())
    adapter._models_cache = (adapter._models_cache[0] - cli.MODEL_CACHE_SECONDS - 1, adapter._models_cache[1])
    assert asyncio.run(adapter.list_models()) == good  # CLI failed: last good listing
    assert asyncio.run(adapter.list_models()) == good  # not asked again right away
    assert len(calls) == 2


def test_no_listing_falls_back_to_the_static_catalog(monkeypatch):
    calls = fake_cli(monkeypatch, [OSError("gone"), b"garbage line\n"])
    adapter = OpenCodeAdapter(executable="opencode")
    assert asyncio.run(adapter.list_models()) == list(STATIC_MODELS["opencode"])
    assert asyncio.run(adapter.list_models()) == list(STATIC_MODELS["opencode"])  # backoff: no retry yet
    assert len(calls) == 1
    adapter._models_failed_at -= cli.MODEL_FAIL_BACKOFF + 1
    assert asyncio.run(adapter.list_models()) == list(STATIC_MODELS["opencode"])  # unparseable listing
    assert len(calls) == 2


def test_cli_timeout_falls_back(monkeypatch):
    adapter = OpenCodeAdapter(executable="opencode")

    async def no_answer(*args, **kwargs):
        return None

    monkeypatch.setattr(adapter, "_capture", no_answer)
    assert asyncio.run(adapter.list_models()) == list(STATIC_MODELS["opencode"])


def test_opencode_dropdown_is_not_cut_to_24():
    models = [(f"prov{i}/model-{i}", f"prov{i}/model-{i}") for i in range(80)]
    assert shortlist_models("opencode", models) == models
    assert len(shortlist_models("cursor", [(f"m{i}", f"m{i}") for i in range(80)])) == 24


class OpenCodeListing(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.refreshes = []

    async def list_models(self, refresh=False):
        self.refreshes.append(refresh)
        return [("opencode/big-pickle", "opencode/big-pickle"), ("anthropic/claude-x", "anthropic/claude-x")]


def test_model_choices_lists_opencode_models_and_refreshes(settings):
    adapter = OpenCodeListing()
    with TestClient(create_app(settings, {"opencode": adapter})) as client:
        choices = client.get("/v1/model-choices", headers=H).json()
        client.get("/v1/model-choices?refresh=1", headers=H)
        bot = client.post("/v1/bots", headers=H, json={"name": "oc", "default_provider": "opencode",
                                                        "model": "opencode/big-pickle"}).json()
        typed = client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"model": "someprovider/typed-model"})
        bad = client.patch(f"/v1/bots/{bot['id']}", headers=H, json={"model": "no-slash-model"})
    assert [m["id"] for m in choices[0]["models"]] == ["opencode/big-pickle", "anthropic/claude-x"]
    assert adapter.refreshes == [False, True]
    assert typed.status_code == 200 and typed.json()["model"] == "someprovider/typed-model"
    assert bad.status_code == 400


def test_concurrent_listings_run_the_cli_once(monkeypatch):
    calls = []

    async def slow_exec(*args, **kwargs):
        calls.append(args)
        await asyncio.sleep(0.05)  # a real CLI run takes time, so the other calls overlap it
        return Proc(LISTING)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)
    adapter = OpenCodeAdapter(executable="opencode")

    async def many():
        return await asyncio.gather(adapter.list_models(), adapter.list_models(refresh=True), adapter.list_models())

    results = asyncio.run(many())
    assert results[0] == results[1] == results[2] and len(calls) == 1


def test_concurrent_failures_share_one_attempt(monkeypatch):
    calls = fake_cli(monkeypatch, [(b"boom", 1), (b"boom", 1)])
    adapter = OpenCodeAdapter(executable="opencode")

    async def many():
        return await asyncio.gather(*(adapter.list_models() for _ in range(3)))

    assert all(r == list(STATIC_MODELS["opencode"]) for r in asyncio.run(many()))
    assert len(calls) == 1


def test_refresh_queued_behind_a_successful_leader_reuses_its_result(monkeypatch):
    calls = []

    async def slow_exec(*args, **kwargs):
        calls.append(args)
        await asyncio.sleep(0.05)
        return Proc(LISTING)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)
    adapter = OpenCodeAdapter(executable="opencode")

    async def run():
        leader = asyncio.ensure_future(adapter.list_models())
        await asyncio.sleep(0.01)  # the leader holds the lock and is running the CLI
        queued = asyncio.ensure_future(adapter.list_models(refresh=True))  # waits on the lock behind the leader
        return await leader, await queued

    first, queued = asyncio.run(run())
    assert queued == first and len(calls) == 1
