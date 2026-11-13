#!/usr/bin/env python3
"""A/B/C experiment: does WAITING for a better entry beat entering at market?

The owner asked to make the desk enter the way a human would -- sell the
PULLBACK, not the middle of the range. That is a hypothesis, not a fact, so we
measure it on the same year of bars, with the SAME signal, the SAME gates and
the SAME stop distance; the ONLY thing that differs is WHEN the entry fires.

Modes (all start from the identical signal bar):
  immediate     -- enter at the signal bar's close (what the desk does now).
  pullback_Xatr -- a limit X*ATR(15m) above (short) / below (long) the signal
                   price; wait up to WINDOW bars for the retrace to fill.
  break_N       -- wait for price to break the prior N-bar swing (continuation).

Honesty guards baked in (a comparison that flatters one mode is worthless):
  * the 15m filter reads the last CLOSED bar (as the live desk does);
  * one trade at a time;
  * a bar spanning both stop and target takes the stop;
  * the FILL BAR itself is checked for a same-bar stop -- a limit that fills
    into a spike must not be booked as a clean entry;
  * results are reported for the FULL year AND both halves, so a mode that only
    wins in one regime is not sold as an edge.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot2026 as B  # noqa: E402

FRAMES = B.FRAMES
CONFIRM = B.CONFIRM
FILTER_TF = B.FILTER_TF
STOP_USD = B.STOP_USD
RR = B.RR
WINDOW = 12          # bars (5m) to wait for a pullback/break = 1 hour


def signals(raw, trend, times, e20, b5):
    """Every bar where the desk's signal fires (direction + gates pass)."""
    t15 = times["15m"]
    adx15 = B._adx(raw["15m"])
    atr15s = B._atr_series(raw["15m"])
    macd15 = B._macd_hist(raw["15m"])
    rsi15 = B._rsi_series([x["close"] for x in raw["15m"]])
    nc_times = times.get(B.NOCHASE_TF, t15)
    nc_rsi = rsi15 if B.NOCHASE_TF == "15m" else \
        B._rsi_series([x["close"] for x in raw[B.NOCHASE_TF]])

    out = []
    for i in range(300, len(b5) - 1):
        bar = b5[i]
        t = bar["openTime"]
        live = bar["close"]
        up = dn = 0
        for tf in FRAMES:
            j = B._at(times[tf], t)
            if j < 0:
                continue
            v = trend[tf][j]
            up += v == 1
            dn += v == -1
        direction = "long" if up >= CONFIRM else "short" if dn >= CONFIRM else None
        if direction is None:
            continue
        jc = B._at(times[FILTER_TF], t) - 1
        e = e20[FILTER_TF][jc] if jc >= 0 else None
        if e is not None:
            if direction == "long" and live <= e:
                continue
            if direction == "short" and live >= e:
                continue
        j15 = B._at(t15, t) - 1
        if j15 >= 0 and not B._gate_ok(direction, j15, live, adx15, atr15s,
                                       macd15, rsi15):
            continue
        if B.NOCHASE_ON and direction in B.NOCHASE_SIDES:
            jn = B._at(nc_times, t) - 1
            if 0 <= jn < len(nc_rsi):
                r = nc_rsi[jn]
                if r is not None:
                    if direction == "short" and r < B.NOCHASE_RSI:
                        continue
                    if direction == "long" and r > 100.0 - B.NOCHASE_RSI:
                        continue
        a15 = atr15s[j15] if j15 >= 0 else None
        out.append({"i": i, "t": t, "price": live, "direction": direction,
                    "atr15": a15})
    return out


def _exit_R(b5, entry_i, entry, direction, stop, target, same_bar=False):
    """First touch wins; a bar spanning both takes the stop.

    If `same_bar`, the fill bar (entry_i) is checked too -- a limit that filled
    into a spike that also reached the stop is a loss, not a clean entry.
    Returns (R, exit_time, result) or (None, None, None)."""
    d = 1 if direction == "long" else -1
    start = entry_i if same_bar else entry_i + 1
    for j in range(start, len(b5)):
        bar = b5[j]
        hi, lo = bar["high"], bar["low"]
        hs = lo <= stop if d == 1 else hi >= stop
        ht = hi >= target if d == 1 else lo <= target
        if hs or ht:
            res = "stop" if hs else "target"
            return (-1.0 if res == "stop" else RR), bar["openTime"], res
    return None, None, None


def run(mode, raw, trend, times, e20, b5, sigs):
    trades = []
    i = 0
    n = len(sigs)
    while i < n:
        s = sigs[i]
        bi = s["i"]
        direction = s["direction"]
        d = 1 if direction == "long" else -1
        entry = entry_i = None
        same_bar = False
        if mode == "immediate":
            entry, entry_i, same_bar = s["price"], bi, False
        elif mode.startswith("pullback"):
            frac = float(mode.split("_")[1].replace("atr", ""))
            a = s["atr15"] or 8.0
            limit = s["price"] - d * frac * a       # short: limit ABOVE price
            for k in range(bi + 1, min(bi + 1 + WINDOW, len(b5))):
                bar = b5[k]
                if (d == -1 and bar["high"] >= limit) or \
                   (d == 1 and bar["low"] <= limit):
                    entry, entry_i, same_bar = limit, k, True
                    break
        elif mode.startswith("break"):
            look = int(mode.split("_")[1])
            prior = [b5[k]["low"] if d == -1 else b5[k]["high"]
                     for k in range(bi - look, bi)]
            lvl = min(prior) if d == -1 else max(prior)
            for k in range(bi + 1, min(bi + 1 + WINDOW, len(b5))):
                bar = b5[k]
                if (d == -1 and bar["close"] <= lvl) or \
                   (d == 1 and bar["close"] >= lvl):
                    entry, entry_i, same_bar = bar["close"], k, True
                    break
        if entry is None:
            i += 1
            continue
        stop = entry - d * STOP_USD
        target = entry + d * STOP_USD * RR
        R, exit_t, res = _exit_R(b5, entry_i, entry, direction, stop, target,
                                 same_bar)
        if R is None:
            i += 1
            continue
        trades.append({"mode": mode, "dir": direction, "entry": entry,
                       "entry_t": b5[entry_i]["openTime"], "R": R, "res": res,
                       "exit_t": exit_t})
        ei = next((k for k in range(entry_i, len(b5))
                   if b5[k]["openTime"] == exit_t), entry_i)
        while i < n and sigs[i]["i"] <= ei:
            i += 1
    return trades


def stats(trades):
    wins = [t for t in trades if t["res"] == "target"]
    losses = [t for t in trades if t["res"] == "stop"]
    netR = sum(t["R"] for t in trades)
    peak = eq = 0.0
    maxdd = 0.0
    streak = maxstreak = 0
    for t in trades:
        eq += t["R"]
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
        streak = streak + 1 if t["res"] == "stop" else 0
        maxstreak = max(maxstreak, streak)
    wr = len(wins) / len(trades) * 100 if trades else 0
    return {"n": len(trades), "wins": len(wins), "losses": len(losses),
            "netR": netR, "wr": wr, "maxdd": maxdd, "maxloss_streak": maxstreak,
            "avgR": netR / len(trades) if trades else 0}


def fmt(tag, s):
    return (f"  {tag:18s} صفقات {s['n']:4d} · فوز {s['wr']:5.1f}% · "
            f"صافي {s['netR']:+7.1f}R · متوسط {s['avgR']:+.3f}R · "
            f"أقصى تراجع {s['maxdd']:6.1f}R · خسائر متتالية {s['maxloss_streak']}")


def main():
    raw, trend, times, e20 = B.load()
    b5 = raw["5m"]
    sigs = signals(raw, trend, times, e20, b5)
    print(f"إشارات البوت على سنة 2026: {len(sigs)}\n")
    modes = ["immediate", "pullback_0.25atr", "pullback_0.5atr",
             "pullback_0.75atr", "pullback_1.0atr", "break_6", "break_12"]
    mid_t = b5[len(b5) // 2]["openTime"]
    print("═══ السنة كاملة ═══")
    for mode in modes:
        print(fmt(mode, stats(run(mode, raw, trend, times, e20, b5, sigs))))
    print("\n═══ النصف الأول (walk-forward) ═══")
    s1 = [s for s in sigs if s["t"] <= mid_t]
    for mode in modes:
        print(fmt(mode, stats(run(mode, raw, trend, times, e20, b5, s1))))
    print("\n═══ النصف الثاني (walk-forward) ═══")
    s2 = [s for s in sigs if s["t"] > mid_t]
    for mode in modes:
        print(fmt(mode, stats(run(mode, raw, trend, times, e20, b5, s2))))


if __name__ == "__main__":
    main()
