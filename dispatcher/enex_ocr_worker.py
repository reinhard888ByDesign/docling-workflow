"""
enex_ocr_worker.py — Phase 2: OCR-Nachtlauf
Verarbeitet die OCR-Queue aus SQLite. Läuft täglich 00:00–07:00.

Aufruf (Cron täglich 00:00):
    python enex_ocr_worker.py

Optionen:
    --dry-run       Nicht schreiben, nur Queue anzeigen
    --limit N       Maximal N Dokumente verarbeiten
    --force-window  Zeitfenster ignorieren (für manuellen Test)

Umgebungsvariablen:
    VAULT_PATH                  Pfad zum Vault
    DB_PATH                     SQLite-DB, default /config/dispatcher.db
    ENEX_OCR_WINDOW_START       Startzeit, default 00:00
    ENEX_OCR_WINDOW_END         Endzeit (Stopp), default 07:00
    ENEX_OCR_PDFMINER_MIN_CHARS Schwelle born-digital, default 300
    ENEX_OCR_DOCLING_TIMEOUT    Max. Sekunden pro Docling-Aufruf, default 300
    DOCLING_URL                 docling-serve Endpoint, default http://docling-serve:5001
    TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import requests

from enml_to_markdown import replace_ocr_placeholder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("enex_ocr_worker")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

VAULT_PATH   = Path(os.getenv("VAULT_PATH", "/data/vault"))
DB_PATH      = Path(os.getenv("DB_PATH", "/config/dispatcher.db"))
ANLAGEN_DIR  = VAULT_PATH / "Anlagen"

_OCR_START_STR = os.getenv("ENEX_OCR_WINDOW_START", "00:00")
_OCR_END_STR   = os.getenv("ENEX_OCR_WINDOW_END",   "07:00")
PDFMINER_MIN_CHARS  = int(os.getenv("ENEX_OCR_PDFMINER_MIN_CHARS", "300"))
DOCLING_TIMEOUT     = int(os.getenv("ENEX_OCR_DOCLING_TIMEOUT",   "300"))
DOCLING_URL         = os.getenv("DOCLING_URL", "http://docling-serve:5001")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def _parse_hhmm(s: str) -> Tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


OCR_WINDOW_END_H, OCR_WINDOW_END_M = _parse_hhmm(_OCR_END_STR)


# ---------------------------------------------------------------------------
# Zeitfenster-Check
# ---------------------------------------------------------------------------

def within_ocr_window() -> bool:
    """Gibt True zurück solange wir noch vor OCR_WINDOW_END liegen."""
    now = datetime.now()
    end_today = now.replace(
        hour=OCR_WINDOW_END_H, minute=OCR_WINDOW_END_M, second=0, microsecond=0
    )
    return now < end_today


# ---------------------------------------------------------------------------
# OCR-Methoden
# ---------------------------------------------------------------------------

def _extract_with_pdfminer(pdf_path: Path) -> Optional[str]:
    """
    Extrahiert Text aus einem born-digital PDF (< 1 Sek.).
    Gibt None zurück wenn pdfminer nicht verfügbar oder Text zu kurz.
    """
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(pdf_path))
        if text and len(text.strip()) >= PDFMINER_MIN_CHARS:
            logger.debug("pdfminer: %d Zeichen extrahiert", len(text.strip()))
            return text.strip()
        return None
    except ImportError:
        logger.warning("pdfminer.six nicht installiert — überspringe born-digital-Check")
        return None
    except Exception as exc:
        logger.warning("pdfminer fehlgeschlagen für %s: %s", pdf_path.name, exc)
        return None


def _extract_with_docling(pdf_path: Path) -> Optional[str]:
    """
    Sendet PDF an docling-serve und gibt den extrahierten Markdown-Text zurück.
    Timeout: DOCLING_TIMEOUT Sekunden.
    """
    try:
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                f"{DOCLING_URL}/convert",
                files={"file": (pdf_path.name, f, "application/pdf")},
                timeout=DOCLING_TIMEOUT,
            )
        resp.raise_for_status()
        result = resp.json()

        # docling-serve gibt {"markdown": "..."} oder {"output": "..."} zurück
        text = result.get("markdown") or result.get("output") or result.get("text") or ""
        if text:
            logger.debug("docling: %d Zeichen extrahiert", len(text.strip()))
            return text.strip()
        logger.warning("docling: leere Antwort für %s", pdf_path.name)
        return None
    except requests.Timeout:
        logger.error("docling Timeout (%d s) für %s", DOCLING_TIMEOUT, pdf_path.name)
        return None
    except Exception as exc:
        logger.error("docling fehlgeschlagen für %s: %s", pdf_path.name, exc)
        return None


def run_ocr(pdf_path: Path) -> Tuple[Optional[str], str]:
    """
    Führt OCR durch: erst pdfminer (born-digital), dann Docling (Scan).

    Returns:
        (ocr_text, source) — source: "pdfminer" | "docling" | None bei Fehler
    """
    # Versuch 1: pdfminer (born-digital, < 1 Sek.)
    text = _extract_with_pdfminer(pdf_path)
    if text:
        return text, "pdfminer"

    # Versuch 2: Docling (Scan-OCR, 2–5 Min.)
    logger.info("  → Scan-PDF erkannt, starte Docling für %s", pdf_path.name)
    text = _extract_with_docling(pdf_path)
    if text:
        return text, "docling"

    return None, ""


# ---------------------------------------------------------------------------
# PDF-Pfad aus DB-Eintrag rekonstruieren
# ---------------------------------------------------------------------------

def find_pdf_for_md(md_vault_path: str) -> Optional[Path]:
    """
    Sucht das zugehörige PDF in Anlagen/ basierend auf dem MD-Dateinamen.
    Konvention: YYYYMMDD_Quelle_Titel.md → Anlagen/YYYYMMDD_Quelle_Titel.pdf
    """
    md_path = Path(md_vault_path)
    pdf_name = md_path.stem + ".pdf"
    pdf_path = ANLAGEN_DIR / pdf_name
    if pdf_path.exists():
        return pdf_path

    # Fallback: suche in Anlagen nach Dateinamen die mit dem Stem beginnen
    if ANLAGEN_DIR.exists():
        candidates = list(ANLAGEN_DIR.glob(f"{md_path.stem}*.pdf"))
        if candidates:
            return candidates[0]

    logger.warning("Kein PDF gefunden für: %s", md_vault_path)
    return None


# ---------------------------------------------------------------------------
# MD-Datei aktualisieren
# ---------------------------------------------------------------------------

def update_md_with_ocr(vault_path_rel: str, ocr_text: str) -> bool:
    """
    Ersetzt den OCR-Platzhalter in der MD-Datei durch den echten Text.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    md_path = VAULT_PATH / vault_path_rel
    if not md_path.exists():
        logger.error("MD-Datei nicht gefunden: %s", md_path)
        return False

    try:
        content = md_path.read_text(encoding="utf-8")
        updated = replace_ocr_placeholder(content, ocr_text)
        md_path.write_text(updated, encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("MD-Update fehlgeschlagen (%s): %s", vault_path_rel, exc)
        return False


# ---------------------------------------------------------------------------
# SQLite-Hilfsfunktionen
# ---------------------------------------------------------------------------

def load_pending_queue(conn: sqlite3.Connection) -> list:
    """Lädt alle Dokumente mit ocr_status='pending' aus der DB."""
    rows = conn.execute(
        """
        SELECT id, dateiname, vault_pfad
        FROM dokumente
        WHERE ocr_status = 'pending'
        ORDER BY erstellt_am ASC
        """
    ).fetchall()
    return rows


def update_ocr_status(
    conn: sqlite3.Connection,
    doc_id: int,
    status: str,
    source: str,
):
    """Setzt ocr_status, ocr_source und ocr_processed_at für ein Dokument."""
    conn.execute(
        """
        UPDATE dokumente
        SET ocr_status = ?,
            ocr_source = ?,
            ocr_processed_at = ?
        WHERE id = ?
        """,
        (status, source, datetime.now(timezone.utc).isoformat(), doc_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Telegram fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------
# Haupt-Verarbeitungsschleife
# ---------------------------------------------------------------------------

def run_worker(
    limit: Optional[int] = None,
    force_window: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Verarbeitet die OCR-Queue bis das Zeitfenster endet oder die Queue leer ist.

    Returns:
        Stats-Dict mit: processed, pdfminer, docling, skipped, failed, remaining
    """
    stats = {
        "processed": 0,
        "pdfminer":  0,
        "docling":   0,
        "skipped":   0,
        "failed":    0,
        "remaining": 0,
    }

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    queue = load_pending_queue(conn)
    total_pending = len(queue)

    logger.info("OCR-Nachtlauf gestartet: %d Dokumente in Queue", total_pending)

    if dry_run:
        logger.info("DRY-RUN: %d ausstehende Dokumente", total_pending)
        for row in queue:
            logger.info("  - [%d] %s", row["id"], row["vault_pfad"])
        conn.close()
        stats["remaining"] = total_pending
        return stats

    processed_count = 0

    for row in queue:
        # Zeitfenster-Check
        if not force_window and not within_ocr_window():
            logger.info("Zeitfenster 07:00 erreicht — Worker stoppt")
            stats["remaining"] = total_pending - processed_count
            break

        # Limit-Check
        if limit is not None and processed_count >= limit:
            logger.info("Limit von %d Dokumenten erreicht", limit)
            stats["remaining"] = total_pending - processed_count
            break

        doc_id = row["id"]
        vault_path_rel = row["vault_pfad"]
        filename = row["dateiname"]

        logger.info("[%d/%d] %s", processed_count + 1, total_pending, filename)

        # PDF suchen
        pdf_path = find_pdf_for_md(vault_path_rel)
        if pdf_path is None:
            logger.warning("Kein PDF für %s — markiere als failed", filename)
            update_ocr_status(conn, doc_id, "failed", "")
            stats["failed"] += 1
            processed_count += 1
            continue

        # OCR durchführen
        t0 = time.time()
        ocr_text, source = run_ocr(pdf_path)
        elapsed = time.time() - t0

        if ocr_text:
            # MD-Datei aktualisieren
            success = update_md_with_ocr(vault_path_rel, ocr_text)
            if success:
                update_ocr_status(conn, doc_id, "completed", source)
                stats["processed"] += 1
                stats[source] = stats.get(source, 0) + 1
                logger.info(
                    "  ✅ %s (%.1f s, %d Zeichen)",
                    source, elapsed, len(ocr_text)
                )
            else:
                update_ocr_status(conn, doc_id, "failed", source)
                stats["failed"] += 1
                logger.error("  ❌ MD-Update fehlgeschlagen: %s", vault_path_rel)
        else:
            update_ocr_status(conn, doc_id, "failed", "")
            stats["failed"] += 1
            logger.error("  ❌ OCR fehlgeschlagen: %s", filename)

        processed_count += 1

    else:
        # Schleife vollständig durchgelaufen — Queue leer
        stats["remaining"] = 0

    conn.close()

    logger.info(
        "Nachtlauf abgeschlossen: %d verarbeitet (%d pdfminer, %d docling), "
        "%d fehlgeschlagen, %d verbleibend",
        stats["processed"], stats["pdfminer"], stats["docling"],
        stats["failed"], stats["remaining"]
    )

    # Telegram-Zusammenfassung
    msg_lines = [
        "🌙 <b>OCR-Nachtlauf abgeschlossen</b>",
        f"✅ {stats['processed']} PDFs verarbeitet",
    ]
    if stats["pdfminer"]:
        msg_lines.append(f"   ⚡ {stats['pdfminer']} born-digital (pdfminer, &lt;1 Sek. je)")
    if stats["docling"]:
        msg_lines.append(f"   🔍 {stats['docling']} Scans (Docling)")
    if stats["remaining"]:
        msg_lines.append(f"⏳ {stats['remaining']} noch ausstehend — nächste Nacht")
    if stats["failed"]:
        msg_lines.append(f"❌ {stats['failed']} Fehler → /api/logs?q=ocr_worker")

    send_telegram("\n".join(msg_lines))

    return stats


# ---------------------------------------------------------------------------
# CLI-Einstiegspunkt
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ENEX OCR-Nachtlauf (Phase 2)")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Queue anzeigen ohne zu verarbeiten")
    parser.add_argument("--limit",        type=int, default=None,
                        help="Maximale Anzahl Dokumente")
    parser.add_argument("--force-window", action="store_true",
                        help="Zeitfenster-Check ignorieren (für Tests)")
    args = parser.parse_args()

    stats = run_worker(
        limit=args.limit,
        force_window=args.force_window,
        dry_run=args.dry_run,
    )

    sys.exit(0 if stats["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
