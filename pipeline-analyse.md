# PDF-Verarbeitungs-Pipeline: Ist-Stand Juli 2026

> **Zweck dieses Dokuments:** Präzise, code-belegte Analyse des heutigen Verarbeitungsprozesses.  
> Welchen Weg durchläuft ein PDF? Welches Frontmatter-Feld kommt woher? Welche Overrides greifen in welcher Reihenfolge?  
> Alle Zeilenangaben beziehen sich auf `dispatcher/dispatcher.py`, Stand 2. Juli 2026.

## Changelog seit 28. Juni

### Identifikator-Extraktion erweitert
`extract_identifiers()` erkennt jetzt zusätzlich:
- **KFZ-Kennzeichen** (DE+IT Format) → `kategorie_hint: fahrzeuge` (Konfidenz=hoch)
- **VIN/Fahrgestellnummer** (17-stellig) → `kategorie_hint: fahrzeuge`
- **Firmen-Namen** aus `absender.yaml` Aliases im Volltext
- **Adress-Matches**: Immobilien-Adressen aus `immobilien.db`, Personen-Adressen aus `personen.yaml`, Absender-Adressen aus `aussteller`-Tabelle

### Adressat-Bestimmung (`resolve_adressat()`)
Neue Reihenfolge:
1. Cod.Fiscale (hart) → `confidence: hoch`
2. Immobilien-Adressen (weich) → `confidence: mittel, needs_review: True`
3. Personen-Adressen (weich) → `confidence: mittel, needs_review: True`
4. Tier-Namen (mittel)
5. **Lernregeln** (Absender → Adressat, aus bestätigten Reviews)

### Adress-Review via Telegram
Weiche Adress-Matches triggern eine Telegram-Nachricht mit Inline-Buttons:
- [✅ Ja] → Adressat wird gesetzt, Lernregel (Absender → Adressat) gespeichert
- [❌ Nein] → LLM-Entscheidung bleibt

### Adress-Lernregeln (`apply_adress_lernregeln()`)
Bestätigte Absender→Adressat-Zuordnungen werden in der `lernregeln`-Tabelle gespeichert (typ=adressat).
Beim nächsten Dokument vom gleichen Absender wird der Adressat automatisch gesetzt (Konfidenz=hoch), keine Review nötig.

### Override-Kaskade in eigene Funktion extrahiert
- `apply_overrides()` — Schritte 1–8 (Adressat, Absender, Taxonomie, Hints)
- `apply_post_overrides()` — Schritte 9–13 (Keyword-Rules, Lernregeln, Konfidenz)
- Pipeline-Debugger ruft dieselben Funktionen auf wie das Live-System

### Tags auf type_id reduziert (Option B)
`_build_frontmatter()` schreibt nur noch `type_id` als Tag (nicht mehr `category_id`).
`kategorie:`-Feld bleibt als verlässliche Quelle erhalten.

### ENEX-Code entfernt
Alle ENEX-Dateien wurden importiert. ~1.500 Zeilen toter Code entfernt.

### Pipeline-Debugger
Neuer Tab `/pipeline-debug` im Dispatcher: PDF-Upload → alle 8 Schritte visuell im Terminal-Stil.
Zeigt OCR-Text, LLM-Prompt mit Modell, Override-Kaskade, Frontmatter-Vorschau.
Nutzt dieselben Funktionen wie das Live-System (`apply_overrides()`).

### personen.yaml
- Adressen für Reinhard und Marion hinzugefügt (Grassauer Str., Podere dei venti)
- Linoa aus persons entfernt (ist ein Hund, kein Mensch)
- Hunde Linoa (Appenzeller, →Reinhard) und Molly (Labrador, →Marion) mit Rasse-Aliases

---

## 1. Eingang: Wie kommt ein PDF ins System?

### 1.1 Watch-Verzeichnis (Primärpfad)

Der Dispatcher überwacht `/data/input-dispatcher` (env `WATCH_DIR`) per `watchdog.Observer`. Zwei Handler:

| Handler | Pfad | Dateitypen | Zeile |
|---------|------|-----------|-------|
| `DocumentHandler` | `WATCH_DIR/` | `.pdf`, `.md`+`.meta.json`, `.enex` | 16506–16555 |
| `EnexHandler` | `WATCH_DIR/enex/` | `.enex` | 16558–16578 |

Beim Start werden bereits vorhandene Dateien per `Path.rglob` enqueued (Zeilen 17625–17648).

**Queue-Worker** (`queue_worker`, Zeilen 16483–16501) dispatched:
- `("rescan", path)` → `rescan_archived_pdf(path)` — erneute Verarbeitung
- `("enex", path)` → `process_enex_file(path)` — ENEX-Import
- `Path`-Objekt → `process_file(path)` — **Haupt-Pipeline** (dieses Dokument)

### 1.2 Batch-Mode

Via `/batch` UI (Dispatcher-Webinterface): Liste von PDFs → OCR-Quelle wählbar (cache/hybrid/docling) → parallele Verarbeitung mit Batch-Ergebnis-Tabelle.

### 1.3 Wilson-Sidecar-Bypass

Wenn neben dem PDF eine `.meta.json` existiert (vom externen Wilson-System auf dem Pi erstellt), wird die **gesamte OCR+LLM-Pipeline übersprungen**. Die Sidecar-Daten (kategorie_id, absender, adressat, beschreibung, rechnungsdatum, summary_de) werden direkt übernommen. Nur Keyword-Rules werden noch angewandt. (`process_file`, Zeilen 15713–15959)

### 1.4 ENEX-Import (Parallelpfad)

`.enex`-Dateien (Evernote-Export) durchlaufen einen **zweiphasigen** Prozess:
- **Phase 1** (`enex_processor.py`): Sofortimport — ENML→Markdown, Tag-Routing, PDF-Extraktion, schreibt MD mit `ocr_status: pending`
- **Phase 2** (`enex_ocr_worker.py`): Nachtlauf — OCR für extrahierte PDFs, ersetzt `<!-- OCR_PLACEHOLDER -->`

---

## 2. OCR: Docling-Konvertierung

### 2.1 Zwei-Pass-Strategie

`convert_to_markdown()` (Zeilen 13614–13653):

```
Pass 1: force_ocr=False → native Text-Extraktion (born-digital PDFs)
  ↓
EasyOCR-Artefakt? (_has_easyocr_artifact, Zeilen 13603–13611)
  ↓ Ja
Pass 2: force_ocr=True → Tesseract CLI mit deu+ita+eng
```

Die Artefakt-Erkennung zählt "spaced-out" Buchstaben-Muster (`J a n n i n g`) in den ersten 2000 Zeichen. Mehr als 5 → Artefakt erkannt.

### 2.2 Docling-API-Call

`_docling_convert()` (Zeilen 13577–13600):

```python
POST http://docling-serve:5001/v1/convert/file
data = {
    "to_formats": "md",
    "image_export_mode": "placeholder",
    "ocr_lang": ["deu", "ita", "eng"],
    "force_ocr": True/False,
}
timeout = 600s
```

### 2.3 OCR-Qualitäts-Gate

`OCR_MIN_CHARS = 150` (Zeile 121). Begründung im Code: "Rezepte sind kurze Dokumente (15–25 Zeilen), selbst mit perfekter OCR oft < 300 Zeichen. 150 erkennt echte OCR-Ausfälle und lässt kurze valide Dokumente durch."

PDFs mit <150 Zeichen → direkt `00 Inbox`, Telegram-Warnung, **kein LLM**, kein Frontmatter außer `Datum_original` + `original` + `erstellt`. (Zeilen 15985–16001)

### 2.4 Text-Trunkierung für LLM

| Stufe | Zeichen | Zeile | Verwendung |
|-------|---------|------|------------|
| Primär | 12.000 | 14535 | Erster Klassifikationsversuch |
| Retry | 4.000 | 16116 | Vereinfachter Prompt bei Fehlschlag |
| Summarizer | 6.000 | vault_summarizer.py:128 | `MAX_INPUT_CHARS` |

Zusätzlich: `sanitize_for_ollama()` (Zeilen 13672–13677) entfernt arabische/kyrillische Zeichen, Steuerzeichen, kollabiert 3+ Spaces.

---

## 3. Deterministische Extraktion (vor LLM)

Diese Schritte laufen **vor** der LLM-Klassifikation und liefern Kontext für den Prompt.

### 3.1 Header-Extraktion

`extract_document_header(md_content)` — Regex-basiert auf den ersten 100 Zeilen (Zeilen 16012–16026). Extrahiert Absender (Name, PLZ, Ort) und Empfänger aus Briefkopf-Strukturen. Wird als JSON-Artefakt (`*.header.json`) gespeichert.

### 3.2 Identifier-Extraktion

`extract_identifiers(md_content)` (Zeilen 16028–16060) sucht per Regex nach:
- **Codice Fiscale** (italienische Steuernummer, 16 Zeichen)
- **Partita IVA** (italienische Umsatzsteuer-ID, 11 Ziffern)
- **USt-IdNr** (deutsche Umsatzsteuer-ID, DE + 9 Ziffern)
- **IBAN** (internationale Bankkontonummer)

Danach Auflösung gegen:
- `personen.yaml` → `resolve_adressat()`: ordnet Cod.Fiscale/IBAN einer Person zu (Reinhard/Marion)
- `absender.yaml` → `resolve_absender()`: ordnet Part.IVA/USt-IdNr/PLZ einem bekannten Absender zu

Ergebnis: `adressat_match` (mit `person_key`, `tier`, `via`) und `absender_match` (mit `id`, `name`, `kategorie_hint`, `typ_hint`, `adressat_default`).

### 3.3 Dokumenttyp-Erkennung

`extract_document_type(md_content)` (Zeilen 16062–16078) matched Keywords aus `doc_types.yaml` gegen die ersten 20 Zeilen des OCR-Texts. Ergebnis: `doc_type_info` mit `erkannter_typ`, `quell_keyword`, `kategorie_hint`.

### 3.4 Spracherkennung

`detect_document_language(md_content)` (Zeilen 16080–16087) via `langdetect`. Fallback `("de", 0.0)` wenn Text < 200 Zeichen. Kein Übersetzungs-Pass mehr (seit 24. Juni entfernt — qwen3:4b-instruct klassifiziert DE+IT+EN direkt).

---

## 4. LLM-Klassifikation

### 4.1 Modell

`OLLAMA_MODEL` = `qwen2.5:7b` (env, default in Zeile 63).  
`OLLAMA_NUM_CTX` = 8192 (env, default in Zeile 53).

### 4.2 Prompt-Struktur

`classify_with_ollama()` (Zeilen 14356–14555) baut einen mehrteiligen Prompt:

1. **KV-Spezialregeln** (Zeilen 14369–14431) — Absender→Adressat-Mapping, Rezept-Erkennung, Erstattungslogik
2. **Header-Block** (Zeilen 14433–14440) — deterministisch extrahierter Absender/Empfänger
3. **Identifier-Block** (Zeilen 14442–14452) — Cod.Fiscale/Part.IVA-Matches
4. **Branchen-Regeln** (Zeilen 14454–14470) — aus `categories.yaml branchen_regeln`
5. **Dokumenttyp-Block** (Zeilen 14472–14479) — erkannter Typ aus `doc_types.yaml`
6. **Negativregeln** (Zeilen 14481–14489) — häufige Fehlklassifikationen
7. **Haupt-Prompt** (Zeilen 14491–14535) — Klassifikationsanweisung mit OCR-Text

### 4.3 Erwartete JSON-Antwort

```json
{
  "category_id": "krankenversicherung",
  "category_label": "Krankenversicherung",
  "type_id": "leistungsabrechnung",
  "type_label": "Leistungsabrechnung",
  "absender": "Gothaer Krankenversicherung",
  "adressat": "Reinhard",
  "rechnungsdatum": "15.03.2026",
  "rechnungsbetrag": "33,06 EUR",
  "erstattungsbetrag": null,
  "faelligkeitsdatum": null,
  "positionen": [],
  "konfidenz_category": "hoch",
  "konfidenz_type": "hoch",
  "konfidenz_absender": "mittel",
  "konfidenz_adressat": "hoch",
  "konfidenz_datum": "mittel"
}
```

### 4.4 Retry-Strategie

**Ebene 1 — GPU-Hang** (Zeilen 14548–14551): HTTP 500 mit `"model runner"` oder `"unexpected EOF"` → 5s Pause → ein Wiederholungsversuch (AMD iGPU cold-load workaround).

**Ebene 2 — Null-Klassifikation** (Zeilen 16113–16127): Wenn Ergebnis `None` oder `category_id` null → vereinfachter Prompt mit nur 4.000 Zeichen Text.

---

## 5. Override-Kaskade

Dies ist der Kern der Pipeline — 13 Schritte in **exakter Ausführungsreihenfolge**, die das LLM-Ergebnis modifizieren. Jeder Schritt kann Felder überschreiben.

| # | Override | Zeilen | Was passiert? |
|---|----------|--------|---------------|
| **1** | **Adressat via Cod.Fiscale** | 16135–16145 | `adressat_match.person_key` (aus personen.yaml) überschreibt LLM-Adressat. Konfidenz=hoch (harter Fakt). |
| **2** | **Adressat via Absender-Default** | 16146–16163 | Wenn Absender in absender.yaml ein `adressat_default` hat (z.B. Gothaer→Reinhard, HUK→Marion), wird das gesetzt. Überschreibt auch "Reinhard & Marion". Konfidenz=hoch. |
| **3** | **Absender-Fallback** | 16165–16167 | Wenn LLM keinen Absender, aber `absender_match.name` existiert → Absender aus DB übernommen. |
| **4** | **Datum-Konfidenz LA** | 16174–16179 | Bei Leistungsabrechnung + bekanntem Absender → konfidenz_datum=hoch (Abrechnungsdatum ist das einzige valide Datum). |
| **5** | **Taxonomie: halluzinierte Kategorie** | 16183–16187 | `category_id` nicht in categories.yaml → null, type_id auch null, konfidenz=niedrig → später Inbox. |
| **6** | **Taxonomie: halluzinierter Typ** | 16193–16202 | `type_id` nicht in Whitelist der Kategorie → null (Dokument bleibt in Kategorie-Wurzel). |
| **7** | **Absender-Hint Kategorie** | 16206–16224 | `absender_match.kategorie_hint` (aus absender.yaml) überschreibt LLM-Kategorie. User-definierte Zuordnung schlägt semantisches Raten. |
| **8** | **Dokumenttyp-Hint** | 16228–16236 | `doc_type_info.kategorie_hint` greift nur wenn **keine** Kategorie gesetzt ist. Schwächster Override. |
| **9** | **Keyword-Rules** | 16282 | `apply_keyword_rules()` — 40+ Regeln aus categories.yaml (z.B. "Gehaltsabrechnung"→business, "Bordkarte"→reisen). |
| **10** | **Lernregeln aus DB** | 16283 | `apply_lernregeln_from_db()` — manuell bestätigte Klassifikationen aus der `lernregeln`-Tabelle. |
| **11** | **Konfidenz-Aggregation** | 16284 | `aggregate_konfidenz()`: Minimum aller Per-Feld-Konfidenzen → Gesamtkonfidenz (hoch/mittel/niedrig). |
| **12** | **Datums-Fallback Dateiname** | 16287–16293 | Wenn kein `rechnungsdatum` → YYYYMMDD aus Dateinamen-Prefix (auch DDMMYYYY Scanner-Format). |
| **13** | **Konfidenz-Gate** | 16297–16302 | `konfidenz_category=niedrig` → category_id+type_id=null → **00 Inbox**. Kein Raten in den Vault. |

### 5.1 Konfidenz-Modell

`aggregate_konfidenz()` (Zeilen 14588–14610):

```
RANK: {"hoch": 2, "mittel": 1, "niedrig": 0}
Gesamt = min(per_feld_werte)
```

Nur **gesetzte** Felder zählen (type_id ohne Wert, adressat ohne Wert, datum ohne Wert werden ignoriert).  
Fallback: altes `konfidenz`-Einzelfeld → `konfidenz_source=fallback`.  
Ohne jeden Wert → `"niedrig"`.

---

## 6. Summarization

`summarize_document()` (Zeilen 14184–14312) läuft für **jedes** erfolgreich klassifizierte Dokument.  
Modell: `qwen3:4b-instruct` (env `SUMMARIZE_MODEL`).  
Prompt aus `summarize_prompt.txt` (in dispatcher-config/).  
Erwartete Ausgabe:

```json
{
  "title": "Kurzer Dokumententitel",
  "summary": "1–2 Sätze Zusammenfassung",
  "key_points": ["Punkt 1", "Punkt 2"],
  "structure": "Gliederung des Dokuments",
  "kennzahlen": {"betrag": "...", "datum": "..."}
}
```

Das Ergebnis wird unter `result["_summary"]` gespeichert und später in den MD-Body geschrieben (ersetzt OCR-Rohdaten, wenn Summary vorhanden).

---

## 7. Datenbank + Vault

### 7.1 DB-Schritte (in Reihenfolge, Zeilen 16318–16343)

| Schritt | Funktion | Wirkung |
|---------|----------|---------|
| 1 | `save_to_db(file_path, result)` | Schreibt in `dokumente`-Tabelle (inkl. Hash-Duplikat-Prüfung) |
| 2 | `save_klassifikation_historie()` | Schreibt LLM-Rohdaten in `klassifikation_historie` |
| 3 | `_write_kk_leistungen_db()` | Extrahiert KV-Positionen → `kk_leistungen.db` |
| 4 | `_write_immobilien_db()` | Extrahiert Immo-Daten → `immobilien.db` |
| 5–7 | Background-Threads | KFZ/AV/SV-Skills parallel (wenn Kategorie passt) |

### 7.2 move_to_vault()

`move_to_vault()` (Zeilen 14806–14887):

1. **Dateiname bauen** (`build_clean_filename`, Zeilen 14667–14678):
   - `YYYYMMDD_Absender_Dokumenttyp.md`
   - Datum-Priorität: LLM → Dateiname-Prefix → `_NODATE_`
2. **Zielpfad bestimmen** (`build_vault_path`, Zeilen 124–150):
   - `{vault_folder}/{type_subfolder Person}/[{year}]/{filename}`
   - Bei `_NODATE_` → Kategorie-ID auf "" → `00 Inbox`
3. **Kollisionsvermeidung**: `_2`, `_3` Suffix bei Namenskonflikt
4. **PDF verschieben**: `file_path` → `VAULT_PDF_ARCHIV/{clean_name}.pdf`
5. **MD schreiben**: `_write_vault_md()` — Frontmatter + Body
6. **Summarizer-Trigger**: `_trigger_summarizer()` im Hintergrund

---

## 8. Frontmatter: Wer schreibt was?

### 8.1 `_build_frontmatter()` (Zeilen 14737–14803)

Jedes Feld mit **exakter Herkunft**:

| Frontmatter-Feld | Primäre Quelle | Fallback | Format |
|-----------------|----------------|----------|--------|
| `Datum_original` | Dateiname-Prefix (YYYYMMDD) | — | `2026-03-15` |
| `datum` | LLM `rechnungsdatum` | Dateiname-Fallback (Override #12) | `"15.03.2026"` |
| `absender` | LLM `absender` | `absender_match.name` (Override #3) | `"Gothaer KV"` |
| `adressat` | LLM `adressat` | Cod.Fiscale (Override #1) / Absender-Default (Override #2) | `"Reinhard"` |
| `thema` | `{absender} {typ_label}` | — | `"Gothaer KV Leistungsabrechnung"` |
| `kategorie` | LLM `category_label` | Überschrieben durch Override #7, #8, #9, #10 | `"Krankenversicherung"` |
| `tags` | `[type_id]` (Option B: nur Typ, nicht Kategorie) | + ggf. `immo-objekt-tag` | `[leistungsabrechnung]` |
| `zusammenfassung` | `result.zusammenfassung` | — (Wilson-only, nicht Pipeline-Summary) | `"..."` |
| `betrag` | LLM `rechnungsbetrag` | — | `"33,06 EUR"` |
| `faellig` | LLM `faelligkeitsdatum` | — | `"01.04.2026"` |
| `sprache` | `langdetect` | — nur wenn != "de" | `"it"` |
| `original` | `Anlagen/{pdf_filename}` | — immer | `"Anlagen/20260315_Gothaer_KV_Leistungsabrechnung.pdf"` |
| `erstellt` | `datetime.now()` | — immer | `"2026-06-28"` |

**Wichtig:** `zusammenfassung` im Frontmatter ist das **Wilson**-Feld, nicht die Pipeline-Summary. Die Pipeline-Summary (`_summary`) landet im **Body** der MD-Datei.

### 8.2 MD-Body-Struktur

```
---
{Frontmatter}
---
📎 [[Anlagen/20260315_Gothaer_KV.pdf]]

> **Wilson-Zusammenfassung (DE):** ... (nur wenn Wilson summary_de)

---

## 📝 Gothaer KV Leistungsabrechnung vom 15.03.2026

**Zusammenfassung:** ... (Pipeline-Summarizer)
**Key Points:** ...
**Dokumentstruktur:** ...
**Kennzahlen:** ...

<!-- SUMMARIZER_PLACEHOLDER --> (entfällt, wenn Pipeline-Summary vorhanden)
```

### 8.3 Ausgabepfad-Logik

```
{VAULT_ROOT}/                          # /data/reinhards-vault
├── 00 Inbox/                          # Unklassifiziert / niedrige Konfidenz
├── 10 Persönlich/
├── 20 Familie/
│   └── Tierarztrechnung Molly/        # type_subfolder + Person
├── 40 Finanzen/
│   ├── 2023/                          # Vorjahre
│   └── 20260315_Allianz_Versicherung.md
├── 49 Krankenversicherung/
│   ├── Leistungsabrechnung Reinhard/  # type_subfolder + Person
│   └── Arztrechnung Marion/
├── 50 Immobilien/
├── 60 Fahrzeuge/
├── 70 Italien/
├── 80 Business/
└── 99 Archiv/
```

---

## 9. Qualitäts-Check: Wann ist eine Datei vollständig?

### 9.1 Schnell-Check (Blick ins Frontmatter)

- [ ] `Datum_original` vorhanden?
- [ ] `kategorie` gesetzt und nicht leer?
- [ ] `tags` enthält mindestens einen Eintrag?
- [ ] `absender` oder `adressat` gesetzt?
- [ ] `📎 [[Anlagen/...]]` im Body — PDF-Link klickbar?
- [ ] Kein `<!-- SUMMARIZER_PLACEHOLDER -->` im Body?
- [ ] Datei liegt **nicht** unter `00 Inbox/`

### 9.2 Deep-Check (pro Datei, via dispatcher.db)

- [ ] `dokumente.ocr_chars` ≥ 150?
- [ ] `dokumente.konfidenz` ≠ `"niedrig"`?
- [ ] `dokumente.vault_pfad` zeigt auf existierende Datei?
- [ ] `dokumente.anlagen_dateiname` zeigt auf existierendes PDF?
- [ ] `rechnungsdatum` weicht ≤ 30 Tage von `Datum_original` ab?
- [ ] MD-Body enthält Zusammenfassung-Text (nicht `<!-- SUMMARIZER_PLACEHOLDER -->`)?

### 9.3 Batch-Report (SQL gegen dispatcher.db)

```sql
-- Unklassifizierte Dokumente
SELECT COUNT(*) FROM dokumente WHERE kategorie IS NULL;

-- Niedrige Konfidenz
SELECT COUNT(*) FROM dokumente WHERE konfidenz = 'niedrig';

-- MD ohne PDF-Link
SELECT COUNT(*) FROM dokumente WHERE vault_pfad IS NOT NULL AND anlagen_dateiname IS NULL;

-- Summarizer-Lücken (via grep im Vault)
grep -rl "SUMMARIZER_PLACEHOLDER" {VAULT_ROOT}/
```

### 9.4 Korrektheits-Check (Stichprobe, manuell)

- [ ] Stimmt `kategorie` mit Keyword-Regeln überein? (OCR-Text gegen categories.yaml matchen)
- [ ] Ist `adressat` plausibel? (Cod.Fiscale im Text vs. Frontmatter)
- [ ] Absender korrekt normalisiert? (Alias aus absender.yaml genutzt?)

---

## 10. Optimierungs-Empfehlungen

### Phase 1: Aufräumen 🟢

**1.1 Cache-Reader stilllegen oder umhängen**
- **Problem:** `index.db` seit 19. April eingefroren. Quelle (`text-extractor` Plugin-Cache) ist leer. Die 2.459 Einträge sind veraltet, neue PDFs fehlen.
- **Ursache:** Der cache-reader indexiert den Obsidian Text-Extractor-Cache (`docker-compose.yml` Zeile 126), nicht die Docling-OCR-Ergebnisse. Die Docling-Ergebnisse gehen in `dispatcher.db` bzw. die `.md`-Dateien im Vault.
- **Empfehlung:** Cache-Reader deaktivieren, Volltextsuche über Dispatcher-Cache-Seite konsolidieren (eine Codebasis weniger).

**1.2 `_NODATE_`-Marker systematisch auswerten**
- **Problem:** Dateien ohne ermittelbares Datum landen still in der Inbox.
- **Lösung:** Wöchentlicher Cron-Job, der `_NODATE_*`-Dateien listet und per Telegram meldet.

### Phase 2: Override-Kaskade entschlacken 🟡

**2.1 Override-Logging verstärken**
- **Problem:** 13 Override-Schritte — bei Fehlklassifikation ist unklar, welcher Schritt schuld ist.
- **Lösung:** Pro Override loggen, welches Feld von welchem Wert auf welchen geändert wurde. Danach redundante Regeln identifizieren und entfernen.
- **Konkretes Risiko:** `apply_keyword_rules` (Schritt 9) überschreibt möglicherweise den LLM-Output, der durch die Schritte 1–8 bereits validiert und korrigiert wurde. Ist das gewollt?

**2.2 Konfidenz-Modell vereinfachen**
- **Problem:** 5 Per-Feld-Konfidenzen + 1 aggregierte + 1 Legacy-Fallback → 7 Werte, die dasselbe aussagen sollen.
- **Lösung:** Reduzieren auf 3 Stufen (sicher/unsicher/unbekannt). `konfidenz_source` als Pflichtfeld (`llm`|`regex`|`absender_db`|`keyword_rule`).

**2.3 Prompt-Komplexität reduzieren**
- **Problem:** ~3000 Tokens Prompt-Overhead bevor das Dokument beginnt. KV-Spezialregeln sind immer dabei, auch bei Nicht-KV-Dokumenten.
- **Lösung:** KV-Regeln nur einblenden, wenn `absender_match` einen KV-Absender erkennt. Negativregeln als Post-Processing statt Prompt-Teil.

### Phase 3: Verarbeitungsqualität 🟡

**3.1 Zweitmeinung bei niedriger Konfidenz**
- **Problem:** `konfidenz=niedrig` → Inbox, aber keine weitere Aktion. Dokument bleibt unklassifiziert.
- **Lösung:** Anderes Modell (qwen3:4b-instruct) als Zweitmeinung befragen. Bei Übereinstimmung → übernehmen. Bei Abweichung → Inbox mit beiden Vorschlägen.

**3.2 Summarizer-Lücke schließen**
- **Problem:** `_trigger_summarizer()` ruft `vault_summarizer.py --test` auf — das **schreibt nicht**. Der Batch-Summarizer (`--run`) muss separat gestartet werden.
- **Lösung:** Auto-Trigger auf `--run` umstellen. Fortschritt in `dispatcher.db` tracken.

### Phase 4: Monitoring 🟡

**4.1 Completeness-Dashboard**
- **Problem:** Kein Überblick, wie viele Dokumente "fertig" vs. "unvollständig" sind.
- **Lösung:** Neuer Tab im Dispatcher (`/completeness`) mit den Kennzahlen aus Abschnitt 9.

**4.2 Pipeline-Health-Metriken**
- OCR-Fehlerrate (7 Tage)
- LLM-Retry-Rate
- Inbox-Rate (Ziel: <5%)
- Durchschnittliche Verarbeitungszeit pro Dokument

---

## 11. Dokumentations-Audit

### 11.1 Zu behalten (Stand Juni 2026)

| Datei | Stand | Status |
|-------|-------|--------|
| `README.md` | Mai 2026 | ⚠️ Veraltet: Modelle (DeepSeek→qwen3), Email-Pfad fehlt, neue Dashboards fehlen |
| `ARCHITEKTUR.md` | 20. Mai | ⚠️ Veraltet: Modelle (mistral-nemo→qwen3), Phasen-Status prüfen |
| `KONZEPT_neue_skills.md` | 17. Mai | ✅ Kanonische Spec für KFZ/AV/SV |
| `ANFORDERUNGEN_immobilien_db.md` | 17. Mai | ✅ Autoritative Spec |
| `VAULT_ANALYSE.md` | 7. Mai | ✅ Historische Referenz |
| `WILSON.md` | 15. Mai | ✅ Aktuell |
| `summarize_prompt.txt` | undatiert | ✅ Aktiv genutzt |

### 11.2 Bereits gelöscht (veraltete Backups)

| Datei | Grund |
|-------|-------|
| `dispatcher/dispatcher.py.20260624_145539.pre-ocr-fix` | OCR-Fix in git (9e1d69c) |
| `dispatcher/dispatcher.py.bak-enex` | 50% kleiner als aktuell, ohne Skill-Integration |
| `docker-compose.yml.20260624_145539.pre-ocr-fix` | Tessdata-Dockerfile ist aktiv |
| `.env.bak-20260502` | Übersetzungsmodell entfernt |
| `dispatcher-config/._*.yaml` (3×) | Apple-Double-Metadaten |

---

## Anhang A: Abbruchkriterien (Wann landet ein PDF in der Inbox?)

| Nr | Bedingung | Zeile |
|----|-----------|-------|
| A1 | OCR < 150 Zeichen | 15985 |
| A2 | Docling-Konvertierung fehlgeschlagen | 15973 |
| A3 | LLM-Klassifikation ergibt None/null category_id (auch nach Retry) | 16245 |
| A4 | `category_id` nicht in categories.yaml (Halluzination) | 16183 |
| A5 | `konfidenz_category = "niedrig"` | 16297 |
| A6 | Dateiname hat keinen Datums-Präfix → `_NODATE_` → Inbox | 14843 |
| A7 | Wilson-Sidecar ohne gültiges `_force_stem`-Datum | 14835 |

## Anhang B: Verarbeitungs-Dauer (Schätzwerte)

| Schritt | Dauer | Faktor |
|---------|-------|--------|
| Docling OCR (born-digital) | 2–10s | PDF-Größe |
| Docling OCR (force_ocr) | 30–120s | Seitenzahl |
| Header/Identifier/Doctype | <1s | deterministisch |
| LLM-Klassifikation | 5–30s | qwen2.5:7b, Prompt-Länge |
| LLM-Retry | 5–15s | kürzerer Text |
| Summarization | 5–20s | qwen3:4b-instruct |
| DB + Vault-Move | <1s | I/O |
| **Gesamt (Normal)** | **~15–60s** | |
| **Gesamt (force_ocr)** | **~60–180s** | |
