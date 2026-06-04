#!/usr/bin/env python3
"""
Klassifiziert und verschiebt PDFs aus Anlagen/ in richtige Vault-Ordner.
Läuft nachtweise, 25 PDFs pro Durchlauf.

Erweiterungen 2026-05-16:
- DB-Check vor Verarbeitung: überspringt PDFs die bereits in dispatcher.db sind (Hash oder Dateiname)
- DB-Eintrag nach Verarbeitung: schreibt verarbeitete Dokumente in dispatcher.db
- Fallback-Datum: Datei-mtime statt 0000-00-00 wenn kein YYYYMMDD im Dateinamen
"""
import hashlib, os, re, json, shutil, sqlite3, requests, logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

VAULT      = Path(os.getenv("VAULT_ROOT",   "/data/reinhards-vault"))
ANLAGEN    = VAULT / "Anlagen"
DOCLING    = os.getenv("DOCLING_URL",       "http://docling-serve:5001")
OLLAMA     = os.getenv("OLLAMA_URL",        "http://ollama:11434")
MODEL      = os.getenv("OLLAMA_MODEL",      "qwen3:4b-instruct")
DB_PATH    = Path(os.getenv("DISPATCHER_DB", "/data/dispatcher-temp/dispatcher.db"))
BATCH      = 25
PROGRESS   = Path("/data/dispatcher-temp/anlagen_processor_progress.json")
MAX_CHARS  = 2500

CATEGORY_FOLDER = {
    "persoenlich":          "10 Persönlich",
    "familie":              "20 Familie",
    "fengshui":             "30 FengShui",
    "finanzen":             "40 Finanzen",
    "krankenversicherung":  "49 Krankenversicherung",
    "immobilien_italien":   "50 Immobilien eigen",
    "immobilien_vermietet": "51 Immobilien vermietet",
    "garten":               "55 Garten",
    "fahrzeuge":            "60 Fahrzeuge",
    "italien":              "70 Italien",
    "business":             "80 Business",
    "digitales":            "82 Digitales",
    "wissen":               "85 Wissen",
    "reisen":               "90 Reisen",
    "bedienungsanleitung":  "95 Bedienungsanleitungen",
    "archiv":               "99 Archiv",
}
CATEGORY_TAG = {
    "persoenlich":          "Persönlich",
    "familie":              "Familie",
    "fengshui":             "FengShui",
    "finanzen":             "Finanzen",
    "krankenversicherung":  "Krankenversicherung",
    "immobilien_italien":   "Finanzen/Immobilien",
    "immobilien_vermietet": "Finanzen/Immobilien",
    "garten":               "Garten",
    "fahrzeuge":            "Fahrzeuge",
    "italien":              "Italien",
    "business":             "Business",
    "digitales":            "Digitales",
    "wissen":               "Wissen",
    "reisen":               "Reisen",
    "bedienungsanleitung":  "Bedienungsanleitung",
    "archiv":               "Archiv",
}
CATEGORIES_TEXT = "\n".join(
    f"- {k}: {v.replace('/', ' / ')}"
    for k, v in CATEGORY_FOLDER.items()
)

PROMPT = """Klassifiziere dieses Dokument in GENAU EINE Kategorie. Antworte NUR mit der Kategorie-ID, ohne Erklärung.

Kategorien:
{cats}

Dokument (Auszug):
{content}"""


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text())
    return {"done": [], "failed": {}, "total": 0, "last_file": ""}

def save_progress(p):
    PROGRESS.write_text(json.dumps(p, ensure_ascii=False, indent=2))

def valid_date(s):
    try:
        datetime.strptime(s, "%Y%m%d")
        return 1900 <= int(s[:4]) <= 2030
    except:
        return False

def iso_from_stem(stem, fallback_mtime=None):
    d = stem[:8]
    if valid_date(d):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    if fallback_mtime:
        return datetime.fromtimestamp(fallback_mtime).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")

def target_dir(category, iso_date):
    folder = CATEGORY_FOLDER.get(category, "99 Archiv")
    year   = iso_date[:4]
    if year == datetime.now().strftime("%Y"):
        return VAULT / folder
    return VAULT / folder / year

def stub_has_unverarbeitet(md_path):
    try:
        t = md_path.read_text(errors="replace")
        return "Anlagen/Unverarbeitet" in t
    except:
        return False

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Dispatcher-DB ─────────────────────────────────────────────────────────────

def db_connect():
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con

def db_already_known(pdf_path, pdf_hash):
    """True wenn PDF per Hash oder Dateiname bereits in dispatcher.db eingetragen ist."""
    con = db_connect()
    if not con:
        return False
    try:
        row = con.execute(
            "SELECT id FROM dokumente WHERE pdf_hash = ? OR dateiname = ?",
            (pdf_hash, pdf_path.name),
        ).fetchone()
        return row is not None
    finally:
        con.close()

def db_insert(pdf_path, pdf_hash, category, vault_pfad, anlagen_dateiname):
    """Trägt verarbeitetes Dokument in dispatcher.db ein."""
    con = db_connect()
    if not con:
        log.warning("DB nicht erreichbar — kein DB-Eintrag für %s", pdf_path.name)
        return
    try:
        con.execute(
            """INSERT OR IGNORE INTO dokumente
               (dateiname, pdf_hash, kategorie, vault_pfad, anlagen_dateiname, erstellt_am)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (pdf_path.name, pdf_hash, category, vault_pfad, anlagen_dateiname),
        )
        con.commit()
    finally:
        con.close()


# ── Verarbeitungs-Pipeline ────────────────────────────────────────────────────

def get_pending(progress):
    done_set   = set(progress["done"])
    failed_set = set(progress["failed"].keys())
    skip       = done_set | failed_set
    pending    = []
    for f in sorted(ANLAGEN.glob("*.pdf")):
        if f.name in skip:
            continue
        md = f.with_suffix(".md")
        if md.exists() and stub_has_unverarbeitet(md):
            pending.append(f)
        elif not md.exists():
            pending.append(f)
    return pending

def ocr_pdf(pdf_path):
    with open(pdf_path, "rb") as fh:
        resp = requests.post(
            f"{DOCLING}/v1/convert/file",
            files={"files": (pdf_path.name, fh, "application/pdf")},
            data={"image_export_mode": "placeholder"},
            timeout=300,
        )
    resp.raise_for_status()
    return resp.text[:MAX_CHARS]

def classify(content):
    prompt = PROMPT.format(cats=CATEGORIES_TEXT, content=content)
    resp = requests.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    raw = resp.json().get("response", "").strip().lower()
    for cid in CATEGORY_FOLDER:
        if cid in raw:
            return cid
    return "archiv"

def process_one(pdf_path, progress):
    stem     = pdf_path.stem
    mtime    = pdf_path.stat().st_mtime
    iso      = iso_from_stem(stem, fallback_mtime=mtime)
    pdf_hash = md5_file(pdf_path)

    # DB-Check: bereits bekanntes Dokument überspringen
    if db_already_known(pdf_path, pdf_hash):
        log.info(f"  Bereits in DB — überspringe: {pdf_path.name}")
        progress["done"].append(pdf_path.name)
        return

    log.info(f"OCR: {pdf_path.name}")
    try:
        content = ocr_pdf(pdf_path)
    except Exception as e:
        log.warning(f"  OCR fehlgeschlagen: {e}")
        progress["failed"][pdf_path.name] = f"OCR: {e}"
        return

    log.info(f"  Klassifiziere ...")
    try:
        category = classify(content)
    except Exception as e:
        log.warning(f"  Klassifizierung fehlgeschlagen: {e}")
        progress["failed"][pdf_path.name] = f"classify: {e}"
        return

    dst_dir = target_dir(category, iso)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_pdf = dst_dir / pdf_path.name
    if dst_pdf.exists():
        dst_pdf = dst_dir / (stem + "_2.pdf")
    shutil.copy2(str(pdf_path), str(dst_pdf))

    # Alten Stub löschen
    old_stub = pdf_path.with_suffix(".md")
    if old_stub.exists():
        old_stub.unlink()

    # Neue .md anlegen (VAULT_FRONTMATTER_SPEC-konform)
    tag      = CATEGORY_TAG.get(category, category)
    preview  = re.sub(r'\s+', ' ', content[:400]).strip()
    new_stub = dst_dir / (stem + ".md")
    vault_pfad = str(new_stub.relative_to(VAULT))
    new_stub.write_text(
        f"---\nDatum_original: {iso}\ntags:\n  - {tag}\n"
        f"original: Anlagen/{dst_pdf.name}\n---\n"
        f"📄 [[Anlagen/{dst_pdf.name}]]\n\n{preview}\n",
        encoding="utf-8",
    )

    # DB-Eintrag
    db_insert(pdf_path, pdf_hash, category, vault_pfad, dst_pdf.name)

    log.info(f"  → {category}  ({dst_dir.relative_to(VAULT)})")
    progress["done"].append(pdf_path.name)
    progress["last_file"] = pdf_path.name


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    progress        = load_progress()
    pending         = get_pending(progress)
    total_remaining = len(pending)
    total_all       = len(progress["done"]) + len(progress["failed"]) + total_remaining
    progress["total"] = total_all
    log.info(f"Anlagen-Prozessor gestartet — {total_remaining} PDFs ausstehend, verarbeite {BATCH} pro Lauf")

    done_this_run = 0
    for pdf in pending[:BATCH]:
        process_one(pdf, progress)
        save_progress(progress)
        done_this_run += 1

    remaining = total_remaining - done_this_run
    log.info(f"Lauf fertig: {done_this_run} verarbeitet, "
             f"{len(progress['failed'])} Fehler, ~{remaining} verbleibend")

if __name__ == "__main__":
    main()
