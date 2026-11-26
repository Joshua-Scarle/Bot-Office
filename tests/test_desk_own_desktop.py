"""remote-desktop: desk uses the bot's own desktop inside a turn; the agent and owner desktop APIs."""
import subprocess

import pytest
from teamkit import MASTER, Desk, run_job
from test_bot_desktops import FakePodman, _ready

from botoffice import bot_desktops as bd
from botoffice import desk

TURN = {"BOT_OFFICE_JOB_ID": "j1", "BOT_OFFICE_AGENT_TOKEN": "agent-token", "BOT_OFFICE_SLOT": "desktop-1"}


def answer(status, body):
    calls = []

    def transport(url, payload, token):
        calls.append((url, token))
        return status, body
    return transport, calls


def test_turn_uses_its_own_desktop():
    transport, calls = answer(200, {"name": "desktop-12", "owner_controlling": False})
    assert desk.pick_desktop(None, TURN, transport) == ("desktop-12", False)
    assert calls == [("http://127.0.0.1:8787/v1/agent/desktop", "agent-token")]
    assert desk.pick_desktop("desktop-12", TURN, transport) == ("desktop-12", False)


def test_leased_pool_slot_still_works_without_asking():
    transport, calls = answer(500, {})
    assert desk.pick_desktop("desktop-1", TURN, transport) == ("desktop-1", False)
    assert calls == []


def test_another_desktop_is_refused():
    transport, _ = answer(200, {"name": "desktop-12"})
    with pytest.raises(desk.DeskError, match="not yours"):
        desk.pick_desktop("desktop-13", TURN, transport)
    with pytest.raises(desk.DeskError, match="not yours"):
        desk.pick_desktop("desktop-2", TURN, transport)


def test_pool_mode_falls_back_to_the_leased_slot():
    transport, _ = answer(409, {"detail": "per-bot desktops are off"})
    assert desk.pick_desktop(None, TURN, transport) == ("desktop-1", False)
    no_lease = {k: v for k, v in TURN.items() if k != "BOT_OFFICE_SLOT"}
    with pytest.raises(desk.DeskError, match="No desktop is free"):
        desk.pick_desktop(None, no_lease, transport)


def test_errors_and_bad_names_surface():
    transport, _ = answer(409, {"detail": "all 4 desktops are in use; try again later"})
    with pytest.raises(desk.DeskError, match="all 4 desktops"):
        desk.pick_desktop(None, TURN, transport)
    transport, _ = answer(200, {"name": "desktop-1; rm -rf /"})
    with pytest.raises(desk.DeskError, match="invalid desktop name"):
        desk.pick_desktop(None, TURN, transport)


def test_operator_shell_keeps_the_old_rules():
    transport, calls = answer(500, {})
    assert desk.pick_desktop("desktop-2", {}, transport) == ("desktop-2", False)
    assert calls == []


def test_owner_control_blocks_input_but_not_screenshot(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(desk, "pick_desktop", lambda slot, env: ("desktop-12", True))
    ran = []
    monkeypatch.setattr(desk, "_run", lambda argv: ran.append(argv))
    for command in (["click", "1", "2"], ["type", "x"], ["key", "a"]):
        assert desk.main(command) == 1
        assert "owner is controlling" in capsys.readouterr().err
    assert ran == []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desk.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert desk.main(["screenshot", "s.png"]) == 0
    assert ran and ran[0][:3] == ["podman", "exec", "desktop-12"]


def test_desk_open_on_own_desktop_waits_for_the_page(monkeypatch, capsys):
    monkeypatch.setattr(desk, "pick_desktop", lambda slot, env: ("desktop-12", False))
    ran = []
    monkeypatch.setattr(desk, "_run", lambda argv: ran.append(argv))
    monkeypatch.setattr(desk, "browser_title", lambda slot: "Old page")
    waited = []
    monkeypatch.setattr(desk, "wait_for_browser", lambda slot, before: waited.append(before) or True)
    assert desk.main(["open", "https://example.com"]) == 0
    assert ran == [["podman", "exec", "-d", "desktop-12", "env", "DISPLAY=:1", "epiphany", "https://example.com"]]
    assert waited == ["Old page"]
    assert "the page has loaded" in capsys.readouterr().out
    assert desk.main(["open", "file:///etc/passwd"]) == 1


def test_wait_for_browser_needs_a_new_non_blank_title():
    titles = iter([None, "", "Blank page", "Old page", "Example Domain"])
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        title = next(titles)
        return subprocess.CompletedProcess(argv, 1 if title is None else 0, stdout=(title or "") + "\n")
    assert desk.wait_for_browser("desktop-12", "Old page", timeout=5, run=run, sleep=lambda s: None)
    assert len(seen) == 5 and "getwindowname" in seen[0]
    never = lambda a, **k: subprocess.CompletedProcess(a, 0, stdout="Blank page\n")  # noqa: E731
    assert not desk.wait_for_browser("desktop-12", None, timeout=0, run=never, sleep=lambda s: None)


# ---------------------------------------------------------------- API
def fake_manager(d, tmp_path):
    manager = d.app.state.bot_desktops
    manager.runner = FakePodman()
    manager.ready = _ready
    manager.registry = bd.Registry(tmp_path / "desktops.json")
    return manager


def test_agent_desktop_is_the_tokens_own_bot(settings, tmp_path):
    with Desk(settings) as d:
        manager = fake_manager(d, tmp_path)
        a, b = d.bot("A"), d.bot("B")
        _, ha = run_job(d.store, a)
        first = d.client.post("/v1/agent/desktop", headers=ha)
        assert first.status_code == 200 and first.json() == {"name": "desktop-10", "owner_controlling": False}
        assert manager.registry.bot("desktop-10") == str(a.id)
        # the master token is not an agent token; the endpoint takes no bot argument at all
        assert d.client.post("/v1/agent/desktop", headers=MASTER).status_code == 401
        assert d.client.post("/v1/agent/desktop").status_code == 401
        # the owner opens B's desktop from the UI
        opened = d.client.post(f"/v1/bots/{b.id}/desktop", headers=MASTER)
        assert opened.json() == {"name": "desktop-11"}
        assert d.client.post(f"/v1/bots/{b.id}/desktop").status_code == 401
        with manager.viewing("desktop-10", "control"):
            assert d.client.post("/v1/agent/desktop", headers=ha).json()["owner_controlling"] is True


def test_owner_start_listing_and_hand_back(settings, tmp_path, monkeypatch):
    from botoffice import vnc
    with Desk(settings) as d:
        manager = fake_manager(d, tmp_path)
        a = d.bot("Alpha")
        assert d.client.post(f"/v1/bots/{a.id}/desktop", headers=MASTER).status_code == 200
        manager.runner.containers["desktop-10"]["running"] = False
        assert d.client.post("/v1/desktops/desktop-10/start", headers=MASTER).json() == {"name": "desktop-10"}
        assert manager.runner.calls[-1] == ["start", "desktop-10"]
        assert d.client.post("/v1/desktops/desktop-1/start", headers=MASTER).status_code == 404
        assert d.client.post("/v1/desktops/desktop-10/start").status_code == 401

        async def desktops():
            return [{"name": "desktop-10", "port": 5910, "status": "running"},
                    {"name": "desktop-1", "port": 5901, "status": "running"},
                    {"name": "desktop-3", "port": 5903, "status": "running"}]
        monkeypatch.setattr(vnc, "desktops", desktops)
        listed = {item["name"]: item for item in d.client.get("/v1/desktops", headers=MASTER).json()}
        assert listed["desktop-10"]["kind"] == "per-bot" and listed["desktop-10"]["bot"] == "Alpha"
        assert listed["desktop-1"]["kind"] == "pool" and listed["desktop-3"]["kind"] == "other"
        assert listed["desktop-10"]["owner_controlling"] is False
        handed = d.client.post("/v1/desktops/desktop-10/hand-back", headers=MASTER)
        assert handed.status_code == 200 and handed.json()["bot_id"] == str(a.id)


def test_pool_mode_agent_endpoint_says_off(tmp_path):
    from botoffice.config import Settings
    settings = Settings(data_dir=tmp_path / "data", token="test-token", job_timeout=2, desktop_mode="pool")
    with Desk(settings) as d:
        a = d.bot("A")
        _, ha = run_job(d.store, a)
        r = d.client.post("/v1/agent/desktop", headers=ha)
        assert r.status_code == 409 and r.json()["detail"] == "per-bot desktops are off"
