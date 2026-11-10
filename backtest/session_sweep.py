#!/usr/bin/env python3
"""Sweep session windows (and the daily-trend filter) over the full year.

The 2-day test liked the session filter; the full year punished it. Before
concluding, try several windows -- if NO window beats the unfiltered baseline,
the session filter is genuinely not helping this bot.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters_year as NF  # noqa: E402


def sim_window(lo, hi, htf=False, stop=9.0, tgt=20.0):
    import bisect
    B5, TIMES, TREND = NF.B5, NF.TIMES, NF.TREND
    tr, op = [], None
    for k in range(300, len(B5) - 1):
        bar = B5[k]
        t = bar["openTime"]
        if op is not None:
            d = op["d"]
            hi_, lo_ = bar["high"], bar["low"]
            hs = lo_ <= op["stop"] if d == "long" else hi_ >= op["stop"]
            ht = hi_ >= op["target"] if d == "long" else lo_ <= op["target"]
            if hs or ht:
                tr.append({"R": -1.0 if hs else tgt / stop, "t": op["t0"],
                           "d": op["d"]})
                op = None
            continue
        d = NF.sig_at(k, False, htf)
        if d is None:
            continue
        h = int(t[11:13])
        if not (lo <= h < hi):
            continue
        sg = 1 if d == "long" else -1
        op = {"d": d, "t0": t, "stop": bar["close"] - sg * stop,
              "target": bar["close"] + sg * tgt}
    return tr


if __name__ == "__main__":
    base = NF.ev(NF.sim(False, False))
    print(f"الأساس (بلا فلترين): {base[0]} صفقة · {base[2]:+.0f}R · تراجع {base[3]:.0f}R")
    print(f"\n{'نافذة الجلسة (UTC)':24}{'صفقات':>7}{'المجموع':>9}{'تراجع':>8}{'أرباع-':>7}")
    for lo, hi in [(0, 24), (7, 20), (8, 21), (12, 20), (13, 17), (14, 18),
                   (7, 16), (0, 12), (12, 24)]:
        e = NF.ev(sim_window(lo, hi))
        tag = " (بلا فلتر)" if (lo, hi) == (0, 24) else ""
        print(f"{f'{lo:02d}:00-{hi:02d}:00':24}{e[0]:7}{e[2]:+8.0f}R"
              f"{e[3]:7.0f}R{e[4]:5}/4{tag}")
    print(f"\nفلتر الترند اليومي (1d) مع أفضل نافذة:")
    for lo, hi in [(0, 24), (12, 20), (13, 17)]:
        e = NF.ev(sim_window(lo, hi, htf=True))
        print(f"{f'{lo:02d}:00-{hi:02d}:00 + 1d':24}{e[0]:7}{e[2]:+8.0f}R"
              f"{e[3]:7.0f}R{e[4]:5}/4")
