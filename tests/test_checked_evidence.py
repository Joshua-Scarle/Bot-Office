"""0.12.20: done-evidence is checked by Bot-Office (a file in the bot's workspace, a commit, a link that
answers, test output in the job's own log), never taken on the bot's word; and a reply cut by the engine's
output limit is published cut with the 20k notice, not replaced by the short note that followed it."""
import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botoffice import evidence
from botoffice.api import create_app
from botoffice.domain import JobStatus, Slot
from botoffice.providers.base import RunResult
from botoffice.providers.streams import ClaudeParser
from botoffice.scheduler import REPLY_LIMIT, Scheduler
from botoffice.store import JobStore
from test_incomplete import H

# job 7954e768 (live, Nov 21): three words of nonsense were recorded as a verified done
NONSENSE = "Sandbox routing test.\n\nBOT-OFFICE-STATUS: done — red lunch widget"
# job d2d28909 (live, Nov 21): said it could not do the work, then marked itself done
COULDNT = ("I can't tell you. This trial copy has no view of what the leads or DPEs have asked for, so I can't "
           "answer from real data.\n\n- **No live state:** I have no GitHub, CI or Railway state to check.\n\n"
           "If you paste in the pending lead or DPE messages, I'll sort them.")
# job 87b14d32 (live, Nov 21): a reply cut at the output-token limit, then a short note as the CLI's result
APOLOGY = ("The length-test reply hit the output token limit, so I can't confirm it reached the required 21,500 "
           "characters or the closing `BOT-OFFICE-STATUS` line. I didn't count the characters, since no tools were used.")
TICKS = "tick " * 64_000


def _claude_tool_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"tool_use_id": "toolu_1", "type": "tool_result", "content": text}]}})


def _claude_text_line(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


class Turn:
    """A turn that may write files into its job directory and a CLI log, stream a draft, and return a final."""

    name = "codex"
    enabled = True

    def __init__(self, final, *, files=None, log="log", draft=None, prepare=None):
        self.final, self.files, self.log, self.draft, self.prepare = final, files or {}, log, draft, prepare

    async def login_status(self):
        return "ok"

    async def run(self, job, workspace, log_path, timeout):
        from botoffice.streaming import emit_event

        for name, text in self.files.items():
            (workspace / name).parent.mkdir(parents=True, exist_ok=True)
            (workspace / name).write_text(text)
        if self.prepare:
            self.prepare(workspace)
        log_path.write_text(self.log)
        if self.draft is not None:
            emit_event("text", text=self.draft)
        return RunResult("succeeded", exit_code=0, final_text=self.final)


def turn(settings, adapter):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    _, job = scheduler.turns.post_message(bot, "do the thing")
    asyncio.run(scheduler._execute(Slot.DESKTOP_1, store.claim(Slot.DESKTOP_1, {"codex"})))
    return store, bot, store.get(job.id)


def reply(store, bot):
    return [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1]


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(workspace):
    repo = workspace / "proj"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "first")
    (workspace / "sha").write_text(git(repo, "rev-parse", "HEAD"))


# --- B7: nonsense, files, commits, test output -------------------------------------------------------------

def test_nonsense_evidence_is_not_verified(settings):
    store, bot, job = turn(settings, Turn(NONSENSE))
    assert job.status == JobStatus.SUCCEEDED and job.outcome == "done" and job.verified == 0
    assert job.evidence == "red lunch widget"  # kept, so the page shows what was cited
    message = reply(store, bot)
    assert message.unverified is True and message.evidence == "red lunch widget"


def test_a_real_file_in_the_workspace_is_verified(settings):
    store, bot, job = turn(settings, Turn("Wrote it.\nBOT-OFFICE-STATUS: done — wrote notes/report.md and read it back",
                                          files={"notes/report.md": "# Report\n"}))
    assert job.outcome == "done" and job.verified == 1
    assert "Bot-Office checked: file notes/report.md exists" in job.evidence
    assert reply(store, bot).unverified is False


def test_an_absolute_path_into_another_turn_of_the_same_bot_is_verified(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    bot = store.create_bot("Ada", "", "codex", None)
    earlier = store.create("earlier", "codex", bot_id=bot.id, source="chat")
    stranger = store.create("other", "codex", bot_id=store.create_bot("Bea", "", "codex", None).id, source="chat")
    ws = settings.data_dir / "workspaces"
    for job in (earlier, stranger):
        (ws / str(job.id)).mkdir(parents=True)
        (ws / str(job.id) / "out.csv").write_text("a,b\n")
    current = store.create("now", "codex", bot_id=bot.id, source="chat")
    scope = evidence.job_scope(settings.data_dir, store, current, network=False)
    assert evidence.check(f"wrote {ws / str(earlier.id) / 'out.csv'}", scope)
    assert evidence.check(f"wrote {ws / str(stranger.id) / 'out.csv'}", scope) is None  # another bot's turn


@pytest.mark.parametrize("cited", [
    "wrote notes/missing.md",  # does not exist
    "wrote ../../agentdesk.sqlite3",  # escapes with ..
    "wrote /etc/hostname",  # outside the workspace
    "wrote notes/empty.md",  # empty
    "wrote link.md",  # a symlink to a file outside
    "wrote linkdir/hostname",  # through a symlinked directory
    "checked SELF.md and prompt.txt",  # written by Bot-Office into every job directory
])
def test_missing_escaping_or_generated_paths_are_not_verified(settings, cited):
    def prepare(workspace):
        (workspace / "notes").mkdir(exist_ok=True)
        (workspace / "notes" / "empty.md").write_text("")
        os.symlink("/etc/hostname", workspace / "link.md")
        os.symlink("/etc", workspace / "linkdir")

    _, _, job = turn(settings, Turn(f"Done.\nBOT-OFFICE-STATUS: done — {cited}", prepare=prepare))
    assert job.outcome == "done" and job.verified == 0, job.evidence


def test_a_real_commit_is_verified_and_a_bogus_one_is_not(settings):
    store, bot, job = turn(settings, Turn("Done.\nBOT-OFFICE-STATUS: done — committed it", prepare=make_repo))
    sha = (settings.data_dir / "workspaces" / str(job.id) / "sha").read_text()
    scope = evidence.job_scope(settings.data_dir, store, job, network=False)
    assert evidence.check(f"committed {sha[:7]}", scope) == f"commit {sha[:7]} is in proj"
    assert evidence.check(f"committed {sha}", scope)
    assert evidence.check("committed deadbee1", scope) is None
    assert evidence.check("committed 1234567890abcdef1234567890abcdef12345678", scope) is None


def test_a_commit_turn_end_to_end(settings):
    def prepare(workspace):
        make_repo(workspace)
        (workspace / "reply.txt").write_text(
            "Committed.\nBOT-OFFICE-STATUS: done — committed " + (workspace / "sha").read_text()[:9])
        (workspace / "sha").unlink()  # the commit alone has to carry it

    _, _, job = turn(settings, Turn("ignored", prepare=prepare))
    assert job.outcome == "done" and job.verified == 1 and "is in proj" in job.evidence


def test_a_commit_of_a_repository_around_the_data_dir_does_not_count(settings):
    """The live data dir sits inside the Bot-Office checkout; a repository around the job directory is not
    one in the bot's workspace (only .git directories inside it are asked)."""
    settings.ensure_dirs()
    git(settings.data_dir, "init", "-q")
    (settings.data_dir / "x.txt").write_text("x")
    git(settings.data_dir, "add", "x.txt")
    git(settings.data_dir, "commit", "-qm", "outer")
    head = git(settings.data_dir, "rev-parse", "HEAD")
    _, _, job = turn(settings, Turn(f"Done.\nBOT-OFFICE-STATUS: done — commit {head[:10]}"))
    assert job.outcome == "done" and job.verified == 0


def test_test_output_only_in_the_reply_is_not_verified(settings):
    log = _claude_text_line("ran pytest: 12 passed") + "\n"  # the bot's own words in the log do not count
    _, _, job = turn(settings, Turn("All good.\nBOT-OFFICE-STATUS: done — ran pytest, 12 passed", log=log))
    assert job.outcome == "done" and job.verified == 0


def test_test_output_in_the_jobs_own_command_output_is_verified(settings):
    log = _claude_tool_line("============ 12 passed in 0.31s ============") + "\n"
    _, _, job = turn(settings, Turn("All good.\nBOT-OFFICE-STATUS: done — ran pytest, 12 passed", log=log))
    assert job.verified == 1 and 'test output "12 passed"' in job.evidence


@pytest.mark.parametrize("line", [
    json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "Tests: 12 passed, 12 total"}}),
    json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {"output": "12 passing (40ms)\n12 passed"}}}),
])
def test_codex_and_opencode_command_output_count(tmp_path, line):
    log = tmp_path / "job.log"
    log.write_text(line + "\n")
    assert evidence.check("ran the suite: 12 passed", evidence.Scope(roots=[tmp_path], log_path=log, network=False))


def test_a_passing_count_on_a_line_with_failures_does_not_count(tmp_path):
    log = tmp_path / "job.log"
    log.write_text(_claude_tool_line("===== 3 failed, 12 passed in 1.2s =====") + "\n")
    assert evidence.check("ran pytest: 12 passed", evidence.Scope(roots=[tmp_path], log_path=log, network=False)) is None


def test_couldnt_do_it_with_valid_evidence_is_not_done(settings):
    text = COULDNT + "\n\nBOT-OFFICE-STATUS: done — wrote notes/report.md"
    store, bot, job = turn(settings, Turn(text, files={"notes/report.md": "real file"}))
    assert job.status == JobStatus.INCOMPLETE and job.outcome.startswith(("blocked:", "waiting_on_user:"))
    assert job.verified is None


# --- links -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://127.0.0.1:9/x", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/",
                                 "http://[::1]/", "http://192.168.1.1/", "http://0.0.0.0/", "ftp://example.com/x",
                                 "http://user:pw@example.com/"])
def test_private_or_odd_links_are_refused_before_any_connection(tmp_path, url):
    resolved = []

    def resolve(host, port, **kwargs):
        resolved.append(host)
        return [(2, 1, 6, "", (host.strip("[]"), port))]

    scope = evidence.Scope(roots=[tmp_path], network=True, resolve=resolve)
    assert evidence.check(f"checked {url}", scope) is None


def test_a_host_that_resolves_to_a_private_address_is_refused(tmp_path):
    scope = evidence.Scope(roots=[tmp_path], resolve=lambda host, port, **kw: [(2, 1, 6, "", ("10.1.2.3", port))])
    assert evidence.check("checked https://intranet.example.com/status", scope) is None
    mixed = evidence.Scope(roots=[tmp_path], resolve=lambda host, port, **kw: [
        (2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("127.0.0.1", port))])
    assert evidence.check("checked https://rebind.example.com/", mixed) is None


@pytest.fixture
def web(monkeypatch):
    """A local server standing in for a public one: "public.test" resolves to it and only that address counts."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(dict(self.headers))
            if self.path == "/ok":
                self.send_response(200)
            elif self.path == "/to-private":
                self.send_response(302)
                self.send_header("Location", "http://intranet.test/secret")
            elif self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
            else:
                self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(evidence, "public_address", lambda address: address == "127.0.0.1")

    def resolve(host, port, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1" if host == "public.test" else "10.0.0.9", port))]

    yield server.server_port, resolve, seen
    server.shutdown()
    server.server_close()


def test_a_link_that_answers_is_verified_and_others_are_not(tmp_path, web):
    port, resolve, seen = web
    scope = evidence.Scope(roots=[tmp_path], resolve=resolve)
    assert evidence.check(f"deployed, http://public.test:{port}/ok answers", scope).startswith("link ")
    assert all("Authorization" not in h and "Cookie" not in h for h in seen)  # nothing of ours is sent
    assert evidence.check(f"see http://public.test:{port}/missing", scope) is None  # 404
    assert evidence.check(f"see http://public.test:{port}/to-private", scope) is None  # redirect to a private host
    assert evidence.check(f"see http://public.test:{port}/loop", scope) is None  # too many redirects
    assert evidence.check(f"see http://public.test:{port}/ok", evidence.Scope(roots=[tmp_path], resolve=resolve,
                                                                         network=False)) is None


def test_a_slow_link_gives_up_within_the_budget(tmp_path, monkeypatch):
    import socket
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)  # accepts, never answers
    monkeypatch.setattr(evidence, "public_address", lambda address: True)
    monkeypatch.setattr(evidence, "URL_TIMEOUT", 0.5)
    monkeypatch.setattr(evidence, "URL_BUDGET", 1.0)
    scope = evidence.Scope(roots=[tmp_path], resolve=lambda host, port, **kw: [(2, 1, 6, "", ("127.0.0.1", port))])
    start = time.monotonic()
    assert evidence.check(f"see http://slow.test:{listener.getsockname()[1]}/", scope) is None
    assert time.monotonic() - start < 3
    listener.close()


# --- task evidence (bot-task done --evidence) ----------------------------------------------------------------

def idle():
    adapter = Turn("")
    adapter.enabled = False  # the app's scheduler leaves the test's jobs alone
    return adapter


@pytest.mark.continuation_defaults
@pytest.mark.parametrize("route", ["tasks", "assignments"])
def test_task_evidence_is_checked_and_nonsense_never_closes_a_task(settings, route):
    with TestClient(create_app(settings, {"codex": idle()})) as client:
        store = client.app.state.store
        bot = store.create_bot("Ada", "", "codex", None)
        item = client.post(f"/v1/bots/{bot.id}/assignments", headers=H, json={"title": "Shelf list"}).json()
        store.create("chat", "codex", bot_id=bot.id, source="chat")
        job = store.claim(["desktop-1"], {"codex"})
        agent = {"Authorization": "Bearer " + store.issue_agent_token(job)}
        workspace = settings.data_dir / "workspaces" / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        # out of the task's own turn: nonsense evidence is refused, the task stays open
        refused = client.post(f"/v1/agent/{route}/{item['id']}/status", headers=agent,
                              json={"state": "done", "evidence": "red lunch widget"})
        assert refused.status_code == 409, refused.text
        assert client.get(f"/v1/bots/{bot.id}/assignments", headers=H).json()[0]["status"] != "done"
        # a real file in the bot's workspace closes it, and the note says what Bot-Office checked
        (workspace / "shelf.md").write_text("- jars\n")
        done = client.post(f"/v1/agent/{route}/{item['id']}/status", headers=agent,
                           json={"state": "done", "evidence": "wrote shelf.md with the list"})
        assert done.status_code == 200, done.text
        task = client.get(f"/v1/bots/{bot.id}/assignments", headers=H).json()[0]
        assert task["status"] == "done" and "Bot-Office checked: file shelf.md exists" in task["status_note"]


def test_unchecked_task_evidence_is_checked_again_when_the_turn_ends(settings):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": Turn("")})
    bot = store.create_bot("Ada", "", "codex", None)
    item = scheduler.assignments.create(bot.id, {"title": "Fix", "goal": "fix it"})
    job = store.create("chat", "codex", bot_id=bot.id, source="chat")
    scheduler.assignments.report(item.id, bot.id, job.id, "waiting_on_user", "", "ran pytest: 12 passed")
    scheduler.assignments.report(item.id, bot.id, job.id, "waiting_on_user", "", "red lunch widget")
    log = settings.data_dir / "logs" / f"{job.id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(_claude_tool_line("== 12 passed in 2.00s ==") + "\n")
    scope = evidence.job_scope(settings.data_dir, store, job, network=False)
    assert scheduler.assignments.recheck_evidence(job.id, lambda text: evidence.check(text, scope)) == 1
    from botoffice.assignments import latest_evidence
    with __import__("contextlib").closing(store._connect()) as db:
        db.row_factory = __import__("sqlite3").Row
        assert "12 passed" in latest_evidence(db, str(item.id))
        kinds = [r[0] for r in db.execute("SELECT kind FROM assignment_events WHERE job_id=? AND kind LIKE 'evidence%' "
                                          "ORDER BY id", (str(job.id),))]
    assert kinds == ["evidence", "evidence_unchecked"]


# --- 20k: the cut notice ---------------------------------------------------------------------------------------

def test_an_over_limit_reply_is_cut_with_the_notice(settings):
    full = "x" * (REPLY_LIMIT + 4321)
    store, bot, job = turn(settings, Turn(full + "\nBOT-OFFICE-STATUS: done — wrote it"))
    message = reply(store, bot)
    assert message.cut_chars == 4321 and len(message.text) == REPLY_LIMIT and job.reply_text == full


def test_the_87b14d32_shape_shows_the_draft_cut_not_the_apology(settings):
    """Huge streamed draft, then the CLI's result is only a short note about the output limit."""
    draft = TICKS + "\n\n" + APOLOGY
    store, bot, job = turn(settings, Turn(APOLOGY, draft=draft))
    message = reply(store, bot)
    assert message.cut_chars == len(draft.strip()) - REPLY_LIMIT > 0
    assert message.text.startswith("tick tick") and APOLOGY not in message.text
    assert job.reply_text == draft.strip()  # the full text stays on the job
    with TestClient(create_app(settings, {"codex": idle()}, store=store)) as client:
        shown = [m for m in client.get(f"/v1/bots/{bot.id}/messages", headers=H).json() if m["role"] == "assistant"][-1]
        assert shown["cut_chars"] > 0


def test_claude_parser_keeps_the_message_cut_at_max_tokens():
    parser = ClaudeParser()
    lines = [
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me write it."}}},
        {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me write it."},
                                                      {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": TICKS}}},
        {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": APOLOGY}}},
        {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}},
        {"type": "result", "subtype": "success", "result": APOLOGY, "is_error": False},
    ]
    events = [e for line in lines for e in parser.feed(line)]
    final = events[-1]
    assert final[0] == "final" and final[1]["text"] == (TICKS + "\n\n" + APOLOGY).strip()
    assert "Let me write it." not in final[1]["text"]  # narration before the cut message is not the reply


def test_claude_parser_without_partials_also_sees_max_tokens():
    parser = ClaudeParser()
    parser.feed({"type": "assistant", "message": {"stop_reason": "max_tokens", "content": [{"type": "text", "text": TICKS}]}})
    parser.feed({"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": APOLOGY}]}})
    assert parser.feed({"type": "result", "result": APOLOGY, "is_error": False})[0][1]["text"].startswith("tick tick")


@pytest.mark.parametrize("draft,final", [
    ("Short draft.\n\nDone, wrote it.", "Done, wrote it."),  # under the limit
    ("Narration. " * 3000 + "\n\nAll done.", "All done."),  # long narration, a normal final: the final is the reply
])
def test_a_normal_reply_is_unchanged(settings, draft, final):
    store, bot, job = turn(settings, Turn(final + "\nBOT-OFFICE-STATUS: done — wrote it", draft=draft))
    message = reply(store, bot)
    assert message.cut_chars == 0 and message.text == final


def test_claude_parser_without_a_cut_reports_the_last_message():
    parser = ClaudeParser()
    parser.feed({"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "a"}]}})
    assert parser.feed({"type": "result", "result": "a", "is_error": False}) == [("final", {"text": "a"})]


# --- review r1: what the checks must not be fooled by --------------------------------------------------------

def test_a_workspace_root_swapped_for_a_link_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0\n")
    (tmp_path / "ws").symlink_to(outside)
    assert evidence.check("checked `passwd`", evidence.Scope(roots=[tmp_path / "ws"], network=False)) is None


def test_a_tool_call_input_is_not_tool_output(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("\n".join(json.dumps(line) for line in (
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "true", "output": "12 passed"}}]}},
        {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"output": "12 passed"}, "output": "ok"}}},
        {"type": "item.completed", "item": {"type": "function_call", "arguments": {"stdout": "12 passed"}}},
    )) + "\n")
    assert evidence.check("ran pytest: 12 passed", evidence.Scope(roots=[tmp_path], log_path=log, network=False)) is None


def test_evidence_that_names_a_failed_check_never_counts_even_with_a_real_file(tmp_path):
    (tmp_path / "report.txt").write_text("FAILED test_x\n")
    scope = evidence.Scope(roots=[tmp_path], network=False)
    assert evidence.check("see report.txt", scope)
    assert evidence.check("tests failed; see report.txt", scope) is None  # task evidence and re-checks use this too


def test_task_evidence_of_the_turn_does_not_cover_a_failed_status_line(settings):
    store, bot, job = turn(settings, Turn("Done.\nBOT-OFFICE-STATUS: done — ran pytest, 3 failed"))
    assert job.verified == 0
    with __import__("contextlib").closing(store._connect()) as db, db:
        db.execute("INSERT INTO assignment_events (assignment_id, kind, note, job_id, created_at) VALUES "
                   "('a', 'evidence', 'wrote x.md (Bot-Office checked: file x.md exists)', ?, '2026-11-21T00:00:00Z')",
                   (str(job.id),))
    message = reply(store, bot)
    assert message.unverified is True and message.evidence == "ran pytest, 3 failed"


@pytest.mark.parametrize("reason", ["nothing new, fixed the build", "nothing new; ran pytest, 12 passed"])
def test_nothing_new_is_quiet_only_as_the_whole_reason(reason):
    from botoffice import failures

    assert failures.judge_evidence("done", "nothing new") == (1, "nothing new")
    assert failures.judge_evidence("done", reason)[0] == 0


def test_a_repository_borrowing_objects_from_outside_does_not_count(settings):
    settings.ensure_dirs()
    outer = settings.data_dir.parent / "outer"
    outer.mkdir()
    make_repo(outer)
    head = (outer / "sha").read_text()

    def prepare(workspace):
        repo = workspace / "proj"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".git" / "objects" / "info" / "alternates").write_text(str(outer / "proj" / ".git" / "objects") + "\n")

    store, _, job = turn(settings, Turn(f"Done.\nBOT-OFFICE-STATUS: done — commit {head[:10]}", prepare=prepare))
    assert job.outcome == "done" and job.verified == 0


def test_an_all_letter_short_sha_is_a_candidate():
    assert [m.group(0) for m in evidence.SHA_RE.finditer("committed abcdefa and 1a2b3c4")] == ["abcdefa", "1a2b3c4"]


def test_a_limit_apology_in_reply_txt_does_not_replace_the_draft(settings):
    draft = TICKS + "\n\n" + APOLOGY

    def prepare(workspace):
        (workspace / "reply.txt").write_text(APOLOGY)

    store, bot, job = turn(settings, Turn(APOLOGY, draft=draft, prepare=prepare))
    message = reply(store, bot)
    assert message.cut_chars > 0 and message.text.startswith("tick tick") and job.reply_text == draft.strip()
