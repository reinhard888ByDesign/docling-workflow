#!/usr/bin/env python3
"""
Wilson Heartbeat — Service Monitor für Ryzen-Dispatcher-Stack.
Pollt alle POLL_INTERVAL Sekunden die Ryzen-Dienste und sendet bei
2 aufeinanderfolgenden Fehlern eine Telegram-Warnung. Erholt sich
ein Service, kommt eine Recovery-Meldung. Täglich um 08:00 ein
kurzer OK-Report (oder Liste der aktuell defekten Services).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests

# ── Konfiguration ──────────────────────────────────────────────────────────────

DISPATCHER_URL = os.environ.get("DISPATCHER_URL", "http://192.168.86.195:8765")
CACHE_URL      = os.environ.get("CACHE_URL",      "http://192.168.86.195:8765/api/proxy/cache-reader")
DOCLING_URL    = os.environ.get("DOCLING_URL",     "http://192.168.86.195:8765/api/proxy/docling")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",      "http://192.168.86.195:11434")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "8620231031")

POLL_INTERVAL   = int(os.environ.get("HEARTBEAT_INTERVAL",     "90"))   # Sekunden
ALERT_THRESHOLD = int(os.environ.get("HEARTBEAT_THRESHOLD",    "2"))    # Fehler bis Alert
CHECK_TIMEOUT   = int(os.environ.get("HEARTBEAT_TIMEOUT",      "8"))    # Sekunden pro Request
DAILY_HOUR      = int(os.environ.get("HEARTBEAT_DAILY_HOUR",   "8"))    # Stunde für Tages-Report

STATE_FILE = Path.home() / ".openclaw" / "heartbeat_state.json"

# ── Service-Definitionen ───────────────────────────────────────────────────────

SERVICES = {
    "dispatcher": {
        "label":  "📬 Dispatcher",
        "url":    lambda: f"{DISPATCHER_URL}/api/queue/state",
        "check":  lambda r: r.status_code == 200 and "waiting" in r.json(),
    },
    "cache_reader": {
        "label":  "🔍 Cache-Reader",
        "url":    lambda: CACHE_URL,
        "check":  lambda r: r.status_code == 200 and "total_documents" in r.json(),
    },
    "docling": {
        "label":  "📄 Docling-Serve",
        "url":    lambda: DOCLING_URL,
        "check":  lambda r: r.status_code == 200 and r.json().get("status") == "ok",
    },
    "ollama": {
        "label":  "🤖 Ollama",
        "url":    lambda: f"{OLLAMA_URL}/api/tags",
        "check":  lambda r: r.status_code == 200 and "models" in r.json(),
    },
}

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("heartbeat")

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram nicht konfiguriert — Nachricht unterdrückt")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram-Fehler: {e}")


def probe_service(svc_id: str) -> tuple[bool, str]:
    """Gibt (ok, detail) zurück."""
    svc = SERVICES[svc_id]
    url = svc["url"]()
    try:
        r = requests.get(url, timeout=CHECK_TIMEOUT)
        if svc["check"](r):
            return True, "ok"
        return False, f"HTTP {r.status_code} / ungültige Antwort"
    except requests.exceptions.ConnectionError:
        return False, "Verbindung abgelehnt"
    except requests.exceptions.Timeout:
        return False, f"Timeout nach {CHECK_TIMEOUT}s"
    except Exception as e:
        return False, str(e)


# ── Kern-Loop ──────────────────────────────────────────────────────────────────

def check_all(state: dict) -> dict:
    now = datetime.now().isoformat(timespec="seconds")

    for svc_id, svc in SERVICES.items():
        entry = state.setdefault(svc_id, {
            "failures":    0,
            "alerted":     False,
            "last_ok":     None,
            "last_fail":   None,
        })

        ok, detail = probe_service(svc_id)

        if ok:
            if entry["alerted"]:
                # Recovery
                down_since = entry.get("last_ok") or "unbekannt"
                send_telegram(
                    f"✅ <b>{svc['label']} ist wieder erreichbar</b>\n"
                    f"Zuletzt OK: {down_since}"
                )
                log.info(f"{svc_id}: RECOVERED")
            entry["failures"] = 0
            entry["alerted"]  = False
            entry["last_ok"]  = now
        else:
            entry["failures"] += 1
            entry["last_fail"] = now
            log.warning(f"{svc_id}: FAIL #{entry['failures']} — {detail}")

            if entry["failures"] >= ALERT_THRESHOLD and not entry["alerted"]:
                send_telegram(
                    f"🚨 <b>{svc['label']} nicht erreichbar</b>\n"
                    f"Fehler: {detail}\n"
                    f"{entry['failures']}× in Folge — Wilson-Heartbeat"
                )
                entry["alerted"] = True
                log.warning(f"{svc_id}: ALERT gesendet")

    return state


def should_send_daily(state: dict) -> bool:
    today = str(date.today())
    if state.get("_daily_sent") == today:
        return False
    if datetime.now().hour != DAILY_HOUR:
        return False
    return True


def send_daily_report(state: dict) -> None:
    failing = [
        svc["label"]
        for svc_id, svc in SERVICES.items()
        if state.get(svc_id, {}).get("failures", 0) > 0
    ]
    if failing:
        lines = "\n".join(f"  ⚠️ {s}" for s in failing)
        msg = f"🌅 <b>Heartbeat Tages-Report</b>\nProbleme:\n{lines}"
    else:
        msg = "🌅 <b>Heartbeat Tages-Report</b>\nAlle Services ✅ erreichbar"
    send_telegram(msg)
    state["_daily_sent"] = str(date.today())
    log.info("Tages-Report gesendet")


def main() -> None:
    log.info(f"Heartbeat gestartet — Interval {POLL_INTERVAL}s, Threshold {ALERT_THRESHOLD}")
    send_telegram("🟢 <b>Wilson Heartbeat gestartet</b>\nÜberwache: Dispatcher · Cache-Reader · Docling · Ollama")

    state = load_state()

    while True:
        try:
            state = check_all(state)
            if should_send_daily(state):
                send_daily_report(state)
            save_state(state)
        except Exception as e:
            log.error(f"Unerwarteter Fehler im Check-Loop: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
