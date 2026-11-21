#!/usr/bin/env python3
"""Survival drills against a running Bot Office (T-028 compaction / session reset, T-029 restart).

    python3 scripts/survival_drill.py compaction [--model claude-sonnet-5-5]   # plant, probe A, ~100k compaction, probe B, reset, probe C
    python3 scripts/survival_drill.py reset --engine codex                      # plant, probe A, reset, probe C on another engine
    python3 scripts/survival_drill.py restart cursor antigravity opencode       # mid-job restart, after-finish restart, follow-up

Reads the master token from data/token itself (never printed). Bots are named "ZZ drill e5 <purpose> (safe to
delete)" and deleted afterwards, also on failure. Raw events go to --out (JSONL, no secrets). `restart` restarts
the service (systemctl --user restart bot-office) only after safety checks: no queued/running job on a bot that is
not a drill bot, no routine firing within 3 minutes, service older than 3 minutes, no install/rebase/restart
process of someone else; otherwise it waits (60 s polls, up to 20 min) and records the restart as UNVERIFIED.
Never touches any bot it did not create.
"""
from __future__ import annotations

import argparse
import json
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

    def note(self, kind: str, **fields) -> None:
        line = {"t": datetime.now(UTC).isoformat(timespec="seconds"), "kind": kind, **fields}
        with self.log.open("a") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(kind, json.dumps(fields, ensure_ascii=False)[:300], flush=True)

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
            return exc.code, exc.read().decode(errors="replace")[:300]
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc)[:200]

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

def service_age() -> float:
    out = subprocess.run(["systemctl", "--user", "show", "bot-office", "-p", "ActiveEnterTimestampMonotonic"],
                         capture_output=True, text=True, check=False).stdout.strip().split("=")[-1]
    up = subprocess.run(["cat", "/proc/uptime"], capture_output=True, text=True, check=False).stdout.split()[0]
    try:
        return float(up) - int(out) / 1e6
    except ValueError:
        return 0.0


def restart_blockers(desk: Desk, mine: set[str], mine_idle: bool = False) -> list[str]:
    """*mine_idle*: a drill bot's own queued/running job also blocks (the after-finish restart must not hit it)."""
    blockers = []
    status, bots = desk.call("GET", "/v1/bots")
    if status != 200 or not isinstance(bots, list):
        return [f"bots api {status}"]
    names = {b["id"]: b["name"] for b in bots}
    status, jobs = desk.call("GET", "/v1/jobs")
    if status != 200 or not isinstance(jobs, list):
        return [f"jobs api {status}"]
    for job in jobs:
        if job.get("status") in ("queued", "running") and (job.get("bot_id") not in mine or mine_idle):
            blockers.append(f"job {job['id'][:8]} {job['status']} on {names.get(job.get('bot_id'), '?')}")
    from botoffice.cron import cron_matches
    now = datetime.now(UTC)
    for bot in bots:
        if bot["id"] in mine:
            continue
        status, routines = desk.call("GET", f"/v1/bots/{bot['id']}/routines")
        for routine in routines if status == 200 and isinstance(routines, list) else []:
            if not routine.get("enabled", True):
                continue
            for minute in range(4):
                try:
                    if cron_matches(routine["cron"], now + timedelta(minutes=minute)):
                        blockers.append(f"routine of {bot['name']} fires within {minute} min")
                        break
                except Exception:  # noqa: BLE001 - a routine we cannot parse is not our blocker
                    break
    age = service_age()
    if age < 180:
        blockers.append(f"service only {age:.0f}s old")
    others = subprocess.run(["pgrep", "-af", r"install\.sh|git rebase|systemctl.*restart"], capture_output=True, text=True, check=False).stdout
    mine_pid = str(subprocess.os.getpid())
    for line in others.splitlines():
        if mine_pid not in line.split()[0:1] and "pgrep" not in line and "survival_drill" not in line:
            blockers.append("process: " + line[:80])
    return blockers


def safe_restart(desk: Desk, mine: set[str], label: str, mine_idle: bool = False) -> bool:
    end = time.monotonic() + 20 * 60
    while True:
        blockers = restart_blockers(desk, mine, mine_idle)
        if not blockers:
            break
        desk.note("restart-blocked", label=label, blockers=blockers)
        if time.monotonic() > end:
            desk.note("restart-skipped", label=label, status="UNVERIFIED")
            return False
        time.sleep(60)
    stamp = datetime.now(UTC)
    desk.note("restart", label=label, utc=stamp.isoformat(timespec="seconds"))
    subprocess.run(["systemctl", "--user", "restart", "bot-office"], check=False)
    for _ in range(60):
        time.sleep(2)
        status, _ = desk.call("GET", "/v1/bots")
        if status == 200:
            desk.note("service-back", label=label)
            return True
    desk.note("service-not-back", label=label)
    return True


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
