# Skill: Altersvorsorge

**Erstellt:** 2026-05-18
**Zweck:** Extraktion strukturierter Daten aus Altersvorsorge-Dokumenten
(Standmitteilungen, Versicherungsscheine, Aenderungen) in SQLite-Datenbank
mit Web-Dashboard.

---

## Architektur

```
PDF (40 Finanzen/Versicherungen/)
  |
  +-- [Keyword-Matching] Vertragsnummer + DokTyp vor LLM
  +-- [Ollama] qwen3:4b-instruct → JSON → altersvorsorge.db
```

## Dateien

```
~/.claude/skills/altersvorsorge/
  SKILL.md              -- Claude Code Skill
  analyze.py            -- Extraktionsskript (pdf / text / list / verlauf / gesamt / init)
  dashboard.py          -- Web-Dashboard (FastAPI, Port 8092)
  schema.sql            -- DB-Schema + 9 Vertraege als Seed
  altersvorsorge.db     -- SQLite-Datenbank
  av-dashboard.service  -- systemd User-Service
  PROTOKOLL.md          -- Diese Datei
```

## Datenbank-Schema (3 Tabellen)

| Tabelle | Inhalt |
|---------|--------|
| vertraege | Stammdaten (Seed, 9 Eintraege) |
| standmitteilungen | Kapitalentwicklung (Zeitreihe) |
| aenderungen | Beitragsfreistellung, Anpassungen |

## Vertraege (Seed)

| ID | Versicherer | Art | Person | Status |
|----|------------|-----|--------|--------|
| av_1 | AXA | kapitalbildend | Reinhard | aktiv |
| av_2 | Nuernberger | direktversicherung | Reinhard | aktiv |
| av_3 | Nuernberger PK | pensionskasse | Reinhard | aktiv |
| av_4 | Nuernberger U-Kasse | ukasse | Reinhard | aktiv |
| av_5 | Nuernberger | direktversicherung | Marion | aktiv |
| av_6 | Nuernberger U-Kasse | ukasse | Marion | aktiv |
| av_7 | LV1871 | basisrente | Marion | beitragsfrei |
| av_8 | HDI-Gerling | fondsgebunden | Reinhard | aktiv |
| av_9 | Allvest | kapitalbildend | ? | aktiv |

## Keyword-Matching

- Vertragsnummern (Regex mit optionalen Leerzeichen)
- Nachhaltigkeitsdokumente werden automatisch geskippt

## analyze.py – CLI

```bash
python3 ~/.claude/skills/altersvorsorge/analyze.py init
python3 ~/.claude/skills/altersvorsorge/analyze.py pdf <pfad.pdf>
python3 ~/.claude/skills/altersvorsorge/analyze.py text "<text>" --quelle <pdf>
python3 ~/.claude/skills/altersvorsorge/analyze.py list
python3 ~/.claude/skills/altersvorsorge/analyze.py verlauf --vertrag av_7
python3 ~/.claude/skills/altersvorsorge/analyze.py gesamt
```

## Web-Dashboard

**URL:** `http://192.168.86.195:8092/`

### Seiten
- **Uebersicht:** Gesamtvermoegen (Reinhard/Marion), Vertragstabelle mit
  Sparklines, Ablauftermine
- **Vertrag-Detail:** `/vertrag/av_7` — Guthaben-Chart, alle Standmitteilungen,
  Aenderungen

## Dispatcher-Integration

**Status:** offen (Phase 2)

## Batch-Import

**Status:** offen

## Aktueller Stand (2026-05-18)

| Metrik | Wert |
|--------|------|
| Vertraege | 9 geseeded |
| Standmitteilungen | 0 (Batch-Import offen) |
| Dashboard | erstellt, nicht gestartet |
