# Wilson Fixes & Aufgaben-Konsolidierung — 2026-06-29

## Gateway-Degradation: Root Cause & Fixes

### Root Cause
Der Gateway-Ausfall heute Morgen (08:56) war **kein OOM**, sondern ein Deadlock zwischen OpenClaw und systemd:

1. OpenClaw löste nach ~22h Laufzeit einen Selbst-Restart aus
2. Systemd sandte SIGTERM, OpenClaw versuchte laufende Tasks zu drainen (Telegram Polling Watchdog)
3. OpenClaws Drain-Timeout: 300s, aber systemds `TimeoutStopSec` war nur 30s
4. Systemd killte nach 30s die gesamte cgroup mit SIGKILL
5. Der Service blieb im Zustand `failed (timeout)`, `Restart=always` griff nicht

Die früheren OOM-Vorfälle (5 seit Mai) wurden analysiert — der Gateway-Memory-Leak ist langsam (~125 MB in 22h) und erreicht nie das 900-MB-Limit. Der Kernel-OOM wurde nie getriggert.

### Fixes (3-Schicht-Schutz)

| Schicht | Mechanismus | Verhindert |
|---------|------------|-----------|
| `earlyoom` | Userspace-OOM-Killer bei 10% RAM frei | Total-Freeze (SSH tot, kein fork) |
| Timer täglich 03:30 | systemd-Timer restartet Gateway | Memory-Leak akkumuliert über Tage |
| `TimeoutStopSec=300` | Deckt OpenClaws Drain-Timeout | Deadlock beim Selbst-Restart |

```bash
# Installiert auf Wilson:
sudo apt install earlyoom
# Config: /etc/default/earlyoom -> EARLYOOM_ARGS="-m 10 -s 10 -r 3600"

# Timer: ~/.config/systemd/user/openclaw-gateway-restart.{service,timer}
# OnCalendar=*-*-* 03:30:00, RandomizedDelaySec=300

# Drop-in: ~/.config/systemd/user/openclaw-gateway.service.d/restart-delay.conf
# TimeoutStopSec=300
```

---

## Aufgaben-Konsolidierung

### Problem
Zwei parallele Systeme ohne Synchronisation:
- **Aufgaben.md** (Markdown, Wilson) — von 4 OpenClaw-Cron-Jobs genutzt
- **Aufgaben App** (:8096, Ryzen) — von doc_processor.py (Telegram) genutzt

### Migration
- 90 Aufgaben aus Aufgaben.md geparst
- Nach Abgleich mit aufgaben.db: 24 neue Tasks importiert
- 20 Artefakt-Tasks (nur "(erledigt ...)" als Name) als erledigt markiert
- **Endergebnis:** 90 Tasks in aufgaben.db (67 offen, 23 erledigt)

### Cron-Jobs auf API umgestellt

| Job | ID | Vorher | Nachher |
|-----|----|--------|---------|
| Abend-Check 22:00 | `8bdd4d29` | Liest/schreibt Aufgaben.md | `GET/POST /api/aufgaben`, `POST /done` |
| Wochenvorschau So 17:00 | `weekly-tasks-...` | Liest Aufgaben.md (war broken) | `GET /api/aufgaben` |
| Fälligkeits-Review So 11:00 | `63c8eb31` | Liest/editiert Aufgaben.md | `GET/PUT /api/aufgaben` |
| Tages-Briefing 07:30 | `1d7e69fb` | Nutzt bereits API | Keine Änderung ✅ |

Datei: `~/.openclaw/cron/jobs.json.migrated` auf Wilson
Backup: `jobs.json.migrated.bak-20260629_aufgaben_api`

### Aufräumen
- `~/Vaults/Aufgaben.md` und `~/Vaults/AUFGABEN.md` auf Wilson gelöscht
- Wilsons MEMORY.md aktualisiert (Abschnitt Aufgabenverwaltung)
- 3 neue Memory-Dateien im Claude-Memory-Verzeichnis

### API-Endpunkte der Aufgaben-App
Basis-URL: `http://192.168.86.195:8096`

| Methode | Pfad | Zweck |
|---------|------|-------|
| GET | /api/aufgaben | Alle Tasks (filter: status, faellig) |
| GET | /api/aufgaben/{id} | Einzelner Task |
| POST | /api/aufgaben | Task anlegen |
| PUT | /api/aufgaben/{id} | Task aktualisieren |
| POST | /api/aufgaben/{id}/done | Als erledigt markieren |
| DELETE | /api/aufgaben/{id} | Task löschen |
| POST | /api/aufgaben/{id}/reminded | Erinnerungs-Datum setzen |

---

## Wilsons zwei Vaults

| | Projekte Vault | Reinhards Vault |
|---|---|---|
| **Ort** | `~/Vaults/` auf Wilson | `~/vault/` auf Ryzen |
| **Funktion** | Wilsons Notizblock | Persönliches Archiv |
| **Inhalt** | openclaw-Persönlichkeit, Aufgaben, Notizen | ~8.900 Dokumente in 16 Kategorien |
| **Zugriff** | Lokal durch openclaw | Via enzyme API (:11180) + vault-grep (:8765) |
| **Sync** | Syncthing Wilson→Mac→Ryzen | Syncthing Mac→Ryzen |
| **Dokumentenfluss** | Quelle (Scanner/Email → staging) | Ziel (Dispatcher → Kategorie/Jahr/) |
