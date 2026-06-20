#!/usr/bin/env python3
"""
Wilson Document Processor
Watches ~/incoming/ for PDFs, extracts metadata via Ryzen Dispatcher (OCR + Ollama),
manages 60-min pending queue, handles Telegram corrections.
"""

import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

# ── Konfiguration ──────────────────────────────────────────────────────────────

INCOMING_DIR    = Path.home() / "incoming"
PENDING_DIR     = Path.home() / "pending"
OUTPUT_DIR      = Path.home() / "input-dispatcher"
DB_PATH         = Path.home() / ".openclaw" / "doc_processor.db"

DISPATCHER_URL  = os.environ.get("DISPATCHER_URL",  "http://192.168.86.195:8765")
OLLAMA_URL      = os.environ.get("OLLAMA_URL",       "http://192.168.86.195:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL",     "gemma4:e4b")
DEEPSEEK_KEY    = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL  = os.environ.get("DEEPSEEK_MODEL",   "deepseek-chat")
DEEPSEEK_URL    = "https://api.deepseek.com/v1/chat/completions"

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID",   "8620231031")
DISABLE_TG_POLL = os.environ.get("DISABLE_TELEGRAM_POLL", "0") == "1"
PENDING_MINUTES = int(os.environ.get("PENDING_MINUTES", "60"))
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL",   "15"))   # Sekunden zwischen Verzeichnis-Scans

GOG_BIN             = os.environ.get("GOG_BIN",  "/home/linuxbrew/.linuxbrew/bin/gog")
GOG_ACCOUNT         = os.environ.get("GOG_ACCOUNT", "reinhard.janning@googlemail.com")
EMAIL_POLL_INTERVAL = int(os.environ.get("EMAIL_POLL_INTERVAL", "300"))  # Sekunden
EMAIL_ARCHIVE_LABEL = os.environ.get("EMAIL_ARCHIVE_LABEL", "Belege")   # Label nach Transfer
EMAIL_SEARCH_QUERY  = os.environ.get("EMAIL_SEARCH_QUERY", "in:inbox")

SENDER_UI_PORT       = int(os.environ.get("SENDER_UI_PORT", "8771"))
SENDER_UI_HOST       = os.environ.get("SENDER_UI_HOST", "192.168.3.124")
SENDER_SCAN_QUERY    = os.environ.get("SENDER_SCAN_QUERY", "is:anywhere")

CATEGORIES = {
    "persoenlich":          "10 Persönlich — Ausweise, Urkunden, Vollmachten",
    "familie":              "20 Familie — Familie Janning/Hutterer, Haustiere",
    "fengshui":             "30 FengShui — Beratung und Audits",
    "finanzen":             "40 Finanzen — Banken, Versicherungen, Steuern",
    "krankenversicherung":  "49 Krankenversicherung — HUK, Gothaer, vigo, Arztrechnungen",
    "versicherung":         "Versicherung — Sachversicherungen, Kfz-Versicherung",
    "immobilien_eigen":     "50 Immobilien eigen — Seggiano, Übersee",
    "immobilien_vermietet": "51 Immobilien vermietet — München, Bremen, Karlsruhe",
    "garten":               "55 Garten — Landschaftspflege, Bewässerung",
    "fahrzeuge":            "60 Fahrzeuge — KFZ, Werkstatt, TÜV",
    "italien":              "70 Italien — Behörden, Comune (nicht Immobilien)",
    "business":             "80 Business — 888byDesign, Buchhaltung",
    "digitales":            "82 Digitales — Smart Home, IT, Netzwerk",
    "wissen":               "85 Wissen — Bücher, Kurse",
    "reisen":               "90 Reisen — Flüge, Hotels, Buchungen",
    "bedienungsanleitung":  "95 Bedienungsanleitungen — Gerätehandbücher",
    "archiv":               "99 Archiv — Historisches, nicht zuordenbar",
    "zustellung":           "Zustellungsbenachrichtigung — Paket, Lieferung",
}

ADRESSATEN = ["Reinhard", "Marion", "Linoa", "Sonstiges"]

# Kurze Anzeigetexte für Telegram-Buttons (max ~18 Zeichen)
_CAT_SHORT = {
    "persoenlich":          "10 Persönlich",
    "familie":              "20 Familie",
    "fengshui":             "30 FengShui",
    "finanzen":             "40 Finanzen",
    "krankenversicherung":  "49 Krankenvers.",
    "versicherung":         "Versicherung",
    "immobilien_eigen":     "50 Immo eigen",
    "immobilien_vermietet": "51 Immo vermietet",
    "garten":               "55 Garten",
    "fahrzeuge":            "60 Fahrzeuge",
    "italien":              "70 Italien",
    "business":             "80 Business",
    "digitales":            "82 Digitales",
    "wissen":               "85 Wissen",
    "reisen":               "90 Reisen",
    "bedienungsanleitung":  "95 Anleitung",
    "archiv":               "99 Archiv",
    "zustellung":           "Zustellung",
}

# Status-Werte für geführten Dialog
GUIDED_STATES = ("guided_kat", "guided_adr", "guided_abs", "guided_fin")

# Deterministisches Absender-Override: Muster → (kategorie_id, adressat)
# Schlägt LLM-Ergebnis, da der Absender ein harter Fakt ist.
ABSENDER_OVERRIDE: list[tuple[str, str, str | None]] = [
    # (muster_lower, kategorie_id, adressat_override)
    ("huk-coburg",           "krankenversicherung", "Marion"),
    ("huk coburg",           "krankenversicherung", "Marion"),
    ("huk",                  "krankenversicherung", "Marion"),
    ("gothaer krankenvers",  "krankenversicherung", "Reinhard"),
    ("gothaer",              "krankenversicherung", "Reinhard"),
    ("barmenia",             "krankenversicherung", "Reinhard"),
    ("vigo krankenvers",     "krankenversicherung", "Reinhard"),
    ("vigo",                 "krankenversicherung", "Reinhard"),
    ("tierarzt",             "familie",             None),
    ("tierklinik",           "familie",             None),
    ("veterinär",            "familie",             None),
    ("veterinaria",          "familie",             None),
    ("clinica veterinaria",  "familie",             None),
]

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Datenbank ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS pending (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pdf_hash    TEXT UNIQUE,
                orig_name   TEXT,
                pending_dir TEXT,
                sidecar     TEXT,           -- JSON der Metadaten
                tg_msg_id   INTEGER,        -- Telegram-Message-ID der Benachrichtigung
                status      TEXT DEFAULT 'pending',  -- pending|correcting|confirmed|rejected|transferred
                corr_field  TEXT,           -- Feld, das gerade korrigiert wird
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                transfer_at TEXT            -- geplante Weitergabe
            );

            CREATE TABLE IF NOT EXISTS email_pending (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id  TEXT UNIQUE,    -- Gmail Message-ID
                thread_id   TEXT,           -- Gmail Thread-ID (für Archivieren)
                subject     TEXT,
                sender      TEXT,
                received_at TEXT,
                pending_dir TEXT,           -- ~/pending/email-{id}/
                sidecar     TEXT,           -- JSON der Metadaten (erst nach LLM-Klassifikation)
                tg_msg_id   INTEGER,
                status      TEXT DEFAULT 'pending',  -- awaiting_approval|pending|correcting|confirmed|rejected|transferred|blocked
                corr_field  TEXT,
                raw_data    TEXT,           -- vollständige email_data (für spätere Verarbeitung nach Absender-Freigabe)
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                transfer_at TEXT
            );

            CREATE TABLE IF NOT EXISTS email_senders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                address      TEXT UNIQUE,   -- email@domain.com oder @domain.com (Domain-Freigabe)
                display_name TEXT,
                status       TEXT DEFAULT 'pending',  -- pending|approved|blocked
                first_seen   TEXT DEFAULT (datetime('now','localtime')),
                updated_at   TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_date TEXT,
                text          TEXT,
                sent          INTEGER DEFAULT 0,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS tg_offset (
                id      INTEGER PRIMARY KEY,
                offset  INTEGER DEFAULT 0
            );
            INSERT OR IGNORE INTO tg_offset(id, offset) VALUES (1, 0);
        """)
        # Migration: raw_data column (für Instanzen vor v2.1)
        try:
            con.execute("ALTER TABLE email_pending ADD COLUMN raw_data TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: Archiv-Scan-Felder (v2.2)
        for col_sql in [
            "ALTER TABLE email_senders ADD COLUMN archive_count INTEGER DEFAULT 0",
            "ALTER TABLE email_senders ADD COLUMN last_scanned TEXT",
        ]:
            try:
                con.execute(col_sql)
            except sqlite3.OperationalError:
                pass
        # Migration: Kategorie + Adressat pro Absender (v2.3)
        for col_sql in [
            "ALTER TABLE email_senders ADD COLUMN category_id TEXT",
            "ALTER TABLE email_senders ADD COLUMN adressat TEXT",
        ]:
            try:
                con.execute(col_sql)
            except sqlite3.OperationalError:
                pass
        # Migration: Retro-Import-Flag (v2.4)
        try:
            con.execute("ALTER TABLE email_senders ADD COLUMN archive_imported INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Migration: Kontaktdaten (v2.5)
        for col_sql in [
            "ALTER TABLE email_senders ADD COLUMN phone TEXT",
            "ALTER TABLE email_senders ADD COLUMN postal TEXT",
            "ALTER TABLE email_senders ADD COLUMN website TEXT",
            "ALTER TABLE email_senders ADD COLUMN notes TEXT",
            "ALTER TABLE email_senders ADD COLUMN contact_updated TEXT",
        ]:
            try:
                con.execute(col_sql)
            except sqlite3.OperationalError:
                pass
        # Einmalige Bereinigung: Anführungszeichen aus vorhandenen Display-Namen entfernen
        con.execute("UPDATE email_senders SET display_name = REPLACE(display_name, '\"', '') WHERE display_name LIKE '%\"%'")

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def normalize_filename(text: str) -> str:
    """Entfernt Sonderzeichen und normalisiert für Dateinamen."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"[\s_]+", "-", text.strip())
    return text[:50]

def _is_valid_date8(s: str) -> bool:
    """Prüft ob 8-stelliger String ein plausibles YYYYMMDD ist."""
    if len(s) != 8 or not s.isdigit():
        return False
    try:
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:])
        return 1950 <= y <= 2035 and 1 <= m <= 12 and 1 <= d <= 31
    except ValueError:
        return False


def build_filename(sidecar: dict) -> str:
    datum = sidecar.get("datum", "")
    if datum:
        datum_clean = datum.replace("-", "")[:8]
        if not _is_valid_date8(datum_clean):
            datum_clean = "_NODATE_"
    else:
        datum_clean = "_NODATE_"
    absender = normalize_filename(sidecar.get("absender", "Unbekannt"))
    kurz = normalize_filename(sidecar.get("kurzbezeichnung", ""))
    parts = [p for p in [datum_clean, absender, kurz] if p]
    return "_".join(parts) + ".pdf"

def pdf_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# ── Ryzen-Kommunikation ────────────────────────────────────────────────────────

def ocr_pdf(pdf_path: Path) -> str | None:
    """Sendet PDF an Dispatcher /api/ocr, gibt Markdown-Text zurück."""
    log.info(f"OCR via Dispatcher: {pdf_path.name}")
    try:
        with open(pdf_path, "rb") as f:
            r = requests.post(
                f"{DISPATCHER_URL}/api/ocr",
                files={"file": (pdf_path.name, f, "application/pdf")},
                timeout=300,
            )
        if r.status_code != 200:
            log.error(f"OCR-Fehler {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("text")
    except Exception as e:
        log.error(f"OCR-Aufruf fehlgeschlagen: {e}")
        return None

def extract_metadata(ocr_text: str) -> dict | None:
    """Extrahiert Metadaten: DeepSeek API wenn Key vorhanden, sonst Ollama."""
    if DEEPSEEK_KEY:
        return _extract_metadata_deepseek(ocr_text)
    return _extract_metadata_ollama(ocr_text)


def _extract_metadata_deepseek(ocr_text: str) -> dict | None:
    """Metadaten-Extraktion via DeepSeek API."""
    return _run_llm_extraction(
        ocr_text,
        url=DEEPSEEK_URL,
        payload_fn=lambda prompt: {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1024,
        },
        content_fn=lambda r: r.json()["choices"][0]["message"]["content"],
        label=f"DeepSeek ({DEEPSEEK_MODEL})",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
    )


def _extract_metadata_ollama(ocr_text: str) -> dict | None:
    """Ollama-Fallback (identisch mit ursprünglichem extract_metadata-Body)."""
    return _run_llm_extraction(
        ocr_text,
        url=f"{OLLAMA_URL}/api/chat",
        payload_fn=lambda prompt: {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_ctx": 8192, "temperature": 0.1},
        },
        content_fn=lambda r: r.json()["message"]["content"],
        label=f"Ollama ({OLLAMA_MODEL})",
        headers={},
    )


def _run_llm_extraction(ocr_text, url, payload_fn, content_fn, label, headers):
    """Gemeinsame LLM-Extraktionslogik für DeepSeek und Ollama."""
    cat_list = "\n".join(f"  {k}: {v}" for k, v in CATEGORIES.items())
    prompt = _build_extraction_prompt(cat_list, ocr_text)
    log.info(f"LLM-Extraktion via {label}")
    try:
        r = requests.post(url, json=payload_fn(prompt), headers=headers, timeout=180)
        if r.status_code != 200:
            log.error(f"{label} Fehler {r.status_code}: {r.text[:200]}")
            return None
        content = content_fn(r)
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            log.error(f"Kein JSON in {label}-Antwort: {content[:200]}")
            return None
        meta = json.loads(m.group())
        import html as _html
        for _f in ("absender", "kurzbezeichnung", "beschreibung"):
            if isinstance(meta.get(_f), str):
                meta[_f] = _html.unescape(meta[_f])
        if meta.get("kategorie_id") not in CATEGORIES:
            meta["kategorie_id"] = "archiv"
        if meta.get("adressat") not in ADRESSATEN:
            meta["adressat"] = "Sonstiges"
        # Deterministischer Keyword-Override für immobilien_eigen
        ocr_lower = ocr_text.lower()
        descr_lower = (meta.get("beschreibung") or "").lower()
        IMMO_EIGEN_KEYWORDS = [
            "podere dei venti", "poderedeiventi", "seggiano", "grassauer",
            "servizi ecologici", "fognaria", "bonifica", "manutenzione",
        ]
        if meta.get("kategorie_id") == "archiv" and any(
            kw in ocr_lower or kw in descr_lower for kw in IMMO_EIGEN_KEYWORDS
        ):
            log.info("Keyword-Override: archiv → immobilien_eigen (Immobilien-Keyword erkannt)")
            meta["kategorie_id"] = "immobilien_eigen"
            if meta.get("adressat") == "Sonstiges":
                meta["adressat"] = "Reinhard"
        # Absender-Override (hardcoded Muster)
        absender_lower = (meta.get("absender") or "").lower()
        for muster, kat, adr in ABSENDER_OVERRIDE:
            if muster in absender_lower:
                if meta.get("kategorie_id") != kat:
                    log.info(f"Absender-Override: '{meta['kategorie_id']}' → '{kat}'")
                    meta["kategorie_id"] = kat
                if adr and meta.get("adressat") != adr:
                    meta["adressat"] = adr
                meta["_override_applied"] = True
                break
        # Domain-Override aus email_senders-DB: @domain im Dokument nachschlagen
        if not meta.get("_override_applied"):
            domains_in_doc = re.findall(r"@([\w\-]+\.[\w\-]{2,})", ocr_lower)
            for dom in dict.fromkeys(domains_in_doc):  # dedupliziert, Reihenfolge erhalten
                row = _get_sender_row("@" + dom)
                if row and row["category_id"]:
                    log.info(f"Domain-Override (DB): @{dom} → {row['category_id']} (war: {meta.get('kategorie_id')})")
                    meta["kategorie_id"] = row["category_id"]
                    if row["adressat"]:
                        meta["adressat"] = row["adressat"]
                    if row["display_name"] and not meta.get("absender"):
                        meta["absender"] = row["display_name"]
                    meta["_override_applied"] = True
                    break
        return meta
    except json.JSONDecodeError as e:
        log.error(f"JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        log.error(f"{label}-Aufruf fehlgeschlagen: {e}")
        return None


def _build_extraction_prompt(cat_list: str, ocr_text: str) -> str:
    return f"""Du bist ein Dokumenten-Assistent. Analysiere den folgenden OCR-Text und antworte ausschließlich mit einem JSON-Objekt — kein Text davor oder danach.

GÜLTIGE KATEGORIEN (kategorie_id → Beschreibung):
{cat_list}

KATEGORIE-REGELN (lese diese sorgfältig, bevor du klassifizierst):

1. krankenversicherung — gilt für ALLE Dokumente von Krankenkassen oder Ärzten:
   - Absender ist HUK, HUK-COBURG → krankenversicherung, adressat="Marion"
   - Absender ist Gothaer, Barmenia, vigo → krankenversicherung, adressat="Reinhard"
   - Absender ist Arzt, Krankenhaus, Labor, MVZ, Zahnarzt, Sanitätshaus, Apotheke → krankenversicherung
   AUSNAHME: Tierarzt / Tierklinik / Veterinär → kategorie="familie"

2. versicherung — nur Sachversicherungen, KFZ-Versicherung (NICHT Krankenversicherung):
   - Gebäudeversicherung, Haftpflicht, KFZ-Versicherung → versicherung

3. immobilien_eigen — Rechnungen für eigene Immobilien:
   - Text enthält: Seggiano, Podere dei Venti, Poderedeiventi, Übersee, Grassauer Straße → immobilien_eigen
   - Servizi Ecologici, Fognaria, Bonifica, Manutenzione, Ecologia → immobilien_eigen
   - adressat="Reinhard" wenn kein anderer Empfänger genannt

4. immobilien_vermietet — Objekte: München/Lipowskystraße, Bremen/Kornstraße,
   Karlsruhe/Kolberger, Neuburg/Schießhausstraße, Schechen/Bahnhofstraße → immobilien_vermietet

5. archiv — LETZTER AUSWEG, nur wenn wirklich keine Kategorie passt

ABSENDER → ADRESSAT-REGELN:
- HUK / HUK-COBURG → adressat="Marion" (IMMER)
- Gothaer / Barmenia / vigo → adressat="Reinhard" (IMMER)
- Dokument für eigene Immobilie (Seggiano, Podere, Übersee) → adressat="Reinhard"

EXTRAHIERE folgende Felder:
{{
  "absender": "Firmen- oder Personenname, kurz und präzise",
  "datum": "Dokumentdatum als YYYY-MM-DD",
  "kategorie_id": "eine der obigen kategorie_id-Werte",
  "adressat": "Reinhard" oder "Marion" oder "Linoa" oder "Sonstiges",
  "kurzbezeichnung": "2-4 Wörter mit Bindestrichen",
  "beschreibung": "3-5 vollständige Sätze auf Deutsch: Worum geht es, welche Beträge, welcher Zeitraum?",
  "konfidenz": "hoch" | "mittel" | "niedrig"
}}

OCR-TEXT:
---
{ocr_text[:6000]}
---"""

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str, reply_markup: dict | None = None, parse_mode: str = "HTML") -> int | None:
    """Sendet Telegram-Nachricht, gibt message_id zurück."""
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=15,
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        log.warning(f"Telegram sendMessage Fehler: {data}")
    except Exception as e:
        log.error(f"Telegram-Fehler: {e}")
    return None

def tg_edit(msg_id: int, text: str, reply_markup: dict | None = None):
    """Bearbeitet bestehende Telegram-Nachricht."""
    payload = {"chat_id": TELEGRAM_CHAT, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json=payload, timeout=15,
        )
    except Exception as e:
        log.error(f"Telegram editMessage Fehler: {e}")

def tg_answer_callback(callback_id: str, text: str = ""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_id": callback_id, "text": text}, timeout=10,
        )
    except Exception:
        pass

def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 10, "allowed_updates": ["message", "callback_query"]},
            timeout=20,
        )
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception:
        return []

def format_notification(sidecar: dict, transfer_at: str, doc_id: int) -> tuple[str, dict]:
    """Erstellt Telegram-Benachrichtigungstext und Keyboard."""
    s = sidecar
    datum_fmt = s.get("datum") or "unbekannt"
    beschreibung = s.get("beschreibung") or "–"
    dateiname = s.get("dateiname") or "unbekannt"
    transfer_dt = datetime.fromisoformat(transfer_at)
    minuten = max(0, int((transfer_dt - datetime.now()).total_seconds() / 60))

    text = (
        f"📄 <b>Neues Dokument erkannt</b>\n"
        f"{'─' * 35}\n"
        f"📋 <b>Absender:</b>  {s.get('absender') or '?'}\n"
        f"📅 <b>Datum:</b>     {datum_fmt}\n"
        f"📁 <b>Kategorie:</b> {CATEGORIES.get(s.get('kategorie_id',''), s.get('kategorie_id','?'))}\n"
        f"👤 <b>Adressat:</b>  {s.get('adressat') or '?'}\n"
        f"{'─' * 35}\n"
        f"📝 <b>Beschreibung:</b>\n{beschreibung}\n"
        f"{'─' * 35}\n"
        f"📌 <code>{dateiname}</code>\n"
        f"⏱️ Weitergabe in {minuten} Min."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Jetzt senden",  "callback_data": f"confirm:{doc_id}"},
        {"text": "✏️ Korrigieren",   "callback_data": f"correct:{doc_id}"},
        {"text": "🗑️ Ablehnen",      "callback_data": f"reject:{doc_id}"},
    ]]}
    return text, keyboard

def format_correction_menu(doc_id: int) -> tuple[str, dict]:
    text = "✏️ <b>Was soll geändert werden?</b>"
    keyboard = {"inline_keyboard": [
        [
            {"text": "Absender",     "callback_data": f"field:{doc_id}:absender"},
            {"text": "Datum",        "callback_data": f"field:{doc_id}:datum"},
            {"text": "Kategorie",    "callback_data": f"field:{doc_id}:kategorie_id"},
        ],
        [
            {"text": "Adressat",     "callback_data": f"field:{doc_id}:adressat"},
            {"text": "Kurzbezeichnung", "callback_data": f"field:{doc_id}:kurzbezeichnung"},
            {"text": "Beschreibung", "callback_data": f"field:{doc_id}:beschreibung"},
        ],
        [
            {"text": "← Zurück",     "callback_data": f"back:{doc_id}"},
        ],
    ]}
    return text, keyboard

def format_category_keyboard(doc_id: int) -> tuple[str, dict]:
    text = "📁 <b>Kategorie wählen:</b>"
    cats = list(CATEGORIES.keys())
    rows = []
    for i in range(0, len(cats), 2):
        row = [{"text": cats[i], "callback_data": f"setcat:{doc_id}:{cats[i]}"}]
        if i + 1 < len(cats):
            row.append({"text": cats[i+1], "callback_data": f"setcat:{doc_id}:{cats[i+1]}"})
        rows.append(row)
    rows.append([{"text": "← Zurück", "callback_data": f"correct:{doc_id}"}])
    return text, {"inline_keyboard": rows}

def format_adressat_keyboard(doc_id: int) -> tuple[str, dict]:
    text = "👤 <b>Adressat wählen:</b>"
    keyboard = {"inline_keyboard": [
        [{"text": a, "callback_data": f"setadr:{doc_id}:{a}"} for a in ADRESSATEN],
        [{"text": "← Zurück", "callback_data": f"correct:{doc_id}"}],
    ]}
    return text, keyboard

# ── Geführter Klassifikations-Dialog ──────────────────────────────────────────

def format_guided_step_kat(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Schritt 1: Kategorie wählen."""
    konfidenz = meta.get("konfidenz", "mittel")
    vorschlag = meta.get("kategorie_id", "")
    vorschlag_label = _CAT_SHORT.get(vorschlag, vorschlag)
    text = (
        f"⚠️ <b>Wilson ist unsicher</b> (Konfidenz: {konfidenz})\n"
        f"{'─' * 32}\n"
        f"📄 <code>{meta.get('dateiname', '?')}</code>\n"
        f"📅 {meta.get('datum') or '?'}  ·  {meta.get('absender') or '?'}\n"
        f"{'─' * 32}\n"
        f"Mein Vorschlag: <b>{vorschlag_label}</b>\n\n"
        f"<b>① Kategorie wählen:</b>"
    )
    cats = list(CATEGORIES.keys())
    rows = []
    for i in range(0, len(cats), 2):
        row = [{"text": _CAT_SHORT.get(cats[i], cats[i]),
                "callback_data": f"gkat:{doc_id}:{cats[i]}"}]
        if i + 1 < len(cats):
            row.append({"text": _CAT_SHORT.get(cats[i+1], cats[i+1]),
                        "callback_data": f"gkat:{doc_id}:{cats[i+1]}"})
        rows.append(row)
    return text, {"inline_keyboard": rows}


def format_guided_step_adr(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Schritt 2: Adressat wählen."""
    kat_label = _CAT_SHORT.get(meta.get("kategorie_id", ""), meta.get("kategorie_id", "?"))
    text = (
        f"⚠️ <b>Geführte Klassifikation</b>\n"
        f"{'─' * 32}\n"
        f"📁 Kategorie: <b>{kat_label}</b> ✓\n\n"
        f"<b>② Adressat wählen:</b>"
    )
    keyboard = {"inline_keyboard": [
        [{"text": a, "callback_data": f"gadr:{doc_id}:{a}"} for a in ADRESSATEN],
        [{"text": "← Kategorie ändern", "callback_data": f"gedit:{doc_id}:kat"}],
    ]}
    return text, keyboard


def format_guided_step_abs(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Schritt 3: Absender bestätigen oder neu eingeben."""
    kat_label = _CAT_SHORT.get(meta.get("kategorie_id", ""), meta.get("kategorie_id", "?"))
    adr = meta.get("adressat", "?")
    vorschlag = meta.get("absender") or ""
    text = (
        f"⚠️ <b>Geführte Klassifikation</b>\n"
        f"{'─' * 32}\n"
        f"📁 Kategorie: <b>{kat_label}</b> ✓\n"
        f"👤 Adressat: <b>{adr}</b> ✓\n\n"
        f"<b>③ Absender bestätigen:</b>\n"
        f"Mein Vorschlag: <code>{vorschlag or '?'}</code>"
    )
    rows = []
    if vorschlag:
        rows.append([{"text": f"✓ {vorschlag[:30]}", "callback_data": f"gabs:{doc_id}:__ok__"}])
    rows.append([{"text": "✏️ Anderen Absender eingeben", "callback_data": f"gabsneu:{doc_id}"}])
    rows.append([{"text": "← Adressat ändern", "callback_data": f"gedit:{doc_id}:adr"}])
    return text, {"inline_keyboard": rows}


def format_guided_summary(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Abschluss: Zusammenfassung + Senden."""
    kat_label = _CAT_SHORT.get(meta.get("kategorie_id", ""), meta.get("kategorie_id", "?"))
    text = (
        f"✅ <b>Klassifikation abgeschlossen</b>\n"
        f"{'─' * 32}\n"
        f"📄 <code>{meta.get('dateiname', '?')}</code>\n"
        f"📅 {meta.get('datum') or '?'}\n"
        f"📁 Kategorie: <b>{kat_label}</b>\n"
        f"👤 Adressat: <b>{meta.get('adressat') or '?'}</b>\n"
        f"📤 Absender: <b>{meta.get('absender') or '?'}</b>\n"
        f"{'─' * 32}\n"
        f"📝 {meta.get('beschreibung') or '–'}"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Jetzt senden",   "callback_data": f"gfin:{doc_id}"},
         {"text": "🗑️ Ablehnen",       "callback_data": f"reject:{doc_id}"}],
        [{"text": "← Absender ändern", "callback_data": f"gedit:{doc_id}:abs"},
         {"text": "← Adressat ändern", "callback_data": f"gedit:{doc_id}:adr"}],
    ]}
    return text, keyboard


# ── Dokument-Verarbeitung ──────────────────────────────────────────────────────

def process_pdf(pdf_path: Path) -> bool:
    """Hauptverarbeitungsroutine für eine neue PDF-Datei.

    Returns True wenn die Datei vollständig verarbeitet wurde (kann aus incoming/ gelöscht werden).
    Returns False bei temporären Fehlern (OCR nicht erreichbar) — Datei bleibt für Retry.
    """
    log.info(f"Verarbeite: {pdf_path.name}")

    # Duplikat-Check
    h = pdf_hash(pdf_path)
    with get_db() as con:
        row = con.execute("SELECT id, orig_name, status FROM pending WHERE pdf_hash=?", (h,)).fetchone()
    if row:
        orig_name = row[1] or pdf_path.name
        log.info(f"Duplikat (Hash {h[:8]}…), überspringe.")
        tg_send(
            f"♻️ <b>Duplikat erkannt</b>\n"
            f"<code>{pdf_path.name}</code>\n"
            f"Bereits verarbeitet als:\n<code>{orig_name}</code>"
        )
        return True  # als erledigt markieren — wird aus incoming/ entfernt

    # OCR direkt auf der incoming-Datei (kein frühzeitiges Kopieren nach pending/)
    ocr_text = ocr_pdf(pdf_path)
    if not ocr_text:
        log.warning(f"OCR fehlgeschlagen für {pdf_path.name} — Ryzen nicht erreichbar?")
        tg_send(
            f"⚠️ <b>OCR fehlgeschlagen</b>\n<code>{pdf_path.name}</code>\n"
            "Ryzen nicht erreichbar. Retry beim nächsten Scan-Durchlauf."
        )
        return False  # Datei bleibt in incoming/ für nächsten Versuch

    # Pending-Verzeichnis anlegen — erst jetzt nach erfolgreichem OCR
    pending_subdir = PENDING_DIR / h[:12]
    pending_subdir.mkdir(parents=True, exist_ok=True)
    dest_pdf = pending_subdir / pdf_path.name
    shutil.copy2(pdf_path, dest_pdf)

    # LLM-Metadaten-Extraktion
    meta = extract_metadata(ocr_text)
    if not meta:
        log.warning(f"LLM-Extraktion fehlgeschlagen für {pdf_path.name}")
        meta = {
            "absender": "Unbekannt",
            "datum": None,
            "kategorie_id": "archiv",
            "adressat": "Sonstiges",
            "kurzbezeichnung": "Unbekannt",
            "beschreibung": "Automatische Extraktion fehlgeschlagen.",
        }

    # Sidecar aufbauen
    transfer_at = (datetime.now() + timedelta(minutes=PENDING_MINUTES)).isoformat(timespec="seconds")
    meta["dateiname"] = build_filename(meta)
    sidecar = {
        "version": "2.0",
        "dokument": meta,
        "verarbeitung": {
            "extrahiert_von": "Wilson",
            "extrahiert_am": datetime.now().isoformat(timespec="seconds"),
            "ocr_via": f"dispatcher@{DISPATCHER_URL}",
            "llm_via": f"{OLLAMA_MODEL}@{OLLAMA_URL}",
            "status": "pending",
            "weitergabe_um": transfer_at,
            "user_bestaetigt": False,
            "korrekturen": [],
        },
    }
    sidecar_path = pending_subdir / (dest_pdf.stem + ".meta.json")
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))

    # DB-Eintrag
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO pending(pdf_hash, orig_name, pending_dir, sidecar, status, transfer_at) "
            "VALUES (?,?,?,?,?,?)",
            (h, pdf_path.name, str(pending_subdir), json.dumps(sidecar), "pending", transfer_at),
        )
        doc_id = cur.lastrowid

    # Konfidenz bestimmen — Override macht es immer sicher
    konfidenz = meta.get("konfidenz", "mittel")
    if meta.get("_override_applied"):
        konfidenz = "hoch"

    # Telegram-Benachrichtigung oder geführter Dialog
    if konfidenz == "hoch":
        text, keyboard = format_notification(meta, transfer_at, doc_id)
        msg_id = tg_send(text, keyboard)
        if msg_id:
            with get_db() as con:
                con.execute("UPDATE pending SET tg_msg_id=? WHERE id=?", (msg_id, doc_id))
    else:
        with get_db() as con:
            con.execute("UPDATE pending SET status='guided_kat' WHERE id=?", (doc_id,))
        text, keyboard = format_guided_step_kat(doc_id, meta)
        msg_id = tg_send(text, keyboard)
        if msg_id:
            with get_db() as con:
                con.execute("UPDATE pending SET tg_msg_id=? WHERE id=?", (msg_id, doc_id))

    log.info(f"Dokument {doc_id} in Pending [{konfidenz}]: {meta.get('dateiname')}, Weitergabe um {transfer_at}")
    return True

# ── Weitergabe ─────────────────────────────────────────────────────────────────

def transfer_document(doc_id: int):
    """Verschiebt PDF + Sidecar nach ~/input-dispatcher/."""
    with get_db() as con:
        row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return
    pending_subdir = Path(row["pending_dir"])
    sidecar = json.loads(row["sidecar"])
    dateiname = sidecar["dokument"].get("dateiname") or row["orig_name"]

    # Zieldateinamen sicherstellen (kein Konflikt)
    dest_pdf = OUTPUT_DIR / dateiname
    if dest_pdf.exists():
        stem = dest_pdf.stem
        dest_pdf = OUTPUT_DIR / f"{stem}_{doc_id}.pdf"
    dest_meta = OUTPUT_DIR / (dest_pdf.stem + ".meta.json")

    # PDF umbenennen und verschieben
    orig_pdf = next(pending_subdir.glob("*.pdf"), None)
    if orig_pdf:
        shutil.move(str(orig_pdf), str(dest_pdf))
    # Sidecar aktualisieren und verschieben
    sidecar["verarbeitung"]["status"] = "transferred"
    sidecar["verarbeitung"]["user_bestaetigt"] = True
    dest_meta.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))

    # Pending-Verzeichnis aufräumen
    shutil.rmtree(pending_subdir, ignore_errors=True)

    with get_db() as con:
        con.execute("UPDATE pending SET status='transferred' WHERE id=?", (doc_id,))

    # Telegram: update message
    msg_id = row["tg_msg_id"]
    if msg_id:
        tg_edit(msg_id, f"✅ <b>Weitergegeben</b>\n<code>{dateiname}</code>\n→ Ryzen verarbeitet demnächst.")
    log.info(f"Dokument {doc_id} übergeben: {dest_pdf.name}")

# ── Telegram-Callback-Handler ──────────────────────────────────────────────────

def handle_callback(query: dict):
    """Verarbeitet Inline-Keyboard-Callback-Queries."""
    cb_id = query["id"]
    data  = query.get("data", "")
    parts = data.split(":", 2)
    action = parts[0]

    tg_answer_callback(cb_id)

    if action == "confirm" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            con.execute("UPDATE pending SET status='confirmed' WHERE id=?", (doc_id,))
        transfer_document(doc_id)

    elif action == "reject" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if row:
            shutil.rmtree(Path(row["pending_dir"]), ignore_errors=True)
            with get_db() as con:
                con.execute("UPDATE pending SET status='rejected' WHERE id=?", (doc_id,))
            msg_id = row["tg_msg_id"]
            if msg_id:
                tg_edit(msg_id, f"🗑️ <b>Abgelehnt</b>\n<code>{row['orig_name']}</code>")
        log.info(f"Dokument {doc_id} abgelehnt und gelöscht.")

    elif action == "correct" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM pending WHERE id=?", (doc_id,)).fetchone()
            con.execute("UPDATE pending SET status='correcting', corr_field=NULL WHERE id=?", (doc_id,))
        if row and row["tg_msg_id"]:
            text, kb = format_correction_menu(doc_id)
            tg_edit(row["tg_msg_id"], text, kb)

    elif action == "back" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if row:
            sidecar = json.loads(row["sidecar"])
            meta = sidecar["dokument"]
            transfer_at = sidecar["verarbeitung"]["weitergabe_um"]
            con2 = get_db()
            con2.execute("UPDATE pending SET status='pending', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_notification(meta, transfer_at, doc_id)
            tg_edit(row["tg_msg_id"], text, kb)

    elif action == "field" and len(parts) == 3:
        doc_id = int(parts[1])
        field  = parts[2]
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM pending WHERE id=?", (doc_id,)).fetchone()
            con.execute("UPDATE pending SET corr_field=? WHERE id=?", (field, doc_id))
        if row and row["tg_msg_id"]:
            if field == "kategorie_id":
                text, kb = format_category_keyboard(doc_id)
                tg_edit(row["tg_msg_id"], text, kb)
            elif field == "adressat":
                text, kb = format_adressat_keyboard(doc_id)
                tg_edit(row["tg_msg_id"], text, kb)
            else:
                field_labels = {
                    "absender": "Absender",
                    "datum": "Datum (YYYY-MM-DD)",
                    "kurzbezeichnung": "Kurzbezeichnung (2-3 Wörter mit Bindestrich)",
                    "beschreibung": "Beschreibung (mehrere Sätze)",
                }
                tg_edit(row["tg_msg_id"],
                    f"✏️ <b>{field_labels.get(field, field)} eingeben:</b>\n"
                    f"(Antworte mit dem neuen Wert als Textnachricht)")

    elif action == "setcat" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_cat = parts[2]
        if new_cat in CATEGORIES:
            _update_sidecar_field(doc_id, "kategorie_id", new_cat)

    elif action == "setadr" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_adr = parts[2]
        if new_adr in ADRESSATEN:
            _update_sidecar_field(doc_id, "adressat", new_adr)

    # ── Geführter Dialog ──────────────────────────────────────────────────────

    elif action == "gkat" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_cat = parts[2]
        if new_cat not in CATEGORIES:
            return
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        sidecar["dokument"]["kategorie_id"] = new_cat
        sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
        with get_db() as con:
            con.execute("UPDATE pending SET sidecar=?, status='guided_adr' WHERE id=?",
                        (json.dumps(sidecar), doc_id))
        text, kb = format_guided_step_adr(doc_id, sidecar["dokument"])
        tg_edit(row["tg_msg_id"], text, kb)

    elif action == "gadr" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_adr = parts[2]
        if new_adr not in ADRESSATEN:
            return
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        sidecar["dokument"]["adressat"] = new_adr
        with get_db() as con:
            con.execute("UPDATE pending SET sidecar=?, status='guided_abs' WHERE id=?",
                        (json.dumps(sidecar), doc_id))
        text, kb = format_guided_step_abs(doc_id, sidecar["dokument"])
        tg_edit(row["tg_msg_id"], text, kb)

    elif action == "gabs" and len(parts) == 3:
        doc_id = int(parts[1])
        # __ok__ bedeutet: LLM-Vorschlag übernehmen
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        if parts[2] != "__ok__":
            sidecar["dokument"]["absender"] = parts[2]
            sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
        with get_db() as con:
            con.execute("UPDATE pending SET sidecar=?, status='guided_fin', corr_field=NULL WHERE id=?",
                        (json.dumps(sidecar), doc_id))
        text, kb = format_guided_summary(doc_id, sidecar["dokument"])
        tg_edit(row["tg_msg_id"], text, kb)

    elif action == "gabsneu" and len(parts) == 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM pending WHERE id=?", (doc_id,)).fetchone()
            con.execute("UPDATE pending SET corr_field='new_absender' WHERE id=?", (doc_id,))
        if row and row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"],
                    "✏️ <b>Absender eingeben:</b>\n(Antworte mit dem Namen als Textnachricht)")

    elif action == "gfin" and len(parts) == 2:
        doc_id = int(parts[1])
        with get_db() as con:
            con.execute("UPDATE pending SET status='confirmed' WHERE id=?", (doc_id,))
        transfer_document(doc_id)

    elif action == "gedit" and len(parts) == 3:
        doc_id = int(parts[1])
        step   = parts[2]
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        meta    = sidecar["dokument"]
        if step == "kat":
            con2 = get_db()
            con2.execute("UPDATE pending SET status='guided_kat', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_guided_step_kat(doc_id, meta)
        elif step == "adr":
            con2 = get_db()
            con2.execute("UPDATE pending SET status='guided_adr', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_guided_step_adr(doc_id, meta)
        elif step == "abs":
            con2 = get_db()
            con2.execute("UPDATE pending SET status='guided_abs', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_guided_step_abs(doc_id, meta)
        else:
            return
        tg_edit(row["tg_msg_id"], text, kb)

    # ── Email-Callbacks ───────────────────────────────────────────────────────

    elif action == "econfirm" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            con.execute("UPDATE email_pending SET status='confirmed' WHERE id=?", (email_id,))
        transfer_email_package(email_id)

    elif action == "ereject" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if row:
            shutil.rmtree(Path(row["pending_dir"]), ignore_errors=True)
            with get_db() as con:
                con.execute("UPDATE email_pending SET status='rejected' WHERE id=?", (email_id,))
            msg_id = row["tg_msg_id"]
            if msg_id:
                tg_edit(msg_id, f"🗑️ <b>Email abgelehnt</b>\n<code>{row['subject']}</code>")
        log.info(f"Email {email_id} abgelehnt.")

    elif action == "ecorrect" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
            con.execute("UPDATE email_pending SET status='correcting', corr_field=NULL WHERE id=?", (email_id,))
        if row and row["tg_msg_id"]:
            text, kb = _format_email_correction_menu(email_id)
            tg_edit(row["tg_msg_id"], text, kb)

    elif action == "eback" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if row:
            sidecar = json.loads(row["sidecar"])
            meta = sidecar["dokument"]
            transfer_at = sidecar["verarbeitung"]["weitergabe_um"]
            with get_db() as con:
                con.execute("UPDATE email_pending SET status='pending', corr_field=NULL WHERE id=?", (email_id,))
            text, kb = _format_email_notification(meta, transfer_at, email_id)
            tg_edit(row["tg_msg_id"], text, kb)

    elif action == "efield" and len(parts) == 3:
        email_id = int(parts[1])
        field = parts[2]
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
            con.execute("UPDATE email_pending SET corr_field=? WHERE id=?", (field, email_id))
        if row and row["tg_msg_id"]:
            if field == "kategorie_id":
                text, kb = _format_email_category_keyboard(email_id)
                tg_edit(row["tg_msg_id"], text, kb)
            elif field == "adressat":
                text, kb = _format_email_adressat_keyboard(email_id)
                tg_edit(row["tg_msg_id"], text, kb)
            else:
                field_labels = {"absender": "Absender", "datum": "Datum (YYYY-MM-DD)",
                                "kurzbezeichnung": "Kurzbezeichnung"}
                tg_edit(row["tg_msg_id"],
                    f"✏️ <b>{field_labels.get(field, field)} eingeben: (Email)</b>\n"
                    "(Antworte mit dem neuen Wert als Textnachricht)")

    elif action == "esetkat" and len(parts) == 3:
        email_id = int(parts[1])
        new_cat = parts[2]
        if new_cat in CATEGORIES:
            _update_email_sidecar_field(email_id, "kategorie_id", new_cat)

    elif action == "esetadr" and len(parts) == 3:
        email_id = int(parts[1])
        new_adr = parts[2]
        if new_adr in ADRESSATEN:
            _update_email_sidecar_field(email_id, "adressat", new_adr)

    # ── Absender-Freigabe-Callbacks ───────────────────────────────────────────

    elif action in ("sapprove", "sonce", "sdomain", "sblock") and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if not row:
            return
        from_addr = row["sender"] or ""
        address = _extract_email_address(from_addr)
        display_name = re.sub(r"<.*?>", "", from_addr).strip()
        tg_msg_id = row["tg_msg_id"]

        if action == "sblock":
            _upsert_sender(address, display_name, "blocked")
            pending_dir = row["pending_dir"]
            if pending_dir:
                shutil.rmtree(Path(pending_dir), ignore_errors=True)
            with get_db() as con:
                con.execute("UPDATE email_pending SET status='blocked' WHERE id=?", (email_id,))
            if tg_msg_id:
                tg_edit(tg_msg_id,
                    f"🚫 <b>Absender blockiert</b>\n<code>{html.escape(address)}</code>\n"
                    f"Künftige Emails werden ignoriert.")
            log.info(f"Absender blockiert: {address}")

        elif action == "sapprove":
            _upsert_sender(address, display_name, "approved")
            if tg_msg_id:
                tg_edit(tg_msg_id, f"⏳ <b>Absender genehmigt</b>\n<code>{html.escape(address)}</code>\nVerarbeite Email…")
            raw_data = row["raw_data"]
            if raw_data:
                pending_subdir = Path(row["pending_dir"])
                pending_subdir.mkdir(parents=True, exist_ok=True)
                _do_full_email_processing(email_id, json.loads(raw_data), pending_subdir)
            log.info(f"Absender genehmigt: {address}")

        elif action == "sdomain":
            domain = "@" + address.split("@")[-1] if "@" in address else address
            _upsert_sender(domain, domain, "approved")
            if tg_msg_id:
                tg_edit(tg_msg_id, f"⏳ <b>Domain genehmigt</b>\n<code>{html.escape(domain)}</code>\nVerarbeite Email…")
            raw_data = row["raw_data"]
            if raw_data:
                pending_subdir = Path(row["pending_dir"])
                pending_subdir.mkdir(parents=True, exist_ok=True)
                _do_full_email_processing(email_id, json.loads(raw_data), pending_subdir)
            log.info(f"Domain genehmigt: {domain}")

        elif action == "sonce":
            # Einmalig verarbeiten ohne den Absender dauerhaft zu genehmigen
            if tg_msg_id:
                tg_edit(tg_msg_id, f"⏳ <b>Einmalig verarbeiten</b>\nVerarbeite Email…")
            raw_data = row["raw_data"]
            if raw_data:
                pending_subdir = Path(row["pending_dir"])
                pending_subdir.mkdir(parents=True, exist_ok=True)
                _do_full_email_processing(email_id, json.loads(raw_data), pending_subdir)
            log.info(f"Einmalige Verarbeitung für Absender: {address}")

    # ── Email-Archivierung (Pfad A) ───────────────────────────────────────────

    elif action == "arch" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT thread_id, tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if row and row["thread_id"]:
            try:
                _gog_run("gmail", "thread", "modify", row["thread_id"],
                         "--add", EMAIL_ARCHIVE_LABEL, "--remove", "INBOX", "--force")
                log.info(f"Email {email_id} archiviert via Telegram-Button")
            except Exception as e:
                log.warning(f"Gmail-Archivierung fehlgeschlagen: {e}")
        if row and row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"], "✅ <b>Archiviert</b>")

    elif action == "archedit" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT subject, tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if not row:
            return
        edit_text = f"✏️ <b>Was soll ich tun?</b>\n{html.escape(row['subject'] or '')}"
        edit_kb = {"inline_keyboard": [
            [{"text": "📁 Trotzdem archivieren",  "callback_data": f"arch:{email_id}"}],
            [{"text": "🗑 Vault-Eintrag löschen", "callback_data": f"archundo:{email_id}"}],
            [{"text": "↩️ In Inbox lassen",        "callback_data": f"archinbox:{email_id}"}],
        ]}
        if row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"], edit_text, edit_kb)

    elif action == "archundo" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
            con.execute("UPDATE email_pending SET status='cancelled' WHERE id=?", (email_id,))
        if row and row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"], "🗑 <b>Vault-Eintrag auf 'cancelled' gesetzt, Email bleibt in Inbox</b>")
        log.info(f"Email {email_id} Vault-Eintrag auf cancelled gesetzt")

    elif action == "archinbox" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if row and row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"], "↩️ <b>Email bleibt in Inbox</b>")

    # ── Kategorie-Auswahl für neuen Absender ─────────────────────────────────

    elif action == "ssetcat" and len(parts) >= 3:
        email_id = int(parts[1])
        category_id = parts[2]
        if category_id not in CATEGORIES:
            return
        with get_db() as con:
            row = con.execute("SELECT * FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if not row:
            return
        from_addr = row["sender"] or ""
        address = _extract_email_address(from_addr)
        display_name = _clean_display_name(re.sub(r"<.*?>", "", from_addr).strip())
        tg_msg_id = row["tg_msg_id"]
        # Absender anlegen / Kategorie setzen
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as con:
            con.execute(
                "INSERT INTO email_senders(address, display_name, status, category_id, adressat, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET "
                "status='approved', category_id=excluded.category_id, updated_at=excluded.updated_at",
                (address, display_name, "approved", category_id, "Reinhard", now),
            )
            sender_row = con.execute("SELECT * FROM email_senders WHERE address=?", (address,)).fetchone()
        cat_label = CATEGORIES.get(category_id, category_id)
        if tg_msg_id:
            tg_edit(tg_msg_id,
                    f"⏳ <b>Kategorie gesetzt:</b> {html.escape(cat_label)}\n"
                    f"<code>{html.escape(address)}</code>\nVerarbeite Email…")
        raw_data = row["raw_data"]
        if raw_data:
            pending_subdir = Path(row["pending_dir"])
            pending_subdir.mkdir(parents=True, exist_ok=True)
            _do_full_email_processing(email_id, json.loads(raw_data), pending_subdir)
        # Retro-Import starten
        if sender_row and not sender_row["archive_imported"]:
            with get_db() as con:
                con.execute("UPDATE email_senders SET archive_imported=1 WHERE address=?", (address,))
            threading.Thread(
                target=_retro_import_sender_bg,
                args=(sender_row["id"], address, display_name),
                daemon=True, name=f"retro-{address[:20]}",
            ).start()
        log.info(f"Neuer Absender via Telegram konfiguriert: {address} → {category_id}")

    elif action == "slater" and len(parts) >= 2:
        email_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM email_pending WHERE id=?", (email_id,)).fetchone()
        if row and row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"],
                    f"⏳ <b>Zurückgestellt</b> — Absender in Web-UI konfigurieren:\n"
                    f"<code>http://{SENDER_UI_HOST}:{SENDER_UI_PORT}/</code>")

    # ── Absender-Verwaltung (aus /absender-Befehl) ────────────────────────────

    elif action in ("sabv_app", "sabv_blk", "sabv_del") and len(parts) >= 2:
        sender_id = int(parts[1])
        with get_db() as con:
            srow = con.execute("SELECT * FROM email_senders WHERE id=?", (sender_id,)).fetchone()
        if not srow:
            return
        address = srow["address"]
        if action == "sabv_app":
            _upsert_sender(address, srow["display_name"] or "", "approved")
            tg_answer_callback(cb_id, f"✅ {address} genehmigt")
        elif action == "sabv_blk":
            _upsert_sender(address, srow["display_name"] or "", "blocked")
            tg_answer_callback(cb_id, f"🚫 {address} blockiert")
        elif action == "sabv_del":
            with get_db() as con:
                con.execute("DELETE FROM email_senders WHERE id=?", (sender_id,))
            tg_answer_callback(cb_id, f"🗑️ {address} entfernt")


def _update_sidecar_field(doc_id: int, field: str, value: str):
    """Aktualisiert ein Feld im Sidecar und zeigt aktualisierte Benachrichtigung."""
    with get_db() as con:
        row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return
    sidecar = json.loads(row["sidecar"])
    sidecar["dokument"][field] = value
    # Dateiname neu bauen
    sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
    sidecar["verarbeitung"]["korrekturen"].append({
        "feld": field, "wert": value,
        "am": datetime.now().isoformat(timespec="seconds"),
    })
    # Sidecar-Datei aktualisieren
    pending_subdir = Path(row["pending_dir"])
    for sf in pending_subdir.glob("*.meta.json"):
        sf.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))
    with get_db() as con:
        con.execute(
            "UPDATE pending SET sidecar=?, status='pending', corr_field=NULL WHERE id=?",
            (json.dumps(sidecar), doc_id),
        )
    msg_id = row["tg_msg_id"]
    if msg_id:
        meta = sidecar["dokument"]
        transfer_at = sidecar["verarbeitung"]["weitergabe_um"]
        text, kb = format_notification(meta, transfer_at, doc_id)
        tg_edit(msg_id, text, kb)

def _handle_tg_pdf_upload(doc: dict):
    """Lädt ein per Telegram hochgeladenes PDF herunter und legt es in ~/incoming/."""
    file_id   = doc.get("file_id", "")
    file_name = doc.get("file_name") or f"telegram_{file_id[:8]}.pdf"
    if not file_name.lower().endswith(".pdf"):
        file_name += ".pdf"
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10,
        )
        file_path = r.json().get("result", {}).get("file_path", "")
        if not file_path:
            tg_send("❌ Telegram-Datei nicht abrufbar.")
            return
        dl = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=30,
        )
        dest = INCOMING_DIR / file_name
        counter = 2
        while dest.exists():
            dest = INCOMING_DIR / f"{Path(file_name).stem}_{counter}.pdf"
            counter += 1
        dest.write_bytes(dl.content)
        log.info(f"PDF von Telegram empfangen: {dest.name}")
        tg_send(f"📥 PDF empfangen: <code>{dest.name}</code>\nVerarbeitung startet…")
    except Exception as e:
        log.error(f"PDF-Download fehlgeschlagen: {e}")
        tg_send(f"❌ PDF-Download fehlgeschlagen: {e}")


def handle_text_message(msg: dict):
    """Verarbeitet eingehende Textnachrichten als Korrektureingaben."""
    text = msg.get("text", "").strip()
    if not text:
        return

    if text.startswith("/"):
        _handle_command(text)
        return

def _handle_command(text: str):
    cmd = text.split()[0].lower()

    if cmd in ("/start", "/hilfe", "/help"):
        tg_send(
            "📄 <b>Dispatcher-Bot</b>\n\n"
            "Schick ein PDF → automatische OCR, Kategorisierung und Ablage in Reinhards Vault.\n\n"
            "<b>Befehle:</b>\n"
            "/status — ausstehende Dokumente und Emails\n"
            "/absender — Email-Absender und Erklärung der Status\n"
            "/hilfe — diese Hilfe\n\n"
            "<b>Email-Absender Status:</b>\n"
            "✅ <b>Genehmigt</b> — Emails automatisch verarbeiten und ablegen\n"
            "⏳ <b>Ausstehend</b> — nächste Email triggers Rückfrage\n"
            "🚫 <b>Blockiert</b> — Emails still ignorieren\n\n"
            f"Absender verwalten: <code>http://{SENDER_UI_HOST}:{SENDER_UI_PORT}/</code>"
        )
        return

    if cmd == "/absender":
        with get_db() as con:
            counts = {
                row["status"]: row["n"]
                for row in con.execute(
                    "SELECT status, COUNT(*) AS n FROM email_senders GROUP BY status"
                ).fetchall()
            }
        n_total    = sum(counts.values())
        n_approved = counts.get("approved", 0)
        n_blocked  = counts.get("blocked",  0)
        n_pending  = counts.get("pending",  0)
        tg_send(
            f"📧 <b>Email-Absender</b>\n"
            f"{'─' * 30}\n"
            f"✅ <b>Genehmigt ({n_approved})</b> — Emails werden automatisch verarbeitet und in den Vault abgelegt.\n\n"
            f"⏳ <b>Ausstehend ({n_pending})</b> — Absender bekannt, aber noch nicht entschieden. Beim nächsten Email fragt Wilson nach Freigabe.\n\n"
            f"🚫 <b>Blockiert ({n_blocked})</b> — Emails werden still ignoriert, kein Vault-Eintrag.\n"
            f"{'─' * 30}\n"
            f"Gesamt: {n_total} bekannte Absender\n\n"
            f"Verwalten im Browser:\n"
            f"<code>http://{SENDER_UI_HOST}:{SENDER_UI_PORT}/</code>"
        )
        return

    if cmd == "/status":
        with get_db() as con:
            rows = con.execute(
                "SELECT orig_name, status, created_at FROM pending "
                "WHERE status NOT IN ('transferred','rejected') ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            email_rows = con.execute(
                "SELECT subject, sender, status, created_at FROM email_pending "
                "WHERE status NOT IN ('transferred','rejected') ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        if not rows and not email_rows:
            tg_send("✅ Keine ausstehenden Dokumente oder Emails.")
            return
        lines = []
        status_icon = {"pending": "⏳", "correcting": "✏️", "guided_kat": "🗂️",
                       "guided_adr": "👤", "guided_abs": "🏢", "guided_fin": "✅"}
        if rows:
            lines.append(f"📋 <b>Ausstehende Dokumente ({len(rows)})</b>")
            for r in rows:
                icon = status_icon.get(r["status"], "❓")
                lines.append(f"{icon} <b>{html.escape(r['orig_name'])}</b>\n   {r['status']} · {r['created_at']}")
        if email_rows:
            lines.append(f"\n📧 <b>Ausstehende Emails ({len(email_rows)})</b>")
            for r in email_rows:
                icon = status_icon.get(r["status"], "❓")
                lines.append(f"{icon} <b>{html.escape(r['subject'] or '?')}</b>\n   {r['status']} · {r['created_at']}")
        tg_send("\n\n".join(lines))
        return

    with get_db() as con:
        # Geführter Dialog: Absender-Texteingabe
        guided_row = con.execute(
            "SELECT * FROM pending WHERE status='guided_abs' AND corr_field='new_absender' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if guided_row:
            doc_id  = guided_row["id"]
            sidecar = json.loads(guided_row["sidecar"])
            sidecar["dokument"]["absender"] = text
            sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
            con.execute(
                "UPDATE pending SET sidecar=?, status='guided_fin', corr_field=NULL WHERE id=?",
                (json.dumps(sidecar), doc_id),
            )
            summary_text, kb = format_guided_summary(doc_id, sidecar["dokument"])
            tg_edit(guided_row["tg_msg_id"], summary_text, kb)
            return

        # Normaler Korrekturmodus
        row = con.execute(
            "SELECT * FROM pending WHERE status='correcting' AND corr_field IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row:
        field = row["corr_field"]
        if field in ("absender", "datum", "kurzbezeichnung", "beschreibung"):
            _update_sidecar_field(row["id"], field, text)
        return

    # Email-Korrekturmodus
    with get_db() as con:
        email_row = con.execute(
            "SELECT * FROM email_pending WHERE status='correcting' AND corr_field IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if email_row:
        field = email_row["corr_field"]
        if field in ("absender", "datum", "kurzbezeichnung"):
            _update_email_sidecar_field(email_row["id"], field, text)

# ── Email-Verarbeitung ────────────────────────────────────────────────────────

def _gog_run(*args, timeout: int = 90) -> dict:
    """Führt einen gog-Befehl aus und gibt das JSON-Ergebnis zurück."""
    env = os.environ.copy()
    env["GOG_ACCOUNT"] = GOG_ACCOUNT
    try:
        result = subprocess.run(
            [GOG_BIN, *args, "--json", "--no-input"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            log.error(f"gog Fehler: {result.stderr[:300]}")
            return {}
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except subprocess.TimeoutExpired:
        log.error(f"gog Timeout: {' '.join(args[:3])}")
        return {}
    except Exception as e:
        log.error(f"gog Subprocess-Fehler: {e}")
        return {}


def _gog_download_attachment(msg_id: str, att_id: str, dest_dir: Path, filename: str) -> bool:
    """Lädt einen Email-Anhang herunter. Gibt True bei Erfolg zurück."""
    env = os.environ.copy()
    env["GOG_ACCOUNT"] = GOG_ACCOUNT
    dest_file = dest_dir / filename
    try:
        result = subprocess.run(
            [GOG_BIN, "gmail", "attachment", msg_id, att_id,
             "--out", str(dest_dir), "--name", filename, "--no-input"],
            capture_output=True, timeout=120, env=env,
        )
        if result.returncode == 0 and dest_file.exists() and dest_file.stat().st_size > 0:
            return True
        log.error(f"Anhang-Download fehlgeschlagen: {filename} — {result.stderr[:200]}")
        return False
    except Exception as e:
        log.error(f"Anhang-Download Exception: {e}")
        return False


def _email_extract_body(payload: dict) -> tuple[str, str]:
    """Extrahiert HTML- und Plaintext-Body rekursiv aus Gmail-Payload. Gibt (html, plain) zurück."""
    html_body = ""
    plain_body = ""

    def _walk(part):
        nonlocal html_body, plain_body
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")
        if mime == "text/html" and data and not html_body:
            html_body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif mime == "text/plain" and data and not plain_body:
            plain_body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(payload)
    return html_body, plain_body


def _email_to_markdown(html_body: str, plain_body: str) -> str:
    """Konvertiert Email-Body nach Markdown. html2text wenn verfügbar, sonst plain text."""
    md = ""
    if html_body:
        try:
            import html2text as _h2t
            h = _h2t.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            h.protect_links = False
            md = h.handle(html_body)
        except ImportError:
            md = plain_body
    else:
        md = plain_body
    md = re.sub(r"\n{3,}", "\n\n", md or "")
    return md.strip()


def _email_extract_pdf_attachments(msg_id: str, payload: dict, dest_dir: Path) -> list[str]:
    """Lädt PDF-Anhänge aus einer Gmail-Nachricht herunter. Gibt Liste der Dateinamen zurück."""
    filenames = []

    def _walk(part):
        fname = part.get("filename", "")
        att_id = part.get("body", {}).get("attachmentId", "")
        if fname and att_id and fname.lower().endswith(".pdf"):
            safe_name = re.sub(r"[^\w\-\.]", "_", fname)
            if _gog_download_attachment(msg_id, att_id, dest_dir, safe_name):
                filenames.append(safe_name)
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(payload)
    return filenames


def _build_email_classification_prompt(cat_list: str, from_addr: str, subject: str, body: str) -> str:
    return f"""Du bist ein Dokumenten-Assistent. Analysiere die folgende Email und antworte ausschließlich mit einem JSON-Objekt — kein Text davor oder danach.

GÜLTIGE KATEGORIEN (kategorie_id → Beschreibung):
{cat_list}

KATEGORIE-REGELN:
1. krankenversicherung — Emails von Krankenkassen oder Ärzten
2. versicherung — Sachversicherungen, KFZ-Versicherung
3. immobilien_eigen — Emails zu eigenen Immobilien (Seggiano, Podere dei Venti, Übersee, Grassauer Straße)
4. immobilien_vermietet — Mieter-Emails (Lipowskystraße, Kornstraße, Kolberger, Schießhausstraße, Schechen)
5. finanzen — Bankemails, Rechnungen, Steuern
6. archiv — LETZTER AUSWEG

ABSENDER → ADRESSAT-REGELN:
- HUK / HUK-COBURG → adressat="Marion"
- Gothaer / Barmenia / vigo → adressat="Reinhard"
- Standard: adressat="Reinhard"

EMAIL-HEADER:
Von: {from_addr}
Betreff: {subject}

EMAIL-INHALT (gekürzt):
---
{body[:3000]}
---

EXTRAHIERE:
{{
  "absender": "Firmen- oder Personenname, kurz und präzise (ohne E-Mail-Domain)",
  "datum": "Datum der Email als YYYY-MM-DD",
  "kategorie_id": "eine der obigen kategorie_id-Werte",
  "adressat": "Reinhard" oder "Marion" oder "Linoa" oder "Sonstiges",
  "kurzbezeichnung": "2-4 Wörter mit Bindestrichen, Thema der Email",
  "beschreibung": "3-5 vollständige Sätze auf Deutsch: Worum geht es in der Email?",
  "konfidenz": "hoch" | "mittel" | "niedrig"
}}"""


def _classify_email(from_addr: str, subject: str, body_md: str) -> dict | None:
    """LLM-Klassifikation für Emails. Gleiche Pipeline wie PDFs (DeepSeek → Ollama)."""
    cat_list = "\n".join(f"  {k}: {v}" for k, v in CATEGORIES.items())
    email_text = f"Von: {from_addr}\nBetreff: {subject}\n\n{body_md}"
    prompt = _build_email_classification_prompt(cat_list, from_addr, subject, body_md)

    if DEEPSEEK_KEY:
        result = _run_llm_extraction(
            email_text,
            url=DEEPSEEK_URL,
            payload_fn=lambda _: {
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            content_fn=lambda r: r.json()["choices"][0]["message"]["content"],
            label=f"DeepSeek ({DEEPSEEK_MODEL}) [email]",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        )
    else:
        result = _run_llm_extraction(
            email_text,
            url=f"{OLLAMA_URL}/api/chat",
            payload_fn=lambda _: {
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_ctx": 8192, "temperature": 0.1},
            },
            content_fn=lambda r: r.json()["message"]["content"],
            label=f"Ollama ({OLLAMA_MODEL}) [email]",
            headers={},
        )
    return result


def _build_email_filename(meta: dict, suffix: str = ".md") -> str:
    datum = meta.get("datum", "")
    datum_clean = datum.replace("-", "")[:8] if datum else "00000000"
    absender = normalize_filename(meta.get("absender", "Unbekannt"))
    kurz = normalize_filename(meta.get("kurzbezeichnung", ""))
    parts = [p for p in [datum_clean, absender, kurz] if p]
    return "_".join(parts) + suffix


def _format_email_notification(meta: dict, transfer_at: str, email_id: int) -> tuple[str, dict]:
    """Erstellt Telegram-Benachrichtigung für eine Email."""
    transfer_dt = datetime.fromisoformat(transfer_at)
    minuten = max(0, int((transfer_dt - datetime.now()).total_seconds() / 60))
    n_anlagen = len(meta.get("anlagen", []))
    anlagen_str = f"\n📎 <b>Anhänge:</b>  {n_anlagen} PDF(s)" if n_anlagen else ""

    text = (
        f"📧 <b>Neue Email erkannt</b>\n"
        f"{'─' * 35}\n"
        f"✉️ <b>Von:</b>      {html.escape(meta.get('von', '?'))}\n"
        f"📋 <b>Betreff:</b>  {html.escape(meta.get('betreff', '?'))}\n"
        f"📅 <b>Datum:</b>    {meta.get('datum', '?')}\n"
        f"{'─' * 35}\n"
        f"🏢 <b>Absender:</b> {html.escape(meta.get('absender', '?'))}\n"
        f"📁 <b>Kategorie:</b> {CATEGORIES.get(meta.get('kategorie_id', ''), meta.get('kategorie_id', '?'))}\n"
        f"👤 <b>Adressat:</b> {meta.get('adressat', '?')}"
        f"{anlagen_str}\n"
        f"{'─' * 35}\n"
        f"📝 <b>Inhalt:</b>\n{meta.get('beschreibung', '–')}\n"
        f"{'─' * 35}\n"
        f"⏱️ Weitergabe in {minuten} Min."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Jetzt senden",  "callback_data": f"econfirm:{email_id}"},
        {"text": "✏️ Korrigieren",   "callback_data": f"ecorrect:{email_id}"},
        {"text": "🗑️ Ablehnen",      "callback_data": f"ereject:{email_id}"},
    ]]}
    return text, keyboard


def _format_email_correction_menu(email_id: int) -> tuple[str, dict]:
    text = "✏️ <b>Was soll geändert werden? (Email)</b>"
    keyboard = {"inline_keyboard": [
        [
            {"text": "Absender",   "callback_data": f"efield:{email_id}:absender"},
            {"text": "Kategorie",  "callback_data": f"efield:{email_id}:kategorie_id"},
            {"text": "Adressat",   "callback_data": f"efield:{email_id}:adressat"},
        ],
        [{"text": "← Zurück", "callback_data": f"eback:{email_id}"}],
    ]}
    return text, keyboard


def _format_email_category_keyboard(email_id: int) -> tuple[str, dict]:
    text = "📁 <b>Kategorie wählen: (Email)</b>"
    cats = list(CATEGORIES.keys())
    rows = []
    for i in range(0, len(cats), 2):
        row = [{"text": _CAT_SHORT.get(cats[i], cats[i]), "callback_data": f"esetkat:{email_id}:{cats[i]}"}]
        if i + 1 < len(cats):
            row.append({"text": _CAT_SHORT.get(cats[i+1], cats[i+1]), "callback_data": f"esetkat:{email_id}:{cats[i+1]}"})
        rows.append(row)
    rows.append([{"text": "← Zurück", "callback_data": f"ecorrect:{email_id}"}])
    return text, {"inline_keyboard": rows}


def _format_email_adressat_keyboard(email_id: int) -> tuple[str, dict]:
    text = "👤 <b>Adressat wählen: (Email)</b>"
    keyboard = {"inline_keyboard": [
        [{"text": a, "callback_data": f"esetadr:{email_id}:{a}"} for a in ADRESSATEN],
        [{"text": "← Zurück", "callback_data": f"ecorrect:{email_id}"}],
    ]}
    return text, keyboard


def _update_email_sidecar_field(email_id: int, field: str, value: str):
    """Aktualisiert ein Feld im Email-Sidecar und zeigt aktualisierte Benachrichtigung."""
    with get_db() as con:
        row = con.execute("SELECT * FROM email_pending WHERE id=?", (email_id,)).fetchone()
    if not row:
        return
    sidecar = json.loads(row["sidecar"])
    sidecar["dokument"][field] = value
    if field in ("absender", "datum", "kurzbezeichnung"):
        sidecar["dokument"]["dateiname"] = _build_email_filename(sidecar["dokument"])
        for pdf_name in sidecar["dokument"].get("anlagen", []):
            anlage_stem = Path(pdf_name).stem
            anlage_sidecar_path = Path(row["pending_dir"]) / (anlage_stem + ".meta.json")
            if anlage_sidecar_path.exists():
                try:
                    asc = json.loads(anlage_sidecar_path.read_text())
                    asc["dokument"][field] = value
                    anlage_sidecar_path.write_text(json.dumps(asc, ensure_ascii=False, indent=2))
                except Exception:
                    pass
    with get_db() as con:
        con.execute(
            "UPDATE email_pending SET sidecar=?, status='pending', corr_field=NULL WHERE id=?",
            (json.dumps(sidecar), email_id),
        )
    msg_id = row["tg_msg_id"]
    if msg_id:
        meta = sidecar["dokument"]
        transfer_at = sidecar["verarbeitung"]["weitergabe_um"]
        text, kb = _format_email_notification(meta, transfer_at, email_id)
        tg_edit(msg_id, text, kb)


def transfer_email_package(email_id: int):
    """Überträgt Email-MD + PDF-Anhänge nach ~/input-dispatcher/."""
    with get_db() as con:
        row = con.execute("SELECT * FROM email_pending WHERE id=?", (email_id,)).fetchone()
    if not row:
        return
    pending_dir = Path(row["pending_dir"])
    sidecar = json.loads(row["sidecar"])
    dok = sidecar["dokument"]
    email_md_filename = dok.get("dateiname", "email.md")
    email_stem = Path(email_md_filename).stem

    # 1. PDF-Anhänge + ihre Sidecars zuerst
    transferred_pdfs = []
    for pdf_orig in dok.get("anlagen", []):
        pdf_path = pending_dir / pdf_orig
        if not pdf_path.exists():
            log.warning(f"Anhang nicht gefunden: {pdf_path}")
            continue
        anlage_sidecar_path = pending_dir / (pdf_path.stem + ".meta.json")

        dest_pdf = OUTPUT_DIR / pdf_path.name
        dest_meta = OUTPUT_DIR / (pdf_path.stem + ".meta.json")
        # Konflikt-Vermeidung
        counter = 2
        while dest_pdf.exists():
            dest_pdf = OUTPUT_DIR / f"{pdf_path.stem}_{counter}.pdf"
            dest_meta = OUTPUT_DIR / f"{pdf_path.stem}_{counter}.meta.json"
            counter += 1

        shutil.copy2(str(pdf_path), str(dest_pdf))
        if anlage_sidecar_path.exists():
            shutil.copy2(str(anlage_sidecar_path), str(dest_meta))
        transferred_pdfs.append(dest_pdf.name)
        log.info(f"Email-Anhang übertragen: {dest_pdf.name}")

    # 2. Kurz warten damit Syncthing-Propagation startet
    if transferred_pdfs:
        time.sleep(3)

    # 3. Email-MD + Email-Sidecar zuletzt (Dispatcher-Trigger)
    email_md_src = pending_dir / "email.md"
    dest_md = OUTPUT_DIR / email_md_filename
    dest_md_meta = OUTPUT_DIR / (email_stem + ".meta.json")
    counter = 2
    while dest_md.exists():
        dest_md = OUTPUT_DIR / f"{email_stem}_{counter}.md"
        dest_md_meta = OUTPUT_DIR / f"{email_stem}_{counter}.meta.json"
        counter += 1

    if email_md_src.exists():
        shutil.copy2(str(email_md_src), str(dest_md))

    sidecar["verarbeitung"]["status"] = "transferred"
    sidecar["verarbeitung"]["user_bestaetigt"] = True
    sidecar["dokument"]["dateiname"] = dest_md.name
    dest_md_meta.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))

    # 4. DB + cleanup
    with get_db() as con:
        con.execute("UPDATE email_pending SET status='transferred' WHERE id=?", (email_id,))
    shutil.rmtree(pending_dir, ignore_errors=True)

    # 5. Gmail-Thread archivieren (INBOX-Label entfernen)
    thread_id = row["thread_id"]
    if thread_id:
        try:
            _gog_run("gmail", "thread", "modify", thread_id,
                     "--add", EMAIL_ARCHIVE_LABEL, "--remove", "INBOX", "--force")
            log.info(f"Gmail-Thread {thread_id[:12]}… archiviert")
        except Exception as e:
            log.warning(f"Gmail-Archivierung fehlgeschlagen: {e}")

    # 6. Telegram: Update
    msg_id = row["tg_msg_id"]
    if msg_id:
        n_anlagen = len(transferred_pdfs)
        anlg = f" + {n_anlagen} Anhang/Anhänge" if n_anlagen else ""
        tg_edit(msg_id, f"✅ <b>Email weitergegeben</b>\n<code>{dest_md.name}</code>{anlg}\n→ Ryzen verarbeitet demnächst.")
    log.info(f"Email {email_id} übergeben: {dest_md.name} + {len(transferred_pdfs)} PDF(s)")


# ── Absender-Verwaltung ───────────────────────────────────────────────────────

def _seed_senders_from_history():
    """Importiert alle bisher verarbeiteten Email-Absender als 'pending' in email_senders.
    Einmalig beim Start — füllt die Tabelle aus email_pending-History (INSERT OR IGNORE)."""
    with get_db() as con:
        rows = con.execute(
            "SELECT DISTINCT sender FROM email_pending WHERE sender IS NOT NULL AND sender != ''"
        ).fetchall()
        count = 0
        for row in rows:
            from_addr = row["sender"]
            m = re.search(r"<([^>]+)>", from_addr)
            address = (m.group(1).strip() if m else from_addr.strip()).lower()
            display_name = _clean_display_name(re.sub(r"<.*?>", "", from_addr).strip())
            if address:
                con.execute(
                    "INSERT OR IGNORE INTO email_senders(address, display_name, status) "
                    "VALUES (?, ?, 'pending')",
                    (address, display_name),
                )
                count += 1
    if count:
        log.info(f"Absender-Tabelle: {count} historische Absender als 'pending' importiert")


def _extract_email_address(from_str: str) -> str:
    """Extrahiert die reine Email-Adresse aus 'Name <email@domain.com>'."""
    m = re.search(r"<([^>]+)>", from_str)
    addr = m.group(1).strip() if m else from_str.strip()
    return addr.lower()


def _get_sender_status(address: str) -> str:
    """Gibt den Status des Absenders zurück: approved|blocked|pending.
    Prüft zuerst exakte Adresse, dann Domain-Eintrag (@domain.com)."""
    domain = "@" + address.split("@")[-1] if "@" in address else ""
    with get_db() as con:
        row = con.execute(
            "SELECT status FROM email_senders WHERE address IN (?, ?) "
            "ORDER BY CASE address WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (address, domain, address),
        ).fetchone()
    return row["status"] if row else "pending"


def _get_sender_row(address: str) -> sqlite3.Row | None:
    """Gibt die vollständige email_senders-Zeile zurück (Adresse oder Domain-Eintrag)."""
    domain = "@" + address.split("@")[-1] if "@" in address else ""
    with get_db() as con:
        return con.execute(
            "SELECT * FROM email_senders WHERE address IN (?,?) "
            "ORDER BY CASE address WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (address, domain, address),
        ).fetchone()


def _create_reminder(date: str, text: str):
    """Legt eine Erinnerung in der DB an. Wird von timer_thread täglich geprüft."""
    with get_db() as con:
        con.execute("INSERT INTO reminders(reminder_date, text) VALUES (?,?)", (date, text))
    log.info(f"Erinnerung angelegt: {date} — {text[:60]}")


def _extract_delivery_date(subject: str, body_md: str) -> str | None:
    """Extrahiert das Lieferdatum aus einer Zustellungsbenachrichtigung via LLM."""
    prompt = (
        f"Extrahiere das Lieferdatum aus dieser Zustellungsbenachrichtigung. "
        f"Antworte NUR mit dem Datum im Format YYYY-MM-DD oder 'unbekannt'.\n"
        f"Betreff: {subject}\n\n{body_md[:500]}"
    )
    content = ""
    try:
        if DEEPSEEK_KEY:
            r = requests.post(
                DEEPSEEK_URL,
                json={"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.1, "max_tokens": 64},
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                timeout=30,
            )
            content = r.json()["choices"][0]["message"]["content"]
        else:
            r = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "options": {"num_ctx": 2048, "temperature": 0.1}},
                timeout=30,
            )
            content = r.json()["message"]["content"]
    except Exception as e:
        log.warning(f"Lieferdatum-Extraktion fehlgeschlagen: {e}")
    m = re.search(r"\d{4}-\d{2}-\d{2}", content or "")
    return m.group(0) if m else None


def _handle_zustellung(meta: dict, subject: str, body_md: str):
    """Verarbeitet Zustellungsbenachrichtigungen: Erinnerung anlegen, kein Vault."""
    delivery_date = _extract_delivery_date(subject, body_md)
    today = datetime.now().strftime("%Y-%m-%d")
    name = html.escape(meta.get("absender", "Unbekannt"))
    sub = html.escape(subject)
    if delivery_date == today:
        tg_send(f"📦 <b>Lieferung heute!</b>\nVon: {name}\nBetreff: {sub}")
    elif delivery_date:
        _create_reminder(delivery_date, f"📦 Lieferung von {name}\nBetreff: {sub}")
        tg_send(f"📦 <b>Zustellungsbenachrichtigung</b>\nVon: {name}\nLieferdatum: {delivery_date}\nErinnerung angelegt.")
    else:
        tg_send(f"📦 <b>Zustellungsbenachrichtigung</b> (kein Datum erkannt)\nVon: {name}\nBetreff: {sub}")


def _clean_display_name(name: str) -> str:
    return name.replace('"', '').strip()


def _extract_contact_from_signature(body_md: str) -> dict:
    """Extrahiert Kontaktdaten aus dem Signatur-Bereich einer Email via LLM.
    Gibt dict mit phone/postal/website/notes zurück (fehlende Felder fehlen im dict)."""
    lines = body_md.strip().splitlines()
    sig_area = "\n".join(lines[-35:]) if len(lines) > 35 else body_md
    prompt = (
        "Extrahiere Kontaktdaten aus diesem Email-Signatur-Bereich. "
        "Antworte NUR mit einem JSON-Objekt, kein Text davor oder danach.\n\n"
        f"---\n{sig_area}\n---\n\n"
        '{"phone": "Telefonnummer oder null", '
        '"postal": "Vollständige Postadresse einzeilig oder null", '
        '"website": "Website-URL oder null", '
        '"notes": "Kurze Anmerkung (z.B. Funktion, Firma) oder null"}'
    )
    content = ""
    try:
        if DEEPSEEK_KEY:
            r = requests.post(
                DEEPSEEK_URL,
                json={"model": DEEPSEEK_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0, "max_tokens": 200},
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                         "Content-Type": "application/json"},
                timeout=30,
            )
            content = r.json()["choices"][0]["message"]["content"]
        else:
            r = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": OLLAMA_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "stream": False,
                      "options": {"num_ctx": 2048, "temperature": 0}},
                timeout=30,
            )
            content = r.json()["message"]["content"]
        m = re.search(r"\{[\s\S]*\}", content or "")
        if not m:
            return {}
        raw = json.loads(m.group())
        return {k: v for k, v in raw.items() if v and v != "null" and k in ("phone", "postal", "website", "notes")}
    except Exception as e:
        log.warning(f"Signatur-Extraktion fehlgeschlagen: {e}")
        return {}


def _update_sender_contact_bg(sender_id: int, body_md: str):
    """Hintergrund-Task: Kontaktdaten aus Signatur extrahieren und leere Felder befüllen.
    Überschreibt nur NULL-Felder — manuell gepflegte Daten bleiben erhalten.
    Läuft max. alle 90 Tage pro Absender."""
    with get_db() as con:
        row = con.execute(
            "SELECT phone, postal, website, notes, contact_updated FROM email_senders WHERE id=?",
            (sender_id,)
        ).fetchone()
    if not row:
        return
    # Prüfen ob Update nötig: alle Felder bereits befüllt ODER letztes Update < 90 Tage
    all_filled = all(row[f] for f in ("phone", "postal", "website", "notes"))
    if all_filled:
        return
    if row["contact_updated"]:
        try:
            last = datetime.fromisoformat(row["contact_updated"])
            if (datetime.now() - last).days < 90:
                return
        except Exception:
            pass
    contact = _extract_contact_from_signature(body_md)
    if not contact:
        return
    # Nur NULL-Felder aktualisieren
    sets, params = [], []
    for field in ("phone", "postal", "website", "notes"):
        if field in contact and not row[field]:
            sets.append(f"{field}=?")
            params.append(contact[field])
    if not sets:
        return
    sets.append("contact_updated=?")
    params.append(datetime.now().isoformat(timespec="seconds"))
    params.append(sender_id)
    with get_db() as con:
        con.execute(f"UPDATE email_senders SET {','.join(sets)} WHERE id=?", params)
    log.info(f"Signatur-Kontakt aktualisiert für Absender {sender_id}: {[s.split('=')[0] for s in sets[:-1]]}")


def _upsert_sender(address: str, display_name: str, status: str):
    """Legt Absender an oder aktualisiert seinen Status."""
    display_name = _clean_display_name(display_name)
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as con:
        con.execute(
            "INSERT INTO email_senders(address, display_name, status, first_seen, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "display_name=excluded.display_name, status=excluded.status, updated_at=excluded.updated_at",
            (address, display_name, status, now, now),
        )


def _retro_import_sender_bg(sender_id: int, address: str, display_name: str):
    """Importiert alle historischen Emails eines Absenders in den Vault.
    Läuft im Hintergrund-Thread. Kein Telegram pro Email (silent), nur Abschluss-Meldung."""
    from email.utils import parsedate_to_datetime as _parsedate
    log.info(f"Retro-Import startet: {address}")
    try:
        data = _gog_run("gmail", "messages", "search", f"from:{address}", "--all", timeout=600)
        messages = data.get("messages", [])
        seen_threads: set = set()
        to_fetch: list = []
        for msg in messages:
            tid = msg.get("threadId") or msg.get("id", "")
            if tid not in seen_threads:
                seen_threads.add(tid)
                to_fetch.append(msg["id"])
        log.info(f"Retro-Import {address}: {len(messages)} Nachrichten, {len(to_fetch)} Threads")
        with get_db() as con:
            known = {row[0] for row in con.execute("SELECT message_id FROM email_pending").fetchall()}
        imported = 0
        skipped = 0
        for msg_id in to_fetch:
            if msg_id in known:
                skipped += 1
                continue
            try:
                full = _gog_run("gmail", "get", msg_id, timeout=30)
                message = full.get("message", {})
                if not message:
                    skipped += 1
                    continue
                payload = message.get("payload", {})
                hdrs = {h.get("name", "").lower(): h.get("value", "")
                        for h in payload.get("headers", [])}
                date_str = hdrs.get("date", "")
                try:
                    received_at = _parsedate(date_str).strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    received_at = datetime.now().isoformat(timespec="seconds")
                email_data = {
                    "message_id":  msg_id,
                    "thread_id":   message.get("threadId", ""),
                    "subject":     hdrs.get("subject", "(kein Betreff)"),
                    "from":        hdrs.get("from", ""),
                    "date":        date_str,
                    "received_at": received_at,
                    "payload":     payload,
                }
                pending_subdir = PENDING_DIR / f"email-{msg_id[:12]}"
                pending_subdir.mkdir(parents=True, exist_ok=True)
                with get_db() as con:
                    try:
                        cur = con.execute(
                            "INSERT INTO email_pending"
                            "(message_id, thread_id, subject, sender, received_at, pending_dir, raw_data, status) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (msg_id, email_data["thread_id"], email_data["subject"],
                             email_data["from"], received_at,
                             str(pending_subdir), json.dumps(email_data), "approved"),
                        )
                        email_id = cur.lastrowid
                    except sqlite3.IntegrityError:
                        shutil.rmtree(pending_subdir, ignore_errors=True)
                        skipped += 1
                        continue
                _do_full_email_processing(email_id, email_data, pending_subdir, silent=True)
                imported += 1
                time.sleep(0.3)
            except Exception as e:
                log.error(f"Retro-Import Fehler für {msg_id}: {e}")
                skipped += 1
        with get_db() as con:
            con.execute("UPDATE email_senders SET archive_imported=1 WHERE id=?", (sender_id,))
        tg_send(
            f"📥 <b>Retro-Import abgeschlossen</b>\n"
            f"Absender: {html.escape(display_name or address)}\n"
            f"Importiert: {imported}, Übersprungen: {skipped}"
        )
        log.info(f"Retro-Import {address} fertig: {imported} importiert, {skipped} übersprungen")
    except Exception as e:
        log.error(f"Retro-Import {address} fehlgeschlagen: {e}")
        tg_send(f"❌ <b>Retro-Import fehlgeschlagen</b>\n{html.escape(address)}\n{html.escape(str(e))}")


def _dispatch_email_to_output(email_id: int, sidecar: dict, pending_dir: Path):
    """Kopiert Email-Paket direkt in ~/input-dispatcher/ ohne Telegram-Review oder Gmail-Archivierung.
    Verwendet für Pfad A (Kategorie-Override): sofortiger Dispatch, Archivierung per Telegram-Button."""
    dok = sidecar["dokument"]
    email_md_filename = dok.get("dateiname", "email.md")
    email_stem = Path(email_md_filename).stem

    for pdf_orig in dok.get("anlagen", []):
        pdf_path = pending_dir / pdf_orig
        if not pdf_path.exists():
            continue
        anlage_sidecar_path = pending_dir / (pdf_path.stem + ".meta.json")
        dest_pdf = OUTPUT_DIR / pdf_path.name
        dest_meta = OUTPUT_DIR / (pdf_path.stem + ".meta.json")
        counter = 2
        while dest_pdf.exists():
            dest_pdf = OUTPUT_DIR / f"{pdf_path.stem}_{counter}.pdf"
            dest_meta = OUTPUT_DIR / f"{pdf_path.stem}_{counter}.meta.json"
            counter += 1
        shutil.copy2(str(pdf_path), str(dest_pdf))
        if anlage_sidecar_path.exists():
            shutil.copy2(str(anlage_sidecar_path), str(dest_meta))

    if dok.get("anlagen"):
        time.sleep(3)

    email_md_src = pending_dir / "email.md"
    dest_md = OUTPUT_DIR / email_md_filename
    dest_md_meta = OUTPUT_DIR / (email_stem + ".meta.json")
    counter = 2
    while dest_md.exists():
        dest_md = OUTPUT_DIR / f"{email_stem}_{counter}.md"
        dest_md_meta = OUTPUT_DIR / f"{email_stem}_{counter}.meta.json"
        counter += 1
    if email_md_src.exists():
        shutil.copy2(str(email_md_src), str(dest_md))

    sidecar["verarbeitung"]["status"] = "transferred"
    sidecar["verarbeitung"]["user_bestaetigt"] = True
    sidecar["dokument"]["dateiname"] = dest_md.name
    dest_md_meta.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))

    with get_db() as con:
        con.execute("UPDATE email_pending SET status='transferred' WHERE id=?", (email_id,))
    shutil.rmtree(pending_dir, ignore_errors=True)
    log.info(f"Email {email_id} direkt dispatched (Pfad A): {dest_md.name}")


def _notify_email_processed(email_id: int, meta: dict, subject: str):
    """Telegram-Zusammenfassung nach direktem Dispatch (Pfad A): Archivieren oder Bearbeiten."""
    text = (
        f"📧 <b>Email verarbeitet</b>\n"
        f"Von: {html.escape(meta.get('absender', '?'))}\n"
        f"Betreff: {html.escape(subject)}\n"
        f"Kategorie: {CATEGORIES.get(meta.get('kategorie_id',''), meta.get('kategorie_id',''))}"
        f" → {meta.get('adressat', '?')}\n"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Archivieren", "callback_data": f"arch:{email_id}"},
        {"text": "✏️ Bearbeiten",  "callback_data": f"archedit:{email_id}"},
    ]]}
    tg_msg_id = tg_send(text, keyboard)
    if tg_msg_id:
        with get_db() as con:
            con.execute("UPDATE email_pending SET tg_msg_id=? WHERE id=?", (tg_msg_id, email_id))


def _ask_sender_approval(email_id: int, from_addr: str, subject: str):
    """Telegram-Anfrage für neuen Absender: Kategorie-Auswahl statt Annehmen/Blockieren."""
    address = _extract_email_address(from_addr)
    text = (
        f"📬 <b>Neuer Email-Absender</b>\n"
        f"{'─' * 35}\n"
        f"✉️ <b>Von:</b>     {html.escape(from_addr)}\n"
        f"📋 <b>Betreff:</b> {html.escape(subject)}\n"
        f"{'─' * 35}\n"
        f"Welche Kategorie soll Wilson für <code>{html.escape(address)}</code> verwenden?"
    )
    # Kategorie-Buttons in 3er-Reihen
    cat_btns = [
        {"text": _CAT_SHORT[k], "callback_data": f"ssetcat:{email_id}:{k}"}
        for k in CATEGORIES
    ]
    rows = [cat_btns[i:i+3] for i in range(0, len(cat_btns), 3)]
    rows.append([
        {"text": "🚫 Blockieren", "callback_data": f"sblock:{email_id}"},
        {"text": "⏳ Später",     "callback_data": f"slater:{email_id}"},
    ])
    tg_msg_id = tg_send(text, {"inline_keyboard": rows})
    if tg_msg_id:
        with get_db() as con:
            con.execute("UPDATE email_pending SET tg_msg_id=? WHERE id=?", (tg_msg_id, email_id))


def _do_full_email_processing(email_id: int, email_data: dict, pending_subdir: Path, silent: bool = False):
    """Vollständige Email-Verarbeitung: Body, PDFs, LLM, Sidecar, Telegram-Benachrichtigung.
    silent=True unterdrückt Telegram-Notification (für Retro-Import-Batches)."""
    subject = email_data.get("subject", "(kein Betreff)")
    from_addr = email_data.get("from", "")
    received_at = email_data.get("received_at", datetime.now().isoformat())
    payload = email_data.get("payload", {})
    msg_id = email_data["message_id"]

    log.info(f"Email {email_id} vollständig verarbeiten: {subject!r}")

    # Body extrahieren und nach Markdown konvertieren
    html_body, plain_body = _email_extract_body(payload)
    body_md = _email_to_markdown(html_body, plain_body)

    # PDF-Anhänge herunterladen
    pdf_attachments = _email_extract_pdf_attachments(msg_id, payload, pending_subdir)

    # ── KATEGORIE-OVERRIDE: Absender hat feste Kategorie → kein LLM ──────────
    address = _extract_email_address(from_addr)
    sender_row = _get_sender_row(address)

    if sender_row and sender_row["category_id"]:
        meta = {
            "absender":        sender_row["display_name"] or re.sub(r"<.*?>", "", from_addr).strip() or "Unbekannt",
            "datum":           received_at[:10] if received_at else datetime.now().strftime("%Y-%m-%d"),
            "kategorie_id":    sender_row["category_id"],
            "adressat":        sender_row["adressat"] or "Reinhard",
            "kurzbezeichnung": normalize_filename(subject[:40]),
            "beschreibung":    f"Email von {sender_row['display_name'] or from_addr}: {subject}",
            "konfidenz":       "hoch",
        }
        log.info(f"Kategorie-Override: {address} → {sender_row['category_id']} (kein LLM, kein Review)")
    else:
        # Pfad B: LLM-Klassifikation
        meta = _classify_email(from_addr, subject, body_md)
        if not meta:
            log.warning(f"LLM-Klassifikation fehlgeschlagen für Email: {subject}")
            meta = {
                "absender": re.sub(r"<.*?>", "", from_addr).strip() or "Unbekannt",
                "datum": received_at[:10] if received_at else None,
                "kategorie_id": "archiv",
                "adressat": "Reinhard",
                "kurzbezeichnung": normalize_filename(subject[:40]),
                "beschreibung": f"Email von {from_addr}: {subject}",
                "konfidenz": "niedrig",
            }

    # Fehlende Felder auffüllen (gemeinsam)
    if not meta.get("datum"):
        meta["datum"] = received_at[:10] if received_at else datetime.now().strftime("%Y-%m-%d")
    meta["von"] = from_addr
    meta["betreff"] = subject
    meta["anlagen"] = pdf_attachments

    # ── ZUSTELLUNGS-SONDERFALL: kein Vault, nur Erinnerung ───────────────────
    if meta.get("kategorie_id") == "zustellung":
        _handle_zustellung(meta, subject, body_md)
        with get_db() as con:
            con.execute("UPDATE email_pending SET status='transferred' WHERE id=?", (email_id,))
        shutil.rmtree(pending_subdir, ignore_errors=True)
        log.info(f"Email {email_id} als Zustellungsbenachrichtigung behandelt (kein Vault)")
        return

    # Standardisierte Dateinamen für Anhänge bauen
    base_stem = _build_email_filename(meta, suffix="").rstrip("_")
    renamed_pdfs = []
    for i, orig_name in enumerate(pdf_attachments, start=1):
        pdf_src = pending_subdir / orig_name
        if not pdf_src.exists():
            continue
        new_name = f"{base_stem}_Anlage-{i}.pdf"
        pdf_dest = pending_subdir / new_name
        pdf_src.rename(pdf_dest)
        renamed_pdfs.append(new_name)
    meta["anlagen"] = renamed_pdfs

    # Sidecar für Anhänge erstellen
    for pdf_name in renamed_pdfs:
        anlage_meta = {
            "absender":        meta.get("absender", ""),
            "datum":           meta.get("datum", ""),
            "kategorie_id":    meta.get("kategorie_id", "archiv"),
            "adressat":        meta.get("adressat", "Reinhard"),
            "kurzbezeichnung": Path(pdf_name).stem.replace("_", "-"),
            "beschreibung":    f"Anhang der Email: {subject}",
            "dateiname":       pdf_name,
            "konfidenz":       "hoch",
            "email_ref":       _build_email_filename(meta),
        }
        anlage_sidecar = {
            "version":    "2.0",
            "source":     "email-anlage",
            "dokument":   anlage_meta,
            "verarbeitung": {
                "extrahiert_von": "Wilson-Email",
                "extrahiert_am":  datetime.now().isoformat(timespec="seconds"),
                "llm_via":        DEEPSEEK_MODEL if DEEPSEEK_KEY else OLLAMA_MODEL,
                "status":         "pending",
            },
        }
        sidecar_name = Path(pdf_name).stem + ".meta.json"
        (pending_subdir / sidecar_name).write_text(
            json.dumps(anlage_sidecar, ensure_ascii=False, indent=2)
        )

    # Email-MD schreiben
    (pending_subdir / "email.md").write_text(body_md, encoding="utf-8")

    # Hauptsidecar
    meta["dateiname"] = _build_email_filename(meta)
    transfer_at = (datetime.now() + timedelta(minutes=PENDING_MINUTES)).isoformat(timespec="seconds")
    sidecar = {
        "version":    "2.0",
        "source":     "email",
        "dokument":   meta,
        "verarbeitung": {
            "extrahiert_von": "Wilson-Email",
            "extrahiert_am":  datetime.now().isoformat(timespec="seconds"),
            "llm_via":        DEEPSEEK_MODEL if DEEPSEEK_KEY else OLLAMA_MODEL,
            "status":         "pending",
            "weitergabe_um":  transfer_at,
            "user_bestaetigt": False,
        },
    }
    (pending_subdir / "email.meta.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2)
    )

    if sender_row and sender_row["category_id"]:
        # ── Pfad A: Direkt in output_dir, kein Review, kein PENDING_MINUTES ────
        with get_db() as con:
            con.execute(
                "UPDATE email_pending SET sidecar=?, pending_dir=?, status='pending' WHERE id=?",
                (json.dumps(sidecar), str(pending_subdir), email_id),
            )
        _dispatch_email_to_output(email_id, sidecar, pending_subdir)
        if not silent:
            _notify_email_processed(email_id, meta, subject)
            # Auto-Retro-Import: einmalig beim ersten Email nach Kategorie-Zuweisung
            if not sender_row["archive_imported"]:
                with get_db() as con:
                    con.execute("UPDATE email_senders SET archive_imported=1 WHERE id=?", (sender_row["id"],))
                threading.Thread(
                    target=_retro_import_sender_bg,
                    args=(sender_row["id"], address, sender_row["display_name"] or ""),
                    daemon=True, name=f"retro-{address[:20]}",
                ).start()
                log.info(f"Auto-Retro-Import gestartet für: {address}")
            # Kontaktdaten aus Signatur im Hintergrund aktualisieren
            threading.Thread(
                target=_update_sender_contact_bg,
                args=(sender_row["id"], body_md),
                daemon=True, name=f"sig-{address[:20]}",
            ).start()
    else:
        # ── Pfad B: Pending-Queue + Telegram-Review (bestehender Flow) ──────────
        with get_db() as con:
            con.execute(
                "UPDATE email_pending SET sidecar=?, pending_dir=?, status='pending', transfer_at=? WHERE id=?",
                (json.dumps(sidecar), str(pending_subdir), transfer_at, email_id),
            )
        text, keyboard = _format_email_notification(meta, transfer_at, email_id)
        tg_msg_id = tg_send(text, keyboard)
        if tg_msg_id:
            with get_db() as con:
                con.execute("UPDATE email_pending SET tg_msg_id=? WHERE id=?", (tg_msg_id, email_id))
        log.info(f"Email {email_id} in Pending: {meta['dateiname']}, {len(renamed_pdfs)} Anhang/Anhänge")


def process_email(email_data: dict) -> bool:
    """Hauptverarbeitungsroutine für eine neue Email.
    Prüft Absender-Status bevor irgendwas verarbeitet wird.
    email_data: {message_id, thread_id, subject, from, date, payload, received_at}
    """
    msg_id = email_data["message_id"]
    thread_id = email_data.get("thread_id", "")
    subject = email_data.get("subject", "(kein Betreff)")
    from_addr = email_data.get("from", "")
    received_at = email_data.get("received_at", datetime.now().isoformat())

    address = _extract_email_address(from_addr)
    display_name = re.sub(r"<.*?>", "", from_addr).strip()
    sender_status = _get_sender_status(address)

    log.info(f"Email empfangen: {subject!r} von {from_addr} [sender={sender_status}]")

    if sender_status == "blocked":
        log.info(f"Blockierter Absender ignoriert: {address}")
        # Minimal-Eintrag für Dedup — damit die Email beim nächsten Poll nicht nochmal geholt wird
        try:
            with get_db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO email_pending"
                    "(message_id, thread_id, subject, sender, received_at, status) "
                    "VALUES (?,?,?,?,?,'blocked')",
                    (msg_id, thread_id, subject, from_addr, received_at),
                )
        except Exception as e:
            log.error(f"DB-Eintrag für blockierte Email fehlgeschlagen: {e}")
        return True

    # Pending-Verzeichnis anlegen
    pending_subdir = PENDING_DIR / f"email-{msg_id[:12]}"
    pending_subdir.mkdir(parents=True, exist_ok=True)

    initial_status = "approved" if sender_status == "approved" else "awaiting_approval"

    with get_db() as con:
        try:
            cur = con.execute(
                "INSERT INTO email_pending"
                "(message_id, thread_id, subject, sender, received_at, pending_dir, raw_data, status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (msg_id, thread_id, subject, from_addr, received_at,
                 str(pending_subdir), json.dumps(email_data), initial_status),
            )
            email_id = cur.lastrowid
        except sqlite3.IntegrityError:
            log.info(f"Email bereits bekannt: {msg_id[:12]}…")
            shutil.rmtree(pending_subdir, ignore_errors=True)
            return True

    if sender_status == "approved":
        _do_full_email_processing(email_id, email_data, pending_subdir)
    else:
        # Unbekannter Absender → Freigabe anfragen, kein LLM / keine Downloads
        _upsert_sender(address, display_name, "pending")
        _ask_sender_approval(email_id, from_addr, subject)

    return True


def _fetch_new_emails() -> list[dict]:
    """Holt neue Emails aus Gmail via gog. Filtert bereits bekannte Message-IDs."""
    data = _gog_run("gmail", "messages", "search", EMAIL_SEARCH_QUERY, "--max", "50")
    messages_raw = data.get("messages", [])
    if not messages_raw:
        return []

    # Bekannte Message-IDs aus DB
    with get_db() as con:
        known = {row[0] for row in con.execute("SELECT message_id FROM email_pending").fetchall()}

    new_emails = []
    for msg in messages_raw:
        msg_id = msg.get("id", "")
        if not msg_id or msg_id in known:
            continue

        # Vollständige Email-Daten laden
        full = _gog_run("gmail", "get", msg_id)
        message = full.get("message", {})
        if not message:
            continue

        payload = message.get("payload", {})
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}

        new_emails.append({
            "message_id":  msg_id,
            "thread_id":   message.get("threadId", ""),
            "subject":     headers.get("subject", "(kein Betreff)"),
            "from":        headers.get("from", ""),
            "date":        headers.get("date", ""),
            "received_at": datetime.now().isoformat(timespec="seconds"),
            "payload":     payload,
        })

    return new_emails


def email_poll_thread():
    """Polling-Loop: prüft alle EMAIL_POLL_INTERVAL Sekunden auf neue Emails."""
    log.info(f"Email-Poller startet (Intervall: {EMAIL_POLL_INTERVAL}s, Account: {GOG_ACCOUNT})")
    while True:
        try:
            emails = _fetch_new_emails()
            if emails:
                log.info(f"Email-Poll: {len(emails)} neue Email(s) gefunden")
            for email_data in emails:
                try:
                    process_email(email_data)
                except Exception as e:
                    log.error(f"Email-Verarbeitung fehlgeschlagen ({email_data.get('subject', '?')}): {e}")
        except Exception as e:
            log.error(f"Email-Poll-Fehler: {e}")
        time.sleep(EMAIL_POLL_INTERVAL)


# ── Hintergrund-Threads ────────────────────────────────────────────────────────

DISPATCHER_CLEANUP_MINUTES = int(os.environ.get("DISPATCHER_CLEANUP_MINUTES", "120"))
_STARTUP_TIME = datetime.now()
_SYNCTHING_APIKEY: str | None = None


def _syncthing_apikey() -> str | None:
    global _SYNCTHING_APIKEY
    if _SYNCTHING_APIKEY:
        return _SYNCTHING_APIKEY
    try:
        import xml.etree.ElementTree as ET
        cfg = Path.home() / ".local/state/syncthing/config.xml"
        root = ET.parse(cfg).getroot()
        _SYNCTHING_APIKEY = root.find(".//gui/apikey").text
    except Exception:
        pass
    return _SYNCTHING_APIKEY


def _cleanup_output_dir():
    """Löscht Dateien aus ~/input-dispatcher/ sobald der Dispatcher sie verarbeitet hat.
    Strategie:
      1. Primär: Syncthing meldet 'needDeletes' → Ryzen hat die Datei gelöscht → sicher entfernen.
      2. Fallback: Datei älter als DISPATCHER_CLEANUP_MINUTES (Standard 120 min) UND
         das Programm läuft mindestens so lange (kein Kaltstart-Wettlauf).
    """
    uptime_min = (datetime.now() - _STARTUP_TIME).total_seconds() / 60

    # Dateien die Ryzen bereits gelöscht hat (= verarbeitet) via Syncthing-API ermitteln
    processed_on_ryzen: set[str] = set()
    apikey = _syncthing_apikey()
    if apikey:
        try:
            r = requests.get(
                "http://127.0.0.1:8384/rest/db/need?folder=input-dispatcher",
                headers={"X-API-Key": apikey}, timeout=3,
            )
            if r.ok:
                for item in r.json().get("deletes", []):
                    processed_on_ryzen.add(item["name"])
        except Exception:
            pass

    cutoff = datetime.now() - timedelta(minutes=DISPATCHER_CLEANUP_MINUTES)
    for f in OUTPUT_DIR.iterdir():
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            # Primär: Ryzen hat verarbeitet
            if f.name in processed_on_ryzen:
                f.unlink()
                log.info(f"Cleanup (verarbeitet): {f.name}")
            # Fallback: Alt + Programm läuft lange genug
            elif mtime < cutoff and uptime_min >= DISPATCHER_CLEANUP_MINUTES:
                f.unlink()
                log.info(f"Cleanup (Timeout {DISPATCHER_CLEANUP_MINUTES} min): {f.name}")
        except Exception as e:
            log.warning(f"Cleanup-Fehler {f.name}: {e}")


def timer_thread():
    """Prüft jede Minute, ob Pending-Dokumente die Wartezeit überschritten haben."""
    while True:
        time.sleep(60)
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with get_db() as con:
                rows = con.execute(
                    "SELECT id FROM pending WHERE status IN "
                    "('pending','correcting','guided_kat','guided_adr','guided_abs','guided_fin') "
                    "AND transfer_at <= ?",
                    (now,),
                ).fetchall()
            for row in rows:
                log.info(f"Timeout: Dokument {row['id']} wird automatisch weitergegeben.")
                transfer_document(row["id"])
            # Emails mit abgelaufener Wartezeit automatisch übertragen
            with get_db() as con:
                email_rows = con.execute(
                    "SELECT id FROM email_pending WHERE status IN ('pending','correcting') "
                    "AND transfer_at <= ?",
                    (now,),
                ).fetchall()
            for row in email_rows:
                log.info(f"Timeout: Email {row['id']} wird automatisch weitergegeben.")
                transfer_email_package(row["id"])
            # Fällige Erinnerungen senden
            today = datetime.now().strftime("%Y-%m-%d")
            with get_db() as con:
                remind_rows = con.execute(
                    "SELECT id, text FROM reminders WHERE reminder_date <= ? AND sent=0", (today,)
                ).fetchall()
            for r in remind_rows:
                tg_send(f"🔔 <b>Erinnerung</b>\n{r['text']}")
                with get_db() as con:
                    con.execute("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
            _cleanup_output_dir()
        except Exception as e:
            log.error(f"Timer-Thread-Fehler: {e}")

def telegram_poll_thread():
    """Long-Polling für Telegram-Updates."""
    if DISABLE_TG_POLL:
        log.info("Telegram-Polling deaktiviert (DISABLE_TELEGRAM_POLL=1)")
        return
    with get_db() as con:
        offset = con.execute("SELECT offset FROM tg_offset WHERE id=1").fetchone()["offset"]

    while True:
        updates = tg_get_updates(offset)
        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            try:
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd and upd["message"].get("chat", {}).get("id") == int(TELEGRAM_CHAT):
                    msg = upd["message"]
                    doc = msg.get("document", {})
                    if doc.get("mime_type") == "application/pdf":
                        _handle_tg_pdf_upload(doc)
                    else:
                        handle_text_message(msg)
            except Exception as e:
                log.error(f"Update-Handler-Fehler: {e}")
        with get_db() as con:
            con.execute("UPDATE tg_offset SET offset=? WHERE id=1", (offset,))

def scan_incoming():
    """Polling-Loop: scannt ~/incoming/ auf neue PDFs."""
    known: set = set()
    while True:
        try:
            current: set = set()
            for f in INCOMING_DIR.glob("*.pdf"):
                if f not in known:
                    time.sleep(2)  # kurz warten bis Datei vollständig geschrieben
                    if f.stat().st_size > 0:
                        try:
                            ok = process_pdf(f)
                        except Exception as e:
                            log.error(f"Fehler bei {f.name}: {e}")
                            ok = False
                        if ok:
                            f.unlink(missing_ok=True)
                        else:
                            # Temporärer Fehler (OCR nicht erreichbar) — beim nächsten Tick erneut versuchen
                            current.add(f)
                    else:
                        current.add(f)
                else:
                    current.add(f)
            known = current
        except Exception as e:
            log.error(f"Scan-Fehler: {e}")
        time.sleep(POLL_INTERVAL)

# ── Callback-Relay (HTTP, Port 8770) ──────────────────────────────────────────
# Empfängt vom Dispatcher weitergeleitete Telegram-Callbacks für den geführten Dialog.

RELAY_PORT = int(os.environ.get("RELAY_PORT", "8770"))

class _RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/tg/callback":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            cb = {
                "id": body["callback_id"],
                "data": body["data"],
                "message": {
                    "chat": {"id": int(body["chat_id"])},
                    "message_id": body["msg_id"],
                    "text": body.get("msg_text", ""),
                },
            }
            handle_callback(cb)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            log.error(f"Relay-Fehler: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *_): pass  # kein Access-Log


def relay_server_thread():
    srv = HTTPServer(("0.0.0.0", RELAY_PORT), _RelayHandler)
    log.info(f"Callback-Relay lauscht auf :{RELAY_PORT}")
    srv.serve_forever()


# ── Absender-Scan & Web-UI ─────────────────────────────────────────────────────

_scan_state: dict = {"status": "idle"}

_SENDER_UI_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Email-Absender · Wilson</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#f0f2f5;min-height:100vh;padding:20px}
.card{background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:24px;max-width:1250px;margin:0 auto}
h1{font-size:1.35rem;margin-bottom:16px;color:#1a1a2e}
.toolbar{display:flex;align-items:center;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.btn{border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:.88rem;font-weight:500;transition:opacity .15s}
.btn:hover{opacity:.85}
.btn:disabled{opacity:.5;cursor:default}
.btn-primary{background:#2563eb;color:#fff}
.btn-success{background:#16a34a;color:#fff}
#progress{font-size:.85rem;color:#6b7280}
#searchBox{border:1px solid #d1d5db;border-radius:6px;padding:7px 12px;font-size:.88rem;min-width:200px}
table{width:100%;border-collapse:collapse}
thead th{background:#f8f9fa;padding:10px 14px;text-align:left;font-size:.78rem;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;border-bottom:2px solid #e5e7eb}
th.num{text-align:right}
td{padding:9px 14px;border-bottom:1px solid #f3f4f6;font-size:.88rem;vertical-align:middle}
tr:hover td{background:#fafbfc}
.mono{font-family:ui-monospace,monospace;font-size:.82rem}
td.cnt{font-weight:700;color:#2563eb;text-align:right}
select.st,select.cat{border:1px solid #d1d5db;border-radius:5px;padding:4px 8px;font-size:.83rem;background:#fff;cursor:pointer}
select.cat{max-width:200px}
td.addr{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend{font-size:.8rem;color:#6b7280;margin-bottom:14px;padding:8px 12px;background:#f8f9fa;border-radius:6px;border-left:3px solid #d1d5db}
.del-btn{background:none;border:1px solid #e5e7eb;border-radius:5px;padding:4px 9px;cursor:pointer;color:#9ca3af;font-size:.85rem;transition:all .15s}
.del-btn:hover{border-color:#ef4444;color:#ef4444}
.imp-btn{background:none;border:1px solid #e5e7eb;border-radius:5px;padding:4px 9px;cursor:pointer;color:#6b7280;font-size:.85rem;transition:all .15s;margin-right:4px}
.imp-btn:hover:not(:disabled){border-color:#2563eb;color:#2563eb}
.imp-btn:disabled{opacity:.3;cursor:default}
td.name-cell{cursor:pointer;color:#4b5563}
td.name-cell:hover{color:#7c3aed;text-decoration:underline}
td.name-cell.has-data{color:#7c3aed}
#ctOverlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:200;align-items:center;justify-content:center}
#ctOverlay.open{display:flex}
#ctBox{background:#fff;border-radius:10px;padding:24px;width:420px;max-width:95vw;box-shadow:0 8px 32px rgba(0,0,0,.2)}
#ctBox h2{font-size:1rem;font-weight:600;margin-bottom:16px;color:#1a1a2e}
.ct-field{margin-bottom:10px}
.ct-field label{font-size:.78rem;color:#6b7280;display:block;margin-bottom:3px;font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.ct-field input,.ct-field textarea{width:100%;border:1px solid #d1d5db;border-radius:6px;padding:7px 10px;font-size:.88rem;font-family:inherit}
.ct-field textarea{resize:vertical}
.ct-actions{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
.add-row{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;border-top:1px solid #f3f4f6;padding-top:16px}
.add-row input,.add-row select{border:1px solid #d1d5db;border-radius:6px;padding:7px 10px;font-size:.88rem}
.add-row input:first-child{flex:2;min-width:200px}
.add-row input:nth-child(2){flex:1;min-width:120px}
.empty{text-align:center;padding:40px;color:#9ca3af}
.tabs{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}
.tab{border:1px solid #e5e7eb;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:.83rem;background:#fff;color:#6b7280;transition:all .15s}
.tab:hover{border-color:#93c5fd;color:#2563eb}
.tab.active{background:#2563eb;color:#fff;border-color:#2563eb}
.badge{display:inline-block;background:#f3f4f6;color:#6b7280;border-radius:10px;padding:1px 7px;font-size:.75rem;margin-left:5px}
.tab.active .badge{background:rgba(255,255,255,.25);color:#fff}
</style>
</head>
<body>
<div class="card">
<h1>\U0001f4e7 Email-Absender Verwaltung</h1>
<div class="toolbar">
  <button class="btn btn-primary" id="scanBtn" onclick="startScan()">\U0001f50d Archiv scannen</button>
  <span id="progress"></span>
  <input id="searchBox" placeholder="\U0001f50d Suchen…" oninput="filterTable(this.value)">
</div>
<div class="tabs">
  <button class="tab" id="tab-all" onclick="setTab('')">Alle <span class="badge" id="cnt-all">–</span></button>
  <button class="tab active" id="tab-pending" onclick="setTab('pending')">⏳ Ausstehend <span class="badge" id="cnt-pending">–</span></button>
  <button class="tab" id="tab-approved" onclick="setTab('approved')">✅ Genehmigt <span class="badge" id="cnt-approved">–</span></button>
  <button class="tab" id="tab-blocked" onclick="setTab('blocked')">\U0001f6ab Blockiert <span class="badge" id="cnt-blocked">–</span></button>
</div>
<div class="legend">✅ <b>Genehmigt</b> — Email wird verarbeitet &nbsp;·&nbsp; ⏳ <b>Ausstehend</b> — nächste Email fragt per Telegram nach &nbsp;·&nbsp; \U0001f6ab <b>Blockiert</b> — Email wird ignoriert</div>
<table>
  <thead>
    <tr>
      <th>Email-Adresse</th>
      <th>Anzeigename</th>
      <th class="num">Archiv</th>
      <th>Kategorie</th>
      <th>Status</th>
      <th></th>
    </tr>
  </thead>
  <tbody id="tbody"><tr><td colspan="6" class="empty">Lade…</td></tr></tbody>
</table>
<form class="add-row" onsubmit="addSender(event)">
  <input id="newAddr" placeholder="email@domain.com oder @domain.com" autocomplete="off">
  <input id="newName" placeholder="Anzeigename (optional)">
  <select id="newSt">
    <option value="approved">✅ Genehmigt</option>
    <option value="pending">⏳ Ausstehend</option>
    <option value="blocked">\U0001f6ab Blockiert</option>
  </select>
  <button type="submit" class="btn btn-success">+ Hinzufügen</button>
</form>
</div>

<div id="ctOverlay" onclick="if(event.target===this)closeContact()">
  <div id="ctBox">
    <h2>📋 Kontaktdaten</h2>
    <div class="ct-field"><label>Telefon</label><input id="ctPhone" placeholder="+49 …"></div>
    <div class="ct-field"><label>Postadresse</label><textarea id="ctPostal" rows="2" placeholder="Straße, PLZ Ort"></textarea></div>
    <div class="ct-field"><label>Website</label><input id="ctWebsite" placeholder="https://…"></div>
    <div class="ct-field"><label>Notizen</label><textarea id="ctNotes" rows="2" placeholder="Freitext"></textarea></div>
    <div class="ct-actions">
      <button class="btn" style="background:#f3f4f6;color:#374151" onclick="closeContact()">Abbrechen</button>
      <button class="btn btn-primary" onclick="saveContact()">Speichern</button>
    </div>
  </div>
</div>
<script>
const SL={approved:'✅ Genehmigt',blocked:'\U0001f6ab Blockiert',pending:'⏳ Ausstehend'};
let cats=[];
let pollTimer=null;
let _activeTab='pending';

async function init(){
  cats=await(await fetch('/api/categories')).json();
  load();
}

function buildCatOptions(selected){
  const opts=[`<option value="">– Automatisch (LLM) –</option>`];
  cats.forEach(c=>{
    opts.push(`<option value="${esc(c.id)}"${selected===c.id?' selected':''}>${esc(c.label)}</option>`);
  });
  return opts.join('');
}

async function load(){
  const rows=await(await fetch('/api/senders')).json();
  const tb=document.getElementById('tbody');
  if(!rows.length){
    tb.innerHTML='<tr><td colspan="6" class="empty">Keine Absender vorhanden. Archiv scannen oder manuell hinzufügen.</td></tr>';
    return;
  }
  // Update tab badge counts
  const counts={all:rows.length,approved:0,pending:0,blocked:0};
  rows.forEach(s=>{if(counts[s.status]!==undefined)counts[s.status]++;});
  ['all','approved','pending','blocked'].forEach(k=>{
    const el=document.getElementById('cnt-'+k);
    if(el)el.textContent=counts[k];
  });
  tb.innerHTML=rows.map(s=>`<tr id="r${s.id}" data-status="${s.status}">
    <td class="mono addr" title="${esc(s.address)}">${esc(s.address)}</td>
    <td class="name-cell${(s.phone||s.postal||s.website||s.notes)?' has-data':''}" onclick="openContactFromEl(${s.id},this)" data-phone="${esc(s.phone||'')}" data-postal="${esc(s.postal||'')}" data-website="${esc(s.website||'')}" data-notes="${esc(s.notes||'')}" title="${(s.phone||s.postal||s.website||s.notes)?'Kontaktdaten bearbeiten':'Kontaktdaten hinzufügen'}">${esc(s.display_name||'–')}</td>
    <td class="cnt">${s.archive_count||0}</td>
    <td><select class="cat" onchange="setCat(${s.id},this.value)">${buildCatOptions(s.category_id||'')}</select></td>
    <td><select class="st" onchange="setStatus(${s.id},this.value)">
      ${['approved','pending','blocked'].map(v=>`<option value="${v}"${s.status===v?' selected':''}>${SL[v]}</option>`).join('')}
    </select></td>
    <td>
      <button class="imp-btn" id="imp${s.id}" onclick="importHistory(${s.id})" title="Historische Emails importieren"${s.category_id?'':' disabled'}>📥</button>
      <button class="del-btn" onclick="del(${s.id})" title="Löschen">\U0001f5d1</button>
    </td>
  </tr>`).join('');
  filterTable(document.getElementById('searchBox').value);
}

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function setTab(status){
  _activeTab=status;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  const tid=status?'tab-'+status:'tab-all';
  const el=document.getElementById(tid);
  if(el)el.classList.add('active');
  filterTable(document.getElementById('searchBox').value);
}

function filterTable(q){
  q=q.toLowerCase();
  document.querySelectorAll('#tbody tr').forEach(tr=>{
    const matchTab=!_activeTab||tr.dataset.status===_activeTab;
    const matchSearch=!q||tr.textContent.toLowerCase().includes(q);
    tr.style.display=(matchTab&&matchSearch)?'':'none';
  });
}

async function setStatus(id,status){
  await fetch('/api/senders/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  const tr=document.getElementById('r'+id);
  if(tr)tr.dataset.status=status;
  load();
}
async function setCat(id,cat){
  await fetch('/api/senders/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({category_id:cat})});
  const btn=document.getElementById('imp'+id);
  if(btn) btn.disabled=!cat;
}
let _ctId=null;
function openContactFromEl(id,el){
  openContact(id,el.dataset.phone,el.dataset.postal,el.dataset.website,el.dataset.notes);
}
function openContact(id,phone,postal,website,notes){
  _ctId=id;
  document.getElementById('ctPhone').value=phone;
  document.getElementById('ctPostal').value=postal;
  document.getElementById('ctWebsite').value=website;
  document.getElementById('ctNotes').value=notes;
  document.getElementById('ctOverlay').classList.add('open');
}
function closeContact(){document.getElementById('ctOverlay').classList.remove('open');}
async function saveContact(){
  const r=await fetch('/api/senders/'+_ctId,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      phone:document.getElementById('ctPhone').value,
      postal:document.getElementById('ctPostal').value,
      website:document.getElementById('ctWebsite').value,
      notes:document.getElementById('ctNotes').value,
    })});
  if(r.ok){closeContact();load();}
}
async function importHistory(id){
  const btn=document.getElementById('imp'+id);
  if(btn){btn.textContent='⏳';btn.disabled=true;}
  const r=await fetch('/api/senders/'+id+'/import',{method:'POST'});
  const d=await r.json();
  if(r.ok){
    if(btn){btn.textContent='✅';setTimeout(()=>{btn.textContent='📥';btn.disabled=false;},3000);}
  }else{
    if(btn){btn.textContent='📥';btn.disabled=false;}
    alert(d.error||'Import fehlgeschlagen');
  }
}
async function del(id){
  if(!confirm('Absender löschen?'))return;
  await fetch('/api/senders/'+id,{method:'DELETE'});
  load();
}
async function addSender(e){
  e.preventDefault();
  const addr=document.getElementById('newAddr').value.trim();
  if(!addr)return;
  const r=await fetch('/api/senders',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({address:addr,display_name:document.getElementById('newName').value.trim(),
    status:document.getElementById('newSt').value})});
  if(r.ok){document.getElementById('newAddr').value='';document.getElementById('newName').value='';load();}
  else{const d=await r.json();alert(d.error||'Fehler');}
}
async function startScan(){
  document.getElementById('scanBtn').disabled=true;
  document.getElementById('progress').textContent='⏳ Starte Scan…';
  await fetch('/api/scan');
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(async()=>{
    const s=await(await fetch('/api/scan/status')).json();
    if(s.status==='running'){
      const tot=s.total?`/${s.total}`:'';
      document.getElementById('progress').textContent=`⏳ ${s.processed}${tot} Emails, ${s.senders} Absender…`;
    }else if(s.status==='done'){
      document.getElementById('progress').textContent=`✅ ${s.total} Emails, ${s.senders} Absender gefunden`;
      document.getElementById('scanBtn').disabled=false;
      clearInterval(pollTimer);load();
    }else if(s.status==='error'){
      document.getElementById('progress').textContent='❌ '+s.error;
      document.getElementById('scanBtn').disabled=false;
      clearInterval(pollTimer);
    }else{
      clearInterval(pollTimer);
      document.getElementById('scanBtn').disabled=false;
    }
  },2000);
}
init();
</script>
</body>
</html>"""


def _scan_archived_emails_bg():
    """Scannt alle Gmail-Emails (SENDER_SCAN_QUERY) und zählt Absender.
    Einmalig per Klick ausgelöst — ändert NICHT die laufende Verarbeitungslogik.
    Aktualisiert email_senders.archive_count. Läuft in Background-Thread."""
    global _scan_state
    if _scan_state.get("status") == "running":
        return
    _scan_state = {"status": "running", "processed": 0, "total": 0, "senders": 0, "error": ""}
    try:
        log.info(f"Archiv-Scan: Lade alle Emails mit '{SENDER_SCAN_QUERY}' (--all)…")
        data = _gog_run("gmail", "messages", "search", SENDER_SCAN_QUERY, "--all", timeout=600)
        messages = data.get("messages", [])
        log.info(f"Archiv-Scan: {len(messages)} Nachrichten gesamt, starte threadId-Deduplication")

        # ThreadId-Deduplication: Pro Thread reicht eine Nachricht (= gleicher Absender)
        seen_threads: set = set()
        to_fetch: list = []
        for msg in messages:
            tid = msg.get("threadId") or msg.get("id", "")
            if tid not in seen_threads:
                seen_threads.add(tid)
                to_fetch.append(msg["id"])
        log.info(f"Archiv-Scan: {len(to_fetch)} eindeutige Threads → {len(to_fetch)} Fetches")

        sender_counts: dict[str, dict] = {}
        for i, msg_id in enumerate(to_fetch):
            full = _gog_run("gmail", "get", msg_id)
            payload = full.get("message", {}).get("payload", {})
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in payload.get("headers", [])}
            from_addr = headers.get("from", "")
            if from_addr:
                m = re.search(r"<([^>]+)>", from_addr)
                address = (m.group(1).strip() if m else from_addr.strip()).lower()
                display = _clean_display_name(re.sub(r"<.*?>", "", from_addr).strip())
                if address:
                    if address not in sender_counts:
                        sender_counts[address] = {"display_name": display, "count": 0}
                    sender_counts[address]["count"] += 1
            _scan_state["processed"] = i + 1
            _scan_state["senders"] = len(sender_counts)

        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as con:
            for address, info in sender_counts.items():
                con.execute(
                    "INSERT INTO email_senders(address, display_name, status, archive_count, last_scanned) "
                    "VALUES (?, ?, 'pending', ?, ?) "
                    "ON CONFLICT(address) DO UPDATE SET "
                    "archive_count=excluded.archive_count, last_scanned=excluded.last_scanned, "
                    "display_name=CASE WHEN display_name IS NULL OR display_name='' "
                    "THEN excluded.display_name ELSE display_name END",
                    (address, info["display_name"], info["count"], now),
                )

        _scan_state = {
            "status": "done",
            "processed": len(to_fetch),
            "total": len(messages),
            "senders": len(sender_counts),
            "error": "",
        }
        log.info(f"Archiv-Scan abgeschlossen: {len(sender_counts)} Absender aus {len(messages)} Emails ({len(to_fetch)} Threads)")
    except Exception as e:
        _scan_state = {"status": "error", "processed": 0, "total": 0, "senders": 0, "error": str(e)}
        log.error(f"Archiv-Scan Fehler: {e}")


class _SenderUIHandler(BaseHTTPRequestHandler):
    """Minimaler HTTP-Server für die Absender-Verwaltung."""

    def do_GET(self):
        if self.path in ("/", ""):
            self._html(_SENDER_UI_HTML)
        elif self.path == "/api/senders":
            with get_db() as con:
                rows = con.execute(
                    "SELECT id, address, display_name, status, category_id, adressat, "
                    "COALESCE(archive_count, 0) AS archive_count, last_scanned, "
                    "phone, postal, website, notes, contact_updated "
                    "FROM email_senders "
                    "ORDER BY COALESCE(archive_count,0) DESC, status, address"
                ).fetchall()
            self._json([dict(r) for r in rows])
        elif self.path == "/api/categories":
            self._json([{"id": k, "label": v} for k, v in CATEGORIES.items()])
        elif self.path == "/api/scan":
            threading.Thread(target=_scan_archived_emails_bg, daemon=True, name="archiv-scan").start()
            self._json({"ok": True, "message": "Scan gestartet"})
        elif self.path == "/api/scan/status":
            self._json(_scan_state)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        m_imp = re.match(r"/api/senders/(\d+)/import$", self.path)
        if m_imp:
            sid = int(m_imp.group(1))
            with get_db() as con:
                row = con.execute("SELECT * FROM email_senders WHERE id=?", (sid,)).fetchone()
            if not row:
                self._json({"error": "Absender nicht gefunden"}, 404); return
            if not row["category_id"]:
                self._json({"error": "Keine Kategorie gesetzt"}, 400); return
            with get_db() as con:
                con.execute("UPDATE email_senders SET archive_imported=0 WHERE id=?", (sid,))
            threading.Thread(
                target=_retro_import_sender_bg,
                args=(sid, row["address"], row["display_name"] or ""),
                daemon=True, name=f"retro-{row['address'][:20]}",
            ).start()
            self._json({"ok": True})
            return
        if self.path == "/api/senders":
            body = self._body()
            address = (body.get("address") or "").strip().lower()
            if not address:
                self._json({"error": "Adresse fehlt"}, 400); return
            display_name = _clean_display_name((body.get("display_name") or "").strip())
            status = body.get("status", "pending")
            if status not in ("approved", "blocked", "pending"):
                status = "pending"
            try:
                with get_db() as con:
                    con.execute(
                        "INSERT INTO email_senders(address, display_name, status) VALUES (?,?,?)",
                        (address, display_name, status),
                    )
                self._json({"ok": True})
            except sqlite3.IntegrityError:
                self._json({"error": "Adresse bereits vorhanden"}, 409)
        else:
            self.send_response(404); self.end_headers()

    def do_PATCH(self):
        m = re.match(r"/api/senders/(\d+)$", self.path)
        if not m:
            self.send_response(404); self.end_headers(); return
        sid = int(m.group(1))
        body = self._body()
        status = body.get("status")
        display_name = body.get("display_name")
        sets, params = [], []
        if status in ("approved", "blocked", "pending"):
            sets.append("status=?"); params.append(status)
        if display_name is not None:
            sets.append("display_name=?"); params.append(_clean_display_name(display_name))
        if "category_id" in body:
            sets.append("category_id=?"); params.append(body["category_id"] or None)
        if "adressat" in body:
            sets.append("adressat=?"); params.append(body["adressat"] or None)
        for field in ("phone", "postal", "website", "notes"):
            if field in body:
                sets.append(f"{field}=?"); params.append(body[field] or None)
        if any(f in body for f in ("phone", "postal", "website", "notes")):
            sets.append("contact_updated=datetime('now','localtime')")
        if sets:
            params.append(sid)
            with get_db() as con:
                con.execute(
                    f"UPDATE email_senders SET {','.join(sets)}, "
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    params,
                )
        self._json({"ok": True})

    def do_DELETE(self):
        m = re.match(r"/api/senders/(\d+)$", self.path)
        if not m:
            self.send_response(404); self.end_headers(); return
        with get_db() as con:
            con.execute("DELETE FROM email_senders WHERE id=?", (int(m.group(1)),))
        self._json({"ok": True})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, src: str):
        body = src.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass


def sender_ui_thread():
    srv = HTTPServer(("0.0.0.0", SENDER_UI_PORT), _SenderUIHandler)
    log.info(f"Absender-UI lauscht auf :{SENDER_UI_PORT} → http://{SENDER_UI_HOST}:{SENDER_UI_PORT}/")
    srv.serve_forever()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("Wilson Document Processor startet …")
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    _seed_senders_from_history()

    # Graceful Shutdown
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT,  lambda *_: stop.set())

    threads = [
        threading.Thread(target=timer_thread,        daemon=True, name="timer"),
        threading.Thread(target=telegram_poll_thread, daemon=True, name="tg-poll"),
        threading.Thread(target=scan_incoming,        daemon=True, name="scanner"),
        threading.Thread(target=relay_server_thread,  daemon=True, name="relay"),
        threading.Thread(target=email_poll_thread,    daemon=True, name="email-poll"),
        threading.Thread(target=sender_ui_thread,     daemon=True, name="sender-ui"),
    ]
    for t in threads:
        t.start()

    tg_send(
        f"🟢 <b>Wilson Document Processor aktiv</b>\n"
        f"Warte auf Dokumente in ~/incoming/\n"
        f"📧 Email-Poller aktiv — {GOG_ACCOUNT} (alle {EMAIL_POLL_INTERVAL}s)\n"
        f"🌐 Absender-UI: http://{SENDER_UI_HOST}:{SENDER_UI_PORT}/"
    )
    log.info(f"Bereit — überwache {INCOMING_DIR}")

    stop.wait()
    log.info("Document Processor beendet.")

if __name__ == "__main__":
    main()
