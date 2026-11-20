// Bot Office watchdog: receives heartbeats, checks them on a cron, mails on trouble.
import { EmailMessage } from "cloudflare:email";
import { sanitize, evaluate, step, composeMail, buildRaw } from "./logic.js";

const MAX_BODY = 16 * 1024;

function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a), y = enc.encode(b);
  // compare against a fixed-length digest so length is not leaked either
  return crypto.subtle.digest("SHA-256", x).then(async (hx) => {
    const hy = await crypto.subtle.digest("SHA-256", y);
    return crypto.subtle.timingSafeEqual(hx, hy);
  });
}

async function sendMail(env, send, now) {
  const { subject, text } = composeMail(send, now);
  const raw = buildRaw({ from: env.ALERT_FROM, to: env.ALERT_TO, subject, text, messageId: crypto.randomUUID(), now });
  await env.ALERT_EMAIL.send(new EmailMessage(env.ALERT_FROM, env.ALERT_TO, raw));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/heartbeat") return new Response("not found", { status: 404 });
    const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!env.HEARTBEAT_SECRET || !(await timingSafeEqual(given, env.HEARTBEAT_SECRET))) {
      return new Response("unauthorized", { status: 401 });
    }
    const text = await request.text();
    if (text.length > MAX_BODY) return new Response("too large", { status: 413 });
    let data;
    try {
      data = sanitize(JSON.parse(text));
    } catch {
      data = null;
    }
    if (!data) return new Response("bad request", { status: 400 });
    await env.WATCHDOG_KV.put("heartbeat", JSON.stringify({ received_at: Date.now(), data }));
    return new Response("ok");
  },

  async scheduled(_event, env) {
    const now = Date.now();
    const hb = JSON.parse((await env.WATCHDOG_KV.get("heartbeat")) || "null");
    let firstSeen = Number(await env.WATCHDOG_KV.get("first_seen")) || null;
    if (!firstSeen) {
      firstSeen = now;
      await env.WATCHDOG_KV.put("first_seen", String(now));
    }
    const prev = JSON.parse((await env.WATCHDOG_KV.get("incident")) || "null");
    const { state, send } = step(prev, evaluate(hb, now, firstSeen), now);
    if (send) {
      try {
        await sendMail(env, send, now);
      } catch (err) {
        // roll back so the next cron retries; log the error class only
        console.log("mail failed:", err && err.name);
        state.mails.pop();
        if (send.type === "alert") state.notified = false;
        else {
          state.open = true;
          state.notified = true;
          state.since = send.since;
        }
      }
    }
    if (JSON.stringify(state) !== JSON.stringify(prev)) await env.WATCHDOG_KV.put("incident", JSON.stringify(state));
  },
};
