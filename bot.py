#!/usr/bin/env python3
"""MarketDesk bot — @Ayzr1324_bot.

One self-contained Telegram bot for the owner and the people he adds: a market
reading, a recommendation, the scenarios and levels, plus a personal trade
journal (daily / weekly / monthly) and the open book. Pure stdlib — no pandas,
no requests — so it runs anywhere Python 3 does.

Data comes from biquote (keyless). Nothing here promises a profit: the reading
is analytical, and every result is reported in R, never dressed up as a call.

Run:  python3 bot.py          (long-polls Telegram and answers buttons)
      python3 bot.py --menu   (publishes the menu once, then exits)
      python3 bot.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape as _unescape
from pathlib import Path

HOME = Path(__file__).resolve().parent
DATA = HOME / "data"
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "journal.db"
ALLOW_PATH = DATA / "allow.json"
OFFSET_PATH = DATA / "offset.json"
TOKEN_PATH = HOME / ".tg_token"

API = "https://api.telegram.org"
BIQUOTE = "https://biquote.io"
UA = "Mozilla/5.0 (compatible; MarketDeskBot/1.0)"
SYMBOL = os.environ.get("DESK_SYMBOL", "XAUUSD")

# On a free host the filesystem is ephemeral, so the journal would die with the
# container. Telegram is the free, permanent store already in reach: the journal
# is uploaded as a document and pinned, and restored from that pin at startup.
# DESK_BACKUP_CHAT overrides the chat; by default it is the first allowed chat.
BACKUP_FILE = "marketdesk-backup.json"
BACKUP_CHAT = os.environ.get("DESK_BACKUP_CHAT", "")
PIN_PATH = DATA / "pin.json"

# The two people the bot serves from the start. Anyone else who sends /start is
# added to allow.json -- the bot is private, but adding a friend must not need a
# redeploy.
SEED_CHATS = ["933571066", "8545528489"]

# ---- the desk's plan, sized to the owner's account ----------------------
# The owner trades gold with $200 and 0.01 lots. On XAUUSD 1 lot is 100 oz, so
# 0.01 lot moves $1 per $1 of gold -- a stop of 9 points is $9 of risk. Every
# number below is in DOLLARS, which is what the account actually feels; the
# point form (points = dollars / POINT_VALUE) is only for the stop line.
ACCOUNT_USD = float(os.environ.get("DESK_ACCOUNT", "200"))
LOT = 0.01
POINT_VALUE = LOT * 100.0        # $ per point at this lot
STOP_MIN_USD = 8.0               # the owner's window: lose $8-12 at worst
STOP_MAX_USD = 12.0
TARGET_MIN_USD = 15.0            # ...and take $15-20
TARGET_MAX_USD = 20.0
STOP_PICK_USD = 9.0              # the validated sweet spot; a near level may move it
TARGET_RR = 2.2                  # 9 * 2.2 = $19.8, top of the window
MIN_RR = 1.5                     # below this a plan is not worth stating
CONFIRM_FRAMES = 4               # a direction needs this many of 6 frames
STRONG_FRAMES = 5                # ...and this many to allow a SECOND trade
MAX_OPEN = 1                     # one trade at a time (raise to 2 to stack)
# The entry must agree with the 15m candle, not just the frame tilt: a long
# taken while price sits UNDER the 15m EMA20 is buying into a falling tape.
# On the owner's own journal this filter allowed every winner (#1-6) and
# blocked every loser (#7-9); the 30m version let a loser through, so the
# filter is pinned to 15m. Set to False to disable and take raw frame tilt.
TREND_FILTER_TF = "15m"          # the frame whose EMA20 the entry must respect
# The quality gate (the "combo" filters), read on the 15m frame. A raw frame
# tilt takes far too many trades (measured on 2026: 2702 trades, 33.9% win,
# -55R drawdown); these four gates cut that to the good ones and lift the win
# rate above 40%. Each gate FAILS OPEN when its reading is missing, so a
# broken feed degrades to the old behaviour instead of silencing the desk.
FILTERS_ON = os.environ.get("DESK_FILTERS", "1").strip().lower() not in \
    ("0", "off", "no", "false")
ADX_MIN, ADX_MAX = 20.0, 40.0    # ride a trend, but not an extended one
ATR_MIN_PCT, ATR_MAX_PCT = 0.0008, 0.0025   # 0.08%-0.25% of price: alive, calm
RSI_LONG = (35.0, 58.0)          # a long is a pullback, not a chase
RSI_SHORT = (42.0, 65.0)         # the short mirror
# Which gates are live. Default "adx,atr": the two that helped on BOTH the
# futures study and the bot's own 6-frame replay. MACD and RSI sit in the
# validated futures combo but measured redundant-to-harmful here -- the 6-frame
# agreement is already a trend filter, so a second momentum gate only cuts the
# trades that pay (2026: full four-gate combo +32R vs adx+atr alone +71R, and
# RSI alone dragged it to +13R). Add them with DESK_FILTER_GATES=adx,atr,macd,rsi.
FILTER_GATES = {g.strip().lower() for g in os.environ.get(
    "DESK_FILTER_GATES", "adx,atr").split(",") if g.strip()}
DEFAULT_GATES = {"adx", "atr"}
# The support watch: the desk's own nearest support (from _levels) gets a
# dedicated heads-up -- approaching, bounced, or broken -- so the owner hears
# about the level he cares about, not only when a whole plan appears.
SUPPORT_NEAR_PTS = 6.0           # "approaching" band around the level, points
SUPPORT_BREAK_PTS = 3.0          # a 15m close this far under = broken

# The key levels the desk announces a crossing of, beyond its single latched
# support. Taken from pivot clusters across 15m..1d (refresh when the market
# moves well away from them). A crossing of ANY of these speaks once. 4138 and
# 4108 are the owner's two triggers (breakout / breakdown); the owner can extend
# the set at deploy time with DESK_WATCH_LEVELS=4138,4108,4200,...
WATCH_LEVELS: tuple[float, ...] = (
    4055.0, 4070.0, 4084.0, 4096.0, 4108.0,
    4119.0, 4132.0, 4138.0, 4147.0, 4168.0, 4192.0)
_extra = os.environ.get("DESK_WATCH_LEVELS", "").strip()
if _extra:
    WATCH_LEVELS = tuple(sorted(set(WATCH_LEVELS) | {
        float(x) for x in _extra.split(",") if x.strip()}))
LEVEL_BAND = 1.0                 # hysteresis: a crossing needs this much clearance
# The green-light watch: the desk's own entry filter is "a long needs price
# above the 15m EMA20". So rather than hard-code the number 4103 (which the
# EMA moves away from within the hour), watch the EMA itself -- the owner hears
# when the desk's OWN gate opens, not when an arbitrary price is touched.
GATE_ARM_PTS = 1.5               # close this far above/below the EMA = a signal
# In-trade monitoring: an open trade must not be silent between entry and exit.
# These fire ONE message each, per trade, when the price gets close to a
# boundary -- so the owner is warned before the stop, and told before the
# target, without a running commentary on every tick.
NEAR_TARGET_FRAC = 0.25          # within this fraction of the entry->target span
NEAR_STOP_FRAC = 0.30            # within this fraction of the entry->stop span


# ---- menus --------------------------------------------------------------
# The news layer is INFORMATION ONLY -- see card_news(). The owner edits this
# list by hand; nothing here can open, block or size a trade. When
# DESK_NEWS_LIVE=1 the card also pulls fresh headlines from Google News (keyless
# RSS) -- still information only, still never a trade input.
NEWS_LIVE = os.environ.get("DESK_NEWS_LIVE", "0").strip().lower() in (
    "1", "true", "yes", "on")
NEWS_QUERY = os.environ.get("DESK_NEWS_QUERY", "gold price XAUUSD").strip()
# The owner's clock: hours to add to UTC when a time is shown (Syria = 3, and
# Syria has had no DST since 2022, so a fixed offset is exact). Set
# DESK_TZ_OFFSET=0 to show GMT.
TZ_OFFSET_H = int(os.environ.get("DESK_TZ_OFFSET", "3") or "3")
TZ_LABEL = os.environ.get("DESK_TZ_LABEL", "بتوقيت سوريا")
NEWS_ITEMS: list[tuple[str, str]] = [
    ("🔥", "اليوم: تقرير التضخم الأميركي (CPI) — أهم حدث بالأسبوع"),
    ("⚠️", "الـCPI رح يطلع بلا نسبة سنوية (تداعيات الإغلاق) — رقم مشوّه"),
    ("🦅", "وظائف قوية → احتمال رفع فايدة بكسمبر >65% (سلبي للذهب)"),
    ("📉", "تضخم متوقع يبطئ → ممكن يدعم اختراق 4,200 (إيجابي)"),
]

MENU = [
    [("🧭 شو تعمل هلّق", "action")],
    [("📊 تحليل الآن", "analysis"), ("🎯 التوصية", "recommend")],
    [("🗺️ خريطة المناطق", "map"), ("📰 الأخبار", "news")],
    [("🎬 السيناريوهات", "scenarios"), ("🔔 المستويات", "levels")],
    [("📂 الصفقات المفتوحة", "open"), ("📒 السجل", "journal_menu")],
    [("👥 المشتركين", "who"), ("➕ سجّل صفقة", "howto")],
    [("🔔 دليل التنبيهات", "alerts"), ("❓ مساعدة", "help")],
]

JOURNAL_MENU = [
    [("📅 السجل اليومي", "j_day"), ("🗓️ السجل الأسبوعي", "j_week")],
    [("📆 السجل الشهري", "j_month"), ("📋 كل الصفقات", "journal")],
    [("🔭 متابعة المفتوحة", "monitor"), ("🏠 القائمة", "menu")],
]

TEXT_ACTIONS = {
    "/start": "menu", "/menu": "menu", "/help": "help",
    "تحليل": "analysis", "توصية": "recommend", "سيناريو": "scenarios",
    "سيناريوهات": "scenarios", "مستويات": "levels", "مفتوحة": "open",
    "الصفقات": "open", "سجل": "journal_menu", "السجل": "journal_menu",
    "يومي": "j_day", "اليوم": "j_day", "أسبوعي": "j_week", "اسبوعي": "j_week",
    "شهري": "j_month", "الشهر": "j_month", "متابعة": "monitor",
    "اعمل": "action", "تنبيهات": "alerts", "التنبيهات": "alerts", "دليل": "alerts",
    "مساعدة": "help", "قائمة": "menu",
    "مين": "who", "المشتركين": "who", "المشتركين؟": "who",
}
TRADE_VERBS = {"سجل", "سجّل", "صفقة", "/trade", "trade"}
CLOSE_VERBS = {"سكر", "سكّر", "أغلق", "اغلق", "اقفل", "أقفل", "/close", "close"}


# ========================================================================
# small helpers
# ========================================================================
def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def num(v, dp: int = 2) -> str:
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def _get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def token_identity(t: str) -> str:
    """The @username this token reaches, or "" if it reaches no bot.

    This is the difference between a token and a *string that looks like* one:
    a stale TELEGRAM_BOT_TOKEN in the environment is a 35-character value, so a
    truthiness test calls it "ok" while every send 404s. Only a getMe round-trip
    can tell them apart.
    """
    if not t:
        return ""
    try:
        r = _get(f"{API}/bot{t}/getMe", timeout=10)
        return (r.get("result") or {}).get("username", "") or ""
    except Exception:  # noqa: BLE001 - unreachable/HTTP error both mean "no"
        return ""


def token() -> str:
    """The bot token, VALIDATED before use.

    A stale TELEGRAM_BOT_TOKEN once pointed at a different, dead bot and
    silently shadowed the right one: every read looked fine and every send
    404'd. So each candidate is checked with getMe and the first one that
    actually reaches a bot wins, whichever store holds it. The local `.tg_token`
    file is the operator's explicit choice and is tried first.

    If nothing verifies (offline, or every candidate is dead) this falls back to
    the first non-empty candidate rather than returning nothing, so a network
    blip at boot cannot leave the bot tokenless -- `--selftest` reports that
    case as DEAD rather than "ok".
    """
    cands: list[str] = []
    if TOKEN_PATH.exists():
        try:
            cands.append(TOKEN_PATH.read_text().strip())
        except OSError:
            pass
    cands += [os.environ.get("TG_TOKEN", ""),
              os.environ.get("TELEGRAM_BOT_TOKEN", "")]
    cands = [c for c in cands if c]
    for t in cands:
        if token_identity(t):
            return t
    return cands[0] if cands else ""


# ========================================================================
# Telegram client
# ========================================================================
class Telegram:
    def __init__(self, tok: str):
        self.tok = tok

    def call(self, method: str, payload: dict, timeout: int = 25) -> dict:
        url = f"{API}/bot{self.tok}/{method}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def send(self, chat, text, markup=None) -> bool:
        text = text[:4000]
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            return bool(self.call("sendMessage", payload).get("ok"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            return False

    def answer(self, cid: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery",
                      {"callback_query_id": cid, "text": text[:190]})
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            pass

    def updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        payload: dict = {"timeout": timeout,
                         "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            body = self.call("getUpdates", payload, timeout=timeout + 20)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            return []
        return body.get("result") or []

    def send_document(self, chat, name: str, data: bytes, caption: str = "") -> dict | None:
        """Upload a document. multipart/form-data by hand -- pure stdlib."""
        boundary = "----desk" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(k: str, v: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"\r\n\r\n{v}\r\n'.encode())

        field("chat_id", str(chat))
        if caption:
            field("caption", caption)
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="document"; filename="{name}"\r\n'
                     "Content-Type: application/octet-stream\r\n\r\n".encode())
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"{API}/bot{self.tok}/sendDocument", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode()).get("result")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            return None

    def file_bytes(self, file_id: str) -> bytes | None:
        try:
            info = self.call("getFile", {"file_id": file_id}, timeout=30).get("result") or {}
            path = info.get("file_path")
            if not path:
                return None
            url = f"{API}/file/bot{self.tok}/{path}"
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            return None

    def pin(self, chat, message_id: int) -> None:
        try:
            self.call("pinChatMessage",
                      {"chat_id": str(chat), "message_id": message_id,
                       "disable_notification": True})
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            pass


def kb(rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                for row in rows]}


def nav(action: str) -> dict:
    rows: list[list[dict]] = []
    if action not in ("menu", "help"):
        rows.append([{"text": "🔄 تحديث", "callback_data": action}])
    if action.startswith("j_") or action in ("monitor", "journal", "open"):
        rows.append([{"text": "📒 السجل", "callback_data": "journal_menu"}])
    rows.append([{"text": "🏠 القائمة", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def keyboard_for(action: str) -> dict:
    if action == "journal_menu":
        return kb(JOURNAL_MENU)
    if action in ("menu", "help"):
        return kb(MENU)
    if action == "open":
        ck = close_keyboard()
        if ck:
            ck["inline_keyboard"] += nav("open")["inline_keyboard"]
            return ck
    return nav(action)


# ========================================================================
# biquote — quote and candles
# ========================================================================
def quote() -> dict:
    return _get(f"{BIQUOTE}/api/{SYMBOL}")


def bars(tf: str, limit: int = 300) -> list[dict]:
    b = _get(f"{BIQUOTE}/api/{SYMBOL}/ohlc?interval={tf}&limit={limit}")
    out = b.get("bars") or []
    return sorted(out, key=lambda x: x["openTime"])


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}


def closed(b: list[dict], tf: str) -> list[dict]:
    """Drop the bar still forming: its close repaints as the bar fills.

    A filter that reads the live bar is reading the future -- the backtest that
    used it measured +58R, but on the last CLOSED bar the same rule is +25R. The
    decision surfaces read closed bars only; the raw frame tilt (a direction,
    not a level) is left as it was.
    """
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


# ========================================================================
# indicators (pure stdlib)
# ========================================================================
def ema(vals: list[float], n: int):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def atr(b: list[dict], n: int = 14):
    if len(b) < n + 1:
        return None
    trs = []
    for i in range(1, len(b)):
        h, low, pc = b[i]["high"], b[i]["low"], b[i - 1]["close"]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def rsi(vals: list[float], n: int = 14):
    """Wilder's RSI. >50 momentum up, <50 down; >70 stretched, <30 washed."""
    if len(vals) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += d if d > 0 else 0.0
        losses += -d if d < 0 else 0.0
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + (d if d > 0 else 0.0)) / n
        al = (al * (n - 1) + (-d if d < 0 else 0.0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def ema_series(vals: list[float], n: int) -> list[float | None]:
    """EMA over the whole series, aligned to `vals` (None before it warms up)."""
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


def macd_hist_series(b: list[dict]) -> list[float | None]:
    """MACD histogram (12/26/9): EMA12 - EMA26, minus its own EMA9 signal."""
    cl = [x["close"] for x in b]
    ef, es = ema_series(cl, 12), ema_series(cl, 26)
    line: list[float | None] = []
    for i in range(len(cl)):
        a, c = ef[i], es[i]
        line.append(None if (a is None or c is None) else a - c)
    sig = ema_series([x if x is not None else 0.0 for x in line], 9)
    hist: list[float | None] = []
    for i in range(len(cl)):
        a, c = line[i], sig[i]
        hist.append(None if (a is None or c is None) else a - c)
    return hist


def adx_series(b: list[dict], n: int = 14) -> list[float | None]:
    """Wilder's ADX: trend STRENGTH 0-100, no direction.

    A gate on this is what keeps the desk out of chop: below ~20 the tape has
    no trend to ride, above ~40 the move is already extended and a pullback
    entry is buying the top.
    """
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

    def wilder(vals: list[float]) -> list[float | None]:
        out: list[float | None] = [None] * len(vals)
        s = sum(vals[1:n + 1])
        out[n] = s
        for i in range(n + 1, len(vals)):
            s = s - s / n + vals[i]
            out[i] = s
        return out

    atr_w, pdm, mdm = wilder(trs), wilder(plus), wilder(minus)
    dx: list[float | None] = [None] * len(b)
    for i in range(len(b)):
        a, p, m = atr_w[i], pdm[i], mdm[i]
        if a and p is not None and m is not None:
            pdi = 100 * p / a if a else 0.0
            mdi = 100 * m / a if a else 0.0
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0
    out: list[float | None] = [None] * len(b)
    if dx[n] is not None:
        out[n] = dx[n]
        for i in range(n + 1, len(b)):
            prev, cur = out[i - 1], dx[i]
            if cur is None or prev is None:
                continue
            out[i] = (prev * (n - 1) + cur) / n
    return out


def pivots(b: list[dict], left: int = 3, right: int = 3):
    highs, lows = [], []
    for i in range(left, len(b) - right):
        h, low = b[i]["high"], b[i]["low"]
        if all(h >= b[i - j]["high"] for j in range(1, left + 1)) and \
           all(h >= b[i + j]["high"] for j in range(1, right + 1)):
            highs.append(h)
        if all(low <= b[i - j]["low"] for j in range(1, left + 1)) and \
           all(low <= b[i + j]["low"] for j in range(1, right + 1)):
            lows.append(low)
    return highs, lows


def _touches(level: float, b: list[dict], tol: float) -> int:
    """How many bars came within `tol` of `level` -- a level's strength.

    A pivot price is a single bar's extreme; what makes it a LEVEL the owner
    cares about is how often price returned to it. Counting touches on the
    frame the level came from is the honest measure of that.
    """
    n = 0
    for x in b:
        if x["low"] - tol <= level <= x["high"] + tol:
            n += 1
    return n


def frame_trend(b: list[dict]) -> int:
    closes = [x["close"] for x in b]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if e20 is None or e50 is None:
        return 0
    if e20 > e50 * 1.0005:
        return 1
    if e20 < e50 * 0.9995:
        return -1
    return 0


FRAMES = ("1d", "4h", "1h", "30m", "15m", "5m")
# The reading may lean on the big frames; a TRADE still needs CONFIRM_FRAMES of
# all six. Big frames weigh more because a daily/4h tilt outlives a 5m wiggle.
BIG_FRAMES = ("1d", "4h", "1h")
SMALL_FRAMES = ("30m", "15m", "5m")


def read_market() -> dict:
    """One read of the tape: quote plus every frame's trend, ATR and RSI."""
    q = quote()
    live = float(q.get("mid") or q.get("last") or 0)
    trends, atrs, rsis = {}, {}, {}
    for tf in FRAMES:
        try:
            b = bars(tf, 300)
            trends[tf] = frame_trend(b)
            atrs[tf] = atr(b)
            rsis[tf] = rsi([x["close"] for x in b])
        except Exception:  # noqa: BLE001 - one missing frame must not kill the read
            trends[tf] = 0
            atrs[tf] = None
            rsis[tf] = None
    return {"q": q, "live": live, "trends": trends, "atrs": atrs, "rsis": rsis}


def market_open(q: dict) -> bool:
    return (str(q.get("marketState")) == "open") and not q.get("stale")


# ========================================================================
# the reading cards
# ========================================================================
def _bias(trends: dict) -> tuple[str, str]:
    """The reading's lean when no trade is confirmed.

    A trade still needs CONFIRM_FRAMES of all six frames -- that bar does not
    move. But "the desk has no opinion" is unhelpful when the tape is merely
    mixed, so the reading states a lean, and it takes that lean from the BIG
    frames: a daily/4h/1h tilt outlives a 5m wiggle. Two of the three big
    frames agreeing is a lean; all three is a direction. Only when the big
    frames are split do the small ones get a vote, and a split with no small
    agreement is simply range.

    This is a reading, not an order -- it never invents a plan.
    """
    big_up = sum(1 for tf in BIG_FRAMES if trends.get(tf) == 1)
    big_dn = sum(1 for tf in BIG_FRAMES if trends.get(tf) == -1)
    if big_up == len(BIG_FRAMES):
        return "صاعد", "🟢"
    if big_dn == len(BIG_FRAMES):
        return "هابط", "🔴"
    if big_up == 2:
        return "مائل للصعود", "🟡"
    if big_dn == 2:
        return "مائل للهبوط", "🟡"
    small_up = sum(1 for tf in SMALL_FRAMES if trends.get(tf) == 1)
    small_dn = sum(1 for tf in SMALL_FRAMES if trends.get(tf) == -1)
    if small_up >= 2 and small_up > small_dn:
        return "مائل للصعود", "🟡"
    if small_dn >= 2 and small_dn > small_up:
        return "مائل للهبوط", "🟡"
    return "عرضي", "⚪"


def _structure_read() -> str | None:
    """Higher-highs/higher-lows on 1h, or None when the tape is unreadable."""
    try:
        bh = bars("1h", 200)
        highs, lows = pivots(bh, 3, 3)
    except Exception:  # noqa: BLE001 - a missing frame must not kill the card
        return None
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    if hh and hl:
        return "البنية (1س): قمم وقيعان صاعدة 🟢 (بنية صاعدة)"
    if not hh and not hl:
        return "البنية (1س): قمم وقيعان هابطة 🔴 (بنية هابطة)"
    return "البنية (1س): مختلطة ⚪ (بلا بنية واضحة)"


def _levels_with_strength(live: float) -> list[str]:
    """Nearest resistances/supports, each tagged with how often price touched it.

    A pivot price is one bar's extreme; the touch count on the 1h frame is what
    turns it into a level worth watching. Tolerance is a quarter of the 1h ATR,
    so a quiet market does not count noise and a wild one still groups a cluster.
    """
    try:
        b1 = bars("1h", 300)
        tol = max(1.0, (atr(b1) or 1.0) * 0.25)
        res, sup = _levels(live)
    except Exception:  # noqa: BLE001 - a missing frame must not kill the card
        return []
    out = []
    if res:
        out.append("مقاومات: " + " · ".join(
            f"{num(x)} ({_touches(x, b1, tol)} لمسة)" for x in res[:3]))
    if sup:
        out.append("دعوم: " + " · ".join(
            f"{num(x)} ({_touches(x, b1, tol)} لمسة)" for x in sup[:3]))
    return out


def _verdict(m: dict) -> dict:
    """The desk's read of this tape, decided ONCE.

    state is one of closed / hold / enter / wait. The 'شو تعمل' card and the
    analysis card's top line both format from THIS dict, so the desk can never
    tell the owner two different things in the same minute. It calls plan(),
    the same engine the auto-desk trades on -- there is no second opinion here.
    """
    if not market_open(m["q"]):
        return {"state": "closed"}
    op = open_trades()
    if op:
        return {"state": "hold", "trade": op[0], "live": m["live"]}
    p = plan(m)
    if p is not None:
        return {"state": "enter", "plan": p}
    trends = m["trends"]
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    lean, mark = _bias(trends)
    try:
        b15 = closed(bars(TREND_FILTER_TF, 300), TREND_FILTER_TF)
        e = ema([x["close"] for x in b15], 20)
    except Exception:  # noqa: BLE001 - the filter is a guard, not a blocker
        b15, e = [], None
    # Why no plan? The frames may agree yet a quality gate (or the EMA20) may
    # hold the trade back -- name the blocker so the desk never looks broken.
    blocked = None
    direction = "long" if up >= CONFIRM_FRAMES else \
        ("short" if dn >= CONFIRM_FRAMES else None)
    if direction and FILTERS_ON:
        fs = filter_status(direction, b15)
        if not fs["ok"]:
            blocked = _filter_line(fs)
        elif e is not None and not frame_agrees(direction, m["live"], b15):
            blocked = f"فلتر EMA20 ({TREND_FILTER_TF})"
    return {"state": "wait", "up": up, "dn": dn, "ema": e,
            "lean": lean, "mark": mark, "blocked": blocked,
            "direction": direction}


def _headline(v: dict) -> str:
    """One line: what to do right now, from a verdict dict."""
    st = v["state"]
    if st == "closed":
        return "⛔ السوق مسكّر — ما في شي تعمله"
    if st == "hold":
        t = v["trade"]
        d = "شراء 🟢" if t["direction"] == "long" else "بيع 🔴"
        return f"📂 عندك صفقة مفتوحة #{t['id']} ({d}) — خلّيها تشتغل"
    if st == "enter":
        p = v["plan"]
        d = "شراء" if p["direction"] == "long" else "بيع"
        return (f"🟢 ادخل {d} — دخول {num(p['entry'])} · "
                f"وقف {num(p['stop'])} · هدف {num(p['target'])}")
    if v.get("blocked"):
        way = "صاعد" if v.get("direction") == "long" else "هابط"
        return f"⏸️ استنى — الاتجاه {way} بس الفلتر مانع ({v['blocked']})"
    if v["up"] >= CONFIRM_FRAMES:
        return "⏸️ استنى — الاتجاه صاعد بس السعر تحت الفلتر"
    if v["dn"] >= CONFIRM_FRAMES:
        return "⏸️ استنى — الاتجاه هابط بس السعر فوق الفلتر"
    return f"⏸️ استنى — السوق محتار ({v['up']}ص · {v['dn']}هـ)"


def _verdict_detail(v: dict) -> str:
    """The full sentence behind the headline, for the analysis card's verdict."""
    st = v["state"]
    if st == "closed":
        return "⛔ السوق مسكّر — ما في قراءة حيّة."
    if st == "hold":
        return (f"📂 صفقة مفتوحة #{v['trade']['id']} — المكتب يديرها، "
                "ما يفتح جديدة الآن.")
    if st == "enter":
        p = v["plan"]
        d = "شراء 🟢" if p["direction"] == "long" else "بيع 🔴"
        return (f"✅ صفقة جاهزة — {d} · دخول {num(p['entry'])} · "
                f"وقف {num(p['stop'])} · هدف {num(p['target'])} · "
                f"R:R {p['rr']:.2f}")
    if v.get("blocked"):
        way = "صاعد" if v.get("direction") == "long" else "هابط"
        return (f"⏸️ الاتجاه <b>{way}</b> بس <b>فلتر الجودة مانع</b>: "
                f"{v['blocked']}. المكتب ما ياخد إلا الصفقات النظيفة.")
    if v["up"] >= CONFIRM_FRAMES:
        return (f"⏸️ الاتجاه <b>صاعد</b> ({v['up']}/6) بس الفلتر مو موافق — "
                "استنى التأكيد.")
    if v["dn"] >= CONFIRM_FRAMES:
        return (f"⏸️ الاتجاه <b>هابط</b> ({v['dn']}/6) بس الفلتر مو موافق — "
                "استنى التأكيد.")
    return (f"⏸️ ما في صفقة — الفريمات ما اتفقت ({v['up']}ص · {v['dn']}هـ). "
            f"الميل: {v['mark']} <b>{v['lean']}</b>")


def card_analysis() -> str:
    """A live, detailed read of the tape -- the deep analysis card.

    Everything here is computed at the instant of the tap from the same engine
    the desk trades on: per-frame trend/RSI/ATR, the EMA20 gate, structure
    (higher-highs/lower-lows), the nearest levels with their touch counts, the
    day range, spread and volatility, and finally the desk's own verdict. It
    never invents a trade -- the verdict section says what the desk would do.
    """
    m = read_market()
    q, live, trends, atrs, rsis = (m["q"], m["live"], m["trends"],
                                   m["atrs"], m["rsis"])
    state = "مفتوح ✅" if market_open(q) else "مسكّر ⛔"
    age = q.get("quoteAgeSeconds")
    age_s = f"{int(age)} ثانية" if isinstance(age, (int, float)) else "—"
    names = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
             "15m": "15د", "5m": "5د"}
    icon = {1: "🟢", -1: "🔴", 0: "⚪"}

    lines = [f"📊 <b>تحليل {SYMBOL}</b> — لحظة الطلب"]
    v = _verdict(m)
    lines.append(f"<b>الخلاصة:</b> {_headline(v)}")
    lines.append("")
    lines.append(f"السعر <b>{num(live)}</b> · السوق {state} · "
                 f"عمر التسعيرة {age_s}")
    if q.get("bid") and q.get("ask"):
        lines.append(f"bid {num(q['bid'])} · ask {num(q['ask'])} · "
                     f"سبريد {num(q.get('spread'))}")
    if q.get("dayDiffPercent") is not None:
        lines.append(f"تغيّر اليوم {q['dayDiffPercent']}%")

    # ---- per frame: trend, RSI, ATR -------------------------------------
    lines.append("")
    lines.append("<b>الفريمات (ميل · RSI · تقلب):</b>")
    for tf in FRAMES:
        r = rsis.get(tf)
        r_txt = f"RSI {r:.0f}" if isinstance(r, (int, float)) else "RSI —"
        a = atrs.get(tf)
        a_txt = f"ATR {a:.1f}" if isinstance(a, (int, float)) else "ATR —"
        lines.append(f"  {names[tf]}: {icon[trends.get(tf,0)]} {r_txt} · {a_txt}")

    # ---- momentum read ---------------------------------------------------
    r15 = rsis.get("15m")
    if isinstance(r15, (int, float)):
        if r15 >= 70:
            mom = "متشبّع شراء — احتمال تصحيح"
        elif r15 >= 55:
            mom = "زخم صاعد"
        elif r15 <= 30:
            mom = "متشبّع بيع — احتمال ارتداد"
        elif r15 <= 45:
            mom = "زخم هابط"
        else:
            mom = "زخم محايد"
        lines.append(f"\nزخم 15د: <b>{mom}</b> (RSI {r15:.0f})")

    # ---- the desk's own gate --------------------------------------------
    try:
        b15f = closed(bars(TREND_FILTER_TF, 300), TREND_FILTER_TF)
        ef = ema([x["close"] for x in b15f], 20)
    except Exception:  # noqa: BLE001
        b15f, ef = [], None
    if ef is not None:
        pos = "فوق ✅" if live > ef else "تحت ⬇️"
        lines.append(f"فلتر EMA20 ({names[TREND_FILTER_TF]}): السعر {pos} "
                     f"({num(ef)})")

    # ---- the quality gate (the combo filters) ---------------------------
    if FILTERS_ON:
        r = filter_status("long", b15f)["readings"]

        def _fmt(v, dp, suf=""):
            return f"{v:.{dp}f}{suf}" if isinstance(v, (int, float)) else "—"

        adx_v, atr_p, rsi_v, macd_v = (r["adx"], r["atr_pct"],
                                       r["rsi"], r["macd_hist"])
        adx_ok = "✅" if (adx_v is None or ADX_MIN <= adx_v <= ADX_MAX) else "❌"
        atr_ok = "✅" if (atr_p is None
                         or ATR_MIN_PCT <= atr_p <= ATR_MAX_PCT) else "❌"
        macd_ico = "📈" if (macd_v or 0) > 0 else "📉"
        lines.append(f"فلتر الجودة: ADX {_fmt(adx_v, 0)} {adx_ok} · "
                     f"ATR {_fmt(atr_p * 100 if atr_p else None, 2, '%')} "
                     f"{atr_ok}")
        lines.append(f"  RSI {_fmt(rsi_v, 0)} · MACD hist "
                     f"{_fmt(macd_v, 2)} {macd_ico}")

    # ---- structure: are the swings making higher highs / lower lows? -----
    struct = _structure_read()
    if struct:
        lines.append(struct)

    # ---- day range position ---------------------------------------------
    hi, lo = q.get("high"), q.get("low")
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi > lo:
        pos = (live - lo) / (hi - lo) * 100
        lines.append(f"نطاق اليوم: {num(lo)}–{num(hi)} · السعر عند "
                     f"<b>{pos:.0f}%</b> منه")

    # ---- levels with strength -------------------------------------------
    lines += _levels_with_strength(live)

    # ---- verdict: the desk's own conclusion -----------------------------
    lines.append("")
    lines.append("<b>حكم المكتب:</b>")
    lines.append(_verdict_detail(v))
    lines.append("\n<i>قراءة تحليلية لحظية — مو وعد ربح.</i>")
    return "\n".join(lines)


def _levels(live: float) -> tuple[list[float], list[float]]:
    """Nearest resistance levels above and support levels below the price."""
    try:
        b = bars("1h", 300)
    except Exception:  # noqa: BLE001
        return [], []
    highs, lows = pivots(b, 3, 3)
    res = sorted({round(h, 2) for h in highs if h > live})
    sup = sorted({round(l, 2) for l in lows if l < live}, reverse=True)
    return res, sup


def card_levels() -> str:
    m = read_market()
    live = m["live"]
    res, sup = _levels(live)
    if not res and not sup:
        return "🔔 ما قدرت أقرأ المستويات الآن."
    lines = [f"🔔 <b>المستويات</b> — {SYMBOL}",
             f"السعر الحالي <b>{num(live)}</b>", "", "<b>مقاومات (فوق):</b>"]
    lines += [f"  🔺 {num(x)}" for x in res[:4]] or ["  —"]
    lines.append("<b>دعوم (تحت):</b>")
    lines += [f"  🔻 {num(x)}" for x in sup[:4]] or ["  —"]
    lines.append("\n<i>مستويات من قمم/قيعان سابقة على فريم الساعة.</i>")
    return "\n".join(lines)


def card_scenarios() -> str:
    """The two conditional trades, priced by the desk's own rules.

    A scenario is a plan with a trigger instead of a market entry, so it is
    sized by the same _sized() the live plan uses: risk in the owner's $8-10
    window, target a fixed multiple of it. The trigger is a hair past the level
    (a break, not a touch), and the level is passed to _sized() so a level close
    enough to matter can tighten the stop.
    """
    m = read_market()
    live = m["live"]
    res, sup = _levels(live)
    lines = [f"🎬 <b>السيناريوهات</b> — {SYMBOL}",
             f"السعر <b>{num(live)}</b>", ""]
    if not res and not sup:
        lines.append("ما قدرت أقرأ المستويات الآن.")
        return "\n".join(lines)
    if res:
        trig = res[0] + 0.5
        p = _sized(trig, "long", res[0])
        nxt = f" · المقاومة التالية {num(res[1])}" if len(res) > 1 else ""
        lines += ["<b>🟢 سيناريو صعود</b>",
                  f"  المشغّل: اختراق <b>{num(trig)}</b> "
                  f"(+{trig - live:.1f} نقطة){nxt}",
                  f"  الوقف: {num(p['stop'])} · الهدف: {num(p['target'])}",
                  f"  R:R ≈ {p['rr']:.2f} (مخاطرة ${p['risk_usd']:.0f})", ""]
    if sup:
        trig = sup[0] - 0.5
        p = _sized(trig, "short", sup[0])
        nxt = f" · الدعم التالي {num(sup[1])}" if len(sup) > 1 else ""
        lines += ["<b>🔴 سيناريو هبوط</b>",
                  f"  المشغّل: كسر <b>{num(trig)}</b> "
                  f"(−{live - trig:.1f} نقطة){nxt}",
                  f"  الوقف: {num(p['stop'])} · الهدف: {num(p['target'])}",
                  f"  R:R ≈ {p['rr']:.2f} (مخاطرة ${p['risk_usd']:.0f})", ""]
    lines.append("<i>سيناريوهات مشروطة — ما تنفّذ إلا لما يتحقق المشغّل. "
                 f"الوقف والهدف من قواعد المكتب (مخاطرة ${STOP_MIN_USD:.0f}-"
                 f"${STOP_MAX_USD:.0f}، R:R {TARGET_RR}).</i>")
    # A third, honest case: when the trend is absent the desk's own gate says
    # "no trade" -- say so, and give the range instead of a fake breakout.
    try:
        b15 = closed(bars("15m", 300), "15m")
        adx_v = adx_series(b15)[-1] if b15 else None
    except Exception:  # noqa: BLE001
        adx_v = None
    if isinstance(adx_v, (int, float)) and adx_v < ADX_MIN:
        lines.append("")
        lines.append("<b>⚪ سيناريو عرضي (بلا ترند)</b>")
        if res and sup:
            lines.append(f"  النطاق: {num(sup[0])} ↔ {num(res[0])} "
                         f"(ADX {adx_v:.0f} — بلا ترند)")
            lines.append("  اللعب: اشترِ عند الدعم إذا صمد · بِع عند المقاومة إذا رفض")
            lines.append("  <i>المكتب ما ياخد صفقة ترند لما ADX تحت 20.</i>")
    return "\n".join(lines)


def _zones(live: float) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
    """Cluster the 1h pivots into zones, split by the live price.

    A zone is a price where several pivots landed close together (within a
    quarter of the 1h ATR) -- the same price, seen more than once, is what makes
    it worth naming. Returns (sell_zones_above, buy_zones_below), each a list of
    (centre_price, touch_count), nearest first.
    """
    try:
        b = bars("1h", 300)
    except Exception:  # noqa: BLE001 - a missing frame must not kill the card
        return [], []
    highs, lows = pivots(b, 3, 3)
    tol = max(1.0, (atr(b) or 1.0) * 0.25)
    pts = sorted(highs + lows)
    zones: list[list[float]] = []
    for p in pts:
        if zones and p - zones[-1][-1] <= tol:
            zones[-1].append(p)
        else:
            zones.append([p])
    out = []
    for z in zones:
        if len(z) < 2:                       # a single touch is not a zone
            continue
        out.append((sum(z) / len(z), len(z)))
    sell = sorted([z for z in out if z[0] > live])
    buy = sorted([z for z in out if z[0] < live], reverse=True)
    return sell, buy


def _liquidity(live: float) -> tuple[list[float], list[float]]:
    """Repeated highs / lows where stops cluster -- a liquidity pool.

    A level touched many times is where resting stops sit: above equal highs
    (buy-stops of shorts) and below equal lows (sell-stops of longs). Reported as
    liquidity, never as 'manipulation' -- it is just where the orders are.
    """
    try:
        b = bars("1h", 300)
    except Exception:  # noqa: BLE001
        return [], []
    highs, lows = pivots(b, 3, 3)
    tol = max(1.0, (atr(b) or 1.0) * 0.3)

    def clusters(seq):
        seq = sorted(seq)
        out, cur = [], []
        for x in seq:
            if cur and x - cur[-1] <= tol:
                cur.append(x)
            else:
                if len(cur) >= 2:
                    out.append(sum(cur) / len(cur))
                cur = [x]
        if len(cur) >= 2:
            out.append(sum(cur) / len(cur))
        return out

    above = [c for c in clusters(highs) if c > live]
    below = [c for c in clusters(lows) if c < live]
    return sorted(above), sorted(below, reverse=True)


def card_map() -> str:
    """The map: where to SELL (zones above) and where to BUY (zones below).

    It is a VIEW of the pivots the desk already uses, not new math: supply and
    demand are the same prices as resistance and support, so the card names them
    once each. Liquidity marks the repeated highs/lows where stops sit. It
    carries no buy/sell instruction -- the recommendation card does that.
    """
    m = read_market()
    live = m["live"]
    sell, buy = _zones(live)
    liq_hi, liq_lo = _liquidity(live)
    lines = [f"🗺️ <b>خريطة {SYMBOL}</b>", f"السعر <b>{num(live)}</b>", ""]
    lines.append("<b>🔴 مناطق البيع (فوق السعر):</b>")
    if sell:
        for z, n in sell[:4]:
            lines.append(f"  🔺 {num(z)} ({n} لمسة)")
    else:
        lines.append("  — ما في منطقة قريبة")
    lines.append("")
    lines.append("<b>🟢 مناطق الشراء (تحت السعر):</b>")
    if buy:
        for z, n in buy[:4]:
            lines.append(f"  🔻 {num(z)} ({n} لمسة)")
    else:
        lines.append("  — ما في منطقة قريبة")
    lines.append("")
    lines.append("<b>💧 سيولة (مكان تجمّع الوقوف):</b>")
    if liq_hi:
        lines.append("  فوق: " + " · ".join(num(x) for x in liq_hi[:3]))
    if liq_lo:
        lines.append("  تحت: " + " · ".join(num(x) for x in liq_lo[:3]))
    if not liq_hi and not liq_lo:
        lines.append("  — بلا تجمّع واضح")
    lines.append("")
    lines.append("<i>خريطة عرضية — العرض والطلب هما نفس الأسعار (مقاومة/دعم). "
                 "ما فيها أمر بيع أو شراء.</i>")
    return "\n".join(lines)


def card_news() -> str:
    """The news layer -- INFORMATION ONLY, never a trade input.

    The desk has no historical news data, so a news rule could never be
    back-tested; it is shown so the owner knows WHY the tape may be jumpy, and
    is explicitly labelled as not a signal. The desk's entries are unchanged by
    anything here.

    The headlines come from the NEWS_ITEMS list the owner maintains, PLUS a
    best-effort live pull from Google News (keyless RSS) when
    DESK_NEWS_LIVE=1. A failed fetch is silent -- the manual list still shows.
    Times are shown in the owner's clock (DESK_TZ_OFFSET hours from UTC, 3 =
    Syria), because Google stamps headlines in GMT.
    """
    lines = [f"📰 <b>أخبار {SYMBOL}</b>", ""]
    lines.append(f"🕒 آخر تحديث: <b>{_clock(datetime.now(UTC))}</b> {TZ_LABEL}")
    lines.append("")
    items: list[tuple] = []
    if NEWS_LIVE:
        with suppress(Exception):      # a dead feed must not hide the list
            items = list(_news_fetch())
    items += [tuple(x) for x in NEWS_ITEMS]
    if not items:
        lines.append("— ما في أخبار. حدّث <code>NEWS_ITEMS</code> أو شغّل "
                     "<code>DESK_NEWS_LIVE=1</code>.")
    else:
        seen: set[str] = set()
        for it in items[:12]:
            tag, title = it[0], it[1]
            when = it[2] if len(it) > 2 else ""
            key = str(title)[:40]
            if key in seen:
                continue
            seen.add(key)
            stamp = f"  <i>({when})</i>" if when else ""
            lines.append(f"• {tag} {esc(title)}{stamp}")
    lines.append("")
    lines.append("<i>⚠️ معلومة فقط — مو إشارة. المكتب ما يفتح صفقة على الخبر، "
                 "وقواعد الدخول ما تتأثر فيه. القرار الخبري إلك.</i>")
    return "\n".join(lines)


def _clock(dt: datetime) -> str:
    """A timestamp in the owner's clock, short: HH:MM today, else DD/MM HH:MM."""
    if dt.tzinfo is None:          # a feed that omits the zone is GMT-stamped
        dt = dt.replace(tzinfo=UTC)
    loc = dt.astimezone(timezone(timedelta(hours=TZ_OFFSET_H)))
    today = datetime.now(UTC).astimezone(
        timezone(timedelta(hours=TZ_OFFSET_H))).date()
    return loc.strftime("%H:%M") if loc.date() == today else loc.strftime(
        "%d/%m %H:%M")


def _news_fetch() -> list[tuple[str, str, str]]:
    """Best-effort gold headlines from Google News RSS (keyless).

    Returns a short list of (emoji, title, time) where time is the headline's
    publication clock in the owner's timezone. Parsed with a regex, not an XML
    parser, so a malformed feed degrades to [] instead of raising. The query is
    the one the desk's symbol implies; the owner can override it with
    DESK_NEWS_QUERY.
    """
    q = urllib.parse.quote(NEWS_QUERY)
    url = (f"https://news.google.com/rss/search?q={q}"
           "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "ignore")
    out: list[tuple[str, str, str]] = []
    parsed: list[tuple[datetime, str, str, str]] = []
    for block in re.findall(r"<item>(.*?)</item>", raw, re.DOTALL)[:10]:
        m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        if not m:
            continue
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip()
        title = _unescape(title)
        when, stamp = "", datetime.min.replace(tzinfo=UTC)
        pm = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
        if pm:
            with suppress(Exception):
                stamp = parsedate_to_datetime(pm.group(1).strip())
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                when = _clock(stamp)
        low = title.lower()
        tag = ("🔥" if any(w in low for w in ("cpi", "inflation", "fed", "rate"))
               else "📈" if any(w in low for w in ("rally", "gain", "rise", "high"))
               else "📉" if any(w in low for w in ("fall", "drop", "slump", "low"))
               else "▫️")
        parsed.append((stamp, tag, title[:120], when))
    # Google's feed is not time-ordered; the owner reads newest first.
    parsed.sort(key=lambda x: x[0], reverse=True)
    out = [(tag, title, when) for _, tag, title, when in parsed]
    return out




def _sized(entry: float, direction: str, stop_level: float | None) -> dict:
    """Size one trade from `entry`, the desk's single set of rules.

    Both ends live inside the owner's windows: risk in $8-12, reward in $15-20.
    The stop is tightened by `stop_level` when that level is close enough to
    matter, then clamped to the risk window. The target is the risk times
    TARGET_RR, then clamped into the reward window -- and re-floored at
    risk * MIN_RR so the R:R floor can never be broken by the clamp (with a
    $8-12 risk the floor needs at most $18, inside the $20 cap by construction).
    plan() and the scenario card both call this, so no surface can quote a
    trade the desk would reject.
    """
    d = 1 if direction == "long" else -1
    pts = STOP_PICK_USD / POINT_VALUE
    if stop_level is not None:
        room = (entry - stop_level) * d / POINT_VALUE
        if room > 0:
            pts = min(room + 1.0, STOP_MAX_USD / POINT_VALUE)
    pts = max(STOP_MIN_USD / POINT_VALUE,
              min(pts, STOP_MAX_USD / POINT_VALUE))
    stop = entry - d * pts * POINT_VALUE
    risk_usd = abs(entry - stop) * POINT_VALUE
    gain_usd = risk_usd * max(TARGET_RR, MIN_RR)
    gain_usd = min(max(gain_usd, TARGET_MIN_USD), TARGET_MAX_USD)
    gain_usd = max(gain_usd, risk_usd * MIN_RR)
    tgt = entry + d * (gain_usd / POINT_VALUE)
    rr = gain_usd / risk_usd if risk_usd else 0.0
    return {"direction": direction, "entry": entry, "stop": stop,
            "target": tgt, "rr": rr, "risk_usd": risk_usd,
            "risk_pts": risk_usd / POINT_VALUE, "gain_usd": gain_usd}


def frame_agrees(direction: str, live: float,
                 b15: list[dict] | None = None) -> bool:
    """Does the live price agree with `direction` on the filter frame?

    A long wants price ABOVE the frame's EMA20, a short BELOW it. A missing
    frame or an unreadable EMA20 returns True: the filter is a guard, and it
    must never be the reason the desk stops trading entirely.
    """
    if not TREND_FILTER_TF:
        return True
    try:
        b = b15 if b15 is not None else bars(TREND_FILTER_TF, 300)
        b = closed(b, TREND_FILTER_TF)
        e = ema([x["close"] for x in b], 20)
        if e is None:
            return True
        return live > e if direction == "long" else live < e
    except Exception:  # noqa: BLE001
        return True


def filter_status(direction: str, b15: list[dict] | None = None) -> dict:
    """The combo's quality gate, read once on the 15m frame.

    Four independent gates, all of which must pass for a plan to be worth
    stating: ADX in a trend band (not chop, not extended), ATR inside a
    volatility band (alive but not wild), MACD histogram turning the entry's
    way, and RSI in a pullback band (a long is a dip, not a chase). Every gate
    fails OPEN when its reading is missing -- the gate is a quality filter, and
    a filter that fails closed is a desk that goes silent on a data blip.

    Returns {"ok", "failed", "readings"} so a card can show the owner exactly
    which gate held a trade back, and by how much.
    """
    try:
        b = b15 if b15 is not None else bars("15m", 300)
    except Exception:  # noqa: BLE001 - a broken feed must not silence the desk
        b = []
    b = closed(b, "15m")
    live = b[-1]["close"] if b else 0.0
    rsi_v = rsi([x["close"] for x in b]) if b else None
    atr_v = atr(b) if b else None
    adx_v = adx_series(b)[-1] if b else None
    hist = macd_hist_series(b)
    h, hp = (hist[-1], hist[-2]) if len(hist) >= 2 else (None, None)
    lo, hi = RSI_LONG if direction == "long" else RSI_SHORT
    atr_pct = (atr_v / live) if (atr_v and live > 0) else None

    gates = {
        "ADX": "adx" not in FILTER_GATES
        or adx_v is None or ADX_MIN <= adx_v <= ADX_MAX,
        "ATR": "atr" not in FILTER_GATES
        or atr_pct is None or ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT,
        "MACD": "macd" not in FILTER_GATES
        or h is None or hp is None or (h > hp if direction == "long"
                                       else h < hp),
        "RSI": "rsi" not in FILTER_GATES
        or rsi_v is None or lo <= rsi_v <= hi,
    }
    readings = {"rsi": rsi_v, "adx": adx_v, "atr_pct": atr_pct,
                "macd_hist": h, "macd_prev": hp}
    return {"ok": all(gates.values()),
            "failed": [k for k, v in gates.items() if not v],
            "readings": readings}


def _filter_line(fs: dict) -> str:
    """One owner-facing line naming the gate that held a trade back."""
    r = fs["readings"]
    names = {"ADX": "ADX (قوة الاتجاه)", "ATR": "ATR (التقلب)",
             "MACD": "MACD (الزخم)", "RSI": "RSI (التشبّع)"}
    parts = []
    for k in fs["failed"][:2]:
        v = r.get({"ADX": "adx", "ATR": "atr_pct", "MACD": "macd_hist",
                   "RSI": "rsi"}[k])
        if k == "ATR" and v is not None:
            shown = f"{v*100:.2f}%"
        elif v is None:
            shown = "—"
        else:
            shown = f"{v:.0f}" if k != "MACD" else f"{v:+.2f}"
        parts.append(f"{names[k]} {shown}")
    return " · ".join(parts)


def plan(m: dict | None = None) -> dict | None:
    """The desk's one decision engine: a sized, valid plan or None.

    One source for both the button and the auto-desk -- two copies of this logic
    would drift, and a card that disagreed with the trade it opened would be the
    worst kind of bug.

    Returns a dict with direction, entry, stop, target, rr, risk_usd, risk_pts,
    gain_usd, up/dn frame counts, and whether the direction is merely confirmed
    (CONFIRM_FRAMES) or strong (STRONG_FRAMES) -- the auto-desk needs that last
    bit to decide whether a second trade is allowed.
    """
    m = m or read_market()
    q, live, trends = m["q"], m["live"], m["trends"]
    if not market_open(q) or live <= 0:
        return None
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    res, sup = _levels(live)

    def sized(direction: str) -> dict:
        # A long's stop belongs just BELOW the nearest support; a short's just
        # ABOVE the nearest resistance.
        level = (max(sup) if sup else None) if direction == "long" \
            else (min(res) if res else None)
        p = _sized(live, direction, level)
        p["up"], p["dn"] = up, dn
        p["strong"] = (up if direction == "long" else dn) >= STRONG_FRAMES
        return p

    try:
        b15 = bars(TREND_FILTER_TF, 300) if TREND_FILTER_TF else None
    except Exception:  # noqa: BLE001 - a broken feed must not kill the plan
        b15 = None

    def gated(direction: str) -> dict | None:
        if not frame_agrees(direction, live, b15):
            return None
        if FILTERS_ON and not filter_status(direction, b15)["ok"]:
            return None
        return sized(direction)

    if up >= CONFIRM_FRAMES:
        p = gated("long")
        if p is not None:
            return p
    if dn >= CONFIRM_FRAMES:
        p = gated("short")
        if p is not None:
            return p
    return None


def _plan_lines(p: dict) -> list[str]:
    d = "شراء 🟢" if p["direction"] == "long" else "بيع 🔴"
    return [f"{d} · دخول {num(p['entry'])}",
            f"🛡 الوقف {num(p['stop'])} "
            f"(−{p['risk_pts']:.1f} نقطة = −${p['risk_usd']:.0f})",
            f"🎯 الهدف {num(p['target'])} "
            f"(+{p['gain_usd']/POINT_VALUE:.1f} نقطة = +${p['gain_usd']:.0f})",
            f"📊 R:R {p['rr']:.2f} · لوت {LOT} · حساب ${ACCOUNT_USD:.0f}"]


def card_recommend() -> str:
    m = read_market()
    p = plan(m)
    if p is None:
        if not market_open(m["q"]):
            return ("🎯 <b>التوصية</b> — XAUUSD\n<b>لا صفقة</b> — السوق مسكّر.\n\n"
                    "<i>المكتب ما يعطي إشارة على سعر ميت.</i>")
        lean, mark = _bias(m["trends"])
        up = sum(1 for tf in FRAMES if m["trends"].get(tf) == 1)
        dn = sum(1 for tf in FRAMES if m["trends"].get(tf) == -1)
        if up >= CONFIRM_FRAMES or dn >= CONFIRM_FRAMES:
            direction = "long" if up >= CONFIRM_FRAMES else "short"
            way = "صاعد" if direction == "long" else "هابط"
            try:
                b15 = bars(TREND_FILTER_TF, 300)
            except Exception:  # noqa: BLE001
                b15 = []
            if FILTERS_ON:
                fs = filter_status(direction, b15)
                if not fs["ok"]:
                    return ("🎯 <b>التوصية</b> — XAUUSD\n"
                            f"<b>لا صفقة</b> — الاتجاه {way} بس <b>فلتر "
                            f"الجودة مانع</b>:\n{_filter_line(fs)}\n\n"
                            "<i>المكتب ما ياخد إلا الصفقات النظيفة.</i>")
            if not frame_agrees(direction, m["live"], b15):
                return ("🎯 <b>التوصية</b> — XAUUSD\n"
                        f"<b>لا صفقة</b> — الاتجاه {way} بس السعر "
                        f"{'تحت' if direction == 'long' else 'فوق'} "
                        f"EMA20 ({TREND_FILTER_TF}).\n"
                        "<i>المكتب ما ياخد عكس الزخم القصير — استنى يرجع.</i>")
        return ("🎯 <b>التوصية</b> — XAUUSD\n"
                f"<b>لا صفقة</b> — الفريمات ما اتفقت "
                f"({CONFIRM_FRAMES}/6 مطلوبة).\n"
                f"ميل القراءة: {mark} <b>{lean}</b>\n\n"
                "<i>المكتب ما يعطي إشارة لما السوق محتار.</i>")
    return ("🎯 <b>التوصية</b> — " + SYMBOL + "\n" +
            "\n".join(_plan_lines(p)) + "\n\n"
            "<i>التزام بالمخاطرة — كل صفقة على حسابك.</i>")


# ========================================================================
# the journal
# ========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, direction TEXT NOT NULL,
    entry REAL NOT NULL, stop REAL NOT NULL, target REAL,
    size REAL, opened_at TEXT NOT NULL, note TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    result TEXT, exit_price REAL, r_multiple REAL, closed_at TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init_db() -> None:
    with closing(_conn()) as c:
        c.executescript(SCHEMA)
        c.commit()


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse(stamp: str | None):
    if not stamp:
        return None
    s = stamp.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def add_trade(direction, entry, stop, target=None, size=None, note=None) -> int:
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO trades (symbol,direction,entry,stop,target,size,"
            "opened_at,note,status) VALUES (?,?,?,?,?,?,?,?,'open')",
            (SYMBOL, direction, entry, stop, target, size, _utcnow(), note))
        c.commit()
        return int(cur.lastrowid or 0)


def all_trades() -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        return list(c.execute("SELECT * FROM trades ORDER BY id"))


def open_trades() -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        return list(c.execute("SELECT * FROM trades WHERE status='open' ORDER BY id"))


def _settle(tid, result, exit_price, r) -> None:
    with closing(_conn()) as c:
        c.execute("UPDATE trades SET status='closed',result=?,exit_price=?,"
                  "r_multiple=?,closed_at=? WHERE id=?",
                  (result, exit_price, r, _utcnow(), tid))
        c.commit()


def _risk(t) -> float:
    return abs(t["entry"] - t["stop"])


def _planned_r(t):
    if t["target"] is None:
        return None
    r = _risk(t)
    return abs(t["target"] - t["entry"]) / r if r else None


def _money(t):
    if t["size"] is None or t["exit_price"] is None:
        return None
    pts = (t["exit_price"] - t["entry"]) if t["direction"] == "long" \
        else (t["entry"] - t["exit_price"])
    return pts * t["size"] * 100.0


def grade() -> int:
    """Walk every open trade forward on 5m bars. First touch wins; a bar that
    spans both the stop and the target takes the stop. Returns how many settled.
    """
    try:
        b = bars("5m", 1000)
    except Exception:  # noqa: BLE001
        return 0
    settled = 0
    for t in open_trades():
        start = _parse(t["opened_at"])
        if start is None:
            continue
        window = [x for x in b if (_parse(x["openTime"]) or start) >= start]
        long = t["direction"] == "long"
        risk = _risk(t)
        for x in window:
            hit_stop = (x["low"] <= t["stop"]) if long else (x["high"] >= t["stop"])
            hit_tgt = t["target"] is not None and (
                (x["high"] >= t["target"]) if long else (x["low"] <= t["target"]))
            if hit_stop:
                _settle(t["id"], "stop", t["stop"], -1.0)
                settled += 1
                break
            if hit_tgt and t["target"] is not None:
                r = (abs(t["target"] - t["entry"]) / risk) if risk else None
                _settle(t["id"], "target", t["target"], r)
                settled += 1
                break
    return settled


def _period_cutoff(period: str):
    now = datetime.now(UTC)
    p = (period or "all").lower()
    if p in ("day", "يومي"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if p in ("week", "أسبوعي"):
        return now - timedelta(days=7)
    if p in ("month", "شهري"):
        return now - timedelta(days=30)
    return None


def stats(period: str = "all") -> dict:
    cutoff = _period_cutoff(period)
    rows = [t for t in all_trades() if t["status"] == "closed"
            and t["r_multiple"] is not None
            and (cutoff is None or (_parse(t["closed_at"]) or datetime.min.replace(
                tzinfo=UTC)) >= cutoff)]
    if not rows:
        return {"n": 0, "open": len(open_trades())}
    rs = [t["r_multiple"] for t in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    monies = [_money(t) for t in rows if _money(t) is not None]
    gw = sum(wins)
    gl = abs(sum(losses))
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(rows), "total_r": sum(rs),
        "expectancy_r": sum(rs) / len(rows),
        "profit_factor": (gw / gl) if gl else None,
        "money": sum(monies) if monies else None,
        "profit_money": sum(m for m in monies if m > 0),
        "loss_money": sum(m for m in monies if m <= 0),
        "unpriced": len(rows) - len(monies),
        "open": len(open_trades()),
    }


def _line(t) -> str:
    d = "شراء 🟢" if t["direction"] == "long" else "بيع 🔴"
    head = f"#{t['id']} {d} — دخل {num(t['entry'])} · وقف {num(t['stop'])}"
    if t["target"] is not None:
        head += f" · هدف {num(t['target'])}"
    if t["status"] == "open":
        tail = "  ⏳ مفتوحة"
    elif t["result"] == "target":
        tail = f"  ✅ الهدف — {t['r_multiple']:+.2f}R"
    elif t["result"] == "stop":
        tail = f"  ❌ الوقف — {t['r_multiple']:+.2f}R"
    elif t["result"] == "manual":
        r = f" — {t['r_multiple']:+.2f}R" if t["r_multiple"] is not None else ""
        tail = f"  🔒 أُغلقت يدوياً{r}"
    else:
        tail = f"  ⚪ {t['result'] or 'مغلقة'}"
    m = _money(t)
    if m is not None:
        tail += f" · {m:+,.2f}$"
    return head + tail


def _stats_block(st: dict) -> list[str]:
    if not st.get("n"):
        return ["ما في صفقات مغلقة بهالفترة."]
    pf = st.get("profit_factor")
    pf_s = f"{pf:.2f}" if pf is not None else "—"
    out = [f"الصفقات: <b>{st['n']}</b> · "
           f"<b>{st['wins']}</b> ربح / <b>{st['losses']}</b> خسارة "
           f"({st['win_rate']:.0%})",
           f"النتيجة: <b>{st['total_r']:+.2f}R</b> مجموع · "
           f"توقّع <b>{st['expectancy_r']:+.2f}R</b> · معامل ربح {pf_s}"]
    if st.get("money") is not None:
        out.append(f"المال: <b>{st['money']:+,.2f}$</b> "
                   f"(ربح {st['profit_money']:+,.2f}$ · "
                   f"خسارة {st['loss_money']:+,.2f}$)")
        if st.get("unpriced"):
            out.append(f"<i>{st['unpriced']} صفقة بلا حجم مسجّل — ما انحسبت بالمال.</i>")
    return out


def period_report(period: str) -> str:
    grade()
    titles = {"day": "📅 السجل اليومي", "week": "🗓️ السجل الأسبوعي",
              "month": "📆 السجل الشهري"}
    cutoff = _period_cutoff(period)
    rows = [t for t in all_trades() if t["status"] == "closed"
            and (cutoff is None or (_parse(t["closed_at"]) or
                                    datetime.min.replace(tzinfo=UTC)) >= cutoff)]
    rows.sort(key=lambda t: t["closed_at"] or "")
    st = stats(period)
    L = [f"<b>{titles.get(period, 'السجل')}</b>"]
    L += _stats_block(st)
    if rows:
        L.append("")
        L += ["  " + _line(t) for t in rows]
    L.append("\n<i>التقييم بالـR مو بالنقاط.</i>")
    return "\n".join(L)


def journal_menu_text() -> str:
    grade()
    def head(label, p):
        st = stats(p)
        if not st.get("n"):
            return f"{label}: —"
        return f"{label}: {st['n']} صفقة · {st['win_rate']:.0%} · {st['total_r']:+.2f}R"
    o = stats()
    return "\n".join([
        "📒 <b>سجل الصفقات</b>",
        f"📂 مفتوحة: <b>{o.get('open', 0)}</b>",
        head("📅 اليوم", "day"), head("🗓️ الأسبوع", "week"),
        head("📆 الشهر", "month"), "", "<i>اختر الفترة 👇</i>"])


def full_journal() -> str:
    grade()
    rows = all_trades()
    if not rows:
        return ("📋 <b>كل الصفقات</b>\nما في صفقات بعد.\n\n"
                "سجّل صفقة: <code>سجل شراء 4075 وقف 4069 هدف 4085</code>")
    L = ["📋 <b>كل الصفقات</b>"] + _stats_block(stats())
    L.append("")
    L += ["  " + _line(t) for t in rows]
    return "\n".join(L)


def card_open() -> str:
    grade()
    rows = open_trades()
    if not rows:
        return ("📂 <b>الصفقات المفتوحة</b>\n"
                "ما في صفقة مفتوحة هلّق — المكتب ما بحمل شي حالياً.")
    L = [f"📂 <b>الصفقات المفتوحة</b> — العدد <b>{len(rows)}</b>", ""]
    L += ["  " + _line(t) for t in rows]
    L.append("")
    L.append("<i>لسكّر صفقة بيدك، اضغط زرها أو ابعت: "
             "<code>سكر 4</code></i>")
    return "\n".join(L)


def close_keyboard() -> dict | None:
    """One button per open trade, so closing is a tap, not a typed id."""
    rows = open_trades()
    if not rows:
        return None
    return {"inline_keyboard":
            [[{"text": f"🔒 سكّر #{t['id']}", "callback_data": f"close:{t['id']}"}]
             for t in rows]}


def card_who(tg: "Telegram") -> str:
    """Who receives everything: the live allow list, with each name resolved.

    The name is read live from Telegram (first_name/username), so a new chat
    shows up here the moment it is added -- this is the answer to 'who is on
    the bot?', not a stale list.
    """
    chats = sorted(load_allow())
    L = [f"👥 <b>مين موجود بالبوت</b> — العدد <b>{len(chats)}</b>", ""]
    for cid in chats:
        role = "👑 المالك" if cid == _backup_chat() else "👤 مشترك"
        try:
            u = (tg.call("getChat", {"chat_id": cid}) or {}).get("result") or {}
            nm = (str(u.get("first_name") or "") + " " +
                  str(u.get("last_name") or "")).strip() or "—"
            un = f"  @{u['username']}" if u.get("username") else ""
            L.append(f"  {role}  {esc(nm)}{un}")
            L.append(f"       <code>{cid}</code>")
        except Exception:  # noqa: BLE001 - a dead id must not hide the rest
            L.append(f"  {role}  <i>تعذّر الجلب</i>")
            L.append(f"       <code>{cid}</code>")
    L.append("")
    L.append("<i>كل هدول بيوصلهم نفس الشي: الصفقات والإغلاقات.</i>")
    return "\n".join(L)


def card_monitor() -> str:
    """A follow-up for every open trade: progress, floating, break-even hint."""
    grade()
    rows = open_trades()
    if not rows:
        return card_open()
    try:
        live = float(quote().get("mid") or 0)
    except Exception:  # noqa: BLE001
        live = 0.0
    out = ["🔭 <b>متابعة الصفقات المفتوحة</b>", ""]
    for t in rows:
        d = 1 if t["direction"] == "long" else -1
        risk = _risk(t)
        span = ((t["target"] - t["entry"]) * d) if t["target"] is not None else 0
        pct = max(0.0, min(1.0, (live - t["entry"]) * d / span)) if span else 0.0
        filled = int(round(pct * 10))
        bar = "▰" * filled + "▱" * (10 - filled)
        float_pts = (live - t["entry"]) * d
        out.append(f"#{t['id']} {'شراء 🟢' if d > 0 else 'بيع 🔴'}")
        out.append(f"  السعر {num(live)} · العائم {float_pts:+,.1f} نقطة")
        out.append(f"  التقدم {bar} {pct*100:.0f}%")
        if risk:
            trig = t["entry"] + d * risk * 0.5
            nstop = t["entry"] + d * (risk * 0.5 - 0.5)
            if (live - trig) * d >= 0:
                out.append(f"  🛡 مؤمّن — انقل الوقف إلى {num(nstop)}")
            else:
                out.append(f"  🛡 عند {num(trig)} انقل الوقف إلى {num(nstop)}")
        out.append("")
    return "\n".join(out)


def parse_trade(text: str) -> dict | str:
    """Read 'سجل شراء 4075 وقف 4069 هدف 4085 حجم 0.01' into fields.

    Returns the parsed dict, or a string explaining what is missing. Pure: it
    touches no storage, so a diagnostic (--selftest) can exercise the parser
    without writing a trade into the owner's journal.
    """
    toks = text.split()
    if not toks:
        return "اكتب الصفقة هيك: <code>سجل شراء 4075 وقف 4069 هدف 4085</code>"
    direction = None
    for w in toks:
        if w in ("شراء", "buy", "long"):
            direction = "long"
            break
        if w in ("بيع", "sell", "short"):
            direction = "short"
            break
    nums: dict[str, float] = {}
    key = None
    for w in toks:
        if w in ("وقف", "هدف", "حجم", "دخل", "entry", "stop", "target", "size"):
            key = {"وقف": "stop", "هدف": "target", "حجم": "size",
                   "دخل": "entry"}.get(w, w)
            continue
        try:
            v = float(w)
        except ValueError:
            continue
        if key and key not in nums:
            nums[key] = v
        elif "entry" not in nums:
            nums["entry"] = v
        key = None
    if direction is None:
        return "ما فهمت الاتجاه — اكتب «شراء» أو «بيع»."
    if "stop" not in nums:
        return "الوقف مطلوب — بلا وقف ما في قياس بالـR."
    if "entry" not in nums:
        return "لازم تحدد سعر الدخول."
    return {"direction": direction, "entry": nums["entry"], "stop": nums["stop"],
            "target": nums.get("target"), "size": nums.get("size")}


def record_trade(text: str) -> str:
    """Parse a trade line and store it in the journal."""
    got = parse_trade(text)
    if isinstance(got, str):
        return got
    tid = add_trade(got["direction"], got["entry"], got["stop"],
                    got["target"], got["size"])
    t = [x for x in all_trades() if x["id"] == tid][0]
    d = "شراء 🟢" if got["direction"] == "long" else "بيع 🔴"
    out = [f"✅ انسجّلت الصفقة #{tid}", f"{d} · دخل {num(got['entry'])} · "
           f"وقف {num(got['stop'])}"]
    if got.get("target"):
        out.append(f"هدف {num(got['target'])}")
    pr = _planned_r(t)
    if pr is not None and pr < 1.0:
        out.append(f"⚠️ المضاعف {pr:.2f}R أقل من 1 — خاطرة أكبر من المكسب.")
    out.append("\n<i>رح أتابعها وأسجّل نتيجتها لما تنحسم.</i>")
    return "\n".join(out)


def help_text() -> str:
    return ("❓ <b>مساعدة</b>\n"
            "أنا مكتب تحليل للذهب (XAUUSD). أقرأ السوق، وأبعتلك <b>صفقة "
            "مناسبة لحسابك</b> (لوت 0.01، $200): ربح $15-20 وخسارة $8-10، "
            "مع الوقف والهدف. المكتب <b>يسجّلها ويسجّل نتيجتها لحاله</b> — "
            "ما في داعي تسجّل شي.\n\n"
            "<b>قواعد الصفقات:</b>\n"
            "• صفقة أولى لمّا يتأكد الاتجاه (4/6 فريمات).\n"
            "• <b>صفقة وحدة بأي وقت</b> — المكتب ما بيفتح تانية إلا لما "
            "تنسكّر الأولى (وقف أو هدف).\n"
            "• إذا كانت الإشارة قوية وبدك تزيد، سكّر الأولى بيدك أولاً.\n\n"
            "<b>بتقدر تسكّر صفقة بيدك</b> (لما تشوف السوق انقلب):\n"
            "«📂 الصفقات المفتوحة» ← اضغط <b>🔒 سكّر #</b>، أو ابعت "
            "<code>سكر 4</code>.\n\n"
            "<b>وكمان بتقدر تسجّل صفقتك بنفسك</b> (اختياري):\n"
            "<code>سجل شراء 4075 وقف 4069 هدف 4085 حجم 0.01</code>\n\n"
            "تابع: «📂 الصفقات المفتوحة» و«📒 السجل» (يومي/أسبوعي/شهري).\n"
            "«👥 المشتركين» بيوريك مين بيوصلو كل شي.\n\n"
            "<i>قراءة تحليلية، مو وعد ربح.</i>")


def card_action() -> str:
    """The one thing to do right now -- the card the owner asked for.

    It reads the same engine as everything else and answers in one line: open a
    trade (with its exact numbers), wait for the gate, stand aside, or manage
    the trade that is already open. It never invents a plan -- if plan() is
    None it says so and gives the single condition that would change the answer.

    It formats from the shared _verdict() dict, the same one the analysis card's
    top line uses, so the two cards cannot contradict each other.
    """
    m = read_market()
    live = m["live"]
    v = _verdict(m)
    head = "🧭 <b>شو تعمل هلّق</b> — " + SYMBOL + "\n"

    if v["state"] == "closed":
        return (head + "⛔ السوق مسكّر — ما في شي تعمله.\n"
                "<i>استنى يفتح السوق.</i>")

    if v["state"] == "hold":
        t = v["trade"]
        d = 1 if t["direction"] == "long" else -1
        r_now = ((live - float(t["entry"])) * d /
                 abs(float(t["entry"]) - float(t["stop"]))
                 if abs(float(t["entry"]) - float(t["stop"])) else 0)
        dir_txt = "شراء 🟢" if d == 1 else "بيع 🔴"
        return (head + f"📂 عندك صفقة مفتوحة #{t['id']} ({dir_txt}).\n"
                f"دخل {num(t['entry'])} · وقف {num(t['stop'])} · "
                f"هدف {num(t['target'])}\n"
                f"حالياً <b>{r_now:+.2f}R</b> · السعر {num(live)}\n\n"
                "<b>شو تعمل:</b> خلّيها — الوقف والهدف بيشتغلوا لحالهم.\n"
                "<i>لا تزيد على نفس الاتجاه إلا لما تسكّرها.</i>")

    if v["state"] == "enter":
        return (head + "🟢 <b>ادخل صفقة الآن</b> — الشروط مكتملة:\n" +
                "\n".join(_plan_lines(v["plan"])) + "\n\n"
                "<i>سجّلها بالبوت أو بمنصتك — الوقف إلزامي.</i>")

    # wait: say why, and the single condition that flips it.
    tf_ar = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
             "15m": "15د", "5m": "5د"}.get(TREND_FILTER_TF, TREND_FILTER_TF)
    e = v["ema"]
    if v.get("blocked"):
        way = "صاعد" if v.get("direction") == "long" else "هابط"
        why = f"الاتجاه {way}، بس فلتر الجودة مانع: {v['blocked']}."
        todo = ("⏸️ <b>استنى</b> يتحسّن الفلتر (اتجاه واضح + زخم مؤيد).")
    elif v["up"] >= CONFIRM_FRAMES:
        why = ("الاتجاه صاعد، بس السعر تحت EMA20 "
               f"({tf_ar}) — الدخول عكس الزخم.")
        todo = (f"⏸️ <b>استنى</b> شمعة {tf_ar} تسكّر فوق "
                f"<b>{num(e)}</b>." if e else "⏸️ <b>استنى</b> تأكيد الزخم.")
    elif v["dn"] >= CONFIRM_FRAMES:
        why = ("الاتجاه هابط، بس السعر فوق EMA20 "
               f"({tf_ar}) — الدخول عكس الزخم.")
        todo = (f"⏸️ <b>استنى</b> شمعة {tf_ar} تسكّر تحت "
                f"<b>{num(e)}</b>." if e else "⏸️ <b>استنى</b> تأكيد الزخم.")
    else:
        why = f"الفريمات ما اتفقت ({v['up']}ص · {v['dn']}هـ) — السوق محتار."
        todo = "⏸️ <b>استنى</b> يتفق 4 من 6 فريمات على اتجاه."
    return (head + "⏸️ <b>لا تدخل الآن.</b>\n"
            f"<i>{why}</i>\n\n"
            f"<b>شو تعمل:</b> {todo}\n"
            "<i>الواقف بأمان أحسن من الداخل غلط.</i>")


def alerts_guide() -> str:
    """The map of every alert the desk sends and what to do about each."""
    return ("🔔 <b>دليل التنبيهات</b> — " + SYMBOL + "\n"
            "هاي كل رسالة رح يبعتها المكتب، وشو تعمل معها 👇\n\n"
            "<b>1️⃣ صفقة جديدة</b> — «المكتب لقط صفقة»\n"
            "  يعني: الشروط اكتملت وفتح صفقة.\n"
            "  👉 <i>ما تعمل شي — المكتب بيديرها. لو بدك، سجّلها بمنصتك.</i>\n\n"
            "<b>2️⃣ انحسمت صفقة</b> — هدف / وقف / يدوي\n"
            "  يعني: الصفقة سكّرت والنتيجة محسوبة بالـR.\n"
            "  👉 <i>راجع النتيجة، والمكتب رح يفتح جديدة إذا تجددت الشروط.</i>\n\n"
            "<b>3️⃣ قرب الهدف</b> 🎯\n"
            "  يعني: السعر قرّب من هدف صفقة مفتوحة.\n"
            "  👉 <i>لك الخيار: خذ الربح الآن، أو خلّي الهدف يشتغل.</i>\n\n"
            "<b>4️⃣ قرب الوقف</b> ⚠️\n"
            "  يعني: السوق ضد صفقتك.\n"
            "  👉 <i>قرّر: اخرج يدوي بجزء من الخسارة، أو خلّي الوقف يشتغل.</i>\n\n"
            "<b>5️⃣ قرب الدعم</b> 👀\n"
            "  يعني: السعر قرب دعم قوي.\n"
            "  👉 <i>لا تدخل؛ استنى — إذا ارتد = فرصة شراء، إذا كسر = خطر.</i>\n\n"
            "<b>6️⃣ كسر الدعم</b> 🚨\n"
            "  يعني: شمعة 15د سكّرت تحت الدعم.\n"
            "  👉 <i>لا تشتري؛ السوق ضعف. استنى يرجع فوقه.</i>\n\n"
            "<b>7️⃣ رجع فوق الدعم</b> ✅\n"
            "  يعني: السعر رجع فوق الدعم بعد كسره.\n"
            "  👉 <i>ارتداد محتمل — راقب تأكيد الصعود.</i>\n\n"
            "<b>7.5️⃣ اختراق/كسر مستوى مراقب</b> 🚀⚠️\n"
            "  يعني: السعر تعدّى مستوى من قائمة المراقبة (منها 4,138 و4,108).\n"
            "  👉 <i>مستوى مراقب اتكسر — راقب التأكيد قبل الدخول.</i>\n\n"
            "<b>8️⃣ باب الشراء / البيع انفتح</b> 🟢🔴\n"
            "  يعني: شمعة 15د سكّرت فوق/تحت EMA20 (فلتر المكتب).\n"
            "  👉 <i>إذا الاتجاه العام بنفس الجهة = فرصة دخول.</i>\n\n"
            "<b>9️⃣ رفض صفقة</b> ⚠️\n"
            "  يعني: إشارة جديدة بس المكتب ما دخل (الكتاب ممتلئ، أو "
            "الصفقة الحالية متقدمة/رابحة).\n"
            "  👉 <i>ما تعمل شي — حماية من المخاطرة، مو خطأ.</i>\n\n"
            "📌 <b>القاعدة الذهبية:</b> كل تنبيه بيقوللك «شو تعمل» بسطر — "
            "اقرأ آخر سطر دايماً.\n\n"
            "<i>قراءة تحليلية، مو وعد ربح.</i>")


def dispatch(action: str) -> str:
    return {
        "analysis": card_analysis,
        "action": card_action,
        "alerts": alerts_guide,
        "recommend": card_recommend,
        "scenarios": card_scenarios,
        "levels": card_levels,
        "map": card_map,
        "news": card_news,
        "open": card_open,
        "monitor": card_monitor,
        "journal": full_journal,
        "journal_menu": journal_menu_text,
        "j_day": lambda: period_report("day"),
        "j_week": lambda: period_report("week"),
        "j_month": lambda: period_report("month"),
        "howto": help_text,
        "help": help_text,
        "menu": lambda: "📊 <b>مكتب تحليل السوق</b>\nاختر من الأزرار 👇",
    }.get(action, lambda: f"⚠️ زر غير معروف: {esc(action)}")()


# ========================================================================
# allow-list and the poll loop
# ========================================================================
def load_allow() -> set[str]:
    if ALLOW_PATH.exists():
        try:
            return set(json.loads(ALLOW_PATH.read_text()))
        except (ValueError, OSError):
            pass
    return set(SEED_CHATS)


def save_allow(chats: set[str]) -> None:
    ALLOW_PATH.write_text(json.dumps(sorted(chats)))


def load_offset() -> int | None:
    if OFFSET_PATH.exists():
        try:
            return int(json.loads(OFFSET_PATH.read_text()).get("offset"))
        except (ValueError, OSError, TypeError):
            pass
    return None


def save_offset(off: int) -> None:
    OFFSET_PATH.write_text(json.dumps({"offset": off}))


# ========================================================================
# journal backup -- Telegram is the durable store on an ephemeral host
# ========================================================================
def _backup_chat() -> str:
    """The owner's own chat with the bot.

    The durable copy must live where the owner can see it, so this is
    SEED_CHATS[0] (the owner) and never `sorted(load_allow())[0]` -- that sorted
    the chat ids as strings and picked whichever guest happened to sort first,
    which quietly filed the journal in someone else's conversation.
    """
    return BACKUP_CHAT or SEED_CHATS[0]


def _dump_journal() -> dict:
    return {"version": 1, "symbol": SYMBOL, "saved_at": _utcnow(),
            "trades": [dict(t) for t in all_trades()],
            "allow": sorted(load_allow())}


def backup_journal(tg: Telegram) -> bool:
    """Upload the journal as a document and pin it. The pin is the pointer a
    fresh container reads at startup, so the journal outlives the filesystem."""
    chat = _backup_chat()
    data = json.dumps(_dump_journal(), ensure_ascii=False, indent=1).encode()
    res = tg.send_document(
        chat, BACKUP_FILE, data,
        caption=("📒 <b>نسخة احتياطية من السجل</b>\n"
                 f"عدد الصفقات: {len(all_trades())}\n"
                 "<i>لا تحذف هالرسالة — البوت يرجّع منها السجل إذا انعاد تشغيله.</i>"))
    if not res:
        return False
    mid = int(res["message_id"])
    tg.pin(chat, mid)
    # Remember where the pin is, so a restore reads the copy we actually wrote
    # even if the default chat ever changes.
    try:
        PIN_PATH.write_text(json.dumps({"chat": str(chat), "message_id": mid}))
    except OSError:
        pass
    return True


def _pinned_chats() -> list[str]:
    """Chats to look in for the backup, most specific first."""
    chats: list[str] = []
    if PIN_PATH.exists():
        try:
            rec = json.loads(PIN_PATH.read_text())
            if rec.get("chat"):
                chats.append(str(rec["chat"]))
        except (ValueError, OSError):
            pass
    default = _backup_chat()
    if default not in chats:
        chats.append(default)
    return chats


def restore_journal(tg: Telegram) -> int:
    """Restore from the pinned backup. Never touches a non-empty journal."""
    if all_trades():
        return 0
    raw = None
    for chat in _pinned_chats():
        try:
            res = tg.call("getChat", {"chat_id": chat}).get("result") or {}
        except Exception:  # noqa: BLE001
            continue
        doc = ((res.get("pinned_message") or {}).get("document")) or {}
        if doc.get("file_name") != BACKUP_FILE:
            continue
        raw = tg.file_bytes(doc.get("file_id") or "")
        if raw:
            break
    if not raw:
        return 0
    try:
        blob = json.loads(raw.decode())
    except ValueError:
        return 0
    n = 0
    with closing(_conn()) as c:
        for t in blob.get("trades") or []:
            c.execute(
                "INSERT INTO trades (symbol,direction,entry,stop,target,size,"
                "opened_at,note,status,result,exit_price,r_multiple,closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.get("symbol") or SYMBOL, t.get("direction"), t.get("entry"),
                 t.get("stop"), t.get("target"), t.get("size"),
                 t.get("opened_at") or _utcnow(), t.get("note"),
                 t.get("status") or "open", t.get("result"),
                 t.get("exit_price"), t.get("r_multiple"), t.get("closed_at")))
            n += 1
        c.commit()
    allow = blob.get("allow") or []
    if allow and not ALLOW_PATH.exists():
        save_allow(set(allow) | set(SEED_CHATS))
    return n


def backup_loop(tg: Telegram, poll_s: int = 60,
                stop: threading.Event | None = None) -> None:
    """Upload only when the journal file changed: a stat every minute is cheap,
    and an idle bot never re-uploads, so the chat stays quiet."""
    last = None
    while not (stop and stop.is_set()):
        try:
            # Watch the allow list too: a new subscriber must reach the durable
            # copy even if no trade has happened since -- otherwise the pinned
            # snapshot quietly lags behind who actually gets the messages.
            stamp = (DB_PATH.stat().st_mtime if DB_PATH.exists() else 0.0,
                     ALLOW_PATH.stat().st_mtime if ALLOW_PATH.exists() else 0.0)
            if stamp != last:
                if backup_journal(tg):
                    last = stamp
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(30, poll_s))


def action_for(text: str | None) -> str:
    first = (text or "").strip().split()[0] if (text or "").strip() else ""
    if first in TRADE_VERBS:
        return "trade"
    if first in CLOSE_VERBS:
        return "close"
    return TEXT_ACTIONS.get(first.lower()) or TEXT_ACTIONS.get(first) or "menu"


def handle(tg: Telegram, upd: dict, allow: set[str]) -> str | None:
    cq = upd.get("callback_query")
    msg = upd.get("message")
    if cq is not None:
        data = cq.get("data") or ""
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
        cid = cq.get("id", "")
        # A close button carries its trade id ("close:4"); a nav button is a
        # plain action name. Both flow through `text` so the close parser sees
        # the id the same way it sees a typed "سكر 4".
        action = "close" if data.startswith("close:") else data
        text = data
    elif msg is not None:
        chat = str((msg.get("chat") or {}).get("id") or "")
        text = msg.get("text")
        action = action_for(text)
        cid = ""
    else:
        return None
    if not chat:
        return None
    if chat not in allow:
        allow.add(chat)
        save_allow(allow)
        tg.send(chat, "👋 أهلاً بك في مكتب التحليل — تمت إضافتك.\n"
                      "رح يوصلك كل شي: الصفقات، الإغلاقات، والتوصيات.\n"
                      "اضغط «❓ مساعدة» لتعرف أكثر.")
        # Bring the newcomer up to date at once: the desk only speaks on an
        # event, so without this a new chat would wait for the next trade to
        # see anything at all.
        try:
            if open_trades():
                tg.send(chat, card_open(), keyboard_for("open"))
        except Exception:  # noqa: BLE001
            pass
        action = "menu"
    if cid:
        tg.answer(cid, "جارٍ التنفيذ…")
    try:
        if action == "trade":
            out = record_trade(text)
        elif action == "close":
            sel = _close_id_or_hint(text)
            out = sel if isinstance(sel, str) else close_trade(sel)
        elif action == "who":
            out = card_who(tg)
        else:
            out = dispatch(action)
    except Exception as exc:  # noqa: BLE001 - one bad action must not kill the loop
        out = f"⚠️ تعذّر التنفيذ ({esc(exc.__class__.__name__)})."
    tg.send(chat, out, keyboard_for(action))
    return action


def poll_forever(tg: Telegram, stop: threading.Event | None = None) -> None:
    allow = load_allow()
    off = load_offset()
    while not (stop and stop.is_set()):
        try:
            ups = tg.updates(off, timeout=30)
        except Exception:  # noqa: BLE001 - a network hiccup must not end the loop
            time.sleep(2)
            continue
        for u in ups:
            # One malformed update must never take the whole poller down: that
            # is the difference between a bot that misses one tap and a bot
            # that is silent until the next deploy.
            try:
                handle(tg, u, allow)
            except Exception as exc:  # noqa: BLE001
                print("handle failed:", exc, flush=True)
            off = max(off or 0, int(u["update_id"]) + 1)
        if ups:
            save_offset(off)
        time.sleep(0.5)


def _write_plan(p: dict) -> int:
    return add_trade(p["direction"], p["entry"], p["stop"], p["target"],
                     LOT, note="desk")


def _in_profit(t) -> bool:
    """Is the trade past its entry, in its own direction?

    The cushion that licenses a second trade in the same direction: stacking on
    a position that has not moved would just double the same bet at the same
    price, which is exactly the risk this gate exists to stop.
    """
    d = 1 if t["direction"] == "long" else -1
    try:
        live = float(quote().get("mid") or 0)
    except Exception:  # noqa: BLE001
        return False
    return (live - t["entry"]) * d > 0


def _winning(t) -> bool:
    """Has the trade travelled half-way to its target?

    Past that the move is mature: a fresh entry only chases it, so one position
    is enough and a second same-direction trade is refused.
    """
    span = (t["target"] - t["entry"]) if t["target"] is not None else 0
    if not span:
        return False
    d = 1 if t["direction"] == "long" else -1
    try:
        live = float(quote().get("mid") or 0)
    except Exception:  # noqa: BLE001
        return False
    # Progress is signed; the goal is a distance, so compare against |span|.
    # (Against the signed span, a short at a small LOSS scored as winning.)
    return (live - t["entry"]) * d >= abs(span) * 0.5


def _open_card(p: dict, t=None) -> str:
    e = ("⚡ صفقة قوية" if p.get("strong") else "🎯 المكتب لقط صفقة")
    head = f"{e} — {SYMBOL}" + (f"  ·  #{t['id']}" if t is not None else "")
    return "\n".join([head] + _plan_lines(p) +
                     ["", "<i>المكتب سجّلها ورح يتابعها لحالها.</i>"])


def _close_card(t) -> str:
    d = "شراء 🟢" if t["direction"] == "long" else "بيع 🔴"
    icon, verdict = {
        "target": ("🏁", "✅ <b>الهدف</b> — صفقة رابحة"),
        "stop": ("🛑", "❌ <b>الوقف</b> — صفقة خاسرة"),
        "manual": ("🔒", "🔒 <b>أُغلقت يدوياً</b>"),
    }.get(t["result"], ("⚪", f"⚪ <b>{t['result'] or 'مغلقة'}</b>"))
    m = _money(t)
    lines = [f"{icon} <b>انحسمت الصفقة #{t['id']}</b>",
             f"{d} — {verdict}",
             f"دخل {num(t['entry'])} · خرج {num(t['exit_price'])}"]
    if t["r_multiple"] is not None:
        lines.append(f"النتيجة {t['r_multiple']:+.2f}R")
    if m is not None:
        lines.append(f"{m:+,.2f}$ · الحساب ≈ ${ACCOUNT_USD + m:,.0f}")
    return "\n".join(lines)


def close_trade(tid: int, price: float | None = None) -> str:
    """Close an open trade by hand at the live price.

    The desk closes its own trades at the stop or the target; this is the
    owner's override -- the way to cut a doubled position. It is priced the
    desk's way (R against the planned risk) and recorded as 'manual', so the
    record never pretends a hand-close was a stop or a target.
    """
    rows = [t for t in all_trades() if t["id"] == tid]
    if not rows:
        return f"ما في صفقة رقم #{tid}."
    t = rows[0]
    if t["status"] != "open":
        return f"الصفقة #{tid} مسكّرة أصلاً."
    if price is None:
        try:
            price = float(quote().get("mid") or 0)
        except Exception:  # noqa: BLE001
            price = 0.0
    if price <= 0:
        return "ما قدرت أقرأ السعر — جرّب كمان شوي."
    risk = _risk(t)
    d = 1 if t["direction"] == "long" else -1
    r = ((price - t["entry"]) * d / risk) if risk else None
    _settle(tid, "manual", price, r)
    return _close_card(next(x for x in all_trades() if x["id"] == tid))


def _close_id_or_hint(text: str) -> int | str:
    """The trade id in a close command, or a hint naming the open trades.

    The id may come from a typed command ("سكر 4") or a button ("close:4"), so
    it is read as the first number anywhere in the text. With one open trade
    the id may be omitted; with several it must be given, and the hint lists
    them so the owner can just send the number back.
    """
    m = re.search(r"\d+", text or "")
    if m:
        return int(m.group())
    op = open_trades()
    if not op:
        return "ما في صفقة مفتوحة لسكّرها."
    if len(op) == 1:
        return op[0]["id"]
    return ("في أكثر من صفقة مفتوحة — حدّد الرقم: "
            + " · ".join(f"#{t['id']}" for t in op))


def _no_second(p: dict, t, why: str) -> str:
    d = "شراء" if p["direction"] == "long" else "بيع"
    frm = "شراء 🟢" if t["direction"] == "long" else "بيع 🔴"
    tail = {
        "full": (f"المكتب بيحمل <b>صفقة وحدة</b> بأي وقت — "
                 f"#{t['id']} {frm} لسا مفتوحة."),
        "weak": (f"الإشارة مو قوية كفاية للتانية (بدها 5/6) — "
                 f"#{t['id']} {frm} لسا مفتوحة."),
        "flat": (f"الأولى #{t['id']} {frm} لسا عند الدخول — "
                 "التانية بتضاعف نفس الرهان بنفس السعر."),
        "mature": (f"الأولى #{t['id']} {frm} مشت نص الطريق — "
                   "التانية بتلحق السعر بس."),
    }[why]
    rule = {
        "full": "قاعدة: صفقة وحدة بأي وقت — سكّر الأولى قبل ما تفتح تانية.",
        "weak": "قاعدة: التانية بدها إشارة قوية (5/6 فريمات).",
        "flat": "قاعدة: زد على رابح، مو على متعادل.",
        "mature": "قاعدة: زد على رابح، مو على سعر راح.",
    }[why]
    return "\n".join([
        f"⚠️ <b>إشارة {d} جديدة</b> — بس {tail}",
        "🛑 <b>ما دخلنا.</b>",
        f"<i>{rule}</i>"])


def desk_is_silenced() -> bool:
    """The kill switch. DESK_AUTO=0 (or off/no/false) stops the desk from
    opening trades and narrating, without touching anything else -- the owner
    can silence the desk from the Render dashboard in seconds."""
    return os.environ.get("DESK_AUTO", "1").strip().lower() in \
        ("0", "off", "no", "false")


def support_watch(seen: dict) -> str | None:
    """The dedicated heads-up for the desk's watched support.

    Returns one message on a STATE CHANGE -- approaching, bounced away, or a
    confirmed break (a 15m close under the level) -- and None otherwise. The
    state lives in `seen` so the same fact is never repeated; a standing "you
    are near 4076" every two minutes is exactly the spam the desk must avoid.

    The watched level is LATCHED in seen['support_level']. That matters: the
    desk's nearest support is recomputed from live price, so the moment price
    drops under 4076 the "nearest support" silently becomes 4054 -- and a break
    of 4076 would never be seen. Latching keeps the alert aimed at the level the
    owner is actually watching until it resolves (bounce, or break + move on).
    """
    try:
        m = read_market()
        live = m["live"]
        res, sup = _levels(live)
        level = seen.get("support_level")
        if level is None:
            if not sup:
                return None
            level = sup[0]
            seen["support_level"] = level
        b = bars("15m", 60)
        closed = [x for x in b if not x.get("isOpen")]
        close = float((closed[-1] if closed else b[-1])["close"])
        prev = seen.get("support")

        if close < level - SUPPORT_BREAK_PTS:
            if prev != "broken":
                seen["support"] = "broken"
                return ("🚨 <b>كسر الدعم</b> — XAUUSD\n"
                        f"شمعة 15د أغلقت <b>{num(close)}</b> تحت الدعم "
                        f"<b>{num(level)}</b>.\nالسعر {num(live)} · "
                        "<i>الدعم انكسر — احتمال استمرار الهبوط. "
                        "لا تشتري هنا؛ استنى يرجع فوقه.</i>")
            # already reported: move the watch to the next support below
            if sup and sup[0] < level:
                seen["support_level"] = sup[0]
                seen["support"] = None
            return None

        if prev == "broken" and close > level:
            seen["support"] = "bounced"
            return ("✅ <b>رجع فوق الدعم</b> — XAUUSD\n"
                    f"إغلاق 15د <b>{num(close)}</b> فوق الدعم "
                    f"<b>{num(level)}</b>.\n<i>ارتداد محتمل — "
                    "راقب تأكيد الصعود.</i>")

        if abs(live - level) <= SUPPORT_NEAR_PTS:
            if prev != "near":
                seen["support"] = "near"
                return ("👀 <b>السعر قرب الدعم</b> — XAUUSD\n"
                        f"السعر <b>{num(live)}</b> · الدعم <b>{num(level)}</b> "
                        f"(باقي {live - level:+.1f} نقطة).\n"
                        "<i>لو ارتد من هنا = فرصة شراء. لو كسر بإغلاق = خطر.</i>")
            return None

        if live > level + SUPPORT_NEAR_PTS and prev == "near":
            seen["support"] = "far"
            return ("↗️ <b>ابتعاد عن الدعم</b> — XAUUSD\n"
                    f"السعر <b>{num(live)}</b> رجع فوق الدعم "
                    f"<b>{num(level)}</b>.\n<i>ارتداد ✅</i>")

        # price has moved well above a stale watch: aim at the nearest support
        if live > level + SUPPORT_NEAR_PTS * 4 and sup and sup[0] != level:
            seen["support_level"] = sup[0]
            seen["support"] = None
        return None
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None


def levels_watch(seen: dict) -> str | None:
    """Announce a crossing of ANY key level in WATCH_LEVELS.

    Returns one message the first time price clears a level by LEVEL_BAND in
    either direction, and None otherwise. The side of each level is kept in
    `seen['levels']` so a level that keeps holding never repeats, and a price
    hovering on a level (inside the band) does not flicker. The first call
    records the starting sides silently -- a break that already happened is not
    a new event.

    This is the multi-level sibling of support_watch: that one follows the
    desk's own latched support, this one follows a fixed set of pivots so the
    owner hears about every level he asked to watch, not only the nearest.
    """
    try:
        live = float(read_market()["live"])
        sides: dict = seen.get("levels")
        first = sides is None
        if first:
            sides = {}
        # support_watch (run just before this one) already narrates the desk's
        # own latched support with richer states (near / bounce / 15m break).
        # A fixed level that lands on it would deliver the same break twice, so
        # its side is still tracked but its event is left to support_watch.
        latched = seen.get("support_level")
        crossed: list[tuple[float, str]] = []
        for lvl in WATCH_LEVELS:
            k = f"{lvl:.2f}"
            prev = sides.get(k, "above" if live > lvl else "below")
            if live >= lvl + LEVEL_BAND:
                now = "above"
            elif live <= lvl - LEVEL_BAND:
                now = "below"
            else:
                now = prev
            sides[k] = now
            if not first and now != prev:
                if latched is not None and abs(lvl - latched) <= SUPPORT_NEAR_PTS:
                    continue
                crossed.append((lvl, now))
        seen["levels"] = sides
        if not crossed:
            return None
        return "\n\n".join(_level_cross_msg(lvl, side, live) for lvl, side in crossed)
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None


def _level_cross_msg(level: float, side: str, live: float) -> str:
    if side == "above":
        return ("🚀 <b>اختراق مستوى</b> — XAUUSD\n"
                f"السعر <b>{num(live)}</b> طلع فوق <b>{num(level)}</b>.\n"
                "<i>مقاومة اتكسرت — راقب التأكيد قبل الدخول.</i>")
    return ("⚠️ <b>كسر مستوى</b> — XAUUSD\n"
            f"السعر <b>{num(live)}</b> نزل تحت <b>{num(level)}</b>.\n"
            "<i>دعم انكسر — احذر، راقب التأكيد.</i>")


def gate_watch(seen: dict) -> str | None:
    """The green light: tell the owner when the desk's own entry gate opens.

    The desk only goes long with price above the 15m EMA20 (frame_agrees). So
    the interesting event is not "price touched 4103" but "a 15m candle CLOSED
    on the other side of the EMA the desk itself reads". This watches the EMA,
    not a frozen number, so the alert stays true as the EMA drifts.

    Fires once per crossing (up or down), keyed in `seen`; a repeat of the same
    side is silent. Returns None on any read error -- a watch never kills the
    loop.
    """
    try:
        b = bars(TREND_FILTER_TF, 300)
        closed = [x for x in b if not x.get("isOpen")]
        if not closed:
            return None
        e = ema([x["close"] for x in closed], 20)
        if e is None:
            return None
        close = float(closed[-1]["close"])
        live = read_market()["live"]
        side = "above" if close > e + GATE_ARM_PTS else (
            "below" if close < e - GATE_ARM_PTS else "inside")
        if side == "inside":
            return None
        prev = seen.get("gate")
        if prev is None:
            # First look after boot is an observation, not an event: seed the
            # state silently so the owner is not told the gate "opened" when the
            # bot merely restarted on the same side it was already on.
            seen["gate"] = side
            return None
        if prev == side:
            return None
        seen["gate"] = side
        tf_ar = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
                 "15m": "15د", "5m": "5د"}.get(TREND_FILTER_TF, TREND_FILTER_TF)
        if side == "above":
            return ("🟢 <b>باب الشراء انفتح</b> — XAUUSD\n"
                    f"شمعة {tf_ar} أغلقت <b>{num(close)}</b> فوق "
                    f"EMA20 (<b>{num(e)}</b>).\nالسعر {num(live)} · "
                    "<i>الفلتر صار يوافق الشراء — إذا الاتجاه العام صاعد، "
                    "هي فرصة الدخول.</i>")
        return ("🔴 <b>باب البيع انفتح</b> — XAUUSD\n"
                f"شمعة {tf_ar} أغلقت <b>{num(close)}</b> تحت "
                f"EMA20 (<b>{num(e)}</b>).\nالسعر {num(live)} · "
                "<i>الفلتر صار يوافق البيع — إذا الاتجاه العام هابط، "
                "هي فرصة الدخول.</i>")
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None


def trade_watch(seen: dict) -> str | None:
    """Narrate an open trade as it approaches its stop or its target.

    The desk already speaks when a trade OPENS and when it CLOSES, but a trade
    can sit for hours and the owner hears nothing. This closes that gap with at
    most two messages per trade:

      * "قرب الهدف" once, when price is within NEAR_TARGET_FRAC of the
        entry->target span -- so the owner can decide to take it or let it run.
      * "قرب الوقف" once, when price is within NEAR_STOP_FRAC of the
        entry->stop span -- so the owner is warned before it hits.

    Keyed per trade id in `seen`, so each message fires once per trade and a
    quiet trade stays quiet. Returns None when there is nothing to say or on any
    read error (a watch never kills the loop).
    """
    try:
        op = open_trades()
        if not op:
            return None
        live = float(quote().get("mid") or 0)
        for t in op:
            if not live or t["target"] is None:
                continue
            d = 1 if t["direction"] == "long" else -1
            entry = float(t["entry"])
            stop = float(t["stop"])
            tgt = float(t["target"])
            span_gain = abs(tgt - entry)
            span_risk = abs(entry - stop)
            if span_gain <= 0 or span_risk <= 0:
                continue
            # Distance still to travel, in the trade's own direction.
            to_tgt = (tgt - live) * d
            to_stop = (live - stop) * d
            dir_txt = "شراء 🟢" if d == 1 else "بيع 🔴"
            if to_tgt <= span_gain * NEAR_TARGET_FRAC:
                key = f"tgt:{t['id']}"
                if seen.get(key) != "said":
                    seen[key] = "said"
                    r_now = (live - entry) * d / span_risk
                    return (f"🎯 <b>قرب الهدف — صفقة #{t['id']}</b>\n"
                            f"{dir_txt} · السعر {num(live)} · الهدف {num(tgt)} "
                            f"(باقي {abs(to_tgt):.1f} نقطة)\n"
                            f"حالياً {r_now:+.2f}R · "
                            "<i>لك الخيار: خذ الربح الآن، أو خلّي الهدف يشتغل.</i>")
            if to_stop <= span_risk * NEAR_STOP_FRAC:
                key = f"stop:{t['id']}"
                if seen.get(key) != "said":
                    seen[key] = "said"
                    r_now = (live - entry) * d / span_risk
                    return (f"⚠️ <b>قرب الوقف — صفقة #{t['id']}</b>\n"
                            f"{dir_txt} · السعر {num(live)} · الوقف {num(stop)} "
                            f"(باقي {abs(to_stop):.1f} نقطة)\n"
                            f"حالياً {r_now:+.2f}R · "
                            "<i>السوق ضد الصفقة. قرّر: تخرج يدوي بجزء من الخسارة، "
                            "أو تخلّي الوقف يشتغل.</i>")
        return None
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None


def desk_tick(tg: Telegram, allow: set[str], seen: dict) -> None:
    """One cycle: settle, announce, and maybe open. Split from the loop so it
    can be tested without a network or a live clock."""
    # Re-read the allow list every cycle: a chat added by the poller (someone
    # new typing /start) must receive the desk's trades, and this set is a
    # snapshot taken at boot -- without this the new chat gets a greeting and
    # then silence forever.
    allow |= load_allow()
    grade()
    # The support watch runs before the silence switch: a level breaking is a
    # fact about the market, not a trade decision, so the owner still hears it
    # when auto-trading is off.
    note = support_watch(seen)
    if note:
        for c in allow:
            tg.send(c, note)
    note = levels_watch(seen)
    if note:
        for c in allow:
            tg.send(c, note)
    note = gate_watch(seen)
    if note:
        for c in allow:
            tg.send(c, note)
    for t in all_trades():
        st = t["status"]
        if st != seen.get(t["id"], "open"):
            seen[t["id"]] = st
            # A manual close already sent its card from the button handler;
            # announcing it here too would deliver the same closure twice.
            if st != "open" and t["result"] != "manual":
                for c in allow:
                    tg.send(c, _close_card(t))
    # In-trade monitoring runs AFTER settle, so it never narrates a trade that
    # this same cycle just closed.
    note = trade_watch(seen)
    if note:
        for c in allow:
            tg.send(c, note)
    if desk_is_silenced():
        return
    p = plan()
    if p is None:
        return
    op = open_trades()
    conflicts = [t for t in op if t["direction"] == p["direction"]]
    t0 = conflicts[0] if conflicts else (op[0] if op else None)
    why = None
    if len(op) >= MAX_OPEN:
        # The book is full: that is the whole reason, and it does not drift as
        # the price moves, so one message stands until a trade closes.
        why = "full"
    elif conflicts:
        if not _in_profit(t0):
            why = "flat"
        elif _winning(t0):
            why = "mature"
        elif not p["strong"]:
            why = "weak"
    if why is not None:
        # Speak once per open trade per reason: a standing refusal is not a
        # change, and repeating it every cycle is the spam the desk must avoid.
        key = f"refuse:{t0['id']}"
        if seen.get(key) != why:
            seen[key] = why
            for c in allow:
                tg.send(c, _no_second(p, t0, why))
    else:
        tid = _write_plan(p)
        row = next(x for x in all_trades() if x["id"] == tid)
        seen[tid] = "open"
        for c in allow:
            tg.send(c, _open_card(p, row))


def desk_loop(tg: Telegram, allow: set[str], every_min: int = 2,
              stop: threading.Event | None = None) -> None:
    """The desk that trades its own book, then narrates.

    Every cycle runs desk_tick: settle first, so a book that just cleared a
    trade may open a fresh one the same minute; then decide and, if a plan is
    valid, open it and tell the owner. The loop speaks only on an event -- an
    opened or closed trade -- so a quiet market stays quiet. Every auto-opened
    trade carries a stop, so it is graded exactly like the owner's own.
    """
    seen: dict = {}
    while not (stop and stop.is_set()):
        try:
            desk_tick(tg, allow, seen)
        except Exception as exc:  # noqa: BLE001 - the desk must not die mid-cycle
            print("desk_loop:", exc, flush=True)
        time.sleep(max(30, every_min * 60))


# ========================================================================
# entry points
# ========================================================================
def _supervise(name: str, fn, args: tuple,
               stop: threading.Event | None) -> None:
    """Run a loop and restart it if it ever dies.

    A host that gives no shell (Render) cannot restart a dead thread itself, so
    each loop is wrapped here: a thread that exits silently turns "the bot
    ignored me" into a mystery. Hosts that restart the process (Fly, a VPS) do
    not need this and start the loops directly.
    """
    while not (stop and stop.is_set()):
        try:
            fn(*args)
            print(f"{name} returned; restarting in 5s", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{name} crashed: {exc!r}; restarting in 5s", flush=True)
        if stop is not None:
            stop.wait(5)
        else:
            time.sleep(5)


def start_desk(tg: Telegram, stop: threading.Event | None = None,
               supervise: bool = False) -> list[threading.Thread]:
    """Start the desk's background threads. THE one place they are listed.

    Every way of running the desk -- a VPS, Fly, or Render's web wrapper -- goes
    through here, so no entry point can forget a thread and quietly run a
    different bot. That bug already existed: `bot.py`'s own main() started only
    the trade monitor and never the auto-desk, so the "permanent bot for a VPS"
    in the README would watch trades and never open one.

    The desk trades its own book and narrates (this is the auto-trader, and it
    also does the trade monitoring); the backup keeps the journal in its pinned
    Telegram message. The POLLER is deliberately NOT here: the host starts it
    itself, because a VPS wants it in the main thread so Ctrl+C stops the bot,
    while Render wants it supervised like the rest. `supervise` wraps each loop
    in a restarter for a host that gives no shell (Render); a host that can
    restart the process itself (Fly, a VPS) leaves it off.
    """
    allow = load_allow()
    jobs = (("desk", desk_loop, (tg, allow, 2, stop)),
            ("backup", backup_loop, (tg, 60, stop)))
    threads: list[threading.Thread] = []
    for name, fn, args in jobs:
        target = (lambda n=name, f=fn, a=args: _supervise(n, f, a, stop)) \
            if supervise else fn
        t = threading.Thread(target=target, args=() if supervise else args,
                             name=name, daemon=True)
        t.start()
        threads.append(t)
    return threads


def token_state() -> str:
    """One line on the token, for --selftest. Proves reachability, not presence.

    A stale TELEGRAM_BOT_TOKEN is a non-empty string that 404s on every send, so
    a truthiness check printed "ok" for a dead token -- the diagnostic hid the
    very bug it existed to catch. MISSING / ok (@name) / DEAD are the only three
    honest answers.
    """
    t = token()
    if not t:
        return "MISSING"
    ident = token_identity(t)
    return f"ok (@{ident})" if ident else "DEAD (has a value, but getMe fails)"


def selftest() -> int:
    print("== token ==", token_state())
    print("== quote ==", quote().get("mid"), quote().get("marketState"))
    print("== frames ==", {tf: len(bars(tf, 50)) for tf in FRAMES})
    print("== analysis ==", card_analysis()[:120].replace("\n", " "))
    print("== recommend ==", card_recommend()[:120].replace("\n", " "))
    print("== levels ==", card_levels()[:120].replace("\n", " "))
    print("== map ==", card_map()[:120].replace("\n", " "))
    print("== news ==", card_news()[:120].replace("\n", " "))
    print("== scenarios ==", card_scenarios()[:120].replace("\n", " "))
    print("== journal ==", journal_menu_text().replace("\n", " | "))
    print("== open ==", card_open()[:80].replace("\n", " "))
    print("== action ==", card_action().replace("\n", " | ")[:160])
    print("== alerts ==", alerts_guide().replace("\n", " | ")[:100])
    sw = support_watch({})
    print("== support ==", (sw or "silent").replace("\n", " ")[:120])
    print("== gate ==", (gate_watch({}) or "silent").replace("\n", " ")[:120])
    print("== trade parse ==", str(parse_trade("سجل شراء 4075 وقف 4069 هدف 4085"))
          .replace("\n", " ")[:120])
    return 0


def main() -> int:
    args = sys.argv[1:]
    _init_db()
    tok = token()
    if "--selftest" in args:
        return selftest()
    if not tok:
        print("لا يوجد توكن — ضع التوكن في .tg_token", file=sys.stderr)
        return 2
    tg = Telegram(tok)
    if "--backup" in args:
        n = restore_journal(tg)
        ok = backup_journal(tg)
        print(f"restored {n}, backed up: {ok}, trades: {len(all_trades())}")
        return 0 if ok else 1
    if "--menu" in args:
        ok = tg.send(SEED_CHATS[0], dispatch("menu"), kb(MENU))
        print("menu published:", ok)
        return 0 if ok else 1
    if "--send" in args:
        allow = load_allow()
        for c in allow:
            print(c, tg.send(c, dispatch("menu"), kb(MENU)))
        return 0
    print("polling @Ayzr1324_bot …")
    restored = restore_journal(tg)
    if restored:
        print(f"restored {restored} trades from the Telegram backup")
    stop = threading.Event()
    start_desk(tg, stop)
    auto = "off" if desk_is_silenced() else "on"
    print(f"marketdesk: auto-trade {auto}", flush=True)
    try:
        # The poller runs in the main thread so Ctrl+C stops the bot; the desk
        # and the backup were started as daemons by start_desk above.
        poll_forever(tg, stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
