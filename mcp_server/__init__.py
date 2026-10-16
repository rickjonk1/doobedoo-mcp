"""
mcp_server — multi-tenant Odoo-MCP-server van doobedoo.

Streamable-HTTP (request/response) JSON-RPC op /mcp, bearer-geauthenticeerd via de
OAuth 2.1 authorization-server (partner_api/oauth2.py). Tools zijn dunne wrappers
over de bestaande OdooAPI; multi-tenant via companies_odoo.

Tool-schema's zijn geïnspireerd op pantalytics/odoo-mcp-pro (MPL-2.0).
Zie routes.register_mcp(app, limiter).

`register_mcp` wordt lazy geladen (PEP 562) zodat de protocol-/tool-laag te
importeren is zonder Flask (handig voor losse unit-tests).
"""


def __getattr__(name):
    if name == "register_mcp":
        from .routes import register_mcp
        return register_mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
