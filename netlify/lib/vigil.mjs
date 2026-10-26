// Shared by the Claude connector (mcp.mjs) and the app's feed/inbox endpoints (claude.mjs).
// One connector token links your Vigil app and your Claude account. The server stores the app's latest data and
// Claude's notes for the app encrypted at rest with a key made from that token, under a one-way hash of it, so the
// stored blobs are unreadable without the token. (The token itself reaches the server on each request, by design.)
import { createCipheriv, createDecipheriv, createHash, hkdfSync, randomBytes } from "node:crypto";

export const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;
export const MAX_FEED = 1_500_000;      // bytes of JSON the app may store (a day's data is ~100 kB)
export const MAX_INBOX = 200;           // items Claude may leave before the app collects them
export const MAX_SPACES = 20;           // connector tokens this site serves

export function tokenFrom(req) {
  const m = (req.headers.get("authorization") || "").match(/^Bearer\s+(\S+)\s*$/i);
  return m && TOKEN.test(m[1]) ? m[1] : null;
}

export const spaceId = (token) => createHash("sha256").update("vigil-claude-id:" + token).digest("hex");
const keyOf = (token) => Buffer.from(hkdfSync("sha256", Buffer.from(token), Buffer.from("vigil-claude-v1"), Buffer.from("aes-gcm"), 32));

export function seal(token, obj) {
  const iv = randomBytes(12);
  const c = createCipheriv("aes-256-gcm", keyOf(token), iv);
  const ct = Buffer.concat([c.update(JSON.stringify(obj), "utf8"), c.final()]);
  return JSON.stringify({ v: 1, iv: iv.toString("base64"), ct: ct.toString("base64"), tag: c.getAuthTag().toString("base64") });
}

export function unseal(token, text) {
  const o = JSON.parse(text);
  const d = createDecipheriv("aes-256-gcm", keyOf(token), Buffer.from(o.iv, "base64"));
  d.setAuthTag(Buffer.from(o.tag, "base64"));
  return JSON.parse(Buffer.concat([d.update(Buffer.from(o.ct, "base64")), d.final()]).toString("utf8"));
}

export const json = (status, body, extra) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...(extra || {}) },
});

export async function readFeed(store, token) {
  const text = await store.get("feed/" + spaceId(token), { type: "text" });
  if (!text) return null;
  try { return unseal(token, text); } catch (e) { return null; }    // a different token wrote it: treat as none
}

export async function addInbox(store, token, item) {
  const id = spaceId(token);
  const have = await store.list({ prefix: "inbox/" + id + "/" });
  if ((have.blobs || []).length >= MAX_INBOX) return { ok: false, error: "Vigil's inbox is full: open Vigil so it collects what is waiting" };
  const key = "inbox/" + id + "/" + Date.now().toString(36) + "-" + randomBytes(4).toString("hex");
  await store.set(key, seal(token, { at: Date.now(), ...item }));
  return { ok: true, key };
}

export async function spaceCount(store) {
  const all = await store.list({ prefix: "feed/" });
  return (all.blobs || []).length;
}
