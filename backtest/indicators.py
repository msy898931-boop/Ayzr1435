#!/usr/bin/env python3
"""Pure-stdlib indicators for the new filter experiments.

Each function takes a list of bars (dicts with open/high/low/close, and volume
where available) and returns a list aligned to the bars, None until it warms up.
"""
from __future__ import annotations


def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


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


def stdev_series(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return out


def bollinger(bars, n=20, k=2.0):
    cl = [x["close"] for x in bars]
    mid = sma(cl, n)
    sd = stdev_series(cl, n)
    up = [None] * len(cl)
    lo = [None] * len(cl)
    for i in range(len(cl)):
        if mid[i] is not None and sd[i] is not None:
            up[i] = mid[i] + k * sd[i]
            lo[i] = mid[i] - k * sd[i]
    return mid, up, lo


def stochastic(bars, n=14, d=3):
    k = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        w = bars[i - n + 1:i + 1]
        hi = max(x["high"] for x in w)
        lo = min(x["low"] for x in w)
        k[i] = 50.0 if hi == lo else 100 * (bars[i]["close"] - lo) / (hi - lo)
    kk = [x if x is not None else None for x in k]
    dd = sma([x if x is not None else 0 for x in kk], d)
    return kk, dd


def macd(bars, fast=12, slow=26, sig=9):
    cl = [x["close"] for x in bars]
    ef, es = ema_series(cl, fast), ema_series(cl, slow)
    line = [None] * len(cl)
    for i in range(len(cl)):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]
    line2 = [x if x is not None else 0 for x in line]
    signal = ema_series(line2, sig)
    hist = [None] * len(cl)
    for i in range(len(cl)):
        if line[i] is not None and signal[i] is not None:
            hist[i] = line[i] - signal[i]
    return line, signal, hist


def atr_series(bars, n=14):
    out = [None] * len(bars)
    if len(bars) < n + 1:
        return out
    trs = [None]
    for i in range(1, len(bars)):
        h, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def adx(bars, n=14):
    """Wilder ADX: trend strength, 0-100 (no direction)."""
    if len(bars) < 2 * n + 1:
        return [None] * len(bars)
    plus_dm, minus_dm, trs = [None], [None], [None]
    for i in range(1, len(bars)):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    def wilder(vals, n):
        out = [None] * len(vals)
        s = sum(vals[1:n + 1])
        out[n] = s
        for i in range(n + 1, len(vals)):
            s = s - s / n + vals[i]
            out[i] = s
        return out
    atr_w = wilder(trs, n)
    pdm = wilder(plus_dm, n)
    mdm = wilder(minus_dm, n)
    dx = [None] * len(bars)
    for i in range(len(bars)):
        if atr_w[i] and pdm[i] is not None and mdm[i] is not None:
            pdi = 100 * pdm[i] / atr_w[i] if atr_w[i] else 0
            mdi = 100 * mdm[i] / atr_w[i] if atr_w[i] else 0
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
    out = [None] * len(bars)
    first = n
    if dx[first] is not None:
        out[first] = dx[first]
        for i in range(first + 1, len(bars)):
            if dx[i] is None or out[i - 1] is None:
                continue
            out[i] = (out[i - 1] * (n - 1) + dx[i]) / n
    return out


def cci(bars, n=20):
    tp = [(x["high"] + x["low"] + x["close"]) / 3 for x in bars]
    m = sma(tp, n)
    out = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        w = tp[i - n + 1:i + 1]
        md = sum(abs(x - m[i]) for x in w) / n
        out[i] = 0.0 if md == 0 else (tp[i] - m[i]) / (0.015 * md)
    return out


def williams_r(bars, n=14):
    out = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        w = bars[i - n + 1:i + 1]
        hi = max(x["high"] for x in w)
        lo = min(x["low"] for x in w)
        out[i] = -50.0 if hi == lo else -100 * (hi - bars[i]["close"]) / (hi - lo)
    return out


def roc(vals, n=10):
    out = [None] * len(vals)
    for i in range(n, len(vals)):
        if vals[i - n]:
            out[i] = 100 * (vals[i] - vals[i - n]) / vals[i - n]
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
