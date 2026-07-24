#!/bin/bash
# HA-Konfig-Backup: HA-Pi → Ryzen
# Läuft täglich via Cron auf dem Ryzen um 06:00 (nach HA Auto-Backup um 05:30)
#
# Sichert:
#   - HA Automatic Backups (.tar) von HA-Pi nach Ryzen
#   - Key Config-Dateien (yaml) für Quick-Restore
# Behält Backups für 14 Tage, Config-Diffs für 60 Tage

set -e

HA_HOST="192.168.86.183"
HA_BACKUP_DIR="/home/reinhard/ha/config/backups"
HA_CONFIG_DIR="/home/reinhard/ha/config"
DEST="/home/reinhard/docker/RYZEN - docling-workflow/backups/homeassistant"
CONFIG_DEST="$DEST/config-snapshots"
LOG="$DEST/backup.log"
DATE=$(date '+%Y-%m-%d %H:%M')
TIMESTAMP=$(date '+%Y%m%d_%H%M')

mkdir -p "$DEST" "$CONFIG_DEST"

# ── 1. Rsync HA-Backup-Archive (.tar) ──────────────────────────────
echo "[$DATE] HA-Backup starting..." >> "$LOG"

rsync -az --timeout=60 \
  -e "ssh -o ConnectTimeout=10 -o ServerAliveInterval=15" \
  "$HA_HOST:$HA_BACKUP_DIR/" "$DEST/backups/" 2>&1 | tail -1 >> "$LOG"

RC1=${PIPESTATUS[0]}

# ── 2. Config-Snapshot (nur yaml/json, kein DB-Müll) ───────────────
rsync -az --timeout=60 \
  -e "ssh -o ConnectTimeout=10 -o ServerAliveInterval=15" \
  --include='*.yaml' --include='*.yml' --include='*.json' \
  --include='*.storage' --include='.HA_VERSION' --include='.storage/' \
  --exclude='*.db' --exclude='*.db-*' --exclude='*.log' --exclude='*.log.*' \
  --exclude='home-assistant.log*' --exclude='home-assistant_v2.db*' \
  --exclude='enelgrid_debug/' --exclude='.cache/' --exclude='deps/' \
  --exclude='*.bak*' --exclude='__pycache__/' \
  "$HA_HOST:$HA_CONFIG_DIR/" "$CONFIG_DEST/$TIMESTAMP/" 2>&1 | tail -1 >> "$LOG"

RC2=${PIPESTATUS[0]}

# ── 3. Cleanup ─────────────────────────────────────────────────────
# Backups älter als 14 Tage löschen
find "$DEST/backups/" -name "*.tar" -mtime +14 -delete 2>/dev/null || true
# Config-Snapshots älter als 60 Tage löschen
find "$CONFIG_DEST/" -maxdepth 1 -type d -mtime +60 -exec rm -rf {} \; 2>/dev/null || true

# ── 4. Log ─────────────────────────────────────────────────────────
BACKUP_COUNT=$(find "$DEST/backups/" -name "*.tar" 2>/dev/null | wc -l)
SNAPSHOT_COUNT=$(find "$CONFIG_DEST/" -maxdepth 1 -type d 2>/dev/null | wc -l)

if [ $RC1 -eq 0 ] && [ $RC2 -eq 0 ]; then
    echo "[$DATE] OK ($BACKUP_COUNT backups, $SNAPSHOT_COUNT configs)" >> "$LOG"
elif [ $RC1 -ne 0 ]; then
    echo "[$DATE] FEHLER: backup-rsync=$RC1 (HA-Pi erreichbar? Backup-Integration aktiv?)" >> "$LOG"
else
    echo "[$DATE] TEILWEISE: backup-rsync=$RC1 config-rsync=$RC2" >> "$LOG"
fi

# Log auf 200 Zeilen begrenzen
tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
