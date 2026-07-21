#!/usr/bin/env python3
"""
Tages-Briefing — kurz, motivierend, prägnant.
Fokus: Wetter, Tagesenergie (1 Zeile), wichtige Aufgaben, Portfolio, PV/Cisterna.

Abgrenzung zum Feng Shui Briefing (07:50):
  - Tages-Briefing: NUR eine Zeile zur aktuellen Tagesenergie
  - Feng Shui Briefing: Ausführliche Interaktions-Analyse aller Energien

Aufruf: python3 tagesbriefing.py [--dry-run]
"""

import sqlite3, json, sys, os, subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import Counter

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

HA_URL = "http://192.168.86.183:8123"
HA_TOKEN_FILE = Path("/home/reinhard/.config/homeassistant/token")
PORTFOLIO_DB = Path("/home/reinhard/Vaults/portfolio.db")
AUFGABEN_MD = Path("/home/reinhard/Vaults/Aufgaben.md.bak-20260703")
GUA_CACHE = Path("/home/reinhard/Vaults/.openclaw/agents/fengshui-gua-calculator/calculations.json")
GUA_CALC_DIR = Path("/home/reinhard/gua-energy-calculator")

TELEGRAM_BOT = "8621101278:AAHI9CkevPBpZ2uxZQIFyxjGP2m4VUXislE"
TELEGRAM_CHAT = "8620231031"

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
           "Juli", "August", "September", "Oktober", "November", "Dezember"]

GUA_NAMES = {
    1: "Wasser", 2: "Große Erde", 3: "Großes Holz", 4: "Kleines Holz",
    5: "Mittlere Erde", 6: "Großes Metall", 7: "Kleines Metall",
    8: "Kleine Erde", 9: "Kleines Feuer",
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

def fval(v, unit="", decimals=1):
    if v is None: return "N/A"
    try:
        f = float(v)
        if decimals == 0: return f"{f:.0f}{unit}"
        return f"{f:.{decimals}f}{unit}"
    except: return f"{v}{unit}"

# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_weather():
    """Kompakte Wetterzeile."""
    state = ha_get("weather.forecast_home")
    if not state:
        return "N/A"

    attrs = state.get("attributes", {})
    temp = attrs.get("temperature")
    humidity = attrs.get("humidity")
    condition = state.get("state", "")

    parts = []
    if temp: parts.append(f"{fval(temp, '°C')}")
    if condition: parts.append(condition)
    if humidity: parts.append(f"{fval(humidity, '%', 0)} Feuchte")
    return ", ".join(parts) if parts else "N/A"

def get_daily_energy():
    """NUR eine Zeile: heutige Tagesenergie mit kurzer Deutung."""
    today = date.today().isoformat()

    # 1. Cache check
    try:
        if GUA_CACHE.exists():
            data = json.loads(GUA_CACHE.read_text())
            entries = data if isinstance(data, list) else [data];
            for entry in reversed(entries):
                td = entry.get("target_date", "")
                if entry.get("first_name") == "Reinhard" and td in (today, "heute"):
                    gua_t = entry.get("gua_t")
                    if gua_t:
                        return f"GUA {gua_t} ({GUA_NAMES.get(gua_t, '')})"
                    break
    except Exception:
        pass

    # 2. Fallback: Calculator direkt aufrufen
    try:
        r = subprocess.run(
            f"cd {GUA_CALC_DIR} && python3 -c \""
            "from gua_calculator import get_gua_energies; "
            "print(get_gua_energies('08.12.1962', 'Männlich', 'heute', 'Reinhard', "
            f"'{GUA_CACHE}'))\"",
            shell=True, capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            for line in r.stdout.split("\n"):
                if "TAGESENERGIE" in line.upper():
                    import re
                    m = re.search(r'GUA\s*(\d)', line)
                    if m:
                        g = int(m.group(1))
                        return f"GUA {g} ({GUA_NAMES.get(g, '')})"
                    break
    except Exception:
        pass

    return None

def get_critical_tasks():
    """Heute fällige + überfällige High-Prio-Aufgaben aus AUFGABEN.md."""
    if not AUFGABEN_MD.exists():
        return None, None

    content = AUFGABEN_MD.read_text()
    today_str = date.today().strftime("%Y-%m-%d")
    due_today, overdue_high = [], []

    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("|") and not line.startswith("|---"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4 and parts[1] and parts[1] != "Aufgabe":
                task = parts[1][:60]
                due_date = parts[2] if len(parts) > 2 else ""
                prio = parts[3] if len(parts) > 3 else ""

                if due_date == today_str:
                    due_today.append(task)
                elif due_date and due_date < today_str and "hoch" in prio.lower():
                    overdue_high.append(task)

    return due_today, overdue_high

def get_portfolio():
    """Top-3 Mover aus portfolio.db."""
    try:
        db = sqlite3.connect(str(PORTFOLIO_DB))
        cur = db.cursor()
        cur.execute("SELECT DISTINCT date FROM portfolio_history ORDER BY date DESC LIMIT 1")
        latest = cur.fetchone()
        if not latest: return None
        cur.execute(
            "SELECT name, delta_pct FROM portfolio_history "
            "WHERE date=? ORDER BY ABS(delta_pct) DESC LIMIT 3",
            (latest[0],)
        )
        movers = []
        for name, delta in cur.fetchall():
            emoji = "🟢" if delta >= 0 else "🔴"
            short = name[:30]
            movers.append(f"{emoji} {short} [{delta:+.1f}%]")
        return "  ".join(movers) if movers else None
    except Exception:
        return None

def get_pv_battery_cisterna():
    """PV, Batterie, Cisterna in einer Zeile."""
    pv1 = ha_get("sensor.fsp_ne_160305365_pv_1_input_power")
    pv2 = ha_get("sensor.fsp_ne_160305365_pv_2_input_power")
    cis = ha_get("sensor.cisterna_flussigkeitsfullstand")

    # PV total
    pv_total = None
    for s in [pv1, pv2]:
        if s and s.get("state") not in (None, "unavailable", "unknown"):
            try: pv_total = (pv_total or 0) + float(s["state"])
            except: pass

    # Batterie average SOC
    bat_avg = None
    socs = []
    for mod in [1, 2]:
        for pack in [1, 2, 3]:
            if mod == 1 and pack == 3: continue
            s = ha_get(f"sensor.fsp_ne_160305509_module_{mod}_battery_pack_{pack}_soc")
            if s and s.get("state") not in (None, "unavailable", "unknown"):
                try: socs.append(float(s["state"]))
                except: pass
    if socs: bat_avg = sum(socs) / len(socs)

    # Cisterna
    cis_val = None
    if cis and cis.get("state") not in (None, "unavailable", "unknown"):
        try: cis_val = float(cis["state"])
        except: pass

    parts = []
    parts.append(f"☀️ {fval(pv_total, 'W', 0)}" if pv_total is not None else "☀️ —W")
    parts.append(f"🔋 {fval(bat_avg, '%', 0)}" if bat_avg is not None else "🔋 —")
    parts.append(f"💧 {fval(cis_val, '%', 0)}" if cis_val is not None else "💧 —")
    return "  ".join(parts)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    today = date.today()
    dry = "--dry-run" in sys.argv

    weather = get_weather()
    daily_energy = get_daily_energy()
    due_today, overdue_high = get_critical_tasks()
    portfolio = get_portfolio()
    pv_bat_cis = get_pv_battery_cisterna()

    # ── Aufbau ──
    lines = []
    lines.append(f"🌅 *Guten Morgen, Reinhard!*")
    lines.append(f"{WEEKDAYS[today.weekday()]}, {today.day}. {MONTHS[today.month-1]} {today.year}")
    lines.append("")

    # Wetter
    lines.append(f"☀️ *Wetter:* {weather}")
    lines.append("")

    # Tagesenergie — eine prägnante Zeile
    if daily_energy:
        lines.append(f"⚡ *Heute:* {daily_energy}")
    lines.append("")

    # Kritische Aufgaben
    if due_today:
        lines.append(f"📋 *Heute fällig:* {', '.join(due_today[:4])}")
    if overdue_high:
        lines.append(f"⚠️ *Überfällig:* {', '.join(overdue_high[:4])}")
    if due_today or overdue_high:
        lines.append("")

    # Portfolio
    if portfolio:
        lines.append(f"📊 *Portfolio:* {portfolio}")
        lines.append("")

    # PV / Batterie / Cisterna
    lines.append(pv_bat_cis)
    lines.append("")

    # Routinen
    lines.append("🗓️ *Routinen:* Italienisch 🇮🇹  •  Feng Shui 🌿")

    message = "\n".join(lines)
    if len(message) > 4000:
        message = message[:4000] + "\n..."

    print(message)
    print(f"\n--- {len(message)} Zeichen ---")

    if dry:
        print("🔍 Dry-run")
    else:
        ok = send_telegram(message)
        print("✅ Telegram" if ok else "❌ Fehler")
        if not ok: sys.exit(1)

if __name__ == "__main__":
    main()
