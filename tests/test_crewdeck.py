import asyncio
import base64
import hashlib
import re
import sqlite3
import time
from datetime import datetime
from uuid import uuid4

from fastapi.testclient import TestClient

import botoffice
from botoffice.api import MAX_ICON_BYTES, create_app
from botoffice.catalog import shortlist_models,  parse_model_listing
from botoffice.domain import Job, JobStatus, Slot
from botoffice.providers.antigravity import AntigravityAdapter
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.cursor import PROMPT_POINTER, CursorAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore

H = {"Authorization": "Bearer test-token"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class RecordingFake(FakeAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.jobs = []

    async def run(self, job, workspace, log_path, timeout):
        self.jobs.append(job)
        (workspace / "reply.txt").write_text(f"reply via {job.provider}/{job.model}", encoding="utf-8")
        return await super().run(job, workspace, log_path, timeout)


class ListingFake(FakeAdapter):
    async def list_models(self):
        return [("auto", "Auto"), ("composer-2.5", "Composer 2.5")]


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def messages(client, bot_id):
    return client.get(f"/v1/bots/{bot_id}/messages", headers=H).json()


def test_version_is_0_7_0():
    assert botoffice.__version__ == "0.12.42"


def test_model_choices_lists_only_signed_in_engines(settings):
    providers = {
        "codex": FakeAdapter(),
        "claude": FakeAdapter(login="needs_login"),
        "cursor": ListingFake(),
        "antigravity": FakeAdapter(enabled=False),
    }
    with TestClient(create_app(settings, providers)) as client:
        assert client.get("/v1/model-choices").status_code == 401
        choices = client.get("/v1/model-choices", headers=H).json()
    by_engine = {c["engine"]: c for c in choices}
    assert list(by_engine) == ["codex", "cursor"]
    assert "grok" not in by_engine and "claude" not in by_engine and "antigravity" not in by_engine
    codex = by_engine["codex"]
    assert codex["label"] == "Codex" and codex["default_model"] == "gpt-6.1-terra"
    assert codex["models"][0] == {
        "id": "gpt-6.1-terra",
        "label": "gpt-6.1-terra",
        "display": "Codex · gpt-6.1-terra",
        "capability_tier": "frontier",
    }
    assert [m["id"] for m in by_engine["cursor"]["models"]] == ["auto", "composer-2.5"]


def test_model_choices_claude_labels(settings):
    providers = {"codex": FakeAdapter(login="error"), "claude": FakeAdapter()}
    with TestClient(create_app(settings, providers)) as client:
        choices = client.get("/v1/model-choices", headers=H).json()
    assert [c["engine"] for c in choices] == ["claude"]
    displays = [m["display"] for m in choices[0]["models"]]
    assert displays[0] == "Claude · Opus 5.5"
    assert {m["id"] for m in choices[0]["models"]} == {
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
    }


def test_changing_model_keeps_identity_memory_and_history(settings):
    codex, cursor = RecordingFake(), RecordingFake()
    with TestClient(create_app(settings, {"codex": codex, "cursor": cursor})) as client:
        bot = client.post(
            "/v1/bots",
            json={"name": "Atlas", "icon": "🛰️", "description": "research", "instructions": "be brief"},
            headers=H,
        ).json()
        assert bot["engine"] == "codex" and bot["model"] == "gpt-6.1-terra" and bot["icon"] == "🛰️"
        client.put(f"/v1/bots/{bot['id']}/memory", json={"text": "remember X"}, headers=H)
        client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "first"}, headers=H)
        wait_for(lambda: len(messages(client, bot["id"])) == 2)
        assert codex.jobs[-1].model == "gpt-6.1-terra"

        patched = client.patch(
            f"/v1/bots/{bot['id']}", json={"engine": "cursor", "model": "composer-2.5"}, headers=H
        )
        assert patched.status_code == 200, patched.text
        after = patched.json()
        for key in ("id", "name", "icon", "description", "instructions", "created_at"):
            assert after[key] == bot[key], key
        assert after["engine"] == after["default_provider"] == "cursor"
        assert after["model"] == "composer-2.5"
        assert client.get(f"/v1/bots/{bot['id']}/memory", headers=H).json()["text"] == "remember X"
        assert messages(client, bot["id"])[0]["text"] == "first"

        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "second"}, headers=H).json()
        assert posted["job"]["provider"] == "cursor" and posted["job"]["model"] == "composer-2.5"
        history = wait_for(lambda: (m := messages(client, bot["id"])) and len(m) == 4 and m)
        assert history[-1]["text"] == "reply via cursor/composer-2.5"
        assert cursor.jobs[-1].model == "composer-2.5"
        assert [b["name"] for b in client.get("/v1/bots", headers=H).json()] == ["Atlas"]

        # switching engine without a model picks that engine's default model
        back = client.patch(f"/v1/bots/{bot['id']}", json={"engine": "codex"}, headers=H).json()
        assert back["model"] == "gpt-6.1-terra" and back["name"] == "Atlas"


def test_bot_model_validation(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bad = [
            {"name": "a", "model": "-rf"},
            {"name": "a", "model": "has space"},
            {"name": "a", "engine": "grok"},
            {"name": "a", "engine": "codex", "default_provider": "cursor"},
            {"name": "a", "icon": "<b>"},
            {"name": "a", "icon": "x" * 17},
        ]
        for body in bad:
            assert client.post("/v1/bots", json=body, headers=H).status_code == 422, body
        legacy = client.post("/v1/bots", json={"name": "legacy", "default_provider": "cursor"}, headers=H)
        assert legacy.json()["engine"] == "cursor" and legacy.json()["model"] == "auto"
        assert legacy.json()["icon"] == "🤖"
        job = client.post("/v1/jobs", json={"prompt": "x", "model": "--yolo"}, headers=H)
        assert job.status_code == 422


def test_claude_model_needs_no_reason(settings):
    # 0.11.8: creating or switching a bot to Claude works with no reason; an old client's reason is still kept
    providers = {"codex": FakeAdapter(), "claude": FakeAdapter()}
    with TestClient(create_app(settings, providers)) as client:
        no_reason = {"name": "Plain", "engine": "claude", "model": "claude-sonnet-5-5"}
        created = client.post("/v1/bots", json=no_reason, headers=H)
        assert created.status_code == 201 and created.json()["model"] == "claude-sonnet-5-5"
        bot = client.post("/v1/bots", json={"name": "Sage"}, headers=H).json()
        switch = {"engine": "claude", "model": "claude-opus-5-5"}
        switched = client.patch(f"/v1/bots/{bot['id']}", json=switch, headers=H)
        assert switched.status_code == 200 and switched.json()["model"] == "claude-opus-5-5"
        ok = client.patch(f"/v1/bots/{bot['id']}", json=switch | {"reason": "long review"}, headers=H)
        assert ok.status_code == 200 and ok.json()["reason"] == "long review"
        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "first"}, headers=H)
        assert posted.status_code == 201
        cleared = client.patch(f"/v1/bots/{bot['id']}", json={"reason": None}, headers=H)
        assert cleared.status_code == 200
        posted = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "SECRET PROMPT"}, headers=H)
        assert posted.status_code == 201 and posted.json()["job"]["model"] == "claude-opus-5-5"
        log = settings.data_dir / "claude-use-log.tsv"
        assert log.stat().st_mode & 0o777 == 0o600
        assert "long review" in log.read_text()
        assert "SECRET" not in log.read_text() and "test-token" not in log.read_text()


def test_delete_bot_removes_only_that_bot(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(delay=5)})) as client:
        keep = client.post("/v1/bots", json={"name": "Keep"}, headers=H).json()
        gone = client.post("/v1/bots", json={"name": "Gone"}, headers=H).json()
        client.put(f"/v1/bots/{keep['id']}/memory", json={"text": "keep me"}, headers=H)
        client.put(f"/v1/bots/{gone['id']}/memory", json={"text": "bye"}, headers=H)
        client.put("/v1/shared-memory", json={"text": "shared stays"}, headers=H)
        client.put(f"/v1/bots/{gone['id']}/icon", content=PNG, headers=H | {"Content-Type": "image/png"})
        client.post(f"/v1/bots/{gone['id']}/routines", json={"cron": "* * * * *", "prompt": "p"}, headers=H)
        client.post(f"/v1/bots/{keep['id']}/routines", json={"cron": "0 9 * * *", "prompt": "k"}, headers=H)
        for _ in range(3):
            client.post(f"/v1/bots/{gone['id']}/messages", json={"text": "hi"}, headers=H)
        client.post(f"/v1/bots/{keep['id']}/messages", json={"text": "stay"}, headers=H)

        assert client.delete(f"/v1/bots/{gone['id']}").status_code == 401
        assert client.delete(f"/v1/bots/{gone['id']}", headers=H).status_code == 204
        assert client.get(f"/v1/bots/{gone['id']}", headers=H).status_code == 404
        assert client.delete(f"/v1/bots/{gone['id']}", headers=H).status_code == 404
        assert not (settings.data_dir / "bots" / gone["id"]).exists()
        store = client.app.state.store
        assert store.list_messages(gone["id"]) == [] and store.list_routines(gone["id"]) == []
        gone_jobs = [j for j in store.list() if str(j.bot_id) == gone["id"]]
        assert gone_jobs and all(j.status in {JobStatus.CANCELLED, JobStatus.RUNNING} for j in gone_jobs)

        assert [b["name"] for b in client.get("/v1/bots", headers=H).json()] == ["Keep"]
        assert client.get(f"/v1/bots/{keep['id']}/memory", headers=H).json()["text"] == "keep me"
        assert client.get("/v1/shared-memory", headers=H).json()["text"] == "shared stays"
        assert len(client.get(f"/v1/bots/{keep['id']}/routines", headers=H).json()) == 1
        assert messages(client, keep["id"])[0]["text"] == "stay"


def test_icon_upload_validation_and_auth(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = client.post("/v1/bots", json={"name": "Pic"}, headers=H).json()
        url = f"/v1/bots/{bot['id']}/icon"
        assert client.get(url, headers=H).status_code == 404                           # 0.12.2: the default is a character, drawn in the browser
        assert client.post(f"{url}/generate", json={"seed": "preset-1"}, headers=H).status_code == 200
        assert client.get(url, headers=H).headers["content-type"] == "image/png"      # a generated pattern is served as a PNG
        assert client.put(url, content=PNG, headers={"Content-Type": "image/png"}).status_code == 401
        assert client.put(url, content=b"", headers=H | {"Content-Type": "image/png"}).status_code == 400
        huge = PNG + b"\x00" * MAX_ICON_BYTES
        assert client.put(url, content=huge, headers=H | {"Content-Type": "image/png"}).status_code == 413
        svg = b'<svg xmlns="x"><script>alert(1)</script></svg>'
        assert client.put(url, content=svg, headers=H | {"Content-Type": "image/svg+xml"}).status_code == 415
        assert client.put(url, content=b"hello", headers=H | {"Content-Type": "image/png"}).status_code == 415

        import io

        from PIL import Image
        real = io.BytesIO()
        Image.new("RGB", (40, 30), (9, 9, 9)).save(real, format="PNG")
        assert client.put(url, content=PNG, headers=H | {"Content-Type": "image/png"}).status_code == 415   # 0.12.0: fully decoded
        ok = client.put(url, content=real.getvalue(), headers=H | {"Content-Type": "image/png"})
        assert ok.status_code == 200 and ok.json()["icon_image"] == "image/png"
        path = settings.data_dir / "bots" / bot["id"] / "icon"
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert client.get(url).status_code == 401
        served = client.get(url, headers=H)
        assert served.content[:8] == b"\x89PNG\r\n\x1a\n" and served.headers["content-type"] == "image/png"   # re-encoded, 256 px
        assert served.headers["x-content-type-options"] == "nosniff"

        removed = client.delete(url, headers=H)
        assert removed.json()["icon_image"] is None and not path.exists()
        assert client.get(url, headers=H).status_code == 404


def test_root_page_is_dark_bot_office_with_strict_csp(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    html = page.text
    assert "<title>Bot-Office</title>" in html and "Bot-Office" in html
    assert "color-scheme: dark" in html
    assert "http://" not in html and "https://" not in html and "test-token" not in html
    assert 'type="password"' in html and "bot-office-token" in html and "crewdeck-token" in html and "agentdesk-token" in html
    assert "sessionStorage" in html
    # localStorage holds only the two layout preferences (0.11.13); the token stays in sessionStorage
    import re as _re
    assert set(_re.findall(r"localStorage\.\w+\(\s*([A-Z_]+)", html)) <= {"NEEDS_OPEN_KEY", "SIDEBAR_KEY"}
    assert "localStorage" in html and not _re.search(r"localStorage[^;\n]*(?:TOKEN|getToken|token)", html)
    assert '"Authorization": "Bearer "' in html and "?token" not in html
    assert 'style="' not in html  # CSP below forbids inline style attributes
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "unsafe-inline" not in csp
    for tag in ("script", "style"):
        [block] = re.findall(rf"<{tag}>(.*?)</{tag}>", html, flags=re.DOTALL)
        digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp


def test_new_routes_require_auth(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        uid = client.post("/v1/bots", json={"name": "z"}, headers=H).json()["id"]
        for method, path in [
            ("get", "/v1/model-choices"),
            ("delete", f"/v1/bots/{uid}"),
            ("get", f"/v1/bots/{uid}/icon"),
            ("put", f"/v1/bots/{uid}/icon"),
            ("delete", f"/v1/bots/{uid}/icon"),
        ]:
            assert getattr(client, method)(path).status_code == 401, (method, path)
            wrong = {"Authorization": "Bearer nope"}
            assert getattr(client, method)(path, headers=wrong).status_code == 401, (method, path)
        assert client.get("/docs").status_code == 404 and client.get("/redoc").status_code == 404
        assert client.get(f"/v1/bots/{uid}", headers=H).status_code == 200


def _legacy_db(path):
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE bots (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, instructions TEXT NOT NULL,
        default_provider TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    db.execute(
        """CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL,
        role TEXT NOT NULL, sender TEXT, source TEXT NOT NULL, text TEXT NOT NULL,
        job_id TEXT, created_at TEXT NOT NULL)"""
    )
    stamp = "2026-11-12T23:50:00+00:00"
    ids = {"Test Bot": str(uuid4()), "Cursor Bot": str(uuid4())}
    db.execute("INSERT INTO bots VALUES (?,?,?,?,?,?,?)", (ids["Test Bot"], "Test Bot", "t", "codex", None, stamp, stamp))
    db.execute("INSERT INTO bots VALUES (?,?,?,?,?,?,?)", (ids["Cursor Bot"], "Cursor Bot", "c", "cursor", None, stamp, stamp))
    db.execute(
        "INSERT INTO messages (bot_id,role,source,text,created_at) VALUES (?,?,?,?,?)",
        (ids["Cursor Bot"], "user", "chat", "old history", stamp),
    )
    db.commit()
    db.close()
    return ids


def test_identity_migration_renames_cli_named_bot_once(tmp_path):
    path = tmp_path / "agentdesk.sqlite3"
    ids = _legacy_db(path)
    store = JobStore(path)
    test_bot, scout = store.get_bot(ids["Test Bot"]), store.get_bot(ids["Cursor Bot"])
    assert test_bot.name == "Test Bot" and test_bot.engine == "codex" and test_bot.model == "gpt-6.1-terra"
    assert test_bot.icon
    assert scout.name == "Scout" and scout.engine == "cursor" and scout.model == "auto" and scout.icon
    assert scout.instructions == "c"
    assert [m.text for m in store.list_messages(scout.id)] == ["old history"]
    # bridge senders that still address the old name reach the same bot
    assert store.find_bot("Cursor Bot").id == scout.id
    # one-time: a later bot may use that name and is not renamed on restart
    store.update_bot(scout.id, model="composer-2.5")
    fresh = JobStore(path)
    assert fresh.get_bot(scout.id).model == "composer-2.5" and fresh.get_bot(scout.id).name == "Scout"
    created = fresh.create_bot("Cursor Bot", "", "codex", None)
    again = JobStore(path)
    assert again.get_bot(created.id).name == "Cursor Bot"
    assert again.find_bot("Cursor Bot").id == created.id


def _job(model=None, prompt="write report"):
    return Job(
        id=uuid4(), prompt=prompt, status=JobStatus.RUNNING, created_at=datetime.now(),
        slot=Slot.DESKTOP_2, model=model, cli_access="restricted",
    )


def test_adapters_pass_selected_model(tmp_path):
    claude = ClaudeAdapter(executable="claude")._exec_args(tmp_path, _job("claude-sonnet-5-5"))
    assert claude[-4:] == ["--model", "claude-sonnet-5-5", "--effort", "medium"]
    assert {"--output-format", "stream-json", "--permission-mode", "acceptEdits", "--permission-prompts", "none"} <= set(claude)
    assert "--dangerously-skip-permissions" not in claude and "--bare" not in claude
    cursor = CursorAdapter(executable="cursor-agent")._exec_args(tmp_path, _job("composer-2.5"))
    assert cursor[0] == "-p" and cursor[-3:] == ["--model", "composer-2.5", PROMPT_POINTER]
    agy = AntigravityAdapter(executable="agy")._exec_args(tmp_path, _job("gemini-3.8-flash-low"))
    assert agy[:2] == ["-p", "write report"]
    assert agy[-2:] == ["--model", "gemini-3.8-flash-low"]  # 0.11.15: the id names its effort; --effort would conflict
    assert AntigravityAdapter(executable="agy")._exec_args(tmp_path, _job("gemini-3.8-pro"))[-4:] == ["--model", "gemini-3.8-pro", "--effort", "medium"]
    codex = CodexAdapter(executable="codex")._exec_args(tmp_path, _job("gpt-6.1-terra"))
    assert codex == [
        "exec", "-m", "gpt-6.1-terra", "-c", "model_reasoning_effort=high", "--approve-for-me",
        "--skip-git-repo-check", "--json", "-C", str(tmp_path), "-",
    ]
    assert CodexAdapter(executable="codex")._exec_args(tmp_path, _job())[2] == "gpt-6.1-terra"


class FakeProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode

    async def communicate(self, input=None):
        return self.stdout, b""


def test_cursor_and_antigravity_list_models_without_chat(monkeypatch):
    calls = []
    listing = {
        "status": b"Logged in as someone",
        "models": b"Available models\n\nauto - Auto (current, default)\ncomposer-2.5 - Composer 2.5\n",
    }

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess(listing[args[1]])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = CursorAdapter(executable="cursor-agent")
    assert asyncio.run(adapter.list_models()) == [("auto", "Auto"), ("composer-2.5", "Composer 2.5")]
    assert asyncio.run(adapter.list_models())[0] == ("auto", "Auto")
    assert calls == [("cursor-agent", "models")]  # cached; no -p chat run

    calls.clear()
    listing["models"] = b"Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    agy = AntigravityAdapter(executable="agy")
    assert asyncio.run(agy.login_status()) == "ok"
    assert asyncio.run(agy.list_models()) == [("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)")]
    assert calls == [("agy", "models")]


def test_parse_model_listing_skips_junk():
    text = "Available models\n-bad - nope\nok-id - Fine\nok-id - dup\n\u200bgpt-x\n"
    assert parse_model_listing(text) == [("ok-id", "Fine"), ("gpt-x", "gpt-x")]


def test_invalid_job_model_fails_without_exec(monkeypatch, tmp_path):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess(b'{"loggedIn": true}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(
        ClaudeAdapter(executable="claude").run(_job("--evil"), tmp_path, tmp_path / "log", 1)
    )
    assert result.status == "failed" and calls == [("claude", "auth", "status")]


def test_shortlist_models_keeps_default_and_caps():
    models = [("auto", "Auto")]
    for family in ("gpt-5.3-codex", "composer-2.5", "claude-opus-5-thinking", "grok-4.7"):
        for suffix in ("-low", "", "-high", "-xhigh", "-high-fast", "-low-fast"):
            mid = family + suffix
            models.append((mid, mid))
    # pad with extra unique families so the list exceeds the cap
    models.extend((f"extra-{i}", f"Extra {i}") for i in range(40))
    short = shortlist_models("cursor", models, limit=24)
    ids = [item[0] for item in short]
    assert ids[0] == "auto"
    assert "gpt-5.3-codex" in ids
    assert "composer-2.5" in ids
    assert all(not mid.endswith("-fast") or mid == "auto" for mid in ids)
    assert len(short) <= 24
    # a small catalog is left untouched
    tiny = [("auto", "Auto"), ("composer-2.5", "Composer 2.5")]
    assert shortlist_models("cursor", tiny) == tiny

