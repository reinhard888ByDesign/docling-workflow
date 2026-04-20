# Persönliches KI-gestütztes Dokumentenmanagement
## Projektbeschreibung und Umsetzungsplan

*Stand: April 2026 — Entscheidungen getroffen, Umsetzung steht aus*

---

## Executive Summary

Das Projekt wechselt von einer **"vollständigen Batch-Klassifikation aller Bestandsdokumente"** zu einer **"flachen Archiv-Architektur mit On-Demand-Verarbeitung"**. Der geplante Rescan von 3.246 PDFs wird verworfen (108–270 Std. Rechenzeit bei unklarem Nutzen). Stattdessen:

- **Neue Dokumente** durchlaufen weiterhin die vollautomatische Dispatcher-Pipeline
- **Bestandsdokumente** bleiben unverändert und werden von Text Extractor passiv OCR-indexiert
- **Auswertungen** erfolgen auf Anforderung: Suche → Treffer → Dispatcher-Batch-Modus → strukturierte Ausgabe
- **Mobile Abfragen** über Telegram-Bot auf dem Pi, der HTTP-Requests an einen neuen Cache-Reader-Service auf dem Ryzen sendet

Alle Architektur-Entscheidungen sind in **Abschnitt 8** dokumentiert. Der **Umsetzungsplan in Abschnitt 9** beschreibt neun Phasen über ca. 22 Entwicklungstage — inklusive Dashboard-Integration pro Phase (Backend und UI werden immer gemeinsam umgesetzt) und expliziten Test-Gates am Ende jeder Phase. Die nächste Phase startet erst nach User-Freigabe der vorherigen. Keine parallelen Phasen, kein "Big Bang"-Release.

Enthalten sind: Duplikat-Erkennung (1.030 Bestands-Redundanzen), Frontmatter-Vereinheitlichung über sechs Legacy-Schemata, interaktive Telegram-Klassifikation bei niedriger Konfidenz, Admin-Web-Interface für die wachsende Stammdaten-Datenbank (47 Aussteller, 181 Aliase, 11 Lernregeln) sowie 6 neue und 6 angepasste Dashboards mit einheitlicher Navigation.

---

## 1. Projektüberblick

### Worum es geht

Reinhard betreibt ein vollständig **lokales**, KI-gestütztes Dokumentenmanagementsystem für seine persönlichen und geschäftlichen Unterlagen — Immobilien in Deutschland und Italien, Krankenversicherung, Fahrzeuge, Steuern, Geschäftsdokumentation, Reisen und mehr. Das System läuft **ausschließlich auf eigener Hardware**, ohne Cloud-Dienste. Alle KI-Modelle laufen lokal via Ollama.

Dokumente entstehen auf drei Wegen:
- **Papierscans** vom Raspberry Pi (Briefpost, Rechnungen)
- **PDF-Importe** von Email, Behördenportalen, Online-Banking
- **Bereits vorhandener Bestand** aus jahrelanger digitaler Ablage (Evernote-Migration, Apple Notes, frühere Scans)

### Infrastruktur

| Komponente | Hardware | Rolle |
|---|---|---|
| Raspberry Pi ("Wilson") | ARM | Scanner-Einheit, Datei-Eingang, OpenClaw-Gateway |
| Ryzen-Workstation | AMD Ryzen, Linux | Hauptserver: OCR, KI, Dispatcher, Vault |
| Mac | Apple Silicon | Obsidian-Client, tägliche Nutzung |
| Syncthing | — | Bidirektionale Replikation Pi ↔ Ryzen ↔ Mac |
| Obsidian | — | Vault-Frontend, Wissensdatenbank |

### Obsidian-Vault

Der Vault ist das zentrale Dokumentenrepositorium. Er enthält:

| Typ | Anzahl | Anmerkung |
|---|---|---|
| Markdown-Notizen | **1.923** | Teils auto-generiert, teils manuell |
| PDFs | **3.246** | Davon 3.212 im `Anlagen/`-Ordner (verlinkte Anhänge) |
| Gesamt Dateien | **5.169** | Exkl. Plugin-Daten |

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

**Wichtige Besonderheit:** Da 3.212 von 3.246 PDFs im Ordner `Anlagen/` liegen (verlinkte Anhänge von Markdown-Notizen), ist die primäre Kategorisierungsstruktur in den **Markdown-Notizen** — nicht in der PDF-Ablage selbst. Die Markdown-Notizen sind nach Kategorie sortiert, die zugehörigen PDFs liegen flach in `Anlagen/` und werden per Wiki-Link referenziert. Nur 105 von 1.923 Markdown-Notizen haben bisher strukturiertes Frontmatter (Kategorie, Absender, Datum) — der Rest ist freier Text.

---

## 2. Der Dispatcher: Vollautomatische Pipeline für neue Dokumente

### Verarbeitungsablauf Schritt für Schritt

Wenn ein neues Dokument auf dem Raspberry Pi eingescannt wird, läuft folgender vollautomatischer Prozess ab:

```
① SCAN AUF PI
   Dokument wird gescannt → landet in ~/input-dispatcher/ auf dem Pi

② SYNCTHING-ÜBERTRAGUNG
   Syncthing repliziert die Datei innerhalb von ~10 Sekunden auf den Ryzen

③ DOCLING: OCR + STRUKTURIERUNG
   - PDF wird durch Docling (OCR-Engine mit ML-Layout-Analyse) verarbeitet
   - Ausgabe: strukturierter Markdown mit erhaltenen Tabellen, Überschriften, Spalten
   - Qualitäts-Gate: Texte mit < 300 Zeichen → Inbox (kein LLM-Aufwand)

④ HEADER-EXTRAKTION (regelbasiert)
   - Absender-Firma, Adresse, Datum aus dem Dokumentkopf extrahieren
   - Identifier: IBAN, Steuernummer, Part.IVA, Rechnungsnummer

⑤ ABSENDER-AUFLÖSUNG (YAML-Datenbank)
   - Abgleich mit absender.yaml (Part.IVA, USt-IdNr, Alias-Match)
   - Liefert deterministisch: Kategorie-Hint, Adressat (Reinhard/Marion/Linoa)
   - Beispiel: Part.IVA "02145060501" → Clinica Veterinaria Amiatina → familie/tierarztrechnung/Reinhard

⑥ SPRACHERKENNUNG + ÜBERSETZUNG
   - Sprache des Dokuments wird erkannt (DE/IT/EN)
   - Italienische Dokumente werden via translategemma ins Deutsche übersetzt
   - Übersetzung wird dem LLM als Kontext mitgegeben (Original-OCR bleibt erhalten)

⑦ LLM-KLASSIFIKATION (mistral-nemo:12B via Ollama)
   - Input: OCR-Text + Absender-Hint + Sprachinformation
   - Output: kategorie_id, typ_id, absender, adressat, rechnungsdatum, konfidenz
   - Halluzinatons-Guard: Nur in categories.yaml bekannte IDs werden akzeptiert

⑧ LERNREGELN-OVERRIDE (deterministisch, nach LLM)
   - Keyword-basierte Regeln überschreiben LLM-Ergebnis bei gesicherter Zuordnung
   - Beispiel: "Kontoauszug" im Text → immer finanzen/kontoauszug, Konfidenz "hoch"
   - Absender-basierte Regeln: "Apartmenthotel am Leuchtturm" → reisen/hotel_rechnung

⑨ DATENBANKSCHREIBUNG + VAULT-MOVE
   - Eintrag in SQLite: dateiname, kategorie, typ, absender, adressat, datum, konfidenz, pdf_hash
   - Datei-Umbenennung: YYYYMMDD_Absender_Typ_N.pdf
   - Vault-Routing via YAML: Ordner, Unterordner, Personen-Suffix
   - Markdown-Notiz wird angelegt mit verlinktem PDF und Frontmatter

⑩ TELEGRAM-BENACHRICHTIGUNG
   - Strukturierte Nachricht mit allen extrahierten Metadaten
   - Inline-Buttons: ✅ Korrekt | ✏️ Korrigieren | 🔄 Neu klassifizieren
   - Korrekturen werden als Lernregel gespeichert (für künftige Dokumente)
```

### Konfiguration ohne Code-Änderung

Das gesamte Routing ist über YAML-Dateien steuerbar:

- **categories.yaml:** 16 Kategorien, ~60 Dokumenttypen, je mit `vault_folder`, `vault_subfolder`, `person_subfolder`, `hints`, `telegram_template`
- **absender.yaml:** Bekannte Absender mit Part.IVA, USt-IdNr, IBAN und deterministischem Kategorie-Mapping
- **personen.yaml, doc_types.yaml:** Ergänzende Konfiguration

Neue Kategorien oder Absender werden ausschließlich durch YAML-Änderung hinzugefügt — kein Python-Code nötig.

### Bisherige Verarbeitungsleistung

- **123 Dokumente** vollständig verarbeitet (alle mit Vault-Pfad in DB)
- Aktiver Entwicklungszweig: `feature/classification-v2`
- **11 Lernregeln** bereits gespeichert (aus Telegram-Korrekturen)
- **47 bekannte Absender** in der Datenbank (inkl. Alias-Varianten)

---

## 3. Warum kein Batch-Rescan des Bestands

### Das Mengen-Problem

Ein vollständiger Rescan aller 3.246 PDFs durch Docling + Ollama würde bei realistischen 2–5 Minuten pro Dokument **108–270 Stunden** Rechenzeit bedeuten — auf dem Ryzen, ohne parallele Nutzung für andere Aufgaben. Selbst mit Optimierungen ist ein Zeitraum von mehreren Wochen realistisch.

### Das Qualitäts-Problem

Der Bestand ist inhomogen:
- **Bedienungsanleitungen, Broschüren, Kataloge:** Keine sinnvolle Klassifikation möglich
- **Handgeschriebene Notizen, schlechte Scans:** OCR-Qualität zu niedrig für LLM-Klassifikation
- **Projektdokumentation (80 Business):** Sehr individuell, Keyword-Regeln greifen nicht
- **Historische Dokumente (99 Archiv):** Oft veraltet, keine Auswertungsrelevanz

**621 Dokumente** in `00 Wiederherstellung/` haben bei einem früheren Bereinigungslauf schon versagt — schlechte OCR-Qualität macht sie auch für den Dispatcher schwierig.

### Das Kosten-Nutzen-Problem

Der Großteil des Bestands (geschätzt >60%) sind reine Ablage-Dokumente, die **nie aktiv ausgewertet werden** — Gebrauchsanleitungen, alte Reiseunterlagen, abgeschlossene Projekte. Für diese Dokumente existiert keine konkrete Auswertungsanforderung. Eine vollständige Klassifikation wäre Aufwand ohne Ertrag.

### Die Entscheidung

Der Bestand bleibt **unverändert**. Neue Dokumente durchlaufen die vollständige Pipeline. Bestandsdokumente werden **on demand** verarbeitet — wenn und nur wenn eine konkrete Auswertungsanforderung entsteht.

---

## 4. Das neue Architekturmodell: Flaches Archiv + On-Demand-Verarbeitung

### Prinzip

```
┌─────────────────────────────────────────────────────────┐
│                    OBSIDIAN VAULT                        │
│                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  Markdown   │    │    PDFs     │    │ Text Extract │  │
│  │  Notizen    │    │  (Anlagen/) │    │    Cache     │  │
│  │  1.923      │    │  3.246      │    │   846 PDFs   │  │
│  └─────────────┘    └─────────────┘    └─────────────┘  │
│                              ↑                           │
│              OmniSearch (Port 51361)                     │
│              Volltextsuche über alle Inhalte             │
└─────────────────────────────────────────────────────────┘
         ↓ Suchergebnisse (JSON: path, score, excerpt)
┌─────────────────────────────────────────────────────────┐
│               DISPATCHER (On-Demand-Modus)               │
│                                                          │
│  1. OCR: Text-Extractor-Cache → Docling (Fallback)       │
│  2. Klassifikation: Ollama mistral-nemo:12B              │
│  3. Ausgabe: Metadaten, Summen, Export                   │
└─────────────────────────────────────────────────────────┘
```

### Drei Betriebsmodi des Dispatchers

**Modus 1: Inbox (heute, unverändert)**
- Dateisystem-Watcher auf `~/input-dispatcher/`
- Neue Scans werden automatisch verarbeitet
- OCR via Docling, Klassifikation via Ollama
- Ergebnis: DB-Eintrag + Vault-Move + Telegram

**Modus 2: Batch (neu)**
- Dispatcher erhält eine Liste von Dateipfaden (via CLI oder API)
- Verarbeitung wie Inbox-Modus, aber für bestehende Vault-Dokumente
- OCR-Quelle: zuerst Text-Extractor-Cache prüfen → wenn Qualität < 300 Zeichen → Docling
- Trigger: manuell, via Telegram-Befehl, oder via OmniSearch-Ergebnis

**Modus 3: Query (Zukunft)**
- OmniSearch-API liefert Trefferliste zu einem Suchbegriff
- Dispatcher verarbeitet alle Treffer automatisch
- Ausgabe ist strukturierte Auswertung (Tabelle, Summen, CSV) statt Vault-Move

### Text Extractor als OCR-Vorrat

Text Extractor (Obsidian-Plugin) baut im Hintergrund einen OCR-Cache auf:

| Kennzahl | Wert |
|---|---|
| Cache-Speicherort | `<vault>/.obsidian/plugins/text-extractor/cache/` |
| Gesamt Cache-Einträge | 2.460 JSON-Dateien |
| Davon PDFs | 846 (26% der 3.246 PDFs) |
| Verwertbarer Text (>50 Zeichen) | 695 PDFs (82% der gecachten PDFs) |
| Faktisch leer (<50 Zeichen) | 151 PDFs (18% der gecachten PDFs) |
| Konfigurierte Sprachen | DE, IT, EN (via `useSystemOCR: true`) |
| Bekanntes Problem | Unter Linux werden DE/IT-Dokumente häufig als EN eingestuft |

Der Cache wird **nicht** synchronisiert — die bestehende `.stignore` schließt den gesamten `.obsidian/`-Ordner aus. Der Ryzen ist die einzige produktive OCR-Quelle (alle PDFs liegen dort), der Cache-Reader-Service liest direkt von dort. Jede andere Maschine baut bei Bedarf einen eigenen, lokalen Cache auf.

---

## 5. Welche Bestandsdokumente sind sofort klassifizierbar?

### Analyse des vorhandenen Caches

Von den 1.744 Text-Extractor-Cache-Einträgen mit verwertbarem Text lassen sich durch einfachen Keyword-Abgleich (entspricht den Keyword-Regeln des Dispatchers) folgende Hochkonfidenz-Treffer identifizieren:

| Dokumenttyp | Erkannte Dokumente | Konfidenz | Basis |
|---|---|---|---|
| Handwerker / Immobilien-Rechnung | ~60 | Hoch | Keywords: Handwerker, Reparatur, Acquedotto, Fognaria |
| Arztrechnung / medizinische Leistung | ~57 | Hoch | Keywords: GOÄ, Liquidation, Dr. med., Honorarrechnung |
| Steuerbescheid | ~47 | Hoch | Keywords: Steuerbescheid, Finanzamt, Agenzia delle Entrate |
| Kaufvertrag / Grundbuch | ~42 | Hoch | Keywords: Kaufvertrag, Rogito, Visura Catastale, Notar |
| Mietvertrag | ~27 | Hoch | Keywords: Mietvertrag, Kaltmiete, Mietzins |
| Hotel-Buchung | ~22 | Hoch | Keywords: Booking.com, Buchungsbestätigung, Agriturismo |
| KV-Leistungsabrechnung | ~22 | Hoch | Keywords: HUK-COBURG, Leistungsabrechnung, Erstattung |
| Darlehensvertrag | ~11 | Hoch | Keywords: Annuitätendarlehen, Tilgungsplan, Baufinanzierung |
| Gehaltsabrechnung | ~4 | Hoch | Keywords: Bruttolohn, Nettolohn, Lohnabrechnung |
| KFZ-Versicherung | ~4 | Hoch | Keywords: HUK24, KFZ-Versicherung |
| Sonstige | ~212 (gesamt) | — | Schätzung nach ~30% Überlappungsbereinigung |

**Fazit:** Aus dem bereits vorhandenen Text-Extractor-Cache könnten **ca. 200–250 Dokumente** sofort mit hoher Konfidenz klassifiziert werden — ohne erneuten OCR-Lauf durch Docling. Das entspricht ~28% der gecachten PDFs.

**Einschränkungen dieser Schätzung:**
- Basiert nur auf den 846 bereits gecachten PDFs (26% des Bestands)
- Italienische Texte sind im Cache oft in schlechter Qualität (Tesseract-Sprachproblem)
- Keine Deduplizierung mit bereits verarbeiteten 123 Dokumenten

### Was nicht automatisch klassifiziert werden kann

- **Bedienungsanleitungen, Broschüren:** Keine strukturierten Metadaten, kein Keyword-Treffer
- **Business-Korrespondenz (80 Business, 292 Notizen):** Sehr individuell
- **Dokumente in 99 Archiv (252 Notizen):** Absichtlich nicht klassifiziert/veraltet
- **621 OCR-Stubs** in `00 Wiederherstellung/`: Schlechte OCR-Qualität, Text zu kurz

---

## 6. Vault-Abfragen via Telegram und OpenClaw

### Die Herausforderung

Telegram auf dem Smartphone hat stark eingeschränkte Display-Möglichkeiten:
- Kurze Nachrichten (keine langen Tabellen)
- Keine Tastatur für komplexe Eingaben
- Inline-Buttons (bis zu ~8 pro Nachricht sinnvoll nutzbar)
- Keine Maus, kein Multi-Window

Gleichzeitig soll der Vault von unterwegs abfragbar und Auswertungen anstoßbar sein.

### OmniSearch als Basis

OmniSearch stellt auf Port 51361 eine HTTP-API bereit:

```
GET http://localhost:51361/search?q=<suchbegriff>

Rückgabe (JSON-Array):
[
  {
    "score": 0.92,
    "path": "40 Finanzen/2025/...",
    "basename": "20250715_Handwerker_Rechnung.md",
    "excerpt": "...Rechnungsbetrag 1.240,00 EUR...",
    "matches": [{"offset": 42, "match": "Handwerker"}]
  },
  ...
]
```

Diese API ist auf dem Ryzen intern erreichbar. Ein Telegram-Bot-Endpunkt auf dem Ryzen kann Suchanfragen entgegennehmen, OmniSearch abfragen und Ergebnisse kompakt zurückgeben.

### Telegram-Workflow mit eingeschränktem Display

**Schritt 1: Suchanfrage (Freitext)**
```
Nutzer → Telegram: /suche Handwerker Seggiano 2025
Bot → OmniSearch API → Trefferliste
Bot → Telegram: Kompakte Antwort (max. 5 Treffer auf einmal)

  📂 Vault-Suche: "Handwerker Seggiano 2025"
  Gefunden: 12 Dokumente

  1. 20250312_Ferroli_Rechnung.pdf (Score: 0.94)
     "...Heizungswartung Podere dei venti..."
  2. 20250601_Bonifica_Amiata_Rechnung.pdf (Score: 0.88)
     "...Entsorgung Seggiano..."
  3. 20250715_Elettricista_Rechnung.pdf (Score: 0.85)
     "...Elektroinstallation..."

  [Mehr anzeigen] [Alle verarbeiten] [Abbrechen]
```

**Schritt 2: Auswertung anstoßen (Inline-Buttons)**
```
Nutzer → [Alle verarbeiten] oder [1,2,3 verarbeiten]
Bot → Dispatcher Batch-Modus → verarbeitet 12 Dokumente
Bot → Telegram: Fortschritt + Ergebnis

  ✅ Verarbeitung abgeschlossen (12/12)
  Gesamt: 8.420,00 EUR (geschätzt)
  Zeitraum: Jan–Jul 2025

  [Detaillierte Tabelle] [Als CSV] [In Vault speichern]
```

**Schritt 3: Ergebnis (kompakt)**
```
  📊 Handwerker Seggiano 2025
  ─────────────────────────────
  Ferroli GmbH       1.240 EUR  03/2025
  Bonifica Amiata      980 EUR  06/2025
  Elettricista Russo   650 EUR  07/2025
  + 9 weitere...
  ─────────────────────────────
  Gesamt: 8.420 EUR

  [Vollständige Liste per Email] [Zurück]
```

### OpenClaw als Gateway auf dem Pi

OpenClaw läuft auf dem Raspberry Pi ("Wilson") und dient als Relay-Punkt:
- Nimmt Telegram-Befehle entgegen
- Leitet Suchanfragen an den Ryzen weiter (HTTP-Request via LAN)
- Puffert Ergebnisse bei schlechter Verbindung
- Kann einfache Status-Abfragen selbst beantworten (Pi-Status, Syncthing, Cron-Jobs)

**Sinnvolle Befehle für Telegram (eingeschränktes Display):**

| Befehl | Funktion |
|---|---|
| `/suche <Begriff>` | Volltextsuche im Vault, kompakte Liste |
| `/kategorie <Name>` | Alle Dokumente einer Kategorie |
| `/auswertung <Begriff> <Jahr>` | Suchen + Beträge summieren |
| `/status` | Pipeline-Status, letzte Verarbeitungen |
| `/inbox` | Aktueller Inhalt der Inbox |
| `/verarbeite <Pfad>` | Einzelnes Dokument on demand klassifizieren |

**Einschränkungen, die berücksichtigt werden müssen:**
- Telegram-Nachrichten sind auf 4.096 Zeichen begrenzt
- Tabellen werden auf Smartphone-Screens schlecht dargestellt → bevorzugt: nummerierte Listen
- Für längere Auswertungen: Ergebnis als Datei senden (PDF/CSV) statt als Nachricht

---

## 7. Welche Ollama-Modelle werden benötigt?

Auf dem Ryzen laufen aktuell folgende Modelle via Ollama:

| Modell | Größe | Aufgabe im Projekt |
|---|---|---|
| **mistral-nemo:12B** | 7,1 GB | **Primäres Klassifikationsmodell** — Kategorie, Typ, Absender, Adressat, Datum aus OCR-Text |
| **translategemma:latest** | 3,3 GB | **Übersetzung** — IT→DE vor Klassifikation, damit mistral-nemo auf deutschem Text arbeitet |
| **vault-assistant:latest** | 4,7 GB | **Custom-Modell für Vault-Abfragen** — fine-tuned für Obsidian-Vault-Kontext |
| **llama3.1:8B** | 4,9 GB | Backup-Klassifikator / Fallback |
| **mxbai-embed-large** | 669 MB | Embedding-Modell für semantische Suche (Qdrant) |
| **nomic-embed-text** | 274 MB | Embedding-Modell (leichtgewichtig, für schnelle Suchen) |
| **qwen2.5:7b** | 4,7 GB | Allgemeines LLM, Wissensabfragen |
| **qwen2.5:7b-enzyme** | 4,7 GB | Angepasste Variante für Enzyme-Vault-Suche |

**Für das neue On-Demand-Architekturmodell werden mindestens benötigt:**

- **mistral-nemo:12B** — unverzichtbar für Klassifikation
- **translategemma** — unverzichtbar für italienische Dokumente
- **mxbai-embed-large** oder **nomic-embed-text** — für semantische Suche falls OmniSearch-Volltextsuche nicht ausreicht

**Optionale Ergänzung für Auswertungen:**
- Ein **Summarization-Modell** (z.B. llama3.2:3b für schnelle Batch-Auswertungen) könnte sinnvoll sein, wenn Betragsextraktion aus vielen Dokumenten gleichzeitig erfolgen soll

---

## 8. Architektur-Entscheidungen

### Entscheidung 1: OCR-Quelle im On-Demand-Modus

**Beschluss: Hybrid mit Qualitäts-Gate**

Bei On-Demand-Verarbeitung (Batch- und Query-Modus) wird zuerst der Text-Extractor-Cache befragt. Nur wenn der Cache-Eintrag fehlt oder die Qualität unter einer definierten Schwelle liegt, wird Docling ausgeführt.

**Entscheidungslogik:**
```
IF cache_entry_exists AND len(text) >= 500 AND detected_language matches expected
    USE cache_entry
ELSE
    RUN docling_ocr
```

**Begründung:**
- 82% der gecachten PDFs (695 von 846) haben verwertbaren Text — dieser Anteil soll genutzt werden
- Cache-Lookups sind in Millisekunden, Docling benötigt 2–5 Minuten pro Dokument
- Das bestehende OCR-Qualitäts-Gate des Dispatchers (>300 Zeichen) wird auf 500 Zeichen angehoben, weil Cache-Text häufig schlechter strukturiert ist als Docling-Output
- Sprach-Validierung schützt vor falscher Tesseract-Erkennung bei DE/IT-Dokumenten

**Override-Option:** CLI-Parameter `--force-docling` erzwingt erneutes OCR, z.B. für Qualitätsstichproben oder strukturierte Auswertungen (Tabellenerhalt).

### Entscheidung 2: Programmatische Suche

**Beschluss: Eigener Cache-Reader-Service mit SQLite FTS5 — statt OmniSearch-Daemon**

Es wird ein neuer Docker-Container aufgesetzt, der den Text-Extractor-Cache direkt liest und als HTTP-Service bereitstellt.

**Spezifikation:**
- **Name:** `cache-reader`
- **Port:** 8501 (frei, konfliktfrei)
- **Technologie:** Python (FastAPI) + SQLite mit FTS5-Extension
- **Endpunkte:**
  - `GET /search?q=<term>&limit=<n>` → JSON-Array mit Treffern (path, score, excerpt)
  - `GET /file?path=<vault_path>` → Vollständiger Cache-Text eines Dokuments
  - `GET /stats` → Indexgröße, Coverage, letzte Aktualisierung
- **Aktualisierung:** Inkrementell via File-Watcher auf Cache-Verzeichnis

**Begründung:**
- OmniSearch läuft nur in aktiver Obsidian-Instanz — für einen Server-Dienst auf dem Ryzen fragil
- SQLite FTS5 ist produktionsreif, schnell (<100ms auf 10.000 Dokumenten) und hat keine Runtime-Abhängigkeiten
- Cache-Format (JSON mit `path` + `text`) ist trivial zu indexieren
- OmniSearch bleibt **parallel verfügbar** für interaktive Nutzung in Obsidian (Mac + Ryzen UI)

**Konsequenz:** Die Angabe "OmniSearch HTTP-API Port 51361" aus früheren Dokumentversionen wird durch den eigenen `cache-reader` auf Port 8501 ersetzt. OmniSearch bleibt als UI-Tool erhalten.

### Entscheidung 3: 621 OCR-Stubs in `00 Wiederherstellung/`

**Beschluss: Lazy Re-OCR on demand**

Die Stubs bleiben unverändert im Ordner liegen. Eine Batch-Re-OCR wird nicht durchgeführt.

**Umgang mit Stubs:**
- Wenn eine Suchanfrage einen Stub als Treffer liefert, bietet der Telegram-Bot einen Button "🔄 Re-OCR versuchen" an
- Der Button triggert Dispatcher-Batch-Modus mit `--force-docling` auf genau diese Datei
- Bei Erfolg: Stub wird durch vollständige Klassifikation ersetzt

**Begründung:**
- 621 × 2–5 Min = 20–50 Std. Rechenzeit ohne garantierten Erfolg (Stubs sind schon einmal OCR-gescheitert)
- Lazy-Ansatz fokussiert Aufwand auf Dokumente mit tatsächlicher Verwendungsabsicht
- Stubs sind durch Frontmatter-`todos:` markiert und bleiben auffindbar

### Entscheidung 4: Syncthing-Isolation

**Beschluss: Kompletter `.obsidian/`-Ausschluss beibehalten (Ist-Zustand bestätigt)**

Die bereits existierende `.stignore` schließt `.obsidian/` vollständig aus der Synchronisation aus. Diese Entscheidung wird **nicht revidiert** — jede Maschine pflegt ihren eigenen Obsidian-State, einschließlich Plugin-Caches.

**Bestehende `.stignore` (unverändert):**
```
.DS_Store
**/.DS_Store
*.pdf.md
**/*.pdf.md
.obsidian/
.enzyme/
.enzyme-embeddings/
```

**Was jede Maschine einzeln hält:**
- Text-Extractor-Cache (OCR-Ergebnisse)
- OmniSearch-Index
- Plugin-Einstellungen und Community-Plugin-Liste
- Workspace-Layouts

**Begründung:**
- Ursprüngliche Annahme (geteilter Cache ohne Konflikte) war optimistisch — OCR-Läufe auf beiden Maschinen hätten `.sync-conflict-*`-Dateien erzeugen können
- Cache-Reader-Service läuft ohnehin nur auf dem Ryzen und greift direkt auf den Ryzen-Cache zu
- Der Ryzen ist die einzige produktive OCR-Quelle (alle PDFs liegen dort)
- Mac kann bei Bedarf eigenständig einen eigenen Cache aufbauen (für interaktive OmniSearch-Nutzung im UI), dieser beeinflusst die Ryzen-Pipeline nicht

**Konsequenz für Cache-Reader-Service:** Liest ausschließlich `/syncthing/data/reinhards-vault/.obsidian/plugins/text-extractor/cache/` auf dem Ryzen — keine Merge-Logik mit Mac-Daten nötig.

### Entscheidung 5: Ollama-Modelle

**Beschluss: Aktuelle Modell-Auswahl bleibt, keine neuen Modelle notwendig**

Für das neue Architekturmodell sind alle benötigten Modelle bereits installiert:

| Zweck | Modell | Status |
|---|---|---|
| Primäre Klassifikation | mistral-nemo:12B | Im Einsatz |
| Übersetzung IT→DE | translategemma:latest | Im Einsatz |
| Fallback-Klassifikation | llama3.1:8B | Verfügbar |
| Semantische Suche | mxbai-embed-large | Verfügbar, optional |

Ein dediziertes Summarization-Modell ist **nicht** erforderlich — mistral-nemo:12B übernimmt sowohl Klassifikation als auch Batch-Auswertungen mit akzeptabler Performance.

**Begründung:**
- Alle KI-Aufgaben sind durch vorhandene Modelle abgedeckt
- Zusätzliche Modelle erhöhen nur Speicher- und Wartungsaufwand
- Embeddings (mxbai, nomic) sind bereits für Enzyme/Qdrant installiert und bei Bedarf für semantische Suche einsetzbar

---

## 9. Umsetzungsplan

### Übersicht

Die Umsetzung gliedert sich in fünf Phasen mit einem geschätzten Gesamtaufwand von **ca. 11 Entwicklungstagen**, realistisch verteilt über **3–4 Wochen**. Jede Phase liefert einen eigenständig nutzbaren Zustand — kein "Big Bang"-Release.

| Phase | Inhalt | Backend | Dashboard | Gesamt |
|---|---|---|---|---|
| 0 | Strategie-Festlegung | 30 Min | — | 30 Min |
| 1 | Infrastruktur (Syncthing, Cache-Reader) | 2 Tage | +1 Tag (Cache-Dashboard + Nav) | 3 Tage |
| 2 | Dispatcher Batch-Modus | 3 Tage | +0,5 Tag (Batch + Pipeline-Update) | 3,5 Tage |
| 2.5 | Duplikat-Erkennung | 1,5 Tage | +0,5 Tag (Duplikate + Anlagen-Update) | 2 Tage |
| 2.6 | Frontmatter-Vereinheitlichung | 1 Tag | +0,5 Tag (Validator + Vault-Update) | 1,5 Tage |
| 2.7 | Interaktive Klassifikation via Telegram | 1,5 Tage | +0,25 Tag (Review-Update) | 1,75 Tage |
| 2.8 | Admin-Web-Interface | 3 Tage | (bereits enthalten) | 3 Tage |
| 3 | Telegram-Bot-Erweiterung | 3 Tage | +0,25 Tag (Wilson-Update) | 3,25 Tage |
| 4 | Standard-Auswertungs-Templates | 2 Tage | +0,5 Tag (Auswertungs-Dashboard) | 2,5 Tage |
| 5 | Monitoring + Home-Konsolidierung | 1 Tag | +0,5 Tag (Home + Nav-Finalisierung) | 1,5 Tage |

**Gesamtaufwand aktualisiert: ~22 Entwicklungstage**

### Vorgehen: Schritt-für-Schritt mit Test-Gates

Jede Phase endet mit einem **expliziten Test-Gate**. Die nachfolgende Phase startet erst, wenn die Abnahmekriterien des Test-Gates erfüllt und vom User freigegeben sind. Keine parallelen Phasen, kein "Big Bang"-Release.

**Test-Gate-Struktur pro Phase:**
1. Automatisierte Tests (unit + integration) — grün
2. Manuelle Stichprobe im Dashboard — sichtbar und korrekt
3. End-to-End-Test mit Echtdaten (kleine Menge) — erwartetes Verhalten
4. **User-Freigabe erforderlich** bevor nächste Phase startet
5. Bei Rückmeldungen/Nachbesserungen: aktuelle Phase wird verlängert, nicht nächste vorgezogen

---

### Phase 0: Strategie-Festlegung ✅ ABGESCHLOSSEN am 2026-04-19

**Ziel:** Die Entscheidung "kein Batch-Rescan" wird verbindlich dokumentiert und kommuniziert.

**Aufgaben und Status:**
- [x] Batch-Rescan-Task aus `project_aufgaben_morgen.md` entfernen / als "verworfen" markieren — Datei neu geschrieben, nur historische Referenzen (durchgestrichen) erhalten
- [x] Neue Architektur in Memory-Index eintragen (`project_flaches_archiv.md`) — Eintrag in `MEMORY.md` aktiv, Memory-Datei 2.219 Bytes
- [x] Dieses Dokument als Referenzarchitektur speichern: `/home/reinhard/docker/docling-workflow/ARCHITEKTUR.md` — 70 KB, inhaltsgleich (via `diff -q` verifiziert)
- [x] Dummy-Eintrag `__rescan_skip__` aus DB entfernen — war bereits nicht mehr vorhanden (0 Artefakte)

**Test-Gate Phase 0 (verifiziert 2026-04-19):**

| # | Test | Prüfung | Ergebnis |
|---|---|---|---|
| 1 | Keine aktiven Rescan-Aufgaben in Memory | `grep -iE "rescan" project_aufgaben_morgen.md` | ✅ Nur durchgestrichene Historien-Einträge |
| 2 | `ARCHITEKTUR.md` inhaltsgleich mit Quelle | `diff -q projekt_beschreibung.md ARCHITEKTUR.md` | ✅ Identisch |
| 3 | Memory-Index verweist auf neue Strategie | `grep flaches_archiv MEMORY.md` | ✅ Eintrag vorhanden |
| 4 | DB frei von Rescan-Artefakten | Python-Filter `dateiname.startswith('__')` | ✅ 0 Artefakte in Tabelle `dokumente` |

**Ergänzender Befund:**
- `dispatcher.py` enthält keine Hardcodes für `rescan_skip` oder `__rescan_` (0 Grep-Treffer)
- Die Routen `/api/rescan/start*` im Dispatcher-Dashboard bestehen weiterhin — Entfernung erfolgt in **Phase 2 (Pipeline-Dashboard-Update)**, nicht in Phase 0

**User-Freigabe Phase 0:** 2026-04-19 — Phase 1 kann starten

---

### Phase 1: Infrastruktur ✅ ABGESCHLOSSEN am 2026-04-19

**Ziel:** Cache-Reader-Service läuft als Docker-Container und ist via HTTP erreichbar, inklusive Dashboard mit Live-Suche.

**Status nach Umsetzung:** Alle Teilaufgaben erledigt, Test-Gate bestanden, User-Freigabe erteilt. Der Service läuft produktiv auf Port 8501, das Dashboard ist unter `/cache` erreichbar.

#### 1.1 Syncthing-Isolation ✅ BEREITS VORHANDEN (2026-04-19)

Die bestehende `.stignore` im Vault schließt den gesamten `.obsidian/`-Ordner aus. Damit ist die Isolation vollständig — **keine Änderung nötig**.

**Ist-Zustand `/syncthing/data/reinhards-vault/.stignore`:**
```
.DS_Store
**/.DS_Store
*.pdf.md
**/*.pdf.md
.obsidian/
.enzyme/
.enzyme-embeddings/
```

**Konsequenz für Phase 1:** Der Cache-Reader-Service greift direkt auf `/syncthing/data/reinhards-vault/.obsidian/plugins/text-extractor/cache/` auf dem Ryzen zu. Der Mac hält optional einen eigenen Cache — ohne Auswirkung auf die Ryzen-Pipeline.

**Aufgabe 1.1 entfällt ohne Änderung** → direkt weiter zu 1.2.

#### 1.2 Cache-Reader-Service ✅ UMGESETZT

##### Warum wir diesen Service bauen

Der Text-Extractor (Obsidian-Plugin) erzeugt für jedes PDF eine JSON-Datei mit extrahiertem OCR-Text. Aktuell liegen **846 solcher JSON-Dateien** im Cache (`~/.obsidian/plugins/text-extractor/cache/`). Diese sind von Obsidian aus durchsuchbar (via OmniSearch), **aber nicht programmatisch von anderen Diensten auf dem Ryzen erreichbar** — weil OmniSearch nur innerhalb von Obsidian läuft.

Der Cache-Reader-Service schließt diese Lücke: Er liest die vorhandenen JSON-Dateien, baut einen eigenen Volltextindex auf und stellt ihn als HTTP-API bereit. Damit können **Dispatcher, Telegram-Bot, Batch-Skripte und künftige Auswertungs-Templates** den Cache abfragen, ohne dass Obsidian laufen muss.

##### Was der Service konkret leistet

Der Service besteht aus drei Komponenten, die zusammen laufen:

**Komponente 1: `indexer.py` — Einmalige und inkrementelle Indexierung**

- **Aufgabe:** Liest alle `*.json`-Dateien aus dem Text-Extractor-Cache und schreibt den Inhalt in eine SQLite-Datenbank mit FTS5-Volltextindex.
- **Was es liest:** Jede JSON-Datei hat die Struktur:
  ```json
  {
    "path": "40 Finanzen/2025/Rechnung_Ferroli.pdf",
    "text": "Rechnung Nr. 2025-0312 ... Heizungswartung ... 1.240 EUR",
    "libVersion": "0.5.0",
    "langs": "deu"
  }
  ```
- **Was es schreibt:** Einen Datensatz in die FTS5-Tabelle `documents`:
  ```
  path     → "40 Finanzen/2025/Rechnung_Ferroli.pdf"
  text     → "Rechnung Nr. 2025-0312 ... Heizungswartung ... 1.240 EUR"
  langs    → "deu"
  mtime    → Zeitstempel der JSON-Datei
  ```
- **FTS5** (SQLite Full-Text Search) baut automatisch einen invertierten Index auf: Jedes Wort wird auf die Dokumente gemappt, in denen es vorkommt. Suchanfragen wie "Ferroli 2025" laufen in <50 ms auch bei tausenden Dokumenten.
- **Erstlauf:** Indexiert alle 846 Dateien auf einmal (ca. 30 Sek.)
- **Inkrementelle Läufe:** Prüft nur geänderte JSON-Dateien (anhand des `mtime`-Zeitstempels) — spart Zeit bei Updates

**Komponente 2: `watcher.py` — Automatische Updates**

- **Aufgabe:** Beobachtet kontinuierlich das Text-Extractor-Cache-Verzeichnis und triggert `indexer.py` bei Änderungen.
- **Technologie:** `watchdog`-Python-Library, die Dateisystem-Events (Erstellen, Ändern, Löschen) über den Linux-Kernel (`inotify`) empfängt.
- **Ablauf:** Sobald Text Extractor eine neue JSON schreibt (weil ein neues PDF im Vault gecacht wurde), wird der Eintrag binnen Sekunden auch im Cache-Reader-Index aufgenommen.
- **Debouncing:** Mehrfach-Änderungen derselben Datei werden zusammengefasst, um unnötige Re-Indexierungen zu vermeiden.

**Komponente 3: `api.py` — HTTP-Schnittstelle**

Eine kleine FastAPI-Anwendung mit vier Endpunkten:

| Methode | Endpoint | Parameter | Rückgabe | Zweck |
|---|---|---|---|---|
| GET | `/search` | `q` (Suchbegriff), `limit` (max. Treffer, Default 10) | JSON-Liste mit `path`, `score`, `excerpt`, `matches` | Volltextsuche für Dispatcher, Telegram-Bot, Skripte |
| GET | `/file` | `path` (Vault-relativer Pfad) | Vollständiger OCR-Text des Dokuments | Dispatcher holt Text bei Hybrid-OCR statt neu-OCR via Docling |
| GET | `/stats` | — | Indexgröße, Coverage-%, letzte Aktualisierung, Fehler | Wird vom `/cache`-Dashboard angezeigt |
| POST | `/reindex` | — | Neu-Indexierung-Job startet | Fallback, wenn File-Watcher Änderungen verpasst |

##### Beispiel: Was passiert bei einer Suchanfrage?

```
1. Telegram-Bot erhält /suche Ferroli Heizung
2. Bot sendet: GET http://cache-reader:8501/search?q=Ferroli+Heizung&limit=10
3. Cache-Reader führt SQL-FTS5-Query aus: SELECT ... FROM documents WHERE documents MATCH ?
4. FTS5 liefert sortierte Trefferliste (nach BM25-Score)
5. Cache-Reader antwortet mit JSON:
   [
     {"path": "40 Finanzen/...", "score": 0.94, "excerpt": "...Rechnung Ferroli..."},
     {"path": "50 Immobilien.../...", "score": 0.88, "excerpt": "...Heizungswartung..."}
   ]
6. Bot formatiert für Telegram-Display und antwortet dem Nutzer
```

Kein Obsidian läuft dabei. Kein LLM wird aufgerufen. Die Antwort kommt in <100 ms.

##### Verzeichnisstruktur und Dateien

```
/home/reinhard/docker/docling-workflow/
└── cache-reader/
    ├── Dockerfile              # Python 3.12 Base Image, Dependencies
    ├── requirements.txt        # fastapi, uvicorn, watchdog (SQLite ist Standard in Python)
    ├── src/
    │   ├── indexer.py          # Scan Cache → FTS5
    │   ├── api.py              # FastAPI mit 4 Endpunkten
    │   ├── watcher.py          # Auto-Update via watchdog/inotify
    │   └── config.py           # Pfade, Port, Log-Level
    └── data/
        └── index.db            # SQLite-Datei, ca. 5–10 MB bei 846 Dokumenten
```

##### Integration in `docker-compose.yml`

```yaml
services:
  cache-reader:
    build: ./cache-reader
    container_name: cache-reader
    ports:
      - "8501:8501"
    volumes:
      - ./syncthing/data/reinhards-vault/.obsidian/plugins/text-extractor/cache:/vault-cache:ro
      - ./cache-reader/data:/data
    environment:
      - CACHE_DIR=/vault-cache
      - INDEX_DB=/data/index.db
      - LOG_LEVEL=info
    restart: unless-stopped
```

**Wichtige Volume-Eigenschaften:**
- `/vault-cache:ro` → **read-only**, der Service kann den Cache nur lesen, nie verändern. Das schützt die Obsidian-Daten vor versehentlicher Manipulation.
- `/data` → read-write, hier lebt ausschließlich der eigene Index.

##### Was gibt es **nicht** zu tun

Dieser Service **ersetzt nicht** OmniSearch in Obsidian. Er ist eine **parallele Zugangsschicht** für alle Systeme außerhalb von Obsidian. Der Nutzer kann weiterhin OmniSearch in der Obsidian-UI verwenden — die beiden Systeme beeinflussen sich nicht.

Der Service **klassifiziert nichts**. Er liest nur bereits vorhandene OCR-Texte und macht sie suchbar. Klassifikation (Kategorie, Absender, Datum) passiert weiterhin im Dispatcher mit Ollama.

##### Umsetzungsschritte

- [ ] `cache-reader/`-Verzeichnisstruktur anlegen
- [ ] `requirements.txt` schreiben (fastapi, uvicorn, watchdog)
- [ ] `config.py`: Pfade, Konfigurations-Defaults
- [ ] `indexer.py`: Scan-Logik + FTS5-Tabellen-Schema
- [ ] `api.py`: FastAPI mit 4 Endpunkten
- [ ] `watcher.py`: File-Watcher mit Debouncing
- [ ] `Dockerfile`: Schlankes Python-Image mit nur notwendigen Libraries
- [ ] `docker-compose.yml` erweitern: `cache-reader`-Service mit Volumes und Port 8501
- [ ] Container bauen und starten, Erstindexierung ausführen
- [ ] Manueller Test via `curl http://localhost:8501/search?q=...`

#### 1.3 Dashboard-Integration ✅ UMGESETZT

**Neues Dashboard `/cache`:**
- Coverage-Statistik: welche Vault-PDFs sind indiziert, wie viele haben verwertbaren Text
- Qualitätsverteilung (Text-Länge-Histogramm, Sprach-Erkennung)
- Live-Suchtester: Eingabefeld → Treffer-Vorschau (nutzt Cache-Reader-API direkt)
- Re-Index-Button (manuell, bei Zweifel an File-Watcher)
- Fehlerliste: Cache-Einträge mit leerem Text oder falscher Sprache

**Haupt-Dashboard `/` — Ergänzung:**
- Neue Kachel "Cache-Coverage: X% der Vault-PDFs"
- Strategie-Hinweis oben: "Flaches Archiv + On-Demand-Verarbeitung aktiv"

**Neue Top-Navigation** (konsistent auf allen bestehenden und neuen Dashboards):
```
[📊 Home] [🔄 Pipeline] [📝 Review] [📂 Vault] 
[📎 Anlagen] [🔍 Cache] [🥧 Wilson]
```
Weitere Navigations-Einträge werden in späteren Phasen ergänzt.

#### 1.4 Test-Gate Phase 1 ✅ BESTANDEN (2026-04-19)

**Automatisiert:**
- [x] Erstindexierung: **2.460 Cache-Einträge in 19,6 Sek.** (inkl. langdetect), weit unter 30-Sek.-Ziel
- [x] Such-Performance: **100 Anfragen in 0,63 Sek.** (Mittel 6,3 ms/Anfrage — Ziel war <50 ms)
- [x] Inkrementelles Update: File-Watcher reagiert via `watchdog`+`inotify` binnen Sekunden
- [x] Syncthing-Isolation: `.obsidian/` komplett ausgeschlossen, keine Änderung nötig

**Manuell im Dashboard:**
- [x] `/cache`-Dashboard zeigt Stats (indiziert / verwertbar / leer / Sprachen / letzte Reindex)
- [x] Live-Suche liefert für "Ferroli", "HUK Leistungsabrechnung", "Kaufvertrag" passende Treffer mit Snippets und BM25-Score
- [x] Re-Index-Button läuft ohne Fehler durch, Stats aktualisieren sich automatisch

**End-to-End:**
- [x] `curl http://localhost:8501/search?q=...` liefert JSON mit plausiblen Treffern
- [x] Proxy-Weiterleitung Dispatcher → cache-reader funktioniert (`/api/cache/*`)
- [x] Klickbare Treffer: Titel öffnet PDF, Excerpt öffnet Volltext-Modal
- [x] Stale-Pfad-Detection: Cache-Einträge ohne Datei im Vault werden grau markiert mit "veraltet"-Badge

#### 1.5 Zusätzliche Erweiterungen über die Planung hinaus

Während der Umsetzung wurden drei Ergänzungen nötig/sinnvoll:

**A) langdetect-basierte Spracherkennung**
Die vom Text Extractor vergebene `langs`-Angabe war unzuverlässig (alles als `eng` oder leer). Neu im Indexer: eigene Sprach-Detection via `langdetect` aus dem OCR-Text.

Ergebnis der Neuindexierung:
- Deutsch: 0 → **703**
- Italienisch: 0 → **108**
- Englisch: 1.613 → **481** (realistischer Wert)
- Unknown: 846 → **1.128** (meist OCR-Müll aus Bildern)

Wichtige Vorarbeit für **Phase 2 Hybrid-OCR**, wo Sprach-Validierung über Cache-Nutzung entscheidet.

**B) Klickbare Suchergebnisse + Volltext-Modal**
Das ursprüngliche Dashboard-Design zeigte nur statische Trefferlisten. Erweiterung:
- Titel-Klick → PDF wird via neuem Endpoint `/api/vault-file?path=...` im Browser geöffnet
- Excerpt-Klick → Modal mit vollständigem OCR-Text + "PDF öffnen"-Button
- `Esc` oder Klick außerhalb schließt Modal

Sicherheitscheck im `/api/vault-file`-Endpoint: Path-Traversal wird via `Path.resolve().relative_to(VAULT_ROOT)` blockiert.

**C) Stale-Pfad-Visualisierung**
Der Cache enthält ca. 1.030 veraltete Pfade (Apple-Notes-Duplikate mit " 2"-Suffix). Das Search-Proxy wurde um einen `exists`-Check erweitert: jeder Treffer wird gegen das Dateisystem geprüft.

Im Dashboard:
- Aktuelle Treffer: blau + unterstrichen + klickbar
- Veraltete Treffer: grau + durchgestrichen + "veraltet"-Badge + PDF-Öffnen deaktiviert
- Macht das Ausmaß der in **Phase 2.5** zu bereinigenden Redundanz sofort sichtbar

**D) Cache-Control-Header für Dashboards**
`Cache-Control: no-store, no-cache, must-revalidate` auf allen HTML-Responses — verhindert künftig, dass der Browser veraltete Dashboard-Versionen anzeigt.

**User-Freigabe Phase 1:** 2026-04-19 — Phase 2 kann starten

**Gelieferte Artefakte (Code):**
- `cache-reader/` — neues Python-Modul mit `indexer.py`, `api.py`, `watcher.py`, `config.py`
- `cache-reader/Dockerfile`, `cache-reader/requirements.txt`
- `docker-compose.yml` — `cache-reader`-Service auf Port 8501 + `CACHE_READER_URL`-Env für Dispatcher
- `dispatcher.py` — neues `_CACHE_HTML`-Template, Routen `/cache`, `/api/cache/*`, `/api/vault-file`, Cache-Control-Header

---

### Phase 2: Dispatcher Batch-Modus

**Ziel:** Dispatcher kann Dateilisten verarbeiten, nutzt Hybrid-OCR, liefert strukturierte Ausgabe ohne Vault-Move.

**Status 2026-04-19:** Schritte 2.0 – 2.4 implementiert. Detailliertes Änderungsprotokoll siehe `PHASE2_PLAN.md`.

| Schritt | Status | Notiz |
|---|---|---|
| 2.0 Auto-Rescan entfernen | ✅ 2026-04-19 | `dispatcher.py` Auto-Batch-Block entfernt, `batch_reimport.py` als deprecated markiert |
| 2.1 CLI `--batch` | ✅ 2026-04-19 | argparse-Kopf in `main()`, `run_batch()` neu, smoke-getestet (dry-run, classify-only, structured) |
| 2.2 Hybrid-OCR-Gate | ✅ 2026-04-19 | `resolve_ocr_text()`, `HYBRID_OCR_MIN_CHARS=500`. Cache-Hit: 2942 chars/0 ms vs. 36 s Docling |
| 2.3 Ausgabeformate | ✅ 2026-04-19 | `dispatcher/batch_output.py`, CSV summary + JSONL details, `vault-move` im Batch unterdrückt |
| 2.4 Dashboard `/batch` | ✅ 2026-04-19 | `batch_runs`/`batch_items`-Tabellen, REST-API + `_BATCH_HTML`, vier Testläufe in DB (nur API verifiziert) |

**Hotfixes 2026-04-19:**
- Batch-Modus durfte keine PDFs im Archiv via Duplikat-/Hash-Check löschen → Schutz eingebaut, aus `/home/reinhard/pdf-archiv/` versehentlich entfernte Datei wiederhergestellt.
- Cache-Reader liefert `langs` als String (`"de"`), nicht als Liste → Parser erweitert.

**Direkt offen vor 2.5:**
1. Container-Rebuild + Browser-Check `/batch` (`docker compose up -d --build dispatcher`).
2. Resume-Logik für `--resume RUN_ID` (CLI-Flag reserviert, Implementierung fehlt).
3. Ollama-Stabilität: `model runner has unexpectedly stopped` im ersten structured-Lauf — Retry/Warm-up evaluieren.

#### 2.1 CLI-Erweiterung ✅ ABGESCHLOSSEN 2026-04-19

Neue CLI-Parameter für `dispatcher`:

| Parameter | Bedeutung | Default | Status |
|---|---|---|---|
| `--batch <input.json>` | Verarbeitungsliste als JSON-Datei oder Textliste | — | ✅ |
| `--ocr-source` | `cache` \| `docling` \| `hybrid` | `hybrid` | ✅ |
| `--output` | `vault-move` \| `classify-only` \| `structured` | `vault-move` | ✅ |
| `--output-dir` | Zielordner für CSV/JSONL bei `structured` | — | ✅ |
| `--limit N`, `--dry-run`, `--resume RUN_ID` | Probelauf / Teilmenge / Fortsetzung | — | ✅ CLI, ⏳ Resume-Logik offen |

**Input-Format `input.json`:**
```json
{
  "documents": [
    "Anlagen/20250312_Ferroli_Rechnung.pdf",
    "Anlagen/20250601_Bonifica_Rechnung.pdf"
  ],
  "query_context": "Handwerker Seggiano 2025",
  "export_target": "/tmp/auswertung_handwerker.csv"
}
```

**Output-Formate:**
- `vault-move`: wie heute (Datei wird umbenannt und verschoben)
- `classify-only`: nur DB-Eintrag, Originaldatei bleibt am Platz
- `structured`: CSV/JSON mit extrahierten Metadaten (keine DB-Schreibung)

#### 2.2 Hybrid-OCR-Logik ✅ ABGESCHLOSSEN 2026-04-19

- [x] Neue Funktion `resolve_ocr_text(pdf_path, mode, cache_hint)` in `dispatcher.py:7891` mit Rückgabe `(text, meta)`.
- [x] `cache_reader.get_file` via HTTP-Endpoint — Dispatcher nutzt proxy `/api/cache/file`.
- [x] Sprach-Gate: `HYBRID_OCR_MIN_CHARS=500`, Sprache muss in `{de,it,en}` liegen. Bei Miss/Gate-Fail → Docling-Fallback (`meta.source="docling_fallback"`).
- [x] Per-Lauf-Metrik: `ocr_source` in `batch_items`-Spalte, im Dashboard unter `/batch/runs/<id>` einsehbar.
- [x] Smoke-Test: Cache-Hit 2942 chars / 0 ms vs. 36 s Docling auf identischer PDF.

#### 2.3 Dashboard-Integration ✅ ABGESCHLOSSEN 2026-04-19 / 2026-04-20

**Neues Dashboard `/batch`:**
- [x] Liste aktiver Batch-Läufe mit Fortschrittsbalken (x/N verarbeitet)
- [x] Pause / Resume / Abort pro Lauf (Abort verifiziert via Run-ID 5)
- [x] Historie abgeschlossener Läufe mit CSV/JSONL-Download-Links
- [ ] Ressourcen-Anzeige (CPU / Ollama-Queue / Docling-Queue) — nicht umgesetzt, nicht blockierend

**Pipeline-Dashboard `/pipeline` — Anpassung:**
- [x] Live-Logs-Button pro Dokument: "📜 Logs" öffnet Modal mit gefiltertem Ringbuffer (5000 Zeilen, `/api/logs?q=<stem>`, Live-Poll 2 s) — ergänzt 2026-04-20
- [x] Queue-Bar erweitert um "⏳ Wartend (N)" mit orangen Chips der noch nicht verarbeiteten Dokumente (`/api/queue/state`)
- [ ] Rescan-Banner entfernen — bleibt vorerst, da Route `/api/rescan/start` noch existiert
- [ ] Kachel "OCR-Quelle" pro Schritt (Cache/Docling/Cache-Fallback mit Farbcode) — offen
- [ ] Per-Feld-Konfidenz in LLM-Kachel — offen

**Nav-Integration:**
- [x] Cache-Export-Button auf `/cache` → "▶ An Batch übergeben" → erzeugt `cache_export_*.json` in `dispatcher-temp/`, Sprung zu `/batch?input=...` mit Pre-Fill des Input-Pfads (ergänzt 2026-04-19)
- [x] Queue-Counter-Badge (orange, nur wenn >0) im Haupt-Dashboard neben "⚡ Pipeline" (ergänzt 2026-04-20)

#### 2.4 Test-Gate Phase 2

**Automatisiert:**
- [x] CLI-Test: `dispatcher --batch test_input.json --output structured` liefert CSV — verifiziert mit `cache_export_ferroli_*.json`, Run-IDs 3/4/6/7
- [x] Hybrid-OCR wählt korrekt: Cache bei ≥500 Zeichen + plausible Sprache, sonst Docling
- [x] Regressionstest: Watch-Inbox-Modus unverändert (Telegram-Pfad läuft, per `/api/queue/state` verifiziert)
- [ ] `--force-docling` explizit testen (Flag reserviert, Regressionstest steht aus)

**Manuell im Dashboard:**
- [x] `/batch`-Dashboard zeigt laufenden Batch mit Fortschritt (Run 7 live beobachtet, 0/1 → done 1/1)
- [x] Abort-Button bricht Lauf sauber ab (Run 5 per API abgebrochen, Status → `aborted`)
- [ ] `/pipeline` zeigt neue OCR-Quellen-Kachel — offen (Kachel nicht umgesetzt)

**End-to-End:**
- [ ] 20 Stichproben-PDFs: Hybrid vs. reines Docling — offen (Modellvergleich qwen2.5 vs. gemma4 ebenfalls dort zu prüfen)
- [x] CSV-Export enthält alle erwarteten Spalten, UTF-8-korrekt — verifiziert in Runs 3/4/7

**Test-Gate-Status (2026-04-20):** Funktionalität stabil, Watch-Modus produktiv mit Telegram-Eingang. Offene Punkte sind die Kür (Stichprobenvergleich, `--force-docling`-Regression, OCR-Quellen-Kachel), nicht blockierend für 2.5.

**Stabilitäts-Hotfixes 2026-04-20 (nachgezogen):**
- `OLLAMA_NUM_CTX` / `OLLAMA_TIMEOUT` als Env-Variablen konfigurierbar (Default 8192 / 300 s) — behebt `model runner has unexpectedly stopped` bei zu großem Kontext auf 2-GB-iGPU.
- `num_ctx` im Ollama-Klassifikations-Call explizit gesetzt (wurde vorher ignoriert → 4096-Default des Modells).
- Modellwechsel auf `gemma4:e4b` getestet, läuft stabil; qwen2.5:7b-Vergleich steht noch aus.
- **Inbox-DB-Eintrag** — Dispatcher schreibt jetzt auch bei fehlgeschlagener Klassifikation einen Minimal-Eintrag in `dokumente` (kategorie=NULL, konfidenz=niedrig). Vorher fehlten diese Dokumente komplett im Dashboard. Altbestand durch `reconcile_inbox_orphans.py` nachgezogen: 26 Orphans mit MD in `00 Inbox/` (Dispatcher-Bug-Opfer) in DB eingetragen, 2799 Altimporte ohne MD bewusst unberührt.

**User-Freigabe:** _______________________ (Datum) — Phase 2.5 erst danach

---

### Phase 2.5: Duplikat-Erkennung und -Management

**Ziel:** Zuverlässige Erkennung von PDF-Duplikaten sowohl bei Neuimporten als auch im Bestand. Sichere Quarantäne-Lösung statt automatischer Löschung. Transparente Kennzeichnung bei Suchanfragen.

**Ausgangslage:** Der Vault enthält aktuell 1.030 redundante PDFs in 886 Duplikat-Gruppen (MD5-basierte Messung). Der Dispatcher erkennt bereits byte-identische Duplikate beim Neuimport, aber keine Re-Scans (gleicher Inhalt, andere Bytes).

#### 2.5.1 Mehrschicht-Hash-Strategie (0,5 Tage)

**DB-Schema-Erweiterung:** Neue Spalte `text_hash` in Tabelle `dokumente`

| Hash-Schicht | Methode | Erkennt |
|---|---|---|
| 1. Byte-Hash | MD5 der PDF-Datei | Exakte Kopien (Download, Upload-Kopie) |
| 2. Text-Hash | SHA256 des normalisierten OCR-Texts | Re-Scans (unterschiedliche Bytes, gleicher Inhalt) |
| 3. (Optional, Phase 2) | MinHash / SimHash | Nahezu-Duplikate (OCR-Varianten, leichte Text-Unterschiede) |

**Text-Normalisierung vor Hash-Bildung:**
- Lowercase
- Whitespace auf einzelnes Leerzeichen kollabieren
- Sonderzeichen und Satzzeichen entfernen
- Zahlen beibehalten (Rechnungsnummer, Datum sind identifizierend)

#### 2.5.2 Dedup-Scan für Bestand (0,5 Tage)

**Neues Skript:** `/home/reinhard/docker/docling-workflow/scripts/dedup_scan.py`

Abläufe:
- [ ] MD5-Scan aller PDFs im Vault (~5 Minuten bei 3.084 PDFs)
- [ ] Text-Hash aus Text-Extractor-Cache (846 PDFs direkt, Rest später bei Bedarf)
- [ ] Gruppenbildung: PDFs mit identischem Byte- oder Text-Hash
- [ ] Auswahl der "Best-Version" nach Regelkatalog (s.u.)
- [ ] **Keine automatische Löschung** — Ausgabe ist nur ein Report

**Regel für Best-Version-Auswahl:**
1. Ist in kategorisiertem Vault-Ordner (nicht `Anlagen/`, nicht `00 Inbox/`) → bevorzugt
2. Hat längsten Pfad (= spezifischste Kategorisierung) → bevorzugt
3. Hat Markdown-Frontmatter-Verlinkung → bevorzugt
4. Ältestes Erstellungsdatum → bevorzugt (vermutlich das Original)

**Report-Format:** CSV mit Spalten
```
gruppe_id, anzahl, hash_typ, beste_version_pfad, duplikat_pfade, groesse_mb
```

**Telegram-Benachrichtigung nach Scan:**
```
📊 Duplikat-Scan abgeschlossen
  Geprüfte PDFs: 3.084
  Duplikat-Gruppen: 886
  Redundante Dateien: 1.030 (2,3 GB)

[Report ansehen] [Quarantäne vorbereiten] [Abbrechen]
```

#### 2.5.3 Quarantäne-Workflow (0,5 Tage)

**Zwei-Stufen-Lösung statt direkter Löschung:**

1. **Quarantäne:** Duplikate werden nach `00 Duplikate/<gruppe_id>/` verschoben
   - Struktur: `00 Duplikate/2026-04-19_scan-001/<originalname>.pdf`
   - Metadaten-Datei `00 Duplikate/2026-04-19_scan-001/INFO.md` mit Original-Pfaden, Hashes, Grund
   - Markdown-Verlinkungen werden **nicht automatisch** angepasst (manuelle Prüfung bei Bedarf)

2. **Endgültige Löschung:** Nach 30 Tagen Aufbewahrung via Cron-Job
   - Vorher Telegram-Erinnerung 3 Tage vor Ablauf
   - User kann Frist verlängern oder Duplikat rückholen

**Neuimport-Verhalten:**

| Szenario | Reaktion |
|---|---|
| Byte-Duplikat (MD5-Match) | Sofort nach `00 Duplikate/`, Telegram-Info |
| Text-Duplikat (OCR-Hash-Match, Bytes anders) | In Inbox halten, Telegram-Rückfrage: "Gleich wie Dokument X — Duplikat oder Re-Scan?" |
| Kein Match | Normale Verarbeitung |

#### 2.5.4 Duplikat-Warnung in Suchergebnissen

**Wenn eine Suchanfrage Dokumente trifft, die mit anderen Dokumenten einen Text- oder Byte-Hash teilen, wird dies klar kommuniziert.**

**Fall A — Treffer ist in Quarantäne:**
```
📂 3 Treffer für "Tierarzt Amiatina 2025"

1. 20250615_Clinica_Veterinaria_Amiatina.pdf  .92
   "...Behandlung Katze Milo, 187 EUR..."
2. ⚠️ 20250615_Clinica_Veterinaria_Amiatina_2.pdf  .91
   "...Behandlung Katze Milo, 187 EUR..."
   → Duplikat in Quarantäne (Gruppe 2026-04-19_scan-001)
   → Original: Treffer 1
3. 20250722_Clinica_Veterinaria_Amiatina.pdf  .88
   "...Impfung Milo, 95 EUR..."

[Nur Originale anzeigen] [Alle inkl. Duplikate]
```

**Fall B — Mehrere Treffer sind untereinander Duplikate:**
```
📂 5 Treffer für "Patientenverfügung Marion"

1. 20240818_Patientenverfügung_Marion.pdf  .95
   "...Patientenverfügung vom 18.08.2024..."
   ℹ️ 4 weitere identische Versionen in verschiedenen Ordnern
   
2. 20250201_Aktualisierung_Patientenverfügung.pdf  .82
   "...überarbeitete Fassung..."

[Duplikat-Gruppe ansehen] [Aufräumen]
```

**Fall C — Bei Auswertungen (Batch-Modus):**
```
🔍 Suche "Handwerker Seggiano 2025" — 18 Roh-Treffer
   Davon 6 Duplikate ausgefiltert
   → 12 eindeutige Dokumente zur Verarbeitung

[Duplikate anzeigen] [Weiter mit 12 Dokumenten]
```

**Technische Umsetzung:**
- Cache-Reader-Service (Phase 1) liefert pro Treffer zusätzliches Feld `duplicate_of`, `is_quarantine`, `group_id`
- Telegram-Bot reduziert Trefferliste auf Original-Versionen, zeigt Duplikat-Info nur bei Bedarf
- Batch-Modus filtert Duplikate standardmäßig aus, Logik dokumentiert in Ausgabe-Report

#### 2.5.5 Dashboard-Integration (0,5 Tage)

**Neues Dashboard `/duplikate`:**
- Dedup-Scan-Button mit Fortschrittsanzeige
- Ergebnisliste: 886 Gruppen mit Best-Version-Vorschlag, erweiterbar per Klick
- Quarantäne-Inhalt mit 30-Tage-Countdown pro Datei
- Bulk-Aktion: "Alle vorgeschlagenen Best-Versionen genehmigen" / Einzelprüfung
- Filter: nur Byte-Duplikate, nur Text-Duplikate, nur offene Gruppen

**Vault-Anlagen-Dashboard `/vault/anlagen` — Anpassung:**
- Neue Spalte "Duplikat-Gruppe" (klickbar → springt zu `/duplikate`)
- Orphan-Detektion erweitern: PDFs in `Anlagen/` ohne MD-Verlinkung als separate Liste
- Bulk-Aktion: Ausgewählte Orphans in Quarantäne verschieben

**Haupt-Dashboard `/` — Kennzahl ergänzen:**
- "Duplikate: 886 Gruppen / 1.030 redundante Dateien" (klickbar → `/duplikate`)

**Top-Navigation erweitern:** `[♻️ Duplikate]`

#### 2.5.6 Test-Gate Phase 2.5

**Automatisiert:**
- [ ] Byte-Dedup-Test: zwei Kopien derselben PDF → nur eine wird verarbeitet
- [ ] Text-Dedup-Test: gleicher Inhalt, andere Bytes → als Duplikat erkannt
- [ ] Quarantäne-Rückhol-Test: Datei aus `00 Duplikate/` kann wiederhergestellt werden
- [ ] Cron-Löschungs-Test: 30-Tage-Frist triggert Erinnerung

**Manuell im Dashboard:**
- [ ] `/duplikate` zeigt die 886 Gruppen aus Scan
- [ ] Stichprobe 3 Gruppen: Best-Version-Vorschlag ist plausibel (längster Pfad, frontmatter-verlinkt)
- [ ] Bulk-Aktion verschiebt 10 ausgewählte Duplikate korrekt in Quarantäne

**End-to-End:**
- [ ] 10 manuell kopierte PDFs werden von Dedup-Scan gefunden und in eine Gruppe zusammengefasst
- [ ] Suchanfrage findet Duplikat → Telegram zeigt ⚠️-Warnung mit Verweis auf Original

**User-Freigabe:** _______________________ (Datum) — Phase 2.6 erst danach

---

### Phase 2.6: Frontmatter-Vereinheitlichung

**Ziel:** Einheitliches, maschinenlesbares Frontmatter-Schema für Markdown-Notizen — ohne Batch-Migration des Bestands. Bestehende Legacy-Felder werden erhalten, das neue Schema kommt bei Neuimport und on-demand bei Batch-Verarbeitung zum Einsatz.

**Ausgangslage:** 95% der 1.903 MD-Dateien haben bereits Frontmatter, aber in sechs konkurrierenden Schemata (Evernote-Import, Apple-Notes-Import, Dispatcher v1, Dispatcher v2, OCR-Stubs, manuell). Das bricht Dataview-Queries und Enzyme-Filter.

#### 2.6.1 Unified Minimal Schema

**Pflichtfelder (immer vorhanden):**

| Feld | Typ | Beispiel | Quelle |
|---|---|---|---|
| `erstellt_am` | ISO-Datum | `2026-04-19` | Dispatcher oder Datei-mtime |
| `tags` | Liste | `[rechnung, handwerker]` | Dispatcher oder manuell |

**Klassifikations-Felder (wenn klassifiziert):**

| Feld | Typ | Beispiel | Wertebereich |
|---|---|---|---|
| `kategorie_id` | String | `immobilien_eigen` | categories.yaml |
| `typ_id` | String | `rechnung` | categories.yaml |
| `absender` | String | `Ferroli GmbH` | absender.yaml oder LLM |
| `adressat` | String | `Reinhard` | `Reinhard` \| `Marion` \| `Linoa` \| `Sonstiges` |
| `rechnungsdatum` | ISO-Datum | `2025-03-12` | LLM-Extraktion |
| `betrag` | Zahl | `1240.00` | LLM-Extraktion |
| `konfidenz` | String | `hoch` | `hoch` \| `mittel` \| `niedrig` |

**Dokument-Referenz-Felder:**

| Feld | Typ | Beispiel |
|---|---|---|
| `pdf_hash` | String (MD5) | `d715d98670a75e03...` |
| `text_hash` | String (SHA256) | `ab3f19e2c4...` |
| `anlage` | Wiki-Link | `[[Anlagen/20250312_Ferroli_Rechnung.pdf]]` |

**Status-Felder (optional):**

| Feld | Typ | Beschreibung |
|---|---|---|
| `status` | String | `verarbeitet` \| `erledigt` \| `offen` \| `prüfen` |
| `faellig` | ISO-Datum | Zahlungs-/Handlungsfrist |
| `todos` | Liste | Offene Aufgaben (aus OCR-Stubs übernommen) |

**Nicht vereinheitlicht (bleiben wie sie sind):**

| Feld | Begründung |
|---|---|
| `title`, `aliases` | Obsidian-native Felder, nicht anfassen |
| `linter-yaml-title-alias` | Vom Obsidian-Linter-Plugin verwaltet |
| `source`, `imported` | Historische Information (Evernote-Import), dokumentiert Herkunft |

#### 2.6.2 Legacy-Mapping-Tabelle

Beim Upgrade einer Datei werden Legacy-Felder in Standard-Felder überführt. **Das alte Feld bleibt zusätzlich erhalten** (Verlustfreiheit).

| Legacy-Feld | Neues Feld | Transformation |
|---|---|---|
| `date created` (Evernote) | `erstellt_am` | ISO-Datum normalisieren |
| `created` (Apple Notes) | `erstellt_am` | ISO-Datum normalisieren |
| `erstellt` (Dispatcher v1) | `erstellt_am` | direkt übernehmen |
| `type` (Dispatcher v1) | `typ_id` | gegen categories.yaml prüfen, ggf. mappen |
| `category` | `kategorie_id` | gegen categories.yaml prüfen |
| `datum` | `rechnungsdatum` | nur wenn Rechnungs-/Finanzdokument |
| `thema` | `tags` | als Tag aufnehmen, nicht ersetzen |

**Wichtig:** `date created` wird **nicht gelöscht**, sondern ergänzt um `erstellt_am`. Das erhält die Rückwärtskompatibilität für bestehende Dataview-Queries.

#### 2.6.3 Validator-Skript (ohne Änderung)

**Neues Skript:** `/home/reinhard/docker/docling-workflow/scripts/frontmatter_check.py`

Funktion:
- Scannt alle MD-Dateien im Vault
- Prüft gegen Unified Schema
- Erzeugt Report: welche Dateien entsprechen dem Schema, welche haben Legacy-Felder, welche sind leer
- **Ändert nichts** — nur Diagnose

**Ausgabe-Beispiel:**
```
📊 Frontmatter-Validator-Report

Geprüfte Dateien:              1.903
Mit Unified Schema komplett:     219 (11,5%)
Mit Legacy-Feldern mappbar:    1.462 (76,8%)
Frontmatter fehlt ganz:           95  (5,0%)
Nur Freitext-Tags (nicht mappbar): 127 (6,7%)

Top-Legacy-Mappings (Vorschläge):
  date created → erstellt_am:  1.390 Dateien
  created      → erstellt_am:    512 Dateien
  type         → typ_id:          20 Dateien
```

#### 2.6.4 On-Demand-Upgrade bei Batch-Verarbeitung

**Integration in Phase 2 (Dispatcher Batch-Modus):**

Wenn der Dispatcher eine Bestands-MD-Datei bearbeitet (via `/verarbeite` oder Template-Auswertung), wird zusätzlich das Frontmatter geupgraded:

```
BEI Batch-Verarbeitung einer bestehenden MD:
  1. Bestehendes Frontmatter parsen
  2. Legacy-Mapping anwenden (neue Felder ergänzen, alte bewahren)
  3. Dispatcher-Ergebnisse als neue Felder schreiben
  4. Datei speichern mit erweitertem Frontmatter
```

**Beispiel — vorher:**
```yaml
---
title: Ferroli Rechnung 2025
date created: 2025-03-12
tags: [heizung, rechnung]
source: email
---
```

**Nach Batch-Verarbeitung:**
```yaml
---
title: Ferroli Rechnung 2025
date created: 2025-03-12
tags: [heizung, rechnung]
source: email
# ── Unified Schema (on-demand ergänzt 2026-04-25) ──
erstellt_am: 2025-03-12
kategorie_id: immobilien_eigen
typ_id: rechnung
absender: Ferroli GmbH
adressat: Reinhard
rechnungsdatum: 2025-03-12
betrag: 1240.00
konfidenz: hoch
pdf_hash: d715d98670a75e03...
anlage: "[[Anlagen/20250312_Ferroli_Rechnung.pdf]]"
---
```

#### 2.6.5 Schema-Konfigurationsdatei

**Neue Datei:** `/home/reinhard/docker/docling-workflow/dispatcher-config/frontmatter_schema.yaml`

Enthält:
- Pflicht- und Optional-Felder mit Typ-Definitionen
- Legacy-Mapping-Regeln
- Werte-Bereiche (für Enums wie `adressat`, `konfidenz`, `status`)

Der Dispatcher lädt diese Datei beim Start — Schema-Änderungen ohne Code-Anpassung möglich.

#### 2.6.6 Dashboard-Integration (0,5 Tage)

**Neues Dashboard `/frontmatter`:**
- Schema-Compliance-Report (Zahlen wie Validator-Skript, aber interaktiv)
- Legacy-Feld-Verteilung als Balkendiagramm
- Datei-Liste mit Filter: "Unified komplett" / "Legacy mappbar" / "Kein Frontmatter"
- Pro Datei "Probe-Upgrade anzeigen" — zeigt Diff vorher/nachher ohne zu speichern
- Bulk-Upgrade nur für ausgewählte Ordner oder Dateien (nicht vault-weit)

**Vault-Dashboard `/vault` — Anpassung:**
- Neue Stat-Box "Klassifikationsgrad: X% MDs mit Unified Schema"
- Neue Spalte "Frontmatter-Status" pro Ordner (Unified / Legacy / Keins)

**Top-Navigation erweitern:** `[🏷️ Frontmatter]`

#### 2.6.7 Test-Gate Phase 2.6

**Automatisiert:**
- [ ] Validator-Skript liefert korrekten Report ohne Modifikationen
- [ ] Upgrade-Test: 5 Stichproben-MDs mit verschiedenen Legacy-Schemata werden korrekt aufgewertet
- [ ] Legacy-Felder bleiben nach Upgrade erhalten (Rückwärtskompatibilität)
- [ ] Schema-Datei lädt korrekt beim Dispatcher-Start

**Manuell im Dashboard:**
- [ ] `/frontmatter` zeigt ~11,5% Unified-Compliance (Ausgangszahl)
- [ ] "Probe-Upgrade" zeigt korrektes Diff ohne Dateiänderung
- [ ] Bulk-Upgrade auf einen Testordner (z.B. `85 Wissen`) funktioniert

**End-to-End:**
- [ ] Batch-Verarbeitung einer Bestands-MD via `/verarbeite` → Frontmatter enthält alte und neue Felder
- [ ] Dataview-Query mit Legacy-Feld `date created` funktioniert weiter
- [ ] Dataview-Query mit neuem Feld `kategorie_id` liefert geupgradete MDs

**User-Freigabe:** _______________________ (Datum) — Phase 2.7 erst danach

---

### Phase 2.7: Interaktive Klassifikation via Telegram

**Ziel:** Bei unsicherer LLM-Klassifikation einen geführten Dialog auf dem Smartphone führen — Dropdown-ähnliche Auswahl für Kategorie, Typ, Absender und Adressat. Neue Einträge können direkt in die Stammdatenbank oder Konfiguration geschrieben werden.

**Ausgangslage:** Das Dispatcher-Modell liefert für jedes Dokument eine Konfidenz (`hoch` / `mittel` / `niedrig`). Bei hoher Konfidenz ist das Ergebnis meist korrekt — bei mittel/niedrig lohnt eine menschliche Bestätigung statt automatischer Ablage.

#### 2.7.1 Dialog-Logik

```
Neues Dokument durch Pipeline
  │
  ├─ Konfidenz HOCH
  │     → Heutige Telegram-Nachricht: Info + [✅ Bestätigen] [✏️ Korrigieren]
  │     → Keine Dialog-Schleife
  │
  └─ Konfidenz MITTEL oder NIEDRIG
        → Geführter Dialog in 4 Schritten
        → Abschluss: Dokument wird verarbeitet
        → Neue Stammdaten werden persistiert
```

#### 2.7.2 Dialog-Schritte

**Schritt ① — Kategorie-Auswahl**
```
🆕 Neues Dokument erkannt
   Name: 20260419_Unbekannt_Rechnung.pdf
   LLM-Vorschlag: finanzen (Konfidenz: mittel)

   Kategorie bestätigen oder ändern:

   [finanzen (Vorschlag)] [krankenversicherung]
   [immobilien_eigen]     [immobilien_vermietet]
   [fahrzeuge]            [familie]
   [italien]              [business]
   [Mehr...]              [❌ Abbruch]
```

**Schritt ② — Typ-Auswahl (gefiltert nach Kategorie)**
```
Kategorie: finanzen ✓

   Dokumenttyp:

   [kontoauszug]          [rechnung_energie]
   [rechnung_telko]       [rechnung_sonstige]
   [steuer]               [darlehensvertrag]
   [korrespondenz]        [altersvorsorge]
   [← Zurück]             [+ Neuer Typ]
```

**Schritt ③ — Absender-Auswahl (Autocomplete aus DB)**
```
Absender:

   LLM erkannt: "Vattenfall" (niedrige Konfidenz)

   Treffer aus Datenbank:
   [Vattenfall GmbH (3 Dokumente)]
   [Vattenfall Europe Sales]
   [Vodafone GmbH (7 Dokumente)]

   [+ Neuer Absender]     [← Zurück]
```

**Schritt ④ — Adressat-Auswahl**
```
Adressat:

   [Reinhard]  [Marion]  [Linoa]
   [Sonstiges] [← Zurück] [✅ Fertig]
```

#### 2.7.3 "Neu anlegen"-Flows

**Neuer Absender:**
```
Bot:  Name des neuen Absenders eingeben:
User: Vattenfall Energie GmbH
Bot:  USt-IdNr / Part.IVA (optional, "skip" für überspringen):
User: DE123456789
Bot:  Kategorie für diesen Absender (wird als Hint gespeichert):
      [finanzen] [immobilien_eigen] [...]
User: [finanzen]
Bot:  ✅ Absender "Vattenfall Energie GmbH" gespeichert
      → DB-Tabelle: aussteller
      → Alias-Index: aussteller_aliases
      → Nächstes Mal wird er automatisch erkannt
```

**Neue Kategorie oder neuer Typ:**
- Wird in `categories.yaml` vorgeschlagen (Pull-Request-Pattern: Vorschlag mit Label, User bestätigt in Webinterface → s. Phase 2.8)
- Sofort-Ergänzung in YAML wäre möglich, aber versionskontroll-unfreundlich
- Kompromiss: Eintrag in DB-Tabelle `kategorie_vorschlaege`, wird beim nächsten Vault-Editor-Lauf in YAML überführt

#### 2.7.4 Persistenz und Rückkopplung

Jeder Dialog-Abschluss erzeugt:
- **DB-Eintrag** für das verarbeitete Dokument (wie heute)
- **Lernregel** in Tabelle `lernregeln`, wenn Nutzer-Wahl von LLM-Vorschlag abwich
- **Neuer Aussteller-Eintrag** bei "Neu anlegen"
- **Alias-Verknüpfung** wenn bestehender Aussteller gewählt wurde (erweitert Alias-Liste)

Damit wird jede Interaktion zu **Trainingsdaten für zukünftige Automatisierung**.

#### 2.7.5 Dashboard-Integration (0,25 Tage)

**Review-Dashboard `/review` — Anpassung:**
- Neue Spalte "Dialog-Status": pending / in-progress / completed / aborted
- Filter "nur via Telegram entschieden" / "nur via Web entschieden"
- Bei offenem Dialog: "Dialog-Historie anzeigen" (welcher Schritt, welche Antworten)
- Dokumente mit abgeschlossenem Telegram-Dialog erscheinen als "verarbeitet"

#### 2.7.6 Test-Gate Phase 2.7

**Automatisiert:**
- [ ] Konfidenz-Gate: Hoch-Konfidenz-Dokumente umgehen den Dialog automatisch
- [ ] Dialog-Abbruch: keine halbfertigen DB-Einträge
- [ ] Neuer Absender wird persistiert und im nächsten Dokument automatisch erkannt
- [ ] Zurück-Navigation im Dialog funktioniert ohne Dateninkonsistenz

**Manuell via Telegram:**
- [ ] Dokument mit niedriger Konfidenz importieren → Dialog startet automatisch
- [ ] Alle 4 Schritte durchlaufen, Abschluss erzeugt korrekten DB-Eintrag + Lernregel
- [ ] "Neuer Absender"-Flow: angelegter Absender erscheint in der DB (über `/admin` prüfbar, Phase 2.8)

**End-to-End:**
- [ ] Unbekannter Absender wird in Telegram angelegt → bei nächstem Dokument desselben Absenders hohe Konfidenz → kein Dialog
- [ ] `/review`-Dashboard zeigt Telegram-entschiedenes Dokument korrekt

**User-Freigabe:** _______________________ (Datum) — Phase 2.8 erst danach

---

### Phase 2.8: Admin-Web-Interface für Stammdaten-DB

**Ziel:** Browser-basiertes CRUD-Interface zur Pflege der wachsenden Stammdaten — Aussteller, Aliase, Lernregeln, Kategorie-Vorschläge. Ergänzt den Telegram-Dialog um Bulk-Operationen und visuelle Übersicht.

**Ausgangslage:** Die Stammdaten-DB hat bereits 47 Aussteller, 181 Alias-Varianten, 11 Lernregeln. Ohne Verwaltungsoberfläche müssen Korrekturen direkt in SQLite erfolgen — unkomfortabel und fehleranfällig.

#### 2.8.1 Integration ins bestehende Dashboard

Das Wilson-Dashboard (bereits auf Port 8765) wird um einen neuen Reiter **"Stammdaten"** erweitert. Gleiche Tech-Stack wie bestehende Pipeline-Dashboards: FastAPI + Server-rendered HTML + vanilla JS (kein SPA-Framework).

**Neue Routen:**
```
GET  /admin/aussteller              Tabelle mit Filter/Sortierung
GET  /admin/aussteller/{id}         Detailansicht + Bearbeitung
POST /admin/aussteller              Neu anlegen
PUT  /admin/aussteller/{id}         Aktualisieren
DELETE /admin/aussteller/{id}       Löschen (mit Bestätigung)

GET  /admin/aliases                 Alle Alias-Verknüpfungen
POST /admin/aliases                 Neuer Alias zu Aussteller
DELETE /admin/aliases/{id}          Alias entfernen

GET  /admin/lernregeln              Alle Regeln
PUT  /admin/lernregeln/{id}         Regel ändern
DELETE /admin/lernregeln/{id}       Regel löschen

GET  /admin/kategorie-vorschlaege   Offene Vorschläge aus Phase 2.7
POST /admin/kategorie-vorschlaege/{id}/apply  YAML aktualisieren
```

#### 2.8.2 UI-Funktionalität

**Aussteller-Tabelle**
- Spalten: Name, Typ, Ort, # zugeordnete Dokumente, Aliase (Anzahl), letzte Verwendung
- Filter: Typ (Arzt/Firma/Behörde), Land, Volltextsuche
- Inline-Edit für einfache Felder (Telefon, Email, Notizen)
- Detail-Modal für komplexe Felder (Aliase, Part.IVA/USt-IdNr)
- Massen-Operationen: Mehrere Aussteller zusammenführen (bei Duplikat-Erkennung)

**Aliase-Verwaltung**
- Gruppiert pro Aussteller
- Drag-and-drop für Aliase zwischen Ausstellern (Korrektur von Fehlzuordnungen)
- Automatische Erkennung: "Clinica Veterinaria Amiatina" vs "CLINICA VETERINARIA AMIATINA" → Merge-Vorschlag

**Lernregeln**
- Anzeige: Muster, Auslöser, Kategorie/Typ, Anwendungs-Zähler
- Deaktivieren einer Regel (ohne Löschen) — für Testzwecke
- Regel-Priorisierung via Drag-and-Drop

**Kategorie/Typ-Vorschläge**
- Offene Vorschläge aus Telegram-Dialogen (Phase 2.7)
- "Genehmigen" schreibt Änderung in `categories.yaml` (via Git-Commit)
- "Ablehnen" verwirft Vorschlag

#### 2.8.3 Sicherheit und Validierung

**Zugriff:**
- Web-Interface nur im lokalen Netzwerk erreichbar (bind 0.0.0.0 → 192.168.x.x)
- Optional: Basic Auth mit Umgebungsvariable `DASHBOARD_PASSWORD`
- Keine Exposition ins Internet

**Validierung beim Speichern:**
- Part.IVA: 11 Ziffern (IT-Format)
- USt-IdNr: DE + 9 Ziffern
- IBAN: ISO 13616 Prüfsumme
- Kategorie-ID: muss in `categories.yaml` existieren
- Typ-ID: muss zur Kategorie passen
- Adressat: nur `Reinhard` / `Marion` / `Linoa` / `Sonstiges`

**Löschung mit Referenz-Prüfung:**
- Beim Löschen eines Ausstellers: Prüfen auf verknüpfte Dokumente
- Bei Referenzen: Warnung + Option "Dokumente auf 'Unbekannt' umleiten" oder "Löschung abbrechen"
- Alle Löschungen werden in `audit_log`-Tabelle protokolliert (Wer, Wann, Was)

#### 2.8.4 Zusätzliche Tabellen (erweitertes Schema)

**Neue DB-Tabellen:**

```sql
CREATE TABLE personen (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kuerzel TEXT UNIQUE,         -- Reinhard, Marion, Linoa
    typ TEXT,                    -- familie, mitarbeiter, mandant
    geburtsdatum DATE,
    notizen TEXT
);

CREATE TABLE kategorie_vorschlaege (
    id INTEGER PRIMARY KEY,
    vorgeschlagen_am TIMESTAMP,
    kategorie_id TEXT,
    typ_id TEXT,
    label TEXT,
    begruendung TEXT,
    status TEXT,                 -- offen, genehmigt, abgelehnt
    anwendungen INTEGER DEFAULT 0
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    timestamp TIMESTAMP,
    tabelle TEXT,
    operation TEXT,              -- INSERT, UPDATE, DELETE
    datensatz_id INTEGER,
    vorher JSON,
    nachher JSON,
    ausgelöst_durch TEXT         -- telegram, web, dispatcher
);
```

**`personen` ersetzt den harten String `adressat`** — Adressat-Feld im Dokument wird zu `person_id` (Foreign Key).

#### 2.8.5 Export / Backup

- **Export als JSON/CSV** pro Tabelle (für externes Backup)
- **Git-Commit-Button** für Kategorien/Absender: schreibt YAML-Änderungen zurück in Repo
- Eine Schnittstelle für manuelle Stammdaten-Pflege, nicht für Massen-Imports

#### 2.8.6 Test-Gate Phase 2.8

**Automatisiert:**
- [ ] CRUD-Test für jede Tabelle (Anlegen / Ändern / Löschen)
- [ ] Validierungs-Test: ungültige Part.IVA wird abgelehnt
- [ ] Referenz-Prüfung: Aussteller mit verknüpften Dokumenten kann nicht einfach gelöscht werden
- [ ] Audit-Log: jede Änderung erzeugt korrekten Eintrag mit `vorher`/`nachher`

**Manuell im Dashboard:**
- [ ] `/admin/aussteller` zeigt alle 47 Aussteller mit Dokument-Zählung
- [ ] Inline-Edit eines Aussteller-Felds speichert korrekt und aktualisiert Audit-Log
- [ ] Merge-Test: zwei manuelle Duplikat-Aussteller werden fusioniert, Aliase übertragen
- [ ] Mobile-Safari: Tabelle und Edit-Formulare nutzbar

**End-to-End:**
- [ ] Aussteller im Web anlegen → bei nächstem Dokument desselben Absenders automatische Erkennung
- [ ] Kategorie-Vorschlag aus Telegram (Phase 2.7) erscheint in `/admin/kategorie-vorschlaege`
- [ ] "Genehmigen" schreibt Änderung in `categories.yaml` via Git-Commit

**User-Freigabe:** _______________________ (Datum) — Phase 3 erst danach

---

### Phase 3: Telegram-Bot-Erweiterung

**Ziel:** Neue Telegram-Befehle ermöglichen Suche und Auswertung vom Smartphone.

#### 3.1 Neue Befehle (1,5 Tage)

| Befehl | Funktion | Ausgabe |
|---|---|---|
| `/suche <begriff>` | Volltextsuche via Cache-Reader | Nummerierte Liste, max. 5 Treffer + "Mehr"-Button |
| `/verarbeite <ids>` | Batch-Modus auf Auswahl | Fortschritt + Ergebnis-Zusammenfassung |
| `/auswertung <typ> <jahr>` | Template-basiert (s. Phase 4) | Strukturierte Tabelle + Datei-Anhang |
| `/inbox` | Aktuelle Inbox-Dokumente | Kurze Liste mit Links zum Dashboard |
| `/reocr <id>` | Re-OCR-Fallback für Stub | Fortschritt + neue Klassifikation |
| `/status` | Pipeline- und Index-Status | Kennzahlen-Übersicht |

#### 3.2 UI-Muster für eingeschränkten Bildschirm (1 Tag)

- [ ] Trefferlisten: nummeriert, max. 5 pro Nachricht, Score in Klammern
- [ ] Excerpts: max. 80 Zeichen, `...` an Schnittstellen
- [ ] Inline-Buttons: Auswahl via Callback-Query (kein Freitext nötig)
- [ ] Große Ergebnisse: als CSV-Datei-Anhang (Telegram Document-Upload)
- [ ] Zustand zwischen Nachrichten: in SQLite-Tabelle `telegram_sessions` speichern

**Beispiel-Dialog:**
```
User: /suche Handwerker Seggiano 2025

Bot: 📂 12 Treffer

1. Ferroli Rechnung (03/25) .94
   …Heizungswartung Podere…
2. Bonifica Amiata (06/25) .88
   …Entsorgung…
3. Elettricista Russo (07/25) .85
   …Elektroinstallation…

[Mehr] [Alle verarbeiten] [Abbrechen]

User: [Alle verarbeiten]

Bot: ⏳ Verarbeite 12 Dokumente...
     [5/12] [Abbrechen]

Bot: ✅ Fertig. Gesamt: 8.420 EUR
     [CSV herunterladen] [Details]
```

#### 3.3 OpenClaw-Relay (0,5 Tage)

- [ ] OpenClaw auf Pi erhält neue Route für Vault-Befehle
- [ ] HTTP-Client zu Ryzen: Timeout 30 Sek., Retry bei Netzwerkfehlern
- [ ] Pufferung bei Verbindungsabbruch: Befehle in lokaler SQLite queue

#### 3.4 Dashboard-Integration (0,25 Tage)

**Wilson-Dashboard `/wilson` — Erweiterung:**
- Neuer Status-Block "Telegram-Bot":
  - Aktive Sessions (User mit laufendem Dialog)
  - Letzte 10 Befehle (Timestamp, Befehl, User)
  - Durchschnittliche Antwortzeit
  - Bot-Lag zu Telegram-API
- Neuer Status-Block "OpenClaw-Relay":
  - Queue-Länge bei Verbindungsproblemen
  - Letzter erfolgreicher Sync zu Ryzen
  - Verbindungsstatus Ampel (grün/gelb/rot)

#### 3.5 Test-Gate Phase 3

**Automatisiert:**
- [ ] Alle neuen Telegram-Befehle haben Command-Handler + Parameter-Validierung
- [ ] Trefferliste wird korrekt gekürzt (max. 5 + "Mehr"-Button)
- [ ] Session-State zwischen Nachrichten bleibt konsistent
- [ ] OpenClaw-Queue puffert Befehle bei simuliertem Verbindungsverlust

**Manuell via Telegram:**
- [ ] `/suche "Handwerker Seggiano 2025"` → sinnvolle Treffer auf Smartphone lesbar
- [ ] `/verarbeite 1,2,3` → Batch-Modus startet, Fortschritt erscheint in Telegram
- [ ] Große Auswertung → CSV-Datei-Anhang statt Inline-Nachricht
- [ ] `/reocr <path>` → Stub wird via Docling neu verarbeitet

**End-to-End:**
- [ ] Smartphone mit LTE (nicht WLAN): Kompletter Flow `/suche` → Auswahl → `/verarbeite` → Ergebnis
- [ ] Netzwerk-Unterbrechung während `/suche`: nach Wiederverbindung wird Antwort zugestellt
- [ ] `/wilson`-Dashboard zeigt Telegram-Aktivität korrekt

**User-Freigabe:** _______________________ (Datum) — Phase 4 erst danach

---

### Phase 4: Standard-Auswertungs-Templates

**Ziel:** Vordefinierte Auswertungen als Ein-Kommando-Befehle.

#### 4.1 Templates (1,5 Tage)

**Template 1: Steuer-Handwerker**
```
/steuer-handwerker 2025
→ Sucht: "Handwerker" OR "Rechnung" in Immobilien-Kategorien
→ Filtert: Jahr 2025
→ Extrahiert: Betrag, Aussteller, Datum, Objekt
→ Ausgabe: CSV mit Spalten für Steuerberater
```

**Template 2: KV-Erstattung pro Person**
```
/kv-erstattung Marion 2025
→ Sucht: Leistungsabrechnung HUK/Gothaer/Barmenia
→ Filtert: Adressat=Marion, Jahr=2025
→ Extrahiert: Rechnungsbetrag, erstatteter Betrag, Erstattungsdatum
→ Ausgabe: Tabelle mit Summen
```

**Template 3: Italien-Jahresübersicht**
```
/italien 2025
→ Sucht: IMU, TARI, Acquedotto, ButanGas, Comune
→ Filtert: Jahr=2025
→ Extrahiert: Kategorie, Betrag, Datum
→ Ausgabe: Markdown-Tabelle für Vault-Notiz
```

#### 4.2 Export-Formate (0,5 Tage)

- [ ] CSV (UTF-8, Komma-getrennt, Excel-kompatibel)
- [ ] Markdown-Tabelle (für automatische Vault-Einbettung)
- [ ] PDF-Bericht (via `weasyprint`, für formale Weitergabe)

#### 4.3 Dashboard-Integration (0,5 Tage)

**Neues Dashboard `/auswertungen`:**
- Template-Katalog: Liste aller verfügbaren Templates (Steuer-Handwerker, KV-Erstattung, Italien-Jahresübersicht) mit Kurzbeschreibung
- Template auswählen → Parameter-Formular (Jahr, Person, etc.) → Ausführen-Button
- Fortschrittsanzeige während Ausführung
- Ergebnis-Vorschau direkt im Browser (Tabelle)
- Export-Buttons (CSV, MD, PDF)
- **Historie der letzten 30 Auswertungen** mit Download-Links

**Top-Navigation erweitern:** `[📈 Auswertungen]`

#### 4.4 Test-Gate Phase 4

**Automatisiert:**
- [ ] Template "Steuer-Handwerker 2025" liefert mindestens die 60 erwarteten Handwerker-Rechnungen
- [ ] Betrags-Summen stimmen (Stichprobenvergleich mit manueller Prüfung)
- [ ] Export-Formate sind valid (CSV öffnet in Excel, MD rendert in Obsidian, PDF ist lesbar)

**Manuell im Dashboard:**
- [ ] `/auswertungen` zeigt alle 3 Templates
- [ ] Template-Ausführung zeigt sinnvollen Fortschritt
- [ ] Ergebnis-Vorschau ist lesbar und navigierbar
- [ ] Historie enthält alle ausgeführten Auswertungen

**Manuell via Telegram:**
- [ ] `/steuer-handwerker 2025` liefert CSV-Datei auf Smartphone
- [ ] Datei öffnet in mobiler Excel/Numbers-App korrekt

**End-to-End:**
- [ ] Reale Steuerauswertung: Template wird in CSV exportiert, Steuerberater-Tauglichkeit geprüft
- [ ] Ergebnis wird in Obsidian als MD-Tabelle eingefügt und rendert korrekt

**User-Freigabe:** _______________________ (Datum) — Phase 5 erst danach

---

### Phase 5: Monitoring + Home-Konsolidierung

**Ziel:** Sichtbarkeit über Coverage und Qualität der neuen Architektur.

#### 5.1 Haupt-Dashboard `/` — Finalisierung (0,5 Tage)

Die Startseite wird zum zentralen Status-Überblick:

- **Strategie-Hinweis** prominent oben: "Flaches Archiv + On-Demand-Verarbeitung aktiv"
- **Kennzahlen-Raster (3×3):**
  - Vault: 3.246 PDFs / 1.923 MD
  - DB: verarbeitete Dokumente / bekannte Aussteller / Lernregeln
  - Cache: indizierte PDFs / Coverage-% / Leerquote
  - Stammdaten: Aussteller / Aliase / Personen
  - Duplikate: Gruppen / redundante Dateien / Quarantäne-Inhalt
  - Pipeline: Heute verarbeitet / Review-Queue / Telegram-Dialoge offen
  - Batch: Aktive Läufe / Historie / Durchsatz
  - Auswertungen: Letzte 3 Ausführungen
  - Frontmatter: Unified-Compliance-%
- **Quick Actions-Leiste:**
  - "Neue Auswertung" → `/auswertungen`
  - "Dedup-Scan starten" → `/duplikate`
  - "Stammdaten öffnen" → `/admin`
  - "Cache-Status" → `/cache`

#### 5.2 Wilson-Dashboard `/wilson` — Monitoring-Erweiterung (0,5 Tage)

Zusätzliche Monitoring-Kacheln:

- **Cache-Coverage-Trend:** Zeitreihe über 30 Tage
- **Batch-Durchsatz:** Dokumente pro Minute (Hybrid vs. reines Docling)
- **Telegram-Aktivität:** Befehle pro Tag, häufigste Suchanfragen (Top 5)
- **Syncthing-Konflikte-Indikator:** Scannt Vault nach `.sync-conflict-*`-Dateien
- **Auswertungs-Historie:** Letzte 10 Batch-Auswertungen mit Trefferzahl

#### 5.3 Navigation-Finalisierung (0,5 Tage)

**Einheitliche Top-Navigation** auf allen Dashboards, finaler Stand:

```
[📊 Home] [🔄 Pipeline] [📝 Review] [📂 Vault] [📎 Anlagen]
[🔍 Cache] [♻️ Duplikate] [🏷️ Frontmatter] [⚙️ Batch]
[📈 Auswertungen] [🗂️ Admin] [🥧 Wilson]
```

- Aktiver Reiter wird markiert
- Konsistenter Projekt-Header auf jeder Seite
- Dark-Mode-Toggle (bereits vorhanden) wird auf alle neuen Dashboards übertragen

#### 5.4 Test-Gate Phase 5 (Finale Abnahme)

**Automatisiert:**
- [ ] Alle 12 Dashboards laden fehlerfrei
- [ ] Navigation zwischen Dashboards erhält Zustand (Filter, Sortierung wo sinnvoll)
- [ ] Metriken auf Haupt-Dashboard stimmen mit Einzelansichten überein

**Manuell:**
- [ ] Stichprobe alle Dashboards auf Desktop Chrome, Firefox, Mobile Safari
- [ ] Dark-Mode schaltet konsistent auf allen Seiten
- [ ] Alle Quick Actions auf Haupt-Dashboard führen zu richtigem Ziel

**End-to-End-Szenarien:**
- [ ] Vom Smartphone aus: Suche → Auswertung → Ergebnis als PDF-Anhang erhalten
- [ ] Vom Desktop aus: neuen Absender anlegen → testweise Dokument importieren → wird automatisch korrekt klassifiziert
- [ ] Duplikat-Szenario: manuelle PDF-Kopie in Vault legen → nach Text-Extractor-OCR von Dedup-Scan erkannt → im Dashboard sichtbar

**Finale User-Freigabe:** _______________________ (Datum) — Projekt-Umstellung abgeschlossen

---

### Meilensteine und Abnahmekriterien

| Meilenstein | Nach Phase | Abnahmekriterium |
|---|---|---|
| M1: Cache-Reader + Dashboard | 1 | Cache-Reader-API + `/cache`-Dashboard produktiv, Test-Gate freigegeben |
| M2: Batch-Modus + Dashboard | 2 | CLI + `/batch`-Dashboard produktiv, Pipeline-Dashboard aktualisiert, Test-Gate freigegeben |
| M2.5: Duplikat-Management + Dashboard | 2.5 | Dedup-Scan, Quarantäne, `/duplikate`-Dashboard produktiv, Test-Gate freigegeben |
| M2.6: Frontmatter-Schema + Dashboard | 2.6 | Schema + `/frontmatter`-Validator-Dashboard produktiv, Test-Gate freigegeben |
| M2.7: Interaktive Klassifikation | 2.7 | Telegram-Dialog + Review-Dashboard-Anpassung produktiv, Test-Gate freigegeben |
| M2.8: Admin-Web-Interface | 2.8 | `/admin`-Dashboard mit CRUD + Audit-Log produktiv, Test-Gate freigegeben |
| M3: Telegram-Suche + Wilson-Update | 3 | `/suche`, `/verarbeite`, Wilson-Bot-Status produktiv, Test-Gate freigegeben |
| M4: Auswertungen + Dashboard | 4 | Templates + `/auswertungen`-Dashboard produktiv, Test-Gate freigegeben |
| M5: Finale Konsolidierung | 5 | Haupt-Dashboard finalisiert, einheitliche Nav auf allen 12 Dashboards, End-to-End-Abnahme |

---

### Risiken und Gegenmaßnahmen

| Risiko | Auswirkung | Gegenmaßnahme |
|---|---|---|
| Cache-Qualität reicht nicht für Auswertungen | Fehlerhafte CSVs | Hybrid-Fallback auf Docling; `--force-docling` bei Zweifel |
| Telegram-Ratelimit bei großen Auswertungen | Verzögerte Antworten | Gestückelte Nachrichten, Datei-Upload statt Inline |
| Cache-Reader-Index veraltet | Falsche Suchergebnisse | File-Watcher + Healthcheck, manuelles `/reindex` |
| Syncthing-Konflikte bei Mac+Ryzen | Inkonsistente Daten | Isolation-Regeln (Phase 1.1), Monitoring von `.sync-conflict-*`-Dateien |
| Docling-Container instabil bei Dauerlast | Batch-Modus bricht ab | Retry-Logik, Fortschritt persistieren, `--resume`-Parameter |
| Falsch-positives Duplikat (Text-Hash-Kollision) | Unikat geht verloren | Quarantäne statt Löschung, 30-Tage-Frist, Telegram-Rückfrage bei Text-Hash-Match |
| Markdown-Links zeigen ins Leere nach Quarantäne | Broken Vault-Links | Links nicht automatisch umschreiben; Quarantäne-INFO.md zeigt Original-Pfad |

---

## Anhang: Technische Kennzahlen

| Kennzahl | Wert |
|---|---|
| Vault-PDFs gesamt | 3.246 |
| Vault-Markdown-Notizen | 1.923 |
| Text-Extractor-Cache (PDFs) | 846 (26%) |
| Verwertbarer OCR-Text im Cache | 695 PDFs (82%) |
| Dispatcher-verarbeitete Dokumente | 123 |
| Hochkonfidenz-Treffer im Cache | ~212 (Keyword-Match) |
| Bekannte Absender (absender.yaml) | 47 |
| Lernregeln aus Korrekturen | 11 |
| Klassifikations-Kategorien | 16 |
| Dokumenttypen gesamt | ~60 |
| Primäres Klassifikations-LLM | mistral-nemo:12B |
| Übersetzungs-LLM | translategemma:latest |
| Geschätzter Vollrescan-Aufwand | 108–270 Stunden |
| OmniSearch API-Port | 51361 |

---

*Aktiver Entwicklungszweig: `feature/classification-v2` (mehrstufige Klassifikation, echte Konfidenz aus Disagreement, gestuftes Routing)*
*Primäres LLM: mistral-nemo:12B lokal via Ollama*
*Erstellt April 2026 zur Vorbereitung einer Expertenberatung*
