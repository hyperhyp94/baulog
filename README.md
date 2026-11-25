# baulog
Baustellen- &amp; Kosten-Tracking (Projekt → Baustelle → Gewerk → Aufgabe) mit Zeit-/Materialerfassung, mit/ohne-Rechnung-Flag, Dokument-/Foto-Upload und Paperless-Anbindung. Flask + SQLite + MCP-Steuerung.

## Auszahlung & Guthaben — immer synchron

Eine **Auszahlung** ist eine Zeile in `guthaben`. Sie wird mit den offenen Stunden der Person verrechnet
(älteste Tage zuerst, Tabelle `guthaben_verbrauch`); was übrig bleibt, ist das **Guthaben**
(Saldo = Einzahlungen − Verrechnungen) und wird mit den nächsten Stunden automatisch verbraucht.

Diese Regeln gelten nach **jeder** Änderung — auch bei Anpassungen im Nachgang —, weil `db.py` sie in
derselben Transaktion wiederherstellt (`_guthaben_abgleichen`):

1. Ein Tag wird nie mit mehr verrechnet, als er kostet oder als bezahlt geführt wird.
2. Verrechnet wird nur innerhalb derselben Person und desselben Projekts.
3. Guthaben und offene Stunden derselben Person schließen sich aus.

Was dabei passiert:

| Anpassung im Nachgang | Ergebnis |
|---|---|
| Stunden/Satz/Anfahrt eines bezahlten Tags sinken | der Überschuss geht ins Guthaben zurück |
| … steigen | die bezahlten Euro bleiben bezahlt, der Mehrbetrag ist offen |
| Tag oder Aufgabe/Baustelle wird gelöscht | die Auszahlung wird frei und deckt andere offene Stunden |
| Tag wechselt die Person | die Verrechnung geht an die bisherige Person zurück, der Tag ist offen |
| Baustelle wechselt das Projekt | die Verrechnung des alten Projekts geht zurück |
| Person wird umbenannt | ihre Auszahlungen ziehen mit um |
| „bezahlt“ auf einem per Auszahlung bezahlten Tag zurücknehmen | wird abgelehnt (409) — die Auszahlung im Verlauf rückgängig machen |
| Verlauf → Auszahlung rückgängig | die damit bezahlten Tage werden wieder offen (Barzahlungen bleiben bezahlt) |
| Verlauf → einzelne Verrechnung / automatische Folgezeile („Abgleich“) | wird abgelehnt — die auslösende Änderung bzw. die Auszahlung/Person als Ganzes rückgängig machen |
| Verlauf → Zuordnung/Betrag rückgängig | nur Person/Betrag/… kommen zurück, der Zahlstatus ergibt sich neu aus der Buchführung (kein „bezahlt“ obendrauf) |
| Satz/Stunden auf 0 und wieder zurück | die Auszahlung wird frei und deckt den Tag danach wieder — nichts doppelt |
| Person wechseln **und** Betrag ändern in einem Schritt | nur bar gezahlte Euro bleiben am Tag, nie erfundene |
| Projekt löschen (und im Verlauf wiederherstellen) | die Verrechnung wird vorher freigegeben; die wiederhergestellte Auszahlung deckt die Tage wieder |
| Personen-Namen | ohne Groß-/Kleinschreibung eindeutig („bob“ neben „Bob“ gäbe gemeinsame Konten); Umbenennen zieht auch Tage ohne Verknüpfung mit; Auszahlung an einen unbekannten Namen wird abgelehnt |

Eine einzige Rundung (SQL-`ROUND`) für Verrechnung, Summen, API, Oberfläche, Bericht und MCP.

Automatische Folgen der Buchführung stehen im Verlauf mit der Quelle **Abgleich**.

### Prüfen und reparieren

```bash
python3 scripts/check_db.py                    # meldet u. a. Abweichungen bei Auszahlung/Guthaben (Exit 1)
python3 scripts/guthaben_abgleich.py           # Trockenlauf: zeigt, was sich ändern würde — ändert nichts
python3 scripts/guthaben_abgleich.py --anwenden  # sichert die DB, korrigiert, prüft danach erneut
```

### Tests

```bash
python -m pytest tests -q                      # Szenarien (jede Art von Anpassung im Nachgang) + Zufallstest
BAULOG_ZUFALL_SEEDS=300 python -m pytest tests/test_guthaben_zufall.py   # Dauertest
```

Der Zufallstest führt lange, beliebige Folgen aus Ändern, Löschen, Umbenennen, Personenwechsel,
Verschieben und Rückgängig aus und prüft nach **jedem** Schritt die Regeln (unabhängiges Prüf-Orakel in
`scripts/check_db.py`) sowie, dass ein erneuter Abgleich nichts mehr ändert.
