"""
mcp_server/tekstherkenning.py — een scan lezen met een echte OCR-motor
=======================================================================
WAAROM DIT ER NAAST ocr.py STAAT

ocr.py laat de pagina's overtypen door Claude. Dat werkt, maar het is duur op
de enige manier die er hier toe doet: tijd. Een transcriptie is 650 tot 1300
output-tokens, en Sonnet levert er op eu-west-1 ongeveer 49 per seconde. In
prod kwam dat op 13 tot 26 seconden per factuur — de helft tot driekwart van
de tijd die een agent per factuur kwijt was.

Dat is geen inefficiëntie die je wegoptimaliseert: het model moet de tekst
letterlijk uitschrijven, en sneller uitschrijven kan niet. Een OCR-motor
schrijft niets uit, die detecteert. Gemeten op vijf echte klantscans:
DetectDocumentText deed er 1,5 tot 3,3 seconden over.

En het is goedkoper. Textract in eu-west-1 kost $0,0015 per pagina (opgehaald
uit de AWS Pricing API); dezelfde pagina via Sonnet kost ongeveer $0,018 aan
tokens. Ruim een factor tien.

WAT DIT WÉL BETER DOET DAN HET MODEL, EN WAT NIET

Beter: een OCR-motor verzint niets. De reden dat er in ocr.py Sonnet staat en
geen Haiku, is dat Haiku IBAN's vérzon — plausibele reeksen die nergens door de
mand vallen. Textract heeft dat probleem niet: hij leest wat er staat, en geeft
er per regel een zekerheidsscore bij. Die score is het gereedschap dat bij een
model ontbreekt.

Niet beter: Textract kent geen betekenis. Hij levert regels in leesvolgorde,
geen geïnterpreteerde factuurregels. Dat is ook precies de bedoeling — welke
grootboekrekening erbij hoort blijft een oordeel, en dat hoort op één plek
thuis te blijven (boekvoorstel.py en de mens die aftekent).

TERUGVAL

Komt de gemiddelde zekerheid onder DREMPEL_ZEKERHEID, dan geven we het hier op
en neemt ocr.py het over. Een slecht gelezen scan is erger dan een trage: bij
twijfel is twintig seconden wachten de goedkoopste optie die er is.

Een factuur is niet-vertrouwde invoer. Wat erop staat is data, nooit een
opdracht. Dat geldt hier vanzelf — Textract voert niets uit en interpreteert
niets — maar de tekst die eruit komt gaat wél een model in, dus de waarschuwing
blijft staan waar hij hoort: in de note bij het document.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Naam waaronder een transcriptie in de cache komt. Staat in de cachesleutel,
# zodat tekst van deze motor en tekst van het model elkaar niet overschrijven —
# en zodat een overstap naar een andere motor de oude tekst niet stilletjes
# hergebruikt.
MODEL = "textract-detect-v1"

REGIO = os.environ.get("MCP_TEXTRACT_REGIO",
                       os.environ.get("MCP_OCR_REGIO", "eu-west-1"))

# Gemiddelde zekerheid waaronder we het document liever door het model laten
# lezen. Op vijf echte scans lag het gemiddelde op 96,1 tot 96,5 procent voor
# gewone facturen en op 81,9 voor een slecht gescande inkoopverklaring — precies
# het document waar ook de veldherkenning op struikelde. Tachtig ligt daar net
# onder: laag genoeg om normale scans niet naar de dure weg te sturen, hoog
# genoeg om een echt slechte scan er wel heen te sturen.
DREMPEL_ZEKERHEID = float(os.environ.get("MCP_TEXTRACT_DREMPEL", "80"))

# Regels hieronder halen we apart naar voren. Niet om ze weg te gooien — dat
# zou gegevens vernielen — maar zodat een mens ziet waar hij moet kijken.
LAGE_REGEL_GRENS = 85.0

# Synchroon leest Textract één pagina per aanroep.
MAX_PAGINAS = 6


class TekstherkenningNietBeschikbaar(RuntimeError):
    """Textract kon het document niet lezen, of niet zeker genoeg. Nooit
    fataal: de aanroeper valt terug op ocr.py, en die op het beeld zelf."""


def ingeschakeld() -> bool:
    return (os.environ.get("MCP_TEXTRACT", "1") or "").strip().lower() not in (
        "0", "false", "nee", "off", "")


def _client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:                       # pragma: no cover
        raise TekstherkenningNietBeschikbaar(f"boto3 ontbreekt: {exc}") from exc
    # Kort: een pagina duurt een paar seconden, dus tien is al ruim. En anders
    # dan bij het model is de terugval hier goedkoop — ocr.py staat klaar.
    return boto3.client("textract", region_name=REGIO,
                        config=Config(read_timeout=20, connect_timeout=5,
                                      retries={"max_attempts": 2,
                                               "mode": "adaptive"}))


def transcribeer(paginas: list[bytes], mimetype: str = "image/jpeg") -> dict:
    """
    Zet paginabeelden om in tekst met Textract.

    Geeft dezelfde vorm terug als ocr.transcribeer, zodat de aanroeper de twee
    motoren niet uit elkaar hoeft te houden: {"text", "model", ...}. Extra zijn
    "gemiddelde_zekerheid" en "lage_regels".
    """
    if not ingeschakeld():
        raise TekstherkenningNietBeschikbaar("MCP_TEXTRACT staat uit.")
    if not paginas:
        raise TekstherkenningNietBeschikbaar("Geen pagina's om te lezen.")
    if len(paginas) > MAX_PAGINAS:
        raise TekstherkenningNietBeschikbaar(
            f"{len(paginas)} pagina's is te veel voor één aanroep "
            f"(maximaal {MAX_PAGINAS}).")

    klant = _client()
    stukken: list[str] = []
    alle_scores: list[float] = []
    lage_regels: list[tuple[str, float]] = []

    for nummer, beeld in enumerate(paginas, start=1):
        try:
            antwoord = klant.detect_document_text(Document={"Bytes": beeld})
        except Exception as exc:                     # noqa: BLE001 — altijd terugvallen
            logger.warning("[MCP] Textract mislukte op pagina %s (%s: %s)",
                           nummer, type(exc).__name__, exc)
            raise TekstherkenningNietBeschikbaar(str(exc)) from exc

        lijnen = [b for b in antwoord.get("Blocks", [])
                  if b.get("BlockType") == "LINE"]
        if len(paginas) > 1:
            stukken.append(f"--- pagina {nummer} ---")
        for b in lijnen:
            stukken.append(b.get("Text", ""))
            zekerheid = float(b.get("Confidence") or 0)
            alle_scores.append(zekerheid)
            if zekerheid < LAGE_REGEL_GRENS:
                lage_regels.append((b.get("Text", ""), round(zekerheid, 1)))

    tekst = "\n".join(s for s in stukken if s).strip()
    if not tekst:
        raise TekstherkenningNietBeschikbaar("Textract vond geen tekst.")

    gemiddeld = sum(alle_scores) / len(alle_scores) if alle_scores else 0.0
    if gemiddeld < DREMPEL_ZEKERHEID:
        # Geen fout maar een oordeel: dit document is te slecht gescand om hier
        # te lezen, dus laat het model het maar doen.
        raise TekstherkenningNietBeschikbaar(
            f"gemiddelde zekerheid {gemiddeld:.1f}% ligt onder "
            f"{DREMPEL_ZEKERHEID:.0f}%")

    # Dezelfde mod-97-toets als bij het model. Textract verzint geen IBAN's,
    # maar een slecht gescande wél verkeerd lezen kan hij prima — en een
    # plausibel verkeerde IBAN is het gevaarlijkste wat hier uit kan komen.
    from .ocr import _maskeer_valse_ibans
    tekst, gemaskeerd = _maskeer_valse_ibans(tekst)

    logger.info("[MCP] Textract: %s pagina('s), %s regels, gem. zekerheid "
                "%.1f%%, %s regel(s) onder %.0f%%%s",
                len(paginas), len(alle_scores), gemiddeld, len(lage_regels),
                LAGE_REGEL_GRENS,
                f", {gemaskeerd} onverifieerbare IBAN(s) gemaskeerd"
                if gemaskeerd else "")

    return {
        "text": tekst,
        "model": MODEL,
        "gemiddelde_zekerheid": round(gemiddeld, 1),
        "lage_regels": lage_regels[:12],
        "ibans_gemaskeerd": gemaskeerd,
    }
