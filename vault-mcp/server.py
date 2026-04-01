#!/usr/bin/env python3
"""vault-mcp: MCP Server für Obsidian Vault Search.

Tools:
  search_vault      — enzyme semantische Suche in Reinhards Vault (Lebensverwaltung)
  grep_vault        — Volltextsuche (ripgrep) über alle Vaults
  search_qdrant     — RAG Suche in silo-spezifischen Qdrant/Open-WebUI Collections
  get_document      — Volltext einer Datei lesen
  vault_stats       — Statistiken pro Silo
"""

import json
import os
import subprocess
import asyncio
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ── Konfiguration ──────────────────────────────────────────────────────────────

REINHARDS_VAULT = Path(os.environ.get(
    "REINHARDS_VAULT",
    "/home/reinhard/docker/docling-workflow/syncthing/data/reinhards-vault"
))
CONVERTED_VAULT = Path(os.environ.get(
    "CONVERTED_VAULT",
    "/home/reinhard/docker/docling-workflow/syncthing/data/obsidian-vault/Converted"
))
PROJEKTE_VAULT = Path(os.environ.get(
    "PROJEKTE_VAULT",
    "/home/reinhard/docker/docling-workflow/syncthing/data/projekte"
))
ENZYME_BIN = os.environ.get("ENZYME_BIN", "/home/reinhard/.local/bin/enzyme")
WEBUI_URL = os.environ.get("WEBUI_URL", "http://localhost:3000")
WEBUI_API_KEY = os.environ.get("WEBUI_API_KEY", "")

SILOS = ["finanzen", "krankenkasse", "anleitungen", "archiv", "projekte", "inbox"]

FRONTMATTER_FIELDS = ["datum", "absender", "thema", "betrag", "zusammenfassung", "kategorie"]

# ── MCP Server ─────────────────────────────────────────────────────────────────

server = Server("vault-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_vault",
            description=(
                "Semantische Suche in Reinhards persönlichem Lebens-Vault "
                "(Finanzen, Familie, Projekte, Reisen etc.) via enzyme. "
                "Gut für konzeptuelle Fragen und Themennavigation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Suchanfrage"},
                    "limit": {"type": "integer", "default": 5, "description": "Max Ergebnisse"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="grep_vault",
            description=(
                "Volltextsuche (ripgrep) in allen Vault-Ordnern. "
                "Gut für exakte Begriffe, Namen, Beträge, Datum-Suche. "
                "Optional auf einen Silo oder Vault einschränken."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex oder Suchbegriff"},
                    "silo": {
                        "type": "string",
                        "enum": SILOS + ["reinhards-vault", "projekte", "all"],
                        "default": "all",
                        "description": "Vault-Bereich einschränken",
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["pattern"],
            },
        ),
        types.Tool(
            name="search_qdrant",
            description=(
                "RAG-Suche in den Open-WebUI Knowledge Collections (Converted-Vault: "
                "gescannte und per Docling konvertierte Dokumente). "
                "Gut für Dokumenteninhalte: Rechnungen, Arztbriefe, Anleitungen."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Suchanfrage"},
                    "silo": {
                        "type": "string",
                        "enum": SILOS[:-1] + ["all"],
                        "default": "all",
                        "description": "Silo/Collection: finanzen | krankenkasse | anleitungen | archiv | projekte | all",
                    },
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_document",
            description="Liest den vollständigen Inhalt einer Vault-Datei.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad ab Vault-Root oder absoluter Pfad",
                    },
                    "vault": {
                        "type": "string",
                        "enum": ["reinhards", "converted", "projekte"],
                        "default": "converted",
                        "description": "Welcher Vault-Bereich",
                    },
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="list_documents",
            description=(
                "Listet Dokumente in einem Silo auf, optional gefiltert nach Jahr und/oder Kategorie. "
                "Gibt Metadaten (Datum, Absender, Betrag, Zusammenfassung) zurück — ideal für "
                "'Welche Dokumente gibt es aus Jahr X?' oder 'Alle Rechnungen von Absender Y?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "silo": {
                        "type": "string",
                        "enum": SILOS,
                        "description": "Silo: finanzen | krankenkasse | anleitungen | archiv | projekte | inbox",
                    },
                    "year": {
                        "type": "string",
                        "description": "Jahresfilter, z.B. '2025'. Optional.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Unterordner-Filter z.B. 'versicherung', 'arztrechnung', 'rezept'. Optional.",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["silo"],
            },
        ),
        types.Tool(
            name="vault_stats",
            description="Zeigt Statistiken pro Silo (Anzahl Dateien, letzte Änderung).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


# ── Tool Implementations ───────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "search_vault":
        return await tool_search_vault(arguments)
    elif name == "grep_vault":
        return await tool_grep_vault(arguments)
    elif name == "search_qdrant":
        return await tool_search_qdrant(arguments)
    elif name == "get_document":
        return await tool_get_document(arguments)
    elif name == "list_documents":
        return await tool_list_documents(arguments)
    elif name == "vault_stats":
        return await tool_vault_stats(arguments)
    else:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def tool_search_vault(args: dict) -> list[types.TextContent]:
    query = args["query"]
    limit = args.get("limit", 5)

    try:
        result = subprocess.run(
            [ENZYME_BIN, "catalyze", "-p", str(REINHARDS_VAULT), query],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return [types.TextContent(type="text", text=f"enzyme error: {result.stderr[:200]}")]

        data = json.loads(result.stdout)
        results = data.get("results", [])[:limit]

        if not results:
            return [types.TextContent(type="text", text="Keine Ergebnisse gefunden.")]

        lines = [f"**enzyme Suche**: '{query}' — {len(results)} Ergebnisse\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"**{i}. {r['file_path']}** (Score: {r['similarity']:.2f})")
            lines.append(r["content"][:300].strip())
            lines.append("")

        return [types.TextContent(type="text", text="\n".join(lines))]

    except subprocess.TimeoutExpired:
        return [types.TextContent(type="text", text="Timeout bei enzyme-Suche.")]
    except json.JSONDecodeError as e:
        return [types.TextContent(type="text", text=f"JSON parse error: {e}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Fehler: {e}")]


async def tool_grep_vault(args: dict) -> list[types.TextContent]:
    pattern = args["pattern"]
    silo = args.get("silo", "all")
    case_sensitive = args.get("case_sensitive", False)
    limit = args.get("limit", 20)

    # Determine search paths
    if silo == "all":
        paths = [CONVERTED_VAULT, REINHARDS_VAULT, PROJEKTE_VAULT]
    elif silo == "reinhards-vault":
        paths = [REINHARDS_VAULT]
    elif silo == "projekte":
        paths = [PROJEKTE_VAULT]
    else:
        paths = [CONVERTED_VAULT / silo]

    cmd = ["rg", "--no-heading", "-n", "--max-count=3"]
    if not case_sensitive:
        cmd.append("-i")
    cmd.append(pattern)
    cmd.extend(str(p) for p in paths if p.exists())

    if not cmd[-1:] or not any(p.exists() for p in paths):
        return [types.TextContent(type="text", text=f"Pfad nicht gefunden für silo: {silo}")]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = result.stdout.strip()

        if not output:
            return [types.TextContent(type="text", text=f"Keine Treffer für: '{pattern}'")]

        lines = output.split("\n")[:limit]
        summary = f"**grep_vault** '{pattern}' in [{silo}] — {len(lines)} Treffer\n\n"
        return [types.TextContent(type="text", text=summary + "\n".join(lines))]

    except subprocess.TimeoutExpired:
        return [types.TextContent(type="text", text="Timeout bei Suche.")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Fehler: {e}")]


async def tool_search_qdrant(args: dict) -> list[types.TextContent]:
    query = args["query"]
    silo = args.get("silo", "all")
    limit = args.get("limit", 5)

    if not WEBUI_API_KEY:
        return [types.TextContent(type="text", text="WEBUI_API_KEY nicht konfiguriert.")]

    import urllib.request, urllib.error

    collection_name = "vault-all" if silo == "all" else f"vault-{silo}"
    headers = {
        "Authorization": f"Bearer {WEBUI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Get knowledge collection ID
    try:
        req = urllib.request.Request(
            f"{WEBUI_URL}/api/v1/knowledge/",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        collections = {item["name"]: item["id"] for item in data.get("items", [])}
        kb_id = collections.get(collection_name)
        if not kb_id:
            return [types.TextContent(type="text", text=f"Collection '{collection_name}' nicht gefunden.")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Knowledge API Fehler: {e}")]

    # Query the knowledge collection
    try:
        payload = json.dumps({"query": query, "k": limit, "collection_names": [kb_id]}).encode()
        req = urllib.request.Request(
            f"{WEBUI_URL}/api/v1/retrieval/query/collection",
            data=payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return [types.TextContent(type="text", text=f"Query Fehler: {e}")]

    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]

    if not documents:
        return [types.TextContent(type="text", text=f"Keine Ergebnisse in '{collection_name}'.")]

    lines = [f"**RAG Suche**: '{query}' in [{collection_name}] — {len(documents)} Ergebnisse\n"]
    for i, (doc, meta) in enumerate(zip(documents, metadatas), 1):
        fname = meta.get("name", "?") if meta else "?"
        lines.append(f"**{i}. {fname}**")
        lines.append(doc[:400].strip())
        lines.append("")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def tool_get_document(args: dict) -> list[types.TextContent]:
    path_str = args["path"]
    vault = args.get("vault", "converted")

    vault_roots = {
        "reinhards": REINHARDS_VAULT,
        "converted": CONVERTED_VAULT,
        "projekte": PROJEKTE_VAULT,
    }
    root = vault_roots.get(vault, CONVERTED_VAULT)

    p = Path(path_str)
    if not p.is_absolute():
        p = root / p

    if not p.exists():
        return [types.TextContent(type="text", text=f"Datei nicht gefunden: {path_str}")]

    if not p.is_file():
        return [types.TextContent(type="text", text=f"Kein File: {path_str}")]

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return [types.TextContent(type="text", text=content)]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Lesefehler: {e}")]


async def tool_list_documents(args: dict) -> list[types.TextContent]:
    silo = args["silo"]
    year = args.get("year")
    category = args.get("category")
    limit = args.get("limit", 50)

    base = CONVERTED_VAULT / silo

    # Build search path
    if category and year:
        search_paths = [base / category / year]
    elif category:
        search_paths = [base / category]
    elif year:
        # Year can appear at different depths: silo/year/ or silo/category/year/
        search_paths = list(base.glob(f"*/{year}")) + list(base.glob(year))
        search_paths = [p for p in search_paths if p.is_dir()]
        if not search_paths:
            # Fallback: search all, filter by frontmatter datum
            search_paths = [base]
    else:
        search_paths = [base]

    if not search_paths or not any(p.exists() for p in search_paths):
        return [types.TextContent(type="text", text=f"Pfad nicht gefunden: silo={silo}, year={year}, category={category}")]

    # Collect markdown files
    files = []
    for sp in search_paths:
        if sp.exists():
            files.extend(sorted(sp.rglob("*.md")))

    files = files[:limit]

    if not files:
        return [types.TextContent(type="text", text=f"Keine Dokumente gefunden (silo={silo}, year={year}, category={category})")]

    def parse_frontmatter(path: Path) -> dict:
        """Extract YAML frontmatter fields quickly without full YAML parse."""
        meta = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end > 0:
                    fm = text[3:end]
                    for line in fm.splitlines():
                        for field in FRONTMATTER_FIELDS:
                            if line.startswith(f"{field}:"):
                                val = line[len(field)+1:].strip().strip('"')
                                meta[field] = val
                                break
        except Exception:
            pass
        return meta

    lines = [f"**list_documents** silo={silo}" +
             (f", year={year}" if year else "") +
             (f", category={category}" if category else "") +
             f" — {len(files)} Dokumente\n"]

    for f in files:
        meta = parse_frontmatter(f)
        rel = str(f.relative_to(CONVERTED_VAULT))
        parts = [f"- **{f.name}**"]
        if meta.get("datum"):
            parts.append(f"Datum: {meta['datum']}")
        if meta.get("absender"):
            parts.append(f"Von: {meta['absender']}")
        if meta.get("betrag"):
            parts.append(f"Betrag: {meta['betrag']}")
        if meta.get("zusammenfassung"):
            parts.append(f"→ {meta['zusammenfassung'][:120]}")
        parts.append(f"[{rel}]")
        lines.append("  ".join(parts))

    return [types.TextContent(type="text", text="\n".join(lines))]


async def tool_vault_stats(args: dict) -> list[types.TextContent]:
    lines = ["**Vault Statistiken**\n"]

    # Converted silos
    lines.append("**Converted/ (Docling-Vault)**")
    total_converted = 0
    for silo in SILOS:
        silo_dir = CONVERTED_VAULT / silo
        if silo_dir.exists():
            count = sum(1 for _ in silo_dir.rglob("*.md"))
            total_converted += count
            lines.append(f"  {silo}: {count} Dateien")
    lines.append(f"  **Gesamt: {total_converted}**")
    lines.append("")

    # Reinhards Vault
    if REINHARDS_VAULT.exists():
        count = sum(1 for _ in REINHARDS_VAULT.rglob("*.md")
                    if not any(p.startswith('.') for p in _.parts))
        lines.append(f"**Reinhards Vault**: {count} Dateien")
        # Top folders
        for sub in sorted(REINHARDS_VAULT.iterdir()):
            if sub.is_dir() and not sub.name.startswith('.'):
                c = sum(1 for _ in sub.rglob("*.md"))
                if c > 0:
                    lines.append(f"  {sub.name}: {c}")

    lines.append("")

    # Projekte Vault
    if PROJEKTE_VAULT.exists():
        count = sum(1 for _ in PROJEKTE_VAULT.rglob("*.md")
                    if not any(p.startswith('.') for p in _.parts))
        lines.append(f"**Projekte Vault (Pi/OpenClaw)**: {count} Dateien")

    return [types.TextContent(type="text", text="\n".join(lines))]


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
