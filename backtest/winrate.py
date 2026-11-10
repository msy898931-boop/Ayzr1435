#!/usr/bin/env python3
"""Chase a high win rate honestly: sweep target/stop and filters, both sides.

Win rate is not free -- a closer target wins more often but pays less. So every
row reports the win rate AND the total R, so a high win rate bought with a bad
payoff is visible. Grading is the desk's own: first touch wins, a bar spanning
both takes the stop. Long and short are both traded.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import newfilters as NF  # noqa: E402
import tune as TU  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PV = bot.POINT_VALUE


class Frames:
    def __init__(self, source):
        self.d = {}
        if source == "spot":
            raw = {tf: sorted([x for x in bot.bars(tf, 300) if not x.get("isOpen")],
                              key=lambda x: x["openTime"])
                   for tf in ("15m", "1h")}
        else:
            raw = {tf: json.load(open(os.path.join(HERE, f"yahoo_{tf}.json")))
                   for tf in ("15m", "1h")}
        for tf, b in raw.items():
            self.d[tf] = NF.Data(b, source)

    def trend_at(self, tf, t):
        f = self.d[tf]; j = f.align(t)
        return f.trend[j] if j >= 0 else 0


def simulate(P, flt, rr, stop_pts, frames=("15m", "1h"), need=2):
    b15 = P.d["15m"].b
    trades, open_t, cd = [], None, 0
    for i in range(60, len(b15) - 1):
        bar = b15[i]; t = bar["openTime"]
        if open_t is not None:
            hi, lo = bar["high"], bar["low"]; d = open_t["direction"]
            hs = lo <= open_t["stop"] if d == "long" else hi >= open_t["stop"]
            ht = hi >= open_t["target"] if d == "long" else lo <= open_t["target"]
            if hs or ht:
                res = "stop" if hs else "target"
                open_t.update(result=res, R=(-1.0 if res == "stop" else open_t["rr"]))
                trades.append(open_t); open_t = None
            continue
        if cd > 0:
            cd -= 1; continue
        tr = {tf: P.trend_at(tf, t) for tf in frames}
        up = sum(1 for f in frames if tr[f] == 1)
        dn = sum(1 for f in frames if tr[f] == -1)
        live = bar["close"]; e20 = P.d["15m"].e20[i]
        lo_ok = up >= need and (e20 is None or live > e20)
        sh_ok = dn >= need and (e20 is None or live < e20)
        if not (lo_ok or sh_ok):
            continue
        direction = "long" if lo_ok else "short"
        if flt and not flt(P.d, i, direction, live):
            continue
        dd = 1 if direction == "long" else -1
        open_t = {"direction": direction, "entry": live,
                  "stop": live - dd * stop_pts, "target": live + dd * stop_pts * rr,
                  "rr": rr, "entry_time": t}
        cd = 1
    return trades


def stat(ts):
    if not ts:
        return {"n": 0, "win": 0.0, "r": 0.0, "longs": 0, "shorts": 0}
    w = sum(1 for x in ts if x["result"] == "target")
    return {"n": len(ts), "win": 100 * w / len(ts), "r": sum(x["R"] for x in ts),
            "longs": sum(1 for x in ts if x["direction"] == "long"),
            "shorts": sum(1 for x in ts if x["direction"] == "short")}


def thirds(ts, P):
    b = P.d["15m"].b; n = len(b)
    cuts = [b[60]["openTime"], b[60 + (n - 60) // 3]["openTime"],
            b[60 + 2 * (n - 60) // 3]["openTime"], b[-1]["openTime"]]
    return [stat([x for x in ts if cuts[k] <= x["entry_time"] < cuts[k + 1]])["r"]
            for k in range(3)]


if __name__ == "__main__":
    P = Frames("futures")
    stack = TU.make_filter(20, 40, 0.0008, 0.0025, "rising")
    no_macd = TU.make_filter(20, 40, 0.0008, 0.0025, "none")
    configs = [
        ("فريمين بس", None),
        ("+ ADX20-40+ATR", no_macd),
        ("+ ADX20-40+ATR+MACD", stack),
        ("+ ATR band بس", NF.atr_band("long")),
    ]
    print("البحث عن نسبة ربح عالية (هدف قريب = ربح أكتر لكن عائد أقل)\n")
    print(f"{'الفلتر':22} {'هدف':>5} {'صفقات':>5} {'شراء':>4} {'بيع':>4} {'ربح%':>6} "
          f"{'المجموع':>8} {'الأثلاث':>16}")
    print("-" * 84)
    best = []
    for name, flt in configs:
        for rr in (0.4, 0.5, 0.6, 0.75, 1.0, 1.5, 2.0):
            ts = simulate(P, flt, rr, 9.0)
            s = stat(ts); t3 = thirds(ts, P)
            print(f"{name:22} {rr:5.2f} {s['n']:5} {s['longs']:4} {s['shorts']:4} "
                  f"{s['win']:5.0f}% {s['r']:+7.1f}R "
                  f"{t3[0]:+5.1f}{t3[1]:+6.1f}{t3[2]:+6.1f}")
            best.append({"filter": name, "rr": rr, "n": s["n"], "win": s["win"],
                         "r": s["r"], "t3": t3, "longs": s["longs"], "shorts": s["shorts"]})
    print()
    hi = [b for b in best if b["win"] >= 60 and b["r"] > 0 and all(x > 0 for x in b["t3"])]
    print("═══ تركيبات ربحها ≥60% ومجموعها موجب وثابتة ═══")
    for b in sorted(hi, key=lambda x: -x["win"])[:10]:
        print(f"  {b['filter']:22} هدف {b['rr']:.2f} | {b['n']}pf ربح {b['win']:.0f}% "
              f"{b['r']:+.1f}R | {[round(x,1) for x in b['t3']]}")
    json.dump(best, open(os.path.join(HERE, "winrate.json"), "w"), ensure_ascii=False, indent=1)
