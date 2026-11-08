#!/usr/bin/env python3
"""MarketDesk on Render -- a free web service that is really the bot.

Why this shape: Render gives a workspace 750 free instance-hours a month and
**suspends every free service** the moment they run out. The rule that makes the
quota last is therefore: exactly ONE free service, and keep it from spinning
down. A free web service spins down after 15 minutes without inbound traffic,
and a spun-down bot answers nothing, so /health exists to be pinged.

This file does three things:
  1. serves HTTP on $PORT with /health -- a Render *web service* must bind a
     port, and it is the only free type that can both hold a long-polling
     process and answer a health check;
  2. runs the real bot (bot.py) in background threads -- the poller and the
     trade monitor;
  3. never exits, so Render has no reason to restart it.

It does NOT fake durability: the free plan has no persistent disk, so
data/journal.db is lost on a redeploy or a spin-down. That is a separate,
deliberate step -- see README.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as desk  # noqa: E402

STARTED = time.time()


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


def main() -> int:
    desk._init_db()
    tok = desk.token()
    if not tok:
        print("no token: set the TG_TOKEN env var", file=sys.stderr)
        return 2
    tg = desk.Telegram(tok)
    stop = threading.Event()
    threading.Thread(target=desk.monitor_loop,
                     args=(tg, desk.load_allow(), 2, stop), daemon=True).start()
    threading.Thread(target=desk.poll_forever, args=(tg, stop), daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    print(f"marketdesk up: polling, health on :{port}/health", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Health).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
