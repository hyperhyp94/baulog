"""
server.py — Flask-REST-API für baulog.

Importiert alle CRUD-Helfer und Aggregationsfunktionen aus db.py.
Setzt alle REST-Endpunkte gemäß PLAN.md Abschnitt 4 um.

Starten: python server.py (Port aus TOOL_PORT, Standard 5000)
"""

import json
import logging
import math
import mimetypes
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

import db
import receipts

# ---------------------------------------------------------------------------
# App-Konfiguration
# ---------------------------------------------------------------------------

# static_folder=None ist Absicht: mit static_folder="." liefert Flask unter der
# Route "/./<pfad>" jede Datei des Projektverzeichnisses aus — inklusive
# data/baulog.db und config.local.json (Paperless-Token). index.html wird
# stattdessen über die eigene "/"-Route ausgeliefert.
app = Flask(__name__, static_folder=None)

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "data", "uploads")

# Erlaubte Dateiendungen für den Dokumenten-Upload
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

# Maximale Upload-Größe: 20 MB
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# Pfad zur optionalen lokalen Konfigurationsdatei (nicht eingecheckt)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.local.json")

# Gültige Dokumenttypen
VALID_DOC_TYPES = {"rechnung", "beleg", "foto"}

# Gültige Status-Werte für Baustellen und Aufgaben
VALID_SITE_STATUS = {"aktiv", "abgeschlossen"}
VALID_TASK_STATUS = {"offen", "in Arbeit", "fertig"}

# Obergrenze für Freitextfelder (Namen, Notizen, Beschreibungen)
MAX_TEXT_LENGTH = 5000


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _load_paperless_config() -> tuple[str | None, str | None]:
    """
    Lädt Paperless-URL und Token aus config.local.json oder Umgebungsvariablen.

    Vorrang: config.local.json überschreibt Umgebungsvariablen.
    Gibt (url, token) zurück; beide None wenn nicht konfiguriert.
    """
    url = os.environ.get("PAPERLESS_URL")
    token = os.environ.get("PAPERLESS_TOKEN")
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            url = cfg.get("paperless_url", url)
            token = cfg.get("paperless_token", token)
        except (json.JSONDecodeError, OSError):
            pass
    return url or None, token or None


def _paperless_enabled() -> bool:
    """Gibt True zurück, wenn Paperless vollständig konfiguriert ist (URL + Token)."""
    url, token = _load_paperless_config()
    return bool(url and token)


def _err(msg: str, code: int = 400):
    """Erstellt eine JSON-Fehlerantwort mit HTTP-Statuscode."""
    return jsonify({"error": msg}), code


def _ok(**kwargs):
    """Erstellt eine JSON-Erfolgsantwort mit status='ok' und optionalen Feldern."""
    return jsonify({"status": "ok", **kwargs})


def _row(row) -> dict:
    """Konvertiert ein sqlite3.Row-Objekt in ein einfaches Dict."""
    return dict(row) if row else {}


def _remove_files(paths: list[str]) -> None:
    """Löscht Upload-Dateien von Disk; Fehler dabei sind nicht fatal."""
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _rows(rows) -> list[dict]:
    """Konvertiert eine Liste von sqlite3.Row-Objekten in eine Liste von Dicts."""
    return [dict(r) for r in rows]


def _allowed_file(filename: str) -> bool:
    """Gibt True zurück, wenn die Dateiendung in der Whitelist steht."""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _int_arg(name: str, value: str) -> tuple[int | None, object]:
    """
    Konvertiert einen Query-/Form-Parameter in int.

    Gibt (int_wert, None) bei Erfolg oder (None, Fehlerantwort) bei Fehler zurück.
    """
    try:
        return int(value), None
    except (ValueError, TypeError):
        return None, _err(f"Ungültiger Wert für '{name}'.")


def _num_field(data: dict, key: str, *, default=None, minimum=None,
               required=False, greater_than=None):
    """
    Liest ein Zahlenfeld aus einem JSON-Body mit Validierung.

    Lehnt Booleans (True wäre sonst 1.0) und nicht-endliche Werte ab:
    Infinity/NaN würden bis in die Aggregationen durchschlagen und dort
    das gesamte Dashboard-JSON ungültig machen.

    Gibt (wert, None) bei Erfolg oder (None, fehlerantwort) bei Fehler zurück.
    """
    if key not in data or data[key] is None:
        if required:
            return None, _err(f"Feld '{key}' ist Pflichtfeld (Zahl).")
        return default, None
    if isinstance(data[key], bool):
        return None, _err(f"'{key}' muss eine Zahl sein.")
    try:
        val = float(data[key])
    except (ValueError, TypeError):
        return None, _err(f"'{key}' muss eine Zahl sein.")
    if not math.isfinite(val):
        return None, _err(f"'{key}' muss eine endliche Zahl sein.")
    if minimum is not None and val < minimum:
        return None, _err(f"'{key}' muss >= {minimum} sein.")
    if greater_than is not None and val <= greater_than:
        return None, _err(f"'{key}' muss > {greater_than} sein.")
    return val, None


def _str_field(data: dict, key: str, *, required=False, default=None,
               max_length=MAX_TEXT_LENGTH):
    """
    Liest ein Textfeld aus einem JSON-Body mit Typ- und Längenprüfung.

    Ohne diese Prüfung führen Nicht-Strings ({"name": 123}) zu einem
    Absturz in .strip() bzw. in SQLite statt zu einer klaren 400-Antwort.

    Gibt (wert, None) bei Erfolg oder (None, fehlerantwort) bei Fehler zurück.
    Ein leerer Wert wird zu None normalisiert, sofern nicht required.
    """
    if key not in data or data[key] is None:
        if required:
            return None, _err(f"Feld '{key}' ist Pflichtfeld.")
        return default, None
    val = data[key]
    if not isinstance(val, str):
        return None, _err(f"'{key}' muss Text sein.")
    val = val.strip()
    if not val:
        if required:
            return None, _err(f"'{key}' darf nicht leer sein.")
        return None, None
    if len(val) > max_length:
        return None, _err(f"'{key}' ist zu lang (max. {max_length} Zeichen).")
    return val, None


def _flag_field(data: dict, key: str, *, default=1):
    """
    Liest ein 0/1-Flag (billed) und gibt es normalisiert zurück.

    Akzeptiert 0/1, "0"/"1" und true/false (bequem für den MCP-Agenten).
    Wichtig: der normalisierte Wert muss auch gespeichert werden — ein
    ungeprüft durchgereichtes 1.5 ließ die Position aus allen Summen
    herausfallen (weder billed=1 noch billed=0).

    Gibt (0|1, None) bei Erfolg oder (None, fehlerantwort) bei Fehler zurück.
    """
    if key not in data:
        return default, None
    val = data[key]
    if isinstance(val, bool):
        return (1 if val else 0), None
    if isinstance(val, int) and val in (0, 1):
        return val, None
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("0", "false", "nein"):
            return 0, None
        if s in ("1", "true", "ja"):
            return 1, None
    return None, _err(f"'{key}' muss 0 oder 1 sein.")


def _teilzahlung_normalisieren(fields: dict, betrag: float):
    """
    Teilzahlungen: 'paid_amount' (Euro) gilt nur, solange 'paid' = 0.

    - paid wird gesetzt (ohne paid_amount)  → paid_amount = 0 (voll bezahlt bzw. wieder offen)
    - paid_amount >= betrag                 → paid = 1, paid_amount = 0 (vollständig bezahlt)
    - paid_amount <= 0                      → paid_amount = 0
    Gibt eine Fehlerantwort oder None zurück; ändert 'fields' direkt.
    """
    if "paid_amount" in fields:
        val, err = _num_field(fields, "paid_amount", default=0.0, minimum=0)
        if err:
            return err
        val = round(val or 0.0, 2)
        if val > 0 and fields.get("paid") == 1:
            return _err("Teilzahlung und 'bezahlt' gleichzeitig ergibt keinen Sinn.")
        if val > 0 and betrag > 0 and val >= round(betrag, 2):
            fields["paid"], val = 1, 0.0
        elif val > 0:
            fields["paid"] = 0
        fields["paid_amount"] = val
    elif "paid" in fields:
        fields["paid_amount"] = 0.0
    return None


def _date_field(data: dict, key: str):
    """
    Liest ein Datumsfeld (YYYY-MM-DD) mit Formatprüfung.

    Leerstring wird zu None normalisiert, damit POST und PUT dasselbe
    Ergebnis liefern. Gibt (wert|None, None) oder (None, fehlerantwort) zurück.
    """
    if key not in data or data[key] is None:
        return None, None
    val = data[key]
    if not isinstance(val, str):
        return None, _err(f"'{key}' muss ein Datum als Text sein (YYYY-MM-DD).")
    val = val.strip()
    if not val:
        return None, None
    try:
        date.fromisoformat(val)
    except ValueError:
        return None, _err(f"'{key}' muss das Format YYYY-MM-DD haben.")
    return val, None


def _trade_or_error(trade_id, site_id: int):
    """
    Prüft eine trade_id auf Typ, Existenz und Zugehörigkeit zur Baustelle.

    Gibt (int|None, None) bei Erfolg oder (None, fehlerantwort) zurück.
    None als trade_id ist erlaubt (Aufgabe ohne Gewerk).
    """
    if trade_id is None:
        return None, None
    try:
        trade_id = int(trade_id)
    except (ValueError, TypeError):
        return None, _err("'trade_id' muss eine Ganzzahl sein.")
    trade = db.get_trade(trade_id)
    if not trade:
        return None, _err("Gewerk nicht gefunden.", 404)
    if trade["site_id"] != site_id:
        return None, _err("Gewerk gehört zu einer anderen Baustelle.")
    return trade_id, None


# ---------------------------------------------------------------------------
# Änderungsprotokoll: Quelle jeder Änderung festhalten (Trigger schreiben die Zeilen)
# ---------------------------------------------------------------------------

@app.before_request
def _changelog_start():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            g.changelog_start = db.changelog_max_id()
        except sqlite3.Error:
            g.changelog_start = None


@app.after_request
def _changelog_tag(resp):
    start = g.get("changelog_start")
    if start is not None:
        quelle = request.headers.get("X-Baulog-Quelle", "")[:40]
        if not quelle:
            ua = request.headers.get("User-Agent", "")
            quelle = "Hermes/MCP" if ua.startswith("Python-urllib") else "Web-Oberfläche"
        try:
            db.changelog_tag(start, quelle)
        except sqlite3.Error:
            app.logger.warning("Änderungsprotokoll: Quelle konnte nicht gesetzt werden")
    return resp


@app.route("/api/changelog", methods=["GET"])
def api_changelog():
    """Letzte Änderungen. Query: limit (max 500), tbl, row_id, before_id (zum Weiterblättern)."""
    args = {}
    for key in ("limit", "row_id", "before_id"):
        if request.args.get(key):
            val, err = _int_arg(key, request.args[key])
            if err:
                return err
            args[key] = val
    args["limit"] = max(1, min(args.get("limit", 200), 500))
    tbl = request.args.get("tbl")
    if tbl and tbl not in db.CHANGELOG_TABLES:
        return _err("Unbekannte Tabelle.")
    rows = []
    for r in db.list_changelog(tbl=tbl, **args):
        d = dict(r)
        d["old"] = json.loads(d.pop("old_json")) if r["old_json"] else None
        d["new"] = json.loads(d.pop("new_json")) if r["new_json"] else None
        rows.append(d)
    return _ok(changes=rows)


@app.route("/api/changelog/<int:cid>/undo", methods=["POST"])
def api_changelog_undo(cid: int):
    """Macht eine einzelne protokollierte Änderung rückgängig."""
    try:
        new_id = db.undo_change(cid)
    except db.UndoError as exc:
        return _err(str(exc), 409)
    return _ok(undo_id=new_id)


# ---------------------------------------------------------------------------
# Fehlerbehandlung: immer JSON, nie HTML
# ---------------------------------------------------------------------------
# Ohne diese Handler liefert Flask bei 404/405/413 und bei unerwarteten
# Ausnahmen eine HTML-Seite. baulog_mcp.py kann die nicht parsen und meldet
# dem Agenten nur "HTTP 500" ohne Ursache.

@app.errorhandler(HTTPException)
def _handle_http_exception(exc: HTTPException):
    """Wandelt Flasks Standard-HTML-Fehlerseiten in JSON um."""
    if exc.code == 413:
        return _err("Datei ist zu groß (max. 20 MB).", 413)
    return _err(exc.description or exc.name, exc.code or 500)


@app.errorhandler(sqlite3.IntegrityError)
def _handle_integrity_error(exc: sqlite3.IntegrityError):
    """Verletzte Datenbank-Constraints sind Eingabefehler, keine Serverfehler."""
    app.logger.warning("Constraint verletzt bei %s %s: %s",
                       request.method, request.path, exc)
    return _err(f"Datenbank-Regel verletzt: {exc}", 409)


@app.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    """Letztes Netz: Stacktrace ins Log, generische JSON-Antwort nach außen."""
    app.logger.exception("Unbehandelter Fehler bei %s %s", request.method, request.path)
    return _err("Interner Serverfehler.", 500)


# ---------------------------------------------------------------------------
# Statische Route
# ---------------------------------------------------------------------------

@app.route("/icons/<path:fname>")
def app_icons(fname):
    mime = "application/manifest+json" if fname.endswith(".webmanifest") else None
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons"), fname, mimetype=mime)


@app.route("/")
def index():
    """Liefert index.html aus dem Projektverzeichnis."""
    resp = send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")
    # iPhone-Home-Bildschirm-App hält die Seite sonst im Cache und zeigt alte Versionen.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------
# Projekte (/api/projects)
# ---------------------------------------------------------------------------

@app.route("/api/projects", methods=["GET"])
def api_projects_list():
    """Gibt alle Projekte zurück."""
    return _ok(projects=_rows(db.list_projects()))


@app.route("/api/projects", methods=["POST"])
def api_projects_create():
    """
    Legt ein neues Projekt an.

    Body (JSON): { "name": "...", "note": "..." }
    Pflichtfeld: name.
    """
    data = request.get_json(silent=True) or {}
    name, err = _str_field(data, "name", required=True)
    if err:
        return err
    note, err = _str_field(data, "note")
    if err:
        return err
    pid = db.create_project(name=name, note=note)
    return _ok(id=pid), 201


@app.route("/api/projects/<int:pid>", methods=["GET"])
def api_project_get(pid: int):
    """Gibt ein Projekt inkl. zugehöriger Baustellen zurück."""
    project = db.get_project(pid)
    if not project:
        return _err("Projekt nicht gefunden.", 404)
    sites = _rows(db.list_sites({"project_id": pid}))
    return _ok(project=_row(project), sites=sites)


@app.route("/api/projects/<int:pid>", methods=["PUT"])
def api_project_update(pid: int):
    """
    Aktualisiert Name und/oder Notiz eines Projekts.

    Body (JSON): beliebige Kombination aus { "name", "note" }.
    """
    if not db.get_project(pid):
        return _err("Projekt nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("name", "note") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "name" in fields:
        fields["name"], err = _str_field(fields, "name", required=True)
        if err:
            return err
    if "note" in fields:
        fields["note"], err = _str_field(fields, "note")
        if err:
            return err
    db.update_project(pid, fields)
    return _ok()


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
def api_project_delete(pid: int):
    """Löscht ein Projekt (Kaskade) und entfernt zugehörige Upload-Dateien."""
    if not db.get_project(pid):
        return _err("Projekt nicht gefunden.", 404)
    paths = db.document_paths_for_scope("project", pid)
    db.delete_project(pid)
    _remove_files(paths)
    return _ok()


# ---------------------------------------------------------------------------
# Baustellen (/api/sites)
# ---------------------------------------------------------------------------

@app.route("/api/sites", methods=["GET"])
def api_sites_list():
    """
    Gibt Baustellen zurück.

    Query-Parameter: ?project=<pid> filtert nach Projekt.
    """
    project_arg = request.args.get("project")
    filters: dict = {}
    if project_arg:
        pid, err = _int_arg("project", project_arg)
        if err:
            return err
        filters["project_id"] = pid
    return _ok(sites=_rows(db.list_sites(filters or None)))


@app.route("/api/sites", methods=["POST"])
def api_sites_create():
    """
    Legt eine neue Baustelle an.

    Body (JSON): { "name", "project_id", "address"?, "status"?, "note"? }
    Pflichtfelder: name, project_id.
    """
    data = request.get_json(silent=True) or {}
    name, err = _str_field(data, "name", required=True)
    if err:
        return err
    project_id = data.get("project_id")
    if project_id is None:
        return _err("Feld 'project_id' ist Pflichtfeld.")
    try:
        project_id = int(project_id)
    except (ValueError, TypeError):
        return _err("'project_id' muss eine Ganzzahl sein.")
    if not db.get_project(project_id):
        return _err("Projekt nicht gefunden.", 404)
    status = data.get("status") or "aktiv"
    if status not in VALID_SITE_STATUS:
        return _err(f"'status' muss eines von {', '.join(sorted(VALID_SITE_STATUS))} sein.")
    address, err = _str_field(data, "address")
    if err:
        return err
    note, err = _str_field(data, "note")
    if err:
        return err
    sid = db.create_site(
        project_id=project_id,
        name=name,
        address=address,
        status=status,
        note=note,
    )
    return _ok(id=sid), 201


@app.route("/api/sites/<int:sid>", methods=["GET"])
def api_site_get(sid: int):
    """Gibt eine Baustelle inkl. ihrer Gewerke zurück."""
    site = db.get_site(sid)
    if not site:
        return _err("Baustelle nicht gefunden.", 404)
    trades = _rows(db.list_trades_with_stats(sid))
    return _ok(site=_row(site), trades=trades)


@app.route("/api/sites/<int:sid>", methods=["PUT"])
def api_site_update(sid: int):
    """
    Aktualisiert Felder einer Baustelle.

    Body (JSON): beliebige Kombination aus { "name", "address", "status", "note", "project_id" }.
    """
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("name", "address", "status", "note", "project_id") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "name" in fields:
        fields["name"], err = _str_field(fields, "name", required=True)
        if err:
            return err
    for optional_text in ("address", "note"):
        if optional_text in fields:
            fields[optional_text], err = _str_field(fields, optional_text)
            if err:
                return err
    if "status" in fields and fields["status"] not in VALID_SITE_STATUS:
        return _err(f"'status' muss eines von {', '.join(sorted(VALID_SITE_STATUS))} sein.")
    if fields.get("project_id") is not None:
        try:
            fields["project_id"] = int(fields["project_id"])
        except (ValueError, TypeError):
            return _err("'project_id' muss eine Ganzzahl sein.")
        if not db.get_project(fields["project_id"]):
            return _err("Projekt nicht gefunden.", 404)
    db.update_site(sid, fields)
    return _ok()


@app.route("/api/sites/<int:sid>", methods=["DELETE"])
def api_site_delete(sid: int):
    """Löscht eine Baustelle (Kaskade) und entfernt zugehörige Upload-Dateien."""
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    paths = db.document_paths_for_scope("site", sid)
    db.delete_site(sid)
    _remove_files(paths)
    return _ok()


# ---------------------------------------------------------------------------
# Gewerke (/api/sites/<sid>/trades, /api/trades/<tid>)
# ---------------------------------------------------------------------------

@app.route("/api/sites/<int:sid>/trades", methods=["GET"])
def api_trades_list(sid: int):
    """Gibt alle Gewerke einer Baustelle zurück."""
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    return _ok(trades=_rows(db.list_trades({"site_id": sid})))


@app.route("/api/sites/<int:sid>/trades", methods=["POST"])
def api_trades_create(sid: int):
    """
    Legt ein neues Gewerk für eine Baustelle an.

    Body (JSON): { "name": "Maurer" }
    Pflichtfeld: name.
    """
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _err("Feld 'name' ist Pflichtfeld.")
    tid = db.create_trade(site_id=sid, name=name)
    return _ok(id=tid), 201


@app.route("/api/trades/<int:tid>", methods=["PUT"])
def api_trade_update(tid: int):
    """
    Benennt ein Gewerk um.

    Body (JSON): { "name": "Neuer Name" }
    """
    if not db.get_trade(tid):
        return _err("Gewerk nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("name",) if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "name" in fields and not (fields["name"] or "").strip():
        return _err("'name' darf nicht leer sein.")
    db.update_trade(tid, fields)
    return _ok()


@app.route("/api/trades/<int:tid>", methods=["DELETE"])
def api_trade_delete(tid: int):
    """Löscht ein Gewerk. Aufgaben mit diesem Gewerk behalten trade_id=NULL."""
    if not db.delete_trade(tid):
        return _err("Gewerk nicht gefunden.", 404)
    return _ok()


# ---------------------------------------------------------------------------
# Personen (/api/persons)
# ---------------------------------------------------------------------------

@app.route("/api/persons", methods=["GET"])
def api_persons_list():
    """Gibt Personen inkl. Aggregaten zurück. Query: ?active=1 filtert auf aktive."""
    active_only = request.args.get("active") == "1"
    return _ok(persons=_rows(db.list_persons(active_only=active_only)))


@app.route("/api/persons", methods=["POST"])
def api_persons_create():
    """
    Legt eine Person an.

    Body (JSON): { "name": "...", "hourly_rate": 20.0, "note": "..." }
    Pflichtfeld: name (eindeutig). hourly_rate >= 0, Standard 0.
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        return _err("Feld 'name' ist Pflichtfeld.")
    rate, err = _num_field(data, "hourly_rate", default=0.0, minimum=0.0)
    if err:
        return err
    try:
        pid = db.create_person(name=name, hourly_rate=rate, note=data.get("note") or None)
    except sqlite3.IntegrityError:
        return _err(f"Person '{name}' existiert bereits.", 409)
    return _ok(id=pid), 201


@app.route("/api/persons/<int:person_id>", methods=["PUT"])
def api_person_update(person_id: int):
    """
    Aktualisiert Felder einer Person.

    Body (JSON): beliebige Kombination aus { "name", "hourly_rate", "active", "note" }.
    Umbenennen synchronisiert die Namen in bestehenden Zeiteinträgen.
    """
    if not db.get_person(person_id):
        return _err("Person nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("name", "hourly_rate", "active", "note") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "name" in fields:
        name_val = fields["name"]
        name_val = name_val.strip() if isinstance(name_val, str) else ""
        if not name_val:
            return _err("'name' darf nicht leer sein.")
        fields["name"] = name_val
    if "hourly_rate" in fields:
        rate, err = _num_field(fields, "hourly_rate", minimum=0.0, required=True)
        if err:
            return err
        fields["hourly_rate"] = rate
    if "active" in fields:
        try:
            active_val = int(fields["active"])
            if active_val not in (0, 1):
                return _err("'active' muss 0 oder 1 sein.")
            fields["active"] = active_val
        except (ValueError, TypeError):
            return _err("'active' muss 0 oder 1 sein.")
    try:
        db.update_person(person_id, fields)
    except sqlite3.IntegrityError:
        return _err(f"Person '{fields.get('name')}' existiert bereits.", 409)
    return _ok()


@app.route("/api/persons/<int:person_id>", methods=["DELETE"])
def api_person_delete(person_id: int):
    """Löscht eine Person ohne Zeiteinträge; mit Zeiteinträgen → 409 (deaktivieren)."""
    if not db.get_person(person_id):
        return _err("Person nicht gefunden.", 404)
    try:
        db.delete_person(person_id)
    except db.PersonInUseError as exc:
        return _err(str(exc), 409)
    return _ok()


# ---------------------------------------------------------------------------
# Aufgaben (/api/sites/<sid>/tasks, /api/tasks/<taskid>)
# ---------------------------------------------------------------------------

@app.route("/api/sites/<int:sid>/tasks", methods=["GET"])
def api_tasks_list(sid: int):
    """
    Gibt Aufgaben einer Baustelle zurück.

    Query-Parameter: ?trade=<tid> und/oder ?status=<s> als optionale Filter.
    """
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    filters: dict = {"site_id": sid}
    trade_arg = request.args.get("trade")
    if trade_arg:
        tid, err = _int_arg("trade", trade_arg)
        if err:
            return err
        filters["trade_id"] = tid
    status_arg = request.args.get("status")
    if status_arg:
        filters["status"] = status_arg
    return _ok(tasks=_rows(db.list_tasks(filters)))


@app.route("/api/sites/<int:sid>/tasks", methods=["POST"])
def api_tasks_create(sid: int):
    """
    Legt eine neue Aufgabe für eine Baustelle an.

    Body (JSON): { "title", "trade_id"?, "status"?, "description"? }
    Pflichtfeld: title.
    """
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    title, err = _str_field(data, "title", required=True)
    if err:
        return err
    trade_id, err = _trade_or_error(data.get("trade_id"), sid)
    if err:
        return err
    status = data.get("status") or "offen"
    if status not in VALID_TASK_STATUS:
        return _err(f"'status' muss eines von {', '.join(sorted(VALID_TASK_STATUS))} sein.")
    description, err = _str_field(data, "description")
    if err:
        return err
    task_id = db.create_task(
        site_id=sid,
        title=title,
        trade_id=trade_id,
        status=status,
        description=description,
    )
    return _ok(id=task_id), 201


@app.route("/api/tasks/<int:task_id>", methods=["GET"])
def api_task_get(task_id: int):
    """
    Gibt eine Aufgabe inkl. Zeiteinträge, Materialien und Dokumente zurück.

    Enthält außerdem berechnete Kostensummen der Aufgabe.
    """
    task = db.get_task(task_id)
    if not task:
        return _err("Aufgabe nicht gefunden.", 404)
    time_entries = _rows(db.list_time_entries({"task_id": task_id}))
    materials = _rows(db.list_materials({"task_id": task_id}))
    documents = _rows(db.list_documents({"task_id": task_id}))
    # Kosten je Eintrag ergänzen (Zeiteinträge inkl. Anfahrt)
    for e in time_entries:
        e["kosten"] = round(e["hours"] * e["hourly_rate"] + e["travel_cost"], 2)
    for m in materials:
        m["kosten"] = round(m["qty"] * m["unit_price"], 2)
    mat_gesamt = sum(m["kosten"] for m in materials)
    pers_gesamt = sum(e["kosten"] for e in time_entries)
    return _ok(
        task=_row(task),
        time_entries=time_entries,
        materials=materials,
        documents=documents,
        kosten={
            "material": round(mat_gesamt, 2),
            "personal": round(pers_gesamt, 2),
            "gesamt": round(mat_gesamt + pers_gesamt, 2),
        },
    )


@app.route("/api/tasks/<int:task_id>", methods=["PUT"])
def api_task_update(task_id: int):
    """
    Aktualisiert Felder einer Aufgabe.

    Body (JSON): beliebige Kombination aus { "title", "trade_id", "status", "description" }.
    """
    task = db.get_task(task_id)
    if not task:
        return _err("Aufgabe nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("title", "trade_id", "status", "description") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "title" in fields:
        fields["title"], err = _str_field(fields, "title", required=True)
        if err:
            return err
    if "description" in fields:
        fields["description"], err = _str_field(fields, "description")
        if err:
            return err
    if fields.get("trade_id") is not None:
        fields["trade_id"], err = _trade_or_error(fields["trade_id"], task["site_id"])
        if err:
            return err
    if "status" in fields and fields["status"] not in VALID_TASK_STATUS:
        return _err(f"'status' muss eines von {', '.join(sorted(VALID_TASK_STATUS))} sein.")
    db.update_task(task_id, fields)
    return _ok()


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_task_delete(task_id: int):
    """Löscht eine Aufgabe (Kaskade) und entfernt zugehörige Upload-Dateien."""
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    paths = db.document_paths_for_scope("task", task_id)
    db.delete_task(task_id)
    _remove_files(paths)
    return _ok()


# ---------------------------------------------------------------------------
# Zeiterfassung (/api/tasks/<taskid>/timeentries, /api/timeentries/<eid>)
# ---------------------------------------------------------------------------

@app.route("/api/tasks/<int:task_id>/timeentries", methods=["GET"])
def api_timeentries_list(task_id: int):
    """Gibt alle Zeiteinträge einer Aufgabe zurück (mit berechneten Kosten inkl. Anfahrt)."""
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    entries = _rows(db.list_time_entries({"task_id": task_id}))
    for e in entries:
        e["kosten"] = round(e["hours"] * e["hourly_rate"] + e["travel_cost"], 2)
    return _ok(time_entries=entries)


@app.route("/api/tasks/<int:task_id>/timeentries", methods=["POST"])
def api_timeentries_create(task_id: int):
    """
    Legt einen Zeiteintrag (Personalkosten) an.

    Body (JSON): { "person_id"?, "person"?, "hours", "hourly_rate"?, "billed"?, "work_date"?,
                   "travel_cost"? }
    Entweder 'person_id' (Person muss existieren; setzt 'person'-Text und
    Standard-Stundensatz) oder 'person' (Freitext, dann ist 'hourly_rate' Pflicht).
    Pflichtfeld: hours (> 0).
    billed: 1 = mit Rechnung (Standard), 0 = ohne Rechnung.
    travel_cost: Anfahrt in Euro für diesen Arbeitstag (>= 0, Standard 0).
    """
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}

    person_row = None
    person_id = data.get("person_id")
    if person_id is not None:
        try:
            person_id = int(person_id)
        except (ValueError, TypeError):
            return _err("'person_id' muss eine Ganzzahl sein.")
        person_row = db.get_person(person_id)
        if not person_row:
            return _err("Person nicht gefunden.", 404)

    if person_row:
        person = person_row["name"]
    else:
        person, err = _str_field(data, "person")
        if err:
            return err
    if not person:
        return _err("Entweder 'person_id' oder 'person' muss angegeben werden.")

    hours, err = _num_field(data, "hours", required=True, greater_than=0)
    if err:
        return err
    default_rate = person_row["hourly_rate"] if person_row else None
    rate, err = _num_field(data, "hourly_rate", default=default_rate, minimum=0)
    if err:
        return err
    if rate is None:
        return _err("Feld 'hourly_rate' ist Pflichtfeld (Zahl).")
    # Ein aus den Stammdaten geerbter Satz von 0 ist fast immer ein Versehen
    # (Person ohne gepflegten Satz) und würde still 0 € Kosten buchen.
    # Bewusste Eigenleistung bleibt möglich: hourly_rate explizit auf 0 setzen.
    if rate == 0 and "hourly_rate" not in data:
        return _err(
            f"Person '{person}' hat keinen Stundensatz hinterlegt. "
            f"Bitte den Stundensatz der Person pflegen oder 'hourly_rate' "
            f"explizit angeben (0 für unbezahlte Eigenleistung)."
        )
    billed, err = _flag_field(data, "billed")
    if err:
        return err
    paid, err = _flag_field(data, "paid", default=0)
    if err:
        return err
    work_date, err = _date_field(data, "work_date")
    if err:
        return err
    travel_cost, err = _num_field(data, "travel_cost", default=0.0, minimum=0)
    if err:
        return err
    tz = {"paid": paid}
    if "paid_amount" in data:
        tz["paid_amount"] = data["paid_amount"]
    err = _teilzahlung_normalisieren(tz, hours * rate + travel_cost)
    if err:
        return err
    eid = db.create_time_entry(
        task_id=task_id, person=person, person_id=person_id,
        hours=hours, hourly_rate=rate, billed=billed, paid=tz["paid"],
        work_date=work_date, travel_cost=travel_cost,
    )
    if tz.get("paid_amount"):
        db.update_time_entry(eid, {"paid_amount": tz["paid_amount"]})
    return _ok(id=eid), 201


@app.route("/api/timeentries/<int:eid>", methods=["PUT"])
def api_timeentry_update(eid: int):
    """
    Aktualisiert Felder eines Zeiteintrags.

    Body (JSON): beliebige Kombination aus
    { "person", "person_id", "hours", "hourly_rate", "billed", "paid", "work_date",
      "travel_cost" }.
    Gesetzte 'person_id' synchronisiert den 'person'-Text; 'person_id': null löst
    die Verknüpfung (Text bleibt erhalten). 'travel_cost': null setzt die Anfahrt auf 0.
    """
    if not db.get_time_entry(eid):
        return _err("Zeiteintrag nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in
              ("person", "person_id", "hours", "hourly_rate", "billed", "paid", "work_date",
               "travel_cost", "paid_amount")
              if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if fields.get("person_id") is not None:
        try:
            fields["person_id"] = int(fields["person_id"])
        except (ValueError, TypeError):
            return _err("'person_id' muss eine Ganzzahl sein.")
        person_row = db.get_person(fields["person_id"])
        if not person_row:
            return _err("Person nicht gefunden.", 404)
        fields["person"] = person_row["name"]
    elif "person" in fields:
        fields["person"], err = _str_field(fields, "person", required=True)
        if err:
            return err
    if "hours" in fields:
        fields["hours"], err = _num_field(fields, "hours", required=True, greater_than=0)
        if err:
            return err
    if "hourly_rate" in fields:
        fields["hourly_rate"], err = _num_field(fields, "hourly_rate", required=True, minimum=0)
        if err:
            return err
    if "billed" in fields:
        fields["billed"], err = _flag_field(fields, "billed")
        if err:
            return err
    if "paid" in fields:
        fields["paid"], err = _flag_field(fields, "paid", default=0)
        if err:
            return err
    if "work_date" in fields:
        fields["work_date"], err = _date_field(fields, "work_date")
        if err:
            return err
    if "travel_cost" in fields:
        fields["travel_cost"], err = _num_field(fields, "travel_cost", default=0.0, minimum=0)
        if err:
            return err
    if "paid" in fields or "paid_amount" in fields:
        alt = db.get_time_entry(eid)
        neu_h = fields.get("hours", alt["hours"])
        neu_r = fields.get("hourly_rate", alt["hourly_rate"])
        neu_t = fields.get("travel_cost", alt["travel_cost"] or 0)
        err = _teilzahlung_normalisieren(fields, neu_h * neu_r + neu_t)
        if err:
            return err
    db.update_time_entry(eid, fields)
    return _ok()


@app.route("/api/projects/<int:pid>/payout", methods=["POST"])
def api_project_payout(pid: int):
    """
    (Teil-)Auszahlung an eine Person verbuchen.

    Body (JSON): { "person": "Robert", "amount": 200 }
    Der Betrag wird auf die offenen Stunden-Einträge verteilt (älteste zuerst);
    der letzte berührte Eintrag kann dabei teilweise bezahlt bleiben.
    """
    if not db.get_project(pid):
        return _err("Projekt nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    person, err = _str_field(data, "person", required=True)
    if err:
        return err
    amount, err = _num_field(data, "amount", required=True, greater_than=0)
    if err:
        return err
    res = db.person_auszahlung(pid, person, amount)
    if "fehler" in res:
        return _err(res["fehler"])
    return _ok(**res)


@app.route("/api/projects/<int:pid>/guthaben", methods=["GET"])
def api_guthaben_get(pid: int):
    """Guthaben einer Person im Projekt. Query: ?person=Koffee"""
    if not db.get_project(pid):
        return _err("Projekt nicht gefunden.", 404)
    person = (request.args.get("person") or "").strip()
    if not person:
        return _err("Query-Parameter 'person' fehlt.")
    return _ok(**db.guthaben_saldo(pid, person))


@app.route("/api/projects/<int:pid>/guthaben", methods=["POST"])
def api_guthaben_create(pid: int):
    """
    Guthaben (Vorauszahlung) einzahlen. Wird sofort und danach bei jeder neuen
    Stunde automatisch mit offenen Einträgen der Person verrechnet (älteste zuerst).

    Body (JSON): { "person": "Koffee", "amount": 500, "notiz"?: "..." }
    """
    if not db.get_project(pid):
        return _err("Projekt nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    person, err = _str_field(data, "person", required=True)
    if err:
        return err
    amount, err = _num_field(data, "amount", required=True, greater_than=0)
    if err:
        return err
    notiz = data.get("notiz") or None
    res = db.guthaben_einzahlen(pid, person, amount, notiz)
    return _ok(**res), 201


@app.route("/api/timeentries/<int:eid>", methods=["DELETE"])
def api_timeentry_delete(eid: int):
    """Löscht einen Zeiteintrag."""
    if not db.delete_time_entry(eid):
        return _err("Zeiteintrag nicht gefunden.", 404)
    return _ok()


# ---------------------------------------------------------------------------
# Material (/api/tasks/<taskid>/materials, /api/materials/<mid>)
# ---------------------------------------------------------------------------

@app.route("/api/tasks/<int:task_id>/materials", methods=["GET"])
def api_materials_list(task_id: int):
    """Gibt alle Materialpositionen einer Aufgabe zurück (mit berechneten Kosten)."""
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    materials = _rows(db.list_materials({"task_id": task_id}))
    for m in materials:
        m["kosten"] = round(m["qty"] * m["unit_price"], 2)
    return _ok(materials=materials)


@app.route("/api/tasks/<int:task_id>/materials", methods=["POST"])
def api_materials_create(task_id: int):
    """
    Legt eine Materialposition an.

    Body (JSON): { "name", "qty", "unit_price", "unit"?, "billed"? }
    Pflichtfelder: name, qty, unit_price.
    billed: 1 = mit Rechnung (Standard), 0 = ohne Rechnung.
    """
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    name, err = _str_field(data, "name", required=True)
    if err:
        return err
    qty, err = _num_field(data, "qty", required=True, minimum=0)
    if err:
        return err
    unit_price, err = _num_field(data, "unit_price", required=True, minimum=0)
    if err:
        return err
    billed, err = _flag_field(data, "billed")
    if err:
        return err
    paid, err = _flag_field(data, "paid", default=0)
    if err:
        return err
    unit, err = _str_field(data, "unit")
    if err:
        return err
    store, err = _str_field(data, "store", max_length=120)
    if err:
        return err
    purchase_date, err = _date_field(data, "purchase_date")
    if err:
        return err
    tz = {"paid": paid}
    if "paid_amount" in data:
        tz["paid_amount"] = data["paid_amount"]
    err = _teilzahlung_normalisieren(tz, qty * unit_price)
    if err:
        return err
    mid = db.create_material(
        task_id=task_id,
        name=name,
        qty=qty,
        unit_price=unit_price,
        unit=unit,
        billed=billed,
        paid=tz["paid"],
        store=store,
        purchase_date=purchase_date,
    )
    if tz.get("paid_amount"):
        db.update_material(mid, {"paid_amount": tz["paid_amount"]})
    return _ok(id=mid), 201


@app.route("/api/materials/<int:mid>", methods=["PUT"])
def api_material_update(mid: int):
    """
    Aktualisiert Felder einer Materialposition.

    Body (JSON): beliebige Kombination aus { "name", "qty", "unit", "unit_price", "billed" }.
    """
    if not db.get_material(mid):
        return _err("Materialposition nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("name", "qty", "unit", "unit_price", "billed", "paid",
                                              "paid_amount", "store", "purchase_date") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "billed" in fields:
        fields["billed"], err = _flag_field(fields, "billed")
        if err:
            return err
    if "paid" in fields:
        fields["paid"], err = _flag_field(fields, "paid", default=0)
        if err:
            return err
    if "name" in fields:
        fields["name"], err = _str_field(fields, "name", required=True)
        if err:
            return err
    if "unit" in fields:
        fields["unit"], err = _str_field(fields, "unit")
        if err:
            return err
    if "store" in fields:
        fields["store"], err = _str_field(fields, "store", max_length=120)
        if err:
            return err
    if "purchase_date" in fields:
        fields["purchase_date"], err = _date_field(fields, "purchase_date")
        if err:
            return err
    if "qty" in fields:
        fields["qty"], err = _num_field(fields, "qty", required=True, minimum=0)
        if err:
            return err
    if "unit_price" in fields:
        fields["unit_price"], err = _num_field(fields, "unit_price", required=True, minimum=0)
        if err:
            return err
    if "paid" in fields or "paid_amount" in fields:
        alt = db.get_material(mid)
        neu_q = fields.get("qty", alt["qty"])
        neu_p = fields.get("unit_price", alt["unit_price"])
        err = _teilzahlung_normalisieren(fields, neu_q * neu_p)
        if err:
            return err
    db.update_material(mid, fields)
    return _ok()


@app.route("/api/materials/<int:mid>", methods=["DELETE"])
def api_material_delete(mid: int):
    """Löscht eine Materialposition (kaskadiert zu verknüpften Dokumenten) und entfernt zugehörige Upload-Dateien."""
    if not db.get_material(mid):
        return _err("Materialposition nicht gefunden.", 404)
    paths = db.document_paths_for_scope("material", mid)
    db.delete_material(mid)
    _remove_files(paths)
    return _ok()


# ---------------------------------------------------------------------------
# Dokumente & Fotos (/api/documents)
# ---------------------------------------------------------------------------

@app.route("/api/documents", methods=["POST"])
def api_documents_upload():
    """
    Lädt ein Dokument oder Foto hoch.

    multipart/form-data mit:
    - file:        Datei (.jpg/.jpeg/.png/.webp/.pdf), max. 20 MB
    - task_id:     Aufgaben-ID (mind. task_id oder material_id angeben)
    - material_id: Materialpositions-ID (alternativ zu task_id)
    - doc_type:    rechnung / beleg / foto (Standard: beleg)

    Speichert die Datei unter data/uploads/<doc_id>_<secure_filename>.
    Ermittelt site_id automatisch über die verknüpfte Aufgabe / Materialposition.
    """
    if "file" not in request.files:
        return _err("Feld 'file' fehlt im Multipart-Request.")
    f = request.files["file"]
    if not f.filename:
        return _err("Keine Datei ausgewählt.")
    if not _allowed_file(f.filename):
        return _err(
            f"Dateityp nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    doc_type = request.form.get("doc_type", "beleg")
    if doc_type not in VALID_DOC_TYPES:
        return _err(f"doc_type muss eines von {', '.join(sorted(VALID_DOC_TYPES))} sein.")

    task_id: int | None = None
    material_id: int | None = None
    site_id: int | None = None

    task_id_raw = request.form.get("task_id")
    material_id_raw = request.form.get("material_id")

    if task_id_raw:
        task_id, err = _int_arg("task_id", task_id_raw)
        if err:
            return err
        task = db.get_task(task_id)
        if not task:
            return _err("Aufgabe nicht gefunden.", 404)
        site_id = task["site_id"]

    if material_id_raw:
        material_id, err = _int_arg("material_id", material_id_raw)
        if err:
            return err
        material = db.get_material(material_id)
        if not material:
            return _err("Materialposition nicht gefunden.", 404)
        if site_id is None:
            # site_id über die Aufgabe der Materialposition ermitteln
            mat_task = db.get_task(material["task_id"])
            if mat_task:
                site_id = mat_task["site_id"]

    if task_id is None and material_id is None:
        return _err("Mindestens 'task_id' oder 'material_id' muss angegeben werden.")

    filename = secure_filename(f.filename)
    ext = Path(filename).suffix.lower()
    mime = mimetypes.types_map.get(ext, "application/octet-stream")

    # DB-Eintrag vorab anlegen, um die doc_id für den Dateinamen zu erhalten.
    # stored_path wird im Anschluss aktualisiert.
    doc_id = db.create_document(
        filename=filename,
        stored_path="",
        task_id=task_id,
        material_id=material_id,
        site_id=site_id,
        mime=mime,
        doc_type=doc_type,
    )

    stored_name = f"{doc_id}_{filename}"
    stored_path = os.path.join(UPLOADS_DIR, stored_name)

    try:
        f.save(stored_path)
    except OSError as exc:
        db.delete_document(doc_id)
        return _err(f"Datei konnte nicht gespeichert werden: {exc}", 500)

    db.update_document(doc_id, {"stored_path": stored_path})

    return _ok(id=doc_id, filename=filename, stored_name=stored_name), 201


@app.route("/api/documents", methods=["GET"])
def api_documents_list():
    """
    Gibt Dokumente zurück, filterbar per Query-Parameter.

    Query-Parameter: task_id, material_id, site_id, doc_type (alle optional).
    """
    filters: dict = {}
    for key in ("task_id", "material_id", "site_id"):
        val = request.args.get(key)
        if val:
            int_val, err = _int_arg(key, val)
            if err:
                return err
            filters[key] = int_val
    doc_type = request.args.get("doc_type")
    if doc_type:
        filters["doc_type"] = doc_type
    return _ok(documents=_rows(db.list_documents(filters or None)))


@app.route("/api/documents/<int:doc_id>/file", methods=["GET"])
def api_document_file(doc_id: int):
    """Liefert die gespeicherte Datei eines Dokuments aus (Vorschau / Download)."""
    doc = db.get_document(doc_id)
    if not doc:
        return _err("Dokument nicht gefunden.", 404)
    stored_path = doc["stored_path"]
    if not stored_path or not os.path.exists(stored_path):
        return _err("Datei nicht auf Disk gefunden.", 404)
    directory = os.path.dirname(os.path.abspath(stored_path))
    fname = os.path.basename(stored_path)
    mime = doc["mime"] or "application/octet-stream"
    return send_from_directory(directory, fname, mimetype=mime)


@app.route("/api/documents/<int:doc_id>", methods=["DELETE"])
def api_document_delete(doc_id: int):
    """Löscht ein Dokument aus der Datenbank und entfernt die Datei vom Dateisystem."""
    doc = db.get_document(doc_id)
    if not doc:
        return _err("Dokument nicht gefunden.", 404)
    stored_path = doc["stored_path"]
    db.delete_document(doc_id)
    if stored_path and os.path.exists(stored_path):
        try:
            os.remove(stored_path)
        except OSError:
            pass  # Fehler beim Löschen der Datei ist nicht fatal
    return _ok()


# ---------------------------------------------------------------------------
# Kassenbons (/api/receipts) — fest definiertes Schema: Markt, Datum, Positionen, Summe
# ---------------------------------------------------------------------------

def _receipt_dict(r) -> dict:
    d = dict(r)
    mats = _rows(db.receipt_materials(d["id"]))
    for m in mats:
        m["kosten"] = round(m["qty"] * m["unit_price"], 2)
    d["positionen"] = mats
    d["summe_positionen"] = round(sum(m["kosten"] for m in mats), 2)
    d["dokumente"] = _rows(db.list_documents({"receipt_id": d["id"]}))
    return d


@app.route("/api/tasks/<int:task_id>/receipts", methods=["POST"])
def api_receipt_create(task_id: int):
    """
    Legt einen Kassenbon mit allen Positionen an (alles oder nichts).

    Body (JSON): { "store", "purchase_date" (YYYY-MM-DD), "total",
                   "positions": [{ "name", "qty", "unit_price", "unit"? }, …],
                   "billed"?, "paid"? }
    Pflicht: store, purchase_date, total, mind. 1 Position.
    Prüfung: Summe der Positionen muss der Gesamtsumme laut Bon entsprechen (±0,02 €).
    Bei Abweichung wird NICHTS gebucht (HTTP 422) — dann Bon prüfen/abgeschnitten?
    """
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    store, err = _str_field(data, "store", required=True, max_length=120)
    if err:
        return err
    pdate, err = _date_field(data, "purchase_date")
    if err:
        return err
    if not pdate:
        return _err("Feld 'purchase_date' ist Pflichtfeld (Kaufdatum laut Bon, YYYY-MM-DD).")
    total, err = _num_field(data, "total", required=True, minimum=0)
    if err:
        return err
    billed, err = _flag_field(data, "billed")
    if err:
        return err
    paid, err = _flag_field(data, "paid", default=0)
    if err:
        return err
    raw = data.get("positions")
    if not isinstance(raw, list) or not raw:
        return _err("Feld 'positions' muss eine nicht leere Liste sein.")
    if len(raw) > 200:
        return _err("Zu viele Positionen (max. 200).")
    positions = []
    for i, p in enumerate(raw, start=1):
        if not isinstance(p, dict):
            return _err(f"Position {i}: muss ein Objekt sein.")
        name, err = _str_field(p, "name", required=True, max_length=200)
        if err:
            return _err(f"Position {i}: Bezeichnung fehlt.")
        qty, err = _num_field(p, "qty", required=True, minimum=0)
        if err:
            return _err(f"Position {i}: Menge ungültig.")
        price, err = _num_field(p, "unit_price", required=True, minimum=0)
        if err:
            return _err(f"Position {i}: Einzelpreis ungültig.")
        unit, err = _str_field(p, "unit", max_length=30)
        if err:
            return _err(f"Position {i}: Einheit ungültig.")
        positions.append({"name": name, "qty": qty, "unit_price": price, "unit": unit})
    summe = round(sum(round(p["qty"] * p["unit_price"], 2) for p in positions), 2)
    if abs(summe - round(total, 2)) > 0.02:
        return _err(
            f"Summe der Positionen ({summe:.2f} €) passt nicht zur Gesamtsumme laut Bon "
            f"({total:.2f} €). Nichts gebucht — Bon vollständig? Rabatte/Pfand als eigene Position?",
            422)
    rid, ids = db.create_receipt_with_materials(
        task_id, store, pdate, round(total, 2), positions, billed=billed, paid=paid)
    return _ok(id=rid, material_ids=ids, summe=summe), 201


@app.route("/api/receipts", methods=["GET"])
def api_receipts_list():
    """Bons auflisten. Query: task_id, site_id, project_id, store (Teilstring)."""
    filters: dict = {}
    for key in ("task_id", "site_id", "project_id"):
        val = request.args.get(key)
        if val:
            iv, err = _int_arg(key, val)
            if err:
                return err
            filters[key] = iv
    if request.args.get("store"):
        filters["store"] = request.args["store"]
    return _ok(receipts=[_receipt_dict(r) for r in db.list_receipts(filters or None)])


@app.route("/api/receipts/<int:rid>", methods=["GET"])
def api_receipt_get(rid: int):
    r = db.get_receipt(rid)
    if not r:
        return _err("Bon nicht gefunden.", 404)
    return _ok(receipt=_receipt_dict(r))


@app.route("/api/receipts/<int:rid>", methods=["DELETE"])
def api_receipt_delete(rid: int):
    """Löscht Bon samt Positionen und Bon-Dokumenten (Dateien werden mit entfernt)."""
    if not db.get_receipt(rid):
        return _err("Bon nicht gefunden.", 404)
    paths = db.receipt_document_paths(rid)
    db.delete_receipt(rid)
    _remove_files(paths)
    return _ok()


@app.route("/api/receipts/<int:rid>/document", methods=["POST"])
def api_receipt_document(rid: int):
    """
    Hängt die Bon-Datei (Foto/PDF) an den Bon. multipart/form-data: file.
    Die Datei hängt am Bon, nicht an jeder Einzelposition.
    """
    r = db.get_receipt(rid)
    if not r:
        return _err("Bon nicht gefunden.", 404)
    if "file" not in request.files or not request.files["file"].filename:
        return _err("Feld 'file' fehlt.")
    f = request.files["file"]
    if not _allowed_file(f.filename):
        return _err(f"Dateityp nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    task = db.get_task(r["task_id"])
    filename = secure_filename(f.filename)
    mime = mimetypes.types_map.get(Path(filename).suffix.lower(), "application/octet-stream")
    doc_id = db.create_document(filename=filename, stored_path="", task_id=r["task_id"],
                                site_id=task["site_id"] if task else None, mime=mime,
                                doc_type="beleg", receipt_id=rid)
    stored_path = os.path.join(UPLOADS_DIR, f"{doc_id}_{filename}")
    try:
        f.save(stored_path)
    except OSError as exc:
        db.delete_document(doc_id)
        return _err(f"Datei konnte nicht gespeichert werden: {exc}", 500)
    db.update_document(doc_id, {"stored_path": stored_path})
    return _ok(id=doc_id, filename=filename), 201


# ---------------------------------------------------------------------------
# Paperless-Integration (/api/documents/<doc_id>/paperless)
# ---------------------------------------------------------------------------


def _pl_lookup(base: str, headers: dict, endpoint: str, name: str, create_body: dict | None = None):
    """
    Sucht in Paperless einen Eintrag (tags / correspondents / document_types) über den Namen
    (exakt, ohne Groß-/Kleinschreibung; bei Korrespondenten zusätzlich Teilstring-Treffer wie
    'toom' → 'toom Baumarkt GmbH'). Fehlt er und create_body ist gesetzt, wird er angelegt.
    Gibt (id | None, neu_angelegt: bool) zurück.
    """
    wanted = name.strip().lower()
    page = f"{base}/api/{endpoint}/?page_size=500&name__icontains={requests.utils.quote(name.strip())}"
    resp = requests.get(page, headers=headers, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    for it in results:
        if it["name"].strip().lower() == wanted:
            return it["id"], False
    if endpoint == "correspondents" and len(wanted) >= 4:
        for it in results:
            if wanted in it["name"].lower():
                return it["id"], False
    if create_body is None:
        return None, False
    made = requests.post(f"{base}/api/{endpoint}/", headers=headers,
                         json={"name": name.strip(), **create_body}, timeout=15)
    made.raise_for_status()
    return made.json()["id"], True


def _paperless_meta(doc) -> dict:
    """
    Metadaten für Paperless aus dem Baulog ableiten: Titel, Datum, Korrespondent (Markt),
    Dokumenttyp und Tag-Namen (Projekt, Aufgabe, Gewerk, Rechnung ja/nein).
    """
    task_id = doc["task_id"]
    receipt = db.get_receipt(doc["receipt_id"]) if doc["receipt_id"] else None
    material = db.get_material(doc["material_id"]) if doc["material_id"] else None
    if task_id is None and receipt:
        task_id = receipt["task_id"]
    if task_id is None and material:
        task_id = material["task_id"]
    task = db.get_task(task_id) if task_id else None
    site = db.get_site(task["site_id"]) if task else None
    project = db.get_project(site["project_id"]) if site else None
    trade = db.get_trade(task["trade_id"]) if task and task["trade_id"] else None

    meta: dict = {"title": doc["filename"], "created": None, "store": None,
                  "doc_type_name": None, "tags": ["Baulog"]}
    if receipt:
        euro = f"{receipt['total']:.2f}".replace(".", ",")
        y, m, d = receipt["purchase_date"].split("-")
        meta["title"] = f"{receipt['store']}, {d}.{m}.{y}, {euro} €"
        meta["created"] = receipt["purchase_date"]
        meta["store"] = receipt["store"]
        meta["doc_type_name"] = "Kassenbon"
        positions = db.receipt_materials(receipt["id"])
        if positions and not positions[0]["billed"]:
            meta["tags"].append("ohne Rechnung")
    elif doc["doc_type"] == "rechnung":
        meta["doc_type_name"] = "Rechnung"
    elif doc["doc_type"] == "beleg":
        meta["doc_type_name"] = "Kassenbon"
    if project:
        meta["tags"].append(project["name"])
    if task:
        meta["tags"].append(task["title"])
    if trade:
        meta["tags"].append(trade["name"])
    # Duplikate entfernen, Reihenfolge behalten
    meta["tags"] = list(dict.fromkeys(t for t in meta["tags"] if t))
    return meta


@app.route("/api/documents/<int:doc_id>/paperless", methods=["POST"])
def api_document_paperless(doc_id: int):
    """
    Sendet ein Dokument an Paperless-ngx und speichert die Rückgabe-ID/-URL.

    Ablauf:
    1. Paperless-Config aus config.local.json oder Umgebungsvariablen laden.
    2. Datei per multipart an POST {url}/api/documents/post_document/ senden.
    3. Antwort enthält eine Task-ID (String).
    4. GET {url}/api/tasks/?task_id=<id> pollen bis status=='SUCCESS'.
    5. related_document = Paperless-Doc-ID → in documents.paperless_id/url speichern.

    Gibt 400 zurück, wenn Paperless nicht konfiguriert ist.
    """
    doc = db.get_document(doc_id)
    if not doc:
        return _err("Dokument nicht gefunden.", 404)

    paperless_url, paperless_token = _load_paperless_config()
    if not paperless_url or not paperless_token:
        return _err(
            "Paperless ist nicht konfiguriert. "
            "Bitte PAPERLESS_URL und PAPERLESS_TOKEN in config.local.json "
            "oder als Umgebungsvariablen setzen."
        )

    stored_path = doc["stored_path"]
    if not stored_path or not os.path.exists(stored_path):
        return _err("Datei nicht auf Disk gefunden.", 404)

    mime = doc["mime"] or "application/octet-stream"
    filename = doc["filename"]
    headers = {"Authorization": f"Token {paperless_token}"}

    # Metadaten aus dem Baulog ableiten und in Paperless auflösen (Tags, Markt, Dokumenttyp).
    # Fehlende Einträge werden NUR angelegt, wenn der Aufruf {"anlegen": true} enthält —
    # sonst bricht die Übergabe ab und meldet, was fehlt (Svens Regel: nichts Neues ohne Okay).
    meta = _paperless_meta(doc)
    body = request.get_json(silent=True) or {}
    anlegen = body.get("anlegen") is True
    form: list[tuple[str, str]] = [("title", meta["title"])]
    angelegt: list[str] = []
    fehlt: list[str] = []
    try:
        if meta["created"]:
            form.append(("created", meta["created"]))
        wanted = []
        if meta["store"]:
            wanted.append(("correspondents", meta["store"], "correspondent", "Korrespondent", {}))
        if meta["doc_type_name"]:
            wanted.append(("document_types", meta["doc_type_name"], "document_type", "Dokumenttyp",
                           {"matching_algorithm": 0}))
        for tag in meta["tags"]:
            wanted.append(("tags", tag, "tags", "Tag", {"matching_algorithm": 0}))
        for endpoint, name, field, label, create_body in wanted:
            found, _ = _pl_lookup(paperless_url, headers, endpoint, name, None)
            if found is None:
                if not anlegen:
                    fehlt.append(f"{label} „{name}“")
                    continue
                found, _ = _pl_lookup(paperless_url, headers, endpoint, name, create_body)
                angelegt.append(f"{label} „{name}“")
            form.append((field, str(found)))
    except requests.RequestException as exc:
        return _err(f"Paperless-Metadaten konnten nicht aufgelöst werden: {exc}", 502)
    if fehlt:
        return jsonify({
            "error": "In Paperless fehlen: " + "; ".join(fehlt) + ". Nichts gesendet. "
                     "Mit anlegen=true werden sie angelegt (vorher Sven fragen).",
            "fehlt": fehlt, "titel": meta["title"], "tags": meta["tags"]}), 409

    # Dokument an Paperless hochladen
    try:
        with open(stored_path, "rb") as fh:
            resp = requests.post(
                f"{paperless_url}/api/documents/post_document/",
                headers=headers,
                files={"document": (filename, fh, mime)},
                data=form,
                timeout=30,
            )
        if resp.status_code not in (200, 201, 202):
            return _err(
                f"Paperless-Upload fehlgeschlagen: HTTP {resp.status_code} — {resp.text[:300]}",
                502,
            )
        # Antwort ist die Task-ID als einfacher String oder JSON-String
        try:
            task_id = resp.json()
        except ValueError:
            task_id = resp.text.strip().strip('"')
    except requests.RequestException as exc:
        return _err(f"Verbindung zu Paperless fehlgeschlagen: {exc}", 502)

    # Task-Status pollen: max. 60 Sekunden (30 × 2 s)
    pl_doc_id = None
    for _ in range(30):
        time.sleep(2)
        try:
            poll = requests.get(
                f"{paperless_url}/api/tasks/",
                headers=headers,
                params={"task_id": task_id},
                timeout=10,
            )
            if poll.status_code != 200:
                continue
            tasks_data = poll.json()
            # Paperless liefert eine (ungefilterte) LISTE von Task-Einträgen —
            # den passenden Eintrag über die exakte task_id suchen, nicht [0] nehmen.
            entries = tasks_data if isinstance(tasks_data, list) else [tasks_data]
            match = None
            for entry in entries:
                if isinstance(entry, dict) and entry.get("task_id") == task_id:
                    match = entry
                    break
            if match is None:
                continue
            status = match.get("status")
            if status == "SUCCESS":
                pl_doc_id = match.get("related_document")
                break
            if status in ("FAILURE", "REVOKED"):
                detail = (match.get("result") or "")[:300]
                return _err(
                    f"Paperless konnte das Dokument nicht verarbeiten ({status}): {detail}", 502
                )
        except requests.RequestException:
            continue
    else:
        return _err("Paperless-Task hat nicht rechtzeitig geantwortet (Timeout 60 s).", 504)

    if pl_doc_id is None:
        return _err("Paperless hat keine Dokument-ID zurückgegeben.", 502)

    pl_url = f"{paperless_url}/documents/{pl_doc_id}"
    db.update_document(doc_id, {"paperless_id": int(pl_doc_id), "paperless_url": pl_url})
    return _ok(paperless_id=pl_doc_id, paperless_url=pl_url, titel=meta["title"],
               tags=meta["tags"], neu_in_paperless=angelegt)


# ---------------------------------------------------------------------------
# Kostenübersicht / Dashboard
# ---------------------------------------------------------------------------

@app.route("/api/sites/<int:sid>/summary", methods=["GET"])
def api_site_summary(sid: int):
    """
    Gibt Kosten-Summen für eine Baustelle zurück.

    Enthält Material- und Personalkosten je mit/ohne Rechnung sowie
    Aufschlüsselung je Gewerk.
    """
    if not db.get_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
    return _ok(summary=db.site_summary(sid))


@app.route("/api/projects/<int:pid>/summary", methods=["GET"])
def api_project_summary(pid: int):
    """
    Gibt Kosten-Summen für ein Projekt zurück (aggregiert über alle Baustellen).
    """
    if not db.get_project(pid):
        return _err("Projekt nicht gefunden.", 404)
    return _ok(summary=db.project_summary(pid))


@app.route("/api/summary", methods=["GET"])
def api_gesamt_summary():
    """Gibt Kosten-Summen über alle Projekte und Baustellen zurück."""
    return _ok(summary=db.gesamt_summary())


@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    """
    Liefert alle Dashboard-Daten in einem Aufruf.

    Query-Parameter: ?project=<pid> und/oder ?site=<sid> (beide optional).
    """
    project_id = site_id = None
    if request.args.get("project"):
        project_id, err = _int_arg("project", request.args["project"])
        if err:
            return err
        if not db.get_project(project_id):
            return _err("Projekt nicht gefunden.", 404)
    if request.args.get("site"):
        site_id, err = _int_arg("site", request.args["site"])
        if err:
            return err
        if not db.get_site(site_id):
            return _err("Baustelle nicht gefunden.", 404)
    return _ok(dashboard=db.dashboard_data(project_id=project_id, site_id=site_id))


# ---------------------------------------------------------------------------
# Konfiguration (kein Token ans Frontend)
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
def api_config():
    """
    Gibt ausschließlich den Paperless-aktiviert-Status zurück.

    Liefert NIEMALS den Paperless-Token oder andere Secrets.
    """
    return _ok(paperless_enabled=_paperless_enabled(),
               receipts_enabled=receipts.enabled(CONFIG_PATH))


# ---------------------------------------------------------------------------
# Kassenbon per Kamera auswerten (Claude) — bucht nichts, liefert nur den Vorschlag
# für die Prüfansicht; gebucht wird über POST /api/tasks/<id>/receipts.
# ---------------------------------------------------------------------------

_MEDIA_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}


@app.route("/api/receipts/analyze", methods=["POST"])
def api_receipts_analyze():
    """multipart: images (1–10 Fotos eines Belegs, von oben nach unten, mit Überlappung)."""
    files = request.files.getlist("images")
    if not files:
        return _err("Keine Bilder im Feld 'images'.")
    if len(files) > receipts.MAX_IMAGES:
        return _err(f"Maximal {receipts.MAX_IMAGES} Bilder pro Beleg.")
    images = []
    try:
        for f in files:
            ext = Path(secure_filename(f.filename or "")).suffix.lower()
            if ext == ".pdf":
                images.extend(receipts.pdf_zu_bildern(f.read(), receipts.MAX_IMAGES - len(images)))
                continue
            media = _MEDIA_BY_EXT.get(ext)
            if not media:
                return _err("Nur Fotos (JPEG, PNG, WebP) oder PDF erlaubt.")
            images.append((f.read(), media))
        images = images[:receipts.MAX_IMAGES]
        result = receipts.analyze(images, CONFIG_PATH)
    except receipts.ReceiptError as exc:
        return _err(str(exc), exc.status)
    return _ok(beleg=result)


@app.route("/api/ki-einstellungen", methods=["GET"])
def api_ki_get():
    """Anbieter/Modell der Belegauswertung — Keys nur maskiert (…abcd)."""
    return _ok(**receipts.public_settings(CONFIG_PATH))


@app.route("/api/ki-einstellungen", methods=["PUT"])
def api_ki_put():
    data = request.get_json(silent=True) or {}
    try:
        receipts.save_settings(CONFIG_PATH, data)
    except receipts.ReceiptError as exc:
        return _err(str(exc), exc.status)
    return _ok(**receipts.public_settings(CONFIG_PATH))


@app.route("/api/ki-einstellungen/test", methods=["POST"])
def api_ki_test():
    try:
        return _ok(meldung=receipts.verbindung_testen(CONFIG_PATH))
    except receipts.ReceiptError as exc:
        return _err(str(exc), exc.status)


# ---------------------------------------------------------------------------
# Eingang: Datei aus dem Teilen-Menü (iPhone-Kurzbefehl) zwischenlagern; die
# Oberfläche öffnet dann #/eingang/<id>, dort wird Projekt + Aufgabe gewählt.
# ---------------------------------------------------------------------------

EINGANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "eingang")
_EINGANG_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}


@app.route("/api/eingang", methods=["POST"])
def api_eingang_post():
    """multipart: file (PDF oder Foto, max. 20 MB). Antwort: id + url zum Öffnen."""
    import uuid
    f = request.files.get("file")
    if f is None:
        # manche Kurzbefehle schicken die Datei unter anderem Feldnamen
        f = next(iter(request.files.values()), None)
    if f is None:
        return _err("Keine Datei übergeben (Feld 'file').")
    name = secure_filename(f.filename or "") or "beleg.pdf"
    ext = Path(name).suffix.lower()
    data = f.read()
    if not ext and data.startswith(b"%PDF"):
        ext, name = ".pdf", name + ".pdf"
    if ext not in _EINGANG_EXT:
        return _err("Nur PDF oder Fotos erlaubt.")
    if not data or len(data) > 20 * 1024 * 1024:
        return _err("Datei leer oder größer als 20 MB.")
    os.makedirs(EINGANG_DIR, exist_ok=True)
    grenze = time.time() - 14 * 86400
    for alt in os.listdir(EINGANG_DIR):
        pfad = os.path.join(EINGANG_DIR, alt)
        if os.path.getmtime(pfad) < grenze:
            os.remove(pfad)
    eid = uuid.uuid4().hex[:16]
    with open(os.path.join(EINGANG_DIR, f"{eid}__{name}"), "wb") as fh:
        fh.write(data)
    url = request.host_url.rstrip("/") + f"/#/eingang/{eid}"
    return _ok(id=eid, url=url, name=name), 201


def _eingang_datei(eid):
    if not eid.isalnum() or not os.path.isdir(EINGANG_DIR):
        return None
    for n in os.listdir(EINGANG_DIR):
        if n.startswith(eid + "__"):
            return n
    return None


@app.route("/api/eingang/<eid>", methods=["GET"])
def api_eingang_get(eid):
    n = _eingang_datei(eid)
    if not n:
        return _err("Datei nicht (mehr) vorhanden.", 404)
    resp = send_from_directory(EINGANG_DIR, n)
    resp.headers["X-Dateiname"] = n.split("__", 1)[1]
    return resp


@app.route("/api/eingang/<eid>", methods=["DELETE"])
def api_eingang_delete(eid):
    n = _eingang_datei(eid)
    if n:
        os.remove(os.path.join(EINGANG_DIR, n))
    return _ok()


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    port = int(os.environ.get("TOOL_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
