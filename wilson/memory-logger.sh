#!/bin/bash
# Loggt Gateway- und System-Memory alle 30 Min fuer OOM-Forensik
# Changelog 2026-07-21: System-Memory aus /proc/meminfo, auch ohne Gateway
LOG=$HOME/.openclaw/logs/memory.csv
HEADER='timestamp,pid,rss_kb,vsz_kb,pcpu,uptime_min,total_mem_kb,used_mem_kb,free_mem_kb,avail_mem_kb'

[ -f "$LOG" ] || echo "$HEADER" > "$LOG"

# Gateway-Prozess
PID=$(pgrep -f 'openclaw.*gateway' | head -1)

# System-Memory aus /proc/meminfo (funktioniert auch unter Memory-Pressure)
read total used free avail <<< $(awk '
/MemTotal:/ {total=$2}
/MemFree:/  {free=$2}
/MemAvailable:/ {avail=$2}
END {
    used=total-free
    print total, used, free, avail
}' /proc/meminfo)

if [ -n "$PID" ]; then
    read pid rss vsz pcpu <<< $(ps -p $PID -o pid,rss,vsz,pcpu --no-headers 2>/dev/null)
    read uptime_sec <<< $(awk '{print int($1)}' /proc/uptime)
    uptime_min=$((uptime_sec / 60))
    echo "$(date -Iseconds),${pid},${rss},${vsz},${pcpu},${uptime_min},${total},${used},${free},${avail}" >> "$LOG"
else
    # Gateway tot — trotzdem System-Memory loggen!
    read uptime_sec <<< $(awk '{print int($1)}' /proc/uptime)
    uptime_min=$((uptime_sec / 60))
    echo "$(date -Iseconds),0,0,0,0,${uptime_min},${total},${used},${free},${avail}" >> "$LOG"
fi
