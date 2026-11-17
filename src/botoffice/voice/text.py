"""Turn a chat message (Markdown) into short plain-text chunks worth reading aloud."""

from __future__ import annotations

import re

CHUNK_CHARS = 400
MAX_CHUNKS = 200

# Every pattern stays on one line and its repeated parts cannot run past the next
# delimiter, so the work is linear in the text length (no catastrophic backtracking on
# input such as 100k of "*a " or "[[[[").
_FENCE = re.compile(r"\s*(```|~~~)")
_IMAGE = re.compile(r"!\[([^\[\]\n]{0,500})\]\([^()\n]{0,2000}\)")
_LINK = re.compile(r"\[([^\[\]\n]{1,500})\]\([^()\n]{0,2000}\)")
_URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
_HTML = re.compile(r"</?[a-zA-Z][^<>\n]{0,500}>")
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^[ \t]*(?:\|[ \t]*)?:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*(?:\|[ \t]*)?$", re.MULTILINE)
# only real emphasis: snake_case and 2*3*4 stay intact; the content never contains its
# own delimiter, so an unclosed marker fails at the next one instead of scanning on
_EMPHASIS = re.compile(
    r"(?<![\w*~])(?:\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*|__(?=\S)([^_\n]+?)(?<=\S)__"
    r"|\*(?=\S)([^*\n]+?)(?<=\S)\*|_(?=\S)([^_\n]+?)(?<=\S)_|~~(?=\S)([^~\n]+?)(?<=\S)~~)(?![\w*~])"
)
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def _without_code_blocks(text: str) -> str:
    """Replace fenced code blocks (an unclosed one runs to the end) with a short note."""
    out: list[str] = []
    fence = None
    for line in text.split("\n"):
        if fence is None:
            match = _FENCE.match(line)
            if match:
                fence = match.group(1)
                out.append("(code block omitted)")
            else:
                out.append(line)
        elif line.lstrip().startswith(fence):
            fence = None
    return "\n".join(out)


def speakable(markdown: str) -> str:
    """Plain prose: code blocks and URLs are announced, not read out character by character."""
    text = _without_code_blocks(markdown.replace("\r\n", "\n"))
    text = _IMAGE.sub(lambda m: f"(image {m.group(1)})" if m.group(1) else "(image)", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("(link)", text)
    text = _HTML.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _TABLE_SEP.sub("", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    for _ in range(2):
        text = _EMPHASIS.sub(lambda m: next(group for group in m.groups() if group is not None), text)
    text = "\n".join(_table_row(line) for line in text.split("\n"))
    lines = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        if line and line[-1] not in ".!?:;,…)":
            line += "."  # a pause between list items, headings and paragraphs
        lines.append(line)
    return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()


def _table_row(line: str) -> str:
    if line.count("|") < 2:
        return line
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return ", ".join(cell for cell in cells if cell)


def chunks(markdown: str, size: int = CHUNK_CHARS) -> list[str]:
    """Speakable text split at sentence (then word) boundaries into chunks of <= *size*."""
    out: list[str] = []
    current = ""
    for paragraph in speakable(markdown).split("\n"):
        for sentence in _SENTENCE.split(paragraph):
            for piece in _split_long(sentence, size):
                if current and len(current) + 1 + len(piece) > size:
                    out.append(current)
                    current = piece
                else:
                    current = f"{current} {piece}" if current else piece
        if len(out) >= MAX_CHUNKS:
            break
    if current:
        out.append(current)
    return [chunk for chunk in out if any(ch.isalnum() for ch in chunk)][:MAX_CHUNKS]


def _split_long(sentence: str, size: int) -> list[str]:
    if len(sentence) <= size:
        return [sentence] if sentence else []
    parts, current = [], ""
    for word in sentence.split(" "):
        while len(word) > size:
            if current:
                parts.append(current)
                current = ""
            parts.append(word[:size])
            word = word[size:]
        if current and len(current) + 1 + len(word) > size:
            parts.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        parts.append(current)
    return parts
