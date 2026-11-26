"""How a bot's chat replies read (0.12.36): one house style for every engine, plus a light tidy-up.

``HOUSE_STYLE`` is the shared block every provider gets: inline in each fresh-session prompt, a short
reminder in each resumed turn, and as an appended system prompt where the CLI takes one (Claude's
``--append-system-prompt``, Codex's ``developer_instructions``), so it outranks the CLI's own
formatting habits (headers, bold labels, bullet dumps).

``tidy_reply`` flattens the few Claude-Code habits that still slip through, in the chat copy of a reply
only (the job keeps the reply as written, and files are never touched). It is deliberately
conservative: it never rewrites sentences, and it never touches fenced code or inline code.

1. A leading ``#`` header on a short reply: a short title ("## Summary") is dropped; a longer one
   keeps its words as a plain line.
2. Bold labels at the start of a line or bullet (``- **Health check:** ok``): the bold is removed, so
   the label reads as plain text (``- Health check: ok``).
3. A trailing filler line ("Let me know if you need anything else."): removed when something else
   remains.
"""
from __future__ import annotations

import re

HOUSE_STYLE = """How your chat replies read (Bot-Office house style; it overrides your CLI's default \
formatting habits in chat replies only; files, code, commits and reports you write keep their normal format):
- Write like a thoughtful colleague replying in a chat: warm, plain, conversational prose with contractions \
(it's, I've, you'll, didn't).
- Keep it short: usually 1-2 sentences, two short paragraphs at most, unless the owner asks for detail or the \
work is research. Answer what was asked, plus anything they must act on; leave other facts out (they can ask). Don't \
recite everything you checked: the status line carries the evidence.
- Put the answer first. No meta openers ("Here's a summary", "Great question", "Sure!") and no filler closings \
("Let me know if...", "Hope this helps").
- No headers. No bold labels and no "Label: value" lines (not "**Status:** done", not "- **Tests:** pass"); \
say it in a sentence: "It's live on 0.12.36 and the tests pass."
- Use a list only for genuinely parallel items (three or more of the same kind), with short plain bullets. \
Use bold rarely, at most once, for the one thing they must not miss.
- Links as markdown with short labels, like [the PR](https://example.com/pr/12); `code` spans only for \
identifiers, commands and paths (not for versions, numbers or names).
- No arrows or symbols in prose (write "then" or "so", not "→"; no ✅ or ❌); emoji rarely.
- The chat renders markdown, so use it lightly and correctly; never leave stray asterisks.
For example, a status question gets: "It's healthy on 0.12.35, nothing's running and last night's backup is \
fine. The only loose end is card #189, which you can decline since we don't need it any more.\""""

# for a resumed session (the full block was in its first prompt, and in the system prompt where the
# CLI takes one): one line, so it stays in force without repeating the whole block every turn
HOUSE_STYLE_REMINDER = ("Reply in the house style: warm, plain conversational prose, answer first, usually "
                        "1-2 sentences, no headers, no bold labels or \"Label: value\" lines, lists only for "
                        "parallel items, no filler closing.")

# a reply up to this size counts as "short" for the leading-header rule
SHORT_REPLY_CHARS = 1500
SHORT_TITLE_WORDS = 4

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
# "**Label:**", "**Label**:" or "__Label:__" at the start of a line, optionally after a bullet or number;
# the label is short (up to 6 words, no asterisks or backticks)
_BOLD_LABEL = re.compile(
    r"^(?P<lead>\s*(?:[-*+]\s+|\d+[.)]\s+)?)"
    r"(?P<mark>\*\*|__)(?P<label>[^*_`\n]{1,60}?)(?P<colon_in>:?)(?P=mark)(?P<colon_out>:?)(?=\s|$)")
_FILLER = re.compile(
    r"^\s*(?:let me know (?:if|whether|when|how)|just let me know|feel free to|"
    r"(?:i )?hope (?:this|that) helps|happy to help|glad to help|"
    r"if you (?:have any (?:other |more |further )?questions|need anything else|'d like (?:anything|more)))"
    r".{0,160}$",
    re.IGNORECASE)


def _fenced(lines: list[str]) -> list[bool]:
    """Per line: is it part of a fenced code block (fence lines included)?"""
    inside, marker, out = False, "", []
    for line in lines:
        hit = _FENCE.match(line)
        if hit and (not inside or hit.group(1) == marker):
            out.append(True)
            inside, marker = (not inside), hit.group(1)
            continue
        out.append(inside)
    return out


def _unbold_label(line: str) -> str:
    match = _BOLD_LABEL.match(line)
    if not match:
        return line
    label = match.group("label").strip()
    has_colon = bool(match.group("colon_in") or match.group("colon_out"))
    rest = line[match.end():]
    # only a label: short, and either followed by a colon or standing alone as the whole bullet's opener
    if not label or len(label.split()) > 6 or (not has_colon and rest.strip()):
        return line
    return match.group("lead") + label + (":" if has_colon else "") + rest


def tidy_reply(text: str) -> str:
    """The chat copy of a reply with the leading header, bold labels and trailing filler flattened."""
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    code = _fenced(lines)

    # 1. a leading header on a short reply
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and not code[first] and len(text) <= SHORT_REPLY_CHARS:
        hit = _HEADER.match(lines[first])
        rest = "\n".join(lines[first + 1:]).strip()
        if hit and rest:
            title = hit.group(1).strip().strip("*_").strip()
            if len(title.split()) <= SHORT_TITLE_WORDS and not title.endswith(("?", "!")):
                del lines[first], code[first]
                while first < len(lines) and not lines[first].strip() and not code[first]:
                    del lines[first], code[first]
            else:
                lines[first] = title

    # 2. bold labels at the start of a line
    lines = [line if code[i] else _unbold_label(line) for i, line in enumerate(lines)]

    # 3. a trailing filler line, when something else remains
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if last is not None and not code[last] and _FILLER.match(lines[last]):
        remaining = "\n".join(lines[:last]).strip()
        if remaining:
            lines = lines[:last]
    elif last is not None and not code[last]:
        # "It's live now. Let me know if anything looks off." loses only its last sentence
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", lines[last].rstrip())
        if len(sentences) > 1 and _FILLER.match(sentences[-1]) and "`" not in sentences[-1]:
            lines[last] = " ".join(sentences[:-1])

    out = "\n".join(lines).strip("\n")
    return out if out.strip() else text
