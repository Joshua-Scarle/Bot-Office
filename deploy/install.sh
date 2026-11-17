#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
mkdir -p data/bots data/shared data/inboxes data/workspaces data/logs
chmod 700 data data/bots data/shared data/inboxes data/workspaces data/logs
chmod 755 bin/desk
if [[ ! -f data/token ]]; then
  openssl rand -hex 32 > data/token
fi
chmod 600 data/token

mkdir -p "$HOME/.config/systemd/user"
install -m 644 deploy/bot-office.service "$HOME/.config/systemd/user/bot-office.service"
systemctl --user daemon-reload
# Prefer the new unit; stop the pre-rename name if it is still enabled.
if systemctl --user is-enabled agentdesk.service >/dev/null 2>&1 || systemctl --user is-active agentdesk.service >/dev/null 2>&1; then
  systemctl --user disable --now agentdesk.service || true
fi
systemctl --user enable --now bot-office.service
ln -sfn "$project_dir" "$HOME/bot-office"
printf '%s\n' 'installed; API on 127.0.0.1:8787; token at data/token (0600); use SSH tunnel'
