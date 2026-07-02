#!/usr/bin/env python3
"""Pipeline Debugger CLI — zeigt jeden Schritt mit voller Transparenz."""
import json, sys, time, re, os
from pathlib import Path
from datetime import datetime

# Pfad-Setup für Importe aus dem dispatcher-Container
sys.path.insert(0, '/app')

# ── Hilfsfunktionen ──────────────────────────────────────────────────
def sep(title=""):
    print(f"\n{'='*70}")
    if title: print(f"  {title}")
    print(f"{'='*70}")

def show(title, data, max_lines=30):
    print(f"\n── {title} ──")
    if isinstance(data, dict):
        for k, v in data.items():
            s = str(v)
            if len(s) > 200: s = s[:200] + "…"
            print(f"  {k}: {s}")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            print(f"  [{i}] {v}")
    elif isinstance(data, str):
        for line in data.split('\n')[:max_lines]:
            print(f"  {line}")
        if len(data.split('\n')) > max_lines:
            print(f"  … ({len(data.split(chr(10)))} Zeilen insgesamt)")
    else:
        print(f"  {data}")

def wait():
    input("\n⏎ Weiter…")

# ── Hauptfunktion ────────────────────────────────────────────────────
def debug_pipeline(pdf_path: str):
    pdf = Path(pdf_path)
    if not pdf.exists():
        print(f"❌ PDF nicht gefunden: {pdf}")
        sys.exit(1)

    sep(f"PIPELINE-DEBUG: {pdf.name}")
    print(f"  Größe: {pdf.stat().st_size:,} Bytes")
    print(f"  Pfad:  {pdf}")

    # ── Schritt 1: OCR ───────────────────────────────────────────
    sep("SCHRITT 1: Docling OCR")
    print("  Starte Konvertierung…")
    t0 = time.monotonic()
    from dispatcher import convert_to_markdown, _has_easyocr_artifact
    md = convert_to_markdown(pdf)
    dur = time.monotonic() - t0
    chars = len(md.strip()) if md else 0
    artifact = _has_easyocr_artifact(md) if md else False
    print(f"  Dauer:    {dur:.1f}s")
    print(f"  Zeichen:  {chars:,}")
    print(f"  Artefakt: {'Ja → force_ocr(Pass2)' if artifact else 'Nein (Pass1)'}")
    print(f"  Quality:  {'✅ Bestanden (≥150)' if chars>=150 else '❌ <150 → Inbox'}")
    if md:
        print(f"\n  ── OCR-Vorschau (erste 1000 Zeichen) ──")
        print(f"  {md[:1000]}")
    wait()

    if not md or chars < 150:
        print("❌ OCR unzureichend — Pipeline ENDE")
        return

    # ── Schritt 2: Header ─────────────────────────────────────────
    sep("SCHRITT 2: Header-Extraktion (Regex)")
    from dispatcher import extract_document_header
    hdr = extract_document_header(md)
    show("Absender", hdr.get("absender", {}))
    show("Empfänger", hdr.get("empfaenger", {}))
    wait()

    # ── Schritt 3: Identifier ─────────────────────────────────────
    sep("SCHRITT 3: Identifier & Personen-Auflösung")
    from dispatcher import extract_identifiers, resolve_adressat, resolve_absender
    idents = extract_identifiers(md)
    print(f"  Cod.Fiscale: {idents.get('cod_fiscale_person', [])}")
    print(f"  Part.IVA:    {idents.get('part_iva_firma', [])}")
    print(f"  IBAN:        {idents.get('iban', [])}")
    print(f"  USt-IdNr:    {idents.get('ust_id', [])}")
    adm = resolve_adressat(idents, md)
    abm = resolve_absender(idents, hdr)
    if adm: show("Adressat-Match", adm)
    else: print("  Adressat: kein Match")
    if abm: show("Absender-Match", abm)
    else: print("  Absender: kein Match")
    wait()

    # ── Schritt 4: DocType ────────────────────────────────────────
    sep("SCHRITT 4: Dokumenttyp-Erkennung (Keywords)")
    from dispatcher import extract_document_type
    dti = extract_document_type(md)
    show("Erkannter Typ", {"typ": dti.get("erkannter_typ"), "keyword": dti.get("quell_keyword"), "kategorie_hint": dti.get("kategorie_hint")})
    wait()

    # ── Schritt 5: Sprache ────────────────────────────────────────
    sep("SCHRITT 5: Spracherkennung")
    from dispatcher import detect_document_language
    lang, prob = detect_document_language(md)
    print(f"  Sprache: {lang} ({prob*100:.1f}%)")
    wait()

    # ── Schritt 6: LLM-Klassifikation ─────────────────────────────
    sep("SCHRITT 6: LLM-Klassifikation (Ollama)")
    from dispatcher import classify_with_ollama, load_categories
    cats = load_categories()
    print(f"  Modell:  {os.environ.get('OLLAMA_MODEL', 'qwen3:4b-instruct')}")
    print(f"  Context: {os.environ.get('OLLAMA_NUM_CTX', '8192')}")
    print(f"  Text an LLM: {len(md[:12000]):,} Zeichen (max 12.000)")
    print(f"\n  ⏳ Ollama läuft (kann 10-60s dauern)…")
    t0 = time.monotonic()
    llm = classify_with_ollama(md, cats, header=hdr, identifiers=idents, adressat_match=adm, absender_match=abm, doc_type_info=dti)
    dur = time.monotonic() - t0
    print(f"  Dauer: {dur:.1f}s")
    if llm:
        show("LLM-Antwort (JSON)", json.dumps(llm, indent=2, ensure_ascii=False), max_lines=50)
    else:
        print("  ❌ LLM gab None zurück")
    wait()

    # Retry
    if not llm or not llm.get("category_id"):
        sep("SCHRITT 6b: LLM-Retry (verkürzter Text, 4.000 Zeichen)")
        print("  ⏳ Ollama läuft…")
        t0 = time.monotonic()
        llm2 = classify_with_ollama(md[:4000], cats, header=hdr, identifiers=idents, adressat_match=adm, absender_match=abm, doc_type_info=dti)
        dur = time.monotonic() - t0
        print(f"  Dauer: {dur:.1f}s")
        if llm2 and llm2.get("category_id"):
            print("  ✅ Retry erfolgreich!")
            llm = llm2
            show("LLM-Antwort (Retry)", json.dumps(llm, indent=2, ensure_ascii=False), max_lines=50)
        else:
            print("  ❌ Auch Retry fehlgeschlagen")
        wait()

    # ── Schritt 7: Override-Kaskade ───────────────────────────────
    sep("SCHRITT 7: Override-Kaskade (13 Schritte)")
    result = dict(llm or {})
    result["_lang"] = lang

    def override(step, field, before, after, source):
        b = str(before) if before is not None else "—"
        a = str(after) if after is not None else "—"
        changed = b != a
        mark = "🔄" if changed else "➡️"
        print(f"  {mark} #{step:2d} {field:20s} | {b:30s} → {a:30s} | {source}")

    # 1: Adressat via Cod.Fiscale
    bef = result.get("adressat")
    if adm and adm.get("person_key"):
        forced = adm["person_key"].capitalize()
        if result.get("adressat") != forced: result["adressat"] = forced
        result["konfidenz_adressat"] = "hoch"
    override(1, "adressat", bef, result.get("adressat"), "Cod.Fiscale" if adm else "—")

    # 2: Adressat via Absender-Default
    bef = result.get("adressat")
    if not adm and abm and abm.get("adressat_default"):
        if result.get("adressat") != abm["adressat_default"]: result["adressat"] = abm["adressat_default"]
        result["konfidenz_adressat"] = "hoch"
    override(2, "adressat", bef, result.get("adressat"), "Absender-DB" if (abm and abm.get("adressat_default")) else "—")

    # 3: Absender-Fallback
    bef = result.get("absender")
    if abm and abm.get("name") and not result.get("absender"):
        result["absender"] = abm["name"]; result["konfidenz_absender"] = "hoch"
    override(3, "absender", bef, result.get("absender"), "Absender-Fallback" if (abm and abm.get("name")) else "—")

    # 4: LA-Datum Konfidenz
    from dispatcher import LEISTUNGSABRECHNUNG_TYPES
    if result.get("type_id") in LEISTUNGSABRECHNUNG_TYPES and abm and result.get("rechnungsdatum"):
        result["konfidenz_datum"] = "hoch"
    override(4, "konfidenz_datum", "—", result.get("konfidenz_datum"), "LA-Datum" if (result.get("type_id") in LEISTUNGSABRECHNUNG_TYPES and abm) else "—")

    # 5: Taxonomy check (category)
    bef = result.get("category_id")
    if bef and bef not in cats:
        result["category_id"] = None; result["type_id"] = None
    override(5, "category_id", bef, result.get("category_id"), "Taxonomie")

    # 6: Type validation
    bef = result.get("type_id")
    if result.get("category_id") and bef:
        vtypes = {t["id"] for t in cats.get(result["category_id"], {}).get("types", [])}
        if vtypes and bef not in vtypes: result["type_id"] = None
    override(6, "type_id", bef, result.get("type_id"), "Type-Validierung")

    # 7: Absender-Hint
    bef = result.get("category_id")
    if abm and abm.get("kategorie_hint") and abm["kategorie_hint"] in cats:
        if result.get("category_id") != abm["kategorie_hint"]:
            result["category_id"] = abm["kategorie_hint"]
            result["category_label"] = cats[abm["kategorie_hint"]].get("label")
            if abm.get("typ_hint"): result["type_id"] = abm["typ_hint"]
    override(7, "category_id", bef, result.get("category_id"), "Absender-Hint" if (abm and abm.get("kategorie_hint")) else "—")

    # 8: DocType-Hint
    bef = result.get("category_id")
    if not bef and dti and dti.get("kategorie_hint") and dti["kategorie_hint"] in cats:
        result["category_id"] = dti["kategorie_hint"]
        result["category_label"] = cats[dti["kategorie_hint"]].get("label")
    override(8, "category_id", bef, result.get("category_id"), "DocType-Hint" if (dti and dti.get("kategorie_hint")) else "—")

    # 9: Keyword-Rules
    from dispatcher import apply_keyword_rules
    bc = result.get("category_id")
    result = apply_keyword_rules(result, md, cats)
    override(9, "category_id", bc, result.get("category_id"), "Keyword-Rules")

    # 10: Lernregeln
    from dispatcher import apply_lernregeln_from_db
    bc = result.get("category_id")
    result = apply_lernregeln_from_db(result, md, result.get("absender"), cats)
    override(10, "category_id", bc, result.get("category_id"), "Lernregeln")

    # 11: Konfidenz
    from dispatcher import aggregate_konfidenz
    result["konfidenz"] = aggregate_konfidenz(result)
    override(11, "konfidenz", "—", result.get("konfidenz"), "Aggregation")

    # 12: Datum-Fallback
    bef = result.get("rechnungsdatum")
    from dispatcher import _date_from_filename_prefix
    if not bef:
        ymd = _date_from_filename_prefix(pdf.stem)
        if ymd and len(ymd) == 8: result["rechnungsdatum"] = f"{ymd[6:]}.{ymd[4:6]}.{ymd[:4]}"
    override(12, "rechnungsdatum", bef, result.get("rechnungsdatum"), "Dateiname-Fallback" if not bef else "LLM")

    # 13: Konfidenz-Gate
    bef = result.get("category_id")
    if result.get("konfidenz_category") == "niedrig" or (not result.get("konfidenz_category") and result.get("konfidenz") == "niedrig"):
        result["category_id"] = None; result["type_id"] = None
    override(13, "category_id", bef, result.get("category_id"), "Konfidenz-Gate")
    wait()

    # ── Schritt 8: Frontmatter ────────────────────────────────────
    sep("SCHRITT 8: Frontmatter & Zielpfad")
    from dispatcher import _build_frontmatter, build_vault_path
    cat_id = result.get("category_id") or ""
    typ_id = result.get("type_id") or ""
    fm = _build_frontmatter(result, pdf.name, cat_id, typ_id)
    adr = result.get("adressat") or ""
    dat = result.get("rechnungsdatum") or ""
    year = dat[-4:] if len(dat) >= 4 else datetime.now().strftime("%Y")
    vp = build_vault_path(cat_id, typ_id, adr, year, "DATEINAME.md") if cat_id else "00 Inbox/DATEINAME.md"

    print(f"  Zielordner: 📁 {vp}")
    print(f"\n  ── Frontmatter ──")
    print(fm)
    print(f"  📎 [[Anlagen/{pdf.name}]]")
    print(f"  (OCR-Text bzw. Summary folgt im Body)")

    sep("✅ PIPELINE ENDE")
    print(f"  Kategorie: {result.get('category_label') or result.get('category_id') or 'Inbox'}")
    print(f"  Typ:       {result.get('type_label') or result.get('type_id') or '—'}")
    print(f"  Absender:  {result.get('absender') or '—'}")
    print(f"  Adressat:  {result.get('adressat') or '—'}")
    print(f"  Datum:     {result.get('rechnungsdatum') or '—'}")
    print(f"  Betrag:    {result.get('rechnungsbetrag') or '—'}")
    print(f"  Konfidenz: {result.get('konfidenz') or '—'}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 pipeline_debug.py <PDF-Pfad>")
        print("  Der PDF-Pfad muss aus Sicht des Containers erreichbar sein")
        print("  (z.B. /data/input-dispatcher/ oder /data/reinhards-vault/Anlagen/)")
        sys.exit(1)
    debug_pipeline(sys.argv[1])
