// The watchdog's coordinated state machine. It runs inside ONE Durable Object, so all state lives in
// strongly consistent transactional storage and every operation is serialised by `#exclusive`.
// No Cloudflare imports: storage and mail sending are injected, so `node --test` can drive it.
//
// Delivery model (nothing is ever dropped silently):
//   1. A notification is first written to storage as pending (a problem opened, a new event seen).
//   2. When a class may send (gap since its last mail), its pending entries become one OUTBOX message with
//      a fixed id, and that is written to storage BEFORE the mail is sent (write-ahead).
//   3. The outbox entry is removed only after a send succeeded. A failed send stays queued and is retried
//      on the next run with the same Message-ID; a failed write after a successful send repeats that one
//      mail with the same Message-ID (at-least-once, never lost).
//   Rate limiting only delays: pending entries are merged into one digest. An incident that recovers
//   before its mail went out is still mailed, marked as already recovered.
import { CLASSES, DEFAULT_PREFS, evaluate, unjudged, classOf, buildRaw } from "./logic.js";

export const MIN_GAP_MS = 10 * 60 * 1000; // between two mails of the same class
export const SLOW_GAP_MS = 60 * 60 * 1000; // the gap once SOFT_CAP mails went out in 24 h (a flapping guard)
export const SOFT_CAP = 12;
export const SEEN_GRACE_MS = 24 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_EVENTS_PER_CLASS = 200;

const fresh = () => ({ hb: null, first_seen: null, prefs: { ...DEFAULT_PREFS }, incidents: {}, seen: {}, pending: [], outbox: {}, last_sent: {}, sent_log: [], counts: {}, backup_reason: null });

const SUBJECT = {
  needs_you: (n) => (n === 1 ? "1 item needs you" : `${n} items need you`),
  failed: (n) => (n === 1 ? "a turn failed" : `${n} turns failed`),
  outage: () => "ALERT: server or controller",
  backup: () => "ALERT: backup",
  done: (n) => (n === 1 ? "1 turn finished" : `${n} turns finished`),
};
const FOOTER = "\n\nThis is an automatic notice from the Bot Office watchdog, not a bot message. Each item is mailed once; mails of one kind are at least 10 minutes apart and are merged into a digest.";

function line(e, now) {
  if (e.type === "more") return `- and ${e.n} more`;
  if (e.type === "recovery") return `- RECOVERED: ${e.text}`;
  if (e.type === "alert") {
    const gone = e.resolved_at ? ` (already recovered after about ${Math.max(1, Math.round((e.resolved_at - e.at) / 60000))} min)` : "";
    return `- ${e.text}${gone}`;
  }
  const it = e.item;
  return `- ${it.bot || "a bot"}: ${it.kind}${it.job ? ` (job ${it.job})` : ""}${it.reason ? `: ${it.reason}` : ""}`;
}

/** The mail for one class: {subject, text}. */
export function compose(cls, entries, counts, now) {
  const alerts = entries.filter((e) => e.type === "alert" || e.type === "recovery").length;
  const n = entries.length;
  const subject = "[Bot Office] " + (cls === "outage" || cls === "backup" ? SUBJECT[cls]() + (n > 1 ? ` (${n} updates)` : "") : SUBJECT[cls](n));
  const shown = entries.slice(0, 40).map((e) => line(e, now));
  const more = n > shown.length ? [`- and ${n - shown.length} more`] : [];
  const total = counts[cls] > n && alerts === 0 ? [`\n${counts[cls]} are open in total.`] : [];
  const head = cls === "needs_you" ? "New items need you" : cls === "failed" ? "Failed turns" : cls === "done" ? "Finished turns" : "Bot Office needs attention";
  return { subject, text: `${head} (${new Date(now).toISOString()}):\n\n${[...shown, ...more].join("\n")}${total.join("")}${FOOTER}` };
}

export class Engine {
  #chain = Promise.resolve();
  constructor({ storage, send, now = () => Date.now(), newId = () => crypto.randomUUID(), from = "", to = "" }) {
    Object.assign(this, { storage, send, clock: now, newId, from, to });
  }

  /** One operation at a time, each reading state afresh from storage. */
  #exclusive(fn) {
    const run = this.#chain.then(fn);
    this.#chain = run.catch(() => {});
    return run;
  }

  async #load() {
    return { ...fresh(), ...((await this.storage.get("s")) || {}) };
  }
  async #save(s) {
    await this.storage.put("s", s);
  }

  heartbeat(data) {
    return this.#exclusive(async () => {
      const s = await this.#load(), now = this.clock();
      s.first_seen ??= now;
      s.hb = { received_at: now, data };
      await this.#save(s);
    });
  }

  events(ev) {
    return this.#exclusive(async () => {
      const s = await this.#load(), now = this.clock();
      s.first_seen ??= now;
      s.prefs = ev.prefs;
      s.backup_reason = ev.backup_reason;
      for (const [cls, list] of Object.entries(ev.lists)) {
        s.counts[cls] = list.count;
        for (const item of list.items) {
          const known = s.seen[item.id];
          s.seen[item.id] = { cls, last_seen: now };
          // a disabled class is remembered (so turning it on later does not replay old items) but not queued
          if (!known && s.prefs[cls]) s.pending.push({ id: item.id, cls, type: "event", item, at: now });
        }
      }
      for (const [id, v] of Object.entries(s.seen)) if (now - v.last_seen > SEEN_GRACE_MS) delete s.seen[id];
      this.#bound(s);
      await this.#save(s);
      await this.#drain(s);
    });
  }

  tick() {
    return this.#exclusive(async () => {
      const s = await this.#load(), now = this.clock();
      s.first_seen ??= now;
      const problems = evaluate(s.hb, now, s.first_seen);
      this.#reconcile(s, problems, now);
      this.#bound(s);
      await this.#save(s);
      await this.#drain(s);
    });
  }

  #bound(s) {
    // storage stays small even if mail cannot go out for days: past MAX_EVENTS_PER_CLASS pending events,
    // the oldest are folded into one "and N more" entry, so they are still counted, never hidden
    for (const cls of ["needs_you", "failed", "done"]) {
      const mine = s.pending.filter((e) => e.cls === cls && e.type === "event");
      if (mine.length <= MAX_EVENTS_PER_CLASS) continue;
      const fold = mine.slice(0, mine.length - MAX_EVENTS_PER_CLASS);
      const gone = new Set(fold);
      const old = s.pending.find((e) => e.cls === cls && e.type === "more");
      const n = fold.length + (old ? old.n : 0);
      s.pending = s.pending.filter((e) => !gone.has(e) && e !== old);
      s.pending.push({ id: `more:${cls}`, cls, type: "more", n, at: fold[0].at });
    }
  }

  #reconcile(s, problems, now) {
    const skip = new Set(unjudged(problems));
    const current = new Map(problems.map((p) => [p.kind, p]));
    for (const p of problems) {
      if (s.incidents[p.kind]) continue;
      const cls = classOf(p.kind);
      const muted = !s.prefs[cls];
      const text = p.text + (cls === "backup" && s.backup_reason ? ` Reason: ${s.backup_reason}` : "");
      s.incidents[p.kind] = { cls, since: now, muted, text };
      if (!muted) s.pending.push({ id: `inc:${p.kind}:${now}`, cls, type: "alert", key: p.kind, text, at: now });
    }
    for (const [kind, inc] of Object.entries(s.incidents)) {
      if (current.has(kind) || skip.has(inc.cls)) continue;
      delete s.incidents[kind];
      if (inc.muted || !s.prefs[inc.cls]) continue;
      const open = s.pending.find((e) => e.type === "alert" && e.key === kind && !e.resolved_at);
      if (open) open.resolved_at = now; // not mailed yet: the one mail says it is already over
      else s.pending.push({ id: `rec:${kind}:${now}`, cls: inc.cls, type: "recovery", key: kind, text: inc.text.split(" Reason:")[0].replace(/\.$/, "") + ` (lasted about ${Math.max(1, Math.round((now - inc.since) / 60000))} min)`, at: now });
    }
  }

  async #drain(s) {
    const now = this.clock();
    s.sent_log = s.sent_log.filter((t) => now - t < DAY_MS);
    const gap = s.sent_log.length >= SOFT_CAP ? SLOW_GAP_MS : MIN_GAP_MS;
    for (const cls of CLASSES) {
      let box = s.outbox[cls];
      if (!box) {
        const mine = s.pending.filter((e) => e.cls === cls);
        if (!mine.length || now - (s.last_sent[cls] || 0) < gap) continue; // held, kept pending, merged later
        box = s.outbox[cls] = { msg_id: this.newId(), entries: mine, attempts: 0 };
        s.pending = s.pending.filter((e) => e.cls !== cls);
        await this.#save(s); // write-ahead: if this fails nothing was sent and nothing is lost
      }
      const { subject, text } = compose(cls, box.entries, s.counts, now);
      try {
        await this.send({ messageId: box.msg_id, subject, text, now });
      } catch (err) {
        box.attempts += 1;
        console.log("mail failed:", cls, err && err.name); // class only; stays queued for the next run
        await this.#save(s).catch(() => {});
        continue;
      }
      delete s.outbox[cls];
      s.last_sent[cls] = now;
      s.sent_log.push(now);
      await this.#save(s); // if this throws, the next run resends the same message id
    }
  }
}

export function makeSender(env, EmailMessage) {
  return async ({ messageId, subject, text, now }) => {
    if (!env.ALERT_TO || !env.ALERT_FROM) throw new Error("ALERT_TO/ALERT_FROM not set");
    const raw = buildRaw({ from: env.ALERT_FROM, to: env.ALERT_TO, subject, text, messageId, now });
    await env.ALERT_EMAIL.send(new EmailMessage(env.ALERT_FROM, env.ALERT_TO, raw));
  };
}
