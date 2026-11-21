from pathlib import Path

import agentdesk
import botoffice
from botoffice.config import Settings, first_env
from botoffice.desk import resolve_slot


def test_agentdesk_is_a_compat_alias_for_botoffice():
    assert agentdesk.__version__ == botoffice.__version__ == "0.12.10"


def test_first_env_prefers_bot_office_names(monkeypatch):
    monkeypatch.setenv("AGENTDESK_PROVIDERS", "codex")
    monkeypatch.setenv("BOT_OFFICE_PROVIDERS", "claude")
    assert first_env("BOT_OFFICE_PROVIDERS", "AGENTDESK_PROVIDERS") == "claude"
    settings = Settings.from_env()
    assert settings.enabled_providers == frozenset({"claude"})


def test_from_env_falls_back_to_legacy_agentdesk_names(monkeypatch):
    monkeypatch.delenv("BOT_OFFICE_PROVIDERS", raising=False)
    monkeypatch.delenv("BOT_OFFICE_DATA_DIR", raising=False)
    monkeypatch.delenv("BOT_OFFICE_TOKEN", raising=False)
    monkeypatch.delenv("BOT_OFFICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENTDESK_PROVIDERS", "cursor")
    monkeypatch.setenv("AGENTDESK_DATA_DIR", "/tmp/legacy-data")
    monkeypatch.setenv("AGENTDESK_TOKEN", "legacy-token")
    settings = Settings.from_env()
    assert settings.enabled_providers == frozenset({"cursor"})
    assert settings.data_dir == Path("/tmp/legacy-data")
    assert settings.token == "legacy-token"


def test_resolve_slot_accepts_legacy_and_new_env():
    assert resolve_slot(None, {"BOT_OFFICE_SLOT": "desktop-1"}) == "desktop-1"
    assert resolve_slot(None, {"AGENTDESK_SLOT": "desktop-2"}) == "desktop-2"
    assert resolve_slot(None, {"BOT_OFFICE_SLOT": "desktop-1", "AGENTDESK_SLOT": "desktop-2"}) == "desktop-1"
