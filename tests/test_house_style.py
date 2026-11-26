"""0.12.36: one chat house style for every engine, and the light tidy-up of chat replies."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from botoffice import house_style, workspace
from botoffice.api import create_app
from botoffice.house_style import HOUSE_STYLE, tidy_reply
from botoffice.memory import Memory
from botoffice.providers.claude import ClaudeAdapter
from botoffice.providers.codex import CodexAdapter
from botoffice.providers.fake import FakeAdapter
from botoffice.store import JobStore
from botoffice.workspace import prepare_bot_workspace

H = {"Authorization": "Bearer test-token"}


def test_leading_short_header_is_dropped():
    assert tidy_reply("## Summary\n\nIt's live on 0.12.36.") == "It's live on 0.12.36."
    assert tidy_reply("# **Status**\nAll good.") == "All good."


def test_leading_long_header_keeps_its_words():
    assert tidy_reply("## The deploy finished cleanly tonight\nTests pass.") == (
        "The deploy finished cleanly tonight\nTests pass.")


def test_header_is_kept_on_a_long_reply_or_alone():
    long = "## Report\n" + "word " * 400
    assert tidy_reply(long) == long.strip("\n")
    assert tidy_reply("## Only a header") == "## Only a header"
    assert tidy_reply("Intro\n## Later header\nbody") == "Intro\n## Later header\nbody"  # only a leading one


def test_bold_labels_are_flattened():
    text = ("**What you need to do:** approve card #187.\n\n"
            "- **Health check:** 0.12.34, ok.\n- **Go-live**: finished.\n1. **Tests:** 103 pass\n"
            "- **Not reviewed** by Codex yet.")
    assert tidy_reply(text) == ("What you need to do: approve card #187.\n\n"
                                "- Health check: 0.12.34, ok.\n- Go-live: finished.\n1. Tests: 103 pass\n"
                                "- **Not reviewed** by Codex yet.")  # bold that isn't a label stays


def test_bold_mid_sentence_and_long_labels_stay():
    assert tidy_reply("Please **don't** restart it.") == "Please **don't** restart it."
    text = "**This is a very long bold sentence that is not a label:** x"
    assert tidy_reply(text) == text


def test_trailing_filler_goes():
    assert tidy_reply("It's live.\n\nLet me know if you need anything else!") == "It's live."
    assert tidy_reply("It's live. Let me know if anything looks off.") == "It's live."
    assert tidy_reply("Done.\nHope this helps.") == "Done."
    assert tidy_reply("Done.\nFeel free to ask for the full log.") == "Done."


def test_filler_alone_or_a_real_question_stays():
    assert tidy_reply("Let me know if you want it.") == "Let me know if you want it."
    assert tidy_reply("It's ready. Want me to deploy it?") == "It's ready. Want me to deploy it?"
    assert tidy_reply("Done.\nShould I merge it now?") == "Done.\nShould I merge it now?"


def test_code_blocks_are_never_touched():
    text = "```\n## not a header\n- **Key:** value\nLet me know if\n```"
    assert tidy_reply(text) == text
    text = "Here:\n```md\n- **Key:** value\n```\nLet me know if it works."
    assert tidy_reply(text) == "Here:\n```md\n- **Key:** value\n```"


def test_plain_replies_are_unchanged():
    for text in ("PONG", "It's live on 0.12.36 and the tests pass.", "- one\n- two\n- three", ""):
        assert tidy_reply(text) == text


def test_house_style_covers_the_owner_rules():
    style = HOUSE_STYLE.lower()
    for phrase in ("contractions", "1-2 sentences", "two short paragraphs", "answer first", "let me know if",
                   "no headers", "bold labels", "genuinely parallel", "bold rarely", "short labels", "`code` spans",
                   "no arrows", "emoji rarely", "renders markdown", "chat replies only"):
        assert phrase in style, phrase


def _job(**kw):
    base = {"provider": "claude", "model": None, "source": "chat", "origin_job_id": None, "prompt": "Status?",
            "bot_id": "b1", "continue_count": 0, "assignment_id": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_every_prompt_carries_the_style():
    job = _job()
    assert house_style.HOUSE_STYLE_REMINDER in workspace.build_resume_prompt(job)
    assert "in the house style" in workspace._reply_rule(job)
    assert "plain text" not in workspace._base_reply_rule(job)


@pytest.fixture
def bot_job(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot_id = client.post("/v1/bots", headers=H, json={"name": "styled"}).json()["id"]
        posted = client.post(f"/v1/bots/{bot_id}/messages", headers=H, json={"text": "status?"}).json()
        store = JobStore(settings.data_dir / "agentdesk.sqlite3")
        job = store.get(posted["job"]["id"])
        workspace = settings.data_dir / "test-workspace"
        workspace.mkdir()
        prompt, _, _ = prepare_bot_workspace(store, Memory(settings.data_dir), store.get_bot(bot_id), job, workspace)
        yield job, workspace, prompt


def test_fresh_prompt_and_cli_system_prompts_carry_the_style(bot_job):
    job, workspace, prompt = bot_job
    assert HOUSE_STYLE in prompt
    assert house_style.HOUSE_STYLE_REMINDER in (workspace / "prompt-resume.txt").read_text()
    for session in (None, "sess-1"):
        codex = CodexAdapter()._exec_args(workspace, job, session)
        assert "developer_instructions=" + json.dumps(HOUSE_STYLE) in codex
        claude = ClaudeAdapter()._exec_args(workspace, job, session)
        assert claude[claude.index("--append-system-prompt") + 1] == HOUSE_STYLE
    # not for a job without a bot (probes, reviews)
    bare = job.model_copy(update={"bot_id": None})
    assert not any("developer_instructions" in a for a in CodexAdapter()._exec_args(workspace, bare))
    assert "--append-system-prompt" not in ClaudeAdapter()._exec_args(workspace, bare)
