#!/usr/bin/env python3
"""Analyse-Skript für die Klassifikations-Historie des Dispatchers.

Aufruf (im Container oder direkt):
    python3 analyze_classifications.py [--db /pfad/dispatcher.db]

Ausgabe:
  - Hit-Rate pro Kategorie (wie oft LLM-Vorschlag korrekt, d.h. keine Korrektur folgte)
  - Korrektur-Häufigkeit (wie oft wurde manuell korrigiert)
  - Häufigste Sprachen der Dokumente
  - Durchschnittliche LLM-Antwortzeit
  - Anzahl Halluzinationen (Einträge ohne gültige Kategorie nach LLM-Phase)
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from collections import Counter

DEFAULT_DB = Path("/data/dispatcher-temp/dispatcher.db")


def get_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def analyze(db_path: Path):
    if not db_path.exists():
        print(f"Datenbank nicht gefunden: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = get_db(db_path)

    # ── Übersicht ──────────────────────────────────────────────────────────────
    section("Gesamtübersicht")
    total_docs = con.execute("SELECT COUNT(*) FROM dokumente").fetchone()[0]
    total_hist = con.execute("SELECT COUNT(*) FROM klassifikations_historie").fetchone()[0]
    total_llm  = con.execute(
        "SELECT COUNT(*) FROM klassifikations_historie WHERE korrektur_von_user = 0"
    ).fetchone()[0]
    total_korr = con.execute(
        "SELECT COUNT(*) FROM klassifikations_historie WHERE korrektur_von_user = 1"
    ).fetchone()[0]

    print(f"  Dokumente gesamt:        {total_docs}")
    print(f"  Klassifikations-Einträge:{total_hist}")
    print(f"    davon LLM-Läufe:       {total_llm}")
    print(f"    davon Korrekturen:     {total_korr}")
    if total_llm:
        korr_rate = round(total_korr / total_llm * 100, 1)
        print(f"  Korrektur-Rate:          {korr_rate}%")

    # ── Hit-Rate pro Kategorie ─────────────────────────────────────────────────
    section("Hit-Rate pro Kategorie (LLM-Vorschlag vs. Korrektur)")
    # Dokumente mit mind. einem LLM-Eintrag + ob eine Korrektur folgte
    rows = con.execute("""
        SELECT
            h.final_category,
            COUNT(DISTINCT h.dokument_id)                                       AS llm_count,
            COUNT(DISTINCT CASE WHEN k.id IS NOT NULL THEN h.dokument_id END)  AS korr_count
        FROM klassifikations_historie h
        LEFT JOIN klassifikations_historie k
            ON k.dokument_id = h.dokument_id AND k.korrektur_von_user = 1
        WHERE h.korrektur_von_user = 0
          AND h.final_category IS NOT NULL
        GROUP BY h.final_category
        ORDER BY llm_count DESC
    """).fetchall()

    if rows:
        print(f"  {'Kategorie':<35} {'LLM':>6} {'Korrekt':>8} {'Hit-Rate':>9}")
        print(f"  {'─'*35} {'─'*6} {'─'*8} {'─'*9}")
        for r in rows:
            cat   = r["final_category"] or "–"
            llm   = r["llm_count"]
            korr  = r["korr_count"]
            korrekt = llm - korr
            rate  = round(korrekt / llm * 100) if llm else 0
            print(f"  {cat:<35} {llm:>6} {korrekt:>8} {rate:>8}%")
    else:
        print("  (noch keine Daten)")

    # ── Sprachen ──────────────────────────────────────────────────────────────
    section("Sprachen der Dokumente")
    lang_rows = con.execute("""
        SELECT lang_detected, COUNT(*) AS n
        FROM klassifikations_historie
        WHERE korrektur_von_user = 0 AND lang_detected IS NOT NULL
        GROUP BY lang_detected
        ORDER BY n DESC
    """).fetchall()

    LANG_LABELS = {"de": "Deutsch", "it": "Italiano", "en": "English", "fr": "Français"}
    if lang_rows:
        for r in lang_rows:
            label = LANG_LABELS.get(r["lang_detected"], r["lang_detected"].upper())
            print(f"  {label:<15} {r['n']:>5} Dokumente")
    else:
        print("  (noch keine Daten)")

    # ── LLM-Antwortzeiten ─────────────────────────────────────────────────────
    section("LLM-Antwortzeiten")
    time_row = con.execute("""
        SELECT
            AVG(duration_ms)  AS avg_ms,
            MIN(duration_ms)  AS min_ms,
            MAX(duration_ms)  AS max_ms,
            COUNT(*)          AS n
        FROM klassifikations_historie
        WHERE korrektur_von_user = 0 AND duration_ms IS NOT NULL
    """).fetchone()

    if time_row and time_row["n"]:
        print(f"  Messungen:   {time_row['n']}")
        print(f"  Durchschnitt:{round(time_row['avg_ms'] / 1000, 1)} s")
        print(f"  Minimum:     {round(time_row['min_ms'] / 1000, 1)} s")
        print(f"  Maximum:     {round(time_row['max_ms'] / 1000, 1)} s")
    else:
        print("  (noch keine Daten)")

    # ── Per-Feld-Konfidenz Verteilung ─────────────────────────────────────────
    section("Per-Feld-Konfidenz (nur LLM-Einträge mit Kategorie)")
    for field in ("konfidenz_category", "konfidenz_type", "konfidenz_absender",
                  "konfidenz_adressat", "konfidenz_datum"):
        rows_k = con.execute(f"""
            SELECT {field} AS k, COUNT(*) AS n
            FROM klassifikations_historie
            WHERE korrektur_von_user = 0 AND {field} IS NOT NULL
            GROUP BY {field}
        """).fetchall()
        if not rows_k:
            continue
        counts = {r["k"]: r["n"] for r in rows_k}
        total_k = sum(counts.values())
        label = field.replace("konfidenz_", "")
        hoch   = counts.get("hoch", 0)
        mittel = counts.get("mittel", 0)
        niedrig= counts.get("niedrig", 0)
        print(
            f"  {label:<12}  🟢 hoch {hoch:>3} ({round(hoch/total_k*100):>2}%)  "
            f"🟡 mittel {mittel:>3} ({round(mittel/total_k*100):>2}%)  "
            f"🔴 niedrig {niedrig:>3} ({round(niedrig/total_k*100):>2}%)"
        )

    # ── Modelle ───────────────────────────────────────────────────────────────
    section("Verwendete Modelle")
    model_rows = con.execute("""
        SELECT llm_model, translate_model, COUNT(*) AS n
        FROM klassifikations_historie
        WHERE korrektur_von_user = 0 AND llm_model IS NOT NULL
        GROUP BY llm_model, translate_model
        ORDER BY n DESC
    """).fetchall()
    if model_rows:
        for r in model_rows:
            tm = r["translate_model"] or "–"
            print(f"  LLM: {r['llm_model']:<25}  Translate: {tm:<25}  {r['n']} Läufe")
    else:
        print("  (noch keine Daten)")

    print()
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dispatcher Klassifikations-Analyse")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Pfad zur dispatcher.db")
    args = parser.parse_args()
    analyze(args.db)
