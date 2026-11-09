#!/usr/bin/env python3
"""Final verdict: base rules + filters, on a 70-day sample, with walk-forward.

Data: Yahoo GC=F (COMEX gold futures) 15m/1h, ~70 days -- a proxy for spot
XAUUSD, so the numbers are indicative. Sizing and grading are the desk's own
($9 risk, R:R 2.0; first touch wins; a bar spanning both takes the stop).

Walk-forward: split the sample in half and report each half, so a result that
only exists in one half is exposed as noise.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import filters as F  # noqa: E402


def run_range(b15, b_all, frames, need, flt, i0, i1):
    """run() but over the 15m index window [i0, i1)."""
    sub = b15[:i1]
    idx = {}
    for tf in b_all:
        j = 0
        while j + 1 < len(b_all[tf]) and b_all[tf][j + 1]["openTime"] <= sub[60]["openTime"]:
            j += 1
        idx[tf] = j
    trades, open_t, cd = [], None, 0
    for i in range(i0, i1):
        bar = sub[i]; t = bar["openTime"]
        for tf in b_all:
            if tf == "15m":
                continue
            while idx[tf] + 1 < len(b_all[tf]) and b_all[tf][idx[tf] + 1]["openTime"] <= t:
                idx[tf] += 1
        if open_t is not None:
            hi, lo = bar["high"], bar["low"]; d = open_t["direction"]
            hs = lo <= open_t["stop"] if d == "long" else hi >= open_t["stop"]
            ht = hi >= open_t["target"] if d == "long" else lo <= open_t["target"]
            if hs or ht:
                res = "stop" if hs else "target"
                open_t.update(exit_time=t, result=res, R=(-1.0 if res == "stop" else open_t["rr"]))
                trades.append(open_t); open_t = None
            continue
        if cd > 0:
            cd -= 1; continue
        tr = {}
        for tf in b_all:
            if tf == "15m":
                tr[tf] = bot.frame_trend(sub[:i + 1]) if i >= 60 else 0
            else:
                tr[tf] = bot.frame_trend(b_all[tf][:idx[tf] + 1]) if idx[tf] >= 60 else 0
        up = sum(1 for f in frames if tr.get(f) == 1)
        dn = sum(1 for f in frames if tr.get(f) == -1)
        live = bar["close"]
        win15 = sub[:i + 1]
        e20 = bot.ema([x["close"] for x in win15], 20)
        long_ok = up >= need and (e20 is None or live > e20)
        short_ok = dn >= need and (e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        direction = "long" if long_ok else "short"
        if flt and not flt(direction, live, win15, b_all["1h"][:idx["1h"] + 1]):
            continue
        p = bot._sized(live, direction, None)
        p["entry_time"] = t
        open_t = p; cd = 1
    return trades


def stat(ts):
    if not ts:
        return 0, 0.0
    w = sum(1 for t in ts if t["result"] == "target")
    return 100 * w / len(ts), sum(t["R"] for t in ts)


if __name__ == "__main__":
    b15 = F.closed("15m")
    b_all = {tf: F.closed(tf) for tf in bot.FRAMES}
    n = len(b15)
    mid = 60 + (n - 60) // 2
    F2, F6 = ("15m", "1h"), bot.FRAMES
    tests = [
        ("4 من 6 (الأصلية)", F6, 4, None),
        ("2 من 2 (15د+1س)", F2, 2, None),
        ("2 من 2 + RSI تراجع", F2, 2, F.f_rsi_pullback),
        ("2 من 2 + اختراق 20 شمعة", F2, 2, F.f_breakout),
        ("2 من 2 + ATR مو مجنون", F2, 2, F.f_atr_calm),
        ("4 من 6 + RSI تراجع", F6, 4, F.f_rsi_pullback),
        ("4 من 6 + اختراق 20 شمعة", F6, 4, F.f_breakout),
        ("4 من 6 + ATR مو مجنون", F6, 4, F.f_atr_calm),
        ("4 من 6 + مو بعيد عن EMA", F6, 4, F.f_not_extended),
    ]
    rows = []
    print(f"العيّنة: {n} شمعة 15د (~{n*15/60/24:.0f} يوم)\n")
    print(f"{'القاعدة':30} {'الكل':>16} {'النصف الأول':>14} {'النصف التاني':>14}")
    print("-" * 80)
    for name, frames, need, flt in tests:
        all_t = run_range(b15, b_all, frames, need, flt, 60, n - 1)
        h1 = run_range(b15, b_all, frames, need, flt, 60, mid)
        h2 = run_range(b15, b_all, frames, need, flt, mid, n - 1)
        wa, ra = stat(all_t)
        w1, r1 = stat(h1)
        w2, r2 = stat(h2)
        rows.append({"name": name, "n": len(all_t), "win": wa, "r": ra,
                     "h1_n": len(h1), "h1_win": w1, "h1_r": r1,
                     "h2_n": len(h2), "h2_win": w2, "h2_r": r2})
        print(f"{name:30} {len(all_t):3}pf {wa:3.0f}% {ra:+6.1f}R "
              f"{len(h1):3}pf {w1:3.0f}% {r1:+5.1f}R  "
              f"{len(h2):3}pf {w2:3.0f}% {r2:+5.1f}R")
    json.dump(rows, open(os.path.join(os.path.dirname(__file__), "final.json"), "w"),
              ensure_ascii=False, indent=1)
