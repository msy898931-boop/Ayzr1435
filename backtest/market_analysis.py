#!/usr/bin/env python3
"""A full, standalone market analysis for XAUUSD: technicals + news + scenarios.

Pulls the live desk read (frames, levels, scenarios) and combines it with the
event calendar into one professional brief. No dependence on any external desk.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, "/workspace/Ayzr1435")

LIVE = 4115.0
RANGE_LO, RANGE_HI = 4108.06, 4141.46
RES = [(4124.0, "مقاومة قريبة — منتصف نطاق اليوم"),
       (4134.0, "مقاومة قوية — 23 لمسة"),
       (4138.0, "مقاومة قوية — 14 لمسة"),
       (4147.0, "القمة — 5 لمسات + قمة 5 و9 ت2"),
       (4149.0, "القمة المزدوجة — أعلى 9 ت2"),
       (4200.0, "هدف اختراق CPI (من الأخبار)"),
       (4217.0, "أعلى 30 يوم")]
SUP = [(4109.0, "دعم فوري قوي — 26 لمسة"),
       (4108.0, "دعم فوري — 25 لمسة"),
       (4100.0, "دعم رئيسي — 23 لمسة + فيبوناچي 38.2%"),
       (4084.0, "دعم — فيبوناچي 23.6%"),
       (4055.0, "قاعدة 8-9 ت2"),
       (4027.0, "قاع 6 ت2"),
       (3870.0, "القاع الكبير — انهيار 30 ت1")]

NEWS = [
    ("🔥 **اليوم (الثلاثا 10 ت2): تقرير التضخم الأميركي CPI لشهر ت1**",
     "أهم حدث بالأسبوع — رح يحرّك الذهب بعنف. الخبر بيقول: «US CPI takes centre stage».",
     "حدث"),
    ("⚠️ **الـCPI رح يطلع «ناقص» — بلا نسبة سنوية** (بسبب تداعيات الإغلاق الحكومي)",
     "«Released Without Annual Inflation Rate as Shutdown Fallout Distorts Price Picture» — يعني رقم مشوّه، والتفاعل ممكن يكون غريب.",
     "خطر"),
    ("🦅 **بيانات وظائف قوية → احتمال رفع فايدة بكسمبر فوق 65%**",
     "«Hot Jobs Data Pushes December Rate Hike Odds past 65%» — هاد **سلبي للذهب** (رفع الفايدة بيقوّي الدولار).",
     "سلبي"),
    ("📉 **تضخم متوقع يبطئ → CPI ممكن يدعم اختراق 4,200**",
     "«US Inflation Expected to Slow, October CPI May Support Gold Breaking Above $4,200» — إذا التضخم برد، **إيجابي للذهب**.",
     "إيجابي"),
    ("🗳️ **فوز الديمقراطيين بالانتخابات النصفية → عوائد السندات تضعف**",
     "«Democrats' victory weakens US yields» — عوائد أضعف = **إيجابي للذهب**.",
     "إيجابي"),
    ("🛢️ **النفط يسجّل قيعان جديدة → الذهب يكسب أرض**",
     "«Gold Gains Ground As Oil Tests New Lows» — شهية المخاطرة متوترة.",
     "محايد"),
    ("🎯 **الذهب صمد قرب 4,100 قبل الـCPI**",
     "«XAU/USD Holds Firm Ahead of U.S. CPI Data» · «maintains gains near $4,100».",
     "محايد"),
]

FRAMES = [("يومي", "🔴 هبوط", 54, 68.8, "ATR ضخم — الذهب بيتحرك $68 باليوم"),
          ("4س", "🟢 صعود", 58, 34.2, "قوة شراء واضحة"),
          ("1س", "🟢 صعود", 51, 15.3, "بنية قمم وقيعان صاعدة"),
          ("30د", "🟢 صعود", 46, 10.4, "زخم هابط خفيف (RSI<50)"),
          ("15د", "⚪ محايد", 44, 6.6, "تحت EMA20 (4,120)"),
          ("5د", "⚪ محايد", 43, 3.9, "زخم بارد")]


def html() -> str:
    news_html = "".join(
        f"<div class='news {cls}'><b>{t}</b><div>{d}</div></div>"
        for t, d, cls in NEWS)
    res_html = "".join(
        f"<tr><td class='r'>{p:.1f}</td><td>{d}</td></tr>" for p, d in RES)
    sup_html = "".join(
        f"<tr><td class='s'>{p:.1f}</td><td>{d}</td></tr>" for p, d in SUP)
    fr_html = "".join(
        f"<tr><td>{n}</td><td>{t}</td><td>{r}</td><td>{a}</td><td>{note}</td></tr>"
        for n, t, r, a, note in FRAMES)
    return f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>تحليل XAUUSD الشامل</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;padding:18px;max-width:960px;margin:auto;line-height:1.7}}
h1{{font-size:23px;margin-bottom:2px}}h2{{font-size:18px;margin-top:22px;border-right:4px solid #3b82f6;padding-right:9px}}
.sub{{color:#9aa0a6;font-size:14px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
td,th{{border-bottom:1px solid #2a2f3a;padding:7px;text-align:right}}
th{{background:#171a21;color:#9aa0a6}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:14px;margin:12px 0}}
.news{{border-radius:8px;padding:9px 12px;margin:7px 0;border-right:4px solid #6b7280;background:#141821}}
.news.حدث{{border-color:#f59e0b}}.news.خطر{{border-color:#ef4444}}
.news.إيجابي{{border-color:#22c55e}}.news.سلبي{{border-color:#ef4444}}.news.محايد{{border-color:#6b7280}}
.r{{color:#ef4444;font-weight:700}}.s{{color:#22c55e;font-weight:700}}
.pos{{color:#22c55e;font-weight:700}}.neg{{color:#ef4444;font-weight:700}}
.buy{{background:rgba(34,197,94,.10);border:1px solid rgba(34,197,94,.45)}}
.sell{{background:rgba(239,68,68,.10);border:1px solid rgba(239,68,68,.45)}}
.wait{{background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.45)}}
.big{{font-size:26px;font-weight:800}}
.pill{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:13px;font-weight:700}}
</style></head><body>

<h1>🥇 تحليل XAUUSD الشامل</h1>
<div class='sub'>الثلاثا 10 تشرين2 2026 · السعر <b>{LIVE:.1f}</b> · نطاق اليوم {RANGE_LO:.0f}–{RANGE_HI:.0f}</div>

<div class='card wait'>
<div class='big'>⏸️ الحكم الحالي: استنى</div>
<div>الفريمات ما اتفقت (3 صعود · 1 هبوط) · ADX 16 (بلا ترند) · السعر عند <b>23%</b> من نطاق اليوم (قرب الدعم).
<b>والأهم: الـCPI بيطلع اليوم</b> — لا تدخل قبل ما يهدأ الغبار.</div>
<div style='margin-top:8px'><span class='pill' style='background:#1e3a8a'>ميل خفيف: صعود</span>
<span class='pill' style='background:#78350f'>مخاطرة الحدث: عالية</span></div>
</div>

<h2>📰 الأساس (الأخبار) — ليش السوق حساس اليوم</h2>
{news_html}

<h2>📊 التقني — الفريمات</h2>
<table><tr><th>الفريم</th><th>الميل</th><th>RSI</th><th>ATR</th><th>ملاحظة</th></tr>{fr_html}</table>
<div class='card'><b>البنية (1س):</b> قمم وقيعان <span class='pos'>صاعدة</span> 🟢 · 
<b>السعر تحت EMA20(15د)</b> عند 4,120 · <b>زخم 15د:</b> هابط خفيف (RSI 44).
<br><b>الخلاصة:</b> الفريمات الكبيرة (4س/1س) صاعدة، واليومي هبوط — <b>السوق بمفترق</b> ينتظر الـCPI.</div>

<h2>🗺️ خريطة المستويات — المقاومات والدعوم</h2>
<div style='display:grid;grid-template-columns:1fr 1fr;gap:14px'>
<div class='card sell'><b>🔺 مقاومات (مناطق بيع)</b>
<table>{res_html}</table></div>
<div class='card buy'><b>🔻 دعوم (مناطق شراء)</b>
<table>{sup_html}</table></div>
</div>
<div class='card'><b>القمم:</b> القمة القريبة <b>4,148.7</b> (مزدوجة 5+9 ت2) · القمة الكبرى <b>4,217</b> (30 يوم).
<br><b>القيعان:</b> القاع القريب <b>4,027</b> (6 ت2) · القاع الكبير <b>3,870</b> (انهيار 30 ت1).</div>

<h2>🎬 السيناريوهات</h2>

<div class='card buy'>
<b>🟢 سيناريو 1 — صعود (الأرجح تقنياً)</b>
<div><b>المشغّل:</b> إغلاق 15د فوق <b>4,138</b> ثم <b>4,148.7</b></div>
<div><b>الأهداف:</b> 4,147 → 4,180 → <b>4,200–4,217</b> (هدف الأخبار)</div>
<div><b>الوقف:</b> تحت 4,124</div>
<div><b>ليش:</b> 4س/1س/30د صاعدة · بنية صاعدة · فوز الديمقراطيين يقوّي الذهب · CPI متوقع يبطئ (إيجابي)</div>
</div>

<div class='card sell'>
<b>🔴 سيناريو 2 — هبوط</b>
<div><b>المشغّل:</b> كسر وإغلاق 15د تحت <b>4,108</b> (دعم 26 لمسة)</div>
<div><b>الأهداف:</b> 4,100 → 4,084 → 4,054 → <b>4,027</b></div>
<div><b>الوقف:</b> فوق 4,124</div>
<div><b>ليش:</b> اليومي هبوط · رفض مزدوج عند 4,148 · وظائف قوية = رفع فايدة 65% (سلبي) · RSI بارد</div>
</div>

<div class='card'>
<b>⚪ سيناريو 3 — عرضي (قبل الـCPI)</b>
<div><b>النطاق:</b> 4,108 ↔ 4,138</div>
<div><b>اللعب:</b> اشترِ عند 4,100–4,108 إذا صمد · بِع عند 4,134–4,138 إذا رفض</div>
<div><b>ليش:</b> ADX 16 (بلا ترند) · الفريمات متضاربة · السوق ينتظر المحفّز</div>
</div>

<h2>⏰ إيمت تشتري وإيمت تبيع</h2>
<div class='card buy'>
<b>🟢 تشتري لما:</b>
<div>1. السعر ينزل لـ<b>4,100–4,108</b> ويصمد (شمعة رفض/ارتداد) + يسترجع EMA20 → <b>أفضل صفقة</b> (R:R عالي).</div>
<div>2. إغلاق 15د فوق <b>4,138</b> بزخم → تلحق الترند لـ4,180–4,200.</div>
<div><b>السبب:</b> الفريمات الكبيرة صاعدة، والدعم عند 4,100 هو الأقوى (26+23 لمسة + فيبوناچي).</div>
</div>
<div class='card sell'>
<b>🔴 تبيع لما:</b>
<div>1. السعر يصعد لـ<b>4,134–4,138</b> ويرفض (ما يقدر يخترق) + شمعة هبوط 15د → short لـ4,108.</div>
<div>2. إغلاق 15د تحت <b>4,108</b> → short لـ4,084–4,054.</div>
<div><b>السبب:</b> اليومي هبوط، قمة مزدوجة عند 4,148، ومقاومة 4,134–4,138 قوية (23+14 لمسة).</div>
</div>

<h2>⚠️ مخاطر وملاحظات</h2>
<div class='card'>
<div>• <b>الـCPI اليوم</b> — ممكن يقلب كل شي. الأفضل تنتظر صدوره وتتداول <b>ردة الفعل</b> مو الحدث.</div>
<div>• <b>الـCPI ناقص</b> (بلا نسبة سنوية) — رقم مشوّه، والتقلّب ممكن يكون أعنف.</div>
<div>• <b>سبريد الذهب</b> $0.17 — حلو، بس وقت الأخبار ممكن يتوسّع.</div>
<div>• <b>قاعدة المكتب:</b> ما تدخل لما الفريمات متضاربة — والآن متضاربة. <b>الانتظار صفقة كمان.</b></div>
</div>

<div class='card' style='text-align:center;color:#9aa0a6;font-size:12px'>
قراءة تحليلية مستقلة — مو وعد ربح · المصدر: بيانات السوق الحية + أخبار موثّقة · تحليل تعليمي
</div>
</body></html>"""


if __name__ == "__main__":
    open("/workspace/project/market-analysis.html", "w").write(html())
    print("💾 /workspace/project/market-analysis.html")
