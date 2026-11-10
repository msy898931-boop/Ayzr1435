#!/usr/bin/env python3
"""Bar-by-bar replay of the desk for one or two days, both sides.

Walks the 5m bars of each day and, at every bar, recomputes exactly what the
live bot sees: the six frame tilts (EMA20 vs EMA50), the 15m EMA20 entry filter,
the ADX+ATR quality gate, and -- when a signal fires -- the $9 stop / $18 target.
Produces a full per-bar table (HTML) plus a summary, so every decision and its
outcome is visible.

Honest rules: the 15m filter reads the last CLOSED bar (closed()); a bar that
spans both the stop and the target takes the stop; one trade at a time.
"""
from __future__ import annotations
import bisect
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

DUR = bot.TF_SECONDS


def _t(s):
    return dt.datetime.fromisoformat(s)


def visible(bars, t):
    """Bars the live feed shows at time t (includes the forming one)."""
    j = bisect.bisect_right([x["openTime"] for x in bars], t)
    return bars[:j]


def closed(bars, tf, t):
    """Bars fully closed at time t (drop the forming one)."""
    d = DUR[tf]
    out = []
    for x in bars:
        if x["openTime"] > t:
            break
        if (_t(x["openTime"]) + dt.timedelta(seconds=d)) <= _t(t):
            out.append(x)
    return out


def trend_arrows(trends):
    m = {1: "🟢", -1: "🔴", 0: "⚪"}
    return {tf: m[v] for tf, v in trends.items()}


def replay_day(bars_all, day, carry=None, stop_usd=None, target_usd=None):
    stop_usd = bot.STOP_PICK_USD if stop_usd is None else stop_usd
    target_usd = bot.STOP_PICK_USD * bot.TARGET_RR if target_usd is None else target_usd
    b5 = [x for x in bars_all["5m"] if x["openTime"][:10] == day]
    rows = []
    op = carry
    for bar in b5:
        t = bar["openTime"]
        live = bar["close"]
        # ---- if a trade is open, check the exit first -------------------
        if op is not None:
            hi, lo = bar["high"], bar["low"]
            d = op["d"]
            hs = lo <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else lo <= op["target"]
            if hs or ht:
                res = "stop" if hs else "target"
                rows.append({**op["row"], "action": "exit", "result": res,
                             "exit_time": t, "entry": op["entry"],
                             "exit_price": op["stop"] if hs else op["target"]})
                op = None
            continue
        # ---- frame tilts at this bar ------------------------------------
        trends = {}
        for tf in bot.FRAMES:
            vis = visible(bars_all[tf], t)
            trends[tf] = bot.frame_trend(vis) if vis else 0
        up = sum(1 for tf in bot.FRAMES if trends[tf] == 1)
        dn = sum(1 for tf in bot.FRAMES if trends[tf] == -1)
        direction = None
        if up >= bot.CONFIRM_FRAMES:
            direction = "long"
        elif dn >= bot.CONFIRM_FRAMES:
            direction = "short"
        # ---- the 15m EMA20 filter + the ADX/ATR gate --------------------
        b15c = closed(bars_all["15m"], "15m", t)
        agrees = None
        if direction is not None:
            agrees = bot.frame_agrees(direction, live, b15c)
        fs = bot.filter_status(direction, b15c) if direction is not None else None
        row = {"time": t, "price": live, "trends": dict(trends),
               "up": up, "dn": dn, "direction": direction,
               "agrees": agrees, "filter": fs, "action": ""}
        # ---- entry ------------------------------------------------------
        if direction is not None and agrees and fs and fs["ok"]:
            d = direction
            sg = 1 if d == "long" else -1
            op = {"d": d, "entry": live, "row": row,
                  "stop": live - sg * stop_usd,
                  "target": live + sg * target_usd}
            row["action"] = "entry"
            row["stop"] = op["stop"]
            row["target"] = op["target"]
        rows.append(row)
    return rows, op


def summarize(rows, stop_usd=9.0, target_usd=18.0):
    bars = [r for r in rows if r["action"] != "exit"]
    entries = [r for r in rows if r["action"] == "entry"]
    exits = [r for r in rows if r["action"] == "exit"]
    longs = [r for r in bars if r["direction"] == "long"]
    shorts = [r for r in bars if r["direction"] == "short"]
    neut = [r for r in bars if r["direction"] is None]
    blocked = [r for r in bars if r["direction"] and not (r["filter"] and r["filter"]["ok"])]
    wins = [e for e in exits if e["result"] == "target"]
    losses = [e for e in exits if e["result"] == "stop"]
    rr = target_usd / stop_usd
    return {"bars": len(bars), "long": len(longs), "short": len(shorts),
            "neut": len(neut), "blocked": len(blocked),
            "entries": len(entries), "wins": len(wins), "losses": len(losses),
            "netR": len(wins) * rr - len(losses), "rr": rr}


def html(day_rows):
    out = ["<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>",
           "<title>تشريح شمعة بشمعة</title><style>",
           "body{background:#0f1115;color:#e8eaed;font-family:-apple-system,"
           "Tahoma,Arial;padding:16px;line-height:1.5}",
           "h2{margin:22px 0 8px;font-size:16px}",
           "table{border-collapse:collapse;font-size:12px;width:100%}",
           "td,th{border-bottom:1px solid #2a2f3a;padding:4px 6px;text-align:center;"
           "white-space:nowrap}",
           "th{color:#9aa0a6;position:sticky;top:0;background:#171a21}",
           ".long{background:rgba(34,197,94,.10)}.short{background:rgba(239,68,68,.10)}",
           ".entry{background:rgba(59,130,246,.28);font-weight:700}",
           ".win{background:rgba(34,197,94,.32);font-weight:700}",
           ".lose{background:rgba(239,68,68,.32);font-weight:700}",
           ".sum{background:#171a21;border:1px solid #2a2f3a;border-radius:10px;"
           "padding:10px;margin:10px 0;font-size:13px}",
           ".mono{font-family:monospace}</style></head><body>"]
    out.append("<h1>🔬 تشريح شمعة بشمعة — البوت</h1>")
    for day, rows in day_rows:
        s = summarize(rows)
        out.append(f"<h2>📅 {day}</h2>")
        out.append("<div class='sum'>"
                   f"شمعات 5د: <b>{s['bars']}</b> · "
                   f"ميل صعود 🟢: <b>{s['long']}</b> · هبوط 🔴: <b>{s['short']}</b> · "
                   f"محايد ⚪: <b>{s['neut']}</b><br>"
                   f"صفقات دخلت: <b>{s['entries']}</b> "
                   f"(ربح <b>{s['wins']}</b> · خسارة <b>{s['losses']}</b> · "
                   f"صافي <b>{s['netR']:+.0f}R</b>) · "
                   f"إشارات رفضها الفلتر: <b>{s['blocked']}</b></div>")
        out.append("<table><tr><th>الوقت (UTC)</th><th>السعر</th>"
                   + "".join(f"<th>{tf}</th>" for tf in bot.FRAMES)
                   + "<th>ص/هـ</th><th>القرار</th><th>الفلتر</th><th>الإجراء</th></tr>")
        for r in rows:
            if r["action"] == "exit":
                cls = "win" if r["result"] == "target" else "lose"
                act = "✅ ربح" if r["result"] == "target" else "❌ خسارة"
                out.append(f"<tr class='{cls}'><td>{r['exit_time'][11:16]}</td>"
                           f"<td>{r['exit_price']:.1f}</td>"
                           + "<td></td>" * len(bot.FRAMES)
                           + f"<td></td><td></td><td></td><td>{act} "
                           f"@ {r['exit_price']:.1f}</td></tr>")
                continue
            cls = "entry" if r["action"] == "entry" else (
                "long" if r["direction"] == "long" else
                "short" if r["direction"] == "short" else "")
            tr = "".join(f"<td>{trend_arrows(r['trends'])[tf]}</td>"
                         for tf in bot.FRAMES)
            if r["direction"]:
                dec = "🟢 شراء" if r["direction"] == "long" else "🔴 بيع"
                f = r["filter"]
                if r["agrees"] is False:
                    flt = "EMA20 ✗"
                elif f and f["ok"]:
                    flt = "✅"
                elif f:
                    flt = "✗ " + ",".join(f["failed"])
                else:
                    flt = "—"
            else:
                dec, flt = "⚪", "—"
            if r["action"] == "entry":
                act = ("🟢 دخول شراء" if r["direction"] == "long" else "🔴 دخول بيع") \
                    + f" @ {r['price']:.1f} (وقف {r['stop']:.1f}/هدف {r['target']:.1f})"
            else:
                act = ""
            out.append(f"<tr class='{cls}'><td>{r['time'][11:16]}</td>"
                       f"<td>{r['price']:.1f}</td>{tr}"
                       f"<td>{r['up']}/{r['dn']}</td><td>{dec}</td>"
                       f"<td>{flt}</td><td>{act}</td></tr>")
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


if __name__ == "__main__":
    bars_all = json.load(open("/tmp/live_bars.json"))
    days = sorted({x["openTime"][:10] for x in bars_all["5m"]})[-2:]
    day_rows, carry = [], None
    for d in days:
        rows, carry = replay_day(bars_all, d, carry)
        day_rows.append((d, rows))
    open("/workspace/project/day-replay.html", "w").write(html(day_rows))
    for d, rows in day_rows:
        s = summarize(rows)
        print(f"📅 {d}: شمعات={s['bars']} شراء={s['long']} بيع={s['short']} "
              f"محايد={s['neut']} · دخلت={s['entries']} (ربح {s['wins']}/خسارة "
              f"{s['losses']}) صافي {s['netR']:+.0f}R · رفض فلتر={s['blocked']}")
        for r in rows:
            if r["action"] == "entry":
                print(f"   🎬 {r['time'][11:16]} {'شراء' if r['direction']=='long' else 'بيع'}"
                      f" @ {r['price']:.1f} (وقف {r['stop']:.1f}/هدف {r['target']:.1f})")
            elif r["action"] == "exit":
                print(f"   🏁 {r['exit_time'][11:16]} "
                      f"{'✅ ربح' if r['result']=='target' else '❌ خسارة'} @ {r['exit_price']:.1f}")
    if carry is not None:
        print(f"   ⏳ صفقة لسا مفتوحة آخر البيانات: {carry['d']} @ {carry['entry']:.1f}")
    print("💾 التقرير: /workspace/project/day-replay.html")
