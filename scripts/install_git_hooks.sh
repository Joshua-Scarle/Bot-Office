#!/usr/bin/env bash
# Install the Bot Office git hooks into the shared hooks dir (applies to the main checkout and every worktree).
# Copies scripts/check_denylist.sh next to them, so worktrees on branches without the script are covered too.
# Re-run after changing any of these files. Also sets the repo-local commit identity to the neutral one.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
H="$(git -C "$REPO" rev-parse --git-common-dir)"; case "$H" in /*) ;; *) H="$REPO/$H";; esac; H="$H/hooks"
mkdir -p "$H"
install -m 0755 "$REPO/scripts/check_denylist.sh" "$H/bo-check-denylist"
install -m 0755 "$REPO/scripts/git-hooks/pre-commit" "$H/pre-commit"
install -m 0755 "$REPO/scripts/git-hooks/pre-push" "$H/pre-push"
install -m 0755 "$REPO/scripts/git-hooks/commit-msg" "$H/commit-msg"
git -C "$REPO" config user.name "${BO_GIT_NAME:-Bot Office}"
git -C "$REPO" config user.email "${BO_GIT_EMAIL:-bot-office@users.noreply.github.com}"
echo "hooks installed in $H; denylist: ${BO_PUSH_DENYLIST:-$HOME/.config/bot-office/push-denylist.txt}"
