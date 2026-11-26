"""0.12.39 bo-intel: stall sweeper, priority inbox messages, visible refusals, rules sync, memory-note cards."""
import json
import runpy
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import freshness, grok_bridge, rules_sync, stall_sweep, usage
from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.memory import Memory
from botoffice.providers.fake import FakeAdapter
from botoffice.rules import Action, Rules, memory_note_command
from botoffice.store import URGENT_PRIORITY, JobStore, now
from botoffice.turns import InboxLimitError, Turns

H = {"Authorization": "Bearer test-token"}


# --- A. stall sweeper -------------------------------------------------------------------------

def _store(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    return store, Assignments(store)


def _started_task(store, asg, bot, hours_ago, status="active"):
    task = asg.create(bot.id, {"title": "Ship the thing", "goal": "g"})
    old = (now() - timedelta(hours=hours_ago)).isoformat()
    with closing(store._connect()) as db:
        db.execute("UPDATE assignments SET status=?, last_progress_at=? WHERE id=?", (status, old, str(task.id)))
    return task


def _wake_jobs(store, bot):
    return [job for job in store.list() if job.bot_id == bot.id and job.source == "wakeup"]


def _system_lines(store, bot):
    with closing(store._connect()) as db:
        return [row[0] for row in db.execute("SELECT text FROM messages WHERE bot_id=? AND role='system' ORDER BY id",
                                             (str(bot.id),))]


def _finish_all(store, bot):
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='succeeded', finished_at=? WHERE bot_id=? AND status IN ('queued','running')",
                   (now().isoformat(), str(bot.id)))


def _age_sweeps(store, hours):
    with closing(store._connect()) as db:
        db.execute("UPDATE stall_sweeps SET last_at=?", ((now() - timedelta(hours=hours)).isoformat(),))


def test_stalled_task_wakes_owner_then_section_lead_once_each(tmp_path):
    store, asg = _store(tmp_path)
    dpe = store.create_bot("MS Data Pipeline Engineer (BO)", "", "claude", None)
    lead = store.create_bot("MS Lead (BO)", "", "claude", None)
    task = _started_task(store, asg, dpe, hours_ago=4, status="waiting_on_user")

    assert stall_sweep.sweep(store, asg, 3)["woken"] == 1
    woke = _wake_jobs(store, dpe)
    assert len(woke) == 1 and "Stall check" in woke[0].prompt and "no progress since" in woke[0].prompt
    assert "bot-office-ask" in woke[0].prompt
    assert any("Stall check" in line for line in _system_lines(store, dpe))
    assert asg.get(task.id).status == "waiting_on_user"  # a chat wake-up: the paused task stays paused
    assert asg.get(task.id).stalled_at is not None

    # the same stall is not woken again, while the bot works or before N more hours
    assert stall_sweep.sweep(store, asg, 3) == {"woken": 0, "escalated": 0, "skipped": 1}
    _finish_all(store, dpe)
    assert stall_sweep.sweep(store, asg, 3)["woken"] == 0
    _age_sweeps(store, 4)
    assert stall_sweep.sweep(store, asg, 3)["escalated"] == 1
    lead_jobs = _wake_jobs(store, lead)
    assert len(lead_jobs) == 1 and "Stall escalation" in lead_jobs[0].prompt
    assert "MS Data Pipeline Engineer (BO)" in lead_jobs[0].prompt
    assert any("told MS Lead (BO)" in line for line in _system_lines(store, dpe))
    _finish_all(store, lead)
    _age_sweeps(store, 10)
    assert stall_sweep.sweep(store, asg, 3)["escalated"] == 0  # two strikes, then quiet

    # progress starts a new stall: the strikes count again
    with closing(store._connect()) as db:
        db.execute("UPDATE assignments SET last_progress_at=? WHERE id=?",
                   ((now() - timedelta(hours=5)).isoformat(), str(task.id)))
    assert stall_sweep.sweep(store, asg, 3)["woken"] == 1


def test_stalled_last_turn_wakes_the_bot(tmp_path):
    store, asg = _store(tmp_path)
    bot = store.create_bot("Engineering Lead (BO)", "", "claude", None)
    job = store.create("work", "claude", bot_id=bot.id, source="chat")
    with closing(store._connect()) as db:
        db.execute("UPDATE jobs SET status='incomplete', outcome='waiting_on_user: approval #19', finished_at=? "
                   "WHERE id=?", ((now() - timedelta(hours=5)).isoformat(), str(job.id)))
    assert stall_sweep.sweep(store, asg, 3)["woken"] == 1
    prompt = _wake_jobs(store, bot)[0].prompt
    assert "your last turn" in prompt.lower() and "approval #19" in prompt


def test_sweep_skips_open_busy_pending_card_retired_and_excluded(tmp_path):
    store, asg = _store(tmp_path)
    names = ("Open", "Busy", "Carded", "Retired one", "Excluded")
    bots = {name: store.create_bot(name, "", "claude", None) for name in names}
    _started_task(store, asg, bots["Open"], 9, status="open")
    for name in names[1:]:
        _started_task(store, asg, bots[name], 9)
    store.create("work", "claude", bot_id=bots["Busy"].id, source="chat")  # queued
    with closing(store._connect()) as db:
        db.execute("INSERT INTO approvals (bot_id,job_id,tool,summary,input_hash,scope,source,status,created_at) "
                   "VALUES (?,?,?,?,?,?,?,'pending',?)", (str(bots["Carded"].id), "j", "Bash", "x", "h", "attended",
                                                          "chat", now().isoformat()))
        db.execute("INSERT INTO bot_sections (id,name,position,collapsed,created_at) VALUES ('r','Retired',9,0,?)",
                   (now().isoformat(),))
        db.execute("INSERT INTO bot_section_members (bot_id,section_id,position) VALUES (?,'r',0)",
                   (str(bots["Retired one"].id),))
    stall_sweep.update(store, exclude=["Excluded"])
    assert stall_sweep.sweep(store, asg, 3)["woken"] == 0
    assert all(not _wake_jobs(store, bot) for bot in bots.values())


def test_sweep_respects_hourly_cap_and_off_switch(tmp_path):
    store, asg = _store(tmp_path)
    bots = [store.create_bot(f"Bot {n}", "", "claude", None) for n in range(4)]
    for bot in bots:
        _started_task(store, asg, bot, 8)
    assert stall_sweep.sweep(store, asg, 0)["woken"] == 0
    assert stall_sweep.sweep(store, asg, 3, per_hour=2)["woken"] == 2
    assert stall_sweep.sweep(store, asg, 3, per_hour=2)["woken"] == 0


def test_lead_resolution(tmp_path):
    store, asg = _store(tmp_path)
    eng = store.create_bot("Engineering Lead (BO)", "", "claude", None)
    ms = store.create_bot("MS Lead (BO)", "", "claude", None)
    dpe = store.create_bot("MS Data Pipeline Engineer (BO)", "", "claude", None)
    other = store.create_bot("Helper Bot", "", "claude", None)
    with pytest.raises(stall_sweep.StallSweepError):
        stall_sweep.update(store, leads={"MS Lead (BO)": "MS Lead (BO)"})
    cfg = stall_sweep.update(store, leads={"MS Lead (BO)": "Engineering Lead (BO)"})
    with closing(store._connect()) as db:
        row = lambda bot: db.execute("SELECT * FROM bots WHERE id=?", (str(bot.id),)).fetchone()
        assert stall_sweep.lead_of(db, row(dpe), cfg)["name"] == "MS Lead (BO)"
        assert stall_sweep.lead_of(db, row(ms), cfg)["name"] == "Engineering Lead (BO)"
        assert stall_sweep.lead_of(db, row(other), cfg) is None
        cfg = stall_sweep.update(store, leads={"*": "Engineering Lead (BO)"})
        assert stall_sweep.lead_of(db, row(other), cfg)["name"] == "Engineering Lead (BO)"
        assert stall_sweep.lead_of(db, row(eng), cfg) is None  # never itself


def test_stall_sweep_api(settings):
    client = TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))
    with client:
        store = client.app.state.store
        store.create_bot("MS Lead (BO)", "", "codex", None)
        assert client.put("/v1/stall-sweep", headers=H, json={"leads": {"*": "MS Lead (BO)"}}).status_code == 200
        got = client.get("/v1/stall-sweep", headers=H).json()
        assert got["hours"] == 3 and list(got["leads"]) == ["*"]
        assert client.put("/v1/stall-sweep", headers=H, json={"exclude": ["nobody"]}).status_code == 400
        assert client.post("/v1/stall-sweep/run", headers=H).json() == {"woken": 0, "escalated": 0, "skipped": 0}
        assert client.get("/v1/stall-sweep").status_code == 401


# --- B. priority inbox messages ---------------------------------------------------------------

def _priority(store, job_id):
    with closing(store._connect()) as db:
        return db.execute("SELECT priority FROM jobs WHERE id=?", (str(job_id),)).fetchone()[0]


def test_priority_inbox_post_wakes_first_past_caps(settings):
    settings = replace(settings, inbox_per_sender_hourly=1, inbox_global_hourly=1)
    store = JobStore(settings.data_dir / "jobs.sqlite3")
    turns = Turns(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Idle", "", "codex", None)
    other = store.create_bot("Other", "", "codex", None)
    store.create("owner chat", "codex", bot_id=other.id, source="chat")
    assert grok_bridge.post_inbox(to="Idle", text="hi", sender="Grok", turns=turns)[0] == "queued"
    with pytest.raises(grok_bridge.DeskError, match="limit"):
        grok_bridge.post_inbox(to="Idle", text="again", sender="Grok", turns=turns)
    status, job_id = grok_bridge.post_inbox(to="Idle", text="wake now", sender="Grok", turns=turns, priority=True)
    assert status == "queued" and _priority(store, job_id) == URGENT_PRIORITY
    first = store.claim(["desktop-1"], {"codex"})
    assert str(first.id) == job_id  # ahead of the earlier inbox turn and another bot's owner chat


def test_priority_turn_skips_the_budget_hold(settings, monkeypatch):
    store = JobStore(settings.data_dir / "jobs.sqlite3")
    turns = Turns(store, settings, {"codex": FakeAdapter()})
    bot = store.create_bot("Gated", "", "codex", None)
    store.update_bot(bot.id, scheduled_runs="gated")
    monkeypatch.setattr(usage, "over_budget", lambda db, bot_id, stamp: "the daily budget is used up")
    _, plain = turns.post_message(bot, "x", source="inbox", sender="Grok")
    _, urgent = turns.post_message(bot, "y", source="inbox", sender="Grok", urgent=True)
    with closing(store._connect()) as db:
        assert usage.budget_gate(db, str(plain.id), now()) is not None
        assert usage.budget_gate(db, str(urgent.id), now()) is None


def test_desk_inbox_priority_flag_and_api(settings, monkeypatch):
    sent = {}
    monkeypatch.setattr(grok_bridge, "call_api", lambda path, body, **kw: sent.update(body) or
                        {"status": "queued", "detail": "j"})
    assert grok_bridge.desk_inbox(["post", "--to", "A", "--from", "B", "--text", "t", "--priority"]) == 0
    assert sent["priority"] is True
    client = TestClient(create_app(settings, {"codex": FakeAdapter()}))
    with client:
        store = client.app.state.store
        store.create_bot("Idle", "", "codex", None)
        response = client.post("/v1/bridge/inbox", headers=H, json={"to": "Idle", "from": "G", "text": "x", "priority": True})
        assert response.json()["status"] == "queued"
        assert _priority(store, response.json()["detail"]) == URGENT_PRIORITY
        plain = client.post("/v1/bridge/inbox", headers=H, json={"to": "Idle", "from": "G", "text": "y"}).json()
        assert _priority(store, plain["detail"]) == 2


# --- A. refusals stay visible to the sender ---------------------------------------------------

def test_refused_and_undelivered_sends_leave_a_line_in_the_senders_chat(settings, monkeypatch):
    client = TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))
    with client:
        store = client.app.state.store
        ada = store.create_bot("Ada", "", "codex", None)
        store.create_bot("Bea", "", "codex", None)
        store.create("work", "codex", bot_id=ada.id, source="chat")
        job = store.claim(["desktop-1"], {"codex"})
        token = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        # refused (Bea's engine is off here): the error goes back to the turn, and a line stays in the chat
        refused = client.post(f"/v1/bots/{ada.id}/message-agent", headers=token, json={"to": "Bea", "body": "one"})
        assert refused.status_code == 400
        assert any(line.startswith("Message to Bea not sent: ") for line in _system_lines(store, ada))
        monkeypatch.setattr(grok_bridge, "send_to_grok", lambda **kw: "dropped: daily limit reached")
        assert client.post("/v1/bridge/grok/send", headers=token,
                           json={"to": "Engineering Lead", "kind": "result", "text": "x"}).status_code == 200
        monkeypatch.setattr(grok_bridge, "send_to_grok", lambda **kw: "dropped: kind=ack is not sent")
        client.post("/v1/bridge/grok/send", headers=token, json={"to": "X", "kind": "ack", "text": "x"})

        def boom(**kw):
            raise grok_bridge.DeskError("webhook HTTP 400: bad request")
        monkeypatch.setattr(grok_bridge, "send_to_grok", boom)
        assert client.post("/v1/bridge/grok/send", headers=token,
                           json={"to": "Engineering Lead", "kind": "result", "text": "x"}).status_code == 502
        lines = [line for line in _system_lines(store, ada) if line.startswith("Grok send")]
        assert len(lines) == 2 and "daily limit" in lines[0] and "HTTP 400" in lines[1]


# --- D. rules sync ----------------------------------------------------------------------------

RULES = """bot: Engineering Lead (BO)
# Rules from the Grok handoff
- [2026-11-22 07:12 ET | the owner, via grok handoff engineering-lead.md | id=demo-item-limit] Remove DEMO_ITEM_LIMIT.
- [2026-11-21 | grok handoff mpg-lead.md] Keep the sample folder read-only; write results to a new folder.
not a rule line
"""


def test_rules_sync_imports_once_dated_and_updates_changed(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Engineering Lead (BO)", "", "claude", None)
    memory = Memory(tmp_path / "data")
    memory.write_bot(bot.id, "- my own note\n")
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "el.md").write_text(RULES)
    (folder / "nobody.md").write_text("- [2026-11-21 | x] y\n")
    result = rules_sync.run(store, memory, folder)
    by_file = {entry["file"]: entry for entry in result["files"]}
    assert by_file["el.md"]["added"] == 2 and "error" in by_file["nobody.md"]
    text = memory.read_bot(bot.id)
    assert text.startswith("- my own note\n")
    assert "- RULE (2026-11-22 07:12 ET; source: the owner, via grok handoff engineering-lead.md) [rule:demo-item-limit]" in text
    ages = freshness.load(memory.bot_path(bot.id))
    stamps = sorted({entry["at"][:10] for entry in ages.values() if entry["by"] == rules_sync.WHO})
    assert stamps == ["2026-11-21", "2026-11-22"]
    shown = freshness.annotate(text, memory.bot_path(bot.id), now=datetime.fromisoformat("2026-11-26T12:00:00+00:00"))
    assert "Nov 22 · 3d · rules sync" in shown and "Nov 21 · 4d · rules sync" in shown

    # unchanged file: skipped; forced: nothing duplicated
    assert rules_sync.run(store, memory, folder)["files"] == [by_file["nobody.md"] | {"file": "nobody.md"}]
    again = rules_sync.run(store, memory, folder, only_changed=False)
    assert {e["file"]: e.get("present") for e in again["files"]}["el.md"] == 2
    assert memory.read_bot(bot.id) == text
    # a changed rule (same id) is replaced in place; a new one is added under the section
    (folder / "el.md").write_text(RULES.replace("Remove DEMO_ITEM_LIMIT.", "Remove DEMO_ITEM_LIMIT entirely.")
                                  + "- [2026-11-23 | the owner] One folder per task.\n")
    changed = {e["file"]: e for e in rules_sync.run(store, memory, folder)["files"]}["el.md"]
    assert (changed["changed"], changed["added"], changed["present"]) == (1, 1, 1)
    final = memory.read_bot(bot.id)
    assert final.count("[rule:demo-item-limit]") == 1 and "entirely" in final
    assert final.count("One folder per task") == 1
    assert not rules_sync.due(store)


def test_rules_sync_file_name_names_the_bot_and_existing_text_is_not_duplicated(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("BT Lead (BO)", "", "claude", None)
    memory = Memory(tmp_path / "data")
    memory.write_bot(bot.id, "- Only the owner's own words approve.\n")
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "bt-lead.md").write_text("- [2026-11-21 | grok handoff bt-lead.md] Only the owner's own words approve.\n")
    entry = rules_sync.run(store, memory, folder)["files"][0]
    assert entry["bot"] == "BT Lead (BO)" and entry["present"] == 1
    assert rules_sync.run(store, memory, tmp_path / "missing").get("error")


# --- E. a note written to the bot's own memory is data ------------------------------------------

E8 = runpy.run_path(str(Path(__file__).resolve().parent / "test_gate_gap.py"))["E8"]
WS = "/srv/data/workspaces/j1"


def _gate(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    bot = store.create_bot("Ada", "", "claude", None)
    gate = Rules(store)
    for kind, pattern in E8:
        gate.add_rule(bot_id=bot.id, effect="ask", kind=kind, pattern=pattern, note="E8")
    job = store.create("work", "claude", bot_id=bot.id, source="inbox")
    return gate, store.get(job.id)


def _judge(gate, job, command, cwd=WS):
    return gate.evaluate(job, Action(tool="Bash", command=command, cwd=cwd, workspace=WS, data={"command": command}))[0]


NOTE = "- Since 0.12.26 BO has app admin. No sudo. ssh to the box is off; git push origin bot/x is fine.\n"


@pytest.mark.parametrize("command", [
    f"cat >> memory.md <<'EOF'\n{NOTE}EOF",
    f"cat >> {WS}/memory.md <<'EOF'\n{NOTE}EOF",
    f'tee -a WORKING.md <<"EOF" >/dev/null\n{NOTE}EOF',
    f"cat <<'EOF' >> memory.md\n{NOTE}EOF",
    "cat >> memory.md <<EOF\n- plain note, git push later\nEOF",
])
def test_memory_notes_with_risky_words_do_not_card(tmp_path, command):
    gate, job = _gate(tmp_path)
    assert memory_note_command(Action(tool="Bash", command=command, cwd=WS, workspace=WS)) is not None
    assert _judge(gate, job, command) == "allow"


@pytest.mark.parametrize("command,cwd", [
    (f"cat >> memory.md <<'EOF'\n{NOTE}EOF\ngit push origin bot/x", WS),        # something else runs
    (f"cat >> memory.md <<'EOF'\n{NOTE}EOF && git push", WS),
    ("cat >> memory.md <<EOF\n$(git push origin main)\nEOF", WS),               # unquoted: the shell runs $( )
    ("cat >> memory.md <<EOF\n`sudo id`\nEOF", WS),
    (f"cat >> memory.md <<'EOF'\n{NOTE}EOF", "/tmp"),                          # another folder's memory.md
    (f"cat >> /srv/data/workspaces/j2/memory.md <<'EOF'\n{NOTE}EOF", WS),       # another job's workspace
    (f"cat > ~/.claude/projects/p/memory/x.md <<'EOF'\n{NOTE}EOF", WS),         # outside the workspace
    (f"bash <<'EOF'\ngit push origin main\nEOF", WS),
])
def test_other_heredocs_still_ask(tmp_path, command, cwd):
    gate, job = _gate(tmp_path)
    assert memory_note_command(Action(tool="Bash", command=command, cwd=cwd, workspace=WS)) is None
    assert _judge(gate, job, command, cwd) in ("ask", "review")  # review: sudo goes to the auto-review
