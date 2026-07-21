#!/usr/bin/env python3
"""
Portfolio-Kursabruf v2 — mit direkter SQLite-Integration.
- Kein LLM-Agent mehr nötig für DB-Writes
- Idempotent: überspringt Tage, die bereits in der DB sind
- CSV-Header korrekt (10 Spalten)
- Dynamische Gewichtsnormalisierung
- JSON-Output für Tages-Briefing

Ersetzt: portfolio_update.py + LLM-Cron-Job
Changelog 2026-07-21: SQLite-Direktschreiben, Idempotenz, Cleanup
"""

import subprocess, json, csv, os, sqlite3
from datetime import datetime, date
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

CSV_FILE      = "/home/reinhard/Vaults/Inbox/portfolio_history.csv"
RESULT_FILE   = "/home/reinhard/Vaults/Inbox/portfolio_latest.json"
DB_FILE       = "/home/reinhard/Vaults/portfolio.db"

POSITIONS = [
    ("LU2233156749", "Amundi MSCI Japan SRI",          "JARI.DE",  0.88),
    ("LU2314312849", "BNP Paribas MSCI China Min TE",  "CHINE.PA", 0.42),
    ("IE00BKS7L097", "Invesco S&P 500 Scored",         "SPXE.L",   2.11),
    ("IE00B52MJY50", "iShares MSCI Pacific ex Japan",  "CPXJ.L",   0.41),
    ("IE00BG0SKF03", "iShares EM Value Factor",        "EMVL.L",   2.35),
    ("IE00BP3QZB59", "iShares World Value Factor",     "IWVL.L",   3.02),
    ("IE00BFNM3D14", "iShares MSCI Europe Screened",   "SLMC.DE",  1.68),
    ("IE00B52VJ196", "iShares MSCI Europe SRI",        "IUSK.DE",  2.04),
    ("IE00B44Z5B48", "SPDR MSCI ACWI",                "ACWI.L",  15.19),
    ("IE00BH04GL39", "Vanguard EUR Gov Bond",          "VGEA.DE", 12.28),
    ("LU0478205379", "Xtrackers EUR Corp Bond",        "XBLC.L",  12.25),
    ("LU1190417599", "Amundi Smart Overnight",         "CSH2.PA", 47.34),
    ("BTC",          "Bitcoin",                        "BTC-EUR",  1.50),
    ("ETH",          "Ether",                          "ETH-EUR",  0.50),
    ("GOLD",         "Gold",                           "GC=F",     2.00),
]

# ═══════════════════════════════════════════════════════════════════════════════
# YAHOO FINANCE
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_yahoo(ticker):
    try:
        r = subprocess.run(
            ["curl", "-s",
             f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
             "-H", "User-Agent: Mozilla/5.0"],
            capture_output=True, text=True, timeout=10
        )
        d = json.loads(r.stdout)
        m = d['chart']['result'][0]['meta']
        return m.get('regularMarketPrice'), m.get('chartPreviousClose'), m.get('currency', '')
    except Exception:
        return None, None, ''

def get_fx():
    gbp_eur, _, _ = fetch_yahoo("GBPEUR=X")
    usd_eur, _, _ = fetch_yahoo("USDEUR=X")
    return gbp_eur or 1.0, usd_eur or 1.0

def to_eur(price, currency, gbp_eur, usd_eur, isin):
    if price is None:
        return None
    if isin == "GOLD":
        return price  # Gold: USD-Position, nicht umrechnen
    if currency == "EUR":   return price
    if currency == "GBP":   return price * gbp_eur
    if currency == "GBp":   return price / 100 * gbp_eur
    if currency == "USD":   return price * usd_eur
    return price

# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db(db_path):
    """Create tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_history (
            date TEXT, isin TEXT, name TEXT, ticker TEXT,
            base_weight REAL, norm_weight REAL,
            price REAL, prev REAL, currency TEXT,
            delta_pct REAL, contribution REAL,
            PRIMARY KEY (date, isin)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_summary (
            date TEXT PRIMARY KEY,
            total_delta REAL
        )
    """)
    conn.commit()
    return conn

def today_exists(conn, today):
    """Check if today's data is already in the DB."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM portfolio_history WHERE date=?",
        (today,)
    )
    return cur.fetchone()[0] >= len(POSITIONS)

def write_to_db(conn, today, raw_data, total_delta):
    """Insert today's positions + summary into SQLite (idempotent)."""
    # Remove existing entries for today (for idempotent re-runs)
    conn.execute("DELETE FROM portfolio_history WHERE date=?", (today,))
    conn.execute("DELETE FROM portfolio_summary WHERE date=?", (today,))

    for p in raw_data:
        conn.execute("""
            INSERT OR REPLACE INTO portfolio_history
            (date, isin, name, ticker, base_weight, norm_weight,
             price, prev, currency, delta_pct, contribution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today, p["isin"], p["name"], p["ticker"],
            p["base_weight"], p["norm_weight"],
            p["price"], p["prev"], p["currency"],
            p["delta"], p["contribution"]
        ))

    conn.execute(
        "INSERT OR REPLACE INTO portfolio_summary (date, total_delta) VALUES (?, ?)",
        (today, round(total_delta, 6))
    )
    conn.commit()

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    today = date.today().isoformat()
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Init DB
    conn = init_db(DB_FILE)

    # Idempotenz: Wenn heute schon in DB → Skip
    if today_exists(conn, today):
        print(f"SKIP: Daten für {today} bereits in portfolio.db (idempotent)")
        conn.close()
        return

    # FX
    gbp_eur, usd_eur = get_fx()

    # CSV-Header (10 Spalten, korrekt!)
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Datum", "ISIN", "Bezeichnung",
                "Basisgewicht%", "Normgewicht%",
                "Kurs EUR", "Vortag EUR", "Währung orig",
                "Delta%", "Beitrag%"
            ])

    # Schritt 1: Kurse abrufen
    raw_data = []
    for isin, name, ticker, base_weight in POSITIONS:
        price_orig, prev_orig, currency = fetch_yahoo(ticker)
        price = to_eur(price_orig, currency, gbp_eur, usd_eur, isin)
        prev  = to_eur(prev_orig,  currency, gbp_eur, usd_eur, isin)
        raw_data.append({
            "isin": isin, "name": name, "ticker": ticker,
            "base_weight": base_weight,
            "price": price, "prev": prev,
            "price_orig": price_orig, "currency": currency
        })

    # Schritt 2: Dynamische Gewichtsnormalisierung
    for p in raw_data:
        if p["price"] and p["prev"] and p["prev"] != 0:
            p["delta"] = round(((p["price"] - p["prev"]) / p["prev"]) * 100, 4)
            p["effective_weight"] = p["base_weight"] * (1 + p["delta"] / 100)
        else:
            p["delta"] = None
            p["effective_weight"] = p["base_weight"]

    total_effective = sum(p["effective_weight"] for p in raw_data)
    for p in raw_data:
        p["norm_weight"] = round((p["effective_weight"] / total_effective) * 100, 4)

    # Schritt 3: Gewichtete Gesamtperformance
    weighted_total = 0.0
    for p in raw_data:
        if p["delta"] is not None:
            p["contribution"] = round((p["norm_weight"] / 100) * p["delta"], 6)
            weighted_total += p["contribution"]
        else:
            p["contribution"] = None

    # Schritt 4: CSV schreiben
    with open(CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        for p in raw_data:
            writer.writerow([
                today, p["isin"], p["name"],
                p["base_weight"], p["norm_weight"],
                round(p["price"], 4) if p["price"] else "n/a",
                round(p["prev"], 4) if p["prev"] else "n/a",
                p["currency"],
                round(p["delta"], 4) if p["delta"] is not None else "n/a",
                round(p["contribution"], 6) if p["contribution"] is not None else "n/a"
            ])

    # Schritt 5: SQLite schreiben (NEU — ersetzt LLM-Agent!)
    write_to_db(conn, today, raw_data, weighted_total)
    conn.close()

    # Schritt 6: JSON für Briefing
    with open(RESULT_FILE, 'w') as f:
        json.dump({
            "date": now,
            "fx": {"GBP_EUR": round(gbp_eur, 4), "USD_EUR": round(usd_eur, 4)},
            "positions": raw_data,
            "total_delta": round(weighted_total, 4)
        }, f, indent=2, default=str)

    # Output
    print(f"OK: Portfolio-Update {now}")
    print(f"FX: GBP/EUR={gbp_eur:.4f} USD/EUR={usd_eur:.4f}")
    print(f"Gesamtperformance (gewichtet, normalisiert): {weighted_total:+.4f}%")
    print(f"DB: {len(raw_data)} Positionen → {DB_FILE}")
    for p in raw_data:
        d = f"{p['delta']:+.2f}%" if p['delta'] is not None else "n/a"
        print(f"  {p['name']:<38} Norm: {p['norm_weight']:>7.4f}%  Δ: {d}")

if __name__ == "__main__":
    main()
