"""
Auszahlung (guthaben), Verrechnung (guthaben_verbrauch) und Guthaben-Saldo müssen mit den
Stundeneinträgen IMMER auf den Cent synchron sein — auch bei Anpassungen im Nachgang:
Betrag ändern, Tag löschen, Person umbenennen oder wechseln, „bezahlt“ umschalten,
Verlauf → Rückgängig.

Jeder Test prüft am Ende zusätzlich mit dem unabhängigen Orakel (scripts/check_db.py),
dass keine Regel der Buchführung verletzt ist.
"""
import pytest

import db


def ca(x: float):
    """Cent-genauer Vergleich."""
    return pytest.approx(x, abs=0.005)


# ---------------------------------------------------------------------------
# Betrag nachträglich geändert (Stunden, Stundensatz, Anfahrt)
# ---------------------------------------------------------------------------

def test_anfahrt_nachtraeglich_auf_null_gibt_guthaben_frei(w):
    """Echtfall vom 25.11.2026: Anfahrt auf bereits bezahlten Tagen nachträglich gestrichen."""
    w.person("Koffee")
    w.auszahlung("Koffee", 500)
    e1 = w.stunden("Koffee", 11, "2026-11-21", anfahrt=10)    # 175,00
    e2 = w.stunden("Koffee", 9.5, "2026-11-22", anfahrt=50)   # 192,50
    assert w.saldo("Koffee") == ca(132.5)
    assert w.befunde() == []

    db.update_time_entry(e1, {"travel_cost": 0})
    db.update_time_entry(e2, {"travel_cost": 0})

    assert w.verrechnet(e1) == ca(165)
    assert w.verrechnet(e2) == ca(142.5)
    assert w.saldo("Koffee") == ca(192.5)                      # 500 − 165 − 142,50
    assert w.zeile(e1)["paid"] == 1 and w.zeile(e2)["paid"] == 1
    assert w.befunde() == []


@pytest.mark.parametrize("feld,wert", [("hours", 8), ("hourly_rate", 12), ("travel_cost", 0)])
@pytest.mark.parametrize("weg", ["mcp", "oberflaeche"])
def test_sinkt_der_betrag_folgt_die_verrechnung(w, feld, wert, weg):
    w.person("Koffee")
    w.auszahlung("Koffee", 500)
    e1 = w.stunden("Koffee", 10, "2026-11-20", anfahrt=10)     # 160,00
    assert w.verrechnet(e1) == ca(160)

    if weg == "mcp":
        db.update_time_entry(e1, {feld: wert})
    else:
        assert w.ui_speichern(e1, **{feld: wert}).status_code == 200

    neu = w.zeile(e1)["betrag"]
    assert neu < 160
    assert w.zeile(e1)["paid"] == 1
    assert w.verrechnet(e1) == ca(neu)
    assert w.saldo("Koffee") == ca(500 - neu)
    assert w.befunde() == []


@pytest.mark.parametrize("weg", ["mcp", "oberflaeche"])
def test_steigt_der_betrag_ist_der_mehrbetrag_offen(w, weg):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00 — ganz per Auszahlung bezahlt
    assert w.zeile(e1)["paid"] == 1 and w.saldo("Koffee") == ca(0)

    if weg == "mcp":
        db.update_time_entry(e1, {"hours": 11})
    else:
        assert w.ui_speichern(e1, hours=11).status_code == 200

    assert w.zeile(e1)["betrag"] == ca(165)
    assert w.zeile(e1)["paid"] == 0                            # nicht mehr „bezahlt“ ...
    assert w.bezahlt(e1) == ca(150)                            # ... die 150 € bleiben bezahlt
    assert w.offen(e1) == ca(15)                               # ... der Mehrbetrag ist offen
    assert w.verrechnet(e1) == ca(150)
    assert w.befunde() == []

    w.auszahlung("Koffee", 15)                                 # die nächste Auszahlung deckt genau den Rest
    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(165)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


def test_freigewordenes_guthaben_deckt_andere_offene_stunden(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00 bezahlt
    e2 = w.stunden("Koffee", 4, "2026-11-21")                  # 60,00 offen (Guthaben aufgebraucht)
    assert w.offen(e2) == ca(60)

    db.update_time_entry(e1, {"hours": 8})                     # 120,00 → 30 € werden frei

    assert w.verrechnet(e1) == ca(120)
    assert w.verrechnet(e2) == ca(30) and w.offen(e2) == ca(30)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


def test_betragsaenderung_rueckgaengig_stellt_die_verrechnung_wieder_her(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 500)
    e1 = w.stunden("Koffee", 11, "2026-11-21", anfahrt=10)     # 175,00
    db.update_time_entry(e1, {"travel_cost": 0})               # 165,00 → 10 € frei
    assert w.saldo("Koffee") == ca(335)

    cid = w.aenderung("time_entries", "update", e1,
                      mit=lambda alt, neu: alt["travel_cost"] == 10 and neu["travel_cost"] == 0)
    db.undo_change(cid)

    assert w.zeile(e1)["travel_cost"] == 10
    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(175)
    assert w.saldo("Koffee") == ca(325)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Tag gelöscht / Aufgabe gelöscht
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("weg", ["db", "api"])
def test_tag_loeschen_gibt_guthaben_frei_und_deckt_offenes(w, weg):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00 bezahlt
    e2 = w.stunden("Koffee", 4, "2026-11-21")                  # 60,00 offen

    if weg == "db":
        assert db.delete_time_entry(e1)
    else:
        assert w.client.delete(f"/api/timeentries/{e1}").status_code == 200

    assert w.zeile(e1) is None
    assert w.zeile(e2)["paid"] == 1 and w.verrechnet(e2) == ca(60)   # 60 € der freien 150 €
    assert w.saldo("Koffee") == ca(90)
    assert w.befunde() == []


def test_aufgabe_loeschen_gibt_guthaben_frei(w):
    w.person("Koffee")
    elektrik = w.aufgabe("Elektrik")
    w.auszahlung("Koffee", 300)
    w.stunden("Koffee", 10, "2026-11-20")                      # 150,00 (Ausbau)
    w.stunden("Koffee", 10, "2026-11-21")                      # 150,00 (Ausbau)
    e3 = w.stunden("Koffee", 6, "2026-11-22", task_id=elektrik)   # 90,00 offen (Elektrik)
    assert w.offen(e3) == ca(90)

    assert db.delete_task(w.tid)                               # beide bezahlten Tage verschwinden

    assert w.zeile(e3)["paid"] == 1 and w.verrechnet(e3) == ca(90)
    assert w.saldo("Koffee") == ca(210)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# „bezahlt“ umschalten auf einem per Auszahlung bezahlten Tag
# ---------------------------------------------------------------------------

def test_per_auszahlung_bezahlter_tag_laesst_sich_nicht_auf_offen_setzen(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")

    r = w.client.put(f"/api/timeentries/{e1}", json={"paid": 0})

    assert r.status_code == 409
    assert "Auszahlung" in r.get_json()["error"]
    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(150)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


def test_teilzahlung_unter_die_verrechnung_senken_wird_abgelehnt(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 100)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00: 100 € per Auszahlung, 50 € offen
    assert w.bezahlt(e1) == ca(100)

    r = w.client.put(f"/api/timeentries/{e1}", json={"paid": 0, "paid_amount": 40})

    assert r.status_code == 409
    assert w.bezahlt(e1) == ca(100) and w.verrechnet(e1) == ca(100)
    assert w.befunde() == []


def test_rest_eines_per_auszahlung_teilbezahlten_tags_darf_bar_bezahlt_werden(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 100)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00: 100 € per Auszahlung

    assert w.client.put(f"/api/timeentries/{e1}", json={"paid": 1}).status_code == 200

    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(100)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Person umbenannt / Tag einer anderen Person zugeordnet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("weg", ["db", "api"])
def test_person_umbenennen_nimmt_die_auszahlungen_mit(w, weg):
    pid = w.person("Koffee")
    w.auszahlung("Koffee", 500)
    w.stunden("Koffee", 10, "2026-11-20")                      # 150,00
    assert w.saldo("Koffee") == ca(350)

    if weg == "db":
        assert db.update_person(pid, {"name": "Kofi"})
    else:
        assert w.client.put(f"/api/persons/{pid}", json={"name": "Kofi"}).status_code == 200

    assert w.saldo("Kofi") == ca(350)
    assert w.saldo("Koffee") == ca(0)
    assert {a["person"] for a in w.auszahlungen()} == {"Kofi"}
    w.personen["Kofi"] = pid
    neu = w.stunden("Kofi", 2, "2026-11-21")                   # 30,00 — das mitgezogene Guthaben greift
    assert w.zeile(neu)["paid"] == 1 and w.saldo("Kofi") == ca(320)
    assert w.befunde() == []


def test_stunden_einer_anderen_person_zuordnen(w):
    w.person("Robert")
    justus = w.person("Justus")
    w.auszahlung("Robert", 150)
    e1 = w.stunden("Robert", 10, "2026-11-20")                 # 150,00 per Robert-Auszahlung bezahlt
    e2 = w.stunden("Robert", 4, "2026-11-21")                  # 60,00 offen

    db.update_time_entry(e1, {"person": "Justus", "person_id": justus})

    z = w.zeile(e1)
    assert z["person"] == "Justus"
    assert z["paid"] == 0 and w.bezahlt(e1) == ca(0)           # Justus hat von Roberts Auszahlung nichts erhalten
    assert w.verrechnet(e1) == ca(0)
    assert w.offen(e1) == ca(150)
    assert w.verrechnet(e2) == ca(60)                          # Roberts freie 150 € decken seinen offenen Tag
    assert w.saldo("Robert") == ca(90)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Verlauf → Rückgängig
# ---------------------------------------------------------------------------

def test_auszahlung_rueckgaengig_oeffnet_die_bezahlten_tage_wieder(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 500)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00
    e2 = w.stunden("Koffee", 8, "2026-11-21")                  # 120,00

    db.undo_change(w.aenderung("guthaben", "insert"))

    assert w.auszahlungen() == []
    for e in (e1, e2):
        assert w.zeile(e)["paid"] == 0 and w.bezahlt(e) == ca(0) and w.verrechnet(e) == ca(0)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


def test_auszahlung_rueckgaengig_laesst_direkte_zahlungen_stehen(w):
    w.person("Koffee")
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00 offen
    assert "fehler" not in db.person_auszahlung(w.pid, "Koffee", 30)   # 30 € bar direkt
    w.auszahlung("Koffee", 120)                                # deckt den Rest
    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(120)

    db.undo_change(w.aenderung("guthaben", "insert"))

    assert w.zeile(e1)["paid"] == 0
    assert w.bezahlt(e1) == ca(30) and w.offen(e1) == ca(120)  # die 30 € bar bleiben bezahlt
    assert w.befunde() == []


def test_geloeschten_tag_wiederherstellen_stellt_den_stand_wieder_her(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")
    vorher = (w.zeile(e1)["paid"], w.verrechnet(e1), w.saldo("Koffee"))
    assert vorher == (1, ca(150), ca(0))

    db.delete_time_entry(e1)
    db.undo_change(w.aenderung("time_entries", "delete", e1))

    assert (w.zeile(e1)["paid"], w.verrechnet(e1), w.saldo("Koffee")) == vorher
    assert w.befunde() == []


def test_geloeschten_tag_wiederherstellen_nachdem_das_guthaben_woanders_gebraucht_wurde(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00 bezahlt
    e2 = w.stunden("Koffee", 4, "2026-11-21")                  # 60,00 offen
    db.delete_time_entry(e1)                                   # e2 wird aus dem freien Guthaben bezahlt
    assert w.zeile(e2)["paid"] == 1 and w.saldo("Koffee") == ca(90)

    db.undo_change(w.aenderung("time_entries", "delete", e1))

    # e1 kommt zurück, ist aber nicht mehr „einfach bezahlt“: nur die noch freien 90 € sind drauf
    assert w.zeile(e2)["paid"] == 1 and w.verrechnet(e2) == ca(60)
    assert w.verrechnet(e1) == ca(90) and w.offen(e1) == ca(60)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


def test_verrechnungszeile_laesst_sich_nicht_einzeln_rueckgaengig_machen(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")

    with pytest.raises(db.UndoError):
        db.undo_change(w.aenderung("guthaben_verbrauch", "insert"))

    assert w.verrechnet(e1) == ca(150) and w.zeile(e1)["paid"] == 1
    assert w.befunde() == []


def test_automatisches_bezahlt_laesst_sich_nicht_einzeln_rueckgaengig_machen(w):
    w.person("Koffee")
    w.auszahlung("Koffee", 150)
    e1 = w.stunden("Koffee", 10, "2026-11-20")
    cid = w.aenderung("time_entries", "update", e1, mit=lambda alt, neu: alt["paid"] == 0 and neu["paid"] == 1)

    with pytest.raises(db.UndoError):
        db.undo_change(cid)

    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(150)
    assert w.befunde() == []


def test_geloeschten_tag_nach_umbenennung_wiederherstellen_traegt_den_neuen_namen(w):
    """Gefunden vom Zufallstest: der alte Name im gelöschten Eintrag trennte den Tag von der Auszahlung."""
    pid = w.person("Eva")
    w.auszahlung("Eva", 100)
    e1 = w.stunden("Eva", 4, "2026-11-20")                     # 60,00 per Auszahlung bezahlt
    db.delete_time_entry(e1)
    db.update_person(pid, {"name": "Evax"})                    # das Guthaben (100 €) zieht mit um

    db.undo_change(w.aenderung("time_entries", "delete", e1))

    assert w.zeile(e1)["person"] == "Evax"
    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(60)   # die Auszahlung greift wieder
    assert w.saldo("Evax") == ca(40)
    assert w.befunde() == []


def test_zuordnung_rueckgaengig_nach_umbenennung_traegt_den_neuen_namen(w):
    robert = w.person("Robert")
    justus = w.person("Justus")
    e1 = w.stunden("Robert", 4, "2026-11-20")
    db.update_time_entry(e1, {"person": "Justus", "person_id": justus})
    db.update_person(robert, {"name": "Robertx"})              # Eintrag gehört gerade nicht zu Robert

    db.undo_change(w.aenderung("time_entries", "update", e1,
                               mit=lambda alt, neu: alt["person_id"] == robert))

    assert w.zeile(e1)["person_id"] == robert and w.zeile(e1)["person"] == "Robertx"
    assert w.befunde() == []


def test_folgezeilen_des_umbenennens_lassen_sich_nicht_einzeln_rueckgaengig_machen(w):
    pid = w.person("Koffee")
    w.auszahlung("Koffee", 100)
    e1 = w.stunden("Koffee", 4, "2026-11-20")
    db.update_person(pid, {"name": "Kofi"})

    with pytest.raises(db.UndoError):                          # Auszahlung heißt sonst wieder anders als die Person
        db.undo_change(w.aenderung("guthaben", "update"))
    with pytest.raises(db.UndoError):                          # Stunden-Name sonst anders als die Person
        db.undo_change(w.aenderung("time_entries", "update", e1,
                                   mit=lambda alt, neu: neu["person"] == "Kofi"))

    assert w.saldo("Kofi") == ca(40) and w.zeile(e1)["person"] == "Kofi"
    assert w.befunde() == []
    db.undo_change(w.aenderung("persons", "update", pid))      # das Umbenennen als Ganzes geht
    assert w.saldo("Koffee") == ca(40) and w.zeile(e1)["person"] == "Koffee"
    assert w.befunde() == []


def test_stornierte_auszahlung_nach_umbenennung_nicht_wiederherstellbar(w):
    pid = w.person("Koffee")
    w.auszahlung("Koffee", 100)
    db.undo_change(w.aenderung("guthaben", "insert"))          # Auszahlung storniert (gelöscht)
    db.update_person(pid, {"name": "Kofi"})

    with pytest.raises(db.UndoError):
        db.undo_change(w.aenderung("guthaben", "delete"))

    assert w.auszahlungen() == []
    assert w.befunde() == []


def test_person_mit_auszahlung_laesst_sich_nicht_loeschen(w):
    """Eine Vorauszahlung hängt nur am Namen — ohne Person würde das Guthaben verwaisen."""
    pid = w.person("Koffee")
    w.auszahlung("Koffee", 100)                                # noch keine Stunden

    with pytest.raises(db.PersonInUseError):
        db.delete_person(pid)
    assert w.client.delete(f"/api/persons/{pid}").status_code == 409

    assert db.get_person(pid) is not None
    assert w.saldo("Koffee") == ca(100)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Neuer Tag mit Teilzahlung, obwohl Guthaben vorhanden ist
# ---------------------------------------------------------------------------

def test_neuer_tag_mit_teilzahlung_und_vorhandenem_guthaben(w):
    person_id = w.person("Koffee")
    w.auszahlung("Koffee", 100)

    r = w.client.post(f"/api/tasks/{w.tid}/timeentries", json={
        "person_id": person_id, "hours": 10, "hourly_rate": 15,
        "work_date": "2026-11-20", "paid_amount": 20})

    assert r.status_code == 201
    eid = r.get_json()["id"]
    assert w.zeile(eid)["paid"] == 0
    assert w.bezahlt(eid) == ca(120)                           # 20 € bar + 100 € Guthaben
    assert w.verrechnet(eid) == ca(100) and w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Direkte Teilzahlung + Auszahlung ergänzen sich (Regression)
# ---------------------------------------------------------------------------

def test_direkte_teilzahlung_und_auszahlung_ergaenzen_sich(w):
    w.person("Koffee")
    e1 = w.stunden("Koffee", 10, "2026-11-20")                 # 150,00 offen
    assert "fehler" not in db.person_auszahlung(w.pid, "Koffee", 50)
    w.auszahlung("Koffee", 100)

    assert w.zeile(e1)["paid"] == 1 and w.verrechnet(e1) == ca(100)
    assert w.saldo("Koffee") == ca(0)
    assert w.befunde() == []


# ---------------------------------------------------------------------------
# Cent-Genauigkeit: eine einzige Rundungsregel für Verrechnung und Summen
# ---------------------------------------------------------------------------

def test_verrechnung_rundet_wie_die_summen(w):
    """0,25 h × 10,50 € = 2,625 €: SQL-ROUND (alle Summen) ergibt 2,63 €, Python-round 2,62 €."""
    w.person("Koffee", satz=10.5)
    w.auszahlung("Koffee", 2.63)
    e = w.stunden("Koffee", 0.25, "2026-11-20", satz=10.5)

    assert w.zeile(e)["betrag"] == ca(2.63)
    assert w.verrechnet(e) == ca(2.63) and w.zeile(e)["paid"] == 1
    assert w.saldo("Koffee") == ca(0)                          # kein übrig gebliebener Cent
    tag = db.guthaben_saldo(w.pid, "Koffee")["einzahlungen"][0]["verrechnet"][0]
    assert tag["tagesbetrag"] == ca(2.63) and tag["betrag"] == ca(2.63)
    assert w.befunde() == []


def test_api_liefert_den_betrag_mit_derselben_rundung(w):
    """Die Oberfläche übernimmt den Server-Betrag — sonst wären „offen €“ und Verrechnung um 1 ct verschieden."""
    w.person("Koffee", satz=10.5)
    w.stunden("Koffee", 0.25, "2026-11-20", satz=10.5)
    aufgabe = w.client.get(f"/api/tasks/{w.tid}").get_json()
    liste = w.client.get(f"/api/tasks/{w.tid}/timeentries").get_json()
    assert aufgabe["time_entries"][0]["kosten"] == ca(2.63)
    assert liste["time_entries"][0]["kosten"] == ca(2.63)
    assert aufgabe["kosten"]["personal"] == ca(2.63)


def test_bericht_rechnet_mit_derselben_rundung():
    import bericht
    assert bericht.betrag_zeit({"hours": 0.25, "hourly_rate": 10.5, "travel_cost": 0}) == ca(2.63)
    assert bericht.betrag_zeit({"hours": 9.5, "hourly_rate": 15, "travel_cost": None}) == ca(142.5)
