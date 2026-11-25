"""
check_db.py — Konsistenz-Checks für die baulog-Datenbank.

Aufruf: python scripts/check_db.py   (DB-Pfad via Umgebungsvariable BAULOG_DB
überschreibbar; Standard: <projekt>/data/baulog.db)
Exit-Code 0 = keine Probleme, 1 = Probleme (werden aufgelistet).
"""
import os
import sqlite3
import sys

_PROJEKT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(_PROJEKT_DIR, "data", "baulog.db")

EPS = 0.005  # Cent-Toleranz beim Vergleichen von Euro-Beträgen

# Betrag eines Stundeneintrags: SQL-ROUND ist die EINZIGE Rundungsquelle (Summen,
# Anzeige und Verrechnung müssen auf den Cent gleich rechnen).
_BETRAG = "ROUND(te.hours * te.hourly_rate + COALESCE(te.travel_cost, 0), 2)"
# Als bezahlt geführter Anteil: voll bezahlt = ganzer Betrag, sonst die Teilzahlung.
_BEZAHLT = f"(CASE WHEN te.paid = 1 THEN {_BETRAG} ELSE COALESCE(te.paid_amount, 0) END)"
_PROJEKT_DER_STUNDE = ("JOIN tasks t ON t.id = te.task_id "
                       "JOIN sites s ON s.id = t.site_id")


def guthaben_befunde(conn: sqlite3.Connection) -> list[str]:
    """
    Prüft die Buchführung von Auszahlung (guthaben) und Verrechnung
    (guthaben_verbrauch) gegen die Stundeneinträge. Leere Liste = 100 % synchron.

    Regeln:
      1. Eine Auszahlung wird nie mit mehr verrechnet, als sie hoch ist.
      2. Ein Stundeneintrag wird nie mit mehr verrechnet, als er kostet
         (und nie mit mehr, als als bezahlt geführt wird).
      3. Verrechnet wird nur innerhalb derselben Person und desselben Projekts.
      4. Guthaben und offene Stunden derselben Person schließen sich aus
         (Guthaben wird automatisch auf offene Stunden verrechnet).
      5. Jede Auszahlung gehört zu einer bekannten Person (Umbenennungs-Waisen).
      6. Der Zahlstatus eines Eintrags ist in sich stimmig.
      7. Der Name im Stundeneintrag passt zur verknüpften Person.
      8. Beträge sind positiv.
    """
    tabellen = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"guthaben", "guthaben_verbrauch", "time_entries"} <= tabellen:
        return []
    alt = conn.row_factory
    conn.row_factory = sqlite3.Row
    befunde: list[str] = []
    try:
        # 1) Auszahlung überverrechnet
        for r in conn.execute(
                "SELECT g.id, g.person, g.betrag, ROUND(SUM(v.betrag), 2) AS verr "
                "FROM guthaben g JOIN guthaben_verbrauch v ON v.guthaben_id = g.id "
                f"GROUP BY g.id HAVING SUM(v.betrag) > g.betrag + {EPS}"):
            befunde.append(f"Auszahlung #{r['id']} ({r['person']}): {r['verr']:.2f} € verrechnet, "
                           f"aber nur {r['betrag']:.2f} € eingezahlt")

        # 2) Stundeneintrag mit mehr Verrechnung als Betrag bzw. als bezahlt geführt
        for r in conn.execute(
                f"SELECT te.id, te.person, COALESCE(te.work_date, substr(te.created_at, 1, 10)) AS tag, "
                f"{_BETRAG} AS betrag, {_BEZAHLT} AS bezahlt, ROUND(SUM(v.betrag), 2) AS verr "
                "FROM time_entries te JOIN guthaben_verbrauch v ON v.time_entry_id = te.id "
                f"GROUP BY te.id HAVING SUM(v.betrag) > MIN({_BETRAG}, {_BEZAHLT}) + {EPS}"):
            if r["verr"] > r["betrag"] + EPS:
                befunde.append(f"Stundeneintrag #{r['id']} ({r['person']}, {r['tag']}): {r['verr']:.2f} € "
                               f"Guthaben verrechnet, der Tag kostet aber nur {r['betrag']:.2f} €")
            else:
                befunde.append(f"Stundeneintrag #{r['id']} ({r['person']}, {r['tag']}): {r['verr']:.2f} € "
                               f"Guthaben verrechnet, aber nur {r['bezahlt']:.2f} € als bezahlt geführt")

        # 3) Verrechnung über Personen-/Projektgrenzen
        for r in conn.execute(
                "SELECT v.id, g.person AS gp, g.project_id AS gpid, te.person AS tp, s.project_id AS tpid "
                "FROM guthaben_verbrauch v JOIN guthaben g ON g.id = v.guthaben_id "
                f"JOIN time_entries te ON te.id = v.time_entry_id {_PROJEKT_DER_STUNDE} "
                "WHERE lower(g.person) != lower(te.person) OR g.project_id != s.project_id"):
            befunde.append(f"Verrechnung #{r['id']}: Auszahlung an {r['gp']} (Projekt {r['gpid']}) "
                           f"bezahlt Stunden von {r['tp']} (Projekt {r['tpid']})")

        # 4) Guthaben und offene Stunden gleichzeitig
        for r in conn.execute(
                "SELECT g.project_id AS pid, MIN(g.person) AS person, "
                "ROUND(SUM(g.betrag) - COALESCE((SELECT SUM(v.betrag) FROM guthaben_verbrauch v "
                "JOIN guthaben g2 ON g2.id = v.guthaben_id WHERE g2.project_id = g.project_id "
                "AND lower(g2.person) = lower(g.person)), 0), 2) AS saldo "
                "FROM guthaben g GROUP BY g.project_id, lower(g.person)"):
            if r["saldo"] <= EPS:
                continue
            offen = conn.execute(
                f"SELECT ROUND(COALESCE(SUM({_BETRAG} - COALESCE(te.paid_amount, 0)), 0), 2) "
                f"FROM time_entries te {_PROJEKT_DER_STUNDE} "
                "WHERE s.project_id = ? AND lower(te.person) = lower(?) AND te.paid = 0",
                (r["pid"], r["person"])).fetchone()[0]
            if offen > EPS:
                befunde.append(f"{r['person']} (Projekt {r['pid']}): {r['saldo']:.2f} € Guthaben und "
                               f"gleichzeitig {offen:.2f} € offene Stunden")

        # 5) Auszahlung ohne bekannte Person (z. B. nach Umbenennung nicht mitgezogen)
        for r in conn.execute(
                "SELECT g.id, g.person FROM guthaben g "
                "WHERE NOT EXISTS (SELECT 1 FROM persons p WHERE lower(p.name) = lower(g.person)) "
                "AND NOT EXISTS (SELECT 1 FROM time_entries te "
                f"{_PROJEKT_DER_STUNDE} WHERE s.project_id = g.project_id "
                "AND lower(te.person) = lower(g.person))"):
            befunde.append(f"Auszahlung #{r['id']}: '{r['person']}' passt zu keiner Person und zu "
                           f"keinen Stunden im Projekt (Umbenennung nicht mitgezogen?)")

        # 6) Zahlstatus in sich stimmig
        for r in conn.execute(
                f"SELECT te.id, te.person, te.paid, COALESCE(te.paid_amount, 0) AS pa, {_BETRAG} AS betrag "
                "FROM time_entries te WHERE te.paid NOT IN (0, 1) "
                "OR (te.paid = 1 AND COALESCE(te.paid_amount, 0) > 0) "
                f"OR (te.paid = 0 AND {_BETRAG} > 0 AND COALESCE(te.paid_amount, 0) >= {_BETRAG} - {EPS}) "
                # ein 0-€-Tag (Satz 0) darf einen bar gezahlten Anteil behalten, bis er wieder etwas kostet
                f"OR ({_BETRAG} > {EPS} AND COALESCE(te.paid_amount, 0) > {_BETRAG} + {EPS})"):
            befunde.append(f"Stundeneintrag #{r['id']} ({r['person']}): widersprüchlicher Zahlstatus "
                           f"(bezahlt={r['paid']}, Teilzahlung={r['pa']:.2f} €, Betrag={r['betrag']:.2f} €)")

        # 7) Name im Stundeneintrag weicht von der verknüpften Person ab (Umbenennung nicht mitgezogen)
        for r in conn.execute(
                "SELECT te.id, te.person, p.name FROM time_entries te JOIN persons p ON p.id = te.person_id "
                "WHERE te.person != p.name"):
            befunde.append(f"Stundeneintrag #{r['id']}: Name '{r['person']}' weicht von der verknüpften "
                           f"Person '{r['name']}' ab")

        # 8) Beträge positiv
        n = conn.execute("SELECT COUNT(*) FROM guthaben WHERE betrag <= 0").fetchone()[0]
        n += conn.execute("SELECT COUNT(*) FROM guthaben_verbrauch WHERE betrag <= 0").fetchone()[0]
        if n:
            befunde.append(f"{n} Auszahlung(en)/Verrechnung(en) mit Betrag ≤ 0")
    finally:
        conn.row_factory = alt
    return befunde


def run_checks(db_path: str) -> list[str]:
    """Führt alle Checks aus und gibt die Liste der Befunde zurück (leer = ok)."""
    probleme: list[str] = []
    uploads = os.path.join(os.path.dirname(db_path), "uploads")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        probleme.append(f"integrity_check: {ic}")
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        probleme.append(f"foreign_key_check: {len(fk)} Verletzung(en)")

    checks = [
        ("SELECT COUNT(*) FROM documents WHERE task_id IS NULL AND material_id IS NULL",
         "Dokument(e) ohne Aufgaben-/Material-Bezug"),
        ("SELECT COUNT(*) FROM time_entries WHERE hours <= 0 OR hourly_rate < 0",
         "Zeiteintrag/-einträge mit ungültigen Zahlen"),
        ("SELECT COUNT(*) FROM materials WHERE qty < 0 OR unit_price < 0",
         "Materialposition(en) mit negativen Zahlen"),
        ("SELECT COUNT(*) FROM sites WHERE status NOT IN ('aktiv','abgeschlossen')",
         "Baustelle(n) mit ungültigem Status"),
        ("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('offen','in Arbeit','fertig')",
         "Aufgabe(n) mit ungültigem Status"),
        ("SELECT COUNT(*) FROM time_entries te WHERE te.person_id IS NOT NULL "
         "AND NOT EXISTS (SELECT 1 FROM persons p WHERE p.id = te.person_id)",
         "Zeiteintrag/-einträge mit unbekannter person_id"),
        # Ein billed ungleich 0/1 lässt die Position aus ALLEN Summen
        # herausfallen (weder „mit" noch „ohne Rechnung") — still und unsichtbar.
        ("SELECT COUNT(*) FROM materials WHERE billed NOT IN (0, 1)",
         "Materialposition(en) mit ungültigem Rechnungs-Flag (fallen aus den Summen)"),
        ("SELECT COUNT(*) FROM time_entries WHERE billed NOT IN (0, 1)",
         "Zeiteintrag/-einträge mit ungültigem Rechnungs-Flag (fallen aus den Summen)"),
        # Infinity/NaN machen das gesamte Dashboard-JSON ungültig.
        ("SELECT COUNT(*) FROM time_entries "
         "WHERE hours != hours OR hourly_rate != hourly_rate "
         "OR hours > 1e12 OR hourly_rate > 1e12",
         "Zeiteintrag/-einträge mit nicht-endlichen Werten"),
        ("SELECT COUNT(*) FROM materials "
         "WHERE qty != qty OR unit_price != unit_price "
         "OR qty > 1e12 OR unit_price > 1e12",
         "Materialposition(en) mit nicht-endlichen Werten"),
        # Datensatz ohne Datei-Pfad: entsteht, wenn der Server zwischen
        # DB-Eintrag und Speichern der Datei abbricht.
        ("SELECT COUNT(*) FROM documents WHERE stored_path IS NULL OR stored_path = ''",
         "Dokument(e) ohne Datei-Pfad (abgebrochener Upload)"),
        # work_date muss YYYY-MM-DD sein, sonst sortieren/filtern Auswertungen falsch.
        ("SELECT COUNT(*) FROM time_entries WHERE work_date IS NOT NULL "
         "AND work_date != '' AND work_date NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
         "Zeiteintrag/-einträge mit ungültigem Datumsformat"),
        # Ein Gewerk einer anderen Baustelle verfälscht die Gewerk-Auswertung.
        ("SELECT COUNT(*) FROM tasks t JOIN trades tr ON t.trade_id = tr.id "
         "WHERE tr.site_id != t.site_id",
         "Aufgabe(n) mit Gewerk einer fremden Baustelle"),
    ]
    # Anfahrt gibt es erst ab Schema v3 — ältere DBs nicht daran scheitern lassen
    te_cols = {r["name"] for r in conn.execute("PRAGMA table_info(time_entries)")}
    if "travel_cost" in te_cols:
        checks.append(
            ("SELECT COUNT(*) FROM time_entries "
             "WHERE travel_cost < 0 OR travel_cost != travel_cost OR travel_cost > 1e12",
             "Zeiteintrag/-einträge mit ungültiger Anfahrt"))
    for sql, text in checks:
        n = conn.execute(sql).fetchone()[0]
        if n:
            probleme.append(f"{n} {text}")

    probleme.extend(guthaben_befunde(conn))

    db_files ={os.path.basename(r["stored_path"])
                for r in conn.execute("SELECT stored_path FROM documents")
                if r["stored_path"]}
    disk = set(os.listdir(uploads)) if os.path.isdir(uploads) else set()
    waisen = sorted(disk - db_files)
    fehlend = sorted(db_files - disk)
    if waisen:
        probleme.append(f"Waisen-Dateien in uploads/ (auf Disk, nicht in DB): {', '.join(waisen)}")
    if fehlend:
        probleme.append(f"Fehlende Dateien (in DB, nicht auf Disk): {', '.join(fehlend)}")

    conn.close()
    return probleme


def main() -> int:
    db_path = os.environ.get("BAULOG_DB", DEFAULT_DB)
    if not os.path.exists(db_path):
        print(f"DB nicht gefunden: {db_path}")
        return 1
    probleme = run_checks(db_path)
    if probleme:
        print("PROBLEME:")
        for p in probleme:
            print(" -", p)
        return 1
    print("OK — keine Probleme gefunden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
