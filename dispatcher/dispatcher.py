import os
import re
import json
import time
import queue
import sqlite3
import logging
import requests
import threading
from datetime import datetime
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

TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Leistungsabrechnung type_ids
LEISTUNGSABRECHNUNG_TYPES = {"leistungsabrechnung_reinhard", "leistungsabrechnung_marion"}

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
        """)
    # Migration: Spalte nachrüsten falls DB bereits existierte
    with get_db() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(rechnungen)")}
        if "erstattungsdatum" not in cols:
            con.execute("ALTER TABLE rechnungen ADD COLUMN erstattungsdatum TEXT")
            log.info("Migration: Spalte erstattungsdatum hinzugefügt")
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
    is_la = type_id in LEISTUNGSABRECHNUNG_TYPES

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
                result.get("category_id"),
                type_id,
                result.get("absender"),
                result.get("adressat"),
                result.get("konfidenz"),
            )
        )
        dok_id = cur.lastrowid

        # 2. Rechnung oder Erstattungspositionen
        match_infos = []

        if not is_la:
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
        return yaml.safe_load(f).get("categories", {})


def build_category_description(categories: dict) -> str:
    lines = []
    for cat_id, cat in categories.items():
        lines.append(f"\nKategorie: {cat['label']} (id: {cat_id})")
        for t in cat.get("types", []):
            hints = ", ".join(t.get("hints", []))
            lines.append(f"  - Typ: {t['label']} (id: {t['id']}) | Erkennungshinweise: {hints}")
    return "\n".join(lines)

# ── Queue ──────────────────────────────────────────────────────────────────────

file_queue: queue.Queue = queue.Queue()

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram nicht konfiguriert.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        if not r.ok:
            log.warning(f"Telegram Fehler: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}")

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
    prompt = f"""Analysiere das folgende Dokument und klassifiziere es anhand der vorgegebenen Kategorien.

Verfügbare Kategorien und Typen:
{cat_desc}

KLASSIFIZIERUNGSREGELN — lies diese sorgfältig:

Schritt 1: Wer ist der ABSENDER des Dokuments?
- Ist der Absender eine Versicherung (Gothaer, Barmenia, HUK, HUK-COBURG)?
  → Dann und NUR dann: "leistungsabrechnung_reinhard" oder "leistungsabrechnung_marion"
  → Erkennbar an: Versicherungslogo, Erstattungsübersicht, Auflistung eingereichter Fremdrechnungen, Erstattungsbetrag
- Ist der Absender ein Arzt, Krankenhaus, Klinik, Labor, Radiologie, MVZ, oder ein Abrechnungsdienstleister der IM AUFTRAG eines Arztes/einer Klinik abrechnet (z.B. unimed GmbH, Doctolib, Mediport)?
  → Immer: "arztrechnung"
  → Erkennbar an: GOÄ-Ziffern, Honorar, Liquidation, Diagnose, Fälligkeitsbetrag direkt an den Patienten
- Ist der Absender ein Sanitätshaus, Optiker, Apotheke (ohne Rezept), Physiotherapie?
  → "sonstige_medizinische_leistung"
- Ist es ein Dokument vom Arzt mit Medikamentenliste?
  → "rezept"

WICHTIG: Die bloße Erwähnung von "Versicherung" im Fließtext (z.B. "reichen Sie bei Ihrer Versicherung ein") macht ein Dokument NICHT zu einer Leistungsabrechnung. Entscheidend ist ausschließlich wer der Absender/Aussteller ist.

Adressat: "Reinhard" wenn Reinhard Janning der Empfänger ist, "Marion" wenn Marion Janning, sonst null.

Antworte NUR mit einem JSON-Objekt mit diesen Feldern:
- "category_id": ID der erkannten Kategorie (z.B. "krankenversicherung"), oder null wenn keine passt
- "category_label": Bezeichnung der Kategorie, oder null
- "type_id": ID des erkannten Typs (z.B. "arztrechnung"), oder null
- "type_label": Bezeichnung des Typs, oder null
- "absender": Name des Absenders/Ausstellers (Firma oder Person), oder null
- "adressat": "Reinhard" | "Marion" | null
- "rechnungsdatum": Datum des Dokuments als String im Format "DD.MM.YYYY" — bei Arztrechnung das Rechnungsdatum, bei Leistungsabrechnung das Abrechnungsdatum. Suche nach Feldern wie "Datum:", "Re.-Datum:", "Rechnungsdatum:", "Druckdatum:" oder ähnlichem. Muss ausgefüllt sein wenn ein Datum im Dokument erkennbar ist.
- "rechnungsbetrag": Gesamtbetrag aller eingereichten Rechnungsbelege als String (z.B. "33,06 EUR") — bei Leistungsabrechnung: Gesamtrechnungsbetrag, bei Arztrechnung: Rechnungsendbetrag; sonst null
- "erstattungsbetrag": Von der Versicherung erstatteter/überwiesener Betrag als String (z.B. "10,72 EUR") — NUR bei Leistungsabrechnung, sonst null
- "faelligkeitsdatum": Datum bis zu dem die Rechnung bezahlt werden muss als String (z.B. "30.04.2023") — NUR bei Arztrechnung/Rezept/sonstige, null wenn kein konkretes Datum angegeben
- "positionen": Liste der Erstattungspositionen — NUR bei leistungsabrechnung-Typen, sonst []. Jede Position als Objekt: {{"leistungserbringer": "Name", "zeitraum": "02.02-19.04.2023", "rechnungsbetrag": 33.06, "erstattungsbetrag": 10.72}}
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

def process_file(file_path: Path):
    if file_path.suffix.lower() != ".pdf":
        return

    log.info(f"Neue Datei: {file_path.name}")

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
            f"⚠️ <b>Klassifizierung nicht möglich</b>\n"
            f"Datei: <code>{file_path.name}</code>"
        )
        log.info(f"Klassifizierung fehlgeschlagen für: {file_path.name}")
        return

    # 4. Datenbank
    match_infos = save_to_db(file_path, result)

    # 5. Telegram-Nachricht
    type_id            = result.get("type_id", "")
    is_la              = type_id in LEISTUNGSABRECHNUNG_TYPES
    absender           = result.get("absender") or "–"
    adressat           = result.get("adressat") or "–"
    rechnungsdatum     = result.get("rechnungsdatum")
    rechnungsbetrag    = result.get("rechnungsbetrag")
    erstattungsbetrag  = result.get("erstattungsbetrag")
    faelligkeitsdatum  = result.get("faelligkeitsdatum")
    konfidenz          = result.get("konfidenz", "")
    konfidenz_icon     = {"hoch": "🟢", "mittel": "🟡", "niedrig": "🔴"}.get(konfidenz, "⚪")

    lines = [
        f"✅ <b>Dokument klassifiziert</b>",
        f"",
        f"📄 Datei:      <code>{file_path.name}</code>",
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
    else:
        if rechnungsbetrag:
            lines.append(f"💰 Betrag:     {rechnungsbetrag}")
        if faelligkeitsdatum:
            lines.append(f"📅 Fällig:     {faelligkeitsdatum}")
        if not rechnungsbetrag:
            lines.append(f"💰 Betrag:     –")

    lines.append(f"🎯 Konfidenz:  {konfidenz_icon} {konfidenz}")
    tg_send("\n".join(lines))
    log.info(f"Klassifiziert: {file_path.name} → {type_id}")


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
