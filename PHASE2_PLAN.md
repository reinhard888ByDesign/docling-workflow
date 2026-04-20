# Phase 2 — Dispatcher Batch-Modus

**Start:** 2026-04-19
**Referenz:** `ARCHITEKTUR.md`, Memory `project_flaches_archiv.md`

Gepflegtes Umsetzungsdokument. Jeder Schritt trägt nach Abschluss Status, Datum und wichtige Implementierungsnotizen.

---

## Phase 2.0 — Altlasten: Auto-Rescan entfernen

**Status:** ✅ erledigt 2026-04-19
**Aufwand:** 0,25 Tag

**Umsetzung:**
- `dispatcher.py:7126-7137` (Auto-Batch-Rescan-Block in `main()`) ersatzlos entfernen.
- `batch_reimport.py` im Projekt-Root mit Deprecation-Header versehen (weiterhin lauffähig, aber nicht mehr empfohlen).

**Test-Gate:** Dispatcher startet bei leerer DB ohne Rescan-Log.

**Doku-Update:** `ARCHITEKTUR.md` Rescan-Abschnitt auf "entfernt".

---

## Phase 2.1 — CLI-Modus `--batch`

**Status:** ✅ erledigt 2026-04-19 — smoke-getestet mit dry-run + classify-only + structured
**Aufwand:** 0,5 Tag

**Umsetzung:**
- `argparse`-Kopf in `main()` vorschalten. Ohne Argumente: heutiger Watch-Daemon. Mit `--batch`: Einmal-Lauf.
- Neue Argumente:
  - `--batch INPUT` — JSON (cache-reader-Format) oder flache Textliste.
  - `--ocr-source {cache,docling,hybrid}` — Default `hybrid`.
  - `--output {vault-move,classify-only,structured}` — Default `vault-move`.
  - `--output-dir PATH` — Ziel für CSV/JSONL bei `structured`.
  - `--limit N`, `--dry-run`, `--resume RUN_ID`.
- Neue Funktion `run_batch(args)`.
- Input-Parser akzeptiert sowohl `{"results":[{"path":...}]}` (cache-reader) als auch flache Liste pfadweise.

**Test-Gate:**
- `--batch <liste> --dry-run` listet auf ohne Änderung.
- `--batch <liste> --output classify-only --limit 3` klassifiziert 3, verschiebt nichts.

**Doku-Update:** `ARCHITEKTUR.md` Abschnitt "Batch-Modus" mit Argument-Referenz.

---

## Phase 2.2 — Hybrid-OCR-Gate

**Status:** ✅ erledigt 2026-04-19 — cache-hit verifiziert (2942 chars, lang=de, 0ms vs. 36s Docling)
**Aufwand:** 0,5 Tag

**Umsetzung:**
- Neue Funktion `resolve_ocr_text(pdf_path, mode, vault_root) -> (text, source, meta)`.
  - `mode=cache`: cache-reader `GET /file?path=...` (Pfad relativ zum Vault). Bei Miss: `source="cache_miss"`, text=None.
  - `mode=docling`: bestehendes `convert_to_markdown()`.
  - `mode=hybrid`: Cache versuchen; wenn Text <`HYBRID_OCR_MIN_CHARS` (500) oder `langs` nicht in {de,it,en} oder Cache-Miss → Docling-Fallback.
- Neue Konstante `HYBRID_OCR_MIN_CHARS = 500` (separat von bestehender `OCR_MIN_CHARS = 300` fürs post-Docling Qualitäts-Gate).
- `meta` enthält `ocr_source`, `char_count`, `lang`, `cache_age_days`.
- Watchdog-Pfad bleibt unverändert (nur `convert_to_markdown`).

**Test-Gate:** Batch über 10 Cache-Treffer → 10× `ocr_source=cache`. OCR-Stub → Docling-Fallback. `--ocr-source docling` ignoriert Cache.

**Doku-Update:** `ARCHITEKTUR.md` OCR-Flussdiagramm aktualisieren.

---

## Phase 2.3 — Ausgabeformate

**Status:** ✅ erledigt 2026-04-19 — CSV + JSONL geschrieben, vault-move im Batch-Modus unterdrückt
**Aufwand:** 0,75 Tag

**Umsetzung:**
- `--output vault-move`: heutiges Verhalten unverändert.
- `--output classify-only`: OCR + Klassifizierung, persistiert Ergebnis in Batch-DB-Tabelle und optional JSONL. Keine Dateiverschiebung, kein Vault-Schreiben, kein Telegram.
- `--output structured`: zusätzlich CSV-Export.
  - `run_<id>_summary.csv` — Spalten: path, kategorie, typ, absender, adressat, datum, betrag, konfidenz, lang, ocr_source.
  - `run_<id>_details.jsonl` — eine Zeile je Dokument mit vollem Klassifikationsergebnis.
- Neue Datei `dispatcher/batch_output.py` isoliert Export-Logik.
- Schema dokumentiert in `schemas/batch_export.md`.

**Test-Gate:** 20-Dokumente-Lauf erzeugt CSV+JSONL, Zeilenzahl stimmt, Konfidenz in [0,1] bzw. hoch/mittel/niedrig je nach Feld.

**Doku-Update:** `ARCHITEKTUR.md` §5 Auswertungs-Ausgaben, neue `schemas/batch_export.md`.

---

## Phase 2.4 — Dashboard `/batch`

**Status:** ✅ erledigt 2026-04-19 — HTML lädt, /api/batch/runs liefert vier Testläufe
**Aufwand:** 0,75 Tag

**Umsetzung:**
- Neue SQLite-Tabellen (in `init_db()` ergänzt):
  - `batch_runs(id, input_source, ocr_mode, output_mode, output_dir, status, total, processed, errors, started_at, finished_at, created_at)`.
  - `batch_items(id, run_id, doc_path, status, ocr_source, result_path, kategorie, konfidenz, error, processed_at)`.
- REST-Endpoints im bestehenden `_ApiHandler`:
  - `GET /api/batch/runs` — Liste mit Pagination-Default 50.
  - `GET /api/batch/runs/<id>` — Detail inkl. letzter Items.
  - `GET /api/batch/runs/<id>/items?status=error` — Items-Filter.
  - `POST /api/batch/start` — body `{"input":..., "ocr_mode":..., "output_mode":..., "output_dir":..., "limit":...}` → `{"run_id":...}`.
  - `POST /api/batch/runs/<id>/pause`, `.../resume`, `.../abort`.
  - `GET /api/batch/runs/<id>/download?kind=summary|details` — CSV/JSONL-Streamen.
- HTML-Seite `_BATCH_HTML` (Stil wie `_CACHE_HTML`):
  - Header + Nav-Link "🧰 Batch".
  - Stats-Kacheln: aktive Läufe, heute gestartet, Dokumente heute verarbeitet, Fehlerquote.
  - Tabelle Historie mit Status-Badges, Progress, Download-Links.
  - Detail-Modal mit Fehler-Liste + Pause/Resume/Abort.
- Nav in allen bestehenden Dashboards (`_DASHBOARD_HTML`, `_PIPELINE_HTML`, `_CACHE_HTML` etc.) um "Batch"-Link ergänzen.

**Test-Gate:** Lauf via Dashboard startbar, Progress tickt, Download liefert gültige CSV/JSONL.

**Doku-Update:** `ARCHITEKTUR.md` §6 Dashboard-Seiten.

---

## Nach 2.1-2.4: Teststrategie

**Status:** offen

- Container neu bauen: `docker compose up -d --build dispatcher`.
- Rauchtests:
  1. Container startet, kein Rescan im Log.
  2. `docker exec document-dispatcher python /app/dispatcher.py --help` zeigt neue Argumente.
  3. `docker exec document-dispatcher python /app/dispatcher.py --batch /tmp/test.json --dry-run` (Testliste wird im Container angelegt).
  4. `/batch`-Dashboard lädt unter http://localhost:8765/batch.
  5. Lauf über Dashboard startbar, Status-Polling aktualisiert.
- Phase 2.5 und 2.6 folgen nach User-Freigabe.

---

---

## Phase 2.4a — Dashboard-Erweiterungen (nachgezogen)

**Status:** ✅ erledigt 2026-04-20
**Aufwand:** 0,5 Tag

Außerhalb des ursprünglichen 2.0–2.4-Umfangs, aber inhaltlich zu Phase 2 gehörend:

1. **Cache → Batch Brücke**
   - `POST /api/cache/export` nimmt aktuelle Suchanfrage, schreibt Trefferliste als JSON nach `dispatcher-temp/cache_export_<slug>_<ts>.json`, liefert Container-Pfad.
   - `/cache`-Dashboard: Button "▶ An Batch übergeben" neben Suche/Leeren, Erfolgsmeldung mit Link `/batch?input=<path>`.
   - `/batch`-Dashboard: `?input=...` pre-fillt Input-Feld mit gelber Hervorhebung.

2. **Live-Logs im Pipeline-Dashboard**
   - In-Memory `LOG_BUFFER` (deque, 5000 Einträge) via custom `_RingBufferHandler` am Root-Logger.
   - `GET /api/logs?q=<substr>&limit=N&since=<ts>` liefert gefilterte Zeilen (Substring-Match auf Message).
   - Pipeline-Dashboard: "📜 Logs"-Button in `dcard-head` öffnet Modal, Live-Poll alle 2 s, Farbcodierung ERROR=rot / WARNING=orange.

3. **Queue-Sichtbarkeit**
   - `GET /api/queue/state` liest `file_queue.queue` (interner Snapshot), liefert `waiting` + `items[].name`.
   - `/pipeline` Queue-Bar erweitert um Sektion "⏳ Wartend (N)" mit orangen Chips (Cursor: default, kein Click-Handler — reine Anzeige).
   - Haupt-Dashboard `/`: Queue-Counter-Badge (orange, pulsiert nicht) neben "⚡ Pipeline" im Nav, Poll alle 3 s, `display:none` wenn Queue leer.

**Test-Gate:** Smoke-getestet nach Container-Rebuild — `/api/queue/state` liefert `{"waiting":0,"items":[]}`, `/api/logs` liefert Startup-Zeilen. Browser-Verifikation durch User.

---

## Phase 2.4b — Stabilitäts-Hotfixes (nachgezogen)

**Status:** ✅ erledigt 2026-04-20
**Aufwand:** 0,25 Tag

- `OLLAMA_NUM_CTX` (Default 8192) und `OLLAMA_TIMEOUT` (Default 300 s) als Env-Variablen konfigurierbar.
- `num_ctx` + `temperature` explizit im Klassifikations-Call gesetzt (`options`-Dict in `/api/generate`).
- `OLLAMA_MODEL` in `docker-compose.yml` per `${OLLAMA_MODEL:-gemma4:e4b}` überschreibbar.
- Grundproblem-Diagnose: 2-GB-iGPU kann Modell nicht allein im VRAM halten → Inference läuft im RAM, bei ctx=65000 OOM-Crash. ctx=8192 reicht für `md_content[:6000]`-Prompt 3-fach, stabil.

---

## Phase 2.4c — Inbox-DB-Eintrag fehlte (nachgezogen)

**Status:** ✅ erledigt 2026-04-20
**Aufwand:** 0,25 Tag

**Diagnose:** Das Dashboard zeigte als "letztes Dokument" die Ferroli-Broschüre aus Batch-Run 7, obwohl am Morgen drei Telegram-Dokumente verarbeitet worden waren. Grund: Im Code-Pfad `Klassifizierung fehlgeschlagen → verschiebe in Inbox` (`dispatcher.py:7755 ff.`) wurde `move_to_vault()` aufgerufen, danach direkt `return` — ohne `save_to_db()`. MD landete in `00 Inbox/`, PDF in `Anlagen/`, aber die `dokumente`-Tabelle blieb unberührt. Betrifft jedes Dokument, das seit Einführung des Inbox-Fallbacks ohne erfolgreiche Klassifikation die Pipeline durchlief.

**Fix 1 — Code (`dispatcher.py:7763–7775`):**
```python
if _batch_active():
    ...  # Batch hat eigene DB-Buchführung über batch_items
else:
    try:
        save_to_db(file_path, {"konfidenz": "niedrig"})
    except Exception as e:
        log.warning(f"Inbox-DB-Eintrag fehlgeschlagen für {_fn}: {e}")
```
`kategorie=NULL` → vom `/review?filter=inbox` erkannt (`WHERE kategorie='Inbox' OR kategorie IS NULL`).

**Fix 2 — Reconcile-Skript** (`reconcile_inbox_orphans.py`):
Scannt `VAULT_PDF_ARCHIV` gegen `dokumente.dateiname` + `pdf_hash`. Splittet Orphans in zwei Scopes:
- `--scope inbox` (Default): nur Orphans mit MD in `00 Inbox/` = Dispatcher-Bug-Opfer
- `--scope all`: zusätzlich Altbestand aus Evernote-/Apple-Notes-Imports

Einmal-Lauf am 2026-04-20:
- 3.207 PDFs in `Anlagen/`, 280 in DB nach Name, 102 als Hash-Duplikate erkannt
- **26 Orphans mit MD** → nachgezogen (id 126–151, darunter die 2 heutigen Telegram-Docs und 23 Legacy-Imports mit Dispatcher-v2-Namensschema)
- 2.799 Orphans ohne MD bewusst nicht angefasst (würden Review-Dashboard fluten)
- 1 heutiger Telegram-Doc (`2229_001_2---fb420f48`) korrekt als Hash-Duplikat von id=123 (anderer UUID, gleicher Scanner-Inhalt) erkannt und übersprungen.

**Verifikation:** `/api/review/queue?filter=inbox` liefert nach Fix 27 Einträge (26 nachgezogen + 1 bereits bekannt). Review-Dashboard zeigt jetzt die Inbox-Pipeline vollständig.

## Änderungslog

| Datum | Schritt | Notiz |
|-------|---------|-------|
| 2026-04-19 | Plan angelegt | Phasen 2.0-2.4 in einem Guss geplant, Test-Gate danach |
| 2026-04-19 | 2.0 abgeschlossen | Auto-Rescan-Block + Legacy-Deprecation |
| 2026-04-19 | 2.1-2.4 abgeschlossen | CLI + run_batch + Hybrid-OCR + CSV/JSONL + /batch-Dashboard |
| 2026-04-19 | Hotfix | Batch-Modus darf keine PDFs im Archiv löschen (Duplikat- und Hash-Check geschützt). `test.json` löste initial einen Datenverlust aus, Datei wurde aus `/home/reinhard/pdf-archiv/` wiederhergestellt. |
| 2026-04-19 | Hotfix | Cache-Reader liefert `langs` als String ("de"), nicht als Liste — Parser erweitert |
| 2026-04-19 | 2.4a (Teil 1) | Cache-Export-Endpoint + "▶ An Batch übergeben"-Button, `/batch?input=` Pre-Fill |
| 2026-04-20 | 2.4b | Ollama-Kontext/Timeout als Env, num_ctx im Call, Modellwechsel gemma4:e4b getestet |
| 2026-04-20 | 2.4a (Teil 2) | Log-Ringbuffer + /api/logs, /api/queue/state, Live-Logs-Modal im Pipeline, Queue-Counter-Badge |
| 2026-04-20 | 2.4c | Inbox-DB-Eintrag im Dispatcher (fehlender `save_to_db` im Inbox-Pfad) + `reconcile_inbox_orphans.py` Einmal-Lauf: 26 Legacy-Einträge nachgezogen |

## Offene Punkte nach 2.1-2.4

1. ~~**Ollama-Stabilität**~~ → 2.4b: `num_ctx`/Timeout jetzt konfigurierbar, Modell läuft stabil mit 8192 Kontext.
2. **Resume** — CLI-Flag `--resume RUN_ID` reserviert, Logik noch nicht implementiert (Phase 2.6 Test-Gate wird das fordern).
3. **20-Stichproben-Vergleich** Hybrid vs. reines Docling + Modell-Vergleich qwen2.5:7b vs. gemma4:e4b — offen.
4. **`--force-docling`-Regressionstest** — steht aus.
5. **OCR-Quellen-Kachel** in `/pipeline` — steht aus.
