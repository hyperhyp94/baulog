"""
baulog_mcp.py — MCP-Server für baulog.

Dünner HTTP-Client auf die REST-API von server.py (Port 5112).
Wird nach ~/hermes-tools/mcp/ kopiert; mcp_helper.py liegt dort im selben Verzeichnis.

Alle Tools gemäß PLAN.md Abschnitt 9 (Stand: 41 registrierte Tools).

Rückgabe-Konvention:
  * Erfolg:  {"status": "ok", ...}
  * Fehler:  {"success": False, "error": "<Klartext>"}
Ein Aufruf gilt genau dann als fehlgeschlagen, wenn "success" vorhanden
und False ist.

Typ-Annotationen: mcp_helper erkennt nur einfache Typen (int, float, bool).
Optionale Parameter deshalb als `int = None` statt `int | None = None`
schreiben — sonst meldet das Schema „string“ und das Modell schickt Text
(„false“ wäre dann wahr). Ja/Nein- und Datumswerte zusätzlich mit _ja_nein /
_datum auswerten, weil Modelle trotzdem gelegentlich Text schicken.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_helper import register_tool, run_server  # noqa: E402 — erst nach sys.path-Patch

import json
import mimetypes
import re
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BAULOG_URL", "http://127.0.0.1:5112")


# ---------------------------------------------------------------------------
# HTTP-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _request(method: str, path: str, payload: dict | None = None) -> dict:
    """
    Sendet einen JSON-Request an die baulog-REST-API (siehe _send).
    """
    body: bytes | None = None
    headers: dict = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return _send(method, path, body, headers)


def _send(method: str, path: str, body: bytes | None, headers: dict) -> dict:
    """
    Schickt einen fertigen Request-Body an die API (JSON oder Multipart).

    Bei HTTPError wird {"success": False, "error": "HTTP <code>: <detail>"} zurückgegeben.
    Bei URLError (Server nicht erreichbar) {"success": False, "error": "baulog nicht erreichbar …"}.
    """
    url = BASE_URL.rstrip("/") + path
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


def _fehler(text: str) -> dict:
    return {"success": False, "error": text}


def _ok_antwort(res: dict) -> bool:
    return res.get("success") is not False


# ---------------------------------------------------------------------------
# Eingaben normalisieren (Modelle schicken Ja/Nein und Daten oft als Text)
# ---------------------------------------------------------------------------

_JA = {"1", "true", "ja", "yes", "wahr", "mit", "bezahlt"}
_NEIN = {"0", "false", "nein", "no", "falsch", "ohne", "offen", "none", ""}


def _ja_nein(wert, feld: str):
    """Wandelt True/False, 1/0 oder Text („ja“, „false“ …) in 1/0. Rückgabe (wert, fehler)."""
    if isinstance(wert, bool):
        return (1 if wert else 0), None
    if isinstance(wert, (int, float)) and wert in (0, 1):
        return int(wert), None
    if isinstance(wert, str):
        s = wert.strip().lower()
        if s in _JA:
            return 1, None
        if s in _NEIN:
            return 0, None
    return None, _fehler(f"'{feld}' muss ja/nein sein (true/false), nicht {wert!r}.")


def _datum(wert):
    """
    Akzeptiert YYYY-MM-DD, TT.MM.JJJJ, TT.MM.JJ, TT.MM. sowie heute/gestern/vorgestern.
    Rückgabe (iso_datum | None, fehler). Leerer Wert → (None, None).
    """
    if wert is None:
        return None, None
    s = str(wert).strip().lower()
    if not s:
        return None, None
    heute = date.today()
    relativ = {"heute": 0, "gestern": 1, "vorgestern": 2}
    if s in relativ:
        return (heute - timedelta(days=relativ[s])).isoformat(), None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s).isoformat(), None
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})?", s)
        if m:
            jahr = m.group(3)
            jahr = heute.year if not jahr else (2000 + int(jahr) if len(jahr) == 2 else int(jahr))
            return date(jahr, int(m.group(2)), int(m.group(1))).isoformat(), None
    except ValueError:
        pass
    return None, _fehler(f"Datum {wert!r} nicht erkannt. Erlaubt: YYYY-MM-DD, TT.MM.JJJJ, "
                         f"heute, gestern, vorgestern.")


_DOC_TYPEN = {"rechnung": "rechnung", "beleg": "beleg", "foto": "foto",
              "bon": "beleg", "kassenbon": "beleg", "quittung": "beleg", "kassenzettel": "beleg",
              "bild": "foto", "photo": "foto", "image": "foto", "invoice": "rechnung"}
_ERLAUBTE_ENDUNGEN = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
_MAX_UPLOAD = 20 * 1024 * 1024


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
# Personen-Auflösung
# ---------------------------------------------------------------------------

def _resolve_person(name: str) -> tuple[dict | None, dict | None]:
    """
    Löst einen Personennamen gegen die Personen-Stammdaten auf.

    Erst exakter Match, dann case-insensitiv (nur wenn eindeutig).
    Rückgabe: (person_dict, None) bei Erfolg, (None, fehler_dict) bei Fehler.
    """
    res = _get("/api/persons")
    if res.get("success") is False:
        return None, res
    persons = res.get("persons", [])
    exact = [p for p in persons if p["name"] == name]
    if len(exact) == 1:
        return exact[0], None
    ci = [p for p in persons if p["name"].lower() == name.lower()]
    if len(ci) == 1:
        return ci[0], None
    known = ", ".join(sorted(p["name"] for p in persons)) or "keine"
    return None, {
        "success": False,
        "error": (f"Person '{name}' nicht gefunden. Bekannte Personen: {known}. "
                  f"Neue Personen zuerst mit person_anlegen anlegen."),
    }


# ---------------------------------------------------------------------------
# 1. Projekte
# ---------------------------------------------------------------------------

def _projekt_baustelle(projekt_id: int) -> tuple[int | None, dict | None]:
    """
    Liefert die Baustelle eines Projekts — wie in der Weboberfläche, die pro
    Projekt genau eine Baustelle im Hintergrund führt. Hat das Projekt noch
    keine, wird sie (benannt wie das Projekt) angelegt. Bei mehreren Bereichen
    muss der Aufrufer baustelle_id angeben.
    """
    res = _get(f"/api/projects/{projekt_id}")
    if not _ok_antwort(res):
        return None, res
    sites = res.get("sites", [])
    if len(sites) == 1:
        return sites[0]["id"], None
    if not sites:
        neu = _post("/api/sites", {"name": res["project"]["name"], "project_id": projekt_id})
        if not _ok_antwort(neu):
            return None, neu
        return neu["id"], None
    namen = ", ".join(f"{s['name']} (baustelle_id={s['id']})" for s in sites)
    return None, _fehler(f"Projekt {projekt_id} hat mehrere Bereiche: {namen}. "
                         f"Bitte baustelle_id angeben.")


def projekt_anlegen(name: str, note: str = "", adresse: str = "") -> dict:
    """
    Legt ein neues Projekt an — der Einstieg für alles Weitere.

    So ist baulog aufgebaut (wie in der Weboberfläche):
      Projekt → Aufgaben → Stunden (inkl. Anfahrt), Material, Fotos/Belege.
    Jedes Projekt hat automatisch genau eine Baustelle im Hintergrund; sie wird
    hier gleich mit angelegt. baustelle_anlegen ist NICHT nötig.
    Danach: aufgabe_anlegen(projekt_id=…, titel=…), dann zeiteintrag_anlegen /
    material_anlegen / dokument_hochladen mit der aufgabe_id.
    Vorher mit projekte_liste prüfen, ob es das Projekt schon gibt.

    Parameter:
        name    — Pflichtfeld: Projektbezeichnung, z. B. „Renovierung 1 OG Miethaus".
        note    — Optionale Beschreibung zum Projekt.
        adresse — Optionale Adresse (Straße, Ort). Nicht erfinden.

    Rückgabe: {"status": "ok", "id": <Projekt-ID>, "baustelle_id": <ID der Hintergrund-Baustelle>}
    """
    payload: dict = {"name": name}
    if note:
        payload["note"] = note
    res = _post("/api/projects", payload)
    if not _ok_antwort(res):
        return res
    site: dict = {"name": name, "project_id": res["id"]}
    if adresse:
        site["address"] = adresse
    s = _post("/api/sites", site)
    if not _ok_antwort(s):
        return {**s, "projekt_id": res["id"],
                "hinweis": "Projekt wurde angelegt, die Baustelle nicht."}
    return {"status": "ok", "id": res["id"], "baustelle_id": s["id"]}


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


def projekt_bearbeiten(projekt_id: int, name: str = None, note: str = None) -> dict:
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
    Legt einen ZUSÄTZLICHEN Bereich (Baustelle) in einem Projekt an.

    Normalerweise NICHT nötig: projekt_anlegen legt die Baustelle schon mit an,
    und in der Weboberfläche hat jedes Projekt genau eine. Nur verwenden, wenn
    Sven ausdrücklich getrennte Bereiche will (z. B. „EG“ und „Dach“).

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


def baustellen_liste(projekt_id: int = None) -> dict:
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
    name: str = None,
    address: str = None,
    status: str = None,
    note: str = None,
    projekt_id: int = None,
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

def _gewerk_id(baustelle_id: int, gewerk: str) -> tuple[int | None, dict | None]:
    """Findet ein Gewerk der Baustelle per Name (ohne Groß-/Kleinschreibung) oder legt es an."""
    res = _get(f"/api/sites/{baustelle_id}/trades")
    if not _ok_antwort(res):
        return None, res
    for t in res.get("trades", []):
        if t["name"].strip().lower() == gewerk.strip().lower():
            return t["id"], None
    neu = _post(f"/api/sites/{baustelle_id}/trades", {"name": gewerk.strip()})
    if not _ok_antwort(neu):
        return None, neu
    return neu["id"], None


def aufgabe_anlegen(
    titel: str,
    projekt_id: int = None,
    baustelle_id: int = None,
    gewerk: str = "",
    gewerk_id: int = None,
    status: str = "offen",
    beschreibung: str = "",
) -> dict:
    """
    Legt eine Aufgabe in einem Projekt an, z. B. „Trockenbau Bad", „Fenstertausch"
    oder einfach „Allgemein". Stunden, Material und Belege hängen immer an einer
    Aufgabe. Vorher mit aufgaben_liste(projekt_id=…) prüfen, ob es schon eine
    passende gibt — nicht doppelt anlegen.

    Parameter:
        titel        — Pflichtfeld: Bezeichnung der Aufgabe.
        projekt_id   — ID des Projekts (Normalfall); die Baustelle wird automatisch ermittelt.
        baustelle_id — Nur bei Projekten mit mehreren Bereichen statt projekt_id angeben.
        gewerk       — Optionaler Gewerk-Name, z. B. „Trockenbau"; wird bei Bedarf angelegt.
        gewerk_id    — Alternativ die ID eines bestehenden Gewerks.
        status       — „offen" (Standard), „in Arbeit" oder „fertig".
        beschreibung — Optionale Notiz zur Aufgabe.

    Rückgabe: {"status": "ok", "id": <aufgabe_id>}
    """
    if baustelle_id is None:
        if projekt_id is None:
            return _fehler("projekt_id angeben (bei mehreren Bereichen stattdessen baustelle_id).")
        baustelle_id, err = _projekt_baustelle(projekt_id)
        if err:
            return err
    payload: dict = {"title": titel, "status": status}
    if gewerk_id is None and gewerk and gewerk.strip():
        gewerk_id, err = _gewerk_id(baustelle_id, gewerk)
        if err:
            return err
    if gewerk_id is not None:
        payload["trade_id"] = gewerk_id
    if beschreibung:
        payload["description"] = beschreibung
    return _post(f"/api/sites/{baustelle_id}/tasks", payload)


def aufgaben_liste(
    projekt_id: int = None,
    baustelle_id: int = None,
    gewerk_id: int = None,
    status: str = None,
) -> dict:
    """
    Gibt Aufgaben zurück (mit aufgabe_id = "id", Titel, Status, Kosten), filterbar
    nach Projekt, Baustelle, Gewerk und Status.

    Parameter:
        projekt_id   — Nur Aufgaben dieses Projekts (Normalfall).
        baustelle_id — Nur Aufgaben dieser Baustelle / dieses Bereichs.
        gewerk_id    — Nur Aufgaben dieses Gewerks.
        status       — Nur Aufgaben mit Status „offen", „in Arbeit" oder „fertig".
        Ohne projekt_id und baustelle_id: Aufgaben aller Projekte.

    Rückgabe: {"status": "ok", "tasks": [...]}
    """
    params: list[str] = []
    if gewerk_id is not None:
        params.append(f"trade={gewerk_id}")
    if status is not None:
        params.append(f"status={urllib.parse.quote(status)}")
    qs = ("?" + "&".join(params)) if params else ""

    if baustelle_id is not None:
        return _get(f"/api/sites/{baustelle_id}/tasks{qs}")

    sites_resp = _get(f"/api/sites?project={projekt_id}" if projekt_id is not None else "/api/sites")
    if not _ok_antwort(sites_resp):
        return sites_resp
    all_tasks: list[dict] = []
    for site in sites_resp.get("sites", []):
        resp = _get(f"/api/sites/{site['id']}/tasks{qs}")
        if not _ok_antwort(resp):
            return resp
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
    titel: str = None,
    gewerk: str = None,
    gewerk_id: int = None,
    status: str = None,
    beschreibung: str = None,
) -> dict:
    """
    Ändert Felder einer bestehenden Aufgabe (nur die angegebenen).

    Parameter:
        aufgabe_id   — ID der zu ändernden Aufgabe (int).
        titel        — Neuer Titel.
        gewerk       — Gewerk per Name setzen (wird bei Bedarf angelegt); "" entfernt das Gewerk.
        gewerk_id    — Alternativ: ID eines bestehenden Gewerks.
        status       — Neuer Status: „offen", „in Arbeit" oder „fertig".
        beschreibung — Neue Beschreibung.

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if titel is not None:
        payload["title"] = titel
    if gewerk is not None and gewerk_id is None:
        if not gewerk.strip():
            payload["trade_id"] = None
        else:
            task = _get(f"/api/tasks/{aufgabe_id}")
            if not _ok_antwort(task):
                return task
            gewerk_id, err = _gewerk_id(task["task"]["site_id"], gewerk)
            if err:
                return err
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

def zeiteintrag_anlegen(aufgabe_id: int, person: str, stunden: float,
                        datum: str = "", stundensatz: float = None,
                        anfahrt: float = 0.0, mit_rechnung: bool = True,
                        bezahlt: bool = False, bereits_gezahlt: float = None) -> dict:
    """
    Erfasst einen Arbeitstag einer Person: Stunden (+ optional Anfahrt) auf einer Aufgabe.
    Kosten = Stunden × Stundensatz + Anfahrt. Ein Eintrag je Person und Tag.

    Vorher mit projekt_uebersicht prüfen, ob der Tag schon erfasst ist (nicht doppelt anlegen).

    :param aufgabe_id: ID der Aufgabe (aus aufgaben_liste oder projekt_uebersicht).
    :param person: Name der Person — muss in den Stammdaten existieren
                   (personen_liste; neue Personen zuerst mit person_anlegen).
    :param stunden: Stunden an diesem Tag (> 0), z. B. 6.5.
    :param datum: Tag der Arbeit: YYYY-MM-DD, TT.MM.JJJJ, „heute", „gestern" oder
                  „vorgestern". Immer angeben, wenn Sven einen Tag nennt.
    :param stundensatz: Euro/Stunde; weglassen = Standard-Satz der Person. Hat die
                        Person keinen Satz (0), wird abgelehnt — dann Satz der Person
                        pflegen (person_bearbeiten) oder hier angeben (0 nur für
                        bewusst unbezahlte Eigenleistung).
    :param anfahrt: Anfahrt in Euro für diesen Arbeitstag, z. B. 10. Anfahrten IMMER
                    hier mitgeben — NICHT als Material („Anfahrt …") anlegen.
    :param mit_rechnung: True = mit Rechnung (Standard), False = ohne Rechnung.
    :param bezahlt: True = schon bezahlt, False = noch offen (Standard).
                    mit_rechnung und bezahlt sind unabhängig voneinander.
    :param bereits_gezahlt: Teilzahlung in Euro (z. B. 50), Rest bleibt offen. Nur bei
                    bezahlt=False; ab dem vollen Betrag gilt der Eintrag als bezahlt.
    """
    p, err = _resolve_person(person)
    if err:
        return err
    billed, err = _ja_nein(mit_rechnung, "mit_rechnung")
    if err:
        return err
    paid, err = _ja_nein(bezahlt, "bezahlt")
    if err:
        return err
    work_date, err = _datum(datum)
    if err:
        return err
    payload = {"person_id": p["id"], "hours": stunden, "billed": billed, "paid": paid}
    if bereits_gezahlt:
        payload["paid_amount"] = bereits_gezahlt
    if stundensatz is not None:
        payload["hourly_rate"] = stundensatz
    if work_date:
        payload["work_date"] = work_date
    if anfahrt:
        payload["travel_cost"] = anfahrt
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


def zeiteintrag_bearbeiten(zeiteintrag_id: int, person: str = None,
                           stunden: float = None, stundensatz: float = None,
                           mit_rechnung: bool = None, datum: str = None,
                           bezahlt: bool = None, anfahrt: float = None,
                           bereits_gezahlt: float = None) -> dict:
    """
    Ändert nur die angegebenen Felder eines Zeiteintrags.

    :param zeiteintrag_id: ID des Zeiteintrags (aus projekt_uebersicht oder zeiteintraege_liste).
    :param person: Neuer Personenname — muss in den Stammdaten existieren.
    :param stunden: Neuer Stundenaufwand (> 0).
    :param stundensatz: Neuer Stundensatz in Euro/Stunde (>= 0).
    :param mit_rechnung: True = mit Rechnung, False = ohne Rechnung.
    :param datum: Tag der Arbeit (YYYY-MM-DD, TT.MM.JJJJ, heute, gestern); "" entfernt das Datum.
    :param bezahlt: True = bezahlt, False = offen.
    :param anfahrt: Anfahrt in Euro für diesen Arbeitstag (0 = keine Anfahrt).
    :param bereits_gezahlt: Teilzahlung in Euro für DIESEN Eintrag (Gesamtstand, nicht
                    zusätzlich); Rest bleibt offen. 0 = nichts gezahlt. Ab dem vollen
                    Betrag gilt der Eintrag als bezahlt. Nicht zusammen mit bezahlt.
                    Für Beträge über mehrere Tage: person_auszahlung.
    """
    payload = {}
    if bereits_gezahlt is not None:
        if bezahlt is not None:
            return _fehler("bezahlt und bereits_gezahlt nicht gleichzeitig angeben.")
        payload["paid_amount"] = bereits_gezahlt
    if person is not None:
        p, err = _resolve_person(person)
        if err:
            return err
        payload["person_id"] = p["id"]
    if stunden is not None:
        payload["hours"] = stunden
    if stundensatz is not None:
        payload["hourly_rate"] = stundensatz
    if mit_rechnung is not None:
        payload["billed"], err = _ja_nein(mit_rechnung, "mit_rechnung")
        if err:
            return err
    if bezahlt is not None:
        payload["paid"], err = _ja_nein(bezahlt, "bezahlt")
        if err:
            return err
    if anfahrt is not None:
        payload["travel_cost"] = anfahrt
    if datum is not None:
        payload["work_date"], err = _datum(datum)
        if err:
            return err
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
# 5.5. Personen (Stammdaten)
# ---------------------------------------------------------------------------

def person_anlegen(name: str, stundensatz: float = 0.0, note: str = "") -> dict:
    """
    Legt eine Person (Stammdaten) an.

    :param name: Eindeutiger Name der Person (Pflicht), z. B. "Robert".
    :param stundensatz: Standard-Stundensatz in Euro/Stunde (>= 0, Standard 0).
    :param note: Optionale Notiz.
    """
    return _post("/api/persons", {"name": name, "hourly_rate": stundensatz,
                                  "note": note or None})


def personen_liste(nur_aktive: bool = True) -> dict:
    """
    Listet Personen inkl. Summen (Stunden, Kosten, Anzahl Zeiteinträge).

    :param nur_aktive: True = nur aktive Personen (Standard), False = alle.
    """
    aktiv, err = _ja_nein(nur_aktive, "nur_aktive")
    if err:
        return err
    return _get("/api/persons?active=1" if aktiv else "/api/persons")


def person_bearbeiten(person_id: int, name: str = None, stundensatz: float = None,
                      aktiv: bool = None, note: str = None) -> dict:
    """
    Ändert nur die angegebenen Felder einer Person.

    :param person_id: ID der Person (aus personen_liste).
    :param name: Neuer Name; Umbenennen aktualisiert auch bestehende Zeiteinträge.
    :param stundensatz: Neuer Standard-Stundensatz in Euro/Stunde (>= 0).
    :param aktiv: False deaktiviert die Person (erscheint nicht mehr in Auswahl),
                  True reaktiviert sie.
    :param note: Neue Notiz.
    """
    payload = {}
    if name is not None:
        payload["name"] = name
    if stundensatz is not None:
        payload["hourly_rate"] = stundensatz
    if aktiv is not None:
        payload["active"], err = _ja_nein(aktiv, "aktiv")
        if err:
            return err
    if note is not None:
        payload["note"] = note
    return _put(f"/api/persons/{person_id}", payload)


def person_loeschen(person_id: int) -> dict:
    """
    Löscht eine Person ohne Zeiteinträge. Mit Zeiteinträgen liefert die API einen
    Fehler mit dem Hinweis, die Person stattdessen zu deaktivieren
    (person_bearbeiten mit aktiv=False).

    :param person_id: ID der Person.
    """
    return _delete(f"/api/persons/{person_id}")


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
    bezahlt: bool = False,
    bereits_gezahlt: float = None,
) -> dict:
    """
    Legt eine Materialposition / Ausgabe zu einer Aufgabe an (Einkauf, Miete, Entsorgung …).
    Kosten = Menge × Einzelpreis. Bei einem Gesamtbetrag: menge=1, einzelpreis=Betrag.

    Anfahrten NICHT hier erfassen, sondern als anfahrt bei zeiteintrag_anlegen.
    Einen Kassenbon/eine Rechnung danach mit dokument_hochladen(material_id=…) anhängen.

    Parameter:
        aufgabe_id   — Pflichtfeld: ID der zugehörigen Aufgabe (int).
        bezeichnung  — Pflichtfeld: Was wurde gekauft, z. B. „Zement 25 kg Sack".
        menge        — Pflichtfeld: Menge (z. B. 10).
        einzelpreis  — Pflichtfeld: Preis je Einheit in Euro (z. B. 8.50).
        einheit      — Mengeneinheit: „Stk" (Standard), „m²", „kg", „l", „Sack", „Pauschal" …
        mit_rechnung — True = mit Rechnung (Standard), False = ohne Rechnung (z. B. Barkauf).
        bezahlt      — True = schon bezahlt, False = noch offen (Standard).

    Rückgabe: {"status": "ok", "id": <material_id>}
    """
    billed, err = _ja_nein(mit_rechnung, "mit_rechnung")
    if err:
        return err
    paid, err = _ja_nein(bezahlt, "bezahlt")
    if err:
        return err
    payload: dict = {
        "name": bezeichnung,
        "qty": menge,
        "unit_price": einzelpreis,
        "unit": einheit,
        "billed": billed,
        "paid": paid,
    }
    if bereits_gezahlt:
        payload["paid_amount"] = bereits_gezahlt
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
    bezeichnung: str = None,
    menge: float = None,
    einheit: str = None,
    einzelpreis: float = None,
    mit_rechnung: bool = None,
    bezahlt: bool = None,
    bereits_gezahlt: float = None,
) -> dict:
    """
    Ändert nur die angegebenen Felder einer Materialposition.

    Parameter:
        material_id  — ID der Materialposition (aus projekt_uebersicht oder materialien_liste).
        bezeichnung  — Neue Bezeichnung.
        menge        — Neue Menge.
        einheit      — Neue Mengeneinheit, z. B. „m²", „kg".
        einzelpreis  — Neuer Einzelpreis in Euro.
        mit_rechnung — True = mit Rechnung, False = ohne Rechnung.
        bezahlt      — True = bezahlt, False = offen.
        bereits_gezahlt — Teilzahlung in Euro (Gesamtstand); Rest bleibt offen. Nicht
                       zusammen mit bezahlt.

    Rückgabe: {"status": "ok"}
    """
    payload: dict = {}
    if bereits_gezahlt is not None:
        if bezahlt is not None:
            return _fehler("bezahlt und bereits_gezahlt nicht gleichzeitig angeben.")
        payload["paid_amount"] = bereits_gezahlt
    if bezeichnung is not None:
        payload["name"] = bezeichnung
    if menge is not None:
        payload["qty"] = menge
    if einheit is not None:
        payload["unit"] = einheit
    if einzelpreis is not None:
        payload["unit_price"] = einzelpreis
    if mit_rechnung is not None:
        payload["billed"], err = _ja_nein(mit_rechnung, "mit_rechnung")
        if err:
            return err
    if bezahlt is not None:
        payload["paid"], err = _ja_nein(bezahlt, "bezahlt")
        if err:
            return err
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
        "mat_mit": ..., "mat_ohne": ..., "mat_gesamt": ...,
        "pers_mit": ..., "pers_ohne": ..., "pers_gesamt": ...,
        "gesamt_mit": ..., "gesamt_ohne": ..., "gesamt": ...,
        "by_trade": [{"trade_id", "trade_name", "mat_mit", "mat_ohne",
                      "pers_mit", "pers_ohne", "gesamt", ...}]
    }}
    Alle Beträge in Euro. „mit"/„ohne" bezieht sich auf das Rechnungs-Flag.
    """
    return _get(f"/api/sites/{baustelle_id}/summary")


def projekt_summary(projekt_id: int) -> dict:
    """
    Gibt Kosten-Summen für ein Projekt zurück, aggregiert über alle Baustellen.

    Enthält Material- und Personalkosten, aufgeschlüsselt nach
    „mit Rechnung" / „ohne Rechnung", sowie eine Aufschlüsselung je Gewerk.

    Für eine Aufschlüsselung je BAUSTELLE stattdessen dashboard_summary
    (Feld „by_site") verwenden — dieses Tool liefert sie nicht.

    Parameter:
        projekt_id — ID des Projekts (int).

    Rückgabe: {"status": "ok", "summary": {
        "mat_mit": ..., "mat_ohne": ..., "mat_gesamt": ...,
        "pers_mit": ..., "pers_ohne": ..., "pers_gesamt": ...,
        "gesamt_mit": ..., "gesamt_ohne": ..., "gesamt": ...,
        "by_trade": [...]
    }}
    """
    return _get(f"/api/projects/{projekt_id}/summary")


def gesamt_summary() -> dict:
    """
    Gibt Kosten-Summen über alle Projekte und Baustellen zurück.

    Keine Parameter.

    Rückgabe: {"status": "ok", "summary": {
        "mat_mit": ..., "mat_ohne": ..., "mat_gesamt": ...,
        "pers_mit": ..., "pers_ohne": ..., "pers_gesamt": ...,
        "gesamt_mit": ..., "gesamt_ohne": ..., "gesamt": ...,
        "by_trade": [...]
    }}
    """
    return _get("/api/summary")


def _r2(wert) -> float:
    return round(float(wert or 0), 2)


def projekt_uebersicht(projekt_id: int) -> dict:
    """
    ALLES zu einem Projekt in einem Aufruf — der beste Einstieg, bevor etwas
    erfasst, geändert oder als bezahlt markiert wird (zeigt auch, ob ein Tag
    schon erfasst ist). Entspricht der Projektseite der Weboberfläche.

    Parameter:
        projekt_id — ID des Projekts (aus projekte_liste).

    Rückgabe (deutsche Felder, Beträge in Euro, IDs direkt für die anderen Tools):
      projekt, bereiche[] (baustelle_id …),
      aufgaben[]  (aufgabe_id, titel, status, gewerk, baustelle_id),
      stunden[]   (zeiteintrag_id, aufgabe_id, datum, person, stunden, stundensatz,
                   anfahrt, betrag, mit_rechnung, bezahlt, teilweise_gezahlt, gezahlt,
                   offen) — neueste zuerst. teilweise_gezahlt = Teilzahlung (Euro) bei
                   noch nicht voll bezahlten Posten; offen = Restbetrag,
      material[]  (material_id, aufgabe_id, erfasst_am, bezeichnung, menge, einheit,
                   einzelpreis, betrag, mit_rechnung, bezahlt, teilweise_gezahlt,
                   gezahlt, offen),
      dokumente[] (dokument_id, dateiname, typ, aufgabe_id, material_id, erfasst_am),
      summen      (gesamt, arbeit, anfahrt, material, stunden, mit_rechnung,
                   ohne_rechnung, bezahlt, offen),
      je_person[] (person, stunden, anfahrt, kosten, offen).
    """
    pd = _get(f"/api/projects/{projekt_id}")
    if not _ok_antwort(pd):
        return pd
    sites = pd.get("sites", [])
    gewerke: dict = {}
    aufgaben, stunden, material = [], [], []
    dokumente: dict = {}
    for s in sites:
        sd = _get(f"/api/sites/{s['id']}")
        tl = _get(f"/api/sites/{s['id']}/tasks")
        dl = _get(f"/api/documents?site_id={s['id']}")
        for res in (sd, tl, dl):
            if not _ok_antwort(res):
                return res
        gewerke.update({t["id"]: t["name"] for t in sd.get("trades", [])})
        for d in dl.get("documents", []):
            dokumente[d["id"]] = d
        for t in tl.get("tasks", []):
            det = _get(f"/api/tasks/{t['id']}")
            if not _ok_antwort(det):
                return det
            aufgaben.append({"aufgabe_id": t["id"], "titel": t["title"], "status": t["status"],
                             "gewerk": gewerke.get(t["trade_id"]), "baustelle_id": t["site_id"]})
            for e in det.get("time_entries", []):
                anfahrt = e.get("travel_cost") or 0
                stunden.append({
                    "zeiteintrag_id": e["id"], "aufgabe_id": t["id"],
                    "datum": e["work_date"] or (e["created_at"] or "")[:10],
                    "ohne_arbeitsdatum": not e["work_date"],
                    "person": e["person"], "stunden": e["hours"], "stundensatz": e["hourly_rate"],
                    "anfahrt": anfahrt, "betrag": _r2(e["hours"] * e["hourly_rate"] + anfahrt),
                    "mit_rechnung": bool(e["billed"]), "bezahlt": bool(e["paid"]),
                    "teilweise_gezahlt": _r2(e.get("paid_amount") or 0) if not e["paid"] else 0,
                })
            for m in det.get("materials", []):
                material.append({
                    "material_id": m["id"], "aufgabe_id": t["id"],
                    "erfasst_am": (m["created_at"] or "")[:10], "bezeichnung": m["name"],
                    "menge": m["qty"], "einheit": m["unit"], "einzelpreis": m["unit_price"],
                    "betrag": _r2(m["qty"] * m["unit_price"]),
                    "mit_rechnung": bool(m["billed"]), "bezahlt": bool(m["paid"]),
                    "teilweise_gezahlt": _r2(m.get("paid_amount") or 0) if not m["paid"] else 0,
                })
            for d in det.get("documents", []):
                dokumente[d["id"]] = d
    stunden.sort(key=lambda e: (e["datum"], e["zeiteintrag_id"]), reverse=True)
    material.sort(key=lambda m: (m["erfasst_am"], m["material_id"]), reverse=True)

    alle = stunden + material
    for x in alle:
        x["gezahlt"] = x["betrag"] if x["bezahlt"] else x["teilweise_gezahlt"]
        x["offen"] = _r2(x["betrag"] - x["gezahlt"])
    summen = {
        "gesamt": _r2(sum(x["betrag"] for x in alle)),
        "arbeit": _r2(sum(e["betrag"] - e["anfahrt"] for e in stunden)),
        "anfahrt": _r2(sum(e["anfahrt"] for e in stunden)),
        "material": _r2(sum(m["betrag"] for m in material)),
        "stunden": _r2(sum(e["stunden"] for e in stunden)),
        "mit_rechnung": _r2(sum(x["betrag"] for x in alle if x["mit_rechnung"])),
        "ohne_rechnung": _r2(sum(x["betrag"] for x in alle if not x["mit_rechnung"])),
        "bezahlt": _r2(sum(x["gezahlt"] for x in alle)),
        "offen": _r2(sum(x["offen"] for x in alle)),
    }
    je_person: dict = {}
    for e in stunden:
        p = je_person.setdefault(e["person"], {"person": e["person"], "stunden": 0,
                                               "anfahrt": 0, "kosten": 0, "offen": 0})
        p["stunden"] += e["stunden"]
        p["anfahrt"] += e["anfahrt"]
        p["kosten"] += e["betrag"]
        p["offen"] += e["offen"]
    for p in je_person.values():
        for k in ("stunden", "anfahrt", "kosten", "offen"):
            p[k] = _r2(p[k])

    return {
        "status": "ok",
        "projekt": {"projekt_id": pd["project"]["id"], "name": pd["project"]["name"],
                    "notiz": pd["project"]["note"]},
        "bereiche": [{"baustelle_id": s["id"], "name": s["name"], "adresse": s["address"],
                      "status": s["status"]} for s in sites],
        "aufgaben": aufgaben,
        "stunden": stunden,
        "material": material,
        "dokumente": [{"dokument_id": d["id"], "dateiname": d["filename"], "typ": d["doc_type"],
                       "aufgabe_id": d["task_id"], "material_id": d["material_id"],
                       "erfasst_am": (d["created_at"] or "")[:10],
                       "in_paperless": bool(d["paperless_id"])}
                      for d in sorted(dokumente.values(), key=lambda d: d["id"], reverse=True)],
        "summen": summen,
        "je_person": sorted(je_person.values(), key=lambda p: -p["kosten"]),
    }


def person_bezahlt_markieren(projekt_id: int, person: str, bezahlt: bool = True,
                             bis_datum: str = "") -> dict:
    """
    Setzt den Zahlungsstatus ALLER Stunden-Einträge (inkl. Anfahrt) einer Person
    in einem Projekt auf einmal — z. B. „Robert ist bis Freitag ausgezahlt"
    → person_bezahlt_markieren(projekt_id, "Robert", bis_datum="2026-11-06").
    Ändert nur Einträge mit anderem Status. Einzelne Einträge stattdessen mit
    zeiteintrag_bearbeiten(bezahlt=…), Material mit material_bearbeiten(bezahlt=…).

    Parameter:
        projekt_id — ID des Projekts.
        person     — Name der Person, z. B. „Robert".
        bezahlt    — True = als bezahlt markieren (Standard), False = wieder auf offen.
        bis_datum  — Optional: nur Arbeitstage bis einschließlich dieses Datums
                     (YYYY-MM-DD, TT.MM.JJJJ, heute, gestern). Einträge ohne
                     Arbeitsdatum zählen mit ihrem Erfassungsdatum.

    Rückgabe: {"status": "ok", "geaendert": [...], "anzahl", "summe", "offen_danach"}
    """
    ziel, err = _ja_nein(bezahlt, "bezahlt")
    if err:
        return err
    grenze, err = _datum(bis_datum)
    if err:
        return err
    ueb = projekt_uebersicht(projekt_id)
    if not _ok_antwort(ueb):
        return ueb
    eigene = [e for e in ueb["stunden"] if e["person"].lower() == person.strip().lower()]
    if not eigene:
        namen = ", ".join(sorted({e["person"] for e in ueb["stunden"]})) or "keine"
        return _fehler(f"Keine Stunden von '{person}' in Projekt {projekt_id}. "
                       f"Personen mit Stunden in diesem Projekt: {namen}.")
    geaendert = []
    for e in eigene:
        if e["bezahlt"] == bool(ziel) or (grenze and e["datum"] > grenze):
            continue
        res = _put(f"/api/timeentries/{e['zeiteintrag_id']}", {"paid": ziel})
        if not _ok_antwort(res):
            return {**res, "bereits_geaendert": geaendert}
        e["bezahlt"] = bool(ziel)
        geaendert.append({k: e[k] for k in ("zeiteintrag_id", "datum", "stunden", "anfahrt", "betrag")})
    return {
        "status": "ok",
        "geaendert": geaendert,
        "anzahl": len(geaendert),
        "summe": _r2(sum(g["betrag"] for g in geaendert)),
        "offen_danach": _r2(sum(e["betrag"] for e in eigene if not e["bezahlt"])),
    }


def person_auszahlung(projekt_id: int, person: str, betrag: float) -> dict:
    """
    Verbucht eine (Teil-)Auszahlung an eine Person in einem Projekt, z. B.
    „Robert 200 € gegeben“ → person_auszahlung(projekt_id, "Robert", 200).
    Der Betrag wird auf die offenen Stunden-Einträge (inkl. Anfahrt) der Person
    verteilt — älteste Arbeitstage zuerst. Voll gedeckte Tage werden „bezahlt“,
    der letzte ggf. nur teilweise (Restbetrag bleibt offen). Mehrfach aufrufbar;
    Beträge addieren sich. Mehr als offen wird abgelehnt.

    Für EINEN bestimmten Tag stattdessen zeiteintrag_bearbeiten(bereits_gezahlt=…).

    Parameter:
        projekt_id — ID des Projekts.
        person     — Name, z. B. „Robert“.
        betrag     — Ausgezahlter Betrag in Euro (> 0).

    Rückgabe: {"status": "ok", "verteilt": [{id, work_date, betrag, anteil, bezahlt,
               gezahlt_gesamt}], "rest_offen": <Euro offen bei dieser Person>}
    """
    return _post(f"/api/projects/{projekt_id}/payout", {"person": person, "amount": betrag})


def dashboard_summary(projekt_id: int = None, baustelle_id: int = None) -> dict:
    """
    Liefert den kompletten Dashboard-Datensatz: KPIs (Kosten mit/ohne Rechnung,
    offene Aufgaben, aktive Baustellen), Aufschlüsselung je Gewerk (by_trade),
    je Baustelle (by_site), je Person (by_person) und die letzten 10 Erfassungen.

    :param projekt_id: Optional auf ein Projekt einschränken.
    :param baustelle_id: Optional auf eine Baustelle einschränken.
    """
    if baustelle_id is not None:
        return _get(f"/api/dashboard?site={baustelle_id}")
    if projekt_id is not None:
        return _get(f"/api/dashboard?project={projekt_id}")
    return _get("/api/dashboard")


# ---------------------------------------------------------------------------
# 8. Dokumente & Fotos
# ---------------------------------------------------------------------------

def dokument_hochladen(
    datei_pfad: str,
    aufgabe_id: int = None,
    material_id: int = None,
    doc_type: str = "beleg",
) -> dict:
    """
    Lädt ein Foto, einen Kassenbon oder eine Rechnung (JPG, PNG, WebP oder PDF,
    max. 20 MB) ins Projekt hoch. Sobald Sven ein Foto/Beleg schickt, direkt
    hochladen — auch ohne Buchung.

    Zuordnung (IDs aus projekt_uebersicht):
      - Baustellenfoto / allgemeines Dokument → aufgabe_id
      - Kassenbon/Rechnung zu einer Ausgabe   → material_id (erscheint in der
        Weboberfläche beim Material; gern zusätzlich aufgabe_id mitgeben)

    Parameter:
        datei_pfad  — Pflichtfeld: Pfad zur Datei auf dem Pi, z. B. ein Chat-Anhang unter
                      /home/sven/.hermes/webui/attachments/<id>/… oder ~/.hermes/image_cache/….
        aufgabe_id  — Aufgabe, zu der die Datei gehört.
        material_id — Materialposition, zu der der Beleg gehört.
        doc_type    — „foto", „beleg" (Kassenbon/Quittung, Standard) oder „rechnung".

    Rückgabe: {"status": "ok", "id": <dokument_id>, "filename": "...", "stored_name": "..."}
    """
    pfad = os.path.expanduser(str(datei_pfad or "").strip())
    if not os.path.isfile(pfad):
        return _fehler(f"Datei nicht gefunden: {datei_pfad}")
    if aufgabe_id is None and material_id is None:
        return _fehler("aufgabe_id oder material_id angeben (IDs aus projekt_uebersicht).")
    endung = os.path.splitext(pfad)[1].lower()
    if endung not in _ERLAUBTE_ENDUNGEN:
        tipp = " iPhone-Fotos (HEIC) bitte als JPG schicken lassen." if endung in (".heic", ".heif") else ""
        return _fehler(f"Dateityp {endung or '(ohne Endung)'} nicht erlaubt. Erlaubt: "
                       f"{', '.join(sorted(_ERLAUBTE_ENDUNGEN))}.{tipp}")
    if os.path.getsize(pfad) > _MAX_UPLOAD:
        return _fehler(f"Datei ist größer als 20 MB ({os.path.getsize(pfad) // 1024 // 1024} MB).")
    typ = _DOC_TYPEN.get(str(doc_type or "beleg").strip().lower())
    if not typ:
        return _fehler(f"doc_type {doc_type!r} unbekannt. Erlaubt: foto, beleg, rechnung.")

    fields: dict = {"doc_type": typ}
    if aufgabe_id is not None:
        fields["task_id"] = str(aufgabe_id)
    if material_id is not None:
        fields["material_id"] = str(material_id)

    try:
        boundary, body = _multipart(fields, pfad)
    except OSError as exc:
        return _fehler(f"Datei konnte nicht gelesen werden: {exc}")
    try:
        return _send("POST", "/api/documents", body,
                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    except Exception as exc:  # noqa: BLE001
        return _fehler(f"Unerwarteter Fehler: {exc}")


def dokumente_liste(
    projekt_id: int = None,
    aufgabe_id: int = None,
    material_id: int = None,
    baustelle_id: int = None,
    doc_type: str = None,
) -> dict:
    """
    Gibt Dokumente zurück, filterbar nach Projekt, Aufgabe, Materialposition,
    Baustelle oder Typ. Alle Parameter sind optional und kombinierbar.

    Parameter:
        projekt_id   — Nur Dokumente dieses Projekts (Normalfall).
        aufgabe_id   — Nur Dokumente dieser Aufgabe.
        material_id  — Nur Dokumente dieser Materialposition.
        baustelle_id — Nur Dokumente dieser Baustelle.
        doc_type     — Nur „rechnung", „beleg" oder „foto".

    Rückgabe: {"status": "ok", "documents": [...]}
    """
    if projekt_id is not None and baustelle_id is None:
        sites = _get(f"/api/sites?project={projekt_id}")
        if not _ok_antwort(sites):
            return sites
        alle: list[dict] = []
        for s in sites.get("sites", []):
            res = dokumente_liste(aufgabe_id=aufgabe_id, material_id=material_id,
                                  baustelle_id=s["id"], doc_type=doc_type)
            if not _ok_antwort(res):
                return res
            alle.extend(res.get("documents", []))
        return {"status": "ok", "documents": alle}

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

# Personen
register_tool("person_anlegen",       person_anlegen)
register_tool("personen_liste",       personen_liste)
register_tool("person_bearbeiten",    person_bearbeiten)
register_tool("person_loeschen",      person_loeschen)

# Material
register_tool("material_anlegen",     material_anlegen)
register_tool("materialien_liste",    materialien_liste)
register_tool("material_bearbeiten",  material_bearbeiten)
register_tool("material_loeschen",    material_loeschen)

# Kostenübersicht
register_tool("baustelle_summary",    baustelle_summary)
register_tool("projekt_summary",      projekt_summary)
register_tool("gesamt_summary",       gesamt_summary)
register_tool("dashboard_summary",    dashboard_summary)
register_tool("projekt_uebersicht",   projekt_uebersicht)
register_tool("person_bezahlt_markieren", person_bezahlt_markieren)
register_tool("person_auszahlung",    person_auszahlung)

# Dokumente & Fotos
register_tool("dokument_hochladen",   dokument_hochladen)
register_tool("dokumente_liste",      dokumente_liste)
register_tool("dokument_loeschen",    dokument_loeschen)
register_tool("dokument_zu_paperless", dokument_zu_paperless)

# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def guthaben_einzahlen(projekt_id: int, person: str, betrag: float, notiz: str = "") -> dict:
    """
    Trägt ein Guthaben (Vorauszahlung/Vorstreckung) für eine Person ein, z. B.
    „Koffee hat 500 € vorgestreckt“ → guthaben_einzahlen(projekt_id, "Koffee", 500).
    Das Guthaben wird sofort auf offene Stunden der Person verrechnet (älteste zuerst)
    und danach bei jeder neuen Stunde automatisch zuerst verbraucht. Die verrechneten
    Einträge gelten als „bezahlt“.

    Parameter:
        projekt_id — ID des Projekts.
        person     — Name, z. B. „Koffee“.
        betrag     — Einzahlung in Euro (> 0).
        notiz      — Optional, z. B. „Barzahlung 05.11.“.

    Rückgabe: {"status": "ok", "verrechnet": <Euro sofort verrechnet>, "saldo": <Restguthaben>}
    """
    payload = {"person": person, "amount": betrag}
    if notiz:
        payload["notiz"] = notiz
    return _post(f"/api/projects/{projekt_id}/guthaben", payload)


def guthaben_stand(projekt_id: int, person: str) -> dict:
    """
    Zeigt das aktuelle Guthaben einer Person im Projekt (Restbetrag, der bei neuen
    Stunden automatisch verbraucht wird) und alle Einzahlungen.
    """
    return _get(f"/api/projects/{projekt_id}/guthaben?person={person}")


register_tool("guthaben_einzahlen", guthaben_einzahlen)
register_tool("guthaben_stand", guthaben_stand)

if __name__ == "__main__":
    run_server()
