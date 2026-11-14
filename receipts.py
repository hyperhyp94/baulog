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

DEFAULT_MODEL = "claude-sonnet-5-5"
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


# Anbieter für die Belegauswertung. Einstellbar in der Oberfläche (⚙ KI),
# gespeichert in data/ki-einstellungen.json (nicht im Git).
ANBIETER = {
    "anthropic": {
        "name": "Anthropic (Claude-API)",
        "env": "ANTHROPIC_API_KEY",
        "standard": "claude-sonnet-5-5",
        "modelle": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
    },
    "opencode-go": {
        "name": "OpenCode Go",
        "env": "OPENCODE_GO_API_KEY",
        "url": "https://opencode.ai/inference/go/openai/v1/chat/completions",
        "standard": "deepseek-v4.1-flash",
        # nur Modelle mit Bild-Eingabe (deepseek-v4-pro kann keine Bilder lesen)
        "modelle": ["deepseek-v4.1-flash", "qwen3.8-flash", "gpt-6-luna", "mimo-v2.6-flash",
                    "claude-haiku-5-5", "gemini-4-argon", "qwen3.8-max"],
    },
}


def settings_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "data", "ki-einstellungen.json")


def _read_settings(config_path: str) -> dict:
    try:
        with open(settings_path(config_path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(config_path: str, anbieter: str | None = None) -> dict:
    """
    Anbieter, Modell und Key. Vorrang: Einstellungen aus der Oberfläche,
    dann config.local.json (anthropic_api_key/receipts_model), dann Umgebung.
    """
    legacy = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                legacy = json.load(fh)
        except (json.JSONDecodeError, OSError):
            legacy = {}
    st = _read_settings(config_path)
    if anbieter is None:
        anbieter = st.get("anbieter")
    if anbieter not in ANBIETER:
        anbieter = "anthropic"
    info = ANBIETER[anbieter]
    keys = st.get("keys") or {}
    ist_a = anbieter == "anthropic"
    key = keys.get(anbieter) or (legacy.get("anthropic_api_key") if ist_a else None) \
        or os.environ.get(info["env"])
    modell = (st.get("modelle") or {}).get(anbieter) \
        or (legacy.get("receipts_model") if ist_a else None) \
        or (os.environ.get("BAULOG_RECEIPTS_MODEL") if ist_a else None) \
        or info["standard"]
    return {"anbieter": anbieter, "model": modell, "api_key": key,
            "key_quelle": "eingetragen" if keys.get(anbieter) else ("Server" if key else None)}


def public_settings(config_path: str) -> dict:
    """Einstellungen für die Oberfläche — Keys nur maskiert."""
    out = {"anbieter": load_config(config_path)["anbieter"], "anbieter_liste": []}
    for k, info in ANBIETER.items():
        cfg = load_config(config_path, k)
        key = cfg["api_key"]
        out["anbieter_liste"].append({
            "id": k, "name": info["name"], "modelle": info["modelle"], "modell": cfg["model"],
            "key": ("…" + key[-4:]) if key else None, "key_quelle": cfg["key_quelle"]})
    return out


def save_settings(config_path: str, data: dict) -> None:
    """Übernimmt anbieter / modell / key (leerer key = unverändert; key_loeschen=true entfernt ihn)."""
    st = _read_settings(config_path)
    anbieter = data.get("anbieter")
    if anbieter not in ANBIETER:
        raise ReceiptError("Unbekannter Anbieter.")
    st["anbieter"] = anbieter
    modell = str(data.get("modell") or "").strip()
    if modell:
        if len(modell) > 100 or not all(c.isalnum() or c in ".-_:/" for c in modell):
            raise ReceiptError("Ungültiger Modellname.")
        st.setdefault("modelle", {})[anbieter] = modell
    key = data.get("key")
    if isinstance(key, str) and key.strip():
        key = key.strip()
        if len(key) > 300 or any(c.isspace() for c in key):
            raise ReceiptError("Ungültiger API-Key.")
        st.setdefault("keys", {})[anbieter] = key
    if data.get("key_loeschen"):
        st.setdefault("keys", {}).pop(anbieter, None)
    path = settings_path(config_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
    if client is None and not cfg["api_key"]:
        raise ReceiptError("Belegauswertung nicht eingerichtet – unter ⚙ KI einen API-Key eintragen.", 503)
    if cfg["anbieter"] == "opencode-go" and client is None:
        text = _opencode(images, cfg)
    else:
        text = _anthropic(images, cfg, client)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        raw = json.loads(text)
    except ValueError:
        raise ReceiptError("Auswertung lieferte kein gültiges JSON – anderes Modell versuchen?", 502)
    return normalize(raw)


def _fehlertext(r) -> str:
    try:
        return (r.json().get("error") or {}).get("message") or r.text
    except ValueError:
        return r.text


def _opencode_post(cfg, payload, timeout):
    import time
    import requests
    try:
        return requests.post(ANBIETER["opencode-go"]["url"], timeout=timeout, headers={
            "Authorization": "Bearer " + cfg["api_key"],
            "x-opencode-session": f"baulog-{time.time_ns()}"}, json=payload)
    except requests.RequestException as exc:
        raise ReceiptError(f"OpenCode nicht erreichbar: {str(exc)[:200]}", 502)


def _anthropic(images, cfg, client=None) -> str:
    if client is None:
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
    except Exception as exc:  # SDK-Fehler (Auth, Guthaben, Netz) verständlich melden
        status = getattr(exc, "status_code", None)
        msg = getattr(exc, "message", None) or str(exc)
        if status in (401, 403):
            raise ReceiptError("Anthropic: API-Key ungültig oder ohne Berechtigung.", 502)
        if status == 400 and "credit" in msg.lower():
            raise ReceiptError("Anthropic: kein Guthaben mehr auf dem Konto.", 502)
        raise ReceiptError(f"Anthropic-API Fehler: {msg[:300]}", 502)
    if getattr(resp, "stop_reason", None) == "refusal":
        raise ReceiptError("Die Auswertung wurde vom Modell abgelehnt.", 502)
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ReceiptError("Beleg zu lang für eine Auswertung – bitte in zwei Teilen erfassen.", 502)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _opencode(images, cfg) -> str:
    """OpenAI-kompatibler Aufruf bei OpenCode Go (Bilder als data-URL)."""
    content = []
    for i, (data, media) in enumerate(images, 1):
        content.append({"type": "text", "text": f"Bild {i} von {len(images)}:"})
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{media};base64," + base64.standard_b64encode(data).decode("ascii")}})
    content.append({"type": "text", "text": PROMPT + "\nAntworte nur mit dem JSON-Objekt."})
    r = _opencode_post(cfg, {
        "model": cfg["model"], "max_tokens": 16000,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "beleg", "strict": True, "schema": SCHEMA}}}, 180)
    if r.status_code != 200:
        if r.status_code in (401, 403):
            raise ReceiptError(f"OpenCode: Zugang abgelehnt – {_fehlertext(r)[:200]}", 502)
        raise ReceiptError(f"OpenCode-Fehler {r.status_code}: {_fehlertext(r)[:300]}", 502)
    ch = (r.json().get("choices") or [{}])[0]
    if ch.get("finish_reason") == "length":
        raise ReceiptError("Beleg zu lang für eine Auswertung – bitte in zwei Teilen erfassen.", 502)
    return (ch.get("message") or {}).get("content") or ""


def verbindung_testen(config_path: str) -> str:
    """Kurzer Text-Aufruf zum Prüfen von Key/Modell."""
    cfg = load_config(config_path)
    if not cfg["api_key"]:
        raise ReceiptError("Kein API-Key für diesen Anbieter.", 400)
    if cfg["anbieter"] == "opencode-go":
        r = _opencode_post(cfg, {"model": cfg["model"], "max_tokens": 20,
                                 "messages": [{"role": "user", "content": "Antworte nur mit OK."}]}, 60)
        if r.status_code != 200:
            raise ReceiptError(f"OpenCode {r.status_code}: {_fehlertext(r)[:300]}", 502)
    else:
        try:
            _client(cfg["api_key"]).messages.create(
                model=cfg["model"], max_tokens=10,
                messages=[{"role": "user", "content": "Antworte nur mit OK."}])
        except Exception as exc:
            msg = getattr(exc, "message", None) or str(exc)
            if "credit" in msg.lower():
                raise ReceiptError("Anthropic: kein Guthaben mehr auf dem Konto.", 502)
            raise ReceiptError(f"Anthropic: {msg[:300]}", 502)
    return f"Verbindung OK: {ANBIETER[cfg['anbieter']]['name']}, Modell {cfg['model']}"


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
