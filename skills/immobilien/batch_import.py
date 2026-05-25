#!/usr/bin/env python3
"""Batch-Import: Immobilien-Dokumente aus 50 Immobilien in immobilien.db."""

import subprocess
import sys
import sqlite3
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
IMMOBILIEN = VAULT / "50 Immobilien"
ANLAGEN = VAULT / "Anlagen"
ANALYZE = Path.home() / ".claude/skills/immobilien/analyze.py"
DB_PATH = Path.home() / ".claude/skills/immobilien/immobilien.db"
LOG = Path.home() / ".claude/skills/immobilien/batch_import.log"
TIMEOUT = 360

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
        n = con.execute("SELECT COUNT(*) FROM dokumente WHERE quelle_pdf = ?", (pdf_name,)).fetchone()[0]
        con.close()
        return n > 0
    except Exception:
        return False

def main():
    md_files = sorted(IMMOBILIEN.rglob("*.md"))
    total = len(md_files)
    done, errors, skipped, db_skip = 0, 0, 0, 0

    print(f"Immo Batch-Import: {total} MD-Dateien in 50 Immobilien/")
    print(f"Log: {LOG}")

    with open(LOG, "a") as log:
        log.write(f"\n=== Immo Batch-Import gestartet ===\nTotal: {total}\n\n")

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

    print(f"\nFertig: {done} neu, {skipped} uebersprungen, {db_skip} in DB, {errors} Fehler")
    with open(LOG, "a") as log:
        log.write(f"\n=== Fertig: {done} neu, {skipped} uebersprungen, {db_skip} in DB, {errors} Fehler ===\n")

if __name__ == "__main__":
    main()
