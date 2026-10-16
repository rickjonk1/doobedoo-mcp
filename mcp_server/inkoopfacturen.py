"""
mcp_server/inkoopfacturen.py — concept-inkoopfacturen invullen
===============================================================
Inkoopfacturen komen per e-mail binnen in Odoo en blijven achter als lege
concepten met de PDF eraan: geen leverancier, geen bedragen, geen regels.
Dat invullen is het handwerk dat deze tools ondersteunen.

Drie keuzes die het verschil maken tussen bruikbaar en gevaarlijk:

  1. INVULLEN, NIET AANMAKEN. Het concept bestaat al, met de juiste bijlage
     eraan. Een bestaand concept bijwerken kan niet leiden tot een dubbele
     boeking en houdt de koppeling met het document intact. Nieuwe facturen
     aanmaken doet deze module niet.

  2. AFLEIDEN, NIET VERZINNEN. Welke grootboekrekening bij een leverancier
     hoort is een boekhoudkundig oordeel, geen tekstherkenning.
     supplier_booking_history geeft terug wat er eerder voor deze leverancier
     is geboekt, met aantallen. Dat is navolgbaar voor een controlerend
     accountant; een rekening die plausibel klinkt is dat niet.

  3. Afboeken alleen op verzoek (`post`). Standaard blijft de factuur in
     concept staan; met `post` gaat hij naar 'Te controleren' in Odoo.

Een factuur is niet-vertrouwde invoer. Zodra tekstherkenning een schrijfactie
wordt, kan wie die factuur opstelt meebepalen wat er in de administratie
belandt — een regel als "boek op rekening X" of een afwijkend IBAN onderaan
het document is een bekende aanval, en bij een administratiekantoor is de
opbrengst een betaalopdracht. Daarom wordt een IBAN uit een document altijd
getoetst aan de bankrekeningen die al bij de crediteur staan, en maakt deze
module er nooit zelf een aan.
"""
from __future__ import annotations

import logging
import re

from .tenants import get_odoo_api

logger = logging.getLogger(__name__)

SCOPE_READ = "odoo:read"
SCOPE_WRITE = "odoo:write"

# Afwijking waarboven het totaal als "klopt niet" geldt. Twee cent vangt
# afrondingsverschillen; alles daarboven is een leesfout of een gemiste regel.
TOTAAL_TOLERANTIE = 0.02

_MOVE_VELDEN = ["id", "name", "state", "move_type", "ref", "partner_id",
                "invoice_date", "invoice_date_due", "amount_untaxed",
                "amount_tax", "amount_total", "journal_id", "currency_id",
                "invoice_source_email", "narration"]

# De wachtrij is een lijst, geen dossier. Een leeg concept heeft per definitie
# geen bedragen, geen leverancier en geen datum — die velden komen allemaal als
# false of 0 terug en vullen alleen de context. `narration` is bovendien een
# vrij tekstveld met HTML erin en kan losse regels van honderden tekens geven.
# Wie het volledige record wil, haalt het op met get_record.
_LIJST_VELDEN = ["id", "name", "state", "ref", "partner_id", "invoice_date",
                 "invoice_date_due", "amount_total", "journal_id",
                 "invoice_source_email"]

# Waarden die niets toevoegen zodra je weet dat het om een leeg concept gaat.
_LEEG = (False, None, 0, 0.0, "", "/")


def _zonder_lege_velden(rij: dict) -> dict:
    """Laat weg wat leeg is, maar houd altijd het id en de bijlagen."""
    uit = {k: v for k, v in rij.items() if k in ("id", "attachments") or v not in _LEEG}
    return uit


def _api(args):
    return get_odoo_api((args or {}).get("database") or "")


def _vereist_int(args, sleutel):
    v = (args or {}).get(sleutel)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"`{sleutel}` is verplicht (geheel getal).")
    return v


def _normaliseer_iban(waarde: object) -> str:
    """IBAN's staan in Odoo met spaties; vergelijken doe je zonder."""
    return re.sub(r"[^A-Z0-9]", "", str(waarde or "").upper())


# ─── Lezen ───────────────────────────────────────────────────────────────────

def _list_draft_bills(args, ctx):
    """
    Concept-inkoopfacturen die nog ingevuld moeten worden.

    Standaard alleen de écht lege (zonder factuurregels); dat is de wachtrij.
    Met alle=true komen ook concepten mee die al deels gevuld zijn.
    """
    api = _api(args)
    # Standaard 20 in plaats van 50: dit is een werkvoorraad die iemand stuk voor
    # stuk afwerkt, en elke rij die je meestuurt staat de rest van het gesprek in
    # de context. Wie er meer wil zien vraagt er expliciet om.
    limiet = max(1, min(200, int((args or {}).get("limit") or 20)))

    domein = [["move_type", "=", "in_invoice"], ["state", "=", "draft"]]
    if not (args or {}).get("alle"):
        domein.append(["invoice_line_ids", "=", False])

    # message_main_attachment_id komt mee voor het voorverwarmen hieronder; hij
    # gaat er verderop weer uit, want de agent heeft er niets aan.
    facturen = api.execute("account.move", "search_read", [domein],
                           {"fields": _LIJST_VELDEN + ["message_main_attachment_id"],
                            "limit": limiet, "order": "id desc"}) or []
    if not facturen:
        return {"count": 0, "bills": [],
                "hint": "Geen concept-inkoopfacturen die op invulling wachten."}

    # Bijlagen erbij: zonder document valt er niets in te vullen.
    ids = [f["id"] for f in facturen]
    bijlagen = api.execute(
        "ir.attachment", "search_read",
        [[["res_model", "=", "account.move"], ["res_id", "in", ids]]],
        {"fields": ["id", "name", "mimetype", "file_size", "res_id"]}) or []

    per_move: dict[int, list] = {}
    for b in bijlagen:
        per_move.setdefault(b["res_id"], []).append(
            {k: b[k] for k in ("id", "name", "mimetype", "file_size")})

    for f in facturen:
        f["attachments"] = per_move.get(f["id"], [])

    # Hier begint het echte tijdvoordeel. Dit is het moment waarop bekend is
    # welke facturen er aankomen, en het overtypen van een scan duurt 13 tot 24
    # seconden (zie voorverwarmen.py). Door dat nu in de achtergrond te starten
    # ligt de tekst klaar tegen de tijd dat de agent bij de factuur komt, in
    # plaats van dat hij er per factuur op wacht. Blokkeert niet en kan niet
    # mislukken: bij twijfel gebeurt er gewoon niets.
    from . import voorverwarmen
    voorverwarmen.start(api, facturen)

    zonder = [f["id"] for f in facturen if not f["attachments"]]
    for f in facturen:
        f.pop("message_main_attachment_id", None)
    facturen = [_zonder_lege_velden(f) for f in facturen]
    uit = {"count": len(facturen), "bills": facturen,
           "hint": ("Roep per factuur book_draft_bill aan: die doet het hele "
                    "werk op de server in één aanroep en schrijft alleen weg "
                    "wat door de controle komt. Wil je het zelf beoordelen, "
                    "gebruik dan prepare_draft_bill en daarna "
                    "update_draft_bill.")}
    if zonder:
        uit["zonder_bijlage"] = zonder
    return uit


def _supplier_booking_history(args, ctx):
    """
    Wat is er eerder voor deze leverancier geboekt?

    Geeft per grootboekrekening en btw-code hoe vaak die gebruikt is op
    geboekte inkoopfacturen van deze crediteur, met het laatste gebruik. Zo is
    een voorstel te onderbouwen met de eigen historie in plaats van met een
    aanname.
    """
    api = _api(args)
    partner_id = _vereist_int(args, "partner_id")

    # Odoo telt zelf al in de database. Tweehonderd regels over de lijn trekken
    # om er hier vijf getallen uit te rekenen was verspilde tijd; read_group
    # geeft de telling meteen.
    gesorteerd = _historie_per_rekening(
        api, partner_id, limiet=max(1, min(500, int((args or {}).get("limit") or 200))))

    if not gesorteerd:
        return {"partner_id": partner_id, "aantal_regels": 0, "accounts": [],
                "hint": ("Geen eerdere geboekte inkoopfacturen voor deze "
                         "leverancier. De rekening kan hier niet uit de "
                         "historie worden afgeleid; leg de keuze voor aan een "
                         "mens.")}

    return {"partner_id": partner_id,
            "aantal_regels": sum(r["aantal"] for r in gesorteerd),
            "accounts": gesorteerd,
            "hint": ("De bovenste combinatie is het vaakst gebruikt. Wijkt de "
                     "factuur daarvan af, leg dat dan voor aan een mens.")}


def _historie_per_rekening(api, partner_id: int, limiet: int = 200) -> list[dict]:
    """Wat er eerder voor deze leverancier is geboekt, per rekening en btw-code.

    Groeperen in plaats van tweehonderd losse regels ophalen en ze hier
    tellen. Odoo telt zelf al in de database; die tweehonderd regels over de
    lijn trekken om er vijf getallen uit te rekenen was verspilde tijd en
    verspild geheugen. De voorbeeldomschrijving komt er apart bij, want die
    past niet in een groepering.

    Het groeperen loopt via groeperen.py: Odoo 19 kent `read_group` niet meer
    en onze databases lopen door elkaar.
    """
    domein = [["move_id.partner_id", "=", partner_id],
              ["move_id.move_type", "=", "in_invoice"],
              ["move_id.state", "=", "posted"],
              ["display_type", "=", "product"]]

    from .groeperen import groepeer
    groepen = groepeer(api, "account.move.line", domein,
                       ["account_id", "tax_ids"],
                       ["price_subtotal:sum", "date:max"]) or []

    # De voorbeeldomschrijving past niet in een groepering en is wél waardevol:
    # hij laat zien hóé deze leverancier bij deze klant beschreven wordt
    # ("Bekleding zijpaneel - Pakbon 295691"). Eén kleine tweede vraag met twee
    # velden is nog altijd een fractie van de tweehonderd volle regels die hier
    # eerst overheen gingen.
    voorbeelden: dict = {}
    try:
        for r in api.execute("account.move.line", "search_read", [domein],
                             {"fields": ["account_id", "name"],
                              "limit": max(1, min(100, limiet)),
                              "order": "date desc"}) or []:
            rek = r.get("account_id")
            rek_id = rek[0] if isinstance(rek, (list, tuple)) else rek
            if rek_id is not None and rek_id not in voorbeelden and r.get("name"):
                voorbeelden[rek_id] = r["name"]
    except Exception as exc:                        # noqa: BLE001 — bijzaak
        logger.debug("[MCP] voorbeeldomschrijvingen ophalen mislukt: %s", exc)

    uit = []
    for g in groepen:
        rekening = g.get("account_id") or [None, ""]
        btw = g.get("tax_ids")
        # Een groepering geeft many2many terug als [id, naam] (Odoo 19) of als
        # één id (ouder), en False als er geen btw op zat. Een regel met twee
        # btw-codes is bij inkoop zeldzaam en valt dan in een eigen groep.
        codes = [btw[0]] if isinstance(btw, (list, tuple)) and btw else (
            [btw] if isinstance(btw, int) and not isinstance(btw, bool) else [])
        uit.append({
            "account_id": rekening[0] if isinstance(rekening, (list, tuple)) else rekening,
            "account_name": rekening[1] if isinstance(rekening, (list, tuple)) else "",
            "tax_ids": codes,
            "aantal": g.get("__count") or g.get("account_id_count") or 0,
            "laatst": g.get("date") or None,
            "bedrag": round(float(g.get("price_subtotal") or 0), 2),
            "voorbeeld_omschrijving": voorbeelden.get(
                rekening[0] if isinstance(rekening, (list, tuple)) else rekening),
        })
    uit.sort(key=lambda x: (-x["aantal"], x["laatst"] or ""))
    return uit[:25]


def _veelgebruikte_rekeningen(api, limiet: int = 40) -> list[dict]:
    """Waar deze administratie inkoopfacturen op boekt, ongeacht leverancier.

    Nodig voor de nieuwe leverancier: die heeft per definitie geen historie, en
    zonder kandidatenlijst blijft er niets over om uit te kiezen. Dit is geen
    volledige grootboekrekeningenlijst — die is honderden regels lang en vult
    alleen de context — maar wat er in de praktijk gebruikt wordt.
    """
    from .groeperen import groepeer
    groepen = groepeer(
        api, "account.move.line",
        [["move_id.move_type", "=", "in_invoice"],
         ["move_id.state", "=", "posted"],
         ["display_type", "=", "product"]],
        ["account_id"], ["balance:sum"]) or []
    rijen = []
    for g in groepen:
        rek = g.get("account_id")
        if not isinstance(rek, (list, tuple)) or not rek:
            continue
        rijen.append({"account_id": rek[0], "account_name": rek[1],
                      "aantal": g.get("__count") or 0})
    rijen.sort(key=lambda r: -r["aantal"])
    return rijen[:limiet]


# De btw-codes en de crediteurenlijst zijn niet factuurgebonden: ze zijn voor
# factuur 1 en factuur 89 hetzelfde. Ze staan hier als losse functies zodat ze
# door admincache heen kunnen — zie _prepare_draft_bill.
def _btw_codes(api) -> list[dict]:
    return api.execute("account.tax", "search_read",
                       [[["type_tax_use", "=", "purchase"]]],
                       {"fields": ["id", "name", "amount", "price_include"],
                        "limit": 60, "order": "sequence, id"}) or []


def _crediteuren(api) -> list[dict]:
    return api.execute("res.partner", "search_read",
                       [[["supplier_rank", ">", 0]]],
                       {"fields": ["id", "name", "vat"], "limit": 400,
                        "order": "supplier_rank desc"}) or []


def _kandidaat_rekeningen(historie: list[dict], veelgebruikt: list[dict]) -> list[dict]:
    """De rekeningen waaruit gekozen mag worden, sterkste bewijs bovenaan.

    Wat déze leverancier eerder kreeg staat vooraan — dat is het sterkste
    bewijs dat er is. Daarna wat de administratie in het algemeen gebruikt.
    """
    uit: dict[int, dict] = {}
    for r in historie:
        rid = r.get("account_id")
        if rid and rid not in uit:
            uit[rid] = {"account_id": rid, "account_name": r.get("account_name"),
                        "waarom": f"{r.get('aantal')}x eerder bij deze leverancier",
                        "tax_ids": r.get("tax_ids") or []}
    for r in veelgebruikt:
        rid = r.get("account_id")
        if rid and rid not in uit:
            uit[rid] = {"account_id": rid, "account_name": r.get("account_name"),
                        "waarom": f"{r.get('aantal')}x in deze administratie"}
    return list(uit.values())


def _herkans_mislukte_deelvragen(uitkomsten: dict, opdrachten: dict) -> None:
    """Probeer mislukte deelvragen nog één keer, nu één voor één.

    Parallel omvallen en serieel slagen is geen toeval: wat omvalt, valt om
    doordat er te veel tegelijk gebeurde. Alleen de deelvragen waarzonder de
    agent vastloopt — het document hoort er niet bij, want dat mislukt om een
    inhoudelijke reden en wordt niet beter van herhalen.
    """
    from .parallel import _Fout

    for naam in ("btw_codes", "veelgebruikt", "historie", "crediteuren"):
        if not isinstance(uitkomsten.get(naam), _Fout):
            continue
        opdracht = opdrachten.get(naam)
        if not callable(opdracht):
            continue
        try:
            uitkomsten[naam] = opdracht()
            logger.info("[MCP] deelvraag %s slaagde alsnog bij de herkansing", naam)
        except Exception as exc:                    # noqa: BLE001 — blijft een gat
            logger.warning("[MCP] deelvraag %s mislukte ook serieel: %s", naam, exc)


def _prepare_draft_bill(args, ctx):
    """
    Alles wat er nodig is om één concept in te vullen, in één aanroep.

    Dit is de snelheidswinst, en die zit niet in onze server maar in het aantal
    beurten. Wie het document leest, dan de historie opvraagt, dan de
    rekeningen zoekt, dan de btw-codes zoekt en dan pas invult, doet vijf
    aanroepen — en tussen elke aanroep zit een volledige denkbeurt van het
    model bij de client. Dat is waar de minuten heen gaan; onze kant van de
    lijn is milliseconden.

    Hier gebeurt alles tegelijk: de vier Odoo-vragen lopen parallel (zie
    parallel.py) en het document wordt in dezelfde golf gelezen. De agent houdt
    twee aanroepen per factuur over: deze, en update_draft_bill.
    """
    from .admincache import via_cache
    from .documents import hoofdbijlage_lezen
    from .parallel import fout_tekst, gelijktijdig, uitkomst

    api = _api(args)
    move_id = _vereist_int(args, "move_id")

    rijen = api.execute("account.move", "read", [[move_id]],
                        {"fields": _MOVE_VELDEN + ["invoice_line_ids",
                                                   "message_main_attachment_id"]})
    if not rijen:
        raise ValueError(f"Factuur {move_id} bestaat niet.")
    move = rijen[0]
    if move.get("move_type") != "in_invoice":
        raise ValueError(f"Boeking {move_id} is een {move.get('move_type')} en "
                         f"geen inkoopfactuur.")
    if move.get("state") != "draft":
        raise ValueError(f"Boeking {move_id} staat op {move.get('state')!r} en is "
                         f"dus al geboekt.")

    partner = move.get("partner_id")
    partner_id = partner[0] if isinstance(partner, (list, tuple)) else None

    # Drie van de vier deelvragen gaan niet over déze factuur maar over de hele
    # administratie, en werden toch per factuur opnieuw gesteld. Dat was niet
    # alleen verspilling: het was ook precies wat Odoo deed afknijpen. In de
    # wachtrij van een klant kwam op 11 september bij ELKE factuur een 429 terug
    # op een van deze drie, met een lege btw-codelijst of crediteurenlijst als
    # gevolg — en dan kan de agent niets meer invullen.
    db_naam = getattr(api, "db", "?")
    opdrachten = {
        "document": lambda: hoofdbijlage_lezen(api, move),
        "btw_codes": lambda: via_cache(db_naam, "btw_codes",
                                       lambda: _btw_codes(api)),
        "veelgebruikt": lambda: via_cache(db_naam, "veelgebruikt",
                                          lambda: _veelgebruikte_rekeningen(api)),
    }
    if partner_id:
        opdrachten["historie"] = lambda: _historie_per_rekening(api, partner_id)
    else:
        # Geen crediteur op het concept: dan is dát de eerste horde, en zonder
        # kandidatenlijst moet de agent gaan zoeken — nog een beurt.
        opdrachten["crediteuren"] = lambda: via_cache(db_naam, "crediteuren",
                                                      lambda: _crediteuren(api))

    uitkomsten = gelijktijdig(api, opdrachten)

    # Een deelvraag die alsnog omvalt, krijgt één seriële herkansing. Dat is
    # geen dubbelop met de 429-herkansing in odoo_api: die vangt drukte op,
    # deze vangt de rest. Het alternatief is een leeg veld teruggeven, en dat
    # kost gegarandeerd een hele beurt van de agent of een mens.
    _herkans_mislukte_deelvragen(uitkomsten, opdrachten)

    document = uitkomst(uitkomsten.get("document"), {}) or {}

    # Leverde de hoofdbijlage geen tekst op, dan is de kans groot dat de échte
    # factuur een ándere bijlage is. Dat gebeurt vooral bij bonnetjes die per
    # mail binnenkomen: `message_main_attachment_id` wijst dan naar het logo uit
    # de handtekening — een BMP van 264 bij 185 — terwijl de foto van de bon er
    # los naast hangt. Zonder deze lijst gaat de agent zelf op zoek, en in de
    # praktijk slaat hij de factuur dan over.
    #
    # Alleen op dit pad, want het kost een extra vraag aan Odoo en in het
    # normale geval voegt hij niets toe.
    if not (document.get("text") or "").strip():
        hoofd = move.get("message_main_attachment_id")
        hoofd_id = hoofd[0] if isinstance(hoofd, (list, tuple)) and hoofd else hoofd
        try:
            andere = [b for b in api.execute(
                "ir.attachment", "search_read",
                [[["res_model", "=", "account.move"], ["res_id", "=", move_id]]],
                {"fields": ["id", "name", "mimetype", "file_size"],
                 "limit": 20, "order": "file_size desc"}) or []
                if b["id"] != hoofd_id]
            if andere:
                uit_bijlagen = andere
            else:
                uit_bijlagen = None
        except Exception as exc:                    # noqa: BLE001 — bijzaak
            logger.debug("[MCP] overige bijlagen bij %s ophalen mislukt: %s",
                         move_id, exc)
            uit_bijlagen = None
    else:
        uit_bijlagen = None

    historie = uitkomst(uitkomsten.get("historie"), []) or []
    btw_codes = uitkomst(uitkomsten.get("btw_codes"), []) or []
    crediteuren = uitkomst(uitkomsten.get("crediteuren"), []) or []
    veelgebruikt = uitkomst(uitkomsten.get("veelgebruikt"), []) or []

    uit = {
        "bill": _zonder_lege_velden(
            {k: move.get(k) for k in _LIJST_VELDEN + ["amount_untaxed", "amount_tax"]}),
        "move_id": move_id,
        "heeft_regels": bool(move.get("invoice_line_ids")),
        "document": document,
        "supplier_history": historie,
        "accounts": _kandidaat_rekeningen(historie, veelgebruikt),
        "tax_codes": btw_codes,
        "creditor_missing": not partner_id,
    }
    if not partner_id:
        uit["creditor_candidates"] = _kandidaten_uit_tekst(
            crediteuren, f"{document.get('text') or ''}\n{document.get('name') or ''}")

    if uit_bijlagen:
        uit["andere_bijlagen"] = uit_bijlagen
        uit["bijlage_hint"] = (
            "De hoofdbijlage leverde geen tekst op. Bij een bon uit de mail is "
            "dat vaak een logo uit de handtekening; de echte factuur staat dan "
            "in een van deze bijlagen. Lees ze met read_document (de grootste "
            "eerst) voordat je deze factuur overslaat.")

    problemen = {naam: fout_tekst(w) for naam, w in uitkomsten.items()
                 if fout_tekst(w)}
    if problemen:
        uit["deelvragen_mislukt"] = problemen

    uit["hint"] = (
        "Alles staat hierboven: de factuurtekst, wat er eerder voor deze "
        "leverancier is geboekt (bovenste combinatie is het vaakst gebruikt) "
        "en de btw-codes. Vul nu in met update_draft_bill. Klopt iets niet of "
        "ontbreekt de leverancier, leg het dan voor aan een mens — zoek niet "
        "verder rond. Doe je dit voor een hele wachtrij, gebruik dan "
        "book_draft_bill in plaats van deze twee aanroepen: dat scheelt een "
        "denkbeurt per factuur.")
    return uit


# Namen korter dan dit leveren toevalstreffers op ("Jan", "BV"), en die zijn
# erger dan geen treffer: ze zien er overtuigend uit.
_MIN_NAAMLENGTE = 5
_RECHTSVORMEN = {"bv", "b", "v", "nv", "n", "vof", "cv", "gmbh", "ltd", "limited",
                 "sarl", "sa", "ag", "plc", "inc", "llc", "kg", "holding",
                 "beheer", "nederland", "netherlands", "the"}


def _kaal(tekst: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (tekst or "").lower()).split())


def _kandidaten_uit_tekst(alle: list[dict], tekst: str) -> list[dict]:
    """Welke bekende leveranciers staan er letterlijk in dit document?

    Deterministisch en gratis: de namen hebben we toch al in de hand. "Voorbeeld
    Diagnostics" op de factuur en "Voorbeeld Diagnostics BV" in
    Odoo zijn dezelfde leverancier, dus de rechtsvorm telt niet mee — mits wat
    overblijft nog onderscheidend is.
    """
    kaal = _kaal(tekst)
    if not kaal:
        return []
    uit = []
    for rij in alle:
        naam = _kaal(rij.get("name") or "")
        if len(naam) < _MIN_NAAMLENGTE:
            continue
        kern = " ".join(w for w in naam.split() if w not in _RECHTSVORMEN)
        treffer = naam in kaal or (
            kern and (len(kern.split()) >= 2 or len(kern) >= 8) and kern in kaal)
        if treffer:
            uit.append({"id": rij["id"], "name": rij.get("name"),
                        "vat": rij.get("vat") or None,
                        "waarom": "staat in de documenttekst"})
    return uit[:10]


# ─── Schrijven ───────────────────────────────────────────────────────────────

def _controleer_concept(api, move_id: int) -> dict:
    rijen = api.execute("account.move", "read", [[move_id]],
                        {"fields": _MOVE_VELDEN + ["invoice_line_ids"]})
    if not rijen:
        raise ValueError(f"Factuur {move_id} bestaat niet.")
    move = rijen[0]

    if move.get("move_type") != "in_invoice":
        raise ValueError(
            f"Factuur {move_id} is een {move.get('move_type')}, geen "
            f"inkoopfactuur. Deze tool werkt alleen op inkoopfacturen.")
    if move.get("state") != "draft":
        raise ValueError(
            f"Factuur {move_id} staat op '{move.get('state')}' en is dus al "
            f"geboekt. Een geboekte factuur wordt hier nooit gewijzigd.")
    return move


def _controleer_dubbel(api, move_id: int, partner_id: int, ref: str) -> None:
    """
    Zelfde leverancier én zelfde factuurnummer = dezelfde factuur.

    Dit is de controle die dubbel boeken tegenhoudt. Odoo waarschuwt hier zelf
    ook over, maar pas bij het posten; dan staat de fout er al in.
    """
    if not ref or not partner_id:
        return
    bestaand = api.execute(
        "account.move", "search_read",
        [[["move_type", "=", "in_invoice"], ["partner_id", "=", partner_id],
          ["ref", "=", ref], ["id", "!=", move_id]]],
        {"fields": ["id", "name", "state", "amount_total"], "limit": 5}) or []
    if bestaand:
        omschrijving = ", ".join(
            f"{b.get('name') or b['id']} ({b.get('state')})" for b in bestaand)
        raise ValueError(
            f"Factuurnummer {ref!r} staat al op deze leverancier: "
            f"{omschrijving}. Niet ingevuld, om dubbel boeken te voorkomen.")


def _controleer_iban(api, partner_id: int, iban: str) -> dict:
    """
    Een IBAN uit een document moet matchen met wat er bij de crediteur staat.

    Dit is de belangrijkste vangrail. Een factuur is niet-vertrouwde invoer:
    wie hem opstelt bepaalt wat erop staat. Een afwijkend rekeningnummer is de
    klassieke factuurfraude, en een automatisch aangemaakte bankrekening zou
    die fraude vastleggen in de stamgegevens.
    """
    gezocht = _normaliseer_iban(iban)
    if not gezocht:
        raise ValueError("Het opgegeven IBAN is leeg of onleesbaar.")

    rekeningen = api.execute(
        "res.partner.bank", "search_read", [[["partner_id", "=", partner_id]]],
        {"fields": ["id", "acc_number"], "limit": 50}) or []

    for r in rekeningen:
        if _normaliseer_iban(r.get("acc_number")) == gezocht:
            return {"partner_bank_id": r["id"], "acc_number": r["acc_number"]}

    bekend = [r.get("acc_number") for r in rekeningen] or ["(geen)"]
    raise ValueError(
        f"Het IBAN op het document ({iban}) komt niet voor bij deze "
        f"crediteur. Bekend: {', '.join(str(b) for b in bekend)}. "
        f"Niet ingevuld — een afwijkend rekeningnummer op een factuur is een "
        f"bekende vorm van fraude en hoort door een mens gecontroleerd te "
        f"worden. Klopt het wel, voeg de rekening dan eerst bij de crediteur toe."
    )


def _bouw_regels(args) -> list:
    ruw = (args or {}).get("lines")
    if not isinstance(ruw, list) or not ruw:
        raise ValueError("`lines` is verplicht (lijst met factuurregels).")

    commandos = []
    for i, regel in enumerate(ruw, 1):
        if not isinstance(regel, dict):
            raise ValueError(f"Regel {i} moet een object zijn.")
        account_id = regel.get("account_id")
        if isinstance(account_id, bool) or not isinstance(account_id, int):
            raise ValueError(f"Regel {i} mist `account_id` (geheel getal).")
        try:
            prijs = float(regel.get("price_unit"))
        except (TypeError, ValueError):
            raise ValueError(f"Regel {i} mist een geldige `price_unit`.")
        try:
            aantal = float(regel.get("quantity", 1) or 1)
        except (TypeError, ValueError):
            raise ValueError(f"Regel {i} heeft een ongeldige `quantity`.")

        waarden = {"account_id": account_id, "quantity": aantal,
                   "price_unit": prijs,
                   "name": str(regel.get("name") or "").strip() or "/"}
        btw = regel.get("tax_ids")
        if btw is not None:
            if not isinstance(btw, list) or any(not isinstance(t, int) or isinstance(t, bool)
                                                for t in btw):
                raise ValueError(f"Regel {i}: `tax_ids` moet een lijst id's zijn.")
            waarden["tax_ids"] = [[6, 0, btw]]
        commandos.append([0, 0, waarden])
    return commandos


# Woorden waarmee een document zichzelf als creditnota aankondigt. Bewust kort
# en talig: het gaat om de kop van het document, niet om een zin ergens in de
# voorwaarden.
_CREDIT_WOORDEN = ("creditnota", "credit nota", "creditfactuur", "credit note",
                   "creditnummer", "gutschrift", "avoir")


def lijkt_creditnota(tekst: str) -> bool:
    kaal = " ".join((tekst or "").lower().split())
    return any(w in kaal for w in _CREDIT_WOORDEN)


# Een zelf opgemaakt inkoopdocument. Een klant koopt een auto van een
# particulier; die schrijft geen factuur, dus maakt de klant het stuk zelf op.
# Het briefhoofd is dan de administratie zélf, en de crediteur staat verderop
# achter "Ingekocht van:".
#
# Dat is geen leesfout maar een documenttype. Textract las factuur 18570
# correct — VENDOR_NAME = de klant, want de klant ís de afzender —
# maar wie dat veld naar partner_id doorzet, boekt de administratie als haar
# eigen leverancier. En de echte verkoper is een particulier, dus meestal
# helemaal geen crediteur in het systeem.
#
# Alleen woorden die bij dit documenttype horen en nergens anders. "marge" of
# "margeregeling" staat er met opzet NIET bij: dat komt op gewone
# margefacturen ook voor, en een valse weigering kost net zo goed handwerk.
_ZELF_OPGEMAAKT_WOORDEN = ("inkoopverklaring", "ingekocht van", "self-billing",
                           "selfbilling", "self billing", "zelffacturering")


def lijkt_zelf_opgemaakt(tekst: str) -> bool:
    """Is dit een document dat de administratie zélf heeft opgemaakt?"""
    kaal = " ".join((tekst or "").lower().split())
    return any(w in kaal for w in _ZELF_OPGEMAAKT_WOORDEN)


def _weiger_creditnota(move_id: int, args, regels) -> None:
    """Een creditnota mag nooit als inkoopfactuur worden ingevuld.

    Dit is geen theoretisch geval. In de wachtrij van een klant zaten
    creditnota's van De Banden Express, terwijl álle concepten in Odoo als
    `in_invoice` binnenkomen — de e-mailinvoer kijkt niet naar de inhoud. Vul je
    zo'n concept met positieve regels, dan staat er een inkoopfactuur waar een
    creditering hoort, en dat is niet zichtbaar fout: het bedrag klopt, het
    teken niet. Dan betaal je een leverancier die jou geld terug moest geven.

    Twee kanten op dus:

      * negatieve bedragen op een `in_invoice` — dat is iemand die een
        creditnota in dit concept probeert te persen. Geweigerd; in Odoo hoort
        er dan een `in_refund` te staan;
      * een document dat zichzelf creditnota noemt — geweigerd, ook als de
        bedragen positief zijn ingevuld. Juist die vorm is gevaarlijk, want er
        is dan niets aan de cijfers te zien.

    Beide gevallen horen bij een mens: het concepttype omzetten is geen
    invulactie maar een beslissing.
    """
    som = 0.0
    for commando in regels:
        waarden = commando[2] if len(commando) > 2 and isinstance(commando[2], dict) else {}
        try:
            som += float(waarden.get("price_unit") or 0) * float(waarden.get("quantity") or 1)
        except (TypeError, ValueError):
            continue

    verwacht = (args or {}).get("amount_total_expected")
    try:
        verwacht = float(verwacht) if verwacht is not None else None
    except (TypeError, ValueError):
        verwacht = None

    if som < 0 or (verwacht is not None and verwacht < 0):
        raise ValueError(
            f"Concept {move_id} is een inkoopfactuur (in_invoice), maar de "
            f"bedragen zijn negatief. Dat is een creditnota, en die hoort in "
            f"Odoo als 'in_refund' te staan. Er is niets ingevuld — laat een "
            f"mens het concepttype omzetten.")

    tekst = " ".join(str((args or {}).get(v) or "") for v in ("ref", "document_tekst"))
    for commando in regels:
        waarden = commando[2] if len(commando) > 2 and isinstance(commando[2], dict) else {}
        tekst += " " + str(waarden.get("name") or "")
    if lijkt_creditnota(tekst):
        raise ValueError(
            f"Concept {move_id} staat als inkoopfactuur in Odoo, maar het "
            f"document noemt zichzelf een creditnota. Invullen met positieve "
            f"bedragen zou een creditering als inkoop vastleggen — het bedrag "
            f"klopt dan wel en het teken niet. Er is niets ingevuld; laat een "
            f"mens het concepttype omzetten naar 'in_refund'.")


def _update_draft_bill(args, ctx):
    """
    Vul een bestaand concept-inkoopfactuur in.

    Werkt alleen op een concept-inkoopfactuur en weigert
    zodra iets niet klopt: een dubbel factuurnummer, een IBAN dat niet bij de
    crediteur staat, of regels die niet optellen tot het bedrag op het
    document.
    """
    api = _api(args)
    move_id = _vereist_int(args, "move_id")
    move = _controleer_concept(api, move_id)

    if move.get("invoice_line_ids") and not (args or {}).get("vervang_regels"):
        raise ValueError(
            f"Factuur {move_id} heeft al {len(move['invoice_line_ids'])} "
            f"regel(s). Zet `vervang_regels` op true om ze te vervangen.")

    partner_id = (args or {}).get("partner_id")
    if partner_id is None:
        huidig = move.get("partner_id")
        partner_id = huidig[0] if isinstance(huidig, (list, tuple)) and huidig else None
    if not partner_id:
        raise ValueError(
            "`partner_id` is verplicht: de factuur heeft nog geen leverancier. "
            "Zoek de crediteur op met search_records op res.partner.")

    ref = str((args or {}).get("ref") or "").strip()
    if not ref:
        raise ValueError("`ref` is verplicht (het factuurnummer van de leverancier).")
    _controleer_dubbel(api, move_id, partner_id, ref)

    regels = _bouw_regels(args)
    _weiger_creditnota(move_id, args, regels)

    waarden = {"partner_id": partner_id, "ref": ref}
    for veld, sleutel in (("invoice_date", "invoice_date"),
                          ("invoice_date_due", "invoice_date_due"),
                          ("payment_reference", "payment_reference")):
        waarde = (args or {}).get(sleutel)
        if waarde:
            waarden[veld] = str(waarde)

    # De boekhouddatum volgt de factuurdatum. `date` heeft in Odoo geen compute
    # en is verplicht, dus hij wordt bij het aanmaken van het concept op de dag
    # van binnenkomst gezet en blijft daar staan; de onchange die dat in de
    # schermen rechttrekt bestaat via de API niet. Zonder deze regel landt een
    # factuur van 30 augustus die op 2 september binnenkwam in september.
    if waarden.get("invoice_date"):
        waarden["date"] = waarden["invoice_date"]

    bank = None
    if (args or {}).get("iban"):
        bank = _controleer_iban(api, partner_id, (args or {})["iban"])
        waarden["partner_bank_id"] = bank["partner_bank_id"]

    if move.get("invoice_line_ids"):
        waarden["invoice_line_ids"] = ([[5, 0, 0]] + regels)   # 5 = alles wissen
    else:
        waarden["invoice_line_ids"] = regels

    api.execute("account.move", "write", [[move_id], waarden])

    # Odoo rekent de btw zelf uit; pas na het schrijven is het totaal bekend.
    na = api.execute("account.move", "read", [[move_id]],
                     {"fields": _MOVE_VELDEN})[0]

    uit = {
        "move_id": move_id,
        "state": na.get("state"),
        "ref": na.get("ref"),
        "partner": na.get("partner_id"),
        "amount_untaxed": na.get("amount_untaxed"),
        "amount_tax": na.get("amount_tax"),
        "amount_total": na.get("amount_total"),
        "regels_geschreven": len(regels),
        "geboekt": False,
        "hint": "De factuur staat in concept.",
    }
    if bank:
        uit["partner_bank"] = bank["acc_number"]

    verwacht = (args or {}).get("amount_total_expected")
    if verwacht is not None:
        try:
            verwacht = float(verwacht)
        except (TypeError, ValueError):
            raise ValueError("`amount_total_expected` moet een getal zijn.")
        verschil = round(float(na.get("amount_total") or 0) - verwacht, 2)
        uit["amount_total_expected"] = verwacht
        uit["verschil"] = verschil
        if abs(verschil) > TOTAAL_TOLERANTIE:
            uit["klopt_niet"] = True
            uit["waarschuwing"] = (
                f"Het totaal in Odoo ({na.get('amount_total')}) wijkt "
                f"{verschil:+.2f} af van het bedrag op het document "
                f"({verwacht:.2f}). Waarschijnlijk een gemiste regel of een "
                f"verkeerde btw-code. De factuur is niet afgeboekt; laat een "
                f"mens ernaar kijken.")
            # Deze waarschuwing blijft bewust in het antwoord en gaat NIET als
            # notitie de factuur op. Dat gebeurde eerder wel, in `narration` —
            # en dat veld heet in Odoo "Terms and Conditions" en komt op de
            # afdruk terecht. De bedoeling was goed (ook zichtbaar voor wie de
            # factuur in Odoo opent) maar het veld was verkeerd: de klant zag
            # het mee. Wil je het daar alsnog zien, dan hoort dat in de chatter
            # of als activiteit, niet in een veld dat meeprint.

    # ── Afboeken ────────────────────────────────────────────────────────────
    # Standaard uit. Alleen op uitdrukkelijk verzoek, en alleen als er verder
    # niets mis is: elke grendel hierboven is al gepasseerd en het totaal moet
    # kloppen. Na het posten blijft `checked` op false staan — dat is in Odoo
    # precies de stand "Te controleren", en die laten we met opzet zo.
    if (args or {}).get("post"):
        if uit.get("klopt_niet"):
            uit["niet_geboekt_reden"] = (
                "Niet afgeboekt: het totaal wijkt af van het document.")
        else:
            api.execute("account.move", "action_post", [[move_id]])
            nu = api.execute("account.move", "read", [[move_id]],
                             {"fields": ["state", "name"]})[0]
            uit["state"] = nu.get("state")
            uit["naam"] = nu.get("name")
            uit["geboekt"] = nu.get("state") == "posted"
            uit["hint"] = ("Afgeboekt en klaargezet ter controle."
                           if uit["geboekt"] else
                           "Afboeken gevraagd, maar de factuur staat niet op posted.")
            logger.info("[MCP] inkoopfactuur %s afgeboekt (%s)", move_id, nu.get("name"))

    logger.info("[MCP] concept-inkoopfactuur %s ingevuld (ref=%s, %s regels)",
                move_id, ref, len(regels))
    return uit


def _book_draft_bill(args, ctx):
    """
    Eén factuur, één aanroep: voorstellen, natellen en wegschrijven.

    Dit haalt de laatste denkbeurt uit het pad. prepare_draft_bill bracht het
    van vijf aanroepen naar twee; hier doet de server ook het voorstel, zodat
    de agent er één overhoudt. Daarmee ligt de duur vast — documenttekst
    ophalen, één modelaanroep, één schrijfactie — in plaats van af te hangen
    van hoe vaak het model besluit nog iets op te zoeken.

    Er wordt alleen geschreven als het voorstel door de controle komt: elke
    rekening en btw-code moet in de meegegeven lijst hebben gestaan, en de
    regels moeten optellen tot het factuurtotaal. Twijfelt het model zelf, dan
    gaat de factuur naar een mens en blijft het concept onaangeroerd.

    Afboeken gebeurt alleen als de aanroeper erom vraagt met `post`, en nooit
    als het totaal afwijkt van het document.
    """
    from . import boekvoorstel as B

    move_id = _vereist_int(args, "move_id")
    gegevens = _prepare_draft_bill(args, ctx)

    try:
        voorstel, verbruik = B.stel_voor(gegevens)
    except B.BoekvoorstelNietBeschikbaar as reden:
        # Geen storing voor de agent: hij kan het gewoon zelf doen met wat hier
        # al opgehaald is. Dat scheelt hem een ronde en maakt het verschil
        # zichtbaar in plaats van een kale fout.
        logger.warning("[MCP] boekvoorstel voor %s niet beschikbaar: %s",
                       move_id, reden)
        gegevens["voorstel_mislukt"] = str(reden)
        gegevens["hint"] = (
            "Het voorstel kon niet op de server gemaakt worden. Alles wat je "
            "nodig hebt staat hierboven; vul zelf in met update_draft_bill.")
        return gegevens

    bezwaren = B.controleer(voorstel, gegevens)
    mag, reden = B.mag_schrijven(voorstel, bezwaren, gegevens)

    uit = {
        "move_id": move_id,
        "voorstel": voorstel,
        "bezwaren": bezwaren,
        "verbruik": verbruik,
        "document": gegevens.get("document"),
        "geschreven": False,
        "geboekt": False,
    }

    if not mag:
        uit["reden"] = reden
        uit["hint"] = (
            "Er is niets geschreven; het concept staat onaangeroerd in Odoo. "
            "Klopt het voorstel volgens jou wel, vul het dan zelf in met "
            "update_draft_bill — dan ligt de keuze bij jou en niet bij een "
            "controle die je overrulet zonder het te zeggen.")
        logger.info("[MCP] factuur %s niet weggeschreven: %s", move_id, reden)
        return uit

    schrijf = {
        "database": (args or {}).get("database"),
        "move_id": move_id,
        "ref": voorstel.get("ref"),
        "lines": voorstel.get("lines"),
        "amount_total_expected": voorstel.get("amount_total"),
    }
    # Afboeken gaat alleen mee als erom gevraagd is. We zijn hier voorbij elke
    # grendel — mag_schrijven eist geen bezwaren, oordeel 'klaar', geen twijfels
    # en een leeg concept — en update_draft_bill weigert alsnog zodra het totaal
    # afwijkt van het document.
    if (args or {}).get("post"):
        schrijf["post"] = True
    if gegevens.get("creditor_missing"):
        schrijf["partner_id"] = voorstel.get("partner_id")
    for veld in ("invoice_date", "invoice_date_due"):
        if voorstel.get(veld):
            schrijf[veld] = voorstel[veld]
    # De IBAN gaat bewust NIET mee als schrijfopdracht. update_draft_bill toetst
    # hem aan de rekeningen die al bij de crediteur staan en maakt er nooit een
    # aan; een afwijkend rekeningnummer onderaan een factuur is de klassieke
    # factuurfraude. Hij komt wel terug in het antwoord, zodat een mens hem ziet.
    if voorstel.get("iban_op_factuur"):
        uit["iban_op_factuur"] = voorstel["iban_op_factuur"]

    uit["resultaat"] = _update_draft_bill(schrijf, ctx)
    uit["geschreven"] = True
    uit["geboekt"] = bool(uit["resultaat"].get("geboekt"))
    uit["reden"] = reden
    uit["hint"] = ("Ingevuld en afgeboekt; hij staat in Odoo klaar ter controle."
                   if uit["geboekt"] else
                   "Ingevuld en in concept gelaten. Staat er een waarschuwing "
                   "in `resultaat`, laat die dan eerst nakijken.")
    logger.info("[MCP] factuur %s ingevuld via boekvoorstel (%s regels, "
                "%s in / %s uit tokens)", move_id,
                len(voorstel.get("lines") or []), verbruik.get("input_tokens"),
                verbruik.get("output_tokens"))
    return uit


# ─── Registratie ─────────────────────────────────────────────────────────────

_DB = {"type": "string", "description": "Naam van de Odoo-database (zie list_databases)."}

_POST = {
    "type": "boolean",
    "description": ("Meteen afboeken als alles klopt. Standaard false — dan "
                    "blijft de factuur in concept staan. Er wordt nooit "
                    "afgeboekt als het totaal afwijkt van het document. Een "
                    "afgeboekte factuur komt in Odoo op 'Te controleren' "
                    "terecht; die stand blijft met opzet staan."),
}

INKOOPFACTUUR_TOOLS = [
    {
        "name": "book_draft_bill",
        "description": (
            "Vul één concept-inkoopfactuur in EEN AANROEP in: de server leest "
            "het document, haalt de historie van de leverancier op, stelt het "
            "voorstel zelf op en schrijft het weg als het door de controle "
            "komt. Dit is de snelle weg — gebruik hem voor de hele wachtrij. "
            "Elke gekozen rekening en btw-code wordt nagerekend tegen wat "
            "werkelijk bestaat, en de regels moeten optellen tot het "
            "factuurtotaal; klopt er iets niet of twijfelt de agent, dan wordt "
            "er NIETS geschreven en komt het voorstel met de reden terug. "
            "Met `post` kun je hem meteen laten afboeken; zonder blijft hij "
            "in concept staan."),
        "scope": SCOPE_WRITE,
        "handler": _book_draft_bill,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB,
                "move_id": {"type": "integer",
                            "description": "Id van het concept (zie list_draft_bills)."},
                "post": _POST,
            },
            "required": ["database", "move_id"],
        },
    },
    {
        "name": "prepare_draft_bill",
        "description": (
            "ALLES wat nodig is om één concept-inkoopfactuur in te vullen, in "
            "één aanroep: de tekst van de bijlage, wat er eerder voor deze "
            "leverancier is geboekt (per grootboekrekening en btw-code, met "
            "aantallen), de btw-codes, en bij een ontbrekende crediteur de "
            "kandidaten die letterlijk in het document staan. Gebruik dit in "
            "plaats van read_document + supplier_booking_history + zoeken naar "
            "rekeningen: dat zijn vier extra beurten per factuur. Daarna nog "
            "één aanroep: update_draft_bill."),
        "scope": SCOPE_READ,
        "handler": _prepare_draft_bill,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB,
                "move_id": {"type": "integer",
                            "description": "Id van het concept (zie list_draft_bills)."},
            },
            "required": ["database", "move_id"],
        },
    },
    {
        "name": "list_draft_bills",
        "description": ("Concept-inkoopfacturen die nog ingevuld moeten worden, "
                        "met hun bijlagen. Dit is de wachtrij: facturen die per "
                        "e-mail binnenkwamen en nog leeg zijn."),
        "scope": SCOPE_READ,
        "handler": _list_draft_bills,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB,
                "alle": {"type": "boolean",
                         "description": "Ook concepten die al regels hebben."},
                "limit": {"type": "integer", "description": "Max aantal (1-200, default 20)."},
            },
            "required": ["database"],
        },
    },
    {
        "name": "supplier_booking_history",
        "description": ("Welke grootboekrekeningen en btw-codes zijn eerder voor "
                        "deze leverancier gebruikt, met aantallen. Gebruik dit om "
                        "een rekening te ONDERBOUWEN in plaats van te kiezen op "
                        "wat plausibel lijkt."),
        "scope": SCOPE_READ,
        "handler": _supplier_booking_history,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB,
                "partner_id": {"type": "integer", "description": "Id van de crediteur."},
                "limit": {"type": "integer", "description": "Max regels (1-500, default 200)."},
            },
            "required": ["database", "partner_id"],
        },
    },
    {
        "name": "update_draft_bill",
        "description": ("Vul een bestaand concept-inkoopfactuur in: leverancier, "
                        "factuurnummer, datums en regels. Maakt nooit een nieuwe "
                        "factuur aan. De boekhouddatum volgt automatisch de "
                        "factuurdatum. Weigert bij een dubbel factuurnummer of "
                        "een IBAN dat niet bij de crediteur staat. Met `post` "
                        "wordt hij meteen afgeboekt; zonder blijft hij in "
                        "concept staan."),
        "scope": SCOPE_WRITE,
        "handler": _update_draft_bill,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB,
                "move_id": {"type": "integer",
                            "description": "Id van het concept (zie list_draft_bills)."},
                "partner_id": {"type": "integer",
                               "description": ("Crediteur. Verplicht als de factuur er "
                                               "nog geen heeft.")},
                "ref": {"type": "string",
                        "description": "Factuurnummer van de leverancier."},
                "invoice_date": {"type": "string", "description": "Factuurdatum (JJJJ-MM-DD)."},
                "invoice_date_due": {"type": "string", "description": "Vervaldatum (JJJJ-MM-DD)."},
                "payment_reference": {"type": "string",
                                      "description": "Betalingskenmerk van het document."},
                "iban": {"type": "string",
                         "description": ("IBAN van het document. Wordt getoetst aan de "
                                         "bankrekeningen van de crediteur; wijkt het af, "
                                         "dan wordt er niets ingevuld.")},
                "lines": {
                    "type": "array",
                    "description": "Factuurregels.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "integer",
                                           "description": "Grootboekrekening."},
                            "name": {"type": "string", "description": "Omschrijving."},
                            "quantity": {"type": "number", "description": "Aantal (default 1)."},
                            "price_unit": {"type": "number",
                                           "description": "Prijs per stuk, exclusief btw."},
                            "tax_ids": {"type": "array", "items": {"type": "integer"},
                                        "description": "Btw-codes."},
                        },
                        "required": ["account_id", "price_unit"],
                    },
                },
                "amount_total_expected": {
                    "type": "number",
                    "description": ("Het totaalbedrag zoals het op het document staat. "
                                    "Wordt na het invullen vergeleken met wat Odoo "
                                    "berekent; een verschil komt als waarschuwing terug "
                                    "én als notitie op de factuur."),
                },
                "vervang_regels": {"type": "boolean",
                                   "description": "Bestaande regels vervangen."},
                "post": _POST,
            },
            "required": ["database", "move_id", "ref", "lines"],
        },
    },
]
