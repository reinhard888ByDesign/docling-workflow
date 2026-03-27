# Docling Workflow

Vollautomatischer, lokaler Dokumenten-Import-Workflow ohne Cloud-Abhängigkeit.

PDFs, DOCX, PPTX und HTML-Dateien werden per Syncthing auf den Server übertragen, von Docling in Markdown konvertiert, von Ollama analysiert und strukturiert im Obsidian Vault abgelegt — inklusive automatischem RAG-Ingest in Open WebUI (Qdrant).

## Architektur

```
Mac / iPhone
    │
    │  Syncthing
    ▼
input-docs/          ← Watcher überwacht diesen Ordner
    │
    ▼
Docling-Serve        PDF/DOCX/PPTX/HTML → Markdown
    │
    ▼
Ollama (lokal)       Markdown → JSON-Metadaten
    │                datum, absender, thema, kategorie, tags,
    │                zusammenfassung, todos, betrag, faellig
    ▼
obsidian-vault/Converted/   YYYYMMDD_Absender_Thema.md
obsidian-vault/Originale/   YYYYMMDD_Absender_Thema.pdf   ← gleicher Dateiname
    │
    ├──► Obsidian (Vault auf Mac via Syncthing)
    └──► Open WebUI → Qdrant (RAG)
```

## Voraussetzungen

- Docker + Docker Compose
- Laufender Ollama-Stack (siehe `../ollama/docker-compose.yml`):
  - `ollama` mit ROCm oder CUDA
  - `qdrant`
  - `open-webui`
- Netzwerk `ollama-net` muss existieren

## Schnellstart

```bash
# 1. Repo klonen
git clone https://github.com/reinhard888ByDesign/docling-workflow
cd docling-workflow

# 2. Umgebungsvariablen anpassen (docker-compose.yml → watcher-Service)
#    WEBUI_API_KEY, OLLAMA_MODEL, etc.

# 3. Starten
docker compose up -d

# 4. Bestehende Dateien einmalig ingestieren
python3 ingest_vault.py
```

## Konfiguration

Alle Einstellungen werden als Umgebungsvariablen im `watcher`-Service gesetzt:

| Variable | Standard | Beschreibung |
|----------|---------|-------------|
| `WATCH_DIR` | `/data/input-docs` | Eingangsordner (überwacht) |
| `OUTPUT_DIR` | `/data/obsidian-vault/Converted` | Zielordner für .md-Dateien |
| `ORIGINALS_DIR` | `/data/obsidian-vault/Originale` | Archiv der Originaldokumente |
| `DOCLING_URL` | `http://docling-serve:5001` | Docling Serve API |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama API |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Modell für Metadaten-Extraktion |
| `WEBUI_URL` | `http://open-webui:8080` | Open WebUI API |
| `WEBUI_API_KEY` | *(leer)* | API-Key für RAG-Ingest |
| `KNOWLEDGE_NAME` | `Vault` | Name der Knowledge Base in Open WebUI |

## Erzeugtes Frontmatter

```yaml
---
datum: 2026-01-08
absender: PVS
thema: Arztrechnung Liquidation Januar
kategorie: Rechnung
tags:
  - PVS
  - Arztrechnung
  - 2026
zusammenfassung: "Die PVS stellt eine Liquidation für ärztliche Leistungen aus."
betrag: "1.271,06 EUR"
faellig: 2026-01-31
quelle: 20260108_PVS_Arztrechnung_Liquidation_Januar.pdf
original: "[[Originale/20260108_PVS_Arztrechnung_Liquidation_Januar.pdf]]"
erstellt: 2026-03-27
geaendert: 2026-03-27
---
```

## Unterstützte Formate

`.pdf` · `.docx` · `.doc` · `.pptx` · `.html`

## Open WebUI RAG

Für den Chat `vault-assistant`-Modell verwenden — es hat die Vault Knowledge Base
automatisch eingebunden und antwortet auf Deutsch in Stichpunkten.

Erstellt via:
```bash
ollama create vault-assistant -f ollama/Modelfile.vault
```

## Bulk-Ingest bestehender Dateien

```bash
WEBUI_URL=http://localhost:3000 \
WEBUI_API_KEY=sk-... \
VAULT_PATH=/pfad/zum/vault/Converted \
python3 ingest_vault.py
```

## Wartung

```bash
# Watcher neu bauen (nach Code-Änderungen)
docker compose up -d --build watcher

# Logs
docker logs docling-watcher -f

# Qdrant zurücksetzen (nach Embedding-Modell-Wechsel)
curl -X DELETE http://localhost:6333/collections/open-webui_knowledge
curl -X DELETE http://localhost:6333/collections/open-webui_files
python3 ingest_vault.py
```
