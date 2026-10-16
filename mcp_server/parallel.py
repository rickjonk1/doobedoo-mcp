"""
mcp_server/parallel.py — meerdere Odoo-aanroepen tegelijk
=========================================================
Een factuur invullen vraagt vier dingen aan Odoo die niets van elkaar weten: de
bijlage, de historie van de leverancier, de grootboekrekeningen en de
btw-codes. Achter elkaar is dat vier keer de latency naar Odoo; tegelijk is het
één keer. Bij een Odoo die er 200–400 ms over doet scheelt dat een seconde per
factuur, en dat is bij een doel van vijf seconden geen detail.

WAAROM HIER EEN EIGEN PROXY PER THREAD STAAT

`xmlrpc.client.ServerProxy` deelt één `Transport`, en die houdt precies één
HTTP-verbinding vast (`self._connection`). Twee threads die tegelijk op dezelfde
proxy aanroepen, schrijven om beurten in diezelfde socket en lezen elkaars
antwoord terug — meestal een onbegrijpelijke parse-fout, soms stilletjes het
verkeerde record. Dat is geen theoretisch risico maar de bekende valkuil van
deze module.

De oplossing is klein: elke thread bouwt zijn eigen ServerProxy. Opnieuw
inloggen hoeft niet — `execute_kw` neemt de uid en de api-key als argument, en
die hebben we al. Er gaat dus geen extra rondje naar Odoo overheen.

EN WAAROM DE FLASK-CONTEXT MEEGAAT

Een tweede valkuil van dezelfde soort, en die heeft maanden stil schade gedaan.
Flask bewaart zijn app-context per thread. Alles wat hier in een worker-thread
draait, draait dus buiten die context — en code die netjes `has_app_context()`
controleert, doet dan stilletjes niets.

Dat is precies wat er met de OCR-cache gebeurde. `documents._ocr_naar_cache` en
`_ocr_uit_cache` slaan zichzelf over zonder context, en sinds het document in
deze golf wordt meegelezen, gebeurde dat élke keer. Resultaat: nul rijen in
api_response_cache met endpoint 'ocr', en dus elke factuur opnieuw 13 tot 26
seconden overtypen — ook een factuur die al eens gelezen was.

Er is geen foutmelding, geen waarschuwing, niets. Vandaar dat de context hier
expliciet wordt doorgegeven: een worker-thread hoort net zoveel te kunnen als
de thread die hem startte.
"""
from __future__ import annotations

import logging
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor

from odoo_api import TimeoutTransport, met_herkansing

logger = logging.getLogger(__name__)

# Meer dan dit tegelijk levert niets op: het zijn er per factuur een stuk of
# vier, en een Odoo-instantie van een administratiekantoor is niet gebaat bij
# een zwerm verbindingen.
MAX_THREADS = 6

# Ruimer dan de standaard tien seconden van OdooAPI: deze aanroepen lopen
# parallel, dus een trage erbij kost geen extra wachttijd, en een timeout
# halverwege kost wél een hele factuur.
TIMEOUT_SECONDEN = 30


class _Fout:
    """Een mislukte deelvraag. Geen exception: één trage query mag de andere
    drie niet weggooien — de aanroeper beslist wat een gat betekent."""

    def __init__(self, exc: BaseException):
        self.exc = exc

    def __repr__(self) -> str:                       # pragma: no cover
        return f"<Fout {type(self.exc).__name__}: {self.exc}>"


def _eigen_proxy(api):
    """Een verse ServerProxy op dezelfde database, voor gebruik in één thread."""
    return xmlrpc.client.ServerProxy(
        f"{api.url}/xmlrpc/2/object",
        transport=TimeoutTransport(timeout=TIMEOUT_SECONDEN))


def _voer_uit(api, model, methode, argumenten, opties):
    proxy = _eigen_proxy(api)
    # Herkansing bij 429/503, net als OdooAPI.execute: juist deze aanroepen
    # komen met een handvol tegelijk binnen en zijn dus de eerste die Odoo
    # afknijpt. Een deelvraag die daarop leeg terugkomt kost de agent een hele
    # extra beurt — zie de docstring van gelijktijdig().
    return met_herkansing(
        lambda: proxy.execute_kw(api.db, api.uid, api.api_key,
                                 model, methode, argumenten, opties or {}))


def _huidige_app():
    """De Flask-app van de thread die ons aanriep, of None buiten een verzoek."""
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            return current_app._get_current_object()
    except Exception:                                # noqa: BLE001 — optioneel
        pass
    return None


def _met_context(app, functie, *argumenten):
    """Voer `functie` uit met dezelfde app-context als de aanroepende thread.

    Zonder dit draait alles hier buiten de Flask-context, en dan doet code die
    `has_app_context()` controleert stilletjes niets — zie de module-docstring
    voor wat dat met de OCR-cache deed.
    """
    if app is None:
        return functie(*argumenten)
    with app.app_context():
        return functie(*argumenten)


def gelijktijdig(api, opdrachten: dict) -> dict:
    """Voer Odoo-aanroepen tegelijk uit en geef de uitkomsten per naam terug.

    `opdrachten` is {naam: (model, methode, argumenten, opties)}. Een opdracht
    mag ook een gewone functie zijn die niets met Odoo te maken heeft — handig
    om het lezen van een document in dezelfde golf mee te nemen.

    Een deelvraag die faalt levert een `_Fout` op in plaats van de hele boel om
    te gooien; gebruik `uitkomst()` om er een waarde of een standaard uit te
    halen.
    """
    if not opdrachten:
        return {}
    if not getattr(api, "uid", None):
        raise RuntimeError("Niet geauthenticeerd bij Odoo.")

    app = _huidige_app()

    uit: dict = {}
    with ThreadPoolExecutor(max_workers=min(MAX_THREADS, len(opdrachten))) as pool:
        futures = {}
        for naam, opdracht in opdrachten.items():
            if callable(opdracht):
                futures[pool.submit(_met_context, app, opdracht)] = naam
            else:
                model, methode, argumenten, opties = opdracht
                futures[pool.submit(_met_context, app, _voer_uit, api, model,
                                    methode, argumenten, opties)] = naam
        for fut, naam in futures.items():
            try:
                uit[naam] = fut.result()
            except Exception as exc:                 # noqa: BLE001 — zie docstring
                logger.warning("[MCP] deelvraag %s mislukt: %s: %s",
                               naam, type(exc).__name__, exc)
                uit[naam] = _Fout(exc)
    return uit


def uitkomst(waarde, standaard=None):
    """De waarde, of `standaard` als die deelvraag mislukte."""
    return standaard if isinstance(waarde, _Fout) else waarde


def fout_tekst(waarde) -> str | None:
    """De reden waarom een deelvraag mislukte, of None als hij slaagde."""
    if isinstance(waarde, _Fout):
        return f"{type(waarde.exc).__name__}: {waarde.exc}"
    return None
