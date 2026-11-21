#!/usr/bin/env bash
# First deployment only: the live master ignores PAUSE, so quiesce it, stop it, and start the new code
# (which reads PAUSE before its first claim and starts paused). Fails closed: any running or queued job
# at the second check aborts BEFORE stop, and start runs only after a successful stop.
# Needs BOT_OFFICE_DATA_DIR (pinned; no default). Uses python3's sqlite3 (the server has no sqlite3 CLI).
# Usage: BOT_OFFICE_DATA_DIR=<dir> scripts/first_deploy_pause.sh   (then rm "$BOT_OFFICE_DATA_DIR/PAUSE" once /healthz shows paused)
set -euo pipefail
: "${BOT_OFFICE_DATA_DIR:?set BOT_OFFICE_DATA_DIR to the controller data directory}"
DB="$BOT_OFFICE_DATA_DIR/agentdesk.sqlite3"
[ -f "$DB" ] || { echo "abort: no database at the pinned data dir" >&2; exit 2; }
UNIT="${BOT_OFFICE_UNIT:-bot-office}"
WAIT="${BOT_OFFICE_QUIESCE_SECONDS:-900}"

busy() {  # prints the number of running + queued jobs; a failed read is an error, never zero
  python3 -c 'import sqlite3,sys,pathlib; c=sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True); print(c.execute("SELECT COUNT(*) FROM jobs WHERE status IN (\"running\",\"queued\")").fetchone()[0])' "$DB"
}

touch "$BOT_OFFICE_DATA_DIR/PAUSE"   # ignored by the old code; the NEW process starts paused
waited=0
until [ "$(busy)" = 0 ]; do
  [ "$waited" -ge "$WAIT" ] && { echo "abort: jobs still running or queued; nothing stopped" >&2; exit 1; }
  sleep 1; waited=$((waited + 1))
done
sleep "${BOT_OFFICE_RECHECK_SECONDS:-5}"
n="$(busy)"
if [ "$n" != 0 ]; then
  echo "abort: $n job(s) running or queued at the second check; nothing stopped or started" >&2
  exit 1
fi
systemctl --user stop "$UNIT"
systemctl --user start "$UNIT"
echo "started paused; check /healthz, then rm \"\$BOT_OFFICE_DATA_DIR/PAUSE\""
