# Vault-Konsolidierung & Dokumentenmanagement

**Stand:** 2026-04-13
**Server:** Ryzen (192.168.86.195)

---

## Ziel

1. Zwei Vaults (Reinhards Vault + Obsidian Vault) zu einem zusammenführen
2. PDFs nur auf Ryzen speichern, per Backup auf UNAS 2 sichern
3. Alle Dokumente klassifizieren (Kategorie → Typ) und mit Frontmatter versehen
4. Dispatcher-Pipeline anpassen

---

## Ausgangslage (vor Beginn)

| Vault | Pfad auf Ryzen | Größe | PDFs | MDs |
|---|---|---|---|---|
| Reinhards Vault | `syncthing/data/reinhards-vault/` | 5.4 GB | 2.199 (davon 2.193 in `Anlagen/`) | 1.669 |
| Obsidian Vault (Silo) | `syncthing/data/obsidian-vault/` | 823 MB | 874 (in `Originale/`) | 1.282 (in `Converted/`) |

**Syncthing-Folder:**
| Folder-ID | Mac-Pfad | Typ Mac | Ryzen-Pfad |
|---|---|---|---|
| `reinhards-vault` | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Reinhards Vault` | sendreceive | `/data/reinhards-vault` |
| `obsidian-vault` | `~/Documents/obsidian-vault` | sendreceive | `/data/obsidian-vault` (entfernt) |
| `input-dispatcher` | `~/Documents/input-dispatcher` | sendreceive | `/data/input-dispatcher` |

---

## Phasenplan

### Phase 1 — Speicherproblem lösen

#### 1.1 PDFs aus Syncthing-Sync ausschließen
- `.stignore` auf Mac + Ryzen für `reinhards-vault`:
  ```
  Anlagen/
  *.pdf
  ```
- **Status: ERLEDIGT (2026-04-09)**
- Ergebnis: Local Files 8.451 → 4.225, Local Bytes 5.34 GB → 0.96 GB

#### 1.2 PDFs auf Ryzen in zentrales Archiv konsolidieren
- Ziel: `~/pdf-archiv/` (außerhalb aller Sync-Folder)
- **Status: ERLEDIGT (2026-04-09)**
- Ergebnis:
  - `Anlagen/` → 2.193 PDFs verschoben
  - `Originale/` → 846 PDFs verschoben (1 Duplikat: `20191215.pdf` vs. `20191215_2.pdf` — unterschiedliche Dateien, gleicher Name)
  - 6 verstreute PDFs aus Vault kopiert
  - **Total: 3.045 PDFs, 3.4 GB in `~/pdf-archiv/`**
  - `Anlagen/` jetzt leer, `Originale/` jetzt leer

#### 1.3 Backup auf UNAS 2 einrichten
- Inkrementelles Backup: `~/pdf-archiv/` → UNAS 2 (`//192.168.86.159/Ryzen`)
- **Status: ERLEDIGT (2026-04-10)**
- Ergebnis:
  - CIFS-Mount persistent via `/etc/fstab` → `/mnt/ryzen-drive`
  - Credentials in `/etc/samba/ryzen-drive.creds`
  - Backup-Skript: `/usr/local/bin/backup_pdf_archiv.sh`
    - Ziel: `/mnt/ryzen-drive/backups/pdf-archiv/YYYY-MM-DD/`
    - Hardlinks auf vorheriges Backup (inkrementell, platzsparend)
    - Prüft ob Mount aktiv, rsync mit `--link-dest`
  - Systemd Timer: `backup-pdf-archiv.timer` — täglich automatisch
  - Manuell: `sudo systemctl start backup-pdf-archiv.service`

#### 1.4 Obsidian-Vault Sync-Folder abschalten
- Syncthing-Folder `obsidian-vault` auf beiden Seiten entfernt
- **Status: ERLEDIGT (2026-04-10)** — erledigt als Teil von Phase 2.3

---

### Phase 2 — Vaults zusammenführen

#### 2.1 Converted-MDs aus Obsidian-Vault nach Reinhards Vault migrieren
- 1.276 .md Dateien aus `obsidian-vault/Converted/` kopiert + 50 Dateien aus `obsidian-vault/Inbox/`
- **Status: ERLEDIGT (2026-04-10)**
- Ergebnis:

| Converted/ (Quelle) | → Reinhards Vault (Ziel) | Dateien |
|---|---|---|
| `krankenkasse/*` | `49 Krankenversicherung/Converted/` | 1.251 |
| `finanzen/*` | `40 Finanzen/Converted/` | 12 |
| `inbox/*` | `00 Inbox/Converted/` | 12 |
| `anleitungen/*` | `95 Bedienungsanleitungen/Converted/` | 1 |
| `Inbox/*` (Root) | `00 Inbox/` | 50 |

- Unterordnerstruktur (typ/jahr/) erhalten
- Alle PDF-Links (1.636 Embeds, Markdown-Links, Frontmatter) umgeschrieben: `Originale/...` → `file:///Volumes/reinhard/pdf-archiv/...` (URL-encoded)
- PDFs klickbar in Obsidian auf Mac via SMB-Mount (`smb://192.168.86.195/reinhard` → `/Volumes/reinhard/`)
- Syncthing `reinhards-vault` auf beiden Seiten auf `sendreceive` umgestellt (dauerhaft bidirektional)

#### 2.2 Dateinamen-Konvention: Datumspräfix
- **Konvention:** Jede .md-Datei (und zugehöriges PDF) beginnt mit `YYYYMMDD_` (Datum aus dem Dokumentinhalt)
- **Datum-Ermittlung (Priorität):**
  1. Datum aus Dokumentinhalt (Frontmatter `datum:` oder Body)
  2. Datum aus Dateiname
  3. Fallback: Erstellungsdatum der Datei
- **Status: ERLEDIGT (2026-04-10)**
- Ergebnis: 92 von 102 Dateien umbenannt (35 aus Content, 10 aus Frontmatter, 44 aus Dateiname, 3 manuell korrigiert)
- **10 Dateien ohne ermittelbares Datum** → verschoben nach `00 Inbox/Converted/manuell_pruefen/` zur manuellen Sichtung

#### 2.3 Aufräumen
- **Status: ERLEDIGT (2026-04-10)**
- Quelldateien in `obsidian-vault/Converted/` und `obsidian-vault/Inbox/` gelöscht
- `obsidian-vault/`-Ordner komplett entfernt
- Syncthing-Folder `obsidian-vault` auf Ryzen + Mac entfernt
- `docling-watcher` Container gestoppt + aus `docker-compose.yml` entfernt (Dispatcher ersetzt ihn vollständig)

---

### Phase 3 — Kategorisierung (iterativ, dreistufig)

#### 3.1 Batch-Konvertierung: PDFs → Markdown
- 2.193 PDFs aus `pdf-archiv/` (ehemals `Anlagen/`) durch Docling laufen lassen
- Nur OCR-Konvertierung, keine Klassifizierung
- Ergebnis: rohe .md Dateien in `~/staging/converted/`
- **Status: ERLEDIGT (2026-04-11)**
- Ergebnis:
  - 2.283 MDs konvertiert, 78 fehlgeschlagen (Timeout/Größe)
  - Retry für 78 Fehler mit 30min Timeout geplant (`~/staging/batch_convert_retry.py`)
  - Skript: `~/staging/batch_convert.py` (1 Worker, 300s Timeout, max 10 MB)

#### 3.2 Stufe 1 — Nur Kategorien zuordnen
- Alle konvertierten MDs per Ollama klassifizieren — ausschließlich Kategorie
- Skript: `~/staging/batch_classify.py` (qwen2.5:7b, max 3.000 Zeichen/Dokument)
- Ergebnis: `~/staging/classification.csv` (filename, category, filesize)

| ID | Kategorie | Vault-Ordner |
|---|---|---|
| `persoenlich` | Persönliches | `10 Persönlich/` |
| `familie` | Familie | `20 Familie/` |
| `fengshui` | FengShui | `30 FengShui/` |
| `finanzen` | Finanzen | `40 Finanzen/` |
| `krankenversicherung` | Krankenversicherung | `49 Krankenversicherung/` |
| `immobilien_eigen` | Immobilien eigen | `50 Immobilien eigen/` |
| `immobilien_vermietet` | Immobilien vermietet | `51 Immobilien vermietet/` |
| `garten` | Garten | `55 Garten/` |
| `fahrzeuge` | Fahrzeuge | `60 Fahrzeuge/` |
| `italien` | Italien | `70 Italien/` |
| `business` | Business | `80 Business/` |
| `digitales` | Digitales | `82 Digitales/` |
| `wissen` | Wissen | `85 Wissen/` |
| `reisen` | Reisen | `90 Reisen/` |
| `bedienungsanleitung` | Bedienungsanleitungen | `95 Bedienungsanleitungen/` |
| `archiv` | Archiv | `99 Archiv/` |

- **Status: ERLEDIGT (2026-04-11)**
- Ergebnis:
  - 2.283 Dateien verarbeitet, davon 2.229 (98,7%) sauber klassifiziert
  - LLM-Tippfehler (korrigierbar): `krankeversicherung` (14), `reise` (2), `garden` (1), `wistes` (1)
  - Fehler (Timeout/Unsinn): 10 Einträge
  - Verteilung:

| Kategorie | Anzahl |
|---|---|
| finanzen | 1.191 |
| business | 371 |
| krankenversicherung | 186 |
| reisen | 122 |
| fahrzeuge | 83 |
| bedienungsanleitung | 57 |
| archiv | 45 |
| immobilien_vermietet | 43 |
| persoenlich | 42 |
| wissen | 33 |
| digitales | 27 |
| italien | 15 |
| immobilien_italien | 13 |
| fengshui | 8 |
| garten | 6 |
| familie | 5 |

#### 3.3 Stufe 2 — Typen aus kategorisierten Dokumenten ableiten
- Pro Kategorie: Dokumente analysiert, Typ-Gruppen identifiziert und freigegeben
- **Status: ERLEDIGT (2026-04-12)**
- Ergebnis:
  - `immobilien_italien` aufgelöst → `immobilien_eigen` (Grassauer Str. + Podere dei venti)
  - Vault-Ordner `50 Immobilien Italien` → `50 Immobilien eigen` umbenannt
  - Versicherungen nach Art aufgeteilt: KFZ→fahrzeuge, Gebäude→immobilien, Sach→finanzen
  - Familie: Personen-Typen (Berta Hutterer, Max Hutterer, Josef Janning)
  - Typen definiert für: finanzen (8), fahrzeuge (4), business (5), reisen (4), immobilien_eigen (4), immobilien_vermietet (5), persoenlich (4), italien (3), digitales (3), wissen (3), familie (4)
  - Kategorien ohne Typen: fengshui, garten, bedienungsanleitung, archiv
  - Alle Typen in `categories.yaml` eingetragen

#### 3.4 Stufe 2b — Typen zuordnen
- Zweiter Ollama-Durchlauf: Typ-Zuordnung pro Kategorie
- Skript: `~/staging/batch_typify.py` + `~/staging/batch_reclassify.py`
- Ergebnis: `~/staging/classification_typed.csv` (filename, category, type_id, filesize)
- **Status: ERLEDIGT (2026-04-12)**
- Ergebnis:
  - 2.335 Dokumente mit Kategorie + Typ klassifiziert
  - Reklassifizierung: 194 Dokumente aus business/krankenversicherung in korrekte Kategorien verschoben
  - `immobilien_italien` → `immobilien_eigen` umbenannt
  - Manuelle Korrekturen: Hochwasserschutz → immobilien_eigen, Reale Mutua → immobilien_eigen/gebaeudeversicherung
  - Verbleibende `allgemein`: 165 (davon 130 in Kategorien ohne Typ-Definition)

#### 3.5 Stufe 3 — Finalisierung: DB + Frontmatter + Einordnung
- Skript: `~/staging/batch_finalize.py`
- **Status: ERLEDIGT (2026-04-13)**
- Ergebnis:
  - 2.316 MDs mit Frontmatter in Vault-Ordner verschoben
  - 19 MDs bereits im Vault (Phase 2) → übersprungen
  - 0 Fehler
  - DB erweitert: Spalten vault_kategorie, vault_typ, vault_pfad
  - Frontmatter enthält: kategorie, typ, datum, Link zum Original-PDF
- Verteilung nach Kategorie:

| Kategorie | Anzahl |
|---|---|
| finanzen | 1.260 |
| business | 293 |
| krankenversicherung | 167 |
| reisen | 126 |
| fahrzeuge | 88 |
| bedienungsanleitung | 64 |
| archiv | 59 |
| immobilien_vermietet | 49 |
| persoenlich | 48 |
| wissen | 46 |
| digitales | 38 |
| immobilien_eigen | 29 |
| versicherung | 23 |
| italien | 18 |
| familie | 12 |
| fengshui | 9 |
| garten | 6 |

---

### Phase 4 — Dispatcher anpassen

#### 4.1 categories.yaml aktualisieren
- Alle 16 Kategorien mit `vault_folder`-Mapping eingetragen
- Typ-Details vorerst nur für krankenversicherung + versicherung (Rest kommt in Phase 3.3)
- **Status: ERLEDIGT (2026-04-12)**

#### 4.2 dispatcher.py erweitern
- Frontmatter beim Konvertieren einfügen
- Neues Routing: .md → Reinhards Vault, PDF → `pdf-archiv/`
- DB-Felder befüllen
- **Status: ERLEDIGT (2026-04-12)**
- Ergebnis:
  - Routing generalisiert: PDF → `pdf-archiv/`, MD → `reinhards-vault/{kategorie}/Converted/{typ}/{jahr}/`
  - `vault_folder` wird aus categories.yaml gelesen (z.B. `finanzen` → `40 Finanzen`)
  - Kategorie-Fallback → `00 Inbox` bei fehlgeschlagener Klassifizierung
  - Ollama-Prompt erweitert für alle 16 Kategorien (KV-Spezialregeln bleiben erhalten)
  - DB speichert alle Kategorien, Rechnungs-Logik nur für KV/Versicherung
  - Smoke-Test: Mietvertrag korrekt als `immobilien_vermietet` → `51 Immobilien vermietet/`
  - Frontmatter-Einfügung und Typ-Routing für alle 16 Kategorien implementiert

#### 4.3 Smoke-Test
- Neues PDF über Pi einwerfen, gesamte Pipeline prüfen
- **Status: ERLEDIGT (2026-04-11)**
- Ergebnis:
  - Test mit `20260315_Gothaer_Leistungsabrechnung_Test.pdf` (Krankenversicherung)
  - Pipeline end-to-end erfolgreich: Erkennung → Docling-Konvertierung (2s) → Ollama-Klassifizierung → Routing
  - PDF korrekt in `pdf-archiv/`, MD korrekt in `reinhards-vault/49 Krankenversicherung/Converted/leistungsabrechnung/2026/`
  - Duplikat-Check funktioniert (bereits vorhandene PDFs werden übersprungen)

#### 4.4 Aufräumen
- **Status: ERLEDIGT (2026-04-13)**
- `~/staging/converted/` geleert (0 Dateien verbleibend)
- 19 bereits im Vault vorhandene MDs aus staging entfernt (keine Duplikate)
- Docling-serve Port 5001 aus docker-compose.yml entfernt (war temporär für Batch-Konvertierung)
- Container neu gestartet

#### 4.5 Dateinamen-Konvention im Dispatcher
- **Status: ERLEDIGT (2026-04-13)**
- Dispatcher benennt Dateien beim Verarbeiten automatisch um: `YYYYMMDD_Absender_Dokumenttyp.pdf/.md`
- Datum aus Ollama-Klassifizierung (Fallback: Dateiname, dann aktuelles Datum)
- Absender aus Ollama-Klassifizierung, max 30 Zeichen
- Kollisionsvermeidung durch Suffix `_2`, `_3`, ...
- Test erfolgreich: `test_gothaer_vertrag.pdf` → `20260206_Gothaer_Krankenversicherung_AG_Versicherungsschein_Tarifänderung.pdf`

#### 4.6 Syncthing-Architektur bereinigen
- **Status: ERLEDIGT (2026-04-13)**
- `input-dispatcher`: Mac direkt als Device zum Ryzen hinzugefügt (vorher nur über Pi)
- Folder-Typ auf beiden Seiten (Mac + Ryzen) auf `sendreceive` umgestellt
- Syncthing Revert aus Dispatcher entfernt — verarbeitete PDFs werden auf beiden Seiten gelöscht
- Alte Ordnerreste `50 Immobilien Italien` auf dem Mac bereinigt (ignorierte Dateien blockierten Sync)
- Syncthing-Variablen (SYNCTHING_URL, SYNCTHING_API_KEY, SYNCTHING_FOLDER) aus docker-compose.yml und dispatcher.py entfernt

---

## Offene Punkte

| Punkt | Status |
|---|---|
| 10 Dateien in `00 Inbox/manuell_pruefen/` | Offen — kein Datum bestimmbar, manuell einordnen |
| 10 Dateien in `00 Inbox/nicht_konvertierbar/` | Offen — Docling-Fehler, Platzhalter-MDs erstellt |

---

## Wichtige Pfade (aktuell)

| Pfad | Beschreibung |
|---|---|
| `~/docker/docling-workflow/syncthing/data/reinhards-vault/` | Reinhards Vault (Syncthing, sendreceive) |
| `~/pdf-archiv/` | Zentrales PDF-Archiv (3.045 Dateien, 3.4 GB) |
| `~/docker/docling-workflow/dispatcher-config/categories.yaml` | Kategorie-Config |
| `~/docker/docling-workflow/dispatcher/dispatcher.py` | Dispatcher-Script |
| `~/docker/docling-workflow/dispatcher-temp/dispatcher.db` | SQLite-Datenbank |
