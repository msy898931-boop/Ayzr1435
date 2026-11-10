#!/usr/bin/env python3
"""The full grid: every stop x every target, ranked honestly.

Entry signals (the filtered 4/6 + EMA20 + ADX/ATR gate) are computed ONCE, then
every (stop, target) pair is simulated on the same entries -- so the grid is
apples-to-apples. Reports total R, drawdown, win rate and robustness (first vs
second half, negative quarters) on the 251-day 2026 sample, plus the same grid
on the last two days so the small-sample noise is visible next to it.
"""
from __future__ import annotations
import bisect
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot2026 as B  # noqa: E402

STOPS = (4.5, 6.0, 7.5, 9.0, 10.5, 12.0, 15.0, 20.0)
TARGETS = (6.0, 9.0, 10.0, 12.0, 13.5, 15.0, 18.0, 20.0, 24.0, 30.0)


def build_signals():
    raw, TREND, TIMES, E20 = B.load()
    b5 = raw["5m"]
    b15 = raw["15m"]
    ADX = B._adx(b15)
    ATR = B._atr_series(b15)
    MACD = B._macd_hist(b15)
    RSI = B._rsi_series([x["close"] for x in b15])
    sig = []
    for k in range(300, len(b5) - 1):
        bar = b5[k]
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
            sig.append(None)
            continue
        jc = bisect.bisect_right(TIMES[B.FILTER_TF], t) - 1 - 1
        if jc >= 0 and E20[B.FILTER_TF][jc] is not None:
            e = E20[B.FILTER_TF][jc]
            if (d == "long" and live <= e) or (d == "short" and live >= e):
                sig.append(None)
                continue
        j15 = bisect.bisect_right(TIMES["15m"], t) - 1 - 1
        if j15 >= 0 and not B._gate_ok(d, j15, live, ADX, ATR, MACD, RSI):
            sig.append(None)
            continue
        sig.append(d)
    return b5, sig


def simulate(b5, sig, stop, tgt, day_filter=None):
    trades = []
    op = None
    for k in range(len(sig)):
        i = k + 300
        bar = b5[i]
        t = bar["openTime"]
        if day_filter and t[:10] not in day_filter:
            if op is not None:
                pass
            continue
        if op is not None:
            d = op["d"]
            hi, lo = bar["high"], bar["low"]
            hs = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else lo <= op["target"]
            if hs or ht:
                trades.append({"R": -1.0 if hs else tgt / stop, "t": op["t0"]})
                op = None
            continue
        d = sig[k]
        if d is None:
            continue
        sg = 1 if d == "long" else -1
        op = {"d": d, "t0": t, "stop": bar["close"] - sg * stop,
              "target": bar["close"] + sg * tgt}
    return trades


def metrics(ts):
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
    b5, sig = build_signals()
    days = sorted({x["openTime"][:10] for x in b5})[-2:]
    print(f"الإشارات: {sum(1 for x in sig if x)} شمعة إشارة من {len(sig)}")

    print("\n=== شبكة المجموع (R) — 251 يوم · الصفوف=الأهداف، الأعمدة=الوقوف ===")
    print("هدف\\وقف " + "".join(f"{s:>8.1f}" for s in STOPS))
    best = []
    grid = {}
    for tgt in TARGETS:
        line = f"{tgt:>7.1f} "
        for stop in STOPS:
            m = metrics(simulate(b5, sig, stop, tgt))
            grid[(stop, tgt)] = m
            line += f"{m['r']:>+8.1f}"
            best.append((m["r"], stop, tgt, m))
        print(line)

    best.sort(reverse=True)
    print("\n=== أفضل 12 (بالمجموع) على 251 يوم ===")
    print(f"{'وقف':>6}{'هدف':>7}{'R:R':>6}{'صفقات':>7}{'ربح%':>7}{'المجموع':>9}"
          f"{'تراجع':>8}{'أرباع-':>7}{'نص1':>7}{'نص2':>7}")
    for r, stop, tgt, m in best[:12]:
        print(f"{stop:6.1f}{tgt:7.1f}{tgt/stop:6.2f}{m['n']:7}{m['win']:6.1f}%"
              f"{m['r']:+8.1f}R{m['dd']:7.1f}R{m['qn']:5}/4"
              f"{m['h1']:+6.0f}R{m['h2']:+6.0f}R")

    print("\n=== نفس الإعدادات على اليومين الأخيرين فقط ===")
    print(f"{'وقف':>6}{'هدف':>7}{'صفقات':>7}{'المجموع':>9}{'$':>8}")
    for r, stop, tgt, m in best[:8]:
        t2 = simulate(b5, sig, stop, tgt, day_filter=set(days))
        mm = metrics(t2)
        usd = sum(x["R"] for x in t2) * stop
        print(f"{stop:6.1f}{tgt:7.1f}{mm['n']:7}{mm['r']:+8.1f}R{usd:+8.0f}$")
