#!/bin/bash
# Sequential Runner v2: Summarizer → Batch #27
# Startet Summarizer, wartet auf Abschluss, resumed dann Batch #27
#
# Usage: ./run_sequential_v2.sh
# Log: dispatcher-temp/sequential_v2.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/dispatcher-temp/sequential_v2.log"
SUMM_LOG="$SCRIPT_DIR/dispatcher-temp/vault_summarizer.log"
PROGRESS="$SCRIPT_DIR/dispatcher-temp/vault_summarizer_progress.json"
CONTAINER="document-dispatcher"
MODEL="qwen3:4b-instruct"
BATCH_URL="http://localhost:8765/api/batch/runs/27/resume"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# ── Phase 1: Summarizer ──────────────────────────────────────────────────────

# Prüfe ob Summarizer bereits läuft
SUMM_RUNNING=$(docker exec "$CONTAINER" python3 -c "
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

if [ "$SUMM_RUNNING" = "yes" ]; then
    log "Phase 1: Summarizer läuft bereits — warte auf Abschluss"
else
    log "Phase 1: Starte Vault Summarizer (Modell: $MODEL)"
    docker exec -d "$CONTAINER" bash -c \
        "python3 -u /data/dispatcher-temp/vault_summarizer.py --run --model $MODEL >> /data/dispatcher-temp/vault_summarizer.log 2>&1"
    log "Summarizer gestartet"
fi

# Warte auf Fertigstellung
STUCK_COUNT=0
CURRENT_RESUME=$(grep -n "^Resume:" "$SUMM_LOG" 2>/dev/null | tail -1 | cut -d: -f1)
LAST_SIZE=$(stat -c %s "$SUMM_LOG" 2>/dev/null || echo 0)

while true; do
    sleep 30

    # Check ob Summarizer noch läuft
    SUMM_RUNNING=$(docker exec "$CONTAINER" python3 -c "
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

    if [ "$SUMM_RUNNING" != "yes" ]; then
        # Summarizer ist beendet — prüfe ob Fertig-Meldung da ist
        if [ -n "$CURRENT_RESUME" ]; then
            if tail -n +"$CURRENT_RESUME" "$SUMM_LOG" | grep -q "^Fertig\."; then
                log "Phase 1: Summarizer abgeschlossen (Fertig-Meldung gefunden)"
                break
            fi
        fi
        # Summarizer beendet ohne Fertig-Meldung — evtl. gecrashed
        log "Phase 1: Summarizer-Prozess beendet, aber keine Fertig-Meldung — prüfe Fortschritt"
        # Check progress.json
        PENDING=$(python3 -c "
import json
d = json.load(open('$PROGRESS'))
pending = sum(1 for v in d.values() if isinstance(v, dict) and v.get('status') not in ('done','skipped_short','skipped_lang_uncertain','error','skipped_already_summarized','skipped_blacklist'))
print(pending)
" 2>&1)
        if [ "${PENDING:-999}" = "0" ]; then
            log "Phase 1: Alle Dateien haben Status — Summarizer de facto fertig"
            break
        fi
        log "Phase 1: $PENDING Dateien noch pending — starte Summarizer neu"
        docker exec -d "$CONTAINER" bash -c \
            "python3 -u /data/dispatcher-temp/vault_summarizer.py --run --model $MODEL >> /data/dispatcher-temp/vault_summarizer.log 2>&1"
        CURRENT_RESUME=$(grep -n "^Resume:" "$SUMM_LOG" 2>/dev/null | tail -1 | cut -d: -f1)
        continue
    fi

    # Summarizer läuft — prüfe ob er festhängt (Log wächst nicht)
    NEW_SIZE=$(stat -c %s "$SUMM_LOG" 2>/dev/null || echo 0)
    if [ "$NEW_SIZE" = "$LAST_SIZE" ]; then
        STUCK_COUNT=$((STUCK_COUNT + 1))
        if [ $STUCK_COUNT -ge 10 ]; then  # 5 Minuten kein Fortschritt
            log "WARNUNG: Summarizer seit 5min ohne Fortschritt — breche ab"
            docker exec "$CONTAINER" python3 -c "
import os, signal
for pid in os.listdir('/proc'):
    if not pid.isdigit(): continue
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read().decode('utf-8', errors='replace')
        if '/vault_summarizer.py' in cmd:
            os.kill(int(pid), signal.SIGTERM)
    except: pass
" 2>&1
            break
        fi
    else
        STUCK_COUNT=0
        LAST_SIZE="$NEW_SIZE"
    fi

    # Fortschritts-Log
    LAST_LINE=$(tail -1 "$SUMM_LOG" 2>/dev/null | cut -c1-120)
    log "Summarizer: $LAST_LINE"
done

# ── Phase 2: Batch #27 ────────────────────────────────────────────────────────

log "Phase 2: Starte Batch #27 Resume"
sleep 5

BATCH_STATUS=$(curl -s http://localhost:8765/api/batch/runs/27 | python3 -c "
import sys,json
r = json.load(sys.stdin)['run']
print(r['status'], r['processed'], r['total'])
" 2>&1)
log "Batch #27 vor Resume: $BATCH_STATUS"

RESP=$(curl -s -X POST "$BATCH_URL")
log "Batch #27 Resume: $RESP"

# Warte auf Batch-Abschluss
while true; do
    sleep 60
    STATUS=$(curl -s http://localhost:8765/api/batch/runs/27 | python3 -c "
import sys,json
r = json.load(sys.stdin)['run']
print(r['status'], r['processed'], r['total'], r['errors'])
" 2>&1)
    log "Batch #27: $STATUS"

    if echo "$STATUS" | grep -qE "completed|aborted|failed"; then
        log "Batch #27 abgeschlossen mit Status: $STATUS"
        break
    fi
done

log "===== Sequential Run v2 beendet ====="
