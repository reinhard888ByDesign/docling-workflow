#!/bin/bash
# Sequential runner: Vault Summarizer → Batch #27
# Läuft non-stop bis beide fertig sind.

set -e

CONTAINER="document-dispatcher"
SUMMARIZER_SCRIPT="/data/dispatcher-temp/vault_summarizer.py"
SUMMARIZER_LOG="/data/dispatcher-temp/vault_summarizer.log"
SUMMARIZER_MODEL="qwen3:4b-instruct"
BATCH_ID=27
API="http://localhost:8765/api"

LOG_FILE="/home/reinhard/docker/RYZEN - docling-workflow/sequential_run.log"

log() {
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] $*" | tee -a "$LOG_FILE"
}

log "============================================="
log "Sequential Run gestartet"
log "Phase 1: Vault Summarizer"
log "============================================="

# Phase 1: Vault Summarizer
log "Starte Vault Summarizer (Modell: $SUMMARIZER_MODEL)..."
docker exec "$CONTAINER" python3 -u "$SUMMARIZER_SCRIPT" --run --model "$SUMMARIZER_MODEL" >> "/home/reinhard/docker/RYZEN - docling-workflow/dispatcher-temp/vault_summarizer.log" 2>&1

SUMMARIZER_EXIT=$?
log "Vault Summarizer beendet mit Exit-Code: $SUMMARIZER_EXIT"

# Phase 2: Batch #27
log "============================================="
log "Phase 2: Batch #27"
log "============================================="

# Kurze Pause für Ollama
sleep 5

log "Resume Batch #27..."
RESULT=$(curl -s -X POST "$API/batch/runs/$BATCH_ID/resume")
log "Batch #27 Resume: $RESULT"

# Optional: auf Abschluss warten
log "Warte auf Abschluss von Batch #27..."
while true; do
    STATUS=$(curl -s "$API/batch/runs/$BATCH_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)[run][status])" 2>/dev/null || echo "error")
    log "Batch #27 Status: $STATUS"
    case "$STATUS" in
        completed|aborted|failed)
            log "Batch #27 fertig: $STATUS"
            break
            ;;
        running|paused)
            sleep 30
            ;;
        *)
            sleep 10
            ;;
    esac
done

# Final stats
FINAL=$(curl -s "$API/batch/runs/$BATCH_ID")
log "Batch #27 Final: $FINAL"

log "============================================="
log "Sequential Run abgeschlossen"
log "============================================="
