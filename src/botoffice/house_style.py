"""How a bot's chat replies read (0.12.36): one house style for every engine, plus a light tidy-up.

``HOUSE_STYLE`` is the shared block every provider gets: inline in each fresh-session prompt, a short
reminder in each resumed turn, and as an appended system prompt where the CLI takes one (Claude's
``--append-system-prompt``, Codex's ``developer_instructions``), so it outranks the CLI's own
formatting habits (headers, bold labels, bullet dumps).

``tidy_reply`` flattens the few Claude-Code habits that still slip through, in the chat copy of a reply
only (the job keeps the reply as written, and files are never touched). It is deliberately
conservative: it never rewrites sentences, and it never touches fenced code or inline code.

1. A leading ``#`` header on a short reply: a generic title ("## Summary", "## Status") is dropped; any
   other keeps its words as a plain line.
2. Bold labels at the start of a line or bullet (``- **Health check:** ok``): the bold is removed, so
   the label reads as plain text (``- Health check: ok``).
3. A trailing generic closing ("Let me know if you need anything else.", "Hope this helps!"): removed
   when something else remains. A "Let me know if <something specific>" is kept: it may be an instruction.
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

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+(\S.*)$")
# a leading header with one of these titles says nothing on its own and is dropped; any other keeps its words
GENERIC_TITLES = {"summary", "status", "status update", "update", "result", "results", "answer", "overview",
                  "tl;dr", "tldr", "done", "report", "quick update", "short answer", "the answer", "recap"}
# "**Label:**" or "**Label**:" at the start of a line, optionally after a bullet or number;
# the label is short (up to 6 words, no asterisks or backticks); __x__ is left alone (__init__)
_BOLD_LABEL = re.compile(
    r"^(?P<lead>\s*(?:[-*+]\s+|\d+[.)]\s+)?)"
    r"\*\*(?P<label>[^*`\n]{1,60}?)(?P<colon_in>:?)\*\*(?P<colon_out>:?)(?=\s|$)")
_FILLER = re.compile(
    r"^\s*(?:"
    r"(?:just )?let me know if (?:you )?(?:have any (?:other |more |further )?questions|need anything(?: else)?|"
    r"(?:would|'d) like (?:anything else|more(?: detail| details| info)?|any changes)|"
    r"want (?:anything else|more(?: detail| details)?|any changes)|there'?s anything else(?: I can do)?|"
    r"I can help(?: with anything else)?)(?: here| on this| with this)?"
    r"|feel free to (?:ask|reach out|let me know)"
    r"|(?:I )?hope (?:this|that) helps"
    r"|(?:I'?m )?(?:always )?happy to help(?: further| with anything else)?"
    r")\s*[.!]?\s*$",
    re.IGNORECASE)


def _fenced(lines: list[str]) -> list[bool]:
    """Per line: is it part of a fenced code block (fence lines included)? A block closes only on a fence of
    the same character, at least as long, with nothing after it (CommonMark)."""
    opener, out = "", []
    for line in lines:
        hit = _FENCE.match(line)
        if not opener:
            if hit and not (hit.group(1)[0] == "`" and "`" in hit.group(2)):
                opener = hit.group(1)
            out.append(bool(opener))
            continue
        out.append(True)
        if hit and hit.group(1)[0] == opener[0] and len(hit.group(1)) >= len(opener) and not hit.group(2).strip():
            opener = ""
    return out


def _code_like(lines: list[str], fenced: list[bool]) -> list[bool]:
    """Per line: fenced code, an indented code block (4+ spaces or a tab after a blank line), or a line that
    starts inside an inline code span opened on an earlier line of the same paragraph."""
    out, ticks, indented, prev_blank = [], 0, False, True
    for line, fence in zip(lines, fenced):
        if fence or not line.strip():
            out.append(fence)
            ticks, indented, prev_blank = 0, False, not line.strip()
            continue
        indented = (indented or prev_blank) and line.startswith(("    ", "\t"))
        out.append(indented or ticks % 2 == 1)
        ticks += line.count("`")
        prev_blank = False
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
            title = hit.group(1).rstrip()
            if title.endswith("#") and title.rstrip("#")[-1:].isspace():  # "## Title ##": closing hashes
                title = title.rstrip("#")
            title = title.strip()
            if len(title) > 4 and title.startswith("**") and title.endswith("**"):
                title = title[2:-2].strip()
            if title.lower().rstrip(":.") in GENERIC_TITLES:
                del lines[first], code[first]
                while first < len(lines) and not lines[first].strip() and not code[first]:
                    del lines[first], code[first]
            elif title:
                lines[first] = title  # its words stay, as a plain line

    # 2. bold labels at the start of a line (never inside inline code that spans lines, or indented code)
    lines = [line if skip else _unbold_label(line) for line, skip in zip(lines, _code_like(lines, code))]

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
