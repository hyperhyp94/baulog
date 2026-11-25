"""
stundenzettel.py — Stundenzettel je Person: wie viele Stunden pro Woche und Monat gearbeitet wurden
(Summen, Ø pro Woche, optional die einzelnen Tage) als PDF-Bericht, ohne Beträge.

Aufbau: zeilen_holen (Daten, nur lesend) → auswerten (Wochen, Monate, Durchschnitte) → html_stundenzettel
mit den Diagrammen wochen_svg und monate_svg → main (PDF). Bis auf main sind das reine Python-Funktionen
ohne WeasyPrint und deshalb auch unter Windows testbar. main läuft bewusst mit dem System-Python (dort ist
WeasyPrint installiert), wie bericht.py:
    python3 stundenzettel.py <ziel.pdf>        (Angaben als JSON auf stdin, siehe main)
"""

import json
import math
import os
import sqlite3
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby

from bericht import DUNKEL, GRAU, GRUEN, datum, e, zahl

BASIS = os.path.dirname(os.path.abspath(__file__))

# Der „Tag“ eines Eintrags wie in der Oberfläche: das Arbeitsdatum, sonst das lokale Erfassungsdatum.
_TAG_SQL = "COALESCE(NULLIF(te.work_date, ''), date(te.created_at, 'localtime'))"


def zeilen_holen(conn: sqlite3.Connection, person: str, projekt_id: int | None,
                 von: str | None, bis: str | None) -> list[dict]:
    """
    Alle Stundeneinträge einer Person (über Aufgabe → Baustelle → Projekt), nach Tag und ID sortiert.

    Der Name wird wie bei Auszahlung und Guthaben (Leerzeichen am Rand abgeschnitten) ohne Groß-/
    Kleinschreibung verglichen, und zwar in SQL (SQLite lowert nur ASCII, auf beiden Seiten gleich).
    ``projekt_id=None`` = alle Projekte;
    ``von``/``bis`` (ISO, inklusive) filtern auf den Tag des Eintrags. Es zählen alle Einträge
    (mit und ohne Rechnung), so wie ``SUM(te.hours)`` in Abrechnen und Projektsumme.

    Zeile: {"tag": "YYYY-MM-DD", "std": float, "aufgabe": Aufgabentitel, "projekt": Projektname,
    "person": Name, wie gespeichert}.
    """
    sql = (f"SELECT {_TAG_SQL}, te.hours, t.title, p.name, te.person "
           "FROM time_entries te JOIN tasks t ON t.id = te.task_id "
           "JOIN sites s ON s.id = t.site_id JOIN projects p ON p.id = s.project_id "
           "WHERE lower(te.person) = lower(?)")
    params: list = [person.strip()]
    if projekt_id is not None:
        sql += " AND s.project_id = ?"
        params.append(projekt_id)
    if von:
        sql += f" AND {_TAG_SQL} >= ?"
        params.append(von)
    if bis:
        sql += f" AND {_TAG_SQL} <= ?"
        params.append(bis)
    sql += f" ORDER BY {_TAG_SQL}, te.id"
    # Spalten nach Position lesen: unabhängig davon, ob die Verbindung sqlite3.Row liefert.
    return [{"tag": tag, "std": float(std), "aufgabe": aufgabe, "projekt": projekt, "person": name}
            for tag, std, aufgabe, projekt, name in conn.execute(sql, params)]


def _r2(x, stellen: int = 2) -> float:
    """
    Auf zwei Stellen runden wie die App: exakte Hälften nach oben (SQLite ``ROUND``, Anzeige der
    Oberfläche), nicht zur geraden Ziffer wie Python-``round()`` (1,125 -> 1,13 statt 1,12). Gerundet
    wird die kürzeste Dezimaldarstellung des floats (``repr``), so wie es ``zahl()`` in der Oberfläche
    tut (2,675 -> 2,68). Das Ergebnis ist immer ein float, auch für ints und 0 (``bericht.zahl``
    formatiert int und float ab 1000 verschieden). ``stellen`` ändert nur die Zahl der Nachkommastellen
    (nur die Anzeige der Durchschnitte braucht eine: 8,67 -> 8,7).
    """
    return float(Decimal(repr(float(x))).quantize(Decimal(10) ** -stellen, rounding=ROUND_HALF_UP))


def _summe(werte) -> float:
    """Stundensumme aus den ungerundeten Werten, einmal gerundet (siehe ``_r2``) — immer ein float."""
    return _r2(sum(werte, 0.0))


def _montag(tag: date) -> date:
    """Der Montag der Kalenderwoche, in der ``tag`` liegt."""
    return tag - timedelta(days=tag.weekday())


def _je_gruppe(eintraege: list, schluessel) -> dict:
    """{Schlüssel: (Stunden, Arbeitstage)} — Arbeitstage zählen Tage, nicht Einträge."""
    gruppen: dict = {}
    for tag, r in eintraege:
        gruppen.setdefault(schluessel(tag), []).append((tag, r["std"]))
    return {k: (_summe(std for _, std in teil), len({tag for tag, _ in teil})) for k, teil in gruppen.items()}


def _wochen(eintraege: list, anfang: date, ende: date) -> list[dict]:
    """Jede Kalenderwoche (Montag–Sonntag) von der Woche des Anfangs bis zur Woche des Endes, auch leere."""
    je_woche = _je_gruppe(eintraege, _montag)
    wochen, montag = [], _montag(anfang)
    while montag <= ende:
        std, tage = je_woche.get(montag, (0.0, 0))
        jahr, kw, _ = montag.isocalendar()          # ISO-Jahr: gehört zur Kalenderwoche (Jahreswechsel)
        wochen.append({"montag": montag.isoformat(), "sonntag": (montag + timedelta(days=6)).isoformat(),
                       "kw": kw, "jahr": jahr, "std": std, "tage": tage})
        montag += timedelta(days=7)
    return wochen


def _monate(eintraege: list) -> list[dict]:
    """Nur Monate mit Einträgen (nach Kalenderdatum), zeitlich geordnet — die Einträge sind nach Tag sortiert."""
    je_monat = _je_gruppe(eintraege, lambda tag: (tag.year, tag.month))
    return [{"jahr": jahr, "monat": monat, "std": std, "tage": tage} for (jahr, monat), (std, tage) in je_monat.items()]


def auswerten(zeilen: list[dict], von: str | None, bis: str | None, heute: str | None = None) -> dict:
    """
    Wertet die Zeilen von zeilen_holen aus: Stunden gesamt, Arbeitstage, Wochen, Monate, Durchschnitte.
    Rein, ohne Datenbank; das Ergebnis besteht nur aus JSON-Typen, alle Stundenwerte sind float, auf
    zwei Stellen gerundet wie in der App (siehe ``_r2``: exakte Hälften nach oben). Summen entstehen aus
    den ungerundeten Eintragswerten und werden einmal gerundet. Datumsangaben sind gültige ISO-Strings
    (prüft der Aufrufer).

    Zeitraum: Ohne ``von`` beginnt er am ersten, ohne ``bis`` endet er am letzten Eintrag — auch wenn
    der nach ``heute`` liegt, nur so entspricht die Summe der App. Ein angegebenes ``bis`` endet
    spätestens ``heute`` (Standard: das heutige Datum). Einträge außerhalb zählen nicht. Liegt ``von``
    nach dem so gedeckelten ``bis`` (oder gibt es gar keine Einträge), ist die Auswertung leer:
    ``person``, ``von`` und ``bis`` sind None, alles andere 0 bzw. leer.

    Ein Arbeitstag ist ein Tag mit mindestens einem Eintrag, eine Arbeitswoche eine Kalenderwoche
    (Montag–Sonntag) mit mindestens einem Arbeitstag. ``wochen_im_zeitraum`` sind die Kalenderwochen von
    Montag(von) bis Montag(bis); ``wochen`` führt jede davon auf, auch Wochen ohne Arbeit, ``monate`` nur
    Monate mit Stunden. ``person`` ist die Schreibweise des ersten Eintrags im Zeitraum, ``tage`` hat
    je Eintrag eine Zeile.
    """
    heute_d = date.fromisoformat(heute) if heute else date.today()
    von_d = date.fromisoformat(von) if von else None
    bis_d = min(date.fromisoformat(bis), heute_d) if bis else None

    # Einträge im Zeitraum, nach Tag sortiert (stabil: am selben Tag bleibt die Reihenfolge der Zeilen).
    eintraege = sorted(((date.fromisoformat(r["tag"]), r) for r in zeilen), key=lambda e: e[0])
    eintraege = [(tag, r) for tag, r in eintraege
                 if (von_d is None or tag >= von_d) and (bis_d is None or tag <= bis_d)]
    if not eintraege:
        return {"person": None, "projekte": [], "von": None, "bis": None, "std": 0.0, "arbeitstage": 0,
                "wochen_im_zeitraum": 0, "arbeitswochen": 0, "std_pro_woche": 0.0, "std_pro_arbeitswoche": 0.0,
                "wochen": [], "monate": [], "tage": []}

    anfang = von_d or eintraege[0][0]
    ende = bis_d or eintraege[-1][0]
    std = _summe(r["std"] for _, r in eintraege)
    wochen = _wochen(eintraege, anfang, ende)
    arbeitswochen = sum(1 for w in wochen if w["tage"])
    # Beide Divisoren sind >= 1 (keine Division durch null): es gibt mindestens einen Eintrag, und der
    # liegt in einer der Wochen von Montag(anfang) bis Montag(ende).
    return {
        "person": eintraege[0][1]["person"],
        "projekte": sorted({r["projekt"] for _, r in eintraege}),
        "von": anfang.isoformat(),
        "bis": ende.isoformat(),
        "std": std,
        "arbeitstage": len({tag for tag, _ in eintraege}),
        "wochen_im_zeitraum": len(wochen),
        "arbeitswochen": arbeitswochen,
        "std_pro_woche": _r2(std / len(wochen)),
        "std_pro_arbeitswoche": _r2(std / arbeitswochen),
        "wochen": wochen,
        "monate": _monate(eintraege),
        "tage": [{"tag": tag.isoformat(), "std": _r2(r["std"]),
                  "aufgabe": r["aufgabe"], "projekt": r["projekt"]} for tag, r in eintraege],
    }


# ---------------------------------------------------------------------------
# Diagramme (Inline-SVG)
# ---------------------------------------------------------------------------

_MONATSNAMEN = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")
_WOCHENTAGE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

_SVG_B = 640            # Breite beider Diagramme im viewBox; mit width="100%" skalieren sie auf die Seitenbreite
_W_LINKS = 30           # Wochendiagramm, Rand links: Achsenwerte
_W_RECHTS = 50          #   rechts: Beschriftung der Ø-Linie
_W_OBEN = 16            #   oben: Zahlen über der höchsten Säule
_W_UNTEN = 20           #   unten: KW-Marken (bei mehreren Jahren kommt eine Zeile mit den Jahreszahlen dazu)
_W_FLAECHE = 150        #   Höhe der Fläche, auf der die Säulen stehen
_W_MARKEN = 26          #   höchstens so viele KW-Marken
_W_SAEULE_MAX = 36      #   breiteste Säule (bei wenigen Wochen)
_W_ZAHL_PLATZ = 32      #   so breit muss eine Woche mindestens sein, damit ihre Zahl über der Säule steht
_W_JAHR_PLATZ = 30      #   so breit müssen die Wochen eines Jahres zusammen mindestens sein für eine Jahreszahl
_M_ZEILE = 22           # Monatsdiagramm: Höhe einer Zeile
_M_LINKS = 64           #   Platz links für „Aug 2026“
_M_RECHTS = 70          #   Platz rechts für den Wert hinter dem Balken


def _text(x: float, y: float, inhalt: str, farbe: str = GRAU, anker: str = "start", groesse: int = 9,
          fett: bool = False) -> str:
    """Ein SVG-Text in DejaVu Sans, der Schrift, die WeasyPrint auf dem Pi für das PDF nimmt."""
    gewicht = ' font-weight="bold"' if fett else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="DejaVu Sans" font-size="{groesse}"{gewicht} '
            f'fill="{farbe}" text-anchor="{anker}">{e(inhalt)}</text>')


def _svg(hoehe: float, teile: list[str]) -> str:
    """Der Rahmen: 640 breit im viewBox, ohne feste Höhe — mit width="100%" folgt er der Breite des Inhalts."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 {_SVG_B} {hoehe:g}">'
            + "".join(teile) + "</svg>")


def _achse(maxwert: float) -> tuple[float, float]:
    """Obergrenze und Schritt einer Y-Achse mit runden Werten (1, 2 oder 5 mal Zehnerpotenz, höchstens fünf Schritte)."""
    maxwert = maxwert if maxwert > 0 else 1.0
    roh = maxwert / 5
    zehner = 10 ** math.floor(math.log10(roh))
    schritt = next(f * zehner for f in (1, 2, 5, 10) if f * zehner >= roh)
    return math.ceil(maxwert / schritt - 1e-9) * schritt, schritt


def _monat_text(jahr: int, monat: int) -> str:
    """„Aug 2026“"""
    return f"{_MONATSNAMEN[monat - 1]} {jahr}"


def wochen_svg(wochen: list[dict], std_pro_woche: float) -> str:
    """
    Säulendiagramm der Stunden je Kalenderwoche mit gestrichelter Linie für den Ø pro Woche. Jede Woche des
    Zeitraums hat ihren Platz, leere Wochen bleiben leer. KW-Marken stehen nur an jeder n-ten Woche (höchstens
    ``_W_MARKEN``, über mehrere Jahre mit einer Zeile Jahreszahlen darunter, nur für Jahre mit genug sichtbarer
    Breite), die Zahlen über den Säulen nur, wenn eine Woche breit genug dafür ist. ``std_pro_woche`` darf
    ungerundet sein: die Beschriftung „Ø 8,6“ rundet ihn einmal auf eine Stelle.
    """
    n = len(wochen)
    mehrere_jahre = len({w["jahr"] for w in wochen}) > 1
    rechts = _SVG_B - _W_RECHTS
    grund = _W_OBEN + _W_FLAECHE                                    # y der Nulllinie
    slot = (rechts - _W_LINKS) / max(n, 1)                          # Platz einer Woche
    grenze, schritt = _achse(max([w["std"] for w in wochen] + [std_pro_woche]))

    def y(std: float) -> float:
        return grund - _W_FLAECHE * std / grenze

    yd = y(std_pro_woche)                                           # Höhe der Ø-Linie
    teile = []
    for i in range(round(grenze / schritt) + 1):                    # Gitterlinien mit runden Achsenwerten
        yw = y(i * schritt)
        teile.append(f'<line x1="{_W_LINKS}" y1="{yw:.1f}" x2="{rechts}" y2="{yw:.1f}" stroke="{GRAU}" '
                     f'stroke-width="0.5" stroke-opacity="{1 if i == 0 else 0.3}"/>')
        teile.append(_text(_W_LINKS - 4, yw + 3, zahl(float(i * schritt)), anker="end"))
    teile.append(_text(_W_LINKS - 8, grund + 12, "KW", anker="end"))
    breite = min(slot * 0.7, _W_SAEULE_MAX)                         # Breite einer Säule
    marke = max(1, math.ceil(n / _W_MARKEN))                        # KW-Marke an jeder marke-ten Woche
    for i, w in enumerate(wochen):
        mitte = _W_LINKS + (i + 0.5) * slot
        if w["std"] > 0:
            hoehe = max(grund - y(w["std"]), 1.0)
            teile.append(f'<rect x="{mitte - breite / 2:.1f}" y="{grund - hoehe:.1f}" width="{breite:.1f}" '
                         f'height="{hoehe:.1f}" fill="{GRUEN}"/>')
            if slot >= _W_ZAHL_PLATZ:
                yz = grund - hoehe - 3                              # Grundlinie der Zahl über der Säule
                if yz - 8 < yd < yz + 3:                            # die Ø-Linie ginge durch die Zahl (7 über bis 2 unter
                    yz = yd - 4                                     # der Grundlinie): die Zahl darüber setzen
                teile.append(_text(mitte, yz, zahl(w["std"]), DUNKEL, "middle"))
        if i % marke == 0:
            teile.append(_text(mitte, grund + 12, str(w["kw"]), anker="middle"))
    if mehrere_jahre:
        # Jahreszahl mittig unter die Wochen jedes Jahres, dessen Wochen zusammen breit genug dafür sind. Ein kurzes
        # Anfangs- oder Endstück bekommt keine (seine Wochen erkennt man an der KW-Achse). So überdecken sich zwei Zahlen
        # nie (ihre Mitten liegen mindestens _W_JAHR_PLATZ auseinander), ein volles Jahr behält seine Zahl, und sie steht
        # nie unter der letzten Woche des Vorjahres, wo sie sich wie „KW 52, 2026“ läse.
        for jahr, lauf in groupby(enumerate(wochen), key=lambda paar: paar[1]["jahr"]):
            indizes = [i for i, _ in lauf]                          # die Wochen dieses Jahres im Diagramm
            if len(indizes) * slot >= _W_JAHR_PLATZ:
                teile.append(_text(_W_LINKS + (indizes[0] + len(indizes) / 2) * slot, grund + 23, str(jahr), DUNKEL, "middle"))
    if std_pro_woche > 0:                                           # gestrichelt: Ø pro Woche
        teile.append(f'<line x1="{_W_LINKS}" y1="{yd:.1f}" x2="{rechts}" y2="{yd:.1f}" stroke="{GRAU}" '
                     f'stroke-width="1.2" stroke-dasharray="5 3"/>')
        teile.append(_text(rechts + 4, yd + 3.5, f"Ø {zahl(_r2(std_pro_woche, 1))}", GRAU, groesse=10, fett=True))
    return _svg(grund + _W_UNTEN + (10 if mehrere_jahre else 0), teile)


def monate_svg(monate: list[dict]) -> str:
    """
    Waagerechte Balken: Stunden je Monat, beschriftet mit „Aug 2026“, dahinter der Wert in Stunden. Gezeichnet
    wird genau, was übergeben wird (``auswerten`` liefert nur Monate mit Stunden).
    """
    spitze = max((m["std"] for m in monate), default=0) or 1
    laenge = _SVG_B - _M_LINKS - _M_RECHTS
    teile = []
    for i, m in enumerate(monate):
        y, breite = i * _M_ZEILE, laenge * m["std"] / spitze
        teile.append(_text(0, y + 15, _monat_text(m["jahr"], m["monat"]), DUNKEL, groesse=10))
        teile.append(f'<rect x="{_M_LINKS}" y="{y + 4}" width="{breite:.1f}" height="14" fill="{GRUEN}"/>')
        teile.append(_text(_M_LINKS + breite + 6, y + 15, f"{zahl(m['std'])} Std.", GRAU, groesse=10))
    return _svg(max(len(monate), 1) * _M_ZEILE + 4, teile)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_LINIE, _LINIE_HELL = "#CFCFCB", "#ECECE8"      # Rahmen der Kennzahlen/Tabellenkopf, Trennlinien der Zeilen

_CSS = f"""
@page {{ size: A4; margin: 16mm 14mm 16mm 14mm;
  @bottom-left {{ content: "BAULOG · STUNDENZETTEL"; font: 8pt 'DejaVu Sans'; color: {GRAU}; }}
  @bottom-right {{ content: "Seite " counter(page) " von " counter(pages); font: 8pt 'DejaVu Sans'; color: {GRAU}; }} }}
body {{ font-family: 'DejaVu Sans', sans-serif; font-size: 9pt; color: {DUNKEL}; }}
h1 {{ font-size: 20pt; margin: 0; }}
h2 {{ font-size: 12pt; margin: 18px 0 6px; border-bottom: 2px solid {DUNKEL}; padding-bottom: 3px; page-break-after: avoid; }}
.kopf {{ border-left: 5px solid {GRUEN}; padding: 4px 0 6px 12px; margin-bottom: 14px; }}
.marke {{ font-size: 8pt; letter-spacing: 2px; color: {GRUEN}; font-weight: bold; }} .sub {{ color: {GRAU}; margin-top: 4px; }}
.kpis {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.kpi {{ flex: 1 1 20%; border: 1px solid {_LINIE}; padding: 7px 8px; }}
.kpi .w {{ font-size: 13pt; font-weight: bold; }} .kpi .l {{ color: {GRAU}; font-size: 8pt; }}
.dia {{ page-break-inside: avoid; }} .dia svg {{ width: 100%; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 4px; }}
th {{ text-align: left; font-size: 8pt; color: {GRAU}; border-bottom: 1px solid {_LINIE}; padding: 3px 4px; }}
td {{ padding: 3px 4px; border-bottom: 1px solid {_LINIE_HELL}; vertical-align: top; }} tr {{ page-break-inside: avoid; }}
.r {{ text-align: right; white-space: nowrap; }} .nw {{ white-space: nowrap; }}
.summe td {{ font-weight: bold; border-top: 1.5px solid {DUNKEL}; }} .klein {{ font-size: 7.5pt; color: {GRAU}; }}
"""


def _anzahl_wochen(n: int) -> str:
    """„1 Woche“, „4 Wochen“"""
    return "1 Woche" if n == 1 else f"{n} Wochen"


def _woche_text(w: dict, mit_jahr: bool = False) -> str:
    """„KW 45 · 02.–08.11.“, über die Monatsgrenze „KW 53 · 28.12.–03.01.“, mit ``mit_jahr`` „KW 53/2026 · 28.12.–03.01.“"""
    montag, sonntag = date.fromisoformat(w["montag"]), date.fromisoformat(w["sonntag"])
    anfang = f"{montag:%d.}" if montag.month == sonntag.month else f"{montag:%d.%m.}"
    kw = f"KW {w['kw']}/{w['jahr']}" if mit_jahr else f"KW {w['kw']}"
    return f"{kw} · {anfang}–{sonntag:%d.%m.}"


def _kpi(wert: str, label: str, zusatz: str = "") -> str:
    zusatz = f'<div class="l">{zusatz}</div>' if zusatz else ""
    return f'<div class="kpi"><div class="w">{wert}</div><div class="l">{label}</div>{zusatz}</div>'


def _wochentabelle(daten: dict) -> str:
    """Eine Zeile je Woche mit Arbeit; leere Wochen stehen nur im Diagramm."""
    wochen = [w for w in daten["wochen"] if w["tage"]]
    mehrere_jahre = len({w["jahr"] for w in wochen}) > 1             # dann trägt jede KW ihr ISO-Jahr: „KW 53/2026“
    zeilen = []
    for w in wochen:
        zeilen.append(f'<tr><td>{_woche_text(w, mehrere_jahre)}</td>'
                      f'<td class="r">{w["tage"]}</td><td class="r">{zahl(w["std"])}</td></tr>')
    return ('<h2>Wochenübersicht</h2><table><thead><tr><th>Woche</th><th class="r">Arbeitstage</th><th class="r">Stunden</th>'
            f'</tr></thead><tbody>{"".join(zeilen)}</tbody></table>')


def _tagestabelle(daten: dict, alle_projekte: bool) -> str:
    """Die einzelnen Tage (ein Eintrag je Zeile), nach jedem Monat mit dessen Summenzeile."""
    je_monat = {(m["jahr"], m["monat"]): m["std"] for m in daten["monate"]}
    zeilen = []
    for monat, gruppe in groupby(daten["tage"], key=lambda t: t["tag"][:7]):          # "2026-11"
        jahr, nr = int(monat[:4]), int(monat[5:])
        for t in gruppe:
            tag = date.fromisoformat(t["tag"])
            projekt = f'<td>{e(t["projekt"])}</td>' if alle_projekte else ""
            zeilen.append(f'<tr><td class="nw">{_WOCHENTAGE[tag.weekday()]} {tag:%d.%m.}</td>'
                          f'<td>{e(t["aufgabe"])}</td>{projekt}<td class="r">{zahl(t["std"])}</td></tr>')
        zeilen.append(f'<tr class="summe"><td colspan="{3 if alle_projekte else 2}">Summe {_monat_text(jahr, nr)}</td>'
                      f'<td class="r">{zahl(je_monat[(jahr, nr)])}</td></tr>')
    kopf = "<th>Datum</th><th>Aufgabe</th>" + ("<th>Projekt</th>" if alle_projekte else "") + '<th class="r">Std.</th>'
    return f'<h2>Einzelne Tage</h2><table><thead><tr>{kopf}</tr></thead><tbody>{"".join(zeilen)}</tbody></table>'


def html_stundenzettel(daten: dict, projekt: str, mit_tagen: bool, erstellt: str, alle_projekte: bool = False) -> str:
    """
    Der Stundenzettel als vollständiges HTML (A4 hoch, im Look des Projektberichts) aus dem Ergebnis von
    ``auswerten``: Kopf, Kennzahlen, Diagramme je Woche und Monat, Tabelle je Woche und mit ``mit_tagen`` die
    einzelnen Tage. ``projekt`` ist der Anzeigename; bei ``alle_projekte`` kommen die Namen der Projekte aus
    den Daten dazu, und die Tage bekommen eine Projektspalte. Ohne Stunden steht nur ein Hinweis unter dem
    Kopf. Jeder Text aus den Daten läuft durch ``e``.
    """
    person = e(daten["person"] or "–")                              # ab hier ist alles fertig maskiert
    projekt_zeile = e(projekt)
    if alle_projekte and daten["projekte"]:
        projekt_zeile += f" ({e(', '.join(daten['projekte']))})"
    zeitraum = f"Zeitraum {datum(daten['von'])} – {datum(daten['bis'])} · " if daten["von"] else ""
    teile = [f'<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Stundenzettel – {person}</title>'
             f'<style>{_CSS}</style></head><body>\n'
             f'<div class="kopf"><div class="marke">BAULOG · STUNDENZETTEL</div><h1>{person}</h1>\n'
             f'<div class="sub">Projekt: {projekt_zeile}</div>\n'
             f'<div class="sub">{zeitraum}Erstellt {e(erstellt)}</div></div>\n']
    if not daten["tage"]:
        teile.append("<p>Keine Stunden im Zeitraum.</p>")
    else:
        n_wochen, n_arbeitswochen = daten["wochen_im_zeitraum"], daten["arbeitswochen"]
        # Die Durchschnitte entstehen aus dem ungerundeten Quotienten und werden nur einmal auf eine Stelle gerundet:
        # daten["std_pro_woche"] ist schon auf zwei Stellen gerundet (8,645 -> 8,65), ein zweites Runden gäbe 8,7,
        # wer die gedruckten Zahlen teilt, käme aber auf 8,6.
        std_je_woche = daten["std"] / n_wochen if n_wochen else 0.0
        std_je_arbeitswoche = daten["std"] / n_arbeitswochen if n_arbeitswochen else 0.0
        teile.append('<div class="kpis">'
                     + _kpi(f"{zahl(daten['std'])} Std.", "Stunden gesamt")
                     + _kpi(str(daten["arbeitstage"]), "Arbeitstage")
                     + _kpi(f"{zahl(_r2(std_je_woche, 1))} Std.", "Ø pro Woche", f"({_anzahl_wochen(n_wochen)})")
                     + _kpi(f"{zahl(_r2(std_je_arbeitswoche, 1))} Std.", "Ø pro Arbeitswoche",
                            f"({_anzahl_wochen(n_arbeitswochen)} mit Arbeit)")
                     + '</div>')
        teile.append('<h2>Stunden je Kalenderwoche</h2><div class="dia">'
                     '<div class="klein">Gestrichelt: Ø pro Woche, leere Wochen zählen mit</div>'
                     + wochen_svg(daten["wochen"], std_je_woche) + '</div>')
        teile.append('<h2>Stunden je Monat</h2><div class="dia">' + monate_svg(daten["monate"]) + '</div>')
        teile.append(_wochentabelle(daten))
        if mit_tagen:
            teile.append(_tagestabelle(daten, alle_projekte))
    teile.append("</body></html>")
    return "".join(teile)


def main() -> None:
    """
    Schreibt das PDF:  python3 stundenzettel.py <ziel.pdf>
    Die Angaben kommen als JSON auf stdin: {"daten": <Ergebnis von auswerten>, "projekt": str,
    "mit_tagen": bool, "erstellt": str, "alle_projekte": bool}.
    """
    from weasyprint import HTML        # nur im System-Python installiert, deshalb erst hier (wie bericht.py)
    ziel = sys.argv[1]
    angaben = json.load(sys.stdin)
    html = html_stundenzettel(angaben["daten"], angaben["projekt"], angaben["mit_tagen"], angaben["erstellt"],
                              angaben.get("alle_projekte", False))
    HTML(string=html, base_url=BASIS).write_pdf(ziel)


if __name__ == "__main__":
    main()
