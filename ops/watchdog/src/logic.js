// Pure watchdog logic (no Cloudflare imports) so it can be tested with `node --test`.

export const HEARTBEAT_MAX_AGE_MS = 15 * 60 * 1000; // VPS / controller considered down
export const BACKUP_MAX_AGE_HOURS = 30;
export const CLASSES = ["needs_you", "failed", "outage", "backup", "done"];
export const DEFAULT_PREFS = { needs_you: true, failed: true, outage: true, backup: true, done: false };
const KIND_CLASS = { heartbeat_missing: "outage", controller_down: "outage", unhealthy: "outage", backup_state: "backup", backup_stale: "backup" };
export const classOf = (kind) => KIND_CLASS[kind];
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

/** Classes `evaluate` could not judge from this problem list (their open incidents stay as they are). */
export function unjudged(problems) {
  return problems.some((p) => p.kind === "heartbeat_missing" || p.kind === "controller_down") ? ["backup"] : [];
}

const clean = (v, n) => (typeof v === "string" ? v.replace(/[\u0000-\u001f\u007f\s]+/g, " ").trim().slice(0, n) : "");

/** The controller's event summary, reduced to what the mail may carry. Anything else is dropped. */
export function sanitizeEvents(body) {
  if (!body || typeof body !== "object") return null;
  const prefs = {};
  for (const c of CLASSES) prefs[c] = body.prefs && typeof body.prefs[c] === "boolean" ? body.prefs[c] : DEFAULT_PREFS[c];
  const out = { prefs, backup_reason: clean(body.backup_reason, 120) || null, lists: {} };
  for (const c of ["needs_you", "failed", "done"]) {
    const list = body[c];
    if (!list || typeof list !== "object" || !Array.isArray(list.items)) continue;
    const items = [];
    for (const it of list.items.slice(0, 50)) {
      if (!it || typeof it.id !== "string" || !/^[A-Za-z0-9:_.-]{1,80}$/.test(it.id)) continue;
      items.push({ id: it.id, bot: clean(it.bot, 60), job: clean(it.job, 40), kind: clean(it.kind, 40), reason: clean(it.reason, 120) });
    }
    const count = Number.isInteger(list.count) && list.count >= items.length ? Math.min(list.count, 1e6) : items.length;
    out.lists[c] = { count, items };
  }
  return out;
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
  if (!d.controller_reachable) return problems; // no healthz, so nothing known about the backup
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
