"""File-Watcher: aktualisiert den FTS5-Index bei Änderungen im Cache-Verzeichnis."""
import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import CACHE_DIR, WATCH_DEBOUNCE_SECONDS
import indexer

log = logging.getLogger("watcher")


class CacheEventHandler(FileSystemEventHandler):
    """Debouncing: mehrfache Änderungen derselben Datei binnen Zeitfenster werden zu einem Update zusammengefasst."""

    def __init__(self, conn, write_lock: threading.Lock):
        self.conn = conn
        self.write_lock = write_lock
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _schedule_flush(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(WATCH_DEBOUNCE_SECONDS, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        with self._lock:
            batch, self._pending = self._pending, {}
        if not batch:
            return
        with self.write_lock:
            for path_str, _ in batch.items():
                path = Path(path_str)
                if path.exists():
                    indexer.index_single_file(self.conn, path)
                else:
                    cache_entry_path = self._resolve_cache_path_by_filename(path.name)
                    if cache_entry_path:
                        indexer.delete_by_path(self.conn, cache_entry_path)
            self.conn.commit()

    def _resolve_cache_path_by_filename(self, json_name: str) -> str | None:
        cur = self.conn.execute("SELECT path FROM documents LIMIT 1")
        # JSON-Hash ist nicht umkehrbar auf Vault-Pfad — Delete-by-Vault-Path ist bei /reindex effektiver
        return None

    def _queue(self, path: str):
        if not path.endswith(".json"):
            return
        with self._lock:
            self._pending[path] = time.time()
        self._schedule_flush()

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory:
            self._queue(event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._queue(event.src_path)

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory:
            self._queue(event.src_path)


def start_watcher(conn, write_lock: threading.Lock) -> Observer:
    handler = CacheEventHandler(conn, write_lock)
    observer = Observer()
    observer.schedule(handler, str(CACHE_DIR), recursive=False)
    observer.start()
    log.info("Watcher started on %s", CACHE_DIR)
    return observer
