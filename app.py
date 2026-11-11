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
SELF_PING_S = 480   # 8 minutes: two beats fit inside Render's 15-minute idle window
FIRST_PING_S = 60   # ping soon after boot, then settle into the 8-minute beat
PING_RETRY_S = 45   # a failed beat is retried quickly, not after a full interval
PING_TRIES = 3
RESTORE_WAIT_S = 60  # never let a slow Telegram delay the bot for longer than this
MAP_CACHE_S = 15     # the live map is a heavy read; serve it from a short cache
PRICE_CACHE_S = 2    # the price is one light call; let it feel near-instant
MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.html")

# The live map: /map serves the page, /map.json serves the numbers. The numbers
# come from bot.map_payload() -- the SAME functions the Telegram cards call --
# so the page can never disagree with the bot. Cached for MAP_CACHE_S seconds
# because map_payload() reads six frames plus the news feed; without the cache
# a page open on a phone would hammer the free instance every few seconds.
_MAP_CACHE: dict = {"at": 0.0, "body": b""}
_MAP_LOCK = threading.Lock()
_PRICE_CACHE: dict = {"at": 0.0, "body": b""}
_PRICE_LOCK = threading.Lock()


def price_json() -> bytes:
    """The light, fast price endpoint. Same shape as map_json but one call."""
    now = time.monotonic()
    with _PRICE_LOCK:
        if _PRICE_CACHE["body"] and now - _PRICE_CACHE["at"] < PRICE_CACHE_S:
            return _PRICE_CACHE["body"]
    try:
        body = json.dumps(desk.price_tick(), ensure_ascii=False).encode()
    except Exception as exc:  # noqa: BLE001 - a failed read must not 500 the page
        print(f"price tick failed: {exc}", flush=True)
        return json.dumps({"error": "read failed"}).encode()
    with _PRICE_LOCK:
        _PRICE_CACHE["at"], _PRICE_CACHE["body"] = now, body
    return body


def map_json() -> bytes:
    now = time.monotonic()
    with _MAP_LOCK:
        if _MAP_CACHE["body"] and now - _MAP_CACHE["at"] < MAP_CACHE_S:
            return _MAP_CACHE["body"]
    # Build OUTSIDE the lock: a slow read must not block a second caller, and
    # two readers racing simply produce the same payload.
    try:
        body = json.dumps(desk.map_payload(), ensure_ascii=False).encode()
    except Exception as exc:  # noqa: BLE001 - a failed read must not 500 the page
        print(f"map payload failed: {exc}", flush=True)
        return json.dumps({"error": "read failed"}).encode()
    with _MAP_LOCK:
        _MAP_CACHE["at"], _MAP_CACHE["body"] = now, body
    return body


class Health(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_raw(self, code: int, ctype: str, raw: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/map.json":
            self._send_raw(200, "application/json; charset=utf-8", map_json())
            return
        if path == "/price":
            self._send_raw(200, "application/json; charset=utf-8", price_json())
            return
        if path == "/map":
            try:
                with open(MAP_PATH, "rb") as fh:
                    self._send_raw(200, "text/html; charset=utf-8", fh.read())
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": f"map page missing: {exc}"})
            return
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
                             "open_trades": trades,
                             # Render injects the running commit; the level
                             # count is a second fingerprint (the owner's two
                             # levels made it 11, it was 9 before).
                             "commit": (os.environ.get("RENDER_GIT_COMMIT")
                                        or "unknown")[:7],
                             "levels": len(desk.WATCH_LEVELS)})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args) -> None:  # keep Render's log readable
        pass


def self_ping_loop(stop: threading.Event) -> None:
    """Keep this service warm from the inside.

    Render spins a free web service down after 15 minutes with no *inbound*
    traffic. An outbound request to the service's own public URL is inbound by
    the time it arrives, so pinging ourselves keeps it awake -- with no second
    host, no GitHub cron, and no uptime-monitor account.

    It cannot report an outage (a dead service cannot ping itself); it exists
    only so the bot is always there to answer a tap.

    A missed beat is the one thing that can let the service sleep, so a failed
    ping is retried at once rather than after a whole interval: one lost beat
    must not open a gap wider than Render's idle window.
    """
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    while not url and not stop.is_set():
        print("self-ping off: RENDER_EXTERNAL_URL is not set", flush=True)
        if stop.wait(3600):
            return
    target = url + "/health"
    print(f"self-ping on: {target} every {SELF_PING_S}s", flush=True)
    delay = FIRST_PING_S
    while not stop.is_set():
        # Wait first: the server has not bound the port yet at startup, and
        # Render's own health check already covers the first minutes.
        if stop.wait(delay):
            break
        for attempt in range(PING_TRIES):
            try:
                with urllib.request.urlopen(target, timeout=30) as r:
                    r.read()
                break
            except Exception as exc:  # noqa: BLE001 - a failed ping must not stop the loop
                print(f"self-ping failed ({attempt + 1}/{PING_TRIES}): {exc}", flush=True)
                if attempt + 1 < PING_TRIES and stop.wait(PING_RETRY_S):
                    return
        delay = SELF_PING_S


def _restore(tg) -> None:
    n = desk.restore_journal(tg)
    print(f"restored {n} trades from the Telegram backup", flush=True)


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

    auto = "off" if desk.desk_is_silenced() else "on"
    # The desk, the backup and the poller all come from bot.py's one start_desk,
    # so this wrapper cannot run a different bot than a VPS would: the only
    # difference is `supervise=True` (this host gives no shell) and the self-ping.
    desk.start_desk(tg, stop, supervise=True)
    threading.Thread(target=desk._supervise,
                     args=("poller", desk.poll_forever, (tg, stop), stop),
                     daemon=True).start()
    threading.Thread(target=desk._supervise,
                     args=("self-ping", self_ping_loop, (stop,), stop),
                     daemon=True).start()
    print(f"marketdesk: polling @Ayzr1324_bot (auto-trade {auto})", flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
