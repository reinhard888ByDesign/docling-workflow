# Wilson — Raspberry Pi 5 Infrastruktur

Stand: 2026-05-15

## Hardware & Zugang

| | |
|---|---|
| **IP** | 192.168.3.124 |
| **SSH-Alias** | `wilson` |
| **Hardware** | Raspberry Pi 5, 8 GB RAM, NVMe-Speicher |
| **OS** | Ubuntu Server (cloud-init) |

## openclaw

**Version:** 2026.5.7 — installiert via npm-global (`~/.npm-global/bin/openclaw`)

**Workspace:** `~/.openclaw/`
- `~/.openclaw/scripts/` — Cron-Scripts, Services-Scripts
- `~/.openclaw/workspace/scripts/` — Workspace-Scripts (cisterna etc.)
- `~/.openclaw/agents/` — Agent-Definitionen

**Ports:**
- `18789` — openclaw Gateway
- `7681` — ttyd TUI (Web-Terminal)
- `8770` — Telegram-Callback-Relay (für Dispatcher)

## Systemd User Services

```bash
systemctl --user status openclaw-gateway.service
systemctl --user status openclaw-watcher.service
systemctl --user status doc-processor.service
systemctl --user status ai-assistant.service
systemctl --user status heartbeat.service
```

| Service | Beschreibung |
|---|---|
| `openclaw-gateway` | Node.js Gateway, Port 18789 |
| `openclaw-watcher` | Model Fallback Watcher |
| `doc-processor` | Dokument-Pipeline + Telegram Bot (Token: 8382100394) |
| `ai-assistant` | AI-Assistent Bot (Token: 8621101278) |
| `laerenbaer` | Laerenbaer-Prozess |
| `heartbeat` | Health-Check: Dispatcher/Cache/Docling/Ollama alle 90s |

Deploy nach Änderungen in `wilson/`:
```bash
./wilson/deploy-wilson.sh
```

## Cron-Jobs (user reinhard)

```
*/30 * * * *    ~/.openclaw/workspace/scripts/cisterna-check.sh
0 2 * * *       ~/.openclaw/scripts/openclaw-update.sh >> ~/.openclaw/openclaw-update.log 2>&1
30 8 * * 1      ~/.openclaw/scripts/remind-zahnreinigung.sh
```

### cisterna-check.sh
- Alle 30 Minuten: Füllstand `sensor.cisterna_flussigkeitsfullstand` via Home Assistant API
- Telegram-Alert bei Unterschreiten von 50 / 25 / 10 % (je einmal pro Tag)
- State: `~/.openclaw/workspace/.cisterna_alarm_state`

### openclaw-update.sh
- Täglich 02:00 Uhr: `npm show openclaw version` vs installierter Version
- Bei Update: `npm install -g openclaw@latest` + Services restart
- Log: `~/.openclaw/openclaw-update.log`
- Status: `~/.openclaw/openclaw-update.status`

## Monitoring von Ryzen

Das Script `wilson/openclaw-monitor.sh` läuft auf dem Ryzen alle 10 Minuten:

```bash
# Crontab auf Ryzen:
*/10 * * * * /home/reinhard/.local/bin/openclaw-monitor.sh
```

**Prüft:** `openclaw-gateway.service` + `openclaw-watcher.service` via SSH  
**Alarmiert per Telegram** bei: Wilson nicht erreichbar, Service down  
**Entwarnung** wenn Services wieder aktiv  
**State-File:** `/tmp/.openclaw-monitor-state` (verhindert Spam)

## Logging

Ab 2026-05-14: **Journal ist persistent** (`/var/log/journal/`).

```bash
# Einrichten (einmalig):
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald

# Logs ansehen:
journalctl -b -1          # vorheriger Boot
journalctl -p err..emerg  # Fehler
```

## Samba / Scanner-Eingang

- Share: `smb://192.168.3.124/incoming` (guest, kein Passwort)
- Path auf Wilson: `/home/reinhard/incoming`
- Scanner (Mac) legt PDF direkt ab → `doc-processor.service` verarbeitet sofort

## Telegram-Callback-Relay

Wilson's `doc-processor` ist der **exklusive Telegram-Poller** (Ryzen hat `DISABLE_TELEGRAM_POLL=1`). Damit Inline-Keyboard-Buttons aus Ryzen-Nachrichten funktionieren, leitet Wilson unbekannte Callback-Prefixe an den Dispatcher weiter:

```
User klickt Button in Ryzen-Nachricht
  → Wilson empfängt Callback via getUpdates
  → Bekannte Prefixe (confirm/reject/correct/gkat/…): Wilson verarbeitet selbst
  → Unbekannte Prefixe (cat:/sc:/st:/ok:/cancel:): POST → DISPATCHER_URL/api/tg/callback
  → Dispatcher antwortet: Kategorie-Keyboard oder Korrektur
```

Ryzen-Endpoint: `POST http://192.168.86.195:8765/api/tg/callback`  
Payload: `{callback_id, data, chat_id, msg_id, msg_text}`

## Bekannte Probleme / Vorfälle

| Datum | Problem | Ursache | Fix |
|---|---|---|---|
| 2026-05-14 | Pi zwischen 02:00–06:38 abgestürzt | Unbekannt (EXT4-Orphan-Cleanup beim Boot, kein Unter-Voltage/OOM) | Neustart, Journal persistent aktiviert, Monitoring eingerichtet |
