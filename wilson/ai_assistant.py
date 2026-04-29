#!/usr/bin/env python3
"""
Wilson AI-Assistent — eigenständiger Telegram-Bot
Separater Bot-Token vom Dispatcher. DeepSeek + enzyme Vault-Suche.
"""
import os, time, json, logging, sqlite3, requests, html
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "8621101278:AAHI9CkevPBpZ2uxZQIFyxjGP2m4VUXislE")
CHAT_ID        = int(os.environ.get("TELEGRAM_CHAT_ID", "8620231031"))
DEEPSEEK_KEY   = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL   = "https://api.deepseek.com/v1/chat/completions"
ENZYME_URL     = os.environ.get("ENZYME_URL", "http://192.168.86.195:11180")
POLL_TIMEOUT   = int(os.environ.get("POLL_TIMEOUT", "20"))
DB_PATH        = Path(os.environ.get("DB_PATH", os.path.expanduser("~/.openclaw/ai_assistant.db")))
MAX_HISTORY    = 20   # Nachrichten pro Gespräch

SYSTEM_PROMPT = (
    "Du bist Reinhards persönlicher KI-Assistent auf Wilson (Raspberry Pi). "
    "Reinhard verwaltet Immobilien in Deutschland und Italien (Podere dei Venti in Seggiano), "
    "betreibt einen KI-Dokumentenworkflow (Dispatcher → Obsidian-Vault), "
    "nutzt Home Assistant für sein Smart Home und spricht Deutsch und Englisch. "
    "Antworte präzise und auf Deutsch, außer der Nutzer wechselt die Sprache. "
    "Für Vault-Inhalte steht der /suche-Befehl zur Verfügung."
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

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

def get_offset(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT val FROM tg_offset WHERE id=1").fetchone()
    return row[0] if row else 0

def set_offset(con: sqlite3.Connection, val: int):
    con.execute("INSERT OR REPLACE INTO tg_offset(id,val) VALUES(1,?)", (val,))
    con.commit()

def history_get(con: sqlite3.Connection, chat_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT role, content FROM history WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
        (chat_id, MAX_HISTORY),
    ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def history_add(con: sqlite3.Connection, chat_id: int, role: str, content: str):
    con.execute(
        "INSERT INTO history(chat_id,role,content,ts) VALUES(?,?,?,?)",
        (chat_id, role, content, time.time()),
    )
    # Alte Einträge bereinigen
    con.execute(
        "DELETE FROM history WHERE chat_id=? AND id NOT IN "
        "(SELECT id FROM history WHERE chat_id=? ORDER BY ts DESC LIMIT ?)",
        (chat_id, chat_id, MAX_HISTORY * 2),
    )
    con.commit()

def history_clear(con: sqlite3.Connection, chat_id: int):
    con.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    con.commit()

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(text: str, parse_mode: str = "HTML") -> dict:
    try:
        r = requests.post(
            f"{TG_BASE}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=15,
        )
        return r.json()
    except Exception as e:
        log.error("tg_send: %s", e)
        return {}

def tg_typing():
    try:
        requests.post(
            f"{TG_BASE}/sendChatAction",
            json={"chat_id": CHAT_ID, "action": "typing"},
            timeout=5,
        )
    except Exception:
        pass

def tg_get_updates(offset: int) -> list[dict]:
    try:
        r = requests.get(
            f"{TG_BASE}/getUpdates",
            params={"offset": offset, "timeout": POLL_TIMEOUT, "limit": 10},
            timeout=POLL_TIMEOUT + 10,
        )
        return r.json().get("result", [])
    except Exception as e:
        log.warning("getUpdates: %s", e)
        time.sleep(5)
        return []

# ── enzyme ────────────────────────────────────────────────────────────────────
def enzyme_catalyze(query: str, limit: int = 5) -> str:
    try:
        r = requests.post(
            f"{ENZYME_URL}/catalyze",
            json={"query": query, "limit": limit},
            timeout=25,
        )
        if r.status_code != 200:
            return f"⚠️ enzyme HTTP {r.status_code}"
        data = r.json()
        results = data.get("results", [])
        if not results:
            return f"🔍 Keine Treffer für <i>{html.escape(query)}</i>"
        lines = [f"🔍 <b>Vault-Suche: {html.escape(query)}</b>"]
        for i, res in enumerate(results[:limit], 1):
            path = res.get("file_path", "?")
            # Dateiname ohne Pfad und Erweiterung als Titel
            title = Path(path).stem if path != "?" else "?"
            snippet = (res.get("content") or "")[:200].replace("\n", " ").strip()
            sim = res.get("similarity", 0)
            lines.append(
                f"\n{i}. <b>{html.escape(title)}</b> ({sim:.0%})\n"
                f"<i>{html.escape(snippet)}</i>"
            )
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
        lines = ["🌱 <b>Aktive Vault-Themen</b>"]
        for e in entities[:8]:
            name = e.get("name", "?")
            freq = e.get("frequency", "")
            trend = e.get("activity_trend", "")
            lines.append(f"• <b>{html.escape(str(name))}</b> {freq} {trend}".strip())
        return "\n".join(lines)
    except requests.ConnectionError:
        return "⚠️ enzyme nicht erreichbar"
    except Exception as e:
        return f"⚠️ petri fehlgeschlagen: {html.escape(str(e))}"

# ── DeepSeek ──────────────────────────────────────────────────────────────────
def ask_deepseek(messages: list[dict]) -> str:
    if not DEEPSEEK_KEY:
        return "⚠️ Kein DeepSeek API-Key konfiguriert (DEEPSEEK_API_KEY)."
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "max_tokens": 1500,
        "temperature": 0.7,
    }
    try:
        r = requests.post(
            DEEPSEEK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
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

<b>Befehle:</b>
/suche &lt;Begriff&gt; — Vault durchsuchen (enzyme)
/themen — aktive Vault-Themen (enzyme petri)
/reset — Gesprächsverlauf löschen
/hilfe — diese Hilfe

Alle anderen Nachrichten gehen direkt an DeepSeek mit Gesprächsgedächtnis.\
"""

# ── Message dispatch ──────────────────────────────────────────────────────────
def handle(con: sqlite3.Connection, msg: dict):
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    if not text or chat_id != CHAT_ID:
        return

    log.info("← %s", text[:100])

    # Commands
    if text in ("/start", "/hilfe", "/help"):
        tg_send(HELP_TEXT)
        return

    if text in ("/reset", "/neu", "/clear"):
        history_clear(con, chat_id)
        tg_send("✅ Gesprächsverlauf gelöscht.")
        return

    if text in ("/themen", "/petri"):
        tg_typing()
        tg_send(enzyme_petri())
        return

    if text.lower().startswith("/suche"):
        query = text[6:].strip()
        if not query:
            tg_send("Verwendung: /suche &lt;Suchbegriff&gt;")
            return
        tg_typing()
        tg_send(enzyme_catalyze(query))
        return

    if text.startswith("/"):
        tg_send(f"Unbekannter Befehl: {html.escape(text)}\n/hilfe für Übersicht.")
        return

    # Free text → DeepSeek mit History
    tg_typing()
    history_add(con, chat_id, "user", text)
    msgs = history_get(con, chat_id)
    reply = ask_deepseek(msgs)
    history_add(con, chat_id, "assistant", reply)
    log.info("→ %s…", reply[:80])
    tg_send(reply)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("Wilson AI-Assistent startet …")
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
        if not updates:
            pass  # Long-Polling — getUpdates selbst schläft 20s

if __name__ == "__main__":
    main()
