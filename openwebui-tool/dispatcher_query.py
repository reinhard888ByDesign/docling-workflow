"""
title: Gesundheits-DB Abfrage
author: dispatcher
description: Beantwortet natürlichsprachliche Fragen zur Gesundheits- und Versicherungsdatenbank (Arztkosten, Erstattungen, offene Rechnungen, etc.)
version: 1.0
license: MIT
"""

import requests


class Tools:
    def __init__(self):
        self.api_url = "http://document-dispatcher:8765/api/query"

    def frage_gesundheitsdb(self, frage: str) -> str:
        """
        Beantwortet Fragen zur Gesundheits- und Versicherungsdatenbank der Familie Janning.

        Beispiele:
        - "Wie hoch waren meine Arztkosten 2024?"
        - "Welche Rechnungen sind noch offen?"
        - "Erstattungsquote Gothaer 2023"
        - "Alle Rezepte von Marion"
        - "Welche Ärzte hat Reinhard 2024 besucht?"

        :param frage: Die Frage in natürlicher Sprache
        :return: Ergebnis der Datenbankabfrage
        """
        try:
            r = requests.post(
                self.api_url,
                json={"question": frage},
                timeout=90,
            )
            if r.ok:
                return r.json().get("result", "Kein Ergebnis")
            return f"API-Fehler {r.status_code}: {r.text[:200]}"
        except requests.exceptions.Timeout:
            return "Timeout — Ollama braucht zu lange. Bitte erneut versuchen."
        except Exception as e:
            return f"Verbindungsfehler: {e}"
