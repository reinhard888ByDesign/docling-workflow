# Backup-Konzept Review — 2026-07-12

> NAS fast voll (22 GB frei), Snapshots überdimensioniert, HA ohne Backup

## Aktueller Stand

### Was läuft

| System | Quelle | Ziel | Rhythmus | Status |
|--------|--------|------|----------|--------|
| Ryzen-Backup | Vault, PDF, Projekte, Config, SSH, Skills | NAS (CIFS) | tägl. 03:00 | ✅ (44 GB frei reicht) |
| Wilson-Backup | OpenClaw, systemd, Skills | Ryzen → NAS | tägl. 04:00 | ✅ |
| GitHub | 7 Code-Repos | github.com | per Push | ✅ alle aktuell |
| Syncthing | Vault-Sync Ryzen↔Wilson↔Mac | live | kontinuierlich | ⚠️ Wilson nur 51 MB |

### Was fehlt

| System | Risiko |
|--------|--------|
| **Home Assistant** (192.168.86.183) | Kein Backup! Automationen, Dashboards, Sensor-History ungesichert |
| Time Machine (Mac) | Nicht verifiziert — läuft vermutlich auf NAS |

## NAS-Belegung (924 GB)

| Bereich | Größe | Notiz |
|--------|-------|-------|
| `backups/ryzen/` (CIFS) | 272 GB | 7 Snapshots, Hardlinks werden auf CIFS mehrfach gezählt |
| Andere Shares | ~630 GB | Vermutlich Time Machine + Medien |
| Frei | ~22 GB | Chronisch knapp |

## Redundanzen

| Daten | Kopien | Orte |
|-------|--------|------|
| Vault | 2-3 | Ryzen + NAS + (Wilson unvollständig) |
| Code | 2 | Ryzen + GitHub |
| Docker-Config | 3 | Ryzen + GitHub + NAS |
| Skills-DBs | 3 | Ryzen + GitHub + NAS |
| HA | **0** | Nirgends! |

## Empfehlungen

1. **HA-Backup einrichten** — HA Web UI → Einstellungen → System → Backups, Ziel: NAS
2. **NAS aufräumen** — Über Web-Oberfläche alte Time-Machine-Backups/Medien prüfen
3. **Snapshots von 7 auf 3 reduzieren** — daily.0-2 reichen
4. **Syncthing auf Wilson prüfen** — warum nur 51 MB von 16 GB?
