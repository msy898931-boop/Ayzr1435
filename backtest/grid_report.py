#!/usr/bin/env python3
"""Render the full stop x target grid as an HTML heatmap with the winner."""
from __future__ import annotations
import json

g = json.load(open("/tmp/grid.json"))
stops, tgts = g["stops"], g["tgts"]


def color(v, lo, hi):
    if v is None:
        return "#171a21"
    t = max(0.0, min(1.0, (v - lo) / (hi - lo) if hi > lo else 0.5))
    # red -> yellow -> green
    if t < 0.5:
        r, gr, b = 239, int(68 + (158 - 68) * (t * 2)), 68
    else:
        r, gr, b = int(239 - (239 - 34) * ((t - 0.5) * 2)), int(158 + (197 - 158) * ((t - 0.5) * 2)), 94
    return f"rgb({r},{gr},{b})"


def table(cost):
    d = g["costs"][cost]
    vals = [v for row in d.values() for v in row]
    lo, hi = min(vals), max(vals)
    out = [f"<h3>تكلفة تنفيذ ${cost} / صفقة</h3><table><tr><th>هدف \\ وقف</th>"
           + "".join(f"<th>{s}</th>" for s in stops) + "</tr>"]
    for tgt in tgts:
        out.append(f"<tr><th>{tgt}</th>")
        for v in d[str(tgt)]:
            bg = color(v, lo, hi)
            mark = " 🏆" if (cost == "0.5" and abs(v - 50.3) < 0.05) else ""
            out.append(f"<td style='background:{bg};color:#fff;font-weight:600'>"
                       f"{v:+.0f}{mark}</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


html = f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>الشبكة الكاملة: الأهداف × الوقوف</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;padding:18px;max-width:1000px;margin:auto;line-height:1.6}}
h1{{font-size:22px}}h3{{margin:18px 0 6px;color:#9aa0a6}}
table{{border-collapse:collapse;font-size:13px;width:100%;margin-bottom:8px}}
td,th{{border:1px solid #2a2f3a;padding:6px;text-align:center}}
th{{background:#171a21;color:#9aa0a6}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:14px;margin:12px 0}}
.win{{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.5)}}
.pos{{color:#22c55e}}.neg{{color:#ef4444}}
</style></head><body>
<h1>🧮 الشبكة الكاملة — كل الأهداف × كل الوقوف</h1>
<div class='sub'>نفس الإشارات (فلتر 4/6 + EMA20 + ADX/ATR) · 251 يوم · الرقم = المجموع R (أخضر=أعلى)</div>
{table("0.0")}
{table("0.5")}
{table("1.0")}

<div class='card win'>
<b>🏆 الفائز (بعد تكلفة واقعية $0.5):</b> وقف <b>$9</b> / هدف <b>$20</b>
<div>المجموع <b class='pos'>+50R</b> · صفر أرباع سالبة · النص الأول +30R والنص التاني +20R (ثابت) · نسبة ربح 35%</div>
<div style='margin-top:8px'>بدائل أعلى ربحاً بس أقل ثباتاً: <b>$7.5/$30</b> (+68R، ربع سالب واحد) · <b>$6/$20</b> (+63R)</div>
</div>

<div class='card'>
<b>⚠️ ليش الوقوف الضغيرة كتير ($4.5) خطيرة:</b>
<div>بلا تكلفة، وقف $4.5 يبيّن الأفضل (+179R). بس مع سبريد واقعي $1.0، <b>تنقلب لخسارة</b> — لأن الوقف ضيق كتير والسبريد ياكل جزء كبير منه. <b>الوقف لازم يبقى $6-9.</b></div>
</div>

<div class='card'>
<b>📌 الجواب المختصر:</b>
<div>• <b>الهدف لازم يكون ≥ ضعف الوقف</b> (R:R ≥ 2) — أي أقل = خسارة على المدى الطويل.</div>
<div>• <b>الوقف الأفضل: $7.5-9</b> (≈1× ATR فريم 15د). أقل من $6 خطر بسبب السبريد.</div>
<div>• <b>الأفضل الكلي: وقف $9 / هدف $20</b> — أعلى ربح مع ثبات حقيقي.</div>
</div>
</body></html>"""
open("/workspace/project/full-grid.html", "w").write(html)
print("💾 /workspace/project/full-grid.html")
