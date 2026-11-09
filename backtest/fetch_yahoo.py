#!/usr/bin/env python3
"""Fetch longer gold history from Yahoo for a bigger backtest sample.

The desk's own feed (biquote) serves only ~193 15m bars (~2 days), far too few
to judge a filter. Yahoo's GC=F (COMEX gold futures) gives up to 60 days of 15m
bars. It is a PROXY -- futures track spot closely but not exactly -- so results
here are indicative, not a verdict on XAUUSD.
"""
from __future__ import annotations
import json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(symbol: str, interval: str, rng: str) -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"openTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                    "open": float(o), "high": float(h), "low": float(l),
                    "close": float(c), "isOpen": False})
    return out


if __name__ == "__main__":
    for tf, rng in (("15m", "60d"), ("1h", "60d"), ("5m", "60d")):
        try:
            b = fetch("GC=F", tf, rng)
            json.dump(b, open(os.path.join(HERE, f"yahoo_{tf}.json"), "w"))
            print(f"{tf}: {len(b)} شمعة | {b[0]['openTime']} → {b[-1]['openTime']}")
        except Exception as e:
            print(f"{tf}: خطأ {e}")
