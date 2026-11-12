#!/usr/bin/env python3
"""Run the desk's OWN decision engine (bot.py's rules) over 2026 bars.

The desk's rules, replayed on the 5m series with no network:
  * trend per frame = EMA20 vs EMA50 (0.05% band)  [bot.frame_trend]
  * entry needs CONFIRM_FRAMES (4) of the 6 frames agreeing
  * the entry must respect the 15m EMA20 (long above / short below)
  * the quality gate (ADX/ATR bands) -- the SAME bands bot.filter_status uses
  * the no-chase gate (RSI on the filter frame) -- the SAME rule bot ships
  * stop = STOP_PICK_USD ($9); target = STOP_PICK_USD * RR
  * one trade at a time; first touch wins, a bar spanning both takes the stop

Frames come from duka_2026.json (Dukascopy XAUUSD), so this is the SAME year
and the SAME fill rule as the combo backtest -- only the entry rules differ.

The bands and the target are READ FROM bot.py, not duplicated: a study that
hardcodes its own numbers drifts away from the bot and quietly measures
something the desk no longer trades.
"""
from __future__ import annotations
import bisect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMES = ("1d", "4h", "1h", "30m", "15m", "5m")
CONFIRM = 4
FILTER_TF = "15m"

# Read the live desk's own constants so this replay can never disagree with it.
try:
    import bot as _bot
    STOP_USD = float(_bot.STOP_PICK_USD)
    RR = float(_bot.TARGET_RR)
    ADX_MIN, ADX_MAX = float(_bot.ADX_MIN), float(_bot.ADX_MAX)
    ATR_MIN_PCT, ATR_MAX_PCT = float(_bot.ATR_MIN_PCT), float(_bot.ATR_MAX_PCT)
    RSI_LONG, RSI_SHORT = _bot.RSI_LONG, _bot.RSI_SHORT
    NOCHASE_ON = bool(_bot.NOCHASE_ON)
    NOCHASE_RSI = float(_bot.NOCHASE_RSI)
    NOCHASE_TF = str(_bot.NOCHASE_TF)
    NOCHASE_SIDES = set(_bot.NOCHASE_SIDES)
except Exception:  # noqa: BLE001 - a standalone study must still run
    STOP_USD, RR = 9.0, 2.2
    ADX_MIN, ADX_MAX = 0.0, 50.0
    ATR_MIN_PCT, ATR_MAX_PCT = 0.0008, 0.0100
    RSI_LONG, RSI_SHORT = (35.0, 58.0), (42.0, 65.0)
    NOCHASE_ON, NOCHASE_RSI, NOCHASE_TF = True, 38.0, "1h"
    NOCHASE_SIDES = {"short", "long"}


def load():
    raw = json.load(open(os.path.join(HERE, "duka_2026.json")))
    trend, times, e20 = {}, {}, {}
    for tf, bars in raw.items():
        cl = [x["close"] for x in bars]
        e20s = _ema_series(cl, 20)
        e50s = _ema_series(cl, 50)
        tr = []
        for a, c in zip(e20s, e50s):
            if a is None or c is None:
                tr.append(0)
            elif a > c * 1.0005:
                tr.append(1)
            elif a < c * 0.9995:
                tr.append(-1)
            else:
                tr.append(0)
        trend[tf] = tr
        times[tf] = [x["openTime"] for x in bars]
        e20[tf] = e20s
    return raw, trend, times, e20


def _ema_series(vals, n):
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


def _at(times, t):
    """Index of the last frame bar whose openTime <= t, or -1."""
    j = bisect.bisect_right(times, t) - 1
    return j


def simulate(filters=False, nochase=True):
    raw, trend, times, e20 = load()
    b5 = raw["5m"]
    t5 = [x["openTime"] for x in b5]
    b15 = raw["15m"]
    t15 = times["15m"]
    macd15 = _macd_hist(b15)
    adx15 = _adx(b15)
    atr15 = _atr_series(b15)
    rsi15 = _rsi_series([x["close"] for x in b15])
    # The no-chase RSI series, computed ONCE for the whole replay.
    nc_times = times.get(NOCHASE_TF, t15)
    nc_rsi = rsi15 if NOCHASE_TF == "15m" else \
        _rsi_series([x["close"] for x in raw[NOCHASE_TF]])
    trades = []
    open_t = None
    for i in range(300, len(b5) - 1):
        bar = b5[i]
        t = bar["openTime"]
        if open_t is not None:
            hi, lo = bar["high"], bar["low"]
            d = open_t["direction"]
            hs = lo <= open_t["stop"] if d == "long" else hi >= open_t["stop"]
            ht = hi >= open_t["target"] if d == "long" else lo <= open_t["target"]
            if hs or ht:
                res = "stop" if hs else "target"
                open_t.update(result=res, R=(-1.0 if res == "stop" else RR),
                              exit_time=t)
                trades.append(open_t)
                open_t = None
            continue
        live = bar["close"]
        up = dn = 0
        for tf in FRAMES:
            j = _at(times[tf], t)
            if j < 0:
                continue
            v = trend[tf][j]
            up += v == 1
            dn += v == -1
        direction = None
        if up >= CONFIRM:
            direction = "long"
        elif dn >= CONFIRM:
            direction = "short"
        if direction is None:
            continue
        # The 15m filter frame is read on the last CLOSED bar, matching the live
        # desk's closed(): the forming bar repaints, and reading it is reading
        # the future (it inflated this backtest from +25R to +58R).
        jc = _at(times[FILTER_TF], t) - 1
        e = e20[FILTER_TF][jc] if jc >= 0 else None
        if e is not None:
            if direction == "long" and live <= e:
                continue
            if direction == "short" and live >= e:
                continue
        if filters:
            j15 = _at(t15, t) - 1
            if j15 >= 0 and not _gate_ok(direction, j15, live, adx15, atr15,
                                         macd15, rsi15):
                continue
        # The no-chase gate, the same rule bot.nochase_status ships: a short may
        # not enter while the filter-frame RSI is deep oversold (a long may not
        # enter deep overbought). Read on the last CLOSED bar of the filter
        # frame -- the live gate reads a closed bar and so must the replay.
        if nochase and NOCHASE_ON and direction in NOCHASE_SIDES:
            jn = _at(nc_times, t) - 1
            if 0 <= jn < len(nc_rsi):
                r = nc_rsi[jn]
                if r is not None:
                    if direction == "short" and r < NOCHASE_RSI:
                        continue
                    if direction == "long" and r > 100.0 - NOCHASE_RSI:
                        continue
        d = 1 if direction == "long" else -1
        open_t = {"direction": direction, "entry": live,
                  "stop": live - d * STOP_USD, "target": live + d * STOP_USD * RR,
                  "rr": RR, "entry_time": t}
    return trades, b5


def _gate_ok(direction, j, live, adx15, atr15, macd15, rsi15,
             gates=("adx", "atr")):
    """The same bands bot.filter_status applies, on the 15m series.

    Default gates match the desk's shipped default (adx+atr). The ADX and ATR
    bands MUST match bot.ADX_MIN/ADX_MAX and bot.ATR_MIN_PCT/ATR_MAX_PCT, or the
    study measures a different bot than the one that trades. test_engine pins
    that they agree (the old 20-40 / 0.25% here was four times tighter than the
    live 0-50 / 1.00% and understated the trade count).
    """
    adx_v = adx15[j]
    atr_v = atr15[j]
    h = macd15[j]
    hp = macd15[j - 1] if j >= 1 else None
    r = rsi15[j]
    if "adx" in gates and adx_v is not None and not (ADX_MIN <= adx_v <= ADX_MAX):
        return False
    if "atr" in gates and atr_v is not None and live > 0 \
            and not (ATR_MIN_PCT <= atr_v / live <= ATR_MAX_PCT):
        return False
    if "macd" in gates and h is not None and hp is not None \
            and not (h > hp if direction == "long" else h < hp):
        return False
    if "rsi" in gates and r is not None:
        lo, hi = RSI_LONG if direction == "long" else RSI_SHORT
        if not (lo <= r <= hi):
            return False
    return True


def _macd_hist(b):
    cl = [x["close"] for x in b]
    ef, es = _ema_series(cl, 12), _ema_series(cl, 26)
    line = [None if (ef[i] is None or es[i] is None) else ef[i] - es[i]
            for i in range(len(cl))]
    sig = _ema_series([x if x is not None else 0.0 for x in line], 9)
    return [None if (line[i] is None or sig[i] is None) else line[i] - sig[i]
            for i in range(len(cl))]


def _adx(b, n=14):
    if len(b) < 2 * n + 1:
        return [None] * len(b)
    plus, minus, trs = [0.0], [0.0], [0.0]
    for i in range(1, len(b)):
        up = b[i]["high"] - b[i - 1]["high"]
        dn = b[i - 1]["low"] - b[i]["low"]
        plus.append(up if (up > dn and up > 0) else 0.0)
        minus.append(dn if (dn > up and dn > 0) else 0.0)
        h, lo, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))

    def wilder(vals):
        out = [None] * len(vals)
        s = sum(vals[1:n + 1])
        out[n] = s
        for i in range(n + 1, len(vals)):
            s = s - s / n + vals[i]
            out[i] = s
        return out

    atr_w, pdm, mdm = wilder(trs), wilder(plus), wilder(minus)
    dx = [None] * len(b)
    for i in range(len(b)):
        a, p, m = atr_w[i], pdm[i], mdm[i]
        if a and p is not None and m is not None:
            pdi = 100 * p / a if a else 0.0
            mdi = 100 * m / a if a else 0.0
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0
    out = [None] * len(b)
    if dx[n] is not None:
        out[n] = dx[n]
        for i in range(n + 1, len(b)):
            if dx[i] is None or out[i - 1] is None:
                continue
            out[i] = (out[i - 1] * (n - 1) + dx[i]) / n
    return out


def _atr_series(b, n=14):
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


def _rsi_series(vals, n=14):
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


def stats(trades):
    closed = [x for x in trades if x.get("result")]
    n = len(closed)
    if not n:
        return {"n": 0}
    wins = sum(1 for x in closed if x["result"] == "target")
    tot = sum(x["R"] for x in closed)
    exp = wins / n * RR - (1 - wins / n) * 1.0
    return {"n": n, "win": 100 * wins / n, "r": tot, "usd": exp * STOP_USD}


def thirds(trades, bars):
    closed = [x for x in trades if x.get("result")]
    if not closed:
        return []
    lo = bars[0]["openTime"]
    hi = bars[-1]["openTime"]
    span = len(closed) // 3
    if span == 0:
        return [sum(x["R"] for x in closed)]
    return [sum(x["R"] for x in closed[k * span:(k + 1) * span])
            for k in range(3)] + ([sum(x["R"] for x in closed[3 * span:])]
                                  if len(closed) % 3 else [])


if __name__ == "__main__":
    trades, b5 = simulate()
    s = stats(trades)
    print("═══ البوت (bot.py) على 2026 كامل ═══")
    print(f"  صفقات: {s['n']} · ربح {s['win']:.1f}% · المجموع {s['r']:+.1f}R "
          f"· ${s['usd']:+.2f}/صفقة")
    print(f"  الأثلاث: {[round(x, 1) for x in thirds(trades, b5)]}")
    bal = peak = mdd = 0
    for x in trades:
        if x.get("result"):
            bal += x["R"]
            peak = max(peak, bal)
            mdd = min(mdd, bal - peak)
    print(f"  أقصى تراجع: {mdd:.1f}R")
    import collections
    mo = collections.defaultdict(list)
    for x in trades:
        if x.get("result"):
            mo[x["entry_time"][:7]].append(x["R"])
    print("  الشهور:", end=" ")
    for k in sorted(mo):
        print(f"{k[5:]}:{len(mo[k])}pf/{sum(mo[k]):+.0f}R", end="  ")
    print()
