#!/usr/bin/env bash
# Run @Ayzr1324_bot anywhere — a VPS, a home server, a Raspberry Pi, WSL.
# Keeps the bot alive: if it ever exits, this restarts it in 5s.
#
#   TG_TOKEN=<your bot token> ./run.sh
#
# Or put the token in .tg_token next to the repo root and just run ./run.sh.
#
# This runs ../bot.py — THE engine, the same file Render runs. It used to run a
# copy of bot.py kept beside this script, which drifted: a fix in the real bot
# never reached the copy. There is no copy now, by design.
set -u
cd "$(dirname "$0")"
BOT="../bot.py"

if [ ! -f "$BOT" ]; then
  echo "ما لقيت المحرك: $BOT — شغّل هالسكربت من داخل مجلد flybot بالريبو الكامل."
  exit 2
fi
if [ -z "${TG_TOKEN:-}" ] && [ ! -s ../.tg_token ]; then
  echo "لا يوجد توكن. ضع التوكن في ../.tg_token أو صدّره:"
  echo "  TG_TOKEN=123:ABC ./run.sh"
  exit 2
fi

command -v python3 >/dev/null || { echo "python3 غير منصّب"; exit 2; }
python3 - <<'PY' || exit 2
import sys
if sys.version_info < (3, 10):
    sys.exit("يلزم Python 3.10 أو أحدث (bot.py يستعمل تلميحات الأنواع الجديدة)")
PY

echo "▶️  تشغيل @Ayzr1324_bot … (Ctrl+C للإيقاف)"
while true; do
  (cd .. && TG_TOKEN="${TG_TOKEN:-}" python3 bot.py) >> bot.log 2>&1
  echo "[$(date -u +%FT%TZ)] خرج البوت — إعادة التشغيل بعد 5 ثوانٍ" >> bot.log
  sleep 5
done
