#!/usr/bin/env python3
"""Independent live read of gold -- my own tools, not the desk's.

Fetches GC=F (COMEX gold futures) at six frames from Yahoo, computes the
indicators I trust (EMA20/50/200, ATR14, RSI14, a Wilder ADX14), finds swing
pivots and clusters them into support/resistance zones near price, then prints a
direction read and two scenarios with entry/stop/target. Read-only.
"""
from __future__ import annotations
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
FRAMES = [("5m", "5m", "5d"), ("15m", "15m", "1mo"), ("30m", "30m", "1mo"),
          ("1h", "60m", "3mo"), ("4h", "1h", "6mo"), ("1d", "1d", "1y")]


def fetch(symbol, interval, rng):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.load(r)["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    ts = d["timestamp"]
    out = []
    for i in range(len(ts)):
        if q["close"][i] is None:
            continue
        out.append({"t": ts[i], "o": q["open"][i], "h": q["high"][i],
                    "l": q["low"][i], "c": q["close"][i]})
    return d["meta"], out


def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def rsi(bars, n=14):
    if len(bars) < n + 1:
        return None
    g = ls = 0.0
    for i in range(1, n + 1):
        d = bars[i]["c"] - bars[i - 1]["c"]
        g += max(d, 0)
        ls += max(-d, 0)
    ag, al = g / n, ls / n
    for i in range(n + 1, len(bars)):
        d = bars[i]["c"] - bars[i - 1]["c"]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)


def adx(bars, n=14):
    if len(bars) < 2 * n:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, len(bars)):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdm.append(up if (up > dn and up > 0) else 0)
        ndm.append(dn if (dn > up and dn > 0) else 0)
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    def sm(x):
        s = sum(x[:n])
        out = [s]
        for v in x[n:]:
            s = s - s / n + v
            out.append(s)
        return out
    tr, p, m = sm(trs), sm(pdm), sm(ndm)
    dxs = []
    for i in range(len(tr)):
        if tr[i] == 0:
            continue
        pdi, ndi = 100 * p[i] / tr[i], 100 * m[i] / tr[i]
        s = pdi + ndi
        dxs.append(100 * abs(pdi - ndi) / s if s else 0)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for v in dxs[n:]:
        a = (a * (n - 1) + v) / n
    return a


def pivots(bars, k=2):
    """Fractal swing highs/lows."""
    hi, lo = [], []
    for i in range(k, len(bars) - k):
        w = bars[i - k:i + k + 1]
        if bars[i]["h"] == max(x["h"] for x in w):
            hi.append(bars[i]["h"])
        if bars[i]["l"] == min(x["l"] for x in w):
            lo.append(bars[i]["l"])
    return hi, lo


def zones(levels, price, band=6.0, min_touch=2):
    """Cluster levels within `band` $; keep clusters with >= min_touch."""
    lv = sorted(levels)
    clusters, cur = [], []
    for x in lv:
        if cur and x - cur[-1] > band:
            clusters.append(cur)
            cur = []
        cur.append(x)
    if cur:
        clusters.append(cur)
    out = []
    for c in clusters:
        if len(c) >= min_touch:
            out.append((round(sum(c) / len(c), 1), len(c)))
    return sorted(out, key=lambda z: z[0])


def main():
    data, meta = {}, None
    for name, iv, rng in FRAMES:
        try:
            m, b = fetch("GC=F", iv, rng)
            data[name] = b
            meta = m
        except Exception as e:  # noqa: BLE001
            print(f"  [{name}] fetch failed: {e}")
    px = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose")
    print(f"=== XAUUSD (GC=F futures) live ===")
    print(f"price {px}  prev close {prev}  "
          f"({100*(px-prev)/prev:+.2f}%)  as of {meta.get('regularMarketTime')}\n")
    print(f"{'frame':>5} {'last':>9} {'EMA20':>9} {'EMA50':>9} {'EMA200':>9} "
          f"{'ATR':>6} {'RSI':>5} {'ADX':>5} {'trend':>10}")
    trend_votes = []
    for name, _, _ in FRAMES:
        b = data.get(name)
        if not b:
            continue
        c = [x["c"] for x in b]
        e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
        a, r, x = atr(b), rsi(b), adx(b)
        last = c[-1]
        # trend = price vs EMA20 and EMA20 vs EMA50
        t = "—"
        if e20 and e50:
            if last > e20 > e50:
                t = "صاعد"
                trend_votes.append(1)
            elif last < e20 < e50:
                t = "هابط"
                trend_votes.append(-1)
            else:
                t = "عرضي"
                trend_votes.append(0)
        def f(v):
            return f"{v:.1f}" if v is not None else "—"
        print(f"{name:>5} {last:>9.1f} {f(e20):>9} {f(e50):>9} {f(e200):>9} "
              f"{f(a):>6} {f(r):>5} {f(x):>5} {t:>10}")
    net = sum(trend_votes)
    print(f"\nتصويت الاتجاه عبر الأطر: {net:+d}  "
          f"({'صاعد' if net > 0 else 'هابط' if net < 0 else 'مختلط'})\n")
    # zones from the 15m/1h/4h pivots
    lv = []
    for name in ("15m", "1h", "4h"):
        if name in data:
            hi, lo = pivots(data[name])
            lv += hi + lo
    z = zones(lv, px)
    res = [x for x in z if x[0] > px]
    sup = [x for x in z if x[0] < px]
    print("أقرب مقاومات:", [f"{p} ({n})" for p, n in res[:4]])
    print("أقرب دعوم  :", [f"{p} ({n})" for p, n in sup[-4:]])
    # day range from 5m
    if "5m" in data:
        b5 = data["5m"]
        print(f"\nمدى اليوم (5m): أعلى {max(x['h'] for x in b5):.1f} · "
              f"أدنى {min(x['l'] for x in b5):.1f}")
    return data


if __name__ == "__main__":
    main()
