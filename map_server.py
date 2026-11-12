#!/usr/bin/env python3
"""Serve the live map page ONLY -- no poller, no desk, no notifications.

`app.py` serves the same page but also starts the bot's threads; running that
here would open a second Telegram long-poll and 409 against the live Render
bot. This wrapper reuses app.py's HTTP handler (so /map, /map.json,
/chart.json, /price, /health are identical) and starts nothing else.

    PORT=12000 python3 map_server.py
"""
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

import app


def main() -> int:
    port = int(os.environ.get("PORT", "12000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), app.Health)
    print(f"map server up: http://0.0.0.0:{port}/map", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
