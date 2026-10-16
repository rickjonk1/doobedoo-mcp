"""
mcp_server/resources.py — MCP-resources: de doobedoo-huisstijl
===============================================================
Zo weet een verbonden client (Claude/ChatGPT) hoe een document voor doobedoo
eruit hoort te zien.

  doobedoo://huisstijl   (text/markdown) — kleuren, typografie, logogebruik
  doobedoo://logo        (image/png)     — horizontaal logo, kleur op licht
  doobedoo://logo-wit    (image/png)     — horizontaal logo, kleur op donker
  doobedoo://icoon       (image/png)     — los beeldmerk
"""
from __future__ import annotations

import base64
import os

_STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "img")

HUISSTIJL_MD = """# doobedoo — huisstijl

doobedoo is de Odoo-consultancy van Hein Koning. Slogan: *Slimmer ondernemen met Odoo.*

## Kleuren
| Naam | Hex | Gebruik |
|---|---|---|
| Nachtpaars | `#110B2E` | Tekst, donkere vlakken |
| Diep paars | `#5B21FF` | Primaire accentkleur, knoppen, koppen |
| Violet | `#8B5CF6` | Secundair accent |
| Koraal | `#FF6B8B` | Spaarzaam accent, verloop |
| Perzik | `#FF9F6B` | Spaarzaam accent, verloop |
| Lavendel | `#F4EFFF` | Lichte achtergrond, tabelzebra |

Het beeldmerk loopt van diep paars via violet en koraal naar perzik; gebruik dat
verloop alleen in het logo of één signatuurelement, niet als vlakvulling.

## Typografie
Strak en modern: een geometrische sans (bijv. Inter, SF Pro, Helvetica als
terugval). Koppen halfvet in nachtpaars, bodytekst regulier 10–11 pt.

## Logo
- Op lichte achtergrond: `doobedoo://logo`. Op donker: `doobedoo://logo-wit`.
- Los beeldmerk: `doobedoo://icoon`.
- Verhoudingen behouden en ruimte rondom laten; geen effecten of schaduwen.

## Getalnotatie
Nederlands: punt als duizendtalscheider, komma als decimaalteken (bv. `€ 1.234,56`).
"""


def _png_b64(name: str) -> str:
    with open(os.path.join(_STATIC, name), "rb") as f:
        return base64.b64encode(f.read()).decode()


_RESOURCES = {
    "doobedoo://huisstijl": {
        "name": "doobedoo huisstijl",
        "description": "Kleuren, typografie en logogebruik van doobedoo.",
        "mimeType": "text/markdown",
        "text": lambda: HUISSTIJL_MD,
    },
    "doobedoo://logo": {
        "name": "doobedoo logo (licht)",
        "description": "Horizontaal logo voor een lichte achtergrond.",
        "mimeType": "image/png",
        "blob": lambda: _png_b64("doobedoo_horizontaal_kleur_1200.png"),
    },
    "doobedoo://logo-wit": {
        "name": "doobedoo logo (donker)",
        "description": "Horizontaal logo voor een donkere achtergrond.",
        "mimeType": "image/png",
        "blob": lambda: _png_b64("doobedoo_horizontaal_kleur_wit_1200.png"),
    },
    "doobedoo://icoon": {
        "name": "doobedoo beeldmerk",
        "description": "Los beeldmerk, vierkant.",
        "mimeType": "image/png",
        "blob": lambda: _png_b64("doobedoo_icoon_512.png"),
    },
}


def list_resources() -> list[dict]:
    return [{"uri": uri, "name": r["name"], "description": r["description"],
             "mimeType": r["mimeType"]} for uri, r in _RESOURCES.items()]


def read_resource(uri: str) -> dict | None:
    r = _RESOURCES.get(uri)
    if not r:
        return None
    content = {"uri": uri, "mimeType": r["mimeType"]}
    if "text" in r:
        content["text"] = r["text"]()
    else:
        content["blob"] = r["blob"]()
    return content
