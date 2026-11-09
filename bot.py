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
from contextlib import closing
from datetime import UTC, datetime, timedelta
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
ACCOUNT_USD = 200.0
LOT = 0.01
POINT_VALUE = LOT * 100.0        # $ per point at this lot
STOP_MIN_USD = 8.0               # the owner's window: lose $8-10 at worst
STOP_MAX_USD = 10.0
TARGET_MIN_USD = 15.0            # ...and take $15-20
TARGET_MAX_USD = 20.0
STOP_PICK_USD = 9.0              # aim mid-window; the nearest level may move it
TARGET_RR = 2.0                  # 9 * 2 = $18, inside the window
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
# The support watch: the desk's own nearest support (from _levels) gets a
# dedicated heads-up -- approaching, bounced, or broken -- so the owner hears
# about the level he cares about, not only when a whole plan appears.
SUPPORT_NEAR_PTS = 6.0           # "approaching" band around the level, points
SUPPORT_BREAK_PTS = 3.0          # a 15m close this far under = broken
# The green-light watch: the desk's own entry filter is "a long needs price
# above the 15m EMA20". So rather than hard-code the number 4103 (which the
# EMA moves away from within the hour), watch the EMA itself -- the owner hears
# when the desk's OWN gate opens, not when an arbitrary price is touched.
GATE_ARM_PTS = 1.5               # close this far above/below the EMA = a signal


# ---- menus --------------------------------------------------------------
MENU = [
    [("📊 تحليل الآن", "analysis"), ("🎯 التوصية", "recommend")],
    [("🎬 السيناريوهات", "scenarios"), ("🔔 المستويات", "levels")],
    [("📂 الصفقات المفتوحة", "open"), ("📒 السجل", "journal_menu")],
    [("👥 المشتركين", "who"), ("➕ سجّل صفقة", "howto")],
    [("❓ مساعدة", "help")],
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


def token() -> str:
    """The bot token. The local `.tg_token` file wins over the environment: a
    stale TELEGRAM_BOT_TOKEN injected into the environment once pointed at a
    different, dead bot and silently shadowed the right one, so every send 404'd
    while every read looked fine. The file is the operator's explicit choice."""
    if TOKEN_PATH.exists():
        t = TOKEN_PATH.read_text().strip()
        if t:
            return t
    return os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""


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
    """One read of the tape: quote plus every frame's trend and ATR."""
    q = quote()
    live = float(q.get("mid") or q.get("last") or 0)
    trends, atrs = {}, {}
    for tf in FRAMES:
        try:
            b = bars(tf, 300)
            trends[tf] = frame_trend(b)
            atrs[tf] = atr(b)
        except Exception:  # noqa: BLE001 - one missing frame must not kill the read
            trends[tf] = 0
            atrs[tf] = None
    return {"q": q, "live": live, "trends": trends, "atrs": atrs}


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


def card_analysis() -> str:
    m = read_market()
    q, live, trends = m["q"], m["live"], m["trends"]
    state = "مفتوح ✅" if market_open(q) else "مسكّر ⛔"
    age = q.get("quoteAgeSeconds")
    age_s = f"{int(age)//60} دقيقة" if isinstance(age, (int, float)) else "—"
    names = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
             "15m": "15د", "5m": "5د"}
    icon = {1: "🟢", -1: "🔴", 0: "⚪"}
    lines = [f"📊 <b>تحليل {SYMBOL}</b>",
             f"السعر <b>{num(live)}</b> · السوق {state} · عمر التسعيرة {age_s}"]
    if q.get("dayDiffPercent") is not None:
        lines.append(f"تغيّر اليوم {q['dayDiffPercent']}%")
    lines.append("")
    lines.append("<b>الميل حسب الفريم:</b>")
    row = " · ".join(f"{names[tf]}:{icon[trends.get(tf,0)]}" for tf in FRAMES)
    lines.append(row)
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    lean, mark = _bias(trends)
    if up >= CONFIRM_FRAMES:
        lines.append(f"\nالميل العام: <b>صاعد</b> ({up}/6 فريمات)")
        if not frame_agrees("long", live):
            lines.append(f"<i>⚠️ بس السعر تحت EMA20 ({TREND_FILTER_TF}) — "
                         "المكتب ما يشتري والزخم هابط.</i>")
    elif dn >= CONFIRM_FRAMES:
        lines.append(f"\nالميل العام: <b>هابط</b> ({dn}/6 فريمات)")
        if not frame_agrees("short", live):
            lines.append(f"<i>⚠️ بس السعر فوق EMA20 ({TREND_FILTER_TF}) — "
                         "المكتب ما يبيع والزخم صاعد.</i>")
    else:
        lines.append(f"\nالميل العام: {mark} <b>{lean}</b> "
                     f"(صاعد {up} · هابط {dn})")
        lines.append("<i>ميل بالقراءة — مو صفقة. الصفقة بتحتاج "
                     f"{CONFIRM_FRAMES}/6 فريمات متوافقة.</i>")
    lines.append("\n<i>قراءة تحليلية، مو وعد ربح.</i>")
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
    return "\n".join(lines)


def _sized(entry: float, direction: str, stop_level: float | None) -> dict:
    """Size one trade from `entry`, the desk's single set of rules.

    Risk sits inside the owner's $8-10 window and is tightened by `stop_level`
    when that level is close enough to matter. The target is a RISK MULTIPLE,
    never the next level: the old scenario card aimed at the next level, which
    can sit a point away and quote a trade at R:R 0.16 -- a trade this desk's
    own floor would refuse. Here the R:R is the target multiple by construction
    and can never fall under MIN_RR. plan() and the scenario card both call
    this, so no surface can quote a trade the desk would reject.
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
    rr = max(TARGET_RR, MIN_RR)
    tgt = entry + d * pts * POINT_VALUE * rr
    risk_usd = abs(entry - stop) * POINT_VALUE
    gain_usd = abs(tgt - entry) * POINT_VALUE
    return {"direction": direction, "entry": entry, "stop": stop,
            "target": tgt, "rr": rr, "risk_usd": risk_usd,
            "risk_pts": risk_usd / POINT_VALUE, "gain_usd": gain_usd}


def frame_agrees(direction: str, live: float) -> bool:
    """Does the live price agree with `direction` on the filter frame?

    A long wants price ABOVE the frame's EMA20, a short BELOW it. A missing
    frame or an unreadable EMA20 returns True: the filter is a guard, and it
    must never be the reason the desk stops trading entirely.
    """
    if not TREND_FILTER_TF:
        return True
    try:
        b = bars(TREND_FILTER_TF, 300)
        e = ema([x["close"] for x in b], 20)
        if e is None:
            return True
        return live > e if direction == "long" else live < e
    except Exception:  # noqa: BLE001
        return True


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
    if up >= CONFIRM_FRAMES and frame_agrees("long", live):
        return sized("long")
    if dn >= CONFIRM_FRAMES and frame_agrees("short", live):
        return sized("short")
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
    p = plan()
    if p is None:
        m = read_market()
        if not market_open(m["q"]):
            return ("🎯 <b>التوصية</b> — XAUUSD\n<b>لا صفقة</b> — السوق مسكّر.\n\n"
                    "<i>المكتب ما يعطي إشارة على سعر ميت.</i>")
        lean, mark = _bias(m["trends"])
        up = sum(1 for tf in FRAMES if m["trends"].get(tf) == 1)
        dn = sum(1 for tf in FRAMES if m["trends"].get(tf) == -1)
        if up >= CONFIRM_FRAMES and not frame_agrees("long", m["live"]):
            return ("🎯 <b>التوصية</b> — XAUUSD\n"
                    "<b>لا صفقة</b> — الاتجاه صاعد بس السعر تحت "
                    f"EMA20 ({TREND_FILTER_TF}).\n"
                    "<i>المكتب ما يشتري والزخم القصير هابط — استنى يرجع فوق.</i>")
        if dn >= CONFIRM_FRAMES and not frame_agrees("short", m["live"]):
            return ("🎯 <b>التوصية</b> — XAUUSD\n"
                    "<b>لا صفقة</b> — الاتجاه هابط بس السعر فوق "
                    f"EMA20 ({TREND_FILTER_TF}).\n"
                    "<i>المكتب ما يبيع والزخم القصير صاعد — استنى يرجع تحت.</i>")
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


def dispatch(action: str) -> str:
    return {
        "analysis": card_analysis,
        "recommend": card_recommend,
        "scenarios": card_scenarios,
        "levels": card_levels,
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


def monitor_loop(tg: Telegram, allow: set[str], every_min: int = 2,
                 stop: threading.Event | None = None) -> None:
    """Watch open trades and push a follow-up on every status change."""
    last: dict[int, str] = {}
    while not (stop and stop.is_set()):
        try:
            grade()
            live = float(quote().get("mid") or 0)
            for t in open_trades():
                d = 1 if t["direction"] == "long" else -1
                status = "open"
                if t["target"] is not None and (live - t["target"]) * d >= 0:
                    status = "target"
                elif (live - t["stop"]) * d <= 0:
                    status = "stop"
                if last.get(t["id"]) != status:
                    last[t["id"]] = status
                    if status in ("target", "stop"):
                        body = card_monitor()
                        for c in allow:
                            tg.send(c, body)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(30, every_min * 60))


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
        if side == "above":
            return ("🟢 <b>باب الشراء انفتح</b> — XAUUSD\n"
                    f"شمعة {TREND_FILTER_TF} أغلقت <b>{num(close)}</b> فوق "
                    f"EMA20 (<b>{num(e)}</b>).\nالسعر {num(live)} · "
                    "<i>الفلتر صار يوافق الشراء — إذا الاتجاه العام صاعد، "
                    "هي فرصة الدخول.</i>")
        return ("🔴 <b>باب البيع انفتح</b> — XAUUSD\n"
                f"شمعة {TREND_FILTER_TF} أغلقت <b>{num(close)}</b> تحت "
                f"EMA20 (<b>{num(e)}</b>).\nالسعر {num(live)} · "
                "<i>الفلتر صار يوافق البيع — إذا الاتجاه العام هابط، "
                "هي فرصة الدخول.</i>")
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
def selftest() -> int:
    print("== token ==", "ok" if token() else "MISSING")
    print("== quote ==", quote().get("mid"), quote().get("marketState"))
    print("== frames ==", {tf: len(bars(tf, 50)) for tf in FRAMES})
    print("== analysis ==", card_analysis()[:120].replace("\n", " "))
    print("== recommend ==", card_recommend()[:120].replace("\n", " "))
    print("== levels ==", card_levels()[:120].replace("\n", " "))
    print("== scenarios ==", card_scenarios()[:120].replace("\n", " "))
    print("== journal ==", journal_menu_text().replace("\n", " | "))
    print("== open ==", card_open()[:80].replace("\n", " "))
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
    threading.Thread(target=monitor_loop, args=(tg, load_allow(), 2, stop), daemon=True).start()
    threading.Thread(target=backup_loop, args=(tg, 60, stop), daemon=True).start()
    try:
        poll_forever(tg, stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
