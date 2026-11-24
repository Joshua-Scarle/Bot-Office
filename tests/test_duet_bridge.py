import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from botoffice import duet_bridge
from botoffice.config import Settings
from botoffice.duet_bridge import DuetBridge, DuetError
from botoffice.store import JobStore

REPO = "owner/mailbox-repo"
BASE = "https://api.github.com"
COMMENTS = f"{BASE}/repos/{REPO}/issues/7/comments"
HEADER = "[Duet → Bot Office]"
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class Fake:
    """A fake GitHub: serves one comment list with an ETag and records every request."""

    def __init__(self):
        self.comments: list[dict] = []
        self.etag = 1
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.posted: list[dict] = []
        self.reactions: list[int] = []
        self.react_status = 201
        self.next_id = 9000
        self.review_urls: list[str] = []

    def add(self, body, login="owner-login", cid=None, stamp="2026-01-01T12:00:01Z"):
        cid = cid or len(self.comments) + 1
        self.comments.append({"id": cid, "body": body, "user": {"login": login},
                              "created_at": stamp, "updated_at": stamp})
        self.etag += 1
        return cid

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET" and url == f"{BASE}/user":
            return 200, {}, json.dumps({"login": "owner-login"}).encode()
        if method == "GET":
            tag = f'W/"{self.etag}"'
            if headers.get("If-None-Match") == tag:
                return 304, {}, b""
            since = unquote(url.split("since=")[1].split("&")[0])
            fresh = [c for c in self.comments if c["updated_at"] >= since]
            return 200, {"etag": tag}, json.dumps(fresh).encode()
        if "/pulls/" in url:
            self.next_id += 1
            self.posted.append(json.loads(body))
            self.review_urls.append(url)
            return 200, {}, json.dumps({"id": self.next_id, "html_url": f"https://github.test/r/{self.next_id}",
                                        "submitted_at": "2026-01-01T12:00:05Z"}).encode()
        if url.endswith("/reactions"):
            self.reactions.append(int(url.split("/")[-2]))
            return self.react_status, {}, b"{}"
        self.next_id += 1
        self.posted.append(json.loads(body))
        return 201, {}, json.dumps({"id": self.next_id, "html_url": f"https://github.test/c/{self.next_id}",
                                    "created_at": "2026-01-01T12:00:05Z"}).encode()

    def gets(self):
        return [c for c in self.calls if c[0] == "GET" and "/comments" in c[1]]


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BO_PUSH_DENYLIST", str(tmp_path / "denylist.txt"))
    settings = Settings(data_dir=tmp_path / "data", token="t")
    settings.ensure_dirs()
    token = settings.control_dir / "github-token"
    token.write_text("ghp_" + "a" * 30 + "\n")
    token.chmod(0o600)
    store = JobStore(settings.db_path)
    target = store.create_bot("Target", "", "codex", None)
    store.create_bot("Lead", "", "codex", None)
    return SimpleNamespace(settings=settings, store=store, target=target, tmp=tmp_path)


def write_config(env, mode=0o600, **overrides):
    data = {"repo": REPO, "pr": 7, "default_bot": "Lead", **overrides}
    path = duet_bridge.config_path(env.settings.control_dir)
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


class Recorder:
    def __init__(self, busy=()):
        self.busy = set(busy)
        self.steered: list[tuple[str, str]] = []
        self.turns_posted: list[tuple[str, str, dict]] = []

    def steer(self, bot, text):
        if bot.name in self.busy:
            self.steered.append((bot.name, text))
            return True
        return False

    def post_message(self, bot, text, **kwargs):
        self.turns_posted.append((bot.name, text, kwargs))
        return object(), SimpleNamespace(id="job-1")


def bridge(env, fake, rec=None, clock=None):
    rec = rec or Recorder()
    return DuetBridge(env.store, env.settings, turns=rec, steer=rec.steer, transport=fake,
                      clock=clock or Clock()), rec


def test_first_start_does_not_replay_history_and_304_does_nothing(env):
    write_config(env)
    fake = Fake()
    fake.add(f"{HEADER} id=D-1 to=Target kind=task\nold message", stamp="2025-12-31T09:00:00Z")
    b, rec = bridge(env, fake)
    b.poll_once()
    # the cursor is "now": it is in the URL, and history is not what was asked for
    assert f"since=2026-01-01T12%3A00%3A00" in fake.gets()[0][1]
    assert env.store.get_meta("duet_cursor") == "2026-01-01T12:00:00Z"
    assert b.report()["inbound"] == 0
    n = len(fake.calls)
    b.poll_once()
    assert fake.gets()[-1][2]["If-None-Match"] == f'W/"{fake.etag}"'
    assert len(fake.calls) == n + 1  # one conditional GET, nothing else
    assert b.report()["not_modified"] == 1
    assert rec.turns_posted == [] and rec.steered == []
    assert b.report()["inbound"] == 0


def test_new_comment_delivered_once_across_polls_and_restarts(env):
    write_config(env)
    fake = Fake()
    b, rec = bridge(env, fake)
    b.poll_once()
    fake.add(f"{HEADER} id=D-2 to=Target thread=th1 kind=question\nplease check this", cid=42)
    b.poll_once()
    b.poll_once()
    assert len(rec.turns_posted) == 1
    name, text, kwargs = rec.turns_posted[0]
    assert name == "Target" and kwargs == {"source": "inbox", "sender": "Claude Duet"}
    assert "please check this" in text and "id=D-2" in text and "thread=th1" in text and "kind=question" in text
    assert "<untrusted-data" in text and "desk duet send --reply-to D-2 --thread th1" in text
    # a restarted controller (new instance, same database) with an old cursor sees it again but skips it
    env.store.set_meta("duet_cursor", "2026-01-01T11:00:00+00:00")
    env.store.delete_meta("duet_etag")
    b2, rec2 = bridge(env, fake)
    b2.poll_once()
    assert rec2.turns_posted == [] and rec2.steered == []
    assert b2.report()["inbound"] == 1


def test_ignores_other_authors_and_non_header_comments(env):
    write_config(env)
    fake = Fake()
    b, rec = bridge(env, fake)
    b.poll_once()
    fake.add(f"{HEADER} id=D-3 to=Target\nfrom a stranger", login="stranger")
    fake.add("just a human note, no header")
    fake.add("[Bot Office → Duet] id=BO-aaaaaaaa from=Target\nour own comment")
    fake.add(f"later {HEADER} id=D-4\nheader not on the first line")
    fake.add("[Duet -> Bot Office] id=D-5 to=Target\nascii arrow works")
    b.poll_once()
    assert [t[1].count("ascii arrow works") for t in rec.turns_posted] == [1]
    assert len(rec.turns_posted) == 1


def test_allowed_authors_from_config_overrides_token_login(env):
    write_config(env, allowed_authors=["Someone-Else"])
    fake = Fake()
    b, rec = bridge(env, fake)
    b.poll_once()
    fake.add(f"{HEADER} id=D-6 to=Target\nfrom token owner", login="owner-login")
    fake.add(f"{HEADER} id=D-7 to=Target\nfrom listed", login="someone-else")
    b.poll_once()
    assert len(rec.turns_posted) == 1 and "from listed" in rec.turns_posted[0][1]
    assert all(c[1] != f"{BASE}/user" for c in fake.calls)


def test_unknown_bot_falls_back_to_default_then_errors_without_one(env):
    write_config(env)
    fake = Fake()
    b, rec = bridge(env, fake)
    b.poll_once()
    fake.add(f"{HEADER} id=D-8 to=Nobody\nhello")
    b.poll_once()
    assert [t[0] for t in rec.turns_posted] == ["Lead"]
    write_config(env, default_bot="Missing")
    fake.add(f"{HEADER} id=D-9 to=Nobody\nhello again")
    b.poll_once()
    assert len(rec.turns_posted) == 1
    err = fake.posted[-1]["body"]
    assert err.startswith("[Bot Office → Duet]") and "kind=error" in err and "reply_to=D-9" in err
    assert "not found" in err


def test_busy_bot_is_steered_and_idle_bot_gets_a_turn(env):
    write_config(env)
    fake = Fake()
    rec = Recorder(busy={"Target"})
    b, _ = bridge(env, fake, rec)
    b.poll_once()
    fake.add(f"{HEADER} id=D-10 to=Target\nbusy one")
    fake.add(f"{HEADER} id=D-11 to=Lead\nidle one")
    b.poll_once()
    assert [s[0] for s in rec.steered] == ["Target"] and "busy one" in rec.steered[0][1]
    assert [t[0] for t in rec.turns_posted] == ["Lead"]
    with b._db() as db:
        modes = {r["msg_id"]: r["mode"] for r in db.execute("SELECT msg_id, mode FROM duet_messages")}
    assert modes == {"D-10": "steer", "D-11": "turn"}


def test_reaction_posted_and_a_failed_reaction_does_not_undo_delivery(env):
    write_config(env)
    fake = Fake()
    b, rec = bridge(env, fake)
    b.poll_once()
    fake.add(f"{HEADER} id=D-12 to=Target\nhi", cid=77)
    b.poll_once()
    assert fake.reactions == [77]
    reaction = [c for c in fake.calls if c[1].endswith("/77/reactions")][0]
    assert json.loads(reaction[3]) == {"content": "eyes"}
    fake.react_status = 500
    fake.add(f"{HEADER} id=D-13 to=Target\nhi2", cid=78)
    b.poll_once()
    assert len(rec.turns_posted) == 2
    assert b.report()["latencies_seconds"]  # delivered_at is recorded


def test_request_headers_and_pagination(env):
    write_config(env)
    fake = Fake()
    b, _ = bridge(env, fake)
    b.poll_once()
    headers = fake.gets()[0][2]
    assert headers["Authorization"].startswith("Bearer ghp_")
    assert headers["Accept"] == "application/vnd.github+json" and headers["X-GitHub-Api-Version"] == "2022-11-28"
    pages = [(200, {"etag": "x", "link": f'<{COMMENTS}?page=2>; rel="next"'}, json.dumps([{
        "id": 1, "body": f"{HEADER} id=P1 to=Target\none", "user": {"login": "owner-login"},
        "created_at": "2026-01-01T12:00:02Z", "updated_at": "2026-01-01T12:00:02Z"}]).encode()),
        (200, {}, json.dumps([{
            "id": 2, "body": f"{HEADER} id=P2 to=Target\ntwo", "user": {"login": "owner-login"},
            "created_at": "2026-01-01T12:00:03Z", "updated_at": "2026-01-01T12:00:03Z"}]).encode())]
    rec = Recorder()
    served = iter(pages)
    b2 = DuetBridge(env.store, env.settings, turns=rec, steer=rec.steer, clock=Clock(),
                    transport=lambda m, u, h, body: (200, {}, b'{"login":"owner-login"}') if u.endswith("/user")
                    else (m == "POST" and (201, {}, b"{}")) or next(served))
    b2.poll_once()
    assert [t[1].count("one") + t[1].count("two") for t in rec.turns_posted] == [1, 1]


def test_rate_limit_and_errors_are_reported_not_fatal(env):
    write_config(env)
    calls = []

    def transport(method, url, headers, body):
        calls.append(url)
        if url.endswith("/user"):
            return 200, {}, b'{"login":"owner-login"}'
        return 429, {"retry-after": "30"}, b""

    b = DuetBridge(env.store, env.settings, transport=transport, clock=Clock())
    with pytest.raises(duet_bridge._RateLimited) as exc:
        b.poll_once()
    assert exc.value.seconds == 30
    assert b._rate_wait(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}) == 1.0
    assert b._rate_wait(403, {}) is None


def test_loose_config_permissions_are_refused(env):
    write_config(env, mode=0o644)
    with pytest.raises(DuetError, match="readable by others"):
        duet_bridge.load_config(env.settings.control_dir)
    b, _ = bridge(env, Fake())
    with pytest.raises(DuetError):
        b.poll_once()
    write_config(env, mode=0o600, interval=1)
    assert duet_bridge.load_config(env.settings.control_dir).interval == 2
    write_config(env, mode=0o600, interval=500)
    assert duet_bridge.load_config(env.settings.control_dir).interval == 60


def test_install_config_validates_and_writes_0600(env, capsys):
    path = duet_bridge.install_config(json.dumps({"repo": REPO, "pr": 7, "default_bot": "Lead"}), env.tmp)
    assert path.stat().st_mode & 0o777 == 0o600
    for bad in ({"repo": "nope", "pr": 7, "default_bot": "x"}, {"repo": REPO, "pr": 0, "default_bot": "x"},
                {"repo": REPO, "pr": 1}, {"repo": REPO, "pr": 1, "default_bot": "x", "allowed_authors": "me"}):
        with pytest.raises(DuetError):
            duet_bridge.install_config(json.dumps(bad), env.tmp)
    with pytest.raises(duet_bridge.DeskError, match="operator"):
        duet_bridge.desk_duet(["install-config"], stdin=io.StringIO("{}"), data_dir=env.tmp,
                              environ={"BOT_OFFICE_AGENT_TOKEN": "x"})
    assert duet_bridge.desk_duet(["install-config"], stdin=io.StringIO(json.dumps(
        {"repo": REPO, "pr": 3, "default_bot": "Lead"})), data_dir=env.tmp, environ={}) == 0
    assert "0600" in capsys.readouterr().out


def test_outbound_header_format_and_record(env):
    write_config(env)
    fake = Fake()
    b, _ = bridge(env, fake)
    result = b.send(text="  done with it  ", sender="Target", to="main", kind="result", thread="th9", reply_to="D-2")
    assert set(result) == {"id", "review_id", "url"} and result["id"].startswith("BO-") and len(result["id"]) == 11
    assert fake.posted[0]["event"] == "COMMENT" and fake.review_urls == [f"{BASE}/repos/{REPO}/pulls/7/reviews"]
    body = fake.posted[0]["body"]
    first, blank, rest = body.split("\n", 2)
    assert first == (f"[Bot Office → Duet] id={result['id']} from=Target to=main thread=th9 "
                     "reply_to=D-2 kind=result")
    assert blank == "" and rest == "done with it"
    post = [c for c in fake.calls if c[0] == "POST"][0]
    assert post[1] == f"{BASE}/repos/{REPO}/pulls/7/reviews" and post[2]["Authorization"].startswith("Bearer ")
    b2 = b.send(text="x", sender="Other")  # defaults: to=main, kind=fyi, no empty keys
    assert fake.posted[1]["body"].split("\n")[0] == f"[Bot Office → Duet] id={b2['id']} from=Other to=main kind=fyi"
    # our own comment is recorded, so polling it back never delivers it
    assert b.report()["outbound"] == 2


@pytest.mark.parametrize("secret", ["ghp_" + "b" * 30, "sk-" + "c" * 30, "AKIA" + "D" * 16,
                                    "-----BEGIN OPENSSH PRIVATE KEY-----", 'x {"api_token": "value"}'])
def test_secret_patterns_refused_without_echo(env, secret):
    write_config(env)
    fake = Fake()
    b, _ = bridge(env, fake)
    with pytest.raises(DuetError) as exc:
        b.send(text=f"see {secret}", sender="Target")
    assert secret not in str(exc.value) and "secret" in str(exc.value)
    assert fake.posted == []


def test_denylist_refused_without_echo_and_missing_file_skips(env):
    write_config(env)
    fake = Fake()
    b, _ = bridge(env, fake)
    b.send(text="fine, no denylist file yet", sender="A")
    (env.tmp / "denylist.txt").write_text("# comment\n\nSecret Phrase Here\n")
    with pytest.raises(DuetError) as exc:
        b.send(text="mentions a SECRET phrase here today", sender="B")
    assert "phrase" not in str(exc.value).lower() and "denylist" in str(exc.value)
    assert len(fake.posted) == 1


def test_rate_limits_and_thread_cap(env):
    write_config(env)
    fake, clock = Fake(), Clock()
    b, _ = bridge(env, fake, clock=clock)
    b.send(text="one", sender="A")
    with pytest.raises(DuetError) as exc:
        b.send(text="two", sender="A")
    assert exc.value.status == 429
    b.send(text="other bot is fine", sender="B")
    clock.advance(4)
    b.send(text="a again", sender="A")
    # 40 a minute overall
    clock.advance(61)
    for i in range(40):
        b.send(text="m", sender=f"bot{i}")
    with pytest.raises(DuetError, match="a minute"):
        b.send(text="m", sender="late")
    # the thread cap counts both directions
    clock.advance(61)
    with b._db() as db:
        for i in range(30):
            db.execute("INSERT INTO duet_messages (comment_id,direction,msg_id,thread,seen_at) VALUES (?,?,?,?,?)",
                       (100000 + i, "in", f"X{i}", "busy-thread", "2020-01-01T00:00:00+00:00"))
    with pytest.raises(DuetError, match="thread cap"):
        b.send(text="m", sender="A", thread="busy-thread")
    b.send(text="m", sender="A", thread="fresh-thread")


def test_daily_limit(env):
    write_config(env)
    fake, clock = Fake(), Clock()
    b, _ = bridge(env, fake, clock=clock)
    with b._db() as db:
        for i in range(1000):
            db.execute("INSERT INTO duet_messages (comment_id,direction,seen_at) VALUES (?,?,?)",
                       (500000 + i, "out", (START - timedelta(hours=2)).isoformat()))
    with pytest.raises(DuetError, match="daily"):
        b.send(text="m", sender="A")


def test_send_validation_and_reply_inherits_thread(env):
    write_config(env)
    fake = Fake()
    b, _ = bridge(env, fake)
    for kwargs in ({"text": ""}, {"text": "x", "kind": "bogus"}, {"text": "x" * 60001}):
        with pytest.raises(DuetError) as exc:
            b.send(sender="A", **kwargs)
        assert exc.value.status == 400
    b.poll_once()
    fake.add(f"{HEADER} id=D-20 to=Target thread=inherit\nhi")
    b.poll_once()
    b.send(text="reply", sender="Target", reply_to="D-20")
    assert "thread=inherit" in fake.posted[-1]["body"].split("\n")[0]


def test_send_refused_when_unconfigured_or_disabled(env):
    b, _ = bridge(env, Fake())
    with pytest.raises(DuetError) as exc:
        b.send(text="x", sender="A")
    assert exc.value.status == 503
    write_config(env, enabled=False)
    with pytest.raises(DuetError):
        b.send(text="x", sender="A")
    assert b.poll_once() is None


def test_status_report_shape(env):
    write_config(env)
    fake = Fake()
    b, _ = bridge(env, fake)
    b.poll_once()
    report = b.report()
    assert report["enabled"] and report["repo"] == REPO and report["pr"] == 7
    assert report["polls"] == 1 and report["last_http_status"] == 200 and report["last_poll_at"]
    assert report["latencies_seconds"] == [] and "last_error" in report
    assert "github-token" not in json.dumps(report) and "ghp_" not in json.dumps(report)


def test_generated_footer_is_stripped_from_inbound_text():
    body = f"{HEADER} id=D-1 to=Target\nreal text\n\n---\n_Generated by SomeTool_\nmore footer"
    fields, text = duet_bridge.parse_inbound(body)
    assert fields["id"] == "D-1" and text == "real text"
    assert duet_bridge.parse_inbound(f"{HEADER} id=D-2\n---\n_Generated by X_")[1] == ""
    assert duet_bridge.parse_inbound(f"{HEADER} id=D-3\nkeep\n---\nnot a footer")[1] == "keep\n---\nnot a footer"
