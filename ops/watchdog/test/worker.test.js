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

function env() {
  const sent = [], store = new Map();
  const state = { storage: { async get(k) { return store.get(k); }, async put(k, v) { store.set(k, structuredClone(v)); } } };
  const bind = { sent };
  const dobj = new Watchdog(state, { ALERT_TO: "me@example.test", ALERT_FROM: "w@example.test", ALERT_EMAIL: { send: async (m) => sent.push(m) } });
  return Object.assign(bind, {
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
  const res = await worker.fetch(post("/events", { prefs: {}, needs_you: { count: 1, items: [{ id: "q:1", bot: "Ann", kind: "question open", to: "evil@example.test" }] }, to: "evil@example.test" }), e);
  assert.equal(res.status, 200);
  assert.equal(e.sent.length, 1);
  assert.equal(e.sent[0].to, "me@example.test");
  assert.ok(!e.sent[0].raw.includes("evil"));
});

test("heartbeat then cron tick: down controller mails once", async () => {
  const e = env();
  assert.equal((await worker.fetch(post("/heartbeat", { controller_reachable: false, healthz: null }), e)).status, 200);
  await worker.scheduled({}, e);
  await worker.scheduled({}, e);
  assert.equal(e.sent.length, 1);
});

test("missing recipient config sends nothing and the cron reports the failure only via logs", async () => {
  const e = env();
  const dobj = new Watchdog({ storage: { async get() {}, async put() {} } }, { ALERT_EMAIL: { send: async (m) => e.sent.push(m) } });
  await dobj.fetch(new Request("https://x/heartbeat", { method: "POST", body: JSON.stringify({ controller_reachable: false }) }));
  await dobj.fetch(new Request("https://x/tick", { method: "POST" }));
  assert.equal(e.sent.length, 0);
});
