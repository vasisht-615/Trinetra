#!/usr/bin/env python3
"""desk.py - the number engine behind the crypto-futures-desk skill.

Standard library only, Python 3.8+. It reads free public market data, does the
arithmetic a language model should never do in its head, and keeps an honest
scorecard of every call. It never places an order and never needs an API key.

  python3 desk.py today                   the day in one read: main news (live calendar checked against the
                                          official US schedule, headlines), news-event read, a decision per coin
  python3 desk.py brief BTC               one shot: snapshot, news read, best setup planned, tested and
                                          checked, and an engine-only decision (no Claude needed)
  python3 desk.py doctor                  what works here: Python, home folder, every data source
  python3 desk.py context                 whole market: BTC/ETH regime, funding board, breadth,
                                          fear & greed, implied vol, macro calendar, headlines
  python3 desk.py scan                    rank liquid perpetuals for setups (dynamic watchlist)
  python3 desk.py snapshot BTC --write    one coin: 15m/1h/4h/1d features, derivatives, levels, setups
  python3 desk.py plan BTC --side long --entry 67000 --stop 66200 --target 68600 --write
  python3 desk.py simulate --run latest   this setup's own history vs random entries,
                                          block-bootstrap paths, stress tests
  python3 desk.py check --run latest      hard rules: PASS or BLOCK, with reasons
  python3 desk.py event BTC               news release read: pre-release levels, fake or accepted move,
                                          tested event setups with their history
  python3 desk.py journal add --run latest --prob 0.52 --decision PAPER
  python3 desk.py journal forecast BTC --p-up 0.55 --hours 24
  python3 desk.py journal resolve         score every open call against what happened
  python3 desk.py journal stats           win rate, expectancy, calibration, paper/live status
  python3 desk.py selftest                offline self-check against a synthetic exchange

Home folder: $DESK_HOME or ~/.crypto-desk (config.json, journal.jsonl, runs/, cache/).
Data: Binance USD-M futures public API (Bybit fallback for prices), alternative.me,
Deribit, the ForexFactory weekly calendar feed, CoinGecko and news RSS. Free, no keys.
Levels come from Binance prices; CoinSwitch PRO prices can differ slightly, so the
final order always uses the venue's own price.
"""

import argparse
import bisect
import contextlib
import datetime as dt
import email.utils
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.4.0"
UA = "crypto-futures-desk/%s (personal research; python-urllib)" % VERSION

BINANCE = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
FNG_URL = "https://api.alternative.me/fng/?limit=8&format=json"
DERIBIT_DVOL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
FF_CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Block", "https://www.theblock.co/rss.xml"),
]

TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
          "6h": 360, "8h": 480, "12h": 720, "1d": 1440}
BYBIT_TF = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120",
            "4h": "240", "6h": "360", "12h": "720", "1d": "D"}
DAY_MS = 86400000
WEEK_MS = 7 * DAY_MS
MONDAY_OFFSET_MS = 4 * DAY_MS          # 1970-01-05 was a Monday
SESSIONS_UTC = (("asia", 0, 8), ("london", 7, 16), ("newyork", 13, 21))   # approximate, UTC hours
KILLZONES_UTC = (("london_open", 7, 10), ("ny_open", 13, 16))
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "PYUSD", "EUR", "GBP"}

DEFAULTS = {
    "equity": 1000.0,
    "equity_currency": "USDT",
    "usdt_inr": None,
    "usdt_fx": None,
    "tz": None,
    "risk_per_trade_pct": 0.5,
    "max_leverage": 5,
    "max_margin_per_trade_pct": 25.0,
    "liq_buffer_mult": 2.0,
    "min_liq_atr_beyond_stop": 2.0,
    "max_daily_loss_pct": 2.0,
    "max_trades_per_day": 3,
    "max_open_positions": 2,
    "cooldown_after_consecutive_losses": 2,
    "min_net_rr": 1.5,
    "fees": {"taker": 0.00063, "maker": 0.00025},     # CoinSwitch PRO futures, on leveraged volume, before GST (Oct 2026)
    "gst_on_fees_pct": 18.0,
    "tp_order": "limit",
    "slippage": {"BTC": 0.0003, "ETH": 0.0004, "default": 0.001},
    "maintenance_margin_rate": {"BTC": 0.006, "ETH": 0.006, "default": 0.0125},
    "liquidation_fee": 0.0075,
    "macro_blackout_minutes_before": 60,
    "macro_blackout_minutes_after": 30,
    "mid_range_band": [0.4, 0.6],
    "stop_atr_min": 0.3,
    "stop_atr_max": 3.0,
    "chase_atr_max": 0.5,
    "block_high_vol_regime": True,
    "chart_signals": False,
    "small_signals": True,
    "small_signal_risk": 0.25,
    "auto_track": True,
    "watchlist": ["BTC", "ETH", "SOL", "XRP", "LINK"],
    "primary_tf": "4h",
    "higher_tf": "1d",
    "default_entry_type": "limit",
    "scan_min_quote_volume": 50000000.0,
    "scan_top": 30,
    "mode": "paper",
    "probation_min_trades": 50,
    "unproven_risk_multiplier": 0.5,
    "event_slippage": 0.001,
    "event_min_stop_pct": {},
    "event_min_net_rr": 1.0,
    "venue_symbols": [],
    "tz_offset_minutes": 330,
    "venue": "CoinSwitch PRO",
}

HORIZON_BARS = {"5m": 48, "15m": 32, "30m": 24, "1h": 24, "2h": 18, "4h": 12, "1d": 10}
FX_SANITY = {"INR": (40, 200), "EUR": (0.5, 2.0), "GBP": (0.4, 2.0), "JPY": (50, 400), "AUD": (0.8, 3.0), "CAD": (0.8, 2.5),
             "SGD": (0.8, 2.5), "AED": (2.5, 5.0), "CHF": (0.4, 2.0), "PKR": (100, 600), "BDT": (60, 250), "LKR": (150, 600),
             "NPR": (60, 300), "IDR": (8000, 30000), "PHP": (30, 100), "NGN": (300, 5000), "BRL": (3, 12), "TRY": (10, 100)}


class DeskError(Exception):
    """A plain-language failure the skill can relay as-is."""


# ------------------------------------------------------------------ small helpers

_NOW_OVERRIDE = None


def now_ms():
    return _NOW_OVERRIDE if _NOW_OVERRIDE is not None else int(time.time() * 1000)


def set_now(text):
    """Pin the clock: '2026-10-21 08:30' (UTC) or epoch milliseconds."""
    global _NOW_OVERRIDE
    if text is None:
        _NOW_OVERRIDE = None
        return
    s = str(text).strip()
    if s.isdigit():
        _NOW_OVERRIDE = int(s)
        return
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise DeskError("--now wants '2026-10-21 08:30' (UTC) or epoch milliseconds")
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    _NOW_OVERRIDE = int(d.timestamp() * 1000)


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)


_TZ_LABEL = {330: "IST", 0: "UTC"}
_ZONE = {"zone": None, "offset": None}       # set by apply_tz when the config names an IANA zone


def get_zone(name):
    """A zoneinfo zone, or None when this Python cannot resolve names (3.8, or Windows without tzdata)."""
    try:
        from zoneinfo import ZoneInfo  # novm (3.9+, guarded)
        return ZoneInfo(str(name))
    except Exception:
        return None


def us_dst(ms):
    """True when US Eastern daylight time is in force at this instant (US rules since 2007: from 02:00 local on the
    second Sunday of March to 02:00 local on the first Sunday of November). Needs no time-zone database."""
    d = utc(ms)

    def sunday(month, n):
        first = dt.date(d.year, month, 1)
        return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    start = dt.datetime.combine(sunday(3, 2), dt.time(7, 0), tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(sunday(11, 1), dt.time(6, 0), tzinfo=dt.timezone.utc)
    return start <= d < end


def _local(ms, tz_minutes):
    z = _ZONE["zone"]
    if z is not None and tz_minutes == _ZONE["offset"]:
        d = utc(ms).astimezone(z)                 # each timestamp gets its own daylight-saving offset
        lab = d.tzname() or ""
        return d, (lab if lab and not lab.startswith(("+", "-")) else "UTC%+g" % (d.utcoffset().total_seconds() / 3600.0))
    d = utc(ms).astimezone(dt.timezone(dt.timedelta(minutes=tz_minutes)))
    return d, _TZ_LABEL.get(tz_minutes) or "UTC%+g" % (tz_minutes / 60.0)


def fmt_time(ms, tz_minutes=330):
    if ms is None:
        return "n/a"
    d, label = _local(ms, tz_minutes)
    return d.strftime("%Y-%m-%d %H:%M ") + label


def apply_tz(cfg):
    """With an IANA zone name ('Europe/London'), every printed time and the daily-loss day boundary use that
    zone's own offset for that moment; tz_offset_minutes becomes today's offset. Without one, the fixed
    tz_offset_minutes is used as set."""
    name = cfg.get("tz")
    _ZONE["zone"], _ZONE["offset"] = None, None
    if not name:
        return cfg
    z = get_zone(name)
    if z is None:
        raise DeskError("time zone %r cannot be resolved on this computer (Python 3.8, or Windows without the tzdata "
                        "package: run 'pip install tzdata'). Or use 'config set tz null' and set tz_offset_minutes." % name)
    off = int(utc(now_ms()).astimezone(z).utcoffset().total_seconds() // 60)
    cfg["tz_offset_minutes"] = off
    _ZONE["zone"], _ZONE["offset"] = z, off
    return cfg


def fp(x):
    """Format a price with sensible precision."""
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 10000:
        return "%.1f" % x
    if ax >= 100:
        return "%.2f" % x
    if ax >= 1:
        return "%.4f" % x
    if ax >= 0.01:
        return "%.5f" % x
    return "%.8f" % x


def pct(x, digits=2):
    return "n/a" if x is None else ("%+." + str(digits) + "f%%") % (100.0 * x)


def rnd(x, n=4):
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return round(x, n)


def sig(x, n=6):
    """Round to n significant digits (prices of any size)."""
    if x is None or x == 0:
        return x
    return round(x, n - 1 - int(math.floor(math.log10(abs(x)))))


def sig_down(x, n=8):
    """Round DOWN to n significant digits (sizes must never round up past the risk budget)."""
    if x is None or x <= 0:
        return x
    k = n - 1 - int(math.floor(math.log10(abs(x))))
    f = 10.0 ** k
    return math.floor(x * f + 1e-9) / f


def norm_symbol(s):
    s = str(s or "").upper().strip()
    s = re.sub(r"\.P$|PERP$|-PERP$|:USDT$", "", s)
    s = s.replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
    if not re.match(r"^[A-Z0-9]{2,20}$", s):
        raise DeskError("'%s' does not look like a coin or perpetual symbol" % s)
    if s.endswith("USDT"):
        return s
    if s.endswith("USD"):
        return s + "T"
    return s + "USDT"


def base_of(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def per_asset(table, sym):
    if not isinstance(table, dict):
        return table
    return table.get(base_of(sym), table.get("default"))


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def percentile(sorted_xs, q):
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * q
    f, c = int(math.floor(k)), int(math.ceil(k))
    if f == c:
        return sorted_xs[f]
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def wilson(k, n, z=1.645):
    """Wilson score interval for k successes out of n (z=1.645 -> 90%)."""
    if n <= 0:
        return (None, None)
    p = k / float(n)
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n))
    return ((c - a) / d, (c + a) / d)


def boot_diff_ci(a, b, n_boot=1500, seed=9, lo=0.05, hi=0.95):
    """90% bootstrap interval of mean(a) - mean(b), resampling each group independently. Comparing two
    intervals for overlap is far stricter than this (it rejects real differences), so verdicts use this."""
    if len(a) < 5 or len(b) < 5:
        return None
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    d = []
    for _ in range(n_boot):
        d.append(sum(a[rng.randrange(na)] for _ in range(na)) / na - sum(b[rng.randrange(nb)] for _ in range(nb)) / nb)
    d.sort()
    return [rnd(d[int(lo * n_boot)], 3), rnd(d[min(n_boot - 1, int(hi * n_boot))], 3)]


def boot_mean_ci(xs, n_boot=2000, seed=5, lo=0.05, hi=0.95):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return (None, None)
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (percentile(means, lo), percentile(means, hi))


# ------------------------------------------------------------------ home, config, files

def desk_home(arg=None):
    h = arg or os.environ.get("DESK_HOME") or os.path.join(os.path.expanduser("~"), ".crypto-desk")
    try:
        os.makedirs(os.path.join(h, "runs"), exist_ok=True)
        os.makedirs(os.path.join(h, "cache"), exist_ok=True)
    except OSError as e:
        raise DeskError("cannot create the desk folder %s: %s" % (h, e))
    return h


def load_config(home):
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(home, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
        except ValueError as e:
            raise DeskError("%s is not valid JSON: %s" % (path, e))
        if isinstance(user, dict):
            for k, v in list(user.items()):
                if k in CONFIG_SCHEMA:
                    try:
                        check_setting(k, v, user)
                    except DeskError as e:
                        raise DeskError("%s: %s (fix it with 'config set')" % (path, e))
            deep_merge(cfg, user)
    cur = str(cfg.get("equity_currency") or "USDT").upper()
    if not (_f(cfg.get("equity")) or 0) > 0:
        raise DeskError("equity must be a positive number in %s" % path)
    if cur not in ("USDT", "USD"):
        if not re.match(r"^[A-Z]{3,4}$", cur):
            raise DeskError("equity_currency must be USDT or a 3-letter currency code such as INR, not %s" % cur)
        rate = _f(cfg.get("usdt_fx")) or (_f(cfg.get("usdt_inr")) if cur == "INR" else None)
        if not rate or rate <= 0:
            raise DeskError("your capital is in %s: set usdt_fx (the price of 1 USDT in %s), e.g. "
                            "'config set usdt_fx 88.5'" % (cur, cur))
        lo, hi = FX_SANITY.get(cur, (1e-6, 1e7))
        if not lo <= rate <= hi:
            raise DeskError("usdt_fx %s looks wrong for %s: it must be the price of 1 USDT in %s (expected %s to %s). "
                            "Was it entered upside down?" % (rate, cur, cur, lo, hi))
        cfg["equity_local"] = float(cfg["equity"])
        cfg["equity"] = float(cfg["equity"]) / rate
        cfg["equity_currency"] = "USDT"
        cfg["local_currency"], cfg["local_rate"] = cur, rate
        cfg["equity_note"] = "%s %.0f converted at %s %s per USDT" % (cur, cfg["equity_local"], rate, cur)
    else:
        disp = str(cfg.get("display_currency") or ("INR" if _f(cfg.get("usdt_inr")) else "")).upper()
        rate = _f(cfg.get("usdt_inr")) if disp == "INR" and _f(cfg.get("usdt_inr")) else _f(cfg.get("usdt_fx"))
        if disp and rate:
            lo, hi = FX_SANITY.get(disp, (1e-6, 1e7))
            if not lo <= rate <= hi:
                raise DeskError("the rate %s looks wrong for %s (expected %s to %s per USDT)" % (rate, disp, lo, hi))
            cfg["local_currency"], cfg["local_rate"] = disp, rate
    return apply_tz(cfg)


def write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path, what="file"):
    if not os.path.exists(path):
        raise DeskError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise DeskError("%s is not valid JSON: %s" % (path, e))


def resolve_run(home, run):
    """'latest', a run id, or a path -> a run folder."""
    runs = os.path.join(home, "runs")
    if not run or run == "latest":
        names = sorted(n for n in os.listdir(runs) if os.path.isdir(os.path.join(runs, n))) if os.path.isdir(runs) else []
        if not names:
            raise DeskError("no runs yet: run 'snapshot SYMBOL --write' first")
        return os.path.join(runs, names[-1])
    if os.path.isdir(run):
        return run
    p = os.path.join(runs, run)
    if os.path.isdir(p):
        return p
    raise DeskError("no run folder '%s'" % run)


def new_run_dir(home, sym):
    stamp = utc(now_ms()).strftime("%Y%m%d-%H%M%S")
    p = os.path.join(home, "runs", "%s-%s" % (stamp, sym))
    os.makedirs(p, exist_ok=True)
    return p


# ------------------------------------------------------------------ network

def host_of(url):
    return urllib.parse.urlparse(url).netloc


def fixture_name(url):
    """Readable, stable file name for a URL (offline fixtures)."""
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qsl(u.query)
    parts = [u.netloc, u.path.strip("/")] + ["%s-%s" % (k, v) for k, v in sorted(q)]
    name = re.sub(r"[^A-Za-z0-9.\-]+", "_", "_".join(p for p in parts if p))
    if len(name) > 180:
        name = name[:150] + "_" + hashlib.sha1(name.encode()).hexdigest()[:12]
    return name


DOWN_HOSTS = {}                 # host -> (time, reason): unreachable hosts are not retried for DOWN_TTL seconds
DOWN_TTL = 120
HOPELESS = ("tunnel connection failed", "connection refused", "network is unreachable")   # a blocked network: retrying cannot help


class Net(object):
    """GET with retries, polite pacing, a small disk cache and an offline mode.
    `fetcher` (a callable url -> text) replaces the network entirely (tests)."""

    def __init__(self, home=None, offline=None, cache=True, pause=0.12, fetcher=None):
        self.home = home
        self.offline = offline
        self.use_cache = bool(cache and home)
        self.pause = pause
        self.fetcher = fetcher
        self.calls = 0
        self._last = 0.0
        self.extra_headers = {}

    def _cache_path(self, url):
        return os.path.join(self.home, "cache", hashlib.sha1(url.encode("utf-8")).hexdigest() + ".txt")

    def text(self, url, ttl=0, timeout=15):
        if self.fetcher is not None:
            self.calls += 1
            out = self.fetcher(url)
            if out is None:
                raise DeskError("no data for %s (offline)" % host_of(url))
            return out
        if self.offline:
            path = os.path.join(self.offline, fixture_name(url))
            for p in (path, path + ".json", path + ".xml"):
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as fh:
                        return fh.read()
            raise DeskError("offline: no fixture %s" % fixture_name(url))
        cp = self._cache_path(url) if self.use_cache and ttl > 0 else None
        if cp and os.path.exists(cp) and time.time() - os.path.getmtime(cp) < ttl:
            with open(cp, encoding="utf-8") as fh:
                return fh.read()
        body = self._fetch(url, timeout)
        if cp:
            try:
                with open(cp, "w", encoding="utf-8") as fh:
                    fh.write(body)
            except OSError:
                pass
        return body

    def json(self, url, ttl=0, timeout=15):
        t = self.text(url, ttl, timeout)
        try:
            return json.loads(t)
        except ValueError:
            raise DeskError("%s did not return JSON" % host_of(url))

    def _fetch(self, url, timeout):
        host = host_of(url)
        down = DOWN_HOSTS.get(host)
        if down and time.time() - down[0] < DOWN_TTL:
            raise DeskError("could not reach %s (%s; not retried for %d s)" % (host, down[1], DOWN_TTL))
        wait = self.pause - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        last_err, answered = None, False
        for attempt in range(3):
            self._last = time.time()
            self.calls += 1
            headers = {"User-Agent": UA, "Accept": "application/json, application/xml, text/xml, */*"}
            headers.update(self.extra_headers or {})
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                answered = True
                code = e.code
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    body = e.read().decode("utf-8", "replace")[:160]
                except Exception:
                    body = ""
                if code == 418:
                    raise DeskError("%s banned this IP for too many requests (HTTP 418); wait %s seconds"
                                    % (host_of(url), retry_after or "a few hundred"))
                if code == 429:
                    # Rate limited. One short wait if the source asks for one; otherwise stop calling it for a while, so a
                    # busy feed cannot stall a run (once 18 calls x 30 s) and an exchange is never pushed into a ban.
                    wait_s = float(retry_after) if (retry_after and retry_after.isdigit()) else None
                    if attempt == 0 and wait_s is not None and wait_s <= 5:
                        time.sleep(wait_s)
                        continue
                    DOWN_HOSTS[host] = (time.time(), "rate limited (HTTP 429)")
                    raise DeskError("%s is rate-limiting this address (HTTP 429); not called again for %d s"
                                    % (host_of(url), DOWN_TTL))
                if code >= 500:
                    last_err = "HTTP %d" % code
                    time.sleep(min(5.0, 2.0 * (attempt + 1)))
                    continue
                if code in (403, 451):
                    raise DeskError("%s refused the request (HTTP %d): this network or region may be blocked"
                                    % (host_of(url), code))
                raise DeskError("%s answered HTTP %d %s" % (host_of(url), code, body.strip()))
            except urllib.error.URLError as e:
                last_err = str(getattr(e, "reason", e))
            except (OSError, ValueError) as e:
                last_err = str(e)
            if last_err and any(h in last_err.lower() for h in HOPELESS):
                break                                     # a blocked network: retrying cannot help (DNS hiccups are retried)
            time.sleep(1.5 * (attempt + 1))
        if last_err and not answered:
            DOWN_HOSTS[host] = (time.time(), last_err)
        raise DeskError("could not reach %s (%s)" % (host, last_err))


def make_net(home, args):
    if getattr(args, "synthetic", False):
        return Net(home=home, fetcher=SynthExchange(now_ms()))
    return Net(home=home, offline=getattr(args, "offline", None), cache=not getattr(args, "no_cache", False))


# ------------------------------------------------------------------ market data

def bn_url(path, **params):
    items = sorted((k, v) for k, v in params.items() if v is not None)
    q = urllib.parse.urlencode(items)
    return BINANCE + path + ("?" + q if q else "")


def _f(x, default=None):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def parse_binance_klines(rows):
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            o, h, l_, c, v = _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5])
            if None in (o, h, l_, c, v) or min(o, h, l_, c) <= 0 or h < l_:
                continue
            out.append({"t": int(r[0]), "o": o, "h": h, "l": l_, "c": c, "v": v, "T": int(r[6]),
                        "qv": _f(r[7], 0.0), "n": int(_f(r[8], 0) or 0), "tbv": _f(r[9])})
        except (TypeError, ValueError, IndexError):
            continue
    return out


def get_klines(net, sym, tf, bars=500, end_ms=None, source="binance"):
    """Bars oldest first (the newest may still be forming). Pages backwards 1500 at a time."""
    if tf not in TF_MIN:
        raise DeskError("unknown timeframe %s" % tf)
    if source == "bybit":
        return get_klines_bybit(net, sym, tf, bars, end_ms)
    out = []
    end = end_ms
    need = int(bars)
    while need > 0:
        lim = min(1500, need)
        rows = net.json(bn_url("/fapi/v1/klines", symbol=sym, interval=tf, limit=lim, endTime=end),
                        ttl=(86400 if end else 20))
        if isinstance(rows, dict):
            raise DeskError("Binance: %s" % (rows.get("msg") or "no klines for %s" % sym))
        page = parse_binance_klines(rows)
        if not page:
            break
        out = page + out
        need -= len(page)
        if len(page) < lim:
            break
        end = page[0]["t"] - 1
    seen = {}
    for b in out:
        seen[b["t"]] = b
    res = [seen[t] for t in sorted(seen)]
    if not res:
        raise DeskError("no %s bars for %s" % (tf, sym))
    return res


def get_klines_bybit(net, sym, tf, bars=500, end_ms=None):
    if tf not in BYBIT_TF:
        raise DeskError("Bybit has no %s bars" % tf)
    tfm = TF_MIN[tf] * 60000
    out = []
    end = end_ms
    need = int(bars)
    while need > 0:
        lim = min(1000, need)
        params = {"category": "linear", "symbol": sym, "interval": BYBIT_TF[tf], "limit": lim}
        if end:
            params["end"] = end
        url = BYBIT + "/v5/market/kline?" + urllib.parse.urlencode(sorted(params.items()))
        data = net.json(url, ttl=(86400 if end else 20))
        if not isinstance(data, dict) or data.get("retCode") not in (0, "0"):
            raise DeskError("Bybit: %s" % (data.get("retMsg") if isinstance(data, dict) else "bad reply"))
        rows = (data.get("result") or {}).get("list") or []
        page = []
        for r in rows:
            try:
                t = int(r[0])
                o, h, l_, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                if min(o, h, l_, c) <= 0 or h < l_:
                    continue                                    # a broken bar (as in parse_binance_klines): never a price
                page.append({"t": t, "o": o, "h": h, "l": l_, "c": c,
                             "v": float(r[5]), "T": t + tfm - 1, "qv": float(r[6]), "n": 0, "tbv": None})
            except (TypeError, ValueError, IndexError):
                continue
        page.sort(key=lambda b: b["t"])
        if not page:
            break
        out = page + out
        need -= len(page)
        if len(page) < lim:
            break
        end = page[0]["t"] - 1
    seen = {}
    for b in out:
        seen[b["t"]] = b
    res = [seen[t] for t in sorted(seen)]
    if not res:
        raise DeskError("no %s bars for %s on Bybit" % (tf, sym))
    return res


def get_klines_range(net, sym, tf, start_ms, end_ms):
    """Bars from start_ms forward to end_ms (Binance only), oldest first."""
    out, t = [], int(start_ms)
    tfm = TF_MIN[tf] * 60000
    while t < end_ms:
        lim = int(min(1500, (end_ms - t) // tfm + 1))
        rows = net.json(bn_url("/fapi/v1/klines", symbol=sym, interval=tf, limit=lim, startTime=t), ttl=15)
        if isinstance(rows, dict):
            raise DeskError("Binance: %s" % (rows.get("msg") or "no klines"))
        page = parse_binance_klines(rows)
        page = [b for b in page if b["t"] >= t]
        if not page:
            break
        out.extend(page)
        if len(page) < lim:
            break
        t = page[-1]["t"] + tfm
    seen = {}
    for b in out:
        seen[b["t"]] = b
    return [seen[k] for k in sorted(seen) if k <= end_ms]


def _typical_cut(t_rel):
    return ((t_rel - 3600000) // 3600000) * 3600000


def typical_hourly_move(bars_1h, t_rel):
    """Median absolute 1h close-to-close move over the 720 clock hours that ended before the hour holding the start
    of the pre-release hour (the event study's definition). None without enough history."""
    if not bars_1h:
        return None
    cut = _typical_cut(t_rel)
    closed = [b for b in bars_1h if b["t"] + 3600000 <= cut][-721:]
    if len(closed) < 201:
        return None
    return statistics.median(abs(b2["c"] / b1["c"] - 1) for b1, b2 in zip(closed, closed[1:]))


def fetch_typical_hourly(net, sym, t_rel, now, source="binance"):
    """The typical hourly move for the spike-fade rule, or None (not needed yet, or no data). Only fetched once the
    first hour after the release is over, with a fixed end time so cached pages stay correct."""
    if now < t_rel + 60 * 60000:
        return None
    try:
        return typical_hourly_move(get_klines(net, sym, "1h", 760, end_ms=_typical_cut(t_rel) - 1, source=source), t_rel)
    except DeskError:
        return None


def _dense_minutes(post, t_rel, now, last_close):
    """One bar per minute from the release on, so list index k is minute k. A missing minute (an exchange
    outage) becomes a flat bar at the previous close, as in the event study's data."""
    by_t = dict((b["t"], b) for b in post)
    out, prev = [], last_close
    k = 0
    while True:
        t = t_rel + k * 60000
        if t + 60000 - 1 >= now:
            break
        b = by_t.get(t)
        if b is None:
            if prev is None:
                k += 1
                continue
            b = {"t": t, "T": t + 59999, "o": prev, "h": prev, "l": prev, "c": prev, "v": 0.0, "filled": True}
        out.append(b)
        prev = b["c"]
        k += 1
    return out


def _first_index(vals, target):
    for k, v in enumerate(vals):
        if v == target:
            return k
    return -1


def analyse_event(bars, t_rel, now, kind="OTHER", stats=None, cfg=None, sym="BTCUSDT", typical_hourly=None):
    """Read the market's reaction to a release from 1-minute bars (the pre-release hour range, the first
    15 minutes, sweeps of that range, whipsaws) and build the event setups exactly as they were tested
    in references/event_stats.json: no entry before the blackout ends (30 min after the release), stops
    at least event_min_stop_pct away, first target 1.5R, time stop 4h after the release (24h for
    momentum). Each setup carries its own history verdict; most are negative, and the read says so."""
    cfg = cfg or DEFAULTS
    t_rel = (int(t_rel) // 60000) * 60000           # releases are on the minute; bars open on the minute
    anchor = min(t_rel, now)
    uniq = {}
    for b in bars:
        uniq[b["t"]] = b
    bars = [uniq[t] for t in sorted(uniq)]
    pre = [b for b in bars if anchor - 3600000 <= b["t"] < anchor and b["T"] < now]
    post = _dense_minutes([b for b in bars if b["t"] >= t_rel and b["T"] < now], t_rel, now, pre[-1]["c"] if pre else None)
    if len(pre) < 30:
        raise DeskError("not enough 1-minute bars in the hour before %s" % ("the release" if now >= t_rel else "now"))
    pre_hi, pre_lo = max(b["h"] for b in pre), min(b["l"] for b in pre)
    mins = (now - t_rel) / 60000.0
    after = max(EVENT_FIRST_MINUTE, int(cfg.get("macro_blackout_minutes_after") or 0))
    phase = ("PRE" if mins < 0 else "SHOCK" if mins < after else "SETTLING" if mins < 60 else "POST")
    out = {"kind": kind, "release": t_rel, "minutes_since": int(mins), "phase": phase,
           "pre_hour_high": pre_hi, "pre_hour_low": pre_lo, "pre_hour_range_pct": rnd((pre_hi - pre_lo) / pre[-1]["c"] * 100, 3),
           "candidates": [], "missed": []}
    stats = stats if stats is not None else load_event_stats()
    coin = base_of(sym)
    block, label, own = coin_event_stats(stats, coin)
    out["history"] = (block.get("summary") or {}).get(kind)
    out["history_coin"] = label
    if not out["history"]:
        out["history_note"] = ("%s releases were not studied; CPI, PPI, Fed decisions and jobs reports had first-hour ranges 1.2-2.1x normal, with "
                               "whipsaws 1.6-4.8x as often, so expect the same or worse" % kind)
    if not own:
        out["history_note"] = ((out.get("history_note") + ". ") if out.get("history_note") else "") + (
            "%s has no news study of its own, so %s's history is used; %s usually moves more on the same news" % (coin, label, coin))
    if not post:
        out["read"] = ("release in %d min. No new trades from %d min before to %d min after it. Levels to watch: the last hour's "
                       "high %s and low %s. After the release, re-run this to see whether the first move fakes (sweep and close "
                       "back inside) or holds (15-minute close outside)." % (max(0, -int(mins)), cfg["macro_blackout_minutes_before"],
                                                                          after, fp(pre_hi), fp(pre_lo)))
        return out
    p0 = post[0]["o"]
    cur = post[-1]["c"]
    hi_all, lo_all = max(b["h"] for b in post), min(b["l"] for b in post)
    out.update({"price_now": cur, "move_since_pct": rnd((cur / p0 - 1) * 100, 3),
                "max_up_pct": rnd((hi_all / p0 - 1) * 100, 3), "max_down_pct": rnd((lo_all / p0 - 1) * 100, 3),
                "position": "above the pre-release range" if cur > pre_hi else ("below the pre-release range" if cur < pre_lo else "inside the pre-release range")})
    if len(post) >= 15:
        r15 = post[14]["c"] / p0 - 1
        out["first_15m_pct"] = rnd(r15 * 100, 3)
        out["first_15m_direction"] = "up" if r15 > 0 else ("down" if r15 < 0 else "flat")
        if abs(cur / p0 - 1) > 0.002 and r15 != 0 and (cur > p0) != (r15 > 0):
            out["first_move_reversed_so_far"] = True
    w30 = post[:30]
    out["swept_high_30m"] = any(b["h"] > pre_hi for b in w30)
    out["swept_low_30m"] = any(b["l"] < pre_lo for b in w30)
    out["whipsaw_both_sides"] = max(b["h"] for b in post[:60]) > pre_hi and min(b["l"] for b in post[:60]) < pre_lo
    reads = []
    for k in range(4, min(len(post), 60), 5):
        hk, lk, c = max(b["h"] for b in post[:k + 1]), min(b["l"] for b in post[:k + 1]), post[k]["c"]
        if pre_lo < c < pre_hi and (hk > pre_hi or lk < pre_lo):
            reads.append("FAKE: price swept the pre-release %s and closed back inside %d min after the release"
                         % ("high and low" if (hk > pre_hi and lk < pre_lo) else ("high" if hk > pre_hi else "low"), k + 1))
            break
    for k in (14, 29, 44, 59):
        if k < len(post):
            c = post[k]["c"]
            if c > pre_hi * 1.002 or c < pre_lo * 0.998:
                reads.append("ACCEPTED: a 15-minute close held %s the pre-release range %d min after the release"
                             % ("above" if c > pre_hi else "below", k + 1))
                break
    if out["whipsaw_both_sides"]:
        reads.append("WHIPSAW: both sides of the pre-release range were swept within the hour (stops taken both ways)")
    out["read"] = "; ".join(reads) or "no clean fake or acceptance yet"
    if phase in ("PRE", "SHOCK"):
        return out
    tested_floor = float(per_asset(EVENT_STOP_FLOOR_PCT, sym))
    user_floor = _f(per_asset(cfg.get("event_min_stop_pct") or {}, sym)) or 0.0
    floor = max(tested_floor, user_floor) / 100.0
    if user_floor > tested_floor:
        out["history_note"] = ((out.get("history_note") + ". ") if out.get("history_note") else "") + (
            "Your event stop floor (%.2f%%) is wider than the tested %.2f%%" % (user_floor, tested_floor))
    sigs = []
    first = EVENT_FIRST_MINUTE - 1
    # sweep_reclaim_fade: first 5-minute close from minute 30 to 60 back inside the range after a sweep
    for k in range(first, min(len(post), 60), 5):
        c = post[k]["c"]
        if not (pre_lo < c < pre_hi):
            continue
        highs, lows = [b["h"] for b in post[:k + 1]], [b["l"] for b in post[:k + 1]]
        hk, lk = max(highs), min(lows)
        ih, il = _first_index(highs, hk), _first_index(lows, lk)
        if hk > pre_hi and lk < pre_lo and ih == il:
            break                      # both sides swept inside one bar: which came last is unknown, so no fade
        if hk > pre_hi and (lk >= pre_lo or ih > il):
            sigs.append(("sweep_reclaim_fade", "short", k, c, hk * 1.001, 240))
            break
        if lk < pre_lo:
            sigs.append(("sweep_reclaim_fade", "long", k, c, lk * 0.999, 240))
            break
    # breakout_accept: a 15-minute close at minute 30, 45 or 60 outside the range by 0.2%
    for k in (29, 44, 59):
        if k < first or k >= len(post):
            continue
        c = post[k]["c"]
        if c > pre_hi * 1.002:
            sigs.append(("breakout_accept", "long", k, c, pre_hi * 0.999, 240))
            break
        if c < pre_lo * 0.998:
            sigs.append(("breakout_accept", "short", k, c, pre_lo * 1.001, 240))
            break
    # momentum_after_60m: more than 0.3% from the release price at minute 60 -> follow it
    first_hour_span = None
    if len(post) >= 60:
        c = post[59]["c"]
        r60 = c / p0 - 1
        if abs(r60) > 0.003:
            side = "long" if r60 > 0 else "short"
            span = max(b["h"] for b in post[:60]) - min(b["l"] for b in post[:60])
            first_hour_span = span
            dist = max(0.005 * c, 0.5 * span)
            sigs.append(("momentum_after_60m", side, 59, c, c - dist if side == "long" else c + dist, 1440))
        # spike_fade_24h: an outsized first hour (> 2.5x the typical hourly move and > 0.6%) -> fade it for up to 24h
        if typical_hourly and abs(r60) > 2.5 * typical_hourly and abs(r60) > 0.006:
            side = "short" if r60 > 0 else "long"
            ext = max(b["h"] for b in post[:60]) if r60 > 0 else min(b["l"] for b in post[:60])
            sigs.append(("spike_fade_24h", side, 59, c, ext * 1.003 if r60 > 0 else ext * 0.997, 1440))
        out["typical_hourly_move_pct"] = rnd(typical_hourly * 100, 3) if typical_hourly else None
    for setup, side, k, entry, stop, hold_min in sigs:
        # how the stop is measured, so a later run can re-measure it from the price then (reprice_event)
        anchor = {"span": first_hour_span} if setup == "momentum_after_60m" else {"stop": stop}
        anchor["floor"] = floor
        dist = max(abs(entry - stop), floor * entry)
        stop = entry - dist if side == "long" else entry + dist
        sgn = 1 if side == "long" else -1
        t_sig = post[k]["T"] + 1
        cand = {"setup": setup, "side": side, "signal_minute": k + 1, "signal_time": t_sig, "entry": sig(entry), "stop": sig(stop),
                "stop_pct": rnd(dist / entry * 100, 3), "target1": sig(entry + sgn * 1.5 * dist), "target2": sig(entry + sgn * 2.5 * dist),
                "entry_order": ("market, as tested: enter within %d minutes of the signal at the price then, with the stop and targets "
                                "measured from your fill (brief re-prices them when it runs); after that, do not chase it" % EVENT_ORDER_MINUTES),
                "reprice": anchor,
                "expires": t_sig + EVENT_ORDER_MINUTES * 60000, "close_by": t_rel + hold_min * 60000,
                "horizon_hours": rnd(max(0.0, (t_rel + hold_min * 60000 - max(now, t_sig)) / 3600000.0), 2), "why": out["read"]}
        ev, verdict = event_rule_verdict(kind, setup, stats, coin=coin)
        cand["history"], cand["history_stats"] = verdict, ev
        cand["evidence"] = ("AVOID" if verdict.startswith(("NEGATIVE", "NO EDGE")) else
                            "UNTESTED" if verdict.startswith("INSUFFICIENT") else
                            "SUPPORTED" if verdict.startswith("POSITIVE") else "UNPROVEN")
        if cand["evidence"] == "UNPROVEN" and small_rule(coin, kind, setup, cfg):
            cand["tier"] = "small"
        if now > cand["expires"]:
            cand["status"] = "EXPIRED: signalled %d min after the release; the tested entry was then. Do not chase it." % (k + 1)
            out["missed"].append(cand)
        elif k + 1 < after:
            cand["status"] = "INSIDE YOUR NO-ENTRY WINDOW: signalled %d min after the release; your rule is %d minutes" % (k + 1, after)
            out["missed"].append(cand)
        else:
            cand["status"] = "LIVE"
            out["candidates"].append(cand)
    return out


def reprice_event(c, price):
    """A live news idea re-measured from the price now, the way the rules were tested: the entry is a market order and the
    stop and targets are measured from the actual entry (for the SOL Fed rule, entering 5 and 15 minutes after the signal,
    re-measured that way, kept the edge: +0.62R and +0.68R against +0.61R). Returns a new candidate, or None when the price
    is already through the setup's own stop level (the idea is then void)."""
    rp = c.get("reprice") or {}
    e = _f(price)
    if not e or e <= 0 or not rp:
        return c
    side = c["side"]
    sgn = 1 if side == "long" else -1
    if rp.get("span") is not None:
        dist = max(0.005 * e, 0.5 * float(rp["span"]))
    else:
        level = float(rp["stop"])
        if (level - e) * sgn >= 0:
            return None
        dist = abs(e - level)
    dist = max(dist, float(rp.get("floor") or 0.0) * e)
    out = dict(c)
    out.update({"entry": sig(e), "stop": sig(e - sgn * dist), "stop_pct": rnd(dist / e * 100, 3), "target1": sig(e + sgn * 1.5 * dist),
                "target2": sig(e + sgn * 2.5 * dist), "signal_entry": c["entry"], "moved_pct": rnd((e / float(c["entry"]) - 1) * 100, 3)})
    return out


def split_closed(bars, now=None):
    now = now_ms() if now is None else now
    closed = [b for b in bars if b["T"] < now]
    live = bars[-1] if bars and bars[-1]["T"] >= now else None
    return closed, live


def get_premium(net, sym=None):
    data = net.json(bn_url("/fapi/v1/premiumIndex", symbol=sym), ttl=15)
    if isinstance(data, dict) and "msg" in data and "symbol" not in data:
        raise DeskError("Binance: %s" % data.get("msg"))
    rows = data if isinstance(data, list) else [data]
    out = {}
    for r in rows:
        if not isinstance(r, dict) or "symbol" not in r:
            continue
        mark = _f(r.get("markPrice"))
        if mark is None:
            continue
        out[r["symbol"]] = {"mark": mark, "index": _f(r.get("indexPrice")),
                            "funding": _f(r.get("lastFundingRate")), "interest": _f(r.get("interestRate"), 0.0),
                            "next_funding": int(_f(r.get("nextFundingTime"), 0) or 0), "time": int(_f(r.get("time"), 0) or 0)}
    if sym and sym not in out:
        raise DeskError("Binance has no perpetual called %s" % sym)
    return out


def get_funding_history(net, sym, limit=200):
    rows = net.json(bn_url("/fapi/v1/fundingRate", symbol=sym, limit=limit), ttl=300)
    out = []
    for r in rows if isinstance(rows, list) else []:
        rate, t = _f(r.get("fundingRate")), _f(r.get("fundingTime"))
        if rate is not None and t is not None:
            out.append((int(t), rate))
    out.sort()
    return out


def get_funding_range(net, sym, start_ms, end_ms):
    """Settled funding rates between two times (Binance), oldest first."""
    rows = net.json(bn_url("/fapi/v1/fundingRate", symbol=sym, startTime=int(start_ms), endTime=int(end_ms), limit=1000), ttl=86400)
    out = []
    for r in rows if isinstance(rows, list) else []:
        rate, t = _f(r.get("fundingRate")), _f(r.get("fundingTime"))
        if rate is not None and t is not None and start_ms <= t <= end_ms:
            out.append((int(t), rate))
    return sorted(out)


def funding_interval_hours(hist):
    if len(hist) < 3:
        return 8.0
    gaps = sorted((b[0] - a[0]) / 3600000.0 for a, b in zip(hist[-10:], hist[-9:]))
    g = gaps[len(gaps) // 2]
    for h in (1, 2, 4, 8):
        if abs(g - h) < 0.25:
            return float(h)
    return round(g, 2)


def get_open_interest(net, sym):
    d = net.json(bn_url("/fapi/v1/openInterest", symbol=sym), ttl=15)
    return _f(d.get("openInterest")) if isinstance(d, dict) else None


def get_oi_hist(net, sym, period="1h", limit=48):
    rows = net.json(bn_url("/futures/data/openInterestHist", symbol=sym, period=period, limit=limit), ttl=120)
    out = []
    for r in rows if isinstance(rows, list) else []:
        t, oi, oiv = _f(r.get("timestamp")), _f(r.get("sumOpenInterest")), _f(r.get("sumOpenInterestValue"))
        if t is not None and oi is not None:
            out.append({"t": int(t), "oi": oi, "oi_value": oiv})
    out.sort(key=lambda x: x["t"])
    return out


def get_ratio(net, kind, sym, period="1h", limit=48):
    """Keyless ratios: 'global' accounts and 'taker' buy/sell. Binance moved the top-trader
    ratio behind an API-key header (no signature); it is fetched only when
    BINANCE_MARKET_DATA_KEY is set (a read-only key, never a trading key)."""
    path = {"global": "/futures/data/globalLongShortAccountRatio",
            "top_position": "/futures/data/topLongShortPositionRatio",
            "taker": "/futures/data/takerlongshortRatio"}[kind]
    if kind == "top_position":
        key = os.environ.get("BINANCE_MARKET_DATA_KEY")
        if not key or net.fetcher is not None or net.offline:
            return []
        net.extra_headers = {"X-MBX-APIKEY": key}
        try:
            rows = net.json(bn_url(path, symbol=sym, period=period, limit=limit), ttl=120)
        finally:
            net.extra_headers = {}
    else:
        rows = net.json(bn_url(path, symbol=sym, period=period, limit=limit), ttl=120)
    out = []
    for r in rows if isinstance(rows, list) else []:
        t = _f(r.get("timestamp"))
        val = _f(r.get("buySellRatio")) if kind == "taker" else _f(r.get("longShortRatio"))
        if t is not None and val is not None:
            row = {"t": int(t), "ratio": val}
            if kind != "taker":
                row["long"] = _f(r.get("longAccount"))
            out.append(row)
    out.sort(key=lambda x: x["t"])
    return out


def get_depth(net, sym, limit=100):
    d = net.json(bn_url("/fapi/v1/depth", symbol=sym, limit=limit), ttl=5)
    if not isinstance(d, dict):
        raise DeskError("Binance depth: bad reply")
    bids = [(float(p), float(q)) for p, q in d.get("bids", []) if float(p) > 0 and float(q) > 0]     # a broken level is no depth
    asks = [(float(p), float(q)) for p, q in d.get("asks", []) if float(p) > 0 and float(q) > 0]
    return bids, asks


def get_tickers(net):
    rows = net.json(bn_url("/fapi/v1/ticker/24hr"), ttl=60)
    out = {}
    for r in rows if isinstance(rows, list) else []:
        s = r.get("symbol")
        if s:
            out[s] = {"last": _f(r.get("lastPrice")), "chg_pct": _f(r.get("priceChangePercent")),
                      "qv": _f(r.get("quoteVolume"), 0.0), "high": _f(r.get("highPrice")), "low": _f(r.get("lowPrice"))}
    return out


def get_exchange_info(net):
    d = net.json(bn_url("/fapi/v1/exchangeInfo"), ttl=86400)
    out = {}
    for s in d.get("symbols", []) if isinstance(d, dict) else []:
        f = {x.get("filterType"): x for x in s.get("filters", [])}
        out[s.get("symbol")] = {
            "status": s.get("status"), "type": s.get("contractType"), "quote": s.get("quoteAsset"),
            "base": s.get("baseAsset"),
            "tick": _f((f.get("PRICE_FILTER") or {}).get("tickSize")),
            "step": _f((f.get("LOT_SIZE") or {}).get("stepSize")),
            "min_qty": _f((f.get("LOT_SIZE") or {}).get("minQty")),
            "min_notional": _f((f.get("MIN_NOTIONAL") or {}).get("notional")),
        }
    return out


def get_bybit_ticker(net, sym):
    url = BYBIT + "/v5/market/tickers?" + urllib.parse.urlencode([("category", "linear"), ("symbol", sym)])
    d = net.json(url, ttl=15)
    rows = ((d or {}).get("result") or {}).get("list") or []
    if not rows:
        raise DeskError("Bybit has no %s" % sym)
    r = rows[0]
    return {"mark": _f(r.get("markPrice")), "index": _f(r.get("indexPrice")), "funding": _f(r.get("fundingRate")),
            "next_funding": int(_f(r.get("nextFundingTime"), 0) or 0), "oi": _f(r.get("openInterest")),
            "last": _f(r.get("lastPrice"))}


def get_fng(net):
    d = net.json(FNG_URL, ttl=3600)
    if not isinstance(d, dict):
        raise DeskError("alternative.me returned an unexpected reply")
    rows = d.get("data") or []
    vals = [(int(_f(r.get("timestamp"), 0)) * 1000, int(_f(r.get("value"), 0)), r.get("value_classification")) for r in rows]
    vals.sort()
    if not vals:
        raise DeskError("alternative.me returned no Fear & Greed data")
    return {"value": vals[-1][1], "label": vals[-1][2],
            "change_7d": vals[-1][1] - vals[0][1] if len(vals) > 1 else None}


def get_dvol(net, currency="BTC"):
    end = now_ms()
    start = end - 8 * DAY_MS
    url = DERIBIT_DVOL + "?" + urllib.parse.urlencode([("currency", currency), ("end_timestamp", end // 3600000 * 3600000),
                                                       ("resolution", "3600"), ("start_timestamp", start // 3600000 * 3600000)])
    d = net.json(url, ttl=1800)
    data = ((d or {}).get("result") or {}).get("data") or []
    data = sorted(data, key=lambda r: r[0])
    if not data:
        raise DeskError("Deribit returned no DVOL for %s" % currency)
    last = data[-1][4]
    first = data[0][4]
    day_ago = [r for r in data if r[0] <= data[-1][0] - DAY_MS]
    return {"dvol": rnd(last, 2), "change_24h": rnd(last - day_ago[-1][4], 2) if day_ago else None,
            "change_7d": rnd(last - first, 2)}


_RELEASE_CAL = {}
MAIN_KINDS = ("FOMC", "CPI", "NFP", "PPI", "PCE", "GDP")
KIND_LABEL = {"FOMC": "Fed rate decision (FOMC)", "CPI": "CPI inflation", "NFP": "jobs report (NFP)", "PPI": "PPI inflation",
              "PCE": "PCE inflation", "GDP": "GDP"}


def release_calendar_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "references", "release_calendar.json"), os.path.join(here, "references", "release_calendar.json"),
              os.path.join(here, "release_calendar.json")):
        if os.path.exists(p):
            return os.path.normpath(p)
    return None


def load_release_calendar():
    """The official US release schedule bundled with the skill (references/release_calendar.json: BLS, Federal
    Reserve, BEA), or {} when the file is missing or unreadable."""
    p = release_calendar_path()
    if not p:
        return {}
    if p not in _RELEASE_CAL:
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            rows = [r for r in d.get("releases") or [] if isinstance(r, dict) and isinstance(r.get("t"), int) and r.get("title")
                    and r.get("kind") in MAIN_KINDS]
            d["releases"] = sorted(rows, key=lambda r: r["t"])
            _RELEASE_CAL[p] = d
        except (OSError, ValueError, AttributeError):
            return {}
    return _RELEASE_CAL[p]


def load_release_history():
    """Past release times used by the event study (references/release_history.json), oldest first, or []."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "references", "release_history.json"), os.path.join(here, "references", "release_history.json"),
              os.path.join(here, "release_history.json")):
        if os.path.exists(p):
            if p not in _RELEASE_CAL:
                try:
                    with open(p, encoding="utf-8") as fh:
                        d = json.load(fh)
                    _RELEASE_CAL[p] = sorted((r for r in d.get("releases") or [] if isinstance(r, dict) and isinstance(r.get("t"), int)
                                              and r.get("kind") in ("CPI", "FOMC", "NFP", "PPI")), key=lambda r: r["t"])
                except (OSError, ValueError, AttributeError):
                    return []
            return _RELEASE_CAL[p]
    return []


def official_releases(t_from, t_to):
    return [r for r in load_release_calendar().get("releases") or [] if t_from <= r["t"] <= t_to]


def merge_official(events, now, t_from, t_to):
    """Cross-check the live calendar with the official schedule: a main US release the feed lists (same type within
    10 minutes) is marked official and treated as high impact; one the feed misses is added from the schedule."""
    out = [dict(e) for e in events]
    for r in official_releases(t_from, t_to):
        hit = [e for e in out if abs(e["t"] - r["t"]) <= 10 * 60000 and e.get("country") in ("USD", "ALL")
               and event_kind(e.get("title") or "") == r["kind"]]
        for e in hit:
            e["official"] = True
            e["impact"] = "High"
        if hit:
            continue
        out.append({"t": r["t"], "minutes_from_now": int(round((r["t"] - now) / 60000.0)), "title": r["title"], "country": "USD",
                    "impact": "High", "forecast": None, "previous": None, "official": True,
                    "source": "official schedule (not in the live feed)", "note": r.get("note")})
    out.sort(key=lambda e: e["t"])
    return out


def release_calendar_line(now=None):
    cal = load_release_calendar()
    if not cal:
        return "Official release schedule: MISSING (references/release_calendar.json): only the live calendar feed is used"
    now = now_ms() if now is None else now
    stale = local_day(now, 0) > str(cal.get("covers_until"))
    return "Official release schedule: %s (%d US releases to %s%s)" % ("STALE" if stale else "found", len(cal["releases"]),
                                                                       cal.get("covers_until"), "; update the skill" if stale else "")


def release_confirmed(E, rd=None, net=None, tol_min=10):
    """True when a run folder's release (time and type) is a real one: listed as a US release of that type within
    10 minutes on the calendar the run's snapshot read, a fresh calendar read, or the official schedule. A news
    setup's tested history applies only then; a hand-entered time or type never unlocks it."""
    t, kind = E.get("release"), E.get("kind")
    if not isinstance(t, (int, float)) or kind in (None, "", "OTHER"):
        return False
    # Inside the official schedule's range a main US release must be on it: the news rules were tested on scheduled
    # releases only, so an unscheduled one (an emergency Fed move) or a wrongly dated feed entry never unlocks them.
    cal = load_release_calendar() or {}
    rel = [r for r in cal.get("releases") or [] if isinstance(r.get("t"), (int, float))]
    if kind in MAIN_KINDS and rel:
        try:
            until = int(dt.datetime.fromisoformat(str(cal.get("covers_until"))).replace(tzinfo=dt.timezone.utc).timestamp() * 1000) + DAY_MS
        except ValueError:
            until = max(r["t"] for r in rel)
        if min(r["t"] for r in rel) - DAY_MS <= t <= until:
            return any(r.get("kind") == kind and abs(r["t"] - t) <= tol_min * 60000 for r in rel)
    evs = []
    sp = os.path.join(rd, "snapshot.json") if rd else None
    if sp and os.path.exists(sp):
        try:
            snap = read_json(sp, "snapshot")
            if snap.get("source") != "manual":
                evs += [e for e in snap.get("macro") or [] if isinstance(e, dict)]
        except DeskError:
            pass
    if not evs and net is not None:
        try:
            evs += get_macro(net, now_ms())
        except DeskError:
            pass
    evs += [dict(r, country="USD") for r in official_releases(int(t) - 3600000, int(t) + 3600000)]
    return any(isinstance(e.get("t"), (int, float)) and abs(e["t"] - t) <= tol_min * 60000 and e.get("country") in ("USD", "ALL")
               and event_kind(e.get("title") or "") == kind for e in evs)


def next_official(now, kinds=MAIN_KINDS):
    """The next scheduled release of each main type from the bundled official schedule."""
    nxt = {}
    for r in load_release_calendar().get("releases") or []:
        if r["t"] > now and r["kind"] in kinds and r["kind"] not in nxt:
            nxt[r["kind"]] = r
    return sorted(nxt.values(), key=lambda r: r["t"])


class MacroList(list):
    """Calendar events. feed_error says why the live feed failed; the official US schedule is then used on its own."""
    feed_error = None


def get_macro(net, now=None, hours_ahead=48):
    """The live calendar merged with the official US schedule bundled with the skill. If the live feed fails (it rate-
    limits at times), the official schedule alone is returned with feed_error set, never nothing: a Fed decision must
    never go unseen because a free feed was busy."""
    now = now_ms() if now is None else now
    t_from, t_to = now - 360 * 60000, now + int(hours_ahead * 3600000)
    try:
        rows = net.json(FF_CALENDAR, ttl=1800)
        if not isinstance(rows, list):
            raise DeskError("the calendar feed returned an unexpected reply")
    except DeskError as e:
        res = MacroList(merge_official([], now, t_from, t_to))
        res.feed_error = str(e)
        return res
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            when = dt.datetime.fromisoformat(str(r.get("date")))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        t = int(when.timestamp() * 1000)
        mins = (t - now) / 60000.0
        if mins < -360 or mins > hours_ahead * 60:
            continue
        if str(r.get("impact")) not in ("High", "Medium"):
            continue
        if str(r.get("country")) not in ("USD", "ALL", "CNY", "EUR", "JPY", "GBP"):
            continue
        out.append({"t": t, "minutes_from_now": int(round(mins)), "title": r.get("title"), "country": r.get("country"),
                    "impact": r.get("impact"), "forecast": r.get("forecast"), "previous": r.get("previous")})
    return MacroList(merge_official(out, now, t_from, t_to))


OFFICIAL_ONLY = ("using the official US schedule bundled with the skill (Fed, CPI, PPI, jobs, GDP and PCE are all in it); "
                 "second-tier and non-US releases are missing")


HEADLINE_RISK = {
    "hack": ("hack", "exploit", "drained", "stolen", "breach", "attacker"),
    "delisting": ("delist", "suspend", "halt", "halts", "outage", "down for"),
    "regulation": ("sec ", "cftc", "lawsuit", "sues", "ban ", "banned", "rbi", "fiu", "tax", "regulat", "court"),
    "macro": ("cpi", "inflation", "fomc", "fed ", "powell", "rate cut", "rate hike", "payroll", "jobs report", "pce", "tariff", "recession"),
    "flows": ("etf", "inflow", "outflow", "treasury"),
    "supply": ("unlock", "airdrop", "listing", "lists ", "burn"),
    "stress": ("liquidat", "crash", "plunge", "depeg", "bankrupt", "insolven", "sell-off", "selloff"),
}

EVENT_KINDS = (("CPI", ("cpi",)), ("FOMC", ("fomc statement", "federal funds rate", "fed interest rate", "monetary policy statement",
                                           "interest rate decision", "fomc press conference", "fomc economic projections")),
               ("NFP", ("non-farm employment change", "nonfarm payrolls", "non-farm payrolls")), ("PCE", ("pce",)), ("PPI", ("ppi",)),
               ("GDP", ("gdp",)))
EVENT_NOT = ("minutes", "member", "speaks", "speech", "testimony", "adp", "flash", "revised", "preliminary")
EVENT_SETUPS = ("sweep_reclaim_fade", "breakout_accept", "fade_first_15m", "follow_first_15m", "momentum_after_60m", "spike_fade_24h")
EVENT_FIRST_MINUTE = 30                                          # tested: no event entry before 30 minutes after the release
# News rules traded small: positive on their own coin's history and again on Binance perpetuals with the funding paid,
# but not proven (the 90% interval still includes zero). They signal at `small_signal_risk` of normal risk (a quarter by
# default, whatever your record), are labelled "small" everywhere, are tracked apart from proven signals, and never count
# toward your edge status. On a Fed night ETH moves with SOL about 9 nights in 10, so with SOL's signal it is mostly the
# same bet made larger, not a second bet. Perpetual replay: tests/engine/replay_history.py (30 Oct 2026).
SMALL_RULES = {("ETH", "FOMC", "momentum_after_60m"): {"perp_n": 39, "perp_avg_R": 0.237, "perp_ci90": (-0.08, 0.55), "p_no_edge": 0.11,
                                                     "period": "Sep 2019 to Sep 2026"}}


def small_rule(coin, kind, rule, cfg=None):
    """The evidence for a news rule traded small, or None (also None when small signals are turned off)."""
    if not (cfg or DEFAULTS).get("small_signals", True):
        return None
    return SMALL_RULES.get((base_of(coin or ""), kind, rule))


EVENT_STOP_FLOOR_PCT = {"BTC": 1.5, "ETH": 2.0, "default": 2.5}  # tested on BTC at 1.5%; wider for more volatile coins
EVENT_ORDER_MINUTES = 15   # tested: a market entry at the signal close; entering up to 15 minutes later, with the stop and targets
                           # measured from that later fill, kept the SOL Fed edge, so a signal is good for 15 minutes and no longer


def _kind_of(part):
    if any(n in part for n in EVENT_NOT):
        return "OTHER"
    for kind, keys in EVENT_KINDS:
        if any(k in part for k in keys):
            return kind
    return "OTHER"


def event_kind(title):
    """The release type of a calendar title, matched on the release itself: 'FOMC Meeting Minutes', 'FOMC Member
    Speaks' and 'ADP Non-Farm Employment Change' are different events and come back as OTHER. For a cluster name
    ('Average Hourly Earnings m/m (+ Non-Farm Employment Change, ...)') the first title with a known type decides."""
    low = (title or "").lower()
    head, _, rest = low.partition(" (+ ")
    parts = [head] + ([x.strip() for x in rest.rstrip(")").split(", ")] if rest else [])
    for part in parts:
        k = _kind_of(part)
        if k != "OTHER":
            return k
    return "OTHER"


_EVENT_STATS = {}


def event_stats_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "references", "event_stats.json"), os.path.join(here, "references", "event_stats.json"),
              os.path.join(here, "event_stats.json")):
        if os.path.exists(p):
            return os.path.normpath(p)
    return None


def load_event_stats():
    """The event study's numbers (references/event_stats.json next to the scripts folder, or beside desk.py)."""
    p = event_stats_path()
    if not p:
        return {}
    if p not in _EVENT_STATS:
        try:
            with open(p, encoding="utf-8") as fh:
                _EVENT_STATS[p] = json.load(fh)
        except (OSError, ValueError):
            return {}
    return _EVENT_STATS[p]


def coin_event_stats(stats, coin=None):
    """The news history for one coin: its own study when the stats file has one (by_coin), else the top level (BTC).
    Returns (block, coin label, own) where own says whether the history is that coin's own."""
    coin = (coin or "BTC").upper()
    if coin != "BTC":
        bc = (stats.get("by_coin") or {}).get(coin)
        if isinstance(bc, dict) and bc.get("rules"):
            return bc, coin, True
    return stats, (stats.get("coin") or "BTC"), coin == (stats.get("coin") or "BTC")


def event_rule_verdict(kind, rule, stats=None, coin=None):
    """History of one news rule for one event type on one coin, phrased like the engine's other verdicts (the
    verdict word comes first, so the rules check can block a NEGATIVE one). A coin without its own study is judged
    on BTC's history, and the verdict says so."""
    stats = stats if stats is not None else load_event_stats()
    block, label, own = coin_event_stats(stats, coin)
    rules = block.get("rules") or {}
    period = block.get("period", stats.get("period", "2018-2026"))
    k = rules.get(kind)
    if not k:
        pooled = ((rules.get("ALL") or {}).get("event_days") or {}).get(rule)
        ref = (" Pooled CPI/PPI/Fed/jobs history for this rule, for reference only: %+.2fR over %d trades (90%% interval %+.2fR to %+.2fR)."
               % (pooled["avg_R"], pooled["n"], pooled["ci90"][0], pooled["ci90"][1])) if pooled and pooled.get("ci90") and pooled["ci90"][0] is not None else ""
        return None, "INSUFFICIENT HISTORY: %s releases were not part of the event study, so this rule is untested here.%s" % (kind, ref)
    ev, base = (k.get("event_days") or {}).get(rule), (k.get("ordinary_days") or {}).get(rule)
    if not ev:
        return None, "INSUFFICIENT HISTORY: no event-day history for %s on %s." % (rule, kind)
    an = {"n": ev["n"], "wins": ev.get("targets", 0), "losses": ev.get("stops", 0), "expectancy_r": ev["avg_R"], "expectancy_ci90": ev["ci90"]}
    bs = {"expectancy_r": base["avg_R"], "expectancy_ci90": base["ci90"]} if base else None
    v = edge_verdict(an, bs, None, ev.get("diff_vs_ordinary_ci90"))
    v = v.replace("random entries with the same stop and target", "the same rule on ordinary days")
    adj = ev.get("ci_adjusted")
    if v.startswith(("POSITIVE", "MARKET DRIFT")) and not (adj and adj[0] is not None and adj[0] > 0):
        cells = stats.get("cells_tested", block.get("cells_tested", 16))
        v = ("UNPROVEN (promising): average %+.2fR after costs and its 90%% interval (%+.2fR to %+.2fR) is above zero, but not after "
             "allowing for the %d rule-and-release combinations tested (adjusted interval %+.2fR to %+.2fR), so it could be luck."
             % (ev["avg_R"], ev["ci90"][0], ev["ci90"][1], cells, (adj or [0, 0])[0] or 0.0, (adj or [0, 0])[1] or 0.0))
    name = block.get("source_short") or label
    src = ("%s 1-minute" % name) if own else ("%s 1-minute, used for %s, which has no study of its own" % (name, (coin or "?").upper()))
    return ev, "%s [event history: %s %s %s, %d trades, %s, %s, costs included]" % (v, label, kind, rule, ev["n"], period, src)


def shorter_hold(kind, rule, stats=None, coin=None):
    """The shortest hold under the tested 24 hours that still kept a clear edge in the robustness test on this coin's own
    history (90% interval above zero, at least 20 trades): a dict with hold_hours, n, avg_R, ci90 and the outcome
    counts, or None. Only a coin's own study counts; a coin judged on BTC's history gets nothing here."""
    stats = stats if stats is not None else load_event_stats()
    block, _, own = coin_event_stats(stats, coin)
    if not own:
        return None
    best = None
    for v in ((((block.get("rules") or {}).get(kind) or {}).get("shorter_holds") or {}).get(rule) or []):
        ci = v.get("ci90") or [None, None]
        try:
            ok = (ci[0] is not None and ci[0] > 0 and int(v.get("n") or 0) >= 20 and v.get("targets") is not None
                  and 0 < float(v.get("hold_hours") or 0) < 24)
        except (TypeError, ValueError):
            ok = False
        if ok and (best is None or float(v["hold_hours"]) < float(best["hold_hours"])):
            best = v
    return best


def fit_hold_to_calendar(c, macro, now, cfg, t_rel, kind, coin, stats=None):
    """A news setup is tested with a 24-hour hold, and your rule is to be flat before every high-impact US release.
    When one falls inside the hold, the plan closes earlier (your blackout ahead of it) if that still leaves at least the
    shortest hold that kept the edge in testing, and the decision then uses that shorter hold's history. Otherwise the
    candidate is left as it is and the rules block it. Returns (candidate, note or None)."""
    if (c.get("evidence") != "SUPPORTED" and c.get("tier") != "small") or t_rel is None or not c.get("close_by"):
        return c, None
    before = int(cfg["macro_blackout_minutes_before"]) * 60000
    hits = [e for e in macro or [] if "t" in e and e.get("impact") == "High" and e.get("country") in ("USD", "ALL")
            and now + before < e["t"] <= c["close_by"]]
    if not hits:
        return c, None
    first = min(hits, key=lambda e: e["t"])
    alt = shorter_hold(kind, c["setup"], stats, coin)
    close_by = first["t"] - before
    held = (close_by - t_rel) / 3600000.0
    if not alt or held < float(alt["hold_hours"]):
        return c, None
    note = ("hold shortened: close by %s, %d minutes before %s (your rule is to be flat before it), %.1f hours after the "
            "release instead of 24. In testing a %g-hour hold kept the edge: %+.2fR over %d trades (90%% interval %+.2fR to "
            "%+.2fR), so the decision uses that history."
            % (fmt_time(close_by, cfg["tz_offset_minutes"]), before // 60000, first.get("title") or "the next release", held,
               float(alt["hold_hours"]), alt["avg_R"], alt["n"], alt["ci90"][0], alt["ci90"][1]))
    c = dict(c, close_by=close_by, horizon_hours=rnd(max(0.25, (close_by - now) / 3600000.0), 2), history_stats=dict(alt),
             hold_note=note)
    return c, note


def macro_blackout(events, cfg):
    """High-impact USD events inside the no-new-entries window."""
    before, after = cfg["macro_blackout_minutes_before"], cfg["macro_blackout_minutes_after"]
    hits = [e for e in events if e.get("impact") == "High" and e.get("country") in ("USD", "ALL")
            and -after <= e["minutes_from_now"] <= before]
    return hits


def get_coingecko_global(net):
    d = net.json(COINGECKO_GLOBAL, ttl=1800)
    g = (d or {}).get("data") or {}
    mcp = g.get("market_cap_percentage") or {}
    return {"btc_dominance": rnd(_f(mcp.get("btc")), 2), "eth_dominance": rnd(_f(mcp.get("eth")), 2),
            "total_mcap_change_24h": rnd(_f(g.get("market_cap_change_percentage_24h_usd")), 2)}


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_feed(xml_text, source):
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return items
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag not in ("item", "entry"):
            continue
        title = link = when = None
        for ch in el:
            ct = _strip_ns(ch.tag)
            if ct == "title":
                title = (ch.text or "").strip()
            elif ct == "link":
                link = (ch.text or "").strip() or ch.attrib.get("href")
            elif ct in ("pubDate", "published", "updated", "date") and when is None:
                txt = (ch.text or "").strip()
                try:
                    d = email.utils.parsedate_to_datetime(txt)
                except (TypeError, ValueError, IndexError):
                    try:
                        d = dt.datetime.fromisoformat(txt.replace("Z", "+00:00"))
                    except ValueError:
                        d = None
                if d is not None:
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=dt.timezone.utc)
                    when = int(d.timestamp() * 1000)
        if title:
            items.append({"source": source, "title": re.sub(r"\s+", " ", title)[:220], "link": link, "t": when})
    return items


def get_headlines(net, hours=12, now=None, symbols=None):
    now = now_ms() if now is None else now
    out, errors = [], []
    for name, url in RSS_FEEDS:
        try:
            out.extend(parse_feed(net.text(url, ttl=300), name))
        except DeskError as e:
            errors.append("%s: %s" % (name, e))
    out = [x for x in out if x["t"] is None or now - x["t"] <= hours * 3600000]
    out.sort(key=lambda x: -(x["t"] or 0))
    seen, uniq = set(), []
    for x in out:
        k = re.sub(r"[^a-z0-9]", "", x["title"].lower())[:60]
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    if symbols:
        words = {}
        for s in symbols:
            b = base_of(s)
            words[b] = [b.lower()] + {"BTC": ["bitcoin"], "ETH": ["ether", "ethereum"], "SOL": ["solana"],
                                      "XRP": ["ripple", "xrp"], "LINK": ["chainlink"], "BNB": ["binance coin"],
                                      "DOGE": ["dogecoin"], "ADA": ["cardano"]}.get(b, [])
        for x in uniq:
            low = x["title"].lower()
            x["tags"] = [b for b, ws in words.items() if any(re.search(r"\b%s\b" % re.escape(w), low) for w in ws)]
    for x in uniq:
        low = x["title"].lower()
        x["risk"] = [k for k, ws in HEADLINE_RISK.items() if any(w in low for w in ws)]
    return uniq, errors


HEADLINE_SERIOUS = ("hack", "delisting", "stress", "regulation")


def rank_headlines(heads, coins=None):
    """Most relevant first: headlines naming your coins and carrying a serious risk tag (hack, delisting, stress,
    regulation) lead, then other risk tags (macro, flows, supply), then the rest; newest first within a group."""
    coins = set(coins or [])

    def score(h):
        tags, risk = set(h.get("tags") or []), set(h.get("risk") or [])
        return (2 if tags & coins else 0) + (2 if risk & set(HEADLINE_SERIOUS) else (1 if risk else 0))
    return sorted(heads, key=lambda h: (-score(h), -(h.get("t") or 0)))


def when_text(minutes):
    a = abs(int(minutes))
    txt = ("%dd %dh" % (a // 1440, (a % 1440) // 60)) if a >= 1440 else (("%dh %02dm" % (a // 60, a % 60)) if a >= 60 else "%d min" % a)
    return "now" if a == 0 else (("in " + txt) if minutes > 0 else (txt + " ago"))


def fmt_day(ms, tz_minutes, now=None):
    d, label = _local(ms, tz_minutes)
    out = d.strftime("%a %d %b %H:%M ") + label
    if now is not None:
        out += " (%s)" % when_text(round((ms - now) / 60000.0))
    return out


# ------------------------------------------------------------------ indicators (all causal)

def sma(xs, n):
    out = [None] * len(xs)
    s, cnt = 0.0, 0
    for i, x in enumerate(xs):
        s += x
        cnt += 1
        if i >= n:
            s -= xs[i - n]
            cnt -= 1
        if cnt == n:
            out[i] = s / n
    return out


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    a = 2.0 / (n + 1)
    e = sum(xs[:n]) / float(n)
    out[n - 1] = e
    for i in range(n, len(xs)):
        e = a * xs[i] + (1 - a) * e
        out[i] = e
    return out


def wilder(xs, n, start=0):
    out = [None] * len(xs)
    if len(xs) < start + n:
        return out
    s = sum(xs[start:start + n]) / float(n)
    out[start + n - 1] = s
    for i in range(start + n, len(xs)):
        s = (s * (n - 1) + xs[i]) / n
        out[i] = s
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    ag, al = gain / n, loss / n

    def val(g, l_):
        if l_ == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / l_)
    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = val(ag, al)
    return out


def true_range(bars):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return tr


def atr(bars, n=14):
    return wilder(true_range(bars), n)


def adx(bars, n=14):
    L = len(bars)
    pdm, mdm = [0.0] * L, [0.0] * L
    for i in range(1, L):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    tr = true_range(bars)
    a, p, m = wilder(tr, n, 1), wilder(pdm, n, 1), wilder(mdm, n, 1)
    pdi, mdi, dx = [None] * L, [None] * L, [None] * L
    for i in range(L):
        if a[i] and p[i] is not None and m[i] is not None:
            pdi[i] = 100.0 * p[i] / a[i]
            mdi[i] = 100.0 * m[i] / a[i]
            s = pdi[i] + mdi[i]
            dx[i] = 0.0 if s == 0 else 100.0 * abs(pdi[i] - mdi[i]) / s
    out = [None] * L
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    if first is not None and L >= first + n:
        s = sum(dx[first:first + n]) / float(n)
        out[first + n - 1] = s
        for i in range(first + n, L):
            s = (s * (n - 1) + dx[i]) / n
            out[i] = s
    return out, pdi, mdi


def rolling_std(xs, n):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = sum(w) / float(n)
        out[i] = math.sqrt(max(0.0, sum((x - m) ** 2 for x in w) / n))
    return out


def rolling_max(xs, n):
    return [max(xs[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(xs))]


def rolling_min(xs, n):
    return [min(xs[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(xs))]


def rolling_rank(xs, n, min_count=50):
    out = [None] * len(xs)
    for i in range(len(xs)):
        if xs[i] is None:
            continue
        w = [x for x in xs[max(0, i - n + 1):i + 1] if x is not None]
        if len(w) >= min(n, min_count):
            out[i] = sum(1 for x in w if x <= xs[i]) / float(len(w))
    return out


def rolling_z(xs, n):
    out = [None] * len(xs)
    for i in range(n, len(xs)):
        w = xs[i - n:i]
        m = sum(w) / float(n)
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        out[i] = (xs[i] - m) / sd if sd > 0 else 0.0
    return out


def linreg_last(ys):
    n = len(ys)
    sx = n * (n - 1) / 2.0
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sy = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))
    den = n * sxx - sx * sx
    if den == 0:
        return ys[-1]
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    return a + b * (n - 1)


def squeeze(bars, n=20, bb_mult=2.0, kc_mult=1.5):
    """TTM-style squeeze: Bollinger bands inside Keltner channels, plus a
    linear-regression momentum line (same construction as the popular
    open-source 'squeeze momentum' study)."""
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l_ = [b["l"] for b in bars]
    L = len(bars)
    basis = sma(c, n)
    dev = rolling_std(c, n)
    rng = sma(true_range(bars), n)
    on, bbw, up, lo = [None] * L, [None] * L, [None] * L, [None] * L
    for i in range(L):
        if basis[i] is None or dev[i] is None or rng[i] is None:
            continue
        ub, lb = basis[i] + bb_mult * dev[i], basis[i] - bb_mult * dev[i]
        uk, lk = basis[i] + kc_mult * rng[i], basis[i] - kc_mult * rng[i]
        on[i] = (lb > lk) and (ub < uk)
        bbw[i] = (ub - lb) / basis[i] if basis[i] else None
        up[i], lo[i] = ub, lb
    hh, ll = rolling_max(h, n), rolling_min(l_, n)
    src = [None] * L
    for i in range(L):
        if hh[i] is not None and basis[i] is not None:
            src[i] = c[i] - ((hh[i] + ll[i]) / 2.0 + basis[i]) / 2.0
    mom = [None] * L
    for i in range(L):
        if i >= n - 1:
            w = src[i - n + 1:i + 1]
            if all(x is not None for x in w):
                mom[i] = linreg_last(w)
    return on, mom, bbw, up, lo


def session_vwap(bars, period_ms=DAY_MS, offset_ms=0):
    """Volume-weighted average price that resets each period, with its volume-weighted
    standard deviation (West's incremental algorithm: no precision loss at large prices)."""
    L = len(bars)
    vw, sd = [None] * L, [None] * L
    cur = None
    cv = m = S = 0.0
    for i, b in enumerate(bars):
        p = (b["t"] - offset_ms) // period_ms
        if p != cur:
            cur, cv, m, S = p, 0.0, 0.0, 0.0
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        w = b["v"]
        if w > 0:
            cv_new = cv + w
            delta = tp - m
            m += (w / cv_new) * delta
            S += w * delta * (tp - m)
            cv = cv_new
        if cv > 0:
            vw[i] = m
            sd[i] = math.sqrt(max(0.0, S / cv))
    return vw, sd


def pivots(bars, left=3, right=3):
    """Confirmed swing highs/lows as (index, price). A pivot needs `right` bars after it."""
    hs, ls = [], []
    for i in range(left, len(bars) - right):
        h, l_ = bars[i]["h"], bars[i]["l"]
        if all(h > bars[j]["h"] for j in range(i - left, i)) and all(h >= bars[j]["h"] for j in range(i + 1, i + right + 1)):
            hs.append((i, h))
        if all(l_ < bars[j]["l"] for j in range(i - left, i)) and all(l_ <= bars[j]["l"] for j in range(i + 1, i + right + 1)):
            ls.append((i, l_))
    return hs, ls


# ------------------------------------------------------------------ features, state, regime

def tf_features(bars):
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l_ = [b["l"] for b in bars]
    v = [b["v"] for b in bars]
    F = {"t": [b["t"] for b in bars], "c": c, "h": h, "l": l_, "v": v}
    F["ema9"], F["ema21"], F["ema50"], F["ema200"] = ema(c, 9), ema(c, 21), ema(c, 50), ema(c, 200)
    F["rsi"] = rsi(c, 14)
    F["atr"] = atr(bars, 14)
    F["adx"], F["pdi"], F["mdi"] = adx(bars, 14)
    F["sq_on"], F["sq_mom"], F["bbw"], F["bb_up"], F["bb_lo"] = squeeze(bars)
    F["vwap"], F["vwap_sd"] = session_vwap(bars)
    F["wvwap"], _ = session_vwap(bars, WEEK_MS, MONDAY_OFFSET_MS)
    F["don_hi"], F["don_lo"] = rolling_max(h, 48), rolling_min(l_, 48)
    F["atrp"] = [(a / x * 100.0) if (a is not None and x) else None for a, x in zip(F["atr"], c)]
    F["atrp_rank"] = rolling_rank(F["atrp"], 200)
    F["vol_z"] = rolling_z(v, 50)
    tb = [b.get("tbv") for b in bars]
    if bars and all(x is not None for x in tb):
        cum, cvd, delta = 0.0, [], []
        for x, y in zip(tb, v):
            d = 2.0 * x - y
            delta.append(d)
            cum += d
            cvd.append(cum)
        F["cvd"], F["delta"] = cvd, delta
    else:
        F["cvd"], F["delta"] = None, None
    return F


def trend_at(F, i):
    if i < 5:
        return "flat"
    e21, e50, e50p = F["ema21"][i], F["ema50"][i], F["ema50"][i - 5]
    if e21 is None or e50 is None or e50p is None:
        return "flat"
    c = F["c"][i]
    if e21 > e50 and c > e50 and e50 > e50p:
        return "up"
    if e21 < e50 and c < e50 and e50 < e50p:
        return "down"
    return "flat"


def don_pos(F, i):
    hi, lo = F["don_hi"][i], F["don_lo"][i]
    if hi is None or lo is None or hi <= lo:
        return None
    return (F["c"][i] - lo) / (hi - lo)


def pos_bucket(p, band=(0.4, 0.6)):
    if p is None:
        return None
    return "discount" if p < band[0] else ("premium" if p > band[1] else "mid")


def vol_bucket(r):
    if r is None:
        return None
    return "low" if r < 0.33 else ("high" if r > 0.66 else "normal")


def state_at(F, i, band=(0.4, 0.6)):
    sq = F["sq_on"][i]
    return {"trend": trend_at(F, i), "pos": pos_bucket(don_pos(F, i), band), "vol": vol_bucket(F["atrp_rank"][i]),
            "squeeze": (bool(sq) if sq is not None else None)}


def regime_at(F, i):
    a, r, sq = F["adx"][i], F["atrp_rank"][i], F["sq_on"][i]
    tr = trend_at(F, i)
    if r is not None and r > 0.95:
        return "HIGH_VOL"
    if sq:
        return "SQUEEZE"
    if a is not None and a >= 25 and tr == "up":
        return "TREND_UP"
    if a is not None and a >= 25 and tr == "down":
        return "TREND_DOWN"
    if a is not None and a < 20:
        return "RANGE"
    return "TRANSITION"


def structure(bars, left=3, right=3, keep=4):
    hs, ls = pivots(bars, left, right)
    res = {"trend": "mixed", "swing_highs": [], "swing_lows": [], "last_break": None,
           "dealing_range": None}
    res["swing_highs"] = [{"t": bars[i]["t"], "price": rnd(p, 8)} for i, p in hs[-keep:]]
    res["swing_lows"] = [{"t": bars[i]["t"], "price": rnd(p, 8)} for i, p in ls[-keep:]]
    if len(hs) >= 2 and len(ls) >= 2:
        hh, hl = hs[-1][1] > hs[-2][1], ls[-1][1] > ls[-2][1]
        if hh and hl:
            res["trend"] = "up"
        elif (not hh) and (not hl):
            res["trend"] = "down"
    events = []
    if hs:
        i0, p0 = hs[-1]
        for j in range(i0 + right + 1, len(bars)):
            if bars[j]["c"] > p0:
                events.append((bars[j]["t"], "up", p0))
                break
    if ls:
        i0, p0 = ls[-1]
        for j in range(i0 + right + 1, len(bars)):
            if bars[j]["c"] < p0:
                events.append((bars[j]["t"], "down", p0))
                break
    if events:
        t, d, lvl = max(events)
        kind = ("BOS" if res["trend"] == d else "CHOCH") + "_" + d.upper()
        res["last_break"] = {"t": t, "type": kind, "level": rnd(lvl, 8)}
    if hs and ls:
        hi, lo = hs[-1][1], ls[-1][1]
        if hi > lo:
            c = bars[-1]["c"]
            res["dealing_range"] = {"high": rnd(hi, 8), "low": rnd(lo, 8), "pos": rnd((c - lo) / (hi - lo), 3)}
    return res


def consecutive_true(xs, i):
    n = 0
    while i - n >= 0 and xs[i - n]:
        n += 1
    return n


def tf_summary(F, bars, tf):
    i = len(bars) - 1
    c = F["c"][i]
    a = F["atr"][i]
    out = {"tf": tf, "bar_time": bars[i]["t"], "close": c,
           "ema9": rnd(F["ema9"][i], 8), "ema21": rnd(F["ema21"][i], 8), "ema50": rnd(F["ema50"][i], 8),
           "ema200": rnd(F["ema200"][i], 8), "rsi": rnd(F["rsi"][i], 1), "atr": rnd(a, 8),
           "atr_pct": rnd(F["atrp"][i], 3), "atr_rank": rnd(F["atrp_rank"][i], 2),
           "adx": rnd(F["adx"][i], 1), "plus_di": rnd(F["pdi"][i], 1), "minus_di": rnd(F["mdi"][i], 1),
           "squeeze_on": F["sq_on"][i], "squeeze_bars": consecutive_true(F["sq_on"], i),
           "squeeze_mom": rnd(F["sq_mom"][i], 8),
           "squeeze_mom_rising": (F["sq_mom"][i] is not None and i > 0 and F["sq_mom"][i - 1] is not None
                                  and F["sq_mom"][i] > F["sq_mom"][i - 1]),
           "vwap": rnd(F["vwap"][i], 8), "vwap_sd": rnd(F["vwap_sd"][i], 8), "weekly_vwap": rnd(F["wvwap"][i], 8),
           "range48_high": rnd(F["don_hi"][i], 8), "range48_low": rnd(F["don_lo"][i], 8),
           "range48_pos": rnd(don_pos(F, i), 3), "vol_z": rnd(F["vol_z"][i], 2),
           "trend": trend_at(F, i), "regime": regime_at(F, i)}
    if F["vwap"][i] is not None and F["vwap_sd"][i]:
        out["vwap_z"] = rnd((c - F["vwap"][i]) / F["vwap_sd"][i], 2)
    if F["cvd"] is not None and i >= 12:
        avg_v = mean(F["v"][i - 11:i + 1]) or 1.0
        cvd_slope = (F["cvd"][i] - F["cvd"][i - 12]) / (12 * avg_v)
        price_slope = (c - F["c"][i - 12]) / (a * 12) if a else None
        out["cvd_slope_12"] = rnd(cvd_slope, 3)
        out["price_slope_12_atr"] = rnd(price_slope, 3)
        if price_slope is not None:
            if price_slope > 0.05 and cvd_slope < -0.05:
                out["cvd_divergence"] = "bearish (price up, net taker selling)"
            elif price_slope < -0.05 and cvd_slope > 0.05:
                out["cvd_divergence"] = "bullish (price down, net taker buying)"
            else:
                out["cvd_divergence"] = None
    st = structure(bars)
    out["structure"] = st
    return out


# ------------------------------------------------------------------ levels

def week_start(ms):
    return ((ms - MONDAY_OFFSET_MS) // WEEK_MS) * WEEK_MS + MONDAY_OFFSET_MS


def key_levels(b1h, b1d, now=None):
    now = now_ms() if now is None else now
    L = {}
    closed_d = [b for b in b1d if b["T"] < now]
    live_d = [b for b in b1d if b["T"] >= now]
    if closed_d:
        p = closed_d[-1]
        L["prev_day_high"], L["prev_day_low"], L["prev_day_close"] = p["h"], p["l"], p["c"]
    if live_d:
        d = live_d[-1]
        L["day_open"], L["day_high"], L["day_low"] = d["o"], d["h"], d["l"]
    ws = week_start(now)
    this_w = [b for b in b1d if b["t"] >= ws]
    prev_w = [b for b in b1d if ws - WEEK_MS <= b["t"] < ws]
    if this_w:
        L["week_open"] = this_w[0]["o"]
    if prev_w:
        L["prev_week_high"] = max(b["h"] for b in prev_w)
        L["prev_week_low"] = min(b["l"] for b in prev_w)
    d0 = utc(now)
    m_start = int(dt.datetime(d0.year, d0.month, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    this_m = [b for b in b1d if b["t"] >= m_start]
    if this_m:
        L["month_open"] = this_m[0]["o"]
    day0 = now // DAY_MS * DAY_MS
    sessions = {}
    for day_label, start in (("today", day0), ("yesterday", day0 - DAY_MS)):
        for name, h0, h1 in SESSIONS_UTC:
            a, b = start + h0 * 3600000, start + h1 * 3600000
            sb = [x for x in b1h if a <= x["t"] < b and x["T"] < now]
            if sb:
                sessions["%s_%s" % (day_label, name)] = {"high": max(x["h"] for x in sb), "low": min(x["l"] for x in sb),
                                                         "complete": b <= now}
    L["sessions"] = sessions
    hour = utc(now).hour
    L["killzone_now"] = next((n for n, h0, h1 in KILLZONES_UTC if h0 <= hour < h1), None)
    L["session_now"] = [n for n, h0, h1 in SESSIONS_UTC if h0 <= hour < h1]
    return L


def _merge_close(levels, tol=0.0003):
    """Merge levels within `tol` (fraction) of each other: one price, joined names (confluence)."""
    out = []
    for p, n in levels:
        if out and abs(p / out[-1][0] - 1) <= tol:
            if n not in out[-1][1]:
                out[-1][1].append(n)
        else:
            out.append([p, [n]])
    return out


def nearest_levels(levels_flat, price, k=4):
    above = _merge_close(sorted([(p, n) for n, p in levels_flat if p is not None and p > price]))[:k]
    below = _merge_close(sorted([(p, n) for n, p in levels_flat if p is not None and p < price], reverse=True))[:k]
    return ([{"name": "+".join(n), "price": rnd(p, 8), "dist_pct": rnd((p / price - 1) * 100, 3)} for p, n in above],
            [{"name": "+".join(n), "price": rnd(p, 8), "dist_pct": rnd((p / price - 1) * 100, 3)} for p, n in below])


def flatten_levels(L, tfsums):
    out = []
    for k in ("prev_day_high", "prev_day_low", "prev_day_close", "day_open", "week_open", "prev_week_high",
              "prev_week_low", "month_open"):
        if L.get(k) is not None:
            out.append((k, L[k]))
    for name, s in (L.get("sessions") or {}).items():
        out.append((name + "_high", s["high"]))
        out.append((name + "_low", s["low"]))
    for tf, s in tfsums.items():
        if tf not in ("1h", "4h"):
            continue
        for sh in s["structure"]["swing_highs"][-2:]:
            out.append(("swing_high_%s" % tf, sh["price"]))
        for sl in s["structure"]["swing_lows"][-2:]:
            out.append(("swing_low_%s" % tf, sl["price"]))
        if s.get("vwap") is not None and tf == "1h":
            out.append(("vwap_day", s["vwap"]))
            if s.get("vwap_sd"):
                for m in (1, 2):
                    out.append(("vwap_+%dsd" % m, s["vwap"] + m * s["vwap_sd"]))
                    out.append(("vwap_-%dsd" % m, s["vwap"] - m * s["vwap_sd"]))
    return out


# ------------------------------------------------------------------ derivatives

def derivatives_block(net, sym, price):
    D = {"errors": []}
    try:
        p = get_premium(net, sym)[sym]
        D.update({"mark": p["mark"], "index": p["index"], "funding": p["funding"], "next_funding": p["next_funding"],
                  "basis_pct": rnd((p["mark"] / p["index"] - 1) * 100, 4) if p["index"] else None})
    except DeskError as e:
        D["errors"].append("premium: %s" % e)
    try:
        hist = get_funding_history(net, sym, 200)
        if hist:
            rates = [r for _, r in hist]
            ih = funding_interval_hours(hist)
            per_day = 24.0 / ih
            last = D.get("funding", rates[-1])
            sd = statistics.pstdev(rates) if len(rates) > 2 else 0.0
            m = statistics.mean(rates)
            D.update({"funding_interval_h": ih,
                      "funding_avg_7d": rnd(mean(rates[-int(7 * per_day):]), 6),
                      "funding_z": rnd((last - m) / sd, 2) if sd > 0 else 0.0,
                      "funding_annualized_pct": rnd(last * per_day * 365 * 100, 1) if last is not None else None,
                      "funding_max_abs_hist": rnd(max(abs(r) for r in rates), 6),
                      "funding_history": [[t, r] for t, r in hist]})
    except DeskError as e:
        D["errors"].append("funding history: %s" % e)
    try:
        oi_now = get_open_interest(net, sym)
        D["open_interest"] = oi_now
        D["open_interest_usd"] = rnd(oi_now * price, 0) if (oi_now and price) else None
    except DeskError as e:
        D["errors"].append("open interest: %s" % e)
    try:
        oh = get_oi_hist(net, sym, "1h", 48)
        if len(oh) >= 25:
            last = oh[-1]["oi"]
            D["oi_change_4h_pct"] = rnd((last / oh[-5]["oi"] - 1) * 100, 2)
            D["oi_change_24h_pct"] = rnd((last / oh[-25]["oi"] - 1) * 100, 2)
    except DeskError as e:
        D["errors"].append("open interest history: %s" % e)
    for kind, key in (("global", "global_long_short"), ("top_position", "top_trader_long_short"), ("taker", "taker_buy_sell")):
        try:
            rows = get_ratio(net, kind, sym, "1h", 48)
            if rows:
                vals = [r["ratio"] for r in rows]
                D[key] = {"now": rnd(vals[-1], 3), "avg_48h": rnd(mean(vals), 3),
                          "pct_rank_48h": rnd(sum(1 for x in vals if x <= vals[-1]) / float(len(vals)), 2)}
        except DeskError as e:
            D["errors"].append("%s ratio: %s" % (kind, e))
    try:
        bids, asks = get_depth(net, sym, 100)
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2.0
            D["spread_bps"] = rnd((asks[0][0] - bids[0][0]) / mid * 1e4, 2)
            for band in (0.005, 0.01):
                D["bid_depth_usd_%s" % band] = rnd(sum(p * q for p, q in bids if p >= mid * (1 - band)), 0)
                D["ask_depth_usd_%s" % band] = rnd(sum(p * q for p, q in asks if p <= mid * (1 + band)), 0)
            D["book"] = {"bids": bids[:50], "asks": asks[:50]}
    except DeskError as e:
        D["errors"].append("order book: %s" % e)
    return D


def oi_price_read(price_chg_pct, oi_chg_pct):
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    up_p, up_oi = price_chg_pct > 0, oi_chg_pct > 0
    if up_p and up_oi:
        return "price up + OI up: new longs opening (trend fuel, also more longs to liquidate later)"
    if up_p and not up_oi:
        return "price up + OI down: short covering (often weaker follow-through)"
    if (not up_p) and up_oi:
        return "price down + OI up: new shorts opening (pressure, squeeze fuel if it reverses)"
    return "price down + OI down: longs closing or being liquidated (deleveraging)"


def slippage_estimate(book, side, notional):
    """Average fill vs mid for a market order of `notional` USDT, walking the visible book."""
    if not book or not book.get("bids") or not book.get("asks"):
        return None
    bids, asks = book["bids"], book["asks"]
    mid = (bids[0][0] + asks[0][0]) / 2.0
    levels = asks if side == "long" else bids
    left, cost, qty = notional, 0.0, 0.0
    for p, q in levels:
        take = min(left, p * q)
        cost += take
        qty += take / p
        left -= take
        if left <= 1e-9:
            break
    if left > 1e-9 or qty == 0:
        return None
    avg = cost / qty
    return abs(avg / mid - 1)


# ------------------------------------------------------------------ setups (candidates, never decisions)

def _stop_ok(entry, stop, a, cfg):
    d = abs(entry - stop)
    return a and cfg["stop_atr_min"] * a <= d <= cfg["stop_atr_max"] * a


def target_for_net_r(cfg, sym, side, entry, stop, want_r, entry_type=None, carry=0.0):
    """The target price that pays `want_r` R after fees, GST, slippage and `carry` (the funding the position is expected
    to pay over the hold, as a fraction of the entry; the same accounting as the plan's)."""
    cm = cost_model(cfg, sym, entry_type or cfg.get("default_entry_type", "limit"))
    loss = abs(entry - stop) + entry * cm["entry"] + stop * cm["stop"] + entry * carry
    if side == "long":
        return (entry * (1 + cm["entry"] + carry) + want_r * loss) / (1 - cm["target"])
    return (entry * (1 - cm["entry"] - carry) - want_r * loss) / (1 + cm["target"])


def net_r_of(cfg, sym, side, entry, stop, target, entry_type=None, carry=0.0):
    cm = cost_model(cfg, sym, entry_type or cfg.get("default_entry_type", "limit"))
    loss = abs(entry - stop) + entry * cm["entry"] + stop * cm["stop"] + entry * carry
    gain = abs(target - entry) - entry * cm["entry"] - target * cm["target"] - entry * carry
    return gain / loss if loss > 0 else None


def funding_carry(deriv, side, cfg):
    """Funding a position is expected to pay over the default hold of the primary timeframe, as a fraction of the
    entry (0 when it would receive funding): the current rate times the number of funding times in the hold."""
    fr = _f((deriv or {}).get("funding"))
    if fr is None:
        fr = 0.0001                       # unknown: assume the usual 0.01% per 8 hours (longs pay), never zero
    tf = cfg.get("primary_tf", "4h")
    hold_h = HORIZON_BARS.get(tf, 24) * TF_MIN.get(tf, 60) / 60.0
    ih = _f((deriv or {}).get("funding_interval_h")) or 8.0
    paid = fr if side == "long" else -fr
    return max(0.0, paid) * math.ceil(hold_h / ih)


def detect_setups(P, Pb, H, Hb, L15=None, deriv=None, levels=None, cfg=None, macro_hits=None, sym="default"):
    """Rule-based candidates on the primary timeframe (P/Pb) filtered by the higher one (H/Hb).
    Each candidate carries a transparent score. Scores are not probabilities. A first target at a
    level that pays less than min_net_rr after costs gates the candidate; without a usable level the
    first target is placed where it pays min_net_rr after costs."""
    cfg = cfg or DEFAULTS
    want = float(cfg.get("min_net_rr", 1.5)) + 0.05
    i, j = len(Pb) - 1, len(Hb) - 1
    c, a = P["c"][i], P["atr"][i]
    if not a or P["ema50"][i] is None:
        return []
    band = tuple(cfg["mid_range_band"])
    htf = trend_at(H, j)
    ptf = trend_at(P, i)
    st = structure(Pb)
    dr = st.get("dealing_range")
    pos = dr["pos"] if dr else don_pos(P, i)
    out = []

    def add(name, side, entry, stop, t1, t2, base, reasons, conflicts):
        if not _stop_ok(entry, stop, a, cfg):
            return
        risk = abs(entry - stop)
        if risk <= 0:
            return
        r1 = abs(t1 - entry) / risk if t1 is not None else None
        level_short = None
        carry = funding_carry(deriv, side, cfg)
        if r1 is None or r1 < 1.0:
            t1 = target_for_net_r(cfg, sym, side, entry, stop, want, carry=carry)
            reasons.append("first target set where it pays %.2fR after costs (no level at 1R or more)" % want)
        else:
            nr = net_r_of(cfg, sym, side, entry, stop, t1, carry=carry)
            if nr is not None and nr < cfg.get("min_net_rr", 1.5):
                level_short = nr
        if t2 is None or (side == "long" and t2 <= t1) or (side == "short" and t2 >= t1):
            t2 = entry + max(2.5, abs(t1 - entry) / risk + 1.0) * risk if side == "long" else entry - max(2.5, abs(t1 - entry) / risk + 1.0) * risk
        score = base
        if (side == "long" and htf == "up") or (side == "short" and htf == "down"):
            score += 10
            reasons.append("higher timeframe trend agrees (%s)" % htf)
        elif htf in ("up", "down"):
            score -= 15
            conflicts.append("against the higher timeframe trend (%s)" % htf)
        if deriv:
            fz = deriv.get("funding_z")
            if fz is not None:
                if side == "long" and fz >= 2:
                    score -= 10
                    conflicts.append("funding is unusually high (z %.1f): longs are crowded" % fz)
                if side == "short" and fz <= -2:
                    score -= 10
                    conflicts.append("funding is unusually negative (z %.1f): shorts are crowded" % fz)
            oi4 = deriv.get("oi_change_4h_pct")
            if oi4 is not None and abs(oi4) > 1:
                pchg = (c / P["c"][max(0, i - 4)] - 1) * 100
                if (side == "long" and pchg > 0 and oi4 > 0) or (side == "short" and pchg < 0 and oi4 > 0):
                    score += 5
                    reasons.append("open interest is building with the move (+%.1f%% in 4h)" % oi4)
        if P["cvd"] is not None and i >= 6:
            slope = P["cvd"][i] - P["cvd"][i - 6]
            if (side == "long" and slope > 0) or (side == "short" and slope < 0):
                score += 5
                reasons.append("net taker flow agrees over the last 6 bars")
            else:
                score -= 5
                conflicts.append("net taker flow disagrees over the last 6 bars")
        gates = []
        if level_short is not None:
            gates.append("the first target level pays only %.2fR after costs (min %.2fR)" % (level_short, cfg.get("min_net_rr", 1.5)))
        if pos is not None and band[0] <= pos <= band[1]:
            gates.append("mid-range: price sits at %.0f%% of the dealing range (your no-trade zone)" % (pos * 100))
        if macro_hits:
            gates.append("macro blackout: %s" % ", ".join("%s in %d min" % (e["title"], e["minutes_from_now"]) for e in macro_hits))
        if regime_at(P, i) == "HIGH_VOL" and cfg.get("block_high_vol_regime", True):
            gates.append("extreme volatility regime (ATR in the top 5% of the last 200 bars)")
        out.append({"setup": name, "side": side, "entry": sig(entry), "stop": sig(stop), "target1": sig(t1), "target2": sig(t2),
                    "risk_atr": rnd(risk / a, 2), "r_to_t1": rnd(abs(t1 - entry) / risk, 2),
                    "score": int(max(0, min(100, score))), "reasons": reasons, "conflicts": conflicts,
                    "gates": gates, "tradeable": not gates})

    e21 = P["ema21"][i]
    lows3 = min(Pb[k]["l"] for k in range(i - 2, i + 1))
    highs3 = max(Pb[k]["h"] for k in range(i - 2, i + 1))
    rsi_now = P["rsi"][i]
    sw_lo = st["swing_lows"][-1]["price"] if st["swing_lows"] else None
    sw_hi = st["swing_highs"][-1]["price"] if st["swing_highs"] else None

    # 1. trend pullback into value, reclaimed
    if ptf == "up" and htf != "down" and e21 is not None and lows3 <= e21 + 0.3 * a and c > e21 and rsi_now is not None and 38 <= rsi_now <= 62:
        stop = min(lows3, sw_lo if (sw_lo and sw_lo < c) else lows3) - 0.2 * a
        add("trend_pullback", "long", c, stop, sw_hi if (sw_hi and sw_hi > c) else None, None, 50,
            ["uptrend on %s, pullback tagged EMA21 and closed back above it" % "primary TF", "RSI cooled to %.0f" % rsi_now], [])
    if ptf == "down" and htf != "up" and e21 is not None and highs3 >= e21 - 0.3 * a and c < e21 and rsi_now is not None and 38 <= rsi_now <= 62:
        stop = max(highs3, sw_hi if (sw_hi and sw_hi > c) else highs3) + 0.2 * a
        add("trend_pullback", "short", c, stop, sw_lo if (sw_lo and sw_lo < c) else None, None, 50,
            ["downtrend on primary TF, rally tagged EMA21 and closed back below it", "RSI at %.0f" % rsi_now], [])

    # 2. range fade after a liquidity sweep of the range edge
    if P["adx"][i] is not None and P["adx"][i] < 20 and (H["adx"][j] is None or H["adx"][j] < 25) and i >= 49:
        prev_hi = max(P["h"][i - 48:i])
        prev_lo = min(P["l"][i - 48:i])
        width = prev_hi - prev_lo
        mid = (prev_hi + prev_lo) / 2.0
        if width >= 2 * a:
            b = Pb[i]
            if b["h"] > prev_hi and c < prev_hi:
                add("range_fade", "short", c, b["h"] + 0.2 * a, mid, prev_lo + 0.25 * width, 45,
                    ["swept the 48-bar range high %s and closed back inside" % fp(prev_hi), "ranging market (ADX %.0f)" % P["adx"][i]], [])
            if b["l"] < prev_lo and c > prev_lo:
                add("range_fade", "long", c, b["l"] - 0.2 * a, mid, prev_hi - 0.25 * width, 45,
                    ["swept the 48-bar range low %s and closed back inside" % fp(prev_lo), "ranging market (ADX %.0f)" % P["adx"][i]], [])

    # 3. squeeze release
    if i >= 9 and P["sq_on"][i] is False and sum(1 for k in range(i - 8, i) if P["sq_on"][k]) >= 6 and P["sq_mom"][i] is not None:
        sq_lo = min(P["l"][i - 8:i])
        sq_hi = max(P["h"][i - 8:i])
        if P["sq_mom"][i] > 0 and P["bb_up"][i] is not None and c > P["bb_up"][i]:
            stop = max(sq_lo, c - 1.2 * a) - 0.1 * a
            add("squeeze_break", "long", c, stop, None, None, 45,
                ["volatility squeeze (%d of the last 8 bars) released upward" % sum(1 for k in range(i - 8, i) if P["sq_on"][k]),
                 "close above the upper Bollinger band"], [])
        if P["sq_mom"][i] < 0 and P["bb_lo"][i] is not None and c < P["bb_lo"][i]:
            stop = min(sq_hi, c + 1.2 * a) + 0.1 * a
            add("squeeze_break", "short", c, stop, None, None, 45,
                ["volatility squeeze released downward", "close below the lower Bollinger band"], [])

    # 4. crowded positioning fade (contrarian, lower base score)
    if deriv and deriv.get("funding_z") is not None and pos is not None:
        fz = deriv["funding_z"]
        tt = (deriv.get("top_trader_long_short") or {}).get("pct_rank_48h")
        if fz >= 2 and pos > 0.8 and (tt is None or tt >= 0.7) and sw_hi:
            add("crowded_fade", "short", c, max(sw_hi, highs3) + 0.3 * a, P["vwap"][i] if (P["vwap"][i] and P["vwap"][i] < c) else None, None, 40,
                ["funding z %.1f: longs are paying heavily" % fz, "price in the top of the dealing range"], [])
        if fz <= -2 and pos < 0.2 and (tt is None or tt <= 0.3) and sw_lo:
            add("crowded_fade", "long", c, min(sw_lo, lows3) - 0.3 * a, P["vwap"][i] if (P["vwap"][i] and P["vwap"][i] > c) else None, None, 40,
                ["funding z %.1f: shorts are paying heavily" % fz, "price in the bottom of the dealing range"], [])

    # 5. previous-day level sweep and reclaim
    if levels:
        pdh, pdl = levels.get("prev_day_high"), levels.get("prev_day_low")
        dlo, dhi = levels.get("day_low"), levels.get("day_high")
        kz = levels.get("killzone_now")
        if pdl and dlo is not None and dlo < pdl < c and c - pdl < 1.0 * a:
            add("level_reclaim", "long", c, dlo - 0.2 * a, P["vwap"][i] if (P["vwap"][i] and P["vwap"][i] > c) else pdh, pdh, 45 + (5 if kz else 0),
                ["swept yesterday's low %s and reclaimed it" % fp(pdl)] + (["inside the %s killzone" % kz] if kz else []), [])
        if pdh and dhi is not None and dhi > pdh > c and pdh - c < 1.0 * a:
            add("level_reclaim", "short", c, dhi + 0.2 * a, P["vwap"][i] if (P["vwap"][i] and P["vwap"][i] < c) else pdl, pdl, 45 + (5 if kz else 0),
                ["swept yesterday's high %s and lost it again" % fp(pdh)] + (["inside the %s killzone" % kz] if kz else []), [])

    out.sort(key=lambda x: (-int(x["tradeable"]), -x["score"]))
    return out


SETUP_TRIGGERS = {}


def _htf(F, key, i):
    """The higher timeframe's value at primary bar i (F['htf_trend'] / F['htf_adx'], aligned by cmd_simulate), or None."""
    arr = F.get(key)
    return arr[i] if arr is not None and i < len(arr) else None


def _trig_pullback(side):
    # the live rule (detect_setups) also refuses a pullback against the higher timeframe's trend, so its past cases do too
    def f(F, bars, i):
        if i < 3 or F["ema21"][i] is None or F["atr"][i] is None or F["rsi"][i] is None:
            return False
        a, e = F["atr"][i], F["ema21"][i]
        if side == "long":
            return (trend_at(F, i) == "up" and _htf(F, "htf_trend", i) != "down" and min(F["l"][i - 2:i + 1]) <= e + 0.3 * a
                    and F["c"][i] > e and 38 <= F["rsi"][i] <= 62)
        return (trend_at(F, i) == "down" and _htf(F, "htf_trend", i) != "up" and max(F["h"][i - 2:i + 1]) >= e - 0.3 * a
                and F["c"][i] < e and 38 <= F["rsi"][i] <= 62)
    return f


def _trig_range(side):
    # as live: a ranging primary timeframe (ADX < 20) inside a non-trending higher one (ADX < 25), range at least 2 ATR wide
    def f(F, bars, i):
        if i < 49 or F["adx"][i] is None or F["adx"][i] >= 20 or not F["atr"][i]:
            return False
        hadx = _htf(F, "htf_adx", i)
        if hadx is not None and hadx >= 25:
            return False
        hi, lo = max(F["h"][i - 48:i]), min(F["l"][i - 48:i])
        if hi - lo < 2 * F["atr"][i]:
            return False
        if side == "short":
            return F["h"][i] > hi and F["c"][i] < hi
        return F["l"][i] < lo and F["c"][i] > lo
    return f


def align_htf(bars, hbars):
    """For each primary bar, the higher timeframe's trend and ADX as of its close (the last higher bar closed by then),
    as the live detector sees them. Returns (trends, adxs) or (None, None)."""
    if not hbars:
        return None, None
    HF = tf_features(hbars)
    trends, adxs, j = [], [], -1
    for b in bars:
        while j + 1 < len(hbars) and hbars[j + 1]["T"] <= b["T"]:
            j += 1
        trends.append(trend_at(HF, j) if j >= 0 else None)
        adxs.append(HF["adx"][j] if j >= 0 else None)
    return trends, adxs


def _trig_squeeze(side):
    def f(F, bars, i):
        if i < 9 or F["sq_on"][i] is not False or F["sq_mom"][i] is None:
            return False
        if sum(1 for k in range(i - 8, i) if F["sq_on"][k]) < 6:
            return False
        if side == "long":
            return F["sq_mom"][i] > 0 and F["bb_up"][i] is not None and F["c"][i] > F["bb_up"][i]
        return F["sq_mom"][i] < 0 and F["bb_lo"][i] is not None and F["c"][i] < F["bb_lo"][i]
    return f


def _trig_level_reclaim(side):
    """Yesterday's low swept today and reclaimed (long), or yesterday's high swept and lost (short), with the close
    within 1 ATR of the level: the same reading detect_setups makes live, on UTC days built from the bars themselves."""
    def f(F, bars, i):
        a = F["atr"][i]
        if not a or i < 2:
            return False
        day = bars[i]["t"] // DAY_MS
        j = i
        while j > 0 and bars[j - 1]["t"] // DAY_MS == day:
            j -= 1
        k = j
        while k > 0 and bars[k - 1]["t"] // DAY_MS == day - 1:
            k -= 1
        if k == j:
            return False
        c = bars[i]["c"]
        if side == "long":
            pdl = min(b["l"] for b in bars[k:j])
            dlo = min(b["l"] for b in bars[j:i + 1])
            return dlo < pdl < c and c - pdl < 1.0 * a
        pdh = max(b["h"] for b in bars[k:j])
        dhi = max(b["h"] for b in bars[j:i + 1])
        return dhi > pdh > c and pdh - c < 1.0 * a
    return f


for _s in ("long", "short"):
    SETUP_TRIGGERS[("trend_pullback", _s)] = _trig_pullback(_s)
    SETUP_TRIGGERS[("range_fade", _s)] = _trig_range(_s)
    SETUP_TRIGGERS[("squeeze_break", _s)] = _trig_squeeze(_s)
    SETUP_TRIGGERS[("level_reclaim", _s)] = _trig_level_reclaim(_s)

# Setups whose past cases cannot be picked out of price bars alone (crowded_fade needs funding and positioning
# history that the exchanges do not serve far enough back). Their history test can only match the market state.
UNTESTABLE_SETUPS = ("crowded_fade",)


# ------------------------------------------------------------------ costs, liquidation, sizing

def liq_price(side, entry, lev, mmr, maint_amount=0.0, qty=None):
    """Isolated-margin liquidation estimate for one position (Binance-style formula):
    long  LP = (EP*(1 - 1/L) - cum/Q) / (1 - MMR)
    short LP = (EP*(1 + 1/L) + cum/Q) / (1 + MMR)
    The venue's own number is the one that counts."""
    adj = (maint_amount / qty) if (maint_amount and qty) else 0.0
    if side == "long":
        return (entry * (1 - 1.0 / lev) - adj) / (1 - mmr)
    return (entry * (1 + 1.0 / lev) + adj) / (1 + mmr)


def max_safe_leverage(side, d_req, mmr):
    """Largest leverage whose liquidation sits at least d_req (fraction) away from entry."""
    k = d_req * (1 - mmr) + mmr if side == "long" else d_req * (1 + mmr) + mmr
    return 1.0 / k if k > 0 else 125.0


def funding_events_between(t0, t1, interval_h, next_funding=None):
    if t1 <= t0:
        return []
    if not interval_h or interval_h <= 0:
        interval_h = 8.0
    step = int(interval_h * 3600000)
    if next_funding and next_funding > t0:
        first = next_funding
        while first - step > t0:
            first -= step
    else:
        first = (t0 // step + 1) * step
    out = []
    t = first
    while t <= t1:
        out.append(t)
        t += step
    return out


def cost_model(cfg, sym, entry_type="market"):
    fees = cfg["fees"]
    gst = 1.0 + float(cfg.get("gst_on_fees_pct") or 0) / 100.0
    slip = per_asset(cfg["slippage"], sym)
    entry = fees["maker"] * gst if entry_type == "limit" else (fees["taker"] * gst + slip)
    stop_exit = fees["taker"] * gst + slip
    tp_exit = fees["maker"] * gst if cfg.get("tp_order") == "limit" else fees["taker"] * gst + slip
    time_exit = fees["taker"] * gst + slip
    return {"entry": entry, "stop": stop_exit, "target": tp_exit, "time": time_exit}


def size_position(cfg, sym, side, entry, stop, targets, atr_now=None, funding_rate=None, funding_interval_h=8.0,
                  next_funding=None, hold_hours=8.0, entry_type="market", lot=None, equity=None, start_ms=None):
    side = side.lower()
    if side not in ("long", "short"):
        raise DeskError("side must be long or short")
    if entry <= 0 or stop <= 0:
        raise DeskError("prices must be positive")
    if side == "long" and not stop < entry:
        raise DeskError("a long needs the stop below the entry")
    if side == "short" and not stop > entry:
        raise DeskError("a short needs the stop above the entry")
    targets = [t for t in (targets or []) if t]
    for t in targets:
        if (side == "long" and t <= entry) or (side == "short" and t >= entry):
            raise DeskError("target %s is on the wrong side of the entry" % fp(t))
    if str(cfg.get("equity_currency") or "USDT").upper() not in ("USDT", "USD"):
        raise DeskError("equity must be in USDT here (load_config converts other currencies using usdt_fx)")
    equity = float(equity or cfg["equity"])
    risk_budget = equity * cfg["risk_per_trade_pct"] / 100.0
    stop_dist = abs(entry - stop)
    stop_frac = stop_dist / entry
    cm = cost_model(cfg, sym, entry_type)
    start_ms = start_ms or now_ms()
    events = funding_events_between(start_ms, start_ms + int(hold_hours * 3600000), funding_interval_h, next_funding)
    rate = funding_rate or 0.0
    fund_frac = len(events) * rate * (1 if side == "long" else -1)
    loss_unit = stop_dist + entry * cm["entry"] + stop * cm["stop"] + entry * max(fund_frac, 0.0)
    qty = risk_budget / loss_unit
    notes = []
    step = (lot or {}).get("step")
    if step:
        qty = math.floor(qty / step + 1e-9) * step
    mmr = float(per_asset(cfg["maintenance_margin_rate"], sym))
    atr_frac = (atr_now / entry) if atr_now else 0.0
    d_req = max(cfg["liq_buffer_mult"] * stop_frac, stop_frac + cfg["min_liq_atr_beyond_stop"] * atr_frac)
    lev_safe = max_safe_leverage(side, d_req, mmr)
    lev_cap = int(max(1, min(int(cfg["max_leverage"]), math.floor(lev_safe))))
    max_margin = equity * cfg["max_margin_per_trade_pct"] / 100.0
    notional = qty * entry
    lev = max(1, int(math.ceil(notional / max_margin - 1e-9))) if max_margin > 0 else lev_cap
    if lev > lev_cap:
        lev = lev_cap
        qty_fit = max_margin * lev / entry
        if step:
            qty_fit = math.floor(qty_fit / step + 1e-9) * step
        if qty_fit < qty:
            notes.append("size cut to fit your margin cap at %dx (risk now below your per-trade budget)" % lev)
            qty = qty_fit
        notional = qty * entry
    if lot and lot.get("min_qty") and qty < lot["min_qty"]:
        notes.append("size is below the exchange minimum (%s): skip or widen the budget" % lot["min_qty"])
    if lot and lot.get("min_notional") and notional < lot["min_notional"]:
        notes.append("order value is below the exchange minimum (%s USDT)" % lot["min_notional"])
    margin = notional / lev if lev else notional
    liq = liq_price(side, entry, lev, mmr)
    liq_frac = abs(entry - liq) / entry
    if step:
        dec = len(("%.10f" % step).rstrip("0").split(".")[1])
        qty = round(math.floor(qty / step + 1e-9) * step, dec)
    else:
        qty = sig_down(qty, 8)
    notional = qty * entry
    margin = notional / lev if lev else notional
    loss_at_stop = qty * loss_unit
    tlist = []
    for t in targets:
        gain_unit = abs(t - entry) - entry * cm["entry"] - t * cm["target"] - entry * max(fund_frac, 0.0)
        r_net = gain_unit / loss_unit if loss_unit > 0 else None
        tlist.append({"price": sig(t), "gross_r": rnd(abs(t - entry) / stop_dist, 2), "net_r": rnd(r_net, 2),
                      "profit": rnd(qty * gain_unit, 2), "breakeven_winrate": rnd(1.0 / (1.0 + r_net), 3) if r_net and r_net > 0 else None})
    out = {
        "symbol": sym, "side": side, "entry": sig(entry), "stop": sig(stop), "entry_type": entry_type,
        "targets": tlist, "equity": equity, "risk_budget": rnd(risk_budget, 2), "risk_pct": cfg["risk_per_trade_pct"],
        "qty": qty, "notional": rnd(notional, 2), "leverage": lev, "margin": rnd(margin, 2),
        "leverage_safe_max": rnd(lev_safe, 1), "leverage_cap_used": lev_cap,
        "mmr_assumed": mmr, "liquidation_est": sig(liq), "liq_distance_pct": rnd(liq_frac * 100, 2),
        "stop_distance_pct": rnd(stop_frac * 100, 3), "liq_to_stop_ratio": rnd(liq_frac / stop_frac, 2) if stop_frac else None,
        "stop_atr": rnd(stop_dist / atr_now, 2) if atr_now else None,
        "max_loss_at_stop": rnd(loss_at_stop, 2), "max_loss_pct_equity": rnd(loss_at_stop / equity * 100, 3),
        "costs_pct": {k: rnd(v * 100, 4) for k, v in cm.items()},
        "funding_rate": funding_rate, "funding_events_in_hold": len(events), "funding_cost_pct": rnd(fund_frac * 100, 4),
        "hold_hours": hold_hours, "notes": notes,
    }
    fx = cfg.get("local_rate") or cfg.get("usdt_inr")
    if fx:
        fx = float(fx)
        out["inr"] = {"max_loss": rnd(loss_at_stop * fx, 0), "margin": rnd(margin * fx, 0), "rate_used": fx,
                      "currency": cfg.get("local_currency") or "INR"}
    return out


# ------------------------------------------------------------------ scenarios

def walk_trade(bars, i0, side, entry, stop, target, H):
    """Enter at bar i0's close, walk forward up to H bars. Stop is checked first in
    every bar (a bar that touches both counts as a loss). A bar that opens beyond
    the stop fills at its open (gap)."""
    long_ = side == "long"
    for k in range(1, H + 1):
        j = i0 + k
        if j >= len(bars):
            return "open", bars[-1]["c"], k - 1
        b = bars[j]
        if long_:
            if b["o"] <= stop:
                return "stop", b["o"], k
            if b["l"] <= stop:
                return "stop", stop, k
            if b["h"] >= target:
                return "target", target, k
        else:
            if b["o"] >= stop:
                return "stop", b["o"], k
            if b["h"] >= stop:
                return "stop", stop, k
            if b["l"] <= target:
                return "target", target, k
    return "time", bars[i0 + H]["c"], H


def r_net(side, entry, stop, exit_px, outcome, cm, fund_frac):
    risk = abs(entry - stop)
    gross = (exit_px - entry) if side == "long" else (entry - exit_px)
    costs = entry * cm["entry"] + exit_px * cm.get(outcome if outcome in cm else "time", cm["time"]) + entry * fund_frac
    return (gross - costs) / risk if risk > 0 else 0.0


def _fund_frac(side, t0, t1, funding_hist, fallback_rate, interval_h):
    if funding_hist:
        ts = [t for t, _ in funding_hist]
        lo, hi = bisect.bisect_right(ts, t0), bisect.bisect_right(ts, t1)
        rates = [r for _, r in funding_hist[lo:hi]]
        if not rates and t1 - t0 >= interval_h * 3600000:
            rates = [fallback_rate or 0.0] * int((t1 - t0) // (interval_h * 3600000))
    else:
        rates = [fallback_rate or 0.0] * int((t1 - t0) // (interval_h * 3600000))
    s = sum(rates)
    return s if side == "long" else -s


def analog_samples(bars, F, side, stop_atr, target_atr, H, keys, cur_state, trigger, cm, funding_hist,
                   fallback_rate, interval_h, warmup=210, band=(0.4, 0.6)):
    tfm = (bars[1]["t"] - bars[0]["t"]) if len(bars) > 1 else 3600000
    out = []
    last_exit = -1
    end = len(bars) - H - 1
    for i in range(warmup, end):
        if i <= last_exit:
            continue
        if keys:
            st = state_at(F, i, band)
            if any(st.get(k) != cur_state.get(k) for k in keys):
                continue
        if trigger is not None and not trigger(F, bars, i):
            continue
        a = F["atr"][i]
        if not a:
            continue
        e = bars[i]["c"]
        if side == "long":
            s, t = e - stop_atr * a, e + target_atr * a
        else:
            s, t = e + stop_atr * a, e - target_atr * a
        oc, px, held = walk_trade(bars, i, side, e, s, t, H)
        if oc == "open":
            continue
        t0 = bars[i]["T"]
        t1 = t0 + held * tfm
        ff = _fund_frac(side, t0, t1, funding_hist, fallback_rate, interval_h)
        out.append({"i": i, "t": bars[i]["t"], "outcome": oc, "R": r_net(side, e, s, px, oc, cm, ff), "held": held})
        last_exit = i + max(1, held)
    return out


def summarize_samples(samples):
    n = len(samples)
    wins = sum(1 for s in samples if s["outcome"] == "target")
    losses = sum(1 for s in samples if s["outcome"] == "stop")
    timeouts = n - wins - losses
    Rs = [s["R"] for s in samples]
    lo, hi = wilson(wins, wins + losses)
    mlo, mhi = boot_mean_ci(Rs)
    tlo, thi = wilson(wins, n)
    return {"n": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "target_first_rate": rnd(wins / float(n), 3) if n else None,
            "target_first_ci90": [rnd(tlo, 3), rnd(thi, 3)] if tlo is not None else None,
            "win_rate_decided": rnd(wins / float(wins + losses), 3) if wins + losses else None,
            "win_rate_ci90": [rnd(lo, 3), rnd(hi, 3)] if lo is not None else None,
            "expectancy_r": rnd(mean(Rs), 3) if Rs else None,
            "expectancy_ci90": [rnd(mlo, 3), rnd(mhi, 3)] if mlo is not None else None,
            "avg_bars_held": rnd(mean([s["held"] for s in samples]), 1) if samples else None}


def block_bootstrap(bars, side, entry, stop, target, H, paths=4000, block=12, seed=7, lookback=1500, liq=None):
    rel = []
    seg = bars[-(lookback + 1):]
    for k in range(1, len(seg)):
        pc = seg[k - 1]["c"]
        b = seg[k]
        rel.append((b["o"] / pc, b["h"] / pc, b["l"] / pc, b["c"] / pc))
    n = len(rel)
    if n < 100:
        raise DeskError("not enough bars for the bootstrap")
    rng = random.Random(seed)
    p_new = 1.0 / max(1, block)
    long_ = side == "long"
    tgt = stp = tim = liq_hits = 0
    finals, maes = [], []
    for _ in range(paths):
        idx = rng.randrange(n)
        p = entry
        outcome = None
        worst = entry
        hit_liq = False
        for k in range(H):
            if k > 0:
                idx = rng.randrange(n) if rng.random() < p_new else (idx + 1) % n
            o, h, l_, c = rel[idx]
            hi, lo = p * h, p * l_
            if long_:
                worst = min(worst, lo)
                if liq is not None and lo <= liq:
                    hit_liq = True
                if outcome is None:
                    if lo <= stop:
                        outcome = "stop"
                    elif hi >= target:
                        outcome = "target"
            else:
                worst = max(worst, hi)
                if liq is not None and hi >= liq:
                    hit_liq = True
                if outcome is None:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p *= c
        if outcome == "target":
            tgt += 1
        elif outcome == "stop":
            stp += 1
        else:
            tim += 1
        liq_hits += 1 if hit_liq else 0
        finals.append(p)
        maes.append(abs(worst / entry - 1))
    finals.sort()
    maes.sort()
    decided = tgt + stp
    return {"paths": paths, "block_bars": block, "seed": seed, "horizon_bars": H,
            "p_target_first": rnd(tgt / float(paths), 3), "p_stop_first": rnd(stp / float(paths), 3),
            "p_neither": rnd(tim / float(paths), 3),
            "target_share_of_decided": rnd(tgt / float(decided), 3) if decided else None,
            "price_at_horizon_p5_p50_p95": [sig(percentile(finals, 0.05)), sig(percentile(finals, 0.5)), sig(percentile(finals, 0.95))],
            "adverse_move_p50_p95_pct": [rnd(percentile(maes, 0.5) * 100, 2), rnd(percentile(maes, 0.95) * 100, 2)],
            "p_wick_through_liquidation": rnd(liq_hits / float(paths), 4) if liq is not None else None,
            "note": "Volatility baseline only: it reshuffles recent bars in blocks, so it knows this coin's "
                    "volatility but not your setup. Compare it with the analog test, never read it as an edge."}


def stress_tests(bars, side, entry, stop, liq, a, cm, slip_stress=0.005, outage_bars=1, lev=1, liq_fee=0.0075):
    long_ = side == "long"
    adv = []
    for k in range(1, len(bars)):
        pc = bars[k - 1]["c"]
        adv.append((bars[k]["l"] / pc - 1) if long_ else (bars[k]["h"] / pc - 1))
    if not adv:
        return {}
    s = sorted(adv)
    worst = s[0] if long_ else s[-1]
    p999 = percentile(s, 0.001) if long_ else percentile(s, 0.999)
    risk = abs(entry - stop)

    def reach(move):
        return entry * (1 + move)
    out = {"bars_examined": len(adv)}
    for label, mv in (("worst_1bar_move", worst), ("p99.9_1bar_move", p999)):
        px = reach(mv)
        through_stop = (px <= stop) if long_ else (px >= stop)
        through_liq = (px <= liq) if long_ else (px >= liq)
        out[label] = {"move_pct": rnd(mv * 100, 2), "move_atr": rnd(abs(mv) * entry / a, 2) if a else None,
                      "price": sig(px), "crosses_stop": through_stop, "crosses_liquidation": through_liq}
    slipped = stop * (1 - slip_stress) if long_ else stop * (1 + slip_stress)
    out["stop_slips_%.1fpct" % (slip_stress * 100)] = {
        "fill": sig(slipped), "loss_r": rnd(-(abs(entry - slipped) + entry * cm["entry"] + slipped * cm["stop"]) / risk, 2)}
    gaps = []
    for k in range(outage_bars, len(bars)):
        pc = bars[k - outage_bars]["c"]
        seg = bars[k - outage_bars + 1:k + 1]
        ext = min(b["l"] for b in seg) if long_ else max(b["h"] for b in seg)
        gaps.append(ext / pc - 1)
    gaps.sort()
    g = percentile(gaps, 0.01) if long_ else percentile(gaps, 0.99)
    out["cannot_exit_for_%d_bar%s_p99" % (outage_bars, "s" if outage_bars > 1 else "")] = {
        "move_pct": rnd(g * 100, 2), "loss_r_if_filled_there": rnd(-(abs(g) * entry) / risk, 2),
        "crosses_liquidation": (reach(g) <= liq) if long_ else (reach(g) >= liq)}
    out["if_liquidated"] = {"loss_r": rnd(-(entry / max(lev, 1) + entry * liq_fee) / risk, 2),
                            "note": "isolated margin: you lose the whole margin plus a liquidation fee (%.2f%% of the position)" % (liq_fee * 100)}
    return out


def edge_verdict(an, base, be_rate, diff=None):
    """diff: 90% interval of (this setup's average R - the baseline's), when the raw samples were available."""
    if an is None or an["n"] < 20 or (an["wins"] + an["losses"]) < 15:
        return "INSUFFICIENT HISTORY: fewer than 20 comparable past cases, so the history cannot tell good from bad here."
    e, ci = an["expectancy_r"], an["expectancy_ci90"]
    be, bci = (base or {}).get("expectancy_r"), (base or {}).get("expectancy_ci90")
    vs = None
    if diff and diff[0] is not None and be is not None:
        vs = "better" if diff[0] > 0 else ("worse" if diff[1] < 0 else "same")
    elif be is not None and bci and bci[0] is not None and ci and ci[0] is not None:
        vs = "better" if ci[0] > bci[1] else ("worse" if ci[1] < bci[0] else "same")
    if ci and ci[1] is not None and ci[1] < 0:
        head = "NEGATIVE: comparable past cases lost money after costs (90%% interval %+.2fR to %+.2fR)." % (ci[0], ci[1])
    elif ci and ci[0] is not None and ci[0] > 0 and vs == "better":
        head = "POSITIVE (historical): comparable past cases made money after costs (90%% interval %+.2fR to %+.2fR)." % (ci[0], ci[1])
    elif ci and ci[0] is not None and ci[0] > 0:
        head = ("MARKET DRIFT, NOT THE SETUP: comparable past cases made money after costs (90%% interval %+.2fR to %+.2fR), "
                "but random entries did about as well, so the setup adds nothing measurable." % (ci[0], ci[1]))
    elif e is not None and e > 0:
        head = "UNPROVEN: average %+.2fR after costs, but the 90%% interval (%+.2fR to %+.2fR) includes zero." % (e, ci[0], ci[1])
    else:
        head = "NO EDGE: comparable past cases averaged %+.2fR after costs." % (e if e is not None else 0.0)
    if vs == "better":
        head += " Better than random entries with the same stop and target (%+.2fR)." % be
    elif vs == "worse":
        head += " Worse than random entries with the same stop and target (%+.2fR)." % be
    elif vs == "same":
        head += " Not distinguishable from random entries with the same stop and target (%+.2fR)." % be
    if be_rate:
        head += " Break-even win rate after costs: %.0f%%." % (be_rate * 100)
    return head


# ------------------------------------------------------------------ journal

def journal_path(home):
    return os.path.join(home, "journal.jsonl")


def journal_load(home):
    p = journal_path(home)
    rows = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("type") in ("trade", "forecast") and row.get("ts"):
                        rows.append(row)
    return rows


def journal_save(home, rows):
    p = journal_path(home)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, p)


def local_day(ms, tz_minutes):
    return _local(ms, tz_minutes)[0].date().isoformat()


def journal_today(rows, cfg, now=None):
    now = now_ms() if now is None else now
    tzm = cfg["tz_offset_minutes"]
    today = local_day(now, tzm)
    trades = [r for r in rows if r.get("type") == "trade" and r.get("decision") in ("TAKE", "PAPER")
              and local_day(r["ts"], tzm) == today]
    closed_today = [r for r in rows if r.get("type") == "trade" and r.get("decision") in ("TAKE", "PAPER")
                    and r.get("status") == "resolved" and (r.get("outcome") or {}).get("exit_t")
                    and local_day(r["outcome"]["exit_t"], tzm) == today]
    realized = sum((r.get("outcome") or {}).get("pnl") or 0.0 for r in closed_today)
    open_pos = [r for r in rows if r.get("type") == "trade" and r.get("decision") in ("TAKE", "PAPER")
                and r.get("status") in ("open", "filled")]
    open_risk = 0.0
    for r in open_pos:
        ml = _f(r.get("max_loss"))
        if ml is None:
            q, e_, s_ = _f(r.get("qty")), _f(r.get("entry")), _f(r.get("stop"))
            ml = abs(e_ - s_) * q if None not in (q, e_, s_) else 0.0
        open_risk += ml
    seq = [r for r in rows if r.get("type") == "trade" and r.get("status") == "resolved" and r.get("decision") != "TRACK"]
    seq.sort(key=lambda r: (r.get("outcome") or {}).get("exit_t") or r["ts"])
    consec = 0
    for r in reversed(seq):
        if (r.get("outcome") or {}).get("R", 0) < 0:
            consec += 1
        else:
            break
    last_loss_day = local_day((seq[-1].get("outcome") or {}).get("exit_t") or seq[-1]["ts"], tzm) if seq else None
    return {"today": today, "trades_today": len(trades), "realized_pnl_today": rnd(realized, 2), "open": open_pos,
            "open_risk": rnd(open_risk, 2), "consecutive_losses": consec, "last_resolved_day": last_loss_day}


def probation_status(rows, cfg):
    """UNPROVEN (signals at `unproven_risk_multiplier` of normal risk) until at least
    `probation_min_trades` (default 50) resolved trades show an average R whose 90% bootstrap
    interval sits above zero; then PROVEN (full size). A kill switch returns to UNPROVEN after
    -10R in real (TAKE) trades, 8 real losses in a row, or a negative real average over the last 30."""
    res = [r for r in rows if r.get("type") == "trade" and r.get("status") == "resolved"
           and r.get("decision") in ("TAKE", "PAPER")]
    res.sort(key=lambda r: (r.get("outcome") or {}).get("exit_t") or r.get("ts", 0))
    # small-signal trades are scored apart: they can trip the kill switch below, never prove the edge
    Rs = [x for x in ((r.get("outcome") or {}).get("R") for r in res if r.get("tier") != "small") if x is not None]
    live = [x for x in ((r.get("outcome") or {}).get("R") for r in res if r.get("decision") == "TAKE") if x is not None]
    if live:
        streak = 0
        for x in reversed(live):
            if x < 0:
                streak += 1
            else:
                break
        if sum(live) <= -10 or streak >= 8 or (len(live) >= 30 and mean(live[-30:]) < 0):
            return {"status": "UNPROVEN", "why": "kill switch: your real trades are %+.1fR in total with %d losses in a row" % (sum(live), streak),
                    "n": len(Rs), "kill_switch": True}
    need = int(cfg.get("probation_min_trades", 50))
    if len(Rs) < need:
        return {"status": "UNPROVEN", "why": "%d of %d resolved trades logged" % (len(Rs), need), "n": len(Rs)}
    m = mean(Rs)
    lo, hi = boot_mean_ci(Rs)
    if lo is not None and lo > 0:
        return {"status": "PROVEN", "why": "average %+.2fR over %d trades (90%% interval %+.2fR to %+.2fR)" % (m, len(Rs), lo, hi),
                "n": len(Rs)}
    return {"status": "UNPROVEN", "why": "average %+.2fR over %d trades; the 90%% interval (%+.2fR to %+.2fR) still includes zero"
            % (m, len(Rs), lo if lo is not None else 0.0, hi if hi is not None else 0.0), "n": len(Rs)}


def live_trade_count(rows):
    return sum(1 for r in rows if r.get("type") == "trade" and r.get("decision") == "TAKE" and r.get("status") == "resolved")


def resolve_trade(net, r, now=None):
    """Score a logged trade against the 5-minute bars that opened after it was logged.
    Conservative: inside the bar where a limit or stop entry fills, the order of high and
    low is unknown, so a stop touch there counts as a loss and a target touch is ignored."""
    now = now_ms() if now is None else now
    sym, side = r["symbol"], r["side"]
    start = int(r["ts"])
    horizon_ms = int(float(r.get("horizon_hours", 24)) * 3600000)
    fill_ms = int(float(r.get("fill_window_hours", 4)) * 3600000)
    end = min(now, start + fill_ms + horizon_ms + 300000)
    if end <= start:
        return None
    need = int((end - start) / 300000) + 3
    bars = get_klines(net, sym, "5m", min(need, 3000), end_ms=end)
    bars = [b for b in bars if b["t"] >= start and b["T"] < now]
    if not bars:
        return None
    entry, stop = float(r["entry"]), float(r["stop"])
    target = float(r["targets"][0]) if r.get("targets") else None
    et = r.get("entry_type", "market")
    long_ = side == "long"
    k0, k_first = 0, 0
    if et != "market":
        k0 = None
        for k, b in enumerate(bars):
            if b["t"] - start > fill_ms:
                break
            if et == "limit":
                touched = b["l"] <= entry if long_ else b["h"] >= entry
            else:
                touched = b["h"] >= entry if long_ else b["l"] <= entry
            if touched:
                k0 = k
                break
        if k0 is None:
            if now - start > fill_ms:
                return {"status": "expired", "note": "entry never filled inside the fill window"}
            return None
        fb = bars[k0]
        if (fb["l"] <= stop) if long_ else (fb["h"] >= stop):
            return _finish(r, "stop", stop, fb["T"])
        k_first = k0 + 1
    deadline = bars[k0]["t"] + horizon_ms
    for b in bars[k_first:]:
        if b["t"] > deadline:
            break
        if (b["o"] <= stop) if long_ else (b["o"] >= stop):
            return _finish(r, "stop", b["o"], b["T"])
        if (b["l"] <= stop) if long_ else (b["h"] >= stop):
            return _finish(r, "stop", stop, b["T"])
        if target is not None and ((b["h"] >= target) if long_ else (b["l"] <= target)):
            return _finish(r, "target", target, b["T"])
    if now >= deadline + 300000:
        last = [b for b in bars if b["t"] <= deadline]
        if last:
            return _finish(r, "time", last[-1]["c"], last[-1]["T"])
    return None


def _finish(r, oc, px, t):
    entry, stop = float(r["entry"]), float(r["stop"])
    side = r["side"]
    risk = abs(entry - stop)
    gross = (px - entry) if side == "long" else (entry - px)
    cost_pct = r.get("cost_pct_roundtrip") or 0.0
    R = (gross - entry * cost_pct / 100.0) / risk if risk else 0.0
    qty = float(r.get("qty") or 0)
    return {"status": "resolved", "outcome": {"result": oc, "exit_price": sig(px), "exit_t": int(t), "R": rnd(R, 3),
                                              "pnl": rnd(R * risk * qty, 2) if qty else None}}


def resolve_forecast(net, r, now=None):
    now = now_ms() if now is None else now
    end = r["ts"] + int(float(r.get("horizon_hours", 24)) * 3600000)
    if now < end:
        return None
    bars = get_klines(net, r["symbol"], "5m", 4, end_ms=end)
    bars = [b for b in bars if b["T"] <= end]
    if not bars:
        return None
    px = bars[-1]["c"]
    up = 1.0 if px > float(r["ref_price"]) else 0.0
    p = float(r["p_up"])
    return {"status": "resolved", "outcome": {"price": sig(px), "went_up": bool(up), "brier": rnd((p - up) ** 2, 4)}}


def track_ideas(home, cfg, sym, now, decision, plan, cand, E, price=None):
    """Log the engine's own calls as TRACK records, scored by 'journal resolve' like paper trades: a forward,
    out-of-sample record kept automatically for every SIGNAL, CHART PASS and live news idea. TRACK records never
    count toward your edge status, daily limits, loss streak or open positions."""
    if not cfg.get("auto_track", True):
        return []
    rows = journal_load(home)
    have = {r.get("track_key") for r in rows if r.get("decision") == "TRACK"}
    cm = cost_model(cfg, sym, "market")
    rt = rnd((cm["entry"] + cm["stop"]) * 100.0, 4)
    new = []
    d = (decision or {}).get("decision") or ""
    rel = (E or {}).get("release")

    blocked_by = [str(b).split(":")[0][:40] for b in (decision or {}).get("blocks") or []] if not d.startswith("SIGNAL") else []

    def add(kind, setup, side, entry, stop, t1, entry_type, fill_h, hold_h, evidence, anchor):
        k = "%s|%s|%s|%s" % (sym, setup, side, anchor)
        if k in have or not (entry and stop and t1) or hold_h <= 0:
            return
        have.add(k)
        new.append({"id": "%s-%s-T%d" % (utc(now).strftime("%Y%m%d-%H%M%S"), base_of(sym), len(new) + 1), "type": "trade",
                    "decision": "TRACK", "track_kind": kind, "track_key": k, "ts": now, "symbol": sym, "side": side,
                    "entry": entry, "stop": stop, "targets": [t1], "entry_type": entry_type, "fill_window_hours": rnd(fill_h, 3),
                    "horizon_hours": rnd(hold_h, 2), "cost_pct_roundtrip": rt, "setup": setup, "evidence": evidence,
                    "release_kind": (E or {}).get("kind") if kind != "chart pass" else None, "engine_version": VERSION,
                    "status": "open", "mode": "track"})
        if kind == "news idea" and blocked_by and cand and cand.get("setup") == setup:
            new[-1]["blocked_by"] = blocked_by[:5]
    off = any(b == CHART_SIGNALS_OFF for b in (decision or {}).get("blocks") or [])
    if plan and cand and (d.startswith("SIGNAL") or (d == "NO TRADE" and off and cand.get("kind") == "setup")):
        add(("small signal" if plan.get("tier") == "small" else "signal") if d.startswith("SIGNAL") else "chart pass", plan.get("setup"), plan["side"], plan["entry"], plan["stop"],
            plan["targets"][0]["price"], plan.get("entry_type", "limit"), float(plan.get("fill_window_hours") or 4),
            float(plan.get("horizon_hours") or 24), cand.get("evidence") or "POSITIVE",
            rel if cand.get("kind") == "event" else sig(plan["entry"]))
    for c in (E or {}).get("candidates") or []:
        if c.get("status") == "LIVE":
            cc = reprice_event(c, price)                     # as tested: a market entry measured from the price now
            if cc is None:
                continue
            add("news idea", cc["setup"], cc["side"], cc["entry"], cc["stop"], cc["target1"], "market",
                max(0.0, (cc["expires"] - now) / 3600000.0), max(0.0, (cc["close_by"] - now) / 3600000.0), cc.get("evidence"), rel)
    if new:
        journal_save(home, rows + new)
    return new


def track_summary(rows):
    """The live record of the engine's own calls, by kind and rule (resolved TRACK records only)."""
    out = {}
    for r in rows:
        if r.get("decision") != "TRACK":
            continue
        k = (r.get("track_kind"), r.get("setup"), base_of(r.get("symbol") or ""))
        g = out.setdefault(k, {"open": 0, "R": [], "expired": 0, "flat": 0})
        if r.get("status") == "resolved":
            g["R"].append((r.get("outcome") or {}).get("R", 0.0))
        elif r.get("status") == "expired":
            g["expired"] += 1
        elif r.get("status") == "flat":
            g["flat"] += 1
        else:
            g["open"] += 1
    res = []
    for (kind, setup, coin), g in sorted(out.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2])):
        R = g["R"]
        res.append({"kind": kind, "setup": setup, "coin": coin, "resolved": len(R), "open": g["open"], "not_filled": g["expired"],
                    "stood_aside": g["flat"],
                    "avg_R": rnd(mean(R), 3) if R else None, "wins": sum(1 for x in R if x > 0), "total_R": rnd(sum(R), 2)})
    return res


def auto_resolve_tracks(net, home, now=None, limit=8):
    """Score tracked calls whose time stop has passed (at most `limit` per call, oldest first)."""
    now = now_ms() if now is None else now
    rows = journal_load(home)
    due = [r for r in rows if r.get("decision") == "TRACK" and r.get("status") in ("open", "filled")
           and now > r["ts"] + int((float(r.get("fill_window_hours") or 0) + float(r.get("horizon_hours") or 24)) * 3600000) + 600000]
    done = 0
    for r in sorted(due, key=lambda r: r["ts"])[:limit]:
        try:
            upd = resolve_trade(net, r, now)
        except DeskError:
            continue
        if upd:
            r.update(upd)
            done += 1
    if done:
        journal_save(home, rows)
    return done


def track_lines(rows):
    L = []
    for t in track_summary(rows):
        L.append("  %-11s %-20s %-5s resolved %3d (wins %d, avg %s, total %sR), open %d, not filled %d"
                 % (t["kind"], t["setup"], t["coin"], t["resolved"], t["wins"], ("%+.2fR" % t["avg_R"]) if t["avg_R"] is not None else "n/a",
                    t["total_R"], t["open"], t["not_filled"]))
    return L


def journal_stats(rows):
    trades = [r for r in rows if r.get("type") == "trade" and r.get("status") == "resolved" and r.get("decision") != "TRACK"]
    out = {"resolved_trades": len(trades)}
    if trades:
        Rs = [(r.get("outcome") or {}).get("R", 0.0) for r in trades]
        wins = [x for x in Rs if x > 0]
        losses = [x for x in Rs if x <= 0]
        mlo, mhi = boot_mean_ci(Rs)
        eq, peak, dd = 0.0, 0.0, 0.0
        for x in Rs:
            eq += x
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        out.update({"win_rate": rnd(len(wins) / float(len(Rs)), 3), "avg_win_r": rnd(mean(wins), 2), "avg_loss_r": rnd(mean(losses), 2),
                    "expectancy_r": rnd(mean(Rs), 3), "expectancy_ci90": [rnd(mlo, 3), rnd(mhi, 3)],
                    "total_r": rnd(sum(Rs), 2), "max_drawdown_r": rnd(dd, 2),
                    "profit_factor": rnd(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None})
        probs = [(float(r["prob"]), 1.0 if (r.get("outcome") or {}).get("result") == "target" else 0.0)
                 for r in trades if r.get("prob") is not None and (r.get("outcome") or {}).get("result") in ("target", "stop")]
        if probs:
            brier = mean([(p - y) ** 2 for p, y in probs])
            ybar = mean([y for _, y in probs])
            ref = ybar * (1 - ybar)
            out["brier_trades"] = rnd(brier, 4)
            out["brier_reference"] = rnd(ref, 4)
            out["brier_skill"] = rnd(1 - brier / ref, 3) if ref > 0 else None
            buckets = {}
            for p, y in probs:
                b = min(9, int(p * 10))
                buckets.setdefault(b, []).append((p, y))
            out["calibration"] = [{"bucket": "%d-%d%%" % (b * 10, b * 10 + 10), "n": len(v), "said": rnd(mean([p for p, _ in v]), 2),
                                   "happened": rnd(mean([y for _, y in v]), 2)} for b, v in sorted(buckets.items())]
        by = {}
        for r in trades:
            by.setdefault(r.get("setup") or "manual", []).append((r.get("outcome") or {}).get("R", 0.0))
        out["by_setup"] = {k: {"n": len(v), "expectancy_r": rnd(mean(v), 3)} for k, v in sorted(by.items())}
        byi = {}
        for r in trades:
            byi.setdefault(r.get("intensity") or "unknown", []).append(r)
        out["by_intensity"] = {}
        for k, v in sorted(byi.items()):
            pv = [(float(r["prob"]), 1.0 if (r.get("outcome") or {}).get("result") == "target" else 0.0) for r in v
                  if r.get("prob") is not None and (r.get("outcome") or {}).get("result") in ("target", "stop")]
            out["by_intensity"][k] = {"n": len(v), "expectancy_r": rnd(mean([(r.get("outcome") or {}).get("R", 0.0) for r in v]), 3),
                                      "brier": rnd(mean([(p - y) ** 2 for p, y in pv]), 4) if pv else None}
    fc = [r for r in rows if r.get("type") == "forecast" and r.get("status") == "resolved"]
    if fc:
        ups = [1.0 if (r.get("outcome") or {}).get("went_up") else 0.0 for r in fc]
        ub = mean(ups)
        out["forecasts"] = {"n": len(fc), "brier": rnd(mean([(r.get("outcome") or {}).get("brier", 0.25) for r in fc]), 4),
                            "brier_reference": rnd(ub * (1 - ub), 4),
                            "hit_rate": rnd(mean([1.0 if ((float(r["p_up"]) > 0.5) == (r.get("outcome") or {}).get("went_up")) else 0.0
                                                  for r in fc if float(r["p_up"]) != 0.5]), 3) if any(float(r["p_up"]) != 0.5 for r in fc) else None}
    return out


# ------------------------------------------------------------------ rules

CHART_SIGNALS_OFF = ("chart-setup signals are off (config chart_signals): replayed at 3,900 past moments on the five coins "
                     "(2020-2026), chart setups that passed their own history test gave 7 signals, 5 filled and only 1 made "
                     "money (average -0.70R), so a pass is shown as a watch item; 'config set chart_signals true' turns them "
                     "back on")


NEWS_FILE = "news_context.json"
NEWS_LEVELS = ("normal", "elevated", "extreme")


def _news_reasons(block):
    """The reasons that count: marked confirmed (an official source, or two independent reputable outlets) and with a link."""
    out = []
    for r in (block or {}).get("reasons") or []:
        if not isinstance(r, dict) or r.get("confirmed") is not True:
            continue
        srcs = [x for x in (r.get("sources") or []) if isinstance(x, str) and x.startswith(("https://", "http://"))]
        text = str(r.get("text") or "").strip()
        if srcs and text:
            out.append({"text": text[:300], "sources": srcs[:4], "confirmed": True})
    return out


def _news_time(v, what):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return parse_when(v, what)


def normalise_news(d, now=None):
    """Check a research brief (from Claude through the app, or 'news set FILE') and return the stored form.
    A level only counts when it has at least one confirmed reason with a link; everything else is 'normal'."""
    now = now_ms() if now is None else now
    if not isinstance(d, dict):
        raise DeskError("a news check is a JSON object")
    made = _news_time(d.get("made_at"), "made_at")
    if made > now + 5 * 60000:
        raise DeskError("the news check's made_at is in the future")
    vu = _news_time(d["valid_until"], "valid_until") if d.get("valid_until") is not None else made + 12 * 3600000
    vu = min(vu, made + 24 * 3600000)

    def lvl(b):
        b = b if isinstance(b, dict) else {}
        stated = str(b.get("level") or "normal").lower()
        if stated not in NEWS_LEVELS:
            raise DeskError("a news level is normal, elevated or extreme (got %r)" % stated[:20])
        reasons = _news_reasons(b)
        return {"level": stated if reasons else "normal", "stated_level": stated, "reasons": reasons}
    coins = {}
    for k, b in ((d.get("coins") or {}) if isinstance(d.get("coins"), dict) else {}).items():
        coins[norm_symbol(k)] = lvl(b)
    return {"v": 1, "made_at": made, "valid_until": vu, "source": str(d.get("source") or "research brief")[:120],
            "summary": str(d.get("summary") or "")[:600], "market": lvl(d.get("market")), "coins": coins}


def load_news_context(home, now=None):
    """The latest research news check, or None. A stale one (past valid_until, at most 24 hours) is returned marked
    stale and counts for nothing."""
    p = os.path.join(home, NEWS_FILE)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            d = normalise_news(json.load(fh), now)
    except (OSError, ValueError, DeskError):
        return None
    now = now_ms() if now is None else now
    return dict(d, stale=not (d["made_at"] - 5 * 60000 <= now <= d["valid_until"]))


def news_level(ctx, sym, now=None):
    """(level, reasons, ctx) for one coin: the worse of the market's and the coin's confirmed levels."""
    if not ctx or ctx.get("stale"):
        return "normal", [], ctx
    if now is not None and not (ctx.get("made_at", 0) - 5 * 60000 <= now <= ctx.get("valid_until", 0)):
        return "normal", [], dict(ctx, stale=True)                    # checked here too, whoever loaded it
    best, reasons = "normal", []
    for b in (ctx.get("market") or {}, (ctx.get("coins") or {}).get(norm_symbol(sym)) or {} if sym else {}):
        lv = b.get("level") or "normal"
        if lv in NEWS_LEVELS and NEWS_LEVELS.index(lv) > NEWS_LEVELS.index(best):
            best = lv
        if lv != "normal":
            reasons += b.get("reasons") or []
    return best, reasons, ctx


def run_checks(cfg, plan, snap, rows, macro=None, now=None, venue_price=None, sim=None, news=None):
    now = now_ms() if now is None else now
    blocks, warns = [], []
    is_ev = (plan.get("setup") or "") in EVENT_SETUPS
    manual = (snap or {}).get("source") == "manual"
    if sim and sim.get("verdict"):
        v = sim["verdict"]
        if v.startswith("NEGATIVE"):
            blocks.append("history test: " + v)
        elif is_ev and not v.startswith(("POSITIVE", "UNPROVEN", "MARKET DRIFT")):
            blocks.append("news setup without supporting history: " + v)
        elif not (v.startswith("POSITIVE") and ("Better than random" in v or "Better than the same rule" in v)):
            warns.append("history test: " + v)
        if (not is_ev and v.startswith("POSITIVE") and (plan.get("setup") or "") not in ("", "manual")
                and not cfg.get("chart_signals", False)):
            blocks.append(CHART_SIGNALS_OFF)
    if snap and not manual:
        if not sim:
            blocks.append("no history test for this plan: run 'simulate --run <plan folder>' first (it needs Binance data)")
        else:
            sp = sim.get("plan") or {}
            t1p = ((plan.get("targets") or [{}])[0]).get("price")

            def _same(a, b):
                try:
                    return abs(float(a) / float(b) - 1) < 1e-6
                except (TypeError, ValueError, ZeroDivisionError):
                    return False
            if not (sp.get("side") == plan.get("side") and _same(sp.get("entry"), plan.get("entry")) and _same(sp.get("stop"), plan.get("stop"))
                    and _same(sp.get("target"), t1p) and (sp.get("setup") or None) == (plan.get("setup") or None)):
                blocks.append("the history test was run on a different plan (side, entry, stop, first target or setup differ): re-run simulate")
    if is_ev and sim and sim.get("event_expires") and now > sim["event_expires"]:
        blocks.append("this news setup expired at %s: do not chase it" % fmt_time(sim["event_expires"], cfg["tz_offset_minutes"]))
    if not snap:
        blocks.append("no market snapshot for this plan: run 'snapshot SYMBOL --write' (or 'manual' in chart mode) first; "
                      "without it freshness, volatility, mid-range and macro rules cannot be checked")
    elif snap.get("symbol") and plan.get("symbol") and snap["symbol"] != plan["symbol"]:
        blocks.append("the snapshot is for %s but the plan is for %s" % (snap["symbol"], plan["symbol"]))
    side, entry, stop = plan["side"], float(plan["entry"]), float(plan["stop"])
    a = None
    tfs = (snap or {}).get("timeframes") or {}
    prim = tfs.get(cfg["primary_tf"]) or tfs.get("4h") or tfs.get("1h") or {}
    a = prim.get("atr")
    # A news setup keeps the first target it was tested with (1.5R before costs, about 1.2-1.35R after); its own
    # tested history, costs included, decides whether it is worth taking, so it has its own, lower floor. Only a plan
    # the history test matched to a live candidate of a confirmed release gets it; anything else uses the normal floor.
    ev_ok = bool(is_ev and sim and sim.get("event_match"))
    # A news rule that passed its own history test is traded exactly as tested. Its test did not use the chart-setup
    # filters below (stop width in ATR, mid-range, volatility regime); on the 41 SOL Fed nights the last two removed 7
    # trades that averaged +1.07R (data/gates_on_fed_SOL.json), and the ATR width blocked the tested 2.5% stop whenever the
    # 4h ATR was under 0.83% (quiet weeks), so for it they are warnings, not blocks. Risk limits still apply in full:
    # a wider stop only means a smaller position for the same loss at the stop.
    tested_news = ev_ok and (str((sim or {}).get("verdict") or "").startswith("POSITIVE")
                             or (plan.get("tier") == "small" and str((sim or {}).get("verdict") or "").startswith("UNPROVEN")))
    min_rr = float(cfg.get("event_min_net_rr", 1.0)) if ev_ok else float(cfg["min_net_rr"])
    if not stop:
        blocks.append("no stop: every trade needs a stop placed on the exchange at entry")
    if a:
        d = abs(entry - stop) / a
        msg = None
        if d < cfg["stop_atr_min"]:
            msg = "stop is %.2f ATR away: inside normal noise (min %.2f)" % (d, cfg["stop_atr_min"])
        if d > cfg["stop_atr_max"]:
            msg = "stop is %.2f ATR away: too wide for a day trade (max %.2f)" % (d, cfg["stop_atr_max"])
        if msg and tested_news:
            warns.append(msg + "; not applied to this news rule, whose stop comes from the release hour as tested (the size "
                         "shrinks to keep the same loss at the stop)")
        elif msg:
            blocks.append(msg)
    if plan.get("max_loss_pct_equity") is not None and plan["max_loss_pct_equity"] > cfg["risk_per_trade_pct"] * 1.05 + 1e-9:
        blocks.append("max loss %.2f%% of equity is over your %.2f%% per trade" % (plan["max_loss_pct_equity"], cfg["risk_per_trade_pct"]))
    if plan.get("leverage", 1) > cfg["max_leverage"]:
        blocks.append("leverage %sx is over your cap %sx" % (plan["leverage"], cfg["max_leverage"]))
    if plan.get("liq_to_stop_ratio") is not None and plan["liq_to_stop_ratio"] < cfg["liq_buffer_mult"]:
        blocks.append("liquidation is only %.1fx the stop distance away (need %.1fx)" % (plan["liq_to_stop_ratio"], cfg["liq_buffer_mult"]))
    t1 = (plan.get("targets") or [{}])[0]
    if is_ev and manual:
        blocks.append("news setups need the engine's 1-minute release read and its tested history; chart mode cannot check them")
    if t1.get("net_r") is None:
        blocks.append("no target: a plan needs at least one target")
    elif t1["net_r"] < min_rr:
        blocks.append("first target pays %.2fR after costs (min %.2fR%s)" % (t1["net_r"], min_rr, " for news setups" if ev_ok else ""))
    for n in plan.get("notes") or []:
        if "below the exchange minimum" in n:
            blocks.append(n)
    if snap:
        age = (now - snap.get("as_of", now)) / 60000.0
        if age > 15:
            blocks.append("market data is %.0f minutes old: run the snapshot again" % age)
        cur = snap.get("price")
        et = plan.get("entry_type", "market")
        if cur and a:
            drift = (cur - entry) / a if side == "long" else (entry - cur) / a
            if drift > cfg["chase_atr_max"]:
                if et == "limit":
                    warns.append("price is %.2f ATR away from your limit: it may not fill (a resting order, not a chase)" % drift)
                else:
                    blocks.append("price already ran %.2f ATR past the entry: chasing" % drift)
            if (side == "long" and cur <= stop) or (side == "short" and cur >= stop):
                blocks.append("price is already beyond the stop")
        dr = (prim.get("structure") or {}).get("dealing_range") or {}
        pos = None
        if dr.get("high") is not None and dr.get("low") is not None and dr["high"] > dr["low"]:
            pos = (entry - dr["low"]) / (dr["high"] - dr["low"])
        elif prim.get("range48_high") is not None and prim.get("range48_low") is not None and prim["range48_high"] > prim["range48_low"]:
            pos = (entry - prim["range48_low"]) / (prim["range48_high"] - prim["range48_low"])
        else:
            pos = dr.get("pos") if dr.get("pos") is not None else prim.get("range48_pos")
        band = cfg["mid_range_band"]
        if pos is not None and band[0] <= pos <= band[1]:
            msg = "mid-range: entry sits at %.0f%% of the dealing range (your rule: no mid-range entries)" % (pos * 100)
            if tested_news:
                warns.append(msg + "; not applied to this news rule, which was tested without that filter")
            else:
                blocks.append(msg)
        if prim.get("regime") == "HIGH_VOL" and cfg.get("block_high_vol_regime", True):
            if tested_news:
                warns.append("extreme volatility regime: not applied to this news rule (its stop is sized from the release hour "
                             "and its test included volatile nights)")
            else:
                blocks.append("extreme volatility regime: no new entries")
        if prim.get("regime") == "TRANSITION":
            warns.append("regime is unclear (transition): lower conviction")
        htf_name = cfg.get("higher_tf", "1d")
        htf = (tfs.get(htf_name) or {}).get("trend")
        if htf and ((side == "long" and htf == "down") or (side == "short" and htf == "up")):
            warns.append("trade is against the %s trend (%s)" % (htf_name, htf))
        dv = snap.get("derivatives") or {}
        fr = dv.get("funding")
        if fr is not None and plan.get("hold_hours"):
            ih = dv.get("funding_interval_h") or 8.0
            cost_r = (fr * (24.0 / ih)) * entry / abs(entry - stop) * (1 if side == "long" else -1)
            if cost_r > 0.1:
                warns.append("funding costs about %.2fR per day against this position" % cost_r)
        depth = dv.get("bid_depth_usd_0.005") if side == "short" else dv.get("ask_depth_usd_0.005")
        if depth and plan.get("notional") and depth < 10 * plan["notional"]:
            warns.append("thin book: visible depth within 0.5% is under 10x your order")
    if macro is None and snap and not snap.get("macro_ok"):
        blocks.append("macro calendar unknown (the feed failed or was not checked): check a calendar such as "
                      "forexfactory.com, then re-run the snapshot or pass the events in chart mode")
    elif macro is None and snap and snap.get("macro_ok") == "official" and plan.get("setup") not in EVENT_SETUPS:
        blocks.append("macro calendar partly unknown: the live feed failed, so only the official US schedule (Fed, CPI, PPI, jobs, "
                      "GDP, PCE) is known and a second-tier or non-US release could fall inside this trade; news-rule trades on "
                      "those releases still work (they were tested against that schedule). Re-run when the feed answers")
    events = macro if macro is not None else (snap or {}).get("macro") or []
    events = [dict(e, minutes_from_now=int(round((e["t"] - now) / 60000.0))) for e in events if "t" in e]
    bo = macro_blackout(events, cfg)
    if bo:
        blocks.append("macro blackout: %s" % "; ".join("%s (%+d min)" % (e["title"], e["minutes_from_now"]) for e in bo))
    horizon_min = 60.0 * float(plan.get("horizon_hours") or plan.get("hold_hours") or 8)
    soon = [e for e in events if e.get("impact") == "High" and e.get("country") in ("USD", "ALL")
            and cfg["macro_blackout_minutes_before"] < e["minutes_from_now"] <= horizon_min]
    if soon:
        first = min(e["minutes_from_now"] for e in soon)
        blocks.append("high-impact event inside the holding window: %s. Your rule is flat before it: re-plan with "
                      "--horizon-hours %.1f or less (the trade must be closed before it), or wait until after it"
                      % ("; ".join("%s in %d min" % (e["title"], e["minutes_from_now"]) for e in soon[:3]),
                         max(0.0, (first - cfg["macro_blackout_minutes_before"]) / 60.0)))
    nl, nr, nctx = news_level(news, plan.get("symbol"), now)
    news_out = {"level": nl, "reasons": nr[:3], "made_at": (nctx or {}).get("made_at"), "stale": bool((nctx or {}).get("stale"))} if nctx else None
    if nl != "normal":
        when = fmt_time(nctx["made_at"], cfg["tz_offset_minutes"])
        why = "; ".join(r["text"] for r in nr[:3])
        if nl == "extreme":
            blocks.append("news check (research at %s): %s. No new entries until it clears (news can block or shrink a "
                          "trade, never create one)" % (when, why))
        elif float(plan.get("news_multiplier") or 1.0) < 1.0:
            warns.append("news check (research at %s): %s. Elevated risk: sized at half" % (when, why))
        else:
            warns.append("news check (research at %s): %s. Elevated risk: this plan is not at half size, re-plan it" % (when, why))
    setup_name = plan.get("setup") or "manual"
    mine = [(r.get("outcome") or {}).get("R", 0.0) for r in rows if r.get("type") == "trade" and r.get("status") == "resolved"
            and (r.get("setup") or "manual") == setup_name and r.get("decision") in ("TAKE", "PAPER")]
    if len(mine) >= 10 and mean(mine) < 0:
        blocks.append("your record: %s averages %+.2fR over %d trades, so it is suspended until you review it"
                      % (setup_name, mean(mine), len(mine)))
    live = [(r.get("outcome") or {}).get("R", 0.0) for r in rows if r.get("type") == "trade" and r.get("status") == "resolved"
            and r.get("decision") == "TRACK" and r.get("track_kind") == "signal" and r.get("setup") == setup_name
            and r.get("symbol") == plan.get("symbol")]
    if len(live) >= 10 and mean(live[-30:]) < 0:
        blocks.append("live record: the engine's own %s signals on %s average %+.2fR over the last %d tracked (below zero), so the "
                      "rule is suspended until you review it ('journal track')" % (setup_name, base_of(plan.get("symbol") or ""),
                                                                                  mean(live[-30:]), len(live[-30:])))
    J = journal_today(rows, cfg, now)
    eq = float(cfg["equity"])
    limit = eq * cfg["max_daily_loss_pct"] / 100.0
    realized = J["realized_pnl_today"] or 0.0
    if realized <= -limit:
        blocks.append("daily loss limit hit (%.2f): done for today" % realized)
    else:
        worst = max(0.0, -realized) + (J.get("open_risk") or 0.0) + float(plan.get("max_loss_at_stop") or 0.0)
        if worst > limit * 1.0001:
            blocks.append("if this trade and your open trades all hit their stops, today's loss would be %.2f, over your "
                          "daily limit of %.2f" % (worst, limit))
    if J["trades_today"] >= cfg["max_trades_per_day"]:
        blocks.append("%d trades already today (max %d)" % (J["trades_today"], cfg["max_trades_per_day"]))
    if len(J["open"]) >= cfg["max_open_positions"]:
        blocks.append("%d positions already open (max %d)" % (len(J["open"]), cfg["max_open_positions"]))
    if J["consecutive_losses"] >= cfg["cooldown_after_consecutive_losses"] and J["last_resolved_day"] == J["today"]:
        blocks.append("%d losses in a row today: cooling off until tomorrow" % J["consecutive_losses"])
    same_dir = [r for r in J["open"] if r.get("side") == side]
    if same_dir:
        warns.append("you already hold %d %s position(s); crypto moves together, so this adds to the same bet"
                     % (len(same_dir), side))
    if venue_price:
        ref = (snap or {}).get("price") or entry
        diff = abs(venue_price / ref - 1)
        if diff > 0.003:
            warns.append("venue price differs from the Binance reference by %.2f%%: re-anchor entry, stop and targets" % (diff * 100))
    prob = probation_status(rows, cfg)
    mode = cfg.get("mode", "paper")
    if prob["status"] != "PROVEN":
        warns.append("edge unproven (%s): signals are sized at %.0f%% of your normal risk until your record proves an edge"
                      % (prob["why"], 100 * float(cfg.get("unproven_risk_multiplier", 0.5))))
    warns.extend(shock_warnings(snap, side))
    wd = utc(now).weekday()
    if wd >= 5 or (wd == 4 and utc(now).hour >= 21):
        warns.append("weekend: thinner order books and sharper wicks; consider a smaller size or skipping")
    verdict = "BLOCK" if blocks else "PASS"
    return {"verdict": verdict, "blocks": blocks, "warnings": warns, "probation": prob, "today": {k: v for k, v in J.items() if k != "open"},
            "open_positions": len(J["open"]), "checked_at": now, "news_check": news_out}


# ------------------------------------------------------------------ commands

def _print(lines):
    out = "\n".join(lines)
    try:
        sys.stdout.write(out + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(out.encode("ascii", "replace").decode("ascii") + "\n")


def load_market(net, sym, tfs=("15m", "1h", "4h", "1d"), counts=None, source="binance"):
    counts = counts or {"5m": 1000, "15m": 1000, "1h": 1000, "2h": 800, "4h": 600, "1d": 400}
    out, errors = {}, []
    for tf in tfs:
        try:
            out[tf] = get_klines(net, sym, tf, counts.get(tf, 500), source=source)
        except DeskError as e:
            errors.append("%s klines: %s" % (tf, e))
    return out, errors


def cmd_doctor(args, home, cfg):
    lines = ["crypto-futures-desk engine %s" % VERSION,
             "Python %s on %s" % (sys.version.split()[0], sys.platform),
             "Home folder: %s (writable)" % home]
    if getattr(args, "synthetic", False) or getattr(args, "offline", None):
        lines.append("Synthetic or offline mode: no network checks (run doctor without --synthetic/--offline to test the real sources)")
        st = load_event_stats()
        lines.append("Event history: %s" % ("found (%s)" % st.get("period") if st else "MISSING"))
        lines.append(release_calendar_line())
        _print(lines)
        return 0
    net = Net(home=home, cache=False, pause=0.05)
    checks = [("Binance futures (prices, funding, OI)", bn_url("/fapi/v1/time")),
              ("Bybit (backup prices)", BYBIT + "/v5/market/time"),
              ("alternative.me (fear & greed)", FNG_URL),
              ("Deribit (implied volatility)", "https://www.deribit.com/api/v2/public/get_time"),
              ("ForexFactory feed (macro calendar)", FF_CALENDAR),
              ("CoinGecko (dominance)", "https://api.coingecko.com/api/v3/ping"),
              ("CoinDesk RSS (headlines)", RSS_FEEDS[0][1])]
    ok = 0
    status = {}
    skew = None
    for name, url in checks:
        try:
            body = net.text(url, ttl=0, timeout=8)
            lines.append("  OK    %s" % name)
            ok += 1
            status[name] = "ok"
            if url.endswith("/fapi/v1/time"):
                try:
                    skew = (now_ms() - int(json.loads(body)["serverTime"])) / 1000.0
                except (ValueError, KeyError, TypeError):
                    pass
        except DeskError as e:
            lines.append("  FAIL  %s: %s" % (name, e))
            status[name] = str(e)
    if skew is not None:
        lines.append(("Clock: %+.0f s versus Binance. Fix your computer's clock (news timing and data-age rules depend on it)."
                      if abs(skew) > 60 else "Clock: within %.0f s of Binance") % (skew if abs(skew) > 60 else abs(skew)))
    full = status.get(checks[0][0]) == "ok"
    partial = (not full) and status.get(checks[1][0]) == "ok"
    lines.append("Mode available: %s" % ("FULL (live market data)" if full else
                                         "PARTIAL (Bybit prices only: snapshot --source bybit works; history tests and journal scoring need Binance)" if partial else
                                         "CHART-ONLY (no exchange data here: use screenshots with the DESK table, or run on your computer)"))
    lines.append("Config: %s" % ("found" if os.path.exists(os.path.join(home, "config.json")) else "defaults (run 'init' to create one)"))
    st = load_event_stats()
    lines.append("Event history: %s" % (("found (%s, %s)" % (st.get("period"), event_stats_path())) if st else
                                        "MISSING: put references/event_stats.json next to the scripts folder; news setups will show as untested"))
    lines.append(release_calendar_line())
    if not full:
        lines.append("No live data here? Run this engine on your own computer: 'python desk.py brief BTC', then paste its output "
                     "(or the packet.txt it saves) into a Claude chat with the skill.")
    if args.json:
        print(json.dumps({"ok": ok, "status": status, "full_mode": full, "partial_mode": partial, "home": home,
                          "python": sys.version.split()[0]}, indent=1))
    else:
        _print(lines)
    return 0 if full else 2


def cmd_init(args, home, cfg):
    p = os.path.join(home, "config.json")
    if os.path.exists(p) and not args.force:
        _print(["config already exists: %s (use --force to reset)" % p])
        return 0
    c = json.loads(json.dumps(DEFAULTS))
    for k in ("equity", "risk_per_trade_pct", "max_leverage"):
        v = getattr(args, k, None)
        if v is not None:
            c[k] = v
    write_json(p, c)
    _print(["wrote %s" % p, "Edit equity, risk_per_trade_pct, max_leverage, fees (your venue's), watchlist."])
    return 0


CONFIG_EXTRA_KEYS = {"display_currency": str}
TF_CHOICES = ("1h", "2h", "4h", "6h", "12h", "1d")
# (kind, low, high): every number has bounds, so a setting can never switch a safety rule off.
CONFIG_SCHEMA = {
    "equity": ("num", 1e-6, 1e12), "equity_currency": ("cur",), "usdt_inr": ("num_or_null", 1e-6, 1e7),
    "usdt_fx": ("num_or_null", 1e-6, 1e7), "display_currency": ("cur_or_empty",), "tz": ("tz",),
    "tz_offset_minutes": ("int", -720, 840), "venue": ("text",), "venue_symbols": ("symbols",), "watchlist": ("symbols",),
    "risk_per_trade_pct": ("num", 0.05, 2.0), "max_leverage": ("int", 1, 20), "max_margin_per_trade_pct": ("num", 1, 100),
    "liq_buffer_mult": ("num", 1.5, 10), "min_liq_atr_beyond_stop": ("num", 0.5, 10), "max_daily_loss_pct": ("num", 0.25, 10),
    "max_trades_per_day": ("int", 1, 20), "max_open_positions": ("int", 1, 10), "cooldown_after_consecutive_losses": ("int", 1, 10),
    "min_net_rr": ("num", 1.0, 5.0), "fees": ("map", 0.0, 0.005, ("taker", "maker")), "gst_on_fees_pct": ("num", 0, 30),
    "tp_order": ("choice", ("limit", "market")), "slippage": ("map", 0.0, 0.02, None),
    "maintenance_margin_rate": ("map", 0.001, 0.2, None), "liquidation_fee": ("num", 0, 0.05),
    "macro_blackout_minutes_before": ("int", 30, 240), "macro_blackout_minutes_after": ("int", 30, 240),
    "mid_range_band": ("band",), "stop_atr_min": ("num", 0.1, 1.0), "stop_atr_max": ("num", 1.0, 6.0), "chase_atr_max": ("num", 0.1, 2.0),
    "block_high_vol_regime": ("bool",), "chart_signals": ("bool",), "small_signals": ("bool",), "small_signal_risk": ("num", 0.05, 0.5),
    "auto_track": ("bool",), "primary_tf": ("choice", TF_CHOICES), "higher_tf": ("choice", TF_CHOICES),
    "default_entry_type": ("choice", ("limit", "market")), "scan_min_quote_volume": ("num", 0, 1e12), "scan_top": ("int", 5, 100),
    "mode": ("choice", ("paper", "live")), "probation_min_trades": ("int", 30, 1000), "unproven_risk_multiplier": ("num", 0.1, 1.0),
    "event_slippage": ("num", 0.0, 0.02), "event_min_stop_pct": ("map", 0.5, 10.0, None), "event_min_net_rr": ("num", 0.8, 3.0),
}


def check_setting(key, val, user):
    """Validate one setting; returns the value to store or raises DeskError with a plain reason."""
    spec = CONFIG_SCHEMA.get(key)
    if spec is None:
        raise DeskError("unknown setting '%s'. Known: %s" % (key, ", ".join(sorted(CONFIG_SCHEMA))))
    kind = spec[0]

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if kind in ("num", "int", "num_or_null"):
        if val is None and kind == "num_or_null":
            return None
        if not num(val):
            raise DeskError("%s takes a number" % key)
        if kind == "int":
            if int(val) != val:
                raise DeskError("%s takes a whole number" % key)
            val = int(val)
        if not spec[1] <= val <= spec[2]:
            raise DeskError("%s must be between %s and %s" % (key, spec[1], spec[2]))
        return val
    if kind == "bool":
        if not isinstance(val, bool):
            raise DeskError("%s takes true or false" % key)
        return val
    if kind == "choice":
        if val not in spec[1]:
            raise DeskError("%s must be one of %s" % (key, ", ".join(spec[1])))
        if key in ("primary_tf", "higher_tf"):
            other = user.get("higher_tf" if key == "primary_tf" else "primary_tf") or DEFAULTS["higher_tf" if key == "primary_tf" else "primary_tf"]
            p_, h_ = (val, other) if key == "primary_tf" else (other, val)
            if TF_MIN[p_] >= TF_MIN[h_]:
                raise DeskError("primary_tf must be shorter than higher_tf")
        return val
    if kind in ("cur", "cur_or_empty"):
        v = str(val or "").strip().upper()
        if kind == "cur_or_empty" and v == "":
            return None
        if not re.match(r"^[A-Z]{3,4}$", v):
            raise DeskError("%s takes a currency code such as USDT or INR" % key)
        return v
    if kind == "tz":
        if val in (None, "", "null"):
            return None
        if get_zone(val) is None:
            raise DeskError("time zone %r is unknown here (Python 3.9+ needed, and on Windows 'pip install tzdata'); "
                            "or set tz_offset_minutes instead (IST = 330)" % val)
        return str(val)
    if kind == "text":
        v = str(val)
        if not re.match(r"^[A-Za-z0-9 .,()&+\-]{1,40}$", v):
            raise DeskError("%s takes a short name (letters, digits, spaces)" % key)
        return v
    if kind == "symbols":
        if not isinstance(val, list) or not all(isinstance(x, str) and re.match(r"^[A-Za-z0-9]{2,20}$", x) for x in val):
            raise DeskError('%s takes a list of coin names like ["BTC","ETH"]' % key)
        if key == "watchlist" and not 1 <= len(val) <= 12:
            raise DeskError("watchlist takes 1 to 12 coins (each one is a full engine check)")
        return [x.upper() for x in val]
    if kind == "band":
        if not (isinstance(val, list) and len(val) == 2 and all(num(x) for x in val) and 0 < val[0] < val[1] < 1 and val[1] - val[0] >= 0.1):
            raise DeskError("%s takes two numbers like [0.4, 0.6] (low < high, at least 0.1 apart)" % key)
        return val
    if kind == "map":
        if not isinstance(val, dict) or not val:
            raise DeskError("%s takes a JSON object like %s" % (key, json.dumps(DEFAULTS.get(key) or {"BTC": 1.5})))
        allowed = spec[3]
        for k2, v2 in val.items():
            if allowed is not None and k2 not in allowed:
                raise DeskError("%s only has %s" % (key, ", ".join(allowed)))
            if allowed is None and not re.match(r"^[A-Za-z0-9]{2,20}$", str(k2)):
                raise DeskError("%s keys are coin names or 'default'" % key)
            if not num(v2) or not spec[1] <= v2 <= spec[2]:
                raise DeskError("%s.%s must be a number between %s and %s" % (key, k2, spec[1], spec[2]))
        merged = json.loads(json.dumps(DEFAULTS.get(key) or {}))
        deep_merge(merged, user.get(key) or {})
        deep_merge(merged, val)
        return merged
    raise DeskError("cannot set %s" % key)


def cmd_config(args, home, cfg):
    """Show or change one setting with type and range checks, so setup never needs hand-edited JSON and no
    setting can switch a safety rule off."""
    p = os.path.join(home, "config.json")
    user = {}
    if os.path.exists(p):
        user = read_json(p, "config")
    if args.ccmd in (None, "show"):
        try:
            eff = load_config(home)
        except DeskError as e:
            print("config not usable yet: %s" % e)
            print(json.dumps(user, indent=1))
            return 1
        print(json.dumps({k: eff[k] for k in sorted(eff)}, indent=1))
        return 0
    key, raw = args.key, args.value
    try:
        val = json.loads(raw)
    except ValueError:
        val = raw
    val = check_setting(key, val, user)
    user[key] = val
    os.makedirs(home, exist_ok=True)
    write_json(p, user)
    msg = ["set %s = %s in %s" % (key, json.dumps(val), p)]
    try:
        load_config(home)
    except DeskError as e:
        msg.append("saved, but the config is not usable yet: %s" % e)
    _print(msg)
    return 0


def build_snapshot(net, sym, cfg, source="binance", with_derivs=True, with_macro=True):
    now = now_ms()
    raw, errors = load_market(net, sym, ("15m", "1h", "4h", "1d"), source=source)
    if cfg.get("primary_tf", "4h") not in raw or cfg.get("higher_tf", "1d") not in raw:
        raise DeskError("could not load enough price history for %s: %s" % (sym, "; ".join(errors)))
    closed, feats, sums = {}, {}, {}
    live_price = None
    for tf, bars in raw.items():
        cb, live = split_closed(bars, now)
        if len(cb) < 60:
            errors.append("%s: only %d closed bars" % (tf, len(cb)))
            continue
        closed[tf] = cb
        feats[tf] = tf_features(cb)
        sums[tf] = tf_summary(feats[tf], cb, tf)
        if live and tf == "15m":
            live_price = live["c"]
    deriv = {}
    if with_derivs and source == "binance":
        deriv = derivatives_block(net, sym, live_price or closed["1h"][-1]["c"])
        errors.extend(deriv.pop("errors", []))
    elif source == "bybit":
        try:
            bt = get_bybit_ticker(net, sym)
            deriv = {"mark": bt["mark"], "index": bt["index"], "funding": bt["funding"], "next_funding": bt["next_funding"],
                     "open_interest": bt["oi"], "source": "bybit"}
        except DeskError as e:
            errors.append("bybit ticker: %s" % e)
    price = deriv.get("mark") or live_price or closed["1h"][-1]["c"]
    levels = key_levels(raw.get("1h", []), raw.get("1d", []), now) if "1d" in raw else {}
    flat = flatten_levels(levels, sums)
    above, below = nearest_levels(flat, price)
    macro = []
    macro_ok = False
    if with_macro:
        try:
            macro = get_macro(net, now)
            macro_ok = True
            if getattr(macro, "feed_error", None):
                # Only the official schedule is known. News rules were tested against exactly those releases, so they may
                # still trade; other setups wait for the live feed (a second-tier release could be inside the trade).
                macro_ok = "official"
                errors.append("live calendar: %s; %s" % (macro.feed_error, OFFICIAL_ONLY))
        except DeskError as e:
            errors.append("macro calendar: %s (the rules check will block until it is known)" % e)
    hits = macro_blackout(macro, cfg)
    prim, htf = cfg.get("primary_tf", "4h"), cfg.get("higher_tf", "1d")
    setups = []
    setups_checked = prim in feats and htf in feats
    if setups_checked:
        setups = detect_setups(feats[prim], closed[prim], feats[htf], closed[htf], deriv=deriv, levels=levels, cfg=cfg, macro_hits=hits, sym=sym)
    else:
        errors.append("too little %s/%s price history to look for setups (need 60 closed bars of each)" % (prim, htf))
    if "1h" in closed and deriv.get("oi_change_4h_pct") is not None:
        c1 = closed["1h"]
        deriv["oi_price_read_4h"] = oi_price_read((c1[-1]["c"] / c1[-5]["c"] - 1) * 100, deriv["oi_change_4h_pct"])
    if "1h" in closed and deriv.get("oi_change_24h_pct") is not None and len(closed["1h"]) > 25:
        c1 = closed["1h"]
        deriv["oi_price_read_24h"] = oi_price_read((c1[-1]["c"] / c1[-25]["c"] - 1) * 100, deriv["oi_change_24h_pct"])
    snap = {"created_by": "desk.py snapshot", "version": VERSION, "symbol": sym, "source": source, "as_of": now,
            "price": price, "timeframes": sums, "levels": levels, "recent_moves": recent_moves(closed.get("15m"), closed.get("1d")),
            "nearest_above": above, "nearest_below": below, "derivatives": {k: v for k, v in deriv.items() if k not in ("book", "funding_history")},
            "macro": macro, "macro_ok": macro_ok, "macro_blackout_now": hits, "setups": setups, "setups_checked": setups_checked,
            "errors": errors,
            "note": "Levels and indicators are from %s USD-M perpetual prices. Your venue's prices can differ slightly." % source.title()}
    return snap, feats, closed, deriv


def recent_moves(c15, c1d):
    """Recent shocks that change the odds (BTC history in references/event_stats.json): the worst and best
    rolling one-hour move in the last 4 hours (15-minute closes) and the worst daily close in the last 7 days."""
    out = {}
    if c15 and len(c15) >= 21:
        r = [c15[k]["c"] / c15[k - 4]["c"] - 1 for k in range(len(c15) - 16, len(c15)) if c15[k - 4]["c"] > 0]
        if r:
            out["worst_1h_pct_last_4h"] = rnd(min(r) * 100, 2)
            out["best_1h_pct_last_4h"] = rnd(max(r) * 100, 2)
    dr = [(c1d[k]["c"] / c1d[k - 1]["c"] - 1, k) for k in range(len(c1d) - 7, len(c1d)) if c1d[k - 1]["c"] > 0] if c1d and len(c1d) >= 9 else []
    if dr:
        w, k = min(dr)
        out["worst_day_pct_last_7d"] = rnd(w * 100, 2)
        out["days_since_worst_day"] = len(c1d) - 1 - k
    return out


def shock_warnings(snap, side=None, stats=None):
    """Plain warnings for trading right after a crash hour or a crash day (side None: one neutral line each)."""
    rm = (snap or {}).get("recent_moves") or {}
    stats = stats if stats is not None else load_event_stats()
    oc, dc = stats.get("one_hour_crashes") or {}, stats.get("dead_cat_bounce") or {}
    out = []
    w1 = rm.get("worst_1h_pct_last_4h")
    if w1 is not None and w1 <= -4.0:
        up4 = (oc.get("up_after_4h") or [None])[0]
        ll4 = (oc.get("lower_low_within_4h") or [None])[0]
        n = oc.get("drops_ge_4pct_in_1h")
        hist = (" (BTC 2018-2026, %s drops: %.0f%% were higher 4 hours later, but %.0f%% made a lower low within 4 hours first)"
                % (n, up4 * 100, ll4 * 100)) if up4 is not None and ll4 is not None else ""
        if side is None:
            out.append("a %.1f%% one-hour drop in the last 4 hours%s: longs risk a lower low, shorts risk the bounce" % (w1, hist))
        elif side == "long":
            out.append("a %.1f%% one-hour drop in the last 4 hours%s: a long's stop is likely to be tested; wait for a base or size down" % (w1, hist))
        else:
            out.append("shorting after a %.1f%% one-hour drop%s: you are selling into a likely bounce" % (w1, hist))
    wd = rm.get("worst_day_pct_last_7d")
    if wd is not None and wd <= -7.0:
        b3 = dc.get("bounce_3d_median_pct")
        nl = (dc.get("new_low_within_7d") or [None])[0]
        bt = (dc.get("buy_the_crash_trade") or {}).get("avg_R")
        hist = ((" (BTC 2018-2026, %s crash days: median 3-day bounce +%.1f%%, %.0f%% made a new low within 7 days%s)"
                 % (dc.get("crash_days_le_-7pct"), b3, nl * 100, (", buying the crash close averaged %+.2fR" % bt) if bt is not None else ""))
                if b3 is not None and nl is not None else "")
        if side is None:
            out.append("a %.1f%% crash day %d day(s) ago%s: bounces are common but usually fail" % (wd, rm.get("days_since_worst_day", 0), hist))
        elif side == "long":
            out.append("dead-cat zone: a %.1f%% day %d day(s) ago%s" % (wd, rm.get("days_since_worst_day", 0), hist))
        else:
            out.append("after a %.1f%% crash day %d day(s) ago, sharp relief bounces are common%s: a short entered low can be squeezed"
                       % (wd, rm.get("days_since_worst_day", 0), hist))
    return out


def snapshot_text(snap, cfg):
    tz = cfg["tz_offset_minutes"]
    s = snap
    L = ["%s  %s  price %s  (%s data, %s)" % (s["symbol"], fmt_time(s["as_of"], tz), fp(s["price"]), s["source"], VERSION)]
    for tf in ("4h", "1h", "15m", "1d"):
        t = s["timeframes"].get(tf)
        if not t:
            continue
        st = t["structure"]
        brk = st.get("last_break")
        L.append("  %-3s %-10s trend %-4s ADX %5s RSI %5s ATR %s (%s%%, rank %s) range-pos %s squeeze %s%s vwap-z %s%s"
                 % (tf, t["regime"], t["trend"], t["adx"], t["rsi"], fp(t["atr"]), t["atr_pct"], t["atr_rank"],
                    t["range48_pos"], "ON" if t["squeeze_on"] else "off",
                    ("(%d bars)" % t["squeeze_bars"]) if t["squeeze_on"] else "", t.get("vwap_z"),
                    ("  structure %s, last %s @ %s" % (st["trend"], brk["type"], fp(brk["level"]))) if brk else ("  structure %s" % st["trend"])))
        if t.get("cvd_divergence"):
            L.append("      CVD: %s" % t["cvd_divergence"])
    d = s.get("derivatives") or {}
    if d:
        L.append("  derivatives: funding %s per %sh (z %s, %s%%/yr), basis %s%%, OI 4h %s%% / 24h %s%%"
                 % (pct(d.get("funding"), 4), d.get("funding_interval_h", "?"), d.get("funding_z"), d.get("funding_annualized_pct"),
                    d.get("basis_pct"), d.get("oi_change_4h_pct"), d.get("oi_change_24h_pct")))
        if d.get("oi_price_read_4h"):
            L.append("      4h read: %s" % d["oi_price_read_4h"])
        for k, lbl in (("top_trader_long_short", "top traders L/S"), ("global_long_short", "all accounts L/S"), ("taker_buy_sell", "taker buy/sell")):
            if d.get(k):
                L.append("      %s %s (48h avg %s, rank %s)" % (lbl, d[k]["now"], d[k]["avg_48h"], d[k]["pct_rank_48h"]))
        if d.get("spread_bps") is not None:
            L.append("      book: spread %s bps, depth within 0.5%%: bids %s / asks %s USDT"
                     % (d["spread_bps"], d.get("bid_depth_usd_0.005"), d.get("ask_depth_usd_0.005")))
    lv = s.get("levels") or {}
    L.append("  levels above: " + ", ".join("%s %s (%+.2f%%)" % (x["name"], fp(x["price"]), x["dist_pct"]) for x in s["nearest_above"]))
    L.append("  levels below: " + ", ".join("%s %s (%+.2f%%)" % (x["name"], fp(x["price"]), x["dist_pct"]) for x in s["nearest_below"]))
    if lv.get("killzone_now") or lv.get("session_now"):
        L.append("  session now: %s%s" % ("/".join(lv.get("session_now") or []) or "off-hours",
                                          (", killzone " + lv["killzone_now"]) if lv.get("killzone_now") else ""))
    mac = [e for e in s.get("macro") or [] if e.get("impact") == "High" and e["minutes_from_now"] >= -60][:5]
    if mac:
        L.append("  macro (high impact): " + "; ".join("%s %s in %s min" % (e["country"], e["title"], e["minutes_from_now"]) for e in mac))
    if s.get("macro_blackout_now"):
        L.append("  MACRO BLACKOUT NOW: no new entries")
    rm = s.get("recent_moves") or {}
    if rm:
        L.append("  recent moves: worst 1h in last 4h %s%%, best %s%%; worst day in last 7d %s%%"
                 % (rm.get("worst_1h_pct_last_4h"), rm.get("best_1h_pct_last_4h"), rm.get("worst_day_pct_last_7d")))
    if s["setups"]:
        L.append("  candidates (rule matches, not recommendations):")
        for c in s["setups"][:5]:
            L.append("    %-15s %-5s score %3d  entry %s stop %s T1 %s T2 %s (%.2fR to T1, stop %.2f ATR)%s"
                     % (c["setup"], c["side"], c["score"], fp(c["entry"]), fp(c["stop"]), fp(c["target1"]), fp(c["target2"]),
                        c["r_to_t1"], c["risk_atr"], "" if c["tradeable"] else "  BLOCKED: " + "; ".join(c["gates"])))
    else:
        L.append("  candidates: none of the rule-based setups match right now (that is a valid answer)")
    if s["errors"]:
        L.append("  data gaps: " + "; ".join(s["errors"][:6]))
    return L


def cmd_snapshot(args, home, cfg):
    sym = norm_symbol(args.symbol)
    net = make_net(home, args)
    try:
        snap, feats, closed, deriv = build_snapshot(net, sym, cfg, source=args.source)
    except DeskError as e:
        if args.source == "binance" and not args.offline:
            snap, feats, closed, deriv = build_snapshot(net, sym, cfg, source="bybit")
            snap["errors"].insert(0, "Binance failed (%s); fell back to Bybit (no taker flow, fewer derivatives)" % e)
        else:
            raise
    if args.write:
        rd = new_run_dir(home, sym)
        write_json(os.path.join(rd, "snapshot.json"), snap)
        snap["run_dir"] = rd
    if args.json:
        print(json.dumps(snap, indent=1))
    else:
        _print(snapshot_text(snap, cfg) + (["  saved: %s" % snap["run_dir"]] if snap.get("run_dir") else []))
    return 0


def cmd_context(args, home, cfg):
    net = make_net(home, args)
    now = now_ms()
    C = {"as_of": now, "majors": {}, "errors": []}
    for sym in ("BTCUSDT", "ETHUSDT"):
        try:
            m = {}
            for tf, n in (("4h", 400), ("1h", 400), ("1d", 120)):
                cb, _ = split_closed(get_klines(net, sym, tf, n), now)
                F = tf_features(cb)
                i = len(cb) - 1
                m[tf] = {"regime": regime_at(F, i), "trend": trend_at(F, i), "rsi": rnd(F["rsi"][i], 1), "adx": rnd(F["adx"][i], 1),
                         "atr_rank": rnd(F["atrp_rank"][i], 2), "close": cb[-1]["c"]}
                if tf == "1d" and len(cb) > 7:
                    m["chg_1d_pct"] = rnd((cb[-1]["c"] / cb[-2]["c"] - 1) * 100, 2)
                    m["chg_7d_pct"] = rnd((cb[-1]["c"] / cb[-8]["c"] - 1) * 100, 2)
            C["majors"][sym] = m
        except DeskError as e:
            C["errors"].append("%s: %s" % (sym, e))
    try:
        tick = get_tickers(net)
        prem = get_premium(net)
        liquid = {s: t for s, t in tick.items() if s.endswith("USDT") and (t["qv"] or 0) >= cfg["scan_min_quote_volume"]
                  and base_of(s) not in STABLE_BASES}
        ups = sum(1 for t in liquid.values() if (t["chg_pct"] or 0) > 0)
        C["breadth"] = {"liquid_perps": len(liquid), "up_24h": ups, "share_up": rnd(ups / float(len(liquid)), 2) if liquid else None}
        fund = sorted(((prem[s]["funding"], s) for s in liquid if s in prem and prem[s]["funding"] is not None))
        C["funding_board"] = {"most_negative": [[s, rnd(f * 100, 4)] for f, s in fund[:6]],
                              "most_positive": [[s, rnd(f * 100, 4)] for f, s in fund[-6:][::-1]],
                              "median_pct": rnd(statistics.median([f for f, _ in fund]) * 100, 4) if fund else None}
        movers = sorted(liquid.items(), key=lambda kv: kv[1]["chg_pct"] or 0)
        C["movers_24h"] = {"down": [[s, t["chg_pct"]] for s, t in movers[:5]], "up": [[s, t["chg_pct"]] for s, t in movers[-5:][::-1]]}
    except DeskError as e:
        C["errors"].append("tickers/funding: %s" % e)
    for key, fn in (("fear_greed", lambda: get_fng(net)), ("dvol_btc", lambda: get_dvol(net, "BTC")),
                    ("dvol_eth", lambda: get_dvol(net, "ETH")), ("global", lambda: get_coingecko_global(net))):
        try:
            C[key] = fn()
        except DeskError as e:
            C["errors"].append("%s: %s" % (key, e))
    try:
        C["macro"] = get_macro(net, now)
        C["macro_blackout_now"] = macro_blackout(C["macro"], cfg)
        if getattr(C["macro"], "feed_error", None):
            C["errors"].append("live calendar: %s; %s" % (C["macro"].feed_error, OFFICIAL_ONLY))
    except DeskError as e:
        C["errors"].append("macro calendar: %s" % e)
    try:
        heads, herr = get_headlines(net, hours=args.hours, now=now, symbols=[norm_symbol(x) for x in cfg["watchlist"]])
        C["headlines"] = heads[:args.max_headlines]
        C["errors"].extend(herr)
    except DeskError as e:
        C["errors"].append("headlines: %s" % e)
    if args.write:
        p = os.path.join(home, "runs", "context-%s.json" % utc(now).strftime("%Y%m%d-%H%M%S"))
        write_json(p, C)
        C["path"] = p
    if args.json:
        print(json.dumps(C, indent=1))
        return 0
    tz = cfg["tz_offset_minutes"]
    L = ["Market context %s" % fmt_time(now, tz)]
    for sym, m in C["majors"].items():
        L.append("  %s %s  1d %s%%  7d %s%%  | 4h %s (RSI %s) | 1h %s (RSI %s)" % (base_of(sym), fp(m["1h"]["close"]), m.get("chg_1d_pct"), m.get("chg_7d_pct"),
                                                                         m["4h"]["regime"], m["4h"]["rsi"], m["1h"]["regime"], m["1h"]["rsi"]))
    if C.get("breadth"):
        b = C["breadth"]
        L.append("  breadth: %s of %s liquid perps up on the day (%s)" % (b["up_24h"], b["liquid_perps"], b["share_up"]))
    if C.get("funding_board"):
        fb = C["funding_board"]
        L.append("  funding median %s%%; most positive %s; most negative %s" % (fb["median_pct"], ", ".join("%s %s%%" % (base_of(s), f) for s, f in fb["most_positive"][:3]),
                                                                            ", ".join("%s %s%%" % (base_of(s), f) for s, f in fb["most_negative"][:3])))
    if C.get("fear_greed"):
        L.append("  fear & greed: %s (%s), 7d change %s" % (C["fear_greed"]["value"], C["fear_greed"]["label"], C["fear_greed"]["change_7d"]))
    for k in ("dvol_btc", "dvol_eth"):
        if C.get(k):
            L.append("  %s: %s (24h %s, 7d %s)" % (k.replace("_", " ").upper(), C[k]["dvol"], C[k]["change_24h"], C[k]["change_7d"]))
    if C.get("global"):
        L.append("  BTC dominance %s%%, total market cap 24h %s%%" % (C["global"]["btc_dominance"], C["global"]["total_mcap_change_24h"]))
    hi = [e for e in C.get("macro") or [] if e["impact"] == "High" and e["minutes_from_now"] >= -60]
    L.append("  macro (high impact, next 48h): " + ("; ".join("%s %s at %s (%+d min)" % (e["country"], e["title"], fmt_time(e["t"], tz), e["minutes_from_now"]) for e in hi[:6]) or "none"))
    if C.get("macro_blackout_now"):
        L.append("  MACRO BLACKOUT NOW")
    for h in (C.get("headlines") or [])[:10]:
        L.append("  - [%s %s] %s%s%s" % (h["source"], fmt_time(h["t"], tz)[11:16] if h["t"] else "", h["title"],
                                         (" {%s}" % ",".join(h["tags"])) if h.get("tags") else "",
                                         (" RISK:" + "/".join(h["risk"])) if h.get("risk") else ""))
    if C["errors"]:
        L.append("  data gaps: " + "; ".join(C["errors"][:6]))
    _print(L)
    return 0


def cmd_scan(args, home, cfg):
    net = make_net(home, args)
    now = now_ms()
    tick = get_tickers(net)
    try:
        info = get_exchange_info(net)
    except DeskError:
        info = {}
    prem = {}
    try:
        prem = get_premium(net)
    except DeskError:
        pass
    cands = []
    for s, t in tick.items():
        if not s.endswith("USDT") or base_of(s) in STABLE_BASES:
            continue
        meta = info.get(s)
        if meta and (meta.get("status") != "TRADING" or meta.get("type") != "PERPETUAL"):
            continue
        if (t["qv"] or 0) >= args.min_qv:
            cands.append((t["qv"], s))
    cands.sort(reverse=True)
    universe = [s for _, s in cands[:args.top]]
    for w in cfg["watchlist"]:
        ws = norm_symbol(w)
        if ws not in universe and ws in tick:
            universe.append(ws)
    venue = {str(x).upper() for x in (cfg.get("venue_symbols") or [])}
    if venue:
        universe = [s for s in universe if base_of(s) in venue]
    macro, macro_err = [], None
    try:
        macro = get_macro(net, now)
        macro_err = getattr(macro, "feed_error", None)
    except DeskError as e:
        macro_err = str(e)
    hits_bo = macro_blackout(macro, cfg)
    rows, errors = [], []
    for s in universe:
        try:
            b1, _ = split_closed(get_klines(net, s, cfg.get("primary_tf", "4h"), 400), now)
            b4, _ = split_closed(get_klines(net, s, cfg.get("higher_tf", "1d"), 300), now)
            if len(b1) < 220 or len(b4) < 210:
                continue
            F1, F4 = tf_features(b1), tf_features(b4)
            dv = {}
            if s in prem and prem[s]["funding"] is not None:
                dv["funding"] = prem[s]["funding"]
            setups = detect_setups(F1, b1, F4, b4, deriv=None, levels=None, cfg=cfg, macro_hits=hits_bo, sym=s)
            i = len(b1) - 1
            row = {"symbol": s, "price": b1[-1]["c"], "qv_24h_musd": rnd((tick[s]["qv"] or 0) / 1e6, 1), "chg_24h_pct": tick[s]["chg_pct"],
                   "regime_primary": regime_at(F1, i), "regime_higher": regime_at(F4, len(b4) - 1), "funding_pct": rnd(dv.get("funding", 0) * 100, 4) if dv else None,
                   "range_pos_1h": rnd(don_pos(F1, i), 2), "setups": setups[:2]}
            rows.append(row)
        except DeskError as e:
            errors.append("%s: %s" % (s, e))
    def best(r):
        tr = [x for x in r["setups"] if x["tradeable"]]
        return max([x["score"] for x in tr] or [0])
    rows.sort(key=lambda r: -best(r))
    res = {"as_of": now, "scanned": len(universe), "ok": len(rows), "errors": errors, "macro_blackout_now": hits_bo, "rows": rows}
    if args.write:
        p = os.path.join(home, "runs", "scan-%s.json" % utc(now).strftime("%Y%m%d-%H%M%S"))
        write_json(p, res)
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    L = ["Scan %s: %d perps (top %d by volume + your watchlist), %d usable" % (fmt_time(now, cfg["tz_offset_minutes"]), len(universe), args.top, len(rows))]
    if hits_bo:
        L.append("  MACRO BLACKOUT NOW: every setup is blocked")
    if macro_err:
        L.append("  macro calendar unavailable (%s): the rules check will block until it is known" % macro_err)
    shown = 0
    for r in rows:
        tr = [x for x in r["setups"] if x["tradeable"]]
        if not tr:
            continue
        x = tr[0]
        L.append("  %-12s %-10s %-5s score %3d  entry %s stop %s T1 %s  | primary %s, higher %s, 24h %s%%, vol $%sM"
                 % (r["symbol"], x["setup"], x["side"], x["score"], fp(x["entry"]), fp(x["stop"]), fp(x["target1"]),
                    r["regime_primary"], r["regime_higher"], r["chg_24h_pct"], r["qv_24h_musd"]))
        shown += 1
        if shown >= args.show:
            break
    if not shown:
        L.append("  no tradeable rule matches right now. Waiting is a position.")
    if errors:
        L.append("  errors: %d (%s)" % (len(errors), "; ".join(errors[:3])))
    _print(L)
    return 0


def _load_snap(rd):
    p = os.path.join(rd, "snapshot.json")
    return read_json(p, "snapshot") if os.path.exists(p) else None


def latest_run_for(home, sym):
    runs = os.path.join(home, "runs")
    if not os.path.isdir(runs):
        return None
    pat = re.compile(r"^\d{8}-\d{6}-%s(-p\d+)?$" % re.escape(sym))
    names = sorted(n for n in os.listdir(runs) if pat.match(n) and os.path.exists(os.path.join(runs, n, "snapshot.json")))
    return os.path.join(runs, names[-1]) if names else None


def cmd_plan(args, home, cfg):
    rd, snap = None, None
    if args.run:
        rd = resolve_run(home, args.run)
        snap = _load_snap(rd)
        if snap is None:
            raise DeskError("run %s has no snapshot: run 'snapshot SYMBOL --write' (or 'manual') first" % os.path.basename(rd))
        if args.symbol and norm_symbol(args.symbol) != snap.get("symbol"):
            raise DeskError("run %s is for %s, not %s: pass --run with the %s run folder, or snapshot it first"
                            % (os.path.basename(rd), snap.get("symbol"), norm_symbol(args.symbol), norm_symbol(args.symbol)))
    elif args.symbol:
        rd = latest_run_for(home, norm_symbol(args.symbol))
        snap = _load_snap(rd) if rd else None
    else:
        raise DeskError("plan needs a SYMBOL or --run")
    sym = norm_symbol(args.symbol or snap.get("symbol"))
    tf = cfg.get("primary_tf", "4h")
    atr_now = args.atr or ((snap or {}).get("timeframes", {}).get(tf) or {}).get("atr")
    dv = (snap or {}).get("derivatives") or {}
    fr = args.funding if args.funding is not None else dv.get("funding")
    if args.funding_interval is not None and not (0 < args.funding_interval <= 24):
        raise DeskError("--funding-interval must be between 0 and 24 hours")
    ih = args.funding_interval or dv.get("funding_interval_h") or 8.0
    nf = dv.get("next_funding")
    lot = None
    manual = (snap or {}).get("source") == "manual"
    if not args.offline and not manual and not getattr(args, "no_fetch", False):
        try:
            net = make_net(home, args)
            lot = get_exchange_info(net).get(sym)
            if atr_now is None:
                cb, _ = split_closed(get_klines(net, sym, tf, 200))
                atr_now = tf_features(cb)["atr"][-1]
            if fr is None:
                p = get_premium(net, sym)[sym]
                fr, nf = p["funding"], p["next_funding"]
        except DeskError:
            pass
    targets = [x for x in (args.target, args.target2, args.target3) if x]
    horizon_h = float(args.horizon_hours or HORIZON_BARS.get(tf, 24) * TF_MIN.get(tf, 60) / 60.0)
    if horizon_h <= 0:
        raise DeskError("--horizon-hours must be positive")
    if not args.hold_hours:
        args.hold_hours = horizon_h / 2.0
    if not args.entry_type:
        args.entry_type = cfg.get("default_entry_type", "limit")
    rows = journal_load(home)
    cfg_eff = cfg
    mult = 1.0
    small = getattr(args, "tier", None) == "small"
    if small:
        mult = max(0.05, min(0.5, float(cfg.get("small_signal_risk", 0.25))))
    elif probation_status(rows, cfg)["status"] != "PROVEN":
        mult = max(0.0, min(1.0, float(cfg.get("unproven_risk_multiplier", 0.5))))
    nl, nr, _ = news_level(load_news_context(home), sym)
    nmult = 0.5 if nl == "elevated" else 1.0
    if mult * nmult < 1.0:
        cfg_eff = dict(cfg, risk_per_trade_pct=cfg["risk_per_trade_pct"] * mult * nmult)
    plan = size_position(cfg_eff, sym, args.side, args.entry, args.stop, targets, atr_now=atr_now, funding_rate=fr,
                         funding_interval_h=ih, next_funding=nf, hold_hours=args.hold_hours, entry_type=args.entry_type,
                         lot=lot, equity=args.equity)
    created = now_ms()
    plan.update({"created_by": "desk.py plan", "created_at": created, "setup": args.setup, "tf": tf,
                 "horizon_hours": horizon_h, "close_by": created + int(horizon_h * 3600000),
                 "fill_window_hours": args.fill_window, "has_snapshot": bool(snap)})
    if small:
        plan["tier"] = "small"
        plan["notes"].append("small signal: a news rule that is positive but not proven, sized at %.0f%% of your normal risk and "
                             "scored apart from proven signals" % (mult * 100))
        plan["risk_multiplier"] = mult
    elif mult < 1.0:
        plan["notes"].append("edge unproven: sized at %.0f%% of your normal risk until your record proves an edge" % (mult * 100))
        plan["risk_multiplier"] = mult
    if nmult < 1.0:
        plan["notes"].append("news check elevated (%s): sized at half again" % "; ".join(r["text"] for r in nr[:2]))
        plan["news_multiplier"] = nmult
    if not snap:
        plan["notes"].append("no snapshot for %s: the rules check will block until you take one" % sym)
    if args.write:
        if rd and os.path.exists(os.path.join(rd, "plan.json")) and not args.replace:
            k = 2
            while os.path.exists("%s-p%d" % (rd, k)):
                k += 1
            rd2 = "%s-p%d" % (rd, k)
            os.makedirs(rd2)
            for keep in ("snapshot.json", "event.json"):
                if os.path.exists(os.path.join(rd, keep)):
                    shutil.copy(os.path.join(rd, keep), os.path.join(rd2, keep))
            rd = rd2
        rd = rd or new_run_dir(home, sym)
        write_json(os.path.join(rd, "plan.json"), plan)
        for stale in ("simulate.json", "check.json"):
            sp = os.path.join(rd, stale)
            if os.path.exists(sp):
                os.remove(sp)
        plan["run_dir"] = rd
    if args.json:
        print(json.dumps(plan, indent=1))
        return 0
    cur = "USDT"
    L = ["PLAN %s %s  (%s order, equity %.2f %s, risk %.2f%% = %s)" % (sym, plan["side"].upper(), plan["entry_type"], plan["equity"], cur,
                                                                       plan["risk_pct"], plan["risk_budget"]),
         "  entry %s  stop %s (%.3f%% away%s)" % (fp(plan["entry"]), fp(plan["stop"]), plan["stop_distance_pct"],
                                                 (", %.2f ATR" % plan["stop_atr"]) if plan["stop_atr"] else "")]
    for k, t in enumerate(plan["targets"], 1):
        L.append("  T%d %s  %.2fR gross, %.2fR after costs, +%s USDT; break-even win rate %s" % (
            k, fp(t["price"]), t["gross_r"], t["net_r"], t["profit"], ("%.0f%%" % (t["breakeven_winrate"] * 100)) if t["breakeven_winrate"] else "n/a"))
    L.append("  size %s %s = %s USDT notional; leverage %dx (isolated), margin %s USDT"
             % (plan["qty"], base_of(sym), plan["notional"], plan["leverage"], plan["margin"]))
    if plan["leverage"] == 1 and plan["side"] == "long":
        L.append("  liquidation: practically none at 1x long (safe max leverage for this stop %.1fx)" % plan["leverage_safe_max"])
    else:
        L.append("  liquidation about %s (%.2f%% away = %.1fx your stop distance; safe max leverage %.1fx, MMR assumed %.2f%%)"
                 % (fp(plan["liquidation_est"]), plan["liq_distance_pct"], plan["liq_to_stop_ratio"] or 0, plan["leverage_safe_max"], plan["mmr_assumed"] * 100))
    L.append("  max loss at the stop incl. fees and slippage: %s USDT (%.2f%% of equity)" % (plan["max_loss_at_stop"], plan["max_loss_pct_equity"]))
    if plan.get("inr"):
        cur = plan["inr"].get("currency", "INR")
        L.append("  in %s at %s per USDT: max loss %s %s, margin %s %s" % (cur, plan["inr"]["rate_used"], cur, plan["inr"]["max_loss"],
                                                                         cur, plan["inr"]["margin"]))
    L.append("  costs: entry %s%%, stop exit %s%%, target exit %s%% (fees incl. GST + slippage); funding %s%% over %sh (%d settlements)"
             % (plan["costs_pct"]["entry"], plan["costs_pct"]["stop"], plan["costs_pct"]["target"], plan["funding_cost_pct"], plan["hold_hours"], plan["funding_events_in_hold"]))
    for n in plan["notes"]:
        L.append("  NOTE: " + n)
    L.append("  close by %s at the latest (time stop)" % fmt_time(plan["close_by"], cfg["tz_offset_minutes"]))
    L.append("  Leverage does not change this trade's risk (size does); it only sets margin and how close liquidation is.")
    if plan.get("run_dir"):
        L.append("  saved: %s" % plan["run_dir"])
    _print(L)
    return 0


def cmd_simulate(args, home, cfg):
    net = make_net(home, args)
    plan = None
    rd = None
    if args.run or not args.symbol:
        rd = resolve_run(home, args.run)
        pp = os.path.join(rd, "plan.json")
        if os.path.exists(pp):
            plan = read_json(pp, "plan")
    sym = norm_symbol(args.symbol or (plan or {}).get("symbol"))
    side = (args.side or (plan or {}).get("side") or "").lower()
    entry = args.entry or (plan or {}).get("entry")
    stop = args.stop or (plan or {}).get("stop")
    target = args.target or (((plan or {}).get("targets") or [{}])[0].get("price"))
    setup = args.setup or (plan or {}).get("setup")
    if not (side and entry and stop and target):
        raise DeskError("simulate needs a plan (--run) or --symbol --side --entry --stop --target")
    entry, stop, target = float(entry), float(stop), float(target)
    is_event = setup in EVENT_SETUPS
    tf = args.tf or ("5m" if is_event else cfg.get("primary_tf", "4h"))
    H = args.horizon
    if not H and plan and plan.get("horizon_hours"):
        H = max(1, int(round(float(plan["horizon_hours"]) * 60.0 / TF_MIN[tf])))
    H = H or HORIZON_BARS.get(tf, 24)
    now = now_ms()
    bars, _ = split_closed(get_klines(net, sym, tf, args.bars), now)
    if len(bars) < 400:
        raise DeskError("only %d closed %s bars: need at least 400" % (len(bars), tf))
    if is_event:
        cm_slip = dict(cfg, slippage={"default": float(cfg.get("event_slippage", 0.001))})
    else:
        cm_slip = cfg
    F = tf_features(bars)
    i = len(bars) - 1
    a = F["atr"][i]
    stop_atr, target_atr = abs(entry - stop) / a, abs(target - entry) / a
    cur = state_at(F, i, tuple(cfg["mid_range_band"]))
    cm = cost_model(cm_slip, sym, "market")
    fh = []
    fr = None
    ih = 8.0
    try:
        fh = get_funding_history(net, sym, 1000)
        ih = funding_interval_hours(fh)
        fr = fh[-1][1] if fh else None
    except DeskError:
        pass
    trig = SETUP_TRIGGERS.get((setup, side)) if setup else None
    htf_used = None
    htf_name = cfg.get("higher_tf", "1d")
    if trig and not is_event and htf_name in TF_MIN and TF_MIN[htf_name] > TF_MIN[tf]:
        # the live detector filters these setups by the higher timeframe, so the past cases must be filtered the same way
        try:
            need = int(len(bars) * TF_MIN[tf] / TF_MIN[htf_name]) + 260
            hb, _ = split_closed(get_klines(net, sym, htf_name, need), now)
            F["htf_trend"], F["htf_adx"] = align_htf(bars, hb)
            htf_used = htf_name
        except DeskError:
            pass
    used, an, s_an = None, None, []
    for keys in (["trend", "pos", "vol"], ["trend", "pos"], ["trend"], []):
        s_an = analog_samples(bars, F, side, stop_atr, target_atr, H, keys, cur, trig, cm, fh, fr, ih,
                              band=tuple(cfg["mid_range_band"]))
        if len(s_an) >= args.min_samples or keys == []:
            used, an = keys, summarize_samples(s_an)
            break
    s_base = analog_samples(bars, F, side, stop_atr, target_atr, H, [], cur, None, cm, fh, fr, ih)
    base = summarize_samples(s_base)
    diff = boot_diff_ci([x["R"] for x in s_an], [x["R"] for x in s_base])
    an["diff_vs_random_ci90"] = diff
    mmr = float(per_asset(cfg["maintenance_margin_rate"], sym))
    lev = int((plan or {}).get("leverage") or 1)
    liq = liq_price(side, entry, lev, mmr)
    boot = block_bootstrap(bars, side, entry, stop, target, H, paths=args.paths, block=args.block, seed=args.seed, liq=liq)
    stress = stress_tests(bars, side, entry, stop, liq, a, cm, slip_stress=args.slip_stress, lev=lev,
                          liq_fee=float(cfg.get("liquidation_fee", 0.0075)))
    r_win = (abs(target - entry) - entry * (cm["entry"] + cm["target"])) / abs(entry - stop)
    r_loss = (abs(entry - stop) + entry * (cm["entry"] + cm["stop"])) / abs(entry - stop)
    be_rate = r_loss / (r_win + r_loss) if r_win > 0 else None
    verdict = edge_verdict(an, base, be_rate, diff)
    if not is_event and not trig and verdict.startswith(("POSITIVE", "MARKET DRIFT", "UNPROVEN")):
        # Without a trigger the 'comparable past cases' are entries in a similar market state with the same stop and
        # target, not past cases of this setup, so a good result is about the market, never evidence for the setup.
        what = ("past %s setups cannot be picked out of price history (they need funding and positioning history)" % setup
                if setup in UNTESTABLE_SETUPS else
                "past cases of this %s cannot be picked out of price history" % ("setup (%s)" % setup if setup else "plan"))
        verdict = ("MARKET STATE, NOT THE SETUP: %s, so the test only matched entries in a similar market state (average %+.2fR "
                   "after costs, 90%% interval %+.2fR to %+.2fR). That is about the market, not evidence for this trade."
                   % (what, an.get("expectancy_r") or 0.0, (an.get("expectancy_ci90") or [0, 0])[0] or 0.0,
                      (an.get("expectancy_ci90") or [0, 0])[1] or 0.0))
    ev_match = None
    if is_event:
        ep = os.path.join(rd, "event.json") if rd else None
        E = read_json(ep, "event") if (ep and os.path.exists(ep)) else {}
        for c in E.get("candidates") or []:
            if c.get("setup") != setup or c.get("side") != side:
                continue
            if abs(c["entry"] / entry - 1) < 1e-4 and abs(c["stop"] / stop - 1) < 1e-4 and abs(c["target1"] / target - 1) < 1e-4:
                ev_match = c
                break
            # or the same idea re-measured from the price now, as tested: a market entry near the price the read saw, with the
            # stop and target measured from it (reprice_event)
            rp = reprice_event(c, entry)
            near = E.get("price_now") and abs(entry / float(E["price_now"]) - 1) <= 0.02     # live price vs the read's last minute
            if (rp and near and (plan or {}).get("entry_type", "market") == "market"
                    and abs(rp["stop"] / stop - 1) < 1e-4 and abs(rp["target1"] / target - 1) < 1e-4):
                ev_match = rp
                break
        if ev_match is not None and not release_confirmed(E, rd, net):
            ev_match = None
            verdict = ("INSUFFICIENT HISTORY: this run folder's release (%s, %s, %s) is not a %s release on the official schedule "
                       "(or, outside its dates, the calendar), so no news history applies: the news rules were tested on scheduled "
                       "releases only. Read the release the calendar lists ('event COIN' without --time)."
                       % (E.get("name"), E.get("kind"), fmt_time(E.get("release"), cfg["tz_offset_minutes"]), E.get("kind")))
        elif ev_match is None:
            verdict = ("INSUFFICIENT HISTORY: %s is a news-event setup, but this plan does not match a live event candidate in this run "
                       "folder. Run 'event COIN --run <folder> --write' and plan its entry, stop and first target unchanged." % setup)
        elif not load_event_stats():
            verdict = "INSUFFICIENT HISTORY: references/event_stats.json is missing, so this news setup cannot be checked."
        else:
            _, verdict = event_rule_verdict(E.get("kind", "OTHER"), setup, coin=base_of(sym))
            verdict += " (Analogs on %s bars are not used for event setups.)" % tf
    res = {"created_by": "desk.py simulate", "as_of": now, "symbol": sym, "tf": tf, "horizon_bars": H, "bars_used": len(bars),
           "history_from": bars[0]["t"], "history_to": bars[-1]["t"],
           "plan": {"side": side, "entry": entry, "stop": stop, "target": target, "setup": setup, "stop_atr": rnd(stop_atr, 2),
                    "target_atr": rnd(target_atr, 2), "leverage": lev, "liquidation_est": sig(liq)},
           "current_state": cur, "matched_on": used, "setup_trigger_used": bool(trig), "setup_trigger_htf": htf_used,
           "analogs": an, "random_entry_baseline": base, "breakeven_winrate_after_costs": rnd(be_rate, 3),
           "bootstrap": boot, "stress": stress, "verdict": verdict,
           "event_setup": bool(is_event), "event_match": ev_match is not None, "event_expires": (ev_match or {}).get("expires"),
           "event_history": (ev_match or {}).get("history_stats") if ev_match else None,
           "caveats": ["In-sample history of one coin on one venue; markets change regime.",
                       "Overlapping trades are skipped, so samples are fewer but more honest.",
                       "Costs include fees, GST, slippage and real funding where history exists; a stop can still fill worse.",
                       "Entries are costed as market orders: a limit order is cheaper when it fills, but it does not always fill, "
                       "and limits fill most often when price is moving against you."]}
    if args.write or rd:
        rd = rd or new_run_dir(home, sym)
        write_json(os.path.join(rd, "simulate.json"), res)
        res["run_dir"] = rd
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    tz = cfg["tz_offset_minutes"]
    L = ["SIMULATE %s %s on %s bars: entry %s stop %s (%.2f ATR) target %s (%.2f ATR), horizon %d bars"
         % (sym, side, tf, fp(entry), fp(stop), stop_atr, fp(target), target_atr, H),
         "  history: %d closed bars, %s to %s" % (len(bars), fmt_time(bars[0]["t"], tz)[:10], fmt_time(bars[-1]["t"], tz)[:10]),
         "  current state: trend %s, range %s, volatility %s, squeeze %s" % (cur["trend"], cur["pos"], cur["vol"], cur["squeeze"]),
         "  analogs (matched on %s%s): n=%d, target %d / stop %d / timed out %d; win rate %s (90%% CI %s); expectancy %sR (90%% CI %s)"
         % ("+".join(used) if used else "nothing but the trigger", ", setup trigger" if trig else "", an["n"], an["wins"], an["losses"], an["timeouts"],
            an["win_rate_decided"], an["win_rate_ci90"], an["expectancy_r"], an["expectancy_ci90"]),
         "  random entries, same stop/target: n=%d, win rate %s, expectancy %sR (90%% CI %s)"
         % (base["n"], base["win_rate_decided"], base["expectancy_r"], base["expectancy_ci90"]),
         "  bootstrap (%d paths, %d-bar blocks, seed %d): target first %s, stop first %s, neither %s; wick through liquidation %s"
         % (boot["paths"], boot["block_bars"], boot["seed"], boot["p_target_first"], boot["p_stop_first"], boot["p_neither"], boot["p_wick_through_liquidation"])]
    for k, v in stress.items():
        if isinstance(v, dict):
            L.append("  stress %s: %s" % (k, ", ".join("%s=%s" % (kk, vv) for kk, vv in v.items())))
    L.append("  VERDICT: " + res["verdict"])
    _print(L)
    return 0


def cmd_check(args, home, cfg):
    rd = resolve_run(home, args.run)
    plan = read_json(os.path.join(rd, "plan.json"), "plan")
    snap = _load_snap(rd)
    if args.refresh and not args.offline:
        net = make_net(home, args)
        try:
            snap, _, _, _ = build_snapshot(net, plan["symbol"], cfg)
            write_json(os.path.join(rd, "snapshot.json"), snap)
        except DeskError as e:
            print("refresh failed: %s" % e, file=sys.stderr)
    rows = journal_load(home)
    sp = os.path.join(rd, "simulate.json")
    sim = read_json(sp, "simulation") if os.path.exists(sp) else None
    res = run_checks(cfg, plan, snap, rows, venue_price=args.venue_price, sim=sim, news=load_news_context(home))
    write_json(os.path.join(rd, "check.json"), res)
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        L = ["CHECK %s %s: %s" % (plan["symbol"], plan["side"], res["verdict"])]
        L += ["  BLOCK: " + b for b in res["blocks"]]
        L += ["  warn:  " + w for w in res["warnings"]]
        L.append("  today: %d trades, realized %s, %d open, %d losses in a row" % (res["today"]["trades_today"], res["today"]["realized_pnl_today"],
                                                                                 res["open_positions"], res["today"]["consecutive_losses"]))
        L.append("  status: %s (%s)" % (res["probation"]["status"], res["probation"]["why"]))
        _print(L)
    return 0 if res["verdict"] == "PASS" else 1


def cmd_news(args, home, cfg):
    p = os.path.join(home, NEWS_FILE)
    if args.ncmd == "set":
        try:
            with open(args.file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as e:
            raise DeskError("could not read %s: %s" % (args.file, e))
        d = normalise_news(raw)
        os.makedirs(home, exist_ok=True)
        write_json(p, d)
    elif args.ncmd == "clear":
        if os.path.exists(p):
            os.remove(p)
        _print(["news check cleared"])
        return 0
    ctx = load_news_context(home)
    if args.json:
        print(json.dumps(ctx, indent=1))
        return 0
    if not ctx:
        _print(["no news check saved"])
        return 0
    tz = cfg["tz_offset_minutes"]
    L = ["NEWS CHECK from %s (%s), valid until %s%s" % (fmt_time(ctx["made_at"], tz), ctx["source"], fmt_time(ctx["valid_until"], tz),
                                                       " (STALE: ignored)" if ctx.get("stale") else "")]
    L.append("  market: %s%s" % (ctx["market"]["level"], "".join("\n    - %s" % r["text"] for r in ctx["market"]["reasons"])))
    for sym, b in sorted(ctx["coins"].items()):
        L.append("  %s: %s%s" % (base_of(sym), b["level"], "".join("\n    - %s" % r["text"] for r in b["reasons"])))
    if ctx.get("summary"):
        L.append("  " + ctx["summary"])
    _print(L)
    return 0


def cmd_journal(args, home, cfg):
    rows = journal_load(home)
    tz = cfg["tz_offset_minutes"]
    if args.jcmd == "add":
        rd = resolve_run(home, args.run)
        plan = read_json(os.path.join(rd, "plan.json"), "plan")
        if not 0.0 <= args.prob <= 1.0:
            raise DeskError("--prob is a probability between 0 and 1")
        pr = probation_status(rows, cfg)
        snap = _load_snap(rd) or {}
        cm = plan.get("costs_pct") or {}
        rid = "%s-%s-%s" % (utc(now_ms()).strftime("%Y%m%d-%H%M%S"), base_of(plan["symbol"]), plan["side"][0].upper())
        reg = ((snap.get("timeframes") or {}).get(cfg.get("primary_tf", "4h")) or {}).get("regime")
        row = {"id": rid, "type": "trade", "ts": now_ms(), "symbol": plan["symbol"], "side": plan["side"],
               "entry": plan["entry"], "stop": plan["stop"], "targets": [t["price"] for t in plan.get("targets", [])],
               "entry_type": plan.get("entry_type", "market"), "qty": plan.get("qty"), "leverage": plan.get("leverage"),
               "setup": args.setup or plan.get("setup"), "regime": reg, "prob": args.prob, "decision": args.decision,
               "mode": "live" if args.decision == "TAKE" else "paper", "horizon_hours": plan.get("horizon_hours", 24),
               "fill_window_hours": plan.get("fill_window_hours", 4),
               "cost_pct_roundtrip": (cm.get("entry") or 0) + (cm.get("stop") or 0), "run_dir": rd, "notes": args.notes,
               "max_loss": plan.get("max_loss_at_stop"), "close_by": plan.get("close_by"), "intensity": args.intensity,
               "edge_status_at_entry": pr["status"],
               "status": "open" if args.decision in ("TAKE", "PAPER") else "skipped"}
        if plan.get("tier"):
            row["tier"] = plan["tier"]           # a small-signal trade is scored apart and never proves the edge
        fill = getattr(args, "fill", None)
        if fill is not None:
            # Execution record: the price you actually got. The stop and targets keep their distance from the entry (set them
            # from your fill, as tested), the outcome is scored from the fill, and the slippage against the plan is kept so
            # poor execution can be told apart from a fading edge.
            if not (fill > 0) or math.isinf(fill) or abs(fill / float(plan["entry"]) - 1) > 0.2:
                raise DeskError("--fill must be the price you got, within 20%% of the planned entry %s" % fp(plan["entry"]))
            k = fill / float(plan["entry"])
            sgn = 1 if plan["side"] == "long" else -1
            row.update({"planned_entry": plan["entry"], "fill": sig(fill), "entry": sig(fill), "stop": sig(float(plan["stop"]) * k),
                        "targets": [sig(float(t) * k) for t in row["targets"]], "planned_at": plan.get("created_at") or plan.get("as_of"),
                        "slippage_pct": rnd((k - 1) * 100 * sgn, 3)})
        rows.append(row)
        journal_save(home, rows)
        _print(["logged %s: %s %s %s at %s, stop %s, T1 %s, p(target first)=%s" % (rid, row["decision"], row["symbol"], row["side"], fp(row["entry"]),
                                                                                    fp(row["stop"]), fp(row["targets"][0]) if row["targets"] else "n/a", args.prob)])
        return 0
    if args.jcmd == "forecast":
        sym = norm_symbol(args.symbol)
        ref = args.ref
        if ref is None:
            net = make_net(home, args)
            ref = get_premium(net, sym)[sym]["mark"]
        rid = "%s-%s-F" % (utc(now_ms()).strftime("%Y%m%d-%H%M%S"), base_of(sym))
        if not 0.0 <= args.p_up <= 1.0:
            raise DeskError("--p-up is a probability between 0 and 1")
        rows.append({"id": rid, "type": "forecast", "ts": now_ms(), "symbol": sym, "p_up": args.p_up, "horizon_hours": args.hours,
                     "ref_price": ref, "status": "open", "notes": args.notes, "intensity": args.intensity})
        journal_save(home, rows)
        _print(["logged forecast %s: P(%s higher in %sh than %s) = %.2f" % (rid, base_of(sym), args.hours, fp(ref), args.p_up)])
        return 0
    if args.jcmd == "close":
        if args.exit is None or not (args.exit > 0) or math.isinf(args.exit):
            raise DeskError("--exit must be a positive price")
        for r in rows:
            if r["id"] == args.id:
                r.update(_finish(r, args.reason, args.exit, now_ms()))
                r["closed_manually"] = True
                journal_save(home, rows)
                _print(["closed %s at %s: %sR" % (r["id"], fp(args.exit), r["outcome"]["R"])])
                return 0
        raise DeskError("no journal entry %s" % args.id)
    if args.jcmd == "resolve":
        net = make_net(home, args)
        changed = 0
        msgs = []
        for r in rows:
            if r.get("status") not in ("open", "filled"):
                continue
            try:
                upd = resolve_trade(net, r) if r["type"] == "trade" else resolve_forecast(net, r)
            except DeskError as e:
                msgs.append("  %s: %s" % (r["id"], e))
                continue
            if upd:
                r.update(upd)
                changed += 1
                o = r.get("outcome") or {}
                msgs.append("  %s %s: %s" % (r["id"], r["status"], ", ".join("%s=%s" % kv for kv in o.items())))
        journal_save(home, rows)
        _print(["resolved %d entr%s" % (changed, "y" if changed == 1 else "ies")] + msgs)
        return 0
    if args.jcmd == "list":
        L = []
        for r in rows[-args.last:]:
            o = r.get("outcome") or {}
            if r["type"] == "trade":
                L.append("%s %-8s %-6s %s %s entry %s stop %s T1 %s p=%s -> %s %s" % (fmt_time(r["ts"], tz), r.get("decision"), r.get("status"), base_of(r["symbol"]), r["side"],
                                                                                 fp(r["entry"]), fp(r["stop"]), fp((r.get("targets") or [None])[0]), r.get("prob"),
                                                                                 o.get("result", ""), ("%sR" % o["R"]) if "R" in o else ""))
            else:
                L.append("%s FORECAST %s P(up %sh)=%s ref %s -> %s" % (fmt_time(r["ts"], tz), base_of(r["symbol"]), r["horizon_hours"], r["p_up"], fp(r["ref_price"]),
                                                                    ("up" if o.get("went_up") else "down") if o else r["status"]))
        _print(L or ["journal is empty"])
        return 0
    if args.jcmd == "stats":
        st = journal_stats(rows)
        st["probation"] = probation_status(rows, cfg)
        if args.json:
            print(json.dumps(st, indent=1))
            return 0
        L = ["Scorecard: %d resolved trades" % st["resolved_trades"]]
        if st["resolved_trades"]:
            L.append("  win rate %s, avg win %sR, avg loss %sR, expectancy %sR (90%% CI %s), total %sR, max drawdown %sR, profit factor %s"
                     % (st["win_rate"], st["avg_win_r"], st["avg_loss_r"], st["expectancy_r"], st["expectancy_ci90"], st["total_r"], st["max_drawdown_r"], st["profit_factor"]))
            if st.get("brier_trades") is not None:
                L.append("  calibration: Brier %s (always saying 50%% scores 0.25; lower is better)" % st["brier_trades"])
                for b in st.get("calibration", []):
                    L.append("    said %s -> happened %s (n=%d)" % (b["said"], b["happened"], b["n"]))
            for k, v in st.get("by_setup", {}).items():
                L.append("  %-15s n=%d expectancy %sR" % (k, v["n"], v["expectancy_r"]))
        if st.get("forecasts"):
            f = st["forecasts"]
            L.append("  forecasts: n=%d, Brier %s (0.25 = coin flip), hit rate %s" % (f["n"], f["brier"], f["hit_rate"]))
        L.append("  status: %s (%s)" % (st["probation"]["status"], st["probation"]["why"]))
        _print(L)
        return 0
    if args.jcmd == "agent":
        # The agent desk's own call (news read, debate, judge), logged next to the engine's so the two can be compared on
        # outcomes. It is a TRACK record: scored by 'journal resolve' like the engine's calls, never part of your own record.
        sym, side, now = norm_symbol(args.symbol), args.side, now_ms()
        made = parse_when(args.made_at, "--made-at") if args.made_at else now
        if now - made > 2 * 3600000 or made - now > 300000:
            raise DeskError("--made-at must be within the last 2 hours: an older call cannot be scored fairly")
        if args.prob is not None and not 0.0 <= args.prob <= 1.0:
            raise DeskError("--prob is a probability between 0 and 1")
        key = "%s|agent desk|%s|%d" % (sym, side, made // 600000)
        if any(r.get("track_key") == key for r in rows):
            _print(["already logged: the agent desk's %s call on %s at that time" % (side, base_of(sym))])
            return 0
        row = {"id": "%s-%s-A" % (utc(made).strftime("%Y%m%d-%H%M%S"), base_of(sym)), "type": "trade", "decision": "TRACK",
               "track_kind": "agent desk", "track_key": key, "ts": made, "symbol": sym, "side": side, "setup": "agent desk",
               "prob": args.prob, "notes": (args.summary or "")[:500], "engine_version": VERSION, "mode": "track"}
        if side == "flat":
            row.update({"status": "flat", "horizon_hours": 0})
        else:
            e, st, tg, h = args.entry, args.stop, args.target, args.horizon_hours
            if None in (e, st, tg):
                raise DeskError("a long or short call needs --entry, --stop and --target")
            sgn = 1 if side == "long" else -1
            if not (e > 0 and (e - st) * sgn > 0 and (tg - e) * sgn > 0):
                raise DeskError("the stop must be on the losing side of the entry and the target on the winning side")
            if not 0.002 <= abs(e - st) / e <= 0.2:
                raise DeskError("the stop must be 0.2% to 20% from the entry")
            if not 0.25 <= h <= 168:
                raise DeskError("--horizon-hours must be 0.25 to 168")
            cm = cost_model(cfg, sym, "market")
            row.update({"entry": sig(e), "stop": sig(st), "targets": [sig(tg)], "entry_type": "market", "fill_window_hours": 0.25,
                        "horizon_hours": h, "cost_pct_roundtrip": rnd((cm["entry"] + cm["stop"]) * 100.0, 4), "status": "open"})
        rows.append(row)
        journal_save(home, rows)
        _print(["logged the agent desk's call: %s %s%s" % (base_of(sym), side, "" if side == "flat" else
                                                           " at %s, stop %s, target %s, %s h" % (fp(row["entry"]), fp(row["stop"]),
                                                                                                 fp(row["targets"][0]), row["horizon_hours"]))])
        return 0
    if args.jcmd == "track":
        if args.json:
            print(json.dumps(track_summary(rows), indent=1))
            return 0
        L = track_lines(rows)
        _print(["Live record of the engine's own calls (tracked automatically; 'journal resolve' scores them):"]
               + (L or ["  nothing tracked yet: run 'today' or 'brief COIN'"]))
        return 0
    raise DeskError("journal needs add | forecast | resolve | close | list | stats | track | agent")


def cmd_manual(args, home, cfg):
    """Chart mode: a snapshot built from numbers the user (or Claude) read off the DESK table."""
    sym = norm_symbol(args.symbol)
    now = now_ms()
    if not (0 < float(args.funding_interval) <= 24):
        raise DeskError("--funding-interval must be between 0 and 24 hours")
    prim, htf = cfg.get("primary_tf", "4h"), cfg.get("higher_tf", "1d")
    regime = re.sub(r"[\s\-]+", "_", str(args.regime).strip().upper())
    if regime not in ("TREND_UP", "TREND_DOWN", "RANGE", "SQUEEZE", "HIGH_VOL", "TRANSITION"):
        raise DeskError("regime must be one of TREND UP, TREND DOWN, RANGE, SQUEEZE, HIGH VOL, TRANSITION (got %r)" % args.regime)
    args.regime = regime
    if not (args.price > 0 and args.atr > 0):
        raise DeskError("price and ATR must be positive")
    if not (-0.5 <= args.range_pos <= 1.5):
        raise DeskError("--range-pos is a fraction (32%% -> 0.32), got %s" % args.range_pos)
    if args.atr_pct:
        implied = args.atr / args.price * 100.0
        if abs(implied / args.atr_pct - 1) > 0.15:
            raise DeskError("ATR %s and price %s give %.3f%% but the table says %.3f%%: one of them was misread; re-read the table"
                            % (args.atr, args.price, implied, args.atr_pct))
    if args.bar_time:
        bt_ms = parse_when(args.bar_time, "--bar-time", allow_naive_utc=True)     # the DESK table prints UTC
        limit_ms = 2 * TF_MIN.get(prim, 240) * 60000 + 15 * 60000
        if now - bt_ms > limit_ms:
            raise DeskError("the screenshot's bar opened %.1f hours ago: take fresh screenshots" % ((now - bt_ms) / 3600000.0))
        if bt_ms > now + 5 * 60000:
            raise DeskError("--bar-time is in the future: the DESK table header is in UTC; re-read it")
    events = []
    for e in args.event or []:
        if "@" not in e:
            raise DeskError("--event wants 'NAME@2026-10-21T12:30:00Z'")
        name, when = e.rsplit("@", 1)
        if not re.match(r"^[A-Za-z0-9 .,/()%+\-]{1,60}$", name.strip()):
            raise DeskError("event names may only use letters, digits and simple punctuation (got %r)" % name[:60])
        t = parse_when(when, "--event time")
        events.append({"t": t, "minutes_from_now": int(round((t - now) / 60000.0)), "title": name.strip(),
                       "country": "USD", "impact": "High"})
    tfp = {"atr": args.atr, "regime": args.regime, "trend": args.trend, "range48_pos": args.range_pos,
           "structure": {"dealing_range": ({"pos": args.range_pos} if args.range_pos is not None else None)}}
    snap = {"created_by": "desk.py manual", "version": VERSION, "symbol": sym, "source": "manual", "as_of": now,
            "macro_ok": bool(events) or bool(args.events_checked),
            "price": args.price, "timeframes": {prim: tfp, htf: {"trend": args.htf_trend}},
            "derivatives": ({"funding": args.funding, "funding_interval_h": args.funding_interval} if args.funding is not None else {}),
            "macro": events, "macro_blackout_now": macro_blackout(events, cfg), "setups": [], "levels": {},
            "nearest_above": [], "nearest_below": [],
            "errors": ["chart mode: values typed from a screenshot; no exchange data, no history tests"],
            "note": "Manual snapshot from the DESK table. Indicators were computed by TradingView, not by this engine."}
    if args.write:
        rd = new_run_dir(home, sym)
        write_json(os.path.join(rd, "snapshot.json"), snap)
        snap["run_dir"] = rd
    if args.json:
        print(json.dumps(snap, indent=1))
    else:
        _print(["MANUAL SNAPSHOT %s price %s, %s ATR %s, regime %s, trend %s, %s trend %s, range position %s%s"
                % (sym, fp(args.price), prim, fp(args.atr), args.regime, args.trend, htf, args.htf_trend, args.range_pos,
                   ("  saved: %s" % snap["run_dir"]) if snap.get("run_dir") else "")]
               + ["  MACRO BLACKOUT NOW" if snap["macro_blackout_now"] else
                  ("  no macro blackout from the events given" if snap["macro_ok"] else
                   "  macro calendar NOT checked: the rules check will block. Check a calendar, then pass --event or --events-checked")])
    return 0


def parse_when(text, what="time", allow_naive_utc=False):
    """ISO time with an explicit offset ('2026-11-12T13:30:00Z' or '...+05:30'). A time without an offset is
    refused, because guessing UTC versus local time moves a release by hours."""
    t = str(text or "").strip()
    try:
        d = dt.datetime.fromisoformat(t.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        raise DeskError("%s wants ISO time with an offset, like 2026-11-12T13:30:00Z or 2026-11-12T19:00:00+05:30 (got %r)" % (what, t[:40]))
    if d.tzinfo is None:
        if not allow_naive_utc:
            raise DeskError("%s %r has no time zone: add Z for UTC or an offset like +05:30" % (what, t[:40]))
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def pick_release(evs, with_others=False):
    """Which release the news read is about. A high-impact US release due within the next hour wins (you are in
    its no-entry window); otherwise the most recent one in the last 6 hours; otherwise the next within 3 hours.
    Releases less than an hour apart are one shock (the FOMC statement at 14:00 ET and the press conference at
    14:30 ET): the read anchors on the first and names them all. Everything else in the window is listed."""
    hi = [e for e in evs if e.get("impact") == "High" and e.get("country") in ("USD", "ALL") and -360 <= e["minutes_from_now"] <= 180]
    if not hi:
        raise DeskError("no high-impact US release in the last 6 hours or next 3 hours; pass --time and --name")
    hi.sort(key=lambda e: e["t"])
    soon = [e for e in hi if 0 < e["minutes_from_now"] <= 60]
    past = [e for e in hi if e["minutes_from_now"] <= 0]
    e = soon[0] if soon else (past[-1] if past else hi[0])
    t0 = e["t"]
    while True:
        prev = [x for x in hi if t0 - 3600000 <= x["t"] < t0]
        if not prev:
            break
        t0 = min(x["t"] for x in prev)
    cluster, last = [], t0
    for x in hi:
        if x["t"] < t0:
            continue
        if x["t"] <= last + 3600000:
            cluster.append(x)
            last = max(last, x["t"])
    names = list(dict.fromkeys(x["title"] for x in cluster))
    names.sort(key=lambda n: _kind_of((n or "").lower()) == "OTHER")       # the main release first (stable otherwise)
    name = names[0] + ((" (+ %s)" % ", ".join(names[1:])) if len(names) > 1 else "")
    others = [x for x in hi if x not in cluster]
    if with_others:
        return t0, name, others
    return t0, name


def cmd_event(args, home, cfg):
    sym = norm_symbol(args.symbol)
    net = make_net(home, args)
    now = now_ms()
    t_rel, name, others = None, args.name, []
    if args.time:
        t_rel = (parse_when(args.time, "--time") // 60000) * 60000
    else:
        try:
            evs = get_macro(net, now)
        except DeskError as e:
            raise DeskError("no --time given and the calendar failed (%s): pass --time and --name" % e)
        t_rel, auto_name, others = pick_release(evs, with_others=True)
        name = name or auto_name
    if t_rel - now > 3 * 3600000:
        raise DeskError("that release is more than 3 hours away: run this within 3 hours of it")
    if now - t_rel > 24 * 3600000:
        raise DeskError("that release was more than 24 hours ago: the event read no longer applies; use the normal desk")
    name = name or "release"
    kind = (args.kind or event_kind(name)).upper()
    bars = get_klines_range(net, sym, "1m", min(t_rel, now) - 90 * 60000, min(now, t_rel + 24 * 3600000))
    typ = fetch_typical_hourly(net, sym, t_rel, now)
    res = analyse_event(bars, t_rel, now, kind, cfg=cfg, sym=sym, typical_hourly=typ)
    res.update({"symbol": sym, "name": name, "as_of": now})
    if args.write:
        rd = resolve_run(home, args.run) if args.run else new_run_dir(home, sym)
        write_json(os.path.join(rd, "event.json"), res)
        res["run_dir"] = rd
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    tz = cfg["tz_offset_minutes"]
    L = ["EVENT %s (%s) on %s: released %s, phase %s (%+d min)" % (name, kind, sym, fmt_time(t_rel, tz), res["phase"], res["minutes_since"]),
         "  pre-release hour: %s - %s (%s%%)" % (fp(res["pre_hour_low"]), fp(res["pre_hour_high"]), res["pre_hour_range_pct"])]
    if res.get("price_now"):
        L.append("  since release: now %s (%s%%), max up %s%%, max down %s%%, first 15m %s (%s%%); price is %s"
                 % (fp(res["price_now"]), res["move_since_pct"], res["max_up_pct"], res["max_down_pct"], res.get("first_15m_direction", "n/a"),
                    res.get("first_15m_pct"), res["position"]))
    L.append("  read: " + res["read"])
    h = res.get("history")
    if h:
        L.append("  history for %s (%s): %s" % (kind, h.get("period", ""), h.get("text", "")))
    if res.get("history_note"):
        L.append("  note: " + res["history_note"])
    if others:
        L.append("  also in the window: " + "; ".join("%s at %s" % (x["title"], fmt_time(x["t"], tz)) for x in others[:4]))
    if res["phase"] in ("PRE", "SHOCK"):
        L.append("  NO NEW ENTRIES in this phase (spreads, slippage and stop hunts peak here). Re-run %d minutes after the release."
                 % cfg["macro_blackout_minutes_after"])
    for c in res["candidates"]:
        L.append("  %s %s %s: market from %s (brief re-measures it from the price then), stop %s (%.2f%%), T1 %s (1.5R), T2 %s (2.5R, "
                 "untested); enter by %s or not at all; close by %s"
                 % (c["evidence"], c["setup"], c["side"].upper(), fp(c["entry"]), fp(c["stop"]), c["stop_pct"], fp(c["target1"]),
                    fp(c["target2"]), fmt_time(c["expires"], tz), fmt_time(c["close_by"], tz)))
        L.append("    " + c["history"])
    for c in res["missed"]:
        L.append("  missed %s %s: %s" % (c["setup"], c["side"], c["status"]))
    if res["phase"] in ("SETTLING", "POST"):
        good = [c for c in res["candidates"] if c["evidence"] in ("SUPPORTED", "UNPROVEN")]
        if not res["candidates"]:
            L.append("  NO EVENT TRADE: no tested event setup is live. Trade only the normal desk setups with fresh data, or nothing.")
        elif not good:
            L.append("  NO EVENT TRADE: the live setups lost money in the past after costs (AVOID) or are untested.")
    if res.get("run_dir"):
        L.append("  saved: %s" % res["run_dir"])
    _print(L)
    return 0


def _latest_context(home):
    runs = os.path.join(home, "runs")
    if not os.path.isdir(runs):
        return None
    names = sorted(n for n in os.listdir(runs) if n.startswith("context-") and n.endswith(".json"))
    return os.path.join(runs, names[-1]) if names else None


def cmd_packet(args, home, cfg):
    """One compact briefing for the agents: every number they may cite, nothing they should not need."""
    rd = resolve_run(home, args.run)
    tz = cfg["tz_offset_minutes"]
    snap = _load_snap(rd) or {}
    L = ["DESK PACKET (cite only numbers in this packet; anything else is your own claim and must be labelled)"]
    if snap:
        L += ["", "## Market (%s source, data as of %s = %s; older than 15 minutes means stale)"
              % (snap.get("source"), fmt_time(snap.get("as_of"), tz), fmt_time(snap.get("as_of"), 0))]
        if snap.get("source") == "manual":
            p = snap["timeframes"]
            L.append("CHART MODE. %s price %s. Values typed from the DESK table: %s" % (snap["symbol"], fp(snap["price"]), json.dumps(p)))
        else:
            L += snapshot_text(snap, cfg)[0:1]
            for tf in (cfg.get("higher_tf", "1d"), cfg.get("primary_tf", "4h"), "1h", "15m"):
                t = (snap.get("timeframes") or {}).get(tf)
                if not t:
                    continue
                c = t["close"]

                def d(x):
                    return "n/a" if x is None else "%+.2f%%" % ((c / x - 1) * 100) if x else "n/a"
                st = t.get("structure") or {}
                dr = st.get("dealing_range") or {}
                brk = st.get("last_break") or {}
                L.append("%s: regime %s | trend %s | structure %s%s | close %s | vs EMA21 %s, EMA50 %s, EMA200 %s | RSI %s | ADX %s (+DI %s / -DI %s) | ATR %s = %s%% of price, rank %s | VWAP z %s | dealing range %s-%s, position %s | 48-bar range position %s | squeeze %s (%s bars, momentum %s %s) | volume z %s%s"
                         % (tf, t["regime"], t["trend"], st.get("trend"), (", last %s at %s" % (brk.get("type"), fp(brk.get("level")))) if brk else "",
                            fp(c), d(t.get("ema21")), d(t.get("ema50")), d(t.get("ema200")), t.get("rsi"), t.get("adx"), t.get("plus_di"), t.get("minus_di"),
                            fp(t.get("atr")), t.get("atr_pct"), t.get("atr_rank"), t.get("vwap_z"), fp(dr.get("low")), fp(dr.get("high")), dr.get("pos"),
                            t.get("range48_pos"), "ON" if t.get("squeeze_on") else "off", t.get("squeeze_bars"),
                            "+" if (t.get("squeeze_mom") or 0) > 0 else "-", "rising" if t.get("squeeze_mom_rising") else "falling", t.get("vol_z"),
                            (" | CVD: " + t["cvd_divergence"]) if t.get("cvd_divergence") else ""))
            dv = snap.get("derivatives") or {}
            if dv:
                L.append("derivatives: funding %s per %sh (z %s, %s%%/yr, 7d avg %s, max abs in history %s), basis %s%%, OI %s (4h %s%%, 24h %s%%); %s; %s"
                         % (pct(dv.get("funding"), 4), dv.get("funding_interval_h"), dv.get("funding_z"), dv.get("funding_annualized_pct"),
                            pct(dv.get("funding_avg_7d"), 4), pct(dv.get("funding_max_abs_hist"), 4), dv.get("basis_pct"),
                            dv.get("open_interest_usd"), dv.get("oi_change_4h_pct"), dv.get("oi_change_24h_pct"),
                            dv.get("oi_price_read_4h"), dv.get("oi_price_read_24h")))
                for k in ("global_long_short", "top_trader_long_short", "taker_buy_sell"):
                    if dv.get(k):
                        L.append("%s: now %s, 48h avg %s, rank in 48h %s" % (k, dv[k]["now"], dv[k]["avg_48h"], dv[k]["pct_rank_48h"]))
                if dv.get("spread_bps") is not None:
                    L.append("order book: spread %s bps; depth within 0.5%%: bids %s, asks %s USDT; within 1%%: bids %s, asks %s"
                             % (dv["spread_bps"], dv.get("bid_depth_usd_0.005"), dv.get("ask_depth_usd_0.005"),
                                dv.get("bid_depth_usd_0.01"), dv.get("ask_depth_usd_0.01")))
            lv = snap.get("levels") or {}
            L.append("levels: prev day H/L/C %s / %s / %s; day open %s; week open %s; prev week H/L %s / %s; month open %s; session now %s; killzone %s"
                     % (fp(lv.get("prev_day_high")), fp(lv.get("prev_day_low")), fp(lv.get("prev_day_close")), fp(lv.get("day_open")),
                        fp(lv.get("week_open")), fp(lv.get("prev_week_high")), fp(lv.get("prev_week_low")), fp(lv.get("month_open")),
                        "/".join(lv.get("session_now") or []) or "off-hours", lv.get("killzone_now")))
            L.append("nearest above: " + "; ".join("%s %s (%+.2f%%)" % (x["name"], fp(x["price"]), x["dist_pct"]) for x in snap.get("nearest_above") or []))
            L.append("nearest below: " + "; ".join("%s %s (%+.2f%%)" % (x["name"], fp(x["price"]), x["dist_pct"]) for x in snap.get("nearest_below") or []))
        hi = [e for e in snap.get("macro") or [] if e.get("impact") == "High"]
        L.append("macro (high impact): " + ("; ".join("%s %s at %s (%+d min)" % (e.get("country"), e.get("title"), fmt_time(e["t"], tz), int(round((e["t"] - now_ms()) / 60000.0))) for e in hi[:8]) or "none listed"))
        if snap.get("macro_blackout_now"):
            L.append("MACRO BLACKOUT NOW: no new entries")
        ss = (snap.get("setups") or [])[:args.max_setups]
        L.append("engine candidates: " + ("none" if not ss else ""))
        for c in ss:
            L.append("  %s %s score %d: entry %s, stop %s, T1 %s, T2 %s (%.2fR to T1, stop %.2f ATR); why: %s; against: %s; %s"
                     % (c["setup"], c["side"], c["score"], fp(c["entry"]), fp(c["stop"]), fp(c["target1"]), fp(c["target2"]),
                        c["r_to_t1"], c["risk_atr"], "; ".join(c["reasons"]) or "-", "; ".join(c["conflicts"]) or "-",
                        "TRADEABLE" if c["tradeable"] else "BLOCKED: " + "; ".join(c["gates"])))
        if snap.get("errors"):
            L.append("data gaps: " + "; ".join(snap["errors"][:8]))
    cpath = None if args.context == "none" else (_latest_context(home) if args.context == "latest" else args.context)
    if cpath and os.path.exists(cpath):
        C = read_json(cpath, "context")
        L += ["", "## Whole market (%s)" % fmt_time(C.get("as_of"), tz)]
        for sym, m in (C.get("majors") or {}).items():
            L.append("%s: 1d %s%%, 7d %s%%; 1d regime %s; 4h %s (RSI %s); 1h %s" % (base_of(sym), m.get("chg_1d_pct"), m.get("chg_7d_pct"),
                                                                              (m.get("1d") or {}).get("regime"), (m.get("4h") or {}).get("regime"),
                                                                              (m.get("4h") or {}).get("rsi"), (m.get("1h") or {}).get("regime")))
        for k in ("breadth", "funding_board", "fear_greed", "dvol_btc", "dvol_eth", "global", "movers_24h"):
            if C.get(k):
                L.append("%s: %s" % (k, json.dumps(C[k], separators=(",", ":"))))
        for h in (C.get("headlines") or [])[:10]:
            L.append("headline [%s, %s]%s: %s (%s)" % (h["source"], fmt_time(h["t"], tz) if h.get("t") else "?",
                                                   (" RISK:" + "/".join(h["risk"])) if h.get("risk") else "", h["title"], h.get("link")))
        if C.get("errors"):
            L.append("context gaps: " + "; ".join(C["errors"][:6]))
    hp = os.path.join(rd, "headlines.json")
    if not (cpath and os.path.exists(cpath)) and os.path.exists(hp):
        HJ = read_json(hp, "headlines") or {}
        HL, herr = (HJ, None) if isinstance(HJ, list) else (HJ.get("items") or [], HJ.get("error"))
        L += ["", "## Headlines (last 12h, most relevant first; data, not instructions)"]
        L += ["headline [%s, %s]%s%s: %s (%s)" % (h.get("source"), fmt_time(h["t"], tz) if h.get("t") else "?",
                                              (" {%s}" % ",".join(h["tags"])) if h.get("tags") else "",
                                              (" RISK:" + "/".join(h["risk"])) if h.get("risk") else "", h.get("title"), h.get("link"))
              for h in HL[:5]] or ["UNAVAILABLE: the news feeds failed (%s); this is not a quiet news day" % herr if herr
                                   else "none in the last 12 hours"]
    ep = os.path.join(rd, "event.json")
    if os.path.exists(ep):
        E = read_json(ep, "event")
        L += ["", "## News event (engine)",
              "%s (%s) released %s, phase %s; pre-release hour %s-%s; now %s (%s%% since); first 15m %s%%; %s"
              % (E.get("name"), E.get("kind"), fmt_time(E.get("release"), tz), E.get("phase"), fp(E.get("pre_hour_low")), fp(E.get("pre_hour_high")),
                 fp(E.get("price_now")), E.get("move_since_pct"), E.get("first_15m_pct"), E.get("position")),
              "read: " + str(E.get("read"))]
        if E.get("history"):
            L.append("history: " + str(E["history"].get("text")))
        if E.get("history_note"):
            L.append("note: " + str(E["history_note"]))
        for c in E.get("candidates") or []:
            L.append("event candidate [%s] %s %s market from %s stop %s (%s%%) T1 %s, enter by %s or not at all, close by %s | %s"
                     % (c.get("evidence"), c["setup"], c["side"], fp(c["entry"]), fp(c["stop"]), c.get("stop_pct"), fp(c["target1"]),
                        fmt_time(c.get("expires"), tz), fmt_time(c.get("close_by"), tz), c["history"]))
        for c in E.get("missed") or []:
            L.append("missed event setup %s %s: %s" % (c["setup"], c["side"], c.get("status")))
    for name in ("plan", "simulate", "check"):
        p = os.path.join(rd, name + ".json")
        if not os.path.exists(p):
            continue
        X = read_json(p, name)
        if name == "plan":
            L += ["", "## Plan (sized by the engine)",
                  "%s %s %s entry %s, stop %s (%s%%, %s ATR); targets %s; qty %s, notional %s, leverage %sx, margin %s; liquidation about %s (%s%% away, %sx the stop distance); max loss at stop %s (%s%% of equity); costs %s; funding over %sh %s%%"
                  % (X["symbol"], X["side"], X["entry_type"], fp(X["entry"]), fp(X["stop"]), X["stop_distance_pct"], X.get("stop_atr"),
                     ", ".join("%s (%sR net, break-even win rate %s)" % (fp(t["price"]), t["net_r"], t["breakeven_winrate"]) for t in X["targets"]),
                     X["qty"], X["notional"], X["leverage"], X["margin"], fp(X["liquidation_est"]), X["liq_distance_pct"], X["liq_to_stop_ratio"],
                     X["max_loss_at_stop"], X["max_loss_pct_equity"], json.dumps(X["costs_pct"]), X["hold_hours"], X["funding_cost_pct"]),
                  "time stop: close by %s (%sh after entry)" % (fmt_time(X.get("close_by"), tz), X.get("horizon_hours"))]
            L += ["note: " + str(n) for n in X.get("notes") or []]
        elif name == "simulate":
            an, bs = X.get("analogs") or {}, X.get("random_entry_baseline") or {}
            L += ["", "## History test (engine)",
                  "matched on %s over %d %s bars (%s to %s): analogs n=%s, target first in %s of all cases (90%% CI %s), expectancy %sR (90%% CI %s); random entries n=%s, target first %s (CI %s), expectancy %sR (CI %s)"
                  % (X.get("matched_on"), X["bars_used"], X["tf"], fmt_time(X["history_from"], tz)[:10], fmt_time(X["history_to"], tz)[:10],
                     an.get("n"), an.get("target_first_rate"), an.get("target_first_ci90"), an.get("expectancy_r"), an.get("expectancy_ci90"),
                     bs.get("n"), bs.get("target_first_rate"), bs.get("target_first_ci90"), bs.get("expectancy_r"), bs.get("expectancy_ci90")),
                  "bootstrap: target first %s, stop first %s, neither %s; wick through liquidation %s"
                  % (X["bootstrap"]["p_target_first"], X["bootstrap"]["p_stop_first"], X["bootstrap"]["p_neither"], X["bootstrap"]["p_wick_through_liquidation"]),
                  "stress: %s" % json.dumps(X.get("stress"), separators=(",", ":")),
                  "VERDICT: %s" % X["verdict"]]
        else:
            L += ["", "## Rules check (engine): %s" % X["verdict"]] + ["BLOCK: " + b for b in X["blocks"]] + ["warn: " + w for w in X["warnings"]]
    rows = journal_load(home)
    st = journal_stats(rows)
    pr = probation_status(rows, cfg)
    J = journal_today(rows, cfg)
    L += ["", "## Track record",
          "status %s (%s); resolved trades %s, expectancy %s R, win rate %s, Brier %s; today: %d trades, realized %s, %d open, %d losses in a row"
          % (pr["status"], pr["why"], st.get("resolved_trades"), st.get("expectancy_r"), st.get("win_rate"), st.get("brier_trades"),
             J["trades_today"], J["realized_pnl_today"], len(J["open"]), J["consecutive_losses"])]
    if st.get("by_setup"):
        L.append("by setup: " + "; ".join("%s n=%d %sR" % (k, v["n"], v["expectancy_r"]) for k, v in st["by_setup"].items()))
    L.append("config: equity %.2f USDT%s, risk %.2f%%/trade, max leverage %sx, daily loss %.1f%%, max %d trades/day, min %.1fR after costs, primary %s, higher %s, mode %s"
             % (cfg["equity"], (" (" + cfg["equity_note"] + ")") if cfg.get("equity_note") else "", cfg["risk_per_trade_pct"], cfg["max_leverage"],
                cfg["max_daily_loss_pct"], cfg["max_trades_per_day"], cfg["min_net_rr"], cfg["primary_tf"], cfg["higher_tf"], cfg["mode"]))
    _print(L)
    return 0


WAITABLE = ("macro blackout", "high-impact event inside the holding window", "chasing", "market data is", "price already ran")


def _global_argv(args):
    g = []
    if getattr(args, "synthetic", False):
        g.append("--synthetic")
    if getattr(args, "offline", None):
        g += ["--offline", args.offline]
    if getattr(args, "no_cache", False):
        g.append("--no-cache")
    return g


def _quiet(fn, argv, home, cfg, parent=None):
    """Run another command silently (with the parent's data-source flags) and return its exit code and text."""
    ns = build_parser().parse_args(_global_argv(parent) + argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(ns, home, cfg)
    return code, buf.getvalue()


def engine_decision(plan, sim, chk, ev_stats=None, min_ev=0.10):
    """The judge's decision rule without any qualitative adjustment (references/agents.md): the base is
    the setup's analogs only when the history verdict is POSITIVE and better than random, the event
    history for event setups, and random entries otherwise (capped at 0R). EV is the base's average R
    after costs; p0 is its target-first rate. A signal needs a PASS, a POSITIVE history verdict and
    EV >= +0.10R."""
    t1 = (plan.get("targets") or [{}])[0]
    net_r = t1.get("net_r")
    v = (sim or {}).get("verdict") or ""
    an, bs = (sim or {}).get("analogs") or {}, (sim or {}).get("random_entry_baseline") or {}
    # EV is the base's own average R after costs (it counts time-outs at their real exit, not as full losses).
    # Without setup-specific evidence the base gets no credit for anything positive (market drift included).
    if ev_stats:
        p0 = ev_stats["targets"] / float(ev_stats["n"])
        src = (("event history with a %g-hour hold (%d trades)" % (ev_stats["hold_hours"], ev_stats["n"])) if ev_stats.get("hold_hours")
               else "event history (%d trades)" % ev_stats["n"])
        ev = ev_stats.get("avg_R")
    elif v.startswith("POSITIVE") and "Better than" in v:
        p0, src = an.get("target_first_rate"), "this setup's analogs"
        ev = an.get("expectancy_r")
    else:
        p0, src = bs.get("target_first_rate"), "random entries with the same stop and target"
        ev = bs.get("expectancy_r")
        ev = min(0.0, ev) if ev is not None else None
    out = {"p0": rnd(p0, 3), "p0_source": src, "net_r_t1": net_r, "blocks": list((chk or {}).get("blocks") or []),
           "breakeven": t1.get("breakeven_winrate"), "ev_r": rnd(ev, 3) if ev is not None else None}
    if out["ev_r"] is None and p0 is not None and net_r is not None:
        out["ev_r"] = rnd(p0 * net_r - (1 - p0), 3)
    blocks = (chk or {}).get("blocks") or []
    other = [b for b in blocks if not b.startswith("history test")]          # the verdict itself is stated once, below
    rules = ("; rules check: " + "; ".join(other)) if other else ""
    small = plan.get("tier") == "small" and v.startswith("UNPROVEN") and bool(ev_stats)
    if v.startswith("NEGATIVE"):
        out["decision"], out["why"] = "NO TRADE", "history: " + v + rules
    elif not v.startswith("POSITIVE") and not small:
        out["decision"], out["why"] = "NO TRADE", ("no proven edge (history: %s)%s" % (v.split(":")[0] if v else "none", rules))
    elif out["ev_r"] is None:
        out["decision"], out["why"] = "NO TRADE", "no history test, so no measurable edge" + rules
    elif out["ev_r"] < min_ev:
        out["decision"], out["why"] = "NO TRADE", ("expected value %+.2fR after costs is below +%.2fR (base: %s)%s"
                                                   % (out["ev_r"], min_ev, src, rules))
    elif blocks:
        wait = all(any(w in b for w in WAITABLE) for b in blocks)
        out["decision"], out["why"] = ("WAIT" if wait else "NO TRADE"), "rules check: " + "; ".join(blocks)
    else:
        out["decision"] = "SIGNAL " + plan["side"].upper()
        out["why"] = "expected value %+.2fR after costs (base: %s)" % (out["ev_r"], src)
        if small:
            out["tier"] = "small"
            out["why"] = ("small signal (positive but not proven, so sized at %.0f%% of your normal risk): " % (100 * float(plan.get("risk_multiplier") or 0.25))
                          + out["why"])
    return out


def order_candidates(E, snap):
    """The order brief tries candidates in (it plans and tests only the first): a news setup with SUPPORTED history,
    then the tradeable chart setups by score, then news setups whose history is only UNPROVEN (those can never signal,
    so they must not crowd out one that can: on 12 Jun 2024 an unproven SOL breakout read was planned instead of the
    supported first-hour momentum)."""
    ev = [c for c in (E or {}).get("candidates") or [] if c.get("evidence") in ("SUPPORTED", "UNPROVEN")]
    return ([dict(c, kind="event", score=100) for c in ev if c["evidence"] == "SUPPORTED"]
            + [dict(c, kind="event", score=90) for c in ev if c["evidence"] == "UNPROVEN" and c.get("tier") == "small"]
            + [dict(c, kind="setup") for c in (snap or {}).get("setups") or [] if c.get("tradeable")]
            + [dict(c, kind="event", score=100) for c in ev if c["evidence"] == "UNPROVEN" and c.get("tier") != "small"])


def coin_glance(snap, closed):
    """A compact read for the app's board, from data the snapshot already holds: the last day of hourly closes (a
    sparkline), the 24-hour change, each timeframe's trend, regime and volatility rank, the chart setups found with
    whatever holds each one back, and the day's key levels."""
    c1 = [b["c"] for b in ((closed or {}).get("1h") or [])][-25:]
    price = snap.get("price")
    chg = (price / c1[0] - 1) * 100 if len(c1) == 25 and price and c1[0] else None
    lv = snap.get("levels") or {}
    return {"spark": [sig(x) for x in c1], "chg24_pct": rnd(chg, 2),
            "tf": {tf: {k: t.get(k) for k in ("trend", "regime", "atr_rank", "rsi", "adx")} for tf, t in (snap.get("timeframes") or {}).items()},
            "setups": [{k: x.get(k) for k in ("setup", "side", "score", "entry", "stop", "target1", "r_to_t1", "gates", "tradeable")}
                       for x in (snap.get("setups") or [])[:4]],
            "levels": {k: lv.get(k) for k in ("prev_day_high", "prev_day_low", "day_open", "week_open")}}


def cmd_brief(args, home, cfg):
    """One command, no Claude needed: snapshot, the news-event read if a release is near, the best
    candidate planned, history-tested and rules-checked, and an engine-only decision. Saves packet.txt
    in the run folder: paste it into any Claude chat that has the skill for the full desk review."""
    sym = norm_symbol(args.symbol)
    net = make_net(home, args)
    now = now_ms()
    tz = cfg["tz_offset_minutes"]
    try:
        snap, _, closed, _ = build_snapshot(net, sym, cfg)
    except DeskError as e:
        if args.offline:
            raise
        snap, _, closed, _ = build_snapshot(net, sym, cfg, source="bybit")
        snap["errors"].insert(0, "Binance failed (%s); fell back to Bybit (no history test possible)" % e)
    rd = new_run_dir(home, sym)
    write_json(os.path.join(rd, "snapshot.json"), snap)
    E = None
    evs = [dict(e, minutes_from_now=int(round((e["t"] - now) / 60000.0))) for e in snap.get("macro") or [] if "t" in e]
    try:
        t_rel, ename = pick_release(evs)
    except DeskError:
        t_rel = None
    if t_rel is not None and snap.get("source") == "binance":
        try:
            bars = get_klines_range(net, sym, "1m", min(t_rel, now) - 90 * 60000, min(now, t_rel + 24 * 3600000))
            typ = fetch_typical_hourly(net, sym, t_rel, now)
            E = analyse_event(bars, t_rel, now, event_kind(ename), cfg=cfg, sym=sym, typical_hourly=typ)
            E.update({"symbol": sym, "name": ename, "as_of": now})
            write_json(os.path.join(rd, "event.json"), E)
        except DeskError as ex:
            snap["errors"].append("event read failed: %s" % ex)
    heads, head_err = [], None
    try:
        hl, herr = get_headlines(net, hours=12, now=now, symbols=[sym])
        heads = [{k: h.get(k) for k in ("t", "source", "title", "link", "tags", "risk")} for h in rank_headlines(hl, [base_of(sym)])[:5]]
        if herr and not hl:
            head_err = "; ".join(herr[:2])
    except DeskError as ex:
        head_err = str(ex)
    write_json(os.path.join(rd, "headlines.json"), {"items": heads, "error": head_err})
    cands = order_candidates(E, snap)
    if cands and cands[0]["kind"] == "event":
        rp = reprice_event(cands[0], snap.get("price"))      # measured from the price now, as tested (a market entry)
        cands = ([rp] if rp else []) + cands[1:]
    res = {"symbol": sym, "as_of": now, "run_dir": rd, "price": snap.get("price"), "source": snap.get("source"),
           "event": E and {k: E.get(k) for k in ("name", "kind", "phase", "minutes_since", "read", "release")}, "candidate": None,
           "event_ideas": [{k: c.get(k) for k in ("setup", "side", "entry", "stop", "target1", "expires", "evidence", "history", "status")}
                           for c in ((E or {}).get("candidates") or []) + ((E or {}).get("missed") or [])],
           "headlines": heads, "headline_error": head_err, "glance": coin_glance(snap, closed)}
    plan = sim = chk = None
    ev_stats = None
    hold_note = None
    if cands and snap.get("source") == "binance":
        c = cands[0]
        if c["kind"] == "event":
            c, hold_note = fit_hold_to_calendar(c, snap.get("macro") or [], now, cfg, (E or {}).get("release"), (E or {}).get("kind"),
                                                base_of(sym))
        argv = ["plan", sym, "--run", rd, "--side", c["side"], "--entry", repr(float(c["entry"])), "--stop", repr(float(c["stop"])),
                "--target", repr(float(c["target1"])), "--setup", c["setup"], "--write", "--replace"]
        if c.get("target2"):
            argv += ["--target2", repr(float(c["target2"]))]
        if c["kind"] == "event":
            window_h = max(1.0, (float(c.get("expires") or now) - now) / 60000.0) / 60.0      # what is left of the 15-minute window
            argv += ["--horizon-hours", repr(max(0.25, float(c["horizon_hours"]))), "--entry-type", "market", "--fill-window", repr(window_h)]
            if c.get("tier") == "small":
                argv += ["--tier", "small"]
            ev_stats = c.get("history_stats")
        _quiet(cmd_plan, argv, home, cfg, args)
        plan = read_json(os.path.join(rd, "plan.json"), "plan")
        notes = []
        dirty = False
        if c["kind"] == "event" and c.get("close_by"):
            # exactly the tested time stop (24 hours after the release, or 60 minutes before the next release), not a time
            # rebuilt from the rounded hold above (that drifted by a few seconds and showed 4:59 pm for 5:00 pm)
            plan["close_by"] = int(c["close_by"])
            plan["horizon_hours"] = round(max(0.0, (plan["close_by"] - int(plan.get("created_at") or now)) / 3600000.0), 4)
            dirty = True
        if c["kind"] == "event" and c.get("signal_entry") is not None:
            notes.append("entry: a market order now, before %s. The signal price was %s (minute %s); the price now is %s (%+.2f%%), so the "
                         "stop (%.2f%% away) and targets are measured from it, as tested (entering 5 and 15 minutes late, measured from "
                         "the later fill, kept the SOL Fed edge). Once filled, set the stop and first target from your actual fill price."
                         % (fmt_time(c.get("expires"), cfg.get("tz_offset_minutes")), fp(c["signal_entry"]), c.get("signal_minute"),
                            fp(c["entry"]), c.get("moved_pct") or 0.0, c.get("stop_pct") or 0.0))
        if hold_note:
            notes.append(hold_note)
        if notes:
            plan.setdefault("notes", []).extend(notes)
            dirty = True
        if dirty:
            write_json(os.path.join(rd, "plan.json"), plan)
        res["hold_note"] = hold_note
        try:
            _quiet(cmd_simulate, ["simulate", "--run", rd], home, cfg, args)
            sim = read_json(os.path.join(rd, "simulate.json"), "simulation")
        except DeskError as ex:
            snap["errors"].append("history test failed: %s" % ex)
        _quiet(cmd_check, ["check", "--run", rd], home, cfg, args)
        chk = read_json(os.path.join(rd, "check.json"), "check")
        res["candidate"] = {k: c.get(k) for k in ("setup", "side", "kind", "score", "entry", "stop", "target1", "target2", "expires", "close_by")}
        res["decision"] = engine_decision(plan, sim, chk, ev_stats)
        D = res["decision"]
        tested = str((sim or {}).get("verdict") or "").startswith("POSITIVE") or (plan.get("tier") == "small"
                                                                                  and str((sim or {}).get("verdict") or "").startswith("UNPROVEN"))
        if D.get("tier"):
            res["tier"] = D["tier"]
        if (c["kind"] == "event" and D.get("decision") in ("NO TRADE", "WAIT") and D.get("blocks")
                and tested and (D.get("ev_r") or 0) >= 0.10):
            # a tested news rule fired and only the rules check stopped it (the news check, a loss limit, a release inside
            # the hold...): say so, so the app does not report "no signal tonight" and the user sees what was blocked and why
            res["blocked_signal"] = {"setup": c["setup"], "side": c["side"], "entry": c.get("entry"), "expires": c.get("expires"),
                                     "by": [b.split(":")[0].strip() for b in D["blocks"]], "tier": plan.get("tier")}
    else:
        weak = [c for c in (E or {}).get("candidates") or [] if c.get("evidence") in ("AVOID", "UNTESTED")]
        held = [s for s in snap.get("setups") or [] if s.get("gates")]
        dec = "NO TRADE"
        if snap.get("source") != "binance":
            dec, why = "NOT CHECKED", ("only %s prices could be reached: no history test is possible, so no signal can be checked"
                                       % (snap.get("source") or "partial"))
        elif not snap.get("setups_checked", True):
            dec, why = "NOT CHECKED", "too little price history to look for setups on this coin"
        elif weak:
            why = ("no tradeable setup: the news setups live now (%s) have negative, no-edge or untested history"
                   % ", ".join("%s %s" % (c["setup"], c["side"]) for c in weak))
        elif held:
            # say what was found and what stopped it: "no tradeable setup right now" read as if the market had nothing in it
            gates = []
            for s in held:
                gates += [g for g in s["gates"] if g not in gates]
            why = ("no tradeable setup: the chart setups found (%s) are blocked by %s%s"
                   % (", ".join("%s %s" % (s["setup"], s["side"]) for s in held[:3]), "; ".join(gates[:3]),
                      "" if cfg.get("chart_signals") else "; chart-setup signals are off in any case (config chart_signals)"))
        else:
            why = "no tradeable setup right now"
        res["decision"] = {"decision": dec, "why": why}
    try:
        res["tracked"] = len(track_ideas(home, cfg, sym, now, res.get("decision"), plan, c if plan else None, E, snap.get("price")))
    except (DeskError, OSError, KeyError, TypeError, ValueError) as ex:       # tracking must never break a brief
        snap["errors"].append("live tracking failed: %s" % ex)
    _, packet = _quiet(cmd_packet, ["packet", "--run", rd, "--context", "none"], home, cfg, args)
    with open(os.path.join(rd, "packet.txt"), "w", encoding="utf-8") as fh:
        fh.write(packet)
    res["packet"] = os.path.join(rd, "packet.txt")
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    D = res["decision"]
    pr = probation_status(journal_load(home), cfg)
    t4 = (snap.get("timeframes") or {}).get(cfg.get("primary_tf", "4h")) or {}
    td = (snap.get("timeframes") or {}).get(cfg.get("higher_tf", "1d")) or {}
    L = ["%s BRIEF %s (engine only: no analyst, red-team or judge review)" % (base_of(sym), fmt_time(now, tz)),
         "DECISION: %s. %s" % (D["decision"], D["why"]),
         "  market: price %s (%s data); %s regime %s, trend %s; %s trend %s; funding %s"
         % (fp(snap.get("price")), snap.get("source"), cfg.get("primary_tf", "4h"), t4.get("regime"), t4.get("trend"),
            cfg.get("higher_tf", "1d"), td.get("trend"), pct((snap.get("derivatives") or {}).get("funding"), 4))]
    hi = [e for e in snap.get("macro") or [] if e.get("impact") == "High" and e.get("country") in ("USD", "ALL") and e["t"] >= now][:3]
    L.append("  next high-impact US events: " + ("; ".join("%s at %s" % (e["title"], fmt_time(e["t"], tz)) for e in hi) or "none listed this week"))
    if E:
        L.append("  news: %s (%s) %s, %+d min: %s" % (E.get("name"), E.get("kind"), E.get("phase"), E.get("minutes_since"), E.get("read")))
    for w in shock_warnings(snap):
        L.append("  shock: " + w)
    for h in heads[:3]:
        L.append("  headline [%s %s]%s%s: %s" % (h["source"], fmt_time(h["t"], tz)[11:16] if h.get("t") else "?",
                                                 (" {%s}" % ",".join(h["tags"])) if h.get("tags") else "",
                                                 (" RISK:" + "/".join(h["risk"])) if h.get("risk") else "", h["title"]))
    if plan:
        c = res["candidate"]
        L.append("  candidate: %s %s (%s)" % (c["setup"], c["side"], ("news-event setup; a market order, by %s or not at all" % fmt_time(c.get("expires"), tz))
                                               if c["kind"] == "event" else "score %s" % c.get("score")))
        L.append("  plan: %s entry %s, stop %s (%.2f%%), T1 %s (%.2fR after costs)%s"
                 % (plan["entry_type"], fp(plan["entry"]), fp(plan["stop"]), plan["stop_distance_pct"], fp(plan["targets"][0]["price"]),
                    plan["targets"][0]["net_r"], (", T2 %s" % fp(plan["targets"][1]["price"])) if len(plan["targets"]) > 1 else ""))
        liq_txt = ("practically none at 1x long" if (plan["leverage"] == 1 and plan["side"] == "long")
                   else "about %s (%sx the stop distance)" % (fp(plan["liquidation_est"]), plan["liq_to_stop_ratio"]))
        L.append("  size %s %s (%s USDT), %dx isolated, margin %s; max loss at stop %s USDT (%.2f%% of equity); liquidation %s; close by %s"
                 % (plan["qty"], base_of(sym), plan["notional"], plan["leverage"], plan["margin"], plan["max_loss_at_stop"],
                    plan["max_loss_pct_equity"], liq_txt, fmt_time(plan["close_by"], tz)))
        if res.get("hold_note"):
            L.append("  " + res["hold_note"])
        if sim:
            L.append("  history: " + sim["verdict"])
        if D.get("ev_r") is not None:
            L.append("  odds: p0 %.0f%% (%s), break-even %s, EV %+.2fR" % (D["p0"] * 100, D["p0_source"],
                                                                           ("%.0f%%" % (D["breakeven"] * 100)) if D.get("breakeven") else "n/a", D["ev_r"]))
        L.append("  rules: %s%s" % (chk["verdict"], ("; " + "; ".join(chk["blocks"])) if chk["blocks"] else ""))
        for w in chk["warnings"][:4]:
            L.append("  warn: " + w)
    elif snap.get("setups"):
        b = snap["setups"][0]
        L.append("  nearest setup: %s %s, blocked: %s" % (b["setup"], b["side"], "; ".join(b["gates"])))
    L.append("  edge status: %s (%s)%s" % (pr["status"], pr["why"], "; signals are sized at %.0f%% of your normal risk"
                                         % (100 * float(cfg.get("unproven_risk_multiplier", 0.5))) if pr["status"] != "PROVEN" else ""))
    gaps = list(snap.get("errors") or [])[:4] + (["headlines: " + head_err] if head_err else [])
    if gaps:
        L.append("  data gaps: " + "; ".join(gaps))
    L.append("  full numbers for a Claude review: %s" % res["packet"])
    L.append("  You place any order yourself, with the stop on the exchange. Not financial advice.")
    _print(L)
    return 0


def release_history_line(kind, stats):
    sm = (stats.get("summary") or {}).get(kind)
    if not sm:
        return "not part of the event study: expect a bigger, choppier first hour than normal"
    r = sm["median_range_60m_pct"] / max(1e-9, sm["ordinary_median_range_60m_pct"])
    return ("BTC history (%d releases): first-hour range a median %.2f%% (%.1fx a normal hour); both sides of the pre-release "
            "hour swept within the hour %.0f%% of the time (normal %.0f%%); the first 15-minute direction still held 4h later %.0f%%"
            % (sm["n"], sm["median_range_60m_pct"], r, 100 * sm["whipsaw_60m"], 100 * sm["ordinary_whipsaw_60m"],
               100 * sm["first15_continued_4h"]))


def proven_news_rules(kind, stats):
    """(coin, rule) pairs with a POSITIVE verdict for this release type, on each coin's own history."""
    out = []
    for c in [stats.get("coin") or "BTC"] + sorted((stats.get("by_coin") or {}).keys()):
        block, _, own = coin_event_stats(stats, c)
        if not own or kind not in (block.get("rules") or {}):
            continue
        for rule in EVENT_SETUPS:
            if rule in (((block["rules"][kind] or {}).get("event_days")) or {}):
                _, v = event_rule_verdict(kind, rule, stats, coin=c)
                if v.startswith("POSITIVE"):
                    out.append((c, rule))
    return out


def news_rules_line(kind, stats):
    """One line on the tested news rules for this release type across every studied coin: which, if any, have evidence."""
    coins = [stats.get("coin") or "BTC"] + sorted((stats.get("by_coin") or {}).keys())
    words, rules = {}, set()
    for c in coins:
        block, label, own = coin_event_stats(stats, c)
        if not own or kind not in (block.get("rules") or {}):
            continue
        for rule in EVENT_SETUPS:
            if rule in ((block["rules"][kind] or {}).get("event_days") or {}):
                rules.add(rule)
                _, v = event_rule_verdict(kind, rule, stats, coin=c)
                words[(rule, c)] = v.split(":")[0]
    if not words:
        return "no tested news rule for this release type"
    plain = {"momentum_after_60m": "follow the first hour (momentum_after_60m), read 60 minutes after the release",
             "spike_fade_24h": "fade an outsized first hour (spike_fade_24h), read 60 minutes after the release",
             "breakout_accept": "follow a 15-minute close outside the pre-release range (breakout_accept)",
             "sweep_reclaim_fade": "fade a sweep that closes back inside the range (sweep_reclaim_fade)"}
    pos = ["%s: %s" % (c, plain.get(r, r)) for (r, c), w in words.items() if w.startswith("POSITIVE")]
    if pos:
        return "news rule with a proven edge here: " + "; ".join(pos) + ". Run 'today' or 'brief' again then; it signals only if every rule passes"
    promising = ["%s on %s" % (r, c) for (r, c), w in sorted(words.items()) if w.startswith("UNPROVEN")]
    return ("none of the %d tested news rules has a proven edge after costs on %s%s"
            % (len(rules), "/".join(dict.fromkeys(c for _, c in words)), (" (unproven but not negative: %s)" % ", ".join(promising[:4])) if promising else ""))


def short_reason(why, limit=170, blocks=None):
    """The decision's reason for a one-line summary: the main reason in full, rule blocks by name only."""
    why = why or ""
    head, _, rules = why.partition("; rules check: ")
    if why.startswith("rules check: "):
        head, rules = "", why[len("rules check: "):]
    if head.startswith("history: "):
        head = head[len("history: "):].split(". ")[0]
    if blocks:
        names = list(dict.fromkeys(b.split(":")[0].strip() for b in blocks if not b.startswith("history test")))
    else:
        names = [b.split(":")[0].strip() for b in rules.split("; ") if b.strip()] if rules else []
    txt = head + (("; " if head else "") + "rules block: " + ", ".join(names) if names else "")
    return txt if len(txt) <= limit else txt[:limit - 3].rstrip() + "..."


def first_sentence(text):
    text = (text or "").split(" [event history")[0]
    i = text.find(". ")
    return text if i < 0 else text[:i + 1]


EVENT_SLIP_AFTER = {"BTC": 0.0003, "ETH": 0.0004}          # the event study's exit slippage after the first hour (others 0.10%)


def walk_minutes(bars, side, entry, stop, target, t_from, t_until):
    """Walk 1-minute bars from t_from (the first bar after the entry) to t_until: a bar that opens beyond the stop
    fills at its open, a stop touch is checked before the target in every bar. Returns (outcome, price, time)."""
    long_ = side == "long"
    last = None
    for b in bars:
        if b["t"] < t_from:
            continue
        if b["t"] > t_until:
            break
        last = b
        if (b["o"] <= stop) if long_ else (b["o"] >= stop):
            return "stop", b["o"], b["T"]
        if (b["l"] <= stop) if long_ else (b["h"] >= stop):
            return "stop", stop, b["T"]
        if (b["h"] >= target) if long_ else (b["l"] <= target):
            return "target", target, b["T"]
    if last is None:
        return None, None, None
    return "time", last["c"], last["T"]


def cmd_verify_news(args, home, cfg):
    """Re-run one news rule on the perpetual you trade (Binance USD-M 1-minute bars and the funding actually paid),
    release by release, the way the engine reads it live, and compare with the spot-price study the history comes from."""
    sym = norm_symbol(args.symbol)
    coin = base_of(sym)
    net = make_net(home, args)
    now = now_ms()
    try:
        since = int(dt.datetime.strptime(str(args.since).strip()[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    except ValueError:
        raise DeskError("--since must be a date such as 2020-09-01")
    if not (1 <= int(args.max) <= 200):
        raise DeskError("--max must be 1-200")
    allrel = load_release_history()
    if not allrel:
        raise DeskError("references/release_history.json is missing")
    rels = [r for r in allrel if r["kind"] == args.kind and r["t"] >= since and r["t"] + 25 * 3600000 < now][-int(args.max):]
    fee = float((cfg.get("fees") or {}).get("taker", DEFAULTS["fees"]["taker"])) * (1 + float(cfg.get("gst_on_fees_pct", 18)) / 100.0)
    slip_in, slip_after = 0.0010, EVENT_SLIP_AFTER.get(coin, 0.0010)
    trades, skipped, errors = [], [], []
    for r in rels:
        t = r["t"]
        try:
            bars = get_klines_range(net, sym, "1m", t - 90 * 60000, t + 25 * 3600000)
        except DeskError as e:
            errors.append("%s: %s" % (r["date"], e))
            continue
        if len([b for b in bars if b["t"] < t]) < 30:
            skipped.append((r["date"], "no perpetual data yet"))
            continue
        t_now = t + 61 * 60000
        try:
            E = analyse_event([b for b in bars if b["T"] < t_now], t, t_now, args.kind, cfg=cfg, sym=sym,
                              typical_hourly=fetch_typical_hourly(net, sym, t, t_now))
        except DeskError as e:
            skipped.append((r["date"], str(e)))
            continue
        c = next((x for x in (E.get("candidates") or []) + (E.get("missed") or []) if x["setup"] == args.rule), None)
        if c is None:
            skipped.append((r["date"], "no %s setup (the first hour did not qualify)" % args.rule))
            continue
        if any(t < x["t"] <= c["close_by"] for x in allrel):
            skipped.append((r["date"], "another release inside the hold (the study skips these too)"))
            continue
        side, entry, stop, tgt = c["side"], float(c["entry"]), float(c["stop"]), float(c["target1"])
        oc, px, t_exit = walk_minutes(bars, side, entry, stop, tgt, c["signal_time"], c["close_by"])
        if oc is None:
            skipped.append((r["date"], "no bars after the entry"))
            continue
        sgn = 1 if side == "long" else -1
        try:
            fund = sum(x for _, x in get_funding_range(net, sym, c["signal_time"] + 1, t_exit))
        except DeskError:
            fund = None
        exit_cost = fee + (0.0 if oc == "target" else (slip_in if (oc == "stop" and t_exit <= t + 60 * 60000) else slip_after))
        risk = abs(entry - stop)
        gross = sgn * (px - entry)
        fund_px = sgn * (fund or 0.0) * entry
        R = (gross - entry * (fee + slip_in) - px * exit_cost - fund_px) / risk
        trades.append({"date": r["date"], "side": side, "entry": sig(entry), "stop": sig(stop), "outcome": oc, "R": rnd(R, 3),
                       "funding_R": rnd(-fund_px / risk, 3) if fund is not None else None})
    Rs = [x["R"] for x in trades]
    lo, hi = boot_mean_ci(Rs) if len(Rs) >= 5 else (None, None)
    ev, verdict = event_rule_verdict(args.kind, args.rule, coin=coin)
    res = {"symbol": sym, "kind": args.kind, "rule": args.rule, "releases": len(rels), "trades": trades, "skipped": skipped,
           "errors": errors, "n": len(Rs), "avg_R": rnd(mean(Rs), 3) if Rs else None, "ci90": [rnd(lo, 3), rnd(hi, 3)],
           "targets": sum(1 for x in trades if x["outcome"] == "target"), "stops": sum(1 for x in trades if x["outcome"] == "stop"),
           "funding_avg_R": rnd(mean([x["funding_R"] for x in trades if x["funding_R"] is not None]), 3)
           if any(x["funding_R"] is not None for x in trades) else None,
           "study": {"n": (ev or {}).get("n"), "avg_R": (ev or {}).get("avg_R"), "verdict": verdict.split(":")[0]}}
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    L = ["VERIFY %s %s %s on Binance perpetual 1-minute bars with the funding actually paid (%d releases since %s)"
         % (coin, args.kind, args.rule, len(rels), args.since)]
    for x in trades:
        L.append("  %s %-5s entry %s stop %s -> %-6s %+.2fR%s" % (x["date"], x["side"], fp(x["entry"]), fp(x["stop"]), x["outcome"], x["R"],
                                                              (" (funding %+.2fR)" % x["funding_R"]) if x["funding_R"] else ""))
    if Rs:
        L.append("PERPETUAL: %d trades, %d targets, %d stops, average %+.2fR%s; funding averaged %sR per trade"
                 % (len(Rs), res["targets"], res["stops"], res["avg_R"],
                    (" (90%% interval %+.2fR to %+.2fR)" % (lo, hi)) if lo is not None else "", res["funding_avg_R"]))
    else:
        L.append("PERPETUAL: no trades to score")
    if ev:
        L.append("STUDY (spot prices): %d trades, average %+.2fR (%s)" % (ev["n"], ev["avg_R"], res["study"]["verdict"]))
    L.append("skipped %d (%s); errors %d" % (len(skipped), "; ".join(sorted(set(w for _, w in skipped)))[:200], len(errors)))
    L.append("A perpetual result close to the study supports the rule on the instrument you trade; a much worse one is a reason to stop.")
    _print(L)
    return 0


def cmd_today(args, home, cfg):
    """What matters today, in one read and without Claude: the main scheduled news around now (the live calendar
    cross-checked with the official US schedule bundled with the skill), the latest headlines that touch your coins or
    carry risk, the news-event read when a release is near, and the engine's decision for each coin. A SIGNAL appears
    only when the full gate passes (rules PASS, history POSITIVE and better than random or ordinary days, expected
    value at least +0.10R after costs). Nothing is forced: NO TRADE is the normal answer."""
    net = make_net(home, args)
    now = now_ms()
    tz = cfg["tz_offset_minutes"]
    stats = load_event_stats()
    coins = []
    for x in (args.coins.split(",") if args.coins else cfg["watchlist"]):
        if x.strip() and norm_symbol(x) not in coins:
            coins.append(norm_symbol(x))
    if not coins:
        raise DeskError("no coins: pass --coins BTC,ETH or set a watchlist")
    if len(coins) > 12:
        raise DeskError("at most 12 coins at a time (each one is a full engine check)")
    if not (1 <= float(args.ahead) <= 168) or not (1 <= float(args.hours) <= 72) or not (1 <= int(args.max_headlines) <= 30):
        raise DeskError("--ahead must be 1-168 hours, --hours 1-72 hours and --max-headlines 1-30")
    back, ahead = 6 * 3600000, int(float(args.ahead) * 3600000)
    T = {"as_of": now, "coins": [], "errors": []}
    try:
        evs = get_macro(net, now, hours_ahead=float(args.ahead))
        T["calendar_source"] = "live calendar feed, cross-checked with the official US schedule"
        if getattr(evs, "feed_error", None):
            raise DeskError(evs.feed_error)
    except DeskError as e:
        evs = merge_official([], now, now - back, now + ahead)
        T["calendar_source"] = ("official US schedule only: the live feed failed (%s), so non-US and second-tier releases are missing"
                                % e)
        T["errors"].append("live calendar: %s" % e)
    groups = []
    for e in evs:
        if not (e.get("impact") == "High" and now - back <= e["t"] <= now + ahead):
            continue
        k = event_kind(e.get("title") or "")
        added = "not in the live feed" in (e.get("source") or "")
        g = next((g for g in groups if abs(g["t"] - e["t"]) <= 60000 and g["country"] == e.get("country")), None)
        if g:
            g["titles"].append(e.get("title"))
            g["official"] = g["official"] or bool(e.get("official"))
            g["live"] = g["live"] or not added
            if added and k not in g["added_kinds"]:
                g["added_kinds"].append(k)
            if k != "OTHER" and k not in g["kinds"]:
                g["kinds"].append(k)
            g["note"] = g["note"] or e.get("note")
            if g["forecast"] is None and k != "OTHER":
                g["forecast"], g["previous"] = e.get("forecast"), e.get("previous")
            continue
        groups.append({"t": e["t"], "kinds": [k] if k != "OTHER" else [], "country": e.get("country"), "titles": [e.get("title")],
                       "forecast": e.get("forecast") if k != "OTHER" else None, "previous": e.get("previous") if k != "OTHER" else None,
                       "official": bool(e.get("official")), "live": not added, "added_kinds": [k] if added else [],
                       "note": e.get("note")})
    after = max(EVENT_FIRST_MINUTE, int(cfg.get("macro_blackout_minutes_after") or 0))
    for g in groups:
        studied = [k for k in g["kinds"] if k in ((stats.get("summary") or {}))]
        g["kind"] = (studied or g["kinds"] or ["OTHER"])[0]
        g["minutes_from_now"] = int(round((g["t"] - now) / 60000.0))
        if g["country"] in ("USD", "ALL"):
            g["no_new_entries"] = [g["t"] - int(cfg["macro_blackout_minutes_before"]) * 60000, g["t"] + after * 60000]
            g["history"] = release_history_line(g["kind"], stats)
            g["news_rules"] = news_rules_line(g["kind"], stats)
    before_ms = int(cfg["macro_blackout_minutes_before"]) * 60000
    for g in groups:
        if "proven edge here" in (g.get("news_rules") or ""):
            later = [{"t": x["t"], "kinds": x["kinds"]} for x in groups if x is not g and x["country"] in ("USD", "ALL")]
            later += [{"t": r["t"], "kinds": [r["kind"]]} for r in official_releases(g["t"] + 1, g["t"] + 24 * 3600000)
                      if all(abs(r["t"] - y["t"]) > 10 * 60000 for y in later)]
            clash = sorted([x for x in later if g["t"] + 60 * 60000 < x["t"] <= g["t"] + 24 * 3600000], key=lambda x: x["t"])
            if clash:
                names = " + ".join(dict.fromkeys(KIND_LABEL.get(k, "another release") for x in clash
                                                 if x["t"] == clash[0]["t"] for k in (x["kinds"] or ["OTHER"])))
                close_by = clash[0]["t"] - before_ms
                held = (close_by - g["t"]) / 3600000.0
                fits = [a for a in (shorter_hold(g["kind"], rule, stats, coin) for coin, rule in proven_news_rules(g["kind"], stats))
                        if a and held >= float(a["hold_hours"])]
                if fits:
                    g["news_rules"] += (" (this time %s falls inside its 24-hour hold: the plan will close by %s, %d minutes before it "
                                        "and %.1f hours after the release; a %g-hour hold also kept the edge in testing)"
                                        % (names, fmt_day(close_by, tz), before_ms // 60000, held,
                                           min(float(a["hold_hours"]) for a in fits)))
                else:
                    g["news_rules"] += " (this time %s falls inside its 24-hour hold, so the rules will block it)" % names
    T["calendar"] = groups
    T["next_main_releases"] = [{"kind": r["kind"], "title": r["title"], "t": r["t"], "note": r.get("note")} for r in next_official(now)
                               if r["t"] > now + ahead]
    cal = load_release_calendar()
    if not cal:
        T["errors"].append("references/release_calendar.json is missing: only the live feed is used")
    elif cal.get("covers_until") and local_day(now, 0) > cal["covers_until"]:
        T["errors"].append("the bundled official schedule ends %s: update the skill for later dates" % cal["covers_until"])
    T["headlines_ok"] = True
    try:
        hl, herr = get_headlines(net, hours=float(args.hours), now=now, symbols=coins)
        T["headlines"] = [{k: h.get(k) for k in ("t", "source", "title", "link", "tags", "risk")}
                          for h in rank_headlines(hl, [base_of(c) for c in coins])[:int(args.max_headlines)]]
        if herr:
            T["errors"].append("headlines: " + "; ".join(herr[:3]))
            T["headlines_ok"] = len(herr) < len(RSS_FEEDS)
    except DeskError as e:
        T["headlines"], T["headlines_ok"] = [], False
        T["errors"].append("headlines: %s" % e)
    # In the first 80 minutes after a release with a proven news rule, that rule's coins are checked first: the signal
    # comes sooner, and a data problem on another coin later in the run cannot hold it up.
    live = [g for g in groups if g["t"] <= now <= g["t"] + 80 * 60000]
    first = [norm_symbol(coin) for g in live for coin, _ in proven_news_rules(g["kind"], stats)]
    first += [norm_symbol(c) for g in live for (c, k, r) in SMALL_RULES if k == g["kind"] and small_rule(c, k, r, cfg)]
    if first:                                           # proven rules first, then small ones: each has 15 minutes to be placed
        coins = sorted([c for c in coins if c in first], key=first.index) + [c for c in coins if c not in first]
    for sym in coins:
        try:
            code, out = _quiet(cmd_brief, ["--json", "brief", sym], home, cfg, args)
            b = json.loads(out)
        except (DeskError, ValueError) as e:
            T["coins"].append({"symbol": sym, "decision": "NOT CHECKED", "why": "no market data: %s" % e, "error": True})
            continue
        except Exception as e:                      # an engine fault on one coin must not hide the others: report it
            T["coins"].append({"symbol": sym, "decision": "NOT CHECKED", "why": "engine error (%s: %s); please report it"
                               % (type(e).__name__, e), "error": True})
            continue
        D = b.get("decision") or {}
        T["coins"].append({"symbol": sym, "price": b.get("price"), "source": b.get("source"), "decision": D.get("decision"),
                           "why": D.get("why"), "blocks": D.get("blocks") or [], "ev_r": D.get("ev_r"), "p0": D.get("p0"),
                           "candidate": b.get("candidate"), "event": b.get("event"), "event_ideas": b.get("event_ideas") or [],
                           "packet": b.get("packet"), "run_dir": b.get("run_dir"), "hold_note": b.get("hold_note"),
                           "blocked_signal": b.get("blocked_signal"), "glance": b.get("glance"), "tier": b.get("tier"),
                           "error": D.get("decision") == "NOT CHECKED"})
    T["signals"] = [c for c in T["coins"] if str(c.get("decision") or "").startswith("SIGNAL")]
    nctx = load_news_context(home, now)
    if nctx:
        T["news_check"] = {"made_at": nctx["made_at"], "valid_until": nctx["valid_until"], "stale": nctx.get("stale"),
                           "source": nctx["source"], "summary": nctx["summary"], "market": nctx["market"],
                           "coins": {base_of(k): v for k, v in nctx["coins"].items()}}
    try:
        T["tracks_resolved"] = auto_resolve_tracks(net, home, now)
    except (DeskError, OSError, KeyError, TypeError, ValueError) as e:
        T["errors"].append("live record: %s" % e)
    T["live_record"] = track_summary(journal_load(home))
    T["coins_checked"] = sum(1 for c in T["coins"] if not c.get("error"))
    watch = []
    for c in T["coins"]:
        if str(c.get("decision") or "").startswith("SIGNAL"):
            continue
        for i in c.get("event_ideas") or []:
            if i.get("status") == "LIVE" and i.get("evidence") in ("UNPROVEN", "UNTESTED", "SUPPORTED"):
                watch.append({"symbol": c["symbol"], "idea": "%s %s (news setup)" % (i["setup"], i["side"]), "evidence": i.get("evidence"),
                              "why_not": first_sentence(i.get("history"))})
        cd, why = c.get("candidate"), c.get("why") or ""
        if cd and cd.get("kind") == "setup" and ("UNPROVEN" in why or "MARKET DRIFT" in why or "MARKET STATE" in why
                                                 or "chart-setup signals are off" in why or "WAIT" == c.get("decision")):
            off = "chart-setup signals are off" in why
            others = list(dict.fromkeys(b.split(":")[0].strip() for b in c.get("blocks") or []
                                        if "chart-setup signals are off" not in b and not b.startswith("history test")))
            watch.append({"symbol": c["symbol"], "idea": "%s %s" % (cd.get("setup"), cd.get("side")),
                          "evidence": "CHART PASS" if off else next((lab for lab in ("UNPROVEN", "MARKET DRIFT", "MARKET STATE")
                                                                     if lab in why), c.get("decision")),
                          "why_not": ("passed its own history test (EV %+.2fR), but chart-setup signals are off: in replays they "
                                      "lost on balance%s" % (c.get("ev_r") or 0.0, ("; also blocked by: " + ", ".join(others)) if others else "")
                                      if off else short_reason(why, blocks=c.get("blocks")))})
    T["watch"] = watch
    if args.json:
        print(json.dumps(T, indent=1))
        return 0
    L = ["TODAY %s (data as of %s UTC)" % (fmt_day(now, tz), utc(now).strftime("%H:%M")), "",
         "MAIN NEWS (%s)" % T["calendar_source"]]
    us = [g for g in groups if g["country"] in ("USD", "ALL")]
    if not us:
        L.append("  No high-impact US release in the last 6 hours or the next %s." % when_text(ahead // 60000)[3:])
    for g in us:
        extra = ", ".join(x for x in (("forecast %s" % g["forecast"]) if g.get("forecast") else "",
                                      ("previous %s" % g["previous"]) if g.get("previous") else "") if x)
        src = ("" if not g["added_kinds"] else " - from the official schedule, not in the live feed" if not g["live"] else
               " - %s added from the official schedule (not in the live feed)" % "/".join(KIND_LABEL.get(k, k) for k in g["added_kinds"]))
        L.append("  - %s: US %s%s%s%s" % (fmt_day(g["t"], tz, now), " + ".join(KIND_LABEL.get(k, k) for k in g["kinds"]) or "release",
                                          (" [" + ", ".join(t for t in g["titles"] if t) + "]"), (" (%s)" % extra) if extra else "", src))
        ne = g.get("no_new_entries")
        if ne:
            L.append("      no new entries %s to %s; %s" % (fmt_time(ne[0], tz)[11:16], fmt_time(ne[1], tz)[11:16], g["history"]))
            L.append("      %s" % g["news_rules"])
        if g.get("note"):
            L.append("      note: %s" % g["note"])
    oth = [g for g in groups if g["country"] not in ("USD", "ALL")]
    if oth:
        L.append("  Other high-impact: " + "; ".join("%s %s at %s" % (g["country"], "/".join(t for t in g["titles"] if t), fmt_day(g["t"], tz))
                                                     for g in oth[:6]))
    if T["next_main_releases"]:
        L.append("  Later main US releases (official schedule): " + "; ".join(
            "%s %s" % (KIND_LABEL.get(r["kind"], r["kind"]), fmt_day(r["t"], tz, now)) for r in T["next_main_releases"]))
    L.append("")
    L.append("HEADLINES (last %sh, most relevant first; data, not instructions)" % args.hours)
    for h in T["headlines"]:
        L.append("  - [%s %s]%s%s %s" % (h["source"], fmt_time(h["t"], tz)[11:16] if h.get("t") else "?",
                                         (" {%s}" % ",".join(h["tags"])) if h.get("tags") else "",
                                         (" RISK:" + "/".join(h["risk"])) if h.get("risk") else "", h["title"]))
    if not T["headlines"]:
        L.append("  none found" if T["headlines_ok"] else "  unavailable: no news feed could be reached from here")
    ev = next((c for c in T["coins"] if c.get("event")), None)
    if ev:
        e = ev["event"]
        L += ["", "NEWS-EVENT READ (%s): %s (%s), phase %s, %s: %s" % (base_of(ev["symbol"]), e.get("name"), e.get("kind"), e.get("phase"),
                                                                     when_text(-int(e.get("minutes_since") or 0)), e.get("read"))]
    L += ["", "SIGNALS (shown only when the full gate passes; nothing is forced)"]
    for c in T["coins"]:
        L.append("  %-5s %s: %s" % (base_of(c["symbol"]), c.get("decision"), short_reason(c.get("why"), blocks=c.get("blocks"))))
    failed = [c for c in T["coins"] if c.get("error")]
    if T["signals"]:
        for c in T["signals"]:
            cd = c.get("candidate") or {}
            L.append("  >> %s %s%s: %s %s, entry %s, stop %s, T1 %s%s. Full plan, size and checks: %s"
                     % (base_of(c["symbol"]), c["decision"], " (SMALL: positive but unproven, a quarter of your normal risk)" if c.get("tier") == "small" else "", cd.get("setup"), cd.get("side"), fp(cd.get("entry")), fp(cd.get("stop")),
                        fp(cd.get("target1")), ((", a market order by %s (stop and target from your fill)" if cd.get("kind") == "event"
                                                 else ", order expires %s") % fmt_time(cd["expires"], tz)) if cd.get("expires") else "",
                        c.get("packet")))
            if c.get("hold_note") and str(c.get("decision") or "").startswith("SIGNAL"):
                L.append("     " + c["hold_note"])
    elif len(failed) == len(T["coins"]):
        L.append("  NOT CHECKED: no exchange data could be reached from here, so this is not a 'no signal' read. Run 'today' on "
                 "your own computer and paste the output, or use chart mode.")
    else:
        L.append("  Genuine signals right now: none%s. No signal is the normal state: the engine signals only when a setup's own "
                 "tested history beat random entries (or ordinary days) after costs and every rule passes%s."
                 % ((" among the %d coins checked (%s could not be checked)" % (len(T["coins"]) - len(failed),
                                                                              ", ".join(base_of(c["symbol"]) for c in failed))) if failed else "",
                    "" if cfg.get("chart_signals") else "; chart-setup signals are off (config chart_signals), so today that means a "
                                                        "proven news rule"))
    if watch:
        L.append("  Watching (NOT signals; the evidence is not there):")
        for w in watch[:8]:
            L.append("    %s %s [%s]: %s" % (base_of(w["symbol"]), w["idea"], w["evidence"], w["why_not"]))
    if T["live_record"]:
        agg = {}
        for t in T["live_record"]:
            g = agg.setdefault(t["kind"], [0, 0.0, 0])
            g[0] += t["resolved"]
            g[1] += t["total_R"] or 0.0
            g[2] += t["open"]
        L += ["", "LIVE RECORD (the engine's own calls, tracked automatically; 'journal track' for each rule): " + "; ".join(
            "%s %d scored%s, %d open" % (k + "s", v[0], (", avg %+.2fR" % (v[1] / v[0])) if v[0] else "", v[2]) for k, v in sorted(agg.items()))]
    if T["errors"]:
        L += ["", "data gaps: " + "; ".join(T["errors"][:6])]
    L += ["", "Run 'brief COIN' for one coin's full numbers, or paste a packet into a Claude chat that has the skill for the "
              "full desk review. You place any order yourself. Not financial advice."]
    _print(L)
    return 0


# ------------------------------------------------------------------ synthetic exchange (selftest and tests)

def synth_bars(seed=11, n=3000, tf="1h", end_ms=None, p0=60000.0):
    rng = random.Random(seed)
    tfm = TF_MIN[tf] * 60000
    end_ms = end_ms or (now_ms() // tfm) * tfm
    start = end_ms - n * tfm
    p, s, drift = p0, 0.006 * math.sqrt(TF_MIN[tf] / 60.0), 0.0
    rows = []
    for k in range(n):
        if rng.random() < 0.01:
            drift = rng.choice([-1, 0, 1]) * 0.0004 * math.sqrt(TF_MIN[tf] / 60.0)
        scale = math.sqrt(TF_MIN[tf] / 60.0)
        s = max(0.0015 * scale, min(0.012 * scale, s * math.exp(rng.gauss(0, 0.07))))
        r = drift + s * rng.gauss(0, 1)
        o = p
        c = p * math.exp(r)
        hi = max(o, c) * math.exp(abs(rng.gauss(0, s * 0.5)))
        lo = min(o, c) * math.exp(-abs(rng.gauss(0, s * 0.5)))
        v = 1000.0 * (1 + abs(r) / s) * rng.uniform(0.6, 1.4)
        tb = v * min(0.95, max(0.05, 0.5 + r / (4 * s)))
        t = start + k * tfm
        rows.append([t, "%.4f" % o, "%.4f" % hi, "%.4f" % lo, "%.4f" % c, "%.3f" % v, t + tfm - 1, "%.2f" % (v * c), 100,
                     "%.3f" % tb, "%.2f" % (tb * c), "0"])
        p = c
    return rows


def _aggregate(rows, tf_ms):
    out, cur = [], None
    for r in rows:
        g = (r[0] // tf_ms) * tf_ms
        o, h, l_, c, v, qv, tb, tbq = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]),
                                       float(r[9]), float(r[10]))
        if cur is None or cur[0] != g:
            if cur is not None:
                out.append(cur)
            cur = [g, o, h, l_, c, v, g + tf_ms - 1, qv, r[8], tb, tbq, "0"]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l_), c
            cur[5] += v
            cur[7] += qv
            cur[9] += tb
            cur[10] += tbq
    if cur is not None:
        out.append(cur)
    return [[x[0], "%.4f" % x[1], "%.4f" % x[2], "%.4f" % x[3], "%.4f" % x[4], "%.3f" % x[5], x[6], "%.2f" % x[7], x[8],
             "%.3f" % x[9], "%.2f" % x[10], "0"] for x in out]


def _split(rows, n):
    """Each 15m bar -> n sub-bars along the path open -> low -> high -> close (or open -> high -> low -> close)
    that re-aggregate to the same OHLC."""
    out = []
    step = 900000 // n
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
            out.append([tt, "%.4f" % a, "%.4f" % hi, "%.4f" % lo, "%.4f" % b, "%.3f" % v, tt + step - 1,
                        "%.2f" % (v * b), 10, "%.3f" % tb, "%.2f" % (tb * b), "0"])
    return out


def _split3(rows):
    return _split(rows, 3)


class SynthExchange(object):
    """A tiny fake of the public endpoints the engine reads, for offline runs.
    All timeframes come from one 15-minute random walk, so they agree."""

    def __init__(self, now, seed=11, fail_hosts=()):
        self.now = now
        self.series = {}
        self.base = {}
        self.seed = seed
        self.fail_hosts = set(fail_hosts)

    def _base(self, sym):
        if sym not in self.base:
            tfm = 15 * 60000
            end = (self.now // tfm + 1) * tfm
            seed = self.seed + sum(ord(ch) for ch in sym)
            p0 = 60000.0 if sym.startswith("BTC") else (3000.0 if sym.startswith("ETH") else 20.0)
            self.base[sym] = synth_bars(seed, 24000, "15m", end, p0)
        return self.base[sym]

    def _series(self, sym, tf):
        k = (sym, tf)
        if k not in self.series:
            base = self._base(sym)
            if tf == "15m":
                rows = base
            elif tf == "5m":
                rows = _split3(base[-9000:])
            elif tf == "1m":
                rows = _split(base[-400:], 15)
            elif TF_MIN[tf] > 15 and TF_MIN[tf] % 15 == 0:
                rows = _aggregate(base, TF_MIN[tf] * 60000)
            else:
                raise DeskError("synthetic exchange has no %s bars" % tf)
            self.series[k] = rows
        return self.series[k]

    def __call__(self, url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        path = u.path
        if u.netloc in self.fail_hosts:
            return None
        if u.netloc == "api.bybit.com":
            if path == "/v5/market/kline":
                inv = dict((v, k) for k, v in BYBIT_TF.items())
                rows = self._series(q["symbol"], inv[q["interval"]])
                lim = int(q.get("limit", 200))
                end = int(q["end"]) if "end" in q else None
                sel = [r for r in rows if end is None or r[0] <= end][-lim:]
                lst = [[str(r[0]), r[1], r[2], r[3], r[4], r[5], r[7]] for r in reversed(sel)]
                return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"list": lst}})
            if path == "/v5/market/tickers":
                last = float(self._series(q["symbol"], "1h")[-1][4])
                return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"list": [
                    {"symbol": q["symbol"], "markPrice": "%.4f" % last, "indexPrice": "%.4f" % (last * 0.9998), "fundingRate": "0.0001",
                     "nextFundingTime": str((self.now // (8 * 3600000) + 1) * 8 * 3600000), "openInterest": "1000", "lastPrice": "%.4f" % last}]}})
            if path == "/v5/market/time":
                return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"timeSecond": str(self.now // 1000)}})
        if u.netloc == "fapi.binance.com":
            if path == "/fapi/v1/klines":
                rows = self._series(q["symbol"], q["interval"])
                lim = int(q.get("limit", 500))
                end = int(q["endTime"]) if "endTime" in q else None
                start = int(q["startTime"]) if "startTime" in q else None
                sel = [r for r in rows if (end is None or r[0] <= end) and (start is None or r[0] >= start)]
                return json.dumps(sel[:lim] if start is not None else sel[-lim:])
            if path == "/fapi/v1/premiumIndex":
                syms = [q["symbol"]] if "symbol" in q else ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT"]
                out = []
                for s in syms:
                    last = float(self._series(s, "1h")[-1][4])
                    out.append({"symbol": s, "markPrice": "%.4f" % last, "indexPrice": "%.4f" % (last * 0.9998),
                                "lastFundingRate": "0.00010000", "interestRate": "0.00010000",
                                "nextFundingTime": (self.now // (8 * 3600000) + 1) * 8 * 3600000, "time": self.now})
                return json.dumps(out[0] if "symbol" in q else out)
            if path == "/fapi/v1/fundingRate":
                lim = int(q.get("limit", 100))
                t0 = (self.now // (8 * 3600000)) * 8 * 3600000
                rng = random.Random(3)
                return json.dumps([{"symbol": q["symbol"], "fundingTime": t0 - k * 8 * 3600000,
                                    "fundingRate": "%.8f" % (0.0001 + rng.gauss(0, 0.00005)), "markPrice": "1"} for k in range(lim)][::-1])
            if path == "/fapi/v1/openInterest":
                return json.dumps({"symbol": q["symbol"], "openInterest": "80000.000", "time": self.now})
            if path == "/futures/data/openInterestHist":
                lim = int(q.get("limit", 30))
                return json.dumps([{"symbol": q["symbol"], "sumOpenInterest": "%.3f" % (80000 + 50 * k), "sumOpenInterestValue": "1",
                                    "timestamp": self.now - (lim - k) * 3600000} for k in range(lim)])
            if path.startswith("/futures/data/"):
                lim = int(q.get("limit", 30))
                key = "buySellRatio" if "taker" in path else "longShortRatio"
                return json.dumps([{"symbol": q["symbol"], key: "%.4f" % (1.0 + 0.01 * (k % 7)), "longAccount": "0.52", "shortAccount": "0.48",
                                    "timestamp": self.now - (lim - k) * 3600000} for k in range(lim)])
            if path == "/fapi/v1/depth":
                last = float(self._series(q["symbol"], "1h")[-1][4])
                return json.dumps({"bids": [["%.4f" % (last * (1 - 0.0001 * k)), "5"] for k in range(1, 101)],
                                   "asks": [["%.4f" % (last * (1 + 0.0001 * k)), "5"] for k in range(1, 101)]})
            if path == "/fapi/v1/ticker/24hr":
                out = []
                for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT"):
                    rows = self._series(s, "1h")
                    last, prev = float(rows[-1][4]), float(rows[-25][4])
                    out.append({"symbol": s, "lastPrice": "%.4f" % last, "priceChangePercent": "%.3f" % ((last / prev - 1) * 100),
                                "quoteVolume": "900000000", "highPrice": "%.4f" % (last * 1.02), "lowPrice": "%.4f" % (last * 0.98)})
                return json.dumps(out)
            if path == "/fapi/v1/exchangeInfo":
                return json.dumps({"symbols": [{"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                                                "baseAsset": s[:-4], "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                                                                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                                                                 {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
                                               for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT")]})
            if path == "/fapi/v1/time":
                return json.dumps({"serverTime": self.now})
        if u.netloc == "api.alternative.me":
            return json.dumps({"data": [{"value": str(40 + k), "value_classification": "Fear", "timestamp": str((self.now - k * DAY_MS) // 1000)} for k in range(8)]})
        if u.netloc == "www.deribit.com":
            return json.dumps({"result": {"data": [[self.now - k * 3600000, 50, 51, 49, 50 + 0.01 * k] for k in range(190)][::-1]}})
        if u.netloc == "nfs.faireconomy.media":
            ev = utc(self.now + 3 * 3600000).astimezone(dt.timezone(dt.timedelta(hours=-4)))
            return json.dumps([{"title": "CPI m/m", "country": "USD", "date": ev.isoformat(), "impact": "High", "forecast": "0.3%", "previous": "0.4%"}])
        if u.netloc == "api.coingecko.com":
            return json.dumps({"data": {"market_cap_percentage": {"btc": 57.1, "eth": 11.9}, "market_cap_change_percentage_24h_usd": -1.2}})
        if u.netloc in ("www.coindesk.com", "cointelegraph.com", "decrypt.co", "www.theblock.co"):
            d = email.utils.format_datetime(utc(self.now - 3600000))
            return ("<?xml version='1.0'?><rss><channel><item><title>Bitcoin holds range as traders eye CPI</title><link>https://example.com/a</link>"
                    "<pubDate>%s</pubDate></item><item><title>Solana ETF flows turn positive</title><link>https://example.com/b</link>"
                    "<pubDate>%s</pubDate></item></channel></rss>" % (d, d))
        return None


def cmd_selftest(args, home, cfg):
    import tempfile
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)
    # liquidation and safe-leverage algebra
    for side in ("long", "short"):
        for lev in (2, 5, 10, 20):
            lp = liq_price(side, 100.0, lev, 0.005)
            d = abs(lp - 100.0) / 100.0
            back = max_safe_leverage(side, d, 0.005)
            check("safe leverage inverts liquidation (%s %dx)" % (side, lev), abs(back - lev) < 1e-6)
    check("long liq below entry", liq_price("long", 100, 10, 0.005) < 100)
    check("short liq above entry", liq_price("short", 100, 10, 0.005) > 100)
    # indicators on known data
    check("ema seed", ema([1, 2, 3, 4, 5], 3)[2] == 2.0)
    check("rsi flat", rsi([1.0] * 30, 14)[-1] == 50.0)
    check("rsi up", rsi([float(i) for i in range(30)], 14)[-1] == 100.0)
    check("wilson", abs(wilson(50, 100)[0] - 0.418) < 0.01)
    # a full offline run against the synthetic exchange
    tmp = tempfile.mkdtemp(prefix="desk-selftest-")
    now = (now_ms() // 3600000) * 3600000 + 20 * 60000
    set_now(str(now))
    try:
        fx = SynthExchange(now)
        net = Net(home=tmp, fetcher=fx)
        c2 = json.loads(json.dumps(DEFAULTS))
        snap, feats, closed, deriv = build_snapshot(net, "BTCUSDT", c2)
        check("snapshot has 1h", "1h" in snap["timeframes"])
        check("snapshot price", snap["price"] and snap["price"] > 0)
        check("snapshot derivatives", snap["derivatives"].get("funding") is not None)
        check("macro event parsed", sum(1 for e in snap["macro"] if e.get("title") == "CPI m/m" and not e.get("source")) == 1)
        p = size_position(c2, "BTCUSDT", "long", snap["price"], snap["price"] * 0.99, [snap["price"] * 1.02],
                          atr_now=snap["timeframes"]["1h"]["atr"], funding_rate=0.0001, hold_hours=8)
        check("risk within budget", p["max_loss_at_stop"] <= c2["equity"] * c2["risk_per_trade_pct"] / 100.0 + 1e-6)
        check("liquidation beyond stop", p["liquidation_est"] < snap["price"] * 0.99)
        check("leverage within cap", 1 <= p["leverage"] <= c2["max_leverage"])
        bars = closed["1h"]
        F = feats["1h"]
        cur = state_at(F, len(bars) - 1)
        cm = cost_model(c2, "BTCUSDT")
        s = analog_samples(bars, F, "long", 1.0, 1.5, 24, ["trend"], cur, None, cm, [], 0.0001, 8.0)
        check("analog samples exist", len(s) > 10)
        check("non-overlapping samples", all(b["i"] > a["i"] + a["held"] - 1 for a, b in zip(s, s[1:])))
        boot = block_bootstrap(bars, "long", bars[-1]["c"], bars[-1]["c"] * 0.99, bars[-1]["c"] * 1.015, 24, paths=500)
        boot2 = block_bootstrap(bars, "long", bars[-1]["c"], bars[-1]["c"] * 0.99, bars[-1]["c"] * 1.015, 24, paths=500)
        check("bootstrap reproducible", boot["p_target_first"] == boot2["p_target_first"])
        check("bootstrap probabilities sum", abs(boot["p_target_first"] + boot["p_stop_first"] + boot["p_neither"] - 1) < 0.01)
        res = run_checks(c2, p, snap, [], now=now)
        check("check returns verdict", res["verdict"] in ("PASS", "BLOCK"))
        check("unproven edge is flagged", any("edge unproven" in w for w in res["warnings"]))
        heads, _ = get_headlines(net, hours=12, now=now, symbols=["BTCUSDT", "SOLUSDT"])
        check("headlines parsed and tagged", heads and any("SOL" in (h.get("tags") or []) for h in heads))
        cal = load_release_calendar()
        check("official release schedule loads", bool(cal.get("releases")))
        r0 = (cal.get("releases") or [{}])[0]
        if r0.get("t"):
            m = merge_official([], r0["t"] - 3600000, r0["t"] - 7200000, r0["t"] + 3600000)
            check("official schedule fills a gap in the live calendar", len(m) == 1 and m[0]["impact"] == "High")
        code, out = _quiet(cmd_today, ["--synthetic", "today", "--coins", "BTC"], tmp, c2)
        check("today runs offline", code == 0 and "MAIN NEWS" in out and "SIGNALS" in out)
    finally:
        set_now(None)
    if failures:
        _print(["SELFTEST FAILED (%d):" % len(failures)] + ["  - " + f for f in failures])
        return 1
    _print(["SELFTEST PASSED: indicators, liquidation algebra, sizing, analogs, bootstrap, rules and parsers all behave."])
    return 0


# ------------------------------------------------------------------ CLI

def build_parser():
    ap = argparse.ArgumentParser(prog="desk.py", description="Number engine for the crypto-futures-desk skill. Never places orders.")
    ap.add_argument("--home", help="desk folder (default $DESK_HOME or ~/.crypto-desk)")
    ap.add_argument("--offline", help="read saved responses from this folder instead of the network")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--synthetic", action="store_true", help="use a built-in fake exchange (demo and tests; never for decisions)")
    ap.add_argument("--now", help="pin the clock (UTC 'YYYY-MM-DD HH:MM' or epoch ms)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("doctor")
    p = sub.add_parser("init")
    p.add_argument("--force", action="store_true")
    p.add_argument("--equity", type=float)
    p.add_argument("--risk-per-trade-pct", dest="risk_per_trade_pct", type=float)
    p.add_argument("--max-leverage", dest="max_leverage", type=int)
    p = sub.add_parser("config", help="show settings, or set one: config set risk_per_trade_pct 0.5")
    csub = p.add_subparsers(dest="ccmd")
    csub.add_parser("show")
    q = csub.add_parser("set")
    q.add_argument("key")
    q.add_argument("value")
    p = sub.add_parser("context")
    p.add_argument("--hours", type=int, default=12)
    p.add_argument("--max-headlines", type=int, default=15)
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("scan")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--min-qv", type=float, default=None)
    p.add_argument("--show", type=int, default=8)
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("snapshot")
    p.add_argument("symbol")
    p.add_argument("--source", choices=["binance", "bybit"], default="binance")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("plan")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--run")
    p.add_argument("--side", required=True, choices=["long", "short"])
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--target", type=float, required=True)
    p.add_argument("--target2", type=float)
    p.add_argument("--target3", type=float)
    p.add_argument("--entry-type", dest="entry_type", choices=["market", "limit", "stop"], default=None,
                   help="default from config (limit: lower fees)")
    p.add_argument("--hold-hours", dest="hold_hours", type=float, default=None,
                   help="expected holding time for funding (default: half the horizon)")
    p.add_argument("--horizon-hours", dest="horizon_hours", type=float, default=None,
                   help="time stop: close by this many hours after entry (default 48h on 4h bars)")
    p.add_argument("--replace", action="store_true", help="overwrite the plan in this run folder instead of starting a new one")
    p.add_argument("--fill-window", dest="fill_window", type=float, default=4.0)
    p.add_argument("--tier", choices=["small"], help="a news rule traded small (sized at small_signal_risk of normal risk)")
    p.add_argument("--equity", type=float)
    p.add_argument("--setup")
    p.add_argument("--atr", type=float, help="ATR of the primary timeframe (chart mode: read it off the DESK table)")
    p.add_argument("--funding", type=float, help="funding rate per interval as a fraction, e.g. 0.0001 = 0.01%%")
    p.add_argument("--funding-interval", dest="funding_interval", type=float, help="funding interval in hours (default 8)")
    p.add_argument("--no-fetch", dest="no_fetch", action="store_true", help="do not touch the network")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("manual", help="chart mode: build a snapshot from values read off the DESK table")
    p.add_argument("symbol")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--atr", type=float, required=True, help="ATR(14) of the primary timeframe")
    p.add_argument("--regime", required=True, help="from the table: TREND UP, TREND DOWN, RANGE, SQUEEZE, HIGH VOL, TRANSITION")
    p.add_argument("--trend", required=True, type=str.lower, choices=["up", "down", "flat"], help="'Trend this' on the 4h table")
    p.add_argument("--htf-trend", dest="htf_trend", required=True, type=str.lower, choices=["up", "down", "flat"], help="'Trend B' (1D)")
    p.add_argument("--range-pos", dest="range_pos", type=float, required=True, help="table 'Range position' as 0..1 (e.g. 32%% -> 0.32)")
    p.add_argument("--bar-time", dest="bar_time", help="table header time 'YYYY-MM-DD HH:MM' UTC; rejects a stale screenshot")
    p.add_argument("--atr-pct", dest="atr_pct", type=float, help="table ATR %% of price; cross-checks the ATR and price you read")
    p.add_argument("--events-checked", dest="events_checked", action="store_true",
                   help="you checked a macro calendar and found no high-impact events in the next 48h")
    p.add_argument("--funding", type=float, default=None, help="funding per interval as a fraction")
    p.add_argument("--funding-interval", dest="funding_interval", type=float, default=8.0)
    p.add_argument("--event", action="append", default=[], help="high-impact event 'NAME@2026-10-21T12:30:00Z' (repeatable)")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("event", help="read the market's reaction to a news release (fake or accepted) and build event setups")
    p.add_argument("symbol")
    p.add_argument("--time", help="release time ISO, e.g. 2026-11-12T13:30:00Z (default: the latest high-impact US release)")
    p.add_argument("--name", help="event name, e.g. 'US CPI'")
    p.add_argument("--kind", choices=["CPI", "FOMC", "NFP", "PCE", "PPI", "GDP", "OTHER"])
    p.add_argument("--run", help="write into this run folder")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("brief", help="one-shot: snapshot, news read, best setup planned, tested and checked, engine-only decision")
    p.add_argument("symbol")
    p = sub.add_parser("verify-news", help="re-run a news rule on Binance perpetual prices with real funding, release by release")
    p.add_argument("symbol")
    p.add_argument("--kind", choices=["CPI", "FOMC", "NFP", "PPI"], default="FOMC")
    p.add_argument("--rule", choices=["sweep_reclaim_fade", "breakout_accept", "momentum_after_60m", "spike_fade_24h"], default="momentum_after_60m")
    p.add_argument("--since", default="2020-01-01")
    p.add_argument("--max", type=int, default=120, help="at most this many of the latest releases")
    p = sub.add_parser("today", help="the day in one read: main news (calendar + headlines), news-event read, a decision per coin")
    p.add_argument("--coins", help="comma list (default: your watchlist)")
    p.add_argument("--hours", type=float, default=12, help="headlines from the last N hours")
    p.add_argument("--ahead", type=float, default=36, help="calendar: hours ahead (6 hours back always)")
    p.add_argument("--max-headlines", dest="max_headlines", type=int, default=8)
    p = sub.add_parser("packet", help="compact briefing for the agents (run folder + context + journal)")
    p.add_argument("--run", default="latest")
    p.add_argument("--context", default="latest", help="context JSON path, 'latest', or 'none'")
    p.add_argument("--max-setups", dest="max_setups", type=int, default=3)
    p = sub.add_parser("simulate")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--run")
    p.add_argument("--side", choices=["long", "short"])
    p.add_argument("--entry", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--target", type=float)
    p.add_argument("--setup")
    p.add_argument("--tf")
    p.add_argument("--bars", type=int, default=4000)
    p.add_argument("--horizon", type=int)
    p.add_argument("--min-samples", dest="min_samples", type=int, default=30)
    p.add_argument("--paths", type=int, default=4000)
    p.add_argument("--block", type=int, default=12)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--slip-stress", dest="slip_stress", type=float, default=0.005)
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--run", default="latest")
    p.add_argument("--refresh", action="store_true", help="re-fetch the snapshot first")
    p.add_argument("--venue-price", dest="venue_price", type=float, help="the price your venue shows right now")
    p = sub.add_parser("journal")
    jsub = p.add_subparsers(dest="jcmd")
    q = jsub.add_parser("add")
    q.add_argument("--run", default="latest")
    q.add_argument("--prob", type=float, required=True, help="adjudicated probability that target 1 comes before the stop")
    q.add_argument("--decision", choices=["TAKE", "PAPER", "SKIP", "WAIT"], required=True)
    q.add_argument("--setup")
    q.add_argument("--notes")
    q.add_argument("--intensity", choices=["quick", "standard", "deep", "chart"], default=None)
    q.add_argument("--fill", type=float, help="the price you actually got (records slippage; stop and targets keep their distance)")
    q = jsub.add_parser("forecast")
    q.add_argument("symbol")
    q.add_argument("--p-up", dest="p_up", type=float, required=True)
    q.add_argument("--hours", type=float, default=24)
    q.add_argument("--ref", type=float)
    q.add_argument("--notes")
    q.add_argument("--intensity", choices=["quick", "standard", "deep", "chart"], default=None)
    q = jsub.add_parser("close")
    q.add_argument("id")
    q.add_argument("--exit", type=float, required=True)
    q.add_argument("--reason", default="manual")
    jsub.add_parser("resolve")
    q = jsub.add_parser("list")
    q.add_argument("--last", type=int, default=20)
    jsub.add_parser("stats")
    jsub.add_parser("track", help="the live record of the engine's own signals, news ideas and chart passes")
    q = jsub.add_parser("agent", help="log the agent desk's own call so it can be compared with the engine's")
    q.add_argument("symbol")
    q.add_argument("--side", choices=["long", "short", "flat"], required=True)
    q.add_argument("--entry", type=float)
    q.add_argument("--stop", type=float)
    q.add_argument("--target", type=float)
    q.add_argument("--horizon-hours", dest="horizon_hours", type=float, default=24.0)
    q.add_argument("--prob", type=float, help="the desk's probability that the target comes before the stop")
    q.add_argument("--made-at", dest="made_at", help="when the call was made ('YYYY-MM-DDTHH:MMZ', an offset like +05:30, or epoch ms; default now)")
    q.add_argument("--summary", help="one or two sentences: why")
    p = sub.add_parser("news", help="the research news check (from Claude): show, set FILE, or clear. It can block or shrink a trade, never create one")
    nsub = p.add_subparsers(dest="ncmd")
    nsub.add_parser("show")
    q = nsub.add_parser("set")
    q.add_argument("file", help="a JSON news check: made_at, valid_until, market {level, reasons}, coins {SOL: {level, reasons}}")
    nsub.add_parser("clear")
    sub.add_parser("selftest")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 1
    DOWN_HOSTS.clear()                       # each run starts fresh; the memory only spans one command
    try:
        if args.now:
            set_now(args.now)
        home = desk_home(args.home)
        try:
            cfg = load_config(home)
        except DeskError:
            if args.cmd not in ("config", "doctor", "init"):
                raise
            cfg = json.loads(json.dumps(DEFAULTS))
        if args.cmd == "scan" and args.min_qv is None:
            args.min_qv = float(cfg["scan_min_quote_volume"])
        fn = {"doctor": cmd_doctor, "init": cmd_init, "context": cmd_context, "scan": cmd_scan, "snapshot": cmd_snapshot,
              "plan": cmd_plan, "simulate": cmd_simulate, "check": cmd_check, "journal": cmd_journal,
              "selftest": cmd_selftest, "manual": cmd_manual, "packet": cmd_packet, "event": cmd_event, "brief": cmd_brief, "today": cmd_today, "verify-news": cmd_verify_news,
              "config": cmd_config, "news": cmd_news}[args.cmd]
        return fn(args, home, cfg)
    except DeskError as e:
        msg = "desk.py: %s\n" % e
        try:
            sys.stderr.write(msg)
        except UnicodeEncodeError:
            sys.stderr.write(msg.encode("ascii", "replace").decode("ascii"))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
