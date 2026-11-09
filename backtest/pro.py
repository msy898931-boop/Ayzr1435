#!/usr/bin/env python3
"""The professional stack: multi-frame agreement + a regime/momentum filter stack.

Tests whether adding frames to the winning filter stack helps or hurts, and
whether an extra higher-timeframe alignment or session filter adds anything.
Scored on thirds and months; a config must be positive in every block.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import indicators as I  # noqa: E402
import newfilters as NF  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_PTS = bot.STOP_PICK_USD / bot.POINT_VALUE


class Frames:
    """Precompute indicators for several frames from one source."""

    def __init__(self, source):
        self.d = {}
        if source == "spot":
            raw = {tf: sorted([x for x in bot.bars(tf, 300) if not x.get("isOpen")],
                              key=lambda x: x["openTime"])
                   for tf in ("15m", "30m", "1h", "4h", "1d")}
            raw["5m"] = raw["15m"]
        else:
            raw = {tf: json.load(open(os.path.join(HERE, f"yahoo_{tf}.json")))
                   for tf in ("15m", "30m", "1h", "4h", "1d", "5m")}
        for tf, b in raw.items():
            self.d[tf] = NF.Data(b, source)

    def trend_at(self, tf, t):
        f = self.d[tf]
        j = f.align(t)
        return f.trend[j] if j >= 0 else 0


def simulate(P, frames, need, flt, slope_h1=False, session=None, rr=2.0):
    b15 = P.d["15m"].b
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
        tr = {tf: P.trend_at(tf, t) for tf in frames}
        up = sum(1 for f in frames if tr[f] == 1)
        dn = sum(1 for f in frames if tr[f] == -1)
        live = bar["close"]; e20 = P.d["15m"].e20[i]
        long_ok = up >= need and (e20 is None or live > e20)
        short_ok = dn >= need and (e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        direction = "long" if long_ok else "short"
        if slope_h1:
            h1 = P.d["1h"]; j = h1.align(t)
            if j >= 25 and h1.e20[j] is not None and h1.e20[j - 3] is not None:
                rising = h1.e20[j] > h1.e20[j - 3]
                if (direction == "long" and not rising) or (direction == "short" and rising):
                    continue
        if session is not None and not (session[0] <= int(t[11:13]) < session[1]):
            continue
        if flt and not flt(P.d, i, direction, live):
            continue
        d = 1 if direction == "long" else -1
        open_t = {"direction": direction, "entry": live, "stop": live - d * STOP_PTS,
                  "target": live + d * STOP_PTS * rr, "rr": rr, "entry_time": t}
        cd = 1
    return trades


def thirds(ts, P):
    b = P.d["15m"].b; n = len(b)
    cuts = [b[60]["openTime"], b[60 + (n - 60) // 3]["openTime"],
            b[60 + 2 * (n - 60) // 3]["openTime"], b[-1]["openTime"]]
    return [NF.stat([x for x in ts if cuts[k] <= x["entry_time"] < cuts[k + 1]])["r"]
            for k in range(3)]


def months(ts):
    m = collections.defaultdict(float)
    for x in ts:
        m[x["entry_time"][:7]] += x["R"]
    return m


if __name__ == "__main__":
    import tune as TU
    P = Frames("futures")
    F2 = ("15m", "1h")
    F3 = ("15m", "1h", "30m")
    F4 = ("15m", "1h", "30m", "4h")
    F6 = ("1d", "4h", "1h", "30m", "15m", "5m")
    stack = TU.make_filter(20, 40, 0.0008, 0.0025, "rising")
    tests = [
        ("2/2 لحاله", F2, 2, None, False, None),
        ("2/2 + stack", F2, 2, stack, False, None),
        ("3/3 + stack", F3, 3, stack, False, None),
        ("4/4 + stack", F4, 4, stack, False, None),
        ("6/6 + stack", F6, 4, stack, False, None),
        ("2/2 + stack + ميل1س", F2, 2, stack, True, None),
        ("2/2 + stack + جلسة", F2, 2, stack, False, (7, 16)),
        ("2/2 + stack + ميل + جلسة", F2, 2, stack, True, (7, 16)),
    ]
    print(f"{'التركيبة':26} {'صفقات':>5} {'ربح':>4} {'المجموع':>8} {'الأثلاث':>16} {'شهور+':>6} {'ثابت':>5}")
    print("-" * 82)
    rows = []
    for name, fr, nd, flt, sl, ss in tests:
        ts = simulate(P, fr, nd, flt, sl, ss)
        if not ts:
            print(f"{name:26} — لا صفقات")
            continue
        s = NF.stat(ts); t3 = thirds(ts, P); mo = months(ts)
        pm = sum(1 for v in mo.values() if v > 0)
        ok = all(x > 0 for x in t3) and pm == len(mo)
        rows.append({"name": name, "n": s["n"], "win": s["win"], "r": s["r"],
                     "t3": t3, "months": dict(mo), "ok": ok})
        print(f"{name:26} {s['n']:5} {s['win']:3.0f}% {s['r']:+7.1f}R "
              f"{t3[0]:+5.1f}{t3[1]:+6.1f}{t3[2]:+6.1f} {pm}/{len(mo):<4} "
              f"{'✅' if ok else '—':>4}")
    json.dump(rows, open(os.path.join(HERE, "pro.json"), "w"), ensure_ascii=False, indent=1)
