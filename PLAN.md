# Docling-Workflow: Syncthing → Obsidian → enzyme.garden

> **Ziel:** Dokumente vom Mac automatisch zu Markdown konvertieren, im Obsidian Vault speichern und per semantischer Suche durchsuchbar machen — alles lokal auf dem Ryzen. Lokale Ollama-Modelle können über Open WebUI direkt im Vault suchen.

---

## Architektur

```
Mac
      │
      │  Syncthing (Port 22000)
      ▼
[input-docs/]  ←── Eingang: PDF, DOCX, MD
      │
      │  Watcher-Service (inotify)
      ▼
Docling Serve  ←── REST API, Port 5001 (intern)
      │
      ▼
[obsidian-vault/Converted/]
      │
      │  Syncthing
      ▼
Mac: ~/Documents/obsidian-vault/  ←── Obsidian App
      │
      ├── enzyme (Binary auf Ryzen, täglich 23:00 neu indexiert)
      │       │
      │       ├── MCP Server (STDIO)  ←── Claude Code (/enzyme)
      │       │
      │       └── mcpo (MCP→OpenAPI)  ←── Open WebUI (Port 3000)
      │                                         │
      │                                    Ollama (Port 11434)
      │                               (llama3.2:3b, qwen3.5:4b)
```

---

## Ordnerstruktur auf dem Ryzen

```
~/docker/
├── ollama/                          ← Ollama + Open WebUI + mcpo
│   └── docker-compose.yml
└── docling-workflow/
    ├── docker-compose.yml
    ├── watcher/
    │   ├── Dockerfile
    │   └── watcher.py
    ├── docling-cache/               ← Docling Modell-Cache (persistent)
    └── syncthing/
        ├── config/                  ← Syncthing Konfiguration
        └── data/
            ├── input-docs/          ← Syncthing Ordner 1: Eingang
            │   └── _processed/      ← verarbeitete Originale
            └── obsidian-vault/      ← Syncthing Ordner 2: Vault
                ├── .obsidian/
                ├── Converted/       ← Docling Output
                └── Inbox/           ← manuelle Notizen
```

---

## Port-Übersicht

| Service         | Port  | Zweck                                    |
|-----------------|-------|------------------------------------------|
| Ollama          | 11434 | Lokale LLM-Ausführung                    |
| Open WebUI      | 3000  | Chat-Interface für Ollama                |
| mcpo            | 8080  | MCP→OpenAPI Brücke (enzyme für Open WebUI) |
| Docling Serve   | intern | nur im Docker-Netzwerk                  |
| Syncthing Web-UI | 8384 | nur für Erstkonfiguration               |
| Syncthing Sync  | 22000 | Datei-Synchronisation                   |
| enzyme          | STDIO | MCP Server für Claude Code              |

---

## Docker Compose: docling-workflow

`~/docker/docling-workflow/docker-compose.yml`

```yaml
services:
  syncthing:
    image: syncthing/syncthing:latest
    container_name: syncthing
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
    ports:
      - "8384:8384"
      - "22000:22000"
      - "22000:22000/udp"
      - "21027:21027/udp"
    volumes:
      - ./syncthing/config:/var/syncthing/config
      - ./syncthing/data:/data
    networks:
      - docling-net

  docling-serve:
    image: quay.io/docling-project/docling-serve:latest
    container_name: docling-serve
    restart: unless-stopped
    environment:
      - DOCLING_SERVE_NUM_WORKERS=2
    volumes:
      - ./docling-cache:/home/docling/.cache
    networks:
      - docling-net

  watcher:
    build: ./watcher
    container_name: docling-watcher
    restart: unless-stopped
    environment:
      - WATCH_DIR=/data/input-docs
      - OUTPUT_DIR=/data/obsidian-vault/Converted
      - DOCLING_URL=http://docling-serve:5001
    volumes:
      - ./syncthing/data:/data
    networks:
      - docling-net
    depends_on:
      - docling-serve

networks:
  docling-net:
    name: docling-net
```

---

## Docker Compose: ollama

`~/docker/ollama/docker-compose.yml`

Enthält: Ollama, Open WebUI, mcpo

- **Open WebUI** auf Port 3000
- **mcpo** stellt enzyme als OpenAPI-Tool für Open WebUI bereit
- enzyme Tool Server wird automatisch via `TOOL_SERVER_CONNECTIONS` konfiguriert

---

## enzyme.garden

Lokales Binary unter `~/.local/bin/enzyme`. Indexiert den Vault für semantische Suche.

### MCP in Claude Code

`~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "enzyme": {
      "command": "enzyme",
      "args": ["mcp", "--vault", "/home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault"],
      "transport": "stdio"
    }
  }
}
```

In Claude Code: `/enzyme` für semantische Vault-Suche.

### Automatisches Re-Indexing

Crontab (täglich 23:00):
```
0 23 * * * /home/reinhard/.local/bin/enzyme init --vault /home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault >> /home/reinhard/.enzyme/init.log 2>&1
```

### Integration mit Ollama / Open WebUI

mcpo übersetzt enzyme MCP → OpenAPI. Open WebUI ruft mcpo intern über `http://mcpo:8080` auf. Modelle (qwen3.5:4b empfohlen) können damit direkt im Vault suchen.

---

## Verfügbare Ollama-Modelle

| Modell         | Größe | Hinweis                        |
|----------------|-------|--------------------------------|
| qwen3.5:4b     | 3.4 GB | Empfohlen für Tool-Use / enzyme |
| llama3.2:3b    | 2.0 GB | Allgemein                      |
| nomic-embed-text | 274 MB | Embeddings                   |

---

## Nützliche Befehle

```bash
# Status aller Services
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "ollama|webui|mcpo|docling|syncthing"

# enzyme Vault-Status
enzyme status --vault ~/docker/docling-workflow/syncthing/data/obsidian-vault

# enzyme manuell neu indexieren
enzyme init --vault ~/docker/docling-workflow/syncthing/data/obsidian-vault

# Open WebUI Tool Server Logs
docker logs open-webui 2>&1 | grep -i "tool server"

# Docling Watcher Logs
cd ~/docker/docling-workflow && docker compose logs -f watcher
```
