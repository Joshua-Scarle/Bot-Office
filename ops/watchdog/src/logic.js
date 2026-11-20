// Pure watchdog logic (no Cloudflare imports) so it can be tested with `node --test`.

export const HEARTBEAT_MAX_AGE_MS = 15 * 60 * 1000; // VPS / controller considered down
export const BACKUP_MAX_AGE_HOURS = 30;
export const MIN_ALERT_GAP_MS = 10 * 60 * 1000; // between two mails
export const MAX_MAILS_PER_DAY = 6; // flapping guard, recovery mails included
const DAY_MS = 24 * 60 * 60 * 1000;
const BACKUP_STATES = ["ok", "failed", "not_configured", "unknown"];

/** Keep only the fields the watchdog uses; everything else in a push is dropped. */
export function sanitize(body) {
  if (!body || typeof body !== "object") return null;
  const h = body.healthz && typeof body.healthz === "object" ? body.healthz : null;
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const str = (v, n) => (typeof v === "string" ? v.slice(0, n) : null);
  const backup = h && h.backup && typeof h.backup === "object" ? h.backup : {};
  return {
    controller_reachable: body.controller_reachable === true && h !== null,
    sent_at: str(body.sent_at, 32),
    ok: h ? h.ok === true : false,
    version: h ? str(h.version, 32) : null,
    uptime_seconds: h ? num(h.uptime_seconds) : null,
    scheduler_alive: h && h.scheduler ? h.scheduler.alive === true : null,
    db_reachable: h ? h.db_reachable === true : null,
    running_jobs: h ? num(h.running_jobs) : null,
    disk_free_pct: h ? num(h.disk_free_pct) : null,
    backup_state: BACKUP_STATES.includes(backup.state) ? backup.state : "unknown",
    backup_age_hours: num(backup.age_hours),
  };
}

/** Problems for the newest heartbeat; empty list = healthy. `hb` is {received_at, data} or null. */
export function evaluate(hb, now, firstSeen) {
  const problems = [];
  const since = hb ? hb.received_at : firstSeen ?? now;
  if (now - since > HEARTBEAT_MAX_AGE_MS) {
    const mins = Math.round((now - since) / 60000);
    problems.push({
      kind: "heartbeat_missing",
      text: hb
        ? `No heartbeat for ${mins} min: the VPS, the network or the heartbeat timer is down.`
        : `No heartbeat has ever arrived (${mins} min since the watchdog started).`,
    });
    return problems; // stale data says nothing reliable about the rest
  }
  if (!hb) return problems;
  const d = hb.data;
  if (!d.controller_reachable) {
    problems.push({ kind: "controller_down", text: "The VPS is pushing, but the Bot Office controller does not answer /healthz." });
  } else if (!d.ok) {
    const why = [];
    if (d.scheduler_alive === false) why.push("scheduler stopped");
    if (d.db_reachable === false) why.push("database unreachable");
    if (d.disk_free_pct !== null && d.disk_free_pct < 5) why.push(`disk ${d.disk_free_pct}% free`);
    problems.push({ kind: "unhealthy", text: `healthz reports ok=false${why.length ? ": " + why.join(", ") : ""}.` });
  }
  if (d.backup_state !== "ok") {
    problems.push({ kind: "backup_state", text: `Backup state is "${d.backup_state}" (expected "ok").` });
  } else if (d.backup_age_hours === null || d.backup_age_hours > BACKUP_MAX_AGE_HOURS) {
    problems.push({
      kind: "backup_stale",
      text: d.backup_age_hours === null ? "Last backup time is unknown." : `Last good backup is ${d.backup_age_hours} h old (limit ${BACKUP_MAX_AGE_HOURS} h).`,
    });
  }
  return problems;
}

/**
 * One cron step. `state` is the stored incident record ({open, since, kinds, notified, mails: [ms]}).
 * Returns {state, send} where send is null or {type: "alert"|"recovery", problems, since}.
 * One alert per incident, one recovery mail; mails are rate-limited (gap + daily cap).
 */
export function step(state, problems, now) {
  const s = { open: false, since: null, kinds: [], notified: false, mails: [], ...(state || {}) };
  s.mails = s.mails.filter((t) => now - t < DAY_MS);
  const last = s.mails.length ? Math.max(...s.mails) : 0;
  let send = null;
  if (problems.length) {
    if (!s.open) {
      s.open = true;
      s.since = now;
      s.notified = false;
    }
    s.kinds = problems.map((p) => p.kind);
    if (!s.notified && s.mails.length < MAX_MAILS_PER_DAY && now - last >= MIN_ALERT_GAP_MS) {
      s.notified = true;
      s.mails.push(now);
      send = { type: "alert", problems, since: s.since };
    }
  } else if (s.open) {
    if (s.notified && s.mails.length < MAX_MAILS_PER_DAY) {
      s.mails.push(now);
      send = { type: "recovery", problems: [], since: s.since };
    }
    s.open = false;
    s.since = null;
    s.kinds = [];
    s.notified = false;
  }
  return { state: s, send };
}

export function composeMail(send, now) {
  const when = new Date(now).toISOString();
  if (send.type === "recovery") {
    const mins = Math.round((now - send.since) / 60000);
    return {
      subject: "[Bot Office] RECOVERED",
      text: `Bot Office is healthy again (${when}). The incident lasted about ${mins} min.\n\nThis is an automatic system notice from the Bot Office watchdog.`,
    };
  }
  return {
    subject: "[Bot Office] ALERT: " + send.problems.map((p) => p.kind).join(", "),
    text:
      `Bot Office needs attention (detected ${when}).\n\n` +
      send.problems.map((p) => "- " + p.text).join("\n") +
      "\n\nYou get one mail per incident and one when it recovers.\nThis is an automatic system notice from the Bot Office watchdog, not a bot message.",
  };
}

/** Plain-text RFC 5322 message; header values are stripped of CR/LF. */
export function buildRaw({ from, to, subject, text, messageId, now }) {
  const clean = (v) => String(v).replace(/[\r\n]+/g, " ");
  const b64 = btoa(String.fromCharCode(...new TextEncoder().encode(text))).replace(/.{76}/g, "$&\r\n");
  const encSubject = /^[\x20-\x7e]*$/.test(subject)
    ? clean(subject)
    : `=?UTF-8?B?${btoa(String.fromCharCode(...new TextEncoder().encode(clean(subject))))}?=`;
  return [
    `From: Bot Office Watchdog <${clean(from)}>`,
    `To: ${clean(to)}`,
    `Subject: ${encSubject}`,
    `Date: ${new Date(now).toUTCString()}`,
    `Message-ID: <${clean(messageId)}@${clean(from).split("@")[1] ?? "localhost"}>`,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=utf-8",
    "Content-Transfer-Encoding: base64",
    "",
    b64,
  ].join("\r\n");
}
