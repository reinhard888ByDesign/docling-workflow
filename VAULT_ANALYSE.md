# Vault-Analyse und Strukturvorschläge
Stand: 2026-05-07

## Kennzahlen

| Metrik | Wert |
|--------|------|
| Gesamte MDs | **4.483** |
| PDFs in Anlagen/ | 4.620 |
| Bilder in Anlagen/ | 2.185 |
| Vault-Größe gesamt | ~9,7 GB |
| MDs mit `kategorie:` | 2.045 (46 %) |
| MDs **ohne** `kategorie:` | **2.438 (54 %)** |
| Broken YAML (`tags: []`) | 188 |
| Generische Titel ("Unbenannte Notiz") | 32 |

---

## Ordner-Übersicht

| Ordner | MDs | Unterordner | Größe | Anmerkung |
|--------|-----|-------------|-------|-----------|
| **00 Inbox** | 600 | 21 Jahresordner | 69 MB | ⚠️ kein echter Inbox |
| 10 Persönlich | 267 | 23 | 49 MB | gemischt Jahr+Thema |
| 20 Familie | 191 | 16 | 3,3 MB | ok |
| 30 FengShui | 95 | 15 | 1,1 MB | ok |
| **40 Finanzen** | **822** | 36 | 46 MB | größte aktive Kategorie |
| **49 Krankenversicherung** | 200 | 24 | 2,0 MB | eigener Top-Level, redundant |
| 50 Immobilien eigen | 187 | 8 | 3,1 MB | ok |
| 51 Immobilien vermietet | 18 | 3 | 2,0 MB | ok |
| 55 Garten | 25 | 4 | 120 KB | sehr klein |
| 60 Fahrzeuge | 259 | 16 | 112 MB | 2016=58MB, 2025=31MB |
| 70 Italien | 241 | 7 | 9,4 MB | ok |
| 80 Business | 293 | 16 | 64 MB | aktiv (Sales Companion) |
| 82 Digitales | 15 | 1 | 296 KB | Systemdoku |
| 85 Wissen | 13 | 4 | 88 KB | sehr klein |
| 90 Reisen | 70 | 13 | 620 KB | ok |
| 95 Bedienungsanleitungen | 2 | 1 | 160 KB | sehr klein |
| **99 Archiv** | **1.164** | 22 | **187 MB** | größte Kategorie überhaupt |
| Anlagen/ | — | 1 | 6,7 GB | PDFs + Bilder |

---

## Problem 1: 00 Inbox ist kein Inbox — er ist ein Archiv-Dump

**Ist-Zustand:** 600 MDs, davon 478 in Jahresordnern (2003–2025) und 122 direkt im Root.  
Diese Dateien sind ENEX-Importe, die beim Evernote-Export in den "Inbox"-Notebook gelandet sind —
sie wurden nie kategorisiert.

**Erwarteter Inbox:** Neue Dokumente vom Dispatcher (heute: 50 MDs über alle Ordner verteilt) landen
korrekt in den Zielordnern. Der `00 Inbox`-Ordner sollte nur wirklich unbearbeitete Dokumente enthalten,
die noch klassifiziert werden müssen.

**Vorschlag A — Schrittweise Umsortierung:**
- Die 122 Loose-Files im Inbox-Root sind zu klassifizieren (Dispatcher-Rerun oder manuell)
- Die Jahresordner-Inhalte (2003–2019) → in `99 Archiv/` unter den jeweiligen Jahren zusammenführen
- Jahresordner 2020–2025 → nach Kategorien umsortieren (Finanzen, Persönlich, etc.)
- Ziel: `00 Inbox` nur noch für wirklich neue, unklassifizierte Docs

**Priorität:** ⚠️ Mittel — die Dokumente sind auffindbar, aber der Inbox-Name irreführt.

---

## Problem 2: 99 Archiv = 1.164 MDs — zu groß und zu undifferenziert

**Ist-Zustand:** 1.164 MDs, fast ausschließlich ENEX-Importe aus der ec4u-Berufszeit (2014–2022).
Schwerpunkte: 2019 (162), 2021 (141), 2018 (131). Plus `ec4u/`-Unterordner mit 38 MDs.

**Problem:** 26 % aller MDs sind in "Archiv", ohne thematische Differenzierung. Das macht
Suche schwierig und die Kategorie wertlos.

**Vorschlag B — Archiv-Segmentierung:**
Die Jahresordner im Archiv enthalten hauptsächlich:
- Business-Docs (ec4u, Kunden, Rechnungen) → bleiben in 80 Business oder 99 Archiv/Business
- Persönliche Docs → in die jeweilige Kategorie
- Finanzen-Docs → 40 Finanzen

Kurzfristig: `ec4u`-Unterordner → `99 Archiv/ec4u` ✅ (schon vorhanden)  
Langfristig: Archiv-Jahre scannen und per Dispatcher-Batch-Rerun kategorisieren.

**Priorität:** 🔵 Niedrig — "Archiv" als Catch-All ist legitim, aber 26 % ist zu viel.

---

## Problem 3: 49 Krankenversicherung — fehlplatzierter Top-Level-Ordner

**Ist-Zustand:** 200 MDs, eigene Nummer 49, mit sinnvollen thematischen Unterordnern
(Arztrechnung, Leistungsabrechnung, Beitragsinformation — je nach Person: Reinhard/Marion).

**Problem:** Krankenversicherung ist eine Unterkategorie von Finanzen/Versicherungen.
Der Ordner `40 Finanzen/Versicherungen/` hat bereits 70 MDs, aber keine KV-Docs.

**Vorschlag C — Konsolidierung:**
```
40 Finanzen/
  Versicherungen/
    Krankenversicherung/
      Arztrechnung Reinhard/
      Arztrechnung Marion/
      Leistungsabrechnung Reinhard/
      Leistungsabrechnung Marion/
      Beitragsinformation/
```
Vorteil: Alle Versicherungen unter einem Dach (70 + 200 = 270 MDs).  
Nachteil: Migration von 200 MDs + Dispatcher-Konfiguration anpassen.

**Priorität:** 🟡 Mittel-niedrig — strukturell korrekt, aber hoher Aufwand.

---

## Problem 4: Kategorie-Feld fehlt bei 54 % der Dokumente

**Ist-Zustand:** 2.438 MDs ohne `kategorie:` — das sind fast alle ENEX-Importe (`source: evernote`).

**Folge:** enzyme-Suche kann nicht nach Kategorie filtern. Dispatcher-Batch-Rerun würde helfen,
aber ist rechenintensiv (2.929 Dateien × LLM-Klassifikation).

**Vorschlag D — Ordnerbasierte Kategorie-Herleitung:**
Script, das `kategorie:` aus dem Ordnerpfad befüllt, wenn das Feld fehlt:
- `40 Finanzen/**` → `kategorie: finanzen`
- `49 Krankenversicherung/**` → `kategorie: krankenversicherung`
- `99 Archiv/**` → `kategorie: archiv`
- etc.

Das ist keine echte KI-Klassifikation, aber füllt das Feld konsistent.
Ca. 2.400 MDs würden eine Basis-Kategorie bekommen.

**Priorität:** 🟡 Mittel — schnell umsetzbar, großer Gewinn für Filterbarkeit.

---

## Problem 5: Kategorie-Normalisierung — 25 Varianten für 15 Kategorien

**Ist-Zustand:**
```
finanzen / Finanzen / "Finanzen" / archiv / Archiv 
fahrzeuge / "Fahrzeuge" / Fahrzeuge
persoenlich / "Persönliches" / Persönlich
immobilien_eigen / "Immobilien eigen" / Immobilien eigen
krankenversicherung / "Krankenversicherung" / Krankenversicherung
...
```

**Standardisierung (Vorschlag):**
| Canonical | Bisher auch | Ordner |
|-----------|-------------|--------|
| `finanzen` | Finanzen, "Finanzen" | 40 |
| `krankenversicherung` | "Krankenversicherung" | 49 |
| `persoenlich` | Persönlich, "Persönliches" | 10 |
| `familie` | Familie | 20 |
| `fengshui` | FengShui | 30 |
| `immobilien_eigen` | "Immobilien eigen", Immobilien eigen | 50 |
| `immobilien_vermietet` | Immobilien vermietet | 51 |
| `garten` | — | 55 |
| `fahrzeuge` | Fahrzeuge, "Fahrzeuge" | 60 |
| `italien` | — | 70 |
| `business` | Business | 80 |
| `digitales` | Digitales | 82 |
| `wissen` | — | 85 |
| `reisen` | — | 90 |
| `archiv` | Archiv | 99 |

**Umsetzung:** `sed -i` bulk-replace über alle betroffenen MDs.
**Priorität:** 🟢 Hoch — einfach, risikoarm, sofort umsetzbar.

---

## Problem 6: 188 Broken YAML — `tags: []` mit nachfolgenden Items

**Ist-Zustand:** 188 MDs haben im Frontmatter `tags: []\n\n  - Tag1\n  - Tag2`.
Das ist kein gültiges YAML — Obsidian und Plugins parsen es inkonsistent.

**Ursache:** Dispatcher-Bug vom 2026-05-01 (batch import), bereits bekannt.

**Fix:**
```python
# tags: []\n\n  - Foo\n  - Bar  →  tags:\n  - Foo\n  - Bar
```

**Priorität:** 🔴 Hoch — aktiver Bug, der Obsidian-Plugins (Dataview, enzyme) beeinträchtigt.

---

## Problem 7: Strukturelle Inkonsistenz — Jahresordner vs. Thema

**Ist-Zustand:** Verschiedene Ordner nutzen unterschiedliche Strategien:

| Ordner | Strategie |
|--------|-----------|
| 40 Finanzen | Gemischt: Jahresordner (1991–2025) + Thema (Versicherungen, Rechnungen) |
| 49 Krankenversicherung | Thema (Arztrechnung, Leistungsabrechnung) + Jahr |
| 60 Fahrzeuge | Nur Jahresordner |
| 80 Business | Gemischt: Jahr + Thema (Leadership, Sales Companion) |
| 99 Archiv | Nur Jahresordner |
| 00 Inbox | Nur Jahresordner (Fehlklassifikation) |

**Empfehlung:** Hybridmodell akzeptieren, aber einheitlich dokumentieren:
- Aktive Kategorien (Finanzen, KV): **Thema-Ordner > Jahr-Unterordner**
- Historisches Archiv (99 Archiv, alte Jahresordner): **Nur Jahr** ist OK

Neue Dispatcher-Dokumente landen bereits korrekt in Thema-Ordnern.

---

## Problem 8: Legacy-Ordner (Converted, Originale)

| Pfad | Inhalt | Aktion |
|------|--------|--------|
| `40 Finanzen/Converted/allgemein/` | 0 MDs | löschen |
| `60 Fahrzeuge/Converted/werkstatt/` | 0 MDs | löschen |
| `Originale/` | 2 MDs (Stubs) | löschen |

---

## Problem 9: Vault-Root-Dateien

Direkt im Vault-Root liegen:
- `CLAUDE.md`, `ENZYME_GUIDE.md`, `VAULT_FRONTMATTER_SPEC.md` → **behalten** (Systemdoku)
- `guide.md` → **behalten** (Vault-Guide)
- `Hochzeitsrede.md` → verschieben nach `10 Persönlich/`
- `Frontmatter Kontrolle (nur md).base` → Obsidian-Base-View, behalten
- `._CLAUDE.md`, `._ENZYME_GUIDE.md`, `._VAULT_GUIDE.md` → macOS-Metadaten, löschen

---

## Kleine Kategorien — konsolidieren oder lassen?

| Ordner | MDs | Empfehlung |
|--------|-----|-----------|
| 55 Garten | 25 | behalten — hat eigene Identität (Piscina, Pflanzen) |
| 82 Digitales | 15 | behalten — Systemdoku (Docker, Passwörter) |
| 85 Wissen | 13 | **prüfen** — könnte in 10 Persönlich/Selbstreflexion |
| 95 Bedienungsanleitungen | 2 | **behalten** — wird wachsen |

---

## Priorisierter Umsetzungsplan

### Phase 1 — Sofort (kein Risiko, hoher Gewinn)
- [ ] **P6: Broken YAML reparieren** — 188 Dateien, Script (30 Min.)
- [ ] **P5: Kategorie-Normalisierung** — bulk-sed, ~100 MDs (30 Min.)
- [ ] **P8: Legacy-Ordner löschen** — 4 leere Ordner (5 Min.)
- [ ] **P9: macOS ._-Dateien im Root löschen**, Hochzeitsrede verschieben (5 Min.)

### Phase 2 — Kurzfristig (niedrig risikoreich)
- [ ] **P4: Ordnerbasierte Kategorie-Befüllung** — Script für 2.438 MDs (2h)
- [ ] **P1: Inbox-Jahresordner → 99 Archiv zusammenführen** — 478 MDs (2h)
- [ ] **P1: Inbox-Root-Loose-Files klassifizieren** — 122 MDs (manuell oder Dispatcher-Rerun)

### Phase 3 — Mittelfristig (Struktur-Entscheidungen nötig)
- [ ] **P3: KV-Entscheidung** — 49 KV behalten vs. nach 40 Finanzen/Versicherungen
- [ ] **P2: Archiv-Tiefenanalyse** — stichprobenartig 2014–2019 Inhalte prüfen

### Phase 4 — Langfristig (optional, hoher Aufwand)
- [ ] **Dispatcher-Batch-Rerun** für ENEX-Dokumente (echte KI-Klassifikation)
- [ ] **32 generische ENEX-Titel** manuell nachbenennen

---

## Gesamtbild: Was wirklich passiert ist

Beim Evernote-Export wurden ~2.929 Notizen in Jahresordner exportiert — ohne inhaltliche
Kategorisierung. Diese wurden als ENEX-Import in den Vault übernommen und landeten in
`99 Archiv/` (Geschäftliches) und `00 Inbox/` (Alles andere).

Nur die ~50 seit dem Dispatcher-Live importierten PDFs haben echte KI-Kategorisierung.

Der Vault hat damit **zwei getrennte Schichten**:
1. **ENEX-Schicht** (2929 MDs): nach Datum sortiert, keine Kategorie, in Inbox/Archiv
2. **Dispatcher-Schicht** (50 MDs): thematisch kategorisiert, volles Frontmatter

Phase 1–2 oben bringt die ENEX-Schicht auf Mindestniveau (Ordner → Kategorie).
Für echte inhaltliche Klassifikation braucht es Batch-Rerun.
