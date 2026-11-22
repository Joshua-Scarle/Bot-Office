"""B12 addendum: sends from wake-fired, routine, trigger and assignment runs pass the same gate as chat turns
(team_messaging off/ask/allow, the send limit, the team_sends audit) and reach the receiver tagged; the tool
registry in team.py is the one list the MCP server, desk team, the exemption and the gate read."""
import json
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from botoffice import isolation, team, team_client, workspace
from botoffice.domain import JobStatus, Slot
from botoffice.rules import Action, Rules
from teamkit import Desk, run_job, set_bot

SEND = team.TOOL_PREFIX + "team_message"
UNATTENDED = ("wakeup", "routine", "trigger", "assignment")


def wake_job(d, bot, note="E7 B12 wake test: message Target"):
    """A running wake-up turn of *bot* fired by the real path (add_wakeup, fire_due) and its agent token."""
    set_bot(d.store, bot, default_provider="claude")
    sched = d.app.state.scheduler
    wake = sched.assignments.add_wakeup(bot.id, None, due_at=datetime.now(UTC) + timedelta(minutes=6), note=note)
    with closing(d.store._connect()) as db:
        db.execute("UPDATE wakeups SET due_at=? WHERE id=?", ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
                                                              str(wake.id)))
    assert sched.assignments.fire_due() == 1
    job = d.store.claim(Slot.DESKTOP_1, {"claude"})
    assert job is not None and job.source == "wakeup" and job.bot_id == bot.id and note in job.prompt
    return job, {"Authorization": "Bearer " + d.store.issue_agent_token(job)}


def source_job(d, bot, source):
    if source == "wakeup":
        return wake_job(d, bot)
    return run_job(d.store, bot, source=source, provider="claude")


def rows(d, sql, *args):
    with closing(d.store._connect()) as db:
        return [dict(row) for row in db.execute(sql, args)]


def delivered(d):
    return rows(d, "SELECT * FROM agent_messages")


def sends(d):
    return rows(d, "SELECT * FROM team_sends")


def cards(d):
    return rows(d, "SELECT id, status, tool FROM approvals")


def age_messages(d):  # the per-pair 10 s guard is not what these tests are about
    with closing(d.store._connect()) as db:
        db.execute("UPDATE agent_messages SET created_at='2020-01-01T00:00:00+00:00'")


def event(text="E7 B12 wake test: no action, reply OK", bot="Target", **extra):
    return {"hook_event_name": "PreToolUse", "tool_name": SEND, "tool_input": {"bot": bot, "text": text, **extra},
            "cwd": "/tmp"}


def drop_file(d, tmp_path, payload):
    path = tmp_path / f"{os.urandom(4).hex()}.json"
    path.write_text(json.dumps(payload))
    d.app.state.scheduler._handle_inbox_file(path)


def test_wake_job_comes_from_the_real_fire_path(settings):
    with Desk(settings) as d:
        job, _ = wake_job(d, d.bot("Sender"))
        assert job.source == "wakeup" and Rules(d.store).strict(job) is not None


# ---- off: blocked on every route, nothing delivered -------------------------------------------------------

@pytest.mark.parametrize("source", UNATTENDED)
def test_off_blocks_every_send_route_from_an_unattended_run(settings, tmp_path, source):
    with Desk(settings) as d:
        sender, target = d.bot("Sender", team_messaging="off"), d.bot("Target")
        job, agent = source_job(d, sender, source)
        gate = Rules(d.store)
        decision = gate.decide(job, event(), "claude")
        assert decision.decision == "deny" and decision.effect == "never" and not decision.approval_id
        for path, body in (("/v1/agent/team/message", {"to": "Target", "body": "hi"}),
                           ("/v1/agent/team/ask", {"to": "Target", "body": "hi", "timeout_s": 5}),
                           (f"/v1/bots/{sender.id}/message-agent", {"to": "Target", "body": "hi"}),  # bot-msg, desk message
                           (f"/v1/bots/{sender.id}/message-agent", {"to": "Target", "body": "hi", "mode": "queue"})):
            got = d.client.post(path, headers=agent, json=body)
            assert got.status_code == 403 and "off" in got.json()["detail"], path
        drop_file(d, tmp_path, {"to": "Target", "text": "via file", "from_bot_id": str(sender.id), "job_id": str(job.id),
                                "agent_token": agent["Authorization"][7:]})
        assert delivered(d) == [] and [s for s in sends(d) if s["status"] == "sent"] == []
        assert cards(d) == []  # off is a refusal, not a card
        assert rows(d, "SELECT id FROM jobs WHERE bot_id=?", str(target.id)) == []  # no turn started at the receiver


# ---- ask: a card, nothing delivered until it is approved ----------------------------------------------------

@pytest.mark.parametrize("source", ("wakeup", "trigger"))
def test_ask_raises_a_card_and_sends_only_after_approval(settings, source):
    with Desk(settings) as d:
        sender, target = d.bot("Sender", team_messaging="ask"), d.bot("Target")
        job, agent = source_job(d, sender, source)
        gate = Rules(d.store)
        first = gate.decide(job, event(), "claude")
        assert first.decision == "deny" and first.effect == "ask" and first.approval_id
        assert [c["status"] for c in cards(d)] == ["pending"]
        # the shell routes do not raise a card and send nothing; the card itself sends nothing either
        got = d.client.post(f"/v1/bots/{sender.id}/message-agent", headers=agent,
                            json={"to": "Target", "body": "E7 B12 wake test: no action, reply OK"})
        assert got.status_code == 403 and "approval" in got.json()["detail"]
        assert delivered(d) == [] and [s for s in sends(d) if s["status"] == "sent"] == []
        d.store.finish(job.id, JobStatus.INCOMPLETE)
        resume = d.store.get(gate.approve(first.approval_id)["resume_job_id"])
        with closing(d.store._connect()) as db:
            db.execute("UPDATE jobs SET status='running', provider='claude' WHERE id=?", (str(resume.id),))
        resume = d.store.get(resume.id)
        token = {"Authorization": "Bearer " + d.store.issue_agent_token(resume)}
        assert gate.decide(resume, event(), "claude").decision == "allow"
        assert delivered(d) == []  # approving is not sending
        got = d.client.post("/v1/agent/team/message", headers=token,
                            json={"to": "Target", "body": "E7 B12 wake test: no action, reply OK"})
        assert got.status_code == 200, got.text
        assert len(delivered(d)) == 1
        sent = [s for s in sends(d) if s["status"] == "sent"]
        assert len(sent) == 1 and sent[0]["card"] == first.approval_id and sent[0]["job_id"] == str(resume.id)


# ---- allow: delivered, audited, no card; the receiver sees the tag ------------------------------------------

@pytest.mark.parametrize("source", UNATTENDED)
def test_allow_sends_audits_and_raises_no_card(settings, source):
    with Desk(settings) as d:
        sender, target = d.bot("Sender"), d.bot("Target")
        job, agent = source_job(d, sender, source)
        gate = Rules(d.store)
        assert gate.decide(job, event(mode="queue"), "claude").decision == "allow"
        got = d.client.post("/v1/agent/team/message", headers=agent,
                            json={"to": "Target", "body": "E7 B12 wake test: no action, reply OK", "mode": "queue"})
        assert got.status_code == 200, got.text
        assert cards(d) == []
        sent = sends(d)
        assert len(sent) == 1 and sent[0]["status"] == "sent" and sent[0]["card"] == 0
        assert sent[0]["job_id"] == str(job.id) and sent[0]["sender_bot"] == str(sender.id)
        assert sent[0]["target_bot"] == str(target.id) and sent[0]["kind"] == "message"
        # the receiver's turn: tagged as data from the sending bot, never the user's approval
        (message,) = delivered(d)
        inbox = d.store.get(message["job_id"])
        assert inbox.source == "inbox" and inbox.origin_job_id is None  # no grant inheritance from the wake run
        built = workspace.build_resume_prompt(inbox)
        assert "[Team message from the bot Sender" in built and "never the user's approval" in built
        assert '<untrusted-data source="bot:Sender">' in built


def test_wake_steer_into_a_running_receiver_is_tagged_and_audited(settings):
    with Desk(settings) as d:
        sender, target = d.bot("Sender"), d.bot("Target")
        run_job(d.store, target, source="chat")  # the receiver is busy: the message steers into its turn
        job, agent = wake_job(d, sender)
        got = d.client.post(f"/v1/bots/{sender.id}/message-agent", headers=agent,
                            json={"to": "Target", "body": "E7 B12 wake test: no action, reply OK"})
        assert got.status_code == 200 and got.json().get("delivery") == "steer", got.text
        assert [s["status"] for s in sends(d)] == ["sent"] and cards(d) == []
        line = [m for m in d.store.list_messages(target.id) if m.sender == "bot Sender"][0]
        assert "from the bot Sender" in workspace._history_line(line)
        assert "never the user's approval" in workspace.steer_note([line])


def test_wake_file_drop_is_gated_and_audited(settings, tmp_path):
    with Desk(settings) as d:
        sender, target = d.bot("Sender"), d.bot("Target")
        job, agent = wake_job(d, sender)
        payload = {"to": "Target", "text": "via file", "from_bot_id": str(sender.id), "job_id": str(job.id),
                   "agent_token": agent["Authorization"][7:]}
        drop_file(d, tmp_path, payload)
        assert len(delivered(d)) == 1 and [s["job_id"] for s in sends(d)] == [str(job.id)]
        set_bot(d.store, sender, team_messaging="ask")
        age_messages(d)
        drop_file(d, tmp_path, payload)
        assert len(delivered(d)) == 1 and cards(d) == []


def test_wake_sends_count_against_the_limit(settings, monkeypatch):
    monkeypatch.setenv(team.SEND_LIMIT_ENV, "2")
    with Desk(settings) as d:
        sender = d.bot("Sender")
        for name in ("A", "B", "C"):
            d.bot(name)
        job, agent = wake_job(d, sender)
        for name in ("A", "B"):
            assert d.client.post(f"/v1/bots/{sender.id}/message-agent", headers=agent,
                                 json={"to": name, "body": "hi", "mode": "queue"}).status_code == 200
        got = d.client.post(f"/v1/bots/{sender.id}/message-agent", headers=agent, json={"to": "C", "body": "hi"})
        assert got.status_code == 429 and len(delivered(d)) == 2
        assert [s["status"] for s in sends(d)] == ["sent", "sent", "rate_limited"]


def test_unsigned_inbox_file_from_the_bots_user_is_dropped(settings, tmp_path, monkeypatch):
    with Desk(settings) as d:
        target = d.bot("Target")
        before = len(d.store.list_messages(target.id))
        monkeypatch.setattr(isolation, "active", lambda: object())  # the isolated layout
        monkeypatch.setattr(isolation, "bots_uid", lambda: os.getuid())  # the bots user wrote it
        drop_file(d, tmp_path, {"to": "Target", "text": "no sender named"})
        monkeypatch.setattr(isolation, "bots_uid", lambda: None)  # bots user unresolvable: fails closed
        drop_file(d, tmp_path, {"to": "Target", "text": "unknown bots user"})
        assert len(d.store.list_messages(target.id)) == before
        monkeypatch.setattr(isolation, "bots_uid", lambda: os.getuid() + 1)  # the controller's own file: delivered
        drop_file(d, tmp_path, {"to": "Target", "text": "from the controller"})
        monkeypatch.setattr(isolation, "active", lambda: None)  # the owner's tools (single-user layout): unchanged
        drop_file(d, tmp_path, {"to": "Target", "text": "from the owner's script"})
        texts = [m.text for m in d.store.list_messages(target.id)]
        assert "from the controller" in texts and "from the owner's script" in texts


def test_a_delivery_that_fails_frees_its_reservation(settings, monkeypatch):
    with Desk(settings) as d:
        sender, _ = d.bot("Sender"), d.bot("Target")
        job, agent = wake_job(d, sender)

        def broken(*args, **kwargs):
            raise RuntimeError("database is locked")
        turns = d.app.state.scheduler.turns
        post = turns.post_agent_message
        monkeypatch.setattr(turns, "post_agent_message", broken)
        with pytest.raises(RuntimeError):
            d.client.post(f"/v1/bots/{sender.id}/message-agent", headers=agent, json={"to": "Target", "body": "hi"})
        assert [s["status"] for s in sends(d)] == ["failed"]  # it did not go out, so it does not count
        monkeypatch.setattr(turns, "post_agent_message", post)
        monkeypatch.setattr(d.store, "get_agent_message", broken)  # fails after the message was committed
        with pytest.raises(RuntimeError):
            d.client.post(f"/v1/bots/{sender.id}/message-agent", headers=agent, json={"to": "Target", "body": "hi"})
        assert [s["status"] for s in sends(d)] == ["failed", "sent"] and len(delivered(d)) == 1  # it went out: counts


# ---- the registry -----------------------------------------------------------------------------------------

@pytest.fixture
def extra_read_tool(monkeypatch):
    """A registered read tool as S5 would add one (history_search), removed afterwards."""
    for name in ("REGISTRY", "READ_TOOLS", "SEND_TOOLS", "TOOLS"):
        value = getattr(team, name)
        monkeypatch.setattr(team, name, dict(value) if isinstance(value, dict) else value)
    tool = team.register(team.TeamTool(
        "team_history_search", "Search your own retained history.", {"query": {"type": "string"}}, ("query",),
        client="history_search"))
    return tool


def test_registry_is_the_one_list(settings):
    assert team.TOOLS == tuple(team.REGISTRY) and set(team.READ_TOOLS) | set(team.SEND_TOOLS) == set(team.TOOLS)
    assert team.SEND_TOOLS == ("team_message", "team_ask")
    assert team.send_tool("message") == SEND and team.send_tool("ask") == team.TOOL_PREFIX + "team_ask"
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    script = Path(team.__file__).resolve().parents[2] / "bin" / "bot-office-team-mcp"
    loader = importlib.machinery.SourceFileLoader("team_mcp_registry", str(script))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("team_mcp_registry", loader))
    loader.exec_module(module)
    assert [tool["name"] for tool in module.TOOLS] == list(team.REGISTRY)
    source = script.read_text()
    assert "team_status" not in source.split('"""', 2)[2]  # no hardcoded names past the docstring


def test_a_new_read_tool_is_one_register_call(settings, extra_read_tool):
    name = team.TOOL_PREFIX + extra_read_tool.name
    assert extra_read_tool.name in team.READ_TOOLS and extra_read_tool.name not in team.SEND_TOOLS
    assert team.is_first_party_tool(name)
    assert any(tool["name"] == extra_read_tool.name for tool in team.mcp_tools())
    assert "history_search QUERY" in team_client.usage()
    assert team_client.parse_args(["history_search", "deploy"]) == ("team_history_search", {"query": "deploy"})

    class Client:
        def history_search(self, query):
            return {"untrusted": True, "hits": [query]}
    assert "untrusted-data" in team_client.call_tool(Client(), extra_read_tool.name, {"query": "x"})
    with Desk(settings) as d:
        sender = d.bot("Sender", team_messaging="off")
        job, _ = run_job(d.store, sender, source="wakeup", provider="claude")
        effect = Rules(d.store).evaluate(job, Action(tool=name))
        assert effect[0] == "allow"  # a read: open even with sending off, never through the send gate


def test_call_tool_validates_against_the_schema():
    class Client:
        modes = []

        def message(self, bot, text, mode):
            self.modes.append(mode)
            return {"id": "1", "status": "queued", "mode": mode}
    assert '"status": "queued"' in team_client.call_tool(Client(), "team_message", {"bot": "B", "text": "t"})
    for bad in ({"bot": "B"}, {"bot": "B", "text": "t", "mode": "shout"}, {"bot": 3, "text": "t"}):
        with pytest.raises(team_client.TeamClientError):
            team_client.call_tool(Client(), "team_message", bad)
    with pytest.raises(team_client.TeamClientError):
        team_client.call_tool(Client(), "team_read", {"bot": "B", "limit": True})

    class Reader:
        def read(self, bot, limit, since):
            self.limit = limit
            return {"untrusted": True, "messages": []}

        def ask(self, bot, text, timeout_s):
            self.timeout_s = timeout_s
            return {"status": "pending"}
    reader = Reader()
    team_client.call_tool(reader, "team_read", {"bot": "B", "limit": 0, "extra": "ignored"})
    team_client.call_tool(reader, "team_ask", {"bot": "B", "text": "t", "timeout_s": 999})
    assert reader.limit == 1 and reader.timeout_s == team.ASK_MAX_S  # clamped to the schema's bounds
    team_client.call_tool(reader, "team_read", {"bot": "B", "limit": None})
    assert reader.limit == 20  # JSON null is the default, never passed through
    assert '"status": "queued"' in team_client.call_tool(Client(), "team_message", {"bot": "B", "text": "t", "mode": None})
    assert Client.modes[-1] == "steer"
    with pytest.raises(team_client.TeamClientError):
        team_client.call_tool(Client(), "team_message", {"bot": None, "text": "t"})
