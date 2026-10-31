// Run with: node --test tests/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { denseMinutes, dueAlerts, groupReleases, parseCoinbase, runWatchdog, solRule } from "../netlify/lib/watchdog.mjs";

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
  assert.deepEqual(dueAlerts(FED + 62 * MIN, g).map((d) => d.key), ["sol:" + FED]);
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

test("a run sends each alert once, reads SOL after the hour, and needs no repeat", async () => {
  const store = memStore();
  const net = fakeNet(hour(FED, -0.006));
  const base = { fetchFn: net.fetchFn, store, topic: "t-secret", server: "https://ntfy.example", site: "https://x.netlify.app", releases: RELEASES };
  assert.deepEqual(await runWatchdog({ ...base, now: FED - 55 * MIN }), ["hello", "remind:" + FED]);
  assert.deepEqual(await runWatchdog({ ...base, now: FED - 53 * MIN }), []);
  assert.deepEqual(await runWatchdog({ ...base, now: FED - 8 * MIN }), ["fed:" + FED]);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 62 * MIN }), ["sol:" + FED]);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 64 * MIN }), []);
  const sol = net.posts[net.posts.length - 1];
  assert.equal(sol.topic, "t-secret");
  assert.equal(sol.priority, 5);
  assert.match(sol.title, /^SOL rule fired: SELL before 01:45 IST$/);
  assert.equal(sol.click, "https://x.netlify.app/#home");
  assert.match(net.posts[0].message, /Next: US jobs report|Next: Fed decision/);
});

test("no data: retry, then tell the user to open the app while there is time", async () => {
  const store = memStore();
  await store.set("sent/hello", "1");
  const net = fakeNet(null);
  const base = { fetchFn: net.fetchFn, store, topic: "t", server: "https://ntfy.example", site: "", releases: RELEASES };
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 62 * MIN }), []);
  assert.deepEqual(await runWatchdog({ ...base, now: FED + 71 * MIN }), ["sol:" + FED]);
  assert.match(net.posts[0].title, /Open Trinetra now/);
  assert.equal(net.posts[0].click, undefined);
});

test("Fed night: ETH's small signal rides along with SOL's answer, and is sent alone when SOL is quiet", async () => {
  const byProduct = (rows) => {
    const posts = [];
    const fetchFn = async (url, init = {}) => {
      if (url.startsWith("https://api.exchange.coinbase.com/")) {
        const p = url.split("/products/")[1].split("/")[0];
        return new Response(JSON.stringify(rows[p] || []), { status: rows[p] ? 200 : 503 });
      }
      posts.push(JSON.parse(init.body));
      return new Response("{}", { status: 200 });
    };
    return { fetchFn, posts };
  };
  // both fire: SOL's alert, with ETH's small signal in it
  let store = memStore(); await store.set("sent/hello", "1");
  let net = byProduct({ "SOL-USD": hour(FED, 0.008), "ETH-USD": hour(FED, 0.007) });
  assert.deepEqual(await runWatchdog({ fetchFn: net.fetchFn, store, topic: "t", server: "https://n", site: "", releases: RELEASES, now: FED + 62 * MIN }), ["sol:" + FED]);
  assert.match(net.posts[0].title, /^SOL rule fired: BUY/);
  assert.match(net.posts[0].message, /Small signal, ETH: buy .* a quarter of your normal risk/);
  // SOL quiet, ETH fires: the small signal is the alert
  store = memStore(); await store.set("sent/hello", "1");
  net = byProduct({ "SOL-USD": hour(FED, 0.001), "ETH-USD": hour(FED, -0.006) });
  await runWatchdog({ fetchFn: net.fetchFn, store, topic: "t", server: "https://n", site: "", releases: RELEASES, now: FED + 62 * MIN });
  assert.match(net.posts[0].title, /^Small signal: ETH SELL before 01:45 IST$/);
  assert.equal(net.posts[0].priority, 4);
  // ETH unreadable: SOL's answer still goes, and says so
  store = memStore(); await store.set("sent/hello", "1");
  net = byProduct({ "SOL-USD": hour(FED, 0.001) });
  await runWatchdog({ fetchFn: net.fetchFn, store, topic: "t", server: "https://n", site: "", releases: RELEASES, now: FED + 62 * MIN });
  assert.match(net.posts[0].title, /^No SOL signal tonight$/);
  assert.match(net.posts[0].message, /ETH's small signal couldn't be read/);
});
