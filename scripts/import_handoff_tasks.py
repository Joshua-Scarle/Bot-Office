#!/usr/bin/env python3
"""0.12.39: import open work from a curated handoff spec (JSON) into Bot Office tasks, idempotently.

    import_handoff_tasks.py SPEC.json [--apply] [--api http://127.0.0.1:8787] [--token-file data/token]

Each spec task: key, bot (exact Bot Office name), title, source, context, decisions, next, joshua, done_when,
optional list. The task's goal carries the whole body (goal, context and state, decisions and rules, the list,
the next step, whether the owner is needed, the owner) and ends with ``Import key: grok-handoff/<key>``. A task
whose key is already on any bot's task list (open or closed) is skipped, so re-running creates no duplicates.
Tasks are created open (not started): starting stays the owner's call. Without --apply nothing is written.
The token is read from the token file and never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

MARK = "Import key: grok-handoff/"


def body(task: dict) -> str:
    parts = [
        f"Owner: {task['bot']}. Imported on Nov 26 2026 from the Grok handoff ({task['source']}; Nov 21 2026 "
        "snapshot plus later decisions). State below is as of the handoff: check the live source before acting.",
        f"## Goal\n{task['title']}",
        f"## Context and state\n{task['context']}",
        f"## Decisions and rules\n{task['decisions']}",
    ]
    if task.get("list"):
        parts.append(f"## Items\n{task['list']}")
    parts += [f"## Next step\n{task['next']}", f"## Needs the owner\n{task['joshua']}", MARK + task["key"]]
    return "\n\n".join(parts)


def call(api: str, token: str, method: str, path: str, data: dict | None = None):
    request = urllib.request.Request(api + path, method=method, data=None if data is None else json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path}: HTTP {exc.code} {exc.read()[:300].decode(errors='replace')}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--api", default="http://127.0.0.1:8787")
    parser.add_argument("--token-file", default=str(Path(__file__).resolve().parents[1] / "data" / "token"))
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text())
    token = Path(args.token_file).read_text().strip()
    bots = {bot["name"]: bot["id"] for bot in call(args.api, token, "GET", "/v1/bots")}
    present: set[str] = set()
    for bot_id in bots.values():
        for item in call(args.api, token, "GET", f"/v1/bots/{bot_id}/assignments?include_closed=true"):
            goal = item.get("goal") or ""
            if MARK in goal:
                present.add(goal.rsplit(MARK, 1)[1].split()[0])
    created = skipped = 0
    for task in spec["tasks"]:
        if task["bot"] not in bots:
            print(f"ERROR {task['key']}: no bot named {task['bot']!r}", file=sys.stderr)
            return 2
        if task["key"] in present:
            print(f"present {task['key']} ({task['bot']})")
            skipped += 1
            continue
        fields = {"title": task["title"][:200], "goal": body(task), "done_when": task["done_when"][:4000],
                  "budget_turns": 20, "budget_minutes": 120, "start": False}
        if not args.apply:
            print(f"would create {task['key']} -> {task['bot']} ({len(fields['goal'])} chars)")
            continue
        made = call(args.api, token, "POST", f"/v1/bots/{bots[task['bot']]}/assignments", fields)
        print(f"created {task['key']} -> {task['bot']} task {made['id'][:8]}")
        created += 1
    print(f"done: {created} created, {skipped} already present" + ("" if args.apply else " (dry run)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
