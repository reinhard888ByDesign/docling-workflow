#!/usr/bin/env python3
"""openclaw Health Server — Port 8095 — Wilson Raspberry Pi 5"""

import json
import subprocess
import os
import threading
import time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

PORT = 8095
HOME = Path.home()
SESSIONS_DIR = HOME / ".openclaw/agents/main/sessions"
HEARTBEAT_FILE = HOME / ".openclaw/heartbeat_state.json"
OPENCLAW_JSON = HOME / ".openclaw/openclaw.json"

SERVICES = [
    ("openclaw-gateway",  "Gateway",        "Node.js LLM-Agent"),
    ("doc-processor",     "Dok-Prozessor",  "Scanner + Telegram Bot"),
    ("heartbeat",         "Heartbeat",      "Ryzen Service Monitor"),
    ("laerenbaer",        "Lärmbär",        "Home Assistant Bot"),
]

ENV = {**os.environ,
       "XDG_RUNTIME_DIR": "/run/user/1000",
       "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}

_cache: dict = {}
_lock = threading.Lock()


def svc_status(unit: str) -> dict:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp"],
            capture_output=True, text=True, env=ENV, timeout=5
        )
        props = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
        return {
            "state": props.get("ActiveState", "unknown"),
            "sub":   props.get("SubState", ""),
            "pid":   props.get("MainPID", "0"),
            "since": props.get("ActiveEnterTimestamp", ""),
        }
    except Exception as e:
        return {"state": "error", "sub": str(e)[:60], "pid": "0", "since": ""}


def gateway_memory() -> dict:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", "openclaw-gateway",
             "--property=MainPID,MemoryMax"],
            capture_output=True, text=True, env=ENV, timeout=5
        )
        props = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
        pid        = props.get("MainPID", "0")
        limit_mb   = int(props.get("MemoryMax", "0") or 0) // (1024 * 1024)
        rss_kb     = 0
        if pid and pid != "0":
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
            except Exception:
                pass
        rss_mb = rss_kb // 1024
        pct    = int(rss_mb / limit_mb * 100) if limit_mb else 0
        return {"rss_mb": rss_mb, "limit_mb": limit_mb, "pct": pct}
    except Exception:
        return {"rss_mb": 0, "limit_mb": 900, "pct": 0}


def sessions_count() -> int:
    try:
        return sum(1 for f in SESSIONS_DIR.iterdir()
                   if f.suffix == ".jsonl" and "trajectory" not in f.name)
    except Exception:
        return -1


def openclaw_version() -> str:
    try:
        return json.loads(OPENCLAW_JSON.read_text()).get("meta", {}).get("lastTouchedVersion", "?")
    except Exception:
        return "?"


def heartbeat_data() -> dict:
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return {}


def collect() -> dict:
    services = []
    for unit, label, desc in SERVICES:
        s = svc_status(unit)
        services.append({"unit": unit, "label": label, "desc": desc, **s})
    mem  = gateway_memory()
    hb   = heartbeat_data()
    sess = sessions_count()
    ver  = openclaw_version()
    up   = sum(1 for s in services if s["state"] == "active")
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "version": ver,
        "services": services,
        "services_up": up,
        "services_total": len(services),
        "memory": mem,
        "sessions": sess,
        "heartbeat": hb,
    }


def refresh_loop():
    while True:
        data = collect()
        with _lock:
            _cache.clear()
            _cache.update(data)
        time.sleep(20)


def color(state: str) -> str:
    return {"active": "#1a7f37", "failed": "#cf2a27", "inactive": "#b36200"}.get(state, "#6c6c70")


def dot_html(state: str) -> str:
    c = color(state)
    return f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{c};flex-shrink:0;margin-right:6px"></span>'


def render_html(d: dict) -> str:
    mem = d["memory"]
    pct = mem["pct"]
    mem_color = "#1a7f37" if pct < 70 else "#b36200" if pct < 90 else "#cf2a27"

    svc_rows = ""
    for s in d["services"]:
        since = (s["since"].replace("CEST", "").replace("CET", "").strip()[:16]
                 if s["since"] else "–")
        label_map = {"active": "läuft", "inactive": "gestoppt",
                     "failed": "Fehler", "error": "Fehler"}
        state_label = label_map.get(s["state"], s["state"])
        svc_rows += f"""
          <tr>
            <td style="display:flex;align-items:center">{dot_html(s['state'])}{s['label']}</td>
            <td style="color:{color(s['state'])};font-weight:500">{state_label}</td>
            <td style="color:#6c6c70;font-size:12px">{s['desc']}</td>
            <td style="color:#6c6c70;font-size:12px;white-space:nowrap">{since}</td>
          </tr>"""

    hb = d["heartbeat"]
    HB_LABELS = {
        "dispatcher":  "Dispatcher :8765",
        "cache_reader": "Cache Reader :8501",
        "docling":     "Docling :5001",
        "ollama":      "Ollama :11434",
    }
    hb_rows = ""
    for key, label in HB_LABELS.items():
        item   = hb.get(key, {})
        fails  = item.get("failures", 0)
        last_ok = (item.get("last_ok") or "–")[:16]
        c = "#30d158" if fails == 0 else "#ff3b30"
        hb_rows += f"""
          <tr>
            <td style="display:flex;align-items:center">
              <span style="display:inline-block;width:8px;height:8px;border-radius:50%;
                           background:{c};flex-shrink:0;margin-right:6px"></span>{label}
            </td>
            <td style="color:{c};font-weight:500">{'OK' if fails == 0 else f'{fails}× Fehler'}</td>
            <td style="color:#6c6c70;font-size:12px">{last_ok}</td>
          </tr>"""

    badge_cls = "badge-ok" if d["services_up"] == d["services_total"] else "badge-warn"

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>openclaw Health</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:#f2f2f7;color:#1c1c1e;font-size:14px;padding:20px 24px}}
  h2{{font-size:17px;font-weight:700;color:#1c1c1e;margin-bottom:3px}}
  .sub{{font-size:12px;color:#6c6c70;margin-bottom:18px}}
  .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:12px}}
  .card{{background:#fff;border-radius:12px;padding:14px 16px;
         box-shadow:0 1px 3px rgba(0,0,0,.08)}}
  .card-full{{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:12px;
              box-shadow:0 1px 3px rgba(0,0,0,.08)}}
  .card-title{{font-size:11px;color:#6c6c70;text-transform:uppercase;
               letter-spacing:.6px;margin-bottom:10px;font-weight:600}}
  .stat-big{{font-size:26px;font-weight:700;line-height:1;color:#1c1c1e}}
  .stat-label{{font-size:12px;color:#6c6c70;margin-top:3px}}
  .mem-bar{{height:6px;background:#e5e5ea;border-radius:3px;margin:10px 0 5px;overflow:hidden}}
  .mem-fill{{height:100%;border-radius:3px;background:{mem_color};width:{pct}%}}
  .mem-text{{font-size:12px;color:#6c6c70}}
  table{{width:100%;border-collapse:collapse}}
  td{{padding:8px 6px;border-bottom:1px solid #e5e5ea;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  .badge{{display:inline-flex;align-items:center;gap:5px;
          padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600}}
  .badge-ok{{background:rgba(52,199,89,.12);color:#1a7f37}}
  .badge-warn{{background:rgba(255,149,0,.12);color:#b36200}}
  .ts{{font-size:11px;color:#aeaeb2;text-align:right;margin-top:8px}}
</style>
</head>
<body>
<h2>🦞 openclaw · Wilson</h2>
<div class="sub">v{d['version']} &nbsp;·&nbsp; Raspberry Pi 5 &nbsp;·&nbsp; 192.168.3.124 &nbsp;·&nbsp; {d['ts']}</div>

<div class="grid">
  <div class="card">
    <div class="card-title">Services</div>
    <div class="stat-big">
      <span class="badge {badge_cls}">{d['services_up']}/{d['services_total']} aktiv</span>
    </div>
    <div class="stat-label" style="margin-top:8px">openclaw auf Wilson</div>
  </div>
  <div class="card">
    <div class="card-title">Gateway Memory</div>
    <div class="stat-big" style="color:{mem_color};font-size:26px">{mem['rss_mb']} MB</div>
    <div class="mem-bar"><div class="mem-fill"></div></div>
    <div class="mem-text">{pct}% von {mem['limit_mb']} MB Limit</div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <div class="card-title">Sessions (aktiv)</div>
    <div class="stat-big">{d['sessions']}</div>
    <div class="stat-label">Konversationsdateien ≤ 14 Tage</div>
  </div>
  <div class="card">
    <div class="card-title">Wartung</div>
    <div class="stat-big" style="font-size:15px;color:#6c6c70;padding-top:4px">So 03:30 Restart</div>
    <div class="stat-label">Cleanup täglich 03:15</div>
  </div>
</div>

<div class="card-full">
  <div class="card-title">Wilson Services</div>
  <table>{svc_rows}</table>
</div>

<div class="card-full">
  <div class="card-title">Ryzen Monitoring (Heartbeat von Wilson)</div>
  <table>{hb_rows}</table>
</div>

<div class="ts">Auto-Refresh alle 20 s</div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        with _lock:
            data = dict(_cache) if _cache else collect()

        if self.path == "/health":
            body = json.dumps({
                "status": "ok" if data.get("services_up") == data.get("services_total") else "degraded",
                "services_up":    data.get("services_up"),
                "services_total": data.get("services_total"),
                "memory_pct":     data.get("memory", {}).get("pct"),
            }).encode()
            ct = "application/json"
        else:
            body = render_html(data).encode()
            ct   = "text/html; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    with _lock:
        _cache.update(collect())
    print(f"openclaw Health Server auf Port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
