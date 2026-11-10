#!/usr/bin/env python3
"""How does trade COUNT change the outcome? Vary the frame-confirmation bar.

The owner's friend's bot "takes more buy/sell trades". This measures the honest
trade-off: loosening the confirmation (2/6 = almost every bar) takes far more
trades -- does it make more money? Same $9/$20 exit, same 251 days.
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


def build(confirm, use_gate=True, use_ema=True):
    sig = []
    for k in range(300, len(B5) - 1):
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
        d = "long" if up >= confirm else ("short" if dn >= confirm else None)
        if d is None:
            sig.append(None)
            continue
        jc = bisect.bisect_right(TIMES[B.FILTER_TF], t) - 1 - 1
        if use_ema and jc >= 0 and E20[B.FILTER_TF][jc] is not None:
            e = E20[B.FILTER_TF][jc]
            if (d == "long" and live <= e) or (d == "short" and live >= e):
                sig.append(None)
                continue
        j15 = bisect.bisect_right(TIMES["15m"], t) - 1 - 1
        if use_gate and j15 >= 0 and not B._gate_ok(d, j15, live, ADX, ATR, MACD, RSI):
            sig.append(None)
            continue
        sig.append(d)
    return sig


def sim(sig, stop=9.0, tgt=20.0):
    tr, op = [], None
    for k in range(len(sig)):
        bar = B5[k + 300]
        t = bar["openTime"]
        if op is not None:
            d = op["d"]
            hi, lo = bar["high"], bar["low"]
            hs = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else lo <= op["target"]
            if hs or ht:
                tr.append({"R": -1.0 if hs else tgt / stop, "t": op["t0"]})
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
    return n, 100 * w / n, tot, mdd, qn


if __name__ == "__main__":
    print("وقف $9 / هدف $20 · 251 يوم · كم صفقة باليوم vs الربح:")
    print(f"{'الفلتر':30}{'صفقات':>7}{'صفقة/يوم':>9}{'ربح%':>7}"
          f"{'المجموع':>9}{'تراجع':>8}{'أرباع-':>7}")
    variants = [
        ("اتفاق 2/6 (شبه بلا فلتر)", dict(confirm=2, use_gate=False, use_ema=False)),
        ("اتفاق 3/6", dict(confirm=3, use_gate=True, use_ema=True)),
        ("اتفاق 4/6 (الحالي)", dict(confirm=4, use_gate=True, use_ema=True)),
        ("اتفاق 5/6", dict(confirm=5, use_gate=True, use_ema=True)),
        ("اتفاق 6/6 (الكل متوافق)", dict(confirm=6, use_gate=True, use_ema=True)),
        ("4/6 بلا بوابة ADX/ATR", dict(confirm=4, use_gate=False, use_ema=True)),
        ("4/6 بلا فلتر EMA", dict(confirm=4, use_gate=True, use_ema=False)),
    ]
    for name, kw in variants:
        e = ev(sim(build(**kw)))
        print(f"{name:30}{e[0]:7}{e[0]/251:9.1f}{e[1]:6.0f}%{e[2]:+8.0f}R"
              f"{e[3]:7.0f}R{e[4]:5}/4")
