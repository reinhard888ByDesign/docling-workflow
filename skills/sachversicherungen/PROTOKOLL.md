# Skill: Sachversicherungen

**Erstellt:** 2026-05-18
**Zweck:** Extraktion strukturierter Daten aus Sachversicherungs-Dokumenten
(Hausrat, Haftpflicht, Wohngebaeude, Rechtsschutz, Unfall etc.) in
SQLite-Datenbank mit Web-Dashboard und Coverage-Check.

---

## Architektur

```
PDF (40 Finanzen/Versicherungen/)
  |
  +-- [Keyword-Matching] Versicherer+Art + DokTyp vor LLM
  +-- [Ollama] qwen3:4b-instruct → JSON → sachversicherungen.db
```

## Dateien

```
~/.claude/skills/sachversicherungen/
  SKILL.md              -- Claude Code Skill
  analyze.py            -- Extraktionsskript (pdf / text / list / coverage / praemien / init)
  dashboard.py          -- Web-Dashboard (FastAPI, Port 8093)
  schema.sql            -- DB-Schema + 10 Vertraege als Seed
  sachversicherungen.db -- SQLite-Datenbank
  sv-dashboard.service  -- systemd User-Service
  PROTOKOLL.md          -- Diese Datei
```

## Datenbank-Schema (4 Tabellen)

| Tabelle | Inhalt |
|---------|--------|
| vertraege | Stammdaten (Seed, 10 Eintraege) |
| praemien | Zahlungszeitreihe |
| schaeden | Schadensmeldungen |
| aenderungen | Kuendigungen, Anpassungen |

## Vertraege (Seed)

| ID | Art | Versicherer | Person | Aktiv |
|----|-----|------------|--------|-------|
| sv_1 | Hausrat | DOCURA | Reinhard | nein |
| sv_2 | Wohngebaeude | Nuernberger | Reinhard | nein |
| sv_3 | Haftpflicht | HDI | Reinhard | nein |
| sv_4 | Haftpflicht | AXA | Reinhard | nein (gek. 02/2026) |
| sv_5 | Unfall | VGH | Familie | ja |
| sv_6 | D&O | VOV | Reinhard | nein |
| sv_7 | Schliessfach | Versicherungskammer | Reinhard | ja |
| sv_8 | Tier | NV | Reinhard | ja |
| sv_9 | Kombi IT | Reale Mutua | Reinhard | ja |
| sv_10 | Rechtsschutz | WGV | Reinhard | ja |

## Coverage-Logik

- **Haftpflicht:** aktiv wenn `haftpflicht_privat` ODER `kombi_it` aktiv → derzeit via sv_9 (Reale Mutua CASAMIA)
- **Hausrat DE:** sv_1 ausgelaufen, kein aktiver Nachfolger → kein Handlungsbedarf (Wohnsitz IT)
- **Wohngebaeude:** via sv_9 (CASAMIA) fuer beide Seggiano-Objekte abgedeckt

## analyze.py – CLI

```bash
python3 ~/.claude/skills/sachversicherungen/analyze.py init
python3 ~/.claude/skills/sachversicherungen/analyze.py pdf <pfad.pdf>
python3 ~/.claude/skills/sachversicherungen/analyze.py text "<text>" --quelle <pdf>
python3 ~/.claude/skills/sachversicherungen/analyze.py list
python3 ~/.claude/skills/sachversicherungen/analyze.py list --aktiv --land IT
python3 ~/.claude/skills/sachversicherungen/analyze.py coverage
python3 ~/.claude/skills/sachversicherungen/analyze.py praemien --jahr 2025
```

## Web-Dashboard

**URL:** `http://192.168.86.195:8093/`

### Seiten
- **Uebersicht:** Coverage-Check mit Ampel (gruen/gelb/rot), Vertragstabelle
  mit Praemiensumme, gekuendigte Vertraege
- **Praemien:** Jahres-Balken, Filter nach Vertrag/Jahr, Zahlungshistorie

## Dispatcher-Integration

**Status:** offen (Phase 2)

## Batch-Import

**Status:** offen

## Aktueller Stand (2026-05-18)

| Metrik | Wert |
|--------|------|
| Vertraege | 10 geseeded |
| Praemien | 0 (Batch-Import offen) |
| Dashboard | erstellt, nicht gestartet |
