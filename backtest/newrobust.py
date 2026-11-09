#!/usr/bin/env python3
"""Robustness pass on the new filters: thirds on futures, plus the spot result."""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters as NF  # noqa: E402


def thirds(ts, P):
    b = P["15m"].b; n = len(b)
    cuts = [b[60]["openTime"], b[60 + (n - 60) // 3]["openTime"],
            b[60 + 2 * (n - 60) // 3]["openTime"], b[-1]["openTime"]]
    return [NF.stat([x for x in ts if cuts[k] <= x["entry_time"] < cuts[k + 1]])["r"]
            for k in range(3)]


def combo(*fs):
    def f(P, i, d, live):
        return all(g(P, i, d, live) for g in fs)
    return f


if __name__ == "__main__":
    L = "long"
    cands = [
        ("بلا فلتر", None),
        ("adx<40", NF.adx_not_choppy(L, 40)),
        ("adx>20", NF.adx_trending(L, 20)),
        ("atr_band", NF.atr_band(L)),
        ("macd_rising", NF.macd_rising(L)),
        ("stoch_revert", NF.stoch_revert(L)),
        ("adx 20-40", combo(NF.adx_trending(L, 20), NF.adx_not_choppy(L, 40))),
        ("adx<40+atr_band", combo(NF.adx_not_choppy(L, 40), NF.atr_band(L))),
        ("adx<40+macd_rise", combo(NF.adx_not_choppy(L, 40), NF.macd_rising(L))),
        ("atr_band+macd_rise", combo(NF.atr_band(L), NF.macd_rising(L))),
    ]
    P = NF.load_source("futures")
    print("═══ فوتشرز (70 يوم): المجموع + الأثلاث ═══")
    rows = []
    for name, flt in cands:
        ts = NF.simulate(P, flt)
        t3 = thirds(ts, P); s = NF.stat(ts)
        ok = all(x > 0 for x in t3)
        rows.append({"name": name, "n": s["n"], "win": s["win"], "r": s["r"],
                     "t3": t3, "ok": ok})
        print(f"  {name:20} {s['n']:4}pf {s['win']:3.0f}% {s['r']:+6.1f}R | "
              f"{t3[0]:+5.1f} {t3[1]:+5.1f} {t3[2]:+5.1f} {'✅' if ok else '—'}")
    Ps = NF.load_source("spot")
    print("\n═══ سبوت (4 أيام): نفس الفلاتر ═══")
    for name, flt in cands:
        s = NF.stat(NF.simulate(Ps, flt))
        print(f"  {name:20} {s['n']:4}pf {s['win']:3.0f}% {s['r']:+6.1f}R")
    json.dump(rows, open(os.path.join(NF.HERE, "newrobust.json"), "w"),
              ensure_ascii=False, indent=1)
