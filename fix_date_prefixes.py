#!/usr/bin/env python3
"""Bereinigt Datumspräfix-Probleme in Vault-Dateinamen und Frontmatter.

Erkennt und behebt:
  P1: 00000000_ Präfix (kein Datum, Wilson-Fallback)
  P2: Unmögliches Datum (Monat=00, Tag=40+, Jahr < 1950)
  P3: DDMMYYYY-Präfix (Scanner-Format, YYYYMMDD-Monat > 12)
  P4: Doppeltes Datum (YYYYMMDD_SOURCE_YYYYMMDD_)
  P5: Bindestrich nach Datum (YYYYMMDD-Title statt YYYYMMDD_Title)
  P6: Kein Datumspräfix (UUID, Freitext)
  P7: Datum_original: 0000-00-00 im Frontmatter
  P8: Datum_original: unbekannt im Frontmatter

Aufruf:
  python fix_date_prefixes.py                    # Dry-Run: nur Analyse
  python fix_date_prefixes.py --apply            # Dateinamen fixen (P1-P6)
  python fix_date_prefixes.py --apply-fm         # Frontmatter fixen (P7-P8)
  python fix_date_prefixes.py --apply --apply-fm # Alles fixen
  python fix_date_prefixes.py --limit 10         # Nur erste 10 pro Phase
  python fix_date_prefixes.py --inbox-only       # Nur Dateien in 00 Inbox/
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# ── Pfade ───────────────────────────────────────────────────────────────────────

VAULT_PATH = Path(
    os.getenv(
        "VAULT_PATH",
        "/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault",
    )
)
ANLAGEN = VAULT_PATH / "Anlagen"
DB_PATH = Path(
    os.getenv(
        "DB_PATH",
        "/home/reinhard/docker/RYZEN - docling-workflow/dispatcher-config/dispatcher.db",
    )
)

# ── Logging ─────────────────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOG_DIR / f"fix_date_prefixes_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("fix_date_prefixes")

# ── Hilfsfunktionen ─────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    body = text[m.end() :]
    return fm, body


def build_frontmatter_str(fm: dict) -> str:
    """Schreibt Frontmatter als YAML-Block zurück."""
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, str) and ("\n" in v or v.startswith('"')):
            lines.append(f"{k}: {v}")
        elif isinstance(v, str) and (":" in v or v.startswith("[")):
            escaped = v.replace('"', '\\"')
            lines.append(f'{k}: "{escaped}"')
        elif v is None:
            lines.append(f"{k}:")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _valid_ymd(yyyy_s: str, mm_s: str, dd_s: str) -> bool:
    """Prüft ob YYYY, MM, DD ein plausibles Datum im Bereich 1950–2035 ergibt."""
    try:
        y, m, d = int(yyyy_s), int(mm_s), int(dd_s)
        return 1950 <= y <= 2035 and 1 <= m <= 12 and 1 <= d <= 31
    except ValueError:
        return False


def date_from_filename_prefix(stem: str) -> Optional[str]:
    """Extrahiert YYYYMMDD aus Dateinamen-Prefix. None wenn ungültig."""
    m = re.match(r"^(\d{8})", stem)
    if not m:
        return None
    s = m.group(1)
    yyyy, mm, dd = s[:4], s[4:6], s[6:]
    if _valid_ymd(yyyy, mm, dd):
        return s  # YYYYMMDD ✓
    # DDMMYYYY (Scanner-Format)
    dd2, mm2, yyyy2 = s[:2], s[2:4], s[4:]
    if _valid_ymd(yyyy2, mm2, dd2):
        return f"{yyyy2}{mm2}{dd2}"  # umdrehen → YYYYMMDD
    return None


def classify_date_problem(stem: str) -> Optional[tuple[str, Optional[str]]]:
    """Klassifiziert das Datumspräfix-Problem eines Dateinamens.

    Returns:
        (problem_code, corrected_date_yyyymmdd | None)
        problem_code: "P1" – "P6" oder None wenn kein Problem.
    """
    m = re.match(r"^(\d{8})", stem)
    if not m:
        return ("P6", None)  # Kein Datumspräfix

    s = m.group(1)
    yyyy, mm, dd = s[:4], s[4:6], s[6:]

    # P1: 00000000
    if s == "00000000":
        return ("P1", None)

    # Prüfe YYYYMMDD
    if _valid_ymd(yyyy, mm, dd):
        # Gültiges YYYYMMDD — prüfe Doppeldatum (P4):
        # Pattern YYYYMMDD_SOURCE_YYYYMMDD_... wobei beide 8-stelligen Blöcke valide Daten sind
        rest = stem[8:]
        second_date_m = re.search(r"[_\s\-](\d{8})[_\s\-]", rest)
        if second_date_m:
            second_date = date_from_filename_prefix(second_date_m.group(1))
            if second_date:
                return ("P4", s)  # Doppeltes Datum
        # Prüfe Bindestrich (P5)
        if stem[8:9] == "-":
            return ("P5", s)  # Bindestrich nach Datum
        return (None, s)  # Alles OK

    # P2/P3: YYYYMMDD ungültig
    dd2, mm2, yyyy2 = s[:2], s[2:4], s[4:]
    if _valid_ymd(yyyy2, mm2, dd2):
        # DDMMYYYY gültig → P3 (flippbar)
        corrected = f"{yyyy2}{mm2}{dd2}"
        return ("P3", corrected)
    else:
        # Beide Interpretationen ungültig → P2
        return ("P2", None)


def get_pdf_name_from_md(md_path: Path) -> Optional[str]:
    """Ermittelt den PDF-Dateinamen aus dem original:-Feld oder Wikilink."""
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = split_frontmatter(content)

    # Aus original-Feld
    original = fm.get("original", "")
    pdf_m = re.search(r"Anlagen/([^\]]+\.pdf)", str(original))
    if pdf_m:
        return pdf_m.group(1)

    # Aus Wikilink im Body
    body_m = re.search(r"\[\[Anlagen/([^\]]+\.pdf)\]\]", body)
    if body_m:
        return body_m.group(1)

    return None


def update_pdf_reference(
    md_path: Path, old_pdf: str, new_pdf: str
) -> None:
    """Aktualisiert original:-Feld und Wikilink im Body."""
    content = md_path.read_text(encoding="utf-8")
    updated = content.replace(
        f"Anlagen/{old_pdf}", f"Anlagen/{new_pdf}"
    )
    if updated != content:
        md_path.write_text(updated, encoding="utf-8")
        log.info(f"  PDF-Referenz aktualisiert: {old_pdf} → {new_pdf}")


def get_date_from_frontmatter(md_path: Path) -> Optional[str]:
    """Versucht ein valides Datum aus dem Frontmatter zu extrahieren (YYYYMMDD)."""
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, _ = split_frontmatter(content)

    # Datum_original (YYYY-MM-DD)
    do = fm.get("Datum_original", "")
    if isinstance(do, str) and re.match(r"\d{4}-\d{2}-\d{2}", do):
        return do.replace("-", "")

    # datum (DD.MM.YYYY)
    datum = fm.get("datum", "")
    if isinstance(datum, str) and re.match(r"\d{2}\.\d{2}\.\d{4}", datum):
        d, m, y = datum.split(".")
        if _valid_ymd(y, m, d):
            return f"{y}{m}{d}"

    # erstellt (YYYY-MM-DD)
    erstellt = fm.get("erstellt", "")
    if isinstance(erstellt, str) and re.match(r"\d{4}-\d{2}-\d{2}", erstellt):
        return erstellt.replace("-", "")

    return None


def rename_md_and_pdf(
    md_path: Path, new_stem: str, dry_run: bool, conn: Optional[sqlite3.Connection]
) -> bool:
    """Benennt MD + verlinktes PDF um. Gibt True bei Erfolg zurück."""
    vault = VAULT_PATH
    anlagen = ANLAGEN

    new_md_name = f"{new_stem}.md"
    new_md_path = md_path.parent / new_md_name

    if new_md_path == md_path:
        return False  # nichts zu tun

    if new_md_path.exists():
        log.warning(f"  [SKIP] Ziel existiert bereits: {new_md_path.relative_to(vault)}")
        return False

    old_pdf_name = get_pdf_name_from_md(md_path)
    new_pdf_name = f"{new_stem}.pdf"
    old_pdf_path = anlagen / old_pdf_name if old_pdf_name else None
    new_pdf_path = anlagen / new_pdf_name

    log.info(f"  [REN] {md_path.relative_to(vault)}")
    log.info(f"     →  {new_md_path.relative_to(vault)}")

    if old_pdf_name:
        log.info(f"    PDF: {old_pdf_name}  →  {new_pdf_name}")

    if dry_run:
        return True

    # 1. PDF umbenennen
    if old_pdf_path and old_pdf_path.exists():
        if not new_pdf_path.exists():
            old_pdf_path.rename(new_pdf_path)
        update_pdf_reference(md_path, old_pdf_name, new_pdf_name)

    # 2. MD umbenennen
    md_path.rename(new_md_path)

    # 3. DB aktualisieren
    if conn:
        try:
            old_rel = str(md_path.relative_to(vault))
            new_rel = str(new_md_path.relative_to(vault))
            conn.execute(
                "UPDATE dokumente SET vault_pfad = ?, dateiname = ? "
                "WHERE vault_pfad = ? OR dateiname = ?",
                (new_rel, new_md_name, old_rel, md_path.name),
            )
            conn.commit()
        except Exception as e:
            log.warning(f"  DB-Update fehlgeschlagen: {e}")

    return True


def move_to_inbox(
    md_path: Path, new_stem: str, dry_run: bool, conn: Optional[sqlite3.Connection]
) -> bool:
    """Verschiebt MD + PDF nach 00 Inbox/."""
    vault = VAULT_PATH
    inbox = vault / "00 Inbox"
    anlagen = ANLAGEN

    new_md_name = f"{new_stem}.md"
    new_md_path = inbox / new_md_name

    if new_md_path.exists():
        # Kollision: nummeriere
        base = new_stem
        counter = 2
        while (inbox / f"{base}_{counter}.md").exists():
            counter += 1
        new_stem = f"{base}_{counter}"
        new_md_name = f"{new_stem}.md"
        new_md_path = inbox / new_md_name

    log.info(f"  [INBOX] {md_path.relative_to(vault)}")
    log.info(f"       →  {new_md_path.relative_to(vault)}")

    old_pdf_name = get_pdf_name_from_md(md_path)
    new_pdf_name = f"{new_stem}.pdf"
    old_pdf_path = anlagen / old_pdf_name if old_pdf_name else None
    new_pdf_path = anlagen / new_pdf_name

    if dry_run:
        return True

    inbox.mkdir(parents=True, exist_ok=True)

    # 1. PDF umbenennen/verschieben
    if old_pdf_path and old_pdf_path.exists():
        if not new_pdf_path.exists():
            old_pdf_path.rename(new_pdf_path)
        update_pdf_reference(md_path, old_pdf_name, new_pdf_name)

    # 2. MD verschieben
    shutil.move(str(md_path), str(new_md_path))

    # 3. Frontmatter aktualisieren (kategorie entfernen)
    try:
        content = new_md_path.read_text(encoding="utf-8")
        fm, body = split_frontmatter(content)
        fm.pop("kategorie", None)
        # Datum_original korrigieren falls nötig
        if fm.get("Datum_original") in ("0000-00-00", "unbekannt", None, ""):
            date_from_fm = get_date_from_frontmatter(md_path)  # nutzt altes fm
            if date_from_fm:
                fm["Datum_original"] = f"{date_from_fm[:4]}-{date_from_fm[4:6]}-{date_from_fm[6:]}"
            else:
                fm["Datum_original"] = "unbekannt"
        new_content = build_frontmatter_str(fm) + body
        new_md_path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        log.warning(f"  Frontmatter-Update fehlgeschlagen: {e}")

    # 4. DB aktualisieren
    if conn:
        try:
            old_rel = str(md_path.relative_to(vault))
            new_rel = str(new_md_path.relative_to(vault))
            conn.execute(
                "UPDATE dokumente SET vault_pfad = ?, dateiname = ?, kategorie = NULL "
                "WHERE vault_pfad = ? OR dateiname = ?",
                (new_rel, new_md_name, old_rel, md_path.name),
            )
            conn.commit()
        except Exception as e:
            log.warning(f"  DB-Update fehlgeschlagen: {e}")

    return True


# ── Phasen ──────────────────────────────────────────────────────────────────────


def phase_p1_p2_p3(
    md_path: Path,
    problem: str,
    corrected_date: Optional[str],
    dry_run: bool,
    conn: Optional[sqlite3.Connection],
) -> tuple[int, int]:
    """P1/P2/P3: Falscher/fehlender Datumspräfix.

    P1: 00000000_ → Datum aus Frontmatter holen, sonst in Inbox mit NODATE_
    P2: unmögliches Datum → DDMMYYYY-Flip versuchen, sonst wie P1
    P3: DDMMYYYY → zu YYYYMMDD flippen
    """
    stem = md_path.stem

    if problem == "P3" and corrected_date:
        # P3: Datum flippen von DDMMYYYY → YYYYMMDD
        rest = re.sub(r"^\d{8}", "", stem)
        # Entferne führenden Separator
        rest = re.sub(r"^[_\s\-]+", "", rest)
        new_stem = f"{corrected_date}_{rest}" if rest else corrected_date
        ok = rename_md_and_pdf(md_path, new_stem, dry_run, conn)
        return (1, 0) if ok else (0, 1)

    # P1/P2: Kein gültiges Datum
    date_from_fm = get_date_from_frontmatter(md_path)

    if date_from_fm:
        # Datum aus Frontmatter gefunden → danach umbenennen
        rest = re.sub(r"^\d{8}", "", stem)
        rest = re.sub(r"^[_\s\-]+", "", rest)
        new_stem = f"{date_from_fm}_{rest}" if rest else date_from_fm
        ok = rename_md_and_pdf(md_path, new_stem, dry_run, conn)
        if ok:
            log.info(f"    Datum aus Frontmatter: {date_from_fm}")
        return (1, 0) if ok else (0, 1)
    else:
        # Kein Datum aus Frontmatter → in Inbox mit NODATE_
        rest = re.sub(r"^\d{8}", "", stem)
        rest = re.sub(r"^[_\s\-]+", "", rest)
        clean_rest = re.sub(r"[^\w\-äöüÄÖÜß]", "", rest.replace(" ", "_"))
        if len(clean_rest) > 60:
            clean_rest = clean_rest[:60]
        new_stem = f"NODATE_{clean_rest}" if clean_rest else "NODATE"
        ok = move_to_inbox(md_path, new_stem, dry_run, conn)
        return (1, 0) if ok else (0, 1)


def phase_p4(md_path: Path, dry_run: bool, conn: Optional[sqlite3.Connection]) -> tuple[int, int]:
    """P4: Doppeltes Datum YYYYMMDD_SOURCE_YYYYMMDD_Rest → YYYYMMDD_SOURCE_Rest."""
    stem = md_path.stem
    # Pattern: YYYYMMDD_SOMETHING_YYYYMMDD_... oder YYYYMMDD-YYYYMMDD_...
    # Entferne das zweite YYYYMMDD_
    m = re.match(r"^(\d{8}[_\-].*?)[_\-](\d{8}[_\-])(.*)$", stem)
    if not m:
        return (0, 1)

    first_part = m.group(1)  # YYYYMMDD_SOURCE
    rest = m.group(3)  # Rest nach zweitem Datum
    new_stem = f"{first_part}_{rest}" if rest else first_part
    # Normalisiere
    new_stem = re.sub(r"[_\s]+", "_", new_stem).strip("_")

    ok = rename_md_and_pdf(md_path, new_stem, dry_run, conn)
    return (1, 0) if ok else (0, 1)


def phase_p5(md_path: Path, dry_run: bool, conn: Optional[sqlite3.Connection]) -> tuple[int, int]:
    """P5: Bindestrich nach Datum YYYYMMDD- → YYYYMMDD_."""
    stem = md_path.stem
    new_stem = re.sub(r"^(\d{8})-", r"\1_", stem)
    if new_stem == stem:
        return (0, 0)
    ok = rename_md_and_pdf(md_path, new_stem, dry_run, conn)
    return (1, 0) if ok else (0, 1)


def phase_p6(md_path: Path, dry_run: bool, conn: Optional[sqlite3.Connection]) -> tuple[int, int]:
    """P6: Kein Datumspräfix → in Inbox verschieben.

    Nur Dispatcher-Dokumente (mit original: Anlagen/ im Frontmatter) werden
    verschoben. Vault-native Notizen ohne original: bleiben unangetastet.
    """
    stem = md_path.stem

    # Nur Dispatcher-Dokumente verschieben (erkennbar am original: Feld)
    pdf_name = get_pdf_name_from_md(md_path)
    if not pdf_name:
        log.info(f"    Übersprungen (kein original: — Vault-native Notiz)")
        return (0, 1)

    date_from_fm = get_date_from_frontmatter(md_path)

    if date_from_fm:
        new_stem = f"{date_from_fm}_{stem}"
        if len(new_stem) > 120:
            new_stem = new_stem[:120]
        ok = rename_md_and_pdf(md_path, new_stem, dry_run, conn)
        return (1, 0) if ok else (0, 1)
    else:
        # In Inbox mit NODATE_
        clean_stem = re.sub(r"[^\w\-äöüÄÖÜß]", "", stem.replace(" ", "_"))
        if len(clean_stem) > 60:
            clean_stem = clean_stem[:60]
        new_stem = f"NODATE_{clean_stem}" if clean_stem else "NODATE"
        ok = move_to_inbox(md_path, new_stem, dry_run, conn)
        return (1, 0) if ok else (0, 1)


def fix_frontmatter_datum_original(
    md_path: Path, dry_run: bool
) -> tuple[bool, str]:
    """Fixiert Datum_original im Frontmatter.
    Returns (changed, new_value).
    """
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return (False, "")

    fm, body = split_frontmatter(content)
    current = fm.get("Datum_original", "")
    current_str = str(current) if current is not None else ""

    # Auch im Raw-Text prüfen (YAML crashed bei "0000-00-00")
    raw_fm_match = re.search(
        r"Datum_original:\s*(0000-00-00|unbekannt)",
        content,
    )
    raw_do = raw_fm_match.group(1) if raw_fm_match else ""

    # Nur korrigieren wenn 0000-00-00 (oder YAML-geparst als 0) oder unbekannt
    if current_str not in ("0000-00-00", "0", "unbekannt", "") and raw_do not in ("0000-00-00", "unbekannt"):
        return (False, str(current))

    # Versuche Datum aus Dateinamen-Präfix
    date_yyyymmdd = date_from_filename_prefix(md_path.stem)
    if date_yyyymmdd:
        new_val = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:]}"
        if raw_do == "0000-00-00":
            # YAML crashed → direkt per Regex ersetzen
            if not dry_run:
                new_content = re.sub(
                    r"Datum_original:\s*0000-00-00",
                    f"Datum_original: {new_val}",
                    content,
                )
                md_path.write_text(new_content, encoding="utf-8")
        else:
            fm["Datum_original"] = new_val
            if not dry_run:
                new_content = build_frontmatter_str(fm) + body
                md_path.write_text(new_content, encoding="utf-8")
        return (True, new_val)

    # Kein valides Datum aus Dateinamen → 0000-00-00 → unbekannt
    if raw_do == "0000-00-00":
        if not dry_run:
            new_content = re.sub(
                r"Datum_original:\s*0000-00-00",
                "Datum_original: unbekannt",
                content,
            )
            md_path.write_text(new_content, encoding="utf-8")
        return (True, "unbekannt")

    return (False, str(current))


# ── Hauptlogik ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Bereinigt Datumspräfix-Probleme in Vault-Dateinamen"
    )
    parser.add_argument("--apply", action="store_true", help="Dateinamen-Fixes durchführen (P1-P6)")
    parser.add_argument("--apply-fm", action="store_true", help="Frontmatter-Fixes durchführen (P7-P8)")
    parser.add_argument("--limit", type=int, default=0, help="Maximale Anzahl pro Phase")
    parser.add_argument("--inbox-only", action="store_true", help="Nur Dateien in 00 Inbox/")
    parser.add_argument(
        "--phase",
        type=str,
        default="",
        help="Nur eine Phase ausführen (P1,P2,P3,P4,P5,P6,P7,P8)",
    )
    args = parser.parse_args()

    vault = VAULT_PATH
    if not vault.exists():
        log.error(f"Vault nicht gefunden: {vault}")
        sys.exit(1)

    dry_run_name = not args.apply and not args.apply_fm
    dry_run_fm = not args.apply_fm
    dry_run_files = not args.apply

    log.info("=" * 60)
    log.info(f"fix_date_prefixes.py — Start: {timestamp}")
    log.info(f"Vault:  {vault}")
    log.info(
        f"Modus:  Dateien={'APPLY' if args.apply else 'DRY-RUN'}, "
        f"Frontmatter={'APPLY' if args.apply_fm else 'DRY-RUN'}"
    )
    if args.inbox_only:
        log.info("Filter: Nur 00 Inbox/")
    if args.phase:
        log.info(f"Phase:  {args.phase}")
    if args.limit:
        log.info(f"Limit:  {args.limit} pro Phase")
    log.info("=" * 60)

    # DB-Verbindung (nur bei --apply)
    conn = None
    if args.apply and DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
        except Exception as e:
            log.warning(f"DB nicht erreichbar: {e}")

    # ── Phase: Dateinamen scannen und klassifizieren ─────────────────────────

    problems: dict[str, list[Path]] = {
        "P1": [], "P2": [], "P3": [], "P4": [], "P5": [], "P6": [],
    }

    log.info("\n🔍 Scanne Vault nach Datumspräfix-Problemen ...")
    for md_path in sorted(vault.rglob("*.md")):
        # Bestimmte Ordner ausschließen
        rel = str(md_path.relative_to(vault))
        if any(
            rel.startswith(skip)
            for skip in (".obsidian", ".trash", ".claude", "_templates", "Anlagen")
        ):
            continue
        # Versteckte Ordner ausschliessen (.resources/, etc.)
        if any(part.startswith(".") and part not in (".", "..") for part in md_path.parent.parts):
            continue
        if md_path.name.startswith("._"):  # macOS resource forks
            continue

        if args.inbox_only and not rel.startswith("00 Inbox"):
            continue

        problem, corrected_date = classify_date_problem(md_path.stem)
        if problem and problem in problems:
            problems[problem].append(md_path)

    # ── Bericht ──────────────────────────────────────────────────────────────
    log.info(f"\n📊 Datumspräfix-Probleme ({'DRY-RUN' if dry_run_files else 'APPLY'}):\n")
    phase_descriptions = {
        "P1": "00000000_ Präfix (Wilson-Fallback ohne Datum)",
        "P2": "Unmögliches Datum (Monat=00, Tag>31, Jahr<1950)",
        "P3": "DDMMYYYY-Präfix (Scanner-Format, Tag>12 verrät es)",
        "P4": "Doppeltes Datum (YYYYMMDD_SOURCE_YYYYMMDD_)",
        "P5": "Bindestrich nach Datum (YYYYMMDD- statt YYYYMMDD_)",
        "P6": "Kein Datumspräfix (UUID, Freitext, NODATE)",
    }

    total = 0
    for phase_code in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        items = problems[phase_code]
        limit = min(len(items), args.limit) if args.limit else len(items)
        total += limit
        if items:
            log.info(f"  {phase_code}: {len(items):5d} Dateien — {phase_descriptions[phase_code]}")
            if args.limit and len(items) > args.limit:
                log.info(f"         (limitiert auf {args.limit})")
        else:
            log.info(f"  {phase_code}:     0 Dateien — {phase_descriptions[phase_code]}")
    log.info(f"  ─────────────────────")
    log.info(f"  Gesamt: {total} problematische Dateien")
    log.info("")

    if total == 0:
        log.info("✅ Keine Datumspräfix-Probleme gefunden.")

    # ── Phase: Dateinamen fixen (P1-P6) ──────────────────────────────────────
    if total > 0:
        stats: dict[str, tuple[int, int]] = {}  # phase → (fixed, skipped)

        for phase_code in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            if args.phase and args.phase != phase_code:
                continue
            items = problems[phase_code]
            if args.limit:
                items = items[: args.limit]
            if not items:
                continue

            log.info(f"\n─ {phase_code}: {phase_descriptions[phase_code]} ─")
            fixed, skipped = 0, 0

            for md_path in items:
                log.info(f"  {md_path.relative_to(vault)}")
                _, corrected_date = classify_date_problem(md_path.stem)

                if phase_code in ("P1", "P2", "P3"):
                    f, s = phase_p1_p2_p3(
                        md_path, phase_code, corrected_date,
                        dry_run_files, conn
                    )
                    fixed += f
                    skipped += s
                elif phase_code == "P4":
                    f, s = phase_p4(md_path, dry_run_files, conn)
                    fixed += f
                    skipped += s
                elif phase_code == "P5":
                    f, s = phase_p5(md_path, dry_run_files, conn)
                    fixed += f
                    skipped += s
                elif phase_code == "P6":
                    f, s = phase_p6(md_path, dry_run_files, conn)
                    fixed += f
                    skipped += s

            stats[phase_code] = (fixed, skipped)
            log.info(f"  → {fixed} behoben, {skipped} übersprungen")

        # Zusammenfassung Dateinamen
        log.info(f"\n{'─' * 40}")
        log.info(f"Dateinamen-Fixes: {'DRY-RUN (keine Änderungen)' if dry_run_files else 'ANGEWENDET'}")
        total_fixed = sum(f for f, s in stats.values())
        total_skipped = sum(s for f, s in stats.values())
        log.info(f"  Behohen:    {total_fixed}")
        log.info(f"  Üersprungen: {total_skipped}")

    # ── Phase: Frontmatter fixen (P7-P8) ────────────────────────────────────
    if args.apply_fm or (dry_run_fm and not args.apply and not args.apply_fm):
        # Frontmatter-Scan ist im Dry-Run immer enthalten
        fm_problems = {"P7": [], "P8": []}  # 0000-00-00, unbekannt
        for md_path in sorted(vault.rglob("*.md")):
            rel = str(md_path.relative_to(vault))
            if any(
                rel.startswith(skip)
                for skip in (".obsidian", ".trash", ".claude", "_templates", "Anlagen")
            ):
                continue
            if any(part.startswith(".") and part not in (".", "..") for part in md_path.parent.parts):
                continue
            if md_path.name.startswith("._"):
                continue
            if args.inbox_only and not rel.startswith("00 Inbox"):
                continue

            try:
                content = md_path.read_text(encoding="utf-8")
                fm, _ = split_frontmatter(content)
                # YAML-safe_load scheitert bei "0000-00-00" (datetime-Konstruktor),
                # daher zusaetzlich im Raw-Text pruefen.
                do = fm.get("Datum_original", "")
                do_str = str(do) if do is not None else ""
                # Raw-Text-Check für kaputte Datum_original-Felder
                raw_fm_match = re.search(
                    r"Datum_original:\s*(0000-00-00|unbekannt)",
                    content,
                )
                raw_do = raw_fm_match.group(1) if raw_fm_match else ""
                if do_str in ("0000-00-00", "0") or raw_do == "0000-00-00":
                    fm_problems["P7"].append(md_path)
                elif do_str == "unbekannt" or raw_do == "unbekannt":
                    fm_problems["P8"].append(md_path)
            except Exception:
                continue

        log.info(f"\n📊 Frontmatter-Probleme ({'DRY-RUN' if dry_run_fm else 'APPLY'}):\n")
        p7_limit = (
            min(len(fm_problems["P7"]), args.limit)
            if args.limit
            else len(fm_problems["P7"])
        )
        p8_limit = (
            min(len(fm_problems["P8"]), args.limit)
            if args.limit
            else len(fm_problems["P8"])
        )
        log.info(f"  P7: {len(fm_problems['P7']):5d} Dateien — Datum_original: 0000-00-00")
        log.info(f"  P8: {len(fm_problems['P8']):5d} Dateien — Datum_original: unbekannt")

        if args.phase and args.phase not in ("P7", "P8"):
            fm_problems = {"P7": [], "P8": []}

        fm_fixed, fm_skipped = 0, 0
        for phase_code in ("P7", "P8"):
            if args.phase and args.phase != phase_code:
                continue
            items = fm_problems[phase_code]
            if args.limit:
                items = items[: args.limit]
            if not items:
                continue

            log.info(f"\n─ {phase_code}: Frontmatter-Fix ─")
            for md_path in items:
                log.info(f"  {md_path.relative_to(vault)}")
                changed, new_val = fix_frontmatter_datum_original(
                    md_path, dry_run_fm
                )
                if changed:
                    log.info(f"    Datum_original → {new_val}")
                    fm_fixed += 1
                else:
                    fm_skipped += 1

        log.info(f"\n  Frontmatter: {fm_fixed} behoben, {fm_skipped} übersprungen")

    # ── Aufräumen ───────────────────────────────────────────────────────────
    if conn:
        conn.close()

    log.info(f"\n✅ Fertig. Log: {LOG_FILE}")
    if dry_run_files and dry_run_fm:
        log.info("💡 DRY-RUN — keine Änderungen. Nutze --apply / --apply-fm zum Ausführen.")


if __name__ == "__main__":
    main()
