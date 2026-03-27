import os
import re
import json
import time
import logging
import shutil
import requests
from datetime import datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

WATCH_DIR      = Path(os.environ.get("WATCH_DIR",      "/data/input-docs"))
OUTPUT_DIR     = Path(os.environ.get("OUTPUT_DIR",     "/data/obsidian-vault/Converted"))
DOCLING_URL    = os.environ.get("DOCLING_URL",          "http://docling-serve:5001")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",           "http://ollama:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",         "qwen2.5:7b")
WEBUI_URL      = os.environ.get("WEBUI_URL",            "http://open-webui:8080")
WEBUI_API_KEY  = os.environ.get("WEBUI_API_KEY",        "")
KNOWLEDGE_NAME = os.environ.get("KNOWLEDGE_NAME",       "Vault")
ORIGINALS_DIR  = Path(os.environ.get("ORIGINALS_DIR",  "/data/obsidian-vault/Originale"))
SUPPORTED      = {".pdf", ".docx", ".doc", ".pptx", ".html"}

DONE_DIR = WATCH_DIR / "_processed"
DONE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)

_knowledge_id = None

ANALYSIS_PROMPT = """Analysiere das folgende Dokument und antworte NUR mit einem JSON-Objekt.

Extrahiere:
- datum: Dokumentdatum als YYYYMMDD (falls nicht gefunden: heute)
- absender: Firmen- oder Personenname ohne Rechtsform (GmbH, AG, etc.)
- thema: Betreff in max. 5 Wörtern
- kategorie: eine von [Rechnung, Erstattung, Versicherung, Arztbrief, Vertrag, Korrespondenz, Finanzen, Sonstiges]
- tags: Liste von 3-5 relevanten Tags (Firmenname, Thema, Jahr)
- zusammenfassung: 2-3 Sätze Zusammenfassung auf Deutsch
- todos: Liste offener Aufgaben (leer wenn keine)
- betrag: Geldbetrag falls vorhanden, sonst null (z.B. "1.271,06 EUR")
- faellig: Zahlungstermin als YYYY-MM-DD falls vorhanden, sonst null

Antworte AUSSCHLIESSLICH mit validem JSON, kein Text davor oder danach.

Dokument:
{content}"""


# ── Ollama ────────────────────────────────────────────────────────────────────

def analyze_with_ollama(md_content: str) -> dict:
    """Sends markdown content to Ollama for metadata extraction. Returns dict."""
    truncated = md_content[:6000]
    prompt = ANALYSIS_PROMPT.format(content=truncated)
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        if not r.ok:
            log.warning(f"Ollama Fehler {r.status_code}: {r.text[:200]}")
            return {}
        raw = r.json().get("response", "")
        # Extract JSON from response (handle markdown code blocks)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        log.warning(f"Kein JSON in Ollama-Antwort: {raw[:200]}")
        return {}
    except Exception as e:
        log.warning(f"Ollama Analyse fehlgeschlagen: {e}")
        return {}


def build_frontmatter(meta: dict, source_file: str, erstellt: str = None) -> str:
    """Creates YAML frontmatter from Ollama analysis result."""
    today = datetime.now().strftime("%Y-%m-%d")
    datum_raw = str(meta.get("datum", "")).strip()
    if re.match(r"^\d{8}$", datum_raw):
        datum = f"{datum_raw[:4]}-{datum_raw[4:6]}-{datum_raw[6:]}"
    else:
        datum = today

    tags = meta.get("tags", [])
    if isinstance(tags, list):
        tags_yaml = "\n" + "\n".join(f"  - {t}" for t in tags)
    else:
        tags_yaml = f"\n  - {tags}"

    todos = meta.get("todos", [])
    todos_yaml = ""
    if todos:
        todos_yaml = "\ntodos:\n" + "\n".join(f"  - {t}" for t in todos)

    betrag = meta.get("betrag") or ""
    faellig = meta.get("faellig") or ""

    lines = [
        "---",
        f"datum: {datum}",
        f"absender: {meta.get('absender', '')}",
        f"thema: {meta.get('thema', '')}",
        f"kategorie: {meta.get('kategorie', 'Sonstiges')}",
        f"tags:{tags_yaml}",
        "zusammenfassung: \"" + meta.get('zusammenfassung', '').replace('"', "'") + "\"",
    ]
    if betrag:
        lines.append(f"betrag: \"{betrag}\"")
    if faellig:
        lines.append(f"faellig: {faellig}")
    lines.append(f"quelle: {source_file}")
    lines.append(f"original: \"[[Originale/{source_file}]]\"")
    lines.append(f"erstellt: {erstellt or today}")
    lines.append(f"geaendert: {today}")
    if todos_yaml:
        lines.append(todos_yaml.strip())
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ── Open WebUI Knowledge ──────────────────────────────────────────────────────

def get_knowledge_id():
    global _knowledge_id
    if _knowledge_id:
        return _knowledge_id
    if not WEBUI_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {WEBUI_API_KEY}"}
    try:
        r = requests.get(f"{WEBUI_URL}/api/v1/knowledge/", headers=headers, timeout=10)
        if r.ok:
            for item in r.json().get("items", []):
                if item["name"] == KNOWLEDGE_NAME:
                    _knowledge_id = item["id"]
                    return _knowledge_id
    except Exception as e:
        log.warning(f"Knowledge API nicht erreichbar: {e}")
    return None


def ingest_to_knowledge(md_file: Path):
    if not WEBUI_API_KEY:
        return
    kb_id = get_knowledge_id()
    if not kb_id:
        log.warning("Knowledge base nicht gefunden, überspringe Ingest.")
        return
    headers = {"Authorization": f"Bearer {WEBUI_API_KEY}"}
    try:
        content = md_file.read_text(encoding="utf-8", errors="ignore")
        r = requests.post(f"{WEBUI_URL}/api/v1/files/", headers=headers,
                          files={"file": (md_file.name, content.encode("utf-8"), "text/plain")},
                          timeout=60)
        if not r.ok:
            log.error(f"Upload fehlgeschlagen {md_file.name}: {r.text[:200]}")
            return
        file_id = r.json()["id"]
        r2 = requests.post(f"{WEBUI_URL}/api/v1/knowledge/{kb_id}/file/add",
                           headers=headers, json={"file_id": file_id}, timeout=60)
        if r2.ok:
            log.info(f"RAG-Ingest OK: {md_file.name}")
        else:
            log.warning(f"Knowledge-Add fehlgeschlagen {md_file.name}: {r2.text[:200]}")
    except Exception as e:
        log.error(f"Ingest-Fehler {md_file.name}: {e}")


# ── Docling ───────────────────────────────────────────────────────────────────

def wait_for_file_stable(path: Path, timeout=30) -> bool:
    last_size = -1
    for _ in range(timeout):
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return False
        if current_size == last_size and current_size > 0:
            return True
        last_size = current_size
        time.sleep(1)
    return False


def wait_for_docling(max_retries=30, delay=10):
    for i in range(max_retries):
        try:
            r = requests.get(f"{DOCLING_URL}/health", timeout=5)
            if r.status_code == 200:
                log.info("Docling Serve ist erreichbar.")
                return True
        except requests.exceptions.ConnectionError:
            pass
        log.info(f"Warte auf Docling Serve... ({i+1}/{max_retries})")
        time.sleep(delay)
    return False


def convert_document(file_path: Path) -> bool:
    log.info(f"Konvertiere: {file_path.name}")
    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                f"{DOCLING_URL}/v1/convert/file",
                files={"files": (file_path.name, f, "application/octet-stream")},
                data={"to_formats": "md", "image_export_mode": "placeholder"},
                timeout=600,
            )

        if response.status_code != 200:
            log.error(f"Docling Fehler {response.status_code}: {response.text[:200]}")
            return False

        result = response.json()
        document = result.get("document", {})
        if not document or result.get("status") != "success":
            log.error(f"Kein Dokument in Antwort für {file_path.name}")
            return False

        md_content = document.get("md_content", "")

        # ── Ollama Analyse ──
        log.info(f"Analysiere mit Ollama: {file_path.name}")
        meta = analyze_with_ollama(md_content)

        # Bestehendes erstellt-Datum erhalten (falls Datei bereits existiert)
        erstellt = None
        stem_candidate = file_path.stem
        existing = OUTPUT_DIR / f"{stem_candidate}.md"
        if existing.exists():
            for line in existing.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("erstellt:"):
                    erstellt = line.split(":", 1)[1].strip()
                    break

        # ── Dateiname aus Metadaten ableiten ──
        stem = file_path.stem
        if meta:
            datum = str(meta.get("datum", "")).strip()
            absender = meta.get("absender", "").strip()
            thema = meta.get("thema", "").strip()
            if re.match(r"^\d{8}$", datum) and absender:
                clean = re.sub(r"[^\w\s-]", "", f"{absender} {thema}").strip()
                clean = re.sub(r"\s+", "_", clean)[:50]
                stem = f"{datum}_{clean}"

        # Neuer Originalname (gleicher stem, originale Extension)
        new_orig_name = f"{stem}{file_path.suffix.lower()}"

        if meta:
            frontmatter = build_frontmatter(meta, new_orig_name, erstellt=erstellt)
            log.info(f"Frontmatter erstellt: kategorie={meta.get('kategorie')}, absender={meta.get('absender')}")
            final_content = frontmatter + md_content
        else:
            log.warning(f"Ollama-Analyse fehlgeschlagen, speichere ohne Frontmatter: {file_path.name}")
            final_content = md_content

        # ── Link zum Original einfügen ──
        orig_link = f"\n> [Original: {new_orig_name}](../Originale/{new_orig_name})\n"
        lines = final_content.split("\n")
        insert_pos = next((i + 1 for i, l in enumerate(lines) if l.startswith("#")), 0)
        lines.insert(insert_pos, orig_link)
        final_content = "\n".join(lines)

        out_file = OUTPUT_DIR / f"{stem}.md"
        counter = 1
        while out_file.exists():
            out_file = OUTPUT_DIR / f"{stem}_{counter}.md"
            counter += 1

        out_file.write_text(final_content, encoding="utf-8")
        log.info(f"Gespeichert: {out_file.name}")

        ingest_to_knowledge(out_file)
        return stem

    except requests.exceptions.Timeout:
        log.error(f"Timeout bei Konvertierung von {file_path.name}")
        return None
    except Exception as e:
        log.error(f"Fehler bei {file_path.name}: {e}")
        return None


# ── File processing ───────────────────────────────────────────────────────────

def process_file(file_path: Path):
    if file_path.suffix.lower() not in SUPPORTED:
        return
    if ORIGINALS_DIR in file_path.parents or DONE_DIR in file_path.parents:
        return

    log.info(f"Neue Datei erkannt: {file_path.name}")
    if not wait_for_file_stable(file_path):
        log.warning(f"Datei nicht stabil: {file_path.name}")
        return

    stem = convert_document(file_path)
    if stem:
        new_orig_name = f"{stem}{file_path.suffix.lower()}"
        dest = ORIGINALS_DIR / new_orig_name
        counter = 1
        while dest.exists():
            dest = ORIGINALS_DIR / f"{stem}_{counter}{file_path.suffix.lower()}"
            counter += 1
        shutil.move(str(file_path), str(dest))
        log.info(f"Original archiviert als: {dest.name}")
    else:
        log.warning(f"Verarbeitung fehlgeschlagen, Datei bleibt: {file_path.name}")


class DocumentHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            process_file(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            process_file(Path(event.dest_path))


def main():
    log.info(f"Watcher startet. Überwache: {WATCH_DIR}")
    log.info(f"Output:  {OUTPUT_DIR}")
    log.info(f"Docling: {DOCLING_URL}")
    log.info(f"Ollama:  {OLLAMA_URL} ({OLLAMA_MODEL})")

    if not wait_for_docling():
        log.error("Docling Serve nicht erreichbar. Beende.")
        raise SystemExit(1)

    log.info("Prüfe bestehende Dateien im Eingangsordner...")
    for f in WATCH_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in SUPPORTED:
            process_file(f)

    observer = Observer()
    observer.schedule(DocumentHandler(), str(WATCH_DIR), recursive=False)
    observer.start()
    log.info("Watcher aktiv.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
