"""
enex_tag_mapper.py — Tag-Routing und Tag-Normalisierung für den ENEX-Import.
Liest dispatcher-config/enex-tags.yaml.

Verwendung:
    from enex_tag_mapper import EnexTagMapper
    mapper = EnexTagMapper("/config/enex-tags.yaml")
    result = mapper.route_by_tags(note.tags)
    print(result.vault_folder, result.normalized_tags)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:
    raise ImportError("PyYAML fehlt: pip install pyyaml")

logger = logging.getLogger(__name__)

_ADRESSATEN = {"Marion", "Reinhard", "Linoa"}


# ---------------------------------------------------------------------------
# Ergebnis-Datenklasse
# ---------------------------------------------------------------------------

@dataclass
class RoutingResult:
    """Ergebnis der Routing-Entscheidung für eine Note."""
    vault_folder: str
    kategorie: Optional[str]
    typ: Optional[str]
    normalized_tags: List[str]
    adressat_hint: Optional[str] = None
    source: str = "fallback"   # "prefix" | "tag_rule" | "llm" | "fallback"

    def __str__(self):
        return (f"RoutingResult(folder={self.vault_folder!r}, "
                f"kategorie={self.kategorie!r}, typ={self.typ!r}, "
                f"source={self.source!r})")


# ---------------------------------------------------------------------------
# Mapper-Klasse
# ---------------------------------------------------------------------------

class EnexTagMapper:
    """
    Liest enex-tags.yaml und bietet:
      - route_by_prefix(prefix, tags)  → Routing via Dateiname-Präfix
      - route_by_tags(tags)            → Routing via Tag-Regeln
      - filter_tags(tags)              → Jahreszahlen, Hex-Codes etc. entfernen
    """

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self._load()

    # ------------------------------------------------------------------
    # Konfiguration laden
    # ------------------------------------------------------------------

    def _load(self):
        if not self.config_path.exists():
            raise FileNotFoundError(f"enex-tags.yaml nicht gefunden: {self.config_path}")

        with open(self.config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.prefix_map: Dict[str, Dict] = cfg.get("prefix_map", {})
        self.tag_rules: List[Dict] = cfg.get("tag_rules", [])
        self.fallback: Dict = cfg.get("fallback", {
            "vault_folder": "00 Inbox",
            "kategorie": None,
            "typ": "enex_import",
            "tag_output": [],
        })

        # Kompilierte Regex-Filter
        remove_patterns = cfg.get("tag_filter", {}).get("remove_patterns", [])
        self._remove_patterns = [re.compile(p) for p in remove_patterns]

        logger.debug(
            "EnexTagMapper: %d Präfixe, %d Tag-Regeln, %d Filter-Pattern",
            len(self.prefix_map), len(self.tag_rules), len(self._remove_patterns)
        )

    # ------------------------------------------------------------------
    # Tag-Filterung
    # ------------------------------------------------------------------

    def filter_tags(self, tags: List[str]) -> List[str]:
        """
        Entfernt Jahreszahlen (2019), Monats-Tags (2023-05),
        Hex-Farben (#ffffffff) und Einzel-Buchstaben aus der Tag-Liste.
        Beibehaltung von Adressaten (Marion, Reinhard, Linoa).
        """
        result = []
        for tag in tags:
            if any(p.match(tag) for p in self._remove_patterns):
                logger.debug("Tag gefiltert: %r", tag)
                continue
            result.append(tag)
        return result

    # ------------------------------------------------------------------
    # Routing via Dateiname-Präfix
    # ------------------------------------------------------------------

    def route_by_prefix(
        self,
        prefix: str,
        note_tags: List[str],
        typ_override: Optional[str] = None,
    ) -> Optional[RoutingResult]:
        """
        Routing über den nn_-Präfix im ENEX-Dateinamen.

        Args:
            prefix: Zweistellige Zahl als String, z.B. "49".
            note_tags: Evernote-Tags der Note (werden gefiltert).
            typ_override: Typ-ID aus __typ-id Teil des Dateinamens.

        Returns:
            RoutingResult oder None wenn Präfix unbekannt.
        """
        entry = self.prefix_map.get(str(prefix))
        if not entry:
            logger.debug("Präfix %r nicht in prefix_map", prefix)
            return None

        normalized = self.filter_tags(note_tags)
        adressat_hint = self._extract_adressat(note_tags)

        return RoutingResult(
            vault_folder=entry["vault_folder"],
            kategorie=entry.get("kategorie"),
            typ=typ_override,
            normalized_tags=normalized,
            adressat_hint=adressat_hint,
            source="prefix",
        )

    # ------------------------------------------------------------------
    # Routing via Tag-Regeln
    # ------------------------------------------------------------------

    def route_by_tags(self, note_tags: List[str]) -> RoutingResult:
        """
        Routing über tag_rules in enex-tags.yaml.
        Erster Match gewinnt. Kein Match → Fallback (00 Inbox).

        Args:
            note_tags: Evernote-Tags der Note.

        Returns:
            RoutingResult (niemals None — Fallback ist garantiert).
        """
        note_tags_lower = [t.lower() for t in note_tags]

        for rule in self.tag_rules:
            match_mode = rule.get("match_mode", "exact")
            rule_tags = rule.get("evernote_tags", [])
            rule_tags_lower = [t.lower() for t in rule_tags]

            matched = self._match_tags(note_tags_lower, rule_tags_lower, match_mode)

            if matched:
                tag_output: List[str] = rule.get("tag_output", [])

                # Evernote-Tags, die nicht in tag_output sind, bereinigt hinzufügen
                rule_tags_set = {t.lower() for t in rule_tags}
                tag_output_set = {t.lower() for t in tag_output}
                extra = self.filter_tags([
                    t for t in note_tags
                    if t.lower() not in rule_tags_set
                    and t.lower() not in tag_output_set
                    and t not in _ADRESSATEN
                ])
                normalized_tags = tag_output + extra

                adressat_hint = rule.get("adressat_hint") or self._extract_adressat(note_tags)

                vault_folder = self._kategorie_to_folder(rule.get("kategorie"))

                logger.debug(
                    "Tag-Match für %r → %s (source=tag_rule)",
                    note_tags, vault_folder
                )

                return RoutingResult(
                    vault_folder=vault_folder,
                    kategorie=rule.get("kategorie"),
                    typ=rule.get("typ"),
                    normalized_tags=normalized_tags,
                    adressat_hint=adressat_hint,
                    source="tag_rule",
                )

        # Kein Match → Fallback
        logger.debug("Kein Tag-Match für %r → Fallback (00 Inbox)", note_tags)
        normalized = self.filter_tags(note_tags)
        adressat_hint = self._extract_adressat(note_tags)

        return RoutingResult(
            vault_folder=self.fallback.get("vault_folder", "00 Inbox"),
            kategorie=self.fallback.get("kategorie"),
            typ=self.fallback.get("typ", "enex_import"),
            normalized_tags=normalized,
            adressat_hint=adressat_hint,
            source="fallback",
        )

    # ------------------------------------------------------------------
    # Hilfsmethoden
    # ------------------------------------------------------------------

    def _match_tags(
        self,
        note_tags_lower: List[str],
        rule_tags_lower: List[str],
        match_mode: str,
    ) -> bool:
        """Prüft ob mindestens ein Rule-Tag in den Note-Tags vorkommt."""
        if match_mode == "exact":
            return any(rt in note_tags_lower for rt in rule_tags_lower)
        elif match_mode == "contains":
            return any(
                rt in nt
                for rt in rule_tags_lower
                for nt in note_tags_lower
            )
        else:
            logger.warning("Unbekannter match_mode: %r", match_mode)
            return False

    def _extract_adressat(self, tags: List[str]) -> Optional[str]:
        """Extrahiert Adressat (Marion/Reinhard/Linoa) aus den Tags."""
        for t in tags:
            if t in _ADRESSATEN:
                return t
        return None

    def _kategorie_to_folder(self, kategorie: Optional[str]) -> str:
        """Sucht den vault_folder für eine Kategorie-ID in der prefix_map."""
        if not kategorie:
            return self.fallback.get("vault_folder", "00 Inbox")
        for entry in self.prefix_map.values():
            if entry.get("kategorie") == kategorie:
                return entry["vault_folder"]
        logger.warning("Kategorie %r nicht in prefix_map gefunden", kategorie)
        return self.fallback.get("vault_folder", "00 Inbox")
