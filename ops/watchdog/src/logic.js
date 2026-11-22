// Pure watchdog logic (no Cloudflare imports) so it can be tested with `node --test`.

export const HEARTBEAT_MAX_AGE_MS = 15 * 60 * 1000; // VPS / controller considered down
export const BACKUP_MAX_AGE_HOURS = 30;
export const CLASSES = ["needs_you", "failed", "outage", "backup", "done"];
export const DEFAULT_PREFS = { needs_you: true, failed: true, outage: true, backup: true, done: false };
const KIND_CLASS = { heartbeat_missing: "outage", controller_down: "outage", unhealthy: "outage", backup_state: "backup", backup_stale: "backup", backup_unverified: "backup" };
export const classOf = (kind) => KIND_CLASS[kind];
const BACKUP_STATES = ["ok", "failed", "not_configured", "unknown", "unjudged"];

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
    // a healthz that carries no backup object at all (the controller's bounded answer was a timeout) cannot
    // vouch for or against the backup: unjudged, never "unknown"
    backup_state: h && !h.backup ? "unjudged" : BACKUP_STATES.includes(backup.state) ? backup.state : "unknown",
    backup_age_hours: num(backup.age_hours),
  };
}

/** Classes `evaluate` could not judge from this problem list (their open incidents stay as they are). */
export function unjudged(problems, hb) {
  if (problems.some((p) => p.kind === "heartbeat_missing" || p.kind === "controller_down")) return ["backup"];
  return hb && hb.data && hb.data.backup_state === "unjudged" ? ["backup"] : [];
}

const PLAIN_ADDRESS = /^[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$/;
const norm = (v) => (typeof v === "string" ? v.trim().toLowerCase() : "");

/** The one address this Worker may ever mail, read from config (`ALLOWED_RECIPIENT`, set in the local,
 *  uncommitted wrangler.toml or as a Worker secret). Exactly one plain address, or null: no lists, no
 *  default. A Worker without it refuses to run, so a missing value fails closed and never mails anyone. */
export function allowedRecipient(env) {
  const a = norm(env && env.ALLOWED_RECIPIENT);
  return PLAIN_ADDRESS.test(a) ? a : null;
}

/** True only when `to` is exactly the allowed recipient (case and surrounding spaces aside). */
export function isAllowedRecipient(to, allowed) {
  return Boolean(allowed) && norm(to) === allowed;
}

/** Config problems as a short fixed string, or null when the Worker may run: ALLOWED_RECIPIENT must be one
 *  plain address, ALERT_TO exactly that address (and the send_email binding present), ALERT_FROM a plain
 *  address. Never echoes a value. */
export function configError(env) {
  const allowed = allowedRecipient(env);
  if (!allowed) return "ALLOWED_RECIPIENT is not one plain address";
  if (!isAllowedRecipient(env.ALERT_TO, allowed)) return "ALERT_TO is not the allowed recipient";
  if (typeof env.ALERT_FROM !== "string" || !PLAIN_ADDRESS.test(env.ALERT_FROM)) return "ALERT_FROM is not a plain address";
  return null;
}

const CODE = /^[a-z][a-z_]{0,31}$/, BOT = /^bot-[0-9a-f]{6}$/, REF = /^[A-Za-z0-9_-]{1,40}$/, KEY = /^[A-Za-z0-9:_.-]{1,80}$/;
export const EVENT_CLASSES = ["needs_you", "failed", "done"];
export const MAX_BATCH = 100;

/** The controller's event batch, reduced to what the mail may carry: hashed ids, a pseudonymous bot label ("bot-" + 6 hex, nothing else is accepted) and fixed codes.
 *  No free text field exists any more; a value that does not match its strict pattern becomes a neutral
 *  placeholder ("other", "a bot"), so nothing a user typed can reach the mail. */
export function sanitizeEvents(body) {
  if (!body || typeof body !== "object") return null;
  const prefs = {};
  for (const c of CLASSES) prefs[c] = body.prefs && typeof body.prefs[c] === "boolean" ? body.prefs[c] : DEFAULT_PREFS[c];
  const int = (v) => (Number.isSafeInteger(v) && v >= 0 ? v : 0);
  const out = {
    prefs,
    backup_reason: typeof body.backup_reason === "string" && CODE.test(body.backup_reason) ? body.backup_reason : null,
    epoch: typeof body.epoch === "string" && /^[A-Za-z0-9]{1,32}$/.test(body.epoch) ? body.epoch : "none",
    upto: int(body.upto),
    events: [],
  };
  const list = Array.isArray(body.events) ? body.events.slice(0, MAX_BATCH) : [];
  for (const e of list) {
    if (!e || !Number.isSafeInteger(e.seq) || e.seq < 1 || typeof e.id !== "string" || !KEY.test(e.id) || !EVENT_CLASSES.includes(e.cls)) continue;
    out.events.push({
      seq: e.seq, id: e.id, cls: e.cls,
      bot: typeof e.bot === "string" && BOT.test(e.bot) ? e.bot : "a bot",
      job: typeof e.job === "string" && REF.test(e.job) ? e.job : "",
      kind: typeof e.kind === "string" && CODE.test(e.kind) ? e.kind : "other",
    });
  }
  out.events.sort((a, b) => a.seq - b.seq);
  out.upto = Math.max(out.upto, out.events.length ? out.events[out.events.length - 1].seq : 0);
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
  if (d.backup_state === "unjudged") return problems; // the controller could not read it in time: no verdict
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
