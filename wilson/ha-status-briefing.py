#!/usr/bin/env python3
"""
🏠 HOME ASSISTANT STATUS-BRIEFING
Generiert einen detaillierten HA-Statusbericht mit Raumklima, Solar, Zisterne, Geräten & Alerts.

Aufruf: python3 ha-status-briefing.py [--dry-run]
Output: Telegram-Nachricht an Reinhard
"""

import json, sys, os
from datetime import datetime, date
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

HA_URL = "http://192.168.86.183:8123"
HA_TOKEN_FILE = Path("/home/reinhard/.config/homeassistant/token")
TELEGRAM_BOT = "8621101278:AAHI9CkevPBpZ2uxZQIFyxjGP2m4VUXislE"
TELEGRAM_CHAT = "8620231031"

ENTITY_GROUPS = {
    "temperatures": [
        "sensor.wetterstation_temperatur",
        "sensor.wetterstation_temperatur_2",
        "sensor.terrazza_temperatur",
        "sensor.windfree_camera_temperatur",
        "sensor.windfree_casetta_temperatur",
        "sensor.windfree_studio_temperatur",
        "sensor.elon_temperature_outside",
    ],
    "humidity": [
        "sensor.wetterstation_feuchte",
        "sensor.wetterstation_feuchte_2",
    ],
    "solar": {
        "pv1": "sensor.fsp_ne_160305365_pv_1_input_power",
        "pv2": "sensor.fsp_ne_160305365_pv_2_input_power",
        "pv_today": "sensor.fsp_ne_160305365_pv_1_input_power",  # Approximation
        "consumption": "sensor.fsp_ne_136056047_flow_battery_power",  # Check
        "self_consumption": "sensor.fsp_ne_136056047_self_consumption_ratio_by_pv_production",
        "feed_in": "sensor.fsp_ne_136056047_pv_feed_in_energy",
    },
    "battery": {
        "soc_m1p1": "sensor.fsp_ne_160305509_module_1_battery_pack_1_soc",
        "soc_m1p2": "sensor.fsp_ne_160305509_module_1_battery_pack_2_soc",
        "soc_m2p1": "sensor.fsp_ne_160305509_module_2_battery_pack_1_soc",
        "soc_m2p2": "sensor.fsp_ne_160305509_module_2_battery_pack_2_soc",
        "soc_m2p3": "sensor.fsp_ne_160305509_module_2_battery_pack_3_soc",
        "temp_m1": "sensor.fsp_ne_160305509_module_1_internal_temperature",
        "temp_m2": "sensor.fsp_ne_160305509_module_2_internal_temperature",
    },
    "cisterna": [
        "sensor.cisterna_flussigkeitsfullstand",
        "sensor.cisterna_tiefe",
        "sensor.cisterna_flussigkeitsstand",
        "number.cisterna_alarm_minimum",
    ],
    "devices": [
        "device_tracker.iphone",
        "device_tracker.ipad_home",
        "sensor.iphone_battery_level",
        "sensor.iphone_battery_state",
        "sensor.ipad_home_battery_level",
        "sensor.ipad_home_battery_state",
        "sensor.iphone_marion_battery_level",
    ],
    "tesla": [
        "sensor.tesla_wall_connector_temperatur_des_griffs",
    ],
}

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _ha_token():
    t = HA_TOKEN_FILE.read_text().strip()
    if "=" in t and not t.startswith("eyJ"):
        t = t.split("=", 1)[-1].strip()
    return t

def ha_get(entity_id):
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {_ha_token()}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None

def ha_get_multi(ids):
    """Batch-get multiple entities. Returns dict entity_id -> (state, attributes)."""
    result = {}
    for eid in ids:
        d = ha_get(eid)
        if d:
            result[eid] = (d.get("state"), d.get("attributes", {}))
        else:
            result[eid] = (None, {})
    return result

def send_telegram(text):
    import urllib.request
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"[ERROR] Telegram: {e}", file=sys.stderr)
        return False

def fval(v, unit="", default="N/A"):
    """Format numeric value."""
    if v is None:
        return default
    try:
        f = float(v)
        if unit == "°C" or unit == "%":
            return f"{f:.1f}{unit}"
        elif unit == "kW":
            return f"{f/1000:.1f} kW"
        elif unit == "W":
            return f"{f:.0f} W"
        return f"{f:.0f}{unit}"
    except (ValueError, TypeError):
        return f"{v}{unit}"

# ═══════════════════════════════════════════════════════════════════════════════
# SECTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def raumklima_section():
    """🌡️ Raumklima: Temperaturen und Feuchte."""
    lines = ["🌡️ *Raumklima*", "──────────────────"]

    # Temperaturen
    temp_data = ha_get_multi(ENTITY_GROUPS["temperatures"])
    online = []
    offline = []
    for eid, (state, attrs) in temp_data.items():
        fn = attrs.get("friendly_name", eid.split(".")[-1])
        if state and state not in ("unavailable", "unknown"):
            online.append(f"  • {fn}: {fval(state, '°C')}")
        else:
            offline.append(f"  • {fn}: offline")

    if online:
        lines.extend(online)
    if offline:
        lines.append("")
        lines.append(f"⚠️ *{len(offline)} Sensoren offline:*")
        lines.extend(offline)
        lines.append("_Funkproblem? Basisstation prüfen._")

    if not online and not offline:
        lines.append("  _Keine Temperatursensoren gefunden_")

    lines.append("")
    return lines

def solar_section():
    """☀️ Solar & Energie."""
    lines = ["☀️ *Solar & Energie*", "──────────────────"]

    solar = ha_get_multi(list(ENTITY_GROUPS["solar"].values()))
    bat = ha_get_multi(list(ENTITY_GROUPS["battery"].values()))

    # PV Produktion
    pv1 = None
    pv2 = None
    for eid, (state, _) in solar.items():
        if state and state not in ("unavailable", "unknown"):
            try:
                if "pv_1_input_power" in eid:
                    pv1 = float(state)
                elif "pv_2_input_power" in eid:
                    pv2 = float(state)
            except ValueError:
                pass

    if pv1 and pv2:
        pv_total = pv1 + pv2
        lines.append(f"• Produktion: {fval(pv_total, 'kW')} – {'gut' if pv_total > 3000 else 'mäßig'}")
    elif pv1:
        lines.append(f"• Produktion: {fval(pv1, 'kW')}")

    # Self-consumption
    sc = None
    for eid, (state, _) in solar.items():
        if "self_consumption" in eid and state and state not in ("unavailable", "unknown"):
            try: sc = float(state)
            except: pass
    if sc is not None:
        lines.append(f"• Eigenverbrauch: {fval(sc, '%')} – {'sehr gut' if sc > 60 else 'ok' if sc > 30 else 'niedrig'}")

    # Feed-in
    feed = None
    for eid, (state, _) in solar.items():
        if "feed_in" in eid and state and state not in ("unavailable", "unknown"):
            try: feed = float(state)
            except: pass
    if feed is not None:
        lines.append(f"• Einspeisung heute: {fval(feed, 'kWh')}")

    # Battery
    socs = []
    for eid, (state, _) in bat.items():
        if "soc" in eid and state and state not in ("unavailable", "unknown"):
            try: socs.append(float(state))
            except: pass

    bat_temps = []
    for eid, (state, _) in bat.items():
        if "temperature" in eid and state and state not in ("unavailable", "unknown"):
            try: bat_temps.append(float(state))
            except: pass

    if socs:
        avg_soc = sum(socs) / len(socs)
        min_soc, max_soc = min(socs), max(socs)
        balanced = "balanced" if max_soc - min_soc < 5 else f"Spreizung {max_soc-min_soc:.0f}%"
        lines.append(f"• Batterie: {fval(avg_soc, '%')} (Min {min_soc:.0f}%, Max {max_soc:.0f}% – {balanced})")

    if bat_temps:
        avg_temp = sum(bat_temps) / len(bat_temps)
        status = "warm, normal" if avg_temp < 65 else "HEISS — beobachten!"
        lines.append(f"• Batterie-Temperatur: {avg_temp:.0f}°C – {status}")

    lines.append("")
    return lines

def cisterna_section():
    """💧 Zisterne."""
    lines = ["💧 *Zisterne*", "──────────────────"]

    cis = ha_get_multi(ENTITY_GROUPS["cisterna"])
    fullstand = None
    tiefe = None
    status = None
    alarm_min = None

    for eid, (state, attrs) in cis.items():
        if "fullstand" in eid:
            fullstand = state
        elif "tiefe" in eid:
            tiefe = state
        elif "flussigkeitsstand" in eid:
            status = state
        elif "alarm_minimum" in eid:
            alarm_min = state

    if fullstand:
        f = float(fullstand)
        level_text = "kritisch" if f < 20 else "niedrig" if f < 40 else "ok" if f < 70 else "gut"
        lines.append(f"• Füllstand: {fval(fullstand, '%')} – {level_text}")
    if alarm_min:
        lines.append(f"• Alarmgrenze: {fval(alarm_min, '%')} Minimum")
    if tiefe:
        lines.append(f"• Tiefe: {fval(tiefe, ' mm')}")
    if status:
        lines.append(f"• Status: {status}")

    if fullstand and float(fullstand) < 40:
        lines.append("→ ⚠️ Unter 40% – bei Trockenheit beobachten")

    lines.append("")
    return lines

def batteries_section():
    """🔋 Batteriestatus — alle kritischen Batterien (nicht iPhone/iPad)."""
    lines = ["🔋 *Batteriestatus*", "──────────────────"]

    import urllib.request
    token = _ha_token()
    req = urllib.request.Request(
        f"{HA_URL}/api/states",
        headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            all_states = json.loads(resp.read())
    except Exception:
        lines.append("_HA nicht erreichbar_")
        lines.append("")
        return lines

    # Batterien sammeln (nicht iPhone/iPad/Watch, nicht FSP-Batterie-Packs)
    batteries = []
    for e in all_states:
        eid = e.get("entity_id", "")
        state = e.get("state", "")
        attrs = e.get("attributes", {})
        fn = attrs.get("friendly_name", eid)
        unit = attrs.get("unit_of_measurement", "")
        device_class = attrs.get("device_class", "")

        if unit != "%":
            continue
        # iPhones, iPads, Watches ausschliessen
        if any(x in eid.lower() for x in ("iphone", "ipad", "watch", "macbook", "mac_")):
            continue
        # FSP-Batterien schon im Solar-Teil
        if "fsp_ne" in eid.lower():
            continue
        # Nur battery-Level, nicht SOC von grossen Batterien
        if device_class != "battery" and "battery" not in eid.lower():
            continue

        try:
            level = float(state)
            batteries.append((level, fn, eid))
        except (ValueError, TypeError):
            pass

    batteries.sort()
    if not batteries:
        lines.append("✅ Alle Batterien ok (keine kritischen)")
    else:
        for level, fn, eid in batteries:
            emoji = "🔴" if level < 20 else "🟡" if level < 40 else "🟢"
            short_fn = fn[:40]
            lines.append(f"{emoji} {short_fn}: {level:.0f}%")

    lines.append("")
    return lines

def alerts_section(klima_offline, cisterna_level):
    """⚠️ Alerts: Zusammenfassung aller Auffälligkeiten."""
    lines = ["⚠️ *Alerts*", "──────────────────"]
    alerts = []

    if klima_offline:
        alerts.append(f"❌ {klima_offline} Wettersensoren offline (Funk/Akku prüfen)")

    if cisterna_level and float(cisterna_level) < 40:
        alerts.append(f"⚠️ Zisterne niedrig ({fval(cisterna_level, '%')}, unter 40%-Schwelle)")

    bat_socs = []
    for eid in ENTITY_GROUPS["battery"]:
        if "soc" in eid:
            d = ha_get(eid)
            if d and d.get("state") not in (None, "unavailable", "unknown"):
                try: bat_socs.append(float(d["state"]))
                except: pass

    # Batterie-Temperaturen
    bat_temps = []
    for eid in ENTITY_GROUPS["battery"]:
        if "temperature" in eid and "battery_pack" not in eid:
            d = ha_get(eid)
            if d and d.get("state") not in (None, "unavailable", "unknown"):
                try: bat_temps.append(float(d["state"]))
                except: pass
    if bat_temps and max(bat_temps) > 65:
        alerts.append(f"⚠️ Batterie-Temp {max(bat_temps):.0f}°C – über 65°C!")

    if not alerts:
        lines.append("✅ Keine kritischen Alerts")
    else:
        lines.extend(alerts)

    lines.append("")
    return lines

def handlung_section():
    """➡️ Handlungsbedarf: konkrete Empfehlungen."""
    lines = ["➡️ *Handlungsbedarf:*", ""]

    items = []
    # Netatmo offline?
    netatmo_offline = 0
    for eid in ENTITY_GROUPS["temperatures"]:
        d = ha_get(eid)
        if not d or d.get("state") in ("unavailable", "unknown"):
            netatmo_offline += 1
    if netatmo_offline > 2:
        items.append("1. *Netatmo Wetter* – Basisstation (Soggiorno) prüfen: Strom? Funkbrücke? Alle Sensoren offline.")

    # Zisterne
    cis = ha_get("sensor.cisterna_flussigkeitsfullstand")
    if cis and cis.get("state") not in (None, "unavailable", "unknown"):
        try:
            cl = float(cis["state"])
            if cl < 30:
                items.append("2. *Zisterne* – KRITISCH unter 30%. Regen prüfen, ggf. sparsam bewässern.")
            elif cl < 40:
                items.append("2. *Zisterne* – Unter 40%. Regenprognose checken, bei Trockenheit sparsam gießen.")
        except: pass

    # Batterie
    bat_socs = []
    for eid in ENTITY_GROUPS["battery"]:
        if "soc" in eid:
            d = ha_get(eid)
            if d and d.get("state") not in (None, "unavailable", "unknown"):
                try: bat_socs.append(float(d["state"]))
                except: pass
    if bat_socs:
        avg = sum(bat_socs) / len(bat_socs)
        if avg < 40:
            items.append(f"3. *Batterie* – Nur {avg:.0f}%. Sollte tagsüber laden, abends voll sein.")

    # Kritische Geräte-Batterien (nicht FSP)
    # Wird in batteries_section abgedeckt

    if not items:
        items.append("_Kein akuter Handlungsbedarf_")

    lines.extend(items)
    return lines

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now()
    dry = "--dry-run" in sys.argv

    # Zähle offline Sensoren vorab
    klima_offline = 0
    for eid in ENTITY_GROUPS["temperatures"]:
        d = ha_get(eid)
        if not d or d.get("state") in ("unavailable", "unknown"):
            klima_offline += 1

    cis = ha_get("sensor.cisterna_flussigkeitsfullstand")
    cis_level = cis.get("state") if cis else None

    header = [
        f"🏠 *HOME ASSISTANT STATUS-BRIEFING*",
        f"{now.strftime('%d.%m.%Y %H:%M')}",
        "═══════════════════════════════════════",
        "",
    ]

    klima = raumklima_section()
    solar = solar_section()
    cisterna = cisterna_section()
    batteries = batteries_section()
    alerts = alerts_section(klima_offline, cis_level)
    handlung = handlung_section()

    message = "\n".join(header + klima + solar + cisterna + batteries + alerts + handlung)

    if len(message) > 4000:
        message = message[:4000] + "\n...\n_(gekürzt)_"

    print(message)
    print(f"\n--- {len(message)} Zeichen ---")

    if dry:
        print("🔍 Dry-run — nicht gesendet")
    else:
        if send_telegram(message):
            print("✅ Telegram gesendet")
        else:
            print("❌ Telegram fehlgeschlagen")
            sys.exit(1)

if __name__ == "__main__":
    main()
