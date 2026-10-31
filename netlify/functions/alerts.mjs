// Trinetra's phone alerts, the two things the app and the alerts themselves call:
//   POST /api/alerts/test  sends a test alert to every channel that is set up (at most one every 2 minutes)
//   POST /api/alerts/ack   "I'm on it" from a Fed-basket alert: its one repeat is not sent (the link is signed with the
//                          alert secret, so only your own alert can stop it)
// The channels are the same as the watchdog's: NTFY_TOPIC (and NTFY_SERVER), and TELEGRAM_BOT_TOKEN with TELEGRAM_CHAT_ID.
import { getStore } from "@netlify/blobs";
import { ackSig, send, testMessage } from "../lib/watchdog.mjs";

const TEST_GAP = 2 * 60000;
const KEY = /^basket:\d{10,16}$/;

const json = (status, body) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});

export function channels(env) {
  const telegram = env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID ? { token: env.TELEGRAM_BOT_TOKEN, chat: env.TELEGRAM_CHAT_ID } : null;
  return { topic: env.NTFY_TOPIC || "", server: env.NTFY_SERVER, telegram };
}

export async function handle(req, { store, env, fetchFn = fetch, now = Date.now() }) {
  if (req.method !== "POST") return json(405, { error: "POST only" });
  const url = new URL(req.url);
  const ch = channels(env);
  const secret = ch.topic || (ch.telegram && ch.telegram.token) || "";
  if (!secret) return json(503, { error: "Phone alerts are not set up on this site yet: add NTFY_TOPIC in Netlify (Site configuration, Environment variables), then try again." });
  if (url.pathname.endsWith("/ack")) {
    const k = url.searchParams.get("k") || "", sig = url.searchParams.get("s") || "";
    if (!KEY.test(k) || sig !== ackSig(secret, k)) return json(403, { error: "not a link from your own alert" });
    await store.set("ack/" + k, JSON.stringify({ at: now }));
    return json(200, { ok: true });
  }
  if (url.pathname.endsWith("/test")) {
    const last = Number(await store.get("test-at")) || 0;
    if (now - last < TEST_GAP) return json(429, { error: "one test every 2 minutes" });
    await store.set("test-at", String(now));
    try {
      return json(200, { ok: true, channels: await send(fetchFn, ch, testMessage(now)) });
    } catch (e) {
      return json(502, { error: "The alert service refused it: " + String((e && e.message) || e).slice(0, 200) });
    }
  }
  return json(404, { error: "no such route" });
}

export default async (req) => handle(req, { store: getStore({ name: "trinetra-watchdog", consistency: "strong" }), env: process.env });

export const config = { path: ["/api/alerts/test", "/api/alerts/ack"] };
