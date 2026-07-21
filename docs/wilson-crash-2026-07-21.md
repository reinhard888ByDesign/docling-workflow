# Wilson Ausfall 20.–21. Juli 2026 — Node-Inkompatibilität + Watchdog-Reboot

## Chronologie

### 14.–20. Juli: Der 7. Ausfall (Node-Inkompatibilität)

| Datum | Ereignis |
|---|---|
| 14.07. ~02:00 | Gateway-Prozess stirbt (Ursache unklar, Kernel-Log verloren) |
| 14.–16.07. | System läuft ohne Gateway weiter (doc_processor, laerenbaer, cron) |
| 16.07. 02:00 | `openclaw-update.sh` installiert OpenClaw **v2026.7.1** (braucht Node ≥22.22.3) |
| 16.07. 13:29 | System rebootet, Gateway Crash-Loop: **Node v22.22.1 zu alt** |
| 16.–20.07. | 4 Tage Crash-Loop, 22 Cron-Jobs stalled, Monitor erkennt ssh_down |
| 20.07. 13:10 | Node v22.23.1 installiert, Gateway läuft wieder |

**Root Cause 7. Ausfall:** KEIN OOM — Node.js-Inkompatibilität nach OpenClaw-Auto-Update.

### 20.–21. Juli: Der 8. Ausfall (Watchdog-Reboot)

| Datum | Ereignis |
|---|---|
| 20.07. 13:32 | Alle Fixes deployed, 4/4 Services, Monitor `ok` |
| 20.07. 22:00 | Gateway 645 MB RSS, läuft normal |
| 20.07. 23:59 | **3 Cron-Jobs gleichzeitig**: Session-Reset + Feng-Shui-Löschen + DB-Sync |
| 21.07. 00:00 | Gateway neugestartet (Session-Reset), 482 MB RSS |
| 21.07. 00:30 | Letzter Memory-Logger-Eintrag: Gateway 509 MB, System OK |
| 21.07. **00:47:33** | **Hardware Watchdog löst Reboot aus** |
| 21.07. 00:50 | Monitor erkennt `ssh_down` |
| 21.07. 07:57 | Physischer Reboot durch Reinhard |

**Root Cause 8. Ausfall:** Hardware Watchdog (BCM2835, 60s Timeout). Um Mitternacht
kollidierten 4 isolierte openclaw-Cron-Sessions (jede startet einen eigenen Node-Prozess,
≥200 MB RAM). Systemd kam unter Memory-Pressure nicht mehr zum Watchdog-Füttern
→ Reboot nach 60 Sekunden.

## Die Watchdog-Deadlock-Kette

```
23:59  Session-Reset (isolated)     ─┐
23:59  Feng-Shui-Löschen (isolated)  ─┤ 4 Node-Prozesse parallel
23:59  DB-Sync (isolated)            ─┤ je ≥200 MB → ~1 GB extra RAM
00:00  Polling-Watcher (isolated)    ─┘
       │
       ▼
  Systemd unter Memory-Pressure, kann Watchdog nicht mehr petten
       │
       ▼ (60 Sekunden)
  BCM2835 Hardware Watchdog → INSTANT REBOOT
       │
       ▼
  Alle Journal-Daten vom laufenden Boot VERLOREN (kein sync vor Reboot)
```

## Implementierte Fixes (2026-07-21)

### 1. Cron-Jobs um Mitternacht entzerrt

| Cron-Job | Vorher | Nachher | Befehl |
|---|---|---|---|
| Session-Reset | 23:59 | **00:15** | `openclaw cron edit --cron '15 0 * * *'` |
| Feng-Shui-Löschen | 23:59 | **00:05** | `openclaw cron edit --cron '5 0 * * *'` |
| Dispatcher-DB-Sync | `*/5 * * * *` (288×/Tag) | `*/5 5-23 * * *` (228×/Tag) | Nachtruhe 00-05h |
| Feng-Shui-Briefing | 07:45 | **07:50** | Entzerrt vom Tages-Briefing (07:30) |

### 2. Tages-Briefing von LLM-Agent auf deterministisches Python-Script umgestellt

- Script: `~/.openclaw/scripts/tagesbriefing.py`
- Datenquellen: HA (Wetter, PV, Batterie, Cisterna), portfolio.db, Vault/Memory, Gua-Calculator
- Cron-Job: `openclaw cron edit --message 'python3 ~/.openclaw/scripts/tagesbriefing.py' --tools 'exec'`

### 3. Memory-Logger um System-Memory erweitert

- Alt: Nur Gateway-RSS
- Neu: Gateway-RSS + System total/used/free/available aus `/proc/meminfo`
- Schreibt auch bei totem Gateway (bisher nur wenn Gateway-PID existierte)

### 4. Journal-Sync beschleunigt

- `SyncIntervalSec=5m` → `SyncIntervalSec=1m`
- Reduziert Datenverlust bei Watchdog-Reboot von ≥5 Min auf ≤1 Min

### 5. Node.js auf v22.23.1 + Pinning (vom 20.07.)

- `apt-mark hold nodejs` — keine automatischen Upgrades mehr
- NodeSource `setup_22.x` statt veralteter Pakete

### 6. openclaw-update.sh mit Pre-Update-Node-Check

- `check_node_compatibility()` — `engines.node` aus npm-Registry vs `node -v`
- Bei Inkompatibilität: Abbruch + Telegram-Alert (keine Installation)
- Post-Update Health-Check (60s) mit automatischem Rollback

## Wilson Architektur (aktueller Stand)

```
Raspberry Pi 5, 8 GB RAM
├─ openclaw-gateway.service    (Node.js v22.23.1, port 18789, MemoryMax 800M)
├─ openclaw-health.service     (Python, port 8095)
├─ doc-processor.service       (Python, MemoryMax 500M)
├─ laerenbaer.service          (Python, MemoryMax 200M)
├─ openclaw-gateway-restart.timer (03:30 + 15:30)
├─ memory-logger.sh            (Cron */30 min)
└─ 22 openclaw cron jobs       (jetzt entzerrt, DB-Sync nachts pausiert)
```

Monitoring von Ryzen:
- `openclaw-monitor.sh` (`*/10` Min): SSH-Check + Gateway/Watcher-Status via Telegram
- `service_check.py` (`7 * * * *`): Hub-API-Healthcheck aller 16 Services
- State-File: `/tmp/.openclaw-monitor-state`

## Präventions-Maßnahmen (Übersicht)

| Ebene | Maßnahme | Status |
|---|---|---|
| **Node.js** | v22.23.1 + apt-mark hold | ✅ |
| **Update** | Pre-Check engines.node + Telegram + Rollback | ✅ |
| **Cron** | Mitternachts-Cluster entzerrt | ✅ |
| **Cron** | DB-Sync nachts pausiert (00-05h) | ✅ |
| **Memory** | Gateway-Memory-Limit (800 MB) | ✅ |
| **Memory** | System-Memory-Logging alle 30 Min | ✅ |
| **Journal** | SyncInterval=1min, SystemMaxUse=200M | ✅ |
| **Briefing** | Deterministisches Python-Script statt LLM-Agent | ✅ |
| **Gateway** | 12h-Restart-Timer (03:30 + 15:30) | ✅ |
| **OS-Watchdog** | BCM2835 Hardware Watchdog (60s) | ⚠️ System-Schutz, aber Auslöser des 8. Ausfalls |
| **Remote-Reboot** | M920q Tiny mit vPro/AMT | ❌ Noch nicht beschafft |

## Neue Dateien

| Datei | Zweck |
|---|---|
| `wilson/tagesbriefing.py` | Deterministisches Morgenbriefing |
| `wilson/entzerre-cron.sh` | Cron-Entzerrung (einmalig ausgeführt) |
| `docs/wilson-crash-2026-07-21.md` | Diese Dokumentation |

## Geänderte Dateien

| Datei | Änderung |
|---|---|
| `wilson/openclaw-update.sh` | Pre-Update-Node-Check, Telegram, Rollback, Watcher entfernt |
| `wilson/memory-logger.sh` | System-Memory aus /proc/meminfo, Log auch ohne Gateway |

## Diagnose-Befehle für nächsten Vorfall

```bash
# System-Memory-Trend (neuer Logger!)
ssh wilson "tail -20 ~/.openclaw/logs/memory.csv | column -t -s,"

# Gateway-Journal
ssh wilson "sudo journalctl _SYSTEMD_USER_UNIT=openclaw-gateway.service --since '...' --no-pager"

# Kernel-Log vom Vorboot (wenn Journal syncen konnte)
ssh wilson "sudo journalctl -k --boot=-1 --no-pager | grep -i 'oom\|killed\|panic\|watchdog'"

# Cron-Job-Status
ssh wilson "~/.npm-global/bin/openclaw cron list 2>&1 | grep -E 'error|00:|Session|DB-Sync'"

# Boot-Liste (wenn Journal mehrere Boots hat)
ssh wilson "sudo journalctl --list-boots"

# Watchdog-Log (wenn vom Kernel geloggt)
ssh wilson "sudo journalctl -k | grep -i watchdog"
```

## Siehe auch

- `docs/wilson-oom-fix-2026-07-11.md` — Gateway Memory-Limit + Restart-Timer
- `docs/wilson-cleanup-2026-07-12.md` — Gmail deaktiviert, Dienste entschlackt
- `docs/backup-review-2026-07-12.md` — Backup-Konzept Review
- `WILSON.md` — Referenz-Konfiguration
