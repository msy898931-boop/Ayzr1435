#!/usr/bin/env python3
"""Validate the two new filters over the full 2026 year (251 days).

Session filter: only 07:00-20:00 UTC.
HTF filter: do not trade against the last CLOSED daily bar's tilt.

The 2-day test alone cannot say if a filter is good -- it can only say what it
did on two days. This runs the same entries over the whole year so the filter's
real value (or harm) is visible, and reports the 2-day slice next to it.
"""
from __future__ import annotations
import bisect
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot2026 as B  # noqa: E402

raw, TREND, TIMES, E20 = B.load()
B5 = raw["5m"]
b15 = raw["15m"]
ADX = B._adx(b15)
ATR = B._atr_series(b15)
MACD = B._macd_hist(b15)
RSI = B._rsi_series([x["close"] for x in b15])


def sig_at(k, session, htf):
    bar = B5[k]
    t = bar["openTime"]
    live = bar["close"]
    up = dn = 0
    for tf in B.FRAMES:
        j = bisect.bisect_right(TIMES[tf], t) - 1
        if j < 0:
            continue
        v = TREND[tf][j]
        up += v == 1
        dn += v == -1
    d = "long" if up >= B.CONFIRM else ("short" if dn >= B.CONFIRM else None)
    if d is None:
        return None
    jc = bisect.bisect_right(TIMES[B.FILTER_TF], t) - 1 - 1
    if jc >= 0 and E20[B.FILTER_TF][jc] is not None:
        e = E20[B.FILTER_TF][jc]
        if (d == "long" and live <= e) or (d == "short" and live >= e):
            return None
    j15 = bisect.bisect_right(TIMES["15m"], t) - 1 - 1
    if j15 >= 0 and not B._gate_ok(d, j15, live, ADX, ATR, MACD, RSI):
        return None
    if session:
        h = int(t[11:13])
        if not (7 <= h < 20):
            return None
    if htf:
        j1d = bisect.bisect_right(TIMES["1d"], t) - 1 - 1
        if j1d >= 0:
            tilt = TREND["1d"][j1d]
            if (d == "long" and tilt == -1) or (d == "short" and tilt == 1):
                return None
    return d


def sim(session, htf, stop=9.0, tgt=20.0, days=None):
    tr, op = [], None
    for k in range(300, len(B5) - 1):
        bar = B5[k]
        t = bar["openTime"]
        if op is not None:
            d = op["d"]
            hi, lo = bar["high"], bar["low"]
            hs = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else lo <= op["target"]
            if hs or ht:
                tr.append({"R": -1.0 if hs else tgt / stop, "t": op["t0"],
                           "d": op["d"]})
                op = None
            continue
        d = sig_at(k, session, htf)
        if d is None:
            continue
        sg = 1 if d == "long" else -1
        op = {"d": d, "t0": t, "stop": bar["close"] - sg * stop,
              "target": bar["close"] + sg * tgt}
    if days:
        tr = [x for x in tr if x["t"][:10] in days]
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
    lo = sum(1 for x in ts if x["d"] == "long")
    return n, 100 * w / n, tot, mdd, qn, lo, n - lo


if __name__ == "__main__":
    days = sorted({x["openTime"][:10] for x in B5})[-2:]
    combos = [
        ("الحالي", False, False),
        ("+ جلسة", True, False),
        ("+ 1d", False, True),
        ("+ جلسة + 1d", True, True),
    ]
    print(f"=== السنة كاملة (251 يوم) · وقف $9/هدف $20 ===")
    print(f"{'الفلتر':16}{'صفقات':>7}{'شراء':>6}{'بيع':>6}{'ربح%':>7}"
          f"{'المجموع':>9}{'تراجع':>8}{'أرباع-':>7}")
    for name, s, h in combos:
        e = ev(sim(s, h))
        print(f"{name:16}{e[0]:7}{e[5]:6}{e[6]:6}{e[1]:6.0f}%{e[2]:+8.0f}R"
              f"{e[3]:7.0f}R{e[4]:5}/4")
    print(f"\n=== نفس الشي على يومين بس ({days[0]} + {days[1]}) ===")
    print(f"{'الفلتر':16}{'صفقات':>7}{'شراء':>6}{'بيع':>6}{'ربح':>5}{'خسارة':>6}{'الصافي':>9}")
    for name, s, h in combos:
        e = ev(sim(s, h, days=set(days)))
        if not e:
            print(f"{name:16}{0:7}{0:6}{0:6}{0:5}{0:6}{0:+8.0f}$")
            continue
        w = round(e[0] * e[1] / 100)
        print(f"{name:16}{e[0]:7}{e[5]:6}{e[6]:6}{w:5}{e[0]-w:6}"
              f"{(w*20-(e[0]-w)*9):+8.0f}$")
