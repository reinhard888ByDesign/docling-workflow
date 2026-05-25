#!/usr/bin/env python3
"""
Vault Summarizer — fasst .md-Dateien im Obsidian-Vault zusammen.

Modi:
  --test FILE    Einzeldatei testen (kein Schreiben)
  --run          Alle Dateien verarbeiten (schreibt Zusammenfassungen)
  --model NAME   Ollama-Modell (default: qwen3:4b-instruct)
  --limit N      Max. Dateien (für Dry-Runs)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from langdetect import DetectorFactory, LangDetectException, detect_langs

DetectorFactory.seed = 0

# ── Konfiguration ─────────────────────────────────────────────────────────────

VAULT = Path(
    os.environ.get(
        "VAULT_PATH",
        "/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault",
    )
)
BACKUP_DIR = Path(
    os.environ.get(
        "BACKUP_DIR",
        "/data/dispatcher-temp/vault_summarizer_backups",
    )
)
PROGRESS_FILE = Path(
    os.environ.get(
        "PROGRESS_FILE",
        "/data/dispatcher-temp/vault_summarizer_progress.json",
    )
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MIN_RATIO = 0.25
MIN_ORIGINAL_CHARS = 300
LANG_CONFIDENCE = 0.90

SKIP_FILENAMES = {
    "guide.md",
    "VAULT_FRONTMATTER_SPEC.md",
    "ENZYME_GUIDE.md",
    "MEMORY.md",
    "CLAUDE.md",
}

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────


def detect_language(text: str) -> str:
    """Gibt ISO-Sprachcode zurück ('it', 'de', …). Wirft ValueError wenn unsicher."""
    clean = text.strip()
    if len(clean) < 80:
        raise ValueError("Text zu kurz für Spracherkennung")
    results = detect_langs(clean[:3000])
    top = results[0]
    if top.prob < LANG_CONFIDENCE:
        raise ValueError(
            f"Spracherkennung unsicher: {top.lang} ({top.prob:.2f} < {LANG_CONFIDENCE})"
        )
    return top.lang


def strip_frontmatter(content: str) -> tuple[str, str]:
    """Trennt YAML-Frontmatter vom Textinhalt. Gibt (frontmatter, body) zurück."""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm = content[: end + 4]
            body = content[end + 4 :].lstrip("\n")
            return fm, body
    return "", content


def ensure_original_in_frontmatter(fm: str, body: str) -> str:
    """Stellt sicher, dass die YAML-Frontmatter ein 'original:'-Feld enthält.
    Extrahiert den PDF-Dateinamen aus Wikilinks oder JSON-Dokument-Metadaten im Body."""
    if not fm:
        return fm

    # Prüfe ob original: bereits vorhanden
    if re.search(r"^original:", fm, re.MULTILINE):
        return fm

    pdf_name = None

    # 1. Wikilink [[...pdf]] im Body
    m = re.search(r"\[\[([^\]|]+\.pdf)\]\]", body)
    if m:
        pdf_name = m.group(1).strip()
    else:
        # 2. JSON "filename":"...pdf" im Body (Docling-Format)
        m = re.search(r'"filename"\s*:\s*"([^"]+\.pdf)"', body)
        if m:
            pdf_name = m.group(1).strip()

    if not pdf_name:
        return fm

    # original: vor dem schließenden --- einfügen
    lines = fm.split("\n")
    # Finde die letzte Zeile vor dem schließenden ---
    insert_at = len(lines) - 1  # vor dem letzten ---
    lines.insert(insert_at, f"original: {pdf_name}")
    return "\n".join(lines)


MAX_INPUT_CHARS = 6000  # Prompt-Limit für LLM (vermeidet Timeouts bei langen OCR-Dokumenten)


def clean_for_summarization(text: str) -> str:
    """Entfernt OCR-Artefakte und reduziert Rauschen vor der Zusammenfassung."""
    # <!-- image --> Tags entfernen
    text = re.sub(r"<!--\s*image\s*-->", "", text, flags=re.IGNORECASE)
    # Zeilen die nur 1-3 Zeichen enthalten (OCR-Artefakte) entfernen
    lines = text.split("\n")
    cleaned = [l for l in lines if len(l.strip()) > 3 or l.strip() == ""]
    text = "\n".join(cleaned)
    # Mehrfache Leerzeilen auf maximal 2 reduzieren
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    # Auf MAX_INPUT_CHARS kürzen — relevanter Inhalt steht bei OCR-Docs vorne
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS] + "\n\n[Dokument gekürzt]"
    return text


def ollama_generate(prompt: str, model: str, timeout: int = 240) -> str:
    """Ruft Ollama API auf und gibt die Antwort zurück."""
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": 8192},
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("response", "").strip()


def summarize_german(text: str, model: str, min_chars: int) -> str:
    """Fasst deutschen (oder sonstigen) Text auf Deutsch zusammen."""
    prompt = f"""Erstelle eine strukturierte Zusammenfassung des folgenden Dokuments auf Deutsch.

Anforderungen:
- Mindestlänge: {min_chars} Zeichen
- Enthalte alle wichtigen Schlüsselwörter, Namen, Daten und Beträge
- Verständliche Zusammenfassung, kein Aufzählen von Rohdaten
- Kein einleitender Satz wie "Hier ist die Zusammenfassung"

Dokument:
{text}

Zusammenfassung:"""
    return ollama_generate(prompt, model)


def summarize_italian_then_translate(text: str, model: str, min_chars: int) -> str:
    """Fasst italienischen Text zuerst auf Italienisch zusammen, dann übersetzt ins Deutsche."""
    # Schritt 1: Zusammenfassung auf Italienisch
    prompt_it = f"""Crea un riassunto strutturato del seguente documento in italiano.

Requisiti:
- Lunghezza minima: {min_chars} caratteri
- Includi tutte le parole chiave importanti, nomi, date e importi
- Riassunto comprensibile, non elencare solo dati grezzi
- Nessuna frase introduttiva come "Ecco il riassunto"

Documento:
{text}

Riassunto:"""
    summary_it = ollama_generate(prompt_it, model)

    # Schritt 2: Übersetzung ins Deutsche
    prompt_de = f"""Übersetze den folgenden italienischen Text vollständig und präzise ins Deutsche.
Behalte alle Zahlen, Daten, Namen und Fachbegriffe bei.
Kein einleitender Satz.

Italienischer Text:
{summary_it}

Deutsche Übersetzung:"""
    summary_de = ollama_generate(prompt_de, model)
    return summary_de + "\n\n---\n\n*Originalzusammenfassung (Italiano):*\n\n" + summary_it


def generate_summary(body: str, lang: str, model: str, original_len: int) -> str:
    """Erzeugt eine Zusammenfassung mit Längenprüfung und einem Retry."""
    clean_body = clean_for_summarization(body)
    # min_chars basiert auf dem bereinigten Text, nicht dem Rohdokument (das viel OCR-Rauschen enthält)
    min_chars = max(int(len(clean_body) * MIN_RATIO), 100)

    for attempt in range(2):
        if lang == "it":
            summary = summarize_italian_then_translate(clean_body, model, min_chars)
        else:
            summary = summarize_german(clean_body, model, min_chars)

        if len(summary) >= min_chars:
            return summary

        if attempt == 0:
            # Retry mit explizitem Hinweis
            clean_body = (
                f"[Hinweis: Die Zusammenfassung muss mindestens {min_chars} Zeichen haben]\n\n"
                + clean_body
            )

    # Nach Retry: Zusammenfassung zurückgeben auch wenn zu kurz (besser als nix)
    return summary


# ── Progress-Tracking ─────────────────────────────────────────────────────────


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2))


# ── Dateiverarbeitung ─────────────────────────────────────────────────────────


def process_file(
    md_path: Path, model: str, test_mode: bool, progress: dict
) -> dict:
    """Verarbeitet eine einzelne .md-Datei. Gibt Status-Dict zurück."""
    rel = str(md_path.relative_to(VAULT))
    ts = datetime.now().isoformat(timespec="seconds")

    # Skip: bereits verarbeitet
    if not test_mode and rel in progress and progress[rel].get("status") == "done":
        return {"status": "already_done", "file": rel}

    content = md_path.read_text(encoding="utf-8", errors="replace")
    original_len = len(content)

    # Skip: Dateiname in Blacklist
    if md_path.name in SKIP_FILENAMES:
        if test_mode:
            print(f"SKIP: Dateiname in Blacklist ({md_path.name})")
        return {"status": "skipped_blacklist", "file": rel}

    # Skip: bereits zusammengefasst
    if "<!-- summarized" in content:
        if test_mode:
            print(f"SKIP: bereits zusammengefasst")
        return {"status": "skipped_already_summarized", "file": rel}

    # Frontmatter trennen
    frontmatter, body = strip_frontmatter(content)

    # PDF-Referenz aus Body retten bevor der Body durch Summary ersetzt wird
    frontmatter = ensure_original_in_frontmatter(frontmatter, body)

    clean_body = clean_for_summarization(body)

    if test_mode:
        print(f"Body: {len(body)} Zeichen, Clean: {len(clean_body)} Zeichen, Original: {original_len} Zeichen")

    # Skip: zu kurz
    if len(clean_body) < MIN_ORIGINAL_CHARS:
        if test_mode:
            print(f"SKIP: zu kurz ({len(clean_body)} < {MIN_ORIGINAL_CHARS})")
        result = {
            "status": "skipped_short",
            "file": rel,
            "original_len": original_len,
            "timestamp": ts,
        }
        if not test_mode:
            progress[rel] = result
        return result

    # Sprache erkennen
    try:
        lang = detect_language(clean_body)
        if test_mode:
            print(f"Sprache erkannt: {lang}")
    except (ValueError, LangDetectException) as e:
        if test_mode:
            print(f"SKIP: Spracherkennung fehlgeschlagen — {e}")
        result = {
            "status": "skipped_lang_uncertain",
            "file": rel,
            "reason": str(e),
            "original_len": original_len,
            "timestamp": ts,
        }
        if not test_mode:
            progress[rel] = result
        return result

    # Zusammenfassung generieren
    try:
        t0 = time.time()
        summary = generate_summary(clean_body, lang, model, original_len)
        elapsed = round(time.time() - t0, 1)
    except Exception as e:
        if test_mode:
            print(f"FEHLER bei Zusammenfassung: {e}")
        result = {
            "status": "error",
            "file": rel,
            "error": str(e),
            "original_len": original_len,
            "timestamp": ts,
        }
        if not test_mode:
            progress[rel] = result
        return result

    summary_len = len(summary)
    ratio = round(summary_len / original_len, 3) if original_len else 0

    result = {
        "status": "done",
        "file": rel,
        "original_len": original_len,
        "summary_len": summary_len,
        "ratio": ratio,
        "lang": lang,
        "elapsed_s": elapsed,
        "timestamp": ts,
    }

    if test_mode:
        # Im Test-Modus: nur anzeigen, nicht schreiben
        min_target = max(int(len(clean_body) * MIN_RATIO), 100)
        print(f"\n{'='*60}")
        print(f"Datei:    {rel}")
        print(f"Sprache:  {lang} (langdetect)")
        print(f"Original: {original_len} Zeichen (bereinigt: {len(clean_body)})")
        print(f"Summary:  {summary_len} Zeichen (Ratio: {ratio:.1%} von Original)")
        print(f"Zeit:     {elapsed}s")
        print(f"Ziel:     ≥ {min_target} Zeichen (25% von bereinigt) — {'OK' if summary_len >= min_target else 'ZU KURZ'}")
        print(f"{'='*60}")
        print(summary)
        print(f"{'='*60}\n")
        return result

    # Backup anlegen (einmalig)
    backup_path = BACKUP_DIR / (rel + ".original")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if not backup_path.exists():
        backup_path.write_text(content, encoding="utf-8")

    # Datei überschreiben
    new_content = (
        (frontmatter + "\n" if frontmatter else "")
        + f"<!-- summarized by vault_summarizer {ts} lang={lang} ratio={ratio:.2f} -->\n\n"
        + summary
    )
    md_path.write_text(new_content, encoding="utf-8")

    progress[rel] = result
    return result


# ── Hauptprogramm ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Vault Summarizer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", metavar="FILE", help="Einzeldatei testen (kein Schreiben)")
    group.add_argument("--run", action="store_true", help="Alle Dateien verarbeiten")
    parser.add_argument("--model", default="qwen3:4b-instruct", help="Ollama-Modell")
    parser.add_argument("--limit", type=int, default=0, help="Max. Dateien (0 = alle)")
    args = parser.parse_args()

    print(f"Modell: {args.model}")
    print(f"Ollama: {OLLAMA_URL}")
    print(f"Vault:  {VAULT}")

    if args.test:
        md_path = Path(args.test)
        if not md_path.exists():
            # Versuche relativen Pfad ab Vault
            md_path = VAULT / args.test
        if not md_path.exists():
            print(f"Fehler: Datei nicht gefunden: {args.test}")
            sys.exit(1)
        process_file(md_path, args.model, test_mode=True, progress={})
        return

    # --run: Alle .md-Dateien
    progress = load_progress()
    md_files = sorted(VAULT.rglob("*.md"))
    if args.limit:
        md_files = md_files[: args.limit]

    stats = {"done": 0, "skipped_short": 0, "skipped_lang_uncertain": 0,
             "skipped_blacklist": 0, "skipped_already_summarized": 0,
             "already_done": 0, "error": 0}
    total = len(md_files)

    for i, md_path in enumerate(md_files, 1):
        print(f"[{i}/{total}] {md_path.relative_to(VAULT)}", end=" ... ", flush=True)
        result = process_file(md_path, args.model, test_mode=False, progress=progress)
        status = result.get("status", "error")
        stats[status] = stats.get(status, 0) + 1

        if status == "done":
            r = result.get("ratio", 0)
            print(f"OK ({r:.0%}, {result.get('elapsed_s', '?')}s)")
        else:
            print(status)

        if i % 10 == 0:
            save_progress(progress)

    save_progress(progress)

    print(f"\n{'='*50}")
    print(f"Fertig. {total} Dateien verarbeitet.")
    for k, v in stats.items():
        if v:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
