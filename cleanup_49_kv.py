#!/usr/bin/env python3
"""Cleanup-Skript für '49 Krankenversicherung'.

Phasen:
  1. Fehlklassifizierte Dateien in andere Vault-Ordner verschieben
  2. LEAS---UUID Dateien umbenennen
  3. Duplikate löschen (Gruppierung nach original:-PDF)
  4. Stubs (< OCR_STUB_CHARS Zeichen nach Frontmatter) nach 00 Wiederherstellung/
  5. Frontmatter-Upgrade: alte Evernote-Felder → kategorie_id / typ_id
  6. Dateien in Typ-Unterordner verschieben
  (Phase 7: DB-Rebuild ist separater Schritt, nicht in diesem Skript)

Aufruf:
  python3 cleanup_49_kv.py [--dry-run] [--phase 1,2,3,4,5,6]
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote
from collections import defaultdict
from datetime import date

VAULT_ROOT = Path("/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault")
KV_DIR     = VAULT_ROOT / "49 Krankenversicherung"
INBOX_DIR  = VAULT_ROOT / "00 Inbox"
WIEDER_DIR = KV_DIR / "00 Wiederherstellung"

# Ziel-Ordner für Phase 1 (fehlklassifiziert)
ZIELE = {
    "20 Familie":             VAULT_ROOT / "20 Familie",
    "30 FengShui":            VAULT_ROOT / "30 FengShui",
    "50 Immobilien eigen":    VAULT_ROOT / "50 Immobilien eigen",
    "51 Immobilien vermietet":VAULT_ROOT / "51 Immobilien vermietet",
    "70 Italien":             VAULT_ROOT / "70 Italien",
}

# Mindestzahl Zeichen im Dokumentkörper (nach Frontmatter), unter der eine Datei als Stub gilt
OCR_STUB_CHARS = 150

# ── Frontmatter ────────────────────────────────────────────────────────────────

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

def split_fm(text: str) -> tuple[str, str]:
    """Gibt (fm_block_ohne_delimiters, body) zurück."""
    m = _FM_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]

def read_fm_field(fm_raw: str, key: str) -> str:
    """Liest einen YAML-Scalar-Wert aus dem FM-Rohtext."""
    m = re.search(rf'^{re.escape(key)}:\s*(.+)', fm_raw, re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")

def set_fm_field(fm_raw: str, key: str, value: str) -> str:
    """Setzt/ersetzt einen Key in FM-Rohtext."""
    new_line = f'{key}: "{value}"'
    if re.search(rf'^{re.escape(key)}:', fm_raw, re.MULTILINE):
        return re.sub(rf'^{re.escape(key)}:.*', new_line, fm_raw, flags=re.MULTILINE)
    return fm_raw.rstrip() + f"\n{new_line}"

def remove_fm_field(fm_raw: str, key: str) -> str:
    """Entfernt einen Key aus FM-Rohtext (inkl. Zeilenende)."""
    return re.sub(rf'^{re.escape(key)}:.*\n?', '', fm_raw, flags=re.MULTILINE)

# ── PDF-Name aus original: ─────────────────────────────────────────────────────

_PDF_PATS = [
    re.compile(r'\[\[(?:[^\]/]+/)?([^\]]+\.pdf)\]\]', re.IGNORECASE),
    re.compile(r'(?:file://)?/[^\s"\']+/([^/\s"\']+\.pdf)', re.IGNORECASE),
    re.compile(r'\[([^\]]+\.pdf)\]\(', re.IGNORECASE),
    re.compile(r'"([^"]+\.pdf)"', re.IGNORECASE),
]

def extract_pdf_name(original_val: str) -> str | None:
    for pat in _PDF_PATS:
        m = pat.search(original_val)
        if m:
            return unquote(m.group(1).strip())
    return None

def original_quality(original_val: str) -> int:
    """Höher = besser. Anlagen-Wikilink ist beste Form."""
    if re.search(r'\[\[Anlagen/', original_val, re.IGNORECASE):
        return 3
    if re.search(r'\[\[', original_val):
        return 2
    if original_val and '/Volumes/' not in original_val and 'file://' not in original_val:
        return 1
    return 0

# ── Typ-Mapping für Phase 5 ────────────────────────────────────────────────────
# Alte Evernote/Altdokument-Felder → neue typ_id

OLD_KATEGORIE_TO_TYP_ID: dict[str, str] = {
    "leistungsabrechnung":          "leistungsabrechnung",
    "leistungsabrechnung_reinhard": "leistungsabrechnung",
    "leistungsabrechnung_marion":   "leistungsabrechnung",
    "arztrechnung":                 "arztrechnung",
    "rezept":                       "rezept",
    "beitragsanpassung":            "beitragsanpassung",
    "versicherungsschein":          "versicherungsschein",
    "beitragsbescheinigung":        "beitragsbescheinigung",
    "kostenuebernahme":             "kostenuebernahme",
    "versicherungsbedingungen":     "versicherungsbedingungen",
    "versicherungskorrespondenz":   "versicherungskorrespondenz",
    "sonstige_medizinische_leistung": "sonstige_medizinische_leistung",
}

# Typ-Label für kategorie:-Wert (Evernote-Format)
OLD_KATEGORIE_LABEL_TO_TYP_ID: dict[str, str] = {
    "leistungsabrechnung":           "leistungsabrechnung",
    "arztrechnung":                  "arztrechnung",
    "rezept":                        "rezept",
    "beitragsanpassung":             "beitragsanpassung",
    "versicherungsschein":           "versicherungsschein",
    "beitragsbescheinigung":         "beitragsbescheinigung",
    "kostenübernahme":               "kostenuebernahme",
    "kostenuebernahme":              "kostenuebernahme",
    "versicherungsbedingungen":      "versicherungsbedingungen",
    "versicherungskorrespondenz":    "versicherungskorrespondenz",
    "korrespondenz":                 "korrespondenz",
    "sonstige medizinische leistung":"sonstige_medizinische_leistung",
}

# Typ-ID → Typ-Label (für neues Frontmatter)
TYP_LABELS: dict[str, str] = {
    "leistungsabrechnung":          "Leistungsabrechnung",
    "arztrechnung":                 "Arztrechnung",
    "rezept":                       "Rezept",
    "beitragsanpassung":            "Beitragsanpassung",
    "versicherungsschein":          "Versicherungsschein",
    "beitragsbescheinigung":        "Beitragsbescheinigung",
    "kostenuebernahme":             "Kostenübernahme",
    "versicherungsbedingungen":     "Versicherungsbedingungen",
    "versicherungskorrespondenz":   "Versicherungskorrespondenz",
    "korrespondenz":                "Korrespondenz",
    "sonstige_medizinische_leistung":"Sonstige medizinische Leistung",
}

# Adressat-Lookup: patient: / adressat: → normiert
ADRESSAT_MAP: dict[str, str] = {
    "reinhard": "Reinhard",
    "reinhard janning": "Reinhard",
    "marion": "Marion",
    "marion janning": "Marion",
    "r": "Reinhard",
    "m": "Marion",
}

# ── Typ-Unterordner für Phase 6 ────────────────────────────────────────────────

TYP_SUBFOLDER: dict[str, str] = {
    "leistungsabrechnung":    "Leistungsabrechnung",
    "arztrechnung":           "Arztrechnung",
    "rezept":                 "Rezept",
    "beitragsanpassung":      "Beitragsinformation",
    "versicherungsschein":    "Beitragsinformation",
    "beitragsbescheinigung":  "Beitragsinformation",
    "kostenuebernahme":       "Arztrechnung",
    "versicherungsbedingungen":"Sonstiges",
    "versicherungskorrespondenz":"Sonstiges",
    "korrespondenz":          "Sonstiges",
    "sonstige_medizinische_leistung":"Sonstiges",
}
# Typen mit person_subfolder (Adressat als Suffix)
TYP_PERSON_SUBFOLDER: set[str] = {"leistungsabrechnung"}

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def iter_md(directory: Path, recursive: bool = False):
    """Iteriert über alle .md-Dateien, überspringt macOS Resource-Forks."""
    gen = directory.rglob("*.md") if recursive else directory.glob("*.md")
    for p in gen:
        if p.name.startswith("._"):
            continue
        yield p

def year_from_filename(name: str) -> str | None:
    """Extrahiert Jahr aus YYYYMMDD-Präfix oder YYYYMMDD_-Präfix."""
    m = re.match(r'^(\d{4})', name)
    if m:
        y = m.group(1)
        if 2000 <= int(y) <= 2035:
            return y
    return None

def body_length(text: str) -> int:
    """Zeichenanzahl nach dem Frontmatter."""
    _, body = split_fm(text)
    return len(body.strip())

def log(msg: str):
    print(msg)

def report(stats: dict):
    print()
    print("─" * 70)
    for k, v in stats.items():
        print(f"  {k:<40} {v}")
    print("─" * 70)

# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Fehlklassifizierte Dateien verschieben
# ══════════════════════════════════════════════════════════════════════════════

# Dateiname-Pattern → (Zielordner, ggf. Ziel-Subfolder innerhalb)
MISCLASSIFIED: list[tuple[re.Pattern, str, str]] = [
    # Tierarzt/Veterinaria → 20 Familie / Haustiere
    (re.compile(r'veterinaria|CLINICA_VET|KLINIK_VET', re.IGNORECASE),
     "20 Familie", "Haustiere"),
    # Health_GUA2, Health_GUA3 → 30 FengShui
    (re.compile(r'Health_GUA[23]', re.IGNORECASE),
     "30 FengShui", ""),
    # Wohnungsgeberbestätigung → 51 Immobilien vermietet
    (re.compile(r'Wohnungsgeberbestätigung|Wohnungsgebergeb', re.IGNORECASE),
     "51 Immobilien vermietet", ""),
    # Grundsteuer München → 51 Immobilien vermietet
    (re.compile(r'Grundsteuer', re.IGNORECASE),
     "51 Immobilien vermietet", ""),
    # Stadtentwässerung München → 20 Familie / Max und Berta Hutterer
    (re.compile(r'Stadtentwässerung|Stadtentwasserung', re.IGNORECASE),
     "20 Familie", "Max und Berta Hutterer"),
    # Acquedotto del Fiora → 50 Immobilien eigen
    (re.compile(r'Acquedotto', re.IGNORECASE),
     "50 Immobilien eigen", ""),
    # LP Pratiche Auto → 70 Italien
    (re.compile(r'LP_PRATICHE_AUTO|PRATICHE_AUTO', re.IGNORECASE),
     "70 Italien", ""),
]

def phase1_move_misclassified(dry_run: bool) -> dict:
    stats = {"moved": 0, "skipped": 0}
    # Suche nur im Root + undatiert-Ordner des KV-Dirs (nicht in Jahres-Unterordnern)
    candidates = list(iter_md(KV_DIR)) + list(iter_md(KV_DIR / "undatiert") if (KV_DIR / "undatiert").exists() else [])
    # Auch in Jahres-Unterordnern (nach Fehlklassifizierungen suchen)
    for year_dir in KV_DIR.iterdir():
        if year_dir.is_dir() and re.match(r'^\d{4}$', year_dir.name):
            candidates.extend(iter_md(year_dir))

    for md in candidates:
        for pattern, ziel_key, subfolder in MISCLASSIFIED:
            if pattern.search(md.name):
                ziel_base = ZIELE.get(ziel_key)
                if not ziel_base:
                    log(f"  ⚠️  Zielordner nicht konfiguriert: {ziel_key}")
                    break
                ziel = ziel_base / subfolder if subfolder else ziel_base
                if ziel == md.parent:
                    stats["skipped"] += 1
                    break
                dest = ziel / md.name
                log(f"  {'[DRY]' if dry_run else '→'} {md.relative_to(VAULT_ROOT)} → {dest.relative_to(VAULT_ROOT)}")
                if not dry_run:
                    ziel.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(md), str(dest))
                stats["moved"] += 1
                break
    return stats

# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — LEAS---UUID Dateien umbenennen
# ══════════════════════════════════════════════════════════════════════════════

def phase2_rename_leas(dry_run: bool) -> dict:
    stats = {"renamed": 0}
    for md in iter_md(KV_DIR):
        if "LEAS---" not in md.name:
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm_raw, body = split_fm(text)

        # Absender aus FM oder Body
        absender = read_fm_field(fm_raw, "absender") or ""
        if not absender:
            m = re.search(r'(Gothaer|HUK[-\s]?COBURG)', body, re.IGNORECASE)
            absender = m.group(0) if m else "Krankenversicherung"

        # Adressat
        adressat = read_fm_field(fm_raw, "adressat") or ""
        adressat_slug = adressat.capitalize() if adressat else "Reinhard"

        # Datum aus Dateiname
        date_m = re.match(r'^(\d{8})', md.stem)
        date_prefix = date_m.group(1) if date_m else date.today().strftime("%Y%m%d")

        # Neuer Name
        abs_slug = re.sub(r'[^\w]', '-', absender.strip())[:30]
        new_stem = f"{date_prefix}_{abs_slug}_Leistungsabrechnung_{adressat_slug}"
        new_name = new_stem + ".md"
        dest = md.parent / new_name

        if dest.exists():
            log(f"  ⚠️  Zieldatei existiert bereits: {new_name} — überspringe {md.name}")
            continue

        log(f"  {'[DRY]' if dry_run else '→'} {md.name} → {new_name}")
        if not dry_run:
            md.rename(dest)
        stats["renamed"] += 1
    return stats

# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Duplikate löschen
# ══════════════════════════════════════════════════════════════════════════════

def phase3_dedup(dry_run: bool) -> dict:
    """Gruppiert MDs nach PDF-Referenz (original:). Behält beste Version."""
    stats = {"deleted": 0, "groups_deduped": 0, "no_original": 0}

    # Sammle alle MDs + ihre Daten
    all_mds: list[dict] = []
    for md in KV_DIR.rglob("*.md"):
        if md.name.startswith("._"):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm_raw, body = split_fm(text)
        orig_val = read_fm_field(fm_raw, "original")
        pdf_name = extract_pdf_name(orig_val) if orig_val else None

        all_mds.append({
            "path":    md,
            "orig":    orig_val,
            "pdf":     pdf_name,
            "quality": original_quality(orig_val) if orig_val else -1,
            "body_len": len(body.strip()),
            "size":    md.stat().st_size,
        })

    # Gruppieren nach PDF-Name
    groups: dict[str, list[dict]] = defaultdict(list)
    no_pdf: list[dict] = []
    for info in all_mds:
        if info["pdf"]:
            groups[info["pdf"].lower()].append(info)
        else:
            no_pdf.append(info)
            stats["no_original"] += 1

    # Pro Gruppe: beste MD behalten
    for pdf_key, members in groups.items():
        if len(members) == 1:
            continue
        # Sortierung: quality DESC, body_len DESC, size DESC, kürzerer Name zuerst
        members.sort(key=lambda x: (
            -x["quality"],
            -x["body_len"],
            -x["size"],
            len(x["path"].name),
        ))
        keeper = members[0]
        duplicates = members[1:]
        stats["groups_deduped"] += 1
        log(f"\n  📌 Behalte: {keeper['path'].relative_to(VAULT_ROOT)} (q={keeper['quality']}, {keeper['body_len']}ch)")
        for dup in duplicates:
            log(f"    🗑  Lösche: {dup['path'].relative_to(VAULT_ROOT)} (q={dup['quality']}, {dup['body_len']}ch)")
            if not dry_run:
                dup["path"].unlink()
            stats["deleted"] += 1

    return stats

# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Stubs nach 00 Wiederherstellung/
# ══════════════════════════════════════════════════════════════════════════════

def phase4_stubs(dry_run: bool) -> dict:
    stats = {"moved": 0, "skipped_already_there": 0}

    for md in KV_DIR.rglob("*.md"):
        if md.name.startswith("._"):
            continue
        # Bereits in Wiederherstellung?
        if WIEDER_DIR in md.parents:
            stats["skipped_already_there"] += 1
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if body_length(text) < OCR_STUB_CHARS:
            dest = WIEDER_DIR / md.name
            # Bei Namenskonflikt Suffix anhängen
            counter = 1
            while dest.exists():
                dest = WIEDER_DIR / f"{md.stem}_{counter}.md"
                counter += 1
            log(f"  {'[DRY]' if dry_run else '→'} STUB {md.relative_to(KV_DIR)} ({body_length(text)}ch) → 00 Wiederherstellung/")
            if not dry_run:
                WIEDER_DIR.mkdir(parents=True, exist_ok=True)
                # todos:-Frontmatter anhängen
                fm_raw, body = split_fm(text)
                fm_raw_new = fm_raw.rstrip() + f'\ntodos: "OCR-Qualität prüfen — nur {body_length(text)} Zeichen erkannt"'
                new_text = f"---\n{fm_raw_new}\n---\n{body}" if fm_raw else f"---\ntodos: 'OCR prüfen'\n---\n{text}"
                dest.write_text(new_text, encoding="utf-8")
                md.unlink()
            stats["moved"] += 1

    return stats

# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Frontmatter-Upgrade (alte Evernote → kategorie_id / typ_id)
# ══════════════════════════════════════════════════════════════════════════════

def phase5_fm_upgrade(dry_run: bool) -> dict:
    stats = {"upgraded": 0, "already_ok": 0, "no_typ": 0}

    for md in KV_DIR.rglob("*.md"):
        if md.name.startswith("._"):
            continue
        if WIEDER_DIR in md.parents:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm_raw, body = split_fm(text)
        if not fm_raw:
            stats["no_typ"] += 1
            continue

        # Schon typ_id und kategorie_id vorhanden?
        existing_typ_id = read_fm_field(fm_raw, "typ_id")
        existing_kat_id = read_fm_field(fm_raw, "kategorie_id")
        existing_adressat = read_fm_field(fm_raw, "adressat")
        if existing_typ_id and existing_kat_id and existing_adressat:
            stats["already_ok"] += 1
            continue
        # typ_id+kat vorhanden aber adressat fehlt → nur Adressat ergänzen
        if existing_typ_id and existing_kat_id and not existing_adressat:
            absender_val = read_fm_field(fm_raw, "absender").lower()
            inferred = ""
            if existing_typ_id == "leistungsabrechnung":
                if "huk" in absender_val or "vigo" in absender_val:
                    inferred = "Marion"
                elif "gothaer" in absender_val:
                    inferred = "Reinhard"
            if inferred:
                log(f"  {'[DRY]' if dry_run else '✓'} {md.relative_to(KV_DIR)} → adressat={inferred!r} (Absender-Fallback)")
                if not dry_run:
                    fm_raw_new = set_fm_field(fm_raw, "adressat", inferred)
                    md.write_text(f"---\n{fm_raw_new}\n---\n{body}", encoding="utf-8")
                stats["upgraded"] += 1
            else:
                stats["already_ok"] += 1
            continue

        # Typ ermitteln: typ_id > typ > kategorie (alt)
        typ_id = existing_typ_id
        if not typ_id:
            typ_raw = read_fm_field(fm_raw, "typ_id") or read_fm_field(fm_raw, "typ")
            typ_id = OLD_KATEGORIE_TO_TYP_ID.get(typ_raw.lower().replace(" ", "_"), "")
        if not typ_id:
            kat_raw = read_fm_field(fm_raw, "kategorie").lower().strip()
            typ_id = OLD_KATEGORIE_LABEL_TO_TYP_ID.get(kat_raw, "")

        if not typ_id:
            stats["no_typ"] += 1
            continue

        typ_label = TYP_LABELS.get(typ_id, typ_id)

        # Adressat ermitteln: adressat > patient > Absender-Fallback (HUK→Marion, Gothaer→Reinhard)
        adressat_raw = (read_fm_field(fm_raw, "adressat") or
                        read_fm_field(fm_raw, "patient") or "").lower().strip()
        adressat = ADRESSAT_MAP.get(adressat_raw, adressat_raw.capitalize() if adressat_raw else "")
        if not adressat and typ_id == "leistungsabrechnung":
            absender_val = read_fm_field(fm_raw, "absender").lower()
            if "huk" in absender_val or "vigo" in absender_val:
                adressat = "Marion"
            elif "gothaer" in absender_val:
                adressat = "Reinhard"

        log(f"  {'[DRY]' if dry_run else '✓'} {md.relative_to(KV_DIR)} → typ_id={typ_id}, adressat={adressat!r}")

        if not dry_run:
            # kategorie_id setzen
            if not existing_kat_id:
                fm_raw = set_fm_field(fm_raw, "kategorie_id", "krankenversicherung")
                fm_raw = set_fm_field(fm_raw, "kategorie", "Krankenversicherung")
            # typ_id + typ setzen
            fm_raw = set_fm_field(fm_raw, "typ_id", typ_id)
            fm_raw = set_fm_field(fm_raw, "typ", typ_label)
            # adressat normieren
            if adressat and not read_fm_field(fm_raw, "adressat"):
                fm_raw = set_fm_field(fm_raw, "adressat", adressat)
            # alte Evernote-Felder entfernen
            for old_key in ("silo", "privacy", "patient"):
                fm_raw = remove_fm_field(fm_raw, old_key)
            md.write_text(f"---\n{fm_raw}\n---\n{body}", encoding="utf-8")

        stats["upgraded"] += 1

    return stats

# ══════════════════════════════════════════════════════════════════════════════
# Phase 6 — Dateien in Typ-Unterordner verschieben
# ══════════════════════════════════════════════════════════════════════════════

def phase6_type_subfolders(dry_run: bool) -> dict:
    stats = {"moved": 0, "skipped_no_typ": 0, "skipped_already_ok": 0, "conflict": 0}

    for md in list(KV_DIR.rglob("*.md")):
        if md.name.startswith("._"):
            continue
        if WIEDER_DIR in md.parents:
            continue

        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm_raw, _ = split_fm(text)
        typ_id = read_fm_field(fm_raw, "typ_id")
        if not typ_id:
            stats["skipped_no_typ"] += 1
            continue

        subfolder_name = TYP_SUBFOLDER.get(typ_id)
        if not subfolder_name:
            stats["skipped_no_typ"] += 1
            continue

        # Person-Subfolder?
        if typ_id in TYP_PERSON_SUBFOLDER:
            adressat = read_fm_field(fm_raw, "adressat").capitalize()
            if not adressat:
                adressat = "Sonstiges"
            subfolder_name = f"{subfolder_name} {adressat}"

        # Ziel: KV / [subfolder] / [Jahr] / file.md
        # Jahr ermitteln: datum-Feld > Dateiname
        datum_raw = read_fm_field(fm_raw, "datum") or read_fm_field(fm_raw, "date")
        year = None
        if datum_raw:
            m = re.search(r'\b(20\d{2})\b', datum_raw)
            if m:
                year = m.group(1)
        if not year:
            year = year_from_filename(md.name)
        if not year:
            year = "undatiert"

        # Bestimme Basisordner: KV oder Jahres-Unterordner
        # Files in KV/20XX/ bleiben in KV/subfolder/20XX/ oder KV/subfolder/
        # Files direkt in KV/ kommen auch in KV/subfolder/[year]/
        current_year_str = date.today().strftime("%Y")
        if year == current_year_str:
            target_dir = KV_DIR / subfolder_name
        else:
            target_dir = KV_DIR / subfolder_name / year

        if md.parent == target_dir:
            stats["skipped_already_ok"] += 1
            continue

        dest = target_dir / md.name
        if dest.exists():
            log(f"  ⚠️  Konflikt: {md.name} bereits in {target_dir.name}/ — überspringe")
            stats["conflict"] += 1
            continue

        log(f"  {'[DRY]' if dry_run else '→'} {md.relative_to(KV_DIR)} → {dest.relative_to(KV_DIR)}")
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(md), str(dest))
        stats["moved"] += 1

    return stats

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Cleanup 49 Krankenversicherung")
    parser.add_argument("--dry-run", action="store_true", help="Nichts schreiben, nur anzeigen")
    parser.add_argument("--phase", default="1,2,3,4,5,6",
                        help="Komma-getrennte Phasen (default: 1,2,3,4,5,6)")
    args = parser.parse_args()

    phases = {int(p.strip()) for p in args.phase.split(",") if p.strip()}
    dry = args.dry_run

    if dry:
        print("⚠️  DRY-RUN — keine Änderungen werden vorgenommen")
    print(f"Phasen: {sorted(phases)}")
    print()

    if 1 in phases:
        print("══ Phase 1: Fehlklassifizierte Dateien verschieben ══")
        s = phase1_move_misclassified(dry)
        report({"Verschoben": s["moved"], "Bereits am Ziel": s["skipped"]})

    if 2 in phases:
        print("\n══ Phase 2: LEAS---UUID Dateien umbenennen ══")
        s = phase2_rename_leas(dry)
        report({"Umbenannt": s["renamed"]})

    if 3 in phases:
        print("\n══ Phase 3: Duplikate löschen ══")
        s = phase3_dedup(dry)
        report({
            "Gruppen dedupliziert": s["groups_deduped"],
            "Dateien gelöscht":     s["deleted"],
            "Ohne original:":       s["no_original"],
        })

    if 4 in phases:
        print("\n══ Phase 4: Stubs nach 00 Wiederherstellung/ ══")
        s = phase4_stubs(dry)
        report({"Verschoben": s["moved"], "Bereits dort": s["skipped_already_there"]})

    if 5 in phases:
        print("\n══ Phase 5: Frontmatter-Upgrade ══")
        s = phase5_fm_upgrade(dry)
        report({
            "Aktualisiert":  s["upgraded"],
            "Bereits OK":    s["already_ok"],
            "Kein Typ":      s["no_typ"],
        })

    if 6 in phases:
        print("\n══ Phase 6: Typ-Unterordner ══")
        s = phase6_type_subfolders(dry)
        report({
            "Verschoben":         s["moved"],
            "Kein Typ":           s["skipped_no_typ"],
            "Bereits korrekt":    s["skipped_already_ok"],
            "Namenskonflikt":     s["conflict"],
        })


if __name__ == "__main__":
    main()
