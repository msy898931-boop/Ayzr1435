#!/usr/bin/env python3
"""Push the win rate up: sweep the STOP size too, not just the target.

A wider stop is hit less often, so the win rate rises -- but the loss when it
comes is bigger in points (the R is still -1). Every row shows win rate AND
total R AND the thirds, so a high win rate that loses money is visible.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters as NF  # noqa: E402
import tune as TU  # noqa: E402
import winrate as W  # noqa: E402


if __name__ == "__main__":
    P = W.Frames("futures")
    Ps = W.Frames("spot")
    filters = [
        ("ADX20-40+ATR+MACD", TU.make_filter(20, 40, 0.0008, 0.0025, "rising")),
        ("ADX20-40+ATR", TU.make_filter(20, 40, 0.0008, 0.0025, "none")),
        ("ADX20-45+ATR+MACD", TU.make_filter(20, 45, 0.0008, 0.0025, "rising")),
    ]
    rows = []
    print(f"{'الفلتر':20} {'وقف':>4} {'هدف':>5} {'صفقات':>5} {'ربح%':>6} {'المجموع':>8} {'الأثلاث':>16}")
    print("-" * 78)
    for name, flt in filters:
        for stop in (9.0, 12.0, 15.0, 20.0, 25.0, 30.0):
            for rr in (0.25, 0.33, 0.4, 0.5, 0.66, 1.0):
                ts = W.simulate(P, flt, rr, stop)
                if len(ts) < 30:
                    continue
                s = W.stat(ts); t3 = W.thirds(ts, P)
                ok = all(x > 0 for x in t3) and s["r"] > 0
                rows.append({"f": name, "stop": stop, "rr": rr, "n": s["n"],
                             "win": s["win"], "r": s["r"], "t3": t3,
                             "longs": s["longs"], "shorts": s["shorts"], "ok": ok})
    # best: high win, positive, stable
    good = [r for r in rows if r["ok"] and r["win"] >= 68]
    good.sort(key=lambda x: (-x["win"], -x["r"]))
    print("\n═══ الأفضل: ربح ≥68% + موجب + ثابت (أثلاث) ═══")
    for r in good[:14]:
        print(f"  {r['f']:20} وقف{r['stop']:>4.0f} هدف{r['rr']:.2f} | {r['n']:3}pf "
              f"ربح {r['win']:.0f}% {r['r']:+6.1f}R | {[round(x,1) for x in r['t3']]} "
              f"| ش{r['longs']}/ب{r['shorts']}")
    # also show top by R with high win
    print("\n═══ الأعلى مجموع R (بشرط ربح ≥65% وثابت) ═══")
    g2 = [r for r in rows if r["ok"] and r["win"] >= 65]
    g2.sort(key=lambda x: -x["r"])
    for r in g2[:8]:
        print(f"  {r['f']:20} وقف{r['stop']:>4.0f} هدف{r['rr']:.2f} | {r['n']:3}pf "
              f"ربح {r['win']:.0f}% {r['r']:+6.1f}R | {[round(x,1) for x in r['t3']]}")
    json.dump(rows, open(os.path.join(W.HERE, "hiwin.json"), "w"), ensure_ascii=False, indent=1)
