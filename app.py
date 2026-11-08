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
SELF_PING_S = 600   # 10 minutes, inside the 15-minute idle window
FIRST_PING_S = 60   # ping soon after boot, then settle into the 10-minute beat
RESTORE_WAIT_S = 60  # never let a slow Telegram delay the bot for longer than this


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
            # Liveness is the whole point of this endpoint, so it must answer
            # even while the database is busy: a health check that can block is
            # a health check that can fail the deploy.
            try:
                trades = len(desk.open_trades())
            except Exception:  # noqa: BLE001
                trades = -1
            self._send(200, {"status": "ok",
                             "uptime_s": int(time.time() - STARTED),
                             "open_trades": trades})
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
    delay = FIRST_PING_S
    while not stop.is_set():
        # Wait first: the server has not bound the port yet at startup, and
        # Render's own health check already covers the first minutes.
        if stop.wait(delay):
            break
        delay = SELF_PING_S
        try:
            with urllib.request.urlopen(target, timeout=30) as r:
                r.read()
        except Exception as exc:  # noqa: BLE001 - a failed ping must not stop the loop
            print("self-ping failed:", exc, flush=True)


def _restore(tg) -> None:
    n = desk.restore_journal(tg)
    print(f"restored {n} trades from the Telegram backup", flush=True)


def _supervise(name: str, fn, args: tuple, stop: threading.Event) -> None:
    """Run a loop and restart it if it ever dies.

    A long-polling bot on a host that gives no shell must heal itself: a thread
    that exits silently turns "the bot ignored me" into a mystery. Every loop
    here is written to keep running, and this is the backstop for the day one
    is not.
    """
    while not stop.is_set():
        try:
            fn(*args)
            print(f"{name} returned; restarting in 5s", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{name} crashed: {exc!r}; restarting in 5s", flush=True)
        stop.wait(5)


def main() -> int:
    desk._init_db()
    tok = desk.token()
    if not tok:
        print("no token: set the TG_TOKEN env var", file=sys.stderr)
        return 2
    stop = threading.Event()
    port = int(os.environ.get("PORT", "10000"))

    # Bind and serve FIRST. Render scans for a listening port and then polls
    # /health with a deadline; anything slow before this point (a network call,
    # a stuck restore) reads as "no port bound" and the deploy is restarted.
    # Everything below this line may take as long as it likes.
    server = ThreadingHTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"marketdesk up: health on :{port}/health", flush=True)

    tg = desk.Telegram(tok)
    # Restore in its own thread with a bounded wait, so a slow Telegram can
    # delay the journal but never the bot.
    restore = threading.Thread(target=_restore, args=(tg,), daemon=True)
    restore.start()
    restore.join(timeout=RESTORE_WAIT_S)
    if restore.is_alive():
        print("restore is slow; starting the bot anyway", flush=True)

    allow = desk.load_allow()
    for name, fn, args in (
            ("poller", desk.poll_forever, (tg, stop)),
            ("monitor", desk.monitor_loop, (tg, allow, 2, stop)),
            ("backup", desk.backup_loop, (tg, 60, stop)),
            ("self-ping", self_ping_loop, (stop,))):
        threading.Thread(target=_supervise, args=(name, fn, args, stop),
                         daemon=True).start()
    print("marketdesk: polling @Ayzr1324_bot", flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
