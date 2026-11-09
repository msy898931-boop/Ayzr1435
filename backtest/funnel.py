#!/usr/bin/env python3
"""Funnel + band sweep for the new filter, on the live spot feed.

Shows how many bars survive each stage, so the 'why only N trades' question has
a number instead of a guess.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import today_test as T  # noqa: E402


def funnel(P, rsi_lo, rsi_hi, atr_max):
    b15 = P["15m"].b
    n = agree = rsi_ok = atr_ok = both = 0
    for i in range(60, len(b15) - 1):
        bar = b15[i]; t = bar["openTime"]
        t15 = P["15m"].trend[i]
        j = P["1h"].align(t)
        t1h = P["1h"].trend[j] if j >= 0 else 0
        if not ((t15 == 1 and t1h == 1) or (t15 == -1 and t1h == -1)):
            continue
        n += 1
        agree += 1
        live = bar["close"]; e20 = P["15m"].e20[i]
        ok_dir = (live > e20) if t15 == 1 else (live < e20)
        if not ok_dir:
            continue
        r = P["15m"].rsi[i]; a = P["15m"].atr[i]
        if r is not None and rsi_lo <= r <= rsi_hi:
            rsi_ok += 1
        if a is not None and a <= live * atr_max:
            atr_ok += 1
        if r is not None and rsi_lo <= r <= rsi_hi and (a is None or a <= live * atr_max):
            both += 1
    return {"agree": agree, "rsi_ok": rsi_ok, "atr_ok": atr_ok, "both": both}


if __name__ == "__main__":
    P = {"15m": T.Live(T.real("15m")), "1h": T.Live(T.real("1h"))}
    print(f"البيانات الحقيقية (سبوت): {len(P['15m'].b)} شمعة 15د\n")
    print("═══ عدّاد المراحل: كم شمعة تعدّي كل شرط ═══")
    print(f"  شمعات الفريمين متفقين + السعر بجهة صحيحة: {funnel(P,0,100,99.0)['agree']}")
    f = funnel(P, 35, 55, 0.0025)
    print(f"    منها RSI 35-55: {f['rsi_ok']}")
    print(f"    منها ATR<0.25%: {f['atr_ok']}")
    print(f"    منها الاثنين معاً: {f['both']}")
    print()
    print("═══ مسح نطاقات RSI (كل واحد يشغّل الصفقات فعلياً) ═══")
    print(f"  {'نطاق RSI':16} {'صفقات':>5} {'شراء':>5} {'بيع':>5} {'ربح':>5} {'المجموع':>8}")
    for lo, hi in [(0,100),(30,70),(35,55),(40,60),(45,65),(45,55),(50,70),(55,100),(0,45)]:
        ts = T.simulate_live(P, lo, hi, 0.0025)
        L = sum(1 for x in ts if x["direction"] == "long")
        w = sum(1 for x in ts if x["result"] == "target")
        tot = sum(x["R"] for x in ts)
        lbl = "بلا فلتر" if (lo,hi)==(0,100) else f"{lo}-{hi}"
        print(f"  {lbl:16} {len(ts):5} {L:5} {len(ts)-L:5} {w:5} {tot:+7.1f}R")
