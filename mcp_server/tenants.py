"""
mcp_server/tenants.py — multi-tenant Odoo-resolutie voor de MCP-server
======================================================================
Elke Odoo-database is een "tenant". De inloggegevens staan Fernet-versleuteld
per administratie in `companies_odoo` (CompanyOdoo): één rij per
(db_name, company_id), maar url/db/user/key zijn binnen één database identiek,
dus we groeperen op db_name.

- `list_tenants()`            → distinct databases (db_name + url + #companies).
- `get_odoo_api(database)`    → een geauthenticeerde OdooAPI voor die database,
                                in-proces gecachet (korte TTL) zodat we niet elke
                                tool-call opnieuw bij Odoo inloggen.

De verbinding gebruikt XML-RPC via de bestaande OdooAPI-wrapper; Odoo's eigen
ACL's van het geregistreerde API-account bepalen wat er gelezen/geschreven mag worden.
"""
from __future__ import annotations

import threading
import time

from odoo_api import OdooAPI

# in-proces cache: db_name -> (OdooAPI, auth_ts)
_API_CACHE: dict[str, tuple[OdooAPI, float]] = {}
_CACHE_GUARD = threading.Lock()
_AUTH_TTL_SECONDS = 30 * 60          # heraut. na 30 min


class TenantError(Exception):
    """Onbekende of onbereikbare database."""


def _rows_by_db():
    """Actieve CompanyOdoo-rijen gegroepeerd per db_name (eerste rij als bron
    voor de inloggegevens; het aantal companies telt mee als info).
    """
    from models.company import CompanyOdoo
    rows = (CompanyOdoo.voor_mcp()
            .order_by(CompanyOdoo.db_name, CompanyOdoo.company_id)
            .all())
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r.db_name, []).append(r)
    return grouped


def list_tenants() -> list[dict]:
    """Beschikbare databases voor de MCP-client (metadata, geen sleutels)."""
    out = []
    for db_name, rows in _rows_by_db().items():
        first = rows[0]
        out.append({
            "database":   db_name,
            "url":        first.db_url,
            "companies":  [{"company_id": r.company_id, "name": r.company_name}
                           for r in rows],
            "aantal_companies": len(rows),
        })
    out.sort(key=lambda t: t["database"].lower())
    return out


def _build_api(db_name: str) -> OdooAPI:
    rows = _rows_by_db().get(db_name)
    if not rows:
        raise TenantError(f"Onbekende database: {db_name!r}")
    row = rows[0]
    api = OdooAPI(url=row.db_url, db=row.db_name,
                  user=row.db_user, api_key=row.get_api_key())
    if not api.authenticate():
        raise TenantError(f"Kon niet inloggen op Odoo-database {db_name!r}.")
    return api


def get_odoo_api(database: str) -> OdooAPI:
    """Geauthenticeerde OdooAPI voor `database`, met korte in-proces cache."""
    if not database:
        raise TenantError("Geen database opgegeven. Gebruik eerst list_databases.")
    now = time.time()
    with _CACHE_GUARD:
        cached = _API_CACHE.get(database)
        if cached and (now - cached[1]) < _AUTH_TTL_SECONDS and cached[0].uid:
            return cached[0]
    # buiten de lock authenticeren (netwerk); daarna cachen
    api = _build_api(database)
    with _CACHE_GUARD:
        _API_CACHE[database] = (api, now)
    return api


def invalidate(database: str | None = None) -> None:
    with _CACHE_GUARD:
        if database is None:
            _API_CACHE.clear()
        else:
            _API_CACHE.pop(database, None)
