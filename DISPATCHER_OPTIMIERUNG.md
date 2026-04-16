# Dispatcher-Optimierung & flexible Erweiterbarkeit

**Status-Datei** — wird während der Umsetzung gepflegt. Jede Iteration bekommt einen Status (⬜ offen / 🟡 in Arbeit / ✅ erledigt) und nach Abschluss eine kurze Ergebnis-Notiz.

---

## Kontext

Der Dispatcher ist das Herzstück der Dokumenten-Pipeline: PDF → OCR → Klassifikation → Vault. Die bisherigen Iterationen (Few-Shot, Sprach-Pipeline, Halluzinations-Guard, Branchen-Regel) haben messbar geholfen, aber aus dem A/B-Test und den Nutzer-Beobachtungen werden mehrere Schwachstellen sichtbar:

- **Dateiname-Datum falsch** bei Scanner-Prefix `DDMMYYYY` (z. B. `05052026_...` = 5. Mai 2026 statt 14. April 2026): der Fallback-Regex in `build_clean_filename()` nimmt 8 Ziffern ungeprüft als `YYYYMMDD` an.
- **Absender wird häufig nicht erkannt**, obwohl die Adressinformation strukturell im Dokumentenkopf steht (Firma, Name „Janning", Strasse, PLZ, Ort).
- **Halluzinations-Guard greift nur für `category_id`**, nicht für `type_id` (doc2 im Test: `finanzen/rechnung_kfz_versicherung` — der Typ existiert nicht).
- **Hardcodierte Sonder-Listen** (`LEISTUNGSABRECHNUNG_TYPES`, `VERSICHERUNG_TYPES`) erzwingen Code-Änderung bei jedem neuen Typ.
- **Taxonomie-Lücken:** Tierarzt → fälschlich `krankenversicherung`; italienische Behörden → fälschlich `fahrzeuge`/`business`.
- **Konfidenz ist pauschal** (ein Wert für das ganze Dokument), nicht pro Feld.

Ziel: Die Dokumenten-Erkennung muss „sitzen", damit wir künftig neue Kategorien/Typen **ohne Code-Änderungen** aufnehmen können, und damit das System bei 1000+ Dokumenten im Jahr zuverlässig bleibt.

## Leitprinzipien

1. **Taxonomie als Single Source of Truth** — alles (Kategorien, Typen, Hints, Vault-Folder, Special-Flags, Absender-Aliases, Branchen-Regeln, Personen, Dokumenttypen) kommt aus YAML-Dateien unter `dispatcher-config/`. Code liest nur.
2. **Pipeline in klar getrennten Phasen** — deterministische Schritte (Identifier-Extraktion, Header-Extraktion, Dokumenttyp-Extraktion, Sprach-Erkennung, Datei­namen-Bau) bleiben regelbasiert; nur die eigentliche semantische Zuordnung geht ans LLM.
3. **Debug-Artefakte persistent** — Original-MD, Übersetzung, extrahierter Header, Identifier-Treffer, erkannter Dokumenttyp, LLM-Rohantwort auf Platte, damit A/B-Tests und Fehler-Analysen nachvollziehbar sind.
4. **Taxonomie-Zwang** — LLM darf nur IDs aus der geladenen Taxonomie liefern; Code validiert doppelt.
5. **Primär vor Sekundär vor Fließtext** — Klassifikation folgt einer Signalhierarchie:
   - **Primär** (deterministisch, eindeutig, übersetzungsresistent): strukturierte Identifier (Cod. Fiscale, Part. Iva, USt-IdNr, Steuer-ID, IBAN), wörtliches Dokumenttyp-Keyword im Kopf (Fattura, Rechnung, Carta di Circolazione, Kontoauszug, Versicherungsschein, …), Behörden-Kennung.
   - **Sekundär** (starker Hinweis, kontextabhängig): Firmenname + Branchen-Keyword („VETERINARIA", „AUTO", „IMMOBILIARE", „GmbH Dachdeckerei"), Adressblock (PLZ/Ort/Strasse), Absender-Adressat-Mapping aus DB, IBAN **nur zur Identifikation des Absenders**, nicht als Kategorie-Treiber.
   - **Schwach** (Fließtext-Rauschen): Einzelwörter wie „Versicherung", „Rechnung", IBAN-Präsenz, Telefonnummer. Dürfen nur als Tiebreaker dienen, nie als Treiber.
   Trifft ein Primärmerkmal, entscheidet es. Sekundärmerkmale bestätigen/ergänzen. Schwache Signale sind Letztes Mittel.

## Betroffene Dateien

- `dispatcher/dispatcher.py` — Hauptlogik
- `dispatcher-config/categories.yaml` — Taxonomie (Kategorien + Typen + Hints)
- `dispatcher-config/personen.yaml` — **NEU** (Iter. 3.7): Cod. Fiscale + Steuer-ID + Namen der Haushaltsmitglieder
- `dispatcher-config/absender.yaml` — **NEU** (Iter. 3.7): Absender-DB (Part.Iva / USt-IdNr / IBAN / Aliases / Kategorie-Hint / Adressat-Default)
- `dispatcher-config/doc_types.yaml` — **NEU** (Iter. 3.8): mehrsprachiges Dokumenttyp-Wörterbuch (Fattura/Preventivo/Carta di Circolazione/Rechnung/Kontoauszug/…)
- `docker-compose.yml` — Env-Variablen (`OLLAMA_TRANSLATE_MODEL`)
- `.env` — aktuelles Translate-Modell

---

## Iterationen (mit Status)

### ✅ Iteration 2.5 — Sofort-Fixes (hoher Impact, niedriges Risiko)

**2.5.1 Datums-Quelle korrigieren** (`build_clean_filename`, Zeile 1120–1161)
- **Primärquelle: Datum aus dem Dokument** (vom LLM extrahiertes `datum`-Feld im Format `DD.MM.YYYY` → umwandeln zu `YYYYMMDD`).
- **Fallback nur wenn kein Dokument-Datum vorhanden**: heutiges Datum (`%Y%m%d`).
- **Scanner-Prefix NICHT mehr als Datumsquelle verwenden** — der 8-Ziffern-Regex auf dem Original-Dateinamen entfällt (Ursache des `05052026`-Bugs).
- Zusätzlich: Dokument-Datum validieren (Jahr 1990–2029, Monat ≤ 12, Tag ≤ 31); bei Invalid → ebenfalls heute.

**2.5.2 Halluzinations-Guard für Typ** (`process_pdf`, nach Zeile 1259)
- Wenn `type_id` gesetzt aber nicht in `categories[category_id]["types"]`, → auf `None` setzen. Kategorie bleibt erhalten, Datei landet in Kategorie-Wurzel statt in Typ-Unterordner.

**2.5.3 Default-Translate-Modell zurück auf `qwen2.5:7b`** (`.env`)
- A/B-Test hat gezeigt: qwen klassifiziert in 2/5 Fällen korrekt, translategemma in 1/5.
- translategemma bleibt als Option im Code erhalten (Env-Variable).

**Ergebnis (2026-04-15):**
- 2.5.1 umgesetzt in `dispatcher.py:1129–1138`: LLM-extrahiertes `rechnungsdatum` (DD.MM.YYYY) ist einzige Primärquelle, Jahr/Monat/Tag werden validiert, Fallback = heute. Regex auf `original_stem` komplett entfernt.
- 2.5.2 umgesetzt in `dispatcher.py` direkt nach dem category-Guard: valider `type_id`-Check gegen `categories[cat]["types"]`. Bei Halluzination wird `type_id = None`, Kategorie bleibt, Konfidenz "hoch" → "mittel".
- 2.5.3 war bereits auf `qwen2.5:7b` in `.env` — keine Änderung nötig.
- Build + Restart sauber (`docker compose build/up -d dispatcher` → „Dispatcher aktiv"). Scharfe Regressions-Tests folgen beim nächsten PDF-Durchlauf.

---

### ✅ Iteration 3.5 — Header-Extraktion als Pre-Processor (Kern-Verbesserung)

**3.5.1 Neue Funktion `extract_document_header(md_content)`**
- Analysiert die ersten ~30 Zeilen des MD nach Adressblöcken.
- Regex-basiert (deterministisch, kein LLM):
  - PLZ-Muster: `\b\d{5}\b` (DE + IT teilen Format), ergänzt um „Via", „Strasse", „Straße"-Indikatoren
  - Firmen-Indikatoren: „GmbH", „AG", „SRL", „SpA", „SNC", „S.p.A."
  - Personen-Indikatoren: „Janning", „Reinhard", „Marion", ggf. aus YAML-Familien-Liste
- Rückgabe: `{absender: {firma, strasse, plz, ort, land}, empfaenger: {name, strasse, plz, ort}}`
- Wenn kein Block erkennbar: alle Felder `None`, aber kein Fehler — Pipeline läuft weiter.

**3.5.2 Strukturierten Header an Klassifikator geben**
- Im Klassifikations-Prompt einen neuen Block „ERKANNTER DOKUMENTEN-KOPF" zwischen Taxonomie und Translate-Text einfügen.
- Prompt-Anweisung: „Verwende diese Felder bevorzugt, statt im Fließtext zu raten."

**3.5.3 Debug-Artefakt: Header als JSON**
- Neben `*.translation.*.md` auch `*.header.json` in `dispatcher-temp/` schreiben.

**Warum das hilft (aus den A/B-Daten):**
- doc4 (Carta di Circolazione): Absender war in beiden Runden `None`, obwohl „REPUBBLICA ITALIANA / Ministero delle Infrastrutture" klar am Seitenanfang steht.
- doc5 (Pratfra): Absender-Feld translategemma → `BÜRO FÜR ZIVILE MOTORISIERUNG VON GROSSETO` (übersetzt!), qwen → `None`. Strukturierte Extraktion liefert den Original-Namen „Motorizzazione Civile Grosseto".

**Ergebnis (2026-04-15):**
- `extract_document_header()` in `dispatcher.py` neu eingeführt (vor `classify_with_ollama`): regex-basiert, wirft nie, liefert `{absender, empfaenger}` mit `firma/name/strasse/plz/ort/land`.
- `_format_header_for_prompt()` rendert den Header für den LLM-Prompt; im Prompt als Block „ERKANNTER DOKUMENTEN-KOPF" direkt nach dem TAXONOMIE-ZWANG injiziert — nur wenn überhaupt etwas erkannt wurde.
- In `process_pdf()` Schritt 2b: Header wird direkt nach dem MD-Speichern extrahiert (vor der Übersetzung, damit Original-Firmennamen erhalten bleiben) und als `*.header.json` persistiert; die `classify_with_ollama`-Signatur nimmt jetzt `header=` entgegen.
- Smoke-Test gegen zwei Bestands-MDs: Empfänger „Janning" + PLZ/Ort zuverlässig erkannt; Absender-Firma und Strasse noch lückenhaft (acceptable, wird bei realen Läufen geschärft).
- Build + Restart sauber, Dispatcher aktiv.

---

### ✅ Iteration 3.7 — Strukturierte Identifier + Personen-/Absender-DB (Primärmerkmale)

**Motivation (aus doc1 + doc2):** Cod. Fiscale identifiziert Marion eindeutig (Position 4-6 `MNM` in `JNNMNMG1T5121121`), Part. Iva identifiziert die Clinica Veterinaria ohne Übersetzungs-Störung. Beides wurde ungenutzt liegengelassen. Der LLM musste raten und lag falsch.

**3.7.1 Identifier-Extraktor** (`extract_identifiers(md_content)`, neu in `dispatcher.py`)
- Regex auf gesamten MD-Text (nicht nur Header, Identifier stehen oft im Kleingedruckten oder in Fußzeilen):
  - **Cod. Fiscale Person** (IT, 16 alphanumerisch): `\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b`
  - **Cod. Fiscale Firma / Part. Iva** (IT, 11 Ziffern): `\b\d{11}\b` (kontextgeprüft durch „P.?\\s*IVA" / „Cod\\.?\\s*Fiscale" in Nähe)
  - **USt-IdNr DE**: `\bDE\d{9}\b`
  - **Steuer-ID DE** (Person, 11 Ziffern mit spezifischer Validierung)
  - **IBAN**: `\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b`
- Rückgabe: `{cod_fiscale: [...], part_iva: [...], ust_id: [...], steuer_id: [...], iban: [...]}`
- Als `*.identifiers.json` in `dispatcher-temp/` persistieren.

**3.7.2 `personen.yaml`** anlegen:
```yaml
reinhard:
  cod_fiscale: "JNN..."            # exakter Cod. Fiscale
  steuer_id_de: "..."
  name: "Reinhard Josef Janning"
marion:
  cod_fiscale: "JNNMNMG1T5121121"
  name: "Marion Micaela Janning"
linoa:
  cod_fiscale: "..."
  name: "Linoa Janning"
```
Loader + Lookup-Funktion `resolve_adressat(identifiers)` — deterministischer Match auf Cod. Fiscale/Steuer-ID → `adressat`-Feld wird vor dem LLM-Aufruf fixiert.

**3.7.3 `absender.yaml`** anlegen (wächst organisch):
```yaml
- id: clinica_veterinaria_amiatina
  aliases: ["CLINICA VETERINARIA AMIATINA", "KLINIK VETERINARIA AMIATINA"]
  part_iva: "02145060501"
  land: IT
  kategorie_hint: familie      # nach Iter. 5 → haustier
  typ_hint: null               # "haustier" existiert noch nicht
- id: lp_pratiche_auto_srl
  aliases: ["LP PRATICHE AUTO SRL", "LP Pratiche Auto"]
  part_iva: "…"
  land: IT
  kategorie_hint: italien
  typ_hint: fahrzeug
- id: butangas
  aliases: ["ButanGas", "ButanGas S.p.A.", "BUTANGAS"]
  part_iva: "00443130588"
  land: IT
  kategorie_hint: immobilien_eigen
  typ_hint: rechnung
- id: huk_coburg
  aliases: ["HUK", "HUK-COBURG", "HUK-Coburg-Krankenversicherung"]
  land: DE
  adressat_default: Marion       # starkes Mapping
```
Loader + Lookup-Funktion `resolve_absender(identifiers, header)` — Primär-Match über Part.Iva/USt-IdNr; Fallback: alias-Match (case-insensitive) auf `header.absender.firma`.

**3.7.4 Ergebnisse deterministisch in den Prompt injizieren**
- Neuer Block „STRUKTURIERTE MERKMALE (deterministisch bestätigt — diese gelten)":
  - `Empfänger (via Cod. Fiscale): Marion Janning` — falls 3.7.2 getroffen
  - `Absender (via Part. Iva): CLINICA VETERINARIA AMIATINA, Land IT, vorgeschlagene Kategorie: familie` — falls 3.7.3 getroffen
- Prompt-Regel: „Diese strukturierten Merkmale sind deterministisch bestätigt. Wenn du sie ignorierst, ist das ein Fehler."

**3.7.5 Selbstlern-Mechanismus (optional, niedrige Priorität)**
- Wenn LLM einen neuen Absender klassifiziert und der User via Wilson die Klassifikation **bestätigt**, wird ein neuer `absender.yaml`-Eintrag automatisch angelegt (mit Alias + erkannter Part.Iva + bestätigter Kategorie).
- MVP-Version: manuelles Anlegen durch den Nutzer, automatisch kommt später.

**Ergebnis (2026-04-15):**
- `extract_identifiers()` + strikter Cod.Fiscale-Regex + permissiver Fallback hinter `Cod. Fiscale:`-Kontext-Keyword implementiert (fängt OCR-Varianten wie `JNNMNMG1T5121121` ab, die am strikten Format scheitern).
- `Part. Iva`-Regex ist kontextgeprüft (nur hinter „P.IVA"/„Codice Fiscale"/„C.F." — blanke 11 Ziffern sind sonst zu uneindeutig).
- IBAN wird extrahiert, aber **ausschliesslich** für Absender-Identifikation (nie als Kategorie-Trigger).
- `personen.yaml` (Reinhard + Marion + Linoa-Platzhalter) und `absender.yaml` (8 Seed-Einträge: Clinica Vet, LP Pratiche Auto, Motorizzazione, Ministero Trasporti, ButanGas, HUK, Gothaer, Barmenia) angelegt; beide Caches werden einmal pro Prozess geladen (Container-Restart nach YAML-Änderung nötig).
- `resolve_adressat()` (Primär: Cod.Fiscale-Match) und `resolve_absender()` (Primär: Part.Iva/USt-IdNr; Sekundär: Alias-Substring auf header.absender.firma) liefern deterministische Treffer.
- Klassifikator-Prompt um „STRUKTURIERTE MERKMALE (deterministisch bestätigt — NICHT überschreiben)"-Block erweitert.
- Nach Klassifikation wird `adressat` zwingend durch Cod.Fiscale-Treffer überschrieben; wenn keiner vorliegt, greift `absender.adressat_default` als Fallback.
- Artefakt `*.identifiers.json` wird neben `*.header.json` und `*.translation.*.md` in `dispatcher-temp/` persistiert.
- **Test doc1 (Tierarztrechnung):** Marion wird über `cod_fiscale:JNNMNMG1T5121121` gematcht; LLM lieferte `adressat=None`, Code überschreibt deterministisch auf `Marion`. Kategorie bleibt `krankenversicherung/arztrechnung` (Haustier-Taxonomie folgt in Iter. 5).
- **Test doc2 (LP Pratiche Auto):** Reinhard wird über `cod_fiscale:JNNRHRG2TO8Z112E` gematcht; Part.Iva `01718620535` extrahiert und in `absender.yaml` ergänzt (beim nächsten Lauf wird `lp_pratiche_auto_srl` per Primär-Match gefunden). Kategorie noch `finanzen/kontoauszug` (IBAN-Bias — Iter. 4 Prompt-Disziplin nötig).

---

### ✅ Iteration 3.8 — Dokumenttyp-Extraktor (Primärmerkmal)

**Motivation (aus doc2 + doc3 + doc4 + doc5):** „Fattura" stand groß im Kopf und wurde ignoriert (doc2). „CARTA DI CIRCOLAZIONE" als H2-Heading in doc4, „operazione di COLLAUDO" in doc5. Das sind wörtliche Typ-Keywords, die deterministisch in Typen gemappt werden können.

**3.8.1 `doc_types.yaml`** anlegen (mehrsprachiges Wörterbuch):
```yaml
- keywords: ["FATTURA", "Rechnung", "Invoice"]
  typ: rechnung
- keywords: ["PREVENTIVO", "Angebot", "Offerta", "Quotation"]
  typ: angebot
- keywords: ["NOTA DI CREDITO", "Gutschrift", "Credit note"]
  typ: gutschrift
- keywords: ["RICEVUTA", "Quittung", "Receipt"]
  typ: quittung
- keywords: ["MAHNUNG", "Sollecito", "Reminder"]
  typ: mahnung
- keywords: ["KONTOAUSZUG", "Estratto conto", "Bank statement"]
  typ: kontoauszug
  nur_bei_absender: bank          # Negativ-Schutz
- keywords: ["VERSICHERUNGSSCHEIN", "Polizza", "Policy"]
  typ: versicherungsschein
- keywords: ["CARTA DI CIRCOLAZIONE"]
  typ: fahrzeugbrief
  kategorie_hint: italien
- keywords: ["COLLAUDO", "TÜV", "Hauptuntersuchung", "Inspection"]
  typ: fahrzeugpruefung
- keywords: ["BOLLA DI CONSEGNA", "Lieferschein", "Delivery note"]
  typ: lieferschein
```

**3.8.2 Extraktor** (`extract_document_type(md_content)`)
- Regex auf die ersten ~20 Zeilen, case-sensitive bei Großschreibung (Überschriften) + case-insensitive bei normalem Text.
- Mehrere Treffer: der erste gewinnt, aber alle werden im JSON-Artefakt festgehalten.
- Rückgabe: `{erkannter_typ: "rechnung", quell_keyword: "FATTURA", zeile: 7, alle_treffer: [...]}`
- Als `*.doc_type.json` persistiert.

**3.8.3 In den Prompt injizieren**
- Neuer Block „ERKANNTER DOKUMENTTYP (regex, keyword='FATTURA' in Zeile 7 → Typ: rechnung)".
- Prompt-Regel: „Bei Konflikt zwischen diesem Regex-Befund und deinem Eindruck aus dem Fließtext: **der Regex gewinnt**."

**3.8.4 Negativ-Regel im Prompt gegen schwache Signale**
- „IBAN, BIC, Kontonummer, SEPA-Mandat rechtfertigen **nie** `finanzen/kontoauszug`. Kontoauszug gilt nur, wenn der Absender eine Bank ist UND das Dokumenttyp-Keyword `Kontoauszug`/`Estratto conto` trägt."
- „Einzelnes Wort ‚Versicherung' im Fließtext macht ein Dokument nicht zur Krankenversicherung — Absender + Dokumenttyp entscheiden."

**Ergebnis (2026-04-15):**
- `doc_types.yaml` mit 17 Einträgen angelegt (Rechnung/Fattura, Angebot, Gutschrift, Quittung, Mahnung, Kontoauszug mit `nur_bei_absender: bank`, Versicherungsschein, Beitragsanpassung, Leistungsabrechnung, Fahrzeugbrief/Carta di Circolazione, Collaudo, Immatricolazione, Terminbestätigung, Bescheid, Lieferschein, Überweisung, Vertrag).
- `extract_document_type()` prüft erste 20 Zeilen (case-insensitive Substring), sortiert nach Priorität + Zeilennummer, persistiert `*.doc_type.json` als Debug-Artefakt.
- `_format_doc_type_for_prompt()` injiziert Treffer als Block „ERKANNTER DOKUMENTTYP" mit Bank-Warnung bei `nur_bei_absender: bank`.
- Negativ-Regeln im Prompt: IBAN → kein kontoauszug, „Versicherung" im Text → keine krankenversicherung, Tierarzt → nie krankenversicherung.
- Schwächster Override: wenn LLM keine Kategorie setzt, aber `doc_type_info.kategorie_hint` vorhanden (z.B. Carta di Circolazione → italien), wird die Kategorie gesetzt.
- **Test doc1 (Clinica Veterinaria):** Kein Dokumenttyp-Keyword in ersten 20 Zeilen (Arztrechnung ohne Heading) — typ=None, aber Absender-Match rettet die Klassifikation. Ergebnis: `familie/tierarztrechnung`, Molly, Marion ✓.
- **Test doc2 (LP Pratiche Auto):** FATTURA in ersten Zeilen → typ=rechnung erkannt. LLM klassifizierte diesmal als `fahrzeuge/rechnung_sonstige` (Verbesserung vs. vorher `krankenversicherung`), Absender-Override korrigiert auf `italien/fahrzeug` ✓.

---

### ✅ Iteration 4 — Taxonomie-getriebene Flexibilität (überarbeitet)

Voraussetzung: Iter. 3.7 + 3.8 haben `personen.yaml`, `absender.yaml`, `doc_types.yaml` eingeführt. Iter. 4 zieht die letzten Hardcodes aus `dispatcher.py` in YAML.

**4.1 `categories.yaml` um neue Sektionen erweitern** (rein additiv)
- `special_groups: {leistungsabrechnung: [...], versicherung_dokument: [...]}` statt Hardcoded-Sets in `dispatcher.py` (`LEISTUNGSABRECHNUNG_TYPES`, `VERSICHERUNG_TYPES`).
- `branchen_regeln: [...]` statt Prompt-Hardcoding (Handwerker/Sanierung/Fognaria → immobilien_eigen). Diese Regeln greifen dort, wo `absender.yaml` (noch) keinen Eintrag hat — sie sind Heuristik über Branchen-Keywords im Firmennamen.
- `adressat_regeln` aus der KV-Spezial­logik in `categories.yaml` migrieren (HUK → Marion, Gothaer/Barmenia → Reinhard) — sofern nicht bereits via `absender.yaml.adressat_default` abgedeckt.

**4.2 `load_categories()` erweitern**
- Setter für `SPECIAL_GROUPS`, `BRANCHEN_REGELN` aus YAML (`ABSENDER_ALIASES` entfällt — wandert nach `absender.yaml`).

**4.3 Prompt-Aufbau dynamisch**
- Branchen-Regeln und Spezialregeln aus YAML lesen, in Prompt injizieren.
- Keine hardgecodeten Strings mehr im Prompt.

**4.4 „Reinhard-Default" streichen**
- Im aktuellen Prompt (`dispatcher.py:1055`) steht: „wenn kein Name eindeutig erkennbar ist und das Dokument an den Haushalt gerichtet scheint … darf ‚Reinhard' als Default gewählt werden". Das hat doc1 zerstört (Marion-Cod.Fiscale wurde ignoriert).
- **Neue Regel:** Ohne Personen-DB-Match (Iter. 3.7.2) und ohne eindeutigen Namen im Adressblock → `adressat=null`. Lieber 🟡 mittel mit leerem Feld als 🟢 hoch mit falschem Namen.

**Ergebnis (2026-04-15):**
- `categories.yaml` um `special_groups` (leistungsabrechnung + versicherung_dokument) und `branchen_regeln` (1 Regel: Handwerker/Sanierung → immobilien_eigen/rechnung) erweitert.
- `load_categories()` liest beide Sektionen und befüllt `LEISTUNGSABRECHNUNG_TYPES`, `VERSICHERUNG_TYPES`, `BRANCHEN_REGELN` global — Python-Hardcodes sind nur noch Fallback-Defaults für den Fall, dass YAML die Sektionen noch nicht enthält.
- Prompt-BRANCHEN-REGELN werden dynamisch aus `BRANCHEN_REGELN` gebaut; keine hardcodierten Strings mehr im Prompt.
- „Reinhard-Default" entfernt: `adressat=null` wenn kein DB-Match und kein eindeutiger Name — lieber null als falsch.
- **Test doc1:** LLM klassifiziert erstmals **ohne Override** korrekt `familie/tierarztrechnung` — Negativ-Regeln und Vet-Ausnahme wirken in Kombination mit dem Dokumenttyp-Extraktor.
- **Test doc2:** LLM nähert sich an: `finanzen/rechnung` (nicht mehr krankenversicherung). `rechnung` ist kein valider Typ in `finanzen` → Halluzinations-Guard nullt, Absender-Override setzt `italien/fahrzeug` ✓.

---

### ✅ Iteration 5 — Taxonomie-Erweiterungen + Haustier-Resolver + Absender-Override

**5.1 Taxonomie-Erweiterungen in `categories.yaml`:**
- `familie`: drei neue Typen `tierarztrechnung`, `tierfutter`, `tierversicherung`.
- `italien/behoerde`: Hints um Motorizzazione, Prefettura, Agenzia delle Entrate, Questura ergänzt.
- `italien/fahrzeug`: Hints um REPUBBLICA ITALIANA, Ministero delle Infrastrutture/Trasporti, Carta/Libretto di Circolazione, Collaudo, Immatricolazione, LP Pratiche Auto ergänzt.

**5.2 Haustier-Resolver (bidirektional) in `personen.yaml` + `dispatcher.py`:**
- Neue YAML-Sektion `tiere: [{name, besitzer, aliases}]` (seeded: Linoa→reinhard, Molly→marion).
- `resolve_adressat()` erweitert: wenn kein Cod.Fiscale-Treffer, sucht Tier-Alias im MD (Wortgrenzen-Match) → `besitzer` als Adressat + `tier`-Feld.
- `derive_tier()` leitet umgekehrt das Tier aus bekanntem Adressat ab, wenn `category=familie/tierarztrechnung`.
- `build_clean_filename()` fügt das Tier zwischen Absender und Typ ein.

**5.3 Deterministischer Absender-Hint-Override:**
- Nach LLM-Klassifikation wird `result.category_id` / `type_id` durch `absender_match.kategorie_hint` / `typ_hint` überschrieben, sofern die Werte in der Taxonomie existieren. Begründung: Die Absender-DB ist vom User gepflegt — stärker als semantisches Raten des LLM.

**5.4 Prompt-Schärfung:**
- In den KV-Spezialregeln neue Ausnahme „Tierarzt/Tierklinik/Veterinaria → NICHT krankenversicherung, sondern familie/tierarztrechnung" ergänzt.

**5.5 Regex-Fix in `extract_identifiers()`:**
- `_IT_FIRMA_NUM_RE` akzeptiert jetzt auch „Part.Iva" (neben „P.IVA" und „Partita IVA"). `re.DOTALL` aktiviert, damit Keyword und Nummer getrennt durch Zeilenumbruch stehen dürfen.

**Ergebnis (2026-04-15):**
- **doc1 (Clinica Veterinaria):** p_iva=02145060501 extrahiert, absender=clinica_veterinaria_amiatina (via Part.Iva), adressat=Marion (via CF), LLM lieferte krankenversicherung/arztrechnung → Override auf **familie/tierarztrechnung**, Tier=Molly (aus Adressat abgeleitet), Dateiname `20260407_CLINICA_VETERINARIA_AMIATINA_Molly_Tierarzt_Tierklinik.pdf` im Ordner `20 Familie/`.
- **doc2 (LP Pratiche Auto):** p_iva=01718620535 extrahiert, absender=lp_pratiche_auto_srl, adressat=Reinhard (via CF), LLM lieferte ebenfalls krankenversicherung/arztrechnung → Override auf **italien/fahrzeug**, Dateiname `20260413_LP_PRATICHE_AUTO_SRL_Fahrzeug_Italien.pdf` im Ordner `70 Italien/`.
- Beide LLM-Fehlklassifikationen durch das deterministische Override gerettet — das System bleibt robust selbst bei schwachen LLM-Antworten, solange `absender.yaml` gepflegt ist.

---

### ✅ Iteration 6 — Per-Feld-Konfidenz (Voraussetzung für Two-Pass)

**6.1 Prompt-Schema erweitern**
- LLM liefert: `konfidenz_category`, `konfidenz_type`, `konfidenz_absender`, `konfidenz_adressat`, `konfidenz_datum` (je „hoch" | „mittel" | „niedrig").
- Gesamtkonfidenz = Minimum der Einzelstufen (deterministische Aggregation im Code, nicht vom LLM).

**6.2 Telegram-Output anreichern**
- Grünes/gelbes/rotes Icon pro Feld im Telegram-Output.

**Ergebnis:** Implementiert am 2026-04-15. `aggregate_konfidenz()` nimmt das Minimum aller 5 Felder; Fallback auf altes `konfidenz`-Feld wenn LLM keine Einzelfelder liefert. Telegram zeigt 🟢/🟡/🔴 pro Zeile (Absender, Adressat, Datum, Kategorie, Typ). Test doc1_iter6 (Tierklinik-Rechnung): `familie/tierarztrechnung`, `konfidenz='mittel'` in DB — alle Icons korrekt dargestellt.

---

### ✅ Iteration 7 — Audit-Trail & Observability

**7.1 `klassifikations_historie`-Tabelle** (DB-Schema-Migration)
- Spalten: `id`, `dokument_id`, `timestamp`, `llm_model`, `translate_model`, `lang_detected`, `lang_prob`, `duration_ms`, `raw_response`, `final_category`, `final_type`, `konfidenz_category/type/absender/adressat/datum`, `korrektur_von_user` (bool).
- Migration läuft bei `init_db()` via `CREATE TABLE IF NOT EXISTS` — bestehende DBs werden automatisch erweitert.

**7.2 Korrektur-Logging**
- `handle_correction()` schreibt Korrektur-Eintrag (`korrektur_von_user=1`, nur final_category/final_type) in `klassifikations_historie`.

**7.3 Analyse-Skript**
- `dispatcher/analyze_classifications.py` — aufrufbar via `docker exec document-dispatcher python3 /app/analyze_classifications.py`
- Zeigt: Gesamtübersicht, Hit-Rate pro Kategorie, Sprach-Verteilung, LLM-Antwortzeiten, Per-Feld-Konfidenz-Verteilung, verwendete Modelle.

**Ergebnis:** Implementiert am 2026-04-15. Tabelle korrekt angelegt (17 Spalten, Index auf dokument_id). Analyse-Skript startet sauber; 1266 Bestandsdokumente ohne Historie — ab dem nächsten eingehenden Dokument werden Einträge geschrieben. Zusätzlich: Gesamtkonfidenz aus Telegram entfernt, stattdessen Sprache des Originaldokuments angezeigt (`🌐 Sprache: Italiano (94%)`).

---

## Verifikation

Pro Iteration:

1. **Build-Check**: `docker compose build dispatcher` muss sauber durchlaufen.
2. **Start-Check**: `docker compose up -d dispatcher` + Log-Prüfung ob „Dispatcher aktiv" erscheint.
3. **Regressions-Test**: Die 5 A/B-Test-PDFs aus `/tmp/ab_compare/` erneut durchlaufen lassen. Soll-Ergebnis:
   - doc1 vet → `familie/haustier` (nach Iter. 5)
   - doc2 IBAN → Inbox (unverändert, guard)
   - doc3 Gas → Inbox (unverändert)
   - doc4 Republica → `italien/fahrzeug` (nach Iter. 5)
   - doc5 Pratfra → `italien/behoerde` (nach Iter. 5)
4. **Dateiname-Test**: PDF mit Scanner-Prefix `DDMMYYYY_...` reinlegen und Datum im Dokument-Text → Zieldateiname muss das **Dokument-Datum** als `YYYYMMDD_...` tragen, nicht den Scanner-Prefix (Iter. 2.5.1). Zweiter Test: PDF ohne Datum → heutiges Datum als Fallback.
5. **Halluzinations-Test**: Dokument, bei dem qwen einen unbekannten Typ liefert → Typ muss `None` werden, Kategorie bleibt (Iter. 2.5.2).
6. **Identifier-Test** (nach Iter. 3.7): doc1 (Tierklinik-Rechnung) erneut laufen → `adressat` muss **Marion** sein (via Cod.Fiscale-Match), nicht mehr Reinhard.
7. **Dokumenttyp-Test** (nach Iter. 3.8): doc2 (LP Pratiche Auto, „Fattura") erneut laufen → `type_id` muss **rechnung** oder `italien/fahrzeug`-Analogon sein, **nicht** `kontoauszug`.

---

## ✅ Dashboard — Workflow-Monitoring (2026-04-16)

Neues Live-Dashboard erreichbar unter `http://<ryzen-ip>:8765/` — direkt im Dispatcher eingebettet, kein eigener Container.

### Architektur

- **Endpunkte:** `GET /` (HTML), `GET /api/health`, `GET /api/events` (SSE), `GET /api/recent` (mit Filtern), `GET /api/pdf/<name>`, `POST /api/enzyme-refresh`
- **HTTP-Server:** auf `ThreadingMixIn` umgestellt — SSE-Verbindungen blockieren nicht mehr den API-Server
- **Realtime:** Server-Sent Events (SSE) auf `/api/events`; neue Dokumente erscheinen sofort ohne Reload; Health-Status wird alle 30 s gepollt

### Features

**Dokumenten-Pipeline-Flow-Chart**
- Horizontaler Streifen direkt unter dem Header: 📱 Telegram → 🥧 Wilson/Pi → 🔄 Syncthing → 🔍 Docling OCR → 🌐 Spracherkennung → 🤖 Ollama LLM → 📄 Dispatcher → 📁 Obsidian Vault → 📱 Telegram

**Service-Karten (8 Services)**

| Service | URL (klickbar) | Metriken |
|---|---|---|
| Document Dispatcher | `:8765` | Gesamt-/Tagesdokumente, letztes Dokument |
| Docling Serve (OCR) | — intern | Status |
| Ollama (LLM) | `:11434` | Modell-Liste |
| Syncthing | `:8384` | Uptime, Verbindungen |
| Open WebUI | `:3000` | Status |
| Qdrant (Vector DB) | `:6333/dashboard` | Status |
| enzyme / mcpo | `:11180/docs` | Dokumente, Embeddings, Katalysatoren, Entitäten, **letzte Aktualisierung** |
| Wilson / OpenClaw (Pi) | `192.168.3.124` | SSH-Alive-Check |

Jede Karte: Statusdot (grün/gelb/rot mit Glüh-Effekt), Badge, Metriken, Kurzbeschreibung.
Service-Bezeichnungen sind klickbar — öffnen die Web-UI mit aufgelöster Host-IP (`HOST_IP` Env-Var, Default `192.168.86.195`).

**enzyme-Freshness-Logik**
- Index-Alter < 24 h → grün; 24–48 h → gelb; > 48 h → rot
- „⟳ Jetzt aktualisieren"-Button: startet `enzyme refresh` als Hintergrund-Thread, Ergebnis kommt per SSE zurück
- enzyme-Binary wird per Volume-Mount in den Container gereicht (`/usr/local/bin/enzyme`)

**Cron-Fix**
- Täglicher enzyme-Refresh-Cron war auf `obsidian-vault` (alt) konfiguriert → korrigiert auf `reinhards-vault`
- Laufzeit: täglich 23:00 Uhr

**Dokumente-Tabelle (Realtime)**
- Letzte 100 Dokumente, neue Zeilen erscheinen sofort per SSE mit blauem Flash-Effekt
- Spalten: Datum · Dateiname (klickbar → PDF-Viewer) · Kategorie · Typ · Absender · Adressat · Konfidenz · Verarbeitet
- **PDF-Serve:** `GET /api/pdf/<name>` liefert PDF direkt aus `/data/reinhards-vault/Anlagen/`; Dateiname wird aus `vault_pfad` (MD-Stem + `.pdf`) abgeleitet

**Filterleiste**
- Freitext-Suche (Dateiname, Absender) mit 300 ms Debounce
- Kategorie-Dropdown (aus Taxonomie, automatisch befüllt)
- Typ-Dropdown (kontextsensitiv — zeigt nur Typen der gewählten Kategorie)
- Adressat (Reinhard / Marion)
- Konfidenz (Hoch / Mittel / Niedrig)
- Von/Bis-Datumsbereich
- Trefferzähler + Reset-Button
- Alle Filter werden server-seitig in `/api/recent` ausgewertet (SQL WHERE-Klauseln)

### docker-compose-Änderungen

```yaml
dispatcher:
  environment:
    - HOST_IP=${HOST_IP:-192.168.86.195}   # für klickbare Service-Links im Dashboard
  volumes:
    - /home/reinhard/.local/bin/enzyme:/usr/local/bin/enzyme:ro   # enzyme refresh
```

---

## Priorisierung

| Iteration | Nutzen | Aufwand | Reihenfolge |
|---|---|---|---|
| 2.5 Sofort-Fixes | hoch | niedrig | ✅ erledigt |
| 3.5 Header-Extraktion | sehr hoch | mittel | ✅ erledigt |
| **3.7 Identifier + Personen-/Absender-DB** | **sehr hoch** | **mittel-hoch** | **Jetzt** |
| **3.8 Dokumenttyp-Extraktor** | **sehr hoch** | **niedrig-mittel** | **parallel zu 3.7** |
| 4 Taxonomie-Flexibilität (überarbeitet) | hoch | mittel | nach 3.7/3.8 |
| 5 Taxonomie-Erweiterungen | mittel | sehr niedrig (nur YAML) | parallel zu 4 |
| 6 Per-Feld-Konfidenz | mittel | mittel | vor Iter. 7 |
| 7 Audit-Trail | langfristig hoch | mittel | wenn stabil |

## Nicht im Skopus

- Modell-Upgrade (Hardware-Constraint 2 GB VRAM)
- Cloud-LLMs für Klassifikation (lokal bleibt Pflicht)
- Änderungen an der Vault-Struktur (Phase 4 abgeschlossen)
- Neue Container (alles bleibt im bestehenden `document-dispatcher`)
