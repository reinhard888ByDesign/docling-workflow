#!/usr/bin/env python3
"""Extrahiert KFZ-Dokumente (Versicherungen, Schaeden, Werkstatt, Steuer, Zulassung)
und schreibt sie in die SQLite-DB kfz.db.

Aufruf:
  analyze.py init                         # DB anlegen
  analyze.py pdf <pfad.pdf>               # PDF -> pdftotext -> Ollama -> DB
  analyze.py text "<text>" --quelle <pdf> # Wilson-Bypass
  analyze.py list [--kfz ID] [--typ TYP] [--jahr YYYY]
  analyze.py aktiv                        # Nur aktive Versicherungen
  analyze.py kosten [--kfz ID]            # Kostenuebersicht pro Fahrzeug
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "kfz.db"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b-instruct")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))


# ── Keyword-Matching (deterministisch, vor LLM) ─────────────────────────────────

KFZ_KENNZEICHEN = [
    ("kfz_1",     [r"gy\s*243\s*zf", r"ts[-\s]?my\s*8888"]),        # Tesla Model Y IT + ehem. DE
    ("kfz_2",     [r"xb\s*fs\s*l4"]),                                 # Piaggio Ape
    ("kfz_3",     [r"fr[-\s]?y\s*1544"]),                             # Anhänger alt DE
    ("kfz_4",     [r"xa\s*328\s*yk", r"ts[-\s]?qz\s*566"]),          # Anhänger IT (XA328YK) + ehem. DE (TS-QZ566)
    ("kfz_5",     [r"bd\s*837\s*h", r"carraro", r"ttr\s*4400", r"tigretrac"]),  # Traktor
    ("kfz_6",     [r"gy\s*9[46]4\s*zf", r"mb\s*930145", r"mb\s*661232"]),  # Mitsubishi L200 IT + ehem. Policen
    ("kfz_alt_1", [r"ts[-\s]?rj\s*801"]),                             # Mini DE (abgem.)
    ("kfz_alt_2", [r"ts[-\s]?rj\s*8888"]),                            # Mitsubishi L200 DE (abgem., jetzt GY964ZF)
]

KFZ_DOKTYP_KEYWORDS = [
    ("versicherung", [r"polizza", r"versicherungsschein", r"versicherungsvertrag",
                       r"carta verde", r"green card", r"deckungskarte",
                       r"versicherungsangebot", r"preventivo.*assicurazione",
                       r"assicurazione.*preventivo", r"versicherungszertifikat",
                       r"certificato.*assicurazione", r"assicurazione.*certificato",
                       r"deckungsanfrage", r"beitragsrechnung", r"beitragsanpassung",
                       r"versicherungspolice", r"polizza.*assicurazione"]),
    ("schaden",      [r"schadenmeldung", r"schadensmeldung", r"sinistro",
                       r"schadenanzeige", r"denuncia", r"incidente",
                       r"schadenfall", r"schaden.*kfz", r"kfz.*schaden"]),
    ("reparatur",    [r"werkstatt", r"officina", r"reparatur", r"wartung",
                       r"inspezione", r"manutenzione", r"kollaudo",
                       r"riparazione", r"gommista", r"pneumatici",
                       r"reifenwechsel", r"reifen.*wechsel"]),
    ("steuer",       [r"kraftfahrzeugsteuer", r"bollo auto", r"tassa automobilistica",
                       r"superbollo", r"kfz.*steuer", r"steuer.*kfz",
                       r"bollo.*assicurazione"]),
    ("zulassung",    [r"fahrzeugschein", r"carta di circolazione",
                       r"zulassungsbescheinigung", r"immatricolazione",
                       r"fahrzeugbrief", r"libretto.*circolazione"]),
]

# Keywords fuer Dokumente die SICHER keine KFZ-Dokumente sind.
# Konservativ halten — falsche Treffer schlimmer als verpasste.
KFZ_AUSSCHLUSS_KEYWORDS = [
    # Sach-/Immobilienversicherungen die keine KFZ sind
    r"casamia", r"realmente\s*protetti", r"ecologica\s*reale\s*solare",
    # Datenschutz/AGB
    r"datenschutz", r"privacy",
    r"connected\s*drive", r"allgemeine\s*geschäftsbedingungen",
    r"sepa[-\s]*mandat", r"einzugsermächtigung",
    # Keine Versicherungsdokumente
    r"einlagerungsschein",
    r"ordnungswidrigkeit", r"bußgeld", r"geschwindigkeitsüberschreitung",
    r"kaufvertrag", r"compravendita",
    r"fahrradträger", r"fahrradhalter", r"porta\s*bici",
    r"tüv[-\s]bericht", r"hauptuntersuchung",
    r"carta\s*di\s*circolazione", r"fahrzeugbrief", r"fahrzeugschein",
    r"kfz[-\s]steuer", r"bollo\s*auto",
]


def match_kfz(text: str) -> str | None:
    """Findet Fahrzeug-ID via Kennzeichen-Regex. Erster Treffer gewinnt."""
    t = text.lower()
    for fzg_id, patterns in KFZ_KENNZEICHEN:
        for pat in patterns:
            if re.search(pat, t):
                return fzg_id
    return None


def match_doktyp(text: str) -> str:
    """Erkennt den Dokumenttyp via Keyword. Erster Treffer gewinnt.
    Ausschluss-Keywords ueberschreiben ALLES — diese Dokumente sind keine KFZ-Dokumente."""
    t = text.lower()
    # Ausschluss: Dokumente die sicher keine KFZ-Dokumente sind
    for pat in KFZ_AUSSCHLUSS_KEYWORDS:
        if re.search(pat, t):
            return "sonstiges"
    for doktyp, patterns in KFZ_DOKTYP_KEYWORDS:
        for pat in patterns:
            if re.search(pat, t):
                return doktyp
    return "sonstiges"


def is_ausschluss(text: str) -> bool:
    """Prueft ob ein Dokument aufgrund von Ausschluss-Keywords kein KFZ-Dokument ist."""
    t = text.lower()
    for pat in KFZ_AUSSCHLUSS_KEYWORDS:
        if re.search(pat, t):
            return True
    return False


# ── Datenbank ──────────────────────────────────────────────────────────────────

def get_db(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
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
        schema = textwrap.dedent("""\
            CREATE TABLE IF NOT EXISTS fahrzeuge (
                id TEXT PRIMARY KEY, kennzeichen TEXT NOT NULL UNIQUE,
                typ TEXT, marke TEXT, modell TEXT, baujahr INTEGER,
                land TEXT DEFAULT 'IT', aktiv INTEGER DEFAULT 1,
                abgemeldet TEXT, bemerkung TEXT
            );
        """)
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


# ── Ollama ─────────────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Du bist Experte fuer KFZ-Dokumente (Deutsch und Italienisch).
Extrahiere alle relevanten Informationen und gib sie als JSON zurueck.

Aktive Fahrzeuge:
- kfz_1: GY243ZF (Tesla Model Y, IT — ehemals TS-MY8888 DE) — Versicherer: Allianz Next, Polizza 539239246
- kfz_2: XBFSL4 (Piaggio Ape, IT, angeschafft 2025-08-18) — Versicherer: Reale Mutua (Automia Reale), Polizza 3201203
- kfz_3: FR-Y1544 (Anhaenger, DE) — Versicherer: WGV
- kfz_4: XA328YK (Anhaenger, IT — ehemals TS-QZ566 DE) — Versicherer: WGV
- kfz_5: BD837H (Antonio Carraro TTR 4400 Tigretrac HST, Traktor, IT) — Versicherer: Reale Mutua, Polizza 2023/354109
- kfz_6: GY964ZF (Mitsubishi L200, IT — ehemals TS-RJ8888 DE) — Versicherer: Zurich Insurance Europe AG, Police MB930145 (Vorgaenger MB661232), Intermediar: Eisendle SRL Bolzano

Altfahrzeuge (aktiv=0, nur fuer historische Zuordnung):
- kfz_alt_1: TS-RJ801 (PKW DE, nicht mehr im Bestand)
- kfz_alt_2: TS-RJ8888 (Mitsubishi L200 DE, umgemeldet IT als GY964ZF ~Nov 2025)

ACHTUNG — Reale Mutua ist auch Sach-/Immobilienversicherer! Nur KFZ-Policen (RC Auto, Kasko etc.)
extraheren. Policen fuer CASAMIA, Realmente Protetti, Ecologica Reale Solare oder Wohngebaeude
sind KEINE KFZ-Dokumente → doktyp: "sonstiges", fahrzeug_id: null.

Dokumenttypen:
- versicherung: NUR echte KFZ-Versicherungspolicen/Beitragsrechnungen (KEINE AGB, KEINE
  Servicekarten, KEINE Mitteilungen ueber Reglements-Aenderungen, KEINE Datenschutz-Erklaerungen)
- schaden: Schadensmeldung/Sinistro/Denuncia
- reparatur: Werkstattrechnung/Officina/Wartung/TUEV/Collaudo
- steuer: KFZ-Steuer/Bollo Auto
- zulassung: Fahrzeugschein/Carta di Circolazione
- sonstiges: ALLES was kein KFZ-Dokument ist (AGB, Datenschutz, Kaufvertraege, TUEV-Berichte,
  Reglements-Mitteilungen, Servicekarten, Einlagerungsscheine, Ordnungswidrigkeiten, etc.)

WICHTIG:
- Italienische Deckungsarten: "RC Auto" = HP, "Kasko" = VK, "Mini Kasko" = TK
- Praemie IMMER als Jahresbetrag (nicht Rate). Wenn nur Rate angegeben: hochrechnen.
- Datum NORMALISIEREN auf JJJJ-MM-TT. Zweistellige Jahre: 00-30 → 20xx, 31-99 → 19xx.
- Betraege als ZAHLEN (nicht String), mit Punkt als Dezimaltrenner.
- Nur das JSON zurueckgeben, keinen anderen Text.
- Wenn du nicht sicher bist ob es ein KFZ-Dokument ist: doktyp="sonstiges", fahrzeug_id=null.
  Lieber nichts extrahieren als falsche Daten in die DB schreiben.

Gib ausschliesslich dieses JSON zurueck (kein Markdown):
{
  "fahrzeug_id": "<kfz_1|kfz_2|kfz_3|kfz_4|kfz_5|kfz_6|kfz_alt_1|kfz_alt_2|null>",
  "kennzeichen": "<Kennzeichen aus Dokument>",
  "doktyp": "<versicherung|schaden|reparatur|steuer|zulassung|sonstiges>",
  "datum_dokument": "<YYYY-MM-DD oder null>",
  "versicherer": "<Name der Versicherung oder null>",
  "vertragsnummer": "<Vertragsnummer oder null>",
  "deckungsart": "<HP|TK|VK|HP+TK|HP+VK|null>",
  "praemie_eur": <Jahrespraemie als Zahl oder null>,
  "gueltig_von": "<YYYY-MM-DD oder null>",
  "gueltig_bis": "<YYYY-MM-DD oder null>",
  "schaden_eur": <Schadenhoehe oder null>,
  "status_schaden": "<gemeldet|in_bearbeitung|reguliert|abgelehnt|null>",
  "betrag_eur": <Rechnungsbetrag Werkstatt/Steuer oder null>,
  "werkstatt": "<Name oder null>",
  "art_reparatur": "<wartung|reparatur|tuev|inspektion|sonstiges|null>",
  "beschreibung": "<kurze Zusammenfassung>"
}

Dokumenttext:
"""


def ollama_extract(text: str, model: str = OLLAMA_MODEL) -> dict:
    import time as _time, socket as _socket, urllib.request as _req, urllib.error as _err
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
    MAX_RETRIES = 3
    RETRY_DELAY = 15

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            req = _req.Request(
                f"{base}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with _req.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            response = data.get("response", "")
            if not response:
                raise RuntimeError("Ollama lieferte leere Antwort")
            return _parse_json_response(response)
        except (_err.URLError, json.JSONDecodeError, RuntimeError,
                TimeoutError, _socket.timeout, ConnectionResetError, OSError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (attempt + 1)
                print(f"  Ollama-Fehler (Versuch {attempt+1}/{MAX_RETRIES}): {e}", file=sys.stderr)
                print(f"  Wiederhole in {delay}s...", file=sys.stderr)
                _time.sleep(delay)

    raise RuntimeError(f"Ollama nach {MAX_RETRIES} Versuchen nicht erreichbar ({base}): {last_error}")


def _repair_json(json_str: str) -> str:
    """Repariert haeufige LLM-JSON-Fehler ohne externe Abhaengigkeiten."""
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start >= 0 and end > start:
        json_str = json_str[start:end + 1]

    # Fix: unquoted numbers with multiple dots
    json_str = re.sub(
        r':\s*(\d+\.\d+\.\d+(?:\.\d+)?)\s*([,}\]])',
        r': "\1" \2',
        json_str,
    )
    # Fix: unquoted alphanumeric values
    json_str = re.sub(
        r':\s*(\d+[A-Za-z/][A-Za-z0-9/\-]*)\s*([,}\]])',
        r': "\1" \2',
        json_str,
    )
    # Fix: trailing comma
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
    # Fix: missing closing braces (truncation)
    open_b = json_str.count("{")
    close_b = json_str.count("}")
    if open_b > close_b:
        json_str += "}" * (open_b - close_b)

    return json_str


def _parse_json_response(response: str) -> dict:
    json_str = response.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", json_str)
    if m:
        json_str = m.group(1).strip()
    json_str = _repair_json(json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Konnte JSON nicht parsen. Ollama-Antwort:\n{json_str[:800]}"
        )


# ── Datum/Betrag Helper ────────────────────────────────────────────────────────

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


def _to_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace("%", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


# ── DB-Insert ──────────────────────────────────────────────────────────────────

def insert_kfz(data: dict, quelle_pdf: str, rohtext: str = "",
               db_path: Path = DEFAULT_DB, force: bool = False) -> tuple[int, int]:
    """Schreibt extrahierte KFZ-Daten in die passende Tabelle."""
    rohtext_md5 = hashlib.md5(rohtext.encode("utf-8")).hexdigest() if rohtext else ""
    doktyp = data.get("doktyp", "sonstiges")
    neu, skipped = 0, 0

    with get_db(db_path) as con:
        try:
            if doktyp == "versicherung":
                neu, skipped = _insert_versicherung(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp == "schaden":
                neu, skipped = _insert_schaden(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp == "reparatur":
                neu, skipped = _insert_reparatur(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp == "steuer":
                neu, skipped = _insert_steuer(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp == "zulassung":
                neu, skipped = _insert_zulassung(con, data, quelle_pdf, rohtext_md5, force)
            else:
                print(f"  [Info] Unbekannter doktyp '{doktyp}', ueberspringe.", file=sys.stderr)
        except Exception as e:
            print(f"  [Warnung] Fehler beim Einfuegen: {e}", file=sys.stderr)

    return neu, skipped


def _insert_versicherung(con, data, quelle_pdf, rohtext_md5, force):
    kfz_id = data.get("fahrzeug_id")
    vers = data.get("versicherer") or "Unbekannt"
    vnr = data.get("vertragsnummer")
    da = data.get("deckungsart")
    praemie = _to_float(data.get("praemie_eur"))
    von = _norm_datum(data.get("gueltig_von") or "")
    bis = _norm_datum(data.get("gueltig_bis") or "")

    # Dedup: identische Police (gleiches Fahrzeug + Versicherer + Vertragsnummer +
    # Deckungsart + Prämie + Zeitraum) nicht erneut einfügen
    if not force and kfz_id and vnr:
        dup = con.execute("""
            SELECT id FROM versicherungen
            WHERE fahrzeug_id = ? AND vertragsnummer = ?
              AND COALESCE(deckungsart,'') = COALESCE(?,'')
              AND COALESCE(gueltig_von,'') = COALESCE(?,'')
              AND COALESCE(gueltig_bis,'') = COALESCE(?,'')
            LIMIT 1
        """, (kfz_id, vnr, da or "", von or "", bis or "")).fetchone()
        if dup:
            print(f"  [Dedup] Police bereits vorhanden (ID {dup[0]}), ueberspringe.")
            return 0, 1

    sql = """INSERT INTO versicherungen
             (fahrzeug_id, versicherer, vertragsnummer, deckungsart,
              praemie_eur, praemie_periode, gueltig_von, gueltig_bis,
              aktiv, quelle_pdf, rohtext_md5)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            kfz_id,
            vers,
            vnr,
            da,
            praemie,
            data.get("praemie_periode"),
            von,
            bis,
            quelle_pdf,
            rohtext_md5,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_schaden(con, data, quelle_pdf, rohtext_md5, force):
    kfz_id = data.get("fahrzeug_id")
    sql = """INSERT INTO schaeden
             (fahrzeug_id, datum_schaden, datum_meldung, versicherer,
              schadennummer, hergang, schaden_eur, regulierung_eur,
              status, quelle_pdf, rohtext_md5)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            kfz_id,
            _norm_datum(data.get("datum_dokument") or ""),
            _norm_datum(data.get("datum_meldung") or ""),
            data.get("versicherer"),
            data.get("vertragsnummer"),  # Schadennummer
            data.get("beschreibung"),    # Hergang
            _to_float(data.get("schaden_eur")),
            _to_float(data.get("regulierung_eur")),
            data.get("status_schaden"),
            quelle_pdf,
            rohtext_md5,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_reparatur(con, data, quelle_pdf, rohtext_md5, force):
    sql = """INSERT INTO reparaturen
             (fahrzeug_id, datum, werkstatt, art, betrag_eur,
              beschreibung, quelle_pdf, rohtext_md5)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            data.get("fahrzeug_id"),
            _norm_datum(data.get("datum_dokument") or ""),
            data.get("werkstatt"),
            data.get("art_reparatur"),
            _to_float(data.get("betrag_eur")),
            data.get("beschreibung"),
            quelle_pdf,
            rohtext_md5,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_steuer(con, data, quelle_pdf, rohtext_md5, force):
    datum = _norm_datum(data.get("datum_dokument") or "")
    jahr = int(datum[:4]) if datum and len(datum) >= 4 else None
    sql = """INSERT INTO steuern
             (fahrzeug_id, jahr, betrag_eur, faellig, bezahlt, quelle_pdf)
             VALUES (?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            data.get("fahrzeug_id"),
            jahr,
            _to_float(data.get("betrag_eur")),
            _norm_datum(data.get("faellig") or ""),
            _norm_datum(data.get("bezahlt") or ""),
            quelle_pdf,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_zulassung(con, data, quelle_pdf, rohtext_md5, force):
    sql = """INSERT INTO zulassungen
             (fahrzeug_id, doktyp, datum_ausstellung, behoerde, quelle_pdf)
             VALUES (?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            data.get("fahrzeug_id"),
            data.get("doktyp"),
            _norm_datum(data.get("datum_dokument") or ""),
            data.get("behoerde"),
            quelle_pdf,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


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


def _extract_and_store(text: str, quelle: str, db: Path,
                       model: str = OLLAMA_MODEL, force: bool = False):
    # Keyword-Matching VOR LLM
    kw_kfz = match_kfz(text)
    kw_doktyp = match_doktyp(text)
    if kw_kfz:
        print(f"  Keyword-Match Fahrzeug: {kw_kfz}")
    if kw_doktyp:
        print(f"  Keyword-Match DokTyp:   {kw_doktyp}")

    print(f"Analysiere mit Ollama ({model})...")
    data = ollama_extract(text, model=model)

    # Keyword hat Vorrang bei Fahrzeug-ID (Kennzeichen-Regex sehr zuverlaessig)
    if kw_kfz and data.get("fahrzeug_id") != kw_kfz:
        data["fahrzeug_id"] = kw_kfz

    # DokTyp: Ausschluss-Keywords ueberschreiben immer.
    # Ansonsten vertrauen wir dem LLM (der hat den Kontext gelesen).
    if kw_doktyp == "sonstiges":
        data["doktyp"] = "sonstiges"
    elif kw_doktyp and data.get("doktyp") == "sonstiges" and kw_doktyp != "sonstiges":
        # LLM sagt sonstiges, aber Keyword findet klaren KFZ-Bezug → Keyword gewinnt
        data["doktyp"] = kw_doktyp

    print(f"\n  Fahrzeug:     {data.get('fahrzeug_id', '?')}")
    print(f"  Typ:          {data.get('doktyp', '?')}")
    print(f"  Datum:        {data.get('datum_dokument', '?')}")
    print(f"  Versicherer:  {data.get('versicherer', '?')}")
    print(f"  Betrag:       {data.get('praemie_eur') or data.get('betrag_eur') or data.get('schaden_eur') or '?'}")
    print(f"  Beschreibung: {data.get('beschreibung', '?')}")

    neu, skipped = insert_kfz(
        data, quelle_pdf=str(quelle), rohtext=text,
        db_path=db, force=force,
    )
    print(f"\nDB: {neu} neu, {skipped} bereits vorhanden -> {db}")


def cmd_list(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht. Nutze --init zum Anlegen.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        # Fahrzeuge anzeigen
        fahrzeuge = con.execute(
            "SELECT * FROM fahrzeuge ORDER BY aktiv DESC, id"
        ).fetchall()
        print(f"\n=== Fahrzeuge ({len(fahrzeuge)}) ===")
        for f in fahrzeuge:
            aktiv_str = "aktiv" if f["aktiv"] else "inaktiv"
            print(f"  {f['id']:12s} {f['kennzeichen']:12s} {f['typ'] or '?':10s} "
                  f"{f['marke'] or '':10s} {f['modell'] or '':10s} [{aktiv_str}]")

        # Versicherungen
        where = []
        params = []
        if args.kfz:
            where.append("v.fahrzeug_id = ?")
            params.append(args.kfz)
        if args.typ:
            where.append("v.deckungsart = ?")
            params.append(args.typ)
        if args.jahr:
            where.append("strftime('%Y', v.gueltig_von) <= ? AND "
                         "(v.gueltig_bis IS NULL OR strftime('%Y', v.gueltig_bis) >= ?)")
            params.extend([str(args.jahr), str(args.jahr)])

        sql = """SELECT v.*, f.kennzeichen FROM versicherungen v
                 JOIN fahrzeuge f ON v.fahrzeug_id = f.id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY v.gueltig_von DESC LIMIT 100"

        rows = con.execute(sql, params).fetchall()
        if rows:
            print(f"\n=== Versicherungen ({len(rows)}) ===")
            print(f"{'FZG':12s} {'Kennz.':12s} {'Versicherer':20s} {'Art':8s} "
                  f"{'Praemie':>10s} {'von':12s} {'bis':12s}")
            print("-" * 90)
            for r in rows:
                print(f"{r['fahrzeug_id']:12s} {r['kennzeichen'] or '?':12s} "
                      f"{r['versicherer']:20s} {r['deckungsart'] or '?':8s} "
                      f"{r['praemie_eur'] or 0:>10.2f} "
                      f"{r['gueltig_von'] or '?':12s} {r['gueltig_bis'] or '?':12s}")

        # Reparaturen
        rep_where = []
        rep_params = []
        if args.kfz:
            rep_where.append("fahrzeug_id = ?")
            rep_params.append(args.kfz)
        if args.jahr:
            rep_where.append("strftime('%Y', datum) = ?")
            rep_params.append(str(args.jahr))

        rep_sql = "SELECT * FROM reparaturen"
        if rep_where:
            rep_sql += " WHERE " + " AND ".join(rep_where)
        rep_sql += " ORDER BY datum DESC LIMIT 100"

        reps = con.execute(rep_sql, rep_params).fetchall()
        if reps:
            print(f"\n=== Reparaturen/Werkstatt ({len(reps)}) ===")
            print(f"{'FZG':12s} {'Datum':12s} {'Werkstatt':25s} {'Art':12s} {'Betrag':>10s}")
            print("-" * 75)
            for r in reps:
                print(f"{r['fahrzeug_id'] or '?':12s} {r['datum'] or '?':12s} "
                      f"{r['werkstatt'] or '?':25s} {r['art'] or '?':12s} "
                      f"{r['betrag_eur'] or 0:>10.2f}")


def cmd_aktiv(args):
    """Zeigt nur aktive Versicherungen (gueltig_bis >= heute)."""
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    heute = date.today().isoformat()
    with get_db(db) as con:
        rows = con.execute(
            """SELECT v.*, f.kennzeichen FROM versicherungen v
               JOIN fahrzeuge f ON v.fahrzeug_id = f.id
               WHERE v.aktiv = 1 AND (v.gueltig_bis IS NULL OR v.gueltig_bis >= ?)
               ORDER BY v.gueltig_bis""",
            (heute,),
        ).fetchall()

        if not rows:
            print("Keine aktiven Versicherungen.")
            return

        print(f"\n=== Aktive Versicherungen ({len(rows)}) ===\n")
        for r in rows:
            bis = r["gueltig_bis"] or "unbekannt"
            warn = ""
            if r["gueltig_bis"] and r["gueltig_bis"] < date.today().replace(
                day=1).isoformat():  # pragma: no cover (heuristic)
                pass
            # Ablauf-Warnung falls < 60 Tage
            if r["gueltig_bis"]:
                try:
                    tage = (date.fromisoformat(r["gueltig_bis"]) - date.today()).days
                    if tage < 30:
                        warn = f"  *** LAEUFT IN {tage} TAGEN AB ***"
                    elif tage < 60:
                        warn = f"  ** laeuft in {tage} Tagen ab"
                except ValueError:
                    pass
            print(f"  {r['fahrzeug_id']:12s} {r['kennzeichen'] or '?':12s} "
                  f"{r['versicherer']:20s} {r['deckungsart'] or '?':8s} "
                  f"bis {bis}{warn}")


def cmd_kosten(args):
    """Kostenuebersicht pro Fahrzeug: Versicherung + Reparatur + Steuer."""
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        fzg_filter = ""
        fzg_params = ()
        if args.kfz:
            fzg_filter = "WHERE f.id = ?"
            fzg_params = (args.kfz,)

        fahrzeuge = con.execute(
            f"SELECT * FROM fahrzeuge {fzg_filter} ORDER BY aktiv DESC, id",
            fzg_params,
        ).fetchall()

        for f in fahrzeuge:
            print(f"\n=== {f['id']} {f['kennzeichen']} ({f['marke'] or ''} {f['modell'] or ''}) ===")

            # Versicherungskosten
            vers = con.execute(
                """SELECT SUM(praemie_eur) as total, COUNT(*) as anz
                   FROM versicherungen WHERE fahrzeug_id = ?""",
                (f["id"],),
            ).fetchone()
            if vers and vers["total"]:
                print(f"  Versicherung: {vers['total']:.2f} EUR ({vers['anz']} Vertraege)")

            # Reparaturkosten
            rep = con.execute(
                """SELECT SUM(betrag_eur) as total, COUNT(*) as anz
                   FROM reparaturen WHERE fahrzeug_id = ?""",
                (f["id"],),
            ).fetchone()
            if rep and rep["total"]:
                print(f"  Reparaturen:  {rep['total']:.2f} EUR ({rep['anz']} Vorgaenge)")

            # Steuern
            steuer = con.execute(
                """SELECT SUM(betrag_eur) as total, COUNT(*) as anz
                   FROM steuern WHERE fahrzeug_id = ?""",
                (f["id"],),
            ).fetchone()
            if steuer and steuer["total"]:
                print(f"  Steuern:      {steuer['total']:.2f} EUR ({steuer['anz']} Zahlungen)")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KFZ-Dokument Extraktor")
    parser.add_argument("--db", default=None, help=f"Pfad zur SQLite-DB (default: {DEFAULT_DB})")
    parser.add_argument("--model", default=OLLAMA_MODEL,
                        help=f"Ollama-Modell (default: {OLLAMA_MODEL})")
    parser.add_argument("--force", action="store_true",
                        help="Bestehende Eintraege ueberschreiben")

    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="DB initialisieren")
    p_init.set_defaults(func=cmd_init)

    p_pdf = sub.add_parser("pdf", help="PDF analysieren")
    p_pdf.add_argument("pdf", help="Pfad zur PDF-Datei")
    p_pdf.set_defaults(func=cmd_pdf)

    p_text = sub.add_parser("text", help="Text analysieren (Wilson-Bypass)")
    p_text.add_argument("text", nargs="?", help="Text (wenn kein --file)")
    p_text.add_argument("--file", default=None, help="Text-Datei lesen")
    p_text.add_argument("--quelle", default=None, help="Quell-PDF fuer Referenz")
    p_text.set_defaults(func=cmd_text)

    p_list = sub.add_parser("list", help="Gespeicherte Eintraege anzeigen")
    p_list.add_argument("--kfz", help="Nach Fahrzeug-ID filtern (z.B. kfz_1)")
    p_list.add_argument("--typ", help="Nach Deckungsart filtern")
    p_list.add_argument("--jahr", type=int, help="Nach Jahr filtern")
    p_list.set_defaults(func=cmd_list)

    p_aktiv = sub.add_parser("aktiv", help="Nur aktive Versicherungen anzeigen")
    p_aktiv.set_defaults(func=cmd_aktiv)

    p_kosten = sub.add_parser("kosten", help="Kostenuebersicht pro Fahrzeug")
    p_kosten.add_argument("--kfz", help="Nach Fahrzeug-ID filtern")
    p_kosten.set_defaults(func=cmd_kosten)

    # Legacy-Aliase: --init, --list
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
