"""
Rookproef: inloggen met 2FA, OAuth 2.1 + PKCE, en een MCP-aanroep.
Odoo zelf is niet nodig (tools/list en list_databases).
"""
import base64
import hashlib
import os
import re
import secrets

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DOOBEDOO_GEEN_AUTOAPP", "1")

import pyotp
import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    import importlib
    import config_loader
    importlib.reload(config_loader)
    import app as appmod
    importlib.reload(appmod)
    a = appmod.maak_app({"TESTING": True, "RATELIMIT_ENABLED": False})
    return a


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _maak_gebruiker(app, email="rick@doobedoo.nl", rol="admin"):
    from auth import bcrypt
    from database import db
    from models.user import User
    with app.app_context():
        u = User(email=email, name="Rick Jonk", role=rol,
                 password_hash=bcrypt.generate_password_hash("een-lang-wachtwoord").decode())
        db.session.add(u)
        db.session.commit()


def _login(c):
    r = c.get("/login")
    r = c.post("/login", data={"csrf_token": _csrf(r.text), "email": "rick@doobedoo.nl",
                               "password": "een-lang-wachtwoord"})
    assert r.status_code == 302 and "/2fa/instellen" in r.location
    r = c.get("/2fa/instellen")
    geheim = re.search(r'Handmatig: ([A-Z2-7]+)<', r.text).group(1)
    r = c.post("/2fa/instellen", data={"csrf_token": _csrf(r.text),
                                       "code": pyotp.TOTP(geheim).now()})
    assert r.status_code == 302
    return geheim


def test_inloggen_en_beveiligingsheaders(app):
    _maak_gebruiker(app)
    c = app.test_client()
    assert c.get("/").status_code == 302                     # niet ingelogd
    _login(c)
    r = c.get("/")
    assert r.status_code == 200 and "Claude koppelen" in r.text
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    for pad in ("/databases", "/koppelingen", "/gebruikers", "/logboek", "/account"):
        assert c.get(pad).status_code == 200, pad


def test_post_zonder_csrf_wordt_geweigerd(app):
    _maak_gebruiker(app)
    c = app.test_client()
    _login(c)
    r = c.post("/gebruikers", data={"email": "x@y.nl", "naam": "X"})
    assert r.status_code == 400


def test_interne_adressen_geweigerd():
    from beheer import controleer_odoo_url
    for url in ("http://bedrijf.odoo.com", "https://127.0.0.1", "https://169.254.169.254",
                "https://localhost", "https://bedrijf.odoo.com/web"):
        with pytest.raises(ValueError):
            controleer_odoo_url(url)


def test_uitnodiging_en_wachtwoord(app):
    _maak_gebruiker(app)
    c = app.test_client()
    _login(c)
    r = c.get("/gebruikers")
    r = c.post("/gebruikers", data={"csrf_token": _csrf(r.text), "email": "eva@doobedoo.nl",
                                    "naam": "Eva de Wit", "rol": "gebruiker"})
    link = re.search(r'id="uitnodig-link">([^<]+)<', r.text).group(1)
    pad = link.split("mcp.doobedoo.nl")[1]
    c2 = app.test_client()
    r = c2.get(pad)
    assert "Kies een wachtwoord" in r.text
    r = c2.post(pad, data={"csrf_token": _csrf(r.text), "nieuw": "kort", "herhaal": "kort"})
    assert "minstens 12" in r.text
    r = c2.post(pad, data={"csrf_token": _csrf(c2.get(pad).text),
                           "nieuw": "nog-een-lang-wachtwoord", "herhaal": "nog-een-lang-wachtwoord"})
    assert r.status_code == 302
    assert c2.get(pad).status_code == 410                    # eenmalig


def test_oauth_pkce_en_mcp(app):
    _maak_gebruiker(app)
    c = app.test_client()
    meta = c.get("/.well-known/oauth-authorization-server").json
    assert meta["issuer"] == "https://mcp.doobedoo.nl"
    reg = c.post("/oauth2/register", json={"client_name": "Claude",
                                           "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
    assert reg.status_code == 201
    cid = reg.json["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    q = (f"/oauth2/authorize?client_id={cid}&redirect_uri=https://claude.ai/api/mcp/auth_callback"
         f"&response_type=code&state=abc&code_challenge={challenge}&code_challenge_method=S256")
    r = c.get(q)
    assert r.status_code == 302 and "/login" in r.location
    _login(c)
    r = c.get(q)
    assert "Claude toegang geven?" in r.text
    params = re.search(r'name="oauth_params" value="([^"]+)"', r.text).group(1)
    r = c.post("/oauth2/authorize", data={"csrf_token": _csrf(r.text), "oauth_params": params,
                                          "decision": "allow"})
    assert r.status_code == 302 and "code=" in r.location and "state=abc" in r.location
    code = re.search(r"code=([^&]+)", r.location).group(1)
    tok = c.post("/oauth2/token", data={"grant_type": "authorization_code", "code": code,
                                        "client_id": cid, "code_verifier": verifier,
                                        "redirect_uri": "https://claude.ai/api/mcp/auth_callback"})
    assert tok.status_code == 200, tok.text
    access = tok.json["access_token"]
    assert set(tok.json["scope"].split()) == {"odoo:read", "odoo:write"}

    m = app.test_client()
    assert m.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    h = {"Authorization": f"Bearer {access}"}
    r = m.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {"protocolVersion": "2025-06-18"}})
    assert r.json["result"]["serverInfo"]["name"] == "doobedoo-odoo-mcp"
    namen = {t["name"] for t in m.post("/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json["result"]["tools"]}
    assert {"list_databases", "search_records", "create_record"} <= namen
    assert not any(n.startswith("av_") for n in namen)
    r = m.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "list_databases", "arguments": {}}})
    assert r.json["result"]["structuredContent"] == {"databases": []}
    res = m.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                                          "params": {"uri": "doobedoo://huisstijl"}}).json
    assert "#5B21FF" in res["result"]["contents"][0]["text"]

    # Intrekken via de koppelingenpagina werkt direct
    r = c.get("/koppelingen")
    assert "Claude" in r.text
    gid = re.search(r'name="gebruiker_id" value="(\d+)"', r.text).group(1)
    c.post("/koppelingen/intrekken", data={"csrf_token": _csrf(r.text), "client_id": cid,
                                           "gebruiker_id": gid})
    assert m.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 5, "method": "ping"}).status_code == 401


def test_formulier_over_https_met_referer(app):
    """Over https eist Flask-WTF een Referer van dezelfde site; de
    Referrer-Policy mag die dus niet wegfilteren."""
    _maak_gebruiker(app)
    c = app.test_client()
    r = c.get("/login", base_url="https://mcp.doobedoo.nl")
    assert r.headers["Referrer-Policy"] == "same-origin"
    r = c.post("/login", base_url="https://mcp.doobedoo.nl",
               headers={"Referer": "https://mcp.doobedoo.nl/login"},
               data={"csrf_token": _csrf(r.text), "email": "rick@doobedoo.nl",
                     "password": "een-lang-wachtwoord"})
    assert r.status_code == 302
