# Project memory — index

Three projects live in this workspace. This file is the durable index; the full
detail for each is in the archive beside it.

| project | where | detail |
|---|---|---|
| **GoldBot** | `src/goldbot/`, bot `@Xauusd1435_bot` | `GOLDBOT.md` |
| **AnalysisDesk** | `msy898931-boop/analysisdesk`, `/workspace/project` | `ANALYSISDESK.md` |
| **MarketDesk** | `msy898931-boop/Xauusd14355`, `/workspace/project/marketdesk` | `MARKETDESK.md` |

Daily logs (`YYYY-MM-DD.md`) hold the working notes; read one on demand.
**The user speaks Levantine Arabic — answer in Arabic.** Code/comments English.
Never echo secrets (`GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

## ⭐ The one lesson, in every shape
Every bug in these projects is **an unknown reported as a known** — silence
rendered as an all-clear, a missing read as a reassuring default, a made-up
number as a measurement. When something is unmeasured, say so; never substitute.
The corollaries that keep recurring:
- **The metric, not the code, is usually what lies.** Grade the *plan*, not the
  direction; report expectancy in R; check a result against a constant and a
  regime split before believing it.
- **"Configured" is not "working".** Probe (`getMe`, a real send, a write).
- **When two things must agree, generate one from the other** — never keep both
  by hand. When you find one copy of a rule, grep for its siblings.
- **A check that is also an intervention cannot observe** (a probe that wakes the
  thing it measures).
- **Look-ahead produces a beautiful, significant, wrong answer.**
- **Verify on every surface the value reaches**, not only the case that motivated
  the fix. Run mypy — it finds bugs tests miss.

## MarketDesk — the multi-agent market/news desk (current work)
`marketdesk/` — pure stdlib, no deps. `gather()` (biquote candles+quote → shared
SQLite) → `dispatch()` (**news agent FIRST, alone**, then the other 7
concurrently) → `synthesise()` (weighted scores + risk gate + plan). **8 agents:**
structure 2.0 · trend 2.0 · levels 1.5 · news 1.5 · momentum 1.2 · risk (veto) ·
volatility 0.8 · correlation 0.7. Each agent has private JSON memory + publishes a
`Study`. Run ~7 s with Groq, ~3 s keyless. Commands: `run` · `hit-rate` · `watch`
· `serve` · `agents` · `memory`. Add an agent = subclass + one line in `ROSTER`.
**Gates:** `pyproject.toml` + `marketdesk-ci.yml` run pytest/mypy/ruff on every
push/PR. **Never run `ruff format` here** — CI checks `check` only, and format
churns ~1200 lines. Tools are modules: `python3 -m ruff/mypy`.
Repo is **PUBLIC**, no licence, tracks `biquote/*.csv`.
- ⚠ **Groq 403 `1010` without a browser User-Agent** (Cloudflare) — same wall as
  Kraken/gold-api/FF. And a reasoning model's `max_tokens` must cover thinking AND
  the answer (800 → empty content; 1500 completes).
- ⚠ **Groq meters a SEPARATE daily quota PER MODEL** → `llm.py` keeps a chain and
  rotates on 429/402/403: `gpt-oss-120b → gpt-oss-20b → qwen3.8-27b → allam-2-7b`
  (`DESK_MODELS`/`DESK_MODEL` override). Never retry the same model on a 429.
- **biquote** (`biquote.io`, keyless MT5): quote carries `marketState`/`stale`/
  `quoteAgeSeconds`; `volume` is always 0 (use `tickVolume`); daily caps at **500
  bars (~20 mo)**, 4h=181, 1h=169. Its terms **forbid re-exposing OHLC**; the spot
  quote is the allowed case.
- **Telegram** deploy is **GitHub Actions** (free, no card, no second account —
  Render needs the owner's sign-in and Cron Jobs are paid). Bot answers pressed
  buttons via `getUpdates` long-poll; **the offset is confirmed only by a LATER
  call** — end with `confirm(next_offset)` or every pass replays the backlog.
  Secrets are owner-set (I can never set them: `/actions/secrets` → 403); a
  workflow log **cannot** distinguish an unset secret from a set one, so add a
  presence-only step.
- **Deploy facts:** the OpenHands GitHub App must have the repo in its
  `repository_selection` (switch it, don't assume); a PR opens as a **DRAFT** and
  needs GraphQL `markPullRequestReadyForReview` before merge; a commit message
  with backticks must go through `-F file`; **a GitHub `schedule` can take hours
  to register** — no runs yet is not yet evidence of breakage.
- **Session + news (PR #24):** `desk/session.py` states the spot-gold week in UTC
  (Fri 21:00 close → Sun 22:00 open); `Orchestrator.run(now=)` gates it **before any
  network call** (`DESK_RESPECT_SESSION=0` overrides). `fetch_news(symbol=...)` was
  **not** a filter — a `GOLD_TERMS` whitelist now drops what cannot move gold and
  the summary states the count.
Detail → `MARKETDESK.md`, `2026-11-02.md`.

## GoldBot — XAUUSD SMC/ICT bot
`src/goldbot/`, `PYTHONPATH=src python3 -m goldbot.cli <cmd>`. Own Groq key/bot.
**THE HEADLINE RESULT: the strategy is gold BETA, not skill** — it wins when gold
rises and loses when it falls, in every direction and frame. Do not tune
parameters to "fix" it. A control module + `cli study` now make that one command.
**AYZR** (a separate AI-first engine) is built but **not yet running**
(`AYZR_ENABLED` off by default). Live candles `kraken:PAXGUSD`; history
`data-api.binance.vision` (never `api.binance.com` → 451). State under
`GOLDBOT_DATA_DIR`. `goldbot.onrender.com` is **not** this project.
Detail → `GOLDBOT.md`, `2026-10-28.md`.

## AnalysisDesk — FastAPI market-analysis desk
Deployed `analysisdesk.onrender.com` (Render free, spins down after 15 min idle;
an outbound long poll is not inbound traffic, so the in-process clock freezes with
the host). Spot chain `biquote → Swissquote → gold-api`. **No keyless XAU/USD
candle feed exists** — gold-backed tokens are the proxy (Kraken PAXG primary,
Coinbase fallback), **~0.2% premium** which is a *level* bug, not a label bug:
`anchor_series_to_spot()` rescales before indicators, disclosed, never silent.
The in-process rate limit / error log are per-process → `--workers 1`.
Detail → `ANALYSISDESK.md`.

## MarketDesk — lessons worth keeping in the index
Detail for all of these lives in `MARKETDESK.md`; the daily log is `2026-11-02.md`.

- ⭐ **A close is a fact about a closed bar, not about now.** Indicators read closed
  bars; the price *compared against them* must be `live_price()`. Taking `closes[-1]`
  (daily, up to a day old: 3938 vs live 4065) made `trend`/`structure` vote bear
  56/56 while gold rose 3% — one bet placed 13×, not 13 losses.
- ⭐ **When every outcome has the same sign, it is a bug, not a view.**
- ⭐ **A live call needs live data.** Gate the ALERT on data age (`data_fresh()`,
  `DESK_MAX_DATA_AGE_S` 3h, measured from `fetched_at` so a quiet market is not stale).
- ⭐ **"Configured" is not "working"**; **a dead webhook is worse than none**; **"the
  token works" ≠ "the owner sees the message"** (print the chat via `getChat`). The
  session-local `TELEGRAM_BOT_TOKEN` here is 35 chars/no colon → `getMe` 404 while
  the repo secret is a valid 46-char token; only `getMe` proves a token.
- ⭐ **A currency tag is not a transmission channel.** Score a curated allowlist and
  state what was dropped; the sign is per-release — never one sign for all.
- **`actual=0.0` is a reading; a missing forecast is not a zero** → return `None`.
- **Audit for data stored but never read before adding anything.**
- ⭐ **"Alert once" is not "follow it".** `desk/active.py` holds the ONE plan in play
  and reports only transitions (waiting→on_track→target/stopped/invalidated) + a
  heartbeat; expires an unfired plan; drops one the fresh read contradicts. The
  ledger fingerprint `symbol:verdict:score` let a COMPLETED plan suppress the next
  same-direction trade forever → now `…:entry`. **The key was lying, not the code.**
  Fresh-VM runner: `active_plan.json` rides the cached `marketdesk/data`.
- ⭐ **Owner condition: keep the 10-year study OUT of my market analysis.** Base
  reading/recommendation/scenarios ONLY on live desk output. Never cite historical
  analogs, expectancy-R, n=47, or any 10y stat; never run `desk gold`.
- **Open risks (accepted):** one data source (biquote); Actions cache is best-effort
  (a duplicate alert is possible after eviction); scoreboard mixes pre/post-fix runs.
- **`origin` had a stale embedded token** that hung `git push` on a password prompt;
  fix = `git remote set-url origin https://${GITHUB_TOKEN}@github.com/...` +
  `GIT_TERMINAL_PROMPT=0`.
- ⭐ **A level is not a position.** The desk printed entry/stop/target in points and
  never a lot size or dollar risk — fatal on a small account. XAUUSD 1 lot = 100 oz
  ($1 move = $100), min lot 0.01 → a 42-pt gold stop risks **$42 = 21% of $200**;
  at 1% risk the widest affordable stop is **2 points**. `desk/account.py` sizes
  every plan/scenario (`lots = risk/(stop_dist×100)`, **floored** — never rounded
  up), refuses an unsizeable setup **with the number that proves it**, and takes a
  hard `DESK_MAX_STOP_POINTS` cap. `desk account` shows it; `DESK_BALANCE=0` keeps
  points-only. **A stop is not a risk until it meets a lot size and a balance.**
- ⭐ **A tolerance the desk cannot satisfy is a tolerance it refuses forever.** The
  owner's stop window (10-15 pts) is *exactly* the 15m/30m ATR band (measured:
  5m 5.7 · 15m 10.5 · 30m 14.7 · 1h 20.2 · 4h 36.9 · 1d 72.3) — the stop IS the
  frame ATR, so the desk had to reach down to those frames AND prefer the level
  whose frame fits the window over a nearer one it would only refuse (falling back
  to nearest when nothing fits). Adding a limit is half the job; the search must be
  able to produce something inside it.


## MarketDesk — later lessons (2026-11-05)

- ⭐ **A metric too bad to be true must be checked, not "fixed".** The board read
  **0/16 plans, -1.00R**; it looked like a grader bug, but tracing the raw plans
  showed the desk was short while gold rallied ~5% and was stopped out 16 times.
  The "fix" would have relabelled every loss `unfilled` — the same sin as grading
  direction-only. Verify against the data before touching the metric.
- **Two entry semantics, deliberately:** `scenarios.py` `entry` = a **breakout
  trigger**; `recommendation.entry` = **market entry at the current price**
  (`mid@issue == entry`). Fill logic must handle both or it hides losses.
- ⭐ **A once-a-day message belongs on a SCHEDULE, not a resident process.**
  The Render free instance for this desk got **suspended** (750 instance-hour
  cap; `/health` → 503 "suspended by its owner"), taking the daemon down with it.
  The GitHub cron kept running. So the daily map moved to `desk.cli map
  --once-a-day` in `marketdesk-watch.yml` (date stamped in the cached DESK_HOME),
  verified `map sent: True` then `map already sent today`. Do NOT add a second
  free Render service to "back up" the first — that is what trips the cap.
- ⭐ **The git remote's embedded token goes stale.** A push failed with
  `could not read Password for 'https://ghu_...'`; fix is
  `git remote set-url origin https://${GITHUB_TOKEN}@github.com/<o>/<r>.git`.
- ⭐ **A map is a view, not new math — and "manipulation" is not measurable.**
  `desk/map.py` clusters `indicators.pivots` into ZONES (0.25% ≈ 1×15m ATR, min 2
  touches), splits them by the live price into sell (above) / buy (below) zones,
  and adds liquidity = repeated highs/lows (where stops sit). Supply/demand and
  S/R are the SAME prices; do not sell them as two findings. Say "liquidity",
  never "manipulation". The map carries no buy/sell instruction. Sent ONCE a day
  by the daemon at `DESK_MAP_HOUR`, keyed on the calendar date, marked sent only
  on success (a dead token retries, not loses the day).
- ⭐ **An alert must carry the trade, not just the level.** `Level` has
  `call_side/stop/target/why`; a level clearing the desk's R:R floor (1.5) renders
  `🟢 اشترِ — stop, target, R:R, why`, else it stays a silent marker (never invent
  conviction). `from_scenarios` must get the owner's window + account, or the
  alert's stop won't match sizing.

## MarketDesk — Phase 0 closed + the acceptance scoreboard (2026-11-02, late)

- ⭐ **An acceptance criterion is a computation or it is decoration.** The plan
  doc's eight "when is this strong?" rows are now `desk acceptance`
  (`desk/acceptance.py`), each printing its measured number vs its target. Same
  lesson as the open-bar / budget / scoreboard ones — *the doc row, not the code,
  was the thing that could lie.*
- ⭐ **Split a gate row from an outcome row.** `Check.gate`: structural rows (one
  machine, no stored numbers, balance, decision rate, scenarios, quality) exit
  nonzero — defects. Market-outcome rows (`edge`, `calibration`) are marked 🟠 and
  do NOT fail the command: this market has no timing edge (t=−0.28) and the desk is
  not calibrated (27.3 pts), and *saying so is the criterion*. **A CI gate that goes
  red for a true market fact is a gate people learn to ignore.**
- ⭐ **The last hardcoded number shown as fact is gone.** `plan.py`'s
  `RUNG_REACH = {1R:0.683, 2R:0.540, 3R:0.413}` was one table for *both sides of
  every symbol*. Now `evidence.rung_reach(C)` measures per side (gold: long
  73/65/58%, short 47/18/12%), using `measure`'s own first-touch / ambiguous-bar-
  takes-the-stop rule; a side with no sample returns `{}` and the card prints no
  figure. `summary_line(m, reach)` carries it onto the recommendation card, so
  `/plan` and `/recommend` read one engine (the acceptance check verifies the two
  loaders' bar sets AND the evidence dicts agree).
- **Push:** this remote has **no credential helper** — a bare `git push` prompts
  for a username and hangs. Push with the injected token inline:
  `git push "https://x-access-token:${GITHUB_TOKEN}@github.com/<o>/<r>.git" main:main`,
  then `git fetch <same-url> main:refs/remotes/origin/main` to sync the tracking ref.
- ⚠ **Standing owner condition vs. this task's acceptance criteria — RESOLVED.**
  The owner said keep the 10-year study OUT of the market reading / plan /
  recommendations / scenarios. Section 6 of the plan the owner asked for still
  *requires* study-based validation (walk-forward edge, calibration). Resolution
  (`b6f0bed`): one flag, `Settings.show_study` (`DESK_SHOW_STUDY`, default OFF),
  gates every study statistic on every live surface; the study lives on as
  **validation/dev tooling** (`desk acceptance`, `walkforward`, `calibration`,
  `gold`). The plan card now prints the study's **verdict** (supported / not) but
  no reach %, no n, no control baseline. Nothing deleted — a display switch.
- Suite: **291 passed**; mypy clean (50 files); ruff clean.
- ⭐ **A trade without a stop cannot be graded, so it is not a record.** Added
  `desk journal add|close|grade` (`desk/journal.py`, `4293b00`) — the owner's own
  trades, measured the desk's way: **first touch wins, a bar spanning both takes
  the stop, expectancy in R not points**, an unresolved trade never a win. `add`
  refuses a missing stop. `data/journal.db` is git-ignored (private, not the
  blackboard). mypy caught 4 real `float | None` bugs here — the checker is a test
  the tests do not run.

## MarketDesk — single-agent automation + Telegram bot facts (2026-11-07)

- ⭐ **The owner asked to collapse the whole desk cycle into ONE agent.** The
  archive `Xauusd14355-full-project.tar.gz` (11 MB → 21 MB, repo
  `msy898931-boop/Xauusd14355`, **public**) holds the full MarketDesk project:
  11-agent desk + daemon + Telegram bot + chart + 5 GitHub workflows + 140
  commits + full `.openhands/memory`. Verified end-to-end: `pip install -e
  marketdesk`, then `DESK_RESPECT_SESSION=0 python3 -m desk.cli run --lang ar`
  runs gather→dispatch→synthesise keyless in ~12 s. Gates all green:
  **326 tests · mypy 52 files · ruff clean**.
- ⭐ **Telegram: the token and the chat id must BOTH match the same bot.** The
  bot is **@Ayzr1324_bot** (name Ayzr1435). A `sendMessage` to the *old* stored
  chat id returned `Bad Request: chat not found` — the stored id (914xxxxxxx)
  was the OLD bot's chat. The owner sent `/start` to @Ayzr1324_bot; `getUpdates`
  then returned the real chat **`933571066`** (user **Almalek121**), and a test
  send to it returned `ok: True`. **A bot cannot message a user until that user
  presses /start** — "chat not found" is the signature of that, not of a bad
  token. `getMe` on the old env token returned **404 Not Found**; the new token
  returned `ok: True`.
- ⚠ **Never write a bot token into a file, code, or an automation prompt** — it
  goes in the deployment's secret store as `TELEGRAM_BOT_TOKEN`; the chat id is
  fine in the prompt (it is not a secret). If a token is pasted into chat,
  recommend `/revoke` in BotFather afterwards.
- **Delivery bot differs from the repo workflows' bot.** `.github/workflows`
  reference `@Xauusd14355_bot`; the new delivery bot is `@Ayzr1324_bot`. Using
  the workflows' `--send` with the new token needs the repo secret
  `TELEGRAM_BOT_TOKEN` updated to the new token, or send with an explicit chat_id.
- **Automation (Agent Canvas form):** kind=prompt, repo public URL, cron daily
  07:00 UTC, timeout 900 s. Prompt = install, `gather`, `run --lang ar`, read
  verdict/gate/plan/scenarios/errors, send ONE Arabic Telegram message. Do NOT
  create/dispatch automations via API in a setup session — the user clicks
  Test / Create Automation on the form.

## MarketDesk — trade monitor + timings (2026-11-07, later)

- ⭐ **The cycle is fast; the network is the slow part.** Measured on the live
  repo: full cycle **16–18 s** (with Groq) / **12 s** keyless. Breakdown:
  `gather` **~4 s** (7 frames + quote from biquote), `dispatch` **~12–13 s**
  (news+calendar+Groq), `synthesise` **~0.03 s**, `scenarios` **~0.1 s**. So the
  desk is not heavy — it is waiting on the network. A 15-minute schedule spends
  18 s working and ~14:42 idle.
- **Monitoring is cheap:** one quote **0.4 s**, `watch_once` (levels) **4.4 s**.
  A 60-second check cycle is right; 10 s would just hammer the feed.
- ⭐ **The desk already HAS the follow-up state machine** (`desk/active.py`:
  `waiting → on_track → target/stopped/invalidated`, pure `evaluate()`, ledger
  fingerprints, heartbeat). What it LACKS vs the owner's example card is the
  *presentation*: progress bar, money P&L, and a profit-lock rule. The owner's
  sample (`🔭 متابعة الصفقة · GI-… · BUY · IN_TRADE · R:R 0.83` + `📊 التقدم` +
  `💵 العائم` + `🛡️ تأمين الربح`) is a richer shape; the repo's own message is
  plainer. Build on `active.py`, don't reinvent the state machine.
- ⭐ **A mirrored copy of decision logic must be diffed against the original.**
  The standalone runner reimplements `ActivePlan.evaluate`; the first draft had
  the stop check inverted (long stop below → wrongly `on_track`). Caught by
  running the mirror and `desk/active.py` over the SAME grid (both sides ×
  waiting/on_track × prices around entry/stop/target). Mirror it, then prove it
  matches — do not eyeball it.
- ⚠ **A stale `TELEGRAM_CHAT_ID` env value is a trap.** The old id (914xxxxxxx)
  was still exported; a script that prefers `os.environ["TELEGRAM_CHAT_ID"]`
  silently sent to the wrong chat (Telegram 400 `chat not found`). The verified
  id `933571066` must WIN over the env value; only explicit config overrides it.
- ⭐ **A demo/test plan must expire, or it spams forever.** Seeding a fake plan
  into the KV store means every 2-minute run re-sends a card. Added
  `demo_ttl_minutes` (default 10): a plan whose id starts `DEMO` is cleared once
  after the TTL, with one closing message. Test seed/repeat/expire as three runs.
- **Automation shape for the monitor:** kind=custom (deterministic, no LLM, no
  desk install), cron `*/2 * * * *` UTC, `entrypoint=python3 main.py`,
  timeout 120 s. State lives in the automation KV store (`AUTOMATION_KV_URL` +
  `AUTOMATION_KV_TOKEN`); the token is read with `get_secret("TELEGRAM_BOT_TOKEN")`.
  Code: `/workspace/project/main.py` (also `desk-monitor.py` for local runs).
- **The full cycle test on a closed market is a FEATURE demo, not a bug:** with
  the market closed the desk correctly returns `no-trade` and the gate says why
  (market closed / stale quote). Verified 3 monitor cycles at 2-minute spacing.
- ⭐ **"Run it permanently" = run forever, speak only on change.** A forever
  monitor must not send an unchanged card every 2 minutes (that is the flood the
  desk's own `notify.py` warns about). Final shape: silent when no plan; seed a
  demo plan **once** (guarded by a `demo_done` KV flag, not every run); send on
  every status change; optional `heartbeat_minutes` to re-send an unchanged
  status. The demo plan still self-expires (`demo_ttl_minutes`) so a test cannot
  become permanent spam. Verify all five paths: seed → repeat → expire → silent
  → real trade.

## MarketDesk — Telegram recipients (2026-11-07, later still)

- **Two subscribers on the delivery bot `@Ayzr1324_bot`:**
  - **رمضان عوينات / @Almalek121 → chat `933571066`** (the owner)
  - **"00" / @E_1_code → chat `8545528489`** (a friend the owner added)
- ⭐ **A bot cannot message anyone who has not pressed /start.** The friend
  appeared in `getUpdates` only after he sent `/start`; that call is what unlocks
  the chat. To find a new recipient: `getUpdates` → read `message.chat.id`. Do
  NOT guess or reuse an id.
- **Broadcast:** the monitor's `chat_id` takes a comma-separated list
  (`"933571066,8545528489"`), and `send()` fans out with `all(...)` so one bad
  chat cannot silence the others.
- **Every message to a human carries an AI-disclosure line** ("أُرسل بواسطة وكيل
  OpenHands (AI)…"). Keep it on any outbound content a person reads.
- **A closed-market "تحليل" is still worth sending** — the decision is honestly
  `no-trade`, but the levels/scenarios/structure are the useful part. Send the
  read; do not dress it up as a call.

## MarketDesk — @Ayzr1324_bot (the owner's ONE bot), 2026-11-07

- ⭐ **The owner has exactly ONE bot: `@Ayzr1324_bot` (id 9012677710).** The
  repo also names two other bots in its own memory (`@Xauusd14355_bot`,
  `@Xauusd1435_bot` / GoldBot) — those belong to the CODE, not to him. He said
  plainly: "هاد البوت نفسو بدي عليه تشتغل … ماضل عندي غيرو". Do not build on the
  repo's bot; build on his.
- ⭐ **The environment's `TELEGRAM_BOT_TOKEN` is a DEAD token for a different
  bot** — `getMe` returns 404 and the prefix is `AAHX…`, not `9012677710:…`.
  A `token()` that read the env first made every SEND 404 while every local read
  looked fine. Fix: the local `.tg_token` file (mode 600) wins over the env.
  Always confirm with `getMe` that the token in use is `@Ayzr1324_bot`.
- **The bot lives in `/workspace/project/bot.py`** — one self-contained stdlib
  file (no repo, no desk install). Cards: analysis / recommendation / scenarios
  / levels / open trades / journal (day·week·month). Journal is a local SQLite
  (`data/journal.db`). Menus: `MENU` + `JOURNAL_MENU`; both recipients
  (`933571066` owner, `8545528489` friend) are seeded in `data/allow.json`, and
  a new `/start` self-adds to that file.
- **Run it forever:** `nohup ./run-bot.sh &` (restarts on exit). Poll loop +
  a 2-minute monitor thread that pushes a follow-up when an open trade's status
  changes.
- **The journal grades the desk's way:** 5m bars, first touch wins, a bar that
  spans both takes the stop, results in R, an unpriced trade (no size) is named
  and never counted as $0.

## MarketDesk automation (cron) — 2026-11-07

- ⭐ **An always-on process needs a host; a cron automation IS the host.** The
  owner had no server, so the bot was reframed as a per-run poller: wake, read
  Telegram updates since last run, answer them, grade open trades, exit. Cron
  `* * * * *` (every minute). No LLM — pure stdlib — so a minute cadence costs
  only CPU. Script: `automation/main.py` (also mirrored into the setup form).
- **Telegram offset with no persisted state:** each run reads `getUpdates`, then
  calls `getUpdates(offset=last_id+1)` once more to CONFIRM them. That makes the
  next run never re-read what was answered, without storing an offset.
- ⭐ **State that must survive a run goes in the automation KV store**
  (`AUTOMATION_KV_TOKEN` + `AUTOMATION_API_URL` -> `PUT/GET /v1/kv/{key}`), NOT a
  local file: a cloud run's disk is thrown away. Keys used: `journal`, `allow`,
  `monitor_seen`.
- ⭐ **The token constant `BOT_TOKEN` is the owner's explicit choice** (he asked
  for "بسيط" over a secret). It is still validated with `getMe` before use, so
  the dead env `TELEGRAM_BOT_TOKEN` can never shadow it.
- ⭐ **A resolution alert must name the trade, not the remaining book.** The first
  draft sent `card_monitor` on close, which reads "ما في صفقة مفتوحة" at the exact
  moment the trade resolved — a real defect. Fixed: `_resolution_line(t)` says
  "✅ الهدف تحقق / ❌ الوقف ضرب — الصفقة #id ... {r:+.2f}R" then "باقي مفتوح: n".
- ⚠️ **Only ONE poller may run at a time.** A resident `bot.py` and the cron
  automation both call getUpdates on the same token and steal each other's
  updates. The local poller was stopped before the automation went live.
- **Menu shape (both files):** MENU (analysis·recommend·scenarios·levels·open·
  journal_menu·howto·help) and JOURNAL_MENU (j_day·j_week·j_month·journal·
  monitor·menu). Every non-menu reply carries a 🔄 refresh + 🏠 menu row.

### gold market hours + quote age (2026-11-07)

- ⭐ **"عمر التسعيرة 1316 دقيقة" was NOT a bug — it was the weekend.** Gold
  (XAUUSD) closes Fri ~22:00 UTC and reopens Sun ~22:00 UTC. On Saturday
  `marketState=closed`, `stale=True`, and `quoteAgeSeconds` ~80,000 (=22h) —
  biquote keeps serving the last close (mid unchanged) with the closing
  timestamp. Any age up to ~48h over a weekend is normal.
- ⭐ **A raw minute count is a bad unit for a weekend gap.** `1316 دقيقة` reads
  like a fault. Replaced with `human_age()`: seconds → minutes → hours → days,
  and the closed branch now prints the exact timestamp ("آخر تسعيرة:
  2026-11-06 21:57:59 UTC — قبل 22 ساعة") plus a line saying the price is the
  last close and the reading is on the last session. `dayDiffPercent` is
  relabelled "تغيّر آخر جلسة" when closed, so it is not read as a live move.
- **How to tell "closed" from "broken feed":** a real outage usually shows
  `stale=True` with an age far beyond the weekend window, or no `timestamp`.
  The weekend window is ~48h; anything past that is worth investigating.

## MarketDesk automation → hosting hunt (2026-11-07, late)

- ⭐ **The OpenHands "New Automation" the owner built was the wrong KIND.** It
  was a **prompt preset** (`endpoint=/v1/preset/prompt`) whose `prompt` was just
  `"@Ayzr1324_bot"` — a bot name, not code. It created a run that started an LLM
  conversation and stalled; a second attempt uploaded a **581-byte** tarball
  (placeholder, not the 900-line bot) on a **daily 09:00** schedule. Mechanically
  it "ran and completed", which is exactly why it looked fine and did nothing.
  Diagnosis came from the API, not the UI: `GET /api/automation/v1` (the list),
  `.../drafts`, and `.../{id}/runs`. The tarball's real size is in
  `GET /api/automation/v1/uploads/{id}` → `size_bytes`.
- ⭐ **This is a setup session: the form is the deploy button, and the agent
  must not press it.** The agent fills the form; the owner clicks Create. Do not
  create/patch/dispatch automations via the API here even when asked.
- ⚠ **Render is dead for this account: every one of 11 services is
  `suspended` with `suspenders=['billing']`.** `POST .../resume` answers "only
  services suspended by a user can be resumed"; `POST .../deploys` answers
  "cannot deploy suspended service". Deleting services would NOT unblock it —
  the block is on the account, so it needs the owner's billing fix. **Before
  deleting anything, back up the service env vars**: `goldbot` alone held 10
  (bot tokens, AI keys). Saved to a 0600, git-ignored `render-backup.json`.
- ⚠ **The injected GitHub token cannot create repos or write Actions secrets
  (both 403)**, and the owner's existing repos are all **public**, so a bot
  token must never be committed to them.
- **Decision: Fly.io**, because it needs no GitHub repo and no billing fix.
  Package in `flybot/`: `bot.py` (the long-polling bot — `poll_forever` +
  `monitor_loop`, the right shape for an always-on host), a slim Dockerfile,
  `fly.toml` (`app=marketdesk-ayzr`, fra, `shared-cpu-1x`, 256MB, a 1GB volume
  at `/app/data` so the SQLite journal survives redeploys), and `deploy.sh`.
  Blocker: **`FLY_API_TOKEN`** — only the owner can create the Fly account.
- Note: two entry shapes exist and must not be confused — `bot.py` (resident
  long-poller) vs `automation/main.py` (one cycle per cron wake). Both are kept.

### Fly.io: the trial 5-minute wall (2026-11-07)

- ⭐ **A Fly *trial* account kills every machine after exactly 5 minutes.**
  The log line is unmistakable:
  `[warn] Trial machine stopping. To run for longer than 5m0s, add a credit card
  by visiting https://fly.io/trial`. The machine exits `exit_code=0,
  requested_stop=true` at ~300s. `billing_status: TRIAL_ACTIVE`,
  `paid_plan: True`, `billable: False`. **Adding a card is the only fix** — no
  CLI flag or config overrides it. An always-on bot cannot run on a trial.
- ⭐ **A Fly token can be scoped so it READS but cannot CREATE.** Symptom:
  `flyctl auth whoami` and `flyctl orgs list` succeed, `apps list` fails
  `unauthorized`, and `createApp` → `UNAUTHORIZED`. The read that DOES work is
  `GET https://api.fly.io/api/v1/organizations?admin=false` (and the GraphQL
  `organizations` query); app creation goes through `api.machines.dev`, which
  403s. Diagnosis path: `LOG_LEVEL=debug flyctl apps create ...` prints the
  exact failing request (`403 https://api.machines.dev/v1/apps`).
- **`createApp` needs the org's opaque `id`, not its slug.** Passing
  `organizationId: "personal"` → `Could not find Node with id 'personal'`.
  Get the id from the GraphQL `organizations { nodes { id slug } }` query
  (here: `P7lZB0nw2ylg8sm1P5Rqp9JzZqIZBJ0ny`).
- **There was already an app `xauusd14355`** (region `ams`, created by the
  owner), running `python -m desk.cli daemon --web`. It is the only app the
  token can WRITE to (a volume create there succeeded). Reused it: `fly.toml`
  now points at `xauusd14355` / `ams`, with the 1GB `marketdesk_data` volume at
  `/app/data`. Note its machines have no volume attached yet, so the mount must
  be re-checked after any deploy.

### The automation route — done right (2026-11-07, final)

- ⭐ **A resident bot cannot live on an OpenHands cron automation, but a
  near-continuous listener can.** The cron wakes the script each minute; if the
  script reads updates once and exits, a tap waits up to 60s. The fix in
  `automation/main.py`: **long-poll for ~40s** (`getUpdates timeout=20`, looped
  until a 40s deadline), then grade/monitor and exit — so a tap is answered in
  ~1s and the run still finishes before the next wake.
- ⭐ **Two pollers on one token = Telegram 409, and both go blind.** Never let a
  run overlap the next. The 40s deadline inside a 60s schedule guarantees a gap.
  The loop also backs off (`sleep 2`) when an empty answer returns in <2s, which
  is the tell-tale of a competing poller.
- **The failure that started this: the automation was the wrong KIND.** The
  owner's was a **prompt preset** (`/v1/preset/prompt`) whose `prompt` was the
  literal string `"@Ayzr1324_bot"` — a bot name, not code — on a daily 09:00
  schedule, with a 581-byte placeholder tarball. It "ran and completed" and did
  nothing. The correct shape is `kind=custom`, `entrypoint=python3 main.py`,
  full code, `* * * * *`.
- **KV, not files.** A run has no disk: `journal`, `allow` and `monitor_seen`
  live in the automation KV store (`AUTOMATION_KV_TOKEN` + `AUTOMATION_API_URL`);
  local `data/*.json` is only a local-run fallback.
- **Everything else was blocked:** Render (billing), Fly (5-min trial wall),
  GitHub Actions (token can't write secrets; repos public). The automation needs
  no card and no extra account — it was the only route left, and it works.

### Where the MarketDesk project lives (2026-11-08)

- ⭐ **The workspace is the ONLY copy — and there was no backup.** `/workspace`
  is an ephemeral session volume; `/workspace/project` is a git repo with **zero
  commits and no remote**, so nothing was persisted anywhere. The 12 public
  GitHub repos (`msy898931-boop/*`) do NOT contain this bot (`bot.py` /
  `automation/main.py` appear in none; `Xauusd14355` holds the OLD 11-agent
  `marketdesk/` desk, a different thing).
- ⭐ **The injected GITHUB_TOKEN is READ-ONLY: it cannot push or write.** `GET
  /user/repos` works and shows `permissions.admin=true`, but `git push` →
  `403`, and `PUT /repos/{o}/{r}/contents/...` → `403 Resource not accessible by
  integration`. `x-oauth-scopes` is empty. So a repo the owner creates is NOT
  enough — the agent cannot upload to it. Do not promise an upload.
- **Deliverable that DID work:** a sanitized tarball served from the sandbox's
  port 12000 via `python3 -m http.server`, reachable at
  `https://work-1-<id>.prod-runtime.all-hands.dev/<file>` (port 12000 → work-1;
  work-2/12001 gave 502). Sanitize first: the bot token is hardcoded at
  `automation/main.py:41` as `BOT_TOKEN = "9012677710:..."`; replace it with
  `REPLACE_WITH_YOUR_BOT_TOKEN` before any upload, since the owner's repos are
  all PUBLIC and a leaked bot token hands over the bot.
- **The real durable home is the automation tarball.** Clicking *Create
  Automation* stores the code server-side; that, not the workspace, is what
  survives.

### ⭐ GitHub write access: it is a per-repo APP INSTALLATION, not a scope (2026-11-08)

- **The token is a GitHub App token (`openhands-ai`, client `Iv23liz...`), not a
  PAT.** So `x-oauth-scopes` is empty and the classic "add a scope" advice is
  useless. Write ability comes from the app **installation's**
  `repository_selection`:
  - `"selected"` → only the listed repos are writable; every other repo returns
    `403 Resource not accessible by integration` on PUT/push **even though the
    repo's `permissions` object says `push: true`** (that field reflects the
    user's rights, not the app's).
  - The app's own permissions were already `contents: write`,
    `pull_requests: write`, `workflows: write` — full write. The wall was purely
    which repos were selected.
- **Diagnosis recipe:** `PUT /repos/{o}/{r}/contents/x` returns 201 vs 403 — a
  one-call answer. `GET /user/installations` shows `repository_selection`.
  `/installation/repositories` is itself 403 (needs a JWT, not the installation
  token), so probe per repo instead.
- **The fix is the OWNER's, and only the owner's:** the agent cannot change the
  installation (that call is 403). Send them to
  `https://github.com/apps/openhands-ai` → *Configure* → set **Repository
  access** → *All repositories* or select the repo → *Save*. It takes effect
  immediately (no re-auth, no new token).
- **`OH_GIT_CREDENTIAL_ACCESS_TOKEN_GITHUB` is NOT a usable token** — it returns
  `Bad credentials`; it is for the git credential helper's own handshake. Use
  `GITHUB_TOKEN`.
- **Push recipe that works:** `git push
  "https://x-access-token:${GITHUB_TOKEN}@github.com/<o>/<r>.git" main:main`
  (no credential helper is configured; a bare push would hang on a password
  prompt). If the remote already has commits, `git fetch` + `git merge
  --allow-unrelated-histories` rather than force-push.
- **Always sanitize before pushing to a PUBLIC repo:** the bot token was
  hardcoded at `automation/main.py:41`; replace with
  `REPLACE_WITH_YOUR_BOT_TOKEN` (`.openhands/build-upload.sh` does the copy +
  scrub). A leaked bot token hands over the bot.

### The manual automation was created — and it needs 4 fixes (2026-11-08)

- **The owner created it by hand** (a "create automation" page, not the setup
  form). Id `9f79c5ab-4eb1-413f-b1a7-2e360085d685`, name `MarketDesk Bot —
  @Ayzr1324_bot`. The API *is* readable for this: `GET
  /api/automation/v1/{id}` with `Authorization: Bearer $OPENHANDS_API_KEY`.
- **Four defects to fix, all visible in that GET:**
  1. `trigger.schedule = "33 10 8 11 *"` — a one-shot (8 Nov 10:33), not
     `* * * * *`. The page's schedule picker had defaulted to "once".
  2. `enabled: false` / `state: INACTIVE`.
  3. `entrypoint = ".venv/bin/python main.py"` + `setup_script_path =
     "setup.sh"` — the page's template, but the bot is pure stdlib and needs no
     venv; plain `python3 main.py` is right (a missing venv/setup would fail the
     run).
  4. **`prompt` is the code, and the Arabic is mojibake** (`ًںٹطھط...`):
     2544 Arabic-range chars present but garbled, so every menu label, every
     card, is broken. Cause: it was pasted from my download link, and
     `python -m http.server` serves `.py` as `text/plain` **with no charset**,
     so the browser decoded UTF-8 bytes as Latin-1. Fix: serve with
     `charset=utf-8` (see `public/serve.py`), or paste from a UTF-8-clean source
     (`raw.githubusercontent.com` of the pushed repo sends the right charset).
- ⭐ **A download link is a data-integrity surface.** Serving a UTF-8 file
  without a charset silently corrupts non-ASCII content on paste. Always set
  `Content-Type: text/plain; charset=utf-8` for code served for copy-paste, and
  prefer `raw.githubusercontent.com/<o>/<r>/<branch>/<path>` for the same file.
- The bot token placeholder in the *served* copy is
  `PUT_YOUR_BOT_TOKEN_HERE`; the owner must put the real token back, or the
  automation's `token()` falls through to a dead candidate.

## The bot is live — resident poller + watchdog (2026-11-08)

- **`bot.py` is the resident bot** (sqlite journal at `data/journal.db`, not KV —
  that is the *scheduled* bot's shape). It reads the token from `.tg_token`
  first, then `TG_TOKEN`/`TELEGRAM_BOT_TOKEN`. It seeds two chats and **adds
  anyone who messages it** to `data/allow.json` (a third chat, `997487756`,
  joined this way — the allow-list is self-growing by design).
- **Two concurrent pollers make Telegram answer 409 and blind BOTH.** Before
  starting the resident bot, check the scheduled automation is not polling, and
  vice-versa. `getWebhookInfo` must show no webhook.
- **`watchdog.sh`** restarts the bot if it dies, but only inside this container.
  It is a crash-restart, **not durability** — the container is ephemeral.
- ⭐ **A running bot is not a durable bot.** The owner asked "if I stay here, can
  it keep running?" — the honest answer: yes while the session lives (it is
  detached via `nohup`, survives closing the tab), but the session is a
  temporary runtime and I cannot make it permanent. Permanent options, in
  order of what I recommended: the automation (once its `Custom` kind sticks),
  Fly.io (`flybot/` is ready, needs `FLY_API_TOKEN`), or his own machine/VPS.
- **Clean up after a smoke test.** My `--selftest` and a direct send left a
  trade (`id 6`, `opened_at 2026-11-08T10:08:03Z`) in the owner's journal.
  Deleted it by (id, opened_at) so only my row went. Test writes to a user's
  data must be rolled back.
- The token in the *pushed* repo stays `REPLACE_WITH_YOUR_BOT_TOKEN`; the
  *served* `main-ready.py` / `marketdesk-custom.tar.gz` carry the real one and
  must never be committed.

## MarketDesk — Render: the quota is a workspace-wide kill switch (2026-11-07)

- ⭐ **750 free instance-hours are per WORKSPACE and shared by every free
  service; run out and Render suspends them ALL** until next month. 11 services
  on the old workspace → all suspended 2026-11-05, `Monthly Quota Exceeded`.
  **One free service only** (~720 h/mo). A second service "as a backup" is what
  trips the cap — the same mistake, wearing a different hat.
- ⭐ **A free web service sleeps after 15 min with no inbound traffic, and a
  sleeping bot answers nothing.** So `/health` + a keep-alive every 5 min. A
  *web* service (not a background worker) because a background worker is a PAID
  type, and a web service can hold the same long-polling threads anyway.
- ⭐ **Render free = no persistent disk, no KV persistence.** `journal.db` is
  ephemeral there; the bot survives a restart, the journal does not. Durable
  needs a paid instance + disk at `/app/data`, or a pinned Telegram backup.
- Render appends a suffix: `marketdesk` → `marketdesk-1o4g.onrender.com`.
- Live: one free web service `marketdesk`, `/health` 200, Telegram 409 (Render
  owns the poll), auto-deploy on, keep-alive workflow run #1 success.
- Details: `.openhands/memory/2026-11-07.md`.

### Render: durable journal + self-keep-alive (2026-11-07, late)
- **Free Render = ephemeral disk.** Durable journal without paying: upload it to
  a chat as a document and **pin** it; restore from `getChat` →
  `pinned_message.document` at boot, only into an empty journal. `getChat`
  returns `pinned_message` **only when something is pinned**.
- **A free service can keep itself warm** by pinging its own
  `RENDER_EXTERNAL_URL` every 10 min (inbound by arrival). It cannot detect an
  outage — that is what an uptime monitor is for.
- **The Render API key is account-scoped** (old key 404s on the new account's
  service). `DELETE /v1/services/<id>` → 204. A deploy stuck at
  `update_in_progress` may already be done — trust a rising `/health` uptime.
- **This GITHUB_TOKEN cannot set Actions variables or create gists** (403), so a
  workflow must carry its own default URL.

### ⭐ "The bot is silent" on Render = the port was bound too late (2026-11-07)
- A Render **web service must bind its port before anything that can block.**
  Calling a network function (a Telegram restore) before `serve_forever` made
  Render's port scan fail → `Port scan timeout reached, no open ports detected`
  → kill and restart, forever. Serve first; do everything else after.
- Diagnose in this order: **`getUpdates`** (a 409 = a poller holds the lock; a
  *result* = nothing is polling) → **`/health` uptime_s** (small = it restarted)
  → Render logs for `Port scan timeout`. **`getWebhookInfo.pending_update_count:
  0`** is the cleanest proof the bot is consuming.
- Run the restore off the critical path (own thread, bounded join), supervise
  every loop (restart a thread that dies), and keep `/health` unable to block.

### ⭐ Chat ids are strings — never `sorted()` them to pick a person (2026-11-07)
- The journal backup was landing in the **guest's** chat, not the owner's:
  `sorted(load_allow())[0]` with ids as strings put `'8545528489'` before
  `'933571066'`. Pick the owner by name (`SEED_CHATS[0]`), never by sort order.
  Owner = `933571066` (@Almalek121 / رمضان عوينات); guest = `8545528489`
  (@E_1_code / "00").
- The backup lives as a **pinned document** in the owner's chat with the bot —
  that is where the owner can see it. Remember the pin in `data/pin.json` and
  look there first on restore.

### The desk trades its own book (2026-11-07)
- **Gold lot math:** 1 lot = 100 oz, so **0.01 lot = $1 per $1 move** -- the
  owner's $ window *is* the point window. Keep every knob in dollars
  (`POINT_VALUE = LOT*100`); `points = usd / POINT_VALUE`.
- **One engine:** `plan()` returns the sized decision or None; `card_recommend()`
  renders that same plan. Never a second copy -- the card would drift from the
  trade the desk took.
- **A stop inside the noise is not a stop:** place it just beyond the nearest
  level (long below support / short above resistance), then cap into the window.
- **Two-trade rule:** open if `len(open) < MAX_OPEN` and (no same-direction open
  OR read is strong ≥5/6 frames); if the first is ≥50% to target, decline a
  same-direction signal ("the first is enough").
- ⚠️ **Signed vs absolute:** `_winning()` compared signed progress against a
  signed span, so a short at a LOSS counted as winning. Compare `abs(span)`.
- `desk_tick()` split from `desk_loop()` for offline testing; `DESK_AUTO=0` is
  the kill switch.

## MarketDesk — one engine, many entry points (2026-11-07, post-review)

- ⭐⭐ **A review that reads FILES, not memory, is what finds entry-point bugs.**
  `bot.py`'s `main()` ran `monitor_loop` (watch-only) while `app.py` ran
  `desk_loop` (the auto-trader) — so the README's "permanent bot for a VPS"
  would have *silently never traded*. Fixed: `bot.py` now has ONE
  `start_desk()` (the only place the desk's threads are listed) + `_supervise()`;
  both `bot.py main()` and `app.py` call it. The poller is NOT in `start_desk`
  (a VPS wants it in the main thread for Ctrl+C; Render wants it supervised).
  `monitor_loop` deleted — `desk_tick` already sends the closure card.
- ⭐ **Prove a no-behaviour-change refactor with `git diff` on the decision
  functions, not with hope.** After the change,
  `git diff HEAD -- bot.py | grep -E 'card_analysis|card_action|_verdict|_sized|plan|frame_agrees'`
  was empty and Render health stayed `ok` — that is the acceptance test.
- **Remove a fork without breaking a deploy:** keep the deployment dir, delete
  the *committed copy*, and have the deploy script `cp` the real engine in at
  build time (`.gitignore` the generated path). `flybot/` now does this.
  `automation/main.py` stays — a genuinely separate scheduled runner — but its
  docstring says "not the engine; change `bot.py` first".
- **Tests now exist:** `tests/test_engine.py`, 21 stdlib `unittest`, offline
  (stubs `quote`/`bars`). Run `python3 -m unittest discover -s tests`. Baselines:
  ruff `bot.py` 16, mypy 2. Gotcha: `grade()` reads bars at/after `opened_at`,
  so tests backdate `opened_at` before grading.

## Ayzr1435 — the gold "professional combo", validated on all of 2026 (2026-11-07)

- ⭐⭐ **The combo is a real, stable edge — verified on a full year, not a
  lucky stretch.** Config: two-frame trend (15m + 1h) + EMA20, ADX 20–40,
  ATR 0.08–0.25%, MACD up, RSI 35–58, **stop $10 / target $18** (R:R 1.8).
  On **2026-01-02 → 2026-11-08** (Dukascopy XAUUSD, 26,784 15m bars, 279 days):
  **160 trades · 43.8% win · +36.0R · +$2.25/trade · max DD −10.4R · longest
  losing streak 8** (≈ +$72 on a $200 account at 1% risk).
- ⭐⭐ **Every quarter was green** (Q1 +14.8R · Q2 +5.8R · Q3 +5.4R · Q4 +10.0R)
  and it **made money in the DOWN months** (Mar −11% +0.4R · Jun −11.8% +6.2R ·
  Sep −6.7% +8.0R · Oct −6.7% +9.2R). It is not just riding gold's uptrend — it
  takes shorts too (long 69 / 46% / +20.6R · short 91 / 42% / +15.4R).
- ⭐ **Sample size changes the number, not the verdict.** The same combo read
  **60% win on a 59-day / 42-trade sample** but **43.8% on the full year** — the
  small sample was optimistic. A small-sample win rate is a *hope*, not a fact.
- ⭐ **Exit config on the full year:** $10/$18 = +36.0R (best) · $10/$15 = +36.5R
  · $9/$11 = +30.3R · $12/$20 = +22.7R · $8/$14 = +20.0R.
- ⭐ **RSI alone is NOT enough.** Layer-by-layer (59d, $9/$11): no filters 288pf
  45% −1.3R · **RSI alone 203pf 48% +12.6R (not stable)** · RSI+MACD 134pf 47%
  +6.0R · RSI+MACD+ADX 69pf 57% +17.7R · **full (+ATR) 62pf 58% +18.0R (stable)**.
  **ADX is the heart of the combo** — adding it alone lifted the win rate 47%→57%.
  Narrowing RSI to 35–55 raises the win rate but cuts trades and total profit.
- ⭐ **The combo beats a random entry** (200-trial random baseline: +$0.06/trade
  avg, best +$2.55; combo +$2.90) — the filters do real work, it is not chance.

### Data source for a full year of gold (Yahoo cannot do it)
- Yahoo gives 15m only for the **last 60 days**; for a full year use **Dukascopy**:
  `https://datafeed.dukascopy.com/datafeed/XAUUSD/{Y}/{M-1:02d}/{D:02d}/BID_candles_min_1.bi5`
  — LZMA, 24-byte records `>iiiiif` = (sec-of-day, open, close, low, high, vol),
  **price = raw / 1000**, bucket the seconds onto the UTC day. Weekends come back
  empty. Loader: `backtest/fetch_duka.py` (12 threads → `duka_2026.json`, aggregates
  to 15m/30m/1h/4h/1d). `Frames("duka")` reads it. Full year ≈ 100 min serial,
  ~8 min at 12 threads.
- ⚠️ `bot.bars()` (the desk's own feed) serves only ~193 15m bars (~2 days) and
  Yahoo `GC=F` is a futures *proxy* — neither is a verdict on XAUUSD spot.

### ⚠️ Dukascopy gotchas + the bot-vs-combo verdict (2026-11-07)
- ⚠️ **Dukascopy PADS the closed market with flat bars (high == low).** All of
  Saturday, most of Sunday, and Friday after the close come back as filler at
  one price (measured: Sat 59,040 flat bars, Sun 57,434, Fri 8,462). Those are
  not the market — they add fake "days" and a dead stretch. Keep the session
  only: **Sunday 22:00 UTC → Friday 21:00 UTC**, and drop any bar with h == l.
  Script: `backtest/clean_duka.py` (421,920 min bars → 284,175). Also: the
  parallel fetch silently lost **19 days** (Jul/Aug) — always verify day coverage.
- ⭐ **The desk's own bot (`bot.py`) vs the combo, same 2026, same minute bars,
  251 trading days:** bot 2702 trades / 33.9% / +49.0R / **−55R max DD** (2 of 4
  quarters negative, Q4 −15R) vs combo 146 trades / 43.8% / +33.2R / **−9.2R DD**
  (all 4 quarters green). On $200 at desk risk the bot drew down **−87%** (hit
  $74) while the combo drew down only −22%. **Verdict: the bot is a raw
  quantity machine (no ADX/ATR/MACD/RSI filters) — higher total R but far too
  risky on a small account; the combo is the safe, stable one.** Simulator:
  `backtest/bot2026.py`. Report: `bot-vs-combo.html`.

## MarketDesk — the combo filters + the look-ahead that flattered them (2026-11-07)

- ⭐⭐ **A filter that reads the forming bar is reading the future.** The combo
  gate first backtested at **+58R** on 2026 — too good. The filter was reading
  the **current 15m bar**, whose close repaints as it fills; the live desk did the
  same (`bars()` returns the forming bar). Fix: `bot.closed(b, tf)` drops the bar
  whose `now - openTime < frame seconds`; `filter_status`/`frame_agrees` and the
  backtest (`_at(t)-1`) read the **last CLOSED bar**. Honest number: **+25R**. Any
  surface that gates on an indicator must call `closed()`; a number too good to be
  true is a bug to find, not a result to ship.
- ⭐ **The filters buy SAFETY, not total profit — and they do NOT port between
  systems.** On the bot's own 6-frame replay (2026, closed bars): raw **2686 trades
  +86R but −51R drawdown** (hit $119 of $200 = −40%, a losing quarter); **ADX+ATR
  → 793 trades +71R, −19R drawdown, zero losing quarters** ($/trade 2.8×). Adding
  **MACD** (+68R/−25R) or **RSI** (**+13R!**) made it worse — the 6-frame agreement
  is *already* a momentum filter, so a second one only cuts paying trades. The full
  4-gate futures combo → 193 trades, +32R. **Default ships `adx,atr`**
  (`DESK_FILTER_GATES`, `DESK_FILTERS=0` off); MACD/RSI stay available but off.
  *A filter validated on one system must be re-measured on the next.*
- **`plan()` is the one engine.** The gate lives inside `plan()` (returns None when
  blocked), so the button, the alerts and auto-trade share it; `_verdict` carries a
  `blocked` string (`_filter_line`) naming the gate that held the trade back, shown
  by every card. Both `--selftest` and the live cards stay consistent.
- Baselines now: **44 tests** (was 34), **ruff 16**, **mypy 3**. New indicators'
  `float | None` arithmetic trips mypy — split into two locals and guard None.

## MarketDesk — البوت المطوّر + نوافذ المالك (2026-11-07)

- ⭐ **"خلي البوت يحكي كلشي قلتو" = طبقة عرض، مو محرّك جديد.** طلب المالك =
  البوت يعطي نفس التحليل (مستويات/سيناريوهات/إيمت ولماذا). الحل: `card_map`
  (مناطق بيع/شراء + سيولة) + `card_news` (معلومة فقط) + سيناريو عرضي بـ
  `card_scenarios` (ADX<20)، مربوطين بالـMENU/dispatch. **الأخبار ما تدخل الصفقة
  أبداً** — `NewsTest` يثبت `plan()` نفس الشي قبل/بعد الأخبار؛ ما في داتا أخبار
  تاريخية، فقاعدة خبريّة ما بتنختبر → معلومة، مو إشارة.
- ⭐ **نافذة المالك ($8-12 وقف / $15-20 هدف) بتحتاج حصر الطرفين.** `_sized`: احصر
  الهدف بين TARGET_MIN/MAX، بعدين أرضِه على `risk × MIN_RR`. TARGET_RR=2.2.
  ⚠️ خطر $12×1.5=$18 ≤ $20، فالترتيب يضمن R:R≥1.5 دايماً. ($12 وقف لوحده كان
  بيطلع هدف $26.4 — خارج النافذة.)
- ⭐ **داتا السنة: `backtest/duka_2026.json`** (251 يوم، 6 فريمات، 56841 شمعة 5د،
  2026-01-01..11-08). `bot2026.load()` → series جاهزة؛ `fullgrid.build_signals()`
  مرة وحدة ثم `simulate(b5,sig,stop,tgt)` لأي إعداد (apple-to-apple).
  `duka_2026_min.json` شكله `[epoch,o,h,l,c]` — ما ينفع مع الloader.
- ⭐ **النتيجة السنوية (تكلفة $0.5/صفقة):** $9/$18 → +26.9R · **$9/$20 → +50.3R،
  تراجع −17.1R، صفر أرباع سالبة** · $10/$20 → +43.8R · $12/$20 → +28.5R ·
  $8/$16 → +6.4R · $9/$15 → +12.3R. → **$9/$20 هو الأفضل** (هدف $20 يتفوّق على
  $18 رغم صفقات أقل 743 مقابل 793 — الجودة بتغلب الكمية).
- **تجربة أسبوع (2026-11-01..11-08، شمعة بشمعة، صفقة وحدة):** $9/$20 = 10 صفقات
  4ر/6خ = **+$26** · $9/$18 = نفس الإشارات = **+$18**. التقارير:
  `week-trial.html`, `bot-upgraded.html`, `market-analysis.html` بـ`/workspace/project`.
- Baselines: **51 tests** (كانوا 44)، ruff 16، mypy 3 — **صفر خطأ جديد مني**.

## ⚠️ Render: مفتاح الـAPI موقوف، والبوت الحي على حساب تاني (2026-11-07)

- **`$Render_api_key` = حساب فاضي/موقوف** (`services: []`, `402 billing_suspended`).
  ما فيني أنشئ/أعدّل خدمة منو.
- **البوت حي:** `marketdesk-1o4g.onrender.com/health` → 200. يعني الخدمة الحية
  على **حساب Render تاني**.
- ⭐ **autoDeploy ما عم يوصل** — الخدمة الحية ما عملت redeploy من 2026-11-09
  18:11 UTC، ولا commit `cb787ab` ولا `d937d24` وصلها. ما في webhook/commit-status
  من Render. **النشر بدو تدخّل المالك من لوحة Render.**
- ⭐ **درس:** الـ200 على `/health` **ما بيعني إنو الكود الجديد وصل**. تأكد إنو
  `uptime_s` رجع لصفر بعد الـpush؛ uptime عالي = ما صار نشر.
