#!/usr/bin/env python3
"""Extrahiert Altersvorsorge-Dokumente (Standmitteilungen, Versicherungsscheine,
Aenderungen) und schreibt sie in die SQLite-DB altersvorsorge.db.

Aufruf:
  analyze.py init                         # DB anlegen
  analyze.py pdf <pfad.pdf>               # PDF -> pdftotext -> Ollama -> DB
  analyze.py text "<text>" --quelle <pdf> # Wilson-Bypass
  analyze.py list [--vertrag ID] [--person NAME] [--typ TYP]
  analyze.py verlauf [--vertrag ID]       # Zeitreihe Guthaben
  analyze.py gesamt [--jahr YYYY]         # Gesamtvermoegen Altersvorsorge
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

DEFAULT_DB = Path(__file__).resolve().parent / "altersvorsorge.db"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b-instruct")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))


# ── Keyword-Matching ─────────────────────────────────────────────────────────────

AV_VERTRAEGE = [
    ("av_1", [r"20412486", r"axa colonia"]),
    ("av_2", [r"l\s*7087352", r"l\s*708735[^0-9]", r"nuernberger.*reinhard.*direkt",
              r"direktversicherung.*reinhard"]),
    ("av_3", [r"l\s*5929705", r"l\s*592970[^0-9]", r"pensionskasse.*reinhard"]),
    ("av_4", [r"l\s*8087353", r"l\s*808735[^0-9]", r"unterstuetzungskasse.*reinhard"]),
    ("av_5", [r"l\s*5087350", r"l\s*508735[^0-9]", r"nuernberger.*marion.*direkt"]),
    ("av_6", [r"unterstuetzungskasse.*marion", r"u-kasse.*marion"]),
    ("av_7", [r"73\s*088\s*025", r"lv\s*1871.*marion", r"basisrente.*marion"]),
    ("av_8", [r"hdi.*fondsgebunden", r"fonds.*rente.*hdi"]),
    ("av_9", [r"allvest"]),
]

AV_DOKTYP = [
    ("standmitteilung", [r"standmitteilung", r"stand der versicherung",
                          r"jahresinformation", r"wertmitteilung"]),
    ("versicherungsschein", [r"versicherungsschein", r"police"]),
    ("aenderung",       [r"beitragsfreistellung", r"beitragsanpassung",
                          r"vertrags.nderung", r"umbuchung"]),
    ("auszahlung",      [r"auszahlung", r"leistungsfall", r"ablauf"]),
    ("nachhaltigkeit",  [r"nachhaltigkeitsthemen", r"offenlegungsverordnung",
                          r"eu.*2019/2088"]),
]


def match_vertrag(text: str) -> str | None:
    t = text.lower()
    for vid, patterns in AV_VERTRAEGE:
        for pat in patterns:
            if re.search(pat, t):
                return vid
    return None


def match_doktyp(text: str) -> str:
    t = text.lower()
    for doktyp, patterns in AV_DOKTYP:
        for pat in patterns:
            if re.search(pat, t):
                return doktyp
    return "sonstiges"


# ── Datenbank ────────────────────────────────────────────────────────────────────

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
            CREATE TABLE IF NOT EXISTS vertraege (
                id TEXT PRIMARY KEY, versicherer TEXT NOT NULL,
                vertragsnummer TEXT, art TEXT NOT NULL,
                versicherungsnehmer TEXT, aktiv INTEGER DEFAULT 1
            );
        """)
    with get_db(db_path) as con:
        con.executescript(schema)
    print(f"DB initialisiert: {db_path}")


# ── PDF → Text ───────────────────────────────────────────────────────────────────

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


# ── Ollama ───────────────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Du bist Experte fuer deutsche Lebens- und Rentenversicherungen.
Extrahiere alle finanziellen Kennzahlen aus dem Dokument.

Bekannte Vertraege:
- av_1: AXA Lebensversicherung, VN 20412486, Reinhard, kapitalbildend (seit 1999)
- av_2: Nuernberger Direktversicherung, Reinhard (L 7087352)
- av_3: Nuernberger Pensionskasse, Reinhard (L 5929705)
- av_4: Nuernberger Unterstuetzungskasse, Reinhard (L 8087353)
- av_5: Nuernberger Direktversicherung, Marion (L 5087350)
- av_6: Nuernberger Unterstuetzungskasse, Marion
- av_7: LV1871 Basisrente, Marion (VN 73 088 025, beitragsfrei seit 2023)
- av_8: HDI fondsgebundene Rentenversicherung
- av_9: Allvest

WICHTIG:
- Falls das Dokument nur ueber Nachhaltigkeitsthemen/Offenlegungsverordnung berichtet
  und keine Kapitalwerte enthaelt, gib {"skip": true} zurueck.
- Datum NORMALISIEREN auf JJJJ-MM-TT.
- Betraege als ZAHLEN (nicht String), mit Punkt als Dezimaltrenner.
- Nuernberger: Nur die finale Spalte "per Ablauf" nehmen, nicht Zwischenwerte.
- Garantiewert und Prognosewert IMMER getrennt extrahieren.
- Nur das JSON zurueckgeben, keinen anderen Text.

Gib ausschliesslich dieses JSON zurueck (kein Markdown):
{
  "skip": false,
  "vertrag_id": "<av_1..av_9 oder null wenn unbekannt>",
  "versicherer": "<Name>",
  "vertragsnummer": "<aus Dokument>",
  "versicherungsnehmer": "<Reinhard|Marion|null>",
  "doktyp": "<standmitteilung|versicherungsschein|aenderung|auszahlung|sonstiges>",
  "datum_mitteilung": "<YYYY-MM-DD>",
  "stichtag": "<YYYY-MM-DD oder null>",
  "guthaben_eur": <aktuelles Guthaben/Rueckkaufswert als Zahl oder null>,
  "ablauf_garantie_eur": <garantierte Ablaufleistung oder null>,
  "ablauf_prognose_eur": <prognostizierte Ablaufleistung oder null>,
  "jahresrente_garantie": <garantierte Jahresrente oder null>,
  "jahresrente_prognose": <prognostizierte Jahresrente oder null>,
  "beitraege_kumuliert_eur": <Summe Einzahlungen oder null>,
  "beitrag_aktuell_eur": <aktueller Monatsbeitrag oder null>,
  "ueberschuss_eur": <Ueberschussbeteiligung oder null>,
  "art_aenderung": "<beitragsfreistellung|umbuchung|beitragsanpassung|null>"
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
        "options": {"temperature": 0.1, "num_predict": 4096},
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
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start >= 0 and end > start:
        json_str = json_str[start:end + 1]
    json_str = re.sub(r':\s*(\d+\.\d+\.\d+(?:\.\d+)?)\s*([,}\]])', r': "\1" \2', json_str)
    json_str = re.sub(r':\s*(\d+[A-Za-z/][A-Za-z0-9/\-]*)\s*([,}\]])', r': "\1" \2', json_str)
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
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
        raise RuntimeError(f"Konnte JSON nicht parsen. Antwort:\n{json_str[:800]}")


# ── Helper ───────────────────────────────────────────────────────────────────────

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


# ── DB-Insert ────────────────────────────────────────────────────────────────────

def insert_av(data: dict, quelle_pdf: str, rohtext: str = "",
              db_path: Path = DEFAULT_DB, force: bool = False) -> tuple[int, int]:
    rohtext_md5 = hashlib.md5(rohtext.encode("utf-8")).hexdigest() if rohtext else ""
    doktyp = data.get("doktyp", "sonstiges")

    if data.get("skip"):
        print("  [Skip] Nachhaltigkeitsdokument ohne Kapitalwerte.", file=sys.stderr)
        return 0, 0

    with get_db(db_path) as con:
        try:
            if doktyp == "standmitteilung":
                return _insert_standmitteilung(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp in ("aenderung", "auszahlung"):
                return _insert_aenderung(con, data, quelle_pdf, rohtext_md5, force)
            else:
                return _insert_standmitteilung(con, data, quelle_pdf, rohtext_md5, force)
        except Exception as e:
            print(f"  [Warnung] Fehler beim Einfuegen: {e}", file=sys.stderr)
            return 0, 0


def _insert_standmitteilung(con, data, quelle_pdf, rohtext_md5, force):
    vertrag_id = data.get("vertrag_id")
    if vertrag_id and not vertrag_id.startswith("av_"):
        vertrag_id = None

    sql = """INSERT INTO standmitteilungen
             (vertrag_id, datum_mitteilung, stichtag, guthaben_eur,
              ablauf_garantie_eur, ablauf_prognose_eur,
              jahresrente_garantie, jahresrente_prognose,
              beitraege_kumuliert_eur, beitrag_aktuell_eur,
              ueberschuss_eur, quelle_pdf, rohtext_md5)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            vertrag_id,
            _norm_datum(data.get("datum_mitteilung") or ""),
            _norm_datum(data.get("stichtag") or ""),
            _to_float(data.get("guthaben_eur")),
            _to_float(data.get("ablauf_garantie_eur")),
            _to_float(data.get("ablauf_prognose_eur")),
            _to_float(data.get("jahresrente_garantie")),
            _to_float(data.get("jahresrente_prognose")),
            _to_float(data.get("beitraege_kumuliert_eur")),
            _to_float(data.get("beitrag_aktuell_eur")),
            _to_float(data.get("ueberschuss_eur")),
            quelle_pdf,
            rohtext_md5,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_aenderung(con, data, quelle_pdf, rohtext_md5, force):
    vertrag_id = data.get("vertrag_id")
    sql = """INSERT INTO aenderungen
             (vertrag_id, datum, art, betrag_eur, beschreibung, quelle_pdf)
             VALUES (?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            vertrag_id,
            _norm_datum(data.get("datum_mitteilung") or ""),
            data.get("art_aenderung"),
            _to_float(data.get("beitrag_aktuell_eur") or data.get("guthaben_eur")),
            data.get("beschreibung"),
            quelle_pdf,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


# ── Kommandos ────────────────────────────────────────────────────────────────────

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
    kw_vertrag = match_vertrag(text)
    kw_doktyp = match_doktyp(text)
    if kw_vertrag:
        print(f"  Keyword-Match Vertrag: {kw_vertrag}")
    if kw_doktyp:
        print(f"  Keyword-Match DokTyp:  {kw_doktyp}")

    if kw_doktyp == "nachhaltigkeit":
        print("  -> Ueberspringe Nachhaltigkeitsdokument")
        return

    print(f"Analysiere mit Ollama ({model})...")
    data = ollama_extract(text, model=model)

    if kw_vertrag and data.get("vertrag_id") != kw_vertrag:
        data["vertrag_id"] = kw_vertrag
    if kw_doktyp and kw_doktyp != "sonstiges" and data.get("doktyp") != kw_doktyp:
        data["doktyp"] = kw_doktyp

    print(f"\n  Vertrag:      {data.get('vertrag_id', '?')}")
    print(f"  Typ:           {data.get('doktyp', '?')}")
    print(f"  Datum:         {data.get('datum_mitteilung', '?')}")
    print(f"  Guthaben:      {data.get('guthaben_eur', '?')}")
    print(f"  Garantie Abl.: {data.get('ablauf_garantie_eur', '?')}")

    neu, skipped = insert_av(data, quelle_pdf=str(quelle), rohtext=text,
                             db_path=db, force=force)
    print(f"\nDB: {neu} neu, {skipped} bereits vorhanden -> {db}")


def cmd_list(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        vertraege = con.execute("SELECT * FROM vertraege ORDER BY aktiv DESC, id").fetchall()
        print(f"\n=== Vertraege ({len(vertraege)}) ===")
        for v in vertraege:
            aktiv_str = "aktiv" if v["aktiv"] else "inaktiv"
            print(f"  {v['id']:6s} {v['versicherer']:28s} {v['art']:22s} "
                  f"{v['versicherungsnehmer'] or '?':10s} [{aktiv_str}]")

        where = []
        params = []
        if args.vertrag:
            where.append("s.vertrag_id = ?")
            params.append(args.vertrag)
        if args.person:
            where.append("v.versicherungsnehmer = ?")
            params.append(args.person)
        if args.typ:
            where.append("v.art = ?")
            params.append(args.typ)

        sql = """SELECT s.*, v.versicherer, v.versicherungsnehmer, v.art
                 FROM standmitteilungen s
                 JOIN vertraege v ON s.vertrag_id = v.id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.datum_mitteilung DESC LIMIT 100"

        rows = con.execute(sql, params).fetchall()
        if rows:
            print(f"\n=== Standmitteilungen ({len(rows)}) ===")
            print(f"{'Vertrag':6s} {'Datum':12s} {'Stichtag':12s} "
                  f"{'Guthaben':>12s} {'Garantie Abl.':>14s} {'Prognose Abl.':>14s}")
            print("-" * 85)
            for r in rows:
                print(f"{r['vertrag_id']:6s} {r['datum_mitteilung'] or '?':12s} "
                      f"{r['stichtag'] or '?':12s} {r['guthaben_eur'] or 0:>12.2f} "
                      f"{r['ablauf_garantie_eur'] or 0:>14.2f} "
                      f"{r['ablauf_prognose_eur'] or 0:>14.2f}")


def cmd_verlauf(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        vertrag_filter = "WHERE s.vertrag_id = ?" if args.vertrag else ""
        params = (args.vertrag,) if args.vertrag else ()

        rows = con.execute(
            f"""SELECT s.*, v.versicherer, v.versicherungsnehmer
                FROM standmitteilungen s
                JOIN vertraege v ON s.vertrag_id = v.id
                {vertrag_filter}
                ORDER BY s.vertrag_id, s.datum_mitteilung""",
            params,
        ).fetchall()

        if not rows:
            print("Keine Standmitteilungen gefunden.")
            return

        current_v = None
        for r in rows:
            if r["vertrag_id"] != current_v:
                current_v = r["vertrag_id"]
                print(f"\n=== {current_v} {r['versicherer']} ({r['versicherungsnehmer']}) ===")
                print(f"{'Datum':12s} {'Guthaben':>12s} {'Garantie':>12s} {'Prognose':>12s} "
                      f"{'Beitraege':>12s} {'Ueberschuss':>12s}")
                print("-" * 80)
            print(f"{r['datum_mitteilung'] or '?':12s} "
                  f"{r['guthaben_eur'] or 0:>12.2f} "
                  f"{r['ablauf_garantie_eur'] or 0:>12.2f} "
                  f"{r['ablauf_prognose_eur'] or 0:>12.2f} "
                  f"{r['beitraege_kumuliert_eur'] or 0:>12.2f} "
                  f"{r['ueberschuss_eur'] or 0:>12.2f}")


def cmd_gesamt(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        rows = con.execute("""
            SELECT s.vertrag_id, v.versicherer, v.versicherungsnehmer, v.aktiv,
                   s.guthaben_eur, s.ablauf_garantie_eur, s.ablauf_prognose_eur,
                   s.datum_mitteilung
            FROM standmitteilungen s
            JOIN vertraege v ON s.vertrag_id = v.id
            WHERE s.id IN (
                SELECT MAX(s2.id) FROM standmitteilungen s2
                WHERE s2.vertrag_id = s.vertrag_id
                GROUP BY s2.vertrag_id
            )
            ORDER BY v.aktiv DESC, s.guthaben_eur DESC
        """).fetchall()

        if not rows:
            print("Keine Standmitteilungen gefunden.")
            return

        summe_guthaben = 0
        summe_garantie = 0
        summe_prognose = 0
        aktiv_count = 0

        print(f"\n=== Gesamtvermoegen Altersvorsorge ===\n")
        print(f"{'Vertrag':6s} {'Versicherer':28s} {'Person':10s} "
              f"{'Guthaben':>12s} {'Garantie':>12s} {'Prognose':>12s} {'Stand':12s}")
        print("-" * 95)
        for r in rows:
            aktiv_str = "aktiv" if r["aktiv"] else "inaktiv"
            g = r["guthaben_eur"] or 0
            ga = r["ablauf_garantie_eur"] or 0
            gp = r["ablauf_prognose_eur"] or 0
            if r["aktiv"]:
                summe_guthaben += g
                summe_garantie += ga
                summe_prognose += gp
                aktiv_count += 1
            print(f"{r['vertrag_id']:6s} {r['versicherer']:28s} "
                  f"{r['versicherungsnehmer'] or '?':10s} {g:>12.2f} "
                  f"{ga:>12.2f} {gp:>12.2f} {r['datum_mitteilung'] or '?':12s}")

        print(f"\n  Summe ({aktiv_count} aktive Vertraege):")
        print(f"    Guthaben:  {summe_guthaben:>12,.2f} EUR")
        print(f"    Garantie:  {summe_garantie:>12,.2f} EUR")
        print(f"    Prognose:  {summe_prognose:>12,.2f} EUR")


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Altersvorsorge-Dokument Extraktor")
    parser.add_argument("--db", default=None, help=f"Pfad zur SQLite-DB (default: {DEFAULT_DB})")
    parser.add_argument("--model", default=OLLAMA_MODEL, help=f"Ollama-Modell (default: {OLLAMA_MODEL})")
    parser.add_argument("--force", action="store_true", help="Bestehende Eintraege ueberschreiben")

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
    p_list.add_argument("--vertrag", help="Nach Vertrag filtern (z.B. av_7)")
    p_list.add_argument("--person", help="Nach Person filtern (Reinhard / Marion)")
    p_list.add_argument("--typ", help="Nach Vertragsart filtern")
    p_list.set_defaults(func=cmd_list)

    p_verlauf = sub.add_parser("verlauf", help="Zeitreihe Guthaben pro Vertrag")
    p_verlauf.add_argument("--vertrag", help="Nach Vertrag filtern")
    p_verlauf.set_defaults(func=cmd_verlauf)

    p_gesamt = sub.add_parser("gesamt", help="Gesamtvermoegen Altersvorsorge")
    p_gesamt.set_defaults(func=cmd_gesamt)

    args = parser.parse_args()
    if args.cmd is None:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
