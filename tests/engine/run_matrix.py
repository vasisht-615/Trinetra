"""The news-day scenario matrix: every coin x release kind x reaction x moment, through the full engine pipeline.

    python3 tests/engine/run_matrix.py [--kinds FOMC,CPI] [--out results.jsonl]

Each row records what the engine decided. The invariants below are what the engine is designed to do; any row that
breaks one is printed as a violation and the script exits 1.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402

KINDS = ("FOMC", "CPI", "NFP", "PPI", "PCE", "GDP")
REACTIONS = ("flat", "up", "down", "small_up", "whipsaw", "fake_up", "spike_up")
MOMENTS = (-90, -30, 10, 31, 61, 70, 80, 300)
MOVE_AT_60 = {"flat": 0.0, "up": 0.008, "down": -0.008, "small_up": 0.002, "whipsaw": 0.001, "fake_up": -0.005, "spike_up": 0.030}


SMALL = {"ETH"}            # ETH's Fed rule is traded small (positive but unproven): a quarter of normal risk
FLOOR = {"SOL": 0.025, "ETH": 0.02}


def expected_signal(coin, kind, reaction, m):
    """Only SOL's Fed rule is proven, and ETH's is traded small: each signals from minute 61 to 75 when the first hour
    moved more than 0.3%. Nothing else ever signals."""
    if coin in ("SOL", "ETH") and kind == "FOMC" and 61 <= m <= 75 and abs(MOVE_AT_60[reaction]) > 0.003:
        return "SIGNAL LONG" if MOVE_AT_60[reaction] > 0 else "SIGNAL SHORT"
    return None


def check_signal(res, rel, now, coin="SOL"):
    """Plan sanity for a SIGNAL: the tested stop and targets, the order window, the shortened hold, and the size (half
    while the record is new; a quarter, labelled small, for a small rule)."""
    bad = []
    with open(os.path.join(res["run_dir"], "plan.json")) as fh:
        p = json.load(fh)
    e, s = float(p["entry"]), float(p["stop"])
    dist = abs(e - s) / e
    t1 = float(p["targets"][0]["price"])
    small = coin in SMALL
    if dist < FLOOR[coin] - 1e-4:                              # prices are rounded to 6 significant digits
        bad.append("stop %.3f%% is inside %s's tested %.1f%% floor" % (dist * 100, coin, FLOOR[coin] * 100))
    tiers = (res.get("tier"), p.get("tier"), (res.get("decision") or {}).get("tier"))
    if any(small != (t == "small") for t in tiers):
        bad.append("tier (brief, plan, decision) is %r, expected %s" % (tiers, "small" if small else "none"))
    if abs(abs(t1 - e) / abs(e - s) - 1.5) > 1e-3:
        bad.append("first target is %.3fR, not the tested 1.5R" % (abs(t1 - e) / abs(e - s)))
    if not (rel["t"] + 12 * H.HOUR <= p["close_by"] <= rel["t"] + 24 * H.HOUR):
        bad.append("close_by %s is outside 12-24 h after the release" % p["close_by"])
    c = res.get("candidate") or {}
    if c.get("expires") != rel["t"] + 75 * H.MIN:
        bad.append("order deadline %s is not release + 75 min" % c.get("expires"))
    risk = float(p.get("max_loss_at_stop") or 0)
    cap = 1000 * 0.005 * (0.25 if small else 0.5) * 1.06               # 0.5% of 1000 USDT: a quarter if small, half while unproven
    if not (0 < risk <= cap):
        bad.append("loss at stop %.2f USDT is over the %s cap %.2f USDT" % (risk, "small (quarter)" if small else "half-size", cap))
    if p.get("leverage", 1) > 5:
        bad.append("leverage %s over the 5x cap" % p.get("leverage"))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--coins", default=",".join(H.COINS))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    H.fast_mode()
    out = open(a.out, "w") if a.out else None
    violations, n, t0 = [], 0, time.time()
    for kind in a.kinds.split(","):
        for coin in a.coins.split(","):
            for reaction in REACTIONS:
                for m in MOMENTS:
                    rel, now, code, res = H.brief(coin, kind, reaction, m)
                    n += 1
                    row = {"coin": coin, "kind": kind, "reaction": reaction, "minutes": m, "code": code}
                    if code != 0 or not isinstance(res, dict):
                        row["error"] = str(res)[-400:]
                        violations.append(("crash", row))
                    else:
                        D = res.get("decision") or {}
                        row.update({"decision": D.get("decision"), "why": (D.get("why") or "")[:160], "phase": (res.get("event") or {}).get("phase"),
                                    "ideas": [(i["setup"], i["side"], i["evidence"], (i.get("status") or "")[:12]) for i in res.get("event_ideas") or []],
                                    "blocked": res.get("blocked_signal"), "tier": res.get("tier")})
                        want = expected_signal(coin, kind, reaction, m)
                        got = row["decision"] if str(row["decision"]).startswith("SIGNAL") else None
                        if got != want:
                            violations.append(("decision: expected %s, got %s" % (want, row["decision"]), row))
                        elif got:
                            bad = check_signal(res, rel, now, coin)
                            if bad:
                                violations.append(("plan: " + "; ".join(bad), row))
                    if out:
                        out.write(json.dumps(row) + "\n")
                        out.flush()
    print("%d scenarios in %.0f s, %d violations" % (n, time.time() - t0, len(violations)))
    for why, row in violations[:60]:
        print("VIOLATION %s | %s %s %s %+d min | %s" % (why, row["coin"], row["kind"], row["reaction"], row["minutes"],
                                                       row.get("why") or row.get("error")))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
