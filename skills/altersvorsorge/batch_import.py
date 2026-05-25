#!/usr/bin/env python3
"""Batch-Import: Altersvorsorge-Dokumente aus 40 Finanzen in altersvorsorge.db.
Pre-filtert mit AV-Vertrags-Keywords bevor Ollama aufgerufen wird."""

import subprocess
import sys
import re
import sqlite3
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
FINANZEN = VAULT / "40 Finanzen"
ANLAGEN = VAULT / "Anlagen"
ANALYZE = Path.home() / ".claude/skills/altersvorsorge/analyze.py"
DB_PATH = Path.home() / ".claude/skills/altersvorsorge/altersvorsorge.db"
LOG = Path.home() / ".claude/skills/altersvorsorge/batch_import.log"
TIMEOUT = 360

AV_KEYWORDS = [
    r"axa.*lebensversicherung", r"axa colonia", r"20412486",
    r"nürnberger", r"nuernberger",
    r"l\s*7087352", r"l\s*5929705", r"l\s*8087353", r"l\s*5087350",
    r"direktversicherung", r"pensionskasse", r"unterstützungskasse",
    r"lv\s*1871", r"73\s*088\s*025", r"basisrente",
    r"hdi.*fondsgebunden", r"hdi.*rente",
    r"allvest",
    r"standmitteilung", r"stand der versicherung",
    r"jahresinformation", r"wertmitteilung",
    r"rückkaufswert", r"ablaufleistung",
]

def is_av_document(text: str) -> bool:
    t = text.lower()
    return any(re.search(pat, t) for pat in AV_KEYWORDS)

def extract_original(md_path: Path) -> str | None:
    try:
        text = md_path.read_text(encoding="utf-8")
        for line in text.split("\n"):
            if line.startswith("original:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None

def already_done(pdf_name: str) -> bool:
    try:
        con = sqlite3.connect(str(DB_PATH))
        for table in ["standmitteilungen", "aenderungen"]:
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE quelle_pdf = ?", (pdf_name,)).fetchone()[0]
            if n > 0:
                con.close()
                return True
        con.close()
    except Exception:
        pass
    return False

def main():
    md_files = sorted(FINANZEN.rglob("*.md"))
    total = len(md_files)
    done, errors, skipped, db_skip, no_match = 0, 0, 0, 0, 0

    print(f"AV Batch-Import: {total} MD-Dateien in 40 Finanzen/")
    print(f"Log: {LOG}")

    with open(LOG, "a") as log:
        log.write(f"\n=== AV Batch-Import gestartet ===\nTotal: {total}\n\n")

    for i, md_path in enumerate(md_files):
        pdf_name = extract_original(md_path)
        if not pdf_name:
            skipped += 1
            continue

        if already_done(pdf_name):
            db_skip += 1
            continue

        pdf_path = ANLAGEN / pdf_name
        if not pdf_path.exists():
            pdf_path = Path(pdf_name)
            if not pdf_path.exists():
                skipped += 1
                continue

        md_text = ""
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except Exception:
            pass

        if not is_av_document(md_text):
            no_match += 1
            continue

        print(f"[{i+1}/{total}] {pdf_name[:60]} ...", end=" ", flush=True)
        try:
            result = subprocess.run(
                ["python3", str(ANALYZE), "pdf", str(pdf_path)],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            errors += 1
            with open(LOG, "a") as log:
                log.write(f"[{i+1}/{total}] {pdf_name} TIMEOUT\n\n")
            continue
        except Exception as e:
            print(f"EXC ({e})")
            errors += 1
            continue

        with open(LOG, "a") as log:
            log.write(f"[{i+1}/{total}] {pdf_name}\n{result.stdout}{result.stderr}\n\n")

        if result.returncode == 0 and ("1 neu" in result.stdout or "1 neu" in result.stderr):
            print("OK")
            done += 1
        elif "0 neu" in result.stdout or "bereits vorhanden" in result.stdout:
            print("dup")
            skipped += 1
        else:
            print(f"ERR ({result.returncode})")
            errors += 1

    print(f"\nFertig: {done} neu, {skipped} uebersprungen, {db_skip} in DB, {no_match} kein AV-Match, {errors} Fehler")
    with open(LOG, "a") as log:
        log.write(f"\n=== Fertig: {done} neu, {skipped} uebersprungen, {db_skip} in DB, {no_match} kein Match, {errors} Fehler ===\n")

if __name__ == "__main__":
    main()
