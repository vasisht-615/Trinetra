"""Replay the whole engine on real Binance history, moment by moment, with no look-ahead.

    python3 tests/engine/replay_market.py --start 2026-09-15 --end 2026-10-30 --every 4h [--primary 1h] [--chart-signals]

At each moment the engine runs `today` (all five coins) with the clock pinned there, against a market rebuilt from
data.binance.vision: klines on every timeframe (only bars closed by then), funding, open interest and long/short
ratios (the 5-minute metrics files), and the US release calendar for that week. It records every decision, every
candidate and exactly what stopped it, and on news days reads the release minute by minute as the app does.
"""
import argparse
import collections
import datetime as dt
import email.utils
import json
import os
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import desk  # noqa: E402
from replay_history import RECENT, Archive, months_between, utc_date  # noqa: E402

MIN, HOUR, DAY = 60000, 3600000, 86400000
FF_TITLE = dict(H.FF_TITLE)


def _metrics_time(s):
    s = s.strip()
    if s.isdigit():
        return int(s)
    return int(dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


class Market(object):
    """Binance futures endpoints answered from the archive, as of `self.now` (set before each run)."""

    def __init__(self, archive):
        self.a = archive
        self.now = None
        self.mem = {}

    # ---------------------------------------------------------------- data
    def bars(self, sym, tf, start, end):
        out = []
        if tf in ("1m", "5m"):
            d = utc_date(start)
            while d <= utc_date(end):
                key = (sym, tf, d)
                if key not in self.mem:
                    self.mem[key] = self.a.klines(sym, tf, day=d)
                out += self.mem[key]
                d += dt.timedelta(days=1)
        else:
            for m in months_between(start, end):
                key = (sym, tf, m)
                if key not in self.mem:
                    rows = self.a.klines(sym, tf, month=m)
                    if not rows and m >= RECENT:            # the latest month or two are published day by day
                        y, mo = map(int, m.split("-"))
                        d = dt.date(y, mo, 1)
                        while d.month == mo and d <= utc_date(end):
                            rows += self.a.klines(sym, tf, day=d)
                            d += dt.timedelta(days=1)
                    self.mem[key] = rows
                out += self.mem[key]
        # only bars that had closed by now: the replay never sees the future
        return [r for r in out if start <= int(r[0]) <= end and int(r[6]) < self.now]

    def funding(self, sym, start, end):
        rows = []
        for m in months_between(start, end):
            key = (sym, "funding", m)
            if key not in self.mem:
                self.mem[key] = self.a.funding(sym, m)
            rows += self.mem[key]
        return [r for r in rows if start <= int(r[0]) <= min(end, self.now)]

    def metrics(self, sym, start, end):
        out = []
        d = utc_date(start)
        while d <= utc_date(end):
            key = (sym, "metrics", d)
            if key not in self.mem:
                self.mem[key] = self.a.rows("daily/metrics/%s/%s-metrics-%s.zip" % (sym, sym, d.isoformat())) or []
            out += self.mem[key]
            d += dt.timedelta(days=1)
        rows = []
        for r in out:
            try:
                t = _metrics_time(r[0])
            except ValueError:
                continue
            if start <= t <= min(end, self.now):
                rows.append((t, r))
        return sorted(rows, key=lambda x: x[0])

    def last_price(self, sym):
        for tf in ("1m", "15m", "1h"):
            rows = self.bars(sym, tf, self.now - 2 * DAY, self.now)
            if rows:
                return float(rows[-1][4])
        return None

    def releases(self, t_from, t_to):
        cal = desk.load_release_calendar() or {}
        rel = list(cal.get("releases") or []) + [dict(r, title=FF_TITLE.get(r["kind"], r["kind"])) for r in desk.load_release_history()]
        seen, out = set(), []
        for r in rel:
            if t_from <= r["t"] <= t_to and (r["kind"], r["t"]) not in seen:
                seen.add((r["kind"], r["t"]))
                out.append(r)
        return out

    # ---------------------------------------------------------------- endpoints
    def __call__(self, url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        host, path = u.netloc, u.path
        if host == "nfs.faireconomy.media":
            out = []
            for r in self.releases(self.now - 7 * DAY, self.now + 7 * DAY):
                et = desk.utc(r["t"]).astimezone(dt.timezone(dt.timedelta(hours=-4 if desk.us_dst(r["t"]) else -5)))
                out.append({"title": FF_TITLE.get(r["kind"], r.get("title")), "country": "USD", "date": et.isoformat(), "impact": "High",
                            "forecast": None, "previous": None})
            return json.dumps(out)
        if host in ("www.coindesk.com", "cointelegraph.com", "decrypt.co", "www.theblock.co"):
            return "<?xml version='1.0'?><rss><channel></channel></rss>"
        if host != "fapi.binance.com":
            return None                                      # fear & greed, Deribit, CoinGecko: not archived; the engine copes
        sym = q.get("symbol")
        if path == "/fapi/v1/time":
            return json.dumps({"serverTime": self.now})
        if path == "/fapi/v1/klines":
            tf, lim = q["interval"], int(q.get("limit", 500))
            span = desk.TF_MIN[tf] * MIN
            end = min(int(q["endTime"]), self.now) if "endTime" in q else self.now
            start = int(q["startTime"]) if "startTime" in q else end - (lim + 2) * span
            rows = self.bars(sym, tf, start, end)
            rows = rows[:lim] if "startTime" in q else rows[-lim:]
            return json.dumps([[int(r[0])] + r[1:6] + [int(r[6])] + r[7:12] for r in rows])
        if path == "/fapi/v1/fundingRate":
            end = int(q.get("endTime", self.now))
            start = int(q.get("startTime", end - int(q.get("limit", 100)) * 8 * HOUR))
            rows = self.funding(sym, start, end)[-int(q.get("limit", 1000)):]
            return json.dumps([{"symbol": sym, "fundingTime": int(r[0]), "fundingRate": r[2], "markPrice": "0"} for r in rows])
        if path == "/fapi/v1/premiumIndex":
            syms = [sym] if sym else list(H.COINS and [c + "USDT" for c in H.COINS])
            out = []
            for s in syms:
                p = self.last_price(s)
                f = self.funding(s, self.now - 3 * DAY, self.now)
                if p is None:
                    continue
                out.append({"symbol": s, "markPrice": str(p), "indexPrice": str(p), "lastFundingRate": f[-1][2] if f else "0.0001",
                            "interestRate": "0.0001", "nextFundingTime": (self.now // (8 * HOUR) + 1) * 8 * HOUR, "time": self.now})
            return json.dumps(out[0] if sym else out) if out else None
        if path == "/fapi/v1/openInterest":
            m = self.metrics(sym, self.now - DAY, self.now)
            return json.dumps({"symbol": sym, "openInterest": m[-1][1][2], "time": m[-1][0]}) if m else None
        if path.startswith("/futures/data/"):
            lim = int(q.get("limit", 30))
            m = self.metrics(sym, self.now - (lim + 2) * HOUR, self.now)
            hourly = [x for x in m if x[0] % HOUR == 0][-lim:]
            if not hourly:
                return None
            if path.endswith("openInterestHist"):
                return json.dumps([{"symbol": sym, "sumOpenInterest": r[2], "sumOpenInterestValue": r[3], "timestamp": t} for t, r in hourly])
            col, key = {"globalLongShortAccountRatio": (6, "longShortRatio"), "topLongShortPositionRatio": (5, "longShortRatio"),
                        "takerlongshortRatio": (7, "buySellRatio")}.get(path.rsplit("/", 1)[-1], (None, None))
            if col is None:
                return None
            return json.dumps([{"symbol": sym, key: r[col], "longAccount": "0.5", "shortAccount": "0.5", "timestamp": t} for t, r in hourly])
        if path == "/fapi/v1/depth":
            p = self.last_price(sym)
            if p is None:
                return None
            return json.dumps({"bids": [["%.8g" % (p * (1 - 0.0001 * k)), "%.8g" % (20000.0 / p)] for k in range(1, 101)],
                               "asks": [["%.8g" % (p * (1 + 0.0001 * k)), "%.8g" % (20000.0 / p)] for k in range(1, 101)]})
        if path == "/fapi/v1/ticker/24hr":
            out = []
            for c in H.COINS:
                rows = self.bars(c + "USDT", "1h", self.now - 2 * DAY, self.now)
                if len(rows) >= 25:
                    last, prev = float(rows[-1][4]), float(rows[-25][4])
                    out.append({"symbol": c + "USDT", "lastPrice": str(last), "priceChangePercent": "%.3f" % ((last / prev - 1) * 100),
                                "quoteVolume": "900000000", "highPrice": str(max(float(r[2]) for r in rows[-24:])),
                                "lowPrice": str(min(float(r[3]) for r in rows[-24:]))})
            return json.dumps(out)
        if path == "/fapi/v1/exchangeInfo":
            return desk.SynthExchange(self.now)(url)
        return None


def run_timeline(market, start, end, every, coins, config, out):
    home = tempfile.mkdtemp(prefix="desk-replay-")
    for k, v in config.items():
        H.run(["config", "set", k, str(v)], start, market, home=home)
    t = start
    rows = []
    while t <= end:
        market.now = t
        code, res = H.run(["today", "--coins", ",".join(coins)], t, market, home=home)
        if code != 0 or not isinstance(res, dict):
            rows.append({"t": t, "error": str(res)[-300:]})
        else:
            for c in res.get("coins") or []:
                cand = c.get("candidate") or {}
                rows.append({"t": t, "coin": c["symbol"], "decision": c.get("decision"), "why": (c.get("why") or "")[:200],
                             "setup": cand.get("setup"), "side": cand.get("side"), "kind": cand.get("kind"), "ev_r": c.get("ev_r"),
                             "blocks": [b.split(":")[0] for b in c.get("blocks") or []], "blocked_signal": c.get("blocked_signal"),
                             "event": (c.get("event") or {}).get("name")})
        if out:
            with open(out, "a") as fh:
                for r in rows[-len(coins):]:
                    fh.write(json.dumps(r) + "\n")
        t += every
    return rows


def summarise(rows):
    dec = collections.Counter(r.get("decision") for r in rows if "decision" in r)
    cands = [r for r in rows if r.get("setup")]
    stops = collections.Counter(b for r in cands for b in (r["blocks"] or ["(history or EV)"]) if not str(r.get("decision")).startswith("SIGNAL"))
    print("decisions: %s" % dict(dec))
    print("moments with a candidate: %d; signals: %d" % (len(cands), sum(1 for r in rows if str(r.get("decision")).startswith("SIGNAL"))))
    print("what stopped the candidates (count of moments):")
    for k, v in stops.most_common(15):
        print("  %4d  %s" % (v, k))
    for r in rows:
        if str(r.get("decision")).startswith("SIGNAL") or r.get("blocked_signal"):
            print("  %s %s %s %s %s ev %s" % (desk.fmt_time(r["t"], 330), r["coin"], r["decision"], r.get("setup"), r.get("side"), r.get("ev_r")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/trinetra-binance"))
    ap.add_argument("--start", default="2026-09-15")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--every", default="4h")
    ap.add_argument("--coins", default="BTC,ETH,SOL,XRP,LINK")
    ap.add_argument("--primary", default="4h")
    ap.add_argument("--chart-signals", action="store_true")
    ap.add_argument("--no-vol-block", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    H.fast_mode()
    step = desk.TF_MIN[a.every] * MIN
    start = desk.parse_when(a.start + "T00:00Z", "start") // step * step + step
    end = desk.parse_when(a.end + "T00:00Z", "end")
    config = {"primary_tf": a.primary}
    if a.chart_signals:
        config["chart_signals"] = "true"
    if a.no_vol_block:
        config["block_high_vol_regime"] = "false"
    m = Market(Archive(a.cache))
    t0 = time.time()
    rows = run_timeline(m, start, end, step, [c.strip() for c in a.coins.split(",")], config, a.out)
    print("%d moments x %d coins in %.0f s (%d files downloaded, %d could not be fetched)" % (
        (end - start) // step + 1, len(a.coins.split(",")), time.time() - t0, m.a.downloads, len(m.a.failures)))
    summarise(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
