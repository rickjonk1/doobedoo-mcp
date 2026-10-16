"""
partner_api/security.py — OAuth2 client_credentials: hashing, tokens, scopes.

- Client-secret: bcrypt (one-way), zoals user-wachtwoorden.
- Access token: opaque (secrets.token_urlsafe), server-side alleen als SHA-256-hash
  opgeslagen → intrekbaar en niet reconstrueerbaar uit de DB.
- require_scope(): decorator die de Bearer-token valideert (actief, niet verlopen,
  niet ingetrokken, client actief, IP toegestaan, juiste scope) en de client op
  flask.g zet.
"""
import functools
import hashlib
import logging
import secrets

from flask import g, jsonify, request

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 3600          # access-token levensduur (1 uur)
_TOKEN_BYTES = 32                 # ~43 tekens urlsafe


# ── Hashing ─────────────────────────────────────────────────────────────────

def hash_secret(secret: str) -> str:
    from auth import bcrypt
    return bcrypt.generate_password_hash(secret).decode()


def verify_secret(secret: str, secret_hash: str) -> bool:
    from auth import bcrypt
    try:
        return bcrypt.check_password_hash(secret_hash, secret or "")
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


# ── Client-authenticatie + token-uitgifte ───────────────────────────────────

_DUMMY_HASH = None


def _dummy_hash() -> str:
    # Vaste bcrypt-hash zodat we óók bij een onbekende client een verify draaien
    # → gelijke responstijd, geen timing-orakel voor geldige client_ids.
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_secret("timing-equalizer-not-a-real-secret")
    return _DUMMY_HASH


def authenticate_client(client_id: str, client_secret: str):
    """PartnerClient bij geldige (client_id, secret) en active — anders None."""
    from models.partner_client import PartnerClient
    if not client_id or not client_secret:
        return None
    client = PartnerClient.query.filter_by(client_id=client_id, active=True).first()
    secret_hash = client.client_secret_hash if client else _dummy_hash()
    ok = verify_secret(client_secret, secret_hash)
    return client if (client and ok) else None


def issue_token(client, scopes: set[str], ttl_seconds: int = TOKEN_TTL_SECONDS):
    """Maak een opaque access token, sla alleen de hash op. Retourneert
    (plaintext_token, expires_in_seconds, granted_scopes)."""
    from datetime import timedelta
    from database import db
    from models.partner_client import PartnerToken
    from utils import nl_now

    granted = " ".join(sorted(scopes))
    plaintext = secrets.token_urlsafe(_TOKEN_BYTES)
    tok = PartnerToken(
        token_hash=token_hash(plaintext),
        client_id=client.client_id,
        scopes=granted,
        expires_at=nl_now() + timedelta(seconds=ttl_seconds),
        revoked=False,
    )
    db.session.add(tok)
    db.session.commit()
    return plaintext, ttl_seconds, granted


def _resolve_bearer():
    """Geef (PartnerToken, PartnerClient) voor een geldige Authorization-header,
    anps (None, None). Valideert vervaltijd, intrekking en client-status."""
    from models.partner_client import PartnerClient, PartnerToken
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, None
    raw = auth[7:].strip()
    if not raw:
        return None, None
    tok = PartnerToken.query.filter_by(token_hash=token_hash(raw)).first()
    if not tok or not tok.is_valid():
        return None, None
    client = PartnerClient.query.filter_by(client_id=tok.client_id, active=True).first()
    if not client:
        return None, None
    # Deprovisioning: hangt dit token aan een dashboard-gebruiker (MCP-flow via
    # consent), weiger dan zodra die gebruiker gedeactiveerd is. Zonder deze check
    # blijft de MCP-toegang van een vertrokken collega tot 30 dagen (refresh-window)
    # geldig. client_credentials-tokens (partner-API) hebben geen subject en
    # blijven ongemoeid.
    subject_id = getattr(tok, "subject_user_id", None)
    if subject_id is not None:
        from models.user import User
        user = User.query.get(subject_id)
        if not user or not getattr(user, "active", True):
            logger.warning("[PARTNER] token geweigerd: subject-gebruiker %s inactief/verwijderd",
                           subject_id)
            return None, None
    return tok, client


# ── IP-allowlist + fout-helper ──────────────────────────────────────────────

def client_ip() -> str:
    # ProxyFix (x_for=1) herschrijft remote_addr al naar het echte client-IP.
    return request.remote_addr or ""


def _ip_allowed(client) -> bool:
    allow = client.allowed_ips()
    if not allow:
        return True          # geen restrictie ingesteld
    return client_ip() in allow


def api_error(code: str, message: str, status: int):
    resp = jsonify({"error": {"code": code, "message": message}})
    resp.status_code = status
    if status == 401:
        resp.headers["WWW-Authenticate"] = 'Bearer realm="partner-api"'
    return resp


# ── Scope-decorator ─────────────────────────────────────────────────────────

def require_scope(*required_scopes):
    """Bescherm een endpoint: geldige Bearer-token met álle vereiste scopes.
    Zet g.partner_client / g.partner_token voor de handler + audit-log."""
    required = set(required_scopes)

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tok, client = _resolve_bearer()
            if not client:
                return api_error("invalid_token",
                                 "Ontbrekende of ongeldige access token.", 401)
            if not _ip_allowed(client):
                logger.warning("[PARTNER] IP geweigerd voor %s: %s",
                               client.client_id, client_ip())
                return api_error("ip_not_allowed", "IP-adres niet toegestaan.", 403)
            if required and not required.issubset(tok.scope_set()):
                return api_error("insufficient_scope",
                                 f"Vereiste scope: {' '.join(sorted(required))}.", 403)
            g.partner_client = client
            g.partner_token = tok
            return fn(*args, **kwargs)
        return wrapper
    return deco
