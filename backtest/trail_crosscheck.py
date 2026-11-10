#!/usr/bin/env python3
"""Cross-check the trailing study on a second, independent data source.

The duka sample (251 days) and the Yahoo sample (59 days, Aug 31 - Nov 9) come
from different feeds, so agreement between them is real evidence rather than one
file's quirk. The Yahoo window sits exactly in the stretch where every exit rule
lost money on the duka sample -- the honest place to look for a rescue.
"""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, HERE)
import owner_plan as OP  # noqa: E402
import exit_compare as EC  # noqa: E402

COST = 0.5
TFS = ("5m", "15m", "30m", "1h", "4h", "1d")


def load_yahoo():
    out = {}
    for tf in TFS:
        out[tf] = json.load(open(os.path.join(HERE, f"yahoo_{tf}.json")))
    return out


def summarise(trades, rule, init_stop=9.0, mode="lag"):
    res = [EC.score(t, rule, init_stop=init_stop, mode=mode) for t in trades]
    n = len(res)
    wins = [x for x in res if x > 0]
    net = sum(res) - COST * n
    eq = peak = dd = 0.0
    for x in res:
        eq += x - COST
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": n, "win": 100 * len(wins) / n, "net": net, "dd": dd}


def main():
    bars = load_yahoo()
    sig = OP.build_signals(bars)
    days = sorted({s["t"][:10] for s in sig})
    print(f"Yahoo sample: {len(sig)} bars, {len(days)} days "
          f"({days[0]}..{days[-1]})\n")
    trades = EC.collect_entries(sig)
    print(f"{len(trades)} entries\n")
    print(f"{'rule':34s} {'n':>4s} {'win%':>6s} {'net$':>9s} {'maxDD$':>9s}")
    checks = [
        ("current: fixed $9 / $20", EC.r_fixed, 9.0, "lag"),
        ("owner ladder (lock $5@+10,$10@+15)", EC.r_owner, 9.0, "lag"),
        ("$9 then trail 2$ (adverse)", EC.make_trail(2), 9.0, "adverse"),
        ("$9 then trail 3$ (adverse)", EC.make_trail(3), 9.0, "adverse"),
        ("$9 then trail 4$ (adverse)", EC.make_trail(4), 9.0, "adverse"),
        ("$9 then trail 6$ (adverse)", EC.make_trail(6), 9.0, "adverse"),
        ("$9 then trail 9$ (adverse)", EC.make_trail(9), 9.0, "adverse"),
    ]
    for name, rule, istop, mode in checks:
        s = summarise(trades, rule, istop, mode)
        print(f"{name:34s} {s['n']:>4d} {s['win']:>6.1f} {s['net']:>9.1f} "
              f"{s['dd']:>9.1f}")


if __name__ == "__main__":
    main()
