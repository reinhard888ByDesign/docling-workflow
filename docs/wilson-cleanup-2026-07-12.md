# Wilson Cleanup — 2026-07-12

> Gmail deaktiviert, 6 Dienste entfernt, RAM halbiert

## Ausgangslage

Wilson (Raspberry Pi 5, 8GB) hatte nach 6 OOM-Vorfällen und diversen Fixes immer noch zu viele
unnötige Komponenten. Gmail-OAuth war mehrfach korrupt und erzeugte Dauerfehler.

## Was entfernt wurde

### Gmail (doc-processor)
- `email_poll_thread()` deaktiviert (early return)
- `GOG_BIN`, `GOG_ACCOUNT`, `EMAIL_POLL_INTERVAL`, `EMAIL_ARCHIVE_LABEL`, `EMAIL_SEARCH_QUERY` aus systemd service entfernt
- gog binary und client_secret.json bleiben auf der Platte für spätere Nutzung

### Systemd-Dienste (stop + disable)
| Dienst | Grund |
|--------|-------|
| smbd, nmbd, winbind (Samba) | Windows-Freigabe, nie genutzt (~191 MB) |
| ttyd-openclaw | Web-Terminal, SSH ist ausreichend |
| openclaw-watcher | Modell-Fallback-Monitor, nicht kritisch |

### Pakete (apt purge)
- `cups*` — Drucker
- `pulseaudio*` — Audio
- `pipewire*`, `wireplumber*` — Audio
- `modemmanager` — Mobile-Broadband

### Datenmüll
- `~/go/pkg` — 477 MB Go-Modul-Cache (kein Go installiert)

### Health-Check
- `openclaw-health.py` von 6 auf 4 Services reduziert
- openclaw-health.py erstmals in Git versioniert

## Ergebnis

| Metrik | Vorher | Nachher |
|--------|--------|---------|
| RAM (nach Boot) | ~1.4 GiB | **716 MiB** |
| Services (Health) | 6 (2 überflüssig) | **4** (alle essenziell) |
| Gmail-Fehler im Log | Dauerfehler | **Keine** |
| Apt-Pakete | 160.636 | 159.562 (-1.074) |

## Verbliebene Services

| Service | Funktion |
|---------|----------|
| openclaw-gateway | LLM-Agent (Node.js, MemoryMax=800M) |
| doc-processor | Scan-Incoming, Telegram-Bot, Callback-Relay |
| heartbeat | Ryzen Service Monitor |
| laerenbaer | Home Assistant Telegram Bot |
| openclaw-health | Health-Endpoint :8095 |
| syncthing | Datei-Sync Wilson → Ryzen |

## Git

- Commit: `7767c8f`
- Dateien: `wilson/doc_processor.py`, `wilson/doc-processor.service`, `wilson/openclaw-health.py`
