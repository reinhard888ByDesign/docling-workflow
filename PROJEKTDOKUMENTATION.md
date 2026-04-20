# Docling-Workflow — Projektdokumentation

**Stand:** 2026-04-19
Lokale AI-Dokumentenpipeline: PDF → OCR → Klassifikation → Obsidian-Vault.
Läuft vollständig on-premise auf einem AMD Ryzen-Host (2 GB VRAM, kein Cloud-Zwang).

Strategische Architekturentscheidung 2026-04-19: **flaches Archiv + On-Demand-Verarbeitung** statt Vollrescan des Bestands. Auswertungen erfolgen bedarfsgesteuert über `cache-reader`-Suche → Dispatcher-Batch-Modus. Detail siehe `ARCHITEKTUR.md`, Phasen-Status in `PHASE2_PLAN.md`.

---

## Übersicht

```
Pi/Scanner
  │  PDF via Syncthing
  ▼
input-dispatcher/
  │
  ▼
[Dispatcher-Container]                       [cache-reader-Container]
  ├─ Docling-Serve  →  Markdown (OCR)        ├─ FastAPI (Port 8501)
  ├─ langdetect     →  Spracherkennung       ├─ SQLite FTS5
  ├─ Ollama/qwen    →  Übersetzung           └─ langdetect über Bestand
  ├─ Ollama/qwen    →  Klassifikation               │
  ├─ Hybrid-OCR-Gate (Cache→Docling)  ◄─────────────┘
  └─ vault_pfad     →  Obsidian-Vault / Anlagen/
         │
         ├─ Telegram-Benachrichtigung
         ├─ SQLite-DB  (dispatcher.db, inkl. batch_runs/batch_items)
         └─ Dashboard 8765  ( / · /pipeline · /cache · /batch · /review )
```

**Container:** `syncthing`, `docling-serve`, `document-dispatcher`, `cache-reader`
**Netzwerke:** `docling-net` (intern), `ollama-net` (extern, geteilt mit Open WebUI)
**Vault:** `/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault`
**Ports:** 8765 (Dispatcher-Dashboard + REST), 8501 (cache-reader)

---

## Vault-Struktur

```
reinhards-vault/
├── Anlagen/                   ← alle PDFs (Obsidian attachmentFolderPath)
├── 00 Inbox/                  ← unklassifiziert / OCR-Fehler
├── 10 Persönlich/
├── 20 Familie/
│   └── Haustiere/             ← Tierarzt-Rechnungen
├── 30 FengShui/
├── 40 Finanzen/
├── 49 Krankenversicherung/    ← Hauptordner KV
│   ├── Leistungsabrechnung Marion/[Jahr]/
│   ├── Leistungsabrechnung Reinhard/[Jahr]/
│   ├── Leistungsabrechnung Sonstiges/[Jahr]/
│   ├── Arztrechnung/[Jahr]/
│   ├── Beitragsinformation/[Jahr]/
│   ├── Rezept/[Jahr]/
│   ├── Sonstiges/[Jahr]/
│   ├── 00 Wiederherstellung/  ← OCR-Stubs zum Nachbearbeiten
│   └── undatiert/
├── 50 Immobilien eigen/
├── 51 Immobilien vermietet/
├── 55 Garten/
├── 60 Fahrzeuge/
├── 70 Italien/
├── 80 Business/
├── 82 Digitales/
├── 85 Wissen/
├── 90 Reisen/
└── 99 Archiv/
```

Aktuelle Jahresdateien landen direkt im Typ-Unterordner, Vorjahre in `/{Jahr}/`.

---

## Dispatcher-Architektur

### Konfigurationsdateien

| Datei | Zweck |
|---|---|
| `dispatcher-config/categories.yaml` | Taxonomie (Kategorien, Typen, Routing, Hints) |
| `dispatcher-config/absender.yaml` | Absender-DB mit `adressat_default` pro Firma |
| `dispatcher-config/personen.yaml` | Personendaten (Cod. Fiscale, IBAN) für Adressat-Auflösung |
| `dispatcher-config/doc_types.yaml` | Keyword-Tabelle für strukturierte Dokumenttyp-Erkennung |

**Leitprinzip:** `categories.yaml` ist Single Source of Truth. Der Python-Code liest, nie hardcodet.

### Pipeline-Phasen (pro PDF)

1. **Datei-Stabilität** — `wait_for_file_stable()`: 3× prüfen ob Größe konstant bleibt
2. **Duplikat-Check** — MD5-Hash gegen DB (`pdf_hash`-Spalte) + Dateiname-Check
3. **OCR** — Docling-Serve via HTTP (`/v1alpha/convert/file/async`)
4. **OCR-Qualitäts-Gate** — `< 300 Zeichen` → Inbox + Telegram-Warnung, kein LLM-Aufwand
5. **Header-Extraktion** — Regex: PLZ, Firmenformen, Personennamen → `*.header.json`
6. **Identifier-Extraktion** — Cod. Fiscale, IBAN, USt-IdNr → Adressat/Absender deterministisch
7. **Sprach-Erkennung** — langdetect; Schwellwert 0.85
8. **Übersetzung** — Ollama (`OLLAMA_TRANSLATE_MODEL`, default `qwen2.5:7b`) bei nicht-DE
9. **Klassifikation** — Ollama (`OLLAMA_MODEL`) mit strukturiertem JSON-Prompt
10. **Halluzinations-Guard** — `category_id` + `type_id` gegen geladene Taxonomie validieren
11. **Dateiname** — `build_clean_filename()`: YYYYMMDD_Absender_Thema
12. **Vault-Pfad** — `build_vault_path()`: aus `TYPE_ROUTING`-Dict (aus categories.yaml)
13. **MD-Schreiben** — Frontmatter + Markdown-Body in Vault
14. **PDF → Anlagen/** — Kopie in `VAULT_PDF_ARCHIV` (= `Anlagen/`)
15. **DB** — `save_to_db()`: Dokument + Rechnungs-/Erstattungs-Positionen
16. **Telegram** — Klassifikationsergebnis mit Per-Feld-Konfidenz-Icons

### Vault-Pfad-Routing

```python
build_vault_path(category_id, type_id, adressat, year, md_filename)
```

- `vault_folder` aus `CATEGORY_TO_VAULT_FOLDER[category_id]`
- `vault_subfolder` + `person_subfolder` aus `TYPE_ROUTING[(category_id, type_id)]`
- Aktuelles Jahr → direkt im Typ-Ordner, Vorjahre → `/{year}/`
- Fallback: `00 Inbox`

### categories.yaml — Aufbau eines Typs

```yaml
categories:
  krankenversicherung:
    label: "Krankenversicherung"
    vault_folder: "49 Krankenversicherung"
    types:
      - id: leistungsabrechnung
        label: "Leistungsabrechnung"
        vault_subfolder: "Leistungsabrechnung"   # Unterordner
        person_subfolder: true                   # + adressat als Suffix
        adressat_fallback: "Sonstiges"           # wenn adressat leer
        telegram_template: leistungsabrechnung   # Nachrichtenformat
        hints: [...]                             # LLM-Erkennungshinweise
```

Neue Kategorien/Typen: nur YAML ändern, kein Python anfassen.

---

## Datenbank (SQLite)

Datei: `dispatcher-temp/dispatcher.db`

### Tabellen

**`dokumente`** — ein Eintrag pro verarbeitetem PDF  
`id`, `dateiname`, `pdf_hash`, `rechnungsdatum`, `kategorie`, `typ`, `absender`, `adressat`, `konfidenz`, `vault_pfad`, `erstellt_am`

**`rechnungen`** — offene Arzt-/Sonstige-Rechnungen  
`id`, `dokument_id`, `rechnungsbetrag`, `faelligkeitsdatum`, `status` (offen/erstattet/teilweise_erstattet), `erstattungsdatum`

**`erstattungspositionen`** — Leistungsabrechnungs-Positionen  
`id`, `dokument_id`, `rechnung_id`, `leistungserbringer`, `zeitraum`, `rechnungsbetrag`, `erstattungsbetrag`, `erstattungsprozent`

**`aussteller`** / **`aussteller_aliases`** — Absender-Stammdaten

**`klassifikations_historie`** — jede LLM-Klassifikation + manuelle Korrekturen  
`llm_model`, `translate_model`, `lang_detected`, `lang_prob`, `duration_ms`, `raw_response`, `final_category`, `final_type`, `konfidenz_*`, `korrektur_von_user`

### Hash-Duplikat-Schutz

Beim Eingang wird MD5 des PDFs berechnet. Treffer in `pdf_hash` → sofort verwerfen + Telegram `♻️ Duplikat`. Schützt gegen Syncthing-Mehrfachlieferungen.

---

## Telegram-Integration

- **Inline-Keyboard** nach Klassifikation: Korrektur-Button pro Kategorie/Typ
- **Per-Feld-Konfidenz:** 🟢 hoch / 🟡 mittel / 🔴 niedrig (je Absender, Adressat, Datum)
- **PDF-Vorschau:** Datei wird direkt in den Chat gesendet
- **Korrektur-Flow:** Nutzer wählt → `handle_correction()` verschiebt MD + aktualisiert DB + schreibt Historien-Eintrag

---

## API (Port 8765)

| Endpoint | Methode | Beschreibung |
|---|---|---|
| `/status` | GET | Dispatcher-Status + Warteschlange |
| `/dokumente` | GET | Alle Dokumente (`?kategorie=`, `?limit=`) |
| `/dokumente/{id}` | GET | Einzeldokument mit MD-Inhalt |
| `/frage` | POST | Natural-Language-Query gegen SQLite (Ollama) |
| `/korrektur` | POST | Manuelle Kategorie-/Typ-Korrektur |
| `/api/cache/*` | GET | Proxy zu cache-reader (Search, File) |
| `/api/batch/runs` | GET | Liste Batch-Läufe |
| `/api/batch/runs/{id}` | GET | Detail + Items |
| `/api/batch/start` | POST | Batch-Lauf starten (`input`, `ocr_mode`, `output_mode`) |
| `/api/batch/runs/{id}/{pause\|resume\|abort}` | POST | Lauf-Steuerung |
| `/api/batch/runs/{id}/download?kind={summary\|details}` | GET | CSV/JSONL-Stream |

## Dashboard-Seiten (Port 8765)

| Pfad | Inhalt |
|---|---|
| `/` | Übersicht + Kennzahlen |
| `/pipeline` | Live-Pipeline-Anzeige |
| `/cache` | cache-reader-Suche, Sprachverteilung, Stale-Pfade |
| `/batch` | Batch-Läufe, Historie, Download (Phase 2.4) |
| `/review` | Klassifikationen prüfen/korrigieren |

## cache-reader-Service

**Container:** `cache-reader` auf Port 8501. Eigenständiger FastAPI-Dienst, indexiert den OCR-Cache via SQLite FTS5 + langdetect über den Bestand (~1.300 PDFs).

| Endpoint | Beschreibung |
|---|---|
| `GET /search?q=…&limit=N` | Volltextsuche, liefert `path`, `score`, `excerpt`, `langs` |
| `GET /file?path=…` | Voll-Text eines Dokuments (vault-relativer Pfad) |
| `GET /stats` | Index-Größe, Sprachverteilung, Stale-Pfad-Quote |

Wird vom Dispatcher im Hybrid-OCR-Gate (Phase 2.2) angefragt: Bei ≥500 Zeichen + plausibler Sprache (de/it/en) wird Cache-Text genutzt; sonst Docling-Fallback.

## Dispatcher-Batch-Modus (CLI)

```bash
docker exec document-dispatcher python /app/dispatcher.py \
  --batch /tmp/input.json \
  --ocr-source hybrid \
  --output structured \
  --output-dir /tmp/run_001 \
  [--limit N] [--dry-run] [--resume RUN_ID]
```

| Flag | Werte | Default |
|---|---|---|
| `--batch` | JSON (cache-reader-Format) oder Textliste | — |
| `--ocr-source` | `cache` / `docling` / `hybrid` | `hybrid` |
| `--output` | `vault-move` / `classify-only` / `structured` | `vault-move` |
| `--output-dir` | Zielordner für CSV/JSONL bei `structured` | — |
| `--resume` | Lauf wieder aufnehmen (Logik in Arbeit) | — |

`structured` schreibt `run_<id>_summary.csv` + `run_<id>_details.jsonl`. `vault-move` ist im Batch unterdrückt — schützt das Archiv vor versehentlicher Verschiebung.

---

## Absender-Wissensbasis

`dispatcher-config/absender.yaml` — wichtige Einträge:

| Absender | `adressat_default` | Hinweis |
|---|---|---|
| HUK-COBURG | Marion | Private KV |
| Gothaer | Reinhard | Private KV |
| vigo | Marion | Pflegezusatzversicherung |
| Barmenia | Reinhard | |

Identifikation erfolgt deterministisch via `resolve_absender()`: Cod. Fiscale / IBAN → `personen.yaml`, dann Keyword-Match in Header.

---

## Wartungs-Skripte

| Skript | Zweck |
|---|---|
| `retrofit_frontmatter.py` | `original:`-Feld in alten MDs auf `[[Anlagen/...]]`-Format korrigieren |
| `cleanup_49_kv.py` | 49-KV-Bereinigung: Fehlklassifizierungen, Deduplizierung, Typ-Unterordner |
| `rebuild_vault_pfad.py` | `vault_pfad` in DB nach Vault-Umstrukturierungen neu aufbauen |
| `dispatcher/analyze_classifications.py` | Statistiken: Hit-Rate, Halluzinationen, Korrekturen pro Modell |
| `batch_reimport.py` | **deprecated** — Vorgänger des Dispatcher-Batch-Modus |

---

## 49 Krankenversicherung — Bereinigung 2026-04

**Ausgangslage:** 5047 Dateien, ~3000 Duplikate, kein Typ-Routing, Evernote-Altformat

**Durchgeführt:**
1. Fehlklassifizierungen verschoben (23 Dateien in andere Vault-Ordner)
2. LEAS-UUID-Dateien umbenannt
3. Duplikate gelöscht (3008, PDF-Hash-basiert): **5047 → 2011 Dateien**
4. OCR-Stubs (621) → `00 Wiederherstellung/` mit `todos:`-Frontmatter
5. Frontmatter-Upgrade: Evernote-Felder → `kategorie_id`, `typ_id`, normierter `adressat`
6. Typ-Unterordner erstellt und befüllt
7. DB `vault_pfad` rebuild: 1009 Einträge aktualisiert

**Ergebnis-Struktur:**
```
49 Krankenversicherung/
  Leistungsabrechnung Marion/  413 Dateien
  Leistungsabrechnung Reinhard/294 Dateien
  Arztrechnung/                282 Dateien
  Sonstiges/                    87 Dateien
  Beitragsinformation/          28 Dateien
  Rezept/                       28 Dateien
  Leistungsabrechnung Sonstiges/ 22 Dateien
  00 Wiederherstellung/        621 Stubs
  undatiert/                    36 Dateien
  [Jahresordner 2013–2025]     199 nicht typisiert
```

---

## Umgebungsvariablen

```env
WATCH_DIR=/data/input-dispatcher
TEMP_DIR=/data/dispatcher-temp
CONFIG_FILE=/config/categories.yaml
DOCLING_URL=http://docling-serve:5001
OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TRANSLATE_MODEL=qwen2.5:7b     # separates Modell für Übersetzung
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
VAULT_PDF_ARCHIV=/data/reinhards-vault/Anlagen
VAULT_ROOT=/data/reinhards-vault
API_PORT=8765
```

---

## Deployment

```bash
# Build + Start
docker compose build dispatcher
docker compose up -d

# Logs
docker logs -f document-dispatcher

# DB-Rebuild nach Vault-Umstrukturierung
python3 rebuild_vault_pfad.py

# Klassifikations-Statistiken
docker exec document-dispatcher python3 analyze_classifications.py
```

---

## Roadmap (offen)

**Aktuelle Phase 2 (Stand 2026-04-19):** Schritte 2.0–2.4 fertig (Auto-Rescan entfernt, CLI `--batch`, Hybrid-OCR-Gate, CSV/JSONL-Export, Dashboard `/batch`). Direkt offen vor 2.5: Container-Rebuild + Browser-Check `/batch`, Resume-Logik, Ollama-Stabilität (Retry/Warm-up). Detail in `PHASE2_PLAN.md`.

**Folgephasen laut `ARCHITEKTUR.md`:**
- 2.5 Duplikat-Erkennung + Quarantäne
- 2.6 Frontmatter-Vereinheitlichung
- 2.7 Telegram-Dialog bei niedriger Konfidenz
- 2.8 Admin-Web-Interface für Stammdaten
- 3 Telegram-Befehle `/suche`, `/verarbeite`, `/auswertung`
- 4 Standard-Templates (Steuer-Handwerker, KV-Erstattung, Italien-Jahr)
- 5 Wilson-Monitoring + Haupt-Dashboard-Finalisierung

**Unabhängige Punkte:**
- **`00 Wiederherstellung/`** durcharbeiten: 621 Stubs (lazy Re-OCR via Hybrid-Gate vorgesehen)
- **Jahresordner 2013–2025** tiefergehend typisieren (199 Dateien ohne `typ_id`)
- **`analyze_classifications.py`** für regelmäßige Hit-Rate-Messung
- **Italienische Datumsextraktion** (`rechnungsdatum` aus ital. Texten unzuverlässig — Phase-2.6-Stichprobenvergleich)
