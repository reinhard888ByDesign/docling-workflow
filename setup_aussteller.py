import sqlite3

conn = sqlite3.connect('/data/dispatcher-temp/dispatcher.db')
cur = conn.cursor()

aussteller_data = [
    ("Arztpraxis Maria Schneider", "arzt", "Bahnhofstrasse 2", "83224", "Grassau", "08641 695100", "schneider@praxisgemeinschaft-grassau.de", "Hausaerztin, Naturheilverfahren, Psychotherapeutin",
     ["Arztpraxis Maria Schneider","MARIA SCHNEIDER","MARIASCHNEIDER","Maria Schneider","Maria-Schneider","MariaSchneider Praktische Aerztin","MARIASCHNEIDER Praktische Arztin","Maria Schneider Praktische Aerztin Aerztliche Psycho","Maria Schneider Praktische Arztin Arztliche Psycho","Arztpraxis Schneider","Arztpraxen Schneider","Arztpraxls Maria Schneider","Arztpraxis Maria Schneider I","Praxis Maria Schneider","OBG 83224 Grassau","Arztpraxis-Schneide\u300c\u30fbcom"]),
    ("Dr. med. Andre Hoffmann", "arzt", "Maximilianstrasse 33", "83278", "Traunstein", None, None, "Gastroenterologie",
     ["Dr. med. André Hoffmann","Dr.med. André Hoffmann","Dr.med. Andre Hoffmann","Dr. med. André Hoffmann Gastroenterologie","Dr.med. André Hoffmann Gastroenterologie"]),
    ("Dr. med. Stefan Bech", "arzt", None, None, "Traunstein", None, None, "Internist, Kardiologie",
     ["Dr. med. Stefan Bech","Dr: med. Stefan Bech","Dr: med. Stefan Bech Internist Kardiologie Filiale"]),
    ("Dr. med. Semeni Cevatli-Trimpl", "arzt", None, None, "Prien am Chiemsee", None, None, "Facharztin fuer Gynaekologie",
     ["Dr. med. Semeni Cevatli-Trimpl","Dr. med. Semeni Cevatll-Trimpl","Dr. med. Semeni Cevat-Trimp! Frauenaerztin","Dr. med. Semeni Cevat Trimpl Frauenaerztin","Dr. med. Cevati-Trimp Semen","Dr. med Semeni Cevatli-Trimpl","Dr. med. Semeni Cevatli-Trimpl Frauenaerztin","Dr. med. Semeni Cevatll-Trimpl Fachärztin für Gynä"]),
    ("Dr. med. Lars D. Kobler", "arzt", None, None, "Prien am Chiemsee", None, None, "Facharzt fuer Dermatologie",
     ["Dr. med. Lars D. Kobler Hautzentrum","Dr. med. Lars d. Kohler Facharzt für Dermatologie","Dr. med. Ijn D. Köhler","Dr. med. Ears D. Köhler","Di. med. Kobler","Dermzentrum Prien am Chiemsee","Dermatologie, Mlergologic, Phlebolovic, Proktologi","Dermaologic Phlebologie Müllerlogic Proktologic am","Phlebologic Proktologic asettherapic"]),
    ("Dr. med. Andreas Weidinger", "arzt", None, None, "Grassau", None, None, None,
     ["Dr.med.A.Weidinger"]),
    ("Orthos-Prien", "arzt", None, None, "Prien am Chiemsee", None, None, "Orthopadie, Dr. Stephan Schill",
     ["Orthos-Prien Dr. med. Stephan Schill","Orthos-Prien","Privatpraxl für Orthopädie rthos-Pr!en","Prien am Chiemsee"]),
    ("Gerhard Flammersberger", "arzt", None, None, "Grassau", None, None, "Physiotherapeut, Heilpraktiker",
     ["Gerhard Flammersberger Physiotherapeut Heilpraktik"]),
    ("AugenCentrum Rosenheim", "arzt", "Bahnhofstrasse 12", "83022", "Rosenheim", "08031 389500", "info@augencentrum.de", "Ortsuebergreifende Gemeinschaftspraxis",
     ["AugenCentrum Rosenheim","Augencentrum Rosenheim","augenArcte-geme!nschat"]),
    ("Augenklinik Rosenheim Betriebs-GmbH & Co. KG", "arzt", "Bahnhofstrasse 12", "83022", "Rosenheim", None, None, "OP-Rechnungen",
     ["Augenklinik Rosenheim Betriebs- GmbH &amp; Co.KG"]),
    ("Fachärztezentrum Kliniken Südostbayern GmbH", "arzt", None, None, "Rosenheim", None, None, None,
     ["Fachärztezentrum Kliniken Südostbayern GmbH","Fachärztezentrum K!!n\u00a1ken SUdestbayern GmbH"]),
    ("Dr. Stefan Hochleitner Zahnaerzte", "zahnarzt", "Haldenhoelzstrasse 2", "83071", "Stephanskirchen", "08036 30395-30", "info@hochleitner-zahnaerzte.de", None,
     ["Hochleitner Zahnaerzte","DR.HEIM ZÜBL - FQBSTEHlDEESTESS"]),
    ("Hautklinik Prien am Chiemsee", "arzt", None, None, "Prien am Chiemsee", None, None, None,
     ["Hautklinik Prien am Chiemsee"]),
    ("MVZ Medicus Uebersee", "arzt", None, None, "Uebersee", None, None, "Hausaerzte, Impfungen",
     ["MVZ Medicus Übersee"]),
    ("MVZ fuer Laboratoriumsdiagnostik Raubling GmbH", "labor", None, None, "Raubling", None, None, None,
     ["MVZ für Laboratoriumsdiagnostik Raubling GmbH","MVZ für Laboratoriumsd!agnost!k Raubling GmbH","Laboratoriumsdtagnostik Raubting GmbR","Institut für Pathologie und Zytologie Rosenheim","Institut für Pathologie und Zoologie Rosenheim"]),
    ("amedes MVZ", "labor", "Georgstrasse 50", "30159", "Hannover", "0551 63401-620", None, "Laboratoriumsmedizin, Haemostaseologie, Humangenetik",
     ["amedes","AMEDES","AMEDES GROUP","amedes MVZ Wagnerstibbefür Laboratoriumsmedizin"]),
    ("Bioscientia Institut fuer Medizinische Diagnostik GmbH", "labor", None, None, None, None, None, None,
     ["Bioscientia Institut für Medizinische Diagnostik G"]),
    ("MVZ Institut fuer Mikrooekologie GmbH", "labor", None, None, None, None, None, None,
     ["MVZ Institut für Mikroökologie GmbH"]),
    ("Dr. Dr. Georg-Friedemann Rust", "arzt", None, None, None, None, None, "Dermatologie",
     ["Dr. Dr. Georg-Friedemann Rust"]),
    ("Praxis fuer Urologie", "arzt", None, None, None, None, None, None,
     ["Praxis für Urologie"]),
    ("Die Radiologie", "arzt", None, None, "Rosenheim", None, None, None,
     ["Die Radiologie"]),
    ("Praxis fuer Frauenheilkunde", "arzt", None, None, None, None, None, None,
     ["PRAXIS FÜR FRAUENHEILKUNDE"]),
    ("Dott.ssa Landi Giulia", "arzt", None, "05086", "Siena (IT)", None, None, "Medico di Medicina Generale, Casteldelpiano",
     ["Dott.ssa Generale Medice"]),
    ("AZIENDA USL TOSCANA SUD-EST", "arzt", None, None, "Toscana (IT)", None, None, "Italienisches Gesundheitsamt",
     ["AZIENDA USL TOSCANA SUD-EST"]),
    ("ABZ Zahnaerztliches Rechenzentrum fuer Bayern GmbH", "abrechnung", "Postfach 14 54", "82182", "Graefelfing", None, None, "Zahnabrechnungen",
     ["ABZ-ZR GmbH","ABZ ZR GmbH","ABZ-ZRGmbH","ABZZR GmbH","ABZ Zahnärztliches Rechenzentrum für Bayern GmbH","ABZ Zshnäraliches Rechenzentrum für Beyern QmbH"]),
    ("PVS Suedwest GmbH", "abrechnung", None, None, "Stuttgart", None, None, "Privatarztliche Verrechnungsstelle",
     ["PVS Südwest GmbH","PVS Sudwest GmbH"]),
    ("PVS Baden-Wuerttemberg eG", "abrechnung", None, None, "Stuttgart", None, None, "Privatarztliche Verrechnungsstelle",
     ["PVS Baden-Württemberg eG","PVSbayern GmbH"]),
    ("PVS Reis GmbH", "abrechnung", None, None, None, None, None, "Privatarztliche Verrechnungsstelle",
     ["PVS Reis GmbH"]),
    ("unimed GmbH", "abrechnung", None, None, "Muenchen", None, None, "Abrechnungsdienstleister",
     ["unimed GmbH","un!med GmbH\u00ae"]),
    ("MEDAS factoring GmbH", "abrechnung", None, None, None, None, None, None,
     ["MEDAS factoring GmbH","Medas factoring GmbH","Medas facloring GmbH","MEDAS eA","MEDAS","MEDAS EA"]),
    ("Nelly Finance GmbH", "abrechnung", None, None, None, None, None, None,
     ["Nelly Finance GmbH"]),
    ("mediserv Bank GmbH", "abrechnung", None, None, None, None, None, None,
     ["mediserv Bank GmbH"]),
    ("Dr. Meindl u. Partner Verrechnungsstelle GmbH", "abrechnung", None, None, None, None, None, None,
     ["Dr. Meindl u. Partner Verrechnungsstelle GmbH","Dr. Meindl u Partner Verrechnungsstelle GmbH","Dr Meindl u Partner Verrechnungsstelle GmbH","Dr. Meindl u. Partner"]),
    ("PAS Dr. Hammerl GmbH & Co. KG", "abrechnung", None, None, None, None, None, None,
     ["PAS Dr. Hammerl GmbH & Co. KG","PAS Pr. Hammerl","PAS Dr Hammerl","Dr. Hammerl","PASDRHAMMERL"]),
    ("dgpar GmbH", "abrechnung", None, None, None, None, None, None,
     ["dgpar GmbH"]),
    ("HUK-COBURG Krankenversicherung AG", "versicherung", "Bahnhofsplatz 1", "96444", "Coburg", None, None, "Marions Krankenversicherung",
     ["HUK-COBURG","HUK-COBURG-Krankenversicherung AG","HUK-COBURG-KrankenversicherungAG","HUK-COBURG-KrankenvereicherungAG","HUK-COBURG-Krankenvereicherung AG","HUK-COBURG-KrankenvereiDhemng AG","HUK-COBURG-KrankBnversicherung AG","HUK-COBURG Krankenversicherung AG","HUK-COBURG.Krankenversicherung AG","HUK-COBURG-Krawkenversicherung AG","HUK-COBURG-Krankenversictiemng AG","HUK-COBURG-Krankenversicl\u00e4ng AG","HUK-COBURG-Krankenversicl\u00e4ng AG","HUK-COBURG-Krankenversichemng AG","HUK-COBURG-KrankenversehrungAG","Firma HUK-COBURG-Krankenversicherung AG","Versicherung HUK-COBURG-Krankenversicherung AG","Krankenversicherung AG","HUK-COBURG-KrankenversichernngAG","HUK-COBURG-KrankenvereicherungAG","HUK-COBURG-KrankenversehrungAG"]),
    ("Gothaer Krankenversicherung AG", "versicherung", "Gothaer Allee 1", "50969", "Koeln", None, None, "Reinhards Krankenversicherung",
     ["Gothaer","Gothaer Krankenversicherung AG","Gothaer Krankenversicherung","Gothaer Versicherung","Gothaer Vesicherung AG","Gothaef Krankenversicherung AG","Golhaer Krankenversicherung AG","GSI Gothaer Krankenversicherung AG","Gothaer Versicherung AG","Gothaer Krankenversicherung AG Kundenservice Leist","Gothaef Krankenversicherung AG"]),
    ("Voggenauer Orthopadie Schuhtechnik", "sanitaetshaus", None, None, "Grassau", None, None, "Orthopaedische Schuhe, Einlagen",
     ["Voggenauer, Orthopädie Schuhtechnik","Voggenauer Orthopädie Schuhtechnik","Voggenauer Orthopädischuhtechnik","Voggenauer Orthopädieschuhtechnik","Dirk Voggenauer, Orthopädie Schuhtechnik","Dirk Voggenauer, Schuhtechnik Sanitäts haus Seestr","u\u0435r, Orthopädie Schuhtechntk"]),
    ("Orthofit Sanitaetshaus GmbH", "sanitaetshaus", None, None, None, None, None, None,
     ["Orthofit Sanitätshaus GmbH"]),
    ("HSAM Chiemsee GmbH", "sanitaetshaus", None, None, None, None, None, None,
     ["HSAM Chiemsee GmbH"]),
    ("Dein-Fuss Schuhtechnik", "sanitaetshaus", None, None, None, None, None, None,
     ["Dein-Fuß Schuhtechnik","Dein-Fuss"]),
    ("Achental Apotheke Grassau", "apotheke", None, None, "Grassau", None, None, None,
     ["Achental Apotheke rassau","Achental Apotheke Grassau","APOTHEKE Grassau R. Schäler","HCheriidl Apotheke Grassonso","Spitzweg Apo 83209","Adler-Apotheke","Apothek FeiAisser str. 33, 83236 i\u00c6.'ersee"]),
]

inserted = 0
alias_count = 0
for row in aussteller_data:
    name, typ, strasse, plz, ort, tel, email, notizen, aliases = row
    cur.execute('INSERT OR IGNORE INTO aussteller (name, typ, strasse, plz, ort, telefon, email, notizen) VALUES (?,?,?,?,?,?,?,?)',
                (name, typ, strasse, plz, ort, tel, email, notizen))
    if cur.lastrowid:
        aussteller_id = cur.lastrowid
        inserted += 1
    else:
        cur.execute('SELECT id FROM aussteller WHERE name=?', (name,))
        aussteller_id = cur.fetchone()[0]
    for alias in aliases:
        cur.execute('INSERT OR IGNORE INTO aussteller_aliases (aussteller_id, alias) VALUES (?,?)', (aussteller_id, alias))
        alias_count += cur.rowcount

conn.commit()
print(f'{inserted} Aussteller, {alias_count} Aliases')

# Link dokumente
cur.execute('SELECT id, absender FROM dokumente WHERE absender IS NOT NULL AND absender != ""')
docs = cur.fetchall()
linked = 0
for dok_id, absender in docs:
    cur.execute('SELECT aussteller_id FROM aussteller_aliases WHERE alias=?', (absender,))
    row = cur.fetchone()
    if row:
        cur.execute('UPDATE dokumente SET aussteller_id=? WHERE id=?', (row[0], dok_id))
        linked += cur.rowcount
conn.commit()

cur.execute('SELECT COUNT(*) FROM dokumente WHERE absender IS NOT NULL AND absender != "" AND aussteller_id IS NULL')
unlinked = cur.fetchone()[0]
print(f'{linked} Dokumente verknuepft, {unlinked} noch ohne Aussteller-ID')

cur.execute('SELECT typ, COUNT(*) FROM aussteller GROUP BY typ ORDER BY COUNT(*) DESC')
print('\nAussteller nach Typ:')
for r in cur.fetchall():
    print(f'  {r[0]:<25} {r[1]}')

# Coverage by absender frequency
cur.execute('''SELECT d.absender, COUNT(*) n FROM dokumente d
               WHERE d.absender IS NOT NULL AND d.absender != "" AND d.aussteller_id IS NULL
               GROUP BY d.absender ORDER BY n DESC LIMIT 10''')
rows = cur.fetchall()
if rows:
    print('\nNoch nicht verknuepft (haeufigste):')
    for r in rows:
        print(f'  {r[1]}x  {r[0]}')
conn.close()
