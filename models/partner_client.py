"""
models/partner_client.py — Partner-API (OAuth2 client_credentials)
==================================================================
Datamodel voor de externe partner-API (bv. Ravical). Gescheiden van de
web-app-auth (users/sessies): eigen clients, eigen tokens, eigen audit-log.

- PartnerClient : een partner-applicatie met client_id + (gehasht) secret,
                  toegestane scopes en optionele IP-allowlist.
- PartnerToken  : uitgegeven access tokens (alleen de HASH opgeslagen), met
                  TTL en intrekbaarheid.
- PartnerApiLog : audit-/calloverzicht van elke API-aanroep.

Secrets en tokens worden NOOIT in plaintext opgeslagen: het client-secret als
bcrypt-hash (one-way, net als user-wachtwoorden), tokens als SHA-256-hash.
"""
from datetime import timedelta

from database import db
from utils import nl_now


class PartnerClient(db.Model):
    __tablename__ = "partner_clients"

    id                 = db.Column(db.Integer, primary_key=True)
    client_id          = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # Leeg tot de partner de uitnodiging activeert en zelf het secret zet.
    client_secret_hash = db.Column(db.String(255), nullable=True)
    name               = db.Column(db.String(150), nullable=False)
    # Uitnodiging: eenmalige activatietoken (alleen hash opgeslagen) + vervaltijd.
    # Zolang deze gezet is en er nog geen secret is, is de client "uitgenodigd".
    invite_token_hash  = db.Column(db.String(64), nullable=True, index=True)
    invite_expires     = db.Column(db.DateTime, nullable=True)
    # Spatie-gescheiden scopes, bv. "aangiftes:read dossiers:read klanten:read btw:read"
    scopes             = db.Column(db.String(500), nullable=False, default="")
    # Optionele IP-allowlist (spatie/komma-gescheiden). Leeg = geen IP-restrictie.
    ip_allowlist       = db.Column(db.String(500), nullable=True)
    # Sandbox-client: endpoints geven duidelijk gemarkeerde mock-data terug zodat
    # de partner kan integreren vóór er echte data is.
    sandbox            = db.Column(db.Boolean, nullable=False, default=False)
    active             = db.Column(db.Boolean, nullable=False, default=True)
    created_by         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at         = db.Column(db.DateTime, nullable=False, default=nl_now)
    updated_at         = db.Column(db.DateTime, nullable=False, default=nl_now, onupdate=nl_now)

    # ── OAuth 2.1 / MCP ──────────────────────────────────────────────────────
    # MCP-clients (Claude/ChatGPT) registreren zich dynamisch (RFC 7591) en
    # gebruiken de authorization-code + PKCE-flow i.p.v. client_credentials.
    is_mcp             = db.Column(db.Boolean, nullable=False, default=False)
    # Nieuwe-regel/spatie-gescheiden toegestane redirect-URI's (exact-match).
    redirect_uris      = db.Column(db.Text, nullable=True)
    # "none" = publieke client (PKCE, geen secret); anders client_secret_basic/post.
    token_endpoint_auth_method = db.Column(db.String(32), nullable=False,
                                           default="client_secret_basic")

    tokens = db.relationship("PartnerToken", backref="client", lazy="dynamic",
                             cascade="all, delete-orphan")

    def scope_set(self) -> set[str]:
        return {s for s in (self.scopes or "").split() if s}

    def allowed_ips(self) -> list[str]:
        raw = (self.ip_allowlist or "").replace(",", " ")
        return [ip for ip in raw.split() if ip]

    def redirect_uri_list(self) -> list[str]:
        raw = (self.redirect_uris or "").replace(",", " ")
        return [u for u in raw.split() if u]

    def is_public(self) -> bool:
        return (self.token_endpoint_auth_method or "") == "none"

    def status(self) -> str:
        """'uitgenodigd' (nog niet geactiveerd) | 'actief' | 'uit'."""
        if not self.client_secret_hash and self.invite_token_hash:
            return "uitgenodigd"
        return "actief" if self.active else "uit"

    def to_dict(self) -> dict:
        return {
            "client_id":    self.client_id,
            "name":         self.name,
            "scopes":       sorted(self.scope_set()),
            "ip_allowlist": self.allowed_ips(),
            "sandbox":      self.sandbox,
            "active":       self.active,
            "status":       self.status(),
            "created_at":   self.created_at.isoformat() if self.created_at else None,
        }


class PartnerToken(db.Model):
    __tablename__ = "partner_tokens"

    id         = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)  # sha256 hex
    client_id  = db.Column(db.String(64), db.ForeignKey("partner_clients.client_id"),
                           nullable=False, index=True)
    scopes     = db.Column(db.String(500), nullable=False, default="")
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked    = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=nl_now)

    # ── OAuth 2.1: refresh-token + welke collega (subject) ───────────────────
    # MCP-sessies zijn langlevend; de assistent vernieuwt via refresh_token.
    # Alleen de hash van het refresh-token wordt opgeslagen.
    refresh_hash    = db.Column(db.String(64), nullable=True, index=True)
    refresh_expires = db.Column(db.DateTime, nullable=True)
    subject_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    def is_valid(self) -> bool:
        return (not self.revoked) and nl_now() < self.expires_at

    def refresh_valid(self) -> bool:
        return (not self.revoked) and bool(self.refresh_hash) and \
            self.refresh_expires is not None and nl_now() < self.refresh_expires

    def scope_set(self) -> set[str]:
        return {s for s in (self.scopes or "").split() if s}


class OAuthAuthCode(db.Model):
    """Kortlevende authorization-code (OAuth 2.1 auth-code + PKCE). Alleen de hash
    van de code wordt opgeslagen; eenmalig inwisselbaar op het token-endpoint."""
    __tablename__ = "oauth_auth_codes"

    id                    = db.Column(db.Integer, primary_key=True)
    code_hash             = db.Column(db.String(64), nullable=False, unique=True, index=True)
    client_id             = db.Column(db.String(64),
                                      db.ForeignKey("partner_clients.client_id"),
                                      nullable=False, index=True)
    redirect_uri          = db.Column(db.String(500), nullable=False)
    scopes                = db.Column(db.String(500), nullable=False, default="")
    code_challenge        = db.Column(db.String(128), nullable=False)
    code_challenge_method = db.Column(db.String(8), nullable=False, default="S256")
    subject_user_id       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    expires_at            = db.Column(db.DateTime, nullable=False)
    used                  = db.Column(db.Boolean, nullable=False, default=False)
    created_at            = db.Column(db.DateTime, nullable=False, default=nl_now)

    def is_valid(self) -> bool:
        return (not self.used) and nl_now() < self.expires_at

    def scope_set(self) -> set[str]:
        return {s for s in (self.scopes or "").split() if s}


class PartnerApiLog(db.Model):
    __tablename__ = "partner_api_log"

    id          = db.Column(db.Integer, primary_key=True)
    ts          = db.Column(db.DateTime, nullable=False, default=nl_now, index=True)
    client_id   = db.Column(db.String(64), nullable=True, index=True)
    method      = db.Column(db.String(8), nullable=False)
    path        = db.Column(db.String(255), nullable=False)
    status      = db.Column(db.Integer, nullable=False)
    ip          = db.Column(db.String(64), nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.Index("ix_partner_api_log_client_ts", "client_id", "ts"),
    )
