#!/usr/bin/env python3
"""
Importiert Vault-Markdown-Dateien (Kategorie krankenkasse) in dispatcher.db.
Reihenfolge: arztrechnung → hilfsmittel → anderes → rezept → leistungsabrechnung
"""
import re
import sqlite3
from pathlib import Path

VAULT_BASE = Path("/home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault/Converted/krankenkasse")
DB_FILE    = Path("/home/reinhard/docker/docling-workflow/dispatcher-temp/dispatcher.db")

IMPORT_ORDER = ["arztrechnung", "hilfsmittel", "anderes", "rezept", "leistungsabrechnung"]

# Kategorien die eine Rechnung in der rechnungen-Tabelle erzeugen
RECHNUNG_TYPEN = {"arztrechnung", "hilfsmittel", "anderes", "rezept"}


def parse_frontmatter(text: str) -> dict:
    """Extrahiert YAML-Frontmatter (nur einfache Key: Value Felder)."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        kv = re.match(r'^(\w+):\s*"?(.*?)"?\s*$', line.strip())
        if kv:
            fm[kv.group(1)] = kv.group(2).strip('"').strip()
    return fm


def parse_betrag(s: str) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(s)).replace(",", ".")
    # Falls mehrere Punkte: letzten als Dezimaltrenner behandeln
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def fmt_datum(s: str) -> str | None:
    """YYYY-MM-DD → DD.MM.YYYY, bereits DD.MM.YYYY bleibt."""
    if not s:
        return None
    s = s.strip('"').strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    if re.match(r"\d{2}\.\d{2}\.\d{4}", s):
        return s
    return None


def infer_adressat(fm: dict, filepath: Path) -> str | None:
    """Adressat aus patient-Feld oder Zusammenfassung."""
    p = fm.get("patient", "")
    if p:
        if "reinhard" in p.lower():
            return "Reinhard"
        if "marion" in p.lower():
            return "Marion"
    # Zusammenfassung
    zus = fm.get("zusammenfassung", "").lower()
    if "reinhard" in zus:
        return "Reinhard"
    if "marion" in zus:
        return "Marion"
    # Tags
    tags_raw = fm.get("tags", "")
    if "reinhard" in tags_raw.lower():
        return "Reinhard"
    if "marion" in tags_raw.lower():
        return "Marion"
    return None


def get_typ(kategorie: str, adressat: str | None) -> str:
    if kategorie == "leistungsabrechnung":
        if adressat == "Marion":
            return "leistungsabrechnung_marion"
        return "leistungsabrechnung_reinhard"
    return kategorie  # arztrechnung, hilfsmittel, rezept, anderes


def get_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def import_kategorie(con: sqlite3.Connection, kategorie: str) -> tuple[int, int]:
    files = sorted(VAULT_BASE.rglob(f"{kategorie}/**/*.md"))
    inserted = 0
    skipped  = 0

    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        fm   = parse_frontmatter(text)
        if not fm:
            skipped += 1
            continue

        quelle   = fm.get("quelle") or f.stem + ".pdf"
        datum    = fmt_datum(fm.get("datum"))
        absender = fm.get("absender") or ""
        adressat = infer_adressat(fm, f)
        typ      = get_typ(kategorie, adressat)

        # Duplikat-Check
        if con.execute("SELECT 1 FROM dokumente WHERE dateiname=?", (quelle,)).fetchone():
            skipped += 1
            continue

        cur = con.execute(
            """INSERT INTO dokumente (dateiname, rechnungsdatum, kategorie, typ, absender, adressat, konfidenz)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (quelle, datum, "krankenversicherung", typ, absender, adressat, "hoch")
        )
        dok_id = cur.lastrowid

        if kategorie in RECHNUNG_TYPEN:
            betrag    = parse_betrag(fm.get("betrag"))
            faellig   = fmt_datum(fm.get("faellig"))
            con.execute(
                "INSERT INTO rechnungen (dokument_id, rechnungsbetrag, faelligkeitsdatum) VALUES (?, ?, ?)",
                (dok_id, betrag, faellig)
            )

        inserted += 1

    con.commit()
    return inserted, skipped


def main():
    with get_db() as con:
        for kat in IMPORT_ORDER:
            ins, skip = import_kategorie(con, kat)
            print(f"{kat:25s}  inserted: {ins:4d}  skipped: {skip:4d}")

    # Zusammenfassung
    with get_db() as con:
        print()
        print("=== DB-Zusammenfassung ===")
        for row in con.execute("SELECT typ, COUNT(*) as n FROM dokumente GROUP BY typ ORDER BY typ"):
            print(f"  {row[0]:40s} {row[1]:4d} Dokumente")
        print()
        total_r = con.execute("SELECT COUNT(*) FROM rechnungen").fetchone()[0]
        total_d = con.execute("SELECT COUNT(*) FROM dokumente").fetchone()[0]
        print(f"  Dokumente gesamt:  {total_d}")
        print(f"  Rechnungen gesamt: {total_r}")


if __name__ == "__main__":
    main()
