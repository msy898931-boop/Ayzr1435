# خطة تطبيق الوقف المتحرّك (جاهزة — تُنفَّذ عند كلمة «طبّقو»)

> الحالة: **محفوظة، غير مطبَّقة.** البوت اللايف ما تغيّر. القيمة المقترحة: **$4**
> (خلف مفتاح `DESK_TRAIL_USD`، **افتراضي 0 = مطفي**، فالسلوك الحالي ما يتغيّر
> إلا بقرار صريح).

## لماذا $4؟
من `backtest/exit_plan_report.html` (722 صفقة، نفس الدخولات):
- تتبّع $2 = +$571 (الأعلى) بس **أرفع** → أول ما تدخل التكلفة بيختفي.
- تتبّع $4 = +$167 (متوازن، رابح حتى على تكلفة $1.0).
- تتبّع $6/−$80 و $9/−$279 (أوسع = أسوأ).
→ **$4 هو الاختيار المتوازن.** (يُختبَر أوّلاً على سجلّ المكتب الورقي، مو بفلوس حقيقية.)

## نقطة مهمة: البوت «ورقي» أصلاً
المكتب **ما بيرسل أوامر لوسيط** — بيسجّل صفقاته بقاعدة `data/*.db`. يعني تشغيل
الوقف المتحرّك عندو = **تجربة ورقية آمنة**، وبعد أسابيع نقارن R سجلّه بالثابت.

## التعديلات (3 مواضع في `bot.py`)

### 1) مفتاح الإعداد (قرب `NEAR_STOP_FRAC`، سطر ~132)
```python
# Stop trailing: once a trade is this many $ in profit, the stop follows the
# price at this distance instead of staying fixed. 0 = OFF (current behaviour).
# The study (251 days) says a trail beats a fixed stop on both data sources, but
# the edge is thin vs real cost -- so it ships OFF and is tried on the paper
# journal first. Try DESK_TRAIL_USD=4.
TRAIL_USD = float(os.environ.get("DESK_TRAIL_USD", "0") or 0)
```

### 2) عمود جديد + ترحيل (قرب `SCHEMA`/`_init_db`، سطر ~1397)
```python
CREATE TABLE IF NOT EXISTS trades (
    ...,
    trail_usd REAL            -- the trail distance in force for this trade
);
```
```python
def _init_db() -> None:
    with closing(_conn()) as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
        if "trail_usd" not in cols:            # migrate an existing DB
            c.execute("ALTER TABLE trades ADD COLUMN trail_usd REAL")
        if "trail_stop" not in cols:           # the stop actually in force now
            c.execute("ALTER TABLE trades ADD COLUMN trail_stop REAL")
        c.commit()
```

### 3) منطق التتبّع في `grade()` (سطر ~1482) و`trade_watch()` (سطر ~2503)
داخل حلقة الشموع لكل صفقة مفتوحة (نفس المكان اللي بيفحص الوقف/الهدف):
```python
trail = t["trail_usd"] or TRAIL_USD
stop = t["trail_stop"] or t["stop"]      # ابدأ بالوقف الأصلي
peak = t["entry"]
for x in window:
    hi, lo = (x["high"], x["low"]) if long else (x["low"], x["high"])
    peak = max(peak, hi) if long else min(peak, lo)     # أعلى/أدنى سعر
    if trail > 0:
        moved = peak - trail if long else peak + trail
        stop = max(stop, moved) if long else min(stop, moved)
        _set_trail_stop(t["id"], stop)      # نحدّث العمود الحيّ
    hit_stop = lo <= stop if long else hi >= stop
    hit_tgt  = ...
```
- **`r_multiple`** يُحسَب على **الوقف الأصلي** (`_risk(t)` = |entry − stop|) مشان
  R يبقى قابلاً للمقارنة مع الصفقات الثابتة.
- الخروج بالوقف المتحرّك يُسجَّل `result="trail"` (نتيجة جديدة) مشان نميّزها
  بالإحصاءات.

### 4) تنبيه «قرب الوقف» يستعمل الوقف **الحيّ**
`trade_watch` يقرأ `trail_stop` بدل `stop`، فيكون التحذير على الوقف المتحرّك.

## الاختبارات المطلوبة (في `tests/test_engine.py`)
1. `DESK_TRAIL_USD=0` → **نفس سلوك اليوم بالضبط** (لا تغيير بالنتائج).
2. صفقة طويلة: السعر يطلع → الوقف يطلع، ويقفل `result="trail"` بربح موجب.
3. صفقة بيع: المرآة.
4. صفقة ما وصلت أي ربح → الوقف يبقى الأصلي → خسارة −1R كاملة.
5. شمعة تلمس الاثنين → **الأسوأ أولاً** (نفس قاعدة المكتب).
6. `r_multiple` محسوب على الوقف الأصلي.
7. الترحيل: قاعدة قديمة بلا `trail_usd` تشتغل بعد `_init_db`.

## خطوات التنفيذ عند «طبّقو»
1. `git checkout -b feat/trail-stop`
2. طبّق الـ4 تعديلات فوق + الاختبارات السبعة.
3. `python3 -m unittest` (المتوقّع 57 + 7 = 64) · `ruff` · `mypy` (baseline).
4. `python3 bot.py --selftest` → يتأكد الصفقات تفتح/تقفل صح.
5. افتح PR → دمج على `main`.
6. على Render: أضف `DESK_TRAIL_USD=4` (رندر `sync:false` أو قيمة صريحة).
7. راقب سجلّ المكتب أسبوعين → قارن R مع الفترة الثابتة → إمّا نثبّت أو نرجّع.

## المخاطر
- ⚠️ الميزة **رفيعة**: على تكلفة $1.5 كل القواعد خاسرة. التجربة الورقية هي اللي
  تحسم، مو الباكتست.
- ⚠️ الترحيل لازم يكون آمن على قاعدة موجودة (الصفقات المفتوحة حالياً = 0، بس
  القاعدة فيها تاريخ).
- ⚠️ لا نبدّل قيمة `STOP_PICK_USD` — التتبّع **زيادة** على الوقف، مو بديل.
