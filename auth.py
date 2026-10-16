"""
auth.py — Flask-Login authenticatie + login-blueprint
======================================================
Sessie-gebaseerde authenticatie met bcrypt wachtwoord-hashing.
Rollen: admin / gebruiker. Inloggen is altijd wachtwoord + verplichte 2FA (TOTP).
Brute-force beveiliging zit in het User-model (models/user.py).

Bootstrap: als er geen gebruikers in de DB staan, worden ADMIN_EMAIL
en ADMIN_PASSWORD (uit env vars) gebruikt om de eerste admin aan te maken.
"""

import os
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from extensions import limiter

login_manager = LoginManager()
bcrypt = Bcrypt()

auth_bp = Blueprint("auth", __name__)


def init_auth(app):
    """Registreer Flask-Login en Bcrypt bij de Flask-app."""
    login_manager.init_app(app)
    bcrypt.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Log in om toegang te krijgen."
    login_manager.login_message_category = "info"
    # 'strong' bindt de sessie aan client-fingerprint en invalideert bij wijziging.
    # Mitigeert sessie-fixatie: de pre-auth sessie blijft niet bruikbaar na login.
    login_manager.session_protection = "strong"

    # API-routes krijgen 401 JSON i.p.v. redirect naar /login
    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Niet ingelogd"}), 401
        return redirect(url_for("auth.login", next=request.path))


@login_manager.user_loader
def load_user(user_id: str):
    from models.user import User
    return User.query.get(int(user_id))


def admin_required(f):
    """Decorator: vereist de admin-rol. Gebruik náást @login_required."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin-rechten vereist"}), 403
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


LANDING = "beheer.overzicht"


def landing_endpoint_for_user(user) -> str:
    return LANDING


def landing_endpoint() -> str:
    return LANDING


def _safe_next_url(url: str) -> str:
    """Valideer dat redirect-URL relatief en veilig is (geen open redirect)."""
    from urllib.parse import urlparse
    if not url:
        return url_for(landing_endpoint())
    parsed = urlparse(url)
    if parsed.netloc or parsed.scheme or not url.startswith("/"):
        return url_for(landing_endpoint())
    return url


def _get_client_ip() -> str:
    # ProxyFix (x_for=1) heeft request.remote_addr al herschreven naar het echte
    # client-IP. We lezen alleen remote_addr — nooit rechtstreeks X-Forwarded-For.
    return request.remote_addr or ""


# ── LOGIN ROUTES ──────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    from models.user import User

    if current_user.is_authenticated:
        return redirect(url_for(landing_endpoint()))

    if request.method == "POST":

        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        # Alleen een EXPLICIETE deep-link 'next' bewaren; de rol-afhankelijke
        # startpagina wordt ná authenticatie bepaald (verify_2fa).
        _explicit_next = request.form.get("next") or request.args.get("next")
        next_url = _safe_next_url(_explicit_next) if _explicit_next else ""

        # Onderhoudsgrendel: alleen de adressen op de lijst mogen naar binnen.
        # Bewust een duidelijke melding — een collega moet niet gaan denken dat
        # zijn wachtwoord stuk is.
        import lockdown
        if not lockdown.mag_inloggen(email):
            from flask import current_app
            current_app.logger.warning("[LOCKDOWN] Login geweigerd voor %s", email)
            flash(lockdown.BOODSCHAP, "error")
            return render_template("login.html", next=next_url)

        user = User.query.filter_by(email=email, active=True).first()

        if not user:
            flash("E-mailadres of wachtwoord onjuist.", "error")
            return render_template("login.html", next=next_url)

        if user.is_locked():
            flash("Account tijdelijk geblokkeerd. Probeer later opnieuw.", "error")
            return render_template("login.html", next=next_url)

        if not bcrypt.check_password_hash(user.password_hash, password):
            user.record_failed_login()
            flash("E-mailadres of wachtwoord onjuist.", "error")
            return render_template("login.html", next=next_url)

        # Wachtwoord correct: start 2FA-flow
        session["pending_2fa_user_id"] = user.id
        session["pending_2fa_next"]    = next_url

        if not user.totp_enabled:
            # Eerste keer: verplichte 2FA-setup
            return redirect(url_for("auth.setup_2fa"))
        return redirect(url_for("auth.verify_2fa"))

    return render_template("login.html", next=_safe_next_url(request.args.get("next", "")))


@auth_bp.route("/2fa", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def verify_2fa():
    """Tweede stap: TOTP-verificatie."""
    user_id = session.get("pending_2fa_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))

    from models.user import User
    import pyotp

    user = User.query.get(user_id)
    if not user or not user.totp_secret:
        session.pop("pending_2fa_user_id", None)
        return redirect(url_for("auth.login"))

    # De grendel kan aangaan terwijl iemand tussen stap 1 en 2 zit.
    import lockdown
    if not lockdown.mag_inloggen(user.email):
        session.pop("pending_2fa_user_id", None)
        flash(lockdown.BOODSCHAP, "error")
        return redirect(url_for("auth.login"))

    error = None
    if request.method == "POST":
        # Account-lockout geldt ook voor de 2FA-stap (anti brute-force op TOTP-codes)
        if user.is_locked():
            session.pop("pending_2fa_user_id", None)
            flash("Account tijdelijk geblokkeerd. Probeer later opnieuw.", "error")
            return redirect(url_for("auth.login"))

        code = request.form.get("code", "").replace(" ", "")

        # Ontsleutel TOTP-geheim (Fernet-versleuteld in DB)
        try:
            from crypto import decrypt_totp_secret, is_totp_encrypted
            if is_totp_encrypted(user.totp_secret):
                totp_secret = decrypt_totp_secret(user.totp_secret)
            else:
                totp_secret = user.totp_secret  # Legacy: nog niet versleuteld
        except ValueError:
            error = "2FA-configuratie fout. Neem contact op met de beheerder."
            return render_template("2fa_verify.html", error=error)

        totp = pyotp.TOTP(totp_secret)
        if totp.verify(code, valid_window=1):
            _nxt = session.pop("pending_2fa_next", "")
            session.pop("pending_2fa_user_id", None)
            user.record_successful_login(ip=_get_client_ip())
            session.permanent = True   # activeert PERMANENT_SESSION_LIFETIME (8u)
            login_user(user, remember=False)
            # Expliciete deep-link 'next' respecteren, anders de rol-startpagina.
            next_url = _safe_next_url(_nxt) if _nxt else url_for(landing_endpoint_for_user(user))
            return redirect(next_url)
        # Mislukte TOTP-poging telt mee voor account-lockout (zelfde teller als login)
        user.record_failed_login()
        error = "Ongeldige code. Probeer opnieuw."

    return render_template("2fa_verify.html", error=error)


@auth_bp.route("/2fa/instellen", methods=["GET", "POST"])
def setup_2fa():
    """Verplichte 2FA-setup bij eerste login (of na reset)."""
    user_id = session.get("pending_2fa_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))

    from models.user import User
    from database import db
    import pyotp
    import qrcode
    import io
    import base64

    user = User.query.get(user_id)
    if not user:
        return redirect(url_for("auth.login"))

    import lockdown
    if not lockdown.mag_inloggen(user.email):
        session.pop("pending_2fa_user_id", None)
        flash(lockdown.BOODSCHAP, "error")
        return redirect(url_for("auth.login"))

    error = None

    if request.method == "GET":
        # Genereer nieuw geheim en sla op in session (nog niet in DB)
        secret = pyotp.random_base32()
        session["pending_totp_secret"] = secret
    else:
        secret = session.get("pending_totp_secret")

    # QR-code genereren
    totp      = pyotp.TOTP(secret)
    otp_uri   = totp.provisioning_uri(name=user.email, issuer_name="doobedoo MCP")
    qr_img    = qrcode.make(otp_uri)
    buf       = io.BytesIO()
    qr_img.save(buf, format="PNG")
    qr_b64    = base64.b64encode(buf.getvalue()).decode()

    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        if pyotp.TOTP(secret).verify(code, valid_window=1):
            # Versleutel het TOTP-geheim voor opslag in de DB
            from crypto import encrypt_totp_secret
            user.totp_secret  = encrypt_totp_secret(secret)
            user.totp_enabled = True
            db.session.commit()
            session.pop("pending_totp_secret", None)
            next_url = _safe_next_url(session.pop("pending_2fa_next", "") or url_for(LANDING))
            session.pop("pending_2fa_user_id", None)
            user.record_successful_login(ip=_get_client_ip())
            session.permanent = True   # activeert PERMANENT_SESSION_LIFETIME (8u)
            login_user(user, remember=False)
            return redirect(next_url)
        error = "Ongeldige code. Scan de QR-code opnieuw en probeer het opnieuw."

    return render_template("2fa_setup.html", qr_b64=qr_b64, secret=secret, error=error)


@auth_bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    logout_user()
    flash("Je bent uitgelogd.", "info")
    return redirect(url_for("auth.login"))


# ── BOOTSTRAP ─────────────────────────────────────────────────────────────────

def seed_admin_user_if_empty():
    """
    Maak de eerste admin-gebruiker aan als de users-tabel leeg is.
    Gebruik ADMIN_EMAIL en ADMIN_PASSWORD env vars.
    Roep aan vanuit app.py binnen een app-context.
    """
    from models.user import User
    from database import db

    if User.query.count() > 0:
        return

    email    = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not email or not password:
        print(
            "[AUTH] ⚠️  Geen gebruikers in DB en ADMIN_EMAIL/ADMIN_PASSWORD niet ingesteld.\n"
            "          Stel deze env vars in of voeg handmatig een gebruiker toe."
        )
        return

    pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
    admin   = User(email=email, name="Beheerder", password_hash=pw_hash, role="admin",
                   totp_enabled=False)
    db.session.add(admin)
    db.session.commit()
    print(f"[AUTH] ✅ Admin-gebruiker aangemaakt: {email}")
