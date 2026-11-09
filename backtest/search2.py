#!/usr/bin/env python3
"""Focused grid around the winner, all frames from the same Yahoo cache.

Requires an edge in EVERY third of the sample, so a config that only works in
one stretch is dropped regardless of its headline number.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import search as S  # noqa: E402


def thirds(ts, P):
    b = P["15m"].b; n = len(b)
    cuts = [b[60]["openTime"], b[60 + (n - 60) // 3]["openTime"],
            b[60 + 2 * (n - 60) // 3]["openTime"], b[-1]["openTime"]]
    return [S.stat([x for x in ts if cuts[k] <= x["entry_time"] < cuts[k + 1]])["r"]
            for k in range(3)]


if __name__ == "__main__":
    P = {tf: S.Pre(tf) for tf in S.FRAMES}
    F2, F3 = ("15m", "1h"), ("15m", "1h", "30m")
    bands = [(30, 50), (35, 55), (38, 58), (40, 60), (42, 62), (45, 55), (45, 65)]
    atrs = [None, 0.0020, 0.0025, 0.0030]
    rrs = [1.5, 2.0, 2.5]
    rows = []
    for fname, fr, nd in [("2/2", F2, 2), ("3/3", F3, 3)]:
        for lo, hi in bands:
            for am in atrs:
                kw = {"rsi_lo": lo, "rsi_hi": hi}
                if am:
                    kw["atr_max"] = am
                flt = S.F(**kw)
                for rr in rrs:
                    ts = S.simulate(P, fr, nd, flt, rr)
                    if len(ts) < 40:
                        continue
                    t3 = thirds(ts, P)
                    st = S.stat(ts)
                    rows.append({"name": f"{fname} rsi{lo}-{hi}" +
                                 (f" atr<{am*100:.2f}%" if am else "") + f" rr={rr}",
                                 "n": st["n"], "win": st["win"], "r": st["r"],
                                 "t3": t3, "all_ok": all(x > 0 for x in t3)})
    rows.sort(key=lambda x: (not x["all_ok"], -x["r"]))
    print(f"{'التركيبة':40} {'صفقات':>5} {'ربح':>4} {'المجموع':>8} {'الأثلاث':>18}")
    print("-" * 82)
    for r in rows[:20]:
        t = r["t3"]
        print(f"{r['name']:40} {r['n']:5} {r['win']:3.0f}% {r['r']:+7.1f}R "
              f"{t[0]:+5.1f} {t[1]:+5.1f} {t[2]:+5.1f}  {'✅' if r['all_ok'] else '—'}")
    json.dump(rows, open(os.path.join(S.HERE, "search2.json"), "w"), ensure_ascii=False, indent=1)
