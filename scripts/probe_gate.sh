#!/usr/bin/env bash
# Operator check: does each official CLI obey Bot-Office's action gate on this host?
# Runs each named CLI twice (fresh + resumed; 2 small model calls per provider) with the exact
# unattended flags Bot-Office uses, against a throwaway loopback gate that refuses a canary
# command, and records enforced / advisory / none in gate-capabilities.json (private dir).
# Strict-profile runs (watcher triggers; scheduled turns of bots set to scheduled_runs=gated)
# start only for providers recorded as enforced. Reads no sign-in files; each CLI uses its own login.
#
# Usage: scripts/probe_gate.sh claude [codex cursor antigravity opencode]
#        scripts/probe_gate.sh --status
# Run it as the user the controller runs as (with the isolation layout: sudo -u botoffice -E ...).
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-$project_dir/.venv/bin/python}"
[ -x "$python" ] || python=python3
if [ "${1:-}" = "--status" ]; then
  exec "$python" -m botoffice gate-status
fi
[ "$#" -ge 1 ] || { echo "usage: $0 PROVIDER... | --status" >&2; exit 2; }
status=0
for provider in "$@"; do
  "$python" -m botoffice probe-gate "$provider" || status=1
done
exit "$status"
