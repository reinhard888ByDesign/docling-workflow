#!/usr/bin/env python3
"""Nachzug-Scan: PDFs in Anlagen/ ohne DB-Eintrag als Inbox-Dokumente eintragen.

Hintergrund: Bug vor dem Fix 2026-04-20 — bei fehlgeschlagener Klassifikation
wurde das PDF nach Anlagen/ verschoben und die MD nach 00 Inbox/, aber kein
DB-Eintrag erzeugt. Diese Dokumente tauchen daher nicht im Review-Dashboard auf.

Dieses Skript:
  1. Listet alle PDFs in VAULT_PDF_ARCHIV (= reinhards-vault/Anlagen/)
  2. Prüft pro PDF, ob ein Eintrag in `dokumente.dateiname` existiert
  3. Orphans erhalten einen Minimal-Eintrag: kategorie=NULL, konfidenz='niedrig',
     pdf_hash gesetzt, vault_pfad auf MD in 00 Inbox/ wenn vorhanden.

Dry-Run per Default. Mit --apply schreiben.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

DB_PATH       = Path("/home/reinhard/docker/docling-workflow/dispatcher-temp/dispatcher.db")
VAULT_ROOT    = Path("/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault")
ANLAGEN_DIR   = VAULT_ROOT / "Anlagen"
INBOX_DIR     = VAULT_ROOT / "00 Inbox"


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="DB schreiben (Default: Dry-Run)")
    ap.add_argument("--scope", choices=("inbox", "all"), default="inbox",
                    help="inbox = nur Orphans mit MD in 00 Inbox/ (Dispatcher-Bug-Opfer, Default). "
                         "all = auch Altbestand ohne MD.")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"DB nicht gefunden: {DB_PATH}", file=sys.stderr)
        return 2
    if not ANLAGEN_DIR.exists():
        print(f"Anlagen-Ordner nicht gefunden: {ANLAGEN_DIR}", file=sys.stderr)
        return 2

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    known_names = {r[0] for r in con.execute("SELECT dateiname FROM dokumente").fetchall()}
    known_hashes = {r[0] for r in con.execute(
        "SELECT pdf_hash FROM dokumente WHERE pdf_hash IS NOT NULL"
    ).fetchall()}

    pdfs = sorted(p for p in ANLAGEN_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    orphans: list[tuple[Path, str, Path | None]] = []
    skipped_hash = 0

    for pdf in pdfs:
        if pdf.name.startswith("._"):
            continue
        if pdf.name in known_names:
            continue
        try:
            pdf_hash = md5_file(pdf)
        except Exception as e:
            print(f"⚠ {pdf.name}: Hash-Fehler: {e}", file=sys.stderr)
            continue
        if pdf_hash in known_hashes:
            skipped_hash += 1
            continue
        # MD-Kandidat in 00 Inbox/ suchen (gleicher Stem)
        md_cand = INBOX_DIR / f"{pdf.stem}.md"
        vault_pfad = None
        if md_cand.exists():
            vault_pfad = str(md_cand.relative_to(VAULT_ROOT))
        orphans.append((pdf, pdf_hash, vault_pfad))

    orphans_with_md    = [o for o in orphans if o[2]]       # Dispatcher-Bug-Opfer
    orphans_without_md = [o for o in orphans if not o[2]]   # Altbestand

    print(f"PDFs in Anlagen/:               {len(pdfs)}")
    print(f"Bereits in DB (Name):           {len(pdfs) - len(orphans) - skipped_hash}")
    print(f"Bereits in DB (Hash-Dup):       {skipped_hash}")
    print(f"Orphans mit MD in 00 Inbox/:    {len(orphans_with_md):>5}   (Dispatcher-Bug-Opfer)")
    print(f"Orphans ohne MD:                {len(orphans_without_md):>5}   (Altbestand aus Imports)")
    print()

    target = orphans_with_md if args.scope == "inbox" else orphans
    print(f"Aktives Scope: --scope={args.scope} → {len(target)} Einträge")
    print()

    for pdf, pdf_hash, vault_pfad in target[:50]:
        vp = f"  → MD: {vault_pfad}" if vault_pfad else "  → (keine MD)"
        print(f"  {pdf.name}\n{vp}")
    if len(target) > 50:
        print(f"  … und {len(target) - 50} weitere")

    if not args.apply:
        print("\n(Dry-Run — nichts geschrieben. Mit --apply ausführen.)")
        return 0

    if not target:
        return 0

    inserted = 0
    for pdf, pdf_hash, vault_pfad in target:
        try:
            con.execute(
                """INSERT INTO dokumente
                   (dateiname, pdf_hash, konfidenz, vault_pfad)
                   VALUES (?, ?, 'niedrig', ?)""",
                (pdf.name, pdf_hash, vault_pfad),
            )
            inserted += 1
        except sqlite3.IntegrityError as e:
            print(f"⚠ {pdf.name}: {e}", file=sys.stderr)
    con.commit()
    print(f"\n✅ {inserted} Inbox-Einträge in DB geschrieben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
