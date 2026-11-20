import test from "node:test";
import assert from "node:assert/strict";
import { sanitize, evaluate, step, composeMail, buildRaw, MIN_ALERT_GAP_MS } from "../src/logic.js";

const MIN = 60000, T0 = 1_800_000_000_000;
const good = { sent_at: "x", controller_reachable: true, healthz: { ok: true, version: "1", scheduler: { alive: true }, db_reachable: true, running_jobs: 0, disk_free_pct: 50, backup: { state: "ok", age_hours: 3 } } };
const hb = (body, at) => ({ received_at: at, data: sanitize(body) });

test("healthy -> no problems", () => assert.deepEqual(evaluate(hb(good, T0), T0 + MIN, T0), []));
test("stale heartbeat", () => assert.equal(evaluate(hb(good, T0), T0 + 16 * MIN, T0)[0].kind, "heartbeat_missing"));
test("never any heartbeat waits 15 min", () => {
  assert.deepEqual(evaluate(null, T0 + 10 * MIN, T0), []);
  assert.equal(evaluate(null, T0 + 16 * MIN, T0)[0].kind, "heartbeat_missing");
});
test("ok=false", () => {
  const b = structuredClone(good); b.healthz.ok = false; b.healthz.db_reachable = false;
  const p = evaluate(hb(b, T0), T0, T0);
  assert.equal(p[0].kind, "unhealthy"); assert.match(p[0].text, /database/);
});
test("controller down push", () => assert.equal(evaluate(hb({ controller_reachable: false, healthz: null }, T0), T0, T0)[0].kind, "controller_down"));
test("backup failed / stale / ok boundary", () => {
  const mk = (state, age) => { const b = structuredClone(good); b.healthz.backup = { state, age_hours: age }; return evaluate(hb(b, T0), T0, T0).map((p) => p.kind); };
  assert.deepEqual(mk("failed", 1), ["backup_state"]);
  assert.deepEqual(mk("ok", 31), ["backup_stale"]);
  assert.deepEqual(mk("ok", 30), []);
  assert.deepEqual(mk("ok", null), ["backup_stale"]);
});
test("sanitize drops unknown fields", () => {
  const d = sanitize({ ...good, token: "s3cret", healthz: { ...good.healthz, path: "/x" } });
  assert.ok(!JSON.stringify(d).includes("s3cret") && !JSON.stringify(d).includes("/x"));
});

test("one alert per incident, one recovery", () => {
  const bad = [{ kind: "k", text: "t" }];
  let r = step(null, bad, T0); assert.equal(r.send.type, "alert");
  r = step(r.state, bad, T0 + 5 * MIN); assert.equal(r.send, null);
  r = step(r.state, bad, T0 + 600 * MIN); assert.equal(r.send, null);
  r = step(r.state, [], T0 + 605 * MIN); assert.equal(r.send.type, "recovery");
  r = step(r.state, [], T0 + 610 * MIN); assert.equal(r.send, null);
});
test("flap: second alert waits for the gap; unnotified recovery is silent", () => {
  const bad = [{ kind: "k", text: "t" }];
  let r = step(null, bad, T0);
  r = step(r.state, [], T0 + 2 * MIN); assert.equal(r.send.type, "recovery");
  r = step(r.state, bad, T0 + 3 * MIN); assert.equal(r.send, null); assert.equal(r.state.notified, false);
  r = step(r.state, [], T0 + 4 * MIN); assert.equal(r.send, null); // never alerted -> no recovery mail
  r = step(r.state, bad, T0 + 4 * MIN + MIN_ALERT_GAP_MS + 2 * MIN); assert.equal(r.send.type, "alert");
});
test("daily cap", () => {
  const bad = [{ kind: "k", text: "t" }];
  let st = null, sent = 0, t = T0;
  for (let i = 0; i < 20; i++) {
    let r = step(st, bad, t); sent += r.send ? 1 : 0; t += 11 * MIN;
    r = step(r.state, [], t); sent += r.send ? 1 : 0; t += 11 * MIN; st = r.state;
  }
  assert.ok(sent <= 6);
});
test("mail has no CRLF injection and is base64 text", () => {
  const raw = buildRaw({ from: "a@b.c", to: "x@y.z\r\nBcc: e@f.g", subject: "s", text: "hello", messageId: "id", now: T0 });
  assert.ok(!/\r\nBcc:/.test(raw));
  assert.equal(composeMail({ type: "recovery", since: T0 }, T0 + 5 * MIN).subject, "[Bot Office] RECOVERED");
});
