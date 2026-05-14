#!/usr/bin/env python3
"""Phase-2-Fixes für den Vault.
5. Ordnerbasierte kategorie: für 2438 unkategorisierte MDs befüllen
6. Inbox-Jahresordner (2003–2019) → 99 Archiv zusammenführen
"""
import re
import shutil
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")

# ── Mapping Ordner → Kategorie ─────────────────────────────────────────────────

FOLDER_TO_KATEGORIE = {
    '00 Inbox':                 'inbox',
    '10 Persönlich':            'persoenlich',
    '20 Familie':               'familie',
    '30 FengShui':              'fengshui',
    '40 Finanzen':              'finanzen',
    '49 Krankenversicherung':   'krankenversicherung',
    '50 Immobilien eigen':      'immobilien_eigen',
    '51 Immobilien vermietet':  'immobilien_vermietet',
    '55 Garten':                'garten',
    '60 Fahrzeuge':             'fahrzeuge',
    '70 Italien':               'italien',
    '80 Business':              'business',
    '82 Digitales':             'digitales',
    '85 Wissen':                'wissen',
    '90 Reisen':                'reisen',
    '95 Bedienungsanleitungen': 'bedienungsanleitungen',
    '99 Archiv':                'archiv',
}

FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

def has_kategorie(text: str) -> bool:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False
    return bool(re.search(r'^kategorie:', m.group(1), re.M))

def insert_kategorie(text: str, kategorie: str) -> str:
    """Insert kategorie: after the first --- block's last field."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter: prepend one
        return f'---\nkategorie: {kategorie}\n---\n\n{text}'

    fm_body = m.group(1)
    new_fm = fm_body.rstrip() + f'\nkategorie: {kategorie}'
    return f'---\n{new_fm}\n---\n' + text[m.end():]

def get_top_folder(md: Path) -> str | None:
    try:
        rel = md.relative_to(VAULT)
        return rel.parts[0]
    except Exception:
        return None

# ── 5. Kategorie aus Ordner befüllen ──────────────────────────────────────────

def fill_kategorie_from_folder():
    added = 0
    skipped_no_fm = 0
    errors = 0

    for md in VAULT.rglob('*.md'):
        top = get_top_folder(md)
        if top is None or top not in FOLDER_TO_KATEGORIE:
            continue

        try:
            text = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f'[ERR] lesen {md.name}: {e}')
            errors += 1
            continue

        if has_kategorie(text):
            continue

        kategorie = FOLDER_TO_KATEGORIE[top]
        new_text = insert_kategorie(text, kategorie)

        try:
            md.write_text(new_text, encoding='utf-8')
            added += 1
        except Exception as e:
            print(f'[ERR] schreiben {md.name}: {e}')
            errors += 1

    print(f'kategorie: hinzugefügt:   {added}')
    print(f'Fehler:                   {errors}')
    return added

# ── 6. Inbox-Jahresordner → 99 Archiv ─────────────────────────────────────────

MOVE_YEARS = [str(y) for y in range(2003, 2020)]  # 2003–2019

def merge_inbox_to_archiv():
    inbox = VAULT / '00 Inbox'
    archiv = VAULT / '99 Archiv'
    moved = 0
    conflicts = 0

    for year in MOVE_YEARS:
        src_dir = inbox / year
        if not src_dir.exists():
            continue

        dst_dir = archiv / year
        dst_dir.mkdir(exist_ok=True)

        for f in src_dir.iterdir():
            if not f.is_file():
                continue
            dst = dst_dir / f.name
            if dst.exists():
                # Rename with suffix to avoid overwrite
                stem = f.stem
                suffix = f.suffix
                counter = 1
                while dst.exists():
                    dst = dst_dir / f'{stem}_ib{counter}{suffix}'
                    counter += 1
                conflicts += 1

            shutil.move(str(f), str(dst))
            moved += 1

        # Remove empty source dir
        try:
            src_dir.rmdir()
            print(f'  verschoben: 00 Inbox/{year} → 99 Archiv/{year}')
        except OSError:
            print(f'  [WARN] 00 Inbox/{year} nicht leer nach move (Unterordner?)')

    print(f'Dateien verschoben:       {moved}')
    print(f'Umbenennungen (Konflikt): {conflicts}')

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('=== Phase 2.5: Ordnerbasierte kategorie: ===')
    fill_kategorie_from_folder()

    print()
    print('=== Phase 2.6: Inbox-Jahresordner → 99 Archiv ===')
    merge_inbox_to_archiv()
