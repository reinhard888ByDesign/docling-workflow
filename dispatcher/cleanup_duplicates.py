"""
cleanup_duplicates.py — Entfernt _1/_2/_3 Duplikate aus ENEX-Importen.

Für jede *_N.md Datei:
  - Base-Datei existiert → _N.md + verknüpftes _N.pdf löschen, DB-Eintrag entfernen
  - Base nicht vorhanden → _N.md → base.md umbenennen, PDF umbenennen, DB aktualisieren

Aufruf:
    python cleanup_duplicates.py [--dry-run] [--vault /pfad/zum/vault]
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

import yaml

VAULT_PATH = Path(os.getenv("VAULT_PATH", "/data/reinhards-vault"))
ANLAGEN    = VAULT_PATH / "Anlagen"
DB_PATH    = Path(os.getenv("DB_PATH", "/config/dispatcher.db"))

SUFFIX_RE = re.compile(r'^(.+)_([1-9]\d?)(\.md)$')


def find_duplicate_mds(vault: Path) -> list[Path]:
    results = []
    for p in vault.rglob("*.md"):
        if SUFFIX_RE.match(p.name):
            results.append(p)
    return sorted(results)


def get_referenced_pdf(md_path: Path) -> str | None:
    """Liest 'original' Frontmatter-Feld und extrahiert den PDF-Dateinamen."""
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
    if not original:
        return None
    # "[[Anlagen/foo.pdf]]" → "foo.pdf"
    pdf_m = re.search(r'\[\[Anlagen/([^\]]+\.pdf)\]\]', str(original))
    return pdf_m.group(1) if pdf_m else None


def update_original_in_md(md_path: Path, old_pdf: str, new_pdf: str, dry_run: bool) -> bool:
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return False
    new_content = content.replace(
        f"[[Anlagen/{old_pdf}]]",
        f"[[Anlagen/{new_pdf}]]",
    )
    if new_content == content:
        return True
    if not dry_run:
        md_path.write_text(new_content, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Nichts verändern, nur anzeigen")
    parser.add_argument("--vault", default=str(VAULT_PATH), help="Vault-Pfad")
    args = parser.parse_args()

    vault = Path(args.vault)
    anlagen = vault / "Anlagen"
    dry_run = args.dry_run

    if dry_run:
        print("=== DRY-RUN — keine Änderungen ===\n")

    conn = sqlite3.connect(str(DB_PATH)) if DB_PATH.exists() else None

    dups = find_duplicate_mds(vault)
    print(f"Gefundene _N.md Dateien: {len(dups)}")

    deleted_md  = 0
    deleted_pdf = 0
    renamed_md  = 0
    renamed_pdf = 0
    skipped     = 0

    for dup_path in dups:
        m = SUFFIX_RE.match(dup_path.name)
        if not m:
            continue
        base_stem, _n, ext = m.group(1), m.group(2), m.group(3)
        base_path = dup_path.parent / f"{base_stem}{ext}"

        dup_pdf_name  = get_referenced_pdf(dup_path)
        dup_pdf_path  = (anlagen / dup_pdf_name) if dup_pdf_name else None

        # ------------------------------------------------------------------ #
        # Fall A: Base-Datei existiert → Duplikat löschen
        # ------------------------------------------------------------------ #
        if base_path.exists():
            print(f"[DEL]  {dup_path.relative_to(vault)}")

            # _N.md löschen
            if not dry_run:
                dup_path.unlink(missing_ok=True)
            deleted_md += 1

            # _N.pdf löschen wenn vorhanden
            if dup_pdf_path and dup_pdf_path.exists():
                print(f"       PDF: {dup_pdf_name}")
                if not dry_run:
                    dup_pdf_path.unlink(missing_ok=True)
                deleted_pdf += 1

            # DB-Eintrag entfernen
            if conn:
                rel = str(dup_path.relative_to(vault))
                if not dry_run:
                    conn.execute("DELETE FROM dokumente WHERE vault_pfad = ? OR dateiname = ?",
                                 (rel, dup_path.name))
                    conn.commit()

        # ------------------------------------------------------------------ #
        # Fall B: Base-Datei fehlt → umbenennen
        # ------------------------------------------------------------------ #
        else:
            base_pdf_name = f"{base_stem}.pdf" if dup_pdf_name else None
            base_pdf_path = (anlagen / base_pdf_name) if base_pdf_name else None

            print(f"[REN]  {dup_path.relative_to(vault)}  →  {base_path.name}")

            # Sicherheitscheck: base_pdf auch noch nicht vorhanden?
            if base_pdf_path and base_pdf_path.exists():
                print(f"       ⚠ Base-PDF bereits vorhanden — überspringe")
                skipped += 1
                continue

            if not dry_run:
                # PDF umbenennen
                if dup_pdf_path and dup_pdf_path.exists() and base_pdf_path:
                    dup_pdf_path.rename(base_pdf_path)
                # original-Feld in MD aktualisieren
                if dup_pdf_name and base_pdf_name:
                    update_original_in_md(dup_path, dup_pdf_name, base_pdf_name, dry_run=False)
                # MD umbenennen
                dup_path.rename(base_path)
                # DB aktualisieren
                if conn:
                    rel_old = str(dup_path.relative_to(vault))
                    rel_new = str(base_path.relative_to(vault))
                    conn.execute(
                        "UPDATE dokumente SET vault_pfad = ?, dateiname = ? WHERE vault_pfad = ?",
                        (rel_new, base_path.name, rel_old),
                    )
                    conn.commit()

            renamed_md += 1
            if dup_pdf_path:
                renamed_pdf += 1

    if conn:
        conn.close()

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Fertig:")
    print(f"  Gelöscht:   {deleted_md} MD,  {deleted_pdf} PDF")
    print(f"  Umbenannt:  {renamed_md} MD,  {renamed_pdf} PDF")
    print(f"  Übersprungen: {skipped}")


if __name__ == "__main__":
    main()
