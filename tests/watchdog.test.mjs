// Run with: node --test tests/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { ackSig, denseMinutes, dueAlerts, groupReleases, mondayOf, parseCoinbase, runWatchdog, solRule, trendRead } from "../netlify/lib/watchdog.mjs";
import { handle as alertsHandle } from "../netlify/functions/alerts.mjs";

const MIN = 60000;
const FED = Date.UTC(2026, 11, 9, 19, 0);          // 9 Dec 2026 14:00 ET = 00:30 IST on the 10th
const NFP = Date.UTC(2026, 11, 4, 13, 30);
const RELEASES = [
  { kind: "NFP", title: "Non-Farm Employment Change", t: NFP },
  { kind: "GDP", title: "Advance GDP q/q", t: Date.UTC(2026, 9, 29, 12, 30) },
  { kind: "PCE", title: "Core PCE Price Index m/m", t: Date.UTC(2026, 9, 29, 12, 30) },
  { kind: "FOMC", title: "FOMC Statement", t: FED },
];

// a first hour that drifts from 100 to 100 * (1 + move), Coinbase row format, newest first
function hour(t0, move, { gapAt = -1, wiggle = 0.001 } = {}) {
  const rows = [];
  for (let k = -5; k < 61; k++) {
    if (k === gapAt) continue;
    const p = 100 * (1 + move * Math.max(0, Math.min(k + 1, 60)) / 60);
    const o = 100 * (1 + move * Math.max(0, Math.min(k, 60)) / 60);
    rows.push([(t0 + k * MIN) / 1000, Math.min(o, p) * (1 - wiggle), Math.max(o, p) * (1 + wiggle), o, p, 10]);
  }
  return rows.reverse();
}

function fakeNet(candleRows) {
  const posts = [];
  const fetchFn = async (url, init = {}) => {
    if (url.startsWith("https://api.exchange.coinbase.com/")) {
      if (!candleRows) return new Response("down", { status: 503 });
      return new Response(JSON.stringify(candleRows), { status: 200 });
    }
    posts.push(JSON.parse(init.body));
    return new Response("{}", { status: 200 });
  };
  return { fetchFn, posts };
}

function memStore() {
  const m = new Map();
  return { get: async (k) => (m.has(k) ? m.get(k) : null), set: async (k, v) => void m.set(k, v), m };
}

test("releases at the same minute become one event", () => {
  const g = groupReleases(RELEASES);
  assert.equal(g.length, 3);
  assert.deepEqual(g[0].kinds, ["GDP", "PCE"]);
});

test("alerts are due only inside their windows", () => {
  const g = groupReleases(RELEASES);
  assert.deepEqual(dueAlerts(NFP - 61 * MIN, g), []);
  assert.deepEqual(dueAlerts(NFP - 59 * MIN, g).map((d) => d.key), ["remind:" + NFP]);
  assert.deepEqual(dueAlerts(NFP - 29 * MIN, g), []);
  assert.deepEqual(dueAlerts(FED - 9 * MIN, g).map((d) => d.key), ["fed:" + FED]);
  assert.deepEqual(dueAlerts(FED + 60 * MIN, g), []);                         // minute 60 is not closed yet
  assert.deepEqual(dueAlerts(FED + 62 * MIN, g).map((d) => d.key), ["basket:" + FED]);
  assert.deepEqual(dueAlerts(FED + 75 * MIN, g), []);                         // the order window is over
});

test("the SOL rule matches the engine's arithmetic", () => {
  const down = solRule(parseCoinbase(hour(FED, -0.006)), FED);
  assert.equal(down.fired, true);
  assert.equal(down.side, "short");
  assert.ok(Math.abs(down.move + 0.006) < 1e-9);
  assert.ok(Math.abs(down.stopPct - 0.025) < 1e-9);                          // SOL's 2.5% floor beats 0.5% and half the range
  assert.ok(Math.abs(down.target1 - down.price * (1 - 1.5 * 0.025)) < 1e-9);
  assert.ok(Math.abs(down.target2 - down.price * (1 - 2.5 * 0.025)) < 1e-9);
  assert.equal(down.deadline, FED + 75 * MIN);
  const up = solRule(parseCoinbase(hour(FED, 0.06, { wiggle: 0 })), FED);
  assert.equal(up.side, "long");
  assert.ok(Math.abs(up.stopPct - 0.5 * (up.price - 100) / up.price) < 1e-9);  // a 6% hour: half the range (2.8%) beats the floor
  assert.equal(solRule(parseCoinbase(hour(FED, 0.002)), FED).fired, false);
  assert.equal(solRule(parseCoinbase(hour(FED, 0.01)).filter((b) => b.t < FED + 40 * MIN), FED), null);   // hour not complete
});

test("a minute without trades is a flat bar at the previous close", () => {
  const bars = parseCoinbase(hour(FED, 0.01, { gapAt: 10 }));
  const d = denseMinutes(bars, FED, 60);
  assert.equal(d[10].filled, true);
  assert.equal(d[10].c, d[9].c);
});

const DAY = 86400000;
function byProduct(rows) {
  const posts = [], urls = [];
  const fetchFn = async (url, init = {}) => {
    urls.push(url);
    if (url.startsWith("https://api.exchange.coinbase.com/")) {
      const p = url.split("/products/")[1].split("/")[0];
      const r = typeof rows === "function" ? rows(p, url) : rows[p];
      return new Response(JSON.stringify(r || []), { status: r ? 200 : 503 });
    }
    posts.push({ url, body: JSON.parse(init.body) });
    return new Response("{}", { status: 200 });
  };
  return { fetchFn, posts, urls };
}

test("a Fed night: all five coins read once after the hour, one alert with each order, one repeat", async () => {
  const store = memStore();
  const net = fakeNet(hour(FED, -0.006));
  const base = { fetchFn: net.fetchFn, store, topic: "t-secret", server: "https://ntfy.example", site: "https://x.netlify.app", releases: RELEASES, trend: false };
  assert.deepEqual(await runWatchdog({ ...base, now: FED - 55 * MIN }), ["hello", "remind:" + FED]);
  assert.deepEqual(await runWatchdog({ ...base, now: FED - 53 * MIN }), []);
  assert.deepEqual(await runWatchdog({ ...base, now: FED - 8 * MIN }), ["fed:" + FED]);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 62 * MIN }), ["basket:" + FED]);
  const b = net.posts[net.posts.length - 1];
  assert.equal(b.topic, "t-secret");
  assert.equal(b.priority, 5);
  assert.match(b.title, /^Fed basket: SELL BTC, ETH, SOL, XRP, LINK before 01:45 IST$/);
  assert.match(b.message, /BTC: market sell at about 99\.\d+, stop 1\.5% above your fill, target 2\.3% below/);
  assert.match(b.message, /ETH: market sell .* stop 2\.0% above/);
  assert.match(b.message, /LINK: market sell .* stop 2\.5% above/);
  assert.equal(b.click, "https://x.netlify.app/#home");
  assert.equal(b.actions[0].label, "I'm on it");
  assert.equal(b.actions[0].url, "https://x.netlify.app/api/alerts/ack?k=basket%3A" + FED + "&s=" + ackSig("t-secret", "basket:" + FED));
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 64 * MIN }), []);                   // not yet: 6 minutes after the first
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 68 * MIN }), ["again:" + FED]);
  assert.match(net.posts[net.posts.length - 1].title, /^Still time: Fed basket orders before 01:45 IST$/);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 70 * MIN }), []);                   // once only
  assert.match(net.posts[0].message, /Next: US jobs report|Next: Fed decision/);
});

test("I'm on it stops the repeat, and only a signed link can", async () => {
  const store = memStore();
  await store.set("sent/hello", "1");
  const net = fakeNet(hour(FED, 0.008));
  const base = { fetchFn: net.fetchFn, store, topic: "t-secret", server: "https://n", site: "https://x.netlify.app", releases: RELEASES, trend: false };
  await runWatchdog({ ...base, now: FED + 62 * MIN });
  const env = { NTFY_TOPIC: "t-secret" };
  const ack = (k, sig) => alertsHandle(new Request("https://x.netlify.app/api/alerts/ack?k=" + encodeURIComponent(k) + "&s=" + sig, { method: "POST" }), { store, env });
  assert.equal((await ack("basket:" + FED, "0".repeat(32))).status, 403);
  assert.equal((await ack("basket:" + FED, ackSig("t-secret", "basket:" + FED))).status, 200);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 68 * MIN }), []);
});

test("quiet coins and unreadable ones are named; the basket waits a little for every coin", async () => {
  let store = memStore(); await store.set("sent/hello", "1");
  let net = byProduct({ "BTC-USD": hour(FED, 0.007), "ETH-USD": hour(FED, 0.006), "SOL-USD": hour(FED, 0.009), "XRP-USD": hour(FED, 0.001) });
  const base = { store, topic: "t", server: "https://n", site: "", releases: RELEASES, trend: false };
  assert.deepEqual(await runWatchdog({ ...base, fetchFn: net.fetchFn, now: FED + 62 * MIN }), []);      // LINK missing: try again
  assert.deepEqual(await runWatchdog({ ...base, fetchFn: net.fetchFn, now: FED + 67 * MIN }), ["basket:" + FED]);
  const m = net.posts[0].body;
  assert.match(m.title, /^Fed basket: BUY BTC, ETH, SOL before 01:45 IST$/);
  assert.match(m.message, /Quiet: XRP\./);
  assert.match(m.message, /Couldn't read LINK here/);
  assert.equal(m.actions, undefined);                                                                    // no site: no buttons
  // nothing fired: a calm note, no repeat
  store = memStore(); await store.set("sent/hello", "1");
  net = byProduct((p) => hour(FED, 0.001));
  await runWatchdog({ ...base, store, fetchFn: net.fetchFn, now: FED + 62 * MIN });
  assert.match(net.posts[0].body.title, /^No Fed-basket signal tonight$/);
  assert.equal(net.posts[0].body.priority, 3);
  assert.deepEqual(await runWatchdog({ ...base, store, fetchFn: net.fetchFn, now: FED + 69 * MIN }), []);
});

test("no data: retry, then tell the user to open the app while there is time", async () => {
  const store = memStore();
  await store.set("sent/hello", "1");
  const net = fakeNet(null);
  const base = { fetchFn: net.fetchFn, store, topic: "t", server: "https://ntfy.example", site: "", releases: RELEASES, trend: false };
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 62 * MIN }), []);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 71 * MIN }), ["basket:" + FED]);
  assert.match(net.posts[0].title, /Open Trinetra now/);
  assert.equal(net.posts[0].click, undefined);
});

// daily candles that fall for 190 days and then jump in the last week: out at the Sunday before, in at the latest Sunday
function days(monday, jump) {
  const rows = [];
  for (let k = 200; k >= 1; k--) {
    const t = monday - k * DAY, i = 200 - k;
    const c = jump && k <= 7 ? 300 : 200 - i * 0.5;
    rows.push([t / 1000, c * 0.99, c * 1.01, c, c, 10]);
  }
  return rows.reverse();
}

test("the weekly trend: Monday morning, once, only when a coin goes in or out", async () => {
  const monday = mondayOf(Date.UTC(2026, 10, 2, 3, 0));
  assert.equal(new Date(monday).getUTCDay(), 1);
  assert.equal(new Date(monday).getUTCHours(), 0);
  const read = trendRead(parseCoinbase(days(monday, true)), monday);
  assert.deepEqual([read.before, read.now, read.ups], [false, true, 5]);
  assert.equal(trendRead(parseCoinbase(days(monday, true)).slice(0, -1), monday), null);                // the latest Sunday is missing
  let store = memStore(); await store.set("sent/hello", "1");
  let net = byProduct((p) => (p === "ETH-USD" ? days(monday, false) : days(monday, true)));
  const base = { store, topic: "t", server: "https://n", site: "https://x.netlify.app", releases: [] };
  assert.deepEqual(await runWatchdog({ ...base, fetchFn: net.fetchFn, now: monday + MIN }), []);           // before 05:32 IST
  assert.deepEqual(await runWatchdog({ ...base, fetchFn: net.fetchFn, now: monday + 3 * MIN }), ["trend:" + monday]);
  const m = net.posts[0].body;
  assert.equal(m.title, "Weekly trend: buy BTC, buy SOL");
  assert.match(m.message, /BTC: 5 of 5 trend lengths up, so hold it: buy at 1x\./);
  assert.match(m.message, /No change: ETH \(out\)\./);
  assert.ok(net.urls.some((u) => u.includes("granularity=86400")));
  assert.deepEqual(await runWatchdog({ ...base, fetchFn: net.fetchFn, now: monday + 9 * MIN }), []);       // once a week
  // no change anywhere: nothing sent, and not asked again that Monday
  store = memStore(); await store.set("sent/hello", "1");
  net = byProduct(() => days(monday, false));
  assert.deepEqual(await runWatchdog({ ...base, store, fetchFn: net.fetchFn, now: monday + 3 * MIN }), []);
  const asked = net.urls.length;
  await runWatchdog({ ...base, store, fetchFn: net.fetchFn, now: monday + 20 * MIN });
  assert.equal(net.urls.length, asked);
  // switched off
  store = memStore(); await store.set("sent/hello", "1");
  net = byProduct((p) => days(monday, true));
  assert.deepEqual(await runWatchdog({ ...base, store, fetchFn: net.fetchFn, now: monday + 3 * MIN, trend: false }), []);
});

test("the test alert: needs a channel, reaches it, and is rate-limited", async () => {
  const store = memStore();
  const net = byProduct({});
  const req = () => new Request("https://x.netlify.app/api/alerts/test", { method: "POST" });
  assert.equal((await alertsHandle(req(), { store, env: {}, fetchFn: net.fetchFn })).status, 503);
  const env = { NTFY_TOPIC: "t-secret", TELEGRAM_BOT_TOKEN: "123:abc", TELEGRAM_CHAT_ID: "42" };
  const r = await alertsHandle(req(), { store, env, fetchFn: net.fetchFn, now: 1e12 });
  assert.equal(r.status, 200);
  assert.deepEqual((await r.json()).channels, ["ntfy", "telegram"]);
  assert.equal(net.posts[0].body.title, "Trinetra test alert");
  assert.equal(net.posts[1].url, "https://api.telegram.org/bot123:abc/sendMessage");
  assert.equal(net.posts[1].body.chat_id, "42");
  assert.equal((await alertsHandle(req(), { store, env, fetchFn: net.fetchFn, now: 1e12 + 60000 })).status, 429);
  assert.equal((await alertsHandle(new Request("https://x.netlify.app/api/alerts/test"), { store, env })).status, 405);
});
