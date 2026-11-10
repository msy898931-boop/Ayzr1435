#!/usr/bin/env python3
"""Isolate the EXIT decision: identical entries, several exits.

The walk() comparison lets the exit policy change which trades get taken (one
trade at a time), which mixes two effects. Here the entry stream is fixed by the
current policy; every trade that the desk would enter is then scored under every
exit rule from the SAME entry bar. The difference is purely the exit.
"""
from __future__ import annotations
import json
import sys

sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, "/workspace/Ayzr1435/backtest")
import owner_plan as OP  # noqa: E402

STOP, TGT, COST = 9.0, 20.0, 0.5


def collect_entries(sig, occ_stop=30.0, occ_tgt=20.0):
    """The desk's own entries, each with the FULL bar path to its end.

    Step 1: find the entry bars exactly as the desk does ($9 stop / $20 target
    occupancy, one trade at a time). Step 2: from each entry, scan forward and
    record bars until the $20 target or a deliberately WIDE `occ_stop` is hit,
    so any tighter or trailing rule can be scored on the same entry without the
    path being truncated. Trades are scored independently, so this is the "given
    this entry stream, what does the exit earn" counterfactual.
    """
    # step 1: the desk's entry bars (no re-entry on the exit bar, as day_replay)
    entries, op = [], None
    for i, s in enumerate(sig):
        if op is not None:
            d, e = op["d"], op["entry"]
            hit_stop = (s["lo"] <= e - STOP) if d == "long" else (s["hi"] >= e + STOP)
            hit_tgt = (s["hi"] >= e + TGT) if d == "long" else (s["lo"] <= e - TGT)
            if hit_stop or hit_tgt:
                op = None
            continue
        if s["d"] and s["ok"]:
            op = {"d": s["d"], "entry": s["px"]}
            entries.append(i)
    # step 2: full path from each entry
    trades = []
    for i in entries:
        s0 = sig[i]
        d, e = s0["d"], s0["px"]
        sg = 1 if d == "long" else -1
        bars = []
        for s in sig[i + 1:]:
            bars.append((s["hi"], s["lo"], s["px"]))
            hit_wide = (s["lo"] <= e - occ_stop) if d == "long" else (s["hi"] >= e + occ_stop)
            hit_tgt = (s["hi"] >= e + occ_tgt) if d == "long" else (s["lo"] <= e - occ_tgt)
            if hit_wide or hit_tgt:
                break
        trades.append({"d": d, "entry": e, "t": s0["t"], "bars": bars})
    return trades


def score(trade, rule, init_stop=STOP, mode="lag"):
    """Run one trade's bar path under an exit `rule`; return $ result.

    init_stop: the stop distance at entry (default the desk's $9).
    mode: "lag"    -- the stop in force is the one from the previous bar's peak
                      (the trail moves after the bar closes; generous).
          "adverse"-- within each bar assume the favourable extreme came first,
                      move the stop, THEN test the adverse extreme (pessimistic).
    A fixed $20 target stays in force either way. If neither is hit the trade is
    marked to the last bar's CLOSE (not its extreme).
    """
    d, e = trade["d"], trade["entry"]
    sg = 1 if d == "long" else -1
    stop = e - sg * init_stop
    tgt = e + sg * TGT
    peak = 0.0
    for hi, lo, close in trade["bars"]:
        fav = (hi - e) if d == "long" else (e - lo)
        if mode == "adverse":
            peak = max(peak, fav)
            stop = rule(stop, e, peak, sg)
            hit_stop = (lo <= stop) if d == "long" else (hi >= stop)
            hit_tgt = (hi >= tgt) if d == "long" else (lo <= tgt)
        else:
            hit_stop = (lo <= stop) if d == "long" else (hi >= stop)
            hit_tgt = (hi >= tgt) if d == "long" else (lo <= tgt)
            peak = max(peak, fav)
            stop = rule(stop, e, peak, sg)
        if hit_stop:
            return (stop - e) * sg
        if hit_tgt:
            return (tgt - e) * sg
    return (trade["bars"][-1][2] - e) * sg


def make_fixed(dist):
    """A plain fixed stop of `dist` dollars (no trailing)."""
    def r(stop, e, peak, sg):
        return e - sg * dist
    return r


def r_fixed(stop, e, peak, sg):
    return stop


def r_owner(stop, e, peak, sg):
    if peak >= 15:
        return max(stop, e + sg * 10)
    if peak >= 10:
        return max(stop, e + sg * 5)
    return stop


def r_be(stop, e, peak, sg):
    return max(stop, e) if peak >= 10 else stop


def make_trail(dist):
    def r(stop, e, peak, sg):
        return max(stop, e + sg * (peak - dist))
    return r


def make_trail_after(act, dist):
    def r(stop, e, peak, sg):
        if peak >= act:
            return max(stop, e + sg * (peak - dist))
        return stop
    return r


def report(name, rule, trades):
    res = [score(t, rule) for t in trades]
    n = len(res)
    wins = [x for x in res if x > 0]
    net = sum(res) - COST * n
    eq = peak = dd = 0.0
    for x in res:
        eq += x - COST
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    print(f"  {name:34s} n={n:4d} win={100*len(wins)/n:5.1f}% "
          f"net={net:8.1f}$ avg={net/n:6.2f}$ maxDD={dd:8.1f}$")


if __name__ == "__main__":
    bars = json.load(open(OP.DATA))
    sig = OP.build_signals(bars)
    trades = collect_entries(sig)
    print(f"same-entry exit comparison on {len(trades)} trades "
          f"(identical entries)\n")
    report("current: fixed $9 / $20", r_fixed, trades)
    report("owner ladder (lock $5@+10,$10@+15)", r_owner, trades)
    report("break-even @+10", r_be, trades)
    for d in (4, 6, 9):
        report(f"trail {d}$ from the start", make_trail(d), trades)
    for a, d in ((10, 4), (10, 6), (12, 6), (15, 8)):
        report(f"trail {d}$ once +{a}$", make_trail_after(a, d), trades)
