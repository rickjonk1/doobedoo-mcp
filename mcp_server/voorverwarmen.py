"""
mcp_server/voorverwarmen.py — de volgende scan alvast laten overtypen
======================================================================
GEMETEN, NIET GEGOKT

Op 11 september liep de wachtrij van een klant in prod. Uit de productielogs
komt een strak ritme van ongeveer 32 seconden per factuur, en bij elke factuur
één regel als deze:

    [MCP] transcriptie: 1 pagina('s), 1883 in / 965 uit tokens

Die uit-tokens zijn de rekening. Sonnet levert op eu-west-1 ongeveer 49
output-tokens per seconde (gemeten, twee runs), en de transcripties in prod
zaten tussen 648 en 1199 tokens uit. Dat is 13 tot 24 seconden waarin de agent
niets anders doet dan wachten tot een scan is overgetypt — de helft tot
driekwart van de tijd per factuur.

Sneller overtypen kan niet zonder aan leesnauwkeurigheid in te boeten, en dat
is precies de ruil die in ocr.py met redenen omkleed is afgewezen. Maar het
hóéft ook niet in de wachttijd te gebeuren. Wie een wachtrij afwerkt, weet al
welke facturen er nog komen: die kunnen overgetypt worden terwijl de agent aan
de vorige bezig is. De transcriptie kost dan nog steeds twintig seconden, maar
niemand zit erop te wachten.

Dat werkt omdat het resultaat in de OCR-cache belandt (documents._ocr_naar_cache,
op checksum). Zodra prepare_draft_bill of book_draft_bill bij de factuur komt,
ligt de tekst klaar en is het overtypen uit het pad verdwenen.

WAT HIER MET OPZET KLEIN BLIJFT

- Twee threads. Dit is achtergrondwerk; het mag de acht gunicorn-threads niet
  opeten die het dashboard en de boekaanroepen zelf nodig hebben.
- Het verandert niets aan wat de agent terugkrijgt. Lukt het voorverwarmen
  niet, dan merkt niemand er iets van behalve dat de factuur weer de oude
  twintig seconden kost.
- Geen enkele fout komt naar buiten. Dit is een optimalisatie; een
  optimalisatie die een boeking laat mislukken is erger dan geen optimalisatie.
- Elke bijlage hoogstens één keer tegelijk in behandeling, zodat twee
  overlappende lijstaanroepen niet hetzelfde werk dubbel bij Bedrock neerleggen.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Hoeveel facturen we vooruitlopen. Verder vooruit levert niets op — de agent
# haalt het toch niet in — en zet alleen onnodig Bedrock-verkeer op de rekening
# voor facturen die misschien worden overgeslagen.
MAX_VOORUIT = int(os.environ.get("MCP_VOORVERWARMEN_AANTAL", "8"))

# Twee tegelijk. Meer is zinloos: er is er maar één tegelijk nodig (de
# volgende), en een zwerm transcripties loopt tegen Bedrock-throttling aan.
GELIJKTIJDIG = 2

_pool: ThreadPoolExecutor | None = None
_pool_slot = threading.Lock()

_bezig: set[tuple[str, int]] = set()
_bezig_slot = threading.Lock()


def ingeschakeld() -> bool:
    return (os.environ.get("MCP_VOORVERWARMEN", "1") or "").strip().lower() not in (
        "0", "false", "nee", "off", "")


def _pool_ophalen() -> ThreadPoolExecutor:
    global _pool
    with _pool_slot:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=GELIJKTIJDIG,
                                       thread_name_prefix="ocr-warm")
        return _pool


def _warm_een(app, api, move: dict, sleutel: tuple[str, int]) -> None:
    try:
        # Een eigen app-context: zonder Flask-context slaat de OCR-cache zichzelf
        # stil over (documents._ocr_naar_cache) en was het hele voorverwarmen
        # voor niets — het resultaat zou nergens blijven staan.
        with app.app_context():
            from .documents import hoofdbijlage_lezen
            hoofdbijlage_lezen(api, move)
        logger.info("[MCP] factuur %s alvast overgetypt", move.get("id"))
    except Exception as exc:                        # noqa: BLE001 — zie docstring
        logger.debug("[MCP] voorverwarmen van %s mislukt: %s", move.get("id"), exc)
    finally:
        with _bezig_slot:
            _bezig.discard(sleutel)


def start(api, moves: list[dict]) -> int:
    """Zet het overtypen van deze facturen in gang en kom meteen terug.

    `moves` zijn dicts met minstens `id` en `message_main_attachment_id`.
    Geeft terug hoeveel er in de wachtrij zijn gezet — puur ter informatie;
    de aanroeper hoort er niets mee te doen en er zeker niet op te wachten.
    """
    if not ingeschakeld() or not moves:
        return 0
    try:
        from . import ocr
        if not ocr.ingeschakeld():
            return 0
        from flask import current_app, has_app_context
        if not has_app_context():
            # Zonder context kan het resultaat niet in de cache, en dan is het
            # werk weggegooid. Dan liever niets doen.
            return 0
        app = current_app._get_current_object()
    except Exception as exc:                        # noqa: BLE001 — optimalisatie
        logger.debug("[MCP] voorverwarmen niet gestart: %s", exc)
        return 0

    db_naam = getattr(api, "db", "?")
    pool = _pool_ophalen()
    gestart = 0
    for move in moves[:MAX_VOORUIT]:
        ref = move.get("message_main_attachment_id")
        bijlage = (ref[0] if isinstance(ref, (list, tuple)) and ref
                   else ref if isinstance(ref, int) and not isinstance(ref, bool)
                   else None)
        if not bijlage:
            continue
        sleutel = (db_naam, bijlage)
        with _bezig_slot:
            if sleutel in _bezig:
                continue
            _bezig.add(sleutel)
        try:
            pool.submit(_warm_een, app, api, move, sleutel)
            gestart += 1
        except Exception as exc:                    # noqa: BLE001 — optimalisatie
            with _bezig_slot:
                _bezig.discard(sleutel)
            logger.debug("[MCP] voorverwarmen niet ingepland: %s", exc)
    if gestart:
        logger.info("[MCP] %s factuur/facturen alvast aan het overtypen", gestart)
    return gestart
