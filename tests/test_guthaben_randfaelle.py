"""
Randfälle aus der unabhängigen Zweitprüfung (ein frischer Reviewer hat gezielt nach Abläufen gesucht,
die der erste Fix nicht abdeckte). Jeder Test hier war vorher rot.

Gemeinsames Muster: ein Tag von A (10 h × 10 € = 100 €) ist per Auszahlung bezahlt; danach wird nachträglich
etwas geändert. Am Ende dürfen weder Geld verdoppelt (Tag „bezahlt“ UND Guthaben frei) noch bar gezahlte
Euro erfunden werden.
"""
import sqlite3

import pytest

import db


def ca(x: float):
    return pytest.approx(x, abs=0.005)


def _tag_a(w, auszahlung: float = 100):
    """Person A, Auszahlung, ein Tag à 100 €."""
    a = w.person("A", 10)
    w.auszahlung("A", auszahlung)
    return a, w.stunden("A", 10, "2026-11-20", satz=10)


# ---------------------------------------------------------------------------
# 1) Satz auf 0 und zurück
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("weg", ["db", "oberflaeche"])
def test_satz_auf_null_und_zurueck_verdoppelt_kein_geld(w, weg):
    _, d = _tag_a(w)

    for satz in (0, 10):
        if weg == "db":
            db.update_time_entry(d, {"hourly_rate": satz})
        else:
            assert w.ui_speichern(d, hourly_rate=satz).status_code == 200

    assert w.zeile(d)["paid"] == 1 and w.verrechnet(d) == ca(100)    # wieder per Auszahlung bezahlt
    assert w.saldo("A") == ca(0)                                     # und das Guthaben ist wieder verbraucht
    assert w.befunde() == []


def test_satz_auf_null_und_zurueck_mit_teilverrechnung(w):
    _, d = _tag_a(w, auszahlung=30)                                  # 30 € per Auszahlung, 70 € offen
    assert w.bezahlt(d) == ca(30)

    db.update_time_entry(d, {"hourly_rate": 0})
    db.update_time_entry(d, {"hourly_rate": 10})

    assert w.bezahlt(d) == ca(30) and w.verrechnet(d) == ca(30)      # nicht 60
    assert w.saldo("A") == ca(0)
    assert w.befunde() == []


def test_satz_auf_null_und_zurueck_behaelt_den_baranteil(w):
    a, d = _tag_a(w, auszahlung=0.01)                                # nur damit die Person ein Guthaben-Konto hat
    db.undo_change(w.aenderung("guthaben", "insert"))
    assert "fehler" not in db.person_auszahlung(w.pid, "A", 40)      # 40 € bar
    w.auszahlung("A", 60)                                            # 60 € per Auszahlung → Tag voll bezahlt
    assert w.zeile(d)["paid"] == 1 and w.verrechnet(d) == ca(60)

    db.update_time_entry(d, {"hourly_rate": 0})
    db.update_time_entry(d, {"hourly_rate": 10})

    assert w.bezahlt(d) == ca(100) and w.verrechnet(d) == ca(60)     # 40 bar + 60 Auszahlung, wie vorher
    assert w.saldo("A") == ca(0)
    assert w.befunde() == []


def test_abgleich_nach_direktem_satz_null_in_der_db_dupliziert_nichts(w):
    """Reparaturweg: jemand setzt den Satz direkt in der DB auf 0 (an der App vorbei); der Abgleich räumt auf."""
    _, d = _tag_a(w)
    c = w._conn()
    c.execute("UPDATE time_entries SET hourly_rate = 0 WHERE id = ?", (d,))
    c.commit()
    c.close()

    db.guthaben_abgleich_alle(anwenden=True)

    assert w.zeile(d)["paid"] == 0 and w.saldo("A") == ca(100)       # Tag offen (0 €), Guthaben wieder frei
    db.update_time_entry(d, {"hourly_rate": 10})
    assert w.zeile(d)["paid"] == 1 and w.verrechnet(d) == ca(100) and w.saldo("A") == ca(0)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# 2) Person wechseln UND Betrag ändern im selben Speichern
# ---------------------------------------------------------------------------

def test_personenwechsel_mit_neuem_betrag_erfindet_keine_barzahlung(w):
    _, d = _tag_a(w)
    b = w.person("B", 10)

    db.update_time_entry(d, {"person": "B", "person_id": b, "hours": 15})     # MCP-Form, ohne Echo

    z = w.zeile(d)
    assert z["person"] == "B" and z["betrag"] == ca(150)
    assert z["paid"] == 0 and w.bezahlt(d) == ca(0)                  # B hat nie etwas bekommen
    assert w.saldo("A") == ca(100)
    assert w.befunde() == []


def test_personenwechsel_mit_neuem_betrag_behaelt_nur_den_baranteil(w):
    a, d = _tag_a(w, auszahlung=0.01)
    db.undo_change(w.aenderung("guthaben", "insert"))
    b = w.person("B", 10)
    assert "fehler" not in db.person_auszahlung(w.pid, "A", 40)      # 40 € bar
    w.auszahlung("A", 60)                                            # 60 € per Auszahlung
    assert w.zeile(d)["paid"] == 1

    db.update_time_entry(d, {"person": "B", "person_id": b, "hours": 15})

    assert w.bezahlt(d) == ca(40)                                    # nur der Baranteil bleibt (nicht 90)
    assert w.saldo("A") == ca(60)
    assert w.befunde() == []


def test_oberflaeche_personenwechsel_mit_kleinerem_betrag_bleibt_offen(w):
    """Das Formular schickt „Teilweise 50“; der Server macht daraus bei Betrag 40 „bezahlt“ — das ist nur Echo."""
    _, d = _tag_a(w, auszahlung=50)                                  # 100 €: 50 per Auszahlung, 50 offen
    b = w.person("B", 10)

    r = w.ui_speichern(d, person_id=b, hours=4)                      # Betrag 40 < 50

    assert r.status_code == 200
    z = w.zeile(d)
    assert z["person"] == "B" and z["paid"] == 0 and w.bezahlt(d) == ca(0)
    assert w.saldo("A") == ca(50)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# 3) Rückgängig einer Personenzuordnung / einer automatischen Zahlstatus-Zeile
# ---------------------------------------------------------------------------

def test_zuordnung_rueckgaengig_dupliziert_die_auszahlung_nicht(w):
    _, d = _tag_a(w)
    b = w.person("B", 10)
    db.update_time_entry(d, {"person": "B", "person_id": b})
    assert w.saldo("A") == ca(100) and w.zeile(d)["paid"] == 0       # Zwischenstand stimmt

    db.undo_change(w.aenderung("time_entries", "update", d, mit=lambda alt, neu: neu["person"] == "B"))

    z = w.zeile(d)
    assert z["person"] == "A" and z["paid"] == 1 and w.verrechnet(d) == ca(100)
    assert w.saldo("A") == ca(0)                                     # nicht „bezahlt“ UND Guthaben 100
    assert w.befunde() == []


def test_zuordnung_rueckgaengig_bei_teilverrechnung(w):
    _, d = _tag_a(w, auszahlung=60)                                  # 60 € per Auszahlung
    b = w.person("B", 10)
    db.update_time_entry(d, {"person": "B", "person_id": b})

    db.undo_change(w.aenderung("time_entries", "update", d, mit=lambda alt, neu: neu["person"] == "B"))

    assert w.bezahlt(d) == ca(60) and w.verrechnet(d) == ca(60)      # keine 60 € „bar“ aus dem Nichts
    assert w.saldo("A") == ca(0)
    assert w.befunde() == []


def test_zahlstatuszeile_nach_baustellen_verschiebung_nicht_einzeln_rueckgaengig(w):
    _, d = _tag_a(w)
    p2 = db.create_project("Zweitprojekt")
    db.update_site(w.sid, {"project_id": p2})                        # Tag gehört nun zu Projekt 2, Verrechnung geht zurück
    assert w.saldo("A") == ca(100) and w.zeile(d)["paid"] == 0

    cid = w.aenderung("time_entries", "update", d, mit=lambda alt, neu: alt["paid"] == 1 and neu["paid"] == 0)
    with pytest.raises(db.UndoError):
        db.undo_change(cid)

    assert w.zeile(d)["paid"] == 0 and w.saldo("A") == ca(100)       # kein „bezahlt“ obendrauf
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# 4) Projekt löschen und alles rückgängig machen
# ---------------------------------------------------------------------------

def test_projekt_loeschen_und_alles_rueckgaengig_ohne_phantomzahlung(w):
    _, d = _tag_a(w)
    vorher = (w.zeile(d)["paid"], w.verrechnet(d), w.saldo("A"))
    assert vorher == (1, ca(100), ca(0))
    basis = db.changelog_max_id()

    assert db.delete_project(w.pid)
    geloescht = [r for r in db.list_changelog(limit=500) if r["id"] > basis and r["action"] == "delete"]
    for tbl in ("projects", "sites", "tasks", "guthaben", "time_entries"):      # Abhängigkeitsreihenfolge
        for r in sorted((r for r in geloescht if r["tbl"] == tbl), key=lambda r: r["id"]):
            db.undo_change(r["id"])

    assert (w.zeile(d)["paid"], w.verrechnet(d), w.saldo("A")) == vorher
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# 5) Umbenennen und Namen
# ---------------------------------------------------------------------------

def test_umbenennen_nimmt_auch_freitext_tage_mit(w):
    pid = w.person("Hans", 10)
    w.auszahlung("Hans", 200)
    e1 = w.stunden("Hans", 10, "2026-11-20", satz=10)
    e2 = db.create_time_entry(w.tid, "Hans", 5, 10, work_date="2026-11-21")      # Freitext: person_id NULL
    assert w.zeile(e2)["person_id"] is None and w.zeile(e2)["paid"] == 1

    db.update_person(pid, {"name": "Johannes"})

    assert w.zeile(e1)["person"] == "Johannes" and w.zeile(e2)["person"] == "Johannes"
    assert w.saldo("Johannes") == ca(50)
    assert w.befunde() == []


def test_person_umbenennen_auf_fremde_schreibweise_wird_abgelehnt(w):
    alice = w.person("Alice", 10)
    w.person("Bob", 10)
    w.auszahlung("Alice", 50)

    with pytest.raises(sqlite3.IntegrityError):
        db.update_person(alice, {"name": "bob"})
    assert w.client.put(f"/api/persons/{alice}", json={"name": "bob"}).status_code == 409
    with pytest.raises(sqlite3.IntegrityError):
        db.create_person("BOB", 10)

    assert db.get_person(alice)["name"] == "Alice"
    assert w.befunde() == []


def test_person_nur_gross_klein_umbenennen_ist_erlaubt(w):
    pid = w.person("max", 10)
    assert db.update_person(pid, {"name": "Max"})
    assert db.get_person(pid)["name"] == "Max"


def test_stunden_person_als_text_ohne_id_bleibt_verknuepft(w):
    w.person("Robert")
    justus = w.person("Justus")
    e = w.stunden("Robert", 4, "2026-11-20")

    assert w.client.put(f"/api/timeentries/{e}", json={"person": "Justus"}).status_code == 200

    z = w.zeile(e)
    assert z["person"] == "Justus" and z["person_id"] == justus
    assert w.befunde() == []

    assert w.client.put(f"/api/timeentries/{e}", json={"person": "Gast"}).status_code == 200
    z = w.zeile(e)
    assert z["person"] == "Gast" and z["person_id"] is None          # Freitext: nicht mehr an Justus gebunden
    assert w.befunde() == []


def test_auszahlung_an_unbekannte_person_wird_abgelehnt(w):
    w.person("Koffee")
    r = w.client.post(f"/api/projects/{w.pid}/guthaben", json={"person": "Kofee", "amount": 100})   # Tippfehler
    assert r.status_code == 400
    assert w.auszahlungen() == []

    ok = w.client.post(f"/api/projects/{w.pid}/guthaben", json={"person": "koffee", "amount": 100})
    assert ok.status_code == 201                                     # Groß-/Kleinschreibung ist egal
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Bericht: Cent-genau wie die Summen
# ---------------------------------------------------------------------------

def test_bericht_arbeit_plus_anfahrt_ergibt_die_summe_der_tagesbetraege(w):
    """Viertelstunden × halbe Euro: Python-round(h·r) + Anfahrt wich um 1 ct von Σ ROUND(h·r + Anfahrt) ab."""
    import bericht
    zeilen = [{"hours": 0.25, "hourly_rate": 10.5, "travel_cost": 5.0},
              {"hours": 5.25, "hourly_rate": 12.5, "travel_cost": 0.0}]
    arbeit, anfahrt = bericht.arbeit_und_anfahrt(zeilen)
    assert arbeit + anfahrt == ca(sum(bericht.betrag_zeit(z) for z in zeilen))
