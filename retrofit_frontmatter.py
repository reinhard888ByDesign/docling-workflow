#!/usr/bin/env python3
"""Retrofit-Skript: Frontmatter-original: in Vault-Ordner korrigieren.

Was das Skript tut:
  1. Alle .md-Dateien im Ziel-Ordner scannen
  2. PDF-Dateiname aus bestehendem original:-Feld ableiten (verschiedene Formate)
  3. PDF von /home/reinhard/pdf-archiv/ nach Anlagen/ kopieren (falls noch nicht dort)
  4. original: auf [[Anlagen/filename.pdf]] korrigieren
  5. Falls kein original:-Feld: hinzufügen
  6. Falls kein Frontmatter: minimalen Block anlegen

Aufruf:
  python3 retrofit_frontmatter.py [--folder "49 Krankenversicherung"] [--dry-run]
"""

import argparse
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote

VAULT_ROOT  = Path("/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault")
ANLAGEN_DIR = VAULT_ROOT / "Anlagen"
PDF_ARCHIV  = Path("/home/reinhard/pdf-archiv")

# Muster, die den PDF-Dateinamen aus einem original:-Wert extrahieren
_PDF_PATTERNS = [
    # [[Anlagen/name.pdf]] oder [[Originale/name.pdf]]
    re.compile(r'\[\[(?:[^\]/]+/)?([^\]]+\.pdf)\]\]', re.IGNORECASE),
    # file:///Volumes/.../name.pdf  oder  /Volumes/.../name.pdf
    re.compile(r'(?:file://)?/[^\s"\']+/([^/\s"\']+\.pdf)', re.IGNORECASE),
    # [name.pdf](file://...) — Markdown-Link
    re.compile(r'\[([^\]]+\.pdf)\]\(', re.IGNORECASE),
    # nackte Dateiname.pdf ohne Pfad
    re.compile(r'"([^"]+\.pdf)"', re.IGNORECASE),
]


def extract_pdf_name(original_value: str) -> str | None:
    """Versucht, nur den Dateinamen (ohne Pfad) aus dem original:-Wert zu extrahieren."""
    for pat in _PDF_PATTERNS:
        m = pat.search(original_value)
        if m:
            name = m.group(1).strip()
            return unquote(name)  # %20 → Leerzeichen etc.
    return None


def guess_pdf_name_from_md(md_path: Path) -> str:
    """Leitet den PDF-Namen aus dem MD-Dateinamen ab (gleicher Stem, .pdf)."""
    return md_path.stem + ".pdf"


def find_pdf(pdf_name: str) -> Path | None:
    """Sucht das PDF: erst in Anlagen/, dann in pdf-archiv/."""
    in_anlagen = ANLAGEN_DIR / pdf_name
    if in_anlagen.exists():
        return in_anlagen
    in_archiv = PDF_ARCHIV / pdf_name
    if in_archiv.exists():
        return in_archiv
    return None


def ensure_pdf_in_anlagen(pdf_name: str, dry_run: bool) -> bool:
    """Kopiert das PDF nach Anlagen/, falls es noch nicht dort ist. Gibt True zurück wenn verfügbar."""
    dest = ANLAGEN_DIR / pdf_name
    if dest.exists():
        return True
    src = PDF_ARCHIV / pdf_name
    if not src.exists():
        return False
    if not dry_run:
        ANLAGEN_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
    return True


# ── Frontmatter-Parsing ────────────────────────────────────────────────────────

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Gibt (frontmatter_lines_as_dict, body) zurück. dict-Werte sind die Rohzeilen (str)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text

    fm_raw = m.group(1)
    body   = text[m.end():]

    # Einfaches Zeilen-basiertes Parsen (nur Top-Level-Schlüssel für original: relevant)
    lines: dict[str, str] = {}
    current_key = None
    block_lines: list[str] = []

    for line in fm_raw.splitlines():
        key_match = re.match(r'^(\w+):\s*(.*)', line)
        if key_match:
            if current_key and block_lines:
                lines[current_key] = "\n".join(block_lines).strip()
            current_key = key_match.group(1)
            block_lines = [line]  # gesamte Zeile inkl. Wert behalten
        elif current_key:
            block_lines.append(line)

    if current_key and block_lines:
        lines[current_key] = "\n".join(block_lines).strip()

    return lines, body


def rebuild_frontmatter_with_original(fm_raw: str, pdf_name: str, inserting: bool) -> str:
    """Setzt/ersetzt original: in einem bestehenden Frontmatter-Block (als String)."""
    new_line = f'original: "[[Anlagen/{pdf_name}]]"'

    if inserting:
        # Einfach am Ende vor dem letzten --- einfügen
        return fm_raw.rstrip() + "\n" + new_line + "\n"

    # Ersetzen — alle Varianten von original: (mehrzeilig, Markdown-Link, etc.)
    # Wir ersetzen die gesamte Zeile, die mit "original:" beginnt
    lines = fm_raw.splitlines()
    result = []
    skip_continuation = False
    replaced = False
    for line in lines:
        if re.match(r'^original:', line):
            result.append(new_line)
            replaced = True
            skip_continuation = False
            continue
        if skip_continuation:
            continue
        result.append(line)

    if not replaced:
        result.append(new_line)
    return "\n".join(result)


def process_md(md_path: Path, dry_run: bool, stats: dict) -> None:
    text = md_path.read_text(encoding="utf-8")
    has_fm = bool(_FM_RE.match(text))
    fm_keys, body = split_frontmatter(text)

    original_line = fm_keys.get("original", "")
    pdf_name: str | None = None
    needs_original_insert = False
    needs_original_update = False

    if original_line:
        pdf_name = extract_pdf_name(original_line)
        if pdf_name:
            # Prüfen ob bereits korrekt
            if f"[[Anlagen/{pdf_name}]]" in original_line:
                # Schon korrekt — PDF ggf. trotzdem kopieren
                if ensure_pdf_in_anlagen(pdf_name, dry_run):
                    stats["already_ok"] += 1
                else:
                    stats["pdf_not_found"] += 1
                    print(f"  ⚠️  PDF nicht gefunden: {pdf_name}  ← {md_path.name}")
                return
            needs_original_update = True
        else:
            # original: vorhanden aber kein PDF-Name extrahierbar
            pdf_name = guess_pdf_name_from_md(md_path)
            needs_original_update = True
            print(f"  ⚠️  original: nicht parsebar → leite PDF-Name ab: {pdf_name}  ({md_path.name})")
    elif has_fm:
        # Frontmatter vorhanden, aber kein original: Feld
        pdf_name = guess_pdf_name_from_md(md_path)
        needs_original_insert = True
    else:
        # Kein Frontmatter
        pdf_name = guess_pdf_name_from_md(md_path)
        needs_original_insert = True  # wird zusammen mit FM angelegt

    # PDF sichern
    pdf_available = ensure_pdf_in_anlagen(pdf_name, dry_run)
    if not pdf_available:
        stats["pdf_not_found"] += 1
        print(f"  ❌  PDF nicht gefunden: {pdf_name}  ← {md_path.name}")

    if dry_run:
        action = "update" if needs_original_update else "insert"
        if not has_fm:
            action = "add-fm"
        stats["would_fix"] += 1
        print(f"  → [{action}] {md_path.name}  →  [[Anlagen/{pdf_name}]]")
        return

    # ── Datei schreiben ────────────────────────────────────────────────────────
    new_original_line = f'original: "[[Anlagen/{pdf_name}]]"'

    if not has_fm:
        # Minimalen Frontmatter-Block anlegen
        from datetime import date
        fm_block = f"---\n{new_original_line}\nerstellt: {date.today().isoformat()}\n---\n\n"
        md_path.write_text(fm_block + text, encoding="utf-8")
        stats["added_fm"] += 1
    elif needs_original_update or needs_original_insert:
        m = _FM_RE.match(text)
        fm_raw = m.group(1)
        body_start = m.end()
        new_fm_raw = rebuild_frontmatter_with_original(fm_raw, pdf_name, inserting=needs_original_insert)
        new_text = f"---\n{new_fm_raw}\n---\n{text[body_start:]}"
        md_path.write_text(new_text, encoding="utf-8")
        stats["fixed"] += 1


def main():
    parser = argparse.ArgumentParser(description="Retrofit original: Frontmatter in Vault-Ordner")
    parser.add_argument("--folder", default="49 Krankenversicherung",
                        help="Ordner relativ zu VAULT_ROOT")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur anzeigen, nichts schreiben")
    args = parser.parse_args()

    target = VAULT_ROOT / args.folder
    if not target.exists():
        print(f"Ordner nicht gefunden: {target}", file=sys.stderr)
        sys.exit(1)

    mds = sorted(target.rglob("*.md"))
    print(f"Scanne {len(mds)} Dateien in '{args.folder}' …")
    if args.dry_run:
        print("  (DRY-RUN — keine Änderungen)")

    stats = {"already_ok": 0, "fixed": 0, "added_fm": 0, "would_fix": 0, "pdf_not_found": 0}

    for md in mds:
        if md.name.startswith("._"):
            continue  # macOS Resource-Fork überspringen
        try:
            process_md(md, args.dry_run, stats)
        except Exception as e:
            print(f"  ❌  Fehler bei {md.name}: {e}")
            stats["pdf_not_found"] += 1

    print()
    print("─" * 60)
    if args.dry_run:
        print(f"  Würde korrigiert:  {stats['would_fix']}")
    else:
        print(f"  Korrigiert:        {stats['fixed']}")
        print(f"  Frontmatter neu:   {stats['added_fm']}")
    print(f"  Bereits korrekt:   {stats['already_ok']}")
    print(f"  PDF nicht gefunden:{stats['pdf_not_found']}")
    print("─" * 60)


if __name__ == "__main__":
    main()
