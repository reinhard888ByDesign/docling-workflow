"""HTTP-API des Cache-Reader-Service (FastAPI)."""
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

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
