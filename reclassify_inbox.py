#!/usr/bin/env python3
"""Reklassifizierung der 00 Inbox MDs via Keyword-Regeln + Ollama.

Für jede Inbox-MD:
1. Text aus MD-Body extrahieren
2. Keyword-Regeln (deterministisch, schnell) prüfen
3. Falls kein Match → Ollama LLM
4. MD in Zielordner verschieben + Frontmatter aktualisieren

Dry-run mit --dry-run, Debug mit --debug, Limit mit --limit N
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

import requests
import yaml

# ── Konfiguration ─────────────────────────────────────────────────────────────

VAULT     = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")
INBOX     = VAULT / "00 Inbox"
CFG_DIR   = Path("/home/reinhard/docker/RYZEN - docling-workflow/dispatcher-config")

OLLAMA_URL   = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:4b-instruct"
OLLAMA_CTX   = 8192
OLLAMA_TMOUT = 180

MIN_TEXT_LEN = 80   # MDs kürzer als dies → kein LLM, bleibt in Inbox

# ── YAML-Config laden ─────────────────────────────────────────────────────────

def load_categories() -> dict:
    data = yaml.safe_load((CFG_DIR / "categories.yaml").read_text("utf-8"))
    return data.get("categories", {})

def load_keyword_rules() -> list:
    data = yaml.safe_load((CFG_DIR / "categories.yaml").read_text("utf-8"))
    return data.get("keyword_rules", [])

# ── Frontmatter parsen ────────────────────────────────────────────────────────

FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

def split_fm(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    body = text[m.end():]
    return fm, body

def build_fm_str(fm: dict) -> str:
    """Schreibt Frontmatter als YAML-Block."""
    lines = ['---']
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f'{k}:')
            for item in v:
                lines.append(f'  - {item}')
        elif isinstance(v, str) and ('\n' in v or ':' in v or v.startswith('"')):
            lines.append(f'{k}: {json.dumps(v, ensure_ascii=False)}')
        elif v is None:
            lines.append(f'{k}:')
        else:
            lines.append(f'{k}: {v}')
    lines.append('---')
    return '\n'.join(lines) + '\n'

# ── Text extrahieren ──────────────────────────────────────────────────────────

def extract_text(body: str) -> str:
    """Bereinigt MD-Body für LLM: entfernt Wikilinks, HTML-Kommentare, Bildplatzhalter."""
    text = re.sub(r'<!--.*?-->', '', body, flags=re.DOTALL)
    text = re.sub(r'!\[\[.*?\]\]', '', text)
    text = re.sub(r'\[\[.*?\]\]', '', text)
    text = re.sub(r'📎.*', '', text)
    text = re.sub(r'#\w+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ── Keyword-Regeln ────────────────────────────────────────────────────────────

def keyword_classify(text: str, rules: list) -> str | None:
    # Nur in den ersten 3000 Zeichen suchen: Vermeidet False-Positives
    # in langen AGB-Texten (z.B. "Albergo" in Versicherungsbedingungen)
    head = text[:3000].lower()
    for rule in rules:
        kws = rule.get("keywords", [])
        alle = rule.get("alle_keywords", False)
        if alle:
            if all(kw.lower() in head for kw in kws):
                return rule["category_id"]
        else:
            if any(kw.lower() in head for kw in kws):
                return rule["category_id"]
    return None

# ── Ollama-Klassifikation ─────────────────────────────────────────────────────

def build_cat_desc(categories: dict) -> str:
    lines = []
    for cid, cfg in categories.items():
        lines.append(f'- {cid}: {cfg["label"]} — {cfg.get("description", "")}')
    return "\n".join(lines)

def classify_ollama(text: str, categories: dict) -> dict | None:
    cat_desc = build_cat_desc(categories)
    prompt = f"""Analysiere das folgende Dokument und klassifiziere es.
Das Dokument kann auf Deutsch oder Italienisch sein.

Verfügbare Kategorien:
{cat_desc}

Antworte NUR mit JSON:
{{
  "category_id": "<ID aus der Liste oben oder null>",
  "absender": "<Absender/Aussteller oder null>",
  "adressat": "Reinhard" | "Marion" | "Reinhard & Marion" | null,
  "rechnungsdatum": "<DD.MM.YYYY oder null>",
  "thema": "<kurze Beschreibung max 60 Zeichen oder null>"
}}

WICHTIG: category_id MUSS exakt eine ID aus der Liste sein oder null.
Falls kein Treffer: category_id=null → landet in Inbox.

Dokument:
{text[:5000]}"""

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_ctx": OLLAMA_CTX},
            },
            timeout=OLLAMA_TMOUT,
        )
        if not r.ok:
            return None
        raw = r.json().get("response", "")
        # Qwen3 Thinking-Tags entfernen
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        # JSON aus Antwort extrahieren
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group())
    except Exception as e:
        print(f"  [Ollama-Fehler] {e}", file=sys.stderr)
        return None

# ── Zielordner bestimmen ───────────────────────────────────────────────────────

def target_folder(category_id: str, categories: dict, fm: dict) -> Path | None:
    cat = categories.get(category_id)
    if not cat:
        return None
    base = VAULT / cat["vault_folder"]
    # Jahr-Unterordner: aus Datum-Feldern ableiten
    year = None
    for field in ("datum", "Datum_original", "rechnungsdatum", "date"):
        val = fm.get(field)
        if val:
            m = re.search(r'(\d{4})', str(val))
            if m and 1980 <= int(m.group(1)) <= 2030:
                year = m.group(1)
                break
    if year and (base / year).exists():
        return base / year
    return base

# ── Frontmatter updaten ───────────────────────────────────────────────────────

def update_fm(fm: dict, result: dict, category_id: str) -> dict:
    fm = dict(fm)
    fm["kategorie"] = category_id
    if result.get("absender") and not fm.get("absender"):
        fm["absender"] = result["absender"]
    if result.get("adressat") and not fm.get("adressat"):
        fm["adressat"] = result["adressat"]
    if result.get("thema") and not fm.get("thema"):
        fm["thema"] = result["thema"]
    if result.get("rechnungsdatum") and not fm.get("datum"):
        fm["datum"] = result["rechnungsdatum"]
    return fm

# ── Hauptlogik ────────────────────────────────────────────────────────────────

def process_inbox(dry_run: bool, limit: int, debug: bool):
    categories   = load_categories()
    kw_rules     = load_keyword_rules()

    # Alle Inbox MDs sammeln (root + Jahresordner 2020-2025 + Sonstige)
    all_mds = sorted(INBOX.rglob("*.md"))
    if limit:
        all_mds = all_mds[:limit]

    stats = {"total": len(all_mds), "moved": 0, "kw": 0, "llm": 0,
             "no_text": 0, "no_match": 0, "error": 0}

    print(f"{'[DRY-RUN] ' if dry_run else ''}Verarbeite {len(all_mds)} Inbox-MDs...\n")

    for i, md in enumerate(all_mds, 1):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[{i}/{stats['total']}] {md.name}: Lesefehler {e}")
            stats["error"] += 1
            continue

        fm, body = split_fm(text)
        clean = extract_text(body)

        if debug:
            print(f"[{i}/{stats['total']}] {md.name}")
            print(f"  Text ({len(clean)} Zeichen): {clean[:120]!r}")

        # Zu kurzer Text → skip
        if len(clean) < MIN_TEXT_LEN:
            print(f"[{i}/{stats['total']}] SKIP (zu kurz: {len(clean)} Z): {md.name}")
            stats["no_text"] += 1
            continue

        # 1. Keyword-Fast-Path
        category_id = keyword_classify(clean, kw_rules)
        method = "KW"
        result = {}

        # 2. Ollama-Fallback
        if not category_id:
            result = classify_ollama(clean, categories) or {}
            category_id = result.get("category_id")
            method = "LLM"
            if category_id:
                stats["llm"] += 1
            else:
                print(f"[{i}/{stats['total']}] NO_MATCH: {md.name}")
                stats["no_match"] += 1
                continue
        else:
            stats["kw"] += 1

        # Zielordner
        dst_dir = target_folder(category_id, categories, fm)
        if not dst_dir:
            print(f"[{i}/{stats['total']}] UNKNOWN_CAT {category_id!r}: {md.name}")
            stats["no_match"] += 1
            continue

        dst = dst_dir / md.name
        # Konflikt vermeiden
        if dst.exists():
            stem, suf = md.stem, md.suffix
            counter = 1
            while dst.exists():
                dst = dst_dir / f"{stem}_rx{counter}{suf}"
                counter += 1

        new_fm = update_fm(fm, result, category_id)
        new_text = build_fm_str(new_fm) + body

        label = categories[category_id]["label"]
        print(f"[{i}/{stats['total']}] [{method}] {label} → {dst.relative_to(VAULT)}  ({md.name})")

        if not dry_run:
            dst_dir.mkdir(parents=True, exist_ok=True)
            md.write_text(new_text, encoding="utf-8")
            shutil.move(str(md), str(dst))

        stats["moved"] += 1

    # Leere Jahresordner im Inbox aufräumen
    if not dry_run:
        for d in sorted(INBOX.iterdir()):
            if d.is_dir() and d.name.isdigit():
                try:
                    d.rmdir()
                except OSError:
                    pass  # noch nicht leer

    print(f"\n{'=' * 50}")
    print(f"Gesamt:      {stats['total']}")
    print(f"Verschoben:  {stats['moved']}")
    print(f"  Keyword:   {stats['kw']}")
    print(f"  Ollama:    {stats['llm']}")
    print(f"Kein Text:   {stats['no_text']}")
    print(f"Kein Match:  {stats['no_match']}")
    print(f"Fehler:      {stats['error']}")

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Nichts schreiben/verschieben")
    ap.add_argument("--limit",   type=int, default=0,  help="Nur N Dokumente")
    ap.add_argument("--debug",   action="store_true",  help="Text-Vorschau")
    args = ap.parse_args()
    process_inbox(dry_run=args.dry_run, limit=args.limit, debug=args.debug)
