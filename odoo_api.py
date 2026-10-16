"""
odoo_api.py — XML-RPC-verbinding met een Odoo-database
=======================================================
Alleen HTTPS (SafeTransport), met time-out en herkansing bij 429/503.
"""

import logging
import random
import threading
import time
import xmlrpc.client

logger = logging.getLogger(__name__)

# Odoo.sh knijpt XML-RPC af zodra er een zwerm aanroepen tegelijk binnenkomt:
# dan volgt een HTTP 429, en xmlrpc maakt daar een ProtocolError van. Dat is
# geen storing maar een verzoek om even te wachten — de aanroep zelf was
# prima. Zonder herkansing slaat zo'n piek een gat in de gegevens: bij het
# invullen van een factuur kwam de btw-codelijst leeg terug, en dan kan de
# agent niets meer en moet er een mens bij. Wachten en het nog eens vragen is
# een fractie van die prijs.
#
# 503 zit erbij omdat Odoo die teruggeeft als alle workers bezet zijn; dat is
# dezelfde situatie met een ander nummer. Andere fouten NIET: een 403 of een
# kapot domein wordt niet beter van herhalen, en stil doorproberen maakt een
# echte fout alleen maar later zichtbaar.
HERKANSBARE_HTTP_CODES = frozenset({429, 503})
HERKANSINGEN = 4


def met_herkansing(aanroep, *, pogingen: int = HERKANSINGEN):
    """Voer `aanroep` uit en probeer opnieuw zolang Odoo om rust vraagt.

    Exponentieel oplopend (0,5 / 1 / 2 seconden) met een willekeurige marge,
    zodat acht threads die tegelijk een 429 krijgen niet ook tegelijk weer
    aankloppen. De laatste poging gooit door: op een gegeven moment is het echt
    te druk, en dan hoort de aanroeper dat te weten in plaats van een leeg
    antwoord te krijgen.
    """
    laatste = None
    for poging in range(max(1, pogingen)):
        try:
            return aanroep()
        except xmlrpc.client.ProtocolError as exc:
            if exc.errcode not in HERKANSBARE_HTTP_CODES:
                raise
            laatste = exc
            if poging == pogingen - 1:
                break
            wacht = 0.5 * (2 ** poging) + random.uniform(0, 0.25)
            logger.warning("[ODOO] %s van %s; opnieuw over %.1fs (poging %s/%s)",
                           exc.errcode, exc.url, wacht, poging + 2, pogingen)
            time.sleep(wacht)
    raise laatste


class TimeoutTransport(xmlrpc.client.SafeTransport):
    """SafeTransport met instelbare socket timeout."""
    def __init__(self, timeout=10, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class OdooAPI:
    """Wrapper voor Odoo API calls.

    DRAADVEILIG PER THREAD, NIET PER OBJECT

    Eén OdooAPI wordt gedeeld: de MCP-server cachet er één per database
    (mcp_server/tenants.py) en gunicorn draait acht threads. Dat ging mis zodra
    twee verzoeken elkaar overlapten.

    `xmlrpc.client.ServerProxy` deelt namelijk één `Transport`, en die houdt
    precies één HTTP-verbinding vast (`self._connection`). Twee threads die
    tegelijk `execute_kw` aanroepen, schrijven om beurten in diezelfde socket en
    lezen elkaars antwoord terug. Wat je dan ziet is geen foutmelding maar een
    verbinding die blijft hangen — aan de clientkant "Request-sent" of "Idle"
    die nooit meer verandert. Precies wat er gebeurde toen een agent meerdere
    facturen tegelijk probeerde te boeken: alle parallelle aanroepen liepen
    vast, en één voor één werken was de enige uitweg.

    De oplossing is klein: de proxy staat in thread-local opslag, dus elke
    thread bouwt zijn eigen verbinding. Opnieuw inloggen hoeft daarvoor niet —
    `execute_kw` krijgt de uid en de api-key als argument mee, en die zijn wél
    veilig te delen. Een ServerProxy aanmaken legt bovendien nog geen verbinding
    aan; dat gebeurt pas bij de eerste aanroep.
    """

    def __init__(self, url: str, db: str, user: str, api_key: str, timeout: int = 10):
        self.url = url
        self.db = db
        self.user = user
        self.api_key = api_key
        self.uid = None
        self.timeout = timeout
        self._lokaal = threading.local()

    @property
    def models(self):
        """De object-proxy van DEZE thread, zo nodig aangemaakt."""
        proxy = getattr(self._lokaal, "models", None)
        if proxy is None:
            proxy = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object",
                transport=TimeoutTransport(timeout=self.timeout))
            self._lokaal.models = proxy
        return proxy

    @models.setter
    def models(self, waarde):
        """Blijft zetbaar, zodat een test er een nepproxy in kan hangen."""
        self._lokaal.models = waarde

    def authenticate(self) -> bool:
        """Authenticeer met Odoo"""
        try:
            common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common",
                transport=TimeoutTransport(timeout=self.timeout))
            # Odoo geeft False terug bij een fout wachtwoord/sleutel, geen None.
            self.uid = common.authenticate(self.db, self.user, self.api_key, {}) or None
            return bool(self.uid)
        except Exception as e:
            logger.warning("Odoo-authenticatie mislukt voor %s: %s", self.db, type(e).__name__)
            return False

    def execute(self, model: str, method: str, domain: list, fields: dict = None):
        """Algemene execute functie"""
        if not self.uid:
            raise Exception("Niet geauthenticeerd")
        
        argumenten = (self.db, self.uid, self.api_key, model, method, domain)
        if fields:
            argumenten += (fields,)
        return met_herkansing(lambda: self.models.execute_kw(*argumenten))
