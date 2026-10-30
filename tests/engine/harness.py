"""Scenario harness for the engine: a scripted exchange that the real desk.py pipeline reads instead of the network.

Each scenario is a release (kind and time from the bundled official schedule), a coin, and a scripted reaction for the
minutes after the release. Prices before the scripted window come from the engine's own synthetic random walk; inside
the window they follow the script with a little noise, and every timeframe is aggregated from the same 1-minute path,
so 1m, 5m, 15m, 1h, 4h and 1d agree. The clock is pinned with --now, so the engine sees exactly the moment we choose.
"""
import contextlib
import io
import json
import math
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "site", "engine", "scripts")))
import desk  # noqa: E402

MIN = 60000
HOUR = 60 * MIN
DAY = 24 * HOUR
COINS = ("BTC", "ETH", "SOL", "XRP", "LINK")
P0 = {"BTC": 78000.0, "ETH": 2200.0, "SOL": 97.0, "XRP": 1.25, "LINK": 11.0}
NOISE = 0.00006                    # per-minute noise in the first two hours after a release: small, so the script decides
# Normal per-minute volatility (a 4h ATR of roughly 0.7% for BTC and 1.2% for SOL, like quiet-to-normal 2026 markets).
# The hour before a release is a third of that (markets go quiet into big news). `vol` scales it per scenario.
VOL = {"BTC": 0.0004, "ETH": 0.0005, "SOL": 0.0007, "XRP": 0.0007, "LINK": 0.0007}
FF_TITLE = {"FOMC": "FOMC Statement", "CPI": "CPI m/m", "NFP": "Non-Farm Employment Change", "PPI": "PPI m/m",
            "PCE": "Core PCE Price Index m/m", "GDP": "Advance GDP q/q"}


def official(kind, after=None):
    """The first official release of this kind on or after `after` (epoch ms), from the engine's bundled schedule."""
    cal = desk.load_release_calendar() or {}
    every = sorted((r for r in cal.get("releases") or [] if r["kind"] == kind), key=lambda r: r["t"])
    rows = [r for r in every if after is None or r["t"] >= after]
    if not rows and every:
        return every[-1]                     # e.g. GDP: BEA had not published dates past October 2026 when the file was built
    if not rows:
        raise ValueError("no official %s release" % kind)
    return rows[0]


# ---------------------------------------------------------------- reactions: cumulative move vs the release price
# f(k) is the move at the close of minute k after the release (k = 0 is the first minute).

def ramp(total, minutes=60):
    return lambda k: total * min(k + 1, minutes) / float(minutes)


def whipsaw(k):
    """Up 1% in 5 minutes, down to -1% by minute 20, back to +0.1% at minute 60."""
    pts = [(0, 0.0), (5, 0.010), (20, -0.010), (60, 0.001)]
    return _interp(pts, k)


def fake_up(k):
    """Sweeps above the pre-release high in the first 10 minutes, back inside by minute 32, ends -0.5% at minute 60."""
    pts = [(0, 0.0), (8, 0.006), (32, 0.0), (60, -0.005)]
    return _interp(pts, k)


def spike_up(k):
    """A 3% first hour, then a fade over the next day."""
    pts = [(0, 0.0), (30, 0.025), (60, 0.030), (600, 0.015), (1440, 0.012)]
    return _interp(pts, k)


def _interp(pts, k):
    x = k + 1
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / float(x1 - x0)
    return pts[-1][1]


REACTIONS = {"flat": lambda k: 0.0, "up": ramp(0.008), "down": ramp(-0.008), "small_up": ramp(0.002), "whipsaw": whipsaw,
             "fake_up": fake_up, "spike_up": spike_up}


# ---------------------------------------------------------------- the exchange

def _fmt(x):
    return "%.10g" % x


def _aggregate(rows, tf_ms):
    """desk._aggregate with full precision (the engine's demo exchange rounds to 4 decimals, so a 0.00001 coin would be 0)."""
    out = []
    for r in rows:
        g = (r[0] // tf_ms) * tf_ms
        o, h, l_, c, v, qv, tb, tbq = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]), float(r[9]), float(r[10]))
        if out and out[-1][0] == g:
            x = out[-1]
            x[2], x[3], x[4] = max(x[2], h), min(x[3], l_), c
            x[5] += v
            x[7] += qv
            x[9] += tb
            x[10] += tbq
        else:
            out.append([g, o, h, l_, c, v, g + tf_ms - 1, qv, r[8], tb, tbq, "0"])
    return [[x[0], _fmt(x[1]), _fmt(x[2]), _fmt(x[3]), _fmt(x[4]), "%.3f" % x[5], x[6], "%.2f" % x[7], x[8], "%.3f" % x[9],
             "%.2f" % x[10], "0"] for x in out]


def _split(rows, n):
    """desk._split with full precision: each 15m bar becomes n bars along open -> low -> high -> close."""
    out, step = [], 900000 // n
    for r in rows:
        t, o, h, l_, c = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])
        pts = [o, l_, h, c] if c >= o else [o, h, l_, c]
        path = []
        for k in range(n + 1):
            x = 3.0 * k / n
            j = min(2, int(x))
            path.append(pts[j] + (pts[j + 1] - pts[j]) * (x - j))
        v, tb = float(r[5]) / n, float(r[9]) / n
        for k in range(n):
            a, b = path[k], path[k + 1]
            hi, lo = max(a, b), min(a, b)
            if k == 0:
                hi, lo = max(hi, o), min(lo, o)
            tt = t + k * step
            out.append([tt, _fmt(a), _fmt(hi), _fmt(lo), _fmt(b), "%.3f" % v, tt + step - 1, "%.2f" % (v * b), 10, "%.3f" % tb,
                        "%.2f" % (tb * b), "0"])
    return out


class ScenarioExchange(desk.SynthExchange):
    """SynthExchange with a scripted 1-minute path from 3 days before the release up to the pinned clock."""

    def __init__(self, now, t_rel, reaction="flat", seed=11, fail_hosts=(), calendar=None, funding=None, prices=None, vol=1.0):
        desk.SynthExchange.__init__(self, now, seed=seed, fail_hosts=fail_hosts)
        self.t_rel = (int(t_rel) // MIN) * MIN
        self.reaction = REACTIONS[reaction] if isinstance(reaction, str) else reaction
        self.calendar = calendar or []          # [(kind, t)] shown by the live calendar feed
        self.funding = funding                  # optional last funding rates, newest last, to fake crowding
        self.prices = dict(P0, **(prices or {}))
        self.vol = vol
        self.min1 = {}

    def _sd(self, coin, t):
        if self.t_rel <= t < self.t_rel + 120 * MIN:
            return NOISE
        sd = VOL.get(coin, 0.0007) * self.vol
        return sd / 3.0 if self.t_rel - 60 * MIN <= t < self.t_rel else sd

    def _path(self, sym, w0, end, p_start):
        rng = random.Random(self.seed * 7919 + sum(ord(ch) for ch in sym))
        coin = sym[:-4] if sym.endswith("USDT") else sym
        rows, lp, p_rel, post = [], math.log(p_start), None, 0.0
        prev = p_start
        for t in range(w0, end, MIN):
            sd = self._sd(coin, t)
            if t < self.t_rel:
                lp += rng.gauss(0, sd)
                c = math.exp(lp)
            else:
                if p_rel is None:
                    p_rel = prev
                post += rng.gauss(0, sd)
                c = p_rel * (1 + self.reaction((t - self.t_rel) // MIN)) * math.exp(post)
            o = prev
            w = abs(rng.gauss(0, sd * 0.5))
            hi, lo = max(o, c) * (1 + w), min(o, c) * (1 - w)
            r = c / o - 1
            v = 100.0 * (1 + min(20.0, abs(r) / sd)) * rng.uniform(0.8, 1.2)
            tb = v * min(0.95, max(0.05, 0.5 + r / (8 * sd)))
            rows.append([t, _fmt(o), _fmt(hi), _fmt(lo), _fmt(c), "%.3f" % v, t + MIN - 1, "%.2f" % (v * c), 100,
                         "%.3f" % tb, "%.2f" % (tb * c), "0"])
            prev = c
        return rows

    def _base(self, sym):
        if sym in self.base:
            return self.base[sym]
        tfm = 15 * MIN
        end = (self.now // tfm + 1) * tfm
        coin = sym[:-4] if sym.endswith("USDT") else sym
        hist = desk.synth_bars(self.seed + sum(ord(ch) for ch in sym), 24000, "15m", end, 1.0)
        w0 = ((self.t_rel - 3 * DAY) // tfm) * tfm
        keep = [r for r in hist if r[0] < w0]
        scale = self.prices.get(coin, 20.0) / float(keep[-1][4])       # the walk ends at this coin's price level
        keep = [[r[0]] + [_fmt(float(x) * scale) for x in r[1:5]] + r[5:] for r in keep]
        rows1 = self._path(sym, w0, end, float(keep[-1][4]))
        self.base[sym] = keep + _aggregate(rows1, tfm)
        self.min1[sym] = (keep, rows1)
        return self.base[sym]

    def _series(self, sym, tf):
        k = (sym, tf)
        if k in self.series:
            return self.series[k]
        base = self._base(sym)
        keep, rows1 = self.min1[sym]
        if tf == "1m":
            rows = _split(keep[-200:], 15) + rows1
        elif tf == "5m":
            rows = _split(keep[-6000:], 3) + _aggregate(rows1, 5 * MIN)
        elif tf == "15m":
            rows = base
        else:
            rows = _aggregate(base, desk.TF_MIN[tf] * MIN)
        self.series[k] = rows
        return rows

    def __call__(self, url):
        host = url.split("/")[2]
        if host in self.fail_hosts:
            return None
        if host == "nfs.faireconomy.media":
            out = []
            for kind, t in self.calendar:
                et = desk.utc(t).astimezone(desk.dt.timezone(desk.dt.timedelta(hours=-4 if desk.us_dst(t) else -5)))
                out.append({"title": FF_TITLE[kind], "country": "USD", "date": et.isoformat(), "impact": "High",
                            "forecast": "0.3%", "previous": "0.2%"})
            return json.dumps(out)
        if host == "fapi.binance.com" and "/fapi/v1/fundingRate" in url and self.funding:
            sym = url.split("symbol=")[1].split("&")[0]
            n = 200
            rates = [0.0001] * (n - len(self.funding)) + list(self.funding)
            t0 = (self.now // (8 * HOUR)) * 8 * HOUR
            return json.dumps([{"symbol": sym, "fundingTime": t0 - (n - 1 - k) * 8 * HOUR, "fundingRate": "%.8f" % rates[k], "markPrice": "1"}
                               for k in range(n)])
        if host == "fapi.binance.com" and "/fapi/v1/premiumIndex" in url and self.funding:
            d = json.loads(desk.SynthExchange.__call__(self, url))
            for x in (d if isinstance(d, list) else [d]):
                x["lastFundingRate"] = "%.8f" % self.funding[-1]
            return json.dumps(d)
        return desk.SynthExchange.__call__(self, url)


# ---------------------------------------------------------------- running engine commands

def run(argv, now, exchange, home=None):
    """Run one engine command against the scripted exchange with the clock pinned at `now`. Returns (code, output)."""
    home = home or tempfile.mkdtemp(prefix="desk-scn-")
    orig = desk.make_net
    desk.make_net = lambda h, a: desk.Net(home=h, fetcher=exchange)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = desk.main(["--home", home, "--now", str(int(now)), "--json"] + list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    finally:
        desk.make_net = orig
        desk._NOW_OVERRIDE = None
    text = out.getvalue().strip()
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = text
    return code, data if data is not None else err.getvalue()


def brief(coin, kind, reaction, minutes_after, release_after=None, home=None, calendar_extra=(), **kw):
    """The full per-coin pipeline at `minutes_after` the first official `kind` release on or after `release_after`."""
    rel = official(kind, release_after if release_after is not None else desk.parse_when("2026-11-01T00:00Z", "x"))
    now = rel["t"] + int(minutes_after * MIN)
    ex = ScenarioExchange(now, rel["t"], reaction, calendar=[(kind, rel["t"])] + list(calendar_extra), **kw)
    code, res = run(["brief", coin], now, ex, home=home)
    return rel, now, code, res


def fast_mode():
    """Fewer bootstrap paths: the bootstrap is shown to the user but never decides (analogs and event history do)."""
    orig = desk.block_bootstrap

    def quick(*a, **k):
        k["paths"] = min(int(k.get("paths") or 400), 400)
        return orig(*a, **k)
    desk.block_bootstrap = quick
