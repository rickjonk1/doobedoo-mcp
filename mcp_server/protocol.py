"""
mcp_server/protocol.py — JSON-RPC 2.0 / MCP-berichtafhandeling
==============================================================
Implementeert de MCP-methoden die een tool-server nodig heeft, over de
Streamable-HTTP-transport in *request/response*-modus (één POST → één JSON-
antwoord, geen langlevende SSE). Dat is spec-conform voor een stateless
tool-server en draait gewoon binnen de bestaande synchrone Flask/gunicorn.

Ondersteunde methoden:
  initialize · notifications/initialized · ping · tools/list · tools/call
"""
from __future__ import annotations

import json
import logging

from .resources import list_resources, read_resource
from .content import CONTENT_KEY
from .tools import TOOLS_BY_NAME, public_tool_list, tool_allowed

logger = logging.getLogger(__name__)

# Onze hoogst ondersteunde (legacy, niet-stateless) protocolversie. We melden
# 'm als de client een versie vraagt die we niet kennen. Alle onderstaande
# 2025-era versies delen dezelfde initialize/notifications/tools-handshake die
# wij volledig implementeren — de stateless revisie 2026-07-28 NIET.
PROTOCOL_VERSION = "2025-11-25"
# Versies die we mogen bevestigen (echoën) als de client erom vraagt. Bewust
# ZONDER 2026-07-28: die vereist het nieuwe stateless model dat wij niet spreken;
# vragen clients daarom, dan antwoorden we met onze eigen PROTOCOL_VERSION zodat
# ze netjes terugvallen op de legacy-handshake.
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
}
SERVER_INFO = {"name": "doobedoo-odoo-mcp", "version": "1.0.0"}

# JSON-RPC foutcodes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_text(payload) -> dict:
    return {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}


def _tool_content(payload) -> tuple[list, object]:
    """
    Splits een handler-resultaat in MCP-inhoudsblokken en structuredContent.

    Zonder CONTENT_KEY blijft het gedrag zoals het was: één tekstblok met de
    JSON. Mét die sleutel gaan de opgegeven blokken mee (bv. afbeeldingen), en
    komt de rest van de payload als samenvattend tekstblok erachteraan — zodat
    een client die alleen tekst toont nog steeds iets bruikbaars laat zien.
    """
    if not isinstance(payload, dict) or CONTENT_KEY not in payload:
        return [_tool_text(payload)], payload

    blokken = list(payload.get(CONTENT_KEY) or [])
    rest = {k: v for k, v in payload.items() if k != CONTENT_KEY}
    if rest:
        blokken.append(_tool_text(rest))
    return blokken, rest


def handle_message(msg: dict, *, scopes=None, base_url=None):
    """Verwerk één JSON-RPC-bericht. Geeft een response-dict terug, of None voor
    een notificatie (geen antwoord). `scopes` = de scopes van de bearer-token
    (bepaalt welke tools zichtbaar/aanroepbaar zijn). `base_url` = de externe
    host (voor het server-logo in serverInfo)."""
    scopes = set(scopes or ())
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None,
                      INVALID_REQUEST, "Ongeldig JSON-RPC 2.0 bericht.")

    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    # ── Notificaties (geen antwoord) ─────────────────────────────────────────
    if method == "notifications/initialized" or (is_notification and method and
                                                 method.startswith("notifications/")):
        return None

    # ── server/discover (MCP-revisie 2026-07-28) — BEWUST NIET IMPLEMENTEREN ──
    # Moderne clients (Claude Code, ClaudeAI, Toolbox) starten met een
    # `server/discover`-probe die 2026-07-28 adverteert. Het officiële
    # backward-compat-algoritme luidt:
    #   • krijgt de client een DiscoverResult terug  → server is MODERN → de
    #     client praat verder STATELESS (geen initialize; protocolVersion,
    #     clientInfo en capabilities reizen in `_meta` op elke request).
    #   • krijgt de client een FOUT of geen antwoord → server is LEGACY → de
    #     client valt terug op de 2025-11-25 initialize/initialized-handshake.
    # (Bron: MCP 2026-07-28 release + TS-SDK v2 migratiegids.)
    #
    # Wij implementeren ALLEEN de legacy-handshake (die volledig werkt). Een
    # eerdere poging beantwoordde `server/discover` mét een DiscoverResult; de
    # client bestempelde ons daardoor als MODERN en probeerde stateless te
    # praten terwijl wij dat niet kunnen → eindeloze discover→initialize→discover-
    # lus (de client kwam nooit bij tools/list). Dé oplossing is dus juist NIET
    # antwoorden op discover: we laten 'm doorvallen naar de standaard
    # METHOD_NOT_FOUND (-32601) onderaan, zodat élke moderne client netjes
    # terugvalt op de legacy-handshake — precies zoals alle 2025-era servers.
    # LET OP: voeg hier GEEN DiscoverResult toe zonder de volledige stateless
    # 2026-07-28-transport te implementeren, anders keert de lus terug.

    # ── initialize ───────────────────────────────────────────────────────────
    if method == "initialize":
        # Versie-onderhandeling: bevestig de door de client gevraagde versie als
        # wij die kennen (alle 2025-era legacy-versies delen onze handshake).
        # Vraagt de client een versie die we niet ondersteunen (bv. de stateless
        # 2026-07-28), meld dan onze eigen hoogste legacy-versie zodat de client
        # terugvalt op de handshake die wij wél volledig kunnen. NB: nooit een
        # oudere versie opdringen dan de client aanbood — Claude Code weigert dan
        # de verbinding en blijft initialize herhalen (zie connect-diagnose).
        client_pv = params.get("protocolVersion")
        resp_pv = client_pv if client_pv in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        server_info = dict(SERVER_INFO)
        server_info["title"] = "doobedoo — Odoo"
        if base_url:
            # SEP-973: server-logo in serverInfo (wordt getoond zodra de client-UI
            # dit rendert).
            logo = base_url.rstrip("/") + "/static/img/doobedoo_icoon_256.png"
            # LET OP: `sizes` MOET een array van strings zijn (schema: Icon.sizes
            # type=array). Ooit stond hier de string "any" — een strikte client
            # (2026-07-28 SDK) verwierp daardoor de HELE InitializeResult en bleef
            # eindeloos `initialize` herhalen (kwam nooit bij notifications/
            # initialized of tools/list). Nooit meer een kale string van maken.
            server_info["icons"] = [{"src": logo, "mimeType": "image/png",
                                     "sizes": ["any"]}]
            server_info["websiteUrl"] = base_url.rstrip("/")
        return _result(msg_id, {
            # Bevestigde protocolversie (zie versie-onderhandeling hierboven):
            # de gevraagde legacy-versie, of onze eigen hoogste legacy-versie als
            # de client een niet-ondersteunde (stateless) versie vroeg.
            "protocolVersion": resp_pv,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False, "subscribe": False}},
            "serverInfo": server_info,
            "instructions": (
                "Odoo-toegang van doobedoo. Roep list_databases aan, kies een "
                "database en gebruik search_records/get_record. Schrijf-tools vereisen "
                "de scope odoo:write. Bijlagen lees je met list_attachments en "
                "read_document; ruwe binaire velden worden nooit teruggegeven.\n\n"
                "Huisstijl: maak je een document of PDF voor doobedoo, lees dan eerst de "
                "resource 'doobedoo://huisstijl' (kleuren en logo)."
            ),
        })

    if method == "ping":
        return _result(msg_id, {})

    # ── tools/list ───────────────────────────────────────────────────────────
    if method == "tools/list":
        return _result(msg_id, {"tools": public_tool_list(scopes)})

    # ── resources (huisstijl) ──────────────────────────────────
    if method == "resources/list":
        return _result(msg_id, {"resources": list_resources()})

    if method == "resources/read":
        uri = (params or {}).get("uri")
        content = read_resource(uri)
        if content is None:
            return _error(msg_id, INVALID_PARAMS, f"Onbekende resource: {uri!r}.")
        return _result(msg_id, {"contents": [content]})

    # ── tools/call ───────────────────────────────────────────────────────────
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if not tool:
            return _error(msg_id, INVALID_PARAMS, f"Onbekende tool: {name!r}.")
        if not tool_allowed(tool, scopes):
            return _result(msg_id, {
                "content": [_tool_text({"error": f"Scope '{tool.get('scope')}' vereist "
                                                 "voor deze tool."})],
                "isError": True,
            })
        try:
            payload = tool["handler"](args, {"scopes": scopes})
            content, gestructureerd = _tool_content(payload)
            return _result(msg_id, {
                "content": content,
                "structuredContent": (gestructureerd if isinstance(gestructureerd, dict)
                                      else {"result": gestructureerd}),
                "isError": False,
            })
        except ValueError as exc:
            return _result(msg_id, {"content": [_tool_text({"error": str(exc)})], "isError": True})
        except Exception as exc:  # noqa: BLE001 — nette tool-fout i.p.v. 500
            logger.warning("[MCP] tool %s faalde: %s", name, exc)
            return _result(msg_id, {
                "content": [_tool_text({"error": f"Tool mislukt: {exc}"})],
                "isError": True,
            })

    # ── Onbekende methode ────────────────────────────────────────────────────
    if is_notification:
        return None
    return _error(msg_id, METHOD_NOT_FOUND, f"Onbekende methode: {method!r}.")
