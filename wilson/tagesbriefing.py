#!/usr/bin/env python3
"""
Tages-Briefing Generator — klassisches Morgenbriefing-Format.
Ersetzt den LLM-Agent-Cron-Job durch deterministische Datenabfrage.

Datenquellen:
  - Wetter:          HA weather.forecast_home (Fallback: N/A)
  - Gestern:         ~/Vaults/memory/YYYY-MM-DD.md
  - Portfolio:       ~/Vaults/portfolio.db (SQLite)
  - PV + Batterie:   HA FSP/PV-Sensoren
  - Cisterna:        HA sensor.cisterna_flussigkeitsfullstand
  - Aufgaben:        ~/Vaults/AUFGABEN.md (Markdown)
  - Feng Shui:       gua-energy-calculator

Aufruf: python3 tagesbriefing.py [--dry-run]
"""

import sqlite3, json, sys, os, subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

HA_URL = "http://192.168.86.183:8123"
HA_TOKEN_FILE = Path("/home/reinhard/.config/homeassistant/token")
VAULT_DIR = Path("/home/reinhard/Vaults")
PORTFOLIO_DB = VAULT_DIR / "portfolio.db"
AUFGABEN_MD = VAULT_DIR / "Aufgaben.md.bak-20260703"  # Aufgaben.md wurde Juni 2026 konsolidiert
GUA_CALC_DIR = Path("/home/reinhard/gua-energy-calculator")
GUA_CACHE = Path("/home/reinhard/Vaults/.openclaw/agents/fengshui-gua-calculator/calculations.json")

TELEGRAM_BOT = "8621101278:AAHI9CkevPBpZ2uxZQIFyxjGP2m4VUXislE"
TELEGRAM_CHAT = "8620231031"

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
           "Juli", "August", "September", "Oktober", "November", "Dezember"]

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _ha_token():
    t = HA_TOKEN_FILE.read_text().strip()
    if "=" in t and not t.startswith("eyJ"):
        t = t.split("=", 1)[-1].strip()
    return t

def ha_get(entity_id):
    """Fetch HA entity state. Returns (state, attributes) or (None, {})."""
    import urllib.request, urllib.error
    try:
        req = urllib.request.Request(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {_ha_token()}"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            d = json.loads(resp.read())
            return d.get("state"), d.get("attributes", {})
    except Exception:
        return None, {}

def send_telegram(text):
    """Send Markdown-formatted message via Telegram bot."""
    import urllib.request, urllib.error
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
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

def fmt_val(val, unit="", decimals=1):
    """Format numeric value, return 'N/A' if None."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        if decimals == 0:
            return f"{v:.0f}{unit}"
        return f"{v:.{decimals}f}{unit}"
    except (ValueError, TypeError):
        return f"{val}{unit}"

# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_weather():
    """Aktuelles Wetter aus HA weather.forecast_home."""
    state, attrs = ha_get("weather.forecast_home")
    if not state:
        return "N/A | Min/Max: N/A/N/A | Regen: N/A"

    temp = attrs.get("temperature")
    humidity = attrs.get("humidity")

    # Versuche OpenWeatherMap forecast (kann fehlen)
    owm_temp_low, _ = ha_get("sensor.openweathermap_forecast_temperature_low")
    owm_temp_high, _ = ha_get("sensor.openweathermap_forecast_temperature_high")
    owm_rain, _ = ha_get("sensor.openweathermap_forecast_precipitation_probability")

    parts = [fmt_val(temp, "°C")]
    if owm_temp_low and owm_temp_high:
        parts.append(f"Min/Max: {fmt_val(owm_temp_low, '°C')}/{fmt_val(owm_temp_high, '°C')}")
    else:
        parts.append(f"Min/Max: N/A/N/A")
    if owm_rain:
        parts.append(f"Regen: {fmt_val(owm_rain, '%', 0)}")
    elif humidity is not None:
        parts.append(f"Feuchte: {fmt_val(humidity, '%', 0)}")
    else:
        parts.append("Regen: N/A")

    return " | ".join(parts)

def get_yesterday_notes():
    """Gestrige Notizen aus Vault/memory/."""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    mf = VAULT_DIR / "memory" / f"{yesterday}.md"
    if mf.exists():
        lines = [l.strip("- ").strip() for l in mf.read_text().split("\n") if l.strip().startswith("-")]
        if lines:
            return "\n".join(f"  • {l}" for l in lines[:8])
    return "_(Keine Stichpunkte im Gedächtnis protokolliert)_"

def get_portfolio():
    """Portfolio-Daten aus portfolio.db (letzter Eintrag)."""
    try:
        db = sqlite3.connect(str(PORTFOLIO_DB))
        cur = db.cursor()
        cur.execute("SELECT DISTINCT date FROM portfolio_history ORDER BY date DESC LIMIT 1")
        latest = cur.fetchone()
        if not latest:
            return "Gesamt N/A"
        cur.execute(
            "SELECT name, delta_pct FROM portfolio_history "
            "WHERE date=? ORDER BY ABS(delta_pct) DESC LIMIT 4",
            (latest[0],)
        )
        movers = cur.fetchall()
        if not movers:
            return f"Gesamt N/A (Stand {latest[0]})"

        parts = [f"Gesamt N/A"]
        for name, delta in movers:
            emoji = "🟢" if delta >= 0 else "🔴"
            parts.append(f"{emoji} {name[:35]} [{delta:+.2f}]%")
        return " | ".join(parts)
    except Exception as e:
        print(f"[WARN] Portfolio: {e}", file=sys.stderr)
        return "Gesamt N/A"

def get_pv_battery():
    """PV-Produktion und Batterie-Status aus HA."""
    # PV: Summe beider Strings
    pv1, _ = ha_get("sensor.fsp_ne_160305365_pv_1_input_power")
    pv2, _ = ha_get("sensor.fsp_ne_160305365_pv_2_input_power")
    # Batterie: Durchschnitt über alle Packs
    bat_states = []
    for mod in [1, 2]:
        for pack in [1, 2, 3]:
            if mod == 1 and pack == 3:
                continue  # Module 1 hat nur 2 Packs
            soc, _ = ha_get(f"sensor.fsp_ne_160305509_module_{mod}_battery_pack_{pack}_soc")
            if soc and soc not in ("unavailable", "unknown"):
                try:
                    bat_states.append(float(soc))
                except ValueError:
                    pass

    pv_total = None
    if pv1 and pv1 not in ("unavailable", "unknown") and pv2 and pv2 not in ("unavailable", "unknown"):
        try:
            pv_total = float(pv1) + float(pv2)
        except ValueError:
            pass

    pv_str = f"{fmt_val(pv_total, 'W', 0)}" if pv_total is not None else "N/A"
    bat_str = f"{fmt_val(sum(bat_states)/len(bat_states), '%', 0)}" if bat_states else "N/A"
    return f"PV: {pv_str} | Batterie: {bat_str}"

def get_cisterna():
    """Cisterna-Füllstand aus HA."""
    state, _ = ha_get("sensor.cisterna_flussigkeitsfullstand")
    if state and state not in ("unavailable", "unknown"):
        return fmt_val(state, "%", 0)
    return "N/A"

def get_tasks():
    """Aufgaben aus AUFGABEN.md (Tabellen-Format wie in Backup)."""
    if not AUFGABEN_MD.exists():
        return None

    content = AUFGABEN_MD.read_text()
    from collections import Counter
    categories = Counter()

    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("|") and not line.startswith("|---"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5 and parts[1] and parts[1] not in ("Datum", "Aufgabe"):
                cat = parts[4] if len(parts) > 4 else "Sonstige"
                if cat:
                    # Kurzform für lange Kategorienamen
                    short = cat.replace("Podere dei venti - ", "").replace("Karlsruhe-", "")
                    short = short.replace("Giardino-Garten", "Giardino")
                    categories[short] += 1

    if not categories:
        return None

    sorted_cats = sorted(categories.items(), key=lambda x: -x[1])
    return " · ".join(f"{cat} ({count})" for cat, count in sorted_cats[:8])

def get_fengshui():
    """Feng Shui Tagesenergie aus gua-calculator."""
    # Versuche Cache
    if GUA_CACHE.exists():
        try:
            data = json.loads(GUA_CACHE.read_text())
            today = date.today().isoformat()
            if today in data:
                entry = data[today]
                focus = entry.get("focus") or entry.get("daily_focus") or ""
                if focus:
                    return focus
        except Exception:
            pass

    # Fallback: Calculator aufrufen
    try:
        r = subprocess.run(
            f"cd {GUA_CALC_DIR} && python3 -c \""
            "from gua_calculator import get_gua_energies; "
            "print(get_gua_energies('08.12.1962', 'Männlich', 'heute', 'Reinhard', "
            f"'{GUA_CACHE}'))\"",
            shell=True, capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()[:600]
    except Exception as e:
        print(f"[WARN] Feng Shui: {e}", file=sys.stderr)
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    today = date.today()
    dry = "--dry-run" in sys.argv

    weather = get_weather()
    yesterday = get_yesterday_notes()
    portfolio = get_portfolio()
    pv_bat = get_pv_battery()
    cisterna = get_cisterna()
    tasks = get_tasks()
    fengshui = get_fengshui()

    lines = [
        f"🌅 *GUTEN MORGEN, REINHARD!* {today.day}. {MONTHS[today.month-1]} {today.year}, {WEEKDAYS[today.weekday()]}",
        "",
        f"☀️ *WETTER:* {weather}",
        "",
        f"📓 *GESTERN:* {yesterday}",
        "",
        f"📊 *PORTFOLIO:* {portfolio}",
        "",
        f"☀️ {pv_bat}",
        "",
        f"💧 *CISTERNA:* {cisterna}",
        "",
    ]

    if tasks:
        lines.append(f"📋 *AUFGABEN:* {tasks}")
    else:
        lines.append("📋 *AUFGABEN:* _(keine Daten)_")

    lines.append("")
    lines.append("🗓️ *ROUTINEN:* Italienisch + Feng Shui")

    if fengshui:
        lines.append("")
        lines.append("---")
        lines.append(fengshui)

    message = "\n".join(lines)

    # Telegram-Limit 4096
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
