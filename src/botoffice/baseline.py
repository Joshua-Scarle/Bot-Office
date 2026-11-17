"""The baseline charter every bot gets on every engine, before its own profile and the owner's rules.

The default ships with the package (prompts/baseline.md). The owner can replace it in Settings; the
override lives in the controller's database (meta table), which bots cannot reach, and "Reset to
default" removes it. Bots see the effective text as BASELINE.md in each job directory and inline
in every fresh-session prompt; edits they make there are not kept.
"""
from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from . import isolation, safefs
from .store import JobStore

META_KEY = "baseline_instructions"
SNAPSHOT = "baseline.sha256"
MAX_CHARS = 16_000  # it is inlined in every fresh prompt (argv-sized on some CLIs)
DEFAULT_PATH = Path(__file__).with_name("prompts") / "baseline.md"
CHANGED_NOTICE = ("The baseline instructions every bot follows changed; re-read BASELINE.md in the current "
                  "directory now (your profile and the owner's standing rules still override it).")


@cache
def default_text() -> str:
    return DEFAULT_PATH.read_text(encoding="utf-8")


def override(store: JobStore) -> str | None:
    return store.get_meta(META_KEY)


def effective(store: JobStore) -> str:
    custom = override(store)
    return default_text() if custom is None else custom


def set_override(store: JobStore, text: str) -> None:
    if len(text) > MAX_CHARS:
        raise ValueError(f"baseline instructions are limited to {MAX_CHARS} characters")
    store.set_meta(META_KEY, text)


def reset(store: JobStore) -> None:
    store.delete_meta(META_KEY)


def state(store: JobStore) -> dict[str, object]:
    custom = override(store)
    return {"text": default_text() if custom is None else custom, "default": default_text(),
            "customized": custom is not None, "max_chars": MAX_CHARS}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def resume_note(text: str, bot_dir: Path) -> str:
    """One line for a resumed session when the baseline differs from the one its last completed turn
    saw (also once for sessions that started before baselines existed)."""
    try:
        previous = safefs.read_bytes(isolation.state_dir(bot_dir) / SNAPSHOT).decode()
    except (OSError, UnicodeError):
        previous = None
    return "" if previous == _sha(text) else f"\n{CHANGED_NOTICE}\n"


def save_snapshot(text: str, bot_dir: Path) -> None:
    safefs.write_text(isolation.state_dir(bot_dir) / SNAPSHOT, _sha(text), isolation.file_mode(),
                      dir_mode=isolation.dir_mode())
