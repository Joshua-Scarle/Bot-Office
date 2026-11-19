#!/usr/bin/env python3
"""Live end-to-end test of the Bot-Office web UI in a real browser (0.11.10).

Two modes:

  Local (default): starts a throwaway Bot-Office on 127.0.0.1 with its own temporary data folder
  and a scripted engine (no CLI, no subscription used), then drives Chromium through the real UI:

    .venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium
    .venv/bin/python scripts/e2e_gui.py [--headed] [--channel chrome] [--out e2e-shots]

  Live (e.g. a staged copy): checks a running install read-only (unlock, Settings and Approvals
  load). With --send-to it also sends one short message with the Enter key to that bot and waits for
  its reply. That is a real turn on its engine, so the bot's name must start with "E2E test":

    .venv/bin/python scripts/e2e_gui.py --url http://127.0.0.1:8787 --token-file ~/.config/bot-office/token \
        [--send-to "E2E test bot"]

  The token comes from --token-file, else BOT_OFFICE_TOKEN_FILE, else BOT_OFFICE_TOKEN (never the
  command line, where other users can read it in the process list). --token still works but is
  deprecated.

Checked (local mode): Enter sends (bubble shows, box clears, Send comes back; Shift+Enter adds a
line), the bot's reply arrives, continuation prompts never look like the owner's messages,
Settings (rules, behavior, approvals: switch a class, add and remove your own, timeout), the
Approve / Deny card in the chat (deny leaves a short note; approve runs it once and hides the
resume prompt), the bot-to-bot row and its "A ⇄ B" panel with Close Chat, and a continuation held
by the per-bot cap that your next message supersedes (it runs as its own turn). Screenshots of each
step go to --out. It ends with a plain PASS/FAIL list; exit code 0 means every check passed.
Browser: Playwright's Chromium by default, or an installed Chrome with --channel chrome.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TOKEN = "e2e-token"
# every bot this script makes or messages is labelled as a test bot
ADA, BEA, CY = "E2E test bot Ada", "E2E test bot Bea", "E2E test bot Cy"
CLAUDE_BOT = "E2E test bot Claude"
TEST_PREFIX = "E2E test"
REPLY = "Hello from the e2e bot."
FAILURES: list[str] = []
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
    RESULTS.append((ok, what))
    if not ok:
        FAILURES.append(what)


# --- local server with a scripted engine ---

class E2EEngine:
    """Answers like a bot would. "send the report email" tries a gated connector action through
    Bot-Office's own gate decision, exactly as the CLI hook would."""

    name = "codex"
    enabled = True
    executable = "/bin/true"  # slash commands: the catalog asks the CLI for its version

    def __init__(self) -> None:
        self.app = None

    async def capture_bytes(self, args, timeout=15, cwd=None):
        return 0, b"codex-cli 0.0.0-e2e", b""

    async def login_status(self) -> str:
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        from botoffice.providers.base import RunResult
        log_path.write_text("e2e run\n", encoding="utf-8")
        await asyncio.sleep(1.5)  # long enough to see the working state
        prompt = job.prompt or ""
        uploads = re.findall(r"^- (/\S+/uploads/\S+)$", prompt, re.M)
        if job.source == "inbox":
            text = "Got it, thanks.\n\nBOT-OFFICE-STATUS: done"
        elif uploads and "quote the code" in prompt:  # 0.11.15: like a CLI opening the file the prompt names
            quoted = " ".join(Path(path).read_text().strip() for path in uploads)
            text = f"The file says: {quoted}\n\nBOT-OFFICE-STATUS: done"
        elif "missing part" in prompt:  # the owner's answer (the prompt also carries the earlier chat)
            text = f"{REPLY}\n\nBOT-OFFICE-STATUS: done"
        elif "work in parts" in prompt or "Automatic continuation" in prompt:
            text = "One more part written.\n\nBOT-OFFICE-STATUS: not_done — next: the following part"
        elif "send the report email" in prompt or "granted by the user" in prompt:
            event = {"hook_event_name": "PreToolUse", "tool_name": "mcp__gmail__send_message",
                     "tool_input": {"to": "joe@example.com", "subject": "Weekly report"}}
            rules = self.app.state.scheduler.rules
            decision = await asyncio.to_thread(rules.decide, job, event, "codex")
            if decision.decision == "allow":
                text = "Sent the weekly report to joe@example.com.\n\nBOT-OFFICE-STATUS: done"
            elif decision.effect == "ask":
                text = (f"The email is drafted; it waits for your approval.\n\n"
                        f"BOT-OFFICE-STATUS: waiting_on_user — approval #{decision.approval_id}")
            else:
                text = f"I couldn't send it: {decision.reason}\n\nBOT-OFFICE-STATUS: blocked — refused"
        else:
            text = f"{REPLY}\n\nBOT-OFFICE-STATUS: done"
        return RunResult("succeeded", exit_code=0, final_text=text)


class E2EClaude(E2EEngine):
    """A Claude Code stand-in: lists commands from a real init event and answers /compact with the real stream lines."""

    name = "claude"

    async def capture_bytes(self, args, timeout=15, cwd=None):
        if "/context" in args:
            return 0, (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "slash" / "claude-init.jsonl").read_bytes(), b""
        return 0, b"2.1.319 (Claude Code)", b""

    async def run(self, job, workspace, log_path, timeout):
        from botoffice.providers.base import RunResult
        from botoffice.providers.streams import ClaudeParser
        log_path.write_text("e2e claude run\n", encoding="utf-8")
        await asyncio.sleep(1)
        parser = ClaudeParser()
        # the lines `claude -p --resume <id> --output-format stream-json --verbose /compact` printed (trimmed)
        for line in ({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 39951, "post_tokens": 10491, "duration_ms": 4686}},
                     {"type": "result", "subtype": "success", "is_error": False, "result": "", "local_command": "compact", "num_turns": 0}):
            parser.feed(line)
        return RunResult("succeeded", exit_code=0, final_text=parser.final_text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_local(data_dir: Path):
    import uvicorn

    from botoffice.api import create_app
    from botoffice.config import Settings
    port = free_port()
    settings = Settings(data_dir=data_dir, token=TOKEN, port=port, job_timeout=60,
                        enabled_providers=frozenset({"codex", "claude"}), voice_mode="off")
    engine = E2EEngine()
    app = create_app(settings, {"codex": engine, "claude": E2EClaude()})
    engine.app = app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        with contextlib.suppress(OSError):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(base + "/", timeout=1)
            return base, app, server
        time.sleep(0.1)
    raise SystemExit("the local Bot-Office did not start")


def call(base: str, token: str, method: str, path: str, body: dict | None = None):
    request = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    with opener.open(request, timeout=20) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


# --- browser ---

def launch(pw, headed: bool, channel: str | None):
    try:
        # keep scrollbars in headless runs: the model list's scrollbar drag is checked with a real mouse
        return pw.chromium.launch(headless=not headed, channel=channel, ignore_default_args=["--hide-scrollbars"])
    except Exception as exc:
        hint = "install Google Chrome" if channel else "run: python -m playwright install chromium"
        raise SystemExit(f"The browser is not available ({exc}); {hint}") from exc


def unlock(page, base: str, token: str) -> None:
    page.goto(base + "/")
    page.fill("#token", token)
    page.click("#lock-form button")
    page.wait_for_selector("#app:not([hidden])", timeout=15000)


def open_bot(page, name: str) -> None:
    page.locator("#bot-list").get_by_text(name, exact=True).first.click()
    page.wait_for_selector("#text", timeout=10000)


def shot(page, out: Path, name: str) -> None:
    page.screenshot(path=str(out / f"{name}.png"), full_page=False)


def send_with_enter(page, text: str) -> None:
    box = page.locator("#text")
    box.click()
    box.fill(text)
    box.press("Enter")


def wait_text(page, text: str, timeout: float = 30000) -> bool:
    try:
        page.locator("#messages").get_by_text(text).first.wait_for(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def enter_to_send(page, out: Path, text: str, reply: str | None) -> None:
    print("Enter to send")
    box = page.locator("#text")
    box.click()
    box.fill("first line")
    box.press("Shift+Enter")
    box.type("second line")
    check(box.input_value() == "first line\nsecond line", "Shift+Enter adds a new line instead of sending")
    box.fill("")
    started = time.monotonic()
    send_with_enter(page, text)
    check(wait_text(page, text, 10000), "Enter sends: the message appears in the chat")
    with contextlib.suppress(Exception):
        page.wait_for_function("() => document.getElementById('text').value === ''", timeout=10000)
    check(box.input_value() == "", "the message box clears after sending")
    with contextlib.suppress(Exception):
        page.wait_for_function("() => !document.getElementById('send').disabled", timeout=10000)
    check(page.locator("#send").is_enabled(), f"Send is usable again ({time.monotonic() - started:.1f} s)")
    shot(page, out, "01-sent")
    if reply is not None:
        check(wait_text(page, reply, 120000), "the bot's reply arrives")
        shot(page, out, "02-reply")


def settings_page(page, out: Path, base: str, token: str, write: bool) -> None:
    print("Settings")
    page.click("#office-settings")
    page.wait_for_selector("#settings-dialog[open]", timeout=10000)
    page.wait_for_function("() => document.getElementById('rules-text').value.length > 100", timeout=10000)
    check(True, "Settings opens with the rules for all bots")
    rows = page.locator("#approval-classes .approval-row")
    rows.first.wait_for(timeout=10000)
    check(rows.count() >= 9, f"Approvals lists the classes ({rows.count()})")
    check("Sending email or messages" in page.locator("#approval-classes").inner_text(), "the email class is listed")
    check(page.locator("#approval-gates").inner_text().startswith("Engines:"), "it says which engines can pause")
    shot(page, out, "03-settings")
    if write:
        remote = page.locator("#approval-classes .approval-row", has_text="Logging in to other computers")
        remote.locator("input[type=checkbox]").check()
        page.fill("#approval-timeout", "15")
        page.click("#approvals-save")
        page.wait_for_function("() => document.getElementById('approvals-state').textContent.startsWith('Saved')",
                               timeout=10000)
        saved = call(base, token, "GET", "/v1/settings")["approvals"]
        by_key = {item["key"]: item for item in saved["classes"]}
        check(by_key["remote"]["enabled"] and saved["timeout_min"] == 15, "Save switches a class on and sets the timeout")
        page.click("#approval-add summary")
        page.fill("#approval-label", "Deploys")
        page.fill("#approval-pattern", r"\bmake\s+deploy\b")
        page.click("#approval-add-btn")
        page.locator("#approval-classes .approval-row", has_text="Deploys").wait_for(timeout=10000)
        check(True, "adding your own class shows it, switched on")
        shot(page, out, "04-approvals-custom")
        page.locator("#approval-classes .approval-row", has_text="Deploys").get_by_role("button", name="Remove").click()
        page.locator("#approval-classes .approval-row", has_text="Deploys").wait_for(state="detached", timeout=10000)
        check(True, "Remove takes your class away")
        call(base, token, "PUT", "/v1/settings", {"approvals": {"enabled": {"remote": False}, "timeout_min": 60}})
    page.click("#settings-close")


def approvals_in_chat(page, out: Path, base: str, app) -> None:
    print("Approve / Deny card")
    send_with_enter(page, "Please send the report email to Joe.")
    card = page.locator(".approval-card").last
    card.wait_for(timeout=60000)
    check("wants to: Sending email or messages" in card.inner_text(), "the card names the bot and the class")
    check("joe@example.com" in card.inner_text(), "the card shows the whole action")
    shot(page, out, "05-approval-card")
    card.get_by_role("button", name="Deny").click()
    page.wait_for_function("() => [...document.querySelectorAll('.approval-card')].pop().classList.contains('denied')",
                           timeout=15000)
    check(wait_text(page, "denied, so the task is stopped", 15000), "Deny leaves a short note; the task is stopped")
    send_with_enter(page, "OK, send the report email after all.")
    page.wait_for_function("() => document.querySelectorAll('.approval-card').length >= 2", timeout=60000)
    page.locator(".approval-card").last.get_by_role("button", name="Approve").click()
    check(wait_text(page, "Sent the weekly report", 90000), "Approve runs the action once and the bot finishes")
    shot(page, out, "06-approved")
    owner_texts = page.evaluate("""() => state.lastMessages.filter((m) => m.author === 'owner').map((m) => m.text)""")
    check(not any(text.startswith("Approval #") for text in owner_texts),
          "the resume prompt never shows as your message")
    check(not any("continue" in text.lower() and "BOT-OFFICE" in text for text in owner_texts),
          "no continuation prompt shows as your message")


def bot_to_bot(page, out: Path, base: str, token: str) -> None:
    print("Bot-to-bot row and panel")
    call(base, token, "POST", "/v1/bots/" + urllib.parse.quote(BEA) + "/message-agent",
         {"to": ADA, "body": "Ada, **can you** check the logs?"})
    open_bot(page, BEA)
    row = page.locator(".agent-group .agent-open").first
    row.wait_for(timeout=30000)
    check(" with" in row.inner_text() and "Ada" in row.inner_text(), f"Bea's chat shows a row: {row.inner_text()!r}")
    check(page.locator("#messages").get_by_text("check the logs").count() == 0, "the message stays folded in the chat")
    row.click()
    page.wait_for_selector("#agent-chat[open]", timeout=10000)
    title = page.locator("#agent-chat-title").inner_text()
    check("⇄" in title and "Bea" in title and "Ada" in title, f"the panel title reads 'Bea ⇄ Ada' ({title!r})")
    check(page.locator("#agent-chat-list strong").filter(has_text="can you").count() >= 1, "markdown renders in the panel")
    shot(page, out, "07-agent-panel")
    page.get_by_role("button", name="Close Chat").click()
    check(page.locator("#agent-chat[open]").count() == 0, "Close Chat closes the panel")
    open_bot(page, ADA)
    page.locator(".agent-group .agent-open").first.wait_for(timeout=30000)
    check(True, "the receiving bot's chat shows the row too")
    shot(page, out, "08-received-row")


def held_continuation(page, out: Path, base: str, token: str, app) -> None:
    print("A cap-held continuation and your message")
    store = app.state.scheduler.store
    call(base, token, "PUT", "/v1/settings", {"auto_continue": {"per_bot_day": 1}})
    bot = next(item for item in call(base, token, "GET", "/v1/bots") if item["name"] == CY)
    open_bot(page, CY)
    send_with_enter(page, "Please work in parts on the report.")
    held = None
    for _ in range(120):
        with contextlib.closing(store._connect()) as db:
            held = db.execute("SELECT id FROM jobs WHERE bot_id=? AND status='queued' AND failure_kind='auto_cap'",
                              (bot["id"],)).fetchone()
        if held:
            break
        time.sleep(0.5)
    check(held is not None, "the second automatic turn is held by the per-bot cap")
    check(wait_text(page, "Automatic work paused", 20000), "the chat says automatic work is paused")
    shot(page, out, "09-held")
    send_with_enter(page, "Here is the missing part, finish now.")
    check(wait_text(page, REPLY, 60000), "your message runs as its own turn and the bot answers")
    with contextlib.closing(store._connect()) as db:
        row = db.execute("SELECT status, error FROM jobs WHERE id=?", (held["id"],)).fetchone() if held else None
        steered = db.execute("SELECT COUNT(*) FROM messages WHERE steer_job_id=?", (held["id"],)).fetchone()[0] if held else 1
    check(bool(row) and row["status"] == "cancelled" and steered == 0,
          "the held continuation is superseded, not given your message")
    shot(page, out, "10-owner-turn")
    call(base, token, "PUT", "/v1/settings", {"auto_continue": {"per_bot_day": 20}})


# --- 0.11.13: searchable model list and the collapsed "Needs you" row ---

def model_search(page, out: Path, base: str, token: str, mock_long_list: bool) -> None:
    print("Model search")
    if mock_long_list:  # a long list so the popup has a scrollbar to drag
        models = [{"id": f"gpt-{n}.{m}-test", "display": f"gpt-{n}.{m}-test"} for n in range(1, 8) for m in range(10)]
        body = json.dumps([{"engine": "codex", "label": "Codex", "default_model": "gpt-1.0-test", "models": models}])
        page.route("**/v1/model-choices*", lambda route: route.fulfill(status=200, content_type="application/json", body=body))
        page.reload()  # the token is kept for the tab: the page comes back unlocked
        page.wait_for_selector("#app:not([hidden])", timeout=15000)
    page.click("#new-bot")
    box = page.locator("#f-model")
    box.wait_for(timeout=10000)
    page.wait_for_function("() => document.querySelector('#f-model').value !== ''", timeout=15000)
    check(box.get_attribute("role") == "combobox", "the model field is a combobox")
    box.click()
    check(page.locator("#f-model-list").is_visible(), "clicking the model field opens the list")
    options = page.locator("#f-model-list [role=option]").all_inner_texts()
    total = len(options)
    query = options[-1][2:7]  # a slice of one model's name, typed in capitals
    box.fill("")
    box.type(query.upper(), delay=20)
    shown = page.locator("#f-model-list [role=option]").all_inner_texts()
    outside = [t for t in shown if query.lower() not in t.lower()]
    check(0 < len(shown) <= total and len(outside) <= 1,
          f"typing {query.upper()!r} filters the list case-insensitively ({len(shown)} of {total}; only the pinned selection may differ)")
    box.fill("zzzz-no-such-model")
    check(page.locator("#f-model-list").get_by_text("No matches").count() == 1, 'a "No matches" row shows')
    box.press("Escape")
    check(not page.locator("#f-model-list").is_visible(), "Escape closes the list")
    check(page.locator("#bot-dialog[open]").count() == 1, "the first Escape leaves the dialog open")
    if mock_long_list:
        box.click()
        box.fill("")
        listbox = page.locator("#f-model-list")
        metrics = listbox.evaluate("(ul) => ({scroll: ul.scrollHeight, client: ul.clientHeight, bar: ul.offsetWidth - ul.clientWidth})")
        check(metrics["scroll"] > metrics["client"] and metrics["bar"] > 0, f"the long list has a visible scrollbar ({metrics})")
        box_ = listbox.bounding_box()
        x, y = box_["x"] + box_["width"] - metrics["bar"] / 2, box_["y"] + 20
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x, y + 60, steps=6)
        still_open = listbox.is_visible()
        scrolled = listbox.evaluate("(ul) => ul.scrollTop") > 0
        page.mouse.up()
        check(still_open and scrolled, f"dragging the scrollbar scrolls and keeps the list open (open={still_open}, scrolled={scrolled})")
        check(listbox.is_visible() and page.evaluate("() => document.activeElement.id") == "f-model",
              "after releasing the scrollbar the list is open and the field keeps focus")
        shot(page, out, "09-model-search-scrollbar")
        page.mouse.click(5, 5)  # a real outside click
        check(not listbox.is_visible(), "a real outside click closes the list")
        page.unroute("**/v1/model-choices*")
    page.keyboard.press("Escape")


def needs_you_sidebar(page, out: Path, expect_items: bool) -> None:
    print("Needs-you row in the sidebar")
    # headless Chromium reports notifications as denied; pretend they can still be switched on
    page.evaluate("""() => { Object.defineProperty(window, 'Notification', {configurable: true, value: {permission: 'default', requestPermission: async () => 'default'}}); }""")
    page.evaluate("() => { localStorage.removeItem('bot-office-needs-open'); }")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    page.evaluate("""() => { Object.defineProperty(window, 'Notification', {configurable: true, value: {permission: 'default', requestPermission: async () => 'default'}}); return loadNeedsYou(); }""")
    section, toggle = page.locator("#needs-you"), page.locator("#needs-toggle")
    if not expect_items:
        check(section.is_hidden() or toggle.get_attribute("aria-expanded") == "false", "live: the Needs-you row starts collapsed")
        if section.is_visible():
            shot(page, out, "sidebar-live")
        return
    check(section.is_visible(), "the Needs-you row shows when something needs you")
    check(toggle.get_attribute("aria-expanded") == "false", "it starts collapsed (aria-expanded=false)")
    check(not page.locator("#needs-list").is_visible(), "the list is not shown while collapsed")
    count = page.locator("#needs-count").inner_text()
    check(page.locator("#needs-count").is_visible() and count.isdigit() and int(count) >= 1, f"the row shows a count badge ({count})")
    check(page.locator("#notify-enable").is_visible(), "Turn on notifications is reachable while collapsed")
    shot(page, out, "sidebar-collapsed")
    toggle.click()
    check(toggle.get_attribute("aria-expanded") == "true" and page.locator("#needs-list").is_visible(), "clicking the row expands the list")
    check(page.locator("#needs-list .needs-item").count() == int(count), "the list has one item per counted request")
    check(page.locator("#notify-enable").is_visible(), "Turn on notifications is reachable while expanded")
    shot(page, out, "sidebar-expanded")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "true", "expanded state is remembered after a reload")
    page.locator("#needs-toggle").wait_for(state="visible", timeout=15000)  # the row appears once the list is loaded
    page.locator("#needs-toggle").focus()
    page.keyboard.press("Enter")
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "false", "Enter on the focused row collapses it")
    page.keyboard.press("Space")
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "true", "Space expands it again")
    page.keyboard.press("Enter")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    page.locator("#needs-toggle").wait_for(state="visible", timeout=15000)
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "false", "collapsed state is remembered after a reload")


# --- 0.11.13: bot search, resizable sidebar, files of any type in the chat ---

def sidebar_search(page, out: Path) -> None:
    print("Sidebar bot search")
    page.evaluate("() => localStorage.clear()")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    page.wait_for_selector("#bot-list .bot-item", timeout=15000)
    search = page.locator("#bot-search")
    total = page.locator("#bot-list .bot-item").count()
    check(search.is_visible() and total >= 1, f"the search box is in the sidebar ({total} bots)")
    all_names = page.locator("#bot-list .bot-item .name").all_inner_texts()
    sub = page.locator("#bot-list .bot-item .sub").first.inner_text().split(" · ")[0]
    target = all_names[-1]
    word = target.split()[-1]  # the last word of one bot's name
    search.click()
    search.type(word.upper(), delay=20)
    names = page.locator("#bot-list .bot-item .name").all_inner_texts()
    check(names == [n for n in all_names if word.lower() in n.lower()] and 1 <= len(names) < len(all_names) or len(all_names) == 1,
          f"typing {word.upper()!r} filters the list case-insensitively ({names})")
    search.fill("no-such-bot-zzz")
    check(page.locator("#bot-empty").is_visible() and page.locator("#bot-list .bot-item").count() == 0, "an empty-result message shows")
    model_word = sub.split()[-1]
    search.fill("")
    expected = sum(1 for t in page.locator("#bot-list .bot-item .meta").all_inner_texts() if model_word.lower() in t.lower())
    search.fill(model_word)
    check(page.locator("#bot-list .bot-item").count() >= expected >= 1, f"the model ({model_word!r}) matches too ({expected} bots show it)")
    search.fill(word)
    shot(page, out, "search-filtering")
    search.press("Escape")
    check(search.input_value() == "" and page.evaluate("() => document.activeElement.id") != "bot-search"
          and page.locator("#bot-list .bot-item").count() == total, "Esc clears the box, blurs it and restores the list")
    page.locator("body").click(position={"x": 700, "y": 400})
    page.keyboard.press("/")
    check(page.evaluate("() => document.activeElement.id") == "bot-search", '"/" focuses the search box')
    page.keyboard.press("Escape")
    page.keyboard.press("Control+k")
    check(page.evaluate("() => document.activeElement.id") == "bot-search", "Ctrl+K focuses the search box")
    page.keyboard.press("Escape")


def sidebar_resize(page, out: Path) -> None:
    print("Resizable sidebar")
    page.evaluate("() => localStorage.removeItem('bot-office-sidebar-width')")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    width = lambda: round(page.locator(".sidebar").bounding_box()["width"])  # noqa: E731
    handle = page.locator("#sidebar-resize")
    start = width()
    check(handle.get_attribute("role") == "separator" and handle.evaluate("(h) => getComputedStyle(h).cursor") == "col-resize",
          f"a col-resize separator handle exists (sidebar {start}px)")
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 300
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 100, y, steps=8)
    page.mouse.up()
    check(width() == start + 100, f"dragging the handle widens the sidebar ({start} -> {width()})")
    shot(page, out, "sidebar-resized")
    box = handle.bounding_box()
    page.mouse.move(box["x"] + 3, y)
    page.mouse.down()
    page.mouse.move(2000, y, steps=5)
    page.mouse.up()
    check(width() == 520, f"it stops at the 520px maximum ({width()})")
    page.mouse.move(handle.bounding_box()["x"] + 3, y)
    page.mouse.down()
    page.mouse.move(0, y, steps=5)
    page.mouse.up()
    check(width() == 200, f"it stops at the 200px minimum ({width()})")
    handle.focus()
    page.keyboard.press("ArrowRight")
    check(width() == 216, "ArrowRight widens by 16px from the keyboard")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    check(width() == 216, f"the width is remembered after a reload ({width()})")
    handle.dblclick()
    check(width() == 272, "double-click resets to the default width")


def file_uploads(page, out: Path, base: str, token: str) -> None:
    print("Uploads of any file type")
    import tempfile as _tf
    with _tf.TemporaryDirectory(prefix="e2e-files-") as tmp:
        stl = Path(tmp) / "bracket v2.stl"
        stl.write_bytes(b"solid bracket\n" + b"x" * 2048 + b"endsolid bracket\n")
        pdf = Path(tmp) / "../evil name.pdf".split("/")[-1]
        pdf.write_bytes(b"%PDF-1.4 e2e")
        page.set_input_files("#image-file", [str(stl), str(pdf)])
        page.wait_for_function("() => document.querySelectorAll('#attachment-list .file-chip').length === 2", timeout=10000)
        page.wait_for_function("() => [...document.querySelectorAll('#attachment-list .attachment-item')].every((i) => i.textContent.includes('Ready'))", timeout=15000)
        chips = page.locator("#attachment-list .file-chip").all_inner_texts()
        check(any("bracket v2.stl" in c and "KB" in c for c in chips), f"pending file chips show name and size ({chips})")
        send_with_enter(page, "Here are the CAD files.")
        page.wait_for_selector("#thread .file-chip", timeout=15000)
        chat = page.locator("#thread .file-chip").first
        page.wait_for_function("() => document.querySelector('#thread .file-chip .file-size').textContent !== ''", timeout=10000)
        text = chat.inner_text()
        check("bracket_v2.stl" in text and "KB" in text, f"the chat shows the file as a chip with name and size ({text!r})")
        shot(page, out, "file-chip")
        with page.expect_download(timeout=10000) as info:
            chat.click()
        download = info.value
        check(download.suggested_filename == "bracket_v2.stl", f"clicking the chip downloads it ({download.suggested_filename})")
        # a drop of a non-image file attaches it too
        page.evaluate("""() => {
            const dt = new DataTransfer(); dt.items.add(new File(["a,b\\n1,2\\n"], "table.csv", {type: "text/csv"}));
            document.getElementById("composer").dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
        }""")
        page.wait_for_function("() => [...document.querySelectorAll('#attachment-list .file-chip')].some((c) => c.textContent.includes('table.csv'))", timeout=10000)
        check(True, "dropping a CSV attaches it as a file chip")
        # a paste of a file does too
        page.evaluate("""() => {
            const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array([80, 75, 3, 4])], "bundle.zip", {type: "application/zip"}));
            document.getElementById("text").dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
        }""")
        page.wait_for_function("() => [...document.querySelectorAll('#attachment-list .file-chip')].some((c) => c.textContent.includes('bundle.zip'))", timeout=10000)
        check(True, "pasting a zip attaches it as a file chip")
        page.evaluate("""() => { for (const b of document.querySelectorAll('#attachment-list .attachment-item button')) b.click(); }""")
        # a file over the limit gets a clear error
        call(base, token, "GET", "/v1/bots")
    bot_id = page.evaluate("() => state.selected")
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{base}/v1/bots/{bot_id}/attachments?name=big.bin", data=b"0", method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Length": str(10**12)})
    try:
        urllib.request.urlopen(req, timeout=10)
        status, detail = 0, ""
    except urllib.error.HTTPError as exc:
        status, detail = exc.code, exc.read().decode()
    except Exception as exc:  # the server may close the connection after the early 413
        status, detail = 413, str(exc)
    check(status == 413, f"an oversize file is refused with 413 ({detail[:80]})")


# --- 0.11.14: slash commands in the chat box ---

def slash_commands(page, out: Path, base: str, token: str) -> None:
    print("Slash commands")
    open_bot(page, ADA)
    box = page.locator("#text")
    popup = page.locator("#slash-list")
    rows = page.locator("#slash-list li[role=option]:not(.note)")
    messages = lambda: len(call(base, token, "GET", "/v1/bots/" + bot_id + "/messages"))  # noqa: E731
    bot_id = next(b["id"] for b in call(base, token, "GET", "/v1/bots") if b["name"] == ADA)
    box.click()
    box.fill("")
    box.type("/")
    popup.wait_for(state="visible", timeout=10000)
    page.wait_for_selector("#slash-list li[role=option]:not(.note)", timeout=10000)
    check(popup.is_visible() and rows.count() >= 5, f'typing "/" opens the command list ({rows.count()} commands)')
    check(box.get_attribute("aria-expanded") == "true" and popup.get_attribute("role") == "listbox", "the list is an ARIA listbox the box controls")
    names = page.locator("#slash-list .slash-name").all_inner_texts()
    check("/e2e-ping" in names and "/model" in names and "/status" in names, "it lists the CLI's commands and the user's custom prompt")
    check(not any(n in ("/login", "/logout") for n in names), "login and logout are never listed")
    shot(page, out, "slash-open")
    box.type("co")
    filtered = page.locator("#slash-list .slash-name").all_inner_texts()
    check(filtered and all("co" in n for n in filtered) or any("co" in n for n in filtered), f'typing "co" filters the list ({filtered})')
    box.fill("/")
    popup.wait_for(state="visible")
    first = page.evaluate("() => document.getElementById('text').getAttribute('aria-activedescendant')")
    box.press("ArrowDown")
    second = page.evaluate("() => document.getElementById('text').getAttribute('aria-activedescendant')")
    box.press("ArrowUp")
    third = page.evaluate("() => document.getElementById('text').getAttribute('aria-activedescendant')")
    check(first == "slash-opt-0" and second == "slash-opt-1" and third == "slash-opt-0", "Up and Down move the highlight")
    box.press("Escape")
    check(popup.is_hidden() and box.input_value() == "/", "Esc closes the list and keeps the text")
    before = messages()
    # a picked custom command fills the box; a second Enter sends it
    box.fill("")
    box.type("/e2e-p")
    page.wait_for_function("() => document.querySelectorAll('#slash-list li[role=option]').length >= 1")
    box.press("Enter")
    check(box.input_value() == "/e2e-ping " and popup.is_hidden() and messages() == before, "Enter picks the command (fills the box, sends nothing yet)")
    box.type("hello there")
    box.press("Enter")
    check(wait_text(page, "/e2e-ping hello there", 10000), "a second Enter sends the command to the bot")
    jobs = call(base, token, "GET", "/v1/jobs")
    mine = [j for j in jobs if j.get("slash")]
    check(bool(mine) and json.loads(mine[0]["slash"])["mode"] == "expand" and "Ping the bot: hello there" in mine[0]["prompt"],
          "the custom prompt was filled in with the arguments and run as a turn")
    check(wait_text(page, REPLY, 60000), "the bot's reply arrives")
    # greyed out
    box.fill("")
    box.type("/stat")
    page.wait_for_selector("#slash-list li.grey", timeout=10000)
    grey = page.locator("#slash-list li.grey").first
    check(grey.get_attribute("aria-disabled") == "true" and "Unavailable" in grey.evaluate("e => getComputedStyle(e.querySelector('.slash-desc'), '::before').content"),
          "a command that cannot run headless is shown greyed out with a reason")
    shot(page, out, "slash-greyed")
    before = messages()
    box.press("Tab")
    box.press("Enter")
    page.wait_for_function("() => document.getElementById('status').classList.contains('bad')", timeout=10000)
    check("unavailable" in page.locator("#status").inner_text() and messages() == before, "running a greyed command says why and sends nothing")
    # confirm-gated
    page.wait_for_function("() => { const b = currentBot(); return b && !['running', 'queued'].includes(b.activity); }", timeout=20000)
    box.fill("")
    box.type("/new")
    page.wait_for_selector("#slash-list li[role=option]")
    box.press("Enter")
    box.press("Enter")
    dialog = page.locator("#slash-confirm")
    dialog.wait_for(state="visible", timeout=10000)
    check(dialog.is_visible() and "fresh" in page.locator("#slash-confirm-text").inner_text().lower(), "a destructive command asks for a confirmation")
    shot(page, out, "slash-confirm")
    page.locator("#slash-confirm-cancel").click()
    page.wait_for_function("() => document.getElementById('status').textContent.includes('Cancelled')", timeout=5000)
    check(dialog.is_hidden() and "Cancelled" in page.locator("#status").inner_text(), "Cancel changes nothing")
    page.wait_for_function("() => { const b = currentBot(); return b && !['running', 'queued'].includes(b.activity); }", timeout=20000)
    box.fill("/new")
    box.press("Enter")  # list is closed after fill? open it again through typing
    page.wait_for_timeout(200)
    if popup.is_visible():
        box.press("Enter")
    box.press("Enter")
    dialog.wait_for(state="visible", timeout=10000)
    page.locator("#slash-confirm-ok").click()
    try:
        page.wait_for_function("() => /session/i.test(document.getElementById('status').textContent)", timeout=10000)
    except Exception:
        print("status was:", page.locator("#status").inner_text())
        raise
    check("session" in page.locator("#status").inner_text().lower(), f'confirming runs it ({page.locator("#status").inner_text()})')
    # a mapped control: /model opens the model picker in Settings
    box.fill("")
    box.type("/model")
    page.wait_for_selector("#slash-list li[role=option]")
    box.press("Enter")
    box.press("Enter")
    page.locator("#bot-dialog").wait_for(state="visible", timeout=10000)
    check(page.evaluate("() => document.activeElement.id") == "f-model", "/model opens Settings with the model picker focused")
    page.locator("#dialog-cancel").click()
    # not a command of this bot: an ordinary message, as before
    box.fill("")
    send_with_enter(page, "/etc/hosts is the file I mean")
    check(wait_text(page, "/etc/hosts is the file I mean", 10000), "text that merely starts with / but is not a command is sent as a normal message")
    wait_text(page, REPLY, 60000)


# --- 0.11.15: a real turn reads an uploaded file; /compact shows a status line ---

def upload_read(page, out: Path) -> None:
    print("A turn reads an uploaded file")
    import tempfile as _tf
    token = "UPLOADTOKEN-e2e-7c41d9"
    with _tf.TemporaryDirectory(prefix="e2e-note-") as tmp:
        note = Path(tmp) / "secret-note.txt"
        note.write_text(f"The secret code is {token}. Nothing else matters.\n")
        page.set_input_files("#image-file", [str(note)])
        page.wait_for_function("() => [...document.querySelectorAll('#attachment-list .attachment-item')].every((i) => i.textContent.includes('Ready'))", timeout=15000)
        send_with_enter(page, "I attached a text file. Please quote the code in it.")
    check(wait_text(page, "The file says: The secret code is " + token, 60000), "the reply quotes the unique token from the uploaded file")
    shot(page, out, "upload-read")


def compact_status(page, out: Path, base: str, token: str) -> None:
    print("/compact on a Claude bot")
    call(base, token, "POST", "/v1/bots", {"name": CLAUDE_BOT, "default_provider": "claude"})
    page.reload()  # the new bot is listed after a reload; the session keeps the unlock
    open_bot(page, CLAUDE_BOT)
    box = page.locator("#text")
    box.click()
    box.fill("")
    box.type("/compact")
    page.wait_for_selector("#slash-list li[role=option]", timeout=10000)
    box.press("Enter")
    box.press("Enter")
    check(wait_text(page, "Conversation compacted (40k → 10.5k tokens, 4.7s)", 30000), "/compact shows a one-line status in the chat")
    thread = page.locator("#thread").inner_text()
    check("is_error" not in thread and "duration_api_ms" not in thread, "no raw result JSON is shown")
    shot(page, out, "compact-status")


def resolve_token(args, environ=None) -> str | None:
    """The live-mode API token: --token-file, BOT_OFFICE_TOKEN_FILE, BOT_OFFICE_TOKEN, then the
    deprecated --token (a warning on stderr; it shows in the process list)."""
    environ = os.environ if environ is None else environ
    path = args.token_file or environ.get("BOT_OFFICE_TOKEN_FILE")
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
    if environ.get("BOT_OFFICE_TOKEN"):
        return environ["BOT_OFFICE_TOKEN"].strip()
    if args.token:
        print("warning: --token is deprecated (it shows in the process list); use --token-file or "
              "BOT_OFFICE_TOKEN_FILE / BOT_OFFICE_TOKEN", file=sys.stderr)
        return args.token
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="a running Bot-Office (live mode, read-only unless --send-to)")
    parser.add_argument("--token-file", help="live mode: a file holding its API token (or set BOT_OFFICE_TOKEN_FILE)")
    parser.add_argument("--token", help="deprecated: its API token on the command line (use --token-file or BOT_OFFICE_TOKEN)")
    parser.add_argument("--send-to", help="live mode: send one short message to this bot with Enter")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--channel", choices=("chrome", "chromium", "msedge"),
                        help="use an installed browser instead of Playwright's Chromium")
    parser.add_argument("--out", default="e2e-shots", help="folder for screenshots")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch(pw, args.headed, None if args.channel == "chromium" else args.channel)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        if args.url:
            token = resolve_token(args)
            if not token:
                raise SystemExit("live mode needs a token: --token-file, BOT_OFFICE_TOKEN_FILE or BOT_OFFICE_TOKEN")
            base = args.url.rstrip("/")
            unlock(page, base, token)
            check(True, "unlocked " + base)
            settings_page(page, out, base, token, write=False)
            model_search(page, out, base, token, mock_long_list=False)
            needs_you_sidebar(page, out, expect_items=False)
            sidebar_search(page, out)
            sidebar_resize(page, out)
            if args.send_to:
                if not args.send_to.startswith(TEST_PREFIX):
                    raise SystemExit(f'--send-to only messages test bots: the name must start with "{TEST_PREFIX}"')
                open_bot(page, args.send_to)
                enter_to_send(page, out, "e2e check from Bot-Office: please reply with just OK.", "OK")
        else:
            with tempfile.TemporaryDirectory(prefix="bot-office-e2e-") as tmp:
                home = Path(tmp) / "home"  # slash commands read the CLI's custom command files from HOME
                (home / ".codex" / "prompts").mkdir(parents=True)
                (home / ".codex" / "prompts" / "e2e-ping.md").write_text("---\ndescription: Ping the bot (e2e)\nargument-hint: TEXT\n---\nPing the bot: $ARGUMENTS\n")
                os.environ["HOME"] = str(home)
                base, app, server = start_local(Path(tmp) / "data")
                for name in (ADA, BEA, CY):
                    call(base, TOKEN, "POST", "/v1/bots", {"name": name, "default_provider": "codex"})
                unlock(page, base, TOKEN)
                open_bot(page, ADA)
                enter_to_send(page, out, "hello from the e2e test", REPLY)
                settings_page(page, out, base, TOKEN, write=True)
                approvals_in_chat(page, out, base, app)
                bot_to_bot(page, out, base, TOKEN)
                held_continuation(page, out, base, TOKEN, app)
                model_search(page, out, base, TOKEN, mock_long_list=True)
                open_bot(page, BEA)  # a bot that has not asked for an approval yet
                send_with_enter(page, "Please send the report email to Joe.")
                page.locator(".approval-card").last.wait_for(timeout=60000)
                needs_you_sidebar(page, out, expect_items=True)
                sidebar_search(page, out)
                sidebar_resize(page, out)
                open_bot(page, ADA)
                file_uploads(page, out, base, TOKEN)
                slash_commands(page, out, base, TOKEN)
                open_bot(page, ADA)
                upload_read(page, out)
                compact_status(page, out, base, TOKEN)
                server.should_exit = True
        check(not errors, "no JavaScript errors" + (f": {errors[:3]}" if errors else ""))
        browser.close()
    print("\nResults:")
    for ok, what in RESULTS:
        print(("PASS  " if ok else "FAIL  ") + what)
    print(f"\n{'PASSED' if not FAILURES else 'FAILED'}: {len(RESULTS) - len(FAILURES)} passed, {len(FAILURES)} failed. "
          f"Screenshots in {out}/")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
