# Portfolio-System — Analyse & Optimierung 2026-07-21

## Architektur

```
07:35  Cron-Job "Portfolio Kursabruf" auf Wilson
         │
         └─ python3 ~/Vaults/portfolio_update.py
              ├─ Yahoo Finance API → Kurse für 15 Positionen + FX-Rates
              ├─ Dynamische Gewichtsnormalisierung
              ├─ → portfolio_history.csv (append)
              ├─ → portfolio_latest.json (überschreiben)
              └─ → portfolio.db SQLite (INSERT, idempotent)  ← NEU

07:30  Tages-Briefing
         └─ get_portfolio() → portfolio.db → Top-3 Mover via ABS(delta_pct)
```

## Die 15 Positionen

| # | ISIN | Name | Ticker | Basis-Gew. | Typ |
|---|---|---|---|---|---|
| 1 | LU1190417599 | Amundi Smart Overnight | CSH2.PA | 47.34% | Geldmarkt |
| 2 | IE00B44Z5B48 | SPDR MSCI ACWI | ACWI.L | 15.19% | Aktien Welt |
| 3 | IE00BH04GL39 | Vanguard EUR Gov Bond | VGEA.DE | 12.28% | Staatsanleihen |
| 4 | LU0478205379 | Xtrackers EUR Corp Bond | XBLC.L | 12.25% | Unt.anleihen |
| 5 | IE00BP3QZB59 | iShares World Value Factor | IWVL.L | 3.02% | Aktien Value |
| 6 | IE00BG0SKF03 | iShares EM Value Factor | EMVL.L | 2.35% | EM Value |
| 7 | IE00BKS7L097 | Invesco S&P 500 Scored | SPXE.L | 2.11% | US Aktien |
| 8 | IE00B52VJ196 | iShares MSCI Europe SRI | IUSK.DE | 2.04% | Europa SRI |
| 9 | GOLD | Gold | GC=F | 2.00% | Rohstoff |
| 10 | IE00BFNM3D14 | iShares MSCI Europe Screened | SLMC.DE | 1.68% | Europa |
| 11 | BTC | Bitcoin | BTC-EUR | 1.50% | Krypto |
| 12 | LU2233156749 | Amundi MSCI Japan SRI | JARI.DE | 0.88% | Japan |
| 13 | ETH | Ether | ETH-EUR | 0.50% | Krypto |
| 14 | LU2314312849 | BNP Paribas MSCI China | CHINE.PA | 0.42% | China |
| 15 | IE00B52MJY50 | iShares MSCI Pacific ex Japan | CPXJ.L | 0.41% | Pazifik |

**Allokation:** ~73% Anleihen/Geldmarkt, ~27% Aktien/ETFs/Krypto/Gold.

## Berechnungslogik

### 1. Yahoo Finance API

```
GET https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d
→ regularMarketPrice, chartPreviousClose, currency
```

### 2. Währungsumrechnung

| Währung | Umrechnung |
|---|---|
| EUR | keine |
| GBP | × GBP/EUR |
| GBp | ÷ 100 × GBP/EUR |
| USD | × USD/EUR |
| Gold (USD) | KEINE Umrechnung (explizit als USD-Position) |

### 3. Dynamische Gewichtsnormalisierung

```
effective_weight = base_weight × (1 + delta/100)
norm_weight      = effective_weight / Σ(effective_weights) × 100
```

Die Gewichte driften täglich — bei Kursgewinnen steigt das Gewicht, bei Verlusten sinkt es.
Die Normalisierung stellt sicher dass die Summe immer 100% ist.

### 4. Portfolio-Gesamtperformance

```
contribution = (norm_weight / 100) × delta%
total_delta  = Σ(contributions)
```

## Optimierungen (2026-07-21)

### 1. SQLite-Direktschreiben (CRITICAL)

**Vorher:** LLM-Agent (Claude/DeepSeek) führte SQL-INSERTs aus.
→ Bei DeepSeek-Ausfall oder Wilson-Down: keine DB-Updates, Datenlücken.

**Nachher:** `portfolio_update.py` schreibt direkt in SQLite.
→ Kein LLM mehr nötig für DB-Writes, immun gegen API-Ausfälle.
→ Idempotent: `today_exists()` check verhindert Doppeleinträge.

### 2. CSV-Header korrigiert

Der Header hat jetzt durchgängig 10 Spalten:
`Datum, ISIN, Bezeichnung, Basisgewicht%, Normgewicht%, Kurs EUR, Vortag EUR, Währung orig, Delta%, Beitrag%`

### 3. Redundante Scripts archiviert

7 Varianten von DB-Writer-Scripts → `/Vaults/.archive/portfolio-scripts/`
Nur noch 1 Script: `portfolio_update.py`.

### 4. Cron-Job vereinfacht

**Vorher:** LLM-Agent führt `portfolio_update.py` aus + macht DB-INSERTs + Telegram-Summary.
**Nachher:** `portfolio_update.py` macht alles bis auf Telegram-Summary. LLM sendet nur noch die Zusammenfassung.

## Datenqualität

### Datenbank (portfolio.db)

| Tabelle | Zeilen | Zeitraum |
|---|---|---|
| portfolio_history | 1380 (92 Tage × 15 Pos.) | 2026-03-26 – 2026-07-21 |
| portfolio_summary | 91 Tage | 2026-03-26 – 2026-07-21 |

### Bekannte Datenlücken

| Zeitraum | Tage | Ursache |
|---|---|---|
| 2026-04-04 – 04-08 | 4 | Ostern (keine Marktdaten) |
| 2026-05-28 – 06-10 | 13 | Wilson down (6. OOM) |
| 2026-07-13 – 07-21 | 8 | Wilson down (7. Ausfall) |

**Lücken können per Yahoo Historical API nachgefüllt werden:**
```
GET /v8/finance/chart/{ticker}?period1={unix_start}&period2={unix_end}&interval=1d
```

### Anomalien

- **NULL-Deltas:** 76 Positions-Tage (6 Daten). Yahoo lieferte keinen `chartPreviousClose`.
- **Doppeleinträge:** 5 Tage. Cron lief zweimal. Jetzt durch Idempotenz-Check verhindert.
- **Timestamp-Format:** 2 Tage mit Zeitstempel (`2026-03-30 07:00`) statt ISO-Datum.
- **Ticker-Change:** Invesco S&P 500 wechselte am 27.03. von `5ESG.L` auf `SPXE.L`.

## Dateien

| Datei | Ort | Zweck |
|---|---|---|
| `portfolio_update.py` | `~/Vaults/` | Hauptscript (Preisabruf + DB + CSV + JSON) |
| `portfolio.db` | `~/Vaults/` | SQLite (live, 224 KB) |
| `portfolio_latest.json` | `~/Vaults/Inbox/` | Tagesoutput (5.8 KB) |
| `portfolio_history.csv` | `~/Vaults/Inbox/` | CSV-Verlauf (155 KB) |
| `.archive/portfolio-scripts/` | `~/Vaults/` | 7 archivierte DB-Writer-Varianten |

## Verwandte Cron-Jobs

| Job | Zeit | Funktion |
|---|---|---|
| Portfolio Kursabruf | 07:35 | `python3 portfolio_update.py` + Telegram-Summary |
| Tages-Briefing | 07:30 | Liest portfolio.db → Top-3 Mover |

## Siehe auch

- `docs/wilson-crash-2026-07-21.md` — Wilson-Stabilität (Ursache der Datenlücken)
- `docs/wilson-oom-fix-2026-07-11.md` — Frühere OOM-Vorfälle
