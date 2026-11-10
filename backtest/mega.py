#!/usr/bin/env python3
"""A wide search across strategy FAMILIES on gold.

Families tried: trend agreement, EMA cross, Donchian breakout, Bollinger
breakout and reversion, RSI reversion and trend, stochastic reversion and cross,
MACD cross, candlestick patterns (engulfing, pin bar, inside-bar breakout) and
ROC momentum. Each family is combined with higher-timeframe and regime filters
and an exit inside the desk's envelope (stop $5-10, target $10-20).

Scored on win rate, total R, $/trade, stability (thirds AND months) and the
4-day spot feed, so a high win rate that is only one lucky stretch is visible.
"""
from __future__ import annotations
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import newfilters as NF  # noqa: E402
import winrate as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ("5m", "15m", "30m", "1h", "4h", "1d")


class Frames:
    """Load every frame a strategy might ask for, from one source."""

    def __init__(self, source):
        import bot
        self.d = {}
        if source == "spot":
            avail = ("15m", "1h")
            raw = {tf: sorted([x for x in bot.bars(tf, 300) if not x.get("isOpen")],
                              key=lambda x: x["openTime"]) for tf in avail}
            for tf in NEED:
                raw.setdefault(tf, raw["1h"])
        elif source == "duka":
            raw = json.load(open(os.path.join(HERE, "duka_2026.json")))
        else:
            raw = {tf: json.load(open(os.path.join(HERE, f"yahoo_{tf}.json")))
                   for tf in NEED}
        for tf, b in raw.items():
            self.d[tf] = NF.Data(b, source)


def aligned(P, tf):
    """Frame trend reindexed onto the 15m bars, so entries need no lookups."""
    b15 = P.d["15m"].b
    f = P.d[tf]
    arr = []
    j = 0
    for i in range(len(b15)):
        t = b15[i]["openTime"]
        while j + 1 < len(f.b) and f.b[j + 1]["openTime"] <= t:
            j += 1
        arr.append(f.trend[j] if f.b[j]["openTime"] <= t else 0)
    return arr


def build(P):
    return {tf: aligned(P, tf) for tf in ("1h", "30m", "4h", "1d")}


# ---------------- entries: 'long' / 'short' / None ----------------
def e_trend2(P, i, A):
    b = P.d["15m"]; live = b.b[i]["close"]; e20 = b.e20[i]
    t15 = b.trend[i]; t1h = A["1h"][i]
    if t15 == 1 and t1h == 1 and (e20 is None or live > e20):
        return "long"
    if t15 == -1 and t1h == -1 and (e20 is None or live < e20):
        return "short"
    return None


def e_trend3(P, i, A):
    b = P.d["15m"]; live = b.b[i]["close"]; e20 = b.e20[i]
    up = (b.trend[i] == 1) + (A["1h"][i] == 1) + (A["30m"][i] == 1)
    dn = (b.trend[i] == -1) + (A["1h"][i] == -1) + (A["30m"][i] == -1)
    if up == 3 and (e20 is None or live > e20):
        return "long"
    if dn == 3 and (e20 is None or live < e20):
        return "short"
    return None


def e_ema_cross(P, i, A):
    e20, e50 = P.d["15m"].e20, P.d["15m"].e50
    if e20[i] is None or e50[i] is None or e20[i - 1] is None or e50[i - 1] is None:
        return None
    if e20[i - 1] <= e50[i - 1] and e20[i] > e50[i]:
        return "long"
    if e20[i - 1] >= e50[i - 1] and e20[i] < e50[i]:
        return "short"
    return None


def _donch(P, i, n):
    b = P.d["15m"].b
    if i < n:
        return None
    hi = max(x["high"] for x in b[i - n:i])
    lo = min(x["low"] for x in b[i - n:i])
    if b[i]["close"] > hi:
        return "long"
    if b[i]["close"] < lo:
        return "short"
    return None


def e_donch20(P, i, A):
    return _donch(P, i, 20)


def e_donch10(P, i, A):
    return _donch(P, i, 10)


def e_boll_break(P, i, A):
    up, lo = P.d["15m"].bup[i], P.d["15m"].blo[i]
    if up is None:
        return None
    live = P.d["15m"].b[i]["close"]
    if live > up:
        return "long"
    if live < lo:
        return "short"
    return None


def e_boll_rev(P, i, A):
    up, lo = P.d["15m"].bup[i], P.d["15m"].blo[i]
    if up is None:
        return None
    live = P.d["15m"].b[i]["close"]
    if live < lo:
        return "long"
    if live > up:
        return "short"
    return None


def e_rsi_rev(P, i, A):
    r = P.d["15m"].rsi[i]
    if r is None:
        return None
    if r < 30:
        return "long"
    if r > 70:
        return "short"
    return None


def e_rsi_trend(P, i, A):
    r = P.d["15m"].rsi[i]
    if r is None:
        return None
    if r > 55:
        return "long"
    if r < 45:
        return "short"
    return None


def e_stoch_rev(P, i, A):
    k = P.d["15m"].stoch_k[i]
    if k is None:
        return None
    if k < 20:
        return "long"
    if k > 80:
        return "short"
    return None


def e_stoch_cross(P, i, A):
    k, d = P.d["15m"].stoch_k, P.d["15m"].stoch_d
    if None in (k[i], d[i], k[i - 1], d[i - 1]):
        return None
    if k[i - 1] <= d[i - 1] and k[i] > d[i] and k[i] < 50:
        return "long"
    if k[i - 1] >= d[i - 1] and k[i] < d[i] and k[i] > 50:
        return "short"
    return None


def e_macd_cross(P, i, A):
    h = P.d["15m"].macd_hist
    if h[i] is None or h[i - 1] is None:
        return None
    if h[i - 1] <= 0 < h[i]:
        return "long"
    if h[i - 1] >= 0 > h[i]:
        return "short"
    return None


def e_engulf(P, i, A):
    b = P.d["15m"].b
    c, o = b[i]["close"], b[i]["open"]
    pc, po = b[i - 1]["close"], b[i - 1]["open"]
    if c > o and pc < po and o <= pc and c >= po:
        return "long"
    if c < o and pc > po and o >= pc and c <= po:
        return "short"
    return None


def e_pinbar(P, i, A):
    b = P.d["15m"].b
    o, h, l, c = b[i]["open"], b[i]["high"], b[i]["low"], b[i]["close"]
    body = abs(c - o) or 1e-9
    up = h - max(o, c)
    dn = min(o, c) - l
    if dn >= 2 * body and up <= body:
        return "long"
    if up >= 2 * body and dn <= body:
        return "short"
    return None


def e_inside(P, i, A):
    b = P.d["15m"].b
    if b[i - 1]["high"] < b[i - 2]["high"] and b[i - 1]["low"] > b[i - 2]["low"]:
        if b[i]["close"] > b[i - 2]["high"]:
            return "long"
        if b[i]["close"] < b[i - 2]["low"]:
            return "short"
    return None


def e_roc(P, i, A):
    r = P.d["15m"].roc[i]
    if r is None:
        return None
    if r > 0.3:
        return "long"
    if r < -0.3:
        return "short"
    return None


ENTRIES = {
    "trend2": e_trend2, "trend3": e_trend3, "ema_cross": e_ema_cross,
    "donch20": e_donch20, "donch10": e_donch10, "boll_break": e_boll_break,
    "boll_rev": e_boll_rev, "rsi_rev": e_rsi_rev, "rsi_trend": e_rsi_trend,
    "stoch_rev": e_stoch_rev, "stoch_cross": e_stoch_cross, "macd_cross": e_macd_cross,
    "engulf": e_engulf, "pinbar": e_pinbar, "inside": e_inside, "roc": e_roc,
}


# ---------------- filters: True = take the trade ----------------
def f_none(P, i, d, live, A):
    return True


def f_htf(P, i, d, live, A):
    return A["1h"][i] == (1 if d == "long" else -1)


def f_adx(P, i, d, live, A):
    a = P.d["15m"].adx[i]
    return a is None or 20 <= a <= 40


def f_atr(P, i, d, live, A):
    a = P.d["15m"].atr[i]
    return a is None or 0.0008 <= a / live <= 0.0025


def f_adxatr(P, i, d, live, A):
    return f_adx(P, i, d, live, A) and f_atr(P, i, d, live, A)


def f_rsi_pb(P, i, d, live, A):
    r = P.d["15m"].rsi[i]
    if r is None:
        return True
    return 35 <= r <= 58 if d == "long" else 42 <= r <= 65


FILTERS = {"none": f_none, "htf": f_htf, "adx": f_adx, "atr": f_atr,
           "adx+atr": f_adxatr, "rsi_pb": f_rsi_pb}


def sim(P, A, entry, flt, stop_pts, rr):
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
        d = entry(P, i, A)
        if d is None:
            continue
        live = bar["close"]
        if not flt(P, i, d, live, A):
            continue
        sg = 1 if d == "long" else -1
        open_t = {"direction": d, "entry": live, "stop": live - sg * stop_pts,
                  "target": live + sg * stop_pts * rr, "rr": rr, "entry_time": t}
        cd = 1
    return trades


def stat(ts):
    if not ts:
        return {"n": 0, "win": 0.0, "r": 0.0, "longs": 0, "shorts": 0}
    w = sum(1 for x in ts if x["result"] == "target")
    return {"n": len(ts), "win": 100 * w / len(ts), "r": sum(x["R"] for x in ts),
            "longs": sum(1 for x in ts if x["direction"] == "long"),
            "shorts": sum(1 for x in ts if x["direction"] == "short")}


def blocks(ts, P, k=3):
    b = P.d["15m"].b; n = len(b)
    out = []
    for j in range(k):
        i0 = min(60 + (n - 60) * j // k, n - 1)
        i1 = min(60 + (n - 60) * (j + 1) // k, n - 1)
        out.append(stat([x for x in ts
                         if b[i0]["openTime"] <= x["entry_time"] <= b[i1]["openTime"]])["r"])
    return out


def month_ok(ts):
    m = collections.defaultdict(float)
    for x in ts:
        m[x["entry_time"][:7]] += x["R"]
    return all(v > 0 for v in m.values()), dict(m)


if __name__ == "__main__":
    P = Frames("futures")
    Ps = Frames("spot")
    A = build(P); As = build(Ps)
    exits = [(9, 18), (10, 15), (10, 12), (10, 20)]
    rows = []
    for en, efn in ENTRIES.items():
        for fn, ffn in FILTERS.items():
            for stop, tgt in exits:
                rr = tgt / stop
                ts = sim(P, A, efn, ffn, float(stop), rr)
                if len(ts) < 25:
                    continue
                s = stat(ts); bl = blocks(ts, P); mok, mo = month_ok(ts)
                ss = stat(sim(Ps, As, efn, ffn, float(stop), rr))
                exp = s["win"] / 100 * tgt - (1 - s["win"] / 100) * stop
                rows.append({"entry": en, "filter": fn, "stop": stop, "tgt": tgt,
                             "rr": rr, "n": s["n"], "win": s["win"], "r": s["r"],
                             "exp": exp, "blocks": bl, "mok": mok,
                             "spot": ss["r"], "spot_n": ss["n"],
                             "longs": s["longs"], "shorts": s["shorts"],
                             "ok": all(x > 0 for x in bl) and mok and s["r"] > 0})
    rows.sort(key=lambda x: (not x["ok"], -x["win"], -x["exp"]))
    json.dump(rows, open(os.path.join(HERE, "mega.json"), "w"), ensure_ascii=False, indent=1)
    hi = [r for r in rows if r["win"] >= 55 and r["r"] > 0]
    print(f"جُرّب {len(rows)} تركيبة · منها {len(hi)} ربحها ≥55% ومجموعها موجب\n")
    print(f"{'المدرسة':12} {'فلتر':8} {'وقف':>4} {'هدف':>4} {'صفقات':>5} {'ربح%':>6} "
          f"{'المجموع':>8} {'$/صفقة':>7} {'ثابت':>5} {'سبوت':>7}")
    print("-" * 84)
    for r in hi[:26]:
        print(f"{r['entry']:12} {r['filter']:8} {r['stop']:4} {r['tgt']:4} {r['n']:5} "
              f"{r['win']:5.0f}% {r['r']:+7.1f}R {r['exp']:+6.2f}$ "
              f"{'✅' if r['ok'] else '—':>4} {r['spot']:+6.1f}R")
