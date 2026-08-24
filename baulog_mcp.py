"""
baulog_mcp.py — MCP-Server für baulog.

Dünner HTTP-Client auf die REST-API von server.py (Port 5112).
Wird nach ~/hermes-tools/mcp/ kopiert; mcp_helper.py liegt dort im selben Verzeichnis.

Alle 34 Tools gemäß PLAN.md Abschnitt 9.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_helper import register_tool, run_server  # noqa: E402 — erst nach sys.path-Patch

import json
import mimetypes
import uuid
import urllib.request
import urllib.error
import urllib.parse

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BAULOG_URL", "http://127.0.0.1:5112")


# ---------------------------------------------------------------------------
# HTTP-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _request(method: str, path: str, payload: dict | None = None) -> dict:
    """
    Sendet einen HTTP-Request an die baulog-REST-API.

    Bei HTTPError wird {"success": False, "error": "HTTP <code>: <detail>"} zurückgegeben.
    Bei URLError (Server nicht erreichbar) {"success": False, "error": "baulog nicht erreichbar …"}.
    """
    url = BASE_URL.rstrip("/") + path
    body: bytes | None = None
    headers: dict = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {"status": "ok"}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(detail)
            msg = parsed.get("error", detail[:300])
        except Exception:
            msg = exc.reason or str(exc)
        return {"success": False, "error": f"HTTP {exc.code}: {msg}"}
    except urllib.error.URLError as exc:
        return {"success": False, "error": f"baulog nicht erreichbar ({BASE_URL}): {exc.reason}"}


def _safe(method: str, path: str, payload: dict | None = None) -> dict:
    """Wrapper um _request mit globalem try/except für unerwartete Fehler."""
    try:
        return _request(method, path, payload)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Unerwarteter Fehler: {exc}"}


def _get(path: str) -> dict:
    return _safe("GET", path)


def _post(path: str, payload: dict) -> dict:
    return _safe("POST", path, payload)


def _put(path: str, payload: dict) -> dict:
    return _safe("PUT", path, payload)


def _delete(path: str) -> dict:
    return _safe("DELETE", path)


# ---------------------------------------------------------------------------
# Multipart-Upload-Hilfsfunktion
# ---------------------------------------------------------------------------

def _multipart(fields: dict, file_path: str) -> tuple[str, bytes]:
    """
    Baut einen multipart/form-data-Body aus Textfeldern und einer Datei.

    Gibt (boundary, body_bytes) zurück.
    """
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n'
            f"\r\n{v}\r\n"
        ).encode("utf-8")
    fn = os.path.basename(file_path)
    mime = mimetypes.guess_type(fn)[0] or "application/octet-stream"
    with open(file_path, "rb") as fh:
        data = fh.read()
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{fn}"\r\n'
        f"Content-Type: {mime}\r\n"
        f"\r\n"
    ).encode("utf-8") + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return boundary, body


# ---------------------------------------------------------------------------
# 1. Projekte
# ---------------------------------------------------------------------------

def projekt_anlegen(name: str, note: str = "") -> dict:
    """
    Legt ein neues Projekt an.

    Parameter:
        name  — Pflichtfeld: Projektbezeichnung, z. B. „Umbau MFH Deisterstraße 40".
        note  — Optionale Notiz / Beschreibung zum Projekt (Standard: leer).

    Rückgabe: {"status": "ok", "id": <neue Projekt-ID>}
    """
    payload: dict = {"name": name}
    if note:
        payload["note"] = note
    return _post("/api/projects", payload)


def projekte_liste() -> dict:
    """
    Gibt alle Projekte zurück, inklusive Baustellen-Anzahl und Gesamtkosten.

    Keine Parameter.

    Rückgabe: {"status": "ok", "projects": [...]}
    """
    return _get("/api/projects")


def projekt_details(projekt_id: int) -> dict:
    """
    Gibt Details zu einem Projekt zurück, inklusive der zugehörigen Baustellen.

    Parameter:
        projekt_id — ID des Projekts (int).

    Rückgabe: {"status": "ok", "project": {...}, "sites": [...]}
    """
    return _get(f"/api/projects/{projekt_id}")


def projekt_bearbeiten(projekt_id: int, name: str | None = None, note: str | None = None) -> dict:
    """
    Ändert Name und/oder Notiz eines bestehenden Projekts.

    Parameter:
        projekt_id — ID des zu ändernden Projekts (int).
        name       — Neuer Projektname (optional, nur angeben wenn ändern).
        note       — Neue Notiz (optional, nur angeben wenn ändern).

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if note is not None:
        payload["note"] = note
    return _put(f"/api/projects/{projekt_id}", payload)


def projekt_loeschen(projekt_id: int) -> dict:
    """
    Löscht ein Projekt inkl. aller zugehörigen Baustellen, Gewerke, Aufgaben,
    Zeiteinträge, Materialpositionen und Dokumente (Kaskade).

    Parameter:
        projekt_id — ID des zu löschenden Projekts (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/projects/{projekt_id}")


# ---------------------------------------------------------------------------
# 2. Baustellen
# ---------------------------------------------------------------------------

def baustelle_anlegen(
    name: str,
    projekt_id: int,
    address: str = "",
    note: str = "",
) -> dict:
    """
    Legt eine neue Baustelle innerhalb eines Projekts an.

    Parameter:
        name       — Pflichtfeld: Name der Baustelle, z. B. „MFH Deisterstraße 40, Feggendorf".
        projekt_id — Pflichtfeld: ID des übergeordneten Projekts (int).
        address    — Adresse der Baustelle, z. B. „Deisterstraße 40, Feggendorf" (optional).
        note       — Optionale Notiz / Beschreibung zur Baustelle (Standard: leer).

    Rückgabe: {"status": "ok", "id": <neue Baustellen-ID>}
    """
    payload: dict = {"name": name, "project_id": projekt_id}
    if address:
        payload["address"] = address
    if note:
        payload["note"] = note
    return _post("/api/sites", payload)


def baustellen_liste(projekt_id: int | None = None) -> dict:
    """
    Gibt alle Baustellen zurück, optional nach Projekt gefiltert.

    Parameter:
        projekt_id — Projekt-ID zur Filterung (int, optional). Fehlt dieser Parameter,
                     werden alle Baustellen über alle Projekte zurückgegeben.

    Rückgabe: {"status": "ok", "sites": [...]}
    """
    path = "/api/sites"
    if projekt_id is not None:
        path += f"?project={projekt_id}"
    return _get(path)


def baustelle_details(baustelle_id: int) -> dict:
    """
    Gibt Details zu einer Baustelle zurück, inklusive aller Gewerke.

    Parameter:
        baustelle_id — ID der Baustelle (int).

    Rückgabe: {"status": "ok", "site": {...}, "trades": [...]}
    """
    return _get(f"/api/sites/{baustelle_id}")


def baustelle_bearbeiten(
    baustelle_id: int,
    name: str | None = None,
    address: str | None = None,
    status: str | None = None,
    note: str | None = None,
    projekt_id: int | None = None,
) -> dict:
    """
    Ändert Felder einer bestehenden Baustelle.

    Parameter:
        baustelle_id — ID der zu ändernden Baustelle (int).
        name         — Neuer Name der Baustelle (optional).
        address      — Neue Adresse (optional).
        status       — Neuer Status: „aktiv" oder „abgeschlossen" (optional).
        note         — Neue Notiz (optional).
        projekt_id   — Neue übergeordnete Projekt-ID (optional, zum Umhängen der Baustelle).

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if address is not None:
        payload["address"] = address
    if status is not None:
        payload["status"] = status
    if note is not None:
        payload["note"] = note
    if projekt_id is not None:
        payload["project_id"] = projekt_id
    return _put(f"/api/sites/{baustelle_id}", payload)


def baustelle_loeschen(baustelle_id: int) -> dict:
    """
    Löscht eine Baustelle inkl. aller Gewerke, Aufgaben, Zeiteinträge,
    Materialpositionen und Dokumente (Kaskade).

    Parameter:
        baustelle_id — ID der zu löschenden Baustelle (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/sites/{baustelle_id}")


# ---------------------------------------------------------------------------
# 3. Gewerke
# ---------------------------------------------------------------------------

def gewerk_anlegen(baustelle_id: int, name: str) -> dict:
    """
    Legt ein neues Gewerk für eine Baustelle an.

    Parameter:
        baustelle_id — Pflichtfeld: ID der Baustelle, der das Gewerk zugeordnet wird (int).
        name         — Pflichtfeld: Name des Gewerks, z. B. „Maurer", „Elektro", „Sanitär",
                       „Trockenbau", „Dach", „Fenster".

    Rückgabe: {"status": "ok", "id": <neue Gewerk-ID>}
    """
    return _post(f"/api/sites/{baustelle_id}/trades", {"name": name})


def gewerke_liste(baustelle_id: int) -> dict:
    """
    Gibt alle Gewerke einer Baustelle zurück.

    Parameter:
        baustelle_id — ID der Baustelle (int).

    Rückgabe: {"status": "ok", "trades": [...]}
    """
    return _get(f"/api/sites/{baustelle_id}/trades")


def gewerk_bearbeiten(gewerk_id: int, name: str) -> dict:
    """
    Benennt ein Gewerk um.

    Parameter:
        gewerk_id — ID des zu ändernden Gewerks (int).
        name      — Neuer Name des Gewerks (str, Pflichtfeld).

    Rückgabe: {"status": "ok"}
    """
    return _put(f"/api/trades/{gewerk_id}", {"name": name})


def gewerk_loeschen(gewerk_id: int) -> dict:
    """
    Löscht ein Gewerk. Aufgaben, die diesem Gewerk zugeordnet waren,
    behalten ihre Daten (trade_id wird auf NULL gesetzt).

    Parameter:
        gewerk_id — ID des zu löschenden Gewerks (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/trades/{gewerk_id}")


# ---------------------------------------------------------------------------
# 4. Aufgaben
# ---------------------------------------------------------------------------

def aufgabe_anlegen(
    baustelle_id: int,
    titel: str,
    gewerk_id: int | None = None,
    status: str = "offen",
    beschreibung: str = "",
) -> dict:
    """
    Legt eine neue Aufgabe innerhalb einer Baustelle an.

    Parameter:
        baustelle_id — Pflichtfeld: ID der Baustelle (int).
        titel        — Pflichtfeld: Kurztitel der Aufgabe, z. B. „Wand ziehen".
        gewerk_id    — ID des zugehörigen Gewerks (int, optional).
        status       — Aufgabenstatus: „offen" (Standard), „in Arbeit" oder „fertig".
        beschreibung — Ausführliche Beschreibung / Notizen zur Aufgabe (optional).

    Rückgabe: {"status": "ok", "id": <neue Aufgaben-ID>}
    """
    payload: dict = {"title": titel, "status": status}
    if gewerk_id is not None:
        payload["trade_id"] = gewerk_id
    if beschreibung:
        payload["description"] = beschreibung
    return _post(f"/api/sites/{baustelle_id}/tasks", payload)


def aufgaben_liste(
    baustelle_id: int | None = None,
    gewerk_id: int | None = None,
    status: str | None = None,
) -> dict:
    """
    Gibt Aufgaben zurück, filterbar nach Baustelle, Gewerk und Status.

    Wenn baustelle_id angegeben ist, werden nur Aufgaben dieser Baustelle zurückgegeben.
    Ohne baustelle_id werden alle Baustellen abgefragt und die Aufgaben aggregiert
    (erfordert einen zusätzlichen Aufruf von baustellen_liste).

    Parameter:
        baustelle_id — ID der Baustelle (int, optional). Ohne Angabe: alle Baustellen.
        gewerk_id    — Nur Aufgaben dieses Gewerks anzeigen (int, optional).
        status       — Nur Aufgaben mit diesem Status: „offen", „in Arbeit" oder „fertig"
                       (optional).

    Rückgabe: {"status": "ok", "tasks": [...]}
    """
    if baustelle_id is not None:
        params: list[str] = []
        if gewerk_id is not None:
            params.append(f"trade={gewerk_id}")
        if status is not None:
            params.append(f"status={urllib.parse.quote(status)}")
        qs = ("?" + "&".join(params)) if params else ""
        return _get(f"/api/sites/{baustelle_id}/tasks{qs}")

    # Kein baustelle_id — alle Baustellen holen und Aufgaben aggregieren
    sites_resp = _get("/api/sites")
    if sites_resp.get("success") is False:
        return sites_resp
    all_tasks: list[dict] = []
    for site in sites_resp.get("sites", []):
        sid = site["id"]
        params_l: list[str] = []
        if gewerk_id is not None:
            params_l.append(f"trade={gewerk_id}")
        if status is not None:
            params_l.append(f"status={urllib.parse.quote(status)}")
        qs = ("?" + "&".join(params_l)) if params_l else ""
        resp = _get(f"/api/sites/{sid}/tasks{qs}")
        if resp.get("status") == "ok":
            all_tasks.extend(resp.get("tasks", []))
    return {"status": "ok", "tasks": all_tasks}


def aufgabe_details(aufgabe_id: int) -> dict:
    """
    Gibt Details zu einer Aufgabe zurück, inklusive Zeiteinträge, Materialpositionen,
    Dokumente und berechnete Kostensummen.

    Parameter:
        aufgabe_id — ID der Aufgabe (int).

    Rückgabe: {"status": "ok", "task": {...}, "time_entries": [...],
               "materials": [...], "documents": [...], "kosten": {...}}
    """
    return _get(f"/api/tasks/{aufgabe_id}")


def aufgabe_bearbeiten(
    aufgabe_id: int,
    titel: str | None = None,
    gewerk_id: int | None = None,
    status: str | None = None,
    beschreibung: str | None = None,
) -> dict:
    """
    Ändert Felder einer bestehenden Aufgabe.

    Parameter:
        aufgabe_id   — ID der zu ändernden Aufgabe (int).
        titel        — Neuer Titel (optional).
        gewerk_id    — Neue Gewerk-ID (int, optional; None lässt den aktuellen Wert unverändert).
        status       — Neuer Status: „offen", „in Arbeit" oder „fertig" (optional).
        beschreibung — Neue Beschreibung (optional).

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if titel is not None:
        payload["title"] = titel
    if gewerk_id is not None:
        payload["trade_id"] = gewerk_id
    if status is not None:
        payload["status"] = status
    if beschreibung is not None:
        payload["description"] = beschreibung
    return _put(f"/api/tasks/{aufgabe_id}", payload)


def aufgabe_loeschen(aufgabe_id: int) -> dict:
    """
    Löscht eine Aufgabe inkl. aller zugehörigen Zeiteinträge, Materialpositionen
    und Dokumente (Kaskade).

    Parameter:
        aufgabe_id — ID der zu löschenden Aufgabe (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/tasks/{aufgabe_id}")


# ---------------------------------------------------------------------------
# 5. Zeiterfassung (Personalkosten)
# ---------------------------------------------------------------------------

def zeiteintrag_anlegen(
    aufgabe_id: int,
    person: str,
    stunden: float,
    stundensatz: float,
    mit_rechnung: bool = True,
    datum: str = "",
) -> dict:
    """
    Erfasst einen Zeiteintrag (Personalkosten) zu einer Aufgabe.

    Die Kosten werden serverseitig berechnet: Kosten = Stunden × Stundensatz.

    Parameter:
        aufgabe_id   — Pflichtfeld: ID der zugehörigen Aufgabe (int).
        person       — Pflichtfeld: Name der Person / des Handwerkers (str).
        stunden      — Pflichtfeld: Anzahl geleisteter Stunden (float, z. B. 7.5).
        stundensatz  — Pflichtfeld: Stundensatz in Euro (float, z. B. 45.0).
        mit_rechnung — True = mit Rechnung (dokumentiert/versteuerbar, Standard),
                       False = ohne Rechnung.
        datum        — Leistungsdatum im ISO-Format YYYY-MM-DD (optional, z. B. „2025-03-15").

    Rückgabe: {"status": "ok", "id": <neue Zeiteintrag-ID>}
    """
    payload: dict = {
        "person": person,
        "hours": stunden,
        "hourly_rate": stundensatz,
        "billed": 1 if mit_rechnung else 0,
    }
    if datum:
        payload["work_date"] = datum
    return _post(f"/api/tasks/{aufgabe_id}/timeentries", payload)


def zeiteintraege_liste(aufgabe_id: int) -> dict:
    """
    Gibt alle Zeiteinträge (Personalkosten) einer Aufgabe zurück,
    inklusive berechneter Kosten je Eintrag.

    Parameter:
        aufgabe_id — ID der Aufgabe (int).

    Rückgabe: {"status": "ok", "time_entries": [...]}
    """
    return _get(f"/api/tasks/{aufgabe_id}/timeentries")


def zeiteintrag_bearbeiten(
    zeiteintrag_id: int,
    person: str | None = None,
    stunden: float | None = None,
    stundensatz: float | None = None,
    mit_rechnung: bool | None = None,
    datum: str | None = None,
) -> dict:
    """
    Ändert Felder eines bestehenden Zeiteintrags.

    Parameter:
        zeiteintrag_id — ID des zu ändernden Zeiteintrags (int).
        person         — Neuer Name der Person (optional).
        stunden        — Neue Stundenzahl (float, optional).
        stundensatz    — Neuer Stundensatz in Euro (float, optional).
        mit_rechnung   — True = mit Rechnung, False = ohne Rechnung (optional).
        datum          — Neues Leistungsdatum YYYY-MM-DD (optional).

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if person is not None:
        payload["person"] = person
    if stunden is not None:
        payload["hours"] = stunden
    if stundensatz is not None:
        payload["hourly_rate"] = stundensatz
    if mit_rechnung is not None:
        payload["billed"] = 1 if mit_rechnung else 0
    if datum is not None:
        payload["work_date"] = datum
    return _put(f"/api/timeentries/{zeiteintrag_id}", payload)


def zeiteintrag_loeschen(zeiteintrag_id: int) -> dict:
    """
    Löscht einen Zeiteintrag.

    Parameter:
        zeiteintrag_id — ID des zu löschenden Zeiteintrags (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/timeentries/{zeiteintrag_id}")


# ---------------------------------------------------------------------------
# 6. Material
# ---------------------------------------------------------------------------

def material_anlegen(
    aufgabe_id: int,
    bezeichnung: str,
    menge: float,
    einzelpreis: float,
    einheit: str = "Stk",
    mit_rechnung: bool = True,
) -> dict:
    """
    Legt eine Materialposition zu einer Aufgabe an.

    Die Kosten werden serverseitig berechnet: Kosten = Menge × Einzelpreis.

    Parameter:
        aufgabe_id   — Pflichtfeld: ID der zugehörigen Aufgabe (int).
        bezeichnung  — Pflichtfeld: Bezeichnung des Materials, z. B. „Zement 25 kg Sack".
        menge        — Pflichtfeld: Menge (float, z. B. 10.0).
        einzelpreis  — Pflichtfeld: Einzelpreis in Euro (float, z. B. 8.50).
        einheit      — Mengeneinheit: z. B. „Stk", „m²", „kg", „l", „lfm" (Standard: „Stk").
        mit_rechnung — True = mit Rechnung (dokumentiert/versteuerbar, Standard),
                       False = ohne Rechnung.

    Rückgabe: {"status": "ok", "id": <neue Material-ID>}
    """
    payload: dict = {
        "name": bezeichnung,
        "qty": menge,
        "unit_price": einzelpreis,
        "unit": einheit,
        "billed": 1 if mit_rechnung else 0,
    }
    return _post(f"/api/tasks/{aufgabe_id}/materials", payload)


def materialien_liste(aufgabe_id: int) -> dict:
    """
    Gibt alle Materialpositionen einer Aufgabe zurück,
    inklusive berechneter Kosten je Position.

    Parameter:
        aufgabe_id — ID der Aufgabe (int).

    Rückgabe: {"status": "ok", "materials": [...]}
    """
    return _get(f"/api/tasks/{aufgabe_id}/materials")


def material_bearbeiten(
    material_id: int,
    bezeichnung: str | None = None,
    menge: float | None = None,
    einheit: str | None = None,
    einzelpreis: float | None = None,
    mit_rechnung: bool | None = None,
) -> dict:
    """
    Ändert Felder einer bestehenden Materialposition.

    Parameter:
        material_id  — ID der zu ändernden Materialposition (int).
        bezeichnung  — Neue Bezeichnung (optional).
        menge        — Neue Menge (float, optional).
        einheit      — Neue Mengeneinheit, z. B. „m²", „kg" (optional).
        einzelpreis  — Neuer Einzelpreis in Euro (float, optional).
        mit_rechnung — True = mit Rechnung, False = ohne Rechnung (optional).

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if bezeichnung is not None:
        payload["name"] = bezeichnung
    if menge is not None:
        payload["qty"] = menge
    if einheit is not None:
        payload["unit"] = einheit
    if einzelpreis is not None:
        payload["unit_price"] = einzelpreis
    if mit_rechnung is not None:
        payload["billed"] = 1 if mit_rechnung else 0
    return _put(f"/api/materials/{material_id}", payload)


def material_loeschen(material_id: int) -> dict:
    """
    Löscht eine Materialposition (kaskadiert zu verknüpften Dokumenten).

    Parameter:
        material_id — ID der zu löschenden Materialposition (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/materials/{material_id}")


# ---------------------------------------------------------------------------
# 7. Kostenübersicht / Dashboard
# ---------------------------------------------------------------------------

def baustelle_summary(baustelle_id: int) -> dict:
    """
    Gibt Kosten-Summen für eine Baustelle zurück.

    Enthält Material- und Personalkosten, jeweils aufgeschlüsselt nach
    „mit Rechnung" / „ohne Rechnung", sowie eine Aufschlüsselung je Gewerk.

    Parameter:
        baustelle_id — ID der Baustelle (int).

    Rückgabe: {"status": "ok", "summary": {
        "material_mit_rechnung": ..., "material_ohne_rechnung": ...,
        "personal_mit_rechnung": ..., "personal_ohne_rechnung": ...,
        "gesamt": ..., "je_gewerk": [...]
    }}
    """
    return _get(f"/api/sites/{baustelle_id}/summary")


def projekt_summary(projekt_id: int) -> dict:
    """
    Gibt Kosten-Summen für ein Projekt zurück, aggregiert über alle Baustellen.

    Enthält Material- und Personalkosten, aufgeschlüsselt nach
    „mit Rechnung" / „ohne Rechnung", sowie Aufschlüsselung je Baustelle.

    Parameter:
        projekt_id — ID des Projekts (int).

    Rückgabe: {"status": "ok", "summary": {
        "material_mit_rechnung": ..., "material_ohne_rechnung": ...,
        "personal_mit_rechnung": ..., "personal_ohne_rechnung": ...,
        "gesamt": ..., "je_baustelle": [...]
    }}
    """
    return _get(f"/api/projects/{projekt_id}/summary")


def gesamt_summary() -> dict:
    """
    Gibt Kosten-Summen über alle Projekte und Baustellen zurück.

    Keine Parameter.

    Rückgabe: {"status": "ok", "summary": {
        "material_mit_rechnung": ..., "material_ohne_rechnung": ...,
        "personal_mit_rechnung": ..., "personal_ohne_rechnung": ...,
        "gesamt": ...
    }}
    """
    return _get("/api/summary")


# ---------------------------------------------------------------------------
# 8. Dokumente & Fotos
# ---------------------------------------------------------------------------

def dokument_hochladen(
    datei_pfad: str,
    aufgabe_id: int | None = None,
    material_id: int | None = None,
    doc_type: str = "beleg",
) -> dict:
    """
    Lädt eine lokale Datei (Foto, PDF) als Dokument zu einer Aufgabe oder
    Materialposition hoch.

    Erlaubte Dateitypen: .jpg, .jpeg, .png, .webp, .pdf (max. 20 MB).
    Es muss genau eine Zuordnung angegeben werden: entweder aufgabe_id oder material_id.

    Parameter:
        datei_pfad  — Pflichtfeld: Absoluter oder relativer Pfad zur lokalen Datei auf dem Server
                      (str), z. B. „/home/sven/fotos/rechnung.pdf".
        aufgabe_id  — ID der Aufgabe, der das Dokument zugeordnet wird (int, optional).
                      Entweder aufgabe_id oder material_id muss gesetzt sein.
        material_id — ID der Materialposition, der das Dokument zugeordnet wird (int, optional).
                      Entweder aufgabe_id oder material_id muss gesetzt sein.
        doc_type    — Dokumenttyp: „rechnung", „beleg" (Standard) oder „foto".

    Rückgabe: {"status": "ok", "id": <doc_id>, "filename": "...", "stored_name": "..."}
    """
    if not os.path.isfile(datei_pfad):
        return {"success": False, "error": f"Datei nicht gefunden: {datei_pfad}"}
    if aufgabe_id is None and material_id is None:
        return {"success": False, "error": "aufgabe_id oder material_id muss angegeben werden."}

    fields: dict = {"doc_type": doc_type}
    if aufgabe_id is not None:
        fields["task_id"] = str(aufgabe_id)
    if material_id is not None:
        fields["material_id"] = str(material_id)

    try:
        boundary, body = _multipart(fields, datei_pfad)
    except OSError as exc:
        return {"success": False, "error": f"Datei konnte nicht gelesen werden: {exc}"}

    url = BASE_URL.rstrip("/") + "/api/documents"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {"status": "ok"}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(detail)
            msg = parsed.get("error", detail[:300])
        except Exception:
            msg = exc.reason or str(exc)
        return {"success": False, "error": f"HTTP {exc.code}: {msg}"}
    except urllib.error.URLError as exc:
        return {"success": False, "error": f"baulog nicht erreichbar ({BASE_URL}): {exc.reason}"}


def dokumente_liste(
    aufgabe_id: int | None = None,
    material_id: int | None = None,
    baustelle_id: int | None = None,
    doc_type: str | None = None,
) -> dict:
    """
    Gibt Dokumente zurück, filterbar nach Aufgabe, Materialposition, Baustelle oder Typ.

    Alle Parameter sind optional und können kombiniert werden.

    Parameter:
        aufgabe_id   — Nur Dokumente dieser Aufgabe (int, optional).
        material_id  — Nur Dokumente dieser Materialposition (int, optional).
        baustelle_id — Nur Dokumente dieser Baustelle (int, optional; nützlich für Galerie).
        doc_type     — Nur Dokumente dieses Typs: „rechnung", „beleg" oder „foto" (optional).

    Rückgabe: {"status": "ok", "documents": [...]}
    """
    params: list[str] = []
    if aufgabe_id is not None:
        params.append(f"task_id={aufgabe_id}")
    if material_id is not None:
        params.append(f"material_id={material_id}")
    if baustelle_id is not None:
        params.append(f"site_id={baustelle_id}")
    if doc_type is not None:
        params.append(f"doc_type={urllib.parse.quote(doc_type)}")
    qs = ("?" + "&".join(params)) if params else ""
    return _get(f"/api/documents{qs}")


def dokument_loeschen(dokument_id: int) -> dict:
    """
    Löscht ein Dokument aus der Datenbank und entfernt die zugehörige Datei vom Dateisystem.

    Parameter:
        dokument_id — ID des zu löschenden Dokuments (int).

    Rückgabe: {"status": "ok"}
    """
    return _delete(f"/api/documents/{dokument_id}")


def dokument_zu_paperless(dokument_id: int) -> dict:
    """
    Sendet ein bereits hochgeladenes Dokument an Paperless-ngx und speichert
    die zurückgegebene Paperless-Dokument-ID und den direkten Link.

    Voraussetzung: Paperless muss in config.local.json oder über die Umgebungsvariablen
    PAPERLESS_URL und PAPERLESS_TOKEN konfiguriert sein. Andernfalls wird ein Fehler
    zurückgegeben.

    Parameter:
        dokument_id — ID des Dokuments in baulog (int).

    Rückgabe: {"status": "ok", "paperless_id": ..., "paperless_url": "..."}
    """
    return _post(f"/api/documents/{dokument_id}/paperless", {})


# ---------------------------------------------------------------------------
# Tool-Registrierung
# ---------------------------------------------------------------------------

# Projekte
register_tool("projekt_anlegen",      projekt_anlegen)
register_tool("projekte_liste",       projekte_liste)
register_tool("projekt_details",      projekt_details)
register_tool("projekt_bearbeiten",   projekt_bearbeiten)
register_tool("projekt_loeschen",     projekt_loeschen)

# Baustellen
register_tool("baustelle_anlegen",    baustelle_anlegen)
register_tool("baustellen_liste",     baustellen_liste)
register_tool("baustelle_details",    baustelle_details)
register_tool("baustelle_bearbeiten", baustelle_bearbeiten)
register_tool("baustelle_loeschen",   baustelle_loeschen)

# Gewerke
register_tool("gewerk_anlegen",       gewerk_anlegen)
register_tool("gewerke_liste",        gewerke_liste)
register_tool("gewerk_bearbeiten",    gewerk_bearbeiten)
register_tool("gewerk_loeschen",      gewerk_loeschen)

# Aufgaben
register_tool("aufgabe_anlegen",      aufgabe_anlegen)
register_tool("aufgaben_liste",       aufgaben_liste)
register_tool("aufgabe_details",      aufgabe_details)
register_tool("aufgabe_bearbeiten",   aufgabe_bearbeiten)
register_tool("aufgabe_loeschen",     aufgabe_loeschen)

# Zeiterfassung
register_tool("zeiteintrag_anlegen",  zeiteintrag_anlegen)
register_tool("zeiteintraege_liste",  zeiteintraege_liste)
register_tool("zeiteintrag_bearbeiten", zeiteintrag_bearbeiten)
register_tool("zeiteintrag_loeschen", zeiteintrag_loeschen)

# Material
register_tool("material_anlegen",     material_anlegen)
register_tool("materialien_liste",    materialien_liste)
register_tool("material_bearbeiten",  material_bearbeiten)
register_tool("material_loeschen",    material_loeschen)

# Kostenübersicht
register_tool("baustelle_summary",    baustelle_summary)
register_tool("projekt_summary",      projekt_summary)
register_tool("gesamt_summary",       gesamt_summary)

# Dokumente & Fotos
register_tool("dokument_hochladen",   dokument_hochladen)
register_tool("dokumente_liste",      dokumente_liste)
register_tool("dokument_loeschen",    dokument_loeschen)
register_tool("dokument_zu_paperless", dokument_zu_paperless)

# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_server()
