"""
enex_parser.py — ENEX-XML-Parser
Liest Evernote-Exportdateien (.enex) und gibt Note-Objekte zurück.

Verwendung:
    from enex_parser import parse_enex
    notes = parse_enex("/data/input-dispatcher/enex/export.enex")
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

ENEX_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class Resource:
    """Ein Anhang (PDF, Bild, …) aus einer ENEX-Note."""
    data: bytes
    mime: str
    filename: str
    md5: str = ""

    def __post_init__(self):
        if not self.md5:
            self.md5 = hashlib.md5(self.data).hexdigest()

    @property
    def is_pdf(self) -> bool:
        return self.mime == "application/pdf"

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


@dataclass
class Note:
    """Eine einzelne Evernote-Notiz, extrahiert aus ENEX."""
    title: str
    created: datetime          # UTC-aware
    updated: Optional[datetime]
    tags: List[str]
    content_enml: str          # Roher ENML-Inhalt
    resources: List[Resource] = field(default_factory=list)
    note_hash: str = ""        # SHA1(title + created_iso) für Duplikat-Check

    def __post_init__(self):
        if not self.note_hash:
            raw = f"{self.title}{self.created.isoformat()}"
            self.note_hash = hashlib.sha1(raw.encode()).hexdigest()

    @property
    def pdf_resources(self) -> List[Resource]:
        return [r for r in self.resources if r.is_pdf]

    @property
    def image_resources(self) -> List[Resource]:
        return [r for r in self.resources if r.is_image]


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _parse_datetime(s: str) -> Optional[datetime]:
    """Parst '20231205T143022Z' → UTC-aware datetime."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s.strip(), ENEX_DATE_FORMAT)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Ungültiges Datum: %r", s)
        return None


def _parse_resource(res_el: ET.Element) -> Optional[Resource]:
    """Parst ein einzelnes <resource>-Element."""
    data_el = res_el.find("data")
    mime_el = res_el.find("mime")
    attrs_el = res_el.find("resource-attributes")

    if data_el is None or not data_el.text:
        return None

    mime = "application/octet-stream"
    if mime_el is not None and mime_el.text:
        mime = mime_el.text.strip()

    filename = "attachment"
    if attrs_el is not None:
        fn_el = attrs_el.find("file-name")
        if fn_el is not None and fn_el.text:
            filename = fn_el.text.strip()

    # base64 kann Zeilenumbrüche enthalten
    raw_b64 = re.sub(r"\s+", "", data_el.text)
    try:
        data = base64.b64decode(raw_b64)
    except Exception as exc:
        logger.warning("Base64-Dekodierung fehlgeschlagen (%s): %s", filename, exc)
        return None

    return Resource(data=data, mime=mime, filename=filename)


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def parse_enex(filepath: str | Path) -> List[Note]:
    """
    Parst eine .enex-Datei.

    Returns:
        Liste von Note-Objekten (ungültige Notes werden übersprungen).

    Raises:
        ET.ParseError: wenn die Datei kein gültiges XML ist.
        FileNotFoundError: wenn die Datei nicht existiert.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"ENEX-Datei nicht gefunden: {filepath}")

    logger.info("Lese ENEX: %s", filepath.name)

    try:
        tree = ET.parse(str(filepath))
        root = tree.getroot()
    except ET.ParseError as exc:
        logger.error("XML-Parsing fehlgeschlagen in %s: %s", filepath.name, exc)
        raise

    all_note_els = root.findall("note")
    notes: List[Note] = []
    skipped = 0

    for note_el in all_note_els:
        try:
            title_el = note_el.find("title")
            title = (title_el.text or "").strip() or "Unbekannt"

            created_el = note_el.find("created")
            created_str = (created_el.text or "").strip() if created_el is not None else ""
            created = _parse_datetime(created_str)
            if created is None:
                logger.warning("Note ohne gültiges Datum übersprungen: %r", title)
                skipped += 1
                continue

            updated_el = note_el.find("updated")
            updated = _parse_datetime((updated_el.text or "").strip()) if updated_el is not None else None

            tags = [t.text.strip() for t in note_el.findall("tag") if t.text and t.text.strip()]

            content_el = note_el.find("content")
            content_enml = (content_el.text or "") if content_el is not None else ""

            resources: List[Resource] = []
            for res_el in note_el.findall("resource"):
                res = _parse_resource(res_el)
                if res is not None:
                    resources.append(res)

            notes.append(Note(
                title=title,
                created=created,
                updated=updated,
                tags=tags,
                content_enml=content_enml,
                resources=resources,
            ))

        except Exception as exc:  # noqa: BLE001
            logger.error("Fehler beim Parsen einer Note: %s", exc, exc_info=True)
            skipped += 1
            continue

    logger.info("  → %d Notes geparst, %d übersprungen", len(notes), skipped)
    return notes
