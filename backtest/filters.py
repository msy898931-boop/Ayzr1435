#!/usr/bin/env python3
"""Filter experiments on top of the two-frame (15m+1h) rule.

Base rule: both 15m and 1h trending the same way AND price on the right side of
the 15m EMA20. Each filter below adds ONE condition and we re-measure with the
desk's own sizing ($9 risk, R:R 2.0) and grading (first touch wins; a bar
spanning both takes the stop).
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


def closed(tf):
    """Prefer the 60-day Yahoo cache (bigger sample); fall back to the live feed."""
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"yahoo_{tf}.json")
    if os.path.exists(cache):
        return json.load(open(cache))
    b = bot.bars(tf, 300)
    return sorted([x for x in b if not x.get("isOpen")], key=lambda x: x["openTime"])


def run(b15, b_all, frames, need, flt=None, gap=1):
    idx = {}
    for tf in b_all:
        j = 0
        while j + 1 < len(b_all[tf]) and b_all[tf][j + 1]["openTime"] <= b15[60]["openTime"]:
            j += 1
        idx[tf] = j
    trades, open_t, cd = [], None, 0
    for i in range(60, len(b15) - 1):
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
                open_t.update(exit_time=t, result=res,
                              R=(-1.0 if res == "stop" else open_t["rr"]))
                trades.append(open_t); open_t = None
            continue
        if cd > 0:
            cd -= 1; continue
        tr = {}
        for tf in b_all:
            if tf == "15m":
                tr[tf] = bot.frame_trend(b15[:i + 1]) if i >= 60 else 0
            else:
                tr[tf] = bot.frame_trend(b_all[tf][:idx[tf] + 1]) if idx[tf] >= 60 else 0
        up = sum(1 for f in frames if tr.get(f) == 1)
        dn = sum(1 for f in frames if tr.get(f) == -1)
        live = bar["close"]
        win15 = b15[:i + 1]
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
        open_t = p; cd = gap
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


# ---- the filters ----
def f_rsi_dir(direction, live, b15, b1h):
    r = bot.rsi([x["close"] for x in b15], 14)
    if r is None:
        return True
    return r > 50 if direction == "long" else r < 50


def f_rsi_not_extreme(direction, live, b15, b1h):
    r = bot.rsi([x["close"] for x in b15], 14)
    if r is None:
        return True
    return r < 70 if direction == "long" else r > 30


def f_atr_alive(direction, live, b15, b1h):
    """Skip a dead market: ATR(14) on 15m must be >= 0.05% of price."""
    a = bot.atr(b15, 14)
    if a is None:
        return True
    return a >= live * 0.0005


def f_atr_calm(direction, live, b15, b1h):
    """Skip a wild market: ATR(14) on 15m must be <= 0.25% of price."""
    a = bot.atr(b15, 14)
    if a is None:
        return True
    return a <= live * 0.0025


def f_not_extended(direction, live, b15, b1h):
    """Price must not be more than 1 ATR away from the 15m EMA20."""
    e = bot.ema([x["close"] for x in b15], 20)
    a = bot.atr(b15, 14)
    if e is None or a is None or a == 0:
        return True
    return abs(live - e) <= a


def f_breakout(direction, live, b15, b1h):
    """Price must be at a fresh extreme of the last 20 bars (momentum)."""
    win = b15[-21:-1]
    if len(win) < 20:
        return True
    hi = max(x["high"] for x in win)
    lo = min(x["low"] for x in win)
    return live >= hi if direction == "long" else live <= lo


def f_1h_slope(direction, live, b15, b1h):
    """The 1h EMA20 must itself be sloping the trade's way."""
    if len(b1h) < 25:
        return True
    e_now = bot.ema([x["close"] for x in b1h], 20)
    e_prev = bot.ema([x["close"] for x in b1h[:-3]], 20)
    if e_now is None or e_prev is None:
        return True
    return e_now > e_prev if direction == "long" else e_now < e_prev


def f_rsi_pullback(direction, live, b15, b1h):
    """Buy a dip in an uptrend / sell a pop in a downtrend: RSI 40-55 long."""
    r = bot.rsi([x["close"] for x in b15], 14)
    if r is None:
        return True
    return 38 <= r <= 58


if __name__ == "__main__":
    b15 = closed("15m")
    b_all = {tf: closed(tf) for tf in bot.FRAMES}
    F2 = ("15m", "1h")
    tests = [
        ("الأساس: 2 من 2 (15د+1س)", None),
        ("+ RSI بجهة الصفقة (>50 شراء)", f_rsi_dir),
        ("+ RSI مو متطرّف (<70 شراء)", f_rsi_not_extreme),
        ("+ ATR: السوق مو ميت", f_atr_alive),
        ("+ ATR: السوق مو مجنون", f_atr_calm),
        ("+ السعر مو بعيد عن EMA20", f_not_extended),
        ("+ اختراق قمة/قاع 20 شمعة", f_breakout),
        ("+ ميل 1س EMA20 بجهة الصفقة", f_1h_slope),
        ("+ RSI تراجع (38-58)", f_rsi_pullback),
    ]
    out = []
    for name, f in tests:
        r = summ(name, run(b15, b_all, F2, 2, flt=f))
        out.append(r)
        print(f"{name:34} صفقات={r['n']:3} (ش={r['longs']} ب={r['shorts']}) "
              f"ربح={r['winrate']:3.0f}% مجموع={r['total_r']:+5.2f}R لكل={r['per_trade']:+.2f}R")
    json.dump(out, open(os.path.join(os.path.dirname(__file__), "filters.json"), "w"),
              ensure_ascii=False, indent=1)


def combos():
    """Combined filters + a robustness look at the small sample."""
    b15 = closed("15m")
    b_all = {tf: closed(tf) for tf in bot.FRAMES}
    F2 = ("15m", "1h")

    def both(*fs):
        def f(direction, live, b, h):
            return all(g(direction, live, b, h) for g in fs)
        return f

    tests = [
        ("تراجع RSI + مو بعيد عن EMA", both(f_rsi_pullback, f_not_extended)),
        ("تراجع RSI + ATR حيّ", both(f_rsi_pullback, f_atr_alive)),
        ("تراجع RSI + RSI<70", both(f_rsi_pullback, f_rsi_not_extreme)),
        ("مو بعيد + RSI بجهة الصفقة", both(f_not_extended, f_rsi_dir)),
        ("تراجع RSI + اختراق", both(f_rsi_pullback, f_breakout)),
    ]
    print("\n=== فلاتر مدمجة ===")
    out = []
    for name, f in tests:
        r = summ(name, run(b15, b_all, F2, 2, flt=f))
        out.append(r)
        print(f"{name:34} صفقات={r['n']:3} ربح={r['winrate']:3.0f}% "
              f"مجموع={r['total_r']:+5.2f}R لكل={r['per_trade']:+.2f}R")
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "combo":
    combos()
