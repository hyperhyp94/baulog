"""
db.py — SQLite-Datenzugriffsschicht für baulog.

Alle Tabellen, CRUD-Helfer und Kosten-Aggregationen gemäß PLAN.md Abschnitt 5 & 6.
Geldbeträge: REAL (Euro). Zeitstempel: ISO-8601-Text. billed: INTEGER (1=mit, 0=ohne).
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "baulog.db")


def _now() -> str:
    """Gibt den aktuellen UTC-Zeitstempel als ISO-8601-String zurück."""
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    """
    Öffnet eine Verbindung zur baulog-Datenbank.

    Legt den Ordner data/ beim ersten Aufruf automatisch an.
    Setzt row_factory=sqlite3.Row für dict-artigen Zugriff auf Spaltennamen.
    Aktiviert Foreign-Key-Unterstützung per PRAGMA.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """
    Legt alle Tabellen der Datenbank an, falls sie noch nicht existieren.

    Tabellen: projects, sites, trades, tasks, time_entries, materials, documents.
    Alle Fremdschlüssel-Kaskaden gemäß PLAN.md Abschnitt 5.
    """
    conn = get_conn()
    with conn:
        conn.executescript("""
            -- Projekte (oberste Ebene)
            CREATE TABLE IF NOT EXISTS projects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                note       TEXT,
                created_at TEXT    NOT NULL
            );

            -- Baustellen (innerhalb eines Projekts)
            CREATE TABLE IF NOT EXISTS sites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name       TEXT    NOT NULL,
                address    TEXT,
                status     TEXT    NOT NULL DEFAULT 'aktiv',
                note       TEXT,
                created_at TEXT    NOT NULL
            );

            -- Gewerke (je Baustelle)
            CREATE TABLE IF NOT EXISTS trades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id    INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                name       TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );

            -- Aufgaben (je Baustelle, optional Gewerk zugeordnet)
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                trade_id    INTEGER REFERENCES trades(id) ON DELETE SET NULL,
                title       TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'offen',
                description TEXT,
                created_at  TEXT    NOT NULL
            );

            -- Zeiterfassung / Personalkosten (je Aufgabe)
            CREATE TABLE IF NOT EXISTS time_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                person      TEXT    NOT NULL,
                hours       REAL    NOT NULL,
                hourly_rate REAL    NOT NULL,
                billed      INTEGER NOT NULL DEFAULT 1,
                work_date   TEXT,
                created_at  TEXT    NOT NULL
            );

            -- Materialpositionen (je Aufgabe)
            CREATE TABLE IF NOT EXISTS materials (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                name       TEXT    NOT NULL,
                qty        REAL    NOT NULL,
                unit       TEXT,
                unit_price REAL    NOT NULL,
                billed     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL
            );

            -- Dokumente & Fotos (hängen an Aufgabe oder Materialposition)
            CREATE TABLE IF NOT EXISTS documents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                material_id   INTEGER REFERENCES materials(id) ON DELETE CASCADE,
                site_id       INTEGER REFERENCES sites(id),
                filename      TEXT    NOT NULL,
                stored_path   TEXT    NOT NULL,
                mime          TEXT,
                doc_type      TEXT,
                paperless_id  INTEGER,
                paperless_url TEXT,
                created_at    TEXT    NOT NULL
            );
        """)
    conn.close()


# ---------------------------------------------------------------------------
# Hilfsfunktion: dict ohne None-Werte für UPDATE-Statements
# ---------------------------------------------------------------------------

def _filter_none(data: dict) -> dict:
    """Entfernt Schlüssel mit None-Werten aus einem Dict (für partielle UPDATEs)."""
    return {k: v for k, v in data.items() if v is not None}


# ---------------------------------------------------------------------------
# CRUD: projects
# ---------------------------------------------------------------------------

def create_project(name: str, note: str | None = None) -> int:
    """
    Legt ein neues Projekt an und gibt dessen ID zurück.

    :param name: Projektname (Pflichtfeld), z. B. „Umbau MFH Deisterstraße 40".
    :param note: Optionale Notiz.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO projects (name, note, created_at) VALUES (?, ?, ?)",
            (name, note, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_projects(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt alle Projekte zurück. Der optionale filters-Dict wird derzeit nicht genutzt
    (Platzhalter für zukünftige Filterung).
    """
    conn = get_conn()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def get_project(project_id: int) -> sqlite3.Row | None:
    """Gibt ein Projekt anhand seiner ID zurück oder None, falls nicht gefunden."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    return row


def update_project(project_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder eines Projekts.

    :param project_id: ID des Projekts.
    :param fields: Beliebige Kombination aus name, note.
    :returns: True wenn ein Datensatz geändert wurde, sonst False.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [project_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_project(project_id: int) -> bool:
    """
    Löscht ein Projekt und kaskadiert (ON DELETE CASCADE) zu Baustellen, Gewerken,
    Aufgaben, Zeiteinträgen, Materialien und Dokumenten.

    :returns: True wenn gelöscht, False wenn nicht gefunden.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: sites
# ---------------------------------------------------------------------------

def create_site(
    project_id: int,
    name: str,
    address: str | None = None,
    status: str = "aktiv",
    note: str | None = None,
) -> int:
    """
    Legt eine neue Baustelle innerhalb eines Projekts an.

    :param project_id: ID des übergeordneten Projekts.
    :param name: Name der Baustelle.
    :param address: Adresse (optional).
    :param status: 'aktiv' oder 'abgeschlossen' (Standard: 'aktiv').
    :param note: Optionale Notiz.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO sites (project_id, name, address, status, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, name, address, status, note, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_sites(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Baustellen zurück.

    :param filters: Optionales Dict, z. B. {'project_id': 1} filtert nach Projekt.
    """
    conn = get_conn()
    if filters and "project_id" in filters:
        rows = conn.execute(
            "SELECT * FROM sites WHERE project_id = ? ORDER BY created_at DESC",
            (filters["project_id"],),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sites ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return rows


def get_site(site_id: int) -> sqlite3.Row | None:
    """Gibt eine Baustelle anhand ihrer ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    conn.close()
    return row


def update_site(site_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder einer Baustelle.

    :param site_id: ID der Baustelle.
    :param fields: Beliebige Kombination aus name, address, status, note, project_id.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [site_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE sites SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_site(site_id: int) -> bool:
    """Löscht eine Baustelle (kaskadiert zu Gewerken, Aufgaben, Zeiten, Material, Dok.)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: trades
# ---------------------------------------------------------------------------

def create_trade(site_id: int, name: str) -> int:
    """
    Legt ein neues Gewerk für eine Baustelle an.

    :param site_id: ID der Baustelle.
    :param name: Name des Gewerks, z. B. „Maurer", „Elektro", „Sanitär".
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO trades (site_id, name, created_at) VALUES (?, ?, ?)",
            (site_id, name, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_trades(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Gewerke zurück.

    :param filters: Optionales Dict, z. B. {'site_id': 2} filtert nach Baustelle.
    """
    conn = get_conn()
    if filters and "site_id" in filters:
        rows = conn.execute(
            "SELECT * FROM trades WHERE site_id = ? ORDER BY name",
            (filters["site_id"],),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM trades ORDER BY name").fetchall()
    conn.close()
    return rows


def get_trade(trade_id: int) -> sqlite3.Row | None:
    """Gibt ein Gewerk anhand seiner ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()
    return row


def update_trade(trade_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder eines Gewerks.

    :param trade_id: ID des Gewerks.
    :param fields: Beliebige Kombination aus name.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [trade_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_trade(trade_id: int) -> bool:
    """
    Löscht ein Gewerk. Aufgaben mit diesem Gewerk behalten trade_id=NULL
    (ON DELETE SET NULL in tasks).
    """
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: tasks
# ---------------------------------------------------------------------------

def create_task(
    site_id: int,
    title: str,
    trade_id: int | None = None,
    status: str = "offen",
    description: str | None = None,
) -> int:
    """
    Legt eine neue Aufgabe an.

    :param site_id: ID der Baustelle.
    :param title: Aufgabentitel, z. B. „Wand ziehen".
    :param trade_id: Optionale Zuordnung zu einem Gewerk.
    :param status: 'offen', 'in Arbeit' oder 'fertig' (Standard: 'offen').
    :param description: Optionale Beschreibung.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks (site_id, trade_id, title, status, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (site_id, trade_id, title, status, description, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_tasks(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Aufgaben zurück, optional gefiltert.

    :param filters: Dict mit optionalen Schlüsseln site_id, trade_id, status.
    """
    conn = get_conn()
    where_parts = []
    params = []
    if filters:
        for key in ("site_id", "trade_id", "status"):
            if key in filters and filters[key] is not None:
                where_parts.append(f"{key} = ?")
                params.append(filters[key])
    sql = "SELECT * FROM tasks"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def get_task(task_id: int) -> sqlite3.Row | None:
    """Gibt eine Aufgabe anhand ihrer ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return row


def update_task(task_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder einer Aufgabe.

    :param task_id: ID der Aufgabe.
    :param fields: Beliebige Kombination aus title, trade_id, status, description.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [task_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_task(task_id: int) -> bool:
    """Löscht eine Aufgabe (kaskadiert zu Zeiteinträgen, Materialien, Dokumenten)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: time_entries
# ---------------------------------------------------------------------------

def create_time_entry(
    task_id: int,
    person: str,
    hours: float,
    hourly_rate: float,
    billed: int = 1,
    work_date: str | None = None,
) -> int:
    """
    Legt einen Zeiteintrag (Personalkosten) an.

    Kosten werden serverseitig als hours * hourly_rate berechnet.

    :param task_id: ID der Aufgabe.
    :param person: Name der Person.
    :param hours: Stundenaufwand.
    :param hourly_rate: Stundensatz in Euro.
    :param billed: 1 = mit Rechnung, 0 = ohne Rechnung (Standard: 1).
    :param work_date: Optionales Datum der Leistung (ISO YYYY-MM-DD).
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO time_entries "
            "(task_id, person, hours, hourly_rate, billed, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, person, hours, hourly_rate, billed, work_date, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_time_entries(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Zeiteinträge zurück.

    :param filters: Optionales Dict, z. B. {'task_id': 5}.
    """
    conn = get_conn()
    if filters and "task_id" in filters:
        rows = conn.execute(
            "SELECT * FROM time_entries WHERE task_id = ? ORDER BY created_at DESC",
            (filters["task_id"],),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM time_entries ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return rows


def get_time_entry(entry_id: int) -> sqlite3.Row | None:
    """Gibt einen Zeiteintrag anhand seiner ID zurück oder None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM time_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    conn.close()
    return row


def update_time_entry(entry_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder eines Zeiteintrags.

    :param entry_id: ID des Zeiteintrags.
    :param fields: Beliebige Kombination aus person, hours, hourly_rate, billed, work_date.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [entry_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE time_entries SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_time_entry(entry_id: int) -> bool:
    """Löscht einen Zeiteintrag anhand seiner ID."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM time_entries WHERE id = ?", (entry_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: materials
# ---------------------------------------------------------------------------

def create_material(
    task_id: int,
    name: str,
    qty: float,
    unit_price: float,
    unit: str | None = None,
    billed: int = 1,
) -> int:
    """
    Legt eine Materialposition an.

    Kosten werden serverseitig als qty * unit_price berechnet.

    :param task_id: ID der Aufgabe.
    :param name: Bezeichnung des Materials.
    :param qty: Menge.
    :param unit_price: Einzelpreis in Euro.
    :param unit: Einheit (z. B. 'Stk', 'm²', 'kg', 'l'); optional.
    :param billed: 1 = mit Rechnung, 0 = ohne Rechnung (Standard: 1).
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO materials "
            "(task_id, name, qty, unit, unit_price, billed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, name, qty, unit, unit_price, billed, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_materials(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Materialpositionen zurück.

    :param filters: Optionales Dict, z. B. {'task_id': 3}.
    """
    conn = get_conn()
    if filters and "task_id" in filters:
        rows = conn.execute(
            "SELECT * FROM materials WHERE task_id = ? ORDER BY created_at DESC",
            (filters["task_id"],),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM materials ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return rows


def get_material(material_id: int) -> sqlite3.Row | None:
    """Gibt eine Materialposition anhand ihrer ID zurück oder None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM materials WHERE id = ?", (material_id,)
    ).fetchone()
    conn.close()
    return row


def update_material(material_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder einer Materialposition.

    :param material_id: ID der Materialposition.
    :param fields: Beliebige Kombination aus name, qty, unit, unit_price, billed.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [material_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE materials SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_material(material_id: int) -> bool:
    """Löscht eine Materialposition (kaskadiert zu verknüpften Dokumenten)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM materials WHERE id = ?", (material_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: documents
# ---------------------------------------------------------------------------

def create_document(
    filename: str,
    stored_path: str,
    task_id: int | None = None,
    material_id: int | None = None,
    site_id: int | None = None,
    mime: str | None = None,
    doc_type: str | None = None,
    paperless_id: int | None = None,
    paperless_url: str | None = None,
) -> int:
    """
    Legt einen Dokument-/Foto-Eintrag an.

    Mindestens eines von task_id oder material_id sollte gesetzt sein.
    site_id dient der Denormalisierung für schnelle Galerie-Filterung.

    :param filename: Originaldateiname.
    :param stored_path: Pfad der gespeicherten Datei in data/uploads/.
    :param task_id: Optionale Verknüpfung mit einer Aufgabe.
    :param material_id: Optionale Verknüpfung mit einer Materialposition.
    :param site_id: Baustellen-ID (Denormalisierung).
    :param mime: MIME-Type, z. B. 'image/jpeg', 'application/pdf'.
    :param doc_type: 'rechnung', 'beleg' oder 'foto'.
    :param paperless_id: ID des Dokuments in Paperless-ngx (nach Sync).
    :param paperless_url: URL des Dokuments in Paperless-ngx (nach Sync).
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO documents "
            "(task_id, material_id, site_id, filename, stored_path, mime, "
            " doc_type, paperless_id, paperless_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, material_id, site_id, filename, stored_path, mime,
             doc_type, paperless_id, paperless_url, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_documents(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Dokumente zurück, optional gefiltert.

    :param filters: Dict mit optionalen Schlüsseln task_id, material_id, site_id, doc_type.
    """
    conn = get_conn()
    where_parts = []
    params = []
    if filters:
        for key in ("task_id", "material_id", "site_id", "doc_type"):
            if key in filters and filters[key] is not None:
                where_parts.append(f"{key} = ?")
                params.append(filters[key])
    sql = "SELECT * FROM documents"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def get_document(doc_id: int) -> sqlite3.Row | None:
    """Gibt ein Dokument anhand seiner ID zurück oder None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    conn.close()
    return row


def update_document(doc_id: int, **fields) -> bool:
    """
    Aktualisiert nur die übergebenen Felder eines Dokuments.

    :param doc_id: ID des Dokuments.
    :param fields: Beliebige Kombination aus paperless_id, paperless_url, doc_type.
    """
    data = _filter_none(fields)
    if not data:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [doc_id]
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = ?", values
        )
    conn.close()
    return cur.rowcount > 0


def delete_document(doc_id: int) -> bool:
    """Löscht den Datenbankeintrag eines Dokuments (Datei auf Disk separat entfernen)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Kosten-Aggregation (PLAN.md Abschnitt 6)
# ---------------------------------------------------------------------------

def _empty_summary() -> dict:
    """Gibt ein leeres Kosten-Summary-Dict mit Nullwerten zurück."""
    return {
        "mat_mit": 0.0,
        "mat_ohne": 0.0,
        "mat_gesamt": 0.0,
        "pers_mit": 0.0,
        "pers_ohne": 0.0,
        "pers_gesamt": 0.0,
        "gesamt_mit": 0.0,
        "gesamt_ohne": 0.0,
        "gesamt": 0.0,
        "by_trade": [],
    }


def _aggregate_costs(conn: sqlite3.Connection, site_filter: str, params: list) -> dict:
    """
    Interne Hilfsfunktion: berechnet Kosten-Summen für einen gegebenen Scope
    (eine Baustelle, ein Projekt oder global).

    :param conn: Offene Datenbankverbindung.
    :param site_filter: SQL-Fragment für den WHERE-Teil, z. B. 't.site_id = ?'.
    :param params: Bindungsparameter passend zum site_filter.
    :returns: Summary-Dict mit Gesamtsummen und Aufschlüsselung je Gewerk.
    """
    result = _empty_summary()

    # --- Material-Summen ---
    mat_sql = f"""
        SELECT
            COALESCE(SUM(CASE WHEN m.billed = 1 THEN m.qty * m.unit_price ELSE 0 END), 0) AS mat_mit,
            COALESCE(SUM(CASE WHEN m.billed = 0 THEN m.qty * m.unit_price ELSE 0 END), 0) AS mat_ohne
        FROM materials m
        JOIN tasks t ON m.task_id = t.id
        WHERE {site_filter}
    """
    row = conn.execute(mat_sql, params).fetchone()
    if row:
        result["mat_mit"] = row["mat_mit"]
        result["mat_ohne"] = row["mat_ohne"]
    result["mat_gesamt"] = result["mat_mit"] + result["mat_ohne"]

    # --- Personal-Summen ---
    pers_sql = f"""
        SELECT
            COALESCE(SUM(CASE WHEN te.billed = 1 THEN te.hours * te.hourly_rate ELSE 0 END), 0) AS pers_mit,
            COALESCE(SUM(CASE WHEN te.billed = 0 THEN te.hours * te.hourly_rate ELSE 0 END), 0) AS pers_ohne
        FROM time_entries te
        JOIN tasks t ON te.task_id = t.id
        WHERE {site_filter}
    """
    row = conn.execute(pers_sql, params).fetchone()
    if row:
        result["pers_mit"] = row["pers_mit"]
        result["pers_ohne"] = row["pers_ohne"]
    result["pers_gesamt"] = result["pers_mit"] + result["pers_ohne"]

    result["gesamt_mit"] = result["mat_mit"] + result["pers_mit"]
    result["gesamt_ohne"] = result["mat_ohne"] + result["pers_ohne"]
    result["gesamt"] = result["gesamt_mit"] + result["gesamt_ohne"]

    # --- Aufschlüsselung je Gewerk ---
    # Material je Gewerk
    trade_mat_sql = f"""
        SELECT
            t.trade_id,
            tr.name AS trade_name,
            COALESCE(SUM(CASE WHEN m.billed = 1 THEN m.qty * m.unit_price ELSE 0 END), 0) AS mat_mit,
            COALESCE(SUM(CASE WHEN m.billed = 0 THEN m.qty * m.unit_price ELSE 0 END), 0) AS mat_ohne
        FROM tasks t
        LEFT JOIN trades tr ON t.trade_id = tr.id
        JOIN materials m ON m.task_id = t.id
        WHERE {site_filter}
        GROUP BY t.trade_id, tr.name
    """
    trade_mat = {
        (r["trade_id"], r["trade_name"]): {
            "mat_mit": r["mat_mit"],
            "mat_ohne": r["mat_ohne"],
        }
        for r in conn.execute(trade_mat_sql, params).fetchall()
    }

    # Personal je Gewerk
    trade_pers_sql = f"""
        SELECT
            t.trade_id,
            tr.name AS trade_name,
            COALESCE(SUM(CASE WHEN te.billed = 1 THEN te.hours * te.hourly_rate ELSE 0 END), 0) AS pers_mit,
            COALESCE(SUM(CASE WHEN te.billed = 0 THEN te.hours * te.hourly_rate ELSE 0 END), 0) AS pers_ohne
        FROM tasks t
        LEFT JOIN trades tr ON t.trade_id = tr.id
        JOIN time_entries te ON te.task_id = t.id
        WHERE {site_filter}
        GROUP BY t.trade_id, tr.name
    """
    trade_pers = {
        (r["trade_id"], r["trade_name"]): {
            "pers_mit": r["pers_mit"],
            "pers_ohne": r["pers_ohne"],
        }
        for r in conn.execute(trade_pers_sql, params).fetchall()
    }

    # Alle beteiligten Gewerk-Schlüssel sammeln
    all_trade_keys = set(trade_mat.keys()) | set(trade_pers.keys())
    by_trade = []
    for key in sorted(all_trade_keys, key=lambda k: (k[1] or "")):
        trade_id, trade_name = key
        mm = trade_mat.get(key, {}).get("mat_mit", 0.0)
        mo = trade_mat.get(key, {}).get("mat_ohne", 0.0)
        pm = trade_pers.get(key, {}).get("pers_mit", 0.0)
        po = trade_pers.get(key, {}).get("pers_ohne", 0.0)
        by_trade.append({
            "trade_id": trade_id,
            "trade_name": trade_name or "(kein Gewerk)",
            "mat_mit": mm,
            "mat_ohne": mo,
            "mat_gesamt": mm + mo,
            "pers_mit": pm,
            "pers_ohne": po,
            "pers_gesamt": pm + po,
            "gesamt_mit": mm + pm,
            "gesamt_ohne": mo + po,
            "gesamt": mm + mo + pm + po,
        })
    result["by_trade"] = by_trade
    return result


def site_summary(site_id: int) -> dict:
    """
    Berechnet Kosten-Summen für eine einzelne Baustelle.

    Liefert Material- und Personalkosten, jeweils getrennt nach
    'mit Rechnung' (billed=1) und 'ohne Rechnung' (billed=0),
    plus Gesamtsummen und Aufschlüsselung je Gewerk.

    :param site_id: ID der Baustelle.
    :returns: Summary-Dict (mat_mit, mat_ohne, mat_gesamt, pers_mit, pers_ohne,
              pers_gesamt, gesamt_mit, gesamt_ohne, gesamt, by_trade).
    """
    conn = get_conn()
    result = _aggregate_costs(conn, "t.site_id = ?", [site_id])
    conn.close()
    return result


def project_summary(project_id: int) -> dict:
    """
    Berechnet Kosten-Summen für ein gesamtes Projekt (alle Baustellen des Projekts).

    Liefert dieselbe Struktur wie site_summary(), aggregiert über alle
    Baustellen, die dem Projekt angehören.

    :param project_id: ID des Projekts.
    :returns: Summary-Dict analog zu site_summary().
    """
    conn = get_conn()
    site_filter = "t.site_id IN (SELECT id FROM sites WHERE project_id = ?)"
    result = _aggregate_costs(conn, site_filter, [project_id])
    conn.close()
    return result


def gesamt_summary() -> dict:
    """
    Berechnet Kosten-Summen über alle Projekte und Baustellen hinweg.

    Liefert dieselbe Struktur wie site_summary(), ohne Einschränkung auf
    ein bestimmtes Projekt oder eine Baustelle.

    :returns: Summary-Dict analog zu site_summary().
    """
    conn = get_conn()
    result = _aggregate_costs(conn, "1 = 1", [])
    conn.close()
    return result
