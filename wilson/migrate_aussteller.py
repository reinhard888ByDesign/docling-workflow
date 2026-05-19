#!/usr/bin/env python3
"""
migrate_aussteller.py — Einmalige Migration der Dispatcher-Aussteller nach email_senders (Wilson).

Liest die 91 Aussteller aus setup_aussteller.py (Ärzte, Labore, Versicherungen, ...)
und legt für jeden Eintrag mit bekannter Email-Adresse einen email_senders-Eintrag an.
Bestehende Einträge werden NICHT überschrieben (INSERT OR IGNORE für Adresse,
UPDATE nur für noch leere Kontaktfelder).

Aufruf auf Wilson:
    python3 migrate_aussteller.py [--db ~/.openclaw/doc_processor.db] [--dry-run]
"""

import argparse
import sqlite3
from pathlib import Path
from datetime import datetime

# ── Aussteller-Daten (aus setup_aussteller.py, direkt eingebettet) ──────────────

AUSSTELLER = [
    # (name, typ, strasse, plz, ort, telefon, email, notizen, aliases)
    ("Arztpraxis Maria Schneider", "arzt", "Bahnhofstrasse 2", "83224", "Grassau",
     "08641 695100", "schneider@praxisgemeinschaft-grassau.de",
     "Hausärztin, Naturheilverfahren, Psychotherapeutin"),
    ("Dr. med. Andre Hoffmann", "arzt", "Maximilianstrasse 33", "83278", "Traunstein",
     None, None, "Gastroenterologie"),
    ("Dr. med. Stefan Bech", "arzt", None, None, "Traunstein",
     None, None, "Internist, Kardiologie"),
    ("Dr. med. Semeni Cevatli-Trimpl", "arzt", None, None, "Prien am Chiemsee",
     None, None, "Fachärztin für Gynäkologie"),
    ("Dr. med. Lars D. Kobler", "arzt", None, None, "Prien am Chiemsee",
     None, None, "Facharzt für Dermatologie"),
    ("Dr. med. Andreas Weidinger", "arzt", None, None, "Grassau", None, None, None),
    ("Orthos-Prien", "arzt", None, None, "Prien am Chiemsee",
     None, None, "Orthopädie, Dr. Stephan Schill"),
    ("Gerhard Flammersberger", "arzt", None, None, "Grassau",
     None, None, "Physiotherapeut, Heilpraktiker"),
    ("AugenCentrum Rosenheim", "arzt", "Bahnhofstrasse 12", "83022", "Rosenheim",
     "08031 389500", "info@augencentrum.de", "Ortsübergreifende Gemeinschaftspraxis"),
    ("Augenklinik Rosenheim Betriebs-GmbH & Co. KG", "arzt", "Bahnhofstrasse 12", "83022", "Rosenheim",
     None, None, "OP-Rechnungen"),
    ("Fachärztezentrum Kliniken Südostbayern GmbH", "arzt", None, None, "Rosenheim", None, None, None),
    ("Dr. Stefan Hochleitner Zahnaerzte", "zahnarzt", "Haldenhoelzstrasse 2", "83071", "Stephanskirchen",
     "08036 30395-30", "info@hochleitner-zahnaerzte.de", None),
    ("Hautklinik Prien am Chiemsee", "arzt", None, None, "Prien am Chiemsee", None, None, None),
    ("MVZ Medicus Uebersee", "arzt", None, None, "Uebersee", None, None, "Hausärzte, Impfungen"),
    ("MVZ fuer Laboratoriumsdiagnostik Raubling GmbH", "labor", None, None, "Raubling", None, None, None),
    ("amedes MVZ", "labor", "Georgstrasse 50", "30159", "Hannover",
     "0551 63401-620", None, "Laboratoriumsmedizin, Hämostaseologie, Humangenetik"),
    ("Bioscientia Institut fuer Medizinische Diagnostik GmbH", "labor", None, None, None, None, None, None),
    ("MVZ Institut fuer Mikrooekologie GmbH", "labor", None, None, None, None, None, None),
    ("Dr. Dr. Georg-Friedemann Rust", "arzt", None, None, None, None, None, "Dermatologie"),
    ("Praxis fuer Urologie", "arzt", None, None, None, None, None, None),
    ("Die Radiologie", "arzt", None, None, "Rosenheim", None, None, None),
    ("Praxis fuer Frauenheilkunde", "arzt", None, None, None, None, None, None),
    ("Dott.ssa Landi Giulia", "arzt", None, "05086", "Siena (IT)",
     None, None, "Medico di Medicina Generale, Casteldelpiano"),
    ("AZIENDA USL TOSCANA SUD-EST", "arzt", None, None, "Toscana (IT)",
     None, None, "Italienisches Gesundheitsamt"),
    ("ABZ Zahnaerztliches Rechenzentrum fuer Bayern GmbH", "abrechnung",
     "Postfach 14 54", "82182", "Graefelfing", None, None, "Zahnabrechnungen"),
    ("PVS Suedwest GmbH", "abrechnung", None, None, "Stuttgart",
     None, None, "Privatärztliche Verrechnungsstelle"),
    ("PVS Baden-Wuerttemberg eG", "abrechnung", None, None, "Stuttgart",
     None, None, "Privatärztliche Verrechnungsstelle"),
    ("PVS Reis GmbH", "abrechnung", None, None, None,
     None, None, "Privatärztliche Verrechnungsstelle"),
    ("unimed GmbH", "abrechnung", None, None, "Muenchen", None, None, "Abrechnungsdienstleister"),
    ("MEDAS factoring GmbH", "abrechnung", None, None, None, None, None, None),
    ("Nelly Finance GmbH", "abrechnung", None, None, None, None, None, None),
    ("mediserv Bank GmbH", "abrechnung", None, None, None, None, None, None),
    ("Dr. Meindl u. Partner Verrechnungsstelle GmbH", "abrechnung", None, None, None, None, None, None),
    ("PAS Dr. Hammerl GmbH & Co. KG", "abrechnung", None, None, None, None, None, None),
    ("dgpar GmbH", "abrechnung", None, None, None, None, None, None),
    ("HUK-COBURG Krankenversicherung AG", "versicherung", "Bahnhofsplatz 1", "96444", "Coburg",
     None, None, "Marions Krankenversicherung"),
    ("Gothaer Krankenversicherung AG", "versicherung", "Gothaer Allee 1", "50969", "Koeln",
     None, None, "Reinhards Krankenversicherung"),
    ("Voggenauer Orthopadie Schuhtechnik", "sanitaetshaus", None, None, "Grassau",
     None, None, "Orthopädische Schuhe, Einlagen"),
    ("Orthofit Sanitaetshaus GmbH", "sanitaetshaus", None, None, None, None, None, None),
    ("HSAM Chiemsee GmbH", "sanitaetshaus", None, None, None, None, None, None),
    ("Dein-Fuss Schuhtechnik", "sanitaetshaus", None, None, None, None, None, None),
    ("Achental Apotheke Grassau", "apotheke", None, None, "Grassau", None, None, None),
]

# Kategorie-Mapping: Dispatcher-Typ → Wilson category_id
TYP_CATEGORY = {
    "arzt":         "krankenversicherung",
    "zahnarzt":     "krankenversicherung",
    "labor":        "krankenversicherung",
    "sanitaetshaus":"krankenversicherung",
    "apotheke":     "krankenversicherung",
    "abrechnung":   "krankenversicherung",
    "versicherung": "krankenversicherung",
}

# Adressat-Überschreibungen (Name → Adressat)
ADRESSAT_OVERRIDE = {
    "HUK-COBURG Krankenversicherung AG": "Marion",
    "Gothaer Krankenversicherung AG":    "Reinhard",
}

# Generische Email-Domains — kein Domain-Eintrag anlegen
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "yahoo.com",
    "outlook.com", "gmx.de", "gmx.net", "web.de", "t-online.de",
    "icloud.com", "me.com", "live.de", "live.com",
}


def _postal(strasse, plz, ort) -> str | None:
    parts = [p for p in [strasse, (f"{plz} {ort}".strip() if plz or ort else None)] if p]
    return ", ".join(parts) if parts else None


def migrate(db_path: Path, dry_run: bool):
    now = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    inserted = updated = skipped = 0

    for entry in AUSSTELLER:
        name, typ, strasse, plz, ort, telefon, email, notizen = entry
        category_id = TYP_CATEGORY.get(typ)
        adressat = ADRESSAT_OVERRIDE.get(name, "Reinhard")
        postal = _postal(strasse, plz, ort)

        addresses = []
        if email:
            email_lower = email.strip().lower()
            addresses.append(email_lower)
            domain = "@" + email_lower.split("@")[-1]
            if domain.lstrip("@") not in GENERIC_DOMAINS:
                addresses.append(domain)

        if not addresses:
            skipped += 1
            continue

        for addr in addresses:
            row = con.execute(
                "SELECT id, category_id, phone, postal, notes FROM email_senders WHERE address=?",
                (addr,)
            ).fetchone()

            if row:
                # Nur leere Felder befüllen — bestehende Daten nicht überschreiben
                sets, params = [], []
                if not row["category_id"] and category_id:
                    sets.append("category_id=?"); params.append(category_id)
                if not row["phone"] and telefon:
                    sets.append("phone=?"); params.append(telefon)
                if not row["postal"] and postal:
                    sets.append("postal=?"); params.append(postal)
                if not row["notes"] and notizen:
                    sets.append("notes=?"); params.append(notizen)
                if sets:
                    sets.append("contact_updated=?"); params.append(now)
                    params.append(row["id"])
                    if not dry_run:
                        con.execute(f"UPDATE email_senders SET {','.join(sets)} WHERE id=?", params)
                    print(f"  UPDATE {addr} — {', '.join(s.split('=')[0] for s in sets[:-1])}")
                    updated += 1
                else:
                    print(f"  SKIP   {addr} — bereits vollständig")
                    skipped += 1
            else:
                if not dry_run:
                    con.execute(
                        "INSERT INTO email_senders"
                        "(address, display_name, status, category_id, adressat, "
                        "phone, postal, notes, contact_updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (addr, name, "approved", category_id, adressat,
                         telefon, postal, notizen, now),
                    )
                print(f"  INSERT {addr} ({name}, {category_id})")
                inserted += 1

    if not dry_run:
        con.commit()
    con.close()

    print(f"\n{'DRY-RUN: ' if dry_run else ''}Fertig — {inserted} eingefügt, {updated} aktualisiert, {skipped} übersprungen")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aussteller → email_senders Migration")
    parser.add_argument("--db", default=str(Path.home() / ".openclaw" / "doc_processor.db"),
                        help="Pfad zur Wilson-DB (Standard: ~/.openclaw/doc_processor.db)")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht schreiben")
    args = parser.parse_args()
    migrate(Path(args.db), args.dry_run)
