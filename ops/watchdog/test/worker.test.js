// Worker + Durable Object wiring with stubbed Cloudflare modules: auth, routing, and who may be mailed.
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

register("data:text/javascript," + encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  if (spec === "cloudflare:email") return { url: "data:text/javascript,export class EmailMessage { constructor(from, to, raw) { this.from = from; this.to = to; this.raw = raw; } }", shortCircuit: true };
  return next(spec, ctx);
}`));
const { default: worker, Watchdog } = await import("../src/index.js");

function env(over = {}) {
  const sent = [], store = new Map();
  const state = { storage: { async get(k) { return store.get(k); }, async put(k, v) { store.set(k, structuredClone(v)); }, async setAlarm() {} } };
  const bind = { sent };
  const conf = { ALLOWED_RECIPIENT: "you@example.test", ALERT_TO: "you@example.test", ALERT_FROM: "w@example.test", ALERT_EMAIL: { send: async (m) => sent.push(m) } };
  const dobj = new Watchdog(state, { ...conf, ...over });
  return Object.assign(bind, {
    ...conf, ...over,
    dobj,
    HEARTBEAT_SECRET: "s3cret",
    WATCHDOG: { idFromName: () => "main", get: () => ({ fetch: (url, init) => dobj.fetch(new Request(url, init)) }) },
  });
}
const post = (path, body, token = "s3cret") => new Request("https://w.example" + path, { method: "POST", headers: token ? { Authorization: `Bearer ${token}` } : {}, body: JSON.stringify(body) });

test("auth, method and path are enforced", async () => {
  const e = env();
  assert.equal((await worker.fetch(post("/events", {}, "wrong"), e)).status, 401);
  assert.equal((await worker.fetch(post("/events", {}, null), e)).status, 401);
  assert.equal((await worker.fetch(new Request("https://w.example/events"), e)).status, 404);
  assert.equal((await worker.fetch(post("/other", {}), e)).status, 404);
  assert.equal((await worker.fetch(post("/events", "x"), e)).status, 400);
  assert.equal((await worker.fetch(new Request("https://w.example/events", { method: "POST", headers: { Authorization: "Bearer s3cret" }, body: "{" }), e)).status, 400);
});

test("an authenticated event push is mailed only to the configured address", async () => {
  const e = env();
  const res = await worker.fetch(post("/events", { prefs: {}, epoch: "e1", upto: 1, events: [{ seq: 1, id: "q:1", cls: "needs_you", bot: "bot-aaaaaa", kind: "question_open", to: "evil@example.test" }], to: "evil@example.test" }), e);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ack: 1 });
  assert.equal(e.sent.length, 0); // a request never sends; the alarm does
  await e.dobj.alarm();
  assert.equal(e.sent.length, 1);
  assert.equal(e.sent[0].to, "you@example.test");
  assert.ok(!e.sent[0].raw.includes("evil"));
});

test("heartbeat then cron tick: down controller mails once", async () => {
  const e = env();
  assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false, healthz: null }), e)).status, 200);
  await worker.scheduled({}, e);
  await worker.scheduled({}, e);
  await e.dobj.alarm();
  await e.dobj.alarm();
  assert.equal(e.sent.length, 1);
});

test("missing recipient config sends nothing and the cron reports the failure only via logs", async () => {
  const e = env();
  const dobj = new Watchdog({ storage: { async get() {}, async put() {} } }, { ALERT_EMAIL: { send: async (m) => e.sent.push(m) } });
  await dobj.fetch(new Request("https://x/heartbeat", { method: "POST", body: JSON.stringify({ controller_reachable: false }) }));
  await dobj.fetch(new Request("https://x/tick", { method: "POST" }));
  await dobj.alarm();
  assert.equal(e.sent.length, 0);
});

test("the Worker refuses any recipient other than the one allowed address, at start and on every route", async () => {
  for (const to of ["evil@example.test", "you@example.test.evil.test", "you@example.test,evil@example.test", "", undefined, "x@gmail.com"]) {
    const e = env({ ALERT_TO: to });
    assert.equal((await worker.fetch(post("/events", { prefs: {}, epoch: "e1", upto: 1, events: [{ seq: 1, id: "q1", cls: "needs_you", bot: "bot-aaaaaa", kind: "x" }] }), e)).status, 503);
    assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false }), e)).status, 503);
    await assert.rejects(worker.scheduled({}, e), /misconfigured/);
    await e.dobj.alarm();
    assert.equal(e.sent.length, 0);
    assert.equal((await e.dobj.fetch(new Request("https://x/tick", { method: "POST" }))).status, 503);
  }
  assert.equal((await worker.fetch(post("/events", {}, "wrong"), env({ ALERT_TO: "evil@example.test" }))).status, 401); // auth first
  const mixed = env({ ALERT_TO: "  YOU@Example.TEST " }); // case and spaces only: still the same address
  assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false }), mixed)).status, 200);
});

test("the mail sender and the engine themselves refuse another address (defence in depth)", async () => {
  const { makeSender, Engine } = await import("../src/engine.js");
  const sent = [];
  const send = makeSender({ ALLOWED_RECIPIENT: "you@example.test", ALERT_EMAIL: { send: async (m) => sent.push(m) } }, class { constructor(f, t, r) { this.to = t; } });
  await assert.rejects(send({ from: "w@example.test", to: "evil@example.test", raw: "x" }), /refused/);
  assert.equal(sent.length, 0);
  await send({ from: "w@example.test", to: "you@example.test", raw: "x" });
  assert.equal(sent.length, 1);
  assert.throws(() => new Engine({ storage: {}, send, from: "w@example.test", to: "evil@example.test", allowed: "you@example.test" }), /allowed/);
  // no configured recipient: the sender and the engine refuse every address, including the usual one
  const bare = makeSender({ ALERT_EMAIL: { send: async (m) => sent.push(m) } }, class {});
  await assert.rejects(bare({ from: "w@example.test", to: "you@example.test", raw: "x" }), /refused/);
  assert.throws(() => new Engine({ storage: {}, send, from: "w@example.test", to: "you@example.test" }), /allowed/);
  assert.equal(sent.length, 1);
});

test("ALLOWED_RECIPIENT comes from config, must be exactly one plain address, and fails closed", async () => {
  const { allowedRecipient, configError } = await import("../src/logic.js");
  assert.equal(allowedRecipient({ ALLOWED_RECIPIENT: "  You@Example.TEST " }), "you@example.test");
  for (const bad of [undefined, "", "   ", "you@example.test,evil@example.test", "you@example.test evil@example.test", "a; b@example.test", "not-an-address", 42]) {
    assert.equal(allowedRecipient({ ALLOWED_RECIPIENT: bad }), null);
    const e = env({ ALLOWED_RECIPIENT: bad });
    assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false }), e)).status, 503);
    await assert.rejects(worker.scheduled({}, e), /misconfigured/);
    await e.dobj.alarm();
    assert.equal(e.sent.length, 0);
  }
  assert.match(configError({ ALERT_TO: "you@example.test", ALERT_FROM: "w@example.test" }), /ALLOWED_RECIPIENT/);
  // another configured address works the same way: the recipient is config, not code
  const other = env({ ALLOWED_RECIPIENT: "ops@example.test", ALERT_TO: "ops@example.test" });
  assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false }), other)).status, 200);
  assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false }), env({ ALLOWED_RECIPIENT: "ops@example.test" }))).status, 503);
  // configError never echoes a configured value
  assert.ok(!/example\.test/.test(configError({ ALLOWED_RECIPIENT: "a@example.test", ALERT_TO: "b@example.test" })));
});

test("/status needs the secret and reports counts only", async () => {
  const e = env();
  assert.equal((await worker.fetch(post("/status", {}, "wrong"), e)).status, 401);
  assert.equal((await worker.fetch(new Request("https://w.example/status"), e)).status, 404);
  await worker.fetch(post("/events", { prefs: {}, epoch: "e1", upto: 1, events: [{ seq: 1, id: "q:1", cls: "needs_you", bot: "bot-aaaaaa", kind: "question_open" }] }), e);
  const res = await worker.fetch(post("/status", {}), e);
  assert.deepEqual(await res.json(), { pending: 1, outbox: 0 });
  await e.dobj.alarm();
  assert.deepEqual(await (await worker.fetch(post("/status", {}), e)).json(), { pending: 0, outbox: 0 });
});
