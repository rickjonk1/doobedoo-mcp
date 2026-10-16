"""
mcp_server/admincache.py — antwoorden die voor de hele administratie gelden
===========================================================================
Bij het invullen van één inkoopfactuur stelt de server vier vragen aan Odoo.
Drie daarvan gaan helemaal niet over díé factuur:

- welke btw-codes er voor inkoop bestaan;
- waar deze administratie inkoopfacturen in het algemeen op boekt;
- welke crediteuren er zijn.

Het antwoord daarop is voor factuur 1 en factuur 89 hetzelfde. Toch werd het
negenentachtig keer opgehaald, en de middelste is geen kleintje: een read_group
over álle geboekte inkoopregels van de administratie.

Dat kost niet alleen tijd. Het is ook wat Odoo doet afknijpen — in de wachtrij
van een klant kwam bij twee facturen op rij een 429 terug op precies deze
deelvragen, met een lege btw-codelijst als gevolg. En zonder btw-codes kan de
agent niets invullen, dus dan is de hele beurt weg.

Daarom hier één korte cache per database. De TTL is ruim genoeg voor een hele
boekronde en kort genoeg dat een nieuwe grootboekrekening of crediteur binnen
tien minuten meedoet.

EÉN OPHALER TEGELIJK PER SLEUTEL

Een gewone cache lost de zwerm niet op: vier facturen die tegelijk starten,
missen alle vier en gaan alle vier ophalen — precies de piek die de 429
veroorzaakte. Daarom een slot per sleutel, en na het slot nóg een blik in de
cache: wie moest wachten vindt het antwoord dan al klaarliggen en vraagt niets.

Mislukte aanroepen komen hier nooit in: die gooien door, zodat een storing niet
tien minuten lang als antwoord wordt uitgeserveerd.
"""
from __future__ import annotations

import threading
import time

# Ruim genoeg voor een boekronde van honderd facturen, kort genoeg dat een
# vandaag aangemaakte grootboekrekening vanzelf meedoet.
TTL_SECONDEN = 600

_LEEG = object()

_waarden: dict[tuple[str, str], tuple[float, object]] = {}
_waarden_slot = threading.Lock()

_sloten: dict[tuple[str, str], threading.Lock] = {}
_sloten_slot = threading.Lock()


def _lees(sleutel, ttl):
    with _waarden_slot:
        item = _waarden.get(sleutel)
    if item is None or time.time() - item[0] >= ttl:
        return _LEEG
    return item[1]


def _slot_voor(sleutel) -> threading.Lock:
    with _sloten_slot:
        return _sloten.setdefault(sleutel, threading.Lock())


def via_cache(database: str, naam: str, ophalen, ttl: float = TTL_SECONDEN):
    """Het antwoord op `naam` voor `database`, uit de cache of vers opgehaald.

    `ophalen` wordt hoogstens door één thread tegelijk aangeroepen; de rest
    wacht en krijgt daarna hetzelfde antwoord zonder zelf iets te vragen.
    """
    sleutel = (database or "?", naam)

    gevonden = _lees(sleutel, ttl)
    if gevonden is not _LEEG:
        return gevonden

    with _slot_voor(sleutel):
        # Tweede blik: terwijl we op het slot wachtten, kan een andere thread
        # het antwoord al hebben neergelegd. Zonder deze regel vraagt iedereen
        # alsnog om de beurt, en is de zwerm alleen uitgesmeerd in plaats van
        # weg.
        gevonden = _lees(sleutel, ttl)
        if gevonden is not _LEEG:
            return gevonden

        waarde = ophalen()
        with _waarden_slot:
            _waarden[sleutel] = (time.time(), waarde)
        return waarde


def leeg(database: str | None = None) -> None:
    """Gooi de cache weg — voor tests, en voor als een administratie net is
    aangepast en je niet tien minuten wilt wachten."""
    with _waarden_slot:
        if database is None:
            _waarden.clear()
        else:
            for sleutel in [k for k in _waarden if k[0] == database]:
                del _waarden[sleutel]
