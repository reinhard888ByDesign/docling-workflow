# Skill: Immobilien

**Erstellt:** 2026-05-17  
**Zweck:** Automatische Extraktion von Dokumentdaten aus Immobilien-Belegen (Rechnungen,
Betriebskostenabrechnungen, Mietverträge, Grundsteuer, Hausgeld, Versicherungen) in eine
strukturierte SQLite-Datenbank mit Web-Dashboard.

---

## Architektur

```
PDF (input-dispatcher/)
  │
  ├─ [Wilson-Bypass] .meta.json → Beschreibungstext
  │     └─ _immo_extract_and_store() → Ollama → immobilien.db
  │
  └─ [Normal-Pfad] OCR → LLM-Klassifikation (Dispatcher)
        ├─ save_to_db() → dispatcher.db (wie bisher)
        └─ _write_immobilien_db() → immobilien.db
```

## Dateien

```
~/.claude/skills/immobilien/
  SKILL.md              -- Claude Code Skill (Trigger + Workflow)
  analyze.py            -- Extraktionsskript (pdf / text / list / --init)
  batch_import.py       -- Batch-Import aller MDs aus 50 Immobilien/
  dashboard.py          -- Web-Dashboard (FastAPI, Port 8091)
  schema.sql            -- DB-Schema + 8 Objekte als Seed-Daten
  immobilien.db         -- SQLite-Datenbank
  immo-dashboard.service -- systemd User-Service
  PROTOKOLL.md          -- Diese Datei
  ROADMAP.md            -- Entwicklungs-Roadmap
```

## Datenbank-Schema

### Tabelle `objekte` (Seed, 8 Einträge)

| ID        | Bezeichnung                    | Typ       | Land |
|-----------|-------------------------------|-----------|------|
| eigen_1   | Grassauer Straße 64, Übersee  | eigen     | DE   |
| eigen_2   | Podere dei venti, Seggiano    | eigen     | IT   |
| vm_1      | Lipowskystraße, München       | vermietet | DE   |
| vm_2      | Kornstraße, Bremen            | vermietet | DE   |
| vm_3      | Kolberger Straße, Karlsruhe   | vermietet | DE   |
| vm_4      | Schießhausstraße, Neuburg     | vermietet | DE   |
| vm_5      | Bahnhofstraße, Schechen       | vermietet | DE   |
| vm_6      | Via dell'ospedale, Seggiano   | vermietet | IT   |

### Tabelle `dokumente`

| Feld          | Typ     | Beschreibung |
|---------------|---------|-------------|
| id            | INTEGER | Auto-PK |
| quelle_pdf    | TEXT    | UNIQUE, Pfad zum Original-PDF |
| objekt_id     | TEXT    | FK → objekte |
| kategorie     | TEXT    | Dispatcher-Kategorie |
| doktyp        | TEXT    | betriebskostenabrechnung / rechnung / mietvertrag / grundsteuer / hausgeld / versicherung / sonstiges |
| absender      | TEXT    | Aussteller |
| datum_dokument| TEXT    | YYYY-MM-DD |
| betrag_eur    | REAL    | Gesamtbetrag |
| rohtext_md5   | TEXT    | Dedup-Hash |
| erstellt_am   | TIMESTAMP | CURRENT_TIMESTAMP |

### Tabelle `positionen`

| Feld        | Typ     | Beschreibung |
|-------------|---------|-------------|
| dokument_id | INTEGER | FK → dokumente |
| beschreibung| TEXT    | Positionstext |
| zeitraum    | TEXT    | z.B. 2025-01 / Q1/2025 / 2025 |
| betrag_eur  | REAL    | Positionsbetrag |
| kostenart   | TEXT    | grundsteuer / hausgeld / strom / wasser / gas / reparatur / versicherung / verwaltung / sonstiges |
| hinweise    | TEXT    | Freitext |

UNIQUE: `(dokument_id, beschreibung, zeitraum, betrag_eur)`

### Tabelle `mietvorgaenge`

| Feld           | Typ     | Beschreibung |
|----------------|---------|-------------|
| dokument_id    | INTEGER | FK → dokumente |
| objekt_id      | TEXT    | FK → objekte |
| mieter         | TEXT    | Mietername |
| zeitraum       | TEXT    | Abrechnungszeitraum |
| typ            | TEXT    | Dokumenttyp |
| betrag_eur     | REAL    | Betrag |
| nachzahlung_eur| REAL    | Nachzahlung/Guthaben bei BKA |

## Objekt-Erkennung (Keyword-Matching, Vorrang vor LLM)

```python
OBJEKT_KEYWORDS = [
    ("vm_1",   [r"lipowsky"]),
    ("vm_2",   [r"kornstraße", r"kornstr"]),
    ("vm_3",   [r"kolberger", r"troltsch"]),
    ("vm_4",   [r"schießhaus", r"schiesshaus"]),
    ("vm_5",   [r"schechen"]),
    ("vm_6",   [r"via dell'ospedale", r"via dell.ospedale"]),
    ("eigen_2",[r"podere dei venti"]),
    ("eigen_1",[r"grassauer", r"übersee"]),
]
```

## analyze.py — CLI

```bash
# Datenbank initialisieren
python3 ~/.claude/skills/immobilien/analyze.py --init

# PDF direkt analysieren
python3 ~/.claude/skills/immobilien/analyze.py pdf <pfad.pdf>

# Text analysieren (Wilson-Bypass)
python3 ~/.claude/skills/immobilien/analyze.py text "<text>" --quelle "datei.pdf"

# Einträge anzeigen
python3 ~/.claude/skills/immobilien/analyze.py list
python3 ~/.claude/skills/immobilien/analyze.py list --objekt vm_1
python3 ~/.claude/skills/immobilien/analyze.py list --objekt vm_1 --jahr 2025

# Fallback für italienische PDFs (qwen3:4b-instruct versagt manchmal bei IT)
python3 ~/.claude/skills/immobilien/analyze.py pdf <pfad.pdf> --model qwen3.5:4b
# gemma4:26b ist zu groß für Ryzen — nicht verwenden

# Überschreiben
python3 ~/.claude/skills/immobilien/analyze.py pdf <pfad.pdf> --force
```

## Web-Dashboard

**URL:** `http://192.168.86.195:8091/`

### Start

```bash
# systemd (dauerhaft)
systemctl --user enable immo-dashboard
systemctl --user start immo-dashboard
systemctl --user status immo-dashboard

# Manuell
cd ~/.claude/skills/immobilien && python3 dashboard.py
```

## Dispatcher-Integration

### Geänderte Dateien

**`dispatcher/docker-compose.yml`:**
```yaml
environment:
  - IMMO_DB_PATH=/data/immobilien/immobilien.db
  - IMMO_EXTRACT_MODEL=qwen3:4b-instruct
volumes:
  - /home/reinhard/.claude/skills/immobilien:/data/immobilien
```

**`dispatcher/dispatcher.py`:**
- Zeile 72–73: `IMMO_DB_PATH`, `IMMO_EXTRACT_MODEL`
- Zeilen ~841–1130: `_immo_is_immobiliendokument()`, `_ensure_immo_schema()`,
  `_write_immobilien_db()`, `_immo_extract_and_store()`, Keyword-Matching, LLM-Prompt
- Zeile ~12389: Bypass-Thread für `category_id=immobilien_eigen/vermietet`
- Zeile ~12766: `_write_immobilien_db()` im Non-Bypass-Pfad

### Aktivierung

```bash
cd "/home/reinhard/docker/RYZEN - docling-workflow"
# WICHTIG: --force-recreate, sonst wird der laufende Container nicht neu erstellt
docker compose up -d --force-recreate dispatcher
```

## Dispatcher-Kategorien

| Kategorie            | Routing |
|----------------------|---------|
| `immobilien_eigen`   | → `_immo_extract_and_store()` (Bypass) + `_write_immobilien_db()` (Normal) |
| `immobilien_vermietet`| → `_immo_extract_and_store()` (Bypass) + `_write_immobilien_db()` (Normal) |

## Claude Code Skill

```
/immobilien <pdf-pfad>
/immobilien list
/immobilien list --objekt vm_1
```

Automatischer Trigger: Datei aus `50 Immobilien eigen/` oder `51 Immobilien vermietet/`
oder Frontmatter `kategorie: immobilien_eigen/vermietet`.

## Batch-Import

```bash
cd ~/.claude/skills/immobilien && python3 batch_import.py
```

Scannt rekursiv `50 Immobilien/*.md`, extrahiert `original:`-Verweis,
prüft `already_done()` in dokumente, ruft `analyze.py pdf` pro neuem PDF.

## Aktueller Stand (2026-05-25)

| Metrik | Wert |
|--------|------|
| Dokumente | **346** |
| Positionen | **1105** |
| Batch-Import | **aktiv** — 1157 MDs in 50 Immobilien/, 99 bereit zum Import |
| Dashboard | **aktiv** — http://192.168.86.195:8091/ (systemd, autostart) |
| Dispatcher | **aktiv** — force-recreate ausgeführt, IMMO_DB_PATH gesetzt |

### Batch-Import Ergebnis nach Objekt

| Objekt | Dokumente |
|--------|-----------|
| Grassauer Straße 64 Übersee (eigen_1) | 27 |
| Via dell'ospedale Seggiano (vm_6) | 27 |
| Podere dei venti Seggiano (eigen_2) | 8 |
| Lipowskystraße München (vm_1) | 2 |
| Kolberger Straße Karlsruhe (vm_3) | 1 |
| Bahnhofstraße Schechen (vm_5) | 1 |
| kein Objekt (ING Energieausweis) | 1 |

### Nicht importierte PDFs (4)

| Datei | Grund |
|-------|-------|
| `20150620-Unbenannte_Notiz.pdf` | Fehlklassifikation im Vault, kein Immobilien-Dok |
| `20260419_ChinaToursde_Angebot.pdf` | Fehlklassifikation, Reiseangebot |
| `09052026.pdf` | PDF fehlt in Anlagen/ |
| `01102009_Dialog.pdf` | PDF fehlt in Anlagen/ |

### Bekannte Modell-Einschränkungen

- `qwen3:4b-instruct` (Standard): versagt bei einigen italienischen PDFs mit
  layout-schwerem pdftotext-Output (gibt SQL-Garbage statt JSON)
- Fallback: `--model qwen3.5:4b` (besser für IT-Dokumente)
- `gemma4:26b`: zu groß für Ryzen — nicht verwenden
- 2× Acquedotto-PDFs: erstes mit qwen3.5:4b importiert, zweites manuell per SQL

## Changelog

- **2026-05-17:** Initiale Erstellung durch externes Team — analyze.py, schema.sql, dashboard.py, immobilien.db
- **2026-05-17:** docker-compose.yml: IMMO_DB_PATH + Volume-Mount ergänzt (analog KV-Pattern)
- **2026-05-17:** immo-dashboard.service angelegt (systemd user, Port 8091)
- **2026-05-17:** PROTOKOLL.md erstellt
- **2026-05-17:** Batch-Import abgeschlossen — 68 Dokumente, 298 Positionen
- **2026-05-17:** Dispatcher force-recreate — Container lief noch mit alter Config ohne IMMO_DB_PATH
- **2026-05-17:** enex-tags.yaml: Immobilien-Routing-Regeln durch externes Team ergänzt
- **2026-05-25:** batch_import.py erstellt (analog KFZ/SV-Pattern). 6 MD-Dateien aus
  KFZ-Cleanup nach 50 Immobilien/ übernommen (CASAMIA, Reale Mutua Policen, Preventivo)
