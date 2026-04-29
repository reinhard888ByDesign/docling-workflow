#!/bin/bash
# OpenClaw nightly auto-update
# Writes result to ~/.openclaw/openclaw-update.status for morning briefing

STATUS_FILE="$HOME/.openclaw/openclaw-update.status"
LOG_FILE="$HOME/.openclaw/openclaw-update.log"
DATE=$(date '+%Y-%m-%d %H:%M')

export PATH="$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
export NPM_CONFIG_PREFIX="$HOME/.npm-global"

current=$(openclaw --version 2>/dev/null | grep -oP '\d{4}\.\d+\.\d+' | head -1)
latest=$(npm show openclaw version 2>/dev/null | tr -d '[:space:]')

echo "[$DATE] current=$current latest=$latest" >> "$LOG_FILE"

if [ -z "$latest" ]; then
    echo "ERROR: npm-Abfrage fehlgeschlagen" > "$STATUS_FILE"
    exit 1
fi

if [ "$current" = "$latest" ]; then
    echo "OK: openclaw $current ist aktuell (geprüft $DATE)" > "$STATUS_FILE"
    exit 0
fi

# Update durchführen
echo "[$DATE] Update von $current auf $latest..." >> "$LOG_FILE"
npm install -g openclaw@latest >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "ERROR: Update auf $latest fehlgeschlagen (Exit $EXIT_CODE, $DATE)" > "$STATUS_FILE"
    exit 1
fi

# Services neu starten
systemctl --user restart openclaw-gateway.service openclaw-watcher.service >> "$LOG_FILE" 2>&1

new_version=$(openclaw --version 2>/dev/null | grep -oP '\d{4}\.\d+\.\d+' | head -1)
echo "UPDATED: openclaw $current → $new_version ($DATE)" > "$STATUS_FILE"
echo "[$DATE] Update erfolgreich: $current → $new_version" >> "$LOG_FILE"
