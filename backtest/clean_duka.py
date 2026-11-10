#!/usr/bin/env python3
"""Rebuild duka_2026.json keeping only real trading bars.

Dukascopy pads the closed market (all Saturday, most of Sunday, Friday after
the close) with flat filler bars at one price. Those bars are not the market:
they create fake "days" and a dead stretch where nothing can trigger. Keep the
spot-gold session only -- Sunday from 22:00 UTC through Friday 21:00 UTC -- and
drop any leftover bar whose high == low (pure filler, cannot move a trade).
"""
from __future__ import annotations
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def in_session(ts: int) -> bool:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    wd, hr = d.weekday(), d.hour
    if wd == 5:                       # Saturday: closed all day
        return False
    if wd == 6 and hr < 22:           # Sunday: opens ~22:00 UTC
        return False
    if wd == 4 and hr >= 21:          # Friday: closes ~21:00 UTC
        return False
    return True


def main():
    import fetch_duka as FD
    mins = json.load(open(os.path.join(HERE, "duka_2026_min.json")))
    mins = [tuple(x) for x in mins]
    mins.sort()
    keep = [x for x in mins if in_session(x[0]) and x[2] > x[3]]
    print(f"قبل: {len(mins)} · بعد التنظيف: {len(keep)} "
          f"(شلنا {len(mins)-len(keep)})")
    frames = {
        "5m": FD.aggregate(keep, 300),
        "15m": FD.aggregate(keep, 900),
        "30m": FD.aggregate(keep, 1800),
        "1h": FD.aggregate(keep, 3600),
        "4h": FD.aggregate(keep, 14400),
        "1d": FD.aggregate(keep, 86400),
    }
    json.dump(frames, open(os.path.join(HERE, "duka_2026.json"), "w"))
    for tf, b in frames.items():
        print(f"{tf}: {len(b)} شمعة | {b[0]['openTime'][:10]} → {b[-1]['openTime'][:10]}")


if __name__ == "__main__":
    main()
