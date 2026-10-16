"""
mcp_server/security.py — bearer-auth voor de MCP-server
=======================================================
Hergebruikt de partner-API tokenopslag (PartnerToken): opaque bearer-tokens,
alleen als SHA-256-hash opgeslagen, intrekbaar. De OAuth 2.1 authorization-server
(partner_api/oauth2.py) geeft deze tokens uit aan Claude/ChatGPT.

Scopes:
- odoo:read   → lezen (search/get/list_models/server_info) + list_databases
- odoo:write  → ook create/update/delete/execute_method
"""
from __future__ import annotations

SCOPE_READ = "odoo:read"
SCOPE_WRITE = "odoo:write"
SCOPE_ADMIN = "odoo:admin"
MCP_SCOPES = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)


def resolve_bearer():
    """(PartnerToken, PartnerClient) voor een geldige Authorization: Bearer-header,
    anders (None, None). Hergebruikt de partner-API-validatie."""
    from partner_api.security import _resolve_bearer
    return _resolve_bearer()


def token_scopes(tok) -> set[str]:
    return tok.scope_set() if tok else set()


def may_write(tok) -> bool:
    return SCOPE_WRITE in token_scopes(tok)


def has_mcp_access(tok) -> bool:
    """Minimaal één MCP-scope (odoo:read/write) om de MCP te gebruiken."""
    return bool(token_scopes(tok) & set(MCP_SCOPES))


# Backwards-compat alias
has_odoo_access = has_mcp_access
