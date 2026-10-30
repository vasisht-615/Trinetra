// The whole app in a real browser: every screen and state, phone to desktop, light and dark, reduced motion.
//
//     node tests/app/qa.cjs [--out DIR] [--quick]
//
// It serves site/ with the headers from site/_headers (the same Content-Security-Policy as Netlify), runs the app in demo
// mode, injects the states demo data never reaches (signals, a small signal, paused rules, a Fed night, news checks, failed
// checks), and on every screen checks: no console error, page error or CSP violation; no sideways scrolling; every
// button and link has a name; no duplicate ids; the words a state must show. It exits 1 on any failure.
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const SITE = path.join(__dirname, "..", "..", "site");
const args = process.argv.slice(2);
const OUT = args.includes("--out") ? args[args.indexOf("--out") + 1] : path.join(require("os").tmpdir(), "trinetra-qa");
const QUICK = args.includes("--quick");
fs.mkdirSync(OUT, { recursive: true });

// ---------------------------------------------------------------- a static server with Netlify's headers
const HEADERS = {};
let block = null;
for (const line of fs.readFileSync(path.join(SITE, "_headers"), "utf8").split("\n")) {
  if (!line.trim()) continue;
  if (!line.startsWith(" ")) { block = line.trim(); continue; }
  if (block === "/*") { const i = line.indexOf(":"); HEADERS[line.slice(0, i).trim()] = line.slice(i + 1).trim(); }
}
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".woff2": "font/woff2", ".py": "text/plain", ".txt": "text/plain", ".wasm": "application/wasm", ".gz": "application/gzip", ".zip": "application/zip",
  ".webmanifest": "application/manifest+json", ".pine": "text/plain" };
function serve() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
      if (p.endsWith("/")) p += "index.html";
      const f = path.join(SITE, p);
      if (!f.startsWith(SITE) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end("not found"); return; }
      res.writeHead(200, Object.assign({ "Content-Type": TYPES[path.extname(f)] || "application/octet-stream", "Cache-Control": "no-store" }, HEADERS));
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}

// ---------------------------------------------------------------- checks
const failures = [], notes = [];
const fail = (label, msg) => { failures.push(label + ": " + msg); console.log("FAIL " + label + ": " + msg); };
async function audit(page, label, must = []) {
  const r = await page.evaluate(() => {
    const out = { overflow: document.documentElement.scrollWidth - window.innerWidth, unnamed: [], dupIds: [], text: document.body.innerText };
    const seen = {};
    document.querySelectorAll("[id]").forEach((n) => { seen[n.id] = (seen[n.id] || 0) + 1; });
    out.dupIds = Object.keys(seen).filter((k) => seen[k] > 1);
    document.querySelectorAll("button, a[href], input, select, textarea, [role=button]").forEach((n) => {
      const cs = getComputedStyle(n);
      // not on screen: hidden, or inside a closed fold (Chrome keeps a box there but renders nothing)
      if (cs.display === "none" || cs.visibility === "hidden" || n.closest("[hidden]") || !n.getClientRects().length
          || (n.checkVisibility && !n.checkVisibility({ contentVisibilityAuto: true, visibilityProperty: true })) || (n.closest("details:not([open])") && !n.closest("summary"))) return;
      const name = (n.getAttribute("aria-label") || n.getAttribute("title") || n.innerText || n.value || n.getAttribute("placeholder") || "").trim();
      const labelled = n.id && document.querySelector('label[for="' + n.id + '"]') || n.closest("label");
      if (!name && !labelled) out.unnamed.push(n.tagName.toLowerCase() + "." + String(n.className && n.className.baseVal !== undefined ? n.className.baseVal : n.className).slice(0, 40));
    });
    return out;
  });
  if (r.overflow > 1) fail(label, "the page scrolls sideways by " + r.overflow + " px");
  if (r.unnamed.length) fail(label, "controls without a name: " + [...new Set(r.unnamed)].slice(0, 6).join(", "));
  if (r.dupIds.length) fail(label, "duplicate ids: " + r.dupIds.join(", "));
  for (const m of must) if (!(m instanceof RegExp ? m.test(r.text) : r.text.includes(m))) fail(label, "missing text " + m);
  return r;
}
async function ready(page) {
  for (let i = 0; i < 240; i++) {
    const s = await page.locator("#status-text").textContent().catch(() => "");
    if (s && !/Starting|Checking/i.test(s)) return true;
    await page.waitForTimeout(1000);
  }
  return false;
}
async function newPage(browser, label, opts = {}) {
  const ctx = await browser.newContext(Object.assign({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, colorScheme: "dark" }, opts.ctx || {}));
  if (opts.demo !== false) await ctx.addInitScript(() => { try { localStorage.setItem("desk-demo", "1"); } catch (e) {} });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") fail(label, "console error: " + m.text().slice(0, 200)); });
  page.on("pageerror", (e) => fail(label, "page error: " + String(e).slice(0, 200)));
  return { ctx, page };
}
const shot = (page, name, full = true) => page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: full }).catch(() => {});

// ---------------------------------------------------------------- states demo data never reaches
const STATES = {
  signal: (s) => {
    const now = Date.now(), sol = s.today.coins.find((c) => c.symbol === "SOLUSDT");
    Object.assign(sol, { decision: "SIGNAL LONG", tier: null, candidate: { setup: "momentum_after_60m", side: "long", kind: "event", entry: 152.4, stop: 148.59, target1: 158.12, target2: 161.93, expires: now + 11 * 60000 } });
    s.signalPlans = { SOLUSDT: { entry: 152.4, stop: 148.59, targets: [{ price: 158.12 }, { price: 161.93 }], qty: 0.0688, notional: 10.49, leverage: 3, max_loss_at_stop: 0.26, inr: { currency: "INR", max_loss: 25 }, close_by: now + 23 * 3600000, risk_multiplier: 0.5 } };
    s.today.signals = [sol];
  },
  small: (s) => {
    const now = Date.now(), eth = s.today.coins.find((c) => c.symbol === "ETHUSDT");
    Object.assign(eth, { decision: "SIGNAL SHORT", tier: "small", candidate: { setup: "momentum_after_60m", side: "short", kind: "event", entry: 2200, stop: 2244, target1: 2134, target2: 2090, expires: now + 9 * 60000 } });
    s.signalPlans = Object.assign(s.signalPlans || {}, { ETHUSDT: { entry: 2200, stop: 2244, targets: [{ price: 2134 }, { price: 2090 }], qty: 0.003, notional: 6.6, leverage: 1, max_loss_at_stop: 0.15, inr: { currency: "INR", max_loss: 14 }, close_by: now + 23 * 3600000, risk_multiplier: 0.281, tier: "small" } });
    s.today.signals = (s.today.signals || []).concat([eth]);
  },
  pausedNews: (s) => { const sol = s.today.coins.find((c) => c.symbol === "SOLUSDT"); sol.blocked_signal = { setup: "momentum_after_60m", side: "short", by: ["news check extreme (market)"] }; },
  pausedMin: (s) => { const eth = s.today.coins.find((c) => c.symbol === "ETHUSDT"); eth.blocked_signal = { setup: "momentum_after_60m", side: "long", tier: "small", by: ["order value is below the exchange minimum (5.0 USDT)"] }; },
  wait: (s) => { const b = s.today.coins.find((c) => c.symbol === "BTCUSDT"); Object.assign(b, { decision: "WAIT", why: "rules check: macro blackout: CPI m/m in 40 min" }); },
  notChecked: (s) => { s.today.coins.forEach((c) => Object.assign(c, { decision: "NOT CHECKED", error: true, why: "no market data: Binance and Bybit did not answer", glance: null })); },
  oneFailed: (s) => { Object.assign(s.today.coins[4], { decision: "NOT CHECKED", error: true, why: "no market data", glance: null }); },
  newsExtreme: (s) => { s.today.news_check = { made_at: Date.now() - 3600000, valid_until: Date.now() + 3600000, source: "Claude research", summary: "Exchange hacked",
    market: { level: "extreme", reasons: [{ text: "Major exchange hacked, withdrawals halted", confirmed: true, sources: ["https://example.com/a"] }] }, coins: {} }; },
  newsElevated: (s) => { s.today.news_check = { made_at: Date.now() - 3600000, valid_until: Date.now() + 3600000, source: "Claude research", summary: "Token unlock",
    market: { level: "normal", reasons: [] }, coins: { ETH: { level: "elevated", reasons: [{ text: "Large unlock within 48 hours", confirmed: true, sources: ["https://example.com/b"] }] } } }; },
  newsStale: (s) => { s.today.news_check = { made_at: Date.now() - 40 * 3600000, valid_until: Date.now() - 10 * 3600000, stale: true, source: "Claude research", summary: "old", market: { level: "elevated", reasons: [] }, coins: {} }; },
  noHeadlines: (s) => { s.today.headlines = []; s.today.headlines_ok = false; },
  fedLive: (s) => { s.upcoming = [{ kind: "FOMC", label: "Fed decision", t: Date.now() - 30 * 60000 }].concat(s.upcoming || []); },
  error: (s) => { s.today = null; s.todayError = "the engine stopped: test"; },
};
async function inject(page, names) {
  await page.evaluate((src) => {
    const fns = src.map((x) => new Function("s", "return (" + x + ")(s)"));
    S.today = JSON.parse(JSON.stringify(S.today));
    for (const f of fns) f(S);
    S.answerKey = null; S.coinsShown = false;
    renderStatus(); renderHome();
  }, names.map((n) => STATES[n].toString()));
  await page.waitForTimeout(700);
}

(async () => {
  const srv = await serve();
  const URL = "http://127.0.0.1:" + srv.address().port + "/";
  const browser = await chromium.launch();
  const t0 = Date.now();
  try {
    // 1. Today in every layout and theme, then the coin page, Record and Settings
    const layouts = [["phone-s", { width: 360, height: 740 }], ["phone", { width: 390, height: 844 }], ["tablet", { width: 768, height: 1024 }], ["desktop", { width: 1280, height: 900 }]];
    for (const [lname, vp] of QUICK ? layouts.slice(1, 2) : layouts) {
      for (const theme of ["dark", "light"]) {
        const label = lname + "/" + theme;
        const { ctx, page } = await newPage(browser, label, { ctx: { viewport: vp, colorScheme: theme } });
        await page.goto(URL + "#home");
        if (!(await ready(page))) { fail(label, "the first check never finished"); await ctx.close(); continue; }
        await page.waitForTimeout(1200);
        await audit(page, label + " today", ["Your coins", /No trade today|Signal|paused/]);
        await shot(page, label.replace("/", "-") + "-today");
        for (const [hash, must] of [["record", ["Record", "Scorecard"]], ["settings", ["Settings"]], ["coins", ["Check a coin"]]]) {
          await page.evaluate((h) => { location.hash = h; }, hash);
          await page.waitForTimeout(2500);
          await audit(page, label + " " + hash, must);
        }
        await page.evaluate(() => { location.hash = "coins/SOL"; });
        await page.waitForTimeout(16000);
        await audit(page, label + " coin SOL", ["The five checks", "Chart"]);
        await shot(page, label.replace("/", "-") + "-coin");
        await ctx.close();
      }
    }
    // 2. Every state on Today (phone, dark), with the words each must show
    const cases = [
      [["signal"], ["Buy SOL now.", "I placed it", "Copy the order"]],
      [["small"], ["Sell ETH now.", "Small signal", "a quarter of your normal risk"]],
      [["signal", "small"], ["Buy SOL now.", "Sell ETH now.", "Small signal"]],
      [["pausedNews"], ["SOL rule paused.", "news check paused"]],
      [["pausedMin"], ["ETH small signal paused.", "minimum order"]],
      [["wait"], [/Wait/]],
      [["notChecked"], ["Can't reach the market."]],
      [["oneFailed"], ["Not checked"]],
      [["newsExtreme"], ["Trading paused", "Ignore this check"]],
      [["newsElevated"], ["Caution"]],
      [["newsStale"], ["Out of date"]],
      [["noHeadlines"], ["No news feed answered"]],
      [["fedLive"], [/Wait for/]],
      [["error"], ["Can't check right now."]],
    ];
    {
      const { ctx, page } = await newPage(browser, "states");
      await page.goto(URL + "#home");
      await ready(page);
      await page.waitForTimeout(1000);
      const base = await page.evaluate(() => JSON.stringify({ today: S.today, upcoming: S.upcoming }));
      for (const [names, must] of cases) {
        await page.evaluate((b) => { const o = JSON.parse(b); S.today = o.today; S.upcoming = o.upcoming; S.todayError = null; S.signalPlans = {}; S.todayDemo = false; }, base);
        await inject(page, names);
        await audit(page, "state " + names.join("+"), must);
        await shot(page, "state-" + names.join("+"), false);
      }
      // the example signal and the health list open as sheets, and close
      await page.evaluate((b) => { const o = JSON.parse(b); S.today = o.today; S.todayDemo = true; renderHome(); }, base);
      await page.click("text=See an example signal");
      await page.waitForTimeout(800);
      await audit(page, "example sheet", ["What a signal looks like", "Example only"]);
      await shot(page, "sheet-example", false);
      await page.keyboard.press("Escape");
      await page.click("#status");
      await page.waitForTimeout(500);
      await audit(page, "health sheet", ["How everything is running"]);
      await page.keyboard.press("Escape");
      await ctx.close();
    }
    // 3. Every coin page, another coin, and a coin that does not exist
    {
      const { ctx, page } = await newPage(browser, "coins", { ctx: { viewport: { width: 1280, height: 900 }, colorScheme: "light" } });
      await page.goto(URL + "#home");
      await ready(page);
      for (const k of QUICK ? ["ETH", "DOGE"] : ["BTC", "ETH", "SOL", "XRP", "LINK", "DOGE", "ZZZQQQ"]) {
        await page.evaluate((x) => { location.hash = "coins/" + x; }, k);
        let ok = false;
        for (let i = 0; i < 60 && !ok; i++) { await page.waitForTimeout(1000); ok = await page.evaluate(() => !document.querySelector("#view-coins .progress")); }
        if (!ok) fail("coin " + k, "the coin check never finished");
        // demo data makes up prices for any symbol, so a made-up coin gets a page too (live, Binance refuses it: "couldn't be checked")
        await audit(page, "coin " + k, ["The five checks"]);
        // the chart's controls: every timeframe and indicator toggles without an error
        if (k === "ETH") {
          for (const tf of ["15m", "1h", "1D", "4h"]) { await page.click('[aria-label="Timeframe"] >> text=' + tf); await page.waitForTimeout(1200); }
          for (const ind of ["Key levels", "EMA 200", "Bollinger", "VWAP", "MACD", "RSI"]) { const b = page.locator(".chiprow button", { hasText: ind }); if (await b.count()) { await b.first().click(); await page.waitForTimeout(500); } }
          await page.click('[aria-label="Full screen"]'); await page.waitForTimeout(600); await page.keyboard.press("Escape"); await page.waitForTimeout(400);
          await shot(page, "coin-ETH-desktop");
        }
      }
      // the coin page of a small signal says what it is
      await page.evaluate(() => {
        const st = S.coin.ETH; if (!st || !st.B) return;
        st.B.decision = { decision: "SIGNAL SHORT", why: "small signal", blocks: [] }; st.B.tier = "small";
        st.B.candidate = { setup: "momentum_after_60m", side: "short", kind: "event", entry: 2200, stop: 2244, target1: 2134, expires: Date.now() + 9 * 60000 };
        st.plan = Object.assign({}, st.plan || {}, { setup: "momentum_after_60m", side: "short", tier: "small", entry: 2200, stop: 2244, targets: [{ price: 2134, net_r: 1.3 }], qty: 0.003, notional: 6.6, leverage: 1,
          max_loss_at_stop: 0.15, max_loss_pct_equity: 0.28, stop_distance_pct: 2, close_by: Date.now() + 20 * 3600000, entry_type: "market" });
        st.sim = { verdict: "UNPROVEN (promising): average +0.24R" }; st.chk = { verdict: "PASS", blocks: [], warnings: [] };
        location.hash = "coins/ETH"; renderCoin("ETH");
      });
      await page.waitForTimeout(1500);
      await audit(page, "coin ETH small signal", ["small", "Probably, but not proven"]);
      await ctx.close();
    }
    // 4. Record: scores with small signals, each tab and filter
    {
      const { ctx, page } = await newPage(browser, "record");
      await page.goto(URL + "#home");
      await ready(page);
      await page.evaluate(() => {
        const now = Date.now(), rows = [];
        const add = (kind, R, i, extra) => rows.push(Object.assign({ type: "trade", id: "r" + rows.length, ts: now - i * 86400000, symbol: kind === "small signal" ? "ETHUSDT" : "SOLUSDT", side: "long",
          entry: 100, stop: 97.5, targets: [103.75], decision: "TRACK", track_kind: kind, status: "resolved", setup: "momentum_after_60m", outcome: { R, result: R > 0 ? "target" : "stop", exit_price: 101, exit_t: now - i * 86400000 + 3600000 } }, extra || {}));
        add("signal", 1.4, 3); add("signal", -1.1, 40); add("small signal", 1.3, 3); add("small signal", -1.05, 40); add("agent desk", 0.5, 5);
        rows.push({ type: "trade", id: "you1", ts: now - 86400000, symbol: "ETHUSDT", side: "short", entry: 2200, stop: 2244, targets: [2134], decision: "TAKE", tier: "small", status: "open", setup: "momentum_after_60m" });
        window.call = ((orig) => async (fn, ...a) => (fn === "records" ? JSON.stringify({ rows }) : orig(fn, ...a)))(window.call);
        S.perp = { n: 39, avg_R: 0.55, ci90: [0.26, 0.85], study: { avg_R: 0.61, n: 41 }, checked_at: now, trades: Array.from({ length: 39 }, (_, i) => ({ date: "2021-" + (i % 12 + 1) + "-15", R: i % 3 ? 1.4 : -1.1, outcome: i % 3 ? "target" : "stop" })) };
        location.hash = "record";
      });
      await page.waitForTimeout(2000);
      await audit(page, "record scorecard", ["Small signals", "Engine", "Agent desk"]);
      await shot(page, "record-score");
      for (const tab of ["History", "SOL Fed rule"]) { await page.click(".tabs >> text=" + tab); await page.waitForTimeout(1200); await audit(page, "record " + tab, tab === "History" ? ["Small"] : ["running total"]); }
      await page.click(".tabs >> text=History");
      for (const f of ["Engine", "Small", "Agent desk", "Yours", "Only watched", "All"]) { await page.click(".seg >> text=" + f); await page.waitForTimeout(400); }
      await audit(page, "record filters", ["You, small"]);
      await ctx.close();
    }
    // 5. Settings: every fold opens, the small-signal switch is there, the theme switch works both ways
    {
      const { ctx, page } = await newPage(browser, "settings", { ctx: { colorScheme: "light" } });
      await page.goto(URL + "#settings");
      await ready(page);
      await page.waitForTimeout(1500);
      const folds = await page.locator("details.fold > summary").count();
      for (let i = 0; i < folds; i++) { await page.locator("details.fold > summary").nth(i).click(); await page.waitForTimeout(250); }
      await audit(page, "settings open", ["Small signals", "Chart-setup signals"]);
      await shot(page, "settings-open");
      const dark = page.locator("button", { hasText: /^Dark$/ });
      if (await dark.count()) { await dark.first().click(); await page.waitForTimeout(400); const th = await page.evaluate(() => document.documentElement.dataset.theme); if (th !== "dark") fail("settings", "the dark theme switch did nothing"); }
      else notes.push("settings: no Dark theme button found");
      await ctx.close();
    }
    // 6. Reduced motion: nothing may stay invisible
    {
      const { ctx, page } = await newPage(browser, "reduced", { ctx: { reducedMotion: "reduce" } });
      await page.goto(URL + "#home");
      await ready(page);
      await inject(page, ["signal", "small"]);
      const hidden = await page.evaluate(() => [...document.querySelectorAll(".board li, .tile, .spark, .ticket, .ladder .rung, .meter .fill, .verdict h1")]
        .filter((n) => Number(getComputedStyle(n).opacity) < 0.99).map((n) => n.className || n.tagName));
      if (hidden.length) fail("reduced motion", "left invisible: " + JSON.stringify(hidden.slice(0, 6)));
      await ctx.close();
    }
    // 7. First run with no network to the exchanges (this sandbox blocks them): setup, then an honest "can't reach the market"
    {
      const { ctx, page } = await newPage(browser, "first run", { demo: false });
      await page.goto(URL);
      await page.waitForSelector("#sheet[open]", { timeout: 180000 }).catch(() => fail("first run", "no setup sheet"));
      await audit(page, "first run sheet", ["Set up Trinetra", "Your trading capital"]);
      await page.fill('#sheet input[aria-label="Capital"]', "5000");
      await page.locator("#sheet button", { hasText: /^INR$/ }).first().click();
      await page.waitForTimeout(3000);                     // the rupee rate is fetched; this sandbox blocks it, so type it like a user would
      if (!(await page.inputValue('#sheet input[aria-label="Price of 1 USDT"]'))) await page.fill('#sheet input[aria-label="Price of 1 USDT"]', "95.08");
      await page.locator("#sheet button", { hasText: /^Start$/ }).first().click();
      let done = false;
      for (let i = 0; i < 180 && !done; i++) { await page.waitForTimeout(1000); done = await page.evaluate(() => !!(S.today || S.todayError) && !S.running); }
      const txt = await page.evaluate(() => document.querySelector("#view-home").innerText);
      if (!/Can't reach the market|Can't check right now|No trade|Signal/.test(txt)) fail("first run", "no honest answer after setup: " + txt.slice(0, 160));
      if (/No trade today/.test(txt) && !/Not checked/.test(txt)) notes.push("first run: the exchanges answered from this sandbox");
      await audit(page, "first run answer");
      await shot(page, "first-run-answer", false);
      await ctx.close();
    }
  } finally {
    await browser.close();
    srv.close();
  }
  console.log("\n" + (failures.length ? failures.length + " failures" : "all screens passed") + " in " + Math.round((Date.now() - t0) / 1000) + " s; screenshots in " + OUT);
  for (const n of notes) console.log("note: " + n);
  process.exit(failures.length ? 1 : 0);
})();
