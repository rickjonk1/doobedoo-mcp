"""
app.py — doobedoo MCP-server
============================
Eén kleine Flask-app met:
  - inloggen met wachtwoord + verplichte 2FA (auth.py)
  - beheer van gebruikers, Odoo-databases en koppelingen (beheer.py)
  - de OAuth 2.1 authorization-server voor Claude/ChatGPT (partner_api/oauth2.py)
  - de MCP-server zelf op /mcp (mcp_server/)

Draait achter Caddy (TLS). Data in SQLite op de versleutelde datadisk.
"""
import logging
import os
from datetime import timedelta

from flask import Flask, request
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from config_loader import DATABASE_URL, IS_PRODUCTION, MERK, PUBLIEKE_URL
from database import db
from extensions import limiter

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("doobedoo")

csrf = CSRFProtect()


def _sessie_sleutel() -> str:
    sleutel = os.environ.get("FLASK_SECRET", "")
    if len(sleutel) >= 32:
        return sleutel
    if IS_PRODUCTION:
        raise RuntimeError("FLASK_SECRET ontbreekt of is korter dan 32 tekens.")
    return "ontwikkel-sleutel-niet-voor-productie-gebruiken-0000"


def maak_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    # Caddy zet X-Forwarded-For/Proto; precies één proxy ervoor.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)

    app.config.update(
        SECRET_KEY=_sessie_sleutel(),
        SQLALCHEMY_DATABASE_URI=DATABASE_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_NAME="__Host-sessie" if IS_PRODUCTION else "sessie",
        SESSION_COOKIE_SECURE=IS_PRODUCTION,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=IS_PRODUCTION,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        WTF_CSRF_TIME_LIMIT=None,          # gebonden aan de sessie (8 uur)
        MERK=MERK,
        PUBLIEKE_URL=PUBLIEKE_URL,
    )
    if DATABASE_URL.startswith("sqlite"):
        # Meerdere gunicorn-threads: wachten op een schrijfslot i.p.v. meteen falen.
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 15}}
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    limiter.init_app(app)
    csrf.init_app(app)

    from auth import auth_bp, init_auth
    init_auth(app)
    app.register_blueprint(auth_bp)

    from partner_api.oauth2 import register_oauth2
    register_oauth2(app, limiter)

    from mcp_server import register_mcp
    register_mcp(app, limiter)

    from beheer import beheer_bp
    app.register_blueprint(beheer_bp)

    # Machine-endpoints hebben geen browsersessie en dus geen CSRF-token.
    for endpoint in ("oauth2.token", "oauth2.register_client", "mcp_server.mcp_post",
                     "mcp_server.mcp_delete"):
        csrf.exempt(app.view_functions[endpoint])

    @app.context_processor
    def _merk():
        return {"merk": MERK, "publieke_url": PUBLIEKE_URL}

    @app.after_request
    def _beveiligingsheaders(resp):
        h = resp.headers
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                     "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                     "base-uri 'none'; object-src 'none'")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")   # nodig voor de CSRF-check over https
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if IS_PRODUCTION:
            h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if request.endpoint and not request.endpoint.startswith("static"):
            h.setdefault("Cache-Control", "no-store")
        return resp

    @app.route("/gezond")
    @limiter.exempt
    def gezond():
        return {"status": "ok"}

    with app.app_context():
        import models  # noqa: F401  — registreert alle tabellen
        db.create_all()
        if DATABASE_URL.startswith("sqlite"):
            with db.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        from beheer import eerste_beheerder
        eerste_beheerder()

    from cli import registreer_cli
    registreer_cli(app)
    return app


app = maak_app() if os.environ.get("DOOBEDOO_GEEN_AUTOAPP") != "1" else None
