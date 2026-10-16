"""
lockdown.py — tijdelijke toegangsgrendel op het dashboard
==========================================================
Bedoeld voor een productie-cutover: zolang de grendel aan staat mogen
alléén de e-mailadressen op de lijst inloggen. Alle anderen worden
geweigerd bij het inloggen én uit een lopende sessie gezet.

Twee bronnen, in deze volgorde:

1. de omgevingsvariabele ``LOGIN_ALLOWLIST`` (komma-gescheiden adressen)
2. de instelling ``login_allowlist`` in AppConfig (via ``flask lockdown``)

De omgevingsvariabele wint. Dat is bewust: hij werkt zonder database, dus
je kunt jezelf niet buitensluiten door een fout in de DB of een migratie
die halverwege staat.

Een lege lijst betekent: geen grendel, iedereen mag gewoon inloggen.

Faalgedrag: kan de AppConfig-waarde niet gelezen worden (database plat,
tabel bestaat nog niet), dan gaat de grendel NIET aan — anders sluit een
databaseprobleem ook de beheerder buiten en is er geen weg terug zonder
een nieuwe deploy. Wil je een harde grendel die dat risico niet loopt,
gebruik dan de omgevingsvariabele.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

ENV_VAR    = "LOGIN_ALLOWLIST"
CONFIG_KEY = "login_allowlist"

# AppConfig niet bij élk verzoek uitlezen; een halve minuut is ruim genoeg
# om een wijziging door te laten komen.
_CACHE_TTL = 30.0
_cache: set[str] | None = None
_cache_ts: float = 0.0
_cache_bron: str = ""
_slot = threading.Lock()


def _ontleed(rauw: str | None) -> set[str]:
    """'a@b.nl, C@D.nl' -> {'a@b.nl', 'c@d.nl'}"""
    if not rauw:
        return set()
    return {deel.strip().lower() for deel in str(rauw).replace(";", ",").split(",")
            if deel.strip()}


def leeg_cache() -> None:
    """Forceer een herlezing (gebruikt door de CLI en de tests)."""
    global _cache, _cache_ts, _cache_bron
    with _slot:
        _cache, _cache_ts, _cache_bron = None, 0.0, ""


def _uit_appconfig() -> set[str]:
    try:
        from models.app_config import AppConfig
        return _ontleed(AppConfig.get(CONFIG_KEY))
    except Exception as exc:
        logger.warning("[LOCKDOWN] AppConfig niet leesbaar (%s) — grendel blijft uit. "
                       "Zet LOGIN_ALLOWLIST als je een grendel wilt die niet van de "
                       "database afhangt.", exc)
        return set()


def toegestane_adressen() -> set[str]:
    """De actieve lijst. Leeg = geen grendel."""
    global _cache, _cache_ts, _cache_bron

    uit_env = _ontleed(os.environ.get(ENV_VAR))
    if uit_env:
        return uit_env

    nu = time.time()
    if _cache is not None and (nu - _cache_ts) < _CACHE_TTL:
        return _cache
    with _slot:
        if _cache is not None and (time.time() - _cache_ts) < _CACHE_TTL:
            return _cache
        _cache = _uit_appconfig()
        _cache_ts = time.time()
        _cache_bron = "appconfig"
    return _cache


def bron() -> str:
    """'env', 'appconfig' of '' — waar de actieve lijst vandaan komt."""
    if _ontleed(os.environ.get(ENV_VAR)):
        return "env"
    return "appconfig" if toegestane_adressen() else ""


def actief() -> bool:
    return bool(toegestane_adressen())


def mag_inloggen(email: str | None) -> bool:
    """True als dit adres nu naar binnen mag."""
    lijst = toegestane_adressen()
    if not lijst:
        return True
    return (email or "").strip().lower() in lijst


def zet_lijst(adressen, door: str = "system") -> set[str]:
    """Schrijf de lijst naar AppConfig. Lege lijst = grendel uit."""
    from models.app_config import AppConfig
    schoon = sorted(_ontleed(",".join(adressen)) if not isinstance(adressen, str)
                    else _ontleed(adressen))
    AppConfig.set(CONFIG_KEY, ",".join(schoon), updated_by=door)
    leeg_cache()
    return set(schoon)


def status() -> dict:
    lijst = sorted(toegestane_adressen())
    return {"actief": bool(lijst), "adressen": lijst, "bron": bron()}


BOODSCHAP = ("De server is tijdelijk gesloten voor onderhoud. "
             "Alleen de beheerder kan nu inloggen.")
