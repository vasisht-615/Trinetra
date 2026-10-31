// The Netlify functions against an in-memory blob store: sync, device pairing, the app's Claude feed and inbox, the Claude
// connector (MCP) and the news proxy. Run with: npm ci && node --test tests/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { handle as sync } from "../netlify/functions/sync.mjs";
import { handle as pair } from "../netlify/functions/pair.mjs";
import { handle as claude } from "../netlify/functions/claude.mjs";
import { handle as mcp } from "../netlify/functions/mcp.mjs";
import { handle as feed } from "../netlify/functions/feed.mjs";

// the parts of @netlify/blobs' store the functions use
function memStore() {
  const m = new Map();
  return {
    m,
    async get(k) { return m.has(k) ? m.get(k).data : null; },
    async getWithMetadata(k) { return m.has(k) ? { data: m.get(k).data, metadata: m.get(k).metadata || {} } : null; },
    async getMetadata(k) { return m.has(k) ? { metadata: m.get(k).metadata || {} } : null; },
    async set(k, data, opts = {}) { m.set(k, { data, metadata: opts.metadata || {} }); },
    async delete(k) { m.delete(k); },
    async list({ prefix = "" } = {}) { return { blobs: [...m.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })) }; },
  };
}
const ID = "a".repeat(64), ID2 = "b".repeat(64);
const req = (method, url, body, headers = {}) => new Request("https://x.netlify.app" + url, { method, headers, body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body) });
const body = async (r) => JSON.parse(await r.text());

test("sync: compare-and-set versions, conflicts, limits and bad input", async () => {
  const s = memStore();
  assert.equal((await sync(req("GET", "/api/sync?id=nothex"), s)).status, 400);
  assert.equal((await sync(req("GET", "/api/sync?id=" + ID), s)).status, 404);
  let r = await sync(req("PUT", "/api/sync?id=" + ID, { blob: "enc1", base: 0 }), s);
  assert.equal(r.status, 200); assert.equal((await body(r)).version, 1);
  r = await sync(req("PUT", "/api/sync?id=" + ID, { blob: "stale", base: 0 }), s);        // a device that missed version 1
  assert.equal(r.status, 409); assert.equal((await body(r)).version, 1);
  r = await sync(req("PUT", "/api/sync?id=" + ID, { blob: "enc2", base: 1 }), s);
  assert.equal((await body(r)).version, 2);
  r = await sync(req("GET", "/api/sync?id=" + ID), s);
  assert.equal((await body(r)).blob, "enc2");
  assert.equal((await sync(req("PUT", "/api/sync?id=" + ID, "not json"), s)).status, 400);
  assert.equal((await sync(req("PUT", "/api/sync?id=" + ID, { blob: 5, base: 2 }), s)).status, 400);
  assert.equal((await sync(req("PUT", "/api/sync?id=" + ID, { blob: "x".repeat(3 * 1024 * 1024 + 10), base: 2 }), s)).status, 413);
  for (let i = 0; i < 19; i++) await sync(req("PUT", "/api/sync?id=" + String(i).padStart(64, "c"), { blob: "e", base: 0 }), s);
  assert.equal((await sync(req("PUT", "/api/sync?id=" + ID2, { blob: "e", base: 0 }), s)).status, 507);   // 20 spaces at most
  assert.equal((await sync(req("DELETE", "/api/sync?id=" + ID), s)).status, 200);
  assert.equal((await sync(req("GET", "/api/sync?id=" + ID), s)).status, 404);
  assert.equal((await sync(req("POST", "/api/sync?id=" + ID), s)).status, 405);
});

test("pair: a code works once, expires after 10 minutes, and can't be reused while waiting", async () => {
  const s = memStore(), t0 = Date.UTC(2026, 9, 30, 12, 0);
  assert.equal((await pair(req("POST", "/api/pair", { id: ID, blob: "sealed" }), s, t0)).status, 200);
  assert.equal((await pair(req("POST", "/api/pair", { id: ID, blob: "again" }), s, t0)).status, 409);
  let r = await pair(req("GET", "/api/pair?id=" + ID), s, t0 + 60000);
  assert.equal(r.status, 200); assert.equal((await body(r)).blob, "sealed");
  assert.equal((await pair(req("GET", "/api/pair?id=" + ID), s, t0 + 61000)).status, 404);        // one use only
  await pair(req("POST", "/api/pair", { id: ID2, blob: "sealed" }), s, t0);
  assert.equal((await pair(req("GET", "/api/pair?id=" + ID2), s, t0 + 11 * 60000)).status, 410);  // expired
  assert.equal((await pair(req("POST", "/api/pair", { id: "short", blob: "x" }), s, t0)).status, 400);
  assert.equal((await pair(req("POST", "/api/pair", { id: ID, blob: "x".repeat(4001) }), s, t0)).status, 400);
  // expired codes are cleared when new ones are made, so 30 old codes never block a new one
  for (let i = 0; i < 30; i++) await pair(req("POST", "/api/pair", { id: String(i).padStart(64, "d"), blob: "e" }), s, t0);
  assert.equal((await pair(req("POST", "/api/pair", { id: "e".repeat(64), blob: "e" }), s, t0 + 1000)).status, 507);
  assert.equal((await pair(req("POST", "/api/pair", { id: "e".repeat(64), blob: "e" }), s, t0 + 11 * 60000)).status, 200);
});

const TOKEN = "T".repeat(40), AUTH = { Authorization: "Bearer " + TOKEN };
const FEED = { v: 1, checked_at: Date.now(), engine: "1.4.0", text: "DESK TODAY ...", settings: { watchlist: ["BTC", "ETH", "SOL"] },
  today: { coins: [{ symbol: "SOLUSDT", decision: "SIGNAL LONG" }, { symbol: "ETHUSDT", decision: "SIGNAL LONG", tier: "small" }],
    signals: [{ symbol: "SOLUSDT", decision: "SIGNAL LONG", candidate: { setup: "momentum_after_60m", side: "long", entry: 150, stop: 146.25, target1: 155.6 }, plan: { qty: 0.09, notional: 13.5, leverage: 1, max_loss_at_stop: 0.26 } },
      { symbol: "ETHUSDT", decision: "SIGNAL LONG", tier: "small", candidate: { setup: "momentum_after_60m", side: "long", entry: 2200, stop: 2156, target1: 2266 }, plan: { qty: 0.003 } }] },
  packets: { SOL: "DESK PACKET SOL" }, upcoming: [], record: "Engine signals: none", health: [] };

test("claude feed: the app's data is stored sealed under a hash of the token, and only that token reads it", async () => {
  const s = memStore();
  assert.equal((await claude(req("PUT", "/api/claude/feed", FEED), s)).status, 401);
  assert.equal((await claude(req("PUT", "/api/claude/feed", FEED, AUTH), s)).status, 200);
  const stored = [...s.m.values()][0].data;
  assert.ok(!stored.includes("DESK PACKET"), "the feed must be stored encrypted");
  assert.equal((await claude(req("GET", "/api/claude/feed", undefined, AUTH), s)).status, 200);
  assert.equal((await claude(req("PUT", "/api/claude/feed", "[1,2]", AUTH), s)).status, 400);
  assert.equal((await claude(req("GET", "/api/claude/inbox", undefined, AUTH), s)).status, 200);
  assert.equal((await claude(req("DELETE", "/api/claude/all", undefined, AUTH), s)).status, 200);
  assert.equal(s.m.size, 0);
});

test("connector: tools list, status and today label a small signal, a wrong token and bad JSON-RPC are refused", async () => {
  const s = memStore();
  await claude(req("PUT", "/api/claude/feed", FEED, AUTH), s);
  const rpc = async (m, auth = AUTH) => mcp(req("POST", "/api/mcp", m, auth), s);
  let r = await rpc({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } });
  assert.equal((await body(r)).result.protocolVersion, "2025-06-18");
  r = await rpc({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  assert.ok((await body(r)).result.tools.some((t) => t.name === "vigil_today"));
  r = await rpc({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "vigil_status", arguments: {} } });
  assert.match((await body(r)).result.content[0].text, /Signals now: SOL SIGNAL LONG, ETH SIGNAL LONG \(small\)/);
  r = await rpc({ jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "vigil_today", arguments: {} } });
  const today = (await body(r)).result.content[0].text;
  assert.match(today, /^SIGNAL SOL LONG/m);
  assert.match(today, /SMALL SIGNAL \(positive but unproven.*\) ETH LONG/);
  r = await rpc({ jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "vigil_coin", arguments: { symbol: "SOLUSDT" } } });
  assert.match((await body(r)).result.content[0].text, /DESK PACKET SOL/);
  r = await rpc({ jsonrpc: "2.0", id: 6, method: "tools/call", params: { name: "no_such_tool" } });
  assert.equal((await body(r)).error.code, -32602);
  assert.equal((await rpc({ jsonrpc: "2.0", id: 7, method: "tools/list" }, { Authorization: "Bearer short" })).status, 401);
  assert.equal((await mcp(req("POST", "/api/mcp/" + TOKEN, { jsonrpc: "2.0", id: 8, method: "ping" }), s)).status, 200);   // token in the address
  assert.equal((await rpc("{oops")).status, 400);
  assert.equal((await rpc({ jsonrpc: "2.0", method: "notifications/initialized" })).status, 202);
  assert.equal((await mcp(req("GET", "/api/mcp", undefined, AUTH), s)).status, 405);
  // a token that sent nothing gets a plain answer, not an error
  r = await rpc({ jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "vigil_status", arguments: {} } }, { Authorization: "Bearer " + "U".repeat(40) });
  assert.match((await body(r)).result.content[0].text, /has not sent any data yet/);
});

test("news proxy: only the four pinned feeds, only RSS back, upstream failures are 502", async () => {
  const rss = '<?xml version="1.0"?><rss><channel><title>x</title></channel></rss>';
  const ok = async () => new Response(rss, { status: 200 });
  assert.equal((await feed(req("GET", "/api/feed/coindesk"), ok)).status, 200);
  assert.equal((await feed(req("GET", "/api/feed/evil.example"), ok)).status, 404);
  assert.equal((await feed(req("POST", "/api/feed/coindesk"), ok)).status, 405);
  assert.equal((await feed(req("GET", "/api/feed/decrypt"), async () => new Response("<html>blocked</html>", { status: 200 }))).status, 502);
  assert.equal((await feed(req("GET", "/api/feed/theblock"), async () => { throw new Error("timeout"); })).status, 502);
});
