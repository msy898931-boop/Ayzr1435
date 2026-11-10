#!/usr/bin/env python3
"""Fetch all of 2026 gold (XAUUSD) minute bars from Dukascopy and cache them.

Dukascopy serves one LZMA file per day of 1-minute BID candles. We download
every calendar day of 2026 (weekends/holidays come back empty and are skipped),
aggregate to 15m/30m/1h/4h/1d and write backtest/duka_2026.json.

Price scale: raw integer / 1000 = USD (verified: raw 4085470 -> 4085.47).
"""
from __future__ import annotations
import datetime as dt
import json
import lzma
import os
import struct
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "duka_2026.json")
YEAR = 2026
MONTHS = range(1, 12)          # Jan..Nov (Dec has not happened yet)
UA = {"User-Agent": "Mozilla/5.0"}


def _day_url(y, m, d):
    return (f"https://datafeed.dukascopy.com/datafeed/XAUUSD/"
            f"{y}/{m - 1:02d}/{d:02d}/BID_candles_min_1.bi5")


def fetch_day(y, m, d, retries=4):
    """Return [(epoch_sec, o, h, l, c)] for one UTC day, or [] if empty."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(_day_url(y, m, d), headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            if not raw:
                return []
            dec = lzma.decompress(raw)
            n = len(dec) // 24
            base = dt.datetime(y, m, d, tzinfo=dt.timezone.utc)
            out = []
            for i in range(n):
                t, o, c, l, h, _v = struct.unpack(">iiiiif", dec[i * 24:(i + 1) * 24])
                out.append((int((base + dt.timedelta(seconds=t)).timestamp()),
                            o / 1000, h / 1000, l / 1000, c / 1000))
            return out
        except Exception:
            if attempt == retries - 1:
                return []
            time.sleep(2 + attempt * 2)
    return []


def aggregate(mins, secs):
    """Bucket minute bars into `secs`-second candles (UTC-aligned)."""
    buckets = {}
    for ts, o, h, l, c in mins:
        k = ts - (ts % secs)
        if k not in buckets:
            buckets[k] = [o, h, l, c]
        else:
            b = buckets[k]
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
    out = []
    for k in sorted(buckets):
        o, h, l, c = buckets[k]
        out.append({"openTime": dt.datetime.fromtimestamp(k, dt.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": o, "high": h, "low": l, "close": c, "isOpen": False})
    return out


def _days():
    out = []
    for m in MONTHS:
        d = 1
        while True:
            try:
                dt.date(YEAR, m, d)
            except ValueError:
                break
            out.append((m, d))
            d += 1
            if d > 31:
                break
    return out


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    days = _days()
    all_min = []
    days_done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_day, YEAR, m, d): (m, d) for m, d in days}
        for fut in as_completed(futs):
            bars = fut.result()
            if bars:
                all_min.extend(bars)
                days_done += 1
            if days_done and days_done % 25 == 0:
                print(f"  ... {days_done} يوم فيها بيانات", flush=True)
    all_min.sort()
    json.dump([[t, o, h, l, c] for t, o, h, l, c in all_min],
              open(os.path.join(HERE, "duka_2026_min.json"), "w"))
    frames = {
        "5m": aggregate(all_min, 300),
        "15m": aggregate(all_min, 900),
        "30m": aggregate(all_min, 1800),
        "1h": aggregate(all_min, 3600),
        "4h": aggregate(all_min, 14400),
        "1d": aggregate(all_min, 86400),
    }
    json.dump(frames, open(OUT, "w"))
    for tf, b in frames.items():
        span = f"{b[0]['openTime']} → {b[-1]['openTime']}" if b else "—"
        print(f"{tf}: {len(b)} شمعة | {span}")
    print(f"أيام فيها بيانات: {days_done}")


if __name__ == "__main__":
    main()
