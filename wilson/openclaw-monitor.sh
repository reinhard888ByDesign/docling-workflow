#!/bin/bash
# Überwacht openclaw auf Wilson (192.168.3.124) und alarmiert per Telegram

BOT_TOKEN="8621101278:AAHI9CkevPBpZ2uxZQIFyxjGP2m4VUXislE"
CHAT_ID="8620231031"
STATE_FILE="/tmp/.openclaw-monitor-state"

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=${1}" \
        -d "parse_mode=Markdown" > /dev/null
}

# Prüfe SSH-Erreichbarkeit
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes wilson "true" 2>/dev/null; then
    if [ "$(cat "$STATE_FILE" 2>/dev/null)" != "ssh_down" ]; then
        echo "ssh_down" > "$STATE_FILE"
        send_telegram "⚠️ *Wilson nicht erreichbar*\n\nSSH-Verbindung zu Wilson (192.168.3.124) fehlgeschlagen.\nOpenclaw-Status unbekannt.\n\n_$(date '+%d.%m.%Y %H:%M')_"
    fi
    exit 1
fi

# Prüfe openclaw-Services
STATUS=$(ssh -o ConnectTimeout=10 -o BatchMode=yes wilson \
    "systemctl --user is-active openclaw-gateway.service openclaw-watcher.service 2>/dev/null" 2>/dev/null)

GW=$(echo "$STATUS" | sed -n '1p')
WA=$(echo "$STATUS" | sed -n '2p')

if [ "$GW" = "active" ] && [ "$WA" = "active" ]; then
    # Alles OK — war vorher down? Dann Entwarnung
    if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE")" != "ok" ]; then
        send_telegram "✅ *Openclaw wieder aktiv*\n\nGateway und Watcher laufen wieder auf Wilson.\n\n_$(date '+%d.%m.%Y %H:%M')_"
    fi
    echo "ok" > "$STATE_FILE"
else
    # Services down — nur einmal alarmieren bis Zustand sich ändert
    PREV=$(cat "$STATE_FILE" 2>/dev/null)
    MSG="DOWN:gw=${GW},wa=${WA}"
    if [ "$PREV" != "$MSG" ]; then
        echo "$MSG" > "$STATE_FILE"
        DETAILS=""
        [ "$GW" != "active" ] && DETAILS="${DETAILS}\n• openclaw-gateway: *${GW}*"
        [ "$WA" != "active" ] && DETAILS="${DETAILS}\n• openclaw-watcher: *${WA}*"
        send_telegram "🚨 *Openclaw auf Wilson ausgefallen!*${DETAILS}\n\n_$(date '+%d.%m.%Y %H:%M')_"
    fi
    exit 1
fi
