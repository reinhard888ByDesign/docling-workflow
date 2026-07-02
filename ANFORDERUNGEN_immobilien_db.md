# Anforderungsdokument: immobilien.db

**Stand:** 2026-05-17  
**Auftraggeber:** Reinhard  
**Ziel:** Automatische Extraktion und Speicherung von Immobilien-Dokumenten in einer dedizierten SQLite-Datenbank, analog zur bestehenden `kk_leistungen.db` für Krankenversicherungs-Leistungsabrechnungen.

---

## 1. Hintergrund und Systemüberblick

Das Docling-Workflow-System verarbeitet gescannte Dokumente vollautomatisch:

```
Scanner (Mac) → smb://wilson/incoming
  → Wilson (Pi 5, doc_processor.py):
      OCR (Port 8765), LLM-Metadatenextraktion, Sidecar (.meta.json)
  → Syncthing → Ryzen (~/input-dispatcher/)
  → dispatcher.py:
      Sidecar-Bypass (kein erneutes OCR/LLM) → Vault (Obsidian .md)
      + Hintergrund-Threads für Domänen-Extraktion (z.B. kk_leistungen.db)
```

**Relevante Dateien (alle auf Ryzen):**
- `dispatcher/dispatcher.py` — Hauptdatei, ~12.500 Zeilen
- `dispatcher-config/categories.yaml` — Kategorie-Definitionen
- `dispatcher-config/absender.yaml` — Absender-Datenbank
- `~/.claude/skills/leistungsabrechnung/` — Referenz-Implementierung (KV)

---

## 2. Ziel: immobilien.db

Eine SQLite-Datenbank `immobilien.db`, die automatisch befüllt wird, wenn der Dispatcher ein Dokument mit `category_id=immobilien_eigen` oder `category_id=immobilien_vermietet` verarbeitet.

**Analogie zur bestehenden Implementierung:**

| KV-Bypass (Referenz) | Immobilien-Bypass (zu bauen) |
|---|---|
| `_kv_la_is_leistungsabrechnung()` | `_immo_is_immobiliendokument()` |
| `_kv_extract_and_store()` | `_immo_extract_and_store()` |
| `_write_kk_leistungen_db()` | `_write_immobilien_db()` |
| `kk_leistungen.db` | `immobilien.db` |
| Hintergrund-Thread nach Sidecar-Bypass | identisch |

---

## 3. Immobilien-Portfolio

### 3.1 Eigene Immobilien (`immobilien_eigen`)
| ID | Objekt | Ort | Zeitraum |
|---|---|---|---|
| eigen_1 | Grassauer Straße 64 | Übersee, Deutschland | bis 2022 |
| eigen_2 | Podere dei venti | Seggiano, Italien | ab 2022 |

### 3.2 Vermietete Immobilien (`immobilien_vermietet`)
| ID | Straße | Stadt |
|---|---|---|
| vm_1 | Lipowskystraße | München |
| vm_2 | Kornstraße | Bremen |
| vm_3 | Kolberger Straße | Karlsruhe |
| vm_4 | Schießhausstraße | Neuburg |
| vm_5 | Bahnhofstraße | Schechen |
| vm_6 | Via dell'ospedale | Seggiano |

---

## 4. Datenbankschema

Datei: `immobilien.db` (SQLite)

### 4.1 Tabelle `objekte`

Statische Referenztabelle — einmalig befüllt, nicht durch den Dispatcher verändert.

```sql
CREATE TABLE IF NOT EXISTS objekte (
    id          TEXT PRIMARY KEY,          -- z.B. "vm_1", "eigen_2"
    bezeichnung TEXT NOT NULL,             -- z.B. "Lipowskystraße München"
    strasse     TEXT,
    ort         TEXT,
    land        TEXT DEFAULT 'DE',
    typ         TEXT NOT NULL,             -- 'eigen' oder 'vermietet'
    aktiv_von   TEXT,                      -- YYYY oder YYYY-MM-DD
    aktiv_bis   TEXT                       -- NULL = aktuell
);
```

Initialdaten (einmalig einfügen beim Schema-Setup):
```sql
INSERT OR IGNORE INTO objekte VALUES
('eigen_1','Grassauer Straße 64 Übersee','Grassauer Straße 64','Übersee','DE','eigen','2000','2022'),
('eigen_2','Podere dei venti Seggiano','Podere dei venti','Seggiano','IT','eigen','2022',NULL),
('vm_1','Lipowskystraße München','Lipowskystraße','München','DE','vermietet',NULL,NULL),
('vm_2','Kornstraße Bremen','Kornstraße','Bremen','DE','vermietet',NULL,NULL),
('vm_3','Kolberger Straße Karlsruhe','Kolberger Straße','Karlsruhe','DE','vermietet',NULL,NULL),
('vm_4','Schießhausstraße Neuburg','Schießhausstraße','Neuburg','DE','vermietet',NULL,NULL),
('vm_5','Bahnhofstraße Schechen','Bahnhofstraße','Schechen','DE','vermietet',NULL,NULL),
('vm_6','Via dell\'ospedale Seggiano','Via dell\'ospedale','Seggiano','IT','vermietet',NULL,NULL);
```

### 4.2 Tabelle `dokumente`

Ein Eintrag pro verarbeitetem PDF.

```sql
CREATE TABLE IF NOT EXISTS dokumente (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    quelle_pdf      TEXT NOT NULL UNIQUE,  -- Dateiname ohne Pfad
    objekt_id       TEXT REFERENCES objekte(id),
    kategorie       TEXT NOT NULL,         -- 'immobilien_eigen' oder 'immobilien_vermietet'
    doktyp          TEXT,                  -- z.B. 'betriebskostenabrechnung', 'rechnung', 'mietvertrag', 'grundsteuer', 'nebenkosten', 'sonstiges'
    absender        TEXT,                  -- Lieferant/Aussteller laut Sidecar
    datum_dokument  TEXT,                  -- ISO YYYY-MM-DD, aus Dokumentinhalt
    betrag_eur      REAL,                  -- Gesamtbetrag des Dokuments (wenn eindeutig)
    rohtext_md5     TEXT,                  -- MD5 des extrahierten PDF-Texts (Deduplizierung)
    erstellt_am     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_immo_dok_objekt ON dokumente(objekt_id);
CREATE INDEX IF NOT EXISTS idx_immo_dok_datum  ON dokumente(datum_dokument);
CREATE INDEX IF NOT EXISTS idx_immo_dok_typ    ON dokumente(doktyp);
```

### 4.3 Tabelle `positionen`

Einzelpositionen aus Rechnungen, Abrechnungen etc. — mehrere pro Dokument möglich.

```sql
CREATE TABLE IF NOT EXISTS positionen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dokument_id     INTEGER NOT NULL REFERENCES dokumente(id),
    beschreibung    TEXT NOT NULL,         -- z.B. "Grundsteuer Q1", "Hausgeld März", "Reparatur Dach"
    zeitraum        TEXT,                  -- z.B. "2025-01", "Q1/2025", "2025"
    betrag_eur      REAL,
    kostenart       TEXT,                  -- z.B. 'grundsteuer', 'hausgeld', 'strom', 'wasser', 'reparatur', 'versicherung', 'sonstiges'
    hinweise        TEXT,
    UNIQUE(dokument_id, beschreibung, zeitraum, betrag_eur)
);

CREATE INDEX IF NOT EXISTS idx_immo_pos_dok ON positionen(dokument_id);
CREATE INDEX IF NOT EXISTS idx_immo_pos_kostenart ON positionen(kostenart);
```

### 4.4 Tabelle `mietvorgaenge` (nur `immobilien_vermietet`)

Mieteinnahmen und Betriebskostenabrechnungen.

```sql
CREATE TABLE IF NOT EXISTS mietvorgaenge (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dokument_id     INTEGER NOT NULL REFERENCES dokumente(id),
    objekt_id       TEXT REFERENCES objekte(id),
    mieter          TEXT,
    zeitraum        TEXT,                  -- z.B. "2025", "2025-01"
    typ             TEXT,                  -- 'mieteinnahme', 'nebenkostenabrechnung', 'mietvertrag'
    betrag_eur      REAL,
    nachzahlung_eur REAL,                  -- positiv = Nachzahlung, negativ = Guthaben
    hinweise        TEXT
);
```

---

## 5. Objekt-Erkennung (Matching-Logik)

Das LLM und/oder Keyword-Matching muss erkennen, welches Objekt ein Dokument betrifft. Reihenfolge:

1. **Keyword-Match im PDF-Text** (deterministisch, Vorrang):
   - `Lipowsky` → `vm_1`
   - `Kornstraße`, `Kornstr` → `vm_2`
   - `Kolberger`, `Troltsch` → `vm_3` (Troltsch = Hausverwaltung Karlsruhe)
   - `Schießhaus`, `Schiesshaus` → `vm_4`
   - `Schechen` → `vm_5`
   - `Via dell'ospedale`, `Seggiano` + Mietkontext → `vm_6`
   - `Podere dei venti`, `Seggiano` + Eigentumskontext → `eigen_2`
   - `Grassauer`, `Übersee` → `eigen_1`

2. **LLM-Extraktion** (Fallback, wenn Keyword kein Treffer):
   - LLM soll `objekt_id` aus der obigen Liste zurückgeben oder `null`

3. **Kategorie als Fallback**:
   - `immobilien_eigen` ohne Keyword → `eigen_2` (aktueller Hauptwohnsitz)
   - `immobilien_vermietet` ohne Keyword → `objekt_id=null`

---

## 6. LLM-Extraktions-Prompt

Das LLM (Ollama `qwen3:4b-instruct`, identisch zur KV-Pipeline) soll folgendes JSON zurückgeben:

```
Du bist ein Spezialist für die Extraktion strukturierter Daten aus Immobilien-Dokumenten.
Extrahiere alle relevanten Informationen und gib sie als JSON zurück.

Objekte:
- eigen_1: Grassauer Straße 64, Übersee (DE) — bis 2022
- eigen_2: Podere dei venti, Seggiano (IT) — ab 2022
- vm_1: Lipowskystraße, München
- vm_2: Kornstraße, Bremen
- vm_3: Kolberger Straße, Karlsruhe (Hausverwaltung: Troltsch)
- vm_4: Schießhausstraße, Neuburg
- vm_5: Bahnhofstraße, Schechen
- vm_6: Via dell'ospedale, Seggiano

Gib folgendes JSON zurück (keine anderen Felder, kein Markdown):
{
  "objekt_id": "<ID aus Liste oben oder null>",
  "doktyp": "<betriebskostenabrechnung|rechnung|mietvertrag|grundsteuer|hausgeld|versicherung|sonstiges>",
  "datum_dokument": "<YYYY-MM-DD oder null>",
  "betrag_eur": <Gesamtbetrag als Zahl oder null>,
  "absender": "<Aussteller/Lieferant>",
  "positionen": [
    {
      "beschreibung": "<Positionstext>",
      "zeitraum": "<z.B. 2025-01 oder Q1/2025 oder 2025 oder null>",
      "betrag_eur": <Zahl oder null>,
      "kostenart": "<grundsteuer|hausgeld|strom|wasser|gas|reparatur|versicherung|verwaltung|sonstiges>"
    }
  ],
  "mieter": "<Name des Mieters wenn erkennbar, sonst null>",
  "nachzahlung_eur": <Nachzahlungs- oder Guthabenbetrag wenn BKA, sonst null>
}

Dokumenttext:
```

**Technische Parameter** (identisch zur KV-Pipeline):
- `temperature: 0.1`
- `num_predict: 4096`
- Ollama-URL: `http://host.docker.internal:11434/api/generate` (bzw. Fallback `172.17.0.1`, `192.168.86.195`)
- Max. Textlänge: `text[:12000]`

---

## 7. Erkennungs-Funktion: `_immo_is_immobiliendokument()`

```python
def _immo_is_immobiliendokument(result: dict) -> bool:
    """Erkennt ob ein Bypass-Dokument ein Immobilien-Dokument ist."""
    if not IMMO_DB_PATH:
        return False
    cat = (result.get("category_id") or "")
    return cat in ("immobilien_eigen", "immobilien_vermietet")
```

Keine weiteren Bedingungen — jedes Dokument in diesen Kategorien wird extrahiert.

---

## 8. Dispatcher-Integration

### 8.1 Umgebungsvariablen (in `docker-compose.yml` ergänzen)

```yaml
environment:
  - IMMO_DB_PATH=/data/immobilien/immobilien.db
  - IMMO_EXTRACT_MODEL=qwen3:4b-instruct
```

Volume:
```yaml
volumes:
  - ~/.claude/skills/immobilien:/data/immobilien
```

### 8.2 Einfügestelle in `dispatcher.py`

Direkt nach dem bestehenden KV-Block (ca. Zeile 12081), im Sidecar-Bypass-Pfad:

```python
# KV-Leistungsabrechnung (bestehend)
if _kv_la_is_leistungsabrechnung(result_bypass):
    threading.Thread(
        target=_kv_extract_and_store,
        args=(file_path, dict(result_bypass)),
        daemon=True,
        name=f"kv-extract-{file_path.stem}",
    ).start()

# NEU: Immobilien-Dokument
if _immo_is_immobiliendokument(result_bypass):
    threading.Thread(
        target=_immo_extract_and_store,
        args=(file_path, dict(result_bypass)),
        daemon=True,
        name=f"immo-extract-{file_path.stem}",
    ).start()
    log.info(f"Immo-Extraktion gestartet (Hintergrund): {file_path.name}")
```

### 8.3 Globale Konfigurationsvariablen (am Anfang von dispatcher.py ergänzen, neben den KV-Vars)

```python
IMMO_DB_PATH = os.getenv("IMMO_DB_PATH", "")
IMMO_EXTRACT_MODEL = os.getenv("IMMO_EXTRACT_MODEL", "qwen3:4b-instruct")
```

---

## 9. Dateistruktur (neu anzulegen)

```
~/.claude/skills/immobilien/          ← Volume-Mount /data/immobilien
├── immobilien.db                     ← SQLite, wird automatisch erstellt
├── schema.sql                        ← Schema + INSERT der Objekte
├── analyze.py                        ← Standalone-Extraktor (manueller Einsatz)
├── dashboard.py                      ← Web-Dashboard Port 8091
└── templates/
    └── index.html
```

---

## 10. Standalone-Extraktor `analyze.py`

Für manuellen Einsatz (identisches Muster wie `~/.claude/skills/leistungsabrechnung/analyze.py`):

```
python analyze.py <PDF-Datei>
```

- Extrahiert Text via `pdfminer.six`
- Schickt an Ollama
- Schreibt in `immobilien.db`
- Gibt Ergebnis auf stdout aus

---

## 11. Web-Dashboard `dashboard.py`

Port **8091** (8090 ist kk_leistungen-Dashboard).

Ansichten:
- **Übersicht**: Alle Objekte mit Dokumentanzahl, letztem Beleg, Jahreskosten
- **Objekt-Detail**: Alle Dokumente + Positionen für ein Objekt, filterbar nach Jahr/Kostenart
- **Jahresvergleich**: Kostenentwicklung pro Objekt über Jahre

Framework: Flask (analog zu `dashboard.py` in kk_leistungen).

---

## 12. Fehlerbehandlung

Identisch zur KV-Pipeline:
- Fehler im Hintergrund-Thread loggen (`log.warning(...)`) — Dispatcher-Hauptpfad darf nie blockieren
- Leerer PDF-Text → Warning + return
- Leere Positionen-Liste → Warning + return
- DB-Fehler → Warning + return (kein crash)
- Duplikat-Insert → `INSERT OR IGNORE` / UNIQUE-Constraint abfangen

---

## 13. Akzeptanzkriterien

1. Ein PDF mit `category_id=immobilien_vermietet` (z.B. Betriebskostenabrechnung Lipowskystraße) landet nach Dispatcher-Verarbeitung in `immobilien.db` mit korrektem `objekt_id=vm_1`.
2. `objekte`-Tabelle enthält alle 8 Objekte nach Schema-Setup.
3. Dashboard auf Port 8091 zeigt Jahresübersicht pro Objekt.
4. Dispatcher-Hauptpfad wird durch Extraktion nicht verzögert (Hintergrund-Thread).
5. Doppelte PDFs (gleicher MD5) werden nicht doppelt eingetragen.
6. `analyze.py` kann manuell auf beliebige PDF aufgerufen werden.

---

## 14. Referenz-Implementierung

Das vollständige Muster (KV-Bypass) zum Nachbauen ist in `dispatcher.py` ab Zeile ~764:
- `_kv_la_is_leistungsabrechnung()` — Erkennung
- `_kv_pdf_to_text()` — PDF → Text (pdfminer.six)
- `_kv_extract_and_store()` — Orchestrierung
- `_write_kk_leistungen_db()` — DB-Schreiben
- `_ensure_kk_schema()` — Schema-Init

Und als Standalone: `~/.claude/skills/leistungsabrechnung/analyze.py`, `schema.sql`, `dashboard.py`.
