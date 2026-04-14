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

# Wird beim Start aus categories.yaml geladen (vault_folder-Feld)
CATEGORY_TO_VAULT_FOLDER: dict[str, str] = {}


def _build_vault_md_relpath(vault_folder: str, year: str, md_filename: str) -> str:
    """Vault-Pfad: aktuelles Jahr direkt in Kategorie-Wurzel, Vorjahre im <year>/-Unterordner."""
    current_year = datetime.now().strftime("%Y")
    if year == current_year:
        return f"{vault_folder}/{md_filename}"
    return f"{vault_folder}/{year}/{md_filename}"

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
            "SELECT dateiname, kategorie, typ, vault_pfad FROM dokumente WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            return f"❌ Dokument {doc_id} nicht gefunden"

        old_cat = row["kategorie"]
        old_type = row["typ"]
        old_vault_pfad = row["vault_pfad"]
        dateiname = row["dateiname"]

        # Neuen Vault-Pfad berechnen
        new_vault_folder = CATEGORY_TO_VAULT_FOLDER.get(new_cat, "00 Inbox")

        # Jahr aus altem Pfad extrahieren oder aus Dateiname
        year_match = re.search(r"/(\d{4})/", old_vault_pfad or "")
        if year_match:
            year = year_match.group(1)
        else:
            m = re.match(r"(\d{4})", dateiname)
            year = m.group(1) if m else datetime.now().strftime("%Y")

        # MD-Dateiname aus vault_pfad extrahieren
        md_filename = Path(old_vault_pfad).name if old_vault_pfad else f"{dateiname}.md"
        new_vault_pfad = _build_vault_md_relpath(new_vault_folder, year, md_filename)

        # DB updaten
        con.execute(
            "UPDATE dokumente SET kategorie=?, typ=?, vault_kategorie=?, vault_typ=?, vault_pfad=? WHERE id=?",
            (new_cat, new_type, new_cat, new_type, new_vault_pfad, doc_id)
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

    prompt = f"""Analysiere das folgende Dokument und klassifiziere es anhand der vorgegebenen Kategorien.

Verfügbare Kategorien und Typen:
{cat_desc}
{kv_rules}
Für ALLE Kategorien:
- Adressat: "Reinhard" wenn Reinhard Janning/R. Janning der Empfänger ist, "Marion" wenn Marion Janning/M. Janning, "Reinhard & Marion" wenn beide adressiert sind.
  - Bei Krankenversicherung gilt IMMER das ABSENDER → ADRESSAT-MAPPING oben (HUK → Marion, Gothaer/Barmenia → Reinhard, Arztrechnung ohne Patient → null).
  - Bei anderen Kategorien: wenn kein Name eindeutig erkennbar ist und das Dokument an den Haushalt gerichtet scheint (Bank, Vermieter, Behörde ohne Namensnennung), darf "Reinhard" als Default gewählt werden — aber nur wenn der Absender typischerweise an Reinhard adressiert. Sonst null. NICHT raten.

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
  - "hoch" NUR wenn category_id, type_id, absender UND adressat alle eindeutig aus dem Dokument ableitbar sind (klarer Briefkopf, klarer Dokumenttyp, klarer Name). Default ist "mittel".
  - "mittel" bei JEDER Unsicherheit: Typ unklar, Adressat geraten, Absender nur indirekt erkennbar.
  - "niedrig" wenn die Kategorie selbst unklar ist oder das Dokument mehrere Kategorien plausibel macht.

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
    """Verschiebt PDF nach pdf-archiv/ und MD nach reinhards-vault/{kategorie}/[<jahr>/]."""
    if not VAULT_PDF_ARCHIV or not VAULT_ROOT:
        log.warning("VAULT_PDF_ARCHIV/VAULT_ROOT nicht konfiguriert — Dateien bleiben in WATCH_DIR")
        return

    rechnungsdatum = result.get("rechnungsdatum") if result else None
    year = rechnungsdatum[-4:] if rechnungsdatum and len(rechnungsdatum) >= 4 else datetime.now().strftime("%Y")

    # Vault-Ordner aus categories.yaml, Fallback auf Inbox
    vault_folder = CATEGORY_TO_VAULT_FOLDER.get(category_id, "00 Inbox")

    # Sauberen Dateinamen generieren
    if result:
        clean_name = build_clean_filename(result, file_path.stem)
    else:
        clean_name = _sanitize_name_part(file_path.stem)

    vault_pfad = _build_vault_md_relpath(vault_folder, year, f"{clean_name}.md")
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

    # PDF verschieben
    shutil.move(str(file_path), str(dest_pdf))
    log.info(f"PDF → pdf-archiv: {dest_pdf.name}")

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

    # PDF im Chat senden zur Überprüfung
    tg_send_document(file_path)

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

    category_id = result.get("category_id", "")
    tg_send("\n".join(lines))
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
    # Telegram-Polling deaktiviert — Wilson/OpenClaw pollt, Dispatcher nutzt API
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
