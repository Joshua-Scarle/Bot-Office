"""Parsers that turn each CLI's official JSONL stream into a few normalized events.

Only documented event types we need are read; everything else is ignored.
Normalized events are ``(kind, data)`` pairs:

* ``text``     - assistant text; ``{"text": delta}`` appends, ``{"text": ..., "replace": True}``
                 replaces the draft (Codex only reports whole messages)
* ``thinking`` - reasoning/thinking text, ``{"text": ...}``
* ``tool``     - a tool call, ``{"name": ..., "summary": ...}``
* ``final``    - the final reply, ``{"text": ...}``
* ``error``    - ``{"message": ...}``
"""

from __future__ import annotations

import re

import json
from typing import Any

Event = tuple[str, dict[str, Any]]

SUMMARY_LIMIT = 200
SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")


def _clip(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_input(value: Any) -> str:
    """A short one-line description of tool arguments (never the full payload)."""
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        for key in SUMMARY_KEYS:
            if isinstance(value.get(key), str) and value[key]:
                return _clip(value[key])
        try:
            return _clip(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError):
            return ""
    return ""


SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,199}")


def session_id_from(obj: Any) -> str | None:
    """Provider session/thread id announced in a stream event, if any.

    Codex: ``{"type":"thread.started","thread_id":...}``; Claude and Cursor:
    ``session_id`` on events; Antigravity: ``result.conversation_id``.
    """
    if not isinstance(obj, dict):
        return None
    result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
    for value in (obj.get("thread_id"), obj.get("session_id"), obj.get("conversation_id"),
                  result.get("conversation_id")):
        if isinstance(value, str) and SESSION_ID_RE.fullmatch(value):
            return value
    return None


class StreamParser:
    """Base parser: ignores everything."""

    def __init__(self) -> None:
        self.final_text: str | None = None
        self.session_id: str | None = None
        # what the CLI itself reported when the turn failed, and token usage when it says
        self.error_text: str | None = None
        self.api_status: int | None = None
        self.usage: dict[str, Any] | None = None
        # 0.11.7: short lines of what this invocation did ("said ...", "ran ..."), kept so that a
        # steering restart can tell the new invocation where the interrupted one got to
        self.progress: list[str] = []
        self._said = ""

    def record(self, kind: str, data: dict[str, Any]) -> None:
        """Remember a normalized event for :attr:`progress` (text is merged into one narration line)."""
        if kind == "text":
            text = str(data.get("text") or "")
            self._said = text if data.get("replace") else self._said + text
            return
        if kind == "tool":
            self.flush_said()
            line = "ran " + str(data.get("name") or "tool")
            if data.get("summary"):
                line += ": " + str(data["summary"])
            self.progress.append(_clip(line, 240))

    def flush_said(self) -> None:
        said = " ".join(self._said.split())
        self._said = ""
        if said:
            self.progress.append("said: " + _clip(said, 600))

    def feed(self, obj: Any) -> list[Event]:
        return []

    def finish(self) -> list[Event]:
        return []


class ClaudeParser(StreamParser):
    """``claude -p --output-format stream-json --verbose --include-partial-messages``.

    """

    def __init__(self) -> None:
        super().__init__()
        self._partial_text = False
        self._partial_thinking = False
        self._has_text = False
        self._new_message = False
        self._compact: dict[str, Any] | None = None

    def _text(self, text: str) -> list[Event]:
        if not text:
            return []
        if self._new_message and self._has_text:
            text = "\n\n" + text
        self._new_message = False
        self._has_text = True
        return [("text", {"text": text})]

    def feed(self, obj: Any) -> list[Event]:
        if not isinstance(obj, dict) or obj.get("parent_tool_use_id"):
            return []
        kind = obj.get("type")
        if kind == "stream_event":
            event = obj.get("event") or {}
            etype = event.get("type")
            if etype == "message_start":
                self._new_message = True
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                    self._partial_text = True
                    return self._text(delta["text"])
                if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                    self._partial_thinking = True
                    return [("thinking", {"text": delta["thinking"]})]
            return []
        if kind == "assistant":
            events: list[Event] = []
            content = (obj.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and not self._partial_text:
                    events += self._text(str(block.get("text") or ""))
                elif btype == "thinking" and not self._partial_thinking and block.get("thinking"):
                    events.append(("thinking", {"text": str(block["thinking"])}))
                elif btype == "tool_use":
                    events.append(
                        (
                            "tool",
                            {
                                "name": str(block.get("name") or "tool"),
                                "summary": summarize_input(block.get("input")),
                            },
                        )
                    )
            self._partial_text = self._partial_thinking = False
            self._new_message = True
            return events
        if kind == "system" and obj.get("subtype") == "compact_boundary":
            meta = obj.get("compact_metadata")
            self._compact = meta if isinstance(meta, dict) else {}
            return []
        if kind == "result":
            result = obj.get("result")
            _usage(self, obj.get("usage"), obj.get("total_cost_usd"))
            if obj.get("local_command") == "compact" and not obj.get("is_error") and not str(result or "").strip():
                # 0.11.15: a successful /compact has an empty result; say what happened in one line
                result = compact_status(self._compact)
            if obj.get("is_error"):
                self.error_text = str(result or obj.get("subtype") or "error")
                if isinstance(obj.get("api_error_status"), int):
                    self.api_status = obj["api_error_status"]
                return [("error", {"message": _clip(self.error_text, 500)})]
            if isinstance(result, str):
                self.final_text = result
                return [("final", {"text": result})]
        return []


def compact_status(meta: dict[str, Any] | None) -> str:
    """One line for a finished Claude Code ``/compact``: ``Conversation compacted (40k -> 10.5k tokens, 4.7s)``."""
    meta = meta or {}
    before, after, took = meta.get("pre_tokens"), meta.get("post_tokens"), meta.get("duration_ms")

    def tokens(value: int) -> str:
        return f"{value / 1000:.1f}k".replace(".0k", "k") if value >= 1000 else str(value)

    details = []
    if isinstance(before, int) and isinstance(after, int) and not isinstance(before, bool):
        details.append(f"{tokens(before)} → {tokens(after)} tokens")
    if isinstance(took, int | float) and not isinstance(took, bool) and took > 0:
        details.append(f"{took / 1000:.1f}s")
    return "Conversation compacted" + (f" ({', '.join(details)})" if details else "")


def _usage(parser: StreamParser, usage: Any, cost: Any = None) -> None:
    if isinstance(usage, dict):
        parser.usage = {key: value for key, value in usage.items() if isinstance(value, int | float)}
    if isinstance(cost, int | float):
        parser.usage = {**(parser.usage or {}), "cost_usd": cost}


class AntigravityParser(StreamParser):
    """``agy -p --output-format stream-json``: ``{"event": init|step_update|result, ...}`` lines.

    Text arrives as ``text_delta`` of ``agent_response`` steps; the ``result`` event carries the
    final ``response``, the usage, and (``status`` ERROR) the error. An ``interrupted`` result is
    what a steering interrupt produces: it sets ``error_text`` but emits no error event.
    """

    def __init__(self) -> None:
        super().__init__()
        self._has_text = False
        self._new_message = False
        self._open_tools: set[tuple[str, str]] = set()

    def feed(self, obj: Any) -> list[Event]:
        if not isinstance(obj, dict):
            return []
        event = obj.get("event")
        if event == "step_update" and isinstance(obj.get("step_update"), dict):
            step = obj["step_update"]
            conversation = step.get("conversation_id")
            if self.session_id is None and isinstance(conversation, str) and SESSION_ID_RE.fullmatch(conversation):
                self.session_id = conversation
            kind, state = step.get("step_type"), step.get("state")
            if kind == "agent_response":
                events: list[Event] = []
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    if self._new_message and self._has_text:
                        delta = "\n\n" + delta
                    self._new_message, self._has_text = False, True
                    events.append(("text", {"text": delta}))
                if state == "DONE":
                    _usage(self, step.get("usage"))
                    self._new_message = True
                return events
            if kind == "tool":
                self._new_message = True
                key = (str(step.get("tool_name") or "tool"), summarize_input(step.get("tool_info")))
                seen = key in self._open_tools
                if state == "DONE":
                    self._open_tools.discard(key)
                else:
                    self._open_tools.add(key)
                if not seen:
                    return [("tool", {"name": key[0], "summary": key[1]})]
            return []
        if event == "result" and isinstance(obj.get("result"), dict):
            result = obj["result"]
            _usage(self, result.get("usage"))
            if str(result.get("status") or "").upper() == "ERROR":
                reason = str(result.get("error") or result.get("finished_reason") or "error")
                self.error_text = reason
                if reason.lower() == "interrupted" or result.get("finished_reason") == "interrupted":
                    return []
                return [("error", {"message": _clip(reason, 500)})]
            response = result.get("response")
            if isinstance(response, str) and response.strip():
                self.final_text = response
                return [("final", {"text": response})]
        return []


class CodexParser(StreamParser):
    """``codex exec --json``: JSONL ``thread``/``turn``/``item`` events.

    Codex reports whole items, not token deltas: a finished ``agent_message``
    replaces the draft and the last one is the final reply.
    """

    def feed(self, obj: Any) -> list[Event]:
        if not isinstance(obj, dict):
            return []
        etype = obj.get("type")
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = item.get("type")
        if etype == "item.completed" and itype == "agent_message" and isinstance(item.get("text"), str):
            self.final_text = item["text"]
            return [("text", {"text": item["text"], "replace": True})]
        if etype == "item.completed" and itype == "reasoning" and item.get("text"):
            return [("thinking", {"text": str(item["text"])})]
        if etype == "item.started":
            if itype == "command_execution":
                return [("tool", {"name": "command", "summary": _clip(item.get("command") or "")})]
            if itype == "mcp_tool_call":
                name = ".".join(str(x) for x in (item.get("server"), item.get("tool")) if x)
                return [("tool", {"name": name or "mcp", "summary": summarize_input(item.get("arguments"))})]
            if itype == "web_search":
                return [("tool", {"name": "web_search", "summary": _clip(item.get("query") or "")})]
        if etype == "item.completed" and itype == "file_change":
            paths = ", ".join(str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict))
            return [("tool", {"name": "file_change", "summary": _clip(paths)})]
        if etype == "turn.completed":
            _usage(self, obj.get("usage"))
            self.error_text = None  # an earlier stream error (e.g. a reconnect) was recovered from
        if etype in ("error", "turn.failed"):
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            message = str(obj.get("message") or error.get("message") or "error")
            self.error_text = message
            return [("error", {"message": _clip(message, 500)})]
        return []

    def finish(self) -> list[Event]:
        return [("final", {"text": self.final_text})] if self.final_text else []


class CursorParser(StreamParser):
    """``cursor-agent -p --output-format stream-json --stream-partial-output``.

    Partial output may be followed by a consolidated assistant snapshot.
    Text before a tool or thinking boundary is narration, not the final reply.
    """

    def __init__(self) -> None:
        super().__init__()
        self._streamed = ""
        self._segment = ""
        self._deltas = 0
        self._new_segment = False

    def feed(self, obj: Any) -> list[Event]:
        if not isinstance(obj, dict):
            return []
        etype = obj.get("type")
        if etype == "assistant":
            content = (obj.get("message") or {}).get("content")
            text = "".join(
                str(b.get("text") or "")
                for b in (content if isinstance(content, list) else [])
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if self._new_segment:
                # an assistant message after a tool or thinking boundary starts the reply segment,
                # even an empty one: the final reply is then empty, not the earlier narration
                self._segment = ""
                self._deltas = 0
            if not text:
                return []
            if not self._new_segment and self._deltas > 1 and text.strip() == self._segment.strip():
                # a consolidated snapshot of the segment, with or without timestamp_ms
                return []
            if "timestamp_ms" not in obj and not self._new_segment and (
                (self._segment and text.strip() == self._segment.strip())
                or (self._streamed and text.strip() == self._streamed.strip())
            ):
                return []
            prefix = "\n\n" if self._new_segment and self._streamed else ""
            self._new_segment = False
            self._segment += text
            self._deltas += 1
            self._streamed += text
            return [("text", {"text": prefix + text})]
        if etype in ("thinking", "tool_call"):
            self._new_segment = True
        if etype == "thinking" and isinstance(obj.get("text"), str) and obj["text"]:
            return [("thinking", {"text": obj["text"]})]
        if etype == "tool_call" and obj.get("subtype") == "started":
            call = obj.get("tool_call")
            if isinstance(call, dict) and call:
                key, body = next(iter(call.items()))
                name = key[: -len("ToolCall")] if key.endswith("ToolCall") else key
                args = body.get("args") if isinstance(body, dict) else None
                return [("tool", {"name": name or "tool", "summary": summarize_input(args)})]
        if etype == "result":
            result = obj.get("result")
            _usage(self, obj.get("usage"))
            if obj.get("is_error"):
                self.error_text = str(result or "error")
                return [("error", {"message": _clip(self.error_text, 500)})]
            final = self._segment.strip() if self._streamed else result
            if isinstance(final, str):
                self.final_text = final
                return [("final", {"text": final})]
        return []


class OpenCodeParser(StreamParser):
    """OpenCode run --format json completed parts and session errors."""

    def __init__(self) -> None:
        super().__init__()
        self.session_id: str | None = None
        self.error: str | None = None
        self._has_text = False

    def feed(self, obj: Any) -> list[Event]:
        if not isinstance(obj, dict):
            return []
        if isinstance(obj.get("sessionID"), str):
            self.session_id = obj["sessionID"]
        kind = obj.get("type")
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if kind == "text" and isinstance(part.get("text"), str):
            # assumes OpenCode emits each completed text part whole (one per step), so the last part is the answer
            # text before a tool call is narration: the reply is only the last text part
            self.final_text = part["text"]
            text = part["text"]
            if text:
                text = ("\n\n" if self._has_text else "") + text
                self._has_text = True
            return [("text", {"text": text})]
        if kind == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            return [("tool", {"name": str(part.get("tool") or "tool"),
                              "summary": summarize_input(state.get("input"))})]
        if kind == "error":
            error = obj.get("error")
            if isinstance(error, dict):
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                self.error = str(data.get("message") or error.get("name") or "OpenCode error")
            else:
                self.error = str(error or "OpenCode error")
            self.error_text = self.error
            return [("error", {"message": _clip(self.error, 500)})]
        return []


PARSERS: dict[str, type[StreamParser]] = {
    "codex": CodexParser,
    "opencode": OpenCodeParser,
    "claude": ClaudeParser,
    "cursor": CursorParser,
    "antigravity": AntigravityParser,
}


def parser_for(engine: str) -> StreamParser:
    return PARSERS.get(engine, StreamParser)()
