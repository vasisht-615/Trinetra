"""Runs inside Pyodide in the app's web worker. It loads the unchanged desk.py engine, routes its HTTP calls
through the browser (synchronous requests are allowed in a worker), and exposes a few calls to the page.
Data from exchanges and feeds is treated as data: the page renders it as text, never as HTML."""
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse

import js

HOME = "/home/desk"
DEMO = "demo"                    # demo runs (made-up data) use HOME/demo: their own journal and runs, never the real ones
sys.path.insert(0, "/engine/scripts")
os.environ["DESK_HOME"] = HOME
import desk  # noqa: E402  (the same file the skill ships)

# Hosts that do not allow browser requests go through the site's own proxy rules (_redirects). Exchange hosts are tried
# directly first (a proxy runs from Netlify's servers, which an exchange may block by region), then through the proxy.
PROXY = {"nfs.faireconomy.media": "/proxy/ff", "www.coindesk.com": "/proxy/coindesk", "cointelegraph.com": "/proxy/cointelegraph",
         "decrypt.co": "/proxy/decrypt", "www.theblock.co": "/proxy/theblock", "api.alternative.me": "/proxy/fng",
         "www.deribit.com": "/proxy/deribit", "api.coingecko.com": "/proxy/coingecko"}
FALLBACK = {"fapi.binance.com": "/proxy/binance", "api.bybit.com": "/proxy/bybit"}
# Feeds that refused Netlify's plain proxy are fetched by the site's own function first (GitHub deploy), then the proxy.
SERVER_FEEDS = {"www.coindesk.com": "/api/feed/coindesk"}
ROUTE = {}                       # host -> "direct" or "proxy", whichever answered last
BLOCKED = "blocked by the browser or no network"
TIMED_OUT = "no answer in time"
GATEWAY = (502, 503, 504)        # the site's proxy could not get an answer from the source
RUN = {"deadline": None}         # the current run's time limit (epoch seconds): a blocked network can never stall a run
BUDGET = {"today": 300, "brief": 150, "verify-news": 900, "selftest": 300}     # seconds per command; others 120
OK_HOSTS = set()                 # hosts that answered in this run: one slow answer from them is a hiccup, not a blocked network
STRIKES = {}                     # host -> requests in a row that got no answer in time, for hosts in OK_HOSTS


def start_run(seconds):
    """Each run starts fresh, as each command line run does: no host is still marked down from an earlier run."""
    desk.DOWN_HOSTS.clear()
    OK_HOSTS.clear()
    STRIKES.clear()
    RUN["deadline"] = time.time() + seconds


def _left():
    return None if RUN["deadline"] is None else RUN["deadline"] - time.time()


def _xhr(url, timeout):
    x = js.XMLHttpRequest.new()
    x.open("GET", url, False)
    x.timeout = int(max(1.0, float(timeout)) * 1000)      # allowed for synchronous requests in a worker
    x.send(None)
    return int(x.status), str(x.responseText or "")


def candidates(url):
    u = urllib.parse.urlparse(url)
    tail = u.path + ("?" + u.query if u.query else "")
    h = u.netloc
    if h in PROXY:
        return ([SERVER_FEEDS[h] + tail] if h in SERVER_FEEDS else []) + [PROXY[h] + tail]
    if h in FALLBACK:
        both = [url, FALLBACK[h] + tail]
        return both[::-1] if ROUTE.get(h) == "proxy" else both
    return [url]


def browser_fetch(self, url, timeout=15):
    """The browser version of desk.Net._fetch: up to 3 rounds over the routes (an exchange directly, then through the
    site's proxy; a feed through the proxy only), with the engine's pacing. It gives up early when retrying cannot help:
    every attempt timed out, the network or region is blocked, or the run's time limit is used up. A host that gave no
    usable answer is skipped for the rest of the run, and a rate limit (HTTP 429) stops calls to that host at once, so
    the app never pushes an exchange into banning this address."""
    host = desk.host_of(url)
    down = desk.DOWN_HOSTS.get(host)
    if down and time.time() - down[0] < desk.DOWN_TTL:
        raise desk.DeskError("could not reach %s (%s; skipped for the rest of this run)" % (host, down[1]))
    wait = self.pause - (time.time() - self._last)
    if wait > 0:
        time.sleep(wait)
    last, answered = None, False
    for attempt in range(3):
        soft, heard, tries, timeouts = [], False, 0, 0
        for cand in candidates(url):
            left = _left()
            if left is not None and left < 2:
                last = "this run's time limit was used up"
                break
            proxied = cand.startswith(("/proxy/", "/api/feed/"))
            self._last = time.time()
            self.calls += 1
            tries += 1
            try:
                status, text = _xhr(cand, timeout if left is None else min(timeout, left - 1))
            except Exception as e:                            # blocked by the browser (CORS), offline, or timed out
                status, text = 0, ""
                if "TimeoutError" in str(e):
                    timeouts += 1
                    last = TIMED_OUT
                else:
                    last = BLOCKED
            if status == 200:
                ROUTE[host] = "proxy" if proxied else "direct"
                OK_HOSTS.add(host)
                STRIKES[host] = 0
                return text
            if status == 0:
                if last != TIMED_OUT:
                    last = BLOCKED
                soft.append("direct" if not proxied else "net")
                continue
            if proxied and status in GATEWAY + (404,):
                last = "the site's proxy got no answer (HTTP %d)" % status
                if status != 404:
                    soft.append("gateway")
                continue
            heard = True                                      # something answered: the network itself works
            if status == 418:
                desk.DOWN_HOSTS[host] = (time.time(), "HTTP 418 ban")
                raise desk.DeskError("%s banned this address for too many requests (HTTP 418); wait a few minutes" % host)
            if status in (403, 451):
                last = "HTTP %d (this network or region is blocked)" % status
                continue                                      # a block is not a usable answer: the host is marked down below
            if status == 429:
                desk.DOWN_HOSTS[host] = (time.time(), "rate limited (HTTP 429)")
                raise desk.DeskError("%s is rate-limiting this address (HTTP 429); no more calls to it in this run, so it "
                                     "does not turn into a ban" % host)
            answered = True
            if status >= 500:
                last = "HTTP %d" % status
                soft.append("busy")
                continue
            raise desk.DeskError("%s answered HTTP %d %s" % (host, status, text[:120].strip()))
        if heard and "direct" in soft:
            # the network works (the proxy answered), so the browser itself refuses the direct call: stop trying it
            ROUTE[host] = "proxy"
            soft = [s for s in soft if s != "direct"]
        if not soft or not tries or attempt == 2:
            break                                             # nothing a retry could fix
        if timeouts == tries and not (host in OK_HOSTS and attempt == 0):
            break                                             # every attempt timed out: retried once, and only for a host
        left = _left()                                        # that answered earlier in this run (a hiccup, not a block)
        if left is not None and left < 4:
            break
        time.sleep(1.5 * (attempt + 1))
    if not answered:
        if last == TIMED_OUT and host in OK_HOSTS:
            # it answered earlier in this run: skip it only after two requests in a row got no answer in time, so one
            # slow answer cannot take a working exchange away from the rest of the run (and from the coins checked later)
            STRIKES[host] = STRIKES.get(host, 0) + 1
            if STRIKES[host] >= 2:
                desk.DOWN_HOSTS[host] = (time.time(), last)
        else:
            desk.DOWN_HOSTS[host] = (time.time(), last)
    raise desk.DeskError("could not reach %s (%s)" % (host, last))


desk.Net._fetch = browser_fetch


def demo_home():
    """Demo runs get their own folder with a fresh copy of the real settings, so made-up prices never reach the real
    journal or the engine's live record."""
    d = os.path.join(HOME, DEMO)
    os.makedirs(d, exist_ok=True)
    src, dst = os.path.join(HOME, "config.json"), os.path.join(d, "config.json")
    if os.path.exists(src):
        shutil.copyfile(src, dst)
    elif os.path.exists(dst):
        os.remove(dst)
    return d


def run(argv_json):
    """Run one engine command; returns {code, out, err} as JSON text."""
    argv = [str(a) for a in json.loads(argv_json)]
    home = demo_home() if "--synthetic" in argv else HOME
    cmd = next((a for a in argv if not a.startswith("--")), "")
    start_run(BUDGET.get(cmd, 120))
    out, err = io.StringIO(), io.StringIO()
    code = 1
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = desk.main(["--home", home] + argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except Exception as e:                               # an engine fault must reach the page, not kill the worker
            err.write("engine error (%s): %s" % (type(e).__name__, e))
            code = 1
    return json.dumps({"code": code, "out": out.getvalue(), "err": err.getvalue()})


def read_text(path):
    """Read a file the engine wrote (a packet), only inside the data folder."""
    p = os.path.realpath(path)
    if not p.startswith(HOME + "/"):
        raise ValueError("outside the data folder")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def chart_bars(sym, tf, n, synthetic):
    """Closed and live bars for the chart, through the same network layer and fallbacks as the engine."""
    sym = desk.norm_symbol(sym)
    if tf not in ("15m", "1h", "4h", "1d"):
        raise ValueError("the chart shows 15m, 1h, 4h or 1d bars")
    start_run(45)
    net = desk.Net(home=HOME, fetcher=desk.SynthExchange(desk.now_ms()) if synthetic else None)
    try:
        bars = desk.get_klines(net, sym, tf, int(n))
    except desk.DeskError:
        bars = desk.get_klines(net, sym, tf, int(n), source="bybit")
    return json.dumps([[b["t"] // 1000, b["o"], b["h"], b["l"], b["c"], b.get("v") or 0.0] for b in bars])


CHECKS = [("Binance futures (prices, funding, open interest)", "https://fapi.binance.com/fapi/v1/time"),
          ("Bybit (fallback prices)", "https://api.bybit.com/v5/market/time"),
          ("Economic calendar (ForexFactory feed)", desk.FF_CALENDAR),
          ("Fear and greed index", "https://api.alternative.me/fng/?limit=1&format=json"),
          ("Deribit (implied volatility)", "https://www.deribit.com/api/v2/public/get_time"),
          ("CoinGecko (dominance)", "https://api.coingecko.com/api/v3/ping")] + [("Headlines: " + n, u) for n, u in desk.RSS_FEEDS]


def connection_check():
    """Try every data source once from this browser, and say which way it answered."""
    res = []
    for name, url in CHECKS:
        host = desk.host_of(url)
        start_run(25)                                       # each source gets its own short time limit
        net = desk.Net(home=None)
        t0 = time.time()
        try:
            body = net.text(url)
            ok, why = True, "%s, %d bytes" % (ROUTE.get(host, "direct"), len(body))
        except desk.DeskError as e:
            ok, why = False, str(e)
        res.append({"name": name, "host": host, "ok": ok, "detail": why, "ms": int((time.time() - t0) * 1000)})
    desk.DOWN_HOSTS.clear()
    return json.dumps(res)


def housekeeping(keep_runs=40, cache_days=2):
    """Keep the browser's storage small: drop old run folders and stale cache files."""
    for runs, keep in ((os.path.join(HOME, "runs"), keep_runs), (os.path.join(HOME, DEMO, "runs"), 5)):
        if os.path.isdir(runs):
            names = sorted(n for n in os.listdir(runs) if os.path.isdir(os.path.join(runs, n)))
            for n in names[:-keep]:
                shutil.rmtree(os.path.join(runs, n), ignore_errors=True)
    cache = os.path.join(HOME, "cache")
    if os.path.isdir(cache):
        cut = time.time() - cache_days * 86400
        for n in os.listdir(cache):
            p = os.path.join(cache, n)
            try:
                if os.path.getmtime(p) < cut:
                    os.remove(p)
            except OSError:
                pass
    return "ok"


def export_data():
    out = {"exported": desk.now_ms(), "engine": desk.VERSION, "files": {}}
    for name in ("config.json", "journal.jsonl"):
        p = os.path.join(HOME, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                out["files"][name] = fh.read()
    return json.dumps(out)


def _stored():
    p = os.path.join(HOME, "config.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        user = json.load(fh)
    return user if isinstance(user, dict) else {}


def _check_whole(user):
    """Run the engine's own start-up check (every setting and the ones that depend on each other, e.g. a capital
    currency needs its exchange rate) on a scratch copy; raises desk.DeskError with the engine's reason."""
    tmp = tempfile.mkdtemp(prefix="desk-check-")
    try:
        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(user, fh)
        desk.load_config(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def settings():
    """The settings as stored (capital in its own currency, not converted), over the defaults, for the form."""
    vals = desk.deep_merge(json.loads(json.dumps(desk.DEFAULTS)), _stored())
    problem = None
    try:
        desk.load_config(HOME)
    except desk.DeskError as e:
        problem = str(e)
    return json.dumps({"values": vals, "problem": problem, "stored": os.path.exists(os.path.join(HOME, "config.json"))})


def save_settings(changes_json):
    """Save several settings at once: each through the engine's check, then the whole set through its start-up check.
    Nothing is written unless all of it passes."""
    changes = json.loads(changes_json)
    if not isinstance(changes, dict):
        raise ValueError("settings must be an object")
    user = _stored()
    for k, v in changes.items():
        user[k] = desk.check_setting(k, v, user)
    _check_whole(user)
    os.makedirs(HOME, exist_ok=True)
    desk.write_json(os.path.join(HOME, "config.json"), user)
    _mark("config")
    return json.dumps(sorted(changes))


def repair_settings():
    """Reset only the stored settings that stop the engine from starting (including ones the form does not show), so the
    app can always be put right from the Settings tab. Returns the names that were reset."""
    user, reset = _stored(), []
    for k in list(user):
        if k in desk.CONFIG_SCHEMA:
            try:
                desk.check_setting(k, user[k], user)
            except desk.DeskError:
                reset.append(k)
                user.pop(k)
    for group in (("equity_currency", "usdt_fx", "usdt_inr", "display_currency"), None):
        try:
            _check_whole(user)
            break
        except desk.DeskError:
            if group is None:
                reset.append("everything (back to the defaults)")
                user = {}
            else:
                reset += [k for k in group if k in user]
                user = {k: v for k, v in user.items() if k not in group}
    os.makedirs(HOME, exist_ok=True)
    desk.write_json(os.path.join(HOME, "config.json"), user)
    if reset:
        _mark("config")
    return json.dumps(reset)


def import_data(text):
    d = json.loads(text)
    files = d.get("files") if isinstance(d, dict) else None
    if not isinstance(files, dict):
        raise ValueError("not a DESK backup file")
    if isinstance(files.get("config.json"), str):
        # The engine's start-up check on a scratch copy, so a bad backup is refused here instead of locking the app.
        user = json.loads(files["config.json"])
        if not isinstance(user, dict):
            raise ValueError("config.json in the backup is not a settings object")
        _check_whole(user)
    done = []
    for name in ("config.json", "journal.jsonl"):
        if name in files and isinstance(files[name], str) and len(files[name]) < 5000000:
            os.makedirs(HOME, exist_ok=True)
            with open(os.path.join(HOME, name), "w", encoding="utf-8") as fh:
                fh.write(files[name])
            done.append(name)
    if "config.json" in done:
        _mark("config")
    return json.dumps(done)


def _ics_time(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_text(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line):
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(cur)
            cur = ""
        cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def fed_nights_ics():
    """A calendar file with every upcoming Fed decision and a reminder to run DESK an hour later (the SOL rule's read)."""
    now = desk.now_ms()
    rows = [r for r in desk.load_release_calendar().get("releases") or [] if r["kind"] == "FOMC" and r["t"] > now]
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//crypto-futures-desk//Fed nights//EN", "CALSCALE:GREGORIAN"]
    for r in rows:
        t_read = r["t"] + 61 * 60000
        for uid, start, minutes, title, text in (
                ("fomc-%s" % r["date"], r["t"], 30, "US Fed rate decision (FOMC)",
                 "No new entries from 60 minutes before to 30 minutes after. Source: federalreserve.gov."),
                ("desk-%s" % r["date"], t_read, 15, "DESK: run Today now (SOL Fed rule, 15-minute window)",
                 "Open the app and run Today. A signal shows only if every rule passes. Not financial advice.")):
            L += ["BEGIN:VEVENT", "UID:%s@crypto-futures-desk" % uid, "DTSTAMP:%s" % _ics_time(now), "DTSTART:%s" % _ics_time(start),
                  "DTEND:%s" % _ics_time(start + minutes * 60000), "SUMMARY:%s" % _ics_text(title), "DESCRIPTION:%s" % _ics_text(text),
                  "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:%s" % _ics_text(title), "TRIGGER:-PT5M", "END:VALARM", "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(x) for x in L) + "\r\n"


def upcoming(days=45):
    """The main US releases from the bundled official schedule over the next `days` days."""
    now = desk.now_ms()
    rows = [r for r in desk.load_release_calendar().get("releases") or [] if now - 6 * 3600000 <= r["t"] <= now + int(days) * 86400000]
    return json.dumps([{"kind": r["kind"], "title": r.get("title"), "t": r["t"], "note": r.get("note"),
                        "label": desk.KIND_LABEL.get(r["kind"], r["kind"])} for r in rows])


# ---------------------------------------------------------------- background checks, records and the diagnostic log

LOG = "applog.jsonl"
LOG_KEEP = 2000


def health(symbols_json):
    """Quick checks next to each Today run: this device's clock against the exchange (signal windows are 15 minutes), and
    Binance's price against Bybit's for each coin (a large gap means one source is wrong or the market is breaking)."""
    syms = [desk.norm_symbol(s) for s in json.loads(symbols_json)][:12]
    start_run(40)
    net = desk.Net(home=None)
    out = {"t": desk.now_ms(), "clock_skew_s": None, "binance": None, "bybit": None, "prices": []}
    try:
        t0 = time.time()
        st = json.loads(net.text(desk.BINANCE + "/fapi/v1/time"))
        out["clock_skew_s"] = round((t0 + time.time()) / 2 - float(st["serverTime"]) / 1000.0, 1)
        out["binance"] = "ok"
    except (desk.DeskError, ValueError, KeyError, TypeError) as e:
        out["binance"] = str(e)[:200]
    marks = {}
    try:
        marks = desk.get_premium(net)
    except desk.DeskError as e:
        out["binance"] = out["binance"] if out["binance"] != "ok" else str(e)[:200]
    for s in syms:
        row = {"symbol": s, "binance": (marks.get(s) or {}).get("mark")}
        try:
            row["bybit"] = desk.get_bybit_ticker(net, s)["mark"]
            out["bybit"] = "ok"
        except (desk.DeskError, KeyError, TypeError) as e:
            out["bybit"] = out["bybit"] or str(e)[:200]
        if row.get("binance") and row.get("bybit"):
            row["gap_pct"] = round(abs(row["binance"] / row["bybit"] - 1) * 100, 3)
        out["prices"].append(row)
    return json.dumps(out)


def usdt_rate(cur):
    """Today's price of 1 USDT in a currency (CoinGecko), to fill in the setup form."""
    cur = str(cur).strip().lower()
    if not (len(cur) == 3 and cur.isalpha()):
        raise ValueError("a three-letter currency code, such as INR")
    start_run(20)
    d = json.loads(desk.Net(home=None).text("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=" + cur))
    rate = float(d["tether"][cur])
    if not rate > 0:
        raise ValueError("no rate for " + cur.upper())
    return json.dumps({"currency": cur.upper(), "rate": rate})


def log_event(entry_json):
    """Append one entry to the app's diagnostic log (runs, data problems, health results, app errors, your actions)."""
    e = json.loads(entry_json)
    if not isinstance(e, dict):
        raise ValueError("a log entry is an object")
    e = {"t": int(e.get("t") or desk.now_ms()), "kind": str(e.get("kind") or "app")[:20], "msg": str(e.get("msg") or "")[:400],
         "data": e.get("data") if isinstance(e.get("data"), (dict, list)) else None}
    p = os.path.join(HOME, LOG)
    os.makedirs(HOME, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, default=str)[:4000] + "\n")
    if os.path.getsize(p) > 900000:                                # keep the newest entries only
        with open(p, encoding="utf-8") as fh:
            lines = fh.readlines()[-LOG_KEEP:]
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return "ok"


def read_log(n=200):
    p = os.path.join(HOME, LOG)
    if not os.path.exists(p):
        return "[]"
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh.readlines()[-int(n):]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return json.dumps(out)


def records():
    """Everything the Record screen shows: the engine's and the agent desk's scored calls, your own trades, and the
    summaries, straight from the journal."""
    rows = desk.journal_load(HOME)
    keep = ("id", "ts", "symbol", "side", "entry", "stop", "targets", "status", "outcome", "decision", "track_kind", "setup",
            "evidence", "prob", "notes", "planned_entry", "fill", "slippage_pct", "horizon_hours", "engine_version", "mode", "qty",
            "close_by", "blocked_by", "release_kind")
    slim = [{k: r.get(k) for k in keep if k in r} for r in rows if r.get("type") in ("trade", None)][-400:]
    try:
        stats = desk.journal_stats(rows)
    except Exception:                                          # an odd old row must not hide the record
        stats = None
    cfg = desk.load_config(HOME)
    return json.dumps({"rows": slim, "track": desk.track_summary(rows), "stats": stats, "probation": desk.probation_status(rows, cfg)},
                      default=str)


def _agent_lines(s):
    """Each DESK-AGENT-CALL {json} in the pasted text, as dicts (a reply may hold one line per coin)."""
    out, i = [], s.find("DESK-AGENT-CALL")
    while i >= 0:
        j = s.find("{", i)
        nxt = s.find("DESK-AGENT-CALL", i + 15)
        if j < 0 or (0 <= nxt < j):
            raise ValueError("this DESK-AGENT-CALL line is not in the app's format (it needs the {...} part). The Claude chat "
                             "used an older copy of the skill: re-upload crypto-futures-desk.zip (0.7.0 or newer) in Claude, then ask again")
        depth, end = 0, None
        for k in range(j, len(s)):
            depth += {"{": 1, "}": -1}.get(s[k], 0)
            if depth == 0:
                end = k + 1
                break
        if end is None:
            raise ValueError("a DESK-AGENT-CALL line is cut off: copy the whole line")
        c = json.loads(s[j:end])
        if not isinstance(c, dict):
            raise ValueError("a DESK-AGENT-CALL line must hold one {...} object")
        out.append(c)
        i = s.find("DESK-AGENT-CALL", end)
    return out


def agent_call(text):
    """Log the agent desk's calls from the lines Claude prints: DESK-AGENT-CALL {json}, one per coin. The engine checks
    every value; nothing is logged unless every line passes the format check."""
    s = str(text)
    if "DESK-AGENT-CALL" not in s:
        raise ValueError("paste the line that starts with DESK-AGENT-CALL")
    argvs = []
    for c in _agent_lines(s):
        side = str(c.get("side") or "").lower()
        if side not in ("long", "short", "flat"):
            raise ValueError("side must be long, short or flat (%s)" % (c.get("symbol") or "?"))
        argv = ["journal", "agent", str(c.get("symbol") or ""), "--side", side, "--summary", str(c.get("summary") or "")[:500]]
        for k, flag in (("entry", "--entry"), ("stop", "--stop"), ("target", "--target"), ("horizon_hours", "--horizon-hours"), ("prob", "--prob")):
            if c.get(k) is not None and not (side == "flat" and k in ("entry", "stop", "target")):
                argv += [flag, repr(float(c[k]))]
        if c.get("made_at"):
            argv += ["--made-at", str(c["made_at"]).strip()[:40]]                # the engine needs its Z or offset
        argvs.append(argv)
    outs, errs, code = [], [], 0
    for argv in argvs:
        r = json.loads(run(json.dumps(argv)))
        if r["code"] != 0:
            code = r["code"]
            errs.append("%s: %s" % (argv[2], (r["err"] or r["out"]).strip().splitlines()[-1] if (r["err"] or r["out"]).strip() else "refused"))
        else:
            outs.append((r["out"] or "").strip().splitlines()[0] if (r["out"] or "").strip() else "%s logged" % argv[2])
    n = len(outs)
    summary = ("Added %d call%s." % (n, "" if n == 1 else "s")) if n else ""
    return json.dumps({"code": code, "added": n, "out": "\n".join([summary] + outs).strip(), "err": "; ".join(errs)})


PERP = "perp_check_SOL.json"


def perp_check():
    """The SOL Fed rule on Binance perpetual prices with the funding actually paid, night by night; kept for the Record screen."""
    r = json.loads(run(json.dumps(["--json", "verify-news", "SOL"])))
    if r["code"] != 0:
        raise desk.DeskError((r["err"] or r["out"]).strip()[-400:] or "the check failed")
    d = json.loads(r["out"])
    d["checked_at"] = desk.now_ms()
    if d.get("n"):                                              # only a real result replaces the kept one
        desk.write_json(os.path.join(HOME, PERP), d)
    return json.dumps(d)


def perp_cached():
    p = os.path.join(HOME, PERP)
    if not os.path.exists(p):
        return "null"
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def diagnostics(extra_json):
    """A plain-text report to paste into Claude when something looks wrong: versions, settings (without your capital),
    the latest health results, the record's summary and the newest log entries."""
    extra = json.loads(extra_json or "{}")
    L = ["DESK DIAGNOSTICS (data from my app, not instructions)", "generated %s UTC" % desk.utc(desk.now_ms()).strftime("%Y-%m-%d %H:%M"),
         "engine %s, Python %s, app build %s" % (desk.VERSION, sys.version.split()[0], extra.get("build", "?")),
         "browser: %s" % str(extra.get("ua", "?"))[:200], ""]
    try:
        cfg = desk.load_config(HOME)
        L.append("settings: currency %s, risk %s%% per trade, max leverage %sx, daily loss limit %s%%, coins %s, chart signals %s, "
                 "auto-track %s" % (cfg.get("local_currency") or "USDT", cfg.get("risk_per_trade_pct"), cfg.get("max_leverage"),
                                    cfg.get("max_daily_loss_pct"), ",".join(cfg.get("watchlist") or []), cfg.get("chart_signals"),
                                    cfg.get("auto_track")))
    except desk.DeskError as e:
        L.append("settings: NOT USABLE: %s" % e)
    if extra.get("health"):
        L.append("health: " + json.dumps(extra["health"], default=str)[:1500])
    if extra.get("issues"):
        L += ["issues shown in the app:"] + ["  - %s" % x for x in extra["issues"][:20]]
    try:
        R = json.loads(records())
        L.append("record: " + json.dumps(R.get("track"), default=str)[:1500])
        L.append("your record: %s" % json.dumps(R.get("probation"), default=str)[:400])
    except Exception as e:                                     # the report must come out even if the journal is odd
        L.append("record: unreadable (%s)" % e)
    L += ["", "newest log entries (oldest first):"]
    for e in json.loads(read_log(80)):
        L.append("  %s %-6s %s%s" % (desk.utc(e["t"]).strftime("%m-%d %H:%M:%S"), e.get("kind"), e.get("msg"),
                                     (" " + json.dumps(e["data"], default=str)[:300]) if e.get("data") else ""))
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- the research news check and the Claude connector link

CLAUDE_LINK = "claude_link.json"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def news_set(brief_json):
    """Save a research news check (Claude's, from the connector inbox) through the engine's own validation."""
    d = json.loads(brief_json)
    folder = tempfile.mkdtemp(prefix="news-")
    try:
        p = os.path.join(folder, "brief.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        r = json.loads(run(json.dumps(["news", "set", p])))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if r["code"] != 0:
        raise desk.DeskError((r["err"] or r["out"]).strip().splitlines()[-1] if (r["err"] or r["out"]).strip() else "refused")
    _mark("news")
    return news_get()


def news_get():
    return json.dumps(desk.load_news_context(HOME))


def news_clear():
    p = os.path.join(HOME, desk.NEWS_FILE)
    if os.path.exists(p):
        os.remove(p)
    _mark("news")
    return "ok"


def claude_link_get():
    p = os.path.join(HOME, CLAUDE_LINK)
    if not os.path.exists(p):
        return "null"
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def claude_link_set(token):
    if not TOKEN_RE.match(str(token or "")):
        raise ValueError("a connector token is 32 to 128 letters, digits, '-' or '_'")
    os.makedirs(HOME, exist_ok=True)
    desk.write_json(os.path.join(HOME, CLAUDE_LINK), {"token": str(token), "at": desk.now_ms()})
    _mark("claude")
    return claude_link_get()


def claude_link_clear():
    p = os.path.join(HOME, CLAUDE_LINK)
    if os.path.exists(p):
        os.remove(p)
    _mark("claude")
    return "ok"


# ---------------------------------------------------------------- sync (the app encrypts this before it leaves the device)

SYNC_META = "sync_meta.json"
RANK = {"open": 0, "filled": 1, "expired": 2, "flat": 2, "cancelled": 2, "resolved": 3}


def _meta():
    p = os.path.join(HOME, SYNC_META)
    try:
        with open(p, encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _mark(part):
    m = _meta()
    m[part + "_at"] = desk.now_ms()
    os.makedirs(HOME, exist_ok=True)
    desk.write_json(os.path.join(HOME, SYNC_META), m)


def _row_key(r):
    # tracked calls are identified by what they are (two devices tracking the same signal make one row), the rest by id
    if r.get("decision") == "TRACK" and r.get("track_key"):
        return "T|" + str(r["track_key"])
    return "I|" + str(r.get("id") or "%s|%s|%s" % (r.get("ts"), r.get("symbol"), r.get("decision")))


def _better(a, b):
    """Of two copies of one journal row, the one further along (scored beats open), then the fuller one."""
    ra, rb = RANK.get(a.get("status"), 1), RANK.get(b.get("status"), 1)
    if ra != rb:
        return a if ra > rb else b
    return a if len(json.dumps(a, sort_keys=True)) >= len(json.dumps(b, sort_keys=True)) else b


def sync_export():
    """Everything worth having on every device: settings, the journal (all three scorecards and your trades) and the SOL
    Fed history. Runs, caches and the log stay per device."""
    m = _meta()
    out = {"v": 1, "at": desk.now_ms(), "config": None, "config_at": m.get("config_at", 0), "journal": desk.journal_load(HOME),
           "perp": None, "news": None, "news_at": m.get("news_at", 0), "claude": json.loads(claude_link_get()), "claude_at": m.get("claude_at", 0)}
    np_ = os.path.join(HOME, desk.NEWS_FILE)
    if os.path.exists(np_):
        with open(np_, encoding="utf-8") as fh:
            out["news"] = json.load(fh)
    p = os.path.join(HOME, "config.json")
    if os.path.exists(p):
        out["config"] = _stored()
    pp = os.path.join(HOME, PERP)
    if os.path.exists(pp):
        with open(pp, encoding="utf-8") as fh:
            out["perp"] = json.load(fh)
    return json.dumps(out, default=str)


def sync_merge(remote_json, prefer_remote=False):
    """Merge another device's copy into this one. Journal rows are united (never lost); for a row on both, the copy that
    is further along wins. Settings: the newer change wins, and only if the engine accepts them. Returns what changed and
    the merged copy to send back."""
    R = json.loads(remote_json)
    if not isinstance(R, dict) or R.get("v") != 1:
        raise ValueError("not a DESK sync copy")
    changed = []
    local = desk.journal_load(HOME)
    rows = {}
    for r in local + [x for x in (R.get("journal") or []) if isinstance(x, dict) and x.get("type") in ("trade", "forecast") and x.get("ts")]:
        k = _row_key(r)
        rows[k] = _better(rows[k], r) if k in rows else r
    merged = sorted(rows.values(), key=lambda r: (r.get("ts") or 0, str(r.get("id"))))
    if [json.dumps(r, sort_keys=True) for r in merged] != [json.dumps(r, sort_keys=True) for r in local]:
        desk.journal_save(HOME, merged)
        changed.append("record")
    m = _meta()
    if R.get("config") and (prefer_remote or (R.get("config_at") or 0) > m.get("config_at", 0)):
        try:
            _check_whole(R["config"])
            desk.write_json(os.path.join(HOME, "config.json"), R["config"])
            m["config_at"] = max(R.get("config_at") or 0, 1)
            desk.write_json(os.path.join(HOME, SYNC_META), m)
            changed.append("settings")
        except desk.DeskError:
            pass                                               # another device's settings the engine refuses are not copied
    pp = os.path.join(HOME, PERP)
    if isinstance(R.get("perp"), dict) and R["perp"].get("n"):
        cur = None
        if os.path.exists(pp):
            with open(pp, encoding="utf-8") as fh:
                cur = json.load(fh)
        if not cur or (R["perp"].get("checked_at") or 0) > (cur.get("checked_at") or 0):
            desk.write_json(os.path.join(HOME, PERP), R["perp"])
            changed.append("Fed history")
    m = _meta()
    if (R.get("news_at") or 0) > m.get("news_at", 0):                 # the newer news check (or its clearing) wins
        np_ = os.path.join(HOME, desk.NEWS_FILE)
        if R.get("news"):
            try:
                desk.write_json(np_, desk.normalise_news(R["news"], now=R["news"].get("made_at")))
                changed.append("news check")
            except desk.DeskError:
                pass
        elif os.path.exists(np_):
            os.remove(np_)
            changed.append("news check")
        m["news_at"] = R["news_at"]
        desk.write_json(os.path.join(HOME, SYNC_META), m)
    if (R.get("claude_at") or 0) > m.get("claude_at", 0):              # the Claude link set (or removed) on another device
        cp = os.path.join(HOME, CLAUDE_LINK)
        c = R.get("claude")
        if c and TOKEN_RE.match(str(c.get("token") or "")):
            desk.write_json(cp, {"token": c["token"], "at": c.get("at") or R["claude_at"]})
            changed.append("Claude link")
        elif not c and os.path.exists(cp):
            os.remove(cp)
            changed.append("Claude link")
        m["claude_at"] = R["claude_at"]
        desk.write_json(os.path.join(HOME, SYNC_META), m)
    return json.dumps({"changed": changed, "merged": json.loads(sync_export())}, default=str)


def info():
    return json.dumps({"engine": desk.VERSION, "python": sys.version.split()[0],
                       "stats": bool(desk.load_event_stats()), "calendar": (desk.load_release_calendar() or {}).get("covers_until")})
