#!/usr/bin/env python3
"""MarketDesk bot as a SCHEDULED automation — a near-continuous listener.

Why this shape: an always-on process needs a host, and the owner has none. An
OpenHands cron automation *is* the host: it wakes every minute and, instead of
reading once and exiting, long-polls Telegram for most of that minute, so a tap
is answered in about a second. It then grades the open trades and exits, with
time to spare before the next wake. No LLM is involved — every number is
computed — so a minute-by-minute schedule costs nothing but a little CPU.

State that must outlive a run (the journal, and who is allowed in) lives in the
automation KV store; local files are a fallback for local runs only.

Telegram update confirmation: the poll loop passes `offset` on every call, so
each update is confirmed as soon as it is answered and no offset has to be
persisted between runs.

Token: the owner's bot token is embedded below (his explicit choice, so the
automation needs no secret wiring). It is validated with getMe before use.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

API = "https://api.telegram.org"
BIQUOTE = "https://biquote.io"
UA = "Mozilla/5.0 (compatible; MarketDeskBot/1.0)"
SYMBOL = os.environ.get("DESK_SYMBOL", "XAUUSD")
MIN_RR = 1.5
SEED_CHATS = ["933571066", "8545528489"]

# The owner's bot, @Ayzr1324_bot. Used only as a last-resort candidate, and only
# after getMe confirms it is that bot -- so a stale TELEGRAM_BOT_TOKEN in the
# environment (which points at a different, dead bot) can never shadow it.
BOT_TOKEN = "REPLACE_WITH_YOUR_BOT_TOKEN"

MENU = [
    [("📊 تحليل الآن", "analysis"), ("🎯 التوصية", "recommend")],
    [("🎬 السيناريوهات", "scenarios"), ("🔔 المستويات", "levels")],
    [("📂 الصفقات المفتوحة", "open"), ("📒 السجل", "journal_menu")],
    [("➕ سجّل صفقة", "howto"), ("❓ مساعدة", "help")],
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
}
TRADE_VERBS = {"سجل", "سجّل", "صفقة", "/trade", "trade"}
FRAMES = ("1d", "4h", "1h", "30m", "15m", "5m")


# ========================================================================
# KV store (per-automation) with a local-file fallback
# ========================================================================
_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
try:
    os.makedirs(LOCAL, exist_ok=True)
except OSError:
    LOCAL = ""  # a read-only run has no local fallback; KV is the real store


def kv_available() -> bool:
    return bool(_KV_TOKEN and _KV_BASE)


def kv_get(key: str):
    if not kv_available():
        if not LOCAL:
            return None
        p = os.path.join(LOCAL, key + ".json")
        if os.path.exists(p):
            try:
                return json.loads(open(p).read())
            except (ValueError, OSError):
                return None
        return None
    req = urllib.request.Request(f"{_KV_BASE}/v1/kv/{key}",
                                 headers={"Authorization": f"Bearer {_KV_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["value"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def kv_set(key: str, value) -> None:
    if not kv_available():
        if not LOCAL:
            return
        with open(os.path.join(LOCAL, key + ".json"), "w") as f:
            json.dump(value, f)
        return
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{key}", data=json.dumps(value).encode(),
        headers={"Authorization": f"Bearer {_KV_TOKEN}",
                 "Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# ========================================================================
# secrets + helpers
# ========================================================================
def get_secret(name: str) -> str:
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
    if url:
        url = f"{url}/api/settings/secrets/{name}"
    else:
        api = os.environ.get("OPENHANDS_CLOUD_API_URL", "").rstrip("/")
        sid = os.environ.get("SANDBOX_ID", "")
        if not api or not sid:
            return ""
        url = f"{api}/api/v1/sandboxes/{sid}/settings/secrets/{name}"
    try:
        req = urllib.request.Request(url, headers={"X-Session-API-Key": key})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return ""


def fire_callback(status: str = "COMPLETED", error: str | None = None) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}"})
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:  # noqa: BLE001
        print(f"Callback error: {e}")


def _valid_token(t: str) -> bool:
    if not t:
        return False
    try:
        r = _get(f"{API}/bot{t}/getMe", timeout=10)
        return (r.get("result") or {}).get("username") == "Ayzr1324_bot"
    except Exception:  # noqa: BLE001
        return False


def token() -> str:
    """The bot token, VALIDATED before use.

    The environment's TELEGRAM_BOT_TOKEN has pointed at a different, dead bot
    (getMe -> 404), which silently shadows the right one. So every candidate is
    checked against getMe and the first one that resolves to @Ayzr1324_bot wins,
    whichever store happens to hold it."""
    cands = [os.environ.get("TG_TOKEN"), get_secret("TG_TOKEN"),
             get_secret("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_BOT_TOKEN"),
             BOT_TOKEN]
    for p in ("/workspace/project/.tg_token",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")):
        if os.path.exists(p):
            try:
                cands.append(open(p).read().strip())
            except OSError:
                pass
    for t in cands:
        if _valid_token(t):
            return t
    return next((t for t in cands if t), "")


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def num(v, dp: int = 2) -> str:
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def _get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Telegram:
    def __init__(self, tok: str):
        self.tok = tok

    def call(self, method: str, payload: dict, timeout: int = 25) -> dict:
        req = urllib.request.Request(
            f"{API}/bot{self.tok}/{method}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def send(self, chat, text, markup=None) -> bool:
        payload = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            return bool(self.call("sendMessage", payload).get("ok"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
            return False

    def answer(self, cid: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": cid, "text": text[:190]})
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
            pass

    def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        payload: dict = {"timeout": timeout,
                         "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            return self.call("getUpdates", payload, timeout=timeout + 20).get("result") or []
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
            return []


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
    return nav(action)


# ========================================================================
# biquote + indicators
# ========================================================================
def quote() -> dict:
    return _get(f"{BIQUOTE}/api/{SYMBOL}")


def bars(tf: str, limit: int = 300) -> list[dict]:
    b = _get(f"{BIQUOTE}/api/{SYMBOL}/ohlc?interval={tf}&limit={limit}")
    return sorted(b.get("bars") or [], key=lambda x: x["openTime"])


def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def atr(b, n: int = 14):
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


def pivots(b, left=3, right=3):
    highs, lows = [], []
    for i in range(left, len(b) - right):
        h, low = b[i]["high"], b[i]["low"]
        hi_ok = all(h >= b[i - j]["high"] for j in range(1, left + 1)) and all(
            h >= b[i + j]["high"] for j in range(1, right + 1))
        lo_ok = all(low <= b[i - j]["low"] for j in range(1, left + 1)) and all(
            low <= b[i + j]["low"] for j in range(1, right + 1))
        if hi_ok:
            highs.append(h)
        if lo_ok:
            lows.append(low)
    return highs, lows


def frame_trend(b) -> int:
    closes = [x["close"] for x in b]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if e20 is None or e50 is None:
        return 0
    if e20 > e50 * 1.0005:
        return 1
    if e20 < e50 * 0.9995:
        return -1
    return 0


def read_market() -> dict:
    q = quote()
    live = float(q.get("mid") or q.get("last") or 0)
    trends, atrs = {}, {}
    for tf in FRAMES:
        try:
            b = bars(tf, 300)
            trends[tf] = frame_trend(b)
            atrs[tf] = atr(b)
        except Exception:  # noqa: BLE001
            trends[tf] = 0
            atrs[tf] = None
    return {"q": q, "live": live, "trends": trends, "atrs": atrs}


def market_open(q) -> bool:
    return str(q.get("marketState")) == "open" and not q.get("stale")


def _levels(live):
    try:
        b = bars("1h", 300)
    except Exception:  # noqa: BLE001
        return [], []
    highs, lows = pivots(b, 3, 3)
    res = sorted({round(h, 2) for h in highs if h > live})
    sup = sorted({round(l, 2) for l in lows if l < live}, reverse=True)
    return res, sup


# ========================================================================
# cards
# ========================================================================
def human_age(sec) -> str:
    if not isinstance(sec, (int, float)):
        return "—"
    sec = int(sec)
    if sec < 90:
        return f"{sec} ثانية"
    if sec < 5400:
        return f"{sec // 60} دقيقة"
    if sec < 172800:
        return f"{sec // 3600} ساعة"
    return f"{sec // 86400} يوم"


def card_analysis() -> str:
    m = read_market()
    q, live, trends = m["q"], m["live"], m["trends"]
    age = q.get("quoteAgeSeconds")
    names = {"1d": "يومي", "4h": "4س", "1h": "1س", "30m": "30د", "15m": "15د", "5m": "5د"}
    icon = {1: "🟢", -1: "🔴", 0: "⚪"}
    L = [f"📊 <b>تحليل {SYMBOL}</b>"]
    if market_open(q):
        L.append(f"السعر <b>{num(live)}</b> · السوق مفتوح ✅ · آخر تحديث قبل {human_age(age)}")
        diff = "تغيّر اليوم"
    else:
        when = str(q.get("timestamp") or "").replace("T", " ").replace("Z", " UTC")
        L.append(f"السعر <b>{num(live)}</b> · <b>السوق مسكّر</b> ⛔")
        L.append(f"آخر تسعيرة: {when} — قبل {human_age(age)}")
        L.append("<i>السعر ثابت من آخر إغلاق (عطلة/ويكند) — القراءة على آخر جلسة.</i>")
        diff = "تغيّر آخر جلسة"
    if q.get("dayDiffPercent") is not None:
        L.append(f"{diff} {q['dayDiffPercent']}%")
    L += ["", "<b>الميل حسب الفريم:</b>",
          " · ".join(f"{names[tf]}:{icon[trends.get(tf, 0)]}" for tf in FRAMES)]
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    if up >= 4:
        L.append(f"\nالميل العام: <b>صاعد</b> ({up}/6 فريمات)")
    elif dn >= 4:
        L.append(f"\nالميل العام: <b>هابط</b> ({dn}/6 فريمات)")
    else:
        L.append(f"\nالميل العام: <b>عرضي</b> (صاعد {up} · هابط {dn})")
    L.append("\n<i>قراءة تحليلية، مو وعد ربح.</i>")
    return "\n".join(L)


def card_levels() -> str:
    m = read_market()
    res, sup = _levels(m["live"])
    if not res and not sup:
        return "🔔 ما قدرت أقرأ المستويات الآن."
    L = [f"🔔 <b>المستويات</b> — {SYMBOL}", f"السعر الحالي <b>{num(m['live'])}</b>",
         "", "<b>مقاومات (فوق):</b>"]
    L += [f"  🔺 {num(x)}" for x in res[:4]] or ["  —"]
    L.append("<b>دعوم (تحت):</b>")
    L += [f"  🔻 {num(x)}" for x in sup[:4]] or ["  —"]
    L.append("\n<i>مستويات من قمم/قيعان سابقة على فريم الساعة.</i>")
    return "\n".join(L)


def card_scenarios() -> str:
    m = read_market()
    live = m["live"]
    a = m["atrs"].get("15m") or m["atrs"].get("1h") or 8.0
    res, sup = _levels(live)
    L = [f"🎬 <b>السيناريوهات</b> — {SYMBOL}", f"السعر <b>{num(live)}</b>", ""]
    if res:
        trig = res[0] + 0.5
        stop = trig - a * 1.2
        tgt = res[1] if len(res) > 1 else trig + a * 3
        rr = abs(tgt - trig) / abs(trig - stop) if trig != stop else 0
        L += ["<b>🟢 سيناريو صعود</b>", f"  المشغّل: اختراق <b>{num(trig)}</b>",
              f"  الوقف: {num(stop)} · الهدف: {num(tgt)}", f"  R:R ≈ {rr:.2f}", ""]
    if sup:
        trig = sup[0] - 0.5
        stop = trig + a * 1.2
        tgt = sup[1] if len(sup) > 1 else trig - a * 3
        rr = abs(trig - tgt) / abs(stop - trig) if trig != stop else 0
        L += ["<b>🔴 سيناريو هبوط</b>", f"  المشغّل: كسر <b>{num(trig)}</b>",
              f"  الوقف: {num(stop)} · الهدف: {num(tgt)}", f"  R:R ≈ {rr:.2f}", ""]
    L.append("<i>سيناريوهات مشروطة — ما تنفّذ إلا لما يتحقق المشغّل.</i>")
    return "\n".join(L)


def card_recommend() -> str:
    m = read_market()
    q, live, trends = m["q"], m["live"], m["trends"]
    if not market_open(q):
        reason = "السوق مسكّر" if str(q.get("marketState")) != "open" else "التسعيرة قديمة"
        return (f"🎯 <b>التوصية</b> — XAUUSD\n<b>لا صفقة</b> — {reason}.\n\n"
                "<i>المكتب ما يعطي إشارة على سعر ميت.</i>")
    up = sum(1 for tf in FRAMES if trends.get(tf) == 1)
    dn = sum(1 for tf in FRAMES if trends.get(tf) == -1)
    a = m["atrs"].get("15m") or 8.0
    res, sup = _levels(live)
    if up >= 4 and res:
        d, stop = "long", live - a * 1.5
        tgt = res[0] if res and res[0] > live else live + a * 3
    elif dn >= 4 and sup:
        d, stop = "short", live + a * 1.5
        tgt = sup[0] if sup and sup[0] < live else live - a * 3
    else:
        return ("🎯 <b>التوصية</b> — XAUUSD\n<b>لا صفقة</b> — الفريمات متضاربة (عرضي).\n\n"
                "<i>المكتب ما يعطي إشارة لما السوق محتار.</i>")
    risk = abs(live - stop)
    rr = abs(tgt - live) / risk if risk else 0
    side = "شراء 🟢" if d == "long" else "بيع 🔴"
    if rr < MIN_RR:
        return (f"🎯 <b>التوصية</b> — XAUUSD\nالاتجاه المحتمل: {side} بس <b>مرفوض</b> — "
                f"R:R {rr:.2f} أقل من الحد ({MIN_RR}).\n\n"
                "<i>ما نعطي صفقة مكسبها أقل من مخاطرتها.</i>")
    return (f"🎯 <b>التوصية</b> — {SYMBOL}\n{side} على السعر الحالي <b>{num(live)}</b>\n"
            f"الوقف: <b>{num(stop)}</b> · الهدف: <b>{num(tgt)}</b>\n"
            f"R:R ≈ <b>{rr:.2f}</b> · المخاطرة {num(risk)} نقطة\n\n"
            "<i>التزام بالمخاطرة — ما تدخل بأكثر من 1-2% من حسابك.</i>")


# ========================================================================
# journal (persisted in KV)
# ========================================================================
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse(stamp):
    if not stamp:
        return None
    try:
        d = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def load_journal() -> list[dict]:
    return list(kv_get("journal") or [])


def save_journal(rows: list[dict]) -> None:
    kv_set("journal", rows)


def _risk(t) -> float:
    return abs(t["entry"] - t["stop"])


def _money(t):
    if t.get("size") is None or t.get("exit_price") is None:
        return None
    pts = ((t["exit_price"] - t["entry"]) if t["direction"] == "long"
           else (t["entry"] - t["exit_price"]))
    return pts * t["size"] * 100.0


def grade(rows: list[dict]) -> int:
    """Walk every open trade forward on 5m bars. First touch wins; a bar that
    spans both takes the stop."""
    open_rows = [t for t in rows if t["status"] == "open"]
    if not open_rows:
        return 0
    try:
        b = bars("5m", 1000)
    except Exception:  # noqa: BLE001
        return 0
    settled = 0
    for t in open_rows:
        start = _parse(t["opened_at"])
        if start is None:
            continue
        window = [x for x in b if (_parse(x["openTime"]) or start) >= start]
        long = t["direction"] == "long"
        risk = _risk(t)
        for x in window:
            hit_stop = (x["low"] <= t["stop"]) if long else (x["high"] >= t["stop"])
            hit_tgt = t.get("target") is not None and (
                (x["high"] >= t["target"]) if long else (x["low"] <= t["target"]))
            if hit_stop:
                t.update(status="closed", result="stop", exit_price=t["stop"],
                         r_multiple=-1.0, closed_at=_now())
                settled += 1
                break
            if hit_tgt:
                t.update(status="closed", result="target", exit_price=t["target"],
                         r_multiple=(abs(t["target"] - t["entry"]) / risk) if risk else None,
                         closed_at=_now())
                settled += 1
                break
    return settled


def _cutoff(period):
    now = datetime.now(UTC)
    p = (period or "all").lower()
    if p in ("day", "يومي"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if p in ("week", "أسبوعي"):
        return now - timedelta(days=7)
    if p in ("month", "شهري"):
        return now - timedelta(days=30)
    return None


def stats(rows, period="all") -> dict:
    cut = _cutoff(period)
    closed = [t for t in rows if t["status"] == "closed" and t.get("r_multiple") is not None
              and (cut is None or (_parse(t.get("closed_at")) or datetime.min.replace(tzinfo=UTC)) >= cut)]
    opened = len([t for t in rows if t["status"] == "open"])
    if not closed:
        return {"n": 0, "open": opened}
    rs = [t["r_multiple"] for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    monies = [_money(t) for t in closed if _money(t) is not None]
    gl = abs(sum(losses))
    return {"n": len(closed), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(closed), "total_r": sum(rs),
            "expectancy_r": sum(rs) / len(closed),
            "profit_factor": (sum(wins) / gl) if gl else None,
            "money": sum(monies) if monies else None,
            "profit_money": sum(m for m in monies if m > 0),
            "loss_money": sum(m for m in monies if m <= 0),
            "unpriced": len(closed) - len(monies), "open": opened}


def _line(t) -> str:
    d = "شراء 🟢" if t["direction"] == "long" else "بيع 🔴"
    head = f"#{t['id']} {d} — دخل {num(t['entry'])} · وقف {num(t['stop'])}"
    if t.get("target") is not None:
        head += f" · هدف {num(t['target'])}"
    if t["status"] == "open":
        tail = "  ⏳ مفتوحة"
    elif t.get("result") == "target":
        tail = f"  ✅ الهدف — {t['r_multiple']:+.2f}R"
    elif t.get("result") == "stop":
        tail = f"  ❌ الوقف — {t['r_multiple']:+.2f}R"
    else:
        state = t.get("result") or "مغلقة"
        tail = f"  ⚪ {state}"
    m = _money(t)
    if m is not None:
        tail += f" · {m:+,.2f}$"
    return head + tail


def _stats_block(st) -> list[str]:
    if not st.get("n"):
        return ["ما في صفقات مغلقة بهالفترة."]
    pf = st.get("profit_factor")
    head = (f"الصفقات: <b>{st['n']}</b> · <b>{st['wins']}</b> ربح / "
            f"<b>{st['losses']}</b> خسارة ({st['win_rate']:.0%})")
    if pf is None:
        line = f"النتيجة: <b>{st['total_r']:+.2f}R</b> مجموع · توقّع <b>{st['expectancy_r']:+.2f}R</b>"
    else:
        line = (f"النتيجة: <b>{st['total_r']:+.2f}R</b> مجموع · توقّع "
                f"<b>{st['expectancy_r']:+.2f}R</b> · معامل ربح {pf:.2f}")
    out = [head, line]
    if st.get("money") is not None:
        out.append(f"المال: <b>{st['money']:+,.2f}$</b> (ربح {st['profit_money']:+,.2f}$ · "
                   f"خسارة {st['loss_money']:+,.2f}$)")
        if st.get("unpriced"):
            out.append(f"<i>{st['unpriced']} صفقة بلا حجم مسجّل — ما انحسبت بالمال.</i>")
    return out


def period_report(rows, period) -> str:
    grade(rows)
    titles = {"day": "📅 السجل اليومي", "week": "🗓️ السجل الأسبوعي", "month": "📆 السجل الشهري"}
    cut = _cutoff(period)
    sel = [t for t in rows if t["status"] == "closed"
           and (cut is None or (_parse(t.get("closed_at")) or datetime.min.replace(tzinfo=UTC)) >= cut)]
    sel.sort(key=lambda t: t.get("closed_at") or "")
    title = titles.get(period, "السجل")
    L = [f"<b>{title}</b>"] + _stats_block(stats(rows, period))
    if sel:
        L.append("")
        L += ["  " + _line(t) for t in sel]
    L.append("\n<i>التقييم بالـR مو بالنقاط.</i>")
    return "\n".join(L)


def journal_menu_text(rows) -> str:
    grade(rows)

    def head(label, p):
        st = stats(rows, p)
        if not st.get("n"):
            return f"{label}: —"
        return f"{label}: {st['n']} صفقة · {st['win_rate']:.0%} · {st['total_r']:+.2f}R"
    o = stats(rows)
    return "\n".join(["📒 <b>سجل الصفقات</b>", f"📂 مفتوحة: <b>{o.get('open', 0)}</b>",
                      head("📅 اليوم", "day"), head("🗓️ الأسبوع", "week"),
                      head("📆 الشهر", "month"), "", "<i>اختر الفترة 👇</i>"])


def full_journal(rows) -> str:
    grade(rows)
    if not rows:
        return ("📋 <b>كل الصفقات</b>\nما في صفقات بعد.\n\n"
                "سجّل صفقة: <code>سجل شراء 4075 وقف 4069 هدف 4085</code>")
    L = ["📋 <b>كل الصفقات</b>"] + _stats_block(stats(rows))
    L.append("")
    L += ["  " + _line(t) for t in rows]
    return "\n".join(L)


def card_open(rows) -> str:
    grade(rows)
    op = [t for t in rows if t["status"] == "open"]
    if not op:
        return "📂 <b>الصفقات المفتوحة</b>\nما في صفقة مفتوحة هلّق."
    return "\n".join([f"📂 <b>الصفقات المفتوحة</b> — العدد <b>{len(op)}</b>", ""]
                     + ["  " + _line(t) for t in op])


def card_monitor(rows) -> str:
    grade(rows)
    op = [t for t in rows if t["status"] == "open"]
    if not op:
        return card_open(rows)
    try:
        live = float(quote().get("mid") or 0)
    except Exception:  # noqa: BLE001
        live = 0.0
    out = ["🔭 <b>متابعة الصفقات المفتوحة</b>", ""]
    for t in op:
        d = 1 if t["direction"] == "long" else -1
        risk = _risk(t)
        span = ((t["target"] - t["entry"]) * d) if t.get("target") is not None else 0
        pct = max(0.0, min(1.0, (live - t["entry"]) * d / span)) if span else 0.0
        filled = int(round(pct * 10))
        side = "شراء 🟢" if d > 0 else "بيع 🔴"
        bar = "▰" * filled + "▱" * (10 - filled)
        out.append(f"#{t['id']} {side}")
        out.append(f"  السعر {num(live)} · العائم {(live - t['entry']) * d:+,.1f} نقطة")
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


def help_text() -> str:
    return ("❓ <b>مساعدة</b>\nأنا مكتب تحليل للذهب (XAUUSD).\n\n"
            "<b>لتسجيل صفقة</b> اكتب مباشرة:\n"
            "<code>سجل شراء 4075 وقف 4069 هدف 4085 حجم 0.01</code>\n\n"
            "بعدها اضغط «📂 الصفقات المفتوحة» أو «📒 السجل».\n\n"
            "<i>قراءة تحليلية، مو وعد ربح.</i>")


def record_trade(rows, text) -> str:
    toks = text.split()
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
            key = {"وقف": "stop", "هدف": "target", "حجم": "size", "دخل": "entry"}.get(w, w)
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
    tid = max((t["id"] for t in rows), default=0) + 1
    t = {"id": tid, "symbol": SYMBOL, "direction": direction, "entry": nums["entry"],
         "stop": nums["stop"], "target": nums.get("target"), "size": nums.get("size"),
         "opened_at": _now(), "status": "open", "result": None, "exit_price": None,
         "r_multiple": None, "closed_at": None}
    rows.append(t)
    save_journal(rows)
    d = "شراء 🟢" if direction == "long" else "بيع 🔴"
    out = [f"✅ انسجّلت الصفقة #{tid}", f"{d} · دخل {num(nums['entry'])} · وقف {num(nums['stop'])}"]
    if nums.get("target"):
        out.append(f"هدف {num(nums['target'])}")
    risk = abs(nums["entry"] - nums["stop"])
    if nums.get("target") and risk and abs(nums["target"] - nums["entry"]) / risk < 1.0:
        out.append("⚠️ المضاعف أقل من 1R — خاطرة أكبر من المكسب.")
    out.append("\n<i>رح أتابعها وأسجّل نتيجتها لما تنحسم.</i>")
    return "\n".join(out)


def dispatch(action: str, rows: list[dict]) -> str:
    try:
        if action == "analysis":
            return card_analysis()
        if action == "recommend":
            return card_recommend()
        if action == "scenarios":
            return card_scenarios()
        if action == "levels":
            return card_levels()
        if action == "open":
            return card_open(rows)
        if action == "monitor":
            return card_monitor(rows)
        if action == "journal":
            return full_journal(rows)
        if action == "journal_menu":
            return journal_menu_text(rows)
        if action == "j_day":
            return period_report(rows, "day")
        if action == "j_week":
            return period_report(rows, "week")
        if action == "j_month":
            return period_report(rows, "month")
        if action in ("howto", "help"):
            return help_text()
        if action == "menu":
            return "📊 <b>مكتب تحليل السوق</b>\nاختر من الأزرار 👇"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ تعذّر التنفيذ ({esc(exc.__class__.__name__)})."
    return f"⚠️ زر غير معروف: {esc(action)}"


# ========================================================================
# the run
# ========================================================================
def action_for(text) -> str:
    s = (text or "").strip()
    if not s:
        return "menu"
    first = s.split()[0]
    if first in TRADE_VERBS:
        return "trade"
    return TEXT_ACTIONS.get(first.lower()) or TEXT_ACTIONS.get(first) or "menu"


def load_allow() -> set[str]:
    return set(kv_get("allow") or SEED_CHATS)


def save_allow(a: set[str]) -> None:
    kv_set("allow", sorted(a))


def handle(tg, upd, allow, rows) -> str | None:
    cq, msg = upd.get("callback_query"), upd.get("message")
    if cq is not None:
        action = cq.get("data") or ""
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
        cid, text = cq.get("id", ""), None
    elif msg is not None:
        chat = str((msg.get("chat") or {}).get("id") or "")
        text = msg.get("text")
        action, cid = action_for(text), ""
    else:
        return None
    if not chat:
        return None
    if chat not in allow:
        allow.add(chat)
        save_allow(allow)
        tg.send(chat, "👋 أهلاً بك في مكتب التحليل — تمت إضافتك.\n"
                      "اضغط «❓ مساعدة» لتعرف كيف تسجّل صفقة.")
        action = "menu"
    if cid:
        tg.answer(cid, "جارٍ التنفيذ…")
    try:
        out = record_trade(rows, text) if action == "trade" else dispatch(action, rows)
    except Exception as exc:  # noqa: BLE001 - one bad action must not kill the run
        out = f"⚠️ تعذّر التنفيذ ({esc(exc.__class__.__name__)})."
    tg.send(chat, out, keyboard_for(action))
    return action


def _resolution_line(t) -> str:
    """The card sent when a trade resolves. It must name the trade and its
    outcome -- announcing only the remaining open book would tell the owner
    'nothing is open' at the exact moment his trade closed."""
    d = "شراء 🟢" if t["direction"] == "long" else "بيع 🔴"
    if t.get("result") == "target":
        head = f"✅ <b>الهدف تحقق</b> — الصفقة #{t['id']} {d}"
    elif t.get("result") == "stop":
        head = f"❌ <b>الوقف ضرب</b> — الصفقة #{t['id']} {d}"
    else:
        head = f"⚪ <b>انحسمت</b> — الصفقة #{t['id']} {d}"
    lines = [head, f"دخل {num(t['entry'])} · خروج {num(t.get('exit_price'))}",
             f"النتيجة: <b>{t['r_multiple']:+.2f}R</b>"]
    m = _money(t)
    if m is not None:
        lines.append(f"المال: <b>{m:+,.2f}$</b>")
    return "\n".join(lines)


def monitor_push(tg, allow, rows) -> None:
    """Push a follow-up when an open trade RESOLVES.

    The state ('have we already announced this trade?') is remembered in KV so a
    run that finds nothing new stays silent -- the schedule must not become a
    spam loop."""
    was_open = {t["id"] for t in rows if t["status"] == "open"}
    if grade(rows):
        save_journal(rows)
    seen = set(kv_get("monitor_seen") or [])
    for t in rows:
        if t["id"] not in was_open or t["status"] == "open" or t["id"] in seen:
            continue
        seen.add(t["id"])
        msg = _resolution_line(t)
        remaining = [x for x in rows if x["status"] == "open"]
        if remaining:
            msg += f"\n\n📂 باقي مفتوح: {len(remaining)}"
        else:
            msg += "\n\n📂 ما في صفقات مفتوحة."
        for c in allow:
            tg.send(c, msg)
    if seen:
        kv_set("monitor_seen", sorted(seen))


def main() -> int:
    try:
        tok = token()
        if not tok:
            print("no token (set the TELEGRAM_BOT_TOKEN secret)")
            fire_callback("FAILED", "missing token")
            return 1
        tg = Telegram(tok)
        allow = load_allow()
        rows = load_journal()

        # Long-poll for most of the minute so a tap is answered in about a
        # second, then stop well before the next wake: two overlapping
        # getUpdates calls make Telegram answer 409 and leave both runs blind.
        offset: int | None = None
        handled: list[str] = []
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            started = time.monotonic()
            updates = tg.updates(offset=offset, timeout=20)
            if not updates:
                # A fast empty answer means another poller holds the socket;
                # back off instead of spinning on it.
                if time.monotonic() - started < 2:
                    time.sleep(2)
                continue
            for u in updates:
                offset = max(offset or 0, int(u["update_id"]) + 1)
                a = handle(tg, u, allow, rows)
                if a:
                    handled.append(a)
            save_journal(rows)

        monitor_push(tg, allow, rows)

        print(f"handled={handled} open={len([t for t in rows if t['status'] == 'open'])}")
        fire_callback("COMPLETED")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        fire_callback("FAILED", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
