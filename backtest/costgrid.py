#!/usr/bin/env python3
"""Cost-aware grid: same entries, but each trade pays spread+slippage.

A stop of $4.5 looks best on paper, but gold costs ~$0.5-1.0 round-trip in
spread and slippage. The cost is charged against BOTH sides: a loss is larger
by `cost`, a win is smaller by `cost`. This is the honest version of the grid.
"""
from __future__ import annotations
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fullgrid as FG  # noqa: E402


def sim(b5, sig, stop, tgt, cost):
    tr, op = [], None
    for k in range(len(sig)):
        bar = b5[k + 300]
        t = bar["openTime"]
        if op is not None:
            d = op["d"]
            hi, lo = bar["high"], bar["low"]
            hs = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else lo <= op["target"]
            if hs or ht:
                r = -((stop + cost) / stop) if hs else (tgt - cost) / stop
                tr.append({"R": r, "t": op["t0"]})
                op = None
            continue
        d = sig[k]
        if d is None:
            continue
        sg = 1 if d == "long" else -1
        op = {"d": d, "t0": t, "stop": bar["close"] - sg * stop,
              "target": bar["close"] + sg * tgt}
    return tr


def ev(ts):
    n = len(ts)
    if not n:
        return None
    w = sum(1 for x in ts if x["R"] > 0)
    tot = sum(x["R"] for x in ts)
    bal = peak = mdd = 0.0
    for x in ts:
        bal += x["R"]
        peak = max(peak, bal)
        mdd = min(mdd, bal - peak)
    q = collections.defaultdict(float)
    for x in ts:
        q[(int(x["t"][5:7]) - 1) // 3 + 1] += x["R"]
    qn = sum(1 for kk in sorted(q) if q[kk] < 0)
    half = n // 2
    h1 = sum(x["R"] for x in ts[:half])
    h2 = sum(x["R"] for x in ts[half:])
    return {"n": n, "win": 100 * w / n, "r": tot, "dd": mdd, "qn": qn,
            "h1": h1, "h2": h2}


if __name__ == "__main__":
    b5, sig = FG.build_signals()
    stops = (4.5, 6.0, 7.5, 9.0, 12.0, 15.0)
    tgts = (9.0, 12.0, 15.0, 18.0, 20.0, 24.0, 30.0)
    for cost in (0.0, 0.5, 1.0):
        print(f"\n=== تكلفة تنفيذ ${cost:.1f}/صفقة ===")
        print(f"{'وقف':>6}{'هدف':>7}{'R:R':>6}{'صفقات':>7}{'ربح%':>7}"
              f"{'المجموع':>9}{'تراجع':>8}{'أرباع-':>7}{'نص1':>7}{'نص2':>7}")
        res = []
        for stop in stops:
            for tgt in tgts:
                m = ev(sim(b5, sig, stop, tgt, cost))
                res.append((m["r"], stop, tgt, m))
        res.sort(reverse=True)
        for r, stop, tgt, m in res[:8]:
            print(f"{stop:6.1f}{tgt:7.1f}{tgt/stop:6.2f}{m['n']:7}{m['win']:6.1f}%"
                  f"{m['r']:+8.1f}R{m['dd']:7.1f}R{m['qn']:5}/4"
                  f"{m['h1']:+6.0f}R{m['h2']:+6.0f}R")
