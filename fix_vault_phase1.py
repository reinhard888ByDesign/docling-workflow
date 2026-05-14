#!/usr/bin/env python3
"""Phase-1-Fixes für den Vault.
1. Broken YAML (tags: [] + nachfolgende Tags)
2. Kategorie-Normalisierung
"""
import re
from pathlib import Path

VAULT = Path("/home/reinhard/docker/RYZEN - docling-workflow/syncthing/data/reinhards-vault")

# ── 1. Broken YAML ─────────────────────────────────────────────────────────────

BROKEN_TAGS_RE = re.compile(
    r'tags: \[\]\n\n((?:  - [^\n]*\n)*  - [^\n]*)---',
    re.MULTILINE
)

def fix_broken_tags(text: str) -> tuple[str, bool]:
    def replacer(m: re.Match) -> str:
        block = m.group(1)
        lines = [l.strip() for l in block.splitlines() if l.strip().startswith('- ')]
        result = 'tags:\n'
        for line in lines:
            tag = line[2:].strip()  # remove '- '
            result += f'  - {tag}\n'
        result += '---'
        return result

    new = BROKEN_TAGS_RE.sub(replacer, text)
    return new, new != text

# ── 2. Kategorie-Normalisierung ────────────────────────────────────────────────

# (alt_pattern, canonical) — alt_pattern matches the raw value after "kategorie: "
KATEGORIE_MAP = [
    # Finanzen
    (re.compile(r'^kategorie:\s*["\']?Finanzen["\']?\s*$', re.M), 'kategorie: finanzen'),
    # Archiv
    (re.compile(r'^kategorie:\s*["\']?Archiv["\']?\s*$', re.M),   'kategorie: archiv'),
    # Fahrzeuge
    (re.compile(r'^kategorie:\s*["\']?Fahrzeuge["\']?\s*$', re.M), 'kategorie: fahrzeuge'),
    # Familie
    (re.compile(r'^kategorie:\s*["\']?Familie["\']?\s*$', re.M),   'kategorie: familie'),
    # FengShui
    (re.compile(r'^kategorie:\s*["\']?FengShui["\']?\s*$', re.M),  'kategorie: fengshui'),
    # Immobilien eigen
    (re.compile(r'^kategorie:\s*["\']?Immobilien eigen["\']?\s*$', re.M), 'kategorie: immobilien_eigen'),
    # Immobilien vermietet
    (re.compile(r'^kategorie:\s*["\']?Immobilien vermietet["\']?\s*$', re.M), 'kategorie: immobilien_vermietet'),
    # Krankenversicherung
    (re.compile(r'^kategorie:\s*["\']?Krankenversicherung["\']?\s*$', re.M), 'kategorie: krankenversicherung'),
    # Persönlich variants
    (re.compile(r'^kategorie:\s*["\']?Persönliches?["\']?\s*$', re.M), 'kategorie: persoenlich'),
    # Business
    (re.compile(r'^kategorie:\s*["\']?Business["\']?\s*$', re.M),  'kategorie: business'),
    # Digitales
    (re.compile(r'^kategorie:\s*["\']?Digitales["\']?\s*$', re.M), 'kategorie: digitales'),
    # Wissen
    (re.compile(r'^kategorie:\s*["\']?Wissen["\']?\s*$', re.M),    'kategorie: wissen'),
    # Reisen
    (re.compile(r'^kategorie:\s*["\']?Reisen["\']?\s*$', re.M),    'kategorie: reisen'),
    # Italien
    (re.compile(r'^kategorie:\s*["\']?Italien["\']?\s*$', re.M),   'kategorie: italien'),
    # Versicherungsdokument → finanzen
    (re.compile(r'^kategorie:\s*["\']?Versicherungsdokument["\']?\s*$', re.M), 'kategorie: finanzen'),
]

def normalize_kategorie(text: str) -> tuple[str, bool]:
    changed = False
    for pattern, replacement in KATEGORIE_MAP:
        new = pattern.sub(replacement, text)
        if new != text:
            text = new
            changed = True
    return text, changed

# ── Main ───────────────────────────────────────────────────────────────────────

def process_vault():
    fixed_yaml = 0
    fixed_kat  = 0
    errors     = 0

    for md in VAULT.rglob('*.md'):
        try:
            text = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f'[ERR] lesen {md.relative_to(VAULT)}: {e}')
            errors += 1
            continue

        original = text
        changed = False

        text, c = fix_broken_tags(text)
        if c:
            fixed_yaml += 1
            changed = True

        text, c = normalize_kategorie(text)
        if c:
            fixed_kat += 1
            changed = True

        if changed:
            try:
                md.write_text(text, encoding='utf-8')
            except Exception as e:
                print(f'[ERR] schreiben {md.relative_to(VAULT)}: {e}')
                errors += 1

    print(f'Broken YAML repariert:    {fixed_yaml}')
    print(f'Kategorie normalisiert:   {fixed_kat}')
    print(f'Fehler:                   {errors}')

if __name__ == '__main__':
    process_vault()
