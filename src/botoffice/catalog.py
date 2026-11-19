"""Engine and model catalog.

A bot is not an engine. Each bot has a changeable ``engine`` (which CLI runs its
turns) and ``model`` (a model id for that engine). This module holds the static
fallback catalog and helpers to parse CLI model listings.
"""

from __future__ import annotations

import re
from typing import Literal

ENGINES: tuple[str, ...] = ("codex", "claude", "cursor", "antigravity", "opencode")

ENGINE_LABELS: dict[str, str] = {
    "opencode": "OpenCode",
    "codex": "Codex",
    "claude": "Claude",
    "cursor": "Cursor",
    "antigravity": "Antigravity",
}

DEFAULT_MODELS: dict[str, str] = {
    "opencode": "opencode/big-pickle",
    "codex": "gpt-6.1-terra",
    "claude": "claude-opus-5-5",
    "cursor": "auto",
    "antigravity": "gemini-3.8-flash-high",
}

# (model id, human label). Used when a CLI cannot list models cheaply.
STATIC_MODELS: dict[str, tuple[tuple[str, str], ...]] = {
    "opencode": (("opencode/big-pickle", "opencode/big-pickle"), ("openai/gpt-5", "openai/gpt-5"), ("anthropic/claude-sonnet-4-5", "anthropic/claude-sonnet-4-5")),
    "codex": (("gpt-6.1-terra", "gpt-6.1-terra"), ("gpt-6.2-sol", "gpt-6.2-sol")),
    "claude": (
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-sonnet-5-5", "Sonnet 5.5"),
        ("claude-haiku-5-5", "Haiku 5.5"),
        ("claude-fable-5-1", "Fable 5.1"),
    ),
    "cursor": (("auto", "Auto"),),
    "antigravity": (("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),),
}

# Thinking-effort levels each engine's CLI really accepts. An engine that is not
# listed here (Cursor) has no effort flag, so the control is hidden and the
# API refuses an effort for it. Codex: ``-c model_reasoning_effort=<level>``;
# Claude and Antigravity: ``--effort <level>``.
EFFORT_LEVELS: dict[str, tuple[str, ...]] = {
    "opencode": ("low", "medium", "high", "xhigh"),
    "codex": ("low", "medium", "high", "xhigh"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "antigravity": ("low", "medium", "high", "xhigh", "max"),
}

# Used when a bot or job has no effort yet. Codex "high" is Joshua's default
# rule; "medium" is Claude Code's current default on 5.5 models.
DEFAULT_EFFORT: dict[str, str] = {"codex": "high", "claude": "medium", "antigravity": "medium"}


def effort_levels(engine: str) -> tuple[str, ...]:
    return EFFORT_LEVELS.get(engine, ())


def default_effort(engine: str) -> str | None:
    return DEFAULT_EFFORT.get(engine)


def valid_effort(engine: str, level: str) -> bool:
    return level in EFFORT_LEVELS.get(engine, ())


CapabilityTier = Literal["small", "mid", "frontier"]

_SMALL_RE = re.compile(r"haiku|nano|(?<![a-z])mini(?![a-z])|(?<![a-z])lite(?![a-z])|flash-lite", re.I)
_FRONTIER_RE = re.compile(
    r"opus|(?<![a-z0-9])gpt-?6(?![0-9])|(?<![a-z0-9])o[34](?![a-z0-9.])", re.I
)
_MID_RE = re.compile(
    r"sonnet|fable|composer|flash|(?<![a-z0-9])gpt-?5(?![0-9])|^auto$|gemini|grok", re.I
)


def capability_tier(model: str | None) -> CapabilityTier:
    """Display-only capability class derived from a model id (never stored).

    Rules, checked in this order so the lower tier wins when an id matches
    several:

    * ``small``: haiku, nano, mini, lite / flash-lite (any "lite" word).
    * ``frontier``: opus, gpt-6.x (including ``gpt-6.1-terra``), o3 / o4 class.
    * ``mid``: sonnet, fable, composer, non-lite flash, gpt-5.x, cursor ``auto``,
      and other known mid families (gemini, grok).
    * anything unrecognised, or no model at all, is ``small``: when unsure
      prefer the lower tier, so routine policy never over-trusts an unknown id.
    """
    if not model:
        return "small"
    if _SMALL_RE.search(model):
        return "small"
    if _FRONTIER_RE.search(model):
        return "frontier"
    if _MID_RE.search(model):
        return "mid"
    return "small"


DEFAULT_ICONS: dict[str, str] = {"Test Bot": "🧪", "Scout": "🧭"}
DEFAULT_ICON = "🤖"

# Model ids are passed as a separate argv element; still forbid a leading dash
# so an id can never be read as a CLI flag.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]=,+-]{0,99}$")


def valid_model_id(value: str) -> bool:
    return bool(MODEL_ID_RE.fullmatch(value))


def model_error(engine: str, model: str | None) -> str | None:
    """Why *model* cannot be used with *engine* (None if fine or unset)."""
    if model and engine == "opencode":
        # provider/model, or a nested id such as openrouter/anthropic/claude-x
        parts = model.split("/")
        if len(parts) < 2 or not all(parts) or model.startswith("-") or any(c.isspace() for c in model):
            return "OpenCode models must be provider/model, e.g. opencode/big-pickle"
    return None


def default_model(engine: str) -> str | None:
    return DEFAULT_MODELS.get(engine)


def parse_model_listing(output: str) -> list[tuple[str, str]]:
    """Parse ``<id> - <label>`` or ``<id>\\t<label>`` lines from a CLI listing."""
    models: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in output.splitlines():
        line = raw.replace("\u200b", "").strip()
        if not line:
            continue
        if "\t" in line:
            model_id, _, label = line.partition("\t")
        elif " - " in line:
            model_id, _, label = line.partition(" - ")
        else:
            model_id, label = line, ""
        model_id = model_id.strip()
        label = re.sub(r"\s*\((current|default)[^)]*\)\s*$", "", label.strip(), flags=re.IGNORECASE)
        label = " ".join(label.split())
        if " " in model_id or not valid_model_id(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        models.append((model_id, label or model_id))
    return models


def choice_label(engine: str, model_id: str, model_label: str | None = None) -> str:
    return f"{ENGINE_LABELS.get(engine, engine)} · {model_label or model_id}"


def _family_key(model_id: str) -> str:
    """Strip speed and effort suffixes so variant ids group together."""
    trimmed = re.sub(r"-fast$", "", model_id, flags=re.I)
    trimmed = re.sub(r"(?:-thinking)?-(?:low|medium|high|xhigh)$", "", trimmed, flags=re.I)
    return trimmed or model_id


def _pick_family_rep(group: list[tuple[str, str]]) -> tuple[str, str]:
    by_id = {model_id: (model_id, label) for model_id, label in group}
    family = _family_key(group[0][0])
    if family in by_id:
        return by_id[family]
    for suffix in ("-high", "-thinking-high", "-medium", "-low", "-xhigh", "-thinking-xhigh"):
        for model_id, label in group:
            if model_id.endswith(suffix):
                return model_id, label
    return group[0]


# dropdown size per engine (default 24); OpenCode lists every model its CLI reports
MODEL_LIST_LIMITS: dict[str, int] = {"opencode": 300}


def shortlist_models(
    engine: str, models: list[tuple[str, str]], *, limit: int | None = None
) -> list[tuple[str, str]]:
    """Keep a dropdown-sized list when a CLI reports a huge catalog.

    ``auto`` / the engine default / static catalog entries are kept first, then
    one representative per model family (dropping ``-fast`` and extra effort
    variants). Other valid ids can still be set through the API.
    """
    limit = limit or MODEL_LIST_LIMITS.get(engine, 24)
    if len(models) <= limit:
        return list(models)
    by_id = {model_id: (model_id, label) for model_id, label in models}
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(item: tuple[str, str] | None) -> None:
        if item and item[0] not in seen:
            seen.add(item[0])
            chosen.append(item)

    default = DEFAULT_MODELS.get(engine)
    if default:
        add(by_id.get(default) or (default, default))
    for model_id, label in STATIC_MODELS.get(engine, ()):
        add(by_id.get(model_id))
    groups: dict[str, list[tuple[str, str]]] = {}
    for item in models:
        groups.setdefault(_family_key(item[0]), []).append(item)
    for group in groups.values():
        if len(chosen) >= limit:
            break
        add(_pick_family_rep(group))
    return chosen[:limit]
