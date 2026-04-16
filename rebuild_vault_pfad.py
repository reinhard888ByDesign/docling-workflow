#!/usr/bin/env python3
"""Phase 7: DB vault_pfad rebuild.

Scannt alle .md-Dateien im Vault, liest das original:-Feld (PDF-Name),
und aktualisiert vault_pfad in der dokumente-Tabelle für jeden Treffer.

Aufruf:
  python3 rebuild_vault_pfad.py [--dry-run]
"""

import argparse
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote
from collections import defaultdict

VAULT_ROOT = Path("/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault")
DB_FILE    = Path("/home/reinhard/docker/docling-workflow/dispatcher-temp/dispatcher.db")

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

_PDF_PATS = [
    re.compile(r'\[\[(?:[^\]/]+/)?([^\]]+\.pdf)\]\]', re.IGNORECASE),
    re.compile(r'(?:file://)?/[^\s"\']+/([^/\s"\']+\.pdf)', re.IGNORECASE),
    re.compile(r'\[([^\]]+\.pdf)\]\(', re.IGNORECASE),
    re.compile(r'"([^"]+\.pdf)"', re.IGNORECASE),
]

def extract_pdf_name(val: str) -> str | None:
    for pat in _PDF_PATS:
        m = pat.search(val)
        if m:
            return unquote(m.group(1).strip())
    return None

def read_fm_field(fm: str, key: str) -> str:
    m = re.search(rf'^{re.escape(key)}:\s*(.+)', fm, re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def scan_vault() -> dict[str, list[Path]]:
    """Gibt ein dict PDF_name(lower) → [md_paths] zurück."""
    pdf_to_mds: dict[str, list[Path]] = defaultdict(list)
    for md in VAULT_ROOT.rglob("*.md"):
        if md.name.startswith("._"):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = _FM_RE.match(text)
        if not m:
            continue
        orig = read_fm_field(m.group(1), "original")
        if not orig:
            continue
        pdf = extract_pdf_name(orig)
        if pdf:
            pdf_to_mds[pdf.lower()].append(md)
    return pdf_to_mds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dry = args.dry_run

    if dry:
        print("DRY-RUN — keine DB-Änderungen")

    print(f"Scanne Vault: {VAULT_ROOT}")
    pdf_to_mds = scan_vault()
    print(f"  {sum(len(v) for v in pdf_to_mds.values())} MD-Dateien mit original: gefunden")
    print(f"  {len(pdf_to_mds)} eindeutige PDF-Referenzen")

    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row

    rows = con.execute("SELECT id, dateiname, vault_pfad FROM dokumente").fetchall()
    print(f"\n{len(rows)} Einträge in DB")

    stats = {
        "updated":        0,
        "already_ok":     0,
        "no_md_found":    0,
        "multi_md":       0,
    }

    updates: list[tuple[str, int]] = []

    for row in rows:
        dateiname: str = row["dateiname"]
        old_pfad:  str = row["vault_pfad"] or ""
        dok_id:    int = row["id"]

        mds = pdf_to_mds.get(dateiname.lower(), [])

        if not mds:
            stats["no_md_found"] += 1
            continue

        # Bei mehreren MD-Kandidaten: bevorzuge die mit [[Anlagen/...]]-Referenz
        if len(mds) > 1:
            anlagen = [p for p in mds if "Anlagen" in p.read_text(encoding="utf-8", errors="replace")]
            mds = anlagen if anlagen else mds
            stats["multi_md"] += 1

        md_path = mds[0]
        new_pfad = str(md_path.relative_to(VAULT_ROOT))

        if new_pfad == old_pfad:
            stats["already_ok"] += 1
            continue

        updates.append((new_pfad, dok_id))
        stats["updated"] += 1

        if dry:
            print(f"  {dateiname[:50]}")
            print(f"    alt: {old_pfad or '(leer)'}")
            print(f"    neu: {new_pfad}")

    if not dry and updates:
        with con:
            con.executemany("UPDATE dokumente SET vault_pfad = ? WHERE id = ?", updates)
        print(f"\n{len(updates)} vault_pfad-Einträge aktualisiert.")

    con.close()

    print()
    print("─" * 60)
    print(f"  Aktualisiert:      {stats['updated']}")
    print(f"  Bereits korrekt:   {stats['already_ok']}")
    print(f"  Kein MD gefunden:  {stats['no_md_found']}")
    print(f"  Mehrere MDs:       {stats['multi_md']}")
    print("─" * 60)

    if stats["no_md_found"] > 0:
        print(f"\n  {stats['no_md_found']} PDFs in DB haben kein MD im Vault —")
        print("  diese wurden möglicherweise nie verarbeitet oder das MD fehlt.")


if __name__ == "__main__":
    main()
