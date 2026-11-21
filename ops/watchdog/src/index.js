// Bot Office watchdog. The Worker authenticates; one Durable Object ("main") owns all state and mail.
import { EmailMessage } from "cloudflare:email";
import { sanitize, sanitizeEvents } from "./logic.js";
import { Engine, makeSender } from "./engine.js";

const MAX_BODY = 32 * 1024;

async function digest(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}
// compare fixed-length digests so neither content nor length is leaked
async function authorized(request, secret) {
  if (!secret) return false;
  const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  const a = new Uint8Array(await digest(given)), b = new Uint8Array(await digest(secret));
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

const ROUTES = { "/heartbeat": sanitize, "/events": sanitizeEvents };

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method !== "POST" || !ROUTES[path]) return new Response("not found", { status: 404 });
    if (!(await authorized(request, env.HEARTBEAT_SECRET))) return new Response("unauthorized", { status: 401 });
    const text = await request.text();
    if (text.length > MAX_BODY) return new Response("too large", { status: 413 });
    let data = null;
    try {
      data = ROUTES[path](JSON.parse(text));
    } catch {
      data = null;
    }
    if (!data) return new Response("bad request", { status: 400 });
    return stub(env).fetch("https://watchdog.internal" + path, { method: "POST", body: JSON.stringify(data) });
  },

  async scheduled(_event, env) {
    const res = await stub(env).fetch("https://watchdog.internal/tick", { method: "POST" });
    if (!res.ok) throw new Error("tick failed: HTTP " + res.status); // surfaces in the cron's error log
  },
};

const stub = (env) => env.WATCHDOG.get(env.WATCHDOG.idFromName("main"));

export class Watchdog {
  constructor(state, env) {
    this.engine = new Engine({ storage: state.storage, send: makeSender(env, EmailMessage) });
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    try {
      if (path === "/heartbeat") await this.engine.heartbeat(await request.json());
      else if (path === "/events") await this.engine.events(await request.json());
      else if (path === "/tick") await this.engine.tick();
      else return new Response("not found", { status: 404 });
      return new Response("ok");
    } catch (err) {
      console.log("watchdog error:", err && err.name);
      return new Response("error", { status: 500 });
    }
  }
}
