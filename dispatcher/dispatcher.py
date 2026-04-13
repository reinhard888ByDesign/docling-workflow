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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

WATCH_DIR      = Path(os.environ.get("WATCH_DIR",      "/data/input-dispatcher"))
TEMP_DIR       = Path(os.environ.get("TEMP_DIR",       "/data/dispatcher-temp"))
CONFIG_FILE    = Path(os.environ.get("CONFIG_FILE",    "/config/categories.yaml"))
DB_FILE        = TEMP_DIR / "dispatcher.db"
DOCLING_URL    = os.environ.get("DOCLING_URL",          "http://docling-serve:5001")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",           "http://ollama:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",         "qwen2.5:7b")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",  "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",    "")
API_PORT       = int(os.environ.get("API_PORT", "8765"))

TEMP_DIR.mkdir(parents=True, exist_ok=True)

_vault_pdf = os.environ.get("VAULT_PDF_ARCHIV", "")
_vault_root = os.environ.get("VAULT_ROOT", "")
VAULT_PDF_ARCHIV = Path(_vault_pdf) if _vault_pdf else None
VAULT_ROOT = Path(_vault_root) if _vault_root else None

# Leistungsabrechnung type_ids
LEISTUNGSABRECHNUNG_TYPES = {"leistungsabrechnung_reinhard", "leistungsabrechnung_marion"}

# Versicherungsdokument type_ids (keine Rechnung in DB anlegen)
VERSICHERUNG_TYPES = {
    "versicherungsschein",
    "beitragsanpassung",
    "beitragsbescheinigung",
    "kostenuebernahme",
    "versicherungsbedingungen",
    "versicherungskorrespondenz",
}

TYP_TO_FOLDER = {
    "leistungsabrechnung_reinhard":  "leistungsabrechnung",
    "leistungsabrechnung_marion":    "leistungsabrechnung",
    "arztrechnung":                  "arztrechnung",
    "rezept":                        "rezept",
    "hilfsmittel":                   "hilfsmittel",
    "anderes":                       "anderes",
    "versicherungsschein":           "versicherung",
    "beitragsanpassung":             "versicherung",
    "beitragsbescheinigung":         "versicherung",
    "kostenuebernahme":              "versicherung",
    "versicherungsbedingungen":      "versicherung",
    "versicherungskorrespondenz":    "versicherung",
}

# Wird beim Start aus categories.yaml geladen (vault_folder-Feld)
CATEGORY_TO_VAULT_FOLDER: dict[str, str] = {}

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

Wichtige Kontextinfos:
- Reinhard → Gothaer Krankenversicherung (leistungsabrechnung_reinhard)
- Marion   → HUK-COBURG Krankenversicherung (leistungsabrechnung_marion)
- Jahresfilter: rechnungsdatum LIKE '%2024'
- SUM/AVG auf rechnungsbetrag immer mit ROUND(...,2)
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
    log.info(f"Datenbank initialisiert: {DB_FILE}")


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

    with get_db() as con:
        # Duplikat-Schutz: bereits verarbeitete Dateinamen überspringen
        existing = con.execute(
            "SELECT id FROM dokumente WHERE dateiname = ?", (file_path.name,)
        ).fetchone()
        if existing:
            log.info(f"Bereits in DB: {file_path.name} — überspringe DB-Insert")
            return []

        # 1. Dokument speichern
        cur = con.execute(
            """INSERT INTO dokumente
               (dateiname, rechnungsdatum, kategorie, typ, absender, adressat, konfidenz)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                file_path.name,
                result.get("rechnungsdatum"),
                category_id,
                type_id,
                result.get("absender"),
                result.get("adressat"),
                result.get("konfidenz"),
            )
        )
        dok_id = cur.lastrowid

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


# ── Kategorien laden ───────────────────────────────────────────────────────────

def load_categories() -> dict:
    if not CONFIG_FILE.exists():
        log.warning(f"Config nicht gefunden: {CONFIG_FILE}")
        return {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cats = yaml.safe_load(f).get("categories", {})
    # vault_folder-Mapping aufbauen
    for cat_id, cat in cats.items():
        if "vault_folder" in cat:
            CATEGORY_TO_VAULT_FOLDER[cat_id] = cat["vault_folder"]
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

def tg_send(text: str, chat_id: str | None = None):
    if not TELEGRAM_TOKEN:
        log.warning("Telegram nicht konfiguriert.")
        return
    target = chat_id or TELEGRAM_CHAT
    if not target:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": target, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        if not r.ok:
            log.warning(f"Telegram Fehler: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}")


# ── NL-Datenbankabfrage ────────────────────────────────────────────────────────

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
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
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

    # Tabellarisch formatieren
    col_str = " | ".join(cols)
    sep = "─" * min(len(col_str), 60)
    lines = [col_str, sep]
    for row in rows:
        lines.append(" | ".join("–" if v is None else str(v) for v in row))

    header = f"📊 {len(rows)} Ergebnis{'se' if len(rows) != 1 else ''}"
    return f"{header}\n\n<pre>{chr(10).join(lines)}</pre>"


# ── Telegram-Polling ───────────────────────────────────────────────────────────

def tg_poll():
    """Empfängt Telegram-Updates und beantwortet /frage-Befehle."""
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
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                # Nur aus dem konfigurierten Chat akzeptieren
                if chat_id != TELEGRAM_CHAT:
                    continue

                if text.lower().startswith("/frage "):
                    question = text[7:].strip()
                    if not question:
                        continue
                    tg_send(f"🔍 <i>{question}</i>", chat_id=chat_id)
                    result = query_db_with_nl(question)
                    tg_send(result[:4096], chat_id=chat_id)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.warning(f"Telegram-Poll Fehler: {e}")
            time.sleep(5)


# ── Query-API (für Open WebUI) ─────────────────────────────────────────────────

class _QueryHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/query":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data     = json.loads(self.rfile.read(length))
            question = data.get("question", "").strip()
        except Exception:
            self.send_response(400); self.end_headers(); return
        if not question:
            self.send_response(400); self.end_headers(); return

        result = query_db_with_nl(question)
        body   = json.dumps({"result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info(f"API: {fmt % args}")


def start_api_server():
    server = HTTPServer(("0.0.0.0", API_PORT), _QueryHandler)
    log.info(f"Query-API gestartet auf Port {API_PORT}")
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


def classify_with_ollama(md_content: str, categories: dict) -> dict | None:
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

C) Sanitätshaus, Optiker, Apotheke, Physiotherapie:
   → category_id="krankenversicherung", type_id="sonstige_medizinische_leistung"

D) Arzt mit Medikamentenliste:
   → category_id="krankenversicherung", type_id="rezept"

WICHTIG: Entscheidend ist NICHT die bloße Erwähnung von "Versicherung" im Text, sondern Absender + Dokumenttyp.

Für Krankenversicherung/Versicherung zusätzlich ausfüllen:
- "rechnungsbetrag": Gesamtbetrag als String (z.B. "33,06 EUR") — bei Leistungsabrechnung: Gesamtrechnungsbetrag, bei Arztrechnung: Endbetrag; sonst null
- "erstattungsbetrag": Erstatteter Betrag als String — NUR bei Leistungsabrechnung, sonst null
- "faelligkeitsdatum": Fälligkeitsdatum als String — NUR bei Arztrechnung/Rezept/sonstige, sonst null
- "positionen": Liste der Erstattungspositionen — NUR bei leistungsabrechnung-Typen, sonst []. Jede Position: {{"leistungserbringer": "Name", "zeitraum": "02.02-19.04.2023", "rechnungsbetrag": 33.06, "erstattungsbetrag": 10.72}}
"""

    prompt = f"""Analysiere das folgende Dokument und klassifiziere es anhand der vorgegebenen Kategorien.

Verfügbare Kategorien und Typen:
{cat_desc}
{kv_rules}
Für ALLE Kategorien:
- Adressat: IMMER ausfüllen. "Reinhard" wenn Reinhard Janning/R. Janning der Empfänger ist, "Marion" wenn Marion Janning/M. Janning, "Reinhard & Marion" wenn beide adressiert sind. Wenn keine andere Person erkennbar ist, ist der Adressat "Reinhard" (Standardwert). Nur null wenn eindeutig eine dritte Person adressiert wird.

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
- "konfidenz": "hoch" | "mittel" | "niedrig"

Antworte AUSSCHLIESSLICH mit validem JSON, kein Text davor oder danach.

Dokument:
{md_content[:6000]}"""

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
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
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Fallback: json-repair für strukturelle LLM-Fehler (fehlende Kommas etc.)
            try:
                repaired = repair_json(json_str, return_objects=True)
                if isinstance(repaired, dict):
                    return repaired
            except Exception:
                pass
            log.warning(f"JSON-Parse fehlgeschlagen (auch nach Reparatur): {repr(json_str[:200])}")
            return None
    except Exception as e:
        log.warning(f"Ollama Klassifizierung fehlgeschlagen: {e}")
        return None

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
    if datum and re.match(r"\d{2}\.\d{2}\.\d{4}", datum):
        parts = datum.split(".")
        date_str = f"{parts[2]}{parts[1]}{parts[0]}"
    else:
        # Fallback: try to extract from original filename
        m = re.match(r"(\d{8})", original_stem)
        date_str = m.group(1) if m else datetime.now().strftime("%Y%m%d")

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

    # Zusammenbauen
    parts = [date_str]
    if absender_clean:
        parts.append(absender_clean)
    if type_clean:
        parts.append(type_clean)

    if len(parts) == 1:
        # Kein Absender, kein Typ → Original-Stem verwenden
        return _sanitize_name_part(original_stem)

    return "_".join(parts)


def move_to_vault(file_path: Path, temp_md: Path, category_id: str, type_id: str, result: dict):
    """Verschiebt PDF nach pdf-archiv/ und MD nach reinhards-vault/{kategorie}/Converted/{typ}/{jahr}/."""
    if not VAULT_PDF_ARCHIV or not VAULT_ROOT:
        log.warning("VAULT_PDF_ARCHIV/VAULT_ROOT nicht konfiguriert — Dateien bleiben in WATCH_DIR")
        return

    rechnungsdatum = result.get("rechnungsdatum") if result else None
    year = rechnungsdatum[-4:] if rechnungsdatum and len(rechnungsdatum) >= 4 else datetime.now().strftime("%Y")

    # Vault-Ordner aus categories.yaml, Fallback auf Inbox
    vault_folder = CATEGORY_TO_VAULT_FOLDER.get(category_id, "00 Inbox")

    # Typ-Unterordner: bei KV/Versicherung aus TYP_TO_FOLDER, sonst type_id direkt
    if type_id and type_id in TYP_TO_FOLDER:
        typ_folder = TYP_TO_FOLDER[type_id]
    elif type_id:
        typ_folder = type_id
    else:
        typ_folder = "allgemein"

    # Sauberen Dateinamen generieren
    if result:
        clean_name = build_clean_filename(result, file_path.stem)
    else:
        clean_name = _sanitize_name_part(file_path.stem)

    dest_pdf = VAULT_PDF_ARCHIV / f"{clean_name}.pdf"
    dest_md_dir = VAULT_ROOT / vault_folder / "Converted" / typ_folder / year
    dest_md = dest_md_dir / f"{clean_name}.md"

    # Kollisionsvermeidung
    counter = 2
    while dest_pdf.exists() or dest_md.exists():
        dest_pdf = VAULT_PDF_ARCHIV / f"{clean_name}_{counter}.pdf"
        dest_md = dest_md_dir / f"{clean_name}_{counter}.md"
        counter += 1

    # PDF verschieben
    shutil.move(str(file_path), str(dest_pdf))
    log.info(f"PDF → pdf-archiv: {dest_pdf.name}")

    # MD verschieben
    dest_md_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp_md), str(dest_md))
    log.info(f"MD → Vault: {vault_folder}/Converted/{typ_folder}/{year}/{dest_md.name}")



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

    # 2. Markdown in TEMP speichern
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r"[^\w\-]", "_", file_path.stem)
    temp_md = TEMP_DIR / f"{timestamp}_{stem}.md"
    temp_md.write_text(md_content, encoding="utf-8")
    log.info(f"Markdown gespeichert: {temp_md.name}")

    # 3. Klassifizierung via Ollama
    categories = load_categories()
    if not categories:
        tg_send(f"❌ Keine Kategorien konfiguriert\n<code>{file_path.name}</code>")
        return

    result = classify_with_ollama(md_content, categories)

    if not result or not result.get("category_id"):
        tg_send(
            f"⚠️ <b>Klassifizierung nicht möglich — Datei in Inbox</b>\n"
            f"Datei: <code>{file_path.name}</code>"
        )
        log.info(f"Klassifizierung fehlgeschlagen für: {file_path.name} — verschiebe in Inbox")
        move_to_vault(file_path, temp_md, "", "", {})

        return

    # 4. Datenbank
    match_infos = save_to_db(file_path, result)

    # 5. Telegram-Nachricht
    type_id            = result.get("type_id", "")
    is_la              = type_id in LEISTUNGSABRECHNUNG_TYPES
    is_versicherung    = type_id in VERSICHERUNG_TYPES
    absender           = result.get("absender") or "–"
    adressat           = result.get("adressat") or "Reinhard"
    rechnungsdatum     = result.get("rechnungsdatum")
    rechnungsbetrag    = result.get("rechnungsbetrag")
    erstattungsbetrag  = result.get("erstattungsbetrag")
    faelligkeitsdatum  = result.get("faelligkeitsdatum")
    konfidenz          = result.get("konfidenz", "")
    konfidenz_icon     = {"hoch": "🟢", "mittel": "🟡", "niedrig": "🔴"}.get(konfidenz, "⚪")

    # Neuen Dateinamen für Telegram-Nachricht berechnen
    clean_name = build_clean_filename(result, file_path.stem)

    lines = [
        f"✅ <b>Dokument klassifiziert</b>",
        f"",
        f"📄 Datei:      <code>{clean_name}.pdf</code>",
        f"🏢 Absender:   {absender}",
        f"👤 Adressat:   {adressat}",
    ]
    if rechnungsdatum:
        lines.append(f"📅 Datum:      {rechnungsdatum}")
    lines += [
        f"🗂 Kategorie:  <b>{result.get('category_label', '–')}</b>",
        f"📁 Typ:        <b>{result.get('type_label', '–')}</b>",
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

    lines.append(f"🎯 Konfidenz:  {konfidenz_icon} {konfidenz}")
    tg_send("\n".join(lines))
    category_id = result.get("category_id", "")
    log.info(f"Klassifiziert: {file_path.name} → {category_id}/{type_id}")

    # 6. Dateien in Vault verschieben
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
    # Telegram-Polling deaktiviert — kollidiert mit OpenClaw (getUpdates conflict)
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
