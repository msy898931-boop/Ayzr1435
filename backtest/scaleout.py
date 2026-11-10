#!/usr/bin/env python3
"""Scale-out: the professional way to raise the win rate WITHOUT giving up profit.

Take half off at a first target, move the stop to break-even, let the rest run
to a second target. A trade that reaches the first target can no longer lose
(the stop sits at entry), so the win rate rises while the winners keep a tail.

Conservative grading: a bar that spans both a favourable and an adverse level is
read as the adverse one first.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters as NF  # noqa: E402
import tune as TU  # noqa: E402
import winrate as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def simulate_scale(P, flt, stop_pts, t1, t2, frac=0.5):
    b15 = P.d["15m"].b
    trades, open_t, cd = [], None, 0
    for i in range(60, len(b15) - 1):
        bar = b15[i]; t = bar["openTime"]
        if open_t is not None:
            hi, lo = bar["high"], bar["low"]; d = open_t["direction"]
            sg = 1 if d == "long" else -1
            stop = open_t["stop"]
            t1lvl = open_t["t1lvl"]; t2lvl = open_t["t2lvl"]
            if not open_t["t1hit"]:
                hit_stop = lo <= stop if d == "long" else hi >= stop
                hit_t1 = hi >= t1lvl if d == "long" else lo <= t1lvl
                if hit_stop:            # adverse first (conservative)
                    open_t.update(result="stop", R=-1.0)
                    trades.append(open_t); open_t = None
                elif hit_t1:
                    open_t["t1hit"] = True
                    open_t["stop"] = open_t["entry"]   # break-even
            if open_t is not None and open_t["t1hit"]:
                hit_be = lo <= open_t["stop"] if d == "long" else hi >= open_t["stop"]
                hit_t2 = hi >= t2lvl if d == "long" else lo <= t2lvl
                if hit_t2:
                    R = frac * t1 + (1 - frac) * t2
                    open_t.update(result="target", R=R)
                    trades.append(open_t); open_t = None
                elif hit_be:
                    R = frac * t1          # half booked, half at break-even
                    open_t.update(result="be", R=R)
                    trades.append(open_t); open_t = None
            continue
        if cd > 0:
            cd -= 1; continue
        tr = {tf: P.trend_at(tf, t) for tf in ("15m", "1h")}
        up = sum(1 for f in ("15m", "1h") if tr[f] == 1)
        dn = sum(1 for f in ("15m", "1h") if tr[f] == -1)
        live = bar["close"]; e20 = P.d["15m"].e20[i]
        lo_ok = up >= 2 and (e20 is None or live > e20)
        sh_ok = dn >= 2 and (e20 is None or live < e20)
        if not (lo_ok or sh_ok):
            continue
        direction = "long" if lo_ok else "short"
        if flt and not flt(P.d, i, direction, live):
            continue
        sg = 1 if direction == "long" else -1
        open_t = {"direction": direction, "entry": live,
                  "stop": live - sg * stop_pts,
                  "t1lvl": live + sg * stop_pts * t1,
                  "t2lvl": live + sg * stop_pts * t2,
                  "t1hit": False, "entry_time": t}
        cd = 1
    return trades


def stat(ts):
    if not ts:
        return {"n": 0, "win": 0.0, "r": 0.0, "longs": 0, "shorts": 0}
    w = sum(1 for x in ts if x["R"] > 0)          # break-even counts as win
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
    P = W.Frames("futures")
    Ps = W.Frames("spot")
    f = TU.make_filter(20, 40, 0.0008, 0.0025, "rising")
    print(f"{'وقف':>4} {'هدف1':>6} {'هدف2':>6} {'صفقات':>5} {'ربح%':>6} {'المجموع':>8} {'الأثلاث':>16} {'سبوت':>7}")
    print("-" * 70)
    rows = []
    for stop in (9.0, 12.0):
        for t1 in (0.5, 0.75, 1.0):
            for t2 in (1.5, 2.0, 3.0):
                ts = simulate_scale(P, f, stop, t1, t2)
                s = stat(ts); t3 = thirds(ts, P)
                ss = stat(simulate_scale(Ps, f, stop, t1, t2))
                rows.append({"stop": stop, "t1": t1, "t2": t2, "n": s["n"],
                             "win": s["win"], "r": s["r"], "t3": t3,
                             "spot_r": ss["r"], "spot_n": ss["n"],
                             "longs": s["longs"], "shorts": s["shorts"],
                             "ok": all(x > 0 for x in t3)})
                print(f"{stop:4.0f} {t1:6.2f} {t2:6.2f} {s['n']:5} {s['win']:5.0f}% "
                      f"{s['r']:+7.1f}R {t3[0]:+5.1f}{t3[1]:+6.1f}{t3[2]:+6.1f} "
                      f"{ss['r']:+6.1f}R")
    print("\n═══ الأفضل (ثابت + ربح ≥70%) ═══")
    good = [r for r in rows if r["ok"] and r["win"] >= 70]
    good.sort(key=lambda x: -x["r"])
    for r in good[:8]:
        print(f"  وقف{r['stop']:.0f} هدف1={r['t1']:.2f} هدف2={r['t2']:.2f} | {r['n']}pf "
              f"ربح {r['win']:.0f}% {r['r']:+.1f}R | أثلاث {[round(x,1) for x in r['t3']]} "
              f"| سبوت {r['spot_r']:+.1f}R")
    json.dump(rows, open(os.path.join(HERE, "scaleout.json"), "w"), ensure_ascii=False, indent=1)
