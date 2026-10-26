// The app's side of the Claude connector: Vigil uploads its latest data (so Claude can read it through the connector)
// and collects what Claude left for it (the agent desk's calls and research news checks).
//   PUT    /api/claude/feed     body: the app's latest data (JSON)        -> { at }
//   GET    /api/claude/feed     -> { at, bytes } (when the data was last sent; not the data)
//   GET    /api/claude/inbox    -> { items: [{ id, at, type, payload }] }
//   DELETE /api/claude/inbox    body: { ids: [...] }                       -> { deleted }
//   DELETE /api/claude/all      -> wipes this token's data (used when the token is replaced or the link removed)
// Every call needs "Authorization: Bearer <connector token>".
import { getStore } from "@netlify/blobs";
import { MAX_FEED, MAX_SPACES, json, seal, spaceCount, spaceId, tokenFrom, unseal } from "../lib/vigil.mjs";

export async function handle(req, store) {
  const token = tokenFrom(req);
  if (!token) return json(401, { error: "missing or malformed connector token" });
  const id = spaceId(token);
  const part = new URL(req.url).pathname.replace(/\/+$/, "").split("/").pop();
  if (part === "feed" && req.method === "PUT") {
    const text = await req.text();
    if (text.length > MAX_FEED) return json(413, { error: "too large" });
    let feed;
    try { feed = JSON.parse(text); } catch (e) { return json(400, { error: "not JSON" }); }
    if (!feed || typeof feed !== "object" || Array.isArray(feed)) return json(400, { error: "the feed is an object" });
    const exists = await store.get("feed/" + id, { type: "text" });
    if (!exists && (await spaceCount(store)) >= MAX_SPACES) return json(507, { error: "this site already serves the most connector tokens it allows" });
    const at = Date.now();
    await store.set("feed/" + id, seal(token, { at, feed }), { metadata: { at, bytes: text.length } });
    return json(200, { at });
  }
  if (part === "feed" && req.method === "GET") {
    const m = await store.getMetadata("feed/" + id);
    return m ? json(200, { at: (m.metadata || {}).at || 0, bytes: (m.metadata || {}).bytes || 0 }) : json(404, { error: "no data sent yet" });
  }
  if (part === "inbox" && req.method === "GET") {
    const list = await store.list({ prefix: "inbox/" + id + "/" });
    const items = [];
    for (const b of (list.blobs || []).slice(0, 100)) {
      const text = await store.get(b.key, { type: "text" });
      if (!text) continue;
      try { items.push({ id: b.key.split("/").pop(), ...unseal(token, text) }); } catch (e) { await store.delete(b.key); }
    }
    items.sort((a, b) => (a.at || 0) - (b.at || 0));
    return json(200, { items });
  }
  if (part === "inbox" && req.method === "DELETE") {
    let body;
    try { body = JSON.parse(await req.text()); } catch (e) { return json(400, { error: "not JSON" }); }
    const ids = Array.isArray(body.ids) ? body.ids.filter((x) => typeof x === "string" && /^[a-z0-9]+-[0-9a-f]{8}$/.test(x)).slice(0, 200) : [];
    for (const k of ids) await store.delete("inbox/" + id + "/" + k);
    return json(200, { deleted: ids.length });
  }
  if (part === "all" && req.method === "DELETE") {
    await store.delete("feed/" + id);
    const list = await store.list({ prefix: "inbox/" + id + "/" });
    for (const b of list.blobs || []) await store.delete(b.key);
    return json(200, { deleted: true });
  }
  return json(405, { error: "unknown call" });
}

export default async (req) => handle(req, getStore({ name: "vigil-claude", consistency: "strong" }));

export const config = { path: "/api/claude/*" };
