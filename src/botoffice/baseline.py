"""The two owner-side layers every bot gets on every engine, before its own profile and the owner's rules.

- ``RULES`` ("Rules for all bots", 0.11.10): short, user-facing rules for agency and competence. Shown
  first in Settings and meant to be edited.
- ``SYSTEM`` ("System prompt"): how Bot-Office works (turns, memory files, bot-msg / bot-task / desk,
  delegation, trust). Prebuilt; editable under Settings > Advanced. It keeps the 0.11.9 storage
  (``meta`` key ``baseline_instructions``, API ``/v1/baseline``), so a 0.11.9 override carries over.

Defaults ship with the package (prompts/). Owner overrides live in the controller's database, which
bots cannot reach; "Reset to default" removes them. Bots see the effective text as files in each job
directory and inline in every fresh-session prompt; edits they make there are not kept.

Also here: the owner's auto-continue setting (0.11.10), a JSON document in ``meta``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from . import continuation, isolation, safefs
from .store import JobStore

PROMPTS = Path(__file__).with_name("prompts")


@dataclass(frozen=True)
class Layer:
    key: str          # API name
    title: str
    meta_key: str
    default_file: str
    file_name: str    # written into each job directory
    snapshot: str     # sha of the text the bot's last completed turn saw
    max_chars: int

    def default_text(self) -> str:
        return _read_default(self.default_file)

    def override(self, store: JobStore) -> str | None:
        return store.get_meta(self.meta_key)

    def effective(self, store: JobStore) -> str:
        custom = self.override(store)
        return self.default_text() if custom is None else custom

    def set_override(self, store: JobStore, text: str) -> None:
        if len(text) > self.max_chars:
            raise ValueError(f"{self.title} is limited to {self.max_chars} characters")
        store.set_meta(self.meta_key, text)

    def reset(self, store: JobStore) -> None:
        store.delete_meta(self.meta_key)

    def state(self, store: JobStore) -> dict[str, object]:
        custom = self.override(store)
        return {"text": self.default_text() if custom is None else custom, "default": self.default_text(),
                "customized": custom is not None, "max_chars": self.max_chars, "title": self.title}

    def changed_notice(self) -> str:
        return (f"The {self.title.lower()} every bot follows changed; re-read {self.file_name} in the current "
                "directory now.")

    def resume_note(self, text: str, bot_dir: Path) -> str:
        """One line for a resumed session when the layer differs from the one its last completed turn
        saw (also once for sessions that started before the layer existed)."""
        try:
            previous = safefs.read_bytes(isolation.state_dir(bot_dir) / self.snapshot).decode()
        except (OSError, UnicodeError):
            previous = None
        return "" if previous == _sha(text) else f"\n{self.changed_notice()}\n"

    def save_snapshot(self, text: str, bot_dir: Path) -> None:
        safefs.write_text(isolation.state_dir(bot_dir) / self.snapshot, _sha(text), isolation.file_mode(),
                          dir_mode=isolation.dir_mode())


@cache
def _read_default(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


SYSTEM = Layer("system", "System prompt", "baseline_instructions", "system.md", "SYSTEM-PROMPT.md",
               "baseline.sha256", 16_000)
RULES = Layer("rules", "Rules for all bots", "rules_for_all_bots", "rules-for-all-bots.md",
              "RULES-FOR-ALL-BOTS.md", "all-bots-rules.sha256", 8_000)
LAYERS = (SYSTEM, RULES)  # prompt order

# 0.11.9 names (the system prompt is what /v1/baseline edits)
META_KEY, MAX_CHARS = SYSTEM.meta_key, SYSTEM.max_chars


def effective_layers(store: JobStore) -> dict[str, str]:
    return {layer.key: layer.effective(store) for layer in LAYERS}


# --- auto-continue (0.11.10): the settings are continuation.py's (one system for chat and tasks) ---

AUTO_CONTINUE_KEY = continuation.KEY
AUTO_CONTINUE_DEFAULT = {"enabled": True, "max": 10}
AUTO_CONTINUE_MAX = continuation.CHAIN_MAX_LIMIT


def auto_continue(store: JobStore) -> dict[str, object]:
    return continuation.get(store)


def set_auto_continue(store: JobStore, enabled: bool | None = None, maximum: int | None = None,
                      **more: object) -> dict[str, object]:
    """*more*: the other continuation settings by key (None for a cap means unlimited)."""
    if maximum is not None and not 1 <= maximum <= AUTO_CONTINUE_MAX:
        raise ValueError(f"the limit must be between 1 and {AUTO_CONTINUE_MAX}")
    changes = dict(more)
    if enabled is not None:
        changes["enabled"] = bool(enabled)
    if maximum is not None:
        changes["max"] = maximum
    return continuation.update(store, changes)
