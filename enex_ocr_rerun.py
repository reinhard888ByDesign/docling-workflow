#!/usr/bin/env python3
"""
enex_ocr_rerun.py — OCR-Nachtlauf für Vault-MDs mit ocr_status: pending

Findet alle vault-MDs mit ocr_status: pending, holt Docling-OCR vom Host,
und aktualisiert die MD-Dateien (Frontmatter + Body).

Laufzeit-Schätzung: ~21 Stunden für 2246 Dokumente (Ø 35s/Dok)
Kann jederzeit unterbrochen und neu gestartet werden (bereits erledigte werden übersprungen).
"""
import re
import sys
import time
import logging
import argparse
import requests
from pathlib import Path

VAULT       = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
ANLAGEN     = VAULT / "Anlagen"
DOCLING_URL = "http://localhost:5001/v1/convert/file"
TIMEOUT     = 300   # Sekunden pro Docling-Aufruf
PDFMINER_MIN = 300  # Zeichen — darunter gilt born-digital als unzureichend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/home/reinhard/docker/RYZEN - docling-workflow/dispatcher-temp/enex_ocr_rerun.log", encoding="utf-8"),
    ],
)
log = logging.getLogger()

FM_RE    = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
OCR_RE   = re.compile(r'(##\s*Dokumentinhalt\s*\(OCR\).*?)(?=\n##|\Z)', re.DOTALL)
ORIG_RE  = re.compile(r"^original:\s*['\"]?\[\[([^\]]+)\]\]['\"]?", re.M)

def find_pending_mds() -> list[Path]:
    return sorted(
        f for f in VAULT.rglob("*.md")
        if "ocr_status: pending" in f.read_text("utf-8", errors="ignore")
    )

def get_pdf_path(md: Path) -> Path | None:
    text = md.read_text("utf-8", errors="ignore")
    m = ORIG_RE.search(text)
    if not m:
        return None
    rel = m.group(1).strip()
    # "Anlagen/foo.pdf" → absolut
    pdf = VAULT / rel
    return pdf if pdf.exists() else None

def docling_ocr(pdf: Path) -> str | None:
    """Ruft Docling v1/convert/file auf, gibt Markdown-Text zurück oder None."""
    try:
        with pdf.open("rb") as fh:
            r = requests.post(
                DOCLING_URL,
                files={"files": (pdf.name, fh, "application/pdf")},
                timeout=TIMEOUT,
            )
        if not r.ok:
            log.warning(f"Docling HTTP {r.status_code} für {pdf.name}")
            return None
        data = r.json()
        item = data[0] if isinstance(data, list) else data
        if item.get("status") != "success":
            log.warning(f"Docling status={item.get('status')} für {pdf.name}")
            return None
        # Markdown kann in verschiedenen Feldern liegen
        md_text = (
            item.get("markdown")
            or item.get("output", {}).get("markdown")
            or (item.get("document", {}) or {}).get("md_content")
            or ""
        )
        return md_text.strip() or None
    except requests.Timeout:
        log.warning(f"Docling Timeout nach {TIMEOUT}s: {pdf.name}")
        return None
    except Exception as e:
        log.warning(f"Docling Fehler für {pdf.name}: {e}")
        return None

def pdfminer_text(pdf: Path) -> str:
    """Extrahiert Text direkt aus born-digital PDF (kein OCR)."""
    try:
        from pdfminer.high_level import extract_text
        return (extract_text(str(pdf)) or "").strip()
    except Exception:
        return ""

def update_md(md: Path, ocr_text: str, source: str) -> bool:
    text = md.read_text("utf-8")

    # Frontmatter aktualisieren
    text = text.replace("ocr_status: pending", "ocr_status: completed", 1)
    text = re.sub(r"^ocr_source:.*$", f"ocr_source: {source}", text, count=1, flags=re.M)

    # Bestehenden OCR-Block ersetzen oder am Ende anhängen
    new_block = f"## Dokumentinhalt (OCR)\n\n{ocr_text}\n"
    if OCR_RE.search(text):
        text = OCR_RE.sub(lambda _: new_block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + new_block

    try:
        md.write_text(text, "utf-8")
        return True
    except Exception as e:
        log.error(f"Schreiben fehlgeschlagen: {md.name}: {e}")
        return False

def process_all(dry_run: bool, limit: int, skip_born_digital: bool):
    mds = find_pending_mds()
    if limit:
        mds = mds[:limit]
    total = len(mds)
    log.info(f"{'[DRY-RUN] ' if dry_run else ''}Starte OCR-Lauf: {total} Dokumente")

    stats = {"done": 0, "born_digital": 0, "no_pdf": 0, "failed": 0, "skipped": 0}
    t_start = time.time()

    for i, md in enumerate(mds, 1):
        pdf = get_pdf_path(md)
        if not pdf:
            log.info(f"[{i}/{total}] KEIN PDF: {md.name}")
            stats["no_pdf"] += 1
            continue

        # Born-digital prüfen (Text-PDF ohne echtes Scan-OCR nötig)
        bd_text = pdfminer_text(pdf)
        if len(bd_text) >= PDFMINER_MIN:
            if skip_born_digital:
                log.info(f"[{i}/{total}] SKIP born-digital ({len(bd_text)}Z): {md.name}")
                stats["skipped"] += 1
                continue
            # Born-digital: pdfminer-Text ist gut genug
            source = "pdfminer"
            ocr_text = bd_text
            log.info(f"[{i}/{total}] pdfminer ({len(ocr_text)}Z): {md.name}")
            stats["born_digital"] += 1
        else:
            # Echter Scan → Docling
            log.info(f"[{i}/{total}] Docling ({pdf.stat().st_size//1024}KB): {md.name}")
            t0 = time.time()
            ocr_text = docling_ocr(pdf)
            elapsed = int(time.time() - t0)
            if not ocr_text:
                log.warning(f"  → fehlgeschlagen nach {elapsed}s")
                stats["failed"] += 1
                continue
            source = "docling"
            log.info(f"  → {len(ocr_text)}Z in {elapsed}s")
            stats["done"] += 1

        if not dry_run:
            update_md(md, ocr_text, source)

        # Fortschritt + ETA alle 10 Dokumente
        processed = stats["done"] + stats["born_digital"]
        if processed > 0 and processed % 10 == 0:
            elapsed_total = time.time() - t_start
            avg = elapsed_total / processed
            remaining = total - i
            eta_h = int(remaining * avg / 3600)
            eta_m = int((remaining * avg % 3600) / 60)
            log.info(f"  ── Fortschritt: {i}/{total} | Ø {avg:.0f}s/Dok | ETA ~{eta_h}h{eta_m:02d}m")

    elapsed_total = time.time() - t_start
    log.info("=" * 50)
    log.info(f"Fertig in {elapsed_total/3600:.1f}h")
    log.info(f"  Docling OCR:    {stats['done']}")
    log.info(f"  Born-digital:   {stats['born_digital']}")
    log.info(f"  Kein PDF:       {stats['no_pdf']}")
    log.info(f"  Fehlgeschlagen: {stats['failed']}")
    log.info(f"  Übersprungen:   {stats['skipped']}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",          action="store_true")
    ap.add_argument("--limit",            type=int, default=0)
    ap.add_argument("--skip-born-digital",action="store_true",
                    help="Nur echte Scans (kein born-digital PDF)")
    args = ap.parse_args()
    process_all(args.dry_run, args.limit, args.skip_born_digital)
