#!/bin/bash
# Deploy Wilson scripts + services nach Ryzen→Wilson Push
# Ausführen auf Ryzen: bash wilson/deploy-wilson.sh
# Voraussetzung: SSH zu Wilson (reinhard@192.168.3.124) funktioniert

set -euo pipefail

WILSON="reinhard@192.168.3.124"
SCRIPTS_DIR="/home/reinhard/.openclaw/scripts"
SERVICE_DIR="/home/reinhard/.config/systemd/user"

echo "=== Deploy nach Wilson ==="

# Scripts
ssh "$WILSON" "mkdir -p $SCRIPTS_DIR"
scp wilson/doc_processor.py  "$WILSON:$SCRIPTS_DIR/doc_processor.py"
scp wilson/heartbeat.py      "$WILSON:$SCRIPTS_DIR/heartbeat.py"
scp wilson/ai_assistant.py   "$WILSON:$SCRIPTS_DIR/ai_assistant.py"

# Services
scp wilson/doc-processor.service  "$WILSON:$SERVICE_DIR/doc-processor.service"
scp wilson/heartbeat.service      "$WILSON:$SERVICE_DIR/heartbeat.service"
scp wilson/ai-assistant.service   "$WILSON:$SERVICE_DIR/ai-assistant.service"

# Reload + Restart
ssh "$WILSON" "
  systemctl --user daemon-reload
  systemctl --user restart doc-processor
  systemctl --user enable --now heartbeat
  systemctl --user enable --now ai-assistant
  echo '--- doc-processor ---'
  systemctl --user status doc-processor --no-pager -l | head -10
  echo '--- heartbeat ---'
  systemctl --user status heartbeat --no-pager -l | head -10
  echo '--- ai-assistant ---'
  systemctl --user status ai-assistant --no-pager -l | head -10
"

echo "=== Deploy abgeschlossen ==="
