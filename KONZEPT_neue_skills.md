# Architekturkonzept: Skills `kfz`, `altersvorsorge`, `sachversicherungen`

**Stand:** 2026-05-17  
**Autor:** Systemarchitektur-Session  
**Basis:** Bewährtes Muster aus `leistungsabrechnung` und `immobilien`

---

## 0. Übergeordnete Prinzipien (gelten für alle drei Skills)

Diese Prinzipien sind in den bestehenden Skills bereits etabliert und werden hier konsequent fortgeführt:

1. **Eine DB pro Domäne** — `kfz.db`, `altersvorsorge.db`, `sachversicherungen.db`. Nie gemeinsame `haushalt.db`. Jede DB ist für sich exportierbar (z.B. an Steuerberater, Werkstatt).
2. **dispatcher.db bleibt reine Workflow-DB** — Klassifikation, Duplikat-Check, Routing-Historie. Domänen-Daten gehören in die jeweilige Skill-DB.
3. **Dispatcher-Integration via analyze.py** — Der Dispatcher ruft `analyze.py text "..." --quelle "datei.pdf"` auf. Keine gemeinsamen Tabellen.
4. **Wilson-Bypass-Pfad** — `.meta.json` → Beschreibungstext aus Sidecar → `analyze.py text`. Identisch zu KV-Skill.
5. **Dedup via UNIQUE-Constraints** — kein doppelter Import bei Neulauf, kein `--force` im Standardbetrieb.
6. **Keyword-Matching vor LLM** — Deterministische Entitäts-Erkennung (Kennzeichen, Vertragsnummer) hat immer Vorrang.
7. **qwen3:4b-instruct als Standard-Modell** — schnell (~15s/PDF), `--model gemma4:26b` für schwierige Dokumente.
8. **Dashboard optional, aber geplant** — FastAPI auf dediziertem Port, HTML inline (keine Jinja2-Abhängigkeit), systemd user service.
9. **Zweisprachigkeit DE/IT** — Dokumente kommen auf Deutsch und Italienisch. Prompts müssen beide Sprachen explizit adressieren.

---

## 1. Skill `kfz` — Fahrzeuge & KFZ-Versicherungen

### 1.1 Scope & Motivation

**Vault-Ordner:** `60 Fahrzeuge/` (286 Dokumente)

Der KFZ-Skill ist die komplexeste der drei Domänen, weil:
- Mehrere Fahrzeuge mit unterschiedlichen Kennzeichen/Typen (DE + IT)
- Mehrsprachige Dokumente: Deutsch (Nürnberger, Zurich) und Italienisch (Allianz, Polizza, Carta di Circolazione)
- Drei Dokumentklassen mit sehr unterschiedlicher Struktur: Versicherungsverträge, Schäden, Werkstatt
- Aktiver Bestand: Neue Zurich-Verträge Mai 2026

**Aktive Fahrzeuge:**

| ID      | Kennzeichen | Typ           | Land | Anmerkung |
|---------|------------|---------------|------|-----------|
| `kfz_1` | GY243ZF    | PKW (Tesla MY)| IT   | Ehemals TS-MY8888 (DE), umgemeldet IT Oktober 2025 |
| `kfz_2` | GY964ZF    | Ape (Dreirad) | IT   | Piaggio Ape, Zurich-Versicherung (MB930145), Steuer |
| `kfz_3` | FR-Y1544   | Anhänger      | DE   | Fahrzeugschein 2025-01-11, WGV-Versicherung |
| `kfz_4` | TS-QZ566   | Anhänger      | DE   | Kauf Mai 2025 (Markus Hutterer) |


**Altfahrzeuge (Bulk-Eintrag `kfz_alt`):**

| ID       | Kennzeichen | Typ     | Land | Anmerkung |
|----------|------------|---------|------|-----------|
| `kfz_alt`| TS-RJ801   | PKW     | DE   | Nürnberger GARANTA 2022, nicht mehr im Bestand |
| `kfz_alt`| TS-MY8888  | PKW     | DE   | Altkennzeichen des Tesla (jetzt GY243ZF IT) |
| `kfz_alt`| (Audi Q5)  | PKW     | DE   | Docs bis 2021, vermutlich verkauft |
| `kfz_alt`| (Mitsubishi)| PKW    | DE   | TÜV-Dok 2024, Status unklar |
| `kfz_alt`| (Mini)     | PKW     | DE   | Kaufvertrag 2014 |

Alle Altfahrzeuge bekommen `aktiv = 0` und werden der Einheit `kfz_alt` ohne vollständigen Seed zugeordnet. Beim Batch-Import landen Dokumente ohne aktives Kennzeichen-Match automatisch in dieser Gruppe.

~~kfz_4~~ **ENTFERNT:** DTD153 = Makita Akku-Schlagschrauber, Fehlklassifikation → umgezogen nach `95 Bedienungsanleitungen/`

### 1.2 Datenbankschema `kfz.db`

```sql
-- Stammdaten der Fahrzeuge (Seed-Tabelle, manuell gepflegt)
CREATE TABLE IF NOT EXISTS fahrzeuge (
    id           TEXT PRIMARY KEY,         -- kfz_1, kfz_2, ...
    kennzeichen  TEXT NOT NULL UNIQUE,     -- TS-RJ801, GY964ZF, ...
    typ          TEXT,                     -- PKW, Ape, Motorrad, ...
    marke        TEXT,
    modell       TEXT,
    baujahr      INTEGER,
    land         TEXT DEFAULT 'IT',        -- DE / IT
    aktiv        INTEGER DEFAULT 1,        -- 1=im Bestand, 0=verkauft/abgemeldet
    abgemeldet   TEXT,                     -- YYYY-MM-DD, NULL wenn aktiv
    bemerkung    TEXT
);

INSERT OR IGNORE INTO fahrzeuge VALUES
-- Aktive Fahrzeuge
('kfz_1','GY243ZF',  'PKW', 'Tesla',  'Model Y', NULL,'IT',1,NULL,'Ehemals TS-MY8888 (DE), umgemeldet IT Okt 2025'),
('kfz_2','GY964ZF',  'Ape', 'Piaggio','Ape',     NULL,'IT',1,NULL,'Zurich MB930145, Steuer'),
('kfz_3','FR-Y1544', 'Anhänger',NULL, NULL,      NULL,'DE',1,NULL,'WGV-Versicherung, Fahrzeugschein 2025-01-11'),
('kfz_4','TS-QZ566', 'Anhänger',NULL, NULL,      NULL,'DE',1,NULL,'Kauf Mai 2025, Markus Hutterer'),
-- Altfahrzeuge (bulk, aktiv=0)
('kfz_alt_1','TS-RJ801', 'PKW',NULL,NULL,NULL,'DE',0,NULL,'Nürnberger GARANTA, nicht mehr im Bestand'),
('kfz_alt_2','TS-MY8888','PKW','Tesla','Model Y',NULL,'DE',0,NULL,'Altkennzeichen, jetzt GY243ZF IT');
-- DTD153: kein Fahrzeug — Makita Akku-Schlagschrauber, umgezogen nach 95 Bedienungsanleitungen/

-- KFZ-Versicherungsverträge
CREATE TABLE IF NOT EXISTS versicherungen (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fahrzeug_id      TEXT NOT NULL REFERENCES fahrzeuge(id),
    versicherer      TEXT NOT NULL,        -- Zurich / Allianz / Nürnberger / ...
    vertragsnummer   TEXT,
    deckungsart      TEXT,                 -- HP / TK / VK / HP+TK / HP+VK
    praemie_eur      REAL,                 -- Jahresprämie
    praemie_periode  TEXT,                 -- jaehrlich / halbjaehrlich / monatlich
    gueltig_von      TEXT,                 -- YYYY-MM-DD
    gueltig_bis      TEXT,                 -- YYYY-MM-DD
    aktiv            INTEGER DEFAULT 1,
    quelle_pdf       TEXT NOT NULL UNIQUE,
    erstellt_am      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kfz_vers_fzg   ON versicherungen(fahrzeug_id);
CREATE INDEX IF NOT EXISTS idx_kfz_vers_bis   ON versicherungen(gueltig_bis);

-- Schadensmeldungen und -regulierungen
CREATE TABLE IF NOT EXISTS schaeden (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fahrzeug_id      TEXT NOT NULL REFERENCES fahrzeuge(id),
    datum_schaden    TEXT,                 -- YYYY-MM-DD
    datum_meldung    TEXT,                 -- YYYY-MM-DD
    versicherer      TEXT,
    schadennummer    TEXT,
    hergang          TEXT,                 -- Kurztext
    schaden_eur      REAL,                 -- gemeldeter Schaden
    regulierung_eur  REAL,                 -- tatsächlich reguliert
    status           TEXT,                 -- gemeldet / in_bearbeitung / reguliert / abgelehnt
    quelle_pdf       TEXT NOT NULL UNIQUE,
    rohtext_md5      TEXT,
    erstellt_am      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kfz_schaden_fzg ON schaeden(fahrzeug_id);

-- Werkstatt, Wartung, TÜV, Inspektion
CREATE TABLE IF NOT EXISTS reparaturen (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fahrzeug_id      TEXT REFERENCES fahrzeuge(id),
    datum            TEXT,                 -- YYYY-MM-DD
    werkstatt        TEXT,
    art              TEXT,                 -- wartung / reparatur / tuev / inspektion / sonstiges
    betrag_eur       REAL,
    beschreibung     TEXT,
    quelle_pdf       TEXT NOT NULL UNIQUE,
    rohtext_md5      TEXT,
    erstellt_am      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kfz_rep_fzg  ON reparaturen(fahrzeug_id);
CREATE INDEX IF NOT EXISTS idx_kfz_rep_dat  ON reparaturen(datum);

-- KFZ-Steuer (vor allem IT: Ape-Steuer, Bollo)
CREATE TABLE IF NOT EXISTS steuern (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fahrzeug_id      TEXT REFERENCES fahrzeuge(id),
    jahr             INTEGER,
    betrag_eur       REAL,
    faellig          TEXT,                 -- YYYY-MM-DD
    bezahlt          TEXT,                 -- YYYY-MM-DD oder NULL
    quelle_pdf       TEXT NOT NULL UNIQUE,
    erstellt_am      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Zulassungsdokumente (Fahrzeugschein, Carta di Circolazione)
CREATE TABLE IF NOT EXISTS zulassungen (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fahrzeug_id      TEXT REFERENCES fahrzeuge(id),
    doktyp           TEXT,                 -- fahrzeugschein / carta_circolazione / zulassung
    datum_ausstellung TEXT,
    behoerde         TEXT,
    quelle_pdf       TEXT NOT NULL UNIQUE,
    erstellt_am      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 1.3 Keyword-Matching (deterministisch, vor LLM)

```python
KFZ_KENNZEICHEN = [
    # Aktive Fahrzeuge
    ("kfz_1", [r"gy\s*243\s*zf"]),                     # Tesla Model Y IT
    ("kfz_2", [r"gy\s*9[46]4\s*zf", r"mb930145"]),     # Ape (GY964ZF; Regex fängt auch GY946ZF)
    ("kfz_3", [r"fr[-\s]?y\s*1544"]),                  # Anhänger FR-Y1544
    ("kfz_4", [r"ts[-\s]?qz\s*566"]),                  # Anhänger TS-QZ566
    # Altkennzeichen → werden zu aktiv=0 Einträgen
    ("kfz_alt_1", [r"ts[-\s]?rj\s*801"]),
    ("kfz_alt_2", [r"ts[-\s]?my\s*8888"]),             # Altkennzeichen Tesla
]

KFZ_DOKTYP = [
    ("versicherung",  [r"polizza", r"versicherungsschein", r"versicherungsvertrag",
                       r"carta verde", r"green card", r"deckungskarte"]),
    ("schaden",       [r"schadenmeldung", r"schadensmeldung", r"sinistro",
                       r"schadenanzeige", r"denuncia"]),
    ("reparatur",     [r"werkstatt", r"officina", r"reparatur", r"wartung",
                       r"inspezione", r"manutenzione", r"kollaudo"]),
    ("steuer",        [r"kraftfahrzeugsteuer", r"bollo auto", r"tassa automobilistica",
                       r"superbollo"]),
    ("zulassung",     [r"fahrzeugschein", r"carta di circolazione",
                       r"zulassungsbescheinigung"]),
]
```

### 1.4 Ollama-Extraktionsprompt

Wichtig: Der Prompt muss explizit auf DE/IT Zweisprachigkeit eingehen und die bekannten Fahrzeuge nennen.

```
Du bist Experte für KFZ-Dokumente (Deutsch und Italienisch).
Extrahiere alle relevanten Informationen und gib sie als JSON zurück.

Aktive Fahrzeuge:
- kfz_1: GY243ZF (Tesla Model Y, IT — ehemals TS-MY8888 DE)
- kfz_2: GY964ZF / Polizza MB930145 (Piaggio Ape, IT)
- kfz_3: FR-Y1544 (Anhänger, DE)
- kfz_4: TS-QZ566 (Anhänger, DE)

Altkennzeichen (aktiv=0, nur für historische Zuordnung):
- kfz_alt_1: TS-RJ801 (PKW DE, nicht mehr im Bestand)
- kfz_alt_2: TS-MY8888 (Altkennzeichen Tesla, jetzt GY243ZF)
- kfz_4: Kennzeichen DTD153 (Italien)

Dokumenttypen:
- versicherung: Polizza/Versicherungsschein/Deckungskarte/Carta Verde
- schaden: Schadensmeldung/Sinistro/Denuncia
- reparatur: Werkstattrechnung/Officina/Wartung/TÜV/Collaudo
- steuer: KFZ-Steuer/Bollo Auto
- zulassung: Fahrzeugschein/Carta di Circolazione

Gib ausschließlich dieses JSON zurück (kein Markdown):
{
  "fahrzeug_id": "<kfz_1|kfz_2|kfz_3|null>",
  "kennzeichen": "<Kennzeichen aus Dokument>",
  "doktyp": "<versicherung|schaden|reparatur|steuer|zulassung|sonstiges>",
  "datum_dokument": "<YYYY-MM-DD oder null>",
  "versicherer": "<Name der Versicherung oder null>",
  "vertragsnummer": "<Vertragsnummer oder null>",
  "deckungsart": "<HP|TK|VK|HP+TK|HP+VK|null>",
  "praemie_eur": <Jahresprämie als Zahl oder null>,
  "gueltig_von": "<YYYY-MM-DD oder null>",
  "gueltig_bis": "<YYYY-MM-DD oder null>",
  "schaden_eur": <Schadenhöhe oder null>,
  "status_schaden": "<gemeldet|in_bearbeitung|reguliert|abgelehnt|null>",
  "betrag_eur": <Rechnungsbetrag Werkstatt/Steuer oder null>,
  "werkstatt": "<Name oder null>",
  "art_reparatur": "<wartung|reparatur|tuev|inspektion|sonstiges|null>",
  "beschreibung": "<kurze Zusammenfassung>"
}

Dokumenttext:
```

### 1.5 CLI-Interface

```bash
analyze.py init                         # DB anlegen
analyze.py pdf <pfad.pdf>               # PDF → DB
analyze.py text "<text>" --quelle <pdf> # Wilson-Bypass
analyze.py list [--kfz kfz_3] [--typ versicherung] [--jahr 2026]
analyze.py aktiv                        # Nur aktive Versicherungen (gueltig_bis >= heute)
analyze.py kosten [--kfz kfz_3]        # Kostenübersicht pro Fahrzeug
```

### 1.6 Dispatcher-Kategorien

```
dispatcher-Kategorie "fahrzeuge" → analyze.py text wird aufgerufen
```

Dispatcher-Erkennung via Keyword im Pfad: `60 Fahrzeuge/` oder `kategorie: fahrzeuge` im Frontmatter.

### 1.7 SKILL.md Trigger

```
Automatisch: Datei aus `60 Fahrzeuge/` oder Frontmatter `kategorie: fahrzeuge`
Manuell:     /kfz <pdf-pfad>
             /kfz list
             /kfz aktiv
             /kfz kosten
```

### 1.8 Dashboard (Port 8091)

- **Summary-Cards:** Fahrzeuge im Bestand, aktive Versicherungen, Ablauf in <30 Tagen (Rot-Warnung)
- **Fahrzeugübersicht:** Pro Fahrzeug: Kennzeichen, aktive Police, nächste Fälligkeit, YTD Kosten
- **Ablauf-Warnung:** Alle Versicherungen die in <60 Tagen ablaufen (Grundlage für Wechsel-Entscheidungen)
- **Kostentabelle:** Reparaturen/Werkstatt + Versicherung + Steuer pro Jahr und Fahrzeug
- **Schadenshistorie:** Chronologisch, mit Status

### 1.9 Bekannte Herausforderungen

- **Italienische Dokumente:** Zurich-Vertrag ist ein Scan in schlechter Qualität → Docling OCR notwendig (nicht pdftotext). Im Wilson-Bypass-Pfad kommt bereits der OCR-Text.
- **Deckungsart-Erkennung IT:** Italienisch: "RC Auto" = Haftpflicht, "Kasko" = Vollkasko, "Mini Kasko" = Teilkasko.
- **Prämie vs. Rate:** Unterscheide Jahresprämie von Ratenzahlung. Standardisierung auf Jahresbetrag.
- **Kennzeichen nicht immer lesbar:** Bei Scanned-Dokumenten kann das Kennzeichen fehlen. Dann Fahrzeug über Versicherernamen + Vertragsnummer matchen.

---

## 2. Skill `altersvorsorge` — Rentenverträge & Kapitalentwicklung

### 2.1 Scope & Motivation

**Vault-Ordner:** `40 Finanzen/Versicherungen/` (Altersvorsorge-Dokumente seit 1999)

Der Altersvorsorge-Skill ist primär ein **Zeitreihen-Tracker**: Standmitteilungen kommen 1x/Jahr und dokumentieren die Kapitalentwicklung. Über mehrere Verträge und Jahrzehnte entsteht ein vollständiges Bild des aufgebauten Altersvorsorge-Vermögens.

**Bekannte Verträge (aus Vault):**

| ID    | Versicherer        | Vertragsnr.              | Art                   | Person  |
|-------|--------------------|--------------------------|-----------------------|---------|
| `av_1`| AXA Lebensversicherung | 20412486-001/003    | kapitalbildend        | Reinhard |
| `av_2`| Nürnberger         | L 7087352 / L 708735x    | Direktversicherung    | Reinhard |
| `av_3`| Nürnberger Pensionskasse | L 592970x / L 5929705 | Pensionskasse    | Reinhard |
| `av_4`| Nürnberger U-Kasse | L 808735x                | Unterstützungskasse   | Reinhard |
| `av_5`| Nürnberger         | L 508735x / L 50873x     | Direktversicherung    | Marion  |
| `av_6`| Nürnberger U-Kasse | (separat)                | Unterstützungskasse   | Marion  |
| `av_7`| LV1871             | 73 088 025               | Basisrente (fondsgebunden) | Marion |
| `av_8`| HDI                | (aus 2019)               | fondsgebundene Rente  | unbekannt |
| `av_9`| Allvest            | (2022)                   | kapitalbildend        | unbekannt |

**Hinweis:** av_8 und av_9 müssen beim initialen Batch-Import aufgelöst werden. Vertragsnummern aus den PDFs nehmen.

### 2.2 Datenbankschema `altersvorsorge.db`

```sql
-- Vertragsstammdaten (primär manuell geseeded, ergänzt durch analyze.py)
CREATE TABLE IF NOT EXISTS vertraege (
    id              TEXT PRIMARY KEY,      -- av_1, av_2, ...
    versicherer     TEXT NOT NULL,
    vertragsnummer  TEXT,                  -- Hauptnummer (kann mehrere Versionen haben)
    art             TEXT NOT NULL,         -- direktversicherung / pensionskasse / ukasse /
                                           -- basisrente / kapitalbildend / fondsgebunden
    versicherungsnehmer TEXT,              -- Reinhard / Marion
    beguenstigter   TEXT,
    beitrag_monat_eur REAL,               -- aktueller Monatsbeitrag
    beginn          TEXT,                  -- YYYY-MM-DD
    ablauf          TEXT,                  -- YYYY-MM-DD (geplantes Vertragsende)
    aktiv           INTEGER DEFAULT 1,     -- 1=läuft, 0=beitragsfrei/abgelaufen
    beitragsfrei_ab TEXT,                  -- YYYY-MM-DD wenn beitragsfrei gestellt
    bemerkung       TEXT
);

INSERT OR IGNORE INTO vertraege VALUES
('av_1','AXA Lebensversicherung','20412486','kapitalbildend','Reinhard',NULL,NULL,'1999-04-01',NULL,1,NULL,'AXA Colonia, seit 1999'),
('av_2','Nürnberger','L7087352','direktversicherung','Reinhard',NULL,NULL,NULL,NULL,1,NULL,'Direktversicherung bAV'),
('av_3','Nürnberger Pensionskasse','L5929705','pensionskasse','Reinhard',NULL,NULL,NULL,NULL,1,NULL,''),
('av_4','Nürnberger U-Kasse','L8087353','ukasse','Reinhard',NULL,NULL,NULL,NULL,1,NULL,'Unterstützungskasse'),
('av_5','Nürnberger','L5087350','direktversicherung','Marion',NULL,NULL,NULL,NULL,1,NULL,'Marion bAV'),
('av_6','Nürnberger U-Kasse',NULL,'ukasse','Marion',NULL,NULL,NULL,NULL,1,NULL,'Marion U-Kasse'),
('av_7','LV1871','73088025','basisrente','Marion',NULL,NULL,NULL,NULL,1,'2023-05-08','Beitragsfrei gestellt 2023'),
('av_8','HDI-Gerling',NULL,'fondsgebunden','Reinhard',NULL,NULL,'2019-05-01',NULL,1,NULL,'fondsgebundene Rentenversicherung'),
('av_9','Allvest',NULL,'kapitalbildend',NULL,NULL,NULL,'2022-01-01',NULL,1,NULL,'Stand 2022');

-- Standmitteilungen (Herzstück: Zeitreihe des Kapitals)
CREATE TABLE IF NOT EXISTS standmitteilungen (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    vertrag_id            TEXT NOT NULL REFERENCES vertraege(id),
    datum_mitteilung      TEXT NOT NULL,   -- YYYY-MM-DD (Datum der Standmitteilung)
    stichtag              TEXT,            -- YYYY-MM-DD (Bewertungsstichtag, oft 30.11.)
    guthaben_eur          REAL,            -- aktuelles Guthaben / Rückkaufswert
    ablauf_garantie_eur   REAL,            -- garantierte Ablaufleistung
    ablauf_prognose_eur   REAL,            -- prognostizierte Ablaufleistung
    jahresrente_garantie  REAL,            -- garantierte Jahresrente ab Ablauf
    jahresrente_prognose  REAL,            -- prognostizierte Jahresrente
    beitraege_kumuliert_eur REAL,          -- Summe aller bisher eingezahlten Beiträge
    beitrag_aktuell_eur   REAL,            -- aktueller Monatsbeitrag laut Mitteilung
    ueberschuss_eur       REAL,            -- Überschussbeteiligung im Berichtsjahr
    quelle_pdf            TEXT NOT NULL,
    rohtext_md5           TEXT,
    UNIQUE(vertrag_id, datum_mitteilung, quelle_pdf)
);

CREATE INDEX IF NOT EXISTS idx_av_sm_vertrag ON standmitteilungen(vertrag_id);
CREATE INDEX IF NOT EXISTS idx_av_sm_datum   ON standmitteilungen(datum_mitteilung);

-- Vertragsänderungen (Beitragsfreistellung, Umbuchung, Änderung)
CREATE TABLE IF NOT EXISTS aenderungen (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vertrag_id       TEXT NOT NULL REFERENCES vertraege(id),
    datum            TEXT,
    art              TEXT,                 -- beitragsfreistellung / umbuchung /
                                           -- beitragsanpassung / kuendigung / auszahlung
    betrag_eur       REAL,
    beschreibung     TEXT,
    quelle_pdf       TEXT NOT NULL UNIQUE,
    erstellt_am      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 2.3 Keyword-Matching (deterministisch)

```python
AV_VERTRAEGE = [
    ("av_1", [r"20412486", r"axa colonia"]),
    ("av_2", [r"l\s*7087352", r"l\s*708735", r"nürnberger.*reinhard.*direkt",
              r"direktversicherung.*reinhard"]),
    ("av_3", [r"l\s*5929705", r"l\s*592970", r"pensionskasse.*reinhard"]),
    ("av_4", [r"l\s*8087353", r"l\s*808735", r"unterstützungskasse.*reinhard"]),
    ("av_5", [r"l\s*5087350", r"l\s*508735", r"nürnberger.*marion.*direkt"]),
    ("av_6", [r"unterstützungskasse.*marion", r"u-kasse.*marion"]),
    ("av_7", [r"73\s*088\s*025", r"lv\s*1871.*marion", r"basisrente.*marion"]),
    ("av_8", [r"hdi.*fondsgebunden", r"fonds.*rente.*hdi"]),
    ("av_9", [r"allvest"]),
]

AV_DOKTYP = [
    ("standmitteilung", [r"standmitteilung", r"stand der versicherung",
                          r"jahresinformation", r"wertmitteilung"]),
    ("versicherungsschein", [r"versicherungsschein", r"police"]),
    ("aenderung",       [r"beitragsfreistellung", r"beitragsanpassung",
                          r"vertragsänderung", r"umbuchung"]),
    ("auszahlung",      [r"auszahlung", r"leistungsfall", r"ablauf"]),
    ("nachhaltigkeit",  [r"nachhaltigkeitsthemen", r"offenlegungsverordnung",
                          r"eu.*2019/2088"]),   -- diese Dokumente überspringen!
]
```

**Wichtig:** Nachhaltigkeits-/ESG-Dokumente (Offenlegungsverordnung) haben keinen Datenwert für die DB und sollen geskippt werden (Rückgabe `{"skip": true}`).

### 2.4 Ollama-Extraktionsprompt

```
Du bist Experte für deutsche Lebens- und Rentenversicherungen.
Extrahiere alle finanziellen Kennzahlen aus dem Dokument.

Bekannte Verträge:
- av_1: AXA Lebensversicherung, VN 20412486, Reinhard, kapitalbildend (seit 1999)
- av_2: Nürnberger Direktversicherung, Reinhard (L 7087352)
- av_3: Nürnberger Pensionskasse, Reinhard (L 5929705)
- av_4: Nürnberger Unterstützungskasse, Reinhard (L 8087353)
- av_5: Nürnberger Direktversicherung, Marion (L 5087350)
- av_6: Nürnberger Unterstützungskasse, Marion
- av_7: LV1871 Basisrente, Marion (VN 73 088 025, beitragsfrei seit 2023)
- av_8: HDI fondsgebundene Rentenversicherung
- av_9: Allvest

Falls das Dokument nur über Nachhaltigkeitsthemen/Offenlegungsverordnung berichtet
und keine Kapitalwerte enthält, gib {"skip": true} zurück.

Gib ausschließlich dieses JSON zurück (kein Markdown):
{
  "skip": false,
  "vertrag_id": "<av_1..av_9 oder null wenn unbekannt>",
  "versicherer": "<Name>",
  "vertragsnummer": "<aus Dokument>",
  "versicherungsnehmer": "<Reinhard|Marion|null>",
  "doktyp": "<standmitteilung|versicherungsschein|aenderung|auszahlung|sonstiges>",
  "datum_mitteilung": "<YYYY-MM-DD>",
  "stichtag": "<YYYY-MM-DD oder null>",
  "guthaben_eur": <aktuelles Guthaben/Rückkaufswert als Zahl oder null>,
  "ablauf_garantie_eur": <garantierte Ablaufleistung oder null>,
  "ablauf_prognose_eur": <prognostizierte Ablaufleistung oder null>,
  "jahresrente_garantie": <garantierte Jahresrente oder null>,
  "jahresrente_prognose": <prognostizierte Jahresrente oder null>,
  "beitraege_kumuliert_eur": <Summe Einzahlungen oder null>,
  "beitrag_aktuell_eur": <aktueller Monatsbeitrag oder null>,
  "ueberschuss_eur": <Überschussbeteiligung oder null>,
  "art_aenderung": "<beitragsfreistellung|umbuchung|beitragsanpassung|null>"
}

Dokumenttext:
```

### 2.5 CLI-Interface

```bash
analyze.py init
analyze.py pdf <pfad.pdf>
analyze.py text "<text>" --quelle <pdf>
analyze.py list [--vertrag av_7] [--person Marion] [--typ standmitteilung]
analyze.py verlauf [--vertrag av_7]   # Zeitreihe Guthaben für einen Vertrag
analyze.py gesamt [--jahr 2025]       # Gesamtvermögen Altersvorsorge (Summe aller aktiven Verträge)
```

### 2.6 Dashboard (Port 8092)

- **Gesamtvermögen-Karte:** Summe aller aktuellen Guthaben (neueste Standmitteilung je Vertrag)
- **Pro Vertrag:** Sparkline der Kapitalentwicklung (Guthaben über die Jahre)
- **Tabelle:** Vertrag | Person | Versicherer | Art | letzter Stand | Guthaben | Prognose Ablauf
- **Beitragsrendite:** `(Guthaben - Beiträge_kumuliert) / Beiträge_kumuliert * 100`
- **Ablauftermine:** Übersicht wann welcher Vertrag ausläuft (Planungshilfe)

### 2.7 Besondere Herausforderungen

- **Standmitteilungs-Extraktion ist anspruchsvoll:** Nürnberger packt Garantierente, Prognoserente, Überschüsse in komplexe Tabellen. LLM muss explizit angewiesen werden, nur die finale "per Ablauf" Spalte zu nehmen.
- **Beitragsfrei-Status:** LV1871 (av_7) ist seit 2023-05-08 beitragsfrei — `beitrag_aktuell_eur = 0`, aber Guthaben wächst weiter. DB-Feld `beitragsfrei_ab` in `vertraege` Tabelle steuert das.
- **Vertragsnummern-Varianten:** Nürnberger schreibt dieselbe Vertragsnummer in verschiedenen Formaten (L7087352, L 708 735 2, 7087352). Keyword-Regex mit `\s*` überall.
- **Prognose vs. Garantie:** Immer beides extrahieren. Prognose kann deutlich höher sein — für Entscheidungen immer Garantiewert maßgeblich.

---

## 3. Skill `sachversicherungen` — Sach-, Haftpflicht- & Sonstige Versicherungen

### 3.1 Scope & Motivation

**Vault-Ordner:** `40 Finanzen/Versicherungen/` (Nicht-KV, nicht-KFZ, nicht-Altersvorsorge)  
**Zusatz:** `50 Immobilien eigen/` (Reale Mutua CASAMIA)

Dieser Skill ist primär ein **Coverage-Tracker**: Welche Risiken sind aktuell abgedeckt? Wo gibt es Lücken? Was wurde wann für wie viel bezahlt?

**Bekannte Verträge (aus Vault):**

| ID     | Art              | Versicherer            | Person  | Status        |
|--------|------------------|------------------------|---------|---------------|
| `sv_1` | Hausrat          | DOCURA                 | Reinhard| unklar (letztes Dok 2018) |
| `sv_2` | Wohngebäude      | Nürnberger PrivatSchutz| Reinhard| unklar (2013-2014) |
| `sv_3` | Privathaftpflicht| HDI                    | Reinhard| 2015 |
| `sv_4` | Privathaftpflicht| AXA                    | Reinhard| **gekündigt 2026-02-27** |
| `sv_5` | Unfallversicherung| VGH                   | (Familie)| unklar (2017) |
| `sv_6` | D&O-Versicherung | VOV                    | Reinhard| 2016 |
| `sv_7` | Schließfach      | Versicherungskammer Bayern | Reinhard | 2018 |
| `sv_8` | Tierversicherung | NV Versicherungen      | Hund    | 2017 |
| `sv_9` | Wohngebäude/Katastrophe | Reale Mutua CASAMIA | Reinhard | aktiv (2026) |
| `sv_10`| Rechtsschutz     | WGV                    | Reinhard| aktiv (2024 Versicherungsschein) |

**Beachte:** sv_9 liegt in `50 Immobilien eigen/` (Seggiano/Podere dei venti), gehört aber thematisch hierher.

### 3.2 Datenbankschema `sachversicherungen.db`

```sql
-- Vertragsstammdaten
CREATE TABLE IF NOT EXISTS vertraege (
    id              TEXT PRIMARY KEY,      -- sv_1, sv_2, ...
    art             TEXT NOT NULL,         -- hausrat / wohngebaeude / haftpflicht_privat /
                                           -- haftpflicht_do / unfall / tier / schliessfach /
                                           -- rechtsschutz / katastrophe /
                                           -- kombi_it (Reale Mutua: Wohngebäude+HP+Katastrophe)
                                           -- sonstiges
    versicherer     TEXT NOT NULL,
    vertragsnummer  TEXT,
    versicherungsnehmer TEXT,              -- Reinhard / Marion / Familie
    versichertes_objekt TEXT,             -- z.B. "Podere dei venti" oder "Lipowskystr."
    praemie_eur     REAL,                  -- zuletzt bekannte Jahresprämie
    gueltig_von     TEXT,
    gueltig_bis     TEXT,
    aktiv           INTEGER DEFAULT 1,     -- 1=aktiv, 0=gekündigt/abgelaufen
    gekuendigt_am   TEXT,                  -- YYYY-MM-DD falls gekündigt
    land            TEXT DEFAULT 'DE',     -- DE / IT
    bemerkung       TEXT
);

INSERT OR IGNORE INTO vertraege VALUES
('sv_1', 'hausrat',           'DOCURA',               NULL,  'Reinhard', NULL, NULL,       NULL,   NULL,   0, NULL, 'DE', 'ausgelaufen, kein Nachfolger bekannt'),
('sv_2', 'wohngebaeude',      'Nürnberger PrivatSchutz',NULL,'Reinhard', NULL, NULL,       NULL,   NULL,   0, NULL, 'DE', 'letztes Dok 2014, vmtl. abgelöst'),
('sv_3', 'haftpflicht_privat','HDI',                  NULL,  'Reinhard', NULL, NULL,       NULL,   NULL,   0, NULL, 'DE', 'letztes Dok 2015'),
('sv_4', 'haftpflicht_privat','AXA',                  NULL,  'Reinhard', NULL, NULL,       NULL,'2026-02-27',0,'2026-02-27','DE','Kündigung 2026-02-27 — Nachfolge: Haftpflicht via Reale Mutua CASAMIA (sv_9)'),
('sv_5', 'unfall',            'VGH',                  NULL,  'Familie',  NULL, NULL,       NULL,   NULL,   1, NULL, 'DE', 'letztes Dok 2017'),
('sv_6', 'haftpflicht_do',    'VOV',                  NULL,  'Reinhard', NULL, NULL,       '2016', NULL,   0, NULL, 'DE', 'D&O, letztes Dok 2016'),
('sv_7', 'schliessfach',      'Versicherungskammer Bayern',NULL,'Reinhard',NULL,NULL,      NULL,   NULL,   1, NULL, 'DE', '2018'),
('sv_8', 'tier',              'NV Versicherungen',    NULL,  'Reinhard', NULL, NULL,       NULL,   NULL,   1, NULL, 'DE', 'Hund'),
('sv_9', 'kombi_it',           'Reale Mutua',          NULL,  'Reinhard', 'Podere dei venti + Appartamento Via dell''ospedale', NULL, NULL, NULL, 1, NULL, 'IT', 'CASAMIA: Wohngebäude + Haftpflicht + Katastrophe, beide Seggiano-Objekte'),
('sv_10','rechtsschutz',      'WGV',                  NULL,  'Reinhard', NULL, NULL,       '2024', NULL,   1, NULL, 'DE', 'Versicherungsschein 2024');

-- Prämien/Beitragsrechnungen (Zeitreihe der Zahlungen)
CREATE TABLE IF NOT EXISTS praemien (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vertrag_id      TEXT NOT NULL REFERENCES vertraege(id),
    datum           TEXT NOT NULL,         -- YYYY-MM-DD der Rechnung/Beitragsanforderung
    betrag_eur      REAL NOT NULL,
    periode_von     TEXT,                  -- YYYY-MM-DD
    periode_bis     TEXT,                  -- YYYY-MM-DD
    quelle_pdf      TEXT NOT NULL UNIQUE,
    rohtext_md5     TEXT,
    erstellt_am     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sv_praemie_vertrag ON praemien(vertrag_id);
CREATE INDEX IF NOT EXISTS idx_sv_praemie_datum   ON praemien(datum);

-- Schadensregulierungen
CREATE TABLE IF NOT EXISTS schaeden (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vertrag_id      TEXT REFERENCES vertraege(id),
    datum_schaden   TEXT,
    datum_meldung   TEXT,
    beschreibung    TEXT,
    schaden_eur     REAL,
    regulierung_eur REAL,
    status          TEXT,                  -- gemeldet / reguliert / abgelehnt
    quelle_pdf      TEXT NOT NULL UNIQUE,
    erstellt_am     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Vertragsänderungen (Kündigung, Anpassung, Neuabschluss)
CREATE TABLE IF NOT EXISTS aenderungen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vertrag_id      TEXT NOT NULL REFERENCES vertraege(id),
    datum           TEXT,
    art             TEXT,                  -- kuendigung / anpassung / neuabschluss / sonstiges
    beschreibung    TEXT,
    quelle_pdf      TEXT NOT NULL UNIQUE,
    erstellt_am     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3.3 Keyword-Matching

```python
SV_VERTRAEGE = [
    ("sv_1",  [r"docura.*hausrat", r"hausrat.*docura"]),
    ("sv_2",  [r"nürnberger.*privatschutz", r"privatschutz.*wohngebäude"]),
    ("sv_3",  [r"hdi.*haftpflicht", r"haftpflicht.*hdi"]),
    ("sv_4",  [r"axa.*haftpflicht", r"haftpflicht.*axa"]),
    ("sv_5",  [r"vgh.*unfall", r"unfallversicherung.*vgh"]),
    ("sv_6",  [r"vov.*d.?o", r"d.?o.*versicherung.*vov"]),
    ("sv_7",  [r"versicherungskammer.*schließfach", r"schließfach.*versicherung"]),
    ("sv_8",  [r"nv versicherungen.*tier", r"tierversicherung.*nv"]),
    ("sv_9",  [r"reale mutua", r"casamia", r"katastrophenversicherung.*seggiano"]),
    ("sv_10", [r"wgv.*rechtsschutz", r"rechtsschutz.*wgv"]),
]

SV_DOKTYP = [
    ("beitragsrechnung", [r"beitragsrechnung", r"prämienrechnung", r"beitragsanforderung",
                           r"versicherungsbeitrag"]),
    ("versicherungsschein", [r"versicherungsschein", r"police", r"polizza"]),
    ("kuendigung",      [r"kündigung", r"vertragskündigung", r"kündigungsbestätigung"]),
    ("schaden",         [r"schadenmeldung", r"schadensmeldung", r"schadenregulierung"]),
    ("angebot",         [r"angebot", r"offerte", r"angebotsnummer"]),
]
```

### 3.4 Ollama-Extraktionsprompt

```
Du bist Experte für deutsche und italienische Sachversicherungen.
Extrahiere Vertragsdetails und Prämienangaben.

Bekannte Verträge:
- sv_1: DOCURA Hausratversicherung (Reinhard)
- sv_2: Nürnberger PrivatSchutz Wohngebäude (Reinhard)
- sv_3: HDI Privathaftpflicht (Reinhard, 2015)
- sv_4: AXA Privathaftpflicht (Reinhard, GEKÜNDIGT 2026-02-27)
- sv_5: VGH Unfallversicherung (Familie)
- sv_6: VOV D&O-Versicherung (Reinhard, 2016)
- sv_7: Versicherungskammer Bayern Schließfach (Reinhard)
- sv_8: NV Versicherungen Tierversicherung (Hund)
- sv_9: Reale Mutua CASAMIA Katastrophenversicherung (Reinhard, Italien, beide Seggiano-Objekte)
- sv_10: WGV Rechtsschutzversicherung (Reinhard)

Gib ausschließlich dieses JSON zurück (kein Markdown):
{
  "vertrag_id": "<sv_1..sv_10 oder null wenn neuer unbekannter Vertrag>",
  "versicherer": "<Name>",
  "art": "<hausrat|wohngebaeude|haftpflicht_privat|haftpflicht_do|unfall|tier|schliessfach|rechtsschutz|katastrophe|sonstiges>",
  "vertragsnummer": "<aus Dokument oder null>",
  "doktyp": "<beitragsrechnung|versicherungsschein|kuendigung|schaden|angebot|sonstiges>",
  "datum_dokument": "<YYYY-MM-DD>",
  "praemie_eur": <Jahresprämie als Zahl oder null>,
  "periode_von": "<YYYY-MM-DD oder null>",
  "periode_bis": "<YYYY-MM-DD oder null>",
  "aktiv": <true/false — false wenn Kündigung>,
  "gekuendigt_am": "<YYYY-MM-DD oder null>",
  "land": "<DE|IT>",
  "beschreibung": "<kurze Zusammenfassung>"
}

Dokumenttext:
```

### 3.5 CLI-Interface

```bash
analyze.py init
analyze.py pdf <pfad.pdf>
analyze.py text "<text>" --quelle <pdf>
analyze.py list [--art hausrat] [--aktiv] [--land IT]
analyze.py coverage                    # Übersicht aktive Deckungen + Lückenanalyse
analyze.py praemien [--jahr 2025]      # Jahreskosten Sachversicherungen gesamt
```

### 3.6 Dashboard (Port 8093)

- **Coverage-Karte:** Welche Risikotypen sind abgedeckt (grün), welche fehlen (gelb/rot)?
  - Pflichtcheck: Haftpflicht vorhanden? Hausrat für aktuelle Wohnung (Seggiano)? 
- **Aktive Verträge:** Tabelle mit Art, Versicherer, letzte Prämie, nächste Fälligkeit
- **Gekündigte Verträge:** Grau, mit Kündigungsdatum
- **Jahreskosten-Balken:** Sachversicherungskosten pro Jahr (aus Prämientabelle)
- **Lückenhinweis:** Feste Regeln:
  - Haftpflicht: aktiver `haftpflicht_privat` ODER aktiver `kombi_it` → gedeckt. Sonst: Warnung.
  - Hausrat DE: kein aktiver `hausrat` vorhanden (sv_1 ausgelaufen) → Hinweis (aktueller Wohnsitz IT, daher kein Handlungsbedarf, aber dokumentieren)
  - Wohngebäude IT: `kombi_it` (sv_9) aktiv → gedeckt für beide Seggiano-Objekte

### 3.7 Besondere Herausforderungen

- **Status-Unsicherheit:** Mehrere Verträge (sv_1 bis sv_8) haben seit 2014-2018 keine neueren Dokumente. Beim initialen Batch-Import wird sich zeigen, ob es Folgedokumente gibt oder ob die Verträge faktisch abgelaufen sind. `aktiv`-Flag nach dem Batch manuell reviewen.
- **Reale Mutua (sv_9) auf Italienisch:** CASAMIA Hausversicherung deckt zwei Objekte (Podere dei venti + Appartamento Via dell'ospedale). Polizza auf Italienisch, Beträge in EUR. Jährliche Prämienrechnung zu erwarten.
- **Neue Verträge:** AXA Haftpflicht wurde 2026 gekündigt — wahrscheinlich wurde ein Nachfolgevertrag (z.B. HUK, ERGO, Gothaer) abgeschlossen. Falls kein Dokument dazu im Vault liegt → Dashboard-Warnung.
- **Dispatcher-Abgrenzung zu KFZ:** KFZ-Dokumente können auch unter `40 Finanzen/Versicherungen/` liegen (historisch). Keyword-Filter auf bekannte KFZ-Kennzeichen zuerst prüfen, dann sv-Routing.

---

## 4. Gemeinsame Infrastruktur

### 4.1 Verzeichnisstruktur

```
~/.claude/skills/
  kfz/
    SKILL.md
    analyze.py
    schema.sql
    dashboard.py           # optional Phase 2
    kfz.db
    PROTOKOLL.md
  altersvorsorge/
    SKILL.md
    analyze.py
    schema.sql
    dashboard.py           # optional Phase 2
    altersvorsorge.db
    PROTOKOLL.md
  sachversicherungen/
    SKILL.md
    analyze.py
    schema.sql
    dashboard.py           # optional Phase 2
    sachversicherungen.db
    PROTOKOLL.md
```

### 4.2 Dispatcher-Integration (gemeinsames Muster)

In `dispatcher/dispatcher.py` (für jeden neuen Skill ein Block):

```python
# Routing-Entscheidung (nach Klassifikation)
if result["kategorie"] == "fahrzeuge":
    _call_skill_analyze("kfz", text, pdf_path)
elif result["kategorie"] in ("finanzen_versicherung_altersvorsorge",):
    _call_skill_analyze("altersvorsorge", text, pdf_path)
elif result["kategorie"] in ("finanzen_versicherung_sach",):
    _call_skill_analyze("sachversicherungen", text, pdf_path)

def _call_skill_analyze(skill_name: str, text: str, pdf_path: str):
    """Ruft analyze.py text im Hintergrund auf (non-blocking)."""
    script = Path.home() / ".claude/skills" / skill_name / "analyze.py"
    subprocess.Popen([
        sys.executable, str(script),
        "text", text[:12000],
        "--quelle", pdf_path,
    ])
```

Für den Wilson-Bypass-Pfad (`.meta.json`) läuft das identisch — der Beschreibungstext aus dem Sidecar wird als `text` übergeben.

### 4.3 Dispatcher-Kategorien (neue Werte)

Die Dispatcher-Klassifikation muss drei neue Kategorien erkennen. Erweiterung des Kategorie-Prompts:

```
fahrzeuge               → KFZ-Versicherung, Werkstatt, Fahrzeugschein, Schäden
finanzen_versicherung_altersvorsorge → Lebensversicherung, Rentenversicherung,
                          Direktversicherung, Pensionskasse, Standmitteilung
finanzen_versicherung_sach → Hausrat, Wohngebäude, Haftpflicht, Unfall, D&O,
                          Rechtsschutz, Sachversicherung allgemein
```

### 4.4 Dashboard-Port-Belegung

| Port | Skill |
|------|-------|
| 8090 | kv-dashboard (leistungsabrechnung) — bereits aktiv |
| 8091 | kfz-dashboard |
| 8092 | altersvorsorge-dashboard |
| 8093 | sachversicherungen-dashboard |

### 4.5 systemd User Services

Pro Dashboard ein Service (Muster wie `kv-dashboard.service`):

```ini
[Unit]
Description=KFZ Dashboard
After=network.target

[Service]
WorkingDirectory=%h/.claude/skills/kfz
ExecStart=/usr/bin/python3 dashboard.py
Restart=on-failure

[Install]
WantedBy=default.target
```

---

## 5. Implementierungs-Reihenfolge & Phasen

### Phase 1: Datenbank & Extraktion (kein Dashboard, kein Dispatcher)

Für jeden Skill zuerst den Kern bauen und mit Batch-Import befüllen:

1. `schema.sql` anlegen + `analyze.py init`
2. `analyze.py pdf` + `analyze.py text` implementieren
3. Extraktionsprompt tunen (5–10 Testdokumente)
4. Batch-Import aller relevanten PDFs aus dem Vault
5. Datenqualität reviewen, offensichtliche Fehler korrigieren
6. `analyze.py list` / `analyze.py kosten` etc. implementieren
7. PROTOKOLL.md anlegen

**Batch-Befehl (Muster KV-Skill):**
```bash
find "/path/to/vault/60 Fahrzeuge" -name "*.md" \
  -exec grep -l "original:" {} \; | while read md; do
    pdf=$(grep "^original:" "$md" | head -1 | sed 's/original: //')
    python3 ~/.claude/skills/kfz/analyze.py pdf "Anlagen/$pdf"
done
```

### Phase 2: Dispatcher-Integration

Nach stabilem Phase-1-Stand:

1. Dispatcher-Kategorien um neue Werte erweitern
2. `_call_skill_analyze()` Funktion implementieren
3. Volume-Mount in `docker-compose.yml` (analog KV: Verzeichnis statt Einzeldatei für WAL-Kompatibilität)
4. 5–10 Dokumente manuell durch Dispatcher laufen lassen, DB-Einträge prüfen

### Phase 3: Dashboard

Nach stabilem Phase-2-Stand:

1. `dashboard.py` implementieren (FastAPI, Port lt. Tabelle oben)
2. systemd Service anlegen
3. SKILL.md um Dashboard-URL ergänzen

### Empfohlene Reihenfolge der Skills

```
1. kfz            — aktiv, viele Dokumente, konkreter Nutzwert sofort (Ablauf-Warnung)
2. altersvorsorge — Zeitreihen-Extraktion, LV1871-Standmitteilungen sind sauber
3. sachversicherungen — Coverage-Check, viele historische Altdokumente
```

---

## 6. Offene Fragen — alle geklärt (2026-05-17)

1. ✅ **kfz_4 (DTD153):** **Kein Fahrzeug.** Bedienungsanleitung Makita Akku-Schlagschrauber (Modell DTD153). Fehlklassifikation im Vault → Dokument gehört nach `95 Bedienungsanleitungen`. Seed-Eintrag kfz_4 entfernt. **Nebenaktion:** Vault-Dokument `20231213_DTD153.md` umkategorisieren.
2. ✅ **av_8 (HDI):** HDI-Gerling, fondsgebundene Rentenversicherung, Versicherungsnehmer Reinhard. av_9 (Allvest): Vertragsnummer beim Batch-Import aus PDF nehmen.
3. ✅ **sv_1 (DOCURA Hausrat):** Ausgelaufen. `aktiv = 0`.
4. ✅ **Haftpflicht-Nachfolger:** Haftpflicht ist als Bestandteil der Reale Mutua CASAMIA (sv_9) abgedeckt. Keine Coverage-Lücke. sv_9 bekommt `art = kombi_it`.
5. ✅ **Reale Mutua Objekte:** Police deckt beide Seggiano-Objekte (Podere dei venti + Appartamento Via dell'ospedale).
6. ✅ **Dispatcher:** Catch-All vorhanden. Neue Kategorien `fahrzeuge`, `finanzen_versicherung_altersvorsorge`, `finanzen_versicherung_sach` müssen in Phase 2 in den Dispatcher-Prompt eingetragen werden, damit das Routing deterministisch wird statt per Catch-All.

---

## Nebenaktion (aus Konzeptklärung)

**Vault-Fehlklassifikation korrigieren:**
```
20231213_DTD153.md in 60 Fahrzeuge/ → umziehen nach 95 Bedienungsanleitungen/
Frontmatter: kategorie: fahrzeuge → kategorie: bedienungsanleitungen
```

## Offene Aufgaben (zurückgestellt, nächste Session)

| # | Aufgabe | Status |
|---|---------|--------|
| 3 | KFZ: analyze.py/schema.sql/kfz.db vom anderen Team bereits geliefert (6 Fahrzeuge + 3 Versicherungen geseeded) | ✅ Basis vorhanden |
| 4 | KFZ: Dispatcher-Integration — kein KFZ-Block in dispatcher.py, kein Volume-Mount in docker-compose.yml | ⏳ offen |
| 5 | KFZ: Batch-Import — Vault `60 Fahrzeuge/` noch nicht importiert | ⏳ offen |
| 6 | KFZ: PROTOKOLL.md + dashboard.py fehlen | ⏳ offen |
| 7 | `altersvorsorge`-Skill — Konzept fertig, Implementierung ausstehend | ⏳ offen |
| 8 | `sachversicherungen`-Skill — Konzept fertig, Implementierung ausstehend | ⏳ offen |
| 9 | Git push zu origin — aktuell 6 Commits lokal ahead | ⏳ offen |

## Changelog

- **2026-05-17:** Initiales Konzept erstellt (alle drei Skills)
- **2026-05-17:** Alle 6 offenen Fragen geklärt — kfz_4 entfernt, av_8 präzisiert, sv_1 auf inaktiv, sv_9 auf `kombi_it`, Coverage-Logik angepasst, Dispatcher-Strategie bestätigt
- **2026-05-17:** Fahrzeugliste vollständig revidiert: Aktive Fahrzeuge = GY243ZF (Tesla MY IT), GY964ZF (Ape), FR-Y1544 (Anhänger), TS-QZ566 (Anhänger). Altfahrzeuge als Bulk. DTD153 aus Vault nach `95 Bedienungsanleitungen/` umgezogen (Fehlklassifikation bereinigt).
- **2026-05-17:** Offene Aufgaben 3–9 dokumentiert, zurückgestellt auf nächste Session.
