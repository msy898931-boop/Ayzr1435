#!/usr/bin/env python3
"""Show what the upgraded desk now says: the live cards + the 2-day replay.

Renders the actual card functions (map, news, scenarios, analysis, action) as
the owner would see them in Telegram, next to a bar-by-bar replay of the last
two days under the new $9 / $20 rules.
"""
from __future__ import annotations
import html
import json
import os
import sys

sys.path.insert(0, "/workspace/Ayzr1435")
sys.path.insert(0, "/workspace/Ayzr1435/backtest")
import bot  # noqa: E402
import day_replay as R  # noqa: E402

STOP, TGT = bot.STOP_PICK_USD, 20.0


def clean(t: str) -> str:
    return html.escape(t.replace("<b>", "").replace("</b>", "")
                       .replace("<i>", "").replace("</i>", "")
                       .replace("<code>", "").replace("</code>", ""))


def cards() -> dict:
    out = {}
    for name, fn in (("🗺️ خريطة المناطق", bot.card_map),
                     ("📰 الأخبار", bot.card_news),
                     ("🎬 السيناريوهات", bot.card_scenarios),
                     ("📊 تحليل الآن", bot.card_analysis),
                     ("🎯 التوصية", bot.card_recommend),
                     ("🧭 شو تعمل هلّق", bot.card_action)):
        try:
            out[name] = clean(fn())
        except Exception as e:  # noqa: BLE001
            out[name] = f"(خطأ: {e})"
    return out


def replay_days() -> tuple[list[str], list[dict]]:
    bars = json.load(open("/tmp/live_bars.json"))
    days = sorted({x["openTime"][:10] for x in bars["5m"]})[-2:]
    trades, carry = [], None
    for day in days:
        rows, carry = R.replay_day(bars, day, carry, STOP, TGT)
        ent = None
        for r in rows:
            if r["action"] == "entry":
                ent = r
            elif r["action"] == "exit" and ent:
                trades.append({"day": day, "t": ent["time"][11:16],
                               "dir": "شراء 🟢" if ent["direction"] == "long" else "بيع 🔴",
                               "entry": ent["price"], "stop": ent["stop"],
                               "target": ent["target"], "exit": r["exit_time"][11:16],
                               "res": r["result"]})
                ent = None
    return days, trades


if __name__ == "__main__":
    cs = cards()
    days, trades = replay_days()
    w = sum(1 for t in trades if t["res"] == "target")
    l = len(trades) - w
    net = w * TGT - l * STOP

    card_html = "".join(
        f"<div class='card'><div class='ct'>{k}</div><pre>{v}</pre></div>"
        for k, v in cs.items())
    tr_html = "".join(
        f"<tr><td>{t['day'][5:]}</td><td>{t['t']}</td><td>{t['dir']}</td>"
        f"<td>{t['entry']:.1f}</td><td>{t['stop']:.1f}</td><td>{t['target']:.1f}</td>"
        f"<td>{t['exit']}</td><td class='{'win' if t['res']=='target' else 'lose'}'>"
        f"{'✅ +$%.0f' % TGT if t['res']=='target' else '❌ −$%.0f' % STOP}</td></tr>"
        for t in trades)

    html_doc = f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>البوت المطوّر — شو صار يحكي</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;padding:18px;max-width:1000px;margin:auto;line-height:1.6}}
h1{{font-size:22px}}h2{{font-size:18px;margin-top:22px;border-right:4px solid #3b82f6;padding-right:9px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:12px}}
.ct{{color:#60a5fa;font-weight:700;margin-bottom:6px}}
pre{{white-space:pre-wrap;font-family:inherit;font-size:13px;margin:0;color:#d1d5db}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border-bottom:1px solid #2a2f3a;padding:6px;text-align:center}}
th{{background:#171a21;color:#9aa0a6}}
.win{{background:rgba(34,197,94,.25);font-weight:700}}
.lose{{background:rgba(239,68,68,.25);font-weight:700}}
.pill{{display:inline-block;padding:4px 12px;border-radius:20px;background:#1e3a8a;font-weight:700;font-size:13px}}
</style></head><body>
<h1>🤖 البوت المطوّر — شو صار يحكي</h1>
<div class='sub'>كل التحليل اللي عطيتك ياه، صار البوت يحكيه بأزراره:
<span class='pill'>🗺️ الخريطة</span> <span class='pill'>📰 الأخبار</span>
<span class='pill'>🎬 السيناريوهات</span> <span class='pill'>🎯 التوصية</span>
<span class='pill'>🧭 شو تعمل</span></div>

<h2>📱 الكروت الجديدة (لايف — نفس اللي بتشوفه بالتلغرام)</h2>
<div class='grid'>{card_html}</div>

<h2>📅 اختبار اليومين بالوقف/الهدف الجديد (${STOP:.0f} / ${TGT:.0f})</h2>
<div class='card'>
<b>النتيجة: {len(trades)} صفقة · ربح {w} · خسارة {l} · صافي {'%+.0f$' % net}</b>
<table><tr><th>اليوم</th><th>الدخول</th><th>الاتجاه</th><th>سعر</th>
<th>الوقف</th><th>الهدف</th><th>الخروج</th><th>النتيجة</th></tr>{tr_html}</table>
</div>

<h2>⚠️ تذكير صادق</h2>
<div class='card'>
<div>• <b>الأخبار معلومة فقط</b> — ما تفتح ولا تمنع ولا تحجّم صفقة. القرار الخبري إلك.</div>
<div>• <b>الصفقات من نفس المحرّك</b> (4/6 + EMA + بوابة ADX/ATR) — بس الوقف/الهدف الجديد ($9/$20).</div>
<div>• <b>يومين = ضجيج</b> — النتيجة الحقيقية على السنة: <b>+50R</b> (بعد تكلفة).</div>
</div>
</body></html>"""
    open("/workspace/project/bot-upgraded.html", "w").write(html_doc)
    print("💾 /workspace/project/bot-upgraded.html")
    print(f"صفقات اليومين: {len(trades)} · ربح {w} خسارة {l} · صافي {net:+.0f}$")
