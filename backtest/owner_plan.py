#!/usr/bin/env python3
"""The owner's break-even ladder, measured honestly on the full 2026 sample.

Owner's idea, as stated:
    stop $9, target $20
    at +$10 profit  -> move the stop to +$5  (lock $5)
    at +$15 profit  -> move the stop to +$10 (lock $10)

This replays it against the current desk (fixed $9 / $20, stop never moves) on
every 5m bar of duka_2026.json (251 days), using the desk's OWN entry rules:
4-of-6 frame confirm, the 15m EMA20 filter, and the ADX/ATR gate.

To make 56k bars fast, the six frame EMAs, the 15m ADX and the 15m ATR are
precomputed once as series and read by index, instead of recomputing each
prefix. verify_against_replay() proves the fast path matches day_replay's
recompute-everything path bar for bar, so the speed does not change the answer.
"""
from __future__ import annotations
import bisect
import datetime as dt
import json
import sys

sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, "/workspace/Ayzr1435/backtest")
import bot  # noqa: E402
import day_replay as R  # noqa: E402

DATA = "/workspace/Ayzr1435/backtest/duka_2026.json"
DUR = bot.TF_SECONDS
STOP, TGT = 9.0, 20.0
COST = 0.5                     # $ per trade (spread/commission), as elsewhere


def _t(s):
    return dt.datetime.fromisoformat(s)


def _atr_series(b, n=14):
    out = [None] * len(b)
    if len(b) < n + 1:
        return out
    trs = []
    for i in range(1, len(b)):
        h, lo, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[:n]) / n
    out[n] = a
    for i in range(n + 1, len(b)):
        a = (a * (n - 1) + trs[i - 1]) / n
        out[i] = a
    return out


def prepare(bars_all):
    """Per frame: bar times, close times, EMA20/50; plus 15m ADX and ATR."""
    P = {}
    for tf in bot.FRAMES:
        b = bars_all[tf]
        cl = [x["close"] for x in b]
        opens = [_t(x["openTime"]) for x in b]
        closes = [o + dt.timedelta(seconds=DUR[tf]) for o in opens]
        P[tf] = {"b": b, "cl": cl, "opens": opens, "closes": closes,
                 "e20": bot.ema_series(cl, 20), "e50": bot.ema_series(cl, 50)}
    P["15m"]["adx"] = bot.adx_series(bars_all["15m"])
    P["15m"]["atr"] = _atr_series(bars_all["15m"])
    return P


def last_closed(P, tf, t):
    """Index of the last tf bar fully closed at (or before) time t, else -1."""
    return bisect.bisect_right(P[tf]["closes"], t) - 1


def signal_at(P, t, live):
    """The desk's entry signal at bar-close time t, exactly as day_replay sees it."""
    up = dn = 0
    for tf in bot.FRAMES:
        i = last_closed(P, tf, t)
        e20 = P[tf]["e20"][i] if i >= 0 else None
        e50 = P[tf]["e50"][i] if i >= 0 else None
        if e20 is None or e50 is None:
            continue
        up += e20 > e50 * 1.0005
        dn += e20 < e50 * 0.9995
    d = ("long" if up >= bot.CONFIRM_FRAMES
         else "short" if dn >= bot.CONFIRM_FRAMES else None)
    if d is None:
        return None, False
    i15 = last_closed(P, "15m", t)
    if i15 < 0:
        return d, False
    e20_15 = P["15m"]["e20"][i15]
    agrees = True if e20_15 is None else (
        live > e20_15 if d == "long" else live < e20_15)
    live15 = P["15m"]["cl"][i15]
    adx_v = P["15m"]["adx"][i15]
    atr_v = P["15m"]["atr"][i15]
    atr_pct = (atr_v / live15) if (atr_v and live15 > 0) else None
    adx_ok = ("adx" not in bot.FILTER_GATES or adx_v is None
              or bot.ADX_MIN <= adx_v <= bot.ADX_MAX)
    atr_ok = ("atr" not in bot.FILTER_GATES or atr_pct is None
              or bot.ATR_MIN_PCT <= atr_pct <= bot.ATR_MAX_PCT)
    return d, bool(agrees and adx_ok and atr_ok)


def build_signals(bars_all):
    P = prepare(bars_all)
    sig = []
    for x in bars_all["5m"]:
        t, live = _t(x["openTime"]), x["close"]
        d, ok = signal_at(P, t, live)
        sig.append({"t": x["openTime"], "px": live, "d": d, "ok": ok,
                    "hi": x["high"], "lo": x["low"]})
    return sig


def walk(sig, policy, stop=STOP, tgt=TGT, cooldown=0, scale=None):
    """Walk the signal stream, one trade at a time.

    cooldown: bars to skip after an exit (selectivity knob).
    scale: None for a single target; or (frac, t1, t2) to take `frac` off at t1
           and run the rest to t2 (the professional scale-out).
    """
    trades, op, cool = [], None, 0
    for s in sig:
        if op is not None:
            d, e = op["d"], op["entry"]
            sg = 1 if d == "long" else -1
            hi, lo = s["hi"], s["lo"]
            fav = (hi - e) if d == "long" else (e - lo)
            hit_stop = (lo <= op["stop"]) if d == "long" else (hi >= op["stop"])
            hit_tgt = (hi >= e + tgt) if d == "long" else (lo <= e - tgt)
            if hit_stop:
                op["r"] = (op["stop"] - e) * sg
                if scale and op.get("t1done"):
                    op["r"] = scale[0] * scale[1] + (1 - scale[0]) * op["r"]
                op.update(res="stop")
                trades.append(op)
                op = None
                cool = cooldown
            elif hit_tgt:
                op["r"] = tgt
                if scale and op.get("t1done"):
                    op["r"] = scale[0] * scale[1] + (1 - scale[0]) * tgt
                op.update(res="target")
                trades.append(op)
                op = None
                cool = cooldown
            else:
                if scale and not op.get("t1done") and fav >= scale[1]:
                    op["t1done"] = True
                    op["stop"] = max(op["stop"], e)      # break-even
                else:
                    policy(op, fav)
        elif cool > 0:
            cool -= 1
        elif s["d"] and s["ok"]:
            sg = 1 if s["d"] == "long" else -1
            op = {"d": s["d"], "entry": s["px"], "t": s["t"],
                  "stop": s["px"] - sg * stop}
    return trades


def pol_fixed(op, fav):
    pass


def pol_owner(op, fav):
    if fav >= 15:
        op["stop"] = max(op["stop"], op["entry"] + 10)
    elif fav >= 10:
        op["stop"] = max(op["stop"], op["entry"] + 5)


def pol_be10(op, fav):
    if fav >= 10:
        op["stop"] = max(op["stop"], op["entry"])


def pol_owner_early(op, fav):
    """The owner's ladder but with the first lock at +$8 instead of +$10."""
    if fav >= 14:
        op["stop"] = max(op["stop"], op["entry"] + 9)
    elif fav >= 8:
        op["stop"] = max(op["stop"], op["entry"] + 4)


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    wins = [t for t in trades if t["r"] > 0]
    losses = [t for t in trades if t["r"] <= 0]
    net = sum(t["r"] for t in trades) - COST * n
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["r"] - COST
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    aw = sum(t["r"] for t in wins) / len(wins) if wins else 0
    al = sum(t["r"] for t in losses) / len(losses) if losses else 0
    return {"n": n, "win%": round(100 * len(wins) / n, 1),
            "net$": round(net, 1), "avg$": round(net / n, 2),
            "maxDD$": round(dd, 1), "avgWin$": round(aw, 1),
            "avgLoss$": round(al, 1)}


def verify_against_replay(bars_all, days=3):
    """Prove the fast signal path equals day_replay's on a few days."""
    P = prepare(bars_all)
    all_days = sorted({x["openTime"][:10] for x in bars_all["5m"]})
    bad = 0
    for day in all_days[:days]:
        b5 = [x for x in bars_all["5m"] if x["openTime"][:10] == day]
        for x in b5:
            t, live = _t(x["openTime"]), x["close"]
            # fast path
            fd, fok = signal_at(P, t, live)
            # slow path (day_replay's own recompute)
            trends = {}
            for tf in bot.FRAMES:
                vis = R.visible(bars_all[tf], x["openTime"])
                trends[tf] = bot.frame_trend(vis) if vis else 0
            up = sum(1 for tf in bot.FRAMES if trends[tf] == 1)
            dn = sum(1 for tf in bot.FRAMES if trends[tf] == -1)
            sd = ("long" if up >= bot.CONFIRM_FRAMES
                  else "short" if dn >= bot.CONFIRM_FRAMES else None)
            sok = False
            if sd:
                b15c = R.closed(bars_all["15m"], "15m", x["openTime"])
                ag = bot.frame_agrees(sd, live, b15c)
                fs = bot.filter_status(sd, b15c)
                sok = bool(ag and fs and fs["ok"])
            if fd != sd or fok != sok:
                bad += 1
                if bad <= 5:
                    print(f"   MISMATCH {x['openTime']}: fast=({fd},{fok}) "
                          f"slow=({sd},{sok})")
    return bad


if __name__ == "__main__":
    bars = json.load(open(DATA))
    print("verifying the fast path against day_replay (3 days)...", flush=True)
    bad = verify_against_replay(bars)
    print(f"   mismatches: {bad}\n", flush=True)
    if bad:
        print("FAST PATH DISAGREES -- do not trust the numbers below.")
        sys.exit(1)
    print("building signals for all 251 days...", flush=True)
    sig = build_signals(bars)
    print(f"   {len(sig)} bars, "
          f"{sum(1 for s in sig if s['ok'])} entry signals\n")

    print("== A. same entries, different exits (no cooldown) ==")
    for name, pol in [("current (fixed $9/$20)", pol_fixed),
                      ("owner ladder (lock $5 @+10, $10 @+15)", pol_owner),
                      ("owner ladder, earlier (lock $4 @+8, $9 @+14)",
                       pol_owner_early),
                      ("break-even only (@+10)", pol_be10)]:
        st = stats(walk(sig, pol))
        print(f"  {name:48s} n={st['n']:4d} win={st['win%']:5.1f}% "
              f"net={st['net$']:8.1f}$ avg/trade={st['avg$']:6.2f}$ "
              f"maxDD={st['maxDD$']:8.1f}$ "
              f"(win {st['avgWin$']}$ / loss {st['avgLoss$']}$)")

    print("\n== B. with a cooldown (fewer, cleaner entries) ==")
    for cd in (6, 12, 24):
        for name, pol in [("current", pol_fixed),
                          ("owner ladder", pol_owner)]:
            st = stats(walk(sig, pol, cooldown=cd))
            print(f"  cd={cd:2d} {name:14s} n={st['n']:4d} "
                  f"win={st['win%']:5.1f}% net={st['net$']:8.1f}$ "
                  f"avg/trade={st['avg$']:6.2f}$ maxDD={st['maxDD$']:8.1f}$")

    print("\n== C. the professional scale-out (half off at t1, stop to BE) ==")
    for t1, t2 in [(10.0, 20.0), (12.0, 20.0), (8.0, 20.0)]:
        st = stats(walk(sig, pol_fixed, scale=(0.5, t1, t2)))
        print(f"  half @+{t1:.0f}$, rest @+{t2:.0f}$   n={st['n']:4d} "
              f"win={st['win%']:5.1f}% net={st['net$']:8.1f}$ "
              f"avg/trade={st['avg$']:6.2f}$ maxDD={st['maxDD$']:8.1f}$")
