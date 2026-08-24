"""
server.py — Flask-REST-API für baulog.

Importiert alle CRUD-Helfer und Aggregationsfunktionen aus db.py.
Setzt alle REST-Endpunkte gemäß PLAN.md Abschnitt 4 um.

Starten: python server.py (Port aus TOOL_PORT, Standard 5000)
"""

import json
import mimetypes
import os
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import db

# ---------------------------------------------------------------------------
# App-Konfiguration
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=".")

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "data", "uploads")

# Erlaubte Dateiendungen für den Dokumenten-Upload
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

# Maximale Upload-Größe: 20 MB
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# Pfad zur optionalen lokalen Konfigurationsdatei (nicht eingecheckt)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.local.json")

# Gültige Dokumenttypen
VALID_DOC_TYPES = {"rechnung", "beleg", "foto"}


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


# ---------------------------------------------------------------------------
# Statische Route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Liefert index.html aus dem Projektverzeichnis."""
    return send_from_directory(".", "index.html")


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
    name = (data.get("name") or "").strip()
    if not name:
        return _err("Feld 'name' ist Pflichtfeld.")
    pid = db.create_project(name=name, note=data.get("note") or None)
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
    db.update_project(pid, **fields)
    return _ok()


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
def api_project_delete(pid: int):
    """Löscht ein Projekt und kaskadiert zu Baustellen, Gewerken, Aufgaben usw."""
    if not db.delete_project(pid):
        return _err("Projekt nicht gefunden.", 404)
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
    name = (data.get("name") or "").strip()
    if not name:
        return _err("Feld 'name' ist Pflichtfeld.")
    project_id = data.get("project_id")
    if project_id is None:
        return _err("Feld 'project_id' ist Pflichtfeld.")
    try:
        project_id = int(project_id)
    except (ValueError, TypeError):
        return _err("'project_id' muss eine Ganzzahl sein.")
    if not db.get_project(project_id):
        return _err("Projekt nicht gefunden.", 404)
    sid = db.create_site(
        project_id=project_id,
        name=name,
        address=data.get("address") or None,
        status=data.get("status") or "aktiv",
        note=data.get("note") or None,
    )
    return _ok(id=sid), 201


@app.route("/api/sites/<int:sid>", methods=["GET"])
def api_site_get(sid: int):
    """Gibt eine Baustelle inkl. ihrer Gewerke zurück."""
    site = db.get_site(sid)
    if not site:
        return _err("Baustelle nicht gefunden.", 404)
    trades = _rows(db.list_trades({"site_id": sid}))
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
    db.update_site(sid, **fields)
    return _ok()


@app.route("/api/sites/<int:sid>", methods=["DELETE"])
def api_site_delete(sid: int):
    """Löscht eine Baustelle und kaskadiert zu Gewerken, Aufgaben, Zeiten, Material, Dokumenten."""
    if not db.delete_site(sid):
        return _err("Baustelle nicht gefunden.", 404)
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
    db.update_trade(tid, **fields)
    return _ok()


@app.route("/api/trades/<int:tid>", methods=["DELETE"])
def api_trade_delete(tid: int):
    """Löscht ein Gewerk. Aufgaben mit diesem Gewerk behalten trade_id=NULL."""
    if not db.delete_trade(tid):
        return _err("Gewerk nicht gefunden.", 404)
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
    title = (data.get("title") or "").strip()
    if not title:
        return _err("Feld 'title' ist Pflichtfeld.")
    trade_id = data.get("trade_id")
    if trade_id is not None:
        try:
            trade_id = int(trade_id)
        except (ValueError, TypeError):
            return _err("'trade_id' muss eine Ganzzahl sein.")
    task_id = db.create_task(
        site_id=sid,
        title=title,
        trade_id=trade_id,
        status=data.get("status") or "offen",
        description=data.get("description") or None,
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
    # Kosten je Eintrag ergänzen
    for e in time_entries:
        e["kosten"] = round(e["hours"] * e["hourly_rate"], 2)
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
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("title", "trade_id", "status", "description") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    db.update_task(task_id, **fields)
    return _ok()


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_task_delete(task_id: int):
    """Löscht eine Aufgabe und kaskadiert zu Zeiteinträgen, Materialien und Dokumenten."""
    if not db.delete_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    return _ok()


# ---------------------------------------------------------------------------
# Zeiterfassung (/api/tasks/<taskid>/timeentries, /api/timeentries/<eid>)
# ---------------------------------------------------------------------------

@app.route("/api/tasks/<int:task_id>/timeentries", methods=["GET"])
def api_timeentries_list(task_id: int):
    """Gibt alle Zeiteinträge einer Aufgabe zurück (mit berechneten Kosten)."""
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    entries = _rows(db.list_time_entries({"task_id": task_id}))
    for e in entries:
        e["kosten"] = round(e["hours"] * e["hourly_rate"], 2)
    return _ok(time_entries=entries)


@app.route("/api/tasks/<int:task_id>/timeentries", methods=["POST"])
def api_timeentries_create(task_id: int):
    """
    Legt einen Zeiteintrag (Personalkosten) an.

    Body (JSON): { "person", "hours", "hourly_rate", "billed"?, "work_date"? }
    Pflichtfelder: person, hours, hourly_rate.
    billed: 1 = mit Rechnung (Standard), 0 = ohne Rechnung.
    """
    if not db.get_task(task_id):
        return _err("Aufgabe nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    person = (data.get("person") or "").strip()
    if not person:
        return _err("Feld 'person' ist Pflichtfeld.")
    try:
        hours = float(data["hours"])
        hourly_rate = float(data["hourly_rate"])
    except (KeyError, ValueError, TypeError):
        return _err("Felder 'hours' und 'hourly_rate' sind Pflichtfelder (Zahlen).")
    billed = int(data.get("billed", 1))
    if billed not in (0, 1):
        return _err("'billed' muss 0 oder 1 sein.")
    eid = db.create_time_entry(
        task_id=task_id,
        person=person,
        hours=hours,
        hourly_rate=hourly_rate,
        billed=billed,
        work_date=data.get("work_date") or None,
    )
    return _ok(id=eid), 201


@app.route("/api/timeentries/<int:eid>", methods=["PUT"])
def api_timeentry_update(eid: int):
    """
    Aktualisiert Felder eines Zeiteintrags.

    Body (JSON): beliebige Kombination aus { "person", "hours", "hourly_rate", "billed", "work_date" }.
    """
    if not db.get_time_entry(eid):
        return _err("Zeiteintrag nicht gefunden.", 404)
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in ("person", "hours", "hourly_rate", "billed", "work_date") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "billed" in fields and int(fields["billed"]) not in (0, 1):
        return _err("'billed' muss 0 oder 1 sein.")
    db.update_time_entry(eid, **fields)
    return _ok()


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
    name = (data.get("name") or "").strip()
    if not name:
        return _err("Feld 'name' ist Pflichtfeld.")
    try:
        qty = float(data["qty"])
        unit_price = float(data["unit_price"])
    except (KeyError, ValueError, TypeError):
        return _err("Felder 'qty' und 'unit_price' sind Pflichtfelder (Zahlen).")
    billed = int(data.get("billed", 1))
    if billed not in (0, 1):
        return _err("'billed' muss 0 oder 1 sein.")
    mid = db.create_material(
        task_id=task_id,
        name=name,
        qty=qty,
        unit_price=unit_price,
        unit=data.get("unit") or None,
        billed=billed,
    )
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
    fields = {k: data[k] for k in ("name", "qty", "unit", "unit_price", "billed") if k in data}
    if not fields:
        return _err("Keine aktualisierbaren Felder angegeben.")
    if "billed" in fields and int(fields["billed"]) not in (0, 1):
        return _err("'billed' muss 0 oder 1 sein.")
    db.update_material(mid, **fields)
    return _ok()


@app.route("/api/materials/<int:mid>", methods=["DELETE"])
def api_material_delete(mid: int):
    """Löscht eine Materialposition (kaskadiert zu verknüpften Dokumenten)."""
    if not db.delete_material(mid):
        return _err("Materialposition nicht gefunden.", 404)
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

    db.update_document(doc_id, stored_path=stored_path)

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
# Paperless-Integration (/api/documents/<doc_id>/paperless)
# ---------------------------------------------------------------------------

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

    # Dokument an Paperless hochladen
    try:
        with open(stored_path, "rb") as fh:
            resp = requests.post(
                f"{paperless_url}/api/documents/post_document/",
                headers=headers,
                files={"document": (filename, fh, mime)},
                data={"title": filename},
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
            # Antwort kann Liste oder einzelnes Objekt sein
            entry = tasks_data[0] if isinstance(tasks_data, list) and tasks_data else tasks_data
            if isinstance(entry, dict) and entry.get("status") == "SUCCESS":
                pl_doc_id = entry.get("related_document")
                break
        except requests.RequestException:
            continue
    else:
        return _err("Paperless-Task hat nicht rechtzeitig geantwortet (Timeout 60 s).", 504)

    if pl_doc_id is None:
        return _err("Paperless hat keine Dokument-ID zurückgegeben.", 502)

    pl_url = f"{paperless_url}/documents/{pl_doc_id}"
    db.update_document(doc_id, paperless_id=int(pl_doc_id), paperless_url=pl_url)
    return _ok(paperless_id=pl_doc_id, paperless_url=pl_url)


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


# ---------------------------------------------------------------------------
# Konfiguration (kein Token ans Frontend)
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
def api_config():
    """
    Gibt ausschließlich den Paperless-aktiviert-Status zurück.

    Liefert NIEMALS den Paperless-Token oder andere Secrets.
    """
    return _ok(paperless_enabled=_paperless_enabled())


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    port = int(os.environ.get("TOOL_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
