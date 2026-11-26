#!/usr/bin/env bash
# Read-only host check for Bot-Office remote access. Prints no file contents.
# Usage: deploy/remote-access/check.sh [port] [remote_access.json path]
set -euo pipefail

PORT="${1:-8787}"
CONFIG="${2:-${BOT_OFFICE_REMOTE_ACCESS_FILE:-${HOME}/bot-office/data/remote_access.json}}"
fail=0

echo "== listener on :${PORT} =="
listeners="$(ss -ltn 2>/dev/null | awk -v p=":${PORT}\$" '$4 ~ p {print $4}' || true)"
if [ -z "$listeners" ]; then
  echo "FAIL: nothing listens on port ${PORT} (is the Bot-Office service running?)"
  fail=1
else
  while IFS= read -r addr; do
    case "$addr" in
      127.0.0.1:*|"[::1]":*) echo "ok: ${addr} (loopback only)" ;;
      *) echo "FAIL: ${addr} is not loopback"; fail=1 ;;
    esac
  done <<<"$listeners"
fi

echo "== remote access config =="
if [ -f "$CONFIG" ]; then
  if python3 -m json.tool "$CONFIG" >/dev/null 2>&1; then
    echo "ok: ${CONFIG} exists and parses as JSON"
  else
    echo "FAIL: ${CONFIG} is not valid JSON"; fail=1
  fi
  mode="$(stat -c '%a' "$CONFIG" 2>/dev/null || echo '?')"
  echo "mode: ${mode}"
else
  echo "FAIL: ${CONFIG} does not exist (remote access stays disabled)"; fail=1
fi

echo "== cloudflared =="
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 'cloudflared*' 2>/dev/null | grep -q cloudflared; then
  systemctl is-active cloudflared 2>/dev/null || true
else
  echo "cloudflared service not installed here (fine if you have not set up the tunnel yet)"
fi

exit "$fail"
