"""Replay the engine's news rules on real Binance USD-M perpetual history, with the engine's own code.

    python3 tests/engine/replay_history.py --cache ~/.cache/trinetra-binance [--coins SOL] [--kinds FOMC] [--rules momentum_after_60m]

It downloads only the files the replay needs from data.binance.vision (1-minute bars around each release, 1-hour bars
for the month before it, monthly funding), serves them to desk.py in Binance's API format, and runs `verify-news`
unchanged for every coin x release kind x rule. The result is the perpetual's own record of each rule, release by
release, next to the spot-price study the engine's verdicts come from. Recent Fed decisions on the official schedule
that the bundled history does not have yet (28 Oct 2026) are added.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import desk  # noqa: E402

BASE = "https://data.binance.vision/data/futures/um"
RULES = ("momentum_after_60m", "sweep_reclaim_fade", "breakout_accept", "spike_fade_24h")
MIN, DAY = 60000, 86400000


def utc_date(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()


class Archive(object):
    """data.binance.vision files, cached on disk; a missing file (before a listing, or not yet published) is None."""

    def __init__(self, cache):
        self.cache = cache
        os.makedirs(cache, exist_ok=True)
        self.downloads = 0

    def rows(self, path):
        local = os.path.join(self.cache, path.replace("/", "_"))
        if not os.path.exists(local):
            req = urllib.request.Request(BASE + "/" + path, headers={"User-Agent": "trinetra-replay/1.0"})
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        data = r.read()
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        open(local + ".missing", "w").close()
                        return None
                    if attempt == 3:
                        raise
                except urllib.error.URLError:
                    if attempt == 3:
                        raise
                time.sleep(2 * (attempt + 1))
            with open(local, "wb") as fh:
                fh.write(data)
            self.downloads += 1
        if os.path.exists(local + ".missing"):
            return None
        with zipfile.ZipFile(local) as z:
            text = z.read(z.namelist()[0]).decode("utf-8")
        return [r for r in csv.reader(io.StringIO(text)) if r and r[0][:1].isdigit()]       # newer files have a header row

    def klines(self, sym, tf, day=None, month=None):
        if day is not None:
            return self.rows("daily/klines/%s/%s/%s-%s-%s.zip" % (sym, tf, sym, tf, day.isoformat())) or []
        return self.rows("monthly/klines/%s/%s/%s-%s-%s.zip" % (sym, tf, sym, tf, month)) or []

    def funding(self, sym, month):
        return self.rows("monthly/fundingRate/%s/%s-fundingRate-%s.zip" % (sym, sym, month)) or []


def months_between(start_ms, end_ms):
    d = utc_date(start_ms).replace(day=1)
    out = []
    while d <= utc_date(end_ms):
        out.append(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


class HistoricalExchange(object):
    """Answers the Binance futures endpoints verify-news reads, from the archive."""

    def __init__(self, archive):
        self.a = archive
        self.mem = {}

    def _bars(self, sym, tf, start, end):
        out = []
        if tf == "1m":
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
                    if not rows:                                   # the current month is published day by day
                        y, mo = map(int, m.split("-"))
                        d = dt.date(y, mo, 1)
                        while d.month == mo and d <= utc_date(end):
                            rows += self.a.klines(sym, tf, day=d)
                            d += dt.timedelta(days=1)
                    self.mem[key] = rows
                out += self.mem[key]
        return [r for r in out if start <= int(r[0]) <= end]

    def __call__(self, url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.netloc != "fapi.binance.com":
            return None
        if u.path == "/fapi/v1/klines":
            tf, lim = q["interval"], int(q.get("limit", 500))
            span = desk.TF_MIN[tf] * MIN
            end = int(q["endTime"]) if "endTime" in q else None
            start = int(q["startTime"]) if "startTime" in q else None
            if start is None:
                start = end - lim * span
            if end is None:
                end = start + lim * span
            rows = self._bars(q["symbol"], tf, start, end)
            rows = rows[:lim] if "startTime" in q else rows[-lim:]
            return json.dumps([[int(r[0])] + r[1:6] + [int(r[6])] + r[7:12] for r in rows])
        if u.path == "/fapi/v1/fundingRate":
            start, end = int(q.get("startTime", 0)), int(q.get("endTime", 0))
            rows = []
            for m in months_between(start, end):
                rows += self.a.funding(q["symbol"], m)
            rows = [r for r in rows if start <= int(r[0]) <= end][:int(q.get("limit", 1000))]
            return json.dumps([{"symbol": q["symbol"], "fundingTime": int(r[0]), "fundingRate": r[2], "markPrice": "0"} for r in rows])
        return None


def with_recent_fed():
    """The bundled history ends mid-October 2026; add official Fed decisions since then that are over."""
    orig = desk.load_release_history
    have = {(r["kind"], r["t"]) for r in orig()}
    extra = [{"kind": r["kind"], "date": r["date"], "t": r["t"]} for r in (desk.load_release_calendar() or {}).get("releases") or []
             if r["kind"] in ("FOMC", "CPI", "NFP", "PPI") and (r["kind"], r["t"]) not in have and r["t"] + DAY < desk.now_ms()]
    merged = sorted(orig() + extra, key=lambda r: r["t"])
    desk.load_release_history = lambda: merged
    return extra


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/trinetra-binance"))
    ap.add_argument("--coins", default="BTC,ETH,SOL,XRP,LINK")
    ap.add_argument("--kinds", default="FOMC,CPI,NFP,PPI")
    ap.add_argument("--rules", default=",".join(RULES))
    ap.add_argument("--since", default="2019-09-01")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    extra = with_recent_fed()
    ex = HistoricalExchange(Archive(a.cache))
    now = desk.now_ms()
    results = []
    print("added from the official schedule: %s" % ", ".join("%s %s" % (r["kind"], r["date"]) for r in extra))
    print("%-5s %-5s %-20s %5s %8s %18s %6s %6s %9s   %s" % ("coin", "kind", "rule", "n", "avg R", "90% interval", "tgts", "stops", "funding", "study (spot)"))
    for coin in a.coins.split(","):
        for kind in a.kinds.split(","):
            for rule in a.rules.split(","):
                code, res = H.run(["verify-news", coin, "--kind", kind, "--rule", rule, "--since", a.since, "--max", "200"], now, ex)
                if code != 0 or not isinstance(res, dict):
                    print("%-5s %-5s %-20s ERROR %s" % (coin, kind, rule, str(res)[-200:]))
                    continue
                res["coin"] = coin
                results.append(res)
                ci = res.get("ci90") or [None, None]
                st = res.get("study") or {}
                print("%-5s %-5s %-20s %5d %+8.3f %18s %6d %6d %9s   n=%s %s %s" % (
                    coin, kind, rule, res["n"], res["avg_R"] or 0.0,
                    ("%+.2f to %+.2f" % tuple(ci)) if ci[0] is not None else "-", res["targets"], res["stops"],
                    ("%+.3f" % res["funding_avg_R"]) if res.get("funding_avg_R") is not None else "-",
                    st.get("n"), ("%+.3f" % st["avg_R"]) if st.get("avg_R") is not None else "", st.get("verdict") or ""))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(results, fh, indent=1)
    print("downloaded %d files" % ex.a.downloads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
