#!/usr/bin/env python3
"""Live check against a running Bot Office: for each engine, a fresh throwaway bot must run one turn on its
default model + effort pair, and a real CLI turn must read an uploaded file and quote its unique token.

    python3 scripts/live_check.py claude codex          # engines to check (default: all)
    python3 scripts/live_check.py claude --compact      # also run /compact on a throwaway Claude bot

Reads the master token from data/token itself (never printed). Bots are named "ZZ live-check <engine> (safe to
delete)" and deleted afterwards, also when a step fails. Exit status 1 if any step failed.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINES = ("claude", "codex", "cursor", "antigravity", "opencode")


class Desk:
    def __init__(self, base: str, token_file: Path):
        self.base, self.token = base.rstrip("/"), token_file.read_text().strip()

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

    def finish(self, job_id: str, limit: int) -> dict:
        end = time.monotonic() + limit
        while time.monotonic() < end:
            _, body = self.call("GET", f"/v1/jobs/{job_id}")
            job = body.get("job", body) if isinstance(body, dict) else {}
            if job.get("status") in ("succeeded", "failed", "cancelled", "needs_login"):
                return job
            time.sleep(3)
        return {"status": "timeout"}

    def reply(self, bot: str) -> str:
        _, messages = self.call("GET", f"/v1/bots/{bot}/messages")
        if not isinstance(messages, list):
            return ""
        texts = [m.get("text") or "" for m in messages if m.get("role") not in ("owner", "user")]
        return texts[-1] if texts else ""

    def turn(self, bot: str, text: str, files: list[tuple[str, bytes]] = (), limit: int = 420) -> tuple[dict, str]:
        ids = []
        for name, data in files:
            status, info = self.call("POST", f"/v1/bots/{bot}/attachments?name={name}", raw=data)
            if status != 201:
                return {"status": "upload-failed", "error": f"{status} {info}"}, ""
            ids.append(info["id"])
        status, posted = self.call("POST", f"/v1/bots/{bot}/messages", {"text": text, "attachments": ids})
        if status != 201:
            return {"status": "post-failed", "error": f"{status} {posted}"}, ""
        job = self.finish(posted["job"]["id"], limit)
        return job, self.reply(bot)


def check_engine(desk: Desk, engine: str, compact: bool) -> list[tuple[str, bool, str]]:
    status, bot = desk.call("POST", "/v1/bots", {"name": f"ZZ live-check {engine} (safe to delete)",
                                                 "default_provider": engine, "instructions": "Reply briefly."})
    if status != 201:
        return [("create", False, f"{status} {bot}")]
    pair = f"{bot['model']} / {bot['effort'] or 'no effort'}"
    results = []
    try:
        job, text = desk.turn(bot["id"], "Reply with exactly the word PONG.")
        results.append((f"default pair {pair}", job.get("status") == "succeeded" and "PONG" in text.upper(),
                        f"{job.get('status')}: {(job.get('error') or text)[:120]}"))
        token = "UPLOADTOKEN-" + secrets.token_hex(6)
        note = f"The secret code is {token}. Nothing else matters.\n".encode()
        job, text = desk.turn(bot["id"], "I attached a text file. Quote the secret code from it exactly.", [("live-check.txt", note)])
        results.append(("upload read", token in text, f"{job.get('status')}: {(job.get('error') or text)[:120]}"))
        if compact and engine == "claude":
            for line in ("name three colors", "name three fruits", "name three cities"):
                desk.turn(bot["id"], line)
            status, out = desk.call("POST", f"/v1/bots/{bot['id']}/slash", {"name": "compact"})
            job = desk.finish(out["job"]["id"], 300) if status == 200 and out.get("job") else {"status": f"{status}"}
            text = desk.reply(bot["id"])
            results.append(("/compact status line", job.get("status") == "succeeded" and text.startswith("Conversation compacted"), text[:120]))
    finally:
        desk.call("DELETE", f"/v1/bots/{bot['id']}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("engines", nargs="*", choices=ENGINES, default=list(ENGINES))
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--token-file", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "token")
    parser.add_argument("--compact", action="store_true", help="also run /compact on a throwaway Claude bot")
    args = parser.parse_args()
    desk, failed = Desk(args.base, args.token_file), False
    for engine in args.engines:
        for name, ok, detail in check_engine(desk, engine, args.compact):
            failed |= not ok
            print(f"{'PASS' if ok else 'FAIL'}  {engine:12} {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
