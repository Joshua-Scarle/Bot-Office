#!/usr/bin/env bash
# Bot-Office 0.11.0 isolation installer (C1/B14).
#
#   deploy/install-isolation.sh             dry run: print every planned action, change nothing
#   deploy/install-isolation.sh --apply     do it (root; idempotent, safe to run again)
#   deploy/install-isolation.sh --rollback  back to the single-user layout (root)
#
# Result: the controller runs as system user botoffice (system unit, User=botoffice) from a
# root-owned copy of the code in /opt/bot-office; the master token, the SQLite DB and the Grok
# webhook config live in /var/lib/bot-office/private (0700 botoffice); the shared tree
# (workspaces, bots, shared, inboxes) is /var/lib/bot-office/data, group bo-share with setgid dirs
# and default ACLs. Provider CLIs and podman run as the bots user only through
#   botoffice ALL=(ubuntu) NOPASSWD: /usr/local/libexec/bot-office-run
# The old user unit is disabled, not deleted. ubuntu's own sudo is not touched, but --apply
# refuses while the bots user has sudo without a password (a bot could reach it outside
# no_new_privs through the user's own systemd, cron or ssh); the result is recorded in
# $PRIVATE_DIR/sudo-check and the GUI claims "token and DB private" only when it passed.
# This script never prints the token, the DB or the webhook config.
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOTS_USER="${BOTS_USER:-ubuntu}"
CTRL_USER="${CTRL_USER:-botoffice}"
SHARE_GROUP="${SHARE_GROUP:-bo-share}"
OLD_DATA="${OLD_DATA:-$src/data}"
APP_DIR="${APP_DIR:-/opt/bot-office}"
STATE_DIR="${STATE_DIR:-/var/lib/bot-office}"
PRIVATE_DIR="$STATE_DIR/private"
DATA_DIR="$STATE_DIR/data"
LAUNCHER="${LAUNCHER:-/usr/local/libexec/bot-office-run}"
SUDOERS_FILE="${SUDOERS_FILE:-/etc/sudoers.d/bot-office}"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/bot-office.service}"
MANIFEST="$STATE_DIR/isolation-installed"
PRIVATE_FILES=(token agentdesk.sqlite3 agentdesk.sqlite3-wal agentdesk.sqlite3-shm grok_webhook.json
  gate-capabilities.json)
# controller-only per-bot state that moves from bots/<id>/ to $PRIVATE_DIR/bot-state/<id>/
STATE_PATTERNS=('*-session' '*-session.*' .handoff-pending self.sha256 rules.sha256 codex-usage-baseline.json)
SHARED_DIRS=(workspaces bots shared inboxes)

mode="dry-run"
case "${1:-}" in
  "") ;;
  --apply) mode="apply" ;;
  --rollback) mode="rollback" ;;
  --dry-run-rollback) mode="dry-rollback" ;;
  *) echo "usage: $0 [--apply | --rollback | --dry-run-rollback]" >&2; exit 2 ;;
esac
applying() { [[ "$mode" == "apply" || "$mode" == "rollback" ]]; }

run() {
  # print the command; run it only when applying
  if applying; then
    printf '+ %s\n' "$*"
    "$@"
  else
    printf 'would run: %s\n' "$*"
  fi
}

write_file() {  # write_file DEST MODE OWNER:GROUP < content
  local dest="$1" fmode="$2" owner="$3" content
  content="$(cat)"
  if applying; then
    printf '+ write %s (%s %s)\n' "$dest" "$fmode" "$owner"
    local tmp
    tmp="$(mktemp "$(dirname "$dest")/.bot-office.XXXXXX")"
    printf '%s\n' "$content" > "$tmp"
    chown "$owner" "$tmp"
    chmod "$fmode" "$tmp"
    mv -f "$tmp" "$dest"
  else
    printf 'would write %s (%s %s):\n%s\n' "$dest" "$fmode" "$owner" "$(sed 's/^/    | /' <<<"$content")"
  fi
}

user_systemctl() {  # systemctl --user for the bots user (their old Bot-Office unit)
  local uid
  uid="$(id -u "$BOTS_USER")"
  run runuser -u "$BOTS_USER" -- env "XDG_RUNTIME_DIR=/run/user/$uid" systemctl --user "$@"
}

sudo_check() {
  # 0.11.0 review Sec 2: no_new_privs only holds while the bots user's sudo needs a password.
  # Sets sudo_result=passed, or refuses --apply when sudo works without one.
  local listing
  sudo_result="unchecked"
  if [[ "$mode" == "dry-run" && -n "${BO_TEST_SUDO_LIST:-}" ]]; then
    listing="$(cat "$BO_TEST_SUDO_LIST")"  # tests only; never honoured by --apply
  elif [[ "$(id -u)" == 0 ]]; then
    listing="$(sudo -n -l -U "$BOTS_USER" 2>&1 || true)"
  else
    printf 'would run: sudo -l -U %s (refuse --apply if it needs no password)\n' "$BOTS_USER"
    return 0
  fi
  if grep -Eq 'NOPASSWD|!authenticate' <<<"$listing"; then
    cat >&2 <<MSG
REFUSED: $BOTS_USER can use sudo without a password (sudo -l -U $BOTS_USER shows NOPASSWD or
!authenticate). A bot running as $BOTS_USER could reach that sudo outside no_new_privs (its own
systemd --user, cron, at or ssh localhost) and read the token and the DB, so isolation would
not protect them. Decide one of:
  - make $BOTS_USER's sudo ask for a password (edit its rule in /etc/sudoers.d with visudo,
    drop NOPASSWD / !authenticate), then run --apply again; or
  - remove $BOTS_USER from sudo entirely; or
  - run the bots as a different user without sudo (BOTS_USER=<user>).
Nothing was changed.
MSG
    if applying; then
      exit 1
    fi
    echo "--apply would REFUSE (see above)"
    sudo_result="nopasswd"
    return 0
  fi
  sudo_result="passed"
  echo "sudo check: $BOTS_USER's sudo needs a password (or none is configured)"
}

checkout_check() {
  # Sec 7: the copy that will run as botoffice comes from this checkout; show exactly what it is
  local writable commit digest
  writable="$(find "$src" \( -path "$src/.git" -o -path "$src/.venv" -o -path "$src/data" \) -prune -o \
    \( -perm -g+w -o -perm -o+w \) -print -quit 2>/dev/null)"
  if [[ -n "$writable" ]]; then
    echo "REFUSED: $src is group- or other-writable (e.g. $writable); run chmod -R go-w $src first" >&2
    if applying; then
      exit 1
    fi
  fi
  commit="$(git -C "$src" rev-parse HEAD 2>/dev/null || echo "not a git checkout")"
  digest="$(cd "$src" && find src bin deploy pyproject.toml -type f ! -path '*/__pycache__/*' -print0 2>/dev/null \
    | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
  echo "code: commit $commit; sha256 of src/ bin/ deploy/ pyproject.toml: $digest"
  if [[ -n "$(git -C "$src" status --porcelain 2>/dev/null)" ]]; then
    echo "warning: the checkout has uncommitted changes (they are copied too)" >&2
  fi
}

refuse_link() {  # refuse_link PATH...: root never cp/chowns through a link the bots user planted
  local path
  for path in "$@"; do
    if [[ -L "$path" ]]; then
      echo "REFUSED: $path is a symlink; remove it (or replace it with the real file) and run again" >&2
      exit 1
    fi
  done
}

old_service_stopped() {
  # Sec 7: no second controller on :8787 and no torn DB copy
  local pids
  pids="$(pgrep -u "$BOTS_USER" -f -- '-m (botoffice|agentdesk)' || true)"
  if [[ -n "$pids" ]]; then
    echo "REFUSED: the old Bot-Office controller is still running as $BOTS_USER (pids: ${pids//$'\n'/ });" \
      "stop it (systemctl --user stop bot-office.service) and run again" >&2
    exit 1
  fi
}

need_root() {
  if [[ "$(id -u)" != 0 ]]; then
    echo "$mode needs root (run it with sudo); a plain run is a dry run" >&2
    exit 1
  fi
}

preflight() {
  id "$BOTS_USER" >/dev/null 2>&1 || { echo "bots user $BOTS_USER does not exist" >&2; exit 1; }
  [[ -f "$src/deploy/bot-office-run" ]] || { echo "missing $src/deploy/bot-office-run" >&2; exit 1; }
  [[ -x /usr/bin/python3 ]] || { echo "the launcher needs /usr/bin/python3" >&2; exit 1; }
  for tool in sudo visudo setfacl rsync runuser loginctl systemctl; do
    command -v "$tool" >/dev/null 2>&1 || echo "warning: $tool not found (needed by --apply)" >&2
  done
}

sudoers_content() {
  cat <<EOF
# Bot-Office 0.11.0 (deploy/install-isolation.sh). The controller may run exactly one program as
# the bots user: the root-owned launcher. Nothing else, no other target user.
Defaults!$LAUNCHER !use_pty
$CTRL_USER ALL=($BOTS_USER) NOPASSWD: $LAUNCHER
EOF
}

install_sudoers() {
  # validate the new rule on its own and the whole sudoers config afterwards
  if applying; then
    local tmp
    tmp="$(mktemp /etc/sudoers.d/.bot-office.XXXXXX)"
    sudoers_content > "$tmp"
    chmod 0440 "$tmp"
    printf '+ visudo -cf %s\n' "$tmp"
    if ! visudo -cf "$tmp" >/dev/null; then
      rm -f "$tmp"
      echo "the sudoers rule did not validate; nothing installed" >&2
      exit 1
    fi
    mv -f "$tmp" "$SUDOERS_FILE"
    printf '+ installed %s (0440 root:root)\n' "$SUDOERS_FILE"
    run visudo -c
  else
    sudoers_content | write_file "$SUDOERS_FILE" 0440 root:root
    printf 'would run: visudo -cf <new rule> (abort if it fails), then visudo -c\n'
  fi
}

do_apply() {
  preflight
  echo "== checks"
  sudo_check
  checkout_check
  local stamp backup uid
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$STATE_DIR/backup-$stamp"
  uid="$(id -u "$BOTS_USER")"

  echo "== users and group"
  getent group "$SHARE_GROUP" >/dev/null || run groupadd --system "$SHARE_GROUP"
  id "$CTRL_USER" >/dev/null 2>&1 || run useradd --system --home-dir "$PRIVATE_DIR" --no-create-home \
    --shell /usr/sbin/nologin --user-group "$CTRL_USER"
  run usermod -aG "$SHARE_GROUP" "$CTRL_USER"
  run usermod -aG "$SHARE_GROUP" "$BOTS_USER"

  echo "== stop the old single-user service (disabled, not deleted)"
  user_systemctl disable --now bot-office.service || true
  user_systemctl disable --now agentdesk.service || true
  if applying; then
    old_service_stopped
  else
    echo "would check: no old controller process left for $BOTS_USER (refuse otherwise)"
  fi

  echo "== code: root-owned copy (the bots user must not be able to change what the controller runs)"
  run install -d -o root -g root -m 0755 "$APP_DIR"
  run rsync -a --delete --exclude /data --exclude /.venv --exclude /.git --exclude __pycache__ "$src/" "$APP_DIR/"
  run chown -R root:root "$APP_DIR"
  run chmod -R go-w,a+rX "$APP_DIR"  # rsync keeps 0700 source dirs; botoffice must read the code
  [[ -x "$APP_DIR/.venv/bin/python" ]] || run python3 -m venv "$APP_DIR/.venv"
  # editable: the package stays in $APP_DIR/src, so bin/ (desk, bot-office-gate) resolves there
  run "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -e "$APP_DIR"

  echo "== launcher and sudoers"
  run install -d -o root -g root -m 0755 "$(dirname "$LAUNCHER")"
  run install -o root -g root -m 0755 "$src/deploy/bot-office-run" "$LAUNCHER"
  install_sudoers

  echo "== state dirs"
  run install -d -o root -g root -m 0755 "$STATE_DIR"
  run install -d -o "$CTRL_USER" -g "$CTRL_USER" -m 0700 "$PRIVATE_DIR"
  run install -d -o "$CTRL_USER" -g "$SHARE_GROUP" -m 2750 "$DATA_DIR"
  run install -d -o "$CTRL_USER" -g "$CTRL_USER" -m 0700 "$DATA_DIR/logs"
  if applying; then
    refuse_link "$OLD_DATA" "${PRIVATE_FILES[@]/#/$OLD_DATA/}" "${SHARED_DIRS[@]/#/$OLD_DATA/}"
  fi
  if [[ -d "$OLD_DATA" && ! -e "$MANIFEST" ]]; then
    echo "== copy the shared tree from $OLD_DATA (token, DB and webhook config are moved separately)"
    run rsync -a --exclude /token --exclude '/agentdesk.sqlite3*' --exclude /grok_webhook.json \
      "$OLD_DATA/" "$DATA_DIR/"
  fi
  if applying; then
    refuse_link "${SHARED_DIRS[@]/#/$DATA_DIR/}"
  fi
  migrate_state
  for name in "${SHARED_DIRS[@]}"; do
    # sticky (Sec 1): a bot cannot rename or delete the controller's entries to plant links
    run install -d -o "$CTRL_USER" -g "$SHARE_GROUP" -m 3770 "$DATA_DIR/$name"
    run chown -R -P "$CTRL_USER:$SHARE_GROUP" "$DATA_DIR/$name"
    run chmod -R g+rwX,o-rwx "$DATA_DIR/$name"
    run find "$DATA_DIR/$name" -type d -exec chmod g+s {} +
    run setfacl -R -P -m "g:$SHARE_GROUP:rwX" -m "d:g:$SHARE_GROUP:rwX" -m d:m::rwX "$DATA_DIR/$name"
    run chmod +t "$DATA_DIR/$name"
  done
  run find "$DATA_DIR/workspaces" "$DATA_DIR/bots" "$DATA_DIR/inboxes" -mindepth 1 -maxdepth 1 -type d \
    -exec chmod +t {} +
  run chown -R "$CTRL_USER:$CTRL_USER" "$DATA_DIR/logs"
  if [[ -d "$OLD_DATA/voice" || -d "$DATA_DIR/voice" ]]; then
    # the controller runs the voice venv: root-owned, readable by botoffice only
    run install -d -o root -g "$CTRL_USER" -m 0750 "$DATA_DIR/voice"
    run chown -R "root:$CTRL_USER" "$DATA_DIR/voice"
    run chmod -R u=rwX,g=rX,o= "$DATA_DIR/voice"
    run install -d -o "$CTRL_USER" -g "$CTRL_USER" -m 0700 "$DATA_DIR/voice/home"
  fi

  echo "== private files: token, DB (+WAL), Grok webhook config -> $PRIVATE_DIR (backup first)"
  local moved=0 name
  for name in "${PRIVATE_FILES[@]}"; do
    if [[ -e "$OLD_DATA/$name" ]]; then
      if [[ "$moved" == 0 ]]; then
        run install -d -o root -g root -m 0700 "$backup"
        moved=1
      fi
      run cp -a "$OLD_DATA/$name" "$backup/$name"
      if [[ -e "$PRIVATE_DIR/$name" ]]; then
        echo "keep existing $PRIVATE_DIR/$name (the old copy is in $backup)"
      else
        run cp -a "$OLD_DATA/$name" "$PRIVATE_DIR/$name"
        run chown "$CTRL_USER:$CTRL_USER" "$PRIVATE_DIR/$name"
        run chmod 0600 "$PRIVATE_DIR/$name"
      fi
      run rm -f "$OLD_DATA/$name"  # the bots user could read it there
    fi
  done
  [[ "$moved" == 0 ]] && echo "nothing to move (already in $PRIVATE_DIR, or no old layout)"
  if applying && [[ ! -e "$PRIVATE_DIR/token" ]]; then
    run sh -c "umask 077; openssl rand -hex 32 > '$PRIVATE_DIR/token'"
    run chown "$CTRL_USER:$CTRL_USER" "$PRIVATE_DIR/token"
  fi
  printf 'old_data=%s\nbackup=%s\ninstalled=%s\n' "$OLD_DATA" "$backup" "$stamp" \
    | write_file "$MANIFEST" 0600 root:root
  # what the GUI's "token and DB private" claim rests on (isolation-status reads it)
  printf 'result=%s\nbots_user=%s\nchecked=%s\n' "$sudo_result" "$BOTS_USER" "$stamp" \
    | write_file "$PRIVATE_DIR/sudo-check" 0600 "$CTRL_USER:$CTRL_USER"

  echo "== podman: the bots user's runtime dir (/run/user/$uid) needs lingering"
  if [[ "$(loginctl show-user "$BOTS_USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
    run loginctl enable-linger "$BOTS_USER"
  else
    echo "linger already enabled for $BOTS_USER"
  fi

  echo "== system unit"
  sed -e "s|/opt/bot-office|$APP_DIR|g; s|/var/lib/bot-office|$STATE_DIR|g; s|/usr/local/libexec/bot-office-run|$LAUNCHER|g" \
    -e "s|ubuntu|$BOTS_USER|g; s|User=botoffice|User=$CTRL_USER|; s|Group=botoffice|Group=$CTRL_USER|; s|bo-share|$SHARE_GROUP|g" \
    "$src/deploy/bot-office-system.service" | write_file "$UNIT_FILE" 0644 root:root
  run systemctl daemon-reload
  run systemctl enable --now bot-office.service

  echo "== check"
  run runuser -u "$CTRL_USER" -- env BOT_OFFICE_PRIVATE_DIR="$PRIVATE_DIR" \
    BOT_OFFICE_TOKEN_FILE="$PRIVATE_DIR/token" BOT_OFFICE_DATA_DIR="$DATA_DIR" \
    "$APP_DIR/.venv/bin/python" -m botoffice isolation-status
  echo "done ($mode). The master token is in $PRIVATE_DIR/token (root/botoffice only)."
}

migrate_state() {
  # Sec 1: session pointers, the handoff marker and the snapshots are controller-only; with
  # isolation on they live in $PRIVATE_DIR/bot-state/<id>/ (regular files only, never a link)
  local dir id pattern file
  echo "== controller-only bot state -> $PRIVATE_DIR/bot-state"
  run install -d -o "$CTRL_USER" -g "$CTRL_USER" -m 0700 "$PRIVATE_DIR/bot-state"
  [[ -d "$DATA_DIR/bots" && ! -L "$DATA_DIR/bots" ]] || return 0
  for dir in "$DATA_DIR"/bots/*/; do
    dir="${dir%/}"
    id="${dir##*/}"
    [[ -d "$dir" && ! -L "$dir" ]] || continue
    for pattern in "${STATE_PATTERNS[@]}"; do
      while IFS= read -r -d '' file; do
        run install -d -o "$CTRL_USER" -g "$CTRL_USER" -m 0700 "$PRIVATE_DIR/bot-state/$id"
        if [[ -e "$PRIVATE_DIR/bot-state/$id/${file##*/}" ]]; then
          run rm -f "$file"  # the private copy is newer
        else
          run mv -T "$file" "$PRIVATE_DIR/bot-state/$id/${file##*/}"
          run chown -h "$CTRL_USER:$CTRL_USER" "$PRIVATE_DIR/bot-state/$id/${file##*/}"
        fi
      done < <(find -P "$dir" -mindepth 1 -maxdepth 1 -type f -name "$pattern" -print0)
    done
  done
}

do_rollback() {
  local old_data="$OLD_DATA" name
  if [[ -f "$MANIFEST" ]]; then
    old_data="$(sed -n 's/^old_data=//p' "$MANIFEST")"
  fi
  echo "== stop the isolated service (unit kept; disabled)"
  run systemctl disable --now bot-office.service || true
  echo "== remove the sudoers rule (the launcher stays, unusable without it)"
  run rm -f "$SUDOERS_FILE"
  run visudo -c
  echo "== shared tree and private files back to $old_data (owned by $BOTS_USER, 0700/0600)"
  run install -d -o "$BOTS_USER" -g "$BOTS_USER" -m 0700 "$old_data"
  if [[ -d "$DATA_DIR" ]]; then
    run rsync -a --exclude /voice/home "$DATA_DIR/" "$old_data/"
  fi
  for name in "${PRIVATE_FILES[@]}"; do
    if [[ -e "$PRIVATE_DIR/$name" ]]; then
      run cp -a "$PRIVATE_DIR/$name" "$old_data/$name"
      run chmod 0600 "$old_data/$name"
    fi
  done
  if [[ -d "$PRIVATE_DIR/bot-state" ]]; then
    # session pointers, handoff markers and snapshots back into each bot's dir
    local dir
    for dir in "$PRIVATE_DIR"/bot-state/*/; do
      [[ -d "$dir" ]] || continue
      dir="${dir%/}"
      run install -d -m 0700 "$old_data/bots/${dir##*/}"
      run cp -a "$dir/." "$old_data/bots/${dir##*/}/"
    done
  fi
  run chown -R "$BOTS_USER:$BOTS_USER" "$old_data"
  run find "$old_data" -type d -exec chmod 0700 {} +
  run setfacl -R -b "$old_data"
  echo "== the old user unit"
  user_systemctl enable --now bot-office.service
  run rm -f "$MANIFEST"
  echo "rolled back. Left in place: $STATE_DIR (private copies; backups), $APP_DIR, $LAUNCHER, user $CTRL_USER,"
  echo "group $SHARE_GROUP. Remove them by hand once the old layout works."
}

case "$mode" in
  dry-run)
    echo "DRY RUN: nothing is changed. Run with --apply (as root) to install."
    do_apply
    ;;
  apply)
    need_root
    do_apply
    ;;
  dry-rollback)
    echo "DRY RUN of --rollback: nothing is changed."
    do_rollback
    ;;
  rollback)
    need_root
    do_rollback
    ;;
esac
