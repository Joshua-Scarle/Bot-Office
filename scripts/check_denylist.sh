#!/usr/bin/env bash
# Block personal values, private-file text and forbidden paths before they reach the public repo.
#
#   scripts/check_denylist.sh --staged            the index about to be committed (+ the commit identity)
#   scripts/check_denylist.sh --message FILE      a commit message file (commit-msg hook)
#   scripts/check_denylist.sh --commits REV...    every commit in `git rev-list REV...` (e.g. "main..topic"
#                                                 or "topic --not --remotes=origin"): added lines, file
#                                                 names, messages, author and committer
#   scripts/check_denylist.sh --identities REV... every author AND committer email in `git rev-list REV...`
#                                                 must be on the noreply allowlist below (no denylist needed)
#
# The identity allowlist (sec-fixes, after a GitHub merge commit carried a personal address into the
# public repo): GitHub noreply addresses (<login>@ or <id>+<login>@users.noreply.github.com, and
# noreply@github.com), noreply@anthropic.com, and the bot @local identities already in history. Any
# other email is refused, so no personal address has to be named here. Extra allowed addresses
# (exact, one per line, '#' comments) may be added in ${BO_PUSH_IDENTITY_ALLOWLIST:-
# ~/.config/bot-office/push-identity-allowlist.txt}. Only the commit and which field are printed.
#
# Denied values live OUTSIDE the repo, so the list itself is never published:
#   ${BO_PUSH_DENYLIST:-~/.config/bot-office/push-denylist.txt}
# one literal per line, case-insensitive substring match, '#' lines ignored.
# A missing or empty denylist fails closed (exit 3). A match exits 1 and names only the commit and
# file, never the matched value. Clean exits 0.
set -euo pipefail
# inspect the objects that are really pushed: local refs/replace/* must not change what git log or cat-file show
export GIT_NO_REPLACE_OBJECTS=1
DL="${BO_PUSH_DENYLIST:-$HOME/.config/bot-office/push-denylist.txt}"
# Paths that must never be committed, whatever their content (extended regexes on the path).
FORBIDDEN_PATHS='^(policy-pack/RULES\.md|tests/fixtures/installed-RULES-[^/]*|data/shared/.*)$'

die() { echo "check_denylist: $*" >&2; exit "${2:-1}"; }

NOREPLY_RE='^([0-9]+\+)?[a-z0-9]([a-z0-9-]{0,38})(\[bot\])?@users\.noreply\.github\.com$|^noreply@github\.com$|^noreply@anthropic\.com$|^bot-office(-[a-z0-9-]+)?@local$|^(e7|dev)@localhost$|^e7@botoffice\.local$'
if [ "${1:-}" = "--identities" ]; then
  shift; [ $# -gt 0 ] || die "--identities needs a revision range" 2
  XA="${BO_PUSH_IDENTITY_ALLOWLIST:-$HOME/.config/bot-office/push-identity-allowlist.txt}"
  extra=""; [ -r "$XA" ] && extra=$({ grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$XA" || true; } | tr 'A-Z' 'a-z' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  # a graft or a shallow boundary would hide ancestors from the walk below: refuse (fail closed)
  gf=$(git rev-parse --git-path info/grafts 2>/dev/null || echo "")
  [ -n "$gf" ] && [ -s "$gf" ] && die "this repository has grafts ($gf); refusing (they can hide commits)" 3
  [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = true ] && die "a shallow repository cannot show every commit; refusing" 3
  for rev in "$@"; do  # every annotated tag in a chain (a tag of a tag ...) carries its own tagger email
    case "$rev" in -*) continue;; esac
    obj="$rev"; depth=0
    while [ "$(git cat-file -t "$obj" 2>/dev/null)" = tag ]; do
      depth=$((depth + 1)); [ "$depth" -le 20 ] || die "BLOCKED: tag $rev: too many nested tags" 1
      header=$(git cat-file tag "$obj" | sed '/^$/q')   # the header only, never the message
      [ "$(printf '%s\n' "$header" | grep -c '^tagger ')" = 1 ] || die "BLOCKED: tag $rev: not exactly one tagger" 1
      tline=$(printf '%s\n' "$header" | grep '^tagger ')
      # git's form exactly: "tagger NAME <EMAIL> SECONDS +ZZZZ", one < and one >; anything else is refused
      printf '%s\n' "$tline" | grep -qE '^tagger [^<>]* <[^<>]*> [0-9]+ [+-][0-9]{4}$' \
        || die "BLOCKED: tag $rev: a malformed or ambiguous tagger line" 1
      te=$(printf '%s\n' "$tline" | sed -n 's/^tagger [^<>]* <\([^<>]*\)>.*/\1/p' | tr 'A-Z' 'a-z')
      printf '%s\n' "$te" | grep -qxE "$NOREPLY_RE" || { [ -n "$extra" ] && printf '%s\n' "$extra" | grep -qxF -- "$te"; } \
        || die "BLOCKED: tag $rev: a tagger email is not a noreply address on the allowlist" 1
      [ "$(printf '%s\n' "$header" | grep -c '^object ')" = 1 ] || die "BLOCKED: tag $rev: not exactly one object line" 1
      obj=$(printf '%s\n' "$header" | sed -n 's/^object //p')
      printf '%s\n' "$obj" | grep -qxE '[0-9a-f]{40}|[0-9a-f]{64}' || die "BLOCKED: tag $rev: a malformed object line" 1
    done
  done
  out=$(git log --no-color --format='%H%x09%ae%x09%ce' "$@") || die "cannot list the commits ($*); refusing (fail closed)" 3
  bad=0
  while IFS=$'\t' read -r c ae ce; do
    [ -n "$c" ] || continue
    for pair in "author:$ae" "committer:$ce"; do
      who="${pair%%:*}"; email=$(printf '%s' "${pair#*:}" | tr 'A-Z' 'a-z')
      if printf '%s\n' "$email" | grep -qE "$NOREPLY_RE"; then continue; fi
      if [ -n "$extra" ] && printf '%s\n' "$extra" | grep -qxF -- "$email"; then continue; fi
      echo "check_denylist: BLOCKED: commit ${c:0:10}: the $who email is not a noreply address on the allowlist" >&2; bad=1
    done
  done <<< "$out"
  [ "$bad" = 0 ] || die "non-noreply commit identity found (values are not shown). Re-author with a noreply identity." 1
  exit 0
fi
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
  *) die "usage: $0 --staged | --message FILE | --commits REV... | --identities REV..." 2 ;;
esac
[ "$bad" = 0 ] || die "denylisted content found (see above; values are not shown). Nothing was committed/pushed." 1
exit 0
