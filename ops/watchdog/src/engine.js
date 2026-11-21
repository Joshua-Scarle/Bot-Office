// The watchdog's coordinated state machine. It runs inside ONE Durable Object, so all state lives in
// strongly consistent transactional storage and every state change is serialised by `#exclusive`.
// No Cloudflare imports: storage and mail sending are injected, so `node --test` can drive it.
//
// Intake: the controller sends its durable event log in batches, in order, each with a sequence number.
// `events()` takes events while there is room (MAX_PENDING per class) and answers {ack: cursor}: the
// controller removes from its queue only up to that cursor, so what does not fit simply stays queued there
// and goes in a later push. Nothing is sampled, truncated or folded away; counts in a digest are exact.
//
// Delivery (sending never happens inside a request, so a slow mail server cannot delay heartbeats):
//   1. State changes (events, heartbeat evaluation) only write storage and set the alarm.
//   2. The alarm runs `drain()`. A class whose gap since its last mail has passed turns its pending entries
//      into ONE outbox message: the complete RFC 5322 text (body, Date, Message-ID) is composed once and
//      saved BEFORE the first send, and exactly that text is re-sent on every retry.
//   3. A send is bounded (SEND_TIMEOUT_MS) and runs outside the state queue. Success removes the outbox
//      entry; failure keeps it and retries with exponential backoff (BACKOFF_BASE_MS doubling, 1 h cap).
//
// At-least-once boundary: if a send times out or the process dies after the mail server accepted it but
// before the outbox entry was removed, the identical message (same Message-ID and Date) is sent again.
// The mail system cannot tell us whether the first one arrived, so a rare duplicate is the price of never
// losing a message; mail clients may or may not fold the two. Exactly-once is not possible here.
//
// A digest is never truncated: every entry is listed in some mail. More than MAX_LINES entries of one
// class are split into several mails ("part i of k"), queued together in that class's outbox and sent in
// order, each with its exact count in the subject.
//
// Rate limiting only delays: pending entries merge into one digest. An incident that recovers before its
// mail went out is still mailed, marked as already recovered.
import { CLASSES, DEFAULT_PREFS, RECIPIENT, evaluate, unjudged, classOf, buildRaw } from "./logic.js";

export const MIN_GAP_MS = 10 * 60 * 1000; // between two mails of the same class
export const SLOW_GAP_MS = 60 * 60 * 1000; // the gap once SOFT_CAP mails went out in 24 h (a flapping guard)
export const SOFT_CAP = 12;
export const MAX_PENDING = 100; // events per class held at once; beyond it the controller keeps them queued
export const SEND_TIMEOUT_MS = 8000;
export const BACKOFF_BASE_MS = 30 * 1000;
export const BACKOFF_MAX_MS = 60 * 60 * 1000;
export const MAX_LINES = 40;
const DAY_MS = 24 * 60 * 60 * 1000;

const fresh = () => ({ hb: null, first_seen: null, prefs: { ...DEFAULT_PREFS }, incidents: {}, pending: [], outbox: {}, last_sent: {}, sent_log: [], epoch: null, cursor: 0, backup_reason: null });

const SUBJECT = {
  needs_you: (n) => (n === 1 ? "1 item needs you" : `${n} items need you`),
  failed: (n) => (n === 1 ? "a turn failed" : `${n} turns failed`),
  outage: () => "ALERT: server or controller",
  backup: () => "ALERT: backup",
  done: (n) => (n === 1 ? "1 turn finished" : `${n} turns finished`),
};
const FOOTER = "\n\nThis is an automatic notice from the Bot Office watchdog, not a bot message. Each item is mailed once; mails of one kind are at least 10 minutes apart and are merged into a digest.";

function line(e) {
  if (e.type === "recovery") return `- RECOVERED: ${e.text}`;
  if (e.type === "alert") {
    const gone = e.resolved_at ? ` (already recovered after about ${Math.max(1, Math.round((e.resolved_at - e.at) / 60000))} min)` : "";
    return `- ${e.text}${gone}`;
  }
  const it = e.item;
  return `- ${it.bot}: ${it.kind.replace(/_/g, " ")}${it.job ? ` (job ${it.job})` : ""}`;
}

/** The mails for one class: [{subject, text}], MAX_LINES entries each, every entry in exactly one. */
export function compose(cls, entries, now) {
  const parts = Math.max(1, Math.ceil(entries.length / MAX_LINES));
  const head = cls === "needs_you" ? "New items need you" : cls === "failed" ? "Failed turns" : cls === "done" ? "Finished turns" : "Bot Office needs attention";
  return Array.from({ length: parts }, (_, i) => {
    const chunk = entries.slice(i * MAX_LINES, (i + 1) * MAX_LINES), n = chunk.length;
    const tail = parts > 1 ? ` [part ${i + 1} of ${parts}]` : "";
    const subject = "[Bot Office] " + (cls === "outage" || cls === "backup" ? SUBJECT[cls]() + (n > 1 ? ` (${n} updates)` : "") : SUBJECT[cls](n)) + tail;
    const total = parts > 1 ? `Part ${i + 1} of ${parts}; ${entries.length} in all.\n` : "";
    return { subject, text: `${head} (${new Date(now).toISOString()}):\n${total}\n${chunk.map(line).join("\n")}${FOOTER}` };
  });
}

export class Engine {
  #chain = Promise.resolve();
  #draining = false;
  constructor({ storage, send, now = () => Date.now(), newId = () => crypto.randomUUID(), from = "", to = "", sendTimeoutMs = SEND_TIMEOUT_MS }) {
    if (to && String(to).trim().toLowerCase() !== RECIPIENT) throw new Error("recipient is not the allowed address");
    Object.assign(this, { storage, send, clock: now, newId, from, to: to ? RECIPIENT : "", sendTimeoutMs });
  }

  /** One state operation at a time, each reading state afresh from storage. */
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

  /** Take the controller's batch; answer {ack}: the highest sequence number safely stored here. */
  events(ev) {
    return this.#exclusive(async () => {
      const s = await this.#load(), now = this.clock();
      s.first_seen ??= now;
      s.prefs = ev.prefs;
      s.backup_reason = ev.backup_reason;
      if (s.epoch !== ev.epoch) { s.epoch = ev.epoch; s.cursor = 0; } // a different controller database: its numbering restarts
      let full = false;
      for (const e of ev.events) {
        if (e.seq <= s.cursor) continue; // already stored (an ack that never reached the controller)
        if (s.prefs[e.cls]) {
          // a disabled class is acknowledged without being queued, so turning it on later does not replay old items
          if (s.pending.filter((p) => p.cls === e.cls).length >= MAX_PENDING) { full = true; break; }
          s.pending.push({ id: e.id, cls: e.cls, type: "event", item: { bot: e.bot, job: e.job, kind: e.kind }, at: now });
        }
        s.cursor = e.seq;
      }
      if (!full) s.cursor = Math.max(s.cursor, ev.upto);
      await this.#save(s);
      await this.#schedule(s);
      return { ack: s.cursor };
    });
  }

  tick() {
    return this.#exclusive(async () => {
      const s = await this.#load(), now = this.clock();
      s.first_seen ??= now;
      this.#reconcile(s, evaluate(s.hb, now, s.first_seen), now);
      await this.#save(s);
      await this.#schedule(s);
    });
  }

  #reconcile(s, problems, now) {
    const skip = new Set(unjudged(problems, s.hb));
    const current = new Map(problems.map((p) => [p.kind, p]));
    for (const p of problems) {
      if (s.incidents[p.kind]) continue;
      const cls = classOf(p.kind);
      const muted = !s.prefs[cls];
      const text = p.text + (cls === "backup" && s.backup_reason ? ` Reason code: ${s.backup_reason}.` : "");
      s.incidents[p.kind] = { cls, since: now, muted, text };
      if (!muted) s.pending.push({ id: `inc:${p.kind}:${now}`, cls, type: "alert", key: p.kind, text, at: now });
    }
    for (const [kind, inc] of Object.entries(s.incidents)) {
      if (current.has(kind) || skip.has(inc.cls)) continue;
      delete s.incidents[kind];
      if (inc.muted || !s.prefs[inc.cls]) continue;
      const open = s.pending.find((e) => e.type === "alert" && e.key === kind && !e.resolved_at);
      if (open) open.resolved_at = now; // not mailed yet: the one mail says it is already over
      else s.pending.push({ id: `rec:${kind}:${now}`, cls: inc.cls, type: "recovery", key: kind, text: inc.text.split(" Reason code:")[0].replace(/\.$/, "") + ` (lasted about ${Math.max(1, Math.round((now - inc.since) / 60000))} min)`, at: now });
    }
  }

  #gap(s, now) {
    s.sent_log = s.sent_log.filter((t) => now - t < DAY_MS);
    return s.sent_log.length >= SOFT_CAP ? SLOW_GAP_MS : MIN_GAP_MS;
  }

  /** Set the alarm to the earliest moment something can be sent (outbox retry time, or a class gap ending). */
  async #schedule(s) {
    if (!this.storage.setAlarm) return;
    const now = this.clock(), gap = this.#gap(s, now);
    let when = null;
    const soon = (t) => { when = when === null ? t : Math.min(when, t); };
    for (const cls of CLASSES) {
      const box = (s.outbox[cls] || [])[0];
      if (box) soon(Math.max(now, box.next_at || 0));
      else if (s.pending.some((e) => e.cls === cls)) soon(Math.max(now, (s.last_sent[cls] || 0) + gap));
    }
    if (when !== null) await this.storage.setAlarm(when);
  }

  /** Run by the alarm: promote what may go out, send it outside the state queue, record the result. */
  async drain() {
    if (this.#draining || !this.from || !this.to) return;
    this.#draining = true;
    try {
      const due = await this.#exclusive(async () => {
        const s = await this.#load(), now = this.clock(), gap = this.#gap(s, now);
        let promoted = false;
        for (const cls of CLASSES) {
          if ((s.outbox[cls] || []).length) continue;
          const mine = s.pending.filter((e) => e.cls === cls);
          if (!mine.length || now - (s.last_sent[cls] || 0) < gap) continue; // held, kept pending, merged later
          // the complete messages, composed once: body, Date and Message-ID never change on a retry
          s.outbox[cls] = compose(cls, mine, now).map(({ subject, text }) => {
            const msg_id = this.newId();
            return { msg_id, raw: buildRaw({ from: this.from, to: this.to, subject, text, messageId: msg_id, now }), attempts: 0, next_at: now };
          });
          s.pending = s.pending.filter((e) => e.cls !== cls);
          promoted = true;
        }
        if (promoted) await this.#save(s); // write-ahead: if this fails nothing was sent and nothing is lost
        // the oldest message of each class, in order: a later part never overtakes a failing earlier one
        return CLASSES.filter((c) => (s.outbox[c] || []).length && (s.outbox[c][0].next_at || 0) <= now).map((c) => ({ cls: c, msg_id: s.outbox[c][0].msg_id, raw: s.outbox[c][0].raw }));
      });
      for (const mail of due) {
        let ok = true;
        try {
          await this.#bounded(this.send({ from: this.from, to: this.to, raw: mail.raw, messageId: mail.msg_id }));
        } catch (err) {
          ok = false;
          console.log("mail failed:", mail.cls, err && err.name); // class only
        }
        await this.#exclusive(async () => {
          const s = await this.#load(), now = this.clock(), box = (s.outbox[mail.cls] || [])[0];
          if (!box || box.msg_id !== mail.msg_id) return;
          if (ok) {
            s.outbox[mail.cls].shift();
            if (!s.outbox[mail.cls].length) delete s.outbox[mail.cls];
            s.last_sent[mail.cls] = now;
            s.sent_log.push(now);
          } else {
            box.attempts += 1;
            box.next_at = now + Math.min(BACKOFF_BASE_MS * 2 ** (box.attempts - 1), BACKOFF_MAX_MS);
          }
          await this.#save(s); // if this throws after a good send, the next run resends the same message
        });
      }
      await this.#exclusive(async () => this.#schedule(await this.#load()));
    } finally {
      this.#draining = false;
    }
  }

  #bounded(promise) {
    let timer;
    const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error("send timed out"), { name: "SendTimeout" })), this.sendTimeoutMs); });
    return Promise.race([promise, limit]).finally(() => clearTimeout(timer));
  }
}

export function makeSender(env, EmailMessage) {
  return async ({ from, to, raw }) => {
    if (String(to).trim().toLowerCase() !== RECIPIENT) throw Object.assign(new Error("recipient refused"), { name: "RecipientRefused" });
    await env.ALERT_EMAIL.send(new EmailMessage(from, to, raw));
  };
}
