"""Edge cases around the one proven rule (SOL after a Fed decision), run through the full engine pipeline.

    python3 -m unittest discover -s tests/engine -p 'test_*.py'

Each test sets up one situation (a news check, a losing day, an outage, a calendar problem...) and checks that the engine
does what it is designed to do. The baseline is SOL falling 0.8% in the first hour after the 9 Dec 2026 Fed decision,
read at minute 61, which signals a short when nothing else is in the way.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import desk  # noqa: E402

H.fast_mode()
FED = H.official("FOMC", desk.parse_when("2026-11-01T00:00Z", "x"))       # 9 Dec 2026, 14:00 ET = 00:30 IST on the 10th
T = FED["t"]


def home_with(config=None, rows=None, news=None, now=None):
    home = tempfile.mkdtemp(prefix="desk-edge-")
    for k, v in (config or {}).items():
        code, out = H.run(["config", "set", k, str(v)], now or T, H.ScenarioExchange(now or T, T), home=home)
        assert code == 0, out
    if rows:
        desk.journal_save(home, rows)
    if news is not None:
        desk.write_json(os.path.join(home, desk.NEWS_FILE), desk.normalise_news(news, now or T))
    return home


def sol(minutes=61, reaction="down", home=None, **kw):
    ex = H.ScenarioExchange(T + int(minutes * H.MIN), T, reaction, calendar=[("FOMC", T)] + list(kw.pop("calendar_extra", ())), **kw)
    code, res = H.run(["brief", "SOLUSDT"], T + int(minutes * H.MIN), ex, home=home)
    return code, res


def plan_of(res):
    return json.load(open(os.path.join(res["run_dir"], "plan.json")))


def check_of(res):
    return json.load(open(os.path.join(res["run_dir"], "check.json")))


def decision(res):
    return ((res or {}).get("decision") or {}).get("decision")


def trade(ts, decision="PAPER", status="resolved", R=-1.0, pnl=-5.0, exit_t=None, setup="trend_pullback", symbol="BTCUSDT"):
    r = {"type": "trade", "id": "t%d" % ts, "ts": ts, "symbol": symbol, "side": "long", "entry": 100.0, "stop": 98.0, "qty": 1.0,
         "targets": [{"price": 103.0}], "decision": decision, "status": status, "setup": setup}
    if status == "resolved":
        r["outcome"] = {"R": R, "pnl": pnl, "exit_t": exit_t or ts + 30 * H.MIN, "result": "stop" if R < 0 else "target"}
    return r


NEWS = {"made_at": T - 2 * H.HOUR, "source": "test", "summary": "test",
        "market": {"level": "extreme", "reasons": [{"text": "Major exchange hacked", "confirmed": True, "sources": ["https://example.com/a"]}]}}


class Baseline(unittest.TestCase):
    def test_signal_short_with_tested_plan(self):
        code, res = sol()
        self.assertEqual(code, 0)
        self.assertEqual(decision(res), "SIGNAL SHORT")
        p = plan_of(res)
        self.assertGreaterEqual(abs(p["stop"] / p["entry"] - 1), 0.025 - 1e-4)
        self.assertLessEqual(p["max_loss_at_stop"], 2.5 * 1.06)               # 0.5% of 1000 USDT, halved while unproven

    def test_minute_boundaries(self):
        self.assertNotEqual(decision(sol(59)[1]), "SIGNAL SHORT")             # minute 60 not closed yet
        self.assertEqual(decision(sol(60)[1]), "SIGNAL SHORT")                 # the 60th minute closed at +60:00
        self.assertEqual(decision(sol(75)[1]), "SIGNAL SHORT")                 # last minute of the 15-minute window
        self.assertNotEqual(decision(sol(76)[1]), "SIGNAL SHORT")             # expired: do not chase


class NewsCheck(unittest.TestCase):
    def test_extreme_blocks_and_is_shown_as_paused(self):
        code, res = sol(home=home_with(news=NEWS))
        self.assertEqual(decision(res), "NO TRADE")
        self.assertTrue(res.get("blocked_signal"), "a tested rule stopped by the news check must be reported as paused")
        self.assertTrue(any("news check" in b for b in res["blocked_signal"]["by"]))

    def test_elevated_halves_the_size(self):
        full = plan_of(sol()[1])["max_loss_at_stop"]
        code, res = sol(home=home_with(news=dict(NEWS, market={"level": "elevated", "reasons": NEWS["market"]["reasons"]})))
        self.assertEqual(decision(res), "SIGNAL SHORT")
        self.assertAlmostEqual(plan_of(res)["max_loss_at_stop"], full / 2, delta=full * 0.06)

    def test_stale_check_is_ignored(self):
        old = dict(NEWS, made_at=T - 30 * H.HOUR)
        self.assertEqual(decision(sol(home=home_with(news=old, now=T - 30 * H.HOUR + H.MIN))[1]), "SIGNAL SHORT")

    def test_unconfirmed_reasons_do_not_count(self):
        weak = dict(NEWS, market={"level": "extreme", "reasons": [{"text": "rumour", "confirmed": False, "sources": ["https://x.com/y"]}]})
        self.assertEqual(decision(sol(home=home_with(news=weak))[1]), "SIGNAL SHORT")

    def test_another_coins_warning_does_not_block_sol(self):
        eth = dict(NEWS, market={"level": "normal"}, coins={"ETH": NEWS["market"]})
        self.assertEqual(decision(sol(home=home_with(news=eth))[1]), "SIGNAL SHORT")


class RiskLimits(unittest.TestCase):
    def test_daily_loss_limit(self):
        rows = [trade(T + 10 * H.MIN, pnl=-25.0, exit_t=T + 40 * H.MIN)]             # limit: 2% of 1000 USDT
        code, res = sol(home=home_with(rows=rows))
        self.assertEqual(decision(res), "NO TRADE")
        self.assertTrue(any("daily loss" in b for b in res["decision"]["blocks"]))

    def test_max_trades_per_day(self):
        rows = [trade(T + k * H.MIN, R=1.0, pnl=1.0) for k in (1, 2, 3)]
        res = sol(home=home_with(rows=rows))[1]
        self.assertTrue(any("trades already today" in b for b in res["decision"]["blocks"]))

    def test_open_positions_limit(self):
        rows = [trade(T - 3 * H.HOUR + k, decision="TAKE", status="open") for k in (1, 2)]
        res = sol(home=home_with(rows=rows))[1]
        self.assertTrue(any("positions already open" in b for b in res["decision"]["blocks"]))

    def test_cooldown_after_losses(self):
        rows = [trade(T + k * H.MIN, R=-1.0, pnl=-2.0, exit_t=T + (k + 5) * H.MIN) for k in (1, 2)]
        res = sol(home=home_with(rows=rows))[1]
        self.assertTrue(any("losses in a row" in b for b in res["decision"]["blocks"]))

    def test_rule_suspended_after_a_losing_record(self):
        rows = [trade(T - (40 - k) * 24 * H.HOUR, R=-0.5, pnl=-1.0, setup="momentum_after_60m", symbol="SOLUSDT") for k in range(10)]
        res = sol(home=home_with(rows=rows))[1]
        self.assertTrue(any("your record" in b for b in res["decision"]["blocks"]))

    def test_leverage_cap(self):
        res = sol(home=home_with(config={"max_leverage": 2}))[1]
        self.assertLessEqual(plan_of(res)["leverage"], 2)

    def test_inr_capital(self):
        res = sol(home=home_with(config={"equity": 100000, "equity_currency": "INR", "usdt_inr": 89}))[1]
        self.assertEqual(decision(res), "SIGNAL SHORT")
        self.assertAlmostEqual(plan_of(res)["max_loss_at_stop"], 100000 / 89.0 * 0.005 * 0.5, delta=0.2)


class DataAndCalendar(unittest.TestCase):
    def test_binance_down_is_not_checked_not_no_trade(self):
        code, res = sol(fail_hosts=("fapi.binance.com",))
        self.assertEqual(res.get("source"), "bybit")
        self.assertEqual(decision(res), "NOT CHECKED")

    def test_live_calendar_down_still_trades_the_tested_rule(self):
        self.assertEqual(decision(sol(fail_hosts=("nfs.faireconomy.media",))[1]), "SIGNAL SHORT")

    def test_missing_minutes_are_filled(self):
        class Gappy(H.ScenarioExchange):
            def _series(self, sym, tf):
                rows = H.ScenarioExchange._series(self, sym, tf)
                if tf != "1m":
                    return rows
                return [r for r in rows if not (T + 20 * H.MIN <= r[0] < T + 25 * H.MIN)]
        ex = Gappy(T + 61 * H.MIN, T, "down", calendar=[("FOMC", T)])
        code, res = H.run(["brief", "SOLUSDT"], T + 61 * H.MIN, ex)
        self.assertEqual(decision(res), "SIGNAL SHORT")

    def test_summer_time_fed_decision(self):
        june = H.official("FOMC", desk.parse_when("2027-06-01T00:00Z", "x"))
        self.assertEqual(desk.utc(june["t"]).hour, 18)                           # 14:00 EDT
        ex = H.ScenarioExchange(june["t"] + 61 * H.MIN, june["t"], "up", calendar=[("FOMC", june["t"])])
        code, res = H.run(["brief", "SOLUSDT"], june["t"] + 61 * H.MIN, ex)
        self.assertEqual(decision(res), "SIGNAL LONG")

    def test_release_too_close_blocks_the_hold(self):
        res = sol(calendar_extra=[("PCE", T + 8 * H.HOUR)])[1]                    # a big release 8 h later: under the 12 h hold
        self.assertNotIn("SIGNAL", str(decision(res)))
        self.assertTrue(res.get("blocked_signal"), "the fired rule must be reported as paused, with the reason")

    def test_calendar_entry_that_is_not_an_official_fed_decision(self):
        fake = T + 7 * 24 * H.HOUR                                                # a 'FOMC Statement' a week later: not official
        ex = H.ScenarioExchange(fake + 61 * H.MIN, fake, "down", calendar=[("FOMC", fake)])
        code, res = H.run(["brief", "SOLUSDT"], fake + 61 * H.MIN, ex)
        self.assertNotIn("SIGNAL", str(decision(res)))

    def test_high_volatility_is_a_warning_for_the_tested_rule(self):
        code, res = sol(vol=8.0)
        self.assertEqual(decision(res), "SIGNAL SHORT")

    def test_quiet_market_is_a_warning_for_the_tested_rule(self):
        code, res = sol(vol=0.2)
        self.assertEqual(decision(res), "SIGNAL SHORT")
        self.assertTrue(any("ATR" in w for w in check_of(res)["warnings"]))

    def test_tiny_price_coin(self):
        ex = H.ScenarioExchange(T + 61 * H.MIN, T, "down", calendar=[("FOMC", T)], prices={"PEPE": 0.00001})
        code, res = H.run(["brief", "PEPEUSDT"], T + 61 * H.MIN, ex)
        self.assertEqual(code, 0, res)
        self.assertNotIn("SIGNAL", str(decision(res)))


class WholeDay(unittest.TestCase):
    def test_today_puts_the_fed_rule_coins_first_and_signals_only_them(self):
        ex = H.ScenarioExchange(T + 61 * H.MIN, T, "down", calendar=[("FOMC", T)])
        code, res = H.run(["today"], T + 61 * H.MIN, ex)
        self.assertEqual(code, 0, res)
        # SOL's proven rule and ETH's small one, checked first, and nothing else
        self.assertEqual([c["symbol"] for c in res["signals"]], ["SOLUSDT", "ETHUSDT"])
        self.assertEqual([c["symbol"] for c in res["coins"][:2]], ["SOLUSDT", "ETHUSDT"])
        self.assertEqual([c.get("tier") for c in res["signals"]], [None, "small"])

    def test_one_broken_coin_does_not_hide_the_others(self):
        class NoFoo(H.ScenarioExchange):
            def __call__(self, url):
                return None if "FOOUSDT" in url else H.ScenarioExchange.__call__(self, url)
        ex = NoFoo(T + 61 * H.MIN, T, "down", calendar=[("FOMC", T)])
        code, res = H.run(["today", "--coins", "FOO,SOL"], T + 61 * H.MIN, ex)
        self.assertEqual(code, 0, res)
        by = {c["symbol"]: c for c in res["coins"]}
        self.assertEqual(by["FOOUSDT"]["decision"], "NOT CHECKED")
        self.assertEqual(by["SOLUSDT"]["decision"], "SIGNAL SHORT")

    def test_a_signal_is_tracked_once(self):
        home = tempfile.mkdtemp(prefix="desk-edge-")
        for m in (61, 63):
            ex = H.ScenarioExchange(T + m * H.MIN, T, "down", calendar=[("FOMC", T)])
            H.run(["today", "--coins", "SOL"], T + m * H.MIN, ex, home=home)
        rows = [r for r in desk.journal_load(home) if r.get("decision") == "TRACK" and r.get("track_kind") == "signal"]
        self.assertEqual(len(rows), 1, rows)


def eth(minutes=61, reaction="down", home=None):
    ex = H.ScenarioExchange(T + int(minutes * H.MIN), T, reaction, calendar=[("FOMC", T)])
    return H.run(["brief", "ETHUSDT"], T + int(minutes * H.MIN), ex, home=home)


class SmallSignals(unittest.TestCase):
    """ETH's Fed rule is traded small: positive on its history and on Binance perpetuals, not proven."""
    QUARTER = 1000 * 0.005 * 0.25          # the default config: 1,000 USDT, 0.5% a trade, a quarter of it

    def test_eth_signals_small_at_a_quarter_of_normal_risk(self):
        code, res = eth()
        self.assertEqual(code, 0, res)
        self.assertEqual(decision(res), "SIGNAL SHORT")
        self.assertEqual(res.get("tier"), "small")
        p = plan_of(res)
        self.assertEqual(p.get("tier"), "small")
        self.assertAlmostEqual(p["max_loss_at_stop"], self.QUARTER, delta=self.QUARTER * 0.06)
        self.assertGreaterEqual(p["stop_distance_pct"], 2.0 - 0.01)          # ETH's tested 2% floor
        self.assertIn("small signal", res["decision"]["why"])

    def test_switched_off_means_no_trade(self):
        code, res = eth(home=home_with(config={"small_signals": "false"}))
        self.assertEqual(decision(res), "NO TRADE")
        self.assertFalse(res.get("tier"))

    def test_elevated_news_halves_it_again(self):
        code, res = eth(home=home_with(news=dict(NEWS, market={"level": "elevated", "reasons": NEWS["market"]["reasons"]})))
        self.assertEqual(decision(res), "SIGNAL SHORT")
        self.assertAlmostEqual(plan_of(res)["max_loss_at_stop"], self.QUARTER / 2, delta=self.QUARTER * 0.04)

    def test_extreme_news_pauses_it_and_says_so(self):
        code, res = eth(home=home_with(news=NEWS))
        self.assertEqual(decision(res), "NO TRADE")
        self.assertTrue(res.get("blocked_signal"))
        self.assertEqual(res["blocked_signal"].get("tier"), "small")

    def test_no_signal_before_the_hour_or_after_the_window(self):
        for m in (59, 76, 300):
            self.assertNotEqual(str(decision(eth(minutes=m)[1])), "SIGNAL SHORT", m)

    def test_tracked_apart_and_never_proves_the_edge(self):
        home = home_with()
        eth(home=home)
        kinds = [r.get("track_kind") for r in desk.journal_load(home) if r.get("decision") == "TRACK"]
        self.assertIn("small signal", kinds)
        self.assertNotIn("signal", kinds)
        # 60 winning small trades logged by hand: the edge status still counts none of them
        rows = []
        for i in range(60):
            r = trade(T - (90 - i) * H.HOUR, decision="TAKE", R=1.3, pnl=2.0, symbol="ETHUSDT", setup="momentum_after_60m")
            r["tier"] = "small"
            rows.append(r)
        st = desk.probation_status(rows, desk.DEFAULTS)
        self.assertEqual(st["status"], "UNPROVEN")
        self.assertEqual(st["n"], 0)

    def test_a_logged_small_trade_keeps_its_tier(self):
        home = home_with()
        code, res = eth(home=home)
        ex = H.ScenarioExchange(T + 62 * H.MIN, T, "down", calendar=[("FOMC", T)])
        code, out = H.run(["journal", "add", "--run", res["run_dir"], "--prob", "0.5", "--decision", "TAKE", "--setup", "momentum_after_60m",
                           "--intensity", "quick", "--fill", str(plan_of(res)["entry"])], T + 62 * H.MIN, ex, home=home)
        self.assertEqual(code, 0, out)
        took = [r for r in desk.journal_load(home) if r.get("decision") == "TAKE"]
        self.assertEqual([r.get("tier") for r in took], ["small"])

    def test_a_small_account_is_sized_up_to_the_exchange_minimum_within_half_risk(self):
        # 5,000 INR at 1%: a quarter is an ETH order under the 5 USDT minimum; the smallest size that clears it risks less than half
        home = home_with(config={"equity": "5000", "equity_currency": "INR", "usdt_fx": "95.08", "risk_per_trade_pct": "1", "max_leverage": "10"})
        code, res = eth(home=home)
        self.assertEqual(decision(res), "SIGNAL SHORT", res.get("decision"))
        p = plan_of(res)
        self.assertGreaterEqual(p["notional"], 5.0)
        self.assertTrue(0.25 < p["min_order_lift"] <= 0.5, p.get("min_order_lift"))
        self.assertLessEqual(p["max_loss_at_stop"], 5000 / 95.08 * 0.01 * 0.5 * 1.02)
        self.assertTrue(any("exchange minimum" in n for n in p["notes"]))

    def test_too_small_even_at_half_risk_is_blocked_and_says_what_to_change(self):
        home = home_with(config={"equity": "5000", "equity_currency": "INR", "usdt_fx": "95.08", "risk_per_trade_pct": "0.25", "max_leverage": "10"})
        code, res = eth(home=home)
        self.assertEqual(decision(res), "NO TRADE")
        self.assertTrue(any("exchange minimum" in b for b in res["decision"]["blocks"]))
        self.assertTrue(any("raise your capital or risk per trade" in n for n in plan_of(res)["notes"]))
        self.assertEqual((res.get("blocked_signal") or {}).get("tier"), "small")

    def test_other_coins_never_signal_small(self):
        for coin in ("BTCUSDT", "XRPUSDT", "LINKUSDT"):
            ex = H.ScenarioExchange(T + 61 * H.MIN, T, "down", calendar=[("FOMC", T)])
            code, res = H.run(["brief", coin], T + 61 * H.MIN, ex)
            self.assertEqual(decision(res), "NO TRADE", coin)
            self.assertFalse(res.get("tier"), coin)


if __name__ == "__main__":
    unittest.main()
