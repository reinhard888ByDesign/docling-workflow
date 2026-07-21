#!/usr/bin/env python3
"""
Portfolio-Backfill: Füllt Datenlücken im portfolio.db per Yahoo Historical API.
Nutzung: python3 portfolio_backfill.py [--dry-run]
"""

import subprocess, json, os, sqlite3
from datetime import date, datetime, timedelta

DB_FILE = "/home/reinhard/Vaults/portfolio.db"
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

# Bekannte Datenlücken
GAPS = [
    ("2026-04-04", "2026-04-08"),   # Ostern
    ("2026-05-28", "2026-06-10"),   # Wilson 6. OOM
    ("2026-07-13", "2026-07-20"),   # Wilson 7. Ausfall (21.7. hat Daten)
]

def fetch_historical(ticker, target_date):
    """Fetch historical close price for a specific date."""
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    start = int((dt - timedelta(days=3)).timestamp())
    end   = int((dt + timedelta(days=1)).timestamp())

    try:
        r = subprocess.run(
            ["curl", "-s",
             f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
             f"?period1={start}&period2={end}&interval=1d",
             "-H", "User-Agent: Mozilla/5.0"],
            capture_output=True, text=True, timeout=10
        )
        d = json.loads(r.stdout)
        result = d['chart']['result'][0]
        timestamps = result.get('timestamp', [])
        quotes = result['indicators']['quote'][0]
        closes = quotes.get('close', [])

        if not timestamps or not closes:
            return None, None

        # Suche den Eintrag für target_date
        for ts, close in zip(timestamps, closes):
            d = date.fromtimestamp(ts)
            if d.isoformat() == target_date and close is not None:
                # Vortag suchen
                prev_close = None
                for ts2, close2 in zip(timestamps, closes):
                    d2 = date.fromtimestamp(ts2)
                    if d2.isoformat() < target_date and close2 is not None:
                        prev_close = close2  # nimm den letzten vor target_date
                return close, prev_close
        return None, None
    except Exception:
        return None, None

def get_fx_historical(target_date):
    """Get FX rates for a specific date."""
    gbp = fetch_historical("GBPEUR=X", target_date)
    usd = fetch_historical("USDEUR=X", target_date)
    gbp_eur = gbp[0] if gbp else None
    usd_eur = usd[0] if usd else None
    return gbp_eur or 1.0, usd_eur or 1.0

def to_eur(price, currency, gbp_eur, usd_eur, isin):
    if price is None: return None
    if isin == "GOLD": return price
    if currency == "EUR": return price
    if currency == "GBP": return price * gbp_eur
    if currency == "GBp": return price / 100 * gbp_eur
    if currency == "USD": return price * usd_eur
    return price

def main():
    dry = "--dry-run" in __import__('sys').argv
    conn = sqlite3.connect(DB_FILE)

    total_filled = 0
    for gap_start, gap_end in GAPS:
        d = datetime.strptime(gap_start, "%Y-%m-%d")
        end = datetime.strptime(gap_end, "%Y-%m-%d")
        print(f"\n{'='*60}")
        print(f"Lücke: {gap_start} → {gap_end}")

        while d <= end:
            day = d.strftime("%Y-%m-%d")
            d += timedelta(days=1)

            # Check ob schon Daten existieren
            cur = conn.execute(
                "SELECT COUNT(*) FROM portfolio_history WHERE date=?", (day,))
            if cur.fetchone()[0] >= len(POSITIONS):
                print(f"  {day}: bereits vorhanden, skip")
                continue

            # Wochentag-Check (nur Mo-Fr, Wochenende skippen)
            if d.weekday() >= 5:  # d ist schon inkrementiert
                continue

            print(f"  {day}: backfilling...")
            gbp_eur, usd_eur = get_fx_historical(day)

            positions_filled = 0
            for isin, name, ticker, base_weight in POSITIONS:
                price, prev = fetch_historical(ticker, day)
                # Currency hardcoded per ticker type
                if ticker.endswith(".DE"): currency = "EUR"
                elif ticker.endswith(".L"): currency = "GBp" if ticker in ("CPXJ.L","EMVL.L","IWVL.L","ACWI.L","XBLC.L") else "GBP"
                elif ticker.endswith(".PA"): currency = "EUR"
                elif "BTC" in ticker or "ETH" in ticker: currency = "EUR"
                elif "GC=F" == ticker: currency = "USD"
                else: currency = "EUR"

                price_eur = None
                prev_eur = None
                if price:

                    price_eur = to_eur(price, currency, gbp_eur, usd_eur, isin)
                    prev_eur = to_eur(prev, currency, gbp_eur, usd_eur, isin)

                if price_eur and prev_eur and prev_eur != 0:
                    delta = ((price_eur - prev_eur) / prev_eur) * 100
                else:
                    delta = None

                if not dry:
                    conn.execute("""
                        INSERT OR REPLACE INTO portfolio_history
                        (date, isin, name, ticker, base_weight, norm_weight,
                         price, prev, currency, delta_pct, contribution)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (day, isin, name, ticker, base_weight, base_weight,
                          price_eur, prev_eur, currency, delta, None))
                positions_filled += 1

            if not dry:
                conn.commit()
            print(f"    {positions_filled} Positionen {'(dry)' if dry else '(saved)'}")
            total_filled += 1

    conn.close()
    print(f"\n{'='*60}")
    print(f"Fertig. {total_filled} Tage {'(dry-run)' if dry else 'gefüllt'}")

if __name__ == "__main__":
    main()
