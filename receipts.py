"""
receipts.py — Auswertung fotografierter Kassenbons/Rechnungen über die Claude-API.

Lange Bons werden in mehreren überlappenden Fotos aufgenommen; alle Bilder
gehen in EINER Anfrage (in Aufnahme-Reihenfolge) an Claude, das doppelt
fotografierte Zeilen nur einmal zählt. Die Antwort wird per Structured Output
erzwungen und hier zusätzlich validiert/normalisiert.
"""

import base64
import json
import math
import os
from datetime import date

DEFAULT_MODEL = "claude-opus-5-5"
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_MEDIA = {"image/jpeg", "image/png", "image/webp"}


class ReceiptError(Exception):
    """Fachlicher Fehler bei der Auswertung (Meldung ist für den Nutzer gedacht)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["haendler", "datum", "belegnr", "gesamt", "positionen", "rabatte", "hinweise"],
    "properties": {
        "haendler": {"type": ["string", "null"]},
        "datum": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "belegnr": {"type": ["string", "null"]},
        "gesamt": {"type": ["number", "null"], "description": "Zu zahlender Endbetrag brutto"},
        "positionen": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "menge", "einheit", "einzelpreis", "summe"],
                "properties": {
                    "name": {"type": "string"},
                    "menge": {"type": "number"},
                    "einheit": {"type": ["string", "null"]},
                    "einzelpreis": {"type": "number"},
                    "summe": {"type": "number"},
                },
            },
        },
        "rabatte": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "betrag"],
                "properties": {"text": {"type": "string"}, "betrag": {"type": "number"}},
            },
        },
        "hinweise": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT = """Die Bilder zeigen EINEN Kassenbon oder EINE Rechnung (Baumarkt, Großhandel o. ä.).
Bei mehreren Bildern sind es nacheinander fotografierte Abschnitte desselben Belegs,
von oben nach unten, mit Überlappung: Zeilen, die auf zwei Bildern zu sehen sind,
nur EINMAL übernehmen.

Erfasse jede gekaufte Artikelzeile als Position (Bruttopreise, wie gedruckt).
- Mengenzeilen wie "3 x 4,99" gehören zur Artikelzeile: menge=3, einzelpreis=4.99.
- Gewichts-/Längenangaben (z. B. "2,5 m x 3,20") als menge mit passender einheit.
- Pfand gehört als eigene Position dazu.
- Rabatte/Gutscheine/Coupons NICHT als Position, sondern unter rabatte mit negativem betrag.
- Zwischensummen, MwSt-Aufstellungen, Zahlungsart, Rückgeld ignorieren.
- gesamt = zu zahlender Endbetrag.
Unleserliches oder Unsicheres kurz unter hinweise nennen (deutsch). Nichts erfinden;
fehlende Werte als null."""


def load_config(config_path: str) -> dict:
    """Liest anthropic_api_key/receipts_model aus config.local.json bzw. Umgebung."""
    cfg = {
        "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        "model": os.environ.get("BAULOG_RECEIPTS_MODEL") or DEFAULT_MODEL,
    }
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                data = json.load(fh)
            cfg["api_key"] = data.get("anthropic_api_key") or cfg["api_key"]
            cfg["model"] = data.get("receipts_model") or cfg["model"]
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def enabled(config_path: str) -> bool:
    return bool(load_config(config_path)["api_key"])


def _num(val, default=0.0) -> float:
    if isinstance(val, bool):
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def normalize(raw: dict) -> dict:
    """Bereinigt die Modellantwort und ergänzt den Summenabgleich."""
    if not isinstance(raw, dict):
        raise ReceiptError("Auswertung lieferte kein gültiges Ergebnis.", 502)
    positionen = []
    for p in raw.get("positionen") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:500]
        if not name:
            continue
        menge = _num(p.get("menge"), 1.0)
        einzel = _num(p.get("einzelpreis"))
        summe = _num(p.get("summe"), round(menge * einzel, 2))
        if menge <= 0:
            menge = 1.0
        # Einzelpreis aus Summe ableiten, wenn er fehlt oder nicht passt
        if summe and abs(round(menge * einzel, 2) - summe) > 0.011:
            einzel = round(summe / menge, 4)
        einheit = p.get("einheit")
        positionen.append({
            "name": name,
            "menge": menge,
            "einheit": (str(einheit).strip()[:30] or None) if einheit else None,
            "einzelpreis": einzel,
            "summe": round(menge * einzel, 2),
        })
    rabatte = []
    for r in raw.get("rabatte") or []:
        if isinstance(r, dict):
            betrag = -abs(_num(r.get("betrag")))
            if betrag:
                rabatte.append({"text": str(r.get("text") or "Rabatt")[:200], "betrag": betrag})

    datum = raw.get("datum")
    try:
        datum = date.fromisoformat(datum).isoformat() if datum else None
    except (TypeError, ValueError):
        datum = None
    gesamt = raw.get("gesamt")
    gesamt = round(_num(gesamt), 2) if gesamt is not None else None

    summe_pos = round(sum(p["summe"] for p in positionen) + sum(r["betrag"] for r in rabatte), 2)
    return {
        "haendler": (str(raw.get("haendler")).strip()[:200] if raw.get("haendler") else None),
        "datum": datum,
        "belegnr": (str(raw.get("belegnr")).strip()[:100] if raw.get("belegnr") else None),
        "gesamt": gesamt,
        "positionen": positionen,
        "rabatte": rabatte,
        "hinweise": [str(h)[:300] for h in (raw.get("hinweise") or []) if h][:20],
        "summe_positionen": summe_pos,
        "differenz": round(summe_pos - gesamt, 2) if gesamt is not None else None,
    }


def _client(api_key: str):
    import anthropic  # erst hier, damit der Server ohne Paket startet
    return anthropic.Anthropic(api_key=api_key, timeout=120.0)


def analyze(images: list[tuple[bytes, str]], config_path: str, client=None) -> dict:
    """
    Wertet 1..MAX_IMAGES Bilder [(bytes, media_type)] aus.
    `client` ist für Tests injizierbar.
    """
    if not images:
        raise ReceiptError("Keine Bilder übergeben.")
    if len(images) > MAX_IMAGES:
        raise ReceiptError(f"Maximal {MAX_IMAGES} Bilder pro Beleg.")
    for data, media in images:
        if media not in ALLOWED_MEDIA:
            raise ReceiptError("Nur JPEG, PNG oder WebP erlaubt.")
        if not data:
            raise ReceiptError("Leeres Bild übergeben.")
        if len(data) > MAX_IMAGE_BYTES:
            raise ReceiptError("Ein Bild ist größer als 8 MB.")

    cfg = load_config(config_path)
    if client is None:
        if not cfg["api_key"]:
            raise ReceiptError("Belegauswertung nicht konfiguriert (anthropic_api_key fehlt).", 503)
        client = _client(cfg["api_key"])

    content = []
    for i, (data, media) in enumerate(images, 1):
        content.append({"type": "text", "text": f"Bild {i} von {len(images)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media,
            "data": base64.standard_b64encode(data).decode("ascii")}})
    content.append({"type": "text", "text": PROMPT})

    try:
        resp = client.messages.create(
            model=cfg["model"],
            max_tokens=16000,
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except ReceiptError:
        raise
    except Exception as exc:  # SDK-Fehler (Auth, Guthaben, Netz) verständlich melden
        status = getattr(exc, "status_code", None)
        msg = getattr(exc, "message", None) or str(exc)
        if status in (401, 403):
            raise ReceiptError("Claude-API-Key ungültig oder ohne Berechtigung.", 502)
        if status == 400 and "credit" in msg.lower():
            raise ReceiptError("Claude-API: kein Guthaben mehr auf dem Konto.", 502)
        raise ReceiptError(f"Claude-API nicht erreichbar oder Fehler: {msg[:300]}", 502)

    if getattr(resp, "stop_reason", None) == "refusal":
        raise ReceiptError("Die Auswertung wurde vom Modell abgelehnt.", 502)
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ReceiptError("Beleg zu lang für eine Auswertung – bitte in zwei Teilen erfassen.", 502)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        raw = json.loads(text)
    except ValueError:
        raise ReceiptError("Auswertung lieferte kein gültiges JSON.", 502)
    return normalize(raw)


def distribute_discount(positionen: list[dict], rabatt: float) -> list[dict]:
    """
    Verteilt einen (negativen) Rabatt anteilig auf die Einzelpreise, damit keine
    negativen Materialpositionen entstehen. Gibt neue Positionen zurück.
    """
    total = sum(p["qty"] * p["unit_price"] for p in positionen)
    if not rabatt or total <= 0:
        return positionen
    faktor = max(0.0, (total + rabatt) / total)
    return [{**p, "unit_price": round(p["unit_price"] * faktor, 4)} for p in positionen]
