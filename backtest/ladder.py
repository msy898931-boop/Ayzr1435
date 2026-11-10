#!/usr/bin/env python3
"""How many trades do we want? A selectivity ladder.

Starts from the flagship stack (very selective, ~2 trades a day) and removes one
gate at a time until almost every bar is a trade. For each rung it reports trades
per day, win rate, total R and $/trade, so the cost of trading more is visible.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mega as M  # noqa: E402
import focused as FO  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def sim2(P, A, entry, flt, stop, tgt, cd=1, allow_both=True):
    b15 = P.d["15m"].b
    trades, open_t, cool = [], None, 0
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
        if cool > 0:
            cool -= 1; continue
        d = entry(P, i, A)
        if d is None:
            continue
        live = bar["close"]
        if flt and not flt(P, i, d, live, A):
            continue
        sg = 1 if d == "long" else -1
        open_t = {"direction": d, "entry": live, "stop": live - sg * stop,
                  "target": live + sg * stop * (tgt / stop), "rr": tgt / stop,
                  "entry_time": t}
        cool = cd
    return trades


def report(name, P, A, entry, flt, stop=9, tgt=11, cd=1):
    ts = sim2(P, A, entry, flt, stop, tgt, cd)
    days = len(set(x["openTime"][:10] for x in P.d["15m"].b[60:]))
    s = M.stat(ts)
    bl = M.blocks(ts, P); mok, mo = M.month_ok(ts)
    exp = s["win"] / 100 * tgt - (1 - s["win"] / 100) * stop
    per_day = s["n"] / days if days else 0
    ok = all(x > 0 for x in bl) and mok
    return {"name": name, "n": s["n"], "days": days, "per_day": per_day,
            "win": s["win"], "r": s["r"], "exp": exp, "blocks": bl,
            "spot": None, "ok": ok, "longs": s["longs"], "shorts": s["shorts"]}


if __name__ == "__main__":
    P = M.Frames("futures"); Ps = M.Frames("spot")
    A = M.build(P); As = M.build(Ps)
    full = FO.andf(M.f_adxatr, FO.f_macd, FO.f_rsi)
    no_rsi = FO.andf(M.f_adxatr, FO.f_macd)
    no_macd = M.f_adxatr
    only_adx = M.f_adx
    only_atr = M.f_atr
    rungs = [
        ("1. كامل (فلاغشب)", M.e_trend2, full, 1),
        ("2. بلا RSI", M.e_trend2, no_rsi, 1),
        ("3. بلا RSI+MACD", M.e_trend2, no_macd, 1),
        ("4. ADX بس", M.e_trend2, only_adx, 1),
        ("5. ATR بس", M.e_trend2, only_atr, 1),
        ("6. بلا فلاتر", M.e_trend2, None, 1),
        ("7. بلا فلاتر + بلا تبريد", M.e_trend2, None, 0),
        ("8. اتجاه 15د بس", lambda P,i,A: ("long" if P.d["15m"].trend[i]==1 else ("short" if P.d["15m"].trend[i]==-1 else None)), None, 0),
    ]
    rows = []
    print(f"{'المستوى':26} {'أيام':>5} {'صفقات':>6} {'صفقة/يوم':>9} {'ربح%':>6} "
          f"{'المجموع':>8} {'$/صفقة':>7} {'ثابت':>5}")
    print("-" * 84)
    for name, entry, flt, cd in rungs:
        r = report(name, P, A, entry, flt, 9, 11, cd)
        rows.append(r)
        print(f"{name:26} {r['days']:5} {r['n']:6} {r['per_day']:8.1f} {r['win']:5.0f}% "
              f"{r['r']:+7.1f}R {r['exp']:+6.2f}$ {'✅' if r['ok'] else '—':>4}")
    json.dump(rows, open(os.path.join(HERE, "ladder.json"), "w"), ensure_ascii=False, indent=1)
