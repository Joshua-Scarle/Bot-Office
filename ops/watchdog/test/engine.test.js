import test from "node:test";
import assert from "node:assert/strict";
import { Engine, compose, MIN_GAP_MS, SOFT_CAP, SLOW_GAP_MS } from "../src/engine.js";
import { sanitize, sanitizeEvents } from "../src/logic.js";

const MIN = 60000, T0 = 1_800_000_000_000;
const good = { controller_reachable: true, healthz: { ok: true, version: "1", scheduler: { alive: true }, db_reachable: true, running_jobs: 0, disk_free_pct: 50, backup: { state: "ok", age_hours: 3 } } };
const down = { controller_reachable: false, healthz: null };
const ev = (over = {}) => sanitizeEvents({ prefs: {}, ...over });
const item = (id, extra = {}) => ({ id, bot: "Ann", kind: "question open", ...extra });

/** Strongly consistent in-memory storage with injectable failures and latency. */
function makeStorage() {
  const m = new Map();
  const st = {
    failPut: 0, puts: 0, delay: 0,
    async get(k) { if (st.delay) await new Promise((r) => setTimeout(r, st.delay)); const v = m.get(k); return v === undefined ? undefined : structuredClone(v); },
    async put(k, v) { st.puts++; if (st.failPut > 0) { st.failPut--; throw new Error("put failed"); } m.set(k, structuredClone(v)); },
    raw: (k) => m.get(k),
  };
  return st;
}
function rig({ sendDelay = 0, failSend = 0 } = {}) {
  const storage = makeStorage(), mails = [], clock = { t: T0 };
  const ctl = { failSend, sendDelay };
  let n = 0;
  const send = async (msg) => {
    if (ctl.sendDelay) await new Promise((r) => setTimeout(r, ctl.sendDelay));
    if (ctl.failSend > 0) { ctl.failSend--; throw new Error("send failed"); }
    mails.push(msg);
  };
  const engine = new Engine({ storage, send, now: () => clock.t, newId: () => `id${++n}` });
  // `beat` = the VPS pushing a healthy heartbeat, so a tick judges the heartbeat fresh
  return { storage, mails, clock, ctl, engine, at: (min) => (clock.t = T0 + min * MIN), beat: () => engine.heartbeat(sanitize(good)) };
}

test("a new needs-you item mails once; the same item again does not", async () => {
  const r = rig();
  await r.engine.events(ev({ needs_you: { count: 1, items: [item("q:1:open")] } }));
  assert.equal(r.mails.length, 1);
  assert.match(r.mails[0].subject, /1 item needs you/);
  r.at(30); await r.beat();
  await r.engine.events(ev({ needs_you: { count: 1, items: [item("q:1:open")] } }));
  await r.engine.tick();
  assert.equal(r.mails.length, 1);
});

test("many arriving together become one digest per class", async () => {
  const r = rig();
  const items = Array.from({ length: 7 }, (_, i) => item(`q:${i}:open`, { bot: `Bot${i}` }));
  await r.engine.events(ev({ needs_you: { count: 7, items }, failed: { count: 2, items: [item("j:1:1", { kind: "auth", job: "1", reason: "sign in" }), item("j:2:1")] } }));
  assert.equal(r.mails.length, 2); // one for needs_you, one for failed
  assert.match(r.mails.find((m) => /need you/.test(m.subject)).subject, /7 items need you/);
  assert.match(r.mails.find((m) => /failed/.test(m.subject)).text, /sign in/);
});

test("mails of a class are rate limited by merging, never dropped", async () => {
  const r = rig();
  await r.engine.events(ev({ needs_you: { count: 1, items: [item("a")] } }));
  r.at(2); await r.engine.events(ev({ needs_you: { count: 2, items: [item("a"), item("b")] } }));
  r.at(4); await r.engine.events(ev({ needs_you: { count: 3, items: [item("a"), item("b"), item("c")] } }));
  assert.equal(r.mails.length, 1); // b and c wait
  r.at(11); await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, 2);
  assert.match(r.mails[1].text, /Ann: question open/);
  assert.equal(r.mails[1].text.match(/^- /gm).length, 2); // b and c in one digest
});

test("an incident that recovers before its mail is allowed is still mailed, as already recovered", async () => {
  const r = rig();
  await r.engine.heartbeat(sanitize(down)); r.at(2); await r.engine.tick(); // alert 1 sent
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 1);
  await r.engine.heartbeat(sanitize(good)); r.at(3); await r.engine.tick(); // recovers inside the gap
  await r.engine.heartbeat(sanitize(down)); r.at(4); await r.engine.tick(); // second incident, held by the gap
  await r.engine.heartbeat(sanitize(good)); r.at(5); await r.engine.tick(); // recovered before being mailed
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 1);
  r.at(13); await r.beat(); await r.engine.tick();
  const outage = r.mails.filter((m) => /server or controller/.test(m.subject));
  assert.equal(outage.length, 2);
  assert.match(outage[1].text, /already recovered/);
  assert.match(outage[1].text, /RECOVERED/); // the first incident's recovery rides along
});

test("heartbeat lost then back: one alert, one recovery; backup incidents are not falsely recovered", async () => {
  const r = rig();
  const badBackup = structuredClone(good); badBackup.healthz.backup = { state: "failed", age_hours: 2 };
  await r.engine.heartbeat(sanitize(badBackup));
  await r.engine.events(ev({ backup_reason: "rclone exit 7" }));
  await r.engine.tick();
  const backupMail = r.mails.filter((m) => /backup/.test(m.subject));
  assert.equal(backupMail.length, 1);
  assert.match(backupMail[0].text, /rclone exit 7/);
  r.at(20); await r.engine.tick(); // heartbeat is now 20 min old: outage, backup not judged
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 1);
  assert.equal(r.mails.filter((m) => /backup/.test(m.subject)).length, 1); // no false backup recovery
  r.at(21); await r.engine.heartbeat(sanitize(good)); await r.engine.tick();
  assert.equal(r.mails.filter((m) => /RECOVERED/.test(m.text)).length >= 1, true);
});

test("class toggles: a disabled class is not mailed, others are", async () => {
  const r = rig();
  await r.engine.events(ev({ prefs: { needs_you: false }, needs_you: { count: 1, items: [item("q")] }, failed: { count: 1, items: [item("j:1:1", { kind: "x" })] } }));
  assert.equal(r.mails.length, 1);
  assert.match(r.mails[0].subject, /failed/);
  r.at(60); await r.engine.events(ev({ prefs: { needs_you: true }, needs_you: { count: 1, items: [item("q")] } }));
  assert.equal(r.mails.length, 1); // the old item was remembered while off; no replay
  await r.engine.events(ev({ prefs: { outage: false }, }));
  await r.engine.heartbeat(sanitize(down)); r.at(61); await r.engine.tick();
  assert.equal(r.mails.filter((m) => /server or controller/.test(m.subject)).length, 0);
  const text = JSON.stringify(r.storage.raw("s").prefs);
  assert.match(text, /"outage":false/);
});

test("done summaries are off by default and on when enabled", async () => {
  const r = rig();
  await r.engine.events(ev({ done: { count: 1, items: [item("j:5:1", { kind: "succeeded" })] } }));
  assert.equal(r.mails.length, 0);
  r.at(1); await r.engine.events(ev({ prefs: { done: true }, done: { count: 2, items: [item("j:5:1"), item("j:6:1")] } }));
  assert.equal(r.mails.length, 1); // only the new one, j:6
  assert.match(r.mails[0].subject, /1 turn finished/);
});

test("overlapping crons and pushes send exactly one mail", async () => {
  const r = rig({ sendDelay: 20 });
  await r.engine.heartbeat(sanitize(down));
  r.at(1);
  await Promise.all([r.engine.tick(), r.engine.tick(), r.engine.tick(), r.engine.events(ev({})), r.engine.tick()]);
  assert.equal(r.mails.length, 1);
});

test("stale reads: every operation re-reads storage; a second engine on the same storage sees the first's writes", async () => {
  const r = rig();
  const other = new Engine({ storage: r.storage, send: async (m) => r.mails.push(m), now: () => r.clock.t, newId: () => "other" });
  await r.engine.events(ev({ needs_you: { count: 1, items: [item("q")] } }));
  await other.events(ev({ needs_you: { count: 1, items: [item("q")] } })); // already seen via storage
  r.at(30); await r.beat(); await other.tick(); await r.engine.tick();
  assert.equal(r.mails.length, 1);
});

test("send failure keeps the message queued and retries it with the same message id", async () => {
  const r = rig({ failSend: 2 });
  await r.engine.events(ev({ needs_you: { count: 1, items: [item("q")] } }));
  assert.equal(r.mails.length, 0);
  assert.equal(Object.keys(r.storage.raw("s").outbox).length, 1);
  r.at(5); await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, 0);
  r.at(10); await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, 1);
  assert.equal(r.mails[0].messageId, "id1");
  assert.deepEqual(r.storage.raw("s").outbox, {});
  r.at(15); await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, 1);
});

test("write failure after a successful send repeats that one mail with the same message id (never loses it)", async () => {
  const r = rig();
  // events(): save (items), write-ahead save, send, final save. Fail only the final one.
  const real = r.storage.put.bind(r.storage);
  let calls = 0;
  r.storage.put = async (k, v) => { calls++; if (calls === 3) throw new Error("put failed after send"); return real(k, v); };
  await assert.rejects(r.engine.events(ev({ needs_you: { count: 1, items: [item("q")] } })));
  assert.equal(r.mails.length, 1);
  r.at(5); await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, 2);
  assert.equal(r.mails[0].messageId, r.mails[1].messageId); // the mail client dedupes on Message-ID
  r.at(10); await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, 2); // and then it is settled
});

test("write failure before the send: nothing is sent and the item is not lost", async () => {
  const r = rig();
  r.storage.failPut = 1;
  await assert.rejects(r.engine.events(ev({ needs_you: { count: 1, items: [item("q")] } })));
  assert.equal(r.mails.length, 0);
  await r.engine.events(ev({ needs_you: { count: 1, items: [item("q")] } })); // the controller pushes it again
  assert.equal(r.mails.length, 1);
});

test("the daily soft cap slows mails down but never drops one", async () => {
  const r = rig();
  let sent = 0, id = 0;
  for (let i = 0; i < SOFT_CAP; i++) {
    r.at(i * 11);
    await r.engine.events(ev({ needs_you: { count: 1, items: [item(`q${id++}`)] } }));
    sent = r.mails.length;
  }
  assert.equal(sent, SOFT_CAP);
  const t = r.clock.t;
  r.clock.t = t + 11 * MIN; await r.engine.events(ev({ needs_you: { count: 1, items: [item("late")] } }));
  assert.equal(r.mails.length, SOFT_CAP); // held
  assert.equal(r.storage.raw("s").pending.length, 1);
  r.clock.t = t + SLOW_GAP_MS + MIN; await r.beat(); await r.engine.tick();
  assert.equal(r.mails.length, SOFT_CAP + 1); // delivered later
});

test("pending overflow is folded into an 'and N more' line, not discarded", async () => {
  const r = rig({ failSend: 1000 });
  const items = (a) => Array.from({ length: 50 }, (_, i) => item(`q${a}-${i}`));
  for (let k = 0; k < 6; k++) { r.at(k); await r.engine.events(ev({ needs_you: { count: 300, items: items(k) } })); }
  const s = r.storage.raw("s");
  const queued = [...(s.outbox.needs_you?.entries || []), ...s.pending];
  assert.ok(queued.length <= 252); // 50 in the outbox, at most 200 + one folded entry pending
  assert.equal(queued.reduce((n, e) => n + (e.type === "more" ? e.n : 1), 0), 300);
});

test("compose: subjects are plain ASCII and entries are one line each", () => {
  const m = compose("failed", [{ type: "event", item: { bot: "A", kind: "auth", job: "1", reason: "x" } }], {}, T0);
  assert.match(m.subject, /^\[Bot Office\] a turn failed$/);
  assert.equal(m.text.match(/^- /gm).length, 1);
});

test("MIN_GAP_MS is ten minutes", () => assert.equal(MIN_GAP_MS, 10 * MIN));
