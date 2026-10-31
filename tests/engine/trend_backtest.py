"""The weekly trend rule's long test on real Binance USD-M perpetual history, with the engine's own code.

    python3 tests/engine/trend_backtest.py --cache ~/.cache/trinetra-binance [--write]

It reads daily bars and funding for BTC, ETH and SOL from data.binance.vision, runs desk.py's trend_path (the same
function the app runs live) from each coin's first full vote, and sums the three coins up together: the average a
year, the swings, the Sharpe ratio with a 90% range, the worst fall, each year, and three robustness checks (trading a
day late, no costs, and simply holding the coins instead). --write saves site/engine/references/trend_backtest.json,
which the app shows as the rule's tested record.
"""
import argparse
import datetime as dt
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import desk  # noqa: E402
from replay_history import Archive, RECENT  # noqa: E402

DAY = 86400000
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "site", "engine", "references", "trend_backtest.json")


def months(a, b):
    y, m = map(int, a.split("-"))
    out = []
    while "%04d-%02d" % (y, m) <= b:
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def daily_bars(A, sym, until):
    rows = []
    for m in months("2019-09", until.strftime("%Y-%m")):
        r = A.klines(sym, "1d", month=m)
        if not r and m >= RECENT:                      # this month's file is not out yet: its daily files are
            y, mo = map(int, m.split("-"))
            d = dt.date(y, mo, 1)
            while d.month == mo and d <= until:
                rows += A.klines(sym, "1d", day=d)
                d += dt.timedelta(days=1)
        rows += r
    bars = {b["t"]: b for b in desk.parse_binance_klines(rows)}
    return [bars[t] for t in sorted(bars) if bars[t]["T"] < (desk.day_ms(until.isoformat()) + DAY)]


def daily_funding(A, sym, until):
    out = {}
    for m in months("2019-09", until.strftime("%Y-%m")):
        for r in A.funding(sym, m):
            k = int(r[0]) // DAY
            out[k] = out.get(k, 0.0) + float(r[2])
    return out


def by_year(rows):
    """Each calendar year's own result (compounded within the year); a part year says how many days it has."""
    ys = {}
    for t, p, *_ in rows:
        ys.setdefault(dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).year, []).append((t, p))
    out = {}
    for y, v in sorted(ys.items()):
        total, worst = desk.trend_growth(v)
        out[str(y)] = {"total_pct": round(total, 2), "worst_drop_pct": round(worst, 2), "days": len(v)}
    return out


def boot_sharpe(xs, block=20, n=2000, seed=3):
    """A 90% range for the Sharpe ratio: resample 20-day blocks, so calm and wild stretches stay together."""
    rng = random.Random(seed)
    k, out = len(xs), []
    for _ in range(n):
        x = []
        while len(x) < k:
            j = rng.randrange(0, k - block)
            x += xs[j:j + block]
        x = x[:k]
        s = statistics.pstdev(x)
        out.append(statistics.mean(x) / s * (365 ** 0.5) if s else 0.0)
    out.sort()
    return [round(out[int(0.05 * n)], 2), round(out[int(0.95 * n)], 2)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/trinetra-binance"))
    ap.add_argument("--until", default=(dt.date.today() - dt.timedelta(days=1)).isoformat(), help="last daily bar (UTC date)")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    A = Archive(a.cache)
    until = dt.date.fromisoformat(a.until)
    cfg = json.loads(json.dumps(desk.DEFAULTS))
    data, runs = {}, {"base": {}, "late": {}, "free": {}, "hold": {}}
    days_of_week = {d: {} for d in range(7)}
    coins = {}
    for c in desk.TREND_COINS:
        sym = desk.norm_symbol(c)
        bars, fund = daily_bars(A, sym, until), daily_funding(A, sym, until)
        if len(bars) <= max(desk.TREND_LOOKBACKS) + 30:
            sys.exit("%s: not enough daily bars in the archive (%d)" % (c, len(bars)))
        cost = desk.cost_model(cfg, sym, "market")["entry"]
        checks, daily, _ = desk.trend_path(bars, fund, cost)
        runs["base"][c] = daily
        runs["late"][c] = desk.trend_path(bars, fund, cost, lag=1)[1]
        runs["free"][c] = desk.trend_path(bars, {}, 0.0)[1]
        for d in range(7):
            days_of_week[d][c] = desk.trend_path(bars, fund, cost, weekday=d)[1]
        t0 = daily[0][0]
        closes = {b["t"]: b["c"] for b in bars}
        ts = [b["t"] for b in bars]
        runs["hold"][c] = [(ts[i], closes[ts[i]] / closes[ts[i - 1]] - 1 - fund.get(ts[i] // DAY, 0.0), 1.0)
                           for i in range(1, len(ts)) if ts[i] >= t0]
        years = (daily[-1][0] - daily[0][0]) / (365.0 * DAY)
        trades = sum(1 for x in checks if x["action"] not in ("HOLD", "OUT"))
        coins[c] = dict(desk.trend_stats(daily), first=dt.datetime.fromtimestamp(t0 / 1000, dt.timezone.utc).date().isoformat(),
                        trades_a_year=round(trades / years, 1), in_market_pct=round(100.0 * sum(1 for x in daily if x[2] > 0) / len(daily), 1),
                        avg_size=round(statistics.mean(x[2] for x in daily if x[2] > 0), 3), cost_per_trade_pct=round(cost * 100, 4))
        data[c] = {"bars": len(bars), "funding_days": len(fund)}
        print("%s: %d daily bars %s to %s, funding on %d days; %s" % (
            c, len(bars), dt.datetime.fromtimestamp(bars[0]["t"] / 1000, dt.timezone.utc).date(),
            dt.datetime.fromtimestamp(bars[-1]["t"] / 1000, dt.timezone.utc).date(), len(fund), coins[c]))
    port = {k: desk.trend_portfolio(v) for k, v in runs.items()}
    base = port["base"]
    res = {
        "made_by": "tests/engine/trend_backtest.py (desk.py %s trend_path, the function the app runs live)" % desk.VERSION,
        "source": "Binance USD-M perpetual daily bars and funding from data.binance.vision",
        "rule": desk.TREND_RULE,
        "params": {"coins": list(desk.TREND_COINS), "lookbacks_days": list(desk.TREND_LOOKBACKS), "vol_target_a_year": desk.TREND_VOL,
                   "ewma": desk.TREND_EWMA, "cap": desk.TREND_CAP, "band": desk.TREND_BAND, "check": "Sunday close (Monday 00:00 UTC)"},
        "costs": "taker fee %.3f%% + %.0f%% GST + slippage (BTC 0.03%%, ETH 0.04%%, SOL 0.10%%) on each change; funding paid while long"
                 % (cfg["fees"]["taker"] * 100, cfg["gst_on_fees_pct"]),
        "from": dt.datetime.fromtimestamp(base[0][0] / 1000, dt.timezone.utc).date().isoformat(),
        "to": dt.datetime.fromtimestamp(base[-1][0] / 1000, dt.timezone.utc).date().isoformat(),
        "portfolio": desk.trend_stats(base),
        "sharpe_90": boot_sharpe([p for _, p in base]),
        "years": by_year(base),
        "coins": coins,
        "late_by_a_day": desk.trend_stats(port["late"]),
        "no_costs": desk.trend_stats(port["free"]),
        "hold_instead": desk.trend_stats(port["hold"]),
        "hold_years": by_year(port["hold"]),
        "check_day": {name: desk.trend_stats(desk.trend_portfolio(days_of_week[d]))
                      for d, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))},
        "note": ("Results are fractions of the trend share, added up (not compounded), with the three coins sharing it equally "
                 "(before SOL's first full vote in 2021, BTC and ETH share it). The rule was chosen from a research pass on "
                 "the same history, so this is the record of a reasonable rule, not a promise: the live record since "
                 "%s is the honest test." % desk.TREND_ANCHOR),
    }
    P = res["portfolio"]
    print("\nPORTFOLIO %s to %s: %+.1f%% a year compounded (average %+.1f%%), swings %.1f%%, Sharpe %.2f (90%% %.2f to %.2f), "
          "worst fall %.1f%%, total %+.0f%%" % (res["from"], res["to"], P["growth_a_year_pct"], P["avg_a_year_pct"], P["vol_pct"], P["sharpe"],
                                               res["sharpe_90"][0], res["sharpe_90"][1], P["worst_drop_pct"], P["total_pct"]))
    print("by year: " + "; ".join("%s %+.1f%% (%d days)" % (y, v["total_pct"], v["days"]) for y, v in res["years"].items()))
    for k in ("late_by_a_day", "no_costs", "hold_instead"):
        v = res[k]
        print("%-14s %+.1f%% a year, Sharpe %.2f, worst fall %.1f%%" % (k, v["growth_a_year_pct"], v["sharpe"], v["worst_drop_pct"]))
    print("hold by year: " + "; ".join("%s %+.1f%%" % (y, v["total_pct"]) for y, v in res["hold_years"].items()))
    print("check day: " + "; ".join("%s %+.1f%%/yr S %.2f worst %.1f%%" % (k[:3], v["growth_a_year_pct"], v["sharpe"], v["worst_drop_pct"])
                                    for k, v in res["check_day"].items()))
    print("downloads %d, failures %d" % (A.downloads, len(A.failures)))
    if A.failures:
        sys.exit("archive files could not be fetched: %s" % A.failures[:5])
    if a.write:
        desk.write_json(os.path.normpath(OUT), res)
        print("wrote", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
