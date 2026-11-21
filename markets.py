"""markets — an INDEPENDENT multi-asset reader (silver + bitcoin).

This file shares NOTHING with bot.py's gold engine and does not import it. It
reads the same public candle/quote FEED (data is data) and computes everything
itself: its own indicators, its own swing structure, its own support/resistance
bands, its own supply/demand zones, its own liquidity pools, its own scenarios,
its own snipe setups and its own sizing.

ONE method, TWO assets. The owner asked for two buttons; what he needs is two
honest reads. The analysis is identical in shape because the market's geometry
is identical in shape -- but every number, level, zone and verdict is computed
from THAT asset's own tape. Nothing is copied across, and nothing here is gold.

The per-asset contract spec is the part that decides money, so it is explicit
and overridable by environment, and the report always states the assumed
contract rather than hiding it:

    XAGUSD  1 lot = 5,000 oz  -> 0.01 lot = 50 oz   -> $1 move = $50
    BTCUSD  1 lot = 1 coin    -> 0.01 lot = 0.01 BTC -> $1 move = $0.01

Run it:  python3 markets.py silver
         python3 markets.py bitcoin
"""
from __future__ import annotations

import json
import os
import urllib.request
from itertools import pairwise

BIQUOTE = "https://biquote.io"
UA = "Mozilla/5.0 (AyzrDesk/1.0)"

FRAMES = ("1d", "4h", "1h", "30m", "15m", "5m")
TF_AR = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
         "15m": "15د", "5m": "5د"}

ACCOUNT_USD = float(os.environ.get("DESK_ACCOUNT", "200"))
RISK_MIN, RISK_MAX = 8.0, 12.0
RISK_TARGET = 10.0
RR_TARGET = 2.0
MAX_CARD = 3900          # Telegram's hard limit is 4096; the send truncates

ASSETS: dict[str, dict] = {
    "silver": {
        "symbol": os.environ.get("DESK_SILVER_SYMBOL", "XAGUSD"),
        "name": "الفضة",
        "tag": "🥈",
        "contract": float(os.environ.get("DESK_SILVER_CONTRACT", "5000")),
        "lot": float(os.environ.get("DESK_SILVER_LOT", "0.01")),
        "digits": 3,
        "round_step": 1.0,
        "atr_floor": 0.05,
    },
    "bitcoin": {
        "symbol": os.environ.get("DESK_BTC_SYMBOL", "BTCUSD"),
        "name": "البتكوين",
        "tag": "🟠",
        "contract": float(os.environ.get("DESK_BTC_CONTRACT", "1")),
        "lot": float(os.environ.get("DESK_BTC_LOT", "0.01")),
        "digits": 2,
        "round_step": 1000.0,
        "atr_floor": 20.0,
    },
}


# ------------------------------------------------------------------ helpers
def num(v, spec: dict | None = None) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{spec['digits'] if spec else 2}f}"
    except (TypeError, ValueError):
        return "—"


def _get(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def quote(sym: str) -> dict:
    q = _get(f"{BIQUOTE}/api/{sym}")
    if not q.get("mid"):
        bid, ask = q.get("bid"), q.get("ask")
        if bid and ask:
            q["mid"] = (float(bid) + float(ask)) / 2
        else:
            q["mid"] = q.get("last")
    return q


def bars(sym: str, tf: str, limit: int = 300) -> list[dict]:
    b = _get(f"{BIQUOTE}/api/{sym}/ohlc?interval={tf}&limit={limit}")
    return sorted(b.get("bars") or [], key=lambda x: x["openTime"])


def closed(b: list[dict]) -> list[dict]:
    """Drop the bar still forming -- its close repaints as it fills."""
    return [x for x in b if not x.get("isOpen")]


# --------------------------------------------------------------- indicators
def ema(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for x in vals[n:]:
        e = x * k + e * (1 - k)
    return e


def rsi(vals: list[float], n: int = 14) -> float | None:
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for a, b in pairwise(vals):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr(b: list[dict], n: int = 14) -> float | None:
    if len(b) < n + 1:
        return None
    trs = []
    for i in range(1, len(b)):
        h, lo, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(b: list[dict], n: int = 14) -> float | None:
    """Trend strength 0-100, no direction. Below ~20 the tape is chop."""
    if len(b) < 2 * n + 1:
        return None
    plus, minus, trs = [], [], []
    for i in range(1, len(b)):
        up = b[i]["high"] - b[i - 1]["high"]
        dn = b[i - 1]["low"] - b[i]["low"]
        plus.append(up if up > dn and up > 0 else 0.0)
        minus.append(dn if dn > up and dn > 0 else 0.0)
        h, lo, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))

    def wilder(seq):
        v = sum(seq[:n])
        out = [v]
        for x in seq[n:]:
            v = v - v / n + x
            out.append(v)
        return out

    sp, sm, st = wilder(plus), wilder(minus), wilder(trs)
    dxs = []
    for p, m, t in zip(sp, sm, st):
        if t <= 0:
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        s = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / s if s else 0.0)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for x in dxs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def frame_trend(b: list[dict]) -> int:
    """+1 price above both EMAs, -1 below both, 0 in between."""
    c = [x["close"] for x in b]
    if not c:
        return 0
    e20, e50 = ema(c, 20), ema(c, 50)
    if e20 is None or e50 is None:
        return 0
    if c[-1] > e20 and c[-1] > e50:
        return 1
    if c[-1] < e20 and c[-1] < e50:
        return -1
    return 0


# ------------------------------------------------------------------ swings
def swings(b: list[dict], left: int = 2, right: int = 2
           ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs, lows = [], []
    for i in range(left, len(b) - right):
        h, lo = b[i]["high"], b[i]["low"]
        if all(h >= b[j]["high"] for j in range(i - left, i + right + 1) if j != i):
            highs.append((i, h))
        if all(lo <= b[j]["low"] for j in range(i - left, i + right + 1) if j != i):
            lows.append((i, lo))
    return highs, lows


def structure(b: list[dict]) -> dict:
    """Swing sequence -> HH/HL/LH/LL, the trend it implies, and the last break."""
    highs, lows = swings(b, 2, 2)
    seq = sorted([(i, p, "H") for i, p in highs] + [(i, p, "L") for i, p in lows])
    labs, last_h, last_l = [], None, None
    for _i, p, kind in seq:
        if kind == "H":
            if last_h is not None:
                labs.append(("HH" if p > last_h else "LH", p))
            last_h = p
        else:
            if last_l is not None:
                labs.append(("HL" if p > last_l else "LL", p))
            last_l = p
    ups = sum(1 for x, _ in labs[-4:] if x in ("HH", "HL"))
    dns = sum(1 for x, _ in labs[-4:] if x in ("LH", "LL"))
    trend = 1 if ups > dns else (-1 if dns > ups else 0)
    event = None
    if len(highs) >= 2 and len(lows) >= 2 and b:
        ph, pl, c = highs[-1][1], lows[-1][1], b[-1]["close"]
        if c > ph:
            event = ("BOS↑" if trend == 1 else "CHoCH↑", ph)
        elif c < pl:
            event = ("BOS↓" if trend == -1 else "CHoCH↓", pl)
    return {"labels": labs[-5:], "trend": trend, "event": event,
            "highs": highs[-5:], "lows": lows[-5:]}


# ------------------------------------------------------------------ levels
def sr_bands(b: list[dict], tol: float, ref: float, top: int = 6,
             extra: list[float] | None = None
             ) -> tuple[list[dict], list[dict]]:
    """Cluster swing pivots into support/resistance bands, split at `ref`.

    A band keeps its real low/high so a wide one reads as a ZONE, and its
    strength is the pivot count -- a price seen three times is a level, a price
    seen once is a dot. `extra` candidates join a band when they fall inside it
    but never merge with each other, which would chain many single touches into
    one fake band.
    """
    highs, lows = swings(b, 2, 2)
    pts = sorted([p for _i, p in highs] + [p for _i, p in lows])
    clusters: list[list[float]] = []
    for p in pts:
        if clusters and p - clusters[-1][-1] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    for e in (extra or []):
        hit = None
        for c in clusters:
            if abs(sum(c) / len(c) - e) <= tol:
                hit = c
                break
        if hit is not None:
            hit.append(e)
        else:
            clusters.append([e])
    res: list[dict] = []
    sup: list[dict] = []
    for c in clusters:
        price = sum(c) / len(c)
        band = {"price": price, "lo": min(c), "hi": max(c),
                "touches": len(c), "dist": abs(ref - price)}
        # `>=` not `>`: a band sitting exactly ON the price is resistance the
        # price is already at (+0.000), never support "below" it. The old `>`
        # filed it under support with a POSITIVE distance -- a level printed
        # above the price it was supposed to support.
        (res if price >= ref else sup).append(band)
    res.sort(key=lambda x: x["price"])
    sup.sort(key=lambda x: -x["price"])
    return res[:top], sup[:top]


def zones(b: list[dict], tol: float, ref: float, top: int = 3
          ) -> tuple[list[dict], list[dict]]:
    """Supply above / demand below: pivot clusters with 2+ touches, widest first.

    Same clustering as the levels but reported as an AREA (lo..hi) with its
    touch count, because that is how the owner trades them: he sells into a
    supply band and buys out of a demand band, not at a single tick.
    """
    highs, lows = swings(b, 2, 2)
    pts = sorted([p for _i, p in highs] + [p for _i, p in lows])
    clusters: list[list[float]] = []
    for p in pts:
        if clusters and p - clusters[-1][-1] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    supply: list[dict] = []
    demand: list[dict] = []
    for c in clusters:
        if len(c) < 2:
            continue
        price = sum(c) / len(c)
        band = {"price": price, "lo": min(c), "hi": max(c), "touches": len(c)}
        (supply if price > ref else demand).append(band)
    supply.sort(key=lambda x: (x["price"], -x["touches"]))
    demand.sort(key=lambda x: (-x["price"], -x["touches"]))
    return supply[:top], demand[:top]


def liquidity(b: list[dict], tol: float) -> tuple[list[float], list[float]]:
    """Equal highs / equal lows -- where resting stops cluster."""
    highs, lows = swings(b, 2, 2)

    def clusters(seq):
        out, cur = [], []
        for x in sorted(seq):
            if cur and x - cur[-1] <= tol:
                cur.append(x)
            else:
                if len(cur) >= 2:
                    out.append(sum(cur) / len(cur))
                cur = [x]
        if len(cur) >= 2:
            out.append(sum(cur) / len(cur))
        return out

    return sorted(clusters([p for _i, p in highs])), \
        sorted(clusters([p for _i, p in lows]), reverse=True)


# ------------------------------------------------------------------ sizing
def build_trade(entry: float, direction: str, stop_level: float | None,
                spec: dict, atr_v: float | None) -> dict:
    """Size one trade for THIS asset from the desk's rules.

    Risk lands in the $8-12 window when the contract allows it. When it cannot
    -- silver's 0.01 lot is 50 oz, so one cent of stop already costs $0.50 --
    the function returns the HONEST minimum it could do plus `fits=False`,
    rather than quoting a stop inside the noise and calling it a plan.
    """
    per_point = spec["contract"] * spec["lot"]
    d = 1 if direction == "long" else -1
    pts = RISK_TARGET / per_point
    if stop_level is not None:
        room = (entry - stop_level) * d
        if room > 0:
            pts = min(room, RISK_MAX / per_point)
    pts = max(RISK_MIN / per_point, min(pts, RISK_MAX / per_point))
    noise = atr_v or spec["atr_floor"]
    fits = pts >= noise * 0.75
    stop = entry - d * pts
    risk_usd = pts * per_point
    gain_usd = risk_usd * RR_TARGET
    target = entry + d * (gain_usd / per_point)
    return {"direction": direction, "entry": entry, "stop": stop,
            "target": target, "rr": RR_TARGET, "risk_usd": risk_usd,
            "risk_pts": pts, "gain_usd": gain_usd, "fits": fits,
            "noise_ratio": pts / noise if noise else 0.0, "per_point": per_point}


# ------------------------------------------------------------------ snapshot
def snapshot(key: str) -> dict:
    """One read of one asset: quote, every frame's trend/ATR/RSI, the structure
    on the 1h, and the level/zone/liquidity maps built from that same tape."""
    spec = ASSETS[key]
    sym = spec["symbol"]
    q = quote(sym)
    live = float(q.get("mid") or 0)
    trends, atrs, rsis = {}, {}, {}
    for tf in FRAMES:
        try:
            b = bars(sym, tf, 300)
            trends[tf] = frame_trend(b)
            atrs[tf] = atr(b)
            rsis[tf] = rsi([x["close"] for x in b])
        except Exception:  # noqa: BLE001 - one missing frame must not kill the read
            trends[tf] = 0
            atrs[tf] = None
            rsis[tf] = None
    try:
        b1 = closed(bars(sym, "1h", 300))
        b15 = closed(bars(sym, "15m", 300))
    except Exception:  # noqa: BLE001
        b1, b15 = [], []
    a1h = atr(b1) if b1 else None
    tol = max(spec["atr_floor"], (a1h or 0) * 0.3)
    day_hi = q.get("high") or (max(x["high"] for x in b1) if b1 else live)
    day_lo = q.get("low") or (min(x["low"] for x in b1) if b1 else live)
    extra = [float(day_hi), float(day_lo)]
    step = spec["round_step"]
    extra += [round(live / step) * step, round(live / step) * step + step]
    res, sup = sr_bands(b1, tol, live, 6, extra)
    supply, demand = zones(b1, tol, live, 3)
    liq_up, liq_dn = liquidity(b1, tol)
    st = structure(b1) if b1 else {"labels": [], "trend": 0, "event": None}
    return {"key": key, "spec": spec, "q": q, "live": live, "sym": sym,
            "trends": trends, "atrs": atrs, "rsis": rsis, "a1h": a1h,
            "adx": adx(b15) if b15 else None,
            "res": res, "sup": sup, "supply": supply, "demand": demand,
            "liq_up": liq_up, "liq_dn": liq_dn, "st": st,
            "day_hi": float(day_hi), "day_lo": float(day_lo), "tol": tol}


def bias_of(trends: dict) -> tuple[int, str, str]:
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    if dn >= 5:
        return -1, "هابط", "🔴"
    if up >= 5:
        return 1, "صاعد", "🟢"
    if dn > up:
        return -1, "مائل للهبوط", "🟡"
    if up > dn:
        return 1, "مائل للصعود", "🟡"
    return 0, "عرضي", "⚪"


# ------------------------------------------------------------------ the read
def _fmt_level(s: dict, b: dict, side: str) -> str:
    """One level line: the band's real area when it is wide, plus its strength."""
    sp = s["spec"]
    width = b["hi"] - b["lo"]
    if width > (s["tol"] * 0.5):
        area = f"{num(b['lo'], sp)}–{num(b['hi'], sp)}"
    else:
        area = num(b["price"], sp)
    dist = b["price"] - s["live"]
    arrow = "🔺" if side == "res" else "🔻"
    return (f"  {arrow} <b>{area}</b> · {dist:+.{sp['digits']}f} · "
            f"{b['touches']} لمسة")


def _structure_line(s: dict) -> str:
    st = s["st"]
    labs = st.get("labels") or []
    if not labs:
        return "البنية (1س): ما في قمم/قيعان كافية للحكم ⚪"
    seq = " → ".join(x for x, _ in labs[-4:])
    ev = st.get("event")
    if ev:
        kind, price = ev
        return (f"البنية (1س): {seq} · آخر حدث <b>{kind}</b> "
                f"عند {num(price, s['spec'])}")
    return f"البنية (1س): {seq}"


def _scenario(s: dict, direction: str) -> list[str]:
    """One conditional trade, priced from THIS asset's own level and ATR."""
    sp = s["spec"]
    live, a1h = s["live"], s["a1h"]
    if direction == "long":
        if not s["res"]:
            return []
        trig = s["res"][0]["price"] + (s["tol"] * 0.1)
        stop_level = s["sup"][0]["price"] if s["sup"] else None
        head, word = "🟢 سيناريو صعود", "اختراق"
    else:
        if not s["sup"]:
            return []
        trig = s["sup"][0]["price"] - (s["tol"] * 0.1)
        stop_level = s["res"][0]["price"] if s["res"] else None
        head, word = "🔴 سيناريو هبوط", "كسر"
    dist = abs(trig - live)
    reach = dist / a1h if a1h else None
    if reach is not None and reach > 3.0:
        return [f"<b>{head}</b>",
                (f"  — <b>{num(trig, sp)}</b> بعيد {dist:.{sp['digits']}f} "
                 f"({reach:.1f}× تقلّب الساعة) — مستوى للمراقبة، مش صفقة اليوم."),
                ""]
    t = build_trade(trig, direction, stop_level, sp, a1h)
    out = [f"<b>{head}</b>",
           (f"  المشغّل: {word} <b>{num(trig, sp)}</b> "
            f"({dist:.{sp['digits']}f} من السعر)"),
           f"  الوقف: {num(t['stop'], sp)} · الهدف: {num(t['target'], sp)}",
           (f"  R:R ≈ {t['rr']:.2f} · مخاطرة ${t['risk_usd']:.2f}"
            f" · مكسب ${t['gain_usd']:.2f}")]
    if not t["fits"]:
        out.append(f"  ⚠️ الوقف {t['risk_pts']:.{sp['digits']}f} = "
                   f"{t['noise_ratio']:.2f}× تقلّب الساعة — داخل الضجيج، "
                   "هاد مستوى للمراقبة مش صفقة.")
    out.append("")
    return out


def _snipe(s: dict) -> list[str]:
    """Where the stops are, and what a sweep would look like.

    A snipe is not a prediction: it names the pools where resting orders sit
    (equal highs above, equal lows below) and the reaction to wait for. The
    value is knowing where the market is likely to reach for BEFORE it does.
    """
    sp = s["spec"]
    live = s["live"]
    ups = [x for x in s["liq_up"] if x > live][:2]
    dns = [x for x in s["liq_dn"] if x < live][:2]
    out = ["<b>🎯 القنص والسيولة (وين الوقوف)</b>"]
    if ups:
        for x in ups:
            out.append(f"  ⬆️ سيولة بيع-ستوب فوق <b>{num(x, sp)}</b> "
                       f"(+{x - live:.{sp['digits']}f}) — كنسها = رفض وبيع")
    if dns:
        for x in dns:
            out.append(f"  ⬇️ سيولة شراء-ستوب تحت <b>{num(x, sp)}</b> "
                       f"(−{live - x:.{sp['digits']}f}) — كنسها = ارتداد وشراء")
    if not ups and not dns:
        out.append("  ما في قمم/قيعان متساوية واضحة — ما في بركة سيولة محددة.")
    out.append("  <i>القنص: استنى الكنس (اختراق سريع ورجوع)، وادخل بالاتجاه "
               "المعاكس للكنس — مش معه.</i>")
    out.append("")
    return out


def _verdict(s: dict) -> list[str]:
    """When to buy, when to sell, and WHY -- the honest core of the card."""
    sp = s["spec"]
    live = s["live"]
    bias, word, mark = bias_of(s["trends"])
    r1h, r4h, r1d = s["rsis"].get("1h"), s["rsis"].get("4h"), s["rsis"].get("1d")
    out = ["<b>🧭 متى أشتري ومتى أبيع — وليش</b>"]

    # --- the case for a BUY
    buy: list[str] = []
    if s["demand"]:
        dz = s["demand"][0]
        buy.append(f"عند منطقة الطلب <b>{num(dz['lo'], sp)}–{num(dz['hi'], sp)}</b>"
                   f" ({dz['touches']} لمسة) — منطقة اشترى منها السوق قبل")
    if s["sup"]:
        buy.append(f"فوق الدعم <b>{num(s['sup'][0]['price'], sp)}</b> "
                   f"({s['sup'][0]['touches']} لمسة)")
    if isinstance(r1d, (int, float)) and r1d < 30:
        buy.append(f"RSI اليومي {r1d:.1f} — تشبّع بيعي (النزول صار صعب)")
    if isinstance(r4h, (int, float)) and r4h < 30:
        buy.append(f"RSI 4س {r4h:.1f} — تشبّع بيعي")
    # --- the case for a SELL
    sell: list[str] = []
    if s["supply"]:
        sz = s["supply"][0]
        sell.append(f"عند منطقة العرض <b>{num(sz['lo'], sp)}–{num(sz['hi'], sp)}</b>"
                    f" ({sz['touches']} لمسة) — منطقة باع منها السوق قبل")
    if s["res"]:
        sell.append(f"تحت المقاومة <b>{num(s['res'][0]['price'], sp)}</b> "
                    f"({s['res'][0]['touches']} لمسة)")
    if isinstance(r1h, (int, float)) and r1h > 70:
        sell.append(f"RSI الساعة {r1h:.1f} — تشبّع شرائي")
    if isinstance(r4h, (int, float)) and r4h > 70:
        sell.append(f"RSI 4س {r4h:.1f} — تشبّع شرائي")

    if bias == -1:
        out.append(f"<b>الميل: {mark} {word}</b> — الوزن مع البيع، والشراء "
                   "مقابل التيار (يحتاج تأكيد عند دعم فقط).")
    elif bias == 1:
        out.append(f"<b>الميل: {mark} {word}</b> — الوزن مع الشراء، والبيع "
                   "مقابل التيار (يحتاج تأكيد عند مقاومة فقط).")
    else:
        out.append(f"<b>الميل: {mark} {word}</b> — بلا اتجاه واضح؛ "
                   "اللعب من الأطراف فقط (دعم/مقاومة)، لا من النص.")

    out.append("<b>🟢 متى أشتري:</b>")
    out += [f"  · {x}" for x in buy] or ["  · ما في سبب شراء واضح هلّق"]
    out.append("<b>🔴 متى أبيع:</b>")
    out += [f"  · {x}" for x in sell] or ["  · ما في سبب بيع واضح هلّق"]

    # --- the range, when there is no trend to ride
    if s["res"] and s["sup"]:
        lo = s["sup"][0]["price"]
        hi = s["res"][0]["price"]
        width = hi - lo
        pos = (live - lo) / width * 100 if width else 50
        out.append(f"<b>⚪ النطاق الآن:</b> {num(lo, sp)} ↔ {num(hi, sp)} "
                   f"— السعر عند <b>{pos:.0f}%</b> من النطاق")
        if pos > 70:
            out.append("  ⚠️ قريب من سقف النطاق — الشراء هون بيشتري القمة.")
        elif pos < 30:
            out.append("  ⚠️ قريب من أرضية النطاق — البيع هون بيبيع القاع.")
        else:
            out.append("  ⚠️ في نص النطاق — ما في R:R بأي اتجاه. استنى طرف.")
    out.append("")
    return out


def _sizing_line(s: dict) -> list[str]:
    """The honest contract math -- the part that decides whether this is even
    tradeable on the owner's account."""
    sp = s["spec"]
    per_point = sp["contract"] * sp["lot"]
    a1h = s["a1h"]
    out = ["<b>💵 الحساب والعقد (الجزء اللي بيقرر)</b>",
           (f"  العقد المفترض: 1 لوت = {sp['contract']:,.0f} · "
            f"اللوت الأدنى {sp['lot']} → ${per_point:,.2f} لكل نقطة")]
    if not a1h:
        # No ATR means no measurable noise, and a stop you cannot size against
        # the noise is not a verdict -- it is a guess. Say so rather than print
        # "tradeable" off a number nobody measured.
        out.append("  ⚠️ ما في بيانات تقلّب كافية (ATR) — "
                   "ما بقدر أحكم إذا الصفقة بتتحمّل حسابك.")
        out.append("")
        return out
    need = a1h * 1.5
    cost = need * per_point
    pct = cost / ACCOUNT_USD * 100
    out.append(f"  وقف سليم (1.5× تقلّب الساعة) = {need:.{sp['digits']}f} "
               f"→ <b>${cost:,.2f}</b> = {pct:.1f}% من ${ACCOUNT_USD:,.0f}")
    if pct > 10:
        out.append(f"  🔴 فوق حدّك (${RISK_MAX:.0f}) — "
                   "الوقف السليم أكبر من اللي بيحملو حسابك.")
        small = RISK_MAX / per_point
        out.append(f"  لو صغّرت الوقف لـ${RISK_MAX:.0f} = "
                   f"{small:.{sp['digits']}f} نقطة = "
                   f"{small / a1h:.2f}× تقلّب الساعة — داخل الضجيج ⚠️")
    elif pct >= 4:
        out.append("  🟡 مقبول بس حدّي — صفقة وحدة بس، ووقف صارم.")
    else:
        out.append("  🟢 داخل حدّك — الصفقة قابلة للتنفيذ.")
    out.append("")
    return out


def report(key: str) -> str:
    """The full read for one asset: tape, structure, levels, zones, liquidity,
    snipe, scenarios, verdict and sizing. Arabic, because the owner reads it."""
    if key not in ASSETS:
        return f"⚠️ أصل غير معروف: {key}"
    try:
        s = snapshot(key)
    except Exception as exc:  # noqa: BLE001 - a dead feed must not raise upward
        return f"⚠️ تعذّر قراءة {ASSETS[key]['name']} هلّق ({exc.__class__.__name__})."
    sp, q, live = s["spec"], s["q"], s["live"]
    if not live:
        return f"⚠️ ما في تسعيرة {sp['name']} هلّق — جرّب بعد شوي."
    dd = q.get("dayDiffPercent")
    _bias, word, mark = bias_of(s["trends"])
    L: list[str] = []
    L.append(f"{sp['tag']} <b>تحليل {sp['name']} — قراءة مستقلة</b>")
    L.append(f"<b>{s['sym']}</b> · السعر <b>{num(live, sp)}</b>")
    if dd is not None:
        L.append(f"اليوم {'🔴' if dd < 0 else '🟢'} {dd:+.2f}% · "
                 f"نطاق اليوم {num(s['day_lo'], sp)}–{num(s['day_hi'], sp)}")
    L.append(f"الميل: {mark} <b>{word}</b>")

    # --- the tape
    L.append("")
    L.append("<b>📊 الفريمات</b>")
    parts = []
    for tf in FRAMES:
        t = s["trends"].get(tf, 0)
        r = s["rsis"].get(tf)
        mk = "🟢" if t == 1 else ("🔴" if t == -1 else "⚪")
        rs = f"RSI {r:.0f}" if isinstance(r, (int, float)) else "RSI —"
        parts.append(f"  {TF_AR[tf]}: {mk} {rs}")
    L += parts
    a1h = s["a1h"]
    if a1h:
        L.append(f"  تقلّب الساعة (ATR) ≈ {num(a1h, sp)}")
    if s["adx"] is not None:
        adx_v = s["adx"]
        tag = "بلا ترند" if adx_v < 20 else ("ترند" if adx_v < 40 else "ممتد")
        L.append(f"  قوة الاتجاه (ADX 15د) ≈ {adx_v:.0f} — {tag}")

    # --- structure
    L.append("")
    L.append("<b>🏗️ البنية</b>")
    L.append(f"  {_structure_line(s)}")

    # --- levels
    L.append("")
    L.append("<b>📐 المقاومات والدعوم</b>")
    if s["res"]:
        L.append("  <b>مقاومات (فوق):</b>")
        L += [_fmt_level(s, b, "res") for b in s["res"][:4]]
    else:
        L.append("  مقاومات: ما في مستوى مؤكد فوق السعر")
    if s["sup"]:
        L.append("  <b>دعوم (تحت):</b>")
        L += [_fmt_level(s, b, "sup") for b in s["sup"][:4]]
    else:
        L.append("  دعوم: ما في مستوى مؤكد تحت السعر")

    # --- zones
    L.append("")
    L.append("<b>🧱 مناطق العرض والطلب</b>")
    if s["supply"]:
        for z in s["supply"][:2]:
            L.append(f"  🔺 عرض: <b>{num(z['lo'], sp)}–{num(z['hi'], sp)}</b> "
                     f"({z['touches']} لمسة)")
    else:
        L.append("  عرض: ما في منطقة عرض مؤكدة")
    if s["demand"]:
        for z in s["demand"][:2]:
            L.append(f"  🔻 طلب: <b>{num(z['lo'], sp)}–{num(z['hi'], sp)}</b> "
                     f"({z['touches']} لمسة)")
    else:
        L.append("  طلب: ما في منطقة طلب مؤكدة")

    # --- snipe + scenarios
    L.append("")
    L += _snipe(s)
    L.append("<b>🎬 السيناريوهات</b>")
    scen = _scenario(s, "long") + _scenario(s, "short")
    L += scen or ["  ما في سيناريو مشروط واضح هلّق.", ""]

    # --- verdict + sizing
    L += _verdict(s)
    L += _sizing_line(s)
    L.append("<i>قراءة مستقلة على بيانات هذا الأصل وحده. "
             "تحليل فني — مو نصيحة استثمارية.</i>")

    text = "\n".join(L)
    if len(text) > MAX_CARD:
        text = text[:MAX_CARD - 40].rsplit("\n", 1)[0] + "\n<i>…(اقتطاع)</i>"
    return text


def silver() -> str:
    return report("silver")


def bitcoin() -> str:
    return report("bitcoin")


def main() -> None:
    import sys
    key = (sys.argv[1] if len(sys.argv) > 1 else "silver").lower()
    key = {"فضة": "silver", "silver": "silver", "xagusd": "silver",
           "بتكوين": "bitcoin", "bitcoin": "bitcoin", "btc": "bitcoin",
           "btcusd": "bitcoin"}.get(key, key)
    print(report(key))


if __name__ == "__main__":
    main()
