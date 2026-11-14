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

    db_files = {os.path.basename(r["stored_path"])
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
