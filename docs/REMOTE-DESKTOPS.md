# Remote desktops (per-bot desktops, viewer, remote viewer)

Goal: every bot has its own desktop with a browser it can drive (`desk screenshot|click|type|key|open`),
and the owner can watch it and take over from the Bot-Office page, locally and (when remote access is
on) through Cloudflare Tunnel + Access.

## 1. What existed before this change (audit on the live host, 2026-11-26)

| Piece | State |
| --- | --- |
| Containers | `desktop-1`, `desktop-2` (the configured lease pool), `desktop-3` (another project's container, label `bot-office.owner`), `desktop-4`, `desktop-5` (unused by the app), two exited `*-old-ncache` containers. Image `localhost/light-desktop:v1` (Debian 12: Xvfb, openbox, x11vnc, xdotool, ImageMagick, Epiphany, Chromium, Firefox ESR). Each publishes `127.0.0.1:590N -> 5900`, x11vnc runs with `-nopw`. No caps dropped, no CPU limit, 3 GiB memory, `--restart always`. |
| Mapping bot -> desktop | None. `store.claim` leases the first free pool desktop to whichever job starts; with none free the job runs without one. In one day 25+ bots used `desktop-1`/`desktop-2`. A screenshot of `desktop-1` showed tabs left behind by other bots (a third-party login page among them): no isolation of browser state between bots. |
| `desk` | `screenshot`, `click`, `type`, `key`, `open URL` (Epiphany) through `podman exec` on the leased slot. Works: a screenshot of `desktop-1` taken during the audit. |
| Viewer | `GET /v1/desktops`, `POST /v1/desktops/{name}/ticket` (single use, 30 s, `view` or `control`), websocket `/v1/desktops/{name}/vnc` (same-origin, ticket in a subprotocol, view mode filters client input with `ViewFilter`), vendored noVNC, "Desktop" button in the page with "Take over" / "Hand back". Scripted check on localhost: ticket -> websocket -> `RFB 003.008` greeting, a replayed ticket is refused. |
| Remote access | Every websocket is refused through the tunnel (the Access JWT was only checked at the handshake). |
| Host | 6 vCPU, 11 GiB RAM (9 GiB available), no swap. An idle desktop uses ~30 MB, one with a browser 200-600 MB. Listeners on public interfaces: only sshd. |

## 2. Pool or per-bot

43 bots exist; most never touch a desktop. Running 43 desktops all the time is ~1.3 GB idle and
several GB once browsers are open, on a host without swap, so "always-on per bot" is out. A pool is
cheap but mixes browser sessions (logins, cookies, open tabs) between bots, which is the actual
problem.

Chosen: **per-bot desktops with persistent state, started on demand, stopped when idle, with a cap
on how many run at once.** The old lease pool stays as it is (jobs still get `BOT_OFFICE_SLOT`, and
`desk --slot desktop-1` still works for bots that rely on what is logged in there).

- A bot gets a desktop the first time it (or the owner) needs one: `desktop-N` with N from 10 to 99,
  so every existing piece keyed on the `desktop-N` name (port 5900+N, tickets, viewer) works as is.
  Numbers 1-9 stay reserved for hand-made/pool desktops and are never touched.
- The mapping bot -> N is stored in `<data dir>/desktops.json` and also as container labels
  (`bot-office.bot-id`, `bot-office.kind=per-bot`). A number whose container exists without our label
  for that bot is skipped, so a hand-made container is never adopted or replaced.
- Home directory (browser profile, cookies) on a named volume `bo-desktop-home-<bot id>`; the
  container itself is stopped, not removed, when idle. State survives idle stops, Bot-Office restarts
  and reboots.
- Created from `localhost/light-desktop:v1` (configurable), nothing new to download.
- At most `BOT_OFFICE_DESKTOP_MAX_RUNNING` (default 4) per-bot desktops run at once. When a fifth is
  needed, the least recently used idle one is stopped first; if none is idle the request fails with a
  clear message ("all 4 desktops are in use, try again later").
- Idle stop: a background task (every 60 s) stops a per-bot desktop that has had no `desk` use for
  `BOT_OFFICE_DESKTOP_IDLE_MINUTES` (default 20), no running turn of its bot, and no viewer connected.
  `0` disables idle stop. After a Bot-Office restart, running desktops count as used "now" (grace).
- Restart policy `no` (an idle-stopped desktop must stay stopped); "survives restarts" is by lazy
  start: the next `desk` call or the owner's "Open" starts it again with its state.
- Rollback: `BOT_OFFICE_DESKTOP_MODE=pool` makes `desk` inside a job use the leased pool desktop again
  (the old behaviour); the per-bot containers are left alone.

Resources per per-bot desktop: `--memory 1536m --cpus 1.5 --pids-limit 1024 --shm-size 256m`
(configurable). Worst case with the cap: 6 GiB, inside the 9 GiB available, leaving the app and the
CLIs room.

## 3. Agent tools

Inside a job, `desk` (no `--slot`) asks the controller for **its bot's own desktop** with the job's
agent token: `POST /v1/agent/desktop`. The controller checks the token, starts or creates the desktop,
waits until its X display is up, records the use, and answers `{"name": "desktop-N", ...}`. `desk`
then runs the command with `podman exec` as before. A bot can only ever get its own desktop (the bot
comes from the token, never from an argument). `--slot <leased pool desktop>` still works.

`desk open URL` (http/https only) opens the URL in Epiphany on the bot's desktop. New:
`desk open` waits briefly for the window so a following `desk screenshot` shows the page.

If the owner is controlling that desktop at that moment, the answer says so and `desk click/type/key`
refuse with "the owner is controlling this desktop; wait for the hand-back" (screenshot and open still
work), so the bot and the owner do not fight over the mouse.

Approvals: `desk click/type/key` stay classed as they are in `approval_classes.py` (unchanged here).

## 4. Owner UI

- "Desktop" panel lists every desktop: per-bot ones as "<bot name> (desktop-N, running/stopped)",
  pool ones as before (with the bot of the job that has it leased).
- Connect is **view only** by default. "Take control" reconnects with a control ticket; a red banner
  "You are controlling <bot>'s desktop" stays visible while in control; "Release" goes back to view.
  "Hand back" (as before) tells the bot that you are done; for a per-bot desktop it goes to that bot.
- A stopped per-bot desktop is started by "Connect" (owner API `POST /v1/desktops/{name}/start`).
- Each bot's own desktop can also be opened from the bot's chat ("Desktop" opens with that bot's
  desktop preselected).
- Phone width: the toolbar wraps, the screen scales to fit (`scaleViewport`), the screen height follows
  the viewport.

## 5. Remote viewer over Cloudflare Access

Remote access stays off by default; nothing in this change turns it on. When it is on, the
middleware allows exactly one kind of remote websocket:

1. Path is `/v1/desktops/desktop-N/vnc` (anchored regex). Every other remote websocket is refused (1008).
2. The identity is a **person** (`kind == "user"`) with role `owner`, verified from the Access JWT
   (`via == "jwt"`): RS256 signature against the team's keys, issuer, audience, `exp`/`nbf`/`iat`,
   the email in `owners`. Service tokens (`remote-client`) and shared keys are refused.
3. `Origin` must be exactly `https://<host>` (no cross-site websocket riding on the Access cookie).
4. The ticket is **bound to the identity**: a ticket issued to a remote owner records
   `(remote, email, host)`; a local one records `(local)`. `consume` needs the same binding, the
   same desktop, within 30 s, once. A ticket obtained locally cannot be used remotely or vice versa.
   The ticket POST itself goes through the normal owner checks (JWT + Origin for a POST).
5. While the socket is open the middleware re-checks every 15 s that the identity is still allowed
   (config still on, host still listed, email still an owner, team/audience unchanged) and closes the
   socket (code 1008) at the JWT's `exp`, on revocation, or at a hard maximum of 1 hour, whichever
   comes first. Closing means: the client gets a close frame, the app's receive side gets a
   disconnect, and the app task is cancelled; nothing more is forwarded in either direction.
   Residual: ASGI has no way to abort a websocket transport, so a frame the app had already handed to
   the server before the cut may still be flushed to the client. Such a frame was produced while the
   credential was valid (every frame is checked before it is sent), so this equals it arriving a few
   milliseconds earlier; no frame produced after the cut is forwarded, and no input is accepted after it.
6. The viewer reconnects with a fresh ticket (and so a fresh JWT from Cloudflare) when the owner
   presses Connect again.

Remote-client tokens remain unable to reach any desktop route (`/v1/desktops*` is not in their
allowlist) or any websocket.

## 6. Security posture

- Listeners: Bot-Office on 127.0.0.1:8787 only (unchanged). Per-bot desktops publish
  `127.0.0.1:59NN -> 5900` only; nothing on `0.0.0.0`/`::`. Checked with `ss -ltnp`.
- VNC password: none, as for the existing desktops. Reason: the only clients that can reach
  `127.0.0.1:59NN` are local processes running as the same user as podman, and those can already
  `podman exec` into the container, so a VNC password adds no protection on this host. The browser
  never reaches x11vnc directly: only through the authenticated, ticketed websocket proxy. If the
  host ever gains other local users, move to a random per-desktop password (stored in the data dir,
  0600, never logged) with the proxy doing VNC auth.
- Containers created by Bot-Office: not privileged, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, private network namespace (rootless `slirp4netns`, never host
  network), memory/CPU/pids/shm limits, `--init` (clean stop), labels for ownership. Xvfb runs with
  `-nolisten tcp`.
- Existing containers and images are not modified, restarted, or deleted.

## 7. Configuration (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOT_OFFICE_DESKTOP_MODE` | `per-bot` | `per-bot` or `pool` (what `desk` uses inside a job without `--slot`) |
| `BOT_OFFICE_DESKTOP_IMAGE` | `localhost/light-desktop:v1` | image for new per-bot desktops |
| `BOT_OFFICE_DESKTOP_MAX_RUNNING` | `4` | per-bot desktops running at once |
| `BOT_OFFICE_DESKTOP_IDLE_MINUTES` | `20` | idle stop (0 = never) |
| `BOT_OFFICE_DESKTOP_MEMORY` | `1536m` | per container |
| `BOT_OFFICE_DESKTOP_CPUS` | `1.5` | per container |
| `BOT_OFFICE_DESKTOP_GEOMETRY` | `1280x800` | screen size |

## 8. Tests

pytest (`PYTHONPATH=src`): ticket identity binding, remote websocket allowed only for owner + closed at
expiry/revocation/max age, remote-client and shared-key websockets still refused, other remote
websocket paths refused, bot -> desktop mapping (stable, skips foreign containers, range), the
podman command line (limits, caps, loopback publish), idle shutdown decisions and LRU at the cap,
`desk` resolving the bot's own desktop through the agent API, `desk open`.
