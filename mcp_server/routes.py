"""
mcp_server/routes.py — MCP Streamable-HTTP endpoint (Flask-blueprint)
=====================================================================
`POST /mcp`  — JSON-RPC 2.0 (initialize/tools.list/tools.call/…). Bearer-auth via
               de OAuth 2.1-tokens (partner_api). Antwoordt met application/json
               (request/response-modus; geen langlevende SSE → geen blokkade op
               de synchrone gunicorn-worker). Accepteert een client uitsluitend
               text/event-stream, dan gaat hetzelfde antwoord als één afgesloten
               SSE-gebeurtenis terug — ook dat is één request, één antwoord.
`GET /mcp`   — 405 (we bieden geen server-initiated SSE-stream aan; spec-conform).
`DELETE /mcp`— 200 (sessie-einde no-op; we zijn stateless).

Elke geweigerde call wordt gelogd én in het partner-logboek gezet. Dat klinkt
klein, maar zonder dat kon je van buitenaf niet zien of een koppeling afketste op
een ontbrekend token, een ontbrekende scope of de onderhoudsgrendel: de call
verdween in een 401 zonder spoor.

Geregistreerd via register_mcp(app, limiter).
"""
from __future__ import annotations

import json
import logging
import secrets
import time

from flask import Blueprint, Response, g, jsonify, request

from .protocol import handle_message
from .security import has_mcp_access, resolve_bearer, token_scopes

logger = logging.getLogger(__name__)


def _basis_url() -> str:
    from config_loader import PUBLIEKE_URL
    return PUBLIEKE_URL


def _resource_metadata_url() -> str:
    return _basis_url() + "/.well-known/oauth-protected-resource"


def _client_omschrijving() -> str:
    """Wie klopt er aan? De User-Agent is het enige dat een niet-ingelogde client
    over zichzelf prijsgeeft, en precies wat je nodig hebt om te zien wélke
    koppeling faalt."""
    ua = (request.headers.get("User-Agent") or "onbekend").strip()
    return ua[:120]


def _wil_sse() -> bool:
    """
    Moet het antwoord als SSE-gebeurtenis terug?

    De Streamable-HTTP-transport laat de server kiezen tussen application/json en
    text/event-stream. Claude en ChatGPT sturen allebei mee dat ze JSON accepteren
    en krijgen dus gewoon JSON — dat blijft precies zoals het was. Een client die
    uitsluitend text/event-stream accepteert kreeg tot nu toe een antwoord dat hij
    volgens zijn eigen Accept-header niet mocht lezen. Die krijgt nu SSE.
    """
    accept = (request.headers.get("Accept") or "").lower()
    if "text/event-stream" not in accept:
        return False
    return not ("application/json" in accept or "*/*" in accept)


def _sse(payload):
    """Eén JSON-RPC-antwoord als afgeronde SSE-stream. Bewust géén open
    verbinding: we sturen de gebeurtenis en sluiten af, zodat er geen
    gunicorn-thread aan blijft hangen."""
    body = "event: message\ndata: " + json.dumps(
        payload, ensure_ascii=False, default=str) + "\n\n"
    resp = Response(body, mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


def _unauthorized():
    resp = jsonify({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32001, "message": "Ontbrekende of ongeldige access token."}})
    resp.status_code = 401
    # MCP-auth-spec: verwijs naar de protected-resource-metadata.
    resp.headers["WWW-Authenticate"] = (
        f'Bearer resource_metadata="{_resource_metadata_url()}"')
    return resp


def _audit(client_id, status, dur_ms, note):
    """Schrijf één regel in het partner-logboek. client_id mag leeg zijn: een
    call die op het token strandde hoort net zo goed zichtbaar te zijn als een
    geslaagde."""
    try:
        from database import db
        from models.partner_client import PartnerApiLog
        db.session.add(PartnerApiLog(
            client_id=client_id, method="POST", path=f"/mcp:{note}"[:255],
            status=status, ip=(request.remote_addr or ""), duration_ms=dur_ms))
        db.session.commit()
    except Exception:
        logger.exception("[MCP] audit-log schrijven mislukt")


def _weiger(status, note, client_id=None, t0=None):
    """Log en registreer een geweigerde call, en geef de duur terug."""
    dur = int((time.time() - (t0 or time.time())) * 1000)
    logger.warning("[MCP] geweigerd (%s) status=%s client=%s ua=%s ip=%s",
                   note, status, client_id or "-", _client_omschrijving(),
                   request.remote_addr or "-")
    _audit(client_id, status, dur, note)


def register_mcp(app, limiter):
    bp = Blueprint("mcp_server", __name__)

    @bp.route("/mcp", methods=["GET"])
    def mcp_get():
        # Geen server-initiated SSE-stream; per spec mag dit 405 zijn.
        resp = jsonify({"error": "SSE-stream niet ondersteund; gebruik POST."})
        resp.status_code = 405
        resp.headers["Allow"] = "POST, DELETE"
        return resp

    @bp.route("/mcp", methods=["DELETE"])
    def mcp_delete():
        return ("", 200)

    @bp.route("/mcp", methods=["POST"])
    @limiter.limit("240 per minute")
    def mcp_post():
        t0 = time.time()
        tok, client = resolve_bearer()
        if not client or not has_mcp_access(tok):
            if not client:
                _weiger(401, "geen-geldig-token", t0=t0)
                return _unauthorized()
            _weiger(403, "geen-mcp-scope", client_id=client.client_id, t0=t0)
            resp = jsonify({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32003,
                                      "message": "Geen MCP-scope (odoo:read)."}})
            resp.status_code = 403
            return resp

        # Onderhoudsgrendel: een bestaand token van een collega mag geen
        # achterdeur zijn. Tokens zonder persoon erachter (zuivere
        # machine-clients) blijven werken.
        import lockdown
        if lockdown.actief():
            subject_id = getattr(tok, "subject_user_id", None)
            if subject_id is not None:
                from models.user import User
                gebruiker = User.query.get(subject_id)
                adres = getattr(gebruiker, "email", None)
                if not lockdown.mag_inloggen(adres):
                    logger.warning("[LOCKDOWN] MCP-call geweigerd voor %s", adres or subject_id)
                    _weiger(403, "onderhoudsgrendel", client_id=client.client_id, t0=t0)
                    resp = jsonify({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32003,
                                              "message": lockdown.BOODSCHAP}})
                    resp.status_code = 403
                    return resp

        body = request.get_json(silent=True)
        if body is None:
            _weiger(400, "ongeldige-json", client_id=client.client_id, t0=t0)
            return jsonify({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Ongeldige JSON."}}), 400

        scopes = token_scopes(tok)
        base_url = _basis_url()
        messages = body if isinstance(body, list) else [body]
        responses = []
        call_notes = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            methode = msg.get("method")
            if methode == "tools/call":
                call_notes.append((msg.get("params") or {}).get("name") or "?")
            if methode == "initialize":
                # De enige plek waar een client zichzelf voorstelt. Zonder deze
                # regel weet je bij een koppeling die niet werkt niet eens welke
                # protocolversie hij vraagt.
                info = (msg.get("params") or {}).get("clientInfo") or {}
                logger.info("[MCP] initialize door %s %s (protocol %s, ua=%s)",
                            info.get("name") or "onbekend", info.get("version") or "",
                            (msg.get("params") or {}).get("protocolVersion") or "-",
                            _client_omschrijving())
            else:
                logger.info("[MCP] %s door client=%s", methode or "?", client.client_id)
            resp = handle_message(msg, scopes=scopes, base_url=base_url)
            if resp is not None:
                responses.append(resp)

        g.partner_client = client  # voor eventuele downstream-audit
        dur = int((time.time() - t0) * 1000)
        # Koppel de call aan de collega achter het token (subject) zodat writes
        # herleidbaar zijn tot een persoon, niet alleen de gedeelde MCP-client.
        note = ",".join(call_notes) or "rpc"
        subj = getattr(tok, "subject_user_id", None)
        if subj is not None:
            note = f"u{subj}:{note}"
        _audit(client.client_id, 200, dur, note)

        # Alleen notificaties/geen antwoorden → 202 zonder body (spec).
        if not responses:
            return ("", 202)
        payload = responses if isinstance(body, list) else responses[0]
        is_init = (not isinstance(body, list)) and isinstance(body, dict) \
            and body.get("method") == "initialize"
        # Standaard kaal application/json (request/response-modus); alleen een
        # client die uitsluitend text/event-stream accepteert krijgt SSE.
        out = _sse(payload) if _wil_sse() else jsonify(payload)
        # Compat: geef een sessie-id terug bij initialize (niet afgedwongen — stateless).
        if is_init:
            out.headers["Mcp-Session-Id"] = secrets.token_urlsafe(18)
        return out

    app.register_blueprint(bp)
    logger.info("[MCP] MCP-server geregistreerd op /mcp")
