"""The weekly trend part, live path vs the archive: run `desk.py trend` and `today` as the app does, with the clock pinned
and Binance's answers served from data.binance.vision, and check every weekly check since the anchor against
trend_path run directly on the archive's daily bars.

    python3 tests/engine/check_trend_live.py --cache ~/.cache/trinetra-binance [--now "2026-10-30 06:00"]

It checks the data plumbing the app depends on (paging daily bars, cutting the forming bar, funding pages, the
exchange minimums), the sizes for a few capital levels, and that the Monday check lands on Monday 00:00 UTC.
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import desk  # noqa: E402
from replay_history import Archive, HistoricalExchange  # noqa: E402

# Lot filters for the test only (Binance's own values are not reachable from every network): step and minimums.
LOTS = {"BTCUSDT": ("0.001", "0.001", "100"), "ETHUSDT": ("0.001", "0.001", "20"), "SOLUSDT": ("0.01", "0.01", "5")}


class PinnedExchange(HistoricalExchange):
    """The archive as Binance, at a pinned time: a request with no end time ends now, and exchangeInfo answers."""

    def __init__(self, archive, now):
        HistoricalExchange.__init__(self, archive)
        self.now = now
        self.calls = []

    def __call__(self, url):
        u = urllib.parse.urlparse(url)
        self.calls.append(u.path)
        if u.path == "/fapi/v1/exchangeInfo":
            return json.dumps({"symbols": [{"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                                            "baseAsset": s[:-4], "filters": [{"filterType": "LOT_SIZE", "stepSize": st, "minQty": mq},
                                                                             {"filterType": "MIN_NOTIONAL", "notional": mn},
                                                                             {"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}
                                           for s, (st, mq, mn) in LOTS.items()]})
        if u.path == "/fapi/v1/klines":
            q = dict(urllib.parse.parse_qsl(u.query))
            if "endTime" not in q and "startTime" not in q:
                url = url + "&endTime=%d" % self.now
        if u.path == "/fapi/v1/fundingRate":
            q = dict(urllib.parse.parse_qsl(u.query))
            if int(q.get("endTime", 0)) > self.now:
                return json.dumps([])
        out = HistoricalExchange.__call__(self, url)
        if u.path == "/fapi/v1/klines":
            rows = [r for r in json.loads(out or "[]") if int(r[0]) <= self.now]
            for r in rows:                               # the bar that is still forming at the pinned time
                if int(r[6]) >= self.now:
                    r[6] = int(r[6])
            out = json.dumps(rows)
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/trinetra-binance"))
    ap.add_argument("--now", default="2026-10-30 06:00")
    a = ap.parse_args()
    now = int(dt.datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    A = Archive(a.cache)
    ex = PinnedExchange(A, now)
    fails = []

    def check(ok, what):
        print(("  ok   " if ok else "  FAIL ") + what)
        if not ok:
            fails.append(what)

    home = tempfile.mkdtemp(prefix="trend-live-")
    for eq, cur in ((1000.0, "USDT"), (5000.0, "INR"), (50000.0, "INR")):
        cfg = {"equity": eq, "equity_currency": cur, "usdt_fx": 88.0 if cur == "INR" else None, "tz": "Asia/Kolkata"}
        desk.write_json(os.path.join(home, "config.json"), cfg)
        desk.DOWN_HOSTS.clear()
        desk.set_now(str(now))
        net = desk.Net(home=home, fetcher=ex)
        conf = desk.load_config(home)
        R = desk.trend_report(net, conf, now)
        print("\ncapital %s %s: errors %s" % (eq, cur, R["errors"]))
        check(not [e for e in R["errors"] if "minimums" not in e], "no data errors")
        check(len(R["coins"]) == 3, "three coins read")
        check(R["next_check"] % desk.WEEK_MS == desk.MONDAY_OFFSET_MS % desk.WEEK_MS and R["next_check"] > now
              and R["next_check"] - now <= desk.WEEK_MS, "next check is the coming Monday 00:00 UTC (%s)" % desk.utc(R["next_check"]))
        start, frm = desk.day_ms(desk.TREND_ANCHOR), desk.day_ms(desk.TREND_FROM)
        for row in R["coins"]:
            sym = row["symbol"]
            bars = [b for b in desk.parse_binance_klines(json.loads(HistoricalExchange.__call__(
                ex, desk.bn_url("/fapi/v1/klines", symbol=sym, interval="1d", startTime=frm, limit=1500))))
                    if b["T"] < now]
            fund = {}
            for r in json.loads(HistoricalExchange.__call__(ex, desk.bn_url("/fapi/v1/fundingRate", symbol=sym, startTime=start,
                                                                             endTime=(now // desk.DAY_MS) * desk.DAY_MS - 1, limit=1000))):
                k = int(r["fundingTime"]) // desk.DAY_MS
                fund[k] = fund.get(k, 0.0) + float(r["fundingRate"])
            checks, daily, _ = desk.trend_path(bars, fund, desk.cost_model(conf, sym, "market")["entry"], start_ms=start)
            ck = checks[-1]
            check(abs(row["w"] - round(ck["w"], 3)) < 1e-9 and row["action"] == ck["action"] and row["checked_at"] == ck["t"],
                  "%s: live check %s matches the archive (%s, size %.3f, %d of 5 up)" % (sym, desk.utc(ck["t"]), ck["action"], ck["w"], row["ups"]))
            check(desk.weekday_of(ck["t"]) == 0, "%s: the check is at a Monday 00:00 UTC" % sym)
            weeks = [w.get(row["coin"]) for w in R["model"]["weeks"]]
            arch = [{"w": round(x["w"], 3), "action": x["action"]} for x in checks][-len(weeks):]
            check([{"w": w["w"], "action": w["action"]} for w in weeks] == arch, "%s: all %d weekly checks since %s match"
                  % (sym, len(arch), desk.TREND_ANCHOR))
            s = row["size"]
            want = conf["equity"] * conf["trend_share"] / 3 * ck["w"]
            check(abs(s["usdt"] - round(want, 2)) < 0.011, "%s: size %.2f USDT = capital x share / 3 x size" % (sym, s["usdt"]))
            if ck["w"] > 0:
                px = row["price"]
                check(s["qty"] * px <= want + 1e-6, "%s: the rounded quantity never exceeds the size" % sym)
                check(s["below_min"] == (s["qty"] * px < float(LOTS[sym][2]) - 1e-9), "%s: below-minimum flag right (%s)"
                      % (sym, s["below_min"]))
            print("     %s %s: %s, %d of 5 up, size %.3f, %s USDT (%s), qty %s%s" % (
                sym, row["state"], row["action"], row["ups"], row["w"], s["usdt"], s.get("local"), s["qty"],
                (", needs %s USDT capital" % row["needs_capital_usdt"]) if row.get("needs_capital_usdt") else ""))
        m = R["model"]
        print("  model since %s: %+.2f%% (last 52 weeks %+.2f%%), worst fall %.2f%%" % (m["since"], m["total_pct"], m["last_52w_pct"],
                                                                                      m["worst_drop_pct"]))
        check(R.get("tested", {}).get("portfolio") is not None, "the long test file is read")
    # today carries the trend part, and a failure in it cannot hide the day
    cfg = {"equity": 1000.0, "equity_currency": "USDT"}
    desk.write_json(os.path.join(home, "config.json"), cfg)
    code, out = H.run(["today", "--coins", "BTC"], now, ex, home=home)
    T = out if code == 0 and isinstance(out, dict) else {}
    check(bool(T.get("trend")) and len(T["trend"]["coins"]) == 3, "today includes the trend part (exit %s)" % code)
    check(not [e for e in T.get("errors", []) if "trend" in e], "today's trend part has no errors")
    print("\n%d checks failed" % len(fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
