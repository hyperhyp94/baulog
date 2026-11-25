"""
Stundenzettel je Person — Daten holen (SQL), auswerten (Wochen, Monate, Durchschnitte) und als
HTML mit Diagrammen aufbereiten.

Die Auswertung wird mit fertigen Zeilen geprüft (Hilfe z), das Holen mit der Werkstatt-Fixture `w`
aus tests/conftest.py. HTML und Diagramme entstehen aus der Auswertung und werden als Text bzw. XML
geprüft; das PDF selbst erzeugt WeasyPrint, das es nur auf dem Pi gibt — `main` läuft hier mit einer Attrappe,
ebenso der Unterprozess, den der Endpunkt in server.py startet (Fixture `attrappe`).
"""
import io
import json
import pathlib
import re
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import date, timedelta

import pytest

import db
import stundenzettel
from bericht import GRUEN
from stundenzettel import _r2, zeilen_holen, auswerten, html_stundenzettel, wochen_svg, monate_svg


def z(tag, std, aufgabe="Allgemein", projekt="Projekt A", person="Anna"):
    """Eine Zeile, wie zeilen_holen sie liefert."""
    return {"tag": tag, "std": float(std), "aufgabe": aufgabe, "projekt": projekt, "person": person}


def zweites_projekt() -> int:
    """Projekt „Gartenhaus“ mit Baustelle „Garten“ und Aufgabe „Zaun“; gibt die Aufgaben-ID zurück."""
    pid = db.create_project("Gartenhaus")
    return db.create_task(db.create_site(pid, "Garten"), "Zaun")


# ---------------------------------------------------------------------------
# Auswertung (rein)
# ---------------------------------------------------------------------------

def test_auswerten_summen_wochen_und_durchschnitte():
    d = auswerten([z("2026-11-02", 8), z("2026-11-03", 6.5), z("2026-11-10", 4), z("2026-11-26", 7.5)],
                  None, None, heute="2026-11-30")
    assert (d["von"], d["bis"]) == ("2026-11-02", "2026-11-26") and d["person"] == "Anna"
    assert d["std"] == 26.0 and d["arbeitstage"] == 4 and d["projekte"] == ["Projekt A"]
    assert d["wochen_im_zeitraum"] == 4 and d["arbeitswochen"] == 3
    assert (d["std_pro_woche"], d["std_pro_arbeitswoche"]) == (6.5, 8.67)
    assert [(w["kw"], w["std"], w["tage"]) for w in d["wochen"]] == [(45, 14.5, 2), (46, 4.0, 1), (47, 0.0, 0), (48, 7.5, 1)]
    assert (d["wochen"][0]["montag"], d["wochen"][0]["sonntag"]) == ("2026-11-02", "2026-11-08")
    assert d["monate"] == [{"jahr": 2026, "monat": 11, "std": 26.0, "tage": 4}]


def test_mehrere_eintraege_am_tag_sind_ein_arbeitstag():
    d = auswerten([z("2026-11-02", 3), z("2026-11-02", 2, "Bad"), z("2026-11-03", 1)], None, None)
    assert d["std"] == 6.0 and d["arbeitstage"] == 2 and len(d["tage"]) == 3 and d["std_pro_woche"] == 6.0


def test_zeitraum_ueber_den_jahreswechsel():                       # Review Focus 2
    d = auswerten([z("2026-12-28", 5), z("2027-01-04", 3)], None, None)
    assert [(w["jahr"], w["kw"], w["montag"], w["sonntag"]) for w in d["wochen"]] == [
        (2026, 53, "2026-12-28", "2027-01-03"), (2027, 1, "2027-01-04", "2027-01-10")]
    assert [(m["jahr"], m["monat"], m["std"]) for m in d["monate"]] == [(2026, 12, 5.0), (2027, 1, 3.0)]


def test_von_bis_filtern_und_angegebenes_bis_endet_spaetestens_heute():
    d = auswerten([z("2026-10-15", 4), z("2026-11-02", 6), z("2026-11-20", 2)], "2026-11-01", "2026-12-31", heute="2026-11-25")
    assert (d["von"], d["bis"]) == ("2026-11-01", "2026-11-25") and d["std"] == 8.0
    assert [w["kw"] for w in d["wochen"]] == [44, 45, 46, 47, 48] and d["wochen_im_zeitraum"] == 5


def test_ohne_bis_endet_am_letzten_eintrag_auch_nach_heute():
    d = auswerten([z("2026-11-20", 2), z("2026-12-05", 3)], None, None, heute="2026-11-25")
    assert d["bis"] == "2026-12-05" and d["std"] == 5.0


def test_eintrag_am_bis_tag_und_an_heute_zaehlt():                 # ein Off-by-one risse die Stunden von heute aus „Dieser Monat“
    am_bis = auswerten([z("2026-11-24", 1), z("2026-11-25", 2), z("2026-11-26", 4)], None, "2026-11-25", heute="2026-11-30")
    assert am_bis["std"] == 3.0 and am_bis["bis"] == "2026-11-25"             # der Tag von bis zählt, der Tag danach nicht
    an_heute = auswerten([z("2026-11-25", 2), z("2026-11-26", 4)], None, "2026-12-31", heute="2026-11-25")
    assert an_heute["std"] == 2.0 and an_heute["bis"] == "2026-11-25"         # ein späteres bis endet heute, heute zählt noch


def test_ein_einziger_arbeitstag():                                # Review Focus 1
    d = auswerten([z("2026-11-04", 6)], None, None)
    assert (d["von"], d["bis"]) == ("2026-11-04", "2026-11-04") and d["wochen_im_zeitraum"] == d["arbeitswochen"] == 1
    assert d["std_pro_woche"] == d["std_pro_arbeitswoche"] == 6.0


def test_keine_stunden():
    d = auswerten([], None, None)
    assert d["std"] == 0.0 and d["arbeitstage"] == 0 and d["wochen"] == d["monate"] == d["tage"] == []
    assert d["von"] is None and d["bis"] is None and d["std_pro_woche"] == 0.0 and d["person"] is None
    assert auswerten([z("2026-12-02", 3)], "2026-12-01", "2026-12-31", heute="2026-11-25")["std"] == 0.0   # von nach gedeckeltem bis


def test_randwochen_zaehlen_nur_tage_im_zeitraum():
    d = auswerten([z("2026-11-01", 2), z("2026-11-02", 3)], "2026-11-02", None)     # So KW 44, Mo KW 45
    assert d["std"] == 3.0 and [w["kw"] for w in d["wochen"]] == [45]


def test_tage_je_woche_und_monat_zaehlen_tage_nicht_eintraege():
    d = auswerten([z("2026-11-02", 3), z("2026-11-02", 2), z("2026-11-03", 1)], None, None)
    assert [(w["kw"], w["std"], w["tage"]) for w in d["wochen"]] == [(45, 6.0, 2)]
    assert d["monate"] == [{"jahr": 2026, "monat": 11, "std": 6.0, "tage": 2}]


def test_auswerten_ordnet_unsortierte_zeilen():
    d = auswerten([z("2026-12-01", 1), z("2026-11-02", 1), z("2026-11-20", 1)], None, None)
    assert (d["von"], d["bis"]) == ("2026-11-02", "2026-12-01")
    assert [t["tag"] for t in d["tage"]] == ["2026-11-02", "2026-11-20", "2026-12-01"]


def test_projekte_sind_sortiert():
    d = auswerten([z("2026-11-02", 1, projekt="Zaun"), z("2026-11-03", 1, projekt="Anbau")], None, None)
    assert d["projekte"] == ["Anbau", "Zaun"]                       # nicht in der Reihenfolge der Zeilen


def test_monat_nach_kalenderdatum_woche_nach_iso_jahr():
    d = auswerten([z("2027-01-01", 5)], None, None)               # Freitag, KW 53 / 2026
    assert [(w["jahr"], w["kw"]) for w in d["wochen"]] == [(2026, 53)]
    assert [(m["jahr"], m["monat"]) for m in d["monate"]] == [(2027, 1)]


def test_montag_im_dezember_kann_zur_kw_1_des_folgejahres_gehoeren():
    woche = auswerten([z("2024-12-31", 2)], None, None)["wochen"][0]                  # ein Dienstag
    assert (woche["montag"], woche["kw"], woche["jahr"]) == ("2024-12-30", 1, 2025)


def test_stundenwerte_sind_immer_float():
    # bericht.zahl formatiert int und float ab 1000 verschieden ("1000" gegen "1.000"): nie einen int liefern.
    def stundenwerte(d):
        return [d["std"], d["std_pro_woche"], d["std_pro_arbeitswoche"],
                *(x["std"] for x in d["wochen"] + d["monate"] + d["tage"])]

    mit_leerer_woche = auswerten([z("2026-11-02", 8), z("2026-11-03", 6.5), z("2026-11-16", 4)], None, None)
    nur_ints = auswerten([dict(z("2026-11-02", 8), std=8), dict(z("2026-11-03", 4), std=4)], None, None)
    assert [w["tage"] for w in mit_leerer_woche["wochen"]] == [2, 0, 1]            # KW 46 bleibt leer
    for d in (mit_leerer_woche, nur_ints):
        assert {type(x) for x in stundenwerte(d)} == {float}
    leer = auswerten([], None, None)
    assert {type(leer[k]) for k in ("std", "std_pro_woche", "std_pro_arbeitswoche")} == {float}


def test_rundung_wie_sqlite_bei_exakter_grenze():
    # 0,125 + 1,0 = 1,125 ist eine exakte Hälfte: SQLite ROUND gibt 1,13, Python-round() zur geraden Ziffer 1,12.
    d = auswerten([z("2026-11-02", 0.125), z("2026-11-03", 1.0)], None, None)
    assert d["std"] == 1.13 and d["tage"][0]["std"] == 0.13
    assert d["wochen"][0]["std"] == d["monate"][0]["std"] == 1.13                    # Wochen- und Monatssumme ebenso
    # Summen entstehen aus den ungerundeten Werten und werden einmal gerundet (0,125 + 0,125 = 0,25, nicht 0,13 + 0,13);
    # die Durchschnitte runden ebenso: 0,25 h / 2 Wochen = 0,125 -> 0,13.
    mittel = auswerten([z("2026-11-02", 0.125), z("2026-11-09", 0.125)], None, None)
    assert mittel["std"] == mittel["monate"][0]["std"] == 0.25
    assert (mittel["std_pro_woche"], mittel["std_pro_arbeitswoche"]) == (0.13, 0.13)
    assert auswerten([z("2026-11-02", 1.124)], None, None)["std"] == 1.12            # unter der Hälfte wird abgerundet


def test_rundung_wie_die_anzeige_der_oberflaeche_bei_2_675():
    # R7: gerundet wird die kürzeste Dezimaldarstellung, wie es zahl() in der Oberfläche anzeigt: 2,675 -> 2,68. SQLite ROUND
    # rechnet mit dem float knapp unter der Hälfte und gibt 2,67; bei solchen Dreistellern widersprechen sich App-SQL und -Anzeige selbst.
    assert auswerten([z("2026-11-02", 2.675)], None, None)["std"] == 2.68


# ---------------------------------------------------------------------------
# Daten holen (SQL)
# ---------------------------------------------------------------------------

def test_zeilen_holen_umfang_schreibweise_sortierung(w):
    w.stunden("Anna", 5, "2026-11-03"); w.stunden("Ben", 2, "2026-11-03")
    w.stunden("Anna", 4, "2026-11-02", task_id=zweites_projekt())
    with closing(db.get_conn()) as c:
        assert [(r["tag"], r["std"], r["aufgabe"], r["projekt"], r["person"]) for r in zeilen_holen(c, "aNNa", w.pid, None, None)] \
            == [("2026-11-03", 5.0, "Ausbau", "Dachbodenausbau", "Anna")]
        assert [r["tag"] for r in zeilen_holen(c, "Anna", None, None, None)] == ["2026-11-02", "2026-11-03"]


def test_zeilen_holen_tag_ohne_arbeitsdatum(w):                    # Review Focus 3
    a, b = w.stunden("Anna", 2, "2026-01-01"), w.stunden("Anna", 3, "2026-01-01")
    with closing(db.get_conn()) as c:
        c.execute("UPDATE time_entries SET work_date=NULL, created_at='2026-11-10T12:00:00+00:00' WHERE id=?", (a,))
        c.execute("UPDATE time_entries SET work_date='',   created_at='2026-11-12T12:00:00+00:00' WHERE id=?", (b,)); c.commit()
        assert [r["tag"] for r in zeilen_holen(c, "Anna", w.pid, None, None)] == ["2026-11-10", "2026-11-12"]


def test_zeilen_holen_von_bis_gelten_auch_fuer_eintraege_ohne_arbeitsdatum(w):
    a, b = w.stunden("Anna", 2, "2026-01-01"), w.stunden("Anna", 3, "2026-01-01")        # Tage wie im Test oben: 10. und 12.11.
    with closing(db.get_conn()) as c:
        c.execute("UPDATE time_entries SET work_date=NULL, created_at='2026-11-10T12:00:00+00:00' WHERE id=?", (a,))
        c.execute("UPDATE time_entries SET work_date='',   created_at='2026-11-12T12:00:00+00:00' WHERE id=?", (b,)); c.commit()
        assert [r["tag"] for r in zeilen_holen(c, "Anna", w.pid, "2026-11-11", "2026-11-12")] == ["2026-11-12"]
        assert [r["tag"] for r in zeilen_holen(c, "Anna", w.pid, "2026-11-10", "2026-11-10")] == ["2026-11-10"]


def test_zeilen_holen_von_bis_sind_inklusive(w):
    for tag in ("2026-11-02", "2026-11-03", "2026-11-04"): w.stunden("Anna", 1, tag)
    with closing(db.get_conn()) as c:
        assert [r["tag"] for r in zeilen_holen(c, "Anna", w.pid, "2026-11-03", "2026-11-03")] == ["2026-11-03"]


def test_summe_stimmt_mit_der_app_ueberein(w):                     # Kernzusage des Spec: gleiche Zahl wie Abrechnen/Projektsumme
    w.person("Anna"); w.stunden("Anna", 2.5, "2026-11-02"); w.stunden("Anna", 3, "2026-11-03", billed=0)
    w.stunden("Anna", 1.25, "2026-11-04", task_id=w.aufgabe("Bad"))
    with closing(db.get_conn()) as c:
        app = {r["person_name"]: r["stunden"] for r in db.dashboard_data(w.pid)["by_person"]}
        assert auswerten(zeilen_holen(c, "Anna", w.pid, None, None), None, None)["std"] == app["Anna"] == 6.75


def test_zeilen_holen_gleicher_tag_nach_eintrags_id(w):
    bad = w.aufgabe("Bad")
    w.stunden("Anna", 1, "2026-11-02", task_id=bad); w.stunden("Anna", 2, "2026-11-02"); w.stunden("Anna", 3, "2026-11-02", task_id=bad)
    with closing(db.get_conn()) as c:
        assert [r["std"] for r in zeilen_holen(c, "Anna", w.pid, None, None)] == [1.0, 2.0, 3.0]


def test_summe_stimmt_auch_bei_rundungsgrenze_mit_der_app_ueberein(w):   # 1,125 h: SQLite ROUND gibt 1,13, Python-round() 1,12
    w.person("Anna"); w.stunden("Anna", 0.125, "2026-11-02"); w.stunden("Anna", 1.0, "2026-11-03")
    with closing(db.get_conn()) as c:
        app = {r["person_name"]: r["stunden"] for r in db.dashboard_data(w.pid)["by_person"]}
        assert auswerten(zeilen_holen(c, "Anna", w.pid, None, None), None, None)["std"] == app["Anna"] == 1.13


# ---------------------------------------------------------------------------
# Bericht: HTML, Diagramme, PDF-main
# ---------------------------------------------------------------------------

d = auswerten([z("2026-11-02", 8), z("2026-11-03", 6.5), z("2026-11-10", 4), z("2026-11-26", 7.5)],
              None, None, heute="2026-11-30")             # KW 45: 14,5 · KW 46: 4 · KW 47 leer · KW 48: 7,5
SVG = "{http://www.w3.org/2000/svg}"


def svg_wurzel(text):
    """Parst ein Diagramm als XML — WeasyPrint nimmt nur wohlgeformtes SVG an."""
    wurzel = ET.fromstring(text)
    assert wurzel.tag == SVG + "svg"
    return wurzel


def svg_texte(text):
    return [t.text for t in svg_wurzel(text).iter(SVG + "text")]


def test_html_kennzahlen_diagramme_und_wochentabelle():
    h = html_stundenzettel(d, "Projekt A", True, "25.11.2026, 13:40")
    for text in ("STUNDENZETTEL", "Anna", "Projekt A", "02.11.2026 – 26.11.2026", "Arbeitstage", "(4 Wochen)",
                 "(3 Wochen mit Arbeit)", "KW 45 · 02.–08.11.", "14,5", "Mo 02.11.", "Allgemein", "Seite "):
        assert text in h
    assert h.count("<svg") == 2 and "stroke-dasharray" in h and "Ø 6,5" in h and "8,7" in h and "8,67" not in h


def test_html_durchschnitt_wird_nur_einmal_gerundet():
    # 34,58 Std. in 4 Wochen = 8,645 -> eine Stelle: „8,6“. Über den gespeicherten Wert 8,65 (zwei Stellen) käme „8,7“, und wer
    # die gedruckten Zahlen teilt, käme auf 8,6: eine Zahl, die abweicht, ist ein Anti-Ziel des Spec.
    x = auswerten([z("2026-11-02", 8.58), z("2026-11-09", 8), z("2026-11-16", 9), z("2026-11-23", 9)], None, None)
    assert x["std"] == 34.58 and x["std_pro_woche"] == 8.65                    # so wird gespeichert (zwei Stellen)
    h = html_stundenzettel(x, "P", False, "x")
    assert h.count("8,6 Std.") == 2 and "Ø 8,6" in h and "8,7" not in h       # beide Kennzahlen und die Linie im Diagramm


def test_html_tagesliste_projektspalte_und_leerer_fall():
    assert "Einzelne Tage" in html_stundenzettel(d, "P", True, "x") and "Einzelne Tage" not in html_stundenzettel(d, "P", False, "x")
    assert "<th>Projekt</th>" in html_stundenzettel(d, "Alle Projekte", True, "x", alle_projekte=True)
    assert "<th>Projekt</th>" not in html_stundenzettel(d, "Projekt A", True, "x")
    assert "Keine Stunden im Zeitraum" in html_stundenzettel(auswerten([], None, None), "P", True, "x")


def test_html_einzahl_bei_einer_woche():                           # Review Focus 1 (Darstellung)
    h = html_stundenzettel(auswerten([z("2026-11-04", 6)], None, None), "P", True, "x")
    assert "(1 Woche)" in h and "(1 Woche mit Arbeit)" in h and "(1 Wochen" not in h


def test_html_maskiert_sonderzeichen():                            # Review Focus 5
    x = auswerten([z("2026-11-02", 8, person="<b>Özgür</b> & O'Brien")], None, None)
    h = html_stundenzettel(x, "P", True, "x")
    assert "&lt;b&gt;Özgür&lt;/b&gt; &amp; O&#x27;Brien" in h and "<b>Özgür" not in h


def test_html_maskiert_auch_aufgabe_projekt_und_erstellt():       # Review Focus 5
    # Mit WeasyPrint ist ungeprüftes Markup mehr als Kosmetik: ein <img src="file:///…"> im Aufgabentitel holte eine lokale Datei ins PDF.
    def gift(feld):                                                # Sonderzeichen, Markup und ein Bild; „feld“ zeigt, wo es gelandet ist
        return "<b>Özgür</b> & O'Brien " + f'<img src="file:///{feld}">'

    def maskiert(feld):
        return f"&lt;b&gt;Özgür&lt;/b&gt; &amp; O&#x27;Brien &lt;img src=&quot;file:///{feld}&quot;&gt;"

    x = auswerten([z("2026-11-02", 8, aufgabe=gift("aufgabe"), projekt=gift("projekt"))], None, None)
    h = html_stundenzettel(x, gift("kopf"), True, gift("erstellt"), alle_projekte=True)
    assert "<b>" not in h and "<img" not in h
    assert f"<td>{maskiert('aufgabe')}</td>" in h                      # Aufgabe in der Tagestabelle
    assert f"<td>{maskiert('projekt')}</td>" in h                      # Zelle der Projektspalte
    assert f"({maskiert('projekt')})" in h                             # Projektliste im Kopf
    assert f"Projekt: {maskiert('kopf')} (" in h                       # Projektzeile (der Parameter projekt)
    assert f"Erstellt {maskiert('erstellt')}</div>" in h               # Erstellt-Zeile
    assert '<td colspan="3">Summe Nov 2026</td>' in h                  # mit Projektspalte stehen drei Spalten vor „Std.“


def test_wochen_svg_drei_jahre_bleibt_lesbar():                    # Review Focus 4
    x = auswerten([z("2024-01-01", 8), z("2026-12-28", 8)], None, None)
    svg = wochen_svg(x["wochen"], x["std_pro_woche"])
    assert x["wochen_im_zeitraum"] > 150 and svg.count("<text") <= 45 and len(svg) < 60_000


def test_monate_svg_nur_monate_mit_stunden():
    x = auswerten([z("2026-08-10", 4), z("2026-11-02", 6)], None, None)
    svg = monate_svg(x["monate"])
    assert "Aug 2026" in svg and "Nov 2026" in svg and "Sep 2026" not in svg


def test_main_liest_stdin_und_schreibt_pdf(tmp_path, monkeypatch):   # WeasyPrint durch Attrappe ersetzt
    gesehen = {}
    class Doc:
        def __init__(self, string, base_url=None): gesehen["html"] = string
        def write_pdf(self, ziel): pathlib.Path(ziel).write_bytes(b"%PDF-1.4 attrappe")
    monkeypatch.setitem(sys.modules, "weasyprint", types.SimpleNamespace(HTML=Doc))
    ziel = tmp_path / "x.pdf"
    monkeypatch.setattr(sys, "argv", ["stundenzettel.py", str(ziel)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"daten": d, "projekt": "P", "mit_tagen": True, "erstellt": "x", "alle_projekte": False})))
    stundenzettel.main()
    assert ziel.read_bytes().startswith(b"%PDF") and "Anna" in gesehen["html"]


def test_svgs_sind_wohlgeformt_und_leer_robust():
    lang = auswerten([z("2024-01-01", 8), z("2026-12-28", 8)], None, None)                  # 157 Wochen
    for x in (d, lang):
        svg_wurzel(wochen_svg(x["wochen"], x["std_pro_woche"]))
        svg_wurzel(monate_svg(x["monate"]))
    svg_wurzel(wochen_svg([], 0.0))                                                         # ohne Stunden: kein Fehler,
    svg_wurzel(monate_svg([]))                                                              # trotzdem gültiges SVG


def test_wochen_svg_saeulen_nur_mit_stunden_und_runde_achse():
    svg = wochen_svg(d["wochen"], d["std_pro_woche"])
    saeulen = [float(r.get("height")) for r in svg_wurzel(svg).iter(SVG + "rect") if r.get("fill") == GRUEN]
    assert len(saeulen) == 3 and saeulen[0] > saeulen[2] > saeulen[1]       # KW 45 (14,5) > KW 48 (7,5) > KW 46 (4), KW 47 leer
    assert {"0", "5", "10", "15"} <= set(svg_texte(svg))                    # rundes Maximum 15 statt 14,5


def test_wochen_svg_zahlen_ueber_den_saeulen_nur_wenn_platz_ist():
    wenige = auswerten([z("2026-11-02", 7.5)], None, None)
    viele = auswerten([z("2024-01-01", 7.5), z("2026-12-28", 7.5)], None, None)             # 157 Wochen
    assert "7,5" in svg_texte(wochen_svg(wenige["wochen"], wenige["std_pro_woche"]))
    assert "7,5" not in svg_texte(wochen_svg(viele["wochen"], viele["std_pro_woche"]))


def test_wochen_svg_zahlen_ueber_den_saeulen_bis_17_wochen():
    # Die Fläche ist 560 breit: 17 Wochen sind je 32,9, 18 je 31,1 breit. Fünfstellige Zahlen („12,25“) brauchen ~29 und
    # dürfen sich nicht berühren, deshalb stehen die Zahlen bis 17 Wochen, ab 18 nicht mehr.
    def zahlen_ueber_den_saeulen(wochen):
        x = auswerten([z((date(2026, 1, 5) + timedelta(weeks=i)).isoformat(), 12.25) for i in range(wochen)], None, None)
        return svg_texte(wochen_svg(x["wochen"], x["std_pro_woche"])).count("12,25")

    assert zahlen_ueber_den_saeulen(17) == 17 and zahlen_ueber_den_saeulen(18) == 0


def test_wochen_svg_zahl_weicht_der_durchschnittslinie_aus():
    x = auswerten([z("2026-11-02", 10), z("2026-11-09", 10), z("2026-11-16", 9.5), z("2026-11-23", 10)], None, None)
    wurzel = svg_wurzel(wochen_svg(x["wochen"], x["std_pro_woche"]))          # Ø 9,88 liegt knapp über der Säule mit 9,5
    ys_linie = float(next(t for t in wurzel.iter(SVG + "line") if t.get("stroke-dasharray")).get("y1"))
    ys_zahl = float(next(t for t in wurzel.iter(SVG + "text") if t.text == "9,5").get("y"))
    assert ys_zahl < ys_linie                                                 # die Zahl steht über der Linie, nicht mittendrin


def test_wochen_svg_jahreszahlen_je_jahr_und_ohne_ueberdeckung():
    drei = auswerten([z("2024-01-01", 8), z("2026-12-28", 8)], None, None)
    assert {"2024", "2025", "2026"} <= set(svg_texte(wochen_svg(drei["wochen"], drei["std_pro_woche"])))
    knapp = auswerten([z("2024-12-23", 8), z("2026-12-28", 8)], None, None)   # nur KW 52/2024 als Anfangsstück, dann zwei volle Jahre
    texte = svg_texte(wochen_svg(knapp["wochen"], knapp["std_pro_woche"]))
    assert "2025" in texte and "2026" in texte and "2024" not in texte        # die eine Woche von 2024 ist zu schmal für eine Zahl


def test_wochen_svg_das_hauptjahr_behaelt_seine_jahreszahl():
    def jahreszahlen(von, bis):                                    # die Jahreszahlen unter der Achse als (Text, x), von Montag `von` bis `bis`
        x = auswerten([z(von, 8), z(bis, 8)], None, None)
        texte = svg_wurzel(wochen_svg(x["wochen"], x["std_pro_woche"])).iter(SVG + "text")
        return [(t.text, float(t.get("x"))) for t in texte if re.fullmatch(r"20\d\d", t.text)]

    faelle = {("2025-12-22", "2026-12-28"): ["2026"],              # KW 52/2025 allein ist zu schmal, die 53 Wochen von 2026 tragen die Zahl
              ("2025-03-03", "2026-01-05"): ["2025"],              # ebenso vor einem Endstück (KW 1-2/2026)
              ("2024-12-23", "2026-12-28"): ["2025", "2026"],
              ("2024-01-01", "2026-12-28"): ["2024", "2025", "2026"],
              ("2026-03-02", "2026-12-28"): []}                    # nur ein Jahr: keine Jahreszeile
    for (von, bis), erwartet in faelle.items():
        paare = jahreszahlen(von, bis)
        assert [jahr for jahr, _ in paare] == erwartet, (von, bis)
        assert all(b - a >= stundenzettel._W_JAHR_PLATZ for (_, a), (_, b) in zip(paare, paare[1:])), (von, bis)   # sie überdecken sich nie


def test_wochen_svg_jahreszahl_steht_mittig_unter_den_wochen_des_jahres():
    # Unter der ersten Woche läge „2026“ direkt unter der Marke „52“ (KW 52/2025) und läse sich wie „KW 52, 2026“.
    x = auswerten([z((date(2025, 12, 22) + timedelta(weeks=i)).isoformat(), 8) for i in range(54)], None, None)   # KW 52/2025, dann ganz 2026
    wurzel = svg_wurzel(wochen_svg(x["wochen"], x["std_pro_woche"]))
    saeulen = [(float(r.get("x")), float(r.get("width"))) for r in wurzel.iter(SVG + "rect") if r.get("fill") == GRUEN]
    mitte_2026 = (saeulen[1][0] + saeulen[-1][0] + saeulen[-1][1]) / 2                # linke Kante der 2. bis rechte der letzten Säule
    jahr = next(t for t in wurzel.iter(SVG + "text") if t.text == "2026")
    assert len(saeulen) == 54 and abs(float(jahr.get("x")) - mitte_2026) < 0.2 and jahr.get("text-anchor") == "middle"


def test_html_ist_ein_vollstaendiges_dokument_mit_maskiertem_titel():
    h = html_stundenzettel(auswerten([z("2026-11-02", 8, person="A&B")], None, None), "P", True, "x")
    assert h.startswith("<!DOCTYPE html>") and '<meta charset="utf-8">' in h and "<title>Stundenzettel – A&amp;B</title>" in h


def test_html_wochentabelle_nur_wochen_mit_stunden_auch_ueber_monatsgrenzen():
    assert "KW 53 · 28.12.–03.01." in html_stundenzettel(auswerten([z("2026-12-28", 5)], None, None), "P", False, "x")
    h = html_stundenzettel(d, "P", False, "x")
    assert "KW 46 · 09.–15.11." in h and "KW 47" not in h                   # die leere Woche steht nur im Diagramm


def test_html_wochentabelle_nennt_das_jahr_an_der_kw_nur_ueber_mehrere_jahre():
    h = html_stundenzettel(auswerten([z("2026-12-28", 5), z("2027-01-04", 3)], None, None), "P", False, "x")
    assert "KW 53/2026 · 28.12.–03.01." in h and "KW 1/2027 · 04.–10.01." in h     # innerhalb eines Monats wie sonst nur „04.–10.01.“
    assert "KW 53 · 28.12.–03.01." not in h and "KW 1 · 04.–10.01." not in h      # früher las sich das Jahr hinter „03.01.“ wie 2026


def test_html_tagesliste_hat_je_monat_eine_summenzeile():
    x = auswerten([z("2026-10-30", 2), z("2026-11-02", 3, "Bad"), z("2026-11-03", 1.5)], None, None)
    h = html_stundenzettel(x, "P", True, "x")
    assert h.index("Fr 30.10.") < h.index("Summe Okt 2026") < h.index("Mo 02.11.") < h.index("Summe Nov 2026")
    assert 'Summe Okt 2026</td><td class="r">2</td>' in h and 'Summe Nov 2026</td><td class="r">4,5</td>' in h


def test_r2_mit_einer_stelle_rundet_exakte_haelften_nach_oben():       # Ø-Anzeige: 8,67 -> 8,7 und 0,25 -> 0,3 (Python-round: 0,2)
    assert (_r2(8.67, 1), _r2(0.25, 1), _r2(1.125)) == (8.7, 0.3, 1.13)


# ---------------------------------------------------------------------------
# Endpunkt GET /api/projects/<pid>/stundenzettel.pdf (der Unterprozess ist durch eine Attrappe ersetzt)
# ---------------------------------------------------------------------------

@pytest.fixture
def attrappe(monkeypatch):
    """Ersetzt subprocess.run: kein WeasyPrint, kein /usr/bin/python3. Merkt sich Aufruf, Argumente und stdin."""
    a = types.SimpleNamespace(aufrufe=0, cmd=None, input=None, kw=None, returncode=0, stderr="")
    def lauf(cmd, input=None, **kw):
        a.aufrufe += 1; a.cmd, a.input, a.kw = cmd, input, kw
        if a.returncode == 0 and cmd[-1].endswith(".pdf"):             # nie eine andere Datei überschreiben (z. B. das Skript)
            pathlib.Path(cmd[-1]).write_bytes(b"%PDF-1.4 attrappe")
        return types.SimpleNamespace(returncode=a.returncode, stdout="", stderr=a.stderr)
    monkeypatch.setattr(subprocess, "run", lauf)
    return a

URL = "/api/projects/{pid}/stundenzettel.pdf"

def test_endpunkt_liefert_pdf_und_reicht_daten_weiter(w, attrappe):
    w.stunden("Anna", 8, "2026-11-02"); w.stunden("Anna", 6.5, "2026-11-03")
    r = w.client.get(URL.format(pid=w.pid) + "?person=anna&von=2026-11-01&bis=2026-11-20&tage=1")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF")
    assert 'filename="Stundenzettel_Anna_2026-11-01_2026-11-20.pdf"' in r.headers["Content-Disposition"]
    p = json.loads(attrappe.input)
    assert p["daten"]["std"] == 14.5 and p["projekt"] == "Dachbodenausbau"
    assert p["mit_tagen"] is True and p["alle_projekte"] is False

def test_endpunkt_alle_projekte_und_ohne_tage(w, attrappe):
    w.stunden("Anna", 8, "2026-11-02"); w.stunden("Anna", 4, "2026-11-10", task_id=zweites_projekt())
    r = w.client.get(URL.format(pid=w.pid) + "?person=Anna&alle=1&tage=0")
    p = json.loads(attrappe.input)
    assert r.status_code == 200 and p["projekt"] == "Alle Projekte" and p["alle_projekte"] is True
    assert p["mit_tagen"] is False and p["daten"]["std"] == 12.0 and p["daten"]["projekte"] == ["Dachbodenausbau", "Gartenhaus"]

def test_endpunkt_fehlerfaelle_starten_keinen_unterprozess(w, attrappe):
    w.stunden("Anna", 3, "2026-11-02")
    assert w.client.get("/api/projects/9999/stundenzettel.pdf?person=Anna").status_code == 404
    for q in ("", "?person=%20", "?person=Anna&von=2026-13-01", "?person=Anna&von=20261101",
              "?person=Anna&von=2026-11-30&bis=2026-11-01", "?person=Anna&von=0001-01-01&bis=2026-11-20"):
        assert w.client.get(URL.format(pid=w.pid) + q).status_code == 400, q
    r = w.client.get(URL.format(pid=w.pid) + "?person=Niemand")
    assert r.status_code == 404 and "Keine Stunden von 'Niemand'" in r.get_json()["error"]
    assert attrappe.aufrufe == 0

def test_endpunkt_sonderzeichen_im_namen(w, attrappe):               # Review Focus 5
    w.stunden("Özgür & O'Brien", 3, "2026-11-02")
    r = w.client.get(URL.format(pid=w.pid), query_string={"person": "Özgür & O'Brien"})
    name = re.search(r'filename="([^"]+)"', r.headers["Content-Disposition"]).group(1)
    assert r.status_code == 200 and re.fullmatch(r"[A-Za-z0-9._-]+", name)
    assert json.loads(attrappe.input)["daten"]["person"] == "Özgür & O'Brien"

def test_endpunkt_unterprozess_fehler_gibt_500_ohne_details(w, attrappe):
    w.stunden("Anna", 3, "2026-11-02"); attrappe.returncode = 1; attrappe.stderr = "Traceback geheim"
    r = w.client.get(URL.format(pid=w.pid) + "?person=Anna")
    assert r.status_code == 500 and "Traceback" not in r.get_data(as_text=True)

def test_endpunkt_unterprozess_fehler_nennt_den_rueckgabecode_im_log(w, attrappe, caplog):
    w.stunden("Anna", 3, "2026-11-02"); attrappe.returncode = 3; attrappe.stderr = "Traceback geheim"
    with caplog.at_level("ERROR"):
        r = w.client.get(URL.format(pid=w.pid) + "?person=Anna")
    assert r.status_code == 500
    assert any("Rückgabecode 3" in m.getMessage() and "Traceback geheim" in m.getMessage() for m in caplog.records)


# Zusatztests: was die Tests oben nicht sehen — der Vertrag mit dem Unterprozess, die übrigen Ausfälle, die Fehlertexte,
# die Grenze von 520 Wochen, der Geltungsbereich und der Dateiname aus dem effektiven Zeitraum.

def test_endpunkt_ruft_den_unterprozess_wie_vereinbart_auf(w, attrappe):
    w.stunden("Özgür", 3, "2026-11-02")
    r = w.client.get(URL.format(pid=w.pid), query_string={"person": "Özgür"})
    assert r.status_code == 200 and attrappe.aufrufe == 1
    cmd, kw = attrappe.cmd, attrappe.kw
    assert len(cmd) == 3 and cmd[0] == "/usr/bin/python3" and cmd[2].endswith(".pdf")          # das Ziel ist das letzte Argument
    assert pathlib.Path(cmd[1]).samefile(stundenzettel.__file__)                              # das Skript liegt neben server.py
    assert (kw["capture_output"], kw["text"], kw["timeout"]) == (True, True, 120)
    p = json.loads(attrappe.input)
    assert sorted(p) == ["alle_projekte", "daten", "erstellt", "mit_tagen", "projekt"]
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}", p["erstellt"])
    assert attrappe.input.isascii() and p["daten"]["person"] == "Özgür"                       # \u-Escapes: unabhängig von der Kodierung

@pytest.mark.parametrize("ausfall", [OSError("python3 geheim"), subprocess.TimeoutExpired("python3 geheim", 120), None],
                         ids=["OSError", "Timeout", "keine_Datei"])
def test_endpunkt_weitere_unterprozess_ausfaelle_geben_500_ohne_details(w, monkeypatch, ausfall):
    w.stunden("Anna", 3, "2026-11-02")
    def lauf(cmd, **kw):
        if ausfall: raise ausfall
        return types.SimpleNamespace(returncode=0, stdout="", stderr="Traceback geheim")      # „erfolgreich“, aber ohne Datei
    monkeypatch.setattr(subprocess, "run", lauf)
    r = w.client.get(URL.format(pid=w.pid) + "?person=Anna")
    assert r.status_code == 500 and r.get_json() == {"error": "Stundenzettel konnte nicht erstellt werden."}

def test_endpunkt_fehlertexte_und_pruefreihenfolge(w, attrappe):
    w.stunden("Anna", 3, "2026-11-02")
    r = w.client.get("/api/projects/9999/stundenzettel.pdf")                                  # das Projekt kommt vor der Person
    assert r.status_code == 404 and r.get_json()["error"] == "Projekt nicht gefunden."
    for q, text in (("", "'person'"), ("?person=%20", "'person'"), ("?person=Anna&von=2026-13-01", "'von'"),
                    ("?person=Anna&bis=2026-11-31", "'bis'"), ("?person=Anna&von=20261101", "'von'"),
                    ("?person=Anna&von=2026-11-30&bis=2026-11-01", "Von liegt nach Bis."),
                    ("?person=Anna&von=0001-01-01&bis=2026-11-20",
                     "Der Zeitraum ist zu groß (höchstens 10 Jahre). Bitte Von/Bis einschränken."),
                    ("?von=2026-13-01", "'person'"),                                           # die Person kommt vor den Daten
                    ("?person=Niemand&von=2026-11-30&bis=2026-11-01", "Von liegt nach Bis.")):  # von nach bis kommt vor „keine Stunden“
        r = w.client.get(URL.format(pid=w.pid) + q)
        assert r.status_code == 400 and text in r.get_json()["error"], q
    r = w.client.get(URL.format(pid=w.pid) + "?person=Niemand&von=0001-01-01&bis=2026-11-20")  # ohne Stunden ist die Auswertung leer: 404,
    assert r.status_code == 404 and "Keine Stunden von 'Niemand'" in r.get_json()["error"]    # auch bei riesigem Zeitraum
    assert attrappe.aufrufe == 0

def test_endpunkt_stunden_nur_nach_heute_sind_keine_stunden_im_zeitraum(w, attrappe):       # R3: entschieden an der Auswertung
    w.stunden("Anna", 3, "2099-01-05")
    with closing(db.get_conn()) as c:
        assert zeilen_holen(c, "Anna", w.pid, "2099-01-01", "2099-12-31")                     # die Zeile gibt es, auswerten verwirft sie
    r = w.client.get(URL.format(pid=w.pid) + "?person=Anna&von=2099-01-01&bis=2099-12-31")
    assert r.status_code == 404 and "Keine Stunden von 'Anna'" in r.get_json()["error"] and attrappe.aufrufe == 0

def test_endpunkt_zeitraum_hoechstens_520_wochen(w, attrappe):
    w.stunden("Anna", 2, "2026-11-02")                                                        # ein Montag
    bis = date(2026, 11, 2)
    r = w.client.get(URL.format(pid=w.pid) + f"?person=Anna&bis=2026-11-02&von={bis - timedelta(weeks=520)}")   # 521 Wochen
    assert r.status_code == 400 and attrappe.aufrufe == 0
    assert r.get_json()["error"] == "Der Zeitraum ist zu groß (höchstens 10 Jahre). Bitte Von/Bis einschränken."
    r = w.client.get(URL.format(pid=w.pid) + f"?person=Anna&bis=2026-11-02&von={bis - timedelta(weeks=519)}")   # genau 520 Wochen
    assert r.status_code == 200 and json.loads(attrappe.input)["daten"]["wochen_im_zeitraum"] == 520

def test_endpunkt_geltungsbereich_leere_grenzen_und_effektiver_zeitraum(w, attrappe):
    w.stunden("Anna", 8, "2026-11-02"); w.stunden("Anna", 4, "2026-11-10"); w.stunden("Anna", 5, "2026-11-12", task_id=zweites_projekt())
    r = w.client.get(URL.format(pid=w.pid) + "?person=Anna&von=&bis=")                        # leer = nicht angegeben, ohne alle: nur dieses Projekt
    p = json.loads(attrappe.input)
    assert p["daten"]["std"] == 12.0 and p["daten"]["projekte"] == ["Dachbodenausbau"] and p["mit_tagen"] is True    # tage fehlt: 1
    assert 'filename="Stundenzettel_Anna_2026-11-02_2026-11-10.pdf"' in r.headers["Content-Disposition"]             # Zeitraum aus den Daten
    r = w.client.get(URL.format(pid=w.pid) + "?person=Anna&von=2026-10-01&bis=2099-12-31")    # ein bis nach heute endet heute
    assert f'filename="Stundenzettel_Anna_2026-10-01_{date.today().isoformat()}.pdf"' in r.headers["Content-Disposition"]
