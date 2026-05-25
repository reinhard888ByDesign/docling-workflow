#!/usr/bin/env python3
"""Extrahiert Sachversicherungs-Dokumente (Hausrat, Haftpflicht, Wohngebaeude,
Rechtsschutz etc.) und schreibt sie in sachversicherungen.db.

Aufruf:
  analyze.py init                         # DB anlegen
  analyze.py pdf <pfad.pdf>               # PDF -> pdftotext -> Ollama -> DB
  analyze.py text "<text>" --quelle <pdf> # Wilson-Bypass
  analyze.py list [--art ART] [--aktiv] [--land IT]
  analyze.py coverage                    # Uebersicht aktive Deckungen + Luecken
  analyze.py praemien [--jahr YYYY]      # Jahreskosten Sachversicherungen
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

DEFAULT_DB = Path(__file__).resolve().parent / "sachversicherungen.db"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b-instruct")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))


# ── Keyword-Matching ─────────────────────────────────────────────────────────────

SV_VERTRAEGE = [
    ("sv_1",  [r"docura.*hausrat", r"hausrat.*docura"]),
    ("sv_2",  [r"nuernberger.*privatschutz", r"privatschutz.*wohngeb.ude"]),
    ("sv_3",  [r"hdi.*haftpflicht", r"haftpflicht.*hdi"]),
    ("sv_4",  [r"axa.*haftpflicht", r"haftpflicht.*axa"]),
    ("sv_5",  [r"vgh.*unfall", r"unfallversicherung.*vgh"]),
    ("sv_6",  [r"vov.*d.?o", r"d.?o.*versicherung.*vov"]),
    ("sv_7",  [r"versicherungskammer.*schlie.fach", r"schlie.fach.*versicherung"]),
    ("sv_8",  [r"nv versicherungen.*tier", r"tierversicherung.*nv"]),
    ("sv_9",  [r"reale mutua", r"casamia", r"katastrophenversicherung.*seggiano"]),
    ("sv_10", [r"wgv.*rechtsschutz", r"rechtsschutz.*wgv"]),
]

SV_DOKTYP = [
    ("beitragsrechnung", [r"beitragsrechnung", r"pr.mienrechnung", r"beitragsanforderung",
                           r"versicherungsbeitrag"]),
    ("versicherungsschein", [r"versicherungsschein", r"police", r"polizza"]),
    ("kuendigung",      [r"k.ndigung", r"vertragsk.ndigung", r"k.ndigungsbest.tigung"]),
    ("schaden",         [r"schadenmeldung", r"schadensmeldung", r"schadenregulierung"]),
    ("angebot",         [r"angebot", r"offerte", r"angebotsnummer"]),
]


def match_vertrag(text: str) -> str | None:
    t = text.lower()
    for vid, patterns in SV_VERTRAEGE:
        for pat in patterns:
            if re.search(pat, t):
                return vid
    return None


def match_doktyp(text: str) -> str:
    t = text.lower()
    for doktyp, patterns in SV_DOKTYP:
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
                id TEXT PRIMARY KEY, art TEXT NOT NULL,
                versicherer TEXT NOT NULL, aktiv INTEGER DEFAULT 1
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

EXTRACTION_PROMPT = """Du bist Experte fuer deutsche und italienische Sachversicherungen.
Extrahiere Vertragsdetails und Praemienangaben.

Bekannte Vertraege:
- sv_1: DOCURA Hausratversicherung (Reinhard, ausgelaufen)
- sv_2: Nuernberger PrivatSchutz Wohngebaeude (Reinhard, vmtl. abgelaufen)
- sv_3: HDI Privathaftpflicht (Reinhard, 2015)
- sv_4: AXA Privathaftpflicht (Reinhard, GEKUENDIGT 2026-02-27)
- sv_5: VGH Unfallversicherung (Familie)
- sv_6: VOV D&O-Versicherung (Reinhard, 2016)
- sv_7: Versicherungskammer Bayern Schliessfach (Reinhard)
- sv_8: NV Versicherungen Tierversicherung (Hund)
- sv_9: Reale Mutua CASAMIA Katastrophenversicherung (Reinhard, Italien, Seggiano)
- sv_10: WGV Rechtsschutzversicherung (Reinhard)

WICHTIG:
- Datum NORMALISIEREN auf JJJJ-MM-TT.
- Betraege als ZAHLEN mit Punkt als Dezimaltrenner.
- Italienische Polizze: "premio" = Praemie, "massimale" = Deckungssumme.
- aktiv=false wenn das Dokument eine Kuendigung ist.
- Nur das JSON zurueckgeben, keinen anderen Text.

Gib ausschliesslich dieses JSON zurueck (kein Markdown):
{
  "vertrag_id": "<sv_1..sv_10 oder null wenn neuer Vertrag>",
  "versicherer": "<Name>",
  "art": "<hausrat|wohngebaeude|haftpflicht_privat|haftpflicht_do|unfall|tier|schliessfach|rechtsschutz|katastrophe|sonstiges>",
  "vertragsnummer": "<aus Dokument oder null>",
  "doktyp": "<beitragsrechnung|versicherungsschein|kuendigung|schaden|angebot|sonstiges>",
  "datum_dokument": "<YYYY-MM-DD>",
  "praemie_eur": <Jahrespraemie als Zahl oder null>,
  "periode_von": "<YYYY-MM-DD oder null>",
  "periode_bis": "<YYYY-MM-DD oder null>",
  "aktiv": <true/false>,
  "gekuendigt_am": "<YYYY-MM-DD oder null>",
  "land": "<DE|IT>",
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

def insert_sv(data: dict, quelle_pdf: str, rohtext: str = "",
              db_path: Path = DEFAULT_DB, force: bool = False) -> tuple[int, int]:
    rohtext_md5 = hashlib.md5(rohtext.encode("utf-8")).hexdigest() if rohtext else ""
    doktyp = data.get("doktyp", "sonstiges")

    with get_db(db_path) as con:
        try:
            if doktyp == "beitragsrechnung":
                return _insert_praemie(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp == "schaden":
                return _insert_schaden(con, data, quelle_pdf, rohtext_md5, force)
            elif doktyp in ("kuendigung", "angebot"):
                return _insert_aenderung(con, data, quelle_pdf, rohtext_md5, force)
            else:
                # versicherungsschein / sonstiges → Praemie
                if data.get("praemie_eur"):
                    return _insert_praemie(con, data, quelle_pdf, rohtext_md5, force)
                return _insert_aenderung(con, data, quelle_pdf, rohtext_md5, force)
        except Exception as e:
            print(f"  [Warnung] Fehler beim Einfuegen: {e}", file=sys.stderr)
            return 0, 0


def _insert_praemie(con, data, quelle_pdf, rohtext_md5, force):
    vertrag_id = data.get("vertrag_id")
    sql = """INSERT INTO praemien
             (vertrag_id, datum, betrag_eur, periode_von, periode_bis, quelle_pdf, rohtext_md5)
             VALUES (?, ?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            vertrag_id,
            _norm_datum(data.get("datum_dokument") or ""),
            _to_float(data.get("praemie_eur")) or 0,
            _norm_datum(data.get("periode_von") or ""),
            _norm_datum(data.get("periode_bis") or ""),
            quelle_pdf,
            rohtext_md5,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_schaden(con, data, quelle_pdf, rohtext_md5, force):
    vertrag_id = data.get("vertrag_id")
    sql = """INSERT INTO schaeden
             (vertrag_id, datum_schaden, datum_meldung, beschreibung,
              schaden_eur, regulierung_eur, status, quelle_pdf)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            vertrag_id,
            _norm_datum(data.get("datum_dokument") or ""),
            _norm_datum(data.get("datum_meldung") or ""),
            data.get("beschreibung"),
            _to_float(data.get("schaden_eur")),
            _to_float(data.get("regulierung_eur")),
            data.get("status"),
            quelle_pdf,
        ))
        con.commit()
        return 1, 0
    except sqlite3.IntegrityError:
        return 0, 1


def _insert_aenderung(con, data, quelle_pdf, rohtext_md5, force):
    vertrag_id = data.get("vertrag_id")
    sql = """INSERT INTO aenderungen
             (vertrag_id, datum, art, beschreibung, quelle_pdf)
             VALUES (?, ?, ?, ?, ?)"""
    if force:
        sql = "INSERT OR REPLACE" + sql[6:]
    try:
        con.execute(sql, (
            vertrag_id,
            _norm_datum(data.get("datum_dokument") or ""),
            data.get("doktyp"),
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

    print(f"Analysiere mit Ollama ({model})...")
    data = ollama_extract(text, model=model)

    if kw_vertrag and data.get("vertrag_id") != kw_vertrag:
        data["vertrag_id"] = kw_vertrag
    if kw_doktyp and kw_doktyp != "sonstiges" and data.get("doktyp") != kw_doktyp:
        data["doktyp"] = kw_doktyp

    print(f"\n  Vertrag:      {data.get('vertrag_id', '?')}")
    print(f"  Typ:           {data.get('doktyp', '?')}")
    print(f"  Versicherer:   {data.get('versicherer', '?')}")
    print(f"  Praemie:       {data.get('praemie_eur', '?')}")

    neu, skipped = insert_sv(data, quelle_pdf=str(quelle), rohtext=text,
                             db_path=db, force=force)
    print(f"\nDB: {neu} neu, {skipped} bereits vorhanden -> {db}")


def cmd_list(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        where = []
        params = []
        if args.art:
            where.append("art = ?")
            params.append(args.art)
        if args.aktiv:
            where.append("aktiv = 1")
        if args.land:
            where.append("land = ?")
            params.append(args.land)

        sql = "SELECT * FROM vertraege"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY aktiv DESC, id"

        vertraege = con.execute(sql, params).fetchall()
        print(f"\n=== Vertraege ({len(vertraege)}) ===")
        for v in vertraege:
            aktiv_str = "aktiv" if v["aktiv"] else "inaktiv"
            print(f"  {v['id']:6s} {v['art']:24s} {v['versicherer']:30s} "
                  f"{v['versicherungsnehmer'] or '?':10s} [{aktiv_str}] "
                  f"{v['land']}")

        # Praemien
        praemien = con.execute(
            "SELECT p.*, v.art, v.versicherer FROM praemien p "
            "JOIN vertraege v ON p.vertrag_id=v.id "
            "ORDER BY p.datum DESC LIMIT 50"
        ).fetchall()
        if praemien:
            print(f"\n=== Praemien ({len(praemien)}) ===")
            for p in praemien:
                print(f"  {p['vertrag_id']:6s} {p['datum'] or '?':12s} "
                      f"{p['betrag_eur'] or 0:>10.2f} EUR  {p['periode_von'] or '?'} – {p['periode_bis'] or '?'}")


def cmd_coverage(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        vertraege = con.execute(
            "SELECT * FROM vertraege ORDER BY aktiv DESC, art"
        ).fetchall()

        print("\n=== Coverage-Check ===\n")

        # Nach Art gruppieren
        deckung = {}
        for v in vertraege:
            deckung.setdefault(v["art"], []).append(v)

        checks = [
            ("haftpflicht_privat", "Privathaftpflicht",
             "DE: Haftpflicht fehlt! sv_4 (AXA) gekuendigt 2026-02-27. sv_9 (Reale Mutua) deckt IT-Objekte ab."),
            ("hausrat", "Hausrat",
             "Kein aktiver Hausrat (sv_1 ausgelaufen). Aktueller Wohnsitz IT → kein DE-Hausrat noetig."),
            ("wohngebaeude", "Wohngebaeude",
             "sv_2 (Nuernberger) vmtl. abgelaufen. Ggf. durch sv_9 (Reale Mutua) fuer IT abgedeckt."),
            ("rechtsschutz", "Rechtsschutz",
             "sv_10 (WGV) aktiv ✓"),
            ("unfall", "Unfall",
             "sv_5 (VGH) Status unklar (letztes Dok 2017)"),
            ("tier", "Tier (Hund)",
             "sv_8 (NV) aktiv ✓"),
        ]

        for art, label, hinweis in checks:
            aktive = [v for v in deckung.get(art, []) if v["aktiv"]]
            inaktive = [v for v in deckung.get(art, []) if not v["aktiv"]]
            kombi_match = [v for v in deckung.get("kombi_it", []) if v["aktiv"]]

            if aktive:
                namen = ", ".join(f"{v['id']} ({v['versicherer']})" for v in aktive)
                print(f"  ✅ {label}: {namen}")
            elif art == "haftpflicht_privat" and kombi_match:
                namen = ", ".join(f"{v['id']} ({v['versicherer']})" for v in kombi_match)
                print(f"  ✅ {label}: via {namen} (IT-Kombi)")
            elif art == "wohngebaeude" and kombi_match:
                namen = ", ".join(f"{v['id']} ({v['versicherer']})" for v in kombi_match)
                print(f"  ✅ {label}: via {namen} (IT-Kombi)")
            else:
                in_str = f" (inaktiv: {', '.join(v['id'] for v in inaktive)})" if inaktive else ""
                print(f"  ⚠️  {label}: KEINE aktive Deckung{in_str}")
                print(f"     → {hinweis}")

        # IT-Kombi
        kombi = deckung.get("kombi_it", [])
        if kombi:
            print(f"\n  🇮🇹 IT-Kombi (Reale Mutua CASAMIA):")
            for v in kombi:
                print(f"     {v['id']}: {v['versichertes_objekt'] or 'Seggiano'} "
                      f"[{'aktiv' if v['aktiv'] else 'inaktiv'}]")


def cmd_praemien(args):
    db = Path(args.db) if args.db else DEFAULT_DB
    if not db.exists():
        print("DB existiert noch nicht.", file=sys.stderr)
        sys.exit(1)

    with get_db(db) as con:
        where = []
        params = []
        if args.jahr:
            where.append("strftime('%Y', p.datum) = ?")
            params.append(str(args.jahr))

        sql = ("SELECT p.*, v.art, v.versicherer, v.land FROM praemien p "
               "JOIN vertraege v ON p.vertrag_id=v.id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.datum DESC"

        rows = con.execute(sql, params).fetchall()
        summe = sum(r["betrag_eur"] or 0 for r in rows)
        summe_aktiv = sum(r["betrag_eur"] or 0 for r in rows
                          if con.execute("SELECT aktiv FROM vertraege WHERE id=?",
                                         (r["vertrag_id"],)).fetchone()[0])

        print(f"\n=== Praemien ({len(rows)} Zahlungen) ===")
        print(f"  Summe: {summe:,.2f} EUR (davon aktive Vertraege: {summe_aktiv:,.2f} EUR)\n")

        for r in rows:
            print(f"  {r['vertrag_id']:6s} {r['datum'] or '?':12s} "
                  f"{r['betrag_eur'] or 0:>10.2f} EUR  {r['art']:24s} "
                  f"{r['versicherer']:30s} {r['periode_von'] or '?'} – {r['periode_bis'] or '?'}")


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sachversicherungs-Dokument Extraktor")
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
    p_list.add_argument("--art", help="Nach Art filtern")
    p_list.add_argument("--aktiv", action="store_true", help="Nur aktive Vertraege")
    p_list.add_argument("--land", help="Nach Land filtern (DE/IT)")
    p_list.set_defaults(func=cmd_list)

    p_cov = sub.add_parser("coverage", help="Coverage-Check (Lueckenanalyse)")
    p_cov.set_defaults(func=cmd_coverage)

    p_praem = sub.add_parser("praemien", help="Praemien/Jahreskosten anzeigen")
    p_praem.add_argument("--jahr", type=int, help="Nach Jahr filtern")
    p_praem.set_defaults(func=cmd_praemien)

    args = parser.parse_args()
    if args.cmd is None:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
