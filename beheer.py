"""
beheer.py — de beheerschermen
=============================
Overzicht (hoe koppel je Claude), Odoo-databases registreren, koppelingen
(uitgegeven tokens) inzien en intrekken, gebruikers uitnodigen, logboek en
het eigen account.

Alles server-side gerenderd; formulieren met CSRF-token (Flask-WTF).
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import socket
from datetime import timedelta
from urllib.parse import urlparse

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required, logout_user

from auth import admin_required, bcrypt
from database import db
from extensions import limiter
from utils import nl_now

logger = logging.getLogger(__name__)

beheer_bp = Blueprint("beheer", __name__)

MIN_WACHTWOORD = 12
UITNODIGING_GELDIG = timedelta(hours=72)


# ── Hulpjes ──────────────────────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def maak_uitnodiging(user) -> str:
    """Nieuwe eenmalige link om een wachtwoord in te stellen. Alleen de hash
    gaat de database in; de link zelf wordt één keer getoond."""
    token = secrets.token_urlsafe(32)
    user.uitnodiging_hash = _hash(token)
    user.uitnodiging_tot = nl_now() + UITNODIGING_GELDIG
    db.session.commit()
    from config_loader import PUBLIEKE_URL
    return f"{PUBLIEKE_URL}/welkom/{token}"


def _onbruikbaar_wachtwoord() -> str:
    return bcrypt.generate_password_hash(secrets.token_urlsafe(48)).decode()


def eerste_beheerder() -> None:
    """Lege database + ADMIN_EMAIL gezet: maak de eerste beheerder aan en zet
    de uitnodigingslink in het serverlogboek (alleen via de AWS-console te zien)."""
    import os
    from models.user import User
    if User.query.count() > 0:
        return
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    if not email:
        logger.warning("Nog geen gebruikers. Zet ADMIN_EMAIL of gebruik `flask uitnodiging`.")
        return
    user = User(email=email, name="Beheerder", role="admin",
                password_hash=_onbruikbaar_wachtwoord(), totp_enabled=False)
    db.session.add(user)
    db.session.commit()
    logger.warning("Eerste beheerder %s aangemaakt. Wachtwoord instellen via: %s",
                   email, maak_uitnodiging(user))


_PRIVE_NETTEN = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]


def controleer_odoo_url(url: str) -> str:
    """Alleen https naar een publiek adres. Voorkomt dat de server via een
    'database-URL' interne adressen benadert (zoals de AWS-metadataservice)."""
    url = (url or "").strip().rstrip("/")
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Gebruik een https-adres, bijvoorbeeld https://bedrijf.odoo.com.")
    if p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("Geef alleen het adres van de Odoo-omgeving, zonder pad.")
    try:
        adressen = {ai[4][0] for ai in socket.getaddrinfo(p.hostname, 443)}
    except socket.gaierror:
        raise ValueError(f"{p.hostname} is niet te vinden. Controleer het adres.")
    for a in adressen:
        ip = ipaddress.ip_address(a.split("%")[0])
        if any(ip in net for net in _PRIVE_NETTEN):
            raise ValueError("Dit adres wijst naar een intern netwerk en is niet toegestaan.")
    return f"https://{p.hostname}" + (f":{p.port}" if p.port else "")


def _laatst_gebruikt() -> dict:
    """client_id -> laatste aanroep (uit het logboek)."""
    from sqlalchemy import func
    from models.partner_client import PartnerApiLog
    rijen = (db.session.query(PartnerApiLog.client_id, func.max(PartnerApiLog.ts))
             .filter(PartnerApiLog.status == 200)
             .group_by(PartnerApiLog.client_id).all())
    return {cid: ts for cid, ts in rijen if cid}


def _tijd(ts) -> str:
    if not ts:
        return "Nog niet"
    delta = nl_now() - ts
    if delta < timedelta(minutes=1):
        return "Zojuist"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} min geleden"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} uur geleden"
    if delta < timedelta(days=2):
        return "Gisteren"
    return ts.strftime("%d-%m-%Y")


# ── Overzicht ────────────────────────────────────────────────────────────────

@beheer_bp.route("/")
@login_required
def overzicht():
    from mcp_server.tenants import list_tenants
    from models.partner_client import PartnerToken
    databases = list_tenants()
    eigen = (PartnerToken.query
             .filter(PartnerToken.subject_user_id == current_user.id,
                     PartnerToken.revoked.is_(False),
                     PartnerToken.refresh_expires > nl_now())
             .count())
    return render_template("overzicht.html", databases=databases, eigen_koppelingen=eigen)


# ── Databases ────────────────────────────────────────────────────────────────

@beheer_bp.route("/databases")
@login_required
def databases():
    from models.company import CompanyOdoo
    rijen = CompanyOdoo.query.order_by(CompanyOdoo.db_name, CompanyOdoo.company_id).all()
    per_db: dict[str, dict] = {}
    for r in rijen:
        d = per_db.setdefault(r.db_name, {"db_name": r.db_name, "url": r.db_url,
                                          "gebruiker": r.db_user, "actief": False,
                                          "bedrijven": []})
        d["bedrijven"].append({"id": r.company_id, "naam": r.company_name,
                               "actief": r.active})
        d["actief"] = d["actief"] or r.active
    return render_template("databases.html", databases=list(per_db.values()))


def _verbind(url, database, gebruiker, sleutel):
    from odoo_api import OdooAPI
    api = OdooAPI(url=url, db=database, user=gebruiker, api_key=sleutel, timeout=15)
    if not api.authenticate():
        raise ValueError("Inloggen bij Odoo lukt niet. Controleer database, gebruiker en API-sleutel.")
    bedrijven = api.execute("res.company", "search_read", [[]],
                            {"fields": ["id", "name"], "order": "id"})
    return api, bedrijven


@beheer_bp.route("/databases/testen", methods=["POST"])
@login_required
@admin_required
@limiter.limit("20 per minute")
def database_testen():
    f = request.get_json(silent=True) or {}
    try:
        url = controleer_odoo_url(f.get("url"))
        database = (f.get("database") or "").strip()
        gebruiker = (f.get("gebruiker") or "").strip()
        sleutel = (f.get("sleutel") or "").strip()
        if not (database and gebruiker and sleutel):
            raise ValueError("Vul database, gebruiker en API-sleutel in.")
        _, bedrijven = _verbind(url, database, gebruiker, sleutel)
    except ValueError as exc:
        return jsonify({"ok": False, "fout": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Verbindingstest mislukt: %s", type(exc).__name__)
        return jsonify({"ok": False, "fout": "Odoo is niet bereikbaar op dit adres."}), 400
    return jsonify({"ok": True, "url": url,
                    "bedrijven": [{"id": b["id"], "naam": b["name"]} for b in bedrijven]})


@beheer_bp.route("/databases", methods=["POST"])
@login_required
@admin_required
@limiter.limit("20 per minute")
def database_opslaan():
    from mcp_server.tenants import invalidate
    from models.company import CompanyOdoo
    f = request.form
    try:
        url = controleer_odoo_url(f.get("url"))
        database = (f.get("database") or "").strip()
        gebruiker = (f.get("gebruiker") or "").strip()
        sleutel = (f.get("sleutel") or "").strip()
        if not (database and gebruiker and sleutel):
            raise ValueError("Vul database, gebruiker en API-sleutel in.")
        _, bedrijven = _verbind(url, database, gebruiker, sleutel)
    except ValueError as exc:
        flash(str(exc), "fout")
        return redirect(url_for("beheer.databases"))
    except Exception:  # noqa: BLE001
        flash("Odoo is niet bereikbaar op dit adres.", "fout")
        return redirect(url_for("beheer.databases"))

    gekozen = {int(x) for x in f.getlist("bedrijf") if x.isdigit()}
    if not gekozen:
        gekozen = {b["id"] for b in bedrijven}
    namen = {b["id"]: b["name"] for b in bedrijven}
    for cid in gekozen & set(namen):
        rij = CompanyOdoo.query.filter_by(db_name=database, company_id=cid).first()
        if rij is None:
            rij = CompanyOdoo(db_name=database, company_id=cid)
            db.session.add(rij)
        rij.db_url, rij.db_user, rij.company_name, rij.active = url, gebruiker, namen[cid], True
        rij.set_api_key(sleutel)
    # Ook de sleutel van bestaande, niet-gekozen bedrijven bijwerken (zelfde account).
    for rij in CompanyOdoo.query.filter_by(db_name=database).all():
        rij.db_url, rij.db_user = url, gebruiker
        rij.set_api_key(sleutel)
    db.session.commit()
    invalidate(database)
    logger.info("Database %s geregistreerd door %s", database, current_user.email)
    flash(f"{database} is gekoppeld.", "ok")
    return redirect(url_for("beheer.databases"))


@beheer_bp.route("/databases/<path:db_name>/actief", methods=["POST"])
@login_required
@admin_required
def database_actief(db_name):
    from mcp_server.tenants import invalidate
    from models.company import CompanyOdoo
    rijen = CompanyOdoo.query.filter_by(db_name=db_name).all()
    if not rijen:
        abort(404)
    aan = request.form.get("aan") == "1"
    for r in rijen:
        r.active = aan
    db.session.commit()
    invalidate(db_name)
    flash(f"{db_name} staat {'aan' if aan else 'uit'}.", "ok")
    return redirect(url_for("beheer.databases"))


@beheer_bp.route("/databases/<path:db_name>/verwijderen", methods=["POST"])
@login_required
@admin_required
def database_verwijderen(db_name):
    from mcp_server.tenants import invalidate
    from models.company import CompanyOdoo
    n = CompanyOdoo.query.filter_by(db_name=db_name).delete()
    db.session.commit()
    invalidate(db_name)
    if n:
        logger.info("Database %s verwijderd door %s", db_name, current_user.email)
        flash(f"{db_name} is verwijderd. De API-sleutel is gewist.", "ok")
    return redirect(url_for("beheer.databases"))


# ── Koppelingen ──────────────────────────────────────────────────────────────

@beheer_bp.route("/koppelingen")
@login_required
def koppelingen():
    from models.partner_client import PartnerClient, PartnerToken
    from models.user import User
    q = (PartnerToken.query
         .filter(PartnerToken.revoked.is_(False), PartnerToken.refresh_expires > nl_now()))
    if not current_user.is_admin():
        q = q.filter(PartnerToken.subject_user_id == current_user.id)
    laatst = _laatst_gebruikt()
    groepen: dict[tuple, dict] = {}
    for t in q.order_by(PartnerToken.created_at.desc()).all():
        sleutel = (t.client_id, t.subject_user_id)
        if sleutel in groepen:
            continue
        client = PartnerClient.query.filter_by(client_id=t.client_id).first()
        user = User.query.get(t.subject_user_id) if t.subject_user_id else None
        groepen[sleutel] = {
            "client_id": t.client_id,
            "app": client.name if client else t.client_id,
            "gebruiker": (user.name or user.email) if user else "—",
            "gebruiker_id": t.subject_user_id,
            "schrijven": "odoo:write" in t.scope_set(),
            "sinds": t.created_at.strftime("%d-%m-%Y") if t.created_at else "",
            "laatst": _tijd(laatst.get(t.client_id)),
        }
    return render_template("koppelingen.html", koppelingen=list(groepen.values()))


@beheer_bp.route("/koppelingen/intrekken", methods=["POST"])
@login_required
def koppeling_intrekken():
    from models.partner_client import PartnerToken
    client_id = request.form.get("client_id") or ""
    gebruiker_id = request.form.get("gebruiker_id")
    gebruiker_id = int(gebruiker_id) if (gebruiker_id or "").isdigit() else None
    if not current_user.is_admin() and gebruiker_id != current_user.id:
        abort(403)
    n = (PartnerToken.query
         .filter_by(client_id=client_id, subject_user_id=gebruiker_id, revoked=False)
         .update({"revoked": True}))
    db.session.commit()
    logger.info("Koppeling %s/%s ingetrokken door %s (%s tokens)",
                client_id, gebruiker_id, current_user.email, n)
    flash("De koppeling is ingetrokken. De app moet opnieuw toestemming vragen.", "ok")
    return redirect(url_for("beheer.koppelingen"))


# ── Gebruikers ───────────────────────────────────────────────────────────────

@beheer_bp.route("/gebruikers")
@login_required
@admin_required
def gebruikers():
    from models.user import User
    lijst = User.query.order_by(User.active.desc(), User.name).all()
    rijen = [{
        "id": u.id, "naam": u.name or u.email, "email": u.email, "rol": u.role,
        "actief": u.active, "tweestaps": u.totp_enabled,
        "uitgenodigd": bool(u.uitnodiging_hash and u.uitnodiging_tot
                            and u.uitnodiging_tot > nl_now()),
        "laatst": _tijd(u.last_login_at), "ik": u.id == current_user.id,
    } for u in lijst]
    return render_template("gebruikers.html", gebruikers=rijen)


@beheer_bp.route("/gebruikers", methods=["POST"])
@login_required
@admin_required
def gebruiker_uitnodigen():
    from models.user import User
    email = (request.form.get("email") or "").strip().lower()
    naam = (request.form.get("naam") or "").strip()[:150]
    rol = "admin" if request.form.get("rol") == "admin" else "gebruiker"
    if "@" not in email or len(email) > 150:
        flash("Vul een geldig e-mailadres in.", "fout")
        return redirect(url_for("beheer.gebruikers"))
    if User.query.filter_by(email=email).first():
        flash("Er bestaat al een gebruiker met dit e-mailadres.", "fout")
        return redirect(url_for("beheer.gebruikers"))
    user = User(email=email, name=naam or email, role=rol,
                password_hash=_onbruikbaar_wachtwoord(), totp_enabled=False)
    db.session.add(user)
    db.session.commit()
    link = maak_uitnodiging(user)
    logger.info("Gebruiker %s uitgenodigd door %s", email, current_user.email)
    return render_template("uitnodiging.html", gebruiker=user, link=link)


@beheer_bp.route("/gebruikers/<int:uid>/<actie>", methods=["POST"])
@login_required
@admin_required
def gebruiker_actie(uid, actie):
    from models.partner_client import PartnerToken
    from models.user import User
    user = User.query.get_or_404(uid)
    if user.id == current_user.id and actie in ("uit", "rol"):
        flash("Dit kun je niet bij je eigen account doen.", "fout")
        return redirect(url_for("beheer.gebruikers"))
    if actie == "uitnodiging":
        return render_template("uitnodiging.html", gebruiker=user, link=maak_uitnodiging(user))
    if actie == "tweestaps":
        user.totp_enabled, user.totp_secret = False, None
        flash(f"Tweestapsverificatie van {user.email} is gereset. "
              "Bij de volgende keer inloggen stelt diegene een nieuwe in.", "ok")
    elif actie == "uit":
        user.active = False
        PartnerToken.query.filter_by(subject_user_id=user.id, revoked=False) \
            .update({"revoked": True})
        flash(f"{user.email} is uitgeschakeld en alle koppelingen zijn ingetrokken.", "ok")
    elif actie == "aan":
        user.active = True
        user.failed_logins, user.locked_until = 0, None
        flash(f"{user.email} is weer actief.", "ok")
    elif actie == "rol":
        user.role = "gebruiker" if user.role == "admin" else "admin"
        flash(f"{user.email} is nu {'beheerder' if user.role == 'admin' else 'gebruiker'}.", "ok")
    else:
        abort(404)
    db.session.commit()
    logger.info("Gebruikersactie %s op %s door %s", actie, user.email, current_user.email)
    return redirect(url_for("beheer.gebruikers"))


# ── Logboek ──────────────────────────────────────────────────────────────────

@beheer_bp.route("/logboek")
@login_required
@admin_required
def logboek():
    from models.partner_client import PartnerApiLog, PartnerClient
    from models.user import User
    rijen = PartnerApiLog.query.order_by(PartnerApiLog.ts.desc()).limit(200).all()
    apps = {c.client_id: c.name for c in PartnerClient.query.all()}
    mensen = {u.id: (u.name or u.email) for u in User.query.all()}
    regels = []
    for r in rijen:
        pad = r.path.split(":", 1)[1] if ":" in r.path else r.path
        wie = "—"
        if pad.startswith("u") and ":" in pad:
            uid, pad = pad[1:].split(":", 1)
            wie = mensen.get(int(uid), "—") if uid.isdigit() else "—"
        regels.append({"tijd": r.ts.strftime("%d-%m %H:%M:%S"), "app": apps.get(r.client_id, "—"),
                       "wie": wie, "wat": pad.replace(",", ", "), "status": r.status,
                       "ms": r.duration_ms, "ip": r.ip})
    return render_template("logboek.html", regels=regels)


# ── Account ──────────────────────────────────────────────────────────────────

@beheer_bp.route("/account", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def account():
    if request.method == "POST" and request.form.get("actie") == "naam":
        naam = (request.form.get("naam") or "").strip()[:150]
        if naam:
            current_user.name = naam
            db.session.commit()
            flash("Je naam is bewaard.", "ok")
        return redirect(url_for("beheer.account"))
    if request.method == "POST":
        huidig = request.form.get("huidig") or ""
        nieuw = request.form.get("nieuw") or ""
        if not bcrypt.check_password_hash(current_user.password_hash, huidig):
            flash("Je huidige wachtwoord klopt niet.", "fout")
        elif len(nieuw) < MIN_WACHTWOORD:
            flash(f"Kies een wachtwoord van minstens {MIN_WACHTWOORD} tekens.", "fout")
        elif nieuw != request.form.get("herhaal"):
            flash("De twee nieuwe wachtwoorden zijn niet gelijk.", "fout")
        else:
            current_user.password_hash = bcrypt.generate_password_hash(nieuw).decode()
            db.session.commit()
            flash("Je wachtwoord is gewijzigd.", "ok")
        return redirect(url_for("beheer.account"))
    return render_template("account.html")


# ── Uitnodiging accepteren ───────────────────────────────────────────────────

@beheer_bp.route("/welkom/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def welkom(token):
    from models.user import User
    user = User.query.filter_by(uitnodiging_hash=_hash(token)).first()
    if not user or not user.uitnodiging_tot or user.uitnodiging_tot < nl_now() or not user.active:
        return render_template("welkom.html", verlopen=True), 410
    if request.method == "POST":
        nieuw = request.form.get("nieuw") or ""
        if len(nieuw) < MIN_WACHTWOORD:
            flash(f"Kies een wachtwoord van minstens {MIN_WACHTWOORD} tekens.", "fout")
        elif nieuw != request.form.get("herhaal"):
            flash("De twee wachtwoorden zijn niet gelijk.", "fout")
        else:
            user.password_hash = bcrypt.generate_password_hash(nieuw).decode()
            user.uitnodiging_hash, user.uitnodiging_tot = None, None
            user.totp_enabled, user.totp_secret = False, None
            user.failed_logins, user.locked_until = 0, None
            db.session.commit()
            if current_user.is_authenticated:
                logout_user()
            flash("Je wachtwoord is ingesteld. Log in en stel daarna je "
                  "tweestapsverificatie in.", "ok")
            return redirect(url_for("auth.login"))
    return render_template("welkom.html", verlopen=False, gebruiker=user)
