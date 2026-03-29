import os
import re
import json
import time
import queue
import logging
import shutil
import requests
import threading
from datetime import datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

WATCH_DIR      = Path(os.environ.get("WATCH_DIR",      "/data/input-docs"))
OUTPUT_DIR     = Path(os.environ.get("OUTPUT_DIR",     "/data/obsidian-vault/Converted"))
DOCLING_URL    = os.environ.get("DOCLING_URL",          "http://docling-serve:5001")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",           "http://ollama:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",         "qwen2.5:7b")
WEBUI_URL      = os.environ.get("WEBUI_URL",            "http://open-webui:8080")
WEBUI_API_KEY  = os.environ.get("WEBUI_API_KEY",        "")
ORIGINALS_DIR  = Path(os.environ.get("ORIGINALS_DIR",  "/data/obsidian-vault/Originale"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",  "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",    "")
SUPPORTED      = {".pdf", ".docx", ".doc", ".pptx", ".html"}

# ── Silo-Konfiguration ─────────────────────────────────────────────────────────

SILOS = {
    "finanzen":     {"privacy": "lokal",    "kategorien": ["Rechnung", "Kontoauszug", "Steuer", "Vertrag", "Finanzen", "Sonstiges"]},
    "krankenkasse": {"privacy": "lokal",    "kategorien": ["Leistungsabrechnung", "Rezept", "Hilfsmittel", "Arztrechnung", "Arztbrief", "Versicherung", "Anderes"]},
    "anleitungen":  {"privacy": "cloud-ok", "kategorien": ["Bedienungsanleitung", "Handbuch", "Datenblatt", "Sonstiges"]},
    "archiv":       {"privacy": "lokal",    "kategorien": ["Korrespondenz", "Vertrag", "Zeugnis", "Sonstiges"]},
    "projekte":     {"privacy": "lokal",    "kategorien": ["Planung", "Notiz", "Meeting", "Ergebnis", "Sonstiges"]},
    "inbox":        {"privacy": "lokal",    "kategorien": ["Sonstiges"]},
}

SILO_KATEGORIEN_PROMPT = {
    "finanzen":     "Rechnung | Kontoauszug | Steuer | Vertrag | Finanzen | Sonstiges",
    "krankenkasse": "Leistungsabrechnung | Rezept | Hilfsmittel | Arztrechnung | Arztbrief | Versicherung | Anderes",
    "anleitungen":  "Bedienungsanleitung | Handbuch | Datenblatt | Sonstiges",
    "archiv":       "Korrespondenz | Vertrag | Zeugnis | Sonstiges",
    "projekte":     "Planung | Notiz | Meeting | Ergebnis | Sonstiges",
    "inbox":        "Rechnung | Versicherung | Arztbrief | Anleitung | Korrespondenz | Sonstiges",
}

# ── Verzeichnisse anlegen ──────────────────────────────────────────────────────

for silo in SILOS:
    (OUTPUT_DIR / silo).mkdir(parents=True, exist_ok=True)
    (ORIGINALS_DIR / silo).mkdir(parents=True, exist_ok=True)
    (WATCH_DIR / silo).mkdir(parents=True, exist_ok=True)

DONE_DIR = WATCH_DIR / "_processed"
DONE_DIR.mkdir(exist_ok=True)

# ── Queue für sequenzielle Verarbeitung ────────────────────────────────────────

file_queue: queue.Queue = queue.Queue()

# ── Telegram ───────────────────────────────────────────────────────────────────

_tg_offset = 0

def tg_send(text: str, reply_markup: dict = None) -> int | None:
    """Sendet Telegram-Nachricht, gibt message_id zurück."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram nicht konfiguriert.")
        return None
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if r.ok:
            return r.json()["result"]["message_id"]
        log.warning(f"Telegram sendMessage Fehler: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}")
    return None


def tg_get_updates() -> list:
    """Holt neue Telegram-Updates (Long-Polling, 5s Timeout)."""
    global _tg_offset
    if not TELEGRAM_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _tg_offset, "timeout": 5, "allowed_updates": ["message", "callback_query"]},
            timeout=10
        )
        if r.ok:
            updates = r.json().get("result", [])
            if updates:
                _tg_offset = updates[-1]["update_id"] + 1
            return updates
    except Exception as e:
        log.debug(f"getUpdates Fehler: {e}")
    return []


def tg_answer_callback(callback_id: str):
    """Bestätigt Callback-Query (entfernt Ladesymbol beim Button)."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id}, timeout=5
        )
    except Exception:
        pass


def tg_wait_confirmation(filename: str, silo: str, meta: dict) -> tuple[str, str]:
    """
    Sendet Vorschlag via Telegram und wartet blockierend auf Bestätigung.
    Gibt (bestätigter_silo, bestätigte_kategorie) zurück.
    """
    kategorie = meta.get("kategorie", "Sonstiges")
    absender  = meta.get("absender", "?")
    thema     = meta.get("thema", "?")
    datum     = meta.get("datum", "?")
    betrag    = meta.get("betrag", "")
    faellig   = meta.get("faellig", "")

    # Nachrichtentext aufbauen
    lines = [
        f"📄 <b>{filename}</b>",
        f"",
        f"🗂 Silo:      <b>{silo}</b>",
        f"📁 Kategorie: <b>{kategorie}</b>",
        f"🏢 Absender:  {absender}",
        f"📝 Thema:     {thema}",
        f"📅 Datum:     {datum}",
    ]
    if betrag:
        lines.append(f"💰 Betrag:    {betrag}")
    if faellig:
        lines.append(f"⏰ Fällig:    {faellig}")
    lines += ["", "Bitte bestätigen oder korrigieren:"]

    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [[
            {"text": "✓ OK", "callback_data": "confirm"},
            {"text": "✎ Ändern", "callback_data": "change"},
        ]]
    }

    tg_send(text, reply_markup=keyboard)
    log.info(f"Warte auf Telegram-Bestätigung für: {filename}")

    # Zustandsmaschine
    state = "waiting_confirm"  # → "waiting_silo" → "waiting_kategorie" → "done"
    new_silo = silo
    new_kategorie = kategorie

    # Offset resetten um nur neue Nachrichten zu empfangen
    tg_get_updates()  # leert den Puffer

    while True:
        updates = tg_get_updates()
        for upd in updates:
            # Callback-Query (Button-Klick)
            if "callback_query" in upd:
                cq = upd["callback_query"]
                tg_answer_callback(cq["id"])
                data = cq.get("data", "")

                if state == "waiting_confirm":
                    if data == "confirm":
                        tg_send(f"✅ Bestätigt: {silo} / {kategorie}\nVerarbeitung startet...")
                        return silo, kategorie
                    elif data == "change":
                        silo_list = " | ".join(SILOS.keys())
                        tg_send(f"Welcher Silo?\n<code>{silo_list}</code>\nBitte eingeben:")
                        state = "waiting_silo"

            # Text-Nachricht
            elif "message" in upd:
                msg = upd["message"]
                # Nur Nachrichten von der konfigurierten Chat-ID
                if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT):
                    continue
                text_in = msg.get("text", "").strip().lower()

                if state == "waiting_silo":
                    if text_in in SILOS:
                        new_silo = text_in
                        kat_list = SILO_KATEGORIEN_PROMPT.get(new_silo, "Sonstiges")
                        tg_send(f"Silo: <b>{new_silo}</b>\nWelche Kategorie?\n<code>{kat_list}</code>\nBitte eingeben:")
                        state = "waiting_kategorie"
                    else:
                        tg_send(f"Unbekannter Silo. Bitte einen von:\n<code>{' | '.join(SILOS.keys())}</code>")

                elif state == "waiting_kategorie":
                    new_kategorie = msg.get("text", "Sonstiges").strip()
                    tg_send(f"✅ Geändert: <b>{new_silo}</b> / <b>{new_kategorie}</b>\nVerarbeitung startet...")
                    return new_silo, new_kategorie

        time.sleep(2)


# ── Ollama ─────────────────────────────────────────────────────────────────────

def build_analysis_prompt(silo: str, content: str) -> str:
    kategorien = SILO_KATEGORIEN_PROMPT.get(silo, "Sonstiges")
    if silo == "krankenkasse":
        extra = (
            "- patient: Name des Patienten falls erkennbar\n"
            "- krankenversicherung: Name der Krankenkasse falls vorhanden\n"
            "- betrag_erstattung: Erstattungsbetrag falls vorhanden\n"
            "- medikamente: Liste der Medikamentennamen falls Rezept (sonst null)\n"
            "- apotheke: Name der Apotheke falls Rezept (sonst null)\n"
            "\n"
            "Hinweise zur Kategorisierung:\n"
            "- 'Leistungsabrechnung': Dokument von HUK-COBURG oder Gothaer/Barmenia Versicherung mit Tabelle eingereichter Rechnungen und Erstattungsbeträgen.\n"
            "- 'Rezept': Kleinformatiges Dokument, ausgestellt von einem Arzt, enthält Medikamentennamen und Preis. Apotheke liefert es oft mit Stempel. Erkennungsworte: Rezept, Privatrezept, Arzneimittel, Verschreibung, Apotheke.\n"
            "- 'Hilfsmittel': Rechnung direkt vom Fachgeschäft (Sanitätshaus, Optiker, Orthopädie-Schuhtechnik) für medizinische Hilfsmittel wie Brille, Einlagen, Bandagen, Kompressionsstrümpfe, Orthesen, Hörgeräte. Absender ist typischerweise ein Fachgeschäft, kein Arzt und keine Versicherung.\n"
            "- 'Arztrechnung': Rechnung direkt vom Arzt, Krankenhaus, Labor, Pathologie, Radiologie oder medizinischen Versorgungszentrum (MVZ). Enthält Leistungsposten und einen Rechnungsbetrag.\n"
            "- 'Arztbrief': Arztbrief, Befundbericht, Diagnose-Dokument oder Korrespondenz zwischen Ärzten — ohne primären Rechnungscharakter.\n"
            "- 'Versicherung': Versicherungsschein, Vertragsunterlagen, Beitragsbescheinigung.\n"
            "- 'Anderes': Einwilligungserklärungen, Wahlleistungsvereinbarungen, Formulare und sonstige Dokumente die in keine andere Kategorie passen.\n"
        )
    elif silo == "finanzen":
        extra = "- betrag: Geldbetrag falls vorhanden (z.B. '342,50 EUR')\n- faellig: Zahlungstermin als YYYY-MM-DD falls vorhanden, sonst null\n- steuerrelevant: true/false\n"
    elif silo == "anleitungen":
        extra = "- hersteller: Hersteller/Marke\n- modell: Modell-/Produktbezeichnung\n- produkttyp: Art des Geräts\n"
    else:
        extra = "- betrag: Geldbetrag falls vorhanden, sonst null\n- faellig: Datum falls vorhanden, sonst null\n"

    return f"""Analysiere das folgende Dokument (Silo: {silo}) und antworte NUR mit einem JSON-Objekt.

Extrahiere:
- datum: Dokumentdatum als YYYYMMDD (falls nicht gefunden: heute)
- absender: Firmen- oder Personenname ohne Rechtsform
- thema: Betreff in max. 5 Wörtern
- kategorie: eine von [{kategorien}]
- tags: Liste von 3-5 relevanten Tags
- zusammenfassung: 2-3 Sätze auf Deutsch
- todos: Liste offener Aufgaben (leer wenn keine)
{extra}
Antworte AUSSCHLIESSLICH mit validem JSON, kein Text davor oder danach.

Dokument:
{content[:6000]}"""


def analyze_with_ollama(md_content: str, silo: str) -> dict:
    prompt = build_analysis_prompt(silo, md_content)
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        if not r.ok:
            log.warning(f"Ollama Fehler {r.status_code}")
            return {}
        raw = r.json().get("response", "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        log.warning(f"Kein JSON in Ollama-Antwort: {raw[:200]}")
        return {}
    except Exception as e:
        log.warning(f"Ollama Analyse fehlgeschlagen: {e}")
        return {}


def build_frontmatter(meta: dict, silo: str, kategorie: str, source_file: str, erstellt: str = None) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    datum_raw = str(meta.get("datum", "")).strip()
    if re.match(r"^\d{8}$", datum_raw):
        datum = f"{datum_raw[:4]}-{datum_raw[4:6]}-{datum_raw[6:]}"
    else:
        datum = today

    tags = meta.get("tags", [])
    tags_yaml = "\n" + "\n".join(f"  - {t}" for t in (tags if isinstance(tags, list) else [tags]))

    todos = meta.get("todos", [])
    todos_yaml = ("\ntodos:\n" + "\n".join(f"  - {t}" for t in todos)) if todos else ""

    lines = [
        "---",
        f"silo: {silo}",
        f"datum: {datum}",
        f"absender: {meta.get('absender', '')}",
        f"thema: {meta.get('thema', '')}",
        f"kategorie: {kategorie}",
        f"tags:{tags_yaml}",
        f"privacy: {SILOS.get(silo, {}).get('privacy', 'lokal')}",
        "zusammenfassung: \"" + meta.get('zusammenfassung', '').replace('"', "'") + "\"",
    ]

    # Silo-spezifische Felder
    for field in ["betrag", "faellig", "steuerrelevant", "patient", "krankenversicherung",
                  "betrag_erstattung", "medikamente", "apotheke", "hersteller", "modell", "produkttyp"]:
        val = meta.get(field)
        if val is not None and val != "":
            lines.append(f"{field}: {val}")

    lines.append(f"quelle: {source_file}")
    lines.append(f"original: \"[[Originale/{silo}/{source_file}]]\"")
    lines.append(f"erstellt: {erstellt or today}")
    lines.append(f"geaendert: {today}")
    if todos_yaml:
        lines.append(todos_yaml.strip())
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ── Open WebUI Knowledge ───────────────────────────────────────────────────────

_knowledge_ids: dict = {}

def get_knowledge_id(collection_name: str) -> str | None:
    if collection_name in _knowledge_ids:
        return _knowledge_ids[collection_name]
    if not WEBUI_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {WEBUI_API_KEY}"}
    try:
        r = requests.get(f"{WEBUI_URL}/api/v1/knowledge/", headers=headers, timeout=10)
        if r.ok:
            for item in r.json().get("items", []):
                if item["name"] == collection_name:
                    _knowledge_ids[collection_name] = item["id"]
                    return item["id"]
    except Exception as e:
        log.warning(f"Knowledge API nicht erreichbar: {e}")
    return None


def ingest_to_knowledge(md_file: Path, silo: str):
    if not WEBUI_API_KEY:
        return
    # Ingest in silo-spezifische Collection UND vault-all
    collections = [f"vault-{silo}", "vault-all"]
    headers = {"Authorization": f"Bearer {WEBUI_API_KEY}"}
    for coll_name in collections:
        kb_id = get_knowledge_id(coll_name)
        if not kb_id:
            log.warning(f"Collection '{coll_name}' nicht gefunden, überspringe.")
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            r = requests.post(
                f"{WEBUI_URL}/api/v1/files/",
                headers=headers,
                files={"file": (md_file.name, content.encode("utf-8"), "text/plain")},
                timeout=60
            )
            if not r.ok:
                log.error(f"Upload fehlgeschlagen {md_file.name} → {coll_name}: {r.text[:200]}")
                continue
            file_id = r.json()["id"]
            r2 = requests.post(
                f"{WEBUI_URL}/api/v1/knowledge/{kb_id}/file/add",
                headers=headers, json={"file_id": file_id}, timeout=60
            )
            if r2.ok:
                log.info(f"RAG-Ingest OK: {md_file.name} → {coll_name}")
            else:
                log.warning(f"Knowledge-Add fehlgeschlagen: {r2.text[:200]}")
        except Exception as e:
            log.error(f"Ingest-Fehler {md_file.name} → {coll_name}: {e}")


# ── Docling ────────────────────────────────────────────────────────────────────

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


def detect_silo(file_path: Path) -> str:
    """Ermittelt Silo anhand des Verzeichnisnamens."""
    for part in file_path.parts:
        if part in SILOS:
            return part
    return "inbox"


# Mapping von Kategorie → Unterordner (nur für Silos mit Unterordner-Routing)
KRANKENKASSE_SUBFOLDERS = {
    "leistungsabrechnung": "leistungsabrechnung",
    "rezept":              "rezept",
    "hilfsmittel":         "hilfsmittel",
    "arztrechnung":        "arztrechnung",
    "arztbrief":           "arztbrief",
    "versicherung":        "versicherung",
    "anderes":             "anderes",
}


def get_output_dir(silo: str, kategorie: str, datum: str) -> Path:
    """Gibt den Ausgabeordner zurück, für krankenkasse mit Kategorie/Jahr-Routing."""
    base = OUTPUT_DIR / silo
    if silo == "krankenkasse":
        kat_lower = (kategorie or "").lower()
        subfolder = KRANKENKASSE_SUBFOLDERS.get(kat_lower)
        if subfolder:
            year = datum[:4] if datum and re.match(r"^\d{4}", datum) else datetime.now().strftime("%Y")
            return base / subfolder / year
    return base


def convert_document(file_path: Path) -> bool:
    log.info(f"Konvertiere: {file_path.name}")
    silo = detect_silo(file_path)
    log.info(f"Erkannter Silo: {silo}")

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
        log.info(f"Analysiere mit Ollama (Silo: {silo}): {file_path.name}")
        meta = analyze_with_ollama(md_content, silo)

        # ── Telegram Bestätigung (blockierend) ──
        if TELEGRAM_TOKEN and TELEGRAM_CHAT:
            confirmed_silo, confirmed_kategorie = tg_wait_confirmation(file_path.name, silo, meta)
        else:
            confirmed_silo = silo
            confirmed_kategorie = meta.get("kategorie", "Sonstiges")
            log.warning("Telegram nicht konfiguriert — automatische Bestätigung.")

        # Kategorie in meta überschreiben
        meta["kategorie"] = confirmed_kategorie

        # ── Dateiname ableiten ──
        stem = file_path.stem
        if meta:
            datum = str(meta.get("datum", "")).strip()
            absender = re.sub(r"[^\w\s-]", "", meta.get("absender", "")).strip()
            thema = re.sub(r"[^\w\s-]", "", meta.get("thema", "")).strip()
            if re.match(r"^\d{8}$", datum) and absender:
                clean = re.sub(r"\s+", "_", f"{absender}_{thema}")[:50]
                stem = f"{datum}_{clean}"

        new_orig_name = f"{stem}{file_path.suffix.lower()}"

        # Bestehenden erstellt-Wert erhalten
        erstellt = None
        existing = OUTPUT_DIR / confirmed_silo / f"{stem}.md"
        if existing.exists():
            for line in existing.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("erstellt:"):
                    erstellt = line.split(":", 1)[1].strip()
                    break

        frontmatter = build_frontmatter(meta, confirmed_silo, confirmed_kategorie, new_orig_name, erstellt)
        orig_link = f"\n> [Original: {new_orig_name}](../Originale/{confirmed_silo}/{new_orig_name})\n"
        lines = (frontmatter + md_content).split("\n")
        insert_pos = next((i + 1 for i, l in enumerate(lines) if l.startswith("#")), 0)
        lines.insert(insert_pos, orig_link)
        final_content = "\n".join(lines)

        # ── Ausgabepfad in Silo-Unterordner (mit Kategorie/Jahr-Routing) ──
        datum_str = str(meta.get("datum", "")).strip()
        silo_out = get_output_dir(confirmed_silo, confirmed_kategorie, datum_str)
        silo_out.mkdir(parents=True, exist_ok=True)
        out_file = silo_out / f"{stem}.md"
        counter = 1
        while out_file.exists():
            out_file = silo_out / f"{stem}_{counter}.md"
            counter += 1

        out_file.write_text(final_content, encoding="utf-8")
        log.info(f"Gespeichert: {out_file}")

        ingest_to_knowledge(out_file, confirmed_silo)
        return stem, confirmed_silo

    except requests.exceptions.Timeout:
        log.error(f"Timeout bei Konvertierung von {file_path.name}")
        return None, None
    except Exception as e:
        log.error(f"Fehler bei {file_path.name}: {e}")
        return None, None


# ── Dateiverarbeitung ──────────────────────────────────────────────────────────

def process_file(file_path: Path):
    if file_path.suffix.lower() not in SUPPORTED:
        return
    if ORIGINALS_DIR in file_path.parents or DONE_DIR in file_path.parents:
        return

    log.info(f"Neue Datei erkannt: {file_path}")
    if not wait_for_file_stable(file_path):
        log.warning(f"Datei nicht stabil: {file_path.name}")
        return

    silo = detect_silo(file_path)
    stem, confirmed_silo = convert_document(file_path)

    if stem and confirmed_silo:
        new_orig_name = f"{stem}{file_path.suffix.lower()}"
        dest = ORIGINALS_DIR / confirmed_silo / new_orig_name
        counter = 1
        while dest.exists():
            dest = ORIGINALS_DIR / confirmed_silo / f"{stem}_{counter}{file_path.suffix.lower()}"
            counter += 1
        shutil.move(str(file_path), str(dest))
        log.info(f"Original archiviert: {dest}")
    else:
        log.warning(f"Verarbeitung fehlgeschlagen, Datei bleibt: {file_path.name}")
        if TELEGRAM_TOKEN and TELEGRAM_CHAT:
            tg_send(f"❌ Fehler bei: {file_path.name}")


# ── Queue-Worker (sequenziell, läuft im Hauptthread) ──────────────────────────

def queue_worker():
    while True:
        file_path = file_queue.get()
        try:
            process_file(file_path)
        except Exception as e:
            log.error(f"Unerwarteter Fehler bei {file_path}: {e}")
        finally:
            file_queue.task_done()


# ── Watchdog ───────────────────────────────────────────────────────────────────

class DocumentHandler(FileSystemEventHandler):
    def _enqueue(self, path: Path):
        if path.suffix.lower() in SUPPORTED:
            log.info(f"In Queue aufgenommen: {path.name}")
            file_queue.put(path)

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._enqueue(Path(event.dest_path))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Watcher startet. Überwache: {WATCH_DIR}")
    log.info(f"Silos: {', '.join(SILOS.keys())}")
    log.info(f"Telegram: {'aktiv' if TELEGRAM_TOKEN else 'nicht konfiguriert'}")

    if not wait_for_docling():
        log.error("Docling Serve nicht erreichbar. Beende.")
        raise SystemExit(1)

    # Queue-Worker in eigenem Thread (blockiert bei Telegram-Warten)
    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    # Bestehende Dateien beim Start verarbeiten
    log.info("Prüfe bestehende Dateien...")
    for silo_dir in [WATCH_DIR] + [WATCH_DIR / s for s in SILOS]:
        if silo_dir.exists():
            for f in silo_dir.iterdir():
                if f.is_file() and f.suffix.lower() in SUPPORTED:
                    file_queue.put(f)

    observer = Observer()
    observer.schedule(DocumentHandler(), str(WATCH_DIR), recursive=True)
    observer.start()
    log.info("Watcher aktiv — warte auf Dokumente.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
