#!/usr/bin/env python3
"""New-filter experiments on BOTH datasets: the live spot feed and the 70-day
futures cache. A filter is only interesting if it helps on both.

Base rule: 15m and 1h agree AND price on the right side of the 15m EMA20.
Sizing/grading are the desk's own ($9 stop, R:R 2.0, first touch wins).
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import indicators as I  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_PTS = bot.STOP_PICK_USD / bot.POINT_VALUE
PV = bot.POINT_VALUE


class Data:
    """Precomputed indicators for a frame, from either source."""

    def __init__(self, bars, source):
        self.b = bars
        self.source = source
        cl = [x["close"] for x in bars]
        self.e20 = I.ema_series(cl, 20)
        self.e50 = I.ema_series(cl, 50)
        self.rsi = I.rsi_series(cl, 14)
        self.atr = I.atr_series(bars, 14)
        self.mid, self.bup, self.blo = I.bollinger(bars, 20, 2.0)
        self.stoch_k, self.stoch_d = I.stochastic(bars, 14, 3)
        self.macd_line, self.macd_sig, self.macd_hist = I.macd(bars)
        self.adx = I.adx(bars, 14)
        self.cci = I.cci(bars, 20)
        self.wr = I.williams_r(bars, 14)
        self.roc = I.roc(cl, 10)
        self.trend = [0] * len(bars)
        for i in range(len(bars)):
            a, c = self.e20[i], self.e50[i]
            if a and c:
                self.trend[i] = 1 if a > c * 1.0005 else (-1 if a < c * 0.9995 else 0)

    def align(self, t):
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


def load_source(source):
    if source == "spot":
        d15 = Data(sorted([x for x in bot.bars("15m", 300) if not x.get("isOpen")],
                          key=lambda x: x["openTime"]), source)
        d1h = Data(sorted([x for x in bot.bars("1h", 300) if not x.get("isOpen")],
                          key=lambda x: x["openTime"]), source)
    else:
        d15 = Data(json.load(open(os.path.join(HERE, "yahoo_15m.json"))), source)
        d1h = Data(json.load(open(os.path.join(HERE, "yahoo_1h.json"))), source)
    return {"15m": d15, "1h": d1h}


def simulate(P, flt=None, rr=2.0):
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
        t15 = P["15m"].trend[i]
        j = P["1h"].align(t)
        t1h = P["1h"].trend[j] if j >= 0 else 0
        up = (t15 == 1) + (t1h == 1)
        dn = (t15 == -1) + (t1h == -1)
        live = bar["close"]; e20 = P["15m"].e20[i]
        long_ok = up == 2 and (e20 is None or live > e20)
        short_ok = dn == 2 and (e20 is None or live < e20)
        if not (long_ok or short_ok):
            continue
        direction = "long" if long_ok else "short"
        if flt and not flt(P, i, direction, live):
            continue
        d = 1 if direction == "long" else -1
        open_t = {"direction": direction, "entry": live, "stop": live - d * STOP_PTS,
                  "target": live + d * STOP_PTS * rr, "rr": rr, "entry_time": t}
        cd = 1
    return trades


def stat(ts):
    if not ts:
        return {"n": 0, "win": 0.0, "r": 0.0, "longs": 0, "shorts": 0}
    w = sum(1 for x in ts if x["result"] == "target")
    return {"n": len(ts), "win": 100 * w / len(ts), "r": sum(x["R"] for x in ts),
            "longs": sum(1 for x in ts if x["direction"] == "long"),
            "shorts": sum(1 for x in ts if x["direction"] == "short")}


# ---------------- the new filters ----------------
def boll_revert(direction, k=0.15):
    """Buy near the lower band / sell near the upper band (mean reversion)."""
    def f(P, i, d, live):
        lo, up = P["15m"].blo[i], P["15m"].bup[i]
        if lo is None or up is None:
            return True
        span = up - lo
        if span <= 0:
            return True
        pos = (live - lo) / span
        return pos <= k if d == "long" else pos >= 1 - k
    return f


def boll_not_upper(direction):
    """Don't buy above the upper band (over-extended)."""
    def f(P, i, d, live):
        up, lo = P["15m"].bup[i], P["15m"].blo[i]
        if up is None:
            return True
        if d == "long" and live > up:
            return False
        if d == "short" and live < lo:
            return False
        return True
    return f


def stoch_revert(direction, lo=25, hi=75):
    def f(P, i, d, live):
        k = P["15m"].stoch_k[i]
        if k is None:
            return True
        return k <= lo if d == "long" else k >= hi
    return f


def stoch_trend(direction):
    """Momentum agrees: %K above 50 for long, below 50 for short."""
    def f(P, i, d, live):
        k = P["15m"].stoch_k[i]
        if k is None:
            return True
        return k >= 50 if d == "long" else k <= 50
    return f


def macd_hist_dir(direction):
    def f(P, i, d, live):
        h = P["15m"].macd_hist[i]
        if h is None:
            return True
        return h >= 0 if d == "long" else h <= 0
    return f


def macd_rising(direction):
    def f(P, i, d, live):
        h, hp = P["15m"].macd_hist[i], P["15m"].macd_hist[i - 1]
        if h is None or hp is None:
            return True
        return h > hp if d == "long" else h < hp
    return f


def adx_trending(direction, th=20):
    def f(P, i, d, live):
        a = P["15m"].adx[i]
        if a is None:
            return True
        return a >= th
    return f


def adx_not_choppy(direction, th=40):
    def f(P, i, d, live):
        a = P["15m"].adx[i]
        if a is None:
            return True
        return a <= th
    return f


def cci_dir(direction, th=0):
    def f(P, i, d, live):
        c = P["15m"].cci[i]
        if c is None:
            return True
        return c >= th if d == "long" else c <= -th
    return f


def cci_revert(direction, lo=-100, hi=100):
    def f(P, i, d, live):
        c = P["15m"].cci[i]
        if c is None:
            return True
        return c <= lo if d == "long" else c >= hi
    return f


def wr_revert(direction, lo=-80, hi=-20):
    def f(P, i, d, live):
        w = P["15m"].wr[i]
        if w is None:
            return True
        return w <= lo if d == "long" else w >= hi
    return f


def roc_dir(direction):
    def f(P, i, d, live):
        r = P["15m"].roc[i]
        if r is None:
            return True
        return r >= 0 if d == "long" else r <= 0
    return f


def atr_band(direction, lo=0.0008, hi=0.0025):
    """Volatility in a usable band: neither dead nor wild."""
    def f(P, i, d, live):
        a = P["15m"].atr[i]
        if a is None:
            return True
        return lo <= a / live <= hi
    return f


ALL = {
    "boll_revert": boll_revert, "boll_not_upper": boll_not_upper,
    "stoch_revert": stoch_revert, "stoch_trend": stoch_trend,
    "macd_hist_dir": macd_hist_dir, "macd_rising": macd_rising,
    "adx>20": adx_trending, "adx<40": adx_not_choppy,
    "cci_dir": cci_dir, "cci_revert": cci_revert,
    "wr_revert": wr_revert, "roc_dir": roc_dir, "atr_band": atr_band,
}


def run_all(P, label):
    print(f"\n═══ {label} ═══")
    base = stat(simulate(P))
    print(f"  {'الفلتر':16} {'صفقات':>5} {'شراء':>4} {'بيع':>4} {'ربح':>4} {'المجموع':>8}")
    print(f"  {'بلا فلتر (الأساس)':16} {base['n']:5} {base['longs']:4} {base['shorts']:4} "
          f"{base['win']:3.0f}% {base['r']:+7.1f}R")
    rows = []
    for name, maker in ALL.items():
        try:
            flt = maker("long")
        except TypeError:
            flt = maker
        ts = simulate(P, flt)
        s = stat(ts)
        rows.append((name, s))
        print(f"  {name:16} {s['n']:5} {s['longs']:4} {s['shorts']:4} "
              f"{s['win']:3.0f}% {s['r']:+7.1f}R")
    return base, rows


if __name__ == "__main__":
    out = {}
    for src, lbl in [("spot", "البيانات الحقيقية (سبوت، 5-9 نوفمبر)"),
                     ("futures", "البيانات القديمة (فوتشرز، 70 يوم)")]:
        P = load_source(src)
        base, rows = run_all(P, lbl)
        out[src] = {"base": base, "rows": {n: s for n, s in rows}}
    json.dump(out, open(os.path.join(HERE, "newfilters.json"), "w"),
              ensure_ascii=False, indent=1)
