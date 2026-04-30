#!/usr/bin/env python3
"""Lärmbär — Home Assistant Telegram Bot auf Wilson (Raspberry Pi)"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import requests

BOT_TOKEN    = os.environ.get("LAERENBAER_BOT_TOKEN", "")
CHAT_ID      = int(os.environ.get("TELEGRAM_CHAT_ID", "8620231031"))
HA_URL       = os.environ.get("HA_URL", "http://192.168.86.183:8123")
HA_TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "/home/reinhard/.config/homeassistant/token")
CAMERA_SCRIPT = "/home/reinhard/Vaults/scripts/ha-camera.sh"
POLL_TIMEOUT  = int(os.environ.get("POLL_TIMEOUT", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SENSOR_GROUPS = {
    "🌤 Wetter": [
        ("sensor.wetterstation_temperatur",       "Temperatur"),
        ("sensor.wetterstation_luftfeuchtigkeit", "Luftfeuchtigkeit"),
        ("sensor.windmesser_windgeschwindigkeit",  "Wind"),
        ("sensor.regenmesser_niederschlagsmenge_heute", "Regen heute"),
        ("sensor.wetterstation_atmospharischer_druck",  "Luftdruck"),
        ("sensor.wetterstation_kohlendioxid",     "CO₂"),
    ],
    "🌡 Räume": [
        ("sensor.terrazza_temperatur",            "Terrazza"),
        ("sensor.terrazza_luftfeuchtigkeit",      "Terrazza Feuchte"),
        ("sensor.windfree_camera_temperatur",     "Windfree Camera"),
        ("sensor.windfree_casetta_temperatur",    "Casetta"),
        ("sensor.windfree_casotto_temperatur",    "Casotto"),
        ("sensor.windfree_studio_temperatur",     "Studio"),
    ],
    "⚡ Energie": [
        ("sensor.fsp_ne_136056047_flow_solar_power",      "Solar"),
        ("sensor.fsp_ne_160305509_charge_discharge_power","Batterie"),
        ("sensor.enelgrid_daily_import",                  "Netz-Import heute"),
        ("sensor.fsp_ne_136056047_consumption_today",     "Verbrauch heute"),
    ],
    "💧 Wasser": [
        ("sensor.cisterna_flussigkeitsfullstand", "Cisterna Füllstand"),
    ],
}

CAMERAS = {
    "cancello":     "camera.cancello",
    "ingresso":     "camera.ingresso",
    "parcheggio":   "camera.parcheggio",
    "viale":        "camera.viale",
    "giardino_nord":"camera.giardino_nord",
    "giardino_ovest":"camera.giardino_ovest",
    "terrazza":     "camera.terrazza",
    "sudseite":     "camera.sudseite",
    # Deutsch-Aliase
    "tor":          "camera.cancello",
    "eingang":      "camera.ingresso",
    "parkplatz":    "camera.parcheggio",
    "zufahrt":      "camera.viale",
    "nord":         "camera.giardino_nord",
    "ovest":        "camera.giardino_ovest",
    "west":         "camera.giardino_ovest",
    "garten":       "camera.giardino_nord",
    "sued":         "camera.sudseite",
}


# ── Home Assistant ──────────────────────────────────────────────────────────

def _ha_token() -> str:
    return Path(HA_TOKEN_FILE).read_text().strip()

def _ha_headers() -> dict:
    return {"Authorization": f"Bearer {_ha_token()}"}

def ha_get(entity_id: str) -> dict | None:
    try:
        r = requests.get(f"{HA_URL}/api/states/{entity_id}",
                         headers=_ha_headers(), timeout=8)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"HA get {entity_id}: {e}")
        return None

def ha_call_service(domain: str, service: str, entity_id: str) -> bool:
    try:
        r = requests.post(
            f"{HA_URL}/api/services/{domain}/{service}",
            headers={**_ha_headers(), "Content-Type": "application/json"},
            json={"entity_id": entity_id},
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log.warning(f"HA {domain}.{service} {entity_id}: {e}")
        return False

def _fmt(state: dict) -> str:
    val  = state.get("state", "?")
    unit = state.get("attributes", {}).get("unit_of_measurement", "")
    return "—" if val == "unavailable" else (f"{val} {unit}".strip())


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_sensoren() -> str:
    lines = ["📊 <b>Sensoren</b>"]
    for group, sensors in SENSOR_GROUPS.items():
        rows = []
        for eid, label in sensors:
            s = ha_get(eid)
            if s:
                rows.append(f"  {label}: {_fmt(s)}")
        if rows:
            lines.append(f"\n{group}")
            lines.extend(rows)
    return "\n".join(lines)

def cmd_sensor(name: str) -> str:
    s = ha_get(name)
    if not s:
        for sensors in SENSOR_GROUPS.values():
            for eid, label in sensors:
                if name.lower() in eid.lower() or name.lower() in label.lower():
                    s = ha_get(eid)
                    name = eid
                    break
            if s:
                break
    if not s:
        return f"Sensor nicht gefunden: {name}"
    attrs = s.get("attributes", {})
    return f"<b>{attrs.get('friendly_name', name)}</b>\nWert: {_fmt(s)}"

def cmd_kamera(name: str) -> str:
    entity = CAMERAS.get(name.lower())
    if not entity:
        for key, val in CAMERAS.items():
            if name.lower() in key or name.lower() in val:
                entity = val
                break
    if not entity:
        avail = ", ".join(sorted({v.replace("camera.", "") for v in CAMERAS.values()}))
        return f"Kamera nicht gefunden: {name}\nVerfügbar: {avail}"
    try:
        res = subprocess.run(["bash", CAMERA_SCRIPT, entity],
                             capture_output=True, text=True, timeout=30)
        out = res.stdout.strip()
        return out if out else f"Kein Bild für {entity}"
    except Exception as e:
        return f"Fehler: {e}"

def cmd_kameras() -> str:
    unique = sorted({v for v in CAMERAS.values()})
    lines = ["📷 <b>Verfügbare Kameras</b>\n"]
    for e in unique:
        short = e.replace("camera.", "")
        aliases = [k for k, v in CAMERAS.items() if v == e and k != short]
        line = f"• <code>{short}</code>"
        if aliases:
            line += f"  ({', '.join(aliases)})"
        lines.append(line)
    return "\n".join(lines)

def cmd_schalten(entity_id: str, action: str) -> str:
    if action.lower() in ("an", "on", "ein"):
        service, label = "turn_on", "eingeschaltet"
    elif action.lower() in ("aus", "off"):
        service, label = "turn_off", "ausgeschaltet"
    else:
        return f"Unbekannte Aktion: {action}  (an / aus)"
    domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"
    ok = ha_call_service(domain, service, entity_id)
    return f"✅ {entity_id} {label}" if ok else f"❌ Fehler: {entity_id}"

def cmd_hilfe() -> str:
    return (
        "🐻 <b>Lärmbär — Home Assistant</b>\n\n"
        "/sensoren — Alle Hauptsensoren\n"
        "/sensor &lt;name&gt; — Einzelner Sensor\n"
        "/kamera &lt;name&gt; — Kamera-Snapshot\n"
        "/kameras — Alle Kameras auflisten\n"
        "/schalten &lt;entity&gt; &lt;an|aus&gt; — Gerät schalten\n"
        "/hilfe — Diese Hilfe\n\n"
        "<i>Beispiele:</i>\n"
        "  /kamera cancello\n"
        "  /sensor wetterstation_temperatur\n"
        "  /schalten switch.pumpe_bewasserung an"
    )


# ── Telegram ────────────────────────────────────────────────────────────────

def tg_api(method: str, **kwargs) -> dict:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            params=kwargs, timeout=35,
        )
        return r.json()
    except Exception as e:
        log.error(f"TG {method}: {e}")
        return {}

def tg_send(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        log.error(f"TG sendMessage: {e}")

def tg_send_photo(path: str, caption: str = "") -> None:
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=30,
            )
    except Exception as e:
        log.error(f"TG sendPhoto: {e}")
        tg_send(f"❌ Foto-Fehler: {e}")

def tg_set_commands() -> None:
    cmds = [
        {"command": "sensoren",  "description": "Alle Hauptsensoren"},
        {"command": "sensor",    "description": "Einzelnen Sensor abfragen"},
        {"command": "kamera",    "description": "Kamera-Snapshot"},
        {"command": "kameras",   "description": "Alle Kameras auflisten"},
        {"command": "schalten",  "description": "Gerät ein-/ausschalten"},
        {"command": "hilfe",     "description": "Hilfe anzeigen"},
    ]
    tg_api("setMyCommands", commands=json.dumps(cmds))


# ── Message handler ─────────────────────────────────────────────────────────

def handle(msg: dict) -> None:
    if msg.get("chat", {}).get("id") != CHAT_ID:
        return
    text = msg.get("text", "").strip()
    if not text:
        return

    parts = text.split()
    cmd   = parts[0].lower().lstrip("/").split("@")[0]
    args  = parts[1:]

    if cmd in ("sensoren", "sensors"):
        tg_send("⏳ Frage Sensoren ab…")
        tg_send(cmd_sensoren())

    elif cmd == "sensor":
        tg_send(cmd_sensor(" ".join(args)) if args else "Verwendung: /sensor &lt;name&gt;")

    elif cmd in ("kamera", "camera", "cam"):
        if not args:
            tg_send("Verwendung: /kamera &lt;name&gt;\n\n" + cmd_kameras())
            return
        result = cmd_kamera(args[0])
        if result.startswith("MEDIA:"):
            tg_send_photo(result[6:].strip(), caption=f"📷 {args[0]}")
        else:
            tg_send(result)

    elif cmd == "kameras":
        tg_send(cmd_kameras())

    elif cmd in ("schalten", "switch"):
        if len(args) < 2:
            tg_send("Verwendung: /schalten &lt;entity_id&gt; &lt;an|aus&gt;")
        else:
            tg_send(cmd_schalten(args[0], args[1]))

    elif cmd in ("hilfe", "help", "start"):
        tg_send(cmd_hilfe())

    else:
        tg_send(f"Unbekannt: /{cmd} — /hilfe für Übersicht")


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        log.error("LAERENBAER_BOT_TOKEN nicht gesetzt — Abbruch")
        return

    log.info("Lärmbär gestartet")
    tg_set_commands()
    tg_send("🐻 Lärmbär online — Home Assistant bereit")

    offset = 0
    while True:
        try:
            data    = tg_api("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
            updates = data.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    handle(msg)
        except Exception as e:
            log.error(f"Poll-Fehler: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
