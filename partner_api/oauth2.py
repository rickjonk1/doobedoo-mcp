"""
partner_api/oauth2.py — OAuth 2.1 authorization-server voor de MCP-server
=========================================================================
De OAuth 2.1 authorization-server waarmee Claude en ChatGPT zich koppelen aan
de Odoo-MCP-server (/mcp). Ondersteunt:

- Dynamic Client Registration (RFC 7591)            → POST /oauth2/register
- Authorization Code + PKCE (S256)                  → GET/POST /oauth2/authorize
- Token + refresh                                   → POST /oauth2/token
- Discovery-metadata                                → /.well-known/oauth-authorization-server
                                                       /.well-known/oauth-protected-resource

De consent-stap hangt aan de Flask-Login-sessie (met 2FA): een gebruiker logt
in en keurt de koppeling goed. Tokens worden alleen als hash opgeslagen in
PartnerToken (intrekbaar).

Scopes: odoo:read en odoo:write.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from flask import (Blueprint, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user

from .security import authenticate_client, token_hash

logger = logging.getLogger(__name__)

ACCESS_TTL_SECONDS = 3600            # 1 uur
REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30   # 30 dagen
CODE_TTL_SECONDS = 300               # 5 min

SUPPORTED_SCOPES = ["odoo:read", "odoo:write"]


def _basis_url() -> str:
    """Vaste publieke URL (niet de Host-header: die is door de client te kiezen)."""
    from config_loader import PUBLIEKE_URL
    return PUBLIEKE_URL


# ── PKCE ─────────────────────────────────────────────────────────────────────

def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    if not code_verifier or not code_challenge:
        return False
    if (method or "S256").upper() == "PLAIN":
        return secrets.compare_digest(code_verifier, code_challenge)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return secrets.compare_digest(_b64url_nopad(digest), code_challenge)


# ── Scope-beleid ─────────────────────────────────────────────────────────────

def _grantable_scopes(user, requested: set[str]) -> set[str]:
    """Welke scopes deze gebruiker daadwerkelijk mag, gegeven het verzoek."""
    # Odoo lezen + bewerken: elke actieve, ingelogde gebruiker.
    allowed = {"odoo:read", "odoo:write"}
    req = requested & set(SUPPORTED_SCOPES)
    # Zonder expliciet verzoek: geef de volledige toegestane set.
    granted = (req & allowed) if req else allowed
    return granted or {"odoo:read"}


# ── Token-uitgifte (access + refresh) ────────────────────────────────────────

def _issue_pair(client, scopes: set[str], subject_user_id):
    from database import db
    from models.partner_client import PartnerToken
    from utils import nl_now

    access_plain = secrets.token_urlsafe(32)
    refresh_plain = secrets.token_urlsafe(32)
    tok = PartnerToken(
        token_hash=token_hash(access_plain),
        client_id=client.client_id,
        scopes=" ".join(sorted(scopes)),
        expires_at=nl_now() + timedelta(seconds=ACCESS_TTL_SECONDS),
        refresh_hash=token_hash(refresh_plain),
        refresh_expires=nl_now() + timedelta(seconds=REFRESH_TTL_SECONDS),
        subject_user_id=subject_user_id,
    )
    db.session.add(tok)
    db.session.commit()
    return access_plain, refresh_plain, " ".join(sorted(scopes))


def _token_response(access, refresh, scope):
    return jsonify({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": scope,
    })


def _oauth_error(code, desc, status=400):
    resp = jsonify({"error": code, "error_description": desc})
    resp.status_code = status
    return resp


_SCOPE_LABELS = {
    "odoo:read":  ("Gegevens lezen", "Zoeken en opvragen in de gekoppelde databases."),
    "odoo:write": ("Gegevens wijzigen", "Records aanmaken, wijzigen en verwijderen."),
    "odoo:admin": ("Odoo admin", "Willekeurige model-methodes aanroepen (execute_method)."),
}


def register_oauth2(app, limiter):
    bp = Blueprint("oauth2", __name__)

    # ── Discovery ────────────────────────────────────────────────────────────
    # De suffix-variant hoort erbij: een client die de resource /mcp gebruikt,
    # probeert volgens RFC 8414 eerst /.well-known/oauth-authorization-server/mcp
    # en pas daarna de kale URL. Claude en ChatGPT vallen netjes terug; niet elke
    # client doet dat, en die kwam dan nooit bij de authorization-server uit.
    @bp.route("/.well-known/oauth-authorization-server")
    @bp.route("/.well-known/oauth-authorization-server/mcp")
    def as_metadata():
        base = _basis_url()
        return jsonify({
            "issuer": base,
            "authorization_endpoint": base + "/oauth2/authorize",
            "token_endpoint": base + "/oauth2/token",
            "registration_endpoint": base + "/oauth2/register",
            "scopes_supported": SUPPORTED_SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic",
                                                      "client_secret_post"],
        })

    @bp.route("/.well-known/oauth-protected-resource")
    @bp.route("/.well-known/oauth-protected-resource/mcp")
    def rs_metadata():
        base = _basis_url()
        return jsonify({
            "resource": base + "/mcp",
            "authorization_servers": [base],
            "scopes_supported": SUPPORTED_SCOPES,
            "bearer_methods_supported": ["header"],
        })

    # ── Dynamic Client Registration (RFC 7591) ───────────────────────────────
    @bp.route("/oauth2/register", methods=["POST"])
    @limiter.limit("30 per minute")   # ruim tijdens instellen; clients retryen soms
    def register_client():
        from database import db
        from models.partner_client import PartnerClient
        from .security import hash_secret
        from utils import nl_now

        body = request.get_json(silent=True)
        if body is None:
            # Sommige clients sturen form-encoded of een leeg lichaam.
            body = request.form.to_dict() or {}
        logger.info("[OAUTH2] DCR-verzoek van %s: keys=%s",
                    request.remote_addr, sorted(body.keys()))

        try:
            redirect_uris = body.get("redirect_uris") or []
            if isinstance(redirect_uris, str):
                redirect_uris = [u for u in redirect_uris.replace(",", " ").split() if u]
            if not isinstance(redirect_uris, list) or not redirect_uris:
                logger.warning("[OAUTH2] DCR geweigerd: geen redirect_uris (body-keys=%s)",
                               sorted(body.keys()))
                return _oauth_error("invalid_redirect_uri", "redirect_uris is verplicht.")
            for u in redirect_uris:
                p = urlparse(u)
                if not p.netloc:
                    return _oauth_error("invalid_redirect_uri", f"Ongeldige redirect_uri: {u}")
                # https verplicht; http alléén voor lokale loopback (dev-clients).
                _host = (p.hostname or "").lower()
                _is_loopback = _host in ("localhost", "127.0.0.1", "::1")
                if p.scheme == "https" or (p.scheme == "http" and _is_loopback):
                    continue
                return _oauth_error(
                    "invalid_redirect_uri",
                    f"redirect_uri moet https zijn (http alleen voor localhost): {u}")

            # Publieke client (PKCE, geen secret) is de standaard voor MCP.
            auth_method = body.get("token_endpoint_auth_method") or "none"
            if auth_method not in ("none", "client_secret_basic", "client_secret_post"):
                auth_method = "none"
            client_id = "mcp_" + secrets.token_hex(12)
            client = PartnerClient(
                client_id=client_id,
                name=(body.get("client_name") or "MCP-client")[:150],
                scopes="odoo:read odoo:write",   # toekenning volgt uit consent/rol
                is_mcp=True,
                redirect_uris="\n".join(redirect_uris),
                token_endpoint_auth_method=auth_method,
                active=True,
            )
            out = {
                "client_id": client_id,
                "client_id_issued_at": int(nl_now().timestamp()),
                "redirect_uris": redirect_uris,
                "token_endpoint_auth_method": auth_method,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "odoo:read odoo:write",
            }
            if auth_method != "none":
                secret = secrets.token_urlsafe(32)
                client.client_secret_hash = hash_secret(secret)
                out["client_secret"] = secret
                out["client_secret_expires_at"] = 0   # verloopt niet
            db.session.add(client)
            db.session.commit()
            logger.info("[OAUTH2] DCR OK: client_id=%s auth=%s redirects=%s",
                        client_id, auth_method, redirect_uris)
            resp = jsonify(out)
            resp.status_code = 201
            return resp
        except Exception as exc:   # noqa: BLE001 — nooit een kale 500 op DCR
            db.session.rollback()
            logger.exception("[OAUTH2] DCR mislukt: %s", exc)
            return _oauth_error("invalid_client_metadata",
                                "Registratie mislukt; probeer opnieuw.", 400)

    # ── Admin: handmatig een MCP-client aanmaken (DCR-terugval) ───────────────
    # Als de automatische registratie in een client faalt, kan een admin hier
    # een publieke PKCE-client aanmaken en het client_id handmatig in Claude/
    # ChatGPT invullen ("add an OAuth Client ID"). Same-origin POST (adminsessie).
    from auth import admin_required as _admin_required

    @bp.route("/api/admin/mcp-clients", methods=["POST"])
    @_admin_required
    def admin_create_mcp_client():
        from database import db
        from models.partner_client import PartnerClient
        from utils import nl_now

        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "MCP-client (handmatig)")[:150]
        redirects = body.get("redirect_uris") or [
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/connector_platform_oauth_redirect",
            "https://chatgpt.com/aip/connector_platform_oauth_redirect",
        ]
        if isinstance(redirects, str):
            redirects = [u for u in redirects.replace(",", " ").split() if u]
        client_id = "mcp_" + secrets.token_hex(12)
        client = PartnerClient(
            client_id=client_id, name=name, scopes="odoo:read odoo:write",
            is_mcp=True, redirect_uris="\n".join(redirects),
            token_endpoint_auth_method="none", active=True,
            created_by=getattr(current_user, "id", None),
        )
        db.session.add(client)
        db.session.commit()
        logger.info("[OAUTH2] handmatige MCP-client %s aangemaakt door admin", client_id)
        return jsonify({"client_id": client_id, "token_endpoint_auth_method": "none",
                        "redirect_uris": redirects,
                        "authorization_server": _basis_url(),
                        "mcp_url": _basis_url() + "/mcp",
                        "created_at": nl_now().isoformat()}), 201

    # ── Authorization endpoint ────────────────────────────────────────────────
    @bp.route("/oauth2/authorize", methods=["GET", "POST"])
    @limiter.limit("60 per minute")
    def authorize():
        from database import db
        from models.partner_client import OAuthAuthCode, PartnerClient
        from utils import nl_now

        # Verzamel de OAuth-parameters (GET) of herstel ze (POST na consent).
        if request.method == "POST":
            src = _decode_params(request.form.get("oauth_params", ""))
        else:
            src = {k: request.args.get(k) for k in
                   ("client_id", "redirect_uri", "response_type", "scope",
                    "state", "code_challenge", "code_challenge_method")}

        client_id = src.get("client_id") or ""
        redirect_uri = src.get("redirect_uri") or ""
        client = PartnerClient.query.filter_by(client_id=client_id, active=True).first()
        if not client or not client.is_mcp:
            return _oauth_error("invalid_client", "Onbekende client_id.", 400)
        if redirect_uri not in client.redirect_uri_list():
            return _oauth_error("invalid_request", "redirect_uri niet geregistreerd.", 400)

        # Vanaf hier mogen fouten naar de redirect_uri (met state).
        state = src.get("state") or ""

        def _redirect_err(code, desc):
            q = {"error": code, "error_description": desc}
            if state:
                q["state"] = state
            return redirect(redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(q))

        if (src.get("response_type") or "") != "code":
            return _redirect_err("unsupported_response_type", "Alleen response_type=code.")
        challenge = src.get("code_challenge") or ""
        method = (src.get("code_challenge_method") or "S256").upper()
        if not challenge or method != "S256":
            return _redirect_err("invalid_request", "PKCE met S256 is verplicht.")

        # Login vereist — elke ingelogde gebruiker mag koppelen (Odoo lezen+bewerken).
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.full_path))

        requested = {s for s in (src.get("scope") or "").split() if s}
        grant = _grantable_scopes(current_user, requested)

        if request.method == "GET":
            scopes_view = [{"title": _SCOPE_LABELS.get(s, (s, ""))[0],
                            "desc": _SCOPE_LABELS.get(s, (s, ""))[1]} for s in sorted(grant)]
            return render_template(
                "consent.html",
                client_name=client.name,
                user_name=getattr(current_user, "name", "") or current_user.email,
                user_role=getattr(current_user, "role", ""),
                scopes=scopes_view,
                # Dynamisch geregistreerde clients (DCR, geen created_by) zijn
                # onverifieerbaar → toon een phishing-waarschuwing op het consent.
                unverified=(getattr(client, "created_by", None) is None),
                oauth_params=_encode_params(src),
            )

        # POST = consent-beslissing
        if request.form.get("decision") != "allow":
            return _redirect_err("access_denied", "Koppeling geweigerd.")

        code_plain = secrets.token_urlsafe(32)
        ac = OAuthAuthCode(
            code_hash=token_hash(code_plain),
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            scopes=" ".join(sorted(grant)),
            code_challenge=challenge,
            code_challenge_method="S256",
            subject_user_id=current_user.id,
            expires_at=nl_now() + timedelta(seconds=CODE_TTL_SECONDS),
        )
        db.session.add(ac)
        db.session.commit()
        logger.info("[OAUTH2] auth-code uitgegeven voor client=%s user=%s scopes=%s",
                    client.client_id, current_user.id, sorted(grant))
        q = {"code": code_plain}
        if state:
            q["state"] = state
        return redirect(redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(q))

    # ── Token endpoint ─────────────────────────────────────────────────────────
    @bp.route("/oauth2/token", methods=["POST"])
    @limiter.limit("60 per minute")
    def token():
        grant_type = request.form.get("grant_type")
        logger.info("[OAUTH2] token-verzoek grant_type=%s client=%s",
                    grant_type, request.form.get("client_id"))
        if grant_type == "authorization_code":
            return _grant_auth_code()
        if grant_type == "refresh_token":
            return _grant_refresh()
        return _oauth_error("unsupported_grant_type",
                            "Alleen authorization_code en refresh_token.")

    def _authenticate_token_client(client):
        """Publieke client (PKCE, geen secret) mag zonder secret; confidential moet
        het secret meesturen (Basic of form)."""
        if client.is_public():
            return True
        import base64 as _b64
        auth = request.headers.get("Authorization", "")
        cid = secret = None
        if auth.startswith("Basic "):
            try:
                raw = _b64.b64decode(auth[6:]).decode()
                cid, _, secret = raw.partition(":")
            except Exception:
                cid = secret = None
        else:
            cid = request.form.get("client_id")
            secret = request.form.get("client_secret")
        return authenticate_client(cid, secret) is not None and cid == client.client_id

    def _grant_auth_code():
        from database import db
        from models.partner_client import OAuthAuthCode, PartnerClient

        code = request.form.get("code") or ""
        client_id = request.form.get("client_id") or ""
        redirect_uri = request.form.get("redirect_uri") or ""
        verifier = request.form.get("code_verifier") or ""

        ac = OAuthAuthCode.query.filter_by(code_hash=token_hash(code)).first()
        if not ac or not ac.is_valid():
            return _oauth_error("invalid_grant", "Code ongeldig of verlopen.")
        client = PartnerClient.query.filter_by(client_id=ac.client_id, active=True).first()
        if not client or client.client_id != client_id:
            return _oauth_error("invalid_client", "Client komt niet overeen.", 401)
        if redirect_uri != ac.redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri komt niet overeen.")
        if not _authenticate_token_client(client):
            return _oauth_error("invalid_client", "Client-authenticatie mislukt.", 401)
        if not verify_pkce(verifier, ac.code_challenge, ac.code_challenge_method):
            return _oauth_error("invalid_grant", "PKCE-verificatie mislukt.")

        ac.used = True
        db.session.commit()
        access, refresh, scope = _issue_pair(client, ac.scope_set(), ac.subject_user_id)
        return _token_response(access, refresh, scope)

    def _grant_refresh():
        from database import db
        from models.partner_client import PartnerClient, PartnerToken

        refresh = request.form.get("refresh_token") or ""
        old = PartnerToken.query.filter_by(refresh_hash=token_hash(refresh)).first()
        if not old or not old.refresh_valid():
            return _oauth_error("invalid_grant", "Refresh-token ongeldig of verlopen.")
        client = PartnerClient.query.filter_by(client_id=old.client_id, active=True).first()
        if not client:
            return _oauth_error("invalid_client", "Onbekende client.", 401)
        if not _authenticate_token_client(client):
            return _oauth_error("invalid_client", "Client-authenticatie mislukt.", 401)

        # Her-evalueer de subject-gebruiker bij élke refresh i.p.v. de oude scopes
        # blind door te rollen. Zo verliest een gedeactiveerde collega zijn toegang
        # direct, en een gedegradeerde/van-de-AV-allowlist-verwijderde collega zijn
        # extra scopes bij de eerstvolgende refresh (i.p.v. pas na de 30-daagse
        # refresh-window). client_credentials-tokens zonder subject blijven ongemoeid.
        scopes = old.scope_set()
        subject_id = old.subject_user_id
        if subject_id is not None:
            from models.user import User
            user = User.query.get(subject_id)
            if not user or not getattr(user, "active", True):
                old.revoked = True
                db.session.commit()
                return _oauth_error("invalid_grant",
                                    "De gekoppelde gebruiker is niet meer actief.")
            scopes = _grantable_scopes(user, scopes)

        # Rotatie: oude token intrekken, nieuw paar uitgeven.
        old.revoked = True
        db.session.commit()
        access, new_refresh, scope = _issue_pair(client, scopes, subject_id)
        return _token_response(access, new_refresh, scope)

    app.register_blueprint(bp)
    logger.info("[OAUTH2] authorization-server geregistreerd (/oauth2, /.well-known)")


# ── Compacte doorgifte van OAuth-params door de consent-stap ─────────────────
# Geen ondertekening nodig: na de POST worden client_id + redirect_uri opnieuw
# tegen de geregistreerde client gevalideerd en scopes opnieuw op rol gefilterd,
# dus knoeien met deze blob kan geen rechten oprekken.

def _encode_params(d: dict) -> str:
    import json
    payload = base64.urlsafe_b64encode(
        json.dumps({k: v for k, v in d.items() if v is not None}).encode()).decode()
    return payload


def _decode_params(s: str) -> dict:
    import json
    try:
        return json.loads(base64.urlsafe_b64decode(s.encode()).decode())
    except Exception:
        return {}
