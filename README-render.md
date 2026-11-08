# MarketDesk on Render — the quota-safe setup

## The one rule that keeps the quota

Render gives a **workspace** 750 free instance-hours a month, shared by every
free service in it. When the hours run out, Render suspends **all** of them
until the next calendar month. That is what happened to the previous workspace:
11 services, all suspended at once, `Monthly Quota Exceeded`.

So:

1. **Exactly one free service.** Never add a second one to "back it up" — that
   is what trips the cap.
2. **Keep it from spinning down.** A free web service sleeps after 15 minutes
   with no inbound traffic, and a sleeping bot answers nothing. Ping `/health`
   every 5 minutes.
3. **No paid extras.** A persistent disk needs a paid instance; a Render cron
   job is a paid service type. Neither is used here.

Budget: one always-on service ≈ 720 h/month, under the 750 h cap.

## Files

| File | Role |
|---|---|
| `app.py` | The Render web service: serves `/health` on `$PORT`, runs the bot's poller, monitor, backup and self-ping in threads |
| `bot.py` | The bot itself, plus the pinned-Telegram journal backup |
| `render.yaml` | Blueprint: one free web service, `healthCheckPath: /health`, `TG_TOKEN` from the dashboard |
| `keepalive.py` | Standalone pinger every 14 min (for a VPS or a cron host) |
| `.github/workflows/keepalive.yml` | Free keep-alive: pings `/health` every 5 min |

## Deploy

The service is already created on the account (`marketdesk`,
`https://marketdesk-1o4g.onrender.com`), so this is the repeatable path:

1. Push to `main`; auto-deploy rebuilds the service.
2. Set the token once: Render Dashboard → the service → **Environment** →
   `TG_TOKEN`. It is a secret and never enters git.
3. Open `https://<service>.onrender.com/health`; it must answer
   `{"status":"ok", ...}`.
4. If the URL ever changes, set the repository variable
   `RENDER_SERVICE_URL` (Settings → Variables → Actions) — the workflow falls
   back to the known URL otherwise.

Do **not** create a second service from `render.yaml` to "back it up": the
blueprint is kept as documentation, and a second free service is exactly what
trips the quota.

## What is not durable

The free plan has **no persistent disk** (a disk requires a paid instance), so
`data/journal.db` is ephemeral: a redeploy or a spin-down resets it.

That is fixed without paying, by using Telegram as the store:

- `bot.py` uploads the journal as `marketdesk-backup.json` and **pins** it, every
  time the journal file changes (checked once a minute).
- At startup it **restores** from that pin, and only into an empty journal, so a
  restart can never double-count a trade.
- `data/allow.json` is carried in the same file, so the people the bot serves
  survive a restart too.
- `python3 bot.py --backup` does one restore + backup by hand.

The backup chat is the first allowed chat. Override it with `DESK_BACKUP_CHAT`.

A paid instance with a disk mounted at `/app/data` remains the cleaner option if
the journal ever outgrows a Telegram document.

## Keep-alive

Two layers, both free:

1. **`app.py` pings itself** every 10 minutes (`RENDER_EXTERNAL_URL`, which
   Render sets automatically). An outbound request to its own public URL is
   inbound when it arrives, so the service stays awake with no second host.
2. **`.github/workflows/keepalive.yml`** pings `/health` every 5 minutes. It
   survives a change that breaks the self-ping, and a failure shows as a red run
   in the repository.

Neither can tell you the service is down — a dead service cannot ping itself,
and GitHub will not email you about a red scheduled run by default. For that,
point an uptime monitor (UptimeRobot, cron-job.org) at `/health`; it alerts you
and doubles as a third keep-alive. Note GitHub disables scheduled workflows after
60 days of repository inactivity.
