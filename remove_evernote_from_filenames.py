#!/usr/bin/env python3
"""Entfernt 'Evernote'/'evernote' aus .md- und PDF-Dateinamen im Vault.
Rennt: .md Dateien, PDFs in Anlagen/, und aktualisiert Wikilinks + original: Frontmatter.
Usage: python3 remove_evernote_from_filenames.py [--dry-run]
"""
import argparse
import re
import shutil
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
ANLAGEN = VAULT / "Anlagen"


def remove_evernote(name: str) -> str:
    """Entfernt _Evernote oder _evernote aus einem Dateinamen (vor der Extension)."""
    # Match stem: everything before .md or .pdf
    m = re.match(r'^(.+)_[Ee]vernote(_?)(.*)$', name)
    if m:
        prefix = m.group(1)
        suffix = m.group(3)
        return prefix + suffix
    return name


def rename_files(dry_run: bool = True):
    stats = {"md_renamed": 0, "pdf_renamed": 0, "frontmatter_updated": 0,
             "wikilink_updated": 0, "skipped": 0, "errors": 0}
    renames = {}  # old_name → new_name mapping (for cross-reference updates)

    # Phase 1: Rename .md files and PDFs
    all_mds = sorted(VAULT.rglob("*Evernote*.md")) + sorted(VAULT.rglob("*evernote*.md"))
    # Dedup
    seen = set()
    unique_mds = []
    for f in all_mds:
        if f not in seen and f.parent != ANLAGEN:
            seen.add(f)
            unique_mds.append(f)

    print(f"{'[DRY-RUN] ' if dry_run else ''}Phase 1: .md-Dateien umbenennen ({len(unique_mds)} Dateien)")
    for md in unique_mds:
        old_name = md.name
        new_name = remove_evernote(old_name)
        if new_name == old_name:
            stats["skipped"] += 1
            continue

        new_md = md.parent / new_name
        if new_md.exists():
            print(f"  ⚠️ Ziel existiert bereits: {new_name}")
            stats["skipped"] += 1
            continue

        renames[old_name] = new_name
        renames[md.stem] = Path(new_name).stem  # stem mapping for wikilinks

        if not dry_run:
            shutil.move(str(md), str(new_md))
        stats["md_renamed"] += 1

    # Rename PDFs in Anlagen/
    all_pdfs = sorted(ANLAGEN.rglob("*Evernote*.pdf")) + sorted(ANLAGEN.rglob("*evernote*.pdf"))
    unique_pdfs = list(set(all_pdfs))
    print(f"Phase 2: PDFs umbenennen ({len(unique_pdfs)} Dateien)")
    for pdf in unique_pdfs:
        old_name = pdf.name
        new_name = remove_evernote(old_name)
        if new_name == old_name:
            stats["skipped"] += 1
            continue

        new_pdf = pdf.parent / new_name
        if new_pdf.exists():
            continue  # silently skip duplicates

        if not dry_run:
            shutil.move(str(pdf), str(new_pdf))
        stats["pdf_renamed"] += 1

    # Phase 3: Update frontmatter original: and body wikilinks in renamed .md files
    print(f"Phase 3: Frontmatter + Wikilinks aktualisieren")
    for old_name, new_name in renames.items():
        old_stem = Path(old_name).stem
        new_stem = Path(new_name).stem
        old_pdf = old_stem + ".pdf"
        new_pdf_name = new_stem + ".pdf"

        # Find the file: in dry-run mode, use old name; otherwise use new name
        if dry_run:
            md_path = None
            for f in VAULT.rglob(old_name):
                if f.parent != ANLAGEN:
                    md_path = f
                    break
        else:
            md_path = None
            for f in VAULT.rglob(new_name):
                if f.parent != ANLAGEN:
                    md_path = f
                    break

        if not md_path or not md_path.exists():
            stats["errors"] += 1
            continue

        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            stats["errors"] += 1
            continue

        updated = content

        # Ersetze original: Zeile
        if old_pdf in updated:
            updated = updated.replace(f"original: Anlagen/{old_pdf}",
                                       f"original: Anlagen/{new_pdf_name}")
            updated = updated.replace(f'original: "[[Anlagen/{old_pdf}]]"',
                                       f"original: Anlagen/{new_pdf_name}")
            updated = updated.replace(f'original: [[Anlagen/{old_pdf}]]',
                                       f"original: Anlagen/{new_pdf_name}")
            stats["frontmatter_updated"] += 1

        # Update body wikilink
        if f"[[Anlagen/{old_pdf}]]" in updated:
            updated = updated.replace(f"[[Anlagen/{old_pdf}]]",
                                       f"[[Anlagen/{new_pdf_name}]]")
            stats["wikilink_updated"] += 1

        if updated != content and not dry_run:
            md_path.write_text(updated, encoding="utf-8")

    # Phase 4: Update cross-references in OTHER .md files
    print(f"Phase 4: Querverweise in anderen .md-Dateien aktualisieren")
    # Only update wikilinks [[...Evernote...]] that refer to renamed files
    # Since wikilinks use just the filename (no path), we match on the old stem
    all_md_files = [f for f in VAULT.rglob("*.md") if f.parent != ANLAGEN]

    # Build a lookup: old_stem → new_name (for the .md wikilinks)
    stem_map = {}
    for old_name, new_name in renames.items():
        old_stem = Path(old_name).stem
        new_stem = Path(new_name).stem
        stem_map[old_stem] = new_stem

    xref_updated = 0
    for md in all_md_files:
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        updated = content
        for old_stem, new_stem in stem_map.items():
            if f"[[{old_stem}]]" in updated:
                updated = updated.replace(f"[[{old_stem}]]", f"[[{new_stem}]]")
                xref_updated += 1
            if f"[[{old_stem}|" in updated:
                updated = updated.replace(f"[[{old_stem}|", f"[[{new_stem}|")
                xref_updated += 1

        if updated != content and not dry_run:
            md.write_text(updated, encoding="utf-8")

    print(f"\n{'=' * 55}")
    print(f"{'[DRY-RUN] ' if dry_run else ''}Ergebnis:")
    print(f"  .md umbenannt:       {stats['md_renamed']:>6}")
    print(f"  PDF umbenannt:       {stats['pdf_renamed']:>6}")
    print(f"  original: aktualisiert: {stats['frontmatter_updated']:>6}")
    print(f"  Wikilinks aktualisiert: {stats['wikilink_updated']:>6}")
    print(f"  Querverweise:        {xref_updated:>6}")
    print(f"  Skipped:             {stats['skipped']:>6}")
    print(f"  Errors:              {stats['errors']:>6}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rename_files(dry_run=args.dry_run)
