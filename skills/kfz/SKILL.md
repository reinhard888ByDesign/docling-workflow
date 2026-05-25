---
name: kfz
description: >
  Analysiert KFZ-Dokumente (Versicherungen, Schaeden, Werkstatt, Steuer,
  Zulassung) aus Deutsch und Italienisch. Extrahiert strukturierte Daten
  und schreibt sie in die SQLite-Datenbank kfz.db.
argument-hint: [PDF-Pfad oder "list" oder "aktiv" oder "kosten"]
allowed-tools: [Bash, Read, Glob, Grep]
---

# KFZ -- Fahrzeuge & KFZ-Versicherungen

## Was dieser Skill tut

Extrahiert aus KFZ-Dokumenten alle relevanten Informationen und speichert
sie strukturiert in `~/.claude/skills/kfz/kfz.db`.

Unterstutzte Sprachen: Deutsch und Italienisch.

## Trigger

**Automatisch** wenn der User eine Datei offnet, deren Pfad `60 Fahrzeuge/`
enthalt oder deren Frontmatter `kategorie: fahrzeuge` hat.

**Manuell** via `/kfz <pdf-pfad>`, `/kfz list`, `/kfz aktiv`, `/kfz kosten`.

## Workflow -- PDF analysieren

1. **Ziel-PDF finden**: Wenn der User ein Markdown-File geoffnet hat, lies das
   `original:`-Feld aus dem Frontmatter und folge dem Wikilink zur PDF in
   `Anlagen/`. Wenn der User direkt einen PDF-Pfad nennt, verwende diesen.

2. **Extraktion starten**:
   ```bash
   python3 ~/.claude/skills/kfz/analyze.py pdf "<absoluter-pfad-zur-pdf>"
   ```

3. **Ergebnis interpretieren**: Das Skript zeigt Fahrzeug-ID, Dokumenttyp,
   und extrahierte Felder an.

4. **Bei Parse-Fehlern**: Wenn Ollama kein valides JSON liefert, zeige die
   Roh-Antwort an und frage ob ein anderer Versuch mit `--model gemma4:26b`
   gestartet werden soll.

## Workflow -- Text analysieren (Wilson-Bypass)

Wenn der Wilson-Sidecar-Beschreibungstext vorliegt (kein PDF-Zugriff):

```bash
python3 ~/.claude/skills/kfz/analyze.py text \
  "<beschreibungstext>" --quelle "<dateiname.pdf>"
```

## Abfragen

```bash
# Alle Eintrage
python3 ~/.claude/skills/kfz/analyze.py list

# Nur kfz_2 (Ape)
python3 ~/.claude/skills/kfz/analyze.py list --kfz kfz_2

# Nur aktive Versicherungen (mit Ablauf-Warnung)
python3 ~/.claude/skills/kfz/analyze.py aktiv

# Kostenubersicht pro Fahrzeug
python3 ~/.claude/skills/kfz/analyze.py kosten
python3 ~/.claude/skills/kfz/analyze.py kosten --kfz kfz_2
```

## Aktive Fahrzeuge

| ID      | Kennzeichen | Typ           | Land | Anmerkung |
|---------|------------|---------------|------|-----------|
| `kfz_1` | GY243ZF    | PKW (Tesla MY)| IT   | Ehemals TS-MY8888 (DE) |
| `kfz_2` | GY964ZF    | Ape (Dreirad) | IT   | Zurich-Versicherung (MB930145) |
| `kfz_3` | FR-Y1544   | Anhanger      | DE   | WGV-Versicherung |
| `kfz_4` | TS-QZ566   | Anhanger      | DE   | Kauf Mai 2025 |

## Datenbank-Schema

6 Tabellen in `~/.claude/skills/kfz/kfz.db`:

| Tabelle | Inhalt |
|---------|--------|
| fahrzeuge | Stammdaten (Seed) |
| versicherungen | KFZ-Versicherungsvertrage |
| schaeden | Schadensmeldungen und -regulierungen |
| reparaturen | Werkstatt, Wartung, TUV |
| steuern | KFZ-Steuer |
| zulassungen | Fahrzeugschein, Carta di Circolazione |

## Keyword-Matching (vor LLM)

Kennzeichen und Dokumenttypen werden vor dem LLM-Aufruf per Regex erkannt.
Das Keyword-Ergebnis hat immer Vorrang vor der LLM-Klassifikation.

## Dashboard

http://ryzen:8094  (KFZ-Dashboard -- geplant Phase C)
