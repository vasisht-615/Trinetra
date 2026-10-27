// Trinetra pairing: links a new device with a short code, for devices that cannot open a sync link themselves (an
// iPhone or iPad Home Screen app has its own storage, apart from Safari, so a QR code scanned with the camera opens
// Safari, not the app). The device that already syncs makes an 8-character code, valid 10 minutes, and leaves its sync
// code here encrypted with a key made from the short code (PBKDF2, 210,000 rounds), under an id made the same way. The
// new device types the code, fetches the blob once (it is deleted on the first read) and opens it. Neither code reaches
// this server.
import { getStore } from "@netlify/blobs";

const ID = /^[0-9a-f]{64}$/;
const TTL = 10 * 60000;
const MAX_WAITING = 30;            // codes waiting at one time on this site
const MAX_BLOB = 4000;             // an encrypted 26-character sync code is about 120 bytes

const json = (status, body) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});

export async function handle(req, store, now = Date.now()) {
  const url = new URL(req.url);
  if (req.method === "POST") {
    let body;
    try { body = JSON.parse(await req.text()); } catch (e) { return json(400, { error: "not JSON" }); }
    if (!body || !ID.test(String(body.id || "")) || typeof body.blob !== "string" || !body.blob || body.blob.length > MAX_BLOB) {
      return json(400, { error: "needs id and blob" });
    }
    // expired codes are removed whenever a new one is made, so the store never fills up
    const all = await store.list({ prefix: "pair/" });
    let waiting = 0;
    for (const b of all.blobs || []) {
      const m = await store.getMetadata(b.key);
      if (!m || now > Number((m.metadata || {}).exp || 0)) await store.delete(b.key);
      else waiting += 1;
    }
    if (waiting >= MAX_WAITING) return json(507, { error: "too many codes are waiting: try again in 10 minutes" });
    const key = "pair/" + body.id;
    if (await store.getMetadata(key)) return json(409, { error: "that code is already waiting: make a new one" });
    const exp = now + TTL;
    await store.set(key, body.blob, { metadata: { exp } });
    return json(200, { expires: exp });
  }
  const id = url.searchParams.get("id") || "";
  if (!ID.test(id)) return json(400, { error: "bad id" });
  const key = "pair/" + id;
  if (req.method === "GET") {
    const got = await store.getWithMetadata(key, { type: "text" });
    if (!got) return json(404, { error: "no such code: check the letters, or make a new code (each lasts 10 minutes and works once)" });
    await store.delete(key);                                   // one use only
    if (now > Number((got.metadata || {}).exp || 0)) return json(410, { error: "that code has expired: make a new one" });
    return json(200, { blob: got.data });
  }
  if (req.method === "DELETE") {
    await store.delete(key);
    return json(200, { deleted: true });
  }
  return json(405, { error: "POST, GET or DELETE" });
}

export default async (req) => handle(req, getStore({ name: "desk-pair", consistency: "strong" }));

export const config = { path: "/api/pair" };
