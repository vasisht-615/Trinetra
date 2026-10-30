"""Does the history test measure the same pattern the live detector trades?

The live detector (detect_setups) and the history test's trigger (SETUP_TRIGGERS) are two pieces of code for the same
setup. At every bar of long synthetic 4h histories this compares them: a bar where the trigger fires but the live
detector would not (or the reverse) means the history test's "past cases" are not the trade the engine would take.

    python3 tests/engine/check_setup_consistency.py [--series 6]
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import desk  # noqa: E402

SETUPS = ("trend_pullback", "range_fade", "squeeze_break")       # level_reclaim needs day levels, checked separately below


def bars_of(rows):
    return desk.parse_binance_klines(rows)


def sliced(F, n):
    return {k: (v[:n] if isinstance(v, list) else v) for k, v in F.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", type=int, default=6)
    a = ap.parse_args()
    counts = collections.Counter()
    examples = collections.defaultdict(list)
    for s in range(a.series):
        end = desk.parse_when("2026-10-01T00:00Z", "x")
        rows4 = desk.synth_bars(100 + s, 2200, "4h", end, 100.0)
        rows1d = desk._aggregate(rows4, desk.DAY_MS)
        P_b, H_b = bars_of(rows4), bars_of(rows1d)
        P, H = desk.tf_features(P_b), desk.tf_features(H_b)
        P["htf_trend"], P["htf_adx"] = desk.align_htf(P_b, H_b)             # what cmd_simulate gives the triggers
        for i in range(400, len(P_b)):
            t_close = P_b[i]["T"]
            j = max(k for k in range(len(H_b)) if H_b[k]["T"] <= t_close) if H_b[0]["T"] <= t_close else None
            if j is None or j < 60:
                continue
            Pi, Hi = sliced(P, i + 1), sliced(H, j + 1)
            live = {(c["setup"], c["side"]) for c in desk.detect_setups(Pi, P_b[:i + 1], Hi, H_b[:j + 1], cfg=desk.DEFAULTS)}
            for setup in SETUPS:
                for side in ("long", "short"):
                    trig = desk.SETUP_TRIGGERS[(setup, side)](P, P_b, i)
                    hit = (setup, side) in live
                    key = (setup, side, "both" if (trig and hit) else "trigger_only" if trig else "live_only" if hit else None)
                    if key[2]:
                        counts[key] += 1
                        if key[2] != "both" and len(examples[key]) < 3:
                            examples[key].append((s, i))
    print("%-16s %-6s %8s %13s %10s" % ("setup", "side", "both", "trigger only", "live only"))
    for setup in SETUPS:
        for side in ("long", "short"):
            print("%-16s %-6s %8d %13d %10d" % (setup, side, counts[(setup, side, "both")], counts[(setup, side, "trigger_only")],
                                               counts[(setup, side, "live_only")]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
