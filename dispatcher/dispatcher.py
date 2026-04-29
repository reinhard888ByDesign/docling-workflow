import argparse
import hashlib
import os
import re
import json
import sys
import time
import queue
import shutil
import socket
import sqlite3
import logging
import requests
import threading
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
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

# Log-Ringbuffer für /api/logs (liefert Pipeline-Dashboard die Log-Historie pro Dokument).
from collections import deque as _deque
LOG_BUFFER: _deque = _deque(maxlen=5000)

class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append({
                "t": record.created,
                "level": record.levelname,
                "msg": record.getMessage(),
            })
        except Exception:
            pass

logging.getLogger().addHandler(_RingBufferHandler())

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
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX",   "8192"))
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT",   "300"))
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
                erstellt_am    TEXT DEFAULT (datetime('now','localtime'))
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
                timestamp           TEXT DEFAULT (datetime('now','localtime')),
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

            CREATE TABLE IF NOT EXISTS pipeline_steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dateiname   TEXT    NOT NULL,
                step_id     TEXT    NOT NULL,
                label       TEXT,
                status      TEXT,
                duration_ms INTEGER,
                ts          TEXT,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_pipeline_steps_dateiname ON pipeline_steps(dateiname);
            CREATE INDEX IF NOT EXISTS idx_pipeline_steps_step ON pipeline_steps(step_id);
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
        if "anlagen_dateiname" not in cols_dok:
            con.execute("ALTER TABLE dokumente ADD COLUMN anlagen_dateiname TEXT")
        # lernregeln-Tabelle
        con.execute("""
            CREATE TABLE IF NOT EXISTS lernregeln (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                typ          TEXT NOT NULL,
                muster       TEXT NOT NULL,
                alle_keywords INTEGER DEFAULT 0,
                category_id  TEXT NOT NULL,
                type_id      TEXT,
                beschreibung TEXT,
                erstellt_am  TEXT DEFAULT (datetime('now','localtime')),
                anwendungen  INTEGER DEFAULT 0
            )
        """)
        # Batch-Läufe: Lauf-Metadaten + pro-Dokument-Details
        con.execute("""
            CREATE TABLE IF NOT EXISTS batch_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                input_source  TEXT,
                ocr_mode      TEXT,
                output_mode   TEXT,
                output_dir    TEXT,
                status        TEXT DEFAULT 'running',
                total         INTEGER DEFAULT 0,
                processed     INTEGER DEFAULT 0,
                errors        INTEGER DEFAULT 0,
                started_at    TEXT,
                finished_at   TEXT,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS batch_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL REFERENCES batch_runs(id),
                doc_path        TEXT,
                status          TEXT,
                ocr_source      TEXT,
                ocr_chars       INTEGER,
                lang            TEXT,
                kategorie       TEXT,
                typ             TEXT,
                absender        TEXT,
                adressat        TEXT,
                rechnungsdatum  TEXT,
                rechnungsbetrag TEXT,
                konfidenz       TEXT,
                result_path     TEXT,
                ocr_meta_json   TEXT,
                error           TEXT,
                processed_at    TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_batch_items_run ON batch_items(run_id)")
        # Migration: Spalten auf bestehender DB nachrüsten
        cols_items = {r[1] for r in con.execute("PRAGMA table_info(batch_items)")}
        for col, ddl in [
            ("ocr_chars",       "INTEGER"),
            ("lang",            "TEXT"),
            ("typ",             "TEXT"),
            ("absender",        "TEXT"),
            ("adressat",        "TEXT"),
            ("rechnungsdatum",  "TEXT"),
            ("rechnungsbetrag", "TEXT"),
            ("ocr_meta_json",   "TEXT"),
        ]:
            if col not in cols_items:
                con.execute(f"ALTER TABLE batch_items ADD COLUMN {col} {ddl}")
                log.info(f"Migration: batch_items.{col} ({ddl}) hinzugefügt")
        # Duplikat-Erkennung Tabellen
        con.execute("""
            CREATE TABLE IF NOT EXISTS duplikat_scans (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                status       TEXT DEFAULT 'running',
                total_pdfs   INTEGER DEFAULT 0,
                byte_gruppen INTEGER DEFAULT 0,
                sem_gruppen  INTEGER DEFAULT 0,
                started_at   TEXT DEFAULT (datetime('now','localtime')),
                finished_at  TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS duplikat_gruppen (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id    INTEGER REFERENCES duplikat_scans(id),
                typ        TEXT,
                pdf_hash   TEXT,
                datum      TEXT,
                absender   TEXT,
                status     TEXT DEFAULT 'offen',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS duplikat_eintraege (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                gruppe_id    INTEGER NOT NULL REFERENCES duplikat_gruppen(id),
                pdf_pfad     TEXT,
                md_pfad      TEXT,
                ist_original INTEGER DEFAULT 0,
                verschoben   INTEGER DEFAULT 0,
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
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

    # keyword_rules → globale Liste
    global KEYWORD_RULES
    KEYWORD_RULES = data.get("keyword_rules", []) or []

    log.info(
        f"Kategorien geladen: {list(cats.keys())} | "
        f"LA-Typen: {len(LEISTUNGSABRECHNUNG_TYPES)} | "
        f"Vers-Typen: {len(VERSICHERUNG_TYPES)} | "
        f"Type-Routing: {len(TYPE_ROUTING)} Einträge | "
        f"Branchen-Regeln: {len(BRANCHEN_REGELN)} | "
        f"Keyword-Rules: {len(KEYWORD_RULES)}"
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

# ── Rescan-Fortschritt ─────────────────────────────────────────────────────────
_rescan_state: dict = {"active": False, "total": 0, "done": 0, "errors": 0, "current": ""}
_rescan_stop_requested: bool = False
KEYWORD_RULES: list = []

# ── Batch-Modus (thread-local) ─────────────────────────────────────────────────
# Schaltet Telegram und Vault-Move aus, schleust OCR-Text von außen ein und hält
# das Klassifikationsergebnis des aktuellen Laufs fest. Wird von run_batch()
# und vom Dashboard-Worker gesetzt. Watchdog-Pfad bleibt davon unberührt.
_batch_ctx = threading.local()

# Hybrid-OCR-Gate: Schwellwert für Cache→Docling-Fallback.
# Separat von OCR_MIN_CHARS, das erst nach Docling greift (Inbox-Fallback bei zu wenig Text).
HYBRID_OCR_MIN_CHARS = int(os.environ.get("HYBRID_OCR_MIN_CHARS", "500"))
HYBRID_OCR_LANGS = {"de", "it", "en"}


def _batch_active() -> bool:
    return getattr(_batch_ctx, "active", False)


def _batch_output_mode() -> str:
    return getattr(_batch_ctx, "output_mode", "vault-move")

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str, chat_id: str | None = None, reply_markup: dict | None = None) -> int | None:
    """Sendet Telegram-Nachricht, optional mit Inline-Keyboard. Gibt message_id zurück."""
    if _batch_active():
        return None
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
    if _batch_active():
        return
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
    if os.environ.get("DISABLE_TELEGRAM_POLL", "0") == "1":
        log.info("Telegram-Polling deaktiviert (DISABLE_TELEGRAM_POLL=1)")
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

                        elif cb_data.startswith(("gkat:", "gadr:", "gabs:", "gabsneu:", "gfin:", "gedit:", "reject:", "confirm:", "correct:", "back:", "field:", "setcat:", "setadr:")):
                            # Wilson-Callbacks — an Wilson-Relay weiterleiten
                            try:
                                r = requests.post(
                                    f"http://{WILSON_PI_HOST}:8770/tg/callback",
                                    json={
                                        "callback_id": cb_id,
                                        "data": cb_data,
                                        "chat_id": cb_chat,
                                        "msg_id": cb_msg_id,
                                        "msg_text": cb_msg.get("text", ""),
                                    },
                                    timeout=10,
                                )
                                if not r.ok:
                                    tg_answer_callback(cb_id, f"❌ Wilson: {r.status_code}")
                            except Exception as we:
                                log.warning(f"Wilson-Relay Fehler: {we}")
                                tg_answer_callback(cb_id, "❌ Wilson nicht erreichbar")

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

                # ── PDF-Datei direkt an Bot geschickt ──
                doc = msg.get("document", {})
                if doc.get("mime_type") == "application/pdf":
                    file_id   = doc["file_id"]
                    file_name = doc.get("file_name") or f"telegram_{file_id[:8]}.pdf"
                    if not file_name.lower().endswith(".pdf"):
                        file_name += ".pdf"
                    log.info(f"TG-Upload PDF: {file_name}")
                    try:
                        r_file = requests.get(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                            params={"file_id": file_id}, timeout=10,
                        )
                        file_path_tg = r_file.json()["result"]["file_path"]
                        r_dl = requests.get(
                            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path_tg}",
                            timeout=60,
                        )
                        dest = WATCH_DIR / file_name
                        # Dateiname-Konflikt vermeiden
                        counter = 1
                        while dest.exists():
                            dest = WATCH_DIR / f"{dest.stem}_{counter}.pdf"
                            counter += 1
                        dest.write_bytes(r_dl.content)
                        tg_send(f"📥 <b>Dokument empfangen</b>\n<code>{file_name}</code>\nWird verarbeitet…")
                        log.info(f"TG-Upload gespeichert: {dest.name}")
                    except Exception as e:
                        log.warning(f"TG-Upload Fehler: {e}")
                        tg_send(f"❌ Fehler beim Empfangen der Datei: {e}")
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

SYNCTHING_API_KEY = os.environ.get("SYNCTHING_API_KEY", "M7iayV5FZMzefpFDwuwJ7ZWgihkqSbo3")
WILSON_PI_HOST   = os.environ.get("WILSON_PI_HOST", "192.168.3.124")

# ── SSE-Broadcaster ───────────────────────────────────────────────────────────
_sse_lock    = threading.Lock()
_sse_clients: list[queue.Queue] = []

def sse_broadcast(event_type: str, data: dict):
    """Schickt ein SSE-Event an alle verbundenen Clients."""
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False, default=str)
    msg = f"event: {event_type}\ndata: {payload}\n\n".encode()
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

def _step_emit(filename: str, step_id: str, label: str, status: str,
               extracted: dict | None = None, duration_ms: float | None = None,
               error: str | None = None):
    """SSE-Event für einen Pipeline-Schritt + persistente Speicherung in DB."""
    ts = datetime.now().isoformat(timespec="seconds")
    dur = round(duration_ms) if duration_ms is not None else None
    data: dict = {
        "filename": filename,
        "step_id":  step_id,
        "label":    label,
        "status":   status,
        "ts":       ts,
    }
    if dur is not None:
        data["duration_ms"] = dur
    if extracted:
        data["extracted"] = extracted
    if error:
        data["error"] = error
    sse_broadcast("doc_step", data)
    if status in ("done", "error", "skip"):
        try:
            with get_db() as con:
                con.execute(
                    "INSERT INTO pipeline_steps (dateiname, step_id, label, status, duration_ms, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (filename, step_id, label, status, dur, ts)
                )
        except Exception as e:
            log.debug(f"pipeline_steps INSERT fehlgeschlagen: {e}")


_WILSON_COLLECTOR = r"""
import json, subprocess, os, re, urllib.request
from pathlib import Path
from datetime import datetime

BASE = Path.home() / ".openclaw"
result = {"ts": datetime.now().isoformat(timespec="seconds")}

# --- Gateway-Prozess ---
try:
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout
    gw = {"running": False}
    for line in ps.splitlines():
        if "openclaw-gateway" in line and "grep" not in line:
            p = line.split()
            gw = {"running": True, "pid": int(p[1]), "cpu_pct": float(p[2]),
                  "mem_pct": float(p[3]), "uptime": p[9]}
            break
    result["gateway"] = gw
except Exception as e:
    result["gateway"] = {"running": False, "error": str(e)}

# --- Version ---
try:
    oc = json.loads((BASE / "openclaw.json").read_text())
    result["gateway"]["version_installed"] = oc.get("meta", {}).get("lastTouchedVersion")
    upd = json.loads((BASE / "update-check.json").read_text())
    result["gateway"]["version_available"] = upd.get("lastAvailableVersion")
    result["gateway"]["update_available"] = (
        result["gateway"].get("version_installed") != result["gateway"].get("version_available"))
except: pass

# --- Health: Gateway antwortet auf 18789 (jede HTTP-Antwort = up) ---
try:
    urllib.request.urlopen("http://localhost:18789/", timeout=2)
    result["gateway"]["health_ok"] = True
except urllib.error.HTTPError:
    result["gateway"]["health_ok"] = True   # HTTP-Fehler = Port offen = Gateway läuft
except:
    result["gateway"]["health_ok"] = False

# --- Browser (Port 18791) ---
try:
    resp = urllib.request.urlopen("http://localhost:18791/", timeout=2)
    b = json.loads(resp.read())
    result["browser"] = {"running": b.get("running", False), "cdp_ready": b.get("cdpReady", False)}
except:
    result["browser"] = {"running": False}

# --- Telegram ---
try:
    tg_off = json.loads((BASE / "telegram" / "update-offset-default.json").read_text())
    oc_cfg = json.loads((BASE / "openclaw.json").read_text())
    tg = oc_cfg.get("channels", {}).get("telegram", {})
    result["telegram"] = {"enabled": tg.get("enabled", False),
                          "last_update_id": tg_off.get("lastUpdateId")}
except Exception as e:
    result["telegram"] = {"error": str(e)}

# --- Ollama (Ryzen) ---
try:
    urllib.request.urlopen("http://192.168.86.195:11434/", timeout=3)
    result["ollama"] = {"reachable": True}
except:
    result["ollama"] = {"reachable": False}

# --- Syncthing ---
try:
    cfg_xml = Path.home().joinpath(".local/state/syncthing/config.xml").read_text()
    m = re.search(r"<apikey>([^<]+)</apikey>", cfg_xml)
    stkey = m.group(1) if m else ""
    req = urllib.request.Request("http://localhost:8384/rest/system/connections",
                                 headers={"X-API-Key": stkey})
    conns = json.loads(urllib.request.urlopen(req, timeout=3).read())
    peers = [v.get("address","") for v in conns.get("connections",{}).values() if v.get("connected")]
    result["syncthing"] = {"connected": len(peers) > 0, "peers": peers}
except Exception as e:
    result["syncthing"] = {"connected": False, "error": str(e)}

# --- Input-Ordner ---
try:
    inp = Path.home() / "input-dispatcher"
    files = sorted(inp.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if inp.exists() else []
    result["input_folder"] = {
        "count": len(files),
        "files": [{"name": f.name,
                   "size_kb": round(f.stat().st_size/1024),
                   "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                  for f in files[:8]]
    }
except Exception as e:
    result["input_folder"] = {"count": 0, "error": str(e)}

# --- Cron-Jobs ---
try:
    jobs = json.loads((BASE / "cron" / "jobs.json").read_text()).get("jobs", [])
    result["cron_jobs"] = [
        {"name": j.get("name"), "enabled": j.get("enabled"),
         "schedule": j.get("schedule", {}).get("expr"),
         "last_status": j.get("state", {}).get("lastRunStatus"),
         "last_dur_ms": j.get("state", {}).get("lastDurationMs"),
         "last_run_ms": j.get("state", {}).get("lastRunAtMs"),
         "next_run_ms": j.get("state", {}).get("nextRunAtMs"),
         "errors": j.get("state", {}).get("consecutiveErrors", 0)}
        for j in jobs
    ]
except:
    result["cron_jobs"] = []

# --- Sessions ---
try:
    sess = json.loads((BASE / "agents" / "main" / "sessions" / "sessions.json").read_text())
    items = list(sess.items()) if isinstance(sess, dict) else list(enumerate(sess))
    recent = [{"label": v.get("label", str(k)), "updatedAt": v.get("updatedAt")}
              for k, v in reversed(items[-8:])]
    result["sessions"] = {"total": len(items), "recent": recent}
except Exception as e:
    result["sessions"] = {"total": 0, "error": str(e)}

print(json.dumps(result, ensure_ascii=False))
"""

_WILSON_HOST = os.environ.get("WILSON_PI_HOST", "192.168.3.124")


def _collect_wilson_status() -> dict:
    try:
        import paramiko, io
        key = paramiko.Ed25519Key.from_private_key_file("/ssh/id_ed25519")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(_WILSON_HOST, username="reinhard", pkey=key, timeout=5, banner_timeout=10)
        stdin, stdout, stderr = client.exec_command("python3", timeout=15)
        stdin.write(_WILSON_COLLECTOR)
        stdin.channel.shutdown_write()
        output = stdout.read().decode()
        client.close()
        if output.strip():
            return json.loads(output)
        err = stderr.read().decode()
        return {"error": err[:300] or "Keine Ausgabe", "ts": datetime.now().isoformat(timespec="seconds")}
    except Exception as e:
        return {"error": str(e), "ts": datetime.now().isoformat(timespec="seconds")}


_WILSON_TUI_PORT = int(os.environ.get("WILSON_TUI_PORT", "7681"))


def _fetch_wilson_tui_info() -> dict:
    """Liefert Host/Port/URL für die ttyd-TUI. Kein Auth — LAN-interne Nutzung."""
    return {
        "host": _WILSON_HOST,
        "port": _WILSON_TUI_PORT,
        "url":  f"http://{_WILSON_HOST}:{_WILSON_TUI_PORT}/",
    }


def _collect_wilson_logs(lines: int = 200) -> dict:
    """Holt die letzten N Zeilen des *aktuellen* openclaw-Logs vom Pi.
    Das Gateway benennt die Logdatei nach dem Startdatum, nicht nach dem heutigen
    Datum — wir wählen daher die zuletzt geänderte Datei unter /tmp/openclaw/."""
    lines = max(10, min(int(lines), 2000))
    try:
        import paramiko
        key = paramiko.Ed25519Key.from_private_key_file("/ssh/id_ed25519")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(_WILSON_HOST, username="reinhard", pkey=key, timeout=5, banner_timeout=10)
        cmd = (
            "F=$(ls -1t /tmp/openclaw/openclaw-*.log 2>/dev/null | head -n 1); "
            "if [ -n \"$F\" ] && [ -f \"$F\" ]; then "
            "  echo __FILE__$F; echo __SIZE__$(stat -c%%s \"$F\"); "
            "  echo __MTIME__$(stat -c%%Y \"$F\"); "
            "  echo __LINES__; tail -n %d \"$F\"; "
            "else echo __MISSING__; fi"
        ) % lines
        stdin, stdout, stderr = client.exec_command(cmd, timeout=10)
        out = stdout.read().decode(errors="replace")
        client.close()
        file_path, size, mtime, raw_lines, missing = None, None, None, [], False
        in_lines = False
        for ln in out.splitlines():
            if ln.startswith("__FILE__"):
                file_path = ln[len("__FILE__"):]
            elif ln.startswith("__SIZE__"):
                try: size = int(ln[len("__SIZE__"):])
                except: pass
            elif ln.startswith("__MTIME__"):
                try: mtime = int(ln[len("__MTIME__"):])
                except: pass
            elif ln.startswith("__MISSING__"):
                missing = True
            elif ln == "__LINES__":
                in_lines = True
            elif in_lines:
                raw_lines.append(ln)
        return {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "file": file_path,
            "missing": missing,
            "size_bytes": size,
            "mtime": mtime,
            "requested_lines": lines,
            "returned_lines": len(raw_lines),
            "lines": raw_lines,
        }
    except Exception as e:
        return {"error": str(e), "ts": datetime.now().isoformat(timespec="seconds")}


def _collect_health() -> dict:
    """Aggregiert Health-Status aller Workflow-Dienste."""
    services = {}

    # 1 — Dispatcher selbst (eigene DB)
    try:
        with get_db() as con:
            total  = con.execute("SELECT COUNT(*) FROM dokumente").fetchone()[0]
            today  = con.execute(
                "SELECT COUNT(*) FROM dokumente WHERE DATE(erstellt_am) = DATE('now')"
            ).fetchone()[0]
            last_r = con.execute(
                "SELECT dateiname, kategorie, typ, adressat, konfidenz, erstellt_am "
                "FROM dokumente ORDER BY id DESC LIMIT 1"
            ).fetchone()
        services["dispatcher"] = {
            "label": "Document Dispatcher",
            "status": "ok",
            "docs_total": total,
            "docs_today": today,
            "last_doc": dict(last_r) if last_r else None,
        }
    except Exception as e:
        services["dispatcher"] = {"label": "Document Dispatcher", "status": "error", "error": str(e)}

    # 2 — Docling Serve
    try:
        r = requests.get("http://docling-serve:5001/health", timeout=4)
        services["docling_serve"] = {
            "label": "Docling Serve (OCR)", "status": "ok" if r.status_code == 200 else "warn"
        }
    except Exception as e:
        services["docling_serve"] = {"label": "Docling Serve (OCR)", "status": "error", "error": str(e)}

    # 3 — Ollama
    try:
        r = requests.get("http://ollama:11434/api/tags", timeout=4)
        models = [m["name"] for m in r.json().get("models", [])]
        services["ollama"] = {
            "label": "Ollama (LLM)", "status": "ok",
            "models": models, "model_count": len(models),
        }
    except Exception as e:
        services["ollama"] = {"label": "Ollama (LLM)", "status": "error", "error": str(e)}

    # 4 — Syncthing
    try:
        hdrs = {"X-API-Key": SYNCTHING_API_KEY}
        rs = requests.get("http://syncthing:8384/rest/system/status", headers=hdrs, timeout=4)
        uptime_h = round(rs.json().get("uptime", 0) / 3600, 1)
        rc = requests.get("http://syncthing:8384/rest/system/connections", headers=hdrs, timeout=4)
        conns = rc.json().get("connections", {})
        connected = sum(1 for v in conns.values() if v.get("connected"))
        # Folder statuses
        rf = requests.get("http://syncthing:8384/rest/config/folders", headers=hdrs, timeout=4)
        folders_info = []
        overall_st = "ok"
        for folder in rf.json():
            fid = folder["id"]
            flabel = folder.get("label") or fid
            fs = requests.get(f"http://syncthing:8384/rest/db/status?folder={fid}", headers=hdrs, timeout=4).json()
            fe = requests.get(f"http://syncthing:8384/rest/folder/errors?folder={fid}", headers=hdrs, timeout=4).json()
            state = fs.get("state", "unknown")
            need = fs.get("needFiles", 0)
            errors = fs.get("errors", 0)
            file_errors = [e["error"] for e in (fe.get("errors") or [])[:3]]
            fstatus = "ok"
            if state == "error" or errors > 0:
                fstatus = "error"
                overall_st = "warn"
            elif need > 0:
                fstatus = "warn"
                if overall_st == "ok":
                    overall_st = "warn"
            folders_info.append({
                "id": fid, "label": flabel, "state": state,
                "need": need, "errors": errors,
                "file_errors": file_errors, "status": fstatus,
            })
        services["syncthing"] = {
            "label": "Syncthing", "status": overall_st,
            "uptime_h": uptime_h, "connections": f"{connected}/{len(conns)}",
            "folders": folders_info,
        }
    except Exception as e:
        services["syncthing"] = {"label": "Syncthing", "status": "error", "error": str(e)}

    # 4b — Syncthing Mac
    _MAC_DEVICE_ID = "GBO2KI7-XYW4XSL-X7YH7RR-42NXUG6-3G366HT-6SWV2WG-76BD7DA-YCUO7AP"
    try:
        hdrs = {"X-API-Key": SYNCTHING_API_KEY}
        rc = requests.get("http://syncthing:8384/rest/system/connections", headers=hdrs, timeout=4)
        mac_conn = rc.json().get("connections", {}).get(_MAC_DEVICE_ID, {})
        connected = mac_conn.get("connected", False)
        address   = mac_conn.get("address", "")
        # Geteilte Ordner mit Mac + Sync-Fortschritt
        rf = requests.get("http://syncthing:8384/rest/config/folders", headers=hdrs, timeout=4)
        mac_folders = []
        for folder in rf.json():
            dev_ids = [d["deviceID"] for d in folder.get("devices", [])]
            if _MAC_DEVICE_ID not in dev_ids:
                continue
            fid    = folder["id"]
            flabel = folder.get("label") or fid
            try:
                rcomp = requests.get(
                    f"http://syncthing:8384/rest/db/completion?device={_MAC_DEVICE_ID}&folder={fid}",
                    headers=hdrs, timeout=4,
                )
                comp = round(rcomp.json().get("completion", 100), 1)
            except Exception:
                comp = None
            mac_folders.append({"id": fid, "label": flabel, "completion": comp})
        services["syncthing_mac"] = {
            "label": "Mac Sync", "status": "ok" if connected else "warn",
            "connected": connected, "address": address, "folders": mac_folders,
        }
    except Exception as e:
        services["syncthing_mac"] = {"label": "Mac Sync", "status": "error", "error": str(e)}

    # 5 — Open WebUI
    try:
        r = requests.get("http://open-webui:8080/health", timeout=4)
        services["open_webui"] = {
            "label": "Open WebUI", "status": "ok" if r.status_code == 200 else "warn"
        }
    except Exception as e:
        services["open_webui"] = {"label": "Open WebUI", "status": "error", "error": str(e)}


    # 7 — mcpo / enzyme-Bridge (Host-Port 11180)
    _enzyme_hosts = ["host.docker.internal", "172.17.0.1", "192.168.3.1"]
    for _h in _enzyme_hosts:
        try:
            r = requests.post(f"http://{_h}:11180/status",
                              json={}, headers={"Content-Type": "application/json"}, timeout=4)
            if r.status_code == 200:
                es = r.json()
                if not isinstance(es, dict):
                    # enzyme noch nicht initialisiert oder falsche Vault-Konfiguration
                    services["enzyme"] = {"label": "enzyme / mcpo (Vault)", "status": "warn",
                                          "error": str(es)[:120] if es else "Keine Daten"}
                    break
                last_refresh = None
                # enzyme.db liegt immer im Vault unter .enzyme/enzyme.db
                _enzyme_db = Path("/data/reinhards-vault/.enzyme/enzyme.db")
                if not _enzyme_db.exists() and VAULT_ROOT:
                    _enzyme_db = VAULT_ROOT / ".enzyme" / "enzyme.db"
                enzyme_status = "ok"
                try:
                    mtime = _enzyme_db.stat().st_mtime
                    last_refresh = datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")
                    age_h = (time.time() - mtime) / 3600
                    if age_h > 48:
                        enzyme_status = "error"
                    elif age_h > 24:
                        enzyme_status = "warn"
                except Exception:
                    pass
                services["enzyme"] = {
                    "label":        "enzyme / mcpo (Vault)",
                    "status":       enzyme_status,
                    "documents":    es.get("documents"),
                    "embedded":     es.get("embedded"),
                    "catalysts":    es.get("catalysts"),
                    "entities":     es.get("entities"),
                    "last_refresh": last_refresh,
                }
                break
            elif r.status_code in (405, 404):
                services["enzyme"] = {"label": "enzyme / mcpo (Vault)", "status": "ok"}
                break
        except Exception:
            continue
    else:
        services["enzyme"] = {"label": "enzyme / mcpo (Vault)", "status": "error", "error": "Port 11180 nicht erreichbar"}

    # 8 — Wilson / OpenClaw (Pi reachable via SSH port)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((WILSON_PI_HOST, 22))
        sock.close()
        services["wilson_pi"] = {
            "label": f"Wilson / OpenClaw (Pi {WILSON_PI_HOST})",
            "status": "ok" if result == 0 else "error",
        }
    except Exception as e:
        services["wilson_pi"] = {"label": f"Wilson / OpenClaw (Pi)", "status": "error", "error": str(e)}

    host_ip = os.environ.get("HOST_IP", "localhost")

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "host_ip":   host_ip,
        "services":  services,
        "overall":   "ok" if all(s["status"] == "ok" for s in services.values()) else "warn",
    }


# ── Frontmatter-Upgrade Helpers ────────────────────────────────────────────

def _fm_classify(keys: set) -> str:
    if "kategorie_id" in keys and "adressat" in keys:
        return "Dispatcher v2"
    if "kategorie_id" in keys:
        return "Dispatcher v2 (teilw.)"
    if "kategorie" in keys and "original" in keys:
        return "Dispatcher v1"
    if "todos" in keys:
        return "OCR-Stub"
    if "imported" in keys and "created" in keys:
        return "Apple Notes"
    if "date created" in keys and "imported" in keys:
        return "Evernote+Import"
    if "date created" in keys:
        return "Evernote"
    if "category" in keys and "source" in keys:
        return "Legacy"
    return "Sonstige"


def _fm_parse_date(val) -> str:
    if val is None:
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    s = re.sub(r"(\d{4}-\d{2})-00\b", r"\1-01", s)  # fix invalid day=00
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # Obsidian: "Wednesday, February 11th 2026, 8:34:58 am"
    m = re.search(r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)\s+(\d{4})", s)
    if m:
        try:
            from datetime import datetime as _dt
            return _dt.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


_FM_SKIP_NAMES = {"CLAUDE.md", "ENZYME_GUIDE.md", "VAULT_GUIDE.md"}


def _fm_mtime_date(md_path: Path) -> str:
    return datetime.fromtimestamp(md_path.stat().st_mtime).strftime("%Y-%m-%d")


def _fm_probe(md_path: Path) -> dict:
    if md_path.name in _FM_SKIP_NAMES or "Anlagen" in md_path.parts:
        return {"schema": "übersprungen", "changes": [], "can_upgrade": False,
                "current": {}, "upgraded": {}}
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": str(e), "can_upgrade": False}

    # No frontmatter at all → insert minimal block
    if not text.startswith("---"):
        mdate = _fm_mtime_date(md_path)
        m2 = re.match(r"^(\d{8})", md_path.stem)
        if m2:
            d = m2.group(1)
            mdate = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            src_note = "Dateiname"
        else:
            src_note = "Datei-Änderungsdatum"
        return {"schema": "kein Frontmatter", "can_upgrade": True, "current": {},
                "upgraded": {"erstellt_am": mdate, "tags": [], "datumUnsicher": src_note == "Datei-Änderungsdatum"},
                "changes": [f'Frontmatter eingefügt — erstellt_am: "{mdate}" ← {src_note}',
                            "tags: []  ← hinzugefügt"]}

    m = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not m:
        return {"schema": "kein Frontmatter", "changes": [], "can_upgrade": False,
                "current": {}, "upgraded": {}}

    # Pre-process raw YAML: fix invalid day=00
    fm_raw = re.sub(r"(\d{4}-\d{2})-00\b", r"\1-01", m.group(1))
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except Exception:
        # Invalid YAML: try raw regex extraction for date
        fm = {}
        dm = re.search(r"(?:date created|created|erstellt|date):\s*['\"]?(\d{4}-\d{2}-\d{2})", fm_raw)
        if dm:
            fm["_raw_date"] = dm.group(1)

    keys = set(fm.keys())
    schema = _fm_classify(keys) if "_raw_date" not in keys else "Ungültiges YAML (reparierbar)"
    changes = []
    new_fm = dict(fm)

    if "erstellt_am" not in fm:
        val = None
        for src in ("date created", "created", "erstellt", "_raw_date"):
            if src in fm:
                val = _fm_parse_date(fm[src])
                if val:
                    changes.append(f'erstellt_am: "{val}"  ← aus \'{src}\'')
                    break
        if val is None:
            m2 = re.match(r"^(\d{8})", md_path.stem)
            if m2:
                d = m2.group(1)
                val = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                changes.append(f'erstellt_am: "{val}"  ← Dateiname')
        if val is None:
            val = _fm_mtime_date(md_path)
            changes.append(f'erstellt_am: "{val}"  ← Datei-Änderungsdatum')
            new_fm["datumUnsicher"] = True
        if val is not None:
            new_fm["erstellt_am"] = val
        new_fm.pop("_raw_date", None)

    if "tags" not in fm:
        new_fm["tags"] = []
        changes.append("tags: []  ← hinzugefügt")

    new_fm.pop("_raw_date", None)
    return {
        "schema": schema,
        "current": fm,
        "upgraded": new_fm if changes else fm,
        "changes": changes,
        "can_upgrade": len(changes) > 0,
    }


def _fm_apply_upgrade(md_path: Path) -> dict:
    probe = _fm_probe(md_path)
    if not probe.get("can_upgrade"):
        return {"ok": False, "reason": "Keine Änderungen notwendig", "changes": []}
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")

        # No frontmatter: prepend block
        if not text.startswith("---"):
            new_yaml = yaml.dump(probe["upgraded"], allow_unicode=True,
                                 default_flow_style=False, sort_keys=False)
            md_path.write_text(f"---\n{new_yaml}---\n{text}", encoding="utf-8")
            return {"ok": True, "changes": probe["changes"]}

        m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
        if not m:
            return {"ok": False, "reason": "Frontmatter-Block nicht gefunden"}
        body = m.group(2)
        new_yaml = yaml.dump(probe["upgraded"], allow_unicode=True,
                             default_flow_style=False, sort_keys=False)
        md_path.write_text(f"---\n{new_yaml}---\n{body}", encoding="utf-8")
        return {"ok": True, "changes": probe["changes"]}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


_FM_STATS_CACHE: dict = {}
_FM_STATS_CACHE_TS: float = 0.0
_FM_STATS_LOCK = threading.Lock()


def _fm_stats(force: bool = False) -> dict:
    global _FM_STATS_CACHE, _FM_STATS_CACHE_TS
    with _FM_STATS_LOCK:
        if not force and _FM_STATS_CACHE and (time.time() - _FM_STATS_CACHE_TS) < 300:
            return _FM_STATS_CACHE
    if VAULT_ROOT is None:
        return {"error": "VAULT_ROOT nicht konfiguriert"}
    schema_counts: dict = {}
    upgradeable = 0
    already_unified = 0
    no_fm = 0
    total = 0
    for md_path in VAULT_ROOT.rglob("*.md"):
        if any(p.startswith(".") for p in md_path.parts):
            continue
        total += 1
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not text.startswith("---"):
            no_fm += 1
            schema_counts["kein Frontmatter"] = schema_counts.get("kein Frontmatter", 0) + 1
            continue
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not match:
            no_fm += 1
            schema_counts["kein Frontmatter"] = schema_counts.get("kein Frontmatter", 0) + 1
            continue
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except Exception:
            schema_counts["Ungültiges YAML"] = schema_counts.get("Ungültiges YAML", 0) + 1
            continue
        keys = set(fm.keys())
        schema = _fm_classify(keys)
        schema_counts[schema] = schema_counts.get(schema, 0) + 1
        has_unified = "erstellt_am" in keys and "tags" in keys
        if has_unified:
            already_unified += 1
        else:
            can_upg = False
            if "erstellt_am" not in keys:
                for src in ("date created", "created", "erstellt"):
                    if src in keys and _fm_parse_date(fm.get(src)):
                        can_upg = True
                        break
                if not can_upg:
                    if re.match(r"^\d{8}", md_path.stem):
                        can_upg = True
            if not can_upg and "tags" not in keys:
                can_upg = True
            if can_upg:
                upgradeable += 1
    result = {
        "total": total,
        "no_frontmatter": no_fm,
        "unified": already_unified,
        "upgradeable": upgradeable,
        "schemas": dict(sorted(schema_counts.items(), key=lambda x: -x[1])),
        "unified_pct": round(already_unified / total * 100, 1) if total else 0,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _FM_STATS_LOCK:
        _FM_STATS_CACHE = result
        _FM_STATS_CACHE_TS = time.time()
    return result


_FM_BATCH_STATE: dict = {"running": False, "done": 0, "total": 0, "errors": 0, "finished": False}
_FM_BATCH_LOCK = threading.Lock()


def _fm_batch_upgrade_all():
    global _FM_BATCH_STATE, _FM_STATS_CACHE_TS
    if VAULT_ROOT is None:
        return
    paths = [p for p in VAULT_ROOT.rglob("*.md")
             if not any(part.startswith(".") for part in p.parts)]
    with _FM_BATCH_LOCK:
        _FM_BATCH_STATE = {"running": True, "done": 0, "total": len(paths),
                           "errors": 0, "finished": False}
    done = 0
    errors = 0
    for md_path in paths:
        try:
            result = _fm_apply_upgrade(md_path)
            if not result.get("ok") and result.get("reason") != "Keine Änderungen notwendig":
                errors += 1
        except Exception:
            errors += 1
        done += 1
        if done % 50 == 0:
            with _FM_BATCH_LOCK:
                _FM_BATCH_STATE["done"] = done
                _FM_BATCH_STATE["errors"] = errors
    with _FM_BATCH_LOCK:
        _FM_BATCH_STATE = {"running": False, "done": done, "total": len(paths),
                           "errors": errors, "finished": True}
    _FM_STATS_CACHE_TS = 0.0  # invalidate stats cache


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling Workflow · Dashboard</title>
<style>
:root{--bg:#f4f5f7;--surface:#fff;--border:#dde1ea;--text:#1a1d2e;--muted:#6b7280;--ok:#059669;--warn:#d97706;--err:#dc2626;--accent:#4f46e5}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}

/* Header */
header{border-bottom:1px solid var(--border);padding:13px 24px;display:flex;align-items:center;gap:10px;background:var(--surface);flex-wrap:wrap}
header h1{font-size:16px;font-weight:700;color:var(--accent);white-space:nowrap;margin-right:4px}
nav a{font-size:12px;padding:4px 12px;border:1px solid var(--border);border-radius:7px;color:var(--text);text-decoration:none;font-weight:600;white-space:nowrap;transition:all .15s;margin-right:5px}
nav a:hover,nav a.hl{border-color:var(--accent);color:var(--accent)}
.sse-wrap{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-left:auto}
.sse-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);transition:background .4s}
.overall{font-size:12px;padding:4px 12px;border-radius:999px;font-weight:700}
.overall.ok{background:#d1fae5;color:var(--ok)}.overall.warn{background:#fef3c7;color:var(--warn)}.overall.err{background:#fee2e2;color:var(--err)}
.ts{font-size:12px;color:var(--muted);white-space:nowrap}

/* Flow strip */
.flow{background:linear-gradient(135deg,#eef2ff 0%,#f0fdf4 100%);border-bottom:1px solid var(--border);padding:22px 28px;overflow-x:auto}
.flow-title{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px}
.flow-inner{display:flex;align-items:center;gap:0;min-width:max-content}
.fstep{display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px 16px;border-radius:12px;background:var(--surface);border:1.5px solid var(--border);min-width:96px;position:relative;cursor:default;transition:box-shadow .2s,border-color .2s;text-decoration:none;color:inherit}
a.fstep:hover{border-color:var(--accent);box-shadow:0 4px 16px rgba(79,70,229,.15)}
.fstep .fi{font-size:24px;line-height:1}
.fstep .fl{font-size:11px;font-weight:700;color:var(--text);white-space:nowrap}
.fstep .fs{font-size:10px;color:var(--muted);white-space:nowrap}
.fdot{position:absolute;top:6px;right:8px;width:10px;height:10px;border-radius:50%;background:#d1d5db;border:2px solid #fff;transition:background .4s}
.fdot.ok{background:var(--ok)}.fdot.warn{background:var(--warn)}.fdot.error{background:var(--err)}
.farr{color:var(--accent);font-size:16px;padding:0 4px;flex-shrink:0;opacity:.45}
.fgroup-label{font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin:0 6px;align-self:flex-end;padding-bottom:6px;white-space:nowrap}

/* Service cards */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;padding:18px 24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:box-shadow .2s,border-color .2s;position:relative}
.card:hover{border-color:var(--accent);box-shadow:0 4px 16px rgba(79,70,229,.08)}
.card.ok  {border-left:5px solid var(--ok)}
.card.warn{border-left:5px solid var(--warn)}
.card.error{border-left:5px solid var(--err)}
.card-header{display:flex;align-items:center;gap:10px;padding:14px 16px 10px}
.card-icon{font-size:24px;flex-shrink:0}
.card-title{font-size:14px;font-weight:700;color:var(--text);flex:1;line-height:1.3}
.sbadge{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:700;white-space:nowrap;flex-shrink:0}
.sbadge.ok{background:#d1fae5;color:var(--ok)}.sbadge.warn{background:#fef3c7;color:var(--warn)}.sbadge.error{background:#fee2e2;color:var(--err)}
.card-body{padding:0 16px 14px;display:flex;flex-direction:column;gap:6px}
.metric{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.metric-label{color:var(--muted);font-size:12px;flex-shrink:0}
.metric-value{font-size:13px;font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
.model-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px}
.model-tag{font-size:11px;background:#f1f2f6;border:1px solid var(--border);border-radius:4px;padding:2px 7px}
.err-msg{font-size:11px;color:var(--err);margin-top:4px;word-break:break-all}
/* Tooltip (card description on hover) */
.card-desc-tip{display:none;position:absolute;left:0;right:0;bottom:calc(100% + 6px);background:#1e2240;color:#e0e7ff;font-size:12px;line-height:1.6;padding:12px 14px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.25);z-index:100;border:1px solid #3730a3;pointer-events:none}
.card:hover .card-desc-tip{display:block}

/* enzyme detail panel */
.enzyme-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:4px}
.estat{background:#f8f9fb;border:1px solid var(--border);border-radius:8px;padding:7px 10px;text-align:center}
.estat .ev{font-size:17px;font-weight:700;color:var(--accent)}
.estat .el{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}

/* Rescan banner */
#rescan-banner{display:none;margin:0 24px;padding:12px 16px;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;align-items:center;gap:12px;font-size:13px;color:#3730a3}
#rescan-banner .rb-bar-wrap{flex:1;height:8px;background:#c7d2fe;border-radius:4px;overflow:hidden}
#rescan-banner .rb-bar{height:100%;background:var(--accent);width:0%;transition:width .5s;border-radius:4px}

/* Filter bar */
.filter-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin:14px 24px 0;padding:14px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px}
.fg{display:flex;flex-direction:column;gap:3px}
.fg label{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.fg input,.fg select{font-size:13px;padding:5px 9px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);height:32px}
.fg input:focus,.fg select:focus{border-color:var(--accent);outline:none}
.fg.wide input{width:200px}.fg.med input,.fg.med select{width:155px}.fg.sm input,.fg.sm select{width:125px}
.filter-results{margin-left:auto;font-size:12px;color:var(--muted);align-self:center}
.btn-reset{font-size:12px;padding:5px 12px;height:32px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--muted);cursor:pointer;font-weight:600;align-self:flex-end}
.btn-reset:hover{border-color:var(--err);color:var(--err)}

/* Docs table */
.docs-section{margin:12px 24px 24px}
.docs-table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.04)}
.docs-table th{text-align:left;padding:10px 14px;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);background:#f8f9fb}
.docs-table td{padding:9px 14px;font-size:13px;border-bottom:1px solid #f0f1f5;vertical-align:middle}
.docs-table tr:last-child td{border-bottom:none}
.docs-table tr:hover td{background:#f8f9fb}
.kbadge{font-size:11px;padding:2px 7px;border-radius:999px;font-weight:600}
.kbadge.hoch{background:#d1fae5;color:var(--ok)}.kbadge.mittel{background:#fef3c7;color:var(--warn)}.kbadge.niedrig{background:#fee2e2;color:var(--err)}.kbadge.null{background:#f1f2f6;color:var(--muted)}
.cat-tag{font-size:12px;color:var(--accent);font-weight:500}
.adressat-tag{font-size:11px;padding:1px 7px;border-radius:4px;background:#f1f2f6}
@keyframes flashRow{0%{background:#e0e7ff}100%{background:transparent}}

/* Footer */
.footer{text-align:center;padding:12px;font-size:12px;color:var(--muted);background:var(--surface);border-top:1px solid var(--border);margin-top:8px}
#countdown{color:var(--accent);font-weight:700}
</style>
</head>
<body>

<!-- Header -->
<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
  <h1>Docling Workflow</h1>
  <nav>
    <a href="/pipeline" class="hl" id="nav-pipeline">⚡ Pipeline <span id="nav-queue-badge" style="display:none;background:var(--warn);color:#fff;border-radius:999px;padding:1px 7px;font-size:10px;font-weight:700;margin-left:4px">0</span></a>
    <a href="/review" target="_blank" rel="noopener">📋 Review</a>
    <a href="/vault" target="_blank" rel="noopener">📁 Vault</a>
    <a href="/cache" target="_blank" rel="noopener">🔍 Cache</a>
    <a href="/batch" target="_blank" rel="noopener">🧰 Batch</a>
    <a href="/wilson" target="_blank" rel="noopener">🥧 Wilson</a>
    <a href="/duplikate" target="_blank" rel="noopener">&#127366; Duplikate</a>
    <a href="/frontmatter" target="_blank" rel="noopener">🏷️ Frontmatter</a>
  </nav>
  <div class="sse-wrap">
    <span class="sse-dot" id="sse-dot"></span>Live
  </div>
  <span class="overall" id="overall-badge">…</span>
  <span class="ts" id="ts">Laden…</span>
  <button class="help-btn" onclick="openHelp()">❓ Hilfe</button>
</header>

<!-- Pipeline flow -->
<div class="flow">
  <div class="flow-title">Dokumenten-Pipeline</div>
  <div class="flow-inner">
    <span class="fstep" title="Foto oder PDF per Telegram senden">
      <span class="fi">📱</span><span class="fl">Telegram</span><span class="fs">Foto / PDF</span>
    </span>
    <span class="farr">→</span>

    <a class="fstep" id="fstep-wilson" href="/wilson">
      <span class="fdot" id="fdot-wilson_pi"></span>
      <span class="fi">🥧</span><span class="fl">Wilson / Pi</span><span class="fs">OpenClaw</span>
    </a>
    <span class="farr">→</span>

    <a class="fstep" id="fstep-syncthing" href="#">
      <span class="fdot" id="fdot-syncthing"></span>
      <span class="fi">🔄</span><span class="fl">Syncthing</span><span class="fs">Pi → Ryzen</span>
    </a>
    <span class="farr">→</span>

    <span class="fstep">
      <span class="fdot" id="fdot-docling_serve"></span>
      <span class="fi">🔍</span><span class="fl">Docling OCR</span><span class="fs">PDF → Markdown</span>
    </span>
    <span class="farr">→</span>

    <span class="fstep">
      <span class="fi">🌐</span><span class="fl">Spracherkennung</span><span class="fs">DE / IT / EN</span>
    </span>
    <span class="farr">→</span>

    <a class="fstep" id="fstep-ollama" href="#">
      <span class="fdot" id="fdot-ollama"></span>
      <span class="fi">🤖</span><span class="fl">Ollama LLM</span><span class="fs">Klassifikation</span>
    </a>
    <span class="farr">→</span>

    <a class="fstep" href="/pipeline">
      <span class="fdot" id="fdot-dispatcher"></span>
      <span class="fi">📄</span><span class="fl">Dispatcher</span><span class="fs">Routing + DB</span>
    </a>
    <span class="farr">→</span>

    <a class="fstep" href="/vault">
      <span class="fi">📁</span><span class="fl">Obsidian Vault</span><span class="fs">MD + PDF</span>
    </a>
    <span class="farr">→</span>

    <a class="fstep" id="fstep-enzyme" href="#">
      <span class="fdot" id="fdot-enzyme"></span>
      <span class="fi">🧪</span><span class="fl">enzyme MCP</span><span class="fs">Vault indexieren</span>
    </a>
    <span class="farr">→</span>

    <span class="fgroup-label">Suche via</span>
    <a class="fstep" id="fstep-openwebui" href="#">
      <span class="fdot" id="fdot-open_webui"></span>
      <span class="fi">💬</span><span class="fl">Open WebUI</span><span class="fs">Chat + Suche</span>
    </a>
    <span class="farr">/</span>
    <span class="fstep" title="Claude Code CLI — nutzt enzyme MCP für Vault-Suche">
      <span class="fi">🤖</span><span class="fl">Claude Code</span><span class="fs">MCP CLI</span>
    </span>

  </div>
</div>

<!-- Service cards -->
<div class="grid" id="grid"></div>

<!-- Rescan banner -->
<div id="rescan-banner">
  <span>🔁 Rescan läuft:</span>
  <div class="rb-bar-wrap"><div class="rb-bar" id="rb-bar"></div></div>
  <span id="rb-label" style="font-weight:700">0 / 0</span>
  <span id="rb-current" style="font-size:12px;color:#6366f1;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
  <button onclick="stopRescan()" style="margin-left:auto;font-size:12px;padding:4px 12px;border:1.5px solid var(--err);color:var(--err);background:transparent;border-radius:6px;cursor:pointer;font-weight:700">■ Stop</button>
</div>

<!-- Filter bar -->
<div class="filter-bar">
  <div class="fg wide"><label>Suche</label><input id="f-q" type="search" placeholder="Dateiname, Absender…" oninput="scheduleFilter()"></div>
  <div class="fg med"><label>Kategorie</label><select id="f-kat" onchange="onKatChange()"><option value="">Alle</option></select></div>
  <div class="fg sm"><label>Adressat</label><select id="f-adr" onchange="loadDocs()"><option value="">Alle</option><option>Reinhard</option><option>Marion</option></select></div>
  <div class="fg sm"><label>Konfidenz</label><select id="f-konfid" onchange="loadDocs()"><option value="">Alle</option><option value="hoch">Hoch</option><option value="mittel">Mittel</option><option value="niedrig">Niedrig</option></select></div>
  <div class="fg sm"><label>Von</label><input id="f-von" type="date" onchange="loadDocs()"></div>
  <div class="fg sm"><label>Bis</label><input id="f-bis" type="date" onchange="loadDocs()"></div>
  <button class="btn-reset" onclick="resetFilter()">✕ Zurücksetzen</button>
  <span class="filter-results" id="filter-results"></span>
</div>

<!-- Docs table -->
<div class="docs-section">
  <table class="docs-table">
    <thead>
      <tr><th>Datum</th><th>Dateiname</th><th>Kategorie</th><th>Absender</th><th>Adressat</th><th>Konfidenz</th><th>Verarbeitet</th></tr>
    </thead>
    <tbody id="docs-body"><tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">Laden…</td></tr></tbody>
  </table>
</div>
<div class="footer">Auto-Refresh in <span id="countdown">30</span>s &nbsp;·&nbsp; <a href="#" onclick="loadAll();return false;" style="color:var(--accent)">Jetzt aktualisieren</a></div>

<script>
// Service metadata: icon, link-fn, description
const SVC = {
  wilson_pi:    { icon:'🥧', label:'Wilson / Pi',      urlFn: _ => `/wilson`,
    desc: 'Raspberry Pi 5 als Vorverarbeitungs-Station. Empfängt Scan-PDFs, führt OCR (via Ryzen-Proxy) und LLM-Klassifikation durch, erstellt Sidecar-Metadaten (.meta.json) und überträgt beides per Syncthing. Der Dispatcher übernimmt das Dokument dann im Bypass-Modus – ohne erneutes OCR oder LLM.' },
  syncthing:    { icon:'🔄', label:'Syncthing',         urlFn: ip => `http://${ip}:8384/`,
    desc: 'Dezentrale P2P-Synchronisation ohne Cloud. Überträgt neue PDFs vom Raspberry Pi auf den Ryzen-Server und hält den Obsidian Vault auf allen Geräten aktuell.' },
  syncthing_mac:{ icon:'💻', label:'Mac Sync',          urlFn: _ => null,
    desc: 'Syncthing-Verbindung zum Mac. Überträgt gescannte PDFs via smb://192.168.3.124/incoming (Wilson) und hält den Obsidian Vault (Reinhards Vault) auf dem Mac aktuell.' },
  docling_serve:{ icon:'🔍', label:'Docling OCR',       urlFn: _ => null,
    desc: 'Wandelt PDFs mit KI-basierter Texterkennung (OCR) in durchsuchbaren Markdown um. Versteht Tabellen, Spalten und Bilder. Basis für die anschließende LLM-Klassifikation.' },
  ollama:       { icon:'🤖', label:'Ollama LLM',        urlFn: ip => `http://${ip}:11434/`,
    desc: 'Lokales Large Language Model – läuft vollständig auf dem Ryzen, kein Cloud-Zugriff. Übernimmt Spracherkennung, Übersetzung (DE/IT→DE) und semantische Klassifikation nach Kategorie, Absender und Adressat (Fallback-Pfad ohne Wilson-Sidecar).' },
  dispatcher:   { icon:'📄', label:'Dispatcher',        urlFn: _ => `/pipeline`,
    desc: 'Herzstück der Pipeline. Überwacht den Eingangsordner, koordiniert OCR und Klassifikation, schreibt Ergebnis-MD in den Obsidian Vault und benachrichtigt via Telegram. Verwaltet Dokumenten-Datenbank und Konfidenz-Historie.' },
  enzyme:       { icon:'🧪', label:'enzyme MCP',        urlFn: ip => `http://${ip}:11180/docs`,
    desc: 'Semantische Suchschicht über den Obsidian Vault. Indexiert alle Vault-MD-Dateien als Katalysatoren und Entitäten, stellt Vault-Inhalte als MCP-Tools für Claude Code (CLI) und Open WebUI bereit. Ermöglicht natürlichsprachliche Suche über 1.000+ Dokumente.' },
  open_webui:   { icon:'💬', label:'Open WebUI',        urlFn: ip => `http://${ip}:3000/`,
    desc: 'Browser-Interface für Gespräche mit den lokalen Ollama-Modellen und dem Vault-Assistenten. Ermöglicht natürlichsprachliche Vault-Suche über den enzyme-MCP-Server.' },
};

// Card render order = same as flow
const CARD_ORDER = ['wilson_pi','syncthing','syncthing_mac','docling_serve','ollama','dispatcher','enzyme','open_webui'];

let _hostIp = 'localhost';

function stLabel(s){ return s==='ok'?'OK':s==='warn'?'WARN':'FEHLER'; }

function renderCard(key, svc) {
  const st   = svc.status || 'error';
  const meta = SVC[key] || {};
  let body   = '';

  if (key === 'dispatcher') {
    const last = svc.last_doc;
    const lastTxt = last
      ? `${(last.absender||last.dateiname||'—').slice(0,28)} (${(last.erstellt_am||'').slice(0,16)})`
      : '—';
    body = `
      <div class="metric"><span class="metric-label">Dokumente gesamt</span><span class="metric-value">${svc.docs_total??'—'}</span></div>
      <div class="metric"><span class="metric-label">Heute verarbeitet</span><span class="metric-value">${svc.docs_today??'—'}</span></div>
      <div class="metric"><span class="metric-label">Letztes Dokument</span><span class="metric-value" title="${last?.dateiname||''}">${lastTxt}</span></div>`;
  } else if (key === 'ollama') {
    const tags = (svc.models||[]).map(m=>`<span class="model-tag">${m}</span>`).join('');
    body = `
      <div class="metric"><span class="metric-label">Geladene Modelle</span><span class="metric-value">${svc.model_count??0}</span></div>
      <div class="model-tags">${tags||'<span style="color:var(--muted);font-size:12px">Keine Modelle geladen</span>'}</div>`;
  } else if (key === 'syncthing') {
    const folderRows = (svc.folders||[]).map(f => {
      const icon = f.status==='ok' ? '✅' : f.status==='warn' ? '⚠️' : '❌';
      const detail = f.errors > 0 ? ` · ${f.errors} Fehler` : f.need > 0 ? ` · ${f.need} fehlend` : '';
      const errLines = (f.file_errors||[]).map(e=>`<div style="font-size:10px;color:var(--err);padding-left:16px;word-break:break-all">${e}</div>`).join('');
      return `<div class="metric" style="flex-direction:column;align-items:flex-start;gap:2px">
        <span style="font-size:12px">${icon} <b>${f.label}</b><span style="color:var(--muted);font-size:11px">${detail}</span></span>
        ${errLines}
      </div>`;
    }).join('');
    body = `
      <div class="metric"><span class="metric-label">Uptime</span><span class="metric-value">${svc.uptime_h??'—'} h</span></div>
      <div class="metric"><span class="metric-label">Verbindungen</span><span class="metric-value">${svc.connections??'—'}</span></div>
      ${folderRows}`;
  } else if (key === 'syncthing_mac') {
    const connTxt = svc.connected ? '🟢 Verbunden' : '🔴 Getrennt';
    const addrTxt = svc.address ? `<div class="metric"><span class="metric-label">Adresse</span><span class="metric-value" style="font-size:11px">${svc.address}</span></div>` : '';
    const folderRows = (svc.folders||[]).map(f => {
      const pct = f.completion;
      const icon = pct === null ? '⚙️' : pct >= 100 ? '✅' : '⚠️';
      const pctTxt = pct === null ? '…' : pct + '%';
      return `<div class="metric"><span class="metric-label">${icon} ${f.label}</span><span class="metric-value">${pctTxt}</span></div>`;
    }).join('');
    body = `
      <div class="metric"><span class="metric-label">Verbindung</span><span class="metric-value">${connTxt}</span></div>
      ${addrTxt}
      ${folderRows||'<div class="metric" style="color:var(--muted);font-size:12px">Keine gemeinsamen Ordner</div>'}`;
  } else if (key === 'enzyme') {
    const pct = svc.documents > 0 ? Math.round((svc.embedded||0)/svc.documents*100) : 0;
    const enzymeUrl = SVC.enzyme.urlFn(_hostIp);
    body = `
      <div class="enzyme-stats">
        <div class="estat"><div class="ev">${svc.documents??'—'}</div><div class="el">Dokumente</div></div>
        <div class="estat"><div class="ev">${svc.embedded??'—'}</div><div class="el">Embeddings (${pct}%)</div></div>
        <div class="estat"><div class="ev">${svc.catalysts??'—'}</div><div class="el">Katalysatoren</div></div>
        <div class="estat"><div class="ev">${svc.entities??'—'}</div><div class="el">Entitäten</div></div>
      </div>
      <div class="metric" style="margin-top:6px"><span class="metric-label">Letzte Aktualisierung</span><span class="metric-value" style="color:var(--accent)">${svc.last_refresh??'—'}</span></div>
      <div style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <button id="enzyme-refresh-btn" onclick="triggerEnzymeRefresh()" style="font-size:12px;padding:4px 12px;border-radius:6px;border:1px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer;font-weight:600">⟳ Aktualisieren</button>
        <a href="${enzymeUrl}" target="_blank" style="font-size:12px;color:var(--accent);text-decoration:none;border:1px solid var(--border);padding:4px 10px;border-radius:6px">API-Docs ↗</a>
        <span id="enzyme-refresh-status" style="font-size:12px;color:var(--muted)"></span>
      </div>`;
  }

  const errHtml = svc.error ? `<div class="err-msg">⚠ ${svc.error}</div>` : '';
  let url = meta.urlFn ? meta.urlFn(_hostIp) : null;
  if (key === 'syncthing_mac' && svc.address) {
    const m = svc.address.match(/(?:tcp:\/\/)?([\d.]+):\d+/);
    if (m) url = `http://${m[1]}:8384/`;
  }
  const target = (key==='dispatcher'||key==='wilson_pi') ? '' : 'target="_blank"';
  const titleHtml = url
    ? `<a href="${url}" ${target} style="color:inherit;text-decoration:none;border-bottom:1px dashed #c7d2fe"
           onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='inherit'">${svc.label||meta.label||key}</a>`
    : (svc.label||meta.label||key);

  const descHtml = meta.desc
    ? `<div class="card-desc-tip">${meta.desc}</div>`
    : '';

  return `<div class="card ${st}">
    ${descHtml}
    <div class="card-header">
      <span class="card-icon">${meta.icon||'⚙️'}</span>
      <span class="card-title">${titleHtml}</span>
      <span class="sbadge ${st}">${stLabel(st)}</span>
    </div>
    <div class="card-body">${body}${errHtml}</div>
  </div>`;
}

async function loadData() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    document.getElementById('ts').textContent = 'Stand: ' + (data.timestamp||'').replace('T',' ').slice(0,16);
    if (data.host_ip) _hostIp = data.host_ip;

    const badge = document.getElementById('overall-badge');
    badge.textContent = data.overall==='ok' ? '✓ Alle Dienste aktiv' : '⚠ Probleme erkannt';
    badge.className = 'overall ' + (data.overall==='ok'?'ok':'warn');

    const svcs = data.services || {};

    // Update flow dots + dynamic links
    ['dispatcher','docling_serve','ollama','syncthing','wilson_pi','enzyme','open_webui'].forEach(k => {
      const dot = document.getElementById('fdot-'+k);
      if (dot && svcs[k]) dot.className = 'fdot ' + (svcs[k].status||'');
    });
    const setHref = (id, url) => { const el=document.getElementById(id); if(el&&url) el.href=url; };
    setHref('fstep-syncthing',  SVC.syncthing.urlFn(_hostIp));
    setHref('fstep-ollama',     SVC.ollama.urlFn(_hostIp));
    setHref('fstep-enzyme',     SVC.enzyme.urlFn(_hostIp));
    setHref('fstep-openwebui',  SVC.open_webui.urlFn(_hostIp));

    // Render cards in flow order
    document.getElementById('grid').innerHTML =
      CARD_ORDER.filter(k=>svcs[k]).map(k=>renderCard(k,svcs[k])).join('');

    // Rescan banner
    const rs = data.rescan_state || {};
    const banner = document.getElementById('rescan-banner');
    if (rs.active) {
      banner.style.display = 'flex';
      const pct = rs.total>0 ? Math.round(rs.done/rs.total*100) : 0;
      document.getElementById('rb-bar').style.width = pct+'%';
      document.getElementById('rb-label').textContent = `${rs.done} / ${rs.total} (${pct}%)`;
      document.getElementById('rb-current').textContent = rs.current ? '↳ '+rs.current : '';
    } else {
      banner.style.display = 'none';
    }
  } catch(e) { document.getElementById('ts').textContent = 'Fehler beim Laden'; }
}

async function stopRescan() { await fetch('/api/rescan/stop',{method:'POST'}); }

async function triggerEnzymeRefresh() {
  const btn = document.getElementById('enzyme-refresh-btn');
  const st  = document.getElementById('enzyme-refresh-status');
  if (btn){btn.disabled=true;btn.textContent='⟳ Läuft…';}
  if (st) st.textContent='Gestartet…';
  try {
    const r = await fetch('/api/enzyme-refresh',{method:'POST'});
    const d = await r.json();
    if (st) st.textContent = d.status==='running' ? 'Läuft im Hintergrund…' : (d.error||'');
  } catch(e) {
    if (st) st.textContent='Fehler';
    if (btn){btn.disabled=false;btn.textContent='⟳ Aktualisieren';}
  }
}

// Categories
let _allCats = {};
async function loadCategories() {
  try {
    const r = await fetch('/api/categories');
    _allCats = await r.json();
    const sel = document.getElementById('f-kat');
    Object.entries(_allCats).forEach(([id,c])=>{
      const o=document.createElement('option'); o.value=id; o.textContent=c.label||id; sel.appendChild(o);
    });
  } catch(_){}
}
function onKatChange() {
  loadDocs();
}
let _ft=null;
function scheduleFilter(){clearTimeout(_ft);_ft=setTimeout(loadDocs,300);}
function resetFilter(){
  ['f-q','f-kat','f-adr','f-konfid','f-von','f-bis'].forEach(id=>{const e=document.getElementById(id);if(e)e.value='';});
  loadDocs();
}

// Docs table
async function loadDocs() {
  const p = new URLSearchParams({limit:100});
  const get=id=>document.getElementById(id)?.value||'';
  const q=get('f-q').trim(),kat=get('f-kat'),adr=get('f-adr'),konfid=get('f-konfid'),von=get('f-von'),bis=get('f-bis');
  if(q)p.set('q',q); if(kat)p.set('kategorie',kat);
  if(adr)p.set('adressat',adr); if(konfid)p.set('konfidenz',konfid);
  if(von)p.set('von',von); if(bis)p.set('bis',bis);
  try {
    const docs = await (await fetch('/api/recent?'+p)).json();
    const fr=document.getElementById('filter-results');
    if(fr) fr.textContent = docs.length===100?'100+ Treffer':`${docs.length} Treffer`;
    const rows = docs.map(d=>{
      const k=(d.konfidenz||'').toLowerCase();
      const kc=['hoch','mittel','niedrig'].includes(k)?k:'null';
      const ts=(d.erstellt_am||'').slice(0,16).replace('T',' ');
      const abs=(d.absender||'—').slice(0,30);
      const pdfName=d.pdf_name||d.dateiname||'';
      return `<tr>
        <td>${d.rechnungsdatum||'—'}</td>
        <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${d.dateiname||''}">
          <a href="/api/pdf/${encodeURIComponent(pdfName)}" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:500"
             onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">${pdfName||'—'}</a>
        </td>
        <td><span class="cat-tag">${d.kategorie||'—'}</span></td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${d.absender||''}">${abs}</td>
        <td><span class="adressat-tag">${d.adressat||'—'}</span></td>
        <td><span class="kbadge ${kc}">${k||'—'}</span></td>
        <td style="color:var(--muted)">${ts}</td>
      </tr>`;
    }).join('');
    document.getElementById('docs-body').innerHTML = rows||'<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">Keine Dokumente gefunden</td></tr>';
  } catch(e) {
    document.getElementById('docs-body').innerHTML='<tr><td colspan="7" style="color:var(--err);text-align:center">Fehler</td></tr>';
  }
}

async function loadAll(){await Promise.all([loadData(),loadDocs()]);}

// SSE
function prependDoc(d) {
  const k=(d.konfidenz||'').toLowerCase();
  const kc=['hoch','mittel','niedrig'].includes(k)?k:'null';
  const ts=(d.erstellt_am||'').slice(0,16).replace('T',' ');
  const pdfName=d.pdf_name||d.dateiname||'';
  const row=document.createElement('tr');
  row.style.animation='flashRow 1.5s ease-out';
  row.innerHTML=`
    <td>${d.rechnungsdatum||'—'}</td>
    <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
      <a href="/api/pdf/${encodeURIComponent(pdfName)}" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:500">${pdfName||'—'}</a>
    </td>
    <td><span class="cat-tag">${d.kategorie||'—'}</span></td>
    <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(d.absender||'—').slice(0,30)}</td>
    <td><span class="adressat-tag">${d.adressat||'—'}</span></td>
    <td><span class="kbadge ${kc}">${k||'—'}</span></td>
    <td style="color:var(--muted)">${ts}</td>`;
  const tbody=document.getElementById('docs-body');
  if(tbody.firstChild?.querySelector?.('[colspan]'))tbody.innerHTML='';
  tbody.insertBefore(row,tbody.firstChild);
  while(tbody.children.length>100)tbody.removeChild(tbody.lastChild);
}

function connectSSE() {
  const es=new EventSource('/api/events');
  const dot=document.getElementById('sse-dot');
  es.onopen=()=>{if(dot){dot.style.background='var(--ok)';dot.title='Live';}};
  es.addEventListener('doc_processed',e=>{try{prependDoc(JSON.parse(e.data));}catch(_){}});
  es.addEventListener('enzyme_refresh_done',e=>{
    try{
      const d=JSON.parse(e.data);
      const st=document.getElementById('enzyme-refresh-status');
      const btn=document.getElementById('enzyme-refresh-btn');
      if(st)st.textContent=d.success?'✓ Fertig':'✗ '+d.msg;
      if(btn){btn.disabled=false;btn.textContent='⟳ Aktualisieren';}
      if(d.success)loadData();
    }catch(_){}
  });
  es.addEventListener('rescan_progress',e=>{
    try{
      const rs=JSON.parse(e.data);
      const banner=document.getElementById('rescan-banner');
      if(rs.active){
        banner.style.display='flex';
        const pct=rs.total>0?Math.round(rs.done/rs.total*100):0;
        document.getElementById('rb-bar').style.width=pct+'%';
        document.getElementById('rb-label').textContent=`${rs.done} / ${rs.total} (${pct}%)`;
        document.getElementById('rb-current').textContent=rs.current?'↳ '+rs.current:'';
      } else { banner.style.display='none'; }
    }catch(_){}
  });
  es.onerror=()=>{if(dot){dot.style.background='var(--err)';dot.title='Verbindung unterbrochen';}};
}

let secs=30;
function tick(){secs--;document.getElementById('countdown').textContent=secs;if(secs<=0){secs=30;loadData();}}
loadAll(); loadCategories(); connectSSE(); setInterval(tick,1000);

async function updateQueueBadge(){
  try{
    const r = await fetch('/api/queue/state');
    const d = await r.json();
    const b = document.getElementById('nav-queue-badge');
    if(!b) return;
    if(d.waiting > 0){ b.textContent = d.waiting; b.style.display = 'inline-block'; }
    else { b.style.display = 'none'; }
  }catch(e){}
}
updateQueueBadge();
setInterval(updateQueueBadge, 3000);
</script>
<style>.help-btn{font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--muted);cursor:pointer;font-weight:600;transition:all .15s;white-space:nowrap}.help-btn:hover{border-color:var(--accent);color:var(--accent)}.help-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center}.help-overlay.open{display:flex}.help-box{background:#23263a;border-radius:14px;padding:28px 32px;max-width:520px;width:90%;position:relative;box-shadow:0 20px 60px rgba(0,0,0,.4);color:#e8eaf0}.help-box h2{font-size:15px;font-weight:700;color:#7c6af7;margin-bottom:18px}.help-box h3{font-size:11px;font-weight:700;color:#8a8fb0;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;margin-top:14px}.help-box p{font-size:13px;line-height:1.6;color:#c8cad8}.help-close{position:absolute;top:12px;right:14px;background:none;border:none;font-size:18px;cursor:pointer;color:#8a8fb0;line-height:1;padding:2px}.help-close:hover{color:#e8eaf0}</style>
<div id="help-overlay" class="help-overlay">
  <div class="help-box">
    <button class="help-close" onclick="closeHelp()">✕</button>
    <h2>❓ Haupt-Dashboard</h2>
    <h3>Was macht dieses Dashboard?</h3>
    <p>Zeigt den Live-Status aller Systemdienste (Dispatcher, Ollama, Syncthing, enzyme u.a.) und fasst zusammen wie viele Dokumente heute verarbeitet wurden.</p>
    <h3>Wann ist es nützlich?</h3>
    <p>Täglich zur schnellen Kontrolle — läuft alles reibungslos, wurden Dokumente verarbeitet? Bei Problemen ist hier sofort der defekte Dienst sichtbar.</p>
    <h3>Beispiel</h3>
    <p>Du hast einen Scan gemacht und erwartest eine Bestätigung. Hier siehst du sofort: Dispatcher aktiv, heute 2 Dokumente verarbeitet, letztes Dokument "HUK-Leistungsabrechnung".</p>
  </div>
</div>
<script>
function openHelp(){document.getElementById('help-overlay').classList.add('open')}
function closeHelp(){document.getElementById('help-overlay').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHelp()})
document.getElementById('help-overlay').addEventListener('click',e=>{if(e.target===e.currentTarget)closeHelp()})
</script>
</body>
</html>"""


_PIPELINE_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dispatcher · Pipeline</title>
<style>
  :root {
    --bg:      #f4f5f7;
    --surface: #ffffff;
    --border:  #dde1ea;
    --text:    #1a1d2e;
    --muted:   #6b7280;
    --ok:      #059669;
    --warn:    #d97706;
    --err:     #dc2626;
    --accent:  #4f46e5;
    --running: #7c3aed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', 'Segoe UI', system-ui, sans-serif; font-size: 13px; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

  /* Header */
  header { border-bottom: 1px solid var(--border); padding: 12px 20px; display: flex; align-items: center; gap: 10px; background: var(--surface); flex-shrink: 0; }
  header h1 { font-size: 14px; font-weight: 700; color: var(--accent); }
  .back-link { font-size: 11px; color: var(--muted); text-decoration: none; border: 1px solid var(--border); border-radius: 6px; padding: 3px 9px; }
  .back-link:hover { color: var(--accent); border-color: var(--accent); }
  .sse-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); margin-left: auto; }
  .sse-dot.live { background: var(--ok); }
  .sse-label { font-size: 11px; color: var(--muted); }

  /* Queue-Bar */
  .queue-bar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 8px 20px; display: flex; gap: 8px; align-items: center; overflow-x: auto; flex-shrink: 0; min-height: 44px; }
  .queue-bar-label { font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; white-space: nowrap; margin-right: 4px; }
  .q-chip { font-size: 11px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--border); background: var(--bg); color: var(--text); white-space: nowrap; cursor: pointer; transition: all .15s; }
  .q-chip:hover { border-color: var(--accent); color: var(--accent); }
  .q-chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .q-chip.done   { border-color: var(--ok); color: var(--ok); }
  .q-chip.error  { border-color: var(--err); color: var(--err); }
  .q-empty { font-size: 11px; color: var(--muted); font-style: italic; }

  /* Main layout */
  .main { display: flex; flex: 1; overflow: hidden; }

  /* Left: Steps */
  .steps-panel { width: 280px; flex-shrink: 0; border-right: 1px solid var(--border); background: var(--surface); overflow-y: auto; padding: 16px 0; }
  .steps-title { font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; padding: 0 16px 10px; }
  .step-row { display: flex; align-items: flex-start; gap: 10px; padding: 7px 16px; border-left: 3px solid transparent; transition: background .15s, border-color .15s; }
  .step-row.active { background: #eef2ff; border-left-color: var(--accent); }
  .step-row.done   { opacity: .85; }
  .step-row.error  { background: #fff5f5; border-left-color: var(--err); }
  .step-row.skip   { opacity: .4; }
  .step-icon { font-size: 16px; line-height: 1; margin-top: 1px; flex-shrink: 0; width: 20px; text-align: center; }
  .step-body { flex: 1; min-width: 0; }
  .step-label { font-weight: 600; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .step-meta  { font-size: 10px; color: var(--muted); margin-top: 1px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .pulsing { animation: pulse 1s ease-in-out infinite; }

  /* Right: Extracted content */
  .content-panel { flex: 1; overflow: hidden; display: flex; flex-direction: column; }
  .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--muted); gap: 10px; }
  .empty-state .big { font-size: 48px; }
  .empty-state p { font-size: 13px; }

  /* Cards */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .card-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--border); background: #f8f9fb; }
  .card-head-icon { font-size: 15px; }
  .card-head-title { font-size: 12px; font-weight: 700; }
  .card-head-dur { margin-left: auto; font-size: 10px; color: var(--muted); }
  .card-body { padding: 12px 14px; }

  /* KV rows */
  .kv { display: flex; flex-direction: column; gap: 5px; }
  .kv-row { display: flex; gap: 8px; align-items: baseline; }
  .kv-key { font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; white-space: nowrap; min-width: 110px; }
  .kv-val { font-size: 12px; font-weight: 500; word-break: break-word; }
  .kv-val.mono { font-family: 'Menlo', 'Consolas', monospace; font-size: 11px; }

  /* OCR preview */
  .ocr-preview { font-family: 'Menlo', 'Consolas', monospace; font-size: 10px; color: var(--muted); background: #f8f9fb; border: 1px solid var(--border); border-radius: 6px; padding: 10px; max-height: 160px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; margin-top: 8px; line-height: 1.5; }

  /* Tag badges */
  .tag { display: inline-block; font-size: 10px; padding: 2px 7px; border-radius: 4px; font-weight: 600; margin: 2px 2px 0 0; }
  .tag.ok  { background: #d1fae5; color: var(--ok); }
  .tag.med { background: #fef3c7; color: var(--warn); }
  .tag.err { background: #fee2e2; color: var(--err); }
  .tag.info{ background: #ede9fe; color: var(--running); }
  .tag.grey{ background: #f1f2f6; color: var(--muted); }

  /* Konfidenz indicator */
  .konf-hoch    { color: var(--ok); font-weight: 700; }
  .konf-mittel  { color: var(--warn); font-weight: 700; }
  .konf-niedrig { color: var(--err); font-weight: 700; }

  /* Progress bar */
  .prog-wrap { height: 3px; background: var(--border); border-radius: 2px; margin: 0 20px; flex-shrink: 0; }
  .prog-bar  { height: 100%; background: var(--accent); border-radius: 2px; transition: width .4s ease; }

  /* Idle banner */
  .idle-banner { background: #fff7ed; border-bottom: 2px solid #fed7aa; padding: 12px 20px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; flex-wrap: wrap; }
  .idle-banner-title { font-size: 13px; font-weight: 800; color: #c2410c; white-space: nowrap; }
  .idle-banner-sub { font-size: 11px; color: #92400e; flex: 1; min-width: 180px; }
  .idle-btns { display: flex; gap: 8px; flex-wrap: wrap; }
  .idle-btn { font-size: 11px; font-weight: 700; padding: 5px 14px; border-radius: 6px; border: none; cursor: pointer; white-space: nowrap; transition: opacity .15s; }
  .idle-btn:hover { opacity: .85; }
  .idle-btn.primary { background: var(--accent); color: #fff; }
  .idle-btn.secondary { background: #fff; color: var(--accent); border: 1.5px solid var(--accent); }
  .idle-btn.warn { background: var(--warn); color: #fff; }
  .idle-btn:disabled { opacity: .5; cursor: not-allowed; }
  .input-badge { font-size: 11px; padding: 3px 10px; border-radius: 999px; font-weight: 700; white-space: nowrap; }
  .input-badge.empty { background: #f1f2f6; color: var(--muted); }
  .input-badge.has-files { background: #fef3c7; color: #92400e; }

  /* Filename banner */
  .fn-banner { padding: 8px 20px; background: #eef2ff; border-bottom: 1px solid #c7d2fe; flex-shrink: 0; display: flex; align-items: center; gap: 8px; }
  .fn-name { font-size: 12px; font-weight: 700; color: var(--accent); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fn-empty { font-size: 12px; color: var(--muted); font-style: italic; }

  /* Dokument-Karte — kompaktes Kachel-Grid */
  #doc-card { flex: 1; display: flex; flex-direction: column; padding: 10px; gap: 8px; overflow: hidden; min-height: 0; }
  .dcard-head { display: flex; align-items: center; gap: 8px; padding: 7px 12px; background: #f8f9fb; border: 1px solid var(--border); border-radius: 8px; flex-shrink: 0; }
  .dcard-title { font-weight: 700; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .dcard-badge { font-size: 10px; padding: 2px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap; background: #d1fae5; color: var(--ok); }
  .dcard-grid { flex: 1; display: grid; grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(2, 1fr); gap: 8px; min-height: 0; }
  .dtile { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; overflow: hidden; display: flex; flex-direction: column; }
  .dtile.clickable { cursor: pointer; }
  .dtile.clickable:hover { border-color: var(--accent); background: #f5f3ff; }
  .content-modal { position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:200;display:flex;align-items:center;justify-content:center }
  .content-modal-box { background:var(--surface);border-radius:12px;width:min(860px,95vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3) }
  .content-modal-head { display:flex;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border);gap:10px }
  .content-modal-body { overflow-y:auto;padding:16px 18px;flex:1;font-family:'Menlo','Consolas',monospace;font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.6;color:var(--text) }
  .dtile.fresh { animation: freshPulse .7s ease-out; }
  @keyframes freshPulse { 0%{background:#eef2ff;border-color:#a5b4fc} 100%{background:var(--surface);border-color:var(--border)} }
  .dtile-label { font-size: 9px; font-weight: 800; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 5px; flex-shrink: 0; }
  .drow { display: flex; align-items: baseline; gap: 5px; margin-bottom: 2px; min-width: 0; }
  .dk { font-size: 10px; font-weight: 600; color: var(--muted); white-space: nowrap; flex-shrink: 0; }
  .dk::after { content: ':'; }
  .dv { font-size: 11px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .dv.pending { color: #d1d5db; }
  .dv.mono { font-family: 'Menlo','Consolas',monospace; font-size: 10px; }
  .dv.hoch    { color: var(--ok);   font-weight: 700; }
  .dv.mittel  { color: var(--warn); font-weight: 700; }
  .dv.niedrig { color: var(--err);  font-weight: 700; }
  .dtags { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 4px; }
  .dtag { font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 700; }
  .dtag.hoch   { background: #d1fae5; color: var(--ok); }
  .dtag.mittel { background: #fef3c7; color: var(--warn); }
  .dtag.niedrig{ background: #fee2e2; color: var(--err); }
  .dtag.grey   { background: #f1f2f6; color: var(--muted); }
</style>
</head>
<body>

<header>
  <a href="/" class="back-link">← Dashboard</a>
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
  <h1>Dispatcher · Pipeline</h1>
  <a href="/vault" style="font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;color:var(--muted);text-decoration:none;font-weight:600" title="Vault-Struktur">📁 Vault</a>
  <a href="#stats" onclick="showStats();return false;" style="font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;color:var(--muted);text-decoration:none;font-weight:600;margin-left:auto" title="Schritt-Statistiken">📊 Statistiken</a>
  <button class="help-btn" onclick="openHelp()">❓ Hilfe</button>
  <span class="sse-dot" id="sse-dot"></span>
  <span class="sse-label">Live</span>
</header>

<div id="idle-banner" style="display:none" class="idle-banner">
  <span class="idle-banner-title">⚠ Kein Rescan aktiv</span>
  <span class="idle-banner-sub" id="idle-sub">Die Queue leert sich bei jedem Container-Neustart. Rescan neu starten:</span>
  <span class="input-badge empty" id="input-badge">📥 Input: –</span>
  <div class="idle-btns">
    <button class="idle-btn warn"      onclick="triggerRescan('undated')"   id="btn-idle-undated">Undatierte PDFs</button>
    <button class="idle-btn secondary" onclick="triggerRescan('dated_de')"  id="btn-idle-dated">Datierte DE-PDFs</button>
    <button class="idle-btn primary"   onclick="triggerRescan('all')"       id="btn-idle-all">Alle PDFs rescannen</button>
  </div>
</div>

<div class="queue-bar" id="queue-bar">
  <span class="queue-bar-label">Verlauf</span>
  <span class="q-empty" id="q-empty">Wartet auf erstes Dokument…</span>
</div>

<div class="fn-banner" id="fn-banner">
  <span class="fn-empty" id="fn-display">Kein aktives Dokument</span>
</div>

<div id="rescan-banner" style="display:none;background:#fef9c3;border-bottom:1px solid #fde68a;padding:6px 20px;display:none;align-items:center;gap:10px;font-size:11px;flex-shrink:0">
  <span style="font-weight:700;color:#92400e">⚡ Batch-Rescan</span>
  <span id="rb-count" style="color:#78350f"></span>
  <div style="flex:1;height:6px;background:#fde68a;border-radius:3px;overflow:hidden">
    <div id="rb-bar" style="height:100%;background:#f59e0b;border-radius:3px;width:0%;transition:width .4s"></div>
  </div>
  <span id="rb-pct" style="color:#78350f;font-weight:700;white-space:nowrap"></span>
  <button onclick="stopRescan()" style="padding:2px 12px;background:#dc2626;color:#fff;border:none;border-radius:5px;font-weight:700;font-size:11px;cursor:pointer;margin-left:8px">■ Stop</button>
</div>
<div class="prog-wrap"><div class="prog-bar" id="prog-bar" style="width:0%"></div></div>

<div class="main">
  <div class="steps-panel">
    <div class="steps-title">Prozessschritte</div>
    <div id="steps-list"></div>
  </div>
  <div class="content-panel" id="content-panel">
    <div class="empty-state" id="empty-state">
      <span class="big">🔍</span>
      <p>Warte auf Dokument…</p>
      <p style="font-size:11px">Sobald ein PDF verarbeitet wird, füllt sich die Karte hier.</p>
    </div>
    <div id="doc-card" style="display:none">
      <div class="dcard-head">
        <span>📄</span>
        <span class="dcard-title" id="dc-filename">–</span>
        <span class="dcard-badge" id="dc-badge" style="display:none"></span>
        <button onclick="showContent('logs')" title="Dispatcher-Logs für dieses Dokument anzeigen"
          style="font-size:11px;padding:3px 10px;border:1px solid var(--border);background:#fff;border-radius:6px;cursor:pointer;color:var(--muted);font-weight:600">📜 Logs</button>
      </div>
      <div class="dcard-grid">

        <div class="dtile clickable" id="dcs-ocr" onclick="showContent('ocr')" title="Klicken: Volltext anzeigen">
          <div class="dtile-label">📝 OCR <span style="font-size:8px;opacity:.6">↗</span></div>
          <div class="drow"><span class="dk">Zeichen</span><span class="dv" id="dc-ocr-chars">–</span></div>
          <div class="drow"><span class="dk">Übersetzt</span><span class="dv" id="dc-translate-info" style="font-size:9px;color:var(--muted)"></span></div>
        </div>

        <div class="dtile" id="dcs-header">
          <div class="dtile-label">🏷️ Header</div>
          <div class="drow"><span class="dk">Absender</span><span class="dv" id="dc-abs-name">–</span></div>
          <div class="drow"><span class="dk">PLZ/Ort</span><span class="dv" id="dc-abs-plz">–</span></div>
          <div class="drow"><span class="dk">Empfänger</span><span class="dv" id="dc-emp-name">–</span></div>
        </div>

        <div class="dtile" id="dcs-idents">
          <div class="dtile-label">🪪 Identifier</div>
          <div class="drow"><span class="dk">IBAN</span><span class="dv mono" id="dc-iban">–</span></div>
          <div class="drow"><span class="dk">IVA</span><span class="dv mono" id="dc-piva">–</span></div>
          <div class="drow"><span class="dk">CF</span><span class="dv mono" id="dc-cf">–</span></div>
          <div class="drow"><span class="dk">Adressat</span><span class="dv" id="dc-adr-match">–</span></div>
          <div class="drow"><span class="dk">Absender</span><span class="dv" id="dc-abs-match">–</span></div>
        </div>

        <div class="dtile clickable" id="dcs-doctype" onclick="showContent('translate')" title="Klicken: Übersetzung anzeigen">
          <div class="dtile-label">📋 Typ & Sprache <span style="font-size:8px;opacity:.6">↗</span></div>
          <div class="drow"><span class="dk">Typ</span><span class="dv" id="dc-doctype">–</span></div>
          <div class="drow"><span class="dk">Sprache</span><span class="dv" id="dc-lang">–</span></div>
          <div class="drow"><span class="dk">Übersetzt</span><span class="dv" id="dc-translate">–</span></div>
        </div>

        <div class="dtile" id="dcs-llm">
          <div class="dtile-label">🤖 LLM</div>
          <div class="drow"><span class="dk">Kategorie</span><span class="dv" id="dc-cat">–</span></div>
          <div class="drow"><span class="dk">Typ</span><span class="dv" id="dc-type">–</span></div>
          <div class="drow"><span class="dk">Absender</span><span class="dv" id="dc-absender">–</span></div>
          <div class="drow"><span class="dk">Adressat</span><span class="dv" id="dc-adressat">–</span></div>
          <div class="drow"><span class="dk">Datum</span><span class="dv" id="dc-datum">–</span></div>
          <div class="drow"><span class="dk">Betrag</span><span class="dv" id="dc-betrag">–</span></div>
          <div class="dtags" id="dc-konf-tags"></div>
        </div>

        <div class="dtile" id="dcs-result">
          <div class="dtile-label">✅ Ergebnis</div>
          <div class="drow"><span class="dk">Konfidenz</span><span class="dv" id="dc-konfidenz">–</span></div>
          <div class="drow"><span class="dk">Dok-ID</span><span class="dv" id="dc-dokid">–</span></div>
          <div class="drow"><span class="dk">Vault</span><span class="dv mono" id="dc-vault">–</span></div>
        </div>

      </div>
    </div>
  </div>
</div>

<script>
const STEP_DEF = [
  { id: 'started',    label: 'Verarbeitung gestartet',           icon: '📄' },
  { id: 'ocr',        label: 'OCR / Docling',                    icon: '🔍' },
  { id: 'ocr_quality',label: 'OCR-Qualitäts-Gate',               icon: '📏' },
  { id: 'header',     label: 'Header-Extraktion',                 icon: '🏷️' },
  { id: 'identifiers',label: 'Identifier & Personen-Auflösung',  icon: '🪪' },
  { id: 'doctype',    label: 'Dokumenttyp-Erkennung',             icon: '📋' },
  { id: 'lang',       label: 'Spracherkennung',                   icon: '🌐' },
  { id: 'translate',  label: 'Übersetzung',                       icon: '🔤' },
  { id: 'llm',        label: 'LLM-Klassifikation (Ollama)',       icon: '🤖' },
  { id: 'overrides',  label: 'Deterministisches Override',        icon: '⚖️' },
  { id: 'db',         label: 'Datenbank speichern',               icon: '💾' },
  { id: 'vault',      label: 'Vault-Move',                        icon: '📁' },
];

const STEP_IDS = STEP_DEF.map(s => s.id);
const STEP_BY_ID = Object.fromEntries(STEP_DEF.map(s => [s.id, s]));

// State
let history = [];      // [{filename, steps, startTs}]
let activeDoc = null;  // same shape
let selectedFilename = null;

function newDoc(filename, ts) {
  return { filename, steps: {}, startTs: ts };
}

function onDocStep(data) {
  const { filename, step_id, label, status, ts, duration_ms, extracted, error } = data;

  // New document?
  if (!activeDoc || activeDoc.filename !== filename) {
    if (activeDoc) {
      history.unshift(activeDoc);
      if (history.length > 12) history.pop();
    }
    activeDoc = newDoc(filename, ts);
    selectedFilename = filename;
    updateQueueBar();
    document.getElementById('fn-display').textContent = filename;
    document.getElementById('fn-display').className = 'fn-name';
    resetDocCard();
    set('dc-filename', filename);
    document.getElementById('doc-card').style.display = '';
    document.getElementById('empty-state').style.display = 'none';
  }

  activeDoc.steps[step_id] = { label, status, ts, duration_ms, extracted, error };

  if (selectedFilename === filename) {
    renderSteps(activeDoc);
    updateDocCard(step_id, activeDoc.steps[step_id]);
    updateProgress(activeDoc);
  }
  updateQueueBar();
}

let _waitingQueue = [];

async function refreshWaitingQueue() {
  try {
    const r = await fetch('/api/queue/state');
    const d = await r.json();
    _waitingQueue = d.items || [];
  } catch (e) { _waitingQueue = []; }
  updateQueueBar();
}

function updateQueueBar() {
  const bar = document.getElementById('queue-bar');
  const empty = document.getElementById('q-empty');
  const all = activeDoc ? [activeDoc, ...history] : history;
  const waiting = _waitingQueue.filter(w => !activeDoc || w.name !== activeDoc.filename);

  if (!all.length && !waiting.length) { empty.style.display = ''; return; }
  empty.style.display = 'none';

  const label = bar.querySelector('.queue-bar-label');
  bar.innerHTML = '';
  bar.appendChild(label);

  all.forEach(doc => {
    const chip = document.createElement('span');
    chip.className = 'q-chip';
    if (doc === activeDoc) chip.classList.add('active');
    else if (docHasError(doc)) chip.classList.add('error');
    else if (isDocDone(doc)) chip.classList.add('done');

    chip.textContent = doc.filename.length > 30 ? doc.filename.slice(0, 28) + '…' : doc.filename;
    chip.title = doc.filename;
    chip.onclick = () => selectDoc(doc);
    bar.appendChild(chip);
  });

  if (waiting.length) {
    const sep = document.createElement('span');
    sep.className = 'queue-bar-label';
    sep.style.marginLeft = '16px';
    sep.style.color = 'var(--warn)';
    sep.textContent = `⏳ Wartend (${waiting.length})`;
    bar.appendChild(sep);
    waiting.slice(0, 30).forEach(w => {
      const chip = document.createElement('span');
      chip.className = 'q-chip';
      chip.style.borderColor = 'var(--warn)';
      chip.style.color = 'var(--warn)';
      chip.style.background = '#fff7ed';
      chip.style.cursor = 'default';
      chip.textContent = w.name.length > 30 ? w.name.slice(0, 28) + '…' : w.name;
      chip.title = w.name + ' (wartet auf Verarbeitung)';
      bar.appendChild(chip);
    });
  }
}

function selectDoc(doc) {
  selectedFilename = doc.filename;
  document.getElementById('fn-display').textContent = doc.filename;
  document.getElementById('fn-display').className = 'fn-name';
  renderSteps(doc);
  rebuildDocCard(doc);
  updateProgress(doc);
  updateQueueBar();
}

function docHasError(doc) {
  return Object.values(doc.steps).some(s => s.status === 'error');
}

function isDocDone(doc) {
  return doc.steps['vault']?.status === 'done';
}

function updateProgress(doc) {
  const done = STEP_IDS.filter(id => doc.steps[id]?.status === 'done' || doc.steps[id]?.status === 'skip').length;
  const pct = Math.round((done / STEP_IDS.length) * 100);
  document.getElementById('prog-bar').style.width = pct + '%';
}

function stepIcon(status) {
  if (status === 'done')    return '✅';
  if (status === 'running') return '⚡';
  if (status === 'error')   return '❌';
  if (status === 'skip')    return '⏭️';
  return '○';
}

function renderSteps(doc) {
  const list = document.getElementById('steps-list');
  list.innerHTML = STEP_DEF.map(def => {
    const step = doc.steps[def.id];
    const status = step?.status || 'pending';
    const dur = step?.duration_ms != null ? `${(step.duration_ms/1000).toFixed(1)}s` : '';
    const errMsg = step?.error ? `<div style="font-size:10px;color:var(--err);margin-top:2px">${step.error}</div>` : '';
    const pulse = status === 'running' ? ' pulsing' : '';
    return `
      <div class="step-row ${status}" data-id="${def.id}">
        <div class="step-icon${pulse}">${stepIcon(status)}</div>
        <div class="step-body">
          <div class="step-label">${def.label}</div>
          <div class="step-meta">${dur}${step?.ts ? ' · ' + step.ts.slice(11) : ''}</div>
          ${errMsg}
        </div>
      </div>`;
  }).join('');
}


const LANG_NAMES = {de:'Deutsch',it:'Italiano',en:'English',fr:'Français',es:'Español'};

function set(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = (val != null && val !== '') ? String(val) : null;
  el.textContent = v ?? '–';
  if (v) el.classList.remove('pending'); else el.classList.add('pending');
}

function flash(sectionId) {
  const el = document.getElementById(sectionId);
  if (!el) return;
  el.classList.remove('fresh');
  void el.offsetWidth;
  el.classList.add('fresh');
}

let _currentTranslatePreview = '';

function showContent(mode) {
  const fn = activeDoc?.filename || selectedFilename;
  if (!fn) return;
  let modal = document.getElementById('content-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'content-modal';
    modal.className = 'content-modal';
    modal.innerHTML = `
      <div class="content-modal-box">
        <div class="content-modal-head">
          <span id="cm-title" style="font-weight:700;font-size:13px;flex:1"></span>
          <button onclick="document.getElementById('content-modal').remove()"
            style="background:none;border:none;font-size:20px;cursor:pointer;color:var(--muted);line-height:1">×</button>
        </div>
        <div id="cm-body" class="content-modal-body">Lade…</div>
      </div>`;
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
    document.body.appendChild(modal);
  }

  const title = document.getElementById('cm-title');
  const body  = document.getElementById('cm-body');

  if (mode === 'translate' && _currentTranslatePreview) {
    title.textContent = '📋 Übersetzter Text (Auszug)';
    body.textContent = _currentTranslatePreview;
    return;
  }

  if (mode === 'logs') {
    title.textContent = '📜 Dispatcher-Logs: ' + fn;
    const stem = fn.replace(/\.(pdf|enex)$/i, '');
    const render = (entries) => {
      body.innerHTML = entries.length === 0
        ? '<span style="color:var(--muted)">Keine Log-Einträge für dieses Dokument im Ringbuffer.</span>'
        : entries.map(e => {
            const ts = new Date(e.t * 1000).toLocaleTimeString('de-DE');
            const color = e.level === 'ERROR' ? 'var(--err)' : e.level === 'WARNING' ? 'var(--warn)' : 'var(--text)';
            const safe = e.msg.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
            return `<div style="color:${color}"><span style="color:var(--muted)">${ts}</span> <span style="font-weight:700;font-size:9px">${e.level}</span> ${safe}</div>`;
          }).join('');
    };
    const load = async () => {
      try {
        const r = await fetch('/api/logs?limit=400&q=' + encodeURIComponent(stem));
        const d = await r.json();
        if (document.getElementById('content-modal')) render(d.entries || []);
      } catch (e) {
        if (document.getElementById('content-modal')) body.innerHTML = 'Fehler: ' + e.message;
      }
    };
    body.textContent = 'Lade…';
    load();
    const iv = setInterval(() => {
      if (!document.getElementById('content-modal')) { clearInterval(iv); return; }
      load();
    }, 2000);
    return;
  }

  title.textContent = mode === 'ocr' ? '📝 OCR-Volltext aus Vault' : '📋 Übersetzter Text';
  body.textContent = 'Lade…';
  fetch('/api/pipeline/content?dateiname=' + encodeURIComponent(fn))
    .then(r => r.ok ? r.text() : Promise.reject(r.status))
    .then(txt => { body.textContent = txt; })
    .catch(err => { body.textContent = err === 404 ? 'Noch nicht im Vault – Dokument wird verarbeitet.' : 'Fehler: ' + err; });
}

function resetDocCard() {
  _currentTranslatePreview = '';
  ['dc-ocr-chars','dc-abs-name','dc-abs-plz','dc-emp-name',
   'dc-iban','dc-piva','dc-cf','dc-adr-match','dc-abs-match',
   'dc-doctype','dc-lang','dc-translate',
   'dc-cat','dc-type','dc-absender','dc-adressat','dc-datum','dc-betrag',
   'dc-konfidenz','dc-dokid','dc-vault'].forEach(id => set(id, null));
  const konf = document.getElementById('dc-konf-tags');
  if (konf) konf.innerHTML = '';
  const badge = document.getElementById('dc-badge');
  if (badge) { badge.textContent = ''; badge.style.display = 'none'; }
}

function updateDocCard(stepId, step) {
  const ex = step?.extracted;
  switch (stepId) {
    case 'ocr':
      if (ex) {
        set('dc-ocr-chars', ex.chars?.toLocaleString('de'));
        const prev = document.getElementById('dc-ocr-preview');
        if (prev && ex.preview) prev.textContent = ex.preview;
        flash('dcs-ocr');
      }
      break;

    case 'ocr_quality':
      if (step.error) flash('dcs-ocr');
      break;

    case 'header':
      if (ex) {
        const abs = ex.absender || {};
        const emp = ex.empfaenger || {};
        set('dc-abs-name', abs.name);
        set('dc-abs-plz', [abs.plz, abs.ort].filter(Boolean).join(' ') || null);
        set('dc-emp-name', emp.name);
        flash('dcs-header');
      }
      break;

    case 'identifiers':
      if (ex) {
        const ids = ex.identifiers || {};
        set('dc-iban', (ids.iban || []).join(', ') || null);
        set('dc-piva', (ids.part_iva_firma || []).join(', ') || null);
        set('dc-cf',   (ids.cod_fiscale_person || []).join(', ') || null);
        const adr = ex.adressat;
        const absm = ex.absender_match;
        set('dc-adr-match', adr ? [adr.person_key, adr.via ? 'via '+adr.via : ''].filter(Boolean).join(' · ') : null);
        set('dc-abs-match', absm ? [absm.id, absm.name].filter(Boolean).join(' · ') : null);
        flash('dcs-idents');
      }
      break;

    case 'doctype':
      if (ex) {
        set('dc-doctype', ex.typ || null);
        flash('dcs-doctype');
      }
      break;

    case 'lang':
      if (ex) {
        const lname = LANG_NAMES[ex.lang] || ex.lang?.toUpperCase();
        set('dc-lang', lname ? lname + (ex.prob != null ? ' (' + Math.round(ex.prob*100) + '%)' : '') : null);
        flash('dcs-doctype');
      }
      break;

    case 'translate':
      if (step.status === 'skip') {
        set('dc-translate', 'Deutsch – kein Übersetzen');
        const ti1 = document.getElementById('dc-translate-info');
        if (ti1) ti1.textContent = '';
      } else if (ex) {
        set('dc-translate', ex.chars ? ex.chars.toLocaleString('de') + ' Zeichen →DE' : 'Übersetzt');
        if (ex.preview) _currentTranslatePreview = ex.preview;
        const ti2 = document.getElementById('dc-translate-info');
        if (ti2 && ex.ocr_chars && ex.input_limit && ex.ocr_chars > ex.input_limit) {
          ti2.textContent = `Auszug: ${ex.input_limit.toLocaleString('de')} / ${ex.ocr_chars.toLocaleString('de')} Zeichen`;
        }
      }
      flash('dcs-doctype');
      break;

    case 'llm':
      if (ex) {
        set('dc-cat',      (ex.category_id || '') + (ex.category_label ? ' – '+ex.category_label : '') || null);
        set('dc-type',     (ex.type_id     || '') + (ex.type_label     ? ' – '+ex.type_label     : '') || null);
        set('dc-absender', ex.absender);
        set('dc-adressat', ex.adressat);
        set('dc-datum',    ex.rechnungsdatum);
        set('dc-betrag',   ex.rechnungsbetrag);
        const tags = document.getElementById('dc-konf-tags');
        if (tags) {
          const kc = ex.konfidenz === 'hoch' ? 'hoch' : ex.konfidenz === 'mittel' ? 'mittel' : 'niedrig';
          tags.innerHTML = [
            ex.konfidenz          ? `<span class="dtag ${kc}">Gesamt: ${ex.konfidenz}</span>` : '',
            ex.konfidenz_category ? `<span class="dtag grey">Kat: ${ex.konfidenz_category}</span>` : '',
            ex.konfidenz_absender ? `<span class="dtag grey">Abs: ${ex.konfidenz_absender}</span>` : '',
            ex.konfidenz_adressat ? `<span class="dtag grey">Adr: ${ex.konfidenz_adressat}</span>` : '',
            ex.konfidenz_datum    ? `<span class="dtag grey">Dat: ${ex.konfidenz_datum}</span>`    : '',
          ].join('');
        }
        flash('dcs-llm');
      }
      break;

    case 'overrides':
      if (ex) {
        if (ex.category_id) set('dc-cat',      ex.category_id + (ex.category_label ? ' – '+ex.category_label : ''));
        if (ex.absender)    set('dc-absender',  ex.absender);
        if (ex.adressat)    set('dc-adressat',  ex.adressat);
        flash('dcs-llm');
      }
      break;

    case 'db':
      if (ex) {
        set('dc-konfidenz', ex.konfidenz);
        set('dc-dokid', ex.dok_id != null ? '#' + ex.dok_id : null);
        flash('dcs-result');
      }
      break;

    case 'vault':
      if (ex) {
        set('dc-vault', ex.vault_pfad || '(00 Inbox)');
        const badge = document.getElementById('dc-badge');
        if (badge) { badge.textContent = '✓ Abgeschlossen'; badge.style.display = ''; }
        flash('dcs-result');
      }
      break;
  }
}

function rebuildDocCard(doc) {
  resetDocCard();
  set('dc-filename', doc.filename);
  const hasSteps = Object.keys(doc.steps).length > 0;
  document.getElementById('doc-card').style.display   = hasSteps ? '' : 'none';
  document.getElementById('empty-state').style.display = hasSteps ? 'none' : '';
  STEP_IDS.forEach(id => { if (doc.steps[id]) updateDocCard(id, doc.steps[id]); });
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Stats-Modal ──
function showStats() {
  let modal = document.getElementById('stats-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'stats-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;display:flex;align-items:center;justify-content:center';
    modal.innerHTML = `
      <div style="background:var(--surface);border-radius:14px;width:min(820px,95vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3)">
        <div style="display:flex;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border)">
          <span style="font-weight:700;font-size:14px">📊 Pipeline-Statistiken</span>
          <a href="/api/pipeline/stats" target="_blank" style="margin-left:10px;font-size:11px;color:var(--accent);text-decoration:none;border:1px solid var(--accent);padding:2px 8px;border-radius:5px">JSON ↗</a>
          <button onclick="document.getElementById('stats-modal').remove()" style="margin-left:auto;background:none;border:none;font-size:18px;cursor:pointer;color:var(--muted)">×</button>
        </div>
        <div id="stats-body" style="overflow-y:auto;padding:20px;flex:1">Lade…</div>
      </div>`;
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
    document.body.appendChild(modal);
  }
  loadStats();
}

async function loadStats() {
  const body = document.getElementById('stats-body');
  if (!body) return;
  try {
    const res = await fetch('/api/pipeline/stats');
    const d = await res.json();
    const agg   = d.aggregates || [];
    const docs  = d.documents  || [];
    const cnt   = d.counts     || {};
    const doneDocs2 = d.done_docs || [];
    const errDocs2  = d.err_docs  || [];
    const openDocs2 = d.open_docs || [];

    // Aggregat-Tabelle
    const aggRows = agg.map(s => {
      const bar = s.avg_ms ? Math.round((s.avg_ms / Math.max(...agg.map(x=>x.avg_ms||0))) * 120) : 0;
      const errCls = s.errors > 0 ? 'color:var(--err);font-weight:700' : 'color:var(--muted)';
      return `<tr>
        <td style="font-weight:600;white-space:nowrap">${s.label || s.step_id}</td>
        <td style="text-align:right">${s.runs}</td>
        <td style="text-align:right">${s.avg_ms != null ? s.avg_ms + ' ms' : '–'}</td>
        <td style="text-align:right">${s.min_ms != null ? s.min_ms + ' ms' : '–'}</td>
        <td style="text-align:right">${s.max_ms != null ? s.max_ms + ' ms' : '–'}</td>
        <td style="${errCls};text-align:right">${s.errors || 0}</td>
        <td style="padding-left:8px"><div style="height:8px;width:${bar}px;background:var(--accent);border-radius:4px;opacity:.7"></div></td>
      </tr>`;
    }).join('');

    // Gesamtzahlen aus counts-Objekt (alle Dateien, nicht nur die angezeigten)
    const doneDocs = doneDocs2;
    const errDocs  = errDocs2;
    const openDocs = openDocs2;

    function docRow(doc, icon) {
      const total = doc.steps.filter(s=>s.duration_ms).reduce((a,s)=>a+(s.duration_ms||0),0);
      const lastTs = doc.steps.filter(s=>s.ts).slice(-1)[0]?.ts?.slice(0,16).replace('T',' ') || '–';
      const stepBoxes = doc.steps.map(s=>`<span title="${s.step_id}: ${s.status} ${s.duration_ms!=null?s.duration_ms+'ms':''}" style="display:inline-block;width:10px;height:10px;border-radius:2px;margin:1px;background:${s.status==='done'?'var(--ok)':s.status==='error'?'var(--err)':s.status==='skip'?'#e5e7eb':'#d1d5db'}"></span>`).join('');
      return `<tr>
        <td style="font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${doc.dateiname}">${doc.dateiname}</td>
        <td style="text-align:center">${icon}</td>
        <td style="text-align:right;color:var(--muted);font-size:11px">${total ? (total/1000).toFixed(1)+' s' : '–'}</td>
        <td style="font-size:10px">${stepBoxes}</td>
        <td style="font-size:10px;color:var(--muted);white-space:nowrap">${lastTs}</td>
      </tr>`;
    }

    const doneRows = doneDocs.slice(0,15).map(d=>docRow(d,'✅')).join('');
    const errRows  = errDocs.slice(0,20).map(d=>docRow(d,'❌')).join('');
    const openRows = openDocs.slice(0,5).map(d=>docRow(d,'⏳')).join('');
    const docRows  = ''; // unused but keeps reference valid

    body.innerHTML = `
      <p style="font-size:11px;color:var(--muted);margin-bottom:12px">${agg.length ? `Ø-Dauern über alle ${Math.max(...agg.map(a=>a.runs))} Dokumente` : 'Noch keine Daten'}</p>
      ${agg.length ? `
      <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:24px">
        <thead><tr style="border-bottom:2px solid var(--border)">
          <th style="text-align:left;padding:6px 8px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em">Schritt</th>
          <th style="text-align:right;padding:6px 8px;font-size:10px;color:var(--muted)">Runs</th>
          <th style="text-align:right;padding:6px 8px;font-size:10px;color:var(--muted)">Ø Dauer</th>
          <th style="text-align:right;padding:6px 8px;font-size:10px;color:var(--muted)">Min</th>
          <th style="text-align:right;padding:6px 8px;font-size:10px;color:var(--muted)">Max</th>
          <th style="text-align:right;padding:6px 8px;font-size:10px;color:var(--muted)">Fehler</th>
          <th style="padding:6px 8px"></th>
        </tr></thead>
        <tbody>${aggRows}</tbody>
      </table>` : ''}
      ${docs.length ? `
      <p style="font-size:11px;font-weight:700;color:var(--ok);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px;margin-top:8px">✅ Vollständig verarbeitet (${cnt.done ?? doneDocs.length})</p>
      ${doneRows ? `<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
        <thead><tr style="border-bottom:2px solid var(--border)">
          <th style="text-align:left;padding:5px 8px;font-size:10px;color:var(--muted)">Dateiname</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)"></th>
          <th style="text-align:right;padding:5px 8px;font-size:10px;color:var(--muted)">Dauer</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)">Schritte</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)">Zeitstempel</th>
        </tr></thead>
        <tbody>${doneRows}</tbody>
      </table>` : '<p style="font-size:12px;color:var(--muted);margin-bottom:16px">Keine vollständig verarbeiteten Dokumente.</p>'}
      ${errRows ? `
      <p style="font-size:11px;font-weight:700;color:var(--err);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">❌ Fehlgeschlagen (${cnt.error ?? errDocs.length} gesamt — Ursache: meist OCR)</p>
      <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
        <thead><tr style="border-bottom:2px solid var(--border)">
          <th style="text-align:left;padding:5px 8px;font-size:10px;color:var(--muted)">Dateiname</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)"></th>
          <th style="text-align:right;padding:5px 8px;font-size:10px;color:var(--muted)">Dauer</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)">Schritte</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)">Zeitstempel</th>
        </tr></thead>
        <tbody>${errRows}</tbody>
      </table>` : ''}
      ${openRows ? `
      <p style="font-size:11px;font-weight:700;color:var(--warn);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">⏳ Unvollständig / noch offen (${cnt.open ?? openDocs.length})</p>
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="border-bottom:2px solid var(--border)">
          <th style="text-align:left;padding:5px 8px;font-size:10px;color:var(--muted)">Dateiname</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)"></th>
          <th style="text-align:right;padding:5px 8px;font-size:10px;color:var(--muted)">Dauer</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)">Schritte</th>
          <th style="padding:5px 8px;font-size:10px;color:var(--muted)">Zeitstempel</th>
        </tr></thead>
        <tbody>${openRows}</tbody>
      </table>` : ''}` : ''}`;
  } catch(e) {
    if (body) body.innerHTML = `<span style="color:var(--err)">Fehler: ${e.message}</span>`;
  }
}

// SSE
function connectSSE() {
  const es = new EventSource('/api/events');
  const dot = document.getElementById('sse-dot');
  es.onopen = () => { dot.className = 'sse-dot live'; };
  es.onerror = () => { dot.className = 'sse-dot'; };
  es.addEventListener('doc_step', e => {
    try { onDocStep(JSON.parse(e.data)); } catch(_) {}
  });
  es.addEventListener('rescan_progress', e => {
    try { onRescanProgress(JSON.parse(e.data)); } catch(_) {}
  });
}

function onRescanProgress(d) {
  const banner = document.getElementById('rescan-banner');
  const bar    = document.getElementById('rb-bar');
  const count  = document.getElementById('rb-count');
  const pct    = document.getElementById('rb-pct');
  if (!d.active && d.done >= d.total && d.total > 0) {
    banner.style.display = 'none'; return;
  }
  // Hide idle banner when rescan is running
  if (d.active) {
    const idleBanner = document.getElementById('idle-banner');
    if (idleBanner) idleBanner.style.display = 'none';
  }
  banner.style.display = 'flex';
  const p = d.total > 0 ? Math.round((d.done / d.total) * 100) : 0;
  bar.style.width = p + '%';
  count.textContent = `${d.done} / ${d.total}${d.errors ? ' · ' + d.errors + ' Fehler' : ''}`;
  pct.textContent = p + '%';
}

function updateInputBadge(count) {
  const badge = document.getElementById('input-badge');
  if (!badge) return;
  if (count == null) { badge.textContent = '📥 Input: –'; badge.className = 'input-badge empty'; return; }
  if (count === 0)   { badge.textContent = '📥 Input: leer'; badge.className = 'input-badge empty'; }
  else               { badge.textContent = `📥 ${count} PDF${count !== 1 ? 's' : ''} warten`; badge.className = 'input-badge has-files'; }
}

function updateIdleBanner(isIdle, inputCount) {
  const banner = document.getElementById('idle-banner');
  if (!banner) return;
  if (isIdle) {
    banner.style.display = 'flex';
    updateInputBadge(inputCount);
  } else {
    banner.style.display = 'none';
  }
}

// Beim Laden: aktuellen Stand aus DB holen (Catch-up nach Seitenöffnung)
async function loadCurrent() {
  try {
    const r = await fetch('/api/pipeline/current');
    const d = await r.json();
    if (d.rescan) onRescanProgress(d.rescan);
    updateIdleBanner(d.is_idle, d.input_count);
    if (!d.dateiname || !d.steps?.length || d.is_stale || d.is_idle) {
      // Stale, idle, or no data: show empty state
      document.getElementById('doc-card').style.display = 'none';
      document.getElementById('empty-state').style.display = '';
      const fnDisplay = document.getElementById('fn-display');
      fnDisplay.textContent = d.is_idle ? 'Dispatcher wartet — kein Rescan aktiv' : 'Kein aktives Dokument';
      fnDisplay.className = 'fn-empty';
      return;
    }
    const doc = newDoc(d.dateiname, d.steps[0]?.ts || '');
    d.steps.forEach(s => {
      doc.steps[s.step_id] = { label: s.label, status: s.status, duration_ms: s.duration_ms, ts: s.ts };
    });
    activeDoc = doc;
    selectedFilename = d.dateiname;
    document.getElementById('fn-display').textContent = d.dateiname;
    document.getElementById('fn-display').className = 'fn-name';
    rebuildDocCard(doc);
    renderSteps(doc);
    updateProgress(doc);
    updateQueueBar();
  } catch(e) { console.warn('loadCurrent:', e); }
}

function stopRescan() {
  fetch('/api/rescan/stop', {method:'POST'})
    .then(r => r.json())
    .then(d => { document.getElementById('rescan-banner').style.display = 'none'; })
    .catch(e => console.warn('stopRescan:', e));
}

function triggerRescan(mode) {
  const url = mode === 'undated'  ? '/api/rescan/start-undated'
            : mode === 'dated_de' ? '/api/rescan/start-dated-de'
            : '/api/rescan/start';
  const btnMap = { undated: 'btn-idle-undated', dated_de: 'btn-idle-dated', all: 'btn-idle-all' };
  const btn = document.getElementById(btnMap[mode]);
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  fetch(url, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      document.getElementById('idle-banner').style.display = 'none';
      if (d.total != null) onRescanProgress({active: true, total: d.total, done: 0, errors: 0});
    })
    .catch(e => {
      console.warn('triggerRescan:', e);
      if (btn) { btn.disabled = false; btn.textContent = mode === 'undated' ? 'Undatierte PDFs' : mode === 'dated_de' ? 'Datierte DE-PDFs' : 'Alle PDFs rescannen'; }
    });
}

loadCurrent();
connectSSE();
refreshWaitingQueue();
setInterval(refreshWaitingQueue, 3000);
</script>
<style>.help-btn{font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--muted);cursor:pointer;font-weight:600;transition:all .15s;white-space:nowrap}.help-btn:hover{border-color:var(--accent);color:var(--accent)}.help-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center}.help-overlay.open{display:flex}.help-box{background:#23263a;border-radius:14px;padding:28px 32px;max-width:520px;width:90%;position:relative;box-shadow:0 20px 60px rgba(0,0,0,.4);color:#e8eaf0}.help-box h2{font-size:15px;font-weight:700;color:#7c6af7;margin-bottom:18px}.help-box h3{font-size:11px;font-weight:700;color:#8a8fb0;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;margin-top:14px}.help-box p{font-size:13px;line-height:1.6;color:#c8cad8}.help-close{position:absolute;top:12px;right:14px;background:none;border:none;font-size:18px;cursor:pointer;color:#8a8fb0;line-height:1;padding:2px}.help-close:hover{color:#e8eaf0}</style>
<div id="help-overlay" class="help-overlay">
  <div class="help-box">
    <button class="help-close" onclick="closeHelp()">✕</button>
    <h2>❓ Pipeline-Monitor</h2>
    <h3>Was macht dieses Dashboard?</h3>
    <p>Zeigt die Verarbeitungsschritte eines laufenden Dokuments in Echtzeit — von OCR über Spracherkennung und LLM-Klassifikation bis zur Ablage im Vault.</p>
    <h3>Wann ist es nützlich?</h3>
    <p>Wenn ein Dokument gerade verarbeitet wird und du den Fortschritt verfolgen oder einen Fehler diagnostizieren möchtest.</p>
    <h3>Beispiel</h3>
    <p>Du hast ein PDF in den Eingangsordner gelegt. Hier siehst du live: "OCR abgeschlossen (2.340 Zeichen) → LLM klassifiziert → abgelegt in 49 Krankenversicherung/2026".</p>
  </div>
</div>
<script>
function openHelp(){document.getElementById('help-overlay').classList.add('open')}
function closeHelp(){document.getElementById('help-overlay').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHelp()})
document.getElementById('help-overlay').addEventListener('click',e=>{if(e.target===e.currentTarget)closeHelp()})
</script>
</body>
</html>"""


_VAULT_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vault - Struktur</title>
<style>
  :root{--bg:#f4f5f7;--surface:#fff;--border:#dde1ea;--text:#1a1d2e;--muted:#6b7280;--ok:#059669;--warn:#d97706;--err:#dc2626;--accent:#4f46e5}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;font-size:13px;min-height:100vh}
  header{border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:10px;background:var(--surface)}
  header h1{font-size:14px;font-weight:700;color:var(--accent)}
  .back-link{font-size:11px;color:var(--muted);text-decoration:none;border:1px solid var(--border);border-radius:6px;padding:3px 9px}
  .back-link:hover{color:var(--accent);border-color:var(--accent)}
  .summary{display:flex;gap:14px;padding:16px 20px;flex-wrap:wrap}
  .sbox{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 18px;min-width:130px}
  .sval{font-size:24px;font-weight:800;color:var(--accent)}
  .slbl{font-size:11px;color:var(--muted);margin-top:2px}
  .twrap{padding:0 20px 30px;overflow-x:auto}
  table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  thead tr{background:#f8f9fb;border-bottom:2px solid var(--border)}
  th{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);padding:10px 12px;text-align:left;white-space:nowrap}
  th.r,td.r{text-align:right}
  th.group{border-left:2px solid var(--border);color:var(--accent)}
  tr.top{border-bottom:1px solid var(--border)}
  tr.top:hover td{background:#f5f3ff;cursor:pointer}
  tr.top td.fw{font-weight:600}
  tr.sub{border-bottom:1px solid #f1f2f6}
  tr.sub td{font-size:11px;color:var(--muted);background:#fafbfc}
  tr.sub td.indent{padding-left:36px}
  tr.sum-row td{font-weight:700;background:#f0f4ff;border-top:2px solid var(--accent);font-size:12px}
  td{padding:7px 12px;font-variant-numeric:tabular-nums}
  td.grp{border-left:2px solid var(--border)}
  .zero{color:#d1d5db}
  .tog{font-size:9px;margin-left:6px;color:var(--accent);border:1px solid var(--accent);border-radius:3px;padding:0 4px;cursor:pointer;user-select:none;vertical-align:middle}
  .ts{margin-left:auto;font-size:11px;color:var(--muted)}
  #err{color:var(--err);padding:20px;display:none}
</style>
</head>
<body>
<header>
  <a href="/" class="back-link">← Dashboard</a>
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
  <h1>Vault - Struktur</h1>
  <button class="help-btn" onclick="openHelp()">❓ Hilfe</button>
  <span class="ts" id="ts"></span>
</header>

<div class="summary">
  <div class="sbox"><div class="sval" id="s-folders">…</div><div class="slbl">Hauptordner</div></div>
  <div class="sbox"><div class="sval" id="s-md">…</div><div class="slbl">Notizen (MD)</div></div>
  <div class="sbox"><div class="sval" id="s-pdf">…</div><div class="slbl">PDFs</div></div>
  <div class="sbox"><div class="sval" id="s-bild">…</div><div class="slbl">Bilder</div></div>
  <div class="sbox"><div class="sval" id="s-office">…</div><div class="slbl">Office</div></div>
  <div class="sbox"><div class="sval" id="s-total">…</div><div class="slbl">Gesamt</div></div>
</div>

<div id="err"></div>
<div class="twrap">
<table>
  <thead>
    <tr>
      <th>Ordner</th>
      <th class="r group">MD</th>
      <th class="r group">PDF</th>
      <th class="r group">Bilder</th>
      <th class="r group">Office</th>
      <th class="r group">Gesamt</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
</div>

<script>
function cel(tag, cls, txt) {
  var el = document.createElement(tag);
  if (cls) el.className = cls;
  if (txt !== undefined) el.textContent = (txt === 0 ? '–' : txt);
  if (txt === 0) el.classList.add('zero');
  return el;
}
function numCell(n, extra) {
  return cel('td', 'r' + (extra ? ' ' + extra : ''), n);
}

fetch('/api/vault/stats')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var folders = d.folders || [];
    var totMd=0, totPdf=0, totBild=0, totOffice=0, totAll=0;
    for (var i=0; i<folders.length; i++) {
      totMd     += folders[i].md;
      totPdf    += folders[i].pdf;
      totBild   += folders[i].bild;
      totOffice += folders[i].office;
      totAll    += folders[i].total;
    }
    document.getElementById('s-folders').textContent = folders.length;
    document.getElementById('s-md').textContent      = totMd.toLocaleString('de');
    document.getElementById('s-pdf').textContent     = totPdf.toLocaleString('de');
    document.getElementById('s-bild').textContent    = totBild.toLocaleString('de');
    document.getElementById('s-office').textContent  = totOffice.toLocaleString('de');
    document.getElementById('s-total').textContent   = totAll.toLocaleString('de');
    document.getElementById('ts').textContent = 'Stand: ' + new Date().toLocaleTimeString('de');

    var tbody = document.getElementById('tbody');

    for (var i=0; i<folders.length; i++) {
      var f = folders[i];
      var hasSub = f.sub && f.sub.length > 0;

      var tr = document.createElement('tr');
      tr.className = 'top';

      var td1 = cel('td', 'fw');
      td1.textContent = '\uD83D\uDCC1 ' + f.folder;
      if (hasSub) {
        var tog = document.createElement('span');
        tog.className = 'tog';
        tog.textContent = '+';
        tog.title = 'Unterordner';
        (function(folder, btn) {
          btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var rows = tbody.querySelectorAll('tr[data-p="' + folder + '"]');
            var open = btn.textContent === '-';
            for (var k=0; k<rows.length; k++) rows[k].style.display = open ? 'none' : '';
            btn.textContent = open ? '+' : '-';
          });
        })(f.folder, tog);
        td1.appendChild(tog);
      }
      tr.appendChild(td1);
      tr.appendChild(numCell(f.md,     'grp'));
      tr.appendChild(numCell(f.pdf,    'grp'));
      tr.appendChild(numCell(f.bild,   'grp'));
      tr.appendChild(numCell(f.office, 'grp'));
      tr.appendChild(numCell(f.total,  'grp'));
      tbody.appendChild(tr);

      if (hasSub) {
        for (var j=0; j<f.sub.length; j++) {
          var s = f.sub[j];
          var sr = document.createElement('tr');
          sr.className = 'sub';
          sr.dataset.p = f.folder;
          sr.style.display = 'none';
          sr.appendChild(cel('td', 'indent', s.name));
          sr.appendChild(numCell(s.md,     'grp'));
          sr.appendChild(numCell(s.pdf,    'grp'));
          sr.appendChild(numCell(s.bild,   'grp'));
          sr.appendChild(numCell(s.office, 'grp'));
          sr.appendChild(numCell(s.total,  'grp'));
          tbody.appendChild(sr);
        }
      }
    }

    // Summenzeile
    var sumTr = document.createElement('tr');
    sumTr.className = 'sum-row';
    var sumTd1 = cel('td', 'fw');
    sumTd1.textContent = 'Gesamt';
    sumTr.appendChild(sumTd1);
    sumTr.appendChild(numCell(totMd,     'grp'));
    sumTr.appendChild(numCell(totPdf,    'grp'));
    sumTr.appendChild(numCell(totBild,   'grp'));
    sumTr.appendChild(numCell(totOffice, 'grp'));
    sumTr.appendChild(numCell(totAll,    'grp'));
    tbody.appendChild(sumTr);
  })
  .catch(function(e){
    var el = document.getElementById('err');
    el.style.display = '';
    el.textContent = 'Fehler: ' + e;
  });
</script>
<style>.help-btn{font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--muted);cursor:pointer;font-weight:600;transition:all .15s;white-space:nowrap}.help-btn:hover{border-color:var(--accent);color:var(--accent)}.help-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center}.help-overlay.open{display:flex}.help-box{background:#23263a;border-radius:14px;padding:28px 32px;max-width:520px;width:90%;position:relative;box-shadow:0 20px 60px rgba(0,0,0,.4);color:#e8eaf0}.help-box h2{font-size:15px;font-weight:700;color:#7c6af7;margin-bottom:18px}.help-box h3{font-size:11px;font-weight:700;color:#8a8fb0;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;margin-top:14px}.help-box p{font-size:13px;line-height:1.6;color:#c8cad8}.help-close{position:absolute;top:12px;right:14px;background:none;border:none;font-size:18px;cursor:pointer;color:#8a8fb0;line-height:1;padding:2px}.help-close:hover{color:#e8eaf0}</style>
<div id="help-overlay" class="help-overlay">
  <div class="help-box">
    <button class="help-close" onclick="closeHelp()">✕</button>
    <h2>❓ Vault-Struktur</h2>
    <h3>Was macht dieses Dashboard?</h3>
    <p>Zeigt eine Strukturübersicht des gesamten Dokumenten-Vaults: Anzahl Dokumente pro Kategorie, Verteilung nach Jahr und Gesamtgröße.</p>
    <h3>Wann ist es nützlich?</h3>
    <p>Wenn du wissen möchtest wie viele Dokumente pro Kategorie existieren oder ob alle Dokumente korrekt eingeordnet wurden.</p>
    <h3>Beispiel</h3>
    <p>Du möchtest wissen wie viele Krankenversicherungs-Dokumente seit 2020 vorhanden sind — das Dashboard zeigt die genaue Zahl direkt auf einen Blick.</p>
  </div>
</div>
<script>
function openHelp(){document.getElementById('help-overlay').classList.add('open')}
function closeHelp(){document.getElementById('help-overlay').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHelp()})
document.getElementById('help-overlay').addEventListener('click',e=>{if(e.target===e.currentTarget)closeHelp()})
</script>
</body>
</html>"""

_ANLAGEN_HTML = None  # entfernt in Phase 6 (Einmal-Tool, Aufgabe abgeschlossen)
if False: r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anlagen – Dateinamen-Analyse</title>
<style>
  :root{--bg:#f4f5f7;--surface:#fff;--border:#dde1ea;--text:#1a1d2e;--muted:#6b7280;--ok:#059669;--warn:#d97706;--err:#dc2626;--accent:#4f46e5}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;font-size:13px;min-height:100vh}
  header{border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:10px;background:var(--surface)}
  header h1{font-size:14px;font-weight:700;color:var(--accent)}
  .back-link{font-size:11px;color:var(--muted);text-decoration:none;border:1px solid var(--border);border-radius:6px;padding:3px 9px}
  .back-link:hover{color:var(--accent);border-color:var(--accent)}
  .summary{display:flex;gap:14px;padding:16px 20px;flex-wrap:wrap}
  .sbox{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 18px;min-width:130px}
  .sval{font-size:24px;font-weight:800;color:var(--accent)}
  .slbl{font-size:11px;color:var(--muted);margin-top:2px}
  .sval.warn{color:var(--warn)}
  .twrap{padding:0 20px 30px;overflow-x:auto}
  table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  thead tr{background:#f8f9fb;border-bottom:2px solid var(--border)}
  th{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);padding:10px 12px;text-align:left;white-space:nowrap}
  th.r,td.r{text-align:right}
  tr{border-bottom:1px solid var(--border)}
  tr:hover td{background:#f5f3ff}
  td{padding:7px 12px;font-variant-numeric:tabular-nums}
  .bar-wrap{width:180px;background:#f1f2f6;border-radius:4px;height:10px;overflow:hidden}
  .bar{height:10px;background:var(--accent);border-radius:4px;transition:width .3s}
  .bar.invalid{background:var(--warn)}
  tr.sum-row td{font-weight:700;background:#f0f4ff;border-top:2px solid var(--accent);font-size:12px}
  tr.invalid-row td{color:var(--warn)}
  .ts{margin-left:auto;font-size:11px;color:var(--muted)}
  #err{color:var(--err);padding:20px;display:none}
  .section-head{padding:12px 20px 6px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
</style>
</head>
<body>
<header>
  <a href="/vault" class="back-link">← Vault</a>
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
  <h1>Anlagen – Dateinamen-Analyse</h1>
  <span class="ts" id="ts"></span>
</header>

<div class="summary">
  <div class="sbox"><div class="sval" id="s-total">…</div><div class="slbl">PDFs gesamt</div></div>
  <div class="sbox"><div class="sval" id="s-dated">…</div><div class="slbl">mit Datum-Prefix</div></div>
  <div class="sbox"><div class="sval warn" id="s-undated">…</div><div class="slbl">ohne Datum-Prefix</div></div>
  <div class="sbox"><div class="sval warn" id="s-invalid">…</div><div class="slbl">ungültiges Datum</div></div>
  <div style="margin-left:auto;display:flex;flex-direction:column;justify-content:center;gap:8px;padding-right:4px">
    <button id="btn-start-dated-de" onclick="startRescan('dated_de')" style="padding:8px 18px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-weight:700;font-size:12px;cursor:pointer">▶ Datierte Deutsch-Dokumente scannen</button>
    <button id="btn-start-undated" onclick="startRescan('undated')" style="padding:8px 18px;background:#fff;color:var(--accent);border:1px solid var(--accent);border-radius:8px;font-weight:700;font-size:12px;cursor:pointer">▶ Undatierte scannen</button>
  </div>
</div>
<div id="rescan-status" style="display:none;margin:0 20px 10px;padding:10px 16px;background:#f0f4ff;border:1px solid var(--accent);border-radius:8px;font-size:12px;color:var(--accent)"></div>

<div id="err"></div>
<div class="section-head">Verteilung nach Jahr (gültige Datumspräfixe 1990–2030)</div>
<div class="twrap">
<table id="year-table">
  <thead>
    <tr>
      <th>Jahr</th>
      <th class="r">Anzahl PDFs</th>
      <th class="r">Anteil</th>
      <th style="width:200px">Balken</th>
    </tr>
  </thead>
  <tbody id="tbody-years"></tbody>
</table>
</div>

<div class="section-head" id="invalid-head" style="display:none">Ungültige Datumspräfixe</div>
<div class="twrap" id="invalid-wrap" style="display:none">
<table>
  <thead><tr><th>Präfix (8 Zeichen)</th><th class="r">Anzahl</th></tr></thead>
  <tbody id="tbody-invalid"></tbody>
</table>
</div>

<div class="section-head" id="undated-head" style="display:none">Dateien ohne Datum-Prefix (erste 20)</div>
<div class="twrap" id="undated-wrap" style="display:none">
<table>
  <thead><tr><th>Dateiname</th></tr></thead>
  <tbody id="tbody-undated"></tbody>
</table>
</div>

<script>
function startRescan(mode) {
  var url = mode === 'undated' ? '/api/rescan/start-undated'
          : mode === 'dated_de' ? '/api/rescan/start-dated-de'
          : '/api/rescan/start';
  var btn = document.getElementById(mode === 'undated' ? 'btn-start-undated'
                                  : mode === 'dated_de' ? 'btn-start-dated-de'
                                  : 'btn-start-all');
  btn.disabled = true;
  btn.textContent = '…';
  fetch(url, {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      var box = document.getElementById('rescan-status');
      box.style.display = '';
      if (d.status === 'started') {
        box.textContent = '▶ Rescan gestartet — ' + d.total.toLocaleString('de') + ' PDFs eingereiht (' + (d.already_known||0).toLocaleString('de') + ' bereits bekannt). Fortschritt im Pipeline-Dashboard.';
      } else if (d.status === 'already_running') {
        box.textContent = '⏳ Rescan läuft bereits — ' + d.done + ' / ' + d.total + ' fertig.';
        btn.disabled = false; btn.textContent = _btnLabel(mode);
      } else {
        box.textContent = JSON.stringify(d);
        btn.disabled = false; btn.textContent = _btnLabel(mode);
      }
    })
    .catch(function(e){
      var box = document.getElementById('rescan-status');
      box.style.display = ''; box.style.color = 'var(--err)';
      box.textContent = 'Fehler: ' + e;
      btn.disabled = false; btn.textContent = _btnLabel(mode);
    });
}
function _btnLabel(mode) {
  return mode === 'undated' ? '▶ Undatierte scannen'
       : mode === 'dated_de' ? '▶ Datierte Deutsch-Dokumente scannen'
       : '▶ Alle scannen';
}

fetch('/api/vault/anlagen-analyse')
  .then(function(r){ return r.json(); })
  .then(function(d){
    document.getElementById('s-total').textContent   = d.total.toLocaleString('de');
    document.getElementById('s-dated').textContent   = d.dated.toLocaleString('de');
    document.getElementById('s-undated').textContent = d.undated.toLocaleString('de');
    document.getElementById('s-invalid').textContent = d.invalid_count.toLocaleString('de');
    document.getElementById('ts').textContent = 'Stand: ' + new Date().toLocaleTimeString('de');

    var years = d.by_year || [];
    var maxCount = years.reduce(function(m,y){ return Math.max(m,y.count); }, 1);
    var tbody = document.getElementById('tbody-years');
    var totalDated = d.dated;
    for (var i=0; i<years.length; i++) {
      var y = years[i];
      var pct = totalDated > 0 ? (y.count / totalDated * 100).toFixed(1) : '0.0';
      var barW = Math.round(y.count / maxCount * 100);
      var tr = document.createElement('tr');
      tr.innerHTML = '<td><strong>' + y.year + '</strong></td>' +
        '<td class="r">' + y.count.toLocaleString('de') + '</td>' +
        '<td class="r">' + pct + ' %</td>' +
        '<td><div class="bar-wrap"><div class="bar" style="width:' + barW + '%"></div></div></td>';
      tbody.appendChild(tr);
    }
    // Summenzeile
    var sumTr = document.createElement('tr');
    sumTr.className = 'sum-row';
    sumTr.innerHTML = '<td>Gesamt</td><td class="r">' + d.dated.toLocaleString('de') + '</td><td class="r">100 %</td><td></td>';
    tbody.appendChild(sumTr);

    // Ungültige Präfixe
    var invalids = d.invalid_dates || [];
    if (invalids.length > 0) {
      document.getElementById('invalid-head').style.display = '';
      document.getElementById('invalid-wrap').style.display = '';
      var tbi = document.getElementById('tbody-invalid');
      for (var j=0; j<invalids.length; j++) {
        var iv = invalids[j];
        var itr = document.createElement('tr');
        itr.className = 'invalid-row';
        itr.innerHTML = '<td>' + iv.prefix + '</td><td class="r">' + iv.count + '</td>';
        tbi.appendChild(itr);
      }
    }

    // Undatierte Dateien (Vorschau)
    var undated = d.undated_samples || [];
    if (undated.length > 0) {
      document.getElementById('undated-head').style.display = '';
      document.getElementById('undated-wrap').style.display = '';
      var tbu = document.getElementById('tbody-undated');
      for (var k=0; k<undated.length; k++) {
        var utr = document.createElement('tr');
        utr.innerHTML = '<td>' + undated[k] + '</td>';
        tbu.appendChild(utr);
      }
    }
  })
  .catch(function(e){
    var el = document.getElementById('err');
    el.style.display = '';
    el.textContent = 'Fehler: ' + e;
  });
</script>
</body>
</html>"""

_REVIEW_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling · Review</title>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#7c6af7;--text:#e0e0e0;--muted:#888;--green:#4caf50;--orange:#ff9800;--red:#f44336}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:16px;padding:10px 18px;background:var(--card);border-bottom:1px solid var(--border)}
header h1{font-size:15px;font-weight:600;color:var(--accent)}
header .links a{color:var(--muted);text-decoration:none;font-size:12px;margin-left:12px}
header .links a:hover{color:var(--text)}
.main{display:flex;flex:1;overflow:hidden}
/* Left panel */
#left{width:340px;min-width:260px;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.filter-bar{padding:8px 10px;border-bottom:1px solid var(--border);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.filter-bar select,.filter-bar input{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 6px;font-size:12px}
.filter-bar input{flex:1;min-width:80px}
#doc-list{flex:1;overflow-y:auto;padding:6px}
.doc-item{padding:8px 10px;border-radius:6px;cursor:pointer;border:1px solid transparent;margin-bottom:4px;transition:background .15s}
.doc-item:hover{background:#22253a}
.doc-item.active{background:#1e2240;border-color:var(--accent)}
.doc-item .fname{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.doc-item .meta{font-size:12px;display:flex;gap:6px;align-items:center;margin-top:3px}
.badge{padding:2px 6px;border-radius:10px;font-size:10px;font-weight:600;background:#2a2d3a}
.badge.inbox{background:#3a2a1a;color:var(--orange)}
.badge.niedrig{background:#3a1a1a;color:var(--red)}
.badge.mittel{background:#1a2a3a;color:#5bc0de}
.badge.hoch{background:#1a3a1a;color:var(--green)}
.count-bar{padding:6px 10px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border)}
/* Right panel */
#right{flex:1;display:flex;flex-direction:column;overflow:hidden}
#right-empty{display:flex;align-items:center;justify-content:center;flex:1;color:var(--muted);font-size:15px}
#right-content{display:none;flex-direction:column;flex:1;overflow:hidden}
.doc-header{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.doc-header h2{font-size:13px;font-weight:600;flex:1;min-width:200px;word-break:break-all}
.doc-meta-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px;padding:10px 16px;border-bottom:1px solid var(--border)}
.meta-box{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 10px}
.meta-box .lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.meta-box .val{font-size:13px;font-weight:500;margin-top:2px;word-break:break-word}
.panels{display:flex;flex:1;overflow:hidden;gap:0}
#md-panel{flex:1;overflow-y:auto;padding:12px 16px;border-right:1px solid var(--border);font-size:12px;line-height:1.6;white-space:pre-wrap;font-family:monospace;color:#b0c4de}
#form-panel{width:320px;min-width:260px;overflow-y:auto;padding:14px 16px}
#form-panel h3{font-size:13px;font-weight:600;margin-bottom:12px;color:var(--accent)}
.form-group{margin-bottom:12px}
.form-group label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px}
.form-group select,.form-group input,.form-group textarea{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:6px 8px;font-size:12px}
.form-group textarea{min-height:60px;resize:vertical;font-family:monospace}
.rule-section{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;margin:10px 0}
.rule-section .rule-title{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.5px;margin-bottom:8px}
.radio-group{display:flex;flex-direction:column;gap:6px}
.radio-group label{display:flex;align-items:center;gap:8px;cursor:pointer;font-size:12px}
.rule-fields{margin-top:8px;display:none}
.rule-fields.visible{display:block}
button.primary{width:100%;padding:9px;background:var(--accent);color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;margin-top:8px}
button.primary:hover{background:#6a5ae0}
button.primary:disabled{opacity:.4;cursor:not-allowed}
.btn-secondary{padding:5px 10px;background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:5px;cursor:pointer;font-size:11px}
.btn-secondary:hover{background:var(--card);color:var(--text)}
.retro-toggle{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;cursor:pointer}
.retro-toggle input{cursor:pointer}
.toast{position:fixed;bottom:20px;right:20px;background:#1a3a1a;border:1px solid var(--green);color:var(--green);padding:10px 16px;border-radius:8px;font-size:13px;z-index:999;display:none}
.toast.error{background:#3a1a1a;border-color:var(--red);color:var(--red)}
/* Lernregeln list */
#rules-panel{padding:14px 16px}
#rules-panel h3{font-size:13px;font-weight:600;margin-bottom:10px}
.rule-row{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.rule-row .rinfo{font-size:12px}
.rule-row .rinfo small{color:var(--muted);font-size:11px}
.rule-row .rdel{cursor:pointer;color:var(--red);font-size:16px;padding:0 4px}
</style>
</head>
<body>
<header>
  <h1>📋 Review Dashboard</h1>
  <div class="links">
    <a href="/">Haupt</a>
    <a href="/pipeline">Pipeline</a>
    <a href="/vault">Vault</a>
    <span id="rules-toggle" style="margin-left:12px;cursor:pointer;color:var(--accent);font-size:12px" onclick="toggleRulesView()">📚 Lernregeln anzeigen</span>
    <button class="help-btn" onclick="openHelp()" style="margin-left:auto;font-size:11px;padding:3px 10px;border:1px solid #2a2d3a;border-radius:6px;background:transparent;color:#888;cursor:pointer;font-weight:600">❓ Hilfe</button>
  </div>
</header>
<div class="main">
  <!-- LEFT: doc list -->
  <div id="left">
    <div class="filter-bar">
      <select id="filter-mode" onchange="loadQueue()">
        <option value="inbox">Inbox</option>
        <option value="niedrig">Niedrige Konfidenz</option>
        <option value="all">Alle</option>
      </select>
      <input id="search" placeholder="Suche…" oninput="filterList()">
    </div>
    <div class="count-bar" id="count-bar">Lade…</div>
    <div id="doc-list"></div>
  </div>
  <!-- RIGHT: detail + form -->
  <div id="right">
    <div id="right-empty">← Dokument auswählen</div>
    <div id="right-content">
      <div class="doc-header">
        <h2 id="dh-name"></h2>
        <a id="dh-pdf-link" href="#" target="_blank" class="btn-secondary">PDF öffnen</a>
      </div>
      <div class="doc-meta-grid">
        <div class="meta-box"><div class="lbl">Datum</div><div class="val" id="dm-datum">—</div></div>
        <div class="meta-box"><div class="lbl">Absender</div><div class="val" id="dm-absender">—</div></div>
        <div class="meta-box"><div class="lbl">Kategorie</div><div class="val" id="dm-kategorie">—</div></div>
        <div class="meta-box"><div class="lbl">Typ</div><div class="val" id="dm-typ">—</div></div>
        <div class="meta-box"><div class="lbl">Konfidenz</div><div class="val" id="dm-konfidenz">—</div></div>
        <div class="meta-box"><div class="lbl">Adressat</div><div class="val" id="dm-adressat">—</div></div>
      </div>
      <div class="panels">
        <div id="md-panel"></div>
        <div id="form-panel">
          <h3>Klassifikation korrigieren</h3>
          <div class="form-group">
            <label>Kategorie</label>
            <select id="sel-cat" onchange="onCatChange()">
              <option value="">— wählen —</option>
            </select>
          </div>
          <div class="form-group">
            <label>Typ</label>
            <select id="sel-type">
              <option value="">— wählen —</option>
            </select>
          </div>
          <div class="rule-section">
            <div class="rule-title">System lernen lassen</div>
            <div class="radio-group">
              <label><input type="radio" name="lernmode" value="einmalig" checked onchange="onRuleMode()"> Einmalig (keine Regel)</label>
              <label><input type="radio" name="lernmode" value="absender" onchange="onRuleMode()"> Für diesen Absender merken</label>
              <label><input type="radio" name="lernmode" value="keyword" onchange="onRuleMode()"> Keyword-Regel erstellen</label>
            </div>
            <div id="rule-fields-absender" class="rule-fields">
              <div class="form-group" style="margin-top:8px">
                <label>Absender-Muster</label>
                <input id="inp-absender-muster" placeholder="z.B. Sparkasse Karlsruhe">
              </div>
            </div>
            <div id="rule-fields-keyword" class="rule-fields">
              <div class="form-group" style="margin-top:8px">
                <label>Keywords (kommagetrennt)</label>
                <input id="inp-keywords" placeholder="z.B. Darlehensvertrag, Kreditvertrag">
              </div>
              <div class="form-group">
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                  <input type="checkbox" id="chk-alle-kw"> Alle Keywords müssen vorkommen
                </label>
              </div>
            </div>
            <label class="retro-toggle">
              <input type="checkbox" id="chk-retro" checked>
              Auf alle bestehenden Dokumente anwenden
            </label>
            <div class="form-group" style="margin-top:8px">
              <label>Beschreibung (optional)</label>
              <input id="inp-beschreibung" placeholder="z.B. Sparkasse → Kontoauszug">
            </div>
          </div>
          <button class="primary" id="btn-save" onclick="saveCorrection()">✓ Speichern</button>
          <div id="save-status" style="margin-top:8px;font-size:12px;color:var(--muted)"></div>
        </div>
      </div>
    </div>
    <!-- Lernregeln overlay -->
    <div id="rules-panel" style="display:none">
      <h3>📚 Gespeicherte Lernregeln</h3>
      <button class="btn-secondary" onclick="toggleRulesView()" style="margin-bottom:12px">← Zurück</button>
      <div id="rules-list">Lade…</div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let cats = {};
let allDocs = [];
let currentDoc = null;

async function init() {
  const r = await fetch('/api/categories');
  cats = await r.json();
  const sel = document.getElementById('sel-cat');
  for (const [id, c] of Object.entries(cats)) {
    const o = document.createElement('option');
    o.value = id; o.textContent = c.label;
    sel.appendChild(o);
  }
  loadQueue();
}

async function loadQueue() {
  const mode = document.getElementById('filter-mode').value;
  const r = await fetch('/api/review/queue?filter=' + mode);
  allDocs = await r.json();
  document.getElementById('count-bar').textContent = allDocs.length + ' Dokument(e)';
  filterList();
}

function filterList() {
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = q ? allDocs.filter(d =>
    (d.dateiname||'').toLowerCase().includes(q) ||
    (d.absender||'').toLowerCase().includes(q) ||
    (d.kategorie||'').toLowerCase().includes(q)
  ) : allDocs;
  const list = document.getElementById('doc-list');
  list.innerHTML = '';
  filtered.forEach(doc => {
    const div = document.createElement('div');
    div.className = 'doc-item' + (currentDoc && currentDoc.id === doc.id ? ' active' : '');
    div.onclick = () => selectDoc(doc);
    const conf = doc.konfidenz || '';
    const kat = doc.kategorie || 'Inbox';
    div.innerHTML = `<div class="meta">
      <span class="badge ${kat==='Inbox'?'inbox':conf}">${kat==='Inbox'?'Inbox':kat.substring(0,12)}</span>
      <span class="badge ${conf}">${conf||'—'}</span>
      <span style="color:var(--muted);font-size:10px">${(doc.absender||'').substring(0,18)}</span>
    </div>
    <div class="fname" title="${doc.dateiname}">${doc.dateiname}</div>`;
    list.appendChild(div);
  });
}

async function selectDoc(doc) {
  currentDoc = doc;
  filterList();
  document.getElementById('right-empty').style.display = 'none';
  document.getElementById('right-content').style.display = 'flex';
  document.getElementById('dh-name').textContent = doc.dateiname;
  const pdfName = doc.anlagen_dateiname || doc.dateiname;
  document.getElementById('dh-pdf-link').href = '/api/pdf/' + encodeURIComponent(pdfName);
  document.getElementById('dm-datum').textContent = doc.rechnungsdatum || '—';
  document.getElementById('dm-absender').textContent = doc.absender || '—';
  document.getElementById('dm-kategorie').textContent = doc.kategorie || 'Inbox';
  document.getElementById('dm-typ').textContent = doc.typ || '—';
  document.getElementById('dm-konfidenz').textContent = doc.konfidenz || '—';
  document.getElementById('dm-adressat').textContent = doc.adressat || '—';

  // Load MD content
  document.getElementById('md-panel').textContent = 'Lade…';
  const r = await fetch('/api/document/' + doc.id);
  const d = await r.json();
  document.getElementById('md-panel').textContent = d.md_content || '(kein MD-Inhalt)';

  // Pre-fill form
  const selCat = document.getElementById('sel-cat');
  selCat.value = doc.kategorie || '';
  onCatChange(doc.typ || '');

  // Pre-fill absender rule muster
  document.getElementById('inp-absender-muster').value = doc.absender || '';
  document.getElementById('inp-beschreibung').value = '';
  document.getElementById('save-status').textContent = '';

  // Reset lernmode
  document.querySelectorAll('input[name=lernmode]').forEach(r => r.checked = r.value === 'einmalig');
  onRuleMode();
}

function onCatChange(preselectType) {
  const catId = document.getElementById('sel-cat').value;
  const selType = document.getElementById('sel-type');
  selType.innerHTML = '<option value="">— wählen —</option>';
  if (catId && cats[catId]) {
    cats[catId].types.forEach(t => {
      const o = document.createElement('option');
      o.value = t.id; o.textContent = t.label;
      selType.appendChild(o);
    });
  }
  if (preselectType) selType.value = preselectType;
}

function onRuleMode() {
  const mode = document.querySelector('input[name=lernmode]:checked').value;
  document.getElementById('rule-fields-absender').classList.toggle('visible', mode === 'absender');
  document.getElementById('rule-fields-keyword').classList.toggle('visible', mode === 'keyword');
}

async function saveCorrection() {
  if (!currentDoc) return;
  const cat = document.getElementById('sel-cat').value;
  const typ = document.getElementById('sel-type').value;
  if (!cat) { showToast('Bitte Kategorie wählen', true); return; }
  const mode = document.querySelector('input[name=lernmode]:checked').value;
  const retro = document.getElementById('chk-retro').checked;
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  document.getElementById('save-status').textContent = 'Speichern…';

  const body = {
    doc_id: currentDoc.id,
    category: cat,
    type_id: typ || 'allgemein',
    retroactive: retro,
  };

  if (mode === 'absender') {
    body.lernregel = {
      typ: 'absender',
      muster: document.getElementById('inp-absender-muster').value.trim(),
      beschreibung: document.getElementById('inp-beschreibung').value.trim() ||
        (document.getElementById('inp-absender-muster').value.trim() + ' → ' + cat + '/' + typ),
    };
  } else if (mode === 'keyword') {
    body.lernregel = {
      typ: 'keyword',
      muster: document.getElementById('inp-keywords').value.trim(),
      alle_keywords: document.getElementById('chk-alle-kw').checked ? 1 : 0,
      beschreibung: document.getElementById('inp-beschreibung').value.trim() ||
        (document.getElementById('inp-keywords').value.trim() + ' → ' + cat + '/' + typ),
    };
  }

  try {
    const r = await fetch('/api/correct', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    showToast('✓ Gespeichert' + (d.rule_id ? ' · Regel #' + d.rule_id : '') +
              (retro && mode !== 'einmalig' ? ' · Retroaktiv angewendet…' : ''));
    document.getElementById('save-status').textContent = '';
    // Move to next doc
    const idx = allDocs.findIndex(x => x.id === currentDoc.id);
    allDocs.splice(idx, 1);
    document.getElementById('count-bar').textContent = allDocs.length + ' Dokument(e)';
    filterList();
    const next = allDocs[idx] || allDocs[idx - 1];
    if (next) selectDoc(next); else {
      currentDoc = null;
      document.getElementById('right-content').style.display = 'none';
      document.getElementById('right-empty').style.display = 'flex';
    }
  } catch(e) {
    showToast(e.message || 'Fehler', true);
    document.getElementById('save-status').textContent = '';
  }
  btn.disabled = false;
}

async function toggleRulesView() {
  const rp = document.getElementById('rules-panel');
  const rc = document.getElementById('right-content');
  const re = document.getElementById('right-empty');
  if (rp.style.display === 'none') {
    rp.style.display = 'block';
    rc.style.display = 'none';
    re.style.display = 'none';
    await loadRules();
    document.getElementById('rules-toggle').textContent = '✕ Lernregeln schließen';
  } else {
    rp.style.display = 'none';
    if (currentDoc) rc.style.display = 'flex'; else re.style.display = 'flex';
    document.getElementById('rules-toggle').textContent = '📚 Lernregeln anzeigen';
  }
}

async function loadRules() {
  const r = await fetch('/api/lernregeln');
  const rules = await r.json();
  const list = document.getElementById('rules-list');
  if (!rules.length) { list.innerHTML = '<p style="color:var(--muted)">Keine Lernregeln gespeichert.</p>'; return; }
  list.innerHTML = rules.map(r => `
    <div class="rule-row">
      <div class="rinfo">
        <strong>${r.beschreibung || r.muster}</strong><br>
        <small>Typ: ${r.typ} · Ziel: ${r.category_id}/${r.type_id||'—'} · ${r.anwendungen}× angewendet · ${r.erstellt_am}</small>
      </div>
      <span class="rdel" onclick="deleteRule(${r.id})" title="Löschen">✕</span>
    </div>
  `).join('');
}

async function deleteRule(id) {
  if (!confirm('Regel löschen?')) return;
  await fetch('/api/lernregeln/' + id, {method: 'DELETE'});
  loadRules();
}

function showToast(msg, err) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' error' : '');
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3500);
}

// SSE for live updates
const es = new EventSource('/api/events');
es.addEventListener('lernregel_applied', e => {
  const d = JSON.parse(e.data);
  if (d.updated > 0) showToast(`✓ Regel "${d.beschreibung}": ${d.updated} Dok. aktualisiert`);
});

init();
</script>
<style>.help-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center}.help-overlay.open{display:flex}.help-box{background:#23263a;border-radius:14px;padding:28px 32px;max-width:520px;width:90%;position:relative;box-shadow:0 20px 60px rgba(0,0,0,.4);color:#e8eaf0}.help-box h2{font-size:15px;font-weight:700;color:#7c6af7;margin-bottom:18px}.help-box h3{font-size:11px;font-weight:700;color:#8a8fb0;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;margin-top:14px}.help-box p{font-size:13px;line-height:1.6;color:#c8cad8}.help-close{position:absolute;top:12px;right:14px;background:none;border:none;font-size:18px;cursor:pointer;color:#8a8fb0;line-height:1;padding:2px}.help-close:hover{color:#e8eaf0}</style>
<div id="help-overlay" class="help-overlay">
  <div class="help-box">
    <button class="help-close" onclick="closeHelp()">✕</button>
    <h2>❓ Review Dashboard</h2>
    <h3>Was macht dieses Dashboard?</h3>
    <p>Zeigt alle zuletzt verarbeiteten Dokumente und ermöglicht die manuelle Korrektur von Kategorie, Absender, Adressat und Datum direkt im Browser.</p>
    <h3>Wann ist es nützlich?</h3>
    <p>Wenn die automatische Klassifikation nicht stimmt und du mehrere Dokumente am Desktop korrigieren möchtest — als Alternative zum Telegram-Dialog.</p>
    <h3>Beispiel</h3>
    <p>3 neue Dokumente wurden als "Archiv" eingestuft. Hier kannst du sie aufrufen, die korrekte Kategorie wählen und per Klick speichern.</p>
  </div>
</div>
<script>
function openHelp(){document.getElementById('help-overlay').classList.add('open')}
function closeHelp(){document.getElementById('help-overlay').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHelp()})
document.getElementById('help-overlay').addEventListener('click',e=>{if(e.target===e.currentTarget)closeHelp()})
</script>
</body>
</html>"""

_WILSON_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wilson · Dashboard</title>
<style>
  :root {
    --bg:      #f4f5f7;
    --surface: #ffffff;
    --border:  #dde1ea;
    --text:    #1a1d2e;
    --muted:   #6b7280;
    --ok:      #059669;
    --warn:    #d97706;
    --err:     #dc2626;
    --accent:  #4f46e5;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter','Segoe UI',system-ui,sans-serif; font-size: 14px; min-height: 100vh; }
  header { border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; gap: 12px; background: var(--surface); }
  header h1 { font-size: 16px; font-weight: 700; color: var(--accent); }
  .back-link { font-size: 11px; color: var(--muted); text-decoration: none; border: 1px solid var(--border); border-radius: 6px; padding: 3px 9px; }
  .back-link:hover { color: var(--accent); border-color: var(--accent); }
  .ts { margin-left: auto; font-size: 12px; color: var(--muted); }
  .badge { font-size: 12px; padding: 3px 10px; border-radius: 999px; font-weight: 600; }
  .badge.ok   { background: #d1fae5; color: var(--ok); }
  .badge.warn { background: #fef3c7; color: var(--warn); }
  .badge.err  { background: #fee2e2; color: var(--err); }

  /* Update-Banner */
  .update-banner { background: #fef3c7; border-bottom: 1px solid #fcd34d; padding: 8px 24px; font-size: 12px; color: var(--warn); display: none; }

  /* Grid */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; padding: 20px 24px; }

  /* Cards */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; transition: box-shadow .2s, border-color .2s; }
  .card:hover { border-color: var(--accent); box-shadow: 0 4px 16px rgba(79,70,229,.08); }
  .card.wide { grid-column: 1 / -1; }
  .card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .dot.ok    { background: var(--ok);   box-shadow: 0 0 0 3px #d1fae5; }
  .dot.warn  { background: var(--warn); box-shadow: 0 0 0 3px #fef3c7; }
  .dot.error { background: var(--err);  box-shadow: 0 0 0 3px #fee2e2; }
  .card-title { font-weight: 600; font-size: 14px; }
  .card-badge { margin-left: auto; font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; }
  .card-badge.ok    { background: #d1fae5; color: var(--ok); }
  .card-badge.warn  { background: #fef3c7; color: var(--warn); }
  .card-badge.error { background: #fee2e2; color: var(--err); }

  /* Metrics */
  .metrics { display: flex; flex-direction: column; gap: 7px; }
  .metric { display: flex; justify-content: space-between; align-items: baseline; }
  .metric-label { color: var(--muted); font-size: 12px; }
  .metric-value { font-size: 13px; font-weight: 500; }
  .metric-value.mono { font-family: monospace; font-size: 11px; }
  .error-msg { margin-top: 8px; font-size: 11px; color: var(--err); }

  /* Cron table */
  .cron-table { width: 100%; border-collapse: collapse; margin-top: 4px; }
  .cron-table th { text-align: left; font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; padding: 5px 8px; border-bottom: 1px solid var(--border); }
  .cron-table td { padding: 7px 8px; font-size: 12px; border-bottom: 1px solid #f0f1f5; vertical-align: middle; }
  .cron-table tr:last-child td { border-bottom: none; }
  .cron-table tr.disabled td { opacity: .45; }
  .kbadge { font-size: 10px; padding: 2px 6px; border-radius: 999px; font-weight: 600; }
  .kbadge.ok  { background: #d1fae5; color: var(--ok); }
  .kbadge.err { background: #fee2e2; color: var(--err); }
  .kbadge.off { background: #f1f2f6; color: var(--muted); }

  /* File list */
  .file-list { display: flex; flex-direction: column; gap: 5px; margin-top: 4px; }
  .file-item { display: flex; justify-content: space-between; align-items: baseline; padding: 5px 8px; background: #f8f9fb; border-radius: 6px; font-size: 11px; }
  .file-name { color: var(--text); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
  .file-meta { color: var(--muted); white-space: nowrap; margin-left: 8px; }

  /* Sessions */
  .session-list { display: flex; flex-direction: column; gap: 5px; margin-top: 4px; }
  .session-item { display: flex; justify-content: space-between; align-items: center; padding: 5px 8px; background: #f8f9fb; border-radius: 6px; font-size: 11px; }
  .session-label { color: var(--text); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 300px; }
  .session-ts { color: var(--muted); white-space: nowrap; margin-left: 8px; }

  /* Log viewer */
  .log-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
  .log-toolbar select, .log-toolbar button { font-size: 11px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer; }
  .log-toolbar button:hover, .log-toolbar select:hover { border-color: var(--accent); color: var(--accent); }
  .log-toolbar label { font-size: 11px; color: var(--muted); display: flex; gap: 4px; align-items: center; cursor: pointer; }
  .log-toolbar .log-meta { margin-left: auto; font-size: 11px; color: var(--muted); font-family: monospace; }
  .log-view { background: #1a1d2e; color: #d1d5db; font-family: 'SF Mono',Menlo,Consolas,monospace; font-size: 11px; line-height: 1.55; padding: 12px 14px; border-radius: 8px; max-height: 420px; overflow-y: auto; white-space: pre; }
  .log-row { display: flex; gap: 8px; padding: 1px 0; }
  .log-row.filtered { display: none; }
  .log-time { color: #6b7280; flex-shrink: 0; }
  .log-level { font-weight: 700; flex-shrink: 0; width: 52px; }
  .log-level.error { color: #f87171; }
  .log-level.warn  { color: #fbbf24; }
  .log-level.info  { color: #60a5fa; }
  .log-level.debug { color: #9ca3af; }
  .log-level.trace { color: #6b7280; }
  .log-msg { color: #e5e7eb; white-space: pre-wrap; word-break: break-word; }
  .log-msg.err { color: #fca5a5; }
  .log-empty { color: var(--muted); font-size: 12px; padding: 18px; text-align: center; }

  /* TUI card */
  .tui-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .tui-btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 8px; background: var(--accent); color: #fff; border: none; font-size: 13px; font-weight: 600; cursor: pointer; text-decoration: none; }
  .tui-btn:hover { opacity: .9; }
  .tui-btn.sec { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
  .tui-btn.sec:hover { border-color: var(--accent); color: var(--accent); }
  .tui-creds { font-family: monospace; font-size: 11px; padding: 5px 10px; background: #f8f9fb; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); }
  .tui-frame-wrap { margin-top: 10px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: #000; }
  .tui-frame-wrap iframe { display: block; width: 100%; height: 520px; border: none; }

  .refresh-bar { text-align: center; padding: 14px; font-size: 11px; color: var(--muted); background: var(--surface); border-top: 1px solid var(--border); }
  #countdown { color: var(--accent); font-weight: 600; }
</style>
</head>
<body>
<header>
  <a href="/" class="back-link">← Dashboard</a>
  <span style="font-size:20px">🥧</span>
  <h1>Wilson · OpenClaw Dashboard</h1>
  <a href="/vault" style="font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;color:var(--muted);text-decoration:none;font-weight:600;margin-left:auto" title="Vault-Struktur">📁 Vault</a>
  <button class="help-btn" onclick="openHelp()">❓ Hilfe</button>
  <span class="badge" id="overall-badge">Laden…</span>
  <span class="ts" id="ts">–</span>
</header>
<div class="update-banner" id="update-banner"></div>
<div class="grid" id="grid">
  <div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted)">Lade Daten von Wilson Pi…</div>
</div>
<div class="grid" id="extras" style="padding-top:0"></div>
<div class="refresh-bar">Auto-Refresh in <span id="countdown">30</span>s &nbsp;·&nbsp; <a href="#" onclick="load();return false;" style="color:var(--accent)">Jetzt aktualisieren</a></div>

<script>
function fmtDur(ms) {
  if (!ms) return '–';
  if (ms < 1000) return ms + ' ms';
  return (ms/1000).toFixed(1) + ' s';
}
function fmtTs(ms) {
  if (!ms) return '–';
  return new Date(ms).toLocaleString('de-DE', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function statusDot(ok) {
  return ok ? 'ok' : 'error';
}

function renderGateway(d) {
  const gw = d.gateway || {};
  const st = gw.running && gw.health_ok ? 'ok' : gw.running ? 'warn' : 'error';
  const stLabel = st === 'ok' ? 'OK' : st === 'warn' ? 'Kein Health' : 'OFFLINE';
  return `<div class="card">
    <div class="card-header">
      <span class="dot ${st}"></span>
      <span class="card-title">⚡ OpenClaw Gateway</span>
      <span class="card-badge ${st}">${stLabel}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="metric-label">PID</span><span class="metric-value">${gw.pid || '–'}</span></div>
      <div class="metric"><span class="metric-label">CPU</span><span class="metric-value">${gw.cpu_pct != null ? gw.cpu_pct + '%' : '–'}</span></div>
      <div class="metric"><span class="metric-label">RAM</span><span class="metric-value">${gw.mem_pct != null ? gw.mem_pct + '%' : '–'}</span></div>
      <div class="metric"><span class="metric-label">Uptime</span><span class="metric-value">${gw.uptime || '–'}</span></div>
      <div class="metric"><span class="metric-label">Version installiert</span><span class="metric-value">${gw.version_installed || '–'}</span></div>
      <div class="metric"><span class="metric-label">Version verfügbar</span><span class="metric-value">${gw.version_available || '–'}</span></div>
    </div>
  </div>`;
}

function renderTelegram(d) {
  const tg = d.telegram || {};
  const st = tg.enabled ? 'ok' : 'warn';
  return `<div class="card">
    <div class="card-header">
      <span class="dot ${st}"></span>
      <span class="card-title">📱 Telegram Bot</span>
      <span class="card-badge ${st}">${tg.enabled ? 'Aktiv' : 'Inaktiv'}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="metric-label">Letztes Update-ID</span><span class="metric-value">${tg.last_update_id || '–'}</span></div>
    </div>
    ${tg.error ? `<div class="error-msg">⚠ ${tg.error}</div>` : ''}
  </div>`;
}

function renderOllama(d) {
  const ol = d.ollama || {};
  const st = ol.reachable ? 'ok' : 'error';
  return `<div class="card">
    <div class="card-header">
      <span class="dot ${st}"></span>
      <span class="card-title">🤖 Ollama (Ryzen)</span>
      <span class="card-badge ${st}">${ol.reachable ? 'Erreichbar' : 'OFFLINE'}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="metric-label">URL</span><span class="metric-value mono">192.168.86.195:11434</span></div>
    </div>
  </div>`;
}

function renderSyncthing(d) {
  const st_data = d.syncthing || {};
  const st = st_data.connected ? 'ok' : 'error';
  const peers = (st_data.peers || []).join(', ') || '–';
  return `<div class="card">
    <div class="card-header">
      <span class="dot ${st}"></span>
      <span class="card-title">🔄 Syncthing → Ryzen</span>
      <span class="card-badge ${st}">${st_data.connected ? 'Verbunden' : 'Getrennt'}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="metric-label">Peer-Adresse</span><span class="metric-value mono">${peers}</span></div>
    </div>
    ${st_data.error ? `<div class="error-msg">⚠ ${st_data.error}</div>` : ''}
  </div>`;
}

function renderInputFolder(d) {
  const inp = d.input_folder || {};
  const st = inp.count === 0 ? 'ok' : 'warn';
  const stLabel = inp.count === 0 ? 'Leer' : `${inp.count} Datei${inp.count !== 1 ? 'en' : ''}`;
  const files = (inp.files || []).map(f =>
    `<div class="file-item">
      <span class="file-name" title="${f.name}">${f.name}</span>
      <span class="file-meta">${f.size_kb} KB · ${f.mtime}</span>
    </div>`
  ).join('');
  return `<div class="card wide">
    <div class="card-header">
      <span class="dot ${st}"></span>
      <span class="card-title">📂 Input-Ordner (~/input-dispatcher)</span>
      <span class="card-badge ${st}">${stLabel}</span>
    </div>
    ${files ? `<div class="file-list">${files}</div>` : '<div style="font-size:12px;color:var(--muted)">Keine Dateien vorhanden.</div>'}
  </div>`;
}

function renderCron(d) {
  const jobs = d.cron_jobs || [];
  const hasErr = jobs.some(j => j.errors > 0);
  const st = hasErr ? 'warn' : 'ok';
  const rows = jobs.map(j => {
    const jst = j.errors > 0 ? 'err' : j.last_status === 'ok' ? 'ok' : 'off';
    const jstLabel = j.errors > 0 ? `${j.errors}✗` : (j.last_status || '–');
    return `<tr class="${j.enabled ? '' : 'disabled'}">
      <td>${j.enabled ? '✅' : '⏸️'}</td>
      <td style="font-weight:500">${j.name || '–'}</td>
      <td><code style="font-size:10px">${j.schedule || '–'}</code></td>
      <td><span class="kbadge ${jst}">${jstLabel}</span></td>
      <td style="color:var(--muted)">${fmtDur(j.last_dur_ms)}</td>
      <td style="color:var(--muted)">${fmtTs(j.last_run_ms)}</td>
      <td style="color:var(--muted)">${fmtTs(j.next_run_ms)}</td>
    </tr>`;
  }).join('');
  return `<div class="card wide">
    <div class="card-header">
      <span class="dot ${st}"></span>
      <span class="card-title">⏰ Cron-Jobs</span>
      <span class="card-badge ${st}">${jobs.length} Jobs</span>
    </div>
    ${rows ? `<table class="cron-table">
      <thead><tr><th></th><th>Name</th><th>Schedule</th><th>Status</th><th>Dauer</th><th>Letzter Run</th><th>Nächster Run</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>` : '<div style="font-size:12px;color:var(--muted)">Keine Cron-Jobs konfiguriert.</div>'}
  </div>`;
}

function renderSessions(d) {
  const sess = d.sessions || {};
  const recent = sess.recent || [];
  const items = recent.map(s => {
    const ts = s.updatedAt ? new Date(s.updatedAt).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '–';
    return `<div class="session-item">
      <span class="session-label" title="${s.label}">${s.label}</span>
      <span class="session-ts">${ts}</span>
    </div>`;
  }).join('');
  return `<div class="card wide">
    <div class="card-header">
      <span class="dot ok"></span>
      <span class="card-title">💬 Letzte Sessions</span>
      <span class="card-badge ok">${sess.total || 0} gesamt</span>
    </div>
    ${items ? `<div class="session-list">${items}</div>` : '<div style="font-size:12px;color:var(--muted)">Keine Sessions.</div>'}
    ${sess.error ? `<div class="error-msg">⚠ ${sess.error}</div>` : ''}
  </div>`;
}

function renderLogCard() {
  return `<div class="card wide">
    <div class="card-header">
      <span class="dot ok" id="log-dot"></span>
      <span class="card-title">📜 OpenClaw Log</span>
      <span class="card-badge ok" id="log-badge">…</span>
    </div>
    <div class="log-toolbar">
      <label>Zeilen
        <select id="log-lines">
          <option value="50">50</option>
          <option value="200" selected>200</option>
          <option value="500">500</option>
          <option value="1000">1000</option>
        </select>
      </label>
      <label>Level
        <select id="log-level">
          <option value="all">Alle</option>
          <option value="error">ERROR</option>
          <option value="warn">WARN+</option>
          <option value="info">INFO+</option>
        </select>
      </label>
      <label title="Automatisch mitscrollen"><input type="checkbox" id="log-follow" checked> Auto-Scroll</label>
      <button onclick="loadLog()">Aktualisieren</button>
      <span class="log-meta" id="log-meta">–</span>
    </div>
    <div class="log-view" id="log-view"><div class="log-empty">Lade Log…</div></div>
  </div>`;
}

function renderTuiCard(info) {
  const url = (info && info.url) || ('http://' + location.hostname + ':7681/');
  const err = info && info.error;
  return `<div class="card wide">
    <div class="card-header">
      <span class="dot ${err ? 'error' : 'ok'}"></span>
      <span class="card-title">💻 OpenClaw TUI</span>
      <span class="card-badge ${err ? 'error' : 'ok'}">${err ? 'Fehler' : 'ttyd aktiv'}</span>
    </div>
    ${err
      ? `<div class="error-msg">⚠ ${err}</div>`
      : `<div class="tui-actions">
          <a class="tui-btn" href="${url}" target="_blank" rel="noopener noreferrer" onclick="window.open('${url}','_blank','noopener,noreferrer');return false;">↗ TUI in neuem Tab</a>
          <button class="tui-btn sec" onclick="toggleTui()">Inline ein-/ausblenden</button>
          <a class="log-meta" href="${url}" target="_blank" rel="noopener noreferrer" style="text-decoration:none">${url}</a>
        </div>
        <div class="tui-frame-wrap" id="tui-frame-wrap" style="display:none">
          <iframe id="tui-frame" title="OpenClaw TUI"></iframe>
        </div>`
    }
  </div>`;
}

let _tuiUrl = '';
function toggleTui() {
  const wrap = document.getElementById('tui-frame-wrap');
  const frame = document.getElementById('tui-frame');
  if (!wrap || !frame) return;
  if (wrap.style.display === 'none') {
    if (!frame.src && _tuiUrl) frame.src = _tuiUrl;
    wrap.style.display = 'block';
  } else {
    wrap.style.display = 'none';
  }
}

function fmtLogTime(t) {
  try {
    const d = new Date(t);
    return d.toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  } catch(e) { return t || ''; }
}

const _LEVEL_RANK = { error:0, warn:1, info:2, debug:3, trace:4 };
function parseLogLine(raw) {
  try {
    const o = JSON.parse(raw);
    const meta = o._meta || {};
    const lvl = (meta.logLevelName || '').toLowerCase() || 'info';
    // Message kann unter "0" liegen (TSLog-Format) oder als einzelnes Feld
    let msg = '';
    if (typeof o['0'] === 'string') msg = o['0'];
    else if (typeof o.message === 'string') msg = o.message;
    else msg = Object.keys(o).filter(k => k !== '_meta' && k !== 'time').map(k => String(o[k])).join(' ');
    return { time: o.time || meta.date, level: lvl, msg: msg.trim(), raw };
  } catch(e) {
    return { time: null, level: 'info', msg: raw, raw };
  }
}

async function loadLog() {
  const lines = document.getElementById('log-lines')?.value || 200;
  const view = document.getElementById('log-view');
  const meta = document.getElementById('log-meta');
  const badge = document.getElementById('log-badge');
  const dot = document.getElementById('log-dot');
  if (!view) return;
  try {
    const res = await fetch('/api/wilson/logs?lines=' + encodeURIComponent(lines));
    const d = await res.json();
    if (d.error) {
      view.innerHTML = `<div class="log-empty" style="color:#f87171">⚠ ${d.error}</div>`;
      if (badge) { badge.textContent = 'Fehler'; badge.className = 'card-badge error'; }
      if (dot)   dot.className = 'dot error';
      return;
    }
    if (d.missing) {
      view.innerHTML = `<div class="log-empty">Keine Log-Datei für heute (${d.file || '–'})</div>`;
      if (badge) { badge.textContent = '–'; badge.className = 'card-badge warn'; }
      if (dot)   dot.className = 'dot warn';
      if (meta)  meta.textContent = d.file || '';
      return;
    }
    const filterLvl = document.getElementById('log-level')?.value || 'all';
    const follow = document.getElementById('log-follow')?.checked;
    const parsed = (d.lines || []).map(parseLogLine);
    const errCount = parsed.filter(p => p.level === 'error').length;
    const warnCount = parsed.filter(p => p.level === 'warn').length;
    const html = parsed.map(p => {
      let hidden = '';
      if (filterLvl !== 'all') {
        const max = _LEVEL_RANK[filterLvl] ?? 99;
        const cur = _LEVEL_RANK[p.level] ?? 99;
        if (cur > max) hidden = ' filtered';
      }
      const t = p.time ? fmtLogTime(p.time) : '';
      const lvlClass = _LEVEL_RANK[p.level] != null ? p.level : 'info';
      const msgClass = p.level === 'error' ? 'err' : '';
      return `<div class="log-row${hidden}">
        <span class="log-time">${t}</span>
        <span class="log-level ${lvlClass}">${p.level.toUpperCase()}</span>
        <span class="log-msg ${msgClass}">${escapeHtml(p.msg)}</span>
      </div>`;
    }).join('');
    view.innerHTML = html || '<div class="log-empty">Keine Zeilen.</div>';
    if (follow) view.scrollTop = view.scrollHeight;
    const kb = d.size_bytes ? Math.round(d.size_bytes/1024) + ' KB' : '';
    const mtime = d.mtime ? ' · Stand ' + new Date(d.mtime * 1000).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
    if (meta)  meta.textContent = `${d.returned_lines}/${d.requested_lines} Zeilen · ${kb}${mtime} · ${d.file || ''}`;
    const state = errCount > 0 ? 'error' : (warnCount > 0 ? 'warn' : 'ok');
    if (badge) { badge.textContent = errCount > 0 ? `${errCount}✗` : (warnCount > 0 ? `${warnCount}⚠` : 'OK'); badge.className = 'card-badge ' + state; }
    if (dot)   dot.className = 'dot ' + state;
  } catch(e) {
    view.innerHTML = `<div class="log-empty" style="color:#f87171">⚠ ${e.message}</div>`;
  }
}
function escapeHtml(s) { return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function copyText(txt, el) {
  try {
    navigator.clipboard.writeText(txt);
    if (el) { const o = el.textContent; el.textContent = '✓ kopiert'; setTimeout(() => el.textContent = o, 1200); }
  } catch(e) { alert(txt); }
}

async function initExtras() {
  // TUI-Info holen und Karten rendern (nur einmal)
  let tui = null;
  try { tui = await (await fetch('/api/wilson/tui-info')).json(); } catch(e) { tui = { error: e.message }; }
  if (tui && tui.url) _tuiUrl = tui.url;
  document.getElementById('extras').innerHTML = renderLogCard() + renderTuiCard(tui);
  document.getElementById('log-lines')?.addEventListener('change', loadLog);
  document.getElementById('log-level')?.addEventListener('change', loadLog);
  loadLog();
}

async function load() {
  try {
    const res = await fetch('/api/wilson/status');
    const d = await res.json();

    document.getElementById('ts').textContent = 'Stand: ' + (d.ts || '').replace('T',' ');

    // Update-Banner
    const banner = document.getElementById('update-banner');
    if (d.gateway?.update_available) {
      banner.style.display = '';
      banner.textContent = `⚠️ Update verfügbar: ${d.gateway.version_installed} → ${d.gateway.version_available}`;
    } else {
      banner.style.display = 'none';
    }

    // Overall badge
    const badge = document.getElementById('overall-badge');
    const allOk = d.gateway?.running && d.gateway?.health_ok && d.ollama?.reachable && d.syncthing?.connected;
    badge.className = 'badge ' + (allOk ? 'ok' : 'warn');
    badge.textContent = allOk ? '✓ Alles OK' : '⚠ Probleme';

    if (d.error) {
      document.getElementById('grid').innerHTML = `<div style="grid-column:1/-1;padding:40px;text-align:center;color:var(--err)">SSH-Fehler: ${d.error}</div>`;
      return;
    }

    document.getElementById('grid').innerHTML =
      renderGateway(d) +
      renderTelegram(d) +
      renderOllama(d) +
      renderSyncthing(d) +
      renderInputFolder(d) +
      renderCron(d) +
      renderSessions(d);

  } catch(e) {
    document.getElementById('ts').textContent = 'Fehler: ' + e.message;
  }
}

let secs = 30;
function tick() {
  secs--;
  document.getElementById('countdown').textContent = secs;
  if (secs <= 0) { secs = 30; load(); loadLog(); }
}

load();
initExtras();
setInterval(tick, 1000);
</script>
<style>.help-btn{font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--muted);cursor:pointer;font-weight:600;transition:all .15s;white-space:nowrap}.help-btn:hover{border-color:var(--accent);color:var(--accent)}.help-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center}.help-overlay.open{display:flex}.help-box{background:#23263a;border-radius:14px;padding:28px 32px;max-width:520px;width:90%;position:relative;box-shadow:0 20px 60px rgba(0,0,0,.4);color:#e8eaf0}.help-box h2{font-size:15px;font-weight:700;color:#7c6af7;margin-bottom:18px}.help-box h3{font-size:11px;font-weight:700;color:#8a8fb0;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;margin-top:14px}.help-box p{font-size:13px;line-height:1.6;color:#c8cad8}.help-close{position:absolute;top:12px;right:14px;background:none;border:none;font-size:18px;cursor:pointer;color:#8a8fb0;line-height:1;padding:2px}.help-close:hover{color:#e8eaf0}</style>
<div id="help-overlay" class="help-overlay">
  <div class="help-box">
    <button class="help-close" onclick="closeHelp()">✕</button>
    <h2>❓ Wilson Pi Dashboard</h2>
    <h3>Was macht dieses Dashboard?</h3>
    <p>Zeigt den Status des Raspberry Pi (Wilson), der deinen Scanner überwacht — welche Dienste laufen, letzte Aktivitäten und Verbindungsqualität zu Ryzen.</p>
    <h3>Wann ist es nützlich?</h3>
    <p>Wenn nach einem Scan keine Telegram-Benachrichtigung kam und du prüfen möchtest ob Wilson und seine Dienste laufen.</p>
    <h3>Beispiel</h3>
    <p>Kein Lebenszeichen seit 3 Stunden → hier siehst du sofort: Heartbeat aktiv, letztes Dokument 14:23 Uhr, Ollama erreichbar — alles OK.</p>
  </div>
</div>
<script>
function openHelp(){document.getElementById('help-overlay').classList.add('open')}
function closeHelp(){document.getElementById('help-overlay').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHelp()})
document.getElementById('help-overlay').addEventListener('click',e=>{if(e.target===e.currentTarget)closeHelp()})
</script>
</body>
</html>"""


CACHE_READER_URL = os.environ.get("CACHE_READER_URL", "http://cache-reader:8501")


_CACHE_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling Workflow · Cache-Reader</title>
<style>
:root{--bg:#f4f5f7;--surface:#fff;--border:#dde1ea;--text:#1a1d2e;--muted:#6b7280;--ok:#059669;--warn:#d97706;--err:#dc2626;--accent:#4f46e5}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
header{border-bottom:1px solid var(--border);padding:13px 24px;display:flex;align-items:center;gap:10px;background:var(--surface);flex-wrap:wrap}
header h1{font-size:16px;font-weight:700;color:var(--accent);white-space:nowrap;margin-right:4px}
nav a{font-size:12px;padding:4px 12px;border:1px solid var(--border);border-radius:7px;color:var(--text);text-decoration:none;font-weight:600;white-space:nowrap;transition:all .15s;margin-right:5px}
nav a:hover,nav a.hl{border-color:var(--accent);color:var(--accent)}
.ts{font-size:12px;color:var(--muted);margin-left:auto}

.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;padding:18px 24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:700;margin-bottom:6px}
.stat .val{font-size:22px;font-weight:700;color:var(--text)}
.stat .sub{font-size:11px;color:var(--muted);margin-top:2px}
.stat.ok .val{color:var(--ok)}
.stat.warn .val{color:var(--warn)}

.section{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin:14px 24px;overflow:hidden}
.section h2{font-size:13px;font-weight:700;color:var(--text);padding:12px 18px;border-bottom:1px solid var(--border);background:#fafbfc;display:flex;align-items:center;gap:8px}
.section h2 .actions{margin-left:auto;display:flex;gap:8px}
.section .body{padding:16px 18px}

.search-row{display:flex;gap:8px}
.search-row input{flex:1;padding:10px 14px;border:1px solid var(--border);border-radius:8px;font-size:14px;font-family:inherit;outline:none}
.search-row input:focus{border-color:var(--accent)}
.search-row button{padding:10px 18px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer}
.search-row button:hover{opacity:.9}
.search-row button.secondary{background:#fff;color:var(--muted);border:1px solid var(--border)}
.search-row button.secondary:hover{border-color:var(--accent);color:var(--accent)}

.results{margin-top:14px;max-height:540px;overflow-y:auto}
.result{padding:12px 14px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;background:#fafbfc;transition:border-color .15s,background .15s}
.result:hover{border-color:var(--accent);background:#fff}
.result.stale{background:#f9fafb;border-style:dashed;opacity:.85}
.result.stale:hover{border-color:var(--warn);background:#fffbf0}
.result a.r-path{display:block;font-size:12px;font-weight:700;color:var(--accent);margin-bottom:4px;word-break:break-all;text-decoration:none}
.result a.r-path:hover{text-decoration:underline}
.result .r-path-stale{display:block;font-size:12px;font-weight:700;color:var(--muted);margin-bottom:4px;word-break:break-all;text-decoration:line-through}
.stale-badge{display:inline-block;font-size:10px;padding:1px 7px;border-radius:4px;background:#fef3c7;color:var(--warn);font-weight:700;text-transform:none;letter-spacing:0;margin-left:6px;vertical-align:middle}
.result .r-excerpt{font-size:12px;color:var(--muted);line-height:1.55;cursor:pointer;padding:4px;margin:-4px;border-radius:4px;transition:background .15s}
.result .r-excerpt:hover{background:#eef2ff;color:var(--text)}
.result .r-meta{font-size:10px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.04em;display:flex;align-items:center;gap:8px}
.result .r-score{display:inline-block;padding:1px 6px;background:#eef2ff;color:var(--accent);border-radius:4px;font-weight:700}
.result .r-action{font-size:10px;color:var(--muted);margin-left:auto;font-style:italic;text-transform:none;letter-spacing:0}

/* Modal */
.modal-backdrop{position:fixed;inset:0;background:rgba(15,18,34,.5);display:none;align-items:center;justify-content:center;z-index:1000;padding:20px}
.modal-backdrop.open{display:flex}
.modal{background:#fff;border-radius:14px;max-width:900px;width:100%;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3);overflow:hidden}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.modal-head h3{font-size:14px;font-weight:700;color:var(--text);flex:1;word-break:break-all}
.modal-head .close{cursor:pointer;font-size:22px;color:var(--muted);background:none;border:none;padding:0 8px}
.modal-head .close:hover{color:var(--err)}
.modal-body{padding:16px 20px;overflow-y:auto;white-space:pre-wrap;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:12px;line-height:1.6;color:var(--text);background:#fafbfc}
.modal-foot{padding:10px 20px;border-top:1px solid var(--border);display:flex;gap:10px;align-items:center}
.modal-foot .openpdf{font-size:12px;padding:6px 14px;background:var(--accent);color:#fff;border:none;border-radius:6px;text-decoration:none;font-weight:700;cursor:pointer}
.modal-foot .openpdf:hover{opacity:.9}
.modal-foot .meta{font-size:11px;color:var(--muted);margin-left:auto}

.empty{color:var(--muted);font-size:12px;text-align:center;padding:24px}

.langs{display:flex;gap:6px;flex-wrap:wrap}
.lang-tag{font-size:11px;padding:2px 8px;border-radius:4px;background:#eef2ff;color:var(--accent);font-weight:700}
</style>
</head>
<body>
<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
  <h1>Docling Workflow</h1>
  <nav>
    <a href="/pipeline" target="_blank" rel="noopener">⚡ Pipeline</a>
    <a href="/review" target="_blank" rel="noopener">📋 Review</a>
    <a href="/vault" target="_blank" rel="noopener">📁 Vault</a>
    <a href="/cache" class="hl">🔍 Cache</a>
    <a href="/batch" target="_blank" rel="noopener">🧰 Batch</a>
    <a href="/wilson" target="_blank" rel="noopener">🥧 Wilson</a>
    <a href="/duplikate" target="_blank" rel="noopener">&#127366; Duplikate</a>
    <a href="/frontmatter" target="_blank" rel="noopener">🏷️ Frontmatter</a>
  </nav>
  <span class="ts" id="ts">Laden…</span>
</header>

<div class="stats" id="stats">
  <div class="stat"><div class="lbl">Indiziert</div><div class="val" id="stat-total">–</div><div class="sub">Cache-Einträge gesamt</div></div>
  <div class="stat ok"><div class="lbl">Verwertbar</div><div class="val" id="stat-usable">–</div><div class="sub">mit Text ≥ 50 Zeichen</div></div>
  <div class="stat warn"><div class="lbl">Leer</div><div class="val" id="stat-empty">–</div><div class="sub">< 50 Zeichen</div></div>
  <div class="stat"><div class="lbl">Sprachen</div><div class="val" style="font-size:14px;line-height:1.8"><div class="langs" id="stat-langs">–</div></div></div>
  <div class="stat"><div class="lbl">Letzte Indexierung</div><div class="val" style="font-size:14px" id="stat-reindex">–</div></div>
</div>

<div class="section">
  <h2>🔎 Live-Suche</h2>
  <div class="body">
    <div class="search-row">
      <input type="text" id="search-input" placeholder="Suchbegriff eingeben, z. B. 'Ferroli Heizung'" autocomplete="off">
      <button onclick="doSearch()">Suchen</button>
      <button class="secondary" onclick="clearResults()">Leeren</button>
      <button class="secondary" id="btn-batch" onclick="sendToBatch()" style="display:none">▶ An Batch übergeben</button>
    </div>
    <div id="batch-msg" style="margin-top:8px;font-size:12px;color:var(--muted)"></div>
    <div class="results" id="results"></div>
  </div>
</div>

<div class="modal-backdrop" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-head">
      <h3 id="modal-title">–</h3>
      <button class="close" onclick="closeModal()">×</button>
    </div>
    <div class="modal-body" id="modal-body">Laden…</div>
    <div class="modal-foot">
      <a class="openpdf" id="modal-openpdf" href="#" target="_blank" rel="noopener">📄 PDF öffnen</a>
      <span class="meta" id="modal-meta">–</span>
    </div>
  </div>
</div>

<div class="section">
  <h2>
    ⚙️ Verwaltung
    <div class="actions">
      <button class="secondary" onclick="reindex()" style="padding:6px 14px;font-size:12px">Neu indizieren</button>
    </div>
  </h2>
  <div class="body" id="admin-status" style="font-size:12px;color:var(--muted)">
    Der Cache-Reader-Service liest den Text-Extractor-Cache und baut einen SQLite-FTS5-Volltextindex auf.
    Änderungen am Cache werden automatisch erkannt (File-Watcher). Eine manuelle Neu-Indizierung ist nur nötig,
    wenn der Index beschädigt ist oder große Bestandsänderungen erfolgten.
  </div>
</div>

<script>
async function loadStats(){
  try{
    const r = await fetch('/api/cache/stats');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const s = await r.json();
    document.getElementById('stat-total').textContent = s.total_documents.toLocaleString('de');
    document.getElementById('stat-usable').textContent = s.usable_documents.toLocaleString('de');
    document.getElementById('stat-empty').textContent = s.empty_documents.toLocaleString('de');
    const langs = Object.entries(s.languages || {}).map(
      ([k,v]) => `<span class="lang-tag">${k}: ${v.toLocaleString('de')}</span>`
    ).join('');
    document.getElementById('stat-langs').innerHTML = langs || '–';
    if(s.last_full_reindex){
      const d = new Date(s.last_full_reindex*1000);
      document.getElementById('stat-reindex').textContent = d.toLocaleString('de-DE', {dateStyle:'short', timeStyle:'short'});
    }
    document.getElementById('ts').textContent = 'Aktualisiert: ' + new Date().toLocaleTimeString('de-DE');
  }catch(e){
    document.getElementById('ts').textContent = 'Fehler: ' + e.message;
  }
}

let lastQuery = '';
let lastCount = 0;

async function doSearch(){
  const q = document.getElementById('search-input').value.trim();
  const results = document.getElementById('results');
  document.getElementById('btn-batch').style.display = 'none';
  document.getElementById('batch-msg').innerHTML = '';
  if(!q){ results.innerHTML = ''; return; }
  results.innerHTML = '<div class="empty">Suche…</div>';
  try{
    const r = await fetch('/api/cache/search?q=' + encodeURIComponent(q) + '&limit=20');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const data = await r.json();
    if(data.count === 0){
      results.innerHTML = '<div class="empty">Keine Treffer für "' + escapeHtml(q) + '"</div>';
      return;
    }
    lastQuery = q;
    lastCount = data.count;
    document.getElementById('btn-batch').style.display = 'inline-block';
    results.innerHTML = data.results.map((r,i) => {
      const stale = r.exists === false;
      const pathHtml = stale
        ? `<div class="r-path-stale" title="Datei nicht mehr im Vault — nur Volltext verfügbar">📄 ${escapeHtml(r.path)}<span class="stale-badge">veraltet</span></div>`
        : `<a class="r-path" href="/api/vault-file?path=${encodeURIComponent(r.path)}" target="_blank" rel="noopener" title="PDF in neuem Tab öffnen">📄 ${escapeHtml(r.path)}</a>`;
      const hint = stale ? '→ Text: Volltext (PDF-Datei fehlt)' : '→ Titel: PDF  ·  Text: Volltext';
      return `
      <div class="result${stale ? ' stale' : ''}">
        ${pathHtml}
        <div class="r-excerpt" data-idx="${i}" title="Klicken für Volltext">${escapeHtml(r.excerpt || '(kein Excerpt)')}</div>
        <div class="r-meta">
          <span class="r-score">Score ${r.score.toFixed(2)}</span>
          <span>Sprache: ${r.langs || '–'}</span>
          <span class="r-action">${hint}</span>
        </div>
      </div>`;
    }).join('');
    // Volltext-Handler auf Excerpts
    document.querySelectorAll('.r-excerpt').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx, 10);
        openFullText(data.results[idx]);
      });
    });
  }catch(e){
    results.innerHTML = '<div class="empty" style="color:var(--err)">Fehler: ' + escapeHtml(e.message) + '</div>';
  }
}

function clearResults(){
  document.getElementById('search-input').value = '';
  document.getElementById('results').innerHTML = '';
  document.getElementById('btn-batch').style.display = 'none';
  document.getElementById('batch-msg').innerHTML = '';
  lastQuery = '';
  lastCount = 0;
}

async function sendToBatch(){
  if(!lastQuery) return;
  const msg = document.getElementById('batch-msg');
  msg.innerHTML = '⏳ Speichere ' + lastCount + ' Treffer für Batch…';
  try{
    const r = await fetch('/api/cache/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({q: lastQuery, limit: 20})
    });
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || ('HTTP '+r.status));
    const url = '/batch?input=' + encodeURIComponent(data.container_path);
    msg.innerHTML = '✅ ' + data.count + ' Treffer als <code>' + escapeHtml(data.filename) +
                    '</code> gespeichert. <a href="' + url + '" style="color:var(--accent);font-weight:600">→ Lauf starten</a>';
  }catch(e){
    msg.innerHTML = '❌ Fehler: ' + escapeHtml(e.message);
  }
}

async function openFullText(r){
  const modal = document.getElementById('modal');
  const title = document.getElementById('modal-title');
  const body  = document.getElementById('modal-body');
  const meta  = document.getElementById('modal-meta');
  const openpdf = document.getElementById('modal-openpdf');
  title.textContent = r.path;
  body.textContent = 'Laden…';
  const staleFlag = r.exists === false ? ' · ⚠ PDF-Datei fehlt im Vault' : '';
  meta.textContent = `Score ${r.score.toFixed(2)} · Sprache: ${r.langs || '–'}${staleFlag}`;
  if(r.exists === false){
    openpdf.style.display = 'none';
  }else{
    openpdf.style.display = 'inline-block';
    openpdf.href = '/api/vault-file?path=' + encodeURIComponent(r.path);
  }
  modal.classList.add('open');
  try{
    const resp = await fetch('/api/cache/file?path=' + encodeURIComponent(r.path));
    if(!resp.ok) throw new Error('HTTP '+resp.status);
    const data = await resp.json();
    body.textContent = data.text || '(kein Text)';
  }catch(e){
    body.textContent = 'Fehler: ' + e.message;
  }
}

function closeModal(){
  document.getElementById('modal').classList.remove('open');
}

document.addEventListener('keydown', e => {
  if(e.key === 'Escape') closeModal();
});

async function reindex(){
  if(!confirm('Kompletten Index neu aufbauen? Dauert einige Sekunden.')) return;
  const st = document.getElementById('admin-status');
  st.innerHTML = '⏳ Neu-Indizierung läuft…';
  try{
    const r = await fetch('/api/cache/reindex', {method:'POST'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const data = await r.json();
    st.innerHTML = `✅ Fertig: ${data.indexed} indiziert, ${data.skipped} übersprungen in ${data.duration_seconds}s`;
    loadStats();
  }catch(e){
    st.innerHTML = '❌ Fehler: ' + escapeHtml(e.message);
  }
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

document.getElementById('search-input').addEventListener('keydown', e => {
  if(e.key === 'Enter') doSearch();
});

loadStats();
setInterval(loadStats, 30000);
</script>
</body>
</html>
"""


_BATCH_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling Workflow · Batch</title>
<style>
:root{--bg:#f4f5f7;--surface:#fff;--border:#dde1ea;--text:#1a1d2e;--muted:#6b7280;--ok:#059669;--warn:#d97706;--err:#dc2626;--accent:#4f46e5}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
header{border-bottom:1px solid var(--border);padding:13px 24px;display:flex;align-items:center;gap:10px;background:var(--surface);flex-wrap:wrap}
header h1{font-size:16px;font-weight:700;color:var(--accent);white-space:nowrap;margin-right:4px}
nav a{font-size:12px;padding:4px 12px;border:1px solid var(--border);border-radius:7px;color:var(--text);text-decoration:none;font-weight:600;white-space:nowrap;transition:all .15s;margin-right:5px}
nav a:hover,nav a.hl{border-color:var(--accent);color:var(--accent)}
.ts{font-size:12px;color:var(--muted);margin-left:auto}

.intro{margin:14px 24px;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 20px}
.intro h2{font-size:14px;margin-bottom:8px;color:var(--accent)}
.intro p{font-size:13px;line-height:1.55;color:var(--text);margin-bottom:6px}
.intro ul{font-size:13px;line-height:1.55;color:var(--text);padding-left:20px;margin-bottom:4px}
.intro code{background:#eef2ff;color:var(--accent);padding:1px 5px;border-radius:3px;font-size:12px}

.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;padding:4px 24px 18px 24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:700;margin-bottom:6px}
.stat .val{font-size:22px;font-weight:700;color:var(--text)}
.stat .sub{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.4}
.stat.ok .val{color:var(--ok)}
.stat.warn .val{color:var(--warn)}
.stat.err .val{color:var(--err)}

.section{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin:14px 24px;overflow:hidden}
.section h2{font-size:13px;font-weight:700;color:var(--text);padding:12px 18px;border-bottom:1px solid var(--border);background:#fafbfc;display:flex;align-items:center;gap:8px}
.section h2 .actions{margin-left:auto;display:flex;gap:8px}
.section .body{padding:16px 18px}
.section .hint{font-size:12px;color:var(--muted);line-height:1.55;margin-bottom:12px}
.section .hint code{background:#eef2ff;color:var(--accent);padding:1px 5px;border-radius:3px;font-size:11px}

.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-bottom:12px}
.form-grid label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.form-grid .help{font-size:11px;color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;line-height:1.4;margin-top:3px}
.form-grid input,.form-grid select{padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;font-family:inherit;outline:none;background:#fff;color:var(--text)}
.form-grid input:focus,.form-grid select:focus{border-color:var(--accent)}
button.primary{padding:9px 18px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer}
button.primary:hover{opacity:.9}
button.secondary{padding:7px 14px;background:#fff;color:var(--muted);border:1px solid var(--border);border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary:hover{border-color:var(--accent);color:var(--accent)}

table.runs,table.items{width:100%;border-collapse:collapse;font-size:12.5px}
table.runs th,table.items th{text-align:left;padding:8px 10px;font-weight:700;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border);background:#fafbfc;white-space:nowrap}
table.runs td,table.items td{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
table.runs tr:hover,table.items tr:hover{background:#fafbfc}
table.items .trunc{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
table.items .name{font-weight:600;color:var(--text)}
table.items tr.item-error td{background:#fef2f2}
table.items tr.item-error td.err-cell{color:var(--err)}

.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.badge.running{background:#eef2ff;color:var(--accent)}
.badge.paused{background:#fef3c7;color:var(--warn)}
.badge.done{background:#ecfdf5;color:var(--ok)}
.badge.aborted{background:#f3f4f6;color:var(--muted)}
.badge.error{background:#fee2e2;color:var(--err)}
.progress-wrap{background:#eef2ff;border-radius:3px;height:6px;overflow:hidden;min-width:80px}
.progress-bar{height:6px;background:var(--accent);transition:width .3s}

.modal-backdrop{position:fixed;inset:0;background:rgba(15,18,34,.5);display:none;align-items:center;justify-content:center;z-index:1000;padding:20px}
.modal-backdrop.open{display:flex}
.modal{background:#fff;border-radius:14px;max-width:1100px;width:100%;max-height:92vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3);overflow:hidden}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.modal-head h3{font-size:14px;font-weight:700;color:var(--text);flex:1;word-break:break-all}
.modal-head .close{cursor:pointer;font-size:22px;color:var(--muted);background:none;border:none;padding:0 8px}
.modal-body{padding:16px 20px;overflow-y:auto}
.modal-foot{padding:10px 20px;border-top:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.modal-foot .foot-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:700}
.modal-foot .spacer{flex:1}
.modal-foot .export-note{font-size:11px;color:var(--muted);font-style:italic}
.empty{color:var(--muted);font-size:12px;text-align:center;padding:24px}

.summary-box{background:#fafbfc;border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.summary-box .sk{display:flex;flex-direction:column;gap:2px}
.summary-box .sk-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:700}
.summary-box .sk-val{font-size:13px;color:var(--text);font-weight:600}
.summary-box .sk-sub{font-size:11px;color:var(--muted)}

.filter-row{display:flex;gap:6px;margin-bottom:10px;align-items:center;flex-wrap:wrap}
.filter-row .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:700}
.filter-row .chip{padding:4px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;cursor:pointer;background:#fff;color:var(--muted);font-weight:600}
.filter-row .chip.active{border-color:var(--accent);background:#eef2ff;color:var(--accent)}
.filter-row .count{font-size:11px;color:var(--muted);margin-left:auto}
</style>
</head>
<body>
<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
  <h1>Docling Workflow</h1>
  <nav>
    <a href="/pipeline" target="_blank" rel="noopener">⚡ Pipeline</a>
    <a href="/review" target="_blank" rel="noopener">📋 Review</a>
    <a href="/vault" target="_blank" rel="noopener">📁 Vault</a>
    <a href="/cache" target="_blank" rel="noopener">🔍 Cache</a>
    <a href="/batch" class="hl">🧰 Batch</a>
    <a href="/wilson" target="_blank" rel="noopener">🥧 Wilson</a>
  </nav>
  <span class="ts" id="ts">Laden…</span>
</header>

<div class="intro">
  <h2>🧰 Was ist das Batch-Dashboard?</h2>
  <p>
    Hier startest du <b>Auswertungsläufe über bestehende Dokumente</b> im Vault — ohne dass etwas im Archiv verschoben wird.
    Du gibst eine Liste von PDFs an (z. B. aus der <a href="/cache" target="_blank" rel="noopener">Cache-Suche</a>), wählst Quelle und Ausgabe,
    und der Dispatcher läuft durch den gesamten Klassifikationsprozess — aber nur zum Lesen, nicht zum Einsortieren.
  </p>
  <ul>
    <li><b>Beispiel:</b> „Alle Dokumente mit dem Wort <code>Gothaer</code> aus dem Jahr 2024 extrahieren und als Tabelle zeigen."</li>
    <li><b>OCR-Quelle</b> entscheidet, ob der bereits indizierte Text (Cache, schnell) genutzt wird oder Docling erneut rendert (langsam, aber frisch).</li>
    <li><b>Alle Ergebnisse landen in der Datenbank</b> und werden im Detail-Modal jedes Laufs angezeigt. CSV/JSONL-Download ist nur für externen Export (z. B. Steuerberater).</li>
  </ul>
</div>

<div class="stats" id="stats">
  <div class="stat">
    <div class="lbl">Aktive Läufe</div>
    <div class="val" id="stat-active">–</div>
    <div class="sub">Läufe im Zustand <code>running</code> oder <code>paused</code>. Steigt nach Start, fällt nach Abschluss.</div>
  </div>
  <div class="stat ok">
    <div class="lbl">Heute abgeschlossen</div>
    <div class="val" id="stat-done-today">–</div>
    <div class="sub">Anzahl Läufe mit Status <code>done</code>, die heute gestartet wurden.</div>
  </div>
  <div class="stat warn">
    <div class="lbl">Dokumente heute</div>
    <div class="val" id="stat-docs-today">–</div>
    <div class="sub">Summe aller Dokumente, die heute durch Läufe gelaufen sind (inkl. Fehler).</div>
  </div>
  <div class="stat err">
    <div class="lbl">Fehler heute</div>
    <div class="val" id="stat-errors-today">–</div>
    <div class="sub">Summe der Einzelfehler (OCR, LLM, Pipeline) über alle heutigen Läufe.</div>
  </div>
</div>

<div class="section">
  <h2>🚀 Neuen Lauf starten</h2>
  <div class="body">
    <div class="hint">
      Ein Batch-Lauf nimmt eine <b>Liste von PDFs</b> und lässt sie einzeln durch die Klassifikations-Pipeline laufen.
      Die Liste gibst du als Datei an, die <b>im Dispatcher-Container</b> liegen muss. Typischer Ablauf:
      1. Dokumente via Cache-Suche finden → Ergebnis als JSON speichern →
      2. Pfad hier eintragen → Start.
    </div>
    <div class="form-grid">
      <label>Input-Datei (Pfad im Container)
        <input id="f-input" placeholder="/data/dispatcher-temp/batch.json">
        <div class="help">JSON aus <code>/api/cache/search</code> oder Textdatei mit einer PDF pro Zeile. Muss aus Sicht des Containers erreichbar sein (z. B. unter <code>/data/dispatcher-temp/</code>).</div>
      </label>
      <label>OCR-Quelle
        <select id="f-ocr">
          <option value="hybrid" selected>hybrid (empfohlen)</option>
          <option value="cache">cache</option>
          <option value="docling">docling</option>
        </select>
        <div class="help"><b>hybrid:</b> Cache-Text, wenn lang genug und Sprache erkannt, sonst Docling. <b>cache:</b> nur Cache — schnell, aber bei Miss = Fehler. <b>docling:</b> immer neu rendern (teuer).</div>
      </label>
      <label>Ausgabe-Modus
        <select id="f-output">
          <option value="structured" selected>structured (DB + CSV/JSONL)</option>
          <option value="classify-only">classify-only (nur DB)</option>
          <option value="vault-move">vault-move (produktive Einsortierung)</option>
        </select>
        <div class="help"><b>structured/classify-only:</b> Auswertung — keine Dateien bewegt, kein Telegram. <b>vault-move:</b> echte Einsortierung wie im Watch-Modus (für diesen Fall besser die Watch-Inbox nutzen).</div>
      </label>
      <label>Ausgabe-Ordner (optional)
        <input id="f-outdir" placeholder="/data/dispatcher-temp/batch_runs">
        <div class="help">Nur bei <code>structured</code>: Ziel für die Export-Dateien. Leer lassen für Standard <code>/data/dispatcher-temp/batch_run_&lt;id&gt;/</code>.</div>
      </label>
      <label>Limit
        <input id="f-limit" type="number" value="0" min="0">
        <div class="help">Maximalzahl Dokumente. <code>0</code> = alle aus der Liste verarbeiten. Zum Probelauf z. B. <code>5</code>.</div>
      </label>
    </div>
    <button class="primary" onclick="startRun()">▶ Lauf starten</button>
    <span id="start-msg" style="margin-left:12px;font-size:12px;color:var(--muted)"></span>
  </div>
</div>

<div class="section">
  <h2>📜 Läufe
    <div class="actions"><button class="secondary" onclick="loadRuns()">🔄 Neu laden</button></div>
  </h2>
  <div class="body">
    <div class="hint">
      Jeder Lauf bekommt eine fortlaufende Nummer. Klick auf <b>Details</b> zeigt alle verarbeiteten Dokumente mit Kategorie, Absender, Betrag, Fehler.
      Während der Lauf aktiv ist, kannst du im Modal <b>Pause/Resume/Abort</b> drücken.
    </div>
    <table class="runs">
      <thead><tr>
        <th>#</th><th>Status</th><th>OCR</th><th>Ausgabe</th><th>Fortschritt</th>
        <th>Start</th><th>Ende</th><th>Input</th><th></th>
      </tr></thead>
      <tbody id="runs-tbody"><tr><td colspan="9" class="empty">Laden…</td></tr></tbody>
    </table>
  </div>
</div>

<div class="modal-backdrop" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-head"><h3 id="modal-title">Lauf …</h3><button class="close" onclick="closeModal()">×</button></div>
    <div class="modal-body" id="modal-body">Laden…</div>
    <div class="modal-foot">
      <span class="foot-label">Steuerung:</span>
      <button class="secondary" id="btn-pause" onclick="ctrl('pause')">⏸ Pause</button>
      <button class="secondary" id="btn-resume" onclick="ctrl('resume')">▶ Resume</button>
      <button class="secondary" id="btn-abort" onclick="ctrl('abort')">⏹ Abort</button>
      <span class="spacer"></span>
      <span class="foot-label">Externer Export:</span>
      <a id="dl-summary" class="secondary" href="#" target="_blank" rel="noopener">⬇ CSV</a>
      <a id="dl-details" class="secondary" href="#" target="_blank" rel="noopener">⬇ JSONL</a>
      <span class="export-note">(für Steuerberater o. ä. — Rohdaten sind oben in der Tabelle)</span>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (d) => d ? d.replace("T"," ").slice(0,16) : "–";
const esc = (s) => (s == null ? "" : String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"));
let currentRunId = null;
let currentFilter = "all";
let timer = null;

async function loadRuns(){
  const r = await fetch("/api/batch/runs"); const data = await r.json();
  $("ts").textContent = new Date().toLocaleTimeString("de-DE");
  const runs = data.runs || [];
  const today = new Date().toISOString().slice(0,10);
  let active=0,doneToday=0,docsToday=0,errsToday=0;
  for(const r of runs){
    if(r.status==='running'||r.status==='paused') active++;
    if((r.started_at||"").slice(0,10)===today){
      if(r.status==='done') doneToday++;
      docsToday += r.processed||0;
      errsToday += r.errors||0;
    }
  }
  $("stat-active").textContent = active;
  $("stat-done-today").textContent = doneToday;
  $("stat-docs-today").textContent = docsToday;
  $("stat-errors-today").textContent = errsToday;

  const tbody = $("runs-tbody");
  if(!runs.length){ tbody.innerHTML='<tr><td colspan="9" class="empty">Noch keine Läufe — starte oben einen neuen Lauf.</td></tr>'; return; }
  tbody.innerHTML = runs.map(r => {
    const pct = r.total>0 ? Math.round((r.processed||0)/r.total*100) : 0;
    return `<tr>
      <td><b>#${r.id}</b></td>
      <td><span class="badge ${r.status}">${r.status}</span></td>
      <td>${esc(r.ocr_mode)}</td>
      <td>${esc(r.output_mode)}</td>
      <td><div style="display:flex;align-items:center;gap:6px"><div class="progress-wrap"><div class="progress-bar" style="width:${pct}%"></div></div><span style="font-size:11px;color:var(--muted)">${r.processed||0}/${r.total||0}${r.errors?` · ${r.errors} Fehler`:''}</span></div></td>
      <td style="font-size:11px;color:var(--muted)">${fmt(r.started_at)}</td>
      <td style="font-size:11px;color:var(--muted)">${fmt(r.finished_at)}</td>
      <td style="font-size:11px;color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.input_source)}">${esc(r.input_source)}</td>
      <td><button class="secondary" onclick="openRun(${r.id})">Details</button></td>
    </tr>`;
  }).join("");
}

async function startRun(){
  const body = {
    input: $("f-input").value.trim(),
    ocr_mode: $("f-ocr").value,
    output_mode: $("f-output").value,
    output_dir: $("f-outdir").value.trim() || null,
    limit: parseInt($("f-limit").value||"0",10)
  };
  if(!body.input){ $("start-msg").textContent="⚠ Input-Pfad fehlt."; return; }
  $("start-msg").textContent = "Starte…";
  const r = await fetch("/api/batch/start", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const data = await r.json();
  if(!r.ok){ $("start-msg").textContent="⚠ "+(data.error||r.status); return; }
  $("start-msg").textContent = `✓ Lauf #${data.run_id} gestartet (${data.total} Dokumente in Warteschlange).`;
  loadRuns();
}

async function openRun(id){
  currentRunId = id;
  currentFilter = "all";
  $("modal-title").textContent = `Lauf #${id} — Details`;
  $("modal").classList.add("open");
  $("dl-summary").href = `/api/batch/runs/${id}/download?kind=summary`;
  $("dl-details").href = `/api/batch/runs/${id}/download?kind=details`;
  await refreshDetail();
}

function setFilter(f){ currentFilter = f; refreshDetail(); }

async function refreshDetail(){
  if(!currentRunId) return;
  const url = `/api/batch/runs/${currentRunId}` + (currentFilter && currentFilter!=="all" ? `?filter=${currentFilter}` : "");
  const r = await fetch(url);
  const d = await r.json();
  const run = d.run||{};
  const items = d.items||[];
  const pct = run.total>0 ? Math.round((run.processed||0)/run.total*100) : 0;

  const summary = `
    <div class="summary-box">
      <div class="sk"><span class="sk-lbl">Status</span><span class="sk-val"><span class="badge ${run.status}">${run.status}</span></span><span class="sk-sub">${run.finished_at ? 'fertig ' + fmt(run.finished_at) : 'gestartet ' + fmt(run.started_at)}</span></div>
      <div class="sk"><span class="sk-lbl">OCR-Quelle</span><span class="sk-val">${esc(run.ocr_mode)}</span><span class="sk-sub">Quelle für den Dokumenttext</span></div>
      <div class="sk"><span class="sk-lbl">Ausgabe</span><span class="sk-val">${esc(run.output_mode)}</span><span class="sk-sub">${run.output_mode==='vault-move' ? 'Datei wird verschoben' : 'nur Auswertung, nichts bewegt'}</span></div>
      <div class="sk"><span class="sk-lbl">Fortschritt</span><span class="sk-val">${run.processed||0} / ${run.total||0}</span><span class="sk-sub">${pct}%</span></div>
      <div class="sk"><span class="sk-lbl">Fehler</span><span class="sk-val" style="color:${run.errors?'var(--err)':'var(--ok)'}">${run.errors||0}</span><span class="sk-sub">${run.errors ? 'siehe Filter „nur Fehler"' : 'keine'}</span></div>
      <div class="sk"><span class="sk-lbl">Input</span><span class="sk-val" style="font-size:11px;word-break:break-all">${esc(run.input_source)}</span><span class="sk-sub">Quelle der Dokumentliste</span></div>
    </div>`;

  const filters = `
    <div class="filter-row">
      <span class="lbl">Anzeigen:</span>
      <span class="chip ${currentFilter==='all'?'active':''}" onclick="setFilter('all')">Alle (${run.total||0})</span>
      <span class="chip ${currentFilter==='done'?'active':''}" onclick="setFilter('done')">Nur erfolgreich (${(run.processed||0)-(run.errors||0)})</span>
      <span class="chip ${currentFilter==='error'?'active':''}" onclick="setFilter('error')">Nur Fehler (${run.errors||0})</span>
      <span class="count">${items.length} Zeile${items.length===1?'':'n'} angezeigt</span>
    </div>`;

  const rows = items.map(it => {
    const name = (it.doc_path||"").split("/").pop();
    const cls = it.status==='error' ? 'item-error' : '';
    return `<tr class="${cls}">
      <td class="name trunc" title="${esc(it.doc_path)}">${esc(name)}</td>
      <td><span class="badge ${it.status}">${it.status}</span></td>
      <td>${esc(it.ocr_source)}${it.ocr_chars?` <span style="color:var(--muted);font-size:11px">(${it.ocr_chars})</span>`:''}</td>
      <td>${esc(it.lang)}</td>
      <td>${esc(it.kategorie)}</td>
      <td>${esc(it.typ)}</td>
      <td>${esc(it.absender)}</td>
      <td>${esc(it.adressat)}</td>
      <td>${esc(it.rechnungsdatum)}</td>
      <td>${esc(it.rechnungsbetrag)}</td>
      <td>${esc(it.konfidenz)}</td>
      <td class="err-cell trunc" title="${esc(it.error||'')}">${esc(it.error||'')}</td>
    </tr>`;
  }).join("");

  const table = `<table class="items">
    <thead><tr>
      <th>Dokument</th><th>Status</th><th>OCR-Quelle</th><th>Sprache</th>
      <th>Kategorie</th><th>Typ</th><th>Absender</th><th>Adressat</th>
      <th>Datum</th><th>Betrag</th><th>Konfidenz</th><th>Fehler</th>
    </tr></thead>
    <tbody>${rows || '<tr><td colspan="12" class="empty">Keine Einträge für diesen Filter.</td></tr>'}</tbody>
  </table>`;

  $("modal-body").innerHTML = summary + filters + table;
}

function closeModal(){ $("modal").classList.remove("open"); currentRunId=null; }

async function ctrl(action){
  if(!currentRunId) return;
  await fetch(`/api/batch/runs/${currentRunId}/${action}`, {method:"POST"});
  await refreshDetail();
  loadRuns();
}

// Vorbelegung des Input-Pfads via ?input=...&autostart=1 (Sprung von /cache)
(function prefillFromQuery(){
  const qs = new URLSearchParams(window.location.search);
  const inp = qs.get('input');
  if(inp){
    const el = $("f-input");
    if(el){
      el.value = inp;
      el.style.background = '#fffbe6';
      $("start-msg").innerHTML = '↑ Pfad aus /cache übernommen — Parameter prüfen, dann <b>Lauf starten</b>.';
    }
  }
})();

loadRuns();
timer = setInterval(() => { loadRuns(); if(currentRunId) refreshDetail(); }, 5000);
</script>
</body>
</html>
"""

_DUPLIKATE_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling · Duplikate</title>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#7c6af7;--text:#e0e0e0;--muted:#888;--green:#4caf50;--orange:#ff9800;--red:#f44336;--blue:#5bc0de}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;min-height:100vh}
header{display:flex;align-items:center;gap:16px;padding:10px 18px;background:var(--card);border-bottom:1px solid var(--border);flex-wrap:wrap}
header h1{font-size:15px;font-weight:600;color:var(--accent)}
.links a{color:var(--muted);text-decoration:none;font-size:12px;margin-left:12px}
.links a:hover{color:var(--text)}
.main{max-width:1100px;margin:0 auto;padding:16px}
.summary-row{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 18px;min-width:140px;text-align:center}
.stat-card .num{font-size:28px;font-weight:700;color:var(--accent)}
.stat-card .lbl{font-size:11px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.actions{display:flex;gap:10px;margin-bottom:16px;align-items:center;flex-wrap:wrap}
button{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:12px;font-weight:600}
button:hover{opacity:.85}
button.secondary{background:var(--card);color:var(--text);border:1px solid var(--border)}
button.danger{background:#7c2020}
button:disabled{opacity:.4;cursor:not-allowed}
.tabs{display:flex;gap:2px;margin-bottom:12px;border-bottom:1px solid var(--border)}
.tab{padding:8px 16px;cursor:pointer;font-size:12px;font-weight:600;color:var(--muted);border-bottom:2px solid transparent;transition:all .15s}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.group-card{background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:10px;overflow:hidden}
.group-header{padding:10px 14px;display:flex;gap:10px;align-items:center;cursor:pointer;user-select:none}
.group-header:hover{background:#1e2140}
.group-header .badge{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}
.badge-byte{background:#1a2a3a;color:var(--blue)}
.badge-sem{background:#2a1a3a;color:#c792ea}
.badge-offen{background:#3a2a1a;color:var(--orange)}
.badge-verarbeitet{background:#1a3a1a;color:var(--green)}
.group-meta{font-size:11px;color:var(--muted);flex:1}
.group-body{padding:10px 14px;border-top:1px solid var(--border);display:none}
.group-body.open{display:block}
.entry-row{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px solid #1e2130}
.entry-row:last-child{border-bottom:none}
.entry-row .icon{font-size:16px;min-width:20px;margin-top:1px}
.entry-info{flex:1;min-width:0}
.entry-fname{font-size:12px;font-weight:500;word-break:break-all}
.entry-meta{font-size:11px;color:var(--muted);margin-top:2px}
.entry-orig{color:var(--green);font-size:10px;font-weight:700;text-transform:uppercase;margin-top:2px}
.entry-dup{color:var(--orange);font-size:10px}
.entry-moved{color:var(--muted);text-decoration:line-through;opacity:.5}
.move-btn{background:#3a2a1a;color:var(--orange);border:1px solid var(--orange);border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer;white-space:nowrap}
.move-btn:hover{background:#5a3a2a}
.move-btn:disabled{opacity:.4;cursor:not-allowed}
.move-all-btn{margin-top:10px;background:#3a1a1a;color:var(--red);border:1px solid var(--red);border-radius:5px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600}
.move-all-btn:hover{background:#5a2a2a}
#status-bar{padding:6px 12px;background:#1a2a1a;border:1px solid var(--green);border-radius:6px;color:var(--green);font-size:12px;display:none}
#status-bar.err{background:#2a1a1a;border-color:var(--red);color:var(--red)}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.empty-msg{padding:40px;text-align:center;color:var(--muted);font-size:14px}
.scan-progress{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:16px;display:none}
</style>
</head>
<body>
<header>
  <h1>&#127366; Duplikate</h1>
  <div class="links">
    <a href="/">Dashboard</a>
    <a href="/review">Review</a>
    <a href="/batch">Batch</a>
    <a href="/pipeline">Pipeline</a>
  </div>
</header>
<div class="main">
  <div id="scan-progress" class="scan-progress">
    <span class="spinner"></span> Scan läuft — bitte warten…
  </div>
  <div id="move-progress" class="scan-progress" style="display:none;background:#1a2a1a;border:1px solid var(--green)">
    <span class="spinner" style="border-top-color:var(--green)"></span>
    <span id="move-progress-text">Batch-Move läuft…</span>
  </div>
  <div id="status-bar"></div>
  <div class="summary-row">
    <div class="stat-card"><div class="num" id="s-total">—</div><div class="lbl">PDFs gescannt</div></div>
    <div class="stat-card"><div class="num" id="s-byte">—</div><div class="lbl">Byte-Duplikat-Gruppen</div></div>
    <div class="stat-card"><div class="num" id="s-sem">—</div><div class="lbl">Text-Duplikat-Gruppen</div></div>
    <div class="stat-card"><div class="num" id="s-ts">—</div><div class="lbl">Letzter Scan</div></div>
  </div>
  <div class="actions">
    <button id="scan-btn" onclick="startScan()">&#128270; Scan starten</button>
    <button id="move-all-btn" class="danger" onclick="confirmMoveAll()">&#128465; Alle Duplikate verschieben</button>
    <button class="secondary" onclick="loadGroups()">&#8635; Aktualisieren</button>
    <span id="scan-status" style="font-size:12px;color:var(--muted)"></span>
  </div>
  <div class="tabs">
    <div class="tab active" id="tab-byte" onclick="switchTab('byte')">Byte-Duplikate</div>
    <div class="tab" id="tab-sem" onclick="switchTab('sem')">Text-Duplikate</div>
  </div>
  <div id="groups-byte"></div>
  <div id="groups-sem" style="display:none"></div>
</div>
<script>
let currentTab = 'byte';
let allGroups = [];
let pollTimer = null;

function switchTab(t) {
  currentTab = t;
  document.getElementById('tab-byte').className = 'tab' + (t==='byte'?' active':'');
  document.getElementById('tab-sem').className = 'tab' + (t==='sem'?' active':'');
  document.getElementById('groups-byte').style.display = t==='byte'?'block':'none';
  document.getElementById('groups-sem').style.display = t==='sem'?'block':'none';
}

function showStatus(msg, isErr=false) {
  const bar = document.getElementById('status-bar');
  bar.textContent = msg;
  bar.className = isErr ? 'err' : '';
  bar.style.display = 'block';
  setTimeout(() => { bar.style.display='none'; }, 6000);
}

async function startScan() {
  document.getElementById('scan-btn').disabled = true;
  document.getElementById('scan-progress').style.display = 'block';
  document.getElementById('scan-status').textContent = 'Scan gestartet…';
  try {
    const r = await fetch('/api/duplikate/scan', {method:'POST'});
    if (!r.ok) { const e=await r.json(); showStatus('Fehler: '+e.error, true); return; }
    pollScanStatus();
  } catch(e) { showStatus('Netzwerkfehler: '+e, true); document.getElementById('scan-btn').disabled=false; }
}

function pollScanStatus() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/duplikate/status');
      const d = await r.json();
      if (d.status === 'done' || d.status === 'error' || !d.running) {
        clearInterval(pollTimer);
        document.getElementById('scan-progress').style.display = 'none';
        document.getElementById('scan-btn').disabled = false;
        document.getElementById('scan-status').textContent = '';
        if (d.status === 'error') showStatus('Scan-Fehler aufgetreten', true);
        else { showStatus('Scan abgeschlossen'); loadGroups(); }
      } else {
        document.getElementById('scan-status').textContent = 'Scannt…';
      }
    } catch(e) {}
  }, 2000);
}

async function loadGroups() {
  try {
    const r = await fetch('/api/duplikate/gruppen');
    const d = await r.json();
    allGroups = d.gruppen || [];
    renderStats(d);
    renderGroups();
  } catch(e) { showStatus('Fehler beim Laden: '+e, true); }
}

function renderStats(d) {
  document.getElementById('s-total').textContent = d.total_pdfs ?? '—';
  document.getElementById('s-byte').textContent = d.byte_gruppen ?? '—';
  document.getElementById('s-sem').textContent = d.sem_gruppen ?? '—';
  const ts = d.finished_at || d.started_at;
  document.getElementById('s-ts').textContent = ts ? ts.substring(5,16) : '—';
}

function pdfName(pfad) {
  return pfad ? pfad.split('/').pop() : '—';
}

function renderGroups() {
  const byteGrps = allGroups.filter(g => g.typ==='byte');
  const semGrps  = allGroups.filter(g => g.typ==='semantisch');
  renderGroupList('groups-byte', byteGrps, 'byte');
  renderGroupList('groups-sem', semGrps, 'sem');
}

function renderGroupList(containerId, groups, typ) {
  const el = document.getElementById(containerId);
  if (!groups.length) {
    el.innerHTML = '<div class="empty-msg">Keine ' + (typ==='byte'?'Byte-':'Text-') + 'Duplikate gefunden.</div>';
    return;
  }
  el.innerHTML = groups.map((g,gi) => {
    const btyp = typ==='byte' ? '<span class="badge badge-byte">BYTE</span>' : '<span class="badge badge-sem">TEXT</span>';
    const bstat = g.status==='verarbeitet' ? '<span class="badge badge-verarbeitet">erledigt</span>' : '<span class="badge badge-offen">offen</span>';
    const meta = [g.datum, g.absender].filter(Boolean).join(' · ') || '(unbekannt)';
    const entries = (g.eintraege || []).map(e => {
      const fname = pdfName(e.pdf_pfad);
      const moved = e.verschoben ? ' entry-moved' : '';
      const isOrig = e.ist_original;
      const label = isOrig
        ? '<div class="entry-orig">&#10003; Original (behalten)</div>'
        : (e.verschoben ? '<div class="entry-dup">&#8594; verschoben</div>' : '<div class="entry-dup">Duplikat</div>');
      const moveBtn = (!isOrig && !e.verschoben)
        ? `<button class="move-btn" onclick="moveEntry(${g.id},${e.id},this)">Verschieben</button>`
        : '';
      return `<div class="entry-row">
        <div class="icon">${isOrig ? '&#128196;' : '&#128464;'}</div>
        <div class="entry-info${moved}">
          <div class="entry-fname">${fname}</div>
          <div class="entry-meta">${e.md_pfad || ''}</div>
          ${label}
        </div>
        ${moveBtn}
      </div>`;
    }).join('');
    const openEntries = (g.eintraege||[]).filter(e=>!e.ist_original&&!e.verschoben);
    const moveAllBtn = openEntries.length > 0
      ? `<button class="move-all-btn" onclick="moveAllInGroup(${g.id},this)">Alle Duplikate dieser Gruppe verschieben (${openEntries.length})</button>`
      : '';
    return `<div class="group-card">
      <div class="group-header" onclick="toggleGroup(${gi})">
        ${btyp} ${bstat}
        <div class="group-meta">${meta} &nbsp;·&nbsp; ${(g.eintraege||[]).length} Dateien</div>
      </div>
      <div class="group-body" id="grp-${gi}">
        ${entries}
        ${moveAllBtn}
      </div>
    </div>`;
  }).join('');
}

function toggleGroup(gi) {
  const el = document.getElementById('grp-' + gi);
  if (el) el.classList.toggle('open');
}

async function moveEntry(gruppeId, eintragId, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const r = await fetch('/api/duplikate/move', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({gruppe_id: gruppeId, eintrag_id: eintragId})
    });
    const d = await r.json();
    if (d.ok) { showStatus('Verschoben: ' + (d.moved||[]).join(', ')); loadGroups(); }
    else { showStatus('Fehler: ' + d.error, true); btn.disabled=false; btn.textContent='Verschieben'; }
  } catch(e) { showStatus('Netzwerkfehler: '+e, true); btn.disabled=false; btn.textContent='Verschieben'; }
}

async function moveAllInGroup(gruppeId, btn) {
  btn.disabled = true;
  const grp = allGroups.find(g => g.id === gruppeId);
  if (!grp) return;
  const dupes = (grp.eintraege||[]).filter(e=>!e.ist_original&&!e.verschoben);
  for (const e of dupes) {
    try {
      const r = await fetch('/api/duplikate/move', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({gruppe_id: gruppeId, eintrag_id: e.id})
      });
      const d = await r.json();
      if (!d.ok) { showStatus('Fehler: ' + d.error, true); }
    } catch(ex) { showStatus('Netzwerkfehler', true); }
  }
  showStatus('Gruppe verarbeitet.');
  loadGroups();
}

let moveAllPollTimer = null;

function confirmMoveAll() {
  const count = allGroups.reduce((s, g) =>
    s + (g.eintraege||[]).filter(e => !e.ist_original && !e.verschoben).length, 0);
  if (count === 0) { showStatus('Keine offenen Duplikate vorhanden.'); return; }
  if (!confirm(`Alle ${count} Duplikate verschieben?\n\nOriginal-PDFs werden auf den kanonischen Namen umbenannt.\nDuplikate wandern in Anlagen/00 Duplikate/ bzw. Anlagen/00 Text-Duplikate/.\n\nDieser Vorgang kann nicht rückgängig gemacht werden.`)) return;
  startMoveAll();
}

async function startMoveAll() {
  document.getElementById('move-all-btn').disabled = true;
  document.getElementById('scan-btn').disabled = true;
  document.getElementById('move-progress').style.display = 'block';
  document.getElementById('move-progress-text').textContent = 'Batch-Move gestartet…';
  try {
    const r = await fetch('/api/duplikate/move-all', {method:'POST'});
    if (!r.ok) { const e=await r.json(); showStatus('Fehler: '+e.error, true); resetMoveBtn(); return; }
    pollMoveAll();
  } catch(e) { showStatus('Netzwerkfehler: '+e, true); resetMoveBtn(); }
}

function resetMoveBtn() {
  document.getElementById('move-all-btn').disabled = false;
  document.getElementById('scan-btn').disabled = false;
  document.getElementById('move-progress').style.display = 'none';
}

function pollMoveAll() {
  if (moveAllPollTimer) clearInterval(moveAllPollTimer);
  moveAllPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/duplikate/move-all/status');
      const d = await r.json();
      const pct = d.total > 0 ? Math.round(d.processed / d.total * 100) : 0;
      document.getElementById('move-progress-text').textContent =
        `Batch-Move: ${d.processed} / ${d.total} verschoben (${pct}%)` +
        (d.errors > 0 ? ` · ${d.errors} Fehler` : '');
      if (!d.running) {
        clearInterval(moveAllPollTimer);
        resetMoveBtn();
        const msg = `Fertig: ${d.processed} verschoben` + (d.errors > 0 ? `, ${d.errors} Fehler` : '');
        showStatus(msg, d.errors > 0);
        loadGroups();
      }
    } catch(e) {}
  }, 1500);
}

// Initial load
loadGroups();
// Check if scan or move-all is running
(async () => {
  const [scanR, moveR] = await Promise.all([
    fetch('/api/duplikate/status'),
    fetch('/api/duplikate/move-all/status'),
  ]);
  const scan = await scanR.json();
  const move = await moveR.json();
  if (scan.running) {
    document.getElementById('scan-progress').style.display='block';
    document.getElementById('scan-btn').disabled=true;
    pollScanStatus();
  }
  if (move.running) {
    document.getElementById('move-progress').style.display='block';
    document.getElementById('move-all-btn').disabled=true;
    document.getElementById('scan-btn').disabled=true;
    pollMoveAll();
  }
})();
</script>
</body>
</html>
"""

_FRONTMATTER_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling · Frontmatter</title>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#7c6af7;--text:#e0e0e0;--muted:#888;--green:#4caf50;--orange:#ff9800;--red:#f44336;--blue:#5bc0de}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;min-height:100vh}
header{display:flex;align-items:center;gap:16px;padding:10px 18px;background:var(--card);border-bottom:1px solid var(--border);flex-wrap:wrap}
header h1{font-size:15px;font-weight:600;color:var(--accent)}
.links a{color:var(--muted);text-decoration:none;font-size:12px;margin-left:12px}
.links a:hover{color:var(--text)}
.main{max-width:1000px;margin:0 auto;padding:16px}
.summary-row{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 18px;min-width:140px;text-align:center}
.stat-card .num{font-size:28px;font-weight:700;color:var(--accent)}
.stat-card .lbl{font-size:11px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.stat-card.green .num{color:var(--green)}
.stat-card.orange .num{color:var(--orange)}
.stat-card.red .num{color:var(--red)}
.section-title{font-size:13px;font-weight:600;color:var(--text);margin:20px 0 10px;letter-spacing:.02em}
.schema-table{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden;border:1px solid var(--border);margin-bottom:20px}
.schema-table th{text-align:left;padding:8px 12px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.schema-table td{padding:8px 12px;border-bottom:1px solid var(--border)}
.schema-table tr:last-child td{border-bottom:none}
.bar-wrap{width:140px;background:#1e2130;border-radius:4px;height:8px;display:inline-block;vertical-align:middle}
.bar-fill{height:8px;border-radius:4px;background:var(--accent)}
.probe-panel{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:20px}
.probe-panel h2{font-size:13px;font-weight:600;color:var(--accent);margin-bottom:12px}
.probe-row{display:flex;gap:8px;margin-bottom:12px;align-items:stretch}
.probe-row input{flex:1;background:#0f1117;border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:12px;font-family:monospace}
.probe-row input:focus{outline:none;border-color:var(--accent)}
button{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:12px;font-weight:600}
button:hover{opacity:.85}
button:disabled{opacity:.4;cursor:not-allowed}
button.danger{background:#7c2020;margin-left:8px}
button.secondary{background:var(--card);color:var(--text);border:1px solid var(--border)}
.diff-area{display:none;margin-top:12px}
.diff-area.visible{display:block}
.diff-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.diff-col h3{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
pre{background:#0f1117;border:1px solid var(--border);border-radius:6px;padding:10px;font-size:11px;overflow-x:auto;white-space:pre-wrap;color:var(--text);max-height:320px;overflow-y:auto}
.changes-list{margin:12px 0;padding-left:0;list-style:none}
.changes-list li{padding:3px 0;color:var(--green);font-size:12px;font-family:monospace}
.changes-list li::before{content:"+ ";color:var(--green)}
.no-changes{color:var(--muted);font-size:12px;padding:8px 0}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}
.badge-ok{background:#1a3a1a;color:var(--green)}
.badge-upg{background:#3a2a1a;color:var(--orange)}
.badge-no{background:#1e2130;color:var(--muted)}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
#msg{margin-top:10px;font-size:12px;padding:6px 10px;border-radius:5px;display:none}
#msg.ok{background:#1a3a1a;color:var(--green);display:block}
#msg.err{background:#3a1a1a;color:var(--red);display:block}
.batch-panel{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:20px}
.batch-panel h2{font-size:13px;font-weight:600;color:var(--accent);margin-bottom:8px}
.batch-desc{font-size:12px;color:var(--muted);margin-bottom:12px}
.progress-wrap{background:#1e2130;border-radius:6px;height:12px;margin:10px 0;overflow:hidden;display:none}
.progress-fill{height:12px;background:var(--green);border-radius:6px;transition:width .5s}
#batch-status{font-size:12px;color:var(--muted);margin-top:6px}
</style>
</head>
<body>
<header>
  <h1>🏷️ Frontmatter</h1>
  <div class="links">
    <a href="/">Dashboard</a>
    <a href="/vault">📁 Vault</a>
    <a href="/cache">🔍 Cache</a>
    <a href="/batch">🧰 Batch</a>
    <a href="/wilson">🥧 Wilson</a>
    <a href="/duplikate">🗂️ Duplikate</a>
  </div>
  <span id="scan-ts" style="margin-left:auto;font-size:11px;color:var(--muted)"></span>
</header>
<div class="main">
  <div class="summary-row" id="stats-row">
    <div class="stat-card"><div class="num" id="s-total">…</div><div class="lbl">MDs gesamt</div></div>
    <div class="stat-card green"><div class="num" id="s-unified">…</div><div class="lbl">Unified</div></div>
    <div class="stat-card orange"><div class="num" id="s-upg">…</div><div class="lbl">Upgradeable</div></div>
    <div class="stat-card red"><div class="num" id="s-nofm">…</div><div class="lbl">Kein Frontmatter</div></div>
    <div class="stat-card"><div class="num" id="s-pct" style="font-size:22px">…</div><div class="lbl">% Unified</div></div>
  </div>

  <div class="section-title">Schema-Verteilung</div>
  <table class="schema-table">
    <thead><tr><th>Schema</th><th>Anzahl</th><th>Anteil</th></tr></thead>
    <tbody id="schema-tbody"><tr><td colspan="3" style="color:var(--muted);text-align:center;padding:20px"><span class="spinner"></span> Lade…</td></tr></tbody>
  </table>

  <div class="batch-panel">
    <h2>Batch-Upgrade</h2>
    <p class="batch-desc">Fügt <code>erstellt_am</code> und <code>tags</code> zu allen noch nicht vereinheitlichten Dokumenten hinzu. Legacy-Felder bleiben erhalten. Einmaliger Vorgang — neue Dokumente bekommen das Schema automatisch.</p>
    <button id="batch-btn" onclick="startBatch()">Alle Dokumente upgraden</button>
    <div class="progress-wrap" id="progress-wrap">
      <div class="progress-fill" id="progress-fill" style="width:0%"></div>
    </div>
    <div id="batch-status"></div>
  </div>

  <div class="probe-panel">
    <h2>Probe-Upgrade</h2>
    <div class="probe-row">
      <input type="text" id="md-input" placeholder="z.B. 49 Krankenversicherung/2025/20250315_HUK-COBURG_Leistungsabrechnung.md">
      <button id="probe-btn" onclick="doProbe()">Prüfen</button>
      <button id="upgrade-btn" class="danger" onclick="doUpgrade()" style="display:none">Upgrade anwenden</button>
    </div>
    <div class="diff-area" id="diff-area">
      <p id="schema-badge" style="margin-bottom:8px;font-size:12px"></p>
      <ul class="changes-list" id="changes-list"></ul>
      <div class="diff-grid" id="diff-grid">
        <div><h3>Aktuell</h3><pre id="pre-current"></pre></div>
        <div><h3>Nach Upgrade</h3><pre id="pre-upgraded"></pre></div>
      </div>
    </div>
    <div id="msg"></div>
  </div>
</div>
<script>
async function loadStats() {
  const r = await fetch('/api/frontmatter/stats');
  const d = await r.json();
  if (d.error) { document.getElementById('schema-tbody').innerHTML = '<tr><td colspan="3" style="color:var(--red)">'+d.error+'</td></tr>'; return; }
  document.getElementById('s-total').textContent = d.total;
  document.getElementById('s-unified').textContent = d.unified;
  document.getElementById('s-upg').textContent = d.upgradeable;
  document.getElementById('s-nofm').textContent = d.no_frontmatter;
  document.getElementById('s-pct').textContent = d.unified_pct + '%';
  if (d.scanned_at) document.getElementById('scan-ts').textContent = 'Scan: ' + d.scanned_at;
  const tbody = document.getElementById('schema-tbody');
  tbody.innerHTML = '';
  for (const [name, cnt] of Object.entries(d.schemas)) {
    const pct = d.total ? Math.round(cnt / d.total * 100) : 0;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${name}</td><td style="font-weight:600">${cnt}</td>
      <td><span class="bar-wrap"><span class="bar-fill" style="width:${pct}%"></span></span>
      <span style="margin-left:8px;color:var(--muted);font-size:11px">${pct}%</span></td>`;
    tbody.appendChild(tr);
  }
}

let lastProbe = null;

async function doProbe() {
  const md = document.getElementById('md-input').value.trim();
  if (!md) return;
  document.getElementById('probe-btn').disabled = true;
  document.getElementById('upgrade-btn').style.display = 'none';
  document.getElementById('msg').className = '';
  const r = await fetch('/api/frontmatter/probe?md=' + encodeURIComponent(md));
  const d = await r.json();
  document.getElementById('probe-btn').disabled = false;
  lastProbe = d;
  const area = document.getElementById('diff-area');
  area.classList.add('visible');
  document.getElementById('schema-badge').innerHTML =
    'Schema: <strong>' + (d.schema||'?') + '</strong>';
  const cl = document.getElementById('changes-list');
  cl.innerHTML = '';
  if (d.error) { cl.innerHTML = '<li style="color:var(--red)">' + d.error + '</li>'; return; }
  if (d.changes && d.changes.length) {
    d.changes.forEach(c => { const li = document.createElement('li'); li.textContent = c; cl.appendChild(li); });
    document.getElementById('upgrade-btn').style.display = '';
  } else {
    cl.innerHTML = '<li class="no-changes" style="color:var(--muted)">Keine Änderungen notwendig — bereits unified.</li>';
  }
  document.getElementById('pre-current').textContent = d.current ? jsYaml(d.current) : '(kein Frontmatter)';
  document.getElementById('pre-upgraded').textContent = d.upgraded ? jsYaml(d.upgraded) : '(kein Frontmatter)';
}

async function doUpgrade() {
  const md = document.getElementById('md-input').value.trim();
  if (!md) return;
  document.getElementById('upgrade-btn').disabled = true;
  const r = await fetch('/api/frontmatter/upgrade?md=' + encodeURIComponent(md), {method:'POST'});
  const d = await r.json();
  document.getElementById('upgrade-btn').disabled = false;
  const msg = document.getElementById('msg');
  if (d.ok) {
    msg.className = 'ok';
    msg.textContent = 'Upgrade erfolgreich: ' + (d.changes||[]).join(' | ');
    document.getElementById('upgrade-btn').style.display = 'none';
    setTimeout(doProbe, 400);
  } else {
    msg.className = 'err';
    msg.textContent = 'Fehler: ' + (d.reason || 'Unbekannt');
  }
}

let batchPoll = null;

async function startBatch() {
  if (!confirm('Alle Vault-Dokumente upgraden? Legacy-Felder bleiben erhalten.')) return;
  document.getElementById('batch-btn').disabled = true;
  document.getElementById('batch-status').textContent = 'Starte…';
  document.getElementById('progress-wrap').style.display = 'block';
  await fetch('/api/frontmatter/batch-upgrade', {method:'POST'});
  batchPoll = setInterval(pollBatch, 1000);
}

async function pollBatch() {
  const r = await fetch('/api/frontmatter/batch-status');
  const d = await r.json();
  const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
  document.getElementById('progress-fill').style.width = pct + '%';
  document.getElementById('batch-status').textContent =
    `${d.done} / ${d.total} verarbeitet (${pct}%)${d.errors ? ' — ' + d.errors + ' Fehler' : ''}`;
  if (d.finished) {
    clearInterval(batchPoll);
    document.getElementById('batch-btn').disabled = false;
    document.getElementById('batch-status').textContent =
      `✅ Fertig — ${d.done} Dokumente verarbeitet${d.errors ? ', ' + d.errors + ' Fehler' : ''}.`;
    setTimeout(loadStats, 1000);
  }
}

function jsYaml(obj) {
  return Object.entries(obj).map(([k,v]) => {
    if (Array.isArray(v)) return k + ': [' + v.join(', ') + ']';
    if (v !== null && typeof v === 'object') return k + ': {...}';
    return k + ': ' + String(v);
  }).join('\n');
}

document.getElementById('md-input').addEventListener('keydown', e => { if(e.key==='Enter') doProbe(); });
loadStats();
</script>
</body>
</html>
"""


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

    def _html_response(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # GET / — Dashboard HTML
        if path in ("/", "/dashboard"):
            self._html_response(_DASHBOARD_HTML)
            return

        # GET /pipeline — Pipeline-Step-Dashboard
        elif path == "/pipeline":
            self._html_response(_PIPELINE_HTML)
            return

        # GET /review — Review Dashboard
        elif path == "/review":
            self._html_response(_REVIEW_HTML)
            return

        # GET /api/review/queue — Dokumente für Review (Inbox + niedrige Konfidenz)
        elif path == "/api/review/queue":
            mode = params.get("filter", ["inbox"])[0]
            with get_db() as con:
                if mode == "inbox":
                    rows = con.execute(
                        "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, "
                        "konfidenz, vault_pfad, anlagen_dateiname FROM dokumente "
                        "WHERE kategorie='Inbox' OR kategorie IS NULL OR kategorie='' "
                        "OR vault_pfad LIKE '00 Inbox%' ORDER BY id DESC"
                    ).fetchall()
                elif mode == "niedrig":
                    rows = con.execute(
                        "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, "
                        "konfidenz, vault_pfad, anlagen_dateiname FROM dokumente "
                        "WHERE konfidenz='niedrig' ORDER BY id DESC"
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, "
                        "konfidenz, vault_pfad, anlagen_dateiname FROM dokumente ORDER BY id DESC LIMIT 200"
                    ).fetchall()
            docs = []
            for r in rows:
                d = dict(r)
                d["pdf_name"] = (d.get("anlagen_dateiname")
                                 or (_safe_pdf_name_from_vault_pfad(d["vault_pfad"], d.get("dateiname", "")) if d.get("vault_pfad") else None)
                                 or d.get("dateiname", ""))
                docs.append(d)
            self._json_response(docs)
            return

        # GET /api/lernregeln — alle gespeicherten Lernregeln
        elif path == "/api/lernregeln":
            with get_db() as con:
                rows = con.execute("SELECT * FROM lernregeln ORDER BY id DESC").fetchall()
            self._json_response([dict(r) for r in rows])
            return

        # GET /vault — Vault-Struktur Dashboard
        elif path == "/vault":
            self._html_response(_VAULT_HTML)
            return

        # GET /cache — Cache-Reader Dashboard
        elif path == "/cache":
            self._html_response(_CACHE_HTML)
            return

        # GET /batch — Batch-Dashboard
        elif path == "/batch":
            self._html_response(_BATCH_HTML)
            return

        # GET /api/batch/runs — Liste aller Batch-Läufe
        elif path == "/api/batch/runs":
            try:
                with get_db() as con:
                    rows = con.execute(
                        "SELECT id, input_source, ocr_mode, output_mode, output_dir, "
                        "status, total, processed, errors, started_at, finished_at, created_at "
                        "FROM batch_runs ORDER BY id DESC LIMIT 100"
                    ).fetchall()
                self._json_response({"runs": [dict(r) for r in rows]})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # GET /api/batch/runs/<id> — Detail + alle Items (optional ?filter=error|done|all, ?limit=)
        elif path.startswith("/api/batch/runs/") and path.count("/") == 4 and not path.endswith("/download"):
            try:
                run_id = int(path.split("/")[4])
            except (ValueError, IndexError):
                self._json_response({"error": "Ungültige run_id"}, 400)
                return
            filter_ = params.get("filter", ["all"])[0]
            try:
                limit_ = int(params.get("limit", ["0"])[0])
            except ValueError:
                limit_ = 0
            try:
                with get_db() as con:
                    run = con.execute("SELECT * FROM batch_runs WHERE id=?", (run_id,)).fetchone()
                    if not run:
                        self._json_response({"error": "run_id nicht gefunden"}, 404)
                        return
                    base_sql = (
                        "SELECT doc_path, status, ocr_source, ocr_chars, lang, kategorie, typ, "
                        "absender, adressat, rechnungsdatum, rechnungsbetrag, konfidenz, "
                        "error, processed_at "
                        "FROM batch_items WHERE run_id=?"
                    )
                    sql_args: list = [run_id]
                    if filter_ in ("error", "done"):
                        base_sql += " AND status=?"
                        sql_args.append(filter_)
                    base_sql += " ORDER BY id ASC"
                    if limit_ > 0:
                        base_sql += " LIMIT ?"
                        sql_args.append(limit_)
                    items = con.execute(base_sql, sql_args).fetchall()
                self._json_response({
                    "run": dict(run),
                    "items": [dict(i) for i in items],
                    "filter": filter_,
                })
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # GET /api/batch/runs/<id>/download?kind=summary|details
        elif path.startswith("/api/batch/runs/") and path.endswith("/download"):
            try:
                run_id = int(path.split("/")[4])
            except (ValueError, IndexError):
                self._json_response({"error": "Ungültige run_id"}, 400)
                return
            kind = params.get("kind", ["summary"])[0]
            with get_db() as con:
                row = con.execute(
                    "SELECT output_dir FROM batch_runs WHERE id=?", (run_id,)
                ).fetchone()
            if not row or not row["output_dir"]:
                # Fallback: Standard-Ort
                target_dir = TEMP_DIR / f"batch_run_{run_id}"
            else:
                target_dir = Path(row["output_dir"])
            suffix = "_summary.csv" if kind == "summary" else "_details.jsonl"
            file_path_ = target_dir / f"run_{run_id}{suffix}"
            if not file_path_.exists():
                self._json_response({"error": f"Datei nicht gefunden: {file_path_}"}, 404)
                return
            data = file_path_.read_bytes()
            ct = "text/csv; charset=utf-8" if kind == "summary" else "application/x-ndjson; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Disposition", f'attachment; filename="{file_path_.name}"')
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
            return

        # GET /api/logs?q=<substring>&limit=200&since=<unix_ts>
        # Liefert Einträge aus dem In-Memory-Ringbuffer. q = Substring-Filter (z.B. Dokumentname).
        elif path == "/api/logs":
            q = params.get("q", [""])[0]
            try:
                limit = int(params.get("limit", ["200"])[0])
            except ValueError:
                limit = 200
            try:
                since = float(params.get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            snap = list(LOG_BUFFER)
            if since > 0:
                snap = [e for e in snap if e["t"] > since]
            if q:
                ql = q.lower()
                snap = [e for e in snap if ql in e["msg"].lower()]
            snap = snap[-limit:]
            self._json_response({"count": len(snap), "entries": snap})
            return

        # GET /api/queue/state — wartende Dokumente im file_queue
        elif path == "/api/queue/state":
            try:
                items = list(file_queue.queue)
            except Exception:
                items = []
            out = []
            for it in items:
                if isinstance(it, tuple) and len(it) == 2:
                    kind, p = it
                    out.append({"kind": str(kind), "name": Path(str(p)).name})
                else:
                    out.append({"kind": "pdf", "name": Path(str(it)).name})
            self._json_response({"waiting": len(out), "items": out})
            return

        # GET /api/cache/stats — Proxy zu cache-reader:8501/stats
        elif path == "/api/cache/stats":
            try:
                r = requests.get(f"{CACHE_READER_URL}/stats", timeout=5)
                self._json_response(r.json(), r.status_code)
            except Exception as e:
                self._json_response({"error": str(e)}, 502)
            return

        # GET /api/cache/search — Proxy zu cache-reader:8501/search + Existenz-Check
        elif path == "/api/cache/search":
            try:
                r = requests.get(
                    f"{CACHE_READER_URL}/search",
                    params={
                        "q": params.get("q", [""])[0],
                        "limit": params.get("limit", ["10"])[0],
                    },
                    timeout=10,
                )
                data = r.json()
                # Jeden Treffer prüfen: existiert die Datei noch im Vault?
                if r.status_code == 200 and VAULT_ROOT and "results" in data:
                    for entry in data["results"]:
                        p = entry.get("path", "")
                        entry["exists"] = bool(p) and (VAULT_ROOT / p).is_file()
                self._json_response(data, r.status_code)
            except Exception as e:
                self._json_response({"error": str(e)}, 502)
            return

        # GET /api/cache/file — Proxy zu cache-reader:8501/file
        elif path == "/api/cache/file":
            try:
                r = requests.get(
                    f"{CACHE_READER_URL}/file",
                    params={"path": params.get("path", [""])[0]},
                    timeout=10,
                )
                self._json_response(r.json(), r.status_code)
            except Exception as e:
                self._json_response({"error": str(e)}, 502)
            return

        # GET /api/vault/stats — Vault-Ordner-Statistik (alle Dateitypen)
        elif path == "/api/vault/stats":
            from collections import defaultdict
            EXT_MAP = {
                ".md":   "md",
                ".pdf":  "pdf",
                ".png":  "bild", ".jpg": "bild", ".jpeg": "bild",
                ".gif":  "bild", ".tiff": "bild", ".tif": "bild",
                ".heic": "bild", ".webp": "bild", ".JPG": "bild",
                ".docx": "office", ".doc": "office",
                ".xlsx": "office", ".xls": "office",
                ".pptx": "office", ".ppt": "office",
            }
            COLS = ["md", "pdf", "bild", "office"]
            result = []
            if VAULT_ROOT and VAULT_ROOT.exists():
                totals: dict = defaultdict(
                    lambda: {"md":0,"pdf":0,"bild":0,"office":0,"sub":defaultdict(lambda:{"md":0,"pdf":0,"bild":0,"office":0})}
                )
                for f in VAULT_ROOT.rglob("*"):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(VAULT_ROOT)
                    parts = rel.parts
                    if any(p.startswith(".") or p.endswith(".resources") for p in parts):
                        continue
                    if len(parts) == 1:
                        continue
                    typ = EXT_MAP.get(f.suffix)
                    if not typ:
                        continue
                    top = parts[0]
                    sub = parts[1] if len(parts) > 2 else None
                    totals[top][typ] += 1
                    if sub:
                        totals[top]["sub"][sub][typ] += 1
                for folder in sorted(totals.keys()):
                    d = totals[folder]
                    total_row = sum(d[c] for c in COLS)
                    if total_row == 0:
                        continue
                    subs_raw = sorted(d["sub"].items(), key=lambda x: -sum(x[1].values()))
                    result.append({
                        "folder": folder,
                        "md": d["md"], "pdf": d["pdf"],
                        "bild": d["bild"], "office": d["office"],
                        "total": total_row,
                        "sub": [{"name": k, "md": v["md"], "pdf": v["pdf"],
                                 "bild": v["bild"], "office": v["office"],
                                 "total": sum(v.values())} for k, v in subs_raw],
                    })
            self._json_response({"folders": result, "cols": COLS})
            return

        # GET /wilson — Wilson/OpenClaw Dashboard
        elif path == "/wilson":
            self._html_response(_WILSON_HTML)
            return

        # GET /api/wilson/status — Wilson Status via SSH
        elif path == "/api/wilson/status":
            self._json_response(_collect_wilson_status())
            return

        # GET /api/wilson/logs?lines=N — OpenClaw-Log des aktuellen Tages
        elif path == "/api/wilson/logs":
            try:
                n = int(params.get("lines", ["200"])[0])
            except ValueError:
                n = 200
            self._json_response(_collect_wilson_logs(n))
            return

        # GET /api/wilson/tui-info — Host/Port/Creds der ttyd-TUI
        elif path == "/api/wilson/tui-info":
            self._json_response(_fetch_wilson_tui_info())
            return

        # GET /api/health — aggregierter Service-Status
        elif path == "/api/health":
            self._json_response(_collect_health())
            return

        # GET /api/events — Server-Sent Events stream
        elif path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            client_q: queue.Queue = queue.Queue(maxsize=50)
            with _sse_lock:
                _sse_clients.append(client_q)
            # Sofort Ping senden damit der Browser die Verbindung bestätigt
            try:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
            except Exception:
                with _sse_lock:
                    if client_q in _sse_clients:
                        _sse_clients.remove(client_q)
                return
            try:
                while True:
                    try:
                        msg = client_q.get(timeout=25)
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except queue.Empty:
                        # Keep-alive comment
                        self.wfile.write(b": ka\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if client_q in _sse_clients:
                        _sse_clients.remove(client_q)
            return

        # GET /api/categories — alle Kategorien + Typen
        elif path == "/api/categories":
            cats = load_categories()
            result = {}
            for cat_id, cat in cats.items():
                result[cat_id] = {
                    "label": cat.get("label", cat_id),
                    "vault_folder": cat.get("vault_folder", "00 Inbox"),
                    "types": [{"id": t["id"], "label": t["label"]} for t in cat.get("types", [])],
                }
            self._json_response(result)

        # GET /api/recent — letzte Dokumente (mit optionalen Filtern)
        elif path == "/api/recent":
            limit  = int(params.get("limit",  [50])[0])
            q      = params.get("q",        [""])[0].strip()
            kat    = params.get("kategorie", [""])[0].strip()
            typ    = params.get("typ",       [""])[0].strip()
            adr    = params.get("adressat",  [""])[0].strip()
            von    = params.get("von",       [""])[0].strip()
            bis    = params.get("bis",       [""])[0].strip()
            konfid = params.get("konfidenz", [""])[0].strip()

            where, args = [], []
            if q:
                where.append("(dateiname LIKE ? OR absender LIKE ?)")
                args += [f"%{q}%", f"%{q}%"]
            if kat:
                where.append("kategorie = ?"); args.append(kat)
            if typ:
                where.append("typ = ?"); args.append(typ)
            if adr:
                where.append("adressat = ?");  args.append(adr)
            if konfid:
                where.append("konfidenz = ?"); args.append(konfid)
            if von:
                where.append("rechnungsdatum >= ?"); args.append(von)
            if bis:
                where.append("rechnungsdatum <= ?"); args.append(bis)

            sql = ("SELECT id, dateiname, rechnungsdatum, kategorie, typ, "
                   "absender, adressat, konfidenz, vault_pfad, anlagen_dateiname, erstellt_am "
                   "FROM dokumente")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit)

            with get_db() as con:
                rows = con.execute(sql, args).fetchall()
            docs = []
            for r in rows:
                d = dict(r)
                # anlagen_dateiname bevorzugen; Fallback: vault_pfad-Stem oder dateiname
                d["pdf_name"] = (d.get("anlagen_dateiname")
                                 or (_safe_pdf_name_from_vault_pfad(d["vault_pfad"], d.get("dateiname", "")) if d.get("vault_pfad") else None)
                                 or d.get("dateiname", ""))
                docs.append(d)
            self._json_response(docs)

        # GET /api/document/<id> — Dokument-Details + MD-Inhalt
        elif path.startswith("/api/document/"):
            try:
                doc_id = int(path.split("/")[-1])
            except ValueError:
                self._json_response({"error": "Ungültige ID"}, 400); return
            with get_db() as con:
                row = con.execute(
                    "SELECT id, dateiname, rechnungsdatum, kategorie, typ, absender, adressat, konfidenz, vault_pfad, anlagen_dateiname, erstellt_am "
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

        # GET /api/pipeline/current — letzter Dokument-Stand + Rescan-Status + Input-Ordner
        elif path == "/api/pipeline/current":
            with get_db() as con:
                latest = con.execute("""
                    SELECT dateiname FROM pipeline_steps
                    GROUP BY dateiname ORDER BY MAX(id) DESC LIMIT 1
                """).fetchone()
                steps = []
                if latest:
                    steps = con.execute("""
                        SELECT step_id, label, status, duration_ms, ts
                        FROM pipeline_steps WHERE dateiname=?
                        ORDER BY id
                    """, (latest["dateiname"],)).fetchall()
            # Input-Ordner: PDFs die noch nicht verarbeitet wurden
            input_pdfs = []
            if WATCH_DIR and WATCH_DIR.exists():
                input_pdfs = [p.name for p in WATCH_DIR.rglob("*.pdf")
                              if not p.name.startswith("._")]
            # Stale check: letzter Step älter als 30min UND kein Rescan aktiv
            is_stale = False
            if latest and not _rescan_state["active"]:
                last_ts_str = steps[-1]["ts"] if steps else None
                if last_ts_str:
                    try:
                        last_ts = datetime.fromisoformat(last_ts_str.replace(" ", "T"))
                        is_stale = (datetime.now() - last_ts).total_seconds() > 1800
                    except Exception:
                        is_stale = True
            self._json_response({
                "dateiname":    latest["dateiname"] if latest and not is_stale else None,
                "steps":        [dict(s) for s in steps] if not is_stale else [],
                "rescan":       dict(_rescan_state),
                "input_count":  len(input_pdfs),
                "input_files":  input_pdfs[:10],
                "is_idle":      not _rescan_state["active"],
                "is_stale":     is_stale,
            })
            return

        # GET /api/pipeline/content?dateiname=X — MD-Inhalt aus Vault lesen
        elif path == "/api/pipeline/content":
            dateiname = params.get("dateiname", [None])[0]
            if not dateiname:
                self._json_response({"error": "dateiname fehlt"}, 400); return
            with get_db() as con:
                row = con.execute(
                    "SELECT vault_pfad FROM dokumente WHERE dateiname=?", (dateiname,)
                ).fetchone()
            if not row or not row["vault_pfad"] or not VAULT_ROOT:
                self._json_response({"error": "Dokument noch nicht im Vault"}, 404); return
            md_path = VAULT_ROOT / row["vault_pfad"]
            if not md_path.exists():
                self._json_response({"error": f"Datei nicht gefunden: {row['vault_pfad']}"}, 404); return
            content = md_path.read_text(encoding="utf-8", errors="replace")
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # GET /api/pipeline/stats — Durchschnittliche Schrittdauern
        elif path == "/api/pipeline/stats":
            doc_limit = int(params.get("limit", [30])[0])   # Zeilen im Modal
            with get_db() as con:
                # Aggregat nur über den jeweils letzten Run pro Datei
                agg = con.execute("""
                    WITH last_start AS (
                        SELECT dateiname, MAX(id) AS sid
                        FROM pipeline_steps WHERE step_id='started'
                        GROUP BY dateiname
                    )
                    SELECT ps.step_id, ps.label,
                           COUNT(*)                                         AS runs,
                           ROUND(AVG(ps.duration_ms))                      AS avg_ms,
                           ROUND(MIN(ps.duration_ms))                      AS min_ms,
                           ROUND(MAX(ps.duration_ms))                      AS max_ms,
                           SUM(CASE WHEN ps.status='error' THEN 1 ELSE 0 END) AS errors
                    FROM pipeline_steps ps
                    JOIN last_start ls ON ps.dateiname = ls.dateiname AND ps.id >= ls.sid
                    WHERE ps.status IN ('done','error')
                    GROUP BY ps.step_id
                    ORDER BY MIN(ps.rowid)
                """).fetchall()
                # Alle Dateien — letzten Run je Datei ermitteln, Geister filtern
                _all_files = con.execute("""
                    WITH last_start AS (
                        SELECT dateiname, MAX(id) AS sid
                        FROM pipeline_steps WHERE step_id='started'
                        GROUP BY dateiname
                    )
                    SELECT dateiname, sid FROM last_start ORDER BY sid DESC
                """).fetchall()
                _ghost_re = re.compile(r'_\d+\.pdf$', re.IGNORECASE)
                real_files = [r for r in _all_files
                              if not _ghost_re.search(r["dateiname"])]
                file_sids = {r["dateiname"]: r["sid"] for r in real_files}

                # Alle Steps laden (nur letzter Run je Datei)
                all_steps_raw = con.execute(
                    "SELECT dateiname, step_id, status, duration_ms, ts, id "
                    "FROM pipeline_steps ORDER BY id"
                ).fetchall()

            # Gruppieren: nur Steps ab sid
            last_run: dict = {}
            for s in all_steps_raw:
                fn = s["dateiname"]
                sid = file_sids.get(fn)
                if sid is None:
                    continue
                if s["id"] >= sid:
                    last_run.setdefault(fn, []).append({
                        "step_id":     s["step_id"],
                        "status":      s["status"],
                        "duration_ms": s["duration_ms"],
                        "ts":          s["ts"],
                    })

            # Summenzähler (alle Dateien)
            total_done  = sum(1 for fn, steps in last_run.items()
                              if any(s["step_id"]=="vault" and s["status"]=="done" for s in steps))
            total_err   = sum(1 for fn, steps in last_run.items()
                              if any(s["status"]=="error" for s in steps))
            total_open  = sum(1 for fn, steps in last_run.items()
                              if not any(s["step_id"]=="vault" and s["status"]=="done" for s in steps)
                              and not any(s["status"]=="error" for s in steps))

            # Für Modal: je Kategorie die letzten doc_limit Einträge
            def _pick(fn_list, limit):
                return [{"dateiname": fn, "steps": last_run[fn]}
                        for fn in fn_list[:limit]]

            done_fns  = [fn for fn in (r["dateiname"] for r in real_files)
                         if fn in last_run and any(s["step_id"]=="vault" and s["status"]=="done"
                                                   for s in last_run[fn])]
            err_fns   = [fn for fn in (r["dateiname"] for r in real_files)
                         if fn in last_run and any(s["status"]=="error" for s in last_run[fn])]
            open_fns  = [fn for fn in (r["dateiname"] for r in real_files)
                         if fn in last_run
                         and not any(s["step_id"]=="vault" and s["status"]=="done" for s in last_run[fn])
                         and not any(s["status"]=="error" for s in last_run[fn])]

            self._json_response({
                "aggregates":  [dict(r) for r in agg],
                "counts":      {"done": total_done, "error": total_err, "open": total_open,
                                "ghosts": len(_all_files) - len(real_files)},
                "documents":   _pick(done_fns, doc_limit) +
                               _pick(err_fns,  doc_limit) +
                               _pick(open_fns, doc_limit),
                "done_docs":   _pick(done_fns, doc_limit),
                "err_docs":    _pick(err_fns,  doc_limit),
                "open_docs":   _pick(open_fns, doc_limit),
            })
            return

        # GET /api/pdf/<dateiname> — PDF aus Anlagen ausliefern (rekursiv)
        # GET /api/vault-pdf?md=<vault-relativer-md-pfad>
        # Liest das MD, extrahiert den original:-Link und liefert das PDF als Download.
        elif path == "/api/vault-pdf":
            md_rel = params.get("md", [""])[0]
            if not md_rel or ".." in md_rel or md_rel.startswith("/"):
                self._json_response({"error": "Ungültiger MD-Pfad"}, 400); return
            if not VAULT_ROOT:
                self._json_response({"error": "VAULT_ROOT nicht konfiguriert"}, 500); return
            md_full = VAULT_ROOT / md_rel
            if not md_full.exists() or md_full.suffix.lower() != ".md":
                self._json_response({"error": f"MD nicht gefunden: {md_rel}"}, 404); return
            try:
                md_full.resolve().relative_to(VAULT_ROOT.resolve())
            except ValueError:
                self._json_response({"error": "Path traversal blockiert"}, 400); return
            content = md_full.read_text(encoding="utf-8", errors="replace")
            # original: "[[Anlagen/DATEI.pdf]]"  oder  original: [[Anlagen/DATEI.pdf]]
            import re as _re
            m = _re.search(r'\[\[([^\]]+\.pdf)\]\]', content)
            if not m:
                self._json_response({"error": "Kein PDF-Link (original:) im MD gefunden"}, 404); return
            pdf_rel = m.group(1)
            if ".." in pdf_rel or pdf_rel.startswith("/"):
                self._json_response({"error": "Ungültiger PDF-Pfad im MD"}, 400); return
            pdf_full = VAULT_ROOT / pdf_rel
            if not pdf_full.exists() or not pdf_full.is_file():
                self._json_response({"error": f"PDF nicht gefunden: {pdf_rel}"}, 404); return
            try:
                pdf_full.resolve().relative_to(VAULT_ROOT.resolve())
            except ValueError:
                self._json_response({"error": "Path traversal blockiert"}, 400); return
            data = pdf_full.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", len(data))
            self.send_header("Content-Disposition", f'attachment; filename="{pdf_full.name}"')
            self.end_headers()
            self.wfile.write(data)
            return

        # GET /api/vault-file?path=... — Liefert jede Vault-Datei (PDF oder andere) als Stream
        elif path == "/api/vault-file":
            rel = params.get("path", [""])[0]
            if not rel or ".." in rel or rel.startswith("/"):
                self._json_response({"error": "Ungültiger Pfad"}, 400); return
            if not VAULT_ROOT:
                self._json_response({"error": "VAULT_ROOT nicht konfiguriert"}, 500); return
            full = VAULT_ROOT / rel
            if not full.exists() or not full.is_file():
                self._json_response({"error": f"Datei nicht gefunden: {rel}"}, 404); return
            try:
                full.resolve().relative_to(VAULT_ROOT.resolve())
            except ValueError:
                self._json_response({"error": "Path traversal blockiert"}, 400); return
            mime = "application/pdf" if full.suffix.lower() == ".pdf" else "application/octet-stream"
            data = full.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(data))
            self.send_header("Content-Disposition", f'inline; filename="{full.name}"')
            self.end_headers()
            self.wfile.write(data)
            return

        elif path.startswith("/api/pdf/"):
            from urllib.parse import unquote
            filename = unquote(path[len("/api/pdf/"):])
            # Sicherheit: kein Path-Traversal
            if ".." in filename or "\\" in filename:
                self._json_response({"error": "Ungültiger Dateiname"}, 400); return
            # Direkter Pfad
            base = VAULT_PDF_ARCHIV or (VAULT_ROOT / "Anlagen" if VAULT_ROOT else None)
            pdf_path = None
            if base:
                candidate = base / filename
                if candidate.exists() and candidate.suffix.lower() == ".pdf":
                    pdf_path = candidate
                else:
                    # Rekursiv suchen (Unterordner)
                    hits = list(base.rglob(Path(filename).name))
                    pdf_path = next((h for h in hits if h.suffix.lower() == ".pdf"), None)
            if not pdf_path:
                self._json_response({"error": "PDF nicht gefunden"}, 404); return
            data = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", len(data))
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        # GET /duplikate — Duplikat-Dashboard
        elif path == "/duplikate":
            self._html_response(_DUPLIKATE_HTML)
            return

        # GET /api/duplikate/status — Scan-Status
        elif path == "/api/duplikate/status":
            with _DEDUP_SCAN_LOCK:
                running = _DEDUP_SCAN_STATUS["running"]
                scan_id = _DEDUP_SCAN_STATUS["scan_id"]
            scan = None
            if scan_id is not None:
                with get_db() as con:
                    row = con.execute(
                        "SELECT id, status, total_pdfs, byte_gruppen, sem_gruppen, started_at, finished_at "
                        "FROM duplikat_scans WHERE id=?", (scan_id,)
                    ).fetchone()
                    if row:
                        scan = dict(row)
            if scan:
                scan["running"] = running
                self._json_response(scan)
            else:
                # Return last scan from DB if any
                with get_db() as con:
                    row = con.execute(
                        "SELECT id, status, total_pdfs, byte_gruppen, sem_gruppen, started_at, finished_at "
                        "FROM duplikat_scans ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                if row:
                    d = dict(row)
                    d["running"] = running
                    self._json_response(d)
                else:
                    self._json_response({"running": False, "status": None})
            return

        # GET /api/duplikate/move-all/status — Batch-Move-Fortschritt
        elif path == "/api/duplikate/move-all/status":
            with _DEDUP_MOVE_LOCK:
                self._json_response(dict(_DEDUP_MOVE_STATUS))
            return

        # GET /api/duplikate/gruppen — alle Duplikat-Gruppen + Einträge
        elif path == "/api/duplikate/gruppen":
            with get_db() as con:
                scan_row = con.execute(
                    "SELECT id, status, total_pdfs, byte_gruppen, sem_gruppen, started_at, finished_at "
                    "FROM duplikat_scans ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if not scan_row:
                    self._json_response({"gruppen": [], "total_pdfs": 0, "byte_gruppen": 0, "sem_gruppen": 0})
                    return
                scan = dict(scan_row)
                gruppen_rows = con.execute(
                    "SELECT id, typ, pdf_hash, datum, absender, status "
                    "FROM duplikat_gruppen WHERE scan_id=? ORDER BY typ, id",
                    (scan["id"],)
                ).fetchall()
                result = []
                for g in gruppen_rows:
                    gd = dict(g)
                    eintraege = con.execute(
                        "SELECT id, pdf_pfad, md_pfad, ist_original, verschoben "
                        "FROM duplikat_eintraege WHERE gruppe_id=? ORDER BY ist_original DESC, id",
                        (g["id"],)
                    ).fetchall()
                    gd["eintraege"] = [dict(e) for e in eintraege]
                    result.append(gd)
            scan["gruppen"] = result
            self._json_response(scan)
            return

        # GET /frontmatter — Frontmatter-Dashboard
        elif path == "/frontmatter":
            self._html_response(_FRONTMATTER_HTML)
            return

        # GET /api/frontmatter/stats — Schema-Verteilung
        elif path == "/api/frontmatter/stats":
            self._json_response(_fm_stats())
            return

        # GET /api/frontmatter/probe?md=<vault-rel-pfad>
        elif path == "/api/frontmatter/probe":
            md_rel = params.get("md", [""])[0]
            if not md_rel or ".." in md_rel or md_rel.startswith("/"):
                self._json_response({"error": "Ungültiger MD-Pfad"}, 400); return
            if not VAULT_ROOT:
                self._json_response({"error": "VAULT_ROOT nicht konfiguriert"}, 500); return
            md_full = VAULT_ROOT / md_rel
            if not md_full.exists():
                self._json_response({"error": f"MD nicht gefunden: {md_rel}"}, 404); return
            try:
                md_full.resolve().relative_to(VAULT_ROOT.resolve())
            except ValueError:
                self._json_response({"error": "Path traversal blockiert"}, 400); return
            self._json_response(_fm_probe(md_full))
            return

        # GET /api/frontmatter/batch-status
        elif path == "/api/frontmatter/batch-status":
            with _FM_BATCH_LOCK:
                self._json_response(dict(_FM_BATCH_STATE))
            return

        # GET /api/proxy/cache-reader — Proxy für Wilson (port 8501 intern nicht erreichbar)
        elif path == "/api/proxy/cache-reader":
            try:
                r = requests.get("http://cache-reader:8501/stats", timeout=5)
                self.send_response(r.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(r.content)
            except Exception as e:
                self._json_response({"error": str(e)}, 502)
            return

        # GET /api/proxy/docling — Proxy für Wilson (port 5001 intern nicht erreichbar)
        elif path == "/api/proxy/docling":
            try:
                r = requests.get("http://docling-serve:5001/health", timeout=5)
                self.send_response(r.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(r.content)
            except Exception as e:
                self._json_response({"error": str(e)}, 502)
            return

        else:
            self._json_response({"error": "Unbekannter Endpunkt"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        # POST /api/batch/start — neuen Batch-Lauf im Hintergrund starten
        if path == "/api/batch/start":
            try:
                data = self._read_body()
            except Exception:
                self._json_response({"error": "Ungültiger Body"}, 400); return
            input_rel = data.get("input")
            ocr_mode = data.get("ocr_mode", "hybrid")
            output_mode = data.get("output_mode", "structured")
            output_dir = data.get("output_dir")
            limit = int(data.get("limit") or 0)
            if not input_rel:
                self._json_response({"error": "input fehlt"}, 400); return
            try:
                input_path = Path(input_rel)
                if not input_path.exists():
                    self._json_response({"error": f"Input nicht gefunden: {input_path}"}, 400); return
                paths = _parse_batch_input(input_path)
                if limit > 0:
                    paths_preview = paths[:limit]
                else:
                    paths_preview = paths
                run_id = _batch_run_start(
                    str(input_path), ocr_mode, output_mode,
                    output_dir, len(paths_preview),
                )
            except Exception as e:
                self._json_response({"error": f"Start fehlgeschlagen: {e}"}, 500); return
            t = threading.Thread(
                target=_dashboard_batch_runner,
                args=(run_id, input_path, ocr_mode, output_mode,
                      Path(output_dir) if output_dir else None, limit),
                daemon=True,
            )
            t.start()
            self._json_response({"run_id": run_id, "total": len(paths_preview)})
            return

        # POST /api/batch/runs/<id>/(pause|resume|abort)
        if path.startswith("/api/batch/runs/") and path.count("/") == 5:
            try:
                parts = path.split("/")
                run_id = int(parts[4])
                action = parts[5]
            except (ValueError, IndexError):
                self._json_response({"error": "Ungültige Route"}, 400); return
            if action not in ("pause", "resume", "abort"):
                self._json_response({"error": "Unbekannte Aktion"}, 400); return
            state = {"pause": "paused", "resume": "running", "abort": "aborted"}[action]
            _batch_control_set(run_id, state)
            if action == "abort":
                try:
                    with get_db() as con:
                        con.execute(
                            "UPDATE batch_runs SET status='aborted', finished_at=datetime('now','localtime') "
                            "WHERE id=? AND status IN ('running','paused')",
                            (run_id,),
                        )
                except Exception as e:
                    log.warning(f"Abort-Statusupdate fehlgeschlagen: {e}")
            elif action == "pause":
                try:
                    with get_db() as con:
                        con.execute("UPDATE batch_runs SET status='paused' WHERE id=? AND status='running'", (run_id,))
                except Exception:
                    pass
            elif action == "resume":
                try:
                    with get_db() as con:
                        con.execute("UPDATE batch_runs SET status='running' WHERE id=? AND status='paused'", (run_id,))
                except Exception:
                    pass
            self._json_response({"run_id": run_id, "state": state})
            return

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

        # POST /api/correct — Kategorie/Typ korrigieren (+ optional Lernregel)
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
            tg_send(result)
            rule_id = None
            lernregel = data.get("lernregel")
            if lernregel and lernregel.get("muster") and lernregel.get("typ"):
                try:
                    with get_db() as con:
                        cur = con.execute(
                            "INSERT INTO lernregeln (typ, muster, alle_keywords, category_id, type_id, beschreibung) "
                            "VALUES (?,?,?,?,?,?)",
                            (lernregel["typ"], lernregel["muster"],
                             int(lernregel.get("alle_keywords", 0)),
                             category, type_id,
                             lernregel.get("beschreibung", ""))
                        )
                        rule_id = cur.lastrowid
                    log.info(f"Lernregel #{rule_id} gespeichert: {lernregel}")
                    if data.get("retroactive") and rule_id:
                        threading.Thread(
                            target=retroactive_apply_lernregel, args=(rule_id,), daemon=True
                        ).start()
                except Exception as e:
                    log.warning(f"Lernregel speichern fehlgeschlagen: {e}")
            self._json_response({"result": result, "rule_id": rule_id})

        # POST /api/lernregel — Lernregel direkt speichern (ohne Korrektur)
        elif path == "/api/lernregel":
            try:
                data = self._read_body()
            except Exception:
                self._json_response({"error": "Ungültiger Body"}, 400); return
            required = ("typ", "muster", "category_id")
            if not all(data.get(k) for k in required):
                self._json_response({"error": "typ, muster, category_id sind Pflicht"}, 400); return
            try:
                with get_db() as con:
                    cur = con.execute(
                        "INSERT INTO lernregeln (typ, muster, alle_keywords, category_id, type_id, beschreibung) "
                        "VALUES (?,?,?,?,?,?)",
                        (data["typ"], data["muster"], int(data.get("alle_keywords", 0)),
                         data["category_id"], data.get("type_id"), data.get("beschreibung", ""))
                    )
                    rule_id = cur.lastrowid
                if data.get("retroactive"):
                    threading.Thread(
                        target=retroactive_apply_lernregel, args=(rule_id,), daemon=True
                    ).start()
                self._json_response({"status": "ok", "rule_id": rule_id})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        # POST /api/enzyme-refresh — enzyme-Index manuell aktualisieren
        elif path == "/api/enzyme-refresh":
            import subprocess
            enzyme_bin = "/usr/local/bin/enzyme"
            vault_path = "/data/reinhards-vault"
            if not os.path.exists(enzyme_bin):
                self._json_response({"status": "error", "error": "enzyme-Binary nicht gefunden"}, 500)
                return
            def _run_refresh():
                try:
                    r = subprocess.run(
                        [enzyme_bin, "refresh", "--vault", vault_path],
                        capture_output=True, text=True, timeout=300
                    )
                    log.info(f"enzyme refresh: exit={r.returncode} stdout={r.stdout[:200]}")
                    sse_broadcast("enzyme_refresh_done", {
                        "success": r.returncode == 0,
                        "msg": (r.stdout or r.stderr or "").strip()[:200],
                    })
                except Exception as e:
                    log.warning(f"enzyme refresh Fehler: {e}")
                    sse_broadcast("enzyme_refresh_done", {"success": False, "msg": str(e)})
            threading.Thread(target=_run_refresh, daemon=True).start()
            self._json_response({"status": "running", "msg": "enzyme refresh gestartet – dauert ca. 1–2 Min."})

        # POST /api/cache/reindex — Proxy zu cache-reader:8501/reindex
        elif path == "/api/cache/reindex":
            try:
                r = requests.post(f"{CACHE_READER_URL}/reindex", timeout=60)
                self._json_response(r.json(), r.status_code)
            except Exception as e:
                self._json_response({"error": str(e)}, 502)
            return

        # POST /api/cache/export — aktuelle Trefferliste als JSON in dispatcher-temp/ ablegen
        # Body: {"q": "<suchbegriff>", "limit": 20}
        # Response: {"container_path", "host_path", "filename", "count"}
        elif path == "/api/cache/export":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception as e:
                self._json_response({"error": f"Ungültiger JSON-Body: {e}"}, 400)
                return
            q = (body.get("q") or "").strip()
            try:
                limit = int(body.get("limit", 20))
            except (TypeError, ValueError):
                limit = 20
            if not q:
                self._json_response({"error": "Feld 'q' fehlt"}, 400)
                return
            try:
                r = requests.get(
                    f"{CACHE_READER_URL}/search",
                    params={"q": q, "limit": limit},
                    timeout=15,
                )
                data = r.json()
            except Exception as e:
                self._json_response({"error": f"cache-reader nicht erreichbar: {e}"}, 502)
                return
            if r.status_code != 200 or "results" not in data:
                self._json_response({"error": "Suche lieferte kein Ergebnis", "upstream": data}, 502)
                return
            results = data["results"]
            slug = re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")[:40] or "export"
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"cache_export_{slug}_{ts}.json"
            target = TEMP_DIR / filename
            payload = {
                "query": q,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(results),
                "results": results,
            }
            try:
                target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                self._json_response({"error": f"Schreiben fehlgeschlagen: {e}"}, 500)
                return
            self._json_response({
                "container_path": f"/data/dispatcher-temp/{filename}",
                "host_path": str(target),
                "filename": filename,
                "count": len(results),
            })
            return

        # POST /api/rescan/start — Batch-Rescan aller Anlagen-PDFs
        elif path == "/api/rescan/start":
            if not VAULT_PDF_ARCHIV or not VAULT_PDF_ARCHIV.exists():
                self._json_response({"error": "VAULT_PDF_ARCHIV nicht konfiguriert"}, 500); return
            if _rescan_state["active"]:
                self._json_response({"status": "already_running",
                                     "done": _rescan_state["done"],
                                     "total": _rescan_state["total"]}); return
            try:
                with get_db() as con:
                    known = {r[0] for r in con.execute("SELECT pdf_hash FROM dokumente WHERE pdf_hash IS NOT NULL").fetchall()}
            except Exception:
                known = set()
            all_pdfs = sorted(VAULT_PDF_ARCHIV.rglob("*.pdf"), key=lambda p: p.name, reverse=True)
            all_pdfs = [p for p in all_pdfs if not p.name.startswith("._")]
            to_process = [p for p in all_pdfs if _md5_file(p) not in known]
            global _rescan_stop_requested
            _rescan_stop_requested = False
            _rescan_state.update({"active": True, "total": len(to_process), "done": 0, "errors": 0, "current": ""})
            sse_broadcast("rescan_progress", dict(_rescan_state))
            for pdf in to_process:
                file_queue.put(("rescan", pdf))
            log.info(f"Rescan manuell gestartet: {len(to_process)} PDFs eingereiht ({len(all_pdfs) - len(to_process)} bereits bekannt)")
            self._json_response({"status": "started", "total": len(to_process),
                                 "already_known": len(all_pdfs) - len(to_process)})

        # POST /api/rescan/start-undated — Rescan nur undatierter PDFs (kein JJJJMMTT-Prefix)
        elif path == "/api/rescan/start-undated":
            import re as _re2
            if not VAULT_PDF_ARCHIV or not VAULT_PDF_ARCHIV.exists():
                self._json_response({"error": "VAULT_PDF_ARCHIV nicht konfiguriert"}, 500); return
            if _rescan_state["active"]:
                self._json_response({"status": "already_running",
                                     "done": _rescan_state["done"],
                                     "total": _rescan_state["total"]}); return
            try:
                with get_db() as con:
                    known = {r[0] for r in con.execute("SELECT pdf_hash FROM dokumente WHERE pdf_hash IS NOT NULL").fetchall()}
            except Exception:
                known = set()
            def _is_undated(p: Path) -> bool:
                prefix = p.name[:8]
                if not _re2.match(r'^\d{8}$', prefix):
                    return True
                year = int(prefix[:4])
                return not (1990 <= year <= 2030)
            all_pdfs = sorted(VAULT_PDF_ARCHIV.rglob("*.pdf"), key=lambda p: p.name)
            all_pdfs = [p for p in all_pdfs if not p.name.startswith("._")]
            undated = [p for p in all_pdfs if _is_undated(p)]
            to_process = [p for p in undated if _md5_file(p) not in known]
            _rescan_stop_requested = False
            _rescan_state.update({"active": True, "total": len(to_process), "done": 0, "errors": 0, "current": ""})
            sse_broadcast("rescan_progress", dict(_rescan_state))
            for pdf in to_process:
                file_queue.put(("rescan", pdf))
            log.info(f"Rescan undatiert gestartet: {len(to_process)} PDFs eingereiht ({len(undated) - len(to_process)} bereits bekannt)")
            self._json_response({"status": "started", "total": len(to_process),
                                 "already_known": len(undated) - len(to_process),
                                 "mode": "undated_only"})

        # POST /api/rescan/start-dated-de — Rescan: nur datierte PDFs, nur Deutsch
        elif path == "/api/rescan/start-dated-de":
            import re as _re3
            if not VAULT_PDF_ARCHIV or not VAULT_PDF_ARCHIV.exists():
                self._json_response({"error": "VAULT_PDF_ARCHIV nicht konfiguriert"}, 500); return
            if _rescan_state["active"]:
                self._json_response({"status": "already_running",
                                     "done": _rescan_state["done"],
                                     "total": _rescan_state["total"]}); return
            try:
                with get_db() as con:
                    known = {r[0] for r in con.execute("SELECT pdf_hash FROM dokumente WHERE pdf_hash IS NOT NULL").fetchall()}
            except Exception:
                known = set()
            def _is_dated_eligible(p: Path) -> bool:
                if p.name.startswith("._"):
                    return False
                if p.stem.endswith("_IT"):
                    return False
                prefix = p.name[:8]
                if not _re3.match(r'^\d{8}$', prefix):
                    return False
                year = int(prefix[:4])
                return 1990 <= year <= 2030
            all_pdfs = sorted(VAULT_PDF_ARCHIV.rglob("*.pdf"), key=lambda p: p.name)
            eligible = [p for p in all_pdfs if _is_dated_eligible(p)]
            to_process = [p for p in eligible if _md5_file(p) not in known]
            _rescan_stop_requested = False
            _rescan_state.update({"active": True, "total": len(to_process), "done": 0, "errors": 0, "current": ""})
            sse_broadcast("rescan_progress", dict(_rescan_state))
            for pdf in to_process:
                file_queue.put(("rescan_dated_de", pdf))
            log.info(f"Rescan datiert-DE gestartet: {len(to_process)} PDFs ({len(eligible)-len(to_process)} bekannt, {len(all_pdfs)-len(eligible)} ineligibel)")
            self._json_response({"status": "started", "total": len(to_process),
                                 "already_known": len(eligible) - len(to_process),
                                 "mode": "dated_de"})

        # POST /api/rescan/stop — Rescan abbrechen
        elif path == "/api/rescan/stop":
            if _rescan_state["active"]:
                _rescan_stop_requested = True
                log.info("Rescan: Stop angefordert")
                self._json_response({"status": "stopping", "done": _rescan_state["done"], "total": _rescan_state["total"]})
            else:
                self._json_response({"status": "not_running"})

        # POST /api/ocr — PDF-Datei per multipart hochladen, Docling-Markdown zurück
        elif path == "/api/ocr":
            ctype = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                self._json_response({"error": "Kein Body"}, 400); return
            raw = self.rfile.read(length)
            import tempfile, cgi, io
            environ = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(length)}
            fs = cgi.FieldStorage(fp=io.BytesIO(raw), environ=environ, keep_blank_values=True)
            pdf_item = None
            for field in ("file", "files"):
                if field in fs:
                    pdf_item = fs[field]
                    break
            if pdf_item is None or not hasattr(pdf_item, "file"):
                self._json_response({"error": "Feld 'file' fehlt"}, 400); return
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_item.file.read())
                tmp_path = Path(tmp.name)
            try:
                md = convert_to_markdown(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
            if md is None:
                self._json_response({"error": "OCR fehlgeschlagen"}, 502); return
            self._json_response({"text": md})

        # POST /api/duplikate/scan — Duplikat-Scan starten
        elif path == "/api/duplikate/scan":
            with _DEDUP_SCAN_LOCK:
                if _DEDUP_SCAN_STATUS["running"]:
                    self._json_response({"error": "Scan läuft bereits"}, 409); return
            t = threading.Thread(target=_run_duplikat_scan, daemon=True)
            t.start()
            self._json_response({"ok": True, "msg": "Scan gestartet"})
            return

        # POST /api/duplikate/move-all — alle offenen Duplikate verschieben
        elif path == "/api/duplikate/move-all":
            with _DEDUP_MOVE_LOCK:
                if _DEDUP_MOVE_STATUS["running"]:
                    self._json_response({"error": "Batch-Move läuft bereits"}, 409); return
            threading.Thread(target=_run_move_all, daemon=True).start()
            self._json_response({"ok": True, "msg": "Batch-Move gestartet"})
            return

        # POST /api/duplikate/move — Duplikat-Eintrag verschieben
        elif path == "/api/duplikate/move":
            try:
                body = self._read_body()
            except Exception:
                self._json_response({"error": "Ungültiger Body"}, 400); return
            gruppe_id = body.get("gruppe_id")
            eintrag_id = body.get("eintrag_id")
            if not gruppe_id or not eintrag_id:
                self._json_response({"error": "gruppe_id und eintrag_id erforderlich"}, 400); return
            result = _move_duplikat(int(gruppe_id), int(eintrag_id))
            status = 200 if result.get("ok") else 400
            self._json_response(result, status)
            return

        # POST /api/frontmatter/upgrade?md=<vault-rel-pfad>
        elif path == "/api/frontmatter/upgrade":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            md_rel = qs.get("md", [""])[0]
            if not md_rel or ".." in md_rel or md_rel.startswith("/"):
                self._json_response({"error": "Ungültiger MD-Pfad"}, 400); return
            if not VAULT_ROOT:
                self._json_response({"error": "VAULT_ROOT nicht konfiguriert"}, 500); return
            md_full = VAULT_ROOT / md_rel
            if not md_full.exists():
                self._json_response({"error": f"MD nicht gefunden: {md_rel}"}, 404); return
            try:
                md_full.resolve().relative_to(VAULT_ROOT.resolve())
            except ValueError:
                self._json_response({"error": "Path traversal blockiert"}, 400); return
            result = _fm_apply_upgrade(md_full)
            if result.get("ok"):
                _FM_STATS_CACHE_TS = 0.0  # invalidate cache
            self._json_response(result, 200 if result.get("ok") else 400)
            return

        # POST /api/frontmatter/batch-upgrade — alle Vault-MDs upgraden (Hintergrund)
        elif path == "/api/frontmatter/batch-upgrade":
            with _FM_BATCH_LOCK:
                if _FM_BATCH_STATE.get("running"):
                    self._json_response({"error": "Batch läuft bereits"}, 409); return
            threading.Thread(target=_fm_batch_upgrade_all, daemon=True).start()
            self._json_response({"ok": True, "message": "Batch-Upgrade gestartet"})
            return

        else:
            self._json_response({"error": "Unbekannter Endpunkt"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/lernregeln/"):
            try:
                rule_id = int(path.split("/")[-1])
            except ValueError:
                self._json_response({"error": "Ungültige ID"}, 400); return
            with get_db() as con:
                con.execute("DELETE FROM lernregeln WHERE id = ?", (rule_id,))
            log.info(f"Lernregel #{rule_id} gelöscht")
            self._json_response({"status": "deleted", "id": rule_id})
        else:
            self._json_response({"error": "Unbekannter Endpunkt"}, 404)

    def log_message(self, fmt, *args):
        pass  # Kein Access-Log-Spam


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def start_api_server():
    server = _ThreadedHTTPServer(("0.0.0.0", API_PORT), _ApiHandler)
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
     → JA: category_id="krankenversicherung", type_id="leistungsabrechnung"
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
            desc = (regel.get("beschreibung") or "").strip().replace("\n", " ")
            hinweis = (regel.get("hinweis") or "").strip().replace("\n", " ")
            regeln_lines.append(
                f"- Absender-Branche: {kws}\n"
                f"  Beschreibung: {desc}\n"
                f"  → category_id=\"{cat}\"\n"
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
- "rechnungsdatum": Datum des Dokuments als String "DD.MM.YYYY", oder null. Datumsformate wie "18/04/2026", "18 aprile 2026", "April 18, 2026" oder "2026-04-18" bitte in DD.MM.YYYY umwandeln. Italienische Monatsnamen: gennaio=01, febbraio=02, marzo=03, aprile=04, maggio=05, giugno=06, luglio=07, agosto=08, settembre=09, ottobre=10, novembre=11, dicembre=12.
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
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_ctx": OLLAMA_NUM_CTX},
            },
            timeout=OLLAMA_TIMEOUT,
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

def _safe_pdf_name_from_vault_pfad(vault_pfad: str, fallback: str = "") -> str:
    """Gibt den PDF-Dateinamen aus vault_pfad zurück — strippt doppelte .pdf-Extension."""
    stem = Path(vault_pfad).stem
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return stem + ".pdf" if stem else fallback

def _sanitize_name_part(s: str) -> str:
    """Sanitize a string for use in filenames: collapse whitespace, keep alphanumeric + umlauts."""
    s = re.sub(r"[^\w\s\-äöüÄÖÜß]", "", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s


def _date_from_filename_prefix(stem: str) -> str | None:
    """Extrahiert YYYYMMDD aus Dateinamen-Prefix (YYYYMMDD oder DDMMYYYY).

    Gibt YYYYMMDD zurück oder None wenn kein valides Datum erkennbar.
    """
    m = re.match(r'^(\d{8})', stem)
    if not m:
        return None
    s = m.group(1)
    yyyy, mm, dd = s[:4], s[4:6], s[6:]
    if 1990 <= int(yyyy) <= 2035 and 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
        return s  # YYYYMMDD ✓
    # Versuche DDMMYYYY-Interpretation (Scanner-Format)
    dd2, mm2, yyyy2 = s[:2], s[2:4], s[4:]
    if 1990 <= int(yyyy2) <= 2035 and 1 <= int(mm2) <= 12 and 1 <= int(dd2) <= 31:
        return f"{yyyy2}{mm2}{dd2}"  # umdrehen → YYYYMMDD
    return None


def build_clean_filename(result: dict, original_stem: str) -> str:
    """Build clean filename: YYYYMMDD_Absender_Dokumenttyp.

    Falls Datum oder Absender fehlt, wird der Original-Dateiname als Fallback verwendet.
    Datums-Priorität: 1. LLM (rechnungsdatum) → 2. Dateiname-Prefix → 3. Heute
    """
    datum = result.get("rechnungsdatum")  # "DD.MM.YYYY"
    absender = result.get("absender")
    type_label = result.get("type_label") or result.get("type_id") or ""

    # Datum → YYYYMMDD
    date_str = None
    # 1. LLM-extrahiertes Datum aus Dokumentinhalt
    if datum and re.match(r"\d{2}\.\d{2}\.\d{4}", datum):
        d, m, y = datum.split(".")
        if 1990 <= int(y) <= 2035 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            date_str = f"{y}{m}{d}"
    # 2. Dateiname-Prefix (YYYYMMDD oder DDMMYYYY vom Scanner)
    if not date_str:
        date_str = _date_from_filename_prefix(original_stem)
    # 3. Heutiges Datum als letzter Ausweg
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

    # Tier (optional, nur bei familie/tierarzt gesetzt)
    tier = result.get("tier")
    tier_clean = _sanitize_name_part(tier) if tier else ""

    # Zusammenbauen — kein type_label mehr (Typen sind abgeschafft)
    parts = [date_str]
    if absender_clean:
        parts.append(absender_clean)
    if tier_clean:
        parts.append(tier_clean)

    if len(parts) == 1:
        # Kein Absender → Original-Stem verwenden
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
    # Im Batch-Modus (classify-only/structured) keine Dateibewegung: Klassifikation
    # wird in DB + Export gespeichert, der Vault bleibt unverändert.
    if _batch_active() and _batch_output_mode() != "vault-move":
        log.info(f"Batch-Modus ({_batch_output_mode()}) — kein Vault-Move für {file_path.name}")
        return
    if not VAULT_PDF_ARCHIV or not VAULT_ROOT:
        log.warning("VAULT_PDF_ARCHIV/VAULT_ROOT nicht konfiguriert — Dateien bleiben in WATCH_DIR")
        return

    rechnungsdatum = result.get("rechnungsdatum") if result else None
    year = rechnungsdatum[-4:] if rechnungsdatum and len(rechnungsdatum) >= 4 else datetime.now().strftime("%Y")
    adressat = (result.get("adressat") or "") if result else ""

    # Sauberen Dateinamen generieren
    # _force_stem: von Wilson vorgegebener Dateiname (Bypass-Modus) — nicht neu ableiten.
    if result and result.get("_force_stem"):
        clean_name = _sanitize_name_part(result["_force_stem"])
    elif result:
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
        vault_pfad = build_vault_path(category_id, type_id, adressat, year, f"{clean_name}_{counter}.md")
        dest_md = VAULT_ROOT / vault_pfad
        dest_pdf = VAULT_PDF_ARCHIV / f"{clean_name}_{counter}.pdf"
        counter += 1

    pdf_filename = dest_pdf.name

    # PDF verschieben
    VAULT_PDF_ARCHIV.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file_path), str(dest_pdf))
    log.info(f"PDF → Anlagen: {pdf_filename}")

    _write_vault_md(dest_pdf, dest_md, vault_pfad, temp_md, result, category_id, type_id, file_path.name)


def _write_vault_md(pdf_dest: Path, dest_md: Path, vault_pfad: str,
                    temp_md: Path, result: dict, category_id: str, type_id: str,
                    original_filename: str):
    """Schreibt das Vault-MD mit Frontmatter. Bewegt keine PDFs.
    Wird sowohl von move_to_vault() als auch von rescan_archived_pdf() genutzt."""
    pdf_filename = pdf_dest.name

    # Frontmatter vor MD-Inhalt prependen + PDF-Link als erste Body-Zeile
    try:
        ocr_content = temp_md.read_text(encoding="utf-8")
        frontmatter = _build_frontmatter(result or {}, pdf_filename, category_id, type_id)
        pdf_link_line = f"📎 [[Anlagen/{pdf_filename}]]\n\n"
        temp_md.write_text(frontmatter + pdf_link_line + ocr_content, encoding="utf-8")
    except Exception as e:
        log.warning(f"Frontmatter konnte nicht geschrieben werden: {e}")

    # mtime der PDF-Datei auf Dokumentdatum setzen
    datum = (result or {}).get("rechnungsdatum")
    if datum:
        try:
            dd, mm, yyyy = datum.split(".")
            ts = datetime(int(yyyy), int(mm), int(dd), 12, 0, 0).timestamp()
            os.utime(pdf_dest, (ts, ts))
        except Exception as e:
            log.debug(f"mtime setzen fehlgeschlagen: {e}")

    # MD verschieben
    dest_md.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp_md), str(dest_md))
    log.info(f"MD → Vault: {vault_pfad}")

    # vault_pfad + anlagen_dateiname in DB speichern
    try:
        with get_db() as con:
            con.execute(
                "UPDATE dokumente SET vault_kategorie=?, vault_typ=?, vault_pfad=?, anlagen_dateiname=? WHERE dateiname=?",
                (category_id, type_id, vault_pfad, pdf_filename, original_filename)
            )
    except Exception as e:
        log.warning(f"vault_pfad DB-Update fehlgeschlagen: {e}")


def apply_keyword_rules(result: dict, text: str, categories: dict) -> dict:
    """Überschreibt LLM-Klassifikation mit deterministischen Keyword-Rules.
    Läuft nach dem LLM. Greift nur wenn ein Keyword im Text gefunden wird."""
    text_lower = text.lower()
    for rule in KEYWORD_RULES:
        keywords = rule.get("keywords", [])
        alle = rule.get("alle_keywords", False)
        if alle:
            match = all(kw.lower() in text_lower for kw in keywords)
        else:
            match = any(kw.lower() in text_lower for kw in keywords)
        if not match:
            continue
        cat_id  = rule.get("category_id")
        type_id = rule.get("type_id")
        if not cat_id or cat_id not in categories:
            continue
        # Nur überschreiben wenn Regel stärker als LLM-Ergebnis
        old_cat  = result.get("category_id")
        old_conf = result.get("konfidenz_category", "niedrig")
        if old_cat == cat_id and old_conf in ("hoch", "mittel"):
            continue  # LLM war schon korrekt und sicher
        result["category_id"]        = cat_id
        result["type_id"]            = type_id  # None wenn nicht in Regel gesetzt
        result["konfidenz_category"] = "hoch"
        log.info(f"Keyword-Rule greift: '{rule.get('beschreibung', cat_id)}' → {cat_id}")
        break  # Erste passende Regel gewinnt
    return result


def apply_lernregeln_from_db(result: dict, text: str, absender: str | None, categories: dict) -> dict:
    """Wendet in der DB gespeicherte Lernregeln an (nach apply_keyword_rules)."""
    try:
        with get_db() as con:
            rules = con.execute("SELECT * FROM lernregeln ORDER BY id").fetchall()
    except Exception:
        return result
    text_lower = text.lower() if text else ""
    absender_lower = (absender or "").lower()
    for rule in rules:
        r = dict(rule)
        cat_id = r["category_id"]
        if cat_id not in categories:
            continue
        matched = False
        if r["typ"] == "absender":
            muster = r["muster"].lower()
            matched = muster and muster in absender_lower
        elif r["typ"] == "keyword":
            keywords = [k.strip().lower() for k in r["muster"].split(",") if k.strip()]
            if r["alle_keywords"]:
                matched = all(kw in text_lower for kw in keywords)
            else:
                matched = any(kw in text_lower for kw in keywords)
        if not matched:
            continue
        old_cat  = result.get("category_id")
        old_conf = result.get("konfidenz_category", "niedrig")
        if old_cat == cat_id and old_conf in ("hoch", "mittel"):
            continue
        result["category_id"]        = cat_id
        result["type_id"]            = r.get("type_id")
        result["konfidenz_category"] = "hoch"
        if result.get("konfidenz_type") == "niedrig":
            result["konfidenz_type"] = "mittel"
        log.info(f"Lernregel #{r['id']} greift: '{r.get('beschreibung', cat_id)}' → {cat_id}/{r.get('type_id')}")
        try:
            with get_db() as con:
                con.execute("UPDATE lernregeln SET anwendungen = anwendungen + 1 WHERE id = ?", (r["id"],))
        except Exception:
            pass
        break
    return result


def retroactive_apply_lernregel(rule_id: int):
    """Hintergrund-Job: neue Lernregel auf alle vorhandenen Dokumente anwenden."""
    try:
        with get_db() as con:
            rule = con.execute("SELECT * FROM lernregeln WHERE id = ?", (rule_id,)).fetchone()
            if not rule:
                return
            rule = dict(rule)
        categories = load_categories()
        cat_id  = rule["category_id"]
        type_id = rule.get("type_id")
        if cat_id not in categories:
            return
        cat_def    = categories.get(cat_id, {})
        cat_label  = cat_def.get("label", cat_id)
        type_label = type_id or ""
        for t in cat_def.get("types", []):
            if t["id"] == type_id:
                type_label = t["label"]
                break
        updated = 0
        with get_db() as con:
            docs = con.execute(
                "SELECT id, dateiname, absender, vault_pfad, kategorie, typ FROM dokumente"
            ).fetchall()
        for doc in docs:
            doc = dict(doc)
            matched = False
            if rule["typ"] == "absender":
                muster = rule["muster"].lower()
                matched = muster and muster in (doc.get("absender") or "").lower()
            elif rule["typ"] == "keyword":
                keywords = [k.strip().lower() for k in rule["muster"].split(",") if k.strip()]
                md_text = ""
                if doc.get("vault_pfad") and VAULT_ROOT:
                    md_path = VAULT_ROOT / doc["vault_pfad"]
                    try:
                        md_text = md_path.read_text(encoding="utf-8", errors="replace").lower()
                    except Exception:
                        pass
                if rule["alle_keywords"]:
                    matched = all(kw in md_text for kw in keywords)
                else:
                    matched = any(kw in md_text for kw in keywords)
            if not matched:
                continue
            if doc.get("kategorie") == cat_id and doc.get("typ") == type_id:
                continue
            try:
                handle_correction(doc["id"], cat_id, type_id or "allgemein")
                updated += 1
            except Exception as e:
                log.warning(f"Retroaktive Lernregel #{rule_id}: Fehler bei dok {doc['id']}: {e}")
        with get_db() as con:
            con.execute("UPDATE lernregeln SET anwendungen = anwendungen + ? WHERE id = ?",
                        (updated, rule_id))
        log.info(f"Retroaktive Lernregel #{rule_id} '{rule.get('beschreibung')}': {updated} Dokumente aktualisiert")
        sse_broadcast("lernregel_applied", {"rule_id": rule_id, "updated": updated,
                                            "beschreibung": rule.get("beschreibung", "")})
    except Exception as e:
        log.error(f"retroactive_apply_lernregel #{rule_id}: {e}")


def _rescan_advance(done_inc: int = 1, error_inc: int = 0):
    """Zähler hochsetzen und SSE senden."""
    _rescan_state["done"]   += done_inc
    _rescan_state["errors"] += error_inc
    if _rescan_state["done"] >= _rescan_state["total"]:
        _rescan_state["active"] = False
    sse_broadcast("rescan_progress", dict(_rescan_state))


def _find_existing_vault_md(pdf_path: Path) -> Path | None:
    """Sucht im Vault nach einer MD, die das PDF per [[Anlagen/NAME]] Wikilink referenziert."""
    if not VAULT_ROOT:
        return None
    link_pattern = f"[[Anlagen/{pdf_path.name}]]"
    for md in VAULT_ROOT.rglob("*.md"):
        try:
            if link_pattern in md.read_text(encoding="utf-8", errors="ignore"):
                return md
        except Exception:
            continue
    return None


def rescan_archived_pdf(pdf_path: Path, language_filter: str | None = None):
    """Verarbeitet ein bereits in Anlagen/ liegendes PDF neu (Rescan-Modus).
    Das PDF wird NICHT verschoben. Nur OCR + Klassifikation + neues MD.
    language_filter=None  → alle Sprachen verarbeiten (bisheriges Verhalten)
    language_filter='de'  → nur Deutsch verarbeiten; Italienisch → _IT.pdf umbenennen; Rest skip
    Skip wenn bereits in DB (via pdf_hash) oder macOS-Ressource-Fork (._)."""
    global _rescan_stop_requested
    if not VAULT_PDF_ARCHIV or not VAULT_ROOT:
        return
    if pdf_path.name.startswith("._"):
        log.debug(f"Rescan skip (macOS-Ressource-Fork): {pdf_path.name}")
        _rescan_advance()
        return
    if _rescan_stop_requested:
        log.info("Rescan: Stop angefordert — überspringe weiteres Dokument")
        _rescan_advance()
        if not _rescan_state["active"]:
            _rescan_stop_requested = False
        return

    # Hash-Check: bereits verarbeitet?
    pdf_hash = _md5_file(pdf_path)
    try:
        with get_db() as con:
            row = con.execute("SELECT id FROM dokumente WHERE pdf_hash=?", (pdf_hash,)).fetchone()
            if row:
                log.debug(f"Rescan skip (bereits in DB): {pdf_path.name}")
                return
    except Exception:
        pass

    # Wikilink-Check: existiert bereits eine manuell gepflegte MD im Vault?
    existing_md = _find_existing_vault_md(pdf_path)
    if existing_md:
        rel = existing_md.relative_to(VAULT_ROOT)
        log.info(f"Rescan skip (bestehende MD: {rel}): {pdf_path.name}")
        _step_emit(pdf_path.name, "started", "Rescan gestartet", "done")
        _step_emit(pdf_path.name, "vault", "Vault (bestehende MD)", "skip",
                   error=f"Bereits verlinkt in: {existing_md.name}")
        try:
            with get_db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO dokumente (dateiname, pdf_hash, vault_pfad, kategorie, konfidenz) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pdf_path.name, pdf_hash, str(rel), "manuell", "hoch")
                )
        except Exception as e:
            log.debug(f"DB-Eintrag für manuell gepflegte MD fehlgeschlagen: {e}")
        _rescan_advance()
        return

    log.info(f"Rescan: {pdf_path.name}")
    _rescan_state["current"] = pdf_path.name
    _step_emit(pdf_path.name, "started", "Rescan gestartet", "done")
    ocr_len_orig = 0  # wird nach OCR gesetzt

    # OCR via Docling
    t0 = datetime.now()
    md_content = convert_to_markdown(pdf_path)
    dur_ocr = (datetime.now() - t0).total_seconds() * 1000
    if not md_content:
        _step_emit(pdf_path.name, "ocr", "OCR / Docling", "error", error="Docling fehlgeschlagen")
        log.warning(f"Rescan: Docling fehlgeschlagen für {pdf_path.name}")
        _rescan_advance(error_inc=1)
        return
    ocr_len_orig = len(md_content)
    _step_emit(pdf_path.name, "ocr", "OCR / Docling", "done",
               extracted={"chars": ocr_len_orig}, duration_ms=dur_ocr)
    if ocr_len_orig < OCR_MIN_CHARS:
        # Bildbasiertes PDF: zu wenig Text für LLM — trotzdem in Inbox archivieren
        _step_emit(pdf_path.name, "ocr_quality", "OCR-Qualitäts-Gate", "error",
                   error=f"Nur {ocr_len_orig} Zeichen erkannt (Minimum: {OCR_MIN_CHARS}) → Inbox")
        log.warning(f"Rescan: Bildbasiertes PDF ({ocr_len_orig} Zeichen) → Inbox: {pdf_path.name}")
        low_result: dict = {"konfidenz": "niedrig", "konfidenz_category": "niedrig"}
        date_yyyymmdd = _date_from_filename_prefix(pdf_path.stem)
        if date_yyyymmdd and len(date_yyyymmdd) == 8:
            yyyy, mm, dd = date_yyyymmdd[:4], date_yyyymmdd[4:6], date_yyyymmdd[6:]
            low_result["rechnungsdatum"] = f"{dd}.{mm}.{yyyy}"
        year_low = low_result.get("rechnungsdatum", "")[-4:] or datetime.now().strftime("%Y")
        ts_low = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        temp_md_low = TEMP_DIR / f"{ts_low}_{pdf_path.stem}.md"
        stub_text = f"*[Bildbasiertes PDF — OCR nur {ocr_len_orig} Zeichen erkannt]*\n\n" + md_content
        temp_md_low.write_text(stub_text, encoding="utf-8")
        clean_name_low = _sanitize_name_part(pdf_path.stem)
        vault_pfad_low = build_vault_path("", "", "", year_low, f"{clean_name_low}.md")
        dest_md_low = VAULT_ROOT / vault_pfad_low
        counter_low = 2
        while dest_md_low.exists():
            vault_pfad_low = build_vault_path("", "", "", year_low, f"{clean_name_low}_{counter_low}.md")
            dest_md_low = VAULT_ROOT / vault_pfad_low
            counter_low += 1
        save_to_db(pdf_path, low_result)
        _step_emit(pdf_path.name, "db", "Datenbank gespeichert", "done",
                   extracted={"konfidenz": "niedrig"})
        _write_vault_md(pdf_path, dest_md_low, vault_pfad_low, temp_md_low,
                        low_result, "", "", pdf_path.name)
        _step_emit(pdf_path.name, "vault", "Vault (Inbox – bildbasiert)", "done",
                   extracted={"vault_pfad": vault_pfad_low})
        _rescan_advance()
        return

    # Temp-MD schreiben
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temp_md = TEMP_DIR / f"{ts}_{pdf_path.stem}.md"
    temp_md.write_text(md_content, encoding="utf-8")

    # Sprache + Übersetzung
    lang, prob = detect_document_language(md_content)
    _step_emit(pdf_path.name, "lang", "Spracherkennung", "done",
               extracted={"lang": lang, "prob": prob})

    # Sprachfilter: nur Deutsch verarbeiten, Italienisch umbenennen
    if language_filter == "de":
        if lang == "it" and prob >= 0.8:
            new_name = f"{pdf_path.stem}_IT.pdf"
            new_path = pdf_path.parent / new_name
            if not new_path.exists():
                pdf_path.rename(new_path)
                log.info(f"Rescan: Italienisch → umbenannt zu {new_name}")
            else:
                log.info(f"Rescan: Italienisch skip (_{new_name} bereits vorhanden): {pdf_path.name}")
            _step_emit(pdf_path.name, "lang", "Spracherkennung", "skip",
                       error=f"Italienisch → {new_name}")
            _rescan_advance()
            return
        elif not (lang == "de" and prob >= 0.6):
            log.info(f"Rescan skip (nicht Deutsch, lang={lang} prob={prob:.2f}): {pdf_path.name}")
            _step_emit(pdf_path.name, "lang", "Spracherkennung", "skip",
                       error=f"Nicht Deutsch (lang={lang}, prob={prob:.2f})")
            _rescan_advance()
            return

    if lang != "de" and prob >= 0.85:
        t1 = datetime.now()
        translated = translate_to_german(md_content, lang)
        dur_tr = (datetime.now() - t1).total_seconds() * 1000
        if translated:
            md_content = translated
            _step_emit(pdf_path.name, "translate", "Übersetzung", "done",
                       extracted={"chars": len(md_content), "ocr_chars": ocr_len_orig,
                                  "input_limit": 6000}, duration_ms=dur_tr)
    else:
        _step_emit(pdf_path.name, "translate", "Übersetzung", "skip")

    # Deterministische Extraktion
    header    = extract_document_header(md_content)
    _step_emit(pdf_path.name, "header", "Header-Extraktion", "done",
               extracted={"absender": header.get("absender", {}), "empfaenger": header.get("empfaenger", {})})
    idents    = extract_identifiers(md_content)
    doc_type  = extract_document_type(md_content)
    _step_emit(pdf_path.name, "doctype", "Dokumenttyp-Erkennung", "done",
               extracted={"typ": doc_type.get("typ"), "keyword": doc_type.get("keyword")})
    adressat_match = resolve_adressat(idents, md_content)
    absender_match = resolve_absender(idents, header)
    _step_emit(pdf_path.name, "identifiers", "Identifier & Personen", "done",
               extracted={"identifiers": idents, "adressat": adressat_match, "absender_match": absender_match})

    # LLM-Klassifikation
    categories = load_categories()
    t2 = datetime.now()
    result = classify_with_ollama(
        md_content,
        categories,
        header=header,
        identifiers=idents,
        adressat_match=adressat_match,
        absender_match=absender_match,
        doc_type_info=doc_type,
    )

    dur_llm = (datetime.now() - t2).total_seconds() * 1000
    if not result or not result.get("category_id"):
        result = result or {}
        result["category_id"] = None
        result["type_id"] = None

    # Halluzinations-Guard + Overrides
    if result.get("category_id") and result["category_id"] not in categories:
        result["category_id"] = None
        result["type_id"] = None
        result["konfidenz_category"] = "niedrig"

    if result.get("category_id") and result.get("type_id"):
        valid_types = [t["id"] for t in categories.get(result["category_id"], {}).get("types", [])]
        if valid_types and result["type_id"] not in valid_types:
            log.warning(f"HALLUZINATION type_id={result['type_id']} in category={result['category_id']} → type=None")
            result["type_id"] = None

    if adressat_match:
        result["adressat"] = adressat_match.get("name", "")
        result["konfidenz_adressat"] = "hoch"
    if absender_match:
        if absender_match.get("adressat_default") and not adressat_match:
            result["adressat"] = absender_match["adressat_default"]
            result["konfidenz_adressat"] = "hoch"

    result = apply_keyword_rules(result, md_content, categories)
    result = apply_lernregeln_from_db(result, md_content, result.get("absender"), categories)
    result["konfidenz"] = aggregate_konfidenz(result)

    # Datums-Fallback: falls LLM kein Datum liefert → Dateiname-Prefix verwenden
    if not result.get("rechnungsdatum"):
        date_yyyymmdd = _date_from_filename_prefix(pdf_path.stem)
        if date_yyyymmdd and len(date_yyyymmdd) == 8:
            yyyy, mm, dd = date_yyyymmdd[:4], date_yyyymmdd[4:6], date_yyyymmdd[6:]
            result["rechnungsdatum"] = f"{dd}.{mm}.{yyyy}"
            result.setdefault("konfidenz_datum", "mittel")
            log.info(f"Datum-Fallback aus Dateiname: {result['rechnungsdatum']} ({pdf_path.name})")

    _step_emit(pdf_path.name, "llm", "LLM-Klassifikation", "done",
               extracted=result, duration_ms=dur_llm)

    # Konfidenz niedrig → 00 Inbox
    category_id = result.get("category_id")
    type_id     = result.get("type_id")
    # Nur in Inbox wenn Kategorie selbst unsicher — nicht wenn nur Datum/Absender fehlt
    if result.get("konfidenz_category") == "niedrig" or (
        not result.get("konfidenz_category") and result.get("konfidenz") == "niedrig"
    ):
        category_id = None
        type_id     = None

    # Dateiname + Vault-Pfad
    adressat_final = (result.get("adressat") or "").strip()
    rechnungsdatum = result.get("rechnungsdatum")
    year = rechnungsdatum[-4:] if rechnungsdatum and len(rechnungsdatum) >= 4 else datetime.now().strftime("%Y")
    clean_name = build_clean_filename(result, pdf_path.stem)

    # Das PDF liegt bereits in Anlagen/ — wir verwenden seinen jetzigen Namen
    pdf_filename = pdf_path.name
    vault_pfad = build_vault_path(category_id or "", type_id or "", adressat_final, year, f"{clean_name}.md")
    dest_md = VAULT_ROOT / vault_pfad

    # Kollisionsvermeidung für MD
    counter = 2
    while dest_md.exists():
        vault_pfad = build_vault_path(category_id or "", type_id or "", adressat_final, year, f"{clean_name}_{counter}.md")
        dest_md = VAULT_ROOT / vault_pfad
        counter += 1

    # In DB speichern
    save_to_db(pdf_path, result)
    _step_emit(pdf_path.name, "db", "Datenbank gespeichert", "done",
               extracted={"konfidenz": result.get("konfidenz"), "category_id": category_id, "type_id": type_id})

    # vault_pfad + anlagen_dateiname setzen (Rescan: PDF behält Originalnamen)
    try:
        with get_db() as con:
            con.execute(
                "UPDATE dokumente SET vault_kategorie=?, vault_typ=?, vault_pfad=?, anlagen_dateiname=? WHERE dateiname=?",
                (category_id, type_id, vault_pfad, pdf_path.name, pdf_path.name)
            )
    except Exception as e:
        log.warning(f"Rescan vault_pfad DB-Update: {e}")

    # mtime der PDF-Datei auf Dokumentdatum setzen
    datum = result.get("rechnungsdatum")
    if datum:
        try:
            dd, mm, yyyy = datum.split(".")
            import time as _time
            ts = datetime(int(yyyy), int(mm), int(dd), 12, 0, 0).timestamp()
            os.utime(pdf_path, (ts, ts))
        except Exception as e:
            log.debug(f"mtime setzen fehlgeschlagen: {e}")

    # MD schreiben (PDF nicht verschieben!)
    try:
        ocr_content = temp_md.read_text(encoding="utf-8")
        frontmatter = _build_frontmatter(result, pdf_filename, category_id or "", type_id or "")
        pdf_link_line = f"📎 [[Anlagen/{pdf_filename}]]\n\n"
        temp_md.write_text(frontmatter + pdf_link_line + ocr_content, encoding="utf-8")
        dest_md.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_md), str(dest_md))
        log.info(f"Rescan MD → Vault: {vault_pfad}")
    except Exception as e:
        log.warning(f"Rescan MD schreiben fehlgeschlagen: {e}")
        return

    _step_emit(pdf_path.name, "vault", "Vault-Move", "done",
               extracted={"vault_pfad": vault_pfad})

    sse_broadcast("doc_processed", {
        "id":        None,
        "dateiname": pdf_path.name,
        "pdf_name":  pdf_path.name,
        "kategorie": category_id,
        "typ":       type_id,
        "absender":  result.get("absender"),
        "adressat":  result.get("adressat"),
        "konfidenz": result.get("konfidenz"),
        "vault_pfad": vault_pfad,
        "erstellt_am": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    _rescan_advance()
    log.info(f"Rescan abgeschlossen: {pdf_path.name} → {category_id}/{type_id}")


def process_enex_file(enex_path: Path):
    """Verarbeitet eine Evernote-ENEX-Datei.

    Track A: PDF-Anhänge → TEMP_DIR/enex_extracted/ → file_queue (normaler Dispatcher-Flow)
    Track B: Native Text-Notes (kein PDF) → direkt als MD in 00 Inbox/
    Track C: Bilder (JPG/PNG/TIFF) → img2pdf → file_queue
             Andere Formate → enex_skipped.log
    """
    try:
        from lxml import etree
    except ImportError:
        log.error("lxml nicht installiert — ENEX-Verarbeitung nicht möglich")
        return

    import base64

    log.info(f"ENEX: {enex_path.name}")
    skipped_log = TEMP_DIR / "enex_skipped.log"
    extract_dir = TEMP_DIR / "enex_extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    done_dir    = TEMP_DIR / "enex_done"
    done_dir.mkdir(parents=True, exist_ok=True)

    notebook_name = enex_path.stem

    try:
        tree = etree.parse(str(enex_path))
    except Exception as e:
        log.error(f"ENEX Parse-Fehler: {enex_path.name}: {e}")
        return

    notes = tree.findall(".//note")
    log.info(f"ENEX {enex_path.name}: {len(notes)} Notizen")

    for note in notes:
        title   = (note.findtext("title") or "untitled")[:80]
        created = note.findtext("created") or ""   # YYYYMMDDTHHMMSSZ
        date_prefix = created[:8] if len(created) >= 8 else ""
        tags = [t.text for t in note.findall("tag") if t.text]

        resources = note.findall(".//resource")
        has_pdf = False

        for res in resources:
            mime = res.findtext("mime") or ""
            data_el = res.find("data")
            if data_el is None or not data_el.text:
                continue
            raw = base64.b64decode(data_el.text.strip())

            fn_el = res.find(".//resource-attributes/file-name")
            orig_name = fn_el.text if fn_el is not None and fn_el.text else "anhang"
            safe_title = re.sub(r"[^\w\-]", "_", title)[:60]
            stem = f"{date_prefix}_{safe_title}" if date_prefix else safe_title

            if mime == "application/pdf":
                has_pdf = True
                dest = extract_dir / f"{stem}.pdf"
                # Kollisionsvermeidung
                c = 2
                while dest.exists():
                    dest = extract_dir / f"{stem}_{c}.pdf"
                    c += 1
                dest.write_bytes(raw)
                log.info(f"ENEX Track A: {dest.name}")
                file_queue.put(dest)  # Außerhalb Watcher-Scope → kein Double-Fire

            elif mime.startswith("image/"):
                try:
                    import img2pdf
                    img_tmp = TEMP_DIR / f"{stem}_img.{mime.split('/')[-1]}"
                    img_tmp.write_bytes(raw)
                    pdf_dest = extract_dir / f"{stem}.pdf"
                    c = 2
                    while pdf_dest.exists():
                        pdf_dest = extract_dir / f"{stem}_{c}.pdf"
                        c += 1
                    pdf_dest.write_bytes(img2pdf.convert(str(img_tmp)))
                    img_tmp.unlink(missing_ok=True)
                    log.info(f"ENEX Track C (img): {pdf_dest.name}")
                    file_queue.put(pdf_dest)
                    has_pdf = True
                except Exception as e:
                    log.warning(f"ENEX img2pdf fehlgeschlagen: {orig_name}: {e}")
                    with open(skipped_log, "a") as f:
                        f.write(f"{enex_path.name}\t{title}\t{orig_name}\t{mime}\timg2pdf_error: {e}\n")
            else:
                # Track C — Andere Formate überspringen
                log.info(f"ENEX übersprungen ({mime}): {orig_name}")
                with open(skipped_log, "a") as f:
                    f.write(f"{enex_path.name}\t{title}\t{orig_name}\t{mime}\tunsupported\n")

        # Track B — Native Text-Note (kein PDF-Anhang)
        if not has_pdf and VAULT_ROOT:
            enml = note.findtext("content") or ""
            try:
                enml_tree = etree.fromstring(enml.encode("utf-8"))
                text = " ".join(enml_tree.itertext()).strip()
            except Exception:
                text = re.sub(r"<[^>]+>", " ", enml).strip()

            safe_title = re.sub(r"[^\w\-äöüÄÖÜß ]", "_", title)[:80]
            md_filename = f"{date_prefix}_{safe_title}.md" if date_prefix else f"{safe_title}.md"
            inbox_dir = VAULT_ROOT / "00 Inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            dest_md = inbox_dir / md_filename
            c = 2
            while dest_md.exists():
                dest_md = inbox_dir / f"{md_filename[:-3]}_{c}.md"
                c += 1

            tag_list = "\n".join(f"  - {t}" for t in ["evernote"] + tags)
            frontmatter = (
                f"---\n"
                f"source: evernote\n"
                f"notebook: \"{notebook_name}\"\n"
                f"tags:\n{tag_list}\n"
                f"erstellt_am: \"{created[:8] or 'unbekannt'}\"\n"
                f"---\n\n"
                f"# {title}\n\n{text}\n"
            )
            dest_md.write_text(frontmatter, encoding="utf-8")
            log.info(f"ENEX Track B: {dest_md.name}")

    # ENEX nach Verarbeitung in done/ verschieben
    try:
        shutil.move(str(enex_path), str(done_dir / enex_path.name))
    except Exception as e:
        log.warning(f"ENEX verschieben nach done/ fehlgeschlagen: {e}")

    log.info(f"ENEX abgeschlossen: {enex_path.name}")


def process_file(file_path: Path):
    if file_path.suffix.lower() != ".pdf":
        return

    _fn = file_path.name
    log.info(f"Neue Datei: {_fn}")
    _step_emit(_fn, "started", "Verarbeitung gestartet", "done")

    # Duplikat-Check gegen pdf-archiv — im Batch-Modus übersprungen,
    # weil die Quelle typischerweise selbst im Archiv liegt (keine Selbst-Löschung).
    if not _batch_active() and VAULT_PDF_ARCHIV and (VAULT_PDF_ARCHIV / file_path.name).exists():
        log.info(f"Bereits in pdf-archiv: {_fn} — überspringe")
        tg_send(f"ℹ️ Bereits in pdf-archiv vorhanden — übersprungen\n<code>{_fn}</code>")
        file_path.unlink()
        return

    # Stabilitäts-Check nur für Watch-Mode (neu eintreffende Dateien).
    if not _batch_active() and not wait_for_file_stable(file_path):
        log.warning(f"Datei nicht stabil: {_fn}")
        tg_send(f"⚠️ Datei nicht stabil (Transfer abgebrochen?)\n<code>{_fn}</code>")
        return

    # ── Wilson-Sidecar-Bypass ──────────────────────────────────────────────────
    # Wenn Wilson das Dokument vorverarbeitet hat, liegt eine .meta.json neben dem PDF.
    # In diesem Fall: OCR + LLM auf Ryzen überspringen, Sidecar-Daten direkt verwenden.
    sidecar_path = file_path.parent / (file_path.stem + ".meta.json")
    if sidecar_path.exists() and not _batch_active():
        log.info(f"Wilson-Sidecar gefunden: {sidecar_path.name} — Bypass-Modus aktiv")
        _step_emit(_fn, "wilson_bypass", "Wilson-Sidecar-Bypass", "running")
        try:
            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Sidecar parse-Fehler: {e} — falle auf normale Pipeline zurück")
            sidecar_data = None

        if sidecar_data and sidecar_data.get("version") == "2.0":
            dok = sidecar_data.get("dokument", {})
            verarb = sidecar_data.get("verarbeitung", {})

            # Datum YYYY-MM-DD → DD.MM.YYYY
            datum_raw = dok.get("datum", "")
            rechnungsdatum = None
            if datum_raw and re.match(r"\d{4}-\d{2}-\d{2}", datum_raw):
                y, m, d = datum_raw.split("-")
                rechnungsdatum = f"{d}.{m}.{y}"

            kategorie_id = dok.get("kategorie_id", "")
            categories = load_categories()
            category_label = categories.get(kategorie_id, {}).get("label", kategorie_id) if categories else kategorie_id
            absender  = dok.get("absender", "") or "–"
            adressat  = dok.get("adressat", "Reinhard") or "Reinhard"
            beschreibung = dok.get("beschreibung", "")
            # Dateiname aus Sidecar als autoritativer Stem (ohne .pdf)
            force_stem = Path(dok.get("dateiname", file_path.name)).stem or file_path.stem

            result_bypass = {
                "absender":           absender,
                "adressat":           adressat,
                "rechnungsdatum":     rechnungsdatum,
                "category_id":        kategorie_id,
                "category_label":     category_label,
                "type_id":            None,
                "type_label":         None,
                "beschreibung":       beschreibung,
                "konfidenz":          "hoch",
                "konfidenz_category": "hoch",
                "konfidenz_absender": "hoch",
                "konfidenz_adressat": "hoch",
                "konfidenz_datum":    "hoch" if rechnungsdatum else "niedrig",
                "_wilson_bypass":     True,
                "_force_stem":        force_stem,
            }

            _step_emit(_fn, "ocr",    "OCR / Docling",             "skip")
            _step_emit(_fn, "header", "Header-Extraktion",         "skip")
            _step_emit(_fn, "identifiers", "Identifier-Extraktion","skip")
            _step_emit(_fn, "doctype","Dokumenttyp-Erkennung",     "skip")
            _step_emit(_fn, "lang",   "Spracherkennung",           "skip")
            _step_emit(_fn, "translate", "Übersetzung",            "skip")
            _step_emit(_fn, "llm",    "LLM-Klassifikation (Ollama)", "skip",
                       extracted={"reason": "Wilson-Sidecar", "kategorie_id": kategorie_id})
            _step_emit(_fn, "overrides", "Deterministisches Override", "skip")

            # Temp-MD: Beschreibung aus Sidecar als Vault-Inhalt
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem_safe = re.sub(r"[^\w\-]", "_", file_path.stem)
            temp_md_bp = TEMP_DIR / f"{timestamp}_{stem_safe}.md"
            wilson_ts = verarb.get("extrahiert_am", "")
            temp_md_bp.write_text(
                f"*Vorverarbeitet von Wilson am {wilson_ts}*\n\n{beschreibung}",
                encoding="utf-8",
            )

            _step_emit(_fn, "db", "Datenbank speichern", "running")
            match_infos_bp = save_to_db(file_path, result_bypass)
            save_klassifikation_historie(result_bypass.get("_dok_id"), result_bypass)
            _step_emit(_fn, "db", "Datenbank speichern", "done",
                       extracted={"dok_id": result_bypass.get("_dok_id"),
                                  "category_id": kategorie_id,
                                  "konfidenz": "hoch"})

            # Telegram
            tg_send_document(file_path)
            tg_lines = [
                f"✅ <b>Dokument von Wilson empfangen</b>",
                f"",
                f"📄 Datei:     <code>{force_stem}.pdf</code>",
                f"🏢 Absender:  🟢 {absender}",
                f"👤 Adressat:  🟢 {adressat}",
            ]
            if rechnungsdatum:
                tg_lines.append(f"📅 Datum:     🟢 {rechnungsdatum}")
            tg_lines += [
                f"🗂 Kategorie: 🟢 <b>{category_label}</b>",
                f"",
                f"📝 {beschreibung[:300]}",
                f"",
                f"🤖 Vorverarbeitet von Wilson — kein LLM auf Ryzen",
            ]
            tg_send("\n".join(tg_lines))
            log.info(f"Wilson-Bypass abgeschlossen: {file_path.name} → {kategorie_id}")

            _step_emit(_fn, "vault", "Vault-Move", "running")
            move_to_vault(file_path, temp_md_bp, kategorie_id, "", result_bypass)
            _step_emit(_fn, "vault", "Vault-Move", "done",
                       extracted={"vault_pfad": result_bypass.get("vault_pfad", "")})

            try:
                sidecar_path.unlink()
                log.info(f"Sidecar gelöscht: {sidecar_path.name}")
            except Exception as e:
                log.warning(f"Sidecar löschen fehlgeschlagen: {e}")

            return
        else:
            log.warning(f"Sidecar ungültig oder falsche Version — falle auf normale Pipeline zurück")
    # ── Ende Wilson-Sidecar-Bypass ─────────────────────────────────────────────

    # 1. PDF → Markdown via Docling (oder Cache, im Batch-Modus)
    _step_emit(_fn, "ocr", "OCR / Docling", "running")
    _t0 = time.monotonic()
    _batch_md_override = getattr(_batch_ctx, "md_override", None) if _batch_active() else None
    _batch_ocr_meta = getattr(_batch_ctx, "ocr_meta", None) if _batch_active() else None
    if _batch_md_override is not None:
        md_content = _batch_md_override
        _ocr_ms = float(_batch_ocr_meta.get("duration_ms", 0.0)) if _batch_ocr_meta else 0.0
        log.info(f"Batch-OCR: Quelle={_batch_ocr_meta.get('source') if _batch_ocr_meta else '?'} ({len(md_content)} chars)")
    else:
        md_content = convert_to_markdown(file_path)
        _ocr_ms = (time.monotonic() - _t0) * 1000
    if not md_content:
        _step_emit(_fn, "ocr", "OCR / Docling", "error", error="Docling fehlgeschlagen")
        tg_send(f"❌ Docling-Konvertierung fehlgeschlagen\n<code>{_fn}</code>")
        if _batch_active():
            setattr(_batch_ctx, "last_result", {"error": "Docling fehlgeschlagen", "ocr_meta": _batch_ocr_meta})
        return

    # OCR-Qualitäts-Gate: zu wenig Text → Inbox, Telegram-Warnung, kein LLM-Aufwand
    ocr_chars = len(md_content.strip())
    _step_emit(_fn, "ocr", "OCR / Docling", "done",
               extracted={"chars": ocr_chars, "preview": md_content[:800]},
               duration_ms=_ocr_ms)
    if ocr_chars < OCR_MIN_CHARS:
        _step_emit(_fn, "ocr_quality", "OCR-Qualitäts-Gate", "error",
                   error=f"Nur {ocr_chars} Zeichen erkannt (Minimum: {OCR_MIN_CHARS}) → Inbox")
        log.warning(f"OCR-Qualität unzureichend ({ocr_chars} Zeichen): {_fn}")
        tg_send(
            f"⚠️ <b>OCR-Qualität unzureichend</b> — Datei in Inbox\n"
            f"<code>{_fn}</code>\n"
            f"Nur {ocr_chars} Zeichen erkannt (Minimum: {OCR_MIN_CHARS})"
        )
        timestamp_ocr = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem_ocr = re.sub(r"[^\w\-]", "_", file_path.stem)
        temp_md_ocr = TEMP_DIR / f"{timestamp_ocr}_{stem_ocr}.md"
        temp_md_ocr.write_text(md_content, encoding="utf-8")
        move_to_vault(file_path, temp_md_ocr, "", "", {})
        if _batch_active():
            setattr(_batch_ctx, "last_result", {"error": f"OCR-Qualität unzureichend ({ocr_chars} Zeichen)", "ocr_meta": _batch_ocr_meta})
        return
    _step_emit(_fn, "ocr_quality", "OCR-Qualitäts-Gate", "done",
               extracted={"chars": ocr_chars})

    # 2. Markdown in TEMP speichern
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r"[^\w\-]", "_", file_path.stem)
    temp_md = TEMP_DIR / f"{timestamp}_{stem}.md"
    temp_md.write_text(md_content, encoding="utf-8")
    log.info(f"Markdown gespeichert: {temp_md.name}")

    # 2b. Header-Extraktion (regex, deterministisch — vor Übersetzung, damit Originalnamen erhalten bleiben)
    _t0 = time.monotonic()
    header_info = extract_document_header(md_content)
    _step_emit(_fn, "header", "Header-Extraktion", "done",
               extracted={"absender": header_info.get("absender", {}),
                          "empfaenger": header_info.get("empfaenger", {})},
               duration_ms=(time.monotonic() - _t0) * 1000)
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
    _t0 = time.monotonic()
    identifiers = extract_identifiers(md_content)
    adressat_match = resolve_adressat(identifiers, md_content)
    absender_match = resolve_absender(identifiers, header_info)
    _step_emit(_fn, "identifiers", "Identifier-Extraktion & Personen-Auflösung", "done",
               extracted={"identifiers": identifiers,
                          "adressat": adressat_match,
                          "absender_match": absender_match},
               duration_ms=(time.monotonic() - _t0) * 1000)
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
    _t0 = time.monotonic()
    doc_type_info = extract_document_type(md_content)
    _step_emit(_fn, "doctype", "Dokumenttyp-Erkennung", "done",
               extracted={"typ": doc_type_info.get("erkannter_typ"),
                          "keyword": doc_type_info.get("quell_keyword"),
                          "kategorie_hint": doc_type_info.get("kategorie_hint")},
               duration_ms=(time.monotonic() - _t0) * 1000)
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
    _t0 = time.monotonic()
    lang, lang_prob = detect_document_language(md_content)
    _step_emit(_fn, "lang", "Spracherkennung", "done",
               extracted={"lang": lang, "prob": round(lang_prob, 3)},
               duration_ms=(time.monotonic() - _t0) * 1000)
    if lang != "de" and lang_prob >= 0.85:
        log.info(f"Nicht-deutsches Dokument erkannt: {lang} (p={lang_prob:.2f}) — übersetze nach DE")
        _step_emit(_fn, "translate", f"Übersetzung {lang}→DE", "running")
        _t0 = time.monotonic()
        translated = translate_to_german(md_content, lang)
        if translated:
            classify_input = translated
            _step_emit(_fn, "translate", f"Übersetzung {lang}→DE", "done",
                       extracted={"chars": len(translated), "ocr_chars": ocr_chars,
                                  "input_limit": 6000, "preview": translated[:400]},
                       duration_ms=(time.monotonic() - _t0) * 1000)
            trans_path = temp_md.with_suffix(f".translation.{OLLAMA_TRANSLATE_MODEL.replace(':', '_').replace('/', '_')}.md")
            trans_path.write_text(
                f"<!-- Übersetzung {lang}→de via {OLLAMA_TRANSLATE_MODEL} -->\n\n{translated}",
                encoding="utf-8",
            )
            log.info(f"Übersetzung ok ({len(translated)} chars) → {trans_path.name}")
        else:
            _step_emit(_fn, "translate", f"Übersetzung {lang}→DE", "error",
                       error="Übersetzung fehlgeschlagen — klassifiziere auf Originaltext")
            log.warning("Übersetzung fehlgeschlagen — klassifiziere auf Originaltext")
    else:
        _step_emit(_fn, "translate", "Übersetzung", "skip")

    # 4. Klassifizierung via Ollama
    categories = load_categories()
    if not categories:
        tg_send(f"❌ Keine Kategorien konfiguriert\n<code>{file_path.name}</code>")
        return

    _step_emit(_fn, "llm", "LLM-Klassifikation (Ollama)", "running")
    _t0 = time.monotonic()
    result = classify_with_ollama(
        classify_input,
        categories,
        header=header_info,
        identifiers=identifiers,
        adressat_match=adressat_match,
        absender_match=absender_match,
        doc_type_info=doc_type_info,
    )
    _step_emit(_fn, "llm", "LLM-Klassifikation (Ollama)",
               "done" if result else "error",
               extracted={k: v for k, v in (result or {}).items() if not k.startswith("_")},
               duration_ms=(time.monotonic() - _t0) * 1000)

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
        # Cod.Fiscale-Match ist ein harter Fakt → Konfidenz ist nicht geraten sondern belegt.
        result["konfidenz_adressat"] = "hoch"
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
        # adressat_default ist eine Regel, kein Ratespiel → Konfidenz auf hoch setzen.
        result["konfidenz_adressat"] = "hoch"

    # Datum-Konfidenz hochsetzen bei bekanntem Absender + Leistungsabrechnung:
    # LAs enthalten viele Behandlungsdaten → LLM zögert, wählt aber immer das Abrechnungsdatum.
    # Wenn Absender sicher erkannt (absender_match) und type=leistungsabrechnung und Datum vorhanden,
    # ist das Datum ein Fakt — nicht mehr geraten.
    if (result
            and absender_match
            and result.get("type_id") in LEISTUNGSABRECHNUNG_TYPES
            and result.get("rechnungsdatum")
            and result.get("konfidenz_datum") == "mittel"):
        result["konfidenz_datum"] = "hoch"
        log.info(f"konfidenz_datum auf hoch gesetzt (bekannter LA-Absender, Datum vorhanden)")

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

    _step_emit(_fn, "overrides", "Deterministisches Override", "done",
               extracted={"category_id": result.get("category_id") if result else None,
                          "type_id": result.get("type_id") if result else None,
                          "adressat": result.get("adressat") if result else None,
                          "absender": result.get("absender") if result else None,
                          "konfidenz_adressat": result.get("konfidenz_adressat") if result else None})

    if not result or not result.get("category_id"):
        tg_send(
            f"⚠️ <b>Klassifizierung nicht möglich — Datei in Inbox</b>\n"
            f"Datei: <code>{_fn}</code>"
        )
        log.info(f"Klassifizierung fehlgeschlagen für: {_fn} — verschiebe in Inbox")
        move_to_vault(file_path, temp_md, "", "", {})

        if _batch_active():
            batch_result = dict(result or {})
            batch_result["_ocr_meta"] = _batch_ocr_meta
            batch_result["error"] = "Klassifizierung fehlgeschlagen"
            setattr(_batch_ctx, "last_result", batch_result)
        else:
            # Minimaler DB-Eintrag, damit Inbox-Docs im Review-Dashboard auftauchen.
            # kategorie=NULL → vom /review-Filter als Inbox erkannt.
            try:
                save_to_db(file_path, {"konfidenz": "niedrig"})
            except Exception as e:
                log.warning(f"Inbox-DB-Eintrag fehlgeschlagen für {_fn}: {e}")
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
    categories = load_categories()
    result = apply_keyword_rules(result, md_content, categories)
    result = apply_lernregeln_from_db(result, md_content, result.get("absender"), categories)
    result["konfidenz"] = aggregate_konfidenz(result)

    # Datums-Fallback: falls LLM kein Datum liefert → Dateiname-Prefix verwenden
    if not result.get("rechnungsdatum"):
        date_yyyymmdd = _date_from_filename_prefix(file_path.stem)
        if date_yyyymmdd and len(date_yyyymmdd) == 8:
            yyyy, mm, dd = date_yyyymmdd[:4], date_yyyymmdd[4:6], date_yyyymmdd[6:]
            result["rechnungsdatum"] = f"{dd}.{mm}.{yyyy}"
            result.setdefault("konfidenz_datum", "mittel")
            log.info(f"Datum-Fallback aus Dateiname: {result['rechnungsdatum']} ({file_path.name})")

    # Konfidenz "niedrig" → 00 Inbox (kein Raten in den Vault)
    # Nur in Inbox wenn Kategorie selbst unsicher
    if result.get("konfidenz_category") == "niedrig" or (
        not result.get("konfidenz_category") and result.get("konfidenz") == "niedrig"
    ):
        log.info(f"Konfidenz Kategorie niedrig → 00 Inbox: {file_path.name}")
        result["category_id"] = None
        result["type_id"] = None

    _step_emit(_fn, "db", "Datenbank speichern", "running")
    _t0 = time.monotonic()
    match_infos = save_to_db(file_path, result)
    save_klassifikation_historie(result.get("_dok_id"), result)
    _step_emit(_fn, "db", "Datenbank speichern", "done",
               extracted={"dok_id": result.get("_dok_id"),
                          "konfidenz": result.get("konfidenz"),
                          "category_id": result.get("category_id"),
                          "type_id": result.get("type_id"),
                          "match_count": len(match_infos) if match_infos else 0},
               duration_ms=(time.monotonic() - _t0) * 1000)

    # Hash-Duplikat: PDF löschen, kurze Benachrichtigung, kein weiterer Vault-Move
    if result.get("_is_hash_duplicate"):
        dup_name = file_path.name
        if _batch_active():
            log.info(f"Hash-Duplikat (Batch-Modus): {dup_name} — markiert, nicht gelöscht")
            batch_result = dict(result)
            batch_result["_ocr_meta"] = _batch_ocr_meta
            batch_result["error"] = "hash_duplicate"
            setattr(_batch_ctx, "last_result", batch_result)
        else:
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

    # SSE — Live-Update ans Dashboard
    _vault_pfad = result.get("vault_pfad", "")
    _pdf_name = _safe_pdf_name_from_vault_pfad(_vault_pfad, file_path.name) if _vault_pfad else file_path.name
    sse_broadcast("doc_processed", {
        "dateiname":      file_path.name,
        "pdf_name":       _pdf_name,
        "rechnungsdatum": rechnungsdatum,
        "kategorie":      category_id,
        "typ":            type_id,
        "absender":       absender,
        "adressat":       adressat,
        "konfidenz":      result.get("konfidenz"),
        "vault_pfad":     _vault_pfad,
        "erstellt_am":    datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    # 7. Dateien in Vault verschieben
    _step_emit(_fn, "vault", "Vault-Move", "running")
    _t0 = time.monotonic()
    move_to_vault(file_path, temp_md, category_id, type_id, result)
    _step_emit(_fn, "vault", "Vault-Move", "done",
               extracted={"vault_pfad": result.get("vault_pfad", "")},
               duration_ms=(time.monotonic() - _t0) * 1000)

    if _batch_active():
        batch_result = dict(result)
        batch_result["_ocr_meta"] = _batch_ocr_meta
        setattr(_batch_ctx, "last_result", batch_result)


# ── Queue-Worker ───────────────────────────────────────────────────────────────

def queue_worker():
    while True:
        item = file_queue.get()
        try:
            if isinstance(item, tuple) and item[0] == "rescan":
                rescan_archived_pdf(item[1])
            elif isinstance(item, tuple) and item[0] == "rescan_dated_de":
                rescan_archived_pdf(item[1], language_filter="de")
            elif isinstance(item, tuple) and item[0] == "enex":
                process_enex_file(item[1])
            else:
                process_file(item)
        except FileNotFoundError as e:
            log.warning(f"Datei nicht mehr vorhanden (bereits gelöscht?): {item} — {e}")
        except Exception as e:
            import traceback
            log.error(f"Unerwarteter Fehler bei {item}: {e}\n{traceback.format_exc()}")
        finally:
            file_queue.task_done()


# ── Watchdog ───────────────────────────────────────────────────────────────────

class DocumentHandler(FileSystemEventHandler):
    """Watchdog-Handler für WATCH_DIR: verarbeitet .pdf und .enex Dateien."""
    def _enqueue(self, path: Path):
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            log.info(f"In Queue (PDF): {path.name}")
            file_queue.put(path)
        elif suffix == ".enex":
            log.info(f"In Queue (ENEX): {path.name}")
            file_queue.put(("enex", path))

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._enqueue(Path(event.dest_path))


# Rückwärtskompatibilität
PdfHandler = DocumentHandler


# ── Batch-Modus ────────────────────────────────────────────────────────────────

def _vault_relative_path(abs_path: Path) -> str | None:
    """Gibt den Vault-relativen Pfad zurück, falls möglich.
    Der cache-reader speichert Pfade relativ zum Vault-Root."""
    if not VAULT_ROOT:
        return None
    try:
        rel = abs_path.resolve().relative_to(VAULT_ROOT.resolve())
        return str(rel)
    except Exception:
        return None


def _cache_lookup(rel_path: str) -> dict | None:
    """Fragt den cache-reader-Service nach einem Cache-Eintrag für rel_path.
    Gibt None zurück bei Miss oder Fehler (Logging, kein Throw)."""
    try:
        r = requests.get(
            f"{CACHE_READER_URL}/file",
            params={"path": rel_path},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        log.warning(f"Cache-Reader /file fehlerhaft ({r.status_code}): {r.text[:200]}")
        return None
    except Exception as e:
        log.warning(f"Cache-Reader /file nicht erreichbar: {e}")
        return None


def resolve_ocr_text(pdf_path: Path, mode: str, cache_hint: str | None = None) -> tuple[str | None, dict]:
    """Liefert OCR-Text für ein PDF gemäß OCR-Modus.

    mode: 'cache' | 'docling' | 'hybrid'
    cache_hint: Vault-relativer MD-Pfad, wenn bekannt (aus cache-reader-Suchtreffer).
                Der cache-reader indiziert Markdown-Dateien, nicht PDFs — deshalb
                muss der Cache-Key der MD-Pfad sein, nicht der PDF-Pfad.
    Rückgabe: (text, meta) mit meta = {source, chars, lang, duration_ms, cache_path?}.
    text ist None bei Fehlschlag (z. B. cache-only und Cache-Miss).
    """
    meta: dict = {"source": None, "chars": 0, "lang": None, "duration_ms": 0.0}

    if mode == "docling":
        t0 = time.monotonic()
        text = convert_to_markdown(pdf_path)
        meta.update({
            "source": "docling",
            "chars": len(text.strip()) if text else 0,
            "duration_ms": (time.monotonic() - t0) * 1000,
        })
        return text, meta

    # Cache-Pfad: primär cache_hint (z. B. aus cache-reader-Output), sonst Vault-relativer PDF-Pfad.
    # cache-reader indiziert PDF-Pfade als Keys — kein .pdf→.md-Mapping nötig.
    rel = cache_hint or _vault_relative_path(pdf_path)
    cache_entry: dict | None = None
    if rel:
        t0 = time.monotonic()
        cache_entry = _cache_lookup(rel)
        meta["cache_lookup_ms"] = (time.monotonic() - t0) * 1000
        meta["cache_path"] = rel
    else:
        meta["cache_path"] = None

    if cache_entry:
        text = cache_entry.get("text") or ""
        langs = cache_entry.get("langs")
        # cache-reader liefert langs als einfachen String ("de"), seltener als Liste/Dict.
        lang: str | None = None
        if isinstance(langs, str):
            lang = langs.split(",")[0].strip() or None
        elif isinstance(langs, list) and langs:
            first = langs[0]
            if isinstance(first, dict):
                lang = first.get("lang")
            elif isinstance(first, str):
                lang = first
        elif isinstance(langs, dict):
            lang = langs.get("lang")
        meta["chars"] = len(text.strip())
        meta["lang"] = lang

        if mode == "cache":
            meta["source"] = "cache" if meta["chars"] > 0 else "cache_empty"
            return (text if meta["chars"] > 0 else None), meta

        # Hybrid: Gate
        lang_ok = lang in HYBRID_OCR_LANGS if lang else False
        chars_ok = meta["chars"] >= HYBRID_OCR_MIN_CHARS
        if chars_ok and lang_ok:
            meta["source"] = "cache"
            return text, meta
        # Gate-Fail → Fallback auf Docling
        meta["cache_gate_fail_reason"] = (
            f"chars={meta['chars']}<{HYBRID_OCR_MIN_CHARS}" if not chars_ok else f"lang={lang}"
        )
    else:
        if mode == "cache":
            meta["source"] = "cache_miss"
            return None, meta
        meta["cache_gate_fail_reason"] = "miss"

    # Hybrid-Fallback oder kein Cache-Treffer → Docling
    t0 = time.monotonic()
    text = convert_to_markdown(pdf_path)
    meta["source"] = "docling_fallback" if cache_entry else "docling"
    meta["chars"] = len(text.strip()) if text else 0
    meta["duration_ms"] = (time.monotonic() - t0) * 1000
    return text, meta


def _parse_batch_input(input_path: Path) -> list[tuple[Path, str | None]]:
    """Akzeptiert sowohl cache-reader-Output-JSON (`{"results":[{"path":...}]}`)
    als auch JSON-Array-Listen oder flache Textdateien (eine PDF pro Zeile).
    Rückgabe: Liste von (pdf_path, cache_hint). cache_hint ist der ursprüngliche
    Eintrag (typisch MD-Pfad aus cache-reader), falls Vault-relativ erkennbar."""
    if not input_path.exists():
        raise FileNotFoundError(f"Batch-Input nicht gefunden: {input_path}")

    text = input_path.read_text(encoding="utf-8").strip()
    paths: list[tuple[Path, str | None]] = []

    def _append(entry_str: str):
        pdf = _resolve_batch_entry_to_pdf(entry_str)
        if pdf:
            hint = _derive_cache_hint(entry_str)
            paths.append((pdf, hint))

    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, dict) and "results" in data:
            entries = data["results"]
        elif isinstance(data, list):
            entries = data
        else:
            raise ValueError(f"Unbekanntes JSON-Batch-Format in {input_path}")
        for entry in entries:
            p = entry.get("path") if isinstance(entry, dict) else entry
            if not p:
                continue
            _append(str(p))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            _append(line)
    return paths


def _derive_cache_hint(entry: str) -> str | None:
    """Gibt den Vault-relativen Pfad zurück, unter dem der cache-reader diesen
    Eintrag indiziert hat. cache-reader nutzt PDF-Pfade als Keys, also reichen
    wir den Eintrag unverändert durch (relativ) bzw. kürzen ihn um den Vault-Root (absolut)."""
    p = Path(entry)
    if p.is_absolute():
        if VAULT_ROOT:
            try:
                rel = p.resolve().relative_to(VAULT_ROOT.resolve())
                return str(rel)
            except Exception:
                return None
        return None
    return str(p)


def _resolve_batch_entry_to_pdf(entry: str) -> Path | None:
    """Wandelt einen Eintrag (MD-Pfad relativ zum Vault, absoluter Pfad, oder PDF-Name)
    in den absoluten Pfad zur PDF-Datei im Anlagen-Archiv um.
    cache-reader liefert typischerweise Vault-relative MD-Pfade."""
    p = Path(entry)
    if p.is_absolute() and p.exists() and p.suffix.lower() == ".pdf":
        return p

    candidate_names: list[str] = []
    if p.suffix.lower() == ".md":
        candidate_names.append(p.stem + ".pdf")
    elif p.suffix.lower() == ".pdf":
        candidate_names.append(p.name)
    else:
        candidate_names.append(p.name + ".pdf")

    if VAULT_PDF_ARCHIV and VAULT_PDF_ARCHIV.exists():
        for name in candidate_names:
            cand = VAULT_PDF_ARCHIV / name
            if cand.exists():
                return cand
    if VAULT_ROOT:
        cand = VAULT_ROOT / entry
        if cand.exists() and cand.suffix.lower() == ".pdf":
            return cand
    log.warning(f"Batch-Eintrag nicht auflösbar: {entry}")
    return None


def _batch_run_start(input_source: str, ocr_mode: str, output_mode: str,
                     output_dir: str | None, total: int) -> int:
    """Legt einen neuen batch_runs-Eintrag an und gibt die run_id zurück."""
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO batch_runs (input_source, ocr_mode, output_mode, output_dir, "
            "status, total, processed, errors, started_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, 0, 0, datetime('now','localtime'))",
            (input_source, ocr_mode, output_mode, output_dir, total),
        )
        return cur.lastrowid


def _batch_run_finish(run_id: int, status: str):
    with get_db() as con:
        con.execute(
            "UPDATE batch_runs SET status=?, finished_at=datetime('now','localtime') WHERE id=?",
            (status, run_id),
        )


def _batch_item_record(run_id: int, pdf_path: Path, status: str,
                       ocr_meta: dict | None = None,
                       result: dict | None = None,
                       error: str | None = None,
                       result_path: str | None = None):
    """Persistiert das volle Item-Ergebnis (inkl. OCR-Meta + Klassifikation).
    Bevorzugter Zugriffspfad für das Dashboard — CSV/JSONL sind nur Export."""
    ocr_meta = ocr_meta or {}
    result = result or {}
    with get_db() as con:
        con.execute(
            "INSERT INTO batch_items (run_id, doc_path, status, ocr_source, ocr_chars, lang, "
            "kategorie, typ, absender, adressat, rechnungsdatum, rechnungsbetrag, konfidenz, "
            "result_path, ocr_meta_json, error, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
            (
                run_id,
                str(pdf_path),
                status,
                ocr_meta.get("source"),
                ocr_meta.get("chars"),
                ocr_meta.get("lang") or result.get("_lang"),
                result.get("category_id"),
                result.get("type_id"),
                result.get("absender"),
                result.get("adressat"),
                result.get("rechnungsdatum"),
                result.get("rechnungsbetrag"),
                result.get("konfidenz"),
                result_path,
                json.dumps(ocr_meta, ensure_ascii=False, default=str) if ocr_meta else None,
                error,
            ),
        )
        con.execute(
            "UPDATE batch_runs SET processed=processed+1, "
            "errors=errors+CASE WHEN ?='error' THEN 1 ELSE 0 END WHERE id=?",
            (status, run_id),
        )


# Per-run Kontrolle (Pause/Abort) — run_id -> "running"|"paused"|"aborted"
_batch_controls: dict[int, str] = {}
_batch_controls_lock = threading.Lock()


def _batch_control_get(run_id: int) -> str:
    with _batch_controls_lock:
        return _batch_controls.get(run_id, "running")


def _batch_control_set(run_id: int, state: str):
    with _batch_controls_lock:
        _batch_controls[run_id] = state


def run_batch(input_path: Path, ocr_mode: str, output_mode: str,
              output_dir: Path | None, limit: int = 0, dry_run: bool = False,
              run_id: int | None = None) -> dict:
    """Führt einen Batch-Lauf aus (CLI-Einstiegspunkt + Dashboard-Worker).

    Schreibt bei structured einen CSV-Summary und ein JSONL-Details-File.
    Gibt Summary-Dict mit processed/errors/run_id zurück.
    """
    init_db()
    categories = load_categories()
    if not categories:
        raise RuntimeError("Keine Kategorien konfiguriert")

    # OCR-Modi, die Docling brauchen, prüfen Erreichbarkeit
    if ocr_mode in ("docling", "hybrid"):
        if not wait_for_docling(max_retries=3, delay=5):
            log.warning("Docling nicht erreichbar — hybrid-Modus wird Cache-only arbeiten")

    entries = _parse_batch_input(input_path)
    if limit > 0:
        entries = entries[:limit]
    total = len(entries)
    log.info(f"Batch-Lauf: {total} Dokumente · ocr={ocr_mode} · output={output_mode} · dry_run={dry_run}")

    if dry_run:
        for p, hint in entries:
            log.info(f"[dry-run] {p}  (cache_hint={hint})")
        return {"run_id": None, "total": total, "processed": 0, "errors": 0, "dry_run": True}

    if run_id is None:
        run_id = _batch_run_start(str(input_path), ocr_mode, output_mode,
                                  str(output_dir) if output_dir else None, total)

    # Exporte öffnen
    summary_path: Path | None = None
    details_path: Path | None = None
    summary_fp = None
    details_fp = None
    csv_writer = None
    if output_mode == "structured":
        target_dir = output_dir or (TEMP_DIR / f"batch_run_{run_id}")
        target_dir.mkdir(parents=True, exist_ok=True)
        summary_path = target_dir / f"run_{run_id}_summary.csv"
        details_path = target_dir / f"run_{run_id}_details.jsonl"
        import csv as _csv
        summary_fp = summary_path.open("w", encoding="utf-8", newline="")
        details_fp = details_path.open("w", encoding="utf-8")
        csv_writer = _csv.writer(summary_fp)
        csv_writer.writerow([
            "path", "kategorie", "typ", "absender", "adressat",
            "rechnungsdatum", "rechnungsbetrag", "konfidenz", "lang",
            "ocr_source", "ocr_chars", "error",
        ])

    processed = 0
    errors = 0
    try:
        for pdf_path, cache_hint in entries:
            # Kontrolle prüfen
            state = _batch_control_get(run_id)
            while state == "paused":
                time.sleep(1)
                state = _batch_control_get(run_id)
            if state == "aborted":
                log.info(f"Batch {run_id} abgebrochen bei {pdf_path.name}")
                break

            log.info(f"Batch {run_id} [{processed+1}/{total}]: {pdf_path.name}  (cache_hint={cache_hint})")
            # OCR-Text ermitteln
            try:
                md_text, ocr_meta = resolve_ocr_text(pdf_path, ocr_mode, cache_hint=cache_hint)
            except Exception as e:
                log.error(f"OCR-Fehler bei {pdf_path.name}: {e}")
                errors += 1
                _batch_item_record(run_id, pdf_path, "error", error=f"OCR: {e}")
                continue

            if md_text is None:
                errors += 1
                reason = ocr_meta.get("source", "ocr_failed")
                _batch_item_record(run_id, pdf_path, "error", ocr_meta=ocr_meta, error=reason)
                if csv_writer:
                    csv_writer.writerow([str(pdf_path), "", "", "", "", "", "", "", "", reason, 0, reason])
                continue

            # Batch-Kontext setzen: schleust OCR-Text ein, unterdrückt Telegram, steuert move_to_vault
            _batch_ctx.active = True
            _batch_ctx.run_id = run_id
            _batch_ctx.ocr_mode = ocr_mode
            _batch_ctx.output_mode = output_mode
            _batch_ctx.md_override = md_text
            _batch_ctx.ocr_meta = ocr_meta
            _batch_ctx.last_result = None
            try:
                process_file(pdf_path)
                result = getattr(_batch_ctx, "last_result", None) or {}
            except Exception as e:
                log.error(f"Pipeline-Fehler bei {pdf_path.name}: {e}")
                errors += 1
                _batch_item_record(run_id, pdf_path, "error", ocr_meta=ocr_meta, error=str(e))
                continue
            finally:
                _batch_ctx.active = False
                _batch_ctx.md_override = None
                _batch_ctx.ocr_meta = None

            err = result.get("error") if isinstance(result, dict) else None
            status = "error" if err else "done"
            if err:
                errors += 1
            processed += 1
            _batch_item_record(
                run_id, pdf_path, status,
                ocr_meta=ocr_meta, result=result,
                result_path=(str(summary_path) if summary_path else None),
                error=err,
            )

            # Exporte schreiben
            if csv_writer:
                csv_writer.writerow([
                    str(pdf_path),
                    result.get("category_id", "") or "",
                    result.get("type_id", "") or "",
                    result.get("absender", "") or "",
                    result.get("adressat", "") or "",
                    result.get("rechnungsdatum", "") or "",
                    result.get("rechnungsbetrag", "") or "",
                    result.get("konfidenz", "") or "",
                    result.get("_lang", "") or "",
                    ocr_meta.get("source", "") or "",
                    ocr_meta.get("chars", 0) or 0,
                    err or "",
                ])
                summary_fp.flush()
            if details_fp:
                details_fp.write(json.dumps({
                    "path": str(pdf_path),
                    "ocr_meta": ocr_meta,
                    "result": {k: v for k, v in result.items() if not k.startswith("_dok")},
                }, ensure_ascii=False, default=str) + "\n")
                details_fp.flush()

    finally:
        if summary_fp:
            summary_fp.close()
        if details_fp:
            details_fp.close()

    final_status = "aborted" if _batch_control_get(run_id) == "aborted" else (
        "error" if errors and processed == 0 else "done"
    )
    _batch_run_finish(run_id, final_status)
    log.info(f"Batch {run_id} abgeschlossen: processed={processed} errors={errors} status={final_status}")
    return {
        "run_id": run_id,
        "total": total,
        "processed": processed,
        "errors": errors,
        "status": final_status,
        "summary_csv": str(summary_path) if summary_path else None,
        "details_jsonl": str(details_path) if details_path else None,
    }


def _dashboard_batch_runner(run_id: int, input_path: Path, ocr_mode: str,
                            output_mode: str, output_dir: Path | None, limit: int):
    """Hintergrund-Thread für Dashboard-getriggerte Läufe.
    run_id wurde bereits vom API-Endpoint angelegt — run_batch() nutzt ihn wieder."""
    try:
        run_batch(
            input_path=input_path,
            ocr_mode=ocr_mode,
            output_mode=output_mode,
            output_dir=output_dir,
            limit=limit,
            dry_run=False,
            run_id=run_id,
        )
    except Exception as e:
        log.error(f"Dashboard-Batch {run_id} fehlgeschlagen: {e}")
        try:
            _batch_run_finish(run_id, "error")
        except Exception:
            pass


# ── Main ───────────────────────────────────────────────────────────────────────

def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dispatcher",
        description="Document Dispatcher — Watch-Daemon (default) oder --batch Einmallauf.",
    )
    p.add_argument("--batch", metavar="INPUT",
                   help="Pfad zu Batch-Input (JSON aus cache-reader oder Textliste, eine PDF pro Zeile).")
    p.add_argument("--ocr-source", choices=["cache", "docling", "hybrid"], default="hybrid",
                   help="OCR-Quelle: cache|docling|hybrid (Default: hybrid).")
    p.add_argument("--output", choices=["vault-move", "classify-only", "structured"],
                   default="vault-move", help="Ausgabemodus (Default: vault-move).")
    p.add_argument("--output-dir", metavar="DIR",
                   help="Zielordner für CSV/JSONL bei --output structured.")
    p.add_argument("--limit", type=int, default=0, help="Maximalanzahl Dokumente (0 = alle).")
    p.add_argument("--dry-run", action="store_true", help="Nur Eingabe auflösen und auflisten.")
    p.add_argument("--resume", type=int, metavar="RUN_ID",
                   help="An bestehenden Batch-Lauf anknüpfen (noch nicht implementiert).")
    return p


# ── Duplikat-Erkennung ─────────────────────────────────────────────────────────

_DEDUP_SCAN_LOCK = threading.Lock()
_DEDUP_SCAN_STATUS: dict = {"running": False, "scan_id": None}

_DEDUP_MOVE_LOCK = threading.Lock()
_DEDUP_MOVE_STATUS: dict = {
    "running": False, "total": 0, "processed": 0, "errors": 0,
    "started_at": None, "finished_at": None, "last_error": None,
}

_DEDUP_SKIP_DIRS = {"00 Duplikate", "00 Text-Duplikate"}


def _extract_amounts(text: str) -> set[str]:
    """Extrahiert normalisierte Geldbeträge aus OCR-Text."""
    amounts: set[str] = set()
    for m in re.finditer(
        r'(?:EUR|€)\s*\d[\d.,]*|\d[\d.,]*\s*(?:EUR|€)|\d{1,3}(?:[.,]\d{3})+[.,]\d{2}',
        text, re.IGNORECASE
    ):
        # Normalize: extract digits + last 2 decimal places
        raw = re.sub(r'\s+', '', m.group(0).upper().replace('€', 'EUR'))
        amounts.add(raw)
    return amounts


def _metadata_for_pdf(pdf_name: str) -> dict:
    """Holt (datum, absender, vault_pfad) für einen PDF-Dateinamen.

    Priorität: DB → MD-Frontmatter → Filename-Prefix.
    """
    res = {"datum": None, "absender": None, "vault_pfad": None, "source": None}
    # 1. DB
    with get_db() as con:
        row = con.execute(
            "SELECT rechnungsdatum, absender, vault_pfad FROM dokumente "
            "WHERE anlagen_dateiname=? OR dateiname LIKE ?",
            (pdf_name, f"%{pdf_name}%")
        ).fetchone()
    if row:
        res.update({"datum": row["rechnungsdatum"], "absender": row["absender"],
                    "vault_pfad": row["vault_pfad"], "source": "db"})
    # 2. MD-Frontmatter (neues Format: datum/absender; altes Format: date/title)
    if VAULT_ROOT and res.get("vault_pfad"):
        md = VAULT_ROOT / res["vault_pfad"]
        if md.exists():
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        for line in content[3:end].splitlines():
                            if ":" not in line:
                                continue
                            k, _, v = line.partition(":")
                            k, v = k.strip(), v.strip().strip("\"'")
                            # neues Format
                            if k == "datum" and not res["datum"]:
                                res["datum"] = v; res["source"] = "frontmatter"
                            elif k == "absender" and not res["absender"]:
                                res["absender"] = v; res["source"] = "frontmatter"
                            # altes Format (Obsidian-Import)
                            elif k == "date" and not res["datum"]:
                                # date: YYYY-MM-DD → DD.MM.YYYY
                                dm = re.match(r'(\d{4})-(\d{2})-(\d{2})', v)
                                if dm:
                                    res["datum"] = f"{dm.group(3)}.{dm.group(2)}.{dm.group(1)}"
                                    res["source"] = "frontmatter"
            except Exception:
                pass
    # 3. Filename-Prefix
    stem = Path(pdf_name).stem
    if not res["datum"]:
        ds = _date_from_filename_prefix(stem)
        if ds:
            res["datum"] = f"{ds[6:]}.{ds[4:6]}.{ds[:4]}"
            res["source"] = res["source"] or "filename"
    if not res["absender"]:
        parts = stem.split("_")
        if len(parts) >= 2:
            res["absender"] = parts[1]
            res["source"] = res["source"] or "filename"
    return res


def _run_duplikat_scan() -> None:
    """Führt vollständigen Duplikat-Scan im Hintergrund aus."""
    with _DEDUP_SCAN_LOCK:
        _DEDUP_SCAN_STATUS["running"] = True

    scan_id: int | None = None
    try:
        with get_db() as con:
            # Alte Scan-Ergebnisse löschen
            con.execute("DELETE FROM duplikat_eintraege")
            con.execute("DELETE FROM duplikat_gruppen")
            con.execute("DELETE FROM duplikat_scans")
            cur = con.execute(
                "INSERT INTO duplikat_scans (status, started_at) VALUES ('running', datetime('now','localtime'))"
            )
            scan_id = cur.lastrowid
        _DEDUP_SCAN_STATUS["scan_id"] = scan_id

        if not VAULT_PDF_ARCHIV or not VAULT_PDF_ARCHIV.exists():
            log.warning("Duplikat-Scan: VAULT_PDF_ARCHIV nicht konfiguriert")
            with get_db() as con:
                con.execute("UPDATE duplikat_scans SET status='error', finished_at=datetime('now','localtime') WHERE id=?", (scan_id,))
            return

        # ── Phase 1: Byte-Duplikate ──────────────────────────────────────────
        log.info("Duplikat-Scan Phase 1: Byte-Duplikate")
        hash_map: dict[str, list[Path]] = {}
        total = 0
        for pdf in sorted(VAULT_PDF_ARCHIV.rglob("*.pdf")):
            if any(p in _DEDUP_SKIP_DIRS for p in pdf.parts):
                continue
            if pdf.name.startswith("._"):
                continue
            total += 1
            try:
                h = _md5_file(pdf)
                hash_map.setdefault(h, []).append(pdf)
            except Exception as e:
                log.warning(f"MD5 Fehler {pdf.name}: {e}")

        log.info(f"Duplikat-Scan: {total} PDFs indexiert")
        byte_gruppen = 0
        byte_dup_set: set[Path] = set()
        for md5, paths in hash_map.items():
            if len(paths) < 2:
                continue
            byte_gruppen += 1

            metas = {p: _metadata_for_pdf(p.name) for p in paths}

            def _score(p: Path) -> tuple:
                m = metas[p]
                has_vault = bool(m.get("vault_pfad"))
                # Prefer YYYYMMDD_ or YYYYMMDD- prefix
                has_clean_date = bool(re.match(r'^\d{8}[_\-]', p.stem))
                # Penalize _1, _2 … duplicates
                is_suffix_dup = bool(re.search(r'_\d+$', p.stem))
                return (not has_vault, not has_clean_date, is_suffix_dup, len(p.stem))

            ordered = sorted(paths, key=_score)
            meta0 = metas[ordered[0]]
            with get_db() as con:
                cur = con.execute(
                    "INSERT INTO duplikat_gruppen (scan_id, typ, pdf_hash, datum, absender) VALUES (?,?,?,?,?)",
                    (scan_id, "byte", md5, meta0["datum"], meta0["absender"])
                )
                gid = cur.lastrowid
                for i, p in enumerate(ordered):
                    em = metas[p]
                    con.execute(
                        "INSERT INTO duplikat_eintraege (gruppe_id, pdf_pfad, md_pfad, ist_original) VALUES (?,?,?,?)",
                        (gid, str(p), em["vault_pfad"], 1 if i == 0 else 0)
                    )
            for p in ordered[1:]:
                byte_dup_set.add(p)

        log.info(f"Duplikat-Scan: {byte_gruppen} Byte-Gruppen")

        # ── Phase 2: Semantische Duplikate ───────────────────────────────────
        log.info("Duplikat-Scan Phase 2: Semantische Duplikate")
        sem_map: dict[tuple, list[dict]] = {}
        for pdf in sorted(VAULT_PDF_ARCHIV.rglob("*.pdf")):
            if any(p in _DEDUP_SKIP_DIRS for p in pdf.parts):
                continue
            if pdf.name.startswith("._"):
                continue
            if pdf in byte_dup_set:
                continue
            m = _metadata_for_pdf(pdf.name)
            datum = (m.get("datum") or "").strip()
            absender = (m.get("absender") or "").strip()
            if not datum and not absender:
                continue
            # Normalize datum → YYYYMMDD
            dn = ""
            dm = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', datum)
            if dm:
                dn = f"{dm.group(3)}{dm.group(2)}{dm.group(1)}"
            else:
                dm2 = re.match(r'(\d{4})-(\d{2})-(\d{2})', datum)
                if dm2:
                    dn = f"{dm2.group(1)}{dm2.group(2)}{dm2.group(3)}"
                else:
                    dn = datum
            an = re.sub(r'[^a-z0-9äöüß]', '', absender.lower())[:20]
            key = (dn, an)
            sem_map.setdefault(key, []).append({
                "pdf": pdf, "vault_pfad": m.get("vault_pfad"),
                "datum": datum, "absender": absender,
            })

        sem_gruppen = 0
        for (dn, an), entries in sem_map.items():
            if len(entries) < 2:
                continue
            if not dn and not an:
                continue

            # Fetch OCR texts from cache
            texts: list[str | None] = []
            for entry in entries:
                vp = entry.get("vault_pfad")
                text = None
                if vp:
                    ce = _cache_lookup(vp)
                    if ce:
                        text = ce.get("text") or ""
                texts.append(text)

            valid = [(i, t) for i, t in enumerate(texts) if t and len(t.strip()) > 50]
            is_dup = False
            if len(valid) >= 2:
                t1, t2 = valid[0][1], valid[1][1]
                a1, a2 = _extract_amounts(t1), _extract_amounts(t2)
                if a1 and a2:
                    overlap = len(a1 & a2) / max(len(a1), len(a2))
                    if overlap >= 0.3:
                        is_dup = True
                if not is_dup:
                    def _trigrams(s: str) -> set:
                        w = re.sub(r'\s+', ' ', s[:600]).lower().split()
                        return set(zip(w, w[1:], w[2:])) if len(w) >= 3 else set()
                    tg1, tg2 = _trigrams(t1), _trigrams(t2)
                    if tg1 and tg2:
                        jaccard = len(tg1 & tg2) / len(tg1 | tg2)
                        if jaccard >= 0.25:
                            is_dup = True
            elif len(entries) >= 2 and len(dn) >= 8 and len(an) >= 3:
                # Same date + sender, no OCR → likely duplicate
                is_dup = True

            if not is_dup:
                continue
            sem_gruppen += 1

            def _sem_score(e: dict) -> tuple:
                has_date = bool(re.match(r'^\d{8}_', e["pdf"].stem))
                has_vault = bool(e.get("vault_pfad"))
                return (not has_date and not has_vault, not has_vault, len(e["pdf"].stem))

            ordered_e = sorted(entries, key=_sem_score)
            with get_db() as con:
                cur = con.execute(
                    "INSERT INTO duplikat_gruppen (scan_id, typ, datum, absender) VALUES (?,?,?,?)",
                    (scan_id, "semantisch", ordered_e[0]["datum"], ordered_e[0]["absender"])
                )
                gid = cur.lastrowid
                for i, entry in enumerate(ordered_e):
                    con.execute(
                        "INSERT INTO duplikat_eintraege (gruppe_id, pdf_pfad, md_pfad, ist_original) VALUES (?,?,?,?)",
                        (gid, str(entry["pdf"]), entry.get("vault_pfad"), 1 if i == 0 else 0)
                    )

        log.info(f"Duplikat-Scan: {sem_gruppen} semantische Gruppen")

        with get_db() as con:
            con.execute(
                "UPDATE duplikat_scans SET status='done', total_pdfs=?, byte_gruppen=?, "
                "sem_gruppen=?, finished_at=datetime('now','localtime') WHERE id=?",
                (total, byte_gruppen, sem_gruppen, scan_id)
            )
        log.info("Duplikat-Scan abgeschlossen")

    except Exception as e:
        log.error(f"Duplikat-Scan Fehler: {e}", exc_info=True)
        if scan_id is not None:
            try:
                with get_db() as con:
                    con.execute(
                        "UPDATE duplikat_scans SET status='error', finished_at=datetime('now','localtime') WHERE id=?",
                        (scan_id,)
                    )
            except Exception:
                pass
    finally:
        with _DEDUP_SCAN_LOCK:
            _DEDUP_SCAN_STATUS["running"] = False


def _update_md_original_link(md_path: Path, old_pdf_name: str, new_pdf_rel: str) -> None:
    """Aktualisiert den original:-Wikilink im MD-Frontmatter auf den neuen PDF-Pfad.

    Behandelt beide Frontmatter-Formate:
    - Neues Format:  original: "[[Anlagen/DATEI.pdf]]"
    - Altes Format:  📎 **PDF:** [[Anlagen/DATEI.pdf]]  (im Body)
    """
    try:
        content = md_path.read_text(encoding="utf-8", errors="replace")
        # Ersetze alle Vorkommen des alten Wikilinks (im Frontmatter und im Body)
        old_link_anlagen = f"[[Anlagen/{old_pdf_name}]]"
        new_link = f"[[Anlagen/{new_pdf_rel}]]"
        if old_link_anlagen in content:
            content = content.replace(old_link_anlagen, new_link)
            md_path.write_text(content, encoding="utf-8")
    except Exception as e:
        log.warning(f"Frontmatter-Update fehlgeschlagen ({md_path.name}): {e}")


def _move_duplikat(gruppe_id: int, eintrag_id: int) -> dict:
    """Verschiebt ein Duplikat in den Quarantäne-Ordner.

    Kanonischer Name: abgeleitet aus dem MD-Stem des Originals (z.B.
    20250710_Versicherungskammer_Bayern_Versicherungsschein_Tarifänderung.pdf).
    Das Original-PDF wird auf diesen Namen umbenannt falls nötig;
    das Duplikat landet mit demselben kanonischen Namen im Quarantäne-Ordner.
    """
    with get_db() as con:
        gruppe = con.execute(
            "SELECT typ, datum, absender FROM duplikat_gruppen WHERE id=?", (gruppe_id,)
        ).fetchone()
        eintrag = con.execute(
            "SELECT id, pdf_pfad, md_pfad, ist_original, verschoben FROM duplikat_eintraege WHERE id=?",
            (eintrag_id,)
        ).fetchone()
        orig_eintrag = con.execute(
            "SELECT id, pdf_pfad, md_pfad FROM duplikat_eintraege WHERE gruppe_id=? AND ist_original=1",
            (gruppe_id,)
        ).fetchone()
    if not gruppe:
        return {"error": f"Gruppe {gruppe_id} nicht gefunden"}
    if not eintrag:
        return {"error": f"Eintrag {eintrag_id} nicht gefunden"}
    if eintrag["ist_original"]:
        return {"error": "Original kann nicht verschoben werden"}
    if eintrag["verschoben"]:
        return {"error": "Bereits verschoben"}

    qname = "00 Duplikate" if gruppe["typ"] == "byte" else "00 Text-Duplikate"
    pdf_src = Path(eintrag["pdf_pfad"])
    if not pdf_src.exists():
        return {"error": f"PDF nicht gefunden: {pdf_src.name}"}

    moved: list[str] = []

    # ── Kanonischen Namen aus dem MD-Stem des Originals ableiten ────────────
    canonical_pdf_name: str | None = None
    if orig_eintrag and orig_eintrag["md_pfad"]:
        canonical_pdf_name = Path(orig_eintrag["md_pfad"]).stem + ".pdf"

    # ── Original-PDF umbenennen falls Name nicht kanonisch ───────────────────
    if canonical_pdf_name and VAULT_PDF_ARCHIV and orig_eintrag:
        orig_pdf = Path(orig_eintrag["pdf_pfad"])
        if orig_pdf.exists() and orig_pdf.name != canonical_pdf_name:
            new_orig_pdf = VAULT_PDF_ARCHIV / canonical_pdf_name
            if not new_orig_pdf.exists():
                try:
                    orig_pdf.rename(new_orig_pdf)
                    # Frontmatter original:-Link im MD aktualisieren
                    if orig_eintrag["md_pfad"] and VAULT_ROOT:
                        _update_md_original_link(
                            VAULT_ROOT / orig_eintrag["md_pfad"],
                            orig_pdf.name, canonical_pdf_name
                        )
                    # DB: anlagen_dateiname + scan-Eintrag
                    with get_db() as con:
                        con.execute(
                            "UPDATE dokumente SET anlagen_dateiname=? WHERE anlagen_dateiname=?",
                            (canonical_pdf_name, orig_pdf.name)
                        )
                        con.execute(
                            "UPDATE duplikat_eintraege SET pdf_pfad=? WHERE id=?",
                            (str(new_orig_pdf), orig_eintrag["id"])
                        )
                    moved.append(f"Original umbenannt: {orig_pdf.name} → {canonical_pdf_name}")
                    log.info(f"Original-PDF umbenannt: {orig_pdf.name} → {canonical_pdf_name}")
                except Exception as e:
                    log.warning(f"Umbenennung Original fehlgeschlagen: {e}")

    # ── Duplikat-PDF in Quarantäne verschieben ────────────────────────────────
    dest_pdf_name = canonical_pdf_name or pdf_src.name
    if not VAULT_PDF_ARCHIV:
        return {"error": "VAULT_PDF_ARCHIV nicht konfiguriert"}
    anlagen_q = VAULT_PDF_ARCHIV / qname
    anlagen_q.mkdir(parents=True, exist_ok=True)
    pdf_dst = anlagen_q / dest_pdf_name
    # Kollision: gleicher Name schon im Quarantäne-Ordner
    if pdf_dst.exists():
        pdf_dst = anlagen_q / (Path(dest_pdf_name).stem + "_dup.pdf")
    try:
        shutil.move(str(pdf_src), str(pdf_dst))
        moved.append(f"PDF → {qname}/{pdf_dst.name}")
        log.info(f"Duplikat verschoben: {pdf_src.name} → {qname}/{pdf_dst.name}")
    except Exception as e:
        return {"error": f"PDF-Verschieben fehlgeschlagen: {e}"}

    # ── Duplikat-MD verschieben + Frontmatter aktualisieren ──────────────────
    md_vault_pfad = eintrag["md_pfad"]
    if md_vault_pfad and VAULT_ROOT:
        md_src = VAULT_ROOT / md_vault_pfad
        if md_src.exists():
            vault_q = VAULT_ROOT / qname
            vault_q.mkdir(parents=True, exist_ok=True)
            md_dst_name = Path(dest_pdf_name).stem + ".md"
            md_dst = vault_q / md_dst_name
            if md_dst.exists():
                md_dst = vault_q / (Path(md_dst_name).stem + "_dup.md")
            try:
                _update_md_original_link(md_src, pdf_src.name, f"{qname}/{pdf_dst.name}")
                shutil.move(str(md_src), str(md_dst))
                moved.append(f"MD → {qname}/{md_dst.name}")
                with get_db() as con:
                    con.execute(
                        "UPDATE dokumente SET vault_pfad=? WHERE vault_pfad=?",
                        (f"{qname}/{md_dst.name}", md_vault_pfad)
                    )
            except Exception as e:
                log.warning(f"MD-Verschieben fehlgeschlagen: {e}")
                moved.append(f"MD-Verschieben fehlgeschlagen: {e}")

    with get_db() as con:
        con.execute("UPDATE duplikat_eintraege SET verschoben=1 WHERE id=?", (eintrag_id,))
        remaining = con.execute(
            "SELECT COUNT(*) FROM duplikat_eintraege WHERE gruppe_id=? AND ist_original=0 AND verschoben=0",
            (gruppe_id,)
        ).fetchone()[0]
        if remaining == 0:
            con.execute("UPDATE duplikat_gruppen SET status='verarbeitet' WHERE id=?", (gruppe_id,))

    return {"ok": True, "moved": moved}


def _run_move_all() -> None:
    """Verschiebt alle offenen Duplikate im Hintergrund."""
    with _DEDUP_MOVE_LOCK:
        _DEDUP_MOVE_STATUS.update({
            "running": True, "total": 0, "processed": 0, "errors": 0,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None, "last_error": None,
        })
    try:
        with get_db() as con:
            scan_row = con.execute(
                "SELECT id FROM duplikat_scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not scan_row:
                log.warning("Move-All: kein Scan vorhanden")
                return
            scan_id = scan_row["id"]
            eintraege = con.execute(
                "SELECT de.id, de.gruppe_id FROM duplikat_eintraege de "
                "JOIN duplikat_gruppen dg ON de.gruppe_id = dg.id "
                "WHERE dg.scan_id=? AND de.ist_original=0 AND de.verschoben=0",
                (scan_id,)
            ).fetchall()

        total = len(eintraege)
        with _DEDUP_MOVE_LOCK:
            _DEDUP_MOVE_STATUS["total"] = total
        log.info(f"Move-All: {total} Duplikate werden verarbeitet")

        processed = 0
        errors = 0
        for row in eintraege:
            result = _move_duplikat(row["gruppe_id"], row["id"])
            if result.get("ok"):
                processed += 1
            else:
                errors += 1
                err = result.get("error", "")
                log.warning(f"Move-All Fehler (eintrag {row['id']}): {err}")
                with _DEDUP_MOVE_LOCK:
                    _DEDUP_MOVE_STATUS["last_error"] = err
            with _DEDUP_MOVE_LOCK:
                _DEDUP_MOVE_STATUS["processed"] = processed
                _DEDUP_MOVE_STATUS["errors"] = errors

        log.info(f"Move-All abgeschlossen: {processed} verschoben, {errors} Fehler")
    except Exception as e:
        log.error(f"Move-All Fehler: {e}", exc_info=True)
        with _DEDUP_MOVE_LOCK:
            _DEDUP_MOVE_STATUS["last_error"] = str(e)
    finally:
        with _DEDUP_MOVE_LOCK:
            _DEDUP_MOVE_STATUS["running"] = False
            _DEDUP_MOVE_STATUS["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main():
    parser = _build_cli_parser()
    args = parser.parse_args()

    # Batch-Modus: einmaliger CLI-Lauf, kein Watcher, kein API-Server
    if args.batch:
        log.info(f"Document Dispatcher — Batch-Modus")
        log.info(f"Input:      {args.batch}")
        log.info(f"OCR-Quelle: {args.ocr_source}")
        log.info(f"Ausgabe:    {args.output}")
        input_path = Path(args.batch)
        output_dir = Path(args.output_dir) if args.output_dir else None
        try:
            summary = run_batch(
                input_path=input_path,
                ocr_mode=args.ocr_source,
                output_mode=args.output,
                output_dir=output_dir,
                limit=args.limit,
                dry_run=args.dry_run,
                run_id=args.resume,
            )
        except Exception as e:
            log.error(f"Batch-Lauf fehlgeschlagen: {e}")
            raise SystemExit(2)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

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
    threading.Thread(target=tg_poll, daemon=True).start()

    # Vorhandene PDFs in WATCH_DIR verarbeiten
    for f in WATCH_DIR.glob("*.pdf"):
        file_queue.put(f)
    for f in WATCH_DIR.glob("*.enex"):
        file_queue.put(("enex", f))

    # Auto-Batch-Rescan entfernt 2026-04-19 (flache Archiv-Architektur):
    # Bestandsdokumente werden nicht mehr beim Start rescanniert.
    # On-Demand-Auswertung stattdessen über CLI `--batch` + Dashboard `/batch`.

    observer = Observer()
    observer.schedule(DocumentHandler(), str(WATCH_DIR), recursive=False)
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
