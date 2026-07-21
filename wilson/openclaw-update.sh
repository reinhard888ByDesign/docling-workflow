#!/bin/bash
# OpenClaw nightly auto-update — mit Pre-Update-Node-Check, Telegram-Alert & Rollback
# Changelog:
#   2026-07-20: Pre-Update Node-Check, Post-Update Health-Check (60s), Rollback, Telegram
#   2026-05-26: wait_for_service(), start_service() mit Retry, daemon-reload fix
#
# Verwendet von: Crontab auf Wilson (täglich 02:00)
# Status-File: ~/.openclaw/openclaw-update.status (wird vom Morgenbriefing gelesen)

STATUS_FILE="$HOME/.openclaw/openclaw-update.status"
LOG_FILE="$HOME/.openclaw/openclaw-update.log"
BACKUP_DIR="$HOME/.openclaw/backups"
DATE=$(date '+%Y-%m-%d %H:%M')

export PATH="$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
export NPM_CONFIG_PREFIX="$HOME/.npm-global"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# ── Telegram (selber Bot wie openclaw-monitor.sh) ──────────────────────────

TG_BOT="8621101278:AAHI9CkevPBpZ2uxZQIFyxjGP2m4VUXislE"
TG_CHAT="8620231031"

send_telegram() {
    local msg="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" \
        -d "chat_id=${TG_CHAT}" \
        -d "text=${msg}" \
        -d "parse_mode=Markdown" \
        --connect-timeout 10 --max-time 15 > /dev/null 2>&1 || true
}

# ── wait_for_service: poll until active or timeout ─────────────────────────

wait_for_service() {
    local service="$1"
    local max_wait="${2:-30}"
    local waited=0
    local interval=2

    while [ $waited -lt $max_wait ]; do
        sleep $interval
        waited=$((waited + interval))
        if systemctl --user is-active --quiet "$service" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

# ── start_service: start with retry and verification ───────────────────────

start_service() {
    local service="$1"
    local attempts=3

    for i in $(seq 1 $attempts); do
        systemctl --user start "$service" 2>&1
        if wait_for_service "$service" 15; then
            echo "[$DATE] $service: aktiv" >> "$LOG_FILE"
            return 0
        fi
        echo "[$DATE] $service: Versuch $i/$attempts fehlgeschlagen" >> "$LOG_FILE"
        journalctl --user -u "$service" -n 5 --no-pager >> "$LOG_FILE" 2>&1
        sleep 2
    done
    return 1
}

# ── check_node_compatibility: engines.node aus Registry vs installiertes Node ──

check_node_compatibility() {
    local target_version="$1"
    local engines_node
    local required_min
    local current_node

    # engines.node aus npm-Registry holen
    engines_node=$(npm show "openclaw@${target_version}" engines.node 2>/dev/null | tr -d '[:space:]')
    if [ -z "$engines_node" ]; then
        echo "[$DATE] WARN: Konnte engines.node für v${target_version} nicht ermitteln — überspringe Check" >> "$LOG_FILE"
        return 0  # kein Check möglich → durchlassen
    fi

    # Erste >= Anforderung extrahieren (z.B. ">=22.22.3" aus ">=22.22.3 <23 || ...")
    required_min=$(echo "$engines_node" | grep -oP '>=\d+\.\d+\.\d+' | head -1 | tr -d '>=')
    if [ -z "$required_min" ]; then
        echo "[$DATE] WARN: Konnte Mindestversion nicht aus '$engines_node' parsen" >> "$LOG_FILE"
        return 0
    fi

    current_node=$(node -v 2>/dev/null | tr -d 'v')
    if [ -z "$current_node" ]; then
        echo "[$DATE] ERROR: node -v liefert nichts" >> "$LOG_FILE"
        return 1
    fi

    echo "[$DATE] Node-Check: benötigt >=${required_min}, installiert ${current_node}" >> "$LOG_FILE"

    # Versionsvergleich mit sort -V
    if [ "$(printf '%s\n' "$required_min" "$current_node" | sort -V | head -1)" != "$required_min" ]; then
        # current_node < required_min → INKOMPATIBEL
        echo "[$DATE] BLOCKIERT: Node ${current_node} zu alt für openclaw v${target_version} (braucht >=${required_min})" >> "$LOG_FILE"
        return 1
    fi

    echo "[$DATE] Node-Check OK: ${current_node} >= ${required_min}" >> "$LOG_FILE"
    return 0
}

# ── rollback: zurück auf vorherige Version ──────────────────────────────────

rollback() {
    local prev_version="$1"
    echo "[$DATE] ROLLBACK: Installiere vorherige Version v${prev_version}..." >> "$LOG_FILE"
    send_telegram "🔄 *Wilson Rollback*\n\nOpenClaw v${prev_version} wird wiederhergestellt..."

    npm install -g "openclaw@${prev_version}" >> "$LOG_FILE" 2>&1
    if [ $? -ne 0 ]; then
        echo "[$DATE] ROLLBACK FEHLGESCHLAGEN: npm install openclaw@${prev_version}" >> "$LOG_FILE"
        send_telegram "🚨 *Wilson Rollback FEHLGESCHLAGEN*\n\nManueller Eingriff nötig! npm install -g openclaw@${prev_version}"
        return 1
    fi

    # Service-Unit zurückschreiben
    local SERVICE_UNIT="$HOME/.config/systemd/user/openclaw-gateway.service"
    if [ -f "$SERVICE_UNIT" ] && [ -n "$prev_version" ]; then
        sed -i "s/Description=OpenClaw Gateway (v[0-9.]*)/Description=OpenClaw Gateway (v$prev_version)/" "$SERVICE_UNIT"
        sed -i "s/OPENCLAW_SERVICE_VERSION=[0-9.]*/OPENCLAW_SERVICE_VERSION=$prev_version/" "$SERVICE_UNIT"
        systemctl --user daemon-reload >> "$LOG_FILE" 2>&1
    fi

    echo "[$DATE] Rollback auf v${prev_version} abgeschlossen, starte Gateway..." >> "$LOG_FILE"
    if start_service openclaw-gateway.service; then
        echo "[$DATE] Gateway mit v${prev_version} läuft wieder" >> "$LOG_FILE"
        send_telegram "✅ *Wilson Rollback erfolgreich*\n\nOpenClaw v${prev_version} läuft wieder."
        echo "ROLLBACK: v${prev_version} wiederhergestellt ($DATE)" > "$STATUS_FILE"
        return 0
    else
        echo "[$DATE] Gateway-Start nach Rollback FEHLGESCHLAGEN" >> "$LOG_FILE"
        send_telegram "🚨 *Wilson Gateway startet auch nach Rollback NICHT!*\n\nManueller Eingriff nötig."
        echo "CRITICAL: Rollback auf v${prev_version} OK, aber Gateway startet nicht ($DATE)" > "$STATUS_FILE"
        return 1
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# ── Main ─────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

mkdir -p "$BACKUP_DIR"

current=$(openclaw --version 2>/dev/null | grep -oP '\d{4}\.\d+\.\d+' | head -1)
latest=$(npm show openclaw version 2>/dev/null | tr -d '[:space:]')

echo "[$DATE] current=$current latest=$latest" >> "$LOG_FILE"

if [ -z "$latest" ]; then
    echo "ERROR: npm-Abfrage fehlgeschlagen" > "$STATUS_FILE"
    echo "[$DATE] ERROR: npm show openclaw version fehlgeschlagen" >> "$LOG_FILE"
    exit 1
fi

# ── Kein Update nötig ───────────────────────────────────────────────────────

if [ "$current" = "$latest" ]; then
    echo "OK: openclaw $current ist aktuell (geprueft $DATE)" > "$STATUS_FILE"
    find "$HOME/.openclaw/logs/stability/" -name '*.json' -mtime +14 -delete 2>/dev/null

    # Defense in depth: prüfen ob Gateway noch läuft
    if ! systemctl --user is-active --quiet openclaw-gateway.service 2>/dev/null; then
        echo "[$DATE] Gateway nicht aktiv – starte neu" >> "$LOG_FILE"
        start_service openclaw-gateway.service
    fi
    exit 0
fi

# ── Pre-Update Node-Check ───────────────────────────────────────────────────

if ! check_node_compatibility "$latest"; then
    echo "BLOCKED: openclaw v${latest} braucht neueres Node.js — Update ausgesetzt ($DATE)" > "$STATUS_FILE"
    send_telegram "⛔ *OpenClaw Update blockiert*\n\nVersion \`${latest}\` braucht neueres Node.js als installiert.\nUpdate wurde *nicht* installiert.\n\nNode: \`$(node -v)\`\nBenötigt: \`$(npm show openclaw@${latest} engines.node 2>/dev/null | tr -d '[:space:]')\`"
    exit 1
fi

# ── Backup der aktuellen Version ────────────────────────────────────────────

echo "[$DATE] Backup: sichere v${current} vor Update auf v${latest}" >> "$LOG_FILE"
npm list -g openclaw --depth=0 > "${BACKUP_DIR}/openclaw-preupdate-${current}.txt" 2>&1

# ── Update ──────────────────────────────────────────────────────────────────

echo "[$DATE] Stoppe Gateway vor Update..." >> "$LOG_FILE"
systemctl --user stop openclaw-gateway.service >> "$LOG_FILE" 2>&1
sleep 3

echo "[$DATE] Update von $current auf $latest..." >> "$LOG_FILE"
npm install -g openclaw@latest >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "ERROR: Update auf $latest fehlgeschlagen (Exit $EXIT_CODE, $DATE)" > "$STATUS_FILE"
    echo "[$DATE] ERROR: npm-Update gescheitert, starte mit alter Version" >> "$LOG_FILE"
    send_telegram "⚠️ *OpenClaw Update fehlgeschlagen*\n\nnpm install -g openclaw@${latest} → Exit ${EXIT_CODE}\nGateway läuft mit v${current} weiter."
    start_service openclaw-gateway.service
    exit 1
fi

# ── Service-Unit aktualisieren ─────────────────────────────────────────────

new_version=$(openclaw --version 2>/dev/null | grep -oP '\d{4}\.\d+\.\d+' | head -1)
SERVICE_UNIT="$HOME/.config/systemd/user/openclaw-gateway.service"

if [ -f "$SERVICE_UNIT" ] && [ -n "$new_version" ]; then
    sed -i "s/Description=OpenClaw Gateway (v[0-9.]*)/Description=OpenClaw Gateway (v$new_version)/" "$SERVICE_UNIT"
    sed -i "s/OPENCLAW_SERVICE_VERSION=[0-9.]*/OPENCLAW_SERVICE_VERSION=$new_version/" "$SERVICE_UNIT"
    systemctl --user daemon-reload >> "$LOG_FILE" 2>&1
    echo "[$DATE] Service-Unit auf v$new_version aktualisiert + daemon-reload" >> "$LOG_FILE"
fi

# ── Gateway starten mit 60s Health-Check ────────────────────────────────────

echo "[$DATE] Starte Gateway v${new_version}..." >> "$LOG_FILE"

if start_service openclaw-gateway.service; then
    echo "[$DATE] Gateway v${new_version} erfolgreich gestartet" >> "$LOG_FILE"

    # ── Post-Update Health-Check (60s) ──────────────────────────────────
    echo "[$DATE] Post-Update Health-Check (max 60s)..." >> "$LOG_FILE"
    if wait_for_service openclaw-gateway.service 60; then
        echo "[$DATE] Health-Check BESTANDEN: Gateway 60s stabil" >> "$LOG_FILE"
    else
        echo "[$DATE] Health-Check FEHLGESCHLAGEN: Gateway innerhalb 60s nicht stabil" >> "$LOG_FILE"
        rollback "$current"
        exit 1
    fi
else
    echo "[$DATE] Gateway-Start nach Update FEHLGESCHLAGEN" >> "$LOG_FILE"
    rollback "$current"
    exit 1
fi

# ── Aufräumen ───────────────────────────────────────────────────────────────

find "$HOME/.openclaw/logs/stability/" -name '*.json' -mtime +14 -delete 2>/dev/null

echo "UPDATED: openclaw $current → $new_version ($DATE)" > "$STATUS_FILE"
echo "[$DATE] Update erfolgreich: $current → $new_version, Gateway aktiv" >> "$LOG_FILE"
send_telegram "✅ *OpenClaw Update*\n\n\`${current}\` → \`${new_version}\`\nGateway läuft, Node $(node -v)"
