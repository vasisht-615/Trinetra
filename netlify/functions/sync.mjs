// DESK sync: keeps one encrypted copy of your data per sync code, so every device with the code sees the same settings
// and record. The browser encrypts before sending (AES-GCM, key made from the sync code), so this function and Netlify
// only ever store scrambled bytes. The code itself never reaches the server: only a one-way hash of it, used as the name.
import { getStore } from "@netlify/blobs";

const MAX_BYTES = 3 * 1024 * 1024;     // a year of heavy use is well under 1 MB
const MAX_SPACES = 20;                 // this site serves you (and anyone you share it with), not the whole internet
const ID = /^[0-9a-f]{64}$/;

const json = (status, body) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});

export async function handle(req, store) {
  const url = new URL(req.url);
  const id = url.searchParams.get("id") || "";
  if (!ID.test(id)) return json(400, { error: "bad id" });
  const key = "space/" + id;
  if (req.method === "GET") {
    const got = await store.getWithMetadata(key, { type: "text" });
    if (!got) return json(404, { error: "no data yet" });
    return json(200, { version: (got.metadata || {}).version || 0, at: (got.metadata || {}).at || 0, blob: got.data });
  }
  if (req.method === "PUT") {
    const text = await req.text();
    if (text.length > MAX_BYTES) return json(413, { error: "too large" });
    let body;
    try { body = JSON.parse(text); } catch (e) { return json(400, { error: "not JSON" }); }
    if (typeof body.blob !== "string" || !Number.isInteger(body.base)) return json(400, { error: "needs blob and base" });
    const cur = await store.getMetadata(key);
    const curVersion = cur ? (cur.metadata || {}).version || 0 : 0;
    if (!cur) {
      const all = await store.list({ prefix: "space/" });
      if ((all.blobs || []).length >= MAX_SPACES) return json(507, { error: "this site already holds the most sync spaces it allows" });
    }
    // Compare-and-set: a device that has not seen the latest copy must fetch, merge and send again.
    if (body.base !== curVersion) return json(409, { error: "newer data exists", version: curVersion });
    const version = curVersion + 1, at = Date.now();
    await store.set(key, body.blob, { metadata: { version, at } });
    return json(200, { version, at });
  }
  if (req.method === "DELETE") {
    await store.delete(key);
    return json(200, { deleted: true });
  }
  return json(405, { error: "GET, PUT or DELETE" });
}

export default async (req) => handle(req, getStore({ name: "desk-sync", consistency: "strong" }));

export const config = { path: "/api/sync" };
