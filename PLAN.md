# baulog — Implementierungsplan

Internes Webtool zur **Baustellen-Verwaltung & Kosten-Tracking**.
Läuft als Flask-App im lokalen Netz auf einem Raspberry Pi
(`http://192.168.1.134:PORT`, Port aus `TOOL_PORT`).

Erste Baustelle: **Mehrfamilienhaus Deisterstraße 40, Feggendorf**.

---

## 1. Zweck

baulog dient der internen Erfassung und Auswertung aller Kosten einer Baustelle:

- Projekte (Baustellen) und deren **Gewerke** (Maurer, Elektro, Sanitär, Trockenbau, Dach, Fenster …) verwalten.
- **Aufgaben** je Gewerk anlegen (z. B. „Wand ziehen") mit Status und Beschreibung.
- **Zeiterfassung** (Stunden × Stundensatz → Personalkosten) und **Materialerfassung**
  (Menge × Einzelpreis → Materialkosten) je Aufgabe.
- Jede Kostenposition (Personal **und** Material) trägt ein **Rechnungs-Flag**
  („mit Rechnung" / „ohne Rechnung") zur Trennung dokumentierter (versteuerbarer)
  von undokumentierten Kosten.
- **Dashboard/Kostenübersicht**: Summen Material + Personal, getrennt nach
  „mit Rechnung"/„ohne Rechnung", pro Baustelle und gesamt.
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

**Konventionen** (aus Vorlage `server.py`):
- `app = Flask(__name__, static_folder=".")`
- Port aus `os.environ.get("TOOL_PORT", "5000")`, `host="0.0.0.0"`.
- `data/` für persistente Daten; DB und Uploads dort.
- Paperless-Zugang ausschließlich serverseitig aus `config.local.json` /
  Umgebungsvariablen (`PAPERLESS_URL`, `PAPERLESS_TOKEN`) — niemals im Frontend.

---

## 3. UI-Skizze (Seiten / Ansichten + Navigation)

Single-Page-App mit Hash-Routing (`#/`, `#/projekt/<id>` …). Persistente
**Top-Bar** mit Titel + Baustellen-Auswahl (Dropdown), darunter **Tab-Navigation**.
Mobile-first: ab 320px bedienbar, Touch-Ziele ≥ 44px, Tabellen werden auf schmalen
Screens als **Card-List** dargestellt (`hidden sm:table` / `sm:hidden` Cards).

### Navigation (Bottom-Nav auf Mobile, Top-Tabs ab `sm`)
```
[ Dashboard ] [ Aufgaben ] [ Gewerke ] [ Dokumente ] [ Projekte ]
```

### 3.1 Dashboard (`#/`)
- Auswahl aktive Baustelle (oder „Alle").
- Kachel-Karten mit Summen:
  - **Material** — mit Rechnung / ohne Rechnung / Gesamt
  - **Personal** — mit Rechnung / ohne Rechnung / Gesamt
  - **Gesamtkosten** — mit Rechnung / ohne Rechnung / Summe
- Balken/Tabelle je Gewerk (Kostenverteilung).
- Umschalter „pro Baustelle" ↔ „gesamt (alle Baustellen)".

### 3.2 Projekte (`#/projekte`)
- Liste aller Baustellen (Name, Adresse, Status, Gesamtkosten).
- Button „+ Neue Baustelle" → Formular (Name, Adresse, Notiz).
- Detail: Gewerke des Projekts verwalten.

### 3.3 Gewerke (`#/projekt/<id>/gewerke`)
- Liste der Gewerke der aktiven Baustelle (Name, #Aufgaben, Kostensumme).
- „+ Gewerk hinzufügen" (Name frei oder aus Vorschlagsliste:
  Maurer, Elektro, Sanitär, Trockenbau, Dach, Fenster).

### 3.4 Aufgaben (`#/projekt/<id>/aufgaben`)
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

### 3.5 Dokumente (`#/projekt/<id>/dokumente`)
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

### Projekte (Baustellen)
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/projects` | alle Baustellen (mit Kosten-Summary) |
| POST | `/api/projects` | Baustelle anlegen |
| GET | `/api/projects/<pid>` | Detail inkl. Gewerke |
| PUT | `/api/projects/<pid>` | Baustelle ändern |
| DELETE | `/api/projects/<pid>` | Baustelle löschen (kaskadiert) |

### Gewerke
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/projects/<pid>/trades` | Gewerke der Baustelle |
| POST | `/api/projects/<pid>/trades` | Gewerk anlegen |
| PUT | `/api/trades/<tid>` | Gewerk umbenennen |
| DELETE | `/api/trades/<tid>` | Gewerk löschen |

### Aufgaben
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/projects/<pid>/tasks?trade=<tid>&status=<s>` | Aufgaben (filterbar) |
| POST | `/api/projects/<pid>/tasks` | Aufgabe anlegen |
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
| GET | `/api/documents?task_id=<>` \| `?material_id=<>` \| `?project_id=<>` | Liste |
| GET | `/api/documents/<docid>/file` | Datei/Vorschau ausliefern |
| DELETE | `/api/documents/<docid>` | Dokument + Datei löschen |
| POST | `/api/documents/<docid>/paperless` | optional an Paperless-ngx senden, Link speichern |

### Kostenübersicht / Dashboard
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/projects/<pid>/summary` | Summen je Baustelle (Material/Personal × mit/ohne Rechnung, je Gewerk) |
| GET | `/api/summary` | Gesamtsummen über alle Baustellen |

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

### `projects` — Baustellen
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| name | TEXT NOT NULL | z. B. „MFH Deisterstraße 40" |
| address | TEXT | „Deisterstraße 40, Feggendorf" |
| status | TEXT DEFAULT 'aktiv' | aktiv / abgeschlossen |
| note | TEXT | |
| created_at | TEXT NOT NULL | |

### `trades` — Gewerke
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| project_id | INTEGER NOT NULL | FK → projects(id) ON DELETE CASCADE |
| name | TEXT NOT NULL | Maurer, Elektro, Sanitär … |
| created_at | TEXT NOT NULL | |

### `tasks` — Aufgaben
| Spalte | Typ | Hinweis |
|---|---|---|
| id | INTEGER PK | |
| project_id | INTEGER NOT NULL | FK → projects(id) ON DELETE CASCADE |
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
| project_id | INTEGER | FK → projects(id) (Denormalisierung für Galerie-Filter) |
| filename | TEXT NOT NULL | Originaldateiname |
| stored_path | TEXT NOT NULL | Pfad in data/uploads/ |
| mime | TEXT | image/jpeg, application/pdf … |
| doc_type | TEXT | rechnung / beleg / foto |
| paperless_id | INTEGER | gesetzt nach Paperless-Sync (nullable) |
| paperless_url | TEXT | Link zum Paperless-Dokument (nullable) |
| created_at | TEXT NOT NULL | |

**Beziehungen:** `projects 1—n trades`, `projects 1—n tasks`, `trades 1—n tasks`,
`tasks 1—n time_entries`, `tasks 1—n materials`, `documents` hängt an `task` **oder**
`material` (mind. eines gesetzt), zusätzlich `project_id` zur schnellen Galerie-Filterung.

---

## 6. Kostenberechnung (Aggregationslogik)

Für `summary`-Endpunkte je Baustelle bzw. gesamt:

- **Material mit Rechnung** = Σ `qty*unit_price` über `materials` mit `billed=1`
- **Material ohne Rechnung** = Σ `qty*unit_price` über `materials` mit `billed=0`
- **Personal mit Rechnung** = Σ `hours*hourly_rate` über `time_entries` mit `billed=1`
- **Personal ohne Rechnung** = Σ `hours*hourly_rate` über `time_entries` mit `billed=0`
- **Gesamt mit Rechnung** / **Gesamt ohne Rechnung** / **Gesamtsumme** entsprechend.
- Zusätzlich Aufschlüsselung je Gewerk (Join `tasks.trade_id`).

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

### Projekte & Gewerke
- [ ] Baustelle „MFH Deisterstraße 40, Feggendorf" anlegen, bearbeiten, in Liste sichtbar.
- [ ] Gewerke (Maurer, Elektro, Sanitär, Trockenbau, Dach, Fenster) anlegen/löschen.
- [ ] Löschen einer Baustelle entfernt zugehörige Gewerke/Aufgaben (Kaskade).

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
- [ ] Gesamtsummen über alle Baustellen korrekt.
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
