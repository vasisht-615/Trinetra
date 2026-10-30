// Trinetra's watchdog, run by Netlify every 2 minutes: reminders before the big US releases and the SOL Fed rule's
// first-hour read, pushed to your phone with ntfy even when the app is closed (the logic is in ../lib/watchdog.mjs).
// Set NTFY_TOPIC in Netlify (Site configuration > Environment variables) to a long random name and subscribe to the
// same topic in the ntfy app; without it this function does nothing. The topic name works as a password: keep it private.
import { getStore } from "@netlify/blobs";
import { runWatchdog } from "../lib/watchdog.mjs";

const CAL_TTL = 6 * 3600000;

// The official US schedule the app ships (site/engine/references), cached for 6 hours.
async function releases(store, site, now) {
  const cached = await store.get("calendar", { type: "json" });
  if (cached && now - (cached.at || 0) < CAL_TTL && Array.isArray(cached.releases)) return cached.releases;
  try {
    const r = await fetch(site.replace(/\/+$/, "") + "/engine/references/release_calendar.json", { signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error("calendar answered " + r.status);
    const cal = await r.json();
    if (!Array.isArray(cal.releases)) throw new Error("calendar has no releases");
    await store.setJSON("calendar", { at: now, releases: cal.releases });
    return cal.releases;
  } catch (e) {
    console.log("watchdog: " + String((e && e.message) || e).slice(0, 200));
    return (cached && cached.releases) || [];
  }
}

export default async (req, context) => {
  const topic = process.env.NTFY_TOPIC;
  if (!topic) {
    console.log("watchdog: NTFY_TOPIC is not set, so there is nowhere to send alerts");
    return new Response("no topic");
  }
  const site = (context && context.site && context.site.url) || process.env.URL || "";
  const store = getStore({ name: "trinetra-watchdog", consistency: "strong" });
  const now = Date.now();
  const sent = await runWatchdog({ now, fetchFn: fetch, store, topic, server: process.env.NTFY_SERVER, site,
    releases: await releases(store, site, now) });
  if (sent.length) console.log("watchdog: sent " + sent.join(", "));
  return new Response("ok");
};

export const config = { schedule: "*/2 * * * *" };
