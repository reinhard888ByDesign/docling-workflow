# Docling-Workflow — Projektdokumentation (Historisch)

**Stand:** 2026-03-27
**Server:** Ryzen (192.168.86.195)

> ⚠️ **Veraltet seit 2026-04.** Dieses Dokument beschreibt die ursprüngliche `watcher.py`-Architektur mit Open-WebUI-Knowledge-Ingest. Die aktuelle Pipeline läuft über `document-dispatcher` (mit eigenem Klassifikations-LLM, Telegram-Korrektur, Cache-Reader und Batch-Modus). Aktuelle Referenzen:
> - `PROJEKTDOKUMENTATION.md` — Stand der Live-Architektur
> - `ARCHITEKTUR.md` — flaches Archiv + On-Demand-Verarbeitung
> - `PHASE2_PLAN.md` — laufender Umsetzungsstand (Schritte 2.0-2.4 abgeschlossen 2026-04-19)
>
> Inhalte unten sind nur noch als historische Referenz erhalten.

---

## Überblick

Vollautomatischer lokaler Dokumenten-Import-Workflow ohne Cloud-Abhängigkeit:

1. PDF/DOCX/PPTX/HTML landet per Syncthing im Eingangsordner
2. Docling konvertiert das Dokument zu Markdown
3. Ollama (qwen2.5:7b) analysiert den Inhalt und extrahiert Metadaten als JSON
4. Watcher schreibt YAML-Frontmatter, benennt Datei nach Schema `YYYYMMDD_Absender_Thema` und speichert im Obsidian Vault
5. Original (PDF etc.) wird mit gleichem Basisnamen in `Originale/` archiviert
6. Open WebUI ingestiert die Markdown-Datei automatisch in die Qdrant-Vektordatenbank
7. Über Open WebUI ist der gesamte Vault per RAG abfragbar

---

## Architektur

```
Mac / iPhone
    │
    │  Syncthing (Dateisync)
    ▼
[input-docs/]  ←──── Watcher überwacht diesen Ordner
    │
    ▼
Docling-Serve            PDF → Markdown (OCR-fähig, Tabellen, Formeln)
    │
    ▼
Ollama (qwen2.5:7b)      Markdown → JSON-Metadaten
    │                    (datum, absender, thema, kategorie, tags, zusammenfassung,
    │                     todos, betrag, faellig)
    ▼
[obsidian-vault/Converted/]   YYYYMMDD_Absender_Thema.md  (mit YAML-Frontmatter)
[obsidian-vault/Originale/]   YYYYMMDD_Absender_Thema.pdf (gleicher Basisname)
    │
    ├──► Obsidian (Vault-Ansicht, Syncthing-Sync zurück zum Mac)
    │
    └──► Open WebUI API  →  Qdrant  (RAG-Ingest)
                                        │
                                        └──► Abfragen über Open WebUI Chat
```

---

## Docker-Stacks

### Stack 1: `/home/reinhard/docker/ollama/docker-compose.yml`

| Container | Image | Port | Zweck |
|-----------|-------|------|-------|
| `ollama` | `ollama/ollama:rocm` | 11434 | LLM-Inference (AMD ROCm) |
| `qdrant` | `qdrant/qdrant:latest` | 6333 | Vektordatenbank |
| `open-webui` | `ghcr.io/open-webui/open-webui:main` | 3000 | Chat-UI + RAG-API |

Netzwerk: `ollama-net` (extern referenziert von Stack 2)

Wichtige Open WebUI Umgebungsvariablen:
```
VECTOR_DB=qdrant
QDRANT_URI=http://qdrant:6333
RAG_EMBEDDING_ENGINE=ollama
RAG_EMBEDDING_MODEL=mxbai-embed-large       # 1024-dim, besser für Deutsche Texte
ENABLE_API_KEYS=true
RAG_TOP_K=20
ENABLE_RAG_HYBRID_SEARCH=true
ENABLE_RAG_HYBRID_SEARCH_ENRICHED_TEXTS=true  # Dateiname 3x im BM25
RAG_HYBRID_BM25_WEIGHT=0.8
RAG_TOP_K_RERANKER=20
CHUNK_SIZE=800                              # ~512 Token-Limit von mxbai-embed-large
CHUNK_OVERLAP=80
```

### Stack 2: `/home/reinhard/docker/docling-workflow/docker-compose.yml`

| Container | Image | Port | Zweck |
|-----------|-------|------|-------|
| `syncthing` | `syncthing/syncthing:latest` | 8384, 22000 | Dateisync Mac ↔ Server |
| `docling-serve` | `quay.io/docling-project/docling-serve:latest` | — (intern) | Dokument-Konvertierung |
| `docling-watcher` | Build `./watcher` | — | Orchestrierung des Workflows |

Netzwerke: `docling-net` (intern) + `ollama-net` (extern, für Ollama + Open WebUI)

---

## Dateipfade (auf dem Server)

```
/home/reinhard/docker/
├── ollama/
│   ├── docker-compose.yml          # Ollama + Qdrant + Open WebUI
│   ├── Modelfile.vault             # vault-assistant Ollama-Modell
│   └── entrypoint.sh
└── docling-workflow/
    ├── docker-compose.yml           # Syncthing + Docling + Watcher
    ├── watcher/
    │   ├── Dockerfile
    │   └── watcher.py               # Kernlogik des Workflows
    ├── ingest_vault.py              # Einmal-Skript: Bulk-Ingest bestehender .md-Dateien
    ├── README.md
    ├── PROJEKT.md                   # Diese Datei
    └── syncthing/
        ├── config/                  # Syncthing-Konfiguration
        └── data/
            ├── input-docs/          # Eingangsordner (überwacht)
            └── obsidian-vault/
                ├── Converted/       # Konvertierte .md-Dateien mit Frontmatter
                └── Originale/       # Originaldokumente (umbenannt, gleicher Stem)
```

---

## Dateinamen-Schema

Alle Dateien — `.md` und Original — erhalten denselben Basisnamen:

```
YYYYMMDD_Absender_Thema_in_max_5_Woertern
```

Beispiele:
```
20260108_PVS_Arztrechnung_Liquidation_Januar.md
20260108_PVS_Arztrechnung_Liquidation_Januar.pdf

20260219_Fuhrmann_Amrei_Mietvertrag_Neuvermietung.md
20260219_Fuhrmann_Amrei_Mietvertrag_Neuvermietung.pdf
```

---

## YAML-Frontmatter (vollständiges Beispiel)

```yaml
---
datum: 2026-01-08
absender: PVS
thema: Arztrechnung Liquidation Januar
kategorie: Rechnung
tags:
  - PVS
  - Arztrechnung
  - Liquidation
  - 2026
zusammenfassung: "Die PVS stellt eine Liquidation für ärztliche Leistungen in Höhe von 1.271,06 EUR aus. Der Betrag ist bis zum 31.01.2026 zu begleichen."
betrag: "1.271,06 EUR"
faellig: 2026-01-31
quelle: 20260108_PVS_Arztrechnung_Liquidation_Januar.pdf
original: "[[Originale/20260108_PVS_Arztrechnung_Liquidation_Januar.pdf]]"
erstellt: 2026-03-15
geaendert: 2026-03-27
todos:
  - Rechnung bezahlen bis 31.01.2026
---
```

**Felder:**

| Feld | Beschreibung |
|------|-------------|
| `datum` | Dokumentdatum (aus Inhalt extrahiert) |
| `absender` | Firmen- oder Personenname (ohne Rechtsform) |
| `thema` | Betreff, max. 5 Wörter |
| `kategorie` | Rechnung / Erstattung / Versicherung / Arztbrief / Vertrag / Korrespondenz / Finanzen / Sonstiges |
| `tags` | 3–5 relevante Tags (Firmenname, Thema, Jahr) |
| `zusammenfassung` | 2–3 Sätze auf Deutsch |
| `betrag` | Geldbetrag, falls vorhanden |
| `faellig` | Zahlungstermin, falls vorhanden |
| `quelle` | Ursprünglicher Dateiname (nach Umbenennung) |
| `original` | Obsidian-Wiki-Link zur Originaldatei |
| `erstellt` | Import-Datum (wird bei Re-Import nicht überschrieben) |
| `geaendert` | Datum der letzten Verarbeitung |
| `todos` | Offene Aufgaben, falls vorhanden |

---

## Dokumentkörper

Direkt nach der ersten Überschrift wird ein klickbarer Link zum Original eingefügt:

```markdown
# Liquidationsrechnung PVS

> [Original: 20260108_PVS_Arztrechnung_Liquidation_Januar.pdf](../Originale/20260108_PVS_Arztrechnung_Liquidation_Januar.pdf)

## Inhalt
...
```

---

## watcher.py — Ablauf im Detail

```
process_file(file_path)
  └── Suffix-Check (.pdf, .docx, .doc, .pptx, .html)
  └── Guard: keine Dateien aus Originale/ oder _processed/
  └── wait_for_file_stable()          # max. 30s warten
  └── convert_document(file_path) → stem
        ├── POST /v1/convert/file     → Docling: Datei → Markdown
        ├── analyze_with_ollama()     → Ollama: Markdown → JSON (max. 6000 Zeichen)
        ├── Stem ableiten:            YYYYMMDD_Absender_Thema (max. 50 Zeichen)
        ├── new_orig_name:            stem + original Extension
        ├── build_frontmatter()       → YAML-Header mit allen Metafeldern
        ├── Originallink einfügen     → nach erster Überschrift
        ├── Datei schreiben           → obsidian-vault/Converted/stem.md
        └── ingest_to_knowledge()
              ├── POST /api/v1/files/
              └── POST /api/v1/knowledge/{id}/file/add
  └── shutil.move(original → Originale/new_orig_name)
```

---

## Open WebUI — Vault Assistant

Modell `vault-assistant` in Ollama mit System-Prompt:
- Antwortet nur auf Deutsch
- Immer Stichpunkte (Spiegelstriche)
- Vault Knowledge Base automatisch eingebunden (kein `#Vault` nötig)
- Gibt Originallink aus, wenn ein konkretes Dokument referenziert wird

Modelfile: `/home/reinhard/docker/ollama/Modelfile.vault`

Neu erstellen nach Ollama-Reset:
```bash
docker cp Modelfile.vault ollama:/tmp/Modelfile.vault
docker exec ollama ollama create vault-assistant -f /tmp/Modelfile.vault
```

---

## RAG-Konfiguration (Qdrant + mxbai-embed-large)

| Parameter | Wert | Grund |
|-----------|------|-------|
| Embedding-Modell | `mxbai-embed-large` | 1024-dim, kennt deutsche Abkürzungen (z.B. PVS) |
| CHUNK_SIZE | 800 Zeichen | Entspricht ~512 Token (Modell-Limit) |
| CHUNK_OVERLAP | 80 Zeichen | Kontext zwischen Chunks |
| RAG_TOP_K | 20 | Kandidaten aus Vektor-Suche |
| BM25_WEIGHT | 0.8 | Keyword-Treffer stark gewichten |
| TOP_K_RERANKER | 20 | Alle Kandidaten ans Modell weitergeben |

---

## Bekannte Probleme & Lösungen

| Problem | Ursache | Lösung |
|---------|---------|--------|
| 504 Timeout bei großen PDFs | `DOCLING_SERVE_MAX_SYNC_WAIT` zu kurz | Auf 600s erhöht |
| Falsche RAG-Ergebnisse | `nomic-embed-text` kennt deutsche Kürzel nicht | Wechsel auf `mxbai-embed-large` |
| Dimension Mismatch nach Modellwechsel | Qdrant-Collection mit alter Dimension | Collections löschen, neu ingestieren |
| Embedding-Fehler (zu große Chunks) | mxbai hat 512-Token-Limit | CHUNK_SIZE=800 |
| BM25-Treffer vom Reranker überschrieben | RerankCompressor re-ranked nach Cosine | `RAG_TOP_K_RERANKER=20` |

---

## Einmalige Aktionen

### Bulk-Ingest bestehender Dateien
```bash
cd /home/reinhard/docker/docling-workflow
python3 ingest_vault.py
```

### Qdrant-Collections zurücksetzen
```bash
curl -X DELETE http://192.168.86.195:6333/collections/open-webui_knowledge
curl -X DELETE http://192.168.86.195:6333/collections/open-webui_files
python3 ingest_vault.py
```

### Watcher nach Code-Änderung neu bauen
```bash
cd /home/reinhard/docker/docling-workflow
docker compose up -d --build watcher
docker logs docling-watcher -f
```

---

## Syncthing-Ordner

| Ordner | Pfad (Server) | Sync-Ziel Mac |
|--------|---------------|---------------|
| input-docs | `syncthing/data/input-docs/` | Eingangsordner für neue Dokumente |
| obsidian-vault | `syncthing/data/obsidian-vault/` | Obsidian-Vault (inkl. Converted/, Originale/) |

---

## Open WebUI API Key

```
sk-6733607e160c777c1cd1315d4aa86f200ad2d11dfe19a2870ba625fcfa99d0c7
```

Knowledge Base ID (Vault): `5ade7fb4-1f85-4bc9-a085-84cc970709ea`
