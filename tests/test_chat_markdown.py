"""0.12.36: bot replies render as safe Markdown in the chat (bot-office.html renderMarkdown)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "src" / "botoffice" / "static" / "bot-office.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# a tiny DOM: elements serialize to escaped HTML, so a test sees exactly what the browser would build
STUB = r"""
const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
class Node {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.className = ""; }
  append(...xs) { this.kids.push(...xs); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  get lastElementChild() { return this.kids.filter((k) => typeof k !== "string").pop() || null; }
  set textContent(t) { this.kids = [String(t)]; }
  get textContent() { return this.kids.map((k) => typeof k === "string" ? k : k.textContent).join(""); }
  get html() {
    const attrs = (this.className ? ` class="${esc(this.className)}"` : "") +
      Object.entries(this.attrs).map(([k, v]) => ` ${k}="${esc(v)}"`).join("");
    const inner = this.kids.map((k) => typeof k === "string" ? esc(k) : k.html).join("");
    return this.tag === "br" || this.tag === "hr" ? `<${this.tag}>` : `<${this.tag}${attrs}>${inner}</${this.tag}>`;
  }
}
const document = { createElement: (t) => new Node(t) };
function el(tag, cls, text) { const n = new Node(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
"""


def _js_function(html: str, name: str) -> str:
    start = html.index(f"function {name}(")
    depth, i = 0, html.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(html[i], 0)
        i += 1
        if depth == 0:
            return html[start:i]


def _renderer() -> str:
    html = HTML.read_text()
    consts = (html[html.index("const MD_INLINE"):html.index("function mdSafeUrl(")] + "\n"
              + html[html.index("const MD_FENCE"):html.index("function mdCells(")])
    names = ("mdSafeUrl", "mdLink", "mdText", "mdInline", "mdCells", "renderMarkdown")
    return "\n".join([STUB, consts, *(_js_function(html, n) for n in names)])


def render(*texts: str) -> list[str]:
    script = _renderer() + f"\nfor (const t of {json.dumps(list(texts))}) console.log(JSON.stringify(renderMarkdown(t).html));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def inner(html: str) -> str:
    assert html.startswith('<div class="md">') and html.endswith("</div>")
    return html[len('<div class="md">'):-len("</div>")]


def test_inline_bold_italic_code_strike():
    [out] = render("It's **live** on *0.12.36*, see `bot-office.html` and ~~old~~ ***both***.")
    assert inner(out) == ("<p>It's <strong>live</strong> on <em>0.12.36</em>, see <code>bot-office.html</code> and "
                          "<del>old</del> <strong><em>both</em></strong>.</p>")


def test_raw_double_asterisks_never_show():
    outs = render("**Status:** done", "a half-written **bold", "**", "5 * 3 = 15 and 2**3", "`a ** b` stays")
    assert inner(outs[0]) == "<p><strong>Status:</strong> done</p>"
    assert inner(outs[1]) == "<p>a half-written bold</p>"
    assert "**" not in "".join(outs[:4])
    assert "5 * 3" in outs[3]  # a lone asterisk is just a character
    assert inner(outs[4]) == "<p><code>a ** b</code> stays</p>"  # code keeps its asterisks


def test_identifiers_with_underscores_stay_plain():
    [out] = render("Edit __init__.py and my_var_name, not _this_.")
    assert inner(out) == "<p>Edit __init__.py and my_var_name, not <em>this</em>.</p>"


def test_links_are_safe():
    outs = render("[the PR](https://example.com/pr/12) and [mail](mailto:a@b.c)",
                  "[bad](javascript:alert(1)) [worse](data:text/html,x) [rel](/v1/x)",
                  "see https://example.com/a_b. and (www.example.org)",
                  "[https://x.example](https://x.example)")
    assert inner(outs[0]) == (
        '<p><a class="md-link" href="https://example.com/pr/12" target="_blank" rel="noopener noreferrer nofollow">'
        'the PR</a> and <a class="md-link" href="mailto:a@b.c" target="_blank" rel="noopener noreferrer nofollow">'
        "mail</a></p>")
    assert "href" not in outs[1] and "javascript" not in outs[1].split("<p>")[0]
    assert inner(outs[1]).startswith("<p>bad") and "worse" in outs[1] and "rel" in outs[1]
    assert 'href="https://example.com/a_b"' in outs[2] and "</a>. and (" in outs[2]
    assert 'href="https://www.example.org"' in outs[2] and "</a>)" in outs[2]
    assert outs[3].count("<a ") == 1  # a link never nests another link


def test_html_is_text_never_markup():
    [out] = render('<img src=x onerror=alert(1)> <script>alert(1)</script> **<b>bold</b>**')
    assert "<img" not in out and "<script" not in out and "<b>" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out and "<strong>&lt;b&gt;bold&lt;/b&gt;</strong>" in out


def test_code_blocks_keep_everything_literal():
    [out] = render("Run this:\n```bash\necho **hi** <b>\n- not a list\n```\nDone.")
    assert inner(out) == ('<p>Run this:</p><pre class="md-code"><code class="lang-bash">echo **hi** &lt;b&gt;\n'
                          "- not a list</code></pre><p>Done.</p>")


def test_unclosed_fence_runs_to_the_end():
    [out] = render("```\ncode **x**")
    assert inner(out) == '<pre class="md-code"><code>code **x**</code></pre>'


def test_lists_nested_ordered_and_loose():
    outs = render("- one\n- two\n  - two a\n- three",
                  "3. third\n4. fourth",
                  "- a\n\n- b",
                  "- wrapped item\n  continues here")
    assert inner(outs[0]) == "<ul><li>one</li><li>two<ul><li>two a</li></ul></li><li>three</li></ul>"
    assert inner(outs[1]) == '<ol start="3"><li>third</li><li>fourth</li></ol>'
    assert inner(outs[2]) == "<ul><li>a</li><li>b</li></ul>"
    assert inner(outs[3]) == "<ul><li>wrapped item continues here</li></ul>"


def test_paragraphs_headings_quotes_rules():
    [out] = render("## Done\nFirst line\nsecond line\n\n> quoted **x**\n\n---\nAfter")
    assert inner(out) == ('<p class="md-h">Done</p><p>First line<br>second line</p>'
                          '<blockquote><div class="md"><p>quoted <strong>x</strong></p></div></blockquote>'
                          "<hr><p>After</p>")


def test_short_tables():
    [out] = render("| Bot | Engine |\n|---|:---:|\n| Test Bot | `codex` |\n| Scout | claude |\n\nAfter")
    assert inner(out) == ('<div class="md-table"><table><thead><tr><th>Bot</th><th>Engine</th></tr></thead><tbody>'
                          "<tr><td>Test Bot</td><td><code>codex</code></td></tr>"
                          "<tr><td>Scout</td><td>claude</td></tr></tbody></table></div><p>After</p>")


def test_main_chat_renders_bot_words_only():
    html = HTML.read_text()
    block = html[html.index("function usesMarkdown("):html.index("function openAgentChat(")]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block and "outerHTML" not in block
    assert 'return m.role === "assistant" && !isAgentRow(m);' in html
    assert 'fillBody(body, m, m.text);' in html  # chat bubbles
    assert "bubble.append(renderMarkdown(m.text));" in html  # the bot-to-bot overlay
    assert ".msg .body.md-body { white-space: normal; }" in html
