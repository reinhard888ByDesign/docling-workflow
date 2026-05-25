#!/usr/bin/env python3
"""Extrahiert Immobilien-Dokumentdaten aus PDFs und schreibt sie in immobilien.db.

Aufruf:
  analyze.py pdf <pfad.pdf>                  # PDF → pdftotext → Ollama → DB
  analyze.py text "<text>" --quelle <pdf>     # Text direkt analysieren
  analyze.py --init                           # DB-Schema anlegen
  analyze.py --list [--objekt OBJ] [--jahr Y] # Eintraege anzeigen
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "immobilien.db"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("IMMO_EXTRACT_MODEL", "qwen3:4b-instruct")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))

# Keyword-Matching fuer Objekterkennung (deterministisch, Vorrang)
OBJEKT_KEYWORDS = [
    ("vm_1", [r"lipowsky"]),
    ("vm_2", [r"kornstraße", r"kornstr"]),
    ("vm_3", [r"kolberger", r"troltsch"]),
    ("vm_4", [r"schießhaus", r"schiesshaus"]),
    ("vm_5", [r"schechen"]),
    ("vm_6", [r"via dell'ospedale", r"via dell.ospedale"]),
    ("eigen_2", [r"podere dei venti"]),
    ("eigen_1", [r"grassauer", r"übersee"]),
]


# ── Datenbank ──────────────────────────────────────────────────────────────────

def get_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(db_path: Path = DEFAULT_DB) -> None:
    if SCHEMA_SQL.exists():
        schema = SCHEMA_SQL.read_text(encoding="utf-8")
    else:
        print("Fehler: schema.sql nicht gefunden", file=sys.stderr)
        sys.exit(1)
    with get_db(db_path) as con:
        con.executescript(schema)
    print(f"DB initialisiert: {db_path}")


# ── PDF → Text ─────────────────────────────────────────────────────────────────

def pdf_to_text(pdf_path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext fehlgeschlagen: {result.stderr}")
    text = result.stdout.strip()
    if len(text) < 50:
        result = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=60,
        )
        text = result.stdout.strip()
    return text


# ── Objekt-Erkennung ───────────────────────────────────────────────────────────

def match_objekt_keyword(text: str) -> str | None:
    """Keyword-Matching im PDF-Text (deterministisch, Vorrang)."""
    t = text.lower()
    for obj_id, patterns in OBJEKT_KEYWORDS:
        for pat in patterns:
            if pat in t:
                return obj_id
    return None


# ── Ollama ─────────────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Du bist ein Spezialist fuer die Extraktion strukturierter Daten \
aus Immobilien-Dokumenten.
Extrahiere alle relevanten Informationen und gib sie als JSON zurueck.

Aktive Objekte:
- eigen_2: Podere dei venti, Seggiano (IT) — ab 2022
- vm_1: Lipowskystrasse 17, Muenchen
- vm_2: Kornstrasse, Bremen
- vm_3: Kolberger Strasse, Karlsruhe (Hausverwaltung: Troltsch)
- vm_4: Schiesshausstrasse, Neuburg
- vm_5: Blumenstrasse 18, Schechen
- vm_6: Via dell'ospedale, Seggiano
- eigen_1: Grassauer Strasse 64, Uebersee (DE) — VERKAUFT 2022, nur noch historisch

Wichtige Regeln fuer betrag_eur-Felder:
- Nur tatsaechlich gezahlte oder zu zahlende Geldbetraege eintragen
- Zaehlerstaende (kWh, m³) sind KEINE Geldbetraege → null
- Versicherungssumme / Deckungssumme ist KEINE Praemie → null (nur Versicherungsbeitrag/Praemie eintragen)
- Grundschulden, Hypotheken, Nennwerte von Loeschungen → null
- Planungsbetraege, Investitionsvolumina aus Informationsschreiben → null
- Gesamtsummen aus Betriebskostenabrechnungen des gesamten Hauses → nur Reinhards Anteil

Gib folgendes JSON zurueck (keine anderen Felder, kein Markdown):
{
  "objekt_id": "<ID aus Liste oben oder null>",
  "doktyp": "<betriebskostenabrechnung|rechnung|mietvertrag|grundsteuer|hausgeld|versicherung|sonstiges>",
  "datum_dokument": "<YYYY-MM-DD oder null>",
  "betrag_eur": <tatsaechlich gezahlter Gesamtbetrag als Zahl oder null>,
  "absender": "<Aussteller/Lieferant>",
  "positionen": [
    {
      "beschreibung": "<Positionstext>",
      "zeitraum": "<z.B. 2025-01 oder Q1/2025 oder 2025 oder null>",
      "betrag_eur": <tatsaechlich gezahlter Betrag oder null>,
      "kostenart": "<grundsteuer|hausgeld|strom|wasser|gas|heizung|reparatur|versicherung|verwaltung|abfallbeseitigung|tilgung|sonstiges>"
    }
  ],
  "mieter": "<Name des Mieters wenn erkennbar, sonst null>",
  "nachzahlung_eur": <Nachzahlungs- oder Guthabenbetrag wenn BKA, sonst null>
}

Dokumenttext:
"""


def ollama_extract(text: str, model: str = OLLAMA_MODEL) -> dict:
    base = OLLAMA_URL.rstrip("/")
    prompt = EXTRACTION_PROMPT + text[:12000]

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 4096,
        },
    }

    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        f"{base}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama nicht erreichbar ({base}): {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama Antwort nicht parse-bar: {e}")

    response = data.get("response", "")
    if not response:
        raise RuntimeError("Ollama lieferte leere Antwort")

    return _parse_json_response(response)


def _repair_json(json_str: str) -> str:
    """Repariert haeufige LLM-JSON-Fehler."""
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start >= 0 and end > start:
        json_str = json_str[start:end + 1]

    json_str = re.sub(
        r':\s*(\d+\.\d+\.\d+(?:\.\d+)?)\s*([,}\]])',
        r': "\1" \2',
        json_str,
    )
    json_str = re.sub(
        r':\s*(\d+[A-Za-z/][A-Za-z0-9/\-]*)\s*([,}\]])',
        r': "\1" \2',
        json_str,
    )
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
    json_str = re.sub(r'\}\s*\{', '},{', json_str)
    json_str = re.sub(r'"\s*\]\s*$', '"]', json_str)
    json_str = re.sub(r'"\s*\}\s*$', '"}', json_str)

    open_braces = json_str.count("{")
    close_braces = json_str.count("}")
    open_brackets = json_str.count("[")
    close_brackets = json_str.count("]")
    if open_braces > close_braces:
        json_str += "}" * (open_braces - close_braces)
    if open_brackets > close_brackets:
        json_str += "]" * (open_brackets - close_brackets)

    return json_str


def _parse_json_response(response: str) -> dict:
    json_str = response.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", json_str)
    if m:
        json_str = m.group(1).strip()

    json_str = _repair_json(json_str)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Konnte JSON nicht parsen. Ollama-Antwort:\n{json_str[:800]}"
        )

    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Erwarte JSON-Objekt, bekam {type(data).__name__}")

    return data


# ── DB-Insert ──────────────────────────────────────────────────────────────────

def _norm_datum(d: str) -> str | None:
    if not d:
        return None
    d = d.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", d)
    if m:
        day, month, year = m.groups()
        if len(year) == 2:
            y = int(year)
            year = str(2000 + y) if y <= 30 else str(1900 + y)
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return d


def _to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", ".").replace("€", "").replace("EUR", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ── Betrag-Bereinigung ─────────────────────────────────────────────────────────

# Beschreibungen die auf Nicht-Geldbeträge hinweisen
_BESCHR_BLACKLIST_RE = re.compile(r"""
    zählerstand | zählerstandserfassung | stromzähler |
    versicherungssumme |
    löschung.{0,20}grundschuld | löschung.{0,20}grundpfandrecht |
    hypothek.{0,20}grundschuld | grundschuld.{0,20}hypothek |
    grundschulden.*rentenschuld |
    generalplan | bauwerksprüfung | planungsprojekte |
    deichamtswahl | deichverstärkungs | deicherhöhungs |
    baukostenzuschuss |
    eintragung.*grundschuld | grundschuld.*eintragung
""", re.IGNORECASE | re.VERBOSE)

# Absender deren Dokumente keine echten Zahlungen enthalten
_ABSENDER_BLACKLIST_RE = re.compile(
    r"bremischer deichverband",
    re.IGNORECASE,
)

# Plausibilitätsgrenzen je kostenart (€ pro Position)
# Werte oberhalb → kein echter Zahlungsbetrag → None
_KOSTENART_MAX = {
    "strom":             800_000,  # große PV-Einspeisevergütung möglich
    "elektrizität":      800_000,
    "energie":           800_000,
    "elektrik":           50_000,
    "grundsteuer":        15_000,
    "wasser":             10_000,
    "gas":                10_000,
    "heizung":            80_000,
    "versicherung":       80_000,  # Prämien; Versicherungssummen per Beschreibung gefiltert
    "verwaltung":         50_000,
    "reinigung":          20_000,
    "abfallbeseitigung":  10_000,
    "hausgeld":          500_000,  # Hausgeld-Gesamtabrechnung ganzes Gebäude
}


def _sanitize_betrag(betrag, beschreibung: str, kostenart: str, absender: str) -> float | None:
    """Gibt None zurück wenn der Betrag kein echter Geldbetrag ist."""
    val = _to_float(betrag)
    if val is None:
        return None

    # Absender-Blacklist: Informationsschreiben von Verbänden/Behörden
    if _ABSENDER_BLACKLIST_RE.search(absender or ""):
        return None

    # Beschreibungs-Blacklist: Zählerstände, Versicherungssummen, Grundschuld-Nennwerte
    if _BESCHR_BLACKLIST_RE.search(beschreibung or ""):
        return None

    # Kostenart-Obergrenzen
    art = (kostenart or "").lower()
    max_val = _KOSTENART_MAX.get(art)
    if max_val is not None and abs(val) > max_val:
        return None

    return val


def insert_dokument(
    data: dict,
    quelle_pdf: str,
    rohtext: str = "",
    db_path: Path = DEFAULT_DB,
    force: bool = False,
) -> tuple[int, int]:
    """Schreibt Dokument + Positionen + Mietvorgang in immobilien.db.
    Returns (neue_dokumente, neue_positionen)."""
    rohtext_md5 = hashlib.md5(rohtext.encode("utf-8")).hexdigest() if rohtext else ""

    positionen = data.get("positionen") or []
    if isinstance(positionen, dict):
        positionen = [positionen]

    with get_db(db_path) as con:
        dok_id = None
        neu_dok = 0

        try:
            if force:
                con.execute("DELETE FROM dokumente WHERE quelle_pdf = ?", (quelle_pdf,))
                con.commit()

            con.execute(
                """INSERT INTO dokumente
                   (quelle_pdf, objekt_id, kategorie, doktyp, absender,
                    datum_dokument, betrag_eur, rohtext_md5)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quelle_pdf,
                    data.get("objekt_id"),
                    data.get("kategorie", ""),
                    data.get("doktyp"),
                    data.get("absender"),
                    _norm_datum(data.get("datum_dokument") or ""),
                    _to_float(data.get("betrag_eur")),
                    rohtext_md5,
                ),
            )
            con.commit()
            dok_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            neu_dok = 1
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT id FROM dokumente WHERE quelle_pdf = ?", (quelle_pdf,)
            ).fetchone()
            if row:
                dok_id = row[0]

        if dok_id is None:
            return 0, 0

        absender = data.get("absender", "")
        neu_pos = 0
        for pos in positionen:
            betrag_clean = _sanitize_betrag(
                pos.get("betrag_eur"),
                beschreibung=pos.get("beschreibung", ""),
                kostenart=pos.get("kostenart", ""),
                absender=absender,
            )
            try:
                con.execute(
                    """INSERT INTO positionen
                       (dokument_id, beschreibung, zeitraum, betrag_eur, kostenart, hinweise)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        dok_id,
                        pos.get("beschreibung", ""),
                        pos.get("zeitraum"),
                        betrag_clean,
                        pos.get("kostenart"),
                        pos.get("hinweise"),
                    ),
                )
                con.commit()
                neu_pos += 1
            except sqlite3.IntegrityError:
                pass

        # Mietvorgang (nur wenn vermietet + relevante Daten)
        if data.get("mieter") or data.get("nachzahlung_eur") is not None:
            try:
                con.execute(
                    """INSERT INTO mietvorgaenge
                       (dokument_id, objekt_id, mieter, zeitraum, typ, betrag_eur, nachzahlung_eur, hinweise)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dok_id,
                        data.get("objekt_id"),
                        data.get("mieter"),
                        data.get("zeitraum"),
                        data.get("doktyp"),
                        _to_float(data.get("betrag_eur")),
                        _to_float(data.get("nachzahlung_eur")),
                        None,
                    ),
                )
                con.commit()
            except Exception:
                pass

    return neu_dok, neu_pos


# ── Kommandos ──────────────────────────────────────────────────────────────────

def cmd_init(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    init_db(db)


def cmd_pdf(args):
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"Fehler: PDF nicht gefunden: {pdf}", file=sys.stderr)
        sys.exit(1)

    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        init_db(db)

    print(f"Extrahiere Text aus: {pdf.name}")
    text = pdf_to_text(pdf)
    print(f"  -> {len(text)} Zeichen")

    _extract_and_store(text, quelle=str(pdf), db=db, model=args.model, force=args.force)


def cmd_text(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        init_db(db)

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = args.text

    quelle = args.quelle or "text-input"
    print(f"Analysiere Text ({len(text)} Zeichen)...")
    _extract_and_store(text, quelle=quelle, db=db, model=args.model, force=args.force)


def _extract_and_store(text: str, quelle: str, db: Path, model: str = OLLAMA_MODEL, force: bool = False):
    # Objekt-Erkennung: erst Keyword, dann LLM
    kw_obj = match_objekt_keyword(text)
    if kw_obj:
        print(f"Objekt via Keyword: {kw_obj}")

    print(f"Analysiere mit Ollama ({model})...")
    data = ollama_extract(text, model=model)

    # Keyword-Match hat Vorrang vor LLM
    if kw_obj and data.get("objekt_id") != kw_obj:
        print(f"  [Info] LLM sagt '{data.get('objekt_id')}', Keyword sagt '{kw_obj}' — nehme Keyword")
        data["objekt_id"] = kw_obj
    elif not data.get("objekt_id") and kw_obj:
        data["objekt_id"] = kw_obj

    print(f"\nExtrahiert:")
    print(f"  Objekt:     {data.get('objekt_id', '?')}")
    print(f"  Doktyp:     {data.get('doktyp', '?')}")
    print(f"  Datum:      {data.get('datum_dokument', '?')}")
    print(f"  Betrag:     {data.get('betrag_eur', '?')}")
    print(f"  Absender:   {data.get('absender', '?')}")
    print(f"  Mieter:     {data.get('mieter', '?')}")
    print(f"  Nachzahlung:{data.get('nachzahlung_eur', '?')}")

    positionen = data.get("positionen") or []
    if positionen:
        print(f"\n{len(positionen)} Position(en):")
        for i, pos in enumerate(positionen, 1):
            print(f"  {i}. {pos.get('beschreibung','?')} | "
                  f"{pos.get('zeitraum','?')} | "
                  f"{pos.get('betrag_eur','?')}€ | "
                  f"{pos.get('kostenart','?')}")

    neu_dok, neu_pos = insert_dokument(
        data, quelle_pdf=str(quelle), rohtext=text,
        db_path=db, force=force,
    )
    print(f"\nDB: {neu_dok} Dok neu, {neu_pos} Pos neu -> {db}")


def cmd_list(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht. Nutze --init zum Anlegen.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        where = []
        params = []
        if args.objekt:
            where.append("d.objekt_id = ?")
            params.append(args.objekt)
        if args.jahr:
            where.append("strftime('%Y', d.datum_dokument) = ?")
            params.append(str(args.jahr))

        sql = (
            "SELECT d.quelle_pdf, d.objekt_id, d.kategorie, d.doktyp, "
            "d.datum_dokument, d.betrag_eur, d.absender, "
            "o.bezeichnung "
            "FROM dokumente d "
            "LEFT JOIN objekte o ON o.id = d.objekt_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.datum_dokument DESC LIMIT 200"

        rows = con.execute(sql, params).fetchall()
        if not rows:
            print("Keine Eintraege gefunden.")
            return

        print(f"{'Objekt':25s} {'Typ':20s} {'Datum':12s} {'Betrag':>10s} {'Absender':20s} PDF")
        print("-" * 120)
        for r in rows:
            print(f"{r['bezeichnung'] or r['objekt_id'] or '?':25s} "
                  f"{r['doktyp'] or '?':20s} "
                  f"{r['datum_dokument'] or '?':12s} "
                  f"{r['betrag_eur'] or 0:>10.2f} "
                  f"{r['absender'] or '?':20s} "
                  f"{r['quelle_pdf'] or ''}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Immobilien-Dokument Extraktor")
    parser.add_argument("--db", default=None, help=f"Pfad zur SQLite-DB (default: {DEFAULT_DB})")
    parser.add_argument("--model", default=OLLAMA_MODEL, help=f"Ollama-Modell (default: {OLLAMA_MODEL})")
    parser.add_argument("--force", action="store_true", help="Bestehende Eintraege ueberschreiben")

    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="DB initialisieren")
    p_init.set_defaults(func=lambda a: cmd_init(a))

    p_pdf = sub.add_parser("pdf", help="PDF analysieren")
    p_pdf.add_argument("pdf", help="Pfad zur PDF-Datei")
    p_pdf.set_defaults(func=cmd_pdf)

    p_text = sub.add_parser("text", help="Text analysieren (statt PDF)")
    p_text.add_argument("text", nargs="?", help="Text (wenn kein --file)")
    p_text.add_argument("--file", default=None, help="Text-Datei lesen")
    p_text.add_argument("--quelle", default=None, help="Quell-PDF fuer Referenz")
    p_text.set_defaults(func=cmd_text)

    p_list = sub.add_parser("list", help="Gespeicherte Eintraege anzeigen")
    p_list.add_argument("--objekt", help="Nach Objekt-ID filtern (z.B. vm_1)")
    p_list.add_argument("--jahr", type=int, help="Nach Jahr filtern")
    p_list.set_defaults(func=cmd_list)

    args, unknown = parser.parse_known_args()
    if unknown and unknown[0] in ("--init", "--list"):
        if unknown[0] == "--init":
            cmd_init(args)
        else:
            cmd_list(args)
        return

    if args.cmd is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
