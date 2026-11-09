#!/usr/bin/env python3
"""The new filter, tested on the desk's OWN live spot feed (not the futures proxy).

Rule under test: two frames (15m + 1h) agree AND RSI(14) on 15m is 35-55 AND
ATR(14) on 15m is under 0.25% of price. Long and short. Sizing and grading are
the desk's own: market entry, $9 stop, R:R 2.0, first touch wins, a bar spanning
both takes the stop.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import search as S  # noqa: E402


def real(tf):
    b = bot.bars(tf, 300)
    return sorted([x for x in b if not x.get("isOpen")], key=lambda x: x["openTime"])


class Live:
    """Precomputed indicators over a live-feed frame."""

    def __init__(self, bars):
        self.b = bars
        cl = [x["close"] for x in bars]
        self.e20 = S.ema_series(cl, 20)
        self.e50 = S.ema_series(cl, 50)
        self.rsi = S.rsi_series(cl, 14)
        self.atr = S.atr_series(bars, 14)
        self.trend = [0] * len(bars)
        for i in range(len(bars)):
            a, c = self.e20[i], self.e50[i]
            if a and c:
                self.trend[i] = 1 if a > c * 1.0005 else (-1 if a < c * 0.9995 else 0)

    def align(self, t):
        lo, hi = 0, len(self.b) - 1
        if t < self.b[0]["openTime"]:
            return -1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.b[mid]["openTime"] <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo


def simulate_live(P, rsi_lo, rsi_hi, atr_max, rr=2.0):
    b15 = P["15m"].b
    trades, open_t, cd = [], None, 0
    for i in range(60, len(b15) - 1):
        bar = b15[i]; t = bar["openTime"]
        if open_t is not None:
            hi, lo = bar["high"], bar["low"]; d = open_t["direction"]
            hs = lo <= open_t["stop"] if d == "long" else hi >= open_t["stop"]
            ht = hi >= open_t["target"] if d == "long" else lo <= open_t["target"]
            if hs or ht:
                res = "stop" if hs else "target"
                open_t.update(result=res, exit_time=t,
                              R=(-1.0 if res == "stop" else open_t["rr"]))
                trades.append(open_t); open_t = None
            continue
        if cd > 0:
            cd -= 1; continue
        t15 = P["15m"].trend[i]
        j = P["1h"].align(t)
        t1h = P["1h"].trend[j] if j >= 0 else 0
        up = (t15 == 1) + (t1h == 1)
        dn = (t15 == -1) + (t1h == -1)
        live = bar["close"]; e20 = P["15m"].e20[i]
        r = P["15m"].rsi[i]; a = P["15m"].atr[i]
        long_ok = up == 2 and (e20 is None or live > e20)
        short_ok = dn == 2 and (e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        if r is None or not (rsi_lo <= r <= rsi_hi):
            continue
        if a is not None and a > live * atr_max:
            continue
        direction = "long" if long_ok else "short"
        d = 1 if direction == "long" else -1
        stop = live - d * S.STOP_PTS
        tgt = live + d * S.STOP_PTS * rr
        open_t = {"direction": direction, "entry": live, "stop": stop, "target": tgt,
                  "rr": rr, "entry_time": t, "rsi": r, "atr": a}
        cd = 1
    return trades


def show(name, ts, P):
    w = sum(1 for x in ts if x["result"] == "target")
    tot = sum(x["R"] for x in ts)
    L = sum(1 for x in ts if x["direction"] == "long")
    print(f"\n=== {name} ===")
    print(f"  صفقات: {len(ts)} (شراء {L} · بيع {len(ts)-L})")
    if ts:
        print(f"  ربح: {w} · خسارة: {len(ts)-w} · نسبة: {100*w/len(ts):.0f}%")
        print(f"  المجموع: {tot:+.1f}R · لكل صفقة: {tot/len(ts):+.2f}R")
        print()
        for x in ts:
            dd = "شراء 🟢" if x["direction"] == "long" else "بيع 🔴"
            res = "هدف ✅" if x["result"] == "target" else "وقف ❌"
            print(f"    {x['entry_time'][:16]}  {dd}  دخول {x['entry']:.2f} | "
                  f"وقف {x['stop']:.2f} | هدف {x['target']:.2f} | RSI {x['rsi']:.0f} | "
                  f"{res} ({x['R']:+.1f}R)")


if __name__ == "__main__":
    P = {"15m": Live(real("15m")), "1h": Live(real("1h"))}
    print(f"بيانات السبوت الحقيقية: 15د={len(P['15m'].b)} · 1س={len(P['1h'].b)}")
    print(f"من {P['15m'].b[0]['openTime']} إلى {P['15m'].b[-1]['openTime']}")
    ts = simulate_live(P, 35, 55, 0.0025)
    show("الفلتر الجديد: 2/2 + RSI 35-55 + ATR<0.25%", ts, P)
    ts2 = simulate_live(P, 0, 100, 99.0)
    show("للمقارنة — فريمين لحالهن (بلا فلتر)", ts2, P)
