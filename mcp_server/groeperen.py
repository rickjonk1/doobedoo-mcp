"""
mcp_server/groeperen.py — tellen in Odoo 17 tot en met 19
==========================================================
Odoo 19 heeft `read_group` van de modellen gehaald. Een aanroep geeft daar:

    AttributeError: The method 'account.move.line.read_group' does not exist

De opvolger heet `formatted_read_group` en heeft een andere handtekening. Onze
databases lopen door elkaar — de ene database doet `read_group`, de andere niet —
dus het is geen kwestie van overstappen maar van allebei kunnen.

Waarom dit zo veel uitmaakte: `_historie_per_rekening` en
`_veelgebruikte_rekeningen` zijn de twee bronnen waar de lijst met toegestane
grootboekrekeningen uit komt. Vielen die om, dan viel de controle in
boekvoorstel terug op een lege lijst en werd élke voorgestelde rekening
afgekeurd. Het gevolg was niet "een foutmelding" maar "book_draft_bill schrijft
niets en de agent gaat alles met de hand doen" — vijf tot tien extra aanroepen
per factuur, met de bijbehorende denkbeurten.

De twee vormen:

    read_group(domein, fields=['balance:sum'], groupby=['account_id'],
               lazy=False)
        -> [{'account_id': [id, naam], 'balance': 12.5, '__count': 3}]

    formatted_read_group(domein, ['account_id'], ['__count', 'balance:sum'])
        -> [{'account_id': [id, naam], 'balance:sum': 12.5, '__count': 3}]

Deze module geeft altijd de eerste vorm terug: de aggregaat-sleutel zonder
achtervoegsel. Zo hoeven de aanroepers niets te weten van de Odoo-versie.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Welke methode werkt op deze database? Eén keer uitzoeken per (url, db) en
# onthouden; de versie van een database verandert hooguit bij een upgrade en
# dan is een herstart van de container goedkoop genoeg.
_METHODE: dict[str, str] = {}
_SLOT = threading.Lock()

NIEUW = "formatted_read_group"
OUD = "read_group"


def _sleutel(api) -> str:
    return f"{getattr(api, 'url', '?')}|{getattr(api, 'db', '?')}"


def _lijkt_ontbrekende_methode(exc: Exception) -> bool:
    """Is dit 'die methode bestaat niet' en niet iets inhoudelijks?

    We kijken naar de tekst omdat Odoo dit als xmlrpc.Fault doorgeeft; de
    oorspronkelijke AttributeError overleeft de lijn niet als type.
    """
    tekst = str(exc).lower()
    return "does not exist" in tekst or "no attribute" in tekst


def _kaal(sleutel: str) -> str:
    """'balance:sum' -> 'balance'. De aanroepers kennen alleen de kale naam."""
    return sleutel.split(":", 1)[0]


def _normaliseer(rijen: list, aggregaten: list) -> list:
    uit = []
    for rij in rijen or []:
        nieuw = {}
        for k, v in rij.items():
            if k.startswith("__") and k != "__count":
                continue                     # __domain / __extra_domain: ruis
            nieuw[_kaal(k)] = v
        for a in aggregaten:
            nieuw.setdefault(_kaal(a), None)
        nieuw.setdefault("__count", 0)
        uit.append(nieuw)
    return uit


def groepeer(api, model: str, domein: list, groupby: list,
             aggregaten: list) -> list:
    """Groepeer en tel, ongeacht de Odoo-versie.

    `aggregaten` zonder `__count`; die komt er altijd bij. Geeft rijen met de
    kale aggregaat-namen terug, plus `__count`.
    """
    sleutel = _sleutel(api)
    volgorde = ([_METHODE[sleutel]] if sleutel in _METHODE
                else [NIEUW, OUD])

    laatste: Exception | None = None
    for methode in volgorde:
        try:
            if methode == NIEUW:
                rijen = api.execute(model, NIEUW,
                                    [domein, groupby, ["__count"] + list(aggregaten)])
            else:
                rijen = api.execute(model, OUD, [domein],
                                    {"fields": list(aggregaten),
                                     "groupby": groupby, "lazy": False})
        except Exception as exc:              # noqa: BLE001 — zie hieronder
            laatste = exc
            # Alleen doorschuiven als de methode zelf ontbreekt. Een echte fout
            # (rechten, verkeerd veld) moet niet stilletjes de andere weg in:
            # dan krijg je twee onbegrijpelijke fouten in plaats van één.
            if sleutel in _METHODE or not _lijkt_ontbrekende_methode(exc):
                raise
            continue

        with _SLOT:
            if _METHODE.get(sleutel) != methode:
                _METHODE[sleutel] = methode
                logger.info("[MCP] %s gebruikt %s voor groeperen", sleutel, methode)
        return _normaliseer(rijen, aggregaten)

    raise laatste if laatste else RuntimeError("groeperen mislukt")


def leeg() -> None:
    """Vergeet wat er onthouden is. Voor tests en na een Odoo-upgrade."""
    with _SLOT:
        _METHODE.clear()
