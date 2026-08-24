# baulog — Implementierungsplan

Internes Webtool zur **Projekt-, Baustellen- & Kosten-Verwaltung**.
Läuft als Flask-App im lokalen Netz auf einem Raspberry Pi
(`http://192.168.1.134:PORT`, Port aus `TOOL_PORT`).

Erstes Projekt: **Umbau Mehrfamilienhaus** mit Baustelle **Deisterstraße 40, Feggendorf**.

---

## 1. Zweck

baulog dient der internen Erfassung und Auswertung aller Kosten über mehrere
Baustellen hinweg, gebündelt in **Projekten**:

- **Projekte** (oberste Ebene) verwalten — z. B. „Umbau MFH Deisterstraße 40",
  „Neubau Garage". Ein Projekt kann **mehrere Baustellen** enthalten.
- **Baustellen** (konkreter Ort/eigenständiger Kostenbereich) innerhalb eines Projekts.
- **Gewerke** je Baustelle (Maurer, Elektro, Sanitär, Trockenbau, Dach, Fenster …).
- **Aufgaben** je Gewerk anlegen (z. B. „Wand ziehen") mit Status und Beschreibung.
- **Zeiterfassung** (Stunden × Stundensatz → Personalkosten) und **Materialerfassung**
  (Menge × Einzelpreis → Materialkosten) je Aufgabe.
- Jede Kostenposition (Personal **und** Material) trägt ein **Rechnungs-Flag**
  („mit Rechnung" / „ohne Rechnung") zur Trennung dokumentierter (versteuerbarer)
  von undokumentierten Kosten.
- **Dashboard/Kostenübersicht**: Summen Material + Personal, getrennt nach
  „mit Rechnung"/„ohne Rechnung", pro Baustelle, pro Projekt und gesamt.
- **Dokumente & Fotos**: Upload von Rechnungen, Belegen und Fotos zu Aufgaben oder
  Materialpositionen mit Vorschau.
- **Paperless-Integration (optional)**: Belege an Paperless-ngx durchreichen und Link anzeigen.

Alle Daten liegen lokal (SQLite + Datei-Uploads). Keine externen Dienste außer optional Paperless.
Keine API-Keys im Frontend.

---

## 2. Dateiliste

```
baulog/
├── PLAN.md              # dieses Dokument
├── META.json            # { title, description, icon } — Werkzeugkisten-Kachel
├── server.py            # Flask-App: Routing, REST-API, DB-Zugriff, Uploads, Paperless
├── index.html           # Single-Page-UI (Tailwind via CDN, mobile-first, Vanilla JS)
├── requirements.txt     # flask, requests (für Paperless)
├── .gitignore           # data/*.db, uploads/, config.local.json
├── config.example.json  # Vorlage: Paperless-URL/-Token (NICHT eingecheckt: config.local.json)
└── data/
    ├── baulog.db        # SQLite-Datenbank (wird beim Start angelegt)
    └── uploads/         # abgelegte Dateien: <doc_id>_<originalname>
```

**MCP-Server (separat, in `~/hermes-tools/mcp/`):** `baulog_mcp.py` — dünner
HTTP-Client auf die REST-API (gleiches Muster wie `rechnung_mcp.py`), damit Hermes
**alle** Operationen direkt aus dem Chat steuern kann. Details in Abschnitt 9.

**Konventionen** (aus Vorlage `server.py`):
- `app = Flask(__name__, static_folder=".")`
- Port aus `os.environ.get("TOOL_PORT", "5000")`, `host="0.0.0.0"`.
- `data/` für persistente Daten; DB und Uploads dort.
- Paperless-Zugang ausschließlich serverseitig aus `config.local.json` /
  Umgebungsvariablen (`PAPERLESS_URL`, `PAPERLESS_TOKEN`) — niemals im Frontend.

---

## 3. UI-Skizze (Seiten / Ansichten + Navigation)

Single-Page-App mit Hash-Routing. Persistente **Top-Bar** mit Titel,
darunter **Tab-Navigation**. Mobile-first: ab 320px bedienbar, Touch-Ziele ≥ 44px,
Tabellen werden auf schmalen Screens als **Card-List** dargestellt
(`hidden sm:table` / `sm:hidden` Cards).

### Navigation (Bottom-Nav auf Mobile, Top-Tabs ab `sm`)
```
[ Dashboard ] [ Projekte ] [ Baustellen ] [ Gewerke ] [ Aufgaben ] [ Dokumente ]
```

### 3.1 Dashboard (`#/`)
- **Auswahl Projekt** (oder „Alle Projekte") und **Auswahl Baustelle** (oder „Alle
  Baustellen des Projekts").
- Kachel-Karten mit Summen:
  - **Material** — mit Rechnung / ohne Rechnung / Gesamt
  - **Personal** — mit Rechnung / ohne Rechnung / Gesamt
  - **Gesamtkosten** — mit Rechnung / ohne Rechnung / Summe
- Balken/Tabelle je Gewerk (Kostenverteilung) und je Baustelle.
- Umschalter „Baustelle" ↔ „Projekt" ↔ „gesamt (alle)".

### 3.2 Projekte (`#/projekte`)
- Liste aller Projekte (Name, Notiz, Anzahl Baustellen, Gesamtkosten).
- Button „+ Neues Projekt" → Formular (Name, Notiz).
- **Projekt-Detail** (`#/projekt/<pid>`): zeigt die Baustellen des Projekts,
  „+ Neue Baustelle" (Name, Adresse, Notiz).

### 3.3 Baustellen (`#/baustellen`)
- Liste aller Baustellen (Name, Adresse, zugehöriges Projekt, Status, Gesamtkosten).
- Filter nach Projekt.
- Button „+ Neue Baustelle" → Formular (Name, Adresse, Projekt, Notiz).
- **Baustellen-Detail** (`#/baustelle/<sid>`): Gewerke der Baustelle verwalten.

### 3.4 Gewerke (`#/baustelle/<sid>/gewerke`)
- Liste der Gewerke der aktiven Baustelle (Name, #Aufgaben, Kostensumme).
- „+ Gewerk hinzufügen" (Name frei oder aus Vorschlagsliste:
  Maurer, Elektro, Sanitär, Trockenbau, Dach, Fenster).

### 3.5 Aufgaben (`#/baustelle/<sid>/aufgaben`)
- Gefiltert nach Gewerk + Status (offen / in Arbeit / fertig).
- Card-List (Mobile) / Tabelle (Desktop): Titel, Gewerk, Status-Badge, Kostensumme.
- „+ Neue Aufgabe" → Titel, Gewerk, Status, Beschreibung.
- **Aufgaben-Detail** (`#/aufgabe/<id>`):
  - Kopf: Titel, Gewerk, Status (änderbar), Beschreibung.
  - Abschnitt **Zeiten**: Liste (Person, Stunden, Stundensatz, Kosten,
    Rechnungs-Flag). „+ Zeit erfassen".
  - Abschnitt **Material**: Liste (Bezeichnung, Menge, Einheit, Einzelpreis,
    Summe, Rechnungs-Flag). „+ Materialposition".
  - Abschnitt **Dokumente/Fotos**: Thumbnails + „+ Upload"
    (Kamera/Datei), optional „an Paperless senden".
  - Fußzeile: Kostensumme der Aufgabe (mit/ohne Rechnung).

### 3.6 Dokumente (`#/baustelle/<sid>/dokumente`)
- Galerie aller Uploads der Baustelle (Vorschau, Dateiname, verknüpft mit Aufgabe/Material).
- Filter nach Typ (Rechnung / Beleg / Foto).
- Paperless-Link-Badge, wenn synchronisiert.

### Wiederverwendbare UI-Elemente
- **Rechnungs-Flag-Toggle**: Segmented-Control „mit Rechnung / ohne Rechnung" (≥44px).
- **Status-Badge**: farbig (offen=grau, in Arbeit=gelb, fertig=grün).
- **Modal-Formulare** für Anlegen/Bearbeiten.
- **Upload-Feld**: `<input type="file" accept="image/*,.pdf" capture>` für Kamera auf Mobile.

---

## 4. API-Endpunkte (Methoden + Pfade)

Alle Daten-Endpunkte unter `/api`, JSON. Beträge serverseitig berechnet.

### Statisch / App
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/` | liefert `index.html` |

### Projekte (oberste Ebene)
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/projects` | alle Projekte (mit Kosten-Summary) |
| POST | `/api/projects` | Projekt anlegen (name, note) |
| GET | `/api/projects/<pid>` | Projekt-Detail inkl. Baustellen |
| PUT | `/api/projects/<pid>` | Projekt ändern |
| DELETE | `/api/projects/<pid>` | Projekt löschen (kaskadiert → Baustellen → …) |

### Baustellen (innerhalb eines Projekts)
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/sites?project=<pid>` | Baustellen (optional nach Projekt gefiltert) |
| POST | `/api/sites` | Baustelle anlegen (name, address, project_id, note) |
| GET | `/api/sites/<sid>` | Baustellen-Detail inkl. Gewerke |
| PUT | `/api/sites/<sid>` | Baustelle ändern |
| DELETE | `/api/sites/<sid>` | Baustelle löschen (kaskadiert) |

### Gewerke
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/sites/<sid>/trades` | Gewerke der Baustelle |
| POST | `/api/sites/<sid>/trades` | Gewerk anlegen |
| PUT | `/api/trades/<tid>` | Gewerk umbenennen |
| DELETE | `/api/trades/<tid>` | Gewerk löschen |

### Aufgaben
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/sites/<sid>/tasks?trade=<tid>&status=<s>` | Aufgaben (filterbar) |
| POST | `/api/sites/<sid>/tasks` | Aufgabe anlegen |
| GET | `/api/tasks/<taskid>` | Aufgaben-Detail inkl. Zeiten, Material, Dokumente |
| PUT | `/api/tasks/<taskid>` | Aufgabe ändern (Titel/Gewerk/Status/Beschreibung) |
| DELETE | `/api/tasks/<taskid>` | Aufgabe löschen (kaskadiert) |

### Zeiterfassung (Personalkosten)
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/tasks/<taskid>/timeentries` | Zeiteinträge der Aufgabe |
| POST | `/api/tasks/<taskid>/timeentries` | Zeiteintrag anlegen (person, hours, hourly_rate, billed) |
| PUT | `/api/timeentries/<eid>` | Zeiteintrag ändern |
| DELETE | `/api/timeentries/<eid>` | Zeiteintrag löschen |

### Material
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/tasks/<taskid>/materials` | Materialpositionen der Aufgabe |
| POST | `/api/tasks/<taskid>/materials` | Position anlegen (name, qty, unit, unit_price, billed) |
| PUT | `/api/materials/<mid>` | Position ändern |
| DELETE | `/api/materials/<mid>` | Position löschen |

### Dokumente & Fotos
| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/documents` | Upload (multipart: file, task_id\|material_id, doc_type) |
| GET | `/api/documents?task_id=<>` \| `?material_id=<>` \| `?site_id=<>` | Liste |
| GET | `/api/documents/<docid>/file` | Datei/Vorschau ausliefern |
| DELETE | `/api/documents/<docid>` | Dokument + Datei löschen |
| POST | `/api/documents/<docid>/paperless` | optional an Paperless-ngx senden, Link speichern |

### Kostenübersicht / Dashboard
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/sites/<sid>/summary` | Summen je Baustelle (Material/Personal × mit/ohne Rechnung, je Gewerk) |
| GET | `/api/projects/<pid>/summary` | Summen je Projekt (über alle Baustellen) |
| GET | `/api/summary` | Gesamtsummen über alle Projekte |

### Konfiguration (nur Status, keine Secrets)
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/config` | liefert nur `{ paperless_enabled: bool }` (kein Token) |

**Antwortkonvention:** Erfolg `{ "status": "ok", ... }`; Fehler HTTP 4xx/5xx mit
`{ "error": "<meldung>" }`. Uploads: `MAX_CONTENT_LENGTH` gesetzt, Whitelist
`.jpg/.jpeg/.png/.webp/.pdf`, Dateiname via `secure_filename`.

---

## 5. Datenmodell (SQLite-Tabellen)

DB: `data/baulog.db`. `PRAGMA foreign_keys = ON`. Zeitstempel als ISO-8601-Text.
Geldbeträge als `REAL` (Euro). Rechnungs-Flag als `billed INTEGER` (1 = mit Rechnung, 0 = ohne).

### `projects` — Projekte (oberste Ebene)
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| name | TEXT NOT NULL | z. B. „Umbau MFH Deisterstraße 40" |
| note | TEXT | |
| created_at | TEXT NOT NULL | |

### `sites` — Baustellen
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| project_id | INTEGER NOT NULL | FK → projects(id) ON DELETE CASCADE |
| name | TEXT NOT NULL | z. B. „MFH Deisterstraße 40" |
| address | TEXT | „Deisterstraße 40, Feggendorf" |
| status | TEXT DEFAULT 'aktiv' | aktiv / abgeschlossen |
| note | TEXT | |
| created_at | TEXT NOT NULL | |

### `trades` — Gewerke
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| site_id | INTEGER NOT NULL | FK → sites(id) ON DELETE CASCADE |
| name | TEXT NOT NULL | Maurer, Elektro, Sanitär … |
| created_at | TEXT NOT NULL | |

### `tasks` — Aufgaben
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| site_id | INTEGER NOT NULL | FK → sites(id) ON DELETE CASCADE |
| trade_id | INTEGER | FK → trades(id) ON DELETE SET NULL |
| title | TEXT NOT NULL | „Wand ziehen" |
| status | TEXT DEFAULT 'offen' | offen / in Arbeit / fertig |
| description | TEXT | |
| created_at | TEXT NOT NULL | |

### `time_entries` — Zeiterfassung (Personalkosten)
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| task_id | INTEGER NOT NULL | FK → tasks(id) ON DELETE CASCADE |
| person | TEXT NOT NULL | Name der Person |
| hours | REAL NOT NULL | Stundenaufwand |
| hourly_rate | REAL NOT NULL | Stundensatz € |
| billed | INTEGER NOT NULL DEFAULT 1 | 1 = mit Rechnung, 0 = ohne |
| work_date | TEXT | Tag der Leistung |
| created_at | TEXT NOT NULL | |

*Kosten = `hours * hourly_rate` (serverseitig berechnet).*

### `materials` — Materialpositionen
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| task_id | INTEGER NOT NULL | FK → tasks(id) ON DELETE CASCADE |
| name | TEXT NOT NULL | Bezeichnung |
| qty | REAL NOT NULL | Menge |
| unit | TEXT | Einheit (Stk, m², kg, l …) |
| unit_price | REAL NOT NULL | Einzelpreis € |
| billed | INTEGER NOT NULL DEFAULT 1 | 1 = mit Rechnung, 0 = ohne |
| created_at | TEXT NOT NULL | |

*Kosten = `qty * unit_price` (serverseitig berechnet).*

### `documents` — Dokumente & Fotos
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| task_id | INTEGER | FK → tasks(id) ON DELETE CASCADE (nullable) |
| material_id | INTEGER | FK → materials(id) ON DELETE CASCADE (nullable) |
| site_id | INTEGER | FK → sites(id) (Denormalisierung für Galerie-Filter) |
| filename | TEXT NOT NULL | Originaldateiname |
| stored_path | TEXT NOT NULL | Pfad in data/uploads/ |
| mime | TEXT | image/jpeg, application/pdf … |
| doc_type | TEXT | rechnung / beleg / foto |
| paperless_id | INTEGER | gesetzt nach Paperless-Sync (nullable) |
| paperless_url | TEXT | Link zum Paperless-Dokument (nullable) |
| created_at | TEXT NOT NULL | |

**Beziehungen:** `projects 1—n sites`, `sites 1—n trades`, `sites 1—n tasks`,
`trades 1—n tasks`, `tasks 1—n time_entries`, `tasks 1—n materials`,
`documents` hängt an `task` **oder** `material` (mind. eines gesetzt), zusätzlich
`site_id` zur schnellen Galerie-Filterung.

---

## 6. Kostenberechnung (Aggregationslogik)

Für `summary`-Endpunkte je Baustelle, je Projekt bzw. gesamt:

- **Material mit Rechnung** = Σ `qty*unit_price` über `materials` mit `billed=1`
- **Material ohne Rechnung** = Σ `qty*unit_price` über `materials` mit `billed=0`
- **Personal mit Rechnung** = Σ `hours*hourly_rate` über `time_entries` mit `billed=1`
- **Personal ohne Rechnung** = Σ `hours*hourly_rate` über `time_entries` mit `billed=0`
- **Gesamt mit Rechnung** / **Gesamt ohne Rechnung** / **Gesamtsumme** entsprechend.
- Zusätzlich Aufschlüsselung je Gewerk (Join `tasks.trade_id`) und je Baustelle.

---

## 7. Paperless-Integration (optional)

- Konfiguration serverseitig: `PAPERLESS_URL` (`http://192.168.1.134:8081`),
  `PAPERLESS_TOKEN` aus `config.local.json` / Umgebung.
- `POST /api/documents/<id>/paperless`: lädt die gespeicherte Datei per
  `requests.post(PAPERLESS_URL + "/api/documents/post_document/", headers={Authorization: Token …})`
  hoch, speichert Rückgabe-ID/-Link in `documents.paperless_id`/`paperless_url`.
- Frontend zeigt Button nur, wenn `/api/config` → `paperless_enabled=true`.
- **Token niemals ans Frontend** ausliefern.

---

## 8. Test-Checkliste

### Setup / Infrastruktur
- [ ] `python server.py` startet, DB `data/baulog.db` wird angelegt, `data/uploads/` existiert.
- [ ] App unter `http://192.168.1.134:$TOOL_PORT` erreichbar; Port aus `TOOL_PORT`.
- [ ] `GET /` liefert `index.html`; META.json (title/description/icon) gepflegt.

### Projekte & Baustellen & Gewerke
- [ ] Projekt „Umbau MFH Deisterstraße 40" anlegen, bearbeiten, in Liste sichtbar.
- [ ] Baustelle „MFH Deisterstraße 40, Feggendorf" innerhalb des Projekts anlegen.
- [ ] Gewerke (Maurer, Elektro, Sanitär, Trockenbau, Dach, Fenster) anlegen/löschen.
- [ ] Löschen eines Projekts entfernt Baustellen → Gewerke → Aufgaben (Kaskade).

### Aufgaben
- [ ] Aufgabe „Wand ziehen" mit Gewerk „Maurer", Status, Beschreibung anlegen.
- [ ] Status offen → in Arbeit → fertig wechseln; Badge-Farbe korrekt.
- [ ] Filter nach Gewerk und Status funktioniert.

### Zeiterfassung
- [ ] Zeiteintrag (Person, Stunden, Stundensatz) → Kosten = Stunden×Satz korrekt.
- [ ] Rechnungs-Flag mit/ohne Rechnung setzbar und persistent.

### Material
- [ ] Materialposition (Bezeichnung, Menge, Einheit, Einzelpreis) → Summe korrekt.
- [ ] Rechnungs-Flag mit/ohne Rechnung setzbar.

### Kostenübersicht / Dashboard
- [ ] Summen Material + Personal je „mit Rechnung"/„ohne Rechnung" pro Baustelle korrekt.
- [ ] Summen pro Projekt und Gesamtsummen über alle Projekte korrekt.
- [ ] Aufschlüsselung je Gewerk stimmt mit Einzelposten überein.

### Dokumente & Fotos
- [ ] Foto (JPG/PNG) und PDF-Rechnung zu Aufgabe hochladen; Vorschau/Thumbnail sichtbar.
- [ ] Upload zu Materialposition möglich; Galerie-Filter nach Typ funktioniert.
- [ ] Nicht erlaubte Dateitypen werden abgelehnt; Größenlimit greift.
- [ ] Löschen entfernt DB-Eintrag **und** Datei in `data/uploads/`.

### Paperless (optional)
- [ ] Bei aktiver Config: Beleg an Paperless senden, Link erscheint, Doc in Paperless sichtbar.
- [ ] Bei inaktiver Config: Button verborgen; kein Token im Frontend/Netzwerk-Tab.

### Mobile-First / UI
- [ ] Bei 320px Breite voll bedienbar, kein horizontales Scrollen.
- [ ] Touch-Ziele (Buttons, Toggles) ≥ 44px.
- [ ] Tabellen erscheinen auf Mobile als Card-List, ab `sm` als Tabelle.
- [ ] Kamera-Upload (`capture`) auf Smartphone auslösbar.

### Sicherheit / Robustheit
- [ ] Keine API-Keys/Tokens im Frontend oder in `/api/config`.
- [ ] `secure_filename` verhindert Pfad-Traversal bei Uploads.
- [ ] Ungültige IDs → sauberer 404/400 mit `{ "error": ... }`.

---

## 9. MCP-Server (Agenten-Steuerung aus dem Chat)

Zusätzlich zur Web-UI wird ein MCP-Server **`baulog_mcp.py`** gebaut (Datei in
`~/hermes-tools/mcp/`, registriert in `~/.hermes/config.yaml` unter `mcp_servers:`),
damit Hermes **alle** Operationen direkt aus dem Chat steuern kann — anlegen, listen,
ändern, löschen, Summen abrufen, Dokumente hochladen. Der MCP-Server ist ein dünner
HTTP-Client auf die REST-API von `server.py` (gleiche SQLite-DB → **eine** Datenquelle,
kein Zustands-Drift zwischen UI und Chat).

**Konventionen (identisch zu `rechnung_mcp.py`):**
- `BASE_URL = os.environ.get("BAULOG_URL", "http://127.0.0.1:5112")`.
- Nur stdlib (`urllib.request`) — **keine** neue Dependency im hermes-tools venv.
- `from mcp_helper import register_tool, run_server`; jede Funktion wird per
  `register_tool("name", fn)` registriert; `run_server()` als Entrypoint.
- **JEDES Feld** wird über Parametername + Typ + Docstring dokumentiert — das
  MCP-Tool-Schema entsteht automatisch aus `inspect.signature`, daher ist die
  Docstring-Qualität die Dokumentation für den Agenten.
- Mapping `mit_rechnung: bool` (MCP) → `billed: 0/1` (API) passiert im Wrapper.

### Tool-Liste (34 Tools, jedes Feld dokumentiert)

**Projekte**
| Tool | Felder | Zweck |
|---|---|---|
| `projekt_anlegen` | `name` (str, Pflicht), `note` (str, "") | Projekt anlegen |
| `projekte_liste` | — | Alle Projekte + Baustellen-Anzahl + Gesamtkosten |
| `projekt_details` | `projekt_id` (int) | Detail inkl. Baustellen |
| `projekt_bearbeiten` | `projekt_id`, `name`=None, `note`=None | Nur angegebene Felder ändern |
| `projekt_loeschen` | `projekt_id` | Löschen (Kaskade → Baustellen → …) |

**Baustellen**
| Tool | Felder | Zweck |
|---|---|---|
| `baustelle_anlegen` | `name` (Pflicht), `projekt_id` (int, Pflicht), `address`="", `note`="" | Baustelle anlegen |
| `baustellen_liste` | `projekt_id` (int, None) | Baustellen, optional nach Projekt gefiltert |
| `baustelle_details` | `baustelle_id` (int) | Detail inkl. Gewerke |
| `baustelle_bearbeiten` | `baustelle_id`, `name`=None, `address`=None, `status`=None, `note`=None, `projekt_id`=None | ändern |
| `baustelle_loeschen` | `baustelle_id` | Löschen (Kaskade) |

**Gewerke**
| Tool | Felder | Zweck |
|---|---|---|
| `gewerk_anlegen` | `baustelle_id` (int, Pflicht), `name` (str, Pflicht) | Gewerk anlegen (z.B. „Maurer") |
| `gewerke_liste` | `baustelle_id` (int) | Gewerke der Baustelle |
| `gewerk_bearbeiten` | `gewerk_id` (int), `name` (str) | umbenennen |
| `gewerk_loeschen` | `gewerk_id` (int) | löschen |

**Aufgaben**
| Tool | Felder | Zweck |
|---|---|---|
| `aufgabe_anlegen` | `baustelle_id` (Pflicht), `titel` (str, Pflicht), `gewerk_id`=None, `status`="offen", `beschreibung`="" | Aufgabe anlegen |
| `aufgaben_liste` | `baustelle_id`=None, `gewerk_id`=None, `status`=None | filterbar (Gewerk + Status) |
| `aufgabe_details` | `aufgabe_id` (int) | Detail inkl. Zeiten/Material/Dokumente/Kosten |
| `aufgabe_bearbeiten` | `aufgabe_id`, `titel`=None, `gewerk_id`=None, `status`=None, `beschreibung`=None | ändern |
| `aufgabe_loeschen` | `aufgabe_id` | Löschen (Kaskade) |

**Zeiterfassung (Personalkosten)**
| Tool | Felder | Zweck |
|---|---|---|
| `zeiteintrag_anlegen` | `aufgabe_id` (Pflicht), `person` (str, Pflicht), `stunden` (float), `stundensatz` (float, €/h), `mit_rechnung`=True, `datum`="" | Zeit erfassen; Kosten=Stunden×Satz |
| `zeiteintraege_liste` | `aufgabe_id` (int) | Zeiteinträge der Aufgabe |
| `zeiteintrag_bearbeiten` | `zeiteintrag_id`, `person`=None, `stunden`=None, `stundensatz`=None, `mit_rechnung`=None, `datum`=None | ändern |
| `zeiteintrag_loeschen` | `zeiteintrag_id` (int) | löschen |

**Material**
| Tool | Felder | Zweck |
|---|---|---|
| `material_anlegen` | `aufgabe_id` (Pflicht), `bezeichnung` (str, Pflicht), `menge` (float), `einzelpreis` (float, €), `einheit`="Stk", `mit_rechnung`=True | Position; Kosten=Menge×Einzelpreis |
| `materialien_liste` | `aufgabe_id` (int) | Positionen der Aufgabe |
| `material_bearbeiten` | `material_id`, `bezeichnung`=None, `menge`=None, `einheit`=None, `einzelpreis`=None, `mit_rechnung`=None | ändern |
| `material_loeschen` | `material_id` (int) | löschen |

**Kostenübersicht**
| Tool | Felder | Zweck |
|---|---|---|
| `baustelle_summary` | `baustelle_id` (int) | Material/Personal × mit/ohne Rechnung, je Gewerk |
| `projekt_summary` | `projekt_id` (int) | Summen über alle Baustellen des Projekts |
| `gesamt_summary` | — | Summen über alle Projekte |

**Dokumente & Fotos**
| Tool | Felder | Zweck |
|---|---|---|
| `dokument_hochladen` | `datei_pfad` (str, Pflicht), `aufgabe_id`=None, `material_id`=None, `doc_type`="beleg" | lokale Datei (Foto/PDF) als multipart hochladen |
| `dokumente_liste` | `aufgabe_id`=None, `material_id`=None, `baustelle_id`=None, `doc_type`=None | Galerie/Filter |
| `dokument_loeschen` | `dokument_id` (int) | DB-Eintrag + Datei löschen |
| `dokument_zu_paperless` | `dokument_id` (int) | Beleg an Paperless-ngx senden, Link speichern |

### Feld-Wertebereiche
- `status` (Baustelle): `aktiv` / `abgeschlossen`.
- `status` (Aufgabe): `offen` / `in Arbeit` / `fertig`.
- `mit_rechnung`: `True` = mit Rechnung (dokumentiert/versteuerbar), `False` = ohne Rechnung.
- `doc_type`: `rechnung` / `beleg` / `foto`.
- `datum` (Zeiteintrag): ISO `YYYY-MM-DD`, optional.
- `dokument_hochladen` verlangt genau **eine** Zuordnung: `aufgabe_id` **oder** `material_id`.

### Upload-Mechanik (`dokument_hochladen`)
Der MCP-Server liest `datei_pfad` (lokaler Pfad auf dem Pi) und sendet ihn als
`multipart/form-data` an `POST /api/documents` (Felder `file`, `task_id`/`material_id`,
`doc_type`). `server.py` speichert unter `data/uploads/`, Whitelist `.jpg/.jpeg/.png/.webp/.pdf`,
`secure_filename` gegen Pfad-Traversal.

### MCP-Test-Checkliste
- [ ] `baulog_mcp.py` startet als MCP-Server (`hermes mcp test baulog` → Tools entdeckt).
- [ ] Projekt + Baustelle + Gewerk + Aufgabe per MCP anlegen und per `_liste` wiederfinden.
- [ ] Zeiteintrag + Material per MCP mit `mit_rechnung=True/False` anlegen; Summen stimmen.
- [ ] `baustelle_summary`/`projekt_summary`/`gesamt_summary` liefern korrekte Beträge.
- [ ] `dokument_hochladen` mit lokalem Bild lädt hoch und erscheint in `dokumente_liste`.
- [ ] `dokument_zu_paperless` nur bei aktiver Paperless-Config erfolgreich.
- [ ] MCP-Änderungen sind in der Web-UI sichtbar (gleiche DB) und umgekehrt.

