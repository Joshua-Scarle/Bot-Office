import test from "node:test";
import assert from "node:assert/strict";
import { sanitize, sanitizeEvents, evaluate, unjudged, buildRaw } from "../src/logic.js";

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

test("backup is not judged while the controller is down", () => {
  const p = evaluate(hb({ controller_reachable: false, healthz: null }, T0), T0, T0);
  assert.deepEqual(p.map((x) => x.kind), ["controller_down"]);
  assert.deepEqual(unjudged(p), ["backup"]);
});
test("mail has no CRLF injection and is base64 text", () => {
  const raw = buildRaw({ from: "a@b.c", to: "x@y.z\r\nBcc: e@f.g", subject: "s", text: "hello", messageId: "id", now: T0 });
  assert.ok(!/\r\nBcc:/.test(raw));
});
test("sanitizeEvents keeps allowlisted fields only and drops bodies, secrets and free text", () => {
  const ev = sanitizeEvents({
    prefs: { needs_you: false, evil: true }, backup_reason: "disk full\nBcc: x", prompt: "SECRET PROMPT", token: "tok", epoch: "e1", upto: 3,
    events: [{ seq: 1, id: "q:1:open", cls: "needs_you", bot: "Ann", kind: "question_open", ask: "SECRET PROMPT", body: "x" },
      { seq: 2, id: "bad id!", cls: "failed", bot: "x" }, null, { seq: 3, id: "j:9:1", cls: "failed", bot: "B", job: "9", kind: "auth", reason: "login needed" }],
  });
  const dump = JSON.stringify(ev);
  assert.ok(!dump.includes("SECRET") && !dump.includes("tok") && !dump.includes("evil") && !dump.includes("login") && !dump.includes("\\n"));
  assert.equal(ev.prefs.needs_you, false); assert.equal(ev.prefs.failed, true); assert.equal(ev.prefs.done, false);
  assert.deepEqual(ev.events.map((i) => i.id), ["q:1:open", "j:9:1"]);
  assert.equal(ev.upto, 3);
  assert.equal(ev.backup_reason, null);
  assert.equal(sanitizeEvents("x"), null);
});
test("a healthz without a backup object (timed out) is unjudged, not unknown", () => {
  const body = { controller_reachable: true, healthz: { ok: false, timed_out: true } };
  assert.equal(sanitize(body).backup_state, "unjudged");
  assert.deepEqual(evaluate(hb(body, T0), T0, T0).map((p) => p.kind), ["unhealthy"]);
  assert.deepEqual(unjudged([], hb(body, T0)), ["backup"]);
  assert.equal(sanitize({ controller_reachable: true, healthz: { ok: true, backup: { state: "weird" } } }).backup_state, "unknown");
});
