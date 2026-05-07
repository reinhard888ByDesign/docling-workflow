# Docling Workflow

Vollautomatischer, lokaler Dokumenten-Import-Workflow ohne Cloud-Abhängigkeit.

PDFs und ENEX-Dateien (Evernote-Export) werden per Syncthing auf den Ryzen-Server übertragen, per Docling OCR verarbeitet, per DeepSeek-API klassifiziert und strukturiert im Obsidian Vault abgelegt.

## Architektur

```
Mac / iPhone / Scanner / Evernote-Export
         │
         │  Syncthing
         ▼
 input-dispatcher/          ← Dispatcher überwacht diesen Ordner
 input-dispatcher/enex/     ← ENEX-Dateien (Evernote-Export)
         │
         ├── Wilson (Pi)    ← Physischer Eingang (Scanner → Pi → Ryzen)
         │
         ▼
 Dispatcher (dispatcher.py, Port 8765)
         │
         ├── Docling-Serve  PDF → Markdown (OCR)
         ├── DeepSeek API   Markdown → JSON-Metadaten (LLM)
         └── Telegram       Bestätigungen + Benachrichtigungen
         │
         ▼
 Vault/YYYYMMDD_Absender_Thema.md    ← Metadaten + OCR-Volltext
 Vault/Anlagen/YYYYMMDD_...pdf       ← Original-PDF
         │
         └──► Obsidian (Mac/iPhone via Syncthing)
              enzyme MCP → Claude Code / Open WebUI (Suche)
```

## Dashboards

| URL | Funktion |
|-----|----------|
| `http://ryzen:8765/` | Übersicht aller Dienste (Health, Logs) |
| `http://ryzen:8765/pipeline` | Verarbeitungs-Queue + Schritt-Statistiken |
| `http://ryzen:8765/enex` | ENEX-Import-Übersicht + Dokument-Anzeige |
| `http://ryzen:8765/db` | Datenbank-Verwaltung (Aussteller, Kategorien) |
| `http://ryzen:8765/vault` | Vault-Struktur-Übersicht |

## Dienste (Docker)

| Container | Funktion | Port |
|-----------|----------|------|
| `dispatcher` | Kern-Service: Queue, OCR-Routing, Klassifikation, Web-UI | 8765 |
| `docling-serve` | PDF → Markdown OCR | intern |
| `enex-ocr-worker` | Hintergrund-OCR für ENEX-Importe | intern |
| `cache-reader` | Vault-Index-Cache für schnelle Suche | intern |
| `syncthing` | Datei-Sync Mac ↔ Ryzen | 8384 |

Wilson auf dem Pi (`~/raspberry/wilson/`) ist kein Docker-Container, sondern ein systemd-Dienst.

## Voraussetzungen

- Docker + Docker Compose
- DeepSeek API Key (`DEEPSEEK_API_KEY` in `.env`)
- Telegram Bot Token + Chat-ID (für Benachrichtigungen)
- enzyme-MCP-Server (optional, für Vault-Suche via Claude Code)

## Start

```bash
docker compose up -d
```

## Erzeugtes Frontmatter (Dispatcher-Dokumente)

```yaml
---
datum: 2026-01-08
absender: PVS
adressat: Reinhard
thema: Arztrechnung Liquidation Januar
kategorie: Rechnung
tags:
  - PVS
  - Arztrechnung
zusammenfassung: "Die PVS stellt eine Liquidation für ärztliche Leistungen aus."
betrag: "1.271,06 EUR"
faellig: 2026-01-31
ocr_status: completed
original: '[[Anlagen/20260108_PVS_Arztrechnung_Liquidation_Januar.pdf]]'
---
```

## Erzeugtes Frontmatter (ENEX-Importe)

```yaml
---
title: Cartier Uhrenreparatur
source: evernote
imported: 2026-02-08 18:29:33
Datum_original: '2001-12-27'
original: '[[Anlagen/20011227-Cartier_Uhrenreparatur-76cf60c1.pdf]]'
# oder bei mehreren Anhängen:
anlagen:
  - '[[Anlagen/...-hash1.pdf]]'
  - '[[Anlagen/...-hash2.pdf]]'
---
```

## Unterstützte Eingangsformate

`.pdf` · `.docx` · `.xlsx` · `.pptx` · `.enex`

## Dispatcher neu bauen (nach Code-Änderungen)

```bash
cd /home/reinhard/docker/RYZEN\ -\ docling-workflow
docker stop dispatcher && docker rm dispatcher
docker build -t dispatcher ./dispatcher
docker compose up -d dispatcher
```

## Logs

```bash
docker logs dispatcher -f
docker logs enex-ocr-worker -f
```
