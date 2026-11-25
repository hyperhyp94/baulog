"""
bericht.py — Projektbericht als PDF (Stand, Kosten, Personen, Aufgaben, Positionen,
optional Fotos/Belege).

Läuft bewusst mit dem System-Python (dort sind WeasyPrint und Pillow installiert):
    python3 bericht.py <project_id> <fotos 0|1> <ziel.pdf>
Liest die Datenbank nur lesend.
"""

import base64
import html
import io
import os
import sqlite3
import sys
from datetime import date, datetime

BASIS = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASIS, "data", "baulog.db")

GRUEN, DUNKEL, GRAU, ROT, BERNSTEIN = "#2E7D5B", "#1A1A1A", "#5C5C5C", "#B23A3A", "#B07D10"


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def euro(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def zahl(v: float) -> str:
    s = f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def datum(iso) -> str:
    if not iso:
        return "–"
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(iso)


def daten(pid: int) -> dict:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    projekt = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if not projekt:
        raise SystemExit("Projekt nicht gefunden")
    sites = c.execute("SELECT * FROM sites WHERE project_id = ? ORDER BY created_at", (pid,)).fetchall()
    tasks = c.execute(
        "SELECT t.*, s.name AS site_name FROM tasks t JOIN sites s ON s.id = t.site_id "
        "WHERE s.project_id = ? ORDER BY t.created_at", (pid,)).fetchall()
    zeiten = c.execute(
        "SELECT te.*, t.title AS task FROM time_entries te JOIN tasks t ON t.id = te.task_id "
        "JOIN sites s ON s.id = t.site_id WHERE s.project_id = ? "
        "ORDER BY COALESCE(te.work_date, substr(te.created_at,1,10)), te.id", (pid,)).fetchall()
    material = c.execute(
        "SELECT m.*, t.title AS task FROM materials m JOIN tasks t ON t.id = m.task_id "
        "JOIN sites s ON s.id = t.site_id WHERE s.project_id = ? "
        "ORDER BY COALESCE(m.purchase_date, substr(m.created_at,1,10)), m.id", (pid,)).fetchall()
    bons = {r["id"]: r for r in c.execute(
        "SELECT r.* FROM receipts r JOIN tasks t ON t.id = r.task_id JOIN sites s ON s.id = t.site_id "
        "WHERE s.project_id = ?", (pid,)).fetchall()}
    docs = c.execute(
        "SELECT d.* FROM documents d LEFT JOIN sites s ON s.id = d.site_id "
        "LEFT JOIN tasks t ON t.id = d.task_id LEFT JOIN sites s2 ON s2.id = t.site_id "
        "WHERE COALESCE(s.project_id, s2.project_id) = ? AND COALESCE(d.doc_type, '') <> 'bericht' "
        "ORDER BY d.created_at", (pid,)).fetchall()
    guthaben = {}
    for r in c.execute(
            "SELECT g.person, ROUND(SUM(g.betrag) - COALESCE((SELECT SUM(v.betrag) FROM guthaben_verbrauch v "
            "JOIN guthaben g2 ON g2.id = v.guthaben_id WHERE g2.project_id = g.project_id "
            "AND lower(g2.person) = lower(g.person)), 0), 2) AS saldo "
            "FROM guthaben g WHERE g.project_id = ? GROUP BY lower(g.person)", (pid,)).fetchall():
        guthaben[r["person"].lower()] = r["saldo"] or 0
    return dict(projekt=projekt, sites=sites, tasks=tasks, zeiten=zeiten, material=material,
                bons=bons, docs=docs, guthaben=guthaben)


def betrag_zeit(z) -> float:
    # SQL-ROUND wie in der App und allen Summen: Python-round() rundet exakte Hälften anders
    # (2,625 → 2,62 statt 2,63) und ließe im PDF einzelne Cent abweichen.
    c = sqlite3.connect(":memory:")
    try:
        return c.execute("SELECT ROUND(? * ? + ?, 2)",
                         (z["hours"], z["hourly_rate"], z["travel_cost"] or 0)).fetchone()[0]
    finally:
        c.close()


def arbeit_und_anfahrt(zeiten) -> tuple[float, float]:
    """
    Arbeit (ohne Anfahrt) und Anfahrt, so aufgeteilt, dass beides zusammen genau die Summe der
    Tagesbeträge ergibt. Python-round(Stunden × Satz) + Anfahrt wich bei Viertelstunden × halben
    Euro-Sätzen um 1 ct von Σ ROUND(Stunden × Satz + Anfahrt) ab, das der Rest der App benutzt.
    """
    anfahrt = sum(z["travel_cost"] or 0 for z in zeiten)
    arbeit = round(sum(betrag_zeit(z) for z in zeiten) - anfahrt, 2)
    return arbeit, anfahrt


def gezahlt(betrag: float, row) -> float:
    return betrag if row["paid"] else round(row["paid_amount"] or 0, 2)


def balken_svg(zeilen: list[tuple[str, float, float]]) -> str:
    """Gestapelte Balken je Aufgabe: Arbeit (grün) + Material (grau)."""
    if not zeilen:
        return ""
    breite, label_b, zeile_h = 640, 210, 26
    maxv = max(a + m for _, a, m in zeilen) or 1
    h = zeile_h * len(zeilen) + 8
    teile = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{breite}" height="{h}" '
             f'viewBox="0 0 {breite} {h}" font-family="DejaVu Sans" font-size="11">']
    for i, (name, a, m) in enumerate(zeilen):
        y = i * zeile_h + 4
        wa = (breite - label_b - 90) * a / maxv
        wm = (breite - label_b - 90) * m / maxv
        kurz = name if len(name) <= 32 else name[:31] + "…"
        teile.append(f'<text x="0" y="{y + 15}" fill="{DUNKEL}">{e(kurz)}</text>')
        teile.append(f'<rect x="{label_b}" y="{y + 3}" width="{wa:.1f}" height="16" fill="{GRUEN}"/>')
        teile.append(f'<rect x="{label_b + wa:.1f}" y="{y + 3}" width="{wm:.1f}" height="16" fill="#9C9C96"/>')
        teile.append(f'<text x="{label_b + wa + wm + 6:.1f}" y="{y + 15}" fill="{GRAU}">{e(euro(a + m))}</text>')
    teile.append("</svg>")
    return "".join(teile)


def ring_svg(bezahlt: float, offen: float) -> str:
    """Ring: Anteil bezahlt/offen."""
    import math
    ges = bezahlt + offen
    anteil = bezahlt / ges if ges else 1
    r, u = 42, 2 * math.pi * 42
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110" viewBox="0 0 110 110">'
            f'<circle cx="55" cy="55" r="{r}" fill="none" stroke="#E6E6E2" stroke-width="14"/>'
            f'<circle cx="55" cy="55" r="{r}" fill="none" stroke="{GRUEN}" stroke-width="14" '
            f'stroke-dasharray="{u * anteil:.1f} {u:.1f}" transform="rotate(-90 55 55)"/>'
            f'<text x="55" y="60" text-anchor="middle" font-family="DejaVu Sans" font-size="16" '
            f'font-weight="bold" fill="{DUNKEL}">{round(anteil * 100)} %</text></svg>')


def bild_data_uri(pfad: str, max_px: int = 1100) -> str | None:
    try:
        from PIL import Image, ImageOps
        with Image.open(pfad) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=78)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def pdf_seiten_uris(pfad: str, max_seiten: int = 4) -> list[str]:
    import glob
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(["pdftoppm", "-r", "110", "-jpeg", "-jpegopt", "quality=78", "-f", "1",
                            "-l", str(max_seiten), pfad, os.path.join(tmp, "s")],
                           check=True, capture_output=True, timeout=60)
        except Exception:
            return []
        return ["data:image/jpeg;base64," + base64.b64encode(open(f, "rb").read()).decode()
                for f in sorted(glob.glob(os.path.join(tmp, "s-*.jpg")))]


def html_bericht(d: dict, fotos: bool, basis_url: str = "") -> str:
    p = d["projekt"]
    docs_je_bon = {}
    for x in d["docs"]:
        if x["receipt_id"]:
            docs_je_bon.setdefault(x["receipt_id"], []).append(x)

    def beleg_link(rid, text=None):
        b = d["bons"].get(rid)
        if not b or not b["beleg_nr"]:
            return ""
        text = text or f"Beleg {b['beleg_nr']}"
        if fotos and rid in docs_je_bon:
            return f'<a class="beleg" href="#beleg-{b["beleg_nr"]}">→ {text}</a>'
        if basis_url and rid in docs_je_bon:
            return f'<a class="beleg" href="{basis_url}/api/documents/{docs_je_bon[rid][0]["id"]}/file">→ {text} ↗</a>'
        return f'<span class="klein">{text}</span>' 
    zeiten, material = d["zeiten"], d["material"]

    arbeit, anfahrt = arbeit_und_anfahrt(zeiten)
    stunden = sum(z["hours"] for z in zeiten)
    mat = sum(round(m["qty"] * m["unit_price"], 2) for m in material)
    gesamt = arbeit + anfahrt + mat
    bez_z = sum(gezahlt(betrag_zeit(z), z) for z in zeiten)
    bez_m = sum(gezahlt(round(m["qty"] * m["unit_price"], 2), m) for m in material)
    bezahlt = bez_z + bez_m
    offen = gesamt - bezahlt
    mit_r = sum(betrag_zeit(z) for z in zeiten if z["billed"]) + \
        sum(round(m["qty"] * m["unit_price"], 2) for m in material if m["billed"])
    ohne_r = gesamt - mit_r

    # Personen
    personen = {}
    for z in zeiten:
        k = z["person"]
        q = personen.setdefault(k, dict(std=0, kosten=0, bezahlt=0))
        b = betrag_zeit(z)
        q["std"] += z["hours"]; q["kosten"] += b; q["bezahlt"] += gezahlt(b, z)
    # Aufgaben
    je_aufgabe = {}
    for t in d["tasks"]:
        je_aufgabe[t["id"]] = [t["title"], 0.0, 0.0, t["status"]]
    for z in zeiten:
        je_aufgabe[z["task_id"]][1] += betrag_zeit(z)
    for m in material:
        je_aufgabe[m["task_id"]][2] += round(m["qty"] * m["unit_price"], 2)
    aufg = sorted([v for v in je_aufgabe.values() if v[1] + v[2] > 0], key=lambda v: -(v[1] + v[2]))

    tage = sorted({str(z["work_date"] or z["created_at"])[:10] for z in zeiten}
                  | {str(m["purchase_date"] or m["created_at"])[:10] for m in material})
    zeitraum = f"{datum(tage[0])} – {datum(tage[-1])}" if tage else "–"
    offene_aufg = sum(1 for t in d["tasks"] if t["status"] != "fertig")

    def kpi(wert, label, farbe=DUNKEL):
        return f'<div class="kpi"><div class="w" style="color:{farbe}">{wert}</div><div class="l">{label}</div></div>'

    teile = [f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 16mm 14mm 16mm 14mm;
  @bottom-left {{ content: "{e(p['name'])} · Stand {date.today().strftime('%d.%m.%Y')}"; font: 8pt 'DejaVu Sans'; color: {GRAU}; }}
  @bottom-right {{ content: "Seite " counter(page) " von " counter(pages); font: 8pt 'DejaVu Sans'; color: {GRAU}; }} }}
body {{ font-family: 'DejaVu Sans', sans-serif; font-size: 9pt; color: {DUNKEL}; }}
h1 {{ font-size: 20pt; margin: 0; }} h2 {{ font-size: 12pt; margin: 18px 0 6px; border-bottom: 2px solid {DUNKEL}; padding-bottom: 3px; }}
.kopf {{ border-left: 5px solid {GRUEN}; padding: 4px 0 6px 12px; margin-bottom: 14px; }}
.kopf .sub {{ color: {GRAU}; margin-top: 4px; }} .kopf .marke {{ font-size: 8pt; letter-spacing: 2px; color: {GRUEN}; font-weight: bold; }}
a {{ color: {GRUEN}; text-decoration: none; }} a.beleg {{ font-size: 7.5pt; font-weight: normal; white-space: nowrap; }}
.belegkopf {{ background: #F4F4F2; border-left: 4px solid {GRUEN}; padding: 5px 8px; margin: 14px 0 6px; page-break-after: avoid; }}
.zurueck {{ font-size: 7.5pt; }}
.kpis {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.kpi {{ flex: 1 1 22%; border: 1px solid #CFCFCB; padding: 7px 8px; }} .kpi .w {{ font-size: 13pt; font-weight: bold; }}
.kpi .l {{ color: {GRAU}; font-size: 8pt; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 4px; }}
th {{ text-align: left; font-size: 8pt; color: {GRAU}; border-bottom: 1px solid #CFCFCB; padding: 3px 4px; }}
td {{ padding: 3px 4px; border-bottom: 1px solid #ECECE8; vertical-align: top; }}
.r {{ text-align: right; white-space: nowrap; }} .summe td {{ font-weight: bold; border-top: 1.5px solid {DUNKEL}; }}
.ok {{ color: {GRUEN}; }} .offen {{ color: {BERNSTEIN}; }} .klein {{ font-size: 7.5pt; color: {GRAU}; }}
.zwei {{ display: flex; gap: 14px; align-items: center; }} .legende span {{ display: inline-block; width: 9px; height: 9px; margin: 0 3px 0 10px; }}
.fotos {{ display: flex; flex-wrap: wrap; gap: 8px; }} .foto {{ width: 48%; page-break-inside: avoid; }}
.foto img {{ width: 100%; border: 1px solid #CFCFCB; }} .bon td {{ background: #F4F4F2; font-weight: bold; }}
</style></head><body>
<div class="kopf"><div class="marke">BAULOG · PROJEKTBERICHT</div><h1>{e(p['name'])}</h1>
<div class="sub">{e(p['note'] or '')}{' · ' if p['note'] else ''}Zeitraum {zeitraum} · Stand {datetime.now().strftime('%d.%m.%Y, %H:%M')} Uhr</div></div>
<div class="kpis">
{kpi(euro(gesamt), 'Kosten gesamt')}{kpi(euro(arbeit + anfahrt), f'Arbeit · {zahl(float(stunden))} Std.' + (f' (inkl. {euro(anfahrt)} Anfahrt)' if anfahrt else ''))}
{kpi(euro(mat), f'Material · {len(material)} Positionen')}{kpi(euro(offen), 'noch zu bezahlen', BERNSTEIN if offen > 0.004 else GRUEN)}
{kpi(euro(bezahlt), 'bezahlt', GRUEN)}{kpi(euro(mit_r), 'mit Rechnung')}{kpi(euro(ohne_r), 'ohne Rechnung')}
{kpi(f'{offene_aufg} / {len(d["tasks"])}', 'Aufgaben offen / gesamt')}
</div>
<h2>Bezahlt / offen</h2>
<div class="zwei">{ring_svg(bezahlt, offen)}
<table style="width:auto"><tr><th></th><th class="r">Summe</th><th class="r">bezahlt</th><th class="r">offen</th></tr>
<tr><td>Arbeit{' inkl. Anfahrt' if anfahrt else ''}</td><td class="r">{euro(arbeit + anfahrt)}</td><td class="r">{euro(bez_z)}</td><td class="r">{euro(arbeit + anfahrt - bez_z)}</td></tr>
<tr><td>Material</td><td class="r">{euro(mat)}</td><td class="r">{euro(bez_m)}</td><td class="r">{euro(mat - bez_m)}</td></tr>
<tr class="summe"><td>Gesamt</td><td class="r">{euro(gesamt)}</td><td class="r">{euro(bezahlt)}</td><td class="r">{euro(offen)}</td></tr></table></div>
"""]

    if aufg:
        teile.append(f'<h2>Kosten je Aufgabe</h2><div class="legende klein"><span style="background:{GRUEN}"></span>Arbeit'
                     f'<span style="background:#9C9C96"></span>Material</div>'
                     + balken_svg([(a[0], a[1], a[2]) for a in aufg]))

    if personen:
        teile.append('<h2>Personen</h2><table><tr><th>Person</th><th class="r">Stunden</th><th class="r">Kosten</th>'
                     '<th class="r">bezahlt</th><th class="r">offen</th><th class="r">Guthaben</th></tr>')
        for name, q in sorted(personen.items(), key=lambda x: -x[1]["kosten"]):
            o = q["kosten"] - q["bezahlt"]
            g = d["guthaben"].get(name.lower(), 0)
            teile.append(f'<tr><td>{e(name)}</td><td class="r">{zahl(float(q["std"]))}</td><td class="r">{euro(q["kosten"])}</td>'
                         f'<td class="r ok">{euro(q["bezahlt"])}</td><td class="r {"offen" if o > 0.004 else ""}">{euro(o)}</td>'
                         f'<td class="r">{euro(g) if g > 0.004 else "–"}</td></tr>')
        teile.append("</table>")

    teile.append('<h2>Aufgaben</h2><table><tr><th>Aufgabe</th><th>Bereich</th><th>Status</th>'
                 '<th class="r">Arbeit</th><th class="r">Material</th><th class="r">Gesamt</th></tr>')
    for t in d["tasks"]:
        a, m = je_aufgabe[t["id"]][1], je_aufgabe[t["id"]][2]
        teile.append(f'<tr><td>{e(t["title"])}</td><td>{e(t["site_name"])}</td>'
                     f'<td class="{"ok" if t["status"] == "fertig" else "offen"}">{e(t["status"])}</td>'
                     f'<td class="r">{euro(a)}</td><td class="r">{euro(m)}</td><td class="r"><b>{euro(a + m)}</b></td></tr>')
    teile.append("</table>")

    if zeiten:
        teile.append('<h2>Stunden</h2><table><tr><th>Datum</th><th>Person</th><th>Aufgabe</th><th class="r">Std.</th>'
                     '<th class="r">Satz</th><th class="r">Anfahrt</th><th class="r">Betrag</th><th>Status</th></tr>')
        for z in zeiten:
            b = betrag_zeit(z); g = gezahlt(b, z)
            st = "bezahlt" if z["paid"] else (f"teilw. {euro(g)}" if g > 0 else "offen")
            teile.append(f'<tr><td>{datum(z["work_date"] or z["created_at"])}</td><td>{e(z["person"])}</td><td>{e(z["task"])}</td>'
                         f'<td class="r">{zahl(float(z["hours"]))}</td><td class="r">{euro(z["hourly_rate"])}</td>'
                         f'<td class="r">{euro(z["travel_cost"] or 0) if z["travel_cost"] else "–"}</td><td class="r">{euro(b)}</td>'
                         f'<td class="{"ok" if z["paid"] else "offen"}">{st}</td></tr>')
        teile.append(f'<tr class="summe"><td colspan="3">Summe</td><td class="r">{zahl(float(stunden))}</td><td></td>'
                     f'<td class="r">{euro(anfahrt)}</td><td class="r">{euro(arbeit + anfahrt)}</td><td></td></tr></table>')

    if material:
        teile.append('<h2>Material</h2><table><tr><th>Datum</th><th>Was</th><th>Aufgabe</th><th class="r">Menge</th>'
                     '<th class="r">€/Einh.</th><th class="r">Betrag</th><th>Status</th></tr>')
        gezeigt = set()
        for m in material:
            rid = m["receipt_id"]
            if rid and rid not in gezeigt and rid in d["bons"]:
                gezeigt.add(rid)
                b = d["bons"][rid]
                teile.append(f'<tr class="bon" id="pos-beleg-{b["beleg_nr"]}"><td>{datum(b["purchase_date"])}</td><td colspan="4">{"Beleg " + str(b["beleg_nr"]) + " · " if b["beleg_nr"] else "Bon "}{e(b["store"] or "")} {beleg_link(rid, "ansehen")}</td>'
                             f'<td class="r">{euro(b["total"])}</td><td></td></tr>')
            betrag = round(m["qty"] * m["unit_price"], 2)
            g = gezahlt(betrag, m)
            st = "bezahlt" if m["paid"] else (f"teilw. {euro(g)}" if g > 0 else "offen")
            einzug = "&nbsp;&nbsp;&nbsp;" if rid else ""
            teile.append(f'<tr><td>{"" if rid else datum(m["purchase_date"] or m["created_at"])}</td>'
                         f'<td>{einzug}{e(m["name"])}{"" if m["billed"] else " <span class=klein>(ohne Rechnung)</span>"}'
                         f'{(" " + beleg_link(rid)) if rid else ""}</td>'
                         f'<td>{e(m["task"])}</td><td class="r">{zahl(float(m["qty"]))} {e(m["unit"] or "")}</td>'
                         f'<td class="r">{euro(m["unit_price"])}</td><td class="r">{euro(betrag)}</td>'
                         f'<td class="{"ok" if m["paid"] else "offen"}">{st}</td></tr>')
        teile.append(f'<tr class="summe"><td colspan="5">Summe Material</td><td class="r">{euro(mat)}</td><td></td></tr></table>')

    if fotos:
        def bild_html(uri, unter):
            return f'<div class="foto"><img src="{uri}"><div class="klein">{unter}</div></div>'

        def doc_bilder(x):
            """Bild-URIs einer Datei: Foto direkt, PDF als Seitenbilder (max. 4 Seiten)."""
            if not x["stored_path"] or not os.path.exists(x["stored_path"]):
                return []
            if (x["mime"] or "").startswith("image/"):
                u = bild_data_uri(x["stored_path"])
                return [u] if u else []
            if x["mime"] == "application/pdf":
                return pdf_seiten_uris(x["stored_path"], 4)
            return []

        bons_mit_datei = sorted((r for r in d["bons"].values() if r["id"] in docs_je_bon),
                                key=lambda r: (r["beleg_nr"] or 9999, r["id"]))
        if bons_mit_datei:
            teile.append('<h2 style="page-break-before: always">Belege</h2>')
            for r in bons_mit_datei:
                nr = r["beleg_nr"]
                teile.append(f'<div class="belegkopf" id="beleg-{nr}"><b>Beleg {nr}</b> · {e(r["store"])} · '
                             f'{datum(r["purchase_date"])} · <b>{euro(r["total"])}</b> '
                             f'<a class="zurueck" href="#pos-beleg-{nr}">↑ zu den Positionen</a></div><div class="fotos">')
                for x in docs_je_bon[r["id"]]:
                    uris = doc_bilder(x)
                    for i, u in enumerate(uris, 1):
                        seite = f" · Seite {i}" if len(uris) > 1 and x["mime"] == "application/pdf" else ""
                        teile.append(bild_html(u, f'{e(x["filename"])}{seite}'))
                teile.append("</div>")

        rest = [x for x in d["docs"] if not x["receipt_id"]]
        rest_bilder = [(x, u) for x in rest for u in doc_bilder(x)]
        if rest_bilder:
            teile.append('<h2 style="page-break-before: always">Fotos &amp; weitere Dokumente</h2><div class="fotos">')
            for x, u in rest_bilder:
                art = {"beleg": "Beleg", "rechnung": "Rechnung"}.get(x["doc_type"], "Foto")
                teile.append(bild_html(u, f'{art} · {datum(x["created_at"])} · {e(x["filename"])}'))
            teile.append("</div>")

    teile.append("</body></html>")
    return "".join(teile)


def main():
    pid, fotos, ziel = int(sys.argv[1]), sys.argv[2] == "1", sys.argv[3]
    basis_url = sys.argv[4].rstrip("/") if len(sys.argv) > 4 else ""
    from weasyprint import HTML
    HTML(string=html_bericht(daten(pid), fotos, basis_url), base_url=BASIS).write_pdf(ziel)


if __name__ == "__main__":
    main()
