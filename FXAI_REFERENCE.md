# FX-AI Quant — تفكيك مرجعي كامل (teardown)

> **المصدر:** `https://forex-consensus-production.up.railway.app/`
> **تاريخ الفحص:** 2026-11-18 (كل الطوابع الزمنية UTC)
> **طريقة الفحص:** قراءة فقط — `GET` لكل الأصول والـAPI العامة، و`POST` بقيم وهمية
> (dummy) على نقاط الـVIP/الإدارة **لمعرفة شكل الردّ فقط**. لم يُكتب أي شيء على
> جهتهم: لا صفقة، لا مفتاح، لا دفعة، لا حذف.
> **الغرض:** بناء نسخة خاصة فينا (MarketDesk/AYZR) بنفس المزايا — **بلا نسخ علامة
> أو نصوص أو محفظة أو قنوات**. المرجع للفكرة والبنية، لا للانتحال.

---

## 0) الملخّص التنفيذي (اقرأ هذا أولاً)

- الموقع **صفحة واحدة** (SPA) بـHTML عربي RTL، خلفه **Express على Railway**.
- الأصول: `index.html` **100 KB / 1510 سطر** · `app.js?v=3.7` **90 KB / 2070 سطر
  / 41 دالة** · `style.css` **3.7 KB / 160 سطر**. Tailwind عبر **CDN** (مش build)،
  Font Awesome، خط Cairo، ومخطط **TradingView `tv.js`**.
- **الواجهة الخلفية:** 12 نقطة API، كلها `access-control-allow-origin: *`
  (CORS مفتوح للعالم). لا مصادقة جلسة — **مفتاح VIP ينتقل في الهيدر/الكوكي/الرابط**.
- **المنتج = بوابة مدفوعة:** كل الرياضيات (RSI/MACD/EMA/BB/ATR/Pivots/Fib) مجانية،
  والمحجوب فقط: **الإشارة، درجة التوافق، خطة التنفيذ (Entry/SL/TP1/TP2/RR)، SMC**.
- **ثغرة تسريب واضحة:** الردّ المجاني يحمل `signalArabic:"بيع (Sell)"` و
  `signalClass:"sell"` — يعني **الاتجاه مسرّب** رغم أن `signal:"VIP_LOCKED"`.
- **باب خلفي:** `?dev=<secret>` يمنح صاحب الموقع مفتاح VIP دائم عبر
  `POST /api/vip/admin-data`.
- **سجل الأداء موثّق كذباً:** 19 صفقة، +1120 نقطة معلنة، ونسبة 84% "رابحة ومؤمّنة".
  الحساب الحقيقي من نفس البيانات = **−353 نقطة**، و14 من 14 صفقة «BE» أُعلنت +50
  نقطة وهي في الحقيقة **مسطّحة أو خاسرة**. (التفصيل في §7.)
- **أخطاء فنية مؤكدة بالقياس:** مُعامِل `timeframe` مُهمَل (5m=30m=1h=1w نفس الأرقام)،
  زوج GBPUSD يرجع سعر الذهب 3725 مع مؤشرات باوند 1.31، وعنوان TradingView لباوند
  مربوط خطأً بـ`OANDA:XAUUSD`.

**الخلاصة لبناء نسختنا:** البنية والواجهة قابلة للتقليد بأمان (فكرة/تخطيط)، لكن
**منطق التسعير والثقة (paywall + track record) فيه خدع** يجب ألّا نقلّدها. عندنا
مسبقاً محرّك واحد (`bot.py`) + `plan()` + `report_payload()` + بطاقات عربية —
الفجوة الحقيقية هي **الواجهة الحيّة على الويب** (لوحة واحدة فيها كل اللوحات).

---

## 1) الحزمة التقنية (stack) والاستضافة

| الطبقة | ما هو بالضبط |
|---|---|
| الاستضافة | **Railway** — الهيدر `server: railway-hikari`، `x-railway-edge: ord1` (مركز بيانات شيكاغو)، `x-hikari-trace`. |
| الواجهة الخلفية | **Express** (Node.js) — `x-powered-by: Express`، صفحات 404 بشكل `Cannot GET /x`. |
| الواجهة الأمامية | HTML/CSS/JS خالص (vanilla)، **بلا framework** (لا React/Vue). |
| CSS | **Tailwind CDN** (`cdn.tailwindcss.com`) + ملف `style.css` صغير للتأثيرات. |
| الأيقونات/الخطوط | Font Awesome 6.4.0 (CDN)، Cairo + JetBrains Mono (Google Fonts). |
| المخطط | **TradingView `tv.js`** widget (تُحمّل السكربت ديناميكياً في `loadTradingViewChart`). |
| الضغط | `content-encoding: gzip` مدعوم. |
| التخزين المؤقت | `cache-control: public, max-age=0` + ETag على الأصول؛ الـAPI بلا cache-control (ETag فقط). |
| الأمان (هيدرات) | **لا يوجد** HSTS/CSP/X-Frame-Options/Referrer-Policy. `access-control-allow-origin: *` على كل الـAPI. |
| التتبع | رابط الإحالة فيه `fbclid` + `utm_id` (حملة إعلانية على فيسبوك) و`#admin` (hash لا يفعّل شيئاً — الأدمِن زر). |

**لا ملفات إضافية:** `/robots.txt`، `/sitemap.xml`، `/manifest.json`، `/sw.js`،
`favicon.ico`، `app.js.map`، `package.json`، `/health` → **كلها 404** (لا source
map = لا كود سيرفر مسرّب، وهذا جيد لهم).

---

## 2) الأصول الثلاثة (ما تمّ تنزيله وفحصه)

```
/tmp/fxai/index.html   100098 B   1510 سطر   ← الصفحة كاملة (SPA)
/tmp/fxai/app.js        90098 B   2070 سطر   ← 41 دالة + 25 مستمع أحداث
/tmp/fxai/style.css      3762 B    160 سطر   ← تأثيرات neon/scrollbar/ticker/modal
```

- `last-modified`: `index.html` 2026-11-18 08:34، `app.js` 08:36، `style.css`
  2026-11-07 14:05 (الستايل قديم — التطوير في الـHTML/JS).
- الصفحة تحمل **188 معرّف DOM فريد**، و`app.js` يقرأ **167** منها بـ`getElementById`.
- **12 معالج `onclick`** ظاهرة في HTML + **25 `addEventListener`** في JS.

---

## 3) خريطة الصفحة (أقسام الواجهة، من الأعلى للأسفل)

`index.html` منظّم بـ102 تعليق HTML. الترتيب المنطقي:

1. **هيدر ثابت (sticky)** — الشعار «FX-AI QUANT» + شارة «بيانات سوق حقيقية 100%»،
   ساعة UTC حيّة، مؤشرات الجلسات (لندن/نيويورك/طوكيو)، **عدّاد زوار عام**، أزرار
   تليجرام، زر **الترقية VIP ($100)**، زر الصوت، زر **Gemini AI**، زر **الإدارة**.
2. **شريط الأسعار المتحرك (ticker tape)** — يُكرّر نفس القوائم 6 مرات (`...data.quotes ×6`).
3. **بانر مجاني** (`freeUserBanner`) + **بانر بيعي VIP** (`vipPitchBanner`) +
   **بانر VIP نشِط** (`vipActiveBanner`) — تُظهر/تُخفي حسب حالة الدخول.
4. **شريط «رادار البوت الحي»** (`activeBotTradeBanner`) — يعرض الصفقات الجارية الآن،
   ويُحجب الـEntry/TP/SL لغير الـVIP.
5. **شريط التحكم** — اختيار الزوج (الذهب حصراً)، أزرار الفريم (1h/4h/1d)، **عدّاد
   إغلاق شمعة 1H**، زر تحديث.
6. **صف الاستخبارات المؤسسية** = 6 أعمدة:
   - **مقياس قوة السيولة** (الذهب vs مؤشر الدولار DXY) — عدّاد score/شريط.
   - **عوائد السندات الأمريكية 10 سنوات (US10Y)** + «حكم الماكرو».
   - **رادار SMT** (تباين الذهب/الفضة + نسبة XAU/XAG).
   - **رادار الأخبار الاقتصادية** (USD فقط) + بانر حالة + قائمة أحداث قابلة للتمرير.
7. **صف آخر** = **مصفوفة تطابق الفريمات** (15m/1H/4H/1D لـ26 مؤشر) +
   **مصائد السيولة** (PDH/PDL/Asian High/Low + حالة الـsweep).
8. **مساحة عمودين**:
   - يسار: **مخطط TradingView** + **شبكة 6 مؤشرات** (RSI/MACD/EMA/BB/ATR/Pivots+Fib)
     + **نماذج الشموع** + **بطاقة SMC/ICT** (Structure / Order Blocks / FVG /
     Liquidity Sweeps) مع **غشاء حجب** لغير الـVIP.
   - يمين: **بطاقة القرار** (شارة إشارة ضخمة + عدّاد توافق) + **خطة التنفيذ**
     (Entry/SL/TP1/TP2/RR) مع **غشاء حجب** + **حاسبة اللوت** + **أسباب التوافق** +
     زر **التقرير الذكي الشامل**.
9. **قسم التقرير الذكي الموسّع** (يظهر بعد التوليد، Markdown → HTML).
10. **جدول سجل الأداء الموثّق** — 3 بطاقات KPI + جدول آخر 15 صفقة ذهب.
11. **الفوتر** — تليجرام + عدّاد الزوار + روابط VIP/الإدارة.
12. **مودالات**: الاشتراك VIP (دفع/تفعيل)، **لوحة الإدارة**، إعداد Gemini.
13. **ودجت تليجرام عائم** + **Toast** إشعارات.

**الجميع عمود واحد على الموبايل، وشبكة 12-عمود على الشاشات الكبيرة** (`lg:col-span-*`).

---

## 4) سطح الـAPI الكامل (12 نقطة — كلها مُختبرة)

كل الردود JSON و`charset=utf-8`، وكلها `access-control-allow-origin: *`.

### 4.1 عامة (بلا مصادقة)

| # | النقطة | الطريقة | الردّ (الشكل) |
|---|---|---|---|
| 1 | `/api/quotes` | GET | `{success, quotes:[{id,name,arabicName,price,change,changePercent,time}]}` — حالياً عنصر واحد (GC=F). |
| 2 | `/api/realtime-price?symbol=` | GET | `{success,symbol,price,timestamp}` — سعر لحظي فقط (يُستدعى كل **2.5s**). |
| 3 | `/api/visitors/stats` | GET | `{success,totalVisits,uniqueVisitors,todayVisits,onlineNow}` — عام (كل زائر يراه). |
| 4 | `/api/analyze?symbol=&timeframe=` | GET | التحليل الكامل (التفصيل أدناه). يقبل هيدر `x-vip-key` اختيارياً. |
| 5 | `/api/market-intelligence?symbol=` | GET | لوحة الاستخبارات الكاملة (التفصيل أدناه). يقبل `x-vip-key`. |

### 4.2 بوابة VIP

| # | النقطة | الطريقة | الجسم | الردّ |
|---|---|---|---|---|
| 6 | `/api/vip/verify` | POST | `{key}` | `{success,valid,details{...}}` أو `{success:true,valid:false,message}`. |
| 7 | `/api/vip/submit-payment` | POST | `{txHash,userContact}` | `{success,message,submission:{id,txHash,...}}` — **يقبل أي TxID طوله ≥10** (وهمي رجع success). |
| 8 | `/api/deep-ai` | POST | `{analysis,customApiKey,vipKey}` + هيدر `x-vip-key` | `{success,provider,report}` — **يتطلب VIP** (وهمي رجع: «مقتصرة حصرياً على أعضاء باقة VIP»). |
| 9 | `/api/signal-chart-preview?symbol=&vipKey=` | GET | — | **صورة PNG** للشارت — بلا VIP: **403** JSON. |

### 4.3 الإدارة (سرّ في الجسم، لا جلسة)

| # | النقطة | الطريقة | الجسم | ملاحظة |
|---|---|---|---|---|
| 10 | `/api/vip/admin-data` | POST | `{adminSecret}` | يرجّع `keys` (كل الأكواد)، `payments`، `visitors` (IP/جهاز/متصفح/مسار)، `ownerVipKey`، `ownerKeysProtected`. |
| 11 | `/api/vip/admin-generate` | POST | `{adminSecret,daysValid,note}` | يولّد كود VIP بأي مدة (6h…lifetime=99999). |
| 12 | `/api/vip/admin-delete-key` | POST | `{adminSecret,key}` | يحذف كود (المفاتيح المحمية لا تُحذف). |
| 13 | `/api/vip/admin-update-payment` | POST | `{adminSecret,paymentId,action}` | `approve` / `delete`. |
| 14 | `/api/vip/admin-send-daily-report` | POST | `{adminSecret}` | يرسل «بطاقة الحصاد اليومي» لقناة تليجرام `@mm6_jh`. |

**سلوك المصادقة (مُختبر):** أي سرّ خاطئ/فارغ على نقاط الإدارة → 
`{"success":false,"error":"كلمة مرور المشرف مطلوبة أو غير صحيحة."}`. `GET` على
نقطة POST → **404**. لا حدّ معدّل ظاهر (5 طلبات متتالية كلها 200).

---

## 5) شكل ردّ `/api/analyze` (مجاني، الذهب، 1h)

```
success:true, isVip:false
pairInfo : {id:"GC=F", name:"XAU / USD (Gold)", arabicName, tvSymbol:"OANDA:XAUUSD", category:"Commodities"}
timeframe: "1h"
analysis:
  symbol, timeframe, timestamp, dateFormatted
  currentPrice: 3725.35, priceChange: 1.28, priceChangePercent: 0.03
  signal: "VIP_LOCKED"        ← المحجوب
  signalArabic: "بيع (Sell)"  ← ⚠️ الاتجاه مسرّب!
  signalClass: "sell"         ← ⚠️ الاتجاه مسرّب!
  confluenceScore: "🔒", confidence: "🔒"
  executionPlan: {entryPrice:"🔒 محجوب", stopLoss, takeProfit1, takeProfit2,
                  riskPips:"🔒", rewardPips1/2:"🔒", riskRewardRatio:"🔒"}
  indicators:
    rsi:29.52, rsiStatus:"تشبع بيعي (Oversold)"
    macd:{macd:-27.39, signal:-25.7, histogram:-1.69, crossover:"NEUTRAL"}
    movingAverages:{ema20:3763.62, ema50:3806.06, ema200:3869.74, trendStatus:"هابط مؤسسي"}
    bollingerBands:{upper,middle,lower,bandwidth:2.47}
    atr:17.52
    pivots:{pp:3726.06, r1:3732.09, r2:3739.04, s1:3719.11, s2:3713.08}
    fibonacci:{highest:3891.07, lowest:3720.02, fib382, fib500, fib618}
  smc: {structure:"🔒 محجوب", structureText:"...محجوب لباقة VIP",
        lastSwingHigh:3790.59, lastSwingLow:3742.975,   ← ⚠️ مسرّب رغم الحجب
        orderBlocks:{bullish:null,bearish:null}, fairValueGaps:[], liquiditySweeps:[]}
  candlestickPatterns: []
  confluenceFactors: [{factor:"عوامل الالتقاء المؤسسي (VIP)", text:"...مشفّر...", sentiment:"LOCKED"}, ...]
  institutionalSummary: "🔒 الملخص المؤسسي ... محجوبة لمشتركي باقة VIP فقط."
  reasons: ["🔒 أسباب الدخول والتوصية محجوبة — اشترك ..."]
  signalText: "إشارة VIP مشفرة (اشترك للكشف)"
```

**ملاحظة مهمة:** **كل الرياضيات مجانية** (RSI/MACD/EMA/BB/ATR/Pivots/Fib كاملة
وبأرقام دقيقة)، والمسرّب أكثر من الاتجاه: `lastSwingHigh/Low` (نقاط الهيكل) تظهر
رغم أن `structure` محجوب. المحجوب فعلياً = **الاتجاه المعلن + درجة التوافق +
خطة التنفيذ + تفاصيل SMC**.

---

## 6) شكل ردّ `/api/market-intelligence` (لوحة الاستخبارات)

```
sessionInfo   : {dateStr, weekday, isWeekend, hour, minute, totalMinutes, session:"CLOSED", sessionNameAr}
strengthMeter : xau{price,changePercent,rsi,score(0-100),statusAr,bias} 
                usd{symbol:"TVC:DXY",price,rsi,score,statusAr:"قوة الدولار تضغط هبوطياً",bias:"STRONG_USD"}
macroCorrelation: us10y{symbol:"TVC:US10Y",yield,changePercent,directionAr}
                  dxy{price,changePercent,directionAr}
                  macroBias:"NEUTRAL", macroVerdictAr:"⚖️ توازن ماكرو ..."
smtDivergence : {goldPrice,silverPrice,silverChangePercent,goldSilverRatio,smtType:"ALIGNED",
                 smtBadgeAr, smtVerdictAr}          ← الذهب vs الفضة (كشف الكسر الكاذب)
mtfMatrix.xau : {tf15m{code,labelAr,dir,dirScore}, tf1h, tf4h, tf1d,
                 buyCount, sellCount, alignmentCode:"FULL_SELL", alignmentTextAr}
liquidityMap  : {symbol,currentPrice,pdh,pdl,asianHigh,asianLow,distPdhPips,distPdlPips,
                 sweepType:"BELOW_PDL", sweepStatusAr}
activeBotTracker: {hasActive,count,sessionNameAr,isWeekend,trades:[{id,symbol,assetAr,direction,
                 directionAr,entryPrice,currentPrice,takeProfit1,takeProfit2,stopLoss,
                 profitPips,securedAt50,isLocked}]}
newsFreeze    : {isFrozen,event}
adminAlerts   : null | {pendingPaymentsCount}
newsRadar     : {alert{level:"SAFE"|"DANGER",titleAr,descAr},
                 events:[{titleEn,titleAr,currency,impact,impactAr,source,affectsAsset,
                          actual,forecast,previous,dayAr,timeAr,dateShort,timestamp,diffMinutes,status}]}
trackRecord   : {totalTrades,winsCount,lossesCount,winRate,totalPoints,goldPoints,gbpPoints,
                 activeCount,recentTrades:[...]}
```

**تفاصيل مُلاحَظة:**
- الأوقات «بتوقيت بغداد (UTC+3)» في الواجهة، لكن الخادم يحسب الجلسات بـUTC
  (`sessionInfo.hour` هو ساعة UTC).
- `newsRadar.events` تحمل `actual` (الرقم الفعلي) — **نفس درسنا**: الرقم يأتي من
  المفكرة، والفرق أنهم يظهرونه في الواجهة (`الحالي: x | المتوقع y | السابق z`).
- `adminAlerts.pendingPaymentsCount` يُستخدم لشارة حمراء على زر الإدارة — يظهر
  فقط لمن يرسل `x-vip-key` صالح (لغير الـVIP: `null`).
- `newsFreeze` موجود في الشكل لكن دائماً `{isFrozen:false,event:null}` حالياً.

---

## 7) ⭐ سجل الأداء «الموثّق» — قياس يكشف التضخيم

هذا أهمّ درس في التفكيك. اللوحة تعرض: **19 صفقة · 84% نجاح · +1120 نقطة**.
حسبنا الأرقام من **نفس بيانات الخادم** (`recentTrades`):

| المقياس | المعلن | الحقيقي (من exit−entry) |
|---|---|---|
| مجموع النقاط | **+1120** | **−353** (فرق **+1473** نقطة تضخيم) |
| «رابحة» | 16 | 2 صفقة TP2 فقط |
| «BE مؤمّنة» | 14 (كلها +50 نقطة) | **14/14 أغلقت مسطّحة أو خاسرة** |
| SL | 3 | 3 (متوافقة تقريباً) |

**الآلية:** كل صفقة تُغلق عند «تأمين الربح» تُسجَّل **+50 نقطة ثابتة** و«BE مؤمّنة»،
مهما كان سعر الخروج. مثال صارخ: `GC=F-SELL-1794834000` دخل 3869.75، خرج
**3872.86** (أي **−31 نقطة** فعلياً للبائع)، والمعلن **+50 نقطة 🛡️ تأمين ربح**.
صفقة ثانية دخل 3871.68 خرج 3872.32 (سلبية)، معلنة **+270 نقطة**.

**مصدر ثانٍ للتضخيم:** الصفقة `GC=F-today-tp2` بلا `entryPrice` ولا `exitPrice`،
لكنها تُحسب **+380 نقطة**. أي رقم مبنيّ على إدخال يدوي لا يُتحقّق منه.

> **الدرس لنا:** «موثّق آلياً» ليست ضمانة — هي ادّعاء. القاعدة عندنا: **لا تُعلن
> نتيجة صفقة بلا سعر خروج مقيس، ونتيجة BE = صفر لا +50**. سجلّنا يقيّم بالـR مع
> first-touch، و«تأمين الربح» حالة لا نقطة.

---

## 8) منظومة VIP / الدفع (الاقتصاد)

- **السعر:** 100 USDT شهرياً (TRC-20).
- **المحفظة:** `THmZivyb8he1TYvt5mCUFpiiC5p8SYoVdJ` (TRON/TRC20) — مكتوبة في HTML
  مباشرة + QR عبر `api.qrserver.com`. (⚠️ عند بناء نسختنا: لا نعيد استخدام محفظتهم.)
- **التفعيل:** إدخال كود `VIP-XXXX-XXXX` → `POST /api/vip/verify`.
- **بديل الدفع:** إرسال TxID + معرف تليجرام → `submit-payment` → مراجعة يدوية.
- **التواصل:** تليجرام `@forex_king2010` (محادثة) + قناة `@mm6_jh`.
- **الأكواد:** يولدها المالك بأي مدة: 6h، 12h، 1d، 7d، 30d (افتراضي)، 90d، 365d،
  **lifetime (99999)**. المفاتيح المحمية (`ownerKeysProtected`) لا تُحذف.

---

## 9) 🔴 الوضع الأمني (ما لا نقلّده)

1. **مفتاح VIP يعيش 10 سنوات** في `localStorage` **و** كوكي
   (`expires = Date.now()+3650 days`, `SameSite=Lax`, **بلا HttpOnly** — أي XSS
   يقرأه)، **ويتنقل في الرابط** `?vip=<key>` (يُخزَّن تلقائياً، ويسرّب في سجلات
   الخادم/الإحالات).
2. **باب خلفي للمالك:** `?dev=<secret>` → `checkVipStatus` يرسله إلى
   `admin-data`، فيرجع `ownerVipKey` **دائم** ويُخزَّن في الكوكي/الـlocalStorage.
   أي من يعرف السرّ (أو يراه في رابط مشارك) يصير «مالكاً».
3. **لا مصادقة جلسة إطلاقاً.** «السرّ» يُرسل في جسم كل طلب إدارة؛ لا JWT، لا
   HttpOnly cookie، لا CSRF token. كل نقطة إدارة مكشوفة لأي `POST` من أي أصل
   (CORS `*`).
4. **لا توقيع/تحقّق على TxID** — أي نصّ ≥10 أحرف يُقبل كدفعة.
5. **`access-control-allow-origin: *` على كل الـAPI** — أي موقع يستطيع قراءة
   التحليل وعدّاد الزوار من متصفح الزائر.
6. **تسريب داخل الحجب:** `signalArabic`/`signalClass`/`lastSwingHigh/Low` تظهر
   لغير المشترك رغم أن الواجهة تقول «🔒 محجوب». الحجب **عرضي** لا خادمي.
7. **لا هيدرات أمنية** (CSP/HSTS/X-Frame)، و`api.qrserver.com` طرف ثالث يرى المحفظة.
8. **الـ`#admin` في الرابط** لا يفعّل شيئاً (زر عادي)، لكنه يوحي بمسار إدارة.

**ما هو جيد عندهم:** لا source maps، لا ملفات إعداد/`.env` مكشوفة، لا توكن بوت في
الواجهة، ومسارات الإدارة ترجع 404 على `GET` (POST فقط).

---

## 10) 🐞 عيوب فنية مؤكدة بالقياس (bugs)

| # | العيب | الدليل |
|---|---|---|
| 1 | **`timeframe` مُهمَل** | 5m و30m و1h و1w كلها نفس الأرقام (rsi 28.7, atr 17.54, ema20 3763.45, fibH 3891.07). القيم المميزة فقط 15m/4h/1d. يبدو أن الخادم يقرأ شمعة واحدة ويقبل أي tf. |
| 2 | **زوج GBPUSD يرجع بيانات الذهب** | `currentPrice: 3724.73` (سعر ذهب!) بينما `ema20: 1.31644` و`atr: 0.00128` (باوند). خلط بين السعر والمؤشرات. |
| 3 | **`tvSymbol` خطأ لباوند** | `pairInfo.tvSymbol:"OANDA:XAUUSD"` حتى مع `symbol=GBPUSD=X` → الشارت يعرض الذهب للباوند. |
| 4 | **اسم الزوج لباوند = المعرّف** | `name:"GBPUSD=X"`, `arabicName:"GBPUSD=X"` (بلا ترجمة، بخلاف الذهب). |
| 5 | **`timeframe=abc` يقبل** | يرجّع 200 بلا تحقّق (fallback صامت). |
| 6 | **`/api/analyze` على زوج مجهول = 500** | `symbol=UNKNOWN` → HTTP 500 + رسالة «تعذر جلب الشموع» (كان الأجدر 400). |
| 7 | **حساب لوت ثابت** | `pipValueStandardLot = 10` للذهب **والباوند** معاً — وثّقوها بأنفسهم في تعليق: «$10 per pip for both». للذهب 1 لوت = 100 أونصة = **$100/نقطة**، فالحساب **أقل بـ10×** من الواقع. |
| 8 | **حاسبة اللوت تكشف SL للـVIP فقط** | `customSlInput` يُملأ من `executionPlan.riskPips` (وهو `"🔒"` لغير الـVIP → الحساب يسقط على 0.05 Lot افتراضي). |
| 9 | **تضخيم السجل** | §7 — +1473 نقطة وهمية، 84% «نجاح» بلا أساس. |
| 10 | **عدّاد زوار عام** | يُظهر `onlineNow`/`totalVisits` لكل زائر — تسويق لكن يكشف بيانات تشغيلية. |

---

## 11) استراتيجية «التوافق» كما تبدو (confluence model)

- **مصفوفة الفريمات:** 4 فريمات (15m/1H/4H/1D) لكل منها `dir ∈ {-1,0,1}` و
  `dirScore`. `alignmentCode` = `FULL_SELL`/`FULL_BUY`/مختلط؛ «تطابق كامل (4/4)
  — قوة دخول قصوى» عند الاتفاق.
- **مقياس القوة:** `score` 0-100 لكل من الذهب والدولار (DXY)، مع `bias`
  (`BEARISH`/`STRONG_USD`)، وربط عكسي (دولار قوي ← ضغط على الذهب).
- **الماكرو:** US10Y + DXY → «حكم ماكرو» (توازن/انحياز).
- **SMT:** تباين الذهب/الفضة ككشف كسر كاذب.
- **مصائد السيولة:** PDH/PDL + نطاق آسيا، مع `sweepType` (فوق/تحت) و«سحب سيولة».
- **التوافق النهائي:** `confluenceScore` + `confidence` (محجوبان) + `confluenceFactors`
  (قائمة أسباب) + `institutionalSummary`.
- **الإدارة:** تأمين ربح عند **+50 نقطة** (الذهب) / **+25** (باوند)، أهداف
  «+80 إلى +160 نقطة»، وقف ديناميكي حسب ATR.

> **درس لنا:** هذا **يشبه** بنية محرّكنا (اتجاه 6 فريمات + فلتر جودة + مستوى/خطة).
> لكن عندهم «26 مؤشر/فريم» ادّعاء تسويقي — الـ`mtfMatrix` يرجّع 4 حقول فقط لكل
> فريم (`code,labelAr,dir,dirScore`)، لا 26 مؤشراً. **ادّعاء ≠ قياس.**

---

## 12) خطة بناء نسختنا (MarketDesk Web) — ماذا نأخذ وماذا نرفض

### نأخذ (الفكرة/التخطيط، مع إعادة كتابة كاملة)
1. **لوحة ويب واحدة** تجمع كل قراءاتنا الموجودة: التحليل، الخطة، السيناريوهات،
   الخريطة، الأخبار، سجل الصفقات — بنفس ترتيب «المستوى العام ← التفاصيل».
2. **الشارت الحيّ** — TradingView widget (أو `map.html` الحالي عندنا للـcanvas).
3. **عدّاد إغلاق الشمعة** + مؤشرات الجلسات (عندنا `desk/session.py` جاهز).
4. **مقياس قوة + ماكرو (DXY/US10Y) + SMT (ذهب/فضة)** كلوحات معلومات.
5. **مصفوفة الفريمات** (عندنا `frame_agrees`/`trends` على 6 فريمات).
6. **حاسبة اللوت** — لكن **برياضيات صحيحة**: 1 لوت ذهب = $100/نقطة (عندنا
   `POINT_VALUE` و`account.py` جاهزان؛ **لا** نستخدم ثابت $10).
7. **سجل صفقات شفّاف** — بالـR، first-touch، بلا «BE = +50».

### نرفض (خطر/خدعة)
1. مفتاح VIP في الكوكي/localStorage/الرابط. → **جلسة HttpOnly من الخادم**.
2. أي `?dev=` backdoor. → لا وجود له إطلاقاً.
3. الحجب العرضي (تسريب الاتجاه في JSON). → **الحجب في الخادم**: لا يُرسل الحقل
   أصلاً لغير المشترك.
4. «تأمين ربح = +50 نقطة» وتضخيم السجل. → نتيجة = خروج مقيس، BE = 0.
5. ادّعاء «26 مؤشر/فريم» بلا بيانات. → نقول ما قِسناه فقط.
6. CORS `*` على كل شيء + `submit-payment` بلا تحقّق.

### عندنا مسبقاً (لا نعيد بناؤه)
- محرّك واحد `bot.py` (`plan()`, `report_payload()`, `_verdict()`, `card_*`).
- `app.py` (خدمة Render) يخدم `/map`, `/map.json`, `/chart.json`, `/price`, `/health`.
- `map.html` (المخطط الحيّ) + `desk/session.py` + `desk/account.py` + `desk/active.py`.
- الفجوة الحقيقية = **صفحة ويب واحدة «مثلهم تماماً»** تربط كل هذا. نبنيها من
  `app.py` الحالي، لا من الصفر.

---

## 13) جدول المقارنة السريع: هم ↔ عندنا

| الميزة | FX-AI Quant | عندنا (Ayzr1435) |
|---|---|---|
| محرّك القرار | مجهول (خادم مغلق) | `bot.py` `plan()` — مقروء، مقيّم |
| الإشارة المجانية | محجوبة (لكن الاتجاه مسرّب) | «تحليل فريق AYZR» للمالك |
| السجل | +1120 نقطة معلنة / −353 حقيقية | بالـR، first-touch، بلا تضخيم |
| تأمين الربح | +50 نقطة ثابتة (تُسجّل ربحاً) | حالة لا نقطة |
| مصادقة | لا جلسة، مفتاح في الرابط | جلسة (يجب أن تكون HttpOnly) |
| حاسبة اللوت | $10/نقطة (خطأ 10× للذهب) | `account.py` ($100/نقطة) |
| بوابة الدفع | USDT/TRC20 يدوي | (يحدّدها المالك) |
| السجل التاريخي | لا دراسة سنة | `duka_2026.json` (سنة كاملة) |
| الفريمات | 4 (وبعضها مُهمَل) | 6 (5m→1d) |
| الأخبار | رادار USD فقط | `_news_items_merged` + `news_result_watch` |

---

## 14) ملاحظات تقنية دقيقة (للمهندس)

- **الاستدعاءات الحيّة (intervals):** `updateClockAndSessions` كل 1s ·
  `fetchRealtimePrice` كل **2.5s** · `fetchPublicVisitorStats` كل 15s ·
  `fetchMarketIntelligence` كل 25s · `updateTicker` كل 25s · `runAnalysis` كل 30s.
  = **~0.4 طلب/ثانية لكل متصفح مفتوح** (كثيف على الـAPI المجاني).
- **لا `setInterval` يُنظَّف** عند مغادرة الصفحة (تسريب مؤقتات طبيعي في SPA بسيط).
- **TradingView:** يُعاد إنشاء الـwidget كاملاً عند كل تغيير زوج/فريم
  (`tvChartContainer.innerHTML=''` + سكربت جديد) — لا إعادة استخدام.
- **Markdown→HTML** يدوي (`formatMarkdown`) مع `escapeHtml` أولاً (جيد ضد XSS).
- **تعديل الدوم بأمان:** معظم الإدراج يمرّ بـ`escapeHtml`، لكن بعض المواضع تكتب
  أرقاماً من الخادم مباشرة في `innerHTML` (مثل `planEntry`) — مقبول لأن الأرقام
  من خادمهم.
- **الشعار** = SVG inline في `<link rel="icon">` (data-URI) — لا ملف favicon.
- **الألوان:** `darkBg #0b0e14` · `darkCard #121824` · `darkBorder #1e293b` ·
  buy `#10b981` · sell `#ef4444` · neutral `#f59e0b` · cyan `#06b6d4` ·
  indigo `#6366f1`. (لوحة داكنة أنيقة — جيدة كمرجع بصري، لا نسخ حرفي.)
- **قنوات/حسابات:** `@mm6_jh` (قناة البوت) · `@forex_king2010` (المالك) —
  **لا نلمسها ولا نتواصل معها**.

---

## 15) حدود هذا الفحص (الأمانة)

- **قراءة فقط.** لم نجرّب أي كتابة حقيقية؛ كل `POST` بقيم وهمية.
- **لم نجرّب تخمين كلمة سرّ الإدارة** ولا استغلال `?dev=` بأي سرّ — فقط وثّقنا
  وجود الباب من الكود.
- **لا نعرف مصدر بياناتهم** (مصدر الشموع/الأخبار) — الشكل يشبه Yahoo/مفكرة عامة
  (GC=F يظهر كمعرّف، والأخبار من «مكتب إحصاءات العمل» و«الفيدرالي»).
- **الأرقام لحظية** (وقت الفحص 2026-11-18 ~21:00 UTC) وتتغيّر — لكن **البنية
  والأخطاء بنيوية**، لا لحظية.
- **لا استخدام تجاري لعلامتهم/نصوصهم/محفظتهم** — المرجع للفكرة فقط.

---

*أُعِدّ بواسطة وكيل OpenHands (AI) بناءً على طلب المالك — فحص قراءة فقط لموقع
منافس، بلا أي تأثير عليه.*
