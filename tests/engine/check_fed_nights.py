"""The live pipeline against the research replay, Fed night by Fed night, on real Binance data.

    python3 tests/engine/check_fed_nights.py --since 2025-01-01 --replay hist_fomc.json [--cache DIR]

At minute 61 after each Fed decision it runs `today` (the five Fed-basket coins) on a market rebuilt from
data.binance.vision, as the app would, and compares each coin's decision with what `verify-news` recorded for that night
(replay_history.py --out): a night the rule traded must show a basket signal, or a block that names its reason; a night
the first hour did not qualify must show none.
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import desk  # noqa: E402
from replay_history import Archive, with_recent_fed  # noqa: E402
from replay_market import Market  # noqa: E402

MIN = 60000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/trinetra-binance"))
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--replay", required=True, help="the --out file of replay_history.py (FOMC, momentum_after_60m)")
    ap.add_argument("--coins", default=",".join(desk.BASKET_COINS))
    a = ap.parse_args()
    H.fast_mode()
    with_recent_fed()
    since = desk.parse_when(a.since + "T00:00Z", "since")
    want = {}
    for r in json.load(open(a.replay)):
        if r.get("kind") == "FOMC" and r.get("rule") == "momentum_after_60m":
            for t in r["trades"]:
                want[(r["coin"], t["date"])] = t["side"]
            for d, why in r["skipped"]:
                want.setdefault((r["coin"], d), None if "did not qualify" in why else "?")
    m = Market(Archive(a.cache))
    bad, n = [], 0
    for rel in [r for r in desk.load_release_history() if r["kind"] == "FOMC" and r["t"] >= since]:
        t = rel["t"] + 61 * MIN
        m.now = t
        home = tempfile.mkdtemp(prefix="desk-fed-")
        code, res = H.run(["today", "--coins", a.coins], t, m, home=home)
        if code != 0 or not isinstance(res, dict):
            bad.append("%s: today failed: %s" % (rel["date"], str(res)[-200:]))
            continue
        for c in res["coins"]:
            coin = desk.base_of(c["symbol"])
            exp = want.get((coin, rel["date"]), "?")
            got = c.get("decision") or ""
            side = "long" if got.endswith("LONG") else "short" if got.endswith("SHORT") else None
            tier = c.get("tier")
            note = ""
            if exp == "?":
                note = "no replay result to compare"
            elif exp is None and side:
                bad.append("%s %s: signalled %s but the replay's first hour did not qualify" % (rel["date"], coin, got))
            elif exp and side and side != exp:
                bad.append("%s %s: signalled %s, the replay traded %s" % (rel["date"], coin, got, exp))
            elif exp and not side:
                note = "blocked: " + "; ".join(b.split(":")[0] for b in c.get("blocks") or []) if c.get("blocks") else "NO SIGNAL: " + (c.get("why") or "")[:120]
                if not c.get("blocks"):
                    bad.append("%s %s: the replay traded %s, the live pipeline gave %s without a block" % (rel["date"], coin, exp, got))
            elif exp and side and tier != "basket":
                bad.append("%s %s: a signal not labelled as the Fed basket" % (rel["date"], coin))
            n += 1
            print("%s %-4s replay %-6s live %-13s %s %s" % (rel["date"], coin, exp or "none", got, "(basket)" if tier == "basket" else "", note))
    print("%d coin-nights compared, %d disagreements" % (n, len(bad)))
    for b in bad:
        print("DISAGREE " + b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
