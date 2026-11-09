#!/usr/bin/env python3
"""Several rule variants over the same bars, so the owner can see the effect."""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


def closed(tf):
    b = bot.bars(tf, 300)
    return sorted([x for x in b if not x.get("isOpen")], key=lambda x: x["openTime"])


def run(b15, b_all, frames, need, use_filter=True, min_gap=1):
    idx = {}
    for tf in b_all:
        j = 0
        while j + 1 < len(b_all[tf]) and b_all[tf][j + 1]["openTime"] <= b15[60]["openTime"]:
            j += 1
        idx[tf] = j
    trades, open_t, cooldown = [], None, 0
    for i in range(60, len(b15)):
        bar = b15[i]; t = bar["openTime"]
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
        if cooldown > 0:
            cooldown -= 1; continue
        tr = {}
        for tf in b_all:
            if tf == "15m":
                tr[tf] = bot.frame_trend(b15[:i + 1]) if i >= 60 else 0
            else:
                tr[tf] = bot.frame_trend(b_all[tf][:idx[tf] + 1]) if idx[tf] >= 60 else 0
        up = sum(1 for f in frames if tr.get(f) == 1)
        dn = sum(1 for f in frames if tr.get(f) == -1)
        live = bar["close"]
        e20 = bot.ema([x["close"] for x in b15[:i + 1]], 20) if i >= 20 else None
        long_ok = up >= need and (not use_filter or e20 is None or live > e20)
        short_ok = dn >= need and (not use_filter or e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        p = bot._sized(live, "long" if long_ok else "short", None)
        p["entry_time"] = t
        open_t = p; cooldown = min_gap
    return trades


def summ(name, ts):
    w = sum(1 for t in ts if t["result"] == "target")
    tot = sum(t["R"] for t in ts)
    longs = sum(1 for t in ts if t["direction"] == "long")
    return {"name": name, "n": len(ts), "longs": longs, "shorts": len(ts) - longs,
            "wins": w, "losses": len(ts) - w,
            "winrate": (100 * w / len(ts)) if ts else 0,
            "total_r": tot, "per_trade": (tot / len(ts)) if ts else 0,
            "trades": [{k: t[k] for k in ("entry_time", "direction", "entry", "stop",
                                          "target", "result", "R")} for t in ts]}


if __name__ == "__main__":
    b15 = closed("15m")
    b_all = {tf: closed(tf) for tf in bot.FRAMES}
    out = []
    out.append(summ("4 من 6 فريمات (الحالي)", run(b15, b_all, bot.FRAMES, 4)))
    out.append(summ("2 من 2: 15د + 1س", run(b15, b_all, ("15m", "1h"), 2)))
    out.append(summ("2 من 2 بلا فلتر EMA", run(b15, b_all, ("15m", "1h"), 2, use_filter=False)))
    out.append(summ("1 من 1: 15د بس", run(b15, b_all, ("15m",), 1)))
    out.append(summ("1 من 1: 1س بس", run(b15, b_all, ("1h",), 1)))
    for r in out:
        print(f"{r['name']:32} صفقات={r['n']:3} (ش={r['longs']} ب={r['shorts']}) "
              f"ربح={r['winrate']:3.0f}% مجموع={r['total_r']:+5.2f}R لكل={r['per_trade']:+.2f}R")
    json.dump(out, open(os.path.join(os.path.dirname(__file__), "variants.json"), "w"),
              ensure_ascii=False, indent=1)
