# Skill: KFZ

**Erstellt:** 2026-05-17 (analyze.py)  
**Erweitert:** 2026-05-18 (Dashboard, PROTOKOLL), 2026-05-25 (Batch, Dedup, Ausschluss)
**Zweck:** Extraktion strukturierter Daten aus KFZ-Dokumenten (Versicherungen, Schaeden,
Werkstatt, Steuer, Zulassung) in SQLite-Datenbank mit Web-Dashboard.

---

## Architektur

```
MD (60 Fahrzeuge/) → original: → PDF (Anlagen/)
  |
  +-- [batch_import.py] Batch-Verarbeitung aller MDs
  +-- [Ausschluss-Keywords] CASAMIA, PRIVACY, TÜV etc. → sonstiges
  +-- [Keyword-Matching] Kennzeichen + DokTyp vor LLM
  +-- [Ollama] qwen3:4b-instruct → JSON
  +-- [Dedup] Prüft (fahrzeug_id, vertragsnummer, deckungsart, von, bis) vor INSERT
  +-- kfz.db (5 Tabellen)
```

## Dateien

```
~/.claude/skills/kfz/
  SKILL.md              -- Claude Code Skill (Trigger + Workflow)
  analyze.py            -- Extraktionsskript (pdf / text / list / aktiv / kosten / init)
  batch_import.py       -- Batch-Import aller MDs aus 60 Fahrzeuge/
  dashboard.py          -- Web-Dashboard (FastAPI, Port 8094)
  schema.sql            -- DB-Schema + 6 Fahrzeuge als Seed-Daten
  kfz.db                -- SQLite-Datenbank
  kfz-dashboard.service -- systemd User-Service
  PROTOKOLL.md          -- Diese Datei
  cleanup.log           -- Cleanup-Log (2026-05-25)
```

## Datenbank-Schema (6 Tabellen)

| Tabelle | Inhalt |
|---------|--------|
| fahrzeuge | Stammdaten (Seed, 6 Eintraege) |
| versicherungen | KFZ-Versicherungsvertraege |
| schaeden | Schadensmeldungen und -regulierungen |
| reparaturen | Werkstatt, Wartung, TUEV |
| steuern | KFZ-Steuer (Bollo) |
| zulassungen | Fahrzeugschein, Carta di Circolazione |

## Fahrzeuge (Seed)

| ID | Kennzeichen | Typ | Marke | Land | Aktiv |
|----|------------|-----|-------|------|-------|
| kfz_1 | GY243ZF | PKW | Tesla Model Y | IT | ja |
| kfz_2 | XBFSL4 | Ape | Piaggio | IT | ja (seit 2025-08-18) |
| kfz_3 | FR-Y1544 | Anhaenger | – | DE | ja |
| kfz_4 | XA328YK | Anhaenger | – | IT | ja |
| kfz_5 | BD837H | Traktor | Antonio Carraro | IT | ja |
| kfz_6 | GY964ZF | PKW | Mitsubishi L200 | IT | ja |
| kfz_alt_1 | TS-RJ801 | PKW | – | DE | nein |
| kfz_alt_2 | TS-RJ8888 | PKW | Mitsubishi L200 | DE | nein |

## Keyword-Matching (vor LLM)

Kennzeichen und Dokumenttypen werden deterministisch per Regex erkannt.

**Ausschluss (vorrangig):** Keywords wie `casamia`, `realmente protetti`, `datenschutz`,
`kaufvertrag`, `tüv-bericht`, `carta di circolazione` etc. zwingen `doktyp="sonstiges"`.

**DokTyp-Priorität:** Ausschluss > Keyword > LLM. LLM hat Vorrang wenn er `sonstiges`
sagt und kein Ausschluss-Keyword vorliegt (der LLM hat den Kontext gelesen).

## Dedup

Vor INSERT prüft `_insert_versicherung()` auf identische Police:
`(fahrzeug_id, vertragsnummer, deckungsart, gueltig_von, gueltig_bis)`.
Ist der Datensatz schon vorhanden → skip (vermeidet Duplikate aus mehrseitigen PDFs).

## analyze.py – CLI

```bash
# DB initialisieren
python3 ~/.claude/skills/kfz/analyze.py init

# PDF analysieren
python3 ~/.claude/skills/kfz/analyze.py pdf <pfad.pdf>

# Text (Wilson-Bypass)
python3 ~/.claude/skills/kfz/analyze.py text "<text>" --quelle <pdf>

# Abfragen
python3 ~/.claude/skills/kfz/analyze.py list
python3 ~/.claude/skills/kfz/analyze.py list --kfz kfz_2
python3 ~/.claude/skills/kfz/analyze.py aktiv
python3 ~/.claude/skills/kfz/analyze.py kosten
python3 ~/.claude/skills/kfz/analyze.py kosten --kfz kfz_1
```

## Web-Dashboard

**URL:** `http://192.168.86.195:8094/`

### Start

```bash
# systemd (dauerhaft)
systemctl --user enable kfz-dashboard
systemctl --user start kfz-dashboard

# Manuell
cd ~/.claude/skills/kfz && python3 dashboard.py
```

### Seiten

- **Uebersicht:** Summary-Cards (Fahrzeuge, Versicherungen, Schaeden, Reparaturen),
  Ablaufwarnungen < 30/60 Tage, Fahrzeug-Tabelle mit kumulierten Kosten
- **Versicherungen:** Filter nach Fahrzeug, Praemien, Laufzeiten
- **Schaeden:** Schadenshistorie mit Status
- **Werkstatt:** Reparatur-/Wartungshistorie mit Summe

## Dispatcher-Integration

**Status:** aktiv (seit 2026-05-22)

- `dispatcher.py`: Kategorie `fahrzeuge` → `_call_skill_analyze("kfz", ...)`
- `docker-compose.yml`: Volume-Mount `~/.claude/skills/kfz:/data/kfz`
- Analog zu KV-Bypass und Immobilien-Pattern

## Batch-Import

**Status:** aktiv

```bash
cd ~/.claude/skills/kfz && python3 batch_import.py
```

Scannt rekursiv `60 Fahrzeuge/*.md`, extrahiert `original:`-Verweis,
prüft `already_done()` in allen KFZ-Tabellen, ruft `analyze.py pdf` pro neuem PDF.

## Aktueller Stand (2026-05-25)

| Metrik | Wert |
|--------|------|
| Fahrzeuge | 6 aktiv, 2 alt |
| Versicherungen | 44 (nach Cleanup: 44 Duplikate/Nicht-KFZ gelöscht) |
| Schaeden | n.v. |
| Dashboard | aktiv — Port 8094 |
| Dispatcher | aktiv |
| Batch-Import | aktiv |

### Cleanup 2026-05-25

36 falsche Einträge gelöscht (Sachversicherungen als KFZ klassifiziert,
Nicht-Versicherungs-Dokumente, Duplikate). 33 MD-Dateien aus 60 Fahrzeuge/
verschoben nach 40 Finanzen/, 50 Immobilien/, 99 Archiv/.

## Changelog

- **2026-05-17:** analyze.py, schema.sql, kfz.db, SKILL.md durch anderes Team erstellt
- **2026-05-18:** dashboard.py, PROTOKOLL.md, kfz-dashboard.service erstellt
- **2026-05-25:** batch_import.py erstellt, analyze.py gehärtet:
  - Ausschluss-Keywords (CASAMIA, Datenschutz, TÜV, Kaufvertrag etc.)
  - Dedup vor INSERT (verhindert Duplikate aus mehrseitigen PDFs)
  - LLM-Prompt verbessert (Reale Mutua Dual-Rolle, Nicht-Police-Warnung)
  - DokTyp-Priorität korrigiert (LLM hat Vorrang bei sonstiges)
  - Fahrzeug-Tabelle aktualisiert (kfz_2 korrigiert, kfz_4/5/6 ergänzt)
  - DB-Cleanup: 36 Fehleinträge gelöscht, 33 MDs in korrekte Ordner verschoben
