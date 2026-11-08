#!/usr/bin/env python3
"""MarketDesk watchdog -- an OUTSIDE check on the Render bot.

The bot is a free Render web service, and a free service spins down after 15
minutes of idle. The bot pings itself and a GitHub cron pings it too, but a
service that pings itself can never notice that it is DOWN, and a GitHub cron
can be delayed or silently dropped. This runs from the automation service,
outside Render, so it both keeps the service warm AND can tell the owner when
the service stops answering.

It speaks only on a state CHANGE (up -> down, or down -> up), so a healthy
service is silent. State lives in the automation KV store, so the alert fires
once per outage, not once per run.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HEALTH = os.environ.get(
    "DESK_HEALTH_URL", "https://marketdesk-1o4g.onrender.com/health")
TG = "https://api.telegram.org"


def load_health_url() -> str:
    """The watched URL, in order: the plugin config, the env override, the
    default. A script runner is handed its config as a file, not an argument."""
    path = os.environ.get("AUTOMATION_CONFIG_PATH", "")
    if path and os.path.exists(path):
        try:
            with open(path) as fh:
                url = (json.load(fh) or {}).get("health_url")
            if url:
                return url
        except (ValueError, OSError):
            pass
    return HEALTH


def get_secret(name):
    """Fetch a named secret: from the agent server when AGENT_SERVER_URL is
    set, else from the OpenHands API (cloud / enterprise)."""
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
    if url:  # local Agent Canvas
        url = f"{url}/api/settings/secrets/{name}"
    else:  # OpenHands Cloud / Enterprise
        api = os.environ["OPENHANDS_CLOUD_API_URL"].rstrip("/")
        url = f"{api}/api/v1/sandboxes/{os.environ['SANDBOX_ID']}/settings/secrets/{name}"
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"X-Session-API-Key": key})) as r:
        return r.read().decode().strip()


def fire_callback(status="COMPLETED", error=None):
    """Signal run completion. Must run on every exit path."""
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}"}))
    except Exception as exc:  # noqa: BLE001
        print(f"Callback error: {exc}")


_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")


def kv_get(key):
    """Read a value from the KV store; None when absent or unavailable."""
    if not (_KV_TOKEN and _KV_BASE):
        return None
    req = urllib.request.Request(f"{_KV_BASE}/v1/kv/{key}",
                                 headers={"Authorization": f"Bearer {_KV_TOKEN}"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())["value"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def kv_set(key, value):
    if not (_KV_TOKEN and _KV_BASE):
        return
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{key}", data=json.dumps(value).encode(), method="PUT",
        headers={"Authorization": f"Bearer {_KV_TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        r.read()


def probe(url, timeout=25):
    """One health probe. Returns (up, detail). A non-200 is DOWN, not an
    error: a 503 is exactly what a suspended free instance answers with."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode()
            if r.status != 200:
                return False, f"HTTP {r.status}"
            try:
                d = json.loads(body)
            except ValueError:
                return True, body[:80]
            return d.get("status") == "ok", json.dumps(d)[:160]
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - a timeout is a DOWN, not a crash
        return False, type(exc).__name__


def telegram(token, chat, text):
    data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"{TG}/bot{token}/sendMessage", data=data,
        headers={"Content-Type": "application/json"}), timeout=25)


def main() -> int:
    token = get_secret("TELEGRAM_BOT_TOKEN")
    chat = get_secret("TELEGRAM_CHAT_ID")
    url = load_health_url()

    up, detail = probe(url)
    prev = kv_get("state") or {}
    fails = int(prev.get("fails", 0))
    alerted = bool(prev.get("alerted", False))

    if up:
        # Only announce a recovery if we had announced the outage, so a single
        # blip stays silent in both directions.
        if alerted:
            telegram(token, chat,
                     "✅ <b>المكتب رجع اشتغل</b>\nالخدمة ردّت على الفحص.")
            print("recovered")
        else:
            print("up (silent)")
        fails, alerted = 0, False
    else:
        fails += 1
        # One miss is normal on a cold start; alert on the second in a row, and
        # only once per outage (alerted stays set until a recovery clears it).
        if fails >= 2 and not alerted:
            telegram(token, chat,
                     "🚨 <b>المكتب واقف!</b>\n"
                     f"الخدمة ما ردّت: <code>{detail}</code>\n"
                     f"الفحص: {url}\n"
                     "<i>جرّب تفتح الرابط؛ إذا ما فتح، الخدمة نايمة.</i>")
            alerted = True
            print(f"down -> alerted ({detail})")
        else:
            print(f"down #{fails} ({detail})")

    kv_set("state", {"up": up, "fails": fails, "alerted": alerted,
                     "detail": str(detail)[:160]})
    return 0


if __name__ == "__main__":
    try:
        rc = main()
        fire_callback("COMPLETED")
        sys.exit(rc)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        fire_callback("FAILED", str(exc))
        sys.exit(1)
