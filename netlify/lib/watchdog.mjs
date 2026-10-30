// Trinetra's watchdog: the checks that must happen even when the app is closed, sent to your phone through ntfy.
//   - a reminder an hour before each big US release (the engine's flat window starts then)
//   - on Fed nights, a heads-up 10 minutes before the decision, then the SOL rule's first-hour read
// Prices come from Coinbase SOL-USD, because Netlify's servers are in the US and Binance refuses US addresses. The
// app still makes the real decision with Binance data and the news check: an alert says "open Trinetra", it is not
// a signal by itself.

const MIN = 60000;
export const REMIND_BEFORE = 60 * MIN;          // the engine keeps you flat from an hour before a release
const REMIND_UNTIL = 30 * MIN;                  // ... so a late run can still remind up to 30 minutes before
export const FED_HEADS_UP = 10 * MIN;
export const READ_AFTER = 60 * MIN;             // momentum_after_60m reads minute 60 after the release
export const ORDER_WINDOW = 15 * MIN;           // tested: a market entry within 15 minutes of the read
const RULE_MOVE = 0.003;                        // more than 0.3% from the release price at minute 60
const STOP_MIN_PCT = 0.005, STOP_FLOOR_PCT = 0.025, T1_R = 1.5, T2_R = 2.5;   // SOL's tested stop floor and targets

export const KIND_NAME = { FOMC: "Fed decision", CPI: "US inflation (CPI)", NFP: "US jobs report", PPI: "US producer prices (PPI)",
  PCE: "US PCE inflation", GDP: "US GDP" };

const IST_CLOCK = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const IST_DAY = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", weekday: "short", day: "numeric", month: "short" });
export const ist = (ms) => IST_CLOCK.format(new Date(ms)) + " IST";
const istDay = (ms) => IST_DAY.format(new Date(ms)) + ", " + ist(ms);
const pct = (x, d = 2) => (x > 0 ? "+" : "") + (x * 100).toFixed(d) + "%";
const price = (x) => (x >= 100 ? x.toFixed(2) : x >= 1 ? x.toFixed(3) : x.toPrecision(4));

/** Releases at the same minute (GDP and PCE often share 08:30) become one event. */
export function groupReleases(releases) {
  const groups = [];
  for (const r of releases || []) {
    if (!r || !Number.isFinite(r.t) || !KIND_NAME[r.kind]) continue;
    const t = Math.floor(r.t / MIN) * MIN;
    const g = groups.find((x) => x.t === t);
    if (g) { if (!g.kinds.includes(r.kind)) g.kinds.push(r.kind); }
    else groups.push({ t, kinds: [r.kind] });
  }
  return groups.sort((a, b) => a.t - b.t);
}

/** What is due at `now`: each alert has a stable key, so a run that repeats within the window sends nothing new. */
export function dueAlerts(now, groups) {
  const out = [];
  for (const g of groups) {
    if (now >= g.t - REMIND_BEFORE && now < g.t - REMIND_UNTIL) out.push({ key: "remind:" + g.t, kind: "remind", group: g });
    if (!g.kinds.includes("FOMC")) continue;
    if (now >= g.t - FED_HEADS_UP && now < g.t) out.push({ key: "fed:" + g.t, kind: "fed", group: g });
    if (now >= g.t + READ_AFTER + MIN && now < g.t + READ_AFTER + ORDER_WINDOW) out.push({ key: "sol:" + g.t, kind: "sol", group: g });
  }
  return out;
}

/** Coinbase candles ([time s, low, high, open, close, volume], newest first) as bars oldest first. */
export function parseCoinbase(rows) {
  return (Array.isArray(rows) ? rows : [])
    .filter((r) => Array.isArray(r) && r.length >= 5 && r.slice(0, 5).every((x) => Number.isFinite(Number(x))))
    .map((r) => ({ t: Number(r[0]) * 1000, l: Number(r[1]), h: Number(r[2]), o: Number(r[3]), c: Number(r[4]) }))
    .sort((a, b) => a.t - b.t);
}

/** One bar per minute from tRel, as the engine's event read builds them: a minute without trades is a flat bar at
 *  the previous close. Null when there is no price to start from. */
export function denseMinutes(bars, tRel, count) {
  const byT = new Map(bars.map((b) => [b.t, b]));
  const before = bars.filter((b) => b.t < tRel);
  let prev = before.length ? before[before.length - 1].c : null;
  const out = [];
  for (let k = 0; k < count; k++) {
    const t = tRel + k * MIN;
    let b = byT.get(t);
    if (!b) {
      if (prev === null) return null;
      b = { t, o: prev, h: prev, l: prev, c: prev, filled: true };
    }
    out.push(b);
    prev = b.c;
  }
  return out;
}

/** The tested SOL Fed rule (momentum_after_60m) on minute bars, measured exactly as desk.py's analyse_event does:
 *  follow the first hour if price is more than 0.3% from the release price at minute 60; stop at the larger of 0.5%,
 *  half the first hour's range and SOL's 2.5% floor; targets at 1.5R and 2.5R. Null when the hour isn't complete. */
export function solRule(bars, tRel) {
  if (!bars.length || bars[bars.length - 1].t < tRel + 55 * MIN) return null;    // the data does not reach minute 60 yet
  const post = denseMinutes(bars, tRel, 60);
  if (!post) return null;
  const p0 = post[0].o, c = post[59].c;
  const move = c / p0 - 1;
  const deadline = tRel + READ_AFTER + ORDER_WINDOW;
  if (Math.abs(move) <= RULE_MOVE) return { fired: false, move, price: c, deadline };
  const side = move > 0 ? "long" : "short";
  const span = Math.max(...post.map((b) => b.h)) - Math.min(...post.map((b) => b.l));
  const dist = Math.max(STOP_MIN_PCT * c, 0.5 * span, STOP_FLOOR_PCT * c);
  const sg = side === "long" ? 1 : -1;
  return { fired: true, side, move, price: c, stop: c - sg * dist, target1: c + sg * T1_R * dist, target2: c + sg * T2_R * dist,
    stopPct: dist / c, deadline };
}

// ---------------------------------------------------------------- messages (ntfy: title, message, priority 1-5, tags)

export function helloMessage(groups, now) {
  const next = groups.find((g) => g.t > now);
  return { title: "Trinetra alerts are on", priority: 2, tags: ["white_check_mark"],
    message: "You'll get a reminder an hour before each big US release and, after Fed decisions, the SOL rule's answer."
      + (next ? " Next: " + next.kinds.map((k) => KIND_NAME[k]).join(" + ") + ", " + istDay(next.t) + "." : "") };
}

export function remindMessage(g) {
  const name = g.kinds.map((k) => KIND_NAME[k]).join(" + ");
  const flat = "No new trades from " + ist(g.t - REMIND_BEFORE) + " until " + ist(g.t + 30 * MIN) + ".";
  if (g.kinds.includes("FOMC")) {
    return { title: name + " in 1 hour", priority: 4, tags: ["bank"],
      message: "At " + ist(g.t) + ". " + flat + " The tested SOL rule reads the first hour: the answer comes here at about "
        + ist(g.t + READ_AFTER + MIN) + ", and Trinetra checks it too if it is open." };
  }
  return { title: name + " in 1 hour", priority: 3, tags: ["calendar"], message: "At " + ist(g.t) + ". " + flat };
}

export function fedMessage(g) {
  return { title: "Fed decision in 10 minutes", priority: 4, tags: ["bank"],
    message: "At " + ist(g.t) + ". Don't trade into it. The SOL rule's answer comes here at about " + ist(g.t + READ_AFTER + MIN) + "." };
}

export function solMessage(res) {
  if (!res.fired) {
    return { title: "No SOL signal tonight", priority: 3, tags: ["zzz"],
      message: "SOL moved " + pct(res.move) + " in the first hour after the Fed; the tested rule needs more than 0.3%. Nothing to do." };
  }
  const buy = res.side === "long";
  const t1Pct = Math.abs(res.target1 / res.price - 1);
  return { title: "SOL rule fired: " + (buy ? "BUY" : "SELL") + " before " + ist(res.deadline), priority: 5, tags: ["rotating_light"],
    message: "SOL " + pct(res.move) + " in the first hour after the Fed (the rule needs more than 0.3%). Tested plan: market "
      + (buy ? "buy" : "sell") + " now at about " + price(res.price) + ", stop " + (res.stopPct * 100).toFixed(1) + "% "
      + (buy ? "below" : "above") + " your fill (about " + price(res.stop) + "), first target " + (t1Pct * 100).toFixed(1) + "% "
      + (buy ? "above" : "below") + " (about " + price(res.target1) + "). Read from Coinbase prices on the server: open Trinetra "
      + "and place it only if Trinetra shows the signal too (it checks Binance data, the news check and your limits)." };
}

export function solFailMessage(g) {
  return { title: "Open Trinetra now: SOL check", priority: 4, tags: ["warning"],
    message: "The server couldn't read SOL's first hour after the Fed. Trinetra reads it itself when it is open; the order "
      + "window ends at " + ist(g.t + READ_AFTER + ORDER_WINDOW) + "." };
}

// ---------------------------------------------------------------- network

export async function coinbaseCandles(fetchFn, product, from, to) {
  const u = "https://api.exchange.coinbase.com/products/" + product + "/candles?granularity=60&start="
    + new Date(from).toISOString() + "&end=" + new Date(to).toISOString();
  const r = await fetchFn(u, { headers: { "User-Agent": "TrinetraWatchdog/1.0", Accept: "application/json" }, signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error("Coinbase answered " + r.status);
  return parseCoinbase(await r.json());
}

/** Publish through ntfy's JSON form, which carries UTF-8 titles (header values cannot). */
export async function publish(fetchFn, server, topic, msg) {
  const r = await fetchFn(String(server || "https://ntfy.sh").replace(/\/+$/, "") + "/", {
    method: "POST", headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(8000),
    body: JSON.stringify({ topic, title: msg.title, message: msg.message, priority: msg.priority || 3, tags: msg.tags || [],
      ...(msg.click ? { click: msg.click } : {}) }),
  });
  if (!r.ok) throw new Error("ntfy answered " + r.status);
}

// ---------------------------------------------------------------- one run

/** Sends what is due and not yet sent; returns the keys sent. `store` needs get(key) and set(key, value). */
export async function runWatchdog({ now, fetchFn, store, topic, server, site, releases }) {
  const groups = groupReleases(releases);
  const due = dueAlerts(now, groups);
  if (!(await store.get("sent/hello"))) due.unshift({ key: "hello", kind: "hello" });
  const sent = [];
  for (const d of due) {
    if (await store.get("sent/" + d.key)) continue;
    let msg = null;
    if (d.kind === "hello") msg = helloMessage(groups, now);
    else if (d.kind === "remind") msg = remindMessage(d.group);
    else if (d.kind === "fed") msg = fedMessage(d.group);
    else if (d.kind === "sol") {
      let res = null;
      try { res = solRule(await coinbaseCandles(fetchFn, "SOL-USD", d.group.t - 5 * MIN, d.group.t + READ_AFTER + MIN), d.group.t); }
      catch (e) { console.log("watchdog: SOL read failed: " + String((e && e.message) || e).slice(0, 200)); }
      if (res) msg = solMessage(res);
      else if (now >= d.group.t + READ_AFTER + 10 * MIN) msg = solFailMessage(d.group);    // give up retrying with time left to act
      else continue;                                                                        // data not there yet: next run
    }
    await publish(fetchFn, server, topic, { ...msg, click: site ? site.replace(/\/+$/, "") + "/#home" : undefined });
    await store.set("sent/" + d.key, JSON.stringify({ at: now }));
    sent.push(d.key);
  }
  return sent;
}
