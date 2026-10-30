"""Chart setups (intraday and swing): the decision rule, the history test's fidelity, and full-pipeline sweeps.

    python3 -m unittest discover -s tests/engine -p 'test_*.py'
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_setup_consistency as C  # noqa: E402
import harness as H  # noqa: E402
from harness import desk  # noqa: E402

H.fast_mode()
QUIET_DAYS = ["2026-11-18T09:00Z", "2026-11-20T15:00Z", "2026-12-01T03:00Z", "2026-12-17T11:00Z", "2027-01-05T20:00Z",
              "2027-02-03T06:00Z"]          # no big US release within a day either side


def decide(verdict, ev_rows=None, blocks=(), ev=0.2, p0=0.45, net_r=1.6):
    plan = {"side": "long", "targets": [{"net_r": net_r, "breakeven_winrate": 0.4}]}
    an = {"target_first_rate": p0, "expectancy_r": ev}
    base = {"target_first_rate": 0.35, "expectancy_r": -0.1}
    return desk.engine_decision(plan, {"verdict": verdict, "analogs": an, "random_entry_baseline": base}, {"blocks": list(blocks)}, ev_rows)


class DecisionRule(unittest.TestCase):
    POS = "POSITIVE (historical): comparable past cases made money after costs (90% interval +0.05R to +0.40R). Better than random entries"

    def test_needs_positive_and_better_than_random(self):
        self.assertEqual(decide(self.POS)["decision"], "SIGNAL LONG")
        self.assertEqual(decide("POSITIVE (historical): ... Not distinguishable from random entries")["decision"], "NO TRADE")
        self.assertEqual(decide("MARKET DRIFT, NOT THE SETUP: ...")["decision"], "NO TRADE")
        self.assertEqual(decide("UNPROVEN: average +0.30R ...")["decision"], "NO TRADE")
        self.assertEqual(decide("NEGATIVE: ...")["decision"], "NO TRADE")
        self.assertEqual(decide("INSUFFICIENT HISTORY: ...")["decision"], "NO TRADE")

    def test_needs_ev_of_a_tenth_of_r(self):
        self.assertEqual(decide(self.POS, ev=0.09)["decision"], "NO TRADE")
        self.assertEqual(decide(self.POS, ev=0.10)["decision"], "SIGNAL LONG")

    def test_random_base_never_gets_credit(self):
        d = decide("NO EDGE: ...", ev=0.5)
        self.assertLessEqual(d["ev_r"], 0.0)

    def test_blocks(self):
        self.assertEqual(decide(self.POS, blocks=["macro blackout: CPI (+20 min)"])["decision"], "WAIT")
        self.assertEqual(decide(self.POS, blocks=["daily loss limit hit (-25.00): done for today"])["decision"], "NO TRADE")
        self.assertEqual(decide(self.POS, blocks=[desk.CHART_SIGNALS_OFF])["decision"], "NO TRADE")


class HistoryFidelity(unittest.TestCase):
    def test_trigger_covers_exactly_what_live_trades(self):
        end = desk.parse_when("2026-10-01T00:00Z", "x")
        rows4 = desk.synth_bars(101, 1600, "4h", end, 100.0)
        P_b, H_b = C.bars_of(rows4), C.bars_of(desk._aggregate(rows4, desk.DAY_MS))
        P, Hf = desk.tf_features(P_b), desk.tf_features(H_b)
        P["htf_trend"], P["htf_adx"] = desk.align_htf(P_b, H_b)
        orig = desk._stop_ok
        desk._stop_ok = lambda *a: True            # the history test applies the plan's own stop to every past case
        try:
            for i in range(400, len(P_b), 3):
                j = max(k for k in range(len(H_b)) if H_b[k]["T"] <= P_b[i]["T"])
                live = {(c["setup"], c["side"]) for c in desk.detect_setups(C.sliced(P, i + 1), P_b[:i + 1], C.sliced(Hf, j + 1),
                                                                             H_b[:j + 1], cfg=desk.DEFAULTS)}
                for setup in C.SETUPS:
                    for side in ("long", "short"):
                        self.assertEqual(desk.SETUP_TRIGGERS[(setup, side)](P, P_b, i), (setup, side) in live, (setup, side, i))
        finally:
            desk._stop_ok = orig


def sweep(chart_signals):
    rows = []
    for when in QUIET_DAYS:
        now = desk.parse_when(when, "x")
        home = tempfile.mkdtemp(prefix="desk-chart-")
        if chart_signals:
            H.run(["config", "set", "chart_signals", "true"], now, H.ScenarioExchange(now, now - 20 * H.DAY), home=home)
        for coin in H.COINS:
            ex = H.ScenarioExchange(now, now - 20 * H.DAY, "flat", seed=int(now // 3600000) % 997 + len(coin))
            code, res = H.run(["brief", coin], now, ex, home=home)
            sim = {}
            if isinstance(res, dict) and res.get("run_dir") and os.path.exists(os.path.join(res["run_dir"], "simulate.json")):
                sim = json.load(open(os.path.join(res["run_dir"], "simulate.json")))
            rows.append((when, coin, code, res, sim))
    return rows


class PipelineSweep(unittest.TestCase):
    def test_off_never_signals_and_tests_every_candidate(self):
        seen = 0
        for when, coin, code, res, sim in sweep(False):
            self.assertEqual(code, 0, (when, coin, res))
            self.assertNotIn("SIGNAL", str((res.get("decision") or {}).get("decision")), (when, coin))
            c = res.get("candidate") or {}
            if c.get("kind") == "setup":
                seen += 1
                self.assertTrue(sim.get("verdict"), "every planned chart setup gets a history test")
                if c["setup"] in ("trend_pullback", "range_fade"):
                    self.assertEqual(sim.get("setup_trigger_htf"), "1d", "the history test filters by the daily trend like live")
        print("\n  chart candidates planned and tested: %d of %d coin-days" % (seen, len(QUIET_DAYS) * len(H.COINS)))

    def test_on_signals_only_with_a_proven_history(self):
        for when, coin, code, res, sim in sweep(True):
            D = res.get("decision") or {}
            if str(D.get("decision")).startswith("SIGNAL"):
                v = sim.get("verdict") or ""
                self.assertTrue(v.startswith("POSITIVE") and "Better than random" in v, v)
                self.assertGreaterEqual(D.get("ev_r") or 0, 0.10)
                p = json.load(open(os.path.join(res["run_dir"], "plan.json")))
                self.assertGreaterEqual(p["targets"][0]["net_r"], 1.5)


if __name__ == "__main__":
    unittest.main()


class HeldBackReason(unittest.TestCase):
    """A chart setup that the rules hold back (extreme volatility, mid-range, a release) is named in the coin's reason,
    instead of "no tradeable setup right now", which read as if the market had nothing in it (live, 30 Oct 2026)."""

    def test_reason_names_the_setup_and_what_blocked_it(self):
        gate = "extreme volatility regime (ATR in the top 5% of the last 200 bars)"
        orig = desk.build_snapshot

        def with_held(net, sym, cfg, **kw):
            out = orig(net, sym, cfg, **kw)
            out[0]["setups"] = [{"setup": "trend_pullback", "side": "short", "entry": 97.28, "stop": 103.61, "target1": 87.13, "target2": 80.81,
                               "risk_atr": 2.84, "r_to_t1": 1.6, "score": 65, "reasons": [], "conflicts": [], "gates": [gate], "tradeable": False}]
            return out
        desk.build_snapshot = with_held
        try:
            now = desk.parse_when(QUIET_DAYS[0], "now")
            code, res = H.run(["brief", "SOL"], now, H.ScenarioExchange(now, now - 30 * 86400000, "flat"))
        finally:
            desk.build_snapshot = orig
        self.assertEqual(code, 0)
        why = res["decision"]["why"]
        self.assertEqual(res["decision"]["decision"], "NO TRADE")
        self.assertIn("trend_pullback short", why)
        self.assertIn(gate, why)
        self.assertNotIn("%%", why)
        self.assertIn("chart-setup signals are off", why)
