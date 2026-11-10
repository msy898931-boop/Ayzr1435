#!/usr/bin/env python3
"""Stop x target grid on the filtered bot, honest and walk-forward.

The owner asked: if the target is $13.5, what should the stop be? This sweeps
the stop ($4.5-$12) against the target (1.0-3.0 x the stop) on the filtered bot's
own entries, and reports the first half vs the second half of 2026 so a choice
that only works in one stretch is visible.

Grading is the desk's own: first touch wins, a bar spanning both takes the stop,
a trade still open at the end of the data is left unresolved.
"""
from __future__ import annotations
import bisect
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot2026 as B  # noqa: E402

S = B.STOP_USD


def _signal(t, live):
    up = dn = 0
    for tf in B.FRAMES:
        j = bisect.bisect_right(B.TIMES[tf], t) - 1
        if j < 0:
            continue
        v = B.TREND[tf][j]
        up += v == 1
        dn += v == -1
    d = "long" if up >= B.CONFIRM else ("short" if dn >= B.CONFIRM else None)
    if d is None:
        return None
    jc = bisect.bisect_right(B.TIMES[B.FILTER_TF], t) - 1 - 1
    if jc >= 0 and B.E20[B.FILTER_TF][jc] is not None:
        e = B.E20[B.FILTER_TF][jc]
        if (d == "long" and live <= e) or (d == "short" and live >= e):
            return None
    j15 = bisect.bisect_right(B.TIMES["15m"], t) - 1 - 1
    if j15 >= 0 and not B._gate_ok(d, j15, live, B.ADX, B.ATR, B.MACD, B.RSI):
        return None
    return d


def run(stop_usd, rr):
    b5 = B.B5
    trades, op = [], None
    for k in range(300, len(b5) - 1):
        bar = b5[k]
        t = bar["openTime"]
        hi, lo = bar["high"], bar["low"]
        if op is not None:
            d = op["d"]
            hit_s = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            hit_t = hi >= op["tgt"] if d == "long" else lo <= op["tgt"]
            if hit_s or hit_t:
                trades.append({"R": -1.0 if hit_s else rr, "t": op["t0"]})
                op = None
            continue
        d = _signal(t, bar["close"])
        if d is None:
            continue
        sg = 1 if d == "long" else -1
        entry = bar["close"]
        op = {"d": d, "t0": t, "stop": entry - sg * stop_usd,
              "tgt": entry + sg * stop_usd * rr}
    return trades


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
    qn = sum(1 for k in sorted(q) if q[k] < 0)
    half = len(ts) // 2
    h1 = sum(x["R"] for x in ts[:half])
    h2 = sum(x["R"] for x in ts[half:])
    return {"n": n, "win": 100 * w / n, "r": tot, "per": tot / n,
            "dd": mdd, "qn": qn, "h1": h1, "h2": h2}


if __name__ == "__main__":
    raw, TREND, TIMES, E20 = B.load()
    B.TREND, B.TIMES, B.E20 = TREND, TIMES, E20
    B.B5 = raw["5m"]
    b15 = raw["15m"]
    B.MACD = B._macd_hist(b15)
    B.ADX = B._adx(b15)
    B.ATR = B._atr_series(b15)
    B.RSI = B._rsi_series([x["close"] for x in b15])
    print(f"{'وقف':>5}{'هدف':>6}{'R:R':>5}{'صفقات':>7}{'ربح%':>7}{'المجموع':>9}"
          f"{'R/صفقة':>8}{'تراجع':>8}{'أرباع-':>7}{'نص1':>7}{'نص2':>7}")
    print("-" * 82)
    for stop in (4.5, 6.0, 7.5, 9.0, 12.0):
        for rr in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
            r = ev(run(stop, rr))
            if not r:
                continue
            flag = " ✅" if r["per"] > 0.10 and r["qn"] == 0 and r["h1"] > 0 \
                and r["h2"] > 0 else ""
            print(f"{stop:5.1f}{stop * rr:6.1f}{rr:5.2f}{r['n']:7}{r['win']:6.1f}%"
                  f"{r['r']:+8.1f}R{r['per']:+7.3f}R{r['dd']:7.1f}R{r['qn']:5}/4"
                  f"{r['h1']:+6.0f}R{r['h2']:+6.0f}R{flag}")
