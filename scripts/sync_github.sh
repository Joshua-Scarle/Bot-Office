#!/usr/bin/env bash
# Push master (and optionally the deployed branch) to origin after a secret scan.
# Idempotent: nothing to push means nothing happens. Plain push only, never forced.
#   scripts/sync_github.sh [--quiet] [branch ...]     master is always included
# A branch whose new commits (those not on origin) contain a gitleaks finding or a denylisted value
# (scripts/check_denylist.sh: personal values and private-file text from the out-of-repo list
# ~/.config/bot-office/push-denylist.txt, plus forbidden paths) is NOT pushed; the script exits 2 and logs
# the branch, never the value. A missing denylist blocks everything (fail closed). So does any commit whose
# author or committer email is not a noreply address on the allowlist (check_denylist.sh --identities).
# Needs gitleaks (~/.local/bin/gitleaks or PATH). The pre-push hook repeats both checks.
set -u
REPO="${BOT_OFFICE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
GL="${GITLEAKS:-$(command -v gitleaks || echo "$HOME/.local/bin/gitleaks")}"
LOG="${BOT_OFFICE_SYNC_LOG:-$HOME/.local/state/bot-office/github-sync.log}"
QUIET=0; [ "${1:-}" = "--quiet" ] && { QUIET=1; shift; }
mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; [ "$QUIET" = 1 ] || echo "$*"; }
exec 9>"$LOG.lock"; flock -n 9 || { log "another sync is running; skipped"; exit 0; }
cd "$REPO" || exit 1
[ -x "$GL" ] || { log "FAIL gitleaks not found; nothing pushed"; exit 1; }
git fetch -q origin || { log "FAIL fetch origin"; exit 1; }
rc=0
for b in master "$@"; do
  git rev-parse -q --verify "refs/heads/$b" >/dev/null || { log "skip $b: no local branch"; continue; }
  base=origin/master; git rev-parse -q --verify "origin/$b" >/dev/null && base="origin/$b"
  if [ "$(git rev-list --count "$base..$b")" = 0 ] && git rev-parse -q --verify "origin/$b" >/dev/null; then
    [ "$QUIET" = 1 ] || echo "$b: in sync"; continue
  fi
  if ! "$REPO/scripts/check_denylist.sh" --identities "refs/heads/$b" 2>>"$LOG"; then
    log "BLOCKED $b: a commit in its history has a non-noreply author or committer email (not pushed; run scripts/check_denylist.sh --identities $b)"; rc=2; continue
  fi
  if ! "$REPO/scripts/check_denylist.sh" --commits "refs/heads/$b" --not --remotes=origin 2>>"$LOG"; then
    log "BLOCKED $b: denylisted value or forbidden path in commits not on origin (not pushed; run scripts/check_denylist.sh --commits $b --not --remotes=origin)"; rc=2; continue
  fi
  if ! "$GL" git "$REPO" --log-opts="$base..refs/heads/$b" --redact --no-banner -l error -v >/dev/null 2>&1; then
    log "BLOCKED $b: gitleaks finding in commits $base..$b (not pushed; run gitleaks locally to see it)"; rc=2; continue
  fi
  if git push -q origin "refs/heads/$b:refs/heads/$b" 2>>"$LOG"; then log "pushed $b ($(git rev-parse --short "$b"))"
  else log "FAIL push $b (diverged or auth? never forced; see $LOG)"; rc=1; fi
done
exit $rc
