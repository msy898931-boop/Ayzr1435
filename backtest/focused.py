#!/usr/bin/env python3
"""Focused sweep on the flagship stack, inside the desk's envelope.

Entry: 15m and 1h agree, price on the right side of EMA20.
Gates: ADX 20-40, ATR 0.08-0.25%, MACD histogram rising, RSI pullback 35-58.
Exit: stop $5-10, target $10-20 (the owner's window).

Reports win rate, total R, $/trade, thirds, months and the spot feed, so a 55%+
win rate is only accepted when it also holds up out of sample.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mega as M  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def f_macd(P, i, d, live, A):
    h = P.d["15m"].macd_hist[i]
    hp = P.d["15m"].macd_hist[i - 1]
    if h is None or hp is None:
        return True
    return h > hp if d == "long" else h < hp


def f_rsi(P, i, d, live, A):
    r = P.d["15m"].rsi[i]
    if r is None:
        return True
    return 35 <= r <= 58 if d == "long" else 42 <= r <= 65


def andf(*fs):
    def g(P, i, d, live, A):
        return all(f(P, i, d, live, A) for f in fs)
    return g


if __name__ == "__main__":
    P = M.Frames("futures")
    Ps = M.Frames("spot")
    A = M.build(P); As = M.build(Ps)
    combos = {
        "adx+atr": M.andf(M.f_adx, M.f_atr) if hasattr(M, "andf") else M.f_adxatr,
        "+macd": andf(M.f_adxatr, f_macd),
        "+rsi": andf(M.f_adxatr, f_rsi),
        "+macd+rsi": andf(M.f_adxatr, f_macd, f_rsi),
    }
    entries = {"trend2": M.e_trend2, "donch20": M.e_donch20, "ema_cross": M.e_ema_cross}
    rows = []
    for en, efn in entries.items():
        for cn, cfn in combos.items():
            for stop in (5, 6, 7, 8, 9, 10):
                for tgt in (10, 11, 12, 13, 14, 16, 18, 20):
                    rr = tgt / stop
                    ts = M.sim(Ps if False else P, A, efn, cfn, float(stop), rr)
                    if len(ts) < 20:
                        continue
                    s = M.stat(ts); bl = M.blocks(ts, P); mok, mo = M.month_ok(ts)
                    ss = M.stat(M.sim(Ps, As, efn, cfn, float(stop), rr))
                    exp = s["win"] / 100 * tgt - (1 - s["win"] / 100) * stop
                    rows.append({"entry": en, "combo": cn, "stop": stop, "tgt": tgt,
                                 "rr": rr, "n": s["n"], "win": s["win"], "r": s["r"],
                                 "exp": exp, "blocks": bl, "mok": mok, "months": mo,
                                 "spot": ss["r"], "spot_n": ss["n"],
                                 "longs": s["longs"], "shorts": s["shorts"],
                                 "ok": all(x > 0 for x in bl) and mok and s["r"] > 0})
    rows.sort(key=lambda x: (not x["ok"], -x["win"], -x["r"]))
    json.dump(rows, open(os.path.join(HERE, "focused.json"), "w"), ensure_ascii=False, indent=1)
    print("═══ الأفضل: ربح ≥55% + مجموع موجب + ثابت (أثلاث+شهور) ═══\n")
    good = [r for r in rows if r["win"] >= 55 and r["r"] > 0 and r["ok"]]
    for r in good[:20]:
        print(f"{r['entry']:10} {r['combo']:10} وقف{r['stop']} هدف{r['tgt']} | {r['n']:3}pf "
              f"ربح {r['win']:.0f}% {r['r']:+6.1f}R {r['exp']:+.2f}$ | ش{r['longs']}/ب{r['shorts']} "
              f"| أثلاث {[round(x,1) for x in r['blocks']]} | سبوت {r['spot']:+.1f}R")
    print("\n═══ الأفضل: ربح ≥55% + مجموع موجب (حتى لو ما ثابت) ═══\n")
    g2 = [r for r in rows if r["win"] >= 55 and r["r"] > 0]
    g2.sort(key=lambda x: (-x["win"], -x["r"]))
    for r in g2[:20]:
        print(f"{r['entry']:10} {r['combo']:10} وقف{r['stop']} هدف{r['tgt']} | {r['n']:3}pf "
              f"ربح {r['win']:.0f}% {r['r']:+6.1f}R {r['exp']:+.2f}$ | "
              f"أثلاث {[round(x,1) for x in r['blocks']]} | سبوت {r['spot']:+.1f}R")
