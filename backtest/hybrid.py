#!/usr/bin/env python3
"""Hybrid entry test: does switching mode by ADX beat a single mode?

The idea the owner asked about: enter at market in a TREND (price will not
retrace) and on a pullback in a RANGE (price will). The switch is ADX(15m).

This measures it honestly against both single modes, on:
  * the full 2026 year (backtest/bot2026.py signals), and
  * the owner's REAL 38-trade week (backtest/week_real.json).

It also reports the SHARE of trades taken immediate, because a "hybrid" that
takes 2% immediate is just the pullback wearing a hybrid label.

Result (2026-11-13): the hybrid NEVER beat the best single mode. On the year the
best "hybrid" (ADX>=40) was 98% pullback -- an artefact, not a blend; every real
blend was worse than pullback-alone. On the owner's week every threshold was
worse than immediate-alone (+9.40R at best vs +12.40R). ADX is a lagging,
noisy regime proxy: it does not separate "will retrace" from "will run".
"""
from __future__ import annotations
import bisect
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot2026 as B   # noqa: E402
import entry_timing as ET  # noqa: E402
import week_compare as WC  # noqa: E402

FRAC = 0.5


def year_hybrid(thr):
    """Sequential year sim: immediate if ADX(15m) >= thr, else pullback."""
    raw, trend, times, e20 = B.load()
    b5 = raw["5m"]
    sigs = ET.signals(raw, trend, times, e20, b5)
    adx15 = B._adx(raw["15m"])
    t15 = times["15m"]

    def adx_at(t):
        j = B._at(t15, t) - 1
        return adx15[j] if j >= 0 else None

    tot = 0.0
    imm = pb = 0
    i = 0
    n = len(sigs)
    while i < n:
        s = sigs[i]
        a = adx_at(s["t"])
        mode = "immediate" if (a is not None and a >= thr) else "pullback"
        d = -1 if s["direction"] == "short" else 1
        bi = s["i"]
        entry = entry_i = None
        same_bar = False
        if mode == "immediate":
            entry, entry_i, same_bar = s["price"], bi, False
        else:
            atr = s["atr15"] or 8.0
            limit = s["price"] - d * FRAC * atr
            for k in range(bi + 1, min(bi + 1 + ET.WINDOW, len(b5))):
                bar = b5[k]
                if (d == -1 and bar["high"] >= limit) or \
                   (d == 1 and bar["low"] <= limit):
                    entry, entry_i, same_bar = limit, k, True
                    break
        if entry is None:
            i += 1
            continue
        stop = entry - d * ET.STOP_USD
        target = entry + d * ET.STOP_USD * ET.RR
        R, exit_t, _ = ET._exit_R(b5, entry_i, entry, s["direction"], stop,
                                  target, same_bar)
        if R is None:
            i += 1
            continue
        tot += R
        imm += mode == "immediate"
        pb += mode == "pullback"
        ei = next((k for k in range(entry_i, len(b5))
                   if b5[k]["openTime"] == exit_t), entry_i)
        while i < n and sigs[i]["i"] <= ei:
            i += 1
    return tot, imm, pb


def week_hybrid(thr):
    """The owner's real week: immediate if ADX(15m) >= thr, else pullback."""
    frames, real = WC.load()
    real = [t for t in real if t.get("status") == "closed"
            and t.get("r_multiple") is not None]
    b = frames["15m"]

    def adx_at(t):
        idx = bisect.bisect_right([x["openTime"] for x in b], t) - 1
        closed = [x for x in b[: idx + 1]
                  if (WC._t(x["openTime"]) + dt.timedelta(seconds=900)) <= WC._t(t)]
        a = B._adx(closed)
        return a[-1] if a else None

    tot = 0.0
    imm = pb = 0
    for tr in real:
        a = adx_at(tr["opened_at"])
        if a is not None and a >= thr:
            r, _, _ = WC.walk(frames["5m"], tr["opened_at"], tr["entry"],
                              tr["direction"], tr["stop"], tr["target"])
            tot += r or 0
            imm += 1
        else:
            p = WC.pullback(frames, tr["opened_at"], tr)
            tot += p["R"] or 0
            pb += 1
    return tot, imm, pb


def main():
    print("السنة: فوري +93.6R · ارتداد +152.8R")
    print("هجين (فوري إذا ADX≥عتبة، ارتداد إذا أقل):")
    for thr in (20, 25, 30, 33, 40):
        net, imm, pb = year_hybrid(thr)
        share = imm / (imm + pb) * 100 if (imm + pb) else 0
        print(f"  عتبة {thr:>2}: {net:+7.1f}R · فوري {imm:>3} · ارتداد {pb:>3} "
              f"({share:.0f}% فوري)")
    print("\nأسبوع المالك: فوري +12.40R · ارتداد -0.13R")
    print("هجين:")
    for thr in (20, 25, 30, 33, 40):
        net, imm, pb = week_hybrid(thr)
        print(f"  عتبة {thr:>2}: {net:+6.2f}R · فوري {imm:>2} · ارتداد {pb:>2}")


if __name__ == "__main__":
    main()
