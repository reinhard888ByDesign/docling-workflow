#!/usr/bin/env python3
"""
Wilson Document Processor
Watches ~/incoming/ for PDFs, extracts metadata via Ryzen Dispatcher (OCR + Ollama),
manages 60-min pending queue, handles Telegram corrections.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

# ── Konfiguration ──────────────────────────────────────────────────────────────

INCOMING_DIR    = Path.home() / "incoming"
PENDING_DIR     = Path.home() / "pending"
OUTPUT_DIR      = Path.home() / "input-dispatcher"
DB_PATH         = Path.home() / ".openclaw" / "doc_processor.db"

DISPATCHER_URL  = os.environ.get("DISPATCHER_URL",  "http://192.168.86.195:8765")
OLLAMA_URL      = os.environ.get("OLLAMA_URL",       "http://192.168.86.195:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL",     "gemma4:e4b")
DEEPSEEK_KEY    = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL  = os.environ.get("DEEPSEEK_MODEL",   "deepseek-chat")
DEEPSEEK_URL    = "https://api.deepseek.com/v1/chat/completions"

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "8382100394:AAE8SWmXbzxqiJpAESnYYWJeNzVfLrokQhA")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID",   "8620231031")
DISABLE_TG_POLL = os.environ.get("DISABLE_TELEGRAM_POLL", "0") == "1"
PENDING_MINUTES = int(os.environ.get("PENDING_MINUTES", "60"))
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL",   "15"))   # Sekunden zwischen Verzeichnis-Scans

CATEGORIES = {
    "persoenlich":          "10 Persönlich — Ausweise, Urkunden, Vollmachten",
    "familie":              "20 Familie — Familie Janning/Hutterer, Haustiere",
    "fengshui":             "30 FengShui — Beratung und Audits",
    "finanzen":             "40 Finanzen — Banken, Versicherungen, Steuern",
    "krankenversicherung":  "49 Krankenversicherung — HUK, Gothaer, vigo, Arztrechnungen",
    "versicherung":         "Versicherung — Sachversicherungen, Kfz-Versicherung",
    "immobilien_eigen":     "50 Immobilien eigen — Seggiano, Übersee",
    "immobilien_vermietet": "51 Immobilien vermietet — München, Bremen, Karlsruhe",
    "garten":               "55 Garten — Landschaftspflege, Bewässerung",
    "fahrzeuge":            "60 Fahrzeuge — KFZ, Werkstatt, TÜV",
    "italien":              "70 Italien — Behörden, Comune (nicht Immobilien)",
    "business":             "80 Business — 888byDesign, Buchhaltung",
    "digitales":            "82 Digitales — Smart Home, IT, Netzwerk",
    "wissen":               "85 Wissen — Bücher, Kurse",
    "reisen":               "90 Reisen — Flüge, Hotels, Buchungen",
    "bedienungsanleitung":  "95 Bedienungsanleitungen — Gerätehandbücher",
    "archiv":               "99 Archiv — Historisches, nicht zuordenbar",
}

ADRESSATEN = ["Reinhard", "Marion", "Linoa", "Sonstiges"]

# Kurze Anzeigetexte für Telegram-Buttons (max ~18 Zeichen)
_CAT_SHORT = {
    "persoenlich":          "10 Persönlich",
    "familie":              "20 Familie",
    "fengshui":             "30 FengShui",
    "finanzen":             "40 Finanzen",
    "krankenversicherung":  "49 Krankenvers.",
    "versicherung":         "Versicherung",
    "immobilien_eigen":     "50 Immo eigen",
    "immobilien_vermietet": "51 Immo vermietet",
    "garten":               "55 Garten",
    "fahrzeuge":            "60 Fahrzeuge",
    "italien":              "70 Italien",
    "business":             "80 Business",
    "digitales":            "82 Digitales",
    "wissen":               "85 Wissen",
    "reisen":               "90 Reisen",
    "bedienungsanleitung":  "95 Anleitung",
    "archiv":               "99 Archiv",
}

# Status-Werte für geführten Dialog
GUIDED_STATES = ("guided_kat", "guided_adr", "guided_abs", "guided_fin")

# Deterministisches Absender-Override: Muster → (kategorie_id, adressat)
# Schlägt LLM-Ergebnis, da der Absender ein harter Fakt ist.
ABSENDER_OVERRIDE: list[tuple[str, str, str | None]] = [
    # (muster_lower, kategorie_id, adressat_override)
    ("huk-coburg",           "krankenversicherung", "Marion"),
    ("huk coburg",           "krankenversicherung", "Marion"),
    ("huk",                  "krankenversicherung", "Marion"),
    ("gothaer krankenvers",  "krankenversicherung", "Reinhard"),
    ("gothaer",              "krankenversicherung", "Reinhard"),
    ("barmenia",             "krankenversicherung", "Reinhard"),
    ("vigo krankenvers",     "krankenversicherung", "Reinhard"),
    ("vigo",                 "krankenversicherung", "Reinhard"),
    ("tierarzt",             "familie",             None),
    ("tierklinik",           "familie",             None),
    ("veterinär",            "familie",             None),
    ("veterinaria",          "familie",             None),
    ("clinica veterinaria",  "familie",             None),
]

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Datenbank ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS pending (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pdf_hash    TEXT UNIQUE,
                orig_name   TEXT,
                pending_dir TEXT,
                sidecar     TEXT,           -- JSON der Metadaten
                tg_msg_id   INTEGER,        -- Telegram-Message-ID der Benachrichtigung
                status      TEXT DEFAULT 'pending',  -- pending|correcting|confirmed|rejected|transferred
                corr_field  TEXT,           -- Feld, das gerade korrigiert wird
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                transfer_at TEXT            -- geplante Weitergabe
            );

            CREATE TABLE IF NOT EXISTS tg_offset (
                id      INTEGER PRIMARY KEY,
                offset  INTEGER DEFAULT 0
            );
            INSERT OR IGNORE INTO tg_offset(id, offset) VALUES (1, 0);
        """)

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def normalize_filename(text: str) -> str:
    """Entfernt Sonderzeichen und normalisiert für Dateinamen."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"[\s_]+", "-", text.strip())
    return text[:50]

def build_filename(sidecar: dict) -> str:
    datum = sidecar.get("datum", "")
    datum_clean = datum.replace("-", "")[:8] if datum else "00000000"
    absender = normalize_filename(sidecar.get("absender", "Unbekannt"))
    kurz = normalize_filename(sidecar.get("kurzbezeichnung", ""))
    parts = [p for p in [datum_clean, absender, kurz] if p]
    return "_".join(parts) + ".pdf"

def pdf_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# ── Ryzen-Kommunikation ────────────────────────────────────────────────────────

def ocr_pdf(pdf_path: Path) -> str | None:
    """Sendet PDF an Dispatcher /api/ocr, gibt Markdown-Text zurück."""
    log.info(f"OCR via Dispatcher: {pdf_path.name}")
    try:
        with open(pdf_path, "rb") as f:
            r = requests.post(
                f"{DISPATCHER_URL}/api/ocr",
                files={"file": (pdf_path.name, f, "application/pdf")},
                timeout=300,
            )
        if r.status_code != 200:
            log.error(f"OCR-Fehler {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("text")
    except Exception as e:
        log.error(f"OCR-Aufruf fehlgeschlagen: {e}")
        return None

def extract_metadata(ocr_text: str) -> dict | None:
    """Extrahiert Metadaten: DeepSeek API wenn Key vorhanden, sonst Ollama."""
    if DEEPSEEK_KEY:
        return _extract_metadata_deepseek(ocr_text)
    return _extract_metadata_ollama(ocr_text)


def _extract_metadata_deepseek(ocr_text: str) -> dict | None:
    """Metadaten-Extraktion via DeepSeek API."""
    return _run_llm_extraction(
        ocr_text,
        url=DEEPSEEK_URL,
        payload_fn=lambda prompt: {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1024,
        },
        content_fn=lambda r: r.json()["choices"][0]["message"]["content"],
        label=f"DeepSeek ({DEEPSEEK_MODEL})",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
    )


def _extract_metadata_ollama(ocr_text: str) -> dict | None:
    """Ollama-Fallback (identisch mit ursprünglichem extract_metadata-Body)."""
    return _run_llm_extraction(
        ocr_text,
        url=f"{OLLAMA_URL}/api/chat",
        payload_fn=lambda prompt: {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_ctx": 8192, "temperature": 0.1},
        },
        content_fn=lambda r: r.json()["message"]["content"],
        label=f"Ollama ({OLLAMA_MODEL})",
        headers={},
    )


def _run_llm_extraction(ocr_text, url, payload_fn, content_fn, label, headers):
    """Gemeinsame LLM-Extraktionslogik für DeepSeek und Ollama."""
    cat_list = "\n".join(f"  {k}: {v}" for k, v in CATEGORIES.items())
    prompt = _build_extraction_prompt(cat_list, ocr_text)
    log.info(f"LLM-Extraktion via {label}")
    try:
        r = requests.post(url, json=payload_fn(prompt), headers=headers, timeout=180)
        if r.status_code != 200:
            log.error(f"{label} Fehler {r.status_code}: {r.text[:200]}")
            return None
        content = content_fn(r)
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            log.error(f"Kein JSON in {label}-Antwort: {content[:200]}")
            return None
        meta = json.loads(m.group())
        import html as _html
        for _f in ("absender", "kurzbezeichnung", "beschreibung"):
            if isinstance(meta.get(_f), str):
                meta[_f] = _html.unescape(meta[_f])
        if meta.get("kategorie_id") not in CATEGORIES:
            meta["kategorie_id"] = "archiv"
        if meta.get("adressat") not in ADRESSATEN:
            meta["adressat"] = "Sonstiges"
        # Deterministischer Keyword-Override für immobilien_eigen
        ocr_lower = ocr_text.lower()
        descr_lower = (meta.get("beschreibung") or "").lower()
        IMMO_EIGEN_KEYWORDS = [
            "podere dei venti", "poderedeiventi", "seggiano", "grassauer",
            "servizi ecologici", "fognaria", "bonifica", "manutenzione",
        ]
        if meta.get("kategorie_id") == "archiv" and any(
            kw in ocr_lower or kw in descr_lower for kw in IMMO_EIGEN_KEYWORDS
        ):
            log.info("Keyword-Override: archiv → immobilien_eigen (Immobilien-Keyword erkannt)")
            meta["kategorie_id"] = "immobilien_eigen"
            if meta.get("adressat") == "Sonstiges":
                meta["adressat"] = "Reinhard"
        # Absender-Override
        absender_lower = (meta.get("absender") or "").lower()
        for muster, kat, adr in ABSENDER_OVERRIDE:
            if muster in absender_lower:
                if meta.get("kategorie_id") != kat:
                    log.info(f"Absender-Override: '{meta['kategorie_id']}' → '{kat}'")
                    meta["kategorie_id"] = kat
                if adr and meta.get("adressat") != adr:
                    meta["adressat"] = adr
                meta["_override_applied"] = True
                break
        return meta
    except json.JSONDecodeError as e:
        log.error(f"JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        log.error(f"{label}-Aufruf fehlgeschlagen: {e}")
        return None


def _build_extraction_prompt(cat_list: str, ocr_text: str) -> str:
    return f"""Du bist ein Dokumenten-Assistent. Analysiere den folgenden OCR-Text und antworte ausschließlich mit einem JSON-Objekt — kein Text davor oder danach.

GÜLTIGE KATEGORIEN (kategorie_id → Beschreibung):
{cat_list}

KATEGORIE-REGELN (lese diese sorgfältig, bevor du klassifizierst):

1. krankenversicherung — gilt für ALLE Dokumente von Krankenkassen oder Ärzten:
   - Absender ist HUK, HUK-COBURG → krankenversicherung, adressat="Marion"
   - Absender ist Gothaer, Barmenia, vigo → krankenversicherung, adressat="Reinhard"
   - Absender ist Arzt, Krankenhaus, Labor, MVZ, Zahnarzt, Sanitätshaus, Apotheke → krankenversicherung
   AUSNAHME: Tierarzt / Tierklinik / Veterinär → kategorie="familie"

2. versicherung — nur Sachversicherungen, KFZ-Versicherung (NICHT Krankenversicherung):
   - Gebäudeversicherung, Haftpflicht, KFZ-Versicherung → versicherung

3. immobilien_eigen — Rechnungen für eigene Immobilien:
   - Text enthält: Seggiano, Podere dei Venti, Poderedeiventi, Übersee, Grassauer Straße → immobilien_eigen
   - Servizi Ecologici, Fognaria, Bonifica, Manutenzione, Ecologia → immobilien_eigen
   - adressat="Reinhard" wenn kein anderer Empfänger genannt

4. immobilien_vermietet — Objekte: München/Lipowskystraße, Bremen/Kornstraße,
   Karlsruhe/Kolberger, Neuburg/Schießhausstraße, Schechen/Bahnhofstraße → immobilien_vermietet

5. archiv — LETZTER AUSWEG, nur wenn wirklich keine Kategorie passt

ABSENDER → ADRESSAT-REGELN:
- HUK / HUK-COBURG → adressat="Marion" (IMMER)
- Gothaer / Barmenia / vigo → adressat="Reinhard" (IMMER)
- Dokument für eigene Immobilie (Seggiano, Podere, Übersee) → adressat="Reinhard"

EXTRAHIERE folgende Felder:
{{
  "absender": "Firmen- oder Personenname, kurz und präzise",
  "datum": "Dokumentdatum als YYYY-MM-DD",
  "kategorie_id": "eine der obigen kategorie_id-Werte",
  "adressat": "Reinhard" oder "Marion" oder "Linoa" oder "Sonstiges",
  "kurzbezeichnung": "2-4 Wörter mit Bindestrichen",
  "beschreibung": "3-5 vollständige Sätze auf Deutsch: Worum geht es, welche Beträge, welcher Zeitraum?",
  "konfidenz": "hoch" | "mittel" | "niedrig"
}}

OCR-TEXT:
---
{ocr_text[:6000]}
---"""

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str, reply_markup: dict | None = None, parse_mode: str = "HTML") -> int | None:
    """Sendet Telegram-Nachricht, gibt message_id zurück."""
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=15,
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        log.warning(f"Telegram sendMessage Fehler: {data}")
    except Exception as e:
        log.error(f"Telegram-Fehler: {e}")
    return None

def tg_edit(msg_id: int, text: str, reply_markup: dict | None = None):
    """Bearbeitet bestehende Telegram-Nachricht."""
    payload = {"chat_id": TELEGRAM_CHAT, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json=payload, timeout=15,
        )
    except Exception as e:
        log.error(f"Telegram editMessage Fehler: {e}")

def tg_answer_callback(callback_id: str, text: str = ""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_id": callback_id, "text": text}, timeout=10,
        )
    except Exception:
        pass

def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 10, "allowed_updates": ["message", "callback_query"]},
            timeout=20,
        )
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception:
        return []

def format_notification(sidecar: dict, transfer_at: str, doc_id: int) -> tuple[str, dict]:
    """Erstellt Telegram-Benachrichtigungstext und Keyboard."""
    s = sidecar
    datum_fmt = s.get("datum") or "unbekannt"
    beschreibung = s.get("beschreibung") or "–"
    dateiname = s.get("dateiname") or "unbekannt"
    transfer_dt = datetime.fromisoformat(transfer_at)
    minuten = max(0, int((transfer_dt - datetime.now()).total_seconds() / 60))

    text = (
        f"📄 <b>Neues Dokument erkannt</b>\n"
        f"{'─' * 35}\n"
        f"📋 <b>Absender:</b>  {s.get('absender') or '?'}\n"
        f"📅 <b>Datum:</b>     {datum_fmt}\n"
        f"📁 <b>Kategorie:</b> {CATEGORIES.get(s.get('kategorie_id',''), s.get('kategorie_id','?'))}\n"
        f"👤 <b>Adressat:</b>  {s.get('adressat') or '?'}\n"
        f"{'─' * 35}\n"
        f"📝 <b>Beschreibung:</b>\n{beschreibung}\n"
        f"{'─' * 35}\n"
        f"📌 <code>{dateiname}</code>\n"
        f"⏱️ Weitergabe in {minuten} Min."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Jetzt senden",  "callback_data": f"confirm:{doc_id}"},
        {"text": "✏️ Korrigieren",   "callback_data": f"correct:{doc_id}"},
        {"text": "🗑️ Ablehnen",      "callback_data": f"reject:{doc_id}"},
    ]]}
    return text, keyboard

def format_correction_menu(doc_id: int) -> tuple[str, dict]:
    text = "✏️ <b>Was soll geändert werden?</b>"
    keyboard = {"inline_keyboard": [
        [
            {"text": "Absender",     "callback_data": f"field:{doc_id}:absender"},
            {"text": "Datum",        "callback_data": f"field:{doc_id}:datum"},
            {"text": "Kategorie",    "callback_data": f"field:{doc_id}:kategorie_id"},
        ],
        [
            {"text": "Adressat",     "callback_data": f"field:{doc_id}:adressat"},
            {"text": "Kurzbezeichnung", "callback_data": f"field:{doc_id}:kurzbezeichnung"},
            {"text": "Beschreibung", "callback_data": f"field:{doc_id}:beschreibung"},
        ],
        [
            {"text": "← Zurück",     "callback_data": f"back:{doc_id}"},
        ],
    ]}
    return text, keyboard

def format_category_keyboard(doc_id: int) -> tuple[str, dict]:
    text = "📁 <b>Kategorie wählen:</b>"
    cats = list(CATEGORIES.keys())
    rows = []
    for i in range(0, len(cats), 2):
        row = [{"text": cats[i], "callback_data": f"setcat:{doc_id}:{cats[i]}"}]
        if i + 1 < len(cats):
            row.append({"text": cats[i+1], "callback_data": f"setcat:{doc_id}:{cats[i+1]}"})
        rows.append(row)
    rows.append([{"text": "← Zurück", "callback_data": f"correct:{doc_id}"}])
    return text, {"inline_keyboard": rows}

def format_adressat_keyboard(doc_id: int) -> tuple[str, dict]:
    text = "👤 <b>Adressat wählen:</b>"
    keyboard = {"inline_keyboard": [
        [{"text": a, "callback_data": f"setadr:{doc_id}:{a}"} for a in ADRESSATEN],
        [{"text": "← Zurück", "callback_data": f"correct:{doc_id}"}],
    ]}
    return text, keyboard

# ── Geführter Klassifikations-Dialog ──────────────────────────────────────────

def format_guided_step_kat(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Schritt 1: Kategorie wählen."""
    konfidenz = meta.get("konfidenz", "mittel")
    vorschlag = meta.get("kategorie_id", "")
    vorschlag_label = _CAT_SHORT.get(vorschlag, vorschlag)
    text = (
        f"⚠️ <b>Wilson ist unsicher</b> (Konfidenz: {konfidenz})\n"
        f"{'─' * 32}\n"
        f"📄 <code>{meta.get('dateiname', '?')}</code>\n"
        f"📅 {meta.get('datum') or '?'}  ·  {meta.get('absender') or '?'}\n"
        f"{'─' * 32}\n"
        f"Mein Vorschlag: <b>{vorschlag_label}</b>\n\n"
        f"<b>① Kategorie wählen:</b>"
    )
    cats = list(CATEGORIES.keys())
    rows = []
    for i in range(0, len(cats), 2):
        row = [{"text": _CAT_SHORT.get(cats[i], cats[i]),
                "callback_data": f"gkat:{doc_id}:{cats[i]}"}]
        if i + 1 < len(cats):
            row.append({"text": _CAT_SHORT.get(cats[i+1], cats[i+1]),
                        "callback_data": f"gkat:{doc_id}:{cats[i+1]}"})
        rows.append(row)
    return text, {"inline_keyboard": rows}


def format_guided_step_adr(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Schritt 2: Adressat wählen."""
    kat_label = _CAT_SHORT.get(meta.get("kategorie_id", ""), meta.get("kategorie_id", "?"))
    text = (
        f"⚠️ <b>Geführte Klassifikation</b>\n"
        f"{'─' * 32}\n"
        f"📁 Kategorie: <b>{kat_label}</b> ✓\n\n"
        f"<b>② Adressat wählen:</b>"
    )
    keyboard = {"inline_keyboard": [
        [{"text": a, "callback_data": f"gadr:{doc_id}:{a}"} for a in ADRESSATEN],
        [{"text": "← Kategorie ändern", "callback_data": f"gedit:{doc_id}:kat"}],
    ]}
    return text, keyboard


def format_guided_step_abs(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Schritt 3: Absender bestätigen oder neu eingeben."""
    kat_label = _CAT_SHORT.get(meta.get("kategorie_id", ""), meta.get("kategorie_id", "?"))
    adr = meta.get("adressat", "?")
    vorschlag = meta.get("absender") or ""
    text = (
        f"⚠️ <b>Geführte Klassifikation</b>\n"
        f"{'─' * 32}\n"
        f"📁 Kategorie: <b>{kat_label}</b> ✓\n"
        f"👤 Adressat: <b>{adr}</b> ✓\n\n"
        f"<b>③ Absender bestätigen:</b>\n"
        f"Mein Vorschlag: <code>{vorschlag or '?'}</code>"
    )
    rows = []
    if vorschlag:
        rows.append([{"text": f"✓ {vorschlag[:30]}", "callback_data": f"gabs:{doc_id}:__ok__"}])
    rows.append([{"text": "✏️ Anderen Absender eingeben", "callback_data": f"gabsneu:{doc_id}"}])
    rows.append([{"text": "← Adressat ändern", "callback_data": f"gedit:{doc_id}:adr"}])
    return text, {"inline_keyboard": rows}


def format_guided_summary(doc_id: int, meta: dict) -> tuple[str, dict]:
    """Abschluss: Zusammenfassung + Senden."""
    kat_label = _CAT_SHORT.get(meta.get("kategorie_id", ""), meta.get("kategorie_id", "?"))
    text = (
        f"✅ <b>Klassifikation abgeschlossen</b>\n"
        f"{'─' * 32}\n"
        f"📄 <code>{meta.get('dateiname', '?')}</code>\n"
        f"📅 {meta.get('datum') or '?'}\n"
        f"📁 Kategorie: <b>{kat_label}</b>\n"
        f"👤 Adressat: <b>{meta.get('adressat') or '?'}</b>\n"
        f"📤 Absender: <b>{meta.get('absender') or '?'}</b>\n"
        f"{'─' * 32}\n"
        f"📝 {meta.get('beschreibung') or '–'}"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Jetzt senden",   "callback_data": f"gfin:{doc_id}"},
         {"text": "🗑️ Ablehnen",       "callback_data": f"reject:{doc_id}"}],
        [{"text": "← Absender ändern", "callback_data": f"gedit:{doc_id}:abs"},
         {"text": "← Adressat ändern", "callback_data": f"gedit:{doc_id}:adr"}],
    ]}
    return text, keyboard


# ── Dokument-Verarbeitung ──────────────────────────────────────────────────────

def process_pdf(pdf_path: Path):
    """Hauptverarbeitungsroutine für eine neue PDF-Datei."""
    log.info(f"Verarbeite: {pdf_path.name}")

    # Duplikat-Check
    h = pdf_hash(pdf_path)
    with get_db() as con:
        row = con.execute("SELECT id, status FROM pending WHERE pdf_hash=?", (h,)).fetchone()
    if row:
        log.info(f"Duplikat (Hash {h[:8]}…), überspringe.")
        return

    # Pending-Verzeichnis anlegen
    pending_subdir = PENDING_DIR / h[:12]
    pending_subdir.mkdir(parents=True, exist_ok=True)
    dest_pdf = pending_subdir / pdf_path.name
    shutil.copy2(pdf_path, dest_pdf)

    # OCR
    ocr_text = ocr_pdf(dest_pdf)
    if not ocr_text:
        log.warning(f"OCR fehlgeschlagen für {pdf_path.name} — Ryzen nicht erreichbar?")
        tg_send(
            f"⚠️ <b>OCR fehlgeschlagen</b>\n<code>{pdf_path.name}</code>\n"
            "Ryzen nicht erreichbar. Dokument bleibt in ~/incoming/ bis Retry."
        )
        return

    # LLM-Metadaten-Extraktion
    meta = extract_metadata(ocr_text)
    if not meta:
        log.warning(f"LLM-Extraktion fehlgeschlagen für {pdf_path.name}")
        meta = {
            "absender": "Unbekannt",
            "datum": None,
            "kategorie_id": "archiv",
            "adressat": "Sonstiges",
            "kurzbezeichnung": "Unbekannt",
            "beschreibung": "Automatische Extraktion fehlgeschlagen.",
        }

    # Sidecar aufbauen
    transfer_at = (datetime.now() + timedelta(minutes=PENDING_MINUTES)).isoformat(timespec="seconds")
    meta["dateiname"] = build_filename(meta)
    sidecar = {
        "version": "2.0",
        "dokument": meta,
        "verarbeitung": {
            "extrahiert_von": "Wilson",
            "extrahiert_am": datetime.now().isoformat(timespec="seconds"),
            "ocr_via": f"dispatcher@{DISPATCHER_URL}",
            "llm_via": f"{OLLAMA_MODEL}@{OLLAMA_URL}",
            "status": "pending",
            "weitergabe_um": transfer_at,
            "user_bestaetigt": False,
            "korrekturen": [],
        },
    }
    sidecar_path = pending_subdir / (dest_pdf.stem + ".meta.json")
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))

    # DB-Eintrag
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO pending(pdf_hash, orig_name, pending_dir, sidecar, status, transfer_at) "
            "VALUES (?,?,?,?,?,?)",
            (h, pdf_path.name, str(pending_subdir), json.dumps(sidecar), "pending", transfer_at),
        )
        doc_id = cur.lastrowid

    # Konfidenz bestimmen — Override macht es immer sicher
    konfidenz = meta.get("konfidenz", "mittel")
    if meta.get("_override_applied"):
        konfidenz = "hoch"

    # Telegram-Benachrichtigung oder geführter Dialog
    if konfidenz == "hoch":
        text, keyboard = format_notification(meta, transfer_at, doc_id)
        msg_id = tg_send(text, keyboard)
        if msg_id:
            with get_db() as con:
                con.execute("UPDATE pending SET tg_msg_id=? WHERE id=?", (msg_id, doc_id))
    else:
        with get_db() as con:
            con.execute("UPDATE pending SET status='guided_kat' WHERE id=?", (doc_id,))
        text, keyboard = format_guided_step_kat(doc_id, meta)
        msg_id = tg_send(text, keyboard)
        if msg_id:
            with get_db() as con:
                con.execute("UPDATE pending SET tg_msg_id=? WHERE id=?", (msg_id, doc_id))

    log.info(f"Dokument {doc_id} in Pending [{konfidenz}]: {meta.get('dateiname')}, Weitergabe um {transfer_at}")

# ── Weitergabe ─────────────────────────────────────────────────────────────────

def transfer_document(doc_id: int):
    """Verschiebt PDF + Sidecar nach ~/input-dispatcher/."""
    with get_db() as con:
        row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return
    pending_subdir = Path(row["pending_dir"])
    sidecar = json.loads(row["sidecar"])
    dateiname = sidecar["dokument"].get("dateiname") or row["orig_name"]

    # Zieldateinamen sicherstellen (kein Konflikt)
    dest_pdf = OUTPUT_DIR / dateiname
    if dest_pdf.exists():
        stem = dest_pdf.stem
        dest_pdf = OUTPUT_DIR / f"{stem}_{doc_id}.pdf"
    dest_meta = OUTPUT_DIR / (dest_pdf.stem + ".meta.json")

    # PDF umbenennen und verschieben
    orig_pdf = next(pending_subdir.glob("*.pdf"), None)
    if orig_pdf:
        shutil.move(str(orig_pdf), str(dest_pdf))
    # Sidecar aktualisieren und verschieben
    sidecar["verarbeitung"]["status"] = "transferred"
    sidecar["verarbeitung"]["user_bestaetigt"] = True
    dest_meta.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))

    # Pending-Verzeichnis aufräumen
    shutil.rmtree(pending_subdir, ignore_errors=True)

    with get_db() as con:
        con.execute("UPDATE pending SET status='transferred' WHERE id=?", (doc_id,))

    # Telegram: update message
    msg_id = row["tg_msg_id"]
    if msg_id:
        tg_edit(msg_id, f"✅ <b>Weitergegeben</b>\n<code>{dateiname}</code>\n→ Ryzen verarbeitet demnächst.")
    log.info(f"Dokument {doc_id} übergeben: {dest_pdf.name}")

# ── Telegram-Callback-Handler ──────────────────────────────────────────────────

def handle_callback(query: dict):
    """Verarbeitet Inline-Keyboard-Callback-Queries."""
    cb_id = query["id"]
    data  = query.get("data", "")
    parts = data.split(":", 2)
    action = parts[0]

    tg_answer_callback(cb_id)

    if action == "confirm" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            con.execute("UPDATE pending SET status='confirmed' WHERE id=?", (doc_id,))
        transfer_document(doc_id)

    elif action == "reject" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if row:
            shutil.rmtree(Path(row["pending_dir"]), ignore_errors=True)
            with get_db() as con:
                con.execute("UPDATE pending SET status='rejected' WHERE id=?", (doc_id,))
            msg_id = row["tg_msg_id"]
            if msg_id:
                tg_edit(msg_id, f"🗑️ <b>Abgelehnt</b>\n<code>{row['orig_name']}</code>")
        log.info(f"Dokument {doc_id} abgelehnt und gelöscht.")

    elif action == "correct" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM pending WHERE id=?", (doc_id,)).fetchone()
            con.execute("UPDATE pending SET status='correcting', corr_field=NULL WHERE id=?", (doc_id,))
        if row and row["tg_msg_id"]:
            text, kb = format_correction_menu(doc_id)
            tg_edit(row["tg_msg_id"], text, kb)

    elif action == "back" and len(parts) >= 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if row:
            sidecar = json.loads(row["sidecar"])
            meta = sidecar["dokument"]
            transfer_at = sidecar["verarbeitung"]["weitergabe_um"]
            con2 = get_db()
            con2.execute("UPDATE pending SET status='pending', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_notification(meta, transfer_at, doc_id)
            tg_edit(row["tg_msg_id"], text, kb)

    elif action == "field" and len(parts) == 3:
        doc_id = int(parts[1])
        field  = parts[2]
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM pending WHERE id=?", (doc_id,)).fetchone()
            con.execute("UPDATE pending SET corr_field=? WHERE id=?", (field, doc_id))
        if row and row["tg_msg_id"]:
            if field == "kategorie_id":
                text, kb = format_category_keyboard(doc_id)
                tg_edit(row["tg_msg_id"], text, kb)
            elif field == "adressat":
                text, kb = format_adressat_keyboard(doc_id)
                tg_edit(row["tg_msg_id"], text, kb)
            else:
                field_labels = {
                    "absender": "Absender",
                    "datum": "Datum (YYYY-MM-DD)",
                    "kurzbezeichnung": "Kurzbezeichnung (2-3 Wörter mit Bindestrich)",
                    "beschreibung": "Beschreibung (mehrere Sätze)",
                }
                tg_edit(row["tg_msg_id"],
                    f"✏️ <b>{field_labels.get(field, field)} eingeben:</b>\n"
                    f"(Antworte mit dem neuen Wert als Textnachricht)")

    elif action == "setcat" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_cat = parts[2]
        if new_cat in CATEGORIES:
            _update_sidecar_field(doc_id, "kategorie_id", new_cat)

    elif action == "setadr" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_adr = parts[2]
        if new_adr in ADRESSATEN:
            _update_sidecar_field(doc_id, "adressat", new_adr)

    # ── Geführter Dialog ──────────────────────────────────────────────────────

    elif action == "gkat" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_cat = parts[2]
        if new_cat not in CATEGORIES:
            return
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        sidecar["dokument"]["kategorie_id"] = new_cat
        sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
        with get_db() as con:
            con.execute("UPDATE pending SET sidecar=?, status='guided_adr' WHERE id=?",
                        (json.dumps(sidecar), doc_id))
        text, kb = format_guided_step_adr(doc_id, sidecar["dokument"])
        tg_edit(row["tg_msg_id"], text, kb)

    elif action == "gadr" and len(parts) == 3:
        doc_id  = int(parts[1])
        new_adr = parts[2]
        if new_adr not in ADRESSATEN:
            return
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        sidecar["dokument"]["adressat"] = new_adr
        with get_db() as con:
            con.execute("UPDATE pending SET sidecar=?, status='guided_abs' WHERE id=?",
                        (json.dumps(sidecar), doc_id))
        text, kb = format_guided_step_abs(doc_id, sidecar["dokument"])
        tg_edit(row["tg_msg_id"], text, kb)

    elif action == "gabs" and len(parts) == 3:
        doc_id = int(parts[1])
        # __ok__ bedeutet: LLM-Vorschlag übernehmen
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        if parts[2] != "__ok__":
            sidecar["dokument"]["absender"] = parts[2]
            sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
        with get_db() as con:
            con.execute("UPDATE pending SET sidecar=?, status='guided_fin', corr_field=NULL WHERE id=?",
                        (json.dumps(sidecar), doc_id))
        text, kb = format_guided_summary(doc_id, sidecar["dokument"])
        tg_edit(row["tg_msg_id"], text, kb)

    elif action == "gabsneu" and len(parts) == 2:
        doc_id = int(parts[1])
        with get_db() as con:
            row = con.execute("SELECT tg_msg_id FROM pending WHERE id=?", (doc_id,)).fetchone()
            con.execute("UPDATE pending SET corr_field='new_absender' WHERE id=?", (doc_id,))
        if row and row["tg_msg_id"]:
            tg_edit(row["tg_msg_id"],
                    "✏️ <b>Absender eingeben:</b>\n(Antworte mit dem Namen als Textnachricht)")

    elif action == "gfin" and len(parts) == 2:
        doc_id = int(parts[1])
        with get_db() as con:
            con.execute("UPDATE pending SET status='confirmed' WHERE id=?", (doc_id,))
        transfer_document(doc_id)

    elif action == "gedit" and len(parts) == 3:
        doc_id = int(parts[1])
        step   = parts[2]
        with get_db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        sidecar = json.loads(row["sidecar"])
        meta    = sidecar["dokument"]
        if step == "kat":
            con2 = get_db()
            con2.execute("UPDATE pending SET status='guided_kat', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_guided_step_kat(doc_id, meta)
        elif step == "adr":
            con2 = get_db()
            con2.execute("UPDATE pending SET status='guided_adr', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_guided_step_adr(doc_id, meta)
        elif step == "abs":
            con2 = get_db()
            con2.execute("UPDATE pending SET status='guided_abs', corr_field=NULL WHERE id=?", (doc_id,))
            con2.commit()
            text, kb = format_guided_step_abs(doc_id, meta)
        else:
            return
        tg_edit(row["tg_msg_id"], text, kb)

def _update_sidecar_field(doc_id: int, field: str, value: str):
    """Aktualisiert ein Feld im Sidecar und zeigt aktualisierte Benachrichtigung."""
    with get_db() as con:
        row = con.execute("SELECT * FROM pending WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return
    sidecar = json.loads(row["sidecar"])
    sidecar["dokument"][field] = value
    # Dateiname neu bauen
    sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
    sidecar["verarbeitung"]["korrekturen"].append({
        "feld": field, "wert": value,
        "am": datetime.now().isoformat(timespec="seconds"),
    })
    # Sidecar-Datei aktualisieren
    pending_subdir = Path(row["pending_dir"])
    for sf in pending_subdir.glob("*.meta.json"):
        sf.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2))
    with get_db() as con:
        con.execute(
            "UPDATE pending SET sidecar=?, status='pending', corr_field=NULL WHERE id=?",
            (json.dumps(sidecar), doc_id),
        )
    msg_id = row["tg_msg_id"]
    if msg_id:
        meta = sidecar["dokument"]
        transfer_at = sidecar["verarbeitung"]["weitergabe_um"]
        text, kb = format_notification(meta, transfer_at, doc_id)
        tg_edit(msg_id, text, kb)

def _handle_tg_pdf_upload(doc: dict):
    """Lädt ein per Telegram hochgeladenes PDF herunter und legt es in ~/incoming/."""
    file_id   = doc.get("file_id", "")
    file_name = doc.get("file_name") or f"telegram_{file_id[:8]}.pdf"
    if not file_name.lower().endswith(".pdf"):
        file_name += ".pdf"
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10,
        )
        file_path = r.json().get("result", {}).get("file_path", "")
        if not file_path:
            tg_send("❌ Telegram-Datei nicht abrufbar.")
            return
        dl = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=30,
        )
        dest = INCOMING_DIR / file_name
        counter = 2
        while dest.exists():
            dest = INCOMING_DIR / f"{Path(file_name).stem}_{counter}.pdf"
            counter += 1
        dest.write_bytes(dl.content)
        log.info(f"PDF von Telegram empfangen: {dest.name}")
        tg_send(f"📥 PDF empfangen: <code>{dest.name}</code>\nVerarbeitung startet…")
    except Exception as e:
        log.error(f"PDF-Download fehlgeschlagen: {e}")
        tg_send(f"❌ PDF-Download fehlgeschlagen: {e}")


def handle_text_message(msg: dict):
    """Verarbeitet eingehende Textnachrichten als Korrektureingaben."""
    text = msg.get("text", "").strip()
    if not text or text.startswith("/"):
        return

    with get_db() as con:
        # Geführter Dialog: Absender-Texteingabe
        guided_row = con.execute(
            "SELECT * FROM pending WHERE status='guided_abs' AND corr_field='new_absender' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if guided_row:
            doc_id  = guided_row["id"]
            sidecar = json.loads(guided_row["sidecar"])
            sidecar["dokument"]["absender"] = text
            sidecar["dokument"]["dateiname"] = build_filename(sidecar["dokument"])
            con.execute(
                "UPDATE pending SET sidecar=?, status='guided_fin', corr_field=NULL WHERE id=?",
                (json.dumps(sidecar), doc_id),
            )
            summary_text, kb = format_guided_summary(doc_id, sidecar["dokument"])
            tg_edit(guided_row["tg_msg_id"], summary_text, kb)
            return

        # Normaler Korrekturmodus
        row = con.execute(
            "SELECT * FROM pending WHERE status='correcting' AND corr_field IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return
    field = row["corr_field"]
    if field in ("absender", "datum", "kurzbezeichnung", "beschreibung"):
        _update_sidecar_field(row["id"], field, text)

# ── Hintergrund-Threads ────────────────────────────────────────────────────────

DISPATCHER_CLEANUP_MINUTES = int(os.environ.get("DISPATCHER_CLEANUP_MINUTES", "120"))
_STARTUP_TIME = datetime.now()
_SYNCTHING_APIKEY: str | None = None


def _syncthing_apikey() -> str | None:
    global _SYNCTHING_APIKEY
    if _SYNCTHING_APIKEY:
        return _SYNCTHING_APIKEY
    try:
        import xml.etree.ElementTree as ET
        cfg = Path.home() / ".local/state/syncthing/config.xml"
        root = ET.parse(cfg).getroot()
        _SYNCTHING_APIKEY = root.find(".//gui/apikey").text
    except Exception:
        pass
    return _SYNCTHING_APIKEY


def _cleanup_output_dir():
    """Löscht Dateien aus ~/input-dispatcher/ sobald der Dispatcher sie verarbeitet hat.
    Strategie:
      1. Primär: Syncthing meldet 'needDeletes' → Ryzen hat die Datei gelöscht → sicher entfernen.
      2. Fallback: Datei älter als DISPATCHER_CLEANUP_MINUTES (Standard 120 min) UND
         das Programm läuft mindestens so lange (kein Kaltstart-Wettlauf).
    """
    uptime_min = (datetime.now() - _STARTUP_TIME).total_seconds() / 60

    # Dateien die Ryzen bereits gelöscht hat (= verarbeitet) via Syncthing-API ermitteln
    processed_on_ryzen: set[str] = set()
    apikey = _syncthing_apikey()
    if apikey:
        try:
            r = requests.get(
                "http://127.0.0.1:8384/rest/db/need?folder=input-dispatcher",
                headers={"X-API-Key": apikey}, timeout=3,
            )
            if r.ok:
                for item in r.json().get("deletes", []):
                    processed_on_ryzen.add(item["name"])
        except Exception:
            pass

    cutoff = datetime.now() - timedelta(minutes=DISPATCHER_CLEANUP_MINUTES)
    for f in OUTPUT_DIR.iterdir():
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            # Primär: Ryzen hat verarbeitet
            if f.name in processed_on_ryzen:
                f.unlink()
                log.info(f"Cleanup (verarbeitet): {f.name}")
            # Fallback: Alt + Programm läuft lange genug
            elif mtime < cutoff and uptime_min >= DISPATCHER_CLEANUP_MINUTES:
                f.unlink()
                log.info(f"Cleanup (Timeout {DISPATCHER_CLEANUP_MINUTES} min): {f.name}")
        except Exception as e:
            log.warning(f"Cleanup-Fehler {f.name}: {e}")


def timer_thread():
    """Prüft jede Minute, ob Pending-Dokumente die Wartezeit überschritten haben."""
    while True:
        time.sleep(60)
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with get_db() as con:
                rows = con.execute(
                    "SELECT id FROM pending WHERE status IN "
                    "('pending','correcting','guided_kat','guided_adr','guided_abs','guided_fin') "
                    "AND transfer_at <= ?",
                    (now,),
                ).fetchall()
            for row in rows:
                log.info(f"Timeout: Dokument {row['id']} wird automatisch weitergegeben.")
                transfer_document(row["id"])
            _cleanup_output_dir()
        except Exception as e:
            log.error(f"Timer-Thread-Fehler: {e}")

def telegram_poll_thread():
    """Long-Polling für Telegram-Updates."""
    if DISABLE_TG_POLL:
        log.info("Telegram-Polling deaktiviert (DISABLE_TELEGRAM_POLL=1)")
        return
    with get_db() as con:
        offset = con.execute("SELECT offset FROM tg_offset WHERE id=1").fetchone()["offset"]

    while True:
        updates = tg_get_updates(offset)
        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            try:
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd and upd["message"].get("chat", {}).get("id") == int(TELEGRAM_CHAT):
                    msg = upd["message"]
                    doc = msg.get("document", {})
                    if doc.get("mime_type") == "application/pdf":
                        _handle_tg_pdf_upload(doc)
                    else:
                        handle_text_message(msg)
            except Exception as e:
                log.error(f"Update-Handler-Fehler: {e}")
        with get_db() as con:
            con.execute("UPDATE tg_offset SET offset=? WHERE id=1", (offset,))

def scan_incoming():
    """Polling-Loop: scannt ~/incoming/ auf neue PDFs."""
    known = set()
    while True:
        try:
            current = set()
            for f in INCOMING_DIR.glob("*.pdf"):
                current.add(f)
                if f not in known:
                    time.sleep(2)  # kurz warten bis Datei vollständig geschrieben
                    if f.stat().st_size > 0:
                        try:
                            process_pdf(f)
                            f.unlink()   # aus incoming/ entfernen nach Verarbeitung
                        except Exception as e:
                            log.error(f"Fehler bei {f.name}: {e}")
            known = current
        except Exception as e:
            log.error(f"Scan-Fehler: {e}")
        time.sleep(POLL_INTERVAL)

# ── Callback-Relay (HTTP, Port 8770) ──────────────────────────────────────────
# Empfängt vom Dispatcher weitergeleitete Telegram-Callbacks für den geführten Dialog.

RELAY_PORT = int(os.environ.get("RELAY_PORT", "8770"))

class _RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/tg/callback":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            cb = {
                "id": body["callback_id"],
                "data": body["data"],
                "message": {
                    "chat": {"id": int(body["chat_id"])},
                    "message_id": body["msg_id"],
                    "text": body.get("msg_text", ""),
                },
            }
            handle_callback(cb)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            log.error(f"Relay-Fehler: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *_): pass  # kein Access-Log


def relay_server_thread():
    srv = HTTPServer(("0.0.0.0", RELAY_PORT), _RelayHandler)
    log.info(f"Callback-Relay lauscht auf :{RELAY_PORT}")
    srv.serve_forever()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("Wilson Document Processor startet …")
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    # Graceful Shutdown
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT,  lambda *_: stop.set())

    threads = [
        threading.Thread(target=timer_thread,        daemon=True, name="timer"),
        threading.Thread(target=telegram_poll_thread, daemon=True, name="tg-poll"),
        threading.Thread(target=scan_incoming,        daemon=True, name="scanner"),
        threading.Thread(target=relay_server_thread,  daemon=True, name="relay"),
    ]
    for t in threads:
        t.start()

    tg_send("🟢 <b>Wilson Document Processor aktiv</b>\nWarte auf Dokumente in ~/incoming/")
    log.info(f"Bereit — überwache {INCOMING_DIR}")

    stop.wait()
    log.info("Document Processor beendet.")

if __name__ == "__main__":
    main()
