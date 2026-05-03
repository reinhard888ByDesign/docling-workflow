"""
enex_processor.py — Phase 1: ENEX Sofortimport
Verarbeitet eine .enex-Datei: parst Notes, routet sie in den Vault,
schreibt Markdown-Dateien und PDF-Anhänge.

Aufruf:
    python enex_processor.py <datei.enex>
    python enex_processor.py --help

Umgebungsvariablen (aus .env / docker-compose):
    VAULT_PATH              Pfad zum Vault, z.B. /data/vault
    ENEX_TAGS_CONFIG        Pfad zu enex-tags.yaml, default /config/enex-tags.yaml
    ENEX_DEFAULT_TIMEZONE   Zeitzone für Datumskonvertierung, default Europe/Berlin
    ENEX_IMPORT_IMAGES      Bilder importieren (true/false), default false
    ENEX_LLM_FALLBACK       LLM bei kein Tag-Match (true/false), default false
    ENEX_BATCH_TELEGRAM     Telegram-Nachricht pro ENEX (true/false), default true
    DB_PATH                 SQLite-DB, default /config/dispatcher.db
    OLLAMA_URL              Ollama-API, default http://ollama:11434
    OLLAMA_MODEL            LLM-Modell, default qwen2.5:7b
    TELEGRAM_TOKEN          Bot-Token
    TELEGRAM_CHAT_ID        Chat-ID
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python < 3.9

import requests
import yaml

from enex_parser import Note, parse_enex
from enml_to_markdown import enml_to_markdown, make_ocr_placeholder
from enex_tag_mapper import EnexTagMapper, RoutingResult

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("enex_processor")

# ---------------------------------------------------------------------------
# Konfiguration aus Umgebungsvariablen
# ---------------------------------------------------------------------------

VAULT_PATH        = Path(os.getenv("VAULT_PATH", "/data/vault"))
ENEX_TAGS_CONFIG  = Path(os.getenv("ENEX_TAGS_CONFIG", "/config/enex-tags.yaml"))
TIMEZONE          = ZoneInfo(os.getenv("ENEX_DEFAULT_TIMEZONE", "Europe/Berlin"))
IMPORT_IMAGES     = os.getenv("ENEX_IMPORT_IMAGES", "false").lower() == "true"
LLM_FALLBACK      = os.getenv("ENEX_LLM_FALLBACK", "false").lower() == "true"
BATCH_TELEGRAM    = os.getenv("ENEX_BATCH_TELEGRAM", "true").lower() == "true"
DB_PATH           = Path(os.getenv("DB_PATH", "/config/dispatcher.db"))
OLLAMA_URL        = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
ANLAGEN_FOLDER    = VAULT_PATH / "Anlagen"

CURRENT_YEAR = datetime.now().year

# ---------------------------------------------------------------------------
# SQLite: Schema-Migration
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {
    "import_source":    "TEXT DEFAULT NULL",
    "enex_tags":        "TEXT DEFAULT NULL",
    "note_hash":        "TEXT DEFAULT NULL",
    "ocr_status":       "TEXT DEFAULT NULL",
    "ocr_processed_at": "TEXT DEFAULT NULL",
    "ocr_source":       "TEXT DEFAULT NULL",
}


def migrate_db(conn: sqlite3.Connection):
    """Fügt fehlende ENEX-Spalten zur dokumente-Tabelle hinzu (idempotent)."""
    cursor = conn.execute("PRAGMA table_info(dokumente)")
    existing = {row[1] for row in cursor.fetchall()}
    for col, definition in REQUIRED_COLUMNS.items():
        if col not in existing:
            logger.info("DB-Migration: Spalte '%s' wird hinzugefügt", col)
            conn.execute(f"ALTER TABLE dokumente ADD COLUMN {col} {definition}")
    conn.commit()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    migrate_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Duplikat-Checks
# ---------------------------------------------------------------------------

def is_duplicate_by_hash(conn: sqlite3.Connection, note_hash: str) -> bool:
    """Prüft auf SHA1(title+created)-Duplikat."""
    row = conn.execute(
        "SELECT id FROM dokumente WHERE note_hash = ?", (note_hash,)
    ).fetchone()
    return row is not None


def find_existing_by_pdf_hash(conn: sqlite3.Connection, md5: str) -> Optional[sqlite3.Row]:
    """Gibt die komplette DB-Zeile zurück wenn ein PDF-Hash-Treffer existiert."""
    return conn.execute(
        "SELECT * FROM dokumente WHERE pdf_hash = ?", (md5,)
    ).fetchone()


# ---------------------------------------------------------------------------
# ENEX-Merge in existierendes Dokument
# ---------------------------------------------------------------------------

def merge_enex_into_db(
    conn: sqlite3.Connection,
    existing_id: int,
    note: "Note",
    routing: "RoutingResult",
) -> None:
    """
    Aktualisiert einen bestehenden DB-Eintrag mit ENEX-Metadaten.
    Überschreibt nur Felder die bisher NULL sind (adressat, kategorie, typ).
    """
    existing = conn.execute(
        "SELECT adressat, kategorie, typ, import_source FROM dokumente WHERE id = ?",
        (existing_id,),
    ).fetchone()
    if not existing:
        return

    updates: dict = {
        "import_source": "enex",
        "enex_tags":     json.dumps(note.tags, ensure_ascii=False),
        "note_hash":     note.note_hash,
        "ocr_status":    "merged",   # Docling-OCR war bereits vorhanden
    }
    # Felder nur setzen wenn bisher leer
    if not existing["adressat"] and routing.adressat_hint:
        updates["adressat"] = routing.adressat_hint
    if not existing["kategorie"] and routing.kategorie:
        updates["kategorie"] = routing.kategorie
    if not existing["typ"] and routing.typ:
        updates["typ"] = routing.typ

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE dokumente SET {set_clause} WHERE id = ?",
        (*updates.values(), existing_id),
    )
    conn.commit()
    logger.info("DB aktualisiert (id=%d): ENEX-Metadaten gemerged", existing_id)


def merge_enex_into_frontmatter(
    md_path: Path,
    note: "Note",
    routing: "RoutingResult",
    local_tz: "ZoneInfo",
) -> bool:
    """
    Liest eine bestehende Markdown-Datei und ergänzt/aktualisiert ENEX-Metadaten
    im Frontmatter, ohne bestehende Dispatcher-Felder zu überschreiben.

    Merge-Regeln:
    - import_quelle: enex  → wird gesetzt (immer)
    - evernote_title       → wird gesetzt wenn nicht vorhanden
    - tags                 → ENEX-Tags werden zu bestehenden Tags addiert (Union)
    - adressat             → nur gesetzt wenn bisher leer
    - Datum_original       → nur gesetzt wenn bisher leer

    Returns True bei Erfolg.
    """
    if not md_path.exists():
        logger.warning("Frontmatter-Merge: MD nicht gefunden: %s", md_path)
        return False

    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("Frontmatter-Merge: Lesen fehlgeschlagen: %s", exc)
        return False

    # Frontmatter-Block extrahieren
    fm_match = re.match(r'^---\r?\n(.*?)\r?\n---\r?\n', content, re.DOTALL)
    if not fm_match:
        # Kein Frontmatter vorhanden → neu anlegen
        fm_text = _build_minimal_frontmatter(note, routing, local_tz)
        content = fm_text + content
        md_path.write_text(content, encoding="utf-8")
        logger.info("Frontmatter-Merge: neues Frontmatter angelegt in %s", md_path.name)
        return True

    raw_fm   = fm_match.group(1)
    rest     = content[fm_match.end():]

    try:
        fm: dict = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError as exc:
        logger.warning("Frontmatter-Merge: YAML-Parse-Fehler in %s: %s", md_path.name, exc)
        fm = {}

    changed = False

    # import_quelle
    if fm.get("import_quelle") != "enex":
        fm["import_quelle"] = "enex"
        changed = True

    # evernote_title (original Evernote-Titel)
    if "evernote_title" not in fm and note.title:
        fm["evernote_title"] = note.title
        changed = True

    # tags: Union bestehender Tags + normalisierte ENEX-Tags
    existing_tags: list = fm.get("tags") or []
    if isinstance(existing_tags, str):
        existing_tags = [existing_tags]
    enex_tags = routing.normalized_tags or []
    merged_tags = list(existing_tags)
    for t in enex_tags:
        if t not in merged_tags:
            merged_tags.append(t)
    if merged_tags != list(existing_tags):
        fm["tags"] = merged_tags
        changed = True

    # adressat: nur wenn bisher leer
    if not fm.get("adressat") and routing.adressat_hint:
        fm["adressat"] = routing.adressat_hint
        changed = True

    # Datum_original: nur wenn bisher leer
    if not fm.get("Datum_original") and note.created:
        fm["Datum_original"] = note.created.astimezone(local_tz).strftime("%Y-%m-%d")
        changed = True

    if not changed:
        logger.debug("Frontmatter-Merge: keine Änderungen nötig in %s", md_path.name)
        return True

    # Frontmatter neu serialisieren — yaml.dump mit unicode-safe, kein Flow-Style
    new_fm_yaml = yaml.dump(
        fm,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip("\n")
    new_content = f"---\n{new_fm_yaml}\n---\n{rest}"

    try:
        md_path.write_text(new_content, encoding="utf-8")
        logger.info("Frontmatter-Merge: %s aktualisiert (%d Tags, adressat=%s)",
                    md_path.name, len(fm.get("tags", [])), fm.get("adressat"))
        return True
    except Exception as exc:
        logger.error("Frontmatter-Merge: Schreiben fehlgeschlagen: %s", exc)
        return False


def _build_minimal_frontmatter(
    note: "Note",
    routing: "RoutingResult",
    local_tz: "ZoneInfo",
) -> str:
    """Minimales Frontmatter wenn das Dokument noch keines hat."""
    fm: dict = {
        "evernote_title": note.title,
        "import_quelle":  "enex",
        "tags":           routing.normalized_tags or [],
    }
    if note.created:
        fm["Datum_original"] = note.created.astimezone(local_tz).strftime("%Y-%m-%d")
    if routing.adressat_hint:
        fm["adressat"] = routing.adressat_hint
    raw = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).rstrip("\n")
    return f"---\n{raw}\n---\n"


# ---------------------------------------------------------------------------
# Dateiname-Generierung
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_UNDERSCORE = re.compile(r'_+')


def sanitize_filename_part(text: str, max_len: int = 60) -> str:
    """Bereinigt Text für Dateinamen: Sonderzeichen → _, gekürzt auf max_len."""
    text = text.strip()
    text = _UNSAFE_CHARS.sub("_", text)
    text = text.replace(" ", "_")
    text = _MULTI_UNDERSCORE.sub("_", text)
    text = text.strip("_")
    return text[:max_len]


def make_filename(note: Note, routing: RoutingResult, local_tz: ZoneInfo) -> str:
    """
    Erzeugt den Vault-Dateinamen ohne Erweiterung.

    Wenn der Note-Titel bereits mit YYYYMMDD beginnt (Nutzer hat Datum
    schon in den Titel eingetragen), wird der Titel direkt verwendet —
    kein doppeltes Datum und kein Kategorie-Suffix.
    Sonst: YYYYMMDD_Quelle_Titel
    """
    title_clean = sanitize_filename_part(note.title)

    # Titel beginnt schon mit YYYYMMDD → direkt verwenden
    if re.match(r'^\d{8}', title_clean):
        return title_clean

    local_dt = note.created.astimezone(local_tz)
    date_str = local_dt.strftime("%Y%m%d")

    source_map = {
        "krankenversicherung": "KV",
        "immobilien_eigen":    "Immobilien",
        "immobilien_vermietet": "ImmV",
        "finanzen":            "Finanzen",
        "fahrzeuge":           "KFZ",
        "reisen":              "Reisen",
        "business":            "Business",
        "digitales":           "Digitales",
        "persoenlich":         "Persönlich",
        "familie":             "Familie",
        "italien":             "Italien",
    }
    source = source_map.get(routing.kategorie or "", "Evernote")
    return f"{date_str}_{source}_{title_clean}"


# ---------------------------------------------------------------------------
# Vault-Pfad (Jahresordner)
# ---------------------------------------------------------------------------

def get_vault_path(
    vault_root: Path,
    vault_folder: str,
    note: Note,
    local_tz: ZoneInfo,
) -> Path:
    """
    Bestimmt den Ablage-Pfad im Vault mit Jahresordner-Logik:
      Aktuelles Jahr → {vault_root}/{vault_folder}/
      Vergangene Jahre → {vault_root}/{vault_folder}/{YYYY}/
    """
    local_dt = note.created.astimezone(local_tz)
    year = local_dt.year
    base = vault_root / vault_folder
    if year < CURRENT_YEAR:
        return base / str(year)
    return base


# ---------------------------------------------------------------------------
# Frontmatter-Generierung
# ---------------------------------------------------------------------------

def build_frontmatter(
    note: Note,
    routing: RoutingResult,
    filename_stem: str,
    pdf_filename: Optional[str],
    local_tz: ZoneInfo,
) -> str:
    """
    Erzeugt YAML-Frontmatter gemäß VAULT_FRONTMATTER_SPEC.
    Pflichtfelder: Datum_original, tags.
    Verboten: date, created, erstellt_am, date_created.
    """
    local_dt = note.created.astimezone(local_tz)
    datum_original = local_dt.strftime("%Y-%m-%d")

    updated_str = None
    if note.updated:
        updated_str = note.updated.astimezone(local_tz).strftime("%Y-%m-%d")

    tags = routing.normalized_tags if routing.normalized_tags else ["Evernote"]

    lines = ["---"]
    lines.append(f'title: "{note.title}"')
    lines.append(f"Datum_original: {datum_original}")
    if updated_str:
        lines.append(f"date modified: {updated_str}")
    lines.append("tags:")
    for tag in tags:
        lines.append(f"  - {tag}")
    lines.append("source: evernote")
    if routing.kategorie:
        lines.append(f"kategorie: {routing.kategorie}")
    if routing.typ:
        lines.append(f"typ: {routing.typ}")
    if routing.adressat_hint:
        lines.append(f"adressat: {routing.adressat_hint}")
    if pdf_filename:
        lines.append(f'original: "[[Anlagen/{pdf_filename}]]"')
    lines.append("import_quelle: enex")
    ocr_status = "pending" if pdf_filename else "not_required"
    lines.append(f"ocr_status: {ocr_status}")
    lines.append("ocr_source:")
    lines.append("---")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# LLM-Fallback (Klassifikation via Ollama)
# ---------------------------------------------------------------------------

def _classify_with_llm(note: Note) -> Optional[RoutingResult]:
    """
    Fragt qwen2.5:7b nach Klassifikation wenn kein Tag-Match.
    Nur aktiv wenn ENEX_LLM_FALLBACK=true.
    """
    categories_hint = (
        "Verfügbare Kategorien: krankenversicherung, immobilien_eigen, "
        "immobilien_vermietet, finanzen, fahrzeuge, reisen, business, "
        "persoenlich, familie, digitales, fengshui, garten, italien, "
        "wissen, bedienungsanleitungen, archiv"
    )
    prompt = (
        f"Klassifiziere diese Evernote-Notiz. Die Sprache kann Deutsch oder Italienisch sein.\n\n"
        f"Titel: {note.title}\n"
        f"Tags: {', '.join(note.tags)}\n\n"
        f"{categories_hint}\n\n"
        f"Antworte NUR mit JSON (keine Erklärung):\n"
        f'{{"kategorie_id": "...", "typ_id": "...", "konfidenz": 0.0}}'
    )
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=180,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        # JSON aus Antwort extrahieren
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        return RoutingResult(
            vault_folder="00 Inbox",   # wird unten überschrieben
            kategorie=data.get("kategorie_id"),
            typ=data.get("typ_id"),
            normalized_tags=[],
            source="llm",
        )
    except Exception as exc:
        logger.warning("LLM-Fallback fehlgeschlagen: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Telegram-Benachrichtigung
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    """Sendet eine Telegram-Nachricht an den konfigurierten Chat."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram nicht konfiguriert — Nachricht übersprungen")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Telegram-Versand fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------
# Kern-Verarbeitung: eine Note
# ---------------------------------------------------------------------------

def process_note(
    note: Note,
    mapper: EnexTagMapper,
    conn: sqlite3.Connection,
    enex_prefix: Optional[str],
    enex_typ_override: Optional[str],
) -> Tuple[str, str]:
    """
    Verarbeitet eine einzelne Note.

    Returns:
        (status, vault_md_path) — status: "imported" | "duplicate" | "error"
    """
    # 1. Duplikat-Check (Note-Hash)
    if is_duplicate_by_hash(conn, note.note_hash):
        logger.info("Duplikat übersprungen (note_hash): %r", note.title)
        return "duplicate", ""

    # 2. Routing-Entscheidung
    routing: Optional[RoutingResult] = None

    if enex_prefix:
        routing = mapper.route_by_prefix(enex_prefix, note.tags, enex_typ_override)

    if routing is None:
        routing = mapper.route_by_tags(note.tags)

    if routing.source == "fallback" and LLM_FALLBACK:
        llm_result = _classify_with_llm(note)
        if llm_result and llm_result.kategorie:
            # LLM-Ergebnis anwenden, aber Fallback-Routing ergänzen
            routing.kategorie = llm_result.kategorie
            routing.typ = llm_result.typ
            routing.vault_folder = mapper._kategorie_to_folder(llm_result.kategorie)
            routing.source = "llm"

    logger.info("Routing: %r → %s (source=%s)", note.title, routing.vault_folder, routing.source)

    # 3. Vault-Pfad bestimmen
    vault_dir = get_vault_path(VAULT_PATH, routing.vault_folder, note, TIMEZONE)
    vault_dir.mkdir(parents=True, exist_ok=True)
    ANLAGEN_FOLDER.mkdir(parents=True, exist_ok=True)

    # 4. Dateiname generieren
    stem = make_filename(note, routing, TIMEZONE)

    # 5. MD bereits vorhanden? → merge statt _1 anlegen (vor PDF-Schreiben prüfen!)
    md_filename = f"{stem}.md"
    md_dest = vault_dir / md_filename

    if md_dest.exists():
        logger.info("MD bereits vorhanden: %s — merge statt Duplikat anlegen", md_filename)
        relative_path = str(md_dest.relative_to(VAULT_PATH))
        existing_db = conn.execute(
            "SELECT id FROM dokumente WHERE vault_pfad = ? OR dateiname = ?",
            (relative_path, md_filename),
        ).fetchone()
        merge_enex_into_frontmatter(md_dest, note, routing, TIMEZONE)
        if existing_db:
            merge_enex_into_db(conn, existing_db["id"], note, routing)
        else:
            # Datei existiert, aber kein DB-Eintrag (z.B. nach gescheitertem ersten Import)
            _pdf_md5 = note.pdf_resources[0].md5 if note.pdf_resources else None
            _local_dt = note.created.astimezone(TIMEZONE)
            conn.execute(
                """
                INSERT OR IGNORE INTO dokumente
                    (dateiname, vault_pfad, kategorie, typ, adressat,
                     rechnungsdatum, pdf_hash, erstellt_am,
                     import_source, enex_tags, note_hash, ocr_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    md_filename, relative_path,
                    routing.kategorie, routing.typ, routing.adressat_hint,
                    _local_dt.strftime("%Y-%m-%d"), _pdf_md5,
                    datetime.now(timezone.utc).isoformat(),
                    "enex", json.dumps(note.tags, ensure_ascii=False),
                    note.note_hash,
                    "pending" if _pdf_md5 else "not_required",
                ),
            )
            conn.commit()
            logger.info("DB-Eintrag nachträglich angelegt: %s", md_filename)
        return "merged", relative_path

    # 6. PDF-Duplikat-Check und PDF-Ablage
    pdf_filename: Optional[str] = None
    pdf_md5: Optional[str] = None

    if note.pdf_resources:
        pdf_res = note.pdf_resources[0]
        pdf_md5 = pdf_res.md5

        existing_row = find_existing_by_pdf_hash(conn, pdf_md5)
        if existing_row is not None:
            logger.info(
                "PDF-Duplikat gefunden (id=%d, %s) — merge ENEX-Metadaten",
                existing_row["id"], existing_row["dateiname"],
            )
            merge_enex_into_db(conn, existing_row["id"], note, routing)
            vault_pfad = existing_row["vault_pfad"] if existing_row["vault_pfad"] else ""
            if vault_pfad:
                md_full = VAULT_PATH / vault_pfad
                merge_enex_into_frontmatter(md_full, note, routing, TIMEZONE)
            return "merged", vault_pfad

        pdf_filename = f"{stem}.pdf"
        pdf_dest = ANLAGEN_FOLDER / pdf_filename
        if pdf_dest.exists():
            # PDF schon auf Disk (z.B. aus gescheitertem Import) — wiederverwenden
            logger.debug("PDF bereits vorhanden, wiederverwendet: %s", pdf_filename)
        else:
            pdf_dest.write_bytes(pdf_res.data)
            logger.debug("PDF gespeichert: %s", pdf_dest)

    # 7. ENML → Markdown
    image_fnames = [r.filename for r in note.image_resources] if IMPORT_IMAGES else []
    body_md = enml_to_markdown(
        note.content_enml,
        image_filenames=image_fnames if IMPORT_IMAGES else None,
        include_images=IMPORT_IMAGES,
    )

    # 8. Bilder kopieren (wenn aktiviert)
    if IMPORT_IMAGES and note.image_resources:
        for img_res in note.image_resources:
            img_dest = ANLAGEN_FOLDER / img_res.filename
            if not img_dest.exists():
                img_dest.write_bytes(img_res.data)

    # 9. Frontmatter aufbauen
    frontmatter = build_frontmatter(note, routing, stem, pdf_filename, TIMEZONE)

    # 10. OCR-Platzhalter wenn PDF vorhanden
    ocr_block = make_ocr_placeholder() if pdf_filename else ""

    # 11. Markdown-Datei schreiben
    md_content = frontmatter + "\n" + body_md + ocr_block
    md_dest.write_text(md_content, encoding="utf-8")
    logger.info("MD gespeichert: %s", md_dest.relative_to(VAULT_PATH))

    # 12. SQLite-Eintrag
    relative_md = str(md_dest.relative_to(VAULT_PATH))
    ocr_status = "pending" if pdf_filename else "not_required"
    local_dt = note.created.astimezone(TIMEZONE)

    conn.execute(
        """
        INSERT INTO dokumente
            (dateiname, vault_pfad, kategorie, typ, adressat,
             rechnungsdatum, pdf_hash, erstellt_am,
             import_source, enex_tags, note_hash, ocr_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            md_filename,
            relative_md,
            routing.kategorie,
            routing.typ,
            routing.adressat_hint,
            local_dt.strftime("%Y-%m-%d"),
            pdf_md5,
            datetime.now(timezone.utc).isoformat(),
            "enex",
            json.dumps(note.tags, ensure_ascii=False),
            note.note_hash,
            ocr_status,
        ),
    )
    conn.commit()

    return "imported", relative_md


# ---------------------------------------------------------------------------
# Haupt-Einstiegspunkt
# ---------------------------------------------------------------------------

def _parse_enex_prefix(enex_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Extrahiert nn_-Präfix und optionale __typ-id aus dem ENEX-Dateinamen.
    Beispiel: '49_KV_Marion__arztrechnung.enex' → ('49', 'arztrechnung')
    """
    stem = enex_path.stem
    prefix_match = re.match(r'^(\d{2})_', stem)
    if not prefix_match:
        return None, None
    prefix = prefix_match.group(1)
    typ_match = re.search(r'__([a-z_]+)$', stem)
    typ_override = typ_match.group(1) if typ_match else None
    return prefix, typ_override


def process_enex_file(enex_path: str | Path) -> dict:
    """
    Verarbeitet eine komplette ENEX-Datei.

    Returns:
        Stats-Dict mit: imported, duplicate, error, total, pdf_pending
    """
    enex_path = Path(enex_path)
    logger.info("=" * 60)
    logger.info("ENEX-Import: %s", enex_path.name)

    stats = {"imported": 0, "merged": 0, "duplicate": 0, "error": 0, "total": 0, "pdf_pending": 0}

    # Konfiguration laden
    mapper = EnexTagMapper(ENEX_TAGS_CONFIG)
    conn = get_db_connection()
    enex_prefix, enex_typ = _parse_enex_prefix(enex_path)

    if enex_prefix:
        logger.info("Kategorie-Präfix erkannt: %s (typ: %s)", enex_prefix, enex_typ)

    # Notes parsen
    try:
        notes = parse_enex(enex_path)
    except Exception as exc:
        logger.error("ENEX-Parsing fehlgeschlagen: %s", exc)
        conn.close()
        return stats

    stats["total"] = len(notes)

    for note in notes:
        try:
            status, md_path = process_note(note, mapper, conn, enex_prefix, enex_typ)
            stats[status] = stats.get(status, 0) + 1
            if status == "imported" and note.pdf_resources:
                stats["pdf_pending"] += 1
        except Exception as exc:
            logger.error("Fehler bei Note %r: %s", note.title, exc, exc_info=True)
            stats["error"] += 1

    conn.close()

    # Zusammenfassung
    logger.info(
        "Fertig: %d importiert | %d gemerged | %d Duplikate | %d Fehler | %d PDFs für Nachtlauf",
        stats["imported"], stats["merged"], stats["duplicate"], stats["error"], stats["pdf_pending"]
    )

    # Telegram-Benachrichtigung
    if BATCH_TELEGRAM:
        msg = (
            f"🐘 <b>ENEX-Import abgeschlossen</b>\n"
            f"📄 {enex_path.name}\n"
            f"📊 {stats['total']} Notizen | "
            f"✅ {stats['imported']} importiert | "
            f"🔀 {stats['merged']} gemerged | "
            f"♻️ {stats['duplicate']} Duplikate | "
            f"❌ {stats['error']} Fehler\n"
            f"📎 {stats['pdf_pending']} PDFs → OCR-Nachtlauf vorgemerkt"
        )
        send_telegram(msg)

    return stats


def main():
    parser = argparse.ArgumentParser(description="ENEX-Import Phase 1 — Sofortimport")
    parser.add_argument(
        "enex_path",
        help="Pfad zur .enex-Datei ODER Verzeichnis mit .enex-Dateien",
    )
    parser.add_argument("--dry-run", action="store_true", help="Nichts schreiben, nur loggen")
    args = parser.parse_args()

    path = Path(args.enex_path)

    # -----------------------------------------------------------------------
    # Verzeichnis-Modus: alle .enex-Dateien im Ordner verarbeiten
    # -----------------------------------------------------------------------
    if path.is_dir():
        enex_files = sorted(path.glob("*.enex"))
        if not enex_files:
            logger.info("Keine .enex-Dateien in %s gefunden.", path)
            sys.exit(0)

        logger.info("Verzeichnis-Modus: %d .enex-Dateien gefunden", len(enex_files))

        if args.dry_run:
            logger.warning("DRY-RUN-Modus: Keine Dateien werden geschrieben")
            for f in enex_files:
                notes = parse_enex(str(f))
                logger.info("[%s] %d Notes", f.name, len(notes))
                for n in notes:
                    logger.info("  - %r (%s) Tags: %s", n.title,
                                n.created.strftime("%Y-%m-%d"), n.tags)
            return

        total_stats: dict = {"imported": 0, "duplicate": 0, "error": 0, "pdf_pending": 0}
        for f in enex_files:
            logger.info("--- Verarbeite: %s ---", f.name)
            stats = process_enex_file(str(f))
            for k in total_stats:
                total_stats[k] += stats.get(k, 0)

        logger.info(
            "Verzeichnis-Lauf abgeschlossen: %d importiert, %d Duplikate, "
            "%d Fehler, %d PDFs → OCR",
            total_stats["imported"], total_stats["duplicate"],
            total_stats["error"], total_stats["pdf_pending"],
        )
        sys.exit(0 if total_stats["error"] == 0 else 1)

    # -----------------------------------------------------------------------
    # Einzeldatei-Modus (bisheriges Verhalten)
    # -----------------------------------------------------------------------
    if args.dry_run:
        logger.warning("DRY-RUN-Modus: Keine Dateien werden geschrieben")
        notes = parse_enex(args.enex_path)
        logger.info("Dry-Run: %d Notes gefunden", len(notes))
        for n in notes:
            logger.info("  - %r (%s) Tags: %s", n.title,
                        n.created.strftime("%Y-%m-%d"), n.tags)
        return

    stats = process_enex_file(args.enex_path)
    sys.exit(0 if stats["error"] == 0 else 1)


if __name__ == "__main__":
    main()
