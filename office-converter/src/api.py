import os, subprocess, json, time, threading, logging
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Office Converter")
log = logging.getLogger("office-converter")

INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/data/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
VAULT_ANLAGEN = Path(os.environ.get("VAULT_ANLAGEN", "/vault-anlagen"))
PORT = int(os.environ.get("HTTP_PORT", "8502"))

OFFICE_EXTS = {".pptx", ".docx", ".xlsx"}

# --- Batch state ---
_batch_state = {"status": "idle", "files": [], "results": []}
_batch_lock = threading.Lock()

class ConvertRequest(BaseModel):
    path: str  # Pfad relativ zu INPUT_DIR

class BatchStartRequest(BaseModel):
    paths: Optional[list[str]] = None
    skip_existing: bool = True

# --- Helper ---

def has_pdf(office_path: Path) -> bool:
    """Prueft ob gleichnamiges PDF im gleichen Ordner existiert."""
    return office_path.with_suffix(".pdf").exists()

def convert_to_pdf(input_path: Path, output_dir: Path) -> Optional[Path]:
    """LibreOffice headless: Office → PDF."""
    cmd = [
        "libreoffice", "--headless", "--norestore",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(input_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log.error(f"LO Fehler {input_path.name}: {result.stderr[:500]}")
            return None
        pdf_path = output_dir / input_path.with_suffix(".pdf").name
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            log.info(f"PDF erstellt: {pdf_path.name} ({pdf_path.stat().st_size} bytes)")
            return pdf_path
        log.error(f"PDF nicht gefunden/leer nach Konvertierung: {input_path.name}")
        return None
    except subprocess.TimeoutExpired:
        log.error(f"LO Timeout bei {input_path.name}")
        return None

def find_office_files(directory: Path) -> list[Path]:
    """Findet alle Office-Dateien rekursiv."""
    files = []
    for ext in OFFICE_EXTS:
        files.extend(directory.rglob(f"*{ext}"))
    return sorted(files)

# --- Endpunkte ---

@app.get("/health")
def health():
    """Prueft ob LibreOffice verfuegbar ist."""
    try:
        r = subprocess.run(["libreoffice", "--version"], capture_output=True, text=True, timeout=10)
        return {"status": "ok", "version": r.stdout.strip()}
    except Exception as e:
        raise HTTPException(503, f"LibreOffice nicht verfuegbar: {e}")

@app.post("/convert")
def convert_single(req: ConvertRequest):
    """Einzelne Office-Datei nach PDF konvertieren."""
    input_path = INPUT_DIR / req.path
    if not input_path.exists():
        raise HTTPException(404, f"Datei nicht gefunden: {req.path}")
    if input_path.suffix.lower() not in OFFICE_EXTS:
        raise HTTPException(400, f"Kein Office-Format: {input_path.suffix}")

    pdf = convert_to_pdf(input_path, OUTPUT_DIR)
    if not pdf:
        raise HTTPException(500, "Konvertierung fehlgeschlagen")

    return {
        "status": "converted",
        "input": str(input_path.name),
        "output": str(pdf.name),
        "pdf_path": str(pdf),
        "groesse": pdf.stat().st_size
    }

@app.get("/batch/scan")
def batch_scan():
    """Scannt INPUT_DIR und zeigt was konvertiert wuerde (Dry-Run)."""
    files = find_office_files(INPUT_DIR)
    result = []
    for f in files:
        rel = str(f.relative_to(INPUT_DIR))
        result.append({
            "file": rel,
            "typ": f.suffix[1:].upper(),
            "groesse": f.stat().st_size,
            "hat_pdf_lokal": has_pdf(f),
            "hat_pdf_vault": (VAULT_ANLAGEN / f.with_suffix(".pdf").name).exists()
        })
    return {
        "total": len(result),
        "mit_pdf": sum(1 for r in result if r["hat_pdf_lokal"] or r["hat_pdf_vault"]),
        "ohne_pdf": sum(1 for r in result if not r["hat_pdf_lokal"] and not r["hat_pdf_vault"]),
        "files": result
    }

@app.post("/batch/start")
def batch_start(req: BatchStartRequest = BatchStartRequest()):
    """Startet Batch-Konvertierung im Hintergrund."""
    global _batch_state
    with _batch_lock:
        if _batch_state["status"] == "running":
            raise HTTPException(409, "Batch laeuft bereits")
        _batch_state = {"status": "running", "files": [], "results": [],
                        "started": datetime.now().isoformat()}

    files = req.paths or [str(p.relative_to(INPUT_DIR)) for p in find_office_files(INPUT_DIR)]

    thread = threading.Thread(target=_batch_runner,
                              args=(files, req.skip_existing), daemon=True)
    thread.start()
    return {"status": "started", "total": len(files), "skip_existing": req.skip_existing}

@app.get("/batch/status")
def batch_status():
    """Aktueller Batch-Status."""
    with _batch_lock:
        return _batch_state.copy()

def _batch_runner(file_list: list[str], skip_existing: bool):
    global _batch_state
    results = []
    total = len(file_list)
    for i, rel_path in enumerate(file_list):
        input_path = INPUT_DIR / rel_path
        log.info(f"[{i+1}/{total}] Verarbeite: {rel_path}")

        if not input_path.exists():
            results.append({"file": rel_path, "status": "error", "error": "nicht_gefunden"})
            continue

        # Skip-Check: PDF im gleichen Ordner?
        if skip_existing and has_pdf(input_path):
            log.info(f"  Uebersprungen (PDF existiert lokal): {rel_path}")
            results.append({"file": rel_path, "status": "skipped", "reason": "pdf_existiert"})
            continue

        # Skip-Check: PDF im Vault?
        vault_check = VAULT_ANLAGEN / input_path.with_suffix(".pdf").name
        if skip_existing and vault_check.exists():
            log.info(f"  Uebersprungen (PDF im Vault): {rel_path}")
            results.append({"file": rel_path, "status": "skipped", "reason": "pdf_in_vault"})
            continue

        # Konvertieren
        pdf = convert_to_pdf(input_path, OUTPUT_DIR)
        if pdf:
            results.append({
                "file": rel_path,
                "status": "converted",
                "pdf": str(pdf.name),
                "pdf_path": str(pdf),
                "groesse": pdf.stat().st_size
            })
        else:
            results.append({"file": rel_path, "status": "error", "error": "konvertierung_fehlgeschlagen"})

        with _batch_lock:
            _batch_state["results"] = list(results)
            _batch_state["converted"] = sum(1 for r in results if r["status"] == "converted")
            _batch_state["skipped"] = sum(1 for r in results if r["status"] == "skipped")
            _batch_state["errors"] = sum(1 for r in results if r["status"] == "error")

    with _batch_lock:
        _batch_state["status"] = "completed"
        _batch_state["finished"] = datetime.now().isoformat()
        _batch_state["results"] = results

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    log.info(f"Office Converter startet auf Port {PORT}")
    log.info(f"INPUT_DIR={INPUT_DIR} OUTPUT_DIR={OUTPUT_DIR} VAULT_ANLAGEN={VAULT_ANLAGEN}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
