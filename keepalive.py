#!/usr/bin/env python3
"""Ping the Render service so a free web service never spins down on idle.

Render spins a free web service down after 15 minutes without inbound traffic,
and a spun-down bot answers nothing. This pings /health every 14 minutes.

Run it somewhere that is always on -- an external uptime monitor is the honest
choice: a monitor outside Render both keeps the service warm AND notices an
outage, which a self-ping never can. Point UptimeRobot or cron-job.org at

    https://<service>.onrender.com/health

every 5 minutes and this file is not needed. Keep it for a VPS or a Render Cron
Job. URL: $RENDER_EXTERNAL_URL or the first argument.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request

EVERY = int(os.environ.get("KEEPALIVE_SECONDS", "840"))  # 14 minutes


def main() -> int:
    base = os.environ.get("RENDER_EXTERNAL_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not base:
        print("give the service URL: $RENDER_EXTERNAL_URL or argv[1]", file=sys.stderr)
        return 2
    url = base.rstrip("/") + "/health"
    print(f"keep-alive: pinging {url} every {EVERY}s", flush=True)
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                print(time.strftime("%FT%TZ", time.gmtime()), r.status, flush=True)
        except Exception as exc:  # noqa: BLE001 - a failed ping must not stop the loop
            print(time.strftime("%FT%TZ", time.gmtime()), "ping failed:", exc, flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    raise SystemExit(main())
