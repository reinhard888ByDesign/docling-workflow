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
| **Home Assistant** (192.168.86.183) | Eingerichtet (Web UI → System → Backups) |
| Time Machine (Mac) | Nicht verifiziert — läuft vermutlich auf NAS |

## NAS-Belegung — UniFi UNAS 2, Pool 1 (2×1TB RAID1)

| Share | Belegt | Quota/Gesamt | Notiz |
|-------|--------|-------------|-------|
| **Ryzen** | 292 GB | 500 GB Quota | Backups (7 Snapshots) |
| **Mac Mini's Drive** | 383 GB | 991 GB | Time Machine |
| **Reinhard's Drive** | 75 GB | — | Persönliche Daten |
| **Shared_Drive** | 65 KB | — | Leer |
| **Marion's Drive** | 65 KB | 20 GB | Leer |

**Pool-Füllstand**: 754 GB / 991 GB (76%) — **237 GB frei, kein Handlungsbedarf** ✅

Die „22 GB frei"-Warnung kam von temporärem Backup-Plattenplatz während der Rotation (incoming + Snapshots). Einmalig, kein Dauerproblem.

## Redundanzen

| Daten | Kopien | Orte |
|-------|--------|------|
| Vault | 2-3 | Ryzen + NAS + (Wilson unvollständig) |
| Code | 2 | Ryzen + GitHub |
| Docker-Config | 3 | Ryzen + GitHub + NAS |
| Skills-DBs | 3 | Ryzen + GitHub + NAS |
| HA | **0** | Nirgends! |

## Empfehlungen

1. ~~HA-Backup einrichten~~ — Besteht bereits (HA Web UI → System → Backups)
2. **NAS aufräumen** — Über Web-Oberfläche alte Time-Machine-Backups/Medien prüfen
3. ~~Snapshots reduzieren~~ — 7 tägliche sind ok, NAS hat 237 GB frei
4. ~~Syncthing Wilson-Vault~~ — Wilson hat Haupt-Vault bewusst nicht (nur projekte-vault + input-dispatcher)
