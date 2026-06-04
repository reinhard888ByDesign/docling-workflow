#!/usr/bin/env python3
"""
Fix: Stellt sicher, dass JEDE .md-Datei im Vault einen klickbaren PDF-Wikilink
im Body hat ([[Anlagen/datei.pdf]]).

Liest das original:-Feld aus dem YAML-Frontmatter und erzeugt daraus einen
Body-Wikilink, falls keiner existiert.

Obsidian-Regel: Nur Wikilinks IM BODY (nach ---) sind klickbar.
YAML-Frontmatter-Werte werden NIE als Links gerendert.

Usage:
  python3 fix_body_wikilinks.py --dry-run      # Nur Analyse
  python3 fix_body_wikilinks.py                # Fix ausführen
  python3 fix_body_wikilinks.py --limit 100    # Nur 100 Dateien
"""

import argparse
import re
import sys
from pathlib import Path

# ── Konfiguration ─────────────────────────────────────────────────────────────

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")

# Regex für YAML-Frontmatter (erste Zeile ---, endet mit ---)
FM_RE = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL)
# Body-Wikilink [[...pdf]]
WL_RE = re.compile(r'\[\[([^\]]+\.pdf)\]\]')
# original: Feld im Frontmatter
ORIG_RE = re.compile(r'^original:\s*(.+)$', re.MULTILINE)


def extract_pdf_filename(fm_text: str) -> str | None:
    """Extrahiert den PDF-Dateinamen aus dem original:-Feld des Frontmatters."""
    m = ORIG_RE.search(fm_text)
    if not m:
        return None
    raw = m.group(1).strip().strip('"').strip("'")
    if not raw:
        return None
    # Format: "[[Anlagen/x.pdf]]" → x.pdf
    wl = re.match(r'\[\[([^\]]+\.pdf)\]\]', raw)
    if wl:
        return Path(wl.group(1).strip()).name
    # Format: "Anlagen/x.pdf" oder "x.pdf"
    return Path(raw.strip()).name


def has_body_wikilink(body: str) -> bool:
    """Prüft ob der Body bereits einen [[...pdf]] Wikilink enthält."""
    return bool(WL_RE.search(body))


def process_file(md_path: Path, dry_run: bool = False) -> str:
    """
    Verarbeitet eine .md-Datei. Returns: 'fixed', 'skipped_ok', 'skipped_no_original',
    'skipped_binary', 'error'
    """
    try:
        content = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  ERROR lesen: {e}")
        return "error"

    # YAML-Frontmatter + Body trennen
    fm_match = FM_RE.match(content)
    if not fm_match:
        return "skipped_no_frontmatter"

    fm_text = fm_match.group(1)
    body = content[fm_match.end():]

    # Prüfen ob bereits ein Body-Wikilink AM ANFANG existiert (richtige Position)
    # Der Wikilink muss auf einer eigenen Zeile nach dem Frontmatter-Newline stehen
    if re.search(r'\n📄\s*\[\[', body) or re.search(r'\n📎\s*\[\[', body):
        return "skipped_ok"

    # Auch als erste Body-Zeile (body.lstrip hilft bei --📄 ohne \n dazwischen)
    body_stripped = body.lstrip()
    if body_stripped.startswith("📄 [[") or body_stripped.startswith("📎 [["):
        # Steht ganz am Anfang, aber ohne \n davor → Fix nötig (---📄 klebt)
        pass

    # PDF-Dateiname aus original: extrahieren
    pdf_name = extract_pdf_filename(fm_text)
    if not pdf_name or not pdf_name.endswith('.pdf'):
        return "skipped_no_original"

    # Wikilink-Zeile bauen (kommt direkt nach Frontmatter, vor Body-Inhalt)
    wikilink_block = f"📄 [[Anlagen/{pdf_name}]]\n\n"

    # Entferne eventuell vorhandenen Wikilink am Ende (vom vorherigen Fix-Lauf)
    body = WL_RE.sub("", body)
    # Entferne leere 📄/📎 Zeilen ohne Wikilink
    body = re.sub(r'\n*📄\s*\n*', '\n', body)
    body = re.sub(r'\n*📎\s*\n*', '\n', body)
    # Cleanup: max 1 Leerzeile am Body-Anfang
    body = re.sub(r'^\n{2,}', '\n', body)

    # Wikilink am Body-Anfang einfügen
    new_body = wikilink_block + body.lstrip('\n')

    new_content = content[:fm_match.end()] + "\n" + new_body

    if dry_run:
        return "fixed"

    try:
        md_path.write_text(new_content, encoding="utf-8")
        return "fixed"
    except Exception as e:
        print(f"  ERROR schreiben: {e}")
        return "error"


def main():
    parser = argparse.ArgumentParser(description="Fix fehlende PDF-Body-Wikilinks im Vault")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur Analyse, keine Änderungen")
    parser.add_argument("--limit", type=int, default=0,
                        help="Nur N Dateien verarbeiten (0=alle)")
    args = parser.parse_args()

    # Alle .md-Dateien im Vault sammeln
    all_mds = sorted(VAULT.rglob("*.md"))
    if args.limit:
        all_mds = all_mds[:args.limit]

    stats = {"total": len(all_mds), "fixed": 0, "skipped_ok": 0,
             "skipped_no_original": 0, "skipped_no_frontmatter": 0, "error": 0}

    label = "[DRY-RUN] " if args.dry_run else ""
    print(f"{label}Verarbeite {stats['total']} .md-Dateien im Vault...\n")

    for i, md in enumerate(all_mds, 1):
        result = process_file(md, dry_run=args.dry_run)
        stats[result] = stats.get(result, 0) + 1

        if i % 500 == 0 or result == "error":
            print(f"[{i}/{stats['total']}] {result}: {md.relative_to(VAULT)}")
            sys.stdout.flush()

    print(f"\n{'=' * 55}")
    print(f"{label}Ergebnis:")
    print(f"  Gesamt:               {stats['total']:>6}")
    print(f"  Gefixt:               {stats['fixed']:>6}  ✨")
    print(f"  OK (hat Wikilink):    {stats['skipped_ok']:>6}")
    print(f"  Kein original: Feld:  {stats['skipped_no_original']:>6}")
    print(f"  Kein Frontmatter:     {stats.get('skipped_no_frontmatter', 0):>6}")
    print(f"  Fehler:               {stats['error']:>6}")


if __name__ == "__main__":
    main()
