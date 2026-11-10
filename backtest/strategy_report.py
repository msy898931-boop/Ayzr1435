#!/usr/bin/env python3
"""Render the final strategy report ($400 account) with an equity curve."""
from __future__ import annotations
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot2026 as B  # noqa: E402
import stopgrid as G  # noqa: E402

CAP = 400.0
RISK = 9.0
W, H, PAD = 900, 300, 40


def svg_curve(pts):
    xs = list(range(len(pts)))
    ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    lo = min(lo, CAP) - 20
    hi = max(hi, CAP) + 20

    def X(i):
        return PAD + (W - 2 * PAD) * i / max(1, len(pts) - 1)

    def Y(v):
        return H - PAD - (H - 2 * PAD) * (v - lo) / (hi - lo)

    d = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}"
                 for i, v in enumerate(ys))
    grid = []
    for lvl in range(int(lo // 100) * 100, int(hi) + 1, 100):
        if lvl < lo or lvl > hi:
            continue
        y = Y(lvl)
        grid.append(f"<line x1='{PAD}' y1='{y:.1f}' x2='{W-PAD}' y2='{y:.1f}' "
                    f"stroke='#2a2f3a' stroke-width='1'/>"
                    f"<text x='{PAD-6}' y='{y+4:.1f}' fill='#9aa0a6' "
                    f"font-size='11' text-anchor='end'>${lvl}</text>")
    # capital line
    y0 = Y(CAP)
    grid.append(f"<line x1='{PAD}' y1='{y0:.1f}' x2='{W-PAD}' y2='{y0:.1f}' "
                f"stroke='#f59e0b' stroke-width='1' stroke-dasharray='4 4'/>")
    area = d + f" L{X(len(ys)-1):.1f},{Y(lo):.1f} L{X(0):.1f},{Y(lo):.1f} Z"
    return (f"<svg viewBox='0 0 {W} {H}' width='100%' "
            f"style='background:#0b0d12;border-radius:12px'>"
            f"{''.join(grid)}"
            f"<path d='{area}' fill='rgba(34,197,94,.12)'/>"
            f"<path d='{d}' fill='none' stroke='#22c55e' stroke-width='2.5'/>"
            f"<text x='{W-PAD}' y='{H-8}' fill='#9aa0a6' font-size='11' "
            f"text-anchor='end'>251 يوم · من ${CAP:.0f} إلى ${ys[-1]:.0f}</text>"
            f"</svg>")


def main():
    raw, TREND, TIMES, E20 = B.load()
    B.TREND, B.TIMES, B.E20 = TREND, TIMES, E20
    B.B5 = raw["5m"]
    b15 = raw["15m"]
    B.MACD = B._macd_hist(b15)
    B.ADX = B._adx(b15)
    B.ATR = B._atr_series(b15)
    B.RSI = B._rsi_series([x["close"] for x in b15])
    ts = G.run(RISK, 18.0 / RISK)

    eq, peak, mdd, low = CAP, CAP, 0.0, CAP
    pts = [(ts[0]["t"][:10], CAP)]
    for x in ts:
        eq += x["R"] * RISK
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
        low = min(low, eq)
        pts.append((x["t"][:10], round(eq, 1)))
    wins = [x for x in ts if x["R"] > 0]
    mR = collections.defaultdict(float)
    mN = collections.defaultdict(int)
    for x in ts:
        mR[x["t"][5:7]] += x["R"]
        mN[x["t"][5:7]] += 1
    names = {"01": "كانون2", "02": "شباط", "03": "آذار", "04": "نيسان",
             "05": "أيار", "06": "حزيران", "07": "تموز", "08": "آب",
             "09": "أيلول", "10": "تشرين1", "11": "تشرين2"}

    rows = "".join(
        f"<tr><td>{names[k]}</td><td>{mN[k]}</td>"
        f"<td class='{'pos' if mR[k]>0 else 'neg' if mR[k]<0 else ''}'>{mR[k]:+.0f}R</td>"
        f"<td class='{'pos' if mR[k]>0 else 'neg' if mR[k]<0 else ''}'>"
        f"{mR[k]*RISK:+.0f}$</td></tr>" for k in sorted(mR))

    html = f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>استراتيجية MarketDesk — حساب $400</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;
padding:20px;line-height:1.6;max-width:980px;margin:auto}}
h1{{font-size:24px;margin:0 0 4px}}.sub{{color:#9aa0a6;margin-bottom:18px}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:14px;
padding:16px;margin:14px 0}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.kpi{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:12px;text-align:center}}
.kpi .v{{font-size:22px;font-weight:800}}
.kpi .l{{font-size:12px;color:#9aa0a6}}
.pos{{color:#22c55e}}.neg{{color:#ef4444}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #2a2f3a;padding:7px 8px;text-align:center}}
th{{color:#9aa0a6;background:#171a21}}
.rule{{display:flex;gap:10px;margin:6px 0;font-size:14px}}
.rule b{{color:#60a5fa}}
.warn{{background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.35)}}
</style></head><body>
<h1>🏆 استراتيجية MarketDesk — حساب ${CAP:.0f}</h1>
<div class='sub'>XAUUSD · بيانات 2026 (251 يوم) · البوت نفسه يشتغل عليها لحظياً</div>

<div class='kpis'>
<div class='kpi'><div class='v pos'>${eq:.0f}</div><div class='l'>رأس المال النهائي</div></div>
<div class='kpi'><div class='v pos'>+{100*(eq/CAP-1):.0f}%</div><div class='l'>نسبة الربح</div></div>
<div class='kpi'><div class='v'>{len(ts)}</div><div class='l'>صفقات</div></div>
<div class='kpi'><div class='v'>{100*len(wins)/len(ts):.0f}%</div><div class='l'>نسبة الصفقات الرابحة</div></div>
</div>

<div class='card'>{svg_curve(pts)}</div>

<div class='card'>
<b>قواعد الاستراتيجية</b>
<div class='rule'><b>الدخول:</b> 4 من 6 فريمات متوافقة (1d·4h·1h·30m·15m·5m) + السعر فوق EMA20 (15د) للشراء / تحته للبيع</div>
<div class='rule'><b>فلتر الجودة:</b> ADX بين 20-40 و ATR بين 0.08-0.25% (يستبعد السوق الميت والمتضخّم)</div>
<div class='rule'><b>الوقف:</b> $9 (≈1× ATR فريم 15د) &nbsp;·&nbsp; <b>الهدف:</b> $18 (R:R = 2:1)</div>
<div class='rule'><b>الحجم:</b> 0.01 لوت · <b>صفقة واحدة</b> بكل وقت · بلا تكرار بالاتجاه نفسه</div>
</div>

<div class='card'>
<b>الأداء الكامل</b>
<table>
<tr><th>المقياس</th><th>القيمة</th></tr>
<tr><td>رأس المال</td><td>$400 → <b class='pos'>${eq:.0f}</b></td></tr>
<tr><td>الربح الصافي</td><td class='pos'>+${eq-CAP:.0f}</td></tr>
<tr><td>أقصى تراجع</td><td class='neg'>${mdd:.0f} ({mdd/CAP*100:.0f}% من الأصل)</td></tr>
<tr><td>أدنى رصيد وصل له</td><td>${low:.0f}</td></tr>
<tr><td>ربح/خسارة لكل صفقة</td><td>+$18 / −$9</td></tr>
<tr><td>إجمالي R</td><td class='pos'>+{sum(x['R'] for x in ts):.0f}R</td></tr>
</table>
</div>

<div class='card'>
<b>الأداء الشهري</b>
<table><tr><th>الشهر</th><th>صفقات</th><th>النتيجة R</th><th>النتيجة $</th></tr>
{rows}
</table>
</div>

<div class='card warn'>
<b>⚠️ بصراحة تامة</b><br>
1) <b>هذا باك-تست على بيانات ماضية</b> — الأداء المستقبلي مو مضمون.<br>
2) الربح +160% صحيح <b>على الورق</b> بلا تكلفة تنفيذ. بالواقع، السبريد والانزلاق (~$0.5/صفقة) بيقلّلوا النتيجة ~15-20%.<br>
3) <b>التراجع −43% حقيقي وموجع</b> — يعني الرصيد نزل لـ$373 بالطريق، وفيه <b>12 خسارة متتابعة</b>. لازم تتحمّل هذا نفسياً.<br>
4) المخاطرة $9 من $400 = <b>2.25% للحساب</b> — مقبولة. لا ترفعها أكتر.<br>
5) <b>8 من 11 شهر رابح</b> — بس فيه شهور صفر وسالبة. الربح مو خط مستقيم.
</div>
</body></html>"""
    open("/workspace/project/strategy-report.html", "w").write(html)
    print(f"💾 /workspace/project/strategy-report.html")
    print(f"   ${CAP:.0f} → ${eq:.0f} (+{100*(eq/CAP-1):.0f}%) · {len(ts)} صفقة · "
          f"تراجع ${mdd:.0f} · ربح% {100*len(wins)/len(ts):.0f}%")


if __name__ == "__main__":
    main()
