---
name: sachversicherungen
description: >
  Analysiert Sachversicherungs-Dokumente (Hausrat, Haftpflicht, Wohngebaeude,
  Rechtsschutz, Unfall etc.) und dokumentiert Deckungsumfang in sachversicherungen.db.
argument-hint: "[PDF-Pfad oder list|coverage|praemien]"
allowed-tools: [Bash, Read, Glob, Grep]
---

# Sachversicherungen — Coverage-Tracker

## Trigger

**Automatisch** wenn der User eine Datei aus `40 Finanzen/Versicherungen/`
mit Sachversicherungs-Bezug oeffnet.

**Manuell** via `/sachversicherungen <pdf-pfad>`, `/sachversicherungen list`,
`/sachversicherungen coverage`, `/sachversicherungen praemien`.

## Workflow

```bash
# DB initialisieren
python3 ~/.claude/skills/sachversicherungen/analyze.py init

# PDF analysieren
python3 ~/.claude/skills/sachversicherungen/analyze.py pdf "<pfad.pdf>"

# Text (Wilson-Bypass)
python3 ~/.claude/skills/sachversicherungen/analyze.py text "<text>" --quelle "<pdf>"
```

## Abfragen

```bash
python3 ~/.claude/skills/sachversicherungen/analyze.py list
python3 ~/.claude/skills/sachversicherungen/analyze.py list --aktiv
python3 ~/.claude/skills/sachversicherungen/analyze.py list --land IT
python3 ~/.claude/skills/sachversicherungen/analyze.py coverage
python3 ~/.claude/skills/sachversicherungen/analyze.py praemien --jahr 2025
```

## Bekannte Vertraege (10)

| ID    | Art                | Versicherer                  | Person  | Aktiv |
|-------|--------------------|------------------------------|---------|-------|
| sv_1  | Hausrat            | DOCURA                       | Reinhard| nein  |
| sv_2  | Wohngebaeude       | Nuernberger PrivatSchutz     | Reinhard| nein  |
| sv_3  | Privathaftpflicht  | HDI                          | Reinhard| nein  |
| sv_4  | Privathaftpflicht  | AXA                          | Reinhard| nein  |
| sv_5  | Unfall             | VGH                          | Familie | ja    |
| sv_6  | D&O                | VOV                          | Reinhard| nein  |
| sv_7  | Schliessfach       | Versicherungskammer Bayern   | Reinhard| ja    |
| sv_8  | Tier (Hund)        | NV Versicherungen            | Reinhard| ja    |
| sv_9  | Kombi IT           | Reale Mutua CASAMIA          | Reinhard| ja    |
| sv_10 | Rechtsschutz       | WGV                          | Reinhard| ja    |

## Dashboard

http://ryzen:8093 (geplant)
