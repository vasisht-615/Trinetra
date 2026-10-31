// Trinetra's watchdog: the checks that must happen even when the app is closed, sent to your phone through ntfy (and
// Telegram, if set up).
//   - a reminder an hour before each big US release (the engine's flat window starts then)
//   - on Fed nights, a heads-up 10 minutes before the decision, then the Fed basket's first-hour read on all five
//     coins, with each order; it repeats once 6 minutes later unless you tap "I'm on it"
//   - on Mondays after 05:30 IST, the weekly trend's changes (a coin going in or out)
// Prices come from Coinbase (BTC-USD and the others), because Netlify's servers are in the US and Binance refuses US
// addresses. The app still makes the real decision with Binance data and the news check: an alert says "open
// Trinetra", it is not a signal by itself.
import { createHmac } from "node:crypto";

const MIN = 60000;
export const REMIND_BEFORE = 60 * MIN;          // the engine keeps you flat from an hour before a release
const REMIND_UNTIL = 30 * MIN;                  // ... so a late run can still remind up to 30 minutes before
export const FED_HEADS_UP = 10 * MIN;
export const READ_AFTER = 60 * MIN;             // momentum_after_60m reads minute 60 after the release
export const ORDER_WINDOW = 15 * MIN;           // tested: a market entry within 15 minutes of the read
const RULE_MOVE = 0.003;                        // more than 0.3% from the release price at minute 60
const STOP_MIN_PCT = 0.005, STOP_FLOOR_PCT = 0.025, T1_R = 1.5, T2_R = 2.5;   // SOL's tested stop floor and targets
// The Fed basket: the five coins and each one's tested stop floor (the engine's EVENT_STOP_FLOOR_PCT)
export const BASKET = [["BTC", 0.015], ["ETH", 0.02], ["SOL", 0.025], ["XRP", 0.025], ["LINK", 0.025]];
export const REPEAT_AFTER = 6 * MIN;            // a fired basket alert repeats once, unless "I'm on it" was tapped
const DAY = 86400000;
export const TREND_COINS = ["BTC", "ETH", "SOL"];
const TREND_LOOKBACKS = [10, 20, 40, 80, 160];
const TREND_FROM = 2 * MIN, TREND_UNTIL = 6.5 * 3600000;    // Mondays 00:02 to 06:30 UTC (05:32 to 12:00 IST)

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
    if (now >= g.t + READ_AFTER + MIN && now < g.t + READ_AFTER + ORDER_WINDOW) out.push({ key: "basket:" + g.t, kind: "basket", group: g });
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
export function solRule(bars, tRel) { return fedRule(bars, tRel, STOP_FLOOR_PCT); }
/** The same first-hour rule with a coin's own stop floor (BTC 1.5%, ETH 2%, the others 2.5%). */
export function fedRule(bars, tRel, floor) {
  if (!bars.length || bars[bars.length - 1].t < tRel + 55 * MIN) return null;    // the data does not reach minute 60 yet
  const post = denseMinutes(bars, tRel, 60);
  if (!post) return null;
  const p0 = post[0].o, c = post[59].c;
  const move = c / p0 - 1;
  const deadline = tRel + READ_AFTER + ORDER_WINDOW;
  if (Math.abs(move) <= RULE_MOVE) return { fired: false, move, price: c, deadline };
  const side = move > 0 ? "long" : "short";
  const span = Math.max(...post.map((b) => b.h)) - Math.min(...post.map((b) => b.l));
  const dist = Math.max(STOP_MIN_PCT * c, 0.5 * span, floor * c);
  const sg = side === "long" ? 1 : -1;
  return { fired: true, side, move, price: c, stop: c - sg * dist, target1: c + sg * T1_R * dist, target2: c + sg * T2_R * dist,
    stopPct: dist / c, deadline };
}

// ---------------------------------------------------------------- messages (ntfy: title, message, priority 1-5, tags)

export function helloMessage(groups, now) {
  const next = groups.find((g) => g.t > now);
  return { title: "Trinetra alerts are on", priority: 2, tags: ["white_check_mark"],
    message: "You'll get a reminder an hour before each big US release, the Fed basket's answer about an hour after each Fed decision (all five coins, with each order), "
      + "and Monday's weekly trend changes." + (next ? " Next: " + next.kinds.map((k) => KIND_NAME[k]).join(" + ") + ", " + istDay(next.t) + "." : "") };
}

export function testMessage(now) {
  return { title: "Trinetra test alert", priority: 4, tags: ["bell"],
    message: "If you can read this, phone alerts work. Sent " + istDay(now) + ". Fed-night signals come at the highest priority: in the ntfy app, let this topic override Do Not Disturb so they ring at night." };
}

export function remindMessage(g) {
  const name = g.kinds.map((k) => KIND_NAME[k]).join(" + ");
  const flat = "No new trades from " + ist(g.t - REMIND_BEFORE) + " until " + ist(g.t + 30 * MIN) + ".";
  if (g.kinds.includes("FOMC")) {
    return { title: name + " in 1 hour", priority: 4, tags: ["bank"],
      message: "At " + ist(g.t) + ". " + flat + " The tested Fed-basket rule reads the first hour on all five coins: the answer comes here at about "
        + ist(g.t + READ_AFTER + MIN) + ", and Trinetra checks it too if it is open." };
  }
  return { title: name + " in 1 hour", priority: 3, tags: ["calendar"], message: "At " + ist(g.t) + ". " + flat };
}

export function fedMessage(g) {
  return { title: "Fed decision in 10 minutes", priority: 4, tags: ["bank"],
    message: "At " + ist(g.t) + ". Don't trade into it. The Fed basket's answer comes here at about " + ist(g.t + READ_AFTER + MIN) + ": have your exchange open then." };
}

const pc1 = (x) => (x * 100).toFixed(1) + "%";
/** The basket's answer: `res` is [{ coin, r }] with r from fedRule (null when the coin's minutes couldn't be read). */
export function basketMessage(res, deadline) {
  const fired = res.filter((x) => x.r && x.r.fired), quiet = res.filter((x) => x.r && !x.r.fired), lost = res.filter((x) => !x.r);
  const lostLine = lost.length ? " Couldn't read " + lost.map((x) => x.coin).join(", ") + " here: Trinetra checks " + (lost.length > 1 ? "them" : "it") + " when it is open." : "";
  if (!fired.length) {
    return { title: "No Fed-basket signal tonight", priority: 3, tags: ["zzz"],
      message: "The first hour after the Fed: " + quiet.map((x) => x.coin + " " + pct(x.r.move)).join(", ") + ". The tested rule needs more than 0.3%. Nothing to do." + lostLine };
  }
  const sides = [...new Set(fired.map((x) => x.r.side))];
  const head = sides.length === 1 ? (sides[0] === "long" ? "BUY " : "SELL ") + fired.map((x) => x.coin).join(", ") : "trade " + fired.map((x) => x.coin).join(", ");
  const lines = fired.map((x) => {
    const buy = x.r.side === "long", t1 = Math.abs(x.r.target1 / x.r.price - 1);
    return x.coin + ": market " + (buy ? "buy" : "sell") + " at about " + price(x.r.price) + ", stop " + pc1(x.r.stopPct) + (buy ? " below" : " above")
      + " your fill, target " + pc1(t1) + (buy ? " above" : " below") + ".";
  });
  return { title: "Fed basket: " + head + " before " + ist(deadline), priority: 5, tags: ["rotating_light"],
    message: lines.join("\n") + "\nEach at 15% of your normal risk: the sizes are in Trinetra (Copy all orders). Read from Coinbase prices on the server: place them only if Trinetra "
      + "shows the signal too (it checks Binance data, the news check and your limits)." + (quiet.length ? " Quiet: " + quiet.map((x) => x.coin).join(", ") + "." : "") + lostLine };
}

export function againMessage(fired, deadline) {
  return { title: "Still time: Fed basket orders before " + ist(deadline), priority: 5, tags: ["alarm_clock"],
    message: "The Fed basket fired on " + fired.join(", ") + ". Market orders are tested only until " + ist(deadline) + ": after that, don't chase. Open Trinetra for the sizes, stops and targets." };
}

export function basketFailMessage(g) {
  return { title: "Open Trinetra now: Fed basket check", priority: 4, tags: ["warning"],
    message: "The server couldn't read the coins' first hour after the Fed. Trinetra reads it itself when it is open; the order "
      + "window ends at " + ist(g.t + READ_AFTER + ORDER_WINDOW) + "." };
}

/** One vote per trend length on daily closes (oldest first): +1 when the close is above the close that many days before. */
export function trendVotes(closes, i) { return TREND_LOOKBACKS.map((n) => (closes[i] > closes[i - n] ? 1 : -1)); }

/** In or out at the last two Sunday closes (the engine's weekly check: hold with 3 or more of 5 up). `bars` are closed
 *  daily bars, oldest first. Null when there is not enough history or the latest Sunday isn't the one before `monday`. */
export function trendRead(bars, monday) {
  const closes = bars.map((b) => b.c);
  const sun = bars.map((b, i) => [b, i]).filter(([b, i]) => new Date(b.t).getUTCDay() === 0 && i >= 160).map(([, i]) => i);
  if (sun.length < 2 || bars[sun[sun.length - 1]].t + DAY !== monday) return null;
  const ups = (i) => trendVotes(closes, i).filter((v) => v > 0).length;
  const a = ups(sun[sun.length - 2]), b = ups(sun[sun.length - 1]);
  return { before: a >= 3, now: b >= 3, ups: b, price: closes[sun[sun.length - 1]] };
}

export function trendMessage(rows) {
  const flips = rows.filter((r) => r.read && r.read.before !== r.read.now);
  if (!flips.length) return null;
  const same = rows.filter((r) => r.read && r.read.before === r.read.now);
  return { title: "Weekly trend: " + flips.map((r) => (r.read.now ? "buy " : "sell all ") + r.coin).join(", "), priority: 4, tags: ["chart_with_upwards_trend"],
    message: flips.map((r) => r.coin + ": " + r.read.ups + " of 5 trend lengths up, so " + (r.read.now ? "hold it: buy at 1x." : "stay out: sell your whole long.")).join(" ")
      + (same.length ? " No change: " + same.map((r) => r.coin + (r.read.now ? " (hold)" : " (out)")).join(", ") + "." : "")
      + " Any time this week, no stop. Open Trinetra for the sizes (Copy the orders). Read from Coinbase daily prices; Trinetra checks Binance's." };
}

// ---------------------------------------------------------------- network

export async function coinbaseCandles(fetchFn, product, from, to, granularity = 60) {
  const u = "https://api.exchange.coinbase.com/products/" + product + "/candles?granularity=" + granularity + "&start="
    + new Date(from).toISOString() + "&end=" + new Date(to).toISOString();
  const r = await fetchFn(u, { headers: { "User-Agent": "TrinetraWatchdog/1.0", Accept: "application/json" }, signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error("Coinbase answered " + r.status);
  return parseCoinbase(await r.json());
}

/** Publish through ntfy's JSON form, which carries UTF-8 titles (header values cannot), and its action buttons. */
export async function publish(fetchFn, server, topic, msg) {
  const r = await fetchFn(String(server || "https://ntfy.sh").replace(/\/+$/, "") + "/", {
    method: "POST", headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(8000),
    body: JSON.stringify({ topic, title: msg.title, message: msg.message, priority: msg.priority || 3, tags: msg.tags || [],
      ...(msg.click ? { click: msg.click } : {}), ...(msg.actions ? { actions: msg.actions } : {}) }),
  });
  if (!r.ok) throw new Error("ntfy answered " + r.status);
}

/** The same message to a Telegram chat (a bot token and chat id), for those who prefer Telegram. */
export async function publishTelegram(fetchFn, telegram, msg) {
  const r = await fetchFn("https://api.telegram.org/bot" + telegram.token + "/sendMessage", {
    method: "POST", headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(8000),
    body: JSON.stringify({ chat_id: telegram.chat, text: msg.title + "\n" + msg.message + (msg.click ? "\n" + msg.click : ""), disable_notification: (msg.priority || 3) < 4 }),
  });
  if (!r.ok) throw new Error("Telegram answered " + r.status);
}

/** Every channel that is set up; fails only when all of them fail. Returns the channels that took it. */
export async function send(fetchFn, ch, msg) {
  const ok = [], errs = [];
  if (ch.topic) { try { await publish(fetchFn, ch.server, ch.topic, msg); ok.push("ntfy"); } catch (e) { errs.push(String((e && e.message) || e)); } }
  if (ch.telegram && ch.telegram.token && ch.telegram.chat) {
    try { await publishTelegram(fetchFn, ch.telegram, msg); ok.push("telegram"); } catch (e) { errs.push(String((e && e.message) || e)); }
  }
  if (!ok.length) throw new Error(errs.join("; ") || "no alert channel is set up");
  return ok;
}

/** "I'm on it" links carry a signature made with the alert secret, so only your own alert can stop its repeat. */
export const ackSig = (secret, key) => createHmac("sha256", String(secret)).update(String(key)).digest("hex").slice(0, 32);
export const mondayOf = (now) => Math.floor((now - 4 * DAY) / (7 * DAY)) * 7 * DAY + 4 * DAY;     // 1970-01-05 was a Monday

// ---------------------------------------------------------------- one run

/** Sends what is due and not yet sent; returns the keys sent. `store` needs get(key) and set(key, value). The
 *  channels are ntfy (`topic`, `server`) and, when given, `telegram` ({ token, chat }). */
export async function runWatchdog({ now, fetchFn, store, topic, server, site, releases, telegram, trend = true }) {
  const ch = { topic, server, telegram };
  const secret = topic || (telegram && telegram.token) || "";
  const home = site ? site.replace(/\/+$/, "") + "/#home" : undefined;
  const groups = groupReleases(releases);
  const due = dueAlerts(now, groups);
  if (!(await store.get("sent/hello"))) due.unshift({ key: "hello", kind: "hello" });
  const sent = [];
  for (const d of due) {
    if (await store.get("sent/" + d.key)) continue;
    let msg = null, fired = null;
    if (d.kind === "hello") msg = helloMessage(groups, now);
    else if (d.kind === "remind") msg = remindMessage(d.group);
    else if (d.kind === "fed") msg = fedMessage(d.group);
    else if (d.kind === "basket") {
      const res = [];
      for (const [coin, floor] of BASKET) {
        let r = null;
        try { r = fedRule(await coinbaseCandles(fetchFn, coin + "-USD", d.group.t - 5 * MIN, d.group.t + READ_AFTER + MIN), d.group.t, floor); }
        catch (e) { console.log("watchdog: " + coin + " read failed: " + String((e && e.message) || e).slice(0, 200)); }
        res.push({ coin, r });
      }
      const deadline = d.group.t + READ_AFTER + ORDER_WINDOW;
      const read = res.filter((x) => x.r);
      if (read.length && (read.length === res.length || now >= d.group.t + READ_AFTER + 6 * MIN)) {   // all five, or what there is after 6 minutes
        msg = basketMessage(res, deadline);
        fired = res.filter((x) => x.r && x.r.fired).map((x) => x.coin);
        if (fired.length && site) {
          const url = site.replace(/\/+$/, "") + "/api/alerts/ack?k=" + encodeURIComponent(d.key) + "&s=" + ackSig(secret, d.key);
          msg.actions = [{ action: "http", label: "I'm on it", url, method: "POST", clear: true }, { action: "view", label: "Open Trinetra", url: home }];
        }
      } else if (now >= d.group.t + READ_AFTER + 10 * MIN) msg = basketFailMessage(d.group);     // give up retrying with time left to act
      else continue;                                                                          // data not there yet: next run
    }
    await send(fetchFn, ch, { ...msg, click: home });
    await store.set("sent/" + d.key, JSON.stringify({ at: now, fired }));
    sent.push(d.key);
  }
  // the repeat: once, 6 minutes after a basket alert that fired, while there is still time to order, unless acknowledged
  for (const g of groups.filter((x) => x.kinds.includes("FOMC") && now > x.t && now < x.t + READ_AFTER + ORDER_WINDOW)) {
    const key = "basket:" + g.t, deadline = g.t + READ_AFTER + ORDER_WINDOW;
    let first = null;
    try { first = JSON.parse((await store.get("sent/" + key)) || "null"); } catch (e) { first = null; }
    if (!first || !(first.fired || []).length || now < first.at + REPEAT_AFTER || now > deadline - 2 * MIN) continue;
    if ((await store.get("ack/" + key)) || (await store.get("sent/again:" + g.t))) continue;
    await send(fetchFn, ch, { ...againMessage(first.fired, deadline), click: home });
    await store.set("sent/again:" + g.t, JSON.stringify({ at: now }));
    sent.push("again:" + g.t);
  }
  // the weekly trend: Monday morning, once, when a coin goes in or out
  const monday = mondayOf(now);
  if (trend && now >= monday + TREND_FROM && now < monday + TREND_UNTIL && !(await store.get("sent/trend:" + monday))) {
    const rows = [];
    for (const coin of TREND_COINS) {
      let read = null;
      try {
        const bars = (await coinbaseCandles(fetchFn, coin + "-USD", monday - 200 * DAY, monday, 86400)).filter((b) => b.t + DAY <= now);
        read = trendRead(bars, monday);
      } catch (e) { console.log("watchdog: " + coin + " daily read failed: " + String((e && e.message) || e).slice(0, 200)); }
      rows.push({ coin, read });
    }
    if (rows.every((r) => r.read) || now >= monday + TREND_UNTIL - 30 * MIN) {        // all three, or what there is before the window ends
      const msg = trendMessage(rows);
      if (msg) { await send(fetchFn, ch, { ...msg, click: home }); sent.push("trend:" + monday); }
      await store.set("sent/trend:" + monday, JSON.stringify({ at: now, flips: !!msg }));
    }
  }
  return sent;
}
