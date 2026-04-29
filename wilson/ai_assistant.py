#!/usr/bin/env python3
"""
Wilson AI-Assistent — eigenständiger Telegram-Bot
Separater Bot-Token vom Dispatcher. DeepSeek + Projekte Vault (direkt) + Reinhards Vault (enzyme).
"""
import os, time, logging, sqlite3, requests, html, subprocess
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = int(os.environ.get("TELEGRAM_CHAT_ID", "8620231031"))
DEEPSEEK_KEY   = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL   = "https://api.deepseek.com/v1/chat/completions"
ENZYME_URL     = os.environ.get("ENZYME_URL", "http://192.168.86.195:11180")
PROJEKTE_VAULT = Path(os.environ.get("PROJEKTE_VAULT", os.path.expanduser("~/Vaults")))
POLL_TIMEOUT   = int(os.environ.get("POLL_TIMEOUT", "20"))
DB_PATH        = Path(os.environ.get("DB_PATH", os.path.expanduser("~/.openclaw/ai_assistant.db")))
MAX_HISTORY    = 20
MAX_FILE_CHARS = 3000   # Zeichen pro Datei im Kontext

# Dateien die immer als Kontext mitgegeben werden
CONTEXT_FILES  = ["AUFGABEN.md", "MEMORY.md", "USER.md"]
# Verzeichnisse/Muster die bei der Suche ignoriert werden
SKIP_DIRS      = {".openclaw", ".trash", ".obsidian", "__pycache__", "memory"}

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Projekte Vault ────────────────────────────────────────────────────────────
def _read_file(path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n… [gekürzt, {len(text)} Zeichen gesamt]"
        return text
    except Exception as e:
        return f"[Lesefehler: {e}]"

def load_context_files() -> str:
    """Lädt AUFGABEN.md, MEMORY.md, USER.md als Kontext-Block."""
    parts = []
    for name in CONTEXT_FILES:
        p = PROJEKTE_VAULT / name
        if p.exists():
            content = _read_file(p, max_chars=2000)
            parts.append(f"=== {name} ===\n{content}")
    return "\n\n".join(parts)

def vault_search(query: str, max_results: int = 6) -> str:
    """Sucht per grep im Projekte Vault (Dateiinhalt + Dateinamen)."""
    query_lower = query.lower()
    matches = []

    for md in PROJEKTE_VAULT.rglob("*.md"):
        # Ignorierte Verzeichnisse überspringen
        if any(skip in md.parts for skip in SKIP_DIRS):
            continue
        if md.name.startswith("._"):
            continue

        rel = md.relative_to(PROJEKTE_VAULT)
        in_name = query_lower in md.stem.lower()

        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        in_content = query_lower in content.lower()
        if not (in_name or in_content):
            continue

        # Snippet: erste Zeile die den Begriff enthält
        snippet = ""
        for line in content.splitlines():
            if query_lower in line.lower():
                snippet = line.strip()[:150]
                break

        matches.append((str(rel), snippet, in_name))

    if not matches:
        return f"🔍 Keine Treffer für <i>{html.escape(query)}</i> im Projekte Vault."

    # Titeltreffern Vorrang
    matches.sort(key=lambda x: (0 if x[2] else 1, x[0]))
    lines = [f"🔍 <b>Projekte Vault: {html.escape(query)}</b>"]
    for rel, snippet, _ in matches[:max_results]:
        name = Path(rel).stem
        lines.append(f"\n📄 <b>{html.escape(name)}</b>\n<code>{html.escape(rel)}</code>")
        if snippet:
            lines.append(f"<i>{html.escape(snippet)}</i>")
    if len(matches) > max_results:
        lines.append(f"\n… und {len(matches) - max_results} weitere Treffer.")
    return "\n".join(lines)

def vault_read(query: str) -> str:
    """Liest eine Datei aus dem Projekte Vault anhand von Namens-Teilstring."""
    query_lower = query.lower().strip("/")
    candidates = []
    for md in PROJEKTE_VAULT.rglob("*.md"):
        if any(skip in md.parts for skip in SKIP_DIRS):
            continue
        if md.name.startswith("._"):
            continue
        rel = str(md.relative_to(PROJEKTE_VAULT))
        if query_lower in rel.lower():
            candidates.append(md)

    if not candidates:
        return f"⚠️ Keine Datei gefunden für: <i>{html.escape(query)}</i>"
    if len(candidates) > 1:
        names = "\n".join(f"• <code>{html.escape(str(c.relative_to(PROJEKTE_VAULT)))}</code>"
                          for c in candidates[:8])
        return f"Mehrere Treffer — bitte präzisieren:\n{names}"

    md = candidates[0]
    rel = md.relative_to(PROJEKTE_VAULT)
    content = _read_file(md)
    return f"📄 <b>{html.escape(md.stem)}</b>\n<code>{html.escape(str(rel))}</code>\n\n{html.escape(content)}"

def vault_list() -> str:
    """Zeigt Struktur des Projekte Vault."""
    lines = ["📁 <b>Projekte Vault – Übersicht</b>\n"]
    # Top-Level .md
    top = sorted(p for p in PROJEKTE_VAULT.glob("*.md") if not p.name.startswith("._"))
    if top:
        lines.append("<b>Hauptdateien:</b>")
        for p in top:
            lines.append(f"• {html.escape(p.stem)}")
    # Themen-Verzeichnis
    themen = PROJEKTE_VAULT / "Themen"
    if themen.exists():
        lines.append("\n<b>Themen:</b>")
        for sub in sorted(themen.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                count = len(list(sub.rglob("*.md")))
                lines.append(f"• {html.escape(sub.name)} ({count} Dateien)")
    return "\n".join(lines)

# ── enzyme (Reinhards Vault) ──────────────────────────────────────────────────
def enzyme_catalyze(query: str, limit: int = 5) -> str:
    try:
        r = requests.post(f"{ENZYME_URL}/catalyze",
                          json={"query": query, "limit": limit}, timeout=25)
        if r.status_code != 200:
            return f"⚠️ enzyme HTTP {r.status_code}"
        results = r.json().get("results", [])
        if not results:
            return f"🔍 Keine Treffer für <i>{html.escape(query)}</i> in Reinhards Vault."
        lines = [f"🔍 <b>Reinhards Vault: {html.escape(query)}</b>"]
        for i, res in enumerate(results[:limit], 1):
            title = Path(res.get("file_path", "?")).stem
            snippet = (res.get("content") or "")[:150].replace("\n", " ").strip()
            sim = res.get("similarity", 0)
            lines.append(f"\n{i}. <b>{html.escape(title)}</b> ({sim:.0%})\n<i>{html.escape(snippet)}</i>")
        return "\n".join(lines)
    except requests.ConnectionError:
        return "⚠️ enzyme nicht erreichbar (192.168.86.195:11180)"
    except Exception as e:
        return f"⚠️ Suche fehlgeschlagen: {html.escape(str(e))}"

def enzyme_petri() -> str:
    try:
        r = requests.post(f"{ENZYME_URL}/petri", json={"top": 8}, timeout=20)
        if r.status_code != 200:
            return f"⚠️ enzyme HTTP {r.status_code}"
        data = r.json()
        entities = data if isinstance(data, list) else data.get("entities", data.get("results", []))
        if not entities:
            return "Keine aktiven Themen gefunden."
        lines = ["🌱 <b>Aktive Themen – Reinhards Vault</b>"]
        for e in entities[:8]:
            lines.append(f"• <b>{html.escape(str(e.get('name','?')))}</b>")
        return "\n".join(lines)
    except requests.ConnectionError:
        return "⚠️ enzyme nicht erreichbar"
    except Exception as e:
        return f"⚠️ petri fehlgeschlagen: {html.escape(str(e))}"

# ── SQLite ───────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role    TEXT,
            content TEXT,
            ts      REAL
        );
        CREATE TABLE IF NOT EXISTS tg_offset (
            id  INTEGER PRIMARY KEY,
            val INTEGER
        );
    """)
    con.commit()
    return con

def get_offset(con): return (con.execute("SELECT val FROM tg_offset WHERE id=1").fetchone() or (0,))[0]
def set_offset(con, val): con.execute("INSERT OR REPLACE INTO tg_offset(id,val) VALUES(1,?)", (val,)); con.commit()

def history_get(con, chat_id):
    rows = con.execute("SELECT role,content FROM history WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
                       (chat_id, MAX_HISTORY)).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def history_add(con, chat_id, role, content):
    con.execute("INSERT INTO history(chat_id,role,content,ts) VALUES(?,?,?,?)",
                (chat_id, role, content, time.time()))
    con.execute("DELETE FROM history WHERE chat_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE chat_id=? ORDER BY ts DESC LIMIT ?)",
                (chat_id, chat_id, MAX_HISTORY * 2))
    con.commit()

def history_clear(con, chat_id):
    con.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    con.commit()

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(text: str, parse_mode: str = "HTML"):
    # Telegram-Limit: 4096 Zeichen
    if len(text) > 4000:
        text = text[:4000] + "\n… [gekürzt]"
    try:
        requests.post(f"{TG_BASE}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
                      timeout=15)
    except Exception as e:
        log.error("tg_send: %s", e)

def tg_typing():
    try:
        requests.post(f"{TG_BASE}/sendChatAction",
                      json={"chat_id": CHAT_ID, "action": "typing"}, timeout=5)
    except Exception:
        pass

def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{TG_BASE}/getUpdates",
                         params={"offset": offset, "timeout": POLL_TIMEOUT, "limit": 10},
                         timeout=POLL_TIMEOUT + 10)
        return r.json().get("result", [])
    except Exception as e:
        log.warning("getUpdates: %s", e)
        time.sleep(5)
        return []

# ── DeepSeek ──────────────────────────────────────────────────────────────────
def build_system_prompt() -> str:
    base = (
        "Du bist Reinhards persönlicher KI-Assistent auf Wilson (Raspberry Pi). "
        "Reinhard verwaltet Immobilien in Deutschland und Italien (Podere dei Venti in Seggiano), "
        "betreibt einen KI-Dokumentenworkflow und nutzt Home Assistant. "
        "Antworte präzise und auf Deutsch, außer der Nutzer wechselt die Sprache.\n\n"
        "Verfügbare Befehle für den Nutzer: /suche (Projekte Vault), "
        "/dokumente (Reinhards Vault via enzyme), /lese <datei>, /aufgaben, /vault, /themen, /reset.\n\n"
    )
    ctx = load_context_files()
    if ctx:
        base += "── Aktueller Kontext aus dem Projekte Vault ──\n" + ctx
    return base

def ask_deepseek(messages: list, system: str) -> str:
    if not DEEPSEEK_KEY:
        return "⚠️ Kein DeepSeek API-Key konfiguriert."
    try:
        r = requests.post(
            DEEPSEEK_URL,
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "system", "content": system}] + messages,
                "max_tokens": 1500,
                "temperature": 0.7,
            },
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except requests.HTTPError as e:
        return f"⚠️ DeepSeek-Fehler: {e}"
    except Exception as e:
        return f"⚠️ Fehler: {html.escape(str(e))}"

# ── Help ──────────────────────────────────────────────────────────────────────
HELP_TEXT = """\
🤖 <b>Wilson AI-Assistent</b>

<b>Projekte Vault (Wilson, direkt):</b>
/suche &lt;Begriff&gt; — Volltextsuche
/lese &lt;dateiname&gt; — Datei lesen
/aufgaben — AUFGABEN.md anzeigen
/vault — Vault-Struktur anzeigen

<b>Reinhards Vault (Ryzen, enzyme):</b>
/dokumente &lt;Begriff&gt; — Dokumentensuche
/themen — aktive Themen (enzyme petri)

<b>Chat:</b>
Freier Text → DeepSeek (kennt AUFGABEN + MEMORY als Kontext)
/reset — Gesprächsverlauf löschen
/hilfe — diese Hilfe\
"""

# ── Message dispatch ──────────────────────────────────────────────────────────
def handle(con, msg: dict):
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    if not text or chat_id != CHAT_ID:
        return

    log.info("← %s", text[:100])
    cmd = text.split()[0].lower() if text.startswith("/") else ""

    if cmd in ("/start", "/hilfe", "/help"):
        tg_send(HELP_TEXT)
        return

    if cmd in ("/reset", "/neu", "/clear"):
        history_clear(con, chat_id)
        tg_send("✅ Gesprächsverlauf gelöscht.")
        return

    if cmd == "/aufgaben":
        tg_typing()
        p = PROJEKTE_VAULT / "AUFGABEN.md"
        tg_send(html.escape(_read_file(p)) if p.exists() else "⚠️ AUFGABEN.md nicht gefunden.")
        return

    if cmd == "/vault":
        tg_send(vault_list())
        return

    if cmd == "/suche":
        query = text[7:].strip()
        if not query:
            tg_send("Verwendung: /suche &lt;Suchbegriff&gt;")
            return
        tg_typing()
        tg_send(vault_search(query))
        return

    if cmd == "/lese":
        query = text[6:].strip()
        if not query:
            tg_send("Verwendung: /lese &lt;Dateiname oder Teilpfad&gt;")
            return
        tg_typing()
        tg_send(vault_read(query))
        return

    if cmd in ("/dokumente", "/docs"):
        query = text[len(cmd):].strip()
        if not query:
            tg_send("Verwendung: /dokumente &lt;Suchbegriff&gt;")
            return
        tg_typing()
        tg_send(enzyme_catalyze(query))
        return

    if cmd in ("/themen", "/petri"):
        tg_typing()
        tg_send(enzyme_petri())
        return

    if text.startswith("/"):
        tg_send(f"Unbekannter Befehl. /hilfe für Übersicht.")
        return

    # Freier Text → DeepSeek mit History + Vault-Kontext
    tg_typing()
    history_add(con, chat_id, "user", text)
    system = build_system_prompt()
    reply = ask_deepseek(history_get(con, chat_id), system)
    history_add(con, chat_id, "assistant", reply)
    log.info("→ %s…", reply[:80])
    tg_send(reply)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("Wilson AI-Assistent startet …")
    log.info("Projekte Vault: %s", PROJEKTE_VAULT)
    con = init_db()
    offset = get_offset(con)
    log.info("Polling ab Update-ID %d", offset)

    while True:
        updates = tg_get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            set_offset(con, offset)
            if "message" in upd:
                try:
                    handle(con, upd["message"])
                except Exception as e:
                    log.error("handle: %s", e, exc_info=True)

if __name__ == "__main__":
    main()
