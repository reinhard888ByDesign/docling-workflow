import hashlib
import os
import re
import json
import time
import queue
import shutil
import sqlite3
import logging
import requests
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
from json_repair import repair_json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from langdetect import detect_langs, DetectorFactory, LangDetectException

DetectorFactory.seed = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

WATCH_DIR      = Path(os.environ.get("WATCH_DIR",      "/data/input-dispatcher"))
TEMP_DIR       = Path(os.environ.get("TEMP_DIR",       "/data/dispatcher-temp"))
CONFIG_FILE    = Path(os.environ.get("CONFIG_FILE",    "/config/categories.yaml"))
PERSONEN_FILE   = Path(os.environ.get("PERSONEN_FILE",   "/config/personen.yaml"))
ABSENDER_FILE   = Path(os.environ.get("ABSENDER_FILE",   "/config/absender.yaml"))
DOC_TYPES_FILE  = Path(os.environ.get("DOC_TYPES_FILE",  "/config/doc_types.yaml"))
DB_FILE        = TEMP_DIR / "dispatcher.db"
DOCLING_URL    = os.environ.get("DOCLING_URL",          "http://docling-serve:5001")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",           "http://ollama:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",         "qwen2.5:7b")
OLLAMA_TRANSLATE_MODEL = os.environ.get("OLLAMA_TRANSLATE_MODEL", OLLAMA_MODEL)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",  "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",    "")
API_PORT       = int(os.environ.get("API_PORT", "8765"))

TEMP_DIR.mkdir(parents=True, exist_ok=True)

_vault_pdf = os.environ.get("VAULT_PDF_ARCHIV", "")
_vault_root = os.environ.get("VAULT_ROOT", "")
VAULT_PDF_ARCHIV = Path(_vault_pdf) if _vault_pdf else None
VAULT_ROOT = Path(_vault_root) if _vault_root else None

# Routing-Sets — werden beim ersten load_categories()-Aufruf aus categories.yaml geladen.
# LEISTUNGSABRECHNUNG_TYPES: type_ids die das LA-Telegram-Template (Rechnungsmatching) bekommen.
# VERSICHERUNG_TYPES: type_ids die das Standard-Versicherungs-Template bekommen.
LEISTUNGSABRECHNUNG_TYPES: set[str] = {"leistungsabrechnung"}
VERSICHERUNG_TYPES: set[str] = {
    "versicherungsschein", "beitragsanpassung", "beitragsbescheinigung",
    "kostenuebernahme", "versicherungsbedingungen", "versicherungskorrespondenz",
}
BRANCHEN_REGELN: list[dict] = []  # wird aus categories.yaml geladen

# Wird beim Start aus categories.yaml geladen.
CATEGORY_TO_VAULT_FOLDER: dict[str, str] = {}

# Routing pro (category_id, type_id):
#   vault_subfolder   → Unterordner unter vault_folder (z.B. "Leistungsabrechnung")
#   person_subfolder  → True: adressat als Suffix anhängen ("Leistungsabrechnung Reinhard")
#   adressat_fallback → Fallback-Person wenn adressat leer ("Sonstiges")
TYPE_ROUTING: dict[tuple[str, str], dict] = {}

# Schwellwert für OCR-Qualitäts-Gate (Zeichen im Docling-Ergebnis)
OCR_MIN_CHARS = 300


def build_vault_path(category_id: str, type_id: str, adressat: str,
                     year: str, md_filename: str) -> str:
    """Berechnet den vollständigen Vault-Relativpfad für eine MD-Datei.

    Struktur: {vault_folder}/[{type_subfolder}[{ person}]/][{year}/]{md_filename}

    Aktuelles Jahr landet direkt im (Typ-)Wurzelordner, Vorjahre in /{year}/.
    Ist kein vault_subfolder definiert, fällt der Pfad auf reines Jahr-Routing zurück
    (Rückwärtskompatibilität für alle nicht-KV-Kategorien).
    """
    vault_folder = CATEGORY_TO_VAULT_FOLDER.get(category_id, "00 Inbox")
    routing = TYPE_ROUTING.get((category_id, type_id), {})
    subfolder = routing.get("vault_subfolder")

    if subfolder:
        if routing.get("person_subfolder"):
            person = (adressat or "").strip().capitalize()
            if not person:
                person = routing.get("adressat_fallback", "Sonstiges")
            subfolder = f"{subfolder} {person}"
        vault_folder = f"{vault_folder}/{subfolder}"

    current_year = datetime.now().strftime("%Y")
    if year != current_year:
        vault_folder = f"{vault_folder}/{year}"

    return f"{vault_folder}/{md_filename}"

# ── DB-Schema für NL-Abfragen ──────────────────────────────────────────────────

DB_SCHEMA = """
SQLite-Datenbank für Dokumente der Familie Janning.

Tabelle: dokumente
  id, dateiname, rechnungsdatum TEXT (Format DD.MM.YYYY),
  kategorie TEXT (z.B. 'krankenversicherung', 'versicherung', 'finanzen', 'fahrzeuge',
    'persoenlich', 'familie', 'fengshui', 'immobilien_eigen', 'immobilien_vermietet',
    'garten', 'italien', 'business', 'digitales', 'wissen', 'reisen',
    'bedienungsanleitung', 'archiv'),
  typ TEXT (bei KV z.B. 'leistungsabrechnung_reinhard', 'arztrechnung', 'rezept';
    bei Versicherung z.B. 'versicherungsschein', 'beitragsanpassung';
    bei anderen Kategorien noch nicht definiert),
  absender TEXT, adressat ('Reinhard' | 'Marion'), konfidenz ('hoch'|'mittel'|'niedrig')

Tabelle: rechnungen
  id, dokument_id (FK dokumente.id), rechnungsbetrag REAL, faelligkeitsdatum TEXT,
  status ('offen' | 'erstattet' | 'teilweise_erstattet'), erstattungsdatum TEXT

Tabelle: erstattungspositionen
  id, dokument_id (FK dokumente.id), rechnung_id (FK rechnungen.id),
  leistungserbringer TEXT, zeitraum TEXT,
  rechnungsbetrag REAL, erstattungsbetrag REAL, erstattungsprozent REAL

Tabelle: aussteller
  id, name, typ, strasse, plz, ort, telefon, email, notizen

Tabelle: klassifikations_historie
  id, dokument_id (FK dokumente.id), timestamp TEXT (datetime),
  llm_model TEXT, translate_model TEXT,
  lang_detected TEXT (z.B. 'de', 'it', 'en'), lang_prob REAL (0.0–1.0),
  duration_ms INTEGER (LLM-Antwortzeit in Millisekunden),
  raw_response TEXT (LLM-Rohantwort, auf 4000 Zeichen begrenzt),
  final_category TEXT, final_type TEXT,
  konfidenz_category TEXT, konfidenz_type TEXT, konfidenz_absender TEXT,
  konfidenz_adressat TEXT, konfidenz_datum TEXT (je 'hoch'|'mittel'|'niedrig'),
  korrektur_von_user INTEGER (0=LLM-Lauf, 1=manuelle Korrektur)

Wichtige Kontextinfos:
- Reinhard → Gothaer Krankenversicherung (leistungsabrechnung_reinhard)
- Marion   → HUK-COBURG Krankenversicherung (leistungsabrechnung_marion)
- Jahresfilter: rechnungsdatum LIKE '%2024'
- SUM/AVG auf rechnungsbetrag immer mit ROUND(...,2)
- Hit-Rate: Dokumente ohne nachfolgende Korrektur gelten als korrekt klassifiziert
"""

# ── Datenbank ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with get_db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS dokumente (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                dateiname      TEXT NOT NULL UNIQUE,
                pdf_hash       TEXT,
                rechnungsdatum TEXT,
                kategorie      TEXT,
                typ            TEXT,
                absender       TEXT,
                adressat       TEXT,
                konfidenz      TEXT,
                erstellt_am    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS rechnungen (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                dokument_id       INTEGER NOT NULL REFERENCES dokumente(id),
                rechnungsbetrag   REAL,
                faelligkeitsdatum TEXT,
                status            TEXT DEFAULT 'offen',
                erstattungsdatum  TEXT
            );

            CREATE TABLE IF NOT EXISTS erstattungspositionen (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                dokument_id        INTEGER NOT NULL REFERENCES dokumente(id),
                rechnung_id        INTEGER REFERENCES rechnungen(id),
                leistungserbringer TEXT,
                zeitraum           TEXT,
                rechnungsbetrag    REAL,
                erstattungsbetrag  REAL,
                erstattungsprozent REAL
            );

            CREATE TABLE IF NOT EXISTS aussteller (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL UNIQUE,
                typ      TEXT,
                strasse  TEXT,
                plz      TEXT,
                ort      TEXT,
                telefon  TEXT,
                email    TEXT,
                notizen  TEXT
            );

            CREATE TABLE IF NOT EXISTS aussteller_aliases (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                aussteller_id INTEGER NOT NULL REFERENCES aussteller(id) ON DELETE CASCADE,
                alias         TEXT NOT NULL UNIQUE
            );

            CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aussteller_aliases(alias);

            CREATE TABLE IF NOT EXISTS klassifikations_historie (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                dokument_id         INTEGER REFERENCES dokumente(id),
                timestamp           TEXT DEFAULT (datetime('now')),
                llm_model           TEXT,
                translate_model     TEXT,
                lang_detected       TEXT,
                lang_prob           REAL,
                duration_ms         INTEGER,
                raw_response        TEXT,
                final_category      TEXT,
                final_type          TEXT,
                konfidenz_category  TEXT,
                konfidenz_type      TEXT,
                konfidenz_absender  TEXT,
                konfidenz_adressat  TEXT,
                konfidenz_datum     TEXT,
                korrektur_von_user  INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_historie_dokument ON klassifikations_historie(dokument_id);
        """)
    # Migrationen: Spalten/Tabellen nachrüsten falls DB bereits existierte
    with get_db() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(rechnungen)")}
        if "erstattungsdatum" not in cols:
            con.execute("ALTER TABLE rechnungen ADD COLUMN erstattungsdatum TEXT")
            log.info("Migration: Spalte erstattungsdatum hinzugefügt")
        cols_dok = {r[1] for r in con.execute("PRAGMA table_info(dokumente)")}
        if "aussteller_id" not in cols_dok:
            con.execute("ALTER TABLE dokumente ADD COLUMN aussteller_id INTEGER REFERENCES aussteller(id)")
            log.info("Migration: Spalte aussteller_id in dokumente hinzugefügt")
        if "pdf_hash" not in cols_dok:
            con.execute("ALTER TABLE dokumente ADD COLUMN pdf_hash TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_dokumente_hash ON dokumente(pdf_hash)")
            log.info("Migration: Spalte pdf_hash + Index in dokumente hinzugefügt")
        # vault_pfad-Spalten (ggf. aus früheren Migrationen)
        if "vault_kategorie" not in cols_dok:
            con.execute("ALTER TABLE dokumente ADD COLUMN vault_kategorie TEXT")
        if "vault_typ" not in cols_dok:
            con.execute("ALTER TABLE dokumente ADD COLUMN vault_typ TEXT")
        if "vault_pfad" not in cols_dok:
            con.execute("ALTER TABLE dokumente ADD COLUMN vault_pfad TEXT")
    log.info(f"Datenbank initialisiert: {DB_FILE}")


def _md5_file(path: Path) -> str:
    """Berechnet MD5-Hash einer Datei (blockweise, speicherschonend)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_betrag(s: str | None) -> float | None:
    """Extrahiert float aus Betragsstring wie '33,06 EUR' oder '33.06'."""
    if not s:
        return None
    cleaned = re.sub(r"[^\d,.]", "", str(s)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def save_to_db(file_path: Path, result: dict) -> list[dict]:
    """
    Speichert Dokument in DB. Bei Leistungsabrechnung: Positionen abgleichen.
    Gibt Liste von Match-Infos zurück: [{leistungserbringer, betrag, prozent, matched}]
    """
    type_id = result.get("type_id", "")
    category_id = result.get("category_id", "")
    is_la = type_id in LEISTUNGSABRECHNUNG_TYPES
    is_versicherung = type_id in VERSICHERUNG_TYPES
    is_kv = category_id in ("krankenversicherung", "versicherung")

    # Hash berechnen (für Duplikat-Check und DB-Speicherung)
    pdf_hash: str | None = None
    try:
        pdf_hash = _md5_file(file_path)
    except Exception as e:
        log.warning(f"MD5-Hash konnte nicht berechnet werden: {e}")

    with get_db() as con:
        # Duplikat-Schutz 1: bereits verarbeiteter Dateiname
        existing = con.execute(
            "SELECT id FROM dokumente WHERE dateiname = ?", (file_path.name,)
        ).fetchone()
        if existing:
            log.info(f"Bereits in DB (Dateiname): {file_path.name} — überspringe DB-Insert")
            result["_dok_id"] = existing["id"]
            return []

        # Duplikat-Schutz 2: identischer PDF-Inhalt (anderer Dateiname)
        if pdf_hash:
            hash_existing = con.execute(
                "SELECT id, dateiname FROM dokumente WHERE pdf_hash = ?", (pdf_hash,)
            ).fetchone()
            if hash_existing:
                log.warning(
                    f"Duplikat erkannt (MD5 {pdf_hash[:8]}…): "
                    f"{file_path.name} ist identisch mit {hash_existing['dateiname']} "
                    f"(id={hash_existing['id']}) — überspringe"
                )
                result["_dok_id"] = hash_existing["id"]
                result["_is_hash_duplicate"] = True
                return []

        # 1. Dokument speichern
        cur = con.execute(
            """INSERT INTO dokumente
               (dateiname, pdf_hash, rechnungsdatum, kategorie, typ, absender, adressat, konfidenz)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_path.name,
                pdf_hash,
                result.get("rechnungsdatum"),
                category_id,
                type_id,
                result.get("absender"),
                result.get("adressat"),
                result.get("konfidenz"),
            )
        )
        dok_id = cur.lastrowid
        result["_dok_id"] = dok_id

        # 2. Rechnung oder Erstattungspositionen (nur für KV-Kategorien)
        match_infos = []

        if not is_kv:
            # Nicht-KV-Dokument: nur Dokument speichern, keine Rechnungs-Logik
            log.info(f"Dokument in DB: {file_path.name} → {category_id}")
            return match_infos

        if not is_la and not is_versicherung:
            # Arztrechnung / Rezept / sonstige → Rechnung anlegen
            con.execute(
                """INSERT INTO rechnungen (dokument_id, rechnungsbetrag, faelligkeitsdatum)
                   VALUES (?, ?, ?)""",
                (
                    dok_id,
                    _parse_betrag(result.get("rechnungsbetrag")),
                    result.get("faelligkeitsdatum"),
                )
            )
            log.info(f"Rechnung in DB: {file_path.name}")

        else:
            # Leistungsabrechnung → Positionen abgleichen
            positionen = result.get("positionen") or []
            adressat = result.get("adressat")

            for pos in positionen:
                pos_betrag = _parse_betrag(str(pos.get("rechnungsbetrag", "")))
                pos_erstattung = _parse_betrag(str(pos.get("erstattungsbetrag", "")))
                leistungserbringer = pos.get("leistungserbringer", "")
                zeitraum = pos.get("zeitraum", "")

                prozent = None
                if pos_betrag and pos_erstattung and pos_betrag > 0:
                    prozent = round(pos_erstattung / pos_betrag * 100, 1)

                # Match-Suche: adressat + Betrag ±1 EUR + Status offen
                rechnung_row = None
                if pos_betrag and adressat:
                    rechnung_row = con.execute(
                        """SELECT r.id FROM rechnungen r
                           JOIN dokumente d ON d.id = r.dokument_id
                           WHERE d.adressat = ?
                             AND ABS(r.rechnungsbetrag - ?) <= 1.0
                             AND r.status = 'offen'
                           ORDER BY r.id DESC LIMIT 1""",
                        (adressat, pos_betrag)
                    ).fetchone()

                rechnung_id = None
                if rechnung_row:
                    rechnung_id = rechnung_row["id"]
                    new_status = "erstattet" if prozent and prozent >= 99 else "teilweise_erstattet"
                    con.execute(
                        "UPDATE rechnungen SET status = ?, erstattungsdatum = ? WHERE id = ?",
                        (new_status, result.get("rechnungsdatum"), rechnung_id)
                    )

                con.execute(
                    """INSERT INTO erstattungspositionen
                       (dokument_id, rechnung_id, leistungserbringer, zeitraum,
                        rechnungsbetrag, erstattungsbetrag, erstattungsprozent)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (dok_id, rechnung_id, leistungserbringer, zeitraum,
                     pos_betrag, pos_erstattung, prozent)
                )

                match_infos.append({
                    "leistungserbringer": leistungserbringer,
                    "rechnungsbetrag":    pos_betrag,
                    "erstattungsbetrag":  pos_erstattung,
                    "prozent":            prozent,
                    "matched":            rechnung_id is not None,
                })

            log.info(f"Leistungsabrechnung in DB: {file_path.name} | "
                     f"{sum(1 for m in match_infos if m['matched'])}/{len(match_infos)} gematcht")

        return match_infos


def save_klassifikation_historie(dok_id: int | None, result: dict, korrektur: bool = False):
    """Schreibt einen Eintrag in klassifikations_historie.

    Bei Erst-Klassifikation: alle LLM-Felder + Per-Feld-Konfidenz.
    Bei Korrektur (korrektur=True): nur final_category/final_type + korrektur_von_user=1.
    """
    if dok_id is None:
        return
    try:
        with get_db() as con:
            con.execute(
                """INSERT INTO klassifikations_historie
                   (dokument_id, llm_model, translate_model, lang_detected, lang_prob,
                    duration_ms, raw_response, final_category, final_type,
                    konfidenz_category, konfidenz_type, konfidenz_absender,
                    konfidenz_adressat, konfidenz_datum, korrektur_von_user)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dok_id,
                    None if korrektur else OLLAMA_MODEL,
                    None if korrektur else OLLAMA_TRANSLATE_MODEL,
                    None if korrektur else result.get("_lang"),
                    None if korrektur else result.get("_lang_prob"),
                    None if korrektur else result.get("_duration_ms"),
                    None if korrektur else result.get("_raw_response"),
                    result.get("category_id") or result.get("final_category"),
                    result.get("type_id") or result.get("final_type"),
                    None if korrektur else result.get("konfidenz_category"),
                    None if korrektur else result.get("konfidenz_type"),
                    None if korrektur else result.get("konfidenz_absender"),
                    None if korrektur else result.get("konfidenz_adressat"),
                    None if korrektur else result.get("konfidenz_datum"),
                    1 if korrektur else 0,
                )
            )
    except Exception as e:
        log.warning(f"Fehler beim Schreiben der Klassifikations-Historie: {e}")


# ── Kategorien laden ───────────────────────────────────────────────────────────

def load_categories() -> dict:
    global LEISTUNGSABRECHNUNG_TYPES, VERSICHERUNG_TYPES, BRANCHEN_REGELN
    if not CONFIG_FILE.exists():
        log.warning(f"Config nicht gefunden: {CONFIG_FILE}")
        return {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cats = data.get("categories", {})

    # vault_folder-Mapping + TYPE_ROUTING aus YAML aufbauen
    for cat_id, cat in cats.items():
        if "vault_folder" in cat:
            CATEGORY_TO_VAULT_FOLDER[cat_id] = cat["vault_folder"]
        for t in cat.get("types", []):
            type_id = t.get("id")
            if not type_id:
                continue
            routing = {}
            if "vault_subfolder" in t:
                routing["vault_subfolder"] = t["vault_subfolder"]
            if "person_subfolder" in t:
                routing["person_subfolder"] = bool(t["person_subfolder"])
            if "adressat_fallback" in t:
                routing["adressat_fallback"] = t["adressat_fallback"]
            if "telegram_template" in t:
                routing["telegram_template"] = t["telegram_template"]
            if routing:
                TYPE_ROUTING[(cat_id, type_id)] = routing

    # special_groups → globale Sets (ersetzt Hardcodes)
    special_groups = data.get("special_groups", {})
    if special_groups.get("leistungsabrechnung"):
        LEISTUNGSABRECHNUNG_TYPES = set(special_groups["leistungsabrechnung"])
    if special_groups.get("versicherung_dokument"):
        VERSICHERUNG_TYPES = set(special_groups["versicherung_dokument"])

    # branchen_regeln → globale Liste
    BRANCHEN_REGELN = data.get("branchen_regeln", []) or []

    log.info(
        f"Kategorien geladen: {list(cats.keys())} | "
        f"LA-Typen: {len(LEISTUNGSABRECHNUNG_TYPES)} | "
        f"Vers-Typen: {len(VERSICHERUNG_TYPES)} | "
        f"Type-Routing: {len(TYPE_ROUTING)} Einträge | "
        f"Branchen-Regeln: {len(BRANCHEN_REGELN)}"
    )

    # Vault-Ordner-Validierung beim Start
    if VAULT_ROOT:
        missing = []
        seen_folders: set[str] = set()
        for cat_id, cat in cats.items():
            folder = cat.get("vault_folder")
            if folder and folder not in seen_folders:
                seen_folders.add(folder)
                if not (VAULT_ROOT / folder).exists():
                    missing.append(folder)
        if missing:
            log.warning(f"Vault-Ordner fehlen: {missing} — betroffene Dokumente landen in 00 Inbox")

    return cats


def build_category_description(categories: dict) -> str:
    lines = []
    for cat_id, cat in categories.items():
        desc = cat.get("description", "")
        desc_str = f" — {desc}" if desc else ""
        lines.append(f"\nKategorie: {cat['label']} (id: {cat_id}){desc_str}")
        for t in cat.get("types", []):
            hints = ", ".join(t.get("hints", []))
            lines.append(f"  - Typ: {t['label']} (id: {t['id']}) | Erkennungshinweise: {hints}")
    return "\n".join(lines)

# ── Queue ──────────────────────────────────────────────────────────────────────

file_queue: queue.Queue = queue.Queue()

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str, chat_id: str | None = None, reply_markup: dict | None = None) -> int | None:
    """Sendet Telegram-Nachricht, optional mit Inline-Keyboard. Gibt message_id zurück."""
    if not TELEGRAM_TOKEN:
        log.warning("Telegram nicht konfiguriert.")
        return None
    target = chat_id or TELEGRAM_CHAT
    if not target:
        return None
    payload = {"chat_id": target, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if not r.ok:
            log.warning(f"Telegram Fehler: {r.text[:200]}")
            return None
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}")
        return None


def tg_edit_message(chat_id: str, message_id: int, text: str, reply_markup: dict | None = None):
    """Bearbeitet eine bestehende Telegram-Nachricht."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json=payload, timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram Edit Fehler: {e}")


def tg_send_document(file_path: Path, caption: str = "", chat_id: str | None = None):
    """Sendet eine Datei (PDF) als Dokument im Telegram-Chat."""
    if not TELEGRAM_TOKEN:
        return
    target = chat_id or TELEGRAM_CHAT
    if not target:
        return
    try:
        with open(file_path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": target, "caption": caption, "parse_mode": "HTML"},
                files={"document": (file_path.name, f)},
                timeout=30,
            )
    except Exception as e:
        log.warning(f"Telegram Dokument-Versand Fehler: {e}")


def tg_answer_callback(callback_query_id: str, text: str = ""):
    """Bestätigt einen Callback-Query (entfernt Ladeindikator)."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10
        )
    except Exception:
        pass


# ── Telegram Inline-Keyboards & Korrektur ─────────────────────────────────────

def build_confirm_keyboard(doc_id: int) -> dict:
    """Baut Inline-Keyboard mit OK/Korrigieren-Buttons."""
    return {"inline_keyboard": [[
        {"text": "✅ Passt", "callback_data": f"ok:{doc_id}"},
        {"text": "✏️ Korrigieren", "callback_data": f"cat:{doc_id}"},
    ]]}


def build_category_keyboard(doc_id: int) -> dict:
    """Baut Inline-Keyboard mit allen Kategorien (2 Spalten)."""
    cats = load_categories()
    buttons = []
    row = []
    for cat_id, cat in cats.items():
        row.append({"text": cat["label"], "callback_data": f"sc:{doc_id}:{cat_id}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "❌ Abbrechen", "callback_data": f"cancel:{doc_id}"}])
    return {"inline_keyboard": buttons}


def build_type_keyboard(doc_id: int, cat_id: str) -> dict:
    """Baut Inline-Keyboard mit Typen einer Kategorie."""
    cats = load_categories()
    cat = cats.get(cat_id, {})
    types = cat.get("types", [])
    buttons = []
    if types:
        row = []
        for t in types:
            # Callback-Daten: max 64 Bytes — kürze type_id falls nötig
            cb = f"st:{doc_id}:{cat_id}:{t['id']}"
            if len(cb.encode()) <= 64:
                row.append({"text": t["label"], "callback_data": cb})
            else:
                # Fallback: kürze type_id auf 20 Zeichen
                row.append({"text": t["label"], "callback_data": f"st:{doc_id}:{cat_id}:{t['id'][:20]}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    else:
        # Kategorie ohne Typen → direkt als "allgemein" setzen
        buttons.append([{"text": "✅ Allgemein", "callback_data": f"st:{doc_id}:{cat_id}:allgemein"}])
    buttons.append([{"text": "⬅️ Zurück", "callback_data": f"cat:{doc_id}"}])
    return {"inline_keyboard": buttons}


def handle_correction(doc_id: int, new_cat: str, new_type: str) -> str:
    """Korrigiert Kategorie/Typ: DB updaten + MD im Vault verschieben."""
    cats = load_categories()
    cat_def = cats.get(new_cat, {})
    cat_label = cat_def.get("label", new_cat)
    type_label = new_type
    for t in cat_def.get("types", []):
        if t["id"] == new_type:
            type_label = t["label"]
            break

    with get_db() as con:
        row = con.execute(
            "SELECT dateiname, kategorie, typ, adressat, vault_pfad FROM dokumente WHERE id = ?",
            (doc_id,)
        ).fetchone()
        if not row:
            return f"❌ Dokument {doc_id} nicht gefunden"

        old_cat = row["kategorie"]
        old_type = row["typ"]
        old_vault_pfad = row["vault_pfad"]
        dateiname = row["dateiname"]
        adressat = row["adressat"] or ""

        # Jahr aus altem Pfad extrahieren oder aus Dateiname
        year_match = re.search(r"/(\d{4})/", old_vault_pfad or "")
        if year_match:
            year = year_match.group(1)
        else:
            m = re.match(r"(\d{4})", dateiname)
            year = m.group(1) if m else datetime.now().strftime("%Y")

        # MD-Dateiname aus vault_pfad extrahieren
        md_filename = Path(old_vault_pfad).name if old_vault_pfad else f"{dateiname}.md"
        # Neuen Vault-Pfad mit einheitlicher Logik berechnen
        new_vault_pfad = build_vault_path(new_cat, new_type, adressat, year, md_filename)

        # DB updaten
        con.execute(
            "UPDATE dokumente SET kategorie=?, typ=?, vault_kategorie=?, vault_typ=?, vault_pfad=? WHERE id=?",
            (new_cat, new_type, new_cat, new_type, new_vault_pfad, doc_id)
        )
        save_klassifikation_historie(
            doc_id,
            {"final_category": new_cat, "final_type": new_type},
            korrektur=True,
        )

    # MD-Datei im Vault verschieben
    if old_vault_pfad and VAULT_ROOT:
        old_md = VAULT_ROOT / old_vault_pfad
        new_md = VAULT_ROOT / new_vault_pfad
        if old_md.exists() and old_md != new_md:
            new_md.parent.mkdir(parents=True, exist_ok=True)
            # Frontmatter aktualisieren
            try:
                content = old_md.read_text(encoding="utf-8")
                if content.startswith("---\n"):
                    # Frontmatter ersetzen
                    end = content.index("---", 4)
                    frontmatter = content[4:end]
                    rest = content[end + 3:]
                    frontmatter = re.sub(r"(?m)^kategorie:.*$", f"kategorie: {cat_label}", frontmatter)
                    frontmatter = re.sub(r"(?m)^kategorie_id:.*$", f"kategorie_id: {new_cat}", frontmatter)
                    frontmatter = re.sub(r"(?m)^typ:.*$", f"typ: {type_label}", frontmatter)
                    frontmatter = re.sub(r"(?m)^typ_id:.*$", f"typ_id: {new_type}", frontmatter)
                    content = f"---\n{frontmatter}---{rest}"
                new_md.write_text(content, encoding="utf-8")
                old_md.unlink()
                # Leeren Quellordner aufräumen
                try:
                    old_md.parent.rmdir()
                except OSError:
                    pass
                log.info(f"Korrektur: MD verschoben {old_vault_pfad} → {new_vault_pfad}")
            except Exception as e:
                log.warning(f"Fehler beim Verschieben der MD: {e}")
                # DB ist bereits aktualisiert, MD manuell verschieben
                return f"⚠️ DB aktualisiert, aber MD-Verschiebung fehlgeschlagen: {e}"

    old_label = f"{old_cat}/{old_type}"
    new_label = f"{new_cat}/{new_type}"
    return f"✅ Korrigiert: {old_label} → <b>{new_label}</b>\n📄 {dateiname}"


# ── NL-Datenbankabfrage ────────────────────────────────────────────────────────

def _get_available_ollama_model() -> str:
    """Gibt das aktuell geladene Ollama-Modell zurück, falls OLLAMA_MODEL nicht geladen ist."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        if r.ok:
            models = r.json().get("models", [])
            loaded = [m["name"] for m in models]
            if OLLAMA_MODEL in loaded:
                return OLLAMA_MODEL
            if loaded:
                log.info(f"NL-Query: {OLLAMA_MODEL} nicht geladen — verwende {loaded[0]}")
                return loaded[0]
    except Exception:
        pass
    return OLLAMA_MODEL


def query_db_with_nl(question: str) -> str:
    """Natürlichsprachliche Frage → Ollama generiert SQL → Ergebnis als Text."""
    prompt = f"""Du bist ein SQL-Experte. Schreibe eine SQLite-SELECT-Abfrage für folgende Frage.

Datenbankschema:
{DB_SCHEMA}

Frage: {question}

Regeln:
- Antworte NUR mit der SQL-Abfrage, kein erklärender Text
- Kein Markdown, keine Backticks, kein ```sql
- Nur SELECT (kein INSERT/UPDATE/DELETE)
- Maximal 50 Zeilen (LIMIT 50)
- Beträge mit ROUND(...,2)
- Bei Datumsfiltern: SUBSTR(rechnungsdatum, 7, 4) = '2024' für Jahrfilter"""

    try:
        model = _get_available_ollama_model()
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=60,
        )
        sql = r.json().get("response", "").strip()
        sql = re.sub(r'```sql\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'```\s*', '', sql).strip()
    except Exception as e:
        return f"❌ SQL-Generierung fehlgeschlagen: {e}"

    if not re.match(r'\s*SELECT', sql, re.IGNORECASE):
        return f"❌ Ungültige SQL-Abfrage:\n{sql[:200]}"

    try:
        with get_db() as con:
            cur = con.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    except Exception as e:
        return f"❌ SQL-Fehler: {e}\n\nSQL: {sql}"

    if not rows:
        return "Keine Ergebnisse gefunden."

    # Sonderfall: eine Zeile, eine Spalte → kompakter Output
    if len(rows) == 1 and len(cols) == 1:
        val = rows[0][0]
        val_str = "–" if val is None else str(val)
        return f"<b>{cols[0]}</b>: {val_str}"

    # Mehrere Zeilen/Spalten → Tabelle
    def _fmt(v) -> str:
        return "–" if v is None else str(v)

    # Spaltenbreiten berechnen
    widths = [len(c) for c in cols]
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(_fmt(v)))

    def _row(values):
        return "  ".join(_fmt(v).ljust(widths[i]) for i, v in enumerate(values))

    lines = [_row(cols), "─" * sum(widths + [2] * (len(cols) - 1))]
    for row in rows:
        lines.append(_row(row))

    header = f"📊 {len(rows)} Ergebnis{'se' if len(rows) != 1 else ''}"
    return f"{header}\n\n<pre>{chr(10).join(lines)}</pre>"


# ── Telegram-Polling ───────────────────────────────────────────────────────────

def tg_poll():
    """Empfängt Telegram-Updates: /frage-Befehle + Callback Queries für Korrekturen."""
    if not TELEGRAM_TOKEN:
        return
    offset = 0
    log.info("Telegram-Polling gestartet.")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            if not r.ok:
                time.sleep(5)
                continue
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1

                # ── Callback Queries (Inline-Buttons) ──
                cb = update.get("callback_query")
                if cb:
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    cb_msg = cb.get("message", {})
                    cb_chat = str(cb_msg.get("chat", {}).get("id", ""))
                    cb_msg_id = cb_msg.get("message_id")

                    if cb_chat != TELEGRAM_CHAT:
                        tg_answer_callback(cb_id, "⛔ Nicht autorisiert")
                        continue

                    try:
                        if cb_data.startswith("ok:"):
                            # Bestätigung — Buttons entfernen
                            tg_answer_callback(cb_id, "✅")
                            tg_edit_message(cb_chat, cb_msg_id,
                                            cb_msg.get("text", "") + "\n\n✅ Bestätigt",
                                            reply_markup={"inline_keyboard": []})

                        elif cb_data.startswith("cat:"):
                            # Kategorie-Auswahl anzeigen
                            doc_id = int(cb_data.split(":")[1])
                            tg_answer_callback(cb_id)
                            tg_edit_message(cb_chat, cb_msg_id,
                                            f"🗂 Kategorie wählen für Dokument #{doc_id}:",
                                            reply_markup=build_category_keyboard(doc_id))

                        elif cb_data.startswith("sc:"):
                            # Kategorie gewählt → Typen anzeigen
                            parts = cb_data.split(":")
                            doc_id = int(parts[1])
                            cat_id = parts[2]
                            cats = load_categories()
                            cat_label = cats.get(cat_id, {}).get("label", cat_id)
                            tg_answer_callback(cb_id)
                            tg_edit_message(cb_chat, cb_msg_id,
                                            f"📁 Typ wählen für <b>{cat_label}</b>:",
                                            reply_markup=build_type_keyboard(doc_id, cat_id))

                        elif cb_data.startswith("st:"):
                            # Typ gewählt → Korrektur durchführen
                            parts = cb_data.split(":")
                            doc_id = int(parts[1])
                            cat_id = parts[2]
                            type_id = parts[3]
                            tg_answer_callback(cb_id, "⏳ Korrigiere...")
                            result_text = handle_correction(doc_id, cat_id, type_id)
                            tg_edit_message(cb_chat, cb_msg_id, result_text,
                                            reply_markup={"inline_keyboard": []})

                        elif cb_data.startswith("cancel:"):
                            tg_answer_callback(cb_id, "Abgebrochen")
                            tg_edit_message(cb_chat, cb_msg_id,
                                            cb_msg.get("text", "") + "\n\n❌ Abgebrochen",
                                            reply_markup={"inline_keyboard": []})

                    except Exception as e:
                        log.warning(f"Callback-Fehler: {e}")
                        tg_answer_callback(cb_id, f"❌ Fehler: {str(e)[:100]}")
                    continue

                # ── Normale Nachrichten ──
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                log.info(f"TG-Poll: chat_id={chat_id!r} erwartet={TELEGRAM_CHAT!r} text={text[:60]!r}")

                if chat_id != TELEGRAM_CHAT:
                    log.warning(f"TG-Poll: chat_id-Mismatch — ignoriere Nachricht")
                    continue

                if text.lower().startswith("/frage"):
                    # Kommando mit oder ohne Leerzeichen / @botname abschneiden
                    question = re.sub(r"^/frage\S*\s*", "", text, flags=re.IGNORECASE).strip()
                    if not question:
                        tg_send("❓ Bitte eine Frage angeben, z. B.: <code>/frage Wie viele Dokumente gab es diesen Monat?</code>")
                        continue
                    log.info(f"TG /frage: {question!r}")
                    tg_send(f"🔍 <i>{question}</i>", chat_id=chat_id)
                    # In eigenem Thread ausführen — blockiert den Poll-Loop nicht
                    def _run_query(q=question, cid=chat_id):
                        res = query_db_with_nl(q)
                        tg_send(res[:4096], chat_id=cid)
                    threading.Thread(target=_run_query, daemon=True).start()

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.warning(f"Telegram-Poll Fehler: {e}")
            time.sleep(5)


# ── REST-API (für Wilson/Open WebUI) ──────────────────────────────────────────

from urllib.parse import urlparse, parse_qs

class _ApiHandler(BaseHTTPRequestHandler):

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # GET /api/categories — alle Kategorien + Typen
        if path == "/api/categories":
            cats = load_categories()
            result = {}
            for cat_id, cat in cats.items():
                result[cat_id] = {
                    "label": cat.get("label", cat_id),
                    "vault_folder": cat.get("vault_folder", "00 Inbox"),
                    "types": [{"id": t["id"], "label": t["label"]} for t in cat.get("types", [])],
                }
            self._json_response(result)

        # GET /api/recent?limit=10 — letzte Dokumente
        elif path == "/api/recent":
            limit = int(params.get("limit", [10])[0])
            with get_db() as con:
                rows = con.execute(
                    "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, konfidenz, vault_pfad, erstellt_am "
                    "FROM dokumente ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            self._json_response([dict(r) for r in rows])

        # GET /api/document/<id> — Dokument-Details + MD-Inhalt
        elif path.startswith("/api/document/"):
            try:
                doc_id = int(path.split("/")[-1])
            except ValueError:
                self._json_response({"error": "Ungültige ID"}, 400); return
            with get_db() as con:
                row = con.execute(
                    "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, konfidenz, vault_pfad, erstellt_am "
                    "FROM dokumente WHERE id = ?", (doc_id,)
                ).fetchone()
            if not row:
                self._json_response({"error": "Dokument nicht gefunden"}, 404); return
            doc = dict(row)
            # MD-Inhalt laden
            vault_pfad = doc.get("vault_pfad")
            if vault_pfad and VAULT_ROOT:
                md_path = VAULT_ROOT / vault_pfad
                if md_path.exists():
                    doc["md_content"] = md_path.read_text(encoding="utf-8", errors="replace")
                else:
                    doc["md_content"] = None
            self._json_response(doc)

        # GET /api/search?q=vodafone&limit=10 — Dokumente suchen
        elif path == "/api/search":
            q = params.get("q", [""])[0]
            limit = int(params.get("limit", [10])[0])
            if not q:
                self._json_response({"error": "Parameter q fehlt"}, 400); return
            with get_db() as con:
                rows = con.execute(
                    "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, vault_pfad "
                    "FROM dokumente WHERE dateiname LIKE ? OR absender LIKE ? OR kategorie LIKE ? OR typ LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", limit)
                ).fetchall()
            self._json_response([dict(r) for r in rows])

        else:
            self._json_response({"error": "Unbekannter Endpunkt"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        # POST /api/query — NL-Datenbankabfrage (bestehend)
        if path == "/api/query":
            try:
                data = self._read_body()
                question = data.get("question", "").strip()
            except Exception:
                self._json_response({"error": "Ungültiger Body"}, 400); return
            if not question:
                self._json_response({"error": "question fehlt"}, 400); return
            result = query_db_with_nl(question)
            self._json_response({"result": result})

        # POST /api/correct — Kategorie/Typ korrigieren
        elif path == "/api/correct":
            try:
                data = self._read_body()
            except Exception:
                self._json_response({"error": "Ungültiger Body"}, 400); return
            doc_id = data.get("doc_id")
            category = data.get("category")
            type_id = data.get("type_id", "allgemein")
            if not doc_id or not category:
                self._json_response({"error": "doc_id und category sind Pflicht"}, 400); return
            result = handle_correction(int(doc_id), category, type_id)
            # Auch Telegram benachrichtigen
            tg_send(result)
            self._json_response({"result": result})

        else:
            self._json_response({"error": "Unbekannter Endpunkt"}, 404)

    def log_message(self, fmt, *args):
        pass  # Kein Access-Log-Spam


def start_api_server():
    server = HTTPServer(("0.0.0.0", API_PORT), _ApiHandler)
    log.info(f"API gestartet auf Port {API_PORT}")
    server.serve_forever()

# ── Docling ────────────────────────────────────────────────────────────────────

def wait_for_file_stable(path: Path, timeout=30) -> bool:
    last_size = -1
    for _ in range(timeout):
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return False
        if current_size == last_size and current_size > 0:
            return True
        last_size = current_size
        time.sleep(1)
    return False


def wait_for_docling(max_retries=30, delay=10):
    for i in range(max_retries):
        try:
            r = requests.get(f"{DOCLING_URL}/health", timeout=5)
            if r.status_code == 200:
                log.info("Docling Serve erreichbar.")
                return True
        except requests.exceptions.ConnectionError:
            pass
        log.info(f"Warte auf Docling Serve... ({i+1}/{max_retries})")
        time.sleep(delay)
    return False


def convert_to_markdown(file_path: Path) -> str | None:
    log.info(f"Konvertiere mit Docling: {file_path.name}")
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                f"{DOCLING_URL}/v1/convert/file",
                files={"files": (file_path.name, f, "application/octet-stream")},
                data={"to_formats": "md", "image_export_mode": "placeholder"},
                timeout=600,
            )
        if r.status_code != 200:
            log.error(f"Docling Fehler {r.status_code}: {r.text[:200]}")
            return None
        result = r.json()
        if result.get("status") != "success":
            log.error(f"Docling Status nicht success: {result.get('status')}")
            return None
        return result.get("document", {}).get("md_content", "")
    except requests.exceptions.Timeout:
        log.error(f"Docling Timeout bei {file_path.name}")
        return None
    except Exception as e:
        log.error(f"Docling Fehler: {e}")
        return None

# ── Ollama Klassifizierung ─────────────────────────────────────────────────────

def _fix_llm_json(s: str) -> str:
    """Korrigiert typische LLM-JSON-Fehler vor dem Parsing."""
    # Steuerzeichen entfernen (außer Tab/LF/CR)
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", s)
    # Python-Literale → JSON
    s = re.sub(r'\bNone\b', 'null', s)
    s = re.sub(r'\bTrue\b', 'true', s)
    s = re.sub(r'\bFalse\b', 'false', s)
    # Deutsches Dezimalkomma in Zahlenwerten (nicht in Strings): 456,64 → 456.64
    s = re.sub(r'(?<=:\s)(\d+),(\d{1,2})(?=\s*[,\}\]])', r'\1.\2', s)
    # Trailing commas vor } oder ]
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    return s


def sanitize_for_ollama(text: str) -> str:
    """Entfernt OCR-Artefakte (arabische/kyrillische Zeichen, Steuerzeichen).
    Behält Deutsch, Zahlen, Tabellen und gängige Satzzeichen."""
    cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\x7E\u00C0-\u024F\u2019\u201C\u201D€|•\-]", " ", text)
    cleaned = re.sub(r" {3,}", "  ", cleaned)
    return cleaned


def detect_document_language(md_content: str) -> tuple[str, float]:
    """Erkennt die dominante Sprache des Dokuments. Gibt (lang_code, prob) zurück.
    Bei zu kurzem Text oder Fehler: ('de', 0.0) — wir nehmen Deutsch an, kein Translate-Pass."""
    text = sanitize_for_ollama(md_content)[:3000].strip()
    if len(text) < 200:
        return ("de", 0.0)
    try:
        candidates = detect_langs(text)
        if not candidates:
            return ("de", 0.0)
        top = candidates[0]
        return (top.lang, top.prob)
    except LangDetectException:
        return ("de", 0.0)


def translate_to_german(md_content: str, source_lang: str) -> str | None:
    """Übersetzt md_content nach Deutsch via Ollama. Behält Zahlen/Datümer/Eigennamen literal.
    Gibt None bei Fehler — Aufrufer arbeitet dann mit Original weiter."""
    text = sanitize_for_ollama(md_content)[:6000]
    prompt = f"""Du bist ein Fachübersetzer. Übersetze den folgenden Text wörtlich nach Deutsch.

REGELN:
- Eigennamen, Firmennamen, Adressen, IBANs, E-Mails, URLs: NICHT übersetzen, exakt übernehmen.
- Zahlen, Datümer, Beträge, Währungen: exakt übernehmen (keine Umrechnung, keine Formatänderung).
- Tabellen-Struktur und Zeilenumbrüche beibehalten.
- KEINE Erklärung, KEINE Kommentare, KEIN "Hier ist die Übersetzung". Nur der übersetzte Text.

Quellsprache: {source_lang}
Zieltext (Deutsch):

---
{text}
---"""
    log.info(f"Translate-Modell: {OLLAMA_TRANSLATE_MODEL}")
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_TRANSLATE_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_ctx": 8192},
            },
            timeout=300,
        )
        if r.status_code != 200:
            log.error(f"Translate Ollama Fehler {r.status_code}: {r.text[:200]}")
            return None
        translated = r.json().get("response", "").strip()
        if not translated or len(translated) < 50:
            log.warning(f"Translate-Output zu kurz ({len(translated)} chars) — verwerfe")
            return None
        return translated
    except Exception as e:
        log.error(f"Translate-Fehler: {e}")
        return None


def extract_document_header(md_content: str) -> dict:
    """Extrahiert Absender/Empfänger aus den ersten ~40 Zeilen (regex, kein LLM).

    Rückgabe: {"absender": {...}, "empfaenger": {...}}. Jedes Unter-Dict hat
    firma, name, strasse, plz, ort, land — jeweils str oder None. Wirft nie.
    """
    empty = {"firma": None, "name": None, "strasse": None, "plz": None, "ort": None, "land": None}
    try:
        lines = [l.rstrip() for l in md_content.splitlines()[:40]]
    except Exception:
        return {"absender": dict(empty), "empfaenger": dict(empty)}

    plz_re = re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][\wäöüß\-\.'/]+(?:\s+[A-ZÄÖÜa-zäöüß\-\.'/]+){0,3})")
    firma_re = re.compile(
        r"\b(GmbH|AG|KG|OHG|mbH|e\.?\s*V\.?|S\.?R\.?L\.?|SRL|S\.?p\.?A\.?|SpA|"
        r"S\.?N\.?C\.?|SNC|Srl|Cooperativa|Ges\.m\.b\.H)\b",
        re.IGNORECASE,
    )
    person_re = re.compile(r"\bJanning\b", re.IGNORECASE)
    strasse_re = re.compile(
        r"\b(straße|strasse|str\.|weg|gasse|platz|allee|via|viale|piazza|corso|"
        r"largo|vicolo|contrada)\b",
        re.IGNORECASE,
    )

    # Gruppiere in Blöcke (durch Leerzeilen getrennt)
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in lines:
        s = raw.strip()
        if s:
            current.append(s)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    # Nur Blöcke mit PLZ sind Adresskandidaten
    candidates = [b for b in blocks if any(plz_re.search(l) for l in b)]
    absender_block: list[str] | None = None
    empfaenger_block: list[str] | None = None
    for block in candidates:
        joined = " ".join(block)
        has_firma = bool(firma_re.search(joined))
        has_person = bool(person_re.search(joined))
        if has_person and empfaenger_block is None:
            empfaenger_block = block
        elif has_firma and absender_block is None:
            absender_block = block
    # Fallback: wenn kein klarer Absender, nimm den ersten PLZ-Block, der nicht Empfänger ist
    if absender_block is None:
        for block in candidates:
            if block is not empfaenger_block:
                absender_block = block
                break

    def parse(block: list[str] | None) -> dict:
        if not block:
            return dict(empty)
        plz = ort = firma = name = strasse = None
        for l in block:
            m = plz_re.search(l)
            if m and not plz:
                plz, ort = m.group(1), m.group(2).strip()
        for l in block:
            if firma_re.search(l) and not firma:
                firma = l
        for l in block:
            if person_re.search(l) and not name:
                name = l
        for l in block:
            if plz_re.search(l):
                continue
            if l == firma or l == name:
                continue
            if strasse_re.search(l) or re.search(r"\d+\s*[a-z]?$", l):
                strasse = l
                break
        land = None
        if firma and re.search(r"\b(SRL|Srl|S\.?R\.?L\.?|SpA|S\.?p\.?A\.?|SNC|S\.?N\.?C\.?|Cooperativa)\b", firma):
            land = "IT"
        elif plz and plz.startswith("39"):
            land = "IT"
        elif plz:
            land = "DE"
        return {"firma": firma, "name": name, "strasse": strasse, "plz": plz, "ort": ort, "land": land}

    return {"absender": parse(absender_block), "empfaenger": parse(empfaenger_block)}


_IT_PERSON_CF_RE  = re.compile(r"\b([A-Z]{6}\d{2}[A-Z]\d{2}[A-Z0-9]\d{3}[A-Z])\b")
# Permissiver Fallback: 16 alphanumerische Zeichen direkt hinter "Cod. Fiscale"/"C.F."
# (OCR verwechselt z.B. O↔0, G↔6, 1↔I; strikter Regex greift dann nicht).
_IT_PERSON_CF_LOOSE_RE = re.compile(
    r"(?:Cod(?:ice|\.)?\s*Fiscale|C\.F\.)\s*[:\-]?\s*([A-Z0-9]{16})\b",
    re.IGNORECASE,
)
_IT_FIRMA_NUM_RE  = re.compile(
    r"(?:P(?:art|artita)?\.?\s*IVA|Cod(?:ice|\.)?\s*Fiscale|C\.F\.)"
    r"[^0-9]{0,30}?(\d{11})\b",
    re.IGNORECASE | re.DOTALL,
)
_DE_USTID_RE      = re.compile(r"\b(DE\d{9})\b")
_IBAN_RE          = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")


def extract_identifiers(md_content: str) -> dict:
    """Regex-basierte Extraktion strukturierter Identifier aus dem Dokumententext.

    Cod. Fiscale (IT-Personen): 6 Buchstaben + 2 Ziffern + 1 Buchstabe + 2 alphanumerisch + 3 Ziffern + 1 Buchstabe.
    Part. Iva / Cod. Fiscale Firma (IT, 11 Ziffern): nur kontextgeprüft (nur wenn in
    Nähe eines passenden Kürzels steht — 11 blanke Ziffern kommen in vielen Dokumenten vor).
    USt-IdNr (DE): `DE` + 9 Ziffern.
    IBAN: 2 Buchstaben + 2 Ziffern + 10–30 alphanumerisch.

    Rückgabe: Dict mit Listen. Ohne Duplikate, Reihenfolge stabil.
    """
    def _uniq(seq):
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    try:
        strict = _IT_PERSON_CF_RE.findall(md_content)
        loose  = [m.upper() for m in _IT_PERSON_CF_LOOSE_RE.findall(md_content)]
        cod_fiscale = _uniq(strict + loose)
        part_iva    = _uniq(_IT_FIRMA_NUM_RE.findall(md_content))
        ust_id      = _uniq(_DE_USTID_RE.findall(md_content))
        iban        = _uniq(_IBAN_RE.findall(md_content))
        # Eine 16-stellige Cod. Fiscale darf nicht versehentlich als IBAN auftauchen
        iban = [x for x in iban if x not in cod_fiscale]
        return {
            "cod_fiscale_person": cod_fiscale,
            "part_iva_firma":     part_iva,
            "ust_id_de":          ust_id,
            "iban":               iban,
        }
    except Exception as e:
        log.warning(f"Identifier-Extraktion fehlgeschlagen: {e}")
        return {"cod_fiscale_person": [], "part_iva_firma": [], "ust_id_de": [], "iban": []}


_personen_cache: dict | None = None
_tiere_cache: list | None = None
_absender_cache: list | None = None


def load_personen() -> dict:
    """Lädt personen.yaml einmal pro Prozess (persons + tiere getrennt gecacht)."""
    global _personen_cache, _tiere_cache
    if _personen_cache is not None:
        return _personen_cache
    if not PERSONEN_FILE.exists():
        log.info(f"personen.yaml nicht gefunden: {PERSONEN_FILE} — Personen-Resolver deaktiviert")
        _personen_cache = {}
        _tiere_cache = []
        return _personen_cache
    try:
        with open(PERSONEN_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _personen_cache = data.get("persons", {}) or {}
        _tiere_cache = data.get("tiere", []) or []
        log.info(f"Personen geladen: {list(_personen_cache.keys())}, Tiere: {[t.get('name') for t in _tiere_cache]}")
    except Exception as e:
        log.warning(f"personen.yaml fehlerhaft: {e} — Personen-Resolver deaktiviert")
        _personen_cache = {}
        _tiere_cache = []
    return _personen_cache


def load_tiere() -> list:
    """Lädt die tiere-Sektion aus personen.yaml (triggert Personen-Load)."""
    if _tiere_cache is None:
        load_personen()
    return _tiere_cache or []


def load_absender() -> list:
    """Lädt absender.yaml einmal pro Prozess."""
    global _absender_cache
    if _absender_cache is not None:
        return _absender_cache
    if not ABSENDER_FILE.exists():
        log.info(f"absender.yaml nicht gefunden: {ABSENDER_FILE} — Absender-Resolver deaktiviert")
        _absender_cache = []
        return _absender_cache
    try:
        with open(ABSENDER_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _absender_cache = data.get("absender", []) or []
        log.info(f"Absender geladen: {len(_absender_cache)} Einträge")
    except Exception as e:
        log.warning(f"absender.yaml fehlerhaft: {e} — Absender-Resolver deaktiviert")
        _absender_cache = []
    return _absender_cache


def resolve_adressat(identifiers: dict, md_content: str = "") -> dict | None:
    """Findet Adressat deterministisch.

    Reihenfolge:
    1. Cod. Fiscale / Steuer-ID (Primär)
    2. Tier-Name im Text → besitzer (Sekundär, nur wenn kein CF-Treffer)

    Rückgabe: {"person_key", "name", "via", "tier"?} oder None.
    """
    personen = load_personen()
    if not personen:
        return None
    for cf in identifiers.get("cod_fiscale_person", []):
        cf_upper = cf.upper()
        for key, info in personen.items():
            cf_list = [str(x).upper() for x in (info.get("cod_fiscale") or [])]
            if cf_upper in cf_list:
                return {"person_key": key, "name": info.get("name"), "via": f"cod_fiscale:{cf}"}

    if md_content:
        md_upper = md_content.upper()
        for tier in load_tiere():
            for alias in tier.get("aliases") or [tier.get("name", "")]:
                if alias and re.search(rf"\b{re.escape(alias.upper())}\b", md_upper):
                    besitzer_key = tier.get("besitzer")
                    info = personen.get(besitzer_key or "", {})
                    if info:
                        return {
                            "person_key": besitzer_key,
                            "name": info.get("name"),
                            "via": f"tier:{tier.get('name')}",
                            "tier": tier.get("name"),
                        }
    return None


def derive_tier(adressat_person_key: str | None, category_id: str, type_id: str | None) -> str | None:
    """Leitet Tier aus bekanntem Adressat ab (für familie/tierarztrechnung-Dokumente)."""
    if not adressat_person_key:
        return None
    if category_id != "familie" or type_id != "tierarztrechnung":
        return None
    for tier in load_tiere():
        if tier.get("besitzer") == adressat_person_key:
            return tier.get("name")
    return None


def resolve_absender(identifiers: dict, header: dict | None) -> dict | None:
    """Findet Absender über Part.Iva/USt-IdNr (Primär) oder Alias-Match (Sekundär).

    Rückgabe: {"id", "kategorie_hint", "typ_hint", "adressat_default", "land", "via"} oder None.
    """
    absender_list = load_absender()
    if not absender_list:
        return None

    def _mk_result(entry: dict, via: str) -> dict:
        return {
            "id": entry.get("id"),
            "kategorie_hint": entry.get("kategorie_hint"),
            "typ_hint": entry.get("typ_hint"),
            "adressat_default": entry.get("adressat_default"),
            "land": entry.get("land"),
            "via": via,
        }

    # 1. Primär: Part.Iva / USt-IdNr Match
    for piva in identifiers.get("part_iva_firma", []):
        for entry in absender_list:
            if piva in (entry.get("part_iva") or []):
                return _mk_result(entry, f"part_iva:{piva}")
    for ust in identifiers.get("ust_id_de", []):
        for entry in absender_list:
            if ust in (entry.get("ust_id") or []):
                return _mk_result(entry, f"ust_id:{ust}")

    # 2. Sekundär: Alias-Match (case-insensitive substring) auf header.absender.firma
    firma = ((header or {}).get("absender") or {}).get("firma") or ""
    if firma:
        firma_upper = firma.upper()
        for entry in absender_list:
            for alias in entry.get("aliases") or []:
                if alias and alias.upper() in firma_upper:
                    return _mk_result(entry, f"alias:{alias}")

    return None


_doc_types_cache: list | None = None


def load_doc_types() -> list:
    """Lädt doc_types.yaml einmal pro Prozess."""
    global _doc_types_cache
    if _doc_types_cache is not None:
        return _doc_types_cache
    if not DOC_TYPES_FILE.exists():
        log.info(f"doc_types.yaml nicht gefunden: {DOC_TYPES_FILE} — Dokumenttyp-Extraktor deaktiviert")
        _doc_types_cache = []
        return _doc_types_cache
    try:
        with open(DOC_TYPES_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _doc_types_cache = data.get("doc_types", []) or []
        log.info(f"Dokumenttypen geladen: {len(_doc_types_cache)} Einträge")
    except Exception as e:
        log.warning(f"doc_types.yaml fehlerhaft: {e} — Dokumenttyp-Extraktor deaktiviert")
        _doc_types_cache = []
    return _doc_types_cache


# Bank-Indikatoren für `nur_bei_absender: bank`
_BANK_KEYWORDS = re.compile(
    r"\b(Volksbank|Sparkasse|ING|HypoVereinsbank|Deutsche Bank|Commerzbank|DKB|Postbank"
    r"|Santander|Targobank|N26|Comdirect|Banca|Banco|Cassa Rurale|BCC|Credito|UniCredit"
    r"|Intesa|Raiffeisen|BNP|Société|IBAN[^A-Z]|Estratto conto)\b",
    re.IGNORECASE,
)


def extract_document_type(md_content: str) -> dict:
    """Keyword-basierte Dokumenttyp-Erkennung aus den ersten 20 Zeilen.

    Rückgabe:
    {
        "erkannter_typ": str | None,
        "erkannter_label": str | None,
        "quell_keyword": str | None,
        "zeile": int | None,
        "kategorie_hint": str | None,
        "nur_bei_absender": str | None,
        "alle_treffer": [{"typ", "keyword", "zeile", "prioritaet"}, ...]
    }
    """
    doc_types = load_doc_types()
    if not doc_types:
        return {
            "erkannter_typ": None, "erkannter_label": None,
            "quell_keyword": None, "zeile": None,
            "kategorie_hint": None, "nur_bei_absender": None,
            "alle_treffer": [],
        }

    lines = md_content.splitlines()[:20]
    alle_treffer: list[dict] = []

    try:
        for entry in doc_types:
            prio = entry.get("prioritaet", 2)
            for kw in entry.get("keywords", []):
                for lineno, line in enumerate(lines, start=1):
                    if kw.upper() in line.upper():
                        alle_treffer.append({
                            "typ": entry.get("typ"),
                            "label": entry.get("label"),
                            "keyword": kw,
                            "zeile": lineno,
                            "prioritaet": prio,
                            "kategorie_hint": entry.get("kategorie_hint"),
                            "nur_bei_absender": entry.get("nur_bei_absender"),
                        })
                        break  # ein Treffer pro Eintrag genügt

        # Sortieren: Priorität aufsteigend (1 = hoch), dann Zeile aufsteigend
        alle_treffer.sort(key=lambda x: (x["prioritaet"], x["zeile"]))

        if alle_treffer:
            bester = alle_treffer[0]
            return {
                "erkannter_typ": bester["typ"],
                "erkannter_label": bester["label"],
                "quell_keyword": bester["keyword"],
                "zeile": bester["zeile"],
                "kategorie_hint": bester["kategorie_hint"],
                "nur_bei_absender": bester["nur_bei_absender"],
                "alle_treffer": alle_treffer,
            }
    except Exception as e:
        log.warning(f"Dokumenttyp-Extraktion fehlgeschlagen: {e}")

    return {
        "erkannter_typ": None, "erkannter_label": None,
        "quell_keyword": None, "zeile": None,
        "kategorie_hint": None, "nur_bei_absender": None,
        "alle_treffer": [],
    }


def _format_doc_type_for_prompt(doc_type: dict, header: dict | None) -> str:
    """Rendert den erkannten Dokumenttyp als Prompt-Block."""
    if not doc_type or not doc_type.get("erkannter_typ"):
        return ""
    parts = [
        f"Erkannter Dokumenttyp (regex, Keyword='{doc_type['quell_keyword']}' in Zeile {doc_type['zeile']}): "
        f"{doc_type['erkannter_label']} (typ={doc_type['erkannter_typ']})"
    ]
    if doc_type.get("kategorie_hint"):
        parts.append(f"→ Kategorie-Hint: {doc_type['kategorie_hint']}")
    if doc_type.get("nur_bei_absender") == "bank":
        # Prüfen ob Header auf Bank hindeutet
        firma = ((header or {}).get("absender") or {}).get("firma") or ""
        if _BANK_KEYWORDS.search(firma):
            parts.append("→ Bank-Absender bestätigt — Typ gilt.")
        else:
            parts.append(
                "→ ACHTUNG: Typ 'kontoauszug' gilt NUR wenn Absender eine Bank ist. "
                "Prüfe Absender sorgfältig — IBAN-Nummern im Text allein reichen nicht."
            )
    return "\n".join(parts)


def _format_header_for_prompt(header: dict) -> str:
    """Formatiert den extrahierten Header menschenlesbar für den Klassifikations-Prompt."""
    def fmt(label: str, d: dict) -> str:
        parts = []
        if d.get("firma"): parts.append(f"Firma: {d['firma']}")
        if d.get("name"):  parts.append(f"Name: {d['name']}")
        if d.get("strasse"): parts.append(f"Strasse: {d['strasse']}")
        if d.get("plz") or d.get("ort"):
            parts.append(f"PLZ/Ort: {(d.get('plz') or '').strip()} {(d.get('ort') or '').strip()}".strip())
        if d.get("land"): parts.append(f"Land: {d['land']}")
        body = "\n  ".join(parts) if parts else "(nicht erkannt)"
        return f"{label}:\n  {body}"
    return f"{fmt('Absender', header.get('absender', {}))}\n{fmt('Empfänger', header.get('empfaenger', {}))}"


def _format_identifiers_for_prompt(
    identifiers: dict,
    adressat_match: dict | None,
    absender_match: dict | None,
) -> str:
    """Rendert deterministische Treffer als STRUKTURIERTE MERKMALE-Block."""
    lines: list[str] = []
    if adressat_match:
        lines.append(
            f"- Empfänger (deterministisch via {adressat_match['via']}): "
            f"{adressat_match['name']}"
        )
    if absender_match:
        parts = [f"ID={absender_match['id']}"]
        if absender_match.get("land"):
            parts.append(f"Land={absender_match['land']}")
        if absender_match.get("kategorie_hint"):
            parts.append(f"Kategorie-Hint={absender_match['kategorie_hint']}")
        if absender_match.get("typ_hint"):
            parts.append(f"Typ-Hint={absender_match['typ_hint']}")
        if absender_match.get("adressat_default"):
            parts.append(f"Adressat-Default={absender_match['adressat_default']}")
        lines.append(
            f"- Absender (via {absender_match['via']}): " + ", ".join(parts)
        )
    if identifiers.get("cod_fiscale_person"):
        lines.append(f"- Cod. Fiscale Personen im Dokument: {identifiers['cod_fiscale_person']}")
    if identifiers.get("part_iva_firma"):
        lines.append(f"- Part. Iva / Cod. Fiscale Firma: {identifiers['part_iva_firma']}")
    if identifiers.get("ust_id_de"):
        lines.append(f"- USt-IdNr DE: {identifiers['ust_id_de']}")
    return "\n".join(lines)


def classify_with_ollama(
    md_content: str,
    categories: dict,
    header: dict | None = None,
    identifiers: dict | None = None,
    adressat_match: dict | None = None,
    absender_match: dict | None = None,
    doc_type_info: dict | None = None,
) -> dict | None:
    cat_desc = build_category_description(categories)
    md_content = sanitize_for_ollama(md_content)

    # Kategorien mit Typ-Details (krankenversicherung, versicherung) bekommen erweiterte Regeln
    kv_rules = """
SPEZIALREGELN für Krankenversicherung und Versicherung — lies diese sorgfältig:

A) Absender ist eine Versicherung (Gothaer, Barmenia, HUK, HUK-COBURG):
   → Enthält das Dokument eine ERSTATTUNGSÜBERSICHT (Liste eingereichter Fremdrechnungen mit Erstattungsbeträgen)?
     → JA: category_id="krankenversicherung", type_id="leistungsabrechnung_reinhard" (Gothaer/Barmenia) oder "leistungsabrechnung_marion" (HUK/HUK-COBURG)
     → NEIN: Es ist ein Versicherungsverwaltungsdokument → category_id="versicherung":
       - Versicherungsschein, Nachtrag, Tarifwechsel → type_id="versicherungsschein"
       - Beitragsanpassung, Beitragsrechnung → type_id="beitragsanpassung"
       - Beitragsbescheinigung, Arbeitgeberbescheinigung → type_id="beitragsbescheinigung"
       - Kostenübernahme, Kostenzusage → type_id="kostenuebernahme"
       - AVB, AGB, Versicherungsbedingungen → type_id="versicherungsbedingungen"
       - Angebot, Antrag, Widerspruch, Schreiben → type_id="versicherungskorrespondenz"

B) Absender ist ein Arzt, Krankenhaus, Labor, MVZ, Abrechnungsdienstleister (z.B. unimed, PVS):
   → category_id="krankenversicherung", type_id="arztrechnung"
   AUSNAHME: Tierarzt / Tierklinik / Veterinaria / Clinica Veterinaria / Tierheilpraktiker
     → NICHT krankenversicherung. Verwende: category_id="familie", type_id="tierarztrechnung".

C) Sanitätshaus, Optiker, Apotheke, Physiotherapie:
   → category_id="krankenversicherung", type_id="sonstige_medizinische_leistung"

D) Arzt mit Medikamentenliste:
   → category_id="krankenversicherung", type_id="rezept"

WICHTIG: Entscheidend ist NICHT die bloße Erwähnung von "Versicherung" im Text, sondern Absender + Dokumenttyp.

ABSENDER → ADRESSAT-MAPPING bei Krankenversicherung (überschreibt jeden Default!):
- HUK / HUK-COBURG (jegliche Schreibweise: HUK, HUK COBURG, HUK-COBURG, HUK-Coburg-Krankenversicherung):
   → adressat="Marion" — IMMER. Auch wenn im Dokument kein Name lesbar ist.
- Gothaer / Barmenia:
   → adressat="Reinhard" — IMMER, außer ein anderer Name (Marion, Linoa, ...) ist explizit als Patient ausgewiesen.
- Arztrechnung / Rezept ohne klaren Patientennamen:
   → adressat=null (NICHT raten, NICHT auf Reinhard defaulten)

NEGATIVE BEISPIELE — diese Fehler hat das System in der Vergangenheit gemacht, NICHT wiederholen:
- ❌ "HUK-COBURG Leistungsabrechnung" mit adressat="Reinhard" → richtig wäre "Marion"
- ❌ Arztrechnung von "Dr. Schneider" ohne Patientenname → adressat="Reinhard" → richtig ist null
- ❌ Versicherungs-Anschreiben mit Erstattungsbetrag, aber OHNE Erstattungsübersicht-Tabelle als Leistungsabrechnung klassifiziert → richtig ist versicherungskorrespondenz
- ❌ Dokument das "Versicherung" im Fließtext erwähnt, aber von einer Bank/Steuerberater/Vermieter stammt, als Krankenversicherung klassifiziert → Absender entscheidet, nicht der Text

Für Krankenversicherung/Versicherung zusätzlich ausfüllen:
- "rechnungsbetrag": Gesamtbetrag als String (z.B. "33,06 EUR") — bei Leistungsabrechnung: Gesamtrechnungsbetrag, bei Arztrechnung: Endbetrag; sonst null
- "erstattungsbetrag": Erstatteter Betrag als String — NUR bei Leistungsabrechnung, sonst null
- "faelligkeitsdatum": Fälligkeitsdatum als String — NUR bei Arztrechnung/Rezept/sonstige, sonst null
- "positionen": Liste der Erstattungspositionen — NUR bei leistungsabrechnung-Typen, sonst []. Jede Position: {{"leistungserbringer": "Name", "zeitraum": "02.02-19.04.2023", "rechnungsbetrag": 33.06, "erstattungsbetrag": 10.72}}
"""

    header_block = ""
    if header and (header.get("absender", {}).get("plz") or header.get("empfaenger", {}).get("plz")
                   or header.get("absender", {}).get("firma") or header.get("empfaenger", {}).get("name")):
        header_block = (
            "\nERKANNTER DOKUMENTEN-KOPF (regex-extrahiert, deterministisch — verwende diese Felder "
            "bevorzugt statt im Fließtext zu raten):\n"
            f"{_format_header_for_prompt(header)}\n"
        )

    ident_block = ""
    if identifiers and (adressat_match or absender_match
                        or identifiers.get("cod_fiscale_person")
                        or identifiers.get("part_iva_firma")
                        or identifiers.get("ust_id_de")):
        rendered = _format_identifiers_for_prompt(identifiers, adressat_match, absender_match)
        if rendered:
            ident_block = (
                "\nSTRUKTURIERTE MERKMALE (deterministisch bestätigt — diese gelten, "
                "NICHT überschreiben):\n" + rendered + "\n"
            )

    # Branchen-Regeln aus YAML dynamisch in Prompt bauen
    if BRANCHEN_REGELN:
        regeln_lines = ["BRANCHEN-REGELN für Rechnungen (sprachunabhängig, gilt für DE/IT/EN):"]
        for regel in BRANCHEN_REGELN:
            kws = " / ".join(regel.get("absender_keywords", []))
            cat = regel.get("category_id", "")
            typ = regel.get("type_id", "")
            desc = (regel.get("beschreibung") or "").strip().replace("\n", " ")
            hinweis = (regel.get("hinweis") or "").strip().replace("\n", " ")
            regeln_lines.append(
                f"- Absender-Branche: {kws}\n"
                f"  Beschreibung: {desc}\n"
                f"  → category_id=\"{cat}\", type_id=\"{typ}\"\n"
                + (f"  Hinweis: {hinweis}\n" if hinweis else "")
            )
        branchen_block = "\n".join(regeln_lines)
    else:
        branchen_block = ""

    doc_type_block = ""
    if doc_type_info and doc_type_info.get("erkannter_typ"):
        rendered_dt = _format_doc_type_for_prompt(doc_type_info, header)
        if rendered_dt:
            doc_type_block = (
                "\nERKANNTER DOKUMENTTYP (regex, deterministisch — bei Konflikt mit Fließtext: "
                "dieser Block gewinnt):\n" + rendered_dt + "\n"
            )

    negativ_regeln = """
NEGATIV-REGELN (häufige Fehlerquellen — strikt beachten):
- IBAN, BIC, Kontonummer oder SEPA-Mandat im Text → das macht das Dokument NICHT zu einem Kontoauszug.
  `finanzen/kontoauszug` gilt NUR wenn: (a) Absender ist eine Bank UND (b) das Dokument trägt
  das Keyword "Kontoauszug" / "Estratto conto" / "Kontoabschluss" im Kopf.
- Das Wort "Versicherung" im Fließtext → macht das Dokument NICHT zur Krankenversicherung.
  Entscheidend sind Absender-Typ + Dokumenttyp, nicht einzelne Wörter im Text.
- Tierarzt / Tierklinik / Veterinaria → NIEMALS krankenversicherung. Immer familie/tierarztrechnung.
"""

    prompt = f"""Analysiere das folgende Dokument und klassifiziere es anhand der vorgegebenen Kategorien.

Verfügbare Kategorien und Typen:
{cat_desc}
{kv_rules}
TAXONOMIE-ZWANG: "category_id" und "type_id" MÜSSEN exakt aus der obigen Liste stammen.
NIEMALS neue Kategorie-IDs erfinden. Wenn keine passt: category_id=null → landet in Inbox.
{header_block}{ident_block}{doc_type_block}{negativ_regeln}
{branchen_block}
Für ALLE Kategorien:
- Adressat: "Reinhard" wenn Reinhard Janning/R. Janning der Empfänger ist, "Marion" wenn Marion Janning/M. Janning, "Reinhard & Marion" wenn beide adressiert sind.
  - Bei Krankenversicherung gilt IMMER das ABSENDER → ADRESSAT-MAPPING oben (HUK → Marion, Gothaer/Barmenia → Reinhard, Arztrechnung ohne Patient → null).
  - Wenn kein Name eindeutig erkennbar ist: adressat=null. NIEMALS "Reinhard" als Default setzen — lieber null als falsch.

Antworte NUR mit einem JSON-Objekt mit diesen Feldern:
- "category_id": ID der erkannten Kategorie (z.B. "krankenversicherung", "finanzen", "fahrzeuge"), oder null
- "category_label": Bezeichnung der Kategorie, oder null
- "type_id": ID des erkannten Typs (nur bei Kategorien mit definierten Typen), oder null
- "type_label": Bezeichnung des Typs, oder null
- "absender": Name des Absenders/Ausstellers (Firma oder Person), oder null
- "adressat": "Reinhard" | "Marion" | "Reinhard & Marion" | null
- "rechnungsdatum": Datum des Dokuments als String "DD.MM.YYYY", oder null
- "rechnungsbetrag": Gesamtbetrag als String (z.B. "33,06 EUR"), oder null
- "erstattungsbetrag": Erstatteter Betrag — NUR bei Leistungsabrechnung, sonst null
- "faelligkeitsdatum": Fälligkeitsdatum — NUR bei Arztrechnung/Rezept, sonst null
- "positionen": Erstattungspositionen — NUR bei Leistungsabrechnung, sonst []
- "konfidenz_category": "hoch" | "mittel" | "niedrig" — wie sicher bist du bei der Kategorie?
- "konfidenz_type":     "hoch" | "mittel" | "niedrig" — wie sicher bist du beim Typ?
- "konfidenz_absender": "hoch" | "mittel" | "niedrig" — wie sicher bist du beim Absender?
- "konfidenz_adressat": "hoch" | "mittel" | "niedrig" — wie sicher bist du beim Adressat?
- "konfidenz_datum":    "hoch" | "mittel" | "niedrig" — wie sicher bist du beim Datum?

Regeln für Per-Feld-Konfidenz:
  - "hoch": Feld eindeutig und direkt aus dem Dokument ablesbar (explizite Nennung, kein Raten).
  - "mittel": Feld ableitbar, aber nicht explizit (z.B. Absender per Logo, Adressat per Kontext).
  - "niedrig": Feld geraten, mehrere Möglichkeiten, oder Feld fehlt komplett.
  Das Gesamtkonfidenz-Feld wird vom Code als Minimum der Einzelwerte berechnet — du musst es NICHT mehr setzen.

Antworte AUSSCHLIESSLICH mit validem JSON, kein Text davor oder danach.

Dokument:
{md_content[:6000]}"""

    try:
        t0 = time.time()
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        duration_ms = int((time.time() - t0) * 1000)
        if not r.ok:
            log.warning(f"Ollama Fehler {r.status_code}: {r.text[:300]}")
            return None
        raw = r.json().get("response", "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            log.warning(f"Kein JSON in Ollama-Antwort: {raw[:200]}")
            return None
        json_str = match.group()
        json_str = _fix_llm_json(json_str)
        parsed = None
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            # Fallback: json-repair für strukturelle LLM-Fehler (fehlende Kommas etc.)
            try:
                repaired = repair_json(json_str, return_objects=True)
                if isinstance(repaired, dict):
                    parsed = repaired
            except Exception:
                pass
        if parsed is None:
            log.warning(f"JSON-Parse fehlgeschlagen (auch nach Reparatur): {repr(json_str[:200])}")
            return None
        parsed["_raw_response"] = raw[:4000]  # auf 4000 Zeichen begrenzen
        parsed["_duration_ms"] = duration_ms
        return parsed
    except Exception as e:
        log.warning(f"Ollama Klassifizierung fehlgeschlagen: {e}")
        return None

_KONFIDENZ_RANK = {"hoch": 2, "mittel": 1, "niedrig": 0}
_KONFIDENZ_FROM_RANK = {2: "hoch", 1: "mittel", 0: "niedrig"}


def aggregate_konfidenz(result: dict) -> str:
    """Berechnet Gesamtkonfidenz als Minimum der Per-Feld-Konfidenz-Werte.

    Rückfall auf Legacy-Feld 'konfidenz' wenn keine Per-Feld-Werte vorhanden.
    """
    per_feld = [
        result.get("konfidenz_category"),
        result.get("konfidenz_type") if result.get("type_id") else None,
        result.get("konfidenz_absender"),
        result.get("konfidenz_adressat") if result.get("adressat") else None,
        result.get("konfidenz_datum") if result.get("rechnungsdatum") else None,
    ]
    werte = [_KONFIDENZ_RANK[v] for v in per_feld if v in _KONFIDENZ_RANK]
    if werte:
        return _KONFIDENZ_FROM_RANK[min(werte)]
    # Fallback: altes Einzel-Konfidenz-Feld
    return result.get("konfidenz") or "mittel"


# ── Verarbeitung ───────────────────────────────────────────────────────────────

def _sanitize_name_part(s: str) -> str:
    """Sanitize a string for use in filenames: collapse whitespace, keep alphanumeric + umlauts."""
    s = re.sub(r"[^\w\s\-äöüÄÖÜß]", "", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s


def build_clean_filename(result: dict, original_stem: str) -> str:
    """Build clean filename: YYYYMMDD_Absender_Dokumenttyp.

    Falls Datum oder Absender fehlt, wird der Original-Dateiname als Fallback verwendet.
    """
    datum = result.get("rechnungsdatum")  # "DD.MM.YYYY"
    absender = result.get("absender")
    type_label = result.get("type_label") or result.get("type_id") or ""

    # Datum → YYYYMMDD
    # Primärquelle: Datum aus dem Dokument (vom LLM extrahiertes rechnungsdatum, Format DD.MM.YYYY).
    # Fallback: heutiges Datum. Scanner-Prefix auf dem Original-Dateinamen wird bewusst NICHT
    # als Quelle verwendet (Scanner schreibt DDMMYYYY, was zu Fehl-Interpretationen führt).
    date_str = None
    if datum and re.match(r"\d{2}\.\d{2}\.\d{4}", datum):
        d, m, y = datum.split(".")
        if 1990 <= int(y) <= 2029 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            date_str = f"{y}{m}{d}"
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    # Absender kürzen
    if absender:
        absender_clean = _sanitize_name_part(absender)
        # Auf max 30 Zeichen kürzen, am Wortende abschneiden
        if len(absender_clean) > 30:
            absender_clean = absender_clean[:30].rsplit("_", 1)[0]
    else:
        absender_clean = ""

    # Typ
    type_clean = _sanitize_name_part(type_label) if type_label else ""

    # Tier (optional, nur bei familie/tierarztrechnung gesetzt)
    tier = result.get("tier")
    tier_clean = _sanitize_name_part(tier) if tier else ""

    # Zusammenbauen
    parts = [date_str]
    if absender_clean:
        parts.append(absender_clean)
    if tier_clean:
        parts.append(tier_clean)
    if type_clean:
        parts.append(type_clean)

    if len(parts) == 1:
        # Kein Absender, kein Typ → Original-Stem verwenden
        return _sanitize_name_part(original_stem)

    return "_".join(parts)


def _build_frontmatter(result: dict, pdf_filename: str, category_id: str, type_id: str) -> str:
    """Baut den YAML-Frontmatter-Block für eine Vault-MD auf."""
    r = result or {}

    # Pflichtfelder
    datum       = r.get("rechnungsdatum") or ""
    absender    = r.get("absender") or ""
    adressat    = r.get("adressat") or ""
    kategorie   = r.get("category_label") or category_id or ""
    typ_label   = r.get("type_label") or type_id or ""
    thema       = f"{absender} {typ_label}".strip() if absender or typ_label else ""
    betrag      = r.get("rechnungsbetrag") or ""
    faellig     = r.get("faelligkeitsdatum") or ""
    zusammen    = r.get("zusammenfassung") or ""
    lang        = r.get("_lang") or "de"
    erstellt    = datetime.now().strftime("%Y-%m-%d")

    # Tags ableiten
    tags: list[str] = []
    if category_id:
        tags.append(category_id.replace("_", "-"))
    if type_id:
        tags.append(type_id.replace("_", "-"))

    def _q(val: str) -> str:
        """YAML-String mit doppelten Anführungszeichen, intern escapt."""
        return '"' + val.replace('"', '\\"') + '"'

    lines = ["---"]
    if datum:
        lines.append(f"datum: {_q(datum)}")
    if absender:
        lines.append(f"absender: {_q(absender)}")
    if adressat:
        lines.append(f"adressat: {_q(adressat)}")
    if thema:
        lines.append(f"thema: {_q(thema)}")
    if kategorie:
        lines.append(f"kategorie: {_q(kategorie)}")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {t}")
    if zusammen:
        lines.append(f"zusammenfassung: {_q(zusammen)}")
    if betrag:
        lines.append(f"betrag: {_q(betrag)}")
    if faellig:
        lines.append(f"faellig: {_q(faellig)}")
    if lang and lang != "de":
        lines.append(f"sprache: {lang}")
    # original: Wikilink auf das PDF in Anlagen/
    lines.append(f'original: "[[Anlagen/{pdf_filename}]]"')
    lines.append(f"erstellt: {erstellt}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def move_to_vault(file_path: Path, temp_md: Path, category_id: str, type_id: str, result: dict):
    """Verschiebt PDF nach Anlagen/ und MD in den korrekten Typ-Unterordner im Vault.

    Pfadlogik: {vault_folder}/{type_subfolder[ person]}/[{year}/]{clean_name}.md
    Das MD erhält einen YAML-Frontmatter-Block (inkl. original: [[Anlagen/…]]).
    """
    if not VAULT_PDF_ARCHIV or not VAULT_ROOT:
        log.warning("VAULT_PDF_ARCHIV/VAULT_ROOT nicht konfiguriert — Dateien bleiben in WATCH_DIR")
        return

    rechnungsdatum = result.get("rechnungsdatum") if result else None
    year = rechnungsdatum[-4:] if rechnungsdatum and len(rechnungsdatum) >= 4 else datetime.now().strftime("%Y")
    adressat = (result.get("adressat") or "") if result else ""

    # Sauberen Dateinamen generieren
    if result:
        clean_name = build_clean_filename(result, file_path.stem)
    else:
        clean_name = _sanitize_name_part(file_path.stem)

    vault_pfad = build_vault_path(category_id, type_id, adressat, year, f"{clean_name}.md")
    dest_md = VAULT_ROOT / vault_pfad
    dest_md_dir = dest_md.parent
    dest_pdf = VAULT_PDF_ARCHIV / f"{clean_name}.pdf"

    # Kollisionsvermeidung
    counter = 2
    while dest_pdf.exists() or dest_md.exists():
        vault_pfad = _build_vault_md_relpath(vault_folder, year, f"{clean_name}_{counter}.md")
        dest_md = VAULT_ROOT / vault_pfad
        dest_pdf = VAULT_PDF_ARCHIV / f"{clean_name}_{counter}.pdf"
        counter += 1

    pdf_filename = dest_pdf.name

    # PDF verschieben
    VAULT_PDF_ARCHIV.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file_path), str(dest_pdf))
    log.info(f"PDF → Anlagen: {pdf_filename}")

    # Frontmatter vor MD-Inhalt prependen + PDF-Link als erste Body-Zeile
    try:
        ocr_content = temp_md.read_text(encoding="utf-8")
        frontmatter = _build_frontmatter(result or {}, pdf_filename, category_id, type_id)
        pdf_link_line = f"📎 [[Anlagen/{pdf_filename}]]\n\n"
        temp_md.write_text(frontmatter + pdf_link_line + ocr_content, encoding="utf-8")
    except Exception as e:
        log.warning(f"Frontmatter konnte nicht geschrieben werden: {e}")

    # MD verschieben
    dest_md_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp_md), str(dest_md))
    log.info(f"MD → Vault: {vault_pfad}")

    # vault_pfad in DB speichern
    try:
        with get_db() as con:
            con.execute(
                "UPDATE dokumente SET vault_kategorie=?, vault_typ=?, vault_pfad=? WHERE dateiname=?",
                (category_id, type_id, vault_pfad, file_path.name)
            )
    except Exception as e:
        log.warning(f"vault_pfad DB-Update fehlgeschlagen: {e}")



def process_file(file_path: Path):
    if file_path.suffix.lower() != ".pdf":
        return

    log.info(f"Neue Datei: {file_path.name}")

    # Duplikat-Check gegen pdf-archiv
    if VAULT_PDF_ARCHIV and (VAULT_PDF_ARCHIV / file_path.name).exists():
        log.info(f"Bereits in pdf-archiv: {file_path.name} — überspringe")
        tg_send(f"ℹ️ Bereits in pdf-archiv vorhanden — übersprungen\n<code>{file_path.name}</code>")
        file_path.unlink()

        return

    if not wait_for_file_stable(file_path):
        log.warning(f"Datei nicht stabil: {file_path.name}")
        tg_send(f"⚠️ Datei nicht stabil (Transfer abgebrochen?)\n<code>{file_path.name}</code>")
        return

    # 1. PDF → Markdown via Docling
    md_content = convert_to_markdown(file_path)
    if not md_content:
        tg_send(f"❌ Docling-Konvertierung fehlgeschlagen\n<code>{file_path.name}</code>")
        return

    # OCR-Qualitäts-Gate: zu wenig Text → Inbox, Telegram-Warnung, kein LLM-Aufwand
    ocr_chars = len(md_content.strip())
    if ocr_chars < OCR_MIN_CHARS:
        log.warning(f"OCR-Qualität unzureichend ({ocr_chars} Zeichen): {file_path.name}")
        tg_send(
            f"⚠️ <b>OCR-Qualität unzureichend</b> — Datei in Inbox\n"
            f"<code>{file_path.name}</code>\n"
            f"Nur {ocr_chars} Zeichen erkannt (Minimum: {OCR_MIN_CHARS})"
        )
        timestamp_ocr = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem_ocr = re.sub(r"[^\w\-]", "_", file_path.stem)
        temp_md_ocr = TEMP_DIR / f"{timestamp_ocr}_{stem_ocr}.md"
        temp_md_ocr.write_text(md_content, encoding="utf-8")
        move_to_vault(file_path, temp_md_ocr, "", "", {})
        return

    # 2. Markdown in TEMP speichern
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r"[^\w\-]", "_", file_path.stem)
    temp_md = TEMP_DIR / f"{timestamp}_{stem}.md"
    temp_md.write_text(md_content, encoding="utf-8")
    log.info(f"Markdown gespeichert: {temp_md.name}")

    # 2b. Header-Extraktion (regex, deterministisch — vor Übersetzung, damit Originalnamen erhalten bleiben)
    header_info = extract_document_header(md_content)
    try:
        header_path = temp_md.with_suffix(".header.json")
        header_path.write_text(json.dumps(header_info, ensure_ascii=False, indent=2), encoding="utf-8")
        abs_plz = header_info.get("absender", {}).get("plz")
        emp_name = header_info.get("empfaenger", {}).get("name")
        log.info(f"Header extrahiert (absender.plz={abs_plz!r}, empfaenger.name={emp_name!r}) → {header_path.name}")
    except Exception as e:
        log.warning(f"Header-Artefakt konnte nicht geschrieben werden: {e}")

    # 2c. Identifier-Extraktion (Cod. Fiscale / Part. Iva / USt-IdNr / IBAN)
    # → deterministische Adressat-/Absender-Auflösung via personen.yaml + absender.yaml
    identifiers = extract_identifiers(md_content)
    adressat_match = resolve_adressat(identifiers, md_content)
    absender_match = resolve_absender(identifiers, header_info)
    try:
        ident_path = temp_md.with_suffix(".identifiers.json")
        ident_path.write_text(
            json.dumps(
                {
                    "identifiers": identifiers,
                    "adressat_match": adressat_match,
                    "absender_match": absender_match,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info(
            f"Identifiers extrahiert (cf_person={len(identifiers.get('cod_fiscale_person', []))}, "
            f"p_iva={len(identifiers.get('part_iva_firma', []))}, "
            f"adressat={adressat_match['person_key'] if adressat_match else None}, "
            f"absender={absender_match['id'] if absender_match else None}) → {ident_path.name}"
        )
    except Exception as e:
        log.warning(f"Identifiers-Artefakt konnte nicht geschrieben werden: {e}")

    # 2d. Dokumenttyp-Extraktion (keyword-basiert auf Original-MD, vor Übersetzung)
    doc_type_info = extract_document_type(md_content)
    try:
        dt_path = temp_md.with_suffix(".doc_type.json")
        dt_path.write_text(json.dumps(doc_type_info, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(
            f"Dokumenttyp extrahiert (typ={doc_type_info.get('erkannter_typ')!r}, "
            f"keyword={doc_type_info.get('quell_keyword')!r}) → {dt_path.name}"
        )
    except Exception as e:
        log.warning(f"Dokumenttyp-Artefakt konnte nicht geschrieben werden: {e}")

    # 3. Sprach-Erkennung + ggf. Übersetzungs-Pass (Klassifikation arbeitet auf Deutsch)
    classify_input = md_content
    lang, lang_prob = detect_document_language(md_content)
    if lang != "de" and lang_prob >= 0.85:
        log.info(f"Nicht-deutsches Dokument erkannt: {lang} (p={lang_prob:.2f}) — übersetze nach DE")
        translated = translate_to_german(md_content, lang)
        if translated:
            classify_input = translated
            trans_path = temp_md.with_suffix(f".translation.{OLLAMA_TRANSLATE_MODEL.replace(':', '_').replace('/', '_')}.md")
            trans_path.write_text(
                f"<!-- Übersetzung {lang}→de via {OLLAMA_TRANSLATE_MODEL} -->\n\n{translated}",
                encoding="utf-8",
            )
            log.info(f"Übersetzung ok ({len(translated)} chars) → {trans_path.name}")
        else:
            log.warning("Übersetzung fehlgeschlagen — klassifiziere auf Originaltext")

    # 4. Klassifizierung via Ollama
    categories = load_categories()
    if not categories:
        tg_send(f"❌ Keine Kategorien konfiguriert\n<code>{file_path.name}</code>")
        return

    result = classify_with_ollama(
        classify_input,
        categories,
        header=header_info,
        identifiers=identifiers,
        adressat_match=adressat_match,
        absender_match=absender_match,
        doc_type_info=doc_type_info,
    )

    # Sprache im Result speichern (für Telegram-Ausgabe)
    if result:
        result["_lang"] = lang
        result["_lang_prob"] = lang_prob

    # Deterministisches Override: Cod.Fiscale-Match schlägt LLM-Adressat
    if result and adressat_match:
        person_key = adressat_match.get("person_key", "")
        forced = person_key.capitalize() if person_key else None
        if forced and result.get("adressat") != forced:
            log.info(
                f"Adressat deterministisch überschrieben: "
                f"'{result.get('adressat')}' → '{forced}' (via {adressat_match.get('via')})"
            )
            result["adressat"] = forced
    elif result and absender_match and absender_match.get("adressat_default"):
        # adressat_default ist ein harter Fakt (z. B. Gothaer → Reinhard, HUK → Marion).
        # Überschreibt auch LLM-Werte wie "Reinhard & Marion", da der Absender eindeutig
        # einem Adressaten zugeordnet ist.
        old = result.get("adressat")
        result["adressat"] = absender_match["adressat_default"]
        if old != result["adressat"]:
            log.info(
                f"Adressat durch Absender-Default überschrieben: '{old}' → '{result['adressat']}' "
                f"(absender={absender_match['id']})"
            )
        else:
            log.info(
                f"Adressat aus Absender-Default gesetzt: {result['adressat']} "
                f"(absender={absender_match['id']})"
            )

    # Taxonomie-Validierung: halluzinierte category_id auf null setzen → Inbox
    if result and result.get("category_id") and result["category_id"] not in categories:
        log.warning(f"LLM halluzinierte Kategorie '{result['category_id']}' — auf null zurückgesetzt (Inbox)")
        result["category_id"] = None
        result["type_id"] = None
        result["konfidenz"] = "niedrig"

    # Taxonomie-Validierung: halluzinierter type_id bei gültiger Kategorie → type auf None,
    # Kategorie bleibt erhalten (Datei landet in Kategorie-Wurzel statt Typ-Unterordner).
    if result and result.get("category_id") and result.get("type_id"):
        valid_type_ids = {t["id"] for t in categories[result["category_id"]].get("types", [])}
        if result["type_id"] not in valid_type_ids:
            log.warning(
                f"LLM halluzinierte Typ '{result['type_id']}' in Kategorie '{result['category_id']}' "
                f"— Typ auf null zurückgesetzt (bleibt in Kategorie-Wurzel)"
            )
            result["type_id"] = None
            if result.get("konfidenz") == "hoch":
                result["konfidenz"] = "mittel"

    # Deterministisches Override: absender_match.kategorie_hint/typ_hint schlagen LLM-Kategorisierung
    # (der User hat diese Zuordnung explizit in absender.yaml hinterlegt — stärker als semantisches Raten).
    if result and absender_match and absender_match.get("kategorie_hint"):
        hint_cat = absender_match["kategorie_hint"]
        hint_typ = absender_match.get("typ_hint")
        if hint_cat in categories and result.get("category_id") != hint_cat:
            log.info(
                f"Kategorie deterministisch überschrieben: '{result.get('category_id')}/{result.get('type_id')}' "
                f"→ '{hint_cat}/{hint_typ}' (via absender={absender_match['id']})"
            )
            result["category_id"] = hint_cat
            result["category_label"] = categories[hint_cat].get("label")
            valid_typs = {t["id"]: t.get("label") for t in categories[hint_cat].get("types", [])}
            if hint_typ and hint_typ in valid_typs:
                result["type_id"] = hint_typ
                result["type_label"] = valid_typs[hint_typ]
            else:
                result["type_id"] = None
                result["type_label"] = None
            if result.get("konfidenz") == "hoch":
                result["konfidenz"] = "mittel"

    # Dokumenttyp-kategorie_hint als schwächster Override: nur wenn weder Absender-Match
    # noch LLM eine Kategorie gesetzt haben — dann ist der Typ ein letzter Anker.
    if result and not result.get("category_id") and doc_type_info and doc_type_info.get("kategorie_hint"):
        hint_cat = doc_type_info["kategorie_hint"]
        if hint_cat in categories:
            log.info(
                f"Kategorie aus Dokumenttyp-Hint gesetzt: '{hint_cat}' "
                f"(keyword={doc_type_info.get('quell_keyword')!r})"
            )
            result["category_id"] = hint_cat
            result["category_label"] = categories[hint_cat].get("label")

    if not result or not result.get("category_id"):
        tg_send(
            f"⚠️ <b>Klassifizierung nicht möglich — Datei in Inbox</b>\n"
            f"Datei: <code>{file_path.name}</code>"
        )
        log.info(f"Klassifizierung fehlgeschlagen für: {file_path.name} — verschiebe in Inbox")
        move_to_vault(file_path, temp_md, "", "", {})

        return

    # Tier-Ableitung für Haustier-Dokumente (bidirektional):
    # 1. Tiername im Text bereits über resolve_adressat → tier steht in adressat_match
    # 2. Nur Adressat bekannt, kategorie=familie/tierarztrechnung → Tier aus Besitzer ableiten
    tier = None
    if adressat_match and adressat_match.get("tier"):
        tier = adressat_match["tier"]
    elif result.get("adressat"):
        person_key = result["adressat"].lower()
        tier = derive_tier(person_key, result.get("category_id"), result.get("type_id"))
    if tier:
        result["tier"] = tier
        log.info(f"Tier zugeordnet: {tier} (Adressat={result.get('adressat')})")

    # 5. Datenbank
    match_infos = save_to_db(file_path, result)
    save_klassifikation_historie(result.get("_dok_id"), result)

    # Hash-Duplikat: PDF löschen, kurze Benachrichtigung, kein weiterer Vault-Move
    if result.get("_is_hash_duplicate"):
        dup_name = file_path.name
        log.info(f"Hash-Duplikat: {dup_name} — wird gelöscht")
        tg_send(
            f"♻️ <b>Duplikat erkannt</b> — übersprungen\n"
            f"<code>{dup_name}</code>\n"
            f"Identischer Inhalt bereits in Vault vorhanden."
        )
        try:
            file_path.unlink()
        except Exception as e:
            log.warning(f"Duplikat konnte nicht gelöscht werden: {e}")
        return

    # 6. Telegram-Nachricht
    type_id            = result.get("type_id", "")
    is_la              = type_id in LEISTUNGSABRECHNUNG_TYPES
    is_versicherung    = type_id in VERSICHERUNG_TYPES
    absender           = result.get("absender") or "–"
    adressat           = result.get("adressat") or "Reinhard"
    rechnungsdatum     = result.get("rechnungsdatum")
    rechnungsbetrag    = result.get("rechnungsbetrag")
    erstattungsbetrag  = result.get("erstattungsbetrag")
    faelligkeitsdatum  = result.get("faelligkeitsdatum")
    doc_lang           = result.get("_lang", "de")
    doc_lang_prob      = result.get("_lang_prob", 0.0)

    def _ki(field: str) -> str:
        """Icon für ein Per-Feld-Konfidenz-Wert."""
        return {"hoch": "🟢", "mittel": "🟡", "niedrig": "🔴"}.get(
            result.get(f"konfidenz_{field}", ""), "⚪"
        )

    # PDF im Chat senden zur Überprüfung
    tg_send_document(file_path)

    # Neuen Dateinamen für Telegram-Nachricht berechnen
    clean_name = build_clean_filename(result, file_path.stem)

    lines = [
        f"✅ <b>Dokument klassifiziert</b>",
        f"",
        f"📄 Datei:      <code>{clean_name}.pdf</code>",
        f"🏢 Absender:   {_ki('absender')} {absender}",
        f"👤 Adressat:   {_ki('adressat')} {adressat}",
    ]
    if rechnungsdatum:
        lines.append(f"📅 Datum:      {_ki('datum')} {rechnungsdatum}")
    lines += [
        f"🗂 Kategorie:  {_ki('category')} <b>{result.get('category_label', '–')}</b>",
        f"📁 Typ:        {_ki('type')} <b>{result.get('type_label', '–')}</b>",
    ]

    if is_la:
        # Gesamtbeträge + Erstattungsprozent
        if rechnungsbetrag:
            lines.append(f"🧾 Eingereicht: {rechnungsbetrag}")
        if erstattungsbetrag and rechnungsbetrag:
            rb = _parse_betrag(rechnungsbetrag)
            eb = _parse_betrag(erstattungsbetrag)
            pct = f" ({round(eb/rb*100)}%)" if rb and eb else ""
            lines.append(f"💚 Erstattet:  {erstattungsbetrag}{pct}")
        elif erstattungsbetrag:
            lines.append(f"💚 Erstattet:  {erstattungsbetrag}")

        # Match-Status
        if match_infos:
            n_matched = sum(1 for m in match_infos if m["matched"])
            n_total   = len(match_infos)
            lines.append(f"🔗 Zugeordnet: {n_matched}/{n_total} Rechnung{'en' if n_total != 1 else ''} gefunden")
            for m in match_infos:
                icon = "✅" if m["matched"] else "❌"
                betrag_str = f"{m['rechnungsbetrag']:.2f} EUR".replace(".", ",") if m["rechnungsbetrag"] else "–"
                pct_str    = f" ({m['prozent']}%)" if m["prozent"] else ""
                suffix     = "" if m["matched"] else " (nicht in DB)"
                lines.append(f"   {icon} {m['leistungserbringer']} → {betrag_str}{pct_str}{suffix}")
    elif is_versicherung:
        # Versicherungsdokument — keine Rechnungsinfos
        pass
    else:
        if rechnungsbetrag:
            lines.append(f"💰 Betrag:     {rechnungsbetrag}")
        if faelligkeitsdatum:
            lines.append(f"📅 Fällig:     {faelligkeitsdatum}")
        if not rechnungsbetrag:
            lines.append(f"💰 Betrag:     –")

    lang_label = {"de": "Deutsch", "it": "Italiano", "en": "English", "fr": "Français"}.get(doc_lang, doc_lang.upper())
    lang_pct   = f" ({round(doc_lang_prob * 100)}%)" if doc_lang_prob > 0 else ""
    lines.append(f"🌐 Sprache:    {lang_label}{lang_pct}")

    category_id = result.get("category_id", "")
    tg_send("\n".join(lines))
    log.info(f"Klassifiziert: {file_path.name} → {category_id}/{type_id}")

    # 7. Dateien in Vault verschieben
    move_to_vault(file_path, temp_md, category_id, type_id, result)


# ── Queue-Worker ───────────────────────────────────────────────────────────────

def queue_worker():
    while True:
        file_path = file_queue.get()
        try:
            process_file(file_path)
        except Exception as e:
            log.error(f"Unerwarteter Fehler bei {file_path}: {e}")
        finally:
            file_queue.task_done()


# ── Watchdog ───────────────────────────────────────────────────────────────────

class PdfHandler(FileSystemEventHandler):
    def _enqueue(self, path: Path):
        if path.suffix.lower() == ".pdf":
            log.info(f"In Queue: {path.name}")
            file_queue.put(path)

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._enqueue(Path(event.dest_path))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Document Dispatcher startet.")
    log.info(f"Watch-Dir:  {WATCH_DIR}")
    log.info(f"Temp-Dir:   {TEMP_DIR}")
    log.info(f"Config:     {CONFIG_FILE}")
    log.info(f"DB:         {DB_FILE}")
    log.info(f"Telegram:   {'aktiv' if TELEGRAM_TOKEN else 'nicht konfiguriert'}")

    init_db()

    if not wait_for_docling():
        log.error("Docling Serve nicht erreichbar. Beende.")
        raise SystemExit(1)

    categories = load_categories()
    log.info(f"Kategorien geladen: {list(categories.keys())}")

    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    threading.Thread(target=start_api_server, daemon=True).start()
    # Telegram-Polling deaktiviert — Wilson/OpenClaw pollt (selber Bot-Token!).
    # Dispatcher ist send-only. Korrekturen laufen über REST-API (/api/correct).
    # threading.Thread(target=tg_poll, daemon=True).start()

    for f in WATCH_DIR.glob("*.pdf"):
        file_queue.put(f)

    observer = Observer()
    observer.schedule(PdfHandler(), str(WATCH_DIR), recursive=False)
    observer.start()
    log.info("Dispatcher aktiv — warte auf Dokumente.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
