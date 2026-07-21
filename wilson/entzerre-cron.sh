#!/bin/bash
# Entzerrt die Mitternachts-Cron-Jobs auf Wilson
# Verhindert Watchdog-Reboots durch gleichzeitige isolierte Sessions

OC="$HOME/.npm-global/bin/openclaw"

echo "=== 1. Session-Reset: 23:59 → 00:15 ==="
$OC cron edit 16cfb032-63d5-472b-8f11-9a6a71b3bdf5 \
  --cron '15 0 * * *' --tz 'Europe/Rome' 2>&1 && echo "OK" || echo "FEHLER"

echo "=== 2. Feng Shui Tagesbriefing: 23:59 → 00:05 ==="
$OC cron edit 91f82b86-d5d6-4440-a7a6-65be142364ec \
  --cron '5 0 * * *' --tz 'Europe/Rome' 2>&1 && echo "OK" || echo "FEHLER"

echo "=== 3. Feng Shui Briefing 7:45 → 7:50 ==="
$OC cron edit eb7c4d08-57aa-422f-9168-ae0042e7de37 \
  --cron '50 7 * * *' --tz 'Europe/Rome' 2>&1 && echo "OK" || echo "FEHLER"

echo "=== 4. Dispatcher-DB-Sync: nur 05-23h ==="
$OC cron edit e9cb4a9b-8e29-4814-bb84-4a72fbf1c467 \
  --cron '*/5 5-23 * * *' --tz 'Europe/Rome' 2>&1 && echo "OK" || echo "FEHLER"

echo ""
echo "=== Verifikation ==="
$OC cron list 2>&1 | grep -E 'Session-Reset|Feng Shui|DB-Sync'
