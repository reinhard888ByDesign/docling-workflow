"""
Vault-Migrationsskript: Flache Converted/-Dateien in Silo/Kategorie/Jahr-Struktur einordnen.

Aufruf:
  python3 migrate_vault.py --dry-run   # Zeigt was passieren würde
  python3 migrate_vault.py             # Führt Migration durch
"""

import os
import re
import sys
import time
import json
import shutil
import requests
import argparse
from pathlib import Path
from datetime import datetime

# ── Konfiguration ──────────────────────────────────────────────────────────────

VAULT_ROOT  = Path("/home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault")
CONVERTED   = VAULT_ROOT / "Converted"
ORIGINALE   = VAULT_ROOT / "Originale"
LOG_FILE    = Path("/tmp/vault_migration.log")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

_tg_offset = 0

# ── Silo + Kategorie Mapping ───────────────────────────────────────────────────

# Absender-Muster → (silo, kategorie)
ABSENDER_RULES = [
    # Krankenversicherungen
    (r"HUK.COBURG.Krank|HUK-COBURG-Kranken",          "krankenkasse", "leistungsabrechnung"),
    (r"Gothaer.Kranken|Gothaer Krank",                  "krankenkasse", "leistungsabrechnung"),
    (r"Krankenversicherung|Krankenkasse",                "krankenkasse", "leistungsabrechnung"),
    # Ärzte, Labore, medizinische Einrichtungen
    (r"Arztpraxis|Praxis|Dr\. med|Dr[\.\s]|Verrechnungsstelle", "krankenkasse", "arztrechnung"),
    (r"MVZ|Medizinisches Versorgungszentrum",           "krankenkasse", "befund"),
    (r"medical care|mediserv|unimed|amedes",            "krankenkasse", "arztrechnung"),
    (r"AugenCentrum|Institut.+Pathologie|Labor",        "krankenkasse", "befund"),
    (r"ABZ|PVS|Zahnärztliches Rechenzentrum",          "krankenkasse", "arztrechnung"),
    (r"Zahnarzt|Zahnaerzte|Zahnmedizin|Orthopädie",   "krankenkasse", "arztrechnung"),
    (r"Nelly Finance",                                   "krankenkasse", "arztrechnung"),
    # Apotheken, Sanitätshäuser, Hilfsmittel
    (r"Apotheke|Sanitätshaus|Schuhtechnik|Einlagen|Dein.Fu",  "krankenkasse", "arztrechnung"),
    # Physiotherapie, Heilpraktiker
    (r"Physiotherapeut|Heilpraktiker|Physiotherapie",          "krankenkasse", "arztrechnung"),
    # Radiologie, Pathologie, Diagnostik
    (r"Radiologie|Pathologie|Diagnostik|Bioscientia|MEDAS",    "krankenkasse", "befund"),
    # Augenoptik
    (r"Augenoptik|Optik|Brille",                               "krankenkasse", "arztrechnung"),
    # HUK- und Gothaer-Tippfehler-Varianten (Ollama-OCR-Fehler)
    (r"HUKCOBURG|HCheriidl|Hochleitner|Hocheitner|Fachärztezentrum|Knken", "krankenkasse", "arztrechnung"),
    (r"Gothaef|Gothaer.Vesicherung|GOTAHER|HUK Leistungsabrechnung",       "krankenkasse", "leistungsabrechnung"),
    # Weitere Arztpraxen / Kliniken
    (r"Arztin|Ärztin|Arztpraxen|Hautklinik|Augenklinik|Dermzentrum|Klinik","krankenkasse", "arztrechnung"),
    (r"PAS |dgpar|Büdingen Med|unmed|un!med|Dottssa|Dott\.",               "krankenkasse", "arztrechnung"),
    (r"Dr\.?:? med|Dr\.\s*med\.",                                           "krankenkasse", "arztrechnung"),
    (r"Dermaologic|Phlebologie|Proktologie|Kardiologie|Internist",          "krankenkasse", "arztrechnung"),
    (r"Blutbild|Schutzimpfung|Ohrproblem|Behandlung|ärztliche Leistung",   "krankenkasse", "arztrechnung"),
    # Maria Schneider als Patientin/Ärztin
    (r"MARIA.?SCHNEIDER|Maria.Schneider|MARIASCHNEIDER",                   "krankenkasse", "befund"),
    # Stadtwerke, Energie, Telefon
    (r"Stadtwerke|Strom|Energie|Gas|Wasser",            "finanzen",     "rechnung"),
    (r"Telekom|Vodafone|O2|Telefon|Internet",           "finanzen",     "rechnung"),
    # Versicherungen (nicht Kranken)
    (r"Gothaer Versicherung AG|Allianz|DEVK|ADAC",     "finanzen",     "versicherung"),
    # Finanzinstitute
    (r"Bank|Sparkasse|Volksbank",                       "finanzen",     "kontoauszug"),
    # Finanzamt, Steuerbüro
    (r"Finanzamt|Steuerberater|ELSTER",                 "finanzen",     "steuer"),
]

# Kategorie-Mapping (alter Wert → neuer Wert pro Silo)
KATEGORIE_MAP = {
    "krankenkasse": {
        "Versicherung":  "leistungsabrechnung",
        "Erstattung":    "leistungsabrechnung",
        "Rechnung":      "arztrechnung",
        "Arztbrief":     "befund",
        "Korrespondenz": "korrespondenz",
        "Finanzen":      "sonstiges",
        "Sonstiges":     "sonstiges",
    },
    "finanzen": {
        "Rechnung":      "rechnung",
        "Versicherung":  "versicherung",
        "Finanzen":      "sonstiges",
        "Korrespondenz": "korrespondenz",
        "Sonstiges":     "sonstiges",
    },
    "archiv": {
        "Korrespondenz": "korrespondenz",
        "Sonstiges":     "sonstiges",
    },
}

PRIVACY_MAP = {
    "krankenkasse": "lokal",
    "finanzen":     "lokal",
    "anleitungen":  "cloud-ok",
    "projekte":     "lokal",
    "archiv":       "lokal",
    "inbox":        "lokal",
}

# ── Frontmatter parsen ─────────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> dict:
    meta = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    block = text[3:end]
    for line in block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"\'')
    return meta


def update_frontmatter(text: str, updates: dict) -> str:
    """Ergänzt oder überschreibt Felder im YAML-Frontmatter."""
    if not text.startswith("---"):
        fm_lines = ["---"] + [f"{k}: {v}" for k, v in updates.items()] + ["---", "", text]
        return "\n".join(fm_lines)

    end = text.find("\n---", 3)
    if end == -1:
        return text

    block = text[3:end]
    lines = block.splitlines()
    existing_keys = {l.split(":")[0].strip() for l in lines if ":" in l}

    new_lines = []
    for line in lines:
        if ":" in line:
            key = line.split(":")[0].strip()
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                del updates[key]
                continue
        new_lines.append(line)

    # Restliche neue Keys einfügen
    for key, val in updates.items():
        new_lines.append(f"{key}: {val}")

    return "---\n" + "\n".join(new_lines) + text[end:]


# ── Silo-Erkennung ─────────────────────────────────────────────────────────────

def detect_silo_and_kategorie(absender: str, alt_kategorie: str, filename: str = "") -> tuple[str, str] | None:
    """Gibt (silo, neue_kategorie) zurück oder None wenn unklar."""
    # Erst Absender prüfen, dann Filename als Fallback
    for search_str in [absender, filename]:
        if not search_str:
            continue
        for pattern, silo, kat in ABSENDER_RULES:
            if re.search(pattern, search_str, re.IGNORECASE):
                return silo, kat

    # Fallback: alte Kategorie auswerten
    if alt_kategorie in ["Versicherung", "Erstattung"]:
        return "krankenkasse", "leistungsabrechnung"
    if alt_kategorie == "Arztbrief":
        return "krankenkasse", "befund"
    if alt_kategorie == "Rechnung":
        return None  # unklar — Telegram fragen
    if alt_kategorie in ["Korrespondenz"]:
        return "archiv", "korrespondenz"

    return None  # unklar


def extract_year(filename: str) -> str:
    """Extrahiert Jahr aus YYYYMMDD-Präfix des Dateinamens."""
    m = re.match(r"^(\d{4})\d{4}", filename)
    return m.group(1) if m else "unbekannt"


# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str, reply_markup: dict = None) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
    except Exception:
        pass


def tg_get_updates() -> list:
    global _tg_offset
    if not TELEGRAM_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _tg_offset, "timeout": 5},
            timeout=10
        )
        if r.ok:
            updates = r.json().get("result", [])
            if updates:
                _tg_offset = updates[-1]["update_id"] + 1
            return updates
    except Exception:
        pass
    return []


def tg_ask(filename: str, absender: str, alt_kat: str, absender_snippet: str) -> tuple[str, str]:
    """Fragt via Telegram nach Silo/Kategorie. Blockiert bis Antwort."""
    silos = "finanzen | krankenkasse | anleitungen | archiv | projekte"
    tg_send(
        f"❓ <b>Unklar:</b> {filename}\n"
        f"🏢 Absender: {absender}\n"
        f"📁 Alt-Kategorie: {alt_kat}\n\n"
        f"Bitte eingeben: <code>silo/kategorie</code>\n"
        f"Silos: <code>{silos}</code>"
    )
    tg_get_updates()  # Puffer leeren
    while True:
        for upd in tg_get_updates():
            if "message" in upd:
                msg = upd["message"]
                if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT):
                    continue
                text = msg.get("text", "").strip()
                if "/" in text:
                    parts = text.split("/", 1)
                    silo = parts[0].strip().lower()
                    kat  = parts[1].strip().lower()
                    tg_send(f"✅ {filename} → <b>{silo}</b> / <b>{kat}</b>")
                    return silo, kat
                else:
                    tg_send(f"Format: <code>silo/kategorie</code> — bitte nochmal:")
        time.sleep(2)


# ── Migration ──────────────────────────────────────────────────────────────────

def migrate_file(md_file: Path, dry_run: bool, log_lines: list) -> dict:
    """Migriert eine einzelne MD-Datei. Gibt Info-Dict zurück."""
    text = md_file.read_text(encoding="utf-8", errors="ignore")
    meta = parse_frontmatter(text)

    absender    = meta.get("absender", "")
    alt_kat     = meta.get("kategorie", "Sonstiges")
    already_silo = meta.get("silo", "")
    filename    = md_file.name

    # Bereits migriert?
    if already_silo and md_file.parent.name != "Converted":
        return {"status": "already_migrated", "file": filename}

    # Silo + Kategorie ermitteln
    result = detect_silo_and_kategorie(absender, alt_kat, filename)

    if result is None:
        silo, neue_kat = "inbox", alt_kat.lower() if alt_kat else "sonstiges"
        log_lines.append(f"UNKLAR (→ inbox): {filename} | absender={absender}")
    else:
        silo, neue_kat = result

    year = extract_year(filename)

    # Zielpfad
    target_dir = CONVERTED / silo / neue_kat / year
    target_file = target_dir / filename

    # Originale ebenfalls verschieben falls vorhanden
    orig_suffix = meta.get("quelle", "")
    orig_src = ORIGINALE / orig_suffix if orig_suffix else None
    orig_dst = (ORIGINALE / silo / neue_kat / year / orig_suffix) if orig_suffix else None

    log_lines.append(
        f"{'[DRY]' if dry_run else '[MOVE]'} {filename}\n"
        f"  → {silo}/{neue_kat}/{year}/\n"
        f"  absender={absender}, alt_kat={alt_kat}"
    )

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

        # Frontmatter aktualisieren
        updated_text = update_frontmatter(text, {
            "silo":     silo,
            "kategorie": neue_kat,
            "privacy":  PRIVACY_MAP.get(silo, "lokal"),
        })
        target_file.write_text(updated_text, encoding="utf-8")

        # Original nur löschen wenn erfolgreich geschrieben
        if target_file.exists():
            md_file.unlink()

        # Original-Datei verschieben
        if orig_src and orig_src.exists() and orig_dst:
            orig_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(orig_src), str(orig_dst))

    return {"status": "migrated", "file": filename, "silo": silo, "kategorie": neue_kat, "year": year}


def run(dry_run: bool):
    log_lines = []
    stats = {"migrated": 0, "already": 0, "unclear": 0, "errors": 0}

    # Alle flachen .md-Dateien in Converted/ (nicht in Unterordnern)
    flat_files = [f for f in CONVERTED.iterdir() if f.is_file() and f.suffix == ".md"]
    total = len(flat_files)

    print(f"{'[DRY RUN] ' if dry_run else ''}Starte Migration von {total} Dateien...")

    if TELEGRAM_TOKEN:
        tg_send(
            f"{'🔍 DRY RUN: ' if dry_run else '🚀 '}Migration startet\n"
            f"{total} Dateien werden klassifiziert.\n"
            f"Unklare Fälle werden hier gefragt."
        )

    for i, md_file in enumerate(sorted(flat_files), 1):
        try:
            result = migrate_file(md_file, dry_run, log_lines)
            if result["status"] == "already_migrated":
                stats["already"] += 1
            elif result["status"] == "migrated":
                stats["migrated"] += 1
                if result.get("silo") == "inbox":
                    stats["unclear"] += 1
        except Exception as e:
            stats["errors"] += 1
            log_lines.append(f"FEHLER: {md_file.name}: {e}")
            print(f"Fehler bei {md_file.name}: {e}")

        if i % 100 == 0:
            print(f"  {i}/{total} verarbeitet...")
            tg_send(f"⏳ Migration: {i}/{total} Dateien verarbeitet...")

    # Bericht
    report = (
        f"{'[DRY RUN] ' if dry_run else ''}Migration abgeschlossen\n"
        f"  Migriert:       {stats['migrated']}\n"
        f"  Bereits fertig: {stats['already']}\n"
        f"  Unklar (inbox): {stats['unclear']}\n"
        f"  Fehler:         {stats['errors']}"
    )
    print(report)

    LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"Log: {LOG_FILE}")

    if TELEGRAM_TOKEN:
        tg_send(f"{'🔍 ' if dry_run else '✅ '}{report}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts verschieben")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
