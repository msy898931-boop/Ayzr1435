#!/usr/bin/env bash
# Keep @Ayzr1324_bot alive. Restarts it if it ever exits, so a crash or a
# reboot does not leave the chat silent. Run with: nohup ./run-bot.sh &
set -u
cd "$(dirname "$0")"
while true; do
    python3 bot.py >> bot.log 2>&1
    echo "[$(date -u +%FT%TZ)] bot exited, restarting in 5s" >> bot.log
    sleep 5
done
