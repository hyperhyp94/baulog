"""
guthaben_abgleich.py — prüft Auszahlung/Guthaben gegen die Stundeneinträge und stellt die
Synchronität her (z. B. nach nachträglich geänderten Beträgen, die früher nicht mitgezogen wurden).

Aufruf:
  python scripts/guthaben_abgleich.py             # Trockenlauf: zeigt, was sich ändern würde — ändert NICHTS
  python scripts/guthaben_abgleich.py --anwenden  # sichert die DB, wendet die Korrektur an, prüft danach erneut

DB-Pfad per Umgebungsvariable BAULOG_DB überschreibbar (Standard: <projekt>/data/baulog.db).
Exit-Code 0 = alles synchron bzw. erfolgreich angewendet, 1 = Abweichung gefunden (Trockenlauf) oder Fehler.
"""
import os
import sqlite3
import sys

_PROJEKT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJEKT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_db  # noqa: E402
import db        # noqa: E402


def _befunde(pfad: str) -> list[str]:
    conn = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
    try:
        return check_db.guthaben_befunde(conn)
    finally:
        conn.close()


def _euro(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def main(argv: list[str]) -> int:
    anwenden = "--anwenden" in argv
    pfad = os.environ.get("BAULOG_DB", db.DB_PATH)
    if not os.path.exists(pfad):
        print(f"DB nicht gefunden: {pfad}")
        return 1
    db.DB_PATH = pfad

    vorher = _befunde(pfad)
    print("Befunde vorher:" if vorher else "Befunde vorher: keine — Auszahlung und Guthaben sind synchron.")
    for b in vorher:
        print(" -", b)

    bericht = db.guthaben_abgleich_alle(anwenden=False)
    print(f"\nPersonen mit Auszahlungen geprüft: {bericht['personen']}")
    if not (bericht["salden"] or bericht["tage"] or bericht["verrechnungen_geaendert"]):
        print("Nichts anzupassen.")
        return 1 if vorher else 0
    for s in bericht["salden"]:
        print(f"  Guthaben {s['person']} (Projekt {s['projekt_id']}): {_euro(s['vorher'])} → {_euro(s['nachher'])}")
    for t in bericht["tage"]:
        print(f"  Stundeneintrag #{t['id']}: bezahlt={t['vorher']['paid']}/Teilzahlung {_euro(t['vorher']['paid_amount'])}"
              f" → bezahlt={t['nachher']['paid']}/Teilzahlung {_euro(t['nachher']['paid_amount'])}")
    print(f"  Verrechnungszeilen geändert: {bericht['verrechnungen_geaendert']}")

    if not anwenden:
        print("\nTrockenlauf — nichts geändert. Mit --anwenden korrigieren (vorher wird die DB gesichert).")
        return 1

    sicherung = db._backup_db()
    print(f"\nSicherung: {sicherung}")
    db.guthaben_abgleich_alle(anwenden=True)
    nachher = _befunde(pfad)
    if nachher:
        print("Nach dem Anwenden bleiben Befunde:")
        for b in nachher:
            print(" -", b)
        return 1
    print("Angewendet. Auszahlung und Guthaben sind jetzt synchron.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
