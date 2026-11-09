#!/usr/bin/env python3
"""Robustness pass on the top configs found by search.py.

A grid search over ~50 configs on one sample will always surface a winner; the
question is whether it survives a stricter test. Here we (a) split the sample
into THIRDS and require the edge in every third, and (b) perturb the stop and
the target -- an edge that lives only at one stop size is a fitted artifact.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import search as S  # noqa: E402


def simulate_stop(P, frames, need, flt, rr, stop_pts):
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
                open_t.update(result=res, R=(-1.0 if res == "stop" else open_t["rr"]))
                trades.append(open_t); open_t = None
            continue
        if cd > 0:
            cd -= 1; continue
        tr = {tf: P[tf].trend[P[tf].align(t)] for tf in S.FRAMES}
        up = sum(1 for f in frames if tr.get(f) == 1)
        dn = sum(1 for f in frames if tr.get(f) == -1)
        live = bar["close"]; e20 = P["15m"].e20[i]
        long_ok = up >= need and (e20 is None or live > e20)
        short_ok = dn >= need and (e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        direction = "long" if long_ok else "short"
        if flt and not flt(P, i, direction, live):
            continue
        d = 1 if direction == "long" else -1
        open_t = {"direction": direction, "entry": live,
                  "stop": live - d * stop_pts, "target": live + d * stop_pts * rr,
                  "rr": rr, "entry_time": t}
        cd = 1
    return trades


def thirds(ts, P):
    b = P["15m"].b
    n = len(b)
    cuts = [b[60]["openTime"], b[60 + (n - 60) // 3]["openTime"],
            b[60 + 2 * (n - 60) // 3]["openTime"], b[-1]["openTime"]]
    out = []
    for k in range(3):
        seg = [x for x in ts if cuts[k] <= x["entry_time"] < cuts[k + 1]]
        out.append(S.stat(seg)["r"])
    return out


if __name__ == "__main__":
    P = {tf: S.Pre(tf) for tf in S.FRAMES}
    F2, F6 = ("15m", "1h"), S.FRAMES
    top = [
        ("2/2+rsi38-58+atr<.25%", F2, 2, S.F(rsi_lo=38, rsi_hi=58, atr_max=0.0025)),
        ("4/6+brk10", F6, 4, S.F(brk=10)),
        ("2/2+rsi38-58", F2, 2, S.F(rsi_lo=38, rsi_hi=58)),
        ("2/2+brk10", F2, 2, S.F(brk=10)),
        ("2/2+atr<.25%+notExt1atr", F2, 2, S.F(atr_max=0.0025, not_ext=1.0)),
        ("4/6 (الأصلية)", F6, 4, None),
        ("2/2 (فريمين)", F2, 2, None),
    ]
    print("═══ فحص 3 أقسام + حساسية الوقف/الهدف ═══\n")
    rows = []
    for name, fr, nd, flt in top:
        ts = S.simulate(P, fr, nd, flt, 2.0)
        t3 = thirds(ts, P)
        # حساسية: وقف 6/9/12 نقطة × R:R 1.5/2/2.5
        grid = []
        for sp in (6, 9, 12):
            row = []
            for rr in (1.5, 2.0, 2.5):
                r = S.stat(simulate_stop(P, fr, nd, flt, rr, sp))["r"]
                row.append(r)
            grid.append(row)
        all_ok = all(r > 0 for r in t3)
        rows.append({"name": name, "n": len(ts), "r": S.stat(ts)["r"],
                     "thirds": t3, "grid": grid, "all_ok": all_ok})
        print(f"{name:26} المجموع={S.stat(ts)['r']:+6.1f}R | "
              f"الأثلاث: {t3[0]:+5.1f} {t3[1]:+5.1f} {t3[2]:+5.1f} | "
              f"{'✅ كلها موجبة' if all_ok else '❌ لا'}")
    print("\n═══ حساسية (صفوف=وقف 6/9/12ن، أعمدة=R:R 1.5/2/2.5) ═══")
    for r in rows:
        cells = " ".join(f"{v:+5.1f}" for row in r["grid"] for v in row)
        print(f"  {r['name']:26} {cells}")
    json.dump(rows, open(os.path.join(S.HERE, "robust.json"), "w"),
              ensure_ascii=False, indent=1)
