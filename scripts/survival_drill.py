#!/usr/bin/env python3
"""Survival drills against a running Bot Office (T-028 compaction / session reset, T-029 restart).

    python3 scripts/survival_drill.py compaction [--model claude-sonnet-5-5]   # plant, probe A, ~100k compaction, probe B, reset, probe C
    python3 scripts/survival_drill.py reset --engine codex                      # plant, probe A, reset, probe C on another engine
    python3 scripts/survival_drill.py restart cursor antigravity opencode       # mid-job restart, after-finish restart, follow-up

Reads the master token from data/token itself (never printed; every line the script prints or logs is redacted
centrally). Bots are named "ZZ drill e5 <purpose> (safe to delete)" and deleted afterwards, also on failure. Raw
events go to --out (JSONL, no secrets). `restart` restarts the service (systemctl --user restart bot-office) only
through the guard below, and never touches a bot it did not create.

Restart guard (fails CLOSED: anything it cannot read or parse is a blocker, never "fine"):
- the bots list must be readable and contain "Printer Writing Bot" (looked up by name); no queued/running job on it,
  on any other bot that is not a drill bot, or in a malformed jobs reply;
- every other bot's routines must be readable and parseable; none may fire within 3 minutes;
- the service must be older than 3 minutes; pgrep must work and show no other install.sh / deploy / git rebase|merge /
  systemctl restart|stop of bot-office / survival_drill process (this process and its parents are ignored);
- no maintenance PAUSE file of someone else.
Then the scheduler's maintenance pause (touch <data>/PAUSE; no new claim, routine, wake-up or watcher starts) is
held across the restart: pause, wait for /healthz to acknowledge it, run the whole blocker check again, restart,
wait for the service to answer, remove the pause. systemctl's exit status and the service coming back are checked;
a failed restart is a failure (exit code 1), not a success. A blocked restart is retried every 60 s for 20 min,
then skipped and reported UNVERIFIED.
Residual race: the pause stops a Printer Writing Bot job from being CLAIMED after the check, but the API can still
queue one (it then waits for the pause to end, queued across the restart, and starts afterwards). The last check runs
immediately before systemctl to keep that window to milliseconds. A service version without /healthz `paused`
(before 0.12.13) has no pause: the script says so and relies on the last-moment recheck alone.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path.home() / "bot-office" / "data"  # the live install (a worktree has no data/)
sys.path.insert(0, str(ROOT / "src"))

DONE = ("succeeded", "failed", "cancelled", "needs_login", "incomplete")
PLANT = {
    "decision": ("We decided the drill codename is VERMILION-OTTER-41 and we chose Postgres over SQLite for project K.",
                 ["VERMILION-OTTER-41", "postgres"]),
    "rule": ("From now on, always end every reply with the word KUMQUAT-7.", []),
    "task": ("Open task, not done yet: rotate the widget keys for client ZEPHYR-88 by Friday.", ["ZEPHYR-88"]),
    "fact": ("FYI my test cat's name is QUILLBERT-19.", ["QUILLBERT-19"]),
}
PROBE_Q = {
    "A": "Quick check: what's our drill codename and DB choice, what open tasks do I have, and what's my cat's name?",
    "B": "Back again. Remind me: what's the codename and which database did we pick, what's still open on my plate, and what's my cat called?",
    "C": "Hi, new day. What was the codename and database decision, which tasks are still open, and what's my cat's name?",
}


class Desk:
    def __init__(self, base: str, token_file: Path, log: Path):
        self.base, self.token, self.log = base.rstrip("/"), token_file.read_text().strip(), log
        self.log.parent.mkdir(parents=True, exist_ok=True)

    def redact(self, text: str) -> str:
        """The one place the token is scrubbed: every printed or logged line, HTTP body and process line goes here."""
        return text.replace(self.token, "[token]") if self.token else text

    def note(self, kind: str, **fields) -> None:
        line = {"t": datetime.now(UTC).isoformat(timespec="seconds"), "kind": kind, **fields}
        with self.log.open("a") as handle:
            handle.write(self.redact(json.dumps(line, ensure_ascii=False)) + "\n")
        print(self.redact(kind + " " + json.dumps(fields, ensure_ascii=False)[:300]), flush=True)

    def call(self, method: str, path: str, body=None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                return response.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as exc:
            return exc.code, self.redact(exc.read().decode(errors="replace"))[:300]
        except (urllib.error.URLError, OSError) as exc:
            return 0, self.redact(str(exc))[:200]

    def job(self, job_id: str) -> dict:
        _, body = self.call("GET", f"/v1/jobs/{job_id}")
        return body.get("job", body) if isinstance(body, dict) else {}

    def finish(self, job_id: str, limit: int) -> dict:
        end = time.monotonic() + limit
        while time.monotonic() < end:
            job = self.job(job_id)
            if job.get("status") in DONE:
                return job
            time.sleep(3)
        return {"status": "timeout"}

    def messages(self, bot: str) -> list[dict]:
        _, messages = self.call("GET", f"/v1/bots/{bot}/messages")
        return messages if isinstance(messages, list) else []

    def reply_of(self, job_id: str) -> str:
        """The job's own full reply (the last chat message can be a Bot-Office status line instead)."""
        _, body = self.call("GET", f"/v1/jobs/{job_id}")
        return (body.get("job", {}).get("reply_text") or "") if isinstance(body, dict) else ""

    def reply(self, bot: str) -> str:
        texts = [m.get("text") or "" for m in self.messages(bot) if m.get("role") not in ("owner", "user")]
        return texts[-1] if texts else ""

    def send(self, bot: str, text: str, files: list[tuple[str, bytes]] = ()) -> str | None:
        ids = []
        for name, data in files:
            status, info = self.call("POST", f"/v1/bots/{bot}/attachments?name={name}", raw=data)
            if status != 201:
                self.note("upload-failed", status=status, info=str(info)[:200])
                return None
            ids.append(info["id"])
        status, posted = self.call("POST", f"/v1/bots/{bot}/messages", {"text": text, "attachments": ids})
        if status != 201:
            self.note("post-failed", status=status, info=str(posted)[:200])
            return None
        return posted["job"]["id"]

    def turn(self, bot: str, text: str, files=(), limit: int = 420) -> tuple[dict, str]:
        job_id = self.send(bot, text, files)
        if job_id is None:
            return {"status": "post-failed"}, ""
        job = self.finish(job_id, limit)
        return job, self.reply_of(job_id) or self.reply(bot)


def bot_files(bot_id: str) -> dict[str, str]:
    base = DATA / "bots" / bot_id
    out = {}
    for name in ("WORKING.md", "memory.md"):
        try:
            out[name] = (base / name).read_text()
        except OSError:
            out[name] = ""
    return out


def create_bot(desk: Desk, purpose: str, engine: str, model: str | None, effort: str | None, created: list[str]) -> dict | None:
    body = {"name": f"ZZ drill e5 {purpose} (safe to delete)", "engine": engine, "icon": "🧪"}
    if model:
        body["model"] = model
    if effort:
        body["effort"] = effort
    status, bot = desk.call("POST", "/v1/bots", body)
    if status != 201:
        desk.note("create-failed", engine=engine, status=status, info=str(bot)[:200])
        return None
    created.append(bot["id"])
    desk.note("bot-created", purpose=purpose, engine=engine, model=bot.get("model"), effort=bot.get("effort"), id=bot["id"])
    return bot


def judge(text: str) -> dict[str, str]:
    low = text.lower()
    result = {}
    result["decision"] = ("survived" if "vermilion-otter-41" in low and "postgres" in low else
                          "partial" if "vermilion-otter-41" in low or "postgres" in low else "lost")
    result["task"] = "survived" if "zephyr-88" in low else ("partial" if "widget" in low else "lost")
    result["fact"] = "survived" if "quillbert-19" in low else "lost"
    result["rule"] = "survived" if re.search(r"kumquat-7\W*$", text.strip(), re.IGNORECASE) else (
        "partial" if "kumquat" in low else "lost")
    return result


def probe(desk: Desk, bot: dict, label: str, results: dict) -> None:
    job, text = desk.turn(bot["id"], PROBE_Q[label])
    verdict = judge(text)
    results[label] = {"job": job.get("status"), "verdict": verdict, "reply": text}
    desk.note(f"probe-{label}", job=job.get("status"), verdict=verdict, reply=text, files=bot_files(bot["id"]))


def plant(desk: Desk, bot: dict) -> None:
    for key, (text, _) in PLANT.items():
        job, reply = desk.turn(bot["id"], text)
        desk.note(f"plant-{key}", job=job.get("status"), reply=reply, files=bot_files(bot["id"]))


CHATTER = ("Name one fruit that is yellow.", "What is 17 plus 25?", "Give me a synonym for quick.",
           "Which planet is closest to the sun?", "Spell the word banana backwards.", "Name a primary color.")


def chatter(desk: Desk, bot: dict, turns: int) -> None:
    """Neutral turns so the planted items fall out of the last-6-messages window of a fresh session."""
    for text in CHATTER[:turns]:
        job, reply = desk.turn(bot["id"], text)
        desk.note("chatter", job=job.get("status"), reply=reply[:120])


def reset(desk: Desk, bot: dict) -> None:
    status, out = desk.call("POST", f"/v1/bots/{bot['id']}/session/reset")
    desk.note("session-reset", status=status, out=out)


def filler_files(count: int = 5, size: int = 80_000) -> list[tuple[str, bytes]]:
    rng = random.Random(1)
    words = [f"{rng.choice('bcdfghjklmnprstvz')}{rng.choice('aeiou')}{rng.choice('bcdfghjklmnprstvz')}{rng.choice('aeiou')}"
             f"{rng.choice('bcdfghjklmnprstvz')}" for _ in range(3000)]
    files = []
    for n in range(count):
        lines, total = [], 0
        while total < size:
            line = " ".join(rng.choice(words) for _ in range(12)) + "\n"
            lines.append(line)
            total += len(line)
        files.append((f"filler-{n + 1}.txt", "".join(lines).encode()))
    return files


def compact_boundaries(job_id: str) -> list[dict]:
    found = []
    for path in (DATA / "logs").glob(f"{job_id}*"):
        try:
            for line in path.read_text(errors="replace").splitlines():
                if "compact_boundary" in line:
                    try:
                        found.append(json.loads(line))
                    except ValueError:
                        found.append({"raw": line[:300]})
        except OSError:
            pass
    return found


def mode_compaction(desk: Desk, args, created: list[str]) -> dict:
    bot = create_bot(desk, "compaction", "claude", args.model, args.effort, created)
    if not bot:
        return {}
    results: dict = {}
    plant(desk, bot)
    probe(desk, bot, "A", results)
    files = filler_files()
    jobs = []
    for start in range(0, len(files), 2):
        chunk = files[start:start + 2]
        job_id = desk.send(bot["id"], "Read these attached files completely (every line, use big reads), then reply "
                           "only with the number of files you read.", chunk)
        job = desk.finish(job_id, 600) if job_id else {"status": "post-failed"}
        jobs.append(job_id)
        found = compact_boundaries(job_id) if job_id else []
        desk.note("filler", status=job.get("status"), boundaries=found, reply=desk.reply(bot["id"])[:200])
    status, out = desk.call("POST", f"/v1/bots/{bot['id']}/slash", {"name": "compact"})
    job_id = out["job"]["id"] if status == 200 and isinstance(out, dict) and out.get("job") else None
    job = desk.finish(job_id, 600) if job_id else {"status": str(status)}
    boundaries = compact_boundaries(job_id) if job_id else []
    results["compact"] = {"job": job.get("status"), "boundaries": boundaries, "reply": desk.reply(bot["id"])[:300]}
    desk.note("compact", **results["compact"], files=bot_files(bot["id"]))
    probe(desk, bot, "B", results)
    reset(desk, bot)
    probe(desk, bot, "C", results)
    return results


def mode_reset(desk: Desk, args, created: list[str]) -> dict:
    bot = create_bot(desk, f"reset {args.engine}", args.engine, args.model, args.effort, created)
    if not bot:
        return {}
    results: dict = {}
    plant(desk, bot)
    probe(desk, bot, "A", results)
    chatter(desk, bot, args.chatter)
    reset(desk, bot)
    probe(desk, bot, "C", results)
    return results


# ---------------------------------------------------------------- restart drill

PWB_NAME = "printer writing bot"  # compared case-insensitively against bot names; never touched, only looked at
PAUSE_FILE = "PAUSE"
BUSY = ("queued", "running")
PROCESS_PATTERN = (r"install\.sh|first_deploy|deploy[\w.-]*\.sh|\bgit\b.* (?:rebase|merge|pull|checkout|reset)\b"
                   r"|systemctl.*(?:restart|stop|kill).*bot-office|survival_drill")


def run_cmd(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """(exit status, stdout); a command that cannot run at all is (-1, reason). Tests replace this."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return done.returncode, done.stdout


def service_age(run=run_cmd) -> float | None:
    """Seconds since the service became active, or None when it cannot be read."""
    code, out = run(["systemctl", "--user", "show", "bot-office", "-p", "ActiveEnterTimestampMonotonic"])
    code2, up = run(["cat", "/proc/uptime"])
    try:
        if code != 0 or code2 != 0:
            return None
        started = int(out.strip().split("=")[-1])
        return float(up.split()[0]) - started / 1e6 if started else None
    except (ValueError, IndexError):
        return None


def own_pids() -> set[str]:
    """This process and its parents (the shell or ssh that started it): their command lines name this script."""
    pids, pid = set(), os.getpid()
    while pid > 1 and str(pid) not in pids:
        pids.add(str(pid))
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def process_blockers(desk: Desk, run=run_cmd, ignore: set[str] | None = None) -> list[str]:
    code, out = run(["pgrep", "-af", PROCESS_PATTERN])
    if code == 1:
        return []  # pgrep's own "no match"
    if code != 0:
        return [f"pgrep failed (exit {code}): cannot tell whether a deploy or restart is running"]
    ignore = own_pids() if ignore is None else ignore
    found = []
    for line in out.splitlines():
        pid = line.split(None, 1)[0] if line.strip() else ""
        if pid in ignore or os.path.basename((line.split() + ["", ""])[1]) == "pgrep":
            continue
        found.append("process: " + desk.redact(line)[:80])
    return found


def routine_blockers(desk: Desk, bots: list[dict], mine: set[str], now: datetime) -> list[str]:
    # routines are scheduled in New York time, not UTC
    from botoffice.cron import cron_matches, to_cron_time

    blockers = []
    for bot in bots:
        if bot["id"] in mine:
            continue
        status, routines = desk.call("GET", f"/v1/bots/{bot['id']}/routines")
        if status != 200 or not isinstance(routines, list):
            blockers.append(f"routines api {status} for {bot.get('name', '?')}: cannot tell what fires soon")
            continue
        for routine in routines:
            if not isinstance(routine, dict):
                blockers.append(f"malformed routine of {bot.get('name', '?')}")
                continue
            if not routine.get("enabled", True):
                continue
            try:
                firing = next((m for m in range(4) if cron_matches(routine["cron"], to_cron_time(now + timedelta(minutes=m)))), None)
            except Exception:  # noqa: BLE001 - a schedule we cannot parse might fire at any moment
                blockers.append(f"unparseable routine schedule of {bot.get('name', '?')}")
                continue
            if firing is not None:
                blockers.append(f"routine of {bot.get('name', '?')} fires within {firing} min")
    return blockers


def restart_blockers(desk: Desk, mine: set[str], mine_idle: bool = False, run=run_cmd, now: datetime | None = None,
                     data: Path | None = None, own_pause: bool = False) -> list[str]:
    """Everything that forbids a restart right now. Empty means the restart may go ahead.
    *mine_idle*: a drill bot's own queued/running job also blocks (the after-finish restart must not hit it)."""
    status, bots = desk.call("GET", "/v1/bots")
    if status != 200 or not isinstance(bots, list) or not all(isinstance(b, dict) and "id" in b for b in bots):
        return [f"bots api unreadable ({status}): cannot check the Printer Writing Bot"]
    pwb = {b["id"] for b in bots if PWB_NAME in str(b.get("name", "")).strip().casefold()}
    if not pwb:
        return ["Printer Writing Bot not found in the bots list: cannot prove it is idle"]
    names = {b["id"]: b.get("name", "?") for b in bots}
    status, jobs = desk.call("GET", "/v1/jobs")
    if status != 200 or not isinstance(jobs, list):
        return [f"jobs api unreadable ({status})"]
    blockers = []
    for job in jobs:
        if not isinstance(job, dict) or "status" not in job:
            blockers.append("malformed job entry in the jobs list")
            continue
        if job["status"] not in BUSY:
            continue
        bot_id, label = job.get("bot_id"), f"job {str(job.get('id', '?'))[:8]} {job['status']}"
        if bot_id in pwb:
            blockers.append(f"PRINTER WRITING BOT {label}")
        elif bot_id not in mine or mine_idle:
            blockers.append(f"{label} on {names.get(bot_id, 'a job without a bot')}")
    blockers += routine_blockers(desk, bots, mine, now or datetime.now(UTC))
    age = service_age(run)
    if age is None:
        blockers.append("service age unreadable")
    elif age < 180:
        blockers.append(f"service only {age:.0f}s old")
    blockers += process_blockers(desk, run)
    if not own_pause and (data or DATA).joinpath(PAUSE_FILE).exists():
        blockers.append("a maintenance PAUSE file already exists (someone else is quiescing the service)")
    return blockers


def pause_state(desk: Desk) -> dict | None:
    """/healthz when it has the maintenance pause fields (0.12.13+), else None (no pause on this version)."""
    status, body = desk.call("GET", "/healthz")
    return body if status == 200 and isinstance(body, dict) and "pause_requested" in body else None


def pause_scheduler(desk: Desk, data: Path, sleep=time.sleep, limit: int = 60) -> str:
    """touch PAUSE and wait until the scheduler acknowledges it: "paused", "unsupported" (no pause on this
    service version; nothing created) or "failed" (the file is removed again)."""
    if pause_state(desk) is None:
        return "unsupported"
    path = data / PAUSE_FILE
    try:
        path.touch(exist_ok=False)
    except OSError:
        return "failed"
    for _ in range(limit // 2):
        state = pause_state(desk)
        if state and state.get("paused") is True:
            return "paused"
        sleep(2)
    resume_scheduler(data)
    return "failed"


def resume_scheduler(data: Path) -> None:
    try:
        (data / PAUSE_FILE).unlink()
    except FileNotFoundError:
        pass


def safe_restart(desk: Desk, mine: set[str], label: str, mine_idle: bool = False, run=run_cmd, sleep=time.sleep,
                 data: Path | None = None, wait: int = 20 * 60, back_limit: int = 120) -> dict:
    """The only way this script restarts the service. Returns {"ok": bool, "state": ...} with state one of
    restarted, unverified-blocked (never restarted), restart-failed (systemctl failed) or service-not-back."""
    data = data or DATA
    end = time.monotonic() + wait
    while True:
        blockers = restart_blockers(desk, mine, mine_idle, run, data=data)
        paused = "unsupported"
        if not blockers:
            paused = pause_scheduler(desk, data, sleep)
            if paused == "failed":
                blockers = ["maintenance pause was not acknowledged"]
            else:
                # nothing new starts now; look at everything again, as late as possible before systemctl
                blockers = restart_blockers(desk, mine, mine_idle, run, data=data, own_pause=paused == "paused")
        if not blockers:
            break
        if paused == "paused":
            resume_scheduler(data)
        desk.note("restart-blocked", label=label, blockers=blockers)
        if time.monotonic() > end:
            desk.note("restart-skipped", label=label, status="UNVERIFIED")
            return {"ok": False, "state": "unverified-blocked", "blockers": blockers}
        sleep(60)
    try:
        desk.note("restart", label=label, utc=datetime.now(UTC).isoformat(timespec="seconds"), pause=paused)
        code, out = run(["systemctl", "--user", "restart", "bot-office"], 120)
        if code != 0:
            desk.note("restart-failed", label=label, exit=code, output=desk.redact(out)[:200])
            return {"ok": False, "state": "restart-failed"}
        for _ in range(max(1, back_limit // 2)):
            sleep(2)
            status, _body = desk.call("GET", "/v1/bots")
            if status == 200:
                desk.note("service-back", label=label)
                return {"ok": True, "state": "restarted", "pause": paused}
        desk.note("service-not-back", label=label)
        return {"ok": False, "state": "service-not-back"}
    finally:
        if paused == "paused":
            resume_scheduler(data)  # the new process started paused (the file survives a restart); let it work


def mode_restart(desk: Desk, args, created: list[str]) -> dict:
    results: dict = {}
    bots: dict[str, dict] = {}
    for engine in args.engines:
        bot = create_bot(desk, f"restart {engine}", engine, None, None if engine == "cursor" else args.effort, created)
        if bot:
            bots[engine] = bot
    word = {e: f"MANGO-{random.Random(e).randint(100, 999)}" for e in bots}
    for engine, bot in bots.items():
        job, reply = desk.turn(bot["id"], f"For later: my lucky word is {word[engine]}. Just say ok.", limit=args.baseline_limit)
        results[engine] = {"baseline": job.get("status"), "baseline_reply": reply}
        desk.note("baseline", engine=engine, job=job.get("status"), reply=reply[:200])
    for engine in [e for e in bots if results[e]["baseline"] != "succeeded"]:
        desk.note("baseline-not-finished", engine=engine, status=results[engine]["baseline"])  # excluded: not a restart result
        results[engine]["excluded"] = True
        desk.call("DELETE", f"/v1/bots/{bots[engine]['id']}")  # also stops its job; a running one would block the restarts
        created.remove(bots.pop(engine)["id"])
    if not bots:
        return results
    before = {e: desk.messages(b["id"]) for e, b in bots.items()}
    long_job = {}
    for engine, bot in bots.items():
        token = f"DONE-{random.randint(1000, 9999)}"
        results[engine]["token"] = token
        long_job[engine] = desk.send(bot["id"], f"Run the shell command `sleep {args.sleep}` (wait for it to finish), then reply with exactly {token}.")
        desk.note("long-job", engine=engine, job=long_job[engine])
    # wait until every long job runs
    end = time.monotonic() + 300
    while time.monotonic() < end and not all(desk.job(j).get("status") == "running" for j in long_job.values() if j):
        time.sleep(3)
    time.sleep(args.settle)
    for engine, j in long_job.items():
        desk.note("pre-restart-job", engine=engine, status=desk.job(j).get("status"))
    mine = {b["id"] for b in bots.values()}
    results["restart1"] = safe_restart(desk, mine, "mid-job")
    if not results["restart1"]["ok"]:
        desk.note("RESTART-FAILED-OR-UNVERIFIED", label="mid-job", **results["restart1"])
    for engine, j in long_job.items():
        job = desk.finish(j, 900)
        results[engine]["long_job"] = {k: job.get(k) for k in ("status", "attempts", "error", "failure_kind")}
        desk.note("long-job-end", engine=engine, **results[engine]["long_job"])
    jobs = desk.call("GET", "/v1/jobs")[1]
    for engine, bot in bots.items():
        msgs = desk.messages(bot["id"])
        replies = [m for m in msgs if m.get("role") not in ("owner", "user")]
        results[engine]["after_midjob"] = {
            "messages": len(msgs), "before": len(before[engine]),
            "bot_replies": len(replies), "token_replies": sum(results[engine]["token"] in (m.get("text") or "") for m in replies),
            "user_msgs": sum(m.get("role") in ("owner", "user") for m in msgs),
            "stuck_jobs": sum(1 for x in jobs if x.get("bot_id") == bot["id"] and x.get("status") in ("queued", "running"))}
        desk.note("after-midjob", engine=engine, **results[engine]["after_midjob"])
    results["restart2"] = safe_restart(desk, mine, "after-finish", mine_idle=True)
    if not results["restart2"]["ok"]:
        desk.note("RESTART-FAILED-OR-UNVERIFIED", label="after-finish", **results["restart2"])
    for engine, bot in bots.items():
        job, reply = desk.turn(bot["id"], "What lucky word did I tell you earlier?", limit=420)
        results[engine]["followup"] = {"job": job.get("status"), "reply": reply, "remembered": word[engine] in reply}
        msgs = desk.messages(bot["id"])
        results[engine]["final"] = {"messages": len(msgs), "texts": [(m.get("role"), (m.get("text") or "")[:80]) for m in msgs]}
        desk.note("followup", engine=engine, **results[engine]["followup"], messages=results[engine]["final"]["messages"])
    jobs = desk.call("GET", "/v1/jobs")[1]
    for engine, bot in bots.items():
        results[engine]["stuck_at_end"] = sum(1 for x in jobs if x.get("bot_id") == bot["id"] and x.get("status") in ("queued", "running"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("compaction", "reset", "restart"))
    parser.add_argument("engines", nargs="*", default=["cursor", "antigravity", "opencode"])
    parser.add_argument("--engine", default="claude")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--chatter", type=int, default=0, help="reset mode: neutral turns between probe A and the reset")
    parser.add_argument("--baseline-limit", type=int, default=600)
    parser.add_argument("--sleep", type=int, default=75)
    parser.add_argument("--settle", type=int, default=10, help="seconds between all jobs running and the restart")
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--token-file", type=Path, default=DATA / "token")
    parser.add_argument("--out", type=Path, default=Path("/tmp/e5-drill"))
    args = parser.parse_args()
    desk = Desk(args.base, args.token_file, args.out / f"{args.mode}-{args.engine if args.mode == 'reset' else 'run'}{'-chatter' if args.chatter else ''}.jsonl")
    created: list[str] = []
    results: dict = {}
    try:
        results = {"compaction": mode_compaction, "reset": mode_reset, "restart": mode_restart}[args.mode](desk, args, created)
    finally:
        for bot_id in created:
            status, _ = desk.call("DELETE", f"/v1/bots/{bot_id}")
            desk.note("bot-deleted", id=bot_id, status=status)
        desk.note("results", results=results)
    restarts = [results.get(k) for k in ("restart1", "restart2") if k in results]
    return 1 if any(isinstance(r, dict) and not r.get("ok") for r in restarts) else 0


if __name__ == "__main__":
    sys.exit(main())
