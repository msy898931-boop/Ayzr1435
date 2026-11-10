# AGENTS.md — working in this repo

## What this is

MarketDesk — a single-file Telegram bot (`bot.py`) that gathers, analyses,
decides and recommends on XAUUSD, plus the one free Render web service that
hosts it (`app.py`). The live bot is **@Ayzr1324_bot**.

## Where the memory lives (read this first)

Project memory lives in **`/workspace/project/.openhands/memory/`** — that is
the copy OpenHands injects at session start. It is the source of truth.

A **sanitized mirror** of it is committed here under `.openhands/memory/` so a
workspace reset cannot lose it. The repos are **PUBLIC**, so the mirror must
never contain a secret value or a personal email.

At the end of a session that changed memory:

```bash
cp /workspace/project/.openhands/memory/*.md .openhands/memory/
# redact anything personal before committing -- the repo is public
git add .openhands/ && git commit -m "memory: sync" && git push
```

If the two copies ever disagree, the workspace copy wins; re-mirror it.

## Deploying

Render auto-deploys `main`. After a push, confirm the deploy reached the commit
and the service is healthy:

```bash
curl -s https://marketdesk-1o4g.onrender.com/health
```

## Verifying a change

The checks that exist, and that must stay green:

```bash
python3 -m unittest discover -s tests    # 42 tests, offline, no network
python3 -m py_compile bot.py             # syntax
python3 bot.py --selftest                # token, quote, every frame, every card (needs network)
python3 -m ruff check bot.py             # baseline: 16 findings, no new ones
python3 -m mypy bot.py                   # baseline: 3 findings, no new ones
```

`tests/` stubs `quote`/`bars`, so it runs anywhere and pins the rules that
decide money (sizing window, the plan gate, the 15m filter, grading) plus token
resolution. Run it before every push. `--selftest` must leave the journal empty.
The lint counts are baselines, not targets: a change that adds a finding is a
change that added a finding.

`--selftest` prints the token as `ok (@name)`, `MISSING`, or `DEAD`. Trust only
`ok`: a stale `TELEGRAM_BOT_TOKEN` in the environment is a non-empty string that
404s on every send, and the old truthiness check printed `ok` for it — a
diagnostic that could not fail. `token()` now verifies each candidate with
`getMe` and takes the first that reaches a bot.

## One engine, many entry points

`bot.py` holds the only copy of the decision logic AND the only place the desk's
threads are listed (`start_desk()`). `app.py` (Render), `bot.py` (VPS/Fly) and
`automation/main.py` (the separate scheduled variant) are entry points, not
forks. When adding a background loop, add it to `start_desk()` — never to one
entry point alone. That exact bug shipped once: `bot.py`'s `main()` started only
the trade monitor and never the auto-desk, so the documented "permanent bot for
a VPS" would watch trades and never open one.

## Style

Arabic for everything the owner reads; English for code, comments and commit
messages. One decision engine (`plan()`), many views — never a second copy of
the decision logic on a card.
