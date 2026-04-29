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
scp wilson/doc_processor.py "$WILSON:$SCRIPTS_DIR/doc_processor.py"
scp wilson/heartbeat.py     "$WILSON:$SCRIPTS_DIR/heartbeat.py"

# Services
scp wilson/doc-processor.service "$WILSON:$SERVICE_DIR/doc-processor.service"
scp wilson/heartbeat.service     "$WILSON:$SERVICE_DIR/heartbeat.service"

# Reload + Restart
ssh "$WILSON" "
  systemctl --user daemon-reload
  systemctl --user restart doc-processor
  systemctl --user enable --now heartbeat
  echo '--- doc-processor ---'
  systemctl --user status doc-processor --no-pager -l | head -20
  echo '--- heartbeat ---'
  systemctl --user status heartbeat --no-pager -l | head -20
"

echo "=== Deploy abgeschlossen ==="
