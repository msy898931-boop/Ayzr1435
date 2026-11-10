#!/usr/bin/env python3
"""Does the trailing edge survive realistic costs? (the decisive test)

A tight trail wins small and wins often, so its profit is thin per trade. Gold
spot spread + slippage on entry and exit is easily $1-2 a round trip, not the
$0.5 the other scripts assume. If the edge dies at $1.5, it is an artefact of
perfect fills, not a strategy.
"""
from __future__ import annotations
import json
import sys

sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, "/workspace/Ayzr1435/backtest")
import owner_plan as OP  # noqa: E402
import exit_compare as EC  # noqa: E402


def stats(trades, rule, init_stop=9.0, mode="adverse"):
    res = [EC.score(t, rule, init_stop=init_stop, mode=mode) for t in trades]
    n = len(res)
    wins = [x for x in res if x > 0]
    losses = [x for x in res if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0
    al = sum(losses) / len(losses) if losses else 0
    return n, res, aw, al


def main():
    bars = json.load(open(OP.DATA))
    sig = OP.build_signals(bars)
    trades = EC.collect_entries(sig)
    rules = [
        ("fixed $9 (current)", EC.r_fixed, 9.0),
        ("$9 then trail 2$", EC.make_trail(2), 9.0),
        ("$9 then trail 3$", EC.make_trail(3), 9.0),
        ("$9 then trail 4$", EC.make_trail(4), 9.0),
        ("$9 then trail 6$", EC.make_trail(6), 9.0),
        ("$9 then trail 9$", EC.make_trail(9), 9.0),
    ]
    costs = [0.5, 1.0, 1.5, 2.0, 3.0]
    print(f"{len(trades)} trades, ADVERSE intrabar, net$ at each cost/trade:\n")
    print(f"{'rule':22s} {'win%':>6s} {'avgWin':>7s} {'avgLoss':>8s}  "
          + " ".join(f"{'$'+str(c):>9s}" for c in costs))
    for name, rule, istop in rules:
        n, res, aw, al = stats(trades, rule, istop, "adverse")
        wr = 100 * len([x for x in res if x > 0]) / n
        nets = [sum(res) - c * n for c in costs]
        cells = " ".join(f"{x:>9.1f}" for x in nets)
        print(f"{name:22s} {wr:>6.1f} {aw:>7.2f} {al:>8.2f}  {cells}")
    print("\n(avgWin/avgLoss are in $, before costs)")


if __name__ == "__main__":
    main()
