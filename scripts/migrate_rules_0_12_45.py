#!/usr/bin/env python3
"""0.12.45 rules migration: the per-bot "E8 safety gate" ask rules (and the Nov 26 interim deploy rule)
are replaced by the approval policy in code (src/botoffice/policy.py, docs/permissions.md).

Through the running app's API only (GET/DELETE /v1/rules), idempotent, with a JSON backup of every rule
first. Deletes, for every bot except the ones named with --skip (default: MPG DevOps (BO)), the ask rules
whose note starts with "E8 safety gate" or "Nov 26 interim". Nothing else is touched: global allow rules,
the owner's own rules, never rules, other bots' settings. General Worker is the template new bots copy
their rules from (bo-runs/bo-intel/ops.py), so it gets the same treatment: new bots start with the policy
alone.

  python3 scripts/migrate_rules_0_12_45.py [--dry-run] [--backup-dir DIR] [--skip NAME ...] [--only NAME ...]

0.12.47: ``--only NAME`` limits the run to the named bots (the owner, Nov 26: "MPG DevOps (BO): remove its
6 old E8 ask rules the same way"); --skip is then ignored.

The token is read from BOT_OFFICE_TOKEN_FILE (or data/token) and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

NOTE_PREFIXES = ("E8 safety gate", "Nov 26 interim")
DEFAULT_SKIP = ("MPG DevOps (BO)",)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default=os.environ.get("BOT_OFFICE_API_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--token-file", default=os.environ.get("BOT_OFFICE_TOKEN_FILE")
                        or str(Path(__file__).resolve().parents[1] / "data" / "token"))
    parser.add_argument("--backup-dir", default=os.path.expanduser("~/backups"))
    parser.add_argument("--skip", action="append", default=None, help="bot name to leave alone (repeatable)")
    parser.add_argument("--only", action="append", default=None, help="only this bot (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    skip_names = () if args.only else (tuple(args.skip) if args.skip else DEFAULT_SKIP)
    token = Path(args.token_file).read_text(encoding="utf-8").strip()

    def call(method: str, path: str):
        request = urllib.request.Request(args.api.rstrip("/") + path, method=method,
                                         headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")[:300]

    status, bots = call("GET", "/v1/bots")
    if status != 200:
        print(f"cannot list bots: HTTP {status}", file=sys.stderr)
        return 2
    bots = bots.get("bots", bots) if isinstance(bots, dict) else bots
    names = {bot["id"]: bot["name"] for bot in bots}
    skip_ids = {bot_id for bot_id, name in names.items() if name in skip_names}
    if args.only:
        skip_ids = {bot_id for bot_id, name in names.items() if name not in args.only}
        if not set(args.only) & set(names.values()):
            print(f"no bot named {sorted(args.only)}", file=sys.stderr)
            return 2
    missing = set(skip_names) - set(names.values())
    if missing:
        print(f"note: no bot named {sorted(missing)}", file=sys.stderr)
    status, listing = call("GET", "/v1/rules")
    if status != 200:
        print(f"cannot list rules: HTTP {status}", file=sys.stderr)
        return 2
    rules = listing["rules"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(args.backup_dir) / f"rules-before-{'0.12.47' if args.only else '0.12.45'}-{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(listing, indent=1), encoding="utf-8")
    backup.chmod(0o600)
    targets = [rule for rule in rules if rule.get("bot_id") and rule["bot_id"] not in skip_ids
               and rule.get("effect") == "ask" and str(rule.get("note") or "").startswith(NOTE_PREFIXES)]
    print(f"backup: {backup} ({len(rules)} rules)")
    print(f"{len(targets)} E8/interim ask rules to remove across "
          f"{len({rule['bot_id'] for rule in targets})} bots; "
          + (f"only: {', '.join(args.only)}" if args.only else f"skipped: {', '.join(skip_names)}"))
    removed = 0
    for rule in targets:
        if args.dry_run:
            continue
        status, _ = call("DELETE", f"/v1/rules/{rule['id']}")
        if status in (204, 404):  # 404: already gone (idempotent)
            removed += status == 204
        else:
            print(f"rule {rule['id']} ({names.get(rule['bot_id'], rule['bot_id'])}): HTTP {status}", file=sys.stderr)
    status, after = call("GET", "/v1/rules")
    left = [rule for rule in after["rules"] if rule.get("bot_id") and rule["bot_id"] not in skip_ids
            and rule.get("effect") == "ask" and str(rule.get("note") or "").startswith(NOTE_PREFIXES)] if status == 200 else []
    print(f"removed {removed}{' (dry run)' if args.dry_run else ''}; rules now {len(after['rules']) if status == 200 else '?'}; "
          f"E8/interim ask rules left outside the skipped bots: {len(left)}")
    return 0 if args.dry_run or not left else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
