#!/usr/bin/env bash
# Reliable status/restart for the Bot-Office systemd USER service, from any shell (non-login ssh,
# cron, a bot job). Plain `systemctl --user ...` fails there ("Failed to connect to bus: No medium
# found") when XDG_RUNTIME_DIR is unset, and plain `systemctl status bot-office` (no --user) says
# "Unit bot-office.service could not be found" because it looks in the system manager.
#
#   scripts/svc.sh status     unit state, MainPID, the process listening on the API port, and
#                             whether they match (exit 2 when the listener is not the unit's)
#   scripts/svc.sh logs [N]   last N journal lines (default 40)
#   scripts/svc.sh restart [--force]   refuses while a job is running unless --force; then
#                             restarts, waits for the port and re-checks the PID match
#   scripts/svc.sh stop | start        same environment, same unit
#
# Env: BOT_OFFICE_UNIT (default bot-office.service), BOT_OFFICE_PORT / AGENTDESK_PORT (default
# 8787). The job count reads the database named by the RUNNING service's own environment
# (BOT_OFFICE_PRIVATE_DIR, else BOT_OFFICE_DATA_DIR); if that cannot be identified, restart refuses.
set -u

UNIT="${BOT_OFFICE_UNIT:-bot-office.service}"
PORT="${BOT_OFFICE_PORT:-${AGENTDESK_PORT:-8787}}"
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -e "$SELF" ]; then
  REPO="$(cd "$(dirname "$(readlink -f "$SELF")")/.." && pwd)"
else
  REPO="$PWD"  # piped into bash: run it from the repo
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
if [ ! -d "$XDG_RUNTIME_DIR" ]; then
  echo "svc: $XDG_RUNTIME_DIR does not exist: the user manager is not running (linger off? run: sudo loginctl enable-linger $(id -un))" >&2
  exit 3
fi

ctl() { systemctl --user "$@"; }

listener_pid() {  # pid listening on the API port (ss shows our own processes without root)
  ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2
}

main_pid() { ctl show "$UNIT" -p MainPID --value 2>/dev/null; }

service_db() {  # the database the RUNNING service uses, from its own environment; empty when unknown
  local env_text pid
  pid="$(main_pid)"
  if [ -n "$pid" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/environ" ]; then
    env_text="$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null)"
  else
    env_text="$(ctl show "$UNIT" -p Environment --value 2>/dev/null | tr ' ' '\n')"
  fi
  python3 -c '
import sys
env = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line)
env = {k.strip("\""): v.strip("\"") for k, v in env.items()}
# same rule as Settings.control_dir: the private dir wins, else the data dir
base = env.get("BOT_OFFICE_PRIVATE_DIR") or env.get("BOT_OFFICE_DATA_DIR") or env.get("AGENTDESK_DATA_DIR")
print(base + "/agentdesk.sqlite3" if base else "")
' <<<"$env_text"
}

running_jobs() {  # read-only count in the service's own database; "?" when it cannot be identified or read
  local db
  db="$(service_db)"
  [ -n "$db" ] && [ -f "$db" ] || { echo "?"; return; }
  python3 -c '
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
print(db.execute("SELECT count(*) FROM jobs WHERE status=\"running\"").fetchone()[0])
' "$db" 2>/dev/null || echo "?"
}

guarded_restart() {  # count running jobs and ask for the restart while holding the database write lock:
  # Store.claim starts a job inside BEGIN IMMEDIATE, so nothing can be claimed between the check and
  # the restart request (--no-block: the stopping service must be able to write once we release)
  local db
  db="$(service_db)"
  [ -n "$db" ] && [ -f "$db" ] || { echo "svc: cannot identify the service's database (BOT_OFFICE_PRIVATE_DIR / BOT_OFFICE_DATA_DIR not in its environment); refusing (use --force to restart anyway)" >&2; return 5; }
  python3 -c '
import sqlite3, subprocess, sys
db = sqlite3.connect(sys.argv[1], timeout=10, isolation_level=None)
db.execute("BEGIN IMMEDIATE")
n = db.execute("SELECT count(*) FROM jobs WHERE status=\"running\"").fetchone()[0]
if n:
    print(f"svc: running jobs: {n}; a restart would interrupt them (use --force to restart anyway)", file=sys.stderr)
    sys.exit(4)
sys.exit(subprocess.run(["systemctl", "--user", "restart", "--no-block", sys.argv[2]]).returncode)
' "$db" "$UNIT"
}

status() {
  if ! ctl cat "$UNIT" >/dev/null 2>&1; then
    echo "svc: $UNIT is not a known user unit (is it installed? ls ~/.config/systemd/user)" >&2
    return 3
  fi
  local state main listen in_unit
  state="$(ctl show "$UNIT" -p ActiveState,SubState --value | paste -sd/ -)"
  main="$(main_pid)"
  listen="$(listener_pid)"
  echo "unit:     $UNIT ($state), enabled=$(ctl is-enabled "$UNIT" 2>&1)"
  echo "MainPID:  ${main:-0}"
  echo "listener: ${listen:-none} on port $PORT"
  echo "linger:   $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo '?')"
  echo "running jobs: $(running_jobs)"
  if [ -z "$listen" ]; then
    echo "RESULT: nothing is listening on port $PORT"; return 2
  fi
  if [ "$listen" = "${main:-0}" ]; then
    echo "RESULT: OK (the listener is the unit's MainPID)"; return 0
  fi
  in_unit="$(tr '\0' '\n' <"/proc/$listen/cgroup" 2>/dev/null | grep -c "$UNIT")"
  if [ "${in_unit:-0}" -gt 0 ]; then
    echo "RESULT: OK (the listener $listen runs inside the unit's cgroup)"; return 0
  fi
  echo "RESULT: MISMATCH: pid $listen listens on $PORT but is not $UNIT (MainPID ${main:-0}): a process started outside systemd; do not restart blindly" >&2
  return 2
}

restart() {
  local i rc
  if [ "${1:-}" = "--force" ]; then
    ctl restart "$UNIT" || return $?
  else
    guarded_restart; rc=$?
    [ "$rc" -eq 0 ] || return "$rc"
    sleep 1
  fi
  for i in $(seq 1 30); do
    [ -n "$(listener_pid)" ] && break
    sleep 1
  done
  status
}

case "${1:-status}" in
  status) status ;;
  logs) journalctl --user -u "$UNIT" -n "${2:-40}" --no-pager ;;
  restart) restart "${2:-}" ;;
  stop|start) ctl "$1" "$UNIT" ;;
  *) echo "usage: svc.sh status | logs [N] | restart [--force] | stop | start" >&2; exit 64 ;;
esac
