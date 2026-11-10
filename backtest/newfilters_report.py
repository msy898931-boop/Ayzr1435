#!/usr/bin/env python3
"""Final report: the two new filters, 2-day replay + full-year verdict."""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import day_replay as R  # noqa: E402
import newfilters_2day as NF  # noqa: E402


def tilt_cell(bars_all, t):
    b = R.closed(bars_all["1d"], "1d", t)
    v = bot.frame_trend(b) if b else 0
    return "🟢" if v == 1 else "🔴" if v == -1 else "⚪"


def build_rows_html(bars_all, days):
    out = []
    for day in days:
        rows, _ = NF.replay(bars_all, day, None, True, "strict")
        out.append(f"<h3>📅 {day}</h3>")
        out.append("<table><tr><th>UTC</th><th>سعر</th><th>الاتجاه</th>"
                   "<th>جلسة</th><th>1d</th><th>EMA</th><th>بوابة</th>"
                   "<th>الإجراء</th></tr>")
        for r in rows:
            if r["action"] == "exit":
                cls = "win" if r["result"] == "target" else "lose"
                out.append(f"<tr class='{cls}'><td>{r['exit_time'][11:16]}</td>"
                           f"<td>{r['exit_price']:.1f}</td><td>—</td><td></td><td></td>"
                           f"<td></td><td></td><td>🏁 "
                           f"{'✅ ربح +$20' if r['result']=='target' else '❌ خسارة −$9'}</td></tr>")
                continue
            if r["direction"] is None:
                continue
            t = r["time"]
            h = int(t[11:13])
            sess = "✅" if 7 <= h < 20 else "❌"
            dec = "شراء 🟢" if r["direction"] == "long" else "بيع 🔴"
            ema = "✅" if r["agrees"] else "❌"
            gat = "✅" if (r["filter"] and r["filter"]["ok"]) else "❌"
            cls = "entry" if r["action"] == "entry" else ""
            act = (f"🎬 دخول @{r['price']:.1f} (وقف{r['stop']:.0f}/هدف{r['target']:.0f})"
                   if r["action"] == "entry" else
                   (f"⛔ رفض: {r['blocked']}" if r["blocked"] else ""))
            out.append(f"<tr class='{cls}'><td>{t[11:16]}</td><td>{r['price']:.1f}</td>"
                       f"<td>{dec}</td><td>{sess}</td><td>{tilt_cell(bars_all, t)}</td>"
                       f"<td>{ema}</td><td>{gat}</td><td>{act}</td></tr>")
        out.append("</table>")
    return "".join(out)


if __name__ == "__main__":
    bars_all = json.load(open("/tmp/live_bars.json"))
    days = sorted({x["openTime"][:10] for x in bars_all["5m"]})[-2:]
    body = build_rows_html(bars_all, days)

    year_rows = [
        ("الحالي (بلا فلترين)", 743, 300, 443, 35, "+92R", "-17R", "0/4"),
        ("+ جلسة (07-20)", 456, 178, 278, 33, "+31R", "-16R", "0/4"),
        ("+ 1d", 520, 177, 343, 35, "+63R", "-15R", "1/4"),
        ("+ جلسة + 1d", 316, 113, 203, 33, "+19R", "-16R", "2/4"),
    ]
    yt = "".join(f"<tr><td>{n}</td><td>{t}</td><td>{l}</td><td>{s}</td>"
                 f"<td>{w}%</td><td class='{'pos' if r[0]=='+' else 'neg'}'>{r}</td>"
                 f"<td class='neg'>{d}</td><td>{q}</td></tr>"
                 for n, t, l, s, w, r, d, q in year_rows)

    html = f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>الفلترين الجديدين — اختبار</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;
padding:18px;max-width:1040px;margin:auto;line-height:1.5}}
h1{{font-size:21px}}h3{{margin:16px 0 6px}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:10px}}
td,th{{border-bottom:1px solid #2a2f3a;padding:4px 6px;text-align:center;white-space:nowrap}}
th{{background:#171a21;color:#9aa0a6;position:sticky;top:0}}
.entry{{background:rgba(59,130,246,.25);font-weight:700}}
.win{{background:rgba(34,197,94,.30);font-weight:700}}
.lose{{background:rgba(239,68,68,.30);font-weight:700}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:14px;margin:12px 0}}
.pos{{color:#22c55e;font-weight:700}}.neg{{color:#ef4444;font-weight:700}}
.warn{{background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.4)}}
</style></head><body>
<h1>🧪 الفلترين الجديدين: الجلسة + الترند اليومي</h1>

<div class='card warn'>
<b>⚠️ الخلاصة أولاً:</b> على <b>اليومين</b>، فلتر الجلسة بدا مفيد (‎−$7 → +$2). لكن على
<b>السنة كاملة</b>، <b>كلاهما بيقلّل الربح</b> — فلتر الجلسة من +92R لـ+31R، وفلتر 1d لـ+63R.
<b>نفس درس الأهداف: فلتر بيحسّن يومين بيضرّ سنة.</b>
</div>

<div class='card'>
<b>التحقق على السنة (251 يوم) · وقف $9/هدف $20</b>
<table><tr><th>الفلتر</th><th>صفقات</th><th>شراء</th><th>بيع</th><th>ربح%</th>
<th>المجموع</th><th>تراجع</th><th>أرباع-</th></tr>{yt}</table>
<div style='color:#9aa0a6;font-size:13px'>لاحظ: فلتر 1d خفّض التراجع شوي (−17R → −15R) بس خسّر 29R ربح.</div>
</div>

<div class='card'>
<b>شبكة نوافذ الجلسة (السنة كاملة)</b>
<div>جرّبت 9 نوافذ — <b>ولا وحدة</b> غلبت «بلا فلتر» (+92R). أحسنها 00:00-12:00 (+59R) وسّت أسوأ.</div>
</div>

<h3>🔬 التفصيل شمعة بشمعة (اليوم ومبارح) — بفلتر الجلسة + 1d صارم</h3>
{body}
</body></html>"""
    open("/workspace/project/new-filters.html", "w").write(html)
    print("💾 /workspace/project/new-filters.html")
