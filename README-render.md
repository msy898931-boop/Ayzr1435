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
| `app.py` | The Render web service: serves `/health` on `$PORT`, runs `bot.py`'s poller and monitor in threads |
| `bot.py` | The bot itself (unchanged) |
| `render.yaml` | Blueprint: one free web service, `healthCheckPath: /health`, `TG_TOKEN` from the dashboard |
| `keepalive.py` | Pings `/health` every 14 min (for a VPS or a cron host) |
| `.github/workflows/keepalive.yml` | Free keep-alive: pings `/health` every 5 min |

## Deploy

1. Push this repository to GitHub (`main`).
2. Render Dashboard → **New → Blueprint** → pick the repo. Render reads
   `render.yaml` and creates one free web service.
3. When asked for `TG_TOKEN`, paste the bot token. It is stored as a secret and
   never enters git.
4. Wait for the first deploy, then open `https://<service>.onrender.com/health`.
   It must answer `{"status":"ok", ...}`.
5. Set the keep-alive target: repository **Settings → Variables → Actions** →
   `RENDER_SERVICE_URL = https://<service>.onrender.com`.

## Keep-alive: GitHub Actions or an uptime monitor

The workflow pings every 5 minutes, inside the 15-minute window. Two caveats:

- GitHub disables scheduled workflows after 60 days of repository inactivity,
  and a scheduled run can be delayed a few minutes under load.
- If the bot must never miss a tap, use an uptime monitor (UptimeRobot,
  cron-job.org) instead. It also alerts you when the service is down, which a
  ping from inside can never do.

## What is not durable

The free plan has **no persistent disk**, so `data/journal.db`, `data/allow.json`
and `data/offset.json` live on an ephemeral filesystem: a redeploy or a spin-down
resets them. The bot survives a restart (the offset is re-fetched from Telegram),
but the trade journal does not.

Two ways to fix it, in order of preference:

1. **A paid instance with a persistent disk** mounted at `/app/data` — one
   line in `render.yaml`, and the journal is truly durable.
2. **Back the journal up to Telegram** — the bot already talks to Telegram and
   a pinned message is free and permanent. Not implemented here; it is the
   next step if the journal must outlive a redeploy on the free plan.
