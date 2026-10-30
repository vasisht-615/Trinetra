# Trinetra: project report

*30 October 2026. Covers the work merged in pull request #1 and the work in the follow-up on `claude/watchdog-ntfy-fees`
(open a second pull request to merge it). Every number here comes from a test or replay that can be run again (section 9).*

R is the unit used throughout: 1R is what one trade loses if it hits its stop. +0.5R a trade means you made half
your risk per trade on average. All results are after exchange fees (CoinSwitch PRO rates plus 18% GST), slippage
and, on perpetuals, the funding actually paid.

---

## 0. In one page

**What Trinetra is.** A crypto-futures signal app for BTC, ETH, SOL, XRP and LINK. A tested Python engine runs in the
browser, alongside a news calendar, an optional Claude research check that can only pause or shrink a trade, and a
record that scores every call. It never places orders.

**Why it almost never shows a signal.** A signal needs three things:
- a setup whose own history beat chance after costs;
- an expected value of at least +0.10R;
- every safety rule passing.

On real Binance data from 2019 to 2026, only one rule clears that bar: **SOL, an hour after a Fed decision**.

| SOL rule | Result |
|---|---|
| Trades | 39 |
| Average | **+0.55R** |
| 90% range | +0.26R to +0.85R |
| Hit target / hit stop | 24 / 11 |

There are about 8 Fed decisions a year, so about 6 SOL signals a year.

**What changed in this follow-up.**
- **Small signals for ETH.** ETH's version of the same rule now gives a **small signal** at a quarter of normal risk.
  - It averages +0.24R over 39 Fed nights, but that is not proven: about a 1-in-9 chance it has no edge.
  - It is clearly labelled and scored apart from proven signals.
- **Everything was re-tested**: the engine, every coin and scenario, the app's screens and the server functions.

**Did signals show up when they should?** Yes:

| Test | Result |
|---|---|
| Every Fed night since January 2025, live app path vs the research replay, on real Binance data | 30 coin-nights, **0 disagreements**. SOL signalled on 12 nights, ETH gave a small signal on 10, and neither signalled on nights its first hour did not qualify. |
| 16 Sep 2026, minute by minute | Both signalled from minute 60 to 75, and not before or after. Both hit their first target (+1.40R, +1.37R). |
| 1,680 made-up news-day scenarios | 0 rule violations. 16 signals, all exactly where designed. |
| Every screen and state of the app | All pass. |

**What did not show signals, correctly.**
- **Chart setups on ordinary days.** Over 15 Sep – 30 Oct 2026, replayed every 4 hours (and every hour for 16–30 Oct), no
  setup passed its own history test. That held even with chart signals switched on and the volatility block off.
- **The live market on 30 Oct (Binance hack sell-off).** Four coins had setups, all blocked by the extreme-volatility
  rule.

**The one warning.** Since January 2025, SOL's rule made 12 trades for +1.23R in total (about +0.10R a trade), well
below its long-run +0.55R. That is too few trades to call the edge gone, but it is the thing to watch. The app's
record tracks it live and suspends a rule after a losing record.

---

## 1. What Trinetra is

| Part | Where | What it does |
|---|---|---|
| Engine | `site/engine/scripts/desk.py` (Python, standard library) | Market snapshot, setups, news-day reads, history tests, sizing, rules check, decision, journal. Version **1.4.0**. |
| Engine runtime | `site/worker.js`, `site/bridge.py`, Pyodide 0.26.4 | Runs the engine in a Web Worker in your browser; the browser fetches Binance data itself. |
| App | `site/app.js`, `site/app.css`, `site/index.html` | Today, coin pages, Record, Settings; installable (PWA) with an offline shell (`site/sw.js`). |
| Event study data | `site/engine/references/` | `event_stats.json`, the rule-by-release-by-coin study: 108 cells tested, 1 passed, about 2.7 expected by luck. `release_calendar.json` is the official US schedule (28 Oct 2026 to 15 Dec 2027). `release_history.json` holds 384 releases. |
| Server functions | `netlify/functions/` | `sync` (encrypted device sync), `pair` (link a device by code), `claude` and `mcp` (the Claude connector, encrypted at rest), `feed` (news proxy), `watchdog` (phone alerts every 2 minutes). |
| Hosting | Netlify | Merging to `main` deploys to vigilmoney.netlify.app. |

**How a signal is decided** (`engine_decision`, `run_checks`):
1. **Find a candidate.** A news-day rule fired (read minute by minute from the release), or a chart setup formed on the
   4h chart.
2. **History.** The setup's own past results must be POSITIVE and better than random entries or ordinary days, after
   costs. A small rule needs UNPROVEN-but-positive history.
3. **Expected value.** At least +0.10R after costs.
4. **Rules check.** Stop width, leverage, liquidation buffer, loss limits, open positions, releases inside the hold,
   data age, exchange minimums, and the news check.
5. **Result.**
   - Proven: SIGNAL at half size while your record is new (full size once it proves itself).
   - Small: SIGNAL at a quarter (section 3).
   - Otherwise NO TRADE or WAIT, with the reason.

**What can signal today.**

| Rule | Coin | When | Size |
|---|---|---|---|
| First-hour momentum after a Fed decision: if price has moved more than 0.3% at minute 60, follow it; market order within 15 minutes; stop at least 2.5%; targets 1.5R and 2.5R; hold 24 h, or less when a release follows | SOL | About 8 nights a year | Half while your record is new |
| The same rule, small (stop at least 2%) | ETH | About 8 nights a year | A quarter (section 3) |
| Chart setups (pullback, range fade, squeeze break, level reclaim, crowded fade) | All | Any day | **Off by default** |

Chart setups are off because replays lost money: 3,900 moments in 2020–26 gave 7 signals that passed their own test,
5 filled, 1 made money, average −0.70R. A setup that passes shows as a watch item instead.

---

## 2. What was done

### Pull request #1 (merged into `main`, live)

- **Phone alerts (watchdog).** A Netlify scheduled function runs every 2 minutes and sends ntfy alerts:
  - a reminder an hour before each big US release;
  - a heads-up 10 minutes before a Fed decision;
  - the SOL rule's first-hour answer at minute 61–75.

  It reads Coinbase prices, because Netlify's US servers are refused by Binance. It needs the `NTFY_TOPIC`
  environment variable.
- **Real fees.** CoinSwitch PRO futures fees are 0.025% maker / 0.063% taker, plus 18% GST.
- **Verification suite and the fixes it forced.** See section 5 for the bugs found. Added:
  - a scenario harness (a scripted exchange with the clock pinned);
  - the news-day matrix;
  - edge-case tests;
  - chart-setup tests;
  - a history/live consistency check;
  - the Binance archive replay.
- **Clearer "no signal".** The coin's reason now names the setups the rules hold back (for example "trend pullback
  short, blocked by extreme volatility") instead of "no tradeable setup right now".
- **Redesign.**
  - A new palette in light and dark. Up/down colours are checked for red-green colour blindness and always paired
    with ▲/▼.
  - A market board: 24-hour sparkline, trend on 15m/1h/4h/1D, and what stands between each coin and a signal.
  - A market-pulse row: 24-hour moves, volatility against the blocking line, setups found.
  - A signal ticket with stop, entry and targets on one price scale, a countdown ring to the order deadline, and
    "Copy the order".
  - An example signal.
  - Coin pages with the setups the engine sees, trend and volatility by timeframe, and key levels on the chart.
  - The SOL rule's running total in Record.
  - Calm animations, all switched off by reduced motion.
- **New icon.** A gold third eye between three tilak bars, in every size the browser, Android (maskable) and iPhone
  use.

### This follow-up (branch `claude/watchdog-ntfy-fees`, after pull request #1)

- **Small signals: ETH an hour after Fed decisions** (section 3). Covers the engine, app, Record, Settings, Claude
  connector and phone alerts.
- **Small accounts.** A small signal under the exchange minimum is sized up just enough to clear it, as long as the
  loss at the stop stays within half your normal risk.
- **Fed-night order.** SOL (proven) is checked before ETH (small); each signal has 15 minutes to be placed.
- **Replay tools.**
  - A bug that froze the current month's chart is fixed (section 5).
  - Days missing from the archive are skipped and listed.
  - Minute-level windows work (`--start 2026-09-16T18:50Z --every 5m`).
  - `check_fed_nights.py` compares the live pipeline with the replay night by night.
- **QA.**
  - `tests/app/qa.cjs` tests the whole app in a browser.
  - `tests/functions.test.mjs` tests every server function.
  - 10 new engine tests for small signals; the matrix now expects ETH's small signals.

---

## 3. Small signals (ETH on Fed nights)

**The evidence.**

| Source | Trades | Average | 90% range |
|---|---|---|---|
| ETH's own history (spot study) | 49 Fed days | +0.24R | — |
| Binance perpetuals, funding paid | 39 nights | +0.24R | −0.06R to +0.56R |

- Resampling the nights puts the chance that the true average is zero or below at about 11%. That is why it is
  "small" and not "proven".
- On a Fed night ETH moves the same way as SOL about 9 nights in 10. With SOL's signal, an ETH small signal is mostly
  the same bet made bigger, not a second bet. The app says so on the ticket.

**How it behaves.**
- **Size.** `small_signal_risk` is 0.25: a quarter of your normal risk, whatever your record says. An elevated news
  check halves it again. An extreme news check pauses it, and the app shows "ETH small signal paused".
- **Exchange minimum.** When a quarter would be under the exchange's minimum order, it is sized up in 4% steps to the
  smallest order that clears the minimum. The loss at the stop never goes past half your normal risk; past that, it is
  blocked with "raise your capital or risk per trade".

  | Account | Result |
  |---|---|
  | 5,000 INR at 1% (example) | 0.003 ETH, about 14 INR at the stop (28% of normal risk) |
  | 3,000 INR at 1% | 0.003 ETH (47%) |
  | 5,000 INR at 0.25% | Blocked |
  | 1,00,000 INR at 1% | 0.053 ETH at a plain quarter |

- **Scoring.** It is tracked as its own kind, "small signal". Record shows a "Small signals" score and filter. A trade
  you log from it keeps the tier: it can trip the losing-streak kill switch but never counts toward proving the edge.
- **Off switch.** Settings → Coins and limits → "Small signals".
- **Phone alert.** The Fed-night alert adds ETH's read. If SOL is quiet and ETH moves, the alert is "Small signal: ETH
  BUY/SELL before …".

**Caveat on exchange minimums.** The live app reads each coin's minimum order from Binance at the moment of the signal,
so it uses the true value. In tests the engine's demo exchange uses 5 USDT. From memory (not verified here, because
Binance's API is blocked in the build sandbox), Binance's ETHUSDT perpetual minimum may be 20 USDT. With a 5,000 INR
account at 1%, that would need more than half your normal risk, so ETH small signals would be blocked with the "raise
your capital or risk" message. CoinSwitch's own minimum and lot sizes may differ from Binance's. Check one order screen
on CoinSwitch.

---

## 4. Simulation and test findings

### 4.1 Every news rule on real Binance perpetuals (2019–2026)

`tests/engine/replay_history.py` replays 4 rules × 4 release types × 5 coins (80 combinations) on Binance USD-M
perpetual 1-minute data from data.binance.vision (2,148 files, 0 fetch failures), after costs and funding. The full
table is in the appendix.

**The Fed rule ("momentum after the first hour"):**

| Coin | Nights | Average | 90% range | Target / stop | Spot study |
|---|---|---|---|---|---|
| **SOL** | 39 | **+0.55R** | **+0.26 to +0.85** | 24 / 11 | +0.61R, n=41, POSITIVE |
| ETH | 39 | +0.24R | −0.06 to +0.56 | 19 / 14 | +0.24R, n=49, unproven |
| XRP | 38 | +0.21R | −0.10 to +0.51 | 14 / 12 | +0.21R, n=45, unproven |
| LINK | 43 | +0.17R | −0.14 to +0.49 | 21 / 19 | +0.14R, n=47, unproven |
| BTC | 37 | +0.02R | −0.32 to +0.37 | 16 / 17 | +0.01R, n=44, unproven |

**Each Fed night as one data point.** The coins move together, so the honest unit is the night, not the coin-trade.
Resampled 20,000 times:

| Coins | Average a night | 90% range | Chance the true average is ≤ 0 |
|---|---|---|---|
| SOL | +0.55R | +0.25 to +0.84 | 0.2% |
| ETH | +0.24R | −0.08 to +0.55 | 11% |
| ETH + XRP + LINK | +0.18R | −0.05 to +0.40 | 10% |
| All except SOL | +0.14R | −0.08 to +0.37 | 15% |

**What else the full table shows.**
- **The perpetual results track the spot study closely**, mostly within about 0.1R. So the study holds on the
  instrument you actually trade.
- **Only one other combination has a range above zero:** BTC on producer-price days with the "spike fade" rule
  (+0.49R, range +0.10 to +0.84). That is only 20 trades, and the study marks it "insufficient history". With 80
  combinations, about 4 such results are expected from luck alone. **Watch it on paper; do not trade it.**
- **Many rules lose money and should never be traded:**
  - SOL on jobs days, sweep and reclaim: −0.53R (range −0.72 to −0.32);
  - SOL on Fed nights, spike fade: −0.62R;
  - LINK on Fed nights, spike fade: −0.53R;
  - BTC on producer-price days, sweep and reclaim: −0.42R;
  - ETH on jobs days, first-hour momentum: −0.37R.
- **Funding is negligible** for these holds: −0.008R to +0.007R a trade.

### 4.2 Fed nights through the live app path (since January 2025)

`tests/engine/check_fed_nights.py` runs the app's own `today` at minute 61 of each Fed night. It uses a market rebuilt
from the Binance archive, as of that minute only, with no look-ahead. It then compares each coin with the research
replay.

| Night | SOL, live app | SOL, what happened | ETH, live app | ETH, what happened |
|---|---|---|---|---|
| 29 Jan 2025 | no trade | first hour did not qualify | no trade | did not qualify |
| 19 Mar 2025 | **signal, buy** | stop, −1.15R | **small, buy** | stop, −1.14R |
| 7 May 2025 | **signal, buy** | target, +1.40R | **small, buy** | target, +1.37R |
| 18 Jun 2025 | **signal, sell** | stop, −1.14R | **small, sell** | stop, −1.15R |
| 30 Jul 2025 | **signal, sell** | stop, −1.14R | **small, sell** | stop, −1.15R |
| 17 Sep 2025 | **signal, buy** | target, +1.40R | **small, buy** | target, +1.37R |
| 29 Oct 2025 | **signal, sell** | target, +1.40R | **small, sell** | target, +1.39R |
| 10 Dec 2025 | **signal, buy** | stop, −1.14R | no trade | did not qualify |
| 28 Jan 2026 | no trade | did not qualify | no trade | did not qualify |
| 18 Mar 2026 | **signal, sell** | 24 h close, +0.51R | **small, sell** | stop, −1.15R |
| 29 Apr 2026 | **signal, sell** | 24 h close, −0.57R | **small, sell** | 24 h close, −0.66R |
| 17 Jun 2026 | **signal, sell** | target, +1.40R | **small, sell** | target, +1.38R |
| 29 Jul 2026 | **signal, buy** | stop, −1.14R | no trade | did not qualify |
| 16 Sep 2026 | **signal, buy** | target, +1.40R | **small, buy** | target, +1.37R |
| 28 Oct 2026 | no data | Binance's archive has no file for that day | no data | — |

- **Result:** 30 coin-nights compared, **0 disagreements**.
- **Totals since January 2025:**
  - SOL: 12 trades, **+1.23R** (about +0.10R a trade);
  - ETH: 10 trades, **+1.65R** (about +0.16R a trade).
- These are far below the long-run averages. It is a small sample, but it is the most important thing to watch in
  Record.

**16 September 2026, minute by minute** (`replay_market.py`, every 5 minutes; the decision was at 23:30 IST):

| Time (IST) | Minute | SOL | ETH |
|---|---|---|---|
| 00:25 | 55 | — | — |
| 00:30, 00:35, 00:40, 00:45 | 60 to 75 | **signal, buy**, expected +0.61R | **small signal, buy**, expected +0.24R |
| 00:50 | 80 | — (window closed) | — |

### 4.3 Ordinary days on real data (chart setups)

`tests/engine/replay_market.py` runs the whole app (`today`, all five coins) moment by moment. It uses klines on every
timeframe, funding, open interest and long/short ratios from the archive. Only bars closed at that moment are shown.

| Run | Moments × coins | Setups that reached the full check | Signals | What stopped them |
|---|---|---|---|---|
| 4h chart, every 4 h, 15 Sep – 30 Oct | 270 × 5 | 194 | **0** | No edge in its own history or EV under +0.10R: 114. A big US release inside the hold: 54. History negative: 33. |
| 1h chart, every hour, 16 – 30 Oct | 336 × 5 | 241 | **0** | No edge or low EV: 205. History negative: 34. A release inside the hold: 2. |
| 4h, **chart signals on, volatility block off** | 270 × 5 | 200 | **0** | Same causes: 119, 55, 33. |

So in these 6½ weeks, not one chart setup on any coin had a history that beat chance after costs, even with every
switch opened. A few moments on 28 Oct were skipped (no archive bars). Moments can have more than one block, so the
counts overlap.

### 4.4 The live market on 30 October (the app's own check at 15:15 IST)

| Coin | Price | Setup the engine saw | Why no signal |
|---|---|---|---|
| BTC | 77,686 | Level reclaim, buy (score 45) | Extreme volatility (4h ATR in the top 1% of 200 bars) |
| ETH | 2,229 | Squeeze break, sell (60); level reclaim, buy (30) | Extreme volatility |
| SOL | 96.99 | Trend pullback, sell (65) | Extreme volatility |
| XRP | 1.2552 | None | — |
| LINK | 11.02 | Trend pullback, sell (65) | Extreme volatility |

This was during the Binance hack sell-off. The news check had also flagged the hack. Even without the volatility rule,
chart signals are off by default, so the only possible signal that week was a Fed-night rule.

### 4.5 Made-up news days: the scenario matrix

`tests/engine/run_matrix.py` runs the real engine pipeline 1,680 times. Every scenario combines:
- a coin (5);
- a release type: CPI, PPI, jobs, Fed, GDP or PCE (6);
- a first-hour reaction: flat, up, down, small up, whipsaw, fake-out, spike (7);
- a moment: 90 or 30 minutes before, or 10, 31, 61, 70, 80 or 300 minutes after (8).

**Result: 0 violations.** 16 signals, all where designed: SOL (proven) and ETH (small) at minutes 61 and 70, for the
four reactions that moved more than 0.3%. Every signal's plan was checked:
- the tested stop floor (SOL 2.5%, ETH 2%);
- the first target at 1.5R;
- the order deadline at release + 75 minutes;
- the 12–24 h close;
- the size (half for SOL, a quarter for ETH);
- leverage within the cap;
- the tier label.

### 4.6 Edge cases (engine unit tests)

`python3 -m unittest discover -s tests/engine -p 'test_*.py'` runs 44 tests. They go through the real pipeline on a
scripted exchange with the clock pinned.

- **Timing:** minute 59 no, 60 and 75 yes, 76 no.
- **News check:** extreme pauses and is shown as paused; elevated halves the size; a stale check is ignored;
  unconfirmed reasons don't count; another coin's warning doesn't block SOL.
- **Risk limits:** daily loss, trades per day, open positions, cooldown after losses, rule suspended after a losing
  record, leverage cap, INR capital.
- **Data and calendar:**
  - Binance down: "not checked" via Bybit, never "no trade".
  - Calendar feed down: the tested rule still trades.
  - Missing minutes are filled.
  - Summer-time Fed decision.
  - A release too close blocks the hold.
  - A fake Fed entry that is not on the official schedule never unlocks the rule.
  - High and quiet volatility are warnings for the tested rule.
  - A tiny-price coin works.
- **Whole day:** the Fed-rule coins are checked first (SOL, then ETH); one broken coin doesn't hide the others; a
  signal is tracked once.
- **Chart setups:** the decision rule; history-test triggers match live detection; a pipeline sweep with chart
  signals off and on; blocked setups are named in the reason.
- **Small signals:**
  - a quarter size;
  - the off switch;
  - elevated news halves it;
  - extreme news pauses it (labelled small);
  - no signal before minute 60 or after 75;
  - tracked apart and never proves the edge;
  - a logged trade keeps its tier;
  - the small-account lift to the exchange minimum;
  - too small even at half risk is blocked with what to change;
  - other coins never signal small.

### 4.7 The app in a real browser

`node tests/app/qa.cjs` serves the site with the same security headers as Netlify and drives Chromium. On every
screen it checks:
- no console error, page error or security-policy violation;
- no sideways scrolling;
- every button and link has a name;
- no duplicate ids;
- the words that state must show.

What it covers:
- **Layouts:** small phone (360 px), phone (390), tablet (768) and desktop (1280), each in light and dark. Each shows
  Today, a coin page, Record, Settings and the coin list.
- **14 Today states:**
  - signal; small signal; both;
  - paused by news; paused by the exchange minimum;
  - wait;
  - all coins unreachable; one coin unreachable;
  - news check extreme, elevated, out of date;
  - no headlines;
  - Fed night in progress;
  - engine error.
- **Sheets:** the example signal and the health list.
- **Coin pages:** BTC, ETH, SOL, XRP, LINK, DOGE and a made-up coin, with every timeframe, indicator and full screen.
  Also a small signal's coin page.
- **Record:** with small signals, every tab and every filter.
- **Settings:** every fold, the small-signals switch, and the theme switch.
- **Reduced motion:** nothing left invisible.
- **First run with the exchanges unreachable:** setup, then an honest "can't reach the market".

**Result: all screens pass.**

### 4.8 Server functions

`npm ci && node --test tests/*.test.mjs` runs 12 tests:
- **Watchdog (7):** due windows; Coinbase parsing; gap filling; the SOL rule matching the engine's arithmetic; sends
  once; retries then "open Trinetra"; ETH's small signal in the Fed-night alert.
- **Sync:** compare-and-set versions, conflicts, size and space limits.
- **Pairing:** one use, 10-minute expiry, flood limit.
- **Claude feed:** encrypted at rest, readable only with its token.
- **Connector:** tools; small-signal labels; wrong token; bad JSON-RPC; token in the address.
- **News proxy:** only the four pinned feeds; anything that is not RSS is a 502.

### 4.9 Two facts that shape everything

**Statistical power.** With about 40 Fed nights per coin, a real but modest edge usually fails the test. Chance a
real edge passes a 90%-interval-above-zero test, with per-trade results varying by about 1.2R:

| True edge | n=20 | n=30 | n=60 | n=120 | n=300 |
|---|---|---|---|---|---|
| +0.10R | 10% | 12% | 16% | 23% | 42% |
| +0.20R | 18% | 23% | 36% | 57% | 89% |
| +0.30R | 30% | 39% | 61% | 86% | 100% |

That is why ETH can look real and still be unproven, and why the cure is more data, not a lower bar.

**Costs.** CoinSwitch PRO is 0.025% maker / 0.063% taker, plus 18% GST. A market round trip with slippage costs:
- about 0.14–0.23R on a 1.5% stop;
- about 0.08–0.14R on the Fed rule's 2–2.5% stops.

Limit entries save about 0.05–0.09R. Whether 1% TDS applies to futures trades is unclear (sources disagree), so check
one contract note.

---

## 5. What went wrong, and what was done about it

**Engine bugs the tests found (fixed):**
1. **Quiet weeks blocked the tested SOL rule.** The stop-width check (in ATR units) blocked the rule whenever the 4h
   ATR was under about 0.83%, because the rule's tested 2.5% stop looked "too wide". It is now a warning for tested
   news rules: their stops come from the release hour as tested, and the size shrinks to keep the same loss.
2. **A fake calendar entry could unlock the Fed rule.** Inside the official schedule's range, a Fed, CPI, jobs, PPI,
   GDP or PCE release must now match an official entry within 10 minutes.
3. **WAIT was not reported as paused.** A tested rule held back by a WAIT block now shows as paused, with what it
   would have done.
4. **Crashes on bad data:** zero prices from Bybit, zero-size order-book levels, a zero divisor in recent moves. All
   are now guarded.
5. **History tests did not match live detection.** The history test's setup triggers lacked the higher-timeframe
   filters that live detection uses. They are now aligned (`align_htf`).
6. **"No tradeable setup right now" hid setups the rules held back.** The reason now names them. A display bug
   ("top 5%%") is also fixed.
7. **On Fed nights ETH was checked before SOL** (watchlist order). Proven rules now go first.
8. **Small accounts.** A quarter-size ETH order (4.4 USDT on a 5,000 INR account) fell under the exchange minimum, so
   the small signal could never be placed. It is now lifted to the minimum within half your normal risk (section 3).

**Mistakes in the test tooling (caught and fixed):**
- **The first market replay was invalid.** The replay tool cached the current month's 4h/1h/1d bars on the first day
  it read them, so the engine saw a frozen chart while prices moved. Most setups were then blocked by "price is
  already beyond the stop", and the first result (0 signals) was not a valid replay. The tool now reads each day as
  the clock reaches it and refuses to run a moment whose bars lag the clock. All replays were re-run. The 0-signal
  result above is from the fixed runs.
- **A comparison in the matrix's own check was wrong**, so it flagged correct small signals as violations (Python's
  `a != b == c` is a chained comparison). Fixed.
- **The browser checker flagged buttons in closed folds as unnamed** (false positives). It now uses the browser's own
  visibility test.

**Things outside the code:**
- **Network blocks in the build sandbox.** Binance, Coinbase and most market-data sites are blocked. Real-data replays
  became possible once `data.binance.vision` was allowed. Binance's archive has no files for 28 Oct 2026, so that Fed
  night can't be replayed yet.
- **Pull requests have to be opened by you.** The GitHub token in the sandbox can't open pull requests, so they are
  opened from the compare link.
- **The daily research task has no connectors.** "Trinetra daily research brief" (08:33 IST daily) was created, but
  this organisation does not let scheduled tasks get connectors from here. It runs web-only until the Trinetra (Vigil)
  connector is added in claude.ai. The old disabled task can be deleted.
- **The Fed-night research tasks** live in the Claude desktop app and could not be reached. US clocks changed on
  1 Nov, so they should run at about 00:33, 01:03 and 01:32 IST.

---

## 6. Known limits and open items

- **ETH small signals are unproven**: about a 1-in-9 chance of no edge. Watch the "Small signals" score in Record.
  Turn them off in Settings if you don't want them.
- **SOL's own rule is the only proven one, and it is thin.** It passed 1 of 108 tests and replicates on perpetuals.
  But its last 12 trades averaged about +0.10R, and on BTC the same rule fails, and fails worse with late entry or
  double costs. Size stays at half until your own record proves it.
- **Exchange minimums and lot sizes** come from Binance. CoinSwitch's may differ (section 3).
- **The app must be open at minute 61–75 of a Fed night**, or you act on the phone alert. Alerts need `NTFY_TOPIC` in
  Netlify and the ntfy app.
- **Chart signals stay off.** No chart setup earned a signal in 6½ weeks of real-data replay, even with the switches
  open.
- **TDS on futures**: check a CoinSwitch contract note.
- **Not yet built** (from the research plan):
  - pooling the history across coins;
  - learning slippage from your fills;
  - limit-entry options for the news rules;
  - GDP and PCE studies.

---

## 7. How to test tomorrow

1. **Merge.** Open https://github.com/vasisht-615/trinetra/compare/main...claude/watchdog-ntfy-fees?expand=1 and click
   **Create pull request**, then **Merge pull request**. Netlify deploys in a minute or two.
2. **Reload.** Open vigilmoney.netlify.app and tap **Reload** on the "new version" bar. On a phone app, close and
   reopen it. The footer should say **Engine 1.4.0**.
3. **Settings.** Under Coins and limits, "Small signals" should be on. Check that your capital, currency and risk are right.
4. **Today.** You should see:
   - the market tiles and the coin board;
   - each coin's line saying what blocks it;
   - "See an example signal" (a labelled example, nothing to place).
5. **Demo data** (Settings) shows every screen with made-up prices. Turn it off afterwards. Demo calls are never
   logged.
6. **A real small signal.** Expect one at the next Fed decision, 9 Dec, 14:00 ET (**00:30 IST on 10 Dec**).
   - Keep Trinetra open from about 01:25 IST.
   - If SOL or ETH has moved more than 0.3% by **01:30 IST**, the signals appear with a 15-minute order window, until
     about **01:45 IST**.
   - ETH's ticket is paler gold, says "Small signal", and gives its size.
7. **Record.** A "Small signals" score appears after the first one is tracked.
8. **Claude.** Ask Claude (with the Trinetra connector on) for `vigil_status`. Small signals are labelled "(small)".

---

## 8. Files changed in the follow-up

| File | Change |
|---|---|
| `site/engine/scripts/desk.py` | `SMALL_RULES`, `small_rule`; tier in `analyse_event`, `order_candidates`, `fit_hold_to_calendar`, `cmd_plan` (`--tier`, the minimum-order lift), `engine_decision`, `run_checks`, `track_ideas`, `probation_status`, journal; Fed-night order; config `small_signals`, `small_signal_risk`; version 1.4.0 |
| `site/app.js`, `site/app.css` | Small-signal ticket, chips, alerts, title, coin page, Record score and filter, Settings switch, paused messages, feed tier |
| `netlify/functions/mcp.mjs` | Small signals labelled for Claude |
| `netlify/lib/watchdog.mjs` | ETH's read in the Fed-night alert (`fedRule`) |
| `tests/engine/*` | `run_matrix.py` expects ETH small signals; 10 new edge tests; `check_fed_nights.py`; replay fixes |
| `tests/app/qa.cjs`, `tests/functions.test.mjs` | The browser campaign and the server-function tests |
| `README.md`, `docs/PROJECT_REPORT.md` | How to run every test; this report |

---

## 9. Running every test again

```bash
npm ci
node --test tests/*.test.mjs                                   # server functions (12)
python3 -m unittest discover -s tests/engine -p 'test_*.py'    # engine edge cases (44)
python3 tests/engine/run_matrix.py                             # 1,680 news-day scenarios
node tests/app/qa.cjs                                          # the app in a browser
# real Binance history (needs data.binance.vision):
python3 tests/engine/replay_history.py --out hist.json         # every news rule, type and coin
python3 tests/engine/replay_history.py --kinds FOMC --rules momentum_after_60m --out fomc.json
python3 tests/engine/check_fed_nights.py --since 2025-01-01 --replay fomc.json
python3 tests/engine/replay_market.py --start 2026-09-15 --end 2026-10-30 --every 4h
python3 tests/engine/replay_market.py --start 2026-09-16T18:50Z --end 2026-09-16T19:20Z --every 5m
```

After editing anything in `site/engine`, run `python3 tools/stamp_build.py` so installed apps load the new engine.

---

## Appendix: every news rule on Binance perpetuals (2019–2026)

After fees, slippage and funding. "Study" is the original spot-price study and its verdict.

| Coin | Release | Rule | n | Avg R | 90% range | Tgt | Stop | Study |
|---|---|---|---|---|---|---|---|---|
| BTC | Fed | momentum | 37 | +0.02 | −0.32 to +0.37 | 16 | 17 | +0.01, unproven |
| BTC | Fed | sweep & reclaim | 37 | −0.29 | −0.53 to −0.04 | 5 | 14 | −0.09, no edge |
| BTC | Fed | breakout | 33 | −0.01 | −0.31 to +0.30 | 10 | 11 | −0.01, no edge |
| BTC | Fed | spike fade | 27 | −0.42 | −0.77 to −0.08 | 6 | 18 | −0.33, no edge |
| BTC | CPI | momentum | 26 | −0.34 | −0.67 to +0.01 | 6 | 15 | −0.28, no edge |
| BTC | CPI | sweep & reclaim | 36 | −0.24 | −0.49 to +0.01 | 6 | 11 | −0.21, negative |
| BTC | CPI | breakout | 54 | −0.19 | −0.39 to +0.01 | 7 | 18 | −0.19, negative |
| BTC | CPI | spike fade | 17 | +0.08 | −0.38 to +0.58 | 8 | 7 | +0.13, insufficient |
| BTC | Jobs | momentum | 49 | −0.20 | −0.45 to +0.06 | 15 | 24 | −0.08, no edge |
| BTC | Jobs | sweep & reclaim | 34 | −0.27 | −0.49 to −0.02 | 5 | 11 | −0.23, negative |
| BTC | Jobs | breakout | 48 | −0.38 | −0.58 to −0.17 | 5 | 21 | −0.34, negative |
| BTC | Jobs | spike fade | 26 | −0.10 | −0.45 to +0.24 | 7 | 12 | +0.00, unproven |
| BTC | PPI | momentum | 35 | −0.15 | −0.48 to +0.17 | 12 | 17 | −0.11, no edge |
| BTC | PPI | sweep & reclaim | 56 | −0.42 | −0.58 to −0.26 | 4 | 19 | −0.30, negative |
| BTC | PPI | breakout | 36 | −0.29 | −0.52 to −0.06 | 4 | 12 | −0.16, no edge |
| BTC | PPI | spike fade | 20 | +0.49 | +0.10 to +0.84 | 10 | 4 | +0.50, insufficient |
| ETH | Fed | momentum | 39 | +0.24 | −0.06 to +0.56 | 19 | 14 | +0.24, unproven |
| ETH | Fed | sweep & reclaim | 36 | +0.01 | −0.22 to +0.22 | 6 | 5 | −0.04, insufficient |
| ETH | Fed | breakout | 29 | −0.01 | −0.26 to +0.26 | 4 | 8 | −0.00, insufficient |
| ETH | Fed | spike fade | 26 | −0.50 | −0.79 to −0.18 | 4 | 17 | −0.49, negative |
| ETH | CPI | momentum | 28 | −0.06 | −0.42 to +0.31 | 10 | 14 | +0.03, unproven |
| ETH | CPI | sweep & reclaim | 34 | −0.25 | −0.49 to −0.01 | 4 | 11 | −0.21, no edge |
| ETH | CPI | breakout | 58 | −0.13 | −0.30 to +0.04 | 5 | 15 | −0.11, no edge |
| ETH | CPI | spike fade | 21 | −0.09 | −0.50 to +0.36 | 8 | 11 | −0.14, no edge |
| ETH | Jobs | momentum | 56 | −0.37 | −0.59 to −0.14 | 12 | 33 | −0.32, negative |
| ETH | Jobs | sweep & reclaim | 33 | −0.31 | −0.53 to −0.07 | 3 | 12 | −0.23, negative |
| ETH | Jobs | breakout | 50 | −0.16 | −0.36 to +0.05 | 7 | 16 | −0.14, no edge |
| ETH | Jobs | spike fade | 28 | −0.23 | −0.55 to +0.10 | 7 | 14 | −0.02, no edge |
| ETH | PPI | momentum | 43 | −0.04 | −0.34 to +0.26 | 15 | 21 | −0.14, no edge |
| ETH | PPI | sweep & reclaim | 47 | −0.07 | −0.25 to +0.12 | 6 | 9 | +0.02, unproven |
| ETH | PPI | breakout | 39 | −0.07 | −0.29 to +0.16 | 6 | 9 | −0.10, insufficient |
| ETH | PPI | spike fade | 17 | +0.24 | −0.25 to +0.72 | 9 | 7 | +0.35, unproven |
| SOL | Fed | **momentum** | 39 | **+0.55** | **+0.26 to +0.85** | 24 | 11 | **+0.61, positive** |
| SOL | Fed | sweep & reclaim | 31 | −0.39 | −0.63 to −0.13 | 3 | 13 | −0.36, negative |
| SOL | Fed | breakout | 32 | +0.19 | −0.10 to +0.48 | 10 | 9 | +0.20, unproven |
| SOL | Fed | spike fade | 25 | −0.62 | −0.89 to −0.31 | 3 | 18 | −0.60, negative |
| SOL | CPI | momentum | 27 | −0.11 | −0.48 to +0.28 | 10 | 15 | −0.10, no edge |
| SOL | CPI | sweep & reclaim | 29 | −0.23 | −0.53 to +0.07 | 5 | 12 | −0.22, no edge |
| SOL | CPI | breakout | 59 | −0.18 | −0.35 to +0.01 | 7 | 20 | −0.13, no edge |
| SOL | CPI | spike fade | 17 | +0.12 | −0.36 to +0.60 | 8 | 7 | +0.13, insufficient |
| SOL | Jobs | momentum | 59 | −0.04 | −0.27 to +0.21 | 20 | 27 | −0.04, no edge |
| SOL | Jobs | sweep & reclaim | 39 | −0.53 | −0.72 to −0.32 | 2 | 19 | −0.44, negative |
| SOL | Jobs | breakout | 49 | −0.33 | −0.53 to −0.13 | 6 | 19 | −0.32, negative |
| SOL | Jobs | spike fade | 24 | +0.15 | −0.22 to +0.49 | 9 | 7 | +0.10, unproven |
| SOL | PPI | momentum | 42 | −0.24 | −0.51 to +0.05 | 11 | 22 | −0.20, no edge |
| SOL | PPI | sweep & reclaim | 50 | −0.02 | −0.22 to +0.19 | 8 | 11 | +0.04, unproven |
| SOL | PPI | breakout | 41 | −0.23 | −0.43 to −0.03 | 3 | 12 | −0.23, negative |
| SOL | PPI | spike fade | 17 | +0.13 | −0.30 to +0.57 | 6 | 6 | +0.14, insufficient |
| XRP | Fed | momentum | 38 | +0.21 | −0.10 to +0.51 | 14 | 12 | +0.21, unproven |
| XRP | Fed | sweep & reclaim | 34 | −0.24 | −0.42 to −0.07 | 1 | 7 | −0.24, insufficient |
| XRP | Fed | breakout | 32 | +0.11 | −0.12 to +0.34 | 4 | 5 | +0.07, insufficient |
| XRP | Fed | spike fade | 27 | −0.40 | −0.70 to −0.09 | 5 | 15 | −0.45, negative |
| XRP | CPI | momentum | 30 | −0.26 | −0.55 to +0.06 | 5 | 15 | −0.27, no edge |
| XRP | CPI | sweep & reclaim | 35 | −0.01 | −0.24 to +0.22 | 4 | 9 | +0.07, insufficient |
| XRP | CPI | breakout | 55 | −0.16 | −0.33 to +0.02 | 7 | 13 | −0.21, negative |
| XRP | CPI | spike fade | 21 | −0.05 | −0.45 to +0.34 | 6 | 10 | −0.04, no edge |
| XRP | Jobs | momentum | 53 | −0.24 | −0.45 to −0.01 | 9 | 21 | −0.26, negative |
| XRP | Jobs | sweep & reclaim | 46 | −0.00 | −0.20 to +0.20 | 8 | 8 | −0.08, no edge |
| XRP | Jobs | breakout | 43 | −0.23 | −0.41 to −0.04 | 3 | 10 | −0.23, insufficient |
| XRP | Jobs | spike fade | 15 | +0.08 | −0.35 to +0.50 | 4 | 4 | +0.12, insufficient |
| XRP | PPI | momentum | 45 | −0.15 | −0.42 to +0.14 | 13 | 21 | −0.23, no edge |
| XRP | PPI | sweep & reclaim | 53 | −0.15 | −0.31 to +0.01 | 4 | 10 | −0.19, insufficient |
| XRP | PPI | breakout | 38 | −0.05 | −0.24 to +0.15 | 4 | 6 | −0.11, insufficient |
| XRP | PPI | spike fade | 15 | −0.08 | −0.53 to +0.39 | 4 | 7 | −0.01, insufficient |
| LINK | Fed | momentum | 43 | +0.17 | −0.14 to +0.49 | 21 | 19 | +0.14, unproven |
| LINK | Fed | sweep & reclaim | 38 | −0.18 | −0.42 to +0.05 | 5 | 10 | −0.14, no edge |
| LINK | Fed | breakout | 40 | −0.05 | −0.27 to +0.18 | 5 | 11 | −0.06, no edge |
| LINK | Fed | spike fade | 23 | −0.53 | −0.85 to −0.18 | 4 | 15 | −0.53, negative |
| LINK | CPI | momentum | 32 | −0.21 | −0.52 to +0.14 | 9 | 17 | −0.23, no edge |
| LINK | CPI | sweep & reclaim | 30 | −0.23 | −0.48 to +0.03 | 3 | 10 | −0.27, negative |
| LINK | CPI | breakout | 59 | −0.14 | −0.31 to +0.04 | 7 | 16 | −0.12, no edge |
| LINK | CPI | spike fade | 21 | +0.20 | −0.24 to +0.65 | 10 | 9 | +0.15, unproven |
| LINK | Jobs | momentum | 68 | −0.10 | −0.32 to +0.12 | 18 | 30 | −0.06, no edge |
| LINK | Jobs | sweep & reclaim | 45 | −0.40 | −0.61 to −0.19 | 5 | 17 | −0.39, negative |
| LINK | Jobs | breakout | 56 | −0.34 | −0.51 to −0.18 | 3 | 22 | −0.30, negative |
| LINK | Jobs | spike fade | 23 | −0.19 | −0.57 to +0.19 | 7 | 11 | −0.19, no edge |
| LINK | PPI | momentum | 49 | −0.04 | −0.30 to +0.22 | 15 | 21 | −0.03, no edge |
| LINK | PPI | sweep & reclaim | 48 | −0.07 | −0.28 to +0.15 | 7 | 9 | −0.11, no edge |
| LINK | PPI | breakout | 44 | −0.17 | −0.36 to +0.04 | 2 | 12 | −0.19, negative |
| LINK | PPI | spike fade | 20 | +0.02 | −0.39 to +0.48 | 8 | 10 | −0.01, no edge |

Targets plus stops can be fewer than n: the rest closed on the 24-hour time stop.
