#!/usr/bin/env python3
"""Why 'more trades' is not 'more money' -- with the buy/sell split.

Renders the trade-count vs profit/drawdown comparison as bars, and reports how
many longs vs shorts the desk actually takes over the year (so 'it only buys' is
answered with a number).
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirm_grid as CG  # noqa: E402


def dir_split(sig, stop=9.0, tgt=20.0):
    lo = sh = 0
    op = None
    for k in range(len(sig)):
        bar = CG.B5[k + 300]
        if op is not None:
            d = op["d"]
            hi, low = bar["high"], bar["low"]
            hs = low <= op["stop"] if d == "long" else hi >= op["stop"]
            ht = hi >= op["target"] if d == "long" else low <= op["target"]
            if hs or ht:
                if d == "long":
                    lo += 1
                else:
                    sh += 1
                op = None
            continue
        d = sig[k]
        if d is None:
            continue
        sg = 1 if d == "long" else -1
        op = {"d": d, "stop": bar["close"] - sg * stop,
              "target": bar["close"] + sg * tgt}
    return lo, sh


if __name__ == "__main__":
    variants = [
        ("بلا فلتر تقريباً (2/6)", dict(confirm=2, use_gate=False, use_ema=False)),
        ("اتفاق 3/6", dict(confirm=3, use_gate=True, use_ema=True)),
        ("الحالي 4/6 ✅", dict(confirm=4, use_gate=True, use_ema=True)),
        ("اتفاق 6/6", dict(confirm=6, use_gate=True, use_ema=True)),
    ]
    data = []
    for name, kw in variants:
        sig = CG.build(**kw)
        m = CG.ev(CG.sim(sig))
        data.append((name, m[0], m[2], m[3]))
    sig4 = CG.build(confirm=4, use_gate=True, use_ema=True)
    lo, sh = dir_split(sig4)

    # bars
    maxn = max(d[1] for d in data)
    rows = ""
    for name, n, r, dd in data:
        wn = 100 * n / maxn
        wr = 100 * r / max(x[2] for x in data)
        wd = 100 * (-dd) / max(-x[3] for x in data)
        rows += (f"<tr><td>{name}</td><td>{n} ({n/251:.1f}/يوم)</td>"
                 f"<td><div style='background:#3b82f6;height:14px;width:{wn:.0f}%'></div></td>"
                 f"<td class='pos'>+{r:.0f}R</td>"
                 f"<td><div style='background:#ef4444;height:14px;width:{wd:.0f}%'></div>"
                 f"<span class='neg'>{dd:.0f}R</span></td></tr>")

    html = f"""<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
<title>صفقات أكتر = فلوس أكتر؟</title><style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,Tahoma,Arial;padding:18px;max-width:900px;margin:auto;line-height:1.6}}
h1{{font-size:21px}}table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #2a2f3a;padding:9px;text-align:right}}
th{{color:#9aa0a6;background:#171a21}}
.pos{{color:#22c55e;font-weight:700}}.neg{{color:#ef4444;font-weight:700}}
.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:14px;margin:12px 0}}
.big{{font-size:30px;font-weight:800;color:#22c55e}}
</style></head><body>
<h1>💡 «صفقات أكتر = فلوس أكتر»؟ لأ — الدليل</h1>
<div class='card'>
<table><tr><th>الفلتر</th><th>عدد الصفقات</th><th></th><th>الربح</th><th>أقصى تراجع</th></tr>
{rows}
</table>
<p style='color:#9aa0a6;font-size:13px'>251 يوم · وقف $9 / هدف $20 · «بلا فلتر» = 18 صفقة/يوم بس تراجع ضخم</p>
</div>

<div class='card'>
<b>البوت بيشتغل شراء وبيع فعلاً:</b>
<div>على 251 يوم — <span class='big'>{lo}</span> صفقة <b>شراء</b> 🟢
و <span class='big'>{sh}</span> صفقة <b>بيع</b> 🔴</div>
<div style='color:#9aa0a6;font-size:13px'>اليوم ومبارح كانوا صعود خالص فما في بيع — بس بالسنة البيع شغّال.</div>
</div>
</body></html>"""
    open("/workspace/project/tradecount.html", "w").write(html)
    print("شراء:", lo, "· بيع:", sh)
    for name, n, r, dd in data:
        print(f"{name:24} صفقات={n:5} ({n/251:.1f}/يوم) ربح={r:+.0f}R تراجع={dd:.0f}R")
    print("💾 /workspace/project/tradecount.html")
