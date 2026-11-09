#!/usr/bin/env python3
"""Fine-tune the ADX/ATR/MACD filter family on the 70-day futures sample.

Sweeps the ADX band, the ATR band and the MACD mode, and reports each config on
THIRDS of the sample and on each MONTH. A config is 'stable' only if every third
and every month is positive -- otherwise a big headline number is just one lucky
stretch. The winner is read as a REGION, not a single peak.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters as NF  # noqa: E402


def thirds(ts, P):
    b = P["15m"].b; n = len(b)
    cuts = [b[60]["openTime"], b[60 + (n - 60) // 3]["openTime"],
            b[60 + 2 * (n - 60) // 3]["openTime"], b[-1]["openTime"]]
    return [NF.stat([x for x in ts if cuts[k] <= x["entry_time"] < cuts[k + 1]])["r"]
            for k in range(3)]


def months(ts):
    m = collections.defaultdict(float)
    for x in ts:
        m[x["entry_time"][:7]] += x["R"]
    return m


def make_filter(adx_lo, adx_hi, atr_lo, atr_hi, macd_mode):
    def f(P, i, d, live):
        a = P["15m"].adx[i]
        if a is None or not (adx_lo <= a <= adx_hi):
            return False
        at = P["15m"].atr[i]
        if at is None or not (atr_lo <= at / live <= atr_hi):
            return False
        if macd_mode == "rising":
            h, hp = P["15m"].macd_hist[i], P["15m"].macd_hist[i - 1]
            if h is None or hp is None:
                return False
            return h > hp if d == "long" else h < hp
        if macd_mode == "dir":
            h = P["15m"].macd_hist[i]
            if h is None:
                return False
            return h >= 0 if d == "long" else h <= 0
        return True
    return f


if __name__ == "__main__":
    P = NF.load_source("futures")
    Ps = NF.load_source("spot")
    adx_los = [15, 20, 25, 30]
    adx_his = [35, 40, 45, 50]
    atr_bands = [(0.0006, 0.0030), (0.0008, 0.0025), (0.0010, 0.0022),
                 (0.0008, 0.0035), (0.0012, 0.0030)]
    macds = ["none", "rising", "dir"]
    rows = []
    for lo in adx_los:
        for hi in adx_his:
            if lo >= hi:
                continue
            for alo, ahi in atr_bands:
                for mm in macds:
                    flt = make_filter(lo, hi, alo, ahi, mm)
                    ts = NF.simulate(P, flt)
                    if len(ts) < 40:
                        continue
                    s = NF.stat(ts); t3 = thirds(ts, P); mo = months(ts)
                    pos_m = sum(1 for v in mo.values() if v > 0)
                    rows.append({
                        "name": f"adx{lo}-{hi} atr{alo}-{ahi} {mm}",
                        "adx": (lo, hi), "atr": (alo, ahi), "macd": mm,
                        "n": s["n"], "win": s["win"], "r": s["r"], "t3": t3,
                        "months": dict(mo), "pos_m": pos_m, "n_m": len(mo),
                        "ok3": all(x > 0 for x in t3),
                        "okm": pos_m == len(mo)})
    rows.sort(key=lambda x: (not (x["ok3"] and x["okm"]), -x["r"]))
    print(f"جُرّب {len(rows)} تركيبة\n")
    print(f"{'ADX':9} {'ATR band':16} {'MACD':7} {'pf':>4} {'ربح':>4} {'المجموع':>8} "
          f"{'الأثلاث':>16} {'شهور+':>6} {'ثابت':>5}")
    print("-" * 92)
    for x in rows[:24]:
        t = x["t3"]
        print(f"{str(x['adx']):9} {str(x['atr']):16} {x['macd']:7} {x['n']:4} {x['win']:3.0f}% "
              f"{x['r']:+7.1f}R {t[0]:+5.1f}{t[1]:+6.1f}{t[2]:+6.1f} "
              f"{x['pos_m']}/{x['n_m']:<4} {'✅' if x['ok3'] and x['okm'] else '—':>4}")
    json.dump(rows, open(os.path.join(NF.HERE, "tune.json"), "w"), ensure_ascii=False, indent=1)
