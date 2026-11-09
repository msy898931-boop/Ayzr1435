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

There is no test suite. The checks that exist, and that must stay green:

```bash
python3 -m py_compile bot.py          # syntax
python3 bot.py --selftest             # token, quote, every frame, every card
python3 -m ruff check bot.py          # baseline: 17 findings, no new ones
python3 -m mypy bot.py                # baseline: 2 findings, no new ones
```

`--selftest` must leave the journal empty. The lint counts are baselines, not
targets: a change that adds a finding is a change that added a finding.

## Style

Arabic for everything the owner reads; English for code, comments and commit
messages. One decision engine (`plan()`), many views — never a second copy of
the decision logic on a card.
