"""
backup.py — tägliche Sicherung des Baulogs, aber nur wenn sich etwas geändert hat.

Gesichert wird ALLES, was nicht im Git liegt:
  - Datenbank (konsistent über die SQLite-Online-Backup-API)
  - data/uploads (Fotos, Belege, Berichte), data/eingang (geteilte Dateien)
  - config.local.json (Paperless-Zugang), data/ki-einstellungen.json (KI-Anbieter + Keys)

Ziele:
  - Backup-Festplatte (Standard /mnt/hdd-backup/backups/paperless/baulog-daily):
    30 tägliche Stände + der erste Stand jedes Monats für 12 Monate
  - zusätzlich data/backups auf der SD-Karte: die letzten 3 Stände (schnelle Kopie)

Ablauf: Fingerabdruck von Datenbank-Inhalt und Dateien bilden. Ist er gleich wie bei der
letzten Sicherung, wird nichts gesichert ("unverändert"). Sonst sichern, die Sicherung prüfen
(Integritätscheck der DB, Dateien im Archiv vollständig) und alte Stände aufräumen.
Bei einem Fehler kommt eine Telegram-Meldung. Exit-Code 0 = Erfolg oder unverändert.

Aufruf:  python scripts/backup.py [--ziel DIR] [--erzwingen]
Wiederherstellen: siehe Ende dieser Datei.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import urllib.parse
import urllib.request
from datetime import datetime

PROJEKT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJEKT, "data")
DB = os.path.join(DATA, "baulog.db")
HDD_ZIEL = "/mnt/hdd-backup/backups/paperless/baulog-daily"
HDD_MOUNT = "/mnt/hdd-backup"
LOKAL_ZIEL = os.path.join(DATA, "backups")
ENV_DATEI = "/home/sven/cloud_home/raspberry-pi-cloud/.env"
TAGE, MONATE, LOKAL_BEHALTEN = 30, 12, 3

# Dateien/Ordner (relativ zum Projekt), die neben der DB ins Archiv kommen
EXTRAS = ["data/uploads", "data/eingang", "config.local.json", "data/ki-einstellungen.json"]


def telegram(text: str) -> None:
    try:
        env = {}
        for zeile in open(ENV_DATEI, encoding="utf-8"):
            if "=" in zeile and not zeile.lstrip().startswith("#"):
                k, v = zeile.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            return
        daten = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", daten, timeout=15)
    except Exception:
        pass


def dateien() -> list[str]:
    """Alle zu sichernden Dateien (absolute Pfade), sortiert."""
    liste = []
    for rel in EXTRAS:
        pfad = os.path.join(PROJEKT, rel)
        if os.path.isfile(pfad):
            liste.append(pfad)
        elif os.path.isdir(pfad):
            for wurzel, _, namen in os.walk(pfad):
                liste += [os.path.join(wurzel, n) for n in namen]
    return sorted(liste)


def fingerabdruck() -> str:
    """Inhalt der DB (als SQL-Dump) + Name/Größe/Zeitstempel aller Dateien."""
    h = hashlib.sha256()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        for zeile in con.iterdump():
            h.update(zeile.encode("utf-8", "replace"))
    finally:
        con.close()
    for f in dateien():
        st = os.stat(f)
        h.update(f"{os.path.relpath(f, PROJEKT)}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


def sichern(ordner: str) -> dict:
    """Schreibt baulog.db + dateien.tar.gz in `ordner` und prüft beides."""
    os.makedirs(ordner)
    db_ziel = os.path.join(ordner, "baulog.db")
    quelle = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ziel = sqlite3.connect(db_ziel)
    try:
        with ziel:
            quelle.backup(ziel)
    finally:
        ziel.close()
        quelle.close()
    liste = dateien()
    tar_ziel = os.path.join(ordner, "dateien.tar.gz")
    with tarfile.open(tar_ziel, "w:gz") as tar:
        for f in liste:
            tar.add(f, arcname=os.path.relpath(f, PROJEKT))
    # Prüfen
    con = sqlite3.connect(f"file:{db_ziel}?mode=ro", uri=True)
    try:
        ergebnis = con.execute("PRAGMA integrity_check").fetchone()[0]
        zahlen = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("projects", "tasks", "time_entries", "materials", "documents", "receipts")}
    finally:
        con.close()
    if ergebnis != "ok":
        raise RuntimeError(f"Integritätscheck der gesicherten DB: {ergebnis}")
    with tarfile.open(tar_ziel, "r:gz") as tar:
        im_archiv = len([m for m in tar.getmembers() if m.isfile()])
    if im_archiv != len(liste):
        raise RuntimeError(f"Archiv unvollständig: {im_archiv} von {len(liste)} Dateien")
    return {"dateien": len(liste), "zahlen": zahlen,
            "groesse_mb": round(sum(os.path.getsize(os.path.join(ordner, n)) for n in os.listdir(ordner)) / 1048576, 1)}


def staende(ziel: str) -> list[str]:
    return sorted(n for n in os.listdir(ziel) if n.startswith("baulog-") and
                  os.path.isdir(os.path.join(ziel, n)))


def aufraeumen(ziel: str, tage: int, monate: int) -> list[str]:
    """Behält die jüngsten `tage` Stände + den ersten Stand je Monat der letzten `monate` Monate."""
    alle = staende(ziel)
    behalten = set(alle[-tage:]) if tage else set()
    erste_je_monat = {}
    for n in alle:  # baulog-YYYYMMDD-HHMMSS
        erste_je_monat.setdefault(n[7:13], n)
    behalten |= set(sorted(erste_je_monat.values())[-monate:]) if monate else set()
    weg = [n for n in alle if n not in behalten]
    for n in weg:
        shutil.rmtree(os.path.join(ziel, n), ignore_errors=True)
    return weg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Baulog-Sicherung (nur bei Änderungen).")
    ap.add_argument("--ziel", default=os.environ.get("BAULOG_BACKUP_DIR", HDD_ZIEL))
    ap.add_argument("--erzwingen", action="store_true", help="auch ohne Änderung sichern")
    ap.add_argument("--behalten", type=int, help=argparse.SUPPRESS)  # alter Aufruf, wird ignoriert
    args = ap.parse_args(argv)
    jetzt = datetime.now()
    stempel = jetzt.strftime("%Y%m%d-%H%M%S")
    try:
        if args.ziel.startswith(HDD_MOUNT) and not os.path.ismount(HDD_MOUNT):
            raise RuntimeError(f"Backup-Festplatte {HDD_MOUNT} ist nicht eingehängt")
        os.makedirs(args.ziel, exist_ok=True)
        status_datei = os.path.join(args.ziel, "letzter-stand.json")
        try:
            status = json.load(open(status_datei, encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        fp = fingerabdruck()
        if not args.erzwingen and status.get("fingerabdruck") == fp and staende(args.ziel):
            print(f"{jetzt:%d.%m.%Y %H:%M} unverändert seit {status.get('zeit')} – keine neue Sicherung")
            return 0
        ordner = os.path.join(args.ziel, f"baulog-{stempel}")
        info = sichern(ordner)
        status = {"fingerabdruck": fp, "zeit": f"{jetzt:%d.%m.%Y %H:%M}", "stand": os.path.basename(ordner), **info}
        json.dump(status, open(status_datei, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        weg = aufraeumen(args.ziel, TAGE, MONATE)
        print(f"{jetzt:%d.%m.%Y %H:%M} gesichert: {ordner} ({info['groesse_mb']} MB, {info['dateien']} Dateien, "
              f"{info['zahlen']}){f'; {len(weg)} alte Stände entfernt' if weg else ''}")
        # schnelle Zusatzkopie auf der SD-Karte
        if os.path.abspath(args.ziel) != os.path.abspath(LOKAL_ZIEL):
            os.makedirs(LOKAL_ZIEL, exist_ok=True)
            shutil.copytree(ordner, os.path.join(LOKAL_ZIEL, os.path.basename(ordner)))
            for alt in [n for n in os.listdir(LOKAL_ZIEL) if n.startswith("baulog-") and not
                        os.path.isdir(os.path.join(LOKAL_ZIEL, n))]:
                os.remove(os.path.join(LOKAL_ZIEL, alt))  # Dateien im alten Format
            aufraeumen(LOKAL_ZIEL, LOKAL_BEHALTEN, 0)
        return 0
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        telegram(f"🚨 <b>Baulog-Sicherung FEHLGESCHLAGEN</b>\n📅 {jetzt:%d.%m.%Y %H:%M}\n❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

# Wiederherstellen (Beispiel):
#   systemctl --user stop tool-baulog
#   cp <stand>/baulog.db ~/hermes-workspace/projects/baulog/data/baulog.db
#   tar -xzf <stand>/dateien.tar.gz -C ~/hermes-workspace/projects/baulog
#   systemctl --user start tool-baulog
