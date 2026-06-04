"""HTTP-API des Cache-Reader-Service (FastAPI)."""
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
import indexer
import watcher as watcher_module

log = logging.getLogger("api")

_conn: sqlite3.Connection | None = None
_write_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = indexer.get_connection()
    return _conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_conn()
    stats = indexer.get_stats(conn)
    if stats["total_documents"] == 0:
        log.info("Empty index — running initial full reindex")
        indexer.full_reindex(conn)
    observer = watcher_module.start_watcher(conn, _write_lock)
    yield
    observer.stop()
    observer.join(timeout=5)
    if _conn is not None:
        _conn.close()


app = FastAPI(title="Cache-Reader", version="0.1.0", lifespan=lifespan)


def _fts5_escape(query: str) -> str:
    """Baut eine FTS5-kompatible Query aus Nutzereingabe.

    Strategie: Alle Terme als Quoted-Tokens mit OR verknüpfen.
    Damit werden Sonderzeichen neutralisiert und Nutzer bekommen
    das intuitive "mindestens ein Term matcht"-Verhalten.
    """
    terms = [t.strip() for t in query.split() if t.strip()]
    if not terms:
        return ""
    quoted = [f'"{t.replace(chr(34), "")}"' for t in terms]
    return " OR ".join(quoted)


@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request) -> HTMLResponse:
    """Einfache Landing-Page mit Suche und Stats-Übersicht."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cache Reader</title>
<style>
:root{{--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--accent:#0071e3;--border:rgba(0,0,0,0.07);--radius:12px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 20px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:28px 32px;max-width:640px;width:100%;box-shadow:0 1px 4px rgba(0,0,0,0.04)}}
h1{{font-size:1.4rem;font-weight:700;margin-bottom:4px;display:flex;align-items:center;gap:8px}}
h1 span{{font-size:.8rem;color:var(--muted);font-weight:400}}
.subtitle{{font-size:.85rem;color:var(--muted);margin-bottom:20px}}
form{{display:flex;gap:8px;margin-bottom:20px}}
input[type=search]{{flex:1;padding:8px 14px;border:1px solid var(--border);border-radius:8px;font-size:.9rem;outline:none}}
input[type=search]:focus{{border-color:var(--accent)}}
button{{padding:8px 18px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:.9rem}}
button:hover{{opacity:.9}}
#results{{margin-top:12px;font-size:.85rem}}
.result-item{{padding:10px 0;border-bottom:1px solid var(--border)}}
.result-path{{font-weight:600;color:var(--accent)}}
.result-excerpt{{color:var(--text);margin-top:3px}}
.result-meta{{font-size:.78rem;color:var(--muted);margin-top:2px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:20px}}
.stat{{background:#f8f9fb;border-radius:8px;padding:12px 14px;text-align:center}}
.stat-value{{font-size:1.3rem;font-weight:700}}
.stat-label{{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-top:2px}}
.loading{{color:var(--muted);font-style:italic;padding:20px;text-align:center}}
.empty{{color:var(--muted);padding:20px;text-align:center}}
.links{{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}}
.links a{{font-size:.8rem;padding:5px 12px;border:1px solid var(--border);border-radius:6px;text-decoration:none;color:var(--accent)}}
.links a:hover{{background:rgba(0,113,227,0.06)}}
.error{{color:#dc2626;font-size:.82rem;padding:10px;background:#fef2f2;border-radius:6px;margin-top:8px;display:none}}
/* Hilfe-Overlay */
.help-overlay{{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;display:none;justify-content:center;align-items:center;padding:20px}}
.help-overlay.open{{display:flex}}
.help-box{{background:var(--card);border-radius:var(--radius);padding:28px 32px;max-width:600px;width:100%;max-height:80vh;overflow-y:auto}}
.help-box h3{{margin:16px 0 6px;font-size:1rem}}
.help-box h3:first-of-type{{margin-top:0}}
.help-box p,.help-box ul{{font-size:.85rem;color:var(--muted);line-height:1.6;margin-bottom:8px}}
.help-box ul{{padding-left:20px}}
.help-box code{{background:#f0f0f5;padding:1px 5px;border-radius:3px;font-size:.8rem}}
.help-close{{float:right;background:none;border:none;font-size:1.4rem;cursor:pointer;padding:0;line-height:1;color:var(--muted)}}
.help-btn{{display:inline-flex;align-items:center;gap:4px;font-size:.8rem;padding:5px 12px;border:1px solid var(--border);border-radius:6px;text-decoration:none;color:var(--accent);cursor:pointer;background:none}}
.help-btn:hover{{background:rgba(0,113,227,0.06)}}
</style>
</head>
<body>
<div class="card">
<h1>🗄️ Cache Reader <span>(Docling Workflow)</span></h1>
<p class="subtitle">Volltextsuche über alle verarbeiteten Dokumente</p>

<div class="stats" id="stats">
  <div class="stat"><div class="stat-value" id="stat-total">…</div><div class="stat-label">Dokumente</div></div>
  <div class="stat"><div class="stat-value" id="stat-usable">…</div><div class="stat-label">Durchsuchbar</div></div>
</div>

<form onsubmit="doSearch(event)">
  <input type="search" name="q" id="q" placeholder="Suchbegriff…" autofocus>
  <button type="submit">Suchen</button>
</form>

<div id="results"><div class="empty">Gib einen Suchbegriff ein.</div></div>
<div id="error" class="error"></div>

<div class="links">
  <a href="/docs">📖 API Docs</a>
  <a href="/openapi.json">📋 OpenAPI</a>
  <a href="/health">💚 Health</a>
  <a href="/stats">📊 Stats (JSON)</a>
  <button class="help-btn" onclick="openHelp()">❓ Hilfe</button>
</div>
</div>

<!-- Hilfe-Overlay -->
<div class="help-overlay" id="helpOverlay" onclick="if(event.target===this)closeHelp()">
<div class="help-box">
<button class="help-close" onclick="closeHelp()">✕</button>
<h2>❓ Cache Reader — Hilfe</h2>

<h3>Was ist der Cache Reader?</h3>
<p>Der Cache Reader ist der <strong>Volltextsuchdienst</strong> im Docling-Workflow. Er durchsucht
alle vom Docling-OCR-Prozess extrahierten PDF-Texte und ermöglicht eine schnelle
Stichwortsuche über den gesamten Dokumentenbestand.</p>

<h3>Wie funktioniert es?</h3>
<p>Jedes verarbeitete PDF wird von einer OCR-Pipeline (Docling) in Text umgewandelt und
in einem Cache-Verzeichnis gespeichert. Der Cache Reader baut daraus einen
<strong>SQLite-FTS5-Volltextindex</strong> auf. Ein File-Watcher erkennt Änderungen
automatisch und hält den Index aktuell.</p>

<h3>Suche</h3>
<ul>
  <li>Suchbegriff ins Suchfeld eingeben und <strong>Enter</strong> drücken</li>
  <li>Es wird nach exakten Wörtern gesucht (FTS5-Phrasensuche)</li>
  <li>Die Ergebnisse zeigen den <strong>Vault-Pfad</strong>, einen Textauszug und den Score</li>
  <li>Der Score (bm25) bewertet die Relevanz: je höher, desto besser der Treffer</li>
</ul>

<h3>Neu-Indizierung</h3>
<p>Normalerweise nie nötig — Änderungen werden automatisch erkannt. Nur bei
beschädigtem Index oder nach großen Batch-Importen manuell über
<code>POST /reindex</code> auslösen.</p>

<h3>Integration</h3>
<p>Der Cache Reader ist im Dispatcher-Dashboard unter <code>/cache</code> eingebettet.
Dort können Suchergebnisse direkt an den Batch-Prozessor übergeben werden,
um Dokumente gezielt neu zu klassifizieren.</p>
</div>
</div>
<script>
// Path-Interceptor: Rewrite fetch()-URLs wenn in Hub-iframe eingebettet
(function(){{
  const p = window.location.pathname.replace(/\/+$/,'');
  if (p !== '' && p !== '/') {{
    const _fetch = window.fetch;
    window.fetch = function(url, opts) {{
      if (typeof url === 'string' && url.startsWith('/')) url = p + url;
      return _fetch.call(window, url, opts);
    }};
  }}
}})();
async function loadStats() {{
  try {{
    const r = await fetch('/stats');
    const d = await r.json();
    document.getElementById('stat-total').textContent = d.total_documents || 0;
    document.getElementById('stat-usable').textContent = d.usable_documents || 0;
  }} catch(e) {{ console.error(e); }}
}}
async function doSearch(e) {{
  e.preventDefault();
  const q = document.getElementById('q').value.trim();
  const results = document.getElementById('results');
  const error = document.getElementById('error');
  error.style.display = 'none';
  if (!q) {{ results.innerHTML = '<div class="empty">Gib einen Suchbegriff ein.</div>'; return; }}
  results.innerHTML = '<div class="loading">Suche…</div>';
  try {{
    const r = await fetch('/search?q=' + encodeURIComponent(q) + '&limit=20');
    if (!r.ok) throw new Error(r.status + ' ' + (await r.text()));
    const d = await r.json();
    if (d.count === 0) {{
      results.innerHTML = '<div class="empty">Keine Treffer für »' + q + '«</div>';
    }} else {{
      results.innerHTML = '<p style="margin-bottom:8px;color:var(--muted)">' + d.count + ' Treffer:</p>' +
        d.results.map(r => '<div class="result-item">' +
          '<div class="result-path">' + r.path + '</div>' +
          '<div class="result-excerpt">' + (r.excerpt || '…') + '</div>' +
          '<div class="result-meta">Score: ' + r.score + ' · ' + (r.langs || '?') + '</div>' +
        '</div>').join('');
    }}
  }} catch(e) {{
    error.textContent = 'Fehler: ' + e.message;
    error.style.display = 'block';
    results.innerHTML = '';
  }}
}}
function openHelp(){{document.getElementById('helpOverlay').classList.add('open')}}
function closeHelp(){{document.getElementById('helpOverlay').classList.remove('open')}}
loadStats();
</script>
</body>
</html>""")


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(config.DEFAULT_SEARCH_LIMIT, ge=1, le=config.MAX_SEARCH_LIMIT),
) -> JSONResponse:
    fts_query = _fts5_escape(q)
    if not fts_query:
        return JSONResponse({"query": q, "count": 0, "results": []})

    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT path, langs, "
            "snippet(documents, 1, '', '', '...', 20) AS excerpt, "
            "bm25(documents) AS score "
            "FROM documents WHERE documents MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, limit),
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        raise HTTPException(status_code=400, detail=f"FTS5 error: {e}")

    results = [
        {
            "path": row[0],
            "langs": row[1] or None,
            "excerpt": row[2],
            "score": round(float(row[3]), 4),
        }
        for row in rows
    ]
    return JSONResponse({"query": q, "count": len(results), "results": results})


@app.get("/file")
def get_file(path: str = Query(..., min_length=1)) -> JSONResponse:
    conn = get_conn()
    cur = conn.execute(
        "SELECT path, text, langs, mtime FROM documents WHERE path = ? LIMIT 1",
        (path,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No cache entry for path: {path}")
    return JSONResponse(
        {"path": row[0], "text": row[1], "langs": row[2] or None, "mtime": row[3]}
    )


@app.get("/stats")
def get_stats() -> JSONResponse:
    conn = get_conn()
    return JSONResponse(indexer.get_stats(conn))


@app.post("/reindex")
def reindex() -> JSONResponse:
    conn = get_conn()
    with _write_lock:
        result = indexer.full_reindex(conn)
    return JSONResponse({"status": "ok", **result})


@app.get("/health")
def health() -> JSONResponse:
    try:
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "healthy"})
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=config.HTTP_PORT)
