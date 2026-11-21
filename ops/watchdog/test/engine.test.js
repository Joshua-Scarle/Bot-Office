import test from "node:test";
import assert from "node:assert/strict";
import { Engine, compose, MIN_GAP_MS, SOFT_CAP, SLOW_GAP_MS, MAX_PENDING, MAX_LINES, BACKOFF_BASE_MS } from "../src/engine.js";
import { sanitize, sanitizeEvents } from "../src/logic.js";

const MIN = 60000, T0 = 1_800_000_000_000;
const good = { controller_reachable: true, healthz: { ok: true, version: "1", scheduler: { alive: true }, db_reachable: true, running_jobs: 0, disk_free_pct: 50, backup: { state: "ok", age_hours: 3 } } };
const down = { controller_reachable: false, healthz: null };
const timedOut = { controller_reachable: true, healthz: { ok: false, version: "1", timed_out: true } };
const failedBackup = structuredClone(good); failedBackup.healthz.backup = { state: "failed", age_hours: 2 };

/** Decode a sent mail back to {subject, text, messageId}. */
function parse(m) {
  const [head, body] = m.raw.split("\r\n\r\n");
  const h = (n) => (head.match(new RegExp(`^${n}: (.*)$`, "m")) || [])[1];
  return { subject: h("Subject"), text: Buffer.from(body.replace(/\r\n/g, ""), "base64").toString("utf8"), messageId: h("Message-ID"), date: h("Date"), to: m.to, raw: m.raw };
}

function makeStorage() {
  const m = new Map();
  const st = {
    failPut: 0, alarm: null,
    async get(k) { const v = m.get(k); return v === undefined ? undefined : structuredClone(v); },
    async put(k, v) { if (st.failPut > 0) { st.failPut--; throw new Error("put failed"); } m.set(k, structuredClone(v)); },
    async setAlarm(t) { st.alarm = t; },
    raw: (k) => m.get(k),
  };
  return st;
}
function rig({ failSend = 0, sendDelay = 0, sendTimeoutMs = 50 } = {}) {
  const storage = makeStorage(), sent = [], clock = { t: T0 }, ctl = { failSend, sendDelay, hang: false, attempts: 0 };
  let n = 0, seq = 0;
  const send = async (msg) => {
    ctl.attempts++;
    if (ctl.hang) await new Promise(() => {});
    if (ctl.sendDelay) await new Promise((r) => setTimeout(r, ctl.sendDelay));
    if (ctl.failSend > 0) { ctl.failSend--; throw new Error("send failed"); }
    sent.push(msg);
  };
  const mk = (st = storage) => new Engine({ storage: st, send, now: () => clock.t, newId: () => `id${++n}`, from: "w@example.test", to: "you@example.test", sendTimeoutMs });
  const engine = mk();
  const r = {
    storage, sent, clock, ctl, engine, mk,
    get mails() { return sent.map(parse); },
    at: (min) => (clock.t = T0 + min * MIN),
    beat: (d = good) => engine.heartbeat(sanitize(d)),
    /** the controller pushes `list` ([{cls, id?, bot?, kind?}]) as the next batch; returns the ack */
    async push(list, over = {}) {
      const events = list.map((e) => ({ seq: ++seq, id: e.id ?? `${e.cls}:${seq}`, cls: e.cls, bot: e.bot ?? "bot-aaaaaa", kind: e.kind ?? "question_open", job: e.job ?? null }));
      return engine.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: seq, events, ...over }));
    },
    /** what the alarm would do */
    go: () => engine.drain(),
  };
  return r;
}
const need = (n, cls = "needs_you") => Array.from({ length: n }, () => ({ cls }));

test("a new needs-you event mails once, from the alarm, not from the push", async () => {
  const r = rig();
  const res = await r.push([{ cls: "needs_you", id: "q:1:open" }]);
  assert.equal(res.ack, 1);
  assert.equal(r.sent.length, 0); // the push only stored it
  assert.ok(r.storage.alarm !== null && r.storage.alarm <= r.clock.t);
  await r.go();
  assert.equal(r.mails.length, 1);
  assert.match(r.mails[0].subject, /1 item needs you/);
  assert.match(r.mails[0].text, /bot-aaaaaa: question open/);
  await r.go();
  assert.equal(r.mails.length, 1);
});

test("events already acknowledged are not queued again (a lost ack is harmless)", async () => {
  const r = rig();
  const ev = sanitizeEvents({ prefs: {}, epoch: "e1", upto: 1, events: [{ seq: 1, id: "q:1:open", cls: "needs_you", bot: "bot-aaaaaa", kind: "question_open" }] });
  await r.engine.events(ev); await r.engine.events(ev);
  await r.go();
  assert.equal(r.mails[0].text.match(/^- /gm).length, 1);
});

test("many arriving together become one digest per class", async () => {
  const r = rig();
  await r.push([...need(7), { cls: "failed", kind: "auth", job: "j1" }, { cls: "failed", kind: "provider_error" }]);
  await r.go();
  assert.equal(r.mails.length, 2);
  assert.match(r.mails.find((m) => /need you/.test(m.subject)).subject, /7 items need you/);
  assert.match(r.mails.find((m) => /failed/.test(m.subject)).text, /auth \(job j1\)/);
});

test("rate limit merges, never drops", async () => {
  const r = rig();
  await r.push(need(1)); await r.go();
  r.at(2); await r.push(need(1)); await r.go();
  r.at(4); await r.push(need(1)); await r.go();
  assert.equal(r.mails.length, 1);
  assert.ok(r.storage.alarm >= T0 + MIN_GAP_MS); // the alarm is set for when the gap ends
  r.at(11); await r.go();
  assert.equal(r.mails.length, 2);
  assert.equal(r.mails[1].text.match(/^- /gm).length, 2);
});

test("OVERFLOW: 500 events end to end: every identity is in some mail, split into parts, exact counts", async () => {
  const r = rig();
  let acked = 0, pushes = 0, minute = 0;
  // the controller keeps pushing from its cursor; the Worker takes what fits and acks just that
  const queue = Array.from({ length: 500 }, (_, i) => ({ seq: i + 1, id: `e${String(i).padStart(16, "0")}`, cls: "failed", bot: "bot-aaaaaa", kind: "auth", job: `job${i}` }));
  while (acked < 500 && pushes++ < 100) {
    r.at(minute);
    const batch = queue.filter((e) => e.seq > acked).slice(0, 100);
    const res = await r.engine.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: batch[batch.length - 1].seq, events: batch }));
    acked = Math.max(acked, res.ack);
    for (let i = 0; i < 6; i++) { await r.go(); r.clock.t += 61 * MIN; } // each part waits out even the slow gap (more than SOFT_CAP mails go out)
    minute = Math.round((r.clock.t - T0) / MIN);
  }
  assert.equal(acked, 500);
  r.at(minute + 61); for (let i = 0; i < 6; i++) { await r.go(); r.clock.t += 61 * MIN; }
  const seen = new Set();
  let total = 0;
  for (const m of r.mails) {
    const lines = m.text.match(/^- .*$/gm);
    assert.ok(lines.length <= MAX_LINES);
    assert.ok(!/\band \d+ more\b/.test(m.text), "no 'and N more'");
    assert.equal(Number(m.subject.match(/(\d+) turns/)[1]), lines.length, "the subject count is the number of lines");
    for (const l of lines) seen.add(l.match(/job (job\d+)/)[1]);
    total += lines.length;
  }
  assert.equal(total, 500);
  assert.equal(seen.size, 500, "every incident identity was delivered");
  assert.ok(r.mails.some((m) => /\[part 3 of 3\]/.test(m.subject)) || r.mails.some((m) => /\[part 2 of 3\]/.test(m.subject)));
});

test("a digest of 100 failed jobs becomes three mails (40, 40, 20) that together name all 100, in order", async () => {
  const r = rig();
  await r.push(Array.from({ length: 100 }, (_, i) => ({ cls: "failed", kind: "auth", job: `j${i}` })));
  for (let i = 0; i < 5; i++) { await r.go(); r.clock.t += MIN_GAP_MS; }
  assert.deepEqual(r.mails.map((m) => m.text.match(/^- .*$/gm).length), [40, 40, 20]);
  assert.match(r.mails[0].subject, /40 turns failed \[part 1 of 3\]/);
  assert.match(r.mails[2].subject, /20 turns failed \[part 3 of 3\]/);
  assert.match(r.mails[1].text, /Part 2 of 3; 100 in all/);
  assert.deepEqual(r.mails.flatMap((m) => m.text.match(/job (j\d+)/g)), Array.from({ length: 100 }, (_, i) => `job j${i}`));
});

test("a failing part is retried and later parts do not overtake it", async () => {
  const r = rig({ failSend: 1 });
  await r.push(Array.from({ length: 50 }, (_, i) => ({ cls: "failed", kind: "auth", job: `j${i}` })));
  await r.go();
  assert.equal(r.sent.length, 0);
  r.clock.t += BACKOFF_BASE_MS + 1;
  for (let i = 0; i < 4; i++) { await r.go(); r.clock.t += MIN_GAP_MS; }
  assert.deepEqual(r.mails.map((m) => m.text.match(/^- .*$/gm).length), [40, 10]);
});

test("parts of one digest keep the mail gap: never two same-class mails closer than ten minutes", async () => {
  const r = rig();
  await r.push(Array.from({ length: 100 }, (_, i) => ({ cls: "failed", kind: "auth", job: `j${i}` })));
  const times = [];
  for (let min = 0; min <= 40 && r.sent.length < 3; min++) {
    r.at(min);
    const before = r.sent.length;
    await r.go();
    if (r.sent.length > before) times.push(min);
    await r.go(); // a second alarm in the same minute sends nothing more
  }
  assert.equal(r.sent.length, 3);
  assert.ok(times[1] - times[0] >= 10 && times[2] - times[1] >= 10, `sent at minutes ${times}`);
  assert.ok(r.storage.alarm === null || r.storage.alarm >= r.clock.t);
});

test("every digest line carries the pseudonymous event id, so two same-kind events are told apart", async () => {
  const r = rig();
  await r.push([{ cls: "needs_you", id: "eaaaaaaaaaaaaaaa1", bot: "bot-abc123" }, { cls: "needs_you", id: "ebbbbbbbbbbbbbbb2", bot: "bot-abc123" }]);
  await r.go();
  const lines = r.mails[0].text.match(/^- .*$/gm);
  assert.equal(lines.length, 2);
  assert.notEqual(lines[0], lines[1]);
  assert.match(lines[0], /\[eaaaaaaaaaaaaaaa1\]$/);
});

test("a backup status unreadable for hours raises an alert, keeps existing incidents, and recovers when readable", async () => {
  const r = rig();
  await r.beat(failedBackup); await r.engine.tick(); await r.go();
  for (let h = 1; h <= 7; h++) { r.at(h * 60); await r.beat(timedOut); await r.engine.tick(); await r.go(); }
  const inc = r.storage.raw("s").incidents;
  assert.ok(inc.backup_state, "the earlier incident is preserved");
  assert.ok(inc.backup_unverified, "sustained inability to read it is its own incident");
  r.at(8 * 60); await r.beat(good); await r.engine.tick(); await r.go();
  assert.equal(r.storage.raw("s").incidents.backup_unverified, undefined);
  assert.equal(r.storage.raw("s").unjudged_since, undefined);
});

test("status reports what the Worker still holds unsent, for a rollback to wait on", async () => {
  const r = rig();
  assert.deepEqual(await r.engine.status(), { pending: 0, outbox: 0 });
  await r.push(need(3));
  assert.deepEqual(await r.engine.status(), { pending: 3, outbox: 0 });
  await r.go();
  assert.deepEqual(await r.engine.status(), { pending: 0, outbox: 0 });
  const f = rig({ failSend: 1 });
  await f.push(need(2)); await f.go();
  assert.deepEqual(await f.engine.status(), { pending: 0, outbox: 1 }); // composed, being retried
});

test("when the Worker is full it acks only what it stored; the rest stays queued at the controller", async () => {
  const r = rig();
  r.engine.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: 0, events: [] }));
  const mkEvents = (from, n) => Array.from({ length: n }, (_, i) => ({ seq: from + i, id: `q:${from + i}`, cls: "needs_you", bot: "bot-aaaaaa", kind: "question_open" }));
  const a = await r.engine.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: 100, events: mkEvents(1, 100) }));
  assert.equal(a.ack, 100);
  const b = await r.engine.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: 200, events: mkEvents(101, 100) }));
  assert.equal(b.ack, 100); // full: nothing taken, nothing lost
  assert.equal(r.storage.raw("s").pending.length, 100);
  await r.go(); r.at(11);
  const c = await r.engine.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: 200, events: mkEvents(101, 100) }));
  assert.equal(c.ack, 200); // room again after the digest went out
});

test("a different controller epoch restarts the cursor", async () => {
  const r = rig();
  await r.push(need(2));
  const res = await r.engine.events(sanitizeEvents({ prefs: {}, epoch: "e2", upto: 1, events: [{ seq: 1, id: "q:9:open", cls: "needs_you", bot: "bot-aaaaaa", kind: "question_open" }] }));
  assert.equal(res.ack, 1);
  await r.go();
  assert.equal(r.mails[0].text.match(/^- /gm).length, 3);
});

test("an incident that recovers before its mail is allowed is still mailed, as already recovered", async () => {
  const r = rig();
  await r.beat(down); r.at(2); await r.engine.tick(); await r.go();
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 1);
  await r.beat(good); r.at(3); await r.engine.tick(); await r.go();
  await r.beat(down); r.at(4); await r.engine.tick(); await r.go();
  await r.beat(good); r.at(5); await r.engine.tick(); await r.go();
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 1);
  r.at(13); await r.beat(); await r.engine.tick(); await r.go();
  const outage = r.mails.filter((m) => /server or controller/.test(m.subject));
  assert.equal(outage.length, 2);
  assert.match(outage[1].text, /already recovered/);
  assert.match(outage[1].text, /RECOVERED/);
});

test("a failed backup mails with the reason CODE; heartbeat loss does not recover it falsely", async () => {
  const r = rig();
  await r.beat(failedBackup);
  await r.push([], { backup_reason: "stage_upload" });
  await r.engine.tick(); await r.go();
  const bk = r.mails.filter((m) => /backup/.test(m.subject));
  assert.equal(bk.length, 1);
  assert.match(bk[0].text, /Reason code: stage_upload/);
  r.at(20); await r.engine.tick(); await r.go();
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 1);
  assert.equal(r.mails.filter((m) => /backup/.test(m.subject)).length, 1);
});

test("a timed-out /healthz leaves the backup unjudged: no backup incident, and an open one is not recovered", async () => {
  const r = rig();
  await r.beat(timedOut); await r.engine.tick(); await r.go();
  assert.equal(r.mails.filter((m) => /backup/.test(m.subject)).length, 0);
  assert.deepEqual(Object.keys(r.storage.raw("s").incidents), ["unhealthy"]);
  // an existing backup incident survives a timed-out answer instead of being recovered by it
  const q = rig();
  await q.beat(failedBackup); await q.engine.tick(); await q.go();
  q.at(11); await q.beat(timedOut); await q.engine.tick(); await q.go();
  assert.ok(q.storage.raw("s").incidents.backup_state);
  assert.equal(q.mails.filter((m) => /RECOVERED/.test(m.text) && /Backup/.test(m.text)).length, 0);
  q.at(12); await q.beat(good); await q.engine.tick(); await q.go();
  assert.equal(q.storage.raw("s").incidents.backup_state, undefined);
});

test("class toggles: a disabled class is acknowledged but not mailed, and not replayed when enabled", async () => {
  const r = rig();
  const res = await r.push([{ cls: "needs_you" }, { cls: "failed", kind: "auth" }], { prefs: { needs_you: false } });
  assert.equal(res.ack, 2);
  await r.go();
  assert.equal(r.mails.length, 1);
  assert.match(r.mails[0].subject, /failed/);
  r.at(60); await r.push([], { prefs: { needs_you: true } }); await r.go();
  assert.equal(r.mails.length, 1);
  await r.beat(down); r.at(61); await r.push([], { prefs: { outage: false } }); await r.engine.tick(); await r.go();
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 0);
});

test("done summaries are off by default and on when enabled", async () => {
  const r = rig();
  await r.push([{ cls: "done", kind: "succeeded" }]); await r.go();
  assert.equal(r.mails.length, 0);
  r.at(1); await r.push([{ cls: "done", kind: "succeeded" }], { prefs: { done: true } }); await r.go();
  assert.equal(r.mails.length, 1);
  assert.match(r.mails[0].subject, /1 turn finished/);
});

test("overlapping crons, pushes and alarms send exactly one mail", async () => {
  const r = rig({ sendDelay: 20, sendTimeoutMs: 500 });
  await r.beat(down); r.at(20);
  await Promise.all([r.engine.tick(), r.engine.tick(), r.go(), r.go(), r.engine.tick()]);
  await r.go();
  assert.equal(r.sent.length, 1);
});

test("a second engine on the same storage sees the first's writes", async () => {
  const r = rig();
  const other = r.mk();
  await r.push(need(1));
  await other.events(sanitizeEvents({ prefs: {}, epoch: "e1", upto: 1, events: [{ seq: 1, id: "q:1", cls: "needs_you", bot: "bot-aaaaaa", kind: "question_open" }] }));
  await other.drain(); await r.go();
  assert.equal(r.sent.length, 1);
});

test("the complete message is persisted before the first send and re-sent byte for byte", async () => {
  const r = rig({ failSend: 2 });
  await r.push([{ cls: "needs_you", bot: "bot-aaaaaa" }]);
  await r.go(); // send fails
  const box = r.storage.raw("s").outbox.needs_you[0];
  assert.ok(box && box.raw.includes("Message-ID: <id1@example.test>") && box.raw.includes("Date: "));
  const saved = box.raw;
  r.clock.t += BACKOFF_BASE_MS + 1; // later: a recomposed message would carry a new Date
  await r.push([{ cls: "needs_you", bot: "bot-bbbbbb" }]); // a new event must not alter the persisted message
  await r.go(); // fails again
  r.clock.t += 2 * BACKOFF_BASE_MS + 1;
  await r.go(); // succeeds
  assert.equal(r.sent.length, 1);
  assert.equal(r.sent[0].raw, saved);
  assert.equal(r.ctl.attempts, 3);
  assert.deepEqual(r.storage.raw("s").outbox, {});
  assert.equal(r.storage.raw("s").pending.length, 1); // Bob waits for the next mail
});

test("failed sends back off exponentially and are not retried on every push", async () => {
  const r = rig({ failSend: 100 });
  await r.push(need(1)); await r.go();
  assert.equal(r.ctl.attempts, 1);
  for (let i = 0; i < 5; i++) { r.clock.t += 1000; await r.push([]); await r.go(); }
  assert.equal(r.ctl.attempts, 1); // pushes and alarms inside the backoff window do not resend
  const waits = [];
  for (let k = 0; k < 4; k++) {
    const next = r.storage.raw("s").outbox.needs_you[0].next_at;
    waits.push(next - r.clock.t);
    r.clock.t = next; await r.go();
  }
  assert.ok(waits[1] > waits[0] && waits[2] > waits[1]);
  assert.ok(r.storage.alarm >= r.storage.raw("s").outbox.needs_you[0].next_at); // alarm follows the backoff
});

test("a hung mail server is cut off by the send timeout and does not block heartbeats", async () => {
  const r = rig({ sendTimeoutMs: 80 });
  r.ctl.hang = true;
  await r.push(need(1));
  const draining = r.go();
  await new Promise((res) => setTimeout(res, 10));
  const t = Date.now();
  await r.beat(); // handled while the send is still in flight
  assert.ok(Date.now() - t < 40, "heartbeat is not queued behind the send");
  await draining;
  assert.equal(r.storage.raw("s").outbox.needs_you[0].attempts, 1);
  assert.equal(r.sent.length, 0);
});

test("write failure after a successful send repeats that one identical mail (at-least-once)", async () => {
  const r = rig();
  await r.push(need(1));
  const real = r.storage.put.bind(r.storage);
  let calls = 0;
  r.storage.put = async (k, v) => { calls++; if (calls === 2) throw new Error("put failed after send"); return real(k, v); }; // 1: write-ahead, 2: result
  await assert.rejects(r.go());
  assert.equal(r.sent.length, 1);
  await r.go();
  assert.equal(r.sent.length, 2);
  assert.equal(r.sent[0].raw, r.sent[1].raw);
  await r.go();
  assert.equal(r.sent.length, 2);
});

test("write failure before the send: nothing is sent and nothing is lost", async () => {
  const r = rig();
  await r.push(need(1));
  r.storage.failPut = 1;
  await assert.rejects(r.go());
  assert.equal(r.sent.length, 0);
  await r.go();
  assert.equal(r.sent.length, 1);
});

test("the daily soft cap slows mails down but never drops one", async () => {
  const r = rig();
  for (let i = 0; i < SOFT_CAP; i++) { r.at(i * 11); await r.push(need(1)); await r.go(); }
  assert.equal(r.sent.length, SOFT_CAP);
  const t = r.clock.t;
  r.clock.t = t + 11 * MIN; await r.push(need(1)); await r.go();
  assert.equal(r.sent.length, SOFT_CAP);
  r.clock.t = t + SLOW_GAP_MS + MIN; await r.go();
  assert.equal(r.sent.length, SOFT_CAP + 1);
});

test("compose: plain ASCII subject, one line per entry, exact counts", () => {
  const e = (i) => ({ type: "event", item: { bot: "A", kind: "auth", job: "" } });
  const [m] = compose("failed", [e()], T0);
  assert.match(m.subject, /^\[Bot Office\] a turn failed$/);
  assert.equal(m.text.match(/^- /gm).length, 1);
  const big = compose("failed", Array.from({ length: 500 }, e), T0);
  assert.equal(big.length, 13); // 12 x 40 + 20: every entry in exactly one mail
  assert.equal(big.reduce((n, p) => n + p.text.match(/^- /gm).length, 0), 500);
  assert.match(big[0].subject, /40 turns failed \[part 1 of 13\]/);
  assert.match(big[12].subject, /20 turns failed \[part 13 of 13\]/);
  assert.ok(big.every((p) => !/and \d+ more/.test(p.text)));
});

test("no free text can reach a mail: only strict patterns survive, the rest become placeholders", () => {
  const evs = sanitizeEvents({ prefs: {}, epoch: "e1", upto: 1, backup_reason: "password: hunter2 here", events: [
    { seq: 1, id: "j:1:1", cls: "failed", bot: "my password is hunter2, call 555-0100", job: "j 1; DROP", kind: "Sentence with spaces.", reason: "secret text", error: "x", text: "y" },
  ] });
  assert.equal(evs.backup_reason, null);
  assert.deepEqual(evs.events[0], { seq: 1, id: "j:1:1", cls: "failed", bot: "a bot", job: "", kind: "other" });
  assert.equal(sanitizeEvents({ backup_reason: "stage_upload" }).backup_reason, "stage_upload");
  assert.equal(compose("failed", [{ type: "event", item: evs.events[0] }], T0)[0].text.includes("hunter2"), false);
});

test("MIN_GAP_MS is ten minutes", () => assert.equal(MIN_GAP_MS, 10 * MIN));

test("a recovery counts the outage from the last good heartbeat, not from its detection", async () => {
  const r = rig();
  await r.beat(); await r.engine.tick(); await r.go(); // last heartbeat at minute 0
  r.at(20); await r.engine.tick(); await r.go(); // detected at minute 20 (heartbeat missing)
  const alert = r.mails.filter((m) => /server or controller/.test(m.subject));
  assert.equal(alert.length, 1);
  r.at(45); await r.beat(); await r.engine.tick(); await r.go();
  const done = r.mails.filter((m) => /RECOVERED/.test(m.text));
  assert.equal(done.length, 1);
  assert.match(done[0].text, /lasted about 45 min/); // not 25 (minutes since detection)
});

test("an unmailed alert that already recovered counts from the last healthy heartbeat", async () => {
  const r = rig();
  await r.beat(); r.at(1); await r.engine.tick();
  await r.beat(down); r.at(2); await r.engine.tick(); await r.go(); // first outage alert mailed at minute 2
  await r.beat(good); r.at(3); await r.engine.tick(); await r.go(); // recovered; its mail waits for the gap
  r.at(4); await r.beat(); // the last healthy heartbeat before the next outage
  r.at(6); await r.beat(down); await r.engine.tick(); await r.go(); // detected at minute 6, held by the gap
  r.at(9); await r.beat(good); await r.engine.tick(); await r.go();
  r.at(13); await r.beat(); await r.engine.tick(); await r.go();
  const outage = r.mails.filter((m) => /server or controller/.test(m.subject));
  assert.equal(outage.length, 2);
  assert.match(outage[1].text, /already recovered after about 5 min/); // minute 4 to 9, not 6 to 9
});

test("with no heartbeat time known the duration falls back to the detection time", async () => {
  const r = rig();
  r.at(0); await r.beat(down); r.at(1); await r.engine.tick(); await r.go(); // never healthy: no last good heartbeat
  r.at(11); await r.beat(good); await r.engine.tick(); await r.go();
  const done = r.mails.filter((m) => /RECOVERED/.test(m.text));
  assert.equal(done.length, 1);
  assert.match(done[0].text, /lasted about 10 min/);
});

test("a recovery ends at the first healthy heartbeat, not at the tick that notices it", async () => {
  const r = rig();
  await r.beat(); await r.engine.tick(); await r.go(); // last heartbeat at minute 0
  r.at(20); await r.engine.tick(); await r.go(); // detected at minute 20
  r.at(45); await r.beat(good); // healthy again at minute 45 ...
  r.at(49); await r.engine.tick(); await r.go(); // ... noticed at minute 49
  const done = r.mails.filter((m) => /RECOVERED/.test(m.text));
  assert.equal(done.length, 1);
  assert.match(done[0].text, /lasted about 45 min/); // not 49
});

test("an unmailed alert that recovered counts to the healthy heartbeat, not the tick", async () => {
  const r = rig();
  await r.beat(); r.at(1); await r.engine.tick();
  await r.beat(down); r.at(2); await r.engine.tick(); await r.go(); // first outage alert mailed at minute 2
  await r.beat(good); r.at(3); await r.engine.tick(); await r.go(); // recovered; its mail waits for the gap
  r.at(4); await r.beat(); // the last healthy heartbeat before the next outage
  r.at(6); await r.beat(down); await r.engine.tick(); await r.go(); // detected at minute 6, held by the gap
  r.at(9); await r.beat(good); // healthy again at minute 9 ...
  r.at(12); await r.engine.tick(); await r.go(); // ... noticed at minute 12
  r.at(13); await r.beat(); await r.engine.tick(); await r.go();
  const outage = r.mails.filter((m) => /server or controller/.test(m.subject));
  assert.equal(outage.length, 2);
  assert.match(outage[1].text, /already recovered after about 5 min/); // minute 4 to 9, not 12
});

test("a heartbeat that fails again after a healthy one does not end the outage", async () => {
  const r = rig();
  await r.beat(good); await r.engine.tick(); await r.go();
  r.at(5); await r.beat(down); r.at(6); await r.engine.tick(); await r.go(); // down, detected at minute 6
  r.at(10); await r.beat(good); r.at(12); await r.beat(down); // a healthy blip, then down again
  r.at(30); await r.beat(good); await r.engine.tick(); await r.go();
  const done = r.mails.filter((m) => /RECOVERED/.test(m.text));
  assert.equal(done.length, 1);
  assert.match(done[0].text, /lasted about 30 min/); // from the last healthy heartbeat (0) to 30
});

const sick = { controller_reachable: true, healthz: { ...good.healthz, db_reachable: false } };

test("controller_down ends at the first heartbeat that reaches the controller, healthy or not", async () => {
  const r = rig();
  await r.beat(good); await r.engine.tick(); await r.go(); // good at minute 0
  r.at(5); await r.beat(down); r.at(6); await r.engine.tick(); await r.go(); // down, detected at 6
  r.at(10); await r.beat(sick); // reachable but unhealthy at minute 10 ...
  r.at(15); await r.engine.tick(); await r.go(); // ... noticed at 15
  r.at(20); await r.beat(good); await r.engine.tick(); await r.go();
  const done = r.mails.filter((m) => /RECOVERED/.test(m.text) && /controller/i.test(m.text));
  assert.ok(done.length >= 1);
  assert.match(done[0].text, /lasted about 10 min/); // 0 to 10, not 15 or 20
});

test("a heartbeat_missing recovery stamp is cleared when another miss happens before reconciliation", async () => {
  const r = rig();
  await r.beat(); await r.engine.tick(); await r.go(); // heartbeat at minute 0
  r.at(20); await r.engine.tick(); await r.go(); // missing, detected at 20
  r.at(25); await r.beat(); // a heartbeat at 25 stamps a recovery ...
  r.at(60); await r.engine.tick(); await r.go(); // ... but the tick at 60 finds it missing again: still the same outage
  r.at(70); await r.beat(); await r.engine.tick(); await r.go();
  const done = r.mails.filter((m) => /RECOVERED/.test(m.text));
  assert.equal(done.length, 1);
  assert.match(done[0].text, /lasted about 70 min/); // not 25
});
