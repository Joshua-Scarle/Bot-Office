# Rename plan for the live Bot-Office install

This plan is for the code that already runs on the Contabo VPS at `~/printpilot-agentdesk`. **It has not been applied.** The live server was left alone so a running service would not break. There is no GitHub remote; when this is applied, do it on a local git branch on that host, review the diff, run tests, then restart the service in a maintenance window.

Display name: **Bot-Office**. Slug: **bot-office**. Use **botoffice** only where a hyphen is not allowed (Python package name, some env vars).

## What would need a restart

After the rename is applied on the host:

1. The systemd user service `agentdesk.service` (or its replacement, `bot-office.service` / `botoffice.service`) must be reloaded and restarted: `systemctl --user daemon-reload` and `systemctl --user restart …`.
2. Confirm with `ss` that the API still listens only on `127.0.0.1:8787` (or the new port, if one is chosen) and that SSH remains the only public port.
3. Any open SSH tunnels to that port should be reopened.
4. Browser tabs still holding `crewdeck-token` or `agentdesk-token` in `sessionStorage` should be recached against the new key (accept the old keys once, then prefer `bot-office-token`).
5. Podman workers `desktop-1` and `desktop-2` should not need a rebuild unless image or unit names change; check they are still Up after the controller restart.

Do not restart these as part of preparing the rename. Do it only when the branch is ready to go live.

## Identifiers to change (current → new)

Taken from the 0.2.x snapshots on the box and from the 13 November 2026 Crewdeck UI capture. Confirm against the live tree before editing; that tree is ahead of the snapshots (Crewdeck UI overhaul, streaming, effort selector).

| Kind | Current | New |
|---|---|---|
| Visible product name (HTML title, headings, README) | Crewdeck (earlier: PrintPilot Agent Desk) | Bot-Office |
| Python package / `python -m` | `agentdesk` | `botoffice` (hyphen not allowed) |
| Project dir (optional; can wait) | `~/printpilot-agentdesk` | `~/bot-office` |
| systemd unit | `agentdesk.service` | `bot-office.service` (or `botoffice.service` if the unit name cannot include a hyphen in a given tool) |
| Env vars | `AGENTDESK_TOKEN`, `AGENTDESK_TOKEN_FILE`, `AGENTDESK_DATA_DIR`, `AGENTDESK_PROVIDERS`, `AGENTDESK_SLOT` | `BOT_OFFICE_TOKEN`, `BOT_OFFICE_TOKEN_FILE`, `BOT_OFFICE_DATA_DIR`, `BOT_OFFICE_PROVIDERS`, `BOT_OFFICE_SLOT`, accepting the old names during a migration |
| sessionStorage token key | `crewdeck-token` (fallback `agentdesk-token`) | `bot-office-token`, still accepting the older keys |
| sessionStorage selected bot | `crewdeck-bot` | `bot-office-bot` |
| pyproject description | "PrintPilot-owned local job controller…" | A brand-free description, for example "Self-hosted workspace for persistent, provider-agnostic agent employees" |
| CLI helper | `bin/desk` can stay; it is a verb, not the product name | Optional: keep `desk` for compatibility |

Strip PrintPilot Pathfinder branding from the UI, README, systemd Description, and package metadata. The company may publish the repo, but this is not a company product.

Add a short "formerly Crewdeck, formerly Agent Desk" line to the README. Leave git history, dated memos, and old log lines as they are.

## Tests and install

Update tests that assert the string "Crewdeck" or "Agent Desk" in `GET /` HTML, token key names, and module import paths. Run `.venv/bin/pytest` on the host before restarting the service. Do not bind the API to anything other than `127.0.0.1`. Do not print or log tokens.

## What this plan does not do

- It does not rename a GitHub repository (none exists for this project).
- It does not change DNS, the Contabo hostname, or SSH keys.
- It does not migrate Hermes Agent; the running code is not a Hermes fork.
