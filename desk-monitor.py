#!/usr/bin/env python3
"""desk-monitor — follow the active trade, check every N seconds, report to Telegram.

After the desk takes a trade (an active plan exists), this watches it: every
EVERY seconds it reads the live price, evaluates the plan's status against the
desk's own rules (desk/active.py), and sends a follow-up card. It stops when the
plan reaches a terminal state (target / stopped / invalidated), or after MAX
seconds.

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   delivery (never printed)
  MON_EVERY   seconds between checks       (default 120 = 2 minutes)
  MON_MAX     stop after this many seconds (default 0 = until the plan ends)
  MON_DEMO    1 = create a demo plan when none exists (for a closed-market test)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "marketdesk"))
os.environ.setdefault("DESK_RESPECT_SESSION", "0")

from desk.cli import _settings          # noqa: E402
from desk import active as A            # noqa: E402
from desk import sources                # noqa: E402

EVERY = int(os.environ.get("MON_EVERY", "120"))
MAX_S = int(os.environ.get("MON_MAX", "0"))
DEMO = os.environ.get("MON_DEMO") == "1"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

_STATE = {"waiting": "WAITING", "on_track": "IN_TRADE", "target": "TP_HIT",
          "stopped": "SL_HIT", "invalidated": "INVALID"}


def send(text: str) -> bool:
    if not (TOKEN and CHAT):
        return False
    body = json.dumps({"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode()).get("ok", False)


def card(plan: A.ActivePlan, live: float, *, closed: bool, age_s: float | None,
         cycle: int, lots: float | None = None) -> str:
    d = 1 if plan.direction == "long" else -1
    risk = abs(plan.entry - plan.stop)
    span = (plan.target - plan.entry) * d
    pct = max(0.0, min(1.0, (live - plan.entry) * d / span)) if span else 0.0
    bar = "▰" * int(round(pct * 10)) + "▱" * (10 - int(round(pct * 10)))
    rr = (span / risk) if risk else 0.0
    side = "BUY" if d > 0 else "SELL"
    state = _STATE.get(plan.status, plan.status)
    ts = time.strftime("%H:%M", time.gmtime())
    head = (f"🔭 <b>متابعة الصفقة</b> · {plan.run_id}\n"
            f"{side} · {state} · R:R {rr:.2f}\n"
            f"🕒 {ts} · السعر {live:,.2f}")
    body = (f"\n\nEntry {plan.entry:,.2f}\nSL    {plan.stop:,.2f}\nTP1   {plan.target:,.2f}\n"
            f"📊 التقدم: {bar} {pct*100:.0f}%")
    pts = (live - plan.entry) * d
    if lots:
        money = pts * 100.0 * lots
        body += f"\n💵 العائم: {money:+,.2f}$ · {pts:+,.1f} نقطة"
    else:
        body += f"\n💵 العائم: {pts:+,.1f} نقطة"
    # profit lock: at entry + dir*risk/2 the stop moves to entry + dir*(risk/2 - 0.5)
    trig = plan.entry + d * risk * 0.5
    nstop = plan.entry + d * (risk * 0.5 - 0.5)
    if (live - trig) * d >= 0:
        body += f"\n🛡️ تأمين الربح: ✅ <b>مؤمّن</b> — الوقف انتقل إلى {nstop:,.2f}"
    else:
        body += (f"\n🛡️ تأمين الربح: عند <b>{trig:,.2f}</b> يُنقل الوقف إلى {nstop:,.2f}")
    note = f"\n\n<i>دورة {cycle} · كل {EVERY//60 or 1} دقيقة</i>"
    if closed:
        note += (f"\n⚠️ <i>السوق مسكّر — السعر مجمّد"
                 + (f" (عمر التسعيرة {age_s/3600:.1f} ساعة)" if age_s else "")
                 + "؛ المراقب شغّال وبيتابع.</i>")
    return head + body + note


def main() -> int:
    s = _settings(None)
    plan = A.load(s)
    if plan is None and DEMO:
        plan = A.ActivePlan(direction="long", entry=4066.0, stop=4060.0, target=4078.0,
                            run_id="TEST-00000000-0000-demo", issued_at=time.time(),
                            status="waiting")
        A.save(s, plan)
        print("demo plan created (MON_DEMO=1)")
    if plan is None:
        send("🔭 <b>مراقب الصفقات</b>\nما في صفقة نشطة هلق — المكتب ما أخذ صفقة بعد.")
        print("no active plan; sent notice")
        return 0

    lots = None
    if getattr(s, "balance", 0):
        from desk.account import size_plan
        sz = size_plan(balance=s.balance, risk_pct=s.risk_pct, entry=plan.entry,
                       stop=plan.stop, target=plan.target,
                       contract_size=s.contract_size, lot_step=s.lot_step,
                       spread=s.spread, min_stop_points=s.min_stop_points,
                       max_stop_points=s.max_stop_points)
        lots = sz.lots if sz.feasible else None

    started = time.time()
    cycle = 0
    while True:
        cycle += 1
        q = {}
        try:
            q = sources.fetch_quote(s.symbol) or {}
        except Exception as exc:                       # one bad tick must not end it
            print(f"  quote failed: {exc.__class__.__name__}")
        live = q.get("mid")
        if not isinstance(live, (int, float)):
            print(f"cycle {cycle}: no live price this tick; skipping")
        else:
            plan.status = plan.evaluate(float(live), expiry_s=s.plan_expiry_s)
            plan.updated_at = time.time()
            A.save(s, plan)
            closed = bool(q.get("stale")) or str(q.get("market_state")) == "closed"
            txt = card(plan, float(live), closed=closed,
                       age_s=q.get("quote_age_s"), cycle=cycle, lots=lots)
            ok = send(txt)
            print(f"cycle {cycle}: live={live} status={plan.status} sent={ok}")
            if plan.is_terminal():
                print("plan terminal — monitor stops")
                break
        if MAX_S and (time.time() - started) >= MAX_S:
            print(f"reached MON_MAX={MAX_S}s — monitor stops")
            break
        time.sleep(EVERY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
