"""
mcp_server/tools.py — de MCP-tools (dunne wrappers over OdooAPI)
================================================================
Generieke Odoo-ORM-tools, multi-tenant: elke tool krijgt een `database`-argument
en werkt via het API-account dat bij die database is geregistreerd. De tool-schema's zijn
geïnspireerd op pantalytics/odoo-mcp-pro (MPL-2.0) maar de uitvoering loopt over
onze eigen `OdooAPI`.

Read-tools:  list_databases, search_records, get_record, list_models, server_info
Write-tools: create_record, update_record, delete_record, execute_method
             (alleen beschikbaar met de scope 'odoo:write')

Elke tool is een dict: {name, description, inputSchema, handler, write}.
`handler(args, ctx)` geeft een JSON-serialiseerbaar resultaat; een ValueError
wordt door de protocol-laag omgezet in een nette tool-fout.
"""
from __future__ import annotations

import logging

from .documents import BINAIRE_VELDEN, DOCUMENT_TOOLS
from .inkoopfacturen import INKOOPFACTUUR_TOOLS
from .tenants import get_odoo_api, list_tenants

logger = logging.getLogger(__name__)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

SCOPE_READ = "odoo:read"
SCOPE_WRITE = "odoo:write"
# execute_method roept een WILLEKEURIGE Odoo-methode aan (execute_kw) en is
# daarmee veel krachtiger dan de gestructureerde CRUD-tools: server-actions,
# massa-unlink, ORM-internals. Daarom een aparte scope die alléén admins
# krijgen (zie partner_api/oauth2._grantable_scopes), niet de brede odoo:write.
SCOPE_ADMIN = "odoo:admin"


def _api(args):
    return get_odoo_api((args or {}).get("database") or "")


def _clamp_limit(val):
    try:
        n = int(val)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))


def _domain(args):
    d = (args or {}).get("domain")
    if d in (None, ""):
        return []
    if not isinstance(d, list):
        raise ValueError("`domain` moet een lijst zijn, bv. [[\"state\",\"=\",\"posted\"]].")
    return d


def _require_str(args, key):
    v = (args or {}).get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"`{key}` is verplicht (tekst).")
    return v.strip()


def _weiger_binaire_velden(fields):
    """
    Binaire velden ('datas', 'raw', ...) mogen niet via de generieke leestools.

    Ze bevatten de ruwe bestandsinhoud als base64. Dat is niet alleen enorm —
    de grootste bijlage in productie is als base64 ruim 1,1 miljoen tokens —
    maar vooral onleesbaar: base64 van een PDF is voor een model geen tekst.
    read_document zet het document om in iets waar wél mee te werken valt.
    """
    if not fields:
        return
    gevonden = [f for f in fields if isinstance(f, str) and f in BINAIRE_VELDEN]
    if gevonden:
        raise ValueError(
            f"Veld(en) {', '.join(sorted(gevonden))} bevatten ruwe bestandsinhoud "
            f"en worden niet via deze tool teruggegeven. Gebruik list_attachments "
            f"om te zien wat er is en read_document om het te lezen."
        )


def _record_id(args):
    v = (args or {}).get("record_id")
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("`record_id` is verplicht (geheel getal).")
    return v


# ── Read-tools ───────────────────────────────────────────────────────────────

def _list_databases(args, ctx):
    return {"databases": list_tenants()}


def _search_records(args, ctx):
    api = _api(args)
    model = _require_str(args, "model")
    fields = (args or {}).get("fields") or []
    _weiger_binaire_velden(fields)
    opts = {"limit": _clamp_limit((args or {}).get("limit", DEFAULT_LIMIT)),
            "offset": int((args or {}).get("offset") or 0)}
    if fields:
        opts["fields"] = fields
    order = (args or {}).get("order")
    if order:
        opts["order"] = str(order)
    rows = api.execute(model, "search_read", [_domain(args)], opts)
    return {"model": model, "count": len(rows), "records": rows}


def _get_record(args, ctx):
    api = _api(args)
    model = _require_str(args, "model")
    rid = _record_id(args)
    fields = (args or {}).get("fields") or []
    _weiger_binaire_velden(fields)
    opts = {"fields": fields} if fields else None
    rows = api.execute(model, "read", [[rid]], opts)
    if not rows:
        raise ValueError(f"Record {rid} niet gevonden in {model}.")
    return {"model": model, "record": rows[0]}


def _list_models(args, ctx):
    api = _api(args)
    flt = (args or {}).get("filter") or ""
    domain = [["model", "ilike", flt]] if flt else []
    rows = api.execute("ir.model", "search_read", [domain],
                       {"fields": ["model", "name"], "limit": MAX_LIMIT, "order": "model"})
    return {"count": len(rows), "models": rows}


def _odoo_versie(api):
    """De Odoo-versie van deze database, uit de basemodule.

    Staat erbij omdat het anders giswerk is: de databases van het kantoor lopen
    uiteen van 17.0 (biznify) tot 19.0 (odoo.com), en een fout die "aan de
    versie ligt" is zonder dit getal niet te controleren."""
    try:
        rijen = api.execute("ir.module.module", "search_read",
                            [[["name", "=", "base"]]],
                            {"fields": ["latest_version"], "limit": 1})
    except Exception as exc:                       # noqa: BLE001 — extraatje, geen must
        logger.warning("[MCP] Odoo-versie opvragen mislukt (%s: %s)",
                       type(exc).__name__, exc)
        return None
    if not rijen:
        return None
    return rijen[0].get("latest_version") or None


def _server_info(args, ctx):
    api = _api(args)
    return {"database": (args or {}).get("database"), "url": api.url,
            "uid": api.uid, "connected": bool(api.uid),
            "odoo_version": _odoo_versie(api)}


# ── Write-tools (alleen met scope odoo:write) ────────────────────────────────

def _values(args):
    v = (args or {}).get("values")
    if not isinstance(v, dict) or not v:
        raise ValueError("`values` is verplicht (object met veld→waarde).")
    return v


def _create_record(args, ctx):
    api = _api(args)
    model = _require_str(args, "model")
    new_id = api.execute(model, "create", [_values(args)])
    return {"model": model, "created_id": new_id}


def _update_record(args, ctx):
    api = _api(args)
    model = _require_str(args, "model")
    rid = _record_id(args)
    ok = api.execute(model, "write", [[rid], _values(args)])
    return {"model": model, "record_id": rid, "updated": bool(ok)}


def _delete_record(args, ctx):
    api = _api(args)
    model = _require_str(args, "model")
    rid = _record_id(args)
    ok = api.execute(model, "unlink", [[rid]])
    return {"model": model, "record_id": rid, "deleted": bool(ok)}


def _execute_method(args, ctx):
    api = _api(args)
    model = _require_str(args, "model")
    method = _require_str(args, "method")
    call_args = (args or {}).get("args") or []
    if not isinstance(call_args, list):
        raise ValueError("`args` moet een lijst zijn.")
    kwargs = (args or {}).get("kwargs") or None
    if kwargs is not None and not isinstance(kwargs, dict):
        raise ValueError("`kwargs` moet een object zijn.")
    result = api.execute(model, method, call_args, kwargs)
    return {"model": model, "method": method, "result": result}


# ── Registry ─────────────────────────────────────────────────────────────────

_DB_PROP = {"type": "string",
            "description": "Naam van de Odoo-database (zie list_databases)."}
_MODEL_PROP = {"type": "string", "description": "Odoo-model, bv. 'res.partner'."}

TOOLS = [
    {
        "name": "list_databases",
        "description": "Toon de beschikbare Odoo-databases (tenants) waar je mee kunt werken. "
                       "Roep dit eerst aan en gebruik de 'database'-naam in de andere tools.",
        "write": False,
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _list_databases,
    },
    {
        "name": "search_records",
        "description": "Zoek records in een Odoo-model met een domain-filter. "
                       "Geeft de gevonden records terug (search_read).",
        "write": False,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB_PROP,
                "model": _MODEL_PROP,
                "domain": {"type": "array",
                           "description": "Odoo-domain, bv. [[\"state\",\"=\",\"posted\"]]. Leeg = alles.",
                           "items": {}},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "Velden om terug te geven. Leeg = standaardvelden."},
                "limit": {"type": "integer", "description": f"Max records (1-{MAX_LIMIT})."},
                "offset": {"type": "integer", "description": "Overslaan (paginering)."},
                "order": {"type": "string", "description": "Sortering, bv. 'date desc'."},
            },
            "required": ["database", "model"],
            "additionalProperties": False,
        },
        "handler": _search_records,
    },
    {
        "name": "get_record",
        "description": "Haal één record op via zijn id (read).",
        "write": False,
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": _DB_PROP, "model": _MODEL_PROP,
                "record_id": {"type": "integer", "description": "Het id van het record."},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["database", "model", "record_id"],
            "additionalProperties": False,
        },
        "handler": _get_record,
    },
    {
        "name": "list_models",
        "description": "Ontdek beschikbare Odoo-modellen (ir.model), optioneel gefilterd op naam.",
        "write": False,
        "inputSchema": {
            "type": "object",
            "properties": {"database": _DB_PROP,
                           "filter": {"type": "string", "description": "Zoekterm in de modelnaam."}},
            "required": ["database"],
            "additionalProperties": False,
        },
        "handler": _list_models,
    },
    {
        "name": "server_info",
        "description": "Verbindingsstatus, uid en Odoo-versie van een database.",
        "write": False,
        "inputSchema": {
            "type": "object",
            "properties": {"database": _DB_PROP},
            "required": ["database"],
            "additionalProperties": False,
        },
        "handler": _server_info,
    },
    {
        "name": "create_record",
        "description": "Maak een nieuw record aan (create). Vereist schrijfrechten.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {"database": _DB_PROP, "model": _MODEL_PROP,
                           "values": {"type": "object", "description": "Veld→waarde."}},
            "required": ["database", "model", "values"],
            "additionalProperties": False,
        },
        "handler": _create_record,
    },
    {
        "name": "update_record",
        "description": "Wijzig een bestaand record (write). Vereist schrijfrechten.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {"database": _DB_PROP, "model": _MODEL_PROP,
                           "record_id": {"type": "integer"},
                           "values": {"type": "object", "description": "Veld→waarde."}},
            "required": ["database", "model", "record_id", "values"],
            "additionalProperties": False,
        },
        "handler": _update_record,
    },
    {
        "name": "delete_record",
        "description": "Verwijder een record (unlink). Vereist schrijfrechten.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {"database": _DB_PROP, "model": _MODEL_PROP,
                           "record_id": {"type": "integer"}},
            "required": ["database", "model", "record_id"],
            "additionalProperties": False,
        },
        "handler": _delete_record,
    },
    {
        "name": "execute_method",
        "description": "Roep een willekeurige model-methode aan (execute_kw). Krachtig — "
                       "vereist schrijfrechten.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {"database": _DB_PROP, "model": _MODEL_PROP,
                           "method": {"type": "string"},
                           "args": {"type": "array", "items": {}},
                           "kwargs": {"type": "object"}},
            "required": ["database", "model", "method"],
            "additionalProperties": False,
        },
        "handler": _execute_method,
    },
]

# Elke Odoo-tool krijgt zijn vereiste scope (read/write). Zo bepaalt de
# token-scope wat een client ziet/mag.
for _t in TOOLS:
    _t.setdefault("scope", SCOPE_WRITE if _t.get("write") else SCOPE_READ)

# Odoo-, document- en inkoopfactuurtools samen in één registry.
TOOLS = TOOLS + DOCUMENT_TOOLS + INKOOPFACTUUR_TOOLS
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def tool_allowed(tool: dict, granted_scopes) -> bool:
    """Een tool is beschikbaar als de token de vereiste scope heeft."""
    return tool.get("scope", SCOPE_READ) in set(granted_scopes or ())


def public_tool_list(granted_scopes) -> list[dict]:
    """Tools voor tools/list — alleen die passen bij de scopes van de token."""
    out = []
    for t in TOOLS:
        if not tool_allowed(t, granted_scopes):
            continue
        out.append({"name": t["name"], "description": t["description"],
                    "inputSchema": t["inputSchema"]})
    return out
