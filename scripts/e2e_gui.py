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

    def __init__(self) -> None:
        self.app = None

    async def login_status(self) -> str:
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        from botoffice.providers.base import RunResult
        log_path.write_text("e2e run\n", encoding="utf-8")
        await asyncio.sleep(1.5)  # long enough to see the working state
        prompt = job.prompt or ""
        if job.source == "inbox":
            text = "Got it, thanks.\n\nBOT-OFFICE-STATUS: done"
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
                        enabled_providers=frozenset({"codex"}), voice_mode="off")
    engine = E2EEngine()
    app = create_app(settings, {"codex": engine})
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
        unlock(page, base, token)
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
    page.locator("#needs-toggle").focus()
    page.keyboard.press("Enter")
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "false", "Enter on the focused row collapses it")
    page.keyboard.press("Space")
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "true", "Space expands it again")
    page.keyboard.press("Enter")
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
    check(page.locator("#needs-toggle").get_attribute("aria-expanded") == "false", "collapsed state is remembered after a reload")


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
            if args.send_to:
                if not args.send_to.startswith(TEST_PREFIX):
                    raise SystemExit(f'--send-to only messages test bots: the name must start with "{TEST_PREFIX}"')
                open_bot(page, args.send_to)
                enter_to_send(page, out, "e2e check from Bot-Office: please reply with just OK.", "OK")
        else:
            with tempfile.TemporaryDirectory(prefix="bot-office-e2e-") as tmp:
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
                open_bot(page, CY)
                send_with_enter(page, "Please send the report email to Joe.")
                page.locator(".approval-card").last.wait_for(timeout=60000)
                needs_you_sidebar(page, out, expect_items=True)
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
