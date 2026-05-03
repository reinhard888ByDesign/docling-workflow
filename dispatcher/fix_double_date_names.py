"""
fix_double_date_names.py — Bereinigt YYYYMMDD_Quelle_YYYYMMDD_Titel Dateinamen.

Erkennt MD-Dateien die mit YYYYMMDD_SOURCE_YYYYMMDD beginnen und benennt
sie nach YYYYMMDD_RestTitel um. Das zugehörige PDF in Anlagen/ wird
ebenfalls umbenannt und das 'original'-Frontmatter-Feld aktualisiert.

Aufruf:
    python fix_double_date_names.py [--dry-run] [--vault /pfad]
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

import yaml

VAULT_PATH = Path(os.getenv("VAULT_PATH", "/data/reinhards-vault"))
ANLAGEN    = VAULT_PATH / "Anlagen"
DB_PATH    = Path(os.getenv("DB_PATH", "/config/dispatcher.db"))

SOURCES = "Persönlich|Familie|KV|Immobilien|ImmV|Finanzen|KFZ|Reisen|Business|Digitales|Evernote|Italien|FengShui|Archiv"
DOUBLE_DATE_RE = re.compile(
    rf'^(\d{{8}})_(?:{SOURCES})_(\d{{8}}.*?)$'
)


def get_referenced_pdf(md_path: Path) -> str | None:
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.match(r'^---\r?\n(.*?)\r?\n---', content, re.DOTALL)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None
    original = fm.get("original", "")
    pdf_m = re.search(r'\[\[Anlagen/([^\]]+\.pdf)\]\]', str(original))
    return pdf_m.group(1) if pdf_m else None


def update_original_field(md_path: Path, old_pdf: str, new_pdf: str) -> None:
    content = md_path.read_text(encoding="utf-8")
    updated = content.replace(f"[[Anlagen/{old_pdf}]]", f"[[Anlagen/{new_pdf}]]")
    if updated != content:
        md_path.write_text(updated, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vault", default=str(VAULT_PATH))
    args = parser.parse_args()

    vault = Path(args.vault)
    anlagen = vault / "Anlagen"
    dry_run = args.dry_run

    if dry_run:
        print("=== DRY-RUN ===\n")

    conn = sqlite3.connect(str(DB_PATH)) if DB_PATH.exists() else None

    renamed = 0
    skipped = 0

    for md_path in sorted(vault.rglob("*.md")):
        m = DOUBLE_DATE_RE.match(md_path.stem)
        if not m:
            continue

        new_stem = m.group(2)        # YYYYMMDD_RestTitel (zweites Datum + Rest)
        new_md_name = f"{new_stem}.md"
        new_md_path = md_path.parent / new_md_name

        if new_md_path == md_path:
            continue  # nichts zu tun

        if new_md_path.exists():
            print(f"[SKIP] Ziel existiert bereits: {new_md_path.relative_to(vault)}")
            skipped += 1
            continue

        # PDF-Handling
        old_pdf_name = get_referenced_pdf(md_path)
        new_pdf_name = f"{new_stem}.pdf" if old_pdf_name else None
        old_pdf_path = (anlagen / old_pdf_name) if old_pdf_name else None
        new_pdf_path = (anlagen / new_pdf_name) if new_pdf_name else None

        print(f"[REN] {md_path.relative_to(vault)}")
        print(f"   →  {new_md_path.relative_to(vault)}")
        if old_pdf_name:
            print(f"  PDF: {old_pdf_name}  →  {new_pdf_name}")

        if not dry_run:
            # 1. PDF umbenennen (vor MD, da MD danach original-Feld aktualisiert)
            if old_pdf_path and old_pdf_path.exists() and new_pdf_path:
                if not new_pdf_path.exists():
                    old_pdf_path.rename(new_pdf_path)
                # frontmatter original-Feld aktualisieren
                update_original_field(md_path, old_pdf_name, new_pdf_name)

            # 2. MD umbenennen
            md_path.rename(new_md_path)

            # 3. DB aktualisieren
            if conn:
                old_rel = str(md_path.relative_to(vault))
                new_rel = str(new_md_path.relative_to(vault))
                conn.execute(
                    "UPDATE dokumente SET vault_pfad = ?, dateiname = ? WHERE vault_pfad = ? OR dateiname = ?",
                    (new_rel, new_md_name, old_rel, md_path.name),
                )
                conn.commit()

        renamed += 1

    if conn:
        conn.close()

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Fertig: {renamed} umbenannt, {skipped} übersprungen")


if __name__ == "__main__":
    main()
