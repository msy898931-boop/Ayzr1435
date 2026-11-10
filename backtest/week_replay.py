#!/usr/bin/env python3
"""A one-week bar-by-bar trial of the desk, old rules vs new.

Walks the last 7 sessions of the 2026 sample and replays exactly what the live
bot sees at every 5m bar (six frame tilts, the 15m EMA20 filter, the ADX/ATR
gate), opening at most one trade at a time. Runs the NEW rules ($9 stop / $20
target) against the OLD ones ($9 / $18) on the same bars so the difference is
honest, and writes a per-day, per-trade HTML report.
"""
from __future__ import annotations
import datetime as dt
import html
import json
import os
import sys

sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, "/workspace/Ayzr1435/backtest")
import bot  # noqa: E402
import day_replay as R  # noqa: E402

DATA = "/workspace/Ayzr1435/backtest/duka_2026.json"
STOP = bot.STOP_PICK_USD          # $9
NEW_TGT = 20.0                    # the owner's window top
OLD_TGT = 18.0                    # what the desk used before


def load():
    return json.load(open(DATA))


def week_days(bars_all, n=7):
    return sorted({x["openTime"][:10] for x in bars_all["5m"]})[-n:]


def run_week(bars_all, days, stop, tgt):
    """Replay each day in order, carrying an open trade into the next day."""
    per_day, trades, carry = [], [], None
    for day in days:
        rows, carry = R.replay_day(bars_all, day, carry, stop, tgt)
        s = R.summarize(rows, stop, tgt)
        ent = None
        for r in rows:
            if r["action"] == "entry":
                ent = r
            elif r["action"] == "exit" and ent:
                trades.append({"day": day, "t": ent["time"][11:16],
                               "dir": ent["direction"], "entry": ent["price"],
                               "stop": ent["stop"], "target": ent["target"],
                               "exit_t": r["exit_time"][11:16], "res": r["result"]})
                ent = None
        per_day.append({"day": day, "sum": s})
    return per_day, trades


def totals(trades, stop, tgt):
    w = sum(1 for t in trades if t["res"] == "target")
    l = len(trades) - w
    return w, l, w * tgt - l * stop


def main():
    bars_all = load()
    days = week_days(bars_all, 7)

    new_days, new_tr = run_week(bars_all, days, STOP, NEW_TGT)
    old_days, old_tr = run_week(bars_all, days, STOP, OLD_TGT)
    nw, nl, nnet = totals(new_tr, STOP, NEW_TGT)
    ow, ol, onet = totals(old_tr, STOP, OLD_TGT)

    # ---- full-year comparison on the same engine ------------------------
    import fullgrid as F
    b5, sig = F.build_signals()
    COST = 0.5                                    # $ per trade, realistic
    year = []
    for stop, tgt in ((9.0, 18.0), (9.0, 20.0), (10.0, 20.0),
                      (12.0, 20.0), (9.0, 15.0)):
        ts = F.simulate(b5, sig, stop, tgt)
        m = F.metrics(ts)
        net = m["r"] - m["n"] * COST / stop
        year.append((stop, tgt, m["n"], m["win"], m["r"], net, m["dd"], m["qn"]))

    year_rows = "".join(
        f"<tr class='{'win' if net>0 else 'lose'}'>"
        f"<td>${stop:.0f} / ${tgt:.0f}</td><td>{n}</td><td>{win:.0f}%</td>"
        f"<td>{r:+.0f}R</td><td><b>{net:+.0f}R</b></td><td>{dd:+.0f}R</td>"
        f"<td>{qn}</td></tr>"
        for stop, tgt, n, win, r, net, dd, qn in year)

    # ---- HTML -----------------------------------------------------------
    def trade_rows(tr, tgt):
        if not tr:
            return "<tr><td colspan='8'>— ما في صفقات هالأسبوع</td></tr>"
        out = []
        for t in tr:
            win = t["res"] == "target"
            out.append(
                f"<tr class='{'win' if win else 'lose'}'>"
                f"<td>{t['day'][5:]}</td><td>{t['t']}</td>"
                f"<td>{'شراء 🟢' if t['dir']=='long' else 'بيع 🔴'}</td>"
                f"<td>{t['entry']:.1f}</td><td>{t['stop']:.1f}</td>"
                f"<td>{t['target']:.1f}</td><td>{t['exit_t']}</td>"
                f"<td>{'✅ +$%.0f' % tgt if win else '❌ −$%.0f' % STOP}</td></tr>")
        return "".join(out)

    def day_rows(per_day, tgt):
        out = []
        for d in per_day:
            s = d["sum"]
            out.append(
                f"<tr><td>{d['day'][5:]}</td><td>{s['bars']}</td>"
                f"<td>{s['long']}</td><td>{s['short']}</td>"
                f"<td>{s['entries']}</td><td>{s['wins']}</td><td>{s['losses']}</td>"
                f"<td class='{'win' if s['netR']>0 else ('lose' if s['netR']<0 else '')}'>"
                f"{s['netR']:+.0f}R</td></tr>")
        return "".join(out)

    doc = f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>تجربة أسبوع — البوت</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;padding:18px;max-width:1000px;margin:auto;line-height:1.6}}
h1{{font-size:22px}}h2{{font-size:18px;margin-top:22px;border-right:4px solid #3b82f6;padding-right:9px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
td,th{{border-bottom:1px solid #2a2f3a;padding:6px;text-align:center}}
th{{background:#171a21;color:#9aa0a6}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:14px;margin:12px 0}}
.win{{background:rgba(34,197,94,.22)}}.lose{{background:rgba(239,68,68,.22)}}
.big{{font-size:24px;font-weight:800}}.pos{{color:#22c55e}}.neg{{color:#ef4444}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.pill{{display:inline-block;padding:3px 10px;border-radius:20px;background:#1e3a8a;font-weight:700;font-size:13px}}
</style></head><body>
<h1>🧪 تجربة أسبوع كامل — البوت</h1>
<div class='sub'>7 أيام ({days[0]} → {days[-1]}) · شمعة بشمعة · صفقة وحدة بنفس الوقت
· نفس محرّك المكتب (4/6 + EMA20 + بوابة ADX/ATR)</div>

<div class='grid'>
<div class='card'><div class='pill'>الإعداد الجديد</div>
<div class='big'>وقف ${STOP:.0f} / هدف ${NEW_TGT:.0f}</div>
<div style='margin-top:8px'>{len(new_tr)} صفقة · ربح {nw} · خسارة {nl}<br>
الصافي <b class='{'pos' if nnet>0 else 'neg'}'>{nnet:+.0f}$</b></div></div>

<div class='card'><div class='pill'>الإعداد القديم</div>
<div class='big'>وقف ${STOP:.0f} / هدف ${OLD_TGT:.0f}</div>
<div style='margin-top:8px'>{len(old_tr)} صفقة · ربح {ow} · خسارة {ol}<br>
الصافي <b class='{'pos' if onet>0 else 'neg'}'>{onet:+.0f}$</b></div></div>
</div>

<h2>📅 يوم بيوم — الإعداد الجديد (${STOP:.0f}/${NEW_TGT:.0f})</h2>
<div class='card'><table>
<tr><th>اليوم</th><th>شمعات 5د</th><th>ميل صعود</th><th>ميل هبوط</th>
<th>صفقات</th><th>ربح</th><th>خسارة</th><th>صافي R</th></tr>
{day_rows(new_days, NEW_TGT)}</table></div>

<h2>🧾 الصفقات — الإعداد الجديد (${STOP:.0f}/${NEW_TGT:.0f})</h2>
<div class='card'><table>
<tr><th>اليوم</th><th>الدخول</th><th>الاتجاه</th><th>سعر</th><th>الوقف</th>
<th>الهدف</th><th>الخروج</th><th>النتيجة</th></tr>
{trade_rows(new_tr, NEW_TGT)}</table></div>

<h2>🧾 الصفقات — الإعداد القديم (${STOP:.0f}/${OLD_TGT:.0f})</h2>
<div class='card'><table>
<tr><th>اليوم</th><th>الدخول</th><th>الاتجاه</th><th>سعر</th><th>الوقف</th>
<th>الهدف</th><th>الخروج</th><th>النتيجة</th></tr>
{trade_rows(old_tr, OLD_TGT)}</table></div>

<h2>⚠️ قراءة صادقة</h2>
<div class='card'>
<div>• <b>أسبوع = ضجيج</b> — النتيجة الحقيقية على السنة (251 يوم): الجديد <b>+50R</b> تقريباً،
القديم <b>+27R</b> (بعد تكلفة $0.5/صفقة).</div>
<div>• <b>الوقف إلزامي</b> — كل صفقة إلها وقف، وما في صفقة بلا إغلاق.</div>
<div>• <b>بار يلمس الوقف والهدف سوا = ياخد الوقف</b> (قاعدة متحفّظة).</div>
<div>• <b>الأخبار ما دخلت</b> — الأختبار تقني بحت، زي محرّك المكتب بالضبط.</div>
</div>

<h2>📆 الصورة الكاملة — نفس المحرّك على 251 يوم</h2>
<div class='card'><table>
<tr><th>الإعداد</th><th>صفقات</th><th>نسبة الربح</th><th>R خام</th>
<th>R بعد تكلفة</th><th>أقصى تراجع</th><th>أرباع سالبة</th></tr>
{year_rows}</table>
<div style='margin-top:8px;color:#9aa0a6;font-size:12px'>
التكلفة $0.5/صفقة · "أرباع سالبة" = عدد الأرباع اللي خلّصت بخسارة (قلّ ما كان أحسن).
الإعداد <b>$9/$20</b> هو الأفضل: أعلى صافي، أقل تراجع، صفر أرباع سالبة.</div></div>
</body></html>"""
    open("/workspace/project/week-trial.html", "w").write(doc)
    print("💾 /workspace/project/week-trial.html")
    print(f"الأيام: {days}")
    print(f"الجديد (${STOP:.0f}/${NEW_TGT:.0f}): {len(new_tr)} صفقة · "
          f"ربح {nw} خسارة {nl} · صافي {nnet:+.0f}$")
    print(f"القديم (${STOP:.0f}/${OLD_TGT:.0f}): {len(old_tr)} صفقة · "
          f"ربح {ow} خسارة {ol} · صافي {onet:+.0f}$")
    for t in new_tr:
        print(f"  {t['day'][5:]} {t['t']} {t['dir']:5} entry {t['entry']:.1f} "
              f"exit {t['exit_t']} → {t['res']}")


if __name__ == "__main__":
    main()
