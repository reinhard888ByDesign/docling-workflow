#!/usr/bin/env python3
"""
Batch-Reimport: Leistungsabrechnungen ohne Erstattungspositionen
nachträglich durch Docling + Ollama schicken und Positionen in DB eintragen.

Läuft innerhalb des Dispatcher-Docker-Containers (oder einem kompatiblen Container)
mit Zugriff auf Docling, Ollama und die SQLite-DB.

Aufruf:
  docker run --rm \
    --network docling-net \
    --add-host host.docker.internal:host-gateway \
    -v /home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault/Originale:/data/originale:ro \
    -v /home/reinhard/docker/docling-workflow/dispatcher-temp:/data/dispatcher-temp \
    -v /home/reinhard/docker/docling-workflow/dispatcher-config:/config \
    -v /home/reinhard/docker/docling-workflow/batch_reimport.py:/app/batch_reimport.py \
    --entrypoint "" \
    document-dispatcher python3 /app/batch_reimport.py
"""

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import requests
import yaml
from json_repair import repair_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

ORIGINALE_DIR  = Path(os.environ.get("ORIGINALE_DIR", "/data/originale"))
DB_FILE        = Path(os.environ.get("DB_FILE",       "/data/dispatcher-temp/dispatcher.db"))
CONFIG_FILE    = Path(os.environ.get("CONFIG_FILE",   "/config/categories.yaml"))
DOCLING_URL    = os.environ.get("DOCLING_URL",         "http://docling-serve:5001")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",          "http://ollama:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",        "qwen2.5:7b")

# Pause zwischen Dokumenten (Sekunden) — Docling/Ollama entlasten
PAUSE_SECONDS  = int(os.environ.get("PAUSE_SECONDS", "3"))

LEISTUNGSABRECHNUNG_TYPES = {"leistungsabrechnung_reinhard", "leistungsabrechnung_marion"}


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def find_pending(con: sqlite3.Connection) -> list[dict]:
    """LAs ohne Erstattungspositionen, die ein PDF in ORIGINALE_DIR haben."""
    rows = con.execute("""
        SELECT d.id, d.dateiname, d.adressat, d.rechnungsdatum
        FROM dokumente d
        WHERE d.typ IN ('leistungsabrechnung_reinhard', 'leistungsabrechnung_marion')
          AND NOT EXISTS (
              SELECT 1 FROM erstattungspositionen e WHERE e.dokument_id = d.id
          )
        ORDER BY d.dateiname
    """).fetchall()

    pending = []
    for r in rows:
        pdf = ORIGINALE_DIR / r["dateiname"]
        if pdf.exists():
            pending.append({
                "id":            r["id"],
                "dateiname":     r["dateiname"],
                "adressat":      r["adressat"],
                "rechnungsdatum": r["rechnungsdatum"],
                "pdf_path":      pdf,
            })
    return pending


# ── Docling ───────────────────────────────────────────────────────────────────

def convert_to_markdown(file_path: Path) -> str | None:
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                f"{DOCLING_URL}/v1/convert/file",
                files={"files": (file_path.name, f, "application/octet-stream")},
                data={"to_formats": "md", "image_export_mode": "placeholder"},
                timeout=600,
            )
        if r.status_code != 200:
            log.error(f"Docling {r.status_code}: {r.text[:200]}")
            return None
        result = r.json()
        if result.get("status") != "success":
            log.error(f"Docling status: {result.get('status')}")
            return None
        return result.get("document", {}).get("md_content", "")
    except requests.exceptions.Timeout:
        log.error(f"Docling Timeout: {file_path.name}")
        return None
    except Exception as e:
        log.error(f"Docling Fehler: {e}")
        return None


# ── Ollama ────────────────────────────────────────────────────────────────────

def sanitize_for_ollama(text: str) -> str:
    cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\x7E\u00C0-\u024F\u2019\u201C\u201D€|•\-]", " ", text)
    cleaned = re.sub(r" {3,}", "  ", cleaned)
    return cleaned


def _fix_llm_json(s: str) -> str:
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", s)
    s = re.sub(r'\bNone\b', 'null', s)
    s = re.sub(r'\bTrue\b', 'true', s)
    s = re.sub(r'\bFalse\b', 'false', s)
    s = re.sub(r'(?<=:\s)(\d+),(\d{1,2})(?=\s*[,\}\]])', r'\1.\2', s)
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    return s


def build_category_description(categories: dict) -> str:
    lines = []
    for cat_id, cat in categories.items():
        lines.append(f"\nKategorie: {cat['label']} (id: {cat_id})")
        for t in cat.get("types", []):
            hints = ", ".join(t.get("hints", []))
            lines.append(f"  - Typ: {t['label']} (id: {t['id']}) | Erkennungshinweise: {hints}")
    return "\n".join(lines)


def classify_with_ollama(md_content: str, categories: dict) -> dict | None:
    cat_desc = build_category_description(categories)
    md_content = sanitize_for_ollama(md_content)
    prompt = f"""Analysiere das folgende Dokument und klassifiziere es anhand der vorgegebenen Kategorien.

Verfügbare Kategorien und Typen:
{cat_desc}

KLASSIFIZIERUNGSREGELN — lies diese sorgfältig:

Schritt 1: Wer ist der ABSENDER des Dokuments?
- Ist der Absender eine Versicherung (Gothaer, Barmenia, HUK, HUK-COBURG)?
  → Dann und NUR dann: "leistungsabrechnung_reinhard" oder "leistungsabrechnung_marion"
  → Erkennbar an: Versicherungslogo, Erstattungsübersicht, Auflistung eingereichter Fremdrechnungen, Erstattungsbetrag
- Ist der Absender ein Arzt, Krankenhaus, Klinik, Labor, Radiologie, MVZ, oder ein Abrechnungsdienstleister der IM AUFTRAG eines Arztes/einer Klinik abrechnet (z.B. unimed GmbH, Doctolib, Mediport)?
  → Immer: "arztrechnung"
  → Erkennbar an: GOÄ-Ziffern, Honorar, Liquidation, Diagnose, Fälligkeitsbetrag direkt an den Patienten
- Ist der Absender ein Sanitätshaus, Optiker, Apotheke (ohne Rezept), Physiotherapie?
  → "sonstige_medizinische_leistung"
- Ist es ein Dokument vom Arzt mit Medikamentenliste?
  → "rezept"

WICHTIG: Die bloße Erwähnung von "Versicherung" im Fließtext macht ein Dokument NICHT zu einer Leistungsabrechnung. Entscheidend ist ausschließlich wer der Absender/Aussteller ist.

Adressat: "Reinhard" wenn Reinhard Janning der Empfänger ist, "Marion" wenn Marion Janning, sonst null.

Antworte NUR mit einem JSON-Objekt mit diesen Feldern:
- "category_id": ID der erkannten Kategorie, oder null
- "type_id": ID des erkannten Typs, oder null
- "absender": Name des Absenders, oder null
- "adressat": "Reinhard" | "Marion" | null
- "rechnungsdatum": Datum als "DD.MM.YYYY" oder null
- "rechnungsbetrag": Gesamtrechnungsbetrag als String (z.B. "456,64 EUR") oder null
- "erstattungsbetrag": Von Versicherung erstatteter Betrag als String oder null
- "positionen": Liste der Erstattungspositionen — NUR bei leistungsabrechnung-Typen, sonst [].
  Jede Position: {{"leistungserbringer": "Name", "zeitraum": "02.02-19.04.2023", "rechnungsbetrag": 33.06, "erstattungsbetrag": 10.72}}
- "konfidenz": "hoch" | "mittel" | "niedrig"

Antworte AUSSCHLIESSLICH mit validem JSON.

Dokument:
{md_content[:6000]}"""

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        if not r.ok:
            log.warning(f"Ollama {r.status_code}: {r.text[:200]}")
            return None
        raw = r.json().get("response", "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            log.warning(f"Kein JSON in Antwort: {raw[:200]}")
            return None
        json_str = _fix_llm_json(match.group())
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            try:
                repaired = repair_json(json_str, return_objects=True)
                if isinstance(repaired, dict):
                    return repaired
            except Exception:
                pass
            log.warning(f"JSON-Parse fehlgeschlagen: {repr(json_str[:200])}")
            return None
    except Exception as e:
        log.warning(f"Ollama Fehler: {e}")
        return None


# ── DB-Insert ─────────────────────────────────────────────────────────────────

def _parse_betrag(s) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[^\d,.]", "", str(s)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def insert_positionen(con: sqlite3.Connection, doc: dict, result: dict) -> list[dict]:
    """
    Fügt Erstattungspositionen für ein bereits in dokumente vorhandenes Dokument ein.
    Matched gegen offene Rechnungen und aktualisiert deren Status.
    """
    dok_id   = doc["id"]
    adressat = result.get("adressat") or doc["adressat"]
    rechnungsdatum = result.get("rechnungsdatum") or doc["rechnungsdatum"]

    positionen = result.get("positionen") or []
    match_infos = []

    for pos in positionen:
        pos_betrag     = _parse_betrag(str(pos.get("rechnungsbetrag", "")))
        pos_erstattung = _parse_betrag(str(pos.get("erstattungsbetrag", "")))
        leistungserbringer = pos.get("leistungserbringer", "")
        zeitraum           = pos.get("zeitraum", "")

        prozent = None
        if pos_betrag and pos_erstattung and pos_betrag > 0:
            prozent = round(pos_erstattung / pos_betrag * 100, 1)

        # Match suchen
        rechnung_row = None
        if pos_betrag and adressat:
            rechnung_row = con.execute(
                """SELECT r.id FROM rechnungen r
                   JOIN dokumente d ON d.id = r.dokument_id
                   WHERE d.adressat = ?
                     AND ABS(r.rechnungsbetrag - ?) <= 1.0
                     AND r.status = 'offen'
                   ORDER BY r.id DESC LIMIT 1""",
                (adressat, pos_betrag)
            ).fetchone()

        rechnung_id = None
        if rechnung_row:
            rechnung_id = rechnung_row["id"]
            new_status = "erstattet" if prozent and prozent >= 99 else "teilweise_erstattet"
            con.execute(
                "UPDATE rechnungen SET status = ?, erstattungsdatum = ? WHERE id = ?",
                (new_status, rechnungsdatum, rechnung_id)
            )

        con.execute(
            """INSERT INTO erstattungspositionen
               (dokument_id, rechnung_id, leistungserbringer, zeitraum,
                rechnungsbetrag, erstattungsbetrag, erstattungsprozent)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (dok_id, rechnung_id, leistungserbringer, zeitraum,
             pos_betrag, pos_erstattung, prozent)
        )

        match_infos.append({
            "leistungserbringer": leistungserbringer,
            "betrag": pos_betrag,
            "prozent": prozent,
            "matched": rechnung_id is not None,
        })

    return match_infos


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Kurze Pause damit docker network connect vor dem ersten API-Aufruf erfolgen kann
    startup_wait = int(os.environ.get("STARTUP_WAIT", "0"))
    if startup_wait > 0:
        log.info(f"Warte {startup_wait}s auf Netzwerk-Setup...")
        time.sleep(startup_wait)

    log.info("=== Batch-Reimport: Leistungsabrechnungen ===")
    log.info(f"Originale-Dir: {ORIGINALE_DIR}")
    log.info(f"DB:            {DB_FILE}")

    if not CONFIG_FILE.exists():
        log.error(f"Config nicht gefunden: {CONFIG_FILE}")
        return

    with open(CONFIG_FILE, encoding="utf-8") as f:
        categories = yaml.safe_load(f).get("categories", {})

    with get_db() as con:
        pending = find_pending(con)

    log.info(f"Zu verarbeiten: {len(pending)} Leistungsabrechnungen")

    if not pending:
        log.info("Nichts zu tun.")
        return

    stats = {"ok": 0, "no_positionen": 0, "docling_fail": 0, "ollama_fail": 0, "wrong_type": 0}

    for i, doc in enumerate(pending, 1):
        log.info(f"[{i}/{len(pending)}] {doc['dateiname']}")

        # Docling
        md = convert_to_markdown(doc["pdf_path"])
        if not md:
            log.warning(f"  → Docling fehlgeschlagen, überspringe")
            stats["docling_fail"] += 1
            continue

        # Ollama
        result = classify_with_ollama(md, categories)
        if not result:
            log.warning(f"  → Ollama fehlgeschlagen, überspringe")
            stats["ollama_fail"] += 1
            continue

        type_id = result.get("type_id", "")
        if type_id not in LEISTUNGSABRECHNUNG_TYPES:
            log.warning(f"  → Falsch klassifiziert als '{type_id}', überspringe")
            stats["wrong_type"] += 1
            continue

        positionen = result.get("positionen") or []
        if not positionen:
            log.warning(f"  → Keine Positionen extrahiert")
            stats["no_positionen"] += 1
            # Trotzdem fortsetzen — leere Positionen-Liste ist möglich (kein Fehler)
            continue

        # DB: Positionen eintragen
        with get_db() as con:
            match_infos = insert_positionen(con, doc, result)

        matched = sum(1 for m in match_infos if m["matched"])
        log.info(f"  → {len(match_infos)} Positionen, {matched} Rechnungen gematcht")
        for m in match_infos:
            icon = "✅" if m["matched"] else "❌"
            pct = f" ({m['prozent']}%)" if m["prozent"] else ""
            log.info(f"     {icon} {m['leistungserbringer']} → {m['betrag']}{pct}")

        stats["ok"] += 1

        if PAUSE_SECONDS > 0:
            time.sleep(PAUSE_SECONDS)

    log.info("=== Fertig ===")
    log.info(f"Erfolgreich:         {stats['ok']}")
    log.info(f"Keine Positionen:    {stats['no_positionen']}")
    log.info(f"Docling-Fehler:      {stats['docling_fail']}")
    log.info(f"Ollama-Fehler:       {stats['ollama_fail']}")
    log.info(f"Falscher Typ:        {stats['wrong_type']}")

    # Abschlussbericht aus DB
    with get_db() as con:
        n_ep = con.execute("SELECT COUNT(*) FROM erstattungspositionen").fetchone()[0]
        n_matched = con.execute("SELECT COUNT(*) FROM erstattungspositionen WHERE rechnung_id IS NOT NULL").fetchone()[0]
        n_updated = con.execute("SELECT COUNT(*) FROM rechnungen WHERE status != 'offen'").fetchone()[0]
        log.info(f"\nDB-Stand:")
        log.info(f"  Erstattungspositionen gesamt:  {n_ep}")
        log.info(f"  Davon mit Rechnungs-Match:     {n_matched}")
        log.info(f"  Rechnungen als erstattet markiert: {n_updated}")


if __name__ == "__main__":
    main()
