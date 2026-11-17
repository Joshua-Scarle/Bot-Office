"""0.11.10: only the owner's own typed messages look like the owner's. Bot-to-bot messages, their reply
copies and Bot-Office's own lines say who wrote them; continuation machinery stays out of the chat."""
import json
import shutil
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_loop import Scripted, run_all
from test_loop import setup as loop_setup

from botoffice.api import create_app
from botoffice.scheduler import Scheduler
from botoffice.store import EditError, JobStore

HTML = Path(__file__).parents[1] / "src" / "botoffice" / "static" / "bot-office.html"


def _office(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": Scripted()})
    return store, scheduler


def _by_text(store, bot, text, **kw):
    return next(m for m in store.list_messages(bot.id, **kw) if m.text == text)


def test_bot_to_bot_messages_name_both_bots(settings):
    store, scheduler = _office(settings)
    writer, lead = store.create_bot("Printer Writing Bot", "", "codex", None), store.create_bot("Lead", "", "codex", None)
    _, job, _ = store.enqueue_agent_message(writer, lead, "please investigate", None, "2000-01-01T00:00:00+00:00")
    out = _by_text(store, writer, "please investigate")
    assert (out.role, out.author, out.peer) == ("user", "bot_out", "Lead")
    incoming = _by_text(store, lead, "please investigate")
    assert (incoming.author, incoming.peer) == ("bot_in", "Printer Writing Bot")
    store.publish_outcome(writer.id, job.id, "assistant", "found it", sender="from Lead", source="inbox")
    reply = _by_text(store, writer, "found it")
    assert (reply.author, reply.peer) == ("bot_reply", "Lead")
    # the writer's own outgoing message counts as the bot's activity; the lead's copy is a prompt it got
    assert store.bot_unread(writer.id).unread_count == 2
    assert store.bot_unread(lead.id).unread_count == 0
    # never editable or rewindable as if the owner had typed it
    with pytest.raises(EditError):
        scheduler.turns.edit_message(writer, out.id, "changed")


def test_owner_routine_watcher_and_bridge_lines(settings):
    store, scheduler = _office(settings)
    bot = store.create_bot("Ada", "", "codex", None)
    scheduler.turns.post_message(bot, "typed by the owner")
    scheduler.turns.post_message(bot, "daily report", source="routine")
    scheduler.turns.post_message(bot, "from the bridge", source="inbox", sender="grok-user")
    scheduler.turns.post_message(bot, "dropped in the inbox", source="inbox")
    authors = {m.text: (m.author, m.peer) for m in store.list_messages(bot.id)}
    assert authors["typed by the owner"] == ("owner", None)
    assert authors["daily report"] == ("office", None)
    assert authors["from the bridge"] == ("bot_in", "grok-user")
    assert authors["dropped in the inbox"] == ("office", None)


def test_assignment_continue_steps_stay_out_of_the_chat(settings):
    store, scheduler, _, bot, _item = loop_setup(settings, "step one\nBOT-OFFICE-STATUS: continue — next",
                                                "all done\nBOT-OFFICE-STATUS: done")
    run_all(store, scheduler)
    shown = store.list_messages(bot.id)
    assert not [m for m in shown if m.text.startswith("Continue ")]
    assert not [m for m in shown if m.text.startswith("Not done (continuing)")]
    assert [m.text for m in shown if m.role == "assistant"] == ["step one", "all done"]
    assert all(m.author != "owner" for m in shown if m.role == "user")
    everything = store.list_messages(bot.id, include_archived=True)
    hidden = [m for m in everything if m.hidden]
    assert any(m.text == 'Continue "Report"' and m.author == "office" for m in hidden)
    assert any(m.text.startswith("Not done (continuing)") for m in hidden)
    # machinery never counts as unread
    assert store.bot_unread(bot.id).unread_count == len([m for m in shown if m.role != "user"])


def test_blocked_assignment_still_tells_the_owner(settings):
    store, scheduler, _, bot, _item = loop_setup(settings, "stuck\nBOT-OFFICE-STATUS: blocked — no access")
    run_all(store, scheduler)
    assert any(m.role == "system" and "blocked" in m.text.lower() for m in store.list_messages(bot.id))


def test_wakeup_prompt_is_hidden(settings):
    store, scheduler = _office(settings)
    bot = store.create_bot("Ada", "", "codex", None)
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM bots WHERE id=?", (str(bot.id),)).fetchone()
        scheduler.assignments._insert_turn(db, row, "check CI", "check CI", sender="Bot-Office", priority=5,
                                           source="wakeup", hidden=True)
    assert not store.list_messages(bot.id)
    assert store.list_messages(bot.id, include_hidden=True)[0].author == "office"


def test_live_bubble_says_continuing(settings):
    settings.ensure_dirs()
    app = create_app(settings, {"codex": Scripted()})
    store = app.state.store
    headers = {"Authorization": "Bearer test-token"}
    client = TestClient(app)  # no lifespan: the scheduler stays off, so the queued turn stays queued
    bot = client.post("/v1/bots", headers=headers, json={"name": "Ada"}).json()
    posted = client.post(f"/v1/bots/{bot['id']}/messages", headers=headers, json={"text": "go"}).json()
    store.cancel(posted["job"]["id"])
    bot_model = store.get_bot(bot["id"])

    def mark(db, job):
        db.execute("UPDATE jobs SET continue_count=3 WHERE id=?", (str(job.id),))

    app.state.scheduler.turns.post_message(bot_model, "Continuing", sender="Bot-Office", hook=mark)
    listed = client.get(f"/v1/bots/{bot['id']}/messages?pending=true", headers=headers).json()
    live = [m for m in listed if m["pending"]]
    assert live and live[-1]["continuing"] == "3 of 10" and live[-1]["author"] == "bot"


def test_bot_hands_off_an_assignment(settings):
    store, scheduler = _office(settings)
    writer, lead = store.create_bot("Writer", "", "codex", None), store.create_bot("Lead", "", "codex", None)
    parent = scheduler.assignments.create(writer.id, {"title": "Print job", "goal": "print it"})
    scheduler.assignments.agent_create(writer.id, {"title": "Root cause", "goal": "find why", "start": True},
                                       for_bot=lead.id, parent_id=parent.id)
    [out] = [m for m in store.list_messages(writer.id) if m.role == "user"]
    assert (out.author, out.peer, out.source) == ("bot_out", "Lead", "assignment") and "Goal: find why" in out.text
    [incoming] = [m for m in store.list_messages(lead.id) if m.role == "user"]
    assert (incoming.author, incoming.peer) == ("bot_in", "Writer") and incoming.job_id
    assert store.bot_unread(writer.id).unread_count == 1


def test_chat_renders_by_author():
    html = HTML.read_text()
    assert 'bot.name + " → " + peer' in html and 'peer + " → " + bot.name' in html
    assert "function isOwnerMessage(m)" in html and "return isOwnerMessage(m) && (m.source" in html
    assert "bubbleClass(m)" in html and "Show more" in html
    # inter-bot messages (Grok Bot style): a time and "N messages with <bot>", the text in an overlay
    assert "return group ? agentGroupRow(group, bot) : null;" in html
    assert '" messages with"' in html and '" message with"' in html
    assert '<dialog id="agent-chat"' in html and 'id="agent-chat-close">Close Chat</button>' in html
    assert '"agent-swap", "⇄"' in html and "accentColor(sender.name)" in html
    assert "innerHTML" not in html.split("function renderMarkdown")[1].split("function openAgentChat")[0]
    assert 'return m.source === "routine" ? "Routine" : "You";' not in html  # the old fallback made bots look like the owner
    assert "continuing, " in html


def _js_function(html, name):
    start = html.index(f"function {name}(")
    depth, i = 0, html.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(html[i], 0)
        i += 1
        if depth == 0:
            return html[start:i]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_inter_bot_rows_group_sent_and_received_by_pair():
    html = HTML.read_text()
    script = "\n".join([_js_function(html, "isAgentRow"), _js_function(html, "agentGroups"), """
const m = (id, author, peer) => ({ id, author, peer, created_at: "2026-11-15T21:32:00Z" });
const msgs = [m(1, "owner"), m(2, "bot_out", "Lead"), m(3, "bot_in", "Lead"), m(4, "bot_reply", "Lead"),
  m(5, "bot_out", "Engineer"), m(6, "bot"), m(7, "bot_in", "Engineer"), m(8, "owner"), m(9, "bot_out", "Engineer"),
  { id: -1, pending: true, author: "bot" }];
const groups = agentGroups(msgs);
const out = {};
for (const [id, g] of groups) out[id] = g ? g.items.map((x) => x.id) : null;
console.log(JSON.stringify(out));
"""])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    groups = json.loads(result.stdout)
    # sent, received and the reply copy with the same bot share one row; a new bot or any other message
    # (the bot's reply, the owner's message) starts a new row
    assert groups == {"2": [2, 3, 4], "3": None, "4": None, "5": [5], "7": [7], "9": [9]}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_overlay_markdown_is_built_from_dom_nodes():
    html = HTML.read_text()
    stub = """
class Node { constructor(tag) { this.tag = tag; this.kids = []; this.className = ""; }
  append(...xs) { this.kids.push(...xs); } get textContent() { return this.kids.map((k) => typeof k === "string" ? k : k.textContent).join(""); }
  set textContent(t) { this.kids = [t]; } }
const document = { createElement: (t) => new Node(t) };
function el(tag, cls, text) { const n = new Node(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
const shape = (n) => typeof n === "string" ? n : [n.tag, ...n.kids.map(shape)];
"""
    script = "\n".join([stub, _js_function(html, "mdInline"), _js_function(html, "renderMarkdown"),
                        'console.log(JSON.stringify(shape(renderMarkdown("**Hatch range:** <b>x</b> `a`\\n- one\\n- two\\n\\n```\\ncode\\n```"))))'])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    tree = json.loads(result.stdout)
    assert tree == ["div", ["p", ["strong", "Hatch range:"], " <b>x</b> ", ["code", "a"]],
                    ["ul", ["li", "one"], ["li", "two"]], ["pre", "code"]]
