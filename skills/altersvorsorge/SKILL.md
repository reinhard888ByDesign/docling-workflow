---
name: altersvorsorge
description: >
  Analysiert Altersvorsorge-Dokumente (Standmitteilungen, Rentenversicherungen)
  und dokumentiert die Kapitalentwicklung in altersvorsorge.db.
argument-hint: "[PDF-Pfad oder list|verlauf|gesamt]"
allowed-tools: [Bash, Read, Glob, Grep]
---

# Altersvorsorge — Rentenvertraege & Kapitalentwicklung

## Trigger

**Automatisch** wenn der User eine Datei aus `40 Finanzen/Versicherungen/`
mit Altersvorsorge-Bezug oeffnet.

**Manuell** via `/altersvorsorge <pdf-pfad>`, `/altersvorsorge list`,
`/altersvorsorge verlauf`, `/altersvorsorge gesamt`.

## Workflow

```bash
# DB initialisieren
python3 ~/.claude/skills/altersvorsorge/analyze.py init

# PDF analysieren
python3 ~/.claude/skills/altersvorsorge/analyze.py pdf "<pfad.pdf>"

# Text (Wilson-Bypass)
python3 ~/.claude/skills/altersvorsorge/analyze.py text "<text>" --quelle "<pdf>"
```

## Abfragen

```bash
python3 ~/.claude/skills/altersvorsorge/analyze.py list
python3 ~/.claude/skills/altersvorsorge/analyze.py list --vertrag av_7
python3 ~/.claude/skills/altersvorsorge/analyze.py verlauf --vertrag av_7
python3 ~/.claude/skills/altersvorsorge/analyze.py gesamt
```

## Bekannte Vertraege (9)

| ID    | Versicherer        | Art                | Person  |
|-------|--------------------|--------------------|---------|
| av_1  | AXA                | kapitalbildend     | Reinhard|
| av_2  | Nuernberger        | direktversicherung | Reinhard|
| av_3  | Nuernberger PK     | pensionskasse      | Reinhard|
| av_4  | Nuernberger U-Kasse| ukasse             | Reinhard|
| av_5  | Nuernberger        | direktversicherung | Marion  |
| av_6  | Nuernberger U-Kasse| ukasse             | Marion  |
| av_7  | LV1871             | basisrente         | Marion  |
| av_8  | HDI-Gerling        | fondsgebunden      | Reinhard|
| av_9  | Allvest            | kapitalbildend     | ?       |

## Dashboard

http://ryzen:8092 (geplant)
