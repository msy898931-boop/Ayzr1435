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


def collect_entries(sig):
    """The trades the CURRENT desk takes, with each bar's high/low kept."""
    trades, op = [], None
    for s in sig:
        if op is not None:
            d, e = op["d"], op["entry"]
            sg = 1 if d == "long" else -1
            op["bars"].append((s["hi"], s["lo"]))
            hit_stop = (s["lo"] <= op["stop"]) if d == "long" else (s["hi"] >= op["stop"])
            hit_tgt = (s["hi"] >= e + TGT) if d == "long" else (s["lo"] <= e - TGT)
            if hit_stop or hit_tgt:
                trades.append(op)
                op = None
        if op is None and s["d"] and s["ok"]:
            sg = 1 if s["d"] == "long" else -1
            op = {"d": s["d"], "entry": s["px"], "bars": [], "t": s["t"],
                  "stop": s["px"] - sg * STOP}
    return trades


def score(trade, rule):
    """Run one trade's bar path under an exit `rule`; return $ result."""
    d, e = trade["d"], trade["entry"]
    sg = 1 if d == "long" else -1
    stop = e - sg * STOP
    tgt = e + sg * TGT
    peak = 0.0
    for hi, lo in trade["bars"]:
        fav = (hi - e) if d == "long" else (e - lo)
        peak = max(peak, fav)
        hit_stop = (lo <= stop) if d == "long" else (hi >= stop)
        hit_tgt = (hi >= tgt) if d == "long" else (lo <= tgt)
        if hit_stop:
            return (stop - e) * sg
        if hit_tgt:
            return (tgt - e) * sg
        stop = rule(stop, e, peak, sg)
    return (trade["bars"][-1][0 if d == "long" else 1] - e) * sg


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
