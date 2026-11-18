"""AYZR — an INDEPENDENT market reader for XAUUSD.

This file shares NOTHING with bot.py's decision engine and does not import it.
It reads the same public candle/quote FEED (data is data), but everything on top
is my own: my own indicators, my own swing structure, my own support/resistance
clusters, my own supply/demand and order-block zones, my own liquidity map
(equal highs/lows, stop sweeps), my own plan, my own strategy and scenarios.

Run it:  python3 ayzr.py
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape as _unescape
from itertools import pairwise

# ---------------------------------------------------------------- feed + cfg
BIQUOTE = "https://biquote.io"
SYMBOL = os.environ.get("AYZR_SYMBOL", "XAUUSD")
UA = "Mozilla/5.0 (AyzrDesk/1.0)"

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}
FRAMES = ("1d", "4h", "1h", "30m", "15m", "5m")
TF_AR = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
         "15m": "15د", "5m": "5د"}

# The owner's account: $200, 0.01 lot. On XAUUSD 1 lot = 100 oz, so 0.01 lot
# moves $1 for every $1 the metal moves -- one "point" here is $1 of risk.
ACCOUNT_USD = 200.0
LOT = 0.01
USD_PER_POINT = 1.0
RISK_USD = 10.0          # the desk's risk window centre ($8-12)
RISK_MIN, RISK_MAX = 8.0, 12.0
RR_TARGET = 2.0


def num(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f}"


# ---------------------------------------------------------------- data layer
def _get(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def quote() -> dict:
    return _get(f"{BIQUOTE}/api/{SYMBOL}")


def bars(tf: str, limit: int = 400) -> list[dict]:
    b = _get(f"{BIQUOTE}/api/{SYMBOL}/ohlc?interval={tf}&limit={limit}")
    out = b.get("bars") or []
    return sorted(out, key=lambda x: x["openTime"])


def closed(b: list[dict], tf: str) -> list[dict]:
    """Drop the bar still forming -- its close repaints as it fills."""
    if not b:
        return b
    dur = TF_SECONDS.get(tf)
    if not dur:
        return b
    try:
        last = datetime.fromisoformat(b[-1]["openTime"])
    except (ValueError, KeyError):
        return b
    if (datetime.now(UTC) - last).total_seconds() < dur:
        return b[:-1]
    return b


# ---------------------------------------------------------------- indicators
def sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def ema(vals: list[float], n: int) -> float | None:
    s = ema_series(vals, n)
    return s[-1] if s else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for a, b in pairwise(closes[1:n + 1]):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for a, b in pairwise(closes[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def true_ranges(b: list[dict]) -> list[float]:
    out = []
    for i in range(1, len(b)):
        h, low, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        out.append(max(h - low, abs(h - pc), abs(low - pc)))
    return out


def atr(b: list[dict], n: int = 14) -> float | None:
    trs = true_ranges(b)
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(b: list[dict], n: int = 14) -> tuple[float, float, float] | None:
    """Wilder's ADX. Returns (adx, +DI, -DI)."""
    if len(b) < 2 * n + 1:
        return None
    plus, minus, trs = [], [], []
    for i in range(1, len(b)):
        up = b[i]["high"] - b[i - 1]["high"]
        dn = b[i - 1]["low"] - b[i]["low"]
        plus.append(up if (up > dn and up > 0) else 0.0)
        minus.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(b[i]["high"] - b[i]["low"],
                       abs(b[i]["high"] - b[i - 1]["close"]),
                       abs(b[i]["low"] - b[i - 1]["close"])))

    def _wilder(vals: list[float]) -> list[float]:
        s = sum(vals[:n])
        out = [s]
        for v in vals[n:]:
            s = s - s / n + v
            out.append(s)
        return out

    str_, sp, sm = _wilder(trs), _wilder(plus), _wilder(minus)
    pdi = [100 * p / t if t else 0 for p, t in zip(sp, str_)]
    mdi = [100 * m / t if t else 0 for m, t in zip(sm, str_)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0
          for p, m in zip(pdi, mdi)]
    if len(dx) < n:
        return None
    a = sum(dx[:n]) / n
    for v in dx[n:]:
        a = (a * (n - 1) + v) / n
    return a, pdi[-1], mdi[-1]


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         sig: int = 9) -> tuple[float, float, float] | None:
    if len(closes) < slow + sig:
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    line = [a - b for a, b in zip(ef, es)
            if a is not None and b is not None]
    if len(line) < sig:
        return None
    esig = ema_series(line, sig)
    m, s = line[-1], esig[-1]
    if s is None:
        return None
    return m, s, m - s


def stoch(b: list[dict], k: int = 14, d: int = 3) -> tuple[float, float] | None:
    if len(b) < k + d:
        return None
    ks = []
    for i in range(k - 1, len(b)):
        win = b[i - k + 1:i + 1]
        hi = max(x["high"] for x in win)
        lo = min(x["low"] for x in win)
        c = b[i]["close"]
        ks.append(100 * (c - lo) / (hi - lo) if hi > lo else 50.0)
    kv = ks[-1]
    dv = sum(ks[-d:]) / d
    return kv, dv


# ---------------------------------------------------------------- swings
def swings(b: list[dict], left: int = 2, right: int = 2
           ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Fractal pivots: a high that is the highest of its `left`/`right`
    neighbours, a low that is the lowest. Returns (highs, lows) as
    (index, price), oldest first."""
    highs, lows = [], []
    for i in range(left, len(b) - right):
        h = b[i]["high"]
        low = b[i]["low"]
        if all(h >= b[j]["high"] for j in range(i - left, i + right + 1)
               if j != i):
            highs.append((i, h))
        if all(low <= b[j]["low"] for j in range(i - left, i + right + 1)
               if j != i):
            lows.append((i, low))
    return highs, lows


def structure(b: list[dict]) -> dict:
    """My own read of market structure: the swing sequence (HH/HL/LH/LL), the
    trend it implies, and the last break (BOS continuation or CHoCH reversal)."""
    highs, lows = swings(b, 2, 2)
    out = {"highs": highs[-6:], "lows": lows[-6:], "trend": 0,
           "labels": [], "event": None}
    seq = sorted([(i, p, "H") for i, p in highs] +
                 [(i, p, "L") for i, p in lows])
    labs = []
    last_h = last_l = None
    for _i, p, kind in seq:
        if kind == "H":
            if last_h is not None:
                labs.append(("HH" if p > last_h else "LH", p))
            last_h = p
        else:
            if last_l is not None:
                labs.append(("HL" if p > last_l else "LL", p))
            last_l = p
    out["labels"] = labs[-6:]
    ups = sum(1 for x, _ in labs[-4:] if x in ("HH", "HL"))
    dns = sum(1 for x, _ in labs[-4:] if x in ("LH", "LL"))
    out["trend"] = 1 if ups > dns else (-1 if dns > ups else 0)

    # the last break of a swing: close beyond the prior swing point
    if len(highs) >= 2 and len(lows) >= 2:
        ph, pl = highs[-1][1], lows[-1][1]
        c = b[-1]["close"]
        if c > ph:
            out["event"] = ("BOS↑" if out["trend"] == 1 else "CHoCH↑", ph)
        elif c < pl:
            out["event"] = ("BOS↓" if out["trend"] == -1 else "CHoCH↓", pl)
    return out


# ---------------------------------------------------------------- levels
def sr_levels(b: list[dict], tol: float, ref: float, top: int = 6,
              extra: list[float] | None = None
              ) -> tuple[list[dict], list[dict]]:
    """Cluster swing pivots into support/resistance bands.

    Pivots within `tol` of each other merge into one band; the band carries its
    real low/high (so a wide band is shown as a ZONE, not one fake price) and a
    strength equal to the pivot count. `extra` candidates (round numbers, the
    day's high/low) do NOT merge with each other -- they only join a pivot band
    when they fall inside it, otherwise they stand alone at one touch. Merging
    the extras together would chain ten round levels into one meaningless band
    with a fake 40-touch score, which is worse than no level at all.

    `ref` is the LIVE price: a band above it is resistance, below is support,
    and `dist` is measured from it -- the same price the header prints, so the
    reader never sees a distance counted from a bar they cannot see.
    """
    highs, lows = swings(b, 2, 2)
    pts = [p for _i, p in highs] + [p for _i, p in lows]
    pts.sort()
    clusters: list[dict] = []
    if pts:
        cur = [pts[0]]
        for p in pts[1:]:
            if p - cur[-1] <= tol:
                cur.append(p)
            else:
                clusters.append({"pts": cur})
                cur = [p]
        clusters.append({"pts": cur})
    for e in (extra or []):
        hit = None
        for c in clusters:
            if abs(sum(c["pts"]) / len(c["pts"]) - e) <= tol:
                hit = c
                break
        if hit is not None:
            hit["pts"].append(e)
        else:
            clusters.append({"pts": [e]})
    res: list[dict] = []
    sup: list[dict] = []
    for c in clusters:
        price = sum(c["pts"]) / len(c["pts"])
        band = {"price": price, "lo": min(c["pts"]), "hi": max(c["pts"]),
                "touches": len(c["pts"]), "dist": abs(ref - price)}
        (res if price > ref else sup).append(band)
    res.sort(key=lambda x: x["price"])
    sup.sort(key=lambda x: -x["price"])
    return res[:top], sup[:top]


def nearest_levels(live: float, res: list[dict], sup: list[dict],
                   atr_v: float) -> tuple[list[float], list[float]]:
    """The rungs above and below, guaranteed non-empty.

    Real clustered levels first; if the market has run past every one of them,
    fall back to ATR-spaced projections so the plan and the scenarios always
    have a floor and a ceiling to name -- never a blank line.
    """
    ups = sorted([x["price"] for x in res if x["price"] > live])
    dns = sorted([x["price"] for x in sup if x["price"] < live],
                 reverse=True)
    step = max(atr_v, 5.0)
    while len(ups) < 3:
        ups.append((ups[-1] if ups else live) + step)
    while len(dns) < 3:
        dns.append((dns[-1] if dns else live) - step)
    return ups, dns


def round_levels(close: float, step: float = 10.0, span: float = 120.0
                 ) -> list[float]:
    lo = int((close - span) // step * step)
    hi = int((close + span) // step * step)
    return [float(x) for x in range(lo, hi + 1, int(step))]


# ---------------------------------------------------------------- zones
def order_blocks(b: list[dict], look: int = 3, atr_v: float = 1.0
                 ) -> tuple[list[dict], list[dict]]:
    """Order blocks: the last opposite candle before a strong impulse.

    A demand block is the last down candle before a run of up candles that
    clears `look` bars by more than `atr_v`; a supply block is its mirror. Each
    zone is the candle's own body/range, so price returning there is a real
    revisit of the origin of the move."""
    demand, supply = [], []
    for i in range(look, len(b) - look):
        o, c = b[i]["open"], b[i]["close"]
        hi, lo = b[i]["high"], b[i]["low"]
        fwd = b[i + 1:i + 1 + look]
        up_move = max(x["high"] for x in fwd) - hi
        dn_move = lo - min(x["low"] for x in fwd)
        if c < o and up_move > atr_v:           # down candle -> demand
            demand.append({"top": max(o, c), "bot": lo, "i": i})
        if c > o and dn_move > atr_v:           # up candle -> supply
            supply.append({"top": hi, "bot": min(o, c), "i": i})
    return demand[-4:], supply[-4:]


def fair_value_gaps(b: list[dict], atr_v: float = 1.0
                    ) -> tuple[list[dict], list[dict]]:
    """Three-candle imbalance: candle 1's high below candle 3's low (bullish
    gap) or candle 1's low above candle 3's high (bearish gap)."""
    bull, bear = [], []
    for i in range(1, len(b) - 1):
        a, c = b[i - 1], b[i + 1]
        if a["high"] < c["low"] and (c["low"] - a["high"]) > atr_v * 0.3:
            bull.append({"bot": a["high"], "top": c["low"], "i": i})
        if a["low"] > c["high"] and (a["low"] - c["high"]) > atr_v * 0.3:
            bear.append({"top": a["low"], "bot": c["high"], "i": i})
    return bull[-4:], bear[-4:]


# ---------------------------------------------------------------- liquidity
def liquidity(b: list[dict], tol: float) -> dict:
    """Where the stops sit, and whether they were just hunted.

    Equal highs = buy-side liquidity (stops of shorts sit just above); equal
    lows = sell-side liquidity. A SWEEP is a bar that pokes beyond a prior
    swing then closes back inside -- the classic stop-hunt / false break."""
    highs, lows = swings(b, 2, 2)
    eq_hi, eq_lo = [], []
    for (i, p), (j, q) in pairwise([(i, p) for i, p in highs]):
        if abs(p - q) <= tol:
            eq_hi.append((p, q, j))
    for (i, p), (j, q) in pairwise([(i, p) for i, p in lows]):
        if abs(p - q) <= tol:
            eq_lo.append((p, q, j))
    sweeps = []
    if highs and lows:
        ph = highs[-1][1]
        pl = lows[-1][1]
        last = b[-1]
        if last["high"] > ph and last["close"] < ph:
            sweeps.append(("قنص قمة", ph))
        if last["low"] < pl and last["close"] > pl:
            sweeps.append(("قنص قاع", pl))
    return {"eq_highs": eq_hi[-3:], "eq_lows": eq_lo[-3:],
            "sweeps": sweeps,
            "buy_side": highs[-1][1] if highs else None,
            "sell_side": lows[-1][1] if lows else None}


# ---------------------------------------------------------------- candles
def candle_read(b: list[dict]) -> list[str]:
    """Read the last few candles the way an eye does: body, wicks, and the
    simple patterns (engulfing, pin, doji)."""
    out = []
    for x in b[-3:]:
        o, c, h, low = x["open"], x["close"], x["high"], x["low"]
        rng = h - low
        body = abs(c - o)
        if rng == 0:
            continue
        bp = 100 * body / rng
        up_wick = h - max(o, c)
        dn_wick = min(o, c) - low
        t = x["openTime"][11:16]
        kind = "صاعدة" if c >= o else "هابطة"
        tag = ""
        if bp < 20:
            tag = " (دوجي — حيرة)"
        elif dn_wick > 2 * body and up_wick < body:
            tag = " (مطرقة/دبوس سفلي)"
        elif up_wick > 2 * body and dn_wick < body:
            tag = " (شهاب/دبوس علوي)"
        out.append(f"{t} {kind} {num(o)}→{num(c)} · جسم {bp:.0f}% "
                   f"· فتيل {up_wick:.0f}/{dn_wick:.0f}{tag}")
    if len(b) >= 2:
        a, x = b[-2], b[-1]
        if (x["close"] > x["open"] and a["close"] < a["open"]
                and x["close"] >= a["open"] and x["open"] <= a["close"]):
            out.append("🟢 شمعة ابتلاع صاعدة (bullish engulfing)")
        if (x["close"] < x["open"] and a["close"] > a["open"]
                and x["close"] <= a["open"] and x["open"] >= a["close"]):
            out.append("🔴 شمعة ابتلاع هابطة (bearish engulfing)")
    return out


# ---------------------------------------------------------------- per frame
def frame_view(tf: str, b_all: list[dict], live: float) -> dict:
    b = closed(b_all, tf)
    if len(b) < 30:
        return {"tf": tf, "label": TF_AR[tf], "ok": False}
    closes = [x["close"] for x in b]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes, 14)
    a = atr(b, 14)
    ad = adx(b, 14)
    mac = macd(closes)
    st = stoch(b)
    if e20 and e50:
        tilt = 1 if live > e20 > e50 else (-1 if live < e20 < e50 else 0)
    else:
        tilt = 0
    return {"tf": tf, "label": TF_AR[tf], "ok": True, "trend": tilt,
            "rsi": r, "atr": a, "ema20": e20, "ema50": e50, "ema200": e200,
            "adx": ad[0] if ad else None,
            "pdi": ad[1] if ad else None, "mdi": ad[2] if ad else None,
            "macd": mac, "stoch": st, "close": closes[-1]}


# ---------------------------------------------------------------- plan
def build_plan(live: float, direction: str, stop_level: float,
               targets: list[float], min_rr: float = 1.5) -> dict:
    """Size a trade on my own rules: risk in the $8-12 window, R:R >= min_rr.

    The stop must sit on the LOSING side and every target on the WINNING side,
    or the trade is nonsense -- a short whose "target" is above entry is not a
    plan, it is a typo with a good R:R. A target that is too CLOSE is dropped
    too: a rung 2 points away against a 12-point stop is R:R 0.2, a losing trade
    dressed as a plan. Only rungs that clear `min_rr` are kept.
    """
    d = 1 if direction == "long" else -1
    pts = (live - stop_level) * d / USD_PER_POINT
    pts = max(RISK_MIN / USD_PER_POINT,
              min(max(pts, RISK_MIN / USD_PER_POINT), RISK_MAX / USD_PER_POINT))
    stop = live - d * pts * USD_PER_POINT
    risk = abs(live - stop) * USD_PER_POINT
    good = [t for t in targets
            if (t - live) * d >= risk * min_rr / USD_PER_POINT]
    tgt = good[0] if good else live + d * risk * max(RR_TARGET, min_rr)
    gain = max(0.0, (tgt - live) * d) * USD_PER_POINT
    rr = gain / risk if risk else 0.0
    return {"direction": direction, "entry": live, "stop": stop,
            "target": tgt, "targets": good, "risk_usd": risk,
            "gain_usd": gain, "rr": rr, "pts": pts}


# ---------------------------------------------------------------- news
# My own news layer: keyless Google News RSS, Arabic, filtered to what actually
# moves gold (the metal, the dollar, the Fed/rate/inflation path) and stripped
# of retail noise (karat shop prices, other currencies). INFORMATION ONLY --
# news never opens, blocks or sizes a trade here; it only colours the read.
_NEWS_CACHE: dict = {"at": 0.0, "items": []}
NEWS_CACHE_S = 600
NEWS_QUERIES = tuple(
    q.strip() for q in os.environ.get(
        "AYZR_NEWS_QUERY",
        "الذهب الفوري|مؤشر الدولار|الفيدرالي الفائدة|التضخم الأميركي").split("|")
    if q.strip())
NEWS_LANG = os.environ.get("AYZR_NEWS_LANG", "ar").strip()
NEWS_REGION = os.environ.get("AYZR_NEWS_REGION", "SA").strip()
_NEWS_KEEP = ("ذهب", "gold", "xau", "أونصة", "اونصة", "أوقية", "اوقية", "نفيسة",
              "دولار", "dollar", "dxy", "فيدرالي", "فدرالي", "الاحتياطي",
              "المركزي", "fomc", "باول", "تضخم", "inflation", "cpi",
              "فائدة", "عائد", "yield")
_NEWS_NOISE = ("عيار", "الكندي", "الأسترالي", "الاسترالي", "النيوزلندي",
               "الجنيه", "محلات", "أسعار الذهب اليوم", "في مصر", "في السعودية",
               "في الإمارات", "في الكويت", "في قطر", "في الأردن", "في سوريا",
               "في لبنان", "في العراق", "في تركيا", "في المغرب")


def _news_keep(title: str) -> bool:
    low = title.lower()
    if any(w in low for w in _NEWS_NOISE):
        return False
    return any(w in low for w in _NEWS_KEEP)


def _news_tag(title: str) -> str:
    low = title.lower()
    if any(w in low for w in ("رفع", "تشديد", "فائدة", "yield", "hawk")):
        return "🦅"
    if any(w in low for w in ("خفض", "تيسير", "cut", "dove")):
        return "🕊️"
    if any(w in low for w in ("تضخم", "inflation", "cpi")):
        return "🔥"
    if any(w in low for w in ("دولار", "dollar", "dxy")):
        return "📈"
    return "▫️"


def _news_one(query: str) -> list[tuple[datetime, str, str]]:
    q = urllib.parse.quote(query)
    url = (f"https://news.google.com/rss/search?q={q}"
           f"&hl={NEWS_LANG}&gl={NEWS_REGION}&ceid={NEWS_REGION}:{NEWS_LANG}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "ignore")
    out = []
    for block in re.findall(r"<item>(.*?)</item>", raw, re.DOTALL)[:10]:
        m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        if not m:
            continue
        title = _unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip())
        stamp = datetime.min.replace(tzinfo=UTC)
        when = ""
        pm = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
        if pm:
            with suppress(Exception):  # a bad date must not kill the feed
                stamp = parsedate_to_datetime(pm.group(1).strip())
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                when = stamp.astimezone(UTC).strftime("%d/%m %H:%M")
        out.append((stamp, title, when))
    return out


def news_headlines(limit: int = 8) -> list[tuple[str, str, str]]:
    """Fresh gold/dollar/Fed headlines, newest first, cached for ten minutes."""
    now = time.monotonic()
    if _NEWS_CACHE["items"] and now - _NEWS_CACHE["at"] < NEWS_CACHE_S:
        return list(_NEWS_CACHE["items"])[:limit]
    merged: list[tuple[datetime, str, str]] = []
    for query in NEWS_QUERIES:
        try:
            merged += _news_one(query)
        except Exception:  # noqa: BLE001,S112 - one dead query must not kill the rest
            continue
    merged.sort(key=lambda x: x[0], reverse=True)
    out, seen = [], set()
    for _stamp, title, when in merged:
        if not _news_keep(title):
            continue
        key = title[:40].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((_news_tag(title), title, when))
    _NEWS_CACHE["at"], _NEWS_CACHE["items"] = now, out
    return out[:limit]


def news_bias(items: list[tuple[str, str, str]]) -> tuple[str, str]:
    """A crude tally: do the headlines lean against gold, with it, or neither?

    Hawkish Fed / a strong dollar is bearish for gold; easing / a weak dollar is
    bullish. This is a HINT from the tags, not a signal -- it never sizes or
    opens a trade, it only tells the reader which way the wind blows.
    """
    hawk = sum(1 for t, _x, _w in items if t == "🦅")
    dove = sum(1 for t, _x, _w in items if t == "🕊️")
    dxy = sum(1 for t, _x, _w in items if t == "📈")
    if hawk + dxy > dove:
        return "ضد الذهب (قوة دولار/تشديد)", "🔻"
    if dove > hawk + dxy:
        return "مع الذهب (تيسير/ضعف دولار)", "🔺"
    return "متوازن — لا ميل واضح", "➖"


# ---------------------------------------------------------------- report
def snapshot() -> dict:
    """ONE read of the tape, everything the report needs.

    The report and the scheduled plan push must read the SAME moment: if each
    called the feed on its own they could disagree about the price, and the plan
    the owner gets by push would contradict the plan he gets by button. One read,
    one truth -- the same rule the desk follows between its card and its map.
    """
    q = quote()
    live = q.get("mid") or q.get("bid") or 0.0
    raw = {tf: bars(tf, 400) for tf in FRAMES}
    fv = {tf: frame_view(tf, raw[tf], live) for tf in FRAMES}
    b1h = closed(raw["1h"], "1h")
    a1h = fv["1h"].get("atr") or 10.0
    day_hi, day_lo = q.get("high"), q.get("low")

    st = structure(b1h)
    tol = max(3.0, a1h * 0.6)
    extra = round_levels(live, 10.0, 120.0) + \
        [x for x in (day_hi, day_lo) if x]
    res, sup = sr_levels(b1h, tol, live, top=5, extra=extra)
    ups, dns = nearest_levels(live, res, sup, a1h)
    dem, sup_z = order_blocks(b1h, 3, a1h * 0.8)
    bull_g, bear_g = fair_value_gaps(b1h, a1h)
    liq = liquidity(b1h, tol)
    up = sum(1 for f in fv.values() if f.get("trend") == 1)
    dn = sum(1 for f in fv.values() if f.get("trend") == -1)
    bias = "short" if dn > up else ("long" if up > dn else None)
    return {"q": q, "live": live, "raw": raw, "fv": fv, "b1h": b1h, "a1h": a1h,
            "day_hi": day_hi, "day_lo": day_lo, "st": st, "tol": tol,
            "res": res, "sup": sup, "ups": ups, "dns": dns, "dem": dem,
            "sup_z": sup_z, "bull_g": bull_g, "bear_g": bear_g, "liq": liq,
            "cnd": candle_read(b1h), "up": up, "dn": dn,
            "neu": len(FRAMES) - up - dn, "bias": bias}


def plan_section(snap: dict | None = None) -> str:
    """Section 8 alone -- the plan and the strategy -- for the scheduled push.

    The owner asked for the plan to reach him on its own every hour or two,
    without opening a button. This returns EXACTLY the block the full report
    prints as section 8 (same helpers, same numbers), so the pushed plan and the
    button's plan can never disagree. It does NOT re-read the tape if a snapshot
    is handed in.
    """
    s = snap or snapshot()
    if not s.get("live"):
        return "⚠️ ما في تسعيرة هلّق — جرّب بعد شوي."
    live = s["live"]
    L: list[str] = []
    L.append("🏛️ <b>AYZR — خطتي والاستراتيجية</b>")
    L.append(f"<b>{SYMBOL}</b> · السعر <b>{num(live)}</b> · "
             f"🕐 {datetime.now(UTC).strftime('%H:%M')} UTC")
    L.append("")
    L += _plan_block(live, s["bias"], s["ups"], s["dns"], s["dem"], s["sup_z"],
                     s["liq"], s["a1h"], s["st"])
    L.append("")
    L.append("<b>🔎 السيناريوهات</b>")
    L += _scenarios(live, s["ups"], s["dns"], s["liq"], s["a1h"])
    L.append("")
    L.append("<b>🎯 التوصية</b>")
    L.append(_recommend(live, s["bias"], s["st"], s["fv"]["1h"]))
    L.append("")
    L.append("<i>📌 قراءة AYZR المستقلة — اجتهاد فني، مو وعد ربح. الوقف إلزامي.</i>")
    return "\n".join(L)


def report() -> str:
    s = snapshot()
    q, live = s["q"], s["live"]
    if not live:
        return "⚠️ ما في تسعيرة هلّق — جرّب بعد شوي."
    dd = q.get("dayDiffPercent")
    day_hi, day_lo = s["day_hi"], s["day_lo"]
    clock = q.get("time", "")[11:16]
    now = datetime.now(UTC)
    fv, st, tol = s["fv"], s["st"], s["tol"]
    res, sup, liq, cnd = s["res"], s["sup"], s["liq"], s["cnd"]
    dem, sup_z = s["dem"], s["sup_z"]
    bull_g, bear_g = s["bull_g"], s["bear_g"]
    ups, dns, a1h = s["ups"], s["dns"], s["a1h"]
    up, dn, neu, bias = s["up"], s["dn"], s["neu"], s["bias"]

    L: list[str] = []
    L.append("🏛️ <b>AYZR — قراءة مستقلة للسوق</b>")
    L.append(f"<b>{SYMBOL}</b> · السعر <b>{num(live)}</b>")
    head = f"bid {num(q.get('bid'))} · ask {num(q.get('ask'))} · " \
           f"سبريد {num(q.get('spread'))}"
    L.append(head)
    if dd is not None:
        L.append(f"اليوم {'🔴' if dd < 0 else '🟢'} {dd:+.2f}% · "
                 f"نطاق اليوم {num(day_lo)}–{num(day_hi)}")
    L.append(f"🕐 {clock} UTC · {now.strftime('%Y-%m-%d')}")
    L.append("")

    # 1) candles
    L.append("<b>1️⃣ قراءة الشمعات (1س)</b>")
    L += [f"• {x}" for x in cnd]
    L.append("")

    # 2) structure
    L.append("<b>2️⃣ الهيكل (Structure)</b>")
    labs = st["labels"]
    if labs:
        seq = " · ".join(f"{lab} {num(p)}" for lab, p in labs)
        L.append(f"• تسلسل القمم/القيعان: {seq}")
    tname = {1: "صاعد", -1: "هابط", 0: "عرضي"}[st["trend"]]
    L.append(f"• الترند الهيكلي (1س): <b>{tname}</b>")
    if st["event"]:
        ev, lvl = st["event"]
        L.append(f"• آخر حدث: <b>{ev}</b> عند {num(lvl)}")
    L.append("")

    # 3) support / resistance
    L.append("<b>3️⃣ المقاومات والدعوم (مناطق الكلاستر)</b>")
    if res:
        L.append("🔴 مقاومات: " + " · ".join(
            f"{num(x['price'])}" +
            (f" [{num(x['lo'])}–{num(x['hi'])}]" if x['hi'] - x['lo'] > tol
             else "") +
            f" ({x['touches']} لمسة، بعد {x['dist']:.0f})" for x in res))
    if sup:
        L.append("🟢 دعوم: " + " · ".join(
            f"{num(x['price'])}" +
            (f" [{num(x['lo'])}–{num(x['hi'])}]" if x['hi'] - x['lo'] > tol
             else "") +
            f" ({x['touches']} لمسة، بعد {x['dist']:.0f})" for x in sup))
    L.append("")

    # 4) supply / demand
    L.append("<b>4️⃣ مناطق العرض والطلب (Order Blocks + فجوات)</b>")
    if dem:
        L.append("🟩 طلب: " + " · ".join(
            f"{num(z['bot'])}–{num(z['top'])}" for z in dem[-3:]))
    if sup_z:
        L.append("🟥 عرض: " + " · ".join(
            f"{num(z['bot'])}–{num(z['top'])}" for z in sup_z[-3:]))
    if bull_g:
        L.append("🟩 فجوة صاعدة (FVG): " + " · ".join(
            f"{num(z['bot'])}–{num(z['top'])}" for z in bull_g[-3:]))
    if bear_g:
        L.append("🟥 فجوة هابطة (FVG): " + " · ".join(
            f"{num(z['bot'])}–{num(z['top'])}" for z in bear_g[-3:]))
    L.append("")

    # 5) liquidity hunt
    L.append("<b>5️⃣ الصيد والقنص (السيولة)</b>")
    if liq["buy_side"]:
        L.append(f"• سيولة شرائية فوق قمة <b>{num(liq['buy_side'])}</b> "
                 "(ستوبات الشورتات)")
    if liq["sell_side"]:
        L.append(f"• سيولة بيعية تحت قاع <b>{num(liq['sell_side'])}</b> "
                 "(ستوبات اللونغات)")
    for a, b, _j in liq["eq_highs"]:
        L.append(f"• قمم متساوية ~{num((a + b) / 2)} = مغناطيس صيد")
    for a, b, _j in liq["eq_lows"]:
        L.append(f"• قيعان متساوية ~{num((a + b) / 2)} = مغناطيس صيد")
    for name, lvl in liq["sweeps"]:
        L.append(f"• 🎯 <b>{name}</b> عند {num(lvl)} — كسر وهمي ورجع")
    L.append("")

    # 6) indicators
    L.append("<b>6️⃣ المؤشرات (حسابي الخاص)</b>")
    L.append(f"• إجماع الفريمات: <b>{up}ص · {dn}هـ · {neu}ع</b>")
    for tf in ("1d", "4h", "1h", "15m"):
        f = fv[tf]
        if not f.get("ok"):
            continue
        bits = []
        if f.get("rsi") is not None:
            bits.append(f"RSI {f['rsi']:.0f}")
        if f.get("adx") is not None:
            bits.append(f"ADX {f['adx']:.0f}")
        if f.get("ema20") and f.get("ema50"):
            side = ("فوق EMA20/50" if live > f["ema20"] > f["ema50"]
                    else "تحت EMA20/50" if live < f["ema20"] < f["ema50"]
                    else "بين المتوسطات")
            bits.append(side)
        if f.get("atr"):
            bits.append(f"ATR {f['atr']:.1f}")
        tile = {1: "🟢", -1: "🔴", 0: "⚪"}[f["trend"]]
        L.append(f"• {f['label']} {tile} " + " · ".join(bits))
    f1 = fv["1h"]
    if f1.get("macd"):
        m, s, h = f1["macd"]
        L.append(f"• MACD(1س): {m:.2f} · إشارة {s:.2f} · "
                 f"هيستوغرام {h:+.2f} {'📈' if h > 0 else '📉'}")
    if f1.get("stoch"):
        k, d = f1["stoch"]
        L.append(f"• Stochastic(1س): %K {k:.0f} · %D {d:.0f}")
    L.append("")

    # 7) news
    L.append("<b>7️⃣ الأخبار (طبقة خاصة فيي)</b>")
    news = news_headlines(8)
    if news:
        nb, nb_icon = news_bias(news)
        L.append(f"• الميل الإخباري: {nb_icon} <b>{nb}</b>")
        for tag, title, when in news[:6]:
            L.append(f"• {tag} {title}" + (f" <i>({when})</i>" if when else ""))
    else:
        L.append("• ما في عناوين هلّق (الفييد ساكت أو محجوب).")
    L.append("")

    # 8) the plan
    L.append("<b>8️⃣ خطتي والاستراتيجية</b>")
    plan_txt = _plan_block(live, bias, ups, dns, dem, sup_z, liq, a1h, st)
    L += plan_txt
    L.append("")

    # 9) scenarios
    L.append("<b>9️⃣ السيناريوهات</b>")
    L += _scenarios(live, ups, dns, liq, a1h)
    L.append("")

    # 10) recommendation
    L.append("<b>🔟 التوصية</b>")
    L.append(_recommend(live, bias, st, fv["1h"]))
    L.append("")
    L.append("<i>📌 قراءة AYZR المستقلة — اجتهاد فني من شمعات حيّة، "
             "مو توصية ولا وعد ربح. الوقف إلزامي.</i>")
    return "\n".join(L)


def _plan_block(live, bias, ups, dns, dem, sup_z, liq, a1h, st) -> list[str]:
    """My plan: pick a side, place the stop beyond the level that would prove me
    wrong, and take targets off the rungs in front of me.

    `ups` and `dns` are the guaranteed rungs (clustered levels, else ATR
    projections), so a plan always has a stop and at least one target that sit
    on the CORRECT side of the entry.
    """
    out = []
    if bias is None:
        out.append("⏸️ الهيكل عرضي — ما في اتجاه أتداوله. بنستنى كسر واضح.")
        return out
    d = 1 if bias == "long" else -1
    word = "شراء 🟢" if d == 1 else "بيع 🔴"
    out.append(f"• الاتجاه المفضّل: <b>{word}</b>")

    if d == -1:
        stop_level = ups[0] if ups else live + a1h * 1.2
        targets = dns[:3]
    else:
        stop_level = dns[0] if dns else live - a1h * 1.2
        targets = ups[:3]

    p = build_plan(live, bias, stop_level, targets)
    out.append(f"• الدخول: <b>{num(p['entry'])}</b> عند السعر")
    out.append(f"• الوقف: <b>{num(p['stop'])}</b> "
               f"(−{p['pts']:.1f} نقطة = −${p['risk_usd']:.0f})")
    tg = p["targets"] or [p["target"]]
    out.append("• الأهداف: " + " · ".join(
        f"{i+1}) {num(t)}" for i, t in enumerate(tg)))
    if not p["targets"]:
        out.append("  (لا مستوى قريب بعيد كفاية — الهدف محسوب من مخاطرة×2)")
    out.append(f"• R:R ≈ <b>{p['rr']:.2f}</b> · لوت {LOT} · "
               f"حساب ${ACCOUNT_USD:.0f}")
    # where to re-enter on a pullback (a level on our side of the tape)
    if d == -1 and ups:
        out.append(f"• دخول أنظف: انتظر ارتداد نحو <b>{num(ups[0])}</b> "
                   "ورفضه، بدل مطاردة السعر.")
    elif d == 1 and dns:
        out.append(f"• دخول أنظف: انتظر ارتداد نحو <b>{num(dns[0])}</b> "
                   "ورفضه، بدل مطاردة السعر.")
    if st["trend"] == -d and st["trend"] != 0:
        out.append("⚠️ الخطة <b>عكس الترند الهيكلي</b> — مخاطرة أعلى، "
                   "صغّر الحجم أو تجاهل.")
    return out


def _scenarios(live, ups, dns, liq, a1h) -> list[str]:
    """Three conditional paths, each with a trigger and a stop.

    `ups`/`dns` are the guaranteed rungs, so the levels here always bracket the
    live price. A scenario is a PLAN, not a prediction: it says what to do IF a
    level breaks or holds, and the stop that voids it.
    """
    out = []
    r1 = ups[0] if ups else live + a1h * 2
    s1 = dns[0] if dns else live - a1h * 2
    r2 = ups[1] if len(ups) > 1 else r1 + a1h
    s2 = dns[1] if len(dns) > 1 else s1 - a1h

    out.append("<b>🔴 سيناريو هبوط (مع الترند)</b>")
    trig = liq["sell_side"] if (liq.get("sell_side")
                                and liq["sell_side"] < live) else s1
    out.append(f"  المشغّل: إغلاق 1س تحت <b>{num(trig)}</b>")
    out.append(f"  الهدف: {num(s2)} → {num(s2 - a1h)} "
               f"· الوقف فوق {num(trig + a1h * 0.8)}")

    out.append("<b>🟢 سيناريو صعود (ارتداد)</b>")
    out.append(f"  المشغّل: استرداد <b>{num(s1)}</b> وإغلاق فوق <b>{num(r1)}</b>")
    out.append(f"  الهدف: {num(r2)} → {num(r2 + a1h)} "
               f"· الوقف تحت {num(s1 - a1h * 0.5)}")

    out.append("<b>⚪ سيناريو نطاق</b>")
    out.append(f"  بين {num(s1)} و {num(r1)} — ما في شي، بنستنى الكسر.")
    return out


def _recommend(live, bias, st, f1) -> str:
    r = f1.get("rsi")
    ad = f1.get("adx")
    if bias is None:
        return "⏸️ <b>لا صفقة</b> — الهيكل عرضي وما في اتجاه واضح."
    if r is not None and ((bias == "short" and r < 38)
                          or (bias == "long" and r > 62)):
        return (f"⏸️ <b>لا ألحق السعر</b> — RSI 1س {r:.0f} يعني الحركة "
                "ركضت. بستنى ارتداد لمستوى وأدخل من هناك.")
    if ad is not None and ad < 18:
        return (f"⏸️ <b>ترند ضعيف</b> (ADX {ad:.0f}) — بنستنى قوة قبل "
                "الدخول.")
    word = "بيع 🔴" if bias == "short" else "شراء 🟢"
    return f"✅ <b>الاتجاه {word}</b> — ادخل من مستوى، بوقف وهدف واضحين."


def main() -> None:
    try:
        print(report())
    except Exception as exc:  # noqa: BLE001
        print("ERR", exc)


if __name__ == "__main__":
    main()
