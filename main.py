"""Trade monitor — follow the active trade every 2 minutes, report to Telegram.

Deterministic: no LLM, no desk install. It reads the live quote from biquote
(keyless), evaluates the trade with the SAME rules as desk/active.py (mirrored
here so the runner stays self-contained), and sends a follow-up card. The active
trade is held in the automation KV store, so it survives across runs (a runner
has no disk).

Config (JSON):
  {"chat_id": "933571066,8545528489",
   "trade": "{\"direction\":\"long\",\"entry\":4066,\"stop\":4060,\"target\":4078}"}
`chat_id` accepts one id or a comma-separated list (broadcast to all). `trade` is
optional: set it to watch a specific trade from the next run.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (compatible; MarketDesk/0.1)"
STATE_KEY = "active_plan"
TERMINAL = ("target", "stopped", "invalidated")
_STATE = {"waiting": "WAITING", "on_track": "IN_TRADE", "target": "TP_HIT",
          "stopped": "SL_HIT", "invalidated": "INVALID"}


# ---- automation service helpers (required) -----------------------------
def get_secret(name):
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
    if url:  # local Agent Canvas
        url = f"{url}/api/settings/secrets/{name}"
    else:  # OpenHands Cloud / Enterprise
        api = os.environ["OPENHANDS_CLOUD_API_URL"].rstrip("/")
        url = f"{api}/api/v1/sandboxes/{os.environ['SANDBOX_ID']}/settings/secrets/{name}"
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"X-Session-API-Key": key})) as r:
        return r.read().decode().strip()


def fire_callback(status="COMPLETED", error=None):
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}"}))
    except Exception as e:  # noqa: BLE001
        print(f"Callback error: {e}")


# ---- KV store (state across runs) --------------------------------------
def _kv(method, path, payload=None):
    base = os.environ.get("AUTOMATION_KV_URL", "").rstrip("/")
    token = os.environ.get("AUTOMATION_KV_TOKEN", "")
    if not (base and token):
        return None
    req = urllib.request.Request(f"{base}{path}",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None


def kv_get(key):
    try:
        r = _kv("GET", f"/v1/kv/{key}")
        return (r or {}).get("value")
    except Exception:  # noqa: BLE001
        return None


def kv_set(key, value):
    try:
        _kv("PUT", f"/v1/kv/{key}", {"value": value})
    except Exception as e:  # noqa: BLE001
        print(f"kv_set failed: {e}")


# ---- config -------------------------------------------------------------
def config():
    p = os.environ.get("AUTOMATION_CONFIG_PATH", "")
    if p and os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return {}


# ---- market + trade logic (mirrors desk/active.py) ----------------------
def quote(symbol="XAUUSD"):
    req = urllib.request.Request(f"https://biquote.io/api/{symbol}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def evaluate(plan, live):
    """The status the live price implies — same rules as desk/active.py."""
    d = 1 if plan["direction"] == "long" else -1
    st = plan.get("status", "waiting")
    if st == "waiting":
        if (live - plan["entry"]) * d >= 0:
            return "on_track"
        if (live - plan["stop"]) * d <= 0:
            return "invalidated"
        return "waiting"
    if (live - plan["target"]) * d >= 0:
        return "target"
    if (live - plan["stop"]) * d <= 0:
        return "stopped"
    return "on_track"


def card(plan, live, q, chat, every_min):
    d = 1 if plan["direction"] == "long" else -1
    risk = abs(plan["entry"] - plan["stop"])
    span = (plan["target"] - plan["entry"]) * d
    pct = max(0.0, min(1.0, (live - plan["entry"]) * d / span)) if span else 0.0
    bar = "▰" * int(round(pct * 10)) + "▱" * (10 - int(round(pct * 10)))
    rr = (span / risk) if risk else 0.0
    side = "BUY" if d > 0 else "SELL"
    ts = time.strftime("%H:%M", time.gmtime())
    out = (f"🔭 <b>متابعة الصفقة</b> · {plan.get('id', 'TRADE')}\n"
           f"{side} · {_STATE.get(plan['status'], plan['status'])} · R:R {rr:.2f}\n"
           f"🕒 {ts} · السعر {live:,.2f}\n\n"
           f"Entry {plan['entry']:,.2f}\nSL    {plan['stop']:,.2f}\n"
           f"TP1   {plan['target']:,.2f}\n"
           f"📊 التقدم: {bar} {pct*100:.0f}%\n"
           f"💵 العائم: {(live-plan['entry'])*d:+,.1f} نقطة\n")
    trig = plan["entry"] + d * risk * 0.5
    nstop = plan["entry"] + d * (risk * 0.5 - 0.5)
    if (live - trig) * d >= 0:
        out += f"🛡️ تأمين الربح: ✅ <b>مؤمّن</b> — الوقف انتقل إلى {nstop:,.2f}"
    else:
        out += f"🛡️ تأمين الربح: عند <b>{trig:,.2f}</b> يُنقل الوقف إلى {nstop:,.2f}"
    if q.get("stale") or str(q.get("marketState")) == "closed":
        out += f"\n\n⚠️ <i>السوق مسكّر — السعر مجمّد؛ المراقب شغّال.</i>"
    out += f"\n\n<i>كل {every_min} دقيقة · تلقائي</i>"
    return out


def send_one(token, chat, text):
    body = json.dumps({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:  # noqa: BLE001 - one bad chat must not stop the others
        return False


def send(token, chat, text):
    """Broadcast to one chat id or a comma-separated list of them."""
    ids = [c.strip() for c in str(chat).split(",") if c.strip()]
    return all(send_one(token, c, text) for c in ids)


# A trade to watch when the KV store holds none. Set via config ("trade" as a
# JSON string) or leave the default; the monitor follows it until it resolves.
DEFAULT_TRADE = {"direction": "long", "entry": 4075.0, "stop": 4069.0,
                 "target": 4085.0, "id": "DEMO-WAITING"}
DEFAULT_CHAT = "933571066,8545528489"   # رمضان (@Almalek121), صديقه 00 (@E_1_code)


def main():
    cfg = config()
    # The verified chat id wins over any stale TELEGRAM_CHAT_ID env value; config
    # can still override it explicitly.
    chat = str(cfg.get("chat_id") or DEFAULT_CHAT).strip()
    every_min = int(cfg.get("every_minutes", 2) or 2)
    # 0 = send every run while a trade is active; >0 = only re-send an unchanged
    # status after that many minutes (a change is always sent).
    heartbeat = int(cfg.get("heartbeat_minutes", 0) or 0) * 60

    plan = kv_get(STATE_KEY)
    if not plan:
        raw = cfg.get("trade")
        if raw:
            plan = json.loads(raw)
            plan.setdefault("status", "waiting")
            plan.setdefault("id", "TRADE-" + time.strftime("%Y%m%d-%H%M%S"))
            plan.setdefault("issued_at", time.time())
            kv_set(STATE_KEY, plan)
            print(f"seeded active plan from config: {plan}")
        elif not kv_get("demo_done"):
            # Seed ONE demo trade on the very first run so the wiring is visible,
            # then never again -- a permanent monitor must not keep inventing a
            # trade it then reports on.
            plan = dict(DEFAULT_TRADE)
            plan.setdefault("issued_at", time.time())
            kv_set(STATE_KEY, plan)
            kv_set("demo_done", True)
            print(f"seeded demo plan (first run only): {plan}")

    if not plan:
        print("no active plan — silent (permanent monitor waiting for a trade)")
        fire_callback("COMPLETED")
        return

    # A demo plan must not be followed forever: it would send a card every run
    # with no real trade behind it. After demo_ttl_minutes it is cleared once.
    if str(plan.get("id", "")).startswith("DEMO"):
        ttl = int(cfg.get("demo_ttl_minutes", 10) or 10) * 60
        if (time.time() - float(plan.get("issued_at", 0))) > ttl:
            kv_set(STATE_KEY, None)
            try:
                token = get_secret("TELEGRAM_BOT_TOKEN")
                send(token, chat, "✅ <b>انتهى الفحص التجريبي للمراقب</b>\n"
                                  "المراقب جاهز — بيبدأ تلقائياً أول ما تكون في صفقة نشطة.")
            except Exception as e:  # noqa: BLE001
                print(f"demo close message failed: {e}")
            print("demo plan expired — cleared")
            fire_callback("COMPLETED")
            return

    q = quote()
    live = float(q["mid"])
    new_status = evaluate(plan, live)
    changed = new_status != plan.get("status")
    plan["status"] = new_status
    plan["last_check"] = time.time()
    kv_set(STATE_KEY, plan)
    # Send on every status change; between changes, only on the heartbeat.
    last_sent = float(plan.get("last_sent", 0) or 0)
    due = (time.time() - last_sent) >= heartbeat if heartbeat else True
    if changed or due:
        plan["last_sent"] = time.time()
        kv_set(STATE_KEY, plan)
        token = get_secret("TELEGRAM_BOT_TOKEN")
        ok = send(token, chat, card(plan, live, q, chat, every_min))
        print(f"live={live} status={plan['status']} changed={changed} sent={ok}")
    else:
        print(f"live={live} status={plan['status']} changed=False — heartbeat not due")
    if plan["status"] in TERMINAL:
        kv_set(STATE_KEY, None)
        print("plan terminal — cleared")
    fire_callback("COMPLETED")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        fire_callback("FAILED", str(e))
        sys.exit(1)
