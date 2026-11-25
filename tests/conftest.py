"""
Gemeinsame Fixtures: temporäre SQLite-DB, Flask-Test-Client und die „Werkstatt“ —
eine kleine Hilfe für Szenario-Tests rund um Auszahlung (guthaben) und Verrechnung.
"""
import os
import sqlite3
import sys

import pytest

_BAULOG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BAULOG)
sys.path.insert(0, os.path.join(_BAULOG, "scripts"))

import check_db                  # noqa: E402  (scripts/check_db.py)
import db as db_module           # noqa: E402
import server as server_module   # noqa: E402

EPS = 0.005


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """Test-Client, dessen DB und Upload-Ordner in tmp_path liegen."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(server_module, "UPLOADS_DIR", str(tmp_path / "uploads"))
    os.makedirs(tmp_path / "uploads", exist_ok=True)
    db_module.init_db()
    server_module.app.config["TESTING"] = True
    return server_module.app.test_client()


class Werkstatt:
    """Legt Projekt/Personen/Stunden/Auszahlungen an und liest den Buchungsstand."""

    def __init__(self, client):
        self.client = client
        self.db_path = db_module.DB_PATH
        self.pid = db_module.create_project("Dachbodenausbau")
        self.sid = db_module.create_site(self.pid, "Haus")
        self.tid = db_module.create_task(self.sid, "Ausbau")
        self.personen: dict[str, int] = {}

    # --- Anlegen -----------------------------------------------------------
    def person(self, name: str, satz: float = 15.0) -> int:
        self.personen[name] = db_module.create_person(name, satz)
        return self.personen[name]

    def aufgabe(self, titel: str) -> int:
        return db_module.create_task(self.sid, titel)

    def stunden(self, person: str, stunden: float, datum: str, satz: float = 15.0,
                anfahrt: float = 0.0, task_id: int | None = None, **kw) -> int:
        return db_module.create_time_entry(
            task_id or self.tid, person, stunden, satz, work_date=datum,
            person_id=self.personen.get(person), travel_cost=anfahrt, **kw)

    def auszahlung(self, person: str, betrag: float, notiz: str = "Auszahlung") -> dict:
        return db_module.guthaben_einzahlen(self.pid, person, betrag, notiz)

    # --- Lesen -------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def saldo(self, person: str) -> float:
        return db_module.guthaben_saldo(self.pid, person)["saldo"]

    def zeile(self, eid: int) -> dict | None:
        c = self._conn()
        try:
            r = c.execute(
                "SELECT te.*, ROUND(te.hours * te.hourly_rate + COALESCE(te.travel_cost, 0), 2) AS betrag "
                "FROM time_entries te WHERE te.id = ?", (eid,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def verrechnet(self, eid: int) -> float:
        c = self._conn()
        try:
            return round(c.execute("SELECT COALESCE(SUM(betrag), 0) FROM guthaben_verbrauch "
                                   "WHERE time_entry_id = ?", (eid,)).fetchone()[0], 2)
        finally:
            c.close()

    def bezahlt(self, eid: int) -> float:
        z = self.zeile(eid)
        return z["betrag"] if z["paid"] else round(z["paid_amount"] or 0, 2)

    def offen(self, eid: int) -> float:
        return round(self.zeile(eid)["betrag"] - self.bezahlt(eid), 2)

    def auszahlungen(self, person: str | None = None) -> list[dict]:
        c = self._conn()
        try:
            sql = "SELECT * FROM guthaben" + (" WHERE lower(person) = lower(?)" if person else "") + " ORDER BY id"
            return [dict(r) for r in c.execute(sql, (person,) if person else ())]
        finally:
            c.close()

    def befunde(self) -> list[str]:
        """Befunde des unabhängigen Prüf-Orakels (leer = 100 % synchron)."""
        c = sqlite3.connect(self.db_path)
        try:
            return check_db.guthaben_befunde(c)
        finally:
            c.close()

    def aenderung(self, tbl: str, action: str, row_id: int | None = None, mit=None) -> int:
        """ID des jüngsten, noch nicht rückgängig gemachten Protokolleintrags (optional gefiltert)."""
        import json
        for r in db_module.list_changelog(limit=500, tbl=tbl, row_id=row_id):
            if r["action"] != action or r["undone_by"]:
                continue
            if mit is None or mit(json.loads(r["old_json"] or "null"), json.loads(r["new_json"] or "null")):
                return r["id"]
        raise AssertionError(f"kein Protokolleintrag {tbl}/{action}/{row_id}")

    # --- Bedienen wie die Oberfläche ----------------------------------------
    def ui_speichern(self, eid: int, **aenderungen):
        """Bearbeiten-Formular speichern: schickt wie speichereZeit() alle Felder + Zahlstatus als Echo."""
        z = self.zeile(eid)
        if z["paid"]:
            zahl = {"paid": 1, "paid_amount": 0}
        elif (z["paid_amount"] or 0) > 0:
            zahl = {"paid": 0, "paid_amount": z["paid_amount"]}
        else:
            zahl = {"paid": 0, "paid_amount": 0}
        body = {"hours": z["hours"], "hourly_rate": z["hourly_rate"], "travel_cost": z["travel_cost"],
                "billed": z["billed"], "work_date": z["work_date"], "person_id": z["person_id"], **zahl}
        body.update(aenderungen)
        return self.client.put(f"/api/timeentries/{eid}", json=body)


@pytest.fixture()
def w(app_client):
    return Werkstatt(app_client)
