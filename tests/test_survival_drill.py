"""The survival drill's judging and filler helpers (the live parts need a running Bot Office)."""
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("survival_drill", Path(__file__).resolve().parents[1] / "scripts" / "survival_drill.py")
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_judge_reads_each_item_and_the_rule_at_the_end():
    ok = "Codename VERMILION-OTTER-41, Postgres. Open: ZEPHYR-88 keys. Cat QUILLBERT-19.\n\nKUMQUAT-7"
    assert set(drill.judge(ok).values()) == {"survived"}
    lost = drill.judge("Open tasks: none. Your cat is QUILLBERT-19. I answered from history.")
    assert lost == {"decision": "lost", "task": "lost", "fact": "survived", "rule": "lost"}
    assert drill.judge("VERMILION-OTTER-41 only. KUMQUAT-7 is in the middle. Then more text")["decision"] == "partial"
    assert drill.judge("KUMQUAT-7 then more")["rule"] == "partial"


def test_filler_is_large_deterministic_and_secret_free():
    files = drill.filler_files(2, 2000)
    assert len(files) == 2 and all(len(data) >= 2000 for _, data in files)
    assert files == drill.filler_files(2, 2000)


# ------------------------------------------------------------ restart guard (all mocked: nothing is restarted)


TOKEN = "tok-SECRET-0123456789"
PWB = {"id": "pwb-1", "name": drill.PROTECTED_BOT.title()}
OTHER = {"id": "bot-2", "name": "Ada"}
MINE = {"id": "drill-1", "name": "ZZ drill e5 restart cursor (safe to delete)"}
NOON = datetime(2026, 11, 22, 12, 7, tzinfo=UTC)  # 07:07 New York (routines run in New York time)


class FakeDesk:
    """Canned API: replies maps "METHOD path" to (status, body)."""

    def __init__(self, replies, tmp_path):
        self.replies, self.token, self.notes = dict(replies), TOKEN, []
        self.log = tmp_path / "log.jsonl"

    def call(self, method, path, body=None, raw=None):
        return self.replies.get(f"{method} {path}", (404, "not found"))

    def note(self, kind, **fields):
        self.notes.append((kind, fields))

    def redact(self, text):
        return text.replace(self.token, "[token]")


def healthy(**over):
    replies = {"GET /v1/bots": (200, [PWB, OTHER, MINE]), "GET /v1/jobs": (200, []),
               "GET /v1/bots/pwb-1/routines": (200, []), "GET /v1/bots/bot-2/routines": (200, []),
               "GET /healthz": (200, {"ok": True, "pause_requested": False, "paused": False, "running_jobs": 0})}
    replies.update(over)
    return replies


class Shell:
    """Replaces run_cmd: records every command, answers from a table of (exit, stdout) by command word."""

    def __init__(self, **answers):
        self.calls, self.answers = [], {"systemctl-show": (0, "ActiveEnterTimestampMonotonic=1000000\n"),
                                        "cat": (0, "5000.00 1.0\n"), "pgrep": (1, ""), "systemctl-restart": (0, "")}
        self.answers.update(answers)

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        key = "systemctl-restart" if argv[:3] == ["systemctl", "--user", "restart"] else (
            "systemctl-show" if argv[0] == "systemctl" else argv[0])
        return self.answers[key]


def blockers(tmp_path, replies=None, shell=None, mine_idle=False, **kw):
    desk = FakeDesk(healthy(**(replies or {})), tmp_path)
    return drill.restart_blockers(desk, {"drill-1"}, mine_idle, shell or Shell(), now=NOON, data=tmp_path, **kw)


def job(bot, status, ident="j1234567890"):
    return {"id": ident, "bot_id": bot, "status": status}


def test_clean_state_has_no_blockers(tmp_path):
    assert blockers(tmp_path) == []


@pytest.mark.parametrize("status", ["queued", "running"])
def test_protected_bot_job_blocks_by_name(tmp_path, status):
    found = blockers(tmp_path, {"GET /v1/jobs": (200, [job("pwb-1", status)])})
    assert len(found) == 1 and found[0].startswith("PROTECTED BOT")


def test_other_bot_job_blocks_but_a_drill_bots_own_job_only_blocks_when_asked(tmp_path):
    jobs = {"GET /v1/jobs": (200, [job("bot-2", "running"), job("drill-1", "running", "j2")])}
    found = blockers(tmp_path, jobs)
    assert len(found) == 1 and "Ada" in found[0]
    assert len(blockers(tmp_path, {"GET /v1/jobs": (200, [job("drill-1", "running")])}, mine_idle=True)) == 1
    assert blockers(tmp_path, {"GET /v1/jobs": (200, [job("drill-1", "running")])}) == []


@pytest.mark.parametrize("replies", [
    {"GET /v1/bots": (500, "boom")}, {"GET /v1/bots": (0, "refused")}, {"GET /v1/bots": (200, {"x": 1})},
    {"GET /v1/bots": (200, [OTHER, MINE])},  # no protected bot to look at
    {"GET /v1/jobs": (500, "boom")}, {"GET /v1/jobs": (200, {"jobs": []})}, {"GET /v1/jobs": (200, ["junk"])},
])
def test_unreadable_or_malformed_bots_and_jobs_fail_closed(tmp_path, replies):
    assert blockers(tmp_path, replies)


@pytest.mark.parametrize("reply", [(500, "boom"), (0, "refused"), (200, {"oops": 1}),
                                   (200, ["junk"]), (200, [{"enabled": True, "cron": "not a cron"}]),
                                   (200, [{"enabled": True}])])
def test_routine_api_errors_malformed_data_and_unparseable_cron_fail_closed(tmp_path, reply):
    assert blockers(tmp_path, {"GET /v1/bots/bot-2/routines": reply})


def test_routine_firing_soon_blocks_and_a_disabled_or_far_one_does_not(tmp_path):
    soon = (200, [{"enabled": True, "cron": "9 7 * * *"}])  # 07:09 ET, within 3 minutes of 07:07 ET
    utc_misread = (200, [{"enabled": True, "cron": "9 12 * * *"}])  # 12:09 would be a UTC misreading
    assert blockers(tmp_path, {"GET /v1/bots/bot-2/routines": utc_misread}) == []
    assert any("fires within" in b for b in blockers(tmp_path, {"GET /v1/bots/bot-2/routines": soon}))
    far = (200, [{"enabled": True, "cron": "30 3 * * *"}, {"enabled": False, "cron": "8 7 * * *"}])
    assert blockers(tmp_path, {"GET /v1/bots/bot-2/routines": far}) == []


def test_pgrep_failure_and_other_processes_block_and_own_process_is_ignored(tmp_path):
    assert any("pgrep failed" in b for b in blockers(tmp_path, shell=Shell(pgrep=(2, ""))))
    assert any("pgrep failed" in b for b in blockers(tmp_path, shell=Shell(pgrep=(-1, "no pgrep"))))
    for line in ("4242 bash ./install.sh", "4242 /bin/sh deploy-prod.sh", "4242 git rebase origin/master",
                 "4242 git -C /srv/bot-office merge feature", "4242 systemctl --user restart bot-office",
                 "4242 python3 scripts/survival_drill.py restart cursor"):
        assert any(b.startswith("process:") for b in blockers(tmp_path, shell=Shell(pgrep=(0, line + "\n")))), line
    mine = str(drill.os.getpid())
    assert blockers(tmp_path, shell=Shell(pgrep=(0, f"{mine} python3 scripts/survival_drill.py restart\n"))) == []


def test_young_unreadable_service_and_foreign_pause_file_block(tmp_path):
    assert not any("only" in b for b in blockers(tmp_path, shell=Shell(cat=(0, "1000.00 1.0\n"))))  # up 999 s: old enough
    young = Shell(cat=(0, "100.5 1.0\n"), **{"systemctl-show": (0, "ActiveEnterTimestampMonotonic=60000000\n")})
    assert any("only 40s old" in b for b in blockers(tmp_path, shell=young))
    assert any("age unreadable" in b for b in blockers(tmp_path, shell=Shell(**{"systemctl-show": (1, "")})))
    (tmp_path / "PAUSE").write_text("")
    assert any("PAUSE file already exists" in b for b in blockers(tmp_path))
    assert blockers(tmp_path, own_pause=True) == []


def test_token_is_redacted_in_process_lines_error_bodies_and_logged_lines(tmp_path, capsys):
    shell = Shell(pgrep=(0, f"4242 curl -H 'Authorization: Bearer {TOKEN}' http://x/install.sh\n"))
    found = blockers(tmp_path, shell=shell)
    assert found and all(TOKEN not in b for b in found)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    desk = drill.Desk("http://127.0.0.1:1", token_file, tmp_path / "out" / "log.jsonl")
    desk.note("probe", reply=f"the key is {TOKEN}")
    assert TOKEN not in (tmp_path / "out" / "log.jsonl").read_text() and TOKEN not in capsys.readouterr().out
    assert desk.redact(f"HTTP 401: bad {TOKEN}") == "HTTP 401: bad [token]"
    status, body = desk.call("GET", "/v1/bots")  # nothing listens on port 1: an error text, never the token
    assert status == 0 and TOKEN not in str(body)


def _go(tmp_path, shell=None, replies=None, mine_idle=False, wait=0):
    desk = FakeDesk(healthy(**(replies or {})), tmp_path)
    sleeps = []
    result = drill.safe_restart(desk, {"drill-1"}, "mid-job", mine_idle, shell or Shell(), sleeps.append, tmp_path,
                                wait=wait, back_limit=4)
    return result, desk, sleeps


def restarts_in(shell):
    return [c for c in shell.calls if c[:3] == ["systemctl", "--user", "restart"]]


def test_restart_succeeds_when_clear_and_removes_a_pause_it_never_got_acknowledged(tmp_path):
    shell = Shell()
    result, _desk, _ = _go(tmp_path, shell)  # healthz never says paused: no ack, so no restart
    assert result["ok"] is False and result["state"] == "unverified-blocked" and not restarts_in(shell)
    assert not (tmp_path / "PAUSE").exists()


class PausingDesk(FakeDesk):
    """healthz acknowledges the pause as soon as the PAUSE file exists, like the real scheduler."""

    def __init__(self, replies, tmp_path, order):
        super().__init__(replies, tmp_path)
        self.order, self.tmp = order, tmp_path

    def call(self, method, path, body=None, raw=None):
        if path == "/healthz":
            pause = (self.tmp / "PAUSE").exists()
            return 200, {"ok": True, "pause_requested": pause, "paused": pause, "running_jobs": 0}
        return super().call(method, path, body, raw)


def test_restart_runs_paused_rechecks_before_systemctl_and_resumes_afterwards(tmp_path):
    order = []
    shell = Shell()
    real = shell.__call__

    def tracking(argv, timeout=60):
        if argv[:3] == ["systemctl", "--user", "restart"]:
            order.append(("restart", (tmp_path / "PAUSE").exists()))
        if argv[0] == "pgrep":
            order.append(("recheck", (tmp_path / "PAUSE").exists()))
        return real(argv, timeout)

    desk = PausingDesk(healthy(), tmp_path, order)
    result = drill.safe_restart(desk, {"drill-1"}, "mid-job", False, tracking, lambda s: None, tmp_path, wait=0, back_limit=4)
    assert result == {"ok": True, "state": "restarted", "pause": "paused"}
    assert order[-1] == ("restart", True)  # paused while systemctl runs
    assert order[-2] == ("recheck", True)  # the full check ran again, paused, right before it
    assert order.count(("recheck", False)) == 1  # and once before pausing
    assert not (tmp_path / "PAUSE").exists()  # resumed afterwards


def test_protected_bot_job_appearing_after_the_pause_cancels_the_restart(tmp_path):
    shell = Shell()
    state = {"checks": 0}

    class Racy(PausingDesk):
        def call(self, method, path, body=None, raw=None):
            if path == "/v1/jobs":
                state["checks"] += 1
                return 200, ([job("pwb-1", "queued")] if state["checks"] >= 2 else [])
            return super().call(method, path, body, raw)

    desk = Racy(healthy(), tmp_path, [])
    result = drill.safe_restart(desk, {"drill-1"}, "x", False, shell, lambda s: None, tmp_path, wait=0, back_limit=4)
    assert result["state"] == "unverified-blocked" and not restarts_in(shell)
    assert not (tmp_path / "PAUSE").exists()  # the pause is lifted again


@pytest.mark.parametrize("name,replies,shell", [
    ("pwb queued", {"GET /v1/jobs": (200, [job("pwb-1", "queued")])}, None),
    ("pwb running", {"GET /v1/jobs": (200, [job("pwb-1", "running")])}, None),
    ("other bot running", {"GET /v1/jobs": (200, [job("bot-2", "running")])}, None),
    ("api error", {"GET /v1/bots": (500, "x")}, None),
    ("routine error", {"GET /v1/bots/bot-2/routines": (500, "x")}, None),
    ("pgrep failure", {}, Shell(pgrep=(2, ""))),
])
def test_every_guard_stops_the_restart_before_systemctl(tmp_path, name, replies, shell):
    shell = shell or Shell()
    desk = PausingDesk(healthy(**replies), tmp_path, [])
    result = drill.safe_restart(desk, {"drill-1"}, "x", False, shell, lambda s: None, tmp_path, wait=0, back_limit=4)
    assert result["ok"] is False and result["state"] == "unverified-blocked", name
    assert not restarts_in(shell) and not (tmp_path / "PAUSE").exists()


def test_restart_failure_and_service_not_back_are_failures_not_success(tmp_path):
    desk = PausingDesk(healthy(), tmp_path, [])
    failed = drill.safe_restart(desk, {"drill-1"}, "x", False, Shell(**{"systemctl-restart": (1, "Failed to restart")}),
                                lambda s: None, tmp_path, wait=0, back_limit=4)
    assert failed == {"ok": False, "state": "restart-failed"} and not (tmp_path / "PAUSE").exists()
    gone = PausingDesk(healthy(**{"GET /v1/bots": (200, [PWB, OTHER, MINE])}), tmp_path, [])
    shell = Shell()
    calls = {"n": 0}
    original = gone.call

    def down_after_restart(method, path, body=None, raw=None):
        if path == "/v1/bots" and restarts_in(shell):
            calls["n"] += 1
            return 0, "connection refused"
        return original(method, path, body, raw)

    gone.call = down_after_restart
    result = drill.safe_restart(gone, {"drill-1"}, "x", False, shell, lambda s: None, tmp_path, wait=0, back_limit=4)
    assert result == {"ok": False, "state": "service-not-back"} and calls["n"] >= 1
    assert not (tmp_path / "PAUSE").exists()


def test_service_without_a_pause_mechanism_is_restarted_after_the_last_moment_recheck(tmp_path):
    shell = Shell()
    result, desk, _ = _go(tmp_path, shell, {"GET /healthz": (404, "no")})
    assert result["ok"] is True and result["pause"] == "unsupported" and len(restarts_in(shell)) == 1
    assert [c[0] for c in shell.calls].count("pgrep") == 2  # checked, then checked again right before systemctl
    json.dumps(desk.notes)
