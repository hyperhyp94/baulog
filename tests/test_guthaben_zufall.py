"""
Zufalls-Stresstest: lange, beliebige Folgen von Anpassungen im Nachgang. Nach JEDEM Schritt
(auch nach abgelehnten) muss gelten:

  1. Das unabhängige Orakel (scripts/check_db.py) meldet keinen Befund.
  2. Ein erneuter Abgleich ändert nichts mehr — jede Änderung hinterlässt die Buchführung
     im Gleichgewicht, es gibt keine versteckte Abweichung, die später „repariert“ würde.
  3. Schattenrechnung: Der DIREKT (bar) bezahlte Anteil eines Tages (bezahlt − verrechnet) wächst nie
     ohne ausdrückliche Zahlung. Sonst wäre Geld aus dem Nichts entstanden — z. B. ein Tag bleibt
     „bezahlt“, obwohl seine Auszahlung frei geworden ist. Das Orakel allein sieht so etwas nicht,
     weil es wie eine Barzahlung aussieht.
"""
import os
import random
import sqlite3

import pytest

import db

PERSONEN = ["Robert", "Justus", "Koffee"]
SAETZE = [0, 10, 10.5, 12.5, 15, 20, 33.33]        # auch 0 € (Eigenleistung) und Rundungs-Grenzfälle
STUNDEN = [x / 4 for x in range(1, 49)]            # Viertelstunden bis 12 h
ANFAHRT = [0, 0, 5, 12.5, 50]
EXPLIZIT = ("direkt", "zahlstatus")                # Operationen, die bar gezahlte Euro bewusst ändern


def _stand(w) -> tuple:
    c = w._conn()
    try:
        return (
            [tuple(r) for r in c.execute(
                "SELECT id, task_id, person, person_id, hours, hourly_rate, travel_cost, paid, paid_amount "
                "FROM time_entries ORDER BY id")],
            [tuple(r) for r in c.execute(
                "SELECT id, guthaben_id, time_entry_id, betrag FROM guthaben_verbrauch ORDER BY id")],
            [tuple(r) for r in c.execute("SELECT id, project_id, person, betrag FROM guthaben ORDER BY id")],
        )
    finally:
        c.close()


def _zahlen(w) -> dict:
    """id → (als bezahlt geführter Betrag, per Auszahlung verrechneter Betrag, Betrag) je Stundeneintrag."""
    c = w._conn()
    try:
        return {r["id"]: (r["bezahlt"], r["verr"], r["betrag"]) for r in c.execute(
            "SELECT te.id, ROUND(te.hours * te.hourly_rate + COALESCE(te.travel_cost, 0), 2) AS betrag, "
            "CASE WHEN te.paid = 1 THEN ROUND(te.hours * te.hourly_rate + COALESCE(te.travel_cost, 0), 2) "
            "ELSE COALESCE(te.paid_amount, 0) END AS bezahlt, "
            "COALESCE((SELECT SUM(v.betrag) FROM guthaben_verbrauch v WHERE v.time_entry_id = te.id), 0) AS verr "
            "FROM time_entries te")}
    finally:
        c.close()


class Zufall:
    def __init__(self, w, rng):
        self.w, self.rng = w, rng
        self.letzter_undo = None
        for n in PERSONEN:
            w.person(n, satz=15.0)
        self.pid2 = db.create_project("Zweitprojekt")
        self.site2 = db.create_site(self.pid2, "Zweitbau")
        w.aufgabe("Elektrik")
        db.create_task(self.site2, "Fliesen")
        self.basis = db.changelog_max_id()                # Grundausstattung wird nicht rückgängig gemacht

    # --- Hilfen (Existenz immer frisch aus der DB: Löschen/Rückgängig kann etwas entfernt haben) ---
    def _ids(self, sql):
        c = self.w._conn()
        try:
            return [tuple(r) if len(r) > 1 else r[0] for r in c.execute(sql)]
        finally:
            c.close()

    def eintraege(self):
        return self._ids("SELECT id FROM time_entries ORDER BY id")

    def tasks(self):
        return self._ids("SELECT id FROM tasks ORDER BY id")

    def sites(self):
        return self._ids("SELECT id FROM sites ORDER BY id")

    def projekte(self):
        return self._ids("SELECT id FROM projects ORDER BY id")

    def person(self):
        personen = self._ids("SELECT id, name FROM persons ORDER BY id")
        return self.rng.choice(personen) if personen else (None, "Robert")

    def datum(self):
        return f"2026-11-{self.rng.randint(1, 20):02d}"

    def ein(self, liste):
        return self.rng.choice(liste) if liste else None

    # --- Operationen -------------------------------------------------------
    def op_stunden(self):
        task = self.ein(self.tasks())
        if task is None:
            return
        pid, name = self.person()
        satz, stunden, anfahrt = self.rng.choice(SAETZE), self.rng.choice(STUNDEN), self.rng.choice(ANFAHRT)
        bar = 0.0
        if self.rng.random() < 0.15:                      # gleich beim Anlegen schon etwas bar gezahlt
            bar = round(self.rng.uniform(0, 0.8) * db.euro_betrag(stunden, satz, anfahrt), 2)
        db.create_time_entry(task, name, stunden, satz, work_date=self.datum(),
                             person_id=None if self.rng.random() < 0.2 else pid,   # auch Freitext-Tage
                             travel_cost=anfahrt, paid_amount=bar)

    def op_auszahlung(self):
        projekt = self.ein(self.projekte())
        if projekt is None:
            return
        _, name = self.person()
        db.guthaben_einzahlen(projekt, name, round(self.rng.uniform(5, 500), 2), "Auszahlung")

    def op_direkt(self):
        projekt = self.ein(self.projekte())
        if projekt is None:
            return
        _, name = self.person()
        res = db.person_auszahlung(projekt, name, round(self.rng.uniform(1, 80), 2))
        assert isinstance(res, dict)

    def op_aendern(self):
        e = self.ein(self.eintraege())
        if e is None:
            return
        felder = {}
        for k, werte in (("hours", STUNDEN), ("hourly_rate", SAETZE), ("travel_cost", ANFAHRT),
                         ("work_date", [self.datum() for _ in range(4)]), ("billed", [0, 1])):
            if self.rng.random() < 0.4:
                felder[k] = self.rng.choice(werte)
        if self.rng.random() < 0.25:                      # Person wechseln UND Betrag ändern in einem Zug
            pid, name = self.person()
            felder.update({"person": name, "person_id": pid} if pid else {"person": name})
        if not felder:
            felder["travel_cost"] = self.rng.choice(ANFAHRT)
        if self.rng.random() < 0.5:                       # MCP/DB-Weg
            db.update_time_entry(e, felder)
        else:                                             # Oberfläche: Formular mit Echo des Zahlstatus
            felder.pop("person", None)                    # die Oberfläche schickt nur person_id
            self.w.ui_speichern(e, **felder)

    def op_zahlstatus(self):
        e = self.ein(self.eintraege())
        if e is None:
            return
        body = self.rng.choice([{"paid": 0}, {"paid": 1}, {"paid_amount": round(self.rng.uniform(0, 200), 2)},
                                {"paid": 0, "paid_amount": round(self.rng.uniform(0, 100), 2)}])
        self.w.client.put(f"/api/timeentries/{e}", json=body)

    def op_personenwechsel(self):
        e = self.ein(self.eintraege())
        if e is None:
            return
        pid, name = self.person()
        wie = self.rng.random()
        if wie < 0.35:
            db.update_time_entry(e, {"person": name, "person_id": pid})
        elif wie < 0.7:
            self.w.client.put(f"/api/timeentries/{e}", json={"person_id": pid})
        else:
            self.w.client.put(f"/api/timeentries/{e}", json={"person": name})     # nur der Name

    def op_loeschen(self):
        e = self.ein(self.eintraege())
        if e is None:
            return
        if self.rng.random() < 0.5:
            db.delete_time_entry(e)
        else:
            self.w.client.delete(f"/api/timeentries/{e}")

    def op_aufgabe_loeschen(self):
        tasks, sites = self.tasks(), self.sites()
        if len(tasks) > 1:
            db.delete_task(self.rng.choice(tasks))
        if sites:
            db.create_task(self.rng.choice(sites), "Neu")

    def op_projekt_loeschen(self):
        projekte = self.projekte()
        if len(projekte) > 1:
            db.delete_project(self.rng.choice(projekte))
        if len(self.projekte()) < 2:                      # damit es weitergehen kann
            p = db.create_project("Neu")
            db.create_task(db.create_site(p, "Neubau"), "Neu")

    def op_umbenennen(self):
        pid, name = self.person()
        if pid is None:
            return
        andere = [n for _, n in self._ids("SELECT id, name FROM persons") if n != name]
        neu = self.rng.choice([name + "x", name.rstrip("x") or name, "Max", "Eva", name.swapcase(), "ZZ"]
                              + [a.swapcase() for a in andere] + andere)     # auch fremde Schreibweisen
        try:
            if self.rng.random() < 0.5:
                db.update_person(pid, {"name": neu})
            else:
                self.w.client.put(f"/api/persons/{pid}", json={"name": neu})
        except sqlite3.IntegrityError:
            return

    def op_baustelle_verschieben(self):
        sites, projekte = self.sites(), self.projekte()
        if sites and projekte:
            db.update_site(self.rng.choice(sites), {"project_id": self.rng.choice(projekte)})

    def op_rueckgaengig(self):
        self.letzter_undo = None
        eintraege = [r for r in db.list_changelog(limit=80)
                     if not r["undone_by"] and r["id"] > self.basis and r["tbl"] in db.CHANGELOG_TABLES]
        if not eintraege:
            return
        r = self.rng.choice(eintraege)
        # eine reine Zahlstatus-Zeile ist eine ausdrückliche Zahlung (oder ihr Widerruf)
        import json
        alt, neu = json.loads(r["old_json"] or "null"), json.loads(r["new_json"] or "null")
        reine_zahlung = (r["tbl"] == "time_entries" and r["action"] == "update" and alt and neu and
                         {k for k in neu if neu[k] != alt.get(k)} <= {"paid", "paid_amount"})
        self.letzter_undo = {"zahlung": bool(reine_zahlung)}
        try:
            db.undo_change(r["id"])
        except db.UndoError:
            pass


OPERATIONEN = ([("stunden", 14), ("auszahlung", 8), ("direkt", 3), ("aendern", 18), ("zahlstatus", 6),
                ("personenwechsel", 5), ("loeschen", 4), ("aufgabe_loeschen", 1), ("umbenennen", 3),
                ("baustelle_verschieben", 1), ("projekt_loeschen", 1), ("rueckgaengig", 10)])


# Mehr Läufe für einen Dauertest: BAULOG_ZUFALL_SEEDS=300 python -m pytest tests/test_guthaben_zufall.py
@pytest.mark.parametrize("seed", range(int(os.environ.get("BAULOG_ZUFALL_SEEDS", "12"))))
def test_buchfuehrung_bleibt_nach_jedem_schritt_synchron(w, seed):
    rng = random.Random(seed)
    z = Zufall(w, rng)
    namen = [n for n, gewicht in OPERATIONEN for _ in range(gewicht)]
    verlauf = []
    for schritt in range(110):
        op = rng.choice(namen)
        verlauf.append(op)
        zahlen_vorher = _zahlen(w)
        z.letzter_undo = None
        try:
            getattr(z, f"op_{op}")()
        except (db.ZahlungsKonfliktError, db.UndoError):
            pass                                              # bewusst abgelehnt — Zustand muss trotzdem stimmen
        ausdruecklich = op in EXPLIZIT or (op == "rueckgaengig" and (z.letzter_undo or {}).get("zahlung"))
        if not ausdruecklich:
            zahlen_nachher = _zahlen(w)
            for eid, (bez0, verr0, bet0) in zahlen_vorher.items():
                if eid not in zahlen_nachher:
                    continue
                bez1, verr1, bet1 = zahlen_nachher[eid]
                # gilt für Tage mit unverändertem Betrag und für per Auszahlung finanzierte Tage
                # (bar gezahlte Tage folgen bei Betragsänderung bewusst dem Flag „bezahlt“)
                if abs(bet1 - bet0) <= 0.005 or verr0 > 0.005:
                    assert (bez1 - verr1) <= (bez0 - verr0) + 0.011, (
                        f"Seed {seed}, Schritt {schritt} ({op}): Tag {eid} wurde ohne Zahlung "
                        f"„bar bezahlter“: direkt {bez0 - verr0:.2f} → {bez1 - verr1:.2f} €\n"
                        f"Verlauf: {verlauf[-8:]}")
        befunde = w.befunde()
        assert befunde == [], f"Seed {seed}, Schritt {schritt} ({op}): {befunde}\nVerlauf: {verlauf[-8:]}"
        vorher = _stand(w)
        bericht = db.guthaben_abgleich_alle(anwenden=True)
        assert _stand(w) == vorher, (
            f"Seed {seed}, Schritt {schritt} ({op}): ein erneuter Abgleich hat noch etwas geändert "
            f"({bericht['tage']}, {bericht['salden']})\nVerlauf: {verlauf[-8:]}")
