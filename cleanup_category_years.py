#!/usr/bin/env python3
"""Vault-Kategorien aufräumen:
1. Leere Jahr-Ordner löschen
2. Dokumente in korrekte Jahr-Ordner verschieben (YYYYMMDD aus Dateiname)
3. 2026-Dokumente bleiben im Kategorie-Root
"""
import re
import shutil
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
DRY_RUN = False  # Auf False setzen zum Ausführen

def get_year_from_stem(stem: str) -> str | None:
    """Extrahiert Jahr aus YYYYMMDD-Präfix."""
    m = re.match(r"^(\d{4})\d{4}", stem)
    if m:
        y = int(m.group(1))
        if 1950 <= y <= 2035:
            return m.group(1)
    return None

# Kategorien = alle Top-Level-Ordner die mit Ziffern beginnen (außer Anlagen)
categories = sorted([
    d for d in VAULT.iterdir()
    if d.is_dir() and d.name[0].isdigit() and d.name != "Anlagen"
])

deleted_dirs = 0
moved_files = 0
skipped_files = 0

for cat in categories:
    cat_name = cat.name
    year_dirs = {d.name: d for d in cat.iterdir() if d.is_dir() and d.name.isdigit()}
    root_mds = [f for f in cat.iterdir() if f.suffix == ".md" and not f.name.startswith("._")]

    # 1. Leere Jahr-Ordner löschen
    for yd_name, yd_path in sorted(year_dirs.items()):
        has_content = any(yd_path.rglob("*.md"))
        if not has_content:
            # Auch rekursiv prüfen: gibt es Unterordner mit Inhalt?
            has_any = any(yd_path.iterdir())
            if not has_any:
                print(f"[DEL] Leerer Ordner: {cat_name}/{yd_name}/")
                if not DRY_RUN:
                    yd_path.rmdir()
                    deleted_dirs += 1

    # 2. Root-Dokumente in Jahr-Ordner verschieben
    for md_file in sorted(root_mds):
        year = get_year_from_stem(md_file.stem)

        if year is None:
            print(f"[SKIP] Kein YYYYMMDD:  {cat_name}/{md_file.name}")
            skipped_files += 1
            continue

        if year == "2026":
            # 2026 bleibt im Root
            continue

        # Ziel: cat/year/
        dest_dir = cat / year
        dest_file = dest_dir / md_file.name

        if dest_file.exists():
            print(f"[SKIP] Existiert:     {cat_name}/{year}/{md_file.name}")
            skipped_files += 1
            continue

        print(f"[MOVE] {cat_name}/{md_file.name} → {cat_name}/{year}/")
        if not DRY_RUN:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(md_file), str(dest_file))
            moved_files += 1

    # 3. Auch in Jahr-Unterordnern rekursiv nach ._ Dateien suchen (nicht löschen, nur melden)
    for yd_name, yd_path in sorted(year_dirs.items()):
        dotfiles = list(yd_path.rglob("._*"))
        if dotfiles:
            for df in dotfiles[:3]:
                print(f"[DOT] macOS-Rest:     {df.relative_to(VAULT)}")
            if len(dotfiles) > 3:
                print(f"      ... und {len(dotfiles)-3} weitere ._ Dateien in {cat_name}/{yd_name}")

if DRY_RUN:
    print(f"\n🔍 DRY-RUN — keine Änderungen. Setze DRY_RUN = False zum Ausführen.")
else:
    print(f"\n✅ Gelöschte Ordner: {deleted_dirs}, Verschoben: {moved_files}, Übersprungen: {skipped_files}")
