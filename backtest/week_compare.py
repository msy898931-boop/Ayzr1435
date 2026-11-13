#!/usr/bin/env python3
"""Replay the OWNER'S REAL week of trades: running bot vs pullback entry.

Ground truth: backtest/week_real.json -- the 39 trades the live bot actually
took (downloaded from the pinned Telegram backup), each with its real open
time, entry, stop and target.

For every real trade we know the SIGNAL moment (opened_at). We then ask: if the
desk had used each entry mode on that SAME signal, what would have happened?

  immediate  -- enter at the recorded entry, walk bars to stop/target.
  pullback   -- place a limit 0.5*ATR(15m) back from the entry (a short waits
                HIGHER, a long LOWER); fill if price reaches it within 12 bars,
                else the trade is MISSED (0R). Walk to stop/target from the fill.

Honesty guards:
  * the SAME absolute stop and target DISTANCE is used for both modes, so only
    the entry price (and therefore the fill) differs -- not the exit policy;
  * both modes are walked on 5m bars with the same rule (first touch wins, a bar
    spanning both takes the stop), so a manual close in the journal does not
    flatter either side;
  * ATR(15m) is read on the last CLOSED bar before the signal, as the live desk
    does;
  * a mode that never fills contributes 0R and is reported as "missed", never
    dropped silently.
"""
from __future__ import annotations
import bisect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

WINDOW = 12          # bars (5m) to wait for the retrace
PULLBACK_ATR = 0.5


def _t(s):
    return bot.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load():
    frames = json.load(open("/tmp/week_frames.json"))
    real = json.load(open("backtest/week_real.json"))["trades"]
    return frames, real


def atr15_at(frames, t):
    """ATR(15m) on the last CLOSED 15m bar at time t."""
    b = frames["15m"]
    idx = bisect.bisect_right([x["openTime"] for x in b], t) - 1
    # drop the forming bar: only bars fully closed at t
    closed = []
    for x in b[: idx + 1]:
        if (_t(x["openTime"]) + bot.timedelta(seconds=900)) <= _t(t):
            closed.append(x)
    return bot.atr(closed) if closed else None


def walk(b5, start_t, entry, direction, stop, target):
    """From the first 5m bar AFTER start_t, first touch wins; a bar spanning
    both takes the stop. Returns (R, exit_time, result) or (None, None, None)."""
    d = 1 if direction == "long" else -1
    times = [x["openTime"] for x in b5]
    i = bisect.bisect_right(times, start_t)
    for j in range(i, len(b5)):
        bar = b5[j]
        hs = bar["low"] <= stop if d == 1 else bar["high"] >= stop
        ht = bar["high"] >= target if d == 1 else bar["low"] <= target
        if hs or ht:
            return (-1.0 if hs else abs(target - entry) / abs(entry - stop)), \
                bar["openTime"], ("stop" if hs else "target")
    return None, None, None


def immediate(frames, t, trade):
    risk = abs(trade["entry"] - trade["stop"])
    rew = abs(trade["target"] - trade["entry"])
    r, xt, res = walk(frames["5m"], t, trade["entry"], trade["direction"],
                      trade["stop"], trade["target"])
    return {"filled": r is not None, "R": r, "exit": xt, "res": res,
            "entry": trade["entry"], "risk": risk, "reward": rew}


def pullback(frames, t, trade):
    risk = abs(trade["entry"] - trade["stop"])
    rew = abs(trade["target"] - trade["entry"])
    d = -1 if trade["direction"] == "short" else 1
    a = atr15_at(frames, t)
    if not a:
        return {"filled": False, "R": 0.0, "entry": None, "risk": risk,
                "reward": rew, "why": "no ATR"}
    limit = trade["entry"] - d * PULLBACK_ATR * a     # short: limit ABOVE
    b5 = frames["5m"]
    times = [x["openTime"] for x in b5]
    i0 = bisect.bisect_right(times, t)
    for k in range(i0, min(i0 + WINDOW, len(b5))):
        bar = b5[k]
        if (d == -1 and bar["high"] >= limit) or (d == 1 and bar["low"] <= limit):
            stop = limit + risk if d == -1 else limit - risk
            tgt = limit - rew if d == -1 else limit + rew
            r, xt, res = walk(b5, bar["openTime"], limit, trade["direction"],
                              stop, tgt)
            return {"filled": r is not None, "R": (r if r is not None else 0.0),
                    "exit": xt, "res": res, "entry": limit, "risk": risk,
                    "reward": rew}
    return {"filled": False, "R": 0.0, "entry": limit, "risk": risk,
            "reward": rew, "why": "no retrace"}


def stats(rows):
    n = len(rows)
    wins = [r for r in rows if r.get("res") == "target"]
    losses = [r for r in rows if r.get("res") == "stop"]
    net = sum(r["R"] or 0 for r in rows)
    filled = [r for r in rows if r["filled"]]
    return {"n": n, "filled": len(filled), "missed": n - len(filled),
            "wins": len(wins), "losses": len(losses), "netR": net,
            "wr": (len(wins) / len(filled) * 100) if filled else 0,
            "avgR": net / n if n else 0}


def main():
    frames, real = load()
    real = [t for t in real if t.get("status") == "closed" and t.get("r_multiple") is not None]
    print(f"الصفقات الحقيقية المغلقة: {len(real)}\n")
    imm_rows, pb_rows = [], []
    print(f"{'#':>3} {'اتجاه':5} {'إشارة':>9} {'وقف':>6} "
          f"{'فوري':>22} {'ارتداد':>26}")
    for tr in real:
        t = tr["opened_at"]
        im = immediate(frames, t, tr)
        pb = pullback(frames, t, tr)
        imm_rows.append(im)
        pb_rows.append(pb)
        im_s = f"{im['res'] or '-':>6} {im['R']:+.2f}R" if im["filled"] else "  open/none"
        if pb["filled"]:
            pb_s = f"دخل {pb['entry']:.1f} {pb['res']:>6} {pb['R']:+.2f}R"
        else:
            pb_s = f"فوت ({pb.get('why','-')}) 0.00R"
        print(f"{tr['id']:>3} {tr['direction']:5} {tr['entry']:>9.2f} "
              f"{abs(tr['entry']-tr['stop']):>6.1f} {im_s:>22} {pb_s:>26}")
    si, sp = stats(imm_rows), stats(pb_rows)
    print("\n═══ الخلاصة (نفس الـ38 إشارة) ═══")
    print(f"  فوري:    صفقات {si['filled']}/{si['n']} · فوز {si['wins']} خسارة "
          f"{si['losses']} · فوز% {si['wr']:.1f} · صافي {si['netR']:+.2f}R · "
          f"متوسط {si['avgR']:+.3f}R")
    print(f"  ارتداد:  صفقات {sp['filled']}/{sp['n']} (فوت {sp['missed']}) · فوز "
          f"{sp['wins']} خسارة {sp['losses']} · فوز% {sp['wr']:.1f} · صافي "
          f"{sp['netR']:+.2f}R · متوسط {sp['avgR']:+.3f}R")
    # Faithfulness check: the immediate replay must reproduce the journal's own
    # R for every trade the desk closed on its own stop/target (a manual close
    # is the owner's discretion, not the bot's rule, so those are excluded).
    match = 0
    checked = 0
    for tr, im in zip(real, imm_rows):
        if tr.get("result") in ("stop", "target") and im["filled"]:
            checked += 1
            if abs((im["R"] or 0) - (tr["r_multiple"] or 0)) < 0.05:
                match += 1
    print(f"\n  ✅ تطابق إعادة التشغيل مع السجل الحقيقي: {match}/{checked} "
          f"صفقة (الباقي إغلاق يدوي — قرارك، مو قاعدة البوت)")
    json.dump({"immediate": imm_rows, "pullback": pb_rows},
              open("/tmp/week_compare.json", "w"))
    print("  حُفظ /tmp/week_compare.json")


if __name__ == "__main__":
    main()
