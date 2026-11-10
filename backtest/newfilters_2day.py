#!/usr/bin/env python3
"""Two extra filters, tested bar-by-bar on the last two days (buy AND sell).

Filter 1 (session): only trade in the liquid window (London + New York),
07:00-20:00 UTC. A 5m bar's UTC hour decides.

Filter 3 (HTF trend): do not trade against the daily frame. Strict form: a long
needs the 1d tilt to be UP and a short needs it DOWN. Loose form: a long only
needs the 1d tilt to be NOT down (and the mirror).

Everything else is the live desk: 4/6 frames + EMA20(15m) + ADX/ATR gate,
$9 stop / $20 target, one trade at a time. Reports each combination and the
trades it takes.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import day_replay as R  # noqa: E402

STOP, TGT = 9.0, 20.0
SESSION_START, SESSION_END = 7, 20   # UTC hours [start, end)


def htf_tilt(bars_all, t, mode):
    """Return the 1d tilt at t, or None if the filter is off."""
    if mode == "off":
        return None
    b = R.closed(bars_all["1d"], "1d", t)
    return bot.frame_trend(b) if b else 0


def session_ok(t):
    h = int(t[11:13])
    return SESSION_START <= h < SESSION_END


def replay(bars_all, day, carry, use_session, htf_mode):
    b5 = [x for x in bars_all["5m"] if x["openTime"][:10] == day]
    rows, op = [], carry
    for bar in b5:
        t = bar["openTime"]
        live = bar["close"]
        if op is not None:
            d = op["d"]
            hi, lo = bar["high"], bar["low"]
            hs = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else lo <= op["target"]
            if hs or ht:
                rows.append({**op["row"], "action": "exit",
                             "result": "stop" if hs else "target",
                             "exit_time": t, "entry": op["entry"],
                             "exit_price": op["stop"] if hs else op["target"]})
                op = None
            continue
        trends = {}
        for tf in bot.FRAMES:
            vis = R.visible(bars_all[tf], t)
            trends[tf] = bot.frame_trend(vis) if vis else 0
        up = sum(1 for tf in bot.FRAMES if trends[tf] == 1)
        dn = sum(1 for tf in bot.FRAMES if trends[tf] == -1)
        direction = "long" if up >= bot.CONFIRM_FRAMES else (
            "short" if dn >= bot.CONFIRM_FRAMES else None)
        b15c = R.closed(bars_all["15m"], "15m", t)
        agrees = bot.frame_agrees(direction, live, b15c) if direction else None
        fs = bot.filter_status(direction, b15c) if direction else None
        row = {"time": t, "price": live, "trends": dict(trends),
               "up": up, "dn": dn, "direction": direction, "agrees": agrees,
               "filter": fs, "action": ""}
        blocked = ""
        if direction is not None:
            if use_session and not session_ok(t):
                blocked = "جلسة"
            elif not agrees:
                blocked = "EMA"
            elif fs and not fs["ok"]:
                blocked = "بوابة"
            else:
                tilt = htf_tilt(bars_all, t, htf_mode)
                if htf_mode == "strict" and tilt is not None and tilt != (1 if direction == "long" else -1):
                    blocked = "1d ضد"
                elif htf_mode == "loose" and tilt is not None and (
                        (direction == "long" and tilt == -1) or
                        (direction == "short" and tilt == 1)):
                    blocked = "1d ضد"
        row["blocked"] = blocked
        if direction is not None and not blocked:
            sg = 1 if direction == "long" else -1
            op = {"d": direction, "entry": live, "row": row,
                  "stop": live - sg * STOP, "target": live + sg * TGT}
            row["action"] = "entry"
            row["stop"] = op["stop"]
            row["target"] = op["target"]
        rows.append(row)
    return rows, op


def run(bars_all, days, use_session, htf_mode):
    carry, allrows = None, []
    for d in days:
        rows, carry = replay(bars_all, d, carry, use_session, htf_mode)
        allrows.extend(rows)
    ent = [r for r in allrows if r["action"] == "entry"]
    ex = [r for r in allrows if r["action"] == "exit"]
    w = sum(1 for r in ex if r["result"] == "target")
    l = sum(1 for r in ex if r["result"] == "stop")
    return ent, ex, w, l, allrows


if __name__ == "__main__":
    bars_all = json.load(open("/tmp/live_bars.json"))
    days = sorted({x["openTime"][:10] for x in bars_all["5m"]})[-2:]
    combos = [
        ("الحالي (بلا فلترين)", False, "off"),
        ("+ جلسة فقط (1)", True, "off"),
        ("+ 1d صارم فقط (3)", False, "strict"),
        ("+ 1d مرن فقط (3)", False, "loose"),
        ("+ جلسة + 1d صارم", True, "strict"),
        ("+ جلسة + 1d مرن", True, "loose"),
    ]
    print(f"اختبار على {days[0]} و {days[1]} · وقف ${STOP:.0f}/هدف ${TGT:.0f}\n")
    print(f"{'الفلتر':24}{'صفقات':>7}{'شراء':>6}{'بيع':>6}{'ربح':>5}{'خسارة':>6}{'الصافي':>9}")
    print("-" * 66)
    for name, us, hm in combos:
        ent, ex, w, l, rows = run(bars_all, days, us, hm)
        nl = sum(1 for e in ent if e["direction"] == "long")
        ns = sum(1 for e in ent if e["direction"] == "short")
        print(f"{name:24}{len(ent):7}{nl:6}{ns:6}{w:5}{l:6}{w*TGT-l*STOP:+8.0f}$")
