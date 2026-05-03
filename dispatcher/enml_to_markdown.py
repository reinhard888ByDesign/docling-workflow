"""
enml_to_markdown.py — ENML → Markdown Konverter
ENML (Evernote Markup Language) ist ein HTML-Dialekt.

Verwendung:
    from enml_to_markdown import enml_to_markdown
    md = enml_to_markdown(note.content_enml, image_filenames=["foto.jpg"])
"""

from __future__ import annotations

import logging
import re
from typing import List

try:
    import html2text as _html2text
    _HTML2TEXT_AVAILABLE = True
except ImportError:
    _HTML2TEXT_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "html2text nicht installiert — Fallback auf einfache Tag-Entfernung. "
        "Installieren: pip install html2text"
    )

logger = logging.getLogger(__name__)

# Bild-Erweiterungen, die als Obsidian-Embed gerendert werden
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}

# Pattern für überschüssige Leerzeilen
_EXCESS_NEWLINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _preprocess_enml(enml: str) -> str:
    """
    Bereitet ENML für html2text vor:
    - XML-Deklaration und DOCTYPE entfernen
    - <en-note> → <div>
    - <en-todo> → HTML-Checkboxen
    - <en-media> entfernen (Ressourcen werden separat verlinkt)
    """
    html = enml

    # Checkboxen VOR dem allgemeinen Cleanup ersetzen
    html = re.sub(r'<en-todo\s+checked="true"\s*/>', '[x] ', html, flags=re.IGNORECASE)
    html = re.sub(r'<en-todo\s+checked="false"\s*/>', '[ ] ', html, flags=re.IGNORECASE)
    html = re.sub(r'<en-todo\s*/>', '[ ] ', html, flags=re.IGNORECASE)

    # XML-Prolog und DOCTYPE entfernen
    html = re.sub(r'<\?xml[^>]*\?>', '', html)
    html = re.sub(r'<!DOCTYPE[^>]*>', '', html)

    # <en-note> → <div> (html2text behandelt es als Block)
    html = re.sub(r'<en-note([^>]*)>', r'<div\1>', html, flags=re.IGNORECASE)
    html = re.sub(r'</en-note>', '</div>', html, flags=re.IGNORECASE)

    # <en-media>-Tags entfernen (Ressourcen-Platzhalter — wir fügen sie manuell ein)
    html = re.sub(r'<en-media[^>]*/>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<en-media[^>]*>.*?</en-media>', '', html, flags=re.IGNORECASE | re.DOTALL)

    return html


def _simple_strip(html: str) -> str:
    """Fallback ohne html2text: entfernt HTML-Tags grob."""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '\n- ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    # HTML-Entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    return text.strip()


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def enml_to_markdown(
    enml: str,
    image_filenames: List[str] | None = None,
    include_images: bool = False,
) -> str:
    """
    Konvertiert ENML zu sauberem Markdown.

    Args:
        enml: Roher ENML-String aus <content>.
        image_filenames: Dateinamen der Bild-Ressourcen (für Obsidian-Embeds).
        include_images: Wenn True, werden Bilder als ![[filename]] angehängt.

    Returns:
        Markdown-String, bereinigt und normalisiert.
    """
    if not enml or not enml.strip():
        return ""

    html = _preprocess_enml(enml)

    if _HTML2TEXT_AVAILABLE:
        h = _html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True      # Bilder werden separat als Obsidian-Link eingefügt
        h.body_width = 0            # Kein automatischer Zeilenumbruch
        h.protect_links = True
        h.unicode_snob = True
        h.wrap_links = False
        md = h.handle(html)
    else:
        md = _simple_strip(html)

    # Bilder als Obsidian-Embeds anhängen
    if include_images and image_filenames:
        img_links = []
        for fname in image_filenames:
            suffix = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if suffix in _IMAGE_EXTENSIONS:
                img_links.append(f"![[{fname}]]")
        if img_links:
            md = md.rstrip() + "\n\n" + "\n".join(img_links)

    # Normalisierung
    md = _EXCESS_NEWLINES.sub("\n\n", md)
    md = md.strip()

    return md


def make_ocr_placeholder() -> str:
    """Erzeugt den Platzhalter-Block für ausstehende OCR (Phase 2)."""
    return "\n\n## Dokumentinhalt (OCR)\n\n_Ausstehend — wird im Nachtlauf verarbeitet._\n"


def replace_ocr_placeholder(md_content: str, ocr_text: str) -> str:
    """
    Ersetzt den OCR-Platzhalter durch den tatsächlichen OCR-Text.
    Wird von enex_ocr_worker.py aufgerufen.
    """
    placeholder = "## Dokumentinhalt (OCR)\n\n_Ausstehend — wird im Nachtlauf verarbeitet._"
    replacement = f"## Dokumentinhalt (OCR)\n\n{ocr_text.strip()}"
    if placeholder in md_content:
        return md_content.replace(placeholder, replacement, 1)
    # Fallback: ans Ende anhängen wenn Platzhalter nicht gefunden
    logger.warning("OCR-Platzhalter nicht gefunden — hänge Text ans Ende")
    return md_content.rstrip() + f"\n\n## Dokumentinhalt (OCR)\n\n{ocr_text.strip()}\n"
