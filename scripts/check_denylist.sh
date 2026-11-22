#!/usr/bin/env bash
# Block personal values, private-file text and forbidden paths before they reach the public repo.
#
#   scripts/check_denylist.sh --staged            the index about to be committed (+ the commit identity)
#   scripts/check_denylist.sh --message FILE      a commit message file (commit-msg hook)
#   scripts/check_denylist.sh --commits REV...    every commit in `git rev-list REV...` (e.g. "main..topic"
#                                                 or "topic --not --remotes=origin"): added lines, file
#                                                 names, messages, author and committer
#
# Denied values live OUTSIDE the repo, so the list itself is never published:
#   ${BO_PUSH_DENYLIST:-~/.config/bot-office/push-denylist.txt}
# one literal per line, case-insensitive substring match, '#' lines ignored.
# A missing or empty denylist fails closed (exit 3). A match exits 1 and names only the commit and
# file, never the matched value. Clean exits 0.
set -euo pipefail
DL="${BO_PUSH_DENYLIST:-$HOME/.config/bot-office/push-denylist.txt}"
# Paths that must never be committed, whatever their content (extended regexes on the path).
FORBIDDEN_PATHS='^(policy-pack/RULES\.md|tests/fixtures/installed-RULES-[^/]*|data/shared/.*)$'

die() { echo "check_denylist: $*" >&2; exit "${2:-1}"; }
[ -r "$DL" ] || die "denylist $DL is missing or unreadable; refusing (fail closed)" 3
PAT=$(mktemp); trap 'rm -f "$PAT" "$PAT.names" "$PAT.hits"' EXIT
grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$DL" > "$PAT" || true
[ -s "$PAT" ] || die "denylist $DL is empty; refusing (fail closed)" 3

hit() { grep -qiF -f "$PAT"; }   # stdin matches any denied literal (value never printed)
bad=0
report() { echo "check_denylist: BLOCKED: $*" >&2; bad=1; }

check_paths() {  # $1 label; stdin: file names
  local p; while IFS= read -r p; do
    [ -n "$p" ] || continue
    if printf '%s\n' "$p" | grep -qE "$FORBIDDEN_PATHS"; then report "$1: forbidden path $p"; fi
    if printf '%s\n' "$p" | hit; then report "$1: a file name contains a denylisted value"; fi
  done
}

case "${1:-}" in
  --staged)
    check_paths "staged" < <(git diff --cached --name-only --diff-filter=ACMR)
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      if git diff --cached --no-color -U0 -- "$f" | grep '^+' | grep -v '^+++ ' | hit; then report "staged: $f adds a denylisted value"; fi
    done < <(git diff --cached --name-only --diff-filter=ACMR)
    if { git var GIT_AUTHOR_IDENT; git var GIT_COMMITTER_IDENT; } 2>/dev/null | hit; then
      report "staged: the commit author/committer identity is denylisted (set the repo-local user.name/user.email)"
    fi
    ;;
  --message)
    [ -r "${2:-}" ] || die "--message needs a readable file" 2
    if grep -v '^#' "$2" | hit; then report "the commit message contains a denylisted value"; fi
    ;;
  --commits)
    shift; [ $# -gt 0 ] || die "--commits needs a revision range" 2
    # Whole-range passes: every line is labelled "commit<TAB>what<TAB>content", grep runs once, and only
    # the labels (cut -f1,2) are ever printed, never the content.
    flag() { grep -iF -f "$PAT" | cut -f1,2 | sort -u || true; }
    # 1. identities and messages
    git log --no-color --format='@@C %H%n%an %ae %cn %ce%n%B' "$@" \
      | awk '/^@@C /{c=substr($2,1,10); n=0; next} {n++; print c"\t"(n==1?"identity":"message")"\t"$0}' | flag > "$PAT.hits"
    while IFS=$'\t' read -r c k; do
      if [ "$k" = identity ]; then report "commit $c: author/committer identity is denylisted"
      else report "commit $c: the commit message contains a denylisted value"; fi
    done < "$PAT.hits"
    # 2. file names: forbidden paths, and names that carry a denylisted value
    git log --no-color --format='@@C %H' --name-only --diff-filter=ACMR -m --first-parent "$@" \
      | awk '/^@@C /{c=substr($2,1,10); next} NF{print c"\t"$0}' | sort -u > "$PAT.names"
    FP="$FORBIDDEN_PATHS" awk -F'\t' '$2 ~ ENVIRON["FP"] {print $1"\t"$2}' "$PAT.names" | while IFS=$'\t' read -r c f; do
      echo "check_denylist: BLOCKED: commit $c: forbidden path $f" >&2; echo x; done > "$PAT.hits"
    [ -s "$PAT.hits" ] && bad=1
    grep -iF -f "$PAT" "$PAT.names" | cut -f1 | sort -u > "$PAT.hits" || true
    while read -r c; do report "commit $c: a file name contains a denylisted value"; done < "$PAT.hits"
    # 3. added lines
    git log --no-color --format='@@C %H' -p -U0 --diff-filter=ACMR -m --first-parent "$@" \
      | awk '/^@@C /{c=substr($2,1,10); next} /^\+\+\+ /{f=substr($0,7); next} /^\+/{print c"\t"f"\t"$0}' | flag > "$PAT.hits"
    while IFS=$'\t' read -r c f; do report "commit $c: $f adds a denylisted value"; done < "$PAT.hits"
    ;;
  *) die "usage: $0 --staged | --message FILE | --commits REV..." 2 ;;
esac
[ "$bad" = 0 ] || die "denylisted content found (see above; values are not shown). Nothing was committed/pushed." 1
exit 0
