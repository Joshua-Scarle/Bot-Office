# Remote access (Cloudflare Tunnel + Cloudflare Access)

This lets you open Bot-Office from a phone browser, and lets a Claude Code cloud session call its API, without opening an inbound port and without exposing the controller to the internet.

All names below (`office.example.test`, `you@example.test`, `yourteam.cloudflareaccess.com`) are placeholders. Use your own.

## Why this design

- **No inbound port.** Bot-Office still listens on `127.0.0.1:8787` only. `cloudflared` on the host connects outbound to Cloudflare and forwards requests to that loopback address.
- **Access in front.** Cloudflare Access (Zero Trust, free plan) asks for a login (or a service token) before a request reaches the tunnel, and adds a signed JSON Web Token (JWT) in the `Cf-Access-Jwt-Assertion` header.
- **Checked again by Bot-Office.** Bot-Office does not trust the tunnel or the headers. For every request that arrived through it, it verifies the JWT itself (RS256 signature against Cloudflare's published keys, issuer, audience, expiry), checks the Host, and maps the identity to a role. Anything that fails is refused, and a broken or missing configuration also refuses (fail closed).
- **Local use is unchanged.** A request is treated as remote when its Host is one of your public hostnames or it carries any Cloudflare header (`Cf-Ray`, `Cf-Connecting-Ip`, `Cf-Access-*`, `Cf-Visitor`). Everything else (the SSH tunnel, bots on loopback) works as before with the bearer token.

## Roles

| Role | Who | What it can do |
| --- | --- | --- |
| `owner` | A person whose email is listed in `owners` and who passed the Access login | The whole GUI and API, with no bearer token. State-changing requests must carry an `Origin` header equal to `https://<host>` (blocks cross-site requests that ride on the browser's cookie). |
| `remote-client` | A service token listed in `clients` | Only the routes in the allowlist in `src/botoffice/remote_access.py`: read bots, chat history, jobs, needs-you and assignments; post a chat message; answer a question card; cancel a job; start, stop or reply to an assignment. No settings, no command execution, no bot create or delete, no memory, rules, watchers, uploads, voice, desktops or `/v1/agent/*`. No websockets. |

A job-scoped agent token is a separate thing and is never satisfied by a remote identity.

## Threat notes

- **The owner role through Access equals full Bot-Office control**, including settings and bot configuration. Anyone who can log in as the owner email controls the host's bots. Use a strong login for that email: a hardware key or passkey on the identity provider, and a short Access session duration.
- The `remote-client` role is least privilege for the API, but it can still send chat messages to bots, and bots with full CLI access can run commands on the host. So it limits accidents and direct admin changes; it is not a wall against someone who steals the token. Treat the service token like a password for the whole Bot-Office.
- The service token secret is shown once by Cloudflare. Keep it out of git, chat and logs.
- If you run `cloudflared` as a service, anyone with the host account can read its credentials file. Keep it mode `0600`.
- Bot-Office refuses a remote request when the file is missing or disabled, so a typo in the configuration never opens access.

## Setup (owner, in the Cloudflare dashboard)

1. **Zero Trust.** Create a free Zero Trust account and note your team domain (`yourteam.cloudflareaccess.com`).
2. **Tunnel.** In Networks, Tunnels, create a tunnel and install its connector on the Bot-Office host (the dashboard shows the command). Keep the credentials file on the host, mode `0600`. `deploy/remote-access/cloudflared-config.example.yml` shows the equivalent file-based config.
3. **Public hostname.** Add a public hostname `office.example.test` that points to `http://localhost:8787`.
4. **Access application.** Add a self-hosted application for `office.example.test` with two policies:
   - Allow, include **Emails** = your owner email.
   - Service Auth, include your service token (create it under Access, Service Auth; name it for the client, for example `claude-cloud`).
5. **Copy two values.** The application's **Audience (AUD) tag** and your team domain.
6. **Bot-Office configuration.** On the host, copy `deploy/remote-access/remote_access.example.json` to `<data dir>/remote_access.json` (the controller's private directory when one is set), or point `BOT_OFFICE_REMOTE_ACCESS_FILE` at it. Fill in the team domain, the AUD tag, the hostname, your email under `owners`, and the service token's Client ID (ending in `.access`) under `clients`. Restrict it with `chmod 600`.
7. **Check.** Run `deploy/remote-access/check.sh`. The file is re-read within seconds of a change, so no restart is needed.

## How a Claude Code cloud environment uses the service token

In the cloud environment's settings, add network secrets for your hostname so the sandbox's proxy adds the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers to requests for that host. The session then calls `https://office.example.test/v1/bots` and never sees the secret. Cloudflare turns those headers into the JWT, and Bot-Office checks it and applies the `remote-client` allowlist.

## Revoking access

- **One client:** delete or rotate the service token in Cloudflare, and remove its entry from `remote_access.json` (effective within seconds, no restart).
- **One person:** remove the email from `owners` and from the Access policy, and end their Access session.
- **Everything:** stop the `cloudflared` service (the kill switch), or set `"enabled": false`. Local access over SSH is not affected.

## Live end-to-end checklist

- [ ] The phone loads the GUI at `https://office.example.test` after the Access login, with no token prompt and the note "Signed in through Cloudflare Access".
- [ ] A request to the hostname without an Access login is blocked at Cloudflare (login page or 403), not by Bot-Office.
- [ ] On the host: `curl -H 'Cf-Ray: x' http://127.0.0.1:8787/v1/bots` (a forged Cloudflare header, no JWT) returns 403.
- [ ] On the host: `curl http://127.0.0.1:8787/healthz` and a bearer-token call still work as before.
- [ ] With the service token: `GET /v1/bots` works and a chat message can be posted; `GET /v1/settings` and `POST /v1/slots/<slot>/exec` return 403.
- [ ] After rotating or deleting the service token, the same call is blocked.
- [ ] `deploy/remote-access/check.sh` reports loopback only.
