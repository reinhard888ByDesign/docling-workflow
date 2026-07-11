# Wilson OOM Fix — 2026-07-11

> 6. OOM-Vorfall, Root Cause gefunden und behoben

## Root Cause

**`cgroup_disable=memory`** im Kernel-Boot-Parameter — injiziert durch Raspberry Pi Firmware.

Ohne cgroup memory controller sind ALLE systemd `MemoryMax`-Limits wirkungslos.
Der Node.js openclaw-gateway wächst unbeschränkt (Spitzen bis 1.5 GB RSS),
bis der OOM-Killer zuschlägt.

### Crash-Sequenz

```
Boot:     Gateway 430 MB → wächst kontinuierlich
24-48h:   Gateway 700+ MB, LLM-Spike → 1.0-1.5 GB
OOM:      Kernel killt Gateway (oom_score=821, höchste Priorität)
Systemd:  Restart in 5s → neuer Gateway braucht 430 MB
Loop:     Kill/Restart bis System unresponsive → Hard Reboot
```

### Warum es 6x passiert ist

| Fehlende Sicherung | Ursache |
|-------------------|---------|
| Memory-Limits wirkungslos | `cgroup_disable=memory` deaktiviert cgroup memory controller |
| earlyoom feuert nie | `-s 10` verlangt Swap < 10% — Swap ist immer 100% frei |
| Swap ungenutzt | swappiness=60 auf 8GB-System zu konservativ |

## Fixes (3-stufig)

### 1. cgroup memory controller aktivieren (REBOOT)

`/boot/firmware/cmdline.txt`:
```
cgroup_enable=memory
```

Nach Reboot: `cat /sys/fs/cgroup/cgroup.subtree_control` muss `memory` enthalten.

### 2. earlyoom reparieren

`/etc/default/earlyoom`:
```
EARLYOOM_ARGS="-m 10 -r 3600"
```

`-s 10` entfernt → earlyoom feuert bei 10% RAM frei, unabhängig vom Swap.

### 3. Memory-Limits via systemd drop-ins

| Service | MemoryMax | MemorySwapMax |
|---------|-----------|---------------|
| openclaw-gateway | 800M | 0 |
| doc-processor | 500M | 0 |
| laerenbaer | 200M | 0 |
| openclaw-watcher | 150M | 0 |
| heartbeat | 100M | 0 |
| ttyd-openclaw | 50M | 0 |

Drop-ins in `~/.config/systemd/user/<service>.service.d/memory-limit.conf`.

### 4. Zusätzlich

- Gateway-Restart alle 12h (03:30 + 15:30) via systemd timer
- Lärmbär-Monitor warnt bei Gateway > 700 MB RSS
- swappiness auf 80 erhöht (Swap-Entlastung vor OOM)

## Verifikation

```bash
# Nach Reboot:
cat /sys/fs/cgroup/cgroup.subtree_control  # muss "memory" enthalten
systemctl --user show openclaw-gateway --property=MemoryCurrent  # Zahl, nicht [not set]
cat /proc/cmdline | grep cgroup  # cgroup_enable=memory vorhanden
```

## Dateien

- `/boot/firmware/cmdline.txt` — `cgroup_enable=memory`
- `/etc/default/earlyoom` — `-m 10 -r 3600` (ohne `-s 10`)
- `/etc/sysctl.d/99-swap.conf` — `vm.swappiness=80`
- `~/.config/systemd/user/*.service.d/memory-limit.conf` — Limits pro Service
