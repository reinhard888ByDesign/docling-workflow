"""Indexiert den Text-Extractor-Cache in eine SQLite FTS5-Datenbank."""
import json
import logging
import sqlite3
import time
from pathlib import Path

from langdetect import DetectorFactory, detect, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from config import CACHE_DIR, INDEX_DB

DetectorFactory.seed = 0

log = logging.getLogger("indexer")

MIN_CHARS_FOR_LANG = 100
EXPECTED_LANGS = {"de", "it", "en"}


def detect_language(text: str) -> str:
    if not text or len(text) < MIN_CHARS_FOR_LANG:
        return "unknown"
    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return "unknown"
    for c in candidates:
        if c.lang in EXPECTED_LANGS and c.prob >= 0.65:
            return c.lang
    if candidates and candidates[0].prob >= 0.9:
        return candidates[0].lang
    return "unknown"


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5(
    path UNINDEXED,
    text,
    langs UNINDEXED,
    mtime UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_DB, check_same_thread=False)
    conn.executescript(SCHEMA)
    return conn


def _load_cache_entry(json_path: Path) -> dict | None:
    try:
        with json_path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict) or "path" not in data:
            return None
        text = data.get("text", "")
        return {
            "path": data.get("path", ""),
            "text": text,
            "langs": detect_language(text),
            "mtime": json_path.stat().st_mtime,
        }
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Skip %s: %s", json_path.name, e)
        return None


def upsert_entry(conn: sqlite3.Connection, entry: dict) -> None:
    conn.execute("DELETE FROM documents WHERE path = ?", (entry["path"],))
    conn.execute(
        "INSERT INTO documents(path, text, langs, mtime) VALUES (?, ?, ?, ?)",
        (entry["path"], entry["text"], entry["langs"], entry["mtime"]),
    )


def delete_by_path(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM documents WHERE path = ?", (path,))


def full_reindex(conn: sqlite3.Connection) -> dict:
    start = time.time()
    conn.execute("DELETE FROM documents")
    count = 0
    skipped = 0
    for json_file in CACHE_DIR.glob("*.json"):
        entry = _load_cache_entry(json_file)
        if entry is None:
            skipped += 1
            continue
        upsert_entry(conn, entry)
        count += 1
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_full_reindex', ?)",
        (str(int(time.time())),),
    )
    conn.commit()
    duration = time.time() - start
    log.info("Full reindex: %d indexed, %d skipped in %.1fs", count, skipped, duration)
    return {"indexed": count, "skipped": skipped, "duration_seconds": round(duration, 2)}


def index_single_file(conn: sqlite3.Connection, json_path: Path) -> bool:
    entry = _load_cache_entry(json_path)
    if entry is None:
        return False
    upsert_entry(conn, entry)
    conn.commit()
    log.info("Indexed: %s", entry["path"])
    return True


def get_stats(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("SELECT COUNT(*) FROM documents")
    total = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM documents WHERE length(text) < 50")
    empty = cur.fetchone()[0]

    cur = conn.execute("SELECT langs, COUNT(*) FROM documents GROUP BY langs")
    langs = {row[0] or "unknown": row[1] for row in cur.fetchall()}

    cur = conn.execute("SELECT value FROM meta WHERE key = 'last_full_reindex'")
    row = cur.fetchone()
    last_reindex = int(row[0]) if row else None

    return {
        "total_documents": total,
        "empty_documents": empty,
        "usable_documents": total - empty,
        "languages": langs,
        "last_full_reindex": last_reindex,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = get_connection()
    result = full_reindex(conn)
    stats = get_stats(conn)
    print(json.dumps({"reindex": result, "stats": stats}, indent=2))
