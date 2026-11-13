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
# The sticky level pool the live map reads (see _sticky_map). Kept on disk so a
# restart does not lose the levels the owner is watching.
STICKY_PATH = DATA / "levels_seen.json"
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
# The owner's own messages (a trade opened, closed, refused, or watched, plus
# the day's record and the account) go to the owner ALONE by default. The market
# ALERTS (a level breaking, the direction flipping, a gate opening, price
# reaching a zone) stay for everyone allowed -- a guest is on the bot for those.
# DESK_OWNER_ONLY=0 restores the old behaviour: everything to everyone allowed.
OWNER_ONLY = os.environ.get("DESK_OWNER_ONLY", "1").strip().lower() not in \
    ("0", "false", "no", "off", "")
PIN_PATH = DATA / "pin.json"
# The owner's last daily-log date. Persisted so a restart cannot resend a day.
DAILY_PATH = DATA / "daily.json"

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
START_CAPITAL = float(os.environ.get("DESK_START_CAPITAL", str(ACCOUNT_USD)))
LOT = 0.01
POINT_VALUE = LOT * 100.0        # $ per point at this lot
STOP_MIN_USD = 8.0               # the owner's window: lose $8-12 at worst
STOP_MAX_USD = 12.0
TARGET_MIN_USD = 15.0            # ...and take $15-20
TARGET_MAX_USD = 20.0
STOP_PICK_USD = 9.0              # the validated sweet spot; a near level may move it
# The target: risk times this, clamped into the reward window. A 2026 sweep
# (with the LIVE bands, see backtest/bot2026.py) put the whole $18-24 range on
# one plateau -- no single value was clearly best, so this keeps the validated
# 2.2. A WIDER stop, or an ATR-scaled stop, both tested WORSE on the same data.
TARGET_RR = 2.2                  # 9 * 2.2 = $19.8, inside the reward window
MIN_RR = 1.5                     # below this a plan is not worth stating
# The desk's OWN replay over 2026 (the same year as duka_2026.json, the SAME
# rules the desk trades -- see backtest/bot2026.py, which now READS these
# constants instead of hardcoding them). A STUDY figure, never a live reading:
# the owner's journal is the live truth and is used FIRST when it has closed
# trades. The card labels the source so a backtest number is never mistaken for
# a measured one. Measured 2026-11-13 on the shipped config (ADX 0-50, ATR
# 0.08-0.30%, no-chase gate, $9 stop, $20 target): 796 closed trades, 34.9% win,
# +0.118 R/trade, max drawdown -19R, longest losing run 13. The old 37% / 16 came
# from a replay that used ADX 20-40 / ATR 0.25% -- tighter than the live bands --
# and no no-chase gate, so it measured a different bot. (The live stop is the $9
# pick nudged to $8-12 by the nearest level; the replay fixes it at $9, which
# measured the same to 0.002 R.)
STUDY_WIN_RATE = 0.35            # full-year replay; spread makes the live one lower
STUDY_LOSS_STREAK = 13           # longest run of losers in the corrected replay
SHOW_STUDY = os.environ.get("DESK_SHOW_STUDY", "1").strip().lower() not in \
    ("0", "off", "no", "false")
CONFIRM_FRAMES = 4               # a direction needs this many of 6 frames
STRONG_FRAMES = 5                # ...and this many to allow a SECOND trade
MAX_OPEN = 1                     # one trade at a time (raise to 2 to stack)
# A late entry: the frames agree on a trend but the 15m RSI is already
# stretched AGAINST it (selling into a washed-out tape, or buying into an
# overbought one). Measured on the full 2026 sample this is the WORST bucket to
# enter: shorts taken with 15m RSI < 30 win 33% against 37% for the rest, and
# buys taken with RSI > 70 are the worst buys of all. The desk does NOT block
# these -- the trend trade is still valid and blocking it threw away real
# winners -- but it SAYS SO, the way the owner reads the tape himself. This is
# the RSI level past which the entry is "late"; the caveat names it.
CHASE_RSI = 30.0                 # 15m RSI <= this when selling = a late entry
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
ADX_MIN, ADX_MAX = 0.0, 50.0     # ride a trend, but not an exhausted one
# Re-measured 2026-11-13 on the CORRECTED replay (backtest/bot2026.py, the LIVE
# bands + the no-chase gate). Band by band on the short side: ADX 0-15 is the
# one band that loses (-0.03R over 122 trades); every band from 15 up pays, and
# the >50 band is thin. Floor -> 0 still nets the MOST total R (0-50 gives +116R
# against 15-50's +102R and 20-50's +72R) because the extra trades are cheap and
# positive. The floor's "bad band" is small and the extra trades carry it.
# The ATR ceiling is the one that MATTERS, and it interacts with the no-chase
# gate: WITHOUT the gate a wide ceiling nets more total R (0.25% -> +114R, 1.00%
# -> +158R), which is why it was widened; WITH the gate the sign flips (0.25% ->
# +117R, 1.00% -> +91R) and a wide ceiling raises the drawdown. The gate already
# refuses the bad high-volatility entries, so the ceiling need not be as tight.
# Set to 0.30% (owner's call, 2026-11-13): it admits 796 trades for +93.6R at a
# -19R drawdown, against 0.25%'s 661 trades for +116.6R at -16R. The tighter cap
# nets MORE R for LESS drawdown; the owner preferred the extra entries at the
# high-volatility edge (the days the tight cap went silent), accepting the lower
# expectancy. Measured, not asserted: 0.25% -> +0.176 R/trade, 0.30% -> +0.118.
ATR_MIN_PCT, ATR_MAX_PCT = 0.0008, 0.0030   # 0.08%-0.30% of price: alive, calm
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
# The entry must agree with the 15m candle, not just the frame tilt. The switch
# that APPLIES TREND_FILTER_TF in plan(); see that constant for the evidence.
TREND_FILTER_ON = os.environ.get("DESK_TREND_FILTER", "1").strip().lower() not in \
    ("0", "off", "no", "false")
# The daily cap: the owner asked for at most TEN trades a day. Counted on
# trades OPENED today (local session), so a fast day cannot turn into a churn
# of a hundred entries. Set DESK_MAX_TRADES_DAY=0 to remove the cap.
MAX_TRADES_DAY = int(os.environ.get("DESK_MAX_TRADES_DAY", "10") or "10")
# The no-chase filter: do not SELL into deep oversold (or BUY into deep
# overbought) on the filter frame -- that is entering after the move, at the
# edge of the range, where the retrace is most likely. Measured on 2026 with the
# LIVE bands (see backtest/bot2026.py): it halves the trade count and lifts
# R/trade from +0.071 to +0.082 overall (+0.096 -> +0.108 on the short side) and
# cuts the max drawdown from -40R to -25R. The reading is taken on a CLOSED bar
# only: on the forming bar the same rule measured -0.245 R/trade instead of
# +0.108 (it reads a price that has not happened yet).
NOCHASE_ON = os.environ.get("DESK_NOCHASE", "1").strip().lower() not in \
    ("0", "off", "no", "false")
NOCHASE_RSI = float(os.environ.get("DESK_NOCHASE_RSI", "38") or "38")
NOCHASE_TF = os.environ.get("DESK_NOCHASE_TF", "1h").strip()
NOCHASE_SIDES = {s.strip().lower() for s in os.environ.get(
    "DESK_NOCHASE_SIDES", "both").split(",") if s.strip()}
if "both" in NOCHASE_SIDES or not NOCHASE_SIDES:
    NOCHASE_SIDES = {"short", "long"}
# ENTRY TIMING. "immediate" enters at market the moment the signal fires (the
# desk's historical behaviour). "pullback" places a LIMIT PULLBACK_ATR * ATR(15m)
# back from the signal price -- a short sells HIGHER, a long buys LOWER -- and
# waits PULLBACK_WINDOW bars for the retrace, cancelling if it never comes.
# The owner asked to enter the way he reads the tape (sell the bounce, not the
# middle of the range). The evidence is MIXED and the switch stays OFF:
#   * over the full 2026 year (backtest/entry_timing.py, 796 trades) the pullback
#     lifts R/trade from +0.118 to +0.243 and the gain holds in both halves;
#   * but on the owner's REAL week of 38 trades (backtest/week_compare.py) the
#     immediate entry wins by 13.5R (+12.40R vs -1.13R) -- that week was a
#     strong downtrend, where price never retraced and the limit simply missed
#     9 winners worth +16.5R.
# Month by month the pullback wins 7 of 11 months -- the RANGE ones -- and loses
# the strong-trend ones. It is a RANGE tool, not a universal upgrade, so the
# default is "immediate" and nothing changes until the owner turns it on.
ENTRY_MODE = os.environ.get("DESK_ENTRY_MODE", "immediate").strip().lower()
PULLBACK_ATR = float(os.environ.get("DESK_PULLBACK_ATR", "0.5") or "0.5")
PULLBACK_WINDOW = int(os.environ.get("DESK_PULLBACK_WINDOW", "12") or "12")
PENDING_PATH = DATA / "pending.json"
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
# Refreshed 2026-11-11: price fell to ~3987, so the old set (all above 4055) held
# nothing near the price. Added the levels that matter on the way down -- today's
# low 3982.5, the 61.8% fib 3976.5, the nearest real support 3937.5, the 50% fib
# 4009.5 and today's open 4038.5 -- so a break or a bounce at these speaks.
WATCH_LEVELS: tuple[float, ...] = (
    3937.5, 3976.5, 3982.5, 4009.5, 4038.5,
    4055.0, 4070.0, 4084.0, 4096.0, 4108.0,
    4119.0, 4132.0, 4138.0, 4147.0, 4168.0, 4192.0)
_extra = os.environ.get("DESK_WATCH_LEVELS", "").strip()
if _extra:
    WATCH_LEVELS = tuple(sorted(set(WATCH_LEVELS) | {
        float(x) for x in _extra.split(",") if x.strip()}))
LEVEL_BAND = 1.0                 # hysteresis: a crossing needs this much clearance
# How far a LIVE pivot may drift from the price before the watch drops it. The
# frozen set never expires, but live pivots are re-read each cycle; latching
# them within this band keeps a broken level around long enough to speak once.
LIVE_LEVEL_BAND = 25.0           # points; ~2x the 1h ATR on gold
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
# The entry-zone heads-up: speak once as price enters this band around one of
# the desk's buy/sell zones, carrying the trade it implies -- the owner asked
# to hear BEFORE an entry, not after. ~0.15% of the price on gold.
ENTRY_NEAR_PTS = float(os.environ.get("DESK_ENTRY_NEAR_PTS", "6") or "6")


# ---- menus --------------------------------------------------------------
# The news layer is INFORMATION ONLY -- see card_news(). The owner edits this
# list by hand; nothing here can open, block or size a trade. The card also
# pulls fresh headlines from Google News (keyless RSS) -- still information
# only, still never a trade input.
#
# Default ON: the service is created via the Render API (inline envVars), so
# render.yaml does NOT apply to it -- a default of "0" silently turned the live
# news off in production. On by default here means it works with no dashboard
# trip. Set DESK_NEWS_LIVE=0 to show only the hand-edited NEWS_ITEMS list.
NEWS_LIVE = os.environ.get("DESK_NEWS_LIVE", "1").strip().lower() in (
    "1", "true", "yes", "on")
# The news card pulls fresh headlines from Google News (keyless RSS). Arabic by
# default -- the owner reads Arabic. Google News is keyed by locale, so both the
# queries AND the feed's language/region are Arabic (ar / SA).
# The queries are chosen for SIGNAL, measured live: "الذهب الفوري" and "مؤشر
# الدولار" returned zero retail-price noise, while "سعر الذهب" (4/10) and
# "الدولار" (3/10) pulled karat-21 shop prices and the Canadian/Australian
# dollar. The Fed's rate path and US inflation are the real gold drivers.
# Override with DESK_NEWS_QUERY, pipe-separated for more than one query.
NEWS_QUERIES: tuple[str, ...] = tuple(
    q.strip() for q in os.environ.get(
        "DESK_NEWS_QUERY",
        "الذهب الفوري|مؤشر الدولار|الفيدرالي الفائدة|التضخم الأميركي").split("|")
    if q.strip())
NEWS_LANG = os.environ.get("DESK_NEWS_LANG", "ar").strip()
NEWS_REGION = os.environ.get("DESK_NEWS_REGION", "SA").strip()
# Headlines that mention gold or the dollar but do not move XAUUSD: a local
# retail gold price (عيار = karat), a different currency (the Canadian/Australian
# dollar, the Egyptian pound), a jewellery shop. Dropped from the live feed so
# the card stays signal. Override with DESK_NEWS_NOISE, pipe-separated.
NEWS_NOISE: tuple[str, ...] = tuple(
    w.strip() for w in os.environ.get(
        "DESK_NEWS_NOISE",
        "عيار|الكندي|الأسترالي|الاسترالي|النيوزلندي|الجنيه|محلات").split("|")
    if w.strip())
NEWS_CACHE_S = 300               # reuse a fetch for 5 minutes (feeds are slow)
# The automatic direction alert: tell the owner when the desk's net direction
# flips (up / down / range) without a tap. A cooldown damps a chop at the edge.
TREND_ALERTS = os.environ.get("DESK_TREND_ALERTS", "1").strip().lower() in (
    "1", "true", "yes", "on")
TREND_ALERT_COOLDOWN_S = 20 * 60  # at most one direction alert per 20 minutes
# The owner's clock: hours to add to UTC when a time is shown (Syria = 3, and
# Syria has had no DST since 2022, so a fixed offset is exact). Set
# DESK_TZ_OFFSET=0 to show GMT.
TZ_OFFSET_H = int(os.environ.get("DESK_TZ_OFFSET", "3") or "3")
TZ_LABEL = os.environ.get("DESK_TZ_LABEL", "بتوقيت سوريا")
# The two hours the full daily read goes out, local time: morning and evening.
# The owner asked for the analysis "للبدو يصير وليلي" -- daybreak and night.
DAILY_AM_HOUR = int(os.environ.get("DESK_DAILY_AM_HOUR", "9") or "9")
DAILY_PM_HOUR = int(os.environ.get("DESK_DAILY_PM_HOUR", "21") or "21")
NEWS_ITEMS: list[tuple[str, str]] = [
    ("🔥", "اليوم: تقرير التضخم الأميركي (CPI) — أهم حدث بالأسبوع"),
    ("⚠️", "الـCPI رح يطلع بلا نسبة سنوية (تداعيات الإغلاق) — رقم مشوّه"),
    ("🦅", "وظائف قوية → احتمال رفع فايدة بكسمبر >65% (سلبي للذهب)"),
    ("📉", "تضخم متوقع يبطئ → ممكن يدعم اختراق 4,200 (إيجابي)"),
]

MENU = [
    [("🧠 قراءتي للسوق", "full")],
    [("🥇 التحليل التفصيلي", "report")],
    [("🧭 شو تعمل هلّق", "action")],
    [("📅 التحليل اليومي الشامل", "day_analysis")],
    [("📊 تحليل الآن", "analysis"), ("🎯 التوصية", "recommend")],
    [("🗺️ خريطة المناطق", "map"), ("📰 الأخبار", "news")],
    [("🎬 السيناريوهات", "scenarios"), ("🔔 المستويات", "levels")],
    [("📂 الصفقات المفتوحة", "open"), ("📒 السجل", "journal_menu")],
    [("📊 سجل اليوم", "daily"), ("👥 المشتركين", "who")],
    [("➕ سجّل صفقة", "howto"),
     ("🔔 دليل التنبيهات", "alerts"), ("❓ مساعدة", "help")],
    [("✖ طي القائمة", "collapse")],
]

# The collapsed menu: ONE button, bottom-left, shown under every card. The full
# grid above is behind it -- the owner asked for the extra buttons to move into
# a list that is only shown when tapped.
COLLAPSE_BTN = [("☰ القائمة", "menu")]
JOURNAL_MENU = [
    [("📅 السجل اليومي", "j_day"), ("🗓️ السجل الأسبوعي", "j_week")],
    [("📆 السجل الشهري", "j_month"), ("📋 كل الصفقات", "journal")],
    [("🔭 متابعة المفتوحة", "monitor"), ("🏠 القائمة", "menu")],
]

TEXT_ACTIONS = {
    "/start": "menu", "/menu": "menu", "/help": "help",
    "تحليل": "analysis", "توصية": "recommend", "سيناريو": "scenarios",
    "كامل": "full", "شامل": "full", "كل شي": "full", "التحليل الكامل": "full",
    "تفصيلي": "report", "التحليل التفصيلي": "report", "تقرير": "report",
    "التقرير": "report", "تقرير كامل": "report", "تحليلي": "report",
    "سيناريوهات": "scenarios", "مستويات": "levels", "مفتوحة": "open",
    "الصفقات": "open", "سجل": "journal_menu", "السجل": "journal_menu",
    "يومي": "j_day", "اليوم": "j_day", "أسبوعي": "j_week", "اسبوعي": "j_week",
    "شهري": "j_month", "الشهر": "j_month", "متابعة": "monitor",
    "سجل اليوم": "daily", "سجل اليوم؟": "daily",
    "/daily": "day_analysis", "تحليل يومي": "day_analysis",
    "التحليل اليومي": "day_analysis", "قراءة اليوم": "day_analysis",
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

    def edit_markup(self, chat, message_id, markup) -> bool:
        """Replace a message's inline keyboard in place.

        The collapsible menu needs this: folding the list must remove the
        buttons from the message that carries them, not post a new message
        (which is the spam the desk avoids).
        """
        try:
            return bool(self.call("editMessageReplyMarkup",
                                  {"chat_id": chat, "message_id": message_id,
                                   "reply_markup": markup}).get("ok"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError):
            return False

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
    """The keyboard under a normal card: ONE button, at the bottom.

    The owner asked for the extra buttons to move into a collapsible list shown
    only when tapped -- so a card carries just the ☰ toggle, and the whole menu
    lives one tap away. A card that is itself a list (the journal) keeps its own
    buttons plus the toggle, and an open-trade card keeps its close buttons.
    """
    return kb([COLLAPSE_BTN])


def keyboard_for(action: str) -> dict:
    if action == "journal_menu":
        return kb(JOURNAL_MENU)
    if action == "menu":
        # The expanded list carries every action, plus a row to fold it back.
        return kb(MENU)
    if action == "open":
        ck = close_keyboard()
        if ck:
            ck["inline_keyboard"] += COLLAPSE_BTN_ROWS
            return ck
    return nav(action)


COLLAPSE_BTN_ROWS = [[{"text": t, "callback_data": d} for t, d in COLLAPSE_BTN]]


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


def trend_from(close: float, e20: float | None,
               e50: float | None) -> int:
    """The frame tilt from the price and its two EMAs -- the desk's ONE
    definition of "up" and "down", so every surface and the study agree.

    A frame is UP while price sits ABOVE both averages, DOWN while it sits
    BELOW both, and NEUTRAL in between. The old rule compared the two averages
    alone, blind to where price actually was: when gold fell 114 points in four
    4h bars the 20-EMA had not caught up, so EMA20 still sat above EMA50 and the
    frame read "up" while price was 50 points under both. Price is the truth; the
    averages only smooth it. Price on one side of both is a trend, between them
    is a transition, and the middle case stays neutral rather than guessing.
    """
    if e20 is None or e50 is None:
        return 0
    if close > e20 and close > e50:
        return 1
    if close < e20 and close < e50:
        return -1
    return 0


def frame_trend(b: list[dict]) -> int:
    closes = [x["close"] for x in b]
    if not closes:
        return 0
    return trend_from(closes[-1], ema(closes, 20), ema(closes, 50))


FRAMES = ("1d", "4h", "1h", "30m", "15m", "5m")
# Arabic labels for the frames, so a report reads in the owner's language.
TF_AR = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
         "15m": "15د", "5m": "5د"}
# The reading may lean on the big frames; a TRADE still needs CONFIRM_FRAMES of
# all six. Big frames weigh more because a daily/4h tilt outlives a 5m wiggle.
BIG_FRAMES = ("1d", "4h", "1h")
SMALL_FRAMES = ("30m", "15m", "5m")


def read_market() -> dict:
    """One read of the tape: quote plus every frame's trend, ATR and RSI."""
    q = quote()
    live = float(q.get("mid") or q.get("last") or 0)
    trends, atrs, rsis, gap = {}, {}, {}, {}
    for tf in FRAMES:
        try:
            b = bars(tf, 300)
            trends[tf] = frame_trend(b)
            atrs[tf] = atr(b)
            rsis[tf] = rsi([x["close"] for x in b])
            c = [x["close"] for x in b]
            e20, e50 = ema(c, 20), ema(c, 50)
            a = atrs[tf]
            gap[tf] = (min(abs(live - e20), abs(live - e50)) / a
                       if a and e20 is not None and e50 is not None else None)
        except Exception:  # noqa: BLE001 - one missing frame must not kill the read
            trends[tf] = 0
            atrs[tf] = None
            rsis[tf] = None
            gap[tf] = None
    # `gap` = how far price sits from BOTH averages on each frame, in ATRs --
    # the margin behind a trend reading. It is INFORMATIONAL: no decision reads
    # it, it only lets the card say when a 6/6 tilt is actually a hair-trigger
    # (price resting on the averages, so a few points flip the whole read).
    return {"q": q, "live": live, "trends": trends, "atrs": atrs,
            "rsis": rsis, "gap": gap}


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
    """Higher-highs/higher-lows on 1h, OR None when the tape is unreadable.

    Two conditions, both required: the swings must be rising AND price must
    still sit on the right side of the last one. The old version compared the
    last two pivots only, so after price collapsed 120 points below the last
    swing low it still announced "بنية صاعدة" -- a fact about the past that the
    present had already broken. A structure is only live while price respects it.
    """
    try:
        bh = bars("1h", 200)
        highs, lows = pivots(bh, 3, 3)
        live = float(bh[-1]["close"])
    except Exception:  # noqa: BLE001 - a missing frame must not kill the card
        return None
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    if hh and hl and live > lows[-1]:
        return "البنية (1س): قمم وقيعان صاعدة 🟢 (بنية صاعدة)"
    lh = highs[-1] < highs[-2]
    ll = lows[-1] < lows[-2]
    if lh and ll and live < highs[-1]:
        return "البنية (1س): قمم وقيعان هابطة 🔴 (بنية هابطة)"
    if live < lows[-1]:
        return "البنية (1س): انكسر آخر قاع 🔴 (البنية انهارت لتحت)"
    if live > highs[-1]:
        return "البنية (1س): اخترق آخر قمة 🟢 (البنية انفتحت لفوق)"
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


def _swing_lines(live: float, seen: set[float],
                 tol: float) -> list[str]:
    """The raw pivot swings per frame, minus any already shown as a level.

    The levels above are the zones the desk trades; these are the bare swing
    highs/lows on the frames the owner watches. Two rules keep this from being
    the levels printed a second time: split by the live price (a raw list prints
    pre-drop lows ABOVE the price, which reads as nonsense), and DROP any swing
    within `tol` of a level already named -- a number shown twice is padding.
    """
    out = []
    for tf, label in (("1h", "1س"), ("30m", "30د"), ("15m", "15د")):
        try:
            hs, ls_ = pivots(bars(tf, 150))
        except Exception:  # noqa: BLE001 - a missing frame must not kill the card
            continue
        tops = sorted(x for x in hs if x > live
                      and not any(abs(x - s) <= tol for s in seen))[:3]
        bots = sorted((x for x in ls_ if x < live
                       and not any(abs(x - s) <= tol for s in seen)),
                      reverse=True)[:3]
        parts = []
        if tops:
            parts.append("قمم فوق: " + " · ".join(num(x) for x in tops))
        if bots:
            parts.append("قيعان تحت: " + " · ".join(num(x) for x in bots))
        if parts:
            out.append(f"  {label} — " + " | ".join(parts))
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
    # plan() is now MY read with no quality gate. A confirmed trend is a trade,
    # so a "wait" here means one of two things: the price is retracing a real
    # leg and has not reached the wall yet, or the frames simply disagree.
    # Carry the wall so the 'شو تعمل' card can name the level it is waiting for.
    wall = None
    big = _big_leg(m["live"])
    if big is not None:
        bhi, blo, bdown, pos = big
        span = bhi - blo
        if bdown and 0.25 <= pos <= 0.62:
            wall = blo + span * 0.382
        elif not bdown and 0.38 <= pos <= 0.75:
            wall = bhi - span * 0.382
    return {"state": "wait", "up": up, "dn": dn, "wall": wall,
            "lean": lean, "mark": mark,
            "direction": "long" if up >= CONFIRM_FRAMES else
            ("short" if dn >= CONFIRM_FRAMES else None)}


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
    if v.get("wall"):
        d = "بيع" if v.get("direction") == "short" else "شراء"
        return f"⏸️ استنى — لمس الجدار {num(v['wall'])} ثم {d}"
    if v["up"] >= CONFIRM_FRAMES:
        return "⏸️ استنى — الاتجاه صاعد بس ما في دخول مؤكد"
    if v["dn"] >= CONFIRM_FRAMES:
        return "⏸️ استنى — الاتجاه هابط بس ما في دخول مؤكد"
    return f"⏸️ استنى — السوق محتار ({v['up']}ص · {v['dn']}هـ)"


def _verdict_detail(v: dict) -> str:
    """The full sentence behind the headline, for the analysis card's verdict."""
    st = v["state"]
    if st == "closed":
        return "⛔ السوق مسكّر — ما في قراءة حيّة."
    if st == "hold":
        return (f"📂 صفقة مفتوحة #{v['trade']['id']} — أنا بيديرها، "
                "ما بفتح جديدة الآن.")
    if st == "enter":
        p = v["plan"]
        d = "شراء 🟢" if p["direction"] == "long" else "بيع 🔴"
        return (f"✅ صفقة جاهزة — {d} · دخول {num(p['entry'])} · "
                f"وقف {num(p['stop'])} · هدف {num(p['target'])} · "
                f"R:R {p['rr']:.2f}")
    if v.get("wall"):
        d = "بيع" if v.get("direction") == "short" else "شراء"
        return (f"⏸️ السعر بيمشي عكس موجة كبيرة — <b>بستنى يلمس الجدار "
                f"{num(v['wall'])}</b> ثم يغلق 1س {d}، وبعدها أدخل.")
    if v["up"] >= CONFIRM_FRAMES:
        return (f"⏸️ الاتجاه <b>صاعد</b> ({v['up']}/6) — بس السعر لسا ما وصل "
                "لزناد الدخول.")
    if v["dn"] >= CONFIRM_FRAMES:
        return (f"⏸️ الاتجاه <b>هابط</b> ({v['dn']}/6) — بس السعر لسا ما وصل "
                "لزناد الدخول.")
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
        lines.append(f"EMA20 ({names[TREND_FILTER_TF]}): السعر {pos} "
                     f"({num(ef)})")

    # ---- indicators (shown as information, NOT a gate) ------------------
    if FILTERS_ON:
        r = filter_status("long", b15f)["readings"]

        def _fmt(v, dp, suf=""):
            return f"{v:.{dp}f}{suf}" if isinstance(v, (int, float)) else "—"

        adx_v, atr_p, rsi_v, macd_v = (r["adx"], r["atr_pct"],
                                       r["rsi"], r["macd_hist"])
        macd_ico = "📈" if (macd_v or 0) > 0 else "📉"
        lines.append(f"المؤشرات: ADX {_fmt(adx_v, 0)} · "
                     f"ATR {_fmt(atr_p * 100 if atr_p else None, 2, '%')}")
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
    lines.append("<b>حكمي:</b>")
    lines.append(_verdict_detail(v))
    lines.append("\n<i>قراءة تحليلية لحظية — مو وعد ربح.</i>")
    return "\n".join(lines)


def _levels(live: float) -> tuple[list[float], list[float]]:
    """Nearest resistance levels above and support levels below the price.

    Pivots need `right` bars to confirm, so right after a sharp move the newest
    extreme is not yet a pivot and the side it sits on can come back EMPTY -- a
    fast fall to a fresh low leaves no confirmed support below, and the report
    loses its support section and half its scenarios. So each side falls back to
    the recent window's own extreme (the lowest low below price / highest high
    above it) when no pivot qualifies: a real level the owner is watching, not
    an invented one.
    """
    try:
        b = bars("1h", 300)
    except Exception:  # noqa: BLE001
        return [], []
    highs, lows = pivots(b, 3, 3)
    res = sorted({round(h, 2) for h in highs if h > live})
    sup = sorted({round(l, 2) for l in lows if l < live}, reverse=True)
    win = b[-120:]
    if not sup and win:
        swing_lo = min(x["low"] for x in win)
        if swing_lo < live:
            sup = [round(swing_lo, 2)]
    if not res and win:
        swing_hi = max(x["high"] for x in win)
        if swing_hi > live:
            res = [round(swing_hi, 2)]
    return res, sup


def _live_levels(live: float, n: int = 3) -> tuple[float, ...]:
    """The nearest pivots above and below the live price, from the desk's own
    _levels(). WATCH_LEVELS is a FROZEN set pinned when price sat near 4100; as
    the market travels those levels drift far away and the ones that matter now
    (the nearest support and resistance) are not in it at all. These travel with
    the price, so the crossing watch always covers the levels that matter now.
    """
    try:
        res, sup = _levels(live)
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return ()
    return tuple(round(x, 2) for x in (res[:n] + sup[:n]))


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
                 f"الوقف والهدف من قواعدي (مخاطرة ${STOP_MIN_USD:.0f}-"
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
            lines.append("  <i>ما باخد صفقة ترند لما ADX تحت 20.</i>")
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


# ========================================================================
# the sticky level pool -- a named level is never taken down
# ========================================================================
# The owner's rule: once the map has named a support, a resistance, a zone or a
# liquidity pool, it STAYS on the map. A refresh may ADD a level; it may never
# silently drop one. Without this a sharp move recomputed the nearest support
# and the level the owner was watching vanished -- the map contradicted itself
# between two reads a minute apart.
#
# Three families, each sticky on its own so a list keeps its meaning: the pivot
# LEVELS (_levels), the multi-touch ZONES (_zones), and the LIQUIDITY pools
# (_liquidity). Every family is stored whole and re-split by the live price at
# read time, so a level crosses from resistance to support as price moves
# through it -- it is never removed, only re-labelled.
#
# The pool is bounded so it cannot grow forever: each side keeps the STICKY_MAX
# nearest to the price. A level leaves only when that many nearer levels have
# been named, never because a refresh recomputed it away -- the exact failure
# the owner reported.
STICKY_MAX = int(os.environ.get("DESK_STICKY_MAX", "15") or "15")
# What is STORED is larger than what is shown, but still bounded: the farthest
# levels are pruned so the file cannot grow forever. Pruning keeps the nearest,
# so the display (which shows the nearest STICKY_MAX) is unaffected.
STICKY_STORE_MAX = int(os.environ.get("DESK_STICKY_STORE_MAX", "80") or "80")
_STICKY_LOCK = threading.Lock()


def _sticky_load() -> dict:
    """The stored pool: {"lvls": [...], "zones": [...], "liq": [...],
    "touches": {price: count}}, every price a plain float."""
    try:
        blob = json.loads(STICKY_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(blob, dict):
        return {}
    out: dict = {}
    for k in ("lvls", "zones", "liq"):
        v = blob.get(k)
        if isinstance(v, list):
            out[k] = [float(x) for x in v if isinstance(x, (int, float))]
    tch = blob.get("touches")
    if isinstance(tch, dict):
        out["touches"] = {float(k): int(v) for k, v in tch.items()
                          if isinstance(v, (int, float))}
    return out


def _sticky_save(pool: dict) -> None:
    body = json.dumps({"lvls": sorted(set(pool.get("lvls") or [])),
                       "zones": sorted(set(pool.get("zones") or [])),
                       "liq": sorted(set(pool.get("liq") or [])),
                       "touches": {f"{p:.2f}": n for p, n
                                   in (pool.get("touches") or {}).items()}},
                      ensure_ascii=False, indent=1)
    # Write only when the pool actually changed: the map re-reads every hour, and
    # an unchanged pool must not churn the file (or its mtime).
    try:
        if STICKY_PATH.read_text(encoding="utf-8") == body:
            return
    except OSError:
        pass
    try:
        STICKY_PATH.write_text(body, encoding="utf-8")
    except OSError:
        pass


def _sticky_map(live: float, res, sup, sell_z, buy_z, liq_hi, liq_lo) -> dict:
    """Union the fresh levels with the stored pool; a watched level never drops.

    Each family is folded into the pool, the pool is written back, and the whole
    pool is re-split by the CURRENT price -- so a level crosses from resistance
    to support as price moves through it, but is never removed. Each side keeps
    the STICKY_MAX nearest to the price, nearest first.

    This is for the MAP'S DISPLAY only. The plan and the scenarios keep reading
    the FRESH levels: a trade may never be triggered by a stale level the market
    has already left behind.
    """
    with _STICKY_LOCK:
        pool = _sticky_load()
        pool["lvls"] = sorted(set(pool.get("lvls", []))
                              | {round(x, 2) for x in list(res) + list(sup)})
        pool["zones"] = sorted(set(pool.get("zones", []))
                               | {round(z, 2) for z, _ in list(sell_z) + list(buy_z)})
        pool["liq"] = sorted(set(pool.get("liq", []))
                             | {round(x, 2) for x in list(liq_hi) + list(liq_lo)})
        tch = dict(pool.get("touches", {}))
        for z, n in list(sell_z) + list(buy_z):
            tch[round(z, 2)] = n
        pool["touches"] = tch
        # Prune the farthest STORED levels so the file stays bounded; the nearest
        # STICKY_STORE_MAX survive, which always covers the displayed STICKY_MAX.
        def _keep_near(vals: list[float]) -> list[float]:
            return sorted(vals, key=lambda v: abs(v - live))[:STICKY_STORE_MAX]
        pool["lvls"] = _keep_near(pool["lvls"])
        pool["zones"] = _keep_near(pool["zones"])
        pool["liq"] = _keep_near(pool["liq"])
        _sticky_save(pool)

        lv, zo, lq = pool["lvls"], pool["zones"], pool["liq"]
        res_out = sorted(x for x in lv if x > live)[:STICKY_MAX]
        sup_out = sorted((x for x in lv if x < live), reverse=True)[:STICKY_MAX]
        sell_out = [(z, tch.get(z, 0))
                    for z in sorted(x for x in zo if x > live)[:STICKY_MAX]]
        buy_out = [(z, tch.get(z, 0))
                   for z in sorted((x for x in zo if x < live),
                                   reverse=True)[:STICKY_MAX]]
        liq_up = sorted(x for x in lq if x > live)[:3]
        liq_dn = sorted((x for x in lq if x < live), reverse=True)[:3]
    return {"res": res_out, "sup": sup_out, "sell": sell_out, "buy": buy_out,
            "liq_above": liq_up, "liq_below": liq_dn}


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


def _display_levels(live: float) -> tuple[list[float], list[float]]:
    """The levels the map DISPLAYS: the sticky pool, split by the live price.

    The map shows the sticky pool -- a level once named is never dropped -- so
    the report reads the SAME set here. A standalone report (the Telegram card)
    therefore names the same prices the live page does; the trade surfaces keep
    the FRESH levels, because a trade may not fire off a level the market left.
    """
    if live <= 0:
        return [], []
    fres, fsup = _levels(live)
    try:
        st = _sticky_map(live, fres, fsup, [], [], [], [])
        return list(st["res"]), list(st["sup"])
    except Exception:  # noqa: BLE001 - a bad pool must not kill the report
        return fres, fsup


def card_report() -> str:
    """The full analysis report as a Telegram card -- the SAME text as the map.

    It prints report_payload()["lines"] verbatim: the very lines the live page
    prints, so the report is word-for-word identical on the two surfaces. One
    text, two views -- they cannot drift, because there is only one text.
    """
    p = report_payload()
    return "\n".join(p["lines"])


def _report_lines(p: dict) -> list[str]:
    """The report as ONE list of HTML lines -- the canonical text.

    The Telegram card and the live map page both print these lines VERBATIM, so
    the report is word-for-word identical on both. Telegram HTML and the page
    share the same subset (<b>, <i>), so one list serves both.
    """
    live = p["live"]
    if not live:
        return ["⚠️ ما في قراءة سوق هلّق — جرّب بعد شوي."]

    L = [f"🥇 <b>تحليلي المستقل — {esc(p['symbol'])}</b>"]
    dd = p.get("dayDiffPercent")
    head = f"السعر <b>{num(live)}</b>"
    if dd is not None:
        head += f" · اليوم {'🔴' if dd < 0 else '🟢'} {dd:+.2f}%"
    L.append(head)
    L.append(f"<i>{p['clock']} {p['tz']}</i>")

    L.append(f"<b>🧭 الميل العام:</b> {esc(p['bias'])} "
             f"({p['dn']} فريم هابط · {p['up']} صاعد من 6)")

    L.append("")
    L.append("<b>📊 الفريمات:</b>")
    for f in p["frames"]:
        icon = "🔴" if f["trend"] == -1 else ("🟢" if f["trend"] == 1 else "⚪")
        r = f"{f['rsi']:.0f}" if f["rsi"] is not None else "—"
        a = f"{f['atr']:.1f}" if f["atr"] is not None else "—"
        L.append(f"{icon} <b>{f['label']}</b> — RSI {r} · ATR {a}")
        if f["read"]:
            L.append(f"   <i>{esc(f['read'])}</i>")

    if p["structure"]:
        L.append("")
        L.append(f"<b>🏗️ البنية:</b> {esc(p['structure'])}")

    if p["levels"]:
        L.append("")
        L.append("<b>🧭 المستويات:</b>")
        for lv in p["levels"][:8]:
            arrow = "🔺" if lv["kind"] == "مقاومة" else "🔻"
            L.append(f"{arrow} {num(lv['price'])} <i>({lv['dist']:+.0f})</i>")

    s = p["sessions"]
    sline = []
    for lbl, key in (("آسيا", "asia"), ("لندن", "london"), ("نيويورك", "ny")):
        if s.get(key):
            sline.append(f"{lbl} {num(s[key]['low'])} → {num(s[key]['high'])} "
                         f"({s[key]['pts']:.1f})")
    if sline:
        L.append("")
        L.append("<b>🌏 نطاق الجلسات:</b>")
        L.extend(sline)

    if p["fib_line"]:
        L.append("")
        L.append(f"<b>📐 فيبو:</b> {esc(p['fib_line'])}")

    if p["days"]:
        L.append("")
        L.append("<b>📅 آخر الأيام (فتح · قمة · قاع · إغلاق · تغيّر):</b>")
        for d in p["days"]:
            ic = "🟢" if d["chg"] >= 0 else "🔴"
            L.append(f"{d['date'][5:]} — {num(d['open'])} · {num(d['high'])} · "
                     f"{num(d['low'])} · {num(d['close'])} ({ic} {d['chg']:+.1f})")

    if p["bullets"]:
        L.append("")
        L.append("<b>🎯 الخلاصة:</b>")
        L.extend(f"• {esc(b)}" for b in p["bullets"])

    L.append("")
    L.append("<i>قراءة فنية من بيانات حيّة — مو توصية ولا وعد ربح. "
             "الوقف إلزامي دايماً.</i>")
    return L


def _ema_last(vals: list[float], n: int) -> float | None:
    """The last EMA of a series, or None when there is not enough history."""
    s = ema_series(vals, n)
    return s[-1] if s else None


def fib_levels(live: float) -> dict:
    """Fibonacci retracement + extension over the live swing, the way I read it
    by hand: take the most recent significant swing (the highest high and lowest
    low of the recent 1h window, whichever came LAST is the anchor), project the
    retracement levels the owner watches (23.6 / 38.2 / 50 / 61.8 / 78.6), and
    the extension targets (127.2 / 161.8) beyond the swing.

    The point is not the exact ratio -- it is which levels price has already
    crossed and which one it is sitting on. "the 61.8% broke" is a fact; a bare
    "61.8% = 3976.5" is decoration. Returns the nearest retracement above and
    below, whether the last big level broke, and the extension target.

    Guarded: a missing frame returns {} and the map prints no fibo section.
    """
    try:
        b = bars("1h", 300)
        if len(b) < 40:
            return {}
        # The swing: the extreme high and extreme low of the recent window.
        # Whichever came LAST is where the move ended, so the move ran FROM the
        # other one TO it: a low more recent than the high is a DOWN move (high
        # -> low), and its retracement levels sit ABOVE the low, extensions BELOW.
        win = b[-120:]
        top = max(x["high"] for x in win)
        bot = min(x["low"] for x in win)
        if top <= bot:
            return {}
        i_top = max(i for i, x in enumerate(win) if x["high"] == top)
        i_bot = max(i for i, x in enumerate(win) if x["low"] == bot)
        down = i_bot > i_top          # the low is more recent -> a down move
        rng = top - bot
        ratios = (0.236, 0.382, 0.5, 0.618, 0.786)
        levels: list[dict] = []
        for r in ratios:
            lv = bot + rng * r if down else top - rng * r
            levels.append({"ratio": r, "price": round(lv, 2)})
        # Which level is price sitting on / has crossed? (down: crossed = above)
        above = [l for l in levels if l["price"] > live]
        below = [l for l in levels if l["price"] <= live]
        nearest_above = min(above, key=lambda l: l["price"] - live) if above else None
        nearest_below = max(below, key=lambda l: live - l["price"]) if below else None
        # Extensions beyond the move's end: below the low for a down move, above
        # the high for an up move -- where price goes if the move keeps going.
        ext = {}
        for r in (1.272, 1.618):
            ext[str(r)] = round(bot - rng * (r - 1), 2) if down \
                else round(top + rng * (r - 1), 2)
        return {
            "top": round(top, 2), "bottom": round(bot, 2),
            "direction": "down" if down else "up",
            "levels": levels,
            "nearest_above": nearest_above, "nearest_below": nearest_below,
            "extensions": ext,
        }
    except Exception:  # noqa: BLE001 - a missing frame must not kill the read
        return {}


def fib_read(live: float) -> str:
    """The fibo sentence the map and the daily note show -- a fact, not a chart."""
    f = fib_levels(live)
    if not f:
        return ""
    d = "هابط" if f["direction"] == "down" else "صاعد"
    a = f.get("nearest_above")
    bl = f.get("nearest_below")
    parts = [f"فيبو (موجة {d}): {f['bottom']:.2f} ← {f['top']:.2f}"]
    if bl:
        parts.append(f"السعر فوق {bl['ratio']*100:.1f}% ({bl['price']:.2f})")
    if a:
        parts.append(f"تحت {a['ratio']*100:.1f}% ({a['price']:.2f})")
    if f.get("extensions"):
        parts.append(f"امتداد 127% → {f['extensions']['1.272']:.2f}")
    return " · ".join(parts)


def divergence_read() -> list[dict]:
    """Momentum divergence, the tell that a move is tiring.

    Bullish: price makes a LOWER low while RSI makes a HIGHER low. Bearish: price
    makes a HIGHER high while RSI makes a LOWER high. Checked on 5m, 15m and 1h so
    a short-term tell and a bigger one both speak. Each hit carries its numbers.

    This is a FACT about the last two swings, not a prediction -- the honest
    reading of "the fall is losing steam" without claiming a reversal.
    """
    out: list[dict] = []
    for tf in ("5m", "15m", "1h"):
        try:
            b = closed(bars(tf, 300), tf)
            if len(b) < 40:
                continue
            cl = [x["close"] for x in b]
            now = rsi(cl, 14)
            prev = rsi(cl[:-8], 14)
            if now is None or prev is None:
                continue
            lo_now, lo_prev = min(cl[-8:]), min(cl[-16:-8])
            hi_now, hi_prev = max(cl[-8:]), max(cl[-16:-8])
            if lo_now < lo_prev and now > prev + 2:
                out.append({"tf": tf, "kind": "دايفرجنس إيجابي", "tone": "warn",
                            "text": (f"دايفرجنس إيجابي ({tf}): السعر قاع أدنى "
                                     f"({lo_now:.1f}←{lo_prev:.1f}) بس RSI طلع "
                                     f"({prev:.0f}→{now:.0f}) — الهبوط عم يتعب.")})
            elif hi_now > hi_prev and now < prev - 2:
                out.append({"tf": tf, "kind": "دايفرجنس سلبي", "tone": "warn",
                            "text": (f"دايفرجنس سلبي ({tf}): السعر قمة أعلى "
                                     f"({hi_now:.1f}←{hi_prev:.1f}) بس RSI نزل "
                                     f"({prev:.0f}→{now:.0f}) — الصعود عم يتعب.")})
        except Exception:  # noqa: BLE001 - one frame must not kill the read
            continue
    return out


def daily_analysis(now: datetime | None = None) -> str:
    """THE DAILY READ -- the whole hand-written analysis, once a day, in Arabic.

    This is the piece the owner asked for: the same shape as the analysis I write
    by hand, assembled from the live tape -- every section named and numbered, so
    it reads as a report, not a card. It carries: the snapshot, the big picture,
    the six frames, structure (higher/lower highs and lows), resistance and
    support with touch counts, supply/demand zones, liquidity, Fibonacci, momentum
    divergence, three named scenarios, and a playbook -- then the honest note.

    It is deliberately NOT a promise: every number is measured, the fibo and
    divergence are facts about the swings, and the honesty line says what could
    go wrong. Nothing is invented; a missing piece drops its section.

    `now` lets the caller stamp the time (tests pass a fixed one).
    """
    m = read_market()
    q, live = m["q"], m["live"]
    res, sup = _levels(live) if live > 0 else ([], [])
    sell_z, buy_z = _zones(live) if live > 0 else ([], [])
    liq_hi, liq_lo = _liquidity(live) if live > 0 else ([], [])
    trends, rsis, atrs = m["trends"], m.get("rsis") or {}, m.get("atrs") or {}
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    r15 = rsis.get("15m")
    now = now or datetime.now(UTC)

    L: list[str] = []
    L.append(f"📊 <b>التحليل اليومي — XAUUSD</b>")
    L.append(f"🗓️ {now.strftime('%Y-%m-%d')} · {_clock(now)} {TZ_LABEL}")
    L.append("")

    # --- 1. the snapshot ---
    L.append("<b>١) اللقطة اللحظية</b>")
    L.append(f"السعر <b>{num(live)}</b> · سبريد {num(q.get('spread'))} · "
             f"{'🟢 مفتوح' if market_open(q) else '🔴 مسكّر'}")
    dd = q.get("dayDiffPercent")
    if dd is not None:
        L.append(f"تغيّر اليوم <b>{dd:+.2f}%</b>")
    hi, lo = q.get("high"), q.get("low")
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi > lo:
        pos = (live - lo) / (hi - lo) * 100
        L.append(f"نطاق اليوم {num(lo)}–{num(hi)} ({hi-lo:.1f} نقطة) · "
                 f"السعر عند <b>{pos:.0f}%</b> منه")
    L.append("")

    # --- 2. the big picture: the multi-day leg ---
    L.append("<b>٢) الصورة الكبيرة (اليومي)</b>")
    with suppress(Exception):
        bd = bars("1d", 30)
        if len(bd) >= 4:
            rows = bd[-4:]
            for x in rows:
                chg = x["close"] - x["open"]
                L.append(f"  {x['openTime'][:10]}: {num(x['open'])}→{num(x['close'])} "
                         f"({chg:+.1f})")
            leg = bd[-1]["close"] - bd[-4]["open"]
            L.append(f"  → حركة آخر 4 أيام: <b>{leg:+.1f}</b> نقطة")
    L.append("")

    # --- 3. the six frames ---
    L.append("<b>٣) الفريمات (ميل · RSI · ATR)</b>")
    for tf in FRAMES:
        t = trends.get(tf)
        icon = "🔴" if t == -1 else ("🟢" if t == 1 else "⚪")
        r = rsis.get(tf)
        a = atrs.get(tf)
        L.append(f"  {TF_AR.get(tf, tf)}: {icon} RSI {r:.0f}" if r is not None
                 else f"  {TF_AR.get(tf, tf)}: {icon}")
        if a is not None:
            L[-1] += f" · ATR {a:.1f}"
    L.append(f"  → إجماع: <b>{dn}/6 هابط</b>" if dn > up else
             (f"  → إجماع: <b>{up}/6 صاعد</b>" if up > dn else "  → إجماع: عرضي"))
    L.append("")

    # --- 4. structure ---
    L.append("<b>٤) البنية</b>")
    sr = _structure_read()
    if sr:
        L.append(f"  {sr}")
    with suppress(Exception):
        b1 = bars("1h", 200)
        hh, ll = pivots(b1, 3, 3)
        if len(hh) >= 2 and len(ll) >= 2:
            L.append(f"  آخر قمم: {' · '.join(num(x) for x in hh[-3:])}")
            L.append(f"  آخر قيعان: {' · '.join(num(x) for x in ll[-3:])}")
    L.append("")

    # --- 5. levels with strength ---
    L.append("<b>٥) المقاومة والدعم</b>")
    L += _levels_with_strength(live) or ["  —"]
    L.append("")

    # --- 6. zones ---
    L.append("<b>٦) مناطق العرض والطلب</b>")
    if sell_z:
        L.append("  عرض (بيع): " + " · ".join(f"{num(p)} ({n} لمسة)"
                                              for p, n in sell_z[:4]))
    if buy_z:
        L.append("  طلب (شراء): " + " · ".join(f"{num(p)} ({n} لمسة)"
                                              for p, n in buy_z[:4]))
    if not sell_z and not buy_z:
        L.append("  —")
    L.append("")

    # --- 7. liquidity ---
    L.append("<b>٧) السيولة</b>")
    if liq_hi:
        L.append("  فوق (ستوبات البائعين): " +
                 " · ".join(num(x) for x in liq_hi[:3]))
    if liq_lo:
        L.append("  تحت (ستوبات الشاريين): " +
                 " · ".join(num(x) for x in liq_lo[:3]))
    if not liq_hi and not liq_lo:
        L.append("  —")
    L.append("")

    # --- 8. fibonacci ---
    L.append("<b>٨) فيبوناتشي</b>")
    fr = fib_levels(live)
    if fr:
        d = "هابط" if fr["direction"] == "down" else "صاعد"
        L.append(f"  موجة {d}: {num(fr['bottom'])} ← {num(fr['top'])}")
        for l in fr["levels"]:
            mark = " ← السعر" if (fr.get("nearest_above") and
                                  abs(l["price"] - fr["nearest_above"]["price"]) < 1) or \
                                 (fr.get("nearest_below") and
                                  abs(l["price"] - fr["nearest_below"]["price"]) < 1) else ""
            L.append(f"    {l['ratio']*100:.1f}% → {num(l['price'])}{mark}")
        L.append(f"  امتداد 127% → {num(fr['extensions']['1.272'])} · "
                 f"161% → {num(fr['extensions']['1.618'])}")
    else:
        L.append("  —")
    L.append("")

    # --- 9. divergence ---
    L.append("<b>٩) الدايفرجنس</b>")
    dv = divergence_read()
    if dv:
        for x in dv:
            L.append(f"  {x['text']}")
    else:
        L.append("  ما في دايفرجنس واضح حالياً.")
    L.append("")

    # --- 10. the three scenarios (the map's own engine) ---
    L.append("<b>١٠) السيناريوهات</b>")
    p = None
    with suppress(Exception):
        p = plan(m)
    adx_v = None
    with suppress(Exception):
        b15 = closed(bars("15m", 300), "15m")
        adx_v = adx_series(b15)[-1] if b15 else None
    a = map_analysis(m, live, res, sup, p, adx_v) if live > 0 else None
    if a:
        L.append(f"  الميل: <b>{a['bias']}</b>")
        for i, s in enumerate(a["scenarios"], 1):
            arrow = "🔴" if s["side"] == "short" else "🟢"
            L.append(f"  {i}. {arrow} <b>{s['name']}</b> ({s.get('tag','')})")
            L.append(f"     مشغّل {num(s['trigger'])} · وقف {num(s['stop'])} · "
                     f"هدف {num(s['target'])} · R:R {s['rr']:.2f}")
        L.append(f"  🎯 الحكم: {a['verdict']}")
    L.append("")

    # --- 11. the playbook ---
    L.append("<b>١١) إيمتى تشتري وإيمتى تبيع</b>")
    if a:
        if a["sell"]:
            L.append("  🔴 البيع:")
            for pl in a["sell"][:3]:
                L.append(f"    • {pl['kind']} — {pl['when']}: دخول {num(pl['entry'])} · "
                         f"وقف {num(pl['stop'])} · هدف {num(pl['target'])} · "
                         f"R:R {pl['rr']:.2f}")
        if a["buy"]:
            L.append("  🟢 الشراء:")
            for pl in a["buy"][:3]:
                L.append(f"    • {pl['kind']} — {pl['when']}: دخول {num(pl['entry'])} · "
                         f"وقف {num(pl['stop'])} · هدف {num(pl['target'])} · "
                         f"R:R {pl['rr']:.2f}")
    L.append("")

    # --- 12. the honest note (the owner's one non-negotiable) ---
    L.append("<b>١٢) ملاحظة الصدق</b>")
    L.append(f"  {a['honesty'] if a else 'قراءة آلية — ما في ضمان. الوقف إلزامي.'}")
    L.append("")
    L.append("<i>قراءة آلية من بيانات السوق الحيّة — مو نصيحة مالية · الوقف إلزامي دايماً.</i>")
    return "\n".join(L)


def map_analysis(m: dict, live: float, res: list[float], sup: list[float],
                 plan_d: dict | None, adx_v: float | None) -> dict:
    """The analysis the owner asked for -- the SAME shape as the read I write by
    hand: a stated bias, THREE named scenarios (continuation / retrace /
    reversal), a when-to-buy / when-to-sell playbook, and an honest note.

    It is deliberately NOT the desk's card. The card shows two conditional
    breakouts; this names the three ways the tape can go and prices each one,
    the way a person would talk it through. Prices still come from _sized() so
    nothing here is a number the desk would not stand behind.
    """
    trends = m["trends"]
    rsis = m.get("rsis") or {}
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    r15 = rsis.get("15m")

    if dn >= 5:
        bias, bside = "هابط", "sell"
    elif up >= 5:
        bias, bside = "صاعد", "buy"
    elif dn > up:
        bias, bside = "مائل للهبوط", "sell"
    elif up > dn:
        bias, bside = "مائل للصعود", "buy"
    else:
        bias, bside = "عرضي", None
    down = dn >= up

    # --- the three named scenarios: continuation, retrace, reversal ---
    scen: list[dict] = []
    if down:
        if sup:
            s = _sized(sup[0] - 0.5, "short", sup[0])
            scen.append({"side": "short", "name": "استمرار الهبوط",
                         "tag": "مع الاتجاه · الأرجح", "trigger": sup[0] - 0.5,
                         "targets": sup[1:4], **s})
            s = _sized(live, "long", sup[0])
            scen.append({"side": "long", "name": "ارتداد تصحيحي (قصير)",
                         "tag": "مضاد · نصّ الحجم", "trigger": live,
                         "targets": ([res[0]] if res else []), **s})
        if res:
            s = _sized(res[0] + 0.5, "long", res[0])
            scen.append({"side": "long", "name": "انعكاس صاعد",
                         "tag": "يحتاج إثبات", "trigger": res[0] + 0.5,
                         "targets": res[1:4], **s})
    else:
        if res:
            s = _sized(res[0] + 0.5, "long", res[0])
            scen.append({"side": "long", "name": "استمرار الصعود",
                         "tag": "مع الاتجاه · الأرجح", "trigger": res[0] + 0.5,
                         "targets": res[1:4], **s})
            s = _sized(live, "short", res[0])
            scen.append({"side": "short", "name": "ارتداد تصحيحي (قصير)",
                         "tag": "مضاد · نصّ الحجم", "trigger": live,
                         "targets": ([sup[0]] if sup else []), **s})
        if sup:
            s = _sized(sup[0] - 0.5, "short", sup[0])
            scen.append({"side": "short", "name": "انكاس هابط",
                         "tag": "يحتاج إثبات", "trigger": sup[0] - 0.5,
                         "targets": sup[1:4], **s})

    # --- the playbook: WHEN to sell and WHEN to buy, each priced ---
    sell: list[dict] = []
    buy: list[dict] = []
    if res:
        s = _sized(res[0], "short", res[0])
        sell.append({"kind": "بيع ارتداد (متحفّظ)",
                     "when": f"لمس {num(res[0])} ورفض", **s})
    if sup:
        s = _sized(sup[0] - 0.5, "short", sup[0])
        sell.append({"kind": "بيع كسر (اختراقي) ⭐",
                     "when": f"كسر {num(sup[0])} بإغلاق", **s})
    if plan_d and plan_d.get("direction") == "short":
        s = _sized(live, "short", min(res) if res else None)
        sell.append({"kind": "بيع فوري (مع الاتجاه)",
                     "when": "بالسعر الحالي", **s})
    if sup:
        s = _sized(sup[0], "long", sup[0])
        buy.append({"kind": "شراء عند الدعم",
                     "when": f"لمس {num(sup[0])} ورفض", **s})
    if res:
        s = _sized(res[0] + 0.5, "long", res[0])
        buy.append({"kind": "شراء اختراق",
                     "when": f"إغلاق فوق {num(res[0])}", **s})

    # --- the plain-spoken verdict ---
    if bside == "sell" and isinstance(r15, (int, float)) and r15 <= 30:
        verdict = ("الاتجاه معك للبيع، بس السعر واقف عند القاع ومتشبّع — "
                   "الأحلى بيع ارتداد، مو بيع فوري هون.")
    elif bside == "sell":
        verdict = "الاتجاه هابط بوضوح — البيع مع الاتجاه هو الأفضل إحصائياً."
    elif bside == "buy" and isinstance(r15, (int, float)) and r15 >= 70:
        verdict = ("الاتجاه صاعد، بس السعر متشبّع شراء — "
                   "الأحلى شراء عند ارتداد، مو مطاردة.")
    elif bside == "buy":
        verdict = "الاتجاه صاعد بوضوح — الشراء مع الاتجاه هو الأفضل إحصائياً."
    else:
        verdict = "التايب مذبذب بلا اتجاه واضح — الأفضل الوقوف جانباً أو اللعب عالنطاق."
    if isinstance(adx_v, (int, float)) and adx_v < ADX_MIN:
        verdict = ("ADX منخفض — بلا ترند واضح. الأفضل اللعب عالنطاق "
                   "(اشترِ عند الدعم · بِع عند المقاومة) أو الوقوف جانباً.")

    # --- the honest note (the owner's one non-negotiable) ---
    if isinstance(r15, (int, float)) and r15 <= 30 and bside == "sell":
        honesty = (f"السوق متشبّع بيع بشدّة (RSI 15د {r15:.0f}). البيع مع الاتجاه "
                   "رابح إحصائياً، بس الدخول عند RSI تحت 30 هو أضعف شريحة. يعني: "
                   "البيع صحيح بس متأخر — خفّف الحجم أو اقعد برا.")
    elif isinstance(r15, (int, float)) and r15 >= 70 and bside == "buy":
        honesty = (f"السوق متشبّع شراء بشدّة (RSI 15د {r15:.0f}). الشراء مع الاتجاه "
                   "رابح إحصائياً، بس الدخول عند RSI فوق 70 هو أضعف شريحة. يعني: "
                   "الشراء صحيح بس متأخر — خفّف الحجم أو اقعد برا.")
    else:
        honesty = ("ما في ضمان — هاد قراءة آلية من بيانات السوق الحيّة، "
                   "والقرار قرارك. الوقف إلزامي دايماً.")

    return {"bias": bias, "side": bside, "verdict": verdict, "honesty": honesty,
            "scenarios": scen, "sell": sell, "buy": buy}


def map_insights(m: dict, live: float, res: list[float], sup: list[float],
                 emas: dict, news: list[dict]) -> list[dict]:
    """The analyst's EYE: the things I notice reading the tape that a fixed
    template does not -- each one a plain sentence, each one earned by a number.

    This is the honest answer to "can the map be as flexible as you?". It cannot
    think, but it CAN look at the same tells I look at and say them out loud:
      * a divergence between momentum and price (RSI higher low while price makes
        a lower low) -- the first sign a fall is tiring;
      * price sitting on an average (a hair-trigger 6/6 tilt that a few points
        would flip);
      * a level that price keeps respecting (a wall) vs a thin one;
      * the day's range position -- at the extreme, where late entries are worst;
      * a stretch far from the average (price is extended, a snap-back is due);
      * news that argues with the tape (dollar up / yields up vs gold down).
    Nothing here is a prediction or a probability: each is a FACT with its number,
    and the desk's own rules still decide the trades. No conviction is invented.
    """
    out: list[dict] = []
    trends = m.get("trends") or {}
    rsis = m.get("rsis") or {}
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    r15 = rsis.get("15m")

    # --- momentum divergence on 15m: RSI turning up while price makes a new low
    with suppress(Exception):
        b15 = closed(bars("15m", 300), "15m")
        if len(b15) > 40:
            cl = [x["close"] for x in b15]
            r_now = rsis.get("15m")
            r_prev = rsi(cl[:-8], 14)
            lo_now = min(cl[-8:])
            lo_prev = min(cl[-16:-8])
            if (r_now is not None and r_prev is not None
                    and lo_now < lo_prev and r_now > r_prev + 2):
                out.append({"kind": "دايفرجنس", "tone": "warn",
                            "text": (f"دايفرجنس إيجابي على 15د: السعر عمل قاع أدنى "
                                     f"({num(lo_now)}) بس RSI طلع ({r_prev:.0f}→{r_now:.0f}). "
                                     "أول إشارة إن الهبوط عم يتعب — راقب ارتداد.")})

    # --- price resting on an average: the 6/6 tilt is a hair-trigger
    with suppress(Exception):
        g = (m.get("gap") or {}).get("15m")
        if isinstance(g, (int, float)) and g < 0.25 and (up >= 5 or dn >= 5):
            side = "هابط" if dn >= 5 else "صاعد"
            out.append({"kind": "حساس", "tone": "info",
                        "text": (f"الاتجاه {side} 6/6 بس السعر ملزوق على المتوسط "
                                 f"(على بعد {g:.2f}×ATR). يعني الاتجاه هشّ — شوية نقاط "
                                 "بتقلبو. لا تعاند المستوى هون.")})

    # --- the day's range position: at the extreme, late entries are worst
    with suppress(Exception):
        q = m["q"]
        hi, lo = q.get("high"), q.get("low")
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi > lo:
            pos = (live - lo) / (hi - lo) * 100
            if pos <= 10:
                out.append({"kind": "موقع", "tone": "warn",
                            "text": (f"السعر عند {pos:.0f}% من نطاق اليوم — قريب من "
                                     "قاعه. بيع هون = مطاردة؛ الأحلى تنتظر ارتداد لفوق "
                                     "أو كسر واضح.")})
            elif pos >= 90:
                out.append({"kind": "موقع", "tone": "warn",
                            "text": (f"السعر عند {pos:.0f}% من نطاق اليوم — قريب من "
                                     "قمتو. شراء هون = مطاردة؛ الأحلى تنتظر ارتداد لتحت.")})

    # --- a stretch far from the average: price is extended
    with suppress(Exception):
        e = emas.get("15m") or {}
        a15 = (m.get("atrs") or {}).get("15m")
        if e.get("e20") and a15:
            dist = abs(live - e["e20"]) / a15
            if dist >= 2.0:
                out.append({"kind": "تمدّد", "tone": "info",
                            "text": (f"السعر بعيد {dist:.1f}×ATR عن متوسطه 15د — "
                                     "متمدّد. السوق المتمدّد بيرجع للمتوسط غالباً قبل "
                                     "ما يكمل، فانتبه للمطاردة.")})

    # --- a level price keeps respecting: a wall
    with suppress(Exception):
        b1 = bars("1h", 300)
        tol = max(1.0, (atr(b1) or 1.0) * 0.25)
        near = (res[:1] or []) + (sup[:1] or [])
        for lv in near:
            t = _touches(lv, b1, tol)
            if t >= 20:
                out.append({"kind": "حيط", "tone": "info",
                            "text": (f"مستوى {num(lv)} لمسو السعر {t} مرة — حيط قوي. "
                                     "اختراقو بإغلاق معناها كلام كبير.")})

    # --- news that argues with the tape (dollar/yields up vs gold falling)
    with suppress(Exception):
        titles = " ".join((n.get("title") or "") for n in news)
        if dn >= 5 and ("الدولار" in titles or "الفائدة" in titles):
            out.append({"kind": "خبر", "tone": "info",
                        "text": ("الأخبار فيها قوة دولار/رفع فائدة — هاد بيدعم الهبوط "
                                 "على الذهب، فالبيع مع الاتجاه متسق مع الخبر.")})
        elif up >= 5 and ("الدولار" in titles or "الفائدة" in titles):
            out.append({"kind": "خبر", "tone": "warn",
                        "text": ("الأخبار فيها قوة دولار/رفع فائدة — هاد ضد الصعود. "
                                 "انتبه: الشراء مع الاتجاه هون ممكن يخالف الخبر.")})

    return out[:5]


def price_tick() -> dict:
    """A LIGHT read: just the live quote. One HTTP call, no frames, no news.

    The map page wants the price to feel instant, but map_payload() reads six
    frames plus the news feed -- far too heavy to run every few seconds on a
    free instance. This is the honest split: the NUMBER ticks every few seconds
    from here, the ANALYSIS refreshes on its own slower beat.
    """
    q = quote()
    live = float(q.get("mid") or q.get("last") or 0)
    return {"live": live, "state": q.get("marketState"),
            "spread": q.get("spread"),
            "dayDiffPercent": q.get("dayDiffPercent"),
            "ts": datetime.now(UTC).isoformat()}


def map_payload() -> dict:
    """Every number the live map page needs, in ONE read.

    The web page must not re-derive anything: supply/demand zones, liquidity,
    the scenario trades and the plan all come from the same functions the
    Telegram cards call, so the page can never disagree with the bot. The page
    is a VIEW of the desk, not a second engine.

    Everything is JSON-serialisable (plain floats/strings/lists) and every
    sub-read is individually guarded: one dead frame or feed degrades that
    section to an empty list, it never blanks the page.
    """
    m = read_market()
    q, live = m["q"], m["live"]
    res, sup = _levels(live) if live > 0 else ([], [])
    sell_z, buy_z = _zones(live) if live > 0 else ([], [])
    liq_hi, liq_lo = _liquidity(live) if live > 0 else ([], [])
    # The map's DISPLAY levels are sticky: unioned with every level the page has
    # already named, so a refresh never drops one. The plan and the scenarios
    # below keep the FRESH levels -- a trade must not fire off a stale level.
    sticky = (_sticky_map(live, res, sup, sell_z, buy_z, liq_hi, liq_lo)
              if live > 0 else
              {"res": [], "sup": [], "sell": [], "buy": [],
               "liq_above": [], "liq_below": []})

    # The live plan (the same dict the action card shows), if one exists now.
    p = None
    with suppress(Exception):
        p = plan(m)

    # Scenario trades, priced by _sized() exactly as card_scenarios does.
    scen: list[dict] = []
    if live > 0:
        if res:
            trig = res[0] + 0.5
            s = _sized(trig, "long", res[0])
            scen.append({"side": "long", "trigger": trig, "level": res[0],
                         "next": res[1] if len(res) > 1 else None, **s})
        if sup:
            trig = sup[0] - 0.5
            s = _sized(trig, "short", sup[0])
            scen.append({"side": "short", "trigger": trig, "level": sup[0],
                         "next": sup[1] if len(sup) > 1 else None, **s})

    # EMAs per frame -- the levels the owner reads against the price. The raw
    # bars are kept so the report below reuses them (ONE tape read per frame).
    emas: dict = {}
    frame_bars: dict = {}
    for tf in FRAMES:
        with suppress(Exception):
            b = bars(tf, 300)
            c = [x["close"] for x in b]
            frame_bars[tf] = closed(b, tf)
            emas[tf] = {"e20": _ema_last(c, 20), "e50": _ema_last(c, 50)}

    # The late-entry caveat for the live plan, from the ONE helper every card
    # uses, so the page cannot show a late entry without saying so.
    side = None
    if p:
        side = "sell" if p["direction"] == "short" else "buy"
    note = _late_note(side, (m.get("rsis") or {}).get("15m"))

    # News: information only, same source the news card shows.
    news: list[dict] = []
    with suppress(Exception):
        items = list(_news_fetch()) if NEWS_LIVE else []
        items += [tuple(x) for x in NEWS_ITEMS]
        for it in items[:8]:
            if len(it) == 3:
                news.append({"tag": it[0], "title": it[1], "when": it[2]})
            elif len(it) == 2:
                news.append({"tag": it[0], "title": it[1], "when": ""})

    try:
        b15 = closed(bars("15m", 300), "15m")
        adx_v = adx_series(b15)[-1] if b15 else None
    except Exception:  # noqa: BLE001
        adx_v = None

    # The analysis layer: bias, named scenarios, the buy/sell playbook, verdict.
    analysis = None
    with suppress(Exception):
        analysis = map_analysis(m, live, res, sup, p, adx_v)
        # The extra tells I read by hand: fibo levels and momentum divergence.
        analysis["fibo"] = fib_levels(live)
        analysis["fibo_line"] = fib_read(live)
        analysis["divergence"] = divergence_read()

    # The analyst's eye: the tells a template misses, each earned by a number.
    insights: list[dict] = []
    with suppress(Exception):
        insights = map_insights(m, live, res, sup, emas, news)

    # The full report -- the SAME engine the bot's "التحليل التفصيلي" card reads,
    # so the page and the card can never disagree. Guarded: a failure drops the
    # section, it never blanks the page. It gets the SAME sticky display levels
    # the page's own level list shows, so the report's "المستويات" cannot differ
    # from the "خريطة المستويات" right above it. Pass the reads we already have
    # so the page pays for ONE tape read, not two.
    report = None
    with suppress(Exception):
        report = report_payload(m, sticky["res"], sticky["sup"], frame_bars)

    return {
        "symbol": SYMBOL,
        "live": live,
        "state": q.get("marketState"),
        "dayDiffPercent": q.get("dayDiffPercent"),
        "spread": q.get("spread"),
        "trends": m["trends"],
        "rsis": m["rsis"],
        "atrs": m["atrs"],
        "emas": emas,
        "res": sticky["res"][:6],
        "sup": sticky["sup"][:6],
        "sell_zones": [{"price": z, "touches": n} for z, n in sticky["sell"][:5]],
        "buy_zones": [{"price": z, "touches": n} for z, n in sticky["buy"][:5]],
        "liquidity": {"above": sticky["liq_above"][:3],
                      "below": sticky["liq_below"][:3]},
        "plan": p,
        "plan_side": side,
        "late_note": note,
        "scenarios": scen,
        "analysis": analysis,
        "insights": insights,
        "adx": adx_v,
        "news": news,
        "report": report,
        "clock": _clock(datetime.now(UTC)),
        "tz": TZ_LABEL,
    }


def _report_read(fr: dict, live: float) -> str:
    """One frame's sentence, derived from its own numbers -- not a template.

    The report must read the tape, not recite it: the trend, where price sits
    against the average, and what the momentum says. Every clause is a fact the
    frame carries, so the sentence changes the moment the frame does.
    """
    tr, r, e20, a = fr["trend"], fr["rsi"], fr["ema20"], fr["atr"]
    parts = [f"الاتجاه {'هابط' if tr == -1 else ('صاعد' if tr == 1 else 'عرضي')}."]
    if e20 is not None:
        parts.append(f"السعر {'فوق' if live > e20 else 'تحت'} EMA20 ({e20:.1f}).")
    if r is not None:
        if r >= 70:
            parts.append(f"RSI {r:.0f} — تشبّع شراء.")
        elif r <= 30:
            parts.append(f"RSI {r:.0f} — تشبّع بيع.")
        elif r < 45:
            parts.append(f"RSI {r:.0f} — زخم ضعيف.")
        elif r > 55:
            parts.append(f"RSI {r:.0f} — زخم قوي.")
        else:
            parts.append(f"RSI {r:.0f} — محايد.")
    if a:
        parts.append(f"ATR {a:.1f} نقطة.")
    return " ".join(parts)


def report_payload(m: dict | None = None,
                   res: list[float] | None = None,
                   sup: list[float] | None = None,
                   frames_bars: dict | None = None) -> dict:
    """The full analysis report -- ONE engine for the map, the bot and the page.

    The owner asked for the report he reads by hand to live on the map AND on
    the bot and update with the market. The only way the two can never disagree
    is to build it once here and let both read it, the same rule the map already
    follows. Everything is derived from the live tape; nothing is a study figure
    dressed up as a reading.

    `m`, `res`, `sup` and `frames_bars` may be supplied by a caller that already
    read the tape (map_payload does), so the page pays for ONE read, not two.
    """
    if m is None:
        m = read_market()
    q, live = m["q"], m["live"]
    if res is None or sup is None:
        res, sup = _display_levels(live)
    fb = frames_bars or {}

    frames: list[dict] = []
    for tf in FRAMES:
        fr: dict = {"tf": tf, "label": TF_AR.get(tf, tf),
                    "trend": m["trends"].get(tf, 0),
                    "rsi": m["rsis"].get(tf), "atr": m["atrs"].get(tf),
                    "ema20": None, "ema50": None}
        with suppress(Exception):
            b = fb.get(tf)
            if b is None:
                b = bars(tf, 300)
            c = [x["close"] for x in b]
            fr["ema20"] = _ema_last(c, 20)
            fr["ema50"] = _ema_last(c, 50)
        fr["read"] = _report_read(fr, live) if live > 0 else ""
        frames.append(fr)

    up = sum(1 for x in frames if x["trend"] == 1)
    dn = sum(1 for x in frames if x["trend"] == -1)
    if dn >= 5:
        bias = "هابط"
    elif up >= 5:
        bias = "صاعد"
    elif dn > up:
        bias = "مائل للهبوط"
    elif up > dn:
        bias = "مائل للصعود"
    else:
        bias = "عرضي"

    # The day's session ranges, so the report can say WHERE the move happened.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    b1h: list[dict] = fb.get("1h") or []
    if not b1h:
        with suppress(Exception):
            b1h = closed(bars("1h", 300), "1h")
    t1 = [x for x in b1h if x["openTime"][:10] == today]

    def _rng(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        hi = max(x["high"] for x in rows)
        lo = min(x["low"] for x in rows)
        return {"low": round(lo, 2), "high": round(hi, 2), "pts": round(hi - lo, 1)}

    sessions = {
        "asia": _rng([x for x in t1 if 0 <= int(x["openTime"][11:13]) < 7]),
        "london": _rng([x for x in t1 if 7 <= int(x["openTime"][11:13]) < 16]),
        "ny": _rng([x for x in t1 if 16 <= int(x["openTime"][11:13]) < 24]),
    }

    # The recent daily bars, so the report carries the multi-day run.
    days: list[dict] = []
    with suppress(Exception):
        for d in closed(bars("1d", 300), "1d")[-8:]:
            days.append({"date": d["openTime"][:10], "open": round(d["open"], 2),
                         "high": round(d["high"], 2), "low": round(d["low"], 2),
                         "close": round(d["close"], 2),
                         "chg": round(d["close"] - d["open"], 1)})

    levels: list[dict] = []
    for rv in res[:6]:
        levels.append({"price": round(rv, 2), "kind": "مقاومة",
                       "dist": round(rv - live, 1)})
    for sv in sup[:6]:
        levels.append({"price": round(sv, 2), "kind": "دعم",
                       "dist": round(sv - live, 1)})

    # The honest conclusion, built from the numbers -- never a fixed script.
    bullets: list[str] = []
    bullets.append(f"الاتجاه العام: {bias} ({dn} فريم هابط · {up} صاعد من 6).")
    if sup:
        bullets.append(f"أقرب دعم {sup[0]:.2f} — كسره لتحت يفتح نزول أعمق.")
    if res:
        bullets.append(f"أقرب مقاومة {res[0]:.2f} — إغلاق ساعة فوقها يعني ارتداد.")
    if sup and len(sup) > 1:
        bullets.append(f"الأهداف النازلة: {sup[0]:.2f} ثم {sup[1]:.2f}.")
    if res and len(res) > 1:
        bullets.append(f"الأهداف الصاعدة: {res[0]:.2f} ثم {res[1]:.2f}.")

    structure = ""
    with suppress(Exception):
        structure = _structure_read() or ""

    plan_d = None
    with suppress(Exception):
        plan_d = plan(m)

    out = {
        "symbol": SYMBOL,
        "live": live,
        "state": q.get("marketState"),
        "dayDiffPercent": q.get("dayDiffPercent"),
        "spread": q.get("spread"),
        "dayHigh": q.get("high"),
        "dayLow": q.get("low"),
        "bias": bias,
        "up": up,
        "dn": dn,
        "frames": frames,
        "levels": levels,
        "res": [round(x, 2) for x in res[:6]],
        "sup": [round(x, 2) for x in sup[:6]],
        "fib": fib_levels(live) if live > 0 else {},
        "fib_line": fib_read(live) if live > 0 else "",
        "divergence": divergence_read() if live > 0 else [],
        "sessions": sessions,
        "days": days,
        "bullets": bullets,
        "structure": structure,
        "plan": plan_d,
        "clock": _clock(datetime.now(UTC)),
        "tz": TZ_LABEL,
    }
    out["lines"] = _report_lines(out)
    return out


def _rsi_series(vals: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI over the whole series, aligned to `vals` (None before warmup).

    `rsi()` answers "what is RSI now"; a chart needs the whole line. Same
    recursion, one value per bar, so the last point equals `rsi()` to the float.
    """
    out: list[float | None] = [None] * len(vals)
    if len(vals) < n + 1:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += d if d > 0 else 0.0
        losses += -d if d < 0 else 0.0
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + (d if d > 0 else 0.0)) / n
        al = (al * (n - 1) + (-d if d < 0 else 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def chart_payload(tf: str = "15m", limit: int = 180) -> dict:
    """The candle chart the live map page draws.

    Every level on it comes from the SAME functions the Telegram cards use
    (`_zones`, `_liquidity`, `_levels`), so the chart cannot disagree with the
    bot. It carries no trade instruction of its own -- it is the tape, drawn.

    Bars are the CLOSED ones plus the live bar (the chart wants to show the
    forming candle), while the zones read the closed tape, exactly as the cards
    do. One dead frame degrades the level lists to empty; it never blanks the
    chart.
    """
    try:
        raw = bars(tf, limit + 1)
        q = quote()
    except Exception:  # noqa: BLE001 - a dead feed must not 500 the page
        return {"tf": tf, "bars": [], "rsi": [], "zones": {}, "liquidity": {}}
    b = raw[-(limit + 1):]
    closes = [x["close"] for x in b]
    # The live price is the QUOTE's, not the last bar's close -- the cards split
    # their zones on this same number, so the chart's zones land identically.
    live = float(q.get("mid") or q.get("last") or 0)
    day = b[-1]["openTime"][:10] if b else ""
    today = [x for x in b if x["openTime"][:10] == day]
    day_hi = max((x["high"] for x in today), default=None)
    day_lo = min((x["low"] for x in today), default=None)

    res, sup = _levels(live) if live > 0 else ([], [])
    sell_z, buy_z = _zones(live) if live > 0 else ([], [])
    liq_hi, liq_lo = _liquidity(live) if live > 0 else ([], [])
    # Same sticky display as the map page: a level drawn once stays drawn.
    sticky = (_sticky_map(live, res, sup, sell_z, buy_z, liq_hi, liq_lo)
              if live > 0 else
              {"res": [], "sup": [], "sell": [], "buy": [],
               "liq_above": [], "liq_below": []})

    return {
        "tf": tf,
        "live": live,
        "dayHigh": day_hi,
        "dayLow": day_lo,
        "bars": [{"t": x["openTime"], "o": x["open"], "h": x["high"],
                  "l": x["low"], "c": x["close"]} for x in b],
        "rsi": _rsi_series(closes),
        "zones": {
            "sell": [{"price": z, "touches": n} for z, n in sticky["sell"][:4]],
            "buy": [{"price": z, "touches": n} for z, n in sticky["buy"][:4]],
        },
        "liquidity": {"above": sticky["liq_above"][:3],
                      "below": sticky["liq_below"][:3]},
        "res": sticky["res"][:3],
        "sup": sticky["sup"][:3],
        "clock": _clock(datetime.now(UTC)),
        "tz": TZ_LABEL,
    }


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
    lines.append("<i>⚠️ معلومة فقط — مو إشارة. ما بفتح صفقة على الخبر، "
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


_NEWS_CACHE: dict = {"at": 0.0, "items": []}


def _news_one(query: str) -> list[tuple[datetime, str, str, str]]:
    """Gold/dollar headlines for ONE Google News query, newest first.

    The feed is keyed by language/region (NEWS_LANG/NEWS_REGION), so an Arabic
    query returns Arabic headlines. Tagging falls back to a neutral dot when no
    English keyword matches -- an Arabic headline is tagged by the query it came
    from (the first query is the price one), not by an English substring.
    """
    q = urllib.parse.quote(query)
    url = (f"https://news.google.com/rss/search?q={q}"
           f"&hl={NEWS_LANG}&gl={NEWS_REGION}&ceid={NEWS_REGION}:{NEWS_LANG}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "ignore")
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
        tag = ("🔥" if any(w in low for w in (
                   "cpi", "inflation", "fed", "rate",
                   "تضخم", "فائدة", "فيدرالي", "المركزي"))
               else "📈" if any(w in low for w in (
                   "rally", "gain", "rise", "high", "ارتفاع", "صعود", "يرتفع"))
               else "📉" if any(w in low for w in (
                   "fall", "drop", "slump", "low", "هبوط", "انخفاض", "ينخفض"))
               else "▫️")
        parsed.append((stamp, tag, title[:120], when))
    return parsed


def _news_keep(title: str) -> bool:
    """True if a headline is worth showing: it mentions gold/dollar AND is not
    a retail price or a different currency (see NEWS_NOISE)."""
    return not any(w in title for w in NEWS_NOISE)


def _news_fetch() -> list[tuple[str, str, str]]:
    """Best-effort gold headlines merged from several Google News queries.

    "Gold price" alone misses the drivers the owner asked for: the Fed's rate
    path, US inflation prints and the dollar index. Each focused query is
    fetched, merged, deduped by title and sorted newest first. Titles matching
    NEWS_NOISE (retail karat prices, other currencies) are dropped. Parsed with
    a regex, not an XML parser, so a malformed feed degrades to [] instead of
    raising. The result is cached for NEWS_CACHE_S seconds -- the feeds are
    slow and the RSS cards are tapped far more often than headlines change.
    """
    now = time.monotonic()
    if _NEWS_CACHE["items"] and now - _NEWS_CACHE["at"] < NEWS_CACHE_S:
        return list(_NEWS_CACHE["items"])
    merged: list[tuple[datetime, str, str, str]] = []
    for query in NEWS_QUERIES:
        with suppress(Exception):      # one dead query must not kill the rest
            merged += _news_one(query)
    merged.sort(key=lambda x: x[0], reverse=True)
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for _, tag, title, when in merged:
        if not _news_keep(title):
            continue
        key = title[:40].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((tag, title, when))
    _NEWS_CACHE["at"], _NEWS_CACHE["items"] = now, out
    return list(out)




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


def nochase_status(direction: str, b15: list[dict] | None = None) -> dict:
    """The no-chase gate: is this entry buying the top / selling the bottom?

    Selling into deep oversold (or buying into deep overbought) on the filter
    frame means entering AFTER the move, at the edge of the range -- the point
    the owner's own eye refused. It is read on a CLOSED bar only; on the forming
    bar the same rule measured a loss instead of a gain (see the constant).

    Fails OPEN when the reading is missing, like every other gate here: a data
    blip must not silence the desk.
    """
    side = "long" if direction == "long" else "short"
    if not NOCHASE_ON or side not in NOCHASE_SIDES:
        return {"ok": True, "rsi": None, "tf": NOCHASE_TF, "limit": None}
    try:
        b = b15 if NOCHASE_TF == "15m" and b15 is not None \
            else bars(NOCHASE_TF, 300)
        b = closed(b, NOCHASE_TF)
    except Exception:  # noqa: BLE001 - a broken feed must not silence the desk
        b = []
    r = rsi([x["close"] for x in b]) if len(b) > 15 else None
    if r is None:
        return {"ok": True, "rsi": None, "tf": NOCHASE_TF, "limit": None}
    limit = NOCHASE_RSI if side == "short" else 100.0 - NOCHASE_RSI
    ok = (r >= limit) if side == "short" else (r <= limit)
    return {"ok": ok, "rsi": r, "tf": NOCHASE_TF, "limit": limit}


def plan_check(m: dict | None = None) -> dict:
    """The one decision engine, WITH its reason -- the owner asked to hear why
    a trade is taken, and (just as important) why one is NOT.

    Returns {"plan": p or None, "reason": <code>, "detail": <owner-facing str>}.
    `plan()` is this function's "plan" field, so every card and the auto-desk
    read one engine and can never disagree.

    The gates, in the order they are checked. A gate that FAILS means NO trade
    -- the owner said "المهم تكون صفقة صحيحة": a trade must clear every gate or
    it is not taken.

      1. closed      -- the market is shut; never a signal on a dead price.
      2. no_signal   -- the frames do not agree and no fib wall was rejected.
      3. trend       -- the 15m candle disagrees with the direction (a long
                        while price sits under the 15m EMA20 = buying a falling
                        tape). His own journal: this kept every winner and
                        blocked every loser.
      4. gate        -- the quality gate (ADX band, ATR band) failed.
      5. chase       -- the entry is late: selling into deep oversold (or buying
                        into deep overbought) on the filter frame. The owner's
                        own eye refused this exact entry, and the backtest
                        agrees (see the NOCHASE_* constants).
    """
    m = m or read_market()
    q, live, trends = m["q"], m["live"], m["trends"]
    if not market_open(q) or live <= 0:
        return {"plan": None, "reason": "closed",
                "detail": "السوق مسكّر — ما بعطي إشارة على سعر ميت."}
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    res, sup = _levels(live)

    def sized(direction: str, level: float | None = None) -> dict:
        # A long's stop belongs just BELOW the nearest support; a short's just
        # ABOVE the nearest resistance. `level` overrides that when the caller
        # knows the exact wall the trade is betting against.
        if level is None:
            level = (max(sup) if sup else None) if direction == "long" \
                else (min(res) if res else None)
        p = _sized(live, direction, level)
        p["up"], p["dn"] = up, dn
        p["strong"] = (up if direction == "long" else dn) >= STRONG_FRAMES
        # Carry the 15m RSI so the open card can say the entry is late, the
        # same way every other card does. The plan dict is never persisted
        # (_write_plan stores only entry/stop/target), so this is a view field.
        p["r15"] = (m.get("rsis") or {}).get("15m")
        return p

    # 1. A confirmed trend, or the retracement of a dominant leg. Same source as
    #    the card, so the desk and the card cannot disagree.
    direction = None
    level = None
    if up >= CONFIRM_FRAMES:
        direction = "long"
    elif dn >= CONFIRM_FRAMES:
        direction = "short"
    else:
        trig = _wall_trigger(live)
        if trig is not None:
            direction, level, _nxt = trig
    if direction is None:
        return {"plan": None, "reason": "no_signal",
                "detail": (f"الفريمات ما اتفقت ({up}/6 صعود · {dn}/6 هبوط) "
                           f"وما في جدار فيبو مرفوض.")}

    p = sized(direction, level)
    side = "long" if direction == "long" else "short"
    # A wall trade (no frame agreement, the fib retracement was rejected) is by
    # nature COUNTER to the short frames -- so the 15m candle filter, which
    # demands the price agree with the entry, would veto every one of them. The
    # wall is its own confirmation: the 1h bar already touched the wall and
    # closed back on the leg's side. So the trend filter applies ONLY to a
    # frame-agreed entry, not to the wall.
    from_trend = up >= CONFIRM_FRAMES or dn >= CONFIRM_FRAMES

    # The 15m bars are read ONCE and shared by both filters, so a plan sized
    # from one read can never be judged against a different one -- and only when
    # a filter that needs them is actually live.
    b15 = None
    if (TREND_FILTER_ON and from_trend) or FILTERS_ON:
        try:
            b15 = bars(TREND_FILTER_TF, 300)
        except Exception:  # noqa: BLE001 - a broken feed must not silence the desk
            b15 = None

    # 2. The trend filter: the entry must agree with the 15m candle.
    if TREND_FILTER_ON and from_trend and not frame_agrees(side, live, b15):
        return {"plan": None, "reason": "trend",
                "detail": ("السعر على فريم 15د ضد اتجاه الصفقة "
                           "(الشمعة ما تأكدت) — ما بدخل ضد الشمعة.")}

    # 3. The quality gate (ADX/ATR), read on the 15m frame. Applied to every
    #    entry, the wall included: a chop tape is not worth trading either way.
    if FILTERS_ON:
        fs = filter_status(side, b15)
        if not fs["ok"]:
            return {"plan": None, "reason": "gate",
                    "detail": f"بوابة الجودة ما تحققت: {_filter_line(fs)}"}

    # 4. The no-chase gate: never SELL into deep oversold / BUY into deep
    #    overbought. The owner's own read refused exactly this entry ("selling
    #    from here is chasing"), and the backtest agrees (see the constants).
    cs = nochase_status(side, b15)
    if not cs["ok"]:
        return {"plan": None, "reason": "chase",
                "detail": (f"دخول متأخر — RSI({cs['tf']}) {cs['rsi']:.0f} "
                           f"{'تحت' if side == 'short' else 'فوق'} "
                           f"{cs['limit']:.0f}: السعر ركض وما بقى فيه مجال، "
                           f"ما بمشي ورا الحركة.")}
    p["nochase_rsi"] = cs["rsi"]
    p["nochase_tf"] = cs["tf"]
    return {"plan": p, "reason": "ok", "detail": ""}


def plan(m: dict | None = None) -> dict | None:
    """MY read, sized into a trade -- the desk's one decision engine.

    Thin wrapper over plan_check() so every existing caller keeps working while
    the reason travels with the decision. A trade exists ONLY when every gate
    passes (see plan_check); otherwise this returns None.
    """
    return plan_check(m)["plan"]


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
                    "<i>ما بعطي إشارة على سعر ميت.</i>")
        up = sum(1 for tf in FRAMES if m["trends"].get(tf) == 1)
        dn = sum(1 for tf in FRAMES if m["trends"].get(tf) == -1)
        lean, mark = _bias(m["trends"])
        if up >= CONFIRM_FRAMES or dn >= CONFIRM_FRAMES:
            way = "صاعد" if up >= CONFIRM_FRAMES else "هابط"
            return ("🎯 <b>التوصية</b> — XAUUSD\n"
                    f"<b>لا صفقة</b> — الاتجاه {way} بس السعر لسا ما وصل "
                    "لزناد الدخول.\n\n"
                    "<i>ما بدخل عالملاحقة — استنى يرتد للجدار.</i>")
        return ("🎯 <b>التوصية</b> — XAUUSD\n"
                f"<b>لا صفقة</b> — الفريمات ما اتفقت "
                f"({CONFIRM_FRAMES}/6 مطلوبة).\n"
                f"ميل القراءة: {mark} <b>{lean}</b>\n\n"
                "<i>ما بعطي إشارة لما السوق محتار.</i>")
    note = _late_note("sell" if p["direction"] == "short" else "buy",
                      m["rsis"].get("15m"))
    tail = f"\n⚠️ {note}" if note else ""
    return ("🎯 <b>التوصية</b> — " + SYMBOL + "\n" +
            "\n".join(_plan_lines(p)) + tail + "\n\n"
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
    if t["exit_price"] is None:
        return None
    pts = (t["exit_price"] - t["entry"]) if t["direction"] == "long" \
        else (t["entry"] - t["exit_price"])
    return pts * _size_of(t) * 100.0


def _size_of(t) -> float:
    """The lot a trade carries. Every trade has one -- the desk's own are LOT,
    the owner's are parsed -- so a missing size falls back to the desk's."""
    s = t["size"]
    return float(s) if s else LOT


def equity_now() -> float:
    """The account as it stands: START_CAPITAL plus the result of every CLOSED
    trade. An open trade is not counted until it settles -- unrealised P&L on a
    paper book is a guess, and the owner asked for the money that actually
    moved."""
    total = START_CAPITAL
    for t in all_trades():
        if t["status"] != "closed" or t["exit_price"] is None:
            continue
        total += _money(t) or 0.0
    return total


def _equity_after(t) -> float:
    """Running capital after `t` closed, walking the closed trades in order."""
    run = START_CAPITAL
    for x in sorted((x for x in all_trades() if x["status"] == "closed"),
                    key=lambda x: x["closed_at"] or ""):
        run += _money(x) or 0.0
        if x["id"] == t["id"]:
            break
    return run


CLOSE_UTC_HOUR = 21               # the gold session closes at 21:00 UTC


def _session_day(now=None):
    """(start_utc, local_date) of the trading day a report should cover.

    The market closes at 21:00 UTC -- which is midnight on the owner's clock
    (UTC+3). So a report that fires AT the close is standing in the first
    minutes of a brand-new local day, and the day it must describe is the one
    that just ENDED. Anchoring on the local calendar day, and stepping back one
    day when we are within a couple of hours of the close, keeps the report
    about the finished session instead of an empty new one.
    """
    now = now or datetime.now(UTC)
    loc = now.astimezone(timezone(timedelta(hours=TZ_OFFSET_H)))
    close_local_h = (CLOSE_UTC_HOUR + TZ_OFFSET_H) % 24
    if (loc.hour - close_local_h) % 24 < 2:
        loc -= timedelta(days=1)
    start_local = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC), start_local.date()


def _closed_since(start) -> list:
    """Closed trades at/after `start` (an aware datetime), earliest first."""
    out = []
    for t in all_trades():
        if t["status"] != "closed" or not t["closed_at"]:
            continue
        d = _parse(t["closed_at"])
        if d is not None and d >= start:
            out.append(t)
    return sorted(out, key=lambda t: t["closed_at"] or "")


def today_key() -> str:
    """The trading session's local date as a string -- the key a once-a-day
    notice is remembered under (the daily cap, the daily read)."""
    _, local_date = _session_day()
    return local_date.isoformat()


def trades_opened_today() -> int:
    """How many trades were OPENED in the current trading session.

    The daily cap reads this: the owner asked for at most MAX_TRADES_DAY trades
    a day. The window is the same trading SESSION the daily report uses
    (_session_day), so "today" means the session the market is in, not the
    calendar day that flips at midnight local.
    """
    start, _ = _session_day()
    n = 0
    for t in all_trades():
        d = _parse(t["opened_at"])
        if d is not None and d >= start:
            n += 1
    return n


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
    cap = (f"💰 <b>رأس المال: {equity_now():,.2f}$</b> "
           f"(البداية {START_CAPITAL:,.0f}$ · "
           f"الفرق {equity_now() - START_CAPITAL:+,.2f}$)")
    if not st.get("n"):
        return ["ما في صفقات مغلقة بهالفترة.", cap]
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
    out.append(cap)
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


def daily_report() -> str:
    """The end-of-day record the owner asked for: today's trades, and the
    capital they left behind. Sent once when the market closes.

    Money is what the account FELT -- start capital plus every closed trade --
    so the last line is a balance, not a running total the owner has to add up.
    The window is the trading SESSION (see _session_day), not the calendar day:
    the close lands on midnight locally, so the calendar day at the moment of
    sending is the empty new one, never the session that just ended.
    """
    grade()
    start, local_date = _session_day()
    rows = _closed_since(start)
    eq = equity_now()
    L = [f"📊 <b>سجل اليوم</b> — {local_date.strftime('%Y-%m-%d')} "
         f"<i>({TZ_LABEL})</i>", ""]
    if not rows:
        L.append("ما في صفقة انسكّرت اليوم.")
    else:
        day_money = 0.0
        for t in rows:
            m = _money(t) or 0.0
            day_money += m
            L.append(f"  {_line(t)}  → <b>{m:+,.2f}$</b>")
        L.append("")
        L.append(f"نتيجة اليوم: <b>{day_money:+,.2f}$</b> "
                 f"({len(rows)} صفقة)")
    L.append("")
    L.append(f"💰 <b>رأس المال: {eq:,.2f}$</b> "
             f"(البداية {START_CAPITAL:,.0f}$)")
    L.append(f"<i>الفرق: {eq - START_CAPITAL:+,.2f}$ — "
             f"الجمع والطرح من رأس المال نفسه.</i>")
    op = open_trades()
    if op:
        L.append(f"\n📂 لسا مفتوحة: <b>{len(op)}</b> — بتنحسب لما تنسكّر.")
    return "\n".join(L)


def card_open() -> str:
    grade()
    rows = open_trades()
    if not rows:
        return ("📂 <b>الصفقات المفتوحة</b>\n"
                "ما في صفقة مفتوحة هلّق — ما بحمل شي حالياً.\n\n"
                f"💰 <b>رأس المال: {equity_now():,.2f}$</b> "
                f"(البداية {START_CAPITAL:,.0f}$)")
    L = [f"📂 <b>الصفقات المفتوحة</b> — العدد <b>{len(rows)}</b>", ""]
    L += ["  " + _line(t) for t in rows]
    L.append("")
    L.append(f"💰 <b>رأس المال: {equity_now():,.2f}$</b> "
             f"(البداية {START_CAPITAL:,.0f}$)")
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
            "مع الوقف والهدف. <b>بسجّلها وبسجّل نتيجتها لحالي</b> — "
            "ما في داعي تسجّل شي.\n\n"
            "<b>قواعد الصفقات:</b>\n"
            "• صفقة أولى لمّا يتأكد الاتجاه (4/6 فريمات).\n"
            "• <b>صفقة وحدة بأي وقت</b> — ما بفتح تانية إلا لما "
            "تنسكّر الأولى (وقف أو هدف).\n"
            "• إذا كانت الإشارة قوية وبدك تزيد، سكّر الأولى بيدك أولاً.\n\n"
            "<b>بتقدر تسكّر صفقة بيدك</b> (لما تشوف السوق انقلب):\n"
            "«📂 الصفقات المفتوحة» ← اضغط <b>🔒 سكّر #</b>، أو ابعت "
            "<code>سكر 4</code>.\n\n"
            "<b>وكمان بتقدر تسجّل صفقتك بنفسك</b> (اختياري):\n"
            "<code>سجل شراء 4075 وقف 4069 هدف 4085 حجم 0.01</code>\n\n"
            "<b>رأس المال:</b> ببلّش من <b>$200</b>، وكل صفقة "
            "بينجمع/بينطرح ربحها أو خسارتها <b>من نفس رأس المال</b>. "
            "بتشوفه بكل تقرير، وبينبعتلك <b>سجل اليوم</b> مرة وحدة "
            "لمّا يسكّر السوق (اضغط «📊 سجل اليوم» بأي وقت).\n\n"
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
        p = v["plan"]
        note = _late_note("sell" if p["direction"] == "short" else "buy",
                          m["rsis"].get("15m"))
        tail = f"\n⚠️ {note}" if note else ""
        return (head + "🟢 <b>ادخل صفقة الآن</b> — الشروط مكتملة:\n" +
                "\n".join(_plan_lines(p)) + tail + "\n\n"
                "<i>سجّلها بالبوت أو بمنصتك — الوقف إلزامي.</i>")

    # wait: say why, and the single condition that flips it.
    if v.get("wall"):
        want = "بيع" if v.get("direction") == "short" else "شراء"
        why = (f"السعر بيمشي عكس موجة كبيرة — لسا ما لمس الجدار "
               f"<b>{num(v['wall'])}</b>.")
        todo = (f"⏸️ <b>استنى</b> يلمس <b>{num(v['wall'])}</b> ويغلق 1س {want}، "
                "وبعدها ادخل.")
    elif v["up"] >= CONFIRM_FRAMES:
        why = "الاتجاه صاعد، بس لسا ما في دخول مؤكد."
        todo = "⏸️ <b>استنى</b> إغلاق 1س يثبّت الاتجاه."
    elif v["dn"] >= CONFIRM_FRAMES:
        why = "الاتجاه هابط، بس لسا ما في دخول مؤكد."
        todo = "⏸️ <b>استنى</b> إغلاق 1س يثبّت الاتجاه."
    else:
        why = f"الفريمات ما اتفقت ({v['up']}ص · {v['dn']}هـ) — السوق محتار."
        todo = "⏸️ <b>استنى</b> يتفق 4 من 6 فريمات على اتجاه."
    return (head + "⏸️ <b>لا تدخل الآن.</b>\n"
            f"<i>{why}</i>\n\n"
            f"<b>شو تعمل:</b> {todo}\n"
            "<i>الواقف بأمان أحسن من الداخل غلط.</i>")


def _fib_retr(hi: float, lo: float, up: bool) -> list[tuple[str, float]]:
    """Fibonacci retracement levels of a swing.

    An UP leg (lo->hi) retraces DOWN from the high: 38.2% sits just under hi.
    A DOWN leg (hi->lo) retraces UP from the low: 38.2% sits just above lo.
    Getting the sides right matters -- the owner reads 38.2% as the shallow one.
    """
    span = hi - lo
    out = []
    for f, label in ((0.382, "38.2%"), (0.5, "50%"), (0.618, "61.8%")):
        out.append((label, hi - span * f if up else lo + span * f))
    return out


def _zigzag(h: list, l: list, thr: float) -> list[tuple[int, float, bool]]:
    """Alternating swing pivots (idx, price, is_high), moves smaller than `thr`
    ignored.

    A pivot from a fixed window (the k-th bar being the local max) turns every
    quiet stretch into a "swing", so a 6-point chop named a leg while a real
    135-point drop was ignored. A threshold makes a pivot mean a MOVE.
    """
    if len(h) < 2:
        return []
    out: list[tuple[int, float, bool]] = []
    up: bool | None = None
    ei, ext = 0, l[0]
    for i in range(1, len(h)):
        if up is None:
            if h[i] - l[ei] >= thr:
                up, ei, ext = True, i, h[i]
            elif l[ei] - l[i] >= thr:
                up, ei, ext = False, i, l[i]
        elif up:
            if h[i] > ext:
                ei, ext = i, h[i]
            elif ext - l[i] >= thr:
                out.append((ei, ext, True))
                up, ei, ext = False, i, l[i]
        else:
            if l[i] < ext:
                ei, ext = i, l[i]
            elif h[i] - ext >= thr:
                out.append((ei, ext, False))
                up, ei, ext = True, i, h[i]
    out.append((ei, ext, bool(up)))
    return out


def _big_leg(live: float) -> tuple[float, float, bool, float] | None:
    """The most recent multi-day swing: high, low, is it a DOWN leg, and where
    the price sits in it (0 = at the low, 1 = at the high).

    The day's range is not the leg. After a 135-point drop the next day's range
    is 40 points, so a retracement measured on the day says nothing -- the card
    read 'range' while the price sat at 38.2% of a real drop. Measuring the leg
    the owner can see on his chart is the difference between a read and a report.

    It takes the last two ALTERNATING swing pivots (a thresholded zigzag on 4h),
    not the window's global extremes: over ten days the extremes can be an old
    crash low and a later high, which names a leg the price has already left.
    The last swing is the one still in play.
    """
    try:
        b = bars("4h", 90)          # ~15 days
        if len(b) < 20:
            return None
        h = [x["high"] for x in b]
        l = [x["low"] for x in b]
    except Exception:  # noqa: BLE001 - a missing frame must not kill the card
        return None
    thr = 0.005 * (live or h[-1])   # 0.5%: noise stays out, real legs stay in
    piv = _zigzag(h, l, thr)
    if len(piv) < 2:
        return None
    # The LAST leg is often just a retracement of the one before it. The big
    # picture is the dominant swing, so when the current move is the smaller
    # counter-move, anchor to the prior leg -- that is the drop the owner sees
    # and the retracement he is trading against.
    if len(piv) >= 3:
        (_, pa, a_high), (_, pb, _b_high), (_, pc, _c_high) = (
            piv[-3], piv[-2], piv[-1])
        if abs(pc - pb) < abs(pb - pa):
            hi, lo = (pa, pb) if a_high else (pb, pa)
            if hi > lo:
                return hi, lo, a_high, (live - lo) / (hi - lo)
    (_pi, pp, p_high), (_ci, cp, c_high) = piv[-2], piv[-1]
    if p_high == c_high:            # not an alternating pair -- no clean leg
        return None
    hi, lo = (pp, cp) if p_high else (cp, pp)
    if hi <= lo:
        return None
    return hi, lo, p_high, (live - lo) / (hi - lo)


def _wall_trigger(live: float,
                  big: tuple | None = None) -> tuple[str, float, float] | None:
    """The wall trade, or None -- ONE trigger, so plan() and the card cannot
    disagree about whether the retracement has been rejected yet.

    Returns (direction, wall, next_level). The rule is the one the card prints:
    the price must have TOUCHED the fib wall and the last closed 1h bar must
    have closed back on the leg's side of it. That is the "بيع عند الجدار"
    moment; before it, the stance is to wait -- and a desk that opened at market
    would be exactly the disagreement the owner keeps catching.
    """
    big = big if big is not None else _big_leg(live)
    if big is None:
        return None
    bhi, blo, bdown, pos = big
    span = bhi - blo
    try:
        b1 = closed(bars("1h", 300), "1h")
    except Exception:  # noqa: BLE001 - a broken feed must not kill the read
        return None
    if not b1:
        return None
    last = b1[-1]
    if bdown and 0.25 <= pos <= 0.62:
        wall = blo + span * 0.382
        if last["high"] >= wall - ENTRY_NEAR_PTS and last["close"] < wall:
            return "short", wall, blo + span * 0.5
    elif not bdown and 0.38 <= pos <= 0.75:
        wall = bhi - span * 0.382
        if last["low"] <= wall + ENTRY_NEAR_PTS and last["close"] > wall:
            return "long", wall, bhi - span * 0.5
    return None


def _late_entry(side: str | None, r15) -> bool:
    """Is this entry joining a move that has mostly happened?

    The frames say buy/sell, but the 15m RSI is already stretched the OTHER way
    (selling a washed-out tape, buying an overbought one). ONE source for the
    caveat, so the action card and the opinion card cannot disagree about
    whether the entry is late -- the same reason plan() is one engine.
    """
    if not isinstance(r15, (int, float)):
        return False
    if side == "sell":
        return r15 <= CHASE_RSI
    if side == "buy":
        return r15 >= 100.0 - CHASE_RSI
    return False


def _late_note(side: str | None, r15) -> str:
    """The one-line honest caveat for a late entry, or '' when it is not late.
    Every card that shows a trade reads this, so none of them can show a late
    entry without saying so."""
    if not _late_entry(side, r15) or not isinstance(r15, (int, float)):
        return ""
    return (f"<b>دخول متأخر</b> — 15د RSI {r15:.0f}: الحركة مشيت أكترها. "
            "الصفقة صحيحة بس متأخرة — خفّف الحجم أو اقعد برا.")


def _opinion(live: float, side: str | None, agree: int,
             sell: list, buy: list, r15: float | None,
             mixed: bool, big: tuple | None = None,
             trig: tuple | None = None,
             plan: dict | None = None,
             thin: bool = False) -> list[str]:
    """The honest stance, up front: what I would DO, how sure, what flips me.

    This is the difference between a report and a view. It can say "wait" or
    "stay out"; it names a confidence; and it states the level that would prove
    it wrong. It never invents a probability -- the old "~74% / ~26%" was just
    50 + 6 * (frames agreeing), the frame count re-spelled as a percentage.

    `side` is the trend the frames agree on ("sell" for down, "buy" for up, None
    for a range); `agree` is how many frames agree; `mixed` is whether the 1h
    structure read as choppy. A tired move (15m RSI saturated) or a mixed
    structure drops the confidence a notch -- an honest read gets quieter when
    the tape does not support it.

    `big` is the multi-day swing (hi, lo, is_down, where price sits in it). When
    the frames disagree but the price sits at the edge of a real multi-day leg,
    "range, stay out" is wrong: the price is at a DECISION point, and the card
    must say which level decides it. That is the read the owner gets in words.
    """
    conf = "متوسطة"
    if agree >= 5:
        conf = "عالية"
    # `late` = the frames say sell/buy but the 15m RSI is already stretched the
    # other way, so this entry is joining a move that has mostly happened. The
    # desk still takes it (the trend is real, and blocking it threw away real
    # winners) -- but it drops the confidence AND says the entry is late, which
    # is the owner's own read of the tape.
    late = _late_entry(side, r15)
    # `thin` = price is resting on the averages, so the tilt is a hair-trigger.
    # It softens the WORD only; the decision is untouched. A "high confidence"
    # sell whose stop is the same 8 points that would flip the trend is not high
    # confidence -- it is a coin toss wearing a label.
    if late or mixed or thin:
        conf = {"عالية": "متوسطة", "متوسطة": "منخفضة"}.get(conf, conf)

    if side is None:
        # The wall trade fires the moment the rejection closes -- say so, and
        # with the SAME numbers plan() would size, because the desk is opening
        # it now. Anything else here is the card and the desk disagreeing.
        if trig is not None:
            d, wall, _nxt = trig
            s = _sized(live, d, wall)
            flip = ("فوق" if d == "short" else "تحت")
            if d == "short":
                head = (f"\n🔴 <b>موقفي: أبيع الآن</b> — الجدار "
                        f"<b>{num(wall)}</b> انرفض (إغلاق 1س تحته).")
            else:
                head = (f"\n🟢 <b>موقفي: أشتري الآن</b> — الجدار "
                        f"<b>{num(wall)}</b> ثبت (إغلاق 1س فوقه).")
            return [head,
                    f"  دخول <b>{num(s['entry'])}</b> · وقف "
                    f"<b>{num(s['stop'])}</b> · هدف <b>{num(s['target'])}</b> "
                    f"(−${s['risk_usd']:.0f}/+${s['gain_usd']:.0f}).",
                    f"  الثقة <b>{conf}</b> · 🔄 بيلغي نظريتي: إغلاق 1س "
                    f"{flip} {num(wall)}."]
        # A range on the frames can still be a DECISION on the chart: if the
        # price is retracing a real multi-day leg, name the fib wall and the
        # break, instead of a flat "stay out" that hides the setup.
        if big is not None:
            bhi, blo, bdown, pos = big
            if bdown and 0.25 <= pos <= 0.62:
                span = bhi - blo
                wall = blo + span * 0.382
                nxt = blo + span * 0.5
                return [f"\n⚖️ <b>موقفي: بستنى — مو عرضي</b> — ارتداد داخل "
                        f"هبوط (السعر عند <b>{pos * 100:.0f}%</b> من هبوط "
                        f"{span:.0f} نقطة).",
                        f"  الجدار <b>{num(wall)}</b> (فيبو 38.2%) هو يلي "
                        f"بيحسم: يرتد منه → <i>بيع</i> · إغلاق 1س فوقه → "
                        f"<i>انقلاب</i> لـ {num(nxt)}.",
                        f"  الثقة <b>{conf}</b> · 🔄 بيلغي نظريتي: إغلاق 1س فوق "
                        f"{num(wall)} يلغي الهبوط."]
            if not bdown and 0.38 <= pos <= 0.75:
                span = bhi - blo
                wall = bhi - span * 0.382
                nxt = bhi - span * 0.5
                return [f"\n⚖️ <b>موقفي: بستنى — مو عرضي</b> — تصحيح داخل "
                        f"صعود (السعر عند <b>{pos * 100:.0f}%</b> من صعود "
                        f"{span:.0f} نقطة).",
                        f"  الجدار <b>{num(wall)}</b> (فيبو 38.2%) هو يلي "
                        f"بيحسم: يثبت فوقه → <i>شراء</i> · إغلاق 1س تحته → "
                        f"<i>انقلاب</i> لـ {num(nxt)}.",
                        f"  الثقة <b>{conf}</b> · 🔄 بيلغي نظريتي: إغلاق 1س تحت "
                        f"{num(wall)} يلغي الصعود."]
        return ["\n🚫 <b>موقفي: ما بتداول هلّق</b> — السوق عرضي بلا اتجاه "
                "واضح.",
                f"  الثقة <b>{conf}</b> · اقعد برا واستنى اتجاه — التداول "
                "بالعرضي هوّ يلي ياكل الحساب."]

    if plan is not None:
        # A confirmed trend IS the trade: plan() opens it at market right now,
        # so the card must show plan()'s OWN numbers and say "now" -- never
        # "wait for the candle", which was the card and the desk disagreeing.
        d = "🔴 <b>أبيع الآن</b>" if side == "sell" else "🟢 <b>أشتري الآن</b>"
        way = "هابط" if side == "sell" else "صاعد"
        flip = "فوق" if side == "sell" else "تحت"
        caveat = " · خفّف الحجم أو اقعد برا" if conf == "منخفضة" else ""
        out = [f"\n🎯 <b>موقفي:</b> {d} — الاتجاه {way} ({agree}/6 فريمات).",
               f"  دخول <b>{num(plan['entry'])}</b> · وقف "
               f"<b>{num(plan['stop'])}</b> · هدف <b>{num(plan['target'])}</b> "
               f"(−${plan['risk_usd']:.0f}/+${plan['gain_usd']:.0f}).",
               f"  الثقة <b>{conf}</b>{caveat}"]
        if late:
            out.append("  ⚠️ " + _late_note(side, r15))
        if thin:
            out.append(f"  ⚠️ السعر ملاصق للمتوسطات — الاتجاه هامشي: حركة "
                       f"صغيرة بتقلبه. القرار ماشي، بس حجمك صغير.")
        out.append(f"  🔄 بيلغي نظريتي: إغلاق 1س {flip} {num(plan['stop'])}.")
        return out

    # No confirmed trend (or the market is closed / a trade is held): name the
    # zone we would act on, if there is one.
    if side == "sell":
        zone = sell[0][0] if sell else None
        near = zone is not None and abs(live - zone) <= ENTRY_NEAR_PTS
        if near:
            head = f"⏸️ <b>بستنى</b> — السعر عالمنطقة {num(zone)}، بدي إغلاق تأكيد"
            how = "بيع كسر مع الاتجاه"
        elif zone:
            head = f"⏸️ <b>بستنى أبيع</b> — يرتد لفوق لـ {num(zone)}"
            how = "بيع القوة (مع الاتجاه) — مو بيع كسر"
        else:
            head, how = "⏸️ <b>بستنى</b> — ما في منطقة بيع واضحة", ""
        inv = (f"إغلاق 1س فوق {num(zone)} يلغي الهبوط" if zone
               else "إغلاق فوق قمة اليوم يلغي الهبوط")
    else:
        zone = buy[0][0] if buy else None
        near = zone is not None and abs(live - zone) <= ENTRY_NEAR_PTS
        if near:
            head = f"⏸️ <b>بستنى</b> — السعر عالمنطقة {num(zone)}، بدي إغلاق تأكيد"
            how = "شراء مع الاتجاه"
        elif zone:
            head = f"⏸️ <b>بستنى أشتري</b> — ينزل لـ {num(zone)}"
            how = "شراء الانخفاض (مع الاتجاه)"
        else:
            head, how = "⏸️ <b>بستنى</b> — ما في منطقة شراء واضحة", ""
        inv = (f"إغلاق 1س تحت {num(zone)} يلغي الصعود" if zone
               else "إغلاق تحت قاع اليوم يلغي الصعود")

    lines = [f"\n🎯 <b>موقفي:</b> {head}"]
    if how:
        lines.append(f"  {how}")
    caveat = " · خفّف الحجم أو اقعد برا" if conf == "منخفضة" else ""
    lines.append(f"  الثقة <b>{conf}</b>{caveat}")
    lines.append(f"  🔄 بيلغي نظريتي: {inv}")
    return lines


def card_full() -> str:
    """MY OWN read of the market -- the agent's analysis, not the desk's verdict.

    Every tap goes to the live feed (read_market -> biquote, the same source the
    desk trades on) and rebuilds the whole read from scratch: the day's leg, the
    trend on every frame, momentum with saturation flags, the 1h structure, the
    support/resistance ladder around the price, Fibonacci retracements, the
    buy/sell zones, and MY call -- when to buy and when to sell, with the
    owner's $9/$20 sizing. It never prints the desk's gate; the owner asked for
    a read of the market, not the desk's permission.
    """
    m = read_market()
    live = m["live"]
    q = m["q"]
    atrs = m["atrs"]
    rsis = m["rsis"]
    names = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د",
             "15m": "15د", "5m": "5د"}
    icon = {1: "🟢", -1: "🔴", 0: "⚪"}
    up = sum(1 for tf in FRAMES if m["trends"].get(tf) == 1)
    dn = sum(1 for tf in FRAMES if m["trends"].get(tf) == -1)
    down = dn >= 4
    up_trend = up >= 4

    try:
        day_open = float(bars("1d", 5)[-1]["open"])
    except Exception:  # noqa: BLE001
        day_open = live

    L = [f"🥇 <b>قراءتي للذهب</b> — لحظياً",
         f"السعر <b>{num(live)}</b> · السوق "
         f"{'مفتوح ✅' if market_open(q) else 'مسكّر ⛔'} · 🕒 "
         f"{_clock(datetime.now(UTC))} {TZ_LABEL}"]

    # ---- 0. MY stance, up front: what I would do, how sure, what flips me -
    sell, buy = _zones(live)
    trend_side = "sell" if down else ("buy" if up_trend else None)
    agree = dn if down else (up if up_trend else 0)
    st = _structure_read()
    mixed = bool(st and "مختلطة" in st)
    big = _big_leg(live)
    trig = _wall_trigger(live, big)
    pnow = plan(m)
    # `thin` = the trend flips BEFORE the stop would: the price sits closer to
    # its 1h averages (the frame the flip is stated on) than the trade's own
    # stop distance. Then "high confidence" is a coin toss wearing a label. Read
    # only -- it softens the WORD, never plan() or the ladder.
    g1 = m["gap"].get("1h")
    a1 = m["atrs"].get("1h")
    thin = bool(pnow is not None and g1 is not None and a1
                and g1 * a1 < pnow["risk_pts"])
    L += _opinion(live, trend_side, agree, sell, buy, rsis.get("15m"), mixed,
                  big, trig, pnow, thin)

    # ---- 1. the day's picture -------------------------------------------
    hi, lo = q.get("high"), q.get("low")
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi > lo:
        drop = hi - lo
        pct = drop / hi * 100 if hi else 0
        L.append("\n<b>📉 الصورة:</b>")
        L.append(f"  فتح {num(day_open)} · قمة {num(hi)} · قاع {num(lo)}")
        L.append(f"  النطاق {drop:.0f} نقطة ({pct:.1f}%) · السعر عند "
                 f"<b>{(live - lo) / drop * 100:.0f}%</b> منه")

    # ---- 1b. the BIG picture: the multi-day leg the day's range hides ----
    # A 40-point day range inside a 135-point drop is not a range -- it is a
    # retracement, and its fib levels are what decide the next move. Without
    # this line the card called a real decision point "عرضي".
    if big is not None:
        bhi, blo, bdown, pos = big
        span = bhi - blo
        if span >= 3 * (atrs.get("1d") or 1.0) * 0.4:
            leg = "هبوط" if bdown else "صعود"
            a, b_ = (bhi, blo) if bdown else (blo, bhi)
            L.append("\n<b>🗻 الصورة الكبيرة:</b>")
            L.append(f"  آخر موجة <b>{leg}</b>: {num(a)} → {num(b_)} "
                     f"({span:.0f} نقطة) · السعر عند <b>{pos * 100:.0f}%</b> منها")
            fibs = _fib_retr(bhi, blo, not bdown)
            L.append("  فيبو: " + " · ".join(f"{k} <b>{num(v)}</b>"
                                             for k, v in fibs))

    # ---- 2. the frames ---------------------------------------------------
    L.append("\n<b>📊 الفريمات:</b>")
    for tf in FRAMES:
        r = rsis.get(tf)
        rt = f"RSI {r:.0f}" if isinstance(r, (int, float)) else "RSI —"
        L.append(f"  {names[tf]}: {icon[m['trends'].get(tf, 0)]} {rt}")
    if down:
        L.append(f"  → <b>هابط</b> ({dn}/6)")
    elif up_trend:
        L.append(f"  → <b>صاعد</b> ({up}/6)")
    else:
        L.append("  → <b>عرضي</b> (بلا اتجاه)")

    r15 = rsis.get("15m")
    r1h = rsis.get("1h")
    if isinstance(r15, (int, float)):
        if r15 <= 30:
            mom = ("تشبّع بيعي قوي — الهبوط متعب، احتمال ارتداد ⚠️"
                   if down else "تشبّع بيعي — احتمال ارتداد لفوق")
        elif r15 >= 70:
            mom = ("تشبّع شرائي قوي — الصعود متعب، احتمال تصحيح ⚠️"
                   if up_trend else "تشبّع شرائي — احتمال تصحيح")
        elif r15 >= 55:
            mom = "زخم صاعد"
        elif r15 <= 45:
            mom = "زخم هابط"
        else:
            mom = "محايد"
        r1 = f" · 1س {r1h:.0f}" if isinstance(r1h, (int, float)) else ""
        L.append(f"\n<b>⚡ الزخم:</b> 15د {r15:.0f}{r1} — <b>{mom}</b>")

    if st:
        L.append(f"\n<b>🧱 البنية:</b> {st}")

    # ---- 3. the levels: ONE block, not three ----------------------------
    # The ladder, the zones and Fibonacci are three views of the same prices.
    # Printing them as three sections padded the card without adding anything
    # (the same lesson the map taught). So they share one header, and the
    # Fibonacci line only appears when the price is actually near a level.
    L.append("\n<b>🧭 المستويات (خريطة واحدة):</b>")
    ls = _levels_with_strength(live)
    shown: set[float] = set()
    for x in ls:
        L.append(f"  {x}")
    for z, _n in sell[:3] + buy[:3]:
        shown.add(z)
    for x in ls:
        shown.update(float(m.group(1).replace(",", ""))
                     for m in re.finditer(r"([\d,]+\.\d+)", x))
    if sell:
        L.append("  🔴 بيع فوق: " + " · ".join(f"{num(z)} ({n})"
                                               for z, n in sell[:3]))
    if buy:
        L.append("  🟢 شراء تحت: " + " · ".join(f"{num(z)} ({n})"
                                                for z, n in buy[:3]))
    if not sell and not buy:
        L.append("  — ما في مناطق واضحة قريبة")
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi > lo:
        chg = q.get("dayDiffPercent")
        up_day = (chg >= 0) if isinstance(chg, (int, float)) else (live >= day_open)
        fibs = _fib_retr(hi, lo, up_day)
        near = min(fibs, key=lambda kv: abs(kv[1] - live))
        if abs(near[1] - live) <= max(3.0, (atrs.get("1h") or 10) * 0.4):
            L.append(f"  ⭐ السعر قريب من فيبو <b>{near[0]}</b> "
                     f"({num(near[1])}) — منطقة قرار <i>(نطاق اليوم)</i>")

    # ---- 5b. the raw swings, minus anything already named as a level -----
    tol_sw = max(1.0, (atrs.get("1h") or 10.0) * 0.25)
    sw = _swing_lines(live, shown, tol_sw)
    if sw:
        L.append("\n<b>🏔️ القمم والقيعان (المحاور — الجديدة):</b>")
        L += sw

    # ---- 6. what I am watching: the triggers, each with its target -------
    # The old section printed "~74% / ~26%". That number was 50 + 6 * (frames
    # agreeing) -- the frame count re-spelled as a percentage, with no
    # predictive content. An honest card names the levels that decide the next
    # move and where each one goes; it does not dress a count up as a
    # probability.
    L.append("\n<b>👀 السيناريوهات (بلا احتمال مخترع):</b>")
    atr1 = atrs.get("1h") or 10.0
    if down:
        below = [z for z, _n in buy if z < lo]
        above = [z for z, _n in sell if z > live]
        dn1 = max(below) if below else lo - atr1
        up1 = min(above) if above else live + atr1
        L.append(f"  <b>A</b> استمرار هبوط: كسر <b>{num(lo)}</b> بإغلاق → "
                 f"هدف <b>{num(dn1)}</b>")
        # B and C name the SAME resistance, and that is correct: it is where a
        # bounce dies OR where a close flips the trend. The card must say which
        # is which, or the two read as one. So B is a TOUCH that gets rejected,
        # C is a 1h CLOSE above it -- and each says so in words. Both levels are
        # the nearest resistance ABOVE the price; taking sell[0] instead picked
        # a zone the price had already left behind (below it), which is no
        # level at all.
        rev = up1
        rev_t = sorted(above)[1] if len(above) > 1 else rev + atr1
        L.append(f"  <b>B</b> ارتداد لفوق: يلمس <b>{num(rev)}</b> ويرتد منها → "
                 f"يرجع يهبط · <i>فرصة بيع</i>")
        L.append(f"  <b>C</b> انقلاب صاعد: إغلاق 1س <b>فوق</b> <b>{num(rev)}</b> "
                 f"<i>(إغلاق، مو مجرد لمس)</i> → هدف <b>{num(rev_t)}</b> · "
                 f"<i>وقّف البيع</i>")
    elif up_trend:
        below = [z for z, _n in buy if z < live]
        above = [z for z, _n in sell if z > hi]
        dn1 = max(below) if below else live - atr1
        up1 = min(above) if above else hi + atr1
        L.append(f"  <b>A</b> استمرار صعود: اختراق <b>{num(hi)}</b> بإغلاق → "
                 f"هدف <b>{num(up1)}</b>")
        # Mirror of the down branch: B and C share the nearest support BELOW the
        # price -- a touch that holds (still buy) vs a 1h close below it (flip
        # down). Taking buy[0] instead could name a support the price already
        # sits above, which is no level at all.
        rev = dn1
        rev_t = sorted(below)[-2] if len(below) > 1 else rev - atr1
        L.append(f"  <b>B</b> ارتداد لتحت: يلمس <b>{num(rev)}</b> ويرتد منها → "
                 f"يرجع يطلع · <i>فرصة شراء</i>")
        L.append(f"  <b>C</b> انقلاب هابط: إغلاق 1س <b>تحت</b> <b>{num(rev)}</b> "
                 f"<i>(إغلاق، مو مجرد لمس)</i> → هدف <b>{num(rev_t)}</b> · "
                 f"<i>وقّف الشراء</i>")
    else:
        # Not a clean trend -- but not necessarily a flat range either. If the
        # price is retracing a real multi-day leg, the DECISION is the fib wall
        # and the break of it, not an arbitrary "above/below the price" line.
        _big_ok = False
        if big is not None:
            bhi, blo, bdown, pos = big
            span = bhi - blo
            if span >= 3 * (atrs.get("1d") or 1.0) * 0.4 and 0.2 <= pos <= 0.75:
                _big_ok = True
                if bdown:
                    wall = blo + span * 0.382
                    nxt = blo + span * 0.5
                    L.append(f"  <b>A</b> الارتداد يكمل: إغلاق 1س فوق "
                             f"<b>{num(wall)}</b> → هدف <b>{num(nxt)}</b>")
                    L.append(f"  <b>B</b> الجدار يصمد: يلمس <b>{num(wall)}</b> "
                             f"ويرتد منها → رجوع للقاع <b>{num(blo)}</b> · "
                             f"<i>فرصة بيع</i>")
                    L.append(f"  <b>C</b> استمرار الهبوط: إغلاق 1س <b>تحت</b> "
                             f"<b>{num(blo)}</b> → هدف <b>{num(blo - atr1)}</b> · "
                             f"<i>وقّف الشراء</i>")
                else:
                    wall = bhi - span * 0.382
                    nxt = bhi - span * 0.5
                    L.append(f"  <b>A</b> التصحيح يكمل: إغلاق 1س تحت "
                             f"<b>{num(wall)}</b> → هدف <b>{num(nxt)}</b>")
                    L.append(f"  <b>B</b> الجدار يصمد: يلمس <b>{num(wall)}</b> "
                             f"ويرتد منها → رجوع للقمة <b>{num(bhi)}</b> · "
                             f"<i>فرصة شراء</i>")
                    L.append(f"  <b>C</b> استمرار الصعود: إغلاق 1س <b>فوق</b> "
                             f"<b>{num(bhi)}</b> → هدف <b>{num(bhi + atr1)}</b> · "
                             f"<i>وقّف البيع</i>")
        if not _big_ok:
            L.append(f"  <b>A</b> صعود: فوق <b>{num(live)}</b> → "
                     f"{num(live + atr1)}")
            L.append(f"  <b>B</b> هبوط: تحت <b>{num(live)}</b> → "
                     f"{num(live - atr1)}")
    L.append("  <i>مستويات قرار بأهدافها — مو احتمالات.</i>")

    # ---- 7. THE PLAN: the whole ladder of entries, sized, plus account ---
    # The owner asked for the same depth he gets in chat, live: not one entry
    # but the whole ladder with stop/target/risk, plus how it maps onto his
    # $200 / 0.01 lot. Everything here is computed from the live feed, never
    # hardcoded. A sell that needs a rally up to the zone is "بيع القوة"; a sell
    # at a zone the price is already on is "بيع كسر" -- naming it "بيع القوة"
    # there was simply wrong.
    L.append("\n<b>🎬 خطتي — كل الدخولات (لحظي من السوق):</b>")

    prim: list[dict] = []          # the priority entry's sizing, for the account line

    def _entry_line(entry: float, side: str, tag: str,
                    stop_level: float | None = None) -> str:
        s = _sized(entry, side, stop_level if stop_level is not None else entry)
        if not prim:
            prim.append(s)
        return (f"    • {tag}: <b>{num(entry)}</b> · وقف {num(s['stop'])} · "
                f"هدف {num(s['target'])} <i>(−${s['risk_usd']:.0f}/"
                f"+${s['gain_usd']:.0f})</i>")

    def _plan_line(p: dict, tag: str) -> str:
        # The priority entry IS the trade plan() is opening -- printed from
        # plan()'s own dict, never re-priced, so the card cannot quote a stop
        # the desk is not using.
        prim.append(p)
        return (f"    • {tag}: <b>{num(p['entry'])}</b> · وقف "
                f"{num(p['stop'])} · هدف {num(p['target'])} "
                f"<i>(−${p['risk_usd']:.0f}/+${p['gain_usd']:.0f})</i>")

    def _ladder(tags: list[tuple[float, str]], side: str) -> list[str]:
        out: list[str] = []
        seen: set[float] = set()
        for e, tag in sorted(tags, key=lambda t: -t[0]):
            if any(abs(e - x) < 3 for x in seen):
                continue
            seen.add(e)
            out.append(_entry_line(e, side, tag))
        return out

    if pnow is not None:
        # One trade, one set of numbers: the priority line is plan()'s trade.
        if pnow["direction"] == "short":
            L.append("  🔴 <b>البيع (الأولوية):</b>")
            basis = ("مع الاتجاه الهابط" if down else "الجدار (انرفض)")
        else:
            L.append("  🟢 <b>الشراء (الأولوية):</b>")
            basis = ("مع الاتجاه الصاعد" if up_trend else "الجدار (ثبت)")
        L.append(_plan_line(pnow, basis))
    elif down or (trig is not None and trig[0] == "short"):
        # No trade fired, but the bias is down: show the zones a sell would use.
        L.append("  🔴 <b>البيع (الأولوية):</b>")
        s_tags = [(sell[i][0], f"منطقة بيع {i + 1}")
                  for i in range(min(2, len(sell)))]
        if isinstance(lo, (int, float)):
            s_tags.append((lo, "بيع كسر تحت القاع"))
        L += _ladder(s_tags, "short")
    elif up_trend or (trig is not None and trig[0] == "long"):
        L.append("  🟢 <b>الشراء (الأولوية):</b>")
        b_tags = [(buy[i][0], f"منطقة شراء {i + 1}")
                  for i in range(min(2, len(buy)))]
        L += _ladder(b_tags, "long")
    elif big is not None and big[2] and 0.25 <= big[3] <= 0.62:
        wall = big[1] + (big[0] - big[1]) * 0.382
        L.append(f"  ارتداد داخل هبوط → بِع عند الجدار <b>{num(wall)}</b> · "
                 f"لا تشتري الارتداد")
        L.append(f"    • الزناد: لمس <b>{num(wall)}</b> + إغلاق 1س تحته")
    elif big is not None and not big[2] and 0.38 <= big[3] <= 0.75:
        wall = big[0] - (big[0] - big[1]) * 0.382
        L.append(f"  تصحيح داخل صعود → اشترِ عند الجدار <b>{num(wall)}</b> · "
                 f"لا تبيع التصحيح")
        L.append(f"    • الزناد: لمس <b>{num(wall)}</b> + إغلاق 1س فوقه")
    else:
        L.append("  السوق عرضي → <b>اشترِ عند الدعم · بِع عند المقاومة</b>")

    # -- account ---------------------------------------------------------
    # The numbers here MUST be the priority entry's own, not the config
    # defaults: a $9 line beside a −$12 entry is the card contradicting itself.
    if prim:
        s = prim[0]
        L.append(f"  💰 <b>حسابك ${ACCOUNT_USD:.0f} · {LOT} لوت:</b> نقطة = "
                 f"${POINT_VALUE:.0f} · خطر ${s['risk_usd']:.0f} "
                 f"({s['risk_usd'] / ACCOUNT_USD * 100:.1f}%) · مكسب "
                 f"${s['gain_usd']:.0f} · R:R {s['rr']:.1f}")
    else:
        L.append(f"  💰 <b>حسابك ${ACCOUNT_USD:.0f} · {LOT} لوت:</b> نقطة = "
                 f"${POINT_VALUE:.0f} · خطر ${STOP_PICK_USD:.0f} "
                 f"({STOP_PICK_USD / ACCOUNT_USD * 100:.1f}%) · مكسب "
                 f"${STOP_PICK_USD * TARGET_RR:.0f} · R:R {TARGET_RR:.1f}")
    L.append("  ⚠️ <i>صفقة واحدة فقط · الوقف مقدّس</i>")

    # ---- 8. the news that moves gold vs the dollar ----------------------
    items: list[tuple] = []
    if NEWS_LIVE:
        with suppress(Exception):      # a dead feed must not hide the read
            items = list(_news_fetch())
    items += [tuple(x) for x in NEWS_ITEMS]
    if items:
        L.append("\n<b>📰 الأخبار (تأثر الذهب/الدولار):</b>")
        seen_t: set[str] = set()
        # Three headlines, not six: the news is context, not the call, and it
        # was eating a fifth of the card while every line ended in "معلومة فقط".
        for it in items[:3]:
            tag, title = it[0], it[1]
            when = it[2] if len(it) > 2 else ""
            key = str(title)[:40]
            if key in seen_t:
                continue
            seen_t.add(key)
            stamp = f" <i>({when})</i>" if when else ""
            L.append(f"  {tag} {esc(str(title)[:100])}{stamp}")
        L.append("  <i>معلومة فقط — مو إشارة دخول.</i>")

    # ---- 9. honest close -------------------------------------------------
    vol = atrs.get("1h")
    L.append("\n<b>⚠️ كلمة صريحة:</b>")
    if isinstance(vol, (int, float)) and live:
        vp = vol / live * 100
        L.append(f"  تقلّب 1س {vol:.1f} نقطة ({vp:.2f}%) — "
                 f"{'عالي، خطر مرتفع' if vp > 0.6 else 'معتدل'}")
    _stop_usd = f"${prim[0]['risk_usd']:.0f}" if prim else f"${STOP_PICK_USD:.0f}"
    L.append(f"  الالتزام بالوقف ({_stop_usd}) أهم من أي توصية.")
    # The win-rate line: the owner's OWN closed trades first (the live truth),
    # and only when he has none does the desk's backtest stand in -- labelled as
    # a backtest, so a study figure is never read as a measured one.
    if SHOW_STUDY:
        try:
            jn = stats("all")
        except Exception:  # noqa: BLE001 - the journal must not kill the card
            jn = {"n": 0}
        if jn.get("n"):
            L.append(f"  📊 <b>نسبتك الحقيقية:</b> {jn['n']} صفقة · "
                     f"فوز {jn['win_rate']:.0%} · مجموع {jn['total_r']:+.1f}R")
        else:
            L.append(f"  📊 <b>نسبة الفوز (باكتست سنة، 796 صفقة):</b> "
                     f"~{STUDY_WIN_RATE:.0%} — تخسر أكتر مما تربح · أطول سلسلة "
                     f"خسارة {STUDY_LOSS_STREAK} صفقة")
            L.append("  <i>هاد رقم دراسة مو رقمك — سجّل صفقاتك ليصير عندك "
                     "سجل حقيقي.</i>")
    L.append("\n<i>قراءتي الفنية من بيانات السوق الحيّة — مو وعد ربح.</i>")
    return "\n".join(L)


def _study_edge() -> dict | None:
    """Measure the desk's timing edge and calibration on the 2026 replay.

    DEV/VALIDATION ONLY -- the owner keeps study numbers off the live cards, so
    this feeds `--score`, never a market card. Returns None when the engine or
    its data is missing, so the score says 'unmeasured' rather than guessing.

    edge = the t-stat of the mean R over the replay (a timing edge needs t >= 2;
    a t near 1 is indistinguishable from luck). calibration = the win rate vs
    the break-even win rate implied by the reward:risk ratio.
    """
    try:
        here = Path(__file__).resolve().parent
        sys.path.insert(0, str(here / "backtest"))
        import bot2026  # type: ignore[import-not-found]
        # The SHIPPED config, or this measures a bot nobody trades: the quality
        # gate (filters) is the biggest determinant of the replay's numbers.
        trades, _ = bot2026.simulate(filters=True, nochase=True)
        rr = float(bot2026.RR)
    except Exception:  # noqa: BLE001 - a missing study must not break the score
        return None
    closed = [t for t in trades if t.get("result")]
    n = len(closed)
    if n < 30:
        return None
    rs = [float(t["R"]) for t in closed]
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1)
    sd = var ** 0.5
    tstat = mean / (sd / n ** 0.5) if sd else 0.0
    wins = sum(1 for t in closed if t["result"] == "target")
    return {"n": n, "mean": mean, "t": tstat,
            "win": wins / n, "be": 1.0 / (1.0 + rr)}


def card_score() -> str:
    """Grade the card by MEASURING it -- never by opinion.

    The lesson from the map and the acceptance work: a score the author gives
    his own work is decoration. So every point below is a computation over the
    rendered card, printed with the number it measured. The /100 is the CARD's
    build quality -- all structural. The two market facts (timing edge,
    calibration) are MEASURED and printed BELOW the score as information, not
    as points: the card can be perfect while the edge is not proven, and mixing
    the two would let a good layout hide a market fact.
    """
    m = read_market()
    live = m["live"]
    card = card_full()
    plain = re.sub(r"<[^>]+>", "", card)

    rows: list[tuple[str, float, float]] = []  # name, got, max

    # -- 1. inside Telegram's hard limit (4096). A card that cannot be sent
    #       scores nothing else. ----------------------------------------------
    n = len(card)
    rows.append((f"الحجم ≤ 4096 (الطول {n})", 20 if n <= 4096 else 0, 20))

    # -- 2. every promised section is present --------------------------------
    want = ["قراءتي للذهب", "موقفي", "الصورة", "الفريمات", "الزخم", "البنية",
            "المستويات", "السيناريوهات", "خطتي", "حسابك"]
    miss = [w for w in want if w not in plain]
    rows.append((f"الأقسام ({len(want) - len(miss)}/{len(want)})",
                 20 * (len(want) - len(miss)) / len(want), 20))

    # -- 3. DUPLICATION: a number printed twice is padding. Count how many of
    #       the swing numbers also appear in the levels block. ----------------
    def _nums(seg: str) -> list[float]:
        return [float(x.replace(",", ""))
                for x in re.findall(r"\d,\d\d\d\.\d\d|\d,\d\d\.\d\d", seg)]
    lev_seg = plain.split("المستويات")[-1].split("القمم")[0]
    sw_seg = plain.split("القمم والقيعان")[-1].split("السيناريوهات")[0]
    lev, sw = set(_nums(lev_seg)), _nums(sw_seg)
    dup = sum(1 for x in sw if any(abs(x - y) <= 2.0 for y in lev))
    dup_pct = (dup / len(sw)) if sw else 0.0
    rows.append((f"تكرار القمم/القيعان ({dup}/{len(sw)})",
                 15 * (1 - dup_pct), 15))

    # -- 4. SCENARIOS: A, B, C present, and B/C must be told apart (a touch vs
    #       a close). The owner's own defect report. --------------------------
    sc = plain.split("السيناريوهات")[-1].split("خطتي")[0]
    sc_ok = (("A" in sc and "B" in sc and "C" in sc)
             and "يلمس" in sc and "إغلاق 1س" in sc)
    rows.append(("السيناريوهات A/B/C واضحة", 15 if sc_ok else 0, 15))

    # -- 5. HONESTY: no invented probability, the invalidation line is there,
    #       and any win-rate line names its SOURCE (باكتست or his own). --------
    honest = 0
    if "احتمال مخترع" in plain or "مو احتمالات" in plain:
        honest += 1
    if "بيلغي نظريتي" in plain:
        honest += 1
    if "نسبة الفوز" not in plain or "باكتست" in plain or "نسبتك الحقيقية" in plain:
        honest += 1
    if "مو وعد ربح" in plain:
        honest += 1
    rows.append((f"الصدق ({honest}/4)", 15 * honest / 4, 15))

    # -- 6. SIZED PLAN: every entry carries BOTH a stop and a target, and the
    #       account block is there. "A trade without a stop is not a trade", so
    #       the invariant is per-ENTRY, not a count of two -- the desk's own
    #       rule is ONE trade at a time, and a single sized entry is complete.
    #       (The old ">=2 entries" proxy contradicted that rule.) -------------
    plan = plain.split("خطتي")[-1].split("الأخبار")[0]
    bullets = [ln for ln in plan.splitlines() if ln.strip().startswith("•")]
    each_ok = bool(bullets) and all("وقف" in b and "هدف" in b for b in bullets)
    plan_ok = each_ok and "حسابك" in plan and "R:R" in plan
    n_stop = sum(1 for b in bullets if "وقف" in b)
    rows.append((f"الخطة مسعّرة ({len(bullets)} دخول · {n_stop} بوقف وهدف)",
                 15 if plan_ok else 0, 15))

    got = sum(r[1] for r in rows)
    mx = sum(r[2] for r in rows)
    L = ["🧮 <b>تقييم الزر — قياس مو رأي</b>",
         f"السعر <b>{num(live)}</b> · الطول <b>{n}</b>/4096", ""]
    for name, g, x in rows:
        mark = "✅" if g >= x else "❌"
        L.append(f"  {mark} {name}: <b>{g:.0f}</b>/{x:.0f}")
    L.append("")
    L.append(f"  🎯 <b>بناء البطاقة: {got:.0f} / {mx:.0f}</b>")
    if miss:
        L.append(f"  ⚠️ ناقص: {' · '.join(miss)}")
    if dup:
        L.append(f"  ⚠️ {dup} رقم مكرر بالمحاور.")
    L.append("  <i>كل نقطة محسوبة من البطاقة — مو رأيي.</i>")

    # -- the two market facts, measured, printed as INFORMATION (not points) --
    L.append("")
    L.append("<b>🔬 حقائق السوق (قياس، مو من الـ100):</b>")
    st = _study_edge()
    if st is None:
        L.append("  ⚪ الحافة/المعايرة: ما في بيانات دراسة هلّق.")
    else:
        t = st["t"]
        verdict = ("معنوية ✅" if t >= 2 else
                   "قريبة (تحتاج t≥2) 🟠" if t >= 1 else "غير معنوية ❌")
        L.append(f"  🔵 الحافة الزمنية: t=<b>{t:+.2f}</b> على {st['n']} صفقة "
                 f"· متوسط {st['mean']:+.3f}R → <b>{verdict}</b>")
        gap = (st["win"] - st["be"]) * 100
        L.append(f"  🔵 المعايرة: فوز {st['win']:.1%} مقابل تعادل "
                 f"{st['be']:.1%} → فرق <b>{gap:+.1f}</b> نقطة")
        L.append("  <i>الحافة تحتاج t≥2 · وهاد رقم دراسة (2026) — مو رقمك.</i>")
    return "\n".join(L)


def alerts_guide() -> str:
    """The map of every alert the desk sends and what to do about each."""
    return ("🔔 <b>دليل التنبيهات</b> — " + SYMBOL + "\n"
            "هاي كل رسالة رح ابعتها، وشو تعمل معها 👇\n\n"
            "<b>1️⃣ صفقة جديدة</b> — «لقطت صفقة»\n"
            "  يعني: الشروط اكتملت وفتح صفقة.\n"
            "  👉 <i>ما تعمل شي — أنا بيديرها. لو بدك، سجّلها بمنصتك.</i>\n\n"
            "<b>2️⃣ انحسمت صفقة</b> — هدف / وقف / يدوي\n"
            "  يعني: الصفقة سكّرت والنتيجة محسوبة بالـR.\n"
            "  👉 <i>راجع النتيجة، وأنا رح افتح جديدة إذا تجددت الشروط.</i>\n\n"
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
            "  يعني: شمعة 15د سكّرت فوق/تحت EMA20 (فلتري).\n"
            "  👉 <i>إذا الاتجاه العام بنفس الجهة = فرصة دخول.</i>\n\n"
            "<b>9️⃣ رفض صفقة</b> ⚠️\n"
            "  يعني: إشارة جديدة بس ما دخلت (الكتاب ممتلئ، أو "
            "الصفقة الحالية متقدمة/رابحة).\n"
            "  👉 <i>ما تعمل شي — حماية من المخاطرة، مو خطأ.</i>\n\n"
            "<b>🔟 تغيّر الاتجاه</b> 🔄\n"
            "  يعني: صافي اتجاه الفريمات انقلب (صاعد/هابط/عرضي) — تلقائياً "
            "بلا ما تكبس.\n"
            "  👉 <i>حدّث نظرتك: صاعد = شراء الانخفاض · هابط = بيع القوة. "
            "(تنبيه وحدة كل 20 دقيقة كحد أقصى.)</i>\n\n"
            "<b>1️⃣1️⃣ قرب منطقة دخول</b> 🎯\n"
            "  يعني: السعر وصل قرب منطقة بيع أو شراء من مناطق قراءتي.\n"
            "  👉 <i>جهّز أمرك: الرسالة فيها الدخول والوقف والهدف. استنى "
            "تأكيد الشمعة — الوقف إلزامي.</i>\n\n"
            "📌 <b>القاعدة الذهبية:</b> كل تنبيه بيقوللك «شو تعمل» بسطر — "
            "اقرأ آخر سطر دايماً.\n\n"
            "<i>قراءة تحليلية، مو وعد ربح.</i>")


def dispatch(action: str) -> str:
    return {
        "analysis": card_analysis,
        "full": card_full,
        "action": card_action,
        "alerts": alerts_guide,
        "recommend": card_recommend,
        "scenarios": card_scenarios,
        "levels": card_levels,
        "map": card_map,
        "report": card_report,
        "news": card_news,
        "open": card_open,
        "monitor": card_monitor,
        "journal": full_journal,
        "journal_menu": journal_menu_text,
        "j_day": lambda: period_report("day"),
        "j_week": lambda: period_report("week"),
        "j_month": lambda: period_report("month"),
        "daily": daily_report,
        "day_analysis": daily_analysis,
        "howto": help_text,
        "help": help_text,
        "collapse": lambda: "📊 <b>مكتب تحليل السوق</b>\nالقائمة مطويّة — اضغط «☰ القائمة» لعرض الأزرار.",
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


def _desk_chats(allow: set[str]) -> list[str]:
    """Who hears the DESK's own messages: a trade opened, closed, refused, or
    watched; the day's record and the account.

    Those carry the owner's money -- entry, stop, size, the balance -- so with
    OWNER_ONLY they go to the owner ALONE. A guest on the bot must not read the
    owner's book. Without OWNER_ONLY the desk stays as it was: everyone allowed.
    """
    if OWNER_ONLY:
        return [_backup_chat()]
    return sorted(allow)


def _alert_chats(allow: set[str]) -> list[str]:
    """Who hears the market ALERTS: a level breaking, the direction flipping, a
    gate opening, price reaching a zone.

    These are facts about the market, not about the owner's account, so they
    stay for everyone allowed -- that is the half of the bot a guest is here for.
    """
    return sorted(allow)


def _dump_journal() -> dict:
    return {"version": 1, "symbol": SYMBOL, "saved_at": _utcnow(),
            "trades": [dict(t) for t in all_trades()],
            "allow": sorted(load_allow()),
            "daily": _load_daily(),
            "sticky": _sticky_load()}


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
    daily = blob.get("daily") or {}
    if daily and not DAILY_PATH.exists():
        _save_daily(daily)
    # The sticky pool outlives the container too: a restart must not forget the
    # levels the owner was watching, so restore it when the file is absent.
    sticky = blob.get("sticky")
    if isinstance(sticky, dict) and not STICKY_PATH.exists():
        _sticky_save(sticky)
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
    # Folding the list edits the message that carries it: no new message, so the
    # owner's chat is not filled with menu copies. The card itself stays.
    if action == "collapse" and cq is not None:
        mid = ((cq.get("message") or {}).get("message_id"))
        if mid:
            tg.edit_markup(chat, mid, kb(COLLAPSE_BTN))
        return "collapse"
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


def _pending_load() -> dict | None:
    """The one pending pullback order, or None. Corrupt/absent reads as None."""
    try:
        d = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("side") else None


def _pending_save(d: dict) -> None:
    try:
        PENDING_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _pending_clear() -> None:
    try:
        PENDING_PATH.unlink()
    except OSError:
        pass


def _pullback_limit(p: dict, m: dict | None = None) -> float | None:
    """The limit price for a pullback entry, or None if ATR is unavailable.

    A short sells PULLBACK_ATR * ATR(15m) ABOVE the signal price; a long buys
    the same distance BELOW it. ATR is read on the CLOSED 15m bar, like every
    other gate -- a limit set from a forming bar would chase a price that has
    not happened.
    """
    m = m or read_market()
    try:
        b15 = closed(bars(TREND_FILTER_TF, 300), TREND_FILTER_TF)
        a = atr(b15)
    except Exception:  # noqa: BLE001
        a = None
    if not a:
        return None
    d = -1 if p["direction"] == "short" else 1
    return p["entry"] - d * PULLBACK_ATR * a


def _pending_plan(p: dict, limit: float) -> dict:
    """Re-size a plan around the LIMIT price, so entry/stop/target all move
    together and the risk window ($8-12) and R:R are preserved."""
    q = _sized(limit, p["direction"], None)
    for k in ("up", "dn", "strong", "r15", "nochase_rsi", "nochase_tf"):
        if k in p:
            q[k] = p[k]
    q["signal_entry"] = p["entry"]
    return q


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


def _why_taken(p: dict, m: dict | None = None) -> list[str]:
    """The WHY a trade is being taken -- the conditions that just came true, in
    the owner's language. This is the "ليش" he asked to hear before a trade: the
    same facts the gates checked, printed as the reason they passed.
    """
    m = m or read_market()
    r = m.get("rsis") or {}
    a = m.get("atrs") or {}
    side = "long" if p["direction"] == "long" else "short"
    fs = filter_status(side)
    rd = fs["readings"]
    out = []
    # 1. The trend: how many frames agree.
    agree = p.get("dn") if side == "short" else p.get("up")
    way = "هابط" if side == "short" else "صاعد"
    out.append(f"✅ الاتجاه {way} ({agree}/6 فريمات متفقين)")
    # 2. The 15m candle agreement.
    if TREND_FILTER_ON:
        out.append("✅ السعر متوافق مع شمعة 15د (مع الاتجاه)")
    # 3. The quality gate readings, printed as the numbers that passed.
    if FILTERS_ON:
        bits = []
        if rd.get("adx") is not None and "adx" in FILTER_GATES:
            bits.append(f"ADX {rd['adx']:.0f}")
        if rd.get("atr_pct") is not None and "atr" in FILTER_GATES:
            bits.append(f"ATR {rd['atr_pct']*100:.2f}%")
        if bits:
            out.append("✅ بوابة الجودة: " + " · ".join(bits))
    # 4. The no-chase check, printed as the reading that passed.
    if NOCHASE_ON and p.get("nochase_rsi") is not None:
        out.append(f"✅ ما بمشي ورا الحركة: RSI({p.get('nochase_tf', NOCHASE_TF)}) "
                   f"{p['nochase_rsi']:.0f}")
    # 5. The level the stop leans on.
    if p.get("rr"):
        out.append(f"✅ R:R {p['rr']:.2f} (فوق الحد الأدنى {MIN_RR})")
    return out


def _pre_trade_note(p: dict, m: dict | None = None) -> str:
    """The heads-up BEFORE a trade is taken: "هلق رح تتاخد صفقة، والسبب...".

    The owner asked to be told a trade is about to be taken and WHY, from the
    market's conditions -- not to find a trade already open. This is that
    message: the direction, the numbers, and the conditions that came true.
    """
    d = "بيع 🔴" if p["direction"] == "short" else "شراء 🟢"
    L = [f"🔎 <b>هلق رح تتاخد صفقة {d}</b> — {SYMBOL}", ""]
    L.append("<b>ليش؟ الشروط تحققت:</b>")
    L += ["  " + x for x in _why_taken(p, m)]
    L.append("")
    L += _plan_lines(p)
    note = _late_note("sell" if p["direction"] == "short" else "buy",
                      p.get("r15"))
    if note:
        L.append(f"\n⚠️ {note}")
    L.append("")
    L.append("<i>عم سجّلها هلّق — رح تابعها لحالي وأخبرك لما تنسكّر.</i>")
    return "\n".join(L)


def _open_card(p: dict, t=None) -> str:
    e = ("⚡ صفقة قوية" if p.get("strong") else "🎯 لقطت صفقة")
    head = f"{e} — {SYMBOL}" + (f"  ·  #{t['id']}" if t is not None else "")
    side = "sell" if p["direction"] == "short" else "buy"
    note = _late_note(side, p.get("r15"))
    tail = [f"\n⚠️ {note}"] if note else []
    return "\n".join([head] + _plan_lines(p) + tail +
                     ["", "<i>سجّلتها ورح تابعها لحالي.</i>"])


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
        lines.append(f"{m:+,.2f}$ · رأس المال <b>{_equity_after(t):,.2f}$</b>")
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
        "full": (f"بحمل <b>صفقة وحدة</b> بأي وقت — "
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
    # A SECOND signal in the SAME direction is not a warning -- it is the
    # market still confirming the trade already open. State the two facts we
    # actually know: the direction is still confirmed (p vs t), and where the
    # first trade stands right now (measured, never assumed). No promise that
    # it reaches the target -- only the owner's own reading is allowed to say
    # "on track", and only as a status, not a guarantee.
    if p["direction"] == t["direction"]:
        if _winning(t):
            where = "مشت <b>نص الطريق عالهدف</b>"
        elif _in_profit(t):
            where = "لسا <b>رابحة</b> وعالطريق"
        else:
            where = "لسا <b>عند الدخول</b> (ما تحركت بعد)"
        return "\n".join([
            f"✅ <b>إشارة {d} تانية بنفس الاتجاه</b>",
            f"يعني الاتجاه لسا <b>صحيح</b> — وصفقتك الأولى #{t['id']} {frm} "
            f"{where}.",
            f"<i>ما فتحنا تانية — {rule}</i>"])
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


def _load_daily() -> dict:
    if DAILY_PATH.exists():
        try:
            return json.loads(DAILY_PATH.read_text())
        except (ValueError, OSError):
            pass
    return {}


def _save_daily(st: dict) -> None:
    try:
        DAILY_PATH.write_text(json.dumps(st))
    except OSError:
        pass


def maybe_daily_report(tg: Telegram, allow: set[str]) -> None:
    """Send the day's record ONCE, at the market close the owner named.

    Triggered on the OBSERVED open->closed transition, not merely "the clock
    says evening": a weekend has no close to report, and a restart mid-evening
    must not resend a day already delivered. The state is persisted, so the
    transition survives a reboot."""
    st = _load_daily()
    try:
        is_open = market_open(quote())
    except Exception:  # noqa: BLE001 - a feed hiccup must not kill the tick
        return
    today = datetime.now(timezone(timedelta(hours=TZ_OFFSET_H))).date().isoformat()
    if is_open:
        if not st.get("was_open"):
            st["was_open"] = True
            _save_daily(st)
        return
    if st.get("was_open") and st.get("last_sent") != today:
        text = daily_report()
        for c in _desk_chats(allow):
            tg.send(c, text)
        st["was_open"] = False
        st["last_sent"] = today
        _save_daily(st)


def maybe_daily_analysis(tg: Telegram, allow: set[str]) -> None:
    """Send the full daily read TWICE a day -- morning and evening -- each once.

    The owner asked for "تحليل يومي كل يوم للبدو يصير وليلي" (a daily analysis,
    morning and night). This is a FACT about the market, not a trade, so it runs
    before the silence switch like the day's record. Keyed on the calendar date
    AND the slot ("am"/"pm"), persisted, so a reboot mid-day neither repeats a
    slot nor loses the other. The hours are configurable (DESK_DAILY_AM_HOUR /
    DESK_DAILY_PM_HOUR, local) and each slot sends only within its hour window,
    so a restart hours later does not fire a stale "morning" read.

    Guarded end to end: a feed hiccup or a send failure never kills the tick.
    """
    st = _load_daily()
    now = datetime.now(timezone(timedelta(hours=TZ_OFFSET_H)))
    today = now.date().isoformat()
    sent = st.get("analysis_sent") or {}
    for slot, hour in (("am", DAILY_AM_HOUR), ("pm", DAILY_PM_HOUR)):
        if now.hour != hour or sent.get(slot) == today:
            continue
        try:
            text = daily_analysis()
        except Exception:  # noqa: BLE001 - a hiccup must not kill the tick
            continue
        ok = False
        for c in _alert_chats(allow):
            try:
                tg.send(c, text)
                ok = True
            except Exception:  # noqa: BLE001
                continue
        if ok:
            sent[slot] = today
            st["analysis_sent"] = sent
            _save_daily(st)


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
    """Announce a crossing of a key level, and carry the trade it implies.

    Watches two sets: the FROZEN WATCH_LEVELS the owner pinned, and the nearest
    LIVE pivots from the desk's own _levels(), which travel with the price. The
    frozen set alone goes stale -- pinned when price sat near 4100, it holds no
    level near 4050 -- so the live set is what keeps the alert useful as the
    market moves.

    Live levels are LATCHED in seen['live_levels']: recomputing them from the
    new price each cycle would make a level that was just broken vanish (it is
    now on the other side), and its break would never be seen. A latched level
    is kept until price is far from it, then dropped.

    The side of every level is kept in seen['levels'] so a level that keeps
    holding never repeats, and a price hovering on a level does not flicker. The
    first call (and any newly added level) records its starting side silently.
    """
    try:
        live = float(read_market()["live"])
        raw_sides = seen.get("levels")
        first = raw_sides is None
        sides: dict[str, str] = dict(raw_sides) if raw_sides else {}
        # Latch the live pivots: add any new ones, keep the old until price is
        # far away, so a level's side transition is stable across cycles.
        tracked = seen.setdefault("live_levels", [])
        for lv in _live_levels(live):
            if all(abs(lv - x) > 0.5 for x in tracked):
                tracked.append(lv)
        tracked[:] = [lv for lv in tracked if abs(lv - live) <= LIVE_LEVEL_BAND]
        # support_watch (run just before this one) already narrates the desk's
        # own latched support with richer states (near / bounce / 15m break).
        # A level that lands on it would deliver the same break twice, so its
        # side is still tracked but its event is left to support_watch.
        latched = seen.get("support_level")
        watched = tuple(sorted(set(WATCH_LEVELS) | set(tracked)))
        crossed: list[tuple[float, str]] = []
        for lvl in watched:
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
        # Carry the trade for the ONE level that matters now (nearest the price)
        # and just name the others: a fast move can break several levels at once,
        # and three conflicting plans in one message is noise, not a signal.
        nearest = min(crossed, key=lambda c: abs(c[0] - live))
        msg = _level_cross_msg(nearest[0], nearest[1], live)
        others = [c for c in crossed if c[0] != nearest[0]]
        if others:
            names = "، ".join(num(l) for l, _ in sorted(others, reverse=True))
            msg += f"\n\n<i>كمان انكسرت: {names}</i>"
        return msg
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None


def _level_cross_msg(level: float, side: str, live: float) -> str:
    """The crossing alert, carrying the trade the desk's own rules would take.

    A bare "level broke" leaves the owner to work out entry, stop and target at
    the worst moment. _sized() prices the trade the desk would open on that
    break, so the alert says what to do -- and stays honest when the desk's own
    gate would refuse the trade (no conviction is invented).
    """
    direction = "long" if side == "above" else "short"
    try:
        p = _sized(live, direction, level)
        rr_ok = p["rr"] >= MIN_RR
    except Exception:  # noqa: BLE001 - the alert must still fire without sizing
        p, rr_ok = None, False
    if side == "above":
        head = ("🚀 <b>اختراق مستوى</b> — XAUUSD\n"
                f"السعر <b>{num(live)}</b> طلع فوق <b>{num(level)}</b>.")
    else:
        head = ("⚠️ <b>كسر مستوى</b> — XAUUSD\n"
                f"السعر <b>{num(live)}</b> نزل تحت <b>{num(level)}</b>.")
    if p is not None and rr_ok:
        arrow = "🟢 شراء" if direction == "long" else "🔴 بيع"
        return (head + "\n"
                f"{arrow} عند {num(live)}\n"
                f"  الوقف: {num(p['stop'])} · الهدف: {num(p['target'])}\n"
                f"  R:R ≈ {p['rr']:.2f} · مخاطرة ${p['risk_usd']:.0f}\n"
                "<i>راجع الفلتر والتأكيد قبل الدخول — التنبيه ليس أمراً.</i>")
    return (head + "\n"
            "<i>مستوى انكسر — راقب التأكيد قبل الدخول (الصفقة تحت عتبة R:R).</i>")


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


def _direction(m: dict) -> str:
    """The desk's net direction from the frame tilt: up / down / range."""
    up = sum(1 for tf in FRAMES if m["trends"].get(tf) == 1)
    dn = sum(1 for tf in FRAMES if m["trends"].get(tf) == -1)
    if dn >= CONFIRM_FRAMES:
        return "down"
    if up >= CONFIRM_FRAMES:
        return "up"
    return "range"


def trend_watch(seen: dict) -> str | None:
    """The automatic direction alert -- speak when the net direction flips.

    The owner asked to be told when the direction changes without tapping, so
    this is the one alert that fires on the market alone. It reads the desk's
    own net direction (CONFIRM_FRAMES of six). Because a market can hover on
    that boundary, a flip must be a real change AND at least TREND_ALERT_COOLDOWN_S
    must have passed since the last alert -- a chop cannot spam. The first look
    after boot seeds the state silently, so a restart never announces a
    direction the market was already in.
    """
    if not TREND_ALERTS:
        return None
    try:
        m = read_market()
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None
    if not market_open(m["q"]):
        return None
    d = _direction(m)
    prev = seen.get("trend")
    now = time.time()
    if prev is None:
        seen["trend"], seen["trend_at"] = d, now
        return None
    if d == prev:
        return None
    if now - seen.get("trend_at", 0) < TREND_ALERT_COOLDOWN_S:
        return None
    seen["trend"], seen["trend_at"] = d, now

    live = m["live"]
    up = sum(1 for tf in FRAMES if m["trends"].get(tf) == 1)
    dn = sum(1 for tf in FRAMES if m["trends"].get(tf) == -1)
    tally = f"{up}ص · {dn}هـ"
    try:
        sell, buy = _zones(live)
    except Exception:  # noqa: BLE001
        sell, buy = [], []

    head = {"up": "🟢 <b>تغيّر الاتجاه → صاعد</b>",
            "down": "🔴 <b>تغيّر الاتجاه → هابط</b>",
            "range": "⚪ <b>تغيّر الاتجاه → عرضي</b>"}[d]
    if d == "down":
        advice = "الأفضل <b>بيع القوة</b> (ارتداد لفوق)"
        zone = f"منطقة بيع قريبة: <b>{num(sell[0][0])}</b>" if sell else ""
    elif d == "up":
        advice = "الأفضل <b>شراء الانخفاض</b>"
        zone = f"منطقة شراء قريبة: <b>{num(buy[0][0])}</b>" if buy else ""
    else:
        advice = "<b>اشترِ عند الدعم · بِع عند المقاومة</b>"
        zone = ""
    lines = [f"{head} — {SYMBOL}",
             f"السعر <b>{num(live)}</b> · الفريمات {tally}"]
    if zone:
        lines.append(zone)
    lines.append(f"<i>{advice}</i>")
    return "\n".join(lines)


def _entry_msg(side: str, zone: float, live: float, direction: str) -> str:
    p = _sized(zone, direction, zone)
    if side == "sell":
        head, d = f"🔴 <b>السعر قرب منطقة بيع</b> — {SYMBOL}", "بيع"
    else:
        head, d = f"🟢 <b>السعر قرب منطقة شراء</b> — {SYMBOL}", "شراء"
    return "\n".join([
        head,
        f"السعر <b>{num(live)}</b> · المنطقة <b>{num(zone)}</b>",
        f"{d}: دخول <b>{num(p['entry'])}</b> · وقف {num(p['stop'])} "
        f"(−${p['risk_usd']:.0f}) · هدف {num(p['target'])} "
        f"(+${p['gain_usd']:.0f})",
        "<i>قراءة فنية — استنى تأكيد الشمعة. الوقف إلزامي.</i>"])


def entry_watch(seen: dict) -> str | None:
    """The entry-zone heads-up -- speak once as price REACHES a desk zone.

    The owner asked to hear when price approaches an entry zone, not after a
    trade opens. So this watches the desk's own buy/sell zones (_zones) and
    fires once per zone per approach, carrying the trade that zone implies --
    entry at the zone, stop and target from _sized. Fired zones are remembered
    in seen['entry_fired'] and forgotten once price is well clear, so a zone
    that price leaves and later re-tests can speak again.
    """
    try:
        m = read_market()
        live = m["live"]
        sell, buy = _zones(live)
    except Exception:  # noqa: BLE001 - a watch must never kill the loop
        return None
    if not market_open(m["q"]):
        return None
    fired = seen.setdefault("entry_fired", {})
    for key in list(fired):
        if abs(live - float(key.split(":")[1])) > ENTRY_NEAR_PTS * 2:
            del fired[key]
    cands = []
    if sell:
        cands.append(("sell", sell[0][0], "short"))
    if buy:
        cands.append(("buy", buy[0][0], "long"))
    near = [c for c in cands if abs(live - c[1]) <= ENTRY_NEAR_PTS]
    if not near:
        return None
    side, zone, direction = min(near, key=lambda c: abs(live - c[1]))
    key = f"{side}:{zone:.2f}"
    if key in fired:
        return None
    fired[key] = True
    return _entry_msg(side, zone, live, direction)


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


def _desk_pullback(tg: Telegram, allow: set[str], seen: dict, pc: dict) -> bool:
    """The pullback entry path (ENTRY_MODE == "pullback"). Returns True when it
    has handled this cycle (a parked order, a fill, or a cancel) and the caller
    must NOT also run the immediate-entry path.

    The order is parked on disk so it survives a restart. It is filled when the
    live price reaches the limit; it is cancelled when PULLBACK_WINDOW bars pass
    with no retrace -- the move has left without us, and chasing it is exactly
    what this mode exists to avoid.
    """
    pend = _pending_load()
    if pend is not None:
        try:
            m = read_market()
        except Exception:  # noqa: BLE001 - a broken feed must not drop the order
            return True
        live = m["live"]
        side = pend.get("side")
        limit = float(pend.get("limit") or 0)
        if not market_open(m["q"]) or live <= 0:
            return True
        filled = (side == "short" and live >= limit) or \
                 (side == "long" and live <= limit)
        if filled:
            p = _pending_plan(pend.get("plan") or {}, limit)
            tid = _write_plan(p)
            row = next(x for x in all_trades() if x["id"] == tid)
            seen[tid] = "open"
            _pending_clear()
            for c in _desk_chats(allow):
                tg.send(c, _open_card(p, row))
            return True
        at = _parse(pend.get("at"))
        if at is not None and \
                (datetime.now(UTC) - at).total_seconds() > \
                PULLBACK_WINDOW * TF_SECONDS.get("5m", 300):
            _pending_clear()
            for c in _desk_chats(allow):
                tg.send(c, f"⌛ <b>انلغى أمر الارتداد</b> — السعر ما رجع لـ"
                           f"<b>{num(limit)}</b> خلال {PULLBACK_WINDOW} شمعة. "
                           "ما بلحق ورا الحركة.")
            return True
        return True
    # No parked order: a fresh signal may be parked.
    p = pc["plan"]
    if p is None or open_trades():
        return False
    limit = _pullback_limit(p)
    if limit is None:
        return False            # no ATR to size the retrace: fall back to market
    _pending_save({"side": p["direction"], "limit": limit, "at": _utcnow(),
                   "plan": p, "signal_entry": p["entry"]})
    for c in _desk_chats(allow):
        tg.send(c, _pending_note(p, limit))
    return True


def _pending_note(p: dict, limit: float) -> str:
    d = "بيع 🔴" if p["direction"] == "short" else "شراء 🟢"
    return (f"⏳ <b>بانتظار ارتداد لأخذ {d}</b>\n"
            f"الإشارة ظهرت عند <b>{num(p['entry'])}</b>، بس ما بدخل بالسوق — "
            f"بحط أمر عند <b>{num(limit)}</b> وباتنظّر يرجع السعر.\n"
            f"<i>هي طريقة الدخول المقيسة: نبيع الارتداد، ما منتصف النطاق. "
            f"إذا ما رجع خلال {PULLBACK_WINDOW} شمعة، بلغي الأمر.</i>")


def desk_tick(tg: Telegram, allow: set[str], seen: dict) -> None:
    """One cycle: settle, announce, and maybe open. Split from the loop so it
    can be tested without a network or a live clock."""
    # Re-read the allow list every cycle: a chat added by the poller (someone
    # new typing /start) must receive the desk's trades, and this set is a
    # snapshot taken at boot -- without this the new chat gets a greeting and
    # then silence forever.
    allow |= load_allow()
    grade()
    # The day's record goes out on the market's close, before the silence
    # switch: it is a fact about the day, not a trade decision, so the owner
    # still gets it when auto-trading is off.
    maybe_daily_report(tg, allow)
    # The full daily read, morning and evening. Same reasoning: a fact about the
    # market, so it speaks before the silence switch.
    maybe_daily_analysis(tg, allow)
    # The support watch runs before the silence switch: a level breaking is a
    # fact about the market, not a trade decision, so the owner still hears it
    # when auto-trading is off.
    note = support_watch(seen)
    if note:
        for c in _alert_chats(allow):
            tg.send(c, note)
    note = levels_watch(seen)
    if note:
        for c in _alert_chats(allow):
            tg.send(c, note)
    note = gate_watch(seen)
    if note:
        for c in _alert_chats(allow):
            tg.send(c, note)
    note = trend_watch(seen)
    if note:
        for c in _alert_chats(allow):
            tg.send(c, note)
    note = entry_watch(seen)
    if note:
        for c in _alert_chats(allow):
            tg.send(c, note)
    for t in all_trades():
        st = t["status"]
        if st != seen.get(t["id"], "open"):
            seen[t["id"]] = st
            # A manual close already sent its card from the button handler;
            # announcing it here too would deliver the same closure twice.
            if st != "open" and t["result"] != "manual":
                for c in _desk_chats(allow):
                    tg.send(c, _close_card(t))
    # In-trade monitoring runs AFTER settle, so it never narrates a trade that
    # this same cycle just closed.
    note = trade_watch(seen)
    if note:
        for c in _desk_chats(allow):
            tg.send(c, note)
    if desk_is_silenced():
        return
    # The daily cap first: if the session already opened MAX_TRADES_DAY trades,
    # nothing more is taken today whatever the signal says. Announced once.
    if MAX_TRADES_DAY and trades_opened_today() >= MAX_TRADES_DAY:
        if seen.get("day_cap") != today_key():
            seen["day_cap"] = today_key()
            for c in _desk_chats(allow):
                tg.send(c, f"🛑 <b>وصلنا حد اليوم</b> — {MAX_TRADES_DAY} صفقات. "
                           "ما رح افتح غير بكرا.")
        return
    pc = plan_check()
    # The pullback entry path: park a limit and wait for the retrace. It runs
    # even when the plan is None, so a parked order still fills or expires on a
    # cycle where no fresh signal fires. When it has handled the cycle, the
    # immediate-entry path below must not also run.
    if ENTRY_MODE == "pullback" and _desk_pullback(tg, allow, seen, pc):
        return
    p = pc["plan"]
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
            for c in _desk_chats(allow):
                tg.send(c, _no_second(p, t0, why))
    else:
        # The heads-up BEFORE the trade: what is about to be taken and WHY, so
        # the owner hears the reason from the conditions, not from a trade that
        # is already open. Sent once per fresh plan (keyed on the entry price so
        # a drifting signal does not repeat it).
        head_key = f"pre:{p['direction']}:{p['entry']:.1f}"
        if seen.get("pre_key") != head_key:
            seen["pre_key"] = head_key
            for c in _desk_chats(allow):
                tg.send(c, _pre_trade_note(p))
        tid = _write_plan(p)
        row = next(x for x in all_trades() if x["id"] == tid)
        seen[tid] = "open"
        for c in _desk_chats(allow):
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
    print("== report ==", card_report()[:120].replace("\n", " "))
    print("== news ==", card_news()[:120].replace("\n", " "))
    print("== scenarios ==", card_scenarios()[:120].replace("\n", " "))
    print("== journal ==", journal_menu_text().replace("\n", " | "))
    print("== open ==", card_open()[:80].replace("\n", " "))
    print("== action ==", card_action().replace("\n", " | ")[:160])
    print("== alerts ==", alerts_guide().replace("\n", " | ")[:100])
    sw = support_watch({})
    print("== support ==", (sw or "silent").replace("\n", " ")[:120])
    print("== gate ==", (gate_watch({}) or "silent").replace("\n", " ")[:120])
    tw = trend_watch({"trend": "up", "trend_at": 0.0})
    print("== trend ==", (tw or "silent").replace("\n", " ")[:120])
    ew = entry_watch({})
    print("== entry ==", (ew or "silent").replace("\n", " ")[:120])
    print("== news live ==", len(_news_fetch()) if NEWS_LIVE else "off")
    print("== trade parse ==", str(parse_trade("سجل شراء 4075 وقف 4069 هدف 4085"))
          .replace("\n", " ")[:120])
    return 0


def main() -> int:
    args = sys.argv[1:]
    _init_db()
    tok = token()
    if "--selftest" in args:
        return selftest()
    if "--score" in args:
        print(re.sub(r"<[^>]+>", "", card_score()))
        return 0
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
