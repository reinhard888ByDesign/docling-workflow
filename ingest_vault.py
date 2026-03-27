#!/usr/bin/env python3
"""Ingest Converted/*.md files into Open WebUI Knowledge base (Qdrant backend)."""

import os
import sys
import json
import requests
from pathlib import Path

API_BASE = os.environ.get("WEBUI_URL", "http://localhost:3000")
API_KEY = os.environ.get("WEBUI_API_KEY", "sk-6733607e160c777c1cd1315d4aa86f200ad2d11dfe19a2870ba625fcfa99d0c7")
VAULT = Path(os.environ.get("VAULT_PATH", "/home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault/Converted"))
KNOWLEDGE_NAME = "Vault"

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def get_or_create_knowledge():
    r = requests.get(f"{API_BASE}/api/v1/knowledge/", headers=HEADERS)
    r.raise_for_status()
    items = r.json().get("items", [])
    for item in items:
        if item["name"] == KNOWLEDGE_NAME:
            print(f"Using existing knowledge base: {item['id']}")
            return item["id"]
    r = requests.post(f"{API_BASE}/api/v1/knowledge/create", headers=HEADERS,
                      json={"name": KNOWLEDGE_NAME, "description": "Persönliche Dokumente aus dem Vault"})
    r.raise_for_status()
    kb_id = r.json()["id"]
    print(f"Created knowledge base: {kb_id}")
    return kb_id


def upload_file(kb_id, md_file):
    content = md_file.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        print(f"  SKIP (empty): {md_file.name}")
        return

    # Upload file
    r = requests.post(
        f"{API_BASE}/api/v1/files/",
        headers=HEADERS,
        files={"file": (md_file.name, content.encode("utf-8"), "text/plain")},
    )
    if not r.ok:
        print(f"  ERROR upload {md_file.name}: {r.status_code} {r.text[:200]}")
        return
    file_id = r.json()["id"]

    # Add file to knowledge base
    r2 = requests.post(
        f"{API_BASE}/api/v1/knowledge/{kb_id}/file/add",
        headers=HEADERS,
        json={"file_id": file_id},
    )
    if not r2.ok:
        print(f"  ERROR add-to-kb {md_file.name}: {r2.status_code} {r2.text[:200]}")
        return

    print(f"  OK: {md_file.name}")


def main():
    kb_id = get_or_create_knowledge()
    md_files = sorted(VAULT.glob("*.md"))
    print(f"Ingesting {len(md_files)} files from {VAULT}")
    for f in md_files:
        upload_file(kb_id, f)
    print("Done.")


if __name__ == "__main__":
    main()
