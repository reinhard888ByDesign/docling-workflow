"""
fix_enex_filenames.py — Bereinigt falsche ENEX-Import-Dateinamen im Vault.

Zwei Probleme werden behoben:
1. Doppeltes Datum+Quelle-Präfix:  20230303_Finanzen_20230208_Titel → 20230208_Titel
   (Tritt auf wenn Notiz-Titel schon mit YYYYMMDD beginnt)
2. Abgeschnittene Titel (max 60 Zeichen):  20191119_..._Untersuchung → vollständiger Titel

Basis: Frontmatter-Feld `title` (Original-Evernote-Titel, nie abgeschnitten).

Aufruf:
    python fix_enex_filenames.py [--dry-run] [--vault /pfad] [--db /pfad]
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

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_ = re.compile(r'_+')


def sanitize(text: str, max_len: int = 120) -> str:
    text = text.strip()
    text = _UNSAFE.sub("_", text)
    text = text.replace(" ", "_")
    text = _MULTI_.sub("_", text)
    return text.strip("_")[:max_len]


def read_frontmatter(md_path: Path) -> dict:
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    m = re.match(r'^---\r?\n(.*?)\r?\n---', content, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def get_referenced_pdf(md_path: Path) -> str | None:
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(r'\[\[Anlagen/([^\]]+\.pdf)\]\]', content)
    return m.group(1) if m else None


def update_frontmatter_original(md_path: Path, old_pdf: str, new_pdf: str) -> None:
    content = md_path.read_text(encoding="utf-8")
    updated = content.replace(f"[[Anlagen/{old_pdf}]]", f"[[Anlagen/{new_pdf}]]")
    if updated != content:
        md_path.write_text(updated, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vault", default=str(VAULT_PATH))
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    vault   = Path(args.vault)
    anlagen = vault / "Anlagen"
    db_path = Path(args.db)
    dry_run = args.dry_run

    if dry_run:
        print("=== DRY-RUN — keine Änderungen werden vorgenommen ===\n")

    conn = sqlite3.connect(str(db_path)) if db_path.exists() else None

    renamed = skipped = conflicts = 0

    for md_path in sorted(vault.rglob("*.md")):
        fm = read_frontmatter(md_path)
        if fm.get("import_quelle") != "enex":
            continue

        raw_title = fm.get("title", "")
        if isinstance(raw_title, (int, float)):
            raw_title = str(raw_title)
        raw_title = raw_title.strip().strip('"')
        if not raw_title:
            continue

        # Only fix notes whose title starts with YYYYMMDD
        if not re.match(r'^\d{8}', raw_title):
            continue

        correct_stem = sanitize(raw_title, max_len=120)
        current_stem = md_path.stem

        if correct_stem == current_stem:
            continue  # already correct

        new_md_path = md_path.parent / f"{correct_stem}.md"

        # Conflict check
        if new_md_path.exists():
            print(f"[CONFLICT] Ziel existiert bereits — übersprungen:")
            print(f"  ALT: {md_path.relative_to(vault)}")
            print(f"  NEU: {new_md_path.relative_to(vault)}")
            conflicts += 1
            continue

        old_pdf_name = get_referenced_pdf(md_path)
        new_pdf_name = f"{correct_stem}.pdf" if old_pdf_name else None
        old_pdf_path = (anlagen / old_pdf_name) if old_pdf_name else None
        new_pdf_path = (anlagen / new_pdf_name) if new_pdf_name else None

        print(f"[REN] {md_path.relative_to(vault)}")
        print(f"   →  {new_md_path.relative_to(vault)}")
        if old_pdf_name:
            print(f"  PDF: {old_pdf_name}  →  {new_pdf_name}")

        if not dry_run:
            # 1. PDF umbenennen und frontmatter aktualisieren
            if old_pdf_path and old_pdf_path.exists() and new_pdf_path:
                if not new_pdf_path.exists():
                    old_pdf_path.rename(new_pdf_path)
                update_frontmatter_original(md_path, old_pdf_name, new_pdf_name)

            # 2. MD umbenennen
            md_path.rename(new_md_path)

            # 3. DB aktualisieren
            if conn:
                old_rel = str(md_path.relative_to(vault))
                new_rel = str(new_md_path.relative_to(vault))
                conn.execute(
                    "UPDATE dokumente SET vault_pfad = ?, dateiname = ? "
                    "WHERE vault_pfad = ? OR dateiname = ?",
                    (new_rel, f"{correct_stem}.md", old_rel, md_path.name),
                )
                conn.commit()

        renamed += 1

    if conn:
        conn.close()

    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}Fertig: {renamed} umbenannt, {skipped} übersprungen, {conflicts} Konflikte")


if __name__ == "__main__":
    main()
