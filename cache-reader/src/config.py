"""Konfiguration des Cache-Reader-Service."""
import os
from pathlib import Path

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/vault-cache"))
INDEX_DB = Path(os.environ.get("INDEX_DB", "/data/index.db"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").lower()
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8501"))

WATCH_DEBOUNCE_SECONDS = float(os.environ.get("WATCH_DEBOUNCE_SECONDS", "2.0"))
DEFAULT_SEARCH_LIMIT = int(os.environ.get("DEFAULT_SEARCH_LIMIT", "10"))
MAX_SEARCH_LIMIT = int(os.environ.get("MAX_SEARCH_LIMIT", "100"))
EXCERPT_LENGTH = int(os.environ.get("EXCERPT_LENGTH", "160"))
