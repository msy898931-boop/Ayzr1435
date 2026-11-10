#!/usr/bin/env python3
"""Trailing-stop study: does the TRAIL help, or is it just a tighter stop?

Same entries as the desk on 251 days, scored from the SAME entry bar so the
difference is purely the exit. Four questions, in order:

  1. fixed stops of many widths (no trailing) -- the baseline for "how much does
     a tighter stop alone earn?"  (the desk's own is $9)
  2. the desk's $9 stop that then TRAILS by `dist` -- the honest test of
     "trailing helps", holding the entry stop at $9.
  3. the same with an intrabar "adverse first" rule (pessimistic): within a bar
     the trail is allowed to move, THEN the low is tested. A rule that survives
     only under the generous reading is not real.
  4. the winner, with a $9 entry stop vs a stop as wide as the trail.

A fixed $20 target stays in force throughout.
"""
from __future__ import annotations
import json
import sys

sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, "/workspace/Ayzr1435/backtest")
import owner_plan as OP  # noqa: E402
import exit_compare as EC  # noqa: E402

COST = 0.5


def quarter(t):
    y, m = t[:4], int(t[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


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
    by_q: dict[str, float] = {}
    for t, x in zip(trades, res):
        by_q[quarter(t["t"])] = by_q.get(quarter(t["t"]), 0.0) + x - COST
    return {"n": n, "win": 100 * len(wins) / n, "net": net, "dd": dd,
            "avg": net / n, "by_q": by_q}


def main():
    bars = json.load(open(OP.DATA))
    sig = OP.build_signals(bars)
    trades = EC.collect_entries(sig)
    qs = sorted({quarter(t["t"]) for t in trades})
    print(f"trailing-stop study: {len(trades)} identical entries, "
          f"quarters {qs}\n")
    hdr = (f"{'rule':30s} {'win%':>6s} {'net$':>9s} {'avg$':>7s} "
           f"{'maxDD$':>9s}  " + " ".join(f"{q[-2:]:>8s}" for q in qs))

    def row(name, s):
        cells = " ".join(f"{s['by_q'].get(q, 0.0):>8.0f}" for q in qs)
        print(f"{name:30s} {s['win']:>6.1f} {s['net']:>9.1f} {s['avg']:>7.2f} "
              f"{s['dd']:>9.1f}  {cells}")

    print("== 1. fixed stops of many widths (NO trailing) ==")
    print(hdr)
    for d in (2, 3, 4, 5, 6, 8, 9, 12):
        row(f"fixed {d}$ stop", summarise(trades, EC.make_fixed(d), init_stop=d))

    print("\n== 2. the desk's $9 stop, then TRAIL by dist (generous bar order) ==")
    print(hdr)
    for d in (2, 3, 4, 5, 6, 8):
        row(f"$9 then trail {d}$",
            summarise(trades, EC.make_trail(d), init_stop=9.0, mode="lag"))

    print("\n== 3. same, but ADVERSE-first inside the bar (pessimistic) ==")
    print(hdr)
    for d in (2, 3, 4, 5, 6, 8):
        row(f"$9 then trail {d}$",
            summarise(trades, EC.make_trail(d), init_stop=9.0, mode="adverse"))

    print("\n== 4. the winner, $9 entry stop vs a stop as wide as the trail ==")
    print(hdr)
    for d in (3, 4, 5):
        row(f"trail {d}$, $9 entry",
            summarise(trades, EC.make_trail(d), init_stop=9.0, mode="adverse"))
        row(f"trail {d}$, entry {d}$",
            summarise(trades, EC.make_trail(d), init_stop=float(d),
                      mode="adverse"))


if __name__ == "__main__":
    main()
