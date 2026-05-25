#!/usr/bin/env python3
"""Batch-Import: Alle KFZ-Dokumente aus dem Vault in kfz.db importieren."""

import subprocess
import sys
import sqlite3
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
KFZ_FOLDER = VAULT / "60 Fahrzeuge"
ANLAGEN = VAULT / "Anlagen"
ANALYZE = Path.home() / ".claude/skills/kfz/analyze.py"
DB_PATH = Path.home() / ".claude/skills/kfz/kfz.db"
LOG = Path.home() / ".claude/skills/kfz/batch_import.log"
TIMEOUT = 360  # 6 min pro PDF (Ollama 300s + pdftotext)

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
        for table in ["versicherungen", "schaeden", "reparaturen", "steuern", "zulassungen"]:
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE quelle_pdf = ?", (pdf_name,)).fetchone()[0]
            if n > 0:
                con.close()
                return True
        con.close()
    except Exception:
        pass
    return False

def main():
    md_files = sorted(KFZ_FOLDER.rglob("*.md"))
    total = len(md_files)
    done, errors, skipped, db_skip = 0, 0, 0, 0

    print(f"KFZ Batch-Import: {total} MD-Dateien gefunden")
    print(f"Log: {LOG}")

    with open(LOG, "a") as log:
        log.write(f"\n=== Batch-Import gestartet ===\n")
        log.write(f"Total: {total} MD-Dateien\n\n")

    for i, md_path in enumerate(md_files):
        pdf_name = extract_original(md_path)
        if not pdf_name:
            skipped += 1
            continue

        # Skip if already in DB
        if already_done(pdf_name):
            db_skip += 1
            continue

        pdf_path = ANLAGEN / pdf_name
        if not pdf_path.exists():
            pdf_path = Path(pdf_name)
            if not pdf_path.exists():
                with open(LOG, "a") as log:
                    log.write(f"SKIP [{pdf_name}]: PDF nicht gefunden\n")
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

        if result.returncode == 0 and "1 neu" in result.stdout:
            print("OK")
            done += 1
        elif "0 neu" in result.stdout or "bereits vorhanden" in result.stdout:
            print("dup")
            skipped += 1
        else:
            print(f"ERR ({result.returncode})")
            errors += 1

    print(f"\nFertig: {done} neu, {skipped} uebersprungen, {db_skip} bereits in DB, {errors} Fehler")
    with open(LOG, "a") as log:
        log.write(f"\n=== Fertig: {done} neu, {skipped} uebersprungen, {db_skip} in DB, {errors} Fehler ===\n")

if __name__ == "__main__":
    main()
