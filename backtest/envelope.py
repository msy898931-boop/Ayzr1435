#!/usr/bin/env python3
"""Search inside the desk's OWN envelope: stop $5-10, target $10-20.

That is R:R 1.0 to 4.0 on a $5-10 stop. Both sides traded. Every config is
scored on thirds AND months of the 70-day sample, plus the 4-day spot feed, so a
config that only works in one stretch is visible.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters as NF  # noqa: E402
import tune as TU  # noqa: E402
import winrate as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def months_r(ts):
    m = collections.defaultdict(float)
    for x in ts:
        m[x["entry_time"][:7]] += x["R"]
    return m


if __name__ == "__main__":
    P = W.Frames("futures")
    Ps = W.Frames("spot")
    filters = {
        "ADX20-40+ATR+MACD": TU.make_filter(20, 40, 0.0008, 0.0025, "rising"),
        "ADX20-40+ATR": TU.make_filter(20, 40, 0.0008, 0.0025, "none"),
        "ADX20-45+ATR+MACD": TU.make_filter(20, 45, 0.0008, 0.0025, "rising"),
        "ADX15-40+ATR+MACD": TU.make_filter(15, 40, 0.0008, 0.0030, "rising"),
    }
    stops = [5, 6, 7, 8, 9, 10]
    targets = [10, 12, 14, 16, 18, 20]
    rows = []
    for fname, flt in filters.items():
        for stop in stops:
            for tgt in targets:
                rr = tgt / stop
                ts = W.simulate(P, flt, rr, float(stop))
                if len(ts) < 30:
                    continue
                s = W.stat(ts); t3 = W.thirds(ts, P); mo = months_r(ts)
                pm = sum(1 for v in mo.values() if v > 0); nm = len(mo)
                ss = W.stat(W.simulate(Ps, flt, rr, float(stop)))
                exp = s["win"] / 100 * tgt - (1 - s["win"] / 100) * stop
                rows.append({"f": fname, "stop": stop, "tgt": tgt, "rr": rr,
                             "n": s["n"], "win": s["win"], "r": s["r"], "t3": t3,
                             "pm": pm, "nm": nm, "spot": ss["r"], "spot_n": ss["n"],
                             "exp": exp, "longs": s["longs"], "shorts": s["shorts"],
                             "ok": all(x > 0 for x in t3) and pm == nm and s["r"] > 0})
    rows.sort(key=lambda x: (not x["ok"], -x["exp"]))
    print(f"جُرّب {len(rows)} توليفة داخل مدى الوقف $5-10 والهدف $10-20\n")
    print(f"{'الفلتر':20} {'وقف':>4} {'هدف':>4} {'R:R':>5} {'صفقات':>5} {'ربح%':>6} "
          f"{'المجموع':>8} {'$/صفقة':>7} {'ثابت':>5}")
    print("-" * 78)
    for r in rows[:22]:
        print(f"{r['f']:20} {r['stop']:4} {r['tgt']:4} {r['rr']:5.2f} {r['n']:5} "
              f"{r['win']:5.0f}% {r['r']:+7.1f}R {r['exp']:+6.2f}$ "
              f"{'✅' if r['ok'] else '—':>4}")
    print("\n═══ الأفضل: ثابت (أثلاث+شهور) مرتّب حسب $/صفقة ═══")
    good = [r for r in rows if r["ok"]]
    for r in good[:12]:
        print(f"  {r['f']:20} وقف${r['stop']} هدف${r['tgt']} (R:R {r['rr']:.2f}) | "
              f"{r['n']}pf ربح {r['win']:.0f}% {r['r']:+.1f}R {r['exp']:+.2f}$/صفقة "
              f"| سبوت {r['spot']:+.1f}R")
    json.dump(rows, open(os.path.join(HERE, "envelope.json"), "w"), ensure_ascii=False, indent=1)
