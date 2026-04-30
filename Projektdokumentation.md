# Persönliches KI-gestütztes Dokumentenmanagement — Projektdokumentation

**Stand: 2026-04-30 — Drei-Bot-Ökosystem (Wilson/Hotelbär/Lärmbär), OpenClaw-native AI-Assistent, Lärmbär (Home Assistant), Feng-Shui-Briefing getrennt, enzyme Auto-Refresh korrigiert**

> Dieses Dokument ist das konsolidierte Referenzdokument des Projekts. Es ersetzt:
> - `projekt_beschreibung_expertenberatung.md` — Strategiedokument (war führend)
> - `PROJEKTDOKUMENTATION.md` — Technische Betriebsreferenz
> - `PROJEKT.md` — **veraltet** (Stand 2026-03-27, beschreibt alte `watcher.py`-Architektur)

---

## Executive Summary

Das Projekt wechselt von einer **"vollständigen Batch-Klassifikation aller Bestandsdokumente"** zu einer **"flachen Archiv-Architektur mit On-Demand-Verarbeitung"**. Der geplante Rescan von 3.246 PDFs wird verworfen (108–270 Std. Rechenzeit bei unklarem Nutzen). Stattdessen:

- **Neue Dokumente** durchlaufen weiterhin die vollautomatische Dispatcher-Pipeline
- **Bestandsdokumente** bleiben unverändert und werden von Text Extractor passiv OCR-indexiert
- **Auswertungen** erfolgen auf Anforderung: Suche → Treffer → Dispatcher-Batch-Modus → strukturierte Ausgabe
- **Mobile Abfragen** über Telegram-Bot auf dem Pi, der HTTP-Requests an einen Cache-Reader-Service auf dem Ryzen sendet

Der **Umsetzungsplan** beschreibt Phasen 0–5 über ca. 22 Entwicklungstage. Phase 0 (Strategie), Phase 1 (Cache-Reader-Infrastruktur), Phase 2 (Batch-Modus), Phase W (Wilson-Pipeline) und Phase 2.5 (Duplikat-Bereinigung) sind abgeschlossen. **2026-04-27: 1.188 Duplikate bereinigt — Anlagen/ von 3.073 auf 1.885 PDFs reduziert.** Keine parallelen Phasen, kein "Big Bang"-Release.

---

## 1. Projektüberblick

### Worum es geht

Reinhard betreibt ein vollständig **lokales**, KI-gestütztes Dokumentenmanagementsystem für persönliche und geschäftliche Unterlagen — Immobilien in Deutschland und Italien, Krankenversicherung, Fahrzeuge, Steuern, Geschäftsdokumentation, Reisen und mehr. Das System läuft **ausschließlich auf eigener Hardware**, ohne Cloud-Dienste. Alle KI-Modelle laufen lokal via Ollama.

Dokumente entstehen auf drei Wegen:
- **Papierscans** vom Raspberry Pi (Briefpost, Rechnungen)
- **PDF-Importe** von Email, Behördenportalen, Online-Banking
- **Bereits vorhandener Bestand** aus jahrelanger digitaler Ablage (Evernote-Migration, Apple Notes, frühere Scans)

### Infrastruktur

| Komponente | Hardware | Rolle |
|---|---|---|
| Raspberry Pi ("Wilson") | ARM | Scanner-Einheit, Datei-Eingang, OpenClaw-Gateway, Vault-Suche via Telegram |
| Ryzen-Workstation (192.168.86.195) | AMD Ryzen, Linux | Hauptserver: OCR, KI, Dispatcher, Vault |
| Mac mini (192.168.86.134) | Apple Silicon | Obsidian-Client, tägliche Nutzung |
| Syncthing | — | Bidirektionale Replikation Pi ↔ Ryzen ↔ Mac |
| Obsidian | — | Vault-Frontend, Wissensdatenbank |

### Container-Übersicht (Ryzen)

```
Pi/Scanner
  │  PDF via Syncthing
  ▼
input-dispatcher/
  │
  ▼
[document-dispatcher]                        [cache-reader]
  ├─ Docling-Serve  →  Markdown (OCR)        ├─ FastAPI (Port 8501)
  ├─ langdetect     →  Spracherkennung       ├─ SQLite FTS5
  ├─ Ollama/qwen    →  Übersetzung           └─ langdetect über Bestand
  ├─ Ollama/qwen    →  Klassifikation               │
  ├─ Hybrid-OCR-Gate (Cache→Docling)  ◄─────────────┘
  └─ vault_pfad     →  Obsidian-Vault / Anlagen/
         │
         ├─ Telegram-Benachrichtigung
         ├─ SQLite-DB  (dispatcher.db, inkl. batch_runs/batch_items)
         └─ Dashboard 8765  ( / · /pipeline · /cache · /batch · /review · /duplikate )
```

**Container:** `syncthing`, `docling-serve`, `document-dispatcher`, `cache-reader`
**Netzwerke:** `docling-net` (intern), `ollama-net` (extern, geteilt mit Open WebUI)
**Vault:** `/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault`
**Ports:** 8765 (Dispatcher-Dashboard + REST), 8501 (cache-reader), 11180 (enzyme mcpo-Bridge → Open WebUI + Wilson)

**Wilson-Dienste (systemd user services auf Pi 5, 192.168.3.124):**

| Service | Datei | Bot-Token | Funktion |
|---|---|---|---|
| `openclaw-gateway` | (OpenClaw intern) | `8621101278` (Wilson-Bot) | AI-Assistent, Vault-Suche, Cron-Jobs, Telegram-Polling **aktiv** |
| `doc-processor` | `wilson/doc_processor.py` | `8382100394` (Hotelbär) | Dokument-Eingang, OCR via Ryzen, Sidecar-JSON, Telegram-Benachrichtigung |
| `laerenbaer` | `wilson/laerenbaer.py` | `8539477131` (Lärmbär) | Home-Assistant-Bot: Sensoren, Kameras, Gerätesteuerung |
| `heartbeat` | `wilson/heartbeat.py` | — | Service-Monitor: pollt Dispatcher/Cache-Reader/Docling/Ollama alle 90s, Telegram-Alert bei 2 Fehlern |

Alle Secrets (`TELEGRAM_BOT_TOKEN`, `LAERENBAER_BOT_TOKEN`, `DEEPSEEK_API_KEY`) werden aus `~/.openclaw/secrets.env` geladen (nicht im Git-Repo). Vorlage: `wilson/secrets.env.example`.

**Drei-Bot-Ökosystem (Stand 2026-04-30):**

| Bot | Name | Token-Prefix | Technologie | Funktion |
|---|---|---|---|---|
| **Wilson** | Wilson AI-Assistent | `8621101278` | OpenClaw Gateway | Persönlicher Assistent, Cron-Jobs, Vault-Suche, Feng-Shui, Portfolio |
| **Hotelbär** | Dispatcher-Bot | `8382100394` | `doc_processor.py` | Dokumenten-Eingang, OCR, Klassifikation, Vault-Ablage |
| **Lärmbär** | HA-Bot | `8539477131` | `laerenbaer.py` | Home Assistant: Sensoren, Kameras, Schalten |

**Warum drei getrennte Tokens:** Telegram erlaubt nur einen aktiven `getUpdates`-Poller pro Bot-Token (409 Conflict). Jeder Dienst pollt seinen eigenen Token — keine Konflikte.

**Wilson OpenClaw Skills** (`~/.openclaw/skills/` auf Wilson):

| Skill | Fähigkeit |
|---|---|
| `enzyme/SKILL.md` | Semantische Vault-Suche via enzyme (Port 11180) |
| `dispatcher/SKILL.md` | Dispatcher-API: Dokumente suchen, lesen, korrigieren, **PDF senden** |
| `homeassistant/SKILL.md` | HA-Sensoren, Kamera-Snapshots |
| `file-manager/SKILL.md` | Dateisystem-Operationen auf `~/Vaults` (Projekte Vault) |

**Wilson OpenClaw Cron-Jobs:**

| Job | Zeit | Funktion |
|---|---|---|
| TODO Neu-Einsortierung | 06:00 täglich | Aufgaben in Aufgaben.md in richtige Zeitabschnitte sortieren |
| Portfolio Kursabruf | 07:30 täglich | `portfolio_update.py` → Kurse abrufen, DB speichern, Telegram-Zusammenfassung |
| Tages-Briefing | 07:30 täglich | Wetter, Gestern-Memory, HA-Sensoren, offene Aufgaben → 1 Telegram-Nachricht |
| Feng Shui Briefing | 07:45 täglich | gua_calculator → Tages-/Monats-/Jahres-/Periodenenergie + Fokus-Satz → Telegram |
| Babbel Erinnerung | 08:00 täglich | Italienisch-Lektion Erinnerung |
| Tägliche Garten-Checkliste | 09:00 täglich | Gartenaufgaben + Wetter → Telegram |
| Garten-Pflegeplan wöchentlich | 07:00 montags | Pflegeplan aus Vault ausgeben |
| Abend-Check | 22:00 täglich | Fällige Aufgaben abfragen, erledigte abhaken, Aufgaben.md aktualisieren |
| Tägliches Tagebuch | 22:30 täglich | Memory-Datei + Chat-History → Tagebucheintrag in Vault |
| Täglicher Session-Reset | 23:59 täglich | `/reset` — Gesprächssession zurücksetzen |
| Wöchentliche Fälligkeits-Review | 11:00 sonntags | Überfällige + termlose Aufgaben reviewen |
| Wochenaufgaben | 17:00 sonntags | Wochenvorschau mit Aufgaben-Übersicht |
| Rudern Erinnerung | alle 48h | Sport-Reminder |
| Übergabeprotokoll Lipowskystr. | 04.05.2026 17:00 | Einmalige Erinnerung (deleteAfterRun) |

**Wilson Identitäts-Kontext (BOOT.md):**
Wilson kennt Reinhards Garten (Südtirol + Karlsruhe), Immobilien, Projekte, Gewohnheiten. Liest beim Start `~/Vaults/BOOT.md` (max. 20.000 Zeichen) als Systemkontext. Default-Modell: `deepseek/deepseek-v4-flash`.

### Obsidian-Vault

Der Vault ist das zentrale Dokumentenrepositorium:

| Typ | Anzahl | Anmerkung |
|---|---|---|
| Markdown-Notizen | **1.923** | Teils auto-generiert, teils manuell |
| PDFs | **1.885** | Im `Anlagen/`-Ordner (nach Duplikat-Bereinigung 2026-04-27, war 3.073) |
| Gesamt Dateien | **~3.808** | Exkl. Plugin-Daten |

**Vault-Ordnerstruktur (16 Kategorien):**

| Ordner | MD-Notizen | Beschreibung |
|---|---|---|
| 10 Persönlich | 167 | Ausweise, Urkunden, Vollmachten |
| 20 Familie | 145 | Berta Hutterer, Josef Janning, Haustiere |
| 30 FengShui | 94 | Beratung, Einrichtung, Audits |
| 40 Finanzen | 383 | Versicherungen, Banken, Steuern |
| 49 Krankenversicherung | 179 | HUK (Marion/Reinhard), Gothaer, vigo |
| 50 Immobilien eigen | 146 | Übersee (bis 2022), Seggiano (ab 2022) |
| 51 Immobilien vermietet | 4 | München, Bremen, Karlsruhe, Schechen, Neuburg |
| 55 Garten | 25 | Landschaftspflege, Bewässerung |
| 60 Fahrzeuge | 47 | KFZ, Werkstatt, TÜV |
| 70 Italien | 23 | Behörden, Comune, nicht-Immobilien |
| 80 Business | 292 | Firma, Beratung, Buchhaltung |
| 82 Digitales | 8 | Smart Home, IT, Netzwerk |
| 85 Wissen | 13 | Bücher, Kurse, Fortbildung |
| 90 Reisen | 70 | Flüge, Hotels, Buchungen |
| 95 Bedienungsanleitungen | 1 | Gerätehandbücher |
| 99 Archiv | 252 | Nicht zuordenbar, veraltet |
| 00 Inbox | 55 | Noch nicht eingeordnet |

**Wichtige Besonderheit:** 1.885 PDFs liegen flach in `Anlagen/` als verlinkte Anhänge von Markdown-Notizen (nach Duplikat-Bereinigung 2026-04-27). Die primäre Kategorisierungsstruktur ist in den **Markdown-Notizen** — nicht in der PDF-Ablage selbst.

```
reinhards-vault/
├── Anlagen/                   ← alle PDFs (Obsidian attachmentFolderPath)
├── 00 Inbox/                  ← unklassifiziert / OCR-Fehler
├── 10 Persönlich/
├── 20 Familie/
│   └── Haustiere/
├── 30 FengShui/
├── 40 Finanzen/
├── 49 Krankenversicherung/
│   ├── Leistungsabrechnung Marion/[Jahr]/
│   ├── Leistungsabrechnung Reinhard/[Jahr]/
│   ├── Arztrechnung/[Jahr]/
│   ├── Beitragsinformation/[Jahr]/
│   ├── Rezept/[Jahr]/
│   ├── Sonstiges/[Jahr]/
│   ├── 00 Wiederherstellung/  ← 621 OCR-Stubs zum Nachbearbeiten
│   └── undatiert/
├── 50 Immobilien eigen/
├── 51 Immobilien vermietet/
...
└── 99 Archiv/
```

---

## 2. Der Dispatcher: Vollautomatische Pipeline für neue Dokumente

### Verarbeitungsablauf

```
① SCAN AUF PI
   Dokument wird gescannt → landet in ~/input-dispatcher/ auf dem Pi

② SYNCTHING-ÜBERTRAGUNG
   Syncthing repliziert die Datei innerhalb von ~10 Sekunden auf den Ryzen

③ DATEI-STABILITÄT
   wait_for_file_stable(): 3× prüfen ob Größe konstant bleibt

④ DUPLIKAT-CHECK
   MD5-Hash gegen DB (pdf_hash-Spalte) + Dateiname-Check → bei Treffer: Telegram ♻️

⑤ DOCLING: OCR + STRUKTURIERUNG
   - PDF wird durch Docling (OCR-Engine mit ML-Layout-Analyse) verarbeitet
   - Ausgabe: strukturierter Markdown mit Tabellen, Überschriften, Spalten
   - Qualitäts-Gate: Texte mit < 300 Zeichen → Inbox (kein LLM-Aufwand)

⑥ HEADER-EXTRAKTION (regelbasiert)
   - Absender-Firma, Adresse, Datum aus dem Dokumentkopf (Regex: PLZ, Firmenformen, Namen)
   - Identifier: IBAN, Steuernummer, Part.IVA, Rechnungsnummer

⑦ ABSENDER-AUFLÖSUNG (YAML-Datenbank)
   - Abgleich mit absender.yaml (Part.IVA, USt-IdNr, Alias-Match)
   - Liefert deterministisch: Kategorie-Hint, Adressat (Reinhard/Marion/Linoa)
   - Beispiel: Part.IVA "02145060501" → Clinica Veterinaria Amiatina → familie/tierarztrechnung/Reinhard

⑧ SPRACHERKENNUNG + ÜBERSETZUNG
   - Sprache des Dokuments wird erkannt (DE/IT/EN), Schwellwert 0.85
   - Italienische Dokumente werden via Ollama-Translate ins Deutsche übersetzt
   - Übersetzung wird dem LLM als Kontext mitgegeben (Original-OCR bleibt erhalten)

⑨ LLM-KLASSIFIKATION (Ollama, OLLAMA_MODEL)
   - Input: OCR-Text + Absender-Hint + Sprachinformation
   - Output: kategorie_id, typ_id, absender, adressat, rechnungsdatum, konfidenz
   - Halluzinations-Guard: Nur in categories.yaml bekannte IDs werden akzeptiert

⑩ LERNREGELN-OVERRIDE (deterministisch, nach LLM)
   - Keyword-basierte Regeln überschreiben LLM-Ergebnis bei gesicherter Zuordnung
   - Absender-basierte Regeln: "Apartmenthotel am Leuchtturm" → reisen/hotel_rechnung

⑪ DATEINAME
   - build_clean_filename(): YYYYMMDD_Absender_Thema

⑫ VAULT-PFAD
   - build_vault_path(): aus TYPE_ROUTING-Dict (aus categories.yaml)
   - Aktuelles Jahr → direkt im Typ-Ordner; Vorjahre → /{year}/; Fallback: 00 Inbox

⑬ MD-SCHREIBEN
   - Frontmatter + Markdown-Body mit verlinktem PDF in Vault

⑭ PDF → Anlagen/
   - Kopie in VAULT_PDF_ARCHIV (= Anlagen/)

⑮ DATENBANKSCHREIBUNG
   - Eintrag in SQLite: dateiname, kategorie, typ, absender, adressat, datum, konfidenz, pdf_hash
   - Auch bei fehlgeschlagener Klassifikation: Minimal-Eintrag (kategorie=NULL, konfidenz=niedrig)

⑯ TELEGRAM-BENACHRICHTIGUNG
   - Strukturierte Nachricht mit allen extrahierten Metadaten + Per-Feld-Konfidenz-Icons
   - Inline-Buttons: ✅ Korrekt | ✏️ Korrigieren | 🔄 Neu klassifizieren
   - Korrekturen werden als Lernregel gespeichert
```

### Konfiguration ohne Code-Änderung

Das gesamte Routing ist über YAML-Dateien steuerbar:

| Datei | Zweck |
|---|---|
| `dispatcher-config/categories.yaml` | Taxonomie (Kategorien, Typen, Routing, Hints) — Single Source of Truth |
| `dispatcher-config/absender.yaml` | Absender-DB mit `adressat_default` pro Firma |
| `dispatcher-config/personen.yaml` | Personendaten (Cod. Fiscale, IBAN) für Adressat-Auflösung |
| `dispatcher-config/doc_types.yaml` | Keyword-Tabelle für strukturierte Dokumenttyp-Erkennung |

**Aufbau eines Typs in categories.yaml:**

```yaml
categories:
  krankenversicherung:
    label: "Krankenversicherung"
    vault_folder: "49 Krankenversicherung"
    types:
      - id: leistungsabrechnung
        label: "Leistungsabrechnung"
        vault_subfolder: "Leistungsabrechnung"
        person_subfolder: true
        adressat_fallback: "Sonstiges"
        telegram_template: leistungsabrechnung
        hints: [...]
```

Neue Kategorien oder Absender werden ausschließlich durch YAML-Änderung hinzugefügt — kein Python-Code nötig.

### Absender-Wissensbasis (wichtige Einträge)

| Absender | `adressat_default` | Hinweis |
|---|---|---|
| HUK-COBURG | Marion | Private KV |
| Gothaer | Reinhard | Private KV |
| vigo | Marion | Pflegezusatzversicherung |
| Barmenia | Reinhard | |

Identifikation erfolgt deterministisch via `resolve_absender()`: Cod. Fiscale / IBAN → `personen.yaml`, dann Keyword-Match in Header.

### Bisherige Verarbeitungsleistung

- **123 Dokumente** vollständig verarbeitet
- **11 Lernregeln** gespeichert (aus Telegram-Korrekturen)
- **47 bekannte Absender** in der Datenbank (inkl. Alias-Varianten)
- Aktiver Entwicklungszweig: `feature/classification-v2`

---

## 3. Warum kein Batch-Rescan des Bestands

### Das Mengen-Problem

Ein vollständiger Rescan aller 1.885 PDFs (nach Duplikat-Bereinigung) durch Docling + Ollama würde bei realistischen 2–5 Minuten pro Dokument **63–157 Stunden** Rechenzeit bedeuten — auf dem Ryzen, ohne parallele Nutzung für andere Aufgaben.

### Das Qualitäts- und Kosten-Nutzen-Problem

- **Bedienungsanleitungen, Broschüren, Kataloge:** Keine sinnvolle Klassifikation möglich
- **Handgeschriebene Notizen, schlechte Scans:** OCR-Qualität zu niedrig
- **Projektdokumentation (80 Business):** Sehr individuell, Keyword-Regeln greifen nicht
- **621 Dokumente** in `00 Wiederherstellung/` haben bei einem früheren Bereinigungslauf bereits versagt
- Der Großteil des Bestands (>60%) sind reine Ablage-Dokumente, die **nie aktiv ausgewertet werden**

### Die Entscheidung

Der Bestand bleibt **unverändert**. Neue Dokumente durchlaufen die vollständige Pipeline. Bestandsdokumente werden **on demand** verarbeitet — wenn und nur wenn eine konkrete Auswertungsanforderung entsteht.

---

## 4. Das Architekturmodell: Flaches Archiv + On-Demand-Verarbeitung

### Prinzip

```
┌─────────────────────────────────────────────────────────┐
│                    OBSIDIAN VAULT                        │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  Markdown   │    │    PDFs     │    │ Text Extract │  │
│  │  Notizen    │    │  (Anlagen/) │    │    Cache     │  │
│  │  1.923      │    │  1.885      │    │   846 PDFs   │  │
│  └─────────────┘    └─────────────┘    └─────────────┘  │
└─────────────────────────────────────────────────────────┘
              ↓ cache-reader (Port 8501)
┌─────────────────────────────────────────────────────────┐
│               DISPATCHER (On-Demand-Batch)               │
│  1. OCR: Text-Extractor-Cache → Docling (Fallback)       │
│  2. Klassifikation: Ollama                               │
│  3. Ausgabe: Metadaten, Summen, Export (CSV/JSONL)       │
└─────────────────────────────────────────────────────────┘
```

### Drei Betriebsmodi des Dispatchers

**Modus 1: Inbox (produktiv, unverändert)**
- Dateisystem-Watcher auf `~/input-dispatcher/`
- Neue Scans werden automatisch verarbeitet
- OCR via Docling, Klassifikation via Ollama
- Ergebnis: DB-Eintrag + Vault-Move + Telegram

**Modus 2: Batch (neu, abgeschlossen Phase 2)**
- Dispatcher erhält eine Liste von Dateipfaden (via CLI oder API)
- Verarbeitung wie Inbox-Modus, aber für bestehende Vault-Dokumente
- OCR-Quelle: zuerst Text-Extractor-Cache prüfen → wenn Qualität < 500 Zeichen → Docling
- Ausgabe: CSV/JSONL statt Vault-Move (schützt das Archiv)
- Trigger: manuell, via Telegram-Befehl, oder via cache-reader-Export

**Modus 3: Query (Zukunft, Phase 3)**
- cache-reader-API liefert Trefferliste zu einem Suchbegriff
- Dispatcher verarbeitet alle Treffer automatisch
- Ausgabe: strukturierte Auswertung (Tabelle, Summen, CSV)

### Text Extractor als OCR-Vorrat

| Kennzahl | Wert |
|---|---|
| Cache-Speicherort | `<vault>/.obsidian/plugins/text-extractor/cache/` |
| Gesamt Cache-Einträge (nach langdetect-Reindexierung) | 2.460 JSON-Dateien |
| Davon PDFs | 846 (26% der 3.246 PDFs) |
| Verwertbarer Text (>50 Zeichen) | 695 PDFs (82%) |
| Erkannte Sprachen (neu, nach langdetect) | DE: 703, IT: 108, EN: 481, Unknown: 1.128 |
| Bekanntes Problem | Text Extractor-Spracherkennung war unzuverlässig → cache-reader nutzt eigene langdetect |

Der Cache wird **nicht** synchronisiert — `.stignore` schließt den gesamten `.obsidian/`-Ordner aus. Der Ryzen ist die einzige produktive OCR-Quelle.

---

## 5. Welche Bestandsdokumente sind sofort klassifizierbar?

Aus dem bereits vorhandenen Text-Extractor-Cache könnten **ca. 200–250 Dokumente** sofort mit hoher Konfidenz klassifiziert werden — ohne erneuten OCR-Lauf durch Docling:

| Dokumenttyp | Erkannte Dokumente | Basis |
|---|---|---|
| Handwerker / Immobilien-Rechnung | ~60 | Keywords: Handwerker, Acquedotto, Fognaria |
| Arztrechnung / medizinische Leistung | ~57 | Keywords: GOÄ, Liquidation, Dr. med. |
| Steuerbescheid | ~47 | Keywords: Steuerbescheid, Finanzamt, Agenzia delle Entrate |
| Kaufvertrag / Grundbuch | ~42 | Keywords: Rogito, Visura Catastale, Notar |
| Mietvertrag | ~27 | Keywords: Mietvertrag, Kaltmiete |
| Hotel-Buchung | ~22 | Keywords: Booking.com, Agriturismo |
| KV-Leistungsabrechnung | ~22 | Keywords: HUK-COBURG, Leistungsabrechnung |
| Darlehensvertrag | ~11 | Keywords: Annuitätendarlehen, Baufinanzierung |

**Was nicht automatisch klassifiziert werden kann:**
- Bedienungsanleitungen, Broschüren
- Business-Korrespondenz (80 Business, 292 Notizen)
- Dokumente in 99 Archiv (252 Notizen)
- 621 OCR-Stubs in `00 Wiederherstellung/`

---

## 6. Drei-Bot-Ökosystem und Vaults

### Vault-Übersicht

Das System nutzt zwei getrennte Obsidian-Vaults mit unterschiedlichen Zwecken:

| Vault | Ort | Inhalt | Zugang | Sync |
|---|---|---|---|---|
| **Reinhards Vault** | Ryzen `/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault` | Dokumentenarchiv — 1.885 PDFs, 1.923 MDs, alle Kategorien | enzyme (Port 11180), Dispatcher-Dashboard (Port 8765) | bidirektional Ryzen ↔ Wilson ↔ Mac |
| **Projekte Vault** | Wilson `~/Vaults` | Projektmanagement — AUFGABEN, Themen, Memory, Business, Garten, Feng Shui | OpenClaw file-manager Skill, Cron-Jobs direkt | bidirektional Wilson ↔ Mac |

**Syncthing-Ordner (Stand 2026-04-30):**

| Folder-ID | Label | Typ (Ryzen) | Mit Mac |
|---|---|---|---|
| `reinhards-vault` | Reinhards Vault | `sendreceive` | ja |
| `input-dispatcher` | Scanner-Eingang | `sendreceive` | ja |
| `projekte-vault` | Projekte Vault | — (auf Wilson) | ja (eingeladen, Annahme ausstehend) |

### Wilson AI-Assistent (openclaw-gateway, Token 8621101278)

Wilson ist Reinhards persönlicher KI-Assistent — jetzt vollständig OpenClaw-nativ. Der frühere `ai_assistant.py`-Bot wurde durch den OpenClaw Gateway ersetzt. Wilson kennt Reinhards Garten, Projekte, Immobilien und Gewohnheiten aus `~/Vaults/BOOT.md`.

**Fähigkeiten über Skills:**

| Skill | Was Wilson kann |
|---|---|
| `enzyme` | Semantische Suche in Reinhards Vault (Ryzen, Port 11180) |
| `dispatcher` | Dokumente suchen, lesen, korrigieren, als PDF senden |
| `homeassistant` | HA-Sensoren abfragen, Kameras, Geräte schalten |
| `file-manager` | Projekte Vault lesen/schreiben (`~/Vaults`) |

**Tägliche Cron-Jobs:** Siehe Tabelle in Abschnitt 1.

**Technisch:**
- OpenClaw Gateway auf Port 18789 (loopback), Telegram-Polling aktiv
- Default-Modell: `deepseek/deepseek-v4-flash`, Fallback: `deepseek/deepseek-chat`
- API-Keys in `~/.openclaw/agents/main/agent/auth-profiles.json` (separater Key-Store, unabhängig von `secrets.env`)
- Session-Key für Telegram: `agent:main:telegram:direct:8620231031`

### Hotelbär / Dispatcher-Bot (doc-processor, Token 8382100394)

Fokussiert ausschließlich auf Dokumentenverarbeitung.

| Befehl | Funktion |
|---|---|
| `/status` | Ausstehende Dokumente mit Status-Icons (⏳✏️🗂️) |
| `/hilfe` | Kurzanleitung |

PDF senden → automatische OCR + Kategorisierung + Vault-Ablage.

### Lärmbär / HA-Bot (laerenbaer, Token 8539477131)

Neuer eigenständiger Bot für Home Assistant. Kein LLM — direkte HA REST API-Abfragen.

| Befehl | Funktion |
|---|---|
| `/sensoren` | Übersicht Haupt-Sensoren (Temp, Feuchte, Wind, Regen, Energie, Wasser) |
| `/sensor <name>` | Einzelnen Sensor abfragen |
| `/kamera <name>` | Snapshot einer EUFY-Kamera via `ha-camera.sh` |
| `/kameras` | Liste aller verfügbaren Kameras |
| `/schalten <entity> <an\|aus>` | Gerät ein-/ausschalten |
| `/hilfe` | Befehlsübersicht |

**Technisch:**
- `wilson/laerenbaer.py` — eigenständiges Polling, kein LLM
- HA-Token aus `~/.config/homeassistant/token`
- Token aus HA `core.config_entries` (Platform: `broadcast` — kein Polling-Konflikt)
- HA-URL: `http://192.168.86.183:8123`

### OmniSearch / cache-reader als Basis

Der cache-reader stellt auf Port 8501 eine HTTP-API bereit (FTS5-Volltextindex):

```
GET http://cache-reader:8501/search?q=<suchbegriff>&limit=<n>
→ [{"path": "...", "score": 0.92, "excerpt": "...", "langs": "de"}, ...]
```

### Sinnvolle Dispatcher-Bot-Befehle (geplant Phase 3)

| Befehl | Funktion |
|---|---|
| `/kategorie <Name>` | Alle Dokumente einer Kategorie |
| `/auswertung <Begriff> <Jahr>` | Suchen + Beträge summieren |
| `/inbox` | Aktueller Inhalt der Inbox |
| `/verarbeite <ids>` | Ausgewählte Dokumente on demand klassifizieren |
| `/reocr <id>` | Re-OCR-Fallback für Stub |

---

## 7. Ollama-Modelle

| Modell | Größe | Aufgabe |
|---|---|---|
| **mistral-nemo:12B** / **gemma4:e4b** | 7,1 GB | Primäres Klassifikationsmodell (Kategorie, Typ, Absender, Adressat, Datum) |
| **translategemma:latest** | 3,3 GB | Übersetzung IT→DE vor Klassifikation |
| **qwen2.5:7b** | 4,7 GB | Allgemeines LLM, Wissensabfragen |
| **llama3.1:8B** | 4,9 GB | Backup-Klassifikator / Fallback |
| **mxbai-embed-large** | 669 MB | Embedding-Modell (Qdrant, enzyme) |
| **nomic-embed-text** | 274 MB | Embedding-Modell (leichtgewichtig) |

**Für das On-Demand-Architekturmodell mindestens benötigt:**
- Primäres Klassifikationsmodell — unverzichtbar
- translategemma — unverzichtbar für italienische Dokumente

Ein dediziertes Summarization-Modell ist **nicht** erforderlich — das Klassifikationsmodell übernimmt auch Batch-Auswertungen.

**Stabilitäts-Konfiguration (Env-Vars):**
- `OLLAMA_NUM_CTX=8192` (Default, verhindert GPU-Hänger auf 2-GB-iGPU)
- `OLLAMA_TIMEOUT=300`

---

## 8. Architektur-Entscheidungen

### Entscheidung 1: OCR-Quelle im On-Demand-Modus — Hybrid mit Qualitäts-Gate

```
IF cache_entry_exists AND len(text) >= 500 AND detected_language in {de, it, en}
    USE cache_entry   (meta.source = "cache")
ELSE
    RUN docling_ocr   (meta.source = "docling_fallback")
```

- Cache-Lookups: Millisekunden vs. Docling 2–5 Minuten pro Dokument
- OCR-Qualitäts-Schwelle auf 500 Zeichen angehoben (Cache-Text oft schlechter strukturiert)
- Override-Option: `--force-docling` für Qualitätsstichproben
- Smoke-Test 2026-04-19: Cache-Hit 2.942 Zeichen / 0 ms vs. 36 s Docling auf identischer PDF

### Entscheidung 2: Programmatische Suche — Eigener cache-reader statt OmniSearch-Daemon

OmniSearch läuft nur in aktiver Obsidian-Instanz — für einen Server-Dienst auf dem Ryzen fragil. SQLite FTS5 ist produktionsreif, schnell (<100 ms auf 10.000 Dokumenten).

OmniSearch bleibt **parallel verfügbar** für interaktive Nutzung in Obsidian.

### Entscheidung 3: OCR-Stubs in `00 Wiederherstellung/` — Lazy Re-OCR on demand

621 × 2–5 Min = 20–50 Std. Rechenzeit ohne garantierten Erfolg. Lazy-Ansatz: Wenn eine Suchanfrage einen Stub als Treffer liefert, bietet der Telegram-Bot einen Button "🔄 Re-OCR versuchen" an.

### Entscheidung 4: Syncthing-Isolation — `.obsidian/`-Ausschluss beibehalten

Die bestehende `.stignore` schließt `.obsidian/` vollständig aus. Diese Entscheidung wird nicht revidiert — Cache-Reader-Service liest direkt vom Ryzen-Cache, keine Merge-Logik nötig.

```
.DS_Store / **/.DS_Store / *.pdf.md / **/*.pdf.md / .obsidian/ / .enzyme/ / .enzyme-embeddings/
```

### Entscheidung 5: Ollama-Modelle — Aktuelle Auswahl bleibt

Alle benötigten Modelle bereits installiert. Ein dediziertes Summarization-Modell ist nicht erforderlich.

---

## 9. Datenbank (SQLite)

Datei: `dispatcher-temp/dispatcher.db`

### Tabellen

**`dokumente`** — ein Eintrag pro verarbeitetem PDF
```
id, dateiname, pdf_hash, rechnungsdatum, kategorie, typ,
absender, adressat, konfidenz, vault_pfad, erstellt_am
```

**`rechnungen`** — offene Arzt-/Sonstige-Rechnungen
```
id, dokument_id, rechnungsbetrag, faelligkeitsdatum,
status (offen/erstattet/teilweise_erstattet), erstattungsdatum
```

**`erstattungspositionen`** — Leistungsabrechnungs-Positionen
```
id, dokument_id, rechnung_id, leistungserbringer, zeitraum,
rechnungsbetrag, erstattungsbetrag, erstattungsprozent
```

**`aussteller` / `aussteller_aliases`** — Absender-Stammdaten (47 Aussteller, 181 Aliase)

**`klassifikations_historie`** — jede LLM-Klassifikation + manuelle Korrekturen
```
llm_model, translate_model, lang_detected, lang_prob, duration_ms,
raw_response, final_category, final_type, konfidenz_*, korrektur_von_user
```

**`batch_runs` / `batch_items`** — Batch-Lauf-Verwaltung (Phase 2)

**`duplikat_scans`** — Metadaten je Scan-Lauf (status, total_pdfs, byte_gruppen, sem_gruppen)

**`duplikat_gruppen`** — Duplikat-Gruppen (scan_id, typ: `byte`/`semantic`, datum, absender, status: `offen`/`verarbeitet`)

**`duplikat_eintraege`** — PDFs in Gruppen (gruppe_id, pdf_pfad, md_pfad, ist_original, verschoben)

**`lernregeln`** — 11 aktive Keyword/Absender-Regeln (aus Telegram-Korrekturen)

### Hash-Duplikat-Schutz

Beim Eingang wird MD5 des PDFs berechnet. Treffer in `pdf_hash` → sofort verwerfen + Telegram `♻️ Duplikat`. Schützt gegen Syncthing-Mehrfachlieferungen.

---

## 10. API und Dashboards (Port 8765)

### API-Endpunkte

| Endpoint | Methode | Beschreibung |
|---|---|---|
| `/status` | GET | Dispatcher-Status + Warteschlange |
| `/dokumente` | GET | Alle Dokumente (`?kategorie=`, `?limit=`) |
| `/dokumente/{id}` | GET | Einzeldokument mit MD-Inhalt |
| `/frage` | POST | Natural-Language-Query gegen SQLite (Ollama) |
| `/korrektur` | POST | Manuelle Kategorie-/Typ-Korrektur |
| `/api/cache/*` | GET | Proxy zu cache-reader (Search, File, Stats) |
| `/api/vault-file?path=...` | GET | PDF aus Vault im Browser öffnen (Path-Traversal blockiert) |
| `/api/queue/state` | GET | Warteschlangen-Status |
| `/api/logs?q=<stem>` | GET | Gefilterte Logs (Ringbuffer 5000 Zeilen) |
| `/api/batch/runs` | GET | Liste Batch-Läufe |
| `/api/batch/runs/{id}` | GET | Detail + Items + ocr_source pro Item |
| `/api/batch/start` | POST | Batch-Lauf starten |
| `/api/batch/runs/{id}/{pause\|resume\|abort}` | POST | Lauf-Steuerung |
| `/api/batch/runs/{id}/download?kind={summary\|details}` | GET | CSV/JSONL-Stream |
| `/api/duplikate/scan` | POST | Duplikat-Scan starten (byte + semantic, Hintergrund-Thread) |
| `/api/duplikate/status` | GET | Scan-Fortschritt (polling) |
| `/api/duplikate/gruppen` | GET | Alle Gruppen mit Einträgen |
| `/api/duplikate/move` | POST | Einzelnes Duplikat in Quarantäne verschieben |
| `/api/duplikate/move-all` | POST | Alle Duplikate in einem Batch verschieben (Hintergrund-Thread) |
| `/api/duplikate/move-all/status` | GET | Batch-Verschiebe-Fortschritt (polling) |
| `/api/vault-pdf?md=<md-pfad>` | GET | PDF zu einer MD-Datei liefern: liest `original:`-Link, serves PDF als `attachment` — für Wilson Telegram-Download |

### Dashboard-Seiten

| Pfad | Inhalt |
|---|---|
| `/` | Übersicht + Kennzahlen + Quick Actions |
| `/pipeline` | Live-Pipeline-Anzeige, Queue-Bar, Live-Logs-Button pro Dokument |
| `/cache` | cache-reader-Suche, Sprachverteilung, Stale-Pfade, Re-Index-Button |
| `/batch` | Batch-Läufe, Fortschrittsbalken, Pause/Resume/Abort, CSV/JSONL-Download |
| `/review` | Klassifikationen prüfen/korrigieren |
| `/duplikate` | Duplikat-Scan, Gruppenübersicht (byte + semantic), Batch-Verschiebe, Fortschrittsbalken |

**Top-Navigation (aktuell):**
```
[📊 Home] [🔄 Pipeline] [📝 Review] [📂 Vault] [📎 Anlagen] [🔍 Cache] [🥧 Wilson] [🗐 Duplikate]
```
Weitere Einträge werden in späteren Phasen ergänzt.

---

## 11. cache-reader-Service

**Container:** `cache-reader`, Port 8501. FastAPI + SQLite FTS5 + langdetect.

| Endpoint | Beschreibung |
|---|---|
| `GET /search?q=…&limit=N` | Volltextsuche, liefert `path`, `score`, `excerpt`, `langs` |
| `GET /file?path=…` | Voll-Text eines Dokuments (vault-relativer Pfad) |
| `GET /stats` | Index-Größe, Sprachverteilung, Stale-Pfad-Quote |
| `POST /reindex` | Manuelle Neu-Indexierung |

**Verzeichnisstruktur:**
```
cache-reader/
├── Dockerfile
├── requirements.txt          # fastapi, uvicorn, watchdog
├── src/
│   ├── indexer.py            # Scan Cache → FTS5 (Erstlauf + inkrementell)
│   ├── api.py                # FastAPI mit 4 Endpunkten
│   ├── watcher.py            # Auto-Update via watchdog/inotify
│   └── config.py             # Pfade, Port, Log-Level
└── data/
    └── index.db              # SQLite FTS5, ~5–10 MB
```

**Performance (verifiziert 2026-04-19):**
- Erstindexierung: 2.460 Cache-Einträge in 19,6 Sek. (weit unter 30-Sek.-Ziel)
- Such-Performance: 100 Anfragen in 0,63 Sek. (6,3 ms/Anfrage — Ziel war <50 ms)

**docker-compose.yml:**
```yaml
cache-reader:
  build: ./cache-reader
  container_name: cache-reader
  ports: ["8501:8501"]
  volumes:
    - ./syncthing/data/reinhards-vault/.obsidian/plugins/text-extractor/cache:/vault-cache:ro
    - ./cache-reader/data:/data
  environment:
    - CACHE_DIR=/vault-cache
    - INDEX_DB=/data/index.db
  restart: unless-stopped
```

---

## 12. Dispatcher-Batch-Modus (CLI)

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
| `--resume` | Lauf wieder aufnehmen (Logik offen) | — |

**Input-Format:**
```json
{
  "documents": ["Anlagen/20250312_Ferroli_Rechnung.pdf", "..."],
  "query_context": "Handwerker Seggiano 2025",
  "export_target": "/tmp/auswertung_handwerker.csv"
}
```

`structured` schreibt `run_<id>_summary.csv` + `run_<id>_details.jsonl`.
`vault-move` ist im Batch unterdrückt — schützt das Archiv vor versehentlicher Verschiebung.

---

## 13. Umgebungsvariablen

```env
WATCH_DIR=/data/input-dispatcher
TEMP_DIR=/data/dispatcher-temp
CONFIG_FILE=/config/categories.yaml
DOCLING_URL=http://docling-serve:5001
OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b                    # oder gemma4:e4b
OLLAMA_TRANSLATE_MODEL=qwen2.5:7b
OLLAMA_NUM_CTX=8192                        # verhindert GPU-Hänger auf 2-GB-iGPU
OLLAMA_TIMEOUT=300
HYBRID_OCR_MIN_CHARS=500
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
VAULT_PDF_ARCHIV=/data/reinhards-vault/Anlagen
VAULT_ROOT=/data/reinhards-vault
CACHE_READER_URL=http://cache-reader:8501
API_PORT=8765
```

---

## 14. Deployment

```bash
# Build + Start
docker compose build dispatcher cache-reader
docker compose up -d

# Nach Änderungen am Code
docker compose up -d --build dispatcher

# Logs
docker logs -f document-dispatcher
docker logs -f cache-reader

# DB-Rebuild nach Vault-Umstrukturierungen
python3 rebuild_vault_pfad.py

# Klassifikations-Statistiken
docker exec document-dispatcher python3 analyze_classifications.py
```

---

## 15. Wartungs-Skripte

| Skript | Zweck |
|---|---|
| `retrofit_frontmatter.py` | `original:`-Feld in alten MDs auf `[[Anlagen/...]]`-Format korrigieren |
| `cleanup_49_kv.py` | 49-KV-Bereinigung: Fehlklassifizierungen, Deduplizierung, Typ-Unterordner |
| `rebuild_vault_pfad.py` | `vault_pfad` in DB nach Vault-Umstrukturierungen neu aufbauen |
| ~~`scripts/dedup_scan.py`~~ | **entfernt** — Duplikat-Erkennung ist jetzt built-in im Dispatcher (`/api/duplikate/*`) |
| `scripts/frontmatter_check.py` | Schema-Validator (liest, ändert nichts) (Phase 2.6) |
| `dispatcher/analyze_classifications.py` | Statistiken: Hit-Rate, Halluzinationen, Korrekturen pro Modell |
| `reconcile_inbox_orphans.py` | 26 Orphans mit MD in `00 Inbox/` nachträglich in DB eingetragen |
| `batch_reimport.py` | **deprecated** — Vorgänger des Dispatcher-Batch-Modus |
| `wilson/heartbeat.py` | Service-Monitor auf Wilson: pollt 4 Endpunkte, Telegram-Alert, Tages-Report |
| `wilson/heartbeat.service` | systemd user service für Heartbeat |
| `wilson/deploy-wilson.sh` | Einschritt-Deploy: alle Wilson-Scripts + Services via SCP + SSH |

---

## 16. 49 Krankenversicherung — Bereinigung 2026-04 (Ergebnis)

**Ausgangslage:** 5.047 Dateien, ~3.000 Duplikate, kein Typ-Routing, Evernote-Altformat

**Durchgeführt:**
1. Fehlklassifizierungen verschoben (23 Dateien)
2. LEAS-UUID-Dateien umbenannt
3. Duplikate gelöscht (3.008, PDF-Hash-basiert): **5.047 → 2.011 Dateien**
4. OCR-Stubs (621) → `00 Wiederherstellung/` mit `todos:`-Frontmatter
5. Frontmatter-Upgrade: Evernote-Felder → `kategorie_id`, `typ_id`, normierter `adressat`
6. Typ-Unterordner erstellt und befüllt
7. DB `vault_pfad` rebuild: 1.009 Einträge aktualisiert

**Ergebnis-Struktur:**
```
49 Krankenversicherung/
  Leistungsabrechnung Marion/   413 Dateien
  Leistungsabrechnung Reinhard/ 294 Dateien
  Arztrechnung/                 282 Dateien
  Sonstiges/                     87 Dateien
  Beitragsinformation/            28 Dateien
  Rezept/                         28 Dateien
  Leistungsabrechnung Sonstiges/  22 Dateien
  00 Wiederherstellung/          621 Stubs
  undatiert/                      36 Dateien
  [Jahresordner 2013–2025]       199 nicht typisiert
```

---

## 17. Umsetzungsplan

### Übersicht und Status

| Phase | Inhalt | Aufwand | Status |
|---|---|---|---|
| 0 | Strategie-Festlegung | 30 Min | ✅ 2026-04-19 |
| 1 | Infrastruktur (Syncthing, Cache-Reader) | 3 Tage | ✅ 2026-04-19 |
| 2 | Dispatcher Batch-Modus | 3,5 Tage | ✅ 2026-04-19/20 |
| **W** | **Wilson-Pipeline (Vorverarbeitung auf Pi)** | **2 Tage** | **✅ 2026-04-26** |
| 2.5 | Duplikat-Erkennung und -Bereinigung | 1 Tag | ✅ 2026-04-27 |
| 2.6 | Frontmatter-Vereinheitlichung | 1,5 Tage | ✅ 2026-04-28 |
| 2.7 | Interaktive Klassifikation via Telegram | 1,75 Tage | ✅ 2026-04-28 |
| 2.8 | Admin-Web-Interface | 3 Tage | ⏳ offen |
| 3 | Telegram-Bot-Erweiterung | 3,25 Tage | ⏳ offen |
| 4 | Standard-Auswertungs-Templates | 2,5 Tage | ⏳ offen |
| 5 | Monitoring + Home-Konsolidierung | 1,5 Tage | ⏳ offen |
| 6 | Dashboard-Review + Hilfe-System | 1 Tag | ⏳ offen |

**Gesamtaufwand: ~22 Entwicklungstage**

### Vorgehen: Schritt-für-Schritt mit Test-Gates

Jede Phase endet mit einem **expliziten Test-Gate**. Die nachfolgende Phase startet erst, wenn die Abnahmekriterien erfüllt und vom User freigegeben sind.

**Test-Gate-Struktur pro Phase:**
1. Automatisierte Tests (unit + integration) — grün
2. Manuelle Stichprobe im Dashboard — sichtbar und korrekt
3. End-to-End-Test mit Echtdaten — erwartetes Verhalten
4. **User-Freigabe erforderlich**

---

### Phase 0: Strategie-Festlegung ✅ ABGESCHLOSSEN am 2026-04-19

Batch-Rescan-Entscheidung verbindlich dokumentiert, neue Architektur in Memory-Index eingetragen, `ARCHITEKTUR.md` als Referenz gespeichert.

**User-Freigabe Phase 0:** 2026-04-19

---

### Phase 1: Infrastruktur ✅ ABGESCHLOSSEN am 2026-04-19

**Ergebnis:** Cache-Reader auf Port 8501 produktiv, Dashboard `/cache` mit Live-Suche.

**Erzielte Performance:**
- Erstindexierung: 2.460 Einträge in 19,6 Sek.
- Such-Performance: 6,3 ms/Anfrage (Ziel <50 ms)
- Inkrementelles Update: File-Watcher reagiert binnen Sekunden

**Zusätzliche Erweiterungen über die Planung hinaus:**
- **langdetect-Spracherkennung** im Indexer (Text Extractor-Angabe war unzuverlässig)
- **Klickbare Suchergebnisse + Volltext-Modal** im Dashboard
- **Stale-Pfad-Visualisierung:** ~1.030 veraltete Pfade (Apple-Notes-Duplikate mit " 2"-Suffix) werden grau markiert
- **Cache-Control-Header** auf allen HTML-Responses

**User-Freigabe Phase 1:** 2026-04-19

---

### Phase 2: Dispatcher Batch-Modus ✅ ABGESCHLOSSEN am 2026-04-19/20

**Ergebnis:** CLI `--batch` + Hybrid-OCR-Gate + CSV/JSONL-Export + Dashboard `/batch` produktiv.

| Schritt | Status |
|---|---|
| 2.0 Auto-Rescan entfernen | ✅ 2026-04-19 |
| 2.1 CLI `--batch` | ✅ 2026-04-19 (smoke-getestet) |
| 2.2 Hybrid-OCR-Gate (`resolve_ocr_text()`, `HYBRID_OCR_MIN_CHARS=500`) | ✅ 2026-04-19 |
| 2.3 Ausgabeformate (CSV summary + JSONL details) | ✅ 2026-04-19 |
| 2.4 Dashboard `/batch` (Tabellen, REST-API, vier Testläufe) | ✅ 2026-04-19 |

**Stabilitäts-Hotfixes 2026-04-20:**
- `OLLAMA_NUM_CTX` / `OLLAMA_TIMEOUT` als konfigurierbare Env-Variablen
- Inbox-DB-Eintrag auch bei fehlgeschlagener Klassifikation
- 26 Orphans via `reconcile_inbox_orphans.py` in DB eingetragen

**Offene Punkte (nicht blockierend für 2.5):**
- [ ] Resume-Logik für `--resume RUN_ID`
- [ ] `--force-docling`-Regressionstest
- [ ] OCR-Quellen-Kachel im Pipeline-Dashboard
- [ ] 20 Stichproben-PDFs: Hybrid vs. reines Docling

**User-Freigabe Phase 2:** 2026-04-27

---

### Phase W: Wilson-Pipeline ✅ ABGESCHLOSSEN am 2026-04-26

**Ziel:** Vorverarbeitung jedes eingehenden Dokuments direkt auf dem Raspberry Pi (Wilson). Wilson extrahiert OCR-Text, Metadaten und schreibt ein Sidecar-JSON. Der Ryzen-Dispatcher verwendet den Sidecar-Bypass und überspringt OCR+LLM komplett.

**Entscheidungen:**
- Kein `typ_id` mehr — Vault-Struktur: `{vault_folder}/{Jahr}/dateiname.md`
- Kategorien sind fest vorgegeben, keine Typen innerhalb der Kategorien
- Datum und Absender aus Dokumentinhalt (LLM), nicht aus Scanner-Prefix
- Wilson hält Dokument 60 Minuten in Pending-Queue, Telegram-Benachrichtigung mit Inline-Korrektur
- Ryzen down → Dokument parkt, Telegram-Warnung

**Umgesetzte Komponenten:**

| Komponente | Datei | Status |
|---|---|---|
| Wilson doc_processor | `wilson/doc_processor.py` | ✅ produktiv |
| Wilson systemd service | `wilson/doc-processor.service` | ✅ aktiv |
| Dispatcher OCR-Proxy | `dispatcher/dispatcher.py` `/api/ocr` | ✅ produktiv |
| Dispatcher Sidecar-Bypass | `dispatcher/dispatcher.py` `process_file()` | ✅ produktiv |
| Kategorien ohne Typen | `dispatcher-config/categories.yaml` | ✅ umgestellt |
| Vault-Pfad (Jahr-only) | `dispatcher/dispatcher.py` `build_vault_path()` | ✅ TYPE_ROUTING=0 |
| Dateiname ohne Typ-Label | `dispatcher/dispatcher.py` `build_clean_filename()` | ✅ vereinfacht |

**Sidecar-Format v2.0:**
```json
{
  "version": "2.0",
  "dokument": {
    "absender": "HUK-COBURG-Krankenversicherung AG",
    "datum": "YYYY-MM-DD",
    "kategorie_id": "krankenversicherung",
    "adressat": "Marion",
    "kurzbezeichnung": "Leistungsabrechnung-KV",
    "beschreibung": "3-5 Sätze Beschreibung...",
    "dateiname": "20260416_HUK-COBURG-Krankenversicherung_Leistungsabrechnung-KV.pdf"
  },
  "verarbeitung": { ... }
}
```

**Deterministisches Absender-Override in Wilson:**
Bekannte KV-Absender (HUK, Gothaer, Barmenia, vigo) werden deterministisch auf `krankenversicherung` korrigiert, auch wenn das LLM eine andere Kategorie wählt. Gleiches gilt für den Adressat (HUK → Marion, Gothaer/Barmenia → Reinhard).

**Testergebnis 2026-04-26:**
- HUK-COBURG Leistungsabrechnung 2020: Sidecar-Bypass aktiv → `49 Krankenversicherung/2020/` in ~1s (statt ~3 Min OCR+LLM)
- TYPE_ROUTING: 0 Einträge (Typen vollständig entfernt)
- Keyword-Rules: 10 aktiv (inkl. KV-Erkennung via HUK/Gothaer und Tierarzt-Routing)

**Ergänzungen 2026-04-27:**

| Komponente | Datei | Beschreibung |
|---|---|---|
| Heartbeat Agent | `wilson/heartbeat.py` | Pollt Dispatcher/Cache-Reader/Docling/Ollama alle 90s. Telegram-Alert nach 2 Fehlern, Recovery-Meldung, Tages-Report 08:00. State in `~/.openclaw/heartbeat_state.json`. |
| Heartbeat Service | `wilson/heartbeat.service` | systemd user service, alle Intervalle via Env-Vars konfigurierbar |
| Deploy-Skript | `wilson/deploy-wilson.sh` | Deployt alle Wilson-Scripts + Services in einem Schritt |
| Vault-Suche Skill | `~/.openclaw/skills/enzyme/SKILL.md` | Semantische Suche im Vault via `POST http://192.168.86.195:11180/catalyze` — **Port-Fix: war 8080 (tot), jetzt 11180** |
| PDF-Download Skill | `~/.openclaw/skills/dispatcher/SKILL.md` | Neuer Abschnitt: `/api/vault-pdf` — Wilson lädt PDF und sendet via Telegram `sendDocument` |
| PDF-Endpunkt | `dispatcher/dispatcher.py` `/api/vault-pdf` | Liest MD, löst `original:`-Link auf, liefert PDF als Download-Attachment |

**Ergänzungen 2026-04-29:**

| Komponente | Datei | Beschreibung |
|---|---|---|
| DeepSeek-Integration | `wilson/doc_processor.py` | Primäres LLM: DeepSeek API (`deepseek-chat`). Fallback: Ollama `gemma4:e4b`. Key via `DEEPSEEK_API_KEY` in Service-Env. |
| HTML-Entity-Fix | `wilson/doc_processor.py` | `html.unescape()` für `absender`, `kurzbezeichnung`, `beschreibung` nach LLM-Extraktion |
| Keyword-Override immobilien_eigen | `wilson/doc_processor.py` | Bei `kategorie=archiv` + Seggiano/Podere/Grassauer/Bonifica → deterministisch `immobilien_eigen` |
| PDF-Upload via Telegram | `wilson/doc_processor.py` | `_handle_tg_pdf_upload()` — PDF aus Dispatcher-Bot-Chat empfangen, OCR, Sidecar, Weiterleitung |
| Getrennter Bot-Token | `wilson/doc-processor.service` | Dispatcher-Bot: `8382100394`. Verhindert 409-Konflikt mit AI-Assistenten. |
| Dispatcher-Bot Befehle | `wilson/doc_processor.py` | `/hilfe`, `/status` (ausstehende Dokumente mit Icons) |
| AI-Assistent-Bot (initial) | `wilson/ai_assistant.py` | Eigenständiger Bot (Token `8621101278`): DeepSeek-Chat + Projekte Vault direkt + enzyme für Reinhards Vault. **Abgelöst durch OpenClaw-native (s.u.)** |
| POLIZIA-Keyword-Regel | `dispatcher-config/categories.yaml` | Polizia Stradale / POLIZIA DI STATO → `fahrzeuge` (zuvor LLM-Fehler → fengshui) |
| Mac Syncthing Tile | `dispatcher/dispatcher.py` | URL-Extraktion aus `svc.address` ohne `tcp://`-Prefix (Regex-Fix) |
| Telegram-Menü | Telegram API `setMyCommands` | Beide Bots haben `/`-Befehlsmenü in Telegram |
| Wilson Update-Button | `dispatcher/dispatcher.py` | `POST /api/wilson/update` + `GET /api/wilson/update/status` im `/wilson`-Dashboard |

**Ergänzungen 2026-04-30 — Drei-Bot-Ökosystem:**

| Komponente | Datei | Beschreibung |
|---|---|---|
| OpenClaw-native Wilson | `~/.openclaw/openclaw.json` | `channels.telegram.enabled: true`. Wilson nutzt jetzt den OpenClaw Gateway als primären AI-Assistenten statt `ai_assistant.py` |
| ai-assistant.service deaktiviert | Wilson systemd | `systemctl --user stop/disable ai-assistant`. `ai_assistant.py` bleibt als Backup im Repo. |
| Lärmbär (neu) | `wilson/laerenbaer.py` | Neuer eigenständiger HA-Bot. Token aus HA `core.config_entries`. Befehle: `/sensoren`, `/sensor`, `/kamera`, `/kameras`, `/schalten`, `/hilfe` |
| laerenbaer.service (neu) | `wilson/laerenbaer.service` | systemd user service, `EnvironmentFile=~/.openclaw/secrets.env` |
| deploy-wilson.sh | `wilson/deploy-wilson.sh` | Erweitert um Lärmbär-Deploy (laerenbaer.py + service + enable) |
| secrets.env (neu) | `~/.openclaw/secrets.env` (auf Wilson) | Zentrale Secrets-Datei (chmod 600): TELEGRAM_BOT_TOKEN, LAERENBAER_BOT_TOKEN, DEEPSEEK_API_KEY |
| secrets.env.example | `wilson/secrets.env.example` | Vorlage ohne echte Werte, im Git-Repo |
| Secrets aus Service-Files entfernt | `wilson/*.service` | Hardcodierte Tokens/Keys aus allen `.service`-Dateien entfernt → `EnvironmentFile` |
| Secrets aus Python entfernt | `wilson/*.py` | Hardcodierte Tokens aus `doc_processor.py`, `ai_assistant.py`, `heartbeat.py` entfernt (leerer Default) |
| BOOT.md | `~/Vaults/BOOT.md` | Neu geschrieben: Wilson-Identität, Drei-Bot-Übersicht, Skills-Tabelle, Kontext (Garten/Immobilien/Business) |
| AGENTS.md | `~/Vaults/AGENTS.md` | Drei-Bot-Tabelle mit Token-Präfixen und Rollen |
| file-manager Skill | `~/.openclaw/skills/file-manager/SKILL.md` | Pfad korrigiert: `~/Vaults/SecondBrain` → `~/Vaults` |
| OpenClaw Default-Modell | `~/.openclaw/openclaw.json` | `agents.defaults.model.primary: "deepseek/deepseek-v4-flash"` (war `deepseek-v4-pro`) |
| auth-profiles.json | `~/.openclaw/agents/main/agent/auth-profiles.json` | DeepSeek-Key aktualisiert (separater Key-Store von secrets.env) |
| Syncthing bidirektional | Syncthing Ryzen API | `reinhards-vault` und `input-dispatcher` auf Ryzen: `sendreceive` (war `sendonly`). Mac als Gerät zu `projekte-vault` auf Wilson eingeladen. |
| Cron-Jobs bereinigt | `~/.openclaw/cron/jobs.json` | 3 abgelaufene Einmal-Jobs entfernt (EVE Steckdose, Windmesser Batterien, Kräuter+Gurken) |
| Tages-Briefing aufgeteilt | `~/.openclaw/cron/jobs.json` | Tages-Briefing (7:30) und Feng-Shui-Briefing (7:45) sind jetzt getrennte Cron-Jobs |
| Feng-Shui-Briefing | `~/.openclaw/cron/jobs.json` | Neuer Job 7:45: gua_calculator-Output direkt senden (alle 4 Energien + Fokus-Satz). Kua-Zahl 2, Geburtsdatum 08.12.1962 |
| OpenClaw reinstalliert | Wilson npm | `npm uninstall -g openclaw && npm install -g openclaw` — Hash-Mismatch in dist/ nach partiellem npm-Update behoben |
| enzyme Cron-Pfad | `/etc/cron` (Ryzen) | Cron-Job für enzyme-Refresh korrigiert: alter Pfad `/docker/docling-workflow/` → `/docker/RYZEN - docling-workflow/`. Zeit: 23:00 → 01:00 |
| enzyme warn-Schwelle | `dispatcher/dispatcher.py` | Warn bei >36h (war 24h), Error bei >72h (war 48h) |
| Mac Sync Dashboard | `dispatcher/dispatcher.py` | Kein Link mehr im Card-Titel (Mac GUI nur auf localhost erreichbar). Card zeigt Verbindungsstatus + Ordner-Sync-%. |

**Vault-Suche + PDF-Download — Telegram-Flow:**
```
Reinhard: "Hast du die Kündigung von Linke?"
Wilson:    → POST :11180/catalyze {"query": "Kündigung Linke"}
           📂 1 Treffer: 20260318-Franziska_Linke-Kündigung_Mietverhältnis.md

Reinhard: "Schick mir das PDF"
Wilson:    → GET :8765/api/vault-pdf?md=51%20Immobilien%20vermietet%2F...md
           → POST api.telegram.org/sendDocument
           [540 KB PDF im Telegram-Chat]
```

**Hinweis OpenClaw Skills:** Skills liegen auf Wilson in `~/.openclaw/skills/` (nicht im Syncthing-Vault). Änderungen erfordern direktes Editieren via SSH + `openclaw gateway restart` (oder `systemctl --user restart openclaw-gateway`).

---

### Phase 2.5: Duplikat-Erkennung und -Bereinigung ✅ ABGESCHLOSSEN am 2026-04-27

**Ergebnis:** 1.188 Duplikate erkannt und bereinigt. Anlagen/ von 3.073 auf 1.885 PDFs reduziert.

**Umsetzung:**

Zwei Erkennungsmethoden wurden direkt in den Dispatcher integriert (kein externes Skript):

| Methode | Erkennt | Treffer |
|---|---|---|
| Byte-Duplikat (MD5) | Exakt gleiche PDF-Bytes | 879 Gruppen |
| Semantisch (datum + absender + Trigram-Jaccard ≥ 0,25) | Re-Scans / gleicher Inhalt, andere Bytes | 111 Gruppen |
| **Gesamt** | | **990 Gruppen, 1.188 Duplikate** |

**Original-Bestimmung (Scoring):**
1. Hat Vault-Eintrag in DB → Original (höchste Priorität)
2. Dateiname beginnt mit `YYYYMMDD[_-]` → bevorzugt
3. Kein `_<zahl>`-Suffix → bevorzugt
4. Kürzerer Dateiname → bevorzugt

**Kanonisches Umbenennen:** Das Original-PDF wird beim Verschieben eines Duplikats auf den kanonischen Namen (aus dem MD-Stem) umbenannt, sodass Original und Quarantäne-Kopie denselben Dateinamen tragen.

**Quarantäne-Struktur:**
- PDF-Duplikate → `Anlagen/00 Duplikate/` (byte) bzw. `Anlagen/00 Text-Duplikate/` (semantisch)
- MD-Duplikate → `00 Duplikate/` (sichtbar in Obsidian)
- Frontmatter-Link im Original-MD wird auf neuen PDF-Pfad aktualisiert

**Scan-Performance:** 3.073 PDFs in ~12 Sekunden

**Batch-Ergebnis:**
- 1.184 erfolgreich verschoben
- 3 stale (Datei fehlend / Sonderzeichen im Namen) → manuell als verarbeitet markiert
- 1.018 PDFs in `Anlagen/00 Duplikate/` + 165 PDFs in `Anlagen/00 Text-Duplikate/` + 3 MDs → nach Prüfung gelöscht

**Neues Dashboard:** `/duplikate` mit Scan-Button, Gruppen-Tabelle (byte + semantic), Batch-Verschieben mit Fortschrittsbalken, Status-Polling alle 1,5 Sek.

**Neue DB-Tabellen:** `duplikat_scans`, `duplikat_gruppen`, `duplikat_eintraege`

**User-Freigabe Phase 2.5:** 2026-04-27

---

### Phase 2.6: Frontmatter-Vereinheitlichung ✅ ABGESCHLOSSEN (2026-04-28)

**Ziel:** Einheitliches Frontmatter-Schema ohne Batch-Migration. Bestehendes bleibt erhalten.

**Ausgangslage (2026-04-28):** 7 konkurrierende Schemata, 1.919 MDs total, 0 mit vollständigem Unified Schema (`erstellt_am` + `tags`), 1.719 upgradeable.

| Schema | Anzahl |
|---|---|
| Evernote (date created) | 797 |
| Apple Notes (created + imported) | 512 |
| Sonstige | 191 |
| OCR-Stub (todos) | 107 |
| kein Frontmatter | 84 |
| Dispatcher v1 (kategorie + original) | 81 |
| Evernote+Import | 80 |
| Legacy (category + source) | 57 |
| Dispatcher v2 (kategorie_id + adressat) | 10 |

**Unified Minimal Schema — Pflichtfelder:**
- `erstellt_am` (ISO-Datum), `tags` (Liste)

**Legacy-Mappings implementiert:**

| Legacy-Feld | Neues Feld |
|---|---|
| `date created` (Evernote) | `erstellt_am` |
| `created` (Apple Notes) | `erstellt_am` |
| `erstellt` (Dispatcher v1) | `erstellt_am` |
| Dateiname `YYYYMMDD_...` | `erstellt_am` |

Legacy-Felder bleiben nach Upgrade erhalten (Rückwärtskompatibilität).

**Umgesetzt:**
- Hilfsfunktionen: `_fm_classify()`, `_fm_parse_date()`, `_fm_probe()`, `_fm_apply_upgrade()`, `_fm_stats()` (mit 5-Min-Cache)
- `GET /frontmatter` — Dashboard (Schema-Verteilung, Probe-Panel, Upgrade-Button)
- `GET /api/frontmatter/stats` — JSON Schema-Report
- `GET /api/frontmatter/probe?md=<pfad>` — Vorschau ohne Schreiben
- `POST /api/frontmatter/upgrade?md=<pfad>` — Upgrade eines einzelnen MD
- Nav-Link 🏷️ Frontmatter in Dashboard- und Cache-Navbar

**Strategie:** On-Demand (ein Dokument per Klick). Kein Vault-weiter Batch-Upgrade.

**User-Freigabe:** 2026-04-28 — Phase 2.7 ab nächster Session

---

### Phase 2.7: Interaktive Klassifikation via Telegram ✅ ABGESCHLOSSEN (2026-04-28)

**Ziel:** Bei unsicherer LLM-Klassifikation (Konfidenz mittel/niedrig) geführter Dialog auf dem Smartphone.

**Dialog-Logik:**
```
Konfidenz HOCH   → heutige Telegram-Nachricht (Info + Bestätigen/Korrigieren)
Konfidenz MITTEL/NIEDRIG → geführter Dialog in 4 Schritten:
  ① Kategorie-Auswahl (Inline-Buttons)
  ② Typ-Auswahl (gefiltert nach Kategorie)
  ③ Absender-Auswahl (Autocomplete aus DB) + "Neu anlegen"-Flow
  ④ Adressat-Auswahl (Reinhard / Marion / Linoa / Sonstiges)
```

**Persistenz:** Jeder Dialog-Abschluss erzeugt DB-Eintrag + Lernregel + ggf. neuen Aussteller-Eintrag. Jede Interaktion wird zu Trainingsdaten für zukünftige Automatisierung.

**User-Freigabe:** 2026-04-28

---

### Phase 2.8: Admin-Web-Interface für Stammdaten-DB ⚡ TEILWEISE ÜBERHOLT

**Ziel:** Browser-basiertes CRUD-Interface für Aussteller (47), Aliase (181), Lernregeln (11), Kategorie-Vorschläge.

**Neue Routen (Port 8765):**
```
GET/POST /admin/aussteller        Tabelle mit Filter/Sortierung, Neu anlegen
GET/PUT/DELETE /admin/aussteller/{id}    Detailansicht + Bearbeitung + Löschen
GET/POST/DELETE /admin/aliases     Alias-Verwaltung
GET/PUT/DELETE /admin/lernregeln   Regeln-Verwaltung
GET/POST /admin/kategorie-vorschlaege   Offene Vorschläge aus Phase 2.7
```

**Neue DB-Tabellen:** `personen` (ersetzt harten String `adressat`), `kategorie_vorschlaege`, `audit_log`.

**Zugriff:** Nur im lokalen Netzwerk. Optional: Basic Auth via `DASHBOARD_PASSWORD`.

**Stand 2026-04-30:** Stammdaten-Verwaltung (Aussteller, Lernregeln, Aliase) ist via Wilson (OpenClaw + natürliche Sprache) grundsätzlich möglich. Ein dediziertes Web-CRUD-Interface wurde nicht gebaut und ist nicht priorisiert.

---

### Phase 3: Telegram-Bot-Erweiterung ⚡ DURCH WILSON ABGELÖST (2026-04-30)

**Ursprüngliches Ziel:** Strukturierte Dispatcher-Bot-Befehle für Suche und Auswertung.

**Warum abgelöst:** Wilson (OpenClaw Gateway) übernimmt diese Funktionen via natürlicher Sprache und Skills:

| Geplanter Befehl | Ersatz via Wilson |
|---|---|
| `/suche <begriff>` | „Suche nach X" → enzyme-Skill + dispatcher-Skill |
| `/inbox` | „Zeige Inbox-Dokumente" → dispatcher-Skill |
| `/status` | „Wie ist der Pipeline-Status?" → dispatcher-Skill |
| `/auswertung` | Natürlichsprachige Abfrage + enzyme |
| `/verarbeite <ids>` | Kein direkter Ersatz (noch offen) |
| `/reocr <id>` | Kein direkter Ersatz (noch offen) |

Dispatcher-Bot (`doc_processor.py`) behält `/status` und `/hilfe` als Basis-Befehle.

---

### Phase 4: Standard-Auswertungs-Templates ⚡ DURCH WILSON ABGELÖST (2026-04-30)

**Ursprüngliches Ziel:** Feste Telegram-Befehle mit CSV-Export.

**Warum abgelöst:** Wilson kann alle drei geplanten Auswertungstypen natürlichsprachig via enzyme + dispatcher-Skill ausführen:

| Geplantes Template | Entsprechung |
|---|---|
| `/steuer-handwerker 2025` | „Welche Handwerkerrechnungen für Seggiano 2025?" |
| `/kv-erstattung Marion 2025` | „KV-Leistungsabrechnungen für Marion 2025 mit Summen" |
| `/italien 2025` | „Alle Italien-Kosten 2025: IMU, TARI, Acquedotto" |

**Noch fehlend:** Strukturierter CSV-Export aus Wilson heraus. Kann bei Bedarf als eigenständiges Feature nachgezogen werden.

---

### Phase 5: Monitoring + Home-Konsolidierung ⏳ OFFEN

**Ziel:** Sichtbarkeit über Coverage und Qualität der neuen Architektur.

**Haupt-Dashboard `/` — Finalisierung:**
- Strategie-Hinweis prominent oben
- Kennzahlen-Raster 3×3: Vault, DB, Cache, Stammdaten, Duplikate, Pipeline, Batch, Auswertungen, Frontmatter
- Quick Actions-Leiste

**Einheitliche finale Top-Navigation:**
```
[📊 Home] [🔄 Pipeline] [📝 Review] [📂 Vault] [📎 Anlagen]
[🔍 Cache] [♻️ Duplikate] [🏷️ Frontmatter] [⚙️ Batch]
[📈 Auswertungen] [🗂️ Admin] [🥧 Wilson]
```

**Finale User-Freigabe:** _______________________ (Datum) — Projekt-Umstellung abgeschlossen

---

### Phase 6: Dashboard-Review + Hilfe-System ⏳ OFFEN

**Ziel:** Alle bestehenden Dashboards auf Sinnhaftigkeit prüfen, unnötige entfernen, verbleibende mit einem laienverständlichen Hilfe-Button ausstatten.

**Schritt 1 — Review (welche Dashboards gibt es, welche brauchen wir wirklich?):**

| URL | Name | Kandidat |
|---|---|---|
| `/` | Haupt-Dashboard (Pipeline, Live-Status) | ✅ behalten |
| `/pipeline` | Pipeline-Schritt-Detail | ? prüfen |
| `/review` | Manuelles Review einzelner Dokumente | ? prüfen |
| `/vault` | Vault-Struktur-Übersicht | ? prüfen |
| `/vault/anlagen` | Anlagen-Dateinamen-Analyse | ? prüfen |
| `/cache` | Cache-Reader-Suche | ✅ behalten |
| `/batch` | Batch-Verarbeitung | ? prüfen |
| `/wilson` | Wilson Pi Status | ? prüfen |
| `/duplikate` | Duplikat-Erkennung und -Bereinigung | ✅ behalten |
| `/frontmatter` | Frontmatter-Vereinheitlichung | ✅ behalten |

**Schritt 2 — Hilfe-Button je Dashboard:**

Jedes verbleibende Dashboard bekommt einen `❓ Hilfe`-Button in der Kopfzeile. Klick öffnet ein Modal mit drei Abschnitten:
- **Was macht dieses Dashboard?** (1–2 Sätze, keine Fachbegriffe)
- **Wann ist es nützlich?** (konkreter Anwendungsfall)
- **Beispiel:** (Screenshot-Beschreibung oder Beispiel-Workflow)

**Implementierung:** Wiederverwendbare JS-Funktion + CSS-Modal, die in alle verbleibenden HTML-Templates eingebaut wird. Hilfe-Texte je Dashboard individuell.

**User-Freigabe:** _______________________ (Datum)

---

## 18. Meilensteine

| Meilenstein | Nach Phase | Status |
|---|---|---|
| M1: Cache-Reader + Dashboard | 1 | ✅ 2026-04-19 |
| M2: Batch-Modus + Dashboard | 2 | ✅ 2026-04-19/20 (User-Freigabe ausstehend) |
| M2.5: Duplikat-Management | 2.5 | ✅ 2026-04-27 |
| M2.6: Frontmatter-Schema | 2.6 | ✅ 2026-04-28 |
| M2.7: Interaktive Klassifikation | 2.7 | ✅ 2026-04-28 |
| M2.8: Admin-Web-Interface | 2.8 | ⚡ durch Wilson abgelöst |
| M3: Telegram-Bot-Erweiterung | 3 | ⚡ durch Wilson abgelöst |
| M4: Auswertungs-Templates | 4 | ⚡ durch Wilson abgelöst |
| M5: Finale Konsolidierung | 5 | ⏳ in Arbeit |
| M6: Dashboard-Review + Hilfe-System | 6 | ⏳ in Arbeit |

---

## 19. Risiken und Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
|---|---|
| Cache-Qualität reicht nicht für Auswertungen | Hybrid-Fallback auf Docling; `--force-docling` bei Zweifel |
| Telegram-Ratelimit bei großen Auswertungen | Gestückelte Nachrichten, Datei-Upload statt Inline |
| Cache-Reader-Index veraltet | File-Watcher + Healthcheck, manuelles `/reindex` |
| Syncthing-Konflikte bei Mac+Ryzen | Isolation-Regeln (Phase 1.1), Monitoring `.sync-conflict-*` |
| Docling-Container instabil bei Dauerlast | Retry-Logik, Fortschritt persistieren, `--resume`-Parameter |
| Falsch-positives Duplikat (Text-Hash-Kollision) | Quarantäne statt Löschung, 30-Tage-Frist, Telegram-Rückfrage |
| Markdown-Links zeigen ins Leere nach Quarantäne | Links nicht automatisch umschreiben; Quarantäne-INFO.md zeigt Original-Pfad |

---

## Anhang: Technische Kennzahlen

| Kennzahl | Wert |
|---|---|
| Vault-PDFs gesamt (nach Dedup 2026-04-27) | **1.885** (war 3.073) |
| Vault-Markdown-Notizen | 1.923 |
| Text-Extractor-Cache (PDFs) | 846 (26% des alten Bestands) |
| Verwertbarer OCR-Text im Cache | 695 PDFs (82%) |
| Dispatcher-verarbeitete Dokumente | 123 |
| Hochkonfidenz-Treffer im Cache | ~212 (Keyword-Match) |
| Bekannte Absender (absender.yaml) | 47 |
| Lernregeln aus Korrekturen | 11 |
| Klassifikations-Kategorien | 16 |
| Primäres Klassifikations-LLM (Dispatcher) | gemma4:e4b (Ollama) |
| Primäres LLM Wilson (AI-Assistent) | deepseek/deepseek-v4-flash (DeepSeek API) |
| Übersetzungs-LLM | qwen2.5:7b |
| Wilson AI-Architektur | OpenClaw Gateway + Skills (enzyme, dispatcher, homeassistant, file-manager) |
| Aktive Wilson Cron-Jobs | 13 (davon 1 Einmal-Job mit deleteAfterRun) |
| Geschätzter Vollrescan-Aufwand (verworfen) | 63–157 Stunden (nach Dedup) |
| Duplikat-Scan 2026-04-27 | 990 Gruppen, 1.188 Duplikate bereinigt |
| MD-Dateien mit Unified Schema | ~219 (11,5%) |

---

*Aktiver Entwicklungszweig: `feature/classification-v2`*
*Repos: `reinhard888ByDesign/docling-workflow`, `reinhard888ByDesign/ollama-stack`*
