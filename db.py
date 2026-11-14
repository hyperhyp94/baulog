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


SCHEMA_VERSION = 6

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sites_project ON sites(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_site ON trades(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_site ON tasks(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_trade ON tasks(trade_id)",
    "CREATE INDEX IF NOT EXISTS idx_time_entries_task ON time_entries(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_time_entries_person ON time_entries(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_materials_task ON materials(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_task ON documents(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_material ON documents(material_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_site ON documents(site_id)",
]


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _db_has_data(conn: sqlite3.Connection) -> bool:
    for t in ("projects", "sites", "tasks", "time_entries", "materials", "documents"):
        if conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
            return True
    return False


def _backup_db() -> str | None:
    """Sichert die DB-Datei vor einer Migration über SQLites Online-Backup-API."""
    if not os.path.exists(DB_PATH):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = f"{DB_PATH}.bak-{stamp}"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    except Exception:
        # Clean up on failure: close connections and remove partial backup file
        src.close()
        dst.close()
        if os.path.exists(target):
            os.remove(target)
        raise
    else:
        # Success: close connections normally
        src.close()
        dst.close()
    return target


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v0 → v1: persons-Tabelle, time_entries.person_id, Backfill, Indizes."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS persons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                hourly_rate REAL    NOT NULL DEFAULT 0,
                active      INTEGER NOT NULL DEFAULT 1,
                note        TEXT,
                created_at  TEXT    NOT NULL
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(time_entries)")]
        if "person_id" not in cols:
            conn.execute(
                "ALTER TABLE time_entries ADD COLUMN person_id INTEGER "
                "REFERENCES persons(id) ON DELETE SET NULL"
            )
        # Backfill: je vorhandenem Namen eine Person, Satz = jüngster Eintrag
        conn.execute("""
            INSERT INTO persons (name, hourly_rate, created_at)
            SELECT te.person,
                   (SELECT te2.hourly_rate FROM time_entries te2
                    WHERE te2.person = te.person
                    ORDER BY te2.created_at DESC, te2.id DESC LIMIT 1),
                   ?
            FROM time_entries te
            WHERE te.person NOT IN (SELECT name FROM persons)
            GROUP BY te.person
        """, (_now(),))
        conn.execute(
            "UPDATE time_entries SET person_id = "
            "(SELECT id FROM persons p WHERE p.name = time_entries.person) "
            "WHERE person_id IS NULL"
        )
        for stmt in _INDEXES:
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 1")


_BASE_SCHEMA = """
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
"""


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: Zahlungsstatus 'paid' (0 = offen, 1 = bezahlt) für Zeiteinträge und Materialien."""
    with conn:
        for table in ("time_entries", "materials"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "paid" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN paid INTEGER NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 2")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: Anfahrt 'travel_cost' (Euro, pauschal je Arbeitstag) für Zeiteinträge."""
    with conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(time_entries)")]
        if "travel_cost" not in cols:
            conn.execute("ALTER TABLE time_entries ADD COLUMN travel_cost REAL NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 3")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: Teilzahlungen — 'paid_amount' (Euro bereits gezahlt, solange paid = 0)."""
    with conn:
        for table in ("time_entries", "materials"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "paid_amount" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 4")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5: Guthaben je Person/Projekt (Vorauszahlungen) + Verbrauch auf Zeiteinträge."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guthaben (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                person     TEXT    NOT NULL,
                betrag     REAL    NOT NULL,
                notiz      TEXT,
                created_at TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guthaben_verbrauch (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guthaben_id   INTEGER NOT NULL REFERENCES guthaben(id) ON DELETE CASCADE,
                time_entry_id INTEGER NOT NULL REFERENCES time_entries(id) ON DELETE CASCADE,
                betrag        REAL    NOT NULL,
                created_at    TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guthaben_person ON guthaben(project_id, person)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gv_entry ON guthaben_verbrauch(time_entry_id)")
        conn.execute("PRAGMA user_version = 5")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v5 → v6: Kassenbons — Tabelle 'receipts' (Markt, Kaufdatum, Summe) und Verknüpfung.

    materials: store (Markt), purchase_date (Kaufdatum), receipt_id (Bon).
    documents: receipt_id (der Bon als Datei hängt am Bon, nicht an einer Einzelposition).
    """
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                store         TEXT    NOT NULL,
                purchase_date TEXT    NOT NULL,
                total         REAL    NOT NULL,
                created_at    TEXT    NOT NULL
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(materials)")]
        if "store" not in cols:
            conn.execute("ALTER TABLE materials ADD COLUMN store TEXT")
        if "purchase_date" not in cols:
            conn.execute("ALTER TABLE materials ADD COLUMN purchase_date TEXT")
        if "receipt_id" not in cols:
            conn.execute("ALTER TABLE materials ADD COLUMN receipt_id INTEGER "
                         "REFERENCES receipts(id) ON DELETE CASCADE")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(documents)")]
        if "receipt_id" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN receipt_id INTEGER "
                         "REFERENCES receipts(id) ON DELETE CASCADE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_receipt ON materials(receipt_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_receipt ON documents(receipt_id)")
        conn.execute("PRAGMA user_version = 6")


def init_db() -> None:
    """Legt das Basis-Schema an und führt ausstehende Migrationen aus."""
    conn = get_conn()
    with conn:
        conn.executescript(_BASE_SCHEMA)
    if _user_version(conn) < SCHEMA_VERSION:
        if _db_has_data(conn):
            _backup_db()
        if _user_version(conn) < 1:
            _migrate_v1(conn)
        if _user_version(conn) < 2:
            _migrate_v2(conn)
        if _user_version(conn) < 3:
            _migrate_v3(conn)
        if _user_version(conn) < 4:
            _migrate_v4(conn)
        if _user_version(conn) < 5:
            _migrate_v5(conn)
        if _user_version(conn) < 6:
            _migrate_v6(conn)
    install_changelog(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Änderungsprotokoll: jede Änderung an den Datentabellen wird per SQLite-Trigger
# mit altem und neuem Zeilenstand (JSON) mitgeschrieben — egal ob sie über die
# Oberfläche, Hermes/MCP oder direkt in der DB passiert. Damit lässt sich jede
# Änderung nachvollziehen und einzeln rückgängig machen.
# ---------------------------------------------------------------------------

CHANGELOG_TABLES = ("projects", "sites", "trades", "tasks", "time_entries", "materials",
                    "documents", "persons", "guthaben", "guthaben_verbrauch", "receipts")


def install_changelog(conn: sqlite3.Connection) -> None:
    """
    Legt die Tabelle changelog an und (re)generiert die Trigger aus den aktuellen
    Spalten — nach jeder Migration aufgerufen, damit neue Spalten mitprotokolliert werden.
    """
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS changelog (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            tbl      TEXT    NOT NULL,
            row_id   INTEGER NOT NULL,
            action   TEXT    NOT NULL,          -- insert | update | delete
            old_json TEXT,
            new_json TEXT,
            quelle   TEXT,                      -- Web-Oberfläche | Hermes/MCP | NULL = direkt in der DB
            undone_by INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_changelog_row ON changelog(tbl, row_id)")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in CHANGELOG_TABLES:
            for act in ("insert", "update", "delete"):
                conn.execute(f"DROP TRIGGER IF EXISTS log_{t}_{act}")
            if t not in existing:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            def obj(prefix):
                return "json_object(" + ", ".join(f"'{c}', {prefix}.{c}" for c in cols) + ")"
            conn.execute(f"""CREATE TRIGGER log_{t}_insert AFTER INSERT ON {t} BEGIN
                INSERT INTO changelog (tbl, row_id, action, new_json)
                VALUES ('{t}', NEW.id, 'insert', {obj('NEW')}); END""")
            changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in cols)
            conn.execute(f"""CREATE TRIGGER log_{t}_update AFTER UPDATE ON {t}
                WHEN {changed} BEGIN
                INSERT INTO changelog (tbl, row_id, action, old_json, new_json)
                VALUES ('{t}', NEW.id, 'update', {obj('OLD')}, {obj('NEW')}); END""")
            conn.execute(f"""CREATE TRIGGER log_{t}_delete AFTER DELETE ON {t} BEGIN
                INSERT INTO changelog (tbl, row_id, action, old_json)
                VALUES ('{t}', OLD.id, 'delete', {obj('OLD')}); END""")


def changelog_max_id() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM changelog").fetchone()[0]
    finally:
        conn.close()


def changelog_tag(after_id: int, quelle: str) -> None:
    """Ordnet alle seit after_id entstandenen, noch unmarkierten Einträge einer Quelle zu."""
    conn = get_conn()
    with conn:
        conn.execute("UPDATE changelog SET quelle = ? WHERE id > ? AND quelle IS NULL",
                     (quelle, after_id))
    conn.close()


def list_changelog(limit: int = 200, tbl: str | None = None, row_id: int | None = None,
                   before_id: int | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM changelog WHERE 1=1", []
    if tbl:
        sql += " AND tbl = ?"; args.append(tbl)
    if row_id is not None:
        sql += " AND row_id = ?"; args.append(row_id)
    if before_id:
        sql += " AND id < ?"; args.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    conn = get_conn()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


class UndoError(Exception):
    pass


def undo_change(change_id: int) -> int:
    """
    Macht eine protokollierte Änderung rückgängig und liefert die id des dadurch
    entstandenen Protokolleintrags.
    - update → alte Werte zurückschreiben (nur wenn die Zeile seitdem nicht weiter geändert wurde)
    - insert → Zeile löschen
    - delete → Zeile mit alten Werten wieder anlegen
    """
    import json
    conn = get_conn()
    try:
        c = conn.execute("SELECT * FROM changelog WHERE id = ?", (change_id,)).fetchone()
        if not c:
            raise UndoError("Protokolleintrag nicht gefunden.")
        if c["undone_by"]:
            raise UndoError("Diese Änderung wurde bereits rückgängig gemacht.")
        t = c["tbl"]
        if t not in CHANGELOG_TABLES:
            raise UndoError("Tabelle nicht unterstützt.")
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        later = conn.execute(
            "SELECT 1 FROM changelog WHERE tbl = ? AND row_id = ? AND id > ? AND undone_by IS NULL "
            "AND id NOT IN (SELECT undone_by FROM changelog WHERE undone_by IS NOT NULL)",
            (t, c["row_id"], change_id)).fetchone()
        if later and c["action"] != "delete":
            raise UndoError("Der Eintrag wurde danach noch einmal geändert – bitte zuerst die "
                            "neueren Änderungen rückgängig machen.")
        with conn:
            if c["action"] == "update":
                old = {k: v for k, v in json.loads(c["old_json"]).items() if k in cols and k != "id"}
                conn.execute(f"UPDATE {t} SET " + ", ".join(f"{k} = ?" for k in old) + " WHERE id = ?",
                             [*old.values(), c["row_id"]])
            elif c["action"] == "insert":
                conn.execute(f"DELETE FROM {t} WHERE id = ?", (c["row_id"],))
            else:
                if conn.execute(f"SELECT 1 FROM {t} WHERE id = ?", (c["row_id"],)).fetchone():
                    raise UndoError("Ein Eintrag mit dieser Nummer existiert bereits.")
                old = {k: v for k, v in json.loads(c["old_json"]).items() if k in cols}
                conn.execute(f"INSERT INTO {t} (" + ", ".join(old) + ") VALUES ("
                             + ", ".join("?" for _ in old) + ")", list(old.values()))
            new_id = conn.execute("SELECT MAX(id) FROM changelog").fetchone()[0]
            if new_id == change_id:
                raise UndoError("Es gab nichts zurückzusetzen.")
            conn.execute("UPDATE changelog SET undone_by = ? WHERE id = ?", (new_id, change_id))
        return new_id
    except sqlite3.IntegrityError as exc:
        raise UndoError(f"Rückgängig nicht möglich (abhängige Daten fehlen): {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hilfsfunktion: partielle UPDATEs mit Spalten-Whitelist (None ⇒ SQL-NULL)
# ---------------------------------------------------------------------------

# Erlaubte Spalten je Tabelle für partielle UPDATEs (None ⇒ SQL-NULL)
_UPDATE_COLS = {
    "projects":     {"name", "note"},
    "sites":        {"name", "address", "status", "note", "project_id"},
    "trades":       {"name"},
    "tasks":        {"title", "trade_id", "status", "description"},
    "time_entries": {"person", "person_id", "hours", "hourly_rate", "billed", "paid", "work_date",
                     "travel_cost", "paid_amount"},
    "materials":    {"name", "qty", "unit", "unit_price", "billed", "paid",
                     "paid_amount", "store", "purchase_date"},
    "documents":    {"stored_path", "doc_type", "paperless_id", "paperless_url", "receipt_id"},
    "receipts":     {"store", "purchase_date", "total"},
    "persons":      {"name", "hourly_rate", "active", "note"},
}


def _validate_update_fields(table: str, fields: dict) -> None:
    """Wirft ValueError, falls fields Spalten enthält, die für table nicht erlaubt sind."""
    unknown = set(fields) - _UPDATE_COLS[table]
    if unknown:
        raise ValueError(f"Unbekannte Felder für {table}: {', '.join(sorted(unknown))}")


def _update_row_in_conn(conn: sqlite3.Connection, table: str, row_id: int, fields: dict) -> bool:
    """
    Wie _update_row, aber auf einer bereits offenen Connection.

    Öffnet/committet/schließt die Connection NICHT selbst — das obliegt dem Aufrufer.
    Dient dazu, mehrere UPDATEs (z. B. persons + der time_entries-Rename-Sync)
    atomar in einer gemeinsamen Transaktion auszuführen.
    """
    _validate_update_fields(table, fields)
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(
        f"UPDATE {table} SET {set_clause} WHERE id = ?",
        list(fields.values()) + [row_id],
    )
    return cur.rowcount > 0


def _update_row(table: str, row_id: int, fields: dict) -> bool:
    """Partielles UPDATE: schreibt genau die übergebenen Keys (None ⇒ NULL)."""
    _validate_update_fields(table, fields)
    if not fields:
        return False
    conn = get_conn()
    with conn:
        changed = _update_row_in_conn(conn, table, row_id, fields)
    conn.close()
    return changed


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
    """Gibt alle Projekte inkl. site_count und total_cost (Material + Personal) zurück."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.*,
               (SELECT COUNT(*) FROM sites s WHERE s.project_id = p.id) AS site_count,
               ROUND(COALESCE((SELECT SUM(ROUND(m.qty * m.unit_price, 2))
                         FROM materials m JOIN tasks t ON m.task_id = t.id
                         JOIN sites s ON t.site_id = s.id WHERE s.project_id = p.id), 0)
             + COALESCE((SELECT SUM(ROUND(te.hours * te.hourly_rate + te.travel_cost, 2))
                         FROM time_entries te JOIN tasks t ON te.task_id = t.id
                         JOIN sites s ON t.site_id = s.id WHERE s.project_id = p.id), 0), 2)
               AS total_cost
        FROM projects p
        ORDER BY p.created_at DESC
    """).fetchall()
    conn.close()
    return rows


def get_project(project_id: int) -> sqlite3.Row | None:
    """Gibt ein Projekt anhand seiner ID zurück oder None, falls nicht gefunden."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    return row


def update_project(project_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder eines Projekts (name, note); None leert das Feld."""
    return _update_row("projects", project_id, fields)


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
    """Gibt Baustellen inkl. total_cost und open_task_count zurück (Filter: project_id)."""
    conn = get_conn()
    where, params = "", []
    if filters and "project_id" in filters:
        where, params = "WHERE s.project_id = ?", [filters["project_id"]]
    rows = conn.execute(f"""
        SELECT s.*,
               ROUND(COALESCE((SELECT SUM(ROUND(m.qty * m.unit_price, 2))
                         FROM materials m JOIN tasks t ON m.task_id = t.id
                         WHERE t.site_id = s.id), 0)
             + COALESCE((SELECT SUM(ROUND(te.hours * te.hourly_rate + te.travel_cost, 2))
                         FROM time_entries te JOIN tasks t ON te.task_id = t.id
                         WHERE t.site_id = s.id), 0), 2) AS total_cost,
               (SELECT COUNT(*) FROM tasks t WHERE t.site_id = s.id
                AND t.status != 'fertig') AS open_task_count
        FROM sites s
        {where}
        ORDER BY s.created_at DESC
    """, params).fetchall()
    conn.close()
    return rows


def get_site(site_id: int) -> sqlite3.Row | None:
    """Gibt eine Baustelle anhand ihrer ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    conn.close()
    return row


def update_site(site_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder einer Baustelle (name, address, status, note, project_id); None leert das Feld."""
    return _update_row("sites", site_id, fields)


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


def list_trades_with_stats(site_id: int) -> list[sqlite3.Row]:
    """Gibt die Gewerke einer Baustelle inkl. task_count und Kostensumme (gesamt) zurück."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT tr.*,
               (SELECT COUNT(*) FROM tasks t WHERE t.trade_id = tr.id) AS task_count,
               ROUND(COALESCE((SELECT SUM(ROUND(m.qty * m.unit_price, 2))
                         FROM materials m JOIN tasks t ON m.task_id = t.id
                         WHERE t.trade_id = tr.id), 0)
             + COALESCE((SELECT SUM(ROUND(te.hours * te.hourly_rate + te.travel_cost, 2))
                         FROM time_entries te JOIN tasks t ON te.task_id = t.id
                         WHERE t.trade_id = tr.id), 0), 2) AS gesamt
        FROM trades tr
        WHERE tr.site_id = ?
        ORDER BY tr.name
    """, (site_id,)).fetchall()
    conn.close()
    return rows


def get_trade(trade_id: int) -> sqlite3.Row | None:
    """Gibt ein Gewerk anhand seiner ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()
    return row


def update_trade(trade_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder eines Gewerks (name); None leert das Feld."""
    return _update_row("trades", trade_id, fields)


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
# CRUD: persons
# ---------------------------------------------------------------------------

class PersonInUseError(Exception):
    """Person hat Zeiteinträge und darf nicht gelöscht werden (nur deaktivieren)."""


def create_person(name: str, hourly_rate: float = 0.0, note: str | None = None) -> int:
    """
    Legt eine Person an und gibt ihre ID zurück.

    :param name: Eindeutiger Name (UNIQUE); Duplikat wirft sqlite3.IntegrityError.
    :param hourly_rate: Standard-Stundensatz in Euro (Vorbefüllung bei Zeiterfassung).
    :param note: Optionale Notiz.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO persons (name, hourly_rate, note, created_at) VALUES (?, ?, ?, ?)",
            (name, hourly_rate, note, _now()),
        )
    conn.close()
    return cur.lastrowid


def list_persons(active_only: bool = False) -> list[sqlite3.Row]:
    """
    Gibt Personen inkl. Aggregaten zurück: entry_count, sum_hours, sum_cost.

    :param active_only: True = nur Personen mit active=1.
    """
    conn = get_conn()
    where = "WHERE p.active = 1" if active_only else ""
    rows = conn.execute(f"""
        SELECT p.*,
               COUNT(te.id)                              AS entry_count,
               ROUND(COALESCE(SUM(te.hours), 0), 2)      AS sum_hours,
               ROUND(COALESCE(SUM(ROUND(te.hours * te.hourly_rate + te.travel_cost, 2)), 0), 2) AS sum_cost
        FROM persons p
        LEFT JOIN time_entries te ON te.person_id = p.id
        {where}
        GROUP BY p.id
        ORDER BY p.active DESC, p.name
    """).fetchall()
    conn.close()
    return rows


def get_person(person_id: int) -> sqlite3.Row | None:
    """Gibt eine Person anhand ihrer ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
    conn.close()
    return row


def get_person_by_name(name: str) -> sqlite3.Row | None:
    """Gibt eine Person anhand ihres exakten Namens zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM persons WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row


def update_person(person_id: int, fields: dict) -> bool:
    """
    Aktualisiert die übergebenen Felder einer Person (name, hourly_rate, active, note).

    Umbenennen synchronisiert das denormalisierte Feld time_entries.person.

    Beide UPDATEs (persons + ggf. der time_entries-Rename-Sync) laufen in EINER
    gemeinsamen Transaktion auf derselben Connection — atomar, nicht zwei
    unabhängige Verbindungen.
    """
    _validate_update_fields("persons", fields)
    if not fields:
        return False
    conn = get_conn()
    with conn:
        changed = _update_row_in_conn(conn, "persons", person_id, fields)
        if changed and fields.get("name"):
            conn.execute(
                "UPDATE time_entries SET person = ? WHERE person_id = ?",
                (fields["name"], person_id),
            )
    conn.close()
    return changed


def delete_person(person_id: int) -> bool:
    """
    Löscht eine Person ohne Zeiteinträge. Mit Zeiteinträgen: PersonInUseError
    (stattdessen per update_person(id, {"active": 0}) deaktivieren).
    """
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM time_entries WHERE person_id = ?", (person_id,)
    ).fetchone()[0]
    if n > 0:
        conn.close()
        raise PersonInUseError(
            f"Person hat {n} Zeiteintrag/-einträge — bitte deaktivieren statt löschen."
        )
    with conn:
        cur = conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
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
                where_parts.append(f"t.{key} = ?")
                params.append(filters[key])
    sql = """
        SELECT t.*,
               ROUND(COALESCE((SELECT SUM(ROUND(m.qty * m.unit_price, 2)) FROM materials m
                         WHERE m.task_id = t.id), 0)
             + COALESCE((SELECT SUM(ROUND(te.hours * te.hourly_rate + te.travel_cost, 2)) FROM time_entries te
                         WHERE te.task_id = t.id), 0), 2) AS total_cost
        FROM tasks t
    """
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY t.created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def get_task(task_id: int) -> sqlite3.Row | None:
    """Gibt eine Aufgabe anhand ihrer ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return row


def update_task(task_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder einer Aufgabe (title, trade_id, status, description); None leert das Feld."""
    return _update_row("tasks", task_id, fields)


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
    person_id: int | None = None,
    paid: int = 0,
    travel_cost: float = 0.0,
) -> int:
    """
    Legt einen Zeiteintrag (Personalkosten) an.

    Kosten werden serverseitig als hours * hourly_rate + travel_cost berechnet.

    :param task_id: ID der Aufgabe.
    :param person: Name der Person.
    :param hours: Stundenaufwand.
    :param hourly_rate: Stundensatz in Euro.
    :param billed: 1 = mit Rechnung, 0 = ohne Rechnung (Standard: 1).
    :param work_date: Optionales Datum der Leistung (ISO YYYY-MM-DD).
    :param person_id: Optionale Verknüpfung mit einer Person.
    :param travel_cost: Anfahrt in Euro für diesen Arbeitstag (Standard: 0).
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO time_entries "
            "(task_id, person, person_id, hours, hourly_rate, billed, paid, work_date, "
            "travel_cost, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, person, person_id, hours, hourly_rate, billed, paid, work_date,
             travel_cost, _now()),
        )
        eid = cur.lastrowid
        if not paid:
            proj = conn.execute(
                "SELECT s.project_id FROM tasks t JOIN sites s ON s.id = t.site_id "
                "WHERE t.id = ?", (task_id,)
            ).fetchone()
            if proj:
                _guthaben_verrechnen(conn, proj["project_id"], person)
    conn.close()
    return eid


def _guthaben_saldo_conn(conn: sqlite3.Connection, project_id: int, person: str) -> float:
    """Verfügbares Guthaben = Einzahlungen minus bereits verbrauchter Betrag."""
    row = conn.execute(
        "SELECT ROUND(COALESCE((SELECT SUM(g.betrag) FROM guthaben g "
        "WHERE g.project_id = ? AND lower(g.person) = lower(?)), 0) - "
        "COALESCE((SELECT SUM(v.betrag) FROM guthaben_verbrauch v "
        "JOIN guthaben g2 ON g2.id = v.guthaben_id "
        "WHERE g2.project_id = ? AND lower(g2.person) = lower(?)), 0), 2) AS saldo",
        (project_id, person.strip(), project_id, person.strip()),
    ).fetchone()
    return round(row["saldo"] or 0.0, 2)


def _guthaben_verrechnen(conn: sqlite3.Connection, project_id: int, person: str) -> float:
    """
    Verrechnet vorhandenes Guthaben automatisch mit offenen Stunden der Person
    (älteste Arbeitstage zuerst). Voll gedeckte Einträge werden 'bezahlt',
    der letzte ggf. teilweise (paid_amount). Gibt den verrechneten Betrag zurück.
    Läuft auf einer offenen Connection (Aufrufer committet).
    """
    saldo = _guthaben_saldo_conn(conn, project_id, person)
    if saldo <= 0.004:
        return 0.0
    offene = conn.execute(
        "SELECT te.* FROM time_entries te "
        "JOIN tasks t ON t.id = te.task_id JOIN sites s ON s.id = t.site_id "
        "WHERE s.project_id = ? AND lower(te.person) = lower(?) AND te.paid = 0 "
        "ORDER BY COALESCE(te.work_date, substr(te.created_at, 1, 10)), te.id",
        (project_id, person.strip()),
    ).fetchall()
    verrechnet = 0.0
    for r in offene:
        if saldo <= 0.004:
            break
        betrag = round(r["hours"] * r["hourly_rate"] + (r["travel_cost"] or 0), 2)
        offen = round(betrag - (r["paid_amount"] or 0), 2)
        take = round(min(saldo, offen), 2)
        if take <= 0.004:
            continue
        # Guthaben-Töpfe FIFO abbuchen
        rest = take
        tops = conn.execute(
            "SELECT g.id, g.betrag - COALESCE((SELECT SUM(v.betrag) FROM guthaben_verbrauch v "
            "WHERE v.guthaben_id = g.id), 0) AS frei "
            "FROM guthaben g WHERE g.project_id = ? AND lower(g.person) = lower(?) "
            "ORDER BY g.created_at, g.id",
            (project_id, person.strip()),
        ).fetchall()
        for top in tops:
            if rest <= 0.004:
                break
            frei = round(top["frei"], 2)
            if frei <= 0.004:
                continue
            abbuchen = round(min(rest, frei), 2)
            conn.execute(
                "INSERT INTO guthaben_verbrauch (guthaben_id, time_entry_id, betrag, created_at) "
                "VALUES (?, ?, ?, ?)",
                (top["id"], r["id"], abbuchen, _now()),
            )
            rest = round(rest - abbuchen, 2)
        neu = round((r["paid_amount"] or 0) + take, 2)
        voll = neu >= betrag - 0.004
        conn.execute(
            "UPDATE time_entries SET paid = ?, paid_amount = ? WHERE id = ?",
            (1 if voll else 0, 0 if voll else neu, r["id"]),
        )
        saldo = round(saldo - take, 2)
        verrechnet = round(verrechnet + take, 2)
    return verrechnet


def guthaben_einzahlen(project_id: int, person: str, betrag: float, notiz: str | None = None) -> dict:
    """
    Legt ein Guthaben (Vorauszahlung) für eine Person im Projekt an und verrechnet
    es sofort mit offenen Stunden (älteste zuerst). Restguthaben bleibt erhalten
    und wird bei künftigen Stunden automatisch verbraucht.
    Rückgabe: {"verrechnet": float, "saldo": float}
    """
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO guthaben (project_id, person, betrag, notiz, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, person.strip(), round(betrag, 2), notiz, _now()),
            )
            verrechnet = _guthaben_verrechnen(conn, project_id, person)
            saldo = _guthaben_saldo_conn(conn, project_id, person)
        return {"verrechnet": verrechnet, "saldo": saldo}
    finally:
        conn.close()


def guthaben_saldo(project_id: int, person: str) -> dict:
    """Aktuelles Guthaben einer Person im Projekt plus Einzahlungen."""
    conn = get_conn()
    try:
        saldo = _guthaben_saldo_conn(conn, project_id, person)
        posten = conn.execute(
            "SELECT id, betrag, notiz, created_at FROM guthaben "
            "WHERE project_id = ? AND lower(person) = lower(?) ORDER BY created_at, id",
            (project_id, person.strip()),
        ).fetchall()
        return {"saldo": saldo, "einzahlungen": [dict(p) for p in posten]}
    finally:
        conn.close()


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


def update_time_entry(entry_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder eines Zeiteintrags (person, person_id, hours, hourly_rate, billed, work_date); None leert das Feld."""
    return _update_row("time_entries", entry_id, fields)


def person_auszahlung(project_id: int, person: str, amount: float) -> dict:
    """
    Verbucht eine (Teil-)Auszahlung an eine Person: Der Betrag wird auf die offenen
    Stunden-Einträge der Person im Projekt verteilt — älteste Arbeitstage zuerst.
    Voll gedeckte Einträge werden 'bezahlt', der letzte ggf. nur teilweise (paid_amount).

    Alles in EINER Transaktion. Rückgabe:
    {"verteilt": [{id, work_date, betrag, bisher, neu_gezahlt, bezahlt}], "rest_offen": float}
    oder {"fehler": "..."}.
    """
    conn = get_conn()
    try:
        with conn:
            rows = conn.execute(
                "SELECT te.* FROM time_entries te "
                "JOIN tasks t ON t.id = te.task_id JOIN sites s ON s.id = t.site_id "
                "WHERE s.project_id = ? AND lower(te.person) = lower(?) AND te.paid = 0 "
                "ORDER BY COALESCE(te.work_date, substr(te.created_at, 1, 10)), te.id",
                (project_id, person.strip()),
            ).fetchall()
            if not rows:
                return {"fehler": f"Keine offenen Stunden von '{person}' in Projekt {project_id}."}
            posten = []
            for r in rows:
                betrag = round(r["hours"] * r["hourly_rate"] + (r["travel_cost"] or 0), 2)
                offen = round(betrag - (r["paid_amount"] or 0), 2)
                posten.append((r, betrag, offen))
            gesamt_offen = round(sum(p[2] for p in posten), 2)
            amount = round(amount, 2)
            if amount > gesamt_offen + 0.004:
                return {"fehler": f"Auszahlung {amount:.2f} € ist höher als der offene Betrag "
                                  f"von {person}: {gesamt_offen:.2f} €."}
            rest = amount
            verteilt = []
            for r, betrag, offen in posten:
                if rest <= 0.004:
                    break
                anteil = round(min(rest, offen), 2)
                neu = round((r["paid_amount"] or 0) + anteil, 2)
                voll = neu >= betrag - 0.004
                conn.execute(
                    "UPDATE time_entries SET paid = ?, paid_amount = ? WHERE id = ?",
                    (1 if voll else 0, 0 if voll else neu, r["id"]),
                )
                verteilt.append({"id": r["id"], "work_date": r["work_date"], "betrag": betrag,
                                 "bisher": round(r["paid_amount"] or 0, 2), "anteil": anteil,
                                 "bezahlt": voll, "gezahlt_gesamt": betrag if voll else neu})
                rest = round(rest - anteil, 2)
            return {"verteilt": verteilt, "rest_offen": round(gesamt_offen - amount, 2)}
    finally:
        conn.close()


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
    paid: int = 0,
    store: str | None = None,
    purchase_date: str | None = None,
    receipt_id: int | None = None,
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
    :param store: Markt/Händler (z. B. 'Hornbach'); optional.
    :param purchase_date: Kaufdatum YYYY-MM-DD; optional.
    :param receipt_id: Bon, zu dem die Position gehört; optional.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO materials "
            "(task_id, name, qty, unit, unit_price, billed, paid, store, purchase_date, "
            " receipt_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, name, qty, unit, unit_price, billed, paid, store, purchase_date,
             receipt_id, _now()),
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


def update_material(material_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder einer Materialposition (name, qty, unit, unit_price, billed); None leert das Feld."""
    return _update_row("materials", material_id, fields)


def delete_material(material_id: int) -> bool:
    """Löscht eine Materialposition (kaskadiert zu verknüpften Dokumenten)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM materials WHERE id = ?", (material_id,))
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD: receipts (Kassenbons)
# ---------------------------------------------------------------------------

def create_receipt_with_materials(
    task_id: int,
    store: str,
    purchase_date: str,
    total: float,
    positions: list[dict],
    billed: int = 1,
    paid: int = 0,
) -> tuple[int, list[int]]:
    """
    Legt einen Bon samt allen Positionen in EINER Transaktion an.

    positions: Liste von {name, qty, unit_price, unit?}. Entweder alles oder nichts.
    Gibt (receipt_id, [material_id, …]) zurück.
    """
    conn = get_conn()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO receipts (task_id, store, purchase_date, total, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, store, purchase_date, total, _now()),
            )
            rid = cur.lastrowid
            ids = []
            for p in positions:
                c = conn.execute(
                    "INSERT INTO materials (task_id, name, qty, unit, unit_price, billed, paid, "
                    "store, purchase_date, receipt_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, p["name"], p["qty"], p.get("unit"), p["unit_price"], billed, paid,
                     store, purchase_date, rid, _now()),
                )
                ids.append(c.lastrowid)
    finally:
        conn.close()
    return rid, ids


def get_receipt(receipt_id: int) -> sqlite3.Row | None:
    """Gibt einen Bon anhand seiner ID zurück oder None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    conn.close()
    return row


def list_receipts(filters: dict | None = None) -> list[sqlite3.Row]:
    """
    Gibt Bons zurück (neueste Kaufdaten zuerst).

    :param filters: optional task_id, site_id, project_id, store (Teilstring, ohne Groß-/Kleinschreibung).
    """
    conn = get_conn()
    where, params = [], []
    f = filters or {}
    if f.get("task_id") is not None:
        where.append("r.task_id = ?"); params.append(f["task_id"])
    if f.get("site_id") is not None:
        where.append("t.site_id = ?"); params.append(f["site_id"])
    if f.get("project_id") is not None:
        where.append("t.site_id IN (SELECT id FROM sites WHERE project_id = ?)")
        params.append(f["project_id"])
    if f.get("store"):
        where.append("LOWER(r.store) LIKE ?"); params.append(f"%{str(f['store']).lower()}%")
    sql = ("SELECT r.*, t.site_id AS site_id FROM receipts r JOIN tasks t ON t.id = r.task_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.purchase_date DESC, r.id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def receipt_materials(receipt_id: int) -> list[sqlite3.Row]:
    """Positionen eines Bons in Erfassungsreihenfolge."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM materials WHERE receipt_id = ? ORDER BY id",
                        (receipt_id,)).fetchall()
    conn.close()
    return rows


def delete_receipt(receipt_id: int) -> bool:
    """Löscht einen Bon (kaskadiert zu Positionen und Bon-Dokumenten)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.close()
    return cur.rowcount > 0


def receipt_document_paths(receipt_id: int) -> list[str]:
    """Dateipfade der Bon-Dokumente — VOR delete_receipt aufrufen."""
    conn = get_conn()
    rows = conn.execute("SELECT stored_path FROM documents WHERE receipt_id = ?",
                        (receipt_id,)).fetchall()
    conn.close()
    return [r["stored_path"] for r in rows if r["stored_path"]]


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
    receipt_id: int | None = None,
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
            " doc_type, paperless_id, paperless_url, receipt_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, material_id, site_id, filename, stored_path, mime,
             doc_type, paperless_id, paperless_url, receipt_id, _now()),
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
        for key in ("task_id", "material_id", "site_id", "doc_type", "receipt_id"):
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


def update_document(doc_id: int, fields: dict) -> bool:
    """Aktualisiert die übergebenen Felder eines Dokuments (stored_path, doc_type, paperless_id, paperless_url); None leert das Feld."""
    return _update_row("documents", doc_id, fields)


def delete_document(doc_id: int) -> bool:
    """Löscht den Datenbankeintrag eines Dokuments (Datei auf Disk separat entfernen)."""
    conn = get_conn()
    with conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.close()
    return cur.rowcount > 0


def document_paths_for_scope(scope: str, obj_id: int) -> list[str]:
    """
    Sammelt die Datei-Pfade aller Dokumente, die beim Löschen des Objekts
    kaskadiert entfernt werden — VOR dem DELETE aufrufen.

    :param scope: 'project', 'site', 'task' oder 'material'.
    :param obj_id: ID des zu löschenden Objekts.
    """
    queries = {
        "task": ("SELECT stored_path FROM documents WHERE task_id = ? "
                 "OR material_id IN (SELECT id FROM materials WHERE task_id = ?)", 2),
        "material": ("SELECT stored_path FROM documents WHERE material_id = ?", 1),
        "site": ("""SELECT stored_path FROM documents WHERE site_id = ?
                    OR task_id IN (SELECT id FROM tasks WHERE site_id = ?)
                    OR material_id IN (SELECT id FROM materials WHERE task_id IN
                        (SELECT id FROM tasks WHERE site_id = ?))""", 3),
        "project": ("""SELECT stored_path FROM documents
                    WHERE site_id IN (SELECT id FROM sites WHERE project_id = ?)
                    OR task_id IN (SELECT id FROM tasks WHERE site_id IN
                        (SELECT id FROM sites WHERE project_id = ?))
                    OR material_id IN (SELECT id FROM materials WHERE task_id IN
                        (SELECT id FROM tasks WHERE site_id IN
                            (SELECT id FROM sites WHERE project_id = ?)))""", 3),
    }
    sql, n = queries[scope]
    conn = get_conn()
    rows = conn.execute(sql, [obj_id] * n).fetchall()
    conn.close()
    return [r["stored_path"] for r in rows if r["stored_path"]]


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
            ROUND(COALESCE(SUM(CASE WHEN m.billed = 1 THEN ROUND(m.qty * m.unit_price, 2) ELSE 0 END), 0), 2) AS mat_mit,
            ROUND(COALESCE(SUM(CASE WHEN m.billed = 0 THEN ROUND(m.qty * m.unit_price, 2) ELSE 0 END), 0), 2) AS mat_ohne
        FROM materials m
        JOIN tasks t ON m.task_id = t.id
        WHERE {site_filter}
    """
    row = conn.execute(mat_sql, params).fetchone()
    if row:
        result["mat_mit"] = row["mat_mit"]
        result["mat_ohne"] = row["mat_ohne"]
    result["mat_gesamt"] = round(result["mat_mit"] + result["mat_ohne"], 2)

    # --- Personal-Summen ---
    pers_sql = f"""
        SELECT
            ROUND(COALESCE(SUM(CASE WHEN te.billed = 1 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 0), 2) AS pers_mit,
            ROUND(COALESCE(SUM(CASE WHEN te.billed = 0 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 0), 2) AS pers_ohne
        FROM time_entries te
        JOIN tasks t ON te.task_id = t.id
        WHERE {site_filter}
    """
    row = conn.execute(pers_sql, params).fetchone()
    if row:
        result["pers_mit"] = row["pers_mit"]
        result["pers_ohne"] = row["pers_ohne"]
    result["pers_gesamt"] = round(result["pers_mit"] + result["pers_ohne"], 2)

    result["gesamt_mit"] = round(result["mat_mit"] + result["pers_mit"], 2)
    result["gesamt_ohne"] = round(result["mat_ohne"] + result["pers_ohne"], 2)
    result["gesamt"] = round(result["gesamt_mit"] + result["gesamt_ohne"], 2)

    # --- Aufschlüsselung je Gewerk ---
    # Material je Gewerk
    trade_mat_sql = f"""
        SELECT
            t.trade_id,
            tr.name AS trade_name,
            ROUND(COALESCE(SUM(CASE WHEN m.billed = 1 THEN ROUND(m.qty * m.unit_price, 2) ELSE 0 END), 0), 2) AS mat_mit,
            ROUND(COALESCE(SUM(CASE WHEN m.billed = 0 THEN ROUND(m.qty * m.unit_price, 2) ELSE 0 END), 0), 2) AS mat_ohne
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
            ROUND(COALESCE(SUM(CASE WHEN te.billed = 1 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 0), 2) AS pers_mit,
            ROUND(COALESCE(SUM(CASE WHEN te.billed = 0 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 0), 2) AS pers_ohne
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
            "mat_gesamt": round(mm + mo, 2),
            "pers_mit": pm,
            "pers_ohne": po,
            "pers_gesamt": round(pm + po, 2),
            "gesamt_mit": round(mm + pm, 2),
            "gesamt_ohne": round(mo + po, 2),
            "gesamt": round(mm + mo + pm + po, 2),
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


def dashboard_data(project_id: int | None = None, site_id: int | None = None) -> dict:
    """
    Liefert alle Dashboard-Daten in einem Aufruf: KPIs, Aufschlüsselung je
    Gewerk/Baustelle/Person und die letzten 10 Erfassungen.

    :param project_id: Optionaler Projekt-Filter.
    :param site_id: Optionaler Baustellen-Filter (übersteuert project_id nicht — beide werden angewandt).
    """
    if site_id is not None:
        scope, params = "t.site_id = ?", [site_id]
    elif project_id is not None:
        scope, params = "t.site_id IN (SELECT id FROM sites WHERE project_id = ?)", [project_id]
    else:
        scope, params = "1 = 1", []

    conn = get_conn()
    agg = _aggregate_costs(conn, scope, params)

    kpis = {k: agg[k] for k in ("mat_mit", "mat_ohne", "pers_mit", "pers_ohne",
                                "gesamt_mit", "gesamt_ohne", "gesamt")}
    kpis["offene_aufgaben"] = conn.execute(
        f"SELECT COUNT(*) FROM tasks t WHERE {scope} AND t.status != 'fertig'", params
    ).fetchone()[0]
    if site_id is not None:
        kpis["aktive_baustellen"] = conn.execute(
            "SELECT COUNT(*) FROM sites WHERE id = ? AND status = 'aktiv'", (site_id,)
        ).fetchone()[0]
    elif project_id is not None:
        kpis["aktive_baustellen"] = conn.execute(
            "SELECT COUNT(*) FROM sites WHERE project_id = ? AND status = 'aktiv'", (project_id,)
        ).fetchone()[0]
    else:
        kpis["aktive_baustellen"] = conn.execute(
            "SELECT COUNT(*) FROM sites WHERE status = 'aktiv'"
        ).fetchone()[0]

    by_site = [dict(r) for r in conn.execute(f"""
        SELECT s.id AS site_id, s.name AS site_name,
               ROUND(COALESCE(mat.mit, 0) + COALESCE(pers.mit, 0), 2)  AS gesamt_mit,
               ROUND(COALESCE(mat.ohne, 0) + COALESCE(pers.ohne, 0), 2) AS gesamt_ohne,
               ROUND(COALESCE(mat.mit, 0) + COALESCE(mat.ohne, 0)
             + COALESCE(pers.mit, 0) + COALESCE(pers.ohne, 0), 2) AS gesamt,
               (SELECT COUNT(*) FROM tasks t2 WHERE t2.site_id = s.id
                AND t2.status != 'fertig') AS offene_aufgaben
        FROM sites s
        LEFT JOIN (SELECT t.site_id sid,
                          SUM(CASE WHEN m.billed = 1 THEN ROUND(m.qty * m.unit_price, 2) ELSE 0 END) mit,
                          SUM(CASE WHEN m.billed = 0 THEN ROUND(m.qty * m.unit_price, 2) ELSE 0 END) ohne
                   FROM materials m JOIN tasks t ON m.task_id = t.id GROUP BY t.site_id) mat
               ON mat.sid = s.id
        LEFT JOIN (SELECT t.site_id sid,
                          ROUND(SUM(CASE WHEN te.billed = 1 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 2) mit,
                          ROUND(SUM(CASE WHEN te.billed = 0 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 2) ohne
                   FROM time_entries te JOIN tasks t ON te.task_id = t.id GROUP BY t.site_id) pers
               ON pers.sid = s.id
        WHERE (? IS NULL OR s.project_id = ?) AND (? IS NULL OR s.id = ?)
        ORDER BY gesamt DESC
    """, [project_id, project_id, site_id, site_id]).fetchall()]

    by_person = [dict(r) for r in conn.execute(f"""
        SELECT te.person_id,
               COALESCE(p.name, te.person) AS person_name,
               ROUND(SUM(te.hours), 2) AS stunden,
               ROUND(SUM(ROUND(te.hours * te.hourly_rate + te.travel_cost, 2)), 2) AS kosten,
               ROUND(SUM(CASE WHEN te.billed = 1 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 2) AS kosten_mit,
               ROUND(SUM(CASE WHEN te.billed = 0 THEN ROUND(te.hours * te.hourly_rate + te.travel_cost, 2) ELSE 0 END), 2) AS kosten_ohne
        FROM time_entries te
        JOIN tasks t ON te.task_id = t.id
        LEFT JOIN persons p ON te.person_id = p.id
        WHERE {scope}
        GROUP BY te.person_id, COALESCE(p.name, te.person)
        ORDER BY kosten DESC
    """, params).fetchall()]

    recent = [dict(r) for r in conn.execute(f"""
        SELECT * FROM (
            SELECT 'zeit' AS typ, te.person || ' · ' || te.hours || ' h' AS label,
                   te.hours * te.hourly_rate + te.travel_cost AS betrag, te.created_at AS datum,
                   t.id AS task_id, t.title AS task_title
            FROM time_entries te JOIN tasks t ON te.task_id = t.id WHERE {scope}
            UNION ALL
            SELECT 'material', m.name, m.qty * m.unit_price, m.created_at, t.id, t.title
            FROM materials m JOIN tasks t ON m.task_id = t.id WHERE {scope}
            UNION ALL
            SELECT 'dokument', d.filename, NULL, d.created_at, t.id, t.title
            FROM documents d JOIN tasks t ON d.task_id = t.id WHERE {scope}
        )
        ORDER BY datum DESC
        LIMIT 10
    """, params * 3).fetchall()]

    conn.close()
    return {"kpis": kpis, "by_trade": agg["by_trade"], "by_site": by_site,
            "by_person": by_person, "recent": recent}
