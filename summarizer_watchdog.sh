#!/bin/bash
# Vault Summarizer Watchdog — verarbeitet neue .md-Dateien seit letztem Lauf
#
# Aufruf: ./summarizer_watchdog.sh [--once]
#   --once  Einmaliger Lauf (für Cron/Timer)
#   ohne    Dauerschleife mit 5min Intervall
#
# Log: dispatcher-temp/summarizer_watchdog.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STAMP_FILE="$SCRIPT_DIR/dispatcher-temp/.summarizer_last_run"
LOG_FILE="$SCRIPT_DIR/dispatcher-temp/summarizer_watchdog.log"
VAULT_DIR="$SCRIPT_DIR/syncthing/data/reinhards-vault"
CONTAINER="document-dispatcher"
MODEL="qwen3:4b-instruct"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

run_summarizer_catchup() {
    log "Watchdog: Starte Catchup-Run..."

    # Prüfe ob Summarizer bereits läuft (via /proc, sucht nach /vault_summarizer.py)
    RUNNING=$(docker exec "$CONTAINER" python3 -c "
import os
for pid in os.listdir('/proc'):
    if not pid.isdigit(): continue
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read().decode('utf-8', errors='replace')
        if '/vault_summarizer.py' in cmd:
            print('yes')
            break
    except: pass
" 2>&1)
    if [ "$RUNNING" = "yes" ]; then
        log "Watchdog: Summarizer läuft bereits — überspringe"
        return 0
    fi

    # Finde neue .md-Dateien seit letztem Lauf (via find -newer)
    NEW_COUNT=0
    if [ -f "$STAMP_FILE" ]; then
        NEW_COUNT=$(find "$VAULT_DIR" -name "*.md" -newer "$STAMP_FILE" 2>/dev/null | wc -l)
        log "Watchdog: $NEW_COUNT neue .md-Dateien seit letztem Lauf"
    else
        log "Watchdog: Erstlauf — kein Stamp-File, starte trotzdem (scannt nur Pending)"
    fi

    # Starte Summarizer im Container (--run skipped bereits verarbeitete)
    docker exec -d "$CONTAINER" bash -c \
        "python3 -u /data/dispatcher-temp/vault_summarizer.py --run --model $MODEL >> /data/dispatcher-temp/vault_summarizer.log 2>&1"

    log "Watchdog: Summarizer --run gestartet (Container: $CONTAINER)"

    # Update timestamp
    touch "$STAMP_FILE"
}

# ── Main ────────────────────────────────────────────────────────────────────────

mkdir -p "$(dirname "$STAMP_FILE")"

if [ "${1:-}" = "--once" ]; then
    run_summarizer_catchup
    exit 0
fi

log "===== Watchdog gestartet (Intervall: 300s) ====="
while true; do
    run_summarizer_catchup
    sleep 300
done
