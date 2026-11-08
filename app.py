#!/usr/bin/env python3
"""MarketDesk on Render -- a free web service that is really the bot.

Why this shape: Render gives a workspace 750 free instance-hours a month and
**suspends every free service** the moment they run out. The rule that makes the
quota last is therefore: exactly ONE free service, and keep it from spinning
down. A free web service spins down after 15 minutes without inbound traffic,
and a spun-down bot answers nothing, so /health exists to be pinged -- including
by this process itself, every 10 minutes.

This file does three things:
  1. serves HTTP on $PORT with /health -- a Render *web service* must bind a
     port, and it is the only free type that can both hold a long-polling
     process and answer a health check;
  2. runs the real bot (bot.py) in background threads -- the poller, the trade
     monitor, the journal backup, and the self-ping;
  3. never exits, so Render has no reason to restart it.

The free plan has no persistent disk, so the journal is not kept on the
filesystem: bot.py uploads it to a pinned Telegram message and restores from
that pin at startup. Telegram is the durable store here.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as desk  # noqa: E402

STARTED = time.time()
SELF_PING_S = 600  # 10 minutes, inside the 15-minute idle window


class Health(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health"):
            self._send(200, {"status": "ok",
                             "uptime_s": int(time.time() - STARTED),
                             "open_trades": len(desk.open_trades())})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args) -> None:  # keep Render's log readable
        pass


def self_ping_loop(stop: threading.Event) -> None:
    """Keep this service warm from the inside.

    Render spins a free web service down after 15 minutes with no *inbound*
    traffic. An outbound request to the service's own public URL is inbound by
    the time it arrives, so pinging ourselves every 10 minutes keeps it awake --
    with no second host, no GitHub cron, and no uptime-monitor account.

    It cannot report an outage (a dead service cannot ping itself); it exists
    only so the bot is always there to answer a tap.
    """
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        print("self-ping off: RENDER_EXTERNAL_URL is not set", flush=True)
        return
    target = url + "/health"
    print(f"self-ping on: {target} every {SELF_PING_S}s", flush=True)
    while not stop.is_set():
        # Wait first: the server has not bound the port yet at startup, and
        # Render's own health check already covers the first minutes.
        if stop.wait(SELF_PING_S):
            break
        try:
            with urllib.request.urlopen(target, timeout=30) as r:
                r.read()
        except Exception as exc:  # noqa: BLE001 - a failed ping must not stop the loop
            print("self-ping failed:", exc, flush=True)


def main() -> int:
    desk._init_db()
    tok = desk.token()
    if not tok:
        print("no token: set the TG_TOKEN env var", file=sys.stderr)
        return 2
    tg = desk.Telegram(tok)
    restored = desk.restore_journal(tg)
    if restored:
        print(f"restored {restored} trades from the Telegram backup", flush=True)
    stop = threading.Event()
    threading.Thread(target=desk.monitor_loop,
                     args=(tg, desk.load_allow(), 2, stop), daemon=True).start()
    threading.Thread(target=desk.backup_loop, args=(tg, 60, stop), daemon=True).start()
    threading.Thread(target=desk.poll_forever, args=(tg, stop), daemon=True).start()
    threading.Thread(target=self_ping_loop, args=(stop,), daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    print(f"marketdesk up: polling, health on :{port}/health", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Health).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
