#!/usr/bin/env python3
"""Grid search over rules and filters on the 70-day gold sample.

Precomputes every indicator once (incremental EMA/RSI/ATR) so hundreds of
configurations can be replayed cheaply. Every configuration is reported on the
WHOLE sample AND on each half -- a config that only works in one half is noise,
not an edge.
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMES = bot.FRAMES
PV = bot.POINT_VALUE
STOP_PTS = bot.STOP_PICK_USD / PV          # 9 points


def load(tf):
    p = os.path.join(HERE, f"yahoo_{tf}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def ema_series(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(vals, n=14):
    out = [None] * len(vals)
    if len(vals) < n + 1:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        g += d if d > 0 else 0.0
        l += -d if d < 0 else 0.0
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + (d if d > 0 else 0.0)) / n
        al = (al * (n - 1) + (-d if d < 0 else 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_series(b, n=14):
    out = [None] * len(b)
    if len(b) < n + 1:
        return out
    trs = [None]
    for i in range(1, len(b)):
        h, lo, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(b)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


class Pre:
    def __init__(self, tf):
        self.b = load(tf)
        cl = [x["close"] for x in self.b]
        self.e20 = ema_series(cl, 20)
        self.e50 = ema_series(cl, 50)
        self.rsi = rsi_series(cl, 14)
        self.atr = atr_series(self.b, 14)
        self.trend = [0] * len(self.b)
        for i in range(len(self.b)):
            a, c = self.e20[i], self.e50[i]
            if a is None or c is None:
                continue
            self.trend[i] = 1 if a > c * 1.0005 else (-1 if a < c * 0.9995 else 0)

    def align(self, t):
        """Index of the last bar with openTime <= t (binary search)."""
        lo, hi = 0, len(self.b) - 1
        if t < self.b[0]["openTime"]:
            return -1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.b[mid]["openTime"] <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo


def simulate(P, frames, need, flt, rr, session=None, gap=1):
    b15 = P["15m"].b
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
        tr = {tf: P[tf].trend[P[tf].align(t)] for tf in FRAMES}
        up = sum(1 for f in frames if tr.get(f) == 1)
        dn = sum(1 for f in frames if tr.get(f) == -1)
        live = bar["close"]
        p15 = P["15m"]
        e20 = p15.e20[i]
        long_ok = up >= need and (e20 is None or live > e20)
        short_ok = dn >= need and (e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        direction = "long" if long_ok else "short"
        if session is not None:
            hr = int(t[11:13])
            if not (session[0] <= hr < session[1]):
                continue
        if flt and not flt(P, i, direction, live):
            continue
        d = 1 if direction == "long" else -1
        stop = live - d * STOP_PTS
        tgt = live + d * STOP_PTS * rr
        open_t = {"direction": direction, "entry": live, "stop": stop,
                  "target": tgt, "rr": rr, "entry_time": t}
        cd = gap
    return trades


def stat(ts):
    if not ts:
        return {"n": 0, "win": 0.0, "r": 0.0}
    w = sum(1 for x in ts if x["result"] == "target")
    return {"n": len(ts), "win": 100 * w / len(ts), "r": sum(x["R"] for x in ts)}


# ---------------- filters (operate on precomputed arrays) ----------------
def F(**kw):
    def make(P, i, d, live):
        p = P["15m"]
        if "rsi_lo" in kw or "rsi_hi" in kw:
            r = p.rsi[i]
            if r is None:
                return True
            if r < kw.get("rsi_lo", 0) or r > kw.get("rsi_hi", 100):
                return False
        if "atr_max" in kw:
            a = p.atr[i]
            if a is not None and a > live * kw["atr_max"]:
                return False
        if "atr_min" in kw:
            a = p.atr[i]
            if a is not None and a < live * kw["atr_min"]:
                return False
        if "not_ext" in kw:
            a, e = p.atr[i], p.e20[i]
            if a and e and abs(live - e) > a * kw["not_ext"]:
                return False
        if "brk" in kw:
            n = kw["brk"]
            win = p.b[i - n:i]
            if len(win) == n:
                hi = max(x["high"] for x in win)
                lo = min(x["low"] for x in win)
                if d == "long" and live < hi:
                    return False
                if d == "short" and live > lo:
                    return False
        if "slope" in kw:
            j = P["1h"].align(P["15m"].b[i]["openTime"])
            q = P["1h"]
            if j >= 25 and q.e20[j] is not None and q.e20[j - 3] is not None:
                rising = q.e20[j] > q.e20[j - 3]
                if d == "long" and not rising:
                    return False
                if d == "short" and rising:
                    return False
        return True
    return make


if __name__ == "__main__":
    P = {tf: Pre(tf) for tf in FRAMES}
    n = len(P["15m"].b)
    mid = 60 + (n - 60) // 2
    print(f"عيّنة: {n} شمعة 15د\n")

    configs = []
    # baselines
    for name, fr, nd in [("4/6", FRAMES, 4), ("3/6", FRAMES, 3), ("2/2", ("15m", "1h"), 2)]:
        for rr in (1.5, 2.0, 2.5):
            configs.append((f"{name} rr={rr}", fr, nd, None, rr, None))
    # single filters on 4/6 and 2/2
    filters = {
        "rsi40-60": F(rsi_lo=40, rsi_hi=60),
        "rsi38-58": F(rsi_lo=38, rsi_hi=58),
        "rsi45-65": F(rsi_lo=45, rsi_hi=65),
        "atr<.15%": F(atr_max=0.0015),
        "atr<.25%": F(atr_max=0.0025),
        "atr<.40%": F(atr_max=0.0040),
        "notExt1atr": F(not_ext=1.0),
        "notExt0.7": F(not_ext=0.7),
        "brk20": F(brk=20),
        "brk10": F(brk=10),
        "slope1h": F(slope=True),
    }
    for name, fr, nd in [("4/6", FRAMES, 4), ("2/2", ("15m", "1h"), 2)]:
        for fn, f in filters.items():
            configs.append((f"{name}+{fn}", fr, nd, f, 2.0, None))
    # sessions
    for name, fr, nd in [("4/6", FRAMES, 4), ("2/2", ("15m", "1h"), 2)]:
        for s, lbl in [((7, 16), "لندن-نيويورك"), ((12, 20), "نيويورك"), ((0, 24), "الكل")]:
            if s != (0, 24):
                configs.append((f"{name}+{lbl}", fr, nd, None, 2.0, s))
    # promising combos
    for name, fr, nd in [("4/6", FRAMES, 4), ("2/2", ("15m", "1h"), 2)]:
        for fn, f in [("rsi40-60", F(rsi_lo=40, rsi_hi=60)),
                      ("rsi38-58", F(rsi_lo=38, rsi_hi=58)),
                      ("atr<.25%", F(atr_max=0.0025)),
                      ("notExt1atr", F(not_ext=1.0))]:
            for fn2, f2 in [("atr<.25%", F(atr_max=0.0025)),
                            ("notExt1atr", F(not_ext=1.0)),
                            ("rsi40-60", F(rsi_lo=40, rsi_hi=60))]:
                if fn == fn2:
                    continue
                def both(P, i, d, live, _a=f, _b=f2):
                    return _a(P, i, d, live) and _b(P, i, d, live)
                configs.append((f"{name}+{fn}+{fn2}", fr, nd, both, 2.0, None))

    rows = []
    for name, fr, nd, flt, rr, sess in configs:
        all_t = simulate(P, fr, nd, flt, rr, sess)
        h1 = [x for x in all_t if x["entry_time"] < P["15m"].b[mid]["openTime"]]
        h2 = [x for x in all_t if x["entry_time"] >= P["15m"].b[mid]["openTime"]]
        s, s1, s2 = stat(all_t), stat(h1), stat(h2)
        rows.append({"name": name, "n": s["n"], "win": s["win"], "r": s["r"],
                     "h1_r": s1["r"], "h2_r": s2["r"], "h1_n": s1["n"], "h2_n": s2["n"],
                     "stable": s1["r"] > 0 and s2["r"] > 0})
    rows.sort(key=lambda x: (not x["stable"], -x["r"]))
    print(f"{'التركيبة':26} {'صفقات':>5} {'ربح':>5} {'المجموع':>8} {'نص1':>7} {'نص2':>7} {'ثابت':>5}")
    print("-" * 74)
    for r in rows[:28]:
        print(f"{r['name']:26} {r['n']:5} {r['win']:4.0f}% {r['r']:+7.1f}R "
              f"{r['h1_r']:+6.1f}R {r['h2_r']:+6.1f}R {'✅' if r['stable'] else '—':>4}")
    json.dump(rows, open(os.path.join(HERE, "search.json"), "w"), ensure_ascii=False, indent=1)
