"""Slash commands (0.11.14): catalog per engine, classification, auth refusal, the API and what a turn runs.

The CLI outputs in tests/fixtures/slash are trimmed copies of what the real CLIs printed on the VPS
(Claude Code 2.1.319 init event, `agy -p /help` 1.5.12, OpenCode 1.19.5 GET /command)."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import slash
from botoffice import store as store_mod
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.providers.opencode import OpenCodeAdapter

H = {"Authorization": "Bearer test-token"}
FIX = Path(__file__).parent / "fixtures" / "slash"


class CliFake(FakeAdapter):
    """A FakeAdapter that answers the catalog's probes like the real CLI would."""

    def __init__(self, engine: str, version: str = "1.0", output: bytes = b"", code: int = 0):
        super().__init__()
        self.name = engine
        self.executable = f"/usr/bin/{engine}"
        self.version, self.output, self.code = version, output, code
        self.calls: list[tuple[str, ...]] = []
        self.jobs: list = []

    async def capture_bytes(self, args, timeout=15, cwd=None):
        self.calls.append(tuple(args))
        if tuple(args) == ("--version",):
            return 0, self.version.encode(), b""
        return self.code, self.output, b""

    async def run(self, job, workspace, log_path, timeout):
        self.jobs.append((job, workspace))
        (workspace / "reply.txt").write_text("done\n")
        return await super().run(job, workspace, log_path, timeout)


def make_bot(client, **overrides):
    body = {"name": "helper", "instructions": "be helpful"} | overrides
    response = client.post("/v1/bots", json=body, headers=H)
    assert response.status_code == 201, response.text
    return response.json()


def by_name(data):
    return {c["name"]: c for c in data["commands"]}


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := predicate():
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


# ---------------------------------------------------------------- parsers and classification
def test_claude_init_is_parsed_and_classified():
    names, terminal = slash.parse_claude_init((FIX / "claude-init.jsonl").read_text())
    assert "compact" in names and terminal == ["doctor", "color", "focus", "reload-plugins"]
    out = {e["name"]: e for e in slash.build_claude(names, terminal, [])}
    assert out["compact"]["action"] == "run" and out["compact"]["source"] == "cli"
    assert out["context"]["action"] == "run" and out["usage"]["action"] == "run"
    assert out["model"]["action"] == "map" and out["model"]["target"] == "model"
    assert out["effort"]["target"] == "effort"
    assert out["clear"]["action"] == "map" and out["clear"]["target"] == "new" and out["clear"]["confirm"] is True
    assert out["doctor"]["action"] == "grey" and "terminal" in out["doctor"]["reason"]  # the CLI says terminal-only
    assert out["fast"]["action"] == "grey" and out["config"]["action"] == "grey"
    assert out["code-review:code-review"]["kind"] == "plugin"
    assert out["deep-research"]["kind"] == "skill"
    assert "login" not in out and "logout" not in out and "__remote-workflow" not in out


def test_claude_custom_command_and_skill_files_give_descriptions(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "commands" / "team").mkdir(parents=True)
    (home / ".claude" / "commands" / "my-team-cmd.md").write_text("---\ndescription: Do the team thing\nargument-hint: [topic]\n---\nBody $ARGUMENTS\n")
    (home / ".claude" / "commands" / "team" / "deep.md").write_text("# Deep dive\nbody")
    project = tmp_path / "proj"
    (project / ".claude" / "commands").mkdir(parents=True)
    (project / ".claude" / "commands" / "project-cmd.md").write_text("Project command without frontmatter\n")
    (home / ".claude" / "skills" / "design").mkdir(parents=True)
    (home / ".claude" / "skills" / "design" / "SKILL.md").write_text("---\nname: design\ndescription: Make a design\n---\n")
    items, seen = slash.custom_files("claude", home, project)
    assert {n for n, _, _ in items} == {"my-team-cmd", "team:deep", "project-cmd", "design"}
    names, terminal = slash.parse_claude_init((FIX / "claude-init.jsonl").read_text())
    out = {e["name"]: e for e in slash.build_claude(names, terminal, items)}
    assert out["my-team-cmd"]["description"] == "Do the team thing [topic]" and out["my-team-cmd"]["source"] == "custom"
    assert out["project-cmd"]["description"] == "Project command without frontmatter"
    assert out["design"]["description"] == "Make a design" and out["design"]["source"] == "skill"
    # the signature moves when a file is edited, so the cached list is dropped
    before = slash.file_signature(seen)
    (project / ".claude" / "commands" / "project-cmd.md").write_text("Changed and longer\n")
    assert slash.file_signature(seen) != before


def test_antigravity_help_is_parsed():
    parsed = slash.parse_agy_help((FIX / "agy-help.txt").read_text())
    assert ("usage", "View model quota usage") in parsed and ("config", "Open settings panel") in parsed
    out = {e["name"]: e for e in slash.build_agy(parsed)}
    assert out["usage"]["action"] == "run" and out["skills"]["action"] == "run" and out["help"]["action"] == "run"
    assert out["model"]["action"] == "map" and out["effort"]["action"] == "map"
    assert out["hooks"]["action"] == "grey" and out["config"]["action"] == "grey"
    assert "login" not in out


def test_opencode_command_list_becomes_command_actions():
    commands = json.loads((FIX / "opencode-command.json").read_text())
    out = {e["name"]: e for e in slash.set_actions("opencode", slash.build_opencode(commands, []))}
    assert out["init"]["action"] == "command" and out["init"]["source"] == "cli" and "$ARGUMENTS" in out["init"]["description"]
    assert out["xlsx"]["kind"] == "skill" and out["review"]["action"] == "command"
    assert out["new"]["action"] == "map" and out["new"]["target"] == "new"      # documented built-in
    assert out["models"]["target"] == "model" and out["compact"]["action"] == "grey"
    assert "connect" not in out  # adds a provider (credentials)


def test_codex_and_cursor_use_documented_builtins_and_custom_files(tmp_path):
    home = tmp_path / "home"
    (home / ".codex" / "prompts").mkdir(parents=True)
    (home / ".codex" / "prompts" / "ship.md").write_text("---\ndescription: Ship it\nargument-hint: BRANCH\n---\nShip $1 now. All: $ARGUMENTS. Cost $$5.\n")
    (home / ".cursor" / "commands").mkdir(parents=True)
    (home / ".cursor" / "commands" / "tidy.md").write_text("Tidy the repo")
    items, _ = slash.custom_files("codex", home, None)
    codex = {e["name"]: e for e in slash.build_expand("codex", slash.CODEX_BUILTINS, items)}
    assert codex["ship"]["action"] == "expand" and codex["ship"]["source"] == "custom" and codex["ship"]["file"]
    assert codex["compact"]["action"] == "grey" and "compaction" in codex["compact"]["reason"]
    assert codex["model"]["action"] == "map" and codex["new"]["confirm"] is True and codex["effort"]["target"] == "effort"
    assert codex["status"]["action"] == "grey" and codex["status"]["source"] == "documented"
    assert "logout" not in codex
    items, _ = slash.custom_files("cursor", home, None)
    cursor = {e["name"]: e for e in slash.build_expand("cursor", slash.CURSOR_BUILTINS, items)}
    assert cursor["tidy"]["action"] == "expand" and "effort" not in cursor and cursor["model"]["action"] == "map"
    assert slash.expand_file(codex["ship"]["file"], "main fast") == "Ship main now. All: main fast. Cost $5."


def test_symlinked_command_and_skill_files_are_never_followed(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("---\ndescription: TOP SECRET\n---\n")
    (home / ".claude" / "commands" / "peek.md").symlink_to(secret)
    (home / ".claude" / "skills" / "s").mkdir(parents=True)
    (home / ".claude" / "skills" / "s" / "SKILL.md").symlink_to(secret)
    (home / ".claude" / "commands" / "real.md").write_text("real one")
    items, _ = slash.custom_files("claude", home, None)
    assert [n for n, _, _ in items] == ["real"]
    assert slash._read(home / ".claude" / "commands" / "peek.md") == ""


def test_a_symlinked_command_directory_is_not_followed(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere" / "commands"
    elsewhere.mkdir(parents=True)
    (elsewhere / "leak.md").write_text("leaked")
    project.mkdir()
    (project / ".cursor").symlink_to(elsewhere.parent)
    (home / ".cursor" / "commands").mkdir(parents=True)
    (home / ".cursor" / "commands" / "mine.md").write_text("mine")
    items, _ = slash.custom_files("cursor", home, project)
    assert [n for n, _, _ in items] == ["mine"]


def test_template_without_placeholders_appends_what_was_typed():
    assert slash.render_template("Review the diff", "src/") == "Review the diff\n\nsrc/"
    assert slash.render_template("Review $ARGUMENTS", "") == "Review"
    assert slash.render_template("$2 then $1 and $9", "a b") == "b then a and"


@pytest.mark.parametrize("name", ["login", "logout", "auth", "oauth-refresh", "mcp:auth", "setup-token", "connect", "sign-in", "relogin", "reauthenticate", "authorize", "api-key", "credentials", "account", "log_in", "sign_out", "log.in"])
def test_auth_names_are_recognised(name):
    assert slash.is_auth(name) and slash.is_auth("/" + name)


@pytest.mark.parametrize("name", ["compact", "author", "authoring-guide", "model", "review"])
def test_other_names_are_not_auth(name):
    assert not slash.is_auth(name)


def test_split_line_and_encoding():
    assert slash.split_line("/review  main branch") == ("review", "main branch")
    assert slash.split_line("/ship") == ("ship", "")
    assert slash.split_line("/etc/passwd is a file") == ("etc/passwd", "is a file")  # the page sends it on as text unless the name is listed
    assert slash.split_line("not a command") is None and slash.split_line("/") is None
    assert slash.decode_slash(slash.encode_slash("a", "b c", "raw")) == {"name": "a", "args": "b c", "mode": "raw"}
    assert slash.decode_slash("{bad") is None and slash.decode_slash(None) is None
    assert slash.decode_slash(json.dumps({"name": "a", "mode": "evil"})) is None


# ---------------------------------------------------------------- the catalog service: probe, cache, fallback
def _catalog(tmp_path, providers):
    return slash.SlashCatalog(providers, tmp_path / "data", home=tmp_path / "home")


def test_catalog_probes_once_per_version_and_keeps_the_cache_between_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(slash, "VERSION_TTL", 0)  # the version is re-read every time here
    fake = CliFake("claude", "2.1.319", (FIX / "claude-init.jsonl").read_bytes())
    catalog = _catalog(tmp_path, {"claude": fake})
    first = asyncio.run(catalog.commands("claude"))
    assert first["source"] == "cli" and first["version"] == "2.1.319" and first["cached"] is False
    asyncio.run(catalog.commands("claude"))
    assert [c for c in fake.calls if c != ("--version",)] == [fake.calls[1]]  # one probe only
    assert asyncio.run(catalog.commands("claude"))["cached"] is True
    restarted = _catalog(tmp_path, {"claude": fake})
    assert asyncio.run(restarted.commands("claude"))["cached"] is True
    assert len([c for c in fake.calls if c != ("--version",)]) == 1
    fake.version = "2.1.320"  # a new CLI version asks again
    assert asyncio.run(restarted.commands("claude"))["cached"] is False
    assert len([c for c in fake.calls if c != ("--version",)]) == 2
    assert oct((tmp_path / "data" / "slash-cache.json").stat().st_mode & 0o777) == "0o600"


def test_catalog_falls_back_and_says_so_when_the_cli_cannot_be_asked(tmp_path):
    fake = CliFake("claude", "2.1.319", b"not json at all")
    data = asyncio.run(_catalog(tmp_path, {"claude": fake}).commands("claude"))
    assert data["source"] == "fallback" and "built-in" in data["note"]
    assert {"context", "usage", "model", "clear"} <= set(by_name(data))
    assert all(c["source"] in ("fallback", "cli", "documented") for c in data["commands"])
    gone = FakeAdapter(enabled=False)
    gone.name = "claude"
    assert asyncio.run(_catalog(tmp_path, {"claude": gone}).commands("claude"))["source"] == "fallback"


def test_catalog_claude_agy_and_opencode_sources(tmp_path, monkeypatch):
    agy = CliFake("antigravity", "1.5.12", (FIX / "agy-help.txt").read_bytes())
    data = asyncio.run(_catalog(tmp_path, {"antigravity": agy}).commands("antigravity"))
    assert data["source"] == "cli" and by_name(data)["usage"]["action"] == "run" and agy.calls[-1] == ("-p", "/help")
    oc = CliFake("opencode", "1.19.5")

    async def probe(adapter, cwd):
        return json.loads((FIX / "opencode-command.json").read_text())
    monkeypatch.setattr(slash, "probe_opencode", probe)
    data = asyncio.run(_catalog(tmp_path, {"opencode": oc}).commands("opencode"))
    assert data["source"] == "cli" and by_name(data)["init"]["action"] == "command"
    codex = CliFake("codex", "0.167.0")
    data = asyncio.run(_catalog(tmp_path, {"codex": codex}).commands("codex"))
    assert "documented" in data["note"] and by_name(data)["status"]["source"] == "documented"
    assert not [c for c in codex.calls if c != ("--version",)]  # nothing to ask codex


def test_sorted_runnable_first_greyed_last():
    rows = [slash.entry("zz", source="cli", action="grey", reason="x"), slash.entry("b", source="cli"),
            slash.entry("a", source="cli", action="map", target="model"), slash.entry("c", source="cli")]
    assert [r["name"] for r in slash.sort_entries(rows)] == ["a", "b", "c", "zz"]


# ---------------------------------------------------------------- API
def _app(settings, tmp_path, engine="claude", output=None, **kw):
    fake = CliFake(engine, "9.9", output if output is not None else (FIX / "claude-init.jsonl").read_bytes())
    app = create_app(settings, {"codex": FakeAdapter(), engine: fake} if engine != "codex" else {"codex": fake})
    return app, fake


def test_api_lists_commands_without_internal_paths_and_requires_auth(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(slash.SlashCatalog, "_home", lambda self: tmp_path / "home")
    (tmp_path / "home" / ".codex" / "prompts").mkdir(parents=True)
    (tmp_path / "home" / ".codex" / "prompts" / "ship.md").write_text("Ship $ARGUMENTS")
    app, fake = _app(settings, tmp_path, "codex")
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="codex")
        assert client.get(f"/v1/bots/{bot['id']}/slash-commands").status_code == 401
        data = client.get(f"/v1/bots/{bot['id']}/slash-commands", headers=H).json()
        assert data["engine"] == "codex" and by_name(data)["ship"]["action"] == "expand"
        assert all("file" not in c for c in data["commands"])
        assert client.get("/v1/bots/00000000-0000-0000-0000-000000000000/slash-commands", headers=H).status_code == 404
        assert client.post(f"/v1/bots/{bot['id']}/slash", json={"name": "ship"}).status_code == 401


def test_api_run_queues_a_raw_slash_turn_and_the_scheduler_sends_the_bare_line(settings, tmp_path):
    app, fake = _app(settings, tmp_path, "claude")
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="claude")
        out = client.post(f"/v1/bots/{bot['id']}/slash", json={"name": "/compact", "args": "keep the plan"}, headers=H)
        assert out.status_code == 200, out.text
        data = out.json()
        assert data["action"] == "run" and data["message"]["text"] == "/compact keep the plan"
        assert json.loads(data["job"]["slash"]) == {"name": "compact", "args": "keep the plan", "mode": "raw"}
        wait_for(lambda: fake.jobs)
        job, _ = fake.jobs[0]
        assert job.prompt == "/compact keep the plan" and job.resume_prompt == "/compact keep the plan"
        got = client.get(f"/v1/jobs/{job.id}", headers=H).json()["job"]
        assert got["slash"] == data["job"]["slash"]


def test_api_command_mode_for_opencode_and_expand_for_codex(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(slash.SlashCatalog, "_home", lambda self: tmp_path / "home")
    (tmp_path / "home" / ".codex" / "prompts").mkdir(parents=True)
    (tmp_path / "home" / ".codex" / "prompts" / "ship.md").write_text("Ship $1 please")
    app, fake = _app(settings, tmp_path, "codex")
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="codex")
        out = client.post(f"/v1/bots/{bot['id']}/slash", json={"name": "ship", "args": "main"}, headers=H).json()
        assert json.loads(out["job"]["slash"])["mode"] == "expand"
        assert out["message"]["text"] == "/ship main"
        wait_for(lambda: fake.jobs)
        job, workspace = fake.jobs[0]
        assert "Ship main please" in job.prompt and "Current message" in job.prompt  # inside the normal prompt
        assert not job.prompt.lstrip().startswith("/")


def test_opencode_adapter_runs_the_command_with_its_arguments(tmp_path):
    from datetime import datetime
    from uuid import uuid4

    from botoffice.domain import Job, JobStatus
    adapter = OpenCodeAdapter()
    base = dict(id=uuid4(), status=JobStatus.RUNNING, created_at=datetime.now(), provider="opencode", cli_access="full")
    job = Job(prompt="main", slash=slash.encode_slash("review", "main", "command"), **base)
    argv = adapter._exec_args(tmp_path, job)
    assert argv[argv.index("--command") + 1] == "review" and argv[-2:] == ["--", "main"]
    bare = Job(prompt="", slash=slash.encode_slash("init", "", "command"), **base)
    assert adapter._exec_args(tmp_path, bare)[-2:] == ["--command", "init"]
    plain = Job(prompt="hello", **base)
    assert "--command" not in adapter._exec_args(tmp_path, plain) and adapter._exec_args(tmp_path, plain)[-2:] == ["--", "hello"]


def test_api_refuses_auth_grey_unknown_and_gates_the_destructive_ones(settings, tmp_path):
    app, fake = _app(settings, tmp_path, "claude")
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="claude")
        url = f"/v1/bots/{bot['id']}/slash"
        assert client.post(url, json={"name": "login"}, headers=H).status_code == 403
        assert client.post(url, json={"name": "mcp:auth"}, headers=H).status_code == 403
        assert "login" not in by_name(client.get(url.replace("/slash", "/slash-commands"), headers=H).json())
        grey = client.post(url, json={"name": "fast"}, headers=H)
        assert grey.status_code == 422 and "agent" in grey.json()["detail"]
        assert client.post(url, json={"name": "doctor"}, headers=H).status_code == 422
        assert client.post(url, json={"name": "nope"}, headers=H).status_code == 404
        assert client.post(url, json={"name": "bad name"}, headers=H).status_code == 422
        assert client.post(url, json={"name": "x", "args": "a" * 30000}, headers=H).status_code == 422
        # mapped controls
        model = client.post(url, json={"name": "model", "args": "claude-opus-5-5"}, headers=H).json()
        assert model == {"action": "map", "target": "model", "args": "claude-opus-5-5"}
        assert client.post(url, json={"name": "effort"}, headers=H).json()["target"] == "effort"
        assert not fake.jobs  # none of that started a turn


def test_new_session_needs_confirm_and_keeps_history(settings, tmp_path):
    app, fake = _app(settings, tmp_path, "claude")
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="claude")
        bot_dir = settings.data_dir / "bots" / bot["id"]
        (bot_dir / "claude-session").write_text("sess-1")
        history = client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json()
        url = f"/v1/bots/{bot['id']}/slash"
        refused = client.post(url, json={"name": "clear"}, headers=H)
        assert refused.status_code == 409 and refused.json()["detail"]["confirm_required"] is True
        assert (bot_dir / "claude-session").exists()
        # /new is an alias of /clear for the same control; both need the confirm
        assert client.post(url, json={"name": "new"}, headers=H).status_code in (404, 409)
        ok = client.post(url, json={"name": "clear", "confirm": True}, headers=H)
        assert ok.status_code == 200 and ok.json()["target"] == "new"
        assert client.get(f"/v1/bots/{bot['id']}/messages", headers=H).json() == history


def test_slash_turn_is_queued_behind_a_running_turn_never_steered(settings, tmp_path):
    fake = CliFake("claude", "9.9", (FIX / "claude-init.jsonl").read_bytes())
    fake.delay = 0.6
    app = create_app(settings, {"codex": FakeAdapter(), "claude": fake})
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="claude")
        client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "hello"}, headers=H)
        wait_for(lambda: client.get(f"/v1/bots/{bot['id']}", headers=H).json().get("activity") == "running")
        out = client.post(f"/v1/bots/{bot['id']}/slash", json={"name": "context"}, headers=H).json()
        assert out["delivery"] == "queue" and out["job"]["status"] == "queued" and out["delivery_note"]
        wait_for(lambda: len(fake.jobs) == 2, timeout=8)
        assert fake.jobs[1][0].prompt == "/context"


def test_a_message_sent_while_a_slash_turn_runs_is_queued_not_steered(settings, tmp_path):
    fake = CliFake("claude", "9.9", (FIX / "claude-init.jsonl").read_bytes())
    fake.delay = 0.8
    app = create_app(settings, {"codex": FakeAdapter(), "claude": fake})
    with TestClient(app) as client:
        bot = make_bot(client, default_provider="claude")
        client.post(f"/v1/bots/{bot['id']}/slash", json={"name": "context"}, headers=H)
        wait_for(lambda: client.get(f"/v1/bots/{bot['id']}", headers=H).json().get("activity") == "running")
        out = client.post(f"/v1/bots/{bot['id']}/messages", json={"text": "and also this"}, headers=H).json()
        assert out["delivery"] == "queue" and out["delivery_note"] == store_mod.SLASH_BUSY_NOTE
        wait_for(lambda: len(fake.jobs) == 2, timeout=10)
        assert fake.jobs[0][0].prompt == "/context" and "and also this" in fake.jobs[1][0].prompt


def test_engine_without_a_cli_lists_nothing(tmp_path):
    data = asyncio.run(_catalog(tmp_path, {}).commands("grok"))
    assert data["commands"] == [] and data["source"] == "none"


def test_a_failed_refresh_keeps_the_stale_list_and_other_versions_stay_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(slash, "VERSION_TTL", 0)
    fake = CliFake("claude", "2.1.319", (FIX / "claude-init.jsonl").read_bytes())
    catalog = _catalog(tmp_path, {"claude": fake})
    assert asyncio.run(catalog.commands("claude"))["source"] == "cli"
    fake.output = b"garbage"  # the probe now yields nothing
    again = asyncio.run(catalog.commands("claude", refresh=True))
    assert again["source"] == "cli" and again["commands"]
    old_key = [k for k in catalog._cache][0]
    fake.version = "2.1.320"
    fake.output = (FIX / "claude-init.jsonl").read_bytes()
    catalog._failed.clear()
    asyncio.run(catalog.commands("claude"))
    assert old_key in catalog._cache and len(catalog._cache) == 2


def test_template_arguments_follow_shell_quoting():
    assert slash.render_template("first $1 second $2", '"a b" c') == "first a b second c"


def test_a_damaged_cache_entry_is_dropped_and_asked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(slash, "VERSION_TTL", 0)
    fake = CliFake("claude", "2.1.319", (FIX / "claude-init.jsonl").read_bytes())
    catalog = _catalog(tmp_path, {"claude": fake})
    asyncio.run(catalog.commands("claude"))
    for key in catalog._cache:
        catalog._cache[key] = {"probe": {"oops": 1}}
    data = asyncio.run(catalog.commands("claude"))
    assert data["commands"] and data["cached"] is False  # the damaged entry was dropped and asked again


def test_arguments_are_kept_when_the_body_only_escapes_a_dollar():
    assert slash.render_template("Cost $$ARGUMENTS", "5") == "Cost $ARGUMENTS\n\n5"
    assert slash.render_template("Do $ARGUMENTS", "x") == "Do x"


def test_a_custom_file_cannot_take_over_a_control_of_bot_office(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    for name in ("clear", "model", "effort"):
        (home / ".claude" / "commands" / f"{name}.md").write_text("do something else")
    items, _ = slash.custom_files("claude", home, None)
    got = {e["name"]: e for e in slash.set_actions("claude", slash.build_claude(["clear", "model", "effort", "mine"], [], items))}
    assert got["clear"]["action"] == "map" and got["model"]["action"] == "map" and got["effort"]["action"] == "map"


def test_codex_and_cursor_report_their_list_as_documented(tmp_path):
    for engine in ("codex", "cursor"):
        data = asyncio.run(_catalog(tmp_path, {engine: CliFake(engine)}).commands(engine))
        assert data["source"] == "documented" and data["commands"]
