#!/usr/bin/env bash
# Deploy the MarketDesk bot to Fly.io as an always-on machine.
# Needs FLY_API_TOKEN (https://fly.io/user/personal_access_tokens) and the bot
# token in ../.tg_token (or $TG_TOKEN).
set -euo pipefail
cd "$(dirname "$0")"
export FLYCTL_INSTALL="/home/openhands/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"

: "${FLY_API_TOKEN:?set FLY_API_TOKEN first}"
BOT_TOKEN="${TG_TOKEN:-$(cat ../.tg_token 2>/dev/null || true)}"
: "${BOT_TOKEN:?no bot token found in ../.tg_token}"

APP="$(sed -nE 's/^app = "(.*)"/\1/p' fly.toml | head -1)"
REGION="$(sed -nE 's/^primary_region = "(.*)"/\1/p' fly.toml | head -1)"
echo "app=$APP region=$REGION"

# The name must be unique across all of Fly; fall back to a suffix when taken.
if ! flyctl apps create "$APP" --org personal >/dev/null 2>&1; then
  if ! flyctl apps list 2>/dev/null | awk '{print $1}' | grep -qx "$APP"; then
    APP="${APP}-$(head -c2 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    sed -i "s/^app = .*/app = \"$APP\"/" fly.toml
    echo "name taken; using $APP"
    flyctl apps create "$APP" --org personal
  fi
fi

flyctl volumes list --app "$APP" 2>/dev/null | grep -q marketdesk_data || \
  flyctl volumes create marketdesk_data --app "$APP" --region "$REGION" --size 1 --yes

flyctl secrets set --app "$APP" --stage \
  TG_TOKEN="$BOT_TOKEN" \
  TELEGRAM_BOT_TOKEN="$BOT_TOKEN" \
  TELEGRAM_CHAT_ID="933571066" \
  DESK_SYMBOL="XAUUSD"

flyctl deploy --app "$APP" --ha=false
flyctl status --app "$APP"
echo "== logs (last 40) =="
flyctl logs --app "$APP" --no-tail | tail -40 || true
